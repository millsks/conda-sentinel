# Identity and authorization

Who a person is, how the product learns it, and how it decides what they may see.

This page is the **product** half. The platform half — how an OIDC identity becomes a
Django user, what the claims contract is, and how the first administrator is
established — is inherited from the accelerator and lives in
[Authentication](../accelerator/authentication.md). Read that first if you have not.

---

## The chain, end to end

```
identity provider
   │  asserts claims in a token
   ▼
claims contract          COMPONENT_IDENTITY_CLAIM, COMPONENT_GROUP_CLAIM,
   │                     COMPONENT_STAFF_GROUP, COMPONENT_SUPERUSER_GROUP
   ▼
sync_authorization       groups on the Django user are made to equal the claim
   │
   ▼
role contract            CPM_SECURITY_REVIEWER_GROUP, CPM_PACKAGING_ENGINEER_GROUP,
   │                     CPM_LEADERSHIP_GROUP
   ▼
three product roles      security_reviewer · packaging_engineer · leadership
   │
   ▼
each surface declares what it requires; one mechanism enforces it
```

Seven environment variables. Four are the platform's (`COMPONENT_`-prefixed) and
three are this product's (`CPM_`-prefixed), and the prefix is the boundary: the first
four are inherited by every component built from the accelerator, the last three exist
only here.

**None of the seven has a default.** Unset stays unset, and a start-up check refuses
rather than guessing. A default group name would be a security decision made by a
codebase on behalf of a deployment.

---

## There are no passwords

Authentication is delegated. An unauthenticated request to any authenticated page
redirects to the identity provider, never to a local credential form — `LOGIN_URL` is
built from the OIDC provider id for exactly that reason.

Two ways in, and both end at the same identity:

| Path | Used by | Verified how |
|---|---|---|
| **OIDC browser flow** | People, on the screens | allauth's OIDC provider, PKCE enabled |
| **Bearer token** | Integrations, on the API | The token's signature, against JWKS |

The Bearer path never accepts the token's own `alg` header — the algorithm allowlist
is configuration (`COMPONENT_OIDC_ALGORITHMS`, `RS256` by default), because trusting
`alg` is the `alg=none` and algorithm-confusion family of attacks. `aud` is required
and is never defaulted to "any": an unconfigured audience refuses every token rather
than accepting all of them. Clock skew tolerance is **zero** by default.

---

## Group membership is re-derived on every sign-in

`sync_authorization` makes the stored authorization *equal* what the claims assert. Not
a union — an equality. Groups asserted are added, groups no longer asserted are
**removed**, and `is_staff` and `is_superuser` are each set from their own designated
group and cleared when it is not asserted.

The consequence to internalise, because it surprises everybody once:

!!! warning "A group granted by hand will not survive"

    Adding a group in the Django admin, or in a shell, works exactly until that
    person signs in again. Then it is erased. The identity provider is the source of
    truth for authorization, and there is no local override.

    Locally, this is why you use a persona rather than editing your own user.

### An absent group claim is a 401, and an empty one is not

These are different and the distinction is deliberate:

- The configured group claim is **missing from the token** → the authentication is
  refused. A misconfigured claim *name* is indistinguishable from a person holding no
  groups once the row is written, and treating one as the other is how a
  misconfiguration presents as a permissions bug that nobody can reproduce.
- The claim is **present and empty** → a legitimate assertion of no groups. It syncs
  normally, and the person holds no roles.

---

## The three product roles

They are **slots**, not group names. What the group occupying each slot is called is
your directory's business, read at call time from configuration:

| Slot | Variable | Reaches |
|---|---|---|
| `security_reviewer` | `CPM_SECURITY_REVIEWER_GROUP` | Compliance review queue; risk acceptance; **Collect now** on a package page |
| `packaging_engineer` | `CPM_PACKAGING_ENGINEER_GROUP` | Remediation queue |
| `leadership` | `CPM_LEADERSHIP_GROUP` | Identity review queue; the identity override; the inventory page; **Collect now** on a package page |

All three reach the read surfaces — Home, Packages, Reports, Coverage. The queues are
where they diverge.

`granted_roles()` is **the one place a group membership becomes a role**, so no view
ever compares a group name itself. A view that did would hard-code an operator's
naming convention into application code.

!!! note "Three is a contract, not a convenience"

    Adding a fourth role means a fourth environment variable, a fourth group for
    somebody to provision, and a change to a contract an audit pins at three. When
    what you actually need is one person who can see everything *locally*, that is
    what `operations-persona` is for — a local sign-in fixture, not a role.

---

## Every surface declares what it requires

Authorization is **declared per surface and enforced centrally**. Three mechanisms,
one for each kind of surface, and all three resolve through `granted_roles()`:

| Surface | Declares with | Example |
|---|---|---|
| HTML view | `RoleRequiredMixin` + `required_roles` | the queue views; the inventory page |
| DRF endpoint | `permission_classes` with `requires_roles(...)` | the identity override |
| Any surface open to all three | `AnyProductRole` | the packages API |

A refusal is logged under the event `authorization.refused`, **with the acting user
identity** — that is the string to alert on. Three services carry a permission of
their own beneath the role check and log their refusals under the same prefix:
`authorization.inventory_change_refused` (the inventory page),
`authorization.override_refused` (the identity override), and
`authorization.recollection_refused` — a **Collect now**
press by somebody whose group holds the role but not `collectors.recollect_package`,
which `core/0013_grant_recollect` attaches to the security-reviewer and leadership
groups on every `migrate`. Alert on the prefix and you have all of them.

!!! note "Collect now is gated per package, and that is an accepted risk"

    A press is refused while *that package* is in flight, and nothing counts
    presses across the estate: a reviewer holding the permission can press
    **Collect now** on package after package, and each press spends the
    sources' allowances the sweep would otherwise spend. That is the reviewer's
    own allowance to spend — the permission is granted to two roles for exactly
    that judgement — and every press is on the record in `package_recollections`
    with the acting identity, so a loop is visible after the fact. An
    estate-wide bound is a product decision nobody has asked for; if one is
    wanted, it belongs in the service, not in a view.

What a refused person reads names the *surface's requirement*, not their own
memberships:

> This surface is scoped to a role you do not hold. Ask whoever administers your
> directory groups for the role that covers it.

That wording is chosen. A list of the groups somebody holds is not something they can
act on; the requirement is what they take to an administrator.

### The audits that keep this honest

Authorization is the area where "mostly right" is a vulnerability, so several test
modules sweep it rather than checking cases:

- Every registered read surface is either gated or on a short, recorded list of
  deliberately ungated ones — and the list must stay smaller than half the surfaces.
- The roles a surface may require are exactly the three the contract declares.
- Three different ways of reinventing the check are detected, because a view writing
  its own `if user.groups...` is `CPM-AD-13`'s named failure.
- Every write path is swept, reading the methods off the route rather than asking
  whether the class has a `post` — a `ModelViewSet` defines `create`/`update`/`destroy`
  and would otherwise have slipped through. That one was a real miss, caught by
  widening the sweep.

---

## Locally, personas stand in for the provider

There is no identity provider on your laptop, so `/_local/` offers six seeded
identities. They are **claims fixtures**, not a bypass: signing in as one builds the
claim payload a provider would have sent and runs it through the same
`sync_authorization`. Every surface gates a persona by the same check it gates
anybody by.

| Persona | Holds | Reaches |
|---|---|---|
| `reviewer-persona` | security review | compliance review, Collect now |
| `engineer-persona` | packaging engineering | remediation |
| `leader-persona` | leadership | identity review, the identity override, the inventory page, Collect now |
| `reader-persona` | nothing | nothing — the zero-groups case |
| `staff-persona` | Django staff only | the admin |
| `operations-persona` | all three roles **and** staff | everything |

`reader-persona` exists to be refused; `operations-persona` exists so you can check
the whole navigation bar in one sign-in. **Use a single-role persona when the question
is whether a screen refuses the wrong person** — signed in as operations every screen
lets you in, which proves nothing.

No persona carries the superuser sentinel. A superuser bypasses every permission
check, so a superuser persona would make every local authorization check pass whether
or not the surface checked anything.

[Seeding them, and minting a development token](development.md#local-personas).

---

## The write paths

Almost everything in this product is read-only to the application. There are exactly
two API endpoints that change governed state, and one HTML page; all three are
role-scoped:

| Surface | Role | What it does |
|---|---|---|
| `POST /conda-sentinel/api/v1/packages/<id>/identity-override/` | `leadership` | Corrects a package's identity, with a required reason, recording who did it |
| `POST /conda-sentinel/api/v1/workflow-items/<id>/transition/` | the **current queue's** owning role | Moves a queue item |
| `POST /conda-sentinel/inventory/` | `leadership` | Adds, changes or retires an inventory row, with a required reason, recording who did it |

The transition endpoint resolves its permission from the queue the item is *in*, not
from a fixed role — so an item that has been routed to another queue is that queue's
to move. [The queues](the-queues.md).

None of the three writes a derived status. Nothing in the application layer can.

### The one write the product makes on its own

A fourth path to `workflow_transitions` is not a person's, and no role reaches it:

| Path | Author | What it does |
|---|---|---|
| the policy run's `workflow.open_queue_items` step | `origin=system`, no actor | Closes the open items of a package the inventory no longer lists at the run's cut-off, with a justification naming the absence |

It runs inside the policy run — a task, never a request — through
`close_for_absence`, which reads the product's own transition table rather than the
human one and checks no role: the product holds none and borrows nobody's account.
Every row it writes names the product by `origin`; `actor` is empty, and the database
refuses a transition that names neither an actor nor an origin, or both. `apply_transition`
stays a person's path and takes an actor unconditionally
([the queues](the-queues.md#the-products-own-table-one-move-one-circumstance)).

### The two governed writes carry a Django permission as well as a role

The identity override and the inventory change are the two human writes `CPM-FR-3`
names as mutating governed reference data, and each is gated **twice**: the surface on
the role, and the service on a Django permission the role group holds. The role is
what a person arrives with; the permission is what stops a second caller -- a shell, a
future command -- reaching the table ungated:

| Permission | Declared on | Granted to | Checked by |
|---|---|---|---|
| `identity.override_package_identity` | `identity.IdentityOverride` | `leadership`, by `core/0005_grant_identity_override` | `identity/services.py` |
| `collectors.change_inventory` | `collectors.InventoryChange` | `leadership`, by `core/0011_grant_inventory_change` | `collectors/inventory.py` |

Both grants are data migrations rather than edits to the role-group migration, so they
reach every database, new and existing; both provision with `preserve_existing=True`,
so a role group that shares a name with a designated group keeps what the claims
contract asked for. The cost is stated in both files: the role contract cannot revoke
by omission -- deleting a codename from `ROLE_GROUP_PERMISSIONS` leaves every
provisioned deployment holding it, and taking it away is a migration shaped like each
grant's own `reverse`.

An unattended `import-watchlist` is **not** permission-gated: it runs from the command
line, which is the gate for every admin process, and every audit row it writes names
the file in place of an actor.
