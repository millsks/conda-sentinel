"""The operator digest: composed from the ledger once a day, stored, and delivered (`CPM-OPERATE-S09`).

A sweep that fails for three days is found on the Coverage screen when a
reviewer asks; nothing tells the operator, and silence and health look the
same. This module is what breaks the silence. `cpm.policy.digest`
(`collectors/tasks.py`, fired daily by `CELERY_BEAT_SCHEDULE`'s `cpm-digest`
entry) calls `compose_digest`, which reads the twenty-four hours of run-ledger
rows ending now, the inventory and the package table, renders one plain-text
digest, delivers it to whatever `CPM_DIGEST_WEBHOOK_URL` and `CPM_DIGEST_EMAIL`
declare, and stores the whole of that as one `OperatorDigest` row the Digests
page reads.

**A day in which nothing changed still delivers, in one line.** `changed` is
whether the figures differ from the previous digest's; when they do not, the
text is one sentence naming the most recent digest that *did* change and
carrying the standing problems -- the collectors still failing, still refused
on their allowance, still past their freshness target -- and it is stored and
delivered exactly as a full digest is. A sweep failing for three days is the
story's own case, and a "nothing changed" that hid it would be a silence again.

**The two freshness figures are measured from `now`.** Every other figure is a
count over the window or over a table as it stands, and the policy run is
recorded by its ending rather than its age; but "past the freshness target" and
"never observed" are asked against the instant the digest is composed, so a
package that crosses its target on an otherwise quiet day makes that day
`changed`. That is the intended reading: a collector quietly falling behind is
a change worth a full digest.

**Why this application.** The digest reads the ledger (`core`), the inventory
(`collectors`) and the package table (`identity`); `core` may import neither
downstream application, and `collectors` already holds the `CPM-OPERATE-S08`
service that reads all three (`CPM-AD-4`).

**Composition is pure; only `compose_digest` reads a table.** The figures are
computed by `collector_figures` and its siblings from plain rows, the text by
`render_text` from the figures, and `changed` by `has_changed` from two
dictionaries -- so `tests/unit/django_apps/test_digest.py` exercises every row
of the story's matrix with a stopped clock and no database, and the integration
module proves the one function that reads and writes.

**Rate-limited is counted by a marker, never by prose.** A failed collection
whose `detail` carries `core/collection.py`'s `ALLOWANCE_REFUSAL_MARKER` was
refused without a call -- the allowance spent, or the credential refused earlier
in the window -- and the marker is declared once and written by the same
function that writes both sentences, so an edit to either cannot silently zero
this count.

**A webhook URL may carry a credential, and nothing here repeats it.** Rows,
log lines, details and exceptions name `host_of(url)` and nothing more; the
`DeclaredDelivery` that carries the URL excludes it from its own `repr`. The
address an e-mail goes to is the row's `target`, as the story fixes.

**Delivered, then stored.** `OperatorDigest` is append-only, so the outcome of a
delivery cannot be written back onto a row stored first. Every failure -- a
refused webhook, an SMTP refusal, a deliverer that raised -- becomes a `failed`
entry in the row's `deliveries` and is logged; nothing is raised into the beat,
because a digest that could not be delivered is exactly the digest the page has
to show.
"""

from __future__ import annotations

from collections import Counter
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from datetime import timedelta
from typing import TYPE_CHECKING
from typing import Any
from typing import Final
from urllib.parse import urlsplit

import structlog
from django.conf import settings
from django.core.mail import send_mail
from django.db.models import Count
from django.db.models import Q

from conda_sentinel.collectors.models import InventoryEntry
from conda_sentinel.collectors.models import OperatorDigest
from conda_sentinel.collectors.selection import RESOLVED_CONFIDENCES
from conda_sentinel.collectors.selection import UNRESOLVED_CONFIDENCES
from conda_sentinel.core.collection import ALLOWANCE_REFUSAL_MARKER
from conda_sentinel.core.credentials import CREDENTIAL_SCHEME
from conda_sentinel.core.credentials import host_of
from conda_sentinel.core.delivery import DEFAULT_WEBHOOK_TIMEOUT
from conda_sentinel.core.delivery import DeliveryOutcome
from conda_sentinel.core.delivery import RequestsWebhookDeliverer
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
from conda_sentinel.core.ledger import current_trace_id
from conda_sentinel.core.models import CollectionRun
from conda_sentinel.core.models import PolicyRun
from conda_sentinel.core.registry import PACKAGE_COLUMN
from conda_sentinel.core.registry import registered_collectors
from conda_sentinel.core.registry import selected_package_ids
from conda_sentinel.core.retention import PRUNE_COLLECTOR
from conda_sentinel.core.runs import RunState
from conda_sentinel.core.runs import counts_by_state
from conda_sentinel.core.runs import states_as_words
from conda_sentinel.identity.confidence import IdentityConfidence
from conda_sentinel.identity.models import Package

if TYPE_CHECKING:
    from collections.abc import Iterable

    from conda_sentinel.core.clock import Clock
    from conda_sentinel.core.collection import Collector
    from conda_sentinel.core.delivery import WebhookDeliverer

__all__ = [
    "CHANNEL_EMAIL",
    "CHANNEL_WEBHOOK",
    "COMPOSED_EVENT_KEYS",
    "DELIVERED",
    "DELIVERY_EVENT_KEYS",
    "DIGEST_COMPOSED_EVENT",
    "DIGEST_DELIVERED_EVENT",
    "DIGEST_DELIVERY_FAILED_EVENT",
    "DIGEST_SETTINGS",
    "DIGEST_STORED_ONLY_EVENT",
    "DIGEST_STORE_FAILED_EVENT",
    "DIGEST_WINDOW",
    "EMAIL_SETTING",
    "FAILED",
    "NOTHING_STANDING",
    "SUBJECT_FORMAT",
    "UNCHANGED_SUFFIX",
    "WEBHOOK_URL_SETTING",
    "CollectorScope",
    "DeclaredDelivery",
    "FreshnessFigures",
    "LedgerRow",
    "collector_figures",
    "compose_digest",
    "declaration_fault",
    "declared_deliveries",
    "digest_subject",
    "email_fault",
    "freshness_figures",
    "has_changed",
    "prune_figures",
    "render_text",
    "standing_problems",
    "unchanged_text",
    "webhook_url_fault",
    "window_start_for",
]

logger: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

#: The events, and the keys each carries -- fixed here so a log query does not
#: have to know which path produced a line (`core/collection.py`'s `EVENT_KEYS`
#: sets the terms). `digest.composed` carries the row and the window;
#: `digest.delivered` and `digest.delivery_failed` carry the channel, the
#: target (a host or an address, never a URL) and the detail;
#: `digest.stored_only` is the line an operator reads when nothing is declared;
#: `digest.store_failed` is the one that says a digest was delivered and then
#: could not be written, carrying what each channel answered so the record is
#: at least in the log.
DIGEST_COMPOSED_EVENT: Final[str] = "digest.composed"
DIGEST_DELIVERED_EVENT: Final[str] = "digest.delivered"
DIGEST_DELIVERY_FAILED_EVENT: Final[str] = "digest.delivery_failed"
DIGEST_STORED_ONLY_EVENT: Final[str] = "digest.stored_only"
DIGEST_STORE_FAILED_EVENT: Final[str] = "digest.store_failed"
COMPOSED_EVENT_KEYS: Final[tuple[str, ...]] = ("digest_id", "window_start", "window_end", "changed", "collectors")
DELIVERY_EVENT_KEYS: Final[tuple[str, ...]] = ("channel", "target", "detail")

#: The two declarations, read from `django.conf.settings` at call time so a case
#: can declare either through `override_settings`. `config/settings/base.py`
#: assigns both from the environment, stripped, empty by default;
#: `config/settings/test.py` empties both whatever the shell holds.
WEBHOOK_URL_SETTING: Final[str] = "CPM_DIGEST_WEBHOOK_URL"
EMAIL_SETTING: Final[str] = "CPM_DIGEST_EMAIL"
DIGEST_SETTINGS: Final[tuple[str, ...]] = (WEBHOOK_URL_SETTING, EMAIL_SETTING)

#: The window one digest covers when there is no previous digest to continue
#: from: the twenty-four hours ending at the instant it is composed. Otherwise
#: the window begins where the previous one ended (`window_start_for`), so a
#: late tick loses no rows and a by-hand run double-counts nothing. Equal to the
#: `cpm-digest` beat entry's interval; `tests/unit/django_apps/test_digest.py`
#: pins the two together.
DIGEST_WINDOW: Final[timedelta] = timedelta(hours=24)

#: How stale a previous digest may be and still have its window continued. A
#: previous digest older than this -- beat down for days -- is not continued:
#: the digest reports one day, and a window of a week would report a week as
#: if it were a day.
CONTINUATION_LIMIT: Final[timedelta] = 2 * DIGEST_WINDOW

#: The two channels and the two delivery states a row's `deliveries` entries
#: carry. Fixed lowercase strings, on `CPM-AD-5`'s terms for a value a machine
#: reads back.
CHANNEL_WEBHOOK: Final[str] = "webhook"
CHANNEL_EMAIL: Final[str] = "email"
DELIVERED: Final[str] = "delivered"
FAILED: Final[str] = "failed"

#: The mail subject and the first line of the text, dated by the day composed,
#: and what is appended to it on a day nothing changed.
SUBJECT_FORMAT: Final[str] = "conda-sentinel digest {date}"
UNCHANGED_SUFFIX: Final[str] = " (unchanged)"

#: What the unchanged line says when no collector is failing, refused or stale.
NOTHING_STANDING: Final[str] = "nothing standing"

#: The keys `figures` is composed under are `core/digest_keys.py`'s, imported
#: above: the Digests page reads them back from the same module, so the two
#: cannot disagree about a spelling. The resolved and unresolved halves of the
#: package table are `collectors/selection.py`'s -- resolved is `verified` and
#: nothing else, because a package leaves the identity review set only on
#: `verified` -- so the digest's "unresolved" is exactly the size of that set.

#: How an instant is written into the text: the page's own `Y-m-d H:i` with the
#: literal `Z` every panel carries. Honest because `TIME_ZONE` is UTC.
_STAMP_FORMAT: Final[str] = "%Y-%m-%d %H:%MZ"
_DATE_FORMAT: Final[str] = "%Y-%m-%d"

#: The two characters that end a header on the wire; an address carrying either
#: would inject a second header into the mail.
_LINE_BREAKS: Final[frozenset[str]] = frozenset({"\r", "\n"})

#: The two characters that make one address read as a list of them.
_LIST_SEPARATORS: Final[frozenset[str]] = frozenset({",", ";"})

_SECONDS_PER_DAY: Final[int] = 24 * 60 * 60
_SECONDS_PER_HOUR: Final[int] = 60 * 60
_SECONDS_PER_MINUTE: Final[int] = 60


@dataclass(frozen=True, slots=True)
class LedgerRow:
    """What the digest reads off one `collection_runs` row.

    Attributes:
        collector: The collector name the row is filed under.
        package_id: The package the run was scoped to, or `None` for a run
            that was not scoped to one -- a dispatch, or a run-scoped run.
        status: The `RunState` value the row holds now.
        detail: Why it ended the way it did; read for the refusal marker.

    """

    collector: str
    package_id: int | None
    status: str
    detail: str = ""


@dataclass(frozen=True, slots=True)
class CollectorScope:
    """What the digest knows about one registered collector before it counts anything.

    Attributes:
        name: The declared name.
        per_package: Whether the collector is swept one package at a time --
            `selectable_packages()` answers something other than `None` -- in
            which case a row with no package is a *dispatch* and a row with one
            is a *collection*. For a run-scoped collector every row is a
            collection.
        freshness_target: The declared target, or `None`.
        collector: The registered class itself, for the reads that need its
            evidence table; `None` in the pure cases, which need none.

    """

    name: str
    per_package: bool
    freshness_target: timedelta | None = None
    collector: type[Collector] | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class DeclaredDelivery:
    """One declared channel, with the destination kept out of its own `repr`.

    Attributes:
        channel: `webhook` or `email`.
        target: What rows and log lines name: the webhook's host, or the
            address.
        destination: Where the delivery actually goes -- the URL, which may
            carry a credential, or the address. Excluded from `repr` so a
            logged or asserted `DeclaredDelivery` never prints it.

    """

    channel: str
    target: str
    destination: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class FreshnessFigures:
    """How far one collector's evidence has fallen behind its selection, measured from `now`.

    Attributes:
        past_target: Selected packages the collector has observed at least
            once, but not inside its freshness target.
        never_observed: Selected packages with no row in its evidence table at
            all. Kept apart from `past_target` because the two call for
            different action -- a source that stopped answering, against a
            selection that grew faster than the sweep -- and because the Coverage
            screen's stale count is the first alone.

    """

    past_target: int
    never_observed: int


# ---------------------------------------------------------------------------
# Composition, pure.
# ---------------------------------------------------------------------------


def collector_figures(
    scope: CollectorScope, rows: Iterable[LedgerRow], *, freshness: FreshnessFigures | None = None
) -> dict[str, Any]:
    """Return one collector's figures from its window of ledger rows.

    Args:
        scope: The collector, and whether it is swept per package.
        rows: Its rows in the window, in any order.
        freshness: How far its evidence has fallen behind its selection, or
            `None` when the collector declares no target or no readable
            selection -- then both keys are absent rather than zero.

    Returns:
        `collections` by state and `rate_limited` always; `dispatches` by state
        for a per-package collector; `past_freshness_target` and
        `never_observed` when given.

    """
    dispatches: Counter[str] = Counter()
    collections: Counter[str] = Counter()
    rate_limited = 0
    for row in rows:
        if scope.per_package and row.package_id is None:
            dispatches[row.status] += 1
            continue
        collections[row.status] += 1
        if row.status == RunState.FAILED.value and ALLOWANCE_REFUSAL_MARKER in row.detail:
            rate_limited += 1
    figures: dict[str, Any] = {}
    if scope.per_package:
        figures[DISPATCHES_KEY] = counts_by_state(dispatches)
    figures[COLLECTIONS_KEY] = counts_by_state(collections)
    figures[RATE_LIMITED_KEY] = rate_limited
    if freshness is not None:
        figures[PAST_TARGET_KEY] = freshness.past_target
        figures[NEVER_OBSERVED_KEY] = freshness.never_observed
    return figures


def prune_figures(rows: Iterable[LedgerRow]) -> dict[str, int]:
    """Return the purge's runs by state.

    `PRUNE_COLLECTOR` is not a registered collector, so its rows are reported
    once under `overall` rather than as a collector -- the same reason the
    Coverage screen never shows them.

    Args:
        rows: The window's rows filed under `PRUNE_COLLECTOR`.

    Returns:
        A count per state.

    """
    return counts_by_state(Counter(row.status for row in rows))


def freshness_figures(
    selected: Iterable[int], *, observed_ever: Iterable[int], observed_within_target: Iterable[int]
) -> FreshnessFigures:
    """Return how many selected packages are past the target, and how many were never observed.

    Args:
        selected: The packages the collector can be asked about.
        observed_ever: The packages its evidence table holds any row for.
        observed_within_target: The packages it holds a row for at or after
            `now - target`.

    Returns:
        The two counts; a package is in at most one of them.

    """
    wanted = set(selected)
    ever = set(observed_ever)
    within = set(observed_within_target)
    return FreshnessFigures(past_target=len((wanted & ever) - within), never_observed=len(wanted - ever))


def window_start_for(*, now: datetime, previous_window_end: datetime | None) -> datetime:
    """Return where this digest's window begins.

    Args:
        now: The instant the digest is composed, which ends the window.
        previous_window_end: Where the previous digest's window ended, or
            `None` for the first digest.

    Returns:
        The previous digest's `window_end` when it lies within
        `CONTINUATION_LIMIT` of `now` and not after it -- so a late tick loses
        no rows and a by-hand run double-counts nothing -- otherwise
        `now - DIGEST_WINDOW`.

    """
    if previous_window_end is not None and timedelta(0) <= now - previous_window_end <= CONTINUATION_LIMIT:
        return previous_window_end
    return now - DIGEST_WINDOW


def has_changed(figures: Mapping[str, Any], previous: Mapping[str, Any] | None) -> bool:
    """Report whether these figures differ from the previous digest's.

    Args:
        figures: Today's.
        previous: Yesterday's, or `None` for the first digest.

    Returns:
        `True` when there is no previous digest, or when any figure differs.

    """
    return previous is None or dict(figures) != dict(previous)


def digest_subject(now: datetime, *, changed: bool = True) -> str:
    """Return the mail subject and the text's first line.

    Args:
        now: The instant the digest is composed.
        changed: Whether the figures differ from the previous digest's; an
            unchanged day's subject says so, so an inbox tells the two apart.

    Returns:
        `conda-sentinel digest YYYY-MM-DD`, with `UNCHANGED_SUFFIX` appended
        when nothing changed.

    """
    subject = SUBJECT_FORMAT.format(date=now.strftime(_DATE_FORMAT))
    return subject if changed else f"{subject}{UNCHANGED_SUFFIX}"


def standing_problems(figures: Mapping[str, Any], scopes: Iterable[CollectorScope]) -> str:
    """Return the collectors still failing, refused or behind, as one clause.

    What an unchanged day must still say: "nothing changed" over a sweep that
    has failed three days running is the silence the digest exists to break.

    Args:
        figures: Today's figures.
        scopes: The collectors, in the order they are reported.

    Returns:
        `feedstock 4 failed collections, 23 rate-limited; source_release 141 past
        target` -- only the collectors with a failed collection, a rate-limited
        refusal, a package past its target or one never observed -- or
        `NOTHING_STANDING`.

    """
    per_collector = figures.get(COLLECTORS_KEY)
    if not isinstance(per_collector, Mapping):
        return NOTHING_STANDING
    clauses = []
    for scope in scopes:
        counted = per_collector.get(scope.name)
        if not isinstance(counted, Mapping):
            continue
        parts = []
        failed = _count(counted.get(COLLECTIONS_KEY, {}), RunState.FAILED.value)
        if failed:
            parts.append(f"{failed} failed collection{'' if failed == 1 else 's'}")
        for key, phrase in (
            (RATE_LIMITED_KEY, "rate-limited"),
            (PAST_TARGET_KEY, "past target"),
            (NEVER_OBSERVED_KEY, "never observed"),
        ):
            count = _count(counted, key)
            if count:
                parts.append(f"{count} {phrase}")
        if parts:
            clauses.append(f"{scope.name} {', '.join(parts)}")
    return "; ".join(clauses) if clauses else NOTHING_STANDING


def _count(counted: object, key: str) -> int:
    """Return one non-negative count off a figures mapping, or zero for anything else.

    Args:
        counted: A figures mapping, or whatever a stored row held.
        key: Which count.

    Returns:
        The count.

    """
    if not isinstance(counted, Mapping):
        return 0
    value = counted.get(key, 0)
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def unchanged_text(*, now: datetime, since: datetime, standing: str, trace_id: str) -> str:
    """Return the digest for a day in which nothing changed.

    Args:
        now: The instant the digest is composed.
        since: When the most recent digest whose figures *did* change was
            composed -- the one whose figures today's still equal.
        standing: What `standing_problems` said of today's figures.
        trace_id: The task's trace id, or `""`.

    Returns:
        The subject line, one sentence naming that digest's date and carrying
        the standing problems, and the trace line.

    """
    return (
        f"{digest_subject(now, changed=False)}\n"
        f"Nothing changed since the digest of {since.strftime(_DATE_FORMAT)} -- standing: {standing}\n"
        f"{_trace_line(trace_id)}"
    )


def _trace_line(trace_id: str) -> str:
    """Return the line that names the task's trace.

    Args:
        trace_id: The id, or `""` outside a span.

    Returns:
        `Trace: <id>`, or `Trace: none`.

    """
    return f"Trace: {trace_id or 'none'}"


def render_text(
    figures: Mapping[str, Any],
    *,
    now: datetime,
    window_start: datetime,
    scopes: Iterable[CollectorScope],
    trace_id: str = "",
) -> str:
    """Render the full digest as plain text.

    Args:
        figures: What `compose_digest` composed.
        now: The instant the digest is composed, which ends the window.
        window_start: Where the window begins.
        scopes: The collectors, in the order they are reported.
        trace_id: The task's trace id, so the text an operator receives leads
            back to the run's own log lines.

    Returns:
        The text the mail and the webhook carry and the page shows.

    """
    lines = [
        digest_subject(now),
        f"Window: {window_start.strftime(_STAMP_FORMAT)} to {now.strftime(_STAMP_FORMAT)}",
        _trace_line(trace_id),
        "",
        "Collectors",
    ]
    per_collector: Mapping[str, Mapping[str, Any]] = figures.get(COLLECTORS_KEY, {})
    lines.extend(f"  {scope.name}: {_collector_line(scope, per_collector.get(scope.name, {}))}" for scope in scopes)
    overall: Mapping[str, Any] = figures.get(OVERALL_KEY, {})
    lines.extend(
        [
            "",
            "Overall",
            f"  prune runs: {states_as_words(overall.get(PRUNE_RUNS_KEY, {}))}",
            f"  inventory: {_inventory_line(overall.get(INVENTORY_KEY, {}))}",
            f"  packages: {_packages_line(overall.get(PACKAGES_KEY, {}))}",
            f"  policy run: {_policy_run_line(overall.get(POLICY_RUN_KEY), now=now)}",
        ]
    )
    return "\n".join(lines)


def _collector_line(scope: CollectorScope, figures: Mapping[str, Any]) -> str:
    """Return one collector's sentence.

    Args:
        scope: The collector.
        figures: Its figures.

    Returns:
        Dispatches (per-package only), collections, rate-limited refusals and
        packages past the target, separated by semicolons.

    """
    parts = []
    if DISPATCHES_KEY in figures:
        parts.append(f"dispatches {states_as_words(figures[DISPATCHES_KEY])}")
    parts.append(f"collections {states_as_words(figures.get(COLLECTIONS_KEY, {}))}")
    parts.append(f"{figures.get(RATE_LIMITED_KEY, 0)} rate-limited")
    if PAST_TARGET_KEY in figures and scope.freshness_target is not None:
        parts.append(
            f"{figures[PAST_TARGET_KEY]} past the freshness target of {_span(scope.freshness_target)}, "
            f"{figures.get(NEVER_OBSERVED_KEY, 0)} never observed"
        )
    return "; ".join(parts)


def _inventory_line(figures: Mapping[str, int]) -> str:
    """Return the inventory's sentence.

    Args:
        figures: Ingested and absent counts.

    Returns:
        `120 ingested, 3 absent`.

    """
    return f"{figures.get(INGESTED_KEY, 0)} ingested, {figures.get(ABSENT_KEY, 0)} absent"


def _packages_line(figures: Mapping[str, int]) -> str:
    """Return the package table's sentence.

    Args:
        figures: Counts by confidence, plus the two totals.

    Returns:
        `98 resolved (98 verified), 22 unresolved (2 inventory-derived, 20 unmapped)`.

    """
    resolved = ", ".join(f"{figures.get(confidence, 0)} {confidence}" for confidence in sorted(RESOLVED_CONFIDENCES))
    unresolved = ", ".join(
        f"{figures.get(confidence, 0)} {confidence}" for confidence in sorted(UNRESOLVED_CONFIDENCES)
    )
    return (
        f"{figures.get(RESOLVED_KEY, 0)} resolved ({resolved}), "
        f"{figures.get(UNRESOLVED_KEY, 0)} unresolved ({unresolved})"
    )


def _policy_run_line(figures: Mapping[str, Any] | None, *, now: datetime) -> str:
    """Return the newest finished policy run's sentence, or that none has finished.

    The age is rendered here rather than stored: figures hold the run's ending,
    which does not move between two days in which no run finished, so the
    digest of a quiet day compares equal to the day before. The run's state is
    stored and said, because a newest run that failed must not read as a
    healthy one.

    Args:
        figures: The version, ending and state, or `None`.
        now: The instant the age is measured from.

    Returns:
        The sentence.

    """
    if not figures:
        return "no policy run has finished"
    finished_at = figures.get(FINISHED_AT_KEY)
    status = figures.get(STATUS_KEY) or "unknown state"
    if not isinstance(finished_at, str):
        return f"version {figures.get(VERSION_KEY)} (unknown ending), {status}"
    ended = datetime.fromisoformat(finished_at)
    return (
        f"version {figures.get(VERSION_KEY)} finished {ended.strftime(_STAMP_FORMAT)}, "
        f"{_span(now - ended)} ago, {status}"
    )


def _span(delta: timedelta) -> str:
    """Return a duration in the largest whole unit a person would say.

    Args:
        delta: The duration. Negative reads as zero.

    Returns:
        `2 days`, `10 hours`, `5 minutes`.

    """
    seconds = max(int(delta.total_seconds()), 0)
    if seconds >= _SECONDS_PER_DAY:
        return _plural(seconds // _SECONDS_PER_DAY, "day")
    if seconds >= _SECONDS_PER_HOUR:
        return _plural(seconds // _SECONDS_PER_HOUR, "hour")
    return _plural(seconds // _SECONDS_PER_MINUTE, "minute")


def _plural(count: int, unit: str) -> str:
    """Return a count with its unit, pluralised.

    Args:
        count: How many.
        unit: The singular.

    Returns:
        `1 day`, `2 days`.

    """
    return f"{count} {unit}" if count == 1 else f"{count} {unit}s"


# ---------------------------------------------------------------------------
# The declarations, and their boot-time faults.
# ---------------------------------------------------------------------------


def webhook_url_fault(value: object) -> str:  # noqa: PLR0911 - one return per reason a URL is refused
    """Return why a declared webhook URL cannot be posted to, or `""` when it can.

    Pure, so the boot hook and a case ask one question of one value. Every
    sentence names the setting and the *kind* of fault and never the value: a
    webhook URL may carry a credential, and a refusal is written to stderr at
    boot and from there into whatever collects a crashed container's logs.

    Args:
        value: Whatever the settings module assigned.

    Returns:
        The reason, or `""`. Empty or whitespace-only is *usable*: nothing is
        declared and the digest is stored only.

    """
    if not isinstance(value, str):
        return (
            f"{WEBHOOK_URL_SETTING} holds {type(value).__name__} rather than a string, so this component cannot "
            f"post the digest to it. Declare it as an https:// URL, or leave it empty to store the digest only "
            f"(CPM-OPERATE-S09)."
        )
    url = value.strip()
    if not url:
        return ""
    if any(character.isspace() for character in url):
        return (
            f"{WEBHOOK_URL_SETTING} carries whitespace inside the value, which no URL does; it is refused rather "
            f"than posted to, and is not repeated here."
        )
    try:
        parts = urlsplit(url)
        # `port` is parsed lazily and raises for `https://host:notaport/`; asked
        # here so a URL `requests` would refuse is refused at boot instead.
        _ = parts.port
    except ValueError:
        return (
            f"{WEBHOOK_URL_SETTING} cannot be read as a URL; it is refused rather than posted to, and is not "
            f"repeated here."
        )
    if parts.scheme.lower() != CREDENTIAL_SCHEME:
        return (
            f"{WEBHOOK_URL_SETTING} is not an {CREDENTIAL_SCHEME}:// URL. The digest names every collector's state "
            f"and the URL may carry a credential, so it is posted over TLS or not at all; the value is refused at "
            f"boot and is not repeated here (CPM-OPERATE-S09)."
        )
    if host_of(url) is None:
        return (
            f"{WEBHOOK_URL_SETTING} names no host, so there is nowhere to post the digest; the value is refused at "
            f"boot and is not repeated here."
        )
    return ""


def email_fault(value: object) -> str:  # noqa: PLR0911 - one return per reason an address is refused
    """Return why a declared address cannot be mailed to, or `""` when it can.

    On `webhook_url_fault`'s terms: pure, and never repeating the value.

    Args:
        value: Whatever the settings module assigned.

    Returns:
        The reason, or `""` for an empty declaration or an address with a
        local part, an `@` and a domain and no whitespace.

    """
    if not isinstance(value, str):
        return (
            f"{EMAIL_SETTING} holds {type(value).__name__} rather than a string, so this component cannot mail the "
            f"digest to it. Declare it as one address, or leave it empty to store the digest only (CPM-OPERATE-S09)."
        )
    address = value.strip()
    if not address:
        return ""
    if any(character in _LINE_BREAKS for character in address):
        return (
            f"{EMAIL_SETTING} carries a line break inside the value. A mail header is terminated by CRLF, so a "
            f"line break inside an address is the start of another header; the value is refused rather than sent, "
            f"and is not repeated here."
        )
    if any(character.isspace() for character in address):
        return (
            f"{EMAIL_SETTING} carries whitespace inside the value, which no address does; it is refused rather than "
            f"sent, and is not repeated here."
        )
    if any(character in _LIST_SEPARATORS for character in address):
        return (
            f"{EMAIL_SETTING} carries a comma or a semicolon, which reads as a list of addresses. The digest goes "
            f"to one address; the value is refused at boot and is not repeated here (CPM-OPERATE-S09)."
        )
    local, separator, domain = address.partition("@")
    if not separator or not local or not domain or "@" in domain:
        return (
            f"{EMAIL_SETTING} is not an address: one local part, exactly one @ and a domain. The value is refused "
            f"at boot and is not repeated here (CPM-OPERATE-S09)."
        )
    return ""


def declaration_fault(declared: Any) -> str:
    """Return why the two digest declarations cannot be used, or `""` when both can.

    The rule `collectors/apps.py` applies at boot. Absent from the settings
    module is a dropped assignment and refused by name; empty is the shipped
    state and boots, storing the digest only.

    Args:
        declared: The settings object the boot hook read.

    Returns:
        The first fault found, or `""`.

    """
    for setting, fault in ((WEBHOOK_URL_SETTING, webhook_url_fault), (EMAIL_SETTING, email_fault)):
        value = getattr(declared, setting, None)
        if value is None:
            return (
                f"{setting} is not configured, so this component cannot tell where the daily digest goes. "
                f"config/settings/base.py assigns it -- empty by default, which stores the digest only -- and a "
                f"settings module with no assignment at all is one that dropped the line (CPM-OPERATE-S09)."
            )
        reason = fault(value)
        if reason:
            return reason
    return ""


def declared_deliveries() -> tuple[DeclaredDelivery, ...]:
    """Return the channels the settings declare, webhook first.

    Read from `django.conf.settings` at *call* time rather than copied at
    import, so a case can declare either through `override_settings`. The
    values were judged at boot; a value that is declared is one that passed.

    Returns:
        Zero, one or two deliveries. Empty means the digest is stored only.

    """
    declared: list[DeclaredDelivery] = []
    url = str(getattr(settings, WEBHOOK_URL_SETTING, "") or "").strip()
    if url:
        declared.append(DeclaredDelivery(channel=CHANNEL_WEBHOOK, target=host_of(url) or "", destination=url))
    address = str(getattr(settings, EMAIL_SETTING, "") or "").strip()
    if address:
        declared.append(DeclaredDelivery(channel=CHANNEL_EMAIL, target=address, destination=address))
    return tuple(declared)


# ---------------------------------------------------------------------------
# The one function that reads and writes.
# ---------------------------------------------------------------------------


def compose_digest(*, clock: Clock, deliverer: WebhookDeliverer | None = None) -> OperatorDigest:
    """Compose the digest for the day ending now, deliver it, and store it.

    Args:
        clock: The injected clock (`CPM-AD-26`); `now` ends the window.
        deliverer: The webhook seam, or `None` for the one that opens a
            connection. A test hands in a recorded fake.

    Returns:
        The stored row, deliveries and all.

    Raises:
        Exception: Whatever the database raised when the row could not be
            written, after `digest.store_failed` has logged what each channel
            answered. Re-raised rather than swallowed, because a digest that was
            delivered and not recorded is a state the task must report.

    """
    now = clock.now()
    trace_id = current_trace_id()
    previous = OperatorDigest.objects.order_by("-observed_at", "-id").first()
    window_start = window_start_for(now=now, previous_window_end=previous.window_end if previous else None)
    asked = tuple(_ask(collector) for collector in registered_collectors())
    scopes = tuple(scope for scope, _selection in asked)
    rows = _rows_in_window(window_start, now)

    figures: dict[str, Any] = {
        COLLECTORS_KEY: {
            scope.name: collector_figures(
                scope, rows.get(scope.name, ()), freshness=_freshness(scope.collector, selection, now=now)
            )
            for scope, selection in asked
        },
        OVERALL_KEY: {
            PRUNE_RUNS_KEY: prune_figures(rows.get(PRUNE_COLLECTOR, ())),
            INVENTORY_KEY: _inventory_figures(),
            PACKAGES_KEY: _package_figures(),
            POLICY_RUN_KEY: _policy_run_figures(),
        },
    }

    changed = has_changed(figures, previous.figures if previous is not None else None)
    if changed:
        text = render_text(figures, now=now, window_start=window_start, scopes=scopes, trace_id=trace_id)
    else:
        # The date named is the most recent digest whose figures *did* change --
        # the one today's still equal -- not merely yesterday's, which on the
        # third quiet day would name a row that itself said nothing changed.
        last_changed = OperatorDigest.objects.filter(changed=True).order_by("-observed_at", "-id").first()
        since = last_changed.observed_at if last_changed is not None else previous.observed_at  # type: ignore[union-attr]
        text = unchanged_text(now=now, since=since, standing=standing_problems(figures, scopes), trace_id=trace_id)

    subject = digest_subject(now, changed=changed)
    deliveries = _deliver(
        subject=subject,
        text=text,
        body={
            "subject": subject,
            "text": text,
            "window_start": window_start.isoformat(),
            "window_end": now.isoformat(),
            "changed": changed,
            "trace_id": trace_id,
            "figures": figures,
        },
        deliverer=deliverer if deliverer is not None else RequestsWebhookDeliverer(),
    )

    digest = OperatorDigest(
        window_start=window_start,
        window_end=now,
        figures=figures,
        text=text,
        changed=changed,
        deliveries=deliveries,
        trace_id=trace_id,
        observed_at=now,
    )
    try:
        digest.save()
    except Exception:
        # Delivered and then not recorded: the one state the append-only order
        # of operations leaves open. Logged with what each channel answered --
        # hosts and addresses, never a URL -- and re-raised into the task.
        logger.exception(
            DIGEST_STORE_FAILED_EVENT,
            window_start=window_start.isoformat(),
            window_end=now.isoformat(),
            changed=changed,
            deliveries=deliveries,
        )
        raise
    logger.info(
        DIGEST_COMPOSED_EVENT,
        digest_id=digest.pk,
        window_start=window_start.isoformat(),
        window_end=now.isoformat(),
        changed=changed,
        collectors=len(scopes),
    )
    if not deliveries:
        logger.info(DIGEST_STORED_ONLY_EVENT, digest_id=digest.pk)
    return digest


def _ask(collector: type[Collector]) -> tuple[CollectorScope, Iterable[int] | None]:
    """Ask one collector for its selection, once, and return what the digest needs to know.

    Args:
        collector: The registered class.

    Returns:
        The scope -- name, whether it is swept per package, its target -- and
        the selection it answered, so the freshness figures read the same
        answer rather than asking again.

    """
    selection = collector.selectable_packages()
    scope = CollectorScope(
        name=collector.name,
        per_package=selection is not None,
        freshness_target=collector.freshness_target,
        collector=collector,
    )
    return scope, selection


def _rows_in_window(window_start: datetime, window_end: datetime) -> dict[str, list[LedgerRow]]:
    """Return the ledger rows that started inside the window, grouped by collector.

    Args:
        window_start: Inclusive.
        window_end: Exclusive.

    Returns:
        Collector name to its rows. Names nothing registers -- the demo
        seeder's, say -- are grouped too and simply never asked for.

    """
    grouped: defaultdict[str, list[LedgerRow]] = defaultdict(list)
    rows = CollectionRun.objects.filter(started_at__gte=window_start, started_at__lt=window_end).values_list(
        "collector", "package_id", "status", "detail"
    )
    for collector, package_id, status, detail in rows.iterator():
        grouped[collector].append(LedgerRow(collector=collector, package_id=package_id, status=status, detail=detail))
    return dict(grouped)


def _freshness(
    collector: type[Collector] | None, selection: Iterable[int] | None, *, now: datetime
) -> FreshnessFigures | None:
    """Return how far one collector's evidence has fallen behind its selection, or `None`.

    Args:
        collector: The registered class.
        selection: What its `selectable_packages()` answered, asked once by
            `_ask`.
        now: The instant the target is measured from.

    Returns:
        The two counts, or `None` when the collector declares no target, no
        evidence table, no selection, or a selection of a shape
        `core/registry.py` cannot read as package keys -- then the figures are
        absent rather than invented.

    """
    if collector is None or selection is None:
        return None
    target = collector.freshness_target
    evidence_model = collector.evidence_model
    if target is None or evidence_model is None:
        return None
    selected = selected_package_ids(collector, selection=selection)
    if selected is None:
        return None
    column = PACKAGE_COLUMN
    observed_ever = evidence_model.objects.values_list(column, flat=True).distinct()
    observed_within = evidence_model.objects.filter(observed_at__gte=now - target).values_list(column, flat=True)
    return freshness_figures(selected, observed_ever=observed_ever, observed_within_target=observed_within.distinct())


def _inventory_figures() -> dict[str, int]:
    """Return how many inventory entries are ingested (active) and how many absent (retired).

    Returns:
        The two counts.

    """
    counted = InventoryEntry.objects.aggregate(
        ingested=Count("pk", filter=Q(retired_at__isnull=True)),
        absent=Count("pk", filter=Q(retired_at__isnull=False)),
    )
    return {INGESTED_KEY: int(counted["ingested"] or 0), ABSENT_KEY: int(counted["absent"] or 0)}


def _package_figures() -> dict[str, int]:
    """Return the package table by confidence, with the resolved and unresolved totals.

    Summed from `collectors/selection.py`'s two halves rather than tested
    against them: `tests/unit/django_apps/test_confidence_gate_audit.py` reads a
    membership test against a confidence as a second gate, and a count selects
    no status.

    Returns:
        One count per declared confidence, every confidence present, plus
        `resolved` (the identity review set's complement) and `unresolved`
        (that set's size).

    """
    counted = {
        row["confidence"]: int(row["count"]) for row in Package.objects.values("confidence").annotate(count=Count("pk"))
    }
    by_confidence = {confidence.value: counted.get(confidence.value, 0) for confidence in IdentityConfidence}
    resolved = sum(by_confidence[confidence] for confidence in RESOLVED_CONFIDENCES)
    unresolved = sum(by_confidence[confidence] for confidence in UNRESOLVED_CONFIDENCES)
    return {**by_confidence, RESOLVED_KEY: resolved, UNRESOLVED_KEY: unresolved}


def _policy_run_figures() -> dict[str, str] | None:
    """Return the newest finished policy run's version, ending and state, or `None`.

    `finished()` answers every run with an ending, a failed one included: the
    newest run is the newest whatever it came to, and its state is stored so a
    failure reads as one.

    Returns:
        `{version, finished_at, status}` with the ending in ISO 8601, or `None`
        when no policy run has finished.

    """
    newest = PolicyRun.objects.finished().first()
    if newest is None or newest.finished_at is None:
        return None
    return {
        VERSION_KEY: newest.policy_version,
        FINISHED_AT_KEY: newest.finished_at.isoformat(),
        STATUS_KEY: str(newest.status),
    }


def _deliver(*, subject: str, text: str, body: Mapping[str, Any], deliverer: WebhookDeliverer) -> list[dict[str, str]]:
    """Deliver to every declared channel and return one entry per channel.

    Args:
        subject: The mail subject.
        text: The digest.
        body: The webhook's JSON document.
        deliverer: The webhook seam.

    Returns:
        `{channel, target, state, detail}` per declared channel, in declaration
        order; empty when nothing is declared.

    """
    entries: list[dict[str, str]] = []
    for delivery in declared_deliveries():
        outcome = _send(delivery, subject=subject, text=text, body=body, deliverer=deliverer)
        state = DELIVERED if outcome.delivered else FAILED
        entries.append(
            {"channel": delivery.channel, "target": delivery.target, "state": state, "detail": outcome.detail}
        )
        if outcome.delivered:
            logger.info(DIGEST_DELIVERED_EVENT, channel=delivery.channel, target=delivery.target, detail=outcome.detail)
    return entries


def _send(
    delivery: DeclaredDelivery, *, subject: str, text: str, body: Mapping[str, Any], deliverer: WebhookDeliverer
) -> DeliveryOutcome:
    """Deliver to one channel, reporting rather than raising every failure.

    Args:
        delivery: The channel.
        subject: The mail subject.
        text: The digest.
        body: The webhook's JSON document.
        deliverer: The webhook seam.

    Returns:
        The outcome. A failure's detail is the exception's class name or the
        status the hook answered -- never the library's own message, which
        prints the URL.

    """
    if delivery.channel == CHANNEL_EMAIL:
        try:
            send_mail(subject, text, settings.DEFAULT_FROM_EMAIL, [delivery.destination], fail_silently=False)
        except Exception as failure:
            # Not only `SMTPException` and `OSError`: a backend that refuses a
            # header, a settings module that names no `DEFAULT_FROM_EMAIL`, a
            # backend that raises its own type -- each is a delivery that did not
            # happen, and none may reach the beat. The traceback is kept; an
            # address is the record's own target and nothing here is a URL.
            logger.exception(
                DIGEST_DELIVERY_FAILED_EVENT,
                channel=delivery.channel,
                target=delivery.target,
                detail=type(failure).__name__,
            )
            return DeliveryOutcome(delivered=False, detail=type(failure).__name__)
        return DeliveryOutcome(delivered=True)
    try:
        outcome = deliverer.post(delivery.destination, body=body, timeout=DEFAULT_WEBHOOK_TIMEOUT)
    except Exception as failure:  # noqa: BLE001 - recorded, never raised into the beat; see below
        # Logged without the traceback, deliberately: the seam's contract is that
        # a deliverer reports rather than raises, so what raises here is a
        # substitute -- and a substitute's message may print the URL it was
        # handed, which is the one string no log line may carry.
        outcome = DeliveryOutcome(delivered=False, detail=type(failure).__name__)
    if not outcome.delivered:
        logger.warning(
            DIGEST_DELIVERY_FAILED_EVENT, channel=delivery.channel, target=delivery.target, detail=outcome.detail
        )
    return outcome
