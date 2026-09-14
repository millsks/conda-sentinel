"""The fixtures the collector-base cases are measured against, in one place.

`CPM-EVIDENCE-S05`'s base owns the transport seam, the rate limit and the
observation window, and it is forbidden to own a concrete evidence model:
`CPM-AD-7` gives each collector its own table and puts the first in
`CPM-EP-CURRENCY`, so a table invented in `core` would be one no collector wants
and one the append-only audits would police forever. What the suite therefore
needs is a *fixture* collector writing a *fixture* evidence table, in the same
shape `tests/integration/django_apps/test_append_only_evidence.py` already uses
for the append-only base.

Two suites need them. `tests/unit/django_apps/test_collection.py` builds
collectors to assert the construction-time refusals and needs no table at all;
`tests/integration/django_apps/test_collection.py` runs `collect()` against a
real one. A second copy of either would be the failure `tests/source_scan.py`
and `tests/model_registry.py` were both extracted to prevent -- two fixtures that
can disagree look exactly like two passing tests.

**The evidence model is built inside `isolate_apps` and built once.**
`isolate_apps` patches `Options.default_apps` rather than the global registry, so
the fixture model is invisible to `tests/model_registry.py`'s sweeps and to
`tests/unit/django_apps/test_migration_completeness.py` -- which is the whole
reason it is built there rather than declared at module scope, where it would be
a model in `core` that no migration builds. The class survives its block (it
holds a reference to the registry it was defined in) and is cached, so the unit
tier's type and the integration tier's real table are the same class.

**The fake transport records what it was asked for.** That is `CPM-AD-27`
expressed as a test helper: the base hands it a locator and it hands back a
`Payload` a case wrote by hand, so every parse, `not_found` and `error` path is
reachable without a socket. `calls` is what proves the *negative* assertions --
the window case is only meaningful if the transport can say it was never asked.

**The response cache is a fake that remembers, not a mock that asserts.**
`RecordingResponseCache` holds one entry per `(collector, source)` in a dict and
records every read, write and forget in order. Both halves are load bearing: the
entry is what makes a `304` replay reachable without arranging a real cache key,
and the *order* is what proves `CPM-EVIDENCE-S08`'s central ordering claim --
that nothing is remembered until the evidence for it is written. A mock asserting
"write was called" could not tell the correct order from the dangerous one.
`tests/unit/django_apps/test_response_cache.py` is where the real cache-backed
implementation is proved against a real cache.

**The limiter is one method and is not a mock.** A rate-limit refusal is a
boolean, and arranging one through the cache would mean writing a counter into a
window whose boundary the case then has to reason about. Substituting the
protocol is the seam the base already offers, and
`tests/unit/django_apps/test_rate_limit.py` is where the real limiter is proved
against a real cache. `FixedLimiter` *records* every ask as well as answering it,
for the reason `RecordedTransport` records its calls: an argument nothing reads
is an argument nothing would notice going wrong, and the cost the base charges is
where retry and the allowance are reconciled (`CPM-AD-20`).

**Six deliberately broken collectors, each broken in exactly one way.** A parser
that raises, a parser that finds nothing, a `sentinel_evidence` that ignores the
state it was asked for, a `translate` that ignores the instant it was handed, an
evidence row the schema will not accept, and a *sentinel* row it will not accept.
Each is a subclass of the ordinary fixture, so it differs from the working
collector by one method and by nothing else -- which is what makes the assertion
about that method rather than about the fixture. The third and fourth exist
because both mistakes type-check perfectly: the base checks the row rather than
trusting it, and these are what measure that check. The last two are the only
shapes that reach the database's own refusals, which is what the per-package
transaction and the failing-write wrapper are each about.

**One collector whose question does not apply, and one that cannot say so on a
row.** `CPM-CURRENCY-S02` gave the base an `inapplicability` hook, and the
ordinary fixture answers it as the base's default does -- "applies" -- so the
window, the allowance and the transport cases stay about what they were about.
`inapplicable_collector_class` overrides that one method with a fixed reason, so
the base's `not_applicable` path -- a row, no call, no allowance, no cache read,
a `succeeded` run -- is measurable against the fixture table. Its subclass
refuses the `not_applicable` sentinel, which is the one collector mistake that
path can meet: a collector saying "this does not apply" and then having no row
shape for it.

A helper module, not a collected one. `[tool.pytest.ini_options] python_files`
matches `test_*.py` and `tests.py`, so nothing here is collected, and it sits at
`tests/` rather than under `tests/unit/` for the reason `tests/source_scan.py`,
`tests/model_registry.py` and `tests/celery_tasks.py` do: a collected test module
is not a helper library.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextlib import suppress
from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from datetime import timedelta
from functools import cache as memoized
from types import MappingProxyType
from typing import TYPE_CHECKING
from typing import ClassVar
from typing import Final

from django.core.cache import cache
from django.db import models
from django.db import transaction
from django.test.utils import isolate_apps

from conda_sentinel.core.collection import Collector
from conda_sentinel.core.collection import CollectorConfigurationError
from conda_sentinel.core.collection import SweepOutcome
from conda_sentinel.core.delivery import DeliveryOutcome
from conda_sentinel.core.models import AppendOnlyModel
from conda_sentinel.core.outcomes import OutcomeState
from conda_sentinel.core.rate_limit import CredentialRefusal
from conda_sentinel.core.rate_limit import RateLimit
from conda_sentinel.core.rate_limit import refusal_key
from conda_sentinel.core.rate_limit import window_end
from conda_sentinel.core.registry import CollectorRegistryError
from conda_sentinel.core.registry import register
from conda_sentinel.core.registry import unregister
from conda_sentinel.core.response_cache import CachedResponse
from conda_sentinel.core.transport import DEFAULT_RETRIES
from conda_sentinel.core.transport import Payload
from conda_sentinel.core.transport import TransportError
from tests.model_registry import FIXTURE_APP
from tests.model_registry import FIXTURE_LABEL

if TYPE_CHECKING:
    from collections.abc import Iterable
    from collections.abc import Iterator
    from collections.abc import Mapping
    from collections.abc import Sequence

    from conda_sentinel.core.clock import Clock
    from conda_sentinel.core.rate_limit import RateLimiter
    from conda_sentinel.core.response_cache import ResponseCache
    from conda_sentinel.core.transport import Transport

#: The name every fixture collector declares, and therefore what its ledger rows
#: and its rate-limit cache keys carry. Prefixed so it cannot be confused with a
#: real collector's name the day one exists.
FIXTURE_COLLECTOR: Final[str] = "cpm-fixture-collector"

#: A second name, for the matrix row where a recent run belongs to a *different*
#: collector and must not suppress this one.
OTHER_FIXTURE_COLLECTOR: Final[str] = "cpm-fixture-other-collector"

#: The GitHub credential every `CPM-OPERATE-S05` case declares, in one spelling
#: for both tiers. It carries the classic-token prefix, because `token_fault`
#: refuses a value without one, and is otherwise a phrase -- hyphens, words --
#: that no real token could be, so nothing in this repository resembles a
#: credential a scanner or a person would mistake for one. Its first eight
#: characters are what the hygiene assertions grep for, because a redaction
#: that kept a "helpful" prefix would pass a whole-value grep.
A_GITHUB_TOKEN: Final[str] = "ghp_token-for-tests"  # noqa: S105 - a phrase standing in for a credential, not one
A_GITHUB_TOKEN_PREFIX: Final[str] = A_GITHUB_TOKEN[:8]

#: Where every `CPM-OPERATE-S09` case delivers the digest, in one spelling for
#: both tiers: a webhook on a reserved test domain, the same URL carrying a
#: credential -- the shape the hygiene assertions prove never reaches a row or
#: a log line -- and one address. `A_WEBHOOK_SECRET` is the substring those
#: assertions grep for.
A_DIGEST_WEBHOOK_URL: Final[str] = "https://hooks.example.test/digest"
A_WEBHOOK_SECRET: Final[str] = "webhook-secret-for-tests"  # noqa: S105 - a phrase standing in for a credential, not one
A_CREDENTIALED_WEBHOOK_URL: Final[str] = f"https://operator:{A_WEBHOOK_SECRET}@hooks.example.test/x"
A_DIGEST_WEBHOOK_HOST: Final[str] = "hooks.example.test"
A_DIGEST_EMAIL: Final[str] = "ops@example.test"

#: The table the fixture evidence model is given, rather than the
#: `core_collectedfact` Django would derive. Load-bearing for the same reason
#: `test_append_only_evidence.py`'s is: the integration fixture drops a stale
#: table of this name at session start -- `--reuse-db` means a killed run leaves
#: one behind -- and a derived name would one day land on a genuine migrated
#: table.
FIXTURE_TABLE: Final[str] = "cpm_fixture_collected_fact"

#: The observation window the fixture collectors declare. An hour, which is long
#: enough that a case can place a prior run comfortably inside it or outside it
#: by naming a fraction or a multiple of this value, rather than by writing an
#: interval at the call site that nothing reconciles against the declaration.
FIXTURE_WINDOW: Final[timedelta] = timedelta(hours=1)

#: The freshness target the fixture collectors declare (`CPM-AD-28`). A day,
#: which is comfortably longer than `FIXTURE_WINDOW` for the reason a real one
#: would be: a target shorter than the window would declare evidence stale before
#: the window let anything re-observe it. Named rather than spelled at the call
#: site so a staleness case can place an observation inside it or past it by a
#: fraction or a multiple of the declaration, rather than by an interval nothing
#: reconciles against it.
FIXTURE_FRESHNESS_TARGET: Final[timedelta] = timedelta(days=1)

#: The timeout the fixture collectors declare. A real number rather than a
#: sentinel: the base builds a `RequestsTransport` from it whenever a case does
#: not substitute one, and that construction must succeed.
FIXTURE_TIMEOUT: Final[float] = 5.0

#: The allowance the fixture collectors declare. Generous, because the cases that
#: are about the limit substitute a refusing limiter rather than exhausting this.
FIXTURE_RATE_LIMIT: Final[RateLimit] = RateLimit(calls=60, per=timedelta(minutes=1))

#: The locator the fixture collectors read, with the package key appended. Not a
#: reachable address: every case substitutes the transport, and the one
#: integration case that makes a real call builds its own URL from the local
#: server's port.
FIXTURE_SOURCE_PREFIX: Final[str] = "https://fixture.invalid/packages/"

#: A whole locator, for a payload a case builds by hand. `Payload.source` is the
#: field that exists so an evidence row can say where an observation came from,
#: so the default has to be something a row could meaningfully carry.
A_SOURCE: Final[str] = f"{FIXTURE_SOURCE_PREFIX}recorded"

#: The locator the sweeping fixture collector reads. A document rather than a
#: package, because that is the whole difference between the two paths: `sweep()`
#: asks for one locator per *run* and `collect()` asks for one per package.
FIXTURE_SWEEP_SOURCE: Final[str] = "https://fixture.invalid/inventory"

#: The package the sweeping fixture collector writes its one row about. It has no
#: `identity.Package` behind it, and needs none: the fixture evidence model
#: carries a plain integer `package_id` rather than a relation.
A_SWEPT_PACKAGE: Final[int] = 4242

#: What the miscounting sweep fixture claims to have written. Large enough that
#: no plausible fixture writes it by accident, so a case asserting the refusal is
#: asserting about the claim rather than about an off-by-one.
A_FABRICATED_ROW_COUNT: Final[int] = 99

#: The table the *second* fixture evidence model is given. It exists only so a
#: collector can be caught writing a row of a model it did not declare, and no
#: case ever inserts one -- the base refuses before the insert -- so this name
#: never reaches a schema editor. It is declared anyway, for the reason
#: `FIXTURE_TABLE` is: a derived name would one day land on a real table.
FIXTURE_OTHER_TABLE: Final[str] = "cpm_fixture_other_fact"

#: What a fixture payload's body says when a case does not care.
A_PAYLOAD_BODY: Final[str] = '{"version": "2.4.0"}'

#: The value a determinate fixture evidence row carries. The generic determinate
#: member's value (`core/outcomes.py`'s `DETERMINATE`), spelled here so a case
#: asserting "no row carries `ok`" has the same string the fixture writes.
DETERMINATE_VALUE: Final[str] = "ok"

#: How many rows `several_sentinels_collector_class` answers with. More than
#: one, which is the whole property, and small enough that a case can assert
#: the set rather than only the count.
SEVERAL_SENTINEL_ROWS: Final[int] = 3

#: The package `foreign_package_sentinel_collector_class` answers about, which
#: is never the one a case collects. Its own constant so the assertion reads as
#: "a package this run is not about" rather than as an arbitrary integer.
A_FOREIGN_PACKAGE: Final[int] = 999_001

#: What one collection costs the rate limiter: the request plus its retry budget
#: (`Collector.request_cost`). Derived rather than written out, so a case that
#: exhausts an allowance stays correct if the default retry count changes.
FIXTURE_REQUEST_COST: Final[int] = 1 + DEFAULT_RETRIES

#: The headers the fixture collectors declare. One, and it is the one every
#: source in `CPM-EP-CURRENCY` expects: a `User-Agent` naming the caller. A
#: non-empty default so the composition with the base's conditional headers is
#: exercised by every case rather than only by the case that is about it.
FIXTURE_USER_AGENT_HEADER: Final[str] = "User-Agent"
FIXTURE_USER_AGENT: Final[str] = "cpm-fixture-collector/1.0"
#:
#: Read-only, and in this module that is not a formality. One mapping is handed
#: to every collector `collector_class` builds, so a plain `dict` would be a
#: shared mutable declaration -- exactly what `_require_headers` returns a
#: `MappingProxyType` to prevent, undone by the fixtures that measure it. A case
#: that mutated it would change the declarations of every collector built after
#: it, in whatever order the suite happened to run that day.
FIXTURE_HEADERS: Final[Mapping[str, str]] = MappingProxyType({FIXTURE_USER_AGENT_HEADER: FIXTURE_USER_AGENT})

#: How long the fixture collectors may replay a remembered response. A day,
#: which is longer than `FIXTURE_WINDOW` for the reason a real one would be: an
#: entry that expired inside the observation window would make every second run
#: unconditional and the cache would save nothing.
FIXTURE_CACHE_TTL: Final[timedelta] = timedelta(days=1)

#: The two validators the fixtures use. Written as a source would send them --
#: an entity tag is quoted, and an HTTP date is RFC 7231's format -- so a case
#: asserting that a validator travels verbatim is asserting it about the shape a
#: real source produces.
AN_ETAG: Final[str] = '"a1b2c3d4"'
A_LAST_MODIFIED: Final[str] = "Wed, 03 Sep 2026 12:00:00 GMT"

#: What a remembered response holds when a case does not care. Distinct from
#: `A_PAYLOAD_BODY`, so a case that replays the cache can tell the replayed body
#: from a freshly fetched one -- which is the assertion, and it would be vacuous
#: if the two strings were equal.
A_CACHED_BODY: Final[str] = '{"version": "2.3.0"}'

#: Why the inapplicable fixture collector says its question does not apply. A
#: sentence rather than a token, because it is what the base writes into the
#: row's `detail` and the ledger row's `detail`, and a case asserting the two
#: agree should be asserting about words a reader would meet.
A_NOT_APPLICABLE_REASON: Final[str] = "the fixture question is not about this package (CPM-FR-6)"

#: The cadence the sweepable fixture collector declares (`CPM-CURRENCY-S05`).
#:
#: Half `FIXTURE_FRESHNESS_TARGET`, because the boot reconciliation refuses a
#: target that is not *strictly greater* than the cadence and a fixture whose two
#: numbers were equal would put every case that registers it one edit away from
#: refusing the boot for a reason it was not written about.
FIXTURE_CADENCE: Final[timedelta] = FIXTURE_FRESHNESS_TARGET / 2

#: A naive instant, for the collector that ignores the one it was handed.
#:
#: `AppendOnlyModel.save()` refuses this and `bulk_create` never calls `save()`,
#: which is the hole `Collector._require_stamped` closes -- so a fixture that
#: could not produce a naive instant would leave that guard unmeasured.
A_NAIVE_INSTANT: Final[datetime] = datetime(2026, 9, 4, 12, 0)  # noqa: DTZ001 - naive on purpose; see above

#: How wide the fixture evidence model's short columns are. Wide enough for the
#: longest `OutcomeState` value, which is `not_applicable`, and for a locator.
_STATE_LENGTH: Final[int] = 32
_SOURCE_LENGTH: Final[int] = 255


@memoized
def fixture_evidence_model() -> type[AppendOnlyModel]:
    """Return the fixture evidence model, building it once for the session.

    An ordinary evidence model: it inherits `AppendOnlyModel`, declares its own
    columns, and carries no unique constraint that could suppress a
    re-observation (`CPM-AD-7`).

    **`isolate_apps` is entered and left before the class is used.** The patched
    registry is not held open -- the class keeps a reference to the registry it
    was defined in, and it declares no relation, so nothing it does needs to look
    another model up. Leaving the block is also what keeps the model out of the
    global registry that `tests/model_registry.py` sweeps.

    Returns:
        A concrete `AppendOnlyModel` subclass whose `db_table` is
        `FIXTURE_TABLE`. Cached, so every caller in the session gets the same
        class -- which is what makes the integration tier's table the unit
        tier's model.

    """
    with isolate_apps(FIXTURE_APP):

        class CollectedFact(AppendOnlyModel):
            #: The package the observation is about, by the integer primary key
            #: `CPM-AD-3` fixes. Not a `ForeignKey`, and that is forced rather
            #: than chosen -- `tests/passes.py` gives the reason at length:
            #: `isolate_apps` patches `Options.default_apps` with a registry
            #: `identity.Package` is not in, so a `ForeignKey(Package)` declared
            #: here resolves against nothing. Every *real* model references the
            #: package by a relation, `core.CollectionRun` included since
            #: `CPM-EVIDENCE-S09`; `core/freshness.py` accepts either spelling
            #: precisely so a fixture built this way is still askable.
            package_id = models.PositiveBigIntegerField()

            #: The `OutcomeState` value this row carries, emitted verbatim
            #: (`CPM-AD-24`). A `CharField`, never a boolean (`CPM-AD-5`).
            state = models.CharField(max_length=_STATE_LENGTH)

            #: What the base or the collector had to say about the observation.
            detail = models.TextField(blank=True, default="")

            #: What the source said, where it said anything.
            body = models.TextField(blank=True, default="")

            #: Where the observation came from. `Payload.source` exists so an
            #: evidence row can say that without the collector reconstructing
            #: it, and a fixture that dropped the column would leave the field
            #: asserted nowhere.
            source = models.CharField(max_length=_SOURCE_LENGTH, blank=True, default="")

            class Meta:
                app_label = FIXTURE_LABEL
                db_table = FIXTURE_TABLE

    return CollectedFact


@memoized
def other_fixture_evidence_model() -> type[AppendOnlyModel]:
    """Return a second fixture evidence model, for the wrong-table case.

    Identical in shape to `fixture_evidence_model` and deliberately not the same
    class: the thing under test is that the base refuses a row whose *model* is
    not the one the collector declared, and a row that differed in its fields as
    well would leave open which of the two differences was noticed.

    No table is ever created for it. The refusal happens before the insert, which
    is the whole point -- a row of the wrong model reaching `bulk_create` is
    already a row in the wrong table's shape.

    Returns:
        A concrete `AppendOnlyModel` subclass distinct from
        `fixture_evidence_model()`. Cached, so identity comparisons hold across a
        session.

    """
    with isolate_apps(FIXTURE_APP):

        class OtherCollectedFact(AppendOnlyModel):
            package_id = models.PositiveBigIntegerField()
            state = models.CharField(max_length=_STATE_LENGTH)
            detail = models.TextField(blank=True, default="")
            body = models.TextField(blank=True, default="")
            source = models.CharField(max_length=_SOURCE_LENGTH, blank=True, default="")

            class Meta:
                app_label = FIXTURE_LABEL
                db_table = FIXTURE_OTHER_TABLE

    return OtherCollectedFact


@dataclass(slots=True)
class RecordedWebhookDeliverer:
    """A webhook deliverer that answers from a script and remembers what it was handed.

    `core/delivery.py`'s seam, substituted on `RecordedTransport`'s terms: the
    digest's whole delivery path is exercisable with no socket, because the
    only thing it needs from the outside world is a `DeliveryOutcome`.

    Attributes:
        outcome: What `post` returns. Delivered by default.
        failure: An exception to raise instead, for the row of the matrix in
            which a substituted deliverer raises rather than answers.
        calls: Every `(url, body, timeout)` `post` was handed, in order. The
            URL is recorded so a case can assert the *deliverer* was given the
            credentialed URL while nothing else was.

    """

    outcome: DeliveryOutcome = field(default_factory=lambda: DeliveryOutcome(delivered=True, status_code=200))
    failure: Exception | None = None
    calls: list[tuple[str, dict[str, object], float]] = field(default_factory=list)

    def post(self, url: str, *, body: Mapping[str, object], timeout: float) -> DeliveryOutcome:
        """Record the call and answer from the script.

        Args:
            url: Where the digest was to be posted.
            body: The document.
            timeout: The timeout the caller stated.

        Returns:
            The scripted outcome.

        Raises:
            Exception: Whatever was scripted as `failure`.

        """
        self.calls.append((url, dict(body), timeout))
        if self.failure is not None:
            raise self.failure
        return self.outcome


@dataclass(slots=True)
class RecordedTransport:
    """A transport that answers from a script and remembers what it was asked.

    The concrete form of `CPM-AD-27`'s promise: the base's whole orchestration is
    exercisable with no socket, because the only thing it needs from the outside
    world is a `Payload`.

    Attributes:
        payload: What `fetch` returns.
        failure: The `TransportError` to raise instead, for the
            source-unavailable row of the matrix.
        calls: Every locator `fetch` was handed, in order. The window cases
            assert this stays empty, which is the only way to show the transport
            was *not* called.
        sent_headers: The header mapping each call carried, in the same order.
            Recorded separately from `calls` rather than as a pair, so the
            dozens of existing assertions about *which locator* was asked stay
            about that -- and so a case about the composed header set reads as
            one list rather than as a tuple index.

    """

    payload: Payload | None = None
    failure: TransportError | None = None
    calls: list[str] = field(default_factory=list)
    sent_headers: list[Mapping[str, str] | None] = field(default_factory=list)

    def fetch(self, source: str, *, headers: Mapping[str, str] | None = None) -> Payload:
        """Record the request and answer from the script.

        Args:
            source: The locator the base asked for.
            headers: The headers the base composed for it. Recorded rather than
                discarded: the conditional request *is* the caching mechanism,
                and an argument nothing reads is an argument nothing would
                notice going missing.

        Returns:
            The scripted payload.

        Raises:
            TransportError: When one was scripted, which is the
                source-unavailable row of the matrix.
            RuntimeError: When neither a payload nor a failure was scripted. A
                raise rather than an `assert`, because `assert` vanishes under
                `python -O` and would then return `None` where a `Payload` is
                annotated -- and a helper that invented an empty payload would
                let a case pass by observing nothing, which is the failure the
                base exists to prevent.

        """
        self.calls.append(source)
        self.sent_headers.append(headers)
        if self.failure is not None:
            raise self.failure
        if self.payload is None:
            message = f"RecordedTransport was asked to fetch {source!r} with neither a payload nor a failure scripted"
            raise RuntimeError(message)
        return self.payload


@dataclass(slots=True)
class ScriptedTransport:
    """A transport that answers each locator from its own script.

    `RecordedTransport` above answers every call with one payload, which is all
    the base's own cases ever need: they make one call. A collector that makes a
    *second* call needs more -- a double that answered both from one script could
    not tell a case that read the fallback from one that read the first document
    twice, and the whole point of a bounded second call is which locator it
    reaches.

    Written for `CPM-CURRENCY-S01`'s tag fallback and moved here when
    `CPM-CURRENCY-S03` needed the same thing on both of its branches: two
    byte-identical copies in two integration modules is the failure this file
    exists to prevent, and a fourth collector is coming.

    Attributes:
        answers: What to return for each locator.
        failures: What to raise for each locator instead.
        calls: Every locator `fetch` was handed, in order.
        sent_headers: The header mapping each call carried, in the same order.

    """

    answers: dict[str, Payload] = field(default_factory=dict)
    failures: dict[str, TransportError] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)
    sent_headers: list[Mapping[str, str] | None] = field(default_factory=list)

    def fetch(self, source: str, *, headers: Mapping[str, str] | None = None) -> Payload:
        """Record the request and answer from the script for this locator.

        Args:
            source: The locator the collector asked for.
            headers: The headers the base composed for it.

        Returns:
            The scripted payload.

        Raises:
            TransportError: When one was scripted for this locator.
            RuntimeError: When nothing was scripted for it. A raise rather than an
                `assert`, because `assert` vanishes under `python -O` and would
                then return `None` where a `Payload` is annotated -- and a helper
                that invented an empty payload would let a case pass by observing
                nothing.

        """
        self.calls.append(source)
        self.sent_headers.append(headers)
        if source in self.failures:
            raise self.failures[source]
        if source not in self.answers:
            message = f"ScriptedTransport was asked to fetch {source!r}, which nothing scripted"
            raise RuntimeError(message)
        return self.answers[source]


def credential_sightings(
    *,
    rows: Iterable[models.Model],
    events: Iterable[Mapping[str, object]],
    details: Iterable[str],
    token: str = A_GITHUB_TOKEN,
) -> list[str]:
    """Return every place the credential, or its first eight characters, was found.

    The `CPM-OPERATE-S05` hygiene grep, in one place for both GitHub-reading
    collectors' integration cases. Every concrete field of every row handed in
    is rendered -- the evidence row's `detail` is the obvious column, and the
    obvious column is not the only one a careless `source` or `body` could
    carry a header into -- and every captured log event is rendered whole, so a
    credential bound as a *field* rather than interpolated into the message is
    found too.

    Args:
        rows: Saved model instances: the evidence rows and the ledger rows.
        events: What `captured_collection_logs` captured.
        details: Any other strings the run produced -- `CollectionResult.detail`.
        token: The credential to look for.

    Returns:
        One entry per sighting, naming where it was seen. Empty is the
        assertion.

    """
    prefix = token[:8]
    rendered: list[tuple[str, str]] = []
    for row in rows:
        fields = row._meta.concrete_fields  # noqa: SLF001 - `_meta` is Django's own public-by-convention API
        rendered.append(
            (
                f"{type(row).__name__} pk={row.pk}",
                " ".join(f"{field.name}={getattr(row, field.attname)!r}" for field in fields),
            )
        )
    rendered.extend((f"event {event.get('event')!r}", repr(dict(event))) for event in events)
    rendered.extend((f"detail #{index}", detail) for index, detail in enumerate(details))
    return [
        f"{where}: {'the token' if token in text else 'its prefix'}"
        for where, text in rendered
        if token in text or prefix in text
    ]


def recorded_payload(  # noqa: PLR0913 - one parameter per `Payload` field; a bundle would hide the field under test
    *,
    source: str = A_SOURCE,
    found: bool = True,
    body: str = A_PAYLOAD_BODY,
    not_modified: bool = False,
    etag: str | None = None,
    last_modified: str | None = None,
) -> Payload:
    """Build a payload a case can hand to the fake transport.

    Args:
        source: The locator the payload claims to have come from. Defaults to a
            whole locator rather than the bare prefix, so a case asserting that
            `Payload.source` reaches an evidence row is asserting something a
            real payload would carry.
        found: Whether the source says the resource exists.
        body: What the source said.
        not_modified: Whether the source answered that the validator this
            process sent still holds. Defaults to `False`, so every case written
            before caching existed still describes an ordinary answer.
        etag: The entity tag the source declared, or `None`.
        last_modified: The modification date the source declared, or `None`.

    Returns:
        A `Payload` carrying no status code, because a transport substituted at
        this seam is not obliged to speak HTTP -- which is `CPM-AD-29`'s file
        adapter in miniature.

    """
    return Payload(
        source=source,
        found=found,
        body=body,
        not_modified=not_modified,
        etag=etag,
        last_modified=last_modified,
    )


def cached_response(
    *,
    body: str = A_CACHED_BODY,
    etag: str | None = AN_ETAG,
    last_modified: str | None = None,
) -> CachedResponse:
    """Build the entry a case seeds the response cache with.

    Args:
        body: What the source last served, defaulting to a body distinguishable
            from the one a fresh fetch produces.
        etag: The entity tag remembered beside it.
        last_modified: The modification date remembered beside it.

    Returns:
        A `CachedResponse` carrying at least one validator, which is the only
        kind that can exist.

    """
    return CachedResponse(body=body, etag=etag, last_modified=last_modified)


@dataclass(slots=True)
class RecordingResponseCache:
    """A response cache that remembers in a dictionary and records what it was asked.

    The seam `CPM-EVIDENCE-S08` opens, expressed as a test helper: the base's
    caching behaviour is exercisable without computing a cache key, and -- more
    importantly -- the *order* of its calls is observable. The story's central
    ordering claim is that nothing is written until the evidence for it is, and
    the only way to see that from outside is to have the cache say when it was
    written relative to everything else.

    Attributes:
        entries: What is remembered, keyed by `(collector, source)`. Seeded by a
            case to arrange a hit; read back to assert what a run left behind.
        reads: Every `(collector, source)` `read` was asked about, in order.
            The `NO_CACHE` case asserts this stays empty, which is the only way
            to show the cache was not consulted.
        writes: Every `(collector, source, response, ttl_seconds)` written.
        forgets: Every `(collector, source)` dropped.

    """

    entries: dict[tuple[str, str], CachedResponse] = field(default_factory=dict)
    reads: list[tuple[str, str]] = field(default_factory=list)
    writes: list[tuple[str, str, CachedResponse, int]] = field(default_factory=list)
    forgets: list[tuple[str, str]] = field(default_factory=list)

    def read(self, *, collector: str, source: str) -> CachedResponse | None:
        """Record the question and answer from what is remembered.

        Args:
            collector: The collector's declared name.
            source: The locator about to be read.

        Returns:
            The seeded entry, or `None`.

        """
        self.reads.append((collector, source))
        return self.entries.get((collector, source))

    def write(self, *, collector: str, source: str, response: CachedResponse, ttl_seconds: int) -> None:
        """Remember an answer and record that it was remembered.

        Args:
            collector: The collector's declared name.
            source: The locator that was read.
            response: The body and its validators.
            ttl_seconds: How long the entry may live.

        """
        self.writes.append((collector, source, response, ttl_seconds))
        self.entries[(collector, source)] = response

    def forget(self, *, collector: str, source: str) -> None:
        """Drop an entry and record that it was dropped.

        Args:
            collector: The collector's declared name.
            source: The locator to forget.

        """
        self.forgets.append((collector, source))
        self.entries.pop((collector, source), None)


@dataclass(slots=True)
class FixedLimiter:
    """A rate limiter whose answer is decided by the case, and which records the ask.

    Recording matters as much as answering. Discarding the arguments would leave
    nothing to notice if `collect()` passed a wall-clock instant, another
    collector's name, or a cost that did not include the retry budget -- and the
    reconciliation of retry against the allowance (`CPM-AD-20`) is exactly what
    lives in that argument.

    The credential-refusal memo (`CPM-OPERATE-S05`) is kept in memory under the
    same key the cache-backed limiter would use, so a case about "the second
    collection in the window makes no fetch" reads the real key arithmetic
    while arranging no cache state.

    Attributes:
        permitted: What `acquire` answers. `False` is the rate-limit-reached row
            of the matrix.
        asks: Every `(collector, limit, now, cost)` the base asked about, in
            order, the way `RecordedTransport` records its calls.
        refusals: Every remembered refusal, by the key the cache would hold it
            under; a case reads it to see what the base remembered, or seeds it
            to place a refusal earlier in the window.

    """

    permitted: bool
    asks: list[tuple[str, RateLimit, datetime, int]] = field(default_factory=list)
    refusals: dict[str, CredentialRefusal] = field(default_factory=dict)

    def acquire(self, *, collector: str, limit: RateLimit, now: datetime, cost: int = 1) -> bool:
        """Record what was asked and answer the scripted verdict.

        Args:
            collector: The name a real limiter would key its counter on.
            limit: The allowance a real limiter would count against.
            now: The instant a real limiter would decide the window from.
            cost: How many requests the caller says it is about to issue.

        Returns:
            `permitted`, unchanged.

        """
        self.asks.append((collector, limit, now, cost))
        return self.permitted

    def remember_refusal(self, *, collector: str, limit: RateLimit, now: datetime, detail: str) -> datetime:
        """Keep the refusal under the window's key, as the cache-backed limiter would.

        Args:
            collector: The collector's declared name.
            limit: Its declared allowance.
            now: The instant the refusal was met.
            detail: What the run recorded.

        Returns:
            When the window turns.

        """
        until = window_end(limit=limit, now=now)
        self.refusals[refusal_key(collector=collector, limit=limit, now=now)] = CredentialRefusal(
            detail=detail,
            until=until,
        )
        return until

    def refusal(self, *, collector: str, limit: RateLimit, now: datetime) -> CredentialRefusal | None:
        """Answer the refusal kept for this window, if any.

        Args:
            collector: The collector's declared name.
            limit: Its declared allowance.
            now: The instant the window is decided from.

        Returns:
            The remembered refusal, or `None`.

        """
        return self.refusals.get(refusal_key(collector=collector, limit=limit, now=now))


def collector_class(  # noqa: PLR0913 - one parameter per declaration; see below
    *,
    declared_model: type[AppendOnlyModel] | None,
    declared_name: str = FIXTURE_COLLECTOR,
    declared_window: timedelta | None = FIXTURE_WINDOW,
    declared_freshness_target: timedelta | None = FIXTURE_FRESHNESS_TARGET,
    declared_timeout: float | None = FIXTURE_TIMEOUT,
    declared_retries: int = DEFAULT_RETRIES,
    declared_rate_limit: RateLimit | None = FIXTURE_RATE_LIMIT,
    declared_headers: Mapping[str, str] = FIXTURE_HEADERS,
    declared_cache_ttl: timedelta | None = FIXTURE_CACHE_TTL,
    declared_credential_host: str | None = None,
) -> type[Collector]:
    """Build a collector subclass with exactly the declarations a case wants.

    A factory rather than a family of classes because the refusal cases differ
    from the working one by a single declaration, and the assertion each makes is
    only meaningful if everything *else* is the same. Written out as six classes
    they would drift, and a refusal case would eventually pass for the wrong
    reason. That is also the argument for the argument count: there is exactly
    one parameter per declaration the base checks, and collapsing any two of them
    into a bundle would put a refusal case one indirection away from the
    declaration it is about.

    The parameter names all carry `declared_` because a class body cannot read an
    enclosing function's local under the same name as an attribute it is
    assigning -- `name = name` in the body below would resolve to the class
    attribute being defined, not to the argument.

    Args:
        declared_model: The evidence model, or `None` for the case that declares
            none.
        declared_name: The collector's name, or `""` for the case that declares
            none.
        declared_window: The observation window, or `None`.
        declared_freshness_target: How long this collector's evidence may be read
            as current, or `None` for the case that declares none -- which is
            `CPM-AD-28`'s named failure and is refused at construction and again
            at boot.
        declared_timeout: The timeout in seconds, or `None`.
        declared_retries: The retry count, which is also what the rate limiter is
            charged per collection.
        declared_rate_limit: The allowance, or `None`.
        declared_headers: What the collector says its source expects. Defaults to
            one `User-Agent`, so every case exercises the composition with the
            base's conditional headers rather than only the case about it.
        declared_credential_host: The one host an `Authorization` header in the
            declaration belongs to (`CPM-OPERATE-S05`), or `None`.
        declared_cache_ttl: How long a remembered response may be replayed, or
            `NO_CACHE` for a collector that caches nothing, or `None` for the
            case that declares none at all.

    Returns:
        A concrete `Collector` subclass. Constructing it is what applies the
        refusals, so a case asserting one instantiates rather than merely
        building the class.

    """

    class _FixtureCollector(Collector):
        """A fixture collector over the fixture evidence table."""

        name: ClassVar[str] = declared_name
        evidence_model: ClassVar[type[AppendOnlyModel] | None] = declared_model
        observation_window: ClassVar[timedelta | None] = declared_window
        freshness_target: ClassVar[timedelta | None] = declared_freshness_target
        timeout: ClassVar[float | None] = declared_timeout
        retries: ClassVar[int] = declared_retries
        rate_limit: ClassVar[RateLimit | None] = declared_rate_limit
        headers: ClassVar[Mapping[str, str]] = declared_headers
        credential_host: ClassVar[str | None] = declared_credential_host
        response_cache_ttl: ClassVar[timedelta | None] = declared_cache_ttl

        def source_for(self, *, package_id: int) -> str:
            """Return the fixture locator for one package.

            Args:
                package_id: The package being collected.

            Returns:
                An unreachable URL naming the package.

            """
            return f"{FIXTURE_SOURCE_PREFIX}{package_id}"

        def translate(self, payload: Payload, *, package_id: int, observed_at: datetime) -> Sequence[AppendOnlyModel]:
            """Turn the recorded body into one determinate evidence row.

            Args:
                payload: What the source said.
                package_id: The package the observation is about.
                observed_at: The instant to stamp the row with.

            Returns:
                One unsaved row carrying `ok` and the body verbatim.

            """
            model = fixture_evidence_model()
            return [
                model(
                    observed_at=observed_at,
                    package_id=package_id,
                    state=DETERMINATE_VALUE,
                    detail="",
                    body=payload.body,
                    source=payload.source,
                ),
            ]

        def sentinel_evidence(
            self,
            *,
            state: OutcomeState,
            package_id: int,
            observed_at: datetime,
            detail: str,
        ) -> AppendOnlyModel:
            """Shape one sentinel row for the fixture table.

            Args:
                state: The sentinel the base decided on.
                package_id: The package the observation is about.
                observed_at: The instant to stamp the row with.
                detail: What happened.

            Returns:
                One unsaved row carrying the sentinel's value verbatim
                (`CPM-AD-24`).

            """
            model = fixture_evidence_model()
            return model(
                observed_at=observed_at,
                package_id=package_id,
                state=state.value,
                detail=detail,
                body="",
                source=self.source_for(package_id=package_id),
            )

    return _FixtureCollector


def breaking_collector_class(*, declared_model: type[AppendOnlyModel]) -> type[Collector]:
    """Build a fixture collector whose parser breaks on the payload it is handed.

    The matrix's translation-raises row. A subclass of the ordinary fixture
    rather than a flag on the factory above, so the two differ by exactly the
    method under test and by nothing else -- and so the factory keeps one
    parameter per *declaration*, which is what makes its own refusal cases read.

    `ValueError` rather than a bespoke type, because the guarantee under test is
    that the base does not depend on which exception a parser produces.

    Args:
        declared_model: The evidence model the sentinel row is written to. The
            base must still write one, which is the whole point of the row.

    Returns:
        A concrete `Collector` subclass whose `translate` always raises.

    """
    ordinary = collector_class(declared_model=declared_model)

    class _BreakingCollector(ordinary):  # type: ignore[valid-type, misc]
        """A collector that cannot parse what it was given."""

        def translate(self, payload: Payload, *, package_id: int, observed_at: datetime) -> Sequence[AppendOnlyModel]:
            """Raise, as a parser meeting a malformed payload does.

            Args:
                payload: What the source said.
                package_id: The package the observation is about.
                observed_at: The instant the row would have carried.

            Raises:
                ValueError: Always.

            """
            message = (
                f"the payload from {payload.source} for package {package_id} at {observed_at.isoformat()} is malformed"
            )
            raise ValueError(message)

    return _BreakingCollector


def working_collector(
    *,
    clock: Clock,
    transport: Transport,
    limiter: RateLimiter | None = None,
    response_cache: ResponseCache | None = None,
) -> Collector:
    """Build a fully declared fixture collector over the fixture evidence model.

    The ordinary case, in one call, so that the cases differ from one another in
    what they arrange rather than in how they build the collector.

    Args:
        clock: The stopped clock the run reads every instant from.
        transport: The transport the case scripted -- a `RecordedTransport` in
            every case but the one that proves the real one.
        limiter: A substituted limiter, or `None` to leave the collector with the
            real cache-backed one.
        response_cache: A substituted response cache, or `None` to leave the
            collector with the real cache-backed one.

    Returns:
        A constructed collector, ready to `collect`.

    """
    built = collector_class(declared_model=fixture_evidence_model())
    return built(clock=clock, transport=transport, limiter=limiter, response_cache=response_cache)


def empty_translation_collector_class(*, declared_model: type[AppendOnlyModel]) -> type[Collector]:
    """Build a collector whose parser finds nothing in a body the source served.

    The silent-mismatch case: a source that changed shape, or a selector that no
    longer matches, produces zero rows and no exception at all. Left unguarded
    that is a `succeeded` run with no evidence, which reads exactly like a
    package nothing has gone wrong with.

    Args:
        declared_model: The evidence model the sentinel row is written to.

    Returns:
        A concrete `Collector` subclass whose `translate` returns nothing.

    """
    ordinary = collector_class(declared_model=declared_model)

    class _EmptyTranslationCollector(ordinary):  # type: ignore[valid-type, misc]
        """A collector whose parser no longer matches its source."""

        def translate(self, payload: Payload, *, package_id: int, observed_at: datetime) -> Sequence[AppendOnlyModel]:
            """Return no rows at all.

            Args:
                payload: What the source said.
                package_id: The package the observation is about.
                observed_at: The instant the rows would have carried.

            Returns:
                An empty sequence.

            """
            return []

    return _EmptyTranslationCollector


def lying_sentinel_collector_class(*, declared_model: type[AppendOnlyModel]) -> type[Collector]:
    """Build a collector whose sentinel row ignores the state it was asked for.

    The subclass mistake nothing else could catch: it type-checks perfectly, it
    writes a row on every failing path so "never no row" still holds, and every
    one of those rows says the package is fine. It is `CPM-NFR-3`'s "never a
    clean result" defeated from inside the contract, which is why the base checks
    the row rather than trusting it.

    Args:
        declared_model: The evidence model the row is built for.

    Returns:
        A concrete `Collector` subclass whose `sentinel_evidence` always writes
        the determinate value.

    """
    ordinary = collector_class(declared_model=declared_model)

    class _LyingSentinelCollector(ordinary):  # type: ignore[valid-type, misc]
        """A collector that reports every failure as a clean observation."""

        def sentinel_evidence(
            self,
            *,
            state: OutcomeState,
            package_id: int,
            observed_at: datetime,
            detail: str,
        ) -> AppendOnlyModel:
            """Ignore `state` and write the determinate value.

            Args:
                state: The sentinel the base asked for, discarded.
                package_id: The package the observation is about.
                observed_at: The instant to stamp the row with.
                detail: What happened.

            Returns:
                One unsaved row carrying `ok`.

            """
            model = fixture_evidence_model()
            return model(
                observed_at=observed_at,
                package_id=package_id,
                state=DETERMINATE_VALUE,
                detail=detail,
                body="",
                source=self.source_for(package_id=package_id),
            )

    return _LyingSentinelCollector


def several_sentinels_collector_class(*, declared_model: type[AppendOnlyModel]) -> type[Collector]:
    """Build a collector that owes several sentinel rows rather than one.

    The shape `CPM-CURRENCY-S04` added `sentinel_evidence_rows` for: a collector
    that observes several surfaces in one collection owes a row per surface on
    the paths the base decides, not the single row `sentinel_evidence` shapes.
    This fixture is the smallest thing with that property, so the base's own
    cases can prove it without reaching for a real collector and its settings.

    Args:
        declared_model: The evidence model the rows are built for.

    Returns:
        A concrete `Collector` subclass whose `sentinel_evidence_rows` returns
        `SEVERAL_SENTINEL_ROWS` rows, each carrying the state it was asked for.

    """
    ordinary = collector_class(declared_model=declared_model)

    class _SeveralSentinelsCollector(ordinary):  # type: ignore[valid-type, misc]
        """A collector that answers for more than one surface."""

        def sentinel_evidence_rows(
            self,
            *,
            state: OutcomeState,
            package_id: int,
            observed_at: datetime,
            detail: str,
        ) -> Sequence[AppendOnlyModel]:
            """Return one row per surface this collector stands for.

            Args:
                state: The sentinel the base decided on.
                package_id: The package the observation is about.
                observed_at: The instant to stamp every row with.
                detail: What happened.

            Returns:
                `SEVERAL_SENTINEL_ROWS` unsaved rows, distinguishable by `body`
                so a case can tell them apart.

                `source` is composed here rather than read back from
                `source_for`, deliberately: this hook is called from paths that
                are already recording a failure, where anything that raises
                replaces the reason being recorded -- and `source_for` is exactly
                the kind of method that raises, in four of the six swept real
                collectors. A fixture the next collector copies should not model
                the call it must not make.

            """
            model = fixture_evidence_model()
            return [
                model(
                    observed_at=observed_at,
                    package_id=package_id,
                    state=state.value,
                    detail=detail,
                    body=f"surface {index}",
                    source=f"{FIXTURE_SOURCE_PREFIX}{package_id}",
                )
                for index in range(SEVERAL_SENTINEL_ROWS)
            ]

    return _SeveralSentinelsCollector


def foreign_package_sentinel_collector_class(*, declared_model: type[AppendOnlyModel]) -> type[Collector]:
    """Build a collector whose sentinel rows are about a package nobody asked about.

    The one thing the base does **not** check about a sentinel row, isolated so
    the gap is a stated fact rather than an assumption. The base checks the
    declared model and the run's instant, and it checks the state verbatim; it
    does not check that the row is about the package the run is about, so a hook
    answering for the wrong one writes evidence attributed to a package this run
    never looked at.

    Args:
        declared_model: The evidence model the rows are built for.

    Returns:
        A concrete `Collector` subclass whose `sentinel_evidence_rows` stamps
        `A_FOREIGN_PACKAGE` on every row.

    """
    ordinary = collector_class(declared_model=declared_model)

    class _ForeignPackageSentinelCollector(ordinary):  # type: ignore[valid-type, misc]
        """A collector that answers about the wrong package."""

        def sentinel_evidence_rows(
            self,
            *,
            state: OutcomeState,
            package_id: int,
            observed_at: datetime,
            detail: str,
        ) -> Sequence[AppendOnlyModel]:
            """Return one row about a package this run is not about.

            Args:
                state: The sentinel the base decided on.
                package_id: The package the observation is about, discarded.
                observed_at: The instant to stamp the row with.
                detail: What happened.

            Returns:
                One unsaved row carrying `A_FOREIGN_PACKAGE`.

            """
            del package_id
            model = fixture_evidence_model()
            return [
                model(
                    observed_at=observed_at,
                    package_id=A_FOREIGN_PACKAGE,
                    state=state.value,
                    detail=detail,
                    body="",
                    source="",
                ),
            ]

    return _ForeignPackageSentinelCollector


def barren_sentinel_collector_class(*, declared_model: type[AppendOnlyModel]) -> type[Collector]:
    """Build a collector whose sentinel hook answers with no rows at all.

    The way a defaulted hook can turn `CPM-NFR-3`'s guarantee off silently: every
    path that reaches it has already promised an observation, so an empty answer
    is a failing run with nothing on the record -- and it happens on a path that
    is already recording a failure, where nobody is looking.

    Args:
        declared_model: The evidence model the rows would have been built for.

    Returns:
        A concrete `Collector` subclass whose `sentinel_evidence_rows` returns an
        empty list.

    """
    ordinary = collector_class(declared_model=declared_model)

    class _BarrenSentinelCollector(ordinary):  # type: ignore[valid-type, misc]
        """A collector that writes nothing where it promised something."""

        def sentinel_evidence_rows(
            self,
            *,
            state: OutcomeState,
            package_id: int,
            observed_at: datetime,
            detail: str,
        ) -> Sequence[AppendOnlyModel]:
            """Return nothing.

            Args:
                state: The sentinel the base decided on, discarded.
                package_id: The package the observation is about, discarded.
                observed_at: The instant, discarded.
                detail: What happened, discarded.

            Returns:
                An empty sequence.

            """
            del state, package_id, observed_at, detail
            return []

    return _BarrenSentinelCollector


def unsequenced_sentinel_collector_class(*, declared_model: type[AppendOnlyModel]) -> type[Collector]:
    """Build a collector whose sentinel hook answers with a row rather than with rows.

    The other plausible way to implement the plural hook wrongly, and the one that
    would otherwise land at `bulk_create` as a message about a field: a single
    model instance is not a sequence at all, and a `str` is one of the wrong
    things.

    Args:
        declared_model: The evidence model the row is built for.

    Returns:
        A concrete `Collector` subclass whose `sentinel_evidence_rows` returns one
        unsaved row, unwrapped.

    """
    ordinary = collector_class(declared_model=declared_model)

    class _UnsequencedSentinelCollector(ordinary):  # type: ignore[valid-type, misc]
        """A collector that forgot the brackets."""

        def sentinel_evidence_rows(
            self,
            *,
            state: OutcomeState,
            package_id: int,
            observed_at: datetime,
            detail: str,
        ) -> Sequence[AppendOnlyModel]:
            """Return the row itself rather than a sequence holding it.

            Args:
                state: The sentinel the base decided on.
                package_id: The package the observation is about.
                observed_at: The instant to stamp the row with.
                detail: What happened.

            Returns:
                Annotated as a sequence and deliberately not one -- which is the
                whole of what the case is about.

            """
            return self.sentinel_evidence(  # type: ignore[return-value]
                state=state,
                package_id=package_id,
                observed_at=observed_at,
                detail=detail,
            )

    return _UnsequencedSentinelCollector


def unstamped_collector_class(*, declared_model: type[AppendOnlyModel]) -> type[Collector]:
    """Build a collector that stamps its rows from somewhere other than the run.

    `bulk_create` does not call `save()`, so `AppendOnlyModel`'s naive-instant
    refusal -- which `core/models.py` calls "the one place every evidence write
    passes through" -- never runs on this path. This is what that hole looks like
    from the outside: a row that is written, that is never corrected because the
    table is append-only, and whose `observed_at` makes every later freshness
    comparison wrong by the writer's offset.

    Args:
        declared_model: The evidence model the row is built for.

    Returns:
        A concrete `Collector` subclass whose `translate` ignores the instant it
        was handed and stamps a naive one.

    """
    ordinary = collector_class(declared_model=declared_model)

    class _UnstampedCollector(ordinary):  # type: ignore[valid-type, misc]
        """A collector that reads its own clock, badly."""

        def translate(self, payload: Payload, *, package_id: int, observed_at: datetime) -> Sequence[AppendOnlyModel]:
            """Stamp the row with a naive instant of its own.

            Args:
                payload: What the source said.
                package_id: The package the observation is about.
                observed_at: The instant it was handed and ignores.

            Returns:
                One unsaved row carrying `A_NAIVE_INSTANT`.

            """
            model = fixture_evidence_model()
            return [
                model(
                    observed_at=A_NAIVE_INSTANT,
                    package_id=package_id,
                    state=DETERMINATE_VALUE,
                    detail="",
                    body=payload.body,
                    source=payload.source,
                ),
            ]

    return _UnstampedCollector


def unwritable_collector_class(*, declared_model: type[AppendOnlyModel]) -> type[Collector]:
    """Build a collector whose evidence rows the database will refuse.

    Used for the per-package transaction case: package *N*'s write raises inside
    the nested `transaction.atomic()`, which rolls back to the savepoint and
    leaves packages 1..*N*-1 exactly where they were. Without the nesting the
    rollback would reach further -- which is the whole of `CPM-AD-23` and is what
    makes `CPM-FR-15`'s partial success reachable.

    The row violates a NOT NULL rather than raising in Python, so the failure
    comes from the database and the transaction is genuinely the thing that
    contains it.

    Args:
        declared_model: The evidence model the row is built for.

    Returns:
        A concrete `Collector` subclass whose translated row cannot be inserted.

    """
    ordinary = collector_class(declared_model=declared_model)

    class _UnwritableCollector(ordinary):  # type: ignore[valid-type, misc]
        """A collector whose rows the schema refuses."""

        def translate(self, payload: Payload, *, package_id: int, observed_at: datetime) -> Sequence[AppendOnlyModel]:
            """Return a row with no `package_id`, which the column forbids.

            Args:
                payload: What the source said.
                package_id: The package the observation is about, dropped.
                observed_at: The instant to stamp the row with.

            Returns:
                One unsaved, uninsertable row.

            """
            model = fixture_evidence_model()
            return [
                model(
                    observed_at=observed_at,
                    package_id=None,
                    state=DETERMINATE_VALUE,
                    detail="",
                    body=payload.body,
                    source=payload.source,
                ),
            ]

    return _UnwritableCollector


def unwritable_sentinel_collector_class(*, declared_model: type[AppendOnlyModel]) -> type[Collector]:
    """Build a collector whose *sentinel* row the database will refuse.

    The ordering hazard the base wraps against: a failing path already has a
    reason -- the source was unreachable -- and then writes the row recording it.
    If that write raises, the database error reaches the run recorder and becomes
    the ledger row's `detail`, replacing the reason with a message about the
    write. A reader is then left with the epitaph and not the death.

    Args:
        declared_model: The evidence model the row is built for.

    Returns:
        A concrete `Collector` subclass whose sentinel row cannot be inserted.

    """
    ordinary = collector_class(declared_model=declared_model)

    class _UnwritableSentinelCollector(ordinary):  # type: ignore[valid-type, misc]
        """A collector that cannot record its own failures."""

        def sentinel_evidence(
            self,
            *,
            state: OutcomeState,
            package_id: int,
            observed_at: datetime,
            detail: str,
        ) -> AppendOnlyModel:
            """Return a correctly stamped, correctly stated, uninsertable row.

            Args:
                state: The sentinel the base asked for, carried faithfully so the
                    row passes every check the base makes before the insert.
                package_id: The package the observation is about, dropped, which
                    is what the column refuses.
                observed_at: The instant to stamp the row with.
                detail: What happened.

            Returns:
                One unsaved row the schema will not accept.

            """
            model = fixture_evidence_model()
            return model(
                observed_at=observed_at,
                package_id=None,
                state=state.value,
                detail=detail,
                body="",
                source=self.source_for(package_id=package_id),
            )

    return _UnwritableSentinelCollector


def inapplicable_collector_class(
    *,
    declared_model: type[AppendOnlyModel],
    reason: str = A_NOT_APPLICABLE_REASON,
) -> type[Collector]:
    """Build a fixture collector whose question does not apply to any package.

    The subject for the base's `not_applicable` path (`CPM-CURRENCY-S02`). A
    subclass of the ordinary fixture overriding `inapplicability` and nothing
    else, so a case about that path is a case about the hook: the same
    `source_for` is still there and must never be called, the same `translate`
    is still there and must never be reached, and the same `sentinel_evidence`
    is what shapes the row the base writes.

    Args:
        declared_model: The evidence model the `not_applicable` row is written to.
        reason: What the collector says, which is what the base writes to the row
            and to the ledger.

    Returns:
        A concrete `Collector` subclass whose `inapplicability` always answers
        with `reason`.

    """
    ordinary = collector_class(declared_model=declared_model)

    class _InapplicableCollector(ordinary):  # type: ignore[valid-type, misc]
        """A collector with nothing to ask about any package."""

        def inapplicability(self, *, package_id: int) -> str:
            """Say the question does not apply, whichever package is asked about.

            Args:
                package_id: The package being collected, which changes nothing.

            Returns:
                The fixed reason.

            """
            return reason

    return _InapplicableCollector


def refusing_inapplicable_collector_class(*, declared_model: type[AppendOnlyModel]) -> type[Collector]:
    """Build an inapplicable collector that has no row shape for `not_applicable`.

    The one subclass mistake the `not_applicable` path can meet: a collector that
    says "this does not apply" and refuses the sentinel the base then asks for.
    `CPM-CURRENCY-S01`'s collector refuses exactly this state, correctly, because
    its question applies to every package; a collector that copied that refusal
    and then grew an `inapplicability` would be this. The base must let the
    refusal out rather than write nothing and call the run a success.

    Args:
        declared_model: The evidence model the row would have been written to.

    Returns:
        A concrete `Collector` subclass whose `sentinel_evidence` refuses
        `OutcomeState.NOT_APPLICABLE` and shapes every other sentinel as the
        ordinary fixture does.

    """
    inapplicable = inapplicable_collector_class(declared_model=declared_model)

    class _RefusingInapplicableCollector(inapplicable):  # type: ignore[valid-type, misc]
        """A collector that declares a question inapplicable and cannot record that."""

        def sentinel_evidence(
            self,
            *,
            state: OutcomeState,
            package_id: int,
            observed_at: datetime,
            detail: str,
        ) -> AppendOnlyModel:
            """Refuse the one state this collector said it would produce.

            Args:
                state: The sentinel the base asked for.
                package_id: The package the observation is about.
                observed_at: The instant to stamp the row with.
                detail: What happened.

            Returns:
                The ordinary fixture's row, for every state but one.

            Raises:
                CollectorConfigurationError: When asked for
                    `OutcomeState.NOT_APPLICABLE`.

            """
            if state is OutcomeState.NOT_APPLICABLE:
                message = f"{type(self).__name__} shapes no row for {state.value!r}"
                raise CollectorConfigurationError(message)
            return super().sentinel_evidence(  # type: ignore[no-any-return]
                state=state,
                package_id=package_id,
                observed_at=observed_at,
                detail=detail,
            )

    return _RefusingInapplicableCollector


def sweeping_collector_class(
    *,
    declared_model: type[AppendOnlyModel],
    declared_headers: Mapping[str, str] = FIXTURE_HEADERS,
    declared_credential_host: str | None = None,
) -> type[Collector]:
    """Build a fixture collector that reads one run-scoped document.

    The base's `sweep()` path needs a subject that is not the inventory
    collector, for one reason and it is a real one: inventory ingestion declares
    `NO_WINDOW`, so the window branch of `sweep()` -- the one that records a
    `skipped` run without touching the transport -- is unreachable through it.
    A fixture declaring `FIXTURE_WINDOW` is what makes that branch a thing a case
    can be about, exactly as `collector_class`'s window is for `collect()`.

    It writes one ordinary determinate row per run, through a transaction of its
    own and then through the base's `_write_evidence`, which is what the
    subclass contract asks for: the atomic unit is inside `persist_sweep` rather
    than around it (`CPM-AD-23`), and every row still passes the base's
    declared-model, stamping and tally checks.

    Args:
        declared_model: The evidence model the row is written to, and the model
            the row is *built* from -- the two must be the same or the base
            refuses, which is what `foreign_model_sweep_collector_class` below
            exists to show.
        declared_headers: What the collector says its source expects, for the
            `CPM-OPERATE-S05` cases that sweep with a credential declared.
        declared_credential_host: The host that credential belongs to, or `None`.

    Returns:
        A concrete `Collector` subclass with a run-scoped source and run-scoped
        persistence, and every other declaration the ordinary fixture's.

    """
    ordinary = collector_class(
        declared_model=declared_model,
        declared_headers=declared_headers,
        declared_credential_host=declared_credential_host,
    )

    class _SweepingCollector(ordinary):  # type: ignore[valid-type, misc]
        """A collector that reads one document naming one package."""

        def sweep_source(self) -> str:
            """Return the run-scoped locator.

            Returns:
                An unreachable URL naming the document rather than a package.

            """
            return FIXTURE_SWEEP_SOURCE

        def persist_sweep(self, payload: Payload, *, observed_at: datetime) -> SweepOutcome:
            """Write one determinate row, in a transaction of its own.

            Args:
                payload: What the source said.
                observed_at: The instant to stamp the row with.

            Returns:
                One observed row and no failures.

            """
            with transaction.atomic():
                written = self._write_evidence(
                    [_swept_row(declared_model, observed_at=observed_at, payload=payload)],
                    observed_at=observed_at,
                )
            return SweepOutcome(observed_rows=written)

    return _SweepingCollector


def _swept_row(
    model: type[AppendOnlyModel],
    *,
    observed_at: datetime,
    payload: Payload,
    package_id: int = A_SWEPT_PACKAGE,
) -> AppendOnlyModel:
    """Build the one row the sweeping fixtures write.

    Written once because five fixtures below differ from the working one by a
    single thing each -- the model, the instant, the count, the door -- and a
    row assembled separately in each would let two of them differ by something
    nobody meant.

    Args:
        model: The model to build the row from.
        observed_at: The instant to stamp it with.
        payload: What the source said, for the body and the locator.
        package_id: The package the row is about.

    Returns:
        One unsaved determinate row.

    """
    return model(
        observed_at=observed_at,
        package_id=package_id,
        state=DETERMINATE_VALUE,
        detail="",
        body=payload.body,
        source=payload.source,
    )


def barren_sweep_collector_class(*, declared_model: type[AppendOnlyModel]) -> type[Collector]:
    """Build a sweeping collector whose document yields nothing and fails nothing.

    The silent-mismatch case on the run-scoped path -- `empty_translation_collector_class`'s
    failure, one level up. A source that changed shape, or a parser that no longer
    matches it, produces no records, no rows and no exception at all; left
    unguarded that is a `succeeded` run over an inventory nobody observed.

    It reports **no failures**, which is the half that matters: a base deciding
    "did this work" on the failure list alone would call this a clean run.

    Args:
        declared_model: The evidence model the collector declares and never
            writes to.

    Returns:
        A concrete `Collector` subclass whose sweep writes nothing.

    """
    sweeping = sweeping_collector_class(declared_model=declared_model)

    class _BarrenSweepCollector(sweeping):  # type: ignore[valid-type, misc]
        """A sweeping collector whose parser no longer matches its source."""

        def persist_sweep(self, payload: Payload, *, observed_at: datetime) -> SweepOutcome:
            """Write nothing and report nothing wrong.

            Args:
                payload: What the source said.
                observed_at: The instant the rows would have carried.

            Returns:
                No rows of either kind and no failures.

            """
            return SweepOutcome(observed_rows=0)

    return _BarrenSweepCollector


def miscounting_sweep_collector_class(*, declared_model: type[AppendOnlyModel]) -> type[Collector]:
    """Build a sweeping collector that reports rows it did not write.

    The mistake `Collector._require_counted` exists for, and the one nothing else
    could catch: `persist_sweep` hands the base an integer, and a base that
    trusted it would finalize a successful run over a table with nothing in it.
    It type-checks perfectly and every row it *does* write is correct.

    Args:
        declared_model: The evidence model the row is written to.

    Returns:
        A concrete `Collector` subclass whose reported count exceeds its writes.

    """
    sweeping = sweeping_collector_class(declared_model=declared_model)

    class _MiscountingCollector(sweeping):  # type: ignore[valid-type, misc]
        """A collector that overstates what it wrote."""

        def persist_sweep(self, payload: Payload, *, observed_at: datetime) -> SweepOutcome:
            """Write one row and claim a great many.

            Args:
                payload: What the source said.
                observed_at: The instant to stamp the row with.

            Returns:
                A count that is not what was written.

            """
            with transaction.atomic():
                self._write_evidence(
                    [_swept_row(declared_model, observed_at=observed_at, payload=payload)],
                    observed_at=observed_at,
                )
            return SweepOutcome(observed_rows=A_FABRICATED_ROW_COUNT)

    return _MiscountingCollector


def bypassing_sweep_collector_class(*, declared_model: type[AppendOnlyModel]) -> type[Collector]:
    """Build a sweeping collector that writes around the base rather than through it.

    The other half of the same guard. This one's count is honest -- it really did
    write one row -- but the row never passed the base's declared-model and
    stamping checks, which is the hole `bulk_create` opens in `save()` reopened
    one level up. The tally is what notices: the base wrote nothing.

    Args:
        declared_model: The evidence model the row is written to.

    Returns:
        A concrete `Collector` subclass that inserts its own rows.

    """
    sweeping = sweeping_collector_class(declared_model=declared_model)

    class _BypassingCollector(sweeping):  # type: ignore[valid-type, misc]
        """A collector that reaches the table on its own."""

        def persist_sweep(self, payload: Payload, *, observed_at: datetime) -> SweepOutcome:
            """Insert directly, reporting the count truthfully.

            Args:
                payload: What the source said.
                observed_at: The instant to stamp the row with.

            Returns:
                The number of rows written, which the base never saw written.

            """
            with transaction.atomic():
                created = declared_model.objects.bulk_create(
                    [_swept_row(declared_model, observed_at=observed_at, payload=payload)],
                )
            return SweepOutcome(observed_rows=len(created))

    return _BypassingCollector


def unstamped_sweep_collector_class(*, declared_model: type[AppendOnlyModel]) -> type[Collector]:
    """Build a sweeping collector that stamps its rows from somewhere other than the run.

    `unstamped_collector_class`'s failure arriving on the run-scoped path. It is
    worth having twice because the two paths reach `_require_stamped` by
    different routes: the per-package one hands the base its rows, and this one
    writes them itself and must still go through the same door.

    Args:
        declared_model: The evidence model the row is written to.

    Returns:
        A concrete `Collector` subclass whose swept row carries a naive instant.

    """
    sweeping = sweeping_collector_class(declared_model=declared_model)

    class _UnstampedSweepCollector(sweeping):  # type: ignore[valid-type, misc]
        """A sweeping collector that reads its own clock, badly."""

        def persist_sweep(self, payload: Payload, *, observed_at: datetime) -> SweepOutcome:
            """Stamp the row with a naive instant of its own.

            Args:
                payload: What the source said.
                observed_at: The instant it was handed and ignores.

            Returns:
                Never; the base refuses the row first.

            """
            with transaction.atomic():
                written = self._write_evidence(
                    [_swept_row(declared_model, observed_at=A_NAIVE_INSTANT, payload=payload)],
                    observed_at=observed_at,
                )
            return SweepOutcome(observed_rows=written)

    return _UnstampedSweepCollector


def foreign_model_sweep_collector_class(
    *,
    declared_model: type[AppendOnlyModel],
    written_model: type[AppendOnlyModel],
) -> type[Collector]:
    """Build a sweeping collector that writes a row of a table it did not declare.

    `CPM-AD-7` gives each collector its own evidence table, and `bulk_create` is
    called on the *declared* model's manager -- so a row of some other model
    reaches it as a bag of attributes and lands in the wrong table's shape. The
    per-package path never made this reachable, because `translate` is written
    beside the model it returns; a sweep assembles rows across a whole document
    and is where it becomes easy.

    Args:
        declared_model: The evidence model the collector declares.
        written_model: The model it actually builds its row from.

    Returns:
        A concrete `Collector` subclass whose swept row is of the wrong model.

    """
    sweeping = sweeping_collector_class(declared_model=declared_model)

    class _ForeignModelSweepCollector(sweeping):  # type: ignore[valid-type, misc]
        """A sweeping collector writing into somebody else's table."""

        def persist_sweep(self, payload: Payload, *, observed_at: datetime) -> SweepOutcome:
            """Build the row from the wrong model.

            Args:
                payload: What the source said.
                observed_at: The instant to stamp the row with.

            Returns:
                Never; the base refuses the row first.

            """
            with transaction.atomic():
                written = self._write_evidence(
                    [_swept_row(written_model, observed_at=observed_at, payload=payload)],
                    observed_at=observed_at,
                )
            return SweepOutcome(observed_rows=written)

    return _ForeignModelSweepCollector


def selectable_collector_class(
    *,
    declared_model: type[AppendOnlyModel],
    declared_name: str = FIXTURE_COLLECTOR,
    declared_cadence: timedelta | None = FIXTURE_CADENCE,
    declared_freshness_target: timedelta | None = FIXTURE_FRESHNESS_TARGET,
    selection: Iterable[int] | None = (),
) -> type[Collector]:
    """Build a fixture collector that a full-inventory dispatch can be pointed at.

    The subject for `collectors/sweep.py` (`CPM-CURRENCY-S05`). A subclass of the
    ordinary fixture overriding the two declarations a dispatch and the boot
    reconciliation read -- `selectable_packages` and `cadence` -- and nothing
    else, so a case about the dispatch is a case about those two: the same
    `source_for`, `translate` and `sentinel_evidence` are still there, and a
    dispatch must reach none of them.

    `selection` defaults to an empty tuple rather than to `None`, because `None`
    is the *other* answer -- "this collector is not swept per package" -- and a
    default that meant that would make the ordinary use of this factory the one
    the dispatch refuses. A case wanting that answer passes `None` explicitly.

    **What the hook answers with is a fresh iterator over whatever the factory
    was handed, not the sequence itself.** `collectors/sweep.py` refuses a
    materialised selection by construction -- a `list` or a `tuple` has already
    done the read `CPM-NFR-1` exists to avoid -- so a fixture handing one back
    would be refused for a reason no case is about. `iter()` per call is what
    keeps the answer lazy *and* re-callable, which matters because the boot
    reconciliation asks the hook once to see whether it answers `None` and the
    dispatch asks it again to read it.

    Args:
        declared_model: The evidence model, which a dispatch never writes to.
        declared_name: The collector's name, which is what the dispatch derives
            `cpm.collect.<name>` from and what the registry keys it under.
        declared_cadence: The declared cadence, or `None` for a collector nothing
            schedules.
        declared_freshness_target: How long this collector's evidence may be read
            as current, so a case can drive the boot refusal that compares it
            against the cadence.
        selection: What `selectable_packages` answers over -- a queryset, an
            iterable of keys, or `None` for a collector that is not swept per
            package. Anything but `None` and a queryset is handed back as a fresh
            iterator over it.

    Returns:
        A concrete `Collector` subclass.

    """
    ordinary = collector_class(
        declared_model=declared_model,
        declared_name=declared_name,
        declared_freshness_target=declared_freshness_target,
    )

    class _SelectableCollector(ordinary):  # type: ignore[valid-type, misc]
        """A collector a dispatch can select packages for."""

        cadence: ClassVar[timedelta | None] = declared_cadence

        @classmethod
        def selectable_packages(cls) -> Iterable[int] | None:
            """Return the packages this fixture says it can be asked about.

            Returns:
                `None` when the factory was handed it, a queryset unchanged, and
                otherwise a **fresh iterator** over whatever it was handed -- so
                the answer is lazy, which the dispatch requires, and re-callable,
                which the boot reconciliation requires.

            """
            if selection is None or isinstance(selection, models.QuerySet):
                return selection
            return iter(selection)

    return _SelectableCollector


@contextmanager
def registered_collector(collector: type[Collector]) -> Iterator[type[Collector]]:
    """Adopt one collector for the duration of a case, and withdraw it after.

    The registry is process-global and the suite is not: a registration left
    behind by one case is a collector every later case's boot sweep refuses over,
    and the failure lands in whichever case happened to run next. So the
    withdrawal is in a `finally` -- the cases that use this are the ones asserting
    a *refusal*, which means the block is left by an exception every time.

    It lives here rather than being written once per suite for the reason
    `cleared_cache` above does, and it is a context manager rather than a fixture
    for the same one: the two suites that need it sit under different
    `conftest.py` files.

    Args:
        collector: The collector class to register, under the name it declares.

    Yields:
        The same class, so a case can name it in the refusal message it asserts
        without binding it twice.

    """
    register(collector)
    try:
        yield collector
    finally:
        # `suppress`, because this runs while an exception is usually already in
        # flight: every case using this helper asserts a *refusal*, so the block
        # is left by an exception nearly every time. `unregister` raises when the
        # name is already gone -- a case that withdrew it itself, or one that
        # re-registered under another name -- and an exception raised in a
        # `finally` replaces the one being propagated. That would swap the
        # assertion the case was making for a registry error about the cleanup,
        # which is the failure that takes longest to read backwards.
        with suppress(CollectorRegistryError):
            unregister(collector.name)


@contextmanager
def cleared_cache() -> Iterator[None]:
    """Empty the cache around a case, in one place both suites use.

    The cache is process-wide and the ledger is not: a rate-limit counter left
    behind by one case is an allowance already spent in the next, and the failure
    would land in whichever case happened to run second. Cleared on the way out
    as well, so a case leaves the cache as it found it for the rest of the
    session.

    It lives here rather than being written once per suite because this module's
    own docstring argues that a second copy of a fixture is the failure
    `tests/source_scan.py` and `tests/model_registry.py` were extracted to
    prevent -- and every module that needs this guard holding its own `autouse`
    copy is exactly that. How many such modules there are is deliberately not
    written down: the number has already grown twice, and a count in prose is a
    fact maintained by whoever has least reason to look for it.
    It is a context manager rather than a fixture because the two suites that
    need it sit under different `conftest.py` files, and a fixture in
    `tests/conftest.py` would either be autouse for the whole suite -- clearing
    the cache under every case in the repository, including the ones whose
    subject is what the cache holds -- or would have to be requested by name in
    each module anyway.

    Yields:
        Nothing; the helper is entirely its two side effects.

    """
    cache.clear()
    try:
        yield
    finally:
        cache.clear()
