"""What the inventory page's three forms post, read into what the service takes.

`CPM-OPERATE-S03`'s surface is one page and three forms -- add, change, retire --
each posting to one view that delegates to `collectors/inventory.py`. This module
is the reading half: it turns a `QueryDict` into the action, the key, the row
and the reason, and refuses what it cannot read. The *deciding* half -- the
permission, the reason's presence, the bounds -- is the service's, on purpose:
a form that re-implemented those would be a second place the rules lived.

**A count is read as the CSV reads one.** Blank is missing and becomes `None`;
otherwise the cell is ASCII digits and nothing else, on `collectors/watchlist.py`'s
terms: `int()` accepts a sign, whitespace and a non-ASCII digit, and a row a
person typed is reviewed by reading it. A cell longer than the ceiling's own
digit count is refused *before* `int()` sees it -- Python refuses to convert a
string of thousands of digits, and that refusal would be a fault rather than a
form error. The service applies the ceiling itself.

**The page's two filters and the audit row's diff live here too.** An active-only
toggle and a key-prefix filter, on the minimal terms `surface/filters.py` sets
for the health table (read off the query, ignored when absent, a condition the
view applies), and `changed_fields`, which turns an audit row's prior/new pairs
into the fields that differ so a count change reads as a change.

Pure but for the model: no request, no settings. `tests/unit/django_apps/test_inventory_form.py`
measures every refusal against a literal mapping.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from typing import Final

from django.db.models import Q
from django.utils.translation import gettext_lazy as _

from conda_sentinel.collectors.inventory import MAX_SIGNAL
from conda_sentinel.collectors.inventory import InventoryRow
from conda_sentinel.collectors.models import INVENTORY_KEY_FIELD
from conda_sentinel.collectors.models import INVENTORY_NAME_FIELD
from conda_sentinel.collectors.models import INVENTORY_OPTIONAL_SIGNALS
from conda_sentinel.collectors.models import INVENTORY_REQUIRED_SIGNALS
from conda_sentinel.collectors.models import INVENTORY_SIGNALS

if TYPE_CHECKING:
    from collections.abc import Mapping

    from django.utils.functional import _StrPromise

    from conda_sentinel.collectors.models import InventoryChange

__all__ = [
    "ACTIONS",
    "ACTION_FIELD",
    "ACTIVE_ONLY_PARAM",
    "ADD",
    "CHANGE",
    "MAX_COUNT_DIGITS",
    "PREFIX_PARAM",
    "REASON_FIELD",
    "RETIRE",
    "SIGNAL_LABELS",
    "FieldChange",
    "InventoryFilters",
    "InventoryFormError",
    "InventoryPosting",
    "applied_inventory_filters",
    "changed_fields",
    "read_posting",
]

#: The two query parameters the page filters on: `?active=1` narrows to active
#: rows, `?prefix=conda-forge/` to keys beginning so.
ACTIVE_ONLY_PARAM: Final[str] = "active"
PREFIX_PARAM: Final[str] = "prefix"

#: How many digits a posted count may carry: the ceiling's own. Refused before
#: `int()` rather than after, for the reason the module docstring gives.
MAX_COUNT_DIGITS: Final[int] = len(str(MAX_SIGNAL))

#: The field naming which form posted, and its three values.
ACTION_FIELD: Final[str] = "action"
ADD: Final[str] = "add"
CHANGE: Final[str] = "change"
RETIRE: Final[str] = "retire"
ACTIONS: Final[frozenset[str]] = frozenset({ADD, CHANGE, RETIRE})

#: The field carrying why.
REASON_FIELD: Final[str] = "reason"

#: What each signal is called on the page. Sentence case, as `display_label`
#: would derive it, spelled here so the template's headings and the form's
#: labels are one table.
SIGNAL_LABELS: Final[dict[str, _StrPromise]] = {
    "internal_component_count": _("Internal components"),
    "internal_lob_count": _("Internal lines of business"),
    "apps": _("Apps"),
    "platforms": _("Platforms"),
    "downloads": _("Downloads"),
    "versions": _("Versions"),
}


class InventoryFormError(ValueError):
    """A posting could not be read as an action on the inventory.

    The page answers it 400 with the message; a `ValueError` on the terms every
    other "this input cannot describe what it claims to" in this product is.
    """


@dataclass(frozen=True, slots=True)
class InventoryPosting:
    """One form's posting, read.

    Attributes:
        action: `add`, `change` or `retire`.
        source_package_key: The row's key, as posted and stripped.
        row: What the row states, for `add` and `change`; `None` for `retire`,
            which changes no value.
        reason: Why, as posted. The service refuses a blank one.

    """

    action: str
    source_package_key: str
    row: InventoryRow | None
    reason: str


def read_posting(posted: Mapping[str, str]) -> InventoryPosting:
    """Read one form's posting, or refuse it.

    Args:
        posted: The form's fields -- `request.POST`, or any mapping of the same
            shape.

    Returns:
        The posting.

    Raises:
        InventoryFormError: When the action is not one of the three, or a count
            is present and is not ASCII digits.

    """
    action = posted.get(ACTION_FIELD, "").strip()
    if action not in ACTIONS:
        message = (
            f"the inventory page was posted an action of {action!r}, which is none of {sorted(ACTIONS)}. The "
            f"three forms on the page post one each; a hand-made request posts nothing this view will act on."
        )
        raise InventoryFormError(message)
    key = posted.get(INVENTORY_KEY_FIELD, "").strip()
    reason = posted.get(REASON_FIELD, "")
    if action == RETIRE:
        return InventoryPosting(action=action, source_package_key=key, row=None, reason=reason)
    counts = {signal: _count(posted.get(signal, ""), field=signal) for signal in INVENTORY_SIGNALS}
    row = InventoryRow(
        package_name=posted.get(INVENTORY_NAME_FIELD, "").strip(),
        internal_component_count=counts[INVENTORY_REQUIRED_SIGNALS[0]],
        internal_lob_count=counts[INVENTORY_REQUIRED_SIGNALS[1]],
        apps=counts[INVENTORY_OPTIONAL_SIGNALS[0]],
        platforms=counts[INVENTORY_OPTIONAL_SIGNALS[1]],
        downloads=counts[INVENTORY_OPTIONAL_SIGNALS[2]],
        versions=counts[INVENTORY_OPTIONAL_SIGNALS[3]],
    )
    return InventoryPosting(action=action, source_package_key=key, row=row, reason=reason)


def _count(cell: str, *, field: str) -> int | None:
    """Read one count cell as the watchlist parser reads one.

    Args:
        cell: What was posted, possibly blank.
        field: Which signal, for the message.

    Returns:
        The count, or `None` for a blank cell -- missing, never zero.

    Raises:
        InventoryFormError: When the cell is not ASCII digits.

    """
    value = cell.strip()
    if not value:
        return None
    if len(value) > MAX_COUNT_DIGITS:
        message = (
            f"{field} was posted as {len(value)} characters, and a usage signal has at most {MAX_COUNT_DIGITS} "
            f"digits ({MAX_SIGNAL})."
        )
        raise InventoryFormError(message)
    if not (value.isascii() and value.isdigit()):
        message = (
            f"{field} was posted as {value!r}, which is not a count. A usage signal is a whole number written in "
            f"digits; leave it blank to record that the source did not say, which is stored as missing and stays "
            f"distinguishable from zero."
        )
        raise InventoryFormError(message)
    return int(value)


@dataclass(frozen=True, slots=True)
class InventoryFilters:
    """What the page's two filters selected.

    Attributes:
        active_only: Whether retired rows are hidden.
        prefix: The key prefix rows must start with, or blank for any.

    """

    active_only: bool
    prefix: str

    def condition(self) -> Q:
        """Return the one condition narrowing the table by both filters.

        Returns:
            The conjunction, empty when nothing is selected.

        """
        condition = Q()
        if self.active_only:
            condition &= Q(retired_at__isnull=True)
        if self.prefix:
            condition &= Q(source_package_key__startswith=self.prefix)
        return condition


def applied_inventory_filters(query: Mapping[str, str]) -> InventoryFilters:
    """Return which filters a request selected.

    Args:
        query: The request's query parameters -- `request.GET`, or any mapping
            of the same shape. Parameters that are not filters are ignored
            rather than refused, as the health table's `applied_filters` ignores
            them: `page` is legitimately there.

    Returns:
        The filters. Any non-blank `active` value narrows to active rows; the
        prefix is stripped and a blank one selects nothing.

    """
    return InventoryFilters(
        active_only=bool(query.get(ACTIVE_ONLY_PARAM, "").strip()),
        prefix=query.get(PREFIX_PARAM, "").strip(),
    )


@dataclass(frozen=True, slots=True)
class FieldChange:
    """One field an audit row records as different.

    Attributes:
        label: What the field is called on the page.
        prior: Its value before, as page text -- an em dash for NULL, never `0`.
        new: Its value after, on the same terms.

    """

    label: str
    prior: str
    new: str


#: How a NULL count reads on the page: missing, and visibly so. Never blank --
#: blank is the form's spelling -- and never `0`, which is a count.
MISSING: Final[str] = "\u2014"


def changed_fields(change: InventoryChange) -> list[FieldChange]:
    """Return the fields an audit row records as different, prior to new.

    The name, the six signals and the retired flag, in that order, and only the
    ones whose two sides differ -- so an add lists every field it set (its priors
    are blank), a count change lists the count, and a retirement lists `retired`.

    Args:
        change: The audit row.

    Returns:
        The differences, in field order.

    """
    differences: list[FieldChange] = []
    if change.prior_package_name != change.new_package_name:
        differences.append(
            FieldChange(
                label=str(_("Package name")),
                prior=change.prior_package_name or MISSING,
                new=change.new_package_name,
            ),
        )
    for signal in INVENTORY_SIGNALS:
        prior = getattr(change, f"prior_{signal}")
        new = getattr(change, f"new_{signal}")
        if prior != new:
            differences.append(FieldChange(label=str(SIGNAL_LABELS[signal]), prior=_text(prior), new=_text(new)))
    if change.prior_retired != change.new_retired:
        differences.append(
            FieldChange(
                label=str(_("Retired")),
                prior=str(_("yes")) if change.prior_retired else str(_("no")),
                new=str(_("yes")) if change.new_retired else str(_("no")),
            ),
        )
    return differences


def _text(count: int | None) -> str:
    """Return a count as page text: the number, or the missing mark for NULL.

    Args:
        count: The stored value.

    Returns:
        Its text.

    """
    return MISSING if count is None else str(count)
