"""`CPM-OPERATE-S03`'s surface: the inventory page, its three forms and its refusals.

One page, leadership only, three forms posting to one view that delegates to
`collectors/inventory.py`. What is asserted here is the *contract at the
boundary*: who may reach it, what each form writes when it lands, and what each
refusal answers with -- 403 for a role the page does not accept, 400 with the
service's message for a change the service refused, 404 for a key naming nothing,
303 back to the page for a change that landed. The service's own rules are proven
in `test_inventory_service.py`; nothing here re-proves them.

Every test rolls back: `@pytest.mark.django_db` wraps each in a transaction.
"""

from __future__ import annotations

from http import HTTPStatus
from typing import TYPE_CHECKING
from typing import Final

import pytest
from django.conf import settings
from django.contrib.auth.models import Group
from django.contrib.auth.models import Permission
from django.test import Client
from django.urls import reverse

from conda_sentinel.collectors.inventory import InventoryRow
from conda_sentinel.collectors.inventory import add_entry
from conda_sentinel.collectors.models import InventoryChange
from conda_sentinel.collectors.models import InventoryEntry
from conda_sentinel.core.clock import FixedClock
from conda_sentinel.core.roles import INVENTORY_APP_LABEL
from conda_sentinel.core.roles import INVENTORY_CHANGE_CODENAME
from conda_sentinel.core.roles import LEADERSHIP
from conda_sentinel.core.roles import PACKAGING_ENGINEER
from conda_sentinel.core.roles import SECURITY_REVIEWER
from conda_sentinel.surface.inventory import ACTION_FIELD
from conda_sentinel.surface.inventory import ACTIVE_ONLY_PARAM
from conda_sentinel.surface.inventory import ADD
from conda_sentinel.surface.inventory import CHANGE
from conda_sentinel.surface.inventory import MAX_COUNT_DIGITS
from conda_sentinel.surface.inventory import MISSING
from conda_sentinel.surface.inventory import PREFIX_PARAM
from conda_sentinel.surface.inventory import REASON_FIELD
from conda_sentinel.surface.inventory import RETIRE
from conda_sentinel.surface.tone import INVENTORY_RETIRED
from conda_sentinel.surface.tone import tone_of
from tests.clocks import FIXED_INSTANT
from tests.factories import UserFactory

if TYPE_CHECKING:
    from django_service.users.models import User

pytestmark = pytest.mark.integration

A_KEY: Final[str] = "conda-forge/numpy"
A_NAME: Final[str] = "numpy"
A_REASON: Final[str] = "the platform team adopted it"

#: A well-formed add, as the form posts it. Optional counts blank: missing.
AN_ADD: Final[dict[str, str]] = {
    ACTION_FIELD: ADD,
    "source_package_key": A_KEY,
    "package_name": A_NAME,
    "internal_component_count": "12",
    "internal_lob_count": "3",
    "apps": "",
    "platforms": "",
    "downloads": "",
    "versions": "",
    REASON_FIELD: A_REASON,
}


def _in_role(role: str, *, username: str) -> User:
    """Return a user in one role group.

    Args:
        role: The role slot.
        username: The account name.

    Returns:
        The user, a member of the group the contract names for the slot.

    """
    user: User = UserFactory.create(username=username)
    user.groups.add(Group.objects.get(name=getattr(settings.ROLE_CONTRACT, role)))
    return user


@pytest.fixture
def leadership() -> Client:
    """Return a client signed in as a leadership persona.

    Returns:
        The client.

    """
    client = Client()
    client.force_login(_in_role(LEADERSHIP, username="a-leader"))
    return client


@pytest.fixture
def packaging() -> Client:
    """Return a client signed in as a packaging engineer.

    Returns:
        The client.

    """
    client = Client()
    client.force_login(_in_role(PACKAGING_ENGINEER, username="a-packager"))
    return client


@pytest.fixture
def an_entry(db: None) -> InventoryEntry:
    """Return one active entry, added through the service by a leader.

    Args:
        db: pytest-django's per-test transaction.

    Returns:
        The entry.

    """
    leader = _in_role(LEADERSHIP, username="the-seeder")
    return add_entry(
        source_package_key=A_KEY,
        row=InventoryRow(package_name=A_NAME, internal_component_count=12, internal_lob_count=3),
        actor=leader,
        reason=A_REASON,
        clock=FixedClock(instant=FIXED_INSTANT),
    ).entry


def _page() -> str:
    """Return the page's URL.

    Returns:
        The reversed route.

    """
    return reverse("conda_sentinel:inventory")


# ---------------------------------------------------------------------------
# Who may reach it.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_a_leadership_persona_reads_the_page(leadership: Client) -> None:
    """200, and the page names the table it is about."""
    response = leadership.get(_page())

    assert response.status_code == HTTPStatus.OK
    assert b"Inventory" in response.content


@pytest.mark.django_db
def test_a_packaging_persona_is_refused(packaging: Client) -> None:
    """403: the inventory is governed reference data, and the read is scoped with the write."""
    assert packaging.get(_page()).status_code == HTTPStatus.FORBIDDEN
    assert packaging.post(_page(), AN_ADD).status_code == HTTPStatus.FORBIDDEN
    assert InventoryEntry.objects.count() == 0


@pytest.mark.django_db
def test_a_security_reviewer_is_refused() -> None:
    """403 for the third role too: leadership alone."""
    client = Client()
    client.force_login(_in_role(SECURITY_REVIEWER, username="a-reviewer"))

    assert client.get(_page()).status_code == HTTPStatus.FORBIDDEN


@pytest.mark.django_db
def test_an_anonymous_visitor_is_sent_to_sign_in() -> None:
    """A redirect, not a bare 403: the mixin's contract for a visitor nobody identified."""
    response = Client().get(_page())

    assert response.status_code == HTTPStatus.FOUND
    assert "login" in response["Location"]


# ---------------------------------------------------------------------------
# The three forms, landing.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_adding_a_row_writes_the_entry_and_its_audit_row_and_redirects(leadership: Client) -> None:
    """The acceptance criterion's first half: a row from the surface, with an audit row naming the actor."""
    response = leadership.post(_page(), AN_ADD)

    assert response.status_code == HTTPStatus.SEE_OTHER
    assert response["Location"] == _page()
    entry = InventoryEntry.objects.get(source_package_key=A_KEY)
    assert entry.package_name == A_NAME
    assert entry.internal_component_count == 12  # noqa: PLR2004 - the posted value
    assert entry.apps is None
    assert entry.reason == A_REASON
    change = InventoryChange.objects.get(entry=entry)
    assert change.actor is not None
    assert change.actor.username == "a-leader"
    assert change.origin == ""
    assert change.prior_package_name == ""
    # Followed, the redirect lands on the page saying what happened.
    landed = leadership.get(_page())
    assert landed.status_code == HTTPStatus.OK
    assert A_KEY.encode() in landed.content
    assert b"Added" in landed.content


@pytest.mark.django_db
def test_changing_a_row_rewrites_it_and_records_the_prior_values(leadership: Client, an_entry: InventoryEntry) -> None:
    """The edit form, prefilled from `?key=`, posts `change` and the audit row carries both sides."""
    prefilled = leadership.get(f"{_page()}?key={A_KEY}")
    assert prefilled.status_code == HTTPStatus.OK
    assert b'value="12"' in prefilled.content

    response = leadership.post(
        _page(),
        {**AN_ADD, ACTION_FIELD: CHANGE, "internal_component_count": "40", "apps": "7", REASON_FIELD: "recounted"},
    )

    assert response.status_code == HTTPStatus.SEE_OTHER
    an_entry.refresh_from_db()
    assert an_entry.internal_component_count == 40  # noqa: PLR2004 - the posted value
    assert an_entry.apps == 7  # noqa: PLR2004 - the posted value
    newest = InventoryChange.objects.filter(entry=an_entry).order_by("-pk").first()
    assert newest is not None
    assert (newest.prior_internal_component_count, newest.new_internal_component_count) == (12, 40)
    assert (newest.prior_apps, newest.new_apps) == (None, 7)
    assert newest.reason == "recounted"


@pytest.mark.django_db
def test_retiring_a_row_sets_the_column_and_the_page_draws_the_chip(
    leadership: Client, an_entry: InventoryEntry
) -> None:
    """Retire is a column write; the page shows the row still there, toned as retired."""
    response = leadership.post(
        _page(),
        {ACTION_FIELD: RETIRE, "source_package_key": A_KEY, REASON_FIELD: "no longer used"},
    )

    assert response.status_code == HTTPStatus.SEE_OTHER
    an_entry.refresh_from_db()
    assert an_entry.retired_at is not None
    assert InventoryEntry.objects.count() == 1
    page = leadership.get(_page())
    assert tone_of(INVENTORY_RETIRED).encode() in page.content
    assert b"Retired" in page.content


# ---------------------------------------------------------------------------
# The refusals, as status codes.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_a_blank_reason_is_a_400_carrying_the_services_words(leadership: Client) -> None:
    """The service refused; the page says so in the service's words and writes nothing."""
    response = leadership.post(_page(), {**AN_ADD, REASON_FIELD: "   "})

    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert b"needs a reason" in response.content
    assert InventoryEntry.objects.count() == 0
    assert InventoryChange.objects.count() == 0


@pytest.mark.django_db
def test_a_count_that_is_not_digits_is_a_400_and_the_posting_is_kept_on_the_form(leadership: Client) -> None:
    """The form refused before the service was asked, and the person does not retype the row."""
    response = leadership.post(_page(), {**AN_ADD, "downloads": "lots"})

    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert b"not a count" in response.content
    assert f'value="{A_KEY}"'.encode() in response.content
    assert InventoryEntry.objects.count() == 0


@pytest.mark.django_db
def test_a_duplicate_key_is_a_400(leadership: Client, an_entry: InventoryEntry) -> None:
    """Adding a key the table holds is refused, and the table is unchanged."""
    response = leadership.post(_page(), AN_ADD)

    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert b"cannot be added again" in response.content
    assert InventoryEntry.objects.count() == 1
    assert InventoryChange.objects.count() == 1


@pytest.mark.django_db
def test_a_key_naming_no_row_is_a_404(leadership: Client) -> None:
    """A stale form posts `change` or `retire` for a row that is not there."""
    response = leadership.post(
        _page(),
        {ACTION_FIELD: RETIRE, "source_package_key": "conda-forge/nothing", REASON_FIELD: "gone"},
    )

    assert response.status_code == HTTPStatus.NOT_FOUND


@pytest.mark.django_db
def test_an_action_the_page_does_not_offer_is_a_400(leadership: Client) -> None:
    """A hand-made request posting a fourth verb is refused rather than guessed at."""
    response = leadership.post(_page(), {**AN_ADD, ACTION_FIELD: "delete"})

    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert InventoryEntry.objects.count() == 0


@pytest.mark.django_db
def test_a_stale_edit_link_prefills_nothing_rather_than_failing(leadership: Client) -> None:
    """`?key=` naming no row renders the page with an empty form."""
    response = leadership.get(f"{_page()}?key=conda-forge/nothing")

    assert response.status_code == HTTPStatus.OK
    assert b"Add a package" in response.content


# ---------------------------------------------------------------------------
# Review patches: the re-render keeps its form, the second gate, the digit guard,
# the diff panel and the filters.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_a_refused_change_re_renders_as_the_change_form_for_that_row(
    leadership: Client, an_entry: InventoryEntry
) -> None:
    """The form's action carries no `?key=`, so the re-render reads the key off the posting.

    Re-rendered as an add form, the retry would post `add` and be refused as
    "already holds a row".
    """
    response = leadership.post(
        _page(),
        {**AN_ADD, ACTION_FIELD: CHANGE, "internal_component_count": "40", REASON_FIELD: ""},
    )

    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert f'value="{CHANGE}"'.encode() in response.content
    assert b"Save change" in response.content
    assert b'value="40"' in response.content
    assert f'name="{ACTION_FIELD}" value="{ADD}"'.encode() not in response.content


@pytest.mark.django_db
def test_a_refused_retire_does_not_prefill_the_add_form(leadership: Client, an_entry: InventoryEntry) -> None:
    """A retire form is the row's own; its refusal leaves the add form empty."""
    response = leadership.post(_page(), {ACTION_FIELD: RETIRE, "source_package_key": A_KEY, REASON_FIELD: ""})

    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert b"Add a package" in response.content
    assert b'id="inv-key" name="source_package_key" value=""' in response.content


@pytest.mark.django_db
def test_a_leadership_member_whose_group_lacks_the_permission_is_refused_403_not_400() -> None:
    """The service's own gate, answered as the provisioning fault it is."""
    group = Group.objects.get(name=settings.ROLE_CONTRACT.leadership)
    permission = Permission.objects.get(content_type__app_label=INVENTORY_APP_LABEL, codename=INVENTORY_CHANGE_CODENAME)
    group.permissions.remove(permission)
    client = Client()
    client.force_login(_in_role(LEADERSHIP, username="an-ungranted-leader"))

    response = client.post(_page(), AN_ADD)

    assert response.status_code == HTTPStatus.FORBIDDEN
    assert InventoryEntry.objects.count() == 0
    assert InventoryChange.objects.count() == 0


@pytest.mark.django_db
def test_a_count_of_thousands_of_digits_is_a_400_not_a_fault(leadership: Client) -> None:
    """`int()` refuses a 4,300-digit string; the form refuses first, and the inputs say the bound."""
    response = leadership.post(_page(), {**AN_ADD, "downloads": "9" * 4_300})

    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert b"at most" in response.content
    assert f'maxlength="{MAX_COUNT_DIGITS}"'.encode() in response.content
    assert InventoryEntry.objects.count() == 0


@pytest.mark.django_db
def test_the_recent_changes_panel_lists_the_fields_that_differ_and_a_null_count_as_the_missing_mark(
    leadership: Client, an_entry: InventoryEntry
) -> None:
    """A count change reads as a change; NULL reads as an em dash, never `0` and never `None`."""
    leadership.post(_page(), {**AN_ADD, ACTION_FIELD: CHANGE, "apps": "7", REASON_FIELD: "recounted"})

    page = leadership.get(_page()).content.decode()
    panel = page[page.index("Recent changes") :]

    assert f"Apps: {MISSING} &rarr; 7" in panel
    assert "None" not in panel
    assert "Apps: 0 &rarr; 7" not in panel
    # The add before it lists what it set, with blank priors as the mark.
    assert f"Internal components: {MISSING} &rarr; 12" in panel


@pytest.mark.django_db
def test_the_active_only_toggle_and_the_prefix_filter_narrow_the_table(
    leadership: Client, an_entry: InventoryEntry
) -> None:
    """`?active=1` hides retired rows; `?prefix=` keeps keys beginning so; both are bookmarkable."""
    leadership.post(_page(), {**AN_ADD, "source_package_key": "internal/tooling", "package_name": "tooling"})
    leadership.post(_page(), {ACTION_FIELD: RETIRE, "source_package_key": A_KEY, REASON_FIELD: "dropped"})

    everything = leadership.get(_page()).content
    active = leadership.get(f"{_page()}?{ACTIVE_ONLY_PARAM}=1").content
    prefixed = leadership.get(f"{_page()}?{PREFIX_PARAM}=internal/").content

    assert A_KEY.encode() in everything
    assert b"internal/tooling" in everything
    assert b"internal/tooling" in active
    assert A_KEY.encode() not in active[active.index(b"<tbody>") : active.index(b"Recent changes")]
    assert b"internal/tooling" in prefixed
    assert A_KEY.encode() not in prefixed[prefixed.index(b"<tbody>") : prefixed.index(b"Recent changes")]
