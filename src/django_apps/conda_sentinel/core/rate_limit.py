"""The collector base's rate limiter, and one of the two modules that touch the cache.

`CPM-AD-20`: "rate limiting, retry with backoff, timeouts and caching live in a
shared collector base in `core`, not per collector". This is the rate-limiting
half. A collector *declares* a `RateLimit` and implements nothing; the base
consults this limiter before every outbound call, and a call with no token left
is refused rather than issued (`core/collection.py` says what the refusal then
writes: a `failed` ledger row and an evidence row carrying `error`).

`core/response_cache.py` is the other module that reaches the cache, and it owns
the caching clause of the same `CPM-AD-20` sentence. The split is by *what is
stored* rather than by size: this module owns **how many requests may be
issued**, that one owns **what a request need not ask for again**. They share a
backend and share nothing else -- different key namespaces (`cpm:rate-limit`
against `cpm:response`), different lifetimes, and different failure modes: an
expired counter here is a fresh window, while an unusable entry there is one
extra fetch. Folding them together would put a counter and a body under one
`clear()`.

**The allowance counts requests, not collections.** `acquire` takes a `cost`, and
the base charges `1 + retries` for one collection because a mounted retry policy
may issue that many real requests against the source. Consulting the limiter once
per collection while the transport retried underneath it would make a declared
"sixty calls a minute" mean up to two hundred and forty -- which is exactly the
reconciliation `CPM-AD-20` puts retry and rate limiting in the same base to make
possible. The cost is charged up front, so a collection either has its whole
worst case available or does not start.

**It reads `django.core.cache` through its public API and nothing below it, and
that is a rule rather than a preference.** `config/settings/local.py` argues, in
its own words, that the LocMem substitution is a *substitution* precisely because
the cache API is preserved -- "no call site may branch on which backend is
active. The moment one does, this stops being a stand-in and becomes a second
code path that local runs never exercise." So this module calls `add`, `incr`,
`set` and nothing that would tell it what is underneath: no `settings.CACHES`, no
backend class, no `isinstance`. `tests/unit/django_apps/test_rate_limit.py`
asserts the call set, and `tests/unit/django_apps/test_collector_base_audit.py`
asserts that the only modules under `src/` reaching the cache at all are this one
and `core/response_cache.py` -- recorded per module and per call, so a third one
fails that file exactly as a second one would have.

**The consequence of that rule, stated rather than discovered.** Under LocMem the
counter lives in one process, so a local run with two workers rate-limits twice
as fast as production does against Redis. That is a property of the substitution,
not of this limiter, and the correct response to noticing it is not a branch
here -- it is to remember that a local run is a stand-in.

**A fixed window, not a sliding one, and the cache API is why.** What a cache
offers atomically is "create if absent, with a time to live" and "increment", and
those two compose into a counter that resets on a boundary. A sliding window
needs a timestamp list read-modify-written under a lock, which the cache API
cannot do atomically and which every backend would implement differently -- the
branching this module exists not to do, arriving as a data structure. The cost is
that a collector may spend its whole allowance just before a boundary and its
whole next allowance just after; the benefit is one code path on every backend.

**The window boundary comes from the injected clock** (`CPM-AD-26`), never from
the cache's own expiry and never from a wall clock read here, and a naive instant
is *refused* rather than interpreted. A naive value is read as local time, so two
workers in different zones would compute different window indices and quietly
stop sharing the allowance -- the same class of silent wrongness
`core/ledger.py`'s `_require_aware` and `AppendOnlyModel.save()`'s refusal both
exist for. The time to live is still handed to the cache, because a counter that
outlived its window would throttle a collector forever, but which window a call
belongs to is decided from `now`, so a window test is a statement about the rule
rather than about how long the test took.

**Refused, not delayed.** A caller with no token left is told so and does not
wait. Waiting inside a Celery task holds a worker slot doing nothing against the
inherited limits (`CPM-AD-9`: a 60-second soft limit and a 5-minute hard one),
and `CPM-AD-23` already fixes the atomic unit as one package -- so the useful
response to exhaustion is to record it and let the next scheduled run collect.

**A refused credential is remembered for the rest of the window**
(`CPM-OPERATE-S05`). A `401` to a request that carried the declared
credential answers identically however many times it is asked, and a daily
sweep would otherwise write ten thousand `failed` runs against one wrong token
at the authenticated rate. So the collector base asks this module to remember
the refusal under the collector's name for the current window, and asks it
back before every fetch: a later collection in the same window fails fast
with no call issued, and the memo dies with the window -- keyed by the same
window index the counter uses and given the same time to live -- so the next
window tries the credential once. That is the blast radius: one refused call
per window per collector. Held here rather than in a table because it is a
statement about *whether a call may be issued*, which is this module's whole
subject, and because this is one of the two modules licensed to read the cache.

**On the `AD-` prefix.** A bare `AD-n` in this repository is an *inherited*
platform decision; a decision from this product's own architecture spine always
carries the `CPM-` prefix.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from typing import TYPE_CHECKING
from typing import Final
from typing import Protocol
from typing import runtime_checkable

import structlog
from django.core.cache import cache

from conda_sentinel.core.clock import is_aware

if TYPE_CHECKING:
    from datetime import timedelta

__all__ = [
    "INITIAL_COUNT",
    "KEY_PREFIX",
    "REFUSAL_KEY_PREFIX",
    "WINDOW_EXPIRED_EVENT",
    "CacheRateLimiter",
    "CredentialRefusal",
    "RateLimit",
    "RateLimitError",
    "RateLimiter",
    "refusal_key",
    "window_end",
    "window_key",
]

logger = structlog.get_logger(__name__)

#: The namespace every counter this module writes lives under.
#:
#: A prefix rather than a bare collector name because the cache is shared with
#: whatever else the component puts there -- the platform's own uses, and
#: anything a later story adds -- and a key called `pypi` would collide with the
#: first cache entry somebody names after the same source.
KEY_PREFIX: Final[str] = "cpm:rate-limit"

#: The namespace a remembered credential refusal lives under. Separate from the
#: counter's so that neither read can ever be answered by the other's value.
REFUSAL_KEY_PREFIX: Final[str] = "cpm:credential-refused"

#: The event logged when a window's counter vanished between the two cache calls.
#: Dotted, as `config/health/drain.py`'s `drain.begin` and
#: `config/health/views.py`'s `health.readiness_refused_draining` are; named so
#: the case that asserts the log and the code that emits it cannot drift.
WINDOW_EXPIRED_EVENT: Final[str] = "rate_limit.window_expired"

#: The value a counter is created with, before the call that created it is
#: counted. Named because the two-step "create at zero, then increment" is the
#: whole reason this works on a backend with no compare-and-set: `add` is a
#: no-op when the key exists, so two processes racing to start a window both end
#: up incrementing the same counter rather than one of them resetting it.
INITIAL_COUNT: Final[int] = 0

#: What one call costs when the caller does not say. One, which is what a caller
#: issuing exactly one request spends; the collector base says otherwise because
#: its transport may retry.
DEFAULT_COST: Final[int] = 1


class RateLimitError(ValueError):
    """A declared rate limit could not describe a rate.

    A `ValueError` subclass rather than a bare one, on the same terms as
    `core/outcomes.py`'s `OutcomeVocabularyError` and
    `core/collection.py`'s `CollectorConfigurationError`: a limit declaring zero
    calls or a zero-length window is a defect in the *declaration*, and every
    declaration defect in this story is a `ValueError` so that a caller catching
    one catches them all. A source being unreachable is not a `ValueError`, which
    is why `core/transport.py`'s `TransportError` is not one.
    """


@dataclass(frozen=True, slots=True)
class RateLimit:
    """How many outbound requests a collector may make, and over what interval.

    Declared by the collector as configuration (`CPM-AD-20`) and applied by the
    base. Frozen, so a collector cannot widen its own allowance at run time --
    which is the shape the rule exists to prevent: eight collectors each deciding
    at the call site how hard they may push a shared source.

    Attributes:
        calls: How many *requests* the window permits -- see the module
            docstring: one collection costs `1 + retries` of these. At least
            one; a limit of zero is not a rate, it is a collector that never
            runs, and saying so by configuration would leave the run ledger
            recording failures for a source nobody meant to disable.
        per: How long the window is. Refused below a second: the counter's time
            to live is handed to the cache in whole seconds, so a sub-second
            window would round to zero and mean "never expires", which is a
            collector throttled permanently by a value that looked generous.

    """

    calls: int
    per: timedelta

    def __post_init__(self) -> None:
        """Refuse a limit that cannot describe a rate.

        Raises:
            RateLimitError: When `calls` is below one, or when `per` is shorter
                than a whole second. Refused at construction, so a misdeclared
                limit fails where it is written rather than in a worker.

        """
        if self.calls < 1:
            message = (
                f"a rate limit of {self.calls!r} calls is not a rate. A collector that should not run is one "
                f"that is not scheduled, not one whose allowance is zero."
            )
            raise RateLimitError(message)
        if self.window_seconds < 1:
            message = (
                f"a rate-limit window of {self.per!r} is shorter than the whole second the cache counts in; "
                f"it would round to a counter that never expires and throttle the collector permanently."
            )
            raise RateLimitError(message)

    @property
    def window_seconds(self) -> int:
        """Return the window length in whole seconds.

        Returns:
            The window as the cache's time to live takes it. Truncated rather
            than rounded, so the declared window is never *longer* than what was
            asked for -- a limiter that throttled for longer than its
            declaration says would be the harder of the two mistakes to notice.

        """
        return int(self.per.total_seconds())


def window_key(*, collector: str, limit: RateLimit, now: datetime) -> str:
    """Return the cache key holding one collector's counter for one window.

    The window index is `now` divided by the window length, so a boundary is a
    property of the instant rather than of when a counter happened to be
    created. Two processes reading the same clock therefore agree on which
    window they are in without coordinating, which is what makes a counter
    shared across workers meaningful at all.

    Args:
        collector: The collector's declared name, which is also what its ledger
            rows carry (`CPM-FR-39`).
        limit: The declared allowance, whose window length sets the boundary.
        now: The instant from the injected clock (`CPM-AD-26`). Must be aware.

    Returns:
        A key under `KEY_PREFIX`, naming the collector and the window index.

    Raises:
        RateLimitError: When `now` is naive. `timestamp()` reads a naive instant
            as *local* time, so two workers in different zones would land in
            different windows and stop sharing the allowance -- silently, and
            only under load. `core/ledger.py` refuses a naive instant for the
            same class of reason.

    """
    if not is_aware(now):
        message = (
            f"a rate-limit window cannot be decided from the naive instant {now!r}. The instant comes from a "
            f"Clock, which always answers in UTC (CPM-AD-26); a naive value is read as local time, so two "
            f"workers in different zones would compute different windows and stop sharing the allowance."
        )
        raise RateLimitError(message)
    window = int(now.timestamp()) // limit.window_seconds
    return f"{KEY_PREFIX}:{collector}:{window}"


def window_end(*, limit: RateLimit, now: datetime) -> datetime:
    """Return the instant the window `now` falls in turns over.

    The same arithmetic `window_key` uses, read the other way: the next window
    index times the window length, as an aware UTC instant. It is what a
    remembered refusal reports as "when the credential is tried again".

    Args:
        limit: The declared allowance, whose window length sets the boundary.
        now: The instant from the injected clock. Must be aware.

    Returns:
        The first instant of the next window, in UTC.

    Raises:
        RateLimitError: When `now` is naive, on `window_key`'s terms.

    """
    if not is_aware(now):
        message = (
            f"a rate-limit window cannot be decided from the naive instant {now!r}. The instant comes from a "
            f"Clock, which always answers in UTC (CPM-AD-26); a naive value is read as local time."
        )
        raise RateLimitError(message)
    window = int(now.timestamp()) // limit.window_seconds
    return datetime.fromtimestamp((window + 1) * limit.window_seconds, tz=UTC)


def refusal_key(*, collector: str, limit: RateLimit, now: datetime) -> str:
    """Return the cache key holding one collector's remembered credential refusal for one window.

    Keyed by the same window index as the counter, so the memo turns over when
    the allowance does and nothing has to clear it.

    Args:
        collector: The collector's declared name.
        limit: The declared allowance, whose window length sets the boundary.
        now: The instant from the injected clock. Must be aware.

    Returns:
        A key under `REFUSAL_KEY_PREFIX`, naming the collector and the window.

    Raises:
        RateLimitError: When `now` is naive, on `window_key`'s terms.

    """
    counter = window_key(collector=collector, limit=limit, now=now)
    return counter.replace(KEY_PREFIX, REFUSAL_KEY_PREFIX, 1)


@dataclass(frozen=True, slots=True)
class CredentialRefusal:
    """A credential refusal remembered for the rest of a window.

    Attributes:
        detail: What the run that met the refusal recorded -- the base's own
            sentence naming the host, never a header.
        until: When the window turns and the credential is tried again.

    """

    detail: str
    until: datetime


#: The two keys a remembered refusal is stored under in the cache. A plain
#: dictionary of primitives rather than the dataclass itself, so a memo written
#: by one build of this component is readable by the next.
_REFUSAL_DETAIL: Final[str] = "detail"
_REFUSAL_UNTIL: Final[str] = "until"


@runtime_checkable
class RateLimiter(Protocol):
    """Something that can be asked whether more calls are permitted.

    Three methods, all one question: `acquire` asks whether the allowance
    covers the next calls, and the pair `remember_refusal`/`refusal` ask
    whether a credential the source already refused this window should be
    sent again (`CPM-OPERATE-S05`) -- which is the same question, with the
    answer known in advance. A base that takes a `RateLimiter` declares that
    it throttles and declares nothing else, and a test supplies one that
    answers from memory without standing up a cache.

    `runtime_checkable` so a test can assert an implementation satisfies it.
    """

    def acquire(self, *, collector: str, limit: RateLimit, now: datetime, cost: int = DEFAULT_COST) -> bool:
        """Count `cost` calls against the collector's allowance.

        The docstring is the whole body, for the reason `core/clock.py`'s
        `Clock.now` gives: a protocol method is never executed, and an `...`
        would be a permanently uncovered line that only a banned `pragma` could
        excuse.

        Args:
            collector: The collector's declared name.
            limit: Its declared allowance.
            now: The instant the window is decided from.
            cost: How many requests the caller is about to issue. The collector
                base charges its whole retry budget up front; see the module
                docstring.

        Returns:
            True when the calls may be issued. False when the allowance for this
            window cannot cover them, in which case the caller must issue none.

        """

    def remember_refusal(self, *, collector: str, limit: RateLimit, now: datetime, detail: str) -> datetime:
        """Remember that the source refused the collector's credential, for the rest of this window.

        Args:
            collector: The collector's declared name.
            limit: Its declared allowance, whose window the memo lives for.
            now: The instant the refusal was met.
            detail: What the run recorded about it -- never a header.

        Returns:
            When the window turns, which is when the credential is tried again.

        """

    def refusal(self, *, collector: str, limit: RateLimit, now: datetime) -> CredentialRefusal | None:
        """Return the refusal remembered for this collector's current window, if any.

        Args:
            collector: The collector's declared name.
            limit: Its declared allowance.
            now: The instant the window is decided from.

        Returns:
            The remembered refusal, or `None` when the credential has not been
            refused this window and a call may be issued.

        """


class CacheRateLimiter:
    """The shared counter, over `django.core.cache`'s public API.

    Stateless: every instance reads the same cache, so a worker holding one per
    collector and a worker holding one for everything behave identically. What
    holds the state is the cache, which is what makes the limit shared across
    processes wherever the backend is (Redis in a deployment; see the module
    docstring for what LocMem changes and why nothing here branches on it).
    """

    def acquire(self, *, collector: str, limit: RateLimit, now: datetime, cost: int = DEFAULT_COST) -> bool:
        """Count `cost` calls against the collector's allowance for the current window.

        Two cache calls, in this order and no other. `add` creates the counter
        at zero only if the window has no counter yet, which is a no-op for
        every process but the first; `incr` then counts the calls. Doing it the
        other way -- read, compare, write -- is the read-modify-write that two
        workers lose a count to, and there is no cache API that makes it atomic.

        The cost is charged whether or not it fits, deliberately: a refused
        caller has still consumed the window as far as every other worker is
        concerned, which is the conservative reading and the one that keeps a
        stampede of refusals from each finding room.

        Args:
            collector: The collector's declared name.
            limit: Its declared allowance.
            now: The instant from the injected clock, which decides the window.
            cost: How many requests are about to be issued.

        Returns:
            True when the calls are within the allowance. False when the window's
            allowance cannot cover them -- the caller must issue none, and
            `core/collection.py` records the refusal rather than waiting.

        Raises:
            RateLimitError: When `cost` is below one, or when `now` is naive.

        """
        if cost < DEFAULT_COST:
            message = f"a call cannot cost {cost!r} requests; a caller issuing no request does not need a token"
            raise RateLimitError(message)
        key = window_key(collector=collector, limit=limit, now=now)
        cache.add(key, INITIAL_COUNT, limit.window_seconds)
        try:
            used = cache.incr(key, cost)
        except ValueError:
            # `incr` raises when the key is gone, which happens when the window
            # expired between the two calls above. Never swallowed: it is logged
            # and the calls are counted as the first of a fresh window, which is
            # what they now are. Treating it as a refusal instead would throttle
            # a collector for losing a race it cannot see, and re-raising would
            # turn a cache expiry into a failed run.
            logger.info(WINDOW_EXPIRED_EVENT, collector=collector, cache_key=key, cost=cost)
            used = INITIAL_COUNT + cost
            cache.set(key, used, limit.window_seconds)
        return used <= limit.calls

    def remember_refusal(self, *, collector: str, limit: RateLimit, now: datetime, detail: str) -> datetime:
        """Write the refusal under the window's key, to live exactly as long as the window's counter.

        Args:
            collector: The collector's declared name.
            limit: Its declared allowance.
            now: The instant the refusal was met.
            detail: What the run recorded about it.

        Returns:
            When the window turns.

        """
        until = window_end(limit=limit, now=now)
        memo = {_REFUSAL_DETAIL: detail, _REFUSAL_UNTIL: until.isoformat()}
        cache.set(refusal_key(collector=collector, limit=limit, now=now), memo, limit.window_seconds)
        return until

    def refusal(self, *, collector: str, limit: RateLimit, now: datetime) -> CredentialRefusal | None:
        """Read the window's refusal back, or nothing when none was remembered.

        Args:
            collector: The collector's declared name.
            limit: Its declared allowance.
            now: The instant the window is decided from.

        Returns:
            The remembered refusal, or `None` -- also `None` for a value under
            the key that is not the shape this module writes, which is treated
            as no memo rather than as a reason to refuse a call nobody refused.

        """
        remembered = cache.get(refusal_key(collector=collector, limit=limit, now=now))
        if not isinstance(remembered, dict):
            return None
        detail = remembered.get(_REFUSAL_DETAIL)
        until = remembered.get(_REFUSAL_UNTIL)
        if not isinstance(detail, str) or not isinstance(until, str):
            return None
        try:
            return CredentialRefusal(detail=detail, until=datetime.fromisoformat(until))
        except ValueError:
            return None
