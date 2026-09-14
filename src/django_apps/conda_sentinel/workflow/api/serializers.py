"""What a queue action accepts, and what it returns.

**`expected_state` is required, and that is the design rather than a formality.**
`apply_transition` refuses a move whose caller believed the item was somewhere else,
and an API is exactly where that matters: two integrators automating the same queue
will race, and without it the second one's move silently lands on an item the first
already resolved. Making the field optional would give a client a way to opt out of
the check by omitting it -- so there is none.

**No `queue` field on the response beyond the item's own.** The routing move changes
the column and creates nothing (`CPM-APP-S04` AC 4), so the item that comes back is
the item that went in, moved.
"""

from __future__ import annotations

from typing import Any

from rest_framework import serializers

from conda_sentinel.workflow.states import ItemState
from conda_sentinel.workflow.states import Queue

__all__ = ["TransitionRequestSerializer", "WorkflowItemSerializer", "WorkflowTransitionSerializer"]


class TransitionRequestSerializer(serializers.Serializer[Any]):
    """One move of one queue item."""

    #: Where the caller believes the item is. Required; see the module docstring.
    expected_state = serializers.ChoiceField(choices=ItemState.choices)

    #: Where they want it. A closed choice rather than a free string, so a typo is a
    #: 400 naming the states rather than a 409 about a move the machine does not
    #: declare -- two different problems that should not read alike.
    to_state = serializers.ChoiceField(choices=ItemState.choices)

    #: Why, for the move that demands one. Which move that is belongs to
    #: `workflow/states.py`, not here: a serializer that knew would be a second
    #: place the transition table lives.
    justification = serializers.CharField(required=False, allow_blank=True, default="")

    #: Where to route it, for a routing move. Ignored otherwise.
    queue = serializers.ChoiceField(choices=Queue.choices, required=False, allow_blank=True, default="")


class WorkflowTransitionSerializer(serializers.Serializer[Any]):
    """One recorded move, as the history shows it."""

    from_state = serializers.CharField(allow_blank=True)
    to_state = serializers.CharField()
    queue = serializers.CharField()
    justification = serializers.CharField(allow_blank=True)
    occurred_at = serializers.DateTimeField()

    #: Who made the move -- `null` for the product's own close (`CPM-OPERATE-S11`),
    #: which names itself in `origin` instead. Exactly one of the two is set, by
    #: the model's constraint; a reader that sees both blank is reading a row that
    #: cannot exist.
    actor = serializers.CharField(source="actor.username", allow_null=True, default=None)
    origin = serializers.CharField(allow_blank=True)


class WorkflowItemSerializer(serializers.Serializer[Any]):
    """One queue item as it stands."""

    id = serializers.IntegerField(source="pk")
    package_id = serializers.IntegerField()
    queue = serializers.CharField()
    state = serializers.CharField()

    #: What the item is *about*, and the reason a replay does not open a second one.
    #: `CPM-AD-22` keys an item on this; an integrator storing it beside a ticket has
    #: the handle that survives a re-run.
    finding_key = serializers.CharField()

    opened_at = serializers.DateTimeField()
    changed_at = serializers.DateTimeField()
    claimed_by = serializers.CharField(source="claimed_by.username", allow_null=True, default=None)
