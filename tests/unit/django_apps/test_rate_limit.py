"""The limiter over a real cache, and the rule that it must not know which one.

`CPM-AD-20` puts rate limiting in the shared collector base. What makes that a
single implementation rather than a shared *name* is the second half of the rule,
which `config/settings/local.py` states in its own words: the LocMem
substitution preserves the cache API precisely so that no call site branches on
the backend, and "the moment one does, this stops being a stand-in and becomes a
second code path that local runs never exercise."

So there are two kinds of case here, and both are necessary.

**The behaviour, against a real cache.** Not a mock: the counter is written to
`django.core.cache` and read back, so the allowance, the refusal past it, the
per-collector separation and the window boundary are proved through the same API
a deployment uses. The backend under the suite is LocMem
(`config/settings/test.py` declares it deliberately rather than inheriting
Django's default), which is in-process memory -- no database, no network, so this
belongs in the unit tier.

**The rule, against the source.** A behavioural case cannot show that the module
does not branch: a branch on the backend would pass every assertion below,
because the suite only ever runs one backend. So the module is parsed, and what
it calls on the cache is compared against the public API -- and it is checked to
mention no backend at all.

`tests/unit/django_apps/test_collector_base_audit.py` holds the other direction:
that no module *outside* this one reaches the cache in the first place.

**On the `AD-` prefix.** A bare `AD-n` in this repository is an *inherited*
platform decision; a decision from this product's own architecture spine always
carries the `CPM-` prefix.
"""

from __future__ import annotations

import ast
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import TYPE_CHECKING
from typing import Final

import pytest
import structlog
from django.core.cache import cache

from conda_sentinel.core import rate_limit
from conda_sentinel.core.rate_limit import KEY_PREFIX
from conda_sentinel.core.rate_limit import REFUSAL_KEY_PREFIX
from conda_sentinel.core.rate_limit import WINDOW_EXPIRED_EVENT
from conda_sentinel.core.rate_limit import CacheRateLimiter
from conda_sentinel.core.rate_limit import CredentialRefusal
from conda_sentinel.core.rate_limit import RateLimit
from conda_sentinel.core.rate_limit import RateLimiter
from conda_sentinel.core.rate_limit import RateLimitError
from conda_sentinel.core.rate_limit import refusal_key
from conda_sentinel.core.rate_limit import window_end
from conda_sentinel.core.rate_limit import window_key
from tests.clocks import FIXED_INSTANT
from tests.collectors import A_NAIVE_INSTANT
from tests.collectors import cleared_cache
from tests.source_scan import SRC_ROOT
from tests.source_scan import dotted_name
from tests.source_scan import parse

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from structlog.typing import EventDict

#: The module this file is about, relative to `src/`.
LIMITER_MODULE: Final[str] = "django_apps/conda_sentinel/core/rate_limit.py"

#: The cache methods the limiter is permitted to call.
#:
#: `add` creates the window's counter, `incr` counts a call, and `set` recovers
#: the one race the two of them have; `set` and `get` are also how a credential
#: refusal is remembered and read back (`CPM-OPERATE-S05`). All four are
#: `BaseCache`'s own public API, offered identically by every backend -- which
#: is the property that makes LocMem a substitution rather than a second code
#: path.
PERMITTED_CACHE_CALLS: Final[frozenset[str]] = frozenset({"add", "get", "incr", "set"})

#: Names that would mean the module knows what is underneath it. `CACHES` is the
#: settings table; the two class names are the backends `config/settings/*.py`
#: configure. Any of them appearing here would be the branch the rule forbids,
#: and a case that only ran one backend could never notice.
BACKEND_TELLS: Final[frozenset[str]] = frozenset({"CACHES", "LocMemCache", "RedisCache", "DummyCache"})

#: A small allowance, so a case can spend it in a comprehension and still read.
ALLOWANCE: Final[int] = 3

#: The whole-second part of the window the truncation case declares. Named so the
#: case asserts a stated expectation rather than repeating a literal on both
#: sides of the comparison.
WHOLE_SECONDS: Final[int] = 90

#: The window every case here declares. A minute: long enough that
#: `FIXED_INSTANT` and an instant a few seconds later are in the same window, so
#: crossing a boundary is something a case does on purpose.
WINDOW: Final[timedelta] = timedelta(minutes=1)

#: The limit every behavioural case declares.
A_LIMIT: Final[RateLimit] = RateLimit(calls=ALLOWANCE, per=WINDOW)

#: The collector names the cases count against. Two, because "one collector's
#: exhaustion does not throttle another" is a claim about the key rather than
#: about the counter.
A_COLLECTOR: Final[str] = "cpm-fixture-limited"
ANOTHER_COLLECTOR: Final[str] = "cpm-fixture-other-limited"

#: The event the capture fixture emits to prove it can see this module's logger.
_CAPTURE_CONTROL: Final[str] = "rate_limit.capture_control"


@pytest.fixture(autouse=True)
def _empty_cache() -> Iterator[None]:
    """Start and end every case with no counters in the cache.

    Autouse, and the body lives in `tests/collectors.py` because every module
    that touches the cache needs the identical guard: `autouse` fixtures that
    can drift apart are exactly the duplication that module's own docstring
    argues against. How many there are is deliberately not stated -- it has
    grown twice already.

    Yields:
        Nothing; the fixture is entirely its two side effects.

    """
    with cleared_cache():
        yield


@pytest.fixture
def captured_events(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[EventDict]]:
    """Capture what the limiter logs, with the two guards the plain helper lacks.

    The reasoning is `tests/unit/test_health_views.py`'s and `tests/unit/test_drain.py`'s
    and is not restated: the module-scope logger is rebound so `capture_logs`
    binds a fresh proxy inside its own processor chain, and a control event
    proves the capture is live before the case runs, so an assertion over an
    empty list fails here and says why rather than reporting that the limiter
    logged nothing.

    Args:
        monkeypatch: pytest's patcher, which restores the module's own logger.

    Yields:
        The captured events, in order.

    """
    monkeypatch.setattr(rate_limit, "logger", structlog.get_logger(rate_limit.__name__))
    with structlog.testing.capture_logs() as captured:
        rate_limit.logger.info(_CAPTURE_CONTROL)
        assert [event["event"] for event in captured] == [_CAPTURE_CONTROL], (
            "structlog.testing.capture_logs() cannot see core/rate_limit.py's logger, so every "
            "assertion over what it logged would be vacuous"
        )
        captured.clear()
        yield captured


def test_a_limit_of_no_calls_is_refused() -> None:
    """A collector that should not run is one that is not scheduled.

    Zero would be accepted silently by a counter comparison and would produce a
    collector whose every run wrote a `failed` ledger row and an `error`
    evidence row -- a source that looks broken because it was switched off by
    configuration nobody reads as a switch.
    """
    with pytest.raises(RateLimitError, match="not a rate"):
        RateLimit(calls=0, per=WINDOW)


def test_a_window_shorter_than_a_second_is_refused() -> None:
    """The failure mode is the opposite of what the value looks like.

    Half a second truncates to a zero-second time to live, which every cache
    backend reads as "never expires" -- so the counter for the first window
    lives forever and the collector is throttled permanently by a value that
    looked like the most generous one available.
    """
    with pytest.raises(RateLimitError, match="whole second"):
        RateLimit(calls=1, per=timedelta(milliseconds=500))


def test_the_window_is_counted_in_whole_truncated_seconds() -> None:
    """Truncated rather than rounded, so a window is never longer than declared.

    A limiter that throttled for longer than its declaration says is the harder
    of the two mistakes to notice: nothing fails, calls are simply not made.
    """
    just_under = timedelta(seconds=WHOLE_SECONDS, milliseconds=999)

    assert RateLimit(calls=1, per=just_under).window_seconds == WHOLE_SECONDS


def test_two_instants_in_one_window_share_a_key() -> None:
    """The counter is shared across the window, which is what makes it a rate."""
    early = window_key(collector=A_COLLECTOR, limit=A_LIMIT, now=FIXED_INSTANT)
    late = window_key(collector=A_COLLECTOR, limit=A_LIMIT, now=FIXED_INSTANT + timedelta(seconds=30))

    assert early == late
    assert early.startswith(f"{KEY_PREFIX}:{A_COLLECTOR}:")


def test_an_instant_in_the_next_window_gets_a_different_key() -> None:
    """The boundary comes from the clock, never from when a counter happened to be created.

    Two workers reading the same clock therefore agree on which window they are
    in without coordinating, which is the whole reason the index is arithmetic on
    the instant rather than a value stored beside the counter.
    """
    this_window = window_key(collector=A_COLLECTOR, limit=A_LIMIT, now=FIXED_INSTANT)
    next_window = window_key(collector=A_COLLECTOR, limit=A_LIMIT, now=FIXED_INSTANT + WINDOW)

    assert this_window != next_window


def test_two_collectors_do_not_share_an_allowance() -> None:
    """One source being pushed hard must not throttle a collector reading another."""
    assert window_key(collector=A_COLLECTOR, limit=A_LIMIT, now=FIXED_INSTANT) != window_key(
        collector=ANOTHER_COLLECTOR,
        limit=A_LIMIT,
        now=FIXED_INSTANT,
    )


def test_the_allowance_is_spent_exactly_once_per_call() -> None:
    """Three calls permitted, the fourth refused -- against a real cache.

    The boundary is the assertion: a limiter that permitted the call *equal* to
    the allowance and one that refused it differ by a single comparison, and only
    one of them matches the number a collector declared.
    """
    limiter = CacheRateLimiter()

    verdicts = [limiter.acquire(collector=A_COLLECTOR, limit=A_LIMIT, now=FIXED_INSTANT) for _ in range(ALLOWANCE + 1)]

    assert verdicts == [True] * ALLOWANCE + [False]


def test_a_spent_allowance_does_not_reach_another_collector() -> None:
    """The key separation of the case above, proved through the counter itself."""
    limiter = CacheRateLimiter()
    for _ in range(ALLOWANCE + 1):
        limiter.acquire(collector=A_COLLECTOR, limit=A_LIMIT, now=FIXED_INSTANT)

    assert limiter.acquire(collector=ANOTHER_COLLECTOR, limit=A_LIMIT, now=FIXED_INSTANT) is True


def test_the_next_window_restores_the_allowance() -> None:
    """A fixed window resets on its boundary, which is what makes it a rate rather than a quota.

    The second window is reached by handing the limiter a later instant, never by
    waiting: `CPM-AD-26` exists so that a window assertion is a statement about
    the rule rather than about how long the test took.
    """
    limiter = CacheRateLimiter()
    for _ in range(ALLOWANCE + 1):
        limiter.acquire(collector=A_COLLECTOR, limit=A_LIMIT, now=FIXED_INSTANT)

    assert limiter.acquire(collector=A_COLLECTOR, limit=A_LIMIT, now=FIXED_INSTANT + WINDOW) is True


def test_a_counter_that_expired_between_the_two_calls_starts_a_fresh_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The race the two-call sequence has, and the reading that is safe.

    `add` then `incr` is the only pair the cache API makes atomic enough to be
    useful, and it has exactly one gap: the window can expire between them, and
    `incr` then raises because the key is gone. Refusing the call would throttle
    a collector for losing a race it cannot see; re-raising would turn a cache
    expiry into a failed run. Counting it as the first call of the window it is
    now in is what actually happened.

    Patched on the cache object rather than on a backend class, so this case says
    nothing about which backend is underneath -- which is the rule this module is
    also here to defend.
    """

    def _expired(key: str, delta: int = 1, version: int | None = None) -> int:
        message = f"Key '{key}' not found"
        raise ValueError(message)

    monkeypatch.setattr(cache, "incr", _expired)
    limiter = CacheRateLimiter()

    permitted = limiter.acquire(collector=A_COLLECTOR, limit=A_LIMIT, now=FIXED_INSTANT)

    monkeypatch.undo()
    assert permitted is True
    assert cache.get(window_key(collector=A_COLLECTOR, limit=A_LIMIT, now=FIXED_INSTANT)) == 1


def test_the_cache_limiter_satisfies_the_protocol() -> None:
    """The base takes a `RateLimiter`, so a test double needs no inheritance."""
    assert isinstance(CacheRateLimiter(), RateLimiter)


def test_something_without_an_acquire_is_not_a_limiter() -> None:
    """The negative control for the protocol check above."""

    class NotALimiter:
        """Something with no way to answer whether a call is permitted."""

    assert not isinstance(NotALimiter(), RateLimiter)


def test_something_that_only_acquires_is_not_a_limiter_either() -> None:
    """`CPM-OPERATE-S05`: the memo pair is part of the contract, so a double lacking it is refused by the check."""

    class OnlyAcquires:
        """A limiter from before the credential memo existed."""

        def acquire(self, *, collector: str, limit: RateLimit, now: datetime, cost: int = 1) -> bool:
            return True

    assert not isinstance(OnlyAcquires(), RateLimiter)


# ---------------------------------------------------------------------------
# The credential-refusal memo (`CPM-OPERATE-S05`).
# ---------------------------------------------------------------------------


def test_the_window_end_is_the_first_instant_of_the_next_window() -> None:
    """The counter's arithmetic read the other way: the next index times the window length, in UTC."""
    at = datetime(2026, 4, 11, 14, 0, 37, tzinfo=UTC)

    end = window_end(limit=A_LIMIT, now=at)

    assert end == datetime(2026, 4, 11, 14, 1, tzinfo=UTC)
    assert window_key(collector=A_COLLECTOR, limit=A_LIMIT, now=end) != window_key(
        collector=A_COLLECTOR,
        limit=A_LIMIT,
        now=at,
    )
    assert window_key(collector=A_COLLECTOR, limit=A_LIMIT, now=end - timedelta(seconds=1)) == window_key(
        collector=A_COLLECTOR,
        limit=A_LIMIT,
        now=at,
    )


def test_the_window_end_refuses_a_naive_instant() -> None:
    """On `window_key`'s terms: a naive instant is local time, and two workers would disagree."""
    with pytest.raises(RateLimitError, match="naive"):
        window_end(limit=A_LIMIT, now=A_NAIVE_INSTANT)


def test_the_refusal_key_names_the_collector_and_the_window_under_its_own_prefix() -> None:
    """Same window index as the counter, different namespace, so neither read answers the other."""
    key = refusal_key(collector=A_COLLECTOR, limit=A_LIMIT, now=FIXED_INSTANT)
    counter = window_key(collector=A_COLLECTOR, limit=A_LIMIT, now=FIXED_INSTANT)

    assert key.startswith(f"{REFUSAL_KEY_PREFIX}:{A_COLLECTOR}:")
    assert key.rpartition(":")[2] == counter.rpartition(":")[2]
    assert key != counter
    assert REFUSAL_KEY_PREFIX != KEY_PREFIX


def test_a_remembered_refusal_is_read_back_for_the_window_and_gone_when_it_turns() -> None:
    """Remember, recall inside the window, nothing after it; another collector sees nothing at all."""
    limiter = CacheRateLimiter()

    until = limiter.remember_refusal(collector=A_COLLECTOR, limit=A_LIMIT, now=FIXED_INSTANT, detail="refused")

    assert until == window_end(limit=A_LIMIT, now=FIXED_INSTANT)
    later_this_window = until - timedelta(seconds=1)
    assert limiter.refusal(collector=A_COLLECTOR, limit=A_LIMIT, now=later_this_window) == CredentialRefusal(
        detail="refused",
        until=until,
    )
    assert limiter.refusal(collector=ANOTHER_COLLECTOR, limit=A_LIMIT, now=later_this_window) is None
    assert limiter.refusal(collector=A_COLLECTOR, limit=A_LIMIT, now=until) is None


def test_a_remembered_refusal_is_given_the_windows_own_time_to_live(monkeypatch: pytest.MonkeyPatch) -> None:
    """The memo cannot outlive the window even if the key arithmetic did: the TTL handed to the cache is `per`."""
    timeouts: list[object] = []
    original = cache.set

    def _recording_set(key: str, value: object, timeout: object = None, *args: object, **kwargs: object) -> None:
        timeouts.append(timeout)
        original(key, value, timeout, *args, **kwargs)

    monkeypatch.setattr(cache, "set", _recording_set)

    CacheRateLimiter().remember_refusal(collector=A_COLLECTOR, limit=A_LIMIT, now=FIXED_INSTANT, detail="refused")

    assert timeouts == [A_LIMIT.window_seconds]


def test_a_memo_that_is_not_the_shape_this_module_writes_reads_as_no_memo() -> None:
    """A foreign value under the key is not a reason to refuse a call nobody refused."""
    limiter = CacheRateLimiter()
    key = refusal_key(collector=A_COLLECTOR, limit=A_LIMIT, now=FIXED_INSTANT)

    for foreign in ("a string", 7, {"detail": "x"}, {"detail": 1, "until": "2026"}, {"detail": "x", "until": "nope"}):
        cache.set(key, foreign, 60)
        assert limiter.refusal(collector=A_COLLECTOR, limit=A_LIMIT, now=FIXED_INSTANT) is None


def test_the_limiter_calls_only_the_public_cache_api() -> None:
    """The rule a behavioural case cannot reach: no call site knows the backend.

    Read off the parsed module rather than by running it, because the suite runs
    exactly one backend: a branch on which one would pass every case above and
    would only be discovered in production, where the *other* branch is the live
    one.
    """
    called = _cache_calls(SRC_ROOT / LIMITER_MODULE)

    assert called != set(), "the limiter should reach the cache; a scan finding nothing proves nothing"
    assert called <= PERMITTED_CACHE_CALLS, f"the limiter calls something outside the cache's public API: {called}"


def test_the_limiter_names_no_cache_backend() -> None:
    """A backend named in code is the branch, whatever shape the branch takes.

    Names and attributes, never string literals: this module's own docstring
    explains at length what LocMem changes and why nothing here branches on it,
    and a detector that read prose would make the explanation an offence. What it
    reads instead is the two spellings a branch actually needs -- a bare name and
    an attribute access, which is what `settings.CACHES[...]` and an imported
    backend class each produce.
    """
    tree = parse(SRC_ROOT / LIMITER_MODULE)

    named = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name) and node.id in BACKEND_TELLS} | {
        node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute) and node.attr in BACKEND_TELLS
    }

    assert named == set(), f"the limiter names a cache backend: {sorted(named)}"


def test_the_backend_detector_would_notice_one() -> None:
    """The anti-vacuity guard for the case above.

    A detector that had stopped recognising a backend reference would report a
    clean module forever. Measured against source text parsed here rather than a
    file on disk: a fixture module under `src/` would be found by every other
    sweep in this repository and would need an exemption of its own.
    """
    branching = ast.parse(
        "from django.conf import settings\n"
        "if settings.CACHES['default']['BACKEND'].endswith('LocMemCache'):\n"
        "    pass\n",
    )

    named = {
        node.attr for node in ast.walk(branching) if isinstance(node, ast.Attribute) and node.attr in BACKEND_TELLS
    }

    assert named == {"CACHES"}


def test_the_window_index_is_read_from_the_instant_it_was_given() -> None:
    """No wall clock, stated as a property rather than trusted to the audit.

    `tests/unit/django_apps/test_clock_audit.py` bans the *form*; this asserts
    the consequence, which is that the same instant always produces the same key
    however long ago the limiter was constructed.
    """
    anchored = datetime(2001, 2, 3, 4, 5, 6, tzinfo=UTC)

    assert window_key(collector=A_COLLECTOR, limit=A_LIMIT, now=anchored) == window_key(
        collector=A_COLLECTOR,
        limit=A_LIMIT,
        now=anchored,
    )


def _cache_calls(path: Path) -> set[str]:
    """Return every method name called on the `cache` object in one module.

    Args:
        path: The module to read.

    Returns:
        The attribute names, so the caller can compare them against the public
        API. Matched on the receiver being spelled `cache`, which is what
        `from django.core.cache import cache` binds and what the audit in
        `test_collector_base_audit.py` resolves properly; here the module is
        known and the simpler match is enough.

    """
    tree = parse(path)
    return {
        dotted_name(node.func).rpartition(".")[2]
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and dotted_name(node.func).startswith("cache.")
    }


def test_a_cost_spends_that_many_of_the_allowance() -> None:
    """The reconciliation with retry, at the limiter's own boundary.

    `CPM-AD-20` puts retry and rate limiting in one base so the two can be made
    to agree. This is the limiter's half: an allowance of three is spent by one
    call costing three, not by three calls. Without it, a collector whose
    transport retries three times would issue four requests per token.
    """
    limiter = CacheRateLimiter()

    first = limiter.acquire(collector=A_COLLECTOR, limit=A_LIMIT, now=FIXED_INSTANT, cost=ALLOWANCE)
    second = limiter.acquire(collector=A_COLLECTOR, limit=A_LIMIT, now=FIXED_INSTANT)

    assert first is True
    assert second is False


def test_a_cost_larger_than_the_allowance_is_refused_before_anything_is_issued() -> None:
    """A collection whose worst case does not fit does not start.

    Charging up front is what makes that true: a caller told "yes" and then
    retrying past the allowance would have spent tokens it was never granted, and
    the limiter would be a suggestion.
    """
    limiter = CacheRateLimiter()

    assert limiter.acquire(collector=A_COLLECTOR, limit=A_LIMIT, now=FIXED_INSTANT, cost=ALLOWANCE + 1) is False


def test_a_call_that_issues_no_request_is_refused_a_token() -> None:
    """Zero is not a cost, and a negative one would hand allowance back."""
    with pytest.raises(RateLimitError, match="cannot cost"):
        CacheRateLimiter().acquire(collector=A_COLLECTOR, limit=A_LIMIT, now=FIXED_INSTANT, cost=0)


def test_a_naive_instant_cannot_decide_a_window() -> None:
    """The one new clock consumer, refusing what `core/ledger.py` refuses.

    `timestamp()` reads a naive instant as *local* time, so two workers in
    different zones would divide by the window length and land in different
    buckets -- and would stop sharing the allowance silently, under load, in
    production only. `core/clock.py` supplies `is_aware` and `core/ledger.py`
    has `_require_aware` for exactly this; a limiter that used neither would be
    the one place the rule was not applied.
    """
    with pytest.raises(RateLimitError, match="naive"):
        window_key(collector=A_COLLECTOR, limit=A_LIMIT, now=A_NAIVE_INSTANT)


def test_the_expired_window_is_logged_under_the_name_the_module_declares(
    monkeypatch: pytest.MonkeyPatch,
    captured_events: list[EventDict],
) -> None:
    """The event constant says it exists so a case and the code cannot drift; this is the case.

    `core/ledger.py`'s `FINALIZATION_FAILED_EVENT` makes the same claim and
    `tests/integration/django_apps/test_run_ledger.py` backs it. An unasserted
    event name is a name that can be changed, or silently not emitted, with the
    suite green -- and this one is the only record that a collector was counted
    into a window it did not start.
    """

    def _expired(key: str, delta: int = 1, version: int | None = None) -> int:
        message = f"Key '{key}' not found"
        raise ValueError(message)

    monkeypatch.setattr(cache, "incr", _expired)

    CacheRateLimiter().acquire(collector=A_COLLECTOR, limit=A_LIMIT, now=FIXED_INSTANT)

    assert [event["event"] for event in captured_events] == [WINDOW_EXPIRED_EVENT]
    assert captured_events[0]["collector"] == A_COLLECTOR
    assert captured_events[0]["cost"] == 1


def test_every_event_name_this_module_declares_is_dotted() -> None:
    """The repository's own shape, applied to a name added by this story.

    `drain.begin`, `health.readiness_refused_draining`,
    `local_dev.seeding_complete`: a dotted prefix is what lets an operator select
    a subsystem's lines without matching prose. A flat sentence reads fine in
    isolation and is invisible to that query.
    """
    assert "." in WINDOW_EXPIRED_EVENT
    assert WINDOW_EXPIRED_EVENT.split(".")[0] == "rate_limit"
