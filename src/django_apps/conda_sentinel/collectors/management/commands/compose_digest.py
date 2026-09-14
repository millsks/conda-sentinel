"""The operator digest as a command an operator runs and a deployment schedules (`CPM-OPERATE-S09`).

`cpm.policy.digest` is fired daily by `CELERY_BEAT_SCHEDULE`'s `cpm-digest`
entry. This is the by-hand trigger beside it: `component.toml`'s `digest` admin
process (`pixi run digest`), which a deployment repository can run after a
first deploy or after a change to where the digest is delivered, and the
operator's `pixi run stack-run compose_digest` -- the acceptance criterion's
own invocation, after which `/digests/` shows the row.

**It enqueues; it does not compose.** `CPM-AD-9`, on `run_policy`'s exact
terms: the command calls `compose_operator_digest.delay()` and reports what
came back. On the `policy` queue under the stack's
`CELERY_TASK_ALWAYS_EAGER=0`; inline here under the default environment and
the suite. The flag chooses which report line to print and nothing else.

**The row it reports is the one this call wrote.** The task returns the row's
primary key, so under eager settings the report reads that row back and says
whether anything changed and what each declared channel answered. No delivery
happens in the caller: `collectors/digest.py` delivers from inside the task,
and a refused channel is a `failed` entry on the row rather than a non-zero
exit here -- the row is the record, and the command says so.

The command opens no transaction and catches nothing the task raises when
eager. Under a settings module that turned propagation off the eager result
*carries* the exception instead, and that is reported as a refusal rather than
read as a primary key.
"""

from __future__ import annotations

from typing import Any
from typing import Final

import structlog
from django.core.management.base import BaseCommand
from django.core.management.base import CommandError

from conda_sentinel.collectors.digest import DELIVERED
from conda_sentinel.collectors.models import OperatorDigest
from conda_sentinel.collectors.tasks import DIGEST_TASK_NAME
from conda_sentinel.collectors.tasks import compose_operator_digest
from conda_sentinel.core.operator_commands import WHERE_TO_WATCH_A_DIGEST
from conda_sentinel.core.operator_commands import runs_eagerly

__all__ = ["COMMAND_COMPOSED_EVENT", "DIGEST_ENQUEUED_EVENT", "UNRECORDED", "Command"]

logger: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

#: The two events, one per outcome the command can report. `COMMAND_COMPOSED_EVENT`
#: is the command's own line and not `collectors/digest.py`'s `digest.composed`,
#: which the task logs whoever fired it.
DIGEST_ENQUEUED_EVENT: Final[str] = "compose_digest.enqueued"
COMMAND_COMPOSED_EVENT: Final[str] = "compose_digest.composed"

#: What the report says for a row id the table does not hold.
UNRECORDED: Final[str] = "unrecorded"


class Command(BaseCommand):
    """Enqueue one operator digest for the day ending now."""

    help = "Admin process: enqueue the operator digest for the twenty-four hours ending now (cpm.policy.digest)."

    def handle(self, *args: Any, **options: Any) -> None:
        """Enqueue the digest and report where it went.

        Args:
            *args: Unused; Django's management interface passes none.
            **options: Unused; the command takes no option.

        Raises:
            CommandError: When the task ran eagerly and failed without
                propagating.

        """
        result = compose_operator_digest.delay()

        if not runs_eagerly():
            logger.info(DIGEST_ENQUEUED_EVENT, task=DIGEST_TASK_NAME, task_id=result.id)
            self.stdout.write(f"enqueued {DIGEST_TASK_NAME} as task {result.id}; {WHERE_TO_WATCH_A_DIGEST}")
            return

        if result.failed():
            message = f"{DIGEST_TASK_NAME} ran inline and failed: {result.result!r}"
            raise CommandError(message)

        digest_id = int(result.result)
        digest = OperatorDigest.objects.filter(pk=digest_id).first()
        state: str = UNRECORDED
        delivered = failed = 0
        if digest is not None:
            state = "changed" if digest.changed else "unchanged"
            delivered = sum(1 for entry in digest.deliveries if entry.get("state") == DELIVERED)
            failed = len(digest.deliveries) - delivered
        logger.info(
            COMMAND_COMPOSED_EVENT,
            task=DIGEST_TASK_NAME,
            task_id=result.id,
            digest_id=digest_id,
            state=state,
            delivered=delivered,
            failed=failed,
        )
        self.stdout.write(
            f"composed the digest inline (CELERY_TASK_ALWAYS_EAGER) as row {digest_id}: {state}, "
            f"{delivered} delivered, {failed} failed"
        )
