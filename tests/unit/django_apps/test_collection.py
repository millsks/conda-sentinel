"""The collector base's declarations and its window rule, proved with no database.

**What is here and what is not, stated rather than left to be inferred.**
`collect()` opens `core/ledger.py`'s recorder, and the recorder's first act is to
insert a `running` row -- that is the whole of `CPM-EVIDENCE-S03`, and it means
running a collection touches a database by construction. Every case that drives
`collect()` therefore lives in
`tests/integration/django_apps/test_collection.py`, where a real table and a real
ledger are available and where the architecture spine puts anything that crosses
a resource boundary.

What does not need one is everything the base decides *before* it commits to a
run, and that is what this module holds:

* **The six declarations, and their refusals.** A collector is refused at
  construction, not at call time -- the matrix row `CPM-EVIDENCE-S05` states for
  the timeout, applied to all six -- and refused for being the *wrong kind of
  thing* as well as for being absent, because a `str` where an interval belongs
  would otherwise surface as a `TypeError` from a comparison in a worker rather
  than as a message naming the declaration. Each case differs from the working
  collector by exactly one declaration, which is what `tests/collectors.py`'s
  factory is for: written out as six classes they would drift, and a refusal case
  would eventually pass for the wrong reason.
* **The window's query, as a `Q` rather than as rows.** Three of its four
  conditions are matrix rows -- a recent run of another collector, of this
  collector against another package, and a recent *failure* or *partial* must
  none of them suppress this observation -- and each is one keyword away from
  being absent. Asserting the shape catches a missing keyword at the point it is
  written; the integration tier then proves the same against real rows, which is
  the half a `Q` cannot show.
* **That the default transport is built from the declared timeout and retry
  count**, which is the one place declared values become call settings, and that
  the base charges the rate limiter the retry budget those values imply.
* **That the fixture evidence model stays out of the app registry**, which is a
  claim about the audits that live in this tier and so belongs in the tier that
  runs on `pixi run test`.

No database, no network: nothing here saves a row, the one queryset expression
built is never evaluated, and the only transport constructed is a
`requests.Session` that has contacted nothing.
"""

from __future__ import annotations

import dataclasses
import inspect
from datetime import timedelta
from math import inf
from typing import TYPE_CHECKING
from typing import Any
from typing import Final

import pytest
from django.apps import apps

from conda_sentinel.collectors.conda_package import CondaPackageCollector
from conda_sentinel.collectors.feedstock import FeedstockCollector
from conda_sentinel.collectors.kev import KevCollector
from conda_sentinel.collectors.pypi_release import PyPIReleaseCollector
from conda_sentinel.collectors.resolve_identity import IdentityResolutionCollector
from conda_sentinel.collectors.source_release import SourceReleaseCollector
from conda_sentinel.collectors.tasks import InventoryIngestionCollector
from conda_sentinel.collectors.vulnerability import VulnerabilityCollector
from conda_sentinel.core.clock import FixedClock
from conda_sentinel.core.collection import CONDITIONAL_HEADERS
from conda_sentinel.core.collection import NO_CACHE
from conda_sentinel.core.collection import NO_WINDOW
from conda_sentinel.core.collection import STATE_FIELD
from conda_sentinel.core.collection import SUPPRESSING_STATES
from conda_sentinel.core.collection import CollectionResult
from conda_sentinel.core.collection import Collector
from conda_sentinel.core.collection import CollectorConfigurationError
from conda_sentinel.core.collection import request_headers
from conda_sentinel.core.collection import window_query
from conda_sentinel.core.models import CollectionRun
from conda_sentinel.core.outcomes import OutcomeState
from conda_sentinel.core.rate_limit import RateLimit
from conda_sentinel.core.rate_limit import RateLimitError
from conda_sentinel.core.runs import RunState
from conda_sentinel.core.transport import DEFAULT_RETRIES
from conda_sentinel.core.transport import RequestsTransport
from tests.clocks import FIXED_INSTANT
from tests.collectors import A_LAST_MODIFIED
from tests.collectors import AN_ETAG
from tests.collectors import DETERMINATE_VALUE
from tests.collectors import FIXTURE_CACHE_TTL
from tests.collectors import FIXTURE_COLLECTOR
from tests.collectors import FIXTURE_FRESHNESS_TARGET
from tests.collectors import FIXTURE_HEADERS
from tests.collectors import FIXTURE_REQUEST_COST
from tests.collectors import FIXTURE_TABLE
from tests.collectors import FIXTURE_TIMEOUT
from tests.collectors import FIXTURE_WINDOW
from tests.collectors import SEVERAL_SENTINEL_ROWS
from tests.collectors import RecordedTransport
from tests.collectors import barren_sentinel_collector_class
from tests.collectors import cached_response
from tests.collectors import collector_class
from tests.collectors import fixture_evidence_model
from tests.collectors import recorded_payload
from tests.collectors import several_sentinels_collector_class
from tests.collectors import unsequenced_sentinel_collector_class

if TYPE_CHECKING:
    from conda_sentinel.core.response_cache import CachedResponse

#: The package every case names. One arbitrary primary key; nothing here depends
#: on its value.
A_PACKAGE: Final[int] = 42

#: A second package, for the assertion that the window query names the package it
#: was asked about.
ANOTHER_PACKAGE: Final[int] = 43

#: The conditions `window_query` must carry, as Django spells them in a `Q`.
#: Written out here so the assertion is against a stated expectation rather than
#: against whatever the function happens to produce.
EXPECTED_WINDOW_CONDITIONS: Final[frozenset[str]] = frozenset(
    {"collector", "package_id", "status__in", "finished_at__gte"},
)

#: A retry count that is neither the default nor zero, so a case asserting the
#: cost is asserting arithmetic rather than a coincidence.
A_RETRY_COUNT: Final[int] = 5

#: A cache lifetime that is positive and truncates to zero whole seconds. The
#: value that looks the most generous and switches caching off, which is why it
#: is refused rather than accepted and rounded.
A_SUB_SECOND_LIFETIME: Final[timedelta] = timedelta(milliseconds=500)

#: What a sentinel row records beside its state, in the cases about the plural
#: hook. One word, because what those cases are about is how many rows there are
#: and what they carry, not what the sentence says.
A_REASON: Final[str] = "a reason"

#: The three arguments every sentinel hook is asked with, bundled so the cases
#: below differ by the collector under test and by nothing else. `state` is
#: deliberately *not* in here: which sentinel is being asked for is the one thing
#: those cases vary.
_SENTINEL_ASK: Final[dict[str, object]] = {
    "package_id": A_PACKAGE,
    "observed_at": FIXED_INSTANT,
    "detail": A_REASON,
}


def _a_determinate_row() -> Any:
    """Return a fixture evidence row carrying the determinate value.

    Returns:
        One unsaved row whose state column says `ok` -- the row a collector
        answering for a second surface would legitimately produce beside a
        `not_found`, and would illegitimately produce beside an `error`.

    """
    model = fixture_evidence_model()
    return model(
        observed_at=FIXED_INSTANT,
        package_id=A_PACKAGE,
        state=DETERMINATE_VALUE,
        detail="",
        body="another surface has it",
        source="",
    )


def _clock() -> FixedClock:
    """Return the stopped clock every case here injects.

    Returns:
        A clock fixed at `FIXED_INSTANT`, so nothing in this module depends on
        when it ran.

    """
    return FixedClock(instant=FIXED_INSTANT)


def test_the_base_cannot_be_instantiated_on_its_own() -> None:
    """Three abstract methods, so `core` never becomes a collector by accident.

    The base owns the transport, the limit and the window; what it deliberately
    does not own is an evidence table (`CPM-AD-7` gives each collector its own,
    and the first arrives with `CPM-EP-CURRENCY`). A base that could be
    constructed would be one that had somehow decided where to write.
    """
    with pytest.raises(TypeError, match="abstract"):
        Collector(clock=_clock())  # type: ignore[abstract]


def test_a_fully_declared_collector_constructs() -> None:
    """The control the refusals below are measured against.

    Without it, a refusal case proves only that *something* about the fixture is
    wrong -- not that it is the one declaration the case removed.
    """
    built = collector_class(declared_model=fixture_evidence_model())

    collector = built(clock=_clock(), transport=RecordedTransport(payload=recorded_payload()))

    assert isinstance(collector, Collector)


@pytest.mark.parametrize("declared", ["", "   ", None, 7], ids=["empty", "blank", "absent", "not-a-string"])
def test_a_collector_without_a_usable_name_is_refused(declared: object) -> None:
    """A run is traceable to the code that performed it (`CPM-FR-39`).

    `core/ledger.py` refuses a blank collector name for the same reason, and
    refusing here as well is not duplication: the ledger's refusal happens once a
    run is already being recorded, and this one happens before the collector
    exists. The non-string case is here because the name also becomes part of a
    cache key, where a wrong type would surface as something else entirely.
    """
    built = collector_class(declared_model=fixture_evidence_model(), declared_name=declared)  # type: ignore[arg-type]

    with pytest.raises(CollectorConfigurationError, match="name="):
        built(clock=_clock())


def test_a_collector_with_no_evidence_model_is_refused() -> None:
    """A collector with nowhere to write cannot record that it failed either.

    Which is the point: `CPM-NFR-3`'s "never no row" is unreachable for a
    collector that has no table, so the absence is refused rather than
    discovered at the first failed call.
    """
    built = collector_class(declared_model=None)

    with pytest.raises(CollectorConfigurationError, match="declares no evidence_model"):
        built(clock=_clock())


def test_an_evidence_model_that_is_not_append_only_is_refused() -> None:
    """A plain `Model` satisfies every annotation here and carries none of the guards.

    `CollectionRun` is the sharpest example available: it is a real model in this
    application, it is mutable *by design* (`CPM-AD-2` exempts the run ledger),
    and it would type-check as somewhere to write evidence. Accepting it would
    mean evidence written through a manager that offers `update()` and
    `delete()` -- every refusal `CPM-AD-2` makes, bypassed by a declaration.
    """
    built = collector_class(declared_model=CollectionRun)  # type: ignore[arg-type]

    with pytest.raises(CollectorConfigurationError, match="does not inherit AppendOnlyModel"):
        built(clock=_clock())


@pytest.mark.parametrize(
    "declared",
    [None, -FIXTURE_WINDOW, 3600, "an hour"],
    ids=["absent", "negative", "a-bare-number", "a-string"],
)
def test_a_collector_without_a_usable_observation_window_is_refused(declared: object) -> None:
    """Omitting the value, mistyping it and inverting it are three different mistakes.

    `timedelta(0)` says "observe on every run", which a reader can see and a
    reviewer can question. `None` says nothing at all, and a base that read it as
    zero would make the two indistinguishable. `3600` is the one worth naming:
    it is a perfectly good number of *unstated units*, it would compare against
    an instant only by raising somewhere else, and it is what somebody writes
    when they are thinking in seconds.
    """
    built = collector_class(declared_model=fixture_evidence_model(), declared_window=declared)  # type: ignore[arg-type]

    with pytest.raises(CollectorConfigurationError, match="observation_window="):
        built(clock=_clock())


def test_a_zero_observation_window_is_accepted() -> None:
    """The declared "observe on every run", which must stay expressible.

    The pair to the refusals above: the guard rejects the absence, the mistyped
    value and the impossible one, and accepts the one that says something.
    """
    built = collector_class(declared_model=fixture_evidence_model(), declared_window=NO_WINDOW)

    assert built(clock=_clock(), transport=RecordedTransport(payload=recorded_payload())) is not None


@pytest.mark.parametrize(
    "declared",
    [None, timedelta(0), -FIXTURE_FRESHNESS_TARGET, 86400, "a day"],
    ids=["absent", "zero", "negative", "a-bare-number", "a-string"],
)
def test_a_collector_without_a_usable_freshness_target_is_refused(declared: object) -> None:
    """`CPM-AD-28`'s rule at the first of its two moments, including the case the window accepts.

    Four of these are the same mistakes the observation window refuses. The one
    that differs is `timedelta(0)`, and it is the reason this case exists
    separately rather than being folded into the window's: the window *accepts*
    zero, because
    "observe on every run" is a thing an operator means. A zero freshness target
    says "evidence is stale the instant it is written", which nobody means and
    which would make every surface permanently amber. Two sentinels that look
    identical and behave oppositely are exactly what a reader generalises
    wrongly from, so the difference is asserted rather than left to the
    docstring.

    `None` is the failure the decision is named for. There is deliberately no
    sentinel meaning "never goes stale" -- an unset target behaving as fresh
    forever is how six-month-old evidence comes to read as current.
    """
    built = collector_class(declared_model=fixture_evidence_model(), declared_freshness_target=declared)  # type: ignore[arg-type]

    with pytest.raises(CollectorConfigurationError, match="freshness_target="):
        built(clock=_clock())


def test_a_positive_freshness_target_is_accepted() -> None:
    """The pair to the refusals above, so the guard is not merely a way to fail.

    The refusals mean nothing unless the value they are protecting is reachable:
    a guard that rejected every target would satisfy every case above and would
    make the declaration impossible to satisfy.
    """
    built = collector_class(
        declared_model=fixture_evidence_model(),
        declared_freshness_target=FIXTURE_FRESHNESS_TARGET,
    )

    assert built(clock=_clock(), transport=RecordedTransport(payload=recorded_payload())) is not None


@pytest.mark.parametrize(
    "declared",
    [None, 0, -1.0, inf, "five", True],
    ids=["absent", "zero", "negative", "infinite", "a-string", "a-flag"],
)
def test_a_collector_without_a_usable_timeout_is_refused_at_construction(declared: object) -> None:
    """The matrix row, in the words it is written in: refused when the collector is constructed.

    Not at call time. A collector discovering this in a worker would already have
    a `running` ledger row and would already have contacted a source; refusing
    here means a misdeclared collector never reaches one.

    `inf` is in the list for the reason `tests/unit/django_apps/test_transport.py`
    gives: it is greater than zero, so a check spelled `timeout > 0` admits
    exactly the value that means an unbounded wait. `True` is in it because
    `bool` is a subclass of `int`, so a flag written where a number belongs would
    otherwise be silently accepted as a one-second timeout.
    """
    built = collector_class(declared_model=fixture_evidence_model(), declared_timeout=declared)  # type: ignore[arg-type]

    with pytest.raises(CollectorConfigurationError, match="timeout="):
        built(clock=_clock())


@pytest.mark.parametrize("declared", [-1, 1.5, "three", True], ids=["negative", "fractional", "a-string", "a-flag"])
def test_a_collector_without_a_usable_retry_count_is_refused(declared: object) -> None:
    """A retry count is a number of attempts, and it is also what the limiter is charged.

    Which is why a wrong one is worth refusing rather than coercing: the value
    reaches `urllib3` *and* the rate limiter, and a fractional or negative count
    would make the declared allowance mean something different from what it says.
    """
    built = collector_class(declared_model=fixture_evidence_model(), declared_retries=declared)  # type: ignore[arg-type]

    with pytest.raises(CollectorConfigurationError, match="retries="):
        built(clock=_clock())


@pytest.mark.parametrize("declared", [None, 60, "60/minute"], ids=["absent", "a-bare-number", "a-string"])
def test_a_collector_without_a_usable_rate_limit_is_refused(declared: object) -> None:
    """ "Never issued unlimited" is not something a collector opts out of by omission.

    `CPM-AD-20` puts rate limiting in this base rather than per collector, and an
    optional declaration would make the base's version the one nobody uses. A
    bare `60` is refused rather than interpreted: per what?
    """
    built = collector_class(declared_model=fixture_evidence_model(), declared_rate_limit=declared)  # type: ignore[arg-type]

    with pytest.raises(CollectorConfigurationError, match="rate_limit="):
        built(clock=_clock())


def test_the_default_transport_carries_the_declared_timeout_and_retries() -> None:
    """The one place declared values become call settings.

    A collector that injects no transport gets one built from its own
    declarations, so "every outbound call carries a timeout" is true by
    construction rather than by every collector remembering to pass one -- and
    `tests/unit/django_apps/test_collector_base_audit.py` can then sweep for a
    second module that sets one. The retry count travels with it, which is what
    keeps the number the limiter is charged and the number `urllib3` will
    actually attempt from being two different numbers.
    """
    built = collector_class(declared_model=fixture_evidence_model(), declared_retries=A_RETRY_COUNT)

    collector = built(clock=_clock())

    transport = collector._transport  # noqa: SLF001 - the built transport is not otherwise observable
    assert isinstance(transport, RequestsTransport)
    assert transport.timeout == FIXTURE_TIMEOUT
    assert transport._retry.total == A_RETRY_COUNT  # noqa: SLF001 - as above
    collector.close()


def test_one_collection_costs_the_request_plus_its_retry_budget() -> None:
    """The reconciliation `CPM-AD-20` puts retry and rate limiting in one base to make.

    A limiter consulted once per collection, with a retry policy issuing three
    more requests underneath it, turns a declared sixty calls a minute into two
    hundred and forty. The cost is what closes that, and it is arithmetic on the
    same declaration the transport is built from -- so the two cannot come to
    disagree.
    """
    built = collector_class(declared_model=fixture_evidence_model(), declared_retries=A_RETRY_COUNT)
    default = collector_class(declared_model=fixture_evidence_model())
    transport = RecordedTransport(payload=recorded_payload())

    assert built(clock=_clock(), transport=transport).request_cost == 1 + A_RETRY_COUNT
    assert default(clock=_clock(), transport=transport).request_cost == FIXTURE_REQUEST_COST
    assert FIXTURE_REQUEST_COST == 1 + DEFAULT_RETRIES


def test_an_injected_transport_is_used_as_given() -> None:
    """`CPM-AD-27`'s seam, asserted as substitution rather than as a default.

    Every unit case and every integration case but one drives the base through a
    recorded payload; if the base quietly built its own transport anyway, they
    would all be making real calls.
    """
    built = collector_class(declared_model=fixture_evidence_model())
    fake = RecordedTransport(payload=recorded_payload())

    collector = built(clock=_clock(), transport=fake)

    assert collector._transport is fake  # noqa: SLF001 - see the case above


def test_closing_releases_a_transport_the_collector_built() -> None:
    """A collector that built a connection pool should be able to give it back.

    Without this every collector instance leaks a pooled session, which on a
    sweep is one per package.
    """
    closed: list[bool] = []
    built = collector_class(declared_model=fixture_evidence_model())
    collector = built(clock=_clock())
    collector._transport.close = lambda: closed.append(True)  # type: ignore[attr-defined] # noqa: SLF001

    collector.close()

    assert closed == [True]


def test_closing_leaves_a_transport_the_caller_supplied_alone() -> None:
    """Tidy-up must not take a pool away from whoever else is holding it.

    A transport passed in belongs to its caller and may well outlive the
    collector -- a sweep sharing one across packages is the obvious case -- so
    closing it here would be the kind of helpfulness that is only ever
    discovered in production.
    """
    supplied = RequestsTransport(timeout=FIXTURE_TIMEOUT)
    closed: list[bool] = []
    supplied.close = lambda: closed.append(True)  # type: ignore[method-assign]
    built = collector_class(declared_model=fixture_evidence_model())

    with built(clock=_clock(), transport=supplied):
        pass

    assert closed == []


def test_the_window_query_names_all_four_conditions() -> None:
    """Three of the four are matrix rows, and each is one keyword from being absent.

    `collector` and `package_id` are what make the window per collector and per
    package; `status__in` is what makes only a *succeeded* run suppress, so a
    source that is failing does not suppress the very observation that would
    record it as failing. `finished_at` rather than `started_at` is the authority
    for the reason `RunLedgerQuerySet.unfinished()` reads it: it is the column
    the recorder's `finally` writes.
    """
    condition = window_query(collector=FIXTURE_COLLECTOR, package_id=A_PACKAGE, since=FIXED_INSTANT)

    named = dict(condition.children)  # type: ignore[arg-type]

    assert set(named) == EXPECTED_WINDOW_CONDITIONS
    assert named["collector"] == FIXTURE_COLLECTOR
    assert named["package_id"] == A_PACKAGE
    assert named["status__in"] == [RunState.SUCCEEDED.value]
    assert named["finished_at__gte"] == FIXED_INSTANT


def test_only_a_succeeded_run_suppresses_an_observation() -> None:
    """The decision named, so `partial` is excluded on purpose rather than by omission.

    `failed` is the obvious exclusion and the query's docstring argues it.
    `partial` is the quiet one: `CPM-AD-23` writes it when a sweep committed some
    packages and not others, so a `partial` run is one that may never have
    observed *this* package at all -- and counting it as an observation is the
    same false-clean move arriving through the freshness mechanism. `running` is
    excluded because it has not finished, which `finished_at__gte` would exclude
    anyway; naming it here means the set says what it means rather than relying
    on a second condition to say it.
    """
    assert {RunState.SUCCEEDED} == SUPPRESSING_STATES
    assert RunState.PARTIAL not in SUPPRESSING_STATES
    assert RunState.FAILED not in SUPPRESSING_STATES
    assert RunState.RUNNING not in SUPPRESSING_STATES


def test_the_window_query_is_asked_about_one_package_at_a_time() -> None:
    """The other-package row, expressed where it is decided.

    A window query that had lost its `package_id` would suppress every package
    the moment any one of them was collected, and every behavioural case that
    collected a single package would still pass.
    """
    one = window_query(collector=FIXTURE_COLLECTOR, package_id=A_PACKAGE, since=FIXED_INSTANT)
    other = window_query(collector=FIXTURE_COLLECTOR, package_id=ANOTHER_PACKAGE, since=FIXED_INSTANT)

    assert dict(one.children)["package_id"] != dict(other.children)["package_id"]  # type: ignore[arg-type]


def test_the_window_start_is_the_declared_window_before_the_instant_given() -> None:
    """The arithmetic the base does, stated once so the integration cases can read.

    A run that finished exactly on the boundary is inside the window: the
    condition is `>=`, which is the inclusive reading, and it is the safer one --
    the alternative re-observes a fact one microsecond before the window it was
    supposed to be suppressed by. The one place that inclusiveness would be
    wrong is a zero-length window, which `Collector._inside_window`
    short-circuits rather than querying.
    """
    since = FIXED_INSTANT - FIXTURE_WINDOW

    condition = window_query(collector=FIXTURE_COLLECTOR, package_id=A_PACKAGE, since=since)

    assert dict(condition.children)["finished_at__gte"] == FIXED_INSTANT - timedelta(hours=1)  # type: ignore[arg-type]


def test_a_collection_result_is_a_frozen_report() -> None:
    """It reports what happened; it is not a workspace a caller adds to.

    The ledger row is the durable record and this is the immediate answer, and
    the two must agree -- which they cannot if the answer is mutable after the
    row is written.
    """
    result = CollectionResult(state=RunState.SKIPPED, evidence_rows=0, detail="inside the window")

    assert dataclasses.is_dataclass(result)
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.state = RunState.FAILED  # type: ignore[misc]


def test_both_declaration_defects_are_value_errors() -> None:
    """One category of defect, one base class, so a caller catching one catches both.

    `CollectorConfigurationError` and `RateLimitError` are the same kind of
    thing -- a declaration that cannot be used -- and they are raised from two
    modules in one story. Giving them different bases would mean a caller
    guarding a registry against a misdeclared collector caught one and not the
    other. `core/transport.py`'s `TransportError` is deliberately *not* one: a
    source being unreachable is not a declaration defect, and folding it in would
    make `except ValueError` around a call mean something nobody intended.
    """
    assert issubclass(CollectorConfigurationError, ValueError)
    assert issubclass(RateLimitError, ValueError)


def test_sentinel_evidence_takes_every_argument_by_keyword() -> None:
    """The method eight subclasses will implement by hand, so its shape is pinned.

    Every other method in the module is keyword-first, and this one is the place
    that inconsistency would cost something: a positional `state` is the one
    parameter an implementer could reorder, rename or shadow without a type
    checker minding, in the method whose whole job is to say which sentinel a row
    carries.
    """
    parameters = inspect.signature(Collector.sentinel_evidence).parameters

    positional = [
        name
        for name, parameter in parameters.items()
        if name != "self" and parameter.kind is not inspect.Parameter.KEYWORD_ONLY
    ]

    assert positional == []
    assert set(parameters) == {"self", "state", "package_id", "observed_at", "detail"}


def test_a_rate_limit_still_refuses_its_own_bad_values() -> None:
    """The base takes the declaration as given, because `RateLimit` has already checked it.

    Asserted here rather than assumed, because the base's refusal is `is not a
    RateLimit` -- which is only sufficient while constructing one is itself
    guarded.
    """
    with pytest.raises(RateLimitError):
        RateLimit(calls=0, per=timedelta(minutes=1))


@pytest.mark.parametrize(
    "declared",
    [None, -FIXTURE_CACHE_TTL, 3600, "a day", A_SUB_SECOND_LIFETIME],
    ids=["absent", "negative", "a-bare-number", "a-string", "sub-second"],
)
def test_a_collector_without_a_usable_response_cache_ttl_is_refused(declared: object) -> None:
    """The eighth declaration, refused on the same terms as the observation window.

    `NO_CACHE` says "read a body every run" and a reader can see it; `None` says
    nothing at all, and a base that read the two as the same thing would make
    "this collector deliberately does not cache" and "somebody forgot" identical
    in the source. `3600` is the one worth naming for the same reason it is
    named for the window: a perfectly good number of unstated units, which is
    what somebody writes when they are thinking in seconds.

    The sub-second case is `RateLimit.per`'s refusal arriving at the other value
    handed to the same API, and it fails the opposite way round. The lifetime is
    truncated to whole seconds, so half a second becomes `0`, which Django reads
    as *do not cache* -- a collector that declared caching, passed every check,
    and quietly remembers nothing. The value that looks the most generous is the
    one that switches the feature off.
    """
    built = collector_class(declared_model=fixture_evidence_model(), declared_cache_ttl=declared)  # type: ignore[arg-type]

    with pytest.raises(CollectorConfigurationError, match="response_cache_ttl="):
        built(clock=_clock())


def test_the_shortest_usable_lifetime_is_accepted() -> None:
    """The boundary belongs to the permitted side, so the refusal above is exact.

    One second is the smallest lifetime the cache can actually count, and a
    guard written as "anything under a minute is a mistake" would refuse a
    perfectly expressible declaration. The pair to the sub-second case: the
    refusal is about truncating to zero, not about the value being small.
    """
    built = collector_class(declared_model=fixture_evidence_model(), declared_cache_ttl=timedelta(seconds=1))

    assert built(clock=_clock(), transport=RecordedTransport(payload=recorded_payload())) is not None


def test_a_collector_that_caches_nothing_is_accepted() -> None:
    """The declared "fetch a body every run", which must stay expressible.

    The pair to the refusals above. A source that offers no validator, or whose
    body must be re-read whatever it says, is a real collector and says so with
    `NO_CACHE` rather than by omitting the value.
    """
    built = collector_class(declared_model=fixture_evidence_model(), declared_cache_ttl=NO_CACHE)

    assert built(clock=_clock(), transport=RecordedTransport(payload=recorded_payload())) is not None


@pytest.mark.parametrize(
    "declared",
    [
        {"If-None-Match": '"a1b2c3d4"'},
        {"If-Modified-Since": "Wed, 03 Sep 2026 12:00:00 GMT"},
        {"if-none-match": '"a1b2c3d4"'},
    ],
    ids=["entity-tag", "date", "lowercased"],
)
def test_a_collector_that_forges_a_validator_is_refused(declared: dict[str, str]) -> None:
    """The base owns conditional headers, and owning them means refusing them elsewhere.

    A collector sending its own `If-None-Match` asks a source about a body this
    process does not hold. The source answers `304`, the base has no entry to
    replay, and the run fails -- a self-inflicted failure that would look
    exactly like a misbehaving source. Refused at construction, where the answer
    already exists.

    The lowercased spelling is the case a set membership test would miss: HTTP
    header names are case-insensitive on the wire, so `if-none-match` is the
    same header and the same mistake.
    """
    built = collector_class(declared_model=fixture_evidence_model(), declared_headers=declared)

    with pytest.raises(CollectorConfigurationError, match="conditional header"):
        built(clock=_clock())


@pytest.mark.parametrize(
    "declared",
    [None, "User-Agent: cpm", {"User-Agent": 7}, {7: "cpm"}],
    ids=["absent", "a-string", "a-numeric-value", "a-numeric-name"],
)
def test_headers_that_are_not_a_mapping_of_strings_are_refused(declared: object) -> None:
    """Refused for their *type* as well as their content, as every declaration here is.

    A header set that is not a mapping of strings surfaces as a `TypeError` from
    inside `requests` in a worker, with a `running` ledger row already written
    and a source already chosen -- which is the failure every construction-time
    refusal in this module exists to move earlier.
    """
    built = collector_class(declared_model=fixture_evidence_model(), declared_headers=declared)  # type: ignore[arg-type]

    with pytest.raises(CollectorConfigurationError, match="headers="):
        built(clock=_clock())


@pytest.mark.parametrize(
    "declared",
    [
        {"Authorization": "Bearer token\r\nX-Injected: yes"},
        {"Authorization": "Bearer token\nX-Injected: yes"},
        {"User-Agent\r\nX-Injected": "yes"},
    ],
    ids=["crlf-in-value", "lf-in-value", "break-in-name"],
)
def test_a_header_carrying_a_line_break_is_refused(declared: dict[str, str]) -> None:
    """A header is terminated by CRLF, so a line break inside one is another header.

    This is the refusal that is about somebody else's input rather than about a
    typo. Header values are assembled from configuration --
    `Authorization: Bearer $TOKEN` is the shape every one of the named sources
    wants -- so a value carrying a newline is an attacker-influenced string
    becoming a second header on a request this process makes. `requests` refuses
    some of these at call time and not all of them, and a refusal in a worker
    halfway through a sweep is not the same as a refusal where the collector is
    written.
    """
    built = collector_class(declared_model=fixture_evidence_model(), declared_headers=declared)

    with pytest.raises(CollectorConfigurationError, match="line feed"):
        built(clock=_clock())


def test_two_header_names_differing_only_in_case_are_refused() -> None:
    """One header to every origin, two keys to Python, and one of them is discarded.

    Which one survives depends on the order a merge happens to run in, which is
    not something the declaring collector can see and is not stable across a
    refactor. The collector said two things and the source will be told one, so
    the declaration is refused rather than silently halved.
    """
    built = collector_class(
        declared_model=fixture_evidence_model(),
        declared_headers={"User-Agent": "one", "user-agent": "two"},
    )

    with pytest.raises(CollectorConfigurationError, match="more than once"):
        built(clock=_clock())


def test_declared_headers_cannot_be_widened_after_construction() -> None:
    """A collector must not be able to grow its own header set at run time.

    The same property `RateLimit` gets from being frozen: what a collector
    declares is checked once, at construction, and a mutable class attribute
    would make that check a snapshot of a set somebody could add an
    `Authorization` to later -- past the refusal that exists to see it.
    """
    built = collector_class(declared_model=fixture_evidence_model())
    collector = built(clock=_clock(), transport=RecordedTransport(payload=recorded_payload()))

    carried = collector._headers  # noqa: SLF001 - the checked copy is not otherwise observable
    assert carried == FIXTURE_HEADERS
    with pytest.raises(TypeError):
        carried["If-None-Match"] = "forged"  # type: ignore[index]


def test_the_conditional_headers_are_exactly_the_two_a_validator_is_sent_in() -> None:
    """The set the refusal is measured against, stated rather than inferred.

    Two, and no more: a set that had grown a third would refuse a collector for
    declaring a header the base does not in fact compose, and one that had lost
    one would let a forged validator through.
    """
    assert {"If-None-Match", "If-Modified-Since"} == CONDITIONAL_HEADERS


def test_a_request_with_nothing_remembered_carries_only_what_was_declared() -> None:
    """The first observation, and the request `CPM-EVIDENCE-S05` already issued.

    Nothing is remembered, so nothing conditional can be asked, so the call is
    the unconditional one -- carrying the collector's own headers and no more.
    A base that sent a conditional header on a cache miss would be asking about
    a body it does not have.
    """
    assert request_headers(declared=FIXTURE_HEADERS, entry=None) == FIXTURE_HEADERS


@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        (cached_response(etag=AN_ETAG), {"If-None-Match": AN_ETAG}),
        (cached_response(etag=None, last_modified=A_LAST_MODIFIED), {"If-Modified-Since": A_LAST_MODIFIED}),
        (
            cached_response(etag=AN_ETAG, last_modified=A_LAST_MODIFIED),
            {"If-None-Match": AN_ETAG, "If-Modified-Since": A_LAST_MODIFIED},
        ),
    ],
    ids=["entity-tag", "date-only", "both"],
)
def test_a_remembered_entry_adds_the_conditional_header_its_validator_supports(
    entry: CachedResponse,
    expected: dict[str, str],
) -> None:
    """Both validators, and the date-only case is the one the matrix names.

    A source offering no `ETag` is common -- a static file server is the usual
    one -- and a base that only ever sent `If-None-Match` would cache those
    sources' bodies and never once revalidate them, transferring every body
    forever while looking like it had caching.
    """
    composed = request_headers(declared=FIXTURE_HEADERS, entry=entry)

    assert composed == {**FIXTURE_HEADERS, **expected}


def test_the_conditional_request_is_composed_over_the_declared_headers() -> None:
    """The order, asserted rather than left to a dictionary literal's shape.

    The base's conditional headers go on top of the collector's declared ones.
    A collector cannot reach this state today -- `_require_headers` refuses a
    declared validator at construction -- and the order is the second line of
    defence: were that refusal ever bypassed, a forged validator still would not
    reach a source.
    """
    composed = request_headers(declared={"If-None-Match": "forged"}, entry=cached_response(etag=AN_ETAG))

    assert composed == {"If-None-Match": AN_ETAG}


def test_the_fixture_evidence_model_is_invisible_to_the_registry_sweeps() -> None:
    """The fixture is a fixture, and the audits must not police it.

    `tests/model_registry.py`'s sweeps read the global app registry, and a model
    declared at module scope in `core` would join it -- becoming a table no
    migration builds, an evidence model `EVIDENCE.02-AUDIT-001` has an opinion
    about, and a missing migration
    `tests/unit/django_apps/test_migration_completeness.py` would fail on.
    `isolate_apps` is what keeps it out; this asserts the consequence rather than
    trusting the mechanism, and it does it *after* the model has certainly been
    built, which is what makes the assertion mean anything.

    It lives in the unit tier because it needs no database and because the audits
    it guards run here: a guard that only ran under `pixi run test-integration`
    would be absent from the loop where the thing it guards is checked.
    """
    built = fixture_evidence_model()

    registered = {model._meta.db_table for model in apps.get_models()}  # noqa: SLF001 - Django's public-by-convention API

    assert built._meta.db_table == FIXTURE_TABLE  # noqa: SLF001 - as above
    assert FIXTURE_TABLE not in registered


def test_the_fixture_collectors_satisfy_the_bases_contract() -> None:
    """A fixture that had drifted from the base would make every case here vacuous.

    Cheap, and it is the one assertion that fails loudly if a later change to the
    abstract methods leaves `tests/collectors.py` implementing a signature the
    base no longer calls.
    """
    built = collector_class(declared_model=fixture_evidence_model())
    collector: Any = built(clock=_clock(), transport=RecordedTransport(payload=recorded_payload()))

    assert collector.source_for(package_id=A_PACKAGE).endswith(str(A_PACKAGE))
    assert collector.translate(recorded_payload(), package_id=A_PACKAGE, observed_at=FIXED_INSTANT) != []


def test_a_collector_that_declares_nothing_about_applicability_applies_to_every_package() -> None:
    """The hook `CPM-CURRENCY-S02` added has a default, and the default is "applies".

    `inapplicability` is not abstract, deliberately: most collectors' questions
    apply to every package, and the two that predate the hook declare nothing
    new. So the fixture collector -- which overrides nothing here -- must answer
    the empty string for any package, or every case in the integration tier that
    is about the window, the allowance or the transport would be a case about a
    path the base never reached.
    """
    built = collector_class(declared_model=fixture_evidence_model())
    collector: Any = built(clock=_clock(), transport=RecordedTransport(payload=recorded_payload()))

    assert collector.inapplicability(package_id=A_PACKAGE) == ""
    assert collector.inapplicability(package_id=ANOTHER_PACKAGE) == ""
    assert Collector.inapplicability(collector, package_id=A_PACKAGE) == ""


def test_the_two_collectors_that_predate_the_hook_declare_nothing_new() -> None:
    """ "The existing collectors declare nothing new", asserted on the real classes.

    `CPM-CURRENCY-S02` added `inapplicability` with a default so that
    `SourceReleaseCollector` and `InventoryIngestionCollector` would not have to
    change. A later edit that gave either of them an override would change what
    their runs write, and every case about those runs would still pass -- so the
    absence of an override is pinned here, by identity with the base's method.
    """
    assert SourceReleaseCollector.inapplicability is Collector.inapplicability
    assert InventoryIngestionCollector.inapplicability is Collector.inapplicability


def test_a_collector_that_declares_nothing_about_sentinel_rows_writes_the_one_row_it_shapes() -> None:
    """The hook `CPM-CURRENCY-S04` added has a default, and the default is "one row".

    `sentinel_evidence_rows` is not abstract for the reason `inapplicability` is
    not: one sentinel row is the right answer for every collector that observes
    one surface per package, which is every one that predates it. So the fixture
    collector -- which overrides nothing here -- must answer with exactly the row
    `sentinel_evidence` shapes, or every case in the integration tier about an
    `error` or `not_found` row would be a case about a path the base no longer
    takes.
    """
    built = collector_class(declared_model=fixture_evidence_model())
    collector: Any = built(clock=_clock(), transport=RecordedTransport(payload=recorded_payload()))

    rows = collector.sentinel_evidence_rows(
        state=OutcomeState.ERROR,
        package_id=A_PACKAGE,
        observed_at=FIXED_INSTANT,
        detail=A_REASON,
    )
    shaped = collector.sentinel_evidence(
        state=OutcomeState.ERROR,
        package_id=A_PACKAGE,
        observed_at=FIXED_INSTANT,
        detail=A_REASON,
    )

    assert len(rows) == 1
    assert type(rows[0]) is type(shaped)
    assert rows[0].state == shaped.state == OutcomeState.ERROR.value
    assert rows[0].detail == shaped.detail == A_REASON
    # And through the base's own unbound method, so the default is the base's
    # rather than something the fixture happens to inherit from elsewhere.
    assert len(Collector.sentinel_evidence_rows(collector, state=OutcomeState.ERROR, **_SENTINEL_ASK)) == 1


def test_the_collectors_that_predate_the_plural_sentinel_hook_declare_nothing_new() -> None:
    """ "The existing collectors declare nothing new", asserted on the real classes.

    `CPM-CURRENCY-S04` added `sentinel_evidence_rows` with a default so that the
    four collectors before it would not have to change. A later edit that gave
    any of them an override would change what their failing runs write, and every
    case about those runs would still pass -- so the absence of an override is
    pinned here, by identity with the base's method, exactly as
    `test_the_two_collectors_that_predate_the_hook_declare_nothing_new` pins
    `inapplicability`.

    `CPM-SECURITY-S01`'s collector, written *after* the hook existed, inherits it
    too and for the reason the four before it do: it observes one surface per
    package, so one sentinel row is the whole of what any sentinel path owes it.
    A collector added after a hook is where an unnecessary override is likeliest,
    which is why it is pinned here rather than only in its own module.

    `CondaPackageCollector` is asserted to be the one that *does* override it,
    which is the anti-vacuity half: an identity check over five classes would pass
    just as happily if the hook had never been overridden by anybody.
    """
    assert InventoryIngestionCollector.sentinel_evidence_rows is Collector.sentinel_evidence_rows
    assert SourceReleaseCollector.sentinel_evidence_rows is Collector.sentinel_evidence_rows
    assert PyPIReleaseCollector.sentinel_evidence_rows is Collector.sentinel_evidence_rows
    assert FeedstockCollector.sentinel_evidence_rows is Collector.sentinel_evidence_rows
    assert VulnerabilityCollector.sentinel_evidence_rows is Collector.sentinel_evidence_rows
    assert KevCollector.sentinel_evidence_rows is Collector.sentinel_evidence_rows
    assert IdentityResolutionCollector.sentinel_evidence_rows is Collector.sentinel_evidence_rows
    assert CondaPackageCollector.sentinel_evidence_rows is not Collector.sentinel_evidence_rows


def test_the_selection_hook_answers_none_and_the_cadence_declaration_is_absent_by_default() -> None:
    """`CPM-CURRENCY-S05`'s two additions, and both defaults say "nothing schedules this".

    A collector written before this story declares neither, which is the only
    shape in which this base grows. `None` from `selectable_packages` is not an
    empty selection -- it is "this collector is not swept one package at a time",
    which `collectors/sweep.py` refuses by name; and `cadence = None` is what keeps
    `config/startup/stage_two.py` from demanding a `CELERY_BEAT_SCHEDULE` entry
    for a collector nothing sweeps.

    Asserted on the base and on the fixture, because a subclass could shadow
    either without the base having changed.
    """
    built = collector_class(declared_model=fixture_evidence_model())

    assert Collector.cadence is None
    assert Collector.selectable_packages() is None
    assert built.cadence is None
    assert built.selectable_packages() is None


def test_the_selection_hook_is_read_off_the_class_rather_than_an_instance() -> None:
    """A classmethod, where the other two defaulted hooks are instance methods.

    Both callers read it off a class: a dispatch selects and enqueues without ever
    collecting, and constructing a collector to ask would build a
    `RequestsTransport` and its connection pool for a dispatch that makes no call
    -- the same hazard `config/startup/stage_two.py` avoids by sweeping classes.
    A hook that had quietly become an instance method would still answer through
    an instance and would fail at the two places that matter.
    """
    assert isinstance(inspect.getattr_static(Collector, "selectable_packages"), classmethod)

    parameters = inspect.signature(Collector.selectable_packages).parameters

    assert set(parameters) == set()


def test_the_collectors_that_predate_the_selection_hook_declare_nothing_new() -> None:
    """The additive half, on the real classes rather than on a fixture.

    Inventory ingestion is run-scoped -- it reads one document naming many
    packages (`CPM-AD-25`) and refuses all three per-package hooks -- so it
    inherits the default, and a later edit that gave it a selection would put it
    on a per-package sweep it cannot serve. The seven per-package collectors are
    asserted to be the ones that *do* override it, which is the anti-vacuity half:
    an identity check over one class would pass just as happily if nobody had
    overridden the hook at all.
    """
    assert InventoryIngestionCollector.selectable_packages() is None
    assert InventoryIngestionCollector.cadence is None

    for collector in (
        SourceReleaseCollector,
        PyPIReleaseCollector,
        FeedstockCollector,
        CondaPackageCollector,
        VulnerabilityCollector,
        KevCollector,
        IdentityResolutionCollector,
    ):
        assert collector.selectable_packages() is not None
        assert collector.cadence is not None


def test_sentinel_evidence_rows_takes_every_argument_by_keyword() -> None:
    """The fifth hook a subclass may implement by hand, pinned as the other four are.

    A positional parameter is the one place hand-written signatures could quietly
    disagree, and this one is called on every path that records a failure.
    """
    parameters = inspect.signature(Collector.sentinel_evidence_rows).parameters

    positional = [
        name
        for name, parameter in parameters.items()
        if name != "self" and parameter.kind is not inspect.Parameter.KEYWORD_ONLY
    ]

    assert positional == []
    assert set(parameters) == {"self", "state", "package_id", "observed_at", "detail"}


def test_a_collector_that_owes_several_sentinel_rows_is_asked_for_all_of_them() -> None:
    """The shape the hook exists for: one collection, several surfaces, a row each.

    Asserted through the base's own checking helper rather than by calling the
    override directly, because what the story needed was for the *base* to write
    whatever the hook returns -- and a case that only called the method would pass
    against a base that still wrote one row.
    """
    built = several_sentinels_collector_class(declared_model=fixture_evidence_model())
    collector: Any = built(clock=_clock(), transport=RecordedTransport(payload=recorded_payload()))

    rows = collector._sentinel_rows(OutcomeState.NOT_FOUND, **_SENTINEL_ASK)  # noqa: SLF001 - the base's own check

    assert len(rows) == SEVERAL_SENTINEL_ROWS
    assert {row.state for row in rows} == {OutcomeState.NOT_FOUND.value}
    assert len({row.body for row in rows}) == SEVERAL_SENTINEL_ROWS


def test_a_sentinel_row_set_carrying_the_state_somewhere_may_carry_other_states_elsewhere() -> None:
    """The verbatim check reads "at least one row", and that relaxation is what the plural hook needs.

    The state the base decided is about the *one call the base made*. A collector
    answering for surfaces that call never touched can only report what each of
    them said -- "this other channel publishes 2.1.3" is an observation, not a
    sentinel that forgot which sentinel it is -- so requiring every row to carry
    the base's state would make the hook useless for the one collector it exists
    for.

    What the check still guarantees is the half `CPM-AD-24` is actually about: the
    base's own answer is on the record.
    `test_a_sentinel_that_ignores_the_state_it_was_asked_for_is_refused` in the
    integration tier is the case that did not move.
    """
    built = several_sentinels_collector_class(declared_model=fixture_evidence_model())
    collector: Any = built(clock=_clock(), transport=RecordedTransport(payload=recorded_payload()))
    model = fixture_evidence_model()
    mixed = [
        collector.sentinel_evidence(state=OutcomeState.NOT_FOUND, **_SENTINEL_ASK),
        model(
            observed_at=FIXED_INSTANT,
            package_id=A_PACKAGE,
            state=DETERMINATE_VALUE,
            detail="",
            body="another surface answered",
            source="",
        ),
    ]

    collector.sentinel_evidence_rows = lambda **_: mixed  # type: ignore[method-assign]

    kept = collector._sentinel_rows(OutcomeState.NOT_FOUND, **_SENTINEL_ASK)  # noqa: SLF001 - the base's own check

    assert kept == mixed
    assert {row.state for row in kept} == {OutcomeState.NOT_FOUND.value, DETERMINATE_VALUE}


@pytest.mark.parametrize(
    "state",
    [OutcomeState.ERROR, OutcomeState.NOT_APPLICABLE],
    ids=lambda state: state.value,
)
def test_a_determinate_row_beside_a_sentinel_is_refused_on_the_paths_that_declared_a_verdict(
    state: OutcomeState,
) -> None:
    """ "Never a clean result" is a rule about the run, not only about one row.

    `_failed` and `_not_applicable` both declare the run's verdict *before* the
    rows are written, so a determinate row returned there is permanent evidence
    that the source answered cleanly underneath a ledger row saying the run did
    not. The relaxed "at least one row carries the state" check does not catch it
    on its own -- the required sentinel is present and the determinate row rides
    beside it -- which is exactly why this second check exists.
    """
    built = collector_class(declared_model=fixture_evidence_model())
    collector: Any = built(clock=_clock(), transport=RecordedTransport(payload=recorded_payload()))
    mixed = [
        collector.sentinel_evidence(state=state, **_SENTINEL_ASK),
        _a_determinate_row(),
    ]
    collector.sentinel_evidence_rows = lambda **_: mixed  # type: ignore[method-assign]

    with pytest.raises(CollectorConfigurationError, match=OutcomeState.OK.value):
        collector._sentinel_rows(state, **_SENTINEL_ASK)  # noqa: SLF001 - the base's own check


def test_a_determinate_row_beside_an_absence_is_kept_because_that_is_what_the_hook_is_for() -> None:
    """The other half, and the one the refusal above must not take with it.

    `not_found` is written on a path the base finalizes `succeeded`: the source
    answered, and a collector observing several surfaces reports what each of them
    said. One surface having the thing while another does not is the observation,
    not a contradiction -- so the same set that is refused above is kept here.
    """
    built = collector_class(declared_model=fixture_evidence_model())
    collector: Any = built(clock=_clock(), transport=RecordedTransport(payload=recorded_payload()))
    mixed = [
        collector.sentinel_evidence(state=OutcomeState.NOT_FOUND, **_SENTINEL_ASK),
        _a_determinate_row(),
    ]
    collector.sentinel_evidence_rows = lambda **_: mixed  # type: ignore[method-assign]

    assert collector._sentinel_rows(OutcomeState.NOT_FOUND, **_SENTINEL_ASK) == mixed  # noqa: SLF001 - as above


def test_the_state_a_row_carries_is_read_from_its_state_column_and_not_from_any_field() -> None:
    """`CPM-AD-24` makes the value a short lowercase word, and short words turn up in other columns.

    A `detail` reading "error", a `source` naming a host called `ok` -- either
    would satisfy a scan of every field, and a hook answering with several rows
    makes an incidental match that many times likelier. The check reads the column
    the outcome is declared in, so a row whose *state* is determinate is caught
    however many other fields happen to spell the sentinel.
    """
    built = collector_class(declared_model=fixture_evidence_model())
    collector: Any = built(clock=_clock(), transport=RecordedTransport(payload=recorded_payload()))
    model = fixture_evidence_model()
    disguised = model(
        observed_at=FIXED_INSTANT,
        package_id=A_PACKAGE,
        state=DETERMINATE_VALUE,
        detail=OutcomeState.ERROR.value,
        body=OutcomeState.ERROR.value,
        source=OutcomeState.ERROR.value,
    )
    collector.sentinel_evidence_rows = lambda **_: [disguised]  # type: ignore[method-assign]

    with pytest.raises(CollectorConfigurationError, match="sentinel_evidence_rows was asked for"):
        collector._sentinel_rows(OutcomeState.ERROR, **_SENTINEL_ASK)  # noqa: SLF001 - the base's own check


def test_a_sentinel_row_declaring_no_state_column_is_refused_by_name() -> None:
    """The check reads one column, so a row that has no such column has nothing to check.

    `CollectionRun` is the nearest real model without one -- it spells its outcome
    `status`, because it records how a *run* ended rather than what a source said
    -- and it is what a collector reaching for the wrong table would return. The
    refusal names the column rather than failing on a missing attribute several
    frames later.
    """
    built = collector_class(declared_model=fixture_evidence_model())
    collector: Any = built(clock=_clock(), transport=RecordedTransport(payload=recorded_payload()))
    collector.sentinel_evidence_rows = lambda **_: [CollectionRun()]  # type: ignore[method-assign]

    with pytest.raises(CollectorConfigurationError, match=STATE_FIELD):
        collector._sentinel_rows(OutcomeState.ERROR, **_SENTINEL_ASK)  # noqa: SLF001 - the base's own check


def test_a_sentinel_hook_that_returns_nothing_is_refused_rather_than_writing_no_row() -> None:
    """`CPM-NFR-3`: never a clean result, and never no row -- including through the new hook.

    This is the way a defaulted hook can turn the guarantee off silently: every
    path that reaches it has already promised an observation, and it is a path
    that is *already* recording a failure, so an empty answer would leave a failed
    run with nothing on the record and nothing to notice.
    """
    built = barren_sentinel_collector_class(declared_model=fixture_evidence_model())
    collector: Any = built(clock=_clock(), transport=RecordedTransport(payload=recorded_payload()))

    with pytest.raises(CollectorConfigurationError, match="no rows"):
        collector._sentinel_rows(OutcomeState.ERROR, **_SENTINEL_ASK)  # noqa: SLF001 - the base's own check


def test_a_sentinel_hook_that_returns_a_row_rather_than_rows_is_refused() -> None:
    """The other plausible way to implement the plural hook wrongly, refused where it is readable.

    A single model instance is not a sequence, so left to reach `bulk_create` it
    would surface as a message about a field rather than about the hook -- and a
    `str`, which *is* a sequence, would surface as a row per character.
    """
    built = unsequenced_sentinel_collector_class(declared_model=fixture_evidence_model())
    collector: Any = built(clock=_clock(), transport=RecordedTransport(payload=recorded_payload()))

    with pytest.raises(CollectorConfigurationError, match="rather than a sequence"):
        collector._sentinel_rows(OutcomeState.ERROR, **_SENTINEL_ASK)  # noqa: SLF001 - the base's own check


def test_inapplicability_takes_its_argument_by_keyword() -> None:
    """The fourth hook subclasses implement by hand, pinned as the third is.

    `test_sentinel_evidence_takes_every_argument_by_keyword` says why: a
    positional parameter is the one place eight hand-written signatures could
    quietly disagree, and this one is read by the base before anything else in a
    run.
    """
    parameters = inspect.signature(Collector.inapplicability).parameters

    positional = [
        name
        for name, parameter in parameters.items()
        if name != "self" and parameter.kind is not inspect.Parameter.KEYWORD_ONLY
    ]

    assert positional == []
    assert set(parameters) == {"self", "package_id"}
