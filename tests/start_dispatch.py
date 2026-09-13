"""What the two unit modules that exercise the start dispatch share (`CPM-OPERATE-S04`).

`tests/unit/test_celery_app.py` asserts the `beat_init` receiver and
`tests/unit/django_apps/test_sweep.py` asserts `dispatch_scheduled_collectors_on_start`,
and both need the same three things: a recorded `apply_async` that enqueues
nothing and can refuse by collector name, a reader that picks captured events
out by name, and a control event that proves a capture is live. Each lived in
both modules first, which is the duplication `tests/celery_tasks.py` and
`tests/source_scan.py` were extracted to prevent -- two copies of a *fake* that
can drift look exactly like two passing tests.

The fixtures that use these live in `tests/unit/conftest.py`; the constants and
classes are here for the reason that file's docstring gives: a conftest is a
plugin, and a name two modules share belongs in a helper module they can both
import without one of them importing a plugin.

A helper module, not a collected one: `[tool.pytest.ini_options] python_files`
matches `test_*.py` and `tests.py`, so nothing here is collected.
"""

from __future__ import annotations

from typing import Final

from kombu.exceptions import OperationalError

__all__ = ["A_TASK_ID", "SWEEP_CAPTURE_CONTROL", "RecordedApplyAsync", "StartReceipt", "events_named"]

#: The id a broker would hand back for an enqueued dispatch.
A_TASK_ID: Final[str] = "00000000-0000-4000-8000-000000000042"

#: The control event the capture fixture emits to prove the capture is live.
#: Named once, for the reason `tests/conftest.py` names its identity-service
#: control: two spellings of a guard's own control value is how one of them
#: stops being checked.
SWEEP_CAPTURE_CONTROL: Final[str] = "sweep-capture-control"


class StartReceipt:
    """What a patched `apply_async` hands back: the id a broker would.

    Args:
        task_id: The id to carry.

    """

    def __init__(self, task_id: str = A_TASK_ID) -> None:
        self.id = task_id


class RecordedApplyAsync:
    """An `apply_async` substitute that remembers every call and enqueues nothing.

    The one shape both modules need. A call is recorded as its keyword arguments
    -- `kwargs` and whatever `options` the entry forwarded -- so a case asserts
    the exact call the task would have received. Ids are numbered per call so a
    case can tell the dispatches apart on the events.

    Args:
        refuse: The collectors the broker cannot be reached for, by name: a
            call naming one raises kombu's `OperationalError`, which is the
            exception a connection that could not be made after its retry
            window raises.

    """

    def __init__(self, *, refuse: frozenset[str] = frozenset()) -> None:
        self.calls: list[dict[str, object]] = []
        self.refuse = refuse

    def __call__(self, **kwargs: object) -> StartReceipt:
        keywords = kwargs.get("kwargs")
        collector = str(keywords.get("collector")) if isinstance(keywords, dict) else ""
        if collector in self.refuse:
            message = "the broker is not there"
            raise OperationalError(message)
        self.calls.append(kwargs)
        return StartReceipt(f"task-{len(self.calls)}")


def events_named(events: list[dict[str, object]], name: str) -> list[dict[str, object]]:
    """Return the captured events logged under one name, in order.

    Args:
        events: What `capture_logs` collected.
        name: The event name to keep.

    Returns:
        The matching events, in the order they were logged.

    """
    return [event for event in events if event["event"] == name]
