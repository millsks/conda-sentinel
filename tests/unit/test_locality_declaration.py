"""Tests for the locality declaration AD-13 puts in `pixi.toml`.

Locality is declared by the pixi *environment*, not by a file in the source tree
and not by a task: `COMPONENT_RUNTIME=local` sits once in
`[feature.dev.activation.env]`, so the declaration is committed with the
manifest and a freshly cloned component runs with one command, while the
`default` environment declares nothing and reads *deployed* -- which is what the
golden base runs and what the release stage invokes.

Three prohibitions carry that, and none of them fails on its own:

* **No `COMPONENT_*` in the `default` environment's resolved activation env.**
  The golden base runs pixi, so that env is evaluated in production: a
  `COMPONENT_RUNTIME=local` reaching the deployed image would invert the
  fail-closed default and disarm every refusal built on it, silently. It also
  takes the deployment platform's own configmap out of sole control of the
  variable.
* **No `COMPONENT_PROCESS` in *any* activation env**, feature-scoped included.
  There it would make every management command declare itself a serving
  process, and `pixi run migrate` -- a release-stage step -- would refuse on the
  unapplied-migrations condition and deadlock the release.
* **No task declares `COMPONENT_RUNTIME` in its own `env`.** A task's `env`
  overrides the caller's environment (probed on pixi 0.70.2), so a task-level
  declaration could not be corrected by the platform. The single declaration
  site is the environment.

Platform-scoped tables are in scope throughout. `[target.<platform>.activation.env]`
and `[feature.<name>.target.<platform>.activation.env]` are honoured by pixi and
reach the process, so a scanner that read only the unscoped tables would let
`COMPONENT_RUNTIME = "local"` in `[target.linux-64.activation.env]` pass green
and ship in the production image.

Read with `tomllib` and asserted over the parsed structure, never over the raw
text: this manifest carries long comment blocks *about* `COMPONENT_RUNTIME`
beside the tables they constrain, and a substring search would be satisfied --
or broken -- by prose.

This is a unit test. It reads one committed repository file and its own process
environment, and opens no network, database or other filesystem surface.
"""

from __future__ import annotations

import os
from typing import Any

import pytest

from tests.pixi_manifest import DEFAULT_FEATURE
from tests.pixi_manifest import PIXI_MANIFEST
from tests.pixi_manifest import activation_envs
from tests.pixi_manifest import activation_scripts
from tests.pixi_manifest import load_manifest
from tests.pixi_manifest import task_env
from tests.pixi_manifest import task_tables
from tests.pixi_manifest import tasks

# The prefix AD-13 keeps out of the deployed activation env. Matched as a prefix
# rather than as the two names, so a third `COMPONENT_*` fact invented later is
# covered on the day it is invented rather than on the day someone remembers
# this test. Matched case-insensitively: environment variables are
# case-insensitive on Windows, which is a declared platform here, so a
# `component_runtime` written in lower case would resolve there exactly as the
# upper-case spelling does.
COMPONENT_PREFIX = "COMPONENT_"

# The two names, restated rather than imported from `config.locality`: a test
# that reads the value it is checking asserts nothing about the manifest, and
# this file's whole job is to hold the manifest to the contract that module
# implements.
RUNTIME_VARIABLE = "COMPONENT_RUNTIME"
PROCESS_VARIABLE = "COMPONENT_PROCESS"
LOCAL = "local"

# The one table permitted to declare a `COMPONENT_*` variable, and the one
# variable it may declare.
DECLARATION_FEATURE = "dev"

# The environments a developer runs in, and therefore the ones allowed to carry
# the `dev` feature and its declaration. `spike-storage` layers the dev feature
# deliberately (it is `dev` plus django-storages, for R-1's fitness spike), so
# it is a developer environment too. Everything else in `[environments]` is
# production-bound: Epic 8's six-environment matrix lands there, and each entry
# it adds has to stay out of this set or fail the assertion below.
DEVELOPER_ENVIRONMENTS: frozenset[str] = frozenset({"dev", "spike-storage"})

# The process types Epic 5 (AD-14) delivers as tasks of their own. They set
# `COMPONENT_PROCESS` in their own `env` and inherit *deployed*, so none of them
# may declare a runtime -- and no *other* task may declare a process type. None
# exists yet, which is why both assertions below are written to pass vacuously
# rather than to be added later.
SERVING_PROCESS_TASKS: frozenset[str] = frozenset({"web", "worker", "beat"})


@pytest.fixture(scope="module")
def manifest() -> dict[str, Any]:
    """Return the parsed pixi manifest.

    Returns:
        The manifest, parsed from TOML through `tests.pixi_manifest`, which is
        the one reader every manifest assertion in the suite shares -- the
        activation-env and script walks this module used to carry copies of live
        there since `CPM-OPERATE-S06`.
    """
    return load_manifest()


def _environment_features(manifest: dict[str, Any]) -> dict[str, list[str]]:
    """Return the features each declared environment resolves.

    An environment entry is either a bare list of feature names or a table with
    a `features` key. Either way pixi adds the implicit `default` feature unless
    the entry sets `no-default-feature`, which is what makes the unscoped
    `[activation.env]` part of what `default` resolves.

    Args:
        manifest: The parsed pixi manifest.

    Returns:
        Environment name -> the features it resolves, `default` included.
    """
    resolved: dict[str, list[str]] = {}
    for name, entry in manifest.get("environments", {}).items():
        if isinstance(entry, dict):
            declared = [str(feature) for feature in entry.get("features", [])]
            implicit = [] if entry.get("no-default-feature", False) else [DEFAULT_FEATURE]
        else:
            declared = [str(feature) for feature in entry]
            implicit = [DEFAULT_FEATURE]
        resolved[str(name)] = implicit + declared
    return resolved


def _is_component_variable(name: str) -> bool:
    """Report whether a variable name is part of the `COMPONENT_*` contract.

    Args:
        name: The variable name as the manifest spells it.

    Returns:
        True when the name carries the prefix, in any casing.
    """
    return name.upper().startswith(COMPONENT_PREFIX)


def test_manifest_is_present() -> None:
    """The manifest resolves from this test file, so the assertions below mean something."""
    assert PIXI_MANIFEST.is_file()


def test_the_scanners_see_the_manifest_they_claim_to(manifest: dict[str, Any]) -> None:
    """The three readers find the tables this file is written against.

    Every prohibition here is an assertion that something is *absent*, so a
    reader that silently found nothing would pass all of them while checking
    nothing at all. This is the non-vacuity guard for the file.
    """
    assert "[activation.env]" in activation_envs(manifest)
    assert "[feature.dev.activation.env]" in activation_envs(manifest)
    assert "[tasks]" in task_tables(manifest)
    assert DEFAULT_FEATURE in _environment_features(manifest)


def test_default_environment_activation_env_declares_no_component_variable(manifest: dict[str, Any]) -> None:
    """No `COMPONENT_*` variable reaches a production-bound environment (AC #3).

    The golden base runs pixi, so the activation env of whatever environment it
    installs is evaluated in production. A `COMPONENT_RUNTIME=local` that
    reached a deployed image would invert the fail-closed locality default and
    disarm every refusal built on it; nothing would break while it did. It would
    also displace the deployment platform's own configuration -- an OpenShift
    configmap or equivalent -- which is meant to be in sole control of the
    variable precisely because `default` declares nothing.

    Scoped to every environment that is not a developer environment, so today's
    `default` is covered and Epic 8's matrix is covered on the day it lands.
    """
    tables = activation_envs(manifest)
    offenders: list[str] = []
    for environment, features in _environment_features(manifest).items():
        if environment in DEVELOPER_ENVIRONMENTS:
            continue
        offenders.extend(
            f"{variable} in {location}, resolved by the {environment!r} environment"
            for location, (feature, table) in tables.items()
            if feature in features
            for variable in table
            if _is_component_variable(variable)
        )

    assert not offenders, (
        f"these {COMPONENT_PREFIX}* variables reach a production-bound environment: {sorted(offenders)}. "
        f"Activation env is evaluated in production because the golden base runs pixi (AD-13). Locality is "
        f"declared once, in [feature.{DECLARATION_FEATURE}.activation.env], and nothing production-bound "
        f"includes that feature."
    )


def test_component_process_absent_from_every_activation_env(manifest: dict[str, Any]) -> None:
    """`COMPONENT_PROCESS` appears in no activation env anywhere (AC #3).

    Absolute, and independent of which environment resolves the table: a
    `COMPONENT_PROCESS` in *any* activation env makes every command run under it
    declare itself a serving process. `pixi run migrate` is one of those
    commands and it is a release-stage step, so it would refuse on the
    unapplied-migrations condition and deadlock the release. Serving processes
    declare it in their own task `env` (Epic 5, AD-14) and nowhere else.
    """
    offenders = sorted(
        location
        for location, (_feature, table) in activation_envs(manifest).items()
        if any(variable.upper() == PROCESS_VARIABLE for variable in table)
    )
    assert not offenders, (
        f"{PROCESS_VARIABLE} is declared in these activation tables: {offenders}. "
        f"It belongs in the `env` of the serving-process tasks and nowhere else -- in an activation env it "
        f"makes every management command a serving process and deadlocks the release stage."
    )


def test_dev_feature_declares_local_runtime(manifest: dict[str, Any]) -> None:
    """The `dev` feature's activation env declares `COMPONENT_RUNTIME=local` (AC #1).

    The single declaration site, committed with the manifest, so a freshly
    cloned component is local from the first command with no untracked file to
    create. The value is pinned to exactly `local` -- stricter than
    `config.locality.is_local()`, which also accepts `LOCAL` and `" local "`.
    That is deliberate: the reader is forgiving so a hand-set variable behaves,
    while the manifest is held to one canonical spelling, and a `"dev"` that the
    reader would *not* accept fails loudly here rather than quietly making every
    developer path deployed.
    """
    tables = activation_envs(manifest)
    location = f"[feature.{DECLARATION_FEATURE}.activation.env]"
    assert location in tables, (
        f"{location} is missing from pixi.toml. It is the one site AD-13 permits for the locality declaration."
    )

    _feature, env = tables[location]
    assert env.get(RUNTIME_VARIABLE) == LOCAL, (
        f"{location} declares {RUNTIME_VARIABLE} = {env.get(RUNTIME_VARIABLE)!r}, not {LOCAL!r}. "
        f"Every developer path resolves to the {DECLARATION_FEATURE} environment and inherits locality from "
        f"this one key."
    )


def test_no_task_declares_component_runtime(manifest: dict[str, Any]) -> None:
    """No task sets `COMPONENT_RUNTIME` in its own `env`, at any value (AC #1).

    The single declaration site is the environment. A task's `env` *overrides*
    the caller's, so a task-level declaration cannot be corrected by the
    deployment platform's configmap: `COMPONENT_RUNTIME=local` on `migrate`
    would make the production release stage read *local* and skip every
    stage-1 refusal, with no way out. This is the assertion that keeps the
    superseded per-task design from creeping back in one task at a time.
    """
    offenders = sorted(
        f"{name} = {env[variable]!r} in {table}"
        for table, name, definition in tasks(manifest)
        for env in [task_env(definition)]
        for variable in env
        if variable.upper() == RUNTIME_VARIABLE
    )
    assert not offenders, (
        f"these tasks declare {RUNTIME_VARIABLE} in their own `env`: {offenders}. "
        f"Locality is declared by the pixi environment, not by the task (AD-13) -- a task `env` overrides the "
        f"caller and takes the deployment platform out of the loop."
    )


def test_only_serving_process_tasks_declare_a_process_type(manifest: dict[str, Any]) -> None:
    """`COMPONENT_PROCESS` in a task `env` is confined to the serving processes.

    Epic 5's `web`, `worker` and `beat` are its only producers (AD-14). A
    `COMPONENT_PROCESS = "web"` on `migrate` would pass every other assertion in
    this file and reproduce verbatim the release deadlock AD-13 exists to
    prevent, so the confinement is asserted rather than assumed. Vacuous today:
    no task declares the variable at all.
    """
    offenders = sorted(
        f"{name} = {env[variable]!r} in {table}"
        for table, name, definition in tasks(manifest)
        if name not in SERVING_PROCESS_TASKS
        for env in [task_env(definition)]
        for variable in env
        if variable.upper() == PROCESS_VARIABLE
    )
    assert not offenders, (
        f"these non-serving tasks declare {PROCESS_VARIABLE}: {offenders}. "
        f"Only {sorted(SERVING_PROCESS_TASKS)} may declare a process type; on a management command it turns "
        f"that command into a serving process and deadlocks the release stage on the migrations refusal."
    )


def test_no_production_bound_environment_includes_the_dev_feature(manifest: dict[str, Any]) -> None:
    """Only developer environments carry the feature holding the declaration (AC #3).

    This is what keeps the narrowed prohibition honest. `COMPONENT_RUNTIME` is
    permitted in `[feature.dev.activation.env]` on the understanding that no
    production-bound environment resolves that feature; the moment one does, the
    declaration ships. Epic 8's six-environment matrix is where that becomes
    easy to get wrong, so the rule is in force before the matrix arrives.
    """
    environments = _environment_features(manifest)
    missing = sorted(DEVELOPER_ENVIRONMENTS - set(environments))
    assert not missing, (
        f"DEVELOPER_ENVIRONMENTS names environments pixi.toml does not declare: {missing}. "
        f"Remove them from the set in this module in the same change that removes the environment."
    )

    offenders = sorted(
        f"{environment} includes {DECLARATION_FEATURE}"
        for environment, features in environments.items()
        if environment not in DEVELOPER_ENVIRONMENTS and DECLARATION_FEATURE in features
    )
    assert not offenders, (
        f"these production-bound environments include the {DECLARATION_FEATURE!r} feature: {offenders}. "
        f"That feature carries {RUNTIME_VARIABLE} = {LOCAL!r}, so including it ships the locality declaration "
        f"into the deployed image and inverts the fail-closed default."
    )


def test_serving_process_tasks_declare_no_runtime(manifest: dict[str, Any]) -> None:
    """`web`, `worker` and `beat` inherit *deployed* and declare no runtime (AC #4).

    They arrive with Epic 5 (AD-14) and declare `COMPONENT_PROCESS` instead.
    Written to pass vacuously until then, so the rule is in force on the day the
    tasks land rather than added afterwards.
    """
    offenders = sorted(
        f"{name} = {env[variable]!r} in {table}"
        for table, name, definition in tasks(manifest)
        if name in SERVING_PROCESS_TASKS
        for env in [task_env(definition)]
        for variable in env
        if variable.upper() == RUNTIME_VARIABLE
    )
    assert not offenders, (
        f"these serving-process tasks declare a runtime: {offenders}. "
        f"A serving process is deployed by default and declares only {PROCESS_VARIABLE}; declaring a runtime "
        f"on one would make a deployed process claim to be local."
    )


def test_no_activation_script_offers_an_unchecked_export_route(manifest: dict[str, Any]) -> None:
    """No activation script exists, so the env-table scan above is the whole surface.

    `[activation] scripts` is the second way pixi exports variables into a
    resolved environment, and its contents live in a shell script this module
    cannot parse. The manifest declares none. Asserting that keeps the
    prohibitions exhaustive: the day someone adds one, this fails and the check
    has to be extended rather than quietly bypassed by an `export
    COMPONENT_RUNTIME=local` in a file nothing reconciles.
    """
    declared = activation_scripts(manifest)
    assert not declared, (
        f"pixi.toml declares activation scripts: {sorted(declared)}. "
        f"A script can export {COMPONENT_PREFIX}* variables that the activation-env assertions in this module "
        f"cannot see -- extend them to cover the script's contents, or drop the script."
    )


def test_the_declared_runtime_is_in_force_in_this_process() -> None:
    """The declaration actually reaches the process, not merely the manifest (AC #1).

    Every assertion above reads TOML, which proves what is *declared* and
    nothing about what pixi does with it. Feature-scoped activation env is the
    mechanism the whole design now rests on, so the two halves are split the way
    `COVERAGE_CORE` already splits them here -- declared in
    `test_coverage_policy.py`, in force in `test_coverage_measurement.py`.

    This suite runs in the `dev` environment (`pixi run test`, `pixi run
    test-cov`, and `pixi run -e dev -- pytest` alike), so the variable is
    present. A failure here means either the declaration is gone or pytest was
    started outside the pixi environment, which no supported route does.
    """
    assert os.environ.get(RUNTIME_VARIABLE) == LOCAL, (
        f"{RUNTIME_VARIABLE} is {os.environ.get(RUNTIME_VARIABLE)!r} in this process, not {LOCAL!r}. "
        f"The suite runs in the {DECLARATION_FEATURE} environment, whose activation env declares it; if this "
        f"fails, either the declaration was removed or pytest was started outside `pixi run`."
    )
