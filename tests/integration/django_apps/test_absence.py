"""`CPM-OPERATE-S11`: an absent package leaves the queues, and comes back with no manual step.

`CPM-OPERATE-S03` made inventory absence an observation -- a `not_found` snapshot
row -- and left every downstream reader ignoring it. This module drives the whole
chain that now reads it, through the product's own policy run: the selection
leaves an absent package out and counts it; the after-run step stamps the rollup
row; opening skips the package and closes its open items as a `system`
transition with a reason; the surfaces label it; the feedstock-gap report
excludes it and says so; and a later listing reverses all of it.

**Every read is cut-off bound**, which is the property the replay cases hold: a
run replayed at the same cut-off selects, stamps, skips and closes the same, and
closes nothing twice.

**The rows are written directly rather than through the ingestion collector.**
`tests/integration/django_apps/test_inventory_ingestion.py` owns the path that
produces absence rows and proves it produces them; what this module needs is
particular arrangements at particular instants, and `InventorySnapshot.objects.create`
is an insert, which is the one write an append-only model permits.

Every case rolls back: `@pytest.mark.django_db` wraps each in a transaction.
"""

from __future__ import annotations

from datetime import timedelta
from html import escape
from http import HTTPStatus
from typing import TYPE_CHECKING
from typing import Final

import pytest
from django.conf import settings
from django.contrib.auth.models import Group
from django.db import IntegrityError
from django.db import connection
from django.db import transaction
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from rest_framework.test import APIClient

from conda_sentinel.collectors import absence as absence_module
from conda_sentinel.collectors.absence import ABSENCE_STEP_NAME
from conda_sentinel.collectors.absence import Absence
from conda_sentinel.collectors.absence import absence_at
from conda_sentinel.collectors.license import package_locator
from conda_sentinel.collectors.match_confidence import MatchConfidence
from conda_sentinel.collectors.models import FeedstockSnapshot
from conda_sentinel.collectors.models import InventorySnapshot
from conda_sentinel.collectors.models import LicenseFinding
from conda_sentinel.collectors.models import VulnerabilityFinding
from conda_sentinel.collectors.outcomes import MATCHED
from conda_sentinel.collectors.outcomes import NORMALIZED
from conda_sentinel.collectors.selection import select_unresolved
from conda_sentinel.collectors.spdx import DetectionMethod
from conda_sentinel.core.after_run import AfterRunStepError
from conda_sentinel.core.after_run import step_registrations
from conda_sentinel.core.clock import FixedClock
from conda_sentinel.core.confidence import GATED_VALUE
from conda_sentinel.core.finding_keys import finding_key_of
from conda_sentinel.core.models import PackageHealth
from conda_sentinel.core.outcomes import OutcomeState
from conda_sentinel.core.policy_run import execute_policy_run
from conda_sentinel.core.replay import compare_runs
from conda_sentinel.core.roles import LEADERSHIP
from conda_sentinel.core.roles import PACKAGING_ENGINEER
from conda_sentinel.core.roles import SECURITY_REVIEWER
from conda_sentinel.identity.models import IdentityConfidence
from conda_sentinel.identity.models import Package
from conda_sentinel.policies.outcomes import PRESENT_AND_MAINTAINED
from conda_sentinel.surface.exports import export_csv
from conda_sentinel.surface.reports import ABSENT_FROM_THE_INVENTORY
from conda_sentinel.surface.reports import REPORTS_BY_SLUG
from conda_sentinel.workflow.api.serializers import WorkflowTransitionSerializer
from conda_sentinel.workflow.models import EXACTLY_ONE_AUTHOR
from conda_sentinel.workflow.models import SYSTEM_ORIGIN
from conda_sentinel.workflow.models import WorkflowItem
from conda_sentinel.workflow.models import WorkflowTransition
from conda_sentinel.workflow.opening import IDENTITY_FINDING_TABLE
from conda_sentinel.workflow.opening import LISTED_SINCE_FACT
from conda_sentinel.workflow.opening import OPENING_STEP_NAME
from conda_sentinel.workflow.services import WorkflowError
from conda_sentinel.workflow.services import apply_transition
from conda_sentinel.workflow.services import close_for_absence
from conda_sentinel.workflow.services import open_keyed_item
from conda_sentinel.workflow.states import SYSTEM_TRANSITIONS
from conda_sentinel.workflow.states import TERMINAL_STATES
from conda_sentinel.workflow.states import ItemState
from conda_sentinel.workflow.states import Queue
from tests.clocks import FIXED_INSTANT
from tests.clocks import OBSERVATION_GAP
from tests.factories import UserFactory
from tests.passes import A_RECORDED_POLICY_VERSION

if TYPE_CHECKING:
    from datetime import datetime

    from conda_sentinel.core.policy_run import PolicyRunSummary
    from django_service.users.models import User

pytestmark = pytest.mark.integration

#: Four sweeps, one gap apart, and the cut-off each run reads at. The run's own
#: clock answers later than every observation, on `CPM-AD-21`'s terms.
FIRST_SWEEP: Final[datetime] = FIXED_INSTANT - 3 * OBSERVATION_GAP
SECOND_SWEEP: Final[datetime] = FIXED_INSTANT - 2 * OBSERVATION_GAP
THIRD_SWEEP: Final[datetime] = FIXED_INSTANT - OBSERVATION_GAP
FOURTH_SWEEP: Final[datetime] = FIXED_INSTANT
RUN_CLOCK: Final[datetime] = FIXED_INSTANT + OBSERVATION_GAP

#: A breadth every listed row carries.
A_COMPONENT_COUNT: Final[int] = 12
A_LOB_COUNT: Final[int] = 3

#: A feedstock pushed to well inside the shipped inactivity threshold.
A_RECENT_PUSH: Final[datetime] = FIXED_INSTANT - timedelta(days=7)

A_JUSTIFICATION_FRAGMENT: Final[str] = "the inventory no longer lists the package"

#: One identity item and one remediation item: what the first run opens, and what
#: the second closes.
TWO_ITEMS: Final[int] = 2

#: How many packages leave in one sweep in the one-statement case.
A_SWEEP_OF_LEAVERS: Final[int] = 10

#: A confidence value `IdentityConfidence` does not declare, written straight into
#: the column so the rollup write for that package fails (`require_known_confidence`)
#: while every other package composes. `tests/integration/django_apps/test_rollup.py`'s
#: spelling.
AN_UNRECOGNISED_CONFIDENCE: Final[str] = "asserted"


def a_package(name: str, *, confidence: str = IdentityConfidence.UNMAPPED) -> Package:
    """Create one package.

    Args:
        name: Its canonical name.
        confidence: How certain its identity is; `unmapped` by default, because
            the identity queue is where most of these cases look.

    Returns:
        The saved package.

    """
    return Package.objects.create(
        canonical_name=name,
        resolved_at=FIXED_INSTANT,
        confidence=confidence,
        identity_source="pypi" if confidence == IdentityConfidence.VERIFIED else "",
        associator_key=f"pypi:{name}" if confidence == IdentityConfidence.VERIFIED else "",
    )


def listed(package: Package, at: datetime) -> InventorySnapshot:
    """Record one present observation.

    Args:
        package: The package the inventory listed.
        at: When.

    Returns:
        The saved snapshot.

    """
    return InventorySnapshot.objects.create(
        observed_at=at,
        package=package,
        source_package_key=package.canonical_name,
        state=OutcomeState.OK.value,
        internal_component_count=A_COMPONENT_COUNT,
        internal_lob_count=A_LOB_COUNT,
    )


def not_listed(package: Package, at: datetime) -> InventorySnapshot:
    """Record one absence observation, as the ingestion collector writes it.

    Args:
        package: The package the inventory stopped listing.
        at: When.

    Returns:
        The saved snapshot, with both counts NULL by the table's constraint.

    """
    return InventorySnapshot.objects.create(
        observed_at=at,
        package=package,
        source_package_key=package.canonical_name,
        state=OutcomeState.NOT_FOUND.value,
    )


def a_maintained_feedstock(package: Package) -> FeedstockSnapshot:
    """Record a feedstock pushed to recently, so the feedstock pass reaches a determinate verdict.

    Args:
        package: The package.

    Returns:
        The saved snapshot.

    """
    return FeedstockSnapshot.objects.create(
        package=package,
        observed_at=FIRST_SWEEP,
        state=OutcomeState.OK,
        feedstock_name=f"{package.canonical_name}-feedstock",
        last_recipe_activity_at=A_RECENT_PUSH,
        absence_established=False,
    )


def a_licence(package: Package) -> LicenseFinding:
    """Record one normalised licence finding, which the shipped rule set sends to review.

    Args:
        package: The package.

    Returns:
        The saved finding.

    """
    return LicenseFinding.objects.create(
        package=package,
        observed_at=FIRST_SWEEP,
        state=NORMALIZED,
        source=package_locator("conda-forge", package.canonical_name),
        channel="conda-forge",
        raw_license="MIT",
        normalized_license="MIT",
        detection_method=DetectionMethod.SPDX_IDENTIFIER.value,
    )


def an_absent_feedstock(package: Package) -> FeedstockSnapshot:
    """Record an established feedstock absence, so the feedstock pass reads `absent`.

    Args:
        package: The package.

    Returns:
        The saved snapshot.

    """
    return FeedstockSnapshot.objects.create(
        package=package,
        observed_at=FIRST_SWEEP,
        state=OutcomeState.NOT_FOUND,
        feedstock_name="",
        absence_established=True,
    )


def an_advisory(package: Package) -> VulnerabilityFinding:
    """Record one matched advisory, so the vulnerability pass opens remediation work.

    Args:
        package: The package.

    Returns:
        The saved finding.

    """
    return VulnerabilityFinding.objects.create(
        package=package,
        observed_at=FIRST_SWEEP,
        state=MATCHED,
        advisory_id="CVE-2024-23334",
        severity="critical",
        affected_range="<3.9.2",
        matched_version="3.9.1",
        match_confidence=MatchConfidence.EXACT_VERSION,
    )


def a_run(*, cutoff: datetime = FOURTH_SWEEP, at: datetime = RUN_CLOCK) -> PolicyRunSummary:
    """Execute one policy run at a stated cut-off, through the product's own orchestration.

    Args:
        cutoff: The instant every read is bound to.
        at: The instant the run's clock answers.

    Returns:
        What the run did, its after-run counts included.

    """
    return execute_policy_run(
        policy_version=A_RECORDED_POLICY_VERSION,
        clock=FixedClock(instant=at),
        evidence_cutoff=cutoff,
    )


def health_of(package: Package) -> PackageHealth:
    """Return a package's rollup row, freshly read.

    Args:
        package: The package.

    Returns:
        The row.

    """
    return PackageHealth.objects.get(package=package)


def items_of(package: Package) -> list[WorkflowItem]:
    """Return a package's queue items, oldest first.

    Args:
        package: The package.

    Returns:
        The items.

    """
    return list(WorkflowItem.objects.filter(package=package).order_by("pk"))


def a_user(*roles: str) -> User:
    """Return a user holding these product roles.

    Args:
        *roles: The role slot names.

    Returns:
        The saved user.

    """
    user: User = UserFactory.create()
    for role in roles:
        user.groups.add(Group.objects.get(name=getattr(settings.ROLE_CONTRACT, role)))
    return user


def a_reader() -> APIClient:
    """Return a client signed in as somebody holding every product role.

    Returns:
        The client.

    """
    client = APIClient()
    client.force_login(a_user(SECURITY_REVIEWER, PACKAGING_ENGINEER, LEADERSHIP))
    return client


def an_absence(*, since: datetime = FOURTH_SWEEP, last_listed: datetime | None = THIRD_SWEEP) -> Absence:
    """Return an absence reading built by hand, for the service cases.

    Args:
        since: When the inventory stopped listing the package.
        last_listed: When it last did.

    Returns:
        The reading.

    """
    return Absence(absent=True, since=since, last_listed=last_listed, listed_since=None)


# ---------------------------------------------------------------------------
# The reader at its cut-off boundaries.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_an_absence_row_at_the_cut_off_counts_and_one_after_it_does_not() -> None:
    """`observed_at <= cutoff`, the same bound every evidence read carries (`CPM-AD-25`).

    Three packages, their absence rows before, at and after one cut-off. The
    first two are absent at it and the third is not -- and at a later cut-off the
    third is, which is what makes the answer a function of the cut-off.
    """
    before = a_package("absent-before")
    listed(before, FIRST_SWEEP)
    not_listed(before, SECOND_SWEEP)
    at = a_package("absent-at")
    listed(at, FIRST_SWEEP)
    not_listed(at, THIRD_SWEEP)
    after = a_package("absent-after")
    listed(after, FIRST_SWEEP)
    not_listed(after, FOURTH_SWEEP)

    readings = absence_at(cutoff=THIRD_SWEEP)

    assert readings[before.pk] == Absence(absent=True, since=SECOND_SWEEP, last_listed=FIRST_SWEEP, listed_since=None)
    assert readings[at.pk] == Absence(absent=True, since=THIRD_SWEEP, last_listed=FIRST_SWEEP, listed_since=None)
    assert readings[after.pk] == Absence(absent=False, since=None, last_listed=FIRST_SWEEP, listed_since=None)
    assert absence_at(cutoff=FOURTH_SWEEP)[after.pk].absent is True


@pytest.mark.django_db
def test_a_package_the_inventory_never_observed_has_no_reading() -> None:
    """No row is not an absence, and a caller that asked for it gets no entry rather than a listed one."""
    package = a_package("never-observed")

    assert package.pk not in absence_at(cutoff=FOURTH_SWEEP)
    assert absence_at(cutoff=FOURTH_SWEEP, package_ids=[package.pk]) == {}


# ---------------------------------------------------------------------------
# The rollup carries absence, and its statuses are untouched.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_the_run_stamps_the_rollup_row_and_a_later_listing_clears_it() -> None:
    """`CPM-AD-11`: one row per package stays; two columns say what the inventory said.

    The columns are written by the after-run step at the run's cut-off, so the
    same package is stamped by one run and cleared by the next once the inventory
    lists it again -- with no manual step in between.
    """
    package = a_package("comes-and-goes")
    listed(package, FIRST_SWEEP)
    not_listed(package, SECOND_SWEEP)

    first = a_run(cutoff=SECOND_SWEEP)
    row = health_of(package)
    assert row.inventory_absent_since == SECOND_SWEEP
    assert row.inventory_last_listed == FIRST_SWEEP
    assert first.after_run[ABSENCE_STEP_NAME] == 1

    listed(package, THIRD_SWEEP)
    second = a_run(cutoff=THIRD_SWEEP)

    row = health_of(package)
    assert row.inventory_absent_since is None
    assert row.inventory_last_listed is None
    assert second.after_run[ABSENCE_STEP_NAME] == 0
    assert PackageHealth.objects.filter(package=package).count() == 1


@pytest.mark.django_db
def test_a_verified_absent_package_keeps_its_determinate_statuses() -> None:
    """Absence gates nothing: the statuses are computed exactly as before, and the row is labelled beside them.

    A `verified` package with a maintained feedstock reads `present_and_maintained`
    while listed, and reads the same once absent -- the feedstock evidence is still
    at the cut-off, and the inventory's silence about the package is not a verdict
    about its feedstock.
    """
    package = a_package("still-verified", confidence=IdentityConfidence.VERIFIED)
    a_maintained_feedstock(package)
    listed(package, FIRST_SWEEP)

    a_run(cutoff=FIRST_SWEEP)
    while_listed = health_of(package)
    assert while_listed.feedstock_presence_status == PRESENT_AND_MAINTAINED
    assert while_listed.inventory_absent_since is None

    not_listed(package, SECOND_SWEEP)
    a_run(cutoff=SECOND_SWEEP)

    once_absent = health_of(package)
    assert once_absent.feedstock_presence_status == PRESENT_AND_MAINTAINED
    assert once_absent.currency_status == while_listed.currency_status
    assert once_absent.priority_status == while_listed.priority_status
    assert once_absent.work_type_status == while_listed.work_type_status
    assert once_absent.inventory_absent_since == SECOND_SWEEP
    assert once_absent.inventory_last_listed == FIRST_SWEEP


@pytest.mark.django_db
def test_an_unmapped_absent_package_is_gated_by_its_confidence_and_never_by_its_absence() -> None:
    """`CPM-AD-4` still decides: an `unmapped` package reads `unknown` because it is unmapped, absent or not."""
    package = a_package("still-unmapped")
    a_maintained_feedstock(package)
    listed(package, FIRST_SWEEP)
    not_listed(package, SECOND_SWEEP)

    a_run(cutoff=SECOND_SWEEP)

    row = health_of(package)
    assert row.feedstock_presence_status == GATED_VALUE
    assert row.confidence == IdentityConfidence.UNMAPPED
    assert row.inventory_absent_since == SECOND_SWEEP


@pytest.mark.django_db
def test_the_absence_step_runs_before_the_opening_step() -> None:
    """Registration order is run order, and the rollup must carry absence before the queues read it.

    `collectors` is installed ahead of `workflow`, and each registers its step at
    `ready()`; the registry preserves that order and `run_after_run_steps` walks
    it. Pinned here rather than assumed, because a reordering of `INSTALLED_APPS`
    would pass every other case and label the package one run late.
    """
    installed = list(settings.INSTALLED_APPS)
    registered = list(step_registrations())

    assert installed.index("conda_sentinel.collectors") < installed.index("conda_sentinel.workflow")
    assert registered.index(ABSENCE_STEP_NAME) < registered.index(OPENING_STEP_NAME)

    summary = a_run()
    assert list(summary.after_run) == registered


# ---------------------------------------------------------------------------
# Opening skips, closing is a `system` write, and a re-listing opens fresh work.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_an_absent_package_is_not_offered_its_items_are_closed_and_the_closes_are_the_products() -> None:
    """The story's first acceptance criterion, end to end through the run.

    A package with two open items -- identity and remediation -- leaves the
    inventory. The next run leaves it out of the selection and counts it, stamps
    its row, and resolves both items by a transition with `origin=system`, no
    actor, and a justification naming the absence and the last-listed date.
    """
    package = a_package("leaving")
    listed(package, FIRST_SWEEP)
    an_advisory(package)
    verified = a_package("staying", confidence=IdentityConfidence.VERIFIED)
    listed(verified, FIRST_SWEEP)
    an_advisory(verified)

    first = a_run(cutoff=FIRST_SWEEP)
    (identity_item,) = items_of(package)
    assert identity_item.queue == Queue.IDENTITY_REVIEW.value
    (remediation_item,) = items_of(verified)
    assert remediation_item.queue == Queue.REMEDIATION.value
    assert first.after_run[OPENING_STEP_NAME] == TWO_ITEMS

    not_listed(package, SECOND_SWEEP)
    not_listed(verified, SECOND_SWEEP)
    second = a_run(cutoff=SECOND_SWEEP)

    selection = select_unresolved(cutoff=SECOND_SWEEP)
    assert selection.packages == ()
    assert selection.left_out_for_absence == 1
    assert second.after_run[OPENING_STEP_NAME] == TWO_ITEMS, "two closes, no opens"
    for item in (identity_item, remediation_item):
        item.refresh_from_db()
        assert item.state == ItemState.RESOLVED.value
        (move,) = WorkflowTransition.objects.filter(item=item)
        assert move.actor is None
        assert move.origin == SYSTEM_ORIGIN
        assert move.to_state == ItemState.RESOLVED.value
        assert A_JUSTIFICATION_FRAGMENT in move.justification
        assert SECOND_SWEEP.date().isoformat() in move.justification
        assert FIRST_SWEEP.date().isoformat() in move.justification
        assert move.occurred_at == RUN_CLOCK
    assert WorkflowItem.objects.count() == TWO_ITEMS, "nothing opened for the absent packages"


@pytest.mark.django_db
def test_a_re_listed_package_is_offered_again_and_opens_a_fresh_item() -> None:
    """The second acceptance criterion: a later listing reverses everything, and the old item stays closed.

    The fresh item's key carries the instant the new listing began, so
    `open_keyed_item`'s `get_or_create` finds nothing and opens -- and the item
    the product closed keeps its `resolved` state and its history.
    """
    package = a_package("returning")
    listed(package, FIRST_SWEEP)
    a_run(cutoff=FIRST_SWEEP)
    (original,) = items_of(package)

    not_listed(package, SECOND_SWEEP)
    a_run(cutoff=SECOND_SWEEP)
    original.refresh_from_db()
    assert original.state == ItemState.RESOLVED.value

    listed(package, THIRD_SWEEP)
    third = a_run(cutoff=THIRD_SWEEP)

    selection = select_unresolved(cutoff=THIRD_SWEEP)
    assert [entry.package.pk for entry in selection.packages] == [package.pk]
    assert selection.left_out_for_absence == 0
    assert third.after_run[OPENING_STEP_NAME] == 1
    original_again, fresh = items_of(package)
    assert original_again.pk == original.pk
    assert original_again.state == ItemState.RESOLVED.value
    assert fresh.state == ItemState.OPEN.value
    assert fresh.finding_key != original.finding_key
    assert f"{LISTED_SINCE_FACT}={THIRD_SWEEP.isoformat()}" in fresh.finding_facts
    assert LISTED_SINCE_FACT not in original.finding_facts
    assert health_of(package).inventory_absent_since is None


@pytest.mark.django_db
def test_a_package_that_was_never_absent_keeps_its_key_across_runs() -> None:
    """Nothing already resolved re-opens: a never-absent package's key carries no epoch and stays stable."""
    package = a_package("constant")
    listed(package, FIRST_SWEEP)
    a_run(cutoff=FIRST_SWEEP)
    (item,) = items_of(package)
    apply_transition(
        item_id=item.pk,
        expected_state=ItemState.OPEN.value,
        to_state=ItemState.TRIAGED.value,
        actor=a_user(LEADERSHIP),
        clock=FixedClock(instant=RUN_CLOCK),
    )

    listed(package, SECOND_SWEEP)
    second = a_run(cutoff=SECOND_SWEEP)

    assert second.after_run[OPENING_STEP_NAME] == 0
    (same,) = items_of(package)
    assert same.pk == item.pk
    assert same.state == ItemState.TRIAGED.value
    assert LISTED_SINCE_FACT not in same.finding_facts


@pytest.mark.django_db
def test_a_claimed_item_is_closed_and_unclaimed() -> None:
    """An `in_progress` item is somebody's; the product resolves it and releases the claim.

    `CLAIMED_ONLY_IN_PROGRESS` would refuse a resolved row that still named a
    claimant, and the audit row is what tells the claimant why the work went away.
    """
    package = a_package("claimed", confidence=IdentityConfidence.VERIFIED)
    listed(package, FIRST_SWEEP)
    an_advisory(package)
    a_run(cutoff=FIRST_SWEEP)
    (item,) = items_of(package)
    engineer = a_user(PACKAGING_ENGINEER)
    apply_transition(
        item_id=item.pk,
        expected_state=ItemState.OPEN.value,
        to_state=ItemState.TRIAGED.value,
        actor=engineer,
        clock=FixedClock(instant=RUN_CLOCK),
    )
    apply_transition(
        item_id=item.pk,
        expected_state=ItemState.TRIAGED.value,
        to_state=ItemState.IN_PROGRESS.value,
        actor=engineer,
        clock=FixedClock(instant=RUN_CLOCK),
    )

    not_listed(package, SECOND_SWEEP)
    a_run(cutoff=SECOND_SWEEP)

    item.refresh_from_db()
    assert item.state == ItemState.RESOLVED.value
    assert item.claimed_by is None
    closing = WorkflowTransition.objects.filter(item=item).order_by("-pk").first()
    assert closing is not None
    assert closing.from_state == ItemState.IN_PROGRESS.value
    assert closing.origin == SYSTEM_ORIGIN


@pytest.mark.django_db
def test_a_replay_selects_stamps_and_closes_the_same_and_closes_nothing_twice() -> None:
    """`CPM-FR-22`: absence is read at the run's cut-off everywhere, so a replay reproduces the queue.

    The second run at the same cut-off opens nothing and closes nothing -- the
    items are already resolved and `close_for_absence` skips a terminal item --
    and writes no second transition. A listing *after* the cut-off changes
    nothing either, because the replay cannot see it.
    """
    package = a_package("replayed")
    listed(package, FIRST_SWEEP)
    a_run(cutoff=FIRST_SWEEP)
    not_listed(package, SECOND_SWEEP)
    original = a_run(cutoff=SECOND_SWEEP)
    (item,) = items_of(package)
    listed(package, THIRD_SWEEP)

    replay = a_run(cutoff=SECOND_SWEEP, at=RUN_CLOCK + OBSERVATION_GAP)

    assert replay.after_run[ABSENCE_STEP_NAME] == original.after_run[ABSENCE_STEP_NAME] == 1
    assert replay.after_run[OPENING_STEP_NAME] == 0
    assert select_unresolved(cutoff=SECOND_SWEEP).left_out_for_absence == 1
    assert WorkflowTransition.objects.filter(item=item).count() == 1
    assert items_of(package) == [item]
    row = health_of(package)
    assert row.inventory_absent_since == SECOND_SWEEP
    assert row.policy_run_id == replay.policy_run.pk


# ---------------------------------------------------------------------------
# The service, on its own terms.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_the_system_table_resolves_from_every_non_terminal_state_and_nothing_else() -> None:
    """`workflow/states.py`'s second table: one move, from every state a person could still act from."""
    sources = {transition.from_state for transition in SYSTEM_TRANSITIONS}

    assert sources == set(ItemState.values) - TERMINAL_STATES
    assert {transition.to_state for transition in SYSTEM_TRANSITIONS} == {ItemState.RESOLVED.value}
    assert all(transition.requires_justification for transition in SYSTEM_TRANSITIONS)
    assert all(A_JUSTIFICATION_FRAGMENT in transition.describes for transition in SYSTEM_TRANSITIONS)


@pytest.mark.django_db
def test_closing_a_finished_item_is_not_a_refusal() -> None:
    """Idempotent on replay: a terminal item comes back as it was, with no second audit row."""
    package = a_package("finished", confidence=IdentityConfidence.VERIFIED)
    an_advisory(package)
    listed(package, FIRST_SWEEP)
    a_run(cutoff=FIRST_SWEEP)
    (item,) = items_of(package)
    clock = FixedClock(instant=RUN_CLOCK)
    first = close_for_absence(item_id=item.pk, absence=an_absence(), clock=clock)

    again = close_for_absence(item_id=item.pk, absence=an_absence(), clock=clock)

    assert first.written is True
    assert again.written is False
    assert again.item.state == ItemState.RESOLVED.value
    assert WorkflowTransition.objects.filter(item=item).count() == 1


@pytest.mark.django_db
def test_the_product_cannot_close_over_a_package_the_inventory_still_lists() -> None:
    """A reading that is not an absence is refused: closing listed work is a person's decision."""
    package = a_package("listed", confidence=IdentityConfidence.VERIFIED)
    an_advisory(package)
    listed(package, FIRST_SWEEP)
    a_run(cutoff=FIRST_SWEEP)
    (item,) = items_of(package)
    still_listed = Absence(absent=False, since=None, last_listed=FIRST_SWEEP, listed_since=None)

    with pytest.raises(WorkflowError, match=r"does not record an absence"):
        close_for_absence(item_id=item.pk, absence=still_listed, clock=FixedClock(instant=RUN_CLOCK))

    item.refresh_from_db()
    assert item.state == ItemState.OPEN.value


@pytest.mark.django_db
def test_closing_an_item_that_does_not_exist_is_refused() -> None:
    """Rather than raising whatever the ORM raises."""
    with pytest.raises(WorkflowError, match=r"no workflow item has id"):
        close_for_absence(item_id=999_999, absence=an_absence(), clock=FixedClock(instant=RUN_CLOCK))


@pytest.mark.django_db
def test_a_never_listed_package_closes_with_a_justification_that_names_no_listing() -> None:
    """The reason still names the absence when there is no last-listed date to name beside it."""
    package = a_package("never-listed", confidence=IdentityConfidence.VERIFIED)
    an_advisory(package)
    a_run(cutoff=FIRST_SWEEP)
    (item,) = items_of(package)

    close_for_absence(
        item_id=item.pk,
        absence=an_absence(last_listed=None),
        clock=FixedClock(instant=RUN_CLOCK),
    )

    (move,) = WorkflowTransition.objects.filter(item=item)
    assert A_JUSTIFICATION_FRAGMENT in move.justification
    assert "last listed" not in move.justification


@pytest.mark.django_db
def test_a_person_still_cannot_move_an_item_without_being_one() -> None:
    """`apply_transition` stays a person's path: it takes an actor, and the product never calls it."""
    package = a_package("human-only", confidence=IdentityConfidence.VERIFIED)
    an_advisory(package)
    a_run(cutoff=FIRST_SWEEP)
    (item,) = items_of(package)

    with pytest.raises(TypeError):
        apply_transition(  # type: ignore[call-arg]
            item_id=item.pk,
            expected_state=ItemState.OPEN.value,
            to_state=ItemState.TRIAGED.value,
            clock=FixedClock(instant=RUN_CLOCK),
        )


@pytest.mark.django_db
def test_the_database_refuses_a_transition_that_names_no_author_or_two() -> None:
    """`EXACTLY_ONE_AUTHOR`: the nullability of `actor` never becomes "nobody", and never "both"."""
    package = a_package("authored", confidence=IdentityConfidence.VERIFIED)
    an_advisory(package)
    a_run(cutoff=FIRST_SWEEP)
    (item,) = items_of(package)
    fields = {
        "item": item,
        "from_state": ItemState.OPEN.value,
        "to_state": ItemState.RESOLVED.value,
        "queue": item.queue,
        "occurred_at": RUN_CLOCK,
    }

    for author in ({"actor": None, "origin": ""}, {"actor": a_user(LEADERSHIP), "origin": SYSTEM_ORIGIN}):
        with pytest.raises(IntegrityError, match=EXACTLY_ONE_AUTHOR), transaction.atomic():
            WorkflowTransition.objects.create(**fields, **author)


@pytest.mark.django_db
def test_a_system_transition_describes_itself_by_its_origin() -> None:
    """An audit row read on its own says who -- the product, by name -- without an actor to join to."""
    package = a_package("described", confidence=IdentityConfidence.VERIFIED)
    an_advisory(package)
    a_run(cutoff=FIRST_SWEEP)
    (item,) = items_of(package)
    close_for_absence(item_id=item.pk, absence=an_absence(), clock=FixedClock(instant=RUN_CLOCK))

    (move,) = WorkflowTransition.objects.filter(item=item)

    assert str(move).endswith(f"by {SYSTEM_ORIGIN}")


# ---------------------------------------------------------------------------
# The surfaces label, the report excludes and says so, the API carries the pair.
# ---------------------------------------------------------------------------


def the_tag(since: datetime, last_listed: datetime) -> str:
    """Return the tag every surface prints for an absent package.

    Args:
        since: When the inventory stopped listing it.
        last_listed: When it last did.

    Returns:
        The sentence.

    """
    return f"absent from the inventory since {since.date().isoformat()} (last listed {last_listed.date().isoformat()})"


@pytest.mark.django_db
def test_the_package_page_and_the_health_list_carry_the_tag_and_lose_it_on_re_listing() -> None:
    """`CPM-FR-38`: labelled wherever it appears, from the rollup columns, and gone when the inventory relists it."""
    package = a_package("tagged", confidence=IdentityConfidence.VERIFIED)
    listed(package, FIRST_SWEEP)
    not_listed(package, SECOND_SWEEP)
    a_run(cutoff=SECOND_SWEEP)
    reader = a_reader()
    detail = reverse("conda_sentinel:package-detail", kwargs={"canonical_name": package.canonical_name})
    tag = the_tag(SECOND_SWEEP, FIRST_SWEEP)

    page = reader.get(detail)
    listing = reader.get(reverse("conda_sentinel:package-health"))
    assert page.status_code == HTTPStatus.OK
    assert tag in page.content.decode()
    assert tag in listing.content.decode()

    listed(package, THIRD_SWEEP)
    a_run(cutoff=THIRD_SWEEP)

    assert "absent from the inventory" not in reader.get(detail).content.decode()
    assert "absent from the inventory" not in reader.get(reverse("conda_sentinel:package-health")).content.decode()


@pytest.mark.django_db
def test_the_package_page_names_the_product_as_the_author_of_the_close() -> None:
    """`CPM-APP-S05` AC 4 still holds for the product's move: who acted, when, and why, on the page."""
    package = a_package("closed-on-page", confidence=IdentityConfidence.VERIFIED)
    listed(package, FIRST_SWEEP)
    an_advisory(package)
    a_run(cutoff=FIRST_SWEEP)
    not_listed(package, SECOND_SWEEP)
    a_run(cutoff=SECOND_SWEEP)

    page = a_reader().get(reverse("conda_sentinel:package-detail", kwargs={"canonical_name": package.canonical_name}))
    history = " ".join(page.content.decode().split())

    assert f"open &rarr; resolved &middot; {SYSTEM_ORIGIN} &middot;" in history
    assert A_JUSTIFICATION_FRAGMENT in history


@pytest.mark.django_db
def test_the_identity_queue_states_how_many_absent_packages_are_not_offered() -> None:
    """The queue page says what the selection left out, so an emptier queue does not read as less work.

    A `verified` package stamped absent beside the unmapped one does not count:
    it was never the identity queue's to offer.
    """
    offered = a_package("offered")
    listed(offered, FIRST_SWEEP)
    gone = a_package("gone")
    listed(gone, FIRST_SWEEP)
    not_listed(gone, SECOND_SWEEP)
    resolved_and_gone = a_package("resolved-and-gone", confidence=IdentityConfidence.VERIFIED)
    listed(resolved_and_gone, FIRST_SWEEP)
    not_listed(resolved_and_gone, SECOND_SWEEP)
    a_run(cutoff=SECOND_SWEEP)

    page = a_reader().get(reverse("conda_sentinel:queue", kwargs={"queue": Queue.IDENTITY_REVIEW.value}))
    remediation = a_reader().get(reverse("conda_sentinel:queue", kwargs={"queue": Queue.REMEDIATION.value}))

    assert page.status_code == HTTPStatus.OK
    assert "1 package absent from the inventory is not offered, across all packages" in page.content.decode()
    assert "absent from the inventory" not in remediation.content.decode()


@pytest.mark.django_db
def test_the_feedstock_gap_report_excludes_absent_packages_and_states_the_count() -> None:
    """The one surface allowed to exclude, and it says so with the count and the reason.

    Both packages match the report -- neither has a feedstock -- and the absent one
    is left out of the rows while the header says it was. The stale-evidence
    report, one slug over, still carries it and labels it, because nothing but the
    feedstock-gap report excludes anything.
    """
    kept = a_package("kept", confidence=IdentityConfidence.VERIFIED)
    listed(kept, FIRST_SWEEP)
    gone = a_package("gone", confidence=IdentityConfidence.VERIFIED)
    listed(gone, FIRST_SWEEP)
    not_listed(gone, SECOND_SWEEP)
    a_run(cutoff=SECOND_SWEEP)
    reader = a_reader()

    report = reader.get(reverse("conda_sentinel:report", kwargs={"slug": "feedstock-lag"})).content.decode()
    stale = reader.get(reverse("conda_sentinel:report", kwargs={"slug": "stale-evidence"})).content.decode()

    assert f"1 package excluded: {escape(ABSENT_FROM_THE_INVENTORY.reason)}" in report
    assert ">kept<" in report
    assert ">gone<" not in report
    assert "gone" in stale
    assert the_tag(SECOND_SWEEP, FIRST_SWEEP) in stale
    assert "excluded" not in stale


@pytest.mark.django_db
def test_the_feedstock_gap_export_and_api_read_the_same_rows_as_the_page() -> None:
    """`CPM-AD-24`: the exclusion is in the one projection, so the export and the API agree with the screen."""
    kept = a_package("kept", confidence=IdentityConfidence.VERIFIED)
    listed(kept, FIRST_SWEEP)
    gone = a_package("gone", confidence=IdentityConfidence.VERIFIED)
    listed(gone, FIRST_SWEEP)
    not_listed(gone, SECOND_SWEEP)
    a_run(cutoff=SECOND_SWEEP)
    reader = a_reader()

    export = reader.get(reverse("conda_sentinel:report-export", kwargs={"slug": "feedstock-lag"})).content.decode()
    api = reader.get(reverse("conda_sentinel_api:report", kwargs={"slug": "feedstock-lag"})).json()

    assert "kept" in export
    assert "gone" not in export
    assert [row[0] for row in api["results"]] == ["kept"]


@pytest.mark.django_db
def test_the_api_carries_both_instants_on_the_health_row_and_the_detail() -> None:
    """The same two columns the screen's tag is built from, `null` for a listed package and set for an absent one."""
    gone = a_package("gone", confidence=IdentityConfidence.VERIFIED)
    listed(gone, FIRST_SWEEP)
    not_listed(gone, SECOND_SWEEP)
    here = a_package("here", confidence=IdentityConfidence.VERIFIED)
    listed(here, FIRST_SWEEP)
    a_run(cutoff=SECOND_SWEEP)
    reader = a_reader()

    rows = {
        row["canonical_name"]: row for row in reader.get(reverse("conda_sentinel_api:package-health")).json()["results"]
    }
    detail = reader.get(reverse("conda_sentinel_api:package-detail", kwargs={"canonical_name": "gone"})).json()

    assert rows["gone"]["inventory_absent_since"] == SECOND_SWEEP.isoformat().replace("+00:00", "Z")
    assert rows["gone"]["inventory_last_listed"] == FIRST_SWEEP.isoformat().replace("+00:00", "Z")
    assert rows["here"]["inventory_absent_since"] is None
    assert rows["here"]["inventory_last_listed"] is None
    assert detail["inventory_absent_since"] == SECOND_SWEEP.isoformat().replace("+00:00", "Z")
    assert detail["inventory_last_listed"] == FIRST_SWEEP.isoformat().replace("+00:00", "Z")


@pytest.mark.django_db
def test_a_queue_row_on_an_absent_package_carries_the_tag() -> None:
    """An open item on an absent package is unusual but possible, and the row says so rather than hiding it.

    Arranged by stamping the row without the closing step having run over the
    item: the after-run seam is what closes, and a run that failed between the
    two steps would leave exactly this.
    """
    package = a_package("open-and-absent", confidence=IdentityConfidence.VERIFIED)
    listed(package, FIRST_SWEEP)
    an_advisory(package)
    a_run(cutoff=FIRST_SWEEP)
    (item,) = items_of(package)
    PackageHealth.objects.filter(package=package).update(
        inventory_absent_since=SECOND_SWEEP,
        inventory_last_listed=FIRST_SWEEP,
    )

    page = a_reader().get(reverse("conda_sentinel:queue", kwargs={"queue": Queue.REMEDIATION.value}))

    assert item.state == ItemState.OPEN.value
    assert the_tag(SECOND_SWEEP, FIRST_SWEEP) in page.content.decode()


# ---------------------------------------------------------------------------
# Review patches: the closer's reach, one open identity item, re-listing every queue.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_a_package_whose_compose_failed_this_run_still_has_its_items_closed() -> None:
    """The closer reads every package with open work, not only the rows the run wrote.

    A confidence the vocabulary does not declare makes the rollup write for that
    package fail (`core/rollup.py` contains the failure per package), so it has
    no rollup row this run -- and it still left the inventory, and its item is
    still closed.
    """
    package = a_package("uncomposed", confidence=IdentityConfidence.VERIFIED)
    listed(package, FIRST_SWEEP)
    an_advisory(package)
    a_run(cutoff=FIRST_SWEEP)
    (item,) = items_of(package)
    Package.objects.filter(pk=package.pk).update(confidence=AN_UNRECOGNISED_CONFIDENCE)
    not_listed(package, SECOND_SWEEP)

    second = a_run(cutoff=SECOND_SWEEP)

    assert second.policy_run.pk != health_of(package).policy_run_id, "the compose failed for this package"
    assert second.after_run[OPENING_STEP_NAME] == 1
    item.refresh_from_db()
    assert item.state == ItemState.RESOLVED.value
    assert WorkflowTransition.objects.get(item=item).origin == SYSTEM_ORIGIN


@pytest.mark.django_db
def test_an_eroded_history_cannot_open_a_second_identity_item_beside_the_epoch_keyed_one() -> None:
    """At most one open identity item per package, whatever its keys.

    The purge can remove a `not_found` row, at which point the fold forgets the
    epoch and the key reverts to the bare pair. Seeded without the row: an open
    item under the epoch key, a history that is all `ok`, one run -- and still
    one open item.
    """
    package = a_package("eroded")
    listed(package, FIRST_SWEEP)
    listed(package, SECOND_SWEEP)
    key, readable = finding_key_of(
        IDENTITY_FINDING_TABLE,
        package.pk,
        [("confidence", IdentityConfidence.UNMAPPED.value), (LISTED_SINCE_FACT, SECOND_SWEEP.isoformat())],
    )
    open_keyed_item(
        finding_key=key,
        finding_facts=readable,
        package=package,
        queue=Queue.IDENTITY_REVIEW.value,
        clock=FixedClock(instant=RUN_CLOCK),
    )

    summary = a_run(cutoff=SECOND_SWEEP)

    assert summary.after_run[OPENING_STEP_NAME] == 0
    (only,) = items_of(package)
    assert only.finding_key == key
    assert only.state == ItemState.OPEN.value


@pytest.mark.django_db
def test_a_re_listed_package_opens_fresh_remediation_and_compliance_items_for_the_same_findings() -> None:
    """An evidence-backed key is the advisory or the licence -- the same row before and after.

    Without the listing epoch on the key, `open_item`'s `get_or_create` would find
    the item the product closed and open nothing, and a package that came back
    with the same advisory would have no remediation work. Proved end to end:
    closed for absence, re-listed, fresh items under epoch keys, the closed ones
    untouched.
    """
    package = a_package("returning-with-work", confidence=IdentityConfidence.VERIFIED)
    listed(package, FIRST_SWEEP)
    an_advisory(package)
    a_licence(package)
    a_run(cutoff=FIRST_SWEEP)
    originals = items_of(package)
    assert {item.queue for item in originals} == {Queue.REMEDIATION.value, Queue.COMPLIANCE_REVIEW.value}

    not_listed(package, SECOND_SWEEP)
    a_run(cutoff=SECOND_SWEEP)
    listed(package, THIRD_SWEEP)
    third = a_run(cutoff=THIRD_SWEEP)

    assert third.after_run[OPENING_STEP_NAME] == TWO_ITEMS
    items = items_of(package)
    closed = [item for item in items if item.state == ItemState.RESOLVED.value]
    fresh = [item for item in items if item.state == ItemState.OPEN.value]
    assert {item.pk for item in closed} == {item.pk for item in originals}
    assert {item.queue for item in fresh} == {Queue.REMEDIATION.value, Queue.COMPLIANCE_REVIEW.value}
    assert all(f"{LISTED_SINCE_FACT}={THIRD_SWEEP.isoformat()}" in item.finding_facts for item in fresh)
    assert all(LISTED_SINCE_FACT not in item.finding_facts for item in closed)


@pytest.mark.django_db
@pytest.mark.parametrize("queue", [Queue.REMEDIATION.value, Queue.COMPLIANCE_REVIEW.value])
def test_a_fresh_finding_on_an_absent_package_opens_nothing(queue: str) -> None:
    """The skip, observed: a matched advisory or a licence to review on a package already gone opens no item.

    Args:
        queue: Which queue the finding would have opened work in.

    """
    package = a_package("gone-before-its-finding", confidence=IdentityConfidence.VERIFIED)
    listed(package, FIRST_SWEEP)
    not_listed(package, SECOND_SWEEP)
    if queue == Queue.REMEDIATION.value:
        an_advisory(package)
    else:
        a_licence(package)

    summary = a_run(cutoff=SECOND_SWEEP)

    assert items_of(package) == []
    assert summary.after_run[OPENING_STEP_NAME] == 0


@pytest.mark.django_db
def test_only_an_unmapped_package_opens_identity_work() -> None:
    """The rule moved from SQL into Python and keeps its meaning: `inventory-derived` is an identity."""
    derived = a_package("weakly-identified", confidence=IdentityConfidence.INVENTORY_DERIVED)
    listed(derived, FIRST_SWEEP)
    unmapped = a_package("unidentified")
    listed(unmapped, FIRST_SWEEP)

    summary = a_run(cutoff=FIRST_SWEEP)

    assert summary.after_run[OPENING_STEP_NAME] == 1
    assert items_of(derived) == []
    (item,) = items_of(unmapped)
    assert item.queue == Queue.IDENTITY_REVIEW.value


@pytest.mark.django_db
def test_the_closer_counts_only_what_it_wrote() -> None:
    """An item a person resolved before the run is theirs, and the run's count says so."""
    package = a_package("half-done", confidence=IdentityConfidence.VERIFIED)
    listed(package, FIRST_SWEEP)
    an_advisory(package)
    a_licence(package)
    a_run(cutoff=FIRST_SWEEP)
    remediation, compliance = sorted(items_of(package), key=lambda item: item.queue == Queue.REMEDIATION.value)
    engineer = a_user(PACKAGING_ENGINEER)
    for expected, to_state in (
        (ItemState.OPEN.value, ItemState.TRIAGED.value),
        (ItemState.TRIAGED.value, ItemState.IN_PROGRESS.value),
        (ItemState.IN_PROGRESS.value, ItemState.RESOLVED.value),
    ):
        apply_transition(
            item_id=remediation.pk,
            expected_state=expected,
            to_state=to_state,
            actor=engineer,
            clock=FixedClock(instant=RUN_CLOCK),
        )

    not_listed(package, SECOND_SWEEP)
    second = a_run(cutoff=SECOND_SWEEP)

    assert second.after_run[OPENING_STEP_NAME] == 1
    compliance.refresh_from_db()
    assert compliance.state == ItemState.RESOLVED.value
    assert WorkflowTransition.objects.filter(item=remediation, origin=SYSTEM_ORIGIN).count() == 0


@pytest.mark.django_db
def test_an_item_in_a_state_the_machine_does_not_declare_is_refused_rather_than_read_as_finished() -> None:
    """A row outside `ItemState` is one the product cannot reason about, and it says so by name."""
    package = a_package("odd-state", confidence=IdentityConfidence.VERIFIED)
    an_advisory(package)
    a_run(cutoff=FIRST_SWEEP)
    (item,) = items_of(package)
    WorkflowItem.objects.filter(pk=item.pk).update(state="parked")

    with pytest.raises(WorkflowError, match=r"'parked'"):
        close_for_absence(item_id=item.pk, absence=an_absence(), clock=FixedClock(instant=RUN_CLOCK))

    assert WorkflowTransition.objects.filter(item=item).count() == 0


@pytest.mark.django_db
def test_ten_packages_that_left_in_one_sweep_are_stamped_by_one_statement() -> None:
    """The stamp is grouped by `(since, last_listed)`: a sweep of leavers is one `UPDATE`, never one per package."""
    leavers = [
        a_package(f"leaver-{index}", confidence=IdentityConfidence.VERIFIED) for index in range(A_SWEEP_OF_LEAVERS)
    ]
    for package in leavers:
        listed(package, FIRST_SWEEP)
        not_listed(package, SECOND_SWEEP)
    stayer = a_package("stayer", confidence=IdentityConfidence.VERIFIED)
    listed(stayer, FIRST_SWEEP)

    with CaptureQueriesContext(connection) as captured:
        summary = a_run(cutoff=SECOND_SWEEP)

    updates = [query["sql"] for query in captured if query["sql"].startswith('UPDATE "package_health"')]
    stamps = [sql for sql in updates if "inventory_absent_since" in sql]
    assert summary.after_run[ABSENCE_STEP_NAME] == A_SWEEP_OF_LEAVERS
    assert len(stamps) == 1, stamps
    assert all(health_of(package).inventory_absent_since == SECOND_SWEEP for package in leavers)
    assert health_of(stayer).inventory_absent_since is None


@pytest.mark.django_db
def test_a_run_folds_the_inventory_twice_and_no_more() -> None:
    """One fold for the stamping step, one for the opening step -- shared by the selection and the closer.

    Three unresolved packages and a verified one with an advisory, so the identity
    opener, the remediation opener and the closer all have work; the snapshot
    table is still read exactly twice. `histories_at` asked about no package
    reads nothing at all.
    """
    for name in ("one", "two", "three"):
        listed(a_package(name), FIRST_SWEEP)
    verified = a_package("verified-with-work", confidence=IdentityConfidence.VERIFIED)
    listed(verified, FIRST_SWEEP)
    not_listed(verified, SECOND_SWEEP)
    an_advisory(verified)

    with CaptureQueriesContext(connection) as captured:
        a_run(cutoff=SECOND_SWEEP)
    with CaptureQueriesContext(connection) as nothing_asked:
        empty = absence_at(cutoff=SECOND_SWEEP, package_ids=[])

    # The fold's shape and nothing else's: the passes read one package at a time
    # through `snapshot_as_of`, newest first; the fold streams every row ascending.
    folds = [
        query["sql"]
        for query in captured
        if 'FROM "inventory_snapshots"' in query["sql"] and query["sql"].endswith("ORDER BY 2 ASC, 3 ASC")
    ]
    assert len(folds) == TWO_ITEMS, folds
    assert empty == {}
    assert len(nothing_asked) == 0


@pytest.mark.django_db
def test_the_three_absence_counts_agree_for_one_run() -> None:
    """What the selection left out, what the rollup stamped at an unresolved confidence, and what the opener skipped.

    Three readings of one fact, taken three ways, so a drift between them --
    the queue line reading the rollup's stale confidence, say -- fails here.
    """
    unmapped_gone = a_package("unmapped-gone")
    listed(unmapped_gone, FIRST_SWEEP)
    not_listed(unmapped_gone, SECOND_SWEEP)
    derived_gone = a_package("derived-gone", confidence=IdentityConfidence.INVENTORY_DERIVED)
    listed(derived_gone, FIRST_SWEEP)
    not_listed(derived_gone, SECOND_SWEEP)
    verified_gone = a_package("verified-gone", confidence=IdentityConfidence.VERIFIED)
    listed(verified_gone, FIRST_SWEEP)
    not_listed(verified_gone, SECOND_SWEEP)
    offered = a_package("offered")
    listed(offered, FIRST_SWEEP)

    summary = a_run(cutoff=SECOND_SWEEP)

    left_out = select_unresolved(cutoff=SECOND_SWEEP).left_out_for_absence
    stamped_unresolved = (
        PackageHealth.objects.filter(inventory_absent_since__isnull=False)
        .exclude(package__confidence=IdentityConfidence.VERIFIED)
        .count()
    )
    skipped_by_the_opener = (
        Package.objects.exclude(confidence=IdentityConfidence.VERIFIED)
        .exclude(
            pk__in=WorkflowItem.objects.filter(queue=Queue.IDENTITY_REVIEW.value).values("package_id"),
        )
        .exclude(pk=derived_gone.pk)
        .count()
    )
    line = a_reader().get(reverse("conda_sentinel:queue", kwargs={"queue": Queue.IDENTITY_REVIEW.value}))

    assert left_out == stamped_unresolved == TWO_ITEMS
    assert skipped_by_the_opener == 1, "the unmapped absent package is the one the opener skipped"
    assert summary.after_run[OPENING_STEP_NAME] == 1
    assert "2 packages absent from the inventory are not offered, across all packages" in line.content.decode()


@pytest.mark.django_db
def test_a_raising_absence_step_fails_the_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """The docs say a failing step fails the run; the absence step is a step like the other."""

    def refuse(**_kwargs: object) -> dict[int, Absence]:
        message = "the inventory could not be read"
        raise RuntimeError(message)

    monkeypatch.setattr(absence_module, "absence_at", refuse)
    a_package("any")

    with pytest.raises(AfterRunStepError, match=ABSENCE_STEP_NAME):
        a_run(cutoff=FIRST_SWEEP)


@pytest.mark.django_db
def test_compare_runs_is_unaffected_by_the_stamped_columns() -> None:
    """A replay comparison reads the derived tables only; the rollup's absence columns are not in what it diffs.

    The row is stamped by hand between the original and the replay, so the two
    runs' rollup rows differ in exactly those columns -- and the comparison, which
    never reads the rollup, reports nothing.
    """
    package = a_package("compared", confidence=IdentityConfidence.VERIFIED)
    listed(package, FIRST_SWEEP)
    original = a_run(cutoff=FIRST_SWEEP)
    PackageHealth.objects.filter(package=package).update(
        inventory_absent_since=SECOND_SWEEP,
        inventory_last_listed=FIRST_SWEEP,
    )
    replayed = a_run(cutoff=FIRST_SWEEP, at=RUN_CLOCK + OBSERVATION_GAP)

    report = compare_runs(original.policy_run, replayed.policy_run)

    assert health_of(package).inventory_absent_since is None, "the replay's compose replaced the row"
    assert report.differences == ()


# ---------------------------------------------------------------------------
# Review patches: the surfaces that say what they left out.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_the_feedstock_gap_facet_excludes_absent_packages_and_states_both_exclusions() -> None:
    """The health list filtered to `feedstock=absent` is the other feedstock-gap surface.

    Three packages with no feedstock: a `verified` one the inventory lists, a
    `verified` one it no longer lists, and an `unmapped` one the gate reports
    `unknown` for. The facet lists the first, excludes the second and says so,
    and says why the third was never there.
    """
    kept = a_package("kept", confidence=IdentityConfidence.VERIFIED)
    listed(kept, FIRST_SWEEP)
    gone = a_package("gone", confidence=IdentityConfidence.VERIFIED)
    listed(gone, FIRST_SWEEP)
    not_listed(gone, SECOND_SWEEP)
    unmapped = a_package("unmapped")
    listed(unmapped, FIRST_SWEEP)
    for package in (kept, gone, unmapped):
        an_absent_feedstock(package)
    a_run(cutoff=SECOND_SWEEP)
    reader = a_reader()

    facet = reader.get(f"{reverse('conda_sentinel:package-health')}?feedstock=absent").content.decode()
    whole = reader.get(reverse("conda_sentinel:package-health")).content.decode()
    api = reader.get(f"{reverse('conda_sentinel_api:package-health')}?feedstock=absent").json()

    assert ">kept<" in facet
    assert ">gone<" not in facet
    assert ">unmapped<" not in facet
    assert f"1 package excluded: {escape(str(ABSENT_FROM_THE_INVENTORY.reason))}" in facet
    assert "1 unmapped package reports unknown rather than absent and is not listed" in facet
    assert ">gone<" in whole
    assert "excluded" not in whole
    assert [row["canonical_name"] for row in api["results"]] == ["kept"]


@pytest.mark.django_db
def test_the_report_api_and_the_csv_carry_the_exclusion() -> None:
    """The rows an integrator reads are the rows the exclusion was applied to, so the body says so."""
    kept = a_package("kept", confidence=IdentityConfidence.VERIFIED)
    listed(kept, FIRST_SWEEP)
    gone = a_package("gone", confidence=IdentityConfidence.VERIFIED)
    listed(gone, FIRST_SWEEP)
    not_listed(gone, SECOND_SWEEP)
    a_run(cutoff=SECOND_SWEEP)
    reader = a_reader()
    reason = str(ABSENT_FROM_THE_INVENTORY.reason)

    page = reader.get(reverse("conda_sentinel_api:report", kwargs={"slug": "feedstock-lag"})).json()
    roster = {
        entry["slug"]: entry for entry in reader.get(reverse("conda_sentinel_api:report-roster")).json()["results"]
    }
    _content, _rows, provenance = export_csv(REPORTS_BY_SLUG["feedstock-lag"])
    _content, _rows, kev_provenance = export_csv(REPORTS_BY_SLUG["kev"])

    assert page["excluded"] == {"count": 1, "reason": reason}
    assert roster["feedstock-lag"]["excluded"] == {"count": 1, "reason": reason}
    assert roster["kev"]["excluded"] is None
    assert provenance.endswith(f"; excluded=1 package excluded: {reason}")
    assert "excluded" not in kev_provenance


@pytest.mark.django_db
def test_a_searched_report_counts_the_exclusions_the_search_would_have_shown() -> None:
    """The count is about the report the reader is looking at: narrowed by the same search as the rows."""
    kept = a_package("kept", confidence=IdentityConfidence.VERIFIED)
    listed(kept, FIRST_SWEEP)
    for name in ("alpha-gone", "beta-gone"):
        gone = a_package(name, confidence=IdentityConfidence.VERIFIED)
        listed(gone, FIRST_SWEEP)
        not_listed(gone, SECOND_SWEEP)
    a_run(cutoff=SECOND_SWEEP)
    reader = a_reader()
    url = reverse("conda_sentinel:report", kwargs={"slug": "feedstock-lag"})

    unsearched = reader.get(url).content.decode()
    searched = reader.get(f"{url}?q=alpha").content.decode()

    assert "2 packages excluded" in unsearched
    assert "1 package excluded" in searched


@pytest.mark.django_db
def test_a_system_close_serialises_with_its_origin_and_no_actor() -> None:
    """`WorkflowTransitionSerializer` says who: `actor` null, `origin` the product."""
    package = a_package("serialised", confidence=IdentityConfidence.VERIFIED)
    an_advisory(package)
    a_run(cutoff=FIRST_SWEEP)
    (item,) = items_of(package)
    close_for_absence(item_id=item.pk, absence=an_absence(), clock=FixedClock(instant=RUN_CLOCK))
    move = WorkflowTransition.objects.get(item=item)

    data = WorkflowTransitionSerializer(move).data

    assert data["actor"] is None
    assert data["origin"] == SYSTEM_ORIGIN
    assert data["to_state"] == ItemState.RESOLVED.value
    assert A_JUSTIFICATION_FRAGMENT in data["justification"]


@pytest.mark.django_db
def test_a_never_listed_absent_package_renders_on_every_surface_without_a_last_listed_clause() -> None:
    """`absence_tag(since, None)` end to end: a `not_found` alone, stamped and shown, is a tag with no parenthesis."""
    package = a_package("never-listed-shown", confidence=IdentityConfidence.VERIFIED)
    not_listed(package, FIRST_SWEEP)
    an_advisory(package)
    a_run(cutoff=FIRST_SWEEP)
    # An open item on the package, so a queue has a row to tag: the run opened
    # none (the package was already absent), and a run that failed between the
    # opening and the closing step is what leaves one.
    key, readable = finding_key_of(IDENTITY_FINDING_TABLE, package.pk, [("confidence", "verified")])
    open_keyed_item(
        finding_key=key,
        finding_facts=readable,
        package=package,
        queue=Queue.REMEDIATION.value,
        clock=FixedClock(instant=RUN_CLOCK),
    )
    reader = a_reader()
    tag = f"absent from the inventory since {FIRST_SWEEP.date().isoformat()}"

    pages = [
        reader.get(reverse("conda_sentinel:package-health")),
        reader.get(reverse("conda_sentinel:package-detail", kwargs={"canonical_name": package.canonical_name})),
        reader.get(reverse("conda_sentinel:queue", kwargs={"queue": Queue.REMEDIATION.value})),
        reader.get(reverse("conda_sentinel:report", kwargs={"slug": "stale-evidence"})),
    ]

    assert health_of(package).inventory_absent_since == FIRST_SWEEP
    assert health_of(package).inventory_last_listed is None
    for page in pages:
        html = page.content.decode()
        assert page.status_code == HTTPStatus.OK
        assert tag in html
        assert f"{tag} (" not in html
