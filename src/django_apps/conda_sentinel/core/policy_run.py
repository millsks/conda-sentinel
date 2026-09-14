"""The orchestration: one run, one cut-off, the registered passes, then the rollup.

`CPM-AD-21` is the whole of this module. A policy run chooses **one** evidence
cut-off, opens **one** ledger row, executes the registered passes in declared
order, and hands their contributions to the one writer of the rollup. Every pass
in one run reads evidence as of the same instant, which is what makes
`CPM-FR-22`'s promise -- re-run this version against this cut-off and get
identical output -- a property of the system rather than of when the sweep
happened to start.

**The cut-off is the `finished_at` of a completed collection run, and a run with
nothing completed behind it refuses.** `CPM-AD-21` names exactly one forbidden
value, the current time, and the reason is not tidiness: a cut-off of *now*
silently includes evidence from a collection run still in flight, so the same
version replayed against the same nominal cut-off reads a different evidence set
and produces a different answer. A ledger with nothing completed has no correct
cut-off, and inventing one makes every later replay of that version disagree with
the original -- quietly, with no failure anywhere to point at. So it raises.

The query is `RunLedgerQuerySet.finished()`, the exact mirror of `unfinished()`,
and it does not filter on `status`: a collection run that failed after writing
some evidence has still *ended*, and choosing a cut-off earlier than evidence the
ledger actually holds would hide rows the system has.

**One `transaction.atomic()` per package, twice, and neither around the
recorder.** `CPM-AD-23` fixes one package as the atomic unit, so the pass phase
takes the package as its outer loop and runs every pass for that package inside
one transaction: a pass raising for one package rolls that package's derived rows
back and leaves every other package's committed. The rollup's own per-package
transaction is `core/rollup.py`'s, and it contains per package too -- a row that
will not compose must not take the packages after it down. Neither encloses
`core/ledger.py`'s recorder -- that module's ordering guarantee depends on the
`running` row committing before anything else happens, and
`tests/unit/django_apps/test_collector_base_audit.py` sweeps for the inversion in
both directions.

**The inventory is read once per run and handed to both phases.** Reading it
twice is a `Package` inserted between the reads getting a rollup row no pass
evaluated -- stamped with a run that never looked at it -- and a "n of m failed"
counted against a different m than the one that ran. `CPM-AD-25` inserts into
that table continuously, so the window is not theoretical.

**A failed package is excluded from the compose rather than written blank.**
`CPM-NFR-3` says the system "degrades to stale evidence, never to a clean
result": a package whose pass raised keeps the row it had, which is the older
answer, rather than being overwritten with a health computed from a pass that
never finished. The consequence, stated rather than discovered: a package seen
for the first time in a run whose pass raised for it has no rollup row until the
next run. "Exactly one row per package" is a property of a run that completed.

**Three endings, and the boundary between them is not arbitrary.** Some packages
failed is `partial`. *Every* package failed is `failed`: `core/runs.py` keeps the
endings distinct so a reader never has to infer which happened, and
`RunLedgerQuerySet.failed()` deliberately excludes `partial` -- so a run that
accomplished nothing and called itself `partial` is invisible to the one query
`CPM-FR-38` exists to make answerable. No packages at all is `succeeded`: zero
failures out of zero is not a failed run, it is a monitor with nothing to monitor
yet.

**Beat schedules this, and never a pass.** `CPM-AD-20` puts every cadence in
`django_celery_beat`; there is no schedule in this module, in `core/tasks.py`, or
anywhere outside `config/settings/`. A pass is not a task and has no cadence of
its own -- it runs because a run ran.

**On the `AD-` prefix.** A bare `AD-n` in this repository is an *inherited*
platform decision; a decision from this product's own architecture spine always
carries the `CPM-` prefix.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from typing import TYPE_CHECKING
from typing import Final
from typing import cast

import structlog
from django.db import models
from django.db import transaction

from conda_sentinel.core.after_run import run_after_run_steps
from conda_sentinel.core.ledger import policy_run
from conda_sentinel.core.models import FINISHED_AT_FIELD
from conda_sentinel.core.models import CollectionRun
from conda_sentinel.core.models import PolicyRun
from conda_sentinel.core.policy import PolicyPassError
from conda_sentinel.core.policy import registered_passes
from conda_sentinel.core.retention import PRUNE_COLLECTOR
from conda_sentinel.core.rollup import compose_rollup
from conda_sentinel.core.rollup import packages_for_rollup
from conda_sentinel.core.rollup import permitted_values

if TYPE_CHECKING:
    from collections.abc import Mapping
    from collections.abc import Sequence
    from datetime import datetime

    from conda_sentinel.core.clock import Clock
    from conda_sentinel.core.ledger import RunHandle
    from conda_sentinel.core.policy import PolicyPass
    from conda_sentinel.identity.models import Package

__all__ = [
    "FAILED_DETAIL",
    "PARTIAL_DETAIL",
    "PolicyRunError",
    "PolicyRunSummary",
    "choose_evidence_cutoff",
    "execute_policy_run",
]

logger = structlog.get_logger(__name__)

#: The column an in-flight collection run is bounded by, and the key
#: `aggregate(Min(...))` returns it under. Named rather than spelled at the call
#: site for the reason `core/models.py`'s `FINISHED_AT_FIELD` is: the aggregate
#: key is derived from the field name, so a typo in one and not the other is a
#: `KeyError` at boot rather than a wrong cut-off, but only if they are written
#: once.
STARTED_AT_FIELD: Final[str] = "started_at"
UNFINISHED_BOUNDARY_KEY: Final[str] = f"{STARTED_AT_FIELD}__min"

#: The event a per-package pass failure is logged under. Named so the case that
#: asserts the log and the code that emits it cannot drift -- and it is asserted:
#: this line is the only operational record of *which* pass broke on *which*
#: package, because the ledger row carries a count and a count with no names sends
#: an operator through the whole inventory by hand.
#:
#: Named `EVALUATION_` rather than after the pass because ruff's `S105` reads any
#: constant whose name contains `PASS` as a hardcoded credential.
EVALUATION_FAILED_EVENT: Final[str] = "policy_pass_failed"

#: What a run finalizes `partial` with when some packages could not be computed.
#: A format string rather than a message built at the call site, so the detail a
#: reader meets in the ledger is the same shape every time. It says "could not be
#: computed" rather than "failed a policy pass" because both phases contribute to
#: the count: a pass that raised, and a rollup row that would not write.
PARTIAL_DETAIL: Final[str] = "{failed} of {total} packages could not be computed"

#: What a run finalizes `failed` with when *no* package could be computed.
#:
#: `core/runs.py` keeps the four endings distinct precisely so a reader is never
#: asked to infer which happened, and `RunLedgerQuerySet.failed()` deliberately
#: excludes `partial` -- so a run that accomplished nothing and recorded itself
#: `partial` is invisible to the one query `CPM-FR-38` exists to make answerable.
#: A run that did some of its work is a different operational fact from one that
#: did none, and the empty inventory is a third: zero packages is not zero
#: failures, so it stays a success.
FAILED_DETAIL: Final[str] = "every one of {total} packages failed; no rollup row was written"


class PolicyRunError(Exception):
    """A policy run could not be started, or could not be started correctly.

    One type rather than a hierarchy, on the same terms as `core/runs.py`'s
    `RunLedgerError`: no caller branches on which precondition was missing --
    every occurrence is an operational state to fix rather than a condition to
    handle -- so the detail lives in the message.

    Not a `ValueError`. The `ValueError` subclasses in this product
    (`PolicyPassError`, `CollectorRegistryError`, `ConfidenceError`) all say "this
    *declaration* is unusable"; this one says the ledger is not yet in a state
    where a run has a correct cut-off, which is a fact about the data.
    """


@dataclass(frozen=True, slots=True)
class PolicyRunSummary:
    """What one policy run did, for the caller that asked for it.

    Returned rather than logged only, because the task that schedules a run and
    the integration cases that assert one both need the same four facts, and a
    caller reconstructing them from the ledger would be asking the database what
    it just did.

    Attributes:
        policy_run: The ledger row this run was recorded on.
        evidence_cutoff: The instant every pass read evidence as of.
        rollup_rows: How many rollup rows were written.
        after_run: What each registered after-run step reported, by name.
        failed_packages: The primary keys of the packages that could not be
            computed, in the order they were met -- the pass phase's failures
            first, then the compose phase's. Empty for a run that finalized
            `succeeded`.

    """

    policy_run: PolicyRun
    evidence_cutoff: datetime
    rollup_rows: int
    failed_packages: tuple[int, ...] = field(default=())

    #: What each registered after-run step reported, by name. Empty in a component
    #: that has adopted no application registering one, which is a legitimate state
    #: and not a failure -- `core` declares the seam and does not require it to be
    #: filled.
    after_run: dict[str, int] = field(default_factory=dict)


def choose_evidence_cutoff() -> datetime:
    """Return the instant this run must read evidence as of.

    `CPM-AD-21` makes two demands of this instant, and only one of them is about
    where the value comes from. It must **be** the `finished_at` of a completed
    collection run -- a real, reproducible instant the ledger holds, never the
    current time -- and no pass reading as of it may see evidence written by a
    run still `running`.

    **The second demand is not satisfied by the first, which is the whole reason
    this function is more than one query.** Take a run that started at 10:00 and
    is still in flight, and a second that ran from 11:00 to 11:30. The newest
    ending is 11:30, and the in-flight run is free to write evidence stamped
    11:15 -- inside that cut-off, from a run that has not finished. Choosing the
    newest ending alone reads it, and reads a *different* set on every replay as
    that run continues writing, which is precisely the non-reproducibility
    `CPM-FR-22` exists to prevent.

    So the boundary is the earliest `started_at` among unfinished runs: no
    in-flight run can write evidence from before it began. The cut-off is the
    newest ending at or before that boundary, which satisfies both demands at
    once.

    Returns:
        The `finished_at` of the most recently ended collection run that no
        in-flight run can still write behind.

    Raises:
        PolicyRunError: When no such ending exists -- either because no
            collection run has ended at all, or because every ending is behind a
            run that is still going. The only alternative is the current time,
            which is the one value `CPM-AD-21` forbids, and a run with nothing
            settled behind it has no correct cut-off to fall back to. A stuck
            collection run therefore holds policy runs back rather than letting
            them read half-written evidence, and `RunLedgerQuerySet.unfinished()`
            is what makes that visible rather than mysterious.

    """
    # The nightly purge records one `collection_runs` row per table under its
    # own name (`CPM-OPERATE-S07`), and those rows are not collections: a purge's
    # ending is not an instant evidence was complete as of, and a purge killed
    # mid-run would otherwise bound every cut-off at its start until the row
    # aged out. Excluded by name, and only that name -- the demo seeder's
    # synthetic run is a collection as far as this choice is concerned, and a
    # fresh seed relies on it being counted.
    collections = CollectionRun.objects.exclude(collector=PRUNE_COLLECTOR)
    endings = collections.finished()
    boundary = collections.unfinished().aggregate(models.Min(STARTED_AT_FIELD))[UNFINISHED_BOUNDARY_KEY]
    if boundary is not None:
        endings = endings.filter(**{f"{FINISHED_AT_FIELD}__lte": boundary})
    cutoff = endings.values_list(FINISHED_AT_FIELD, flat=True).first()
    if cutoff is None:
        message = (
            "this policy run has no evidence cut-off. CPM-AD-21 makes the cut-off the finished_at of a "
            "completed collection run and forbids the current time: a cut-off of now silently includes "
            "evidence from a run still in flight, and every replay of this version would then read a "
            f"different evidence set (CPM-FR-22). Ended runs usable as a cut-off: {endings.count()}; "
            f"earliest still-running run started at {boundary!r}. A run that is still going bounds the "
            "cut-off to before it began, because it may still write evidence from any instant after that."
        )
        raise PolicyRunError(message)
    # `values_list(flat=True)` is typed as yielding `Any`, because the column it
    # names is resolved at runtime. The cast names what `finished_at` is declared
    # as rather than letting `Any` leak into every caller's cut-off.
    return cast("datetime", cutoff)


def execute_policy_run(
    *,
    policy_version: str,
    clock: Clock,
    evidence_cutoff: datetime | None = None,
) -> PolicyRunSummary:
    """Run every registered pass at one cut-off, then compose the rollup.

    **`evidence_cutoff` is what makes `CPM-FR-22`'s replay an operation rather
    than a property.** The guarantee is "re-run *this version* against *this
    cut-off* and get identical output", and it has two halves: the passes must be
    deterministic, and a caller must be able to name the cut-off. Choosing one
    afresh on every call delivers only the first -- any collection run finishing
    between the original and the replay moves the boundary, so the two runs read
    different evidence and the comparison a replay exists for is not available.

    Additive and defaulted, so every existing caller keeps the behaviour it had:
    a scheduled run passes nothing and gets `choose_evidence_cutoff()`'s answer,
    which is the newest ending no in-flight collection can write behind. A replay
    passes the `evidence_cutoff` off the run it is replaying, which every
    `PolicyRun` row and every `PackageHealth` row carries.

    A supplied cut-off is used as given and is **not** validated against the
    ledger. That is deliberate: the ledger's own rule is about choosing an
    instant nothing can still write behind, and a replay is asking about an
    instant that has already been chosen and recorded -- re-deriving it would
    refuse the exact operation this parameter exists for, because the run being
    replayed may sit behind a collection that has since started.

    Args:
        policy_version: The version of the rule data this run applies
            (`CPM-AD-8`), recorded on the ledger row and on every rollup row's
            per-domain version map.
        clock: The clock the run's lifecycle instants and the rollup's
            `computed_at` are read from (`CPM-AD-26`). Separate from the evidence
            cut-off, which is a property of the evidence rather than of when this
            run happens to execute.
        evidence_cutoff: The instant to read evidence as of. `None` -- the
            default -- chooses one from the run ledger, which is what a scheduled
            run does. Pass the `evidence_cutoff` of an earlier run to replay it.

    Returns:
        What the run did.

    Raises:
        PolicyRunError: When no cut-off was supplied and the ledger holds none to
            choose. Raised *before* the ledger row is opened: a run that cannot
            read evidence correctly should leave no row claiming it tried.
        Exception: Whatever a pass's `prepare` raised. Not contained per package
            -- see `_execute_passes` -- so the ledger row finalizes `failed`, no
            derived row and no rollup row is written, and the caller is told
            once rather than once per package.

    """
    if evidence_cutoff is None:
        evidence_cutoff = choose_evidence_cutoff()
    passes = registered_passes()
    versions = {policy_pass.name: policy_version for policy_pass in passes}

    # Never inside a `transaction.atomic()`. `core/ledger.py` says the ordering
    # guarantee -- a row exists for a worker killed mid-run -- depends on the
    # `running` row committing before the work starts.
    with policy_run(policy_version=policy_version, evidence_cutoff=evidence_cutoff, clock=clock) as handle:
        # The recorder builds the row and knows its type; `RunHandle` is shared
        # with the collection recorder and is therefore typed to the abstract
        # base. The cast names what `policy_run` yielded rather than adding a
        # runtime branch nothing can reach.
        run = cast("PolicyRun", handle.run)
        # Read **once**, and handed to both phases. Two reads of a table
        # `CPM-AD-25` inserts into continuously is a package evaluated by no pass
        # but given a rollup row anyway, and a "n of m failed" counted against a
        # different m than the one that ran.
        packages = packages_for_rollup()
        contributions, failed = _execute_passes(passes, packages, run=run, evidence_cutoff=evidence_cutoff)
        rollup_rows, unwritten = compose_rollup(
            policy_run=run,
            evidence_cutoff=evidence_cutoff,
            packages=packages,
            contributions=contributions,
            policy_versions=versions,
            clock=clock,
            skipped=failed,
        )
        failed.extend(unwritten)
        # After the rollup, because a step reads what the run concluded. Inside the
        # ledger's `with`, because a step that fails has to fail the run: a run that
        # reported success with the queues it was meant to fill still empty is nobody
        # looking at work nobody knows exists.
        #
        # What the steps *are* is not this module's business -- `core/after_run.py`
        # declares the seam and an adopted application fills it, exactly as the pass
        # registry works. This orchestrator has never heard of a queue.
        after_run = run_after_run_steps(run=run, clock=clock)
        if failed:
            _declare_ending(handle, failed=len(failed), total=len(packages))

    return PolicyRunSummary(
        policy_run=run,
        evidence_cutoff=evidence_cutoff,
        rollup_rows=rollup_rows,
        failed_packages=tuple(failed),
        after_run=after_run,
    )


def _declare_ending(handle: RunHandle, *, failed: int, total: int) -> None:
    """Declare `partial` or `failed` according to how much of the run survived.

    Args:
        handle: The recorder's handle for this run.
        failed: How many packages could not be computed, from both phases.
        total: How many packages the run was over.

    """
    if failed >= total:
        handle.failed(detail=FAILED_DETAIL.format(total=total))
        return
    handle.partial(detail=PARTIAL_DETAIL.format(failed=failed, total=total))


def _execute_passes(
    passes: Sequence[type[PolicyPass]],
    packages: Sequence[Package],
    *,
    run: PolicyRun,
    evidence_cutoff: datetime,
) -> tuple[dict[int, dict[str, str]], list[int]]:
    """Run every pass over every package, one transaction per package.

    The package is the outer loop because `CPM-AD-23` makes one package the
    atomic unit: everything a run does for a package either commits together or
    rolls back together. The passes run inside it in declared order, which is
    where "a later pass reads an earlier pass's derived rows for this run" holds
    -- the reads a pass makes are about the package being evaluated.

    **Every pass prepares first, and a preparation failure is not contained.**
    See the loop below and `core/policy.py`'s `PolicyPass.prepare`: a fault that
    is every package's is cheaper, more legible and more honest as one failed run
    than as one failure per package.

    Args:
        passes: The registered pass classes, in declared order.
        packages: The packages to evaluate.
        run: The ledger row every derived row is keyed to.
        evidence_cutoff: The instant every pass reads evidence as of.

    Returns:
        The contributions by package primary key, and the primary keys of the
        packages a pass raised for. A failed package appears in the second and
        not the first: its derived rows were rolled back, so there is nothing
        left to contribute.

    """
    instances = [policy_pass() for policy_pass in passes]
    # Once per run, before the loop, and **not** contained. `PolicyPass.prepare`
    # is where a pass establishes what cannot differ between packages -- a rule
    # set chosen by the run's policy version, say -- and a failure of it is every
    # package's failure. Catching it per package would produce one traceback and
    # one failed row per package for a condition knowable before the first one,
    # and would report an inventory-wide misconfiguration as ten thousand
    # individual faults. Raised here, the ledger row finalizes `failed`, nothing
    # is written, and the caller is told once.
    for policy_pass in instances:
        policy_pass.prepare(policy_run=run, evidence_cutoff=evidence_cutoff)
    contributions: dict[int, dict[str, str]] = {}
    failed: list[int] = []
    for package in packages:
        produced: dict[str, str] = {}
        try:
            # `CPM-AD-23`: one package, one transaction, nested inside the run
            # recorder and never around it.
            with transaction.atomic():
                for policy_pass in instances:
                    produced.update(
                        _owned(
                            policy_pass,
                            policy_pass.evaluate(package, policy_run=run, evidence_cutoff=evidence_cutoff),
                        ),
                    )
        # Caught this widely on purpose, and not swallowed: a pass is somebody
        # else's code and may raise anything at all, and `CPM-AD-23`'s partial
        # success is the whole point -- the packages that worked commit, this one
        # rolls back, and the run finalizes `partial` saying how many. The
        # failure is logged with the package and the traceback, so it is on the
        # record rather than merely counted.
        except Exception:
            logger.exception(EVALUATION_FAILED_EVENT, policy_run_pk=run.pk, package_pk=package.pk)
            failed.append(package.pk)
            continue
        contributions[package.pk] = produced
    return contributions, failed


def _owned(policy_pass: PolicyPass, produced: Mapping[str, str]) -> Mapping[str, str]:
    """Refuse a contribution the pass did not declare, or that the column cannot hold.

    The registry checks what a pass *claims* at registration; this checks what it
    actually returned, on three counts.

    **The column must be one the pass declared.** A pass that declared
    `currency_status` and returned `licence_status` would otherwise write into a
    column another pass owns -- `CPM-AD-11`'s single-owner rule broken at runtime
    by a declaration that passed every static check.

    **The value must be one the column offers.** `choices` is a form and
    `full_clean()` rule and Django enforces neither on `save()`, so a pass
    returning `"clean"` where the column offers `ok` writes `"clean"` into the
    database -- and `CPM-AD-24` has every read surface emit that verbatim to a
    consumer that has never heard of it. `core/rollup.py`'s `permitted_values`
    reads the column's own declared vocabulary, which is what lets a per-status
    type's determinate verdicts through while refusing an invented one.

    **`None` is refused rather than read as "no verdict".** It is the one value
    with two plausible readings -- "leave the column alone", which the full-row
    replace has no way to honour, and "write the default", which is what a merge
    would silently do -- and a pass that means the second has four sentinels to
    say it with. `not_found`, `unknown`, `error` and `not_applicable` exist
    precisely so that "we have nothing determinate" is a *stated* value rather
    than an absence (`CPM-FR-6`). A pass that means "I did not run for this
    package" omits the key.

    Args:
        policy_pass: The pass that produced the mapping.
        produced: What it returned for one package.

    Returns:
        The mapping, unchanged.

    Raises:
        PolicyPassError: On any of the three. Raised inside the package's
            transaction, so the package rolls back and the run finalizes `partial`
            naming it -- a defect in one pass does not take the inventory down.

    """
    declared = set(policy_pass.contributes)
    undeclared = sorted(set(produced) - declared)
    if undeclared:
        message = (
            f"policy pass {policy_pass.name!r} produced {undeclared}, which it did not declare in "
            f"contributes={sorted(declared)}. A rollup column has one owner (CPM-AD-11), and a pass writing "
            f"into a column it did not claim is that rule broken at runtime."
        )
        raise PolicyPassError(message)

    for column, value in sorted(produced.items()):
        if value is None:
            message = (
                f"policy pass {policy_pass.name!r} returned None for {column!r}. None has two readings and "
                f"this writer can honour neither: a full-row replace cannot leave a column alone, and "
                f"writing the field default silently would be the merge it exists not to do. Say it with a "
                f"sentinel -- unknown, not_found, error or not_applicable (CPM-FR-6) -- or omit the key."
            )
            raise PolicyPassError(message)
        permitted = permitted_values(column)
        if value not in permitted:
            message = (
                f"policy pass {policy_pass.name!r} returned {value!r} for {column!r}, which that column does "
                f"not offer. Its declared values are {sorted(permitted)}. Django does not enforce choices on "
                f"save(), so an unrecognised value reaches the database and is then emitted verbatim by every "
                f"read surface (CPM-AD-5, CPM-AD-24)."
            )
            raise PolicyPassError(message)
    return produced
