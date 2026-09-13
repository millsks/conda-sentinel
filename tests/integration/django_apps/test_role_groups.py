"""The product's role groups against a real database.

What the unit tests cannot show: that the migration left three rows behind, that
provisioning them again converges rather than duplicating, and -- the criterion
this story exists for -- that a role asserted by the identity provider actually
becomes a Django group membership, and stops being one when the provider stops
asserting it.

The sync half deliberately calls the *platform's* `sync_authorization` with
nothing stubbed. `AD-10` says the product never re-implements group resolution
and `CPM-FR-30` is satisfied there, so what is under test here is that the claim
the product configures resolves through the mechanism that already exists. A test
that drove a role-specific code path would be testing code this story is
forbidden to write.

Groups are never created directly. `django_service.users.provisioning` is the one
mechanism permitted to create a `Group` (AD-27), and `AD-12`'s "a claim naming no
row is ignored, never created" is defensible only while that guarantee holds -- a
test that made its own rows would hide a real defect in it.

Every test here rolls back. `@pytest.mark.django_db` wraps each in a transaction,
which is what leaves the state as found; `transaction=True` would truncate the
tables the migrations seeded and take the first test's evidence with it.

**The cases that read what a migration left carry a local-feedback caveat, and it
is worth naming rather than discovering.** `--reuse-db` is in `addopts`, so on a
developer's machine those cases can be reading rows an *earlier* database build
wrote -- which means a change to `core/0001` or `core/0004` may not be reflected
until the test database is rebuilt (`--create-db`, or deleting it). CI builds
fresh every run and `pixi run gate-postgres` starts a throwaway server, so the
gate always reads the current migrations and this is not a hole in it. What it
costs is the inner loop: a migration edit that broke provisioning could pass
locally and fail in CI, which is the one direction of surprise worth writing
down. The cases that *run* `forward` and `reverse` themselves are unaffected --
they execute the code as it is now.
"""

from __future__ import annotations

from importlib import import_module
from types import SimpleNamespace
from typing import TYPE_CHECKING
from typing import Any

import pytest
import structlog
from django.apps import apps as global_apps
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.contrib.auth.models import Permission
from django.db import connection

from conda_sentinel.core.roles import IDENTITY_OVERRIDE_CODENAME
from conda_sentinel.core.roles import IDENTITY_OVERRIDE_PERMISSION
from conda_sentinel.core.roles import INVENTORY_APP_LABEL
from conda_sentinel.core.roles import INVENTORY_CHANGE_CODENAME
from conda_sentinel.core.roles import INVENTORY_CHANGE_PERMISSION
from conda_sentinel.core.roles import RoleContract
from conda_sentinel.core.roles import role_group_permissions
from config.authorization.exceptions import ClaimsRejected
from config.authorization.mapper import sync_authorization
from django_service.users import provisioning
from django_service.users.provisioning import provision_designated_groups
from django_service.users.provisioning import provision_groups
from tests.factories import UserFactory

if TYPE_CHECKING:
    from pytest_django.fixtures import SettingsWrapper

    from django_service.users.models import User

MIGRATION_MODULE = "conda_sentinel.core.migrations.0001_provision_role_groups"

# `CPM-IDENTITY-S05`'s grant. `tests/unit/django_apps/test_role_migration.py`
# holds its shape; what is here is the half only a run can show.
GRANT_MIGRATION_MODULE = "conda_sentinel.core.migrations.0005_grant_identity_override"

#: `CPM-OPERATE-S03`'s grant, on `0005`'s terms: the second governed write.
INVENTORY_GRANT_MIGRATION_MODULE = "conda_sentinel.core.migrations.0011_grant_inventory_change"

#: What the leadership row holds after both grants have run.
THE_LEADERSHIP_GRANTS = {IDENTITY_OVERRIDE_PERMISSION, INVENTORY_CHANGE_PERMISSION}

# The leadership group the suite is configured with, bound once so the
# unconfigured-contract case can still name the row after it has replaced the
# contract that names it.
TEST_LEADERSHIP_GROUP = "cpm-leadership"


def _schema_editor() -> Any:
    """Return what `RunPython` hands a data migration for `schema_editor`.

    `0004`'s `forward` reads `schema_editor.connection.alias` to tell
    `create_permissions` which database to write to. `0001`'s cases pass `None`
    because its `forward` never touches the argument; this one does, so the cases
    below hand it the live connection wrapper -- which is what `migrate` passes,
    reached through the object that actually carries it.

    Returns:
        A stand-in exposing `.connection`, typed `Any` because a schema editor is
        a backend-specific class no annotation here should assert.

    """
    return SimpleNamespace(connection=connection)


def _label(permission: Permission) -> str:
    """Return a permission as the `app_label.codename` string a declaration names it by.

    Args:
        permission: The row.

    Returns:
        The dotted spelling `ROLE_GROUP_PERMISSIONS` and `provision_groups` use,
        so a case compares declarations against declarations rather than against
        primary keys.

    """
    return f"{permission.content_type.app_label}.{permission.codename}"


# Names chosen to look nothing like the ones the suite is configured with, so a
# case passing on them cannot be passing on a literal in the source.
A_REVIEWER_GROUP = "wharf-inspectors"
AN_ENGINEER_GROUP = "wharf-riggers"
A_LEADERSHIP_GROUP = "wharf-masters"

AN_UNCONFIGURED_CONTRACT = RoleContract(security_reviewer="", packaging_engineer="", leadership="")

SUBJECT = "urn:example:principal:role-holder"

# How many role slots there are, and so how many rows a fully configured contract
# pointing at three distinct groups asks for.
ROLE_COUNT = 3


@pytest.fixture
def arbitrary_contract(settings: SettingsWrapper) -> RoleContract:
    """Point the role contract at three groups no migration has created."""
    contract = RoleContract(
        security_reviewer=A_REVIEWER_GROUP,
        packaging_engineer=AN_ENGINEER_GROUP,
        leadership=A_LEADERSHIP_GROUP,
    )
    settings.ROLE_CONTRACT = contract
    return contract


@pytest.fixture
def colliding_contract(settings: SettingsWrapper) -> RoleContract:
    """Point one role slot at the group the claims contract already designates.

    Not a contrived state. "Platform and engineering leadership" and the group
    that confers `is_staff` are frequently one directory group, and this codebase
    already treats the analogous collision *within* the claims contract -- both
    designated variables naming one group -- as a configuration rather than a
    mistake.
    """
    contract = RoleContract(
        security_reviewer=A_REVIEWER_GROUP,
        packaging_engineer=AN_ENGINEER_GROUP,
        leadership=settings.CLAIMS_CONTRACT.staff_group,
    )
    settings.ROLE_CONTRACT = contract
    return contract


@pytest.fixture
def role_holder(db: None) -> User:
    """A user carrying an identity key, as `resolve_user` would have left it."""
    created: User = UserFactory.create(username="reviewer", idp_subject=SUBJECT)
    return created


def _claims(*names: str, groups: bool = True) -> dict[str, Any]:
    """Build a claim set asserting `names` at the configured group claim.

    Args:
        names: The group names the token asserts.
        groups: False to omit the group claim entirely, which is the case AD-12
            makes a refusal rather than an assertion of no groups.

    Returns:
        The claims, keyed by the names `settings.CLAIMS_CONTRACT` configures.

    """
    contract = settings.CLAIMS_CONTRACT
    claims: dict[str, Any] = {contract.identity_key_claim: SUBJECT}
    if groups:
        claims[contract.group_claim] = list(names)
    return claims


# ---------------------------------------------------------------------------
# AC #1, first half -- the rows exist, provisioned by migration.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_the_migration_provisions_the_three_role_groups() -> None:
    """Asserted on the state the migration left, before anything here provisions.

    Nothing in this test creates a group. `core/0001_provision_role_groups` ran
    when the test database was built, and what is read back is its output -- which
    is the only way to show that the migration is what guarantees the rows,
    rather than some later call that happens to run first in production too.
    """
    expected = set(role_group_permissions(settings.ROLE_CONTRACT))

    assert len(expected) == ROLE_COUNT
    assert set(Group.objects.filter(name__in=expected).values_list("name", flat=True)) == expected


@pytest.mark.django_db
def test_the_migrations_grant_the_two_governed_writes_to_leadership_and_to_nobody_else() -> None:
    """AC #7: the rows after migration hold exactly the two grants, and the other two hold none.

    Two since `CPM-OPERATE-S03`: `core/0005` attaches the identity override and
    `core/0011` the inventory change, both to the leadership row.

    This case used to assert that every role group carried no permissions at all,
    which was true until `CPM-IDENTITY-S05` -- the first story with anything to
    grant. It is rewritten rather than deleted, because the property it protected
    is the half that still matters: a permission on the security-reviewer or
    packaging-engineer row is the accident the empty assertion existed to catch,
    and it is now a *narrower* claim than "nobody holds anything", not a weaker
    one.

    **Asserted on the rows, not on the declaration.** An entry in
    `ROLE_GROUP_PERMISSIONS` and a `Permission` attached to an `auth_group` row
    are different claims, and everything between them can fail quietly:
    `_resolve_permissions` logs an unresolvable codename at warning and attaches
    nothing, `create_permissions` not being called leaves `auth_permission` empty
    for `identity` on a fresh database, and a migration that never ran attaches
    nothing at all. Each of those ends with leadership holding no permission, the
    unit case in `tests/unit/django_apps/test_roles.py` still passing, and every
    override refused as forbidden. This is the case that catches all three.

    Nothing here provisions anything. `core/0005_grant_identity_override` ran when
    the test database was built, and what is read back is its output -- which is
    the only way to show that the migration is what guarantees the grant, rather
    than some later call that happens to run first in production too.
    """
    contract = settings.ROLE_CONTRACT
    held = {
        name: {
            f"{permission.content_type.app_label}.{permission.codename}"
            for permission in Group.objects.get(name=name).permissions.select_related("content_type")
        }
        for name in role_group_permissions(contract)
    }

    assert held[contract.leadership] == THE_LEADERSHIP_GRANTS
    assert held[contract.security_reviewer] == set()
    assert held[contract.packaging_engineer] == set()


@pytest.mark.django_db
def test_the_granted_permission_is_the_one_the_override_actually_checks() -> None:
    """The grant and the check are one string, proven against a real user and real rows.

    The unit suite reconciles `IDENTITY_OVERRIDE_PERMISSION` against the model's
    `Meta.permissions`, which is a claim about two declarations. This is the claim
    about the world: a person put in the leadership group by nothing but a
    membership passes `has_perm` for the permission `override_identity` asks
    about, and the same person outside it does not.

    `has_perm` is called on a user re-read from the database, because Django
    caches a user's permissions on the instance the first time it resolves them --
    so asserting the negative and then the positive on one object would report the
    stale answer and pass whatever the grant did.
    """
    user = UserFactory.create(username="a-leader", idp_subject=SUBJECT)

    assert get_user_model().objects.get(pk=user.pk).has_perm(IDENTITY_OVERRIDE_PERMISSION) is False

    user.groups.add(Group.objects.get(name=settings.ROLE_CONTRACT.leadership))

    assert get_user_model().objects.get(pk=user.pk).has_perm(IDENTITY_OVERRIDE_PERMISSION) is True


# ---------------------------------------------------------------------------
# Provisioning through the one writer.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_provisioning_creates_a_row_per_configured_role_and_converges(
    arbitrary_contract: RoleContract,
) -> None:
    """Three names in, three rows out, and the second pass creates nothing.

    Idempotence is structural rather than a re-run guard, so it is asserted at
    the call site: `migrate` is run again on every deployment, and a second
    insert would either duplicate or raise.
    """
    asked = role_group_permissions(arbitrary_contract)

    first = provision_groups(asked)
    second = provision_groups(asked)

    assert set(first.created) == set(asked)
    assert first.existing == ()
    assert second.created == ()
    assert set(second.existing) == set(asked)
    assert Group.objects.filter(name__in=asked).count() == ROLE_COUNT


@pytest.mark.django_db
def test_two_slots_naming_one_group_produce_one_row(settings: SettingsWrapper) -> None:
    """An operator may legitimately point two role variables at one group.

    A small deployment where the packaging engineers are also the leadership
    audience is a configuration, not a mistake. It must produce one row rather
    than two passes over the same name, because the second pass would `set` its
    own permissions over the first's and silently clear the earlier grant.
    """
    settings.ROLE_CONTRACT = RoleContract(
        security_reviewer=A_REVIEWER_GROUP,
        packaging_engineer=A_LEADERSHIP_GROUP,
        leadership=A_LEADERSHIP_GROUP,
    )
    asked = role_group_permissions(settings.ROLE_CONTRACT)

    result = provision_groups(asked)

    assert set(result.created) == {A_REVIEWER_GROUP, A_LEADERSHIP_GROUP}
    assert Group.objects.filter(name=A_LEADERSHIP_GROUP).count() == 1


@pytest.mark.django_db
def test_the_migration_forward_provisions_through_the_one_writer(
    arbitrary_contract: RoleContract,
) -> None:
    """The happy path of `forward`, driven directly rather than only inferred.

    Every other provisioning case here calls `provision_groups`, which leaves
    `forward` itself -- the contract read, the guard, and the arguments it hands
    the writer -- exercised by nothing. `*/migrations/*` is omitted from
    coverage, so that gap is invisible to the floor, which is the very reason the
    migration's docstring gives for keeping logic out of it.
    """
    migration = import_module(MIGRATION_MODULE)
    asked = role_group_permissions(arbitrary_contract)

    migration.forward(global_apps, None)

    assert set(Group.objects.filter(name__in=asked).values_list("name", flat=True)) == set(asked)


@pytest.mark.django_db
def test_the_migration_forward_hands_the_writer_the_registry_it_was_given(
    arbitrary_contract: RoleContract,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`apps` is the seam that lets one implementation serve migration and runtime.

    A `forward` that dropped it would provision against the *live* registry from
    inside a migration, which works on a fresh database and fails on any
    deployment whose historical `auth` state differs from the current one -- the
    kind of defect that only appears on the oldest database in the estate.
    """
    migration = import_module(MIGRATION_MODULE)
    received: list[object] = []

    def _spy(groups: object, apps: object = None, **kwargs: object) -> None:
        received.append(apps)

    monkeypatch.setattr(provisioning, "provision_groups", _spy)

    migration.forward(global_apps, None)

    assert received == [global_apps]


@pytest.mark.django_db
def test_the_migration_provisions_nothing_when_the_contract_is_unconfigured(
    settings: SettingsWrapper,
) -> None:
    """`migrate` on a fresh clone must stay usable before any contract exists.

    Driven through the migration's own `forward` rather than through
    `provision_groups`, because the guard being asserted is the migration's: a
    raise here would fire inside `migrate`, before anyone has role groups to
    declare, and leave the database mid-migration over a configuration mistake.

    The skip is logged rather than silent, and the event is asserted: an operator
    who set two of the three variables gets no role groups, and without this line
    would get no signal either.
    """
    migration = import_module(MIGRATION_MODULE)
    settings.ROLE_CONTRACT = AN_UNCONFIGURED_CONTRACT
    before = set(Group.objects.values_list("name", flat=True))

    with structlog.testing.capture_logs() as captured:
        migration.forward(global_apps, None)

    assert set(Group.objects.values_list("name", flat=True)) == before
    events = [event for event in captured if event["event"] == "authorization.provisioning_skipped"]
    assert len(events) == 1
    assert events[0]["log_level"] == "warning"
    assert events[0]["reason"] == "role_contract_unconfigured"


# ---------------------------------------------------------------------------
# The role contract is a second declaration over one `auth_group` table.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_provisioning_a_role_group_never_clears_a_designated_groups_permissions(
    colliding_contract: RoleContract,
) -> None:
    """A shared name must not let the later pass disarm the earlier one.

    `core/0001` declares `users/0003` as a dependency, so in a single `migrate`
    the role pass always runs after the designated groups were given their
    permissions. A `permissions.set` with the role slot's empty declaration would
    clear `users.view_user` and `users.change_user` from that row, and nothing
    would catch it: `stage_two`'s condition asks whether the designated rows
    *exist*, not what they hold. The symptom is staff members authenticating,
    receiving `is_staff`, and landing on an empty admin index.

    **Asserted as a superset since `CPM-IDENTITY-S05`, not as equality.** The
    leadership slot now declares a real codename, so a pass over a row that slot
    shares with the staff group legitimately *adds* one permission. Equality would
    have made this case fail on the grant rather than on the disarming it exists
    to catch. What it asserts instead is the property itself, in both directions:
    nothing the earlier pass attached is gone, and everything this pass added was
    asked for by the role declaration -- so a `set` in place of the `add` still
    fails here, and so does a codename attached from somewhere nobody declared.
    """
    migration = import_module(MIGRATION_MODULE)
    provision_designated_groups()
    shared = Group.objects.get(name=colliding_contract.leadership)
    before = set(shared.permissions.values_list("pk", flat=True))

    migration.forward(global_apps, None)

    shared.refresh_from_db()
    assert before, "the designated staff group must hold permissions for this to mean anything"
    after = set(shared.permissions.values_list("id", "content_type__app_label", "codename"))
    assert {row[0] for row in after} >= before
    declared = set(role_group_permissions(colliding_contract)[colliding_contract.leadership])
    gained = {f"{app_label}.{codename}" for row_id, app_label, codename in after if row_id not in before}
    assert gained <= declared


@pytest.mark.django_db
def test_the_migration_reverse_never_deletes_a_designated_group(
    colliding_contract: RoleContract,
) -> None:
    """Rolling back the role migration must not take the administrators with it.

    `forward` did not create a row the claims contract names -- `users/0003` did
    -- and deleting it cascades through `auth_user_groups`, removing every
    membership it held. The next start would then refuse for a missing designated
    group, with the memberships already gone.
    """
    migration = import_module(MIGRATION_MODULE)
    provision_designated_groups()
    migration.forward(global_apps, None)
    shared = colliding_contract.leadership

    migration.reverse(global_apps, None)

    assert Group.objects.filter(name=shared).exists()
    assert not Group.objects.filter(name__in=(A_REVIEWER_GROUP, AN_ENGINEER_GROUP)).exists()


@pytest.mark.django_db
def test_the_migration_reverse_removes_only_the_role_groups(
    arbitrary_contract: RoleContract,
) -> None:
    """The rollback half, which nothing else runs.

    `reverse` is executable code kept permanently in the graph -- the operation
    declares `elidable=False` -- so a broken filter, a wrong model or a raise
    would surface only when an operator rolled the migration back, which is the
    worst moment to find out.
    """
    migration = import_module(MIGRATION_MODULE)
    asked = role_group_permissions(arbitrary_contract)
    provision_groups(asked)
    designated = settings.CLAIMS_CONTRACT.staff_group

    migration.reverse(global_apps, None)

    assert not Group.objects.filter(name__in=asked).exists()
    assert Group.objects.filter(name=designated).exists()


@pytest.mark.django_db
def test_the_migration_reverse_deletes_nothing_when_the_contract_is_unconfigured(
    settings: SettingsWrapper,
) -> None:
    """An unconfigured contract names nothing, so the rollback removes nothing.

    The guard matters because the alternative -- falling through to a filter on
    three empty strings -- would delete any group that happened to carry an empty
    name rather than declining to act.
    """
    migration = import_module(MIGRATION_MODULE)
    settings.ROLE_CONTRACT = AN_UNCONFIGURED_CONTRACT
    before = set(Group.objects.values_list("name", flat=True))

    migration.reverse(global_apps, None)

    assert set(Group.objects.values_list("name", flat=True)) == before


# ---------------------------------------------------------------------------
# AC #1, second half -- an asserted role is held, and a revoked one is removed.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_an_asserted_role_group_is_held_after_the_next_sync(role_holder: User) -> None:
    """The criterion the story exists for, driven through the platform's own mapper.

    Nothing product-specific runs. The role group is an ordinary `Group` and the
    role claim is the ordinary group claim, which is exactly what `AD-10` says:
    the product provisions the rows and the platform resolves them.
    """
    role = settings.ROLE_CONTRACT.security_reviewer

    outcome = sync_authorization(role_holder, _claims(role))

    assert outcome.added == (role,)
    assert outcome.ignored == ()
    assert set(role_holder.groups.values_list("name", flat=True)) == {role}


@pytest.mark.parametrize("slot", ["security_reviewer", "packaging_engineer", "leadership"])
@pytest.mark.django_db
def test_a_role_group_confers_neither_staff_nor_superuser(role_holder: User, slot: str) -> None:
    """The premise the whole design rests on: a role group cannot escalate.

    `sync_authorization` derives both flags from the *designated* names alone, so
    a role group is an ordinary group -- but "ordinary" is exactly the property
    that would be silently lost if a role name were ever pointed at the staff or
    superuser slot, or if the derivation were widened. Asserted per slot, because
    the three are configured independently and only one of them needs to be
    wrong.
    """
    role = getattr(settings.ROLE_CONTRACT, slot)

    outcome = sync_authorization(role_holder, _claims(role))

    assert outcome.is_staff is False
    assert outcome.is_superuser is False
    role_holder.refresh_from_db()
    assert role_holder.is_staff is False
    assert role_holder.is_superuser is False


@pytest.mark.django_db
def test_a_role_revoked_at_the_provider_is_removed_at_the_next_sync(role_holder: User) -> None:
    """Revocation reaches the component at the next resolution, not before.

    `R-2` records the consequence this cannot promise around: a group revoked at
    the provider is honoured until the token the claims came from expires. What
    it does promise is that the *next* resolution removes it, which is the half
    that would otherwise leave a departed reviewer holding the role forever.
    """
    role = settings.ROLE_CONTRACT.packaging_engineer
    sync_authorization(role_holder, _claims(role))

    outcome = sync_authorization(role_holder, _claims())

    assert outcome.removed == (role,)
    assert set(role_holder.groups.values_list("name", flat=True)) == set()


@pytest.mark.django_db
def test_one_role_replaces_another_in_a_single_sync(role_holder: User) -> None:
    """The add and the remove are one diff, so no observer sees both roles at once.

    A person moving between roles is the ordinary case the two tests above only
    cover the ends of; that it is one transaction is what keeps the intermediate
    state -- holding the union, or neither -- unobservable.
    """
    contract = settings.ROLE_CONTRACT
    sync_authorization(role_holder, _claims(contract.security_reviewer))

    outcome = sync_authorization(role_holder, _claims(contract.leadership))

    assert outcome.added == (contract.leadership,)
    assert outcome.removed == (contract.security_reviewer,)
    assert set(role_holder.groups.values_list("name", flat=True)) == {contract.leadership}


# ---------------------------------------------------------------------------
# AC #3 -- an absent group claim is refused, and is distinguishable from zero.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_an_absent_group_claim_is_refused(role_holder: User) -> None:
    """A misconfigured claim *name* must not read as "this person has no roles".

    The two are indistinguishable on the row afterwards, so treating the absent
    case as an assertion of no groups is how a misconfiguration presents as a
    permissions bug -- and, with role-scoped surfaces, as a queue that silently
    empties for everyone.
    """
    with pytest.raises(ClaimsRejected):
        sync_authorization(role_holder, _claims(groups=False))


@pytest.mark.django_db
def test_a_group_claim_asserting_no_groups_syncs_to_no_roles(role_holder: User) -> None:
    """The other side of the same distinction: present and empty is legitimate.

    An authenticated caller holding no role is a real state -- somebody in the
    directory who has not been granted one yet. It syncs, and it does not raise.
    """
    outcome = sync_authorization(role_holder, _claims())

    assert outcome.added == ()
    assert outcome.removed == ()
    assert outcome.ignored == ()
    assert set(role_holder.groups.values_list("name", flat=True)) == set()


# ---------------------------------------------------------------------------
# `core/0005_grant_identity_override`, run rather than only read.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_the_grant_pass_converges_rather_than_duplicating() -> None:
    """`migrate` is re-run on every deployment, so the pass must be idempotent.

    Asserted at the call site rather than inferred from `get_or_create` and
    `permissions.add`, because idempotence is a property of the whole pass: it
    creates permission rows, resolves them and attaches them, and any of the three
    could have been written to duplicate.
    """
    grant = import_module(GRANT_MIGRATION_MODULE)
    leadership = Group.objects.get(name=settings.ROLE_CONTRACT.leadership)

    grant.forward(global_apps, _schema_editor())
    grant.forward(global_apps, _schema_editor())

    # Both grants, once each: `core/0011` attached the second when the test
    # database was built, and re-running this pass adds nothing and removes
    # nothing -- it provisions its own codename and no other.
    assert {_label(permission) for permission in leadership.permissions.all()} == THE_LEADERSHIP_GRANTS
    assert leadership.permissions.count() == len(THE_LEADERSHIP_GRANTS)


@pytest.mark.django_db
def test_the_grant_reverse_detaches_the_permission_and_leaves_the_group() -> None:
    """The rollback path, executed rather than read.

    `reverse` is executable code kept permanently in the migration graph, and
    nothing but an operator rolling back ever runs it -- so a broken filter, a
    wrong model or a raise surfaces at the worst possible moment. That is the
    argument `0001`'s three reverse cases make, and this is the same argument for
    the migration that carries the product's one grant.

    The group survives, and so does every membership hanging off it: unapplying a
    grant is not unapplying the role.
    """
    grant = import_module(GRANT_MIGRATION_MODULE)
    grant.forward(global_apps, _schema_editor())
    leadership = Group.objects.get(name=settings.ROLE_CONTRACT.leadership)
    assert {_label(permission) for permission in leadership.permissions.all()} == THE_LEADERSHIP_GRANTS

    grant.reverse(global_apps, _schema_editor())

    leadership.refresh_from_db()
    # Its own grant and no other: the inventory permission `core/0011` attached
    # is not this migration's to take away.
    assert {_label(permission) for permission in leadership.permissions.all()} == {INVENTORY_CHANGE_PERMISSION}
    assert Group.objects.filter(name=settings.ROLE_CONTRACT.leadership).exists()
    assert Permission.objects.filter(codename=IDENTITY_OVERRIDE_CODENAME).exists()


@pytest.mark.django_db
def test_the_grant_reverse_leaves_a_group_the_role_contract_does_not_name(
    settings: SettingsWrapper,
) -> None:
    """The rollback revokes what this migration granted, and not what somebody else did.

    `forward` provisions with `preserve_existing=True` precisely so it speaks only
    for what it asks for. A `reverse` that stripped the codename from *every* group
    holding it would re-create through the rollback path exactly the disarming that
    keyword exists to prevent -- a group another contract, another migration or an
    administrator in the admin had granted it to, silently revoked by unapplying a
    migration that never granted it.

    The other group here is outside the role contract entirely, which is what makes
    the case about scope rather than about ordering.
    """
    grant = import_module(GRANT_MIGRATION_MODULE)
    grant.forward(global_apps, _schema_editor())
    permission = Permission.objects.get(codename=IDENTITY_OVERRIDE_CODENAME)
    somebody_elses = Group.objects.create(name=A_REVIEWER_GROUP)
    somebody_elses.permissions.add(permission)

    grant.reverse(global_apps, _schema_editor())

    assert permission not in Group.objects.get(name=settings.ROLE_CONTRACT.leadership).permissions.all()
    assert list(somebody_elses.permissions.all()) == [permission]


@pytest.mark.django_db
def test_the_grant_pass_provisions_nothing_on_an_unconfigured_contract(
    settings: SettingsWrapper,
) -> None:
    """A fresh clone is migrated long before anyone has role groups to declare.

    `forward` logs and returns rather than raising, on `0001`'s terms exactly: a
    migration that refused would make `pixi run migrate` unusable during bring-up,
    and refusing to *serve* on an unconfigured contract is a startup concern that
    belongs to stage one. `reverse` returns on the same condition, so it undoes
    exactly what `forward` does.

    The permission row is still created -- that half runs before the contract is
    read, and it is `post_migrate`'s work being done early rather than anything to
    do with the contract.
    """
    grant = import_module(GRANT_MIGRATION_MODULE)
    settings.ROLE_CONTRACT = AN_UNCONFIGURED_CONTRACT
    leadership_before = set(
        Group.objects.get(name=TEST_LEADERSHIP_GROUP).permissions.values_list("pk", flat=True),
    )

    grant.forward(global_apps, _schema_editor())
    grant.reverse(global_apps, _schema_editor())

    assert Permission.objects.filter(codename=IDENTITY_OVERRIDE_CODENAME).exists()
    assert set(Group.objects.get(name=TEST_LEADERSHIP_GROUP).permissions.values_list("pk", flat=True)) == (
        leadership_before
    )


# ---------------------------------------------------------------------------
# `core/0011_grant_inventory_change`, run rather than only read (CPM-OPERATE-S03).
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_the_granted_inventory_permission_is_the_one_the_service_actually_checks() -> None:
    """The grant and the check are one string, proven against a real user and real rows.

    On the override's terms: a person put in the leadership group by nothing but
    a membership passes `has_perm` for the permission `collectors/inventory.py`
    asks about, and the same person outside it does not. Re-read from the
    database each time, because Django caches permissions on the instance.
    """
    user = UserFactory.create(username="a-leader", idp_subject=SUBJECT)

    assert get_user_model().objects.get(pk=user.pk).has_perm(INVENTORY_CHANGE_PERMISSION) is False

    user.groups.add(Group.objects.get(name=settings.ROLE_CONTRACT.leadership))

    assert get_user_model().objects.get(pk=user.pk).has_perm(INVENTORY_CHANGE_PERMISSION) is True


@pytest.mark.django_db
def test_the_inventory_grant_pass_converges_rather_than_duplicating() -> None:
    """`migrate` is re-run on every deployment, so the pass must be idempotent."""
    grant = import_module(INVENTORY_GRANT_MIGRATION_MODULE)
    leadership = Group.objects.get(name=settings.ROLE_CONTRACT.leadership)

    grant.forward(global_apps, _schema_editor())
    grant.forward(global_apps, _schema_editor())

    assert {_label(permission) for permission in leadership.permissions.all()} == THE_LEADERSHIP_GRANTS
    assert leadership.permissions.count() == len(THE_LEADERSHIP_GRANTS)


@pytest.mark.django_db
def test_the_inventory_grant_reverse_detaches_only_its_own_permission() -> None:
    """The rollback path, executed: the inventory grant goes, the override grant and the group stay."""
    grant = import_module(INVENTORY_GRANT_MIGRATION_MODULE)
    grant.forward(global_apps, _schema_editor())
    leadership = Group.objects.get(name=settings.ROLE_CONTRACT.leadership)
    assert {_label(permission) for permission in leadership.permissions.all()} == THE_LEADERSHIP_GRANTS

    grant.reverse(global_apps, _schema_editor())

    leadership.refresh_from_db()
    assert {_label(permission) for permission in leadership.permissions.all()} == {IDENTITY_OVERRIDE_PERMISSION}
    assert Group.objects.filter(name=settings.ROLE_CONTRACT.leadership).exists()
    assert Permission.objects.filter(
        content_type__app_label=INVENTORY_APP_LABEL,
        codename=INVENTORY_CHANGE_CODENAME,
    ).exists()


@pytest.mark.django_db
def test_the_inventory_grant_reverse_leaves_a_group_the_role_contract_does_not_name() -> None:
    """The rollback revokes what this migration granted, and not what somebody else did."""
    grant = import_module(INVENTORY_GRANT_MIGRATION_MODULE)
    grant.forward(global_apps, _schema_editor())
    permission = Permission.objects.get(content_type__app_label=INVENTORY_APP_LABEL, codename=INVENTORY_CHANGE_CODENAME)
    somebody_elses = Group.objects.create(name=A_REVIEWER_GROUP)
    somebody_elses.permissions.add(permission)

    grant.reverse(global_apps, _schema_editor())

    assert permission not in Group.objects.get(name=settings.ROLE_CONTRACT.leadership).permissions.all()
    assert list(somebody_elses.permissions.all()) == [permission]


@pytest.mark.django_db
def test_the_inventory_grant_pass_provisions_nothing_on_an_unconfigured_contract(
    settings: SettingsWrapper,
) -> None:
    """A fresh clone is migrated long before anyone has role groups to declare; the permission row is still created."""
    grant = import_module(INVENTORY_GRANT_MIGRATION_MODULE)
    settings.ROLE_CONTRACT = AN_UNCONFIGURED_CONTRACT
    leadership_before = set(
        Group.objects.get(name=TEST_LEADERSHIP_GROUP).permissions.values_list("pk", flat=True),
    )

    grant.forward(global_apps, _schema_editor())
    grant.reverse(global_apps, _schema_editor())

    assert Permission.objects.filter(
        content_type__app_label=INVENTORY_APP_LABEL, codename=INVENTORY_CHANGE_CODENAME
    ).exists()
    assert set(Group.objects.get(name=TEST_LEADERSHIP_GROUP).permissions.values_list("pk", flat=True)) == (
        leadership_before
    )


@pytest.mark.django_db
def test_the_two_grants_converge_whichever_order_a_rollback_and_a_re_apply_happen_in() -> None:
    """Unapply the second, re-run the first, re-apply the second: both held, once each.

    The order `0011`'s docstring claims converges. Each grant provisions only its
    own codename and removes nothing, so no sequence of the two leaves the
    leadership row with more or fewer than the two.
    """
    first = import_module(GRANT_MIGRATION_MODULE)
    second = import_module(INVENTORY_GRANT_MIGRATION_MODULE)
    leadership = Group.objects.get(name=settings.ROLE_CONTRACT.leadership)

    second.reverse(global_apps, _schema_editor())
    assert {_label(permission) for permission in leadership.permissions.all()} == {IDENTITY_OVERRIDE_PERMISSION}
    first.forward(global_apps, _schema_editor())
    assert {_label(permission) for permission in leadership.permissions.all()} == {IDENTITY_OVERRIDE_PERMISSION}
    second.forward(global_apps, _schema_editor())

    assert {_label(permission) for permission in leadership.permissions.all()} == THE_LEADERSHIP_GRANTS
    assert leadership.permissions.count() == len(THE_LEADERSHIP_GRANTS)
