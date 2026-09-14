"""Which packages the inventory no longer lists, read as of a stated cut-off.

`CPM-OPERATE-S03` made absence an *observation*: when the inventory stops listing
a package, the ingestion collector writes an `InventorySnapshot` row whose `state`
is `not_found`, carrying that run's timestamp, and nothing is deleted. Until
`CPM-OPERATE-S11` nothing downstream read that row -- the identity review
selection ranked an absent package last instead of leaving it out, workflow
opening still opened items for it, and no surface said it was gone. This module
is the one reader of inventory absence, and it feeds four consumers: the
selection (`collectors/selection.py`), the after-run step below that stamps the
rollup row, workflow opening (`workflow/opening.py`) and, through the rollup's
two columns, every surface.

**Cut-off bound, like every read of evidence** (`CPM-AD-25`). Absence is answered
as of the instant a policy run read evidence at, never as of now, so a replayed
run (`CPM-FR-22`) selects, stamps, skips and closes the same packages the run it
replays did. A `not_found` row written after the cut-off is invisible to this
read, exactly as a later `ok` row is invisible to `snapshot_as_of`.

**Never `InventoryEntry.retired_at`.** The inventory table is governed reference
data, mutable and not cut-off bound: a row retired this morning reads as retired
at every cut-off, which would make a replay disagree with the run it replays. The
snapshot log is the only source that can answer "was it absent *then*".

**One iterator query, folded in Python, shared with the selection.** The fold is
the one `collectors/selection.py` used for breadth -- every snapshot at or before
the cut-off, streamed ascending by `(observed_at, pk)`, the newest row per package
decided by comparison -- widened to carry `state` and to remember the instants
absence needs. The selection reads its breadth through the same fold, so the two
can never disagree about which row is a package's newest, and a policy run folds
**once**: `workflow/opening.py` reads the histories for every package the run
touches and hands them to the selection and to the closer.

**What "absent" means here.** A package is absent when the inventory has recorded
a `not_found` and has not listed it (`ok`) since -- whatever rows came in between.
`since` is the *first* `not_found` of that absence, so a source that repeats its
absence row night after night does not move the date; and an `error`, `unknown`
or `not_applicable` row after a `not_found` is a look that said nothing, not a
re-listing, so the package stays absent until an `ok` row says otherwise.

**On the `AD-` prefix.** A bare `AD-n` in this repository is an *inherited*
platform decision; a decision from this product's own architecture spine always
carries the `CPM-` prefix.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING
from typing import Final

import structlog

from conda_sentinel.collectors.models import InventoryReadError
from conda_sentinel.collectors.models import InventorySnapshot
from conda_sentinel.core.clock import is_aware
from conda_sentinel.core.models import PackageHealth
from conda_sentinel.core.outcomes import OutcomeState

if TYPE_CHECKING:
    from collections.abc import Collection
    from collections.abc import Iterable

    from conda_sentinel.core.clock import Clock
    from conda_sentinel.core.models import PolicyRun

__all__ = [
    "ABSENCE_MARKED_EVENT",
    "ABSENCE_STEP_NAME",
    "ID_CHUNK",
    "Absence",
    "PackageHistory",
    "absence_at",
    "fold_observations",
    "histories_at",
    "mark_inventory_absence",
    "require_usable_cutoff",
]

logger: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

#: The name the after-run step is registered under, and the one a failed run
#: reports. Registered by `collectors/apps.py` *before* `workflow`'s opening step:
#: `INSTALLED_APPS` lists `collectors` first, so the rollup row carries absence by
#: the time the queues read it.
ABSENCE_STEP_NAME: Final[str] = "collectors.mark_inventory_absence"

#: What the step logs once per run: how many rollup rows it stamped absent.
ABSENCE_MARKED_EVENT: Final[str] = "inventory_absence_marked"

#: How many snapshot rows are held in memory at once while the fold runs.
#:
#: `InventorySnapshot` is append-only and kept for the declared retention
#: (`CPM-OPERATE-S07`, ninety days by default), so the rows at or before a
#: cut-off grow with the *history* as well as with the inventory. Streaming in
#: chunks keeps the fold's memory proportional to the number of packages rather
#: than to the number of observations ever made of them.
SNAPSHOT_CHUNK: Final[int] = 2000

#: How many package ids one `IN (...)` carries when the read is narrowed to named
#: packages. Under SQLite's 999-parameter ceiling with room for the cut-off, and
#: small enough that PostgreSQL plans each chunk as an index probe.
ID_CHUNK: Final[int] = 500


@dataclass(frozen=True, slots=True)
class Absence:
    """What the inventory said about one package's presence, as of a cut-off.

    Frozen and slotted like every other value object in this product, so a
    consumer cannot rewrite what was read at a cut-off.
    """

    #: Whether the inventory recorded a `not_found` at or before the cut-off with
    #: no `ok` after it. The only meaning of "absent" here: an `error` or an
    #: `unknown` row is a look that failed, and neither starts nor ends an absence.
    absent: bool

    #: When the inventory was first seen not listing it -- the `observed_at` of
    #: the first `not_found` row of the current absence. `None` unless `absent`.
    since: datetime | None

    #: The newest `ok` observation at or before the cut-off, whether or not the
    #: package is absent now. `None` for a package the inventory never listed.
    last_listed: datetime | None

    #: The first `ok` observation after the most recent `not_found`, at or before
    #: the cut-off: the start of the package's current listing epoch. `None` for a
    #: package that was never absent, and `None` again while it is absent.
    listed_since: datetime | None


@dataclass(slots=True)
class PackageHistory:
    """The fold's running answer for one package, updated row by row.

    Mutable on purpose: it is the accumulator the stream writes into, never a
    value handed to a caller. `absence()` and `breadth()` are the two readings the
    two consumers take of it.
    """

    #: The newest row's `(observed_at, pk)`, the pair the comparison is over, and
    #: the breadth that row carried.
    newest: tuple[datetime, int]
    internal_component_count: int | None
    internal_lob_count: int | None

    #: The newest `ok` row's instant, and the first `ok` after the most recent
    #: `not_found`.
    last_listed: datetime | None = None
    listed_since: datetime | None = None

    #: The first `not_found` with no `ok` after it yet -- set while the package is
    #: absent, cleared by the `ok` that starts a new listing epoch.
    absent_since: datetime | None = None

    def absence(self) -> Absence:
        """Return the absence reading of this history.

        Returns:
            The `Absence` value this package's rows fold to.

        """
        return Absence(
            absent=self.absent_since is not None,
            since=self.absent_since,
            last_listed=self.last_listed,
            listed_since=self.listed_since,
        )

    def breadth(self) -> tuple[int | None, int | None]:
        """Return the breadth reading of this history.

        Returns:
            The newest row's two counts -- `None` on an absence, an error or any
            other non-`ok` row, by the table's own constraint.

        """
        return (self.internal_component_count, self.internal_lob_count)


def require_usable_cutoff(cutoff: datetime, *, subject: str) -> None:
    """Refuse a cut-off a cut-off-bound read cannot be answered in terms of.

    Two refusals rather than one, because they are two different mistakes and a
    caller fixes them differently. Both are `InventoryReadError`, which is the
    documented failure of every inventory read -- without the first, a `None` or
    a `date` reaches `is_aware` and comes back as an `AttributeError` naming
    `utcoffset`, which tells the caller nothing about which argument was wrong.

    Args:
        cutoff: The value the caller supplied.
        subject: What is being read, for the message -- "the unresolved package
            queue", "inventory absence".

    Raises:
        InventoryReadError: When it is not a `datetime` at all, or when it is a
            naive one. A naive instant is refused rather than converted on the
            terms `snapshot_as_of` refuses one: there is no offset to convert
            from, `USE_TZ` is on so Django would read it as if it were UTC, and a
            cut-off silently shifted by the reader's offset selects a different
            answer on every replay -- the opposite of what `CPM-FR-22` promises.

    """
    # `datetime` first: a `date` is not a `datetime`, but a `datetime` *is* a
    # `date`, so the narrower test has to be the one that runs.
    if not isinstance(cutoff, datetime):
        message = (
            f"{subject} is read as of an instant, and {cutoff!r} is not one. The cut-off comes from the run "
            f"being served (CPM-AD-25) and is an aware datetime; a date has no time of day to bound the "
            f"evidence at and None is not a cut-off at all."
        )
        raise InventoryReadError(message)
    if not is_aware(cutoff):
        message = (
            f"{subject} cannot be read as of the naive cutoff {cutoff!r}. Every instant comes from a Clock, "
            f"which always answers in UTC (CPM-AD-26); a naive value has no offset to interpret, so the read "
            f"would be silently shifted by whichever offset the reader happened to be in and the replay "
            f"CPM-FR-22 promises would return a different answer each time."
        )
        raise InventoryReadError(message)


def histories_at(*, cutoff: datetime, package_ids: Collection[int] | None = None) -> dict[int, PackageHistory]:
    """Fold every snapshot at or before the cut-off to one history per package.

    One query, streamed, folded in Python. `distinct(*fields)` would do the
    newest-per-package half in the database and is PostgreSQL-only, which is the
    reason `collectors/tasks.py` folds its own latest-state-per-package in Python
    rather than there; a window function would be the same bet with a different
    name, and the epoch half -- "the first `ok` after the most recent `not_found`"
    -- is a scan over the ordered history in any case.

    **Which row is newest is compared in Python, not inferred from the order the
    rows arrived in.** The surviving entry for a package is the one with the
    greatest `(observed_at, pk)`, decided by the `<` below. That tie is the normal
    case rather than an exotic one -- one sweep stamps every row it writes with
    the run's single instant (`CPM-AD-7`) -- and it is the same row `snapshot_as_of`
    returns for the same `(package, cutoff)` pair. The epoch bookkeeping *does*
    read the stream in order, which is why the `order_by` is asked for and is
    total: a package's `ok` and `not_found` rows have to be walked in the order
    they were observed, and `pk` makes that order the same on both backends.

    **Membership comes from `package_ids` and from nothing else.** The set of
    packages is decided once, by the caller, and this query only answers what was
    observed of them -- narrowed in SQL, `ID_CHUNK` ids at a time, so a caller
    asking about a few packages reads a few packages' rows; `None` answers for
    every package with a snapshot, which is what the after-run step wants. An
    empty collection is answered `{}` without a query.

    **What this costs, stated rather than implied.** Asked for every package, it
    reads every snapshot at or before the cut-off -- one row per package per sweep
    for the retained history, close to a million rows at `CPM-NFR-1`'s scale over
    ninety daily sweeps -- in a fixed number of queries, which is not the same
    claim as a fixed amount of work. `.iterator()` bounds the memory to
    `SNAPSHOT_CHUNK` rows plus one entry per package.

    Args:
        cutoff: The instant to read as of, aware. Checked by the caller.
        package_ids: The packages to answer for, or `None` for all of them.

    Returns:
        One history per package with at least one snapshot at or before the
        cut-off. A package with none is absent from the mapping rather than
        present with an empty history: "no row" and "a row observing nothing" are
        different facts and only one of them is in here.

    """
    if package_ids is None:
        return fold_observations(_observations(cutoff))
    wanted = sorted(set(package_ids))
    if not wanted:
        return {}
    histories: dict[int, PackageHistory] = {}
    # Chunked, because a `package_id IN (...)` over the whole inventory would be
    # ten thousand parameters against SQLite's ceiling of 999; each chunk folds
    # its own packages, so the chunks are disjoint and the union is the answer.
    for start in range(0, len(wanted), ID_CHUNK):
        chunk = wanted[start : start + ID_CHUNK]
        histories.update(fold_observations(_observations(cutoff, package_ids=chunk)))
    return histories


def _observations(
    cutoff: datetime,
    *,
    package_ids: Collection[int] | None = None,
) -> Iterable[tuple[int, datetime, int, str, int | None, int | None]]:
    """Return the ordered stream of snapshot rows at or before the cut-off.

    Args:
        cutoff: The instant to read as of.
        package_ids: A chunk of packages to narrow the query to, or `None` for
            every package.

    Returns:
        The rows, streamed.

    """
    rows = InventorySnapshot.objects.filter(observed_at__lte=cutoff)
    if package_ids is not None:
        rows = rows.filter(package_id__in=package_ids)
    return (
        rows.order_by("observed_at", "pk")
        .values_list("package_id", "observed_at", "pk", "state", "internal_component_count", "internal_lob_count")
        .iterator(chunk_size=SNAPSHOT_CHUNK)
    )


def fold_observations(
    rows: Iterable[tuple[int, datetime, int, str, int | None, int | None]],
    *,
    package_ids: Collection[int] | None = None,
) -> dict[int, PackageHistory]:
    """Fold an ordered stream of snapshot rows to one history per package.

    The pure half of `histories_at`, so the fold can be exercised without a
    database: `tests/unit/django_apps/test_absence.py` feeds it arrangements as
    tuples. The stream must be ascending by `(observed_at, pk)` for the epoch
    bookkeeping to be right; the newest-row half does not depend on it.

    Args:
        rows: `(package_id, observed_at, pk, state, internal_component_count,
            internal_lob_count)` tuples, in the order the query asks for.
        package_ids: The packages to answer for, or `None` for all of them.

    Returns:
        One history per package with at least one row in the stream.

    """
    # A set rather than the caller's sequence: the membership test runs once per
    # snapshot row, which is the one thing here that grows without bound.
    wanted = None if package_ids is None else frozenset(package_ids)
    histories: dict[int, PackageHistory] = {}
    for package_id, observed_at, snapshot_id, state, component_count, lob_count in rows:
        if wanted is not None and package_id not in wanted:
            continue
        stamp = (observed_at, snapshot_id)
        history = histories.get(package_id)
        if history is None:
            history = PackageHistory(
                newest=stamp,
                internal_component_count=component_count,
                internal_lob_count=lob_count,
            )
            histories[package_id] = history
        # Written as "take it when it is strictly newer" rather than "skip it
        # when it is not". `pk` is unique, so two stamps for one package are never
        # equal and both spellings behave identically while the code is right;
        # they differ when it is wrong -- a stamp narrowed to `observed_at` alone
        # makes ties compare equal, and this spelling then keeps the lowest key,
        # which is the wrong answer and one a test can fail on.
        elif history.newest < stamp:
            history.newest = stamp
            history.internal_component_count = component_count
            history.internal_lob_count = lob_count
        _record_presence(history, state=state, observed_at=observed_at)
    return histories


def _record_presence(history: PackageHistory, *, state: str, observed_at: datetime) -> None:
    """Advance one package's listing bookkeeping by one row, in stream order.

    Args:
        history: The package's accumulator.
        state: The row's `state`.
        observed_at: When the row was observed.

    """
    if state == OutcomeState.OK.value:
        history.last_listed = observed_at
        if history.absent_since is not None:
            history.listed_since = observed_at
            history.absent_since = None
    elif state == OutcomeState.NOT_FOUND.value and history.absent_since is None:
        # The first absence row closes the current epoch: whatever `listed_since`
        # was is history now, and the next `ok` starts a new one. A second
        # `not_found` while already absent changes nothing -- `since` is the
        # first one, so a source repeating its absence does not move the date.
        history.listed_since = None
        history.absent_since = observed_at


def absence_at(*, cutoff: datetime, package_ids: Collection[int] | None = None) -> dict[int, Absence]:
    """Return what the inventory said about each package's presence, as of a cut-off.

    Args:
        cutoff: The instant to read as of, aware. Supplied by the caller from its
            run (`CPM-AD-25`), never derived here and never a clock reading.
        package_ids: The packages to answer for, or `None` for every package with
            a snapshot.

    Returns:
        An `Absence` by package primary key, holding an entry only for packages
        with at least one snapshot at or before the cut-off. A package with none
        is not absent -- nothing has observed it either way -- and is missing from
        the mapping rather than present as listed.

    Raises:
        InventoryReadError: When `cutoff` is not an aware `datetime`. See
            `require_usable_cutoff`.

    """
    require_usable_cutoff(cutoff, subject="inventory absence")
    histories = histories_at(cutoff=cutoff, package_ids=package_ids)
    return {package_id: history.absence() for package_id, history in histories.items()}


def mark_inventory_absence(*, run: PolicyRun, clock: Clock) -> int:
    """Stamp the run's rollup rows with what the inventory said at the run's cut-off.

    The after-run step `collectors/apps.py` registers (`CPM-AD-11`). It reads
    absence at `run.evidence_cutoff` and writes the two nullable columns
    `PackageHealth` carries for it: `inventory_absent_since` and
    `inventory_last_listed` for an absent package; the compose has already
    written both `NULL` on every row of the run, which is what a listed package
    carries. The row itself stays -- one row per package is the rollup's rule, and an absent
    package's statuses are computed exactly as before; absence gates nothing.

    **It lives here rather than in `core/rollup.py`** because `core` may not read
    `InventorySnapshot` (`CPM-AD-4`'s layering, held by
    `tests/unit/django_apps/test_app_layering_audit.py`); the columns are
    `core`'s, the reading is this application's, and the after-run seam is the
    one place the two meet without `core` learning a collector's table.

    **Only the run's rows.** A package whose rollup write failed this run still
    holds the previous run's row, and that row's absence columns describe *that*
    run's cut-off; rewriting them against this run's cut-off would make the row
    disagree with the `evidence_cutoff` it carries.

    Args:
        run: The finished policy run whose rows are stamped.
        clock: Unread. The step is a function of the run's cut-off alone, which
            is what makes a replay reproduce it; the seam supplies a clock to
            every step and this one has no instant to read.

    Returns:
        How many rollup rows were stamped absent.

    """
    del clock
    absences = absence_at(cutoff=run.evidence_cutoff)
    # The rows to stamp, grouped by what they are stamped with. Packages leave
    # the inventory in sweeps, so a night that retires fifty rows is a handful of
    # distinct `(since, last_listed)` pairs and as many statements -- never one
    # per package. The compose has already written both columns `NULL` on every
    # row of this run (`core/rollup.py`), so a listed package needs no write here.
    stamps: dict[tuple[datetime, datetime | None], list[int]] = {}
    for package_id, reading in absences.items():
        if reading.absent and reading.since is not None:
            stamps.setdefault((reading.since, reading.last_listed), []).append(package_id)
    # Spelled on the manager rather than on a local queryset, so
    # `tests/unit/django_apps/test_mutation_path_audit.py` sees it and counts it
    # against this module's recorded allowance of one: `package_health` is derived
    # state, not evidence, and these two columns are the after-run seam's to
    # write -- but a second `update()` here is a decision somebody records.
    stamped = 0
    for (since, last_listed), package_ids in stamps.items():
        stamped += PackageHealth.objects.filter(policy_run=run, package_id__in=package_ids).update(
            inventory_absent_since=since,
            inventory_last_listed=last_listed,
        )
    logger.info(
        ABSENCE_MARKED_EVENT,
        policy_run=run.pk,
        cutoff=run.evidence_cutoff.isoformat(),
        absent=sum(len(package_ids) for package_ids in stamps.values()),
        stamped=stamped,
    )
    return stamped
