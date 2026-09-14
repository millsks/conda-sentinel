"""Tests for the gate contract: one task, one invocation, one place it runs.

AD-18 requires that a single workflow invokes `pixi run ci`, and that the gate
runs pre-commit, build, check, lint and cov in that order. These tests read the
manifests rather than executing the gate, so they are unit tests: no I/O beyond
reading repository files, no network, no database.
"""

from __future__ import annotations

import re
import tomllib
from fnmatch import fnmatch
from pathlib import Path
from typing import Any
from urllib.parse import unquote
from urllib.parse import urlsplit

import pytest
import yaml

Workflows = dict[str, dict[str, Any]]

REPO_ROOT = Path(__file__).resolve().parents[2]
PIXI_MANIFEST = REPO_ROOT / "pixi.toml"
PYPROJECT = REPO_ROOT / "pyproject.toml"
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"
TESTS_ROOT = REPO_ROOT / "tests"

# R-1's django-storages fitness spike (Story 1.8). It runs in the
# `spike-storage` environment, which the gate's `dev` environment is not, so it
# must be reachable by its own task and unreachable from `pixi run ci`.
#
# The mechanism is naming, not a pytest flag. `test-cov` runs `pytest tests/`,
# and the two flags that would exclude a subtree from it -- `-m "not spike"` and
# `--ignore=tests/spikes` -- are both banned on the floor-carrying task by
# tests/unit/test_coverage_policy.py, because narrowing what the floor measures
# is how the floor stops being a floor. So the spike lives in a module whose
# name `[tool.pytest.ini_options] python_files` does not match; pytest collects
# such a file only when it is named on the command line, which the spike task
# does and the gate never does.
SPIKE_TASK = "spike-storage"
SPIKE_ENVIRONMENT = "spike-storage"
SPIKE_DIRECTORY = REPO_ROOT / "tests" / "spikes"
SPIKE_MODULE_PREFIX = "spike_"

# The five gate steps in the order AD-18 fixes. These are this repository's task
# identifiers, which differ from the global standard's fmt/check/cov names; the
# AD names the steps, not the identifiers, and renaming them would break
# .pre-commit-config.yaml, release.yml and sonarqube.yml in the same change.
GATE_SEQUENCE = ["precommit", "build", "typecheck", "lint", "test-cov"]

# The task that *is* the gate, and the invariant that keeps `pixi run ci`
# unambiguous. pixi rejects `default-environment` on a task declaring only
# `depends-on`, so `ci` cannot pin an environment the way every other task does;
# the only thing that can keep it unambiguous is the feature declaring it
# belonging to exactly one `[environments]` entry. That is what the
# dependency-free `gate` feature is for, and until now it was argued for in prose
# and asserted nowhere -- while Epic 8's six-environment matrix is exactly the
# change that would reintroduce `the task 'ci' is ambiguous`.
GATE_TASK = "ci"

# Gate steps that must not be invoked by any workflow other than the gate job.
# `build` also has its own narrower check for cron specifically
# (test_no_scheduled_workflow_invokes_build), which AC #3 asks for by name;
# it is included here too so the exclusivity guarantee matches AC #5's full
# intent -- "no step exists only in CI or only locally" -- rather than only
# the schedule-triggered case.
GATE_ONLY_TASKS = ["precommit", "build", "test-cov", "lint", "typecheck"]

THREE_OS_RUNNERS = {"ubuntu-latest", "windows-latest", "macos-latest"}

# FR-32: the gate runs against the database the immovable core names.
#
# Only the family is asserted here, not the tag. Nothing in the repository can
# fix the server major version: `libpq` in pixi.toml is the *client* library and
# a libpq 18 client connects happily to an older server, so a `libpq` pin is not
# a server pin and must not be described as one. The tag in ci.yml is a choice
# made to match that client, and it is the workflow's comment -- not a test --
# that carries the reason.
#
# Matched on the image's repository segment rather than a `postgres:` prefix so
# that a registry-qualified mirror is not rejected for being mirrored. Pulling
# `postgres:18` from a mirror is the ordinary remedy for Docker Hub's
# unauthenticated pull limit on shared runners, and nothing about FR-32 cares
# where the image came from.
POSTGRES_IMAGE_NAMES = frozenset({"postgres", "postgresql", "postgis", "pgvector", "timescaledb"})

# Image families that constitute "a database service" for the exclusivity checks
# below. Broader than POSTGRES_IMAGE_NAMES on purpose: the reason the matrix may
# not have a database is that GitHub Actions `services:` containers are
# Linux-only, and that reason does not care which database it is.
DATABASE_IMAGE_NAMES = POSTGRES_IMAGE_NAMES | frozenset({"mysql", "mariadb", "cockroachdb", "mssql"})

# URL schemes that select PostgreSQL, as `urlsplit().scheme` reports them.
# `django-environ` accepts several spellings, and `config/settings/base.py:57`
# branches on the variable being truthy, so an empty or sqlite URL would revert
# the whole gate to the substitution while every other assertion here passed.
POSTGRES_URL_SCHEMES = frozenset({"postgres", "postgresql", "psql", "pgsql", "postgis"})

# The variables that move a job off the sqlite substitution. There are two, not
# one: `config/settings/base.py:57-69` selects PostgreSQL from `DATABASE_URL`
# *or* from `POSTGRES_DB` alone, and this suite treats both branches as live
# (tests/unit/test_database_selection.py pins each). A guard that knew only
# about `DATABASE_URL` would let the second branch put a database on a job that
# must not have one -- and, worse, would let the sqlite leg below stop being a
# sqlite leg while still counting as one.
DATABASE_SELECTOR_VARS = frozenset({"DATABASE_URL", "POSTGRES_DB"})

# CPM-EVIDENCE-S08 AC 4: the gate runs the shared-allowance proof against a real
# Redis. Only the family is asserted, not the tag, for the reason the PostgreSQL
# block above gives -- and matched on the repository segment so a mirrored image
# is not rejected for being mirrored.
REDIS_IMAGE_NAMES = frozenset({"redis", "valkey", "redis-stack", "redis-stack-server"})

# URL schemes that select Redis over TCP, as `urlsplit().scheme` reports them.
# `config/settings/test.py` branches on the variable being truthy, so an empty
# value would revert the gate to the LocMem substitution while every other
# assertion here passed.
#
# `unix://` is deliberately absent although `django-redis` accepts it: a socket
# URL has no hostname and no port, so the two assertions beside this one --
# `hostname == "localhost"` and a port matching the service's published one --
# would both be comparing against `None`, and a URL that named no service at all
# would pass every one of them. A service container is reached over TCP from the
# runner and cannot be reached over a socket, so admitting the scheme would
# widen what passes and never what works.
REDIS_URL_SCHEMES = frozenset({"redis", "rediss", "valkey"})

# The variable that moves the suite off the in-process cache substitution.
# One variable, read in one place (`config/settings/test.py`), and it both
# selects the backend and arms the case that needs it.
REDIS_URL_VARIABLE = "CPM_TEST_REDIS_URL"

# The port a Redis service is reached on inside the runner's network.
REDIS_PORT = "6379"

# The task that reproduces that service locally, in the shape `gate-postgres`
# already has. Neither is a gate step: AD-18 keeps `pixi run ci` the one
# sequence CI runs, and neither script's container is declared by a job.
REDIS_GATE_TASK = "gate-redis"

# CPM-OPERATE-S10's scale spike: a second `spike_*.py` module, run by a task
# whose body is a script on `gate-postgres`'s terms (a container, a readiness
# loop, `docker rm -f` on exit). It differs from R-1's spike in every constant
# above but the directory and the prefix -- it runs in `dev`, not in an
# environment of its own, because it needs nothing the runtime set lacks -- so
# it is admitted by its own test rather than by widening `SPIKE_*` into a
# roster. What keeps it out of the gate is what keeps R-1's out: its name.
SCALE_SPIKE_TASK = "spike-scale"
SCALE_SPIKE_MODULE = SPIKE_DIRECTORY / "spike_evidence_scale.py"


@pytest.fixture(scope="module")
def manifest() -> dict[str, Any]:
    """Return the parsed pixi manifest."""
    with PIXI_MANIFEST.open("rb") as handle:
        parsed: dict[str, Any] = tomllib.load(handle)
    return parsed


@pytest.fixture(scope="module")
def workflows() -> Workflows:
    """Return every parsed workflow file, keyed by filename."""
    parsed: dict[str, dict[str, Any]] = {}
    paths = sorted(WORKFLOW_DIR.glob("*.yml")) + sorted(WORKFLOW_DIR.glob("*.yaml"))
    for path in paths:
        with path.open(encoding="utf-8") as handle:
            parsed[path.name] = yaml.safe_load(handle)
    return parsed


def _all_tasks(manifest: dict[str, Any]) -> dict[str, Any]:
    """Return every task definition, whether it sits in [tasks] or a feature."""
    tasks: dict[str, Any] = dict(manifest.get("tasks", {}))
    for feature in manifest.get("feature", {}).values():
        tasks.update(feature.get("tasks", {}))
    return tasks


def _feature_declaring(manifest: dict[str, Any], task: str) -> str | None:
    """Return the name of the feature whose `tasks` table declares a task.

    Args:
        manifest: The parsed pixi manifest.
        task: The task name to locate.

    Returns:
        The feature name, or None when the task is declared in the top-level
        `[tasks]` table (the default feature) or not at all.
    """
    for name, feature in manifest.get("feature", {}).items():
        if task in feature.get("tasks", {}):
            return name
    return None


def _environments_carrying(manifest: dict[str, Any], feature: str) -> list[str]:
    """Return every declared environment built from a given feature.

    Args:
        manifest: The parsed pixi manifest.
        feature: The feature name to look for.

    Returns:
        The environment names, sorted.
    """
    carrying: list[str] = []
    for name, spec in manifest.get("environments", {}).items():
        declared = spec if isinstance(spec, list) else spec.get("features", [])
        if feature in declared:
            carrying.append(name)
    return sorted(carrying)


def _spike_directories() -> list[Path]:
    """Return every directory under `tests/` that holds a spike module.

    Found rather than hard-coded, and found recursively: a second spike
    directory, or a subdirectory of an existing one, is covered on the day it
    appears rather than on the day someone remembers to add it here.

    Returns:
        The directories, sorted and de-duplicated.
    """
    return sorted({path.parent for path in TESTS_ROOT.rglob(f"{SPIKE_MODULE_PREFIX}*.py")})


def _run_steps(workflow: dict[str, Any]) -> list[str]:
    """Return the `run` body of every step in every job of a workflow."""
    steps: list[str] = []
    for job in workflow.get("jobs", {}).values():
        for step in job.get("steps", []):
            run = step.get("run")
            if isinstance(run, str):
                steps.append(run)
    return steps


def _invokes(run_body: str, task: str) -> bool:
    """Report whether a step body invokes `pixi run <task>`.

    Matches on token boundaries so that `pixi run test-cov` is not read as an
    invocation of `pixi run test`.
    """
    needle = f"pixi run {task}"
    for line in run_body.splitlines():
        stripped = line.strip()
        if stripped == needle or stripped.startswith(f"{needle} "):
            return True
    return False


def _is_scheduled(workflow: dict[str, Any]) -> bool:
    """Report whether a workflow has a cron trigger.

    PyYAML parses the unquoted key `on` as the boolean True, so both spellings
    are checked rather than assuming either.
    """
    triggers = workflow.get("on", workflow.get(True, {}))
    return isinstance(triggers, dict) and "schedule" in triggers


def test_ci_task_reaches_every_gate_step(manifest: dict[str, Any]) -> None:
    """`pixi run ci` must reach all five steps AD-18 names."""
    ci = _all_tasks(manifest)["ci"]
    assert ci.get("depends-on") is not None, "ci must declare its steps via depends-on"
    assert set(ci["depends-on"]) == set(GATE_SEQUENCE)


def test_ci_task_runs_the_gate_steps_in_order(manifest: dict[str, Any]) -> None:
    """The executed order must be pre-commit, build, check, lint, cov.

    pixi executes `depends-on` sequentially in declaration order and stops at
    the first failure, so the list order is the executed order.
    """
    ci = _all_tasks(manifest)["ci"]
    assert ci["depends-on"] == GATE_SEQUENCE


def test_every_gate_step_pins_its_environment(manifest: dict[str, Any]) -> None:
    """`pixi run ci` must never prompt for an environment.

    pixi rejects `default-environment` on a task that only declares
    `depends-on`, so `ci` cannot pin one itself. What makes the gate
    non-interactive is that every step it depends on pins `dev`.
    """
    tasks = _all_tasks(manifest)
    unpinned = [step for step in GATE_SEQUENCE if tasks[step].get("default-environment") != "dev"]
    assert unpinned == [], f"these steps would prompt for an environment: {unpinned}"


def test_the_gate_task_is_reachable_from_exactly_one_environment(manifest: dict[str, Any]) -> None:
    """`pixi run ci` must never be ambiguous, and only one thing can make that true.

    Every other task pins `default-environment`, which keeps `pixi run <task>`
    unambiguous however many environments carry the feature declaring it. `ci`
    cannot: pixi rejects `default-environment` on a task that declares only
    `depends-on`. So the invariant is structural -- the feature that declares
    `ci` must belong to exactly one `[environments]` entry -- and it is the whole
    reason the dependency-free `gate` feature exists.

    It was argued at length in the story that introduced it and asserted nowhere.
    The failure it guards has already happened once: `spike-storage` layered the
    `dev` *feature*, `ci` became visible from two environments, and `pixi run ci`
    aborted with `the task 'ci' is ambiguous` before running a step. Epic 8's
    six-environment matrix is the same change again, six times over.
    """
    assert GATE_TASK not in manifest.get("tasks", {}), (
        f"{GATE_TASK!r} is declared in [tasks], which belongs to the default feature and is therefore "
        "visible from every environment. `pixi run ci` would be ambiguous the moment a second environment "
        "exists; declare it in a feature carried by exactly one environment instead."
    )

    feature = _feature_declaring(manifest, GATE_TASK)
    assert feature is not None, f"no feature declares the {GATE_TASK!r} task"

    carrying = _environments_carrying(manifest, feature)
    assert len(carrying) == 1, (
        f"feature {feature!r} declares {GATE_TASK!r} and is carried by environments {carrying}. "
        f"`pixi run {GATE_TASK}` cannot pin an environment -- pixi rejects `default-environment` on a "
        "depends-on-only task -- so the feature declaring it must belong to exactly one environment."
    )


def test_every_task_with_a_command_pins_its_environment(manifest: dict[str, Any]) -> None:
    """`docs/accelerator/development.md`'s "you never need `-e` for a task" has to be true of every task.

    The gate's own five steps are checked by the sibling above this one, and the
    spike task by its own test -- six of the manifest's tasks. The other twelve
    were covered by the documented rule and by nothing else. `changelog` is the
    concrete case: `.github/workflows/release.yml` invokes it, so losing its
    `default-environment` would surface at release time, with `pixi run ci`
    green and the tag already pushed.

    A task declared as a bare string cannot pin an environment at all, so it
    fails here too rather than slipping through the `.get` on a dict.
    """
    unpinned = sorted(
        name
        for name, task in _all_tasks(manifest).items()
        if not isinstance(task, dict) or ("cmd" in task and not task.get("default-environment"))
    )
    assert unpinned == [], (
        f"these tasks declare a command without pinning `default-environment`: {unpinned}. "
        "`pixi run <task>` would prompt, or pick an environment by accident, as soon as more than one "
        "environment carries the feature declaring it."
    )


def test_exactly_one_workflow_invokes_the_gate(workflows: Workflows) -> None:
    """AD-18: a single workflow invokes `pixi run ci`."""
    invoking = [
        name for name, workflow in workflows.items() if any(_invokes(step, "ci") for step in _run_steps(workflow))
    ]
    assert invoking == ["ci.yml"], f"only ci.yml may invoke the gate, got {invoking}"


def test_no_scheduled_workflow_invokes_build(workflows: Workflows) -> None:
    """AD-18: `build` comes off its fortnightly cron and onto the gate."""
    offenders = [
        name
        for name, workflow in workflows.items()
        if _is_scheduled(workflow) and any(_invokes(step, "build") for step in _run_steps(workflow))
    ]
    assert offenders == [], f"a cron must not invoke build: {offenders}"


@pytest.mark.parametrize("task", GATE_ONLY_TASKS)
def test_gate_steps_run_only_inside_the_gate(task: str, workflows: Workflows) -> None:
    """No workflow other than the gate may run a gate step on its own.

    This is what keeps AC #5 true: a step that exists only in one workflow is a
    step a developer cannot reproduce with `pixi run ci`.
    """
    offenders = [
        name
        for name, workflow in workflows.items()
        if name != "ci.yml" and any(_invokes(step, task) for step in _run_steps(workflow))
    ]
    assert offenders == [], f"{task} must run only in the gate, found in {offenders}"


def test_the_storage_spike_is_not_a_gate_step(manifest: dict[str, Any]) -> None:
    """R-1's spike runs on demand, in its own environment, and never inside the gate.

    It needs `django-storages`, which only the `spike-storage` environment has.
    A spike promoted to a gate step would fail the gate on every developer
    machine for a package the runtime set deliberately does not carry.
    """
    tasks = _all_tasks(manifest)
    assert SPIKE_TASK in tasks, f"{SPIKE_TASK} must stay runnable as a task of its own"
    assert tasks[SPIKE_TASK].get("default-environment") == SPIKE_ENVIRONMENT, (
        f"{SPIKE_TASK} must pin the {SPIKE_ENVIRONMENT!r} environment; the gate's `dev` environment does "
        "not carry django-storages"
    )
    assert SPIKE_TASK not in _all_tasks(manifest)["ci"]["depends-on"], f"{SPIKE_TASK} must not be a gate step"


def test_the_spike_task_names_a_file_that_exists(manifest: dict[str, Any]) -> None:
    """The spike task's command path is reconciled with the tree, not merely written down.

    `pixi run spike-storage` is the one command the recorded verdict tells Epic 7
    Story 7.5 to re-run, and its `cmd` names the spike module by path. Renaming
    the module while updating its `RECORDED_EXEMPTIONS` key in
    `tests/unit/test_suite_policy.py` -- the natural paired edit -- would leave
    the gate green and the task failing with "file or directory not found".
    """
    command = _all_tasks(manifest)[SPIKE_TASK]["cmd"]
    named = [token for token in command.split() if token.endswith(".py")]
    assert named, f"the {SPIKE_TASK} task names no module: {command!r}"

    missing = sorted(token for token in named if not (REPO_ROOT / token).is_file())
    assert missing == [], (
        f"the {SPIKE_TASK} task runs {command!r}, and these paths do not exist: {missing}. "
        "Rename the task's target in the same change that renames the module."
    )


def test_the_gate_cannot_collect_the_storage_spike() -> None:
    """The gate runs `pytest tests/`, and the spike lives under `tests/`. Naming is what separates them.

    Asserted from both sides, because either alone is satisfiable while the gate
    still breaks: every module under a spike directory is named `spike_*.py`, and
    no pattern in `python_files` matches that name. Add a `test_*.py` to such a
    directory, or add `spike_*.py` to `python_files`, and `pixi run ci` starts
    collecting a module whose imports its environment cannot satisfy.

    The scan recurses, and it finds its directories rather than being told one.
    A non-recursive `glob` over a single hard-coded path let
    `tests/spikes/s3/test_helpers.py` be collected by the gate while the test
    written to prevent exactly that went on passing.
    """
    with PYPROJECT.open("rb") as handle:
        patterns = tomllib.load(handle)["tool"]["pytest"]["ini_options"]["python_files"]

    assert SPIKE_DIRECTORY.is_dir(), f"{SPIKE_DIRECTORY} does not exist; the spike has no home"
    directories = _spike_directories()
    assert SPIKE_DIRECTORY in directories, (
        f"{SPIKE_DIRECTORY} holds no {SPIKE_MODULE_PREFIX}*.py module; this assertion would pass vacuously"
    )

    collectable = sorted(
        str(path.relative_to(TESTS_ROOT))
        for directory in directories
        for path in directory.rglob("*.py")
        if path.name != "__init__.py"
        if any(fnmatch(path.name, pattern) for pattern in patterns)
    )
    assert collectable == [], (
        f"these modules under {[str(path.relative_to(TESTS_ROOT)) for path in directories]} match "
        f"`python_files` {patterns} and so are collected by `pytest tests/` in the gate: {collectable}. "
        f"Name them {SPIKE_MODULE_PREFIX}*.py instead."
    )

    matching_patterns = sorted(pattern for pattern in patterns if fnmatch(f"{SPIKE_MODULE_PREFIX}anything.py", pattern))
    assert matching_patterns == [], (
        f"`python_files` now matches {SPIKE_MODULE_PREFIX}*.py via {matching_patterns}, so the gate would "
        "collect the spike. The spike needs the spike-storage environment and would fail on import."
    )


def _image_repository(image: str) -> str:
    """Return an image's repository name, without registry, tag or digest.

    `ghcr.io/example/postgres:18`, `postgres:18` and a bare `postgres` all
    reduce to `postgres`. The tag is stripped only after the final `/`, because
    a registry may itself carry a port -- `registry:5000/example/postgres` is a
    repository named `postgres`, not one named `registry`.
    """
    repository = image.split("@", 1)[0]
    name = repository.rsplit("/", 1)[-1]
    return name.rsplit(":", 1)[0].lower() if ":" in name else name.lower()


def _is_database_image(image: str) -> bool:
    """Report whether a service image is a database, registry prefix or not."""
    return _image_repository(image) in DATABASE_IMAGE_NAMES


def _is_postgres_image(image: str) -> bool:
    """Report whether a service image is a PostgreSQL, registry prefix or not."""
    return _image_repository(image) in POSTGRES_IMAGE_NAMES


def _postgres_service(workflows: Workflows) -> dict[str, Any]:
    """Return the gate job's PostgreSQL service definition."""
    services = workflows["ci.yml"]["jobs"]["gate"].get("services", {})
    for service in services.values():
        if _is_postgres_image(str(service.get("image", ""))):
            return service
    return {}


def _database_selectors(job: dict[str, Any]) -> list[str]:
    """Return every database-selecting variable a job sets, at job or step level.

    Step-level `env:` counts as well as job-level: the two are interchangeable
    for this purpose, and pinning only one of them leaves the obvious way in.
    """
    found = set(DATABASE_SELECTOR_VARS & job.get("env", {}).keys())
    for step in job.get("steps", []):
        found |= DATABASE_SELECTOR_VARS & step.get("env", {}).keys()
    return sorted(found)


def test_the_gate_declares_a_postgresql_service(workflows: Workflows) -> None:
    """FR-32: CI declares a PostgreSQL service, which no workflow did before."""
    services = workflows["ci.yml"]["jobs"]["gate"].get("services", {})
    images = [str(service.get("image", "")) for service in services.values()]
    assert any(_is_postgres_image(image) for image in images), (
        f"the gate job declares no postgres service, got images {images}"
    )


def _published_port(service: dict[str, Any], *, container: str) -> str:
    """Return the host side of the service's mapping for one container port.

    Docker accepts `container`, `host:container` and `ip:host:container`, so
    the host side is the second field from the right rather than the first
    field from the left. A bare `container` mapping publishes an ephemeral
    port and so names no host port at all.

    Args:
        service: The service definition.
        container: The port inside the container, as a string.

    Returns:
        The host port, or `""` when the service publishes none for it.
    """
    for port in service.get("ports", []):
        head, _, published = str(port).rpartition(":")
        if published == container and head:
            return head.rsplit(":", 1)[-1]
    return ""


def _published_host_port(service: dict[str, Any]) -> str:
    """Return the host side of the service's port mapping for 5432.

    Kept as its own name because the PostgreSQL assertions read better for it;
    the logic is shared with the Redis service below rather than written twice.
    """
    return _published_port(service, container="5432")


def test_the_postgresql_service_is_health_gated_and_reachable(workflows: Workflows) -> None:
    """The service must be ready before a step runs, and be published to the runner.

    Without the health check the gate starts `pixi run ci` against a database
    still coming up, which fails as connection-refused on a change that has
    nothing to do with the database. Without the port mapping the URL below
    resolves to nothing on `localhost`. Both are silent-in-review, loud-at-3am.

    The health command must reach the server over TCP, not the local socket.
    The postgres image runs initdb against a temporary server with
    `listen_addresses=''`, so a socket-scoped `pg_isready` reports ready while
    the port the runner uses is still closed -- a health gate that opens early
    is barely better than none.
    """
    service = _postgres_service(workflows)
    options = str(service.get("options", ""))
    assert "pg_isready" in options, f"the postgres service must health-check with pg_isready, got {options!r}"
    assert "-h localhost" in options, f"the health check must probe TCP, not the unix socket, got {options!r}"

    ports = [str(port) for port in service.get("ports", [])]
    assert any(port.endswith(":5432") for port in ports), f"the postgres service must publish 5432, got {ports}"


def test_the_gate_sets_the_database_url_for_the_whole_job(workflows: Workflows) -> None:
    """FR-32: the gate run is pointed at that service.

    The variable must sit on the job's own `env`, not on a step's, so that no
    step of `pixi run ci` can be reordered or added into a position where the
    database-touching ones no longer see it.
    """
    gate = workflows["ci.yml"]["jobs"]["gate"]
    assert "DATABASE_URL" in gate.get("env", {}), "DATABASE_URL must be set at job level on the gate"


def test_the_database_url_names_the_declared_postgresql_service(workflows: Workflows) -> None:
    """The URL's *value* is the mechanism, so the value is what must be asserted.

    Asserting only that the key exists lets `DATABASE_URL: ""` -- or a sqlite
    URL -- pass this file while reverting the gate to the substitution, because
    `config/settings/base.py:57` tests the variable for truthiness. That is the
    one edit that would make FR-32 false with a green suite, so it is pinned to
    the service declared a few lines above it in ci.yml.

    The URL is parsed rather than substring-searched. Containment would accept
    a credential that merely appears somewhere in the string (`POSTGRES_DB:
    user` is "in" `gateuser`), would break on a legitimate `?sslmode=` query
    parameter, and above all would not notice the host and port -- the halves
    that decide whether the URL reaches the declared service at all. A
    `ports: ["55432:5432"]` edit passed every previous assertion here and left
    the gate connection-refused.
    """
    url = str(workflows["ci.yml"]["jobs"]["gate"]["env"]["DATABASE_URL"])
    parsed = urlsplit(url)
    assert parsed.scheme in POSTGRES_URL_SCHEMES, f"the gate's DATABASE_URL must name PostgreSQL, got {url!r}"

    service = _postgres_service(workflows)
    service_env = service.get("env", {})
    missing = {"POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB"} - service_env.keys()
    assert not missing, f"the postgres service must declare its own credentials, missing {sorted(missing)}"

    # Percent-decoded before comparing, matching what `django-environ` does to
    # the same string: a password containing `@`, `:` or `/` *must* be encoded
    # in the URL to parse at all, and comparing the encoded form against the
    # service's plain value would fail a correct configuration -- pressure
    # toward a weaker password, from a test.
    assert unquote(parsed.username or "") == str(service_env["POSTGRES_USER"]), (
        f"the gate's DATABASE_URL user is not the service's POSTGRES_USER: {url!r}"
    )
    assert unquote(parsed.password or "") == str(service_env["POSTGRES_PASSWORD"]), (
        f"the gate's DATABASE_URL password is not the service's POSTGRES_PASSWORD: {url!r}"
    )
    assert parsed.path.rstrip("/") == f"/{service_env['POSTGRES_DB']}", (
        f"the gate's DATABASE_URL must select the service's database, got {url!r}"
    )

    # The runner reaches a service container through the port it publishes on
    # the host, so the URL's host and port are as load-bearing as the password.
    assert parsed.hostname == "localhost", f"a service container is reached on localhost, got {url!r}"
    published = _published_host_port(service)
    assert str(parsed.port) == published, (
        f"the gate's DATABASE_URL port {parsed.port} is not the service's published port {published!r}"
    )


def test_no_other_job_declares_a_database_service(workflows: Workflows) -> None:
    """The three-OS matrix stays on the sqlite substitution.

    GitHub Actions `services:` containers run only on Linux runners, so a
    database attached to that matrix could not work on two of its three legs.

    Scoped to *database* services on purpose: a future non-database service --
    a Redis for Celery integration tests, say -- is legitimate work that this
    check has no business blocking, and the Linux-runner reasoning above does
    not extend to it. It is not scoped to PostgreSQL though: the reason the
    matrix may not have a database does not care which database it is, and a
    `mysql:8` or a registry-qualified `ghcr.io/.../postgresql:18` would break
    the same two legs for the same reason -- so both are matched, by repository
    segment, rather than by the `postgres:` spelling this repository happens to
    use today.
    """
    offenders = [
        f"{name}:{job_name}"
        for name, workflow in workflows.items()
        for job_name, job in workflow.get("jobs", {}).items()
        if not (name == "ci.yml" and job_name == "gate")
        for service in job.get("services", {}).values()
        if _is_database_image(str(service.get("image", "")))
    ]
    assert offenders == [], f"only the gate job may declare a database service: {offenders}"


def test_no_other_job_points_itself_at_a_database(workflows: Workflows) -> None:
    """A database selector elsewhere would move a job off sqlite without a service.

    The sibling check above bans the service; this one bans the other half of
    the same mistake. A matrix leg that selected a database with no service to
    back it would fail on connection rather than run on the substitution, and
    the three-OS claim in ci.yml's comment would quietly stop being true.

    Both selectors are checked, not just `DATABASE_URL`: `base.py:59` selects
    PostgreSQL from `POSTGRES_DB` alone, so a guard that knew only the first
    branch would leave the second one open.
    """
    offenders = [
        f"{name}:{job_name} sets {selectors}"
        for name, workflow in workflows.items()
        for job_name, job in workflow.get("jobs", {}).items()
        if not (name == "ci.yml" and job_name == "gate")
        if (selectors := _database_selectors(job))
    ]
    assert offenders == [], f"only the gate job may select a database: {offenders}"


def test_no_gate_step_overrides_the_gate_database_url(workflows: Workflows) -> None:
    """The gate's own steps must not shadow the job-level `DATABASE_URL`.

    The sibling checks above police every job *except* the gate, so the gate
    was the one job where a step-level `env: DATABASE_URL: ""` would revert
    FR-32 with the whole contract still green: `test_the_gate_sets_...` and
    `test_the_database_url_names_...` both read the job-level value, which
    would still be correct, and `test_the_connection_is_the_backend_...` in the
    integration suite derives its expectation from the same emptied variable it
    then checks, so it would agree that sqlite was expected. Nothing observed
    the shadowing itself. This does.
    """
    gate = workflows["ci.yml"]["jobs"]["gate"]
    offenders = [
        step.get("name", step.get("run", "<unnamed step>"))
        for step in gate.get("steps", [])
        if DATABASE_SELECTOR_VARS & step.get("env", {}).keys()
    ]
    assert offenders == [], f"the gate's DATABASE_URL must not be shadowed at step level: {offenders}"


def test_some_job_still_exercises_the_sqlite_substitution(workflows: Workflows) -> None:
    """AC #4: sqlite is exercised in CI, not merely left configured.

    Once the gate moved onto PostgreSQL this became a real hole rather than a
    theoretical one: the unit tests open no database connection at all, so
    without an integration run somewhere that selects no database, nothing in
    CI touches sqlite and PostgreSQL-only ORM code passes every job while
    breaking `pixi run ci` for every developer with nothing running.

    The step that closes it lives on the compatibility matrix and was the only
    part of this change with nothing asserting it -- deleting it was silent,
    while its sibling checks above guarded loudly against a database appearing
    where it should not. This is the symmetric half.

    "Selects no database" means neither selector, for the same reason as the
    check above: a leg given `POSTGRES_DB` is on PostgreSQL, and counting it as
    the sqlite leg would leave sqlite untested while this test stayed green.
    """
    qualifying = [
        f"{name}:{job_name}"
        for name, workflow in workflows.items()
        for job_name, job in workflow.get("jobs", {}).items()
        if not _database_selectors(job)
        if any(_invokes(step.get("run", ""), "test-integration") for step in job.get("steps", []) if step.get("run"))
    ]
    assert qualifying != [], "no CI job runs pixi run test-integration without a database; sqlite is untested"


def _redis_service(workflows: Workflows) -> dict[str, Any]:
    """Return the gate job's Redis service definition.

    Matched on the image's repository segment, exactly as the PostgreSQL lookup
    is and for the same reason: a registry-qualified mirror is the ordinary
    remedy for Docker Hub's unauthenticated pull limit on shared runners, and
    nothing about AC 4 cares where the image came from.

    Args:
        workflows: Every parsed workflow file.

    Returns:
        The service definition, or an empty mapping when the gate declares none.
    """
    services = workflows["ci.yml"]["jobs"]["gate"].get("services", {})
    for service in services.values():
        if _image_repository(str(service.get("image", ""))) in REDIS_IMAGE_NAMES:
            return service
    return {}


def test_the_gate_declares_a_redis_service(workflows: Workflows) -> None:
    """`CPM-EVIDENCE-S08` AC 4: the gate runs the one case LocMem cannot fail.

    `core/rate_limit.py`'s `add`-then-`incr` exists so two worker *processes*
    racing a new window increment one counter rather than one of them resetting
    the other. Under the in-process substitution each process holds its own
    counter, so the property cannot fail and every unit case passes against a
    limiter that loses the race.
    `tests/integration/django_apps/test_shared_allowance.py` is the case that
    can fail, and it needs this service to run at all.

    Pinned here because deleting the service would not fail anything else:
    the case would go back to skipping, the suite would stay green, and AC 4's
    proof would quietly become a recorded exemption for a test nobody runs.
    """
    services = workflows["ci.yml"]["jobs"]["gate"].get("services", {})
    images = [str(service.get("image", "")) for service in services.values()]

    assert any(_image_repository(image) in REDIS_IMAGE_NAMES for image in images), (
        f"the gate job declares no redis service, got images {images}"
    )


def test_the_redis_service_is_health_gated_and_reachable(workflows: Workflows) -> None:
    """The service must accept commands before a step runs, and be published to the runner.

    Without the health check the gate starts `pixi run ci` against a Redis still
    coming up, and the two workers fail to connect -- which reads as "the
    counter is not shared" and points at the limiter rather than at the runner.
    Without the port mapping the URL below resolves to nothing on `localhost`.

    `redis-cli ping` rather than a container-is-running check, for the same
    reason the PostgreSQL health command probes TCP: a container that has
    started is not a server that answers.
    """
    service = _redis_service(workflows)
    options = str(service.get("options", ""))

    assert "redis-cli ping" in options, f"the redis service must health-check with redis-cli ping, got {options!r}"

    ports = [str(port) for port in service.get("ports", [])]
    assert any(port.endswith(f":{REDIS_PORT}") for port in ports), (
        f"the redis service must publish {REDIS_PORT}, got {ports}"
    )


def test_the_redis_url_names_the_declared_redis_service(workflows: Workflows) -> None:
    """The URL's *value* is the mechanism, so the value is what must be asserted.

    `config/settings/test.py` reads `CPM_TEST_REDIS_URL` for truthiness, exactly
    as `base.py` reads `DATABASE_URL`, so an empty value would revert the gate to
    the in-process substitution and turn AC 4's proof back into a skip while
    every other assertion here passed. The host and port are parsed rather than
    substring-searched, because they are the halves that decide whether the URL
    reaches the declared service at all.

    Job level, not step level: `pixi run ci` fans out into five steps and the
    one that runs the suite must not depend on which step declares the variable.
    """
    gate = workflows["ci.yml"]["jobs"]["gate"]

    assert REDIS_URL_VARIABLE in gate.get("env", {}), f"{REDIS_URL_VARIABLE} must be set at job level on the gate"

    parsed = urlsplit(str(gate["env"][REDIS_URL_VARIABLE]))
    assert parsed.scheme in REDIS_URL_SCHEMES, f"the gate's {REDIS_URL_VARIABLE} must name Redis, got {parsed!r}"
    assert parsed.hostname == "localhost", f"a service container is reached on localhost, got {parsed!r}"
    published = _published_port(_redis_service(workflows), container=REDIS_PORT)
    assert str(parsed.port) == published, (
        f"the gate's {REDIS_URL_VARIABLE} port {parsed.port} is not the service's published port {published!r}"
    )


def test_no_gate_step_overrides_the_gate_redis_url(workflows: Workflows) -> None:
    """The gate's own steps must not shadow the job-level `CPM_TEST_REDIS_URL`.

    The sibling of `test_no_gate_step_overrides_the_gate_database_url`, and it
    guards a quieter failure: a step-level `CPM_TEST_REDIS_URL: ""` would leave
    every assertion above reading the correct job-level value while the case it
    arms skipped, and a skipped case reports nothing at all.
    """
    gate = workflows["ci.yml"]["jobs"]["gate"]
    offenders = [
        step.get("name", step.get("run", "<unnamed step>"))
        for step in gate.get("steps", [])
        if REDIS_URL_VARIABLE in step.get("env", {})
    ]

    assert offenders == [], f"the gate's {REDIS_URL_VARIABLE} must not be shadowed at step level: {offenders}"


def test_the_redis_gate_script_is_runnable_as_a_task(manifest: dict[str, Any]) -> None:
    """The local counterpart of the service above, reachable the way its sibling is.

    `gate-postgres` and `gate-redis` are the two commands that reproduce what
    the gate job's services provide, and both must stay outside `pixi run ci` --
    the gate is one sequence (AD-18) and neither script's container is declared
    by any job that runs it.

    The script path is reconciled with the tree rather than merely written down,
    for the reason `test_the_spike_task_names_a_file_that_exists` gives: a
    renamed script leaves the task failing with "file not found" and the gate
    green.
    """
    tasks = _all_tasks(manifest)

    assert REDIS_GATE_TASK in tasks, f"{REDIS_GATE_TASK} must stay runnable as a task of its own"
    assert tasks[REDIS_GATE_TASK].get("default-environment") == "dev"
    assert REDIS_GATE_TASK not in tasks[GATE_TASK]["depends-on"], f"{REDIS_GATE_TASK} must not be a gate step"

    named = [token for token in tasks[REDIS_GATE_TASK]["cmd"].split() if token.endswith(".sh")]
    assert named, f"the {REDIS_GATE_TASK} task names no script: {tasks[REDIS_GATE_TASK]['cmd']!r}"
    missing = sorted(token for token in named if not (REPO_ROOT / token).is_file())
    assert missing == [], f"the {REDIS_GATE_TASK} task runs scripts that do not exist: {missing}"


def test_the_scale_spike_is_runnable_as_a_task_and_never_a_gate_step(manifest: dict[str, Any]) -> None:
    """CPM-OPERATE-S10's spike runs on demand, in `dev`, through its script, and never inside the gate.

    Reconciled with the tree rather than written down, in five parts: the task
    pins `dev` (the documented "you never need `-e`" rule) and is not a gate
    step; the script it runs exists; the script's pytest line names a spike
    module that exists and carries the prefix the gate's collection rule
    excludes; that line passes `-s` (the plans are printed, and the story
    records what is printed) and `-m spike`; and the module's `pytestmark`
    carries the marker the `-m` expression selects, so the two cannot drift
    apart into a run that collects nothing. A renamed module or script would
    otherwise leave the gate green and the task failing with "file not found"
    -- the failure `test_the_spike_task_names_a_file_that_exists` guards for
    R-1's spike.
    """
    tasks = _all_tasks(manifest)

    assert SCALE_SPIKE_TASK in tasks, f"{SCALE_SPIKE_TASK} must stay runnable as a task of its own"
    assert tasks[SCALE_SPIKE_TASK].get("default-environment") == "dev"
    assert SCALE_SPIKE_TASK not in tasks[GATE_TASK]["depends-on"], f"{SCALE_SPIKE_TASK} must not be a gate step"

    named = [token for token in tasks[SCALE_SPIKE_TASK]["cmd"].split() if token.endswith(".sh")]
    assert named, f"the {SCALE_SPIKE_TASK} task names no script: {tasks[SCALE_SPIKE_TASK]['cmd']!r}"
    missing = sorted(token for token in named if not (REPO_ROOT / token).is_file())
    assert missing == [], f"the {SCALE_SPIKE_TASK} task runs scripts that do not exist: {missing}"

    script = (REPO_ROOT / named[0]).read_text(encoding="utf-8")
    module = SCALE_SPIKE_MODULE.relative_to(REPO_ROOT).as_posix()
    assert SCALE_SPIKE_MODULE.is_file(), f"{module} does not exist"
    assert SCALE_SPIKE_MODULE.name.startswith(SPIKE_MODULE_PREFIX), (
        f"{module} must be named {SPIKE_MODULE_PREFIX}*.py, or `pytest tests/` collects it in the gate"
    )
    assert f'SPIKE_MODULE="{module}"' in script, (
        f"{named[0]} must run {module}; it is the only thing that names the spike"
    )
    pytest_lines = [line for line in script.splitlines() if "pytest" in line and '"${SPIKE_MODULE}"' in line]
    assert len(pytest_lines) == 1, f"{named[0]} must invoke pytest on the module exactly once, found {pytest_lines}"
    (invocation,) = pytest_lines
    assert " -s" in invocation, f"{named[0]} must pass -s, or the plans the story records never reach the log"
    marker = re.search(r"-m (\w+)", invocation)
    assert marker is not None, f"{named[0]} must select the spike by marker: {invocation!r}"

    pytestmark = re.search(r"^pytestmark = (.+)$", SCALE_SPIKE_MODULE.read_text(encoding="utf-8"), re.MULTILINE)
    assert pytestmark is not None, f"{module} declares no module-level pytestmark"
    assert f"pytest.mark.{marker.group(1)}" in pytestmark.group(1), (
        f"{named[0]} selects `-m {marker.group(1)}` and {module}'s pytestmark is {pytestmark.group(1)}: "
        "the two must agree, or the run collects nothing"
    )


def test_reference_application_keeps_its_three_os_matrix(workflows: Workflows) -> None:
    """AC #4: the three-OS matrix stays on the reference application.

    The PostgreSQL gate cannot join it, because GitHub Actions `services:`
    containers run only on Linux runners.
    """
    jobs = workflows["ci.yml"]["jobs"]
    matrices = [job.get("strategy", {}).get("matrix", {}).get("os", []) for job in jobs.values()]
    assert any(THREE_OS_RUNNERS.issubset(set(os_list)) for os_list in matrices), (
        f"no job declares all of {sorted(THREE_OS_RUNNERS)}"
    )
