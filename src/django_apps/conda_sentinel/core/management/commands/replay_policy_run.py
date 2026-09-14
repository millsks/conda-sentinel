"""`CPM-FR-22`'s replay, as an operation a compliance reviewer can run.

`core/policy_run.py` already takes the cut-off rather than choosing one, and every
pass is written to be deterministic at one -- so the *capability* to replay has been
there since the orchestration was built. What has not been there is a way for the
person who needs it to use it. `CPM-PRIORITY-S03`'s reviewer wants to "reproduce
exactly what the system concluded at a point in time", and until this command that
meant writing Python.

**It replays and then checks, and the checking is the point.** Re-running a version
at a cut-off and being told nothing is not reproduction -- it is a second run. So the
command executes the replay, compares it against the run it replayed with
`core/replay.py`, prints what differs, and **exits non-zero when anything does**. A
reviewer's answer is the exit status; the report is why.

**It requires no recollection**, which is `CPM-FR-22`'s second half and follows from
the cut-off rather than from anything here: every pass reads evidence
`observed_at <= cutoff`, and evidence is append-only (`CPM-AD-2`), so the rows the
original run read are still there and still say the same thing. Nothing is
re-fetched and no collector runs.

**Replayable exactly as far as the ledger reaches (`CPM-OPERATE-S07`).** The
nightly purge keeps every row a policy run still in the ledger read at its cut-off,
so `--of-run` of any run the ledger holds reproduces after any number of purges,
however old the run -- the run behind the current rollup is never purged, and an
older run is either still there or "no such run", as any unknown id is. A cut-off
stated by hand is admitted only when a run still in the ledger holds exactly that
cut-off: at any other instant the purge has been free to remove what a run there
would read, and the command refuses before running anything rather than replaying
against a thinner history and reporting the difference as a defect in a pass.

**It overwrites current health, and that is stated three times because it is the one
surprise.** `CPM-AD-11` gives `package_health` exactly one row per package and
`core/rollup.py` replaces it, so a replay of a quarter-old cut-off leaves the
current-health table showing what was true a quarter ago -- until the next scheduled
run puts it back. Every row carries `computed_at` and `evidence_cutoff` so the state
is visible rather than silent, but a reviewer running this against a production
database at 09:00 should know before they press return. Hence the confirmation, the
warning in the output, and the section in `docs/conda-sentinel/operations.md`.

**A management command rather than a task** (`AD-31`'s shape). A replay is something
a person decides to do and reads the output of; a Celery task would put the answer in
a result backend and the exit status nowhere. It declares no schedule for the same
reason -- nothing should replay history on a timer.

**On the `AD-` prefix.** A bare `AD-n` in this repository is an *inherited*
platform decision; a decision from this product's own architecture spine always
carries the `CPM-` prefix.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from typing import Any
from typing import Final

from django.core.management.base import BaseCommand
from django.core.management.base import CommandError
from django.utils.dateparse import parse_datetime

from conda_sentinel.core.clock import SystemClock
from conda_sentinel.core.models import PolicyRun
from conda_sentinel.core.policy_run import execute_policy_run
from conda_sentinel.core.replay import compare_runs

if TYPE_CHECKING:
    from argparse import ArgumentParser
    from datetime import datetime

__all__ = ["Command"]

#: What the command says before it does anything, because it is the one consequence
#: a reviewer would not predict.
ROLLUP_WARNING: Final[str] = (
    "This rewrites package_health: CPM-AD-11 gives it one row per package and a run replaces it, so current "
    "health will show what was true at the replayed cut-off until the next scheduled policy run puts it back. "
    "The rows carry computed_at and evidence_cutoff, so the state is visible -- but every read surface will "
    "show it."
)

#: What a reviewer types to proceed, and what the prompt asks for.
CONFIRMATION: Final[str] = "yes"

#: How many differences are printed before the rest are summarised. A replay that
#: differs on every package of a ten-thousand-package inventory produces twenty
#: thousand lines, and the first few are what a reviewer diagnoses from; the count
#: is what tells them how bad it is.
REPORTED_DIFFERENCES: Final[int] = 20


class Command(BaseCommand):
    """Re-run a stated policy version against a stated cut-off and report what changed."""

    help = "Replay a policy run at a stated version and evidence cut-off, and report whether it reproduced."

    def add_arguments(self, parser: ArgumentParser) -> None:
        """Declare the two ways of naming what to replay, and the confirmation.

        **Two forms rather than one, because a reviewer arrives with one of two
        things.** Usually a run they are auditing, so `--of-run` reads the version
        and the cut-off off that row -- which is also the form that cannot get them
        out of step. Sometimes a version and an instant from a report, so those can
        be stated directly.

        Args:
            parser: The command's argument parser.

        """
        parser.add_argument(
            "--of-run",
            type=int,
            metavar="ID",
            help="Replay the policy run with this id, at its own version and cut-off.",
        )
        parser.add_argument(
            "--policy-version",
            help="The policy version to apply. Requires --evidence-cutoff.",
        )
        parser.add_argument(
            "--evidence-cutoff",
            help="The instant to read evidence as of, as an ISO 8601 datetime with a timezone.",
        )
        parser.add_argument(
            "--no-input",
            action="store_true",
            help="Do not prompt before rewriting current health.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        """Replay one run, compare it with the original, and report.

        Args:
            *args: Unused; Django's signature.
            **options: The parsed arguments.

        Raises:
            CommandError: When the arguments do not name exactly one run to replay,
                when the named run does not exist, when the cut-off cannot be read
                as an aware instant, when a stated cut-off is one no run in the
                ledger holds, when the reviewer declines the confirmation, or when
                **the replay did not reproduce the original**. The last is the
                whole point: a non-zero exit is the answer.

        """
        clock = SystemClock()
        original, policy_version, evidence_cutoff = self._subject(options)

        self.stdout.write(f"Replaying policy version {policy_version!r} at evidence cut-off {evidence_cutoff}.")
        self.stdout.write(self.style.WARNING(ROLLUP_WARNING))
        if not options["no_input"] and input(f"Type '{CONFIRMATION}' to continue: ").strip() != CONFIRMATION:
            message = "the replay was not confirmed, so nothing was run and nothing was rewritten."
            raise CommandError(message)

        summary = execute_policy_run(
            policy_version=policy_version,
            clock=clock,
            evidence_cutoff=evidence_cutoff,
        )

        if original is None:
            self.stdout.write(
                self.style.SUCCESS(
                    f"Ran policy version {policy_version!r} at {evidence_cutoff} as run "
                    f"{summary.policy_run.pk}, writing {summary.rollup_rows} rollup row(s). No earlier run was "
                    f"named, so nothing was compared -- pass --of-run to check a replay against what it replays.",
                ),
            )
            return

        report = compare_runs(original, summary.policy_run)
        self._report(report)

    def _subject(self, options: dict[str, Any]) -> tuple[PolicyRun | None, str, datetime]:
        """Return the run being replayed, its version and its cut-off.

        Args:
            options: The parsed arguments.

        Returns:
            The original run where one was named, and the version and cut-off to
            replay at.

        Raises:
            CommandError: When the arguments do not name exactly one subject, when
                the named run does not exist, when a stated cut-off is not an
                aware ISO 8601 instant, or when no run in the ledger holds it.

        """
        of_run = options["of_run"]
        stated_version = options["policy_version"]
        stated_cutoff = options["evidence_cutoff"]

        if of_run is not None and (stated_version or stated_cutoff):
            message = "--of-run names a version and a cut-off already; do not also pass them."
            raise CommandError(message)
        if of_run is None and not (stated_version and stated_cutoff):
            message = "name what to replay: --of-run ID, or both --policy-version and --evidence-cutoff."
            raise CommandError(message)

        if of_run is not None:
            original = PolicyRun.objects.filter(pk=of_run).first()
            if original is None:
                message = f"no policy run has id {of_run}."
                raise CommandError(message)
            return original, original.policy_version, original.evidence_cutoff

        stated = _aware_instant(stated_cutoff)
        _require_a_run_at(stated)
        return None, stated_version, stated

    def _report(self, report: Any) -> None:
        """Print what the comparison found, and refuse when anything differs.

        Args:
            report: The `ReplayReport`.

        Raises:
            CommandError: When the replay did not reproduce the original.

        """
        if report.reproduced:
            self.stdout.write(
                self.style.SUCCESS(
                    f"Replay of run {report.original.pk} as run {report.replayed.pk} reproduced it exactly, "
                    f"over {report.compared_rows} row(s).",
                ),
            )
            return

        for difference in report.differences[:REPORTED_DIFFERENCES]:
            self.stdout.write(self.style.ERROR(difference.describe()))
        remaining = len(report.differences) - REPORTED_DIFFERENCES
        if remaining > 0:
            self.stdout.write(self.style.ERROR(f"... and {remaining} more."))
        message = (
            f"replay of run {report.original.pk} as run {report.replayed.pk} did NOT reproduce it: "
            f"{len(report.differences)} difference(s) over {report.compared_rows} row(s) compared. "
            f"CPM-FR-22 requires that re-running a stated version at a stated cut-off reproduces identical "
            f"results, so this is a defect in a pass, in the reviewed parameter file for this version, or in "
            f"evidence that was not append-only."
        )
        raise CommandError(message)


def _require_a_run_at(evidence_cutoff: datetime) -> None:
    """Refuse a stated cut-off that no run still in the ledger read at.

    The purge keeps what a retained run read at *its* cut-off and nothing else
    older than the retention, so an instant no run holds is one whose evidence
    may be thinner than it was.

    Args:
        evidence_cutoff: The stated instant.

    Raises:
        CommandError: When no `PolicyRun` row carries exactly this cut-off.

    """
    if PolicyRun.objects.filter(evidence_cutoff=evidence_cutoff).exists():
        return
    message = (
        f"no retained run read at this cut-off ({evidence_cutoff.isoformat()}); the nightly purge was free to "
        f"remove what a run there would read, so a replay at it would not be a reproduction (CPM-FR-22, "
        f"CPM-OPERATE-S07). Name a run with --of-run, or state the evidence_cutoff of a run still in the ledger."
    )
    raise CommandError(message)


def _aware_instant(stated: str) -> datetime:
    """Return a stated cut-off as an aware instant, or refuse it.

    Args:
        stated: The `--evidence-cutoff` argument.

    Returns:
        The parsed instant.

    Raises:
        CommandError: When it cannot be read as ISO 8601, or carries no timezone. A
            naive cut-off is refused here rather than deeper: every instant this
            product records is aware (`CPM-AD-26`), and a naive one compared against
            them silently reads a window nobody chose.

    """
    parsed = parse_datetime(stated)
    if parsed is None:
        message = f"--evidence-cutoff {stated!r} is not an ISO 8601 datetime."
        raise CommandError(message)
    if parsed.tzinfo is None or parsed.tzinfo.utcoffset(parsed) is None:
        message = (
            f"--evidence-cutoff {stated!r} carries no timezone. Every instant this product records is aware "
            f"(CPM-AD-26); a naive one would read a window nobody chose. Copy the evidence_cutoff off the run "
            f"you are replaying, or state an offset."
        )
        raise CommandError(message)
    return parsed
