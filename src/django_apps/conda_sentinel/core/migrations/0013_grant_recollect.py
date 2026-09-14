"""Attach the third role-group grant: the manual recollection.

`CPM-OPERATE-S08` gives `CPM-UJ-1`'s manual recollection a surface -- "Collect
now" on the package page -- and `CPM-AD-13` puts it behind a permission
(`collectors.recollect_package`, declared in `core/roles.py` and attached on
`collectors.PackageRecollection.Meta.permissions`). This migration is what grants
it, on exactly the terms `0011_grant_inventory_change` set: a further migration
rather than an edit to either earlier grant, because a data migration that has
already run against a deployed database is not re-run by editing it, and this one
runs on every database, new and existing.

**Two roles rather than one, for the first time.** The identity override and the
inventory change are governed writes and leadership's alone; a recollection
mutates no reference data, and it is the security reviewer who waits a day for
the sweep after an override, so `core/roles.py` declares it for the reviewer
and for leadership. The packaging engineer's tuple stays empty.

**`create_permissions` first, for the reason `0005` gives.** `Permission` rows are
created by the `post_migrate` signal, not by a migration; on a fresh database the
whole `migrate` invocation runs before that signal fires, so a pass here would
find `auth_permission` empty for the `collectors` models and attach nothing at
all -- silently, because attaching zero permissions is not an error, and the only
symptom would be that every "Collect now" is refused as forbidden.

**`preserve_existing=True`, and the cost `0005` states.** The role contract is a
secondary declaration over `auth_group` rows the claims contract also writes to,
so it adds what it asks for and removes nothing -- which means it cannot revoke by
omission. Deleting `RECOLLECT_PERMISSION` from `ROLE_GROUP_PERMISSIONS` leaves
every already-provisioned deployment holding it; `reverse` below is the worked
example of how the grant is taken away.

**Each grant provisions its own codename and no other.** `0005` grants the
identity override, `0011` the inventory change, this one the recollection, and
none touches another's: the three converge to the same rows whichever order a
rollback and a re-apply happen in, because each adds only what it asks for and
`preserve_existing=True` removes nothing.
"""

import structlog
from django.db import migrations

logger = structlog.get_logger(__name__)


def forward(apps, schema_editor):
    """Create the collectors permissions, then re-provision the role groups.

    `app_config.models_module` is set truthy and put back because
    `create_permissions` returns early on an app config without one, and the
    historical registry's app configs are stubs that have no models module --
    Django's own documented workaround. Put back to what it *was* rather than
    to `None`: on the historical stub that is the same thing, but a test that
    runs this `forward` against the live registry would otherwise leave the
    live `collectors` app config with no models module, and `post_migrate`
    would create no content type and no permission for it for the rest of the
    process.

    An unconfigured contract logs and returns, on `0001`'s terms: a fresh clone is
    migrated long before anyone has role groups to declare.
    """
    from django.conf import settings
    from django.contrib.auth.management import create_permissions

    from conda_sentinel.core.roles import RECOLLECT_APP_LABEL
    from conda_sentinel.core.roles import RECOLLECT_PERMISSION
    from conda_sentinel.core.roles import role_group_permissions
    from django_service.users.provisioning import provision_groups

    app_config = apps.get_app_config(RECOLLECT_APP_LABEL)
    # `getattr` with a default: the historical stub carries no attribute at all
    # until this sets one.
    models_module = getattr(app_config, "models_module", None)
    app_config.models_module = True
    try:
        create_permissions(
            app_config,
            apps=apps,
            using=schema_editor.connection.alias,
            verbosity=0,
        )
    finally:
        app_config.models_module = models_module

    contract = settings.ROLE_CONTRACT
    if not contract.is_configured:
        logger.warning(
            "authorization.provisioning_skipped",
            reason="role_contract_unconfigured",
        )
        return

    own = {
        name: tuple(code for code in codenames if code == RECOLLECT_PERMISSION)
        for name, codenames in role_group_permissions(contract).items()
    }
    provision_groups(
        own,
        apps,
        declared_by="role_contract",
        preserve_existing=True,
    )


def reverse(apps, schema_editor):
    """Detach the recollection permission from the groups this migration granted it to.

    Scoped to the role contract's own names and narrowed to the slots whose
    declaration asks for this codename, exactly as `0011`'s reverse is: a reverse
    that stripped the codename from every group holding it would revoke a grant
    some other contract, migration or administrator made. The `Permission` row
    itself, the groups, the users and the memberships are all left alone; this
    migration created none of them.
    """
    from django.conf import settings

    from conda_sentinel.core.roles import RECOLLECT_APP_LABEL
    from conda_sentinel.core.roles import RECOLLECT_CODENAME
    from conda_sentinel.core.roles import RECOLLECT_PERMISSION
    from conda_sentinel.core.roles import role_group_permissions

    contract = settings.ROLE_CONTRACT
    if not contract.is_configured:
        return

    names = [
        name for name, codenames in role_group_permissions(contract).items() if RECOLLECT_PERMISSION in codenames
    ]
    if not names:
        return

    group_model = apps.get_model("auth", "Group")
    permission_model = apps.get_model("auth", "Permission")

    permission = permission_model.objects.filter(
        content_type__app_label=RECOLLECT_APP_LABEL,
        codename=RECOLLECT_CODENAME,
    ).first()
    if permission is None:
        return
    for group in group_model.objects.filter(name__in=names):
        group.permissions.remove(permission)


class Migration(migrations.Migration):
    """Grant `collectors.recollect_package` to the reviewer and leadership role groups (CPM-AD-13, CPM-OPERATE-S08)."""

    dependencies = [
        # The product's own latest, so this application's migrations stay a single
        # line rather than two leaves the graph cannot order.
        ("core", "0012_ledger_time_indexes"),
        # The second grant, whose `forward` this one re-runs the shape of and whose
        # rows it converges with.
        ("core", "0011_grant_inventory_change"),
        # The model the codename hangs off. Without this dependency
        # `create_permissions` can run against a state in which
        # `package_recollections` does not exist, and the content type it
        # resolves would be for a model the historical registry has never heard of.
        ("collectors", "0015_package_recollections"),
        # Both are touched by the forward function: `Group` and `Permission` come
        # from `auth`, and `create_permissions` resolves content types.
        ("auth", "0012_alter_user_first_name_max_length"),
        ("contenttypes", "0002_remove_content_type_name"),
    ]

    operations = [
        # Not elidable, for the reason `0005` is not: squashing this away would
        # take the only guarantee that the two groups hold the permission, and
        # every "Collect now" would then be refused as forbidden.
        migrations.RunPython(forward, reverse, elidable=False),
    ]
