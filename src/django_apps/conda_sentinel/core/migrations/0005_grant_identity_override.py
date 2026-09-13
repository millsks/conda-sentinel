"""Attach the first real role-group grant: the audited identity override.

The call `core/0001_provision_role_groups`'s own docstring said would have to be
written. That migration provisions the three role groups from the role contract
and deliberately makes no `create_permissions` call, because every role slot
declared an empty permission tuple -- and it records, in as many words, that
"when `CPM-EP-APP` attaches the first real codename, that call has to be added
here the way it was added" in `django_service/users/migrations/0003_provision_designated_groups`.
`CPM-IDENTITY-S05` is that moment, arriving from `CPM-EP-IDENTITY` rather than
`CPM-EP-APP` because `CPM-FR-3` puts the permission on the *write* and the
surface that calls it comes later.

**A second migration rather than an edit to `0001`.** A data migration that has
already run against a deployed database is not re-run by editing it, so the grant
would reach no environment that had migrated before today. This one runs on every
database, new and existing, and it provisions through the same one writer
(`AD-27`) with the same `preserve_existing=True` -- so a role group that shares a
name with a designated group keeps what the claims contract asked for.

**The cost of that keyword, because it is the thing that will surprise somebody.**
`preserve_existing=True` adds and removes nothing, which is what a *secondary*
declaration owes a row it shares -- and it means the role contract cannot revoke
by omission. Deleting `IDENTITY_OVERRIDE_PERMISSION` from
`core/roles.py`'s `ROLE_GROUP_PERMISSIONS` will leave every already-provisioned
deployment holding the grant, silently and forever. `provision_groups`' own
docstring says so: "whoever removes one of its codenames has to say so." Saying
so means a migration shaped like this one's `reverse`, and `reverse` is
consequently not only rollback machinery -- it is the worked example of how the
grant is taken away.

**`create_permissions` first, and it is the whole reason this file is not three
lines.** `Permission` rows are created by the `post_migrate` signal, not by a
migration. On a fresh database the whole `migrate` invocation runs before that
signal fires, so a pass here would find `auth_permission` empty for the `identity`
models and attach nothing at all -- silently, because attaching zero permissions
is not an error, and the only symptom would be that every override is refused as
forbidden. `users/0003` solved this once; this is the same solution against the
`identity` app config.

Two further reasons the work is not inline, both `0001`'s: `*/migrations/*` is
omitted from coverage, so logic written here would be invisible to the floor, and
a migration that owns behaviour cannot be re-run by anything but `migrate`.
"""

import structlog
from django.db import migrations

logger = structlog.get_logger(__name__)


def forward(apps, schema_editor):
    """Create the identity permissions, then re-provision the role groups.

    `app_config.models_module` is set truthy and cleared again because
    `create_permissions` returns early on an app config without one, and the
    historical registry's app configs are stubs that have no models module. This
    is Django's own documented workaround for the ordering, applied to the stub
    rather than to the live app config on purpose: clearing the live one would
    make `post_migrate` skip permission creation for `identity` for the rest of
    the process.

    An unconfigured contract logs and returns, on `0001`'s terms exactly: a fresh
    clone is migrated long before anyone has role groups to declare, and a
    migration that refused would make `migrate` unusable during bring-up.
    """
    from django.conf import settings
    from django.contrib.auth.management import create_permissions

    from conda_sentinel.core.roles import IDENTITY_OVERRIDE_PERMISSION
    from conda_sentinel.core.roles import role_group_permissions
    from django_service.users.provisioning import provision_groups

    app_config = apps.get_app_config("identity")
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

    # Narrowed to this migration's own codename since `CPM-OPERATE-S03` added a
    # second grant to the contract (`core/0011_grant_inventory_change`). Each
    # grant migration provisions the codename it is about and no other: on a
    # fresh database this one runs before `collectors/0013_inventory` exists,
    # and provisioning the whole contract here would log the inventory codename
    # as unresolved on every first `migrate` -- a warning about nothing, from the
    # one pass an operator reads most carefully. Applied databases are unaffected:
    # a data migration that has already run is not re-run by editing it, and the
    # rows it left hold exactly what this narrowing would have written.
    own = {
        name: tuple(code for code in codenames if code == IDENTITY_OVERRIDE_PERMISSION)
        for name, codenames in role_group_permissions(contract).items()
    }
    provision_groups(
        own,
        apps,
        declared_by="role_contract",
        preserve_existing=True,
    )


def reverse(apps, schema_editor):
    """Detach the override permission from the groups this migration granted it to.

    **Scoped to the role contract's own names, and that is the whole of the
    care.** `forward` provisions with `preserve_existing=True` precisely so it
    speaks for what it asks for and never for what another declaration asked --
    the rule that stops a role pass clearing the staff group's permissions on a
    shared row. A reverse that stripped the codename from *every* group holding it
    would re-create that disarming through the rollback path: a group some other
    contract, some other migration or an administrator in the admin had granted it
    to would be silently revoked by unapplying a migration that never granted it.
    So the set is read from the contract, exactly as `0001`'s reverse reads it,
    and narrowed further to the slots whose declaration actually asks for this
    codename.

    An unconfigured contract names nothing and this returns, on `0001`'s terms: it
    undoes exactly what `forward` does, and a contract that provisioned nothing has
    nothing to roll back.

    The `Permission` row itself is left alone. Deleting it would take with it every
    grant this migration did not make, and it is `post_migrate`'s to create and to
    leave. Groups, users and memberships are untouched for the same reason: this
    migration created none of them.
    """
    from django.conf import settings

    from conda_sentinel.core.roles import IDENTITY_APP_LABEL
    from conda_sentinel.core.roles import IDENTITY_OVERRIDE_CODENAME
    from conda_sentinel.core.roles import IDENTITY_OVERRIDE_PERMISSION
    from conda_sentinel.core.roles import role_group_permissions

    contract = settings.ROLE_CONTRACT
    if not contract.is_configured:
        return

    names = [
        name
        for name, codenames in role_group_permissions(contract).items()
        if IDENTITY_OVERRIDE_PERMISSION in codenames
    ]
    if not names:
        return

    group_model = apps.get_model("auth", "Group")
    permission_model = apps.get_model("auth", "Permission")

    permission = permission_model.objects.filter(
        content_type__app_label=IDENTITY_APP_LABEL,
        codename=IDENTITY_OVERRIDE_CODENAME,
    ).first()
    if permission is None:
        return
    for group in group_model.objects.filter(name__in=names):
        group.permissions.remove(permission)


class Migration(migrations.Migration):
    """Grant `identity.override_package_identity` to the leadership role group (CPM-AD-14)."""

    dependencies = [
        # The product's own latest, so this application's migrations stay a single
        # line rather than two leaves the graph cannot order.
        # `CPM-EVIDENCE-S09` landed `0004_collection_run_package` while this story
        # was in review, so this depends on that rather than on `0003` -- one line
        # per application, never two leaves the graph cannot order.
        ("core", "0004_collection_run_package"),
        # The migration whose docstring specified this one, and the one that
        # created the group rows this pass attaches a permission to.
        ("core", "0001_provision_role_groups"),
        # The model the codename hangs off. Without this dependency
        # `create_permissions` below can run against a state in which
        # `identity_overrides` does not exist, and the content type it resolves
        # would be for a model the historical registry has never heard of.
        ("identity", "0003_identity_override"),
        # Both are touched by the forward function: `Group` and `Permission` come
        # from `auth`, and `create_permissions` resolves content types.
        ("auth", "0012_alter_user_first_name_max_length"),
        ("contenttypes", "0002_remove_content_type_name"),
    ]

    operations = [
        # Not elidable, for the reason `0001_provision_role_groups` is not:
        # squashing this away would take the only guarantee that the leadership
        # group holds the override permission, and every correction would then be
        # refused as forbidden with nothing saying why.
        migrations.RunPython(forward, reverse, elidable=False),
    ]
