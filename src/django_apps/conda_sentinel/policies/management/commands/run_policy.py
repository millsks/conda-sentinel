"""A policy run as a command an operator runs and a deployment schedules (`CPM-OPERATE-S02`).

`cpm.policy.run` is registered, routed and runnable, and nothing fires it: no beat
entry, no chained call at the end of a sweep. So a deployed component collects
evidence on schedule and never computes a verdict from it until an operator
arranges the run -- and until this command, arranging it meant a `stack-shell -c`
snippet deriving the version by hand. This is `component.toml`'s `policy-run`
admin process (`pixi run policy-run`), which a deployment repository can
schedule after the sweeps have landed, and the operator's
`pixi run stack-run run_policy [--version V]`.

**It enqueues; it does not run the passes.** `CPM-AD-9`. The command calls
`run_policy.delay(version)` -- the task's own positional contract -- and reports
what came back. On the `policy` queue under the stack's
`CELERY_TASK_ALWAYS_EAGER=0`; inline here under the default environment and the
suite. The flag chooses which report line to print and nothing else.

**The version is validated here and never defaulted in `core`.** `core/tasks.py`'s
`run_policy` takes its version and forbids a default: a version invented by the
task would be `core` deciding which rules apply. This command lives in
`policies`, beside the parameter file, and derives the newest *recorded* version
from what the file records, so what is passed in is always a version the file
holds. A `--version` the file does not record is refused before anything is
enqueued, listing the ones it does: an unrecorded version fails every package
(`CPM-CURRENCY-S07`) and a run that did so would be on the ledger.

**"Newest" is numeric, segment by segment.** Versions are dotted strings and a
lexicographic sort puts `2026.09.10` before `2026.09.4`, so a scheduled
`policy-run` would silently apply stale rules the day a tenth revision was
recorded. `policies/parameters.py`'s `version_key` splits on `.` and compares
each all-digit segment as a number -- lifted there by `CPM-OPERATE-S03` so the
demo seeder derives "newest" through the same rule -- and `tests/passes.py` pins
the newest shipped version independently so the ordering is measured against
the file rather than against itself.

**The row it reports is the one this call wrote.** The ledger's highest
`PolicyRun` id is read before `.delay()` and only a row above it is reported --
on a stack with history, or beside a concurrent worker, "the newest row at this
version" could belong to another run. No such row is reported as unrecorded.

The command opens no transaction, writes no ledger row and catches nothing the
task raises when eager: a `PolicyRunError` for a ledger with no ended collection
to take a cut-off from escapes with its reason, as it would from the worker.
Under a settings module that turned propagation off the eager result *carries*
the exception instead, and that is reported as a refusal rather than read as a
count.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from typing import Any
from typing import Final

import structlog
from django.core.management.base import BaseCommand
from django.core.management.base import CommandError
from django.db.models import Max

from conda_sentinel.core.models import PolicyRun
from conda_sentinel.core.operator_commands import WHERE_TO_WATCH_A_POLICY_RUN
from conda_sentinel.core.operator_commands import runs_eagerly
from conda_sentinel.core.tasks import POLICY_RUN_TASK_NAME
from conda_sentinel.core.tasks import run_policy
from conda_sentinel.policies.parameters import VERSION_SEPARATOR
from conda_sentinel.policies.parameters import newest_recorded_version
from conda_sentinel.policies.parameters import recorded_versions
from conda_sentinel.policies.parameters import version_key

if TYPE_CHECKING:
    from argparse import ArgumentParser

    from django.core.management.base import CommandParser

__all__ = [
    "POLICY_ENQUEUED_EVENT",
    "POLICY_RAN_EVENT",
    "UNRECORDED",
    "VERSION_SEPARATOR",
    "Command",
    "recorded_versions",
    "version_key",
]

logger: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

#: The two events, one per outcome the command can report.
POLICY_ENQUEUED_EVENT: Final[str] = "run_policy.enqueued"
POLICY_RAN_EVENT: Final[str] = "run_policy.ran"

#: What the report says for a run id the ledger does not hold.
UNRECORDED: Final[str] = "unrecorded"

#: The option the version is stated under. Django gives every command a
#: `--version` that prints Django's own; this command's `--version` is the policy
#: version, which is the word an operator reaches for, so `create_parser` below
#: resolves the collision in this command's favour.
VERSION_OPTION: Final[str] = "--version"

#: `VERSION_SEPARATOR`, `version_key` and `recorded_versions` are declared in
#: `policies/parameters.py` since `CPM-OPERATE-S03` and imported back here; they
#: stay in `__all__` because this command is where an operator reading the
#: `--version` refusal goes looking for the ordering rule.


def _highest_policy_run_id() -> int:
    """Return the highest `PolicyRun` id, or zero when the ledger holds none.

    Returns:
        The watermark a run written by this call must sit above.

    """
    return int(PolicyRun.objects.aggregate(Max("pk"))["pk__max"] or 0)


class Command(BaseCommand):
    """Enqueue one policy run at a recorded version, the newest by default."""

    help = "Admin process: enqueue a policy run at a recorded policy version, the newest by default (cpm.policy.run)."

    def create_parser(self, prog_name: str, subcommand: str, **kwargs: Any) -> CommandParser:
        """Build the parser with this command's `--version` replacing Django's.

        `BaseCommand` adds `--version` (Django's own version string) to every
        parser before `add_arguments` runs, and argparse refuses a second
        definition of the same option. `conflict_handler="resolve"` lets the later
        definition win, which is the one below.

        Args:
            prog_name: The program name Django passes.
            subcommand: This command's name.
            **kwargs: Further parser options Django passes through.

        Returns:
            The parser, with `--version` meaning the policy version.

        """
        return super().create_parser(prog_name, subcommand, conflict_handler="resolve", **kwargs)

    def add_arguments(self, parser: ArgumentParser) -> None:
        """Declare the one option: the policy version to apply.

        Args:
            parser: The parser Django hands every management command.

        """
        parser.add_argument(
            VERSION_OPTION,
            dest="policy_version",
            metavar="VERSION",
            help="A policy version the parameter file records. Defaults to the newest recorded one.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        """Resolve the version, enqueue the run, and report where it went.

        Args:
            *args: Unused; Django's management interface passes none.
            **options: The parsed options. `policy_version` is the only one read.

        Raises:
            CommandError: When the stated version is one the parameter file does
                not record (raised before anything is enqueued), or when the task
                ran eagerly and failed without propagating.

        """
        version = self._version(options.get("policy_version"))
        watermark = _highest_policy_run_id()
        result = run_policy.delay(version)

        if not runs_eagerly():
            logger.info(POLICY_ENQUEUED_EVENT, task=POLICY_RUN_TASK_NAME, policy_version=version, task_id=result.id)
            self.stdout.write(
                f"enqueued {POLICY_RUN_TASK_NAME} at policy version {version} as task {result.id}; "
                f"{WHERE_TO_WATCH_A_POLICY_RUN}"
            )
            return

        if result.failed():
            message = f"{POLICY_RUN_TASK_NAME} at policy version {version} ran inline and failed: {result.result!r}"
            raise CommandError(message)

        rollup_rows = int(result.result)
        run = PolicyRun.objects.filter(policy_version=version, pk__gt=watermark).order_by("-pk").first()
        run_id: int | str = run.pk if run is not None else UNRECORDED
        state = str(run.status) if run is not None else UNRECORDED
        logger.info(
            POLICY_RAN_EVENT,
            task=POLICY_RUN_TASK_NAME,
            policy_version=version,
            task_id=result.id,
            run_id=run_id,
            state=state,
            rollup_rows=rollup_rows,
        )
        self.stdout.write(
            f"ran policy version {version} inline (CELERY_TASK_ALWAYS_EAGER) as run {run_id}: {state}, "
            f"{rollup_rows} rollup row(s)"
        )

    @staticmethod
    def _version(stated: str | None) -> str:
        """Return the version to run at: the stated one if recorded, else the newest.

        Args:
            stated: The `--version` argument, or `None`.

        Returns:
            A version the parameter file records.

        Raises:
            CommandError: When the stated version is not recorded.

        """
        if stated is None:
            return newest_recorded_version()
        recorded = recorded_versions()
        if stated not in recorded:
            message = (
                f"policy version {stated!r} is not one the parameter file records, so a run at it would fail "
                f"every package (CPM-CURRENCY-S07). The recorded versions are {recorded}; omit {VERSION_OPTION} "
                f"for the newest, {recorded[-1]!r}."
            )
            raise CommandError(message)
        return stated
