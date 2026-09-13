"""The policy application is installed, labelled, placed, and adopts its passes.

Shaped after `tests/unit/django_apps/test_identity_app.py`, which is a template
rather than a sweep: it hardcodes its application because the properties it
asserts are properties of *that* application's adoption, and the same is true
here. A single parametrized module over four applications would look like an
economy and would lose the thing that makes any of them useful -- an adoption can
be forgotten in one of its two halves, and a sweep that iterated `INSTALLED_APPS`
would pass on whatever it happened to find.

What is deliberately *not* repeated from `tests/unit/django_apps/test_core_app.py`:
`src/django_apps/` carrying no `__init__.py`, and `django_apps` failing to
import. Those are facts about the import root rather than about an application,
`CPM-PLATFORM-S01` owns them, and asserting them four times would mean four
modules to edit the day the root changes. What *is* repeated is the resolution
probe, because "this application resolves from the second root" is a claim about
this application: a copy of it under `src/` would satisfy the import and fail
here.

**This application declares a `ready()`, which is why this module has cases the
other two do not.** `core` and `identity` declare none and their modules assert
that; `collectors` was the first that did. The hook here adopts the five passes
this component ships -- `CurrencyPass`, `FeedstockPresencePass`,
`VulnerabilityPass`, `LicensePass` and `RemediationPass` -- into `core`'s
registry, and what is asserted is that every adoption happened, in the declared
order, that they are idempotent, and that no refusal was invented to go with
them.

No database, no network, no subprocess. The app registry is populated at session
start. It does read the application's own directory -- which modules are present,
which subdirectories, which migrations, which reviewed data files -- because
"this application ships exactly these files" is not a claim the registry can
answer; every one of those reads is of a path this repository owns.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Final

from django.apps import apps
from django.conf import settings

from conda_sentinel.core.policy import column_owners
from conda_sentinel.core.policy import pass_registrations
from conda_sentinel.core.policy import registered_passes
from conda_sentinel.core.rollup import contributable_columns
from conda_sentinel.policies.apps import PoliciesConfig
from conda_sentinel.policies.currency import POLICY_NAME
from conda_sentinel.policies.currency import ROLLUP_COLUMN
from conda_sentinel.policies.currency import CurrencyPass
from conda_sentinel.policies.feedstock import POLICY_NAME as FEEDSTOCK_POLICY_NAME
from conda_sentinel.policies.feedstock import ROLLUP_COLUMN as FEEDSTOCK_ROLLUP_COLUMN
from conda_sentinel.policies.feedstock import FeedstockPresencePass
from conda_sentinel.policies.licence import POLICY_NAME as LICENCE_POLICY_NAME
from conda_sentinel.policies.licence import LicensePass
from conda_sentinel.policies.parameters import parameters_directory
from conda_sentinel.policies.parameters import parameters_file
from conda_sentinel.policies.remediation import POLICY_NAME as REMEDIATION_POLICY_NAME
from conda_sentinel.policies.remediation import RemediationPass
from conda_sentinel.policies.vulnerability import POLICY_NAME as VULNERABILITY_POLICY_NAME
from conda_sentinel.policies.vulnerability import VulnerabilityPass
from tests.passes import ADOPTED_PASS_NAMES
from tests.source_scan import SRC_ROOT

#: This repository's root, four levels up from `tests/unit/django_apps/`.
REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[3]

#: Where the application must resolve from. Not `src/`: an application that
#: resolved from the first root would mean the second one is doing nothing.
APPLICATION_ROOT: Final[Path] = REPO_ROOT / "src" / "django_apps"

#: The dotted name the application is installed and imported as. `django_apps` is
#: absent from it on purpose -- it is a path root, not a package.
APPLICATION_NAME: Final[str] = "conda_sentinel.policies"

#: Derived by Django from the last segment of the name.
APPLICATION_LABEL: Final[str] = "policies"

#: The application adopted before this one. Order in `LOCAL_APPS` is load-bearing
#: (AD-8 appends contributions in it), so "after `collectors`" is asserted rather
#: than assumed -- and the pass registry keeps *declaration* order (`CPM-AD-21`),
#: so where this application sits is part of what is declared about which pass
#: may read which.
PRECEDING_APPLICATION_NAME: Final[str] = "conda_sentinel.collectors"

#: The stage-2 owner (AD-26). No adopted application may precede it.
STAGE_TWO_OWNER_NAME: Final[str] = "django_service.users"

#: The applications declared *after* this one, and why that is allowed.
#:
#: This application used to be last, and the reason was `CPM-AD-21`: the pass
#: registry is in declaration order because a later pass may read an earlier pass's
#: derived rows, so where a *pass-registering* application sits is part of what has
#: been declared. `CPM-APP-S02` added `conda_sentinel.surface`, which registers no
#: pass at all -- it is a read surface with no models, no `ready()` and nothing in
#: the registry -- so it can sit after this one without changing any pass's inputs.
#: `CPM-APP-S04` added `conda_sentinel.workflow` on the same terms: it owns the queue
#: items the policy run opens, and registers no pass either.
#:
#: Listed rather than left as "anything may follow", because the rule that matters
#: survives: nothing that registers a pass may be declared after this application,
#: and `test_nothing_after_this_application_registers_a_pass` is what holds it.
APPLICATIONS_AFTER: Final[tuple[str, ...]] = ("conda_sentinel.workflow", "conda_sentinel.surface")

#: The call that adopts a pass, swept for as text.
#:
#: Text rather than an import graph, because what matters is whether the call is
#: *there* -- an application that imported the function and never called it registers
#: nothing, and one that called it through an alias is doing something worth failing
#: on anyway.
REGISTER_PASS_CALL: Final[str] = "register_pass("  # noqa: S105 - a function call, not a credential

#: Every module this application declares today. `CPM-AD-19` gives a domain
#: application `urls.py`, `tasks.py` and an `api/` subpackage when it has views or
#: work to schedule; this one has neither. A pass runs inside a policy run, which
#: `core/tasks.py` already schedules, so there is no task here to declare and
#: nothing for `tests/unit/django_apps/test_task_routing_audit.py` to route.
#:
#: `outcomes.py` is a leaf module holding the two composed vocabularies and
#: nothing else, on exactly the terms `identity/confidence.py` holds
#: `IdentityConfidence`: `core/models.py` reads them for the rollup columns while
#: `policies/currency.py` and `policies/feedstock.py` read `core.policy`, and
#: those edges together would be an import cycle. Its own docstring carries the
#: reasoning.
#:
#: `parameters.py` is `CPM-CURRENCY-S07`'s versioned parameter reader. It is the
#: one module here that reads a file, and the file it reads is the `data/` tree
#: below.
EXPECTED_MODULES: Final[tuple[str, ...]] = (
    "__init__.py",
    "apps.py",
    "currency.py",
    "feedstock.py",
    "licence.py",
    "models.py",
    "outcomes.py",
    "parameters.py",
    "priority.py",
    "py314_readiness.py",
    "work_type.py",
    "remediation.py",
    "vulnerability.py",
)

#: The surfaces this story does not build, in both shapes each can take. A
#: `tasks.py` and a `tasks/` package are the same surface to Django and to the
#: audits that sweep for it, so an absence check that saw only the file would be
#: satisfied by the directory.
ABSENT_SURFACES: Final[tuple[str, ...]] = ("api", "admin", "serializers", "tasks", "urls", "views")

#: The one subpackage this application must have.
#: `tests/unit/django_apps/test_migration_completeness.py` compares the declared
#: models against the migration graph, and an application whose `migrations/` is
#: not a package contributes nothing to that graph while still declaring tables.
MIGRATIONS_PACKAGE: Final[str] = "migrations"

#: The reviewed-data tree, which is a directory and deliberately **not** a
#: package: it holds the TOML `policies/parameters.py` reads and a README stating
#: the contract, and an `__init__.py` in it would make reviewed data importable
#: and put it in front of every audit that sweeps modules.
#: `collectors/data/` is the precedent and has the same shape.
DATA_DIRECTORY: Final[str] = "data"

#: The management-command package `CPM-OPERATE-S02` added, holding `run_policy`
#: -- the one command that may derive a policy version, because the parameter
#: file it reads lives beside it and `core/tasks.py` forbids a default. A
#: package rather than a `tasks.py`: the task it enqueues is `core`'s, and this
#: application still declares no work of its own to schedule.
MANAGEMENT_PACKAGE: Final[str] = "management"

#: What ships inside it. Named so that a file added there is a deliberate act and
#: a file *lost* there fails here rather than at the first policy run in a
#: container -- `tests/integration/test_import_resolution.py` asserts the same
#: names are inside the built wheel.
EXPECTED_DATA_FILES: Final[tuple[str, ...]] = ("README.md", "policy-parameters.toml")

#: The migrations this application ships, by name. A second, weaker claim beside
#: the package check above, and meant to be edited: it is what makes an *added*
#: migration a deliberate act, so a file generated by `makemigrations` and left
#: under its autogenerated name fails here rather than landing with a name nobody
#: chose.
EXPECTED_MIGRATIONS: Final[tuple[str, ...]] = (
    "0001_package_currency.py",
    "0002_package_feedstock_presence.py",
    "0003_package_vulnerability.py",
    "0004_package_license.py",
    "0005_package_remediation.py",
    "0006_package_python_readiness.py",
    "0007_package_priority.py",
    "0008_package_work_type.py",
)


def test_the_application_is_installed() -> None:
    """A name naming no installed application would make every case below vacuous."""
    assert APPLICATION_NAME in settings.INSTALLED_APPS


def test_the_application_resolves_from_the_second_import_root() -> None:
    """The import that the second root exists to make work.

    Nothing is on `sys.path` for this. The editable install's redirecting finder
    maps `conda_sentinel` straight onto
    `src/django_apps/conda_sentinel/__init__.py`, and the
    application resolves as a subpackage through that.

    The assertion is on the resolved file's location, which is what makes this
    about the *second* root: a copy of the package under `src/` would satisfy the
    import and fail here.
    """
    module = importlib.import_module(APPLICATION_NAME)

    assert module.__file__ is not None
    assert Path(module.__file__).is_relative_to(
        APPLICATION_ROOT / "conda_sentinel" / APPLICATION_LABEL,
    )


def test_the_app_config_names_the_application_without_the_root() -> None:
    """`name` is the installed dotted path, and the label falls out of it."""
    assert PoliciesConfig.name == APPLICATION_NAME
    assert apps.get_app_config(APPLICATION_LABEL).name == APPLICATION_NAME


def test_the_app_config_declares_no_default_auto_field() -> None:
    """`DEFAULT_AUTO_FIELD` is set once, project-wide, in `config/settings/base.py`.

    A per-app restatement would be a second declaration of one decision, and the
    failure it invites is the quiet kind: the two agree today, somebody changes
    the project setting, and one application's tables keep the old key width with
    nothing failing. `CPM-AD-3` fixes the surrogate integer for every table in
    this product, so there is one place to say so.
    """
    assert "default_auto_field" not in vars(PoliciesConfig)


def test_the_app_config_declares_no_label_of_its_own() -> None:
    """The label is Django's derivation from `name`, not a second spelling.

    An explicit `label = "policies"` would be the same value written twice, and
    the day the two disagreed the tables, the migrations and the app registry
    would follow the explicit one while every reference to the dotted name
    followed the other.
    """
    assert "label" not in vars(PoliciesConfig)
    assert apps.get_app_config(APPLICATION_LABEL).label == APPLICATION_LABEL


def test_the_application_is_ordered_after_the_stage_two_owner() -> None:
    """Read off the resolved registry, because `local.py` prepends to `INSTALLED_APPS`.

    `tests/unit/startup/test_installed_apps_ordering.py` holds the structural
    half of this and reconciles the whole adopted roster; this is the same
    invariant stated for the application this story adds -- and it matters more
    here than for `core` and `identity`, because this application declares a
    `ready()` and would therefore actually run before stage two if it preceded
    the owner.
    """
    names = [app_config.name for app_config in apps.get_app_configs()]

    assert names.index(STAGE_TWO_OWNER_NAME) < names.index(APPLICATION_NAME)


def test_the_application_is_declared_in_local_apps_last() -> None:
    """Both halves of the adoption, on the installing side.

    Appended rather than inserted, which is what puts it behind the stage-2 owner
    by construction and behind every application it depends on -- `core` for the
    pass machinery, `identity` for the package and the authority order, and
    `collectors` for the four evidence tables it reads.

    Last, and that is asserted rather than merely true: `CPM-AD-21` keeps the
    pass registry in *declaration* order because a later pass may read an earlier
    pass's derived rows, so the position of the application that registers a pass
    is part of what has been declared about it.
    """
    local_apps = list(settings.LOCAL_APPS)

    assert local_apps.index(STAGE_TWO_OWNER_NAME) < local_apps.index(APPLICATION_NAME)
    assert local_apps.index(PRECEDING_APPLICATION_NAME) < local_apps.index(APPLICATION_NAME)
    assert local_apps[local_apps.index(APPLICATION_NAME) + 1 :] == list(APPLICATIONS_AFTER)


def test_nothing_after_this_application_registers_a_pass() -> None:
    """The rule the "last" assertion above was really about, stated directly.

    `CPM-AD-21` keeps the registry in declaration order so a later pass can read an
    earlier pass's derived rows. What that forbids is an application registering a
    **pass** after this one -- not an application existing after it, and not an
    application having a `ready()`.

    **The earlier version of this case asserted the second thing**, which was a
    proxy: `conda_sentinel.surface` had no `ready()` at all, so "declares no
    `ready()`" happened to hold. `CPM-APP-S05` gave `conda_sentinel.workflow` one --
    it registers an *after-run step*, which is a different registry and runs after
    every pass has finished -- and the proxy failed while the rule it stood for was
    untouched. So the rule is now checked directly, by sweeping for the call.
    """
    offenders = {name: sorted(calls) for name in APPLICATIONS_AFTER if (calls := _pass_registrations_in(name))}

    assert offenders == {}, f"these applications register a pass after {APPLICATION_NAME}: {offenders}"


def _pass_registrations_in(dotted: str) -> set[str]:
    """Return every module of an application that calls `register_pass`.

    Args:
        dotted: The application's dotted name.

    Returns:
        The offending modules, by file name.

    """
    package = SRC_ROOT / "django_apps" / dotted.replace(".", "/")
    return {path.name for path in package.rglob("*.py") if REGISTER_PASS_CALL in path.read_text(encoding="utf-8")}


def test_the_ready_hook_adopted_this_applications_passes() -> None:
    """The registration `policies/apps.py` performs, asserted against the live registry.

    This is the case that would fail if `ready()` were removed, if the
    application were dropped from `LOCAL_APPS`, or if the tuple in the hook were
    emptied -- each of which would silently return
    `tests/unit/django_apps/test_pass_ownership_audit.py` to sweeping nothing,
    which is the state that module spent an epic being honest about.

    Asserted by *identity*, not by name: two classes under one name are what
    `register_pass` refuses, and a name check would pass for whichever of them
    got there.
    """
    assert pass_registrations().get(POLICY_NAME) is CurrencyPass
    assert pass_registrations().get(FEEDSTOCK_POLICY_NAME) is FeedstockPresencePass
    assert pass_registrations().get(VULNERABILITY_POLICY_NAME) is VulnerabilityPass
    assert pass_registrations().get(LICENCE_POLICY_NAME) is LicensePass
    assert pass_registrations().get(REMEDIATION_POLICY_NAME) is RemediationPass
    assert set(ADOPTED_PASS_NAMES) <= set(pass_registrations())


def test_the_passes_were_adopted_in_the_order_the_hook_declares() -> None:
    """`ready()` argues its ordering at length, so the ordering is asserted.

    `core/policy.py` keeps *registration* order where the collector registry sorts
    by name, because `CPM-AD-21` lets a later pass read an earlier pass's derived
    rows for the same run -- and `policies/apps.py` spends four paragraphs saying
    which order it chose and why each pass sits where it does. Every other
    assertion about the roster sorts or takes a set, so reordering that tuple
    failed nothing anywhere: the reasoning was a claim about a decision no case
    could tell had been made.

    Removal is caught elsewhere. This is the other half, and it is the half the
    docstring is actually about.
    """
    adopted = [policy_pass.name for policy_pass in registered_passes()]

    assert [name for name in adopted if name in set(ADOPTED_PASS_NAMES)] == list(ADOPTED_PASS_NAMES)


def test_the_adopted_pass_owns_the_rollup_column_it_declares() -> None:
    """The other half of adoption: the contribution the registry recorded.

    A pass can be registered and own nothing -- `contributes` is legitimately
    empty for a pass whose whole output is its own derived table -- so
    registration alone does not say the column arrived. This does, in both
    directions: the rollup offers the column, and this pass is who owns it.
    """
    assert ROLLUP_COLUMN in contributable_columns()
    assert column_owners().get(ROLLUP_COLUMN) == POLICY_NAME
    assert FEEDSTOCK_ROLLUP_COLUMN in contributable_columns()
    assert column_owners().get(FEEDSTOCK_ROLLUP_COLUMN) == FEEDSTOCK_POLICY_NAME
    assert ROLLUP_COLUMN != FEEDSTOCK_ROLLUP_COLUMN, (
        "two adopted passes claiming one column is what CPM-AD-11 forbids, and the registry would have "
        "refused the second -- so this case must be about two columns or it is about nothing"
    )


def test_the_ready_hook_can_run_twice_without_aborting_boot() -> None:
    """`AppConfig.ready` is Django's to call, and a second `django.setup()` calls it again.

    `register_pass` refuses a duplicate name, correctly -- two different classes
    under one name share an entry in the rollup's version map and are
    indistinguishable in every report. What the guard in the hook stops is that
    refusal firing on an adoption that had *already succeeded*, which would be a
    component that will not start for no reason anybody chose.

    Driven rather than read: calling `ready()` again is the exact thing a second
    `django.setup()` does, and asserting the source contains an `if` would be
    asserting the shape of the fix rather than the property.
    """
    before = pass_registrations()

    apps.get_app_config(APPLICATION_LABEL).ready()

    assert pass_registrations() == before
    assert column_owners().get(ROLLUP_COLUMN) == POLICY_NAME
    assert column_owners().get(FEEDSTOCK_ROLLUP_COLUMN) == FEEDSTOCK_POLICY_NAME


def test_the_application_ships_a_migrations_package() -> None:
    """The half `test_migration_completeness.py` depends on, asserted directly.

    That audit compares this repository's declared models against the migration
    graph and fails on any difference. An application whose `migrations/` is a
    plain directory rather than a package contributes nothing to the graph --
    Django finds no migrations for it, treats it as unmigrated, and the audit's
    `ignore_no_migrations=True` loader is what keeps that from being an error. So
    the models would declare tables that no migration builds, and the failure
    would land at `migrate` on somebody else's machine.
    """
    package = Path(apps.get_app_config(APPLICATION_LABEL).path)
    migrations = package / MIGRATIONS_PACKAGE

    assert migrations.is_dir(), migrations
    assert (migrations / "__init__.py").is_file()
    assert tuple(sorted(path.name for path in migrations.glob("0*.py"))) == EXPECTED_MIGRATIONS


def test_the_application_declares_no_urls_serializers_or_tasks() -> None:
    """The surface this story does not build, asserted rather than merely omitted.

    `CPM-AD-19` gives a domain application `urls.py`, an `api/` subpackage and
    `tasks.py` when it has views or work to schedule. This one has neither: a
    pass is executed by `core/policy_run.py` inside the run `core/tasks.py`
    schedules, and every read surface is `CPM-EP-APP`'s. An empty `urls.py` added
    "for later" would be routed by nothing and would still have to be reviewed,
    and a `tasks.py` would be swept by
    `tests/unit/django_apps/test_task_routing_audit.py` for a route it has no
    task to declare.

    **Both shapes of each surface.** A module comparison alone is satisfied by a
    `tasks/` package, which is the same surface with the same consequences and a
    different filesystem shape -- so each name is checked as a directory as well.

    Three subdirectories are expected and each has a case of its own:
    `migrations/`, which must be a package; `data/`, which must not be; and
    `management/`, the command package `CPM-OPERATE-S02` added. The equality
    here is what makes a *fourth* one -- an `api/` package, a stray fixture tree
    -- a failure rather than something a later reader has to notice.
    """
    package = Path(apps.get_app_config(APPLICATION_LABEL).path)

    assert package.is_dir(), package
    assert sorted(path.name for path in package.glob("*.py")) == sorted(EXPECTED_MODULES)

    present = [name for name in ABSENT_SURFACES if (package / name).exists() or (package / f"{name}.py").exists()]
    assert present == [], f"this story builds none of these surfaces, but they are present: {present}"

    subdirectories = sorted(path.name for path in package.iterdir() if path.is_dir() and not path.name.startswith("__"))
    assert subdirectories == sorted([DATA_DIRECTORY, MANAGEMENT_PACKAGE, MIGRATIONS_PACKAGE])


def test_the_management_package_holds_exactly_the_policy_run_command() -> None:
    """`management/commands/` is a package pair holding `run_policy` and nothing else.

    Both `__init__.py` files, because Django discovers commands only through an
    importable `management.commands` package -- a plain directory would leave
    `pixi run policy-run` reporting `Unknown command` at whatever hour a
    deployment scheduled it. Exactly one command, because `CPM-OPERATE-S02` put
    the other two under `collectors/` and a fourth was its Block-If.
    """
    management = Path(apps.get_app_config(APPLICATION_LABEL).path) / MANAGEMENT_PACKAGE
    commands = management / "commands"

    assert (management / "__init__.py").is_file()
    assert (commands / "__init__.py").is_file()
    assert sorted(path.name for path in commands.glob("*.py") if path.name != "__init__.py") == ["run_policy.py"]


def test_the_reviewed_parameter_tree_ships_beside_the_module_that_reads_it() -> None:
    """`CPM-AD-14`'s governed data, in the one place `policies/parameters.py` looks.

    The reader resolves the file relative to its own `__file__` rather than from
    `BASE_DIR`, because the `src/` segment does not exist in the wheel layout --
    so a tree that moved, or a file dropped from it, would surface as a refused
    policy run in a container rather than as a failure here.
    `tests/integration/test_import_resolution.py` asserts the same file is inside
    the built artifact; this asserts it is where the reader will look.

    **`data/` carries no `__init__.py`, and that is asserted rather than assumed.**
    A package there would make reviewed data importable and would put a TOML tree
    in front of every audit that sweeps modules; `collectors/data/` is the
    precedent and has the same shape.
    """
    data = Path(apps.get_app_config(APPLICATION_LABEL).path) / DATA_DIRECTORY

    assert data.is_dir(), data
    assert not (data / "__init__.py").exists(), "reviewed data is not a package"
    assert sorted(path.name for path in data.iterdir() if path.is_file()) == sorted(EXPECTED_DATA_FILES)


def test_the_parameter_reader_points_at_that_tree() -> None:
    """The two halves of "the file ships beside the reader", reconciled.

    The case above says the tree is beside the application; this says the module
    that reads it computes the same path. Two facts that agree today and are one
    refactor apart from not.

    **Functions rather than module constants**, because the resolution is lazy:
    an installation that shipped the modules and dropped the `data/` tree fails
    the policy run that needed it rather than refusing to boot a web process that
    would never have read it, which is what `CPM-AD-23`'s per-package containment
    asks of a domain application's faults.
    """
    data = Path(apps.get_app_config(APPLICATION_LABEL).path) / DATA_DIRECTORY

    assert parameters_file().parent == parameters_directory()
    assert parameters_directory().resolve() == data.resolve()
    assert parameters_file().name in EXPECTED_DATA_FILES
