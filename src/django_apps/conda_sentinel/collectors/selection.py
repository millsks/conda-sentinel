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
hands it down. Nothing here derives one or reads a clock (`CPM-AD-26`). The first
production caller is `workflow/opening.py` (`CPM-OPERATE-S11`), which selects at
`run.evidence_cutoff` when it opens identity review items -- so the opening is
cut-off bound and a replayed run opens for the same packages.

**An absent package is not offered** (`CPM-OPERATE-S11`). A package whose newest
inventory snapshot at the cut-off is `not_found` is one the organisation no
longer runs, and a queue that offered it would be asking a person to establish
the identity of something nobody uses. `collectors/absence.py` is the reader;
this module leaves such packages out and reports how many, rather than ranking
them last for want of breadth as it did before. A package that departs *after*
the cut-off keeps its place, because at this cut-off it has not departed.

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
from typing import TYPE_CHECKING
from typing import Final

import structlog
from django.db import transaction
from django.db.models import Prefetch

from conda_sentinel.collectors.absence import histories_at
from conda_sentinel.collectors.absence import require_usable_cutoff
from conda_sentinel.identity.models import IdentityConfidence
from conda_sentinel.identity.models import Package
from conda_sentinel.identity.models import PackageMapping

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import datetime

    from conda_sentinel.collectors.absence import PackageHistory

__all__ = [
    "QUEUE_SELECTED_EVENT",
    "RESOLVED_CONFIDENCES",
    "UNRESOLVED_CONFIDENCES",
    "IdentityReviewSelection",
    "UnresolvedPackage",
    "select_unresolved",
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


@dataclass(frozen=True, slots=True)
class IdentityReviewSelection:
    """The identity review queue at one cut-off, with what it left out.

    The count travels with the queue rather than being logged alone, because the
    queue page states it (`CPM-FR-38`: an absent package is labelled, never
    silently dropped) and a surface reading the log to find it would be reading
    the wrong thing.
    """

    #: The queue, ranked by `_breadth_ordering_key`.
    packages: tuple[UnresolvedPackage, ...]

    #: How many unresolved packages were absent from the inventory at the cut-off
    #: and therefore not offered.
    left_out_for_absence: int


def select_unresolved(
    *,
    cutoff: datetime,
    histories: Mapping[int, PackageHistory] | None = None,
) -> IdentityReviewSelection:
    """Select every package needing identity review at a cut-off, most used first.

    `CPM-FR-4`'s queue as a read. See the module docstring for why the ranking is
    an ordering rather than a score, and why the NULL handling is Python's rather
    than the database's.

    **One fold per run.** A caller that has already folded the inventory at this
    cut-off -- `workflow/opening.py` reads the histories once for every package
    the run touches and hands them to the selection and to the closer -- passes
    them in, and nothing is read twice. Without them the selection folds for the
    packages it selects, `collectors/absence.py`'s `histories_at` narrowed to
    those ids.

    **Both reads happen inside one `atomic` block**, and what that buys is worth
    being exact about. On SQLite it is a genuine consistent view: the transaction's
    read snapshot covers both statements. On PostgreSQL's default `READ COMMITTED`
    each statement still takes its own snapshot, so the block alone would not make
    the pair consistent -- what does is that the *set* of packages is decided by
    the first read and the fold is told which packages to answer for, so a
    concurrent commit can change what a package's breadth is but cannot make a
    selected package silently lose it. The block is what makes a deployment at
    `REPEATABLE READ` consistent too, without anything here having to know which
    it got.

    Args:
        cutoff: The instant to read the inventory as of, aware. Supplied by the
            caller from its run (`CPM-AD-25`), never derived here and never a clock
            reading: the queue at a stated cut-off is a function of the cut-off,
            which is what lets a replayed run see what the run it replays saw.
        histories: The inventory histories at that cut-off, already folded by the
            caller for at least the packages this selects; `None` folds here. A
            package missing from the mapping is read as never observed.

    Returns:
        The queue and the count of what it left out.

        **Offered:** every package at a confidence outside `RESOLVED_CONFIDENCES`
        that the inventory has not recorded absent at the cut-off, ordered by
        `_breadth_ordering_key` -- descending internal component count, then
        descending internal line-of-business count, then ascending primary key --
        each carrying the breadth its latest snapshot at the cut-off recorded and
        the mapping outcomes it already has. Packages with no breadth are offered
        last rather than dropped: no snapshot at all, a latest that is `error`,
        or a latest that is another non-`ok` sentinel.

        **Not offered:** a package the inventory recorded absent at the cut-off --
        a `not_found` with no `ok` after it (`CPM-OPERATE-S11`). It is counted in
        `left_out_for_absence` instead. An empty queue is an ordinary answer
        rather than an error.

    Raises:
        InventoryReadError: When `cutoff` is not an aware `datetime`. See
            `collectors/absence.py`'s `require_usable_cutoff`.

    """
    require_usable_cutoff(cutoff, subject="the unresolved package queue")

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
        if histories is None:
            histories = histories_at(cutoff=cutoff, package_ids=[package.pk for package in selected])

    # One fold, two readings: absence decides membership and breadth decides rank.
    absent = {package_id for package_id, history in histories.items() if history.absence().absent}
    breadth = {package_id: history.breadth() for package_id, history in histories.items()}
    queue = [
        UnresolvedPackage(
            package=package,
            internal_component_count=breadth.get(package.pk, _NO_BREADTH)[0],
            internal_lob_count=breadth.get(package.pk, _NO_BREADTH)[1],
            mappings=tuple(package.mappings.all()),
        )
        for package in selected
        if package.pk not in absent
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
        with_breadth=len(breadth) - len(absent),
        left_out_for_absence=len(absent),
    )
    return IdentityReviewSelection(packages=tuple(queue), left_out_for_absence=len(absent))


def unresolved_packages(*, cutoff: datetime) -> list[UnresolvedPackage]:
    """Return the ranked queue alone, without the count of what was left out.

    `select_unresolved(...).packages` as a list, kept for the callers that want
    the queue and nothing else. New callers that render the queue want the
    selection, because the page states how many packages were not offered.

    Args:
        cutoff: The instant to select as of, aware.

    Returns:
        The ranked queue, on `select_unresolved`'s terms.

    Raises:
        InventoryReadError: When `cutoff` is not an aware `datetime`.

    """
    return list(select_unresolved(cutoff=cutoff).packages)
