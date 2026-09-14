"""What is decidable about the operator commands without a run (`CPM-OPERATE-S02`).

`tests/integration/django_apps/test_operator_commands.py` holds every case that
needs a ledger row. This module holds the two declarations that must agree with
something else in the repository and the one pure function:

* **`--all` is the beat schedule's set.** `dispatch_sweep --all` derives the
  swept collectors from the registry; `CELERY_BEAT_SCHEDULE` names the collectors
  beat sweeps. Those are two spellings of one fact, and a collector added to the
  registry without a schedule entry -- or scheduled without being swept -- would
  otherwise be dispatched by hand and never by beat, or the reverse, with every
  other gate green.
* **The three admin processes.** `component.toml` declares `ingest`, `sweep` and
  `policy-run` and `pixi.toml` declares a root task for each, running the command
  this story wrote. `tests/unit/test_process_model.py` already audits every
  declared admin process for existence, placement, a real management command and
  no process type; what it cannot know is *which* three this story promised, so
  that is pinned here.
* **The count parser** reads the dispatch row's `detail`, and the wordings it
  reads are `collectors/sweep.py`'s. Each ending is measured against the sentence
  that module actually writes; the integration module proves it once more
  against a row the dispatcher wrote.
* **"Newest" is numeric.** `run_policy` derives its default version by ordering
  the recorded ones, and a lexicographic order puts `2026.09.10` before
  `2026.09.4`. The key is measured on exactly that shape, and the shipped file's
  newest version is derived here a second way -- `tomllib` and the same numeric
  ordering, with no code from the command in the path -- and compared with the
  oracle `tests/passes.py` pins by hand.
"""

from __future__ import annotations

import tomllib
from typing import Any
from typing import Final

import pytest
from django.conf import settings

from conda_sentinel.collectors.management.commands.dispatch_sweep import offered_count
from conda_sentinel.collectors.management.commands.dispatch_sweep import swept_collector_names
from conda_sentinel.collectors.py314_verification import COLLECTOR_NAME as PY314_VERIFICATION_NAME
from conda_sentinel.collectors.sweep import COLLECTOR_KWARG
from conda_sentinel.collectors.sweep import RESERVED_COLLECTOR_NAME
from conda_sentinel.collectors.sweep import SWEEP_TASK_NAME
from conda_sentinel.collectors.tasks import COLLECTOR_NAME as INVENTORY_NAME
from conda_sentinel.core.registry import registered_collectors
from conda_sentinel.policies.management.commands.run_policy import recorded_versions
from conda_sentinel.policies.management.commands.run_policy import version_key
from conda_sentinel.policies.parameters import VERSIONS_TABLE
from conda_sentinel.policies.parameters import newest_recorded_version
from conda_sentinel.policies.parameters import parameters_file
from config.component import load_component_declaration
from tests.passes import THE_NEWEST_RECORDED_POLICY_VERSION
from tests.pixi_manifest import load_manifest
from tests.pixi_manifest import task_command
from tests.pixi_manifest import task_env
from tests.pixi_manifest import tasks_named

#: The admin processes `CPM-OPERATE-S02` declared, the one `CPM-OPERATE-S03`
#: added, the nightly purge `CPM-OPERATE-S07` added and the digest
#: `CPM-OPERATE-S09` added, and the management command each root task must
#: invoke. The task name
#: is the `[[admin_processes]]` `task` and the `[tasks]` key; the command is what
#: `python manage.py` is given.
ADMIN_PROCESSES: Final[dict[str, str]] = {
    "ingest": "ingest_inventory",
    "sweep": "dispatch_sweep --all",
    "policy-run": "run_policy",
    "import-watchlist": "import_watchlist --replace",
    "prune-evidence": "prune_evidence",
    "digest": "compose_digest",
}

#: The one admin process that predates this story, so a case about "exactly
#: these" can say what the whole set is.
THE_INHERITED_ADMIN_PROCESS: Final[str] = "prune"

#: The manifest table an admin task must be declared in: the root one, which the
#: deployed environment carries.
ROOT_TASK_TABLE: Final[str] = "[tasks]"

#: The two collectors the dispatch never sweeps, by the names they register under.
THE_UNSWEPT: Final[frozenset[str]] = frozenset({INVENTORY_NAME, PY314_VERIFICATION_NAME})


def _scheduled_collectors() -> list[str]:
    """Return every collector `CELERY_BEAT_SCHEDULE` fires the dispatch for.

    Returns:
        The `collector` keyword of every entry naming the sweep task, in
        declaration order.

    """
    return [
        str(entry["kwargs"][COLLECTOR_KWARG])
        for entry in settings.CELERY_BEAT_SCHEDULE.values()
        if entry["task"] == SWEEP_TASK_NAME
    ]


# ---------------------------------------------------------------------------
# `--all` against the beat schedule
# ---------------------------------------------------------------------------


def test_all_dispatches_exactly_the_collectors_beat_sweeps() -> None:
    """The set `--all` derives and the set the schedule declares are one set."""
    assert set(swept_collector_names()) == set(_scheduled_collectors())


def test_all_never_names_the_two_unswept_collectors_or_the_reserved_name() -> None:
    """`inventory` and `py314_verification` are registered and never dispatched."""
    names = set(swept_collector_names())

    assert not names & THE_UNSWEPT
    assert RESERVED_COLLECTOR_NAME not in names
    assert {collector.name for collector in registered_collectors()} >= THE_UNSWEPT


def test_all_is_in_the_registrys_deterministic_order() -> None:
    """The order is the registry's (by name), not the schedule's and not insertion."""
    names = swept_collector_names()

    assert list(names) == sorted(names)
    assert len(names) == len(set(names))


# ---------------------------------------------------------------------------
# The count parser against the dispatcher's own wordings
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("detail", "expected"),
    [
        ("pypi_release enqueued 98 selected package(s) as cpm.collect.pypi_release.", 98),
        (
            (
                "pypi_release enqueued 3 of 5 selected package(s) as cpm.collect.pypi_release; 2 could not be "
                "enqueued (ABrokerRefusalError: no). The ones that were enqueued stay enqueued (CPM-FR-15); each "
                "refused package is on the sweep.package_refused log line."
            ),
            3,
        ),
        ("feedstock enqueued 2 package(s) as cpm.collect.feedstock and then stopped: SoftTimeLimitExceeded()", 2),
        (
            (
                "conda_package selected no packages, so nothing was enqueued as cpm.collect.conda_package. An empty "
                "selection is a collector that was asked and answered, not a sweep that failed."
            ),
            0,
        ),
        (
            (
                "kev could not enqueue any of 5 selected package(s) as cpm.collect.kev (ABrokerRefusalError: no). "
                "Nothing was dispatched; each refused package is on the sweep.package_refused log line."
            ),
            0,
        ),
        ("license enqueued nothing as cpm.collect.license and stopped: SoftTimeLimitExceeded()", 0),
        (
            (
                "pypi_release's previous dispatch has not finished, so this tick enqueued nothing rather than "
                "offering the inventory a second time. A sweep that cannot be drained inside its cadence grows a "
                "queue nobody can measure (CPM-NFR-1); the previous run's packages are still due."
            ),
            0,
        ),
        ("", 0),
    ],
    ids=[
        "succeeded",
        "partial-refused",
        "partial-interrupted",
        "empty-selection",
        "failed-refused",
        "failed-interrupted",
        "skipped-overlapping",
        "blank",
    ],
)
def test_the_offered_count_is_read_off_each_ending_the_dispatcher_writes(detail: str, expected: int) -> None:
    """Every `detail` `collectors/sweep.py` can write, and the count each carries.

    Args:
        detail: The sentence, as the dispatcher writes it.
        expected: The enqueued count it states, or zero for an ending that enqueued nothing.

    """
    assert offered_count(detail) == expected


# ---------------------------------------------------------------------------
# "Newest" is numeric, and the shipped file's newest is pinned independently
# ---------------------------------------------------------------------------


def test_the_version_key_orders_segments_numerically() -> None:
    """`2026.09.10` is newer than `2026.09.4`, and a bare `2026.09` is older than both."""
    versions = ["2026.09.4", "2026.09.10", "2026.09"]

    assert sorted(versions, key=version_key) == ["2026.09", "2026.09.4", "2026.09.10"]
    assert sorted(versions) != sorted(versions, key=version_key), (
        "the lexicographic order must differ, or this proves nothing"
    )


def test_the_version_key_compares_a_non_numeric_segment_without_raising() -> None:
    """A segment that is not all digits sorts after numbers and never raises `TypeError`."""
    assert sorted(["2026.09.rc1", "2026.09.4"], key=version_key) == ["2026.09.4", "2026.09.rc1"]


def test_the_newest_recorded_version_is_the_pinned_oracle() -> None:
    """The shipped file's newest version, derived without the command, is what `tests/passes.py` pins.

    Read with `tomllib` and ordered with the numeric key here, so the only thing
    shared with `recorded_versions()` is the ordering rule itself -- and the
    pinned literal is what catches a rule that quietly changed.
    """
    document = tomllib.loads(parameters_file().read_text(encoding="utf-8"))
    newest = max(document[VERSIONS_TABLE], key=version_key)

    assert newest == THE_NEWEST_RECORDED_POLICY_VERSION
    assert recorded_versions()[-1] == THE_NEWEST_RECORDED_POLICY_VERSION
    # The one rule both the command and the demo seeder derive "newest" through
    # since `CPM-OPERATE-S03`, measured against the same oracle.
    assert newest_recorded_version() == THE_NEWEST_RECORDED_POLICY_VERSION


# ---------------------------------------------------------------------------
# The three admin-process declarations
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def manifest() -> dict[str, Any]:
    """Return the parsed pixi manifest.

    Returns:
        The manifest, parsed from TOML.

    """
    return load_manifest()


def test_component_toml_declares_the_three_admin_processes_beside_prune() -> None:
    """`ingest`, `sweep` and `policy-run`, each scheduled by the deployment repository."""
    declared = {admin.name: admin for admin in load_component_declaration().admin_processes}

    assert set(declared) == {THE_INHERITED_ADMIN_PROCESS, *ADMIN_PROCESSES}
    for name in ADMIN_PROCESSES:
        assert declared[name].task == name, name
        assert declared[name].schedule == "deployment-repository", name


@pytest.mark.parametrize(("task", "command"), sorted(ADMIN_PROCESSES.items()), ids=sorted(ADMIN_PROCESSES))
def test_each_admin_task_runs_its_command_from_the_root_table_with_no_env(
    manifest: dict[str, Any], task: str, command: str
) -> None:
    """The root task exists, runs the promised command in `default`, and declares no `env`.

    No `env` at all, as `prune` declares none: no `COMPONENT_PROCESS`, because an
    admin process is not a serving process, and no `COMPONENT_RUNTIME`, because
    locality is the environment's to declare.

    Args:
        manifest: The parsed pixi manifest.
        task: The task name.
        command: What `python manage.py` must be given.

    """
    declared = tasks_named(manifest, task)
    tables = {table for table, _name, _definition in declared}

    assert ROOT_TASK_TABLE in tables, f"{task} is not declared in {ROOT_TASK_TABLE}: {sorted(tables)}"
    for _table, _name, definition in declared:
        assert task_command(definition) == f"python manage.py {command}"
        assert definition["default-environment"] == "default"
        assert task_env(definition) == {}
