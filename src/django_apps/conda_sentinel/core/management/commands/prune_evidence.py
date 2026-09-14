"""The nightly purge as a command an operator runs and a deployment schedules (`CPM-OPERATE-S07`).

`core/retention.py` is the purge: the declared retention, the roster, the floor
rule and the one audited door. This is its front door -- `pixi run prune-evidence`
as `component.toml`'s `prune-evidence` admin process, which the deployment
repository schedules nightly; `pixi run stack-run prune_evidence --dry-run` by
hand on the local stack first, because a rehearsal writes the same run records
with the same counts and removes nothing.

**Two options and no more.** `--dry-run` counts and records; `--batch N` bounds
every `DELETE` (default `DEFAULT_BATCH_SIZE`), refused below one because an
unbounded batch is the one statement the purge exists not to issue, and above
`MAXIMUM_BATCH_SIZE` because each key is one bound parameter of the statement. The
retention itself is not an option: it is `CPM_EVIDENCE_RETENTION_DAYS`, a
declared setting refused at boot below one day, and a purge that could be told
a different window on the command line would be a purge whose window nobody
reviewed.

**Two channels, as every admin process here reports.** One structlog event per
table, emitted by the purge itself as each table's record is finalised, and one
human line per table plus a summary on `self.stdout`. The numbers on both are the
ones the run records carry.

**A table that fails does not stop the night.** A `DatabaseError` on one table
finalizes that table's record `failed`, carrying what was committed before it,
and the purge carries on to the next; the command then exits non-zero naming the
tables that failed, so the schedule sees the failure and the other tables were
still purged.

**Never overlapping `policy-run`.** The purge selects what no surviving run
cites, then deletes; a policy run that starts in between writes citations the
selection did not see, and every such row is a `PROTECT` refusal counted as
`protected` -- harmless, but a `partial` record every night. Schedule the purge
where no policy run is, and the purge's own ledger rows never bound a policy
run's cut-off (`core/policy_run.py` excludes them by name).

**`SystemClock()` and no other.** The command is the process boundary, so it is
where the clock is constructed (`CPM-AD-26`); the purge takes it and reads `now`
once. No `timezone.now()` anywhere on this path.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from typing import Any

from django.core.exceptions import ImproperlyConfigured
from django.core.management.base import BaseCommand
from django.core.management.base import CommandError

from conda_sentinel.core.clock import SystemClock
from conda_sentinel.core.retention import DEFAULT_BATCH_SIZE
from conda_sentinel.core.retention import DELETED_LEG
from conda_sentinel.core.retention import MAXIMUM_BATCH_SIZE
from conda_sentinel.core.retention import RETENTION_SETTING
from conda_sentinel.core.retention import purge_evidence
from conda_sentinel.core.retention import retention_days

if TYPE_CHECKING:
    from argparse import ArgumentParser

__all__ = ["Command"]


class Command(BaseCommand):
    """Remove evidence and run-ledger rows older than the declared retention, in bounded batches."""

    help = (
        "Admin process: purge evidence and run-ledger rows older than CPM_EVIDENCE_RETENTION_DAYS in bounded "
        "batches, recording one collection run per table (CPM-OPERATE-S07)."
    )

    def add_arguments(self, parser: ArgumentParser) -> None:
        """Declare the rehearsal switch and the batch bound.

        Args:
            parser: The parser Django hands every management command.

        """
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Record what would be purged, per table, and remove nothing.",
        )
        parser.add_argument(
            "--batch",
            type=int,
            default=DEFAULT_BATCH_SIZE,
            metavar="N",
            help=f"How many rows one DELETE may remove (default {DEFAULT_BATCH_SIZE}).",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        """Purge every table in order and report each.

        Args:
            *args: Unused; Django's management interface passes none.
            **options: The parsed options: `dry_run` and `batch`.

        Raises:
            CommandError: When `--batch` is below one or above the ceiling, or
                when the retention setting is unusable -- both before anything
                is touched; and after the purge, when any table's purge failed
                part-way, naming the tables.

        """
        batch = int(options["batch"])
        if batch < 1:
            message = f"--batch must be at least 1, not {batch}: the purge never issues an unbounded DELETE."
            raise CommandError(message)
        if batch > MAXIMUM_BATCH_SIZE:
            message = (
                f"--batch must be at most {MAXIMUM_BATCH_SIZE}, not {batch}: each key is one bound parameter of "
                f"the DELETE, and the ceiling keeps every batch inside what every backend accepts."
            )
            raise CommandError(message)
        dry_run = bool(options.get("dry_run"))
        try:
            days = retention_days()
        except ImproperlyConfigured as fault:
            message = f"{RETENTION_SETTING} is unusable: {fault}"
            raise CommandError(message) from fault

        results = purge_evidence(clock=SystemClock(), retention_days=days, batch_size=batch, dry_run=dry_run)

        verb = "would purge" if dry_run else "purged"
        for result in results:
            legs = ", ".join(f"{name} {count}" for name, count in result.removed if name != DELETED_LEG)
            breakdown = f" ({legs})" if legs else ""
            failure = f"; FAILED: {result.error}" if result.error is not None else ""
            line = (
                f"{result.table}: {verb} {result.deleted} row(s) older than {result.cutoff.isoformat()}{breakdown}; "
                f"kept by rule {result.kept_by_rule}, protected {result.protected}; run {result.run_id}{failure}"
            )
            self.stdout.write(self.style.ERROR(line) if result.error is not None else line)
        total = sum(result.deleted for result in results)
        protected = sum(result.protected for result in results)
        failed = [result.table for result in results if result.error is not None]
        summary = (
            f"{verb} {total} row(s) across {len(results)} table(s) at a retention of {days} day(s); "
            f"{protected} protected row(s) skipped."
        )
        if failed:
            message = (
                f"{summary} {len(failed)} table(s) failed part-way and carry a failed run record: {failed}. "
                f"The other tables were purged; the failed ones are retried by the next scheduled purge."
            )
            raise CommandError(message)
        self.stdout.write(self.style.SUCCESS(summary))
