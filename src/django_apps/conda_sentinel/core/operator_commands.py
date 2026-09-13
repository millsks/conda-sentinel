"""What the three operator commands share (`CPM-OPERATE-S02`).

`ingest_inventory` and `dispatch_sweep` live in `collectors`, `run_policy` in
`policies`, and each does the same two things after its own validation: call an
existing task's `.delay()`, then print one line that depends on whether settings
made that call run the task inline or hand it to a worker. The answer to that
question and the words that follow a task id belong to neither application, so
they live here -- `core` reads only the Celery app's configuration and holds
two strings, and imports nothing downstream, which is what the layering audit
asks of it.

**The flag is read off the Celery app, not off Django settings.** The app is
configured from `django.conf:settings` with the `CELERY_` namespace, so the two
agree in every process this product runs; but `current_app.conf.task_always_eager`
is what `.delay()` itself consults, so reading it here means the report line
can never disagree with what the call just did.
"""

from __future__ import annotations

from typing import Final

from celery import current_app

__all__ = ["WHERE_TO_WATCH_A_COLLECTION", "WHERE_TO_WATCH_A_POLICY_RUN", "runs_eagerly"]

#: Where an enqueued collection's outcome becomes visible, said on the human
#: line because a task id alone sends an operator looking for a result backend.
WHERE_TO_WATCH_A_COLLECTION: Final[str] = "watch it on the Coverage screen, or in flower"

#: Where an enqueued policy run's outcome becomes visible. Not the Coverage
#: screen: that screen is collectors and their ledger, and a policy run never
#: appears on it. The home page's "rollup computed" stamp is what moves.
WHERE_TO_WATCH_A_POLICY_RUN: Final[str] = 'watch the home page\'s "rollup computed" stamp, or flower'


def runs_eagerly() -> bool:
    """Return whether the Celery app runs every task inline in the caller.

    Returns:
        True when `.delay()` executes the task in-process -- the default
        environment and the suite; False under the stack's and a deployment's
        settings, where `.delay()` publishes to the broker.

    """
    return bool(current_app.conf.task_always_eager)
