"""The product's role contract: three group *names*, read from the environment.

The brief names three account-holding roles -- a security and compliance
reviewer, a packaging engineer, and platform and engineering leadership -- and
says the identity provider's group claims are what determine which one a person
holds. This module is the product-level half of that: it declares the three
*slots* and reads the *name* of the group that occupies each one from the
environment. It never reads a membership and never resolves a claim; the
platform already does both (`AD-10`), and `CPM-FR-30` is satisfied there.

It is shaped exactly like `config/authorization/claims.py`, deliberately:

* Every field holds a group **name**, never a value and never a membership.
* Nothing is defaulted. An unset variable yields the empty string, and the empty
  string means *unconfigured*. Defaulting a plausible name here would turn a
  missing configuration into a wrong one that provisions groups nobody declared.
* Values are stripped, so a variable holding only whitespace -- a block scalar in
  a ConfigMap, a trailing space in a `.env` line -- reads as unset rather than as
  a truthy name that matches nothing.
* Nothing raises. This module is imported from `config/settings/base.py`, so a
  raise here would fire during every management command and every test run, and
  `migrate` on a fresh clone would stop being usable.

No group name appears anywhere below. Keying by slot is what keeps it that way:
the same source serves a deployment whose reviewers are called
`sec-review-prod` and one whose reviewers are called anything else.

Imports nothing from `django.contrib.auth` and nothing from `django.apps`. It is
imported at settings-import time, long before the app registry is populated, and
an import of either would make the settings module unloadable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from typing import Final

if TYPE_CHECKING:
    import environ

__all__ = [
    "IDENTITY_APP_LABEL",
    "IDENTITY_OVERRIDE_CODENAME",
    "IDENTITY_OVERRIDE_PERMISSION",
    "INVENTORY_APP_LABEL",
    "INVENTORY_CHANGE_CODENAME",
    "INVENTORY_CHANGE_PERMISSION",
    "LEADERSHIP",
    "PACKAGING_ENGINEER",
    "ROLE_ENVIRONMENT_VARIABLES",
    "ROLE_GROUP_PERMISSIONS",
    "SECURITY_REVIEWER",
    "RoleContract",
    "load_role_contract",
    "role_group_permissions",
]

#: The three role slots the brief's role table names. They are slots, not group
#: names: what the group that occupies each one is called is configuration, read
#: at call time. Spelled as the contract's own field names so the two cannot
#: disagree -- `tests/unit/django_apps/test_roles.py` pins the correspondence.
SECURITY_REVIEWER: Final = "security_reviewer"
PACKAGING_ENGINEER: Final = "packaging_engineer"
LEADERSHIP: Final = "leadership"

#: The three variables the contract is read from, in field order. Declared here
#: rather than only inline in `load_role_contract` so the operator-facing
#: documentation can be pinned against the names actually read -- the same reason
#: `CLAIMS_ENVIRONMENT_VARIABLES` exists.
#:
#: `CPM_`-prefixed, not `COMPONENT_`-prefixed: the four `COMPONENT_*` names are
#: the platform's contract, inherited by every component built from the
#: accelerator, while these three are this product's own.
ROLE_ENVIRONMENT_VARIABLES: Final[tuple[str, ...]] = (
    "CPM_SECURITY_REVIEWER_GROUP",
    "CPM_PACKAGING_ENGINEER_GROUP",
    "CPM_LEADERSHIP_GROUP",
)

#: The application the one governed write lives in, and the codename that gates
#: it. Declared *here* rather than in `identity/models.py`, which is the module
#: that attaches the codename to a model: this file is imported from
#: `config/settings/base.py`, long before the app registry is populated, so it
#: could not read a value out of a model even if it wanted to -- while
#: `identity/models.py` imports these two freely, because this module imports
#: nothing but `dataclasses` and `typing`.
#:
#: One spelling, therefore, rather than a codename written in the model and an
#: `app_label.codename` string written again in the grant below. Those two would
#: drift the day either is renamed, and the symptom is the quietest one there is:
#: `provision_groups` logs the unresolved codename at warning and attaches
#: nothing, so the permission simply stops being held and every override is
#: refused as forbidden.
#: `tests/unit/django_apps/test_identity_overrides.py` reconciles this against
#: `IdentityOverride._meta` in both halves, so a model that moved application or
#: renamed its permission fails there.
IDENTITY_APP_LABEL: Final = "identity"
IDENTITY_OVERRIDE_CODENAME: Final = "override_package_identity"
IDENTITY_OVERRIDE_PERMISSION: Final = f"{IDENTITY_APP_LABEL}.{IDENTITY_OVERRIDE_CODENAME}"

#: The second governed write and the codename that gates it (`CPM-OPERATE-S03`,
#: `CPM-FR-3` as amended). The inventory is governed reference data in a table
#: the `collectors` application declares -- `collectors.InventoryChange` attaches
#: this codename on its `Meta.permissions`, exactly as `identity.IdentityOverride`
#: attaches the first -- and `collectors/inventory.py`'s service is the one door
#: that checks it. Declared here for the reason the first pair is: one spelling,
#: importable at settings time, reconciled against the model's `_meta` by
#: `tests/unit/django_apps/test_inventory_service.py`.
INVENTORY_APP_LABEL: Final = "collectors"
INVENTORY_CHANGE_CODENAME: Final = "change_inventory"
INVENTORY_CHANGE_PERMISSION: Final = f"{INVENTORY_APP_LABEL}.{INVENTORY_CHANGE_CODENAME}"

#: What each role group may do, keyed by **role slot** and never by group name.
#:
#: **One grant, and it is the first.** Every tuple here was empty until
#: `CPM-IDENTITY-S05`: `core` has no models, the product has no views, and a
#: codename written before the thing it guards exists would resolve to nothing,
#: be logged as unresolved on every `migrate`, and grant nobody anything -- while
#: still being maintained, shown in the admin, and inherited from by whoever
#: writes the first role-scoped surface. That is how a decorative grant drifts
#: into a load-bearing one; `SUPERUSER_ROLE`'s empty tuple in
#: `django_service/users/provisioning.py` is the same decision for the same
#: reason. The grants arrive with the surfaces they guard (`CPM-AD-13`).
#:
#: `CPM-FR-3` makes the audited identity override the **only** human write in the
#: product that mutates governed reference data, and `CPM-AD-14` puts the whole
#: weight of the rule on it -- so its codename is the first thing there has ever
#: been to grant, and it arrives with the write rather than with a surface.
#:
#: **Two grants since `CPM-OPERATE-S03`, both to leadership.** The inventory table
#: is the second governed write `CPM-FR-3` names, and its permission arrives with
#: the write on the terms the first did -- granted by `core/0011_grant_inventory_change`
#: on every database, new and existing, because a data migration that already ran
#: is not re-run by editing it.
#:
#: **Leadership alone, and the other two stay empty on purpose.** The security
#: and compliance reviewer and the packaging engineer read the review queue and
#: act on what it says; correcting governed reference data is a different act,
#: and `CPM-IDENTITY-S05` says in as many words that neither slot receives it.
#: Two empty tuples beside one grant are also what keeps this table honest: they
#: are what a test asserts is *still* empty, so a second permission attached to
#: the wrong slot is a failure rather than a diff nobody reads.
#:
#: **Deleting a codename from this table does not revoke the grant, and there is
#: no way to make it.** The role contract is a *secondary* declaration over the
#: `auth_group` rows the claims contract also writes to, so it provisions with
#: `preserve_existing=True` -- it adds what it asks for and removes nothing,
#: because the alternative is a pass that silently clears whatever the claims
#: contract attached to a shared group. `provision_groups`' own docstring states
#: the trade in as many words: "a secondary declaration cannot revoke by
#: omission; whoever removes one of its codenames has to say so." Concretely:
#: taking `IDENTITY_OVERRIDE_PERMISSION` off leadership below leaves every
#: already-provisioned deployment holding it, and the person who wants it gone
#: writes a migration that detaches it -- the shape
#: `core/0005_grant_identity_override`'s `reverse` already has.
#: `tests/integration/django_apps/test_role_groups.py` pins this in both
#: directions, so it is a property somebody demonstrated rather than a caveat in
#: a comment.
ROLE_GROUP_PERMISSIONS: Final[dict[str, tuple[str, ...]]] = {
    SECURITY_REVIEWER: (),
    PACKAGING_ENGINEER: (),
    LEADERSHIP: (IDENTITY_OVERRIDE_PERMISSION, INVENTORY_CHANGE_PERMISSION),
}


@dataclass(frozen=True, slots=True)
class RoleContract:
    """The names of the three groups that confer the product's roles.

    Each field is a group name, never a membership: `security_reviewer` is the
    name of the group whose members review, not a person and not a boolean.

    Attributes:
        security_reviewer: Name of the group conferring the security and
            compliance reviewer role.
        packaging_engineer: Name of the group conferring the packaging engineer
            role.
        leadership: Name of the group conferring the platform and engineering
            leadership role.

    """

    security_reviewer: str
    packaging_engineer: str
    leadership: str

    @property
    def is_configured(self) -> bool:
        """Report whether all three names were supplied.

        A plain predicate, as `ClaimsContract.is_configured` is. Its one reader
        is `core/0001_provision_role_groups`, which logs
        `authorization.provisioning_skipped` and provisions nothing when this is
        False; no startup condition reads it and nothing refuses on it. A
        partially configured contract is unconfigured -- two roles provisioned
        and one silently absent is the shape of a misconfiguration that presents
        later as a permissions bug.

        Stripped before the test, not merely truthiness-tested. `load_role_contract`
        already strips what it reads, so this only bites a contract constructed
        directly -- a settings module's local fill, or a test -- but a
        whitespace-only name that reported *configured* would provision a `Group`
        row no claim can ever match, which is the exact failure the strip in the
        loader exists to prevent.

        Returns:
            True when every field holds a name that survives stripping.

        """
        return all(name.strip() for name in (self.security_reviewer, self.packaging_engineer, self.leadership))


def load_role_contract(env: environ.Env) -> RoleContract:
    """Read the role contract from the environment.

    Reads exactly three variables, each defaulting to the empty string. There is
    no fallback value of any kind, and each value is stripped -- see the module
    docstring for why both of those are load-bearing rather than tidy.

    Args:
        env: The `environ.Env` the settings module already holds. Passing it in
            rather than constructing one keeps a single `.env` read (FR-38).

    Returns:
        The contract as configured, which may be entirely unconfigured. Never
        raises.

    """
    security_reviewer, packaging_engineer, leadership = (
        env.str(name, default="").strip() for name in ROLE_ENVIRONMENT_VARIABLES
    )
    return RoleContract(
        security_reviewer=security_reviewer,
        packaging_engineer=packaging_engineer,
        leadership=leadership,
    )


def role_group_permissions(contract: RoleContract) -> dict[str, tuple[str, ...]]:
    """Collapse the three role slots onto the group names the contract gives them.

    Keyed by name rather than by slot, for the reason `_designated_groups` is:
    nothing stops an operator pointing two role variables at one group -- a small
    deployment where the packaging engineers are also the leadership audience.
    Iterating the slots directly would then hand the provisioner the same name
    twice, and the second pass would `set` its own codenames over the first's,
    clearing whatever the earlier slot asked for. Unioning first makes that
    configuration mean what it reads as.

    Names are stripped and a name that is blank after stripping is skipped rather
    than passed through, on the same terms as `is_configured`. Provisioning is
    only ever reached with a configured contract, but a blank name that did reach
    it would be created as a `Group` row with an empty name -- a row no claim can
    ever match and nothing would delete.

    Args:
        contract: The role contract, configured or not.

    Returns:
        One entry per distinct configured group name, in the order the slots
        declare them, each carrying the union of the permissions its slots ask
        for. Empty when nothing is configured.

    """
    by_name: dict[str, tuple[str, ...]] = {}
    for role, configured in (
        (SECURITY_REVIEWER, contract.security_reviewer),
        (PACKAGING_ENGINEER, contract.packaging_engineer),
        (LEADERSHIP, contract.leadership),
    ):
        name = configured.strip()
        if not name:
            continue
        held = by_name.get(name, ())
        by_name[name] = (*held, *(code for code in ROLE_GROUP_PERMISSIONS[role] if code not in held))
    return by_name
