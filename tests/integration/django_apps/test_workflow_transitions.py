"""`CPM-APP-S04`: one item per finding, one function that moves it, one audit trail.

`CPM-AD-22` asks for four things in one sentence -- lock the row, check the expected
prior state, refuse on mismatch, append the audit row in the same transaction -- and
each of them prevents a different failure. So each has its own case, and the cases
are written so that removing any one clause makes exactly one of them fail.

**The stale-move case is the one worth reading.** The lock alone does not catch it:
by the time the lock is held, the item is simply in a different state, and a service
that only locked would move it from wherever it happened to be. What catches it is
the caller saying what it *believed* -- which is the difference between "resolve this
item" and "resolve this item, which I saw was in progress".

**The re-observation case is `CPM-APP-S04`'s reason for existing.** Evidence is
append-only, so tonight's run inserts a new row for the advisory it saw last night.
The item must be found, not created, and must keep whatever state a human moved it
to. A test that only checked "no duplicate row" would pass on a service that reset
the state to `open`, which is the same failure wearing a different shape.

Every test rolls back: `@pytest.mark.django_db` wraps each in a transaction.
"""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import TYPE_CHECKING
from typing import Final

import pytest
from django.conf import settings
from django.contrib.auth.models import Group
from django.db import IntegrityError
from django.db import models
from django.db import transaction

from conda_sentinel.collectors.match_confidence import MatchConfidence
from conda_sentinel.collectors.models import VulnerabilityFinding
from conda_sentinel.collectors.outcomes import MATCHED
from conda_sentinel.core.clock import FixedClock
from conda_sentinel.core.roles import LEADERSHIP
from conda_sentinel.core.roles import PACKAGING_ENGINEER
from conda_sentinel.core.roles import SECURITY_REVIEWER
from conda_sentinel.identity.models import IdentityConfidence
from conda_sentinel.identity.models import Package
from conda_sentinel.workflow.models import ONE_ITEM_PER_FINDING
from conda_sentinel.workflow.models import WorkflowItem
from conda_sentinel.workflow.models import WorkflowTransition
from conda_sentinel.workflow.services import WorkflowError
from conda_sentinel.workflow.services import apply_transition
from conda_sentinel.workflow.services import open_item
from conda_sentinel.workflow.states import SYSTEM_TRANSITIONS
from conda_sentinel.workflow.states import TRANSITIONS
from conda_sentinel.workflow.states import ItemState
from conda_sentinel.workflow.states import Queue
from conda_sentinel.workflow.states import transition_for
from tests.factories import UserFactory

if TYPE_CHECKING:
    from django_service.users.models import User

pytestmark = pytest.mark.integration

NOW: Final[datetime] = datetime(2026, 9, 4, 6, 12, tzinfo=UTC)
LATER: Final[datetime] = NOW + timedelta(hours=1)

A_JUSTIFICATION: Final[str] = "Internal fork; the affected code path is not reachable from our entry points."

#: How many moves a person may make, as `docs/conda-sentinel/the-queues.md` documents them.
SEVEN_HUMAN_MOVES: Final[int] = 7


def a_package(name: str = "aiohttp") -> Package:
    """Return one identified package.

    Args:
        name: Its canonical name.

    Returns:
        The saved package.

    """
    return Package.objects.create(
        canonical_name=name,
        resolved_at=NOW,
        confidence=IdentityConfidence.VERIFIED,
        identity_source="pypi",
        associator_key=f"pypi:{name}",
    )


def an_advisory(
    package: Package,
    *,
    observed_at: datetime = NOW,
    advisory_id: str = "CVE-2024-23334",
) -> VulnerabilityFinding:
    """Return one saved advisory finding.

    Args:
        package: The package it is about.
        observed_at: When it was observed.
        advisory_id: Which advisory.

    Returns:
        The saved finding.

    """
    return VulnerabilityFinding.objects.create(
        package=package,
        observed_at=observed_at,
        state=MATCHED,
        advisory_id=advisory_id,
        severity="critical",
        affected_range="<3.9.2",
        matched_version="3.9.1",
        match_confidence=MatchConfidence.EXACT_VERSION,
    )


def a_user(*roles: str) -> User:
    """Return a user holding these product roles.

    Args:
        *roles: The role slot names.

    Returns:
        The saved user.

    """
    user = UserFactory.create()
    for role in roles:
        user.groups.add(Group.objects.get(name=getattr(settings.ROLE_CONTRACT, role)))
    return user


def an_open_item(package: Package | None = None) -> WorkflowItem:
    """Return one item opened by the policy run, in `open`.

    Args:
        package: The package, created when not given.

    Returns:
        The saved item.

    """
    subject = package or a_package()
    return open_item(
        evidence=an_advisory(subject),
        package=subject,
        queue=Queue.REMEDIATION.value,
        clock=FixedClock(instant=NOW),
    ).item


def triaged(item: WorkflowItem, actor: User) -> WorkflowItem:
    """Move an item to `triaged`, which most cases start from.

    Args:
        item: The open item.
        actor: Who acknowledges it.

    Returns:
        The moved item.

    """
    return apply_transition(
        item_id=item.pk,
        expected_state=ItemState.OPEN.value,
        to_state=ItemState.TRIAGED.value,
        actor=actor,
        clock=FixedClock(instant=NOW),
    )


# ---------------------------------------------------------------------------
# AC 1 and AC 2: one item per finding, across re-observation.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_an_item_is_keyed_on_the_finding_and_not_on_the_evidence_row() -> None:
    """AC 1: the key is the evidence's declared one, not its primary key.

    Asserted against the evidence's own `finding_key()` rather than against a literal,
    so a change to the key mechanism moves both together or fails here.
    """
    package = a_package()
    finding = an_advisory(package)

    item = open_item(
        evidence=finding,
        package=package,
        queue=Queue.REMEDIATION.value,
        clock=FixedClock(instant=NOW),
    ).item

    expected_key, expected_facts = finding.finding_key()
    assert item.finding_key == expected_key
    assert item.finding_facts == expected_facts
    # The key names the *table* and the package, never the row. A key carrying the
    # evidence id would be re-observation-unstable, which the case below proves
    # directly -- this asserts the shape that makes it so.
    assert item.finding_key.startswith(f"{VulnerabilityFinding._meta.db_table}:{package.pk}:")  # noqa: SLF001


@pytest.mark.django_db
def test_a_re_observation_finds_the_existing_item_rather_than_opening_a_second() -> None:
    """AC 2, and the reason this story exists.

    The second finding is a *different row* -- append-only means a re-observation
    inserts -- and it must resolve to the same item.
    """
    package = a_package()
    first = open_item(
        evidence=an_advisory(package),
        package=package,
        queue=Queue.REMEDIATION.value,
        clock=FixedClock(instant=NOW),
    )
    second = open_item(
        evidence=an_advisory(package, observed_at=LATER),
        package=package,
        queue=Queue.REMEDIATION.value,
        clock=FixedClock(instant=LATER),
    )

    assert first.created is True
    assert second.created is False
    assert second.item.pk == first.item.pk
    assert WorkflowItem.objects.count() == 1


@pytest.mark.django_db
def test_an_accepted_finding_does_not_come_back_as_unactioned_work() -> None:
    """AC 2 in the words the criterion uses, and the half a duplicate check would miss.

    A service that reset the state to `open` on re-observation would create no second
    row and would still put an accepted finding back at the top of a queue.
    """
    package = a_package()
    reviewer = a_user(SECURITY_REVIEWER)
    item = an_open_item(package)
    triaged(item, reviewer)
    apply_transition(
        item_id=item.pk,
        expected_state=ItemState.TRIAGED.value,
        to_state=ItemState.ACCEPTED.value,
        actor=reviewer,
        clock=FixedClock(instant=NOW),
        justification=A_JUSTIFICATION,
    )

    open_item(
        evidence=an_advisory(package, observed_at=LATER),
        package=package,
        queue=Queue.REMEDIATION.value,
        clock=FixedClock(instant=LATER),
    )

    assert WorkflowItem.objects.get(pk=item.pk).state == ItemState.ACCEPTED.value
    assert WorkflowItem.objects.count() == 1


@pytest.mark.django_db
def test_the_database_refuses_a_second_item_for_one_finding() -> None:
    """The rule is a schema constraint, not a convention the opening service keeps.

    A rule enforced in one writer holds until somebody writes a second one -- a
    backfill, a management command, a fixture -- and this product will grow one.
    """
    package = a_package()
    item = an_open_item(package)

    # Matched on the column rather than on `ONE_ITEM_PER_FINDING`, because the two
    # backends this suite runs on word the refusal differently: PostgreSQL names the
    # constraint, and sqlite implements a `UniqueConstraint` as a unique index and
    # names the columns. Matching the constraint name would pass in the gate and fail
    # on every developer's machine -- see docs/accelerator/development.md on the parity gap.
    # The constraint's own name is asserted against the model's declaration in
    # `test_the_uniqueness_rule_is_declared_once`.
    with pytest.raises(IntegrityError, match=r"finding_key"), transaction.atomic():
        WorkflowItem.objects.create(
            finding_key=item.finding_key,
            package=package,
            queue=Queue.COMPLIANCE_REVIEW.value,
            state=ItemState.OPEN.value,
            opened_at=NOW,
            changed_at=NOW,
        )


# ---------------------------------------------------------------------------
# AC 3: the declared transition, the lock, the expected state, the audit row.
# ---------------------------------------------------------------------------


def test_the_uniqueness_rule_is_declared_once_and_by_name() -> None:
    """The rule the case above proves the database enforces, named where it is declared.

    Split out because the two backends word their refusals differently, so the case
    above cannot match on the name -- and a rule nothing names is one a later reader
    cannot find from the failure message.
    """
    names = {constraint.name for constraint in WorkflowItem._meta.constraints}  # noqa: SLF001 - model metadata
    unique_on = {
        tuple(constraint.fields)
        for constraint in WorkflowItem._meta.constraints  # noqa: SLF001 - as above
        if isinstance(constraint, models.UniqueConstraint)
    }

    assert ONE_ITEM_PER_FINDING in names
    assert unique_on == {("finding_key",)}
    assert WorkflowItem._meta.get_field("finding_key").unique is False, (  # noqa: SLF001 - as above
        "declaring both `unique=True` and a named UniqueConstraint creates two indexes for one rule"
    )


@pytest.mark.django_db
def test_a_declared_move_is_applied_and_recorded_together() -> None:
    """The happy path, and both halves of it.

    `CPM-AD-23` puts the audit row in the transaction that moves the item, so a case
    that checked only the state would pass on a service that wrote no history at all.
    """
    actor = a_user(PACKAGING_ENGINEER)
    item = an_open_item()

    moved = triaged(item, actor)

    assert moved.state == ItemState.TRIAGED.value
    assert moved.changed_at == NOW
    record = WorkflowTransition.objects.get(item=item)
    assert record.from_state == ItemState.OPEN.value
    assert record.to_state == ItemState.TRIAGED.value
    assert record.actor == actor
    assert record.occurred_at == NOW


@pytest.mark.django_db
def test_a_move_the_machine_does_not_declare_is_refused() -> None:
    """`open` straight to `resolved` skips the acknowledgement the queue exists to record.

    The refusal is what makes the transition table the machine rather than a
    suggestion.
    """
    actor = a_user(LEADERSHIP)
    item = an_open_item()

    with pytest.raises(WorkflowError, match=r"declares no move"):
        apply_transition(
            item_id=item.pk,
            expected_state=ItemState.OPEN.value,
            to_state=ItemState.RESOLVED.value,
            actor=actor,
            clock=FixedClock(instant=NOW),
        )

    assert WorkflowItem.objects.get(pk=item.pk).state == ItemState.OPEN.value
    assert WorkflowTransition.objects.count() == 0


@pytest.mark.django_db
def test_a_move_made_against_a_state_the_item_has_left_is_refused() -> None:
    """The stale move, which the lock alone does not catch.

    Two people open the queue. The first accepts the risk. The second, whose page
    still says `triaged`, clicks resolve. By the time the second holds the lock the
    item is simply in a different state -- so what refuses it is the caller having
    said what it believed.
    """
    reviewer = a_user(SECURITY_REVIEWER)
    item = an_open_item()
    triaged(item, reviewer)
    apply_transition(
        item_id=item.pk,
        expected_state=ItemState.TRIAGED.value,
        to_state=ItemState.ACCEPTED.value,
        actor=reviewer,
        clock=FixedClock(instant=NOW),
        justification=A_JUSTIFICATION,
    )

    with pytest.raises(WorkflowError, match=r"moved it since this page was rendered"):
        apply_transition(
            item_id=item.pk,
            expected_state=ItemState.TRIAGED.value,
            to_state=ItemState.IN_PROGRESS.value,
            actor=reviewer,
            clock=FixedClock(instant=LATER),
        )

    assert WorkflowItem.objects.get(pk=item.pk).state == ItemState.ACCEPTED.value


@pytest.mark.django_db
def test_a_refused_move_writes_no_audit_row() -> None:
    """The audit trail records what happened, not what was attempted.

    A refused move changed nothing, and a history full of attempts would bury the
    moves that did.
    """
    actor = a_user(LEADERSHIP)
    item = an_open_item()

    with pytest.raises(WorkflowError):
        apply_transition(
            item_id=item.pk,
            expected_state=ItemState.TRIAGED.value,
            to_state=ItemState.RESOLVED.value,
            actor=actor,
            clock=FixedClock(instant=NOW),
        )

    assert WorkflowTransition.objects.count() == 0


@pytest.mark.django_db
def test_the_history_reads_in_order_and_carries_both_states() -> None:
    """ "What did this look like before" is the question an audit trail answers.

    Carrying both states means a reader gets it from one row rather than by walking
    backwards through the others.
    """
    actor = a_user(PACKAGING_ENGINEER)
    item = an_open_item()
    triaged(item, actor)
    apply_transition(
        item_id=item.pk,
        expected_state=ItemState.TRIAGED.value,
        to_state=ItemState.IN_PROGRESS.value,
        actor=actor,
        clock=FixedClock(instant=LATER),
    )

    history = list(WorkflowTransition.objects.filter(item=item).order_by("occurred_at", "pk"))

    assert [(row.from_state, row.to_state) for row in history] == [
        (ItemState.OPEN.value, ItemState.TRIAGED.value),
        (ItemState.TRIAGED.value, ItemState.IN_PROGRESS.value),
    ]


# ---------------------------------------------------------------------------
# The role gate and the justification, both from the declaration.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_accepting_a_risk_is_refused_to_a_role_that_does_not_own_it() -> None:
    """`CPM-FR-31` gives risk acceptance to the security and compliance reviewer.

    A packaging engineer deciding not to fix something is the decision that most
    needs to be somebody's, and the machine says whose.
    """
    engineer = a_user(PACKAGING_ENGINEER)
    item = an_open_item()
    triaged(item, engineer)

    with pytest.raises(WorkflowError, match=r"requires one of"):
        apply_transition(
            item_id=item.pk,
            expected_state=ItemState.TRIAGED.value,
            to_state=ItemState.ACCEPTED.value,
            actor=engineer,
            clock=FixedClock(instant=NOW),
            justification=A_JUSTIFICATION,
        )

    assert WorkflowItem.objects.get(pk=item.pk).state == ItemState.TRIAGED.value


@pytest.mark.django_db
def test_accepting_a_risk_without_a_reason_is_refused() -> None:
    """The reason is the only thing a later reader has to go on.

    Required by the *declaration*, so no caller can forget it -- which is the point
    of the transition table carrying the flag rather than each view checking.
    """
    reviewer = a_user(SECURITY_REVIEWER)
    item = an_open_item()
    triaged(item, reviewer)

    with pytest.raises(WorkflowError, match=r"cannot be made without a reason"):
        apply_transition(
            item_id=item.pk,
            expected_state=ItemState.TRIAGED.value,
            to_state=ItemState.ACCEPTED.value,
            actor=reviewer,
            clock=FixedClock(instant=NOW),
            justification="   ",
        )


@pytest.mark.django_db
def test_an_accepted_risk_records_the_reason_on_the_audit_row() -> None:
    """Recorded where a later reader will look: beside the move, not on the item.

    The item says what it is now; the history says why it got there, and the reason
    belongs to the decision rather than to the current state.
    """
    reviewer = a_user(SECURITY_REVIEWER)
    item = an_open_item()
    triaged(item, reviewer)

    apply_transition(
        item_id=item.pk,
        expected_state=ItemState.TRIAGED.value,
        to_state=ItemState.ACCEPTED.value,
        actor=reviewer,
        clock=FixedClock(instant=NOW),
        justification=A_JUSTIFICATION,
    )

    accepted = WorkflowTransition.objects.get(item=item, to_state=ItemState.ACCEPTED.value)
    assert accepted.justification == A_JUSTIFICATION
    assert accepted.actor == reviewer


@pytest.mark.django_db
def test_an_ordinary_move_needs_no_reason() -> None:
    """Only one transition demands one, and requiring them everywhere teaches people to type "x"."""
    actor = a_user(LEADERSHIP)
    item = an_open_item()

    moved = triaged(item, actor)

    assert moved.state == ItemState.TRIAGED.value


# ---------------------------------------------------------------------------
# AC 4: routing changes the queue and creates nothing.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_routing_changes_the_queue_and_creates_no_second_item() -> None:
    """AC 4. `CPM-AD-22`: one item cannot diverge from itself, and two can.

    The count assertion is the criterion; the queue assertion is what makes it mean
    the item moved rather than that routing did nothing.
    """
    actor = a_user(SECURITY_REVIEWER)
    item = an_open_item()
    triaged(item, actor)

    routed = apply_transition(
        item_id=item.pk,
        expected_state=ItemState.TRIAGED.value,
        to_state=ItemState.ROUTED.value,
        actor=actor,
        clock=FixedClock(instant=LATER),
        queue=Queue.COMPLIANCE_REVIEW.value,
    )

    assert routed.pk == item.pk
    assert routed.queue == Queue.COMPLIANCE_REVIEW.value
    assert WorkflowItem.objects.count() == 1


@pytest.mark.django_db
def test_the_audit_row_records_the_queue_the_move_happened_in() -> None:
    """A routed item's history says which queue each step was in.

    Without it the trail says only where the item ended up, and "who was looking at
    this when it was triaged" becomes unanswerable.
    """
    actor = a_user(SECURITY_REVIEWER)
    item = an_open_item()
    triaged(item, actor)
    apply_transition(
        item_id=item.pk,
        expected_state=ItemState.TRIAGED.value,
        to_state=ItemState.ROUTED.value,
        actor=actor,
        clock=FixedClock(instant=LATER),
        queue=Queue.COMPLIANCE_REVIEW.value,
    )

    queues = list(WorkflowTransition.objects.filter(item=item).order_by("pk").values_list("queue", flat=True))

    assert queues == [Queue.REMEDIATION.value, Queue.COMPLIANCE_REVIEW.value]


# ---------------------------------------------------------------------------
# The claim, and the states nothing leaves.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_claiming_records_who_and_releasing_clears_it() -> None:
    """A claim is meaningful in one state, and the model constrains that.

    An item that is resolved and still claimed reads as work in progress on every
    listing that sorts by claim, and the person named gets asked about work they
    finished last month.
    """
    actor = a_user(PACKAGING_ENGINEER)
    item = an_open_item()
    triaged(item, actor)

    claimed = apply_transition(
        item_id=item.pk,
        expected_state=ItemState.TRIAGED.value,
        to_state=ItemState.IN_PROGRESS.value,
        actor=actor,
        clock=FixedClock(instant=LATER),
    )
    assert claimed.claimed_by == actor

    released = apply_transition(
        item_id=item.pk,
        expected_state=ItemState.IN_PROGRESS.value,
        to_state=ItemState.TRIAGED.value,
        actor=actor,
        clock=FixedClock(instant=LATER),
    )
    assert released.claimed_by is None


@pytest.mark.django_db
def test_resolving_clears_the_claim() -> None:
    """The state a listing sorts by claim would otherwise misreport for ever."""
    actor = a_user(PACKAGING_ENGINEER)
    item = an_open_item()
    triaged(item, actor)
    apply_transition(
        item_id=item.pk,
        expected_state=ItemState.TRIAGED.value,
        to_state=ItemState.IN_PROGRESS.value,
        actor=actor,
        clock=FixedClock(instant=LATER),
    )

    resolved = apply_transition(
        item_id=item.pk,
        expected_state=ItemState.IN_PROGRESS.value,
        to_state=ItemState.RESOLVED.value,
        actor=actor,
        clock=FixedClock(instant=LATER),
    )

    assert resolved.state == ItemState.RESOLVED.value
    assert resolved.claimed_by is None


@pytest.mark.django_db
@pytest.mark.parametrize("terminal", [ItemState.RESOLVED.value, ItemState.ACCEPTED.value])
def test_nothing_leaves_a_terminal_state(terminal: str) -> None:
    """Reopening would make "resolved" mean "resolved for now".

    A finding that comes back is either a new finding under a different key, or the
    same finding whose item is still there -- which is what the key is for.

    Args:
        terminal: The ending to try to move out of.

    """
    reviewer = a_user(SECURITY_REVIEWER)
    item = an_open_item()
    triaged(item, reviewer)
    if terminal == ItemState.ACCEPTED.value:
        apply_transition(
            item_id=item.pk,
            expected_state=ItemState.TRIAGED.value,
            to_state=terminal,
            actor=reviewer,
            clock=FixedClock(instant=NOW),
            justification=A_JUSTIFICATION,
        )
    else:
        apply_transition(
            item_id=item.pk,
            expected_state=ItemState.TRIAGED.value,
            to_state=ItemState.IN_PROGRESS.value,
            actor=reviewer,
            clock=FixedClock(instant=NOW),
        )
        apply_transition(
            item_id=item.pk,
            expected_state=ItemState.IN_PROGRESS.value,
            to_state=terminal,
            actor=reviewer,
            clock=FixedClock(instant=NOW),
        )

    with pytest.raises(WorkflowError, match=r"already finished"):
        apply_transition(
            item_id=item.pk,
            expected_state=terminal,
            to_state=ItemState.TRIAGED.value,
            actor=reviewer,
            clock=FixedClock(instant=LATER),
        )


@pytest.mark.django_db
def test_no_transition_returns_an_item_to_open() -> None:
    """`open` means "nobody has looked at this", and only the policy run writes it.

    Asserted over the declaration rather than by attempting every move: what makes it
    true is that no `(*, open)` row exists, and a case per state would still miss the
    one somebody adds later.
    """
    assert [t for t in TRANSITIONS if t.to_state == ItemState.OPEN.value] == []
    assert [t for t in SYSTEM_TRANSITIONS if t.to_state == ItemState.OPEN.value] == []


@pytest.mark.django_db
def test_the_human_table_is_unchanged_by_the_products_own() -> None:
    """`CPM-OPERATE-S11` added a second table rather than rows to this one.

    The product's close is declared in `SYSTEM_TRANSITIONS` and applied by
    `close_for_absence`; `transition_for` reads only `TRANSITIONS`, so no
    `apply_transition` call -- however its arguments are spelled -- can make the
    product's move, and the documented human table keeps its seven rows.
    """
    human_moves = {(t.from_state, t.to_state) for t in TRANSITIONS}
    system_moves = {(t.from_state, t.to_state) for t in SYSTEM_TRANSITIONS}

    assert len(TRANSITIONS) == SEVEN_HUMAN_MOVES
    assert all(transition_for(*move) is None for move in system_moves - human_moves)
    assert all(t.required_roles for t in TRANSITIONS), "every human move names who may make it"


@pytest.mark.django_db
def test_moving_an_item_that_does_not_exist_is_refused() -> None:
    """Rather than raising whatever the ORM raises, which a view would report as a 500."""
    with pytest.raises(WorkflowError, match=r"no workflow item has id"):
        apply_transition(
            item_id=999_999,
            expected_state=ItemState.OPEN.value,
            to_state=ItemState.TRIAGED.value,
            actor=a_user(LEADERSHIP),
            clock=FixedClock(instant=NOW),
        )


@pytest.mark.django_db
def test_an_item_describes_itself_by_queue_state_and_facts() -> None:
    """The three things a reader needs to tell two items apart in a log or the admin.

    The primary key is not one of them: an operator reading a queue knows which
    advisory they are looking at from the facts, and nothing from an integer.
    """
    item = an_open_item()

    described = str(item)

    assert item.queue in described
    assert item.state in described
    assert "CVE-2024-23334" in described


@pytest.mark.django_db
def test_an_item_with_no_readable_facts_falls_back_to_the_key() -> None:
    """The facts are a convenience and the key is the identity.

    An item whose readable form was never written -- an older row, a backfill -- still
    has to describe itself as something rather than as a queue and a state with a
    blank where the subject should be.
    """
    item = an_open_item()
    item.finding_facts = ""

    assert item.finding_key in str(item)


@pytest.mark.django_db
def test_a_transition_describes_the_move_it_records() -> None:
    """An audit row read on its own says what happened, without joining to the item."""
    actor = a_user(LEADERSHIP)
    item = an_open_item()
    triaged(item, actor)

    described = str(WorkflowTransition.objects.get(item=item))

    assert ItemState.OPEN.value in described
    assert ItemState.TRIAGED.value in described
