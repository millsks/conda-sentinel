"""Fixtures scoped to unit tests.

Unit tests must not touch the database, the network or the filesystem; add
fixtures here only if they hold to that.

`fixed_clock` lives here rather than in `tests/conftest.py` because the whole
point of `CPM-AD-26` is that time is a *parameter*: an integration case that
needs a stopped clock constructs one and passes it, exactly as production code
does, and a shared fixture would only be a second way to reach the same one-line
construction. It is here at all so that every unit case that stops the clock
stops it at the same instant, which is what makes two failures comparable.

The instant itself is in `tests/clocks.py`, not here: a conftest is a plugin, and
a constant two modules share belongs in a helper module they can both import
without one of them importing a plugin.

`captured_sweep_events` and `start_dispatch` are here on the same terms: two unit
modules assert the start dispatch (`CPM-OPERATE-S04`) and the fake, the control
event and the reader they share live in `tests/start_dispatch.py`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import structlog

from conda_sentinel.collectors import sweep
from conda_sentinel.collectors.tasks import collect_sweep
from conda_sentinel.core.clock import FixedClock
from tests.clocks import FIXED_INSTANT
from tests.start_dispatch import SWEEP_CAPTURE_CONTROL
from tests.start_dispatch import RecordedApplyAsync

if TYPE_CHECKING:
    from collections.abc import Iterator

    from pytest_django.fixtures import SettingsWrapper


@pytest.fixture
def fixed_clock() -> FixedClock:
    """A clock stopped at `tests.clocks.FIXED_INSTANT`.

    Returns:
        A `FixedClock` every reader handed it observes the same instant from.

    """
    return FixedClock(instant=FIXED_INSTANT)


@pytest.fixture
def captured_sweep_events(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[dict[str, object]]]:
    """Capture what `collectors/sweep.py` logs, with the two guards the plain helper lacks.

    Shared by `tests/unit/test_celery_app.py` and `tests/unit/django_apps/test_sweep.py`,
    which both assert over the start dispatch's events (`CPM-OPERATE-S04`). The
    reasoning is `tests/unit/test_drain.py`'s and is not restated: the
    module-scope logger is rebound so `capture_logs` binds a fresh proxy inside
    its own processor chain -- a proxy another module bound earlier in the run
    keeps its processors and the capture sees nothing -- and a control event
    proves the capture is live before the case relies on what it holds.

    Args:
        monkeypatch: pytest's patcher, which restores the module's own logger.

    Yields:
        The captured events, in order, with the control event already cleared.

    """
    monkeypatch.setattr(sweep, "logger", structlog.get_logger(sweep.__name__))
    with structlog.testing.capture_logs() as captured:
        sweep.logger.warning(SWEEP_CAPTURE_CONTROL)
        assert [event["event"] for event in captured] == [SWEEP_CAPTURE_CONTROL], (
            "structlog.testing.capture_logs() cannot see collectors/sweep.py's logger, so every assertion "
            "over what it logged would be vacuous"
        )
        captured.clear()
        yield captured


@pytest.fixture
def start_dispatch(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, settings: SettingsWrapper
) -> RecordedApplyAsync:
    """Patch the dispatch task's `apply_async` and configure a worker's settings.

    Not eager, through Django settings, because `app.conf.task_always_eager`
    resolves the namespaced Django setting live and that is what the start
    dispatch's eager guard reads. Parametrize indirectly with a `frozenset` of
    collector names to make the broker unreachable for those calls; unparametrized,
    the broker takes everything.

    Args:
        request: The requesting case, read for an indirect `refuse=` parameter.
        monkeypatch: Substitutes the task's `apply_async`, restored afterwards.
        settings: pytest-django's settings wrapper, restored after the case.

    Returns:
        The recorder, whose `calls` are what reached the broker.

    """
    refuse = getattr(request, "param", frozenset())
    recorded = RecordedApplyAsync(refuse=frozenset(refuse))
    monkeypatch.setattr(collect_sweep, "apply_async", recorded)
    settings.CELERY_TASK_ALWAYS_EAGER = False
    return recorded
