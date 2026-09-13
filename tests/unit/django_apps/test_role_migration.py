"""Shape and authorship assertions for the role-group migration. No database.

Four properties, none of which a run of the migration would demonstrate:

* it is **not elidable**, so a future squash cannot take the guarantee that the
  role groups exist away with it;
* it **depends on the one writer's own migration**, so the mechanism it calls has
  already run against the database before any role group is asked for;
* it **creates no `Group` of its own**, which is a property of the source rather
  than of any run -- a second writer is wrong the moment it is written, whether
  or not a test ever executes it;
* it **names no group**, for the reason `roles.py` names none: the names are the
  operator's and a literal here would be a second, silent source for one.

The scan for the third is `tests/group_writers.py`, the shared detector
`tests/unit/users/test_provisioning.py` runs over the whole of `src/`. It is
asked again here, of this one file, because a migration that creates groups
inline is the exact shape AD-27 was written against -- so the failure should name
this story's file rather than only the repository-wide audit.

The behavioural half -- the rows that appear, the collision with a designated
group, and the rollback -- is in
`tests/integration/django_apps/test_role_groups.py`.
"""

from __future__ import annotations

from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from django.conf import settings
from django.db import migrations

from conda_sentinel.core import roles
from tests.group_writers import group_creation_verbs

if TYPE_CHECKING:
    from types import ModuleType

MIGRATION_MODULE = "conda_sentinel.core.migrations.0001_provision_role_groups"

# `auth` carries `Group`; `users.0003` is the one writer's own migration.
EXPECTED_DEPENDENCIES = [
    ("auth", "0012_alter_user_first_name_max_length"),
    ("users", "0003_provision_designated_groups"),
]

# `CPM-IDENTITY-S05`'s grant, which is a second migration rather than an edit to
# the one above: a data migration that has already run against a deployed
# database is not re-run by editing it, so a grant added to `0001` would reach no
# environment that had migrated before it.
GRANT_MIGRATION_MODULE = "conda_sentinel.core.migrations.0005_grant_identity_override"

# Five entries, and the shape of the graph rather than decoration. See
# `test_the_grant_migration_runs_after_the_model_and_after_the_role_groups`.
EXPECTED_GRANT_DEPENDENCIES = [
    ("auth", "0012_alter_user_first_name_max_length"),
    ("contenttypes", "0002_remove_content_type_name"),
    ("core", "0001_provision_role_groups"),
    ("core", "0004_collection_run_package"),
    ("identity", "0003_identity_override"),
]

# `CPM-OPERATE-S03`'s grant: the second governed write's permission, on the
# first's terms exactly -- a further migration rather than an edit to `0005`.
INVENTORY_GRANT_MIGRATION_MODULE = "conda_sentinel.core.migrations.0011_grant_inventory_change"

# Five entries again: `collectors.0013_inventory` is the model the codename hangs
# off, `core.0005` the first grant this one converges with, `core.0010` this
# application's own latest.
EXPECTED_INVENTORY_GRANT_DEPENDENCIES = [
    ("auth", "0012_alter_user_first_name_max_length"),
    ("collectors", "0013_inventory"),
    ("contenttypes", "0002_remove_content_type_name"),
    ("core", "0005_grant_identity_override"),
    ("core", "0010_background_jobs"),
]

# The module the role declaration lives in, read by the revocation case below.
# Off `__file__` for the reason `migration_source` is: a hand-built path is a
# second copy of a layout `tests/unit/test_import_roots.py` owns.
ROLES_MODULE = Path(roles.__file__ or "")

# The property `preserve_existing=True` costs, which both files have to state.
# Searched for in prose with the comment markers and the line breaks taken out,
# because it is a sentence a person wrote and wrapping it differently is not
# changing it.
REVOCATION_NOTE = "revoke by omission"


def _prose(path: Path) -> str:
    """Return a module's text with comment markers and line breaks flattened.

    A sentence written across three comment lines is one sentence, and a substring
    search over the raw file cannot see it. Nothing here parses: the subject is
    prose, and the point is that the *explanation* is present rather than that any
    particular expression is.

    Args:
        path: The module to read.

    Returns:
        The source with `#:` and `#` markers removed and all whitespace collapsed
        to single spaces.

    """
    stripped = path.read_text(encoding="utf-8").replace("#:", " ").replace("#", " ")
    return " ".join(stripped.split())


@pytest.fixture
def migration() -> ModuleType:
    """The migration module, imported per case rather than at module scope.

    An import at module scope would make a broken migration fail *collection* of
    this file, which reports as an error with no test name attached and takes the
    other cases with it.
    """
    return import_module(MIGRATION_MODULE)


@pytest.fixture
def migration_source(migration: ModuleType) -> Path:
    """The migration's own file, resolved from the imported module.

    Read off `__file__` rather than rebuilt from `settings.BASE_DIR` and the
    second import root's layout: that layout is
    `tests/unit/test_import_roots.py`'s to assert, a hand-built path is a second
    copy of it, and a guessed path that stopped existing would need a guard of
    its own to keep the scan below from passing vacuously.
    """
    assert migration.__file__ is not None
    return Path(migration.__file__)


def test_the_migration_is_a_single_run_python_operation(migration: ModuleType) -> None:
    """One data operation, forward and reverse, and no schema change.

    `core` has no models, so a schema operation appearing here would mean a model
    was added without its own migration alongside it.
    """
    operations = migration.Migration.operations

    assert [type(operation).__name__ for operation in operations] == ["RunPython"]
    operation = operations[0]
    assert isinstance(operation, migrations.RunPython)
    assert operation.code is migration.forward
    assert operation.reverse_code is migration.reverse


def test_the_migration_is_not_elidable(migration: ModuleType) -> None:
    """Squashing it away would take the only guarantee the role groups exist.

    An asserted role would then resolve to no row, be ignored and logged under
    AD-12, and the person would authenticate with no access at all.
    """
    assert migration.Migration.operations[0].elidable is False


def test_the_migration_depends_on_the_one_writers_own_migration(migration: ModuleType) -> None:
    """Ordering, not decoration.

    `users.0003_provision_designated_groups` is where the provisioning mechanism
    first runs against a database. Depending on it means the platform's own
    groups exist before the product's do, in one `migrate` invocation, in a
    declared order rather than in whichever order the graph happened to resolve.
    """
    assert sorted(migration.Migration.dependencies) == sorted(EXPECTED_DEPENDENCIES)


def test_the_migration_creates_no_group_of_its_own(migration_source: Path) -> None:
    """AD-27: group creation has exactly one call site, and this is not it."""
    assert group_creation_verbs(migration_source) == set()


def test_the_migration_names_no_group(migration_source: Path) -> None:
    """No configured group name appears anywhere in the migration's source.

    The same assertion `tests/unit/users/test_provisioning.py` makes about the
    platform's provisioning module and `test_roles.py` makes about `roles.py`,
    asked of the third file that could hold one. A literal here would survive
    every rename of the environment variables, and would be provisioned once and
    then never corrected -- the migration is applied exactly once.
    """
    source = migration_source.read_text(encoding="utf-8")
    contract = settings.ROLE_CONTRACT

    for name in (contract.security_reviewer, contract.packaging_engineer, contract.leadership):
        assert name, "the suite must run against a configured role contract for this to mean anything"
        assert name not in source, f"the migration hardcodes the configured group name {name!r}"


# ---------------------------------------------------------------------------
# `core/0005_grant_identity_override`: the first real grant, and its rollback.
# ---------------------------------------------------------------------------


@pytest.fixture
def grant_migration() -> ModuleType:
    """The grant migration, imported per case for the reason its sibling is.

    An import at module scope would make a broken migration fail *collection* of
    this file, which reports as an error with no test name attached and takes the
    other cases with it.
    """
    return import_module(GRANT_MIGRATION_MODULE)


@pytest.fixture
def grant_migration_source(grant_migration: ModuleType) -> Path:
    """The grant migration's own file, resolved from the imported module.

    Read off `__file__` on the same terms as `migration_source`: a hand-built path
    is a second copy of a layout `tests/unit/test_import_roots.py` owns, and a
    guessed path that stopped existing would need a guard of its own to keep the
    scans below from passing vacuously.
    """
    assert grant_migration.__file__ is not None
    return Path(grant_migration.__file__)


def test_the_grant_migration_is_a_single_run_python_operation(grant_migration: ModuleType) -> None:
    """One data operation, forward and reverse, and no schema change.

    The table this grants against is `identity/0003_identity_override`'s. A schema
    operation appearing here would mean a model had been added to `core` without
    its own migration beside it, which is the thing
    `tests/unit/django_apps/test_migration_completeness.py` measures from the
    other side.
    """
    operations = grant_migration.Migration.operations

    assert [type(operation).__name__ for operation in operations] == ["RunPython"]
    operation = operations[0]
    assert isinstance(operation, migrations.RunPython)
    assert operation.code is grant_migration.forward
    assert operation.reverse_code is grant_migration.reverse


def test_the_grant_migration_is_not_elidable(grant_migration: ModuleType) -> None:
    """Squashing it away would take the only guarantee the leadership grant exists.

    Every override would then be refused as forbidden, with `_resolve_permissions`
    silent because nothing asked for the codename -- a product whose one governed
    human write is unreachable, and no log line saying why.
    """
    assert grant_migration.Migration.operations[0].elidable is False


def test_the_grant_migration_runs_after_the_model_and_after_the_role_groups(
    grant_migration: ModuleType,
) -> None:
    """Ordering, not decoration, and three of the five entries are load-bearing.

    `identity.0003_identity_override` is what makes the codename resolvable at
    all: `create_permissions` in `forward` needs the model in the historical
    state, and without the dependency it can run against a state that has never
    heard of `identity_overrides`. `core.0001_provision_role_groups` is what
    created the rows this attaches a permission to. `core.0004_collection_run_package` is
    this application's own latest, so `core`'s migrations stay a single line
    rather than two leaves the graph cannot order -- which is a `migrate` that
    refuses to run at all rather than a subtle fault.
    """
    assert sorted(grant_migration.Migration.dependencies) == sorted(EXPECTED_GRANT_DEPENDENCIES)


def test_the_grant_migration_creates_no_group_of_its_own(grant_migration_source: Path) -> None:
    """AD-27: group creation has exactly one call site, and this is not it."""
    assert group_creation_verbs(grant_migration_source) == set()


def test_the_grant_migration_names_no_group(grant_migration_source: Path) -> None:
    """No configured group name appears anywhere in the grant migration's source.

    The fourth file that could hold one, on the same terms as the three before it.
    A literal here would be provisioned once and then never corrected, because a
    migration is applied exactly once.
    """
    source = grant_migration_source.read_text(encoding="utf-8")
    contract = settings.ROLE_CONTRACT

    for name in (contract.security_reviewer, contract.packaging_engineer, contract.leadership):
        assert name, "the suite must run against a configured role contract for this to mean anything"
        assert name not in source, f"the grant migration hardcodes the configured group name {name!r}"


def test_the_grant_migration_provisions_without_revoking(grant_migration_source: Path) -> None:
    """`preserve_existing=True`, which is what a *secondary* declaration owes a shared row.

    The role contract writes to the same `auth_group` table the claims contract
    does, and nothing stops an operator pointing `CPM_LEADERSHIP_GROUP` at the
    group `COMPONENT_STAFF_GROUP` already names. A pass calling `set` with its own
    declaration would clear `users.view_user` and `users.change_user` from that
    row, and nothing would refuse: `stage_two`'s condition asks whether the
    designated rows *exist*, not what they hold, so the only symptom is staff
    members landing on an empty admin index.

    Asserted over the source rather than by running the pass, because the keyword
    is the decision -- a behavioural case would pass on a `set` that happened to
    be handed a superset.
    """
    source = grant_migration_source.read_text(encoding="utf-8")

    assert "preserve_existing=True" in source
    assert "preserve_existing=False" not in source


def test_the_role_declaration_cannot_revoke_by_omission(grant_migration_source: Path) -> None:
    """The trade `preserve_existing=True` buys, stated where somebody would look for it.

    `provision_groups`' own docstring is explicit: "a secondary declaration cannot
    revoke by omission; whoever removes one of its codenames has to say so."
    Deleting `IDENTITY_OVERRIDE_PERMISSION` from `ROLE_GROUP_PERMISSIONS` would
    therefore leave every already-provisioned deployment holding it, and the only
    thing that takes it away is a migration that detaches it -- the shape this
    migration's own `reverse` already has.

    That is not a defect and it is not obvious, so both files say so and this is
    what keeps them saying it. A person who deletes the codename and expects the
    grant to go is otherwise told nothing at all.
    """
    assert REVOCATION_NOTE in _prose(grant_migration_source)
    assert REVOCATION_NOTE in _prose(ROLES_MODULE)


# ---------------------------------------------------------------------------
# `core/0011_grant_inventory_change`: the second grant, on the first's terms.
# ---------------------------------------------------------------------------


@pytest.fixture
def inventory_grant_migration() -> ModuleType:
    """The second grant migration, imported per case for the reason its siblings are."""
    return import_module(INVENTORY_GRANT_MIGRATION_MODULE)


@pytest.fixture
def inventory_grant_migration_source(inventory_grant_migration: ModuleType) -> Path:
    """The second grant migration's own file, resolved from the imported module."""
    assert inventory_grant_migration.__file__ is not None
    return Path(inventory_grant_migration.__file__)


def test_the_inventory_grant_is_a_single_non_elidable_run_python_operation(
    inventory_grant_migration: ModuleType,
) -> None:
    """One data operation, forward and reverse, never squashed away."""
    operations = inventory_grant_migration.Migration.operations

    assert [type(operation).__name__ for operation in operations] == ["RunPython"]
    operation = operations[0]
    assert isinstance(operation, migrations.RunPython)
    assert operation.code is inventory_grant_migration.forward
    assert operation.reverse_code is inventory_grant_migration.reverse
    assert operation.elidable is False


def test_the_inventory_grant_runs_after_the_model_and_after_the_first_grant(
    inventory_grant_migration: ModuleType,
) -> None:
    """`collectors.0013` makes the codename resolvable; `core.0005` is what this converges with."""
    assert sorted(inventory_grant_migration.Migration.dependencies) == sorted(EXPECTED_INVENTORY_GRANT_DEPENDENCIES)


def test_the_inventory_grant_creates_and_names_no_group(inventory_grant_migration_source: Path) -> None:
    """AD-27 and the no-names rule, for the fifth file that could break either."""
    source = inventory_grant_migration_source.read_text(encoding="utf-8")
    contract = settings.ROLE_CONTRACT

    assert group_creation_verbs(inventory_grant_migration_source) == set()
    for name in (contract.security_reviewer, contract.packaging_engineer, contract.leadership):
        assert name, "the suite must run against a configured role contract for this to mean anything"
        assert name not in source, f"the inventory grant migration hardcodes the configured group name {name!r}"


def test_the_inventory_grant_provisions_without_revoking_and_says_so(inventory_grant_migration_source: Path) -> None:
    """`preserve_existing=True`, and the trade it buys stated where somebody would look."""
    source = inventory_grant_migration_source.read_text(encoding="utf-8")

    assert "preserve_existing=True" in source
    assert "preserve_existing=False" not in source
    assert REVOCATION_NOTE in _prose(inventory_grant_migration_source)


def test_each_grant_provisions_only_its_own_codename(
    grant_migration_source: Path, inventory_grant_migration_source: Path
) -> None:
    """The narrowing `CPM-OPERATE-S03` made to both grants, asserted over the source.

    A grant that provisioned the whole contract logged the other grant's codename
    as unresolved on every fresh `migrate` -- `0005` runs before
    `collectors/0013_inventory` exists. Each `forward` now filters the contract to
    the one permission it is about, and this is what keeps a third grant doing the
    same rather than reintroducing the warning.
    """
    first = grant_migration_source.read_text(encoding="utf-8")
    second = inventory_grant_migration_source.read_text(encoding="utf-8")

    assert "code == IDENTITY_OVERRIDE_PERMISSION" in first
    assert "INVENTORY_CHANGE_PERMISSION" not in first
    assert "code == INVENTORY_CHANGE_PERMISSION" in second
    assert "IDENTITY_OVERRIDE_PERMISSION" not in second
