"""`CPM-OPERATE-S03`: reading the inventory page's postings, measured against literal mappings.

`surface/inventory.py` turns a form's fields into what the service takes and
refuses only what it cannot read; every rule about the *values* is the
service's. So what is here is the reading: the three actions, blank as missing,
digits as the one spelling of a count, and the refusals for a fourth action and
a cell that is not digits.
"""

from __future__ import annotations

import pytest

from conda_sentinel.collectors.inventory import MAX_SIGNAL
from conda_sentinel.collectors.inventory import InventoryRow
from conda_sentinel.collectors.models import InventoryChange
from conda_sentinel.surface.inventory import ACTION_FIELD
from conda_sentinel.surface.inventory import ACTIONS
from conda_sentinel.surface.inventory import ACTIVE_ONLY_PARAM
from conda_sentinel.surface.inventory import ADD
from conda_sentinel.surface.inventory import CHANGE
from conda_sentinel.surface.inventory import MAX_COUNT_DIGITS
from conda_sentinel.surface.inventory import MISSING
from conda_sentinel.surface.inventory import PREFIX_PARAM
from conda_sentinel.surface.inventory import REASON_FIELD
from conda_sentinel.surface.inventory import RETIRE
from conda_sentinel.surface.inventory import SIGNAL_LABELS
from conda_sentinel.surface.inventory import InventoryFilters
from conda_sentinel.surface.inventory import InventoryFormError
from conda_sentinel.surface.inventory import applied_inventory_filters
from conda_sentinel.surface.inventory import changed_fields
from conda_sentinel.surface.inventory import read_posting

A_POSTING = {
    ACTION_FIELD: ADD,
    "source_package_key": " conda-forge/numpy ",
    "package_name": " numpy ",
    "internal_component_count": "312",
    "internal_lob_count": "9",
    "apps": "",
    "platforms": " 4 ",
    "downloads": "",
    "versions": "0",
    REASON_FIELD: "adopted",
}


def test_the_three_actions_are_the_vocabulary() -> None:
    """Add, change, retire; a fourth verb is refused below."""
    assert {ADD, CHANGE, RETIRE} == ACTIONS
    assert set(SIGNAL_LABELS) == {
        "internal_component_count",
        "internal_lob_count",
        "apps",
        "platforms",
        "downloads",
        "versions",
    }


@pytest.mark.parametrize("action", sorted({ADD, CHANGE}))
def test_an_add_or_a_change_reads_the_key_the_row_and_the_reason(action: str) -> None:
    """Blank is missing, `0` is zero, whitespace is stripped off text and digits alike.

    Args:
        action: Which of the two.

    """
    posting = read_posting({**A_POSTING, ACTION_FIELD: action})

    assert posting.action == action
    assert posting.source_package_key == "conda-forge/numpy"
    assert posting.reason == "adopted"
    assert posting.row == InventoryRow(
        package_name="numpy",
        internal_component_count=312,
        internal_lob_count=9,
        apps=None,
        platforms=4,
        downloads=None,
        versions=0,
    )


def test_a_retire_reads_the_key_and_the_reason_and_no_row() -> None:
    """Retire changes no value, so the row is `None` and the counts are not read at all."""
    posting = read_posting({ACTION_FIELD: RETIRE, "source_package_key": "conda-forge/numpy", REASON_FIELD: "gone"})

    assert posting.action == RETIRE
    assert posting.row is None
    assert posting.reason == "gone"


def test_a_missing_required_count_is_read_as_missing_and_left_for_the_service() -> None:
    """The form does not decide what is required; the service does, naming the key."""
    posting = read_posting({**A_POSTING, "internal_lob_count": ""})

    assert posting.row is not None
    assert posting.row.internal_lob_count is None


@pytest.mark.parametrize("action", ["delete", "", "ADD", "remove"])
def test_a_verb_the_page_does_not_offer_is_refused(action: str) -> None:
    """Exactly the three, spelled exactly.

    Args:
        action: The refused verb.

    """
    with pytest.raises(InventoryFormError, match=r"none of"):
        read_posting({**A_POSTING, ACTION_FIELD: action})


@pytest.mark.parametrize("cell", ["lots", "-1", "1.5", "1,000", "٣", "+2"])
def test_a_count_that_is_not_ascii_digits_is_refused_naming_the_field(cell: str) -> None:
    """On the watchlist parser's terms: `int()` accepts most of these, and a typed row is reviewed by reading it.

    Args:
        cell: The refused cell.

    """
    with pytest.raises(InventoryFormError, match=r"downloads"):
        read_posting({**A_POSTING, "downloads": cell})


def test_a_count_longer_than_the_ceilings_digits_is_refused_before_int_sees_it() -> None:
    """`int()` of thousands of digits raises `ValueError`; the form refuses first, as a form error."""
    assert len(str(MAX_SIGNAL)) == MAX_COUNT_DIGITS
    with pytest.raises(InventoryFormError, match=r"at most"):
        read_posting({**A_POSTING, "downloads": "9" * 5_000})
    # Exactly the ceiling's digit count is still read; the service applies the value bound.
    posting = read_posting({**A_POSTING, "downloads": "9" * MAX_COUNT_DIGITS})
    assert posting.row is not None
    assert posting.row.downloads == int("9" * MAX_COUNT_DIGITS)


# ---------------------------------------------------------------------------
# The two filters.
# ---------------------------------------------------------------------------


def test_the_filters_are_read_off_the_query_and_absent_means_everything() -> None:
    """`?active=1` narrows to active rows, `?prefix=` to a key prefix, and `page` is ignored."""
    assert applied_inventory_filters({}) == InventoryFilters(active_only=False, prefix="")
    assert applied_inventory_filters({"page": "2"}) == InventoryFilters(active_only=False, prefix="")
    assert applied_inventory_filters({ACTIVE_ONLY_PARAM: "1", PREFIX_PARAM: " internal/ "}) == InventoryFilters(
        active_only=True, prefix="internal/"
    )
    assert applied_inventory_filters({ACTIVE_ONLY_PARAM: "  "}).active_only is False


def test_the_filters_condition_is_empty_when_nothing_is_selected() -> None:
    """No filter, no narrowing: the condition an unfiltered page applies is the empty `Q`."""
    assert not InventoryFilters(active_only=False, prefix="").condition()
    assert InventoryFilters(active_only=True, prefix="").condition()
    assert InventoryFilters(active_only=False, prefix="conda-forge/").condition()


# ---------------------------------------------------------------------------
# The audit row's diff.
# ---------------------------------------------------------------------------


def _a_change(**values: object) -> InventoryChange:
    """Return an unsaved audit row with every pair equal unless overridden.

    Args:
        **values: Columns to set.

    Returns:
        The instance. Unsaved: `changed_fields` reads attributes and touches no
        database.

    """
    change = InventoryChange(prior_package_name="numpy", new_package_name="numpy", reason="x")
    for name, value in values.items():
        setattr(change, name, value)
    return change


def test_changed_fields_lists_only_what_differs_prior_to_new() -> None:
    """A count change reads as a change; equal pairs are not listed."""
    change = _a_change(prior_internal_component_count=12, new_internal_component_count=40)

    differences = changed_fields(change)

    assert [(d.label, d.prior, d.new) for d in differences] == [
        (str(SIGNAL_LABELS["internal_component_count"]), "12", "40")
    ]


def test_a_null_count_renders_as_the_missing_mark_and_never_as_zero_or_none() -> None:
    """NULL is missing: an em dash on the page, never `0` (a count) and never `None` (a Python word)."""
    change = _a_change(prior_apps=None, new_apps=7, prior_downloads=0, new_downloads=None)

    rendered = {d.label: (d.prior, d.new) for d in changed_fields(change)}

    assert rendered[str(SIGNAL_LABELS["apps"])] == (MISSING, "7")
    assert rendered[str(SIGNAL_LABELS["downloads"])] == ("0", MISSING)
    assert "None" not in {value for pair in rendered.values() for value in pair}


def test_an_add_lists_every_field_it_set_and_a_retirement_lists_the_transition() -> None:
    """Blank priors on an add; the `retired` flag on a retirement."""
    added = _a_change(prior_package_name="", new_package_name="numpy", new_internal_lob_count=3)
    retired = _a_change(prior_retired=False, new_retired=True)

    assert [d.label for d in changed_fields(added)] == ["Package name", str(SIGNAL_LABELS["internal_lob_count"])]
    assert changed_fields(added)[0].prior == MISSING
    assert [(d.label, d.prior, d.new) for d in changed_fields(retired)] == [("Retired", "no", "yes")]
