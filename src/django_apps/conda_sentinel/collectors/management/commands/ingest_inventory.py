"""Inventory ingestion as a command an operator runs and a deployment schedules (`CPM-OPERATE-S02`).

`cpm.collect.inventory` has been registered, routed and runnable since
`CPM-EVIDENCE-S05`, and nothing fires it: no beat entry, no chained call. Until
this command the documented way to start one was a `stack-shell -c` snippet, and
a deployment repository had no process it could schedule for the re-read of the
watchlist. This is that process -- `component.toml`'s `ingest` admin process,
`pixi run ingest` -- and the operator's way to run one by hand
(`pixi run stack-run ingest_inventory`).

**It enqueues; it does not ingest.** `CPM-AD-9`: requests and commands enqueue,
tasks collect. The command calls the task object's `.delay()` with the task's own
keyword contract and reports what came back. Whether that ran the ingestion
inline or handed it to a worker is *settings'* decision -- the stack's `env` sets
`CELERY_TASK_ALWAYS_EAGER=0` and the task lands on the `collect` queue; the
default environment and the suite set it true and the task runs here. The flag is
read once, to choose which of the two report lines to print, and never to change
what the command does.

**It opens no transaction, writes no ledger row and catches nothing.** The row is
the recorder's, opened inside the task; an `InventoryAdapterError` for a
component with no declared adapter, or an `InventoryRecordError` for a document
that is not readable as records, escapes the eager run exactly as it escapes the
worker (`CELERY_TASK_EAGER_PROPAGATES`), so a failed run is a non-zero exit with
the reason rather than a line that says it ran. Under a settings module that
turned propagation off the eager result *carries* the exception instead of
raising it, and that is reported as a refusal rather than read as a state.

**The row it reports is the one this call wrote.** The ledger's highest
`inventory` run id is read before `.delay()`, and afterwards only a row above it
is reported; on a stack with history, or beside a concurrent worker, "the newest
row" could otherwise belong to somebody else's run. When no such row exists the
line says so rather than inventing one.

Reporting is on two channels, as `prune_expired_state` does: one structlog event
naming the task, its arguments and the task id or run state, and one human line on
stdout.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from typing import Any
from typing import Final

import structlog
from django.core.management.base import BaseCommand
from django.core.management.base import CommandError
from django.db.models import Max

from conda_sentinel.collectors.tasks import COLLECTOR_NAME
from conda_sentinel.collectors.tasks import INGEST_TASK_NAME
from conda_sentinel.collectors.tasks import ingest_inventory
from conda_sentinel.core.models import CollectionRun
from conda_sentinel.core.operator_commands import WHERE_TO_WATCH_A_COLLECTION
from conda_sentinel.core.operator_commands import runs_eagerly

if TYPE_CHECKING:
    from argparse import ArgumentParser

__all__ = ["INGEST_ENQUEUED_EVENT", "INGEST_RAN_EVENT", "UNRECORDED", "Command"]

logger: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

#: The two events, one per outcome the command can report. Two names rather than
#: one with an `eager` field, for the reason `prune_expired_state` keeps two: an
#: operator alerting on "an ingestion was enqueued" must not count one that ran
#: inline in a shell, and the reverse.
INGEST_ENQUEUED_EVENT: Final[str] = "ingest_inventory.enqueued"
INGEST_RAN_EVENT: Final[str] = "ingest_inventory.ran"

#: What the report says for a run id or a count the ledger does not hold. Said in
#: a word rather than as `0` or `None`, because a zero is a claim about the run and
#: this is a statement that the run left no row this command can see.
UNRECORDED: Final[str] = "unrecorded"


def _highest_inventory_run_id() -> int:
    """Return the highest `inventory` run-scoped ledger id, or zero when there is none.

    Returns:
        The watermark a run written by this call must sit above.

    """
    highest = CollectionRun.objects.filter(collector=COLLECTOR_NAME, package__isnull=True).aggregate(Max("pk"))
    return int(highest["pk__max"] or 0)


class Command(BaseCommand):
    """Enqueue one inventory ingestion through the declared adapter."""

    help = "Admin process: ingest the inventory once through the declared adapter (cpm.collect.inventory)."

    def add_arguments(self, parser: ArgumentParser) -> None:
        """Declare the one option the task takes.

        Args:
            parser: The parser Django hands every management command.

        """
        parser.add_argument(
            "--force",
            action="store_true",
            help="Bypass the observation window (inert for this collector today; carried through to the task).",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        """Enqueue the ingestion and report where it went.

        Args:
            *args: Unused; Django's management interface passes none.
            **options: The parsed options. `force` is the only one read here.

        Raises:
            CommandError: When the task ran eagerly and failed without
                propagating, so the result carries its exception.

        """
        force = bool(options.get("force"))
        watermark = _highest_inventory_run_id()
        result = ingest_inventory.delay(force=force)

        if not runs_eagerly():
            logger.info(INGEST_ENQUEUED_EVENT, task=INGEST_TASK_NAME, force=force, task_id=result.id)
            self.stdout.write(f"enqueued {INGEST_TASK_NAME} as task {result.id}; {WHERE_TO_WATCH_A_COLLECTION}")
            return

        if result.failed():
            message = f"{INGEST_TASK_NAME} ran inline and failed: {result.result!r}"
            raise CommandError(message)

        state = str(result.result)
        run = (
            CollectionRun.objects.filter(collector=COLLECTOR_NAME, package__isnull=True, pk__gt=watermark)
            .order_by("-pk")
            .first()
        )
        run_id: int | str = run.pk if run is not None else UNRECORDED
        logger.info(
            INGEST_RAN_EVENT,
            task=INGEST_TASK_NAME,
            force=force,
            task_id=result.id,
            run_id=run_id,
            state=state,
        )
        self.stdout.write(f"{INGEST_TASK_NAME} ran inline (CELERY_TASK_ALWAYS_EAGER) as run {run_id}: {state}")
