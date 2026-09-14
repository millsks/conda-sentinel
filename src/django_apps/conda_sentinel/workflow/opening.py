"""What the policy run opens, once it has decided what it thinks.

`CPM-APP-S04` built the table and left this unwired on purpose: the run has to open
the items, and having `core/policy_run.py` import this module would invert the
orchestration `core` is built on. So `core/after_run.py` declares a seam,
`workflow/apps.py` registers this step into it at `ready()`, and the orchestrator
still has never heard of a queue.

**A step reads what the run concluded, and concludes nothing itself.** It looks at
the derived rows the passes wrote and opens an item where one of them says there is
work. It does not decide *whether* something is a problem -- `CPM-AD-10` gives that
to the policy engine -- and it does not rank anything, because the priority pass
already did.

**Every item is opened by finding key**, so a run that has already opened one finds
it rather than making a second. That is `CPM-APP-S04`'s AC 2 and it is what makes
this step safe to run every night: after the first run, most nights open nothing at
all.

**Three sources, three queues.** A vulnerability the passes matched is remediation
work. A licence the passes could not clear is compliance work. A package the
resolver could not identify is identity work, and it is the one that is not
evidence-backed -- the finding is the *absence* of an identity, so the key is built
from the package rather than from a row.

**The confidence gate decides what is even askable.** An `unmapped` package has no
verdicts worth acting on -- every status on it is `unknown` by `CPM-AD-4` -- so it
opens exactly one item, in the identity queue, and none of the others. Opening a
remediation item for a package nobody has identified would put work in a queue that
cannot be done until different work in a different queue is finished first.

**An absent package leaves the queues** (`CPM-OPERATE-S11`). A package the
inventory no longer listed at the run's cut-off -- `collectors/absence.py`'s reading,
never the inventory table's `retired_at` -- is skipped by every opener, and every
open item it already has is closed by the product afterwards, as a `system`
transition with a reason (`workflow/services.py`'s `close_for_absence`). Both halves
are read at `run.evidence_cutoff`, so a replayed run skips and closes the same
packages, and the close is idempotent: an item already resolved is left alone.

**The identity opener is the selection's first production caller.** It opens for
what `collectors/selection.py`'s `select_unresolved` offers at the run's cut-off
rather than for every `unmapped` package as of now, which is what makes the opening
cut-off bound and replayable. A re-listed package -- one the inventory dropped and
later listed again -- gets a *fresh* item in every queue: its keys carry the instant
its current listing began, so the items the product closed stay closed and new work
opens beside them. A package that was never absent keeps the keys it always had, so
nothing already resolved re-opens; and a package with an open identity item under
*any* key gets no second one, so a history the purge has eroded -- the `not_found`
row gone, the key reverting to the bare pair -- cannot open a duplicate beside the
epoch-keyed item.

**One fold per run.** The inventory histories are read once, for every package the
run rolled up and every package with an open item, and handed to the selection and
to the closer: the closer has to reach a package whose compose failed this run,
which has no rollup row this run and still has work.

**On the `AD-` prefix.** A bare `AD-n` in this repository is an *inherited* platform
decision; a decision from this product's own architecture spine always carries the
`CPM-` prefix.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from typing import Final

from conda_sentinel.collectors.absence import histories_at
from conda_sentinel.collectors.selection import select_unresolved
from conda_sentinel.core.finding_keys import finding_key_of
from conda_sentinel.identity.confidence import IdentityConfidence
from conda_sentinel.policies.models import PackageLicense
from conda_sentinel.policies.models import PackageVulnerability
from conda_sentinel.workflow.models import WorkflowItem
from conda_sentinel.workflow.services import close_for_absence
from conda_sentinel.workflow.services import open_item
from conda_sentinel.workflow.services import open_keyed_item
from conda_sentinel.workflow.states import TERMINAL_STATES
from conda_sentinel.workflow.states import Queue

if TYPE_CHECKING:
    from collections.abc import Iterable
    from collections.abc import Mapping

    from conda_sentinel.collectors.absence import Absence
    from conda_sentinel.collectors.absence import PackageHistory
    from conda_sentinel.core.clock import Clock
    from conda_sentinel.core.finding_keys import FindingKeyed
    from conda_sentinel.core.models import PolicyRun
    from conda_sentinel.identity.models import Package

__all__ = ["IDENTITY_FINDING_TABLE", "LISTED_SINCE_FACT", "OPENING_STEP_NAME", "open_queue_items"]

#: The name the step is registered under, and the one a failed run reports.
OPENING_STEP_NAME: Final[str] = "workflow.open_queue_items"

#: What an identity finding is keyed against.
#:
#: The packages table rather than an evidence table, because this is the one queue
#: whose finding is not an observation: nothing was *seen*, and what makes it work is
#: that resolution could not establish an identity. `packages` is where that fact
#: lives.
IDENTITY_FINDING_TABLE: Final[str] = "packages"

#: The fact a finding key carries for a package that has been absent and is listed
#: again: the instant its current listing began, ISO 8601.
#:
#: **Only** for such a package, and on every queue's key. A never-absent package's
#: keys are what they always were, so every existing key is stable and nothing
#: already resolved re-opens; a re-listed one is a new epoch and new items, because
#: the items the product closed when it left say nothing about the package that
#: came back -- and an evidence-backed key is the advisory or the licence, which is
#: the same row before and after, so without the epoch a remediation or compliance
#: item closed for absence would never re-open.
LISTED_SINCE_FACT: Final[str] = "listed_since"

#: The vulnerability verdict that means there is remediation work.
#:
#: Only this one. `no_advisory_matched` is a clean answer and the four sentinels mean
#: the product formed no opinion -- opening work for those would fill a queue with
#: packages nobody can act on, which is the queue nobody then reads.
ADVISORIES_MATCHED: Final[str] = "advisories_matched"

#: The licence verdicts that mean somebody has to look.
#:
#: `manual_review` is the shipped default at every recorded parameter version, so
#: this is currently most packages -- which is correct and is worth knowing: until an
#: operator records a licence rule set (PRD Open Question 4), the compliance queue is
#: the whole inventory. `forbidden` and `restricted` are the verdicts a rule set
#: produces once there is one.
LICENCE_NEEDS_A_HUMAN: Final[frozenset[str]] = frozenset({"manual_review", "restricted", "forbidden"})


def open_queue_items(*, run: PolicyRun, clock: Clock) -> int:
    """Open a queue item for everything this run concluded is work, and close what left.

    Args:
        run: The finished policy run whose derived rows this reads.
        clock: The clock the stamps are read from (`CPM-AD-26`).

    Returns:
        How many items this call opened plus how many it closed for absence. Zero
        on a night when nothing changed, which is the ordinary case once the first
        run has been through the inventory. One number rather than two because the
        seam reports one count per step; the log lines say which is which.

    """
    histories = histories_at(cutoff=run.evidence_cutoff, package_ids=_packages_the_run_touches(run))
    absences = {package_id: history.absence() for package_id, history in histories.items()}
    absent = frozenset(package_id for package_id, reading in absences.items() if reading.absent)
    opened = sum(
        (
            _open_identity_items(run=run, histories=histories, absences=absences, clock=clock),
            _open_remediation_items(run=run, absent=absent, absences=absences, clock=clock),
            _open_compliance_items(run=run, absent=absent, absences=absences, clock=clock),
        ),
    )
    closed = _close_absent_items(absences=absences, clock=clock)
    return opened + closed


def _packages_the_run_touches(run: PolicyRun) -> set[int]:
    """Return every package this step has to read the inventory for.

    The run's rollup rows, and every package with an open item: the two sets differ
    by exactly the packages whose compose failed this run, which have work to close
    and no row this run to find them by. Decided once, here, so the selection and
    the closer read one fold.

    Args:
        run: The finished run.

    Returns:
        The package ids.

    """
    rolled_up = set(run.rollup_rows.values_list("package_id", flat=True))
    with_open_work = set(
        WorkflowItem.objects.exclude(state__in=TERMINAL_STATES).values_list("package_id", flat=True).distinct(),
    )
    return rolled_up | with_open_work


def _epoch_facts(reading: Absence | None) -> list[tuple[str, str]]:
    """Return the key fact a re-listed package's items carry, or nothing.

    Args:
        reading: What the inventory said about the package at the cut-off, or
            `None` when it never observed the package.

    Returns:
        `[(LISTED_SINCE_FACT, <iso>)]` when the package has been absent and is
        listed again; `[]` otherwise, so every never-absent key is unchanged.

    """
    if reading is None or reading.listed_since is None:
        return []
    return [(LISTED_SINCE_FACT, reading.listed_since.isoformat())]


def _open_identity_items(
    *,
    run: PolicyRun,
    histories: Mapping[int, PackageHistory],
    absences: Mapping[int, Absence],
    clock: Clock,
) -> int:
    """Open an item for every package the resolver could not identify, at the run's cut-off.

    The one queue whose finding is not evidence-backed: nothing was observed, and
    what makes it work is that `CPM-AD-4`'s gate is blanking every verdict on this
    package until somebody resolves it.

    Read through `select_unresolved` at `run.evidence_cutoff` rather than off
    `packages` as of now: the selection leaves out what the inventory no longer
    listed at the cut-off, so the opening is a function of the run and a replay
    opens for the same packages. Identity is still *current state* -- the selection
    reads `Package.confidence` as it is -- and a package resolved between two runs
    stops needing the work at once; the item it already has is closed by a person.

    **At most one open identity item per package.** A package with an open item
    under any key gets no second one: the purge can erode a history so that the
    epoch fact disappears and the key reverts to the bare pair, and without this
    rule that would open a duplicate beside the epoch-keyed item.

    Args:
        run: The finished run, for its cut-off.
        histories: The inventory histories at that cut-off, already folded.
        absences: The readings off those histories, for the listing epoch a
            re-listed package's key carries.
        clock: The clock the stamps are read from.

    Returns:
        How many items this opened.

    """
    already_open = set(
        WorkflowItem.objects.filter(queue=Queue.IDENTITY_REVIEW.value)
        .exclude(state__in=TERMINAL_STATES)
        .values_list("package_id", flat=True),
    )
    opened = 0
    for entry in select_unresolved(cutoff=run.evidence_cutoff, histories=histories).packages:
        if entry.package.confidence != IdentityConfidence.UNMAPPED:
            # The selection offers every unresolved confidence; the identity queue
            # is for the one the gate blanks everything on. `inventory-derived` is
            # an identity, if a weak one, and opens no work.
            continue
        if entry.package.pk in already_open:
            continue
        facts = [("confidence", IdentityConfidence.UNMAPPED.value), *_epoch_facts(absences.get(entry.package.pk))]
        key, readable = finding_key_of(IDENTITY_FINDING_TABLE, entry.package.pk, facts)
        opened += open_keyed_item(
            finding_key=key,
            finding_facts=readable,
            package=entry.package,
            queue=Queue.IDENTITY_REVIEW.value,
            clock=clock,
        ).created
    return opened


def _close_absent_items(*, absences: Mapping[int, Absence], clock: Clock) -> int:
    """Close every open item of every package the inventory no longer listed at the cut-off.

    After the openers, so a package that left the inventory and still had a
    verdict this run cannot be opened for and closed in the same breath in the
    wrong order. Each close is `close_for_absence`'s: a `system` transition with a
    reason, idempotent on an item already finished -- and only a close this step
    *wrote* is counted, so an item a person resolved between the read and the
    lock is theirs rather than the run's.

    Args:
        absences: What the inventory said at the cut-off.
        clock: The clock the stamps are read from.

    Returns:
        How many items this step closed.

    """
    absent = {package_id: reading for package_id, reading in absences.items() if reading.absent}
    if not absent:
        return 0
    closed = 0
    open_items = (
        WorkflowItem.objects.filter(package_id__in=absent)
        .exclude(state__in=TERMINAL_STATES)
        .order_by("pk")
        .values_list("pk", "package_id")
    )
    for item_id, package_id in list(open_items):
        closed += close_for_absence(item_id=item_id, absence=absent[package_id], clock=clock).written
    return closed


def _open_remediation_items(
    *,
    run: PolicyRun,
    absent: frozenset[int],
    absences: Mapping[int, Absence],
    clock: Clock,
) -> int:
    """Open an item for every advisory this run matched to a package.

    Skips `unmapped` packages: every verdict on one is `unknown`, so a remediation
    item would be work that cannot be done until the identity queue is worked first.
    Skips absent packages too: a verdict on a package the organisation no longer
    runs is still a verdict, but it is not work.

    Args:
        run: The finished run.
        absent: The packages the inventory no longer listed at the run's cut-off.
        absences: What the inventory said about each package, for the epoch a
            re-listed package's key carries.
        clock: The clock the stamps are read from.

    Returns:
        How many items this opened.

    """
    verdicts = (
        PackageVulnerability.objects.filter(policy_run=run, vulnerability_status=ADVISORIES_MATCHED)
        .exclude(package__confidence=IdentityConfidence.UNMAPPED)
        .select_related("package", "vulnerability_finding")
    )
    return _open_evidence_items(
        ((verdict.package, verdict.vulnerability_finding) for verdict in verdicts),
        queue=Queue.REMEDIATION.value,
        absent=absent,
        absences=absences,
        clock=clock,
    )


def _open_compliance_items(
    *,
    run: PolicyRun,
    absent: frozenset[int],
    absences: Mapping[int, Absence],
    clock: Clock,
) -> int:
    """Open an item for every licence this run could not clear.

    Args:
        run: The finished run.
        absent: The packages the inventory no longer listed at the run's cut-off,
            which open no work on the terms `_open_remediation_items` states.
        absences: What the inventory said about each package, for the epoch a
            re-listed package's key carries.
        clock: The clock the stamps are read from.

    Returns:
        How many items this opened.

    """
    verdicts = (
        PackageLicense.objects.filter(policy_run=run, license_outcome__in=LICENCE_NEEDS_A_HUMAN)
        .exclude(package__confidence=IdentityConfidence.UNMAPPED)
        .select_related("package", "license_finding")
    )
    return _open_evidence_items(
        ((verdict.package, verdict.license_finding) for verdict in verdicts),
        queue=Queue.COMPLIANCE_REVIEW.value,
        absent=absent,
        absences=absences,
        clock=clock,
    )


def _open_evidence_items(
    findings: Iterable[tuple[Package, FindingKeyed | None]],
    *,
    queue: str,
    absent: frozenset[int],
    absences: Mapping[int, Absence],
    clock: Clock,
) -> int:
    """Open one item per evidence-backed finding, in one queue.

    Args:
        findings: The package and the finding row each verdict cites. A verdict
            with no finding is skipped: a determinate verdict names its finding by
            constraint, so this is unreachable through the passes -- and it is
            checked rather than assumed, because the alternative is an
            `AttributeError` inside a nightly run that then reports the whole run
            failed.
        queue: The queue to open in.
        absent: The packages to skip.
        absences: The readings, for the epoch fact.
        clock: The clock the stamps are read from.

    Returns:
        How many items this opened.

    """
    opened = 0
    for package, finding in findings:
        if package.pk in absent or finding is None:
            continue
        opened += open_item(
            evidence=finding,
            package=package,
            queue=queue,
            clock=clock,
            epoch=_epoch_facts(absences.get(package.pk)),
        ).created
    return opened
