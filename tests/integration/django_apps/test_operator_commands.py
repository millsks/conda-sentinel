"""The three operator commands against real tables (`CPM-OPERATE-S02`).

`ingest_inventory`, `dispatch_sweep` and `run_policy` are the management commands
that turn "enqueue the existing task" into something an operator runs by hand
and a deployment repository schedules. Each is one `.delay()` on a task that
already exists, so what these cases measure is the seam and not the task: that a
bad argument is refused **before** anything is enqueued and leaves no row; that a
good one enqueues with the task's own argument contract; that the ledger row the
task writes is reported back on two channels, and that it is *this call's* row
rather than the newest one on a ledger with history; and that when Celery is not
eager the command enqueues once and writes nothing itself.

**Eager under test.** `config/settings/test.py` sets `CELERY_TASK_ALWAYS_EAGER`,
so `.delay()` here runs the task inline and the happy paths end with a real
ledger row. The non-eager cases flip `CELERY_TASK_ALWAYS_EAGER` in Django
settings -- which the Celery app's configuration resolves live, and is therefore
what `.delay()` and the command both consult -- and patch `.delay` on the task
object so nothing reaches a broker.

**Nothing is collected.** A dispatch over an empty inventory selects nothing and
writes one `succeeded` row saying so; the one dispatch over a real selection
uses a fixture collector whose per-package task's `apply_async` is substituted,
on the terms `tests/integration/django_apps/test_sweep.py` sets; the ingestion
cases declare a recorded adapter; the policy run computes over evidence that is
already there. The session-wide network guard in `tests/conftest.py` would
refuse anything else.

Every test here rolls back: `@pytest.mark.django_db` wraps each in a transaction.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from io import StringIO
from typing import TYPE_CHECKING
from typing import Any
from typing import Final

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from kombu.exceptions import OperationalError
from structlog.testing import capture_logs

from conda_sentinel.collectors.management.commands import dispatch_sweep as sweep_command
from conda_sentinel.collectors.management.commands import ingest_inventory as ingest_command
from conda_sentinel.collectors.py314_verification import COLLECTOR_NAME as PY314_VERIFICATION_NAME
from conda_sentinel.collectors.sweep import COLLECTOR_KWARG
from conda_sentinel.collectors.sweep import PACKAGE_KWARG
from conda_sentinel.collectors.sweep import RESERVED_COLLECTOR_NAME
from conda_sentinel.collectors.sweep import SWEEP_TASK_NAME
from conda_sentinel.collectors.sweep import collection_task_name
from conda_sentinel.collectors.tasks import COLLECTOR_NAME as INVENTORY_NAME
from conda_sentinel.collectors.tasks import INGEST_TASK_NAME
from conda_sentinel.collectors.tasks import INVENTORY_SOURCE
from conda_sentinel.collectors.tasks import PACKAGE_NAME
from conda_sentinel.collectors.tasks import SOURCE_PACKAGE_KEY
from conda_sentinel.collectors.tasks import InventoryAdapterError
from conda_sentinel.collectors.tasks import collect_sweep
from conda_sentinel.collectors.tasks import declare_inventory_adapter
from conda_sentinel.collectors.tasks import ingest_inventory
from conda_sentinel.collectors.tasks import withdraw_inventory_adapter
from conda_sentinel.core.models import CollectionRun
from conda_sentinel.core.models import PolicyRun
from conda_sentinel.core.policy_run import PolicyRunError
from conda_sentinel.core.registry import registered_collectors
from conda_sentinel.core.runs import RunState
from conda_sentinel.core.tasks import POLICY_RUN_TASK_NAME
from conda_sentinel.core.tasks import run_policy
from conda_sentinel.policies.management.commands import run_policy as policy_command
from config.celery_app import app
from tests.clocks import FIXED_INSTANT
from tests.collectors import RecordedTransport
from tests.collectors import cleared_cache
from tests.collectors import fixture_evidence_model
from tests.collectors import recorded_payload
from tests.collectors import registered_collector
from tests.collectors import selectable_collector_class
from tests.passes import A_RECORDED_POLICY_VERSION
from tests.passes import THE_NEWEST_RECORDED_POLICY_VERSION

if TYPE_CHECKING:
    from collections.abc import Iterator

    from pytest_django.fixtures import SettingsWrapper

    from conda_sentinel.core.collection import Collector

#: The three commands, by the names `manage.py` dispatches them under.
INGEST: Final[str] = "ingest_inventory"
SWEEP: Final[str] = "dispatch_sweep"
POLICY: Final[str] = "run_policy"

#: Two swept collectors the by-name cases dispatch, in the order they are named.
A_SWEPT_COLLECTOR: Final[str] = "pypi_release"
ANOTHER_SWEPT_COLLECTOR: Final[str] = "feedstock"

#: A version the parameter file does not record.
AN_UNRECORDED_VERSION: Final[str] = "1999.01"

#: What a substituted `.delay` answers with, standing in for a broker's receipt.
A_TASK_ID: Final[str] = "00000000-0000-4000-8000-000000000042"

#: The fixture collector the real-selection case registers, and what it selects.
#: Distinct from every name `test_sweep.py` registers, because the registry and
#: Celery's task registry are both process-global.
A_FIXTURE_COLLECTOR: Final[str] = "operator_fixture_selecting"
A_SELECTION: Final[tuple[int, ...]] = (11, 22, 33)

#: A registered collector whose per-package task the registry-hold case removes.
A_TASKLESS_COLLECTOR: Final[str] = "operator_fixture_taskless"

#: One well-formed inventory record, for the ingestion happy path.
A_RECORD: Final[dict[str, object]] = {
    SOURCE_PACKAGE_KEY: "internal/numpy",
    PACKAGE_NAME: "numpy",
    "internal_component_count": 3,
    "internal_lob_count": 2,
}


class _Receipt:
    """What a patched `.delay` hands back: the id a broker would."""

    id = A_TASK_ID


class _RecordedDelay:
    """A `.delay` substitute that remembers every call and enqueues nothing.

    Args:
        refuse_at: The 1-based position of the call the broker refuses, or zero
            for a broker that takes everything.

    """

    def __init__(self, *, refuse_at: int = 0) -> None:
        self.calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
        self.refuse_at = refuse_at

    def __call__(self, *args: object, **kwargs: object) -> _Receipt:
        if len(self.calls) + 1 == self.refuse_at:
            message = "the broker is not there"
            raise OperationalError(message)
        self.calls.append((args, kwargs))
        return _Receipt()


class ATaskFailureError(RuntimeError):
    """What a substituted task body raises, so the refusal is about this case's reason."""


@pytest.fixture(autouse=True)
def _empty_cache() -> Iterator[None]:
    """Leave no rate-limit counter behind, in either direction.

    Yields:
        Nothing; the fixture is entirely its two side effects.

    """
    with cleared_cache():
        yield


@pytest.fixture
def declared_adapter() -> Iterator[RecordedTransport]:
    """Declare a recorded inventory adapter for the body of one case.

    Yields:
        The adapter, so a case can assert it was read.

    """
    adapter = RecordedTransport(payload=recorded_payload(source=INVENTORY_SOURCE, body=json.dumps([A_RECORD])))
    declare_inventory_adapter(adapter)
    try:
        yield adapter
    finally:
        withdraw_inventory_adapter()


@pytest.fixture
def not_eager(settings: SettingsWrapper) -> None:
    """Configure the process as a worker's settings would: `.delay()` publishes.

    Through Django settings, because that is where the Celery app reads its
    configuration from -- `app.conf.task_always_eager` resolves the `CELERY_`
    namespaced Django setting live, so this is what `.delay()` and the command
    both see. (An assignment on `app.conf` itself is shadowed by that lookup.)

    Args:
        settings: pytest-django's settings wrapper, restored after the case.

    """
    settings.CELERY_TASK_ALWAYS_EAGER = False
    assert app.conf.task_always_eager is False


@pytest.fixture
def eager_without_propagation(settings: SettingsWrapper) -> None:
    """Configure the process to run inline but hand a failure back as a result.

    Args:
        settings: pytest-django's settings wrapper, restored after the case.

    """
    settings.CELERY_TASK_EAGER_PROPAGATES = False
    assert app.conf.task_eager_propagates is False


def _run(command: str, *arguments: str) -> str:
    """Invoke one command and return what it wrote to its own stdout.

    Args:
        command: Which command.
        *arguments: Its command-line arguments.

    Returns:
        Everything the command wrote to stdout.

    """
    output = StringIO()
    call_command(command, *arguments, stdout=output)
    return output.getvalue()


def _dispatch_rows(collector: str | None = None) -> list[CollectionRun]:
    """Return the run-scoped ledger rows, oldest first.

    Args:
        collector: Narrow to one collector, or every one when `None`.

    Returns:
        The `CollectionRun` rows with no package, in primary-key order.

    """
    rows = CollectionRun.objects.filter(package__isnull=True).order_by("pk")
    if collector is not None:
        rows = rows.filter(collector=collector)
    return list(rows)


def _an_ended_collection_run(collector: str = INVENTORY_NAME, status: str = RunState.SUCCEEDED) -> CollectionRun:
    """Record an ended run-scoped collection run.

    What supplies a policy cut-off, and what a case seeds as "history" to prove
    the command reports its own row rather than the newest one.

    Args:
        collector: The collector the row is filed under.
        status: The state it ended in.

    Returns:
        The saved row.

    """
    return CollectionRun.objects.create(
        collector=collector,
        started_at=FIXED_INSTANT,
        finished_at=FIXED_INSTANT,
        status=status,
    )


def _events_named(events: list[dict[str, Any]], name: str) -> list[dict[str, Any]]:
    """Return the captured events with one name.

    Args:
        events: What `capture_logs` collected.
        name: The event name.

    Returns:
        The matching events, in order.

    """
    return [event for event in events if event["event"] == name]


@contextmanager
def _fixture_task_recorded(task_name: str) -> Iterator[list[int]]:
    """Register a fixture per-package task and record what a dispatch enqueues to it.

    On the terms `tests/integration/django_apps/test_sweep.py::_enqueue_recorded`
    sets: the registration is real and the removal is in a `finally`.

    Args:
        task_name: The derived per-package task name.

    Yields:
        The package keys the substituted `apply_async` accepted, in order.

    """
    assert task_name not in app.tasks, task_name
    accepted: list[int] = []

    @app.task(name=task_name, shared=False)
    def _fixture(*, package_id: int) -> None:
        """Do nothing; the registration is what a dispatch looks for."""

    def _apply_async(*, kwargs: dict[str, Any], expires: object = None) -> None:
        accepted.append(int(kwargs[PACKAGE_KWARG]))

    app.tasks[task_name].apply_async = _apply_async
    try:
        yield accepted
    finally:
        app.tasks.pop(task_name, None)


def _fixture_collector(name: str, selection: tuple[int, ...]) -> type[Collector]:
    """Return a swept fixture collector class under one name.

    Args:
        name: The name it registers under.
        selection: What it says it can be asked about.

    Returns:
        The class, not yet registered.

    """
    return selectable_collector_class(
        declared_model=fixture_evidence_model(),
        declared_name=name,
        selection=selection,
    )


# ---------------------------------------------------------------------------
# ingest_inventory
# ---------------------------------------------------------------------------


@pytest.mark.django_db
@pytest.mark.usefixtures("declared_adapter")
def test_ingest_runs_the_task_eagerly_and_reports_the_ledger_state() -> None:
    """The happy path: one `inventory` run on the ledger, its state and id on both channels."""
    with capture_logs() as events:
        output = _run(INGEST)

    rows = _dispatch_rows(INVENTORY_NAME)
    assert len(rows) == 1
    assert rows[0].status == RunState.SUCCEEDED
    assert f"run {rows[0].pk}" in output
    assert RunState.SUCCEEDED.value in output
    (ran,) = _events_named(events, ingest_command.INGEST_RAN_EVENT)
    assert ran["task"] == INGEST_TASK_NAME
    assert ran["force"] is False
    assert ran["run_id"] == rows[0].pk
    assert ran["state"] == RunState.SUCCEEDED.value


@pytest.mark.django_db
@pytest.mark.usefixtures("declared_adapter")
def test_ingest_reports_its_own_row_and_not_an_older_one() -> None:
    """A failed `inventory` run already on the ledger is history, not the answer."""
    older = _an_ended_collection_run(INVENTORY_NAME, status=RunState.FAILED)

    with capture_logs() as events:
        output = _run(INGEST)

    newest = _dispatch_rows(INVENTORY_NAME)[-1]
    assert newest.pk > older.pk
    assert f"run {newest.pk}" in output
    assert f"run {older.pk}:" not in output
    (ran,) = _events_named(events, ingest_command.INGEST_RAN_EVENT)
    assert ran["run_id"] == newest.pk


@pytest.mark.django_db
def test_ingest_with_no_declared_adapter_propagates_and_writes_no_row() -> None:
    """The task's own refusal escapes the command uncaught, and nothing is on the record."""
    with pytest.raises(InventoryAdapterError):
        _run(INGEST)

    assert _dispatch_rows(INVENTORY_NAME) == []


@pytest.mark.django_db
@pytest.mark.usefixtures("eager_without_propagation")
def test_ingest_eager_without_propagation_refuses_with_the_tasks_reason() -> None:
    """A result that carries the exception is a refusal, not a state to print."""
    with pytest.raises(CommandError, match="ran inline and failed") as refused:
        _run(INGEST)

    assert InventoryAdapterError.__name__ in str(refused.value)


@pytest.mark.django_db
@pytest.mark.usefixtures("not_eager")
def test_ingest_not_eager_enqueues_once_with_the_tasks_keyword_and_writes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Under a worker's settings the command hands the task off and reports the id.

    Args:
        monkeypatch: Substitutes the task's `.delay`.

    """
    delay = _RecordedDelay()
    monkeypatch.setattr(ingest_inventory, "delay", delay)

    with capture_logs() as events:
        output = _run(INGEST, "--force")

    assert delay.calls == [((), {"force": True})]
    assert A_TASK_ID in output
    assert "Coverage screen" in output
    assert CollectionRun.objects.count() == 0
    (enqueued,) = _events_named(events, ingest_command.INGEST_ENQUEUED_EVENT)
    assert enqueued["task_id"] == A_TASK_ID
    assert enqueued["force"] is True


# ---------------------------------------------------------------------------
# dispatch_sweep
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_sweep_of_one_collector_writes_one_dispatch_row_and_reports_it() -> None:
    """One name, one run-scoped row, and the line carries name, id, state and the offered count."""
    with capture_logs() as events:
        output = _run(SWEEP, A_SWEPT_COLLECTOR)

    rows = _dispatch_rows()
    assert [row.collector for row in rows] == [A_SWEPT_COLLECTOR]
    assert rows[0].status == RunState.SUCCEEDED
    assert f"{A_SWEPT_COLLECTOR}: run {rows[0].pk} {RunState.SUCCEEDED.value}, offered 0 package(s)" in output
    (ran,) = _events_named(events, sweep_command.SWEEP_RAN_EVENT)
    assert ran["task"] == SWEEP_TASK_NAME
    assert ran["collector"] == A_SWEPT_COLLECTOR
    assert ran["run_id"] == rows[0].pk
    assert ran["state"] == RunState.SUCCEEDED.value
    assert ran["offered"] == 0


@pytest.mark.django_db
def test_sweep_reports_the_count_off_the_row_the_dispatcher_wrote() -> None:
    """A real selection: the offered count is read off `sweep.py`'s own sentence, not a copy of it."""
    collector = _fixture_collector(A_FIXTURE_COLLECTOR, A_SELECTION)

    with (
        registered_collector(collector),
        _fixture_task_recorded(collection_task_name(A_FIXTURE_COLLECTOR)) as accepted,
        capture_logs() as events,
    ):
        output = _run(SWEEP, A_FIXTURE_COLLECTOR)

    assert accepted == list(A_SELECTION)
    (row,) = _dispatch_rows(A_FIXTURE_COLLECTOR)
    assert sweep_command.offered_count(row.detail) == len(A_SELECTION)
    assert f"offered {len(A_SELECTION)} package(s)" in output
    (ran,) = _events_named(events, sweep_command.SWEEP_RAN_EVENT)
    assert ran["offered"] == len(A_SELECTION)


@pytest.mark.django_db
def test_sweep_reports_its_own_row_and_not_an_older_one() -> None:
    """A dispatch row already on the ledger for the collector is history, not the answer."""
    older = _an_ended_collection_run(A_SWEPT_COLLECTOR, status=RunState.FAILED)

    with capture_logs() as events:
        output = _run(SWEEP, A_SWEPT_COLLECTOR)

    newest = _dispatch_rows(A_SWEPT_COLLECTOR)[-1]
    assert newest.pk > older.pk
    assert f"run {newest.pk} " in output
    assert f"run {older.pk} " not in output
    (ran,) = _events_named(events, sweep_command.SWEEP_RAN_EVENT)
    assert ran["run_id"] == newest.pk


@pytest.mark.django_db
def test_sweep_of_many_writes_one_row_each_in_argument_order() -> None:
    """Two names, two rows, in the order they were named rather than the registry's."""
    output = _run(SWEEP, ANOTHER_SWEPT_COLLECTOR, A_SWEPT_COLLECTOR)

    assert [row.collector for row in _dispatch_rows()] == [ANOTHER_SWEPT_COLLECTOR, A_SWEPT_COLLECTOR]
    assert output.index(ANOTHER_SWEPT_COLLECTOR) < output.index(A_SWEPT_COLLECTOR)


@pytest.mark.django_db
def test_sweep_strips_a_name_before_validating_it() -> None:
    """A name padded by the shell is the name, not a refusal."""
    _run(SWEEP, f"  {A_SWEPT_COLLECTOR} ")

    assert [row.collector for row in _dispatch_rows()] == [A_SWEPT_COLLECTOR]


@pytest.mark.django_db
def test_sweep_all_dispatches_every_swept_collector_and_never_the_two_unswept() -> None:
    """`--all` is the swept set, in registry order, and one line per collector."""
    output = _run(SWEEP, "--all")

    dispatched = [row.collector for row in _dispatch_rows()]
    assert dispatched == list(sweep_command.swept_collector_names())
    assert INVENTORY_NAME not in dispatched
    assert PY314_VERIFICATION_NAME not in dispatched
    assert RESERVED_COLLECTOR_NAME not in dispatched
    assert all(name in output for name in dispatched)
    assert output.count("offered") == len(dispatched)


@pytest.mark.django_db
def test_sweep_all_with_nothing_swept_is_refused_rather_than_an_empty_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A registry with no swept collector is a refusal, not exit 0 with nothing printed.

    Args:
        monkeypatch: Empties the swept set the command derives.

    """
    monkeypatch.setattr(sweep_command, "swept_collector_names", lambda: ())

    with pytest.raises(CommandError, match="found no collector to dispatch"):
        _run(SWEEP, "--all")

    assert CollectionRun.objects.count() == 0


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        ([RESERVED_COLLECTOR_NAME], "reserved"),
        (["nope"], "no collector is registered"),
        ([INVENTORY_NAME], "not swept"),
        ([PY314_VERIFICATION_NAME], "not swept"),
        ([""], "blank"),
        (["   "], "blank"),
        (["--all", A_SWEPT_COLLECTOR], "not both"),
        ([], "name at least one collector"),
        ([A_SWEPT_COLLECTOR, A_SWEPT_COLLECTOR], "named more than once"),
        ([A_SWEPT_COLLECTOR, f" {A_SWEPT_COLLECTOR}"], "named more than once"),
    ],
    ids=[
        "reserved",
        "unknown",
        "inventory",
        "py314_verification",
        "blank",
        "blank-after-strip",
        "all-and-names",
        "nothing",
        "repeated",
        "repeated-after-strip",
    ],
)
def test_sweep_refuses_before_any_row_and_lists_the_swept_names(arguments: list[str], expected: str) -> None:
    """Every refusal happens before the recorder opens, so a typo is not a run on the record.

    Args:
        arguments: What the operator typed.
        expected: What the refusal says.

    """
    with pytest.raises(CommandError, match=expected) as refused:
        _run(SWEEP, *arguments)

    assert CollectionRun.objects.count() == 0
    if "swept collectors are" in str(refused.value):
        for name in sweep_command.swept_collector_names():
            assert name in str(refused.value)


@pytest.mark.django_db
def test_sweep_refuses_a_collector_whose_task_celery_does_not_hold() -> None:
    """The dispatcher's fourth refusal, made before the recorder rather than inside it."""
    collector = _fixture_collector(A_TASKLESS_COLLECTOR, A_SELECTION)

    with registered_collector(collector), pytest.raises(CommandError, match="registry does not hold") as refused:
        _run(SWEEP, A_TASKLESS_COLLECTOR)

    assert collection_task_name(A_TASKLESS_COLLECTOR) in str(refused.value)
    assert CollectionRun.objects.count() == 0


@pytest.mark.django_db
@pytest.mark.usefixtures("not_eager")
def test_sweep_not_eager_enqueues_once_per_name_with_the_tasks_keyword_and_writes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Under a worker's settings each name is one enqueue and the line carries the id.

    Args:
        monkeypatch: Substitutes the task's `.delay`.

    """
    delay = _RecordedDelay()
    monkeypatch.setattr(collect_sweep, "delay", delay)

    with capture_logs() as events:
        output = _run(SWEEP, A_SWEPT_COLLECTOR, ANOTHER_SWEPT_COLLECTOR)

    assert delay.calls == [
        ((), {COLLECTOR_KWARG: A_SWEPT_COLLECTOR}),
        ((), {COLLECTOR_KWARG: ANOTHER_SWEPT_COLLECTOR}),
    ]
    assert output.count(A_TASK_ID) == len(delay.calls)
    assert CollectionRun.objects.count() == 0
    enqueued = _events_named(events, sweep_command.SWEEP_ENQUEUED_EVENT)
    assert [event["collector"] for event in enqueued] == [A_SWEPT_COLLECTOR, ANOTHER_SWEPT_COLLECTOR]


@pytest.mark.django_db
@pytest.mark.usefixtures("not_eager")
def test_sweep_reports_how_far_it_got_when_the_broker_refuses_mid_way(monkeypatch: pytest.MonkeyPatch) -> None:
    """The first name is enqueued and stays enqueued; the refusal says so by count and by name.

    Args:
        monkeypatch: Substitutes the task's `.delay` with a broker that refuses the second call.

    """
    delay = _RecordedDelay(refuse_at=2)
    monkeypatch.setattr(collect_sweep, "delay", delay)

    with pytest.raises(CommandError, match="the broker refused") as refused:
        _run(SWEEP, A_SWEPT_COLLECTOR, ANOTHER_SWEPT_COLLECTOR)

    assert delay.calls == [((), {COLLECTOR_KWARG: A_SWEPT_COLLECTOR})]
    assert "1 of 2 dispatch(es) had been enqueued" in str(refused.value)
    assert f"[{A_SWEPT_COLLECTOR!r}]" in str(refused.value)
    assert f"for {ANOTHER_SWEPT_COLLECTOR!r}" in str(refused.value)


@pytest.mark.django_db
@pytest.mark.usefixtures("eager_without_propagation")
def test_sweep_eager_without_propagation_refuses_with_the_tasks_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    """A result that carries the exception is a refusal, not a state to print.

    Args:
        monkeypatch: Makes the task body raise.

    """

    def _raise(**_kwargs: object) -> str:
        message = "the dispatch broke"
        raise ATaskFailureError(message)

    monkeypatch.setattr(collect_sweep, "run", _raise)

    with pytest.raises(CommandError, match="ran inline and failed") as refused:
        _run(SWEEP, A_SWEPT_COLLECTOR)

    assert "the dispatch broke" in str(refused.value)


@pytest.mark.django_db
def test_sweep_validation_makes_no_enqueue(monkeypatch: pytest.MonkeyPatch) -> None:
    """A refusal never reaches `.delay`, whatever else the arguments name.

    Args:
        monkeypatch: Substitutes the task's `.delay` with one that must not be called.

    """
    delay = _RecordedDelay()
    monkeypatch.setattr(collect_sweep, "delay", delay)

    with pytest.raises(CommandError):
        _run(SWEEP, A_SWEPT_COLLECTOR, "nope")

    assert delay.calls == []


# ---------------------------------------------------------------------------
# run_policy
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_policy_run_defaults_to_the_newest_recorded_version() -> None:
    """No `--version`: the newest recorded version by the pinned oracle, one `PolicyRun`."""
    _an_ended_collection_run()

    with capture_logs() as events:
        output = _run(POLICY)

    (run,) = PolicyRun.objects.all()
    assert run.policy_version == THE_NEWEST_RECORDED_POLICY_VERSION
    assert run.status == RunState.SUCCEEDED
    assert f"ran policy version {THE_NEWEST_RECORDED_POLICY_VERSION}" in output
    assert f"as run {run.pk}" in output
    assert "rollup row(s)" in output
    (ran,) = _events_named(events, policy_command.POLICY_RAN_EVENT)
    assert ran["task"] == POLICY_RUN_TASK_NAME
    assert ran["policy_version"] == THE_NEWEST_RECORDED_POLICY_VERSION
    assert ran["run_id"] == run.pk
    assert ran["state"] == RunState.SUCCEEDED.value


@pytest.mark.django_db
def test_policy_run_reports_its_own_row_and_not_an_older_one_at_the_same_version() -> None:
    """A failed run at the same version already on the ledger is history, not the answer."""
    cutoff = _an_ended_collection_run()
    older = PolicyRun.objects.create(
        policy_version=THE_NEWEST_RECORDED_POLICY_VERSION,
        evidence_cutoff=cutoff.finished_at,
        started_at=FIXED_INSTANT,
        finished_at=FIXED_INSTANT,
        status=RunState.FAILED,
    )

    with capture_logs() as events:
        output = _run(POLICY)

    newest = PolicyRun.objects.order_by("-pk").first()
    assert newest is not None
    assert newest.pk > older.pk
    assert f"as run {newest.pk}: {RunState.SUCCEEDED.value}" in output
    assert RunState.FAILED.value not in output
    (ran,) = _events_named(events, policy_command.POLICY_RAN_EVENT)
    assert ran["run_id"] == newest.pk
    assert ran["state"] == RunState.SUCCEEDED.value


@pytest.mark.django_db
def test_policy_run_at_a_named_recorded_version_runs_that_version() -> None:
    """`--version` names a recorded version and that is the one the run applies."""
    _an_ended_collection_run()
    assert A_RECORDED_POLICY_VERSION != THE_NEWEST_RECORDED_POLICY_VERSION, "pick a non-newest version"

    output = _run(POLICY, "--version", A_RECORDED_POLICY_VERSION)

    assert [run.policy_version for run in PolicyRun.objects.all()] == [A_RECORDED_POLICY_VERSION]
    assert A_RECORDED_POLICY_VERSION in output


@pytest.mark.django_db
def test_policy_run_refuses_an_unrecorded_version_listing_the_recorded_ones(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unrecorded version is refused before anything is enqueued, naming what is recorded.

    Args:
        monkeypatch: Substitutes the task's `.delay` with one that must not be called.

    """
    _an_ended_collection_run()
    delay = _RecordedDelay()
    monkeypatch.setattr(run_policy, "delay", delay)

    with pytest.raises(CommandError, match="not one the parameter file records") as refused:
        _run(POLICY, "--version", AN_UNRECORDED_VERSION)

    assert delay.calls == []
    assert PolicyRun.objects.count() == 0
    for version in policy_command.recorded_versions():
        assert version in str(refused.value)


@pytest.mark.django_db
def test_policy_run_with_no_ended_collection_propagates_and_writes_no_row() -> None:
    """The task's own refusal -- no cut-off to take -- escapes the command uncaught."""
    with pytest.raises(PolicyRunError):
        _run(POLICY)

    assert PolicyRun.objects.count() == 0


@pytest.mark.django_db
@pytest.mark.usefixtures("eager_without_propagation")
def test_policy_run_eager_without_propagation_refuses_with_the_tasks_reason() -> None:
    """A result that carries the exception is a refusal, not a count to read."""
    with pytest.raises(CommandError, match="ran inline and failed") as refused:
        _run(POLICY)

    assert PolicyRunError.__name__ in str(refused.value)


@pytest.mark.django_db
@pytest.mark.usefixtures("not_eager")
def test_policy_run_not_eager_enqueues_once_positionally_and_writes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Under a worker's settings the version goes as the task's one positional argument.

    Args:
        monkeypatch: Substitutes the task's `.delay`.

    """
    delay = _RecordedDelay()
    monkeypatch.setattr(run_policy, "delay", delay)

    with capture_logs() as events:
        output = _run(POLICY)

    assert delay.calls == [((THE_NEWEST_RECORDED_POLICY_VERSION,), {})]
    assert A_TASK_ID in output
    assert "rollup computed" in output
    assert PolicyRun.objects.count() == 0
    (enqueued,) = _events_named(events, policy_command.POLICY_ENQUEUED_EVENT)
    assert enqueued["policy_version"] == THE_NEWEST_RECORDED_POLICY_VERSION
    assert enqueued["task_id"] == A_TASK_ID


@pytest.mark.django_db
def test_the_swept_set_is_exactly_the_registrys_selectable_collectors() -> None:
    """`swept_collector_names()` applies the dispatcher's own predicate, in registry order."""
    expected = [collector.name for collector in registered_collectors() if collector.selectable_packages() is not None]

    assert list(sweep_command.swept_collector_names()) == expected
    assert INVENTORY_NAME not in expected
    assert PY314_VERIFICATION_NAME not in expected
