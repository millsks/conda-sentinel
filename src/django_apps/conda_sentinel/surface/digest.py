"""What the Digests page reads, and nothing it may not (`CPM-OPERATE-S09`).

One read and a projection. `newest_and_history` is one query slice -- the
newest `operator_digests` row and the thirty before it -- split into the two,
so a row inserted mid-request can neither appear twice nor out of place;
`collector_rows` / `overall_rows` turn the newest row's `figures` into what a
template prints, one line per collector, one per overall figure, so the
template never reaches into a JSON column by key.

**Read-only, and imports nothing that composes or delivers.** The view reaches
this module and the model, never `collectors/digest.py`: that module imports
`core/delivery.py`, which `tests/unit/django_apps/test_request_boundary_audit.py`
forbids a request to reach, and the composer reads every collector's evidence
table. The figures' keys are `core/digest_keys.py`'s, imported by both sides,
and the count-per-state words are `core/runs.py`'s; nothing is spelled twice.

**The page orders collectors by the registry, never by the row.** PostgreSQL's
`jsonb` does not keep key order, so a row read back carries its collectors in
whatever order the store chose; the projection walks `registered_collectors()`
and appends any name the row carries that the registry no longer does. A
malformed row -- a figure that is not a mapping -- renders `NOT_COUNTED` rather
than a 500.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from typing import Final

from conda_sentinel.collectors.models import OperatorDigest
from conda_sentinel.core.digest_keys import ABSENT_KEY
from conda_sentinel.core.digest_keys import COLLECTIONS_KEY
from conda_sentinel.core.digest_keys import COLLECTORS_KEY
from conda_sentinel.core.digest_keys import DISPATCHES_KEY
from conda_sentinel.core.digest_keys import FINISHED_AT_KEY
from conda_sentinel.core.digest_keys import INGESTED_KEY
from conda_sentinel.core.digest_keys import INVENTORY_KEY
from conda_sentinel.core.digest_keys import NEVER_OBSERVED_KEY
from conda_sentinel.core.digest_keys import OVERALL_KEY
from conda_sentinel.core.digest_keys import PACKAGES_KEY
from conda_sentinel.core.digest_keys import PAST_TARGET_KEY
from conda_sentinel.core.digest_keys import POLICY_RUN_KEY
from conda_sentinel.core.digest_keys import PRUNE_RUNS_KEY
from conda_sentinel.core.digest_keys import RATE_LIMITED_KEY
from conda_sentinel.core.digest_keys import RESOLVED_KEY
from conda_sentinel.core.digest_keys import STATUS_KEY
from conda_sentinel.core.digest_keys import UNRESOLVED_KEY
from conda_sentinel.core.digest_keys import VERSION_KEY
from conda_sentinel.core.registry import registered_collectors
from conda_sentinel.core.runs import states_as_words
from conda_sentinel.surface.labels import display_label

__all__ = [
    "DIGEST_HISTORY_LIMIT",
    "NOT_COUNTED",
    "CollectorRow",
    "OverallRow",
    "collector_rows",
    "newest_and_history",
    "overall_rows",
]

#: How many earlier digests the page lists beneath the newest: a month.
DIGEST_HISTORY_LIMIT: Final[int] = 30

#: What a figure reads as when the row holds none for it, or holds something
#: that is not a figure.
NOT_COUNTED: Final[str] = "—"


@dataclass(frozen=True, slots=True)
class CollectorRow:
    """One collector's line of the figures table.

    Attributes:
        name: The collector's label.
        dispatches: Dispatches by state as words, or `NOT_COUNTED` for a
            collector that is not swept per package.
        collections: Collections by state as words.
        rate_limited: Failed collections refused on the allowance, as text.
        past_freshness_target: Selected packages observed once but not inside
            the target, or `NOT_COUNTED` where the collector declares neither.
        never_observed: Selected packages with no evidence at all, likewise.

    """

    name: str
    dispatches: str
    collections: str
    rate_limited: str
    past_freshness_target: str
    never_observed: str


@dataclass(frozen=True, slots=True)
class OverallRow:
    """One line of the overall figures.

    Attributes:
        label: What the figure is.
        value: The figure, as words.

    """

    label: str
    value: str


def newest_and_history(*, limit: int = DIGEST_HISTORY_LIMIT) -> tuple[OperatorDigest | None, list[OperatorDigest]]:
    """Return the newest digest and the digests before it, from one read.

    Args:
        limit: How many earlier digests, at most.

    Returns:
        The newest row, or `None` when none has been composed, and the rows
        before it newest first. One slice, so the two halves come from the
        same instant of the table.

    """
    rows = list(OperatorDigest.objects.order_by("-observed_at", "-id")[: limit + 1])
    if not rows:
        return None, []
    return rows[0], rows[1:]


def collector_rows(digest: OperatorDigest) -> list[CollectorRow]:
    """Return one line per collector in the digest's figures, in registry order.

    Args:
        digest: The row.

    Returns:
        The lines: every registered collector the row carries, in the
        registry's order, then any name the row carries that the registry no
        longer does. A collector whose figure is not a mapping reads as
        `NOT_COUNTED` throughout.

    """
    per_collector = _mapping(_mapping(digest.figures).get(COLLECTORS_KEY))
    registered = [collector.name for collector in registered_collectors()]
    ordered = [name for name in registered if name in per_collector]
    ordered.extend(name for name in per_collector if name not in registered)
    return [_collector_row(name, per_collector.get(name)) for name in ordered]


def _collector_row(name: str, counted: object) -> CollectorRow:
    """Return one collector's line.

    Args:
        name: The collector name the row carries.
        counted: Its figure, or whatever the row held.

    Returns:
        The line.

    """
    if not isinstance(counted, Mapping):
        return CollectorRow(display_label(name), NOT_COUNTED, NOT_COUNTED, NOT_COUNTED, NOT_COUNTED, NOT_COUNTED)
    return CollectorRow(
        name=display_label(name),
        dispatches=_words(counted.get(DISPATCHES_KEY)) if DISPATCHES_KEY in counted else NOT_COUNTED,
        collections=_words(counted.get(COLLECTIONS_KEY)),
        rate_limited=_number(counted.get(RATE_LIMITED_KEY), absent="0"),
        past_freshness_target=_number(counted.get(PAST_TARGET_KEY)),
        never_observed=_number(counted.get(NEVER_OBSERVED_KEY)),
    )


def overall_rows(digest: OperatorDigest) -> list[OverallRow]:
    """Return the overall figures as lines.

    Args:
        digest: The row.

    Returns:
        Prune runs, the inventory, the packages by confidence and the newest
        finished policy run.

    """
    overall = _mapping(_mapping(digest.figures).get(OVERALL_KEY))
    inventory = _mapping(overall.get(INVENTORY_KEY))
    packages = _mapping(overall.get(PACKAGES_KEY))
    policy_run = overall.get(POLICY_RUN_KEY)
    if isinstance(policy_run, Mapping):
        policy = (
            f"version {policy_run.get(VERSION_KEY)}, finished {_stamp(policy_run.get(FINISHED_AT_KEY))}, "
            f"{display_label(policy_run.get(STATUS_KEY) or 'unknown')}"
        )
    else:
        policy = "no policy run has finished"
    return [
        OverallRow(label="Prune runs", value=_words(overall.get(PRUNE_RUNS_KEY))),
        OverallRow(
            label="Inventory",
            value=f"{_number(inventory.get(INGESTED_KEY))} ingested, {_number(inventory.get(ABSENT_KEY))} absent",
        ),
        OverallRow(
            label="Packages",
            value=(
                f"{_number(packages.get(RESOLVED_KEY))} resolved, {_number(packages.get(UNRESOLVED_KEY))} unresolved "
                f"({', '.join(_confidences(packages))})"
            ),
        ),
        OverallRow(label="Newest policy run", value=policy),
    ]


def _mapping(value: object) -> Mapping[str, Any]:
    """Return a stored figure as a mapping, or an empty one for anything else.

    Args:
        value: Whatever the row held.

    Returns:
        The mapping.

    """
    return value if isinstance(value, Mapping) else {}


def _words(counted: object) -> str:
    """Return a count per state as words, or `NOT_COUNTED` for anything that is not one.

    Args:
        counted: The stored count per state, or whatever the row held.

    Returns:
        The words.

    """
    return states_as_words(counted) if isinstance(counted, Mapping) else NOT_COUNTED


def _number(value: object, *, absent: str = NOT_COUNTED) -> str:
    """Return a stored count as text, or `absent` for anything that is not one.

    Args:
        value: The stored count, or whatever the row held.
        absent: What a missing or malformed count reads as.

    Returns:
        The text.

    """
    return str(value) if isinstance(value, int) and not isinstance(value, bool) else absent


def _confidences(packages: Mapping[str, Any]) -> list[str]:
    """Return the per-confidence counts as words.

    Args:
        packages: The package figures.

    Returns:
        One `count confidence` per confidence the figures carry, in their order.

    """
    return [
        f"{_number(count)} {display_label(confidence)}"
        for confidence, count in packages.items()
        if confidence not in {RESOLVED_KEY, UNRESOLVED_KEY}
    ]


def _stamp(finished_at: object) -> str:
    """Return an ISO instant as the page writes one, or the value's own text.

    Args:
        finished_at: What the figures stored.

    Returns:
        `2026-09-14 02:00Z` for an ISO string; otherwise `str(finished_at)`.

    """
    if not isinstance(finished_at, str):
        return str(finished_at)
    try:
        parsed = datetime.fromisoformat(finished_at)
    except ValueError:
        return finished_at
    return parsed.strftime("%Y-%m-%d %H:%MZ")
