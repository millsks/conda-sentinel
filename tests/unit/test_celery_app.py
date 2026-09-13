"""Tests for the Celery application module."""

from __future__ import annotations

import inspect
import logging
import weakref
from typing import TYPE_CHECKING

import pytest
from celery.signals import beat_init
from celery.signals import before_task_publish
from celery.signals import worker_ready
from django_structlog.celery.receivers import CeleryReceiver
from django_structlog.celery.steps import DjangoStructLogInitStep

from conda_sentinel.collectors.sweep import COLLECTOR_KWARG
from conda_sentinel.collectors.sweep import SWEEP_ON_BEAT_START_SETTING
from conda_sentinel.collectors.sweep import SWEEP_START_DISPATCHED_EVENT
from conda_sentinel.collectors.sweep import SWEEP_START_REFUSED_EVENT
from conda_sentinel.collectors.sweep import SWEEP_START_SUMMARY_EVENT
from conda_sentinel.collectors.sweep import scheduled_dispatches
from conda_sentinel.collectors.tasks import collect_sweep
from conda_sentinel.core.queues import CELERY_TASK_ROUTES
from config.celery_app import app
from config.celery_app import config_loggers
from config.celery_app import dispatch_on_beat_start
from config.celery_app import install_drain_handler
from tests.start_dispatch import RecordedApplyAsync
from tests.start_dispatch import events_named

if TYPE_CHECKING:
    from celery.utils.dispatch import Signal
    from pytest_django.fixtures import SettingsWrapper


def _connected_receivers(signal: Signal) -> list[object]:
    """Return the live receivers connected to one celery signal.

    Celery's dispatcher stores receivers weakly by default, so the entries are
    `(lookup_key, weakref)` pairs and a dead reference resolves to `None`. It is
    walked rather than asserted through `has_listeners()`, which would be
    satisfied by any receiver at all.

    Args:
        signal: The celery signal whose receivers to resolve.

    Returns:
        Every receiver still reachable from the signal, in connection order.

    """
    receivers: list[object] = []
    for _key, receiver in signal.receivers:
        resolved = receiver() if isinstance(receiver, weakref.ReferenceType) else receiver
        if resolved is not None:
            receivers.append(resolved)
    return receivers


def test_celery_app_is_named_for_the_service():
    assert app.main == "django_service"


def test_config_loggers_applies_the_django_logging_config():
    """The setup_logging receiver hands Django's LOGGING dict to dictConfig.

    Celery otherwise installs its own logging config and Django's is ignored.
    """
    config_loggers()
    assert logging.getLogger("django").handlers


def test_the_drain_handler_is_connected_to_worker_ready():
    """AD-22: a worker installs the same handler the web process does.

    `worker_ready` and not `worker_process_init`: Celery installs its own
    `SIGTERM` handler in the main worker process, which is the process the
    platform signals, while the prefork children have their handlers reset and
    never receive it directly.

    Importing `config.celery_app` only *connects* the receiver -- nothing is
    installed until Celery fires the signal -- so this case has no effect on the
    test process's own handler.
    """
    assert install_drain_handler in _connected_receivers(worker_ready)


def test_the_worker_ready_receiver_installs_the_sigterm_handler(monkeypatch: pytest.MonkeyPatch):
    """The connection is only worth asserting if the receiver does the work.

    The installer is patched where it is defined rather than where it is used:
    the receiver imports it inside its own body -- `config/__init__.py` imports
    `config.celery_app`, so a module-level import would drag the health concern
    into every import of anything under `config` -- and a function-local import
    resolves against the defining module at call time.
    """
    calls: list[int] = []
    monkeypatch.setattr("config.health.drain.install_sigterm_handler", lambda: calls.append(1))

    install_drain_handler()

    assert calls == [1]


def test_the_django_structlog_bootstep_is_registered_on_the_worker():
    """FR-46: the step that binds an enqueueing request's context in the task.

    `DjangoStructLogInitStep` is what calls `connect_worker_signals()`, and
    `task_prerun` -- the receiver that reads `__django_structlog__` off the
    message and binds `request_id` for the task's own log lines -- is connected
    nowhere else. Without the step a worker logs tasks with no correlation at
    all, and nothing else in the tree would notice.
    """
    assert DjangoStructLogInitStep in app.steps["worker"]


def test_the_application_routes_are_the_table_core_declares():
    """`CPM-AD-20`: settings is the contribution site, and `app.conf` is a live view.

    `config_from_object("django.conf:settings", namespace="CELERY")` leaves
    `app.conf` a view over Django's settings rather than a copy of them, so
    `CELERY_TASK_ROUTES` in `config/settings/base.py` *is* `app.conf.task_routes`
    with no wiring in this module. That is worth an assertion rather than an
    assumption: the whole reason the route table can live in `core` and be
    installed from settings is that this indirection holds, and nothing else in
    the suite would notice if it stopped.

    Read as configuration rather than by publishing a task, and the distinction
    is the point. `CELERY_TASK_ALWAYS_EAGER` is on for the whole suite, and eager
    `apply_async` short-circuits to `apply()`: `task_routes` is never consulted
    and no message acquires a queue, so a `.delay()` assertion here would assert
    nothing at all.
    """
    assert app.conf.task_routes == CELERY_TASK_ROUTES


def test_the_three_queues_exist_because_celery_creates_them_on_demand():
    """The inherited default this story's routing quietly depends on.

    Nothing in this repository declares `task_queues`, so `collect`, `policy` and
    `verify` are not declared queues -- they exist because
    `task_create_missing_queues` is `True` by celery's own default and the
    producer declares each one the first time it publishes to it. That is a
    perfectly ordinary way to run celery, and it is a dependency rather than a
    decision anybody wrote down, which is why it is pinned here.

    Set it `False` in a deployed component -- as an operator reasonably might,
    to make queue creation explicit -- and every routed publish raises
    `QueueNotFound` instead: no collector runs, and the failure is at publish time
    in a worker, not in this gate. It would also break the router lookup
    `tests/unit/django_apps/test_task_routing_audit.py` reads, so the audit would
    start erroring rather than reporting.

    The day queues are declared explicitly is the day `task_queues` stops being
    `None`, and this case is where that arrives: it fails, and whoever declared
    them writes the `-Q` list and the `[[processes]]` split at the same time.
    """
    assert app.conf.task_queues is None
    assert app.conf.task_create_missing_queues is True


def test_the_publish_side_receiver_is_connected():
    """The observable consequence of `DJANGO_STRUCTLOG_CELERY_ENABLED = True`.

    Asserted as a consequence rather than by reading the setting back: the
    setting's value is already held by `tests/unit/test_settings.py`, and a
    reading of it here would pass just as happily against a django-structlog
    that had stopped acting on it. `DjangoStructLogConfig.ready()` calls
    `CeleryReceiver().connect_signals()`, which is what writes the enqueueing
    request's context into the published headers.
    """
    owners = {
        type(receiver.__self__) for receiver in _connected_receivers(before_task_publish) if inspect.ismethod(receiver)
    }

    assert CeleryReceiver in owners, f"no CeleryReceiver method is connected to before_task_publish: {owners}"


# ---------------------------------------------------------------------------
# CPM-OPERATE-S04 -- beat's first tick, brought forward.
# ---------------------------------------------------------------------------


def test_the_start_dispatch_receiver_is_connected_to_beat_init():
    """`CPM-OPERATE-S04`: the receiver is on the signal beat sends once, from `Service.start`.

    `beat_init` and not `beat_embedded_init`: the deployed beat and the stack's
    both run as their own process, and the embedded signal fires only for a
    beat started inside a worker with `-B`, which nothing here does.
    """
    assert dispatch_on_beat_start in _connected_receivers(beat_init)


def test_the_receiver_runs_when_celery_sends_the_signal(
    settings: SettingsWrapper, start_dispatch: RecordedApplyAsync, captured_sweep_events: list[dict[str, object]]
):
    """The connection is only worth asserting if Celery's own `send` reaches the receiver.

    Sent the way `Service.start` sends it -- `beat_init.send(sender=...)`, with
    the keyword Celery's dispatcher passes every receiver -- so the case proves
    the receiver's signature accepts what the dispatcher hands it, which a
    direct call cannot. The sender is an arbitrary object because the receiver
    reads nothing off it and the real one is a `Service` this case has no reason
    to build.
    """
    setattr(settings, SWEEP_ON_BEAT_START_SETTING, True)
    declared = scheduled_dispatches(settings.CELERY_BEAT_SCHEDULE)

    beat_init.send(sender=object())

    assert [call["kwargs"] for call in start_dispatch.calls] == [
        {COLLECTOR_KWARG: scheduled.collector} for scheduled in declared
    ]
    assert len(events_named(captured_sweep_events, SWEEP_START_SUMMARY_EVENT)) == 1


def test_the_receiver_enqueues_nothing_when_the_setting_is_off(
    settings: SettingsWrapper, monkeypatch: pytest.MonkeyPatch, captured_sweep_events: list[dict[str, object]]
):
    """Off is the shipped default, and off means the receiver returns before it reads the schedule.

    Asserted with eager Celery left as the suite has it: an `apply_async` under
    eager settings would run the dispatch inline, so a receiver that read the
    setting wrongly would leave a call on the recorder rather than a row in a
    database this case never opens.
    """
    recorded = RecordedApplyAsync()
    monkeypatch.setattr(collect_sweep, "apply_async", recorded)
    setattr(settings, SWEEP_ON_BEAT_START_SETTING, False)

    dispatch_on_beat_start()

    assert recorded.calls == []
    assert captured_sweep_events == []


@pytest.mark.parametrize("declared", ["1", "true", "on", 1, "0", "off"], ids=repr)
def test_the_receiver_reads_the_setting_as_on_only_when_it_is_the_boolean_true(
    declared: object,
    settings: SettingsWrapper,
    monkeypatch: pytest.MonkeyPatch,
    captured_sweep_events: list[dict[str, object]],
):
    """`is True`, not truthiness: a leaf module that assigned a string fails closed.

    `env.bool` in `base.py` answers a boolean, so anything else on the attribute
    is a settings module that assigned the variable's raw text -- and `"0"` or
    `"off"` is a non-empty string that truthiness would read as on. The string
    `"1"` is here too: it is on as an environment variable and off as an
    attribute, and the difference is exactly what the receiver must not blur.
    """
    recorded = RecordedApplyAsync()
    monkeypatch.setattr(collect_sweep, "apply_async", recorded)
    setattr(settings, SWEEP_ON_BEAT_START_SETTING, declared)

    dispatch_on_beat_start()

    assert recorded.calls == []
    assert captured_sweep_events == []


def test_the_receiver_returns_when_a_settings_module_dropped_the_setting(
    settings: SettingsWrapper, monkeypatch: pytest.MonkeyPatch, captured_sweep_events: list[dict[str, object]]
):
    """Absent reads off: the direction that fails closed, and a beat that starts regardless."""
    recorded = RecordedApplyAsync()
    monkeypatch.setattr(collect_sweep, "apply_async", recorded)
    delattr(settings, SWEEP_ON_BEAT_START_SETTING)

    dispatch_on_beat_start()

    assert recorded.calls == []
    assert captured_sweep_events == []


def test_the_receiver_logs_rather_than_raises_when_a_settings_module_dropped_the_schedule(
    settings: SettingsWrapper, start_dispatch: RecordedApplyAsync, captured_sweep_events: list[dict[str, object]]
):
    """The setting on and no schedule at all: a summary of nothing, never an `AttributeError` out of beat's start."""
    setattr(settings, SWEEP_ON_BEAT_START_SETTING, True)
    delattr(settings, "CELERY_BEAT_SCHEDULE")

    dispatch_on_beat_start()

    assert start_dispatch.calls == []
    summary = events_named(captured_sweep_events, SWEEP_START_SUMMARY_EVENT)
    assert len(summary) == 1
    assert (summary[0]["declared"], summary[0]["enqueued"]) == (0, 0)


def test_the_receiver_refuses_under_eager_celery_and_enqueues_nothing(
    settings: SettingsWrapper, monkeypatch: pytest.MonkeyPatch, captured_sweep_events: list[dict[str, object]]
):
    """On and eager: one refusal event, and nothing runs inside the scheduler.

    A bare `pixi run -e dev beat` outside the stack resolves
    `CELERY_TASK_ALWAYS_EAGER` to its local default of true, and an
    `apply_async` there would run nine collectors inline in the beat process
    before it ticked once. The refusal names that as the reason.
    """
    recorded = RecordedApplyAsync()
    monkeypatch.setattr(collect_sweep, "apply_async", recorded)
    setattr(settings, SWEEP_ON_BEAT_START_SETTING, True)
    assert app.conf.task_always_eager is True

    dispatch_on_beat_start()

    assert recorded.calls == []
    refused = events_named(captured_sweep_events, SWEEP_START_REFUSED_EVENT)
    assert len(refused) == 1
    assert "no worker" in str(refused[0]["detail"])
    assert events_named(captured_sweep_events, SWEEP_START_DISPATCHED_EVENT) == []


def test_the_receiver_enqueues_once_per_scheduled_entry_in_schedule_order_with_each_entrys_options(
    settings: SettingsWrapper, start_dispatch: RecordedApplyAsync, captured_sweep_events: list[dict[str, object]]
):
    """On and not eager: beat's first tick, entry by entry, with the schedule's own options.

    Reconciled against `scheduled_dispatches` over the live schedule rather than
    against a list written here: the nine names and the three offsets are
    `config/settings/base.py`'s declaration, and `tests/unit/test_settings.py`
    already pins those. What this asserts is that the receiver enqueues exactly
    that reading -- same order, same keyword, same options -- and logs one
    event per dispatch carrying the task id, then one summary.
    """
    setattr(settings, SWEEP_ON_BEAT_START_SETTING, True)
    assert app.conf.task_always_eager is False
    declared = scheduled_dispatches(settings.CELERY_BEAT_SCHEDULE)
    assert declared, "the schedule declares no dispatch, so this case would assert nothing"

    dispatch_on_beat_start()

    assert start_dispatch.calls == [
        {"kwargs": {COLLECTOR_KWARG: scheduled.collector}, **scheduled.options} for scheduled in declared
    ]
    dispatched = events_named(captured_sweep_events, SWEEP_START_DISPATCHED_EVENT)
    assert [(event["collector"], event["countdown"], event["task_id"]) for event in dispatched] == [
        (scheduled.collector, scheduled.countdown, f"task-{position}") for position, scheduled in enumerate(declared, 1)
    ]
    summary = events_named(captured_sweep_events, SWEEP_START_SUMMARY_EVENT)
    assert len(summary) == 1
    assert summary[0]["enqueued"] == len(declared)
    assert summary[0]["refused"] == 0
    assert summary[0]["not_offered"] == 0
