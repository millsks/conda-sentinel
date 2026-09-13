"""Attach the second role-group grant: the audited inventory change.

`CPM-OPERATE-S03` moves the inventory from a CSV inside the wheel to a governed
table, and `CPM-FR-3` as amended names it the second human write that mutates
governed reference data -- so it carries the three obligations the identity
override carries, and the first of them is a permission
(`collectors.change_inventory`, declared in `core/roles.py` and attached on
`collectors.InventoryChange.Meta.permissions`). This migration is what grants it,
on exactly the terms `0005_grant_identity_override` set: a second migration rather
than an edit to `0005`, because a data migration that has already run against a
deployed database is not re-run by editing it, and this one runs on every
database, new and existing.

**`create_permissions` first, for the reason `0005` gives.** `Permission` rows are
created by the `post_migrate` signal, not by a migration; on a fresh database the
whole `migrate` invocation runs before that signal fires, so a pass here would
find `auth_permission` empty for the `collectors` models and attach nothing at
all -- silently, because attaching zero permissions is not an error, and the only
symptom would be that every inventory change is refused as forbidden.

**`preserve_existing=True`, and the cost `0005` states.** The role contract is a
secondary declaration over `auth_group` rows the claims contract also writes to,
so it adds what it asks for and removes nothing -- which means it cannot revoke by
omission. Deleting `INVENTORY_CHANGE_PERMISSION` from `ROLE_GROUP_PERMISSIONS`
leaves every already-provisioned deployment holding it; `reverse` below is the
worked example of how the grant is taken away.

**Each grant provisions its own codename and no other.** `0005` grants the
identity override, this one grants the inventory change, and neither touches the
other's: on a fresh database `0005` runs before `collectors/0013_inventory`
exists, so a pass that provisioned the whole contract there would log the
inventory codename as unresolved on every first `migrate`. The two converge to
the same rows whichever order a rollback and a re-apply happen in, because each
adds only what it asks for and `preserve_existing=True` removes nothing.
"""

import structlog
from django.db import migrations

logger = structlog.get_logger(__name__)


def forward(apps, schema_editor):
    """Create the collectors permissions, then re-provision the role groups.

    `app_config.models_module` is set truthy and cleared again because
    `create_permissions` returns early on an app config without one, and the
    historical registry's app configs are stubs that have no models module --
    Django's own documented workaround, applied to the stub rather than to the
    live app config so that `post_migrate` still creates permissions for
    `collectors` for the rest of the process.

    An unconfigured contract logs and returns, on `0001`'s terms: a fresh clone is
    migrated long before anyone has role groups to declare.
    """
    from django.conf import settings
    from django.contrib.auth.management import create_permissions

    from conda_sentinel.core.roles import INVENTORY_APP_LABEL
    from conda_sentinel.core.roles import INVENTORY_CHANGE_PERMISSION
    from conda_sentinel.core.roles import role_group_permissions
    from django_service.users.provisioning import provision_groups

    app_config = apps.get_app_config(INVENTORY_APP_LABEL)
    app_config.models_module = True
    try:
        create_permissions(
            app_config,
            apps=apps,
            using=schema_editor.connection.alias,
            verbosity=0,
        )
    finally:
        app_config.models_module = None

    contract = settings.ROLE_CONTRACT
    if not contract.is_configured:
        logger.warning(
            "authorization.provisioning_skipped",
            reason="role_contract_unconfigured",
        )
        return

    own = {
        name: tuple(code for code in codenames if code == INVENTORY_CHANGE_PERMISSION)
        for name, codenames in role_group_permissions(contract).items()
    }
    provision_groups(
        own,
        apps,
        declared_by="role_contract",
        preserve_existing=True,
    )


def reverse(apps, schema_editor):
    """Detach the inventory permission from the groups this migration granted it to.

    Scoped to the role contract's own names and narrowed to the slots whose
    declaration asks for this codename, exactly as `0005`'s reverse is: a reverse
    that stripped the codename from every group holding it would revoke a grant
    some other contract, migration or administrator made. The `Permission` row
    itself, the groups, the users and the memberships are all left alone; this
    migration created none of them.
    """
    from django.conf import settings

    from conda_sentinel.core.roles import INVENTORY_APP_LABEL
    from conda_sentinel.core.roles import INVENTORY_CHANGE_CODENAME
    from conda_sentinel.core.roles import INVENTORY_CHANGE_PERMISSION
    from conda_sentinel.core.roles import role_group_permissions

    contract = settings.ROLE_CONTRACT
    if not contract.is_configured:
        return

    names = [
        name
        for name, codenames in role_group_permissions(contract).items()
        if INVENTORY_CHANGE_PERMISSION in codenames
    ]
    if not names:
        return

    group_model = apps.get_model("auth", "Group")
    permission_model = apps.get_model("auth", "Permission")

    permission = permission_model.objects.filter(
        content_type__app_label=INVENTORY_APP_LABEL,
        codename=INVENTORY_CHANGE_CODENAME,
    ).first()
    if permission is None:
        return
    for group in group_model.objects.filter(name__in=names):
        group.permissions.remove(permission)


class Migration(migrations.Migration):
    """Grant `collectors.change_inventory` to the leadership role group (CPM-AD-14, CPM-OPERATE-S03)."""

    dependencies = [
        # The product's own latest, so this application's migrations stay a single
        # line rather than two leaves the graph cannot order.
        ("core", "0010_background_jobs"),
        # The first grant, whose `forward` this one re-runs the shape of and whose
        # rows it converges with.
        ("core", "0005_grant_identity_override"),
        # The model the codename hangs off. Without this dependency
        # `create_permissions` can run against a state in which `inventory_changes`
        # does not exist, and the content type it resolves would be for a model
        # the historical registry has never heard of.
        ("collectors", "0013_inventory"),
        # Both are touched by the forward function: `Group` and `Permission` come
        # from `auth`, and `create_permissions` resolves content types.
        ("auth", "0012_alter_user_first_name_max_length"),
        ("contenttypes", "0002_remove_content_type_name"),
    ]

    operations = [
        # Not elidable, for the reason `0005` is not: squashing this away would
        # take the only guarantee that the leadership group holds the permission,
        # and every inventory change would then be refused as forbidden.
        migrations.RunPython(forward, reverse, elidable=False),
    ]
