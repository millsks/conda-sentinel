"""Which packages still need a human to look at their identity, and in what order.

`CPM-FR-4` wants the packages whose resolution did not conclude worked as a queue
"ranked by internal usage breadth" rather than read as a report. This module is
the *selection* behind that queue and nothing else: no queue table, no workflow
model, no row recording that somebody looked. `CPM-AD-22` puts every queue in the
`workflow` application and `CPM-APP-S05` builds the surface that works this one.

**It extends `snapshot_as_of` rather than bypassing it.** `collectors/models.py`
says that function is "the only supported way" to read a usage signal, and the
rule it carries is about *when* a signal is read -- at a stated cut-off, never as
of now -- rather than about how many packages one call covers. This read keeps
the same `observed_at__lte` bound, the same "latest wins, ties by descending
primary key" resolution and the same refusal of an unusable cut-off; what it does
not do is issue that query once per package, which over `CPM-NFR-1`'s
ten-thousand-package sizing is an N+1.
`tests/integration/django_apps/test_selection.py` asserts the equivalence
differentially, by calling both.

**The cut-off is an argument.** `CPM-AD-25`: a policy reads the latest snapshot
at or before *its run's* cut-off, and `core/policy_run.py` derives one per run and
hands it down. Nothing here derives one or reads a clock (`CPM-AD-26`). That
contract has **no production caller yet** -- `CPM-APP-S05` builds the first one --
so it has never been exercised against a real `core/policy_run.py` cut-off, only
against instants the suite supplies.

**NULL ordering is decided in Python, and that is the point rather than a
detail.** SQLite and PostgreSQL disagree about where a NULL sorts, in opposite
directions on ascending and descending orders, and `NULLS FIRST`/`NULLS LAST` is
not portable. The breadth columns are nullable by construction, so an `order_by`
over them means a developer's suite and `CPM-AD-18`'s PostgreSQL gate disagree
about the shape of the queue while both report green. `_breadth_ordering_key`
below is therefore the whole ordering, and the fold to latest-per-package is in
Python for the reason `collectors/tasks.py` folds latest-state-per-package there
rather than reaching for the PostgreSQL-only `distinct(*fields)`.

**Ranking is an ordering, not a score.** PRD Open Question 3b makes
`internal_component_count` and `internal_lob_count` together *be* the breadth
`CPM-FR-4` ranks by, and how they combine is `CPM-FR-20`'s score function, which
is PRD Open Question 8 and undecided. So the order is lexicographic over the pair
and commits to no weighting.

**"Candidate mappings" has no referent yet, and that is what `CPM-FR-4`'s "where
any exist" is for.** Nothing in this tree models a *proposed* mapping:
`PackageMapping` records what a resolution concluded. Inventing a candidate table
would create the queue-adjacent state `CPM-AD-22` keeps out of this layer and
would model a concept no story has defined, so what travels is what exists.

The story `_bmad-output/implementation-artifacts/stories/cpm-identity-s04-unresolved-packages-selectable-ranked.md`
carries the rest of the argument -- why this module is in `collectors` rather than
in `identity`, and why the three no-breadth states are not zero.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING
from typing import Final

import structlog
from django.db import transaction
from django.db.models import Prefetch

from conda_sentinel.collectors.models import InventoryReadError
from conda_sentinel.collectors.models import InventorySnapshot
from conda_sentinel.core.clock import is_aware
from conda_sentinel.identity.models import IdentityConfidence
from conda_sentinel.identity.models import Package
from conda_sentinel.identity.models import PackageMapping

if TYPE_CHECKING:
    from collections.abc import Collection

__all__ = [
    "QUEUE_SELECTED_EVENT",
    "RESOLVED_CONFIDENCES",
    "UNRESOLVED_CONFIDENCES",
    "UnresolvedPackage",
    "unresolved_packages",
]

logger = structlog.get_logger(__name__)

#: The event one selection emits, once per call.
#:
#: `CPM-AD-15` correlates a log line to the run that produced it through the
#: active span, which `config/observability/logging.py` binds. What is worth
#: binding *here* is the cut-off: when a replayed run disagrees with the run it
#: replays, the first question is whether the two read as of the same instant, and
#: a queue that logged only its size cannot answer it.
QUEUE_SELECTED_EVENT: Final[str] = "identity_review_queue_selected"

#: The confidences at which nothing further is asked of a human.
#:
#: One member, and it is the one `CPM-AD-4` describes as an identity a person
#: established: `verified`. **This is the set the queue is the complement of**, and
#: the direction matters: the SQL below excludes these rather than including the
#: others, so a confidence value the enum does not declare -- one written by a data
#: migration, or one removed from `IdentityConfidence` while rows still carry it --
#: arrives in the review queue rather than disappearing from it. A package wrongly
#: in the queue is a question a human answers once; one wrongly out of it is a
#: package nobody ever looks at (`CPM-FR-2`).
RESOLVED_CONFIDENCES: Final[frozenset[str]] = frozenset({IdentityConfidence.VERIFIED.value})

#: The declared confidences that put a package in this queue.
#:
#: Derived from the enum rather than written as `{"unmapped", "inventory-derived"}`,
#: on the shape `core/confidence.py`'s `_KNOWN_CONFIDENCES` uses: a literal pair
#: would be a second spelling of values whose spelling `IdentityConfidence` has
#: already fixed once.
#:
#: **It documents the partition; it is not the filter.** The filter is the
#: exclusion above, which is a superset of this: every value here is selected, and
#: so is anything stored that the enum does not know about.
#: `tests/unit/django_apps/test_selection.py` pins both halves and their union, so
#: a fourth confidence added without a decision about which half it belongs to
#: fails there rather than changing the queue quietly.
UNRESOLVED_CONFIDENCES: Final[frozenset[str]] = frozenset(IdentityConfidence.values) - RESOLVED_CONFIDENCES

#: How many snapshot rows are held in memory at once while the fold runs.
#:
#: `InventorySnapshot` is append-only and kept for the declared retention
#: (`CPM-OPERATE-S07`, ninety days by default), so the rows at or before a
#: cut-off grow with the *history* as well as with the inventory -- see
#: `_breadth_at`, which says what that costs. Streaming in chunks is what keeps the
#: fold's memory proportional to the number of packages rather than to the number
#: of observations ever made of them.
_SNAPSHOT_CHUNK: Final[int] = 2000

#: What the fold answers for a package it found no snapshot for at the cut-off.
#:
#: The same pair `not_found` and `error` rows carry by
#: `inventory_counts_present_exactly_when_observed`, and that is deliberate: "no
#: observation" and "an observation of an absence" are different facts about the
#: source, but they are the same fact about breadth, which is that there is none.
_NO_BREADTH: Final[tuple[int | None, int | None]] = (None, None)


@dataclass(frozen=True, slots=True)
class UnresolvedPackage:
    """One package needing identity review, with what ranks it and what is known about it.

    Frozen and slotted like every other value object in this product
    (`core/collection.py`, `core/policy_run.py`), so a caller cannot rewrite a
    selection made at a cut-off. **The freezing is one field deep**: `package` is a
    live Django model instance and its own attributes are as writable as any
    other's. What is protected is the *selection* -- which package, at what
    breadth, with which outcomes -- rather than the row behind it.

    The two counts are carried beside the package rather than left to be read off
    a snapshot, because the snapshot they came from is the latest one *at the
    cut-off*, and re-deriving that from the package would need the cut-off again --
    which is how a surface ends up reading a signal as of now.
    """

    #: The package itself, at a confidence outside `RESOLVED_CONFIDENCES`.
    package: Package

    #: How many internal components used it, as its latest snapshot at the cut-off
    #: recorded. `None` when it has no snapshot by then, or when its latest is an
    #: absence or an error: missing, never zero (`CPM-FR-42`).
    internal_component_count: int | None

    #: How many internal lines of business used it, on the same terms.
    internal_lob_count: int | None

    #: What resolution has concluded about this package's mappings, ordered by
    #: primary key. Empty rather than absent for a package nothing has resolved,
    #: so a caller iterates one shape instead of testing for two.
    mappings: tuple[PackageMapping, ...]


def _breadth_ordering_key(
    *,
    internal_component_count: int | None,
    internal_lob_count: int | None,
    package_id: int,
) -> tuple[bool, int, bool, int, int]:
    """Return the sort key that puts one package in its place in the queue.

    **This function is why the ordering is not in the database**, and it is private
    for a related reason: a surface handed a ranked queue must not be able to
    re-sort it, which is the same thing `UnresolvedPackage`'s frozen-ness says.
    Exporting the key would be an invitation to do exactly that.

    The key is ascending on every element, which composes as: packages with a
    component count first, highest count first; then the same for lines of
    business; then the surrogate key ascending. Booleans lead each pair because
    `False < True`, so "has a count" sorts ahead of "has none".

    Args:
        internal_component_count: How many internal components used the package at
            the cut-off, or `None` when that is not known.
        internal_lob_count: How many lines of business did, or `None`.
        package_id: The surrogate key, which makes the order total: two packages
            with identical breadth are still ordered, and ordered the same way on
            both backends and on every call.

    Returns:
        A tuple that sorts ascending into `CPM-FR-4`'s ranking. A package with a
        genuine `0` outranks one whose count is missing, because blank is missing
        and is never conflated with zero (PRD Appendix A.1).

    """
    return (
        internal_component_count is None,
        -internal_component_count if internal_component_count is not None else 0,
        internal_lob_count is None,
        -internal_lob_count if internal_lob_count is not None else 0,
        package_id,
    )


def _require_usable_cutoff(cutoff: datetime) -> None:
    """Refuse a cut-off this read cannot be answered in terms of.

    Two refusals rather than one, because they are two different mistakes and a
    caller fixes them differently. Both are `InventoryReadError`, which is the
    documented failure of every inventory read -- without the first, a `None` or a
    `date` reaches `is_aware` and comes back as an `AttributeError` naming
    `utcoffset`, which tells the caller nothing about which argument was wrong.

    Args:
        cutoff: The value the caller supplied.

    Raises:
        InventoryReadError: When it is not a `datetime` at all, or when it is a
            naive one. A naive instant is refused rather than converted on the
            terms `snapshot_as_of` refuses one: there is no offset to convert
            from, `USE_TZ` is on so Django would read it as if it were UTC, and a
            cut-off silently shifted by the reader's offset selects a different
            queue on every replay -- the opposite of what `CPM-FR-22` promises.

    """
    # `datetime` first: a `date` is not a `datetime`, but a `datetime` *is* a
    # `date`, so the narrower test has to be the one that runs.
    if not isinstance(cutoff, datetime):
        message = (
            f"the unresolved package queue is selected as of an instant, and {cutoff!r} is not one. The "
            f"cut-off comes from the run being served (CPM-AD-25) and is an aware datetime; a date has no "
            f"time of day to bound the evidence at and None is not a cut-off at all."
        )
        raise InventoryReadError(message)
    if not is_aware(cutoff):
        message = (
            f"the unresolved package queue cannot be selected as of the naive cutoff {cutoff!r}. Every "
            f"instant comes from a Clock, which always answers in UTC (CPM-AD-26); a naive value has no "
            f"offset to interpret, so the selection would be silently shifted by whichever offset the "
            f"reader happened to be in and the replay CPM-FR-22 promises would return a different queue "
            f"each time."
        )
        raise InventoryReadError(message)


def _breadth_at(*, cutoff: datetime, package_ids: Collection[int]) -> dict[int, tuple[int | None, int | None]]:
    """Return the breadth each named package's latest snapshot at the cut-off recorded.

    One query, streamed, folded to latest-per-package in Python. `distinct(*fields)`
    would do the fold in the database in one row per package and is PostgreSQL-only,
    which is the reason `collectors/tasks.py` folds its own latest-state-per-package
    here rather than there; a window function or a `FILTER` clause would be the same
    bet with a different name.

    **Which row wins is compared in Python, not inferred from the order the rows
    arrived in.** The surviving entry for a package is the one with the greatest
    `(observed_at, pk)`, decided by the `>` below. That tie is the normal case
    rather than an exotic one -- one sweep stamps every row it writes with the run's
    single instant (`CPM-AD-7`), so two observations of one package really do share
    an `observed_at` -- and it is the same row `snapshot_as_of` returns for the same
    `(package, cutoff)` pair, which is what makes this a set-based spelling of that
    read rather than a second answer to its question.

    A "last row wins over an ordered stream" fold would give the same answer while
    resting on the database's tie-breaking rather than on this module's, which is
    the arrangement this story exists to avoid: SQLite returns tied rows in rowid
    order, so such a fold produces the right answer there whether or not it asked
    for one, and a dropped `"pk"` would be invisible until PostgreSQL sorted the tie
    the other way. The `order_by` is still asked for, because a totally ordered
    stream is what makes the comparison's own failure modes reproducible on both
    backends -- but it is an aid, not the rule.

    **Membership comes from `package_ids` and from nothing else.** An earlier
    spelling narrowed this query with a join on `Package.confidence`, which made the
    queue depend on two reads agreeing: a package whose confidence changed between
    them would be selected and then found to have no snapshots, and would be ranked
    as though it had no breadth. `core/rollup.py` names that defect in as many words
    -- "two reads inside one run is the defect that shape exists to prevent" -- so
    the set of packages is decided once, by the caller, and this query only answers
    what was observed.

    **What this costs, stated rather than implied.** It reads every snapshot at or
    before the cut-off, filtering to the named packages in Python. `InventorySnapshot`
    is append-only and kept for the declared retention (`CPM-OPERATE-S07`), so that
    is one row per package per sweep for the whole retained history -- ninety days
    of daily sweeps over ten thousand packages is close to a million rows, and the
    number grows with the retention even when the inventory does not. It is a
    fixed number of *queries*, which is not the same claim as a fixed amount of
    work. `.iterator()` bounds the memory to
    `_SNAPSHOT_CHUNK` rows plus one entry per named package. The rows themselves
    are bounded by the pruning policy `CPM-OPERATE-S07` declared -- the nightly
    purge keeps the retention's worth per `(package, source_package_key)` and
    the newest row per key beyond it -- so what is left to bound the *read* is a
    materialised latest-per-package projection, which no story has claimed.

    Args:
        cutoff: The instant to read as of, aware. Checked by the caller.
        package_ids: The packages to answer for. Rows for anything else are
            discarded, so this read cannot disagree with the read that chose them.

    Returns:
        Breadth by package primary key, holding an entry only for packages with at
        least one snapshot at or before the cut-off. A package with none is absent
        rather than present with `None`, and the caller supplies `_NO_BREADTH` for
        it: "no row" and "a row observing nothing" are different facts and only one
        of them is in this mapping.

    """
    # A set rather than the caller's sequence: the membership test runs once per
    # snapshot row, which is the one thing here that grows without bound.
    wanted = frozenset(package_ids)
    latest: dict[int, tuple[int | None, int | None]] = {}
    # How new the surviving row for each package is, as the `(observed_at, pk)` pair
    # the comparison is over. Kept beside the answer rather than folded into it
    # because the caller wants the counts and nothing else.
    newest: dict[int, tuple[datetime, int]] = {}
    rows = (
        InventorySnapshot.objects.filter(observed_at__lte=cutoff)
        .order_by("observed_at", "pk")
        .values_list("package_id", "observed_at", "pk", "internal_component_count", "internal_lob_count")
        .iterator(chunk_size=_SNAPSHOT_CHUNK)
    )
    for package_id, observed_at, snapshot_id, component_count, lob_count in rows:
        if package_id not in wanted:
            continue
        stamp = (observed_at, snapshot_id)
        # Written as "take it when it is strictly newer" rather than "skip it when
        # it is not", and the two are not interchangeable. `pk` is unique, so two
        # stamps for one package are never equal and both spellings behave
        # identically while the code is right. They differ when it is *wrong*: a
        # stamp narrowed to `observed_at` alone makes ties compare equal, and this
        # spelling then keeps the first row of the ascending stream -- the lowest
        # primary key, on every backend, which is the wrong answer and one a test
        # can fail on. The inverted spelling would keep the last, which is the
        # right answer reached by a route nothing checks.
        if package_id not in newest or newest[package_id] < stamp:
            newest[package_id] = stamp
            latest[package_id] = (component_count, lob_count)
    return latest


def unresolved_packages(*, cutoff: datetime) -> list[UnresolvedPackage]:
    """Return every package needing identity review at a cut-off, most used first.

    `CPM-FR-4`'s queue as a read. See the module docstring for why the ranking is
    an ordering rather than a score, and why the NULL handling is Python's rather
    than the database's.

    **Both reads happen inside one `atomic` block**, and what that buys is worth
    being exact about. On SQLite it is a genuine consistent view: the transaction's
    read snapshot covers both statements. On PostgreSQL's default `READ COMMITTED`
    each statement still takes its own snapshot, so the block alone would not make
    the pair consistent -- what does is that the *set* of packages is decided by
    the first read and `_breadth_at` is told which packages to answer for, so a
    concurrent commit can change what a package's breadth is but cannot make a
    selected package silently lose it. The block is what makes a deployment at
    `REPEATABLE READ` consistent too, without anything here having to know which
    it got.

    Args:
        cutoff: The instant to read the inventory as of, aware. Supplied by the
            caller from its run (`CPM-AD-25`), never derived here and never a clock
            reading: the queue at a stated cut-off is a function of the cut-off,
            which is what lets a replayed run see what the run it replays saw.

    Returns:
        Every package at a confidence outside `RESOLVED_CONFIDENCES`, ordered by
        `_breadth_ordering_key` -- descending internal component count, then
        descending internal line-of-business count, then ascending primary key --
        each carrying the breadth its latest snapshot at the cut-off recorded and
        the mapping outcomes it already has. Empty when nothing needs review, which
        is an ordinary answer rather than an error.

        Packages with no breadth are last rather than absent, and there are three
        ways to be one of them: no snapshot at all, a latest that is `not_found`,
        and a latest that is `error`.

    Raises:
        InventoryReadError: When `cutoff` is not an aware `datetime`. See
            `_require_usable_cutoff`.

    """
    _require_usable_cutoff(cutoff)

    with transaction.atomic():
        # The complement in SQL rather than the derived set, so a stored confidence
        # the enum does not declare is reviewed rather than dropped -- see
        # `RESOLVED_CONFIDENCES`. No `order_by`: the ranking is decided below, and
        # a database ordering here would suggest otherwise.
        #
        # The mapping outcomes come back with the packages rather than one query per
        # package, and they come back ordered. Neither backend has been observed
        # returning them out of primary-key order -- the prefetch reads through the
        # foreign key's own index -- so the `order_by` is not fixing a divergence
        # anyone has measured. It is here because "whatever the index happened to
        # give us" is not an ordering anything promises, and `PackageMapping` is
        # rewritten in place when a later resolution concludes differently, so the
        # physical order is not even stable over the row's life. An unordered
        # attachment is the same "two identical calls, two different answers"
        # defect as an unordered queue, one field down.
        # `tests/integration/django_apps/test_selection.py` asserts the emitted SQL
        # asks for an order, because a result-shaped assertion cannot see this one.
        selected = list(
            Package.objects.exclude(confidence__in=RESOLVED_CONFIDENCES).prefetch_related(
                Prefetch("mappings", queryset=PackageMapping.objects.order_by("pk")),
            ),
        )
        breadth = _breadth_at(cutoff=cutoff, package_ids=[package.pk for package in selected])

    queue = [
        UnresolvedPackage(
            package=package,
            internal_component_count=breadth.get(package.pk, _NO_BREADTH)[0],
            internal_lob_count=breadth.get(package.pk, _NO_BREADTH)[1],
            mappings=tuple(package.mappings.all()),
        )
        for package in selected
    ]
    queue.sort(
        key=lambda entry: _breadth_ordering_key(
            internal_component_count=entry.internal_component_count,
            internal_lob_count=entry.internal_lob_count,
            package_id=entry.package.pk,
        ),
    )
    logger.info(
        QUEUE_SELECTED_EVENT,
        cutoff=cutoff.isoformat(),
        selected=len(queue),
        with_breadth=len(breadth),
    )
    return queue
