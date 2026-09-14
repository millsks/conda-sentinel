"""Tests for the product's role contract.

Everything here is pure: an `environ.Env` in, three names out. No database, no
network, and no read of the real environment -- the `Env` instances are
constructed in-test and `monkeypatch` owns every variable that is set.

Shaped after `tests/unit/authorization/test_claims.py`, which is the contract
this one mirrors. The two claims the file makes are the two that keep a role
group name out of the source: the variables read are the variables declared and
documented, and no configured name appears in `roles.py`, the module that
declares the contract. The same scan is run over the migration that provisions
from it in `tests/unit/django_apps/test_role_migration.py`.

The behavioural half -- the rows that actually appear, and a claim asserting a
role group resolving to one -- is in
`tests/integration/django_apps/test_role_groups.py`.
"""

from __future__ import annotations

import ast
import dataclasses
import re
from itertools import combinations
from pathlib import Path

import environ
import pytest
from django.conf import settings

from conda_sentinel.core import roles
from conda_sentinel.core.roles import IDENTITY_APP_LABEL
from conda_sentinel.core.roles import IDENTITY_OVERRIDE_CODENAME
from conda_sentinel.core.roles import IDENTITY_OVERRIDE_PERMISSION
from conda_sentinel.core.roles import INVENTORY_APP_LABEL
from conda_sentinel.core.roles import INVENTORY_CHANGE_CODENAME
from conda_sentinel.core.roles import INVENTORY_CHANGE_PERMISSION
from conda_sentinel.core.roles import LEADERSHIP
from conda_sentinel.core.roles import PACKAGING_ENGINEER
from conda_sentinel.core.roles import RECOLLECT_APP_LABEL
from conda_sentinel.core.roles import RECOLLECT_CODENAME
from conda_sentinel.core.roles import RECOLLECT_PERMISSION
from conda_sentinel.core.roles import ROLE_ENVIRONMENT_VARIABLES
from conda_sentinel.core.roles import ROLE_GROUP_PERMISSIONS
from conda_sentinel.core.roles import SECURITY_REVIEWER
from conda_sentinel.core.roles import RoleContract
from conda_sentinel.core.roles import load_role_contract
from conda_sentinel.core.roles import role_group_permissions

# The three slots the brief's role table names, written out rather than derived
# from the module under test -- a derived set would agree with whatever it found.
ROLE_SLOTS = frozenset({"security_reviewer", "packaging_engineer", "leadership"})

# The module the contract is declared in, scanned below for a hardcoded name and
# for a forbidden import. Read off `__file__` rather than rebuilt from
# `settings.BASE_DIR` and the second import root's layout: that layout is
# `tests/unit/test_import_roots.py`'s to assert, and a hand-built path here would
# be a second copy of it that could stop pointing at anything without saying so.
ROLES_MODULE = Path(roles.__file__ or "")

# Packages `roles.py` must not import. It is imported from
# `config/settings/base.py`, so it loads before the app registry is populated and
# before `AUTH_USER_MODEL` is resolvable; either import would make the settings
# module unloadable, and both its own docstring and the comment beside
# `ROLE_CONTRACT` in `base.py` rest on this being true.
FORBIDDEN_IMPORT_ROOTS = ("django.apps", "django.contrib.auth")

# Names that look nothing like the ones the suite is configured with, so a case
# passing on them cannot be passing on a literal in the source.
A_REVIEWER_GROUP = "wharf-inspectors"
AN_ENGINEER_GROUP = "wharf-riggers"
A_LEADERSHIP_GROUP = "wharf-masters"

# The fixture `config/settings/test.py` declares, pinned here so the override
# cannot be deleted or drift back to whatever `CPM_` variables a developer's
# shell happens to hold.
TEST_ROLE_CONTRACT = RoleContract(
    security_reviewer="cpm-security-reviewer",
    packaging_engineer="cpm-packaging-engineer",
    leadership="cpm-leadership",
)


@pytest.fixture
def empty_env(monkeypatch: pytest.MonkeyPatch) -> environ.Env:
    """An `environ.Env` with none of the three role variables set."""
    for name in ROLE_ENVIRONMENT_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    return environ.Env()


# ---------------------------------------------------------------------------
# AC #2 -- each of the three names is read from the environment, with no default.
# ---------------------------------------------------------------------------


def test_all_three_names_are_read_from_the_environment(
    empty_env: environ.Env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The configured row of the matrix: all three set, all three returned."""
    monkeypatch.setenv("CPM_SECURITY_REVIEWER_GROUP", A_REVIEWER_GROUP)
    monkeypatch.setenv("CPM_PACKAGING_ENGINEER_GROUP", AN_ENGINEER_GROUP)
    monkeypatch.setenv("CPM_LEADERSHIP_GROUP", A_LEADERSHIP_GROUP)

    contract = load_role_contract(empty_env)

    assert contract.security_reviewer == A_REVIEWER_GROUP
    assert contract.packaging_engineer == AN_ENGINEER_GROUP
    assert contract.leadership == A_LEADERSHIP_GROUP
    assert contract.is_configured is True


def test_an_unset_contract_defaults_no_name(empty_env: environ.Env) -> None:
    """Nothing is defaulted, so an unset variable stays unset.

    A plausible default -- `security-reviewers`, say -- would turn a missing
    configuration into a wrong one: three groups provisioned under names nobody
    declared, and an IdP asserting the real ones resolving to nothing.
    """
    contract = load_role_contract(empty_env)

    assert contract == RoleContract(security_reviewer="", packaging_engineer="", leadership="")
    assert contract.is_configured is False


#: Every proper non-empty subset of the three variables: the one- and two-name
#: partial configurations. Derived rather than written out so that a fourth role
#: slot arriving later cannot leave a partial shape untested.
PARTIAL_CONFIGURATIONS = [subset for size in (1, 2) for subset in combinations(ROLE_ENVIRONMENT_VARIABLES, size)]


@pytest.mark.parametrize(
    "configured",
    PARTIAL_CONFIGURATIONS,
    ids=["+".join(subset) for subset in PARTIAL_CONFIGURATIONS],
)
def test_a_partial_contract_is_not_configured(
    empty_env: environ.Env,
    monkeypatch: pytest.MonkeyPatch,
    configured: tuple[str, ...],
) -> None:
    """Partial configuration is unconfigured, at one name set and at two.

    Two roles provisioned and one silently absent is the shape of a
    misconfiguration that surfaces much later as a permissions bug: the third
    role's people authenticate, assert a group that matches no row, and are
    admitted with no access rather than refused. The two-name subsets are the
    ones that shape actually takes, so they are parametrized alongside the
    one-name case rather than left to the reader to assume.
    """
    for name in configured:
        monkeypatch.setenv(name, A_REVIEWER_GROUP)

    assert load_role_contract(empty_env).is_configured is False


@pytest.mark.parametrize("blank", ["  ", "\n", "\t\n "], ids=["spaces", "newline", "mixed"])
def test_a_blank_value_reads_as_unset(
    empty_env: environ.Env,
    monkeypatch: pytest.MonkeyPatch,
    blank: str,
) -> None:
    """A block scalar in a ConfigMap, or a trailing space in a `.env` line.

    Without the strip the field would be truthy, `is_configured` would report
    True, and provisioning would create a `Group` whose name is whitespace -- a
    row no claim can ever match and nothing would delete.
    """
    monkeypatch.setenv("CPM_SECURITY_REVIEWER_GROUP", A_REVIEWER_GROUP)
    monkeypatch.setenv("CPM_PACKAGING_ENGINEER_GROUP", AN_ENGINEER_GROUP)
    monkeypatch.setenv("CPM_LEADERSHIP_GROUP", blank)

    contract = load_role_contract(empty_env)

    assert contract.leadership == ""
    assert contract.is_configured is False


def test_a_surrounding_whitespace_value_is_stripped_rather_than_rejected(
    empty_env: environ.Env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The name is still a name; only the whitespace goes."""
    monkeypatch.setenv("CPM_LEADERSHIP_GROUP", f"  {A_LEADERSHIP_GROUP} \n")

    assert load_role_contract(empty_env).leadership == A_LEADERSHIP_GROUP


def test_loading_never_raises(empty_env: environ.Env, monkeypatch: pytest.MonkeyPatch) -> None:
    """Called from `config/settings/base.py`, so a raise fires on every command.

    Every management command, every test run and `migrate` on a fresh clone all
    import that module. The refusal to serve on a misconfiguration is a startup
    concern with a locality signal to gate it; this is a read.
    """
    monkeypatch.setenv("CPM_SECURITY_REVIEWER_GROUP", "  ")

    contract = load_role_contract(empty_env)

    # The shape, not merely `is not None` -- which every possible return value
    # satisfies and which would therefore pass on a loader that had stopped
    # reading anything at all.
    assert contract == RoleContract(security_reviewer="", packaging_engineer="", leadership="")
    assert contract.is_configured is False


# ---------------------------------------------------------------------------
# The declared surface: the slots, the variables, and the names kept out.
# ---------------------------------------------------------------------------


def test_the_three_slots_are_distinct() -> None:
    """Two slots sharing a spelling would silently collapse to one grant."""
    assert len({SECURITY_REVIEWER, PACKAGING_ENGINEER, LEADERSHIP}) == len(ROLE_SLOTS)


def test_the_permission_declaration_is_keyed_by_role_slot() -> None:
    """Keyed by slot, never by group name -- which is what keeps names out of the source."""
    assert set(ROLE_GROUP_PERMISSIONS) == ROLE_SLOTS


def test_leadership_holds_the_governed_writes_the_reviewer_recollects_and_the_engineer_holds_nothing() -> None:
    """AC #7's declaration half: three grants across two slots, and one slot still empty.

    Two governed writes since `CPM-OPERATE-S03`: the identity override and the
    inventory change are the two governed human writes `CPM-FR-3` (as amended)
    names, both leadership's, and both arrive with the write rather than with a
    surface. A third grant since `CPM-OPERATE-S08`: the manual recollection is
    not a governed write, but it spends a source's allowance and `CPM-AD-13`
    gates it -- for the security reviewer, who is the one waiting on the sweep
    after an override, and for leadership. The packaging engineer's tuple is
    the one still asserted empty.

    This case used to assert that every slot granted nothing, which was the state
    until `CPM-IDENTITY-S05`. It is rewritten rather than deleted, because the
    property it protected still matters and is now the harder half: a permission
    attached to the security-reviewer or packaging-engineer slot is exactly the
    accident an empty tuple was pinned to catch, and deleting the case would have
    removed the mechanism at the moment there was finally something to attach.

    The grant itself is asserted by *equality with a tuple*, not by membership: a
    second codename added to leadership without a decision is the same accident
    seen from the other side, and `codenames >= {...}` would pass on it.

    `IDENTITY_OVERRIDE_PERMISSION` is imported rather than spelled, because a
    literal `"identity.override_package_identity"` here would be a second spelling
    of the string whose *single* spelling is the entire reason `core/roles.py`
    declares it -- see that module, and
    `tests/unit/django_apps/test_identity_overrides.py`, which reconciles it
    against the model's own `Meta.permissions`.
    """
    assert ROLE_GROUP_PERMISSIONS[LEADERSHIP] == (
        IDENTITY_OVERRIDE_PERMISSION,
        INVENTORY_CHANGE_PERMISSION,
        RECOLLECT_PERMISSION,
    )
    assert ROLE_GROUP_PERMISSIONS[SECURITY_REVIEWER] == (RECOLLECT_PERMISSION,)
    assert ROLE_GROUP_PERMISSIONS[PACKAGING_ENGINEER] == ()


def test_the_override_permission_is_an_app_label_and_a_codename() -> None:
    """The shape `provision_groups` resolves, asserted where the string is declared.

    `_resolve_permissions` splits on the dot and looks the two halves up in
    `auth_permission`; a codename written without its application resolves to
    nothing, is logged at warning, and attaches silently -- so the leadership
    group would simply not hold the permission and every override would be
    refused as forbidden with nothing in the log saying why.

    Asserted against the two constants rather than against a literal, so the pair
    stays reconciled if either is renamed.
    """
    assert f"{IDENTITY_APP_LABEL}.{IDENTITY_OVERRIDE_CODENAME}" == IDENTITY_OVERRIDE_PERMISSION
    assert IDENTITY_OVERRIDE_PERMISSION.count(".") == 1
    assert IDENTITY_APP_LABEL
    assert IDENTITY_OVERRIDE_CODENAME


def test_the_inventory_permission_is_an_app_label_and_a_codename() -> None:
    """The second grant's shape, on the first's terms (`CPM-OPERATE-S03`).

    The application is `collectors`, because that is where the table and the
    audit model live; the codename must not collide with the four Django derives
    for the two models there, which is why it names the *table* rather than a
    model.
    """
    assert f"{INVENTORY_APP_LABEL}.{INVENTORY_CHANGE_CODENAME}" == INVENTORY_CHANGE_PERMISSION
    assert INVENTORY_CHANGE_PERMISSION.count(".") == 1
    assert INVENTORY_APP_LABEL == "collectors"
    assert INVENTORY_CHANGE_CODENAME
    assert INVENTORY_CHANGE_PERMISSION != IDENTITY_OVERRIDE_PERMISSION


def test_the_recollect_permission_is_an_app_label_and_a_codename() -> None:
    """The third grant's shape, on the first two's terms (`CPM-OPERATE-S08`).

    The application is `collectors`, where `PackageRecollection` lives; the
    codename names the act rather than a model, so it cannot collide with the
    four Django derives.
    """
    assert f"{RECOLLECT_APP_LABEL}.{RECOLLECT_CODENAME}" == RECOLLECT_PERMISSION
    assert RECOLLECT_PERMISSION.count(".") == 1
    assert RECOLLECT_APP_LABEL == "collectors"
    assert RECOLLECT_CODENAME
    assert RECOLLECT_PERMISSION not in {IDENTITY_OVERRIDE_PERMISSION, INVENTORY_CHANGE_PERMISSION}


def test_the_declared_variables_pair_with_the_contract_fields_in_order() -> None:
    """`ROLE_ENVIRONMENT_VARIABLES` is not a second, drifting list.

    The loader unpacks a generator over this tuple into the three fields
    positionally, so a variable in the wrong position would fill the wrong field
    -- silently, because all three hold strings of the same shape.
    """
    fields = [field.name for field in dataclasses.fields(RoleContract)]

    assert fields == [SECURITY_REVIEWER, PACKAGING_ENGINEER, LEADERSHIP]
    assert list(ROLE_ENVIRONMENT_VARIABLES) == [f"CPM_{field.upper()}_GROUP" for field in fields]


def test_the_documented_variables_are_the_variables_read() -> None:
    """`docs/accelerator/authentication.md` is the only operator-facing home for these names.

    With no `.env.example` in the tree, a rename here would otherwise leave the
    published documentation instructing operators to set a variable nothing
    reads.
    """
    doc = Path(settings.BASE_DIR) / "docs" / "accelerator" / "authentication.md"
    documented = set(re.findall(r"CPM_[A-Z_]+", doc.read_text(encoding="utf-8")))

    assert documented == set(ROLE_ENVIRONMENT_VARIABLES)


def test_the_role_module_names_no_group() -> None:
    """No configured group name appears anywhere in the module's source.

    Checked against the source text rather than against the declaration alone, so
    a name smuggled in as a default argument, a fallback or a comparison is
    caught too. The names are the ones the suite is configured with, which are
    arbitrary as far as this module is concerned -- that is the point.
    """
    source = ROLES_MODULE.read_text(encoding="utf-8")
    contract = settings.ROLE_CONTRACT

    for name in (contract.security_reviewer, contract.packaging_engineer, contract.leadership):
        assert name, "the suite must run against a configured role contract for this to mean anything"
        assert name not in source, f"the role module hardcodes the configured group name {name!r}"


def test_the_active_settings_carry_the_role_contract_fixture() -> None:
    """The suite runs against the fixture in `config/settings/test.py`.

    Pinned so the override cannot be deleted or drift back to whatever `CPM_`
    variables a developer's shell happens to hold. Two things depend on it being
    exactly this: the two source scans above and in `test_role_migration.py`
    mean nothing against an empty contract, and every integration case in
    `tests/integration/django_apps/test_role_groups.py` reads the rows the
    migration provisioned from it.
    """
    assert settings.ROLE_CONTRACT == TEST_ROLE_CONTRACT


def test_the_role_module_imports_no_app_registry_and_no_auth_models() -> None:
    """It is imported at settings-import time, before either exists.

    Load-bearing rather than stylistic: `config/settings/base.py` imports this
    module at module scope, and an import of `django.apps` or
    `django.contrib.auth` from there raises `AppRegistryNotReady` -- which
    presents as every management command failing to start. Asserted on the parsed
    tree rather than by text search so that prose naming either package, of which
    the module's own docstring has several lines, is not itself an offence.
    """
    tree = ast.parse(ROLES_MODULE.read_text(encoding="utf-8"), filename=str(ROLES_MODULE))

    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module is not None and node.level == 0:
            imported.add(node.module)

    assert imported, "the scan found no imports at all, so it would pass on any module"
    for name in imported:
        assert not name.startswith(FORBIDDEN_IMPORT_ROOTS), f"roles.py imports {name!r}"


# ---------------------------------------------------------------------------
# `role_group_permissions` -- the name -> codenames mapping handed to the writer.
# ---------------------------------------------------------------------------


def test_the_mapping_carries_one_entry_per_configured_name() -> None:
    """Three distinct names, three entries, in slot declaration order."""
    contract = RoleContract(
        security_reviewer=A_REVIEWER_GROUP,
        packaging_engineer=AN_ENGINEER_GROUP,
        leadership=A_LEADERSHIP_GROUP,
    )

    assert list(role_group_permissions(contract)) == [A_REVIEWER_GROUP, AN_ENGINEER_GROUP, A_LEADERSHIP_GROUP]


def test_two_slots_naming_one_group_union_rather_than_clobber() -> None:
    """Nothing stops an operator pointing two role variables at one group.

    Iterating the slots directly would hand the provisioner the same name twice,
    and the second pass would `set` its own codenames over the first's. Since
    `CPM-OPERATE-S08` the reviewer's tuple is not empty, so this is no longer
    only about shape: a group named by both slots holds the reviewer's grant
    and the engineer's nothing, once, in slot order -- the mistake is
    unrecoverable afterwards, because a cleared grant looks exactly like one
    never made.
    """
    contract = RoleContract(
        security_reviewer=A_REVIEWER_GROUP,
        packaging_engineer=A_REVIEWER_GROUP,
        leadership=A_LEADERSHIP_GROUP,
    )

    mapping = role_group_permissions(contract)

    assert list(mapping) == [A_REVIEWER_GROUP, A_LEADERSHIP_GROUP]
    expected = tuple(
        dict.fromkeys(ROLE_GROUP_PERMISSIONS[SECURITY_REVIEWER] + ROLE_GROUP_PERMISSIONS[PACKAGING_ENGINEER]),
    )
    assert mapping[A_REVIEWER_GROUP] == expected
    assert mapping[A_REVIEWER_GROUP] == (RECOLLECT_PERMISSION,)


def test_an_unconfigured_contract_asks_for_no_group() -> None:
    """The mapping the migration hands the writer when nothing is declared."""
    assert role_group_permissions(RoleContract("", "", "")) == {}


def test_a_blank_name_is_never_asked_for() -> None:
    """A blank name would be created as a `Group` row nothing can match.

    Provisioning is only ever reached with a configured contract, so this guards
    the path rather than a reachable state today -- which is why it is asserted
    here rather than left to the caller.
    """
    contract = RoleContract(security_reviewer=A_REVIEWER_GROUP, packaging_engineer="", leadership="")

    assert list(role_group_permissions(contract)) == [A_REVIEWER_GROUP]


# ---------------------------------------------------------------------------
# A directly constructed contract -- the path the loader's strip does not cover.
# ---------------------------------------------------------------------------


def test_a_whitespace_only_name_is_not_a_configured_contract() -> None:
    """`is_configured` strips before it tests, not merely truthiness-tests.

    `load_role_contract` already strips what it reads, so this is reachable only
    through direct construction -- a settings module's local fill, or a test. It
    is closed anyway because the consequence is silent: a contract reporting
    *configured* on a whitespace name provisions a `Group` no claim can ever
    match, and the role's people are then admitted with no access.
    """
    contract = RoleContract(
        security_reviewer=A_REVIEWER_GROUP,
        packaging_engineer=AN_ENGINEER_GROUP,
        leadership="   ",
    )

    assert contract.is_configured is False


def test_a_whitespace_only_name_is_never_asked_for() -> None:
    """The same rule applied where it would reach the database.

    Asserted separately from `is_configured` because the two are read by
    different callers: the migration's guard reads the predicate, and the writer
    is handed this mapping.
    """
    contract = RoleContract(
        security_reviewer=f"  {A_REVIEWER_GROUP} ",
        packaging_engineer=" \n",
        leadership="",
    )

    assert list(role_group_permissions(contract)) == [A_REVIEWER_GROUP]
