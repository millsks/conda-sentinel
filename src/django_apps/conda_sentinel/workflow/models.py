"""One table for every queue item, keyed on a finding rather than on an observation.

`CPM-AD-22` in two models. `WorkflowItem` is the piece of work; `WorkflowTransition`
is the append-only record of every move anybody made, written in the same transaction
as the move itself (`CPM-AD-23`).

**One table, three queues.** The decision says the three queues are "filtered views
over one table, not three models", and names what that prevents: the same item
existing twice in two role-exclusive queues with diverging state. One row cannot
diverge from itself, and routing is an update to a column rather than a create-and-
delete that can half-happen.

**Keyed on the finding, and unique on it.** The uniqueness constraint is the whole of
`CPM-APP-S04`'s second acceptance criterion: a re-observation that inserts a new
evidence row finds the existing item by key and cannot open a second one, because the
database will not have it. Enforced there rather than in the opening service, because
a rule enforced in one writer is a convention -- and this product will grow a second
writer the first time somebody backfills.

**The item points at the finding key, not at an evidence row.** There is deliberately
no foreign key to `vulnerability_findings`: the row that produced the item is
superseded the next time a collector runs, and an item pointing at a superseded row
would age into pointing at history. `finding_facts` carries the readable form so a
reviewer can see what the key stands for without joining anything, and the detail
view reaches current evidence through the *package* and the key rather than through
the item.

**`claimed_by` is meaningful in exactly one state**, and the constraint says so
rather than the service remembering. An item that is `resolved` and still claimed
reads as work in progress on every queue listing that sorts by claim.

**On the `AD-` prefix.** A bare `AD-n` in this repository is an *inherited* platform
decision; a decision from this product's own architecture spine always carries the
`CPM-` prefix.
"""

from __future__ import annotations

from typing import Final

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from conda_sentinel.core.finding_keys import FINDING_FACTS_LENGTH
from conda_sentinel.core.finding_keys import FINDING_KEY_LENGTH
from conda_sentinel.workflow.states import QUEUE_LENGTH
from conda_sentinel.workflow.states import STATE_LENGTH
from conda_sentinel.workflow.states import ItemState
from conda_sentinel.workflow.states import Queue

__all__ = [
    "CLAIMED_ONLY_IN_PROGRESS",
    "EXACTLY_ONE_AUTHOR",
    "JUSTIFICATION_LENGTH",
    "ONE_ITEM_PER_FINDING",
    "ORIGIN_LENGTH",
    "SYSTEM_ORIGIN",
    "WorkflowItem",
    "WorkflowTransition",
]

#: The constraint names, declared here so the cases that assert the database refuses
#: a violation can name them too.
ONE_ITEM_PER_FINDING: Final[str] = "workflow_one_item_per_finding_key"
CLAIMED_ONLY_IN_PROGRESS: Final[str] = "workflow_claimed_only_while_in_progress"
EXACTLY_ONE_AUTHOR: Final[str] = "workflow_transition_exactly_one_author"

#: What `WorkflowTransition.origin` says when the product itself made the move
#: (`CPM-OPERATE-S11`): the one non-human author, closing the open items of a
#: package the inventory no longer lists. A person's transition carries an
#: `actor` and a blank origin; the product's carries this and no actor -- never
#: a fake account, because the product holds no role and borrows nobody's.
SYSTEM_ORIGIN: Final[str] = "system"

#: How long an origin may be. Sized for the one value declared, with room for a
#: second product-authored path should one ever be argued for.
ORIGIN_LENGTH: Final[int] = 32

#: How long a recorded justification may be. Generous: the one transition that
#: requires it is a decision not to fix something, and the reason is the only thing
#: a later reader has to go on.
JUSTIFICATION_LENGTH: Final[int] = 2000


class WorkflowItem(models.Model):
    """One piece of work, in one queue, keyed on the finding that produced it. Table `workflow_items`."""

    #: What this work is filed under, across every re-observation of the fact behind
    #: it. See `core/finding_keys.py` for what makes it stable and why an evidence
    #: row id would not be.
    #: Unique, and the uniqueness lives in `Meta.constraints` rather than in
    #: `unique=True` here. Declaring both creates two indexes for one rule, and the
    #: field-level one is what the database names when it refuses -- so a case
    #: asserting the refusal could never match the constraint this table meant to
    #: declare.
    finding_key = models.CharField(_("finding key"), max_length=FINDING_KEY_LENGTH)

    #: The readable form of the key, so a reviewer never has to reverse a digest.
    #: Not part of the key and not unique: two items could in principle carry the
    #: same facts about different packages, and the key already separates them.
    finding_facts = models.CharField(_("finding facts"), max_length=FINDING_FACTS_LENGTH, blank=True, default="")

    #: The package the work is about. A real relation, so a queue can join to
    #: identity and health without a second lookup, and so deleting a package with
    #: open work is refused by the database.
    package = models.ForeignKey(
        "identity.Package",
        on_delete=models.PROTECT,
        related_name="workflow_items",
        verbose_name=_("package"),
    )

    #: Which queue it currently sits in. Routing changes this and creates nothing.
    queue = models.CharField(_("queue"), max_length=QUEUE_LENGTH, choices=Queue.choices)

    #: Where it is in the machine. Only `workflow/services.py` moves it, and only
    #: along a transition `workflow/states.py` declares.
    state = models.CharField(
        _("state"),
        max_length=STATE_LENGTH,
        choices=ItemState.choices,
        default=ItemState.OPEN,
    )

    #: Who has claimed it, and null whenever nobody has. Constrained to be set in
    #: `in_progress` and unset everywhere else.
    claimed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="claimed_workflow_items",
        null=True,
        blank=True,
        default=None,
        verbose_name=_("claimed by"),
    )

    #: When the policy run first opened this item, and when it last moved. Both are
    #: supplied by the writer from an injected clock (`CPM-AD-26`); neither has a
    #: default, so a row written by a path that skipped the service is a database
    #: error rather than a row stamped with whenever it was inserted.
    opened_at = models.DateTimeField(_("opened at"))
    changed_at = models.DateTimeField(_("changed at"))

    class Meta:
        """The table the architecture names, not the one Django would derive."""

        db_table = "workflow_items"
        verbose_name = _("workflow item")
        verbose_name_plural = _("workflow items")
        constraints = (
            # `CPM-APP-S04`'s AC 2, as a schema rule. A re-observation that inserts a
            # new evidence row cannot open a second item for the same finding,
            # whatever the writer believes -- and the writer this protects against is
            # the one nobody has written yet.
            #
            # The only declaration of the rule -- see the field above for why it is
            # not also `unique=True`.
            models.UniqueConstraint(fields=("finding_key",), name=ONE_ITEM_PER_FINDING),
            # A claim means something in one state only. An item that is `resolved`
            # and still claimed reads as work in progress on every listing that sorts
            # by claim, and the person named on it gets asked about work they
            # finished last month.
            models.CheckConstraint(
                condition=(
                    models.Q(state=ItemState.IN_PROGRESS, claimed_by__isnull=False)
                    | (~models.Q(state=ItemState.IN_PROGRESS) & models.Q(claimed_by__isnull=True))
                ),
                name=CLAIMED_ONLY_IN_PROGRESS,
            ),
        )
        indexes = (
            # The read every queue performs: one queue, the states that are still
            # work. Ordering is the priority pass's and is applied by the view.
            models.Index(fields=("queue", "state"), name="workflow_queue_state"),
            # "What work is open on this package", which the package detail view asks
            # for every render.
            models.Index(fields=("package", "state"), name="workflow_package_state"),
        )

    def __str__(self) -> str:
        """Return what this item is about and where it is.

        Returns:
            A one-line summary naming the queue, the state and the facts behind the
            finding -- the three things a reader needs to tell two items apart in a
            log line or an admin listing.

        """
        return f"{self.queue}/{self.state}: {self.finding_facts or self.finding_key}"


class WorkflowTransition(models.Model):
    """Every move anybody made, appended in the same transaction as the move. Table `workflow_transitions`.

    **Not an `AppendOnlyModel`, and the difference is worth stating.** That base is
    for *evidence* -- observations of the outside world, which `CPM-AD-2` forbids
    updating because a re-observation is a new fact. This is an audit trail of things
    people did inside the product. It is equally never updated, but it carries
    `occurred_at` rather than `observed_at` and it answers a different question, so
    inheriting would put it in every evidence sweep in the suite and make each of
    them slightly wrong about what it covers.

    **Written in the transaction that moves the item** (`CPM-AD-23`), which is the
    only way the two can never disagree: an audit row written afterwards is a row
    that is missing whenever the process dies in between, and the moves that go
    missing are exactly the ones somebody will later want to ask about.
    """

    item = models.ForeignKey(
        WorkflowItem,
        on_delete=models.PROTECT,
        related_name="transitions",
        verbose_name=_("item"),
    )

    #: Where it went. Both states, because "what did this look like before" is the
    #: question an audit trail is read to answer, and reconstructing it by walking
    #: earlier rows is work a reader should not have to do.
    from_state = models.CharField(_("from state"), max_length=STATE_LENGTH, choices=ItemState.choices)
    to_state = models.CharField(_("to state"), max_length=STATE_LENGTH, choices=ItemState.choices)

    #: The queue at the moment of the move, so a routed item's history says which
    #: queue each step happened in rather than only where it ended up.
    queue = models.CharField(_("queue"), max_length=QUEUE_LENGTH, choices=Queue.choices)

    #: Who did it. `PROTECT`, so a user with audit history cannot be deleted out from
    #: under it -- an audit row naming nobody is not an audit row.
    #:
    #: Nullable since `CPM-OPERATE-S11`, and only for the product's own move:
    #: `origin` names the author then, and `Meta.constraints` makes exactly one of
    #: the two do so on every row. The shape is `collectors.InventoryChange`'s --
    #: "an author is exactly one of a person and something that is not one" --
    #: so a row can never name nobody and never name two.
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="workflow_transitions",
        verbose_name=_("actor"),
        null=True,
        blank=True,
        default=None,
    )

    #: What made the move when no person did: `SYSTEM_ORIGIN` for the product
    #: closing the items of a package the inventory no longer lists
    #: (`CPM-OPERATE-S11`). Blank on a human move, which names its actor instead.
    origin = models.CharField(_("origin"), max_length=ORIGIN_LENGTH, blank=True, default="")

    #: Why, where the transition demands a reason. Required for accepting a risk and
    #: blank everywhere else -- the service enforces which, from the declaration.
    justification = models.CharField(_("justification"), max_length=JUSTIFICATION_LENGTH, blank=True, default="")

    #: When. From the injected clock, with no default, on the terms `opened_at` above
    #: states.
    occurred_at = models.DateTimeField(_("occurred at"))

    class Meta:
        """The table the architecture names."""

        db_table = "workflow_transitions"
        verbose_name = _("workflow transition")
        verbose_name_plural = _("workflow transitions")
        indexes = (models.Index(fields=("item", "occurred_at"), name="workflow_item_occurred"),)
        constraints = (
            # Exactly one author: a person, or the product by name. `actor` is
            # nullable so the product can move an item without a fake account, and
            # this is what stops the nullability becoming "nobody": a row with
            # neither, and a row with both, are both refused by the database.
            models.CheckConstraint(
                condition=(
                    (models.Q(actor__isnull=False) & models.Q(origin=""))
                    | (models.Q(actor__isnull=True) & ~models.Q(origin=""))
                ),
                name=EXACTLY_ONE_AUTHOR,
            ),
        )

    def __str__(self) -> str:
        """Return the move this row records.

        Returns:
            A one-line summary naming both states and the author -- the actor's
            id for a person's move, the origin for the product's.

        """
        author = self.origin if self.actor_id is None else str(self.actor_id)
        return f"{self.from_state} -> {self.to_state} by {author}"
