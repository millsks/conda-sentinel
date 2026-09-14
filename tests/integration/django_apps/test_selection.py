"""`CPM-IDENTITY-S04`: the review queue against a real database, on both backends.

`tests/unit/django_apps/test_selection.py` proves the ordering *rule* and proves it
in the tier a developer runs. This module proves the rule is what the queue
actually comes back in when the rows are in a table, that the set-based read
agrees with the per-package one it extends, and that the whole thing is the same
on SQLite and on PostgreSQL.

**Every case here runs twice in practice**, once on SQLite locally and once on
PostgreSQL under `pixi run gate-postgres`, and the second run is not a formality:
the breadth columns are nullable by construction, and the two backends disagree
about where a NULL sorts -- in opposite directions on ascending and descending
orders. Several cases below are arranged so that the *natural* order of the
database disagrees with the order the queue must produce, because a case whose
expectation the database would satisfy by accident proves nothing about the code
that was supposed to produce it.

**The rows are written directly rather than through the ingestion collector.**
`tests/integration/django_apps/test_inventory_ingestion.py` owns the path that
produces snapshots and already proves it produces them; what this module needs is
particular *arrangements* -- two observations at one instant, a latest that is an
error, a package whose only snapshot is later than the cut-off -- and driving the
collector to produce each would make the arrangement a consequence of a document
format rather than the thing under test. `InventorySnapshot.objects.create` is an
insert, which is the one write an append-only model permits.

Every case rolls back: `@pytest.mark.django_db` wraps each in a transaction.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from typing import Final

import pytest
import structlog
from django.db import connection
from django.test.utils import CaptureQueriesContext

from conda_sentinel.collectors import selection as selection_module
from conda_sentinel.collectors.models import InventoryReadError
from conda_sentinel.collectors.models import InventorySnapshot
from conda_sentinel.collectors.models import snapshot_as_of
from conda_sentinel.collectors.selection import QUEUE_SELECTED_EVENT
from conda_sentinel.collectors.selection import select_unresolved
from conda_sentinel.collectors.selection import unresolved_packages
from conda_sentinel.core.outcomes import OutcomeState
from conda_sentinel.identity.models import ESTABLISHED
from conda_sentinel.identity.models import UNKNOWN
from conda_sentinel.identity.models import IdentityConfidence
from conda_sentinel.identity.models import MappingKind
from conda_sentinel.identity.models import Package
from conda_sentinel.identity.models import PackageMapping
from tests.clocks import FIXED_INSTANT
from tests.clocks import LATER_INSTANT
from tests.clocks import OBSERVATION_GAP

if TYPE_CHECKING:
    from collections.abc import Iterator
    from datetime import datetime

    from structlog.typing import EventDict

#: The instant every case reads as of unless it is about the cut-off itself.
#: `FIXED_INSTANT`, so that a case can place an observation on either side of it
#: without needing a fourth constant.
CUTOFF: Final = FIXED_INSTANT

#: An instant after the cut-off, for the cases about evidence the queue must not
#: see. It is `tests/clocks.py`'s `LATER_INSTANT`, which that module derives as
#: `FIXED_INSTANT + OBSERVATION_GAP`; the derivation is deliberately not repeated
#: here, and `test_the_instants_these_cases_are_built_on_are_in_the_order_they_are_named_in`
#: below is what stops this module from silently testing the opposite claim if the
#: two ever swapped.
AFTER_THE_CUTOFF: Final = LATER_INSTANT

#: An instant before the cut-off, for the cases that need two observations of one
#: package on the near side of it: a present one, and then the absence that
#: supersedes it.
BEFORE_THE_CUTOFF: Final = FIXED_INSTANT - OBSERVATION_GAP

#: A cut-off late enough to include `AFTER_THE_CUTOFF`, for the half of the
#: replay case that reads the later evidence.
LATER_CUTOFF: Final = LATER_INSTANT + OBSERVATION_GAP

#: Three component counts, ordered, so an assertion about rank is about the values
#: rather than about the order the packages were created in.
A_SMALL_COUNT: Final[int] = 2
A_MIDDLING_COUNT: Final[int] = 20
A_LARGE_COUNT: Final[int] = 200

#: The line-of-business count most cases carry. Held constant so that a case about
#: the component count is not accidentally about this one as well.
A_LOB_COUNT: Final[int] = 4

#: A larger one, for the case that ties on components and separates on this.
A_LARGER_LOB_COUNT: Final[int] = 40

#: A confidence value `IdentityConfidence` does not declare, written straight into
#: the column because Django enforces `choices` on neither `save()` nor
#: `create()`. It stands for what a version skew or a hand-run data fix leaves
#: behind, and it is what the complement-in-SQL case is built on. The spelling is
#: `tests/integration/django_apps/test_rollup.py`'s, which uses it for the same
#: kind of fault.
AN_UNRECOGNISED_CONFIDENCE: Final[str] = "asserted"

#: The event the capture fixture logs to prove the capture is live before a case
#: asserts over what it caught. The pattern and its reason are
#: `tests/integration/django_apps/test_rollup.py`'s.
CAPTURE_CONTROL: Final[str] = "selection-capture-control"

#: `PackageMapping`'s table, as it appears in emitted SQL. Read from the model
#: rather than spelled, so a renamed table does not turn the ordering assertion
#: below into one that silently matches no query and unpacks an empty list.
MAPPING_TABLE: Final[str] = PackageMapping._meta.db_table  # noqa: SLF001 - `_meta` is Django's own public-by-convention API

#: The three package counts the query-count case measures at. AC 5 is about the
#: number of queries being *independent* of how many packages there are, so it is
#: asserted as an equality across cardinalities rather than against a hard-coded
#: number -- which would be a fact about one arrangement wearing the name of a
#: property.
QUERY_COUNT_CARDINALITIES: Final[tuple[int, ...]] = (1, 3, 9)


@pytest.fixture
def captured_events(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[EventDict]]:
    """Capture what `collectors/selection.py` logs, with the two guards the plain helper lacks.

    The reasoning is `tests/unit/test_drain.py`'s and is not restated: the
    module-scope logger is rebound so `capture_logs` binds a fresh proxy inside its
    own processor chain, and a control event proves the capture is live before the
    case runs.

    Args:
        monkeypatch: pytest's patcher, which restores the module's own logger.

    Yields:
        The captured events, in order.

    """
    monkeypatch.setattr(selection_module, "logger", structlog.get_logger(selection_module.__name__))
    with structlog.testing.capture_logs() as captured:
        selection_module.logger.warning(CAPTURE_CONTROL)
        assert [event["event"] for event in captured] == [CAPTURE_CONTROL], (
            "structlog.testing.capture_logs() cannot see collectors.selection's logger, so every "
            "assertion over what it logged would be vacuous"
        )
        captured.clear()
        yield captured


def _a_package(name: str, *, confidence: str = IdentityConfidence.UNMAPPED.value) -> Package:
    """Create one package at a stated identity confidence.

    Args:
        name: Its canonical name, which is unique.
        confidence: How certain its identity is (`CPM-AD-4`). Defaults to
            `unmapped`, because that is what most of these cases are about.

    Returns:
        The saved `Package`.

    """
    return Package.objects.create(canonical_name=name, resolved_at=FIXED_INSTANT, confidence=confidence)


def _observed(
    package: Package,
    *,
    components: int,
    lobs: int = A_LOB_COUNT,
    at: datetime = FIXED_INSTANT,
) -> InventorySnapshot:
    """Record one present observation of a package's internal usage.

    Args:
        package: The package observed.
        components: How many internal components used it.
        lobs: How many lines of business did.
        at: When the observation was made.

    Returns:
        The saved snapshot.

    """
    return InventorySnapshot.objects.create(
        observed_at=at,
        package=package,
        source_package_key=package.canonical_name,
        state=OutcomeState.OK.value,
        internal_component_count=components,
        internal_lob_count=lobs,
    )


def _observed_nothing(package: Package, *, state: str, at: datetime = FIXED_INSTANT) -> InventorySnapshot:
    """Record one observation that carries no counts, by the check constraint.

    Args:
        package: The package the source said nothing usable about.
        state: Any `OutcomeState` but `ok` -- `not_found` for a package the source
            stopped listing, `error` for a look that failed, and the two remaining
            sentinels, which the constraint treats identically.
        at: When it was observed.

    Returns:
        The saved snapshot, with both counts NULL.

    """
    return InventorySnapshot.objects.create(
        observed_at=at,
        package=package,
        source_package_key=package.canonical_name,
        state=state,
    )


def _names(cutoff: datetime = CUTOFF) -> list[str]:
    """Return the queue's package names in the order the queue puts them.

    Names rather than rows, because every ordering assertion here is about
    *sequence* and a list of canonical names is the one spelling of that a failure
    message can be read straight off.

    Args:
        cutoff: The instant to select as of.

    Returns:
        The canonical names, ranked.

    """
    return [entry.package.canonical_name for entry in unresolved_packages(cutoff=cutoff)]


# ---------------------------------------------------------------------------
# The instants these cases are built on.
# ---------------------------------------------------------------------------


def test_the_instants_these_cases_are_built_on_are_in_the_order_they_are_named_in() -> None:
    """The guard that stops this module from asserting the opposite of what it says.

    Half the cases here mean nothing unless `BEFORE_THE_CUTOFF` really is before
    the cut-off and `AFTER_THE_CUTOFF` really is after it. The four constants are
    derived from two in `tests/clocks.py`, so a change there could reorder them
    with nothing failing -- every case would still pass, testing a claim nobody
    made. No database: this is arithmetic on constants.
    """
    assert BEFORE_THE_CUTOFF < CUTOFF < AFTER_THE_CUTOFF < LATER_CUTOFF


# ---------------------------------------------------------------------------
# AC #1: the unresolved set.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_only_the_two_unresolved_confidences_are_returned() -> None:
    """`unmapped` and `inventory-derived` are the queue; `verified` is not in it.

    `CPM-FR-4`'s queue is the packages resolution did not conclude about. A
    verified package has an identity a person established, and putting it in front
    of another person is asking a question that has already been answered.
    """
    _a_package("unmapped-one")
    _a_package("derived-one", confidence=IdentityConfidence.INVENTORY_DERIVED.value)
    _a_package("verified-one", confidence=IdentityConfidence.VERIFIED.value)

    assert set(_names()) == {"unmapped-one", "derived-one"}


@pytest.mark.django_db
def test_a_confidence_the_enum_does_not_declare_is_reviewed_rather_than_dropped() -> None:
    """The complement is taken in SQL, which is what makes the fail-safe real.

    Django validates `choices` on neither `save()` nor `create()`, so a data
    migration or a hand-run fix can leave a confidence in the column that
    `IdentityConfidence` has never heard of -- and so can removing a member while
    rows still carry it. A positive `confidence__in=UNRESOLVED_CONFIDENCES` filter
    would drop such a package out of the review queue silently, which is the one
    direction the module argues must not happen: a package nobody looks at is worse
    than a question a human answers once (`CPM-FR-2`).
    """
    _a_package("unrecognised", confidence=AN_UNRECOGNISED_CONFIDENCE)
    _a_package("verified-one", confidence=IdentityConfidence.VERIFIED.value)

    assert _names() == ["unrecognised"]


@pytest.mark.django_db
def test_a_package_that_reaches_verified_leaves_the_queue() -> None:
    """The queue is a query, so working an item is what removes it -- not a row saying so.

    The story forbids a queue table and `CPM-AD-22` puts every queue in `workflow`.
    This is what that buys: nothing has to mark the package handled, because the
    confidence *is* the handling.
    """
    package = _a_package("becomes-verified")
    _observed(package, components=A_LARGE_COUNT)
    assert _names() == ["becomes-verified"]

    package.confidence = IdentityConfidence.VERIFIED.value
    package.save(update_fields=["confidence"])

    assert _names() == []


@pytest.mark.django_db
def test_nothing_to_review_is_an_empty_queue_rather_than_an_error() -> None:
    """An ordinary question with an ordinary answer.

    Raising would push every caller into a try block for the state the product is
    trying to reach.
    """
    _a_package("all-done", confidence=IdentityConfidence.VERIFIED.value)

    assert unresolved_packages(cutoff=CUTOFF) == []


# ---------------------------------------------------------------------------
# AC #2: the ranking, its tiebreak, and its stability across two calls.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_packages_come_back_ranked_by_the_breadth_their_latest_snapshot_recorded() -> None:
    """`CPM-FR-4`: most used first, which is the whole reason this is a queue.

    Created in an order that is neither the ranking nor its reverse, so a queue
    that happened to return insertion order would fail here.
    """
    _observed(_a_package("middling"), components=A_MIDDLING_COUNT)
    _observed(_a_package("largest"), components=A_LARGE_COUNT)
    _observed(_a_package("smallest"), components=A_SMALL_COUNT)

    assert _names() == ["largest", "middling", "smallest"]


@pytest.mark.django_db
def test_the_line_of_business_count_ranks_packages_that_tie_on_components() -> None:
    """The second half of the pair, which is where a weighted score would have gone.

    PRD Open Question 8 leaves how the two counts combine undecided, so the order
    is lexicographic over the pair. A package used by more lines of business at the
    same component count is the wider exposure, and that is as much as this story
    is entitled to say.
    """
    _observed(_a_package("one-line-of-business"), components=A_MIDDLING_COUNT, lobs=A_LOB_COUNT)
    _observed(_a_package("many-lines-of-business"), components=A_MIDDLING_COUNT, lobs=A_LARGER_LOB_COUNT)

    assert _names() == ["many-lines-of-business", "one-line-of-business"]


@pytest.mark.django_db
def test_two_snapshots_of_one_package_at_one_instant_are_resolved_by_the_highest_key() -> None:
    """The tiebreak inside the fold, which is the normal case rather than an exotic one.

    One sweep stamps every row it writes with the run's single instant
    (`CPM-AD-7`), so two observations of one package sharing an `observed_at` is
    what ordinary operation produces -- two runs at one instant, or one run writing
    an observation and an absence.

    **This case bites on both backends**, which took a design change to achieve
    rather than only a test. While the fold resolved the tie by "last row of an
    ordered stream wins", narrowing it to `observed_at` alone still produced the
    right answer on SQLite -- tied rows come back in rowid order there, so the last
    one is the highest primary key whether or not anybody asked -- and the defect
    would have surfaced only under PostgreSQL. The fold now compares
    `(observed_at, pk)` in Python, so a stamp narrowed to the instant resolves the
    tie to the *first* row of a totally ordered stream on every backend, and this
    case fails locally as well as in the gate.

    The two rows disagree by enough to change the *rank*, not merely the stored
    number, so the failure shows up as a queue in the wrong order rather than as an
    attribute nobody compares.
    """
    contested = _a_package("two-rows-one-instant")
    _observed(contested, components=A_LARGE_COUNT, at=CUTOFF)
    _observed(contested, components=A_SMALL_COUNT, at=CUTOFF)
    _observed(_a_package("steady"), components=A_MIDDLING_COUNT, at=CUTOFF)

    queue = unresolved_packages(cutoff=CUTOFF)

    assert [entry.package.canonical_name for entry in queue] == ["steady", "two-rows-one-instant"]
    assert queue[1].internal_component_count == A_SMALL_COUNT


@pytest.mark.django_db
def test_two_calls_at_one_cut_off_return_the_identical_sequence() -> None:
    """AC 2's replay stability, asserted by calling twice rather than by reading the code.

    The four packages tie in pairs on purpose: two with identical breadth and two
    with none at all. Without the surrogate-key tiebreak both pairs would come back
    in whatever order the database happened to produce, which is not a promise any
    database makes -- and the two calls would agree only by luck.
    """
    tied_first = _a_package("tied-a")
    tied_second = _a_package("tied-b")
    _observed(tied_first, components=A_MIDDLING_COUNT)
    _observed(tied_second, components=A_MIDDLING_COUNT)
    blank_first = _a_package("blank-a")
    blank_second = _a_package("blank-b")

    first_call = [entry.package.pk for entry in unresolved_packages(cutoff=CUTOFF)]
    second_call = [entry.package.pk for entry in unresolved_packages(cutoff=CUTOFF)]

    assert first_call == second_call
    assert first_call == [tied_first.pk, tied_second.pk, blank_first.pk, blank_second.pk]


@pytest.mark.django_db
def test_packages_with_identical_breadth_are_ordered_by_the_surrogate_key() -> None:
    """The tiebreak, on the key rather than on the name.

    The names are chosen so that alphabetical order is the *reverse* of primary-key
    order: a queue that had fallen back to the model's own ordering, or to a name,
    would fail here rather than pass by coincidence.
    """
    first_created = _a_package("zulu")
    second_created = _a_package("alpha")
    _observed(first_created, components=A_MIDDLING_COUNT)
    _observed(second_created, components=A_MIDDLING_COUNT)

    assert _names() == ["zulu", "alpha"]
    assert first_created.pk < second_created.pk


# ---------------------------------------------------------------------------
# AC #3: the cut-off fixes what is read, and the fold agrees with `snapshot_as_of`.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_the_breadth_at_the_cut_off_ranks_the_package_rather_than_the_later_one() -> None:
    """AC 3: a replayed run reads what the run it replays read (`CPM-FR-22`).

    One package's usage grows after the cut-off. Read at the earlier cut-off it is
    still the smaller of the two; read at the later one it is the larger. The same
    rows answer both, which is what makes the queue a function of its cut-off
    rather than of when it was asked.
    """
    grower = _a_package("grew-later")
    _observed(grower, components=A_SMALL_COUNT, at=CUTOFF)
    _observed(grower, components=A_LARGE_COUNT, at=AFTER_THE_CUTOFF)
    _observed(_a_package("steady"), components=A_MIDDLING_COUNT, at=CUTOFF)

    assert _names(cutoff=CUTOFF) == ["steady", "grew-later"]
    assert _names(cutoff=LATER_CUTOFF) == ["grew-later", "steady"]


@pytest.mark.django_db
def test_a_package_that_departs_after_the_cut_off_keeps_its_breadth_at_the_cut_off() -> None:
    """The mirror of the absence case, and the one most likely to break silently.

    The package was observed with real usage before the cut-off and stopped being
    listed after it. At this cut-off the departure has not happened yet, so the
    package ranks on its counts -- an implementation that dropped
    `observed_at__lte` from the fold would read the absence, leave the package
    out (`CPM-OPERATE-S11`), and every other case in this module would still pass.
    At the later cut-off the departure has happened, and the package is not offered.
    """
    departs = _a_package("departs-later")
    _observed(departs, components=A_LARGE_COUNT, at=BEFORE_THE_CUTOFF)
    _observed_nothing(departs, state=OutcomeState.NOT_FOUND.value, at=AFTER_THE_CUTOFF)
    _observed(_a_package("stays"), components=A_MIDDLING_COUNT, at=BEFORE_THE_CUTOFF)

    assert _names(cutoff=CUTOFF) == ["departs-later", "stays"]
    assert _names(cutoff=LATER_CUTOFF) == ["stays"]
    assert select_unresolved(cutoff=CUTOFF).left_out_for_absence == 0
    assert select_unresolved(cutoff=LATER_CUTOFF).left_out_for_absence == 1


@pytest.mark.django_db
def test_the_fold_returns_what_the_per_package_read_returns_for_every_arrangement() -> None:
    """The module's central correctness claim, asserted by calling both.

    `collectors/absence.py`'s `histories_at` -- the fold the selection reads its
    breadth off -- says the surviving row is "the same row `snapshot_as_of` returns
    for the same `(package, cutoff)` pair", which is what makes the set-based read
    an extension of `CPM-AD-25`'s rule rather than a second answer to its question.
    Without a differential case the two can drift -- a changed tiebreak, a changed
    boundary, a changed absence rule -- and each would still look right on its own
    terms.

    Every arrangement this module has a case for is present in one table, and the
    comparison is made for all of them at two cut-offs, so the equivalence is a
    property rather than an example. The absent package is asserted the other way
    round: it is in the table, `snapshot_as_of` says its newest row is `not_found`,
    and the queue has left it out (`CPM-OPERATE-S11`) -- so the differential claim
    covers membership as well as breadth.
    """
    tied = _a_package("tied-at-one-instant")
    _observed(tied, components=A_LARGE_COUNT, at=BEFORE_THE_CUTOFF)
    _observed(tied, components=A_SMALL_COUNT, at=BEFORE_THE_CUTOFF)
    straddles = _a_package("straddles")
    _observed(straddles, components=A_MIDDLING_COUNT, at=BEFORE_THE_CUTOFF)
    _observed(straddles, components=A_LARGE_COUNT, at=AFTER_THE_CUTOFF)
    departed = _a_package("departed")
    _observed(departed, components=A_LARGE_COUNT, at=BEFORE_THE_CUTOFF)
    _observed_nothing(departed, state=OutcomeState.NOT_FOUND.value, at=CUTOFF)
    _observed_nothing(_a_package("errored"), state=OutcomeState.ERROR.value, at=CUTOFF)
    _observed(_a_package("only-later"), components=A_LARGE_COUNT, at=AFTER_THE_CUTOFF)
    _a_package("never-observed")

    for cutoff in (CUTOFF, LATER_CUTOFF):
        queue = unresolved_packages(cutoff=cutoff)
        for entry in queue:
            row = snapshot_as_of(package_id=entry.package.pk, cutoff=cutoff)
            expected = (None, None) if row is None else (row.internal_component_count, row.internal_lob_count)
            assert (entry.internal_component_count, entry.internal_lob_count) == expected, (
                f"the set-based read and snapshot_as_of disagree about {entry.package.canonical_name} "
                f"at {cutoff.isoformat()}"
            )
        offered = {entry.package.pk for entry in queue}
        for package in Package.objects.all():
            row = snapshot_as_of(package_id=package.pk, cutoff=cutoff)
            absent = row is not None and row.state == OutcomeState.NOT_FOUND.value
            assert (package.pk not in offered) == absent, (
                f"the queue and snapshot_as_of disagree about whether {package.canonical_name} is absent "
                f"at {cutoff.isoformat()}"
            )


# ---------------------------------------------------------------------------
# AC #4: the ways to have no breadth, and none of them is zero.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_a_package_with_no_snapshot_at_all_is_returned_and_ranked_last() -> None:
    """Reachable rather than theoretical: `CPM-IDENTITY-S05`'s override path creates one.

    AC 1 says every package needing review is returned, so a package the inventory
    never saw is ranked last rather than filtered away -- it is precisely the
    package nobody has any information about, which is not a reason to hide it.
    """
    _observed(_a_package("observed"), components=A_SMALL_COUNT)
    _a_package("never-observed")

    assert _names() == ["observed", "never-observed"]


@pytest.mark.django_db
def test_a_package_whose_latest_snapshot_is_an_absence_is_left_out_and_counted() -> None:
    """`CPM-OPERATE-S11`: an absent package leaves the queue rather than sinking to the bottom.

    The package *was* observed with real usage once, and then the source stopped
    listing it. Before this story the queue ranked it last for want of breadth;
    now it is not offered at all -- a person asked to establish the identity of a
    package the organisation no longer runs is a person asked to do nothing --
    and the selection says how many it left out, because a queue that silently
    omitted packages would read as "nothing to do".
    """
    departed = _a_package("stopped-being-listed")
    _observed(departed, components=A_LARGE_COUNT, at=BEFORE_THE_CUTOFF)
    _observed_nothing(departed, state=OutcomeState.NOT_FOUND.value, at=CUTOFF)
    _observed(_a_package("still-listed"), components=A_SMALL_COUNT)

    selection = select_unresolved(cutoff=CUTOFF)

    assert [entry.package.canonical_name for entry in selection.packages] == ["still-listed"]
    assert selection.left_out_for_absence == 1
    assert _names() == ["still-listed"]


@pytest.mark.django_db
def test_a_package_listed_again_after_an_absence_is_offered_again() -> None:
    """The reverse, with no manual step: a later `ok` row is the whole of re-listing.

    The newest row at the cut-off decides, so a package the inventory dropped and
    then listed again is back in the queue at the breadth its new listing recorded
    -- and it is not counted as left out, because it was not.
    """
    returned = _a_package("listed-again")
    _observed(returned, components=A_LARGE_COUNT, at=BEFORE_THE_CUTOFF - OBSERVATION_GAP)
    _observed_nothing(returned, state=OutcomeState.NOT_FOUND.value, at=BEFORE_THE_CUTOFF)
    _observed(returned, components=A_SMALL_COUNT, at=CUTOFF)
    _observed(_a_package("always-listed"), components=A_MIDDLING_COUNT)

    selection = select_unresolved(cutoff=CUTOFF)

    assert [entry.package.canonical_name for entry in selection.packages] == ["always-listed", "listed-again"]
    assert selection.packages[1].internal_component_count == A_SMALL_COUNT
    assert selection.left_out_for_absence == 0


@pytest.mark.django_db
def test_a_package_whose_latest_snapshot_is_an_error_is_returned_and_ranked_last() -> None:
    """The sentinel path's row, which observes nothing and must not be read as zero.

    An `error` is "we looked and the look failed", which is a different fact from
    "nobody uses it" and would be a fabricated ranking if the two were conflated
    (`CPM-NFR-3`).
    """
    failed = _a_package("look-failed")
    _observed_nothing(failed, state=OutcomeState.ERROR.value)
    _observed(_a_package("look-succeeded"), components=A_SMALL_COUNT)

    assert _names() == ["look-succeeded", "look-failed"]


@pytest.mark.django_db
@pytest.mark.parametrize("state", [OutcomeState.UNKNOWN.value, OutcomeState.NOT_APPLICABLE.value])
def test_the_remaining_sentinels_are_absences_of_breadth_too(state: str) -> None:
    """`OutcomeState` has five members and the constraint treats four of them alike.

    `unknown` and `not_applicable` are as writable on this table as `not_found` and
    `error` -- the check constraint says the counts are present exactly when the
    state is `ok`, so all four carry NULL -- and the queue must rank all four the
    same way. Parametrized rather than written twice, because the claim is about
    the *class* of non-`ok` states rather than about these two.
    """
    sentinel = _a_package("said-nothing-usable")
    _observed_nothing(sentinel, state=state)
    _observed(_a_package("said-something"), components=A_SMALL_COUNT)

    assert _names() == ["said-something", "said-nothing-usable"]


@pytest.mark.django_db
def test_a_package_observed_only_after_the_cut_off_is_returned_as_though_it_had_none() -> None:
    """Evidence the run being replayed could not have seen does not rank anything.

    This is the cut-off case at its boundary: the package has breadth, and at this
    cut-off the queue must not know it. Returned rather than dropped, because at
    this cut-off it is exactly a package with no observation.
    """
    _observed(_a_package("observed-later"), components=A_LARGE_COUNT, at=AFTER_THE_CUTOFF)
    _observed(_a_package("observed-in-time"), components=A_SMALL_COUNT, at=CUTOFF)

    assert _names(cutoff=CUTOFF) == ["observed-in-time", "observed-later"]


@pytest.mark.django_db
def test_a_genuine_zero_outranks_every_package_with_no_breadth() -> None:
    """The distinction PRD Appendix A.1 exists to protect, at the one place it ranks.

    A package the inventory observed and found nobody using is a fact worth
    ranking; a package with no usable observation is the absence of one. On a
    backend-decided ordering these two would swap places between SQLite and
    PostgreSQL, which is the divergence this case pins shut.
    """
    _observed(_a_package("observed-unused"), components=0, lobs=0)
    _a_package("never-observed")

    assert _names() == ["observed-unused", "never-observed"]


@pytest.mark.django_db
def test_the_no_breadth_states_are_ordered_among_themselves_by_the_key() -> None:
    """AC 4's "in an order that is the same on SQLite and on PostgreSQL".

    Every no-breadth state carries NULL for both counts, so nothing but the
    surrogate key separates them -- and a NULL ordering left to the backend would
    put this whole block at the *front* of the queue on one of them and at the back
    on the other. The absent package is created between the two so the key order
    would have placed it in the middle; it is not there, because
    `CPM-OPERATE-S11` leaves it out rather than ranking it.
    """
    unobserved = _a_package("no-snapshot")
    absent = _a_package("absent")
    _observed_nothing(absent, state=OutcomeState.NOT_FOUND.value)
    failed = _a_package("errored")
    _observed_nothing(failed, state=OutcomeState.ERROR.value)
    _observed(_a_package("has-breadth"), components=A_SMALL_COUNT)

    assert _names() == ["has-breadth", "no-snapshot", "errored"]
    assert unobserved.pk < absent.pk < failed.pk


# ---------------------------------------------------------------------------
# AC #5: the mapping outcomes travel, and cost no query per package.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_the_mapping_outcomes_travel_with_the_package() -> None:
    """What `CPM-FR-4`'s "candidate mappings, where any exist" is satisfiable by today.

    Nothing in this tree models a *proposed* mapping: `PackageMapping` records what
    a resolution concluded. So what the queue exposes is the conclusions that
    exist, which is what a reviewer needs in order to see what has already been
    looked for.

    **The ordering is asserted twice, and the second assertion is the one that
    works.** The first row is rewritten before the queue is read, because
    `PackageMapping` is mutable by construction -- `identity/models.py` says a row
    is rewritten in place when a later resolution concludes differently -- and an
    UPDATE on PostgreSQL writes a new tuple rather than editing one, so insertion
    order and physical order can diverge. That was *measured* rather than assumed,
    and it does not diverge here: removing the `order_by("pk")` from the prefetch
    leaves this sequence assertion green on SQLite **and** on
    `pixi run gate-postgres`, because the prefetch reads through the foreign key's
    own index and gets insertion order back either way.

    So the sequence assertion alone cannot observe a deleted `order_by`, and the
    second assertion is what does: the emitted SQL for `package_mappings` is
    required to carry an `ORDER BY`. That is coupled to the shape of a query rather
    than to a result, which is a cost worth naming -- but the alternative is a
    property the module argues for at length and that nothing in the suite can
    fail on.
    """
    package = _a_package("has-outcomes")
    _observed(package, components=A_SMALL_COUNT)
    first = PackageMapping.objects.create(
        package=package,
        kind=MappingKind.SOURCE_REPOSITORY.value,
        outcome=UNKNOWN,
        resolved_at=FIXED_INSTANT,
    )
    PackageMapping.objects.create(
        package=package,
        kind=MappingKind.FEEDSTOCK.value,
        outcome=UNKNOWN,
        resolved_at=FIXED_INSTANT,
    )
    first.outcome = ESTABLISHED
    first.save(update_fields=["outcome"])

    with CaptureQueriesContext(connection) as captured:
        (entry,) = unresolved_packages(cutoff=CUTOFF)

    assert [mapping.pk for mapping in entry.mappings] == sorted(mapping.pk for mapping in entry.mappings)
    assert [mapping.kind for mapping in entry.mappings] == [
        MappingKind.SOURCE_REPOSITORY.value,
        MappingKind.FEEDSTOCK.value,
    ]
    assert [mapping.outcome for mapping in entry.mappings] == [ESTABLISHED, UNKNOWN]
    (attachment,) = [query["sql"] for query in captured.captured_queries if MAPPING_TABLE in query["sql"].lower()]
    assert "order by" in attachment.lower(), (
        f"the mapping outcomes are attached by a query that asks for no order, so what a caller reads "
        f"is whatever the backend returned: {attachment}"
    )


@pytest.mark.django_db
def test_each_package_carries_only_its_own_mapping_outcomes() -> None:
    """A mis-scoped prefetch hands every package everybody's outcomes, and looks fine.

    Every other case here has one package with its own mappings, which a prefetch
    filtered on nothing at all would satisfy. Two packages with one outcome each,
    of different kinds, is the smallest arrangement that can tell the difference.
    """
    first = _a_package("first-owner")
    _observed(first, components=A_LARGE_COUNT)
    PackageMapping.objects.create(
        package=first,
        kind=MappingKind.SOURCE_REPOSITORY.value,
        outcome=ESTABLISHED,
        resolved_at=FIXED_INSTANT,
    )
    second = _a_package("second-owner")
    _observed(second, components=A_SMALL_COUNT)
    PackageMapping.objects.create(
        package=second,
        kind=MappingKind.FEEDSTOCK.value,
        outcome=UNKNOWN,
        resolved_at=FIXED_INSTANT,
    )

    ranked = unresolved_packages(cutoff=CUTOFF)

    assert [entry.package.canonical_name for entry in ranked] == ["first-owner", "second-owner"]
    assert [[mapping.kind for mapping in entry.mappings] for entry in ranked] == [
        [MappingKind.SOURCE_REPOSITORY.value],
        [MappingKind.FEEDSTOCK.value],
    ]


@pytest.mark.django_db
def test_a_package_nothing_has_resolved_carries_an_empty_mapping_set() -> None:
    """Empty rather than absent, so a caller iterates one shape instead of testing for two.

    A shell created by ingestion has no outcome rows at all, and it is the
    commonest thing in this queue.
    """
    _observed(_a_package("a-shell"), components=A_SMALL_COUNT)

    (entry,) = unresolved_packages(cutoff=CUTOFF)

    assert entry.mappings == ()


@pytest.mark.django_db
def test_the_query_count_does_not_grow_with_the_number_of_packages() -> None:
    """AC 5, asserted as the property rather than as a number.

    A hard-coded count measured at one cardinality is a fact about that
    arrangement, not about the shape of the read: it passes just as well if the
    read is N+1 and N happens to be one. Measuring at three cardinalities and
    asserting the counts are *equal* is the claim AC 5 actually makes, and it is
    what fails when someone adds an innocent-looking `entry.package.mappings.count()`
    inside a loop or restores a `snapshot_as_of` per package.

    The equality is also what makes this backend-agnostic: the absolute number can
    legitimately differ between SQLite and PostgreSQL, and does not have to be
    restated here when it does.
    """
    counts: list[int] = []
    created = 0
    for cardinality in QUERY_COUNT_CARDINALITIES:
        while created < cardinality:
            package = _a_package(f"package-{created}")
            _observed(package, components=A_MIDDLING_COUNT + created)
            PackageMapping.objects.create(
                package=package,
                kind=MappingKind.SOURCE_REPOSITORY.value,
                outcome=ESTABLISHED,
                resolved_at=FIXED_INSTANT,
            )
            created += 1
        with CaptureQueriesContext(connection) as captured:
            queue = unresolved_packages(cutoff=CUTOFF)
        assert len(queue) == cardinality
        assert [len(entry.mappings) for entry in queue] == [1] * cardinality
        counts.append(len(captured))

    assert len(set(counts)) == 1, (
        f"the query count grew with the package count: {dict(zip(QUERY_COUNT_CARDINALITIES, counts, strict=True))}"
    )


# ---------------------------------------------------------------------------
# What the read says it did (`CPM-AD-15`).
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_the_selection_logs_the_cut_off_it_read_as_of(captured_events: list[EventDict]) -> None:
    """The one field worth binding, because it is the first question a disagreement raises.

    When a replayed run produces a different queue from the run it replays, the
    first thing to establish is whether the two read as of the same instant. A log
    line carrying only the size cannot answer that; one carrying the cut-off can,
    and `CPM-AD-15` correlates it to the run through the active span without this
    module having to bind anything itself.
    """
    _observed(_a_package("in-the-queue"), components=A_SMALL_COUNT)
    _a_package("verified-one", confidence=IdentityConfidence.VERIFIED.value)
    _observed_nothing(_a_package("gone"), state=OutcomeState.NOT_FOUND.value)

    unresolved_packages(cutoff=CUTOFF)

    (event,) = [entry for entry in captured_events if entry["event"] == QUEUE_SELECTED_EVENT]
    assert event["cutoff"] == CUTOFF.isoformat()
    assert event["selected"] == 1
    assert event["with_breadth"] == 1
    assert event["left_out_for_absence"] == 1


# ---------------------------------------------------------------------------
# The refusal, against a real connection.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_a_naive_cut_off_is_refused_with_rows_in_the_table() -> None:
    """The refusal holds where it matters: there is something to return and it is not returned.

    The unit case proves the check runs before the database is touched. This one
    proves it is not quietly skipped when there is a real connection and a real
    queue behind it -- a shifted cut-off selects a different queue on every replay,
    and it would do it silently.
    """
    _observed(_a_package("would-have-been-returned"), components=A_LARGE_COUNT)

    with pytest.raises(InventoryReadError):
        unresolved_packages(cutoff=FIXED_INSTANT.replace(tzinfo=None))
