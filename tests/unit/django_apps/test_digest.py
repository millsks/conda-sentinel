"""`CPM-OPERATE-S09`: the operator digest, composed with a stopped clock and no database.

`collectors/digest.py` keeps composition pure -- figures from rows, text from
figures, `changed` from two dictionaries, the declarations' faults from a value
-- so every row of the story's matrix that is about *what the digest says* is
exercised here over literal ledger rows, and the integration module keeps to the
one function that reads and writes. The seam that opens a connection is measured
against a substituted `requests.post`, because what it must do with a failure --
name the class or the status and never the URL -- is a property of the seam and
not of a socket.
"""

from __future__ import annotations

import inspect
import re
from dataclasses import dataclass
from dataclasses import field
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING
from typing import Final
from typing import Self

import pytest
import requests
from django.apps import apps
from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings

from conda_sentinel.collectors import digest
from conda_sentinel.collectors.digest import COMPOSED_EVENT_KEYS
from conda_sentinel.collectors.digest import CONTINUATION_LIMIT
from conda_sentinel.collectors.digest import DELIVERY_EVENT_KEYS
from conda_sentinel.collectors.digest import DIGEST_SETTINGS
from conda_sentinel.collectors.digest import DIGEST_WINDOW
from conda_sentinel.collectors.digest import EMAIL_SETTING
from conda_sentinel.collectors.digest import NOTHING_STANDING
from conda_sentinel.collectors.digest import UNCHANGED_SUFFIX
from conda_sentinel.collectors.digest import WEBHOOK_URL_SETTING
from conda_sentinel.collectors.digest import CollectorScope
from conda_sentinel.collectors.digest import DeclaredDelivery
from conda_sentinel.collectors.digest import FreshnessFigures
from conda_sentinel.collectors.digest import LedgerRow
from conda_sentinel.collectors.digest import collector_figures
from conda_sentinel.collectors.digest import declaration_fault
from conda_sentinel.collectors.digest import declared_deliveries
from conda_sentinel.collectors.digest import digest_subject
from conda_sentinel.collectors.digest import email_fault
from conda_sentinel.collectors.digest import freshness_figures
from conda_sentinel.collectors.digest import has_changed
from conda_sentinel.collectors.digest import prune_figures
from conda_sentinel.collectors.digest import render_text
from conda_sentinel.collectors.digest import standing_problems
from conda_sentinel.collectors.digest import unchanged_text
from conda_sentinel.collectors.digest import webhook_url_fault
from conda_sentinel.collectors.digest import window_start_for
from conda_sentinel.collectors.selection import RESOLVED_CONFIDENCES
from conda_sentinel.collectors.selection import UNRESOLVED_CONFIDENCES
from conda_sentinel.collectors.tasks import DIGEST_TASK_NAME
from conda_sentinel.collectors.tasks import declared_inventory_adapter
from conda_sentinel.collectors.tasks import withdraw_inventory_adapter
from conda_sentinel.core.clock import FixedClock
from conda_sentinel.core.collection import ALLOWANCE_REFUSAL_MARKER
from conda_sentinel.core.collection import Collector
from conda_sentinel.core.delivery import DEFAULT_WEBHOOK_TIMEOUT
from conda_sentinel.core.delivery import DeliveryOutcome
from conda_sentinel.core.delivery import RequestsWebhookDeliverer
from conda_sentinel.core.delivery import WebhookDeliverer
from conda_sentinel.core.digest_keys import COLLECTIONS_KEY
from conda_sentinel.core.digest_keys import COLLECTORS_KEY
from conda_sentinel.core.digest_keys import DISPATCHES_KEY
from conda_sentinel.core.digest_keys import INVENTORY_KEY
from conda_sentinel.core.digest_keys import NEVER_OBSERVED_KEY
from conda_sentinel.core.digest_keys import OVERALL_KEY
from conda_sentinel.core.digest_keys import PACKAGES_KEY
from conda_sentinel.core.digest_keys import PAST_TARGET_KEY
from conda_sentinel.core.digest_keys import POLICY_RUN_KEY
from conda_sentinel.core.digest_keys import PRUNE_RUNS_KEY
from conda_sentinel.core.digest_keys import RATE_LIMITED_KEY
from conda_sentinel.core.digest_keys import STATUS_KEY
from conda_sentinel.core.queues import Queue
from conda_sentinel.core.queues import task_name
from conda_sentinel.core.runs import RunState
from conda_sentinel.core.runs import states_as_words
from conda_sentinel.identity.confidence import IdentityConfidence
from tests.clocks import FIXED_INSTANT
from tests.collectors import A_CREDENTIALED_WEBHOOK_URL
from tests.collectors import A_DIGEST_EMAIL
from tests.collectors import A_DIGEST_WEBHOOK_HOST
from tests.collectors import A_DIGEST_WEBHOOK_URL
from tests.collectors import A_WEBHOOK_SECRET
from tests.collectors import RecordedWebhookDeliverer

if TYPE_CHECKING:
    from collections.abc import Iterator
    from collections.abc import Mapping

#: The application whose `ready()` refuses a malformed declaration.
COLLECTORS_APP_LABEL: Final[str] = "collectors"

#: The collectors the cases below compose over: one swept per package with a
#: two-day target, one run-scoped.
SOURCE_RELEASE: Final[CollectorScope] = CollectorScope(
    name="source_release", per_package=True, freshness_target=timedelta(days=2)
)
INVENTORY: Final[CollectorScope] = CollectorScope(name="inventory", per_package=False, freshness_target=None)
SCOPES: Final[tuple[CollectorScope, ...]] = (INVENTORY, SOURCE_RELEASE)

WINDOW_START: Final[datetime] = FIXED_INSTANT - DIGEST_WINDOW
A_TRACE: Final[str] = "0adaeefdf97a30a3d1722fdc6c9e24cf"
A_POLICY_VERSION: Final[str] = "2026.09.4"
A_RUN_ENDING: Final[datetime] = FIXED_INSTANT - timedelta(hours=10)

#: The counts the daily scenario seeds, named so an assertion reads as a claim.
THREE: Final[int] = 3
FIVE: Final[int] = 5
TWO: Final[int] = 2


def a_dispatch(collector: str = SOURCE_RELEASE.name, *, status: str = RunState.SUCCEEDED.value) -> LedgerRow:
    """Return one dispatch row: scoped to no package.

    Args:
        collector: Whose.
        status: Its state.

    Returns:
        The row.

    """
    return LedgerRow(collector=collector, package_id=None, status=status)


def a_collection(
    collector: str = SOURCE_RELEASE.name,
    *,
    package_id: int = 1,
    status: str = RunState.SUCCEEDED.value,
    detail: str = "",
) -> LedgerRow:
    """Return one collection row: scoped to a package.

    Args:
        collector: Whose.
        package_id: Which package.
        status: Its state.
        detail: Its detail.

    Returns:
        The row.

    """
    return LedgerRow(collector=collector, package_id=package_id, status=status, detail=detail)


def a_rate_limited_collection(package_id: int) -> LedgerRow:
    """Return a failed collection refused on the allowance, as `_call_refusal` would write it.

    Args:
        package_id: Which package.

    Returns:
        The row, carrying the marker inside a longer sentence.

    """
    return a_collection(
        package_id=package_id,
        status=RunState.FAILED.value,
        detail=f"source_release has spent its allowance of 60 requests per 1:00:00. {ALLOWANCE_REFUSAL_MARKER}",
    )


def daily_figures() -> dict[str, object]:
    """Return the figures the daily scenario composes to.

    Returns:
        Per-collector and overall figures, as `compose_digest` would assemble them.

    """
    return {
        COLLECTORS_KEY: {
            INVENTORY.name: collector_figures(INVENTORY, [a_collection(INVENTORY.name, package_id=None)]),  # type: ignore[arg-type]
            SOURCE_RELEASE.name: collector_figures(
                SOURCE_RELEASE,
                [
                    a_dispatch(),
                    a_collection(package_id=1),
                    a_collection(package_id=2, status=RunState.FAILED.value, detail="TransportError: refused"),
                    a_rate_limited_collection(3),
                ],
                freshness=FreshnessFigures(past_target=THREE, never_observed=TWO),
            ),
        },
        OVERALL_KEY: {
            PRUNE_RUNS_KEY: prune_figures([LedgerRow("prune_evidence", None, RunState.SUCCEEDED.value)]),
            INVENTORY_KEY: {"ingested": 120, "absent": 3},
            PACKAGES_KEY: {
                IdentityConfidence.VERIFIED.value: 98,
                IdentityConfidence.INVENTORY_DERIVED.value: 2,
                IdentityConfidence.UNMAPPED.value: 20,
                "resolved": 98,
                "unresolved": 22,
            },
            POLICY_RUN_KEY: {
                "version": A_POLICY_VERSION,
                "finished_at": A_RUN_ENDING.isoformat(),
                STATUS_KEY: RunState.SUCCEEDED.value,
            },
        },
    }


def rendered(figures: dict[str, object] | None = None, *, now: datetime = FIXED_INSTANT, trace: str = A_TRACE) -> str:
    """Render the daily scenario, or the figures given.

    Args:
        figures: What to render; the daily figures by default.
        now: The instant.
        trace: The trace id.

    Returns:
        The text.

    """
    return render_text(
        daily_figures() if figures is None else figures,
        now=now,
        window_start=now - DIGEST_WINDOW,
        scopes=SCOPES,
        trace_id=trace,
    )


# ---------------------------------------------------------------------------
# The figures, per collector: dispatches, collections, rate-limited, past target.
# ---------------------------------------------------------------------------


def test_a_per_package_collector_counts_dispatches_apart_from_collections_by_state() -> None:
    """The matrix's daily row: a row with no package is a dispatch, a row with one a collection."""
    figures = collector_figures(
        SOURCE_RELEASE,
        [
            a_dispatch(),
            a_dispatch(status=RunState.SKIPPED.value),
            a_collection(package_id=1),
            a_collection(package_id=2, status=RunState.FAILED.value),
            a_collection(package_id=3, status=RunState.RUNNING.value),
        ],
    )

    assert figures[DISPATCHES_KEY] == {"running": 0, "succeeded": 1, "partial": 0, "failed": 0, "skipped": 1}
    assert figures[COLLECTIONS_KEY] == {"running": 1, "succeeded": 1, "partial": 0, "failed": 1, "skipped": 0}
    assert figures[RATE_LIMITED_KEY] == 0
    assert PAST_TARGET_KEY not in figures
    assert NEVER_OBSERVED_KEY not in figures


def test_a_run_scoped_collector_counts_every_row_as_a_collection() -> None:
    """Inventory ingestion is scoped to no package, and its rows are not dispatches."""
    figures = collector_figures(
        INVENTORY,
        [a_collection(INVENTORY.name, package_id=None), a_collection(INVENTORY.name, package_id=None)],  # type: ignore[arg-type]
    )

    assert DISPATCHES_KEY not in figures
    assert figures[COLLECTIONS_KEY]["succeeded"] == TWO


def test_every_state_is_present_so_two_days_compare_by_value() -> None:
    """A state nothing hit is zero, not absent -- absent would make a quiet day differ from a busy one's shape."""
    figures = collector_figures(SOURCE_RELEASE, [])

    assert set(figures[DISPATCHES_KEY]) == {state.value for state in RunState}
    assert set(figures[COLLECTIONS_KEY]) == {state.value for state in RunState}
    assert sum(figures[COLLECTIONS_KEY].values()) == 0


def test_rate_limited_is_the_failed_collections_carrying_the_marker() -> None:
    """The matrix's rate-limited row: three refusals for `source_release` count three."""
    rows = [
        a_rate_limited_collection(1),
        a_rate_limited_collection(2),
        a_rate_limited_collection(3),
        a_collection(package_id=4, status=RunState.FAILED.value, detail="TransportError: 503"),
        # The marker on a row that did not fail is not a refusal -- nothing writes
        # that, and the count is about failures.
        a_collection(package_id=5, status=RunState.SUCCEEDED.value, detail=ALLOWANCE_REFUSAL_MARKER),
    ]

    figures = collector_figures(SOURCE_RELEASE, rows)

    assert figures[RATE_LIMITED_KEY] == THREE
    assert figures[COLLECTIONS_KEY]["failed"] == THREE + 1


def test_the_marker_is_the_one_both_refusal_texts_are_written_with() -> None:
    """The marker constant is written by `_call_refusal`, for both texts, and read here -- no substring guessed twice.

    Asserted against the source of the two writers rather than against a sample
    sentence, because a sample sentence is the second spelling this rule forbids.
    """
    allowance = inspect.getsource(Collector._call_refusal)  # noqa: SLF001 - the writer under audit
    credential = inspect.getsource(Collector._credential_refused_earlier)  # noqa: SLF001 - as above

    assert "ALLOWANCE_REFUSAL_MARKER" in allowance
    assert "ALLOWANCE_REFUSAL_MARKER" in credential
    assert "ALLOWANCE_REFUSAL_MARKER" in inspect.getsource(digest)  # read by the counter's module, by name
    assert re.fullmatch(r"\[[a-z ]+\]", ALLOWANCE_REFUSAL_MARKER), "a marker is a bracketed tag, not a sentence"


def test_past_target_and_never_observed_are_told_apart() -> None:
    """The matrix's stale row, split: five selectable, two within the target, one observed only long ago, two never."""
    figures = freshness_figures(range(1, FIVE + 1), observed_ever=[1, 2, 3, 99], observed_within_target=[1, 2])

    assert figures == FreshnessFigures(past_target=1, never_observed=TWO)
    assert freshness_figures([1, 2, 3], observed_ever=[1, 2, 3], observed_within_target=[1, 2, 3]) == FreshnessFigures(
        0, 0
    )
    assert freshness_figures([], observed_ever=[], observed_within_target=[]) == FreshnessFigures(0, 0)


def test_the_freshness_figures_are_carried_when_given_and_absent_otherwise() -> None:
    """A collector with no target or no selection has neither figure, rather than zeros that read as fresh."""
    carried = collector_figures(SOURCE_RELEASE, [], freshness=FreshnessFigures(past_target=THREE, never_observed=1))

    assert carried[PAST_TARGET_KEY] == THREE
    assert carried[NEVER_OBSERVED_KEY] == 1
    absent = collector_figures(SOURCE_RELEASE, [], freshness=None)
    assert PAST_TARGET_KEY not in absent
    assert NEVER_OBSERVED_KEY not in absent


def test_prune_runs_are_counted_by_state() -> None:
    """`PRUNE_COLLECTOR` rows are reported once under overall, by state."""
    counted = prune_figures(
        [
            LedgerRow("prune_evidence", None, RunState.SUCCEEDED.value),
            LedgerRow("prune_evidence", None, RunState.FAILED.value),
        ]
    )

    assert counted["succeeded"] == 1
    assert counted["failed"] == 1
    assert counted["running"] == 0


# ---------------------------------------------------------------------------
# Changed or not.
# ---------------------------------------------------------------------------


def test_the_two_halves_are_the_identity_review_sets_and_partition_the_enum() -> None:
    """Resolved is `verified` alone -- a package leaves the review set only on `verified`; the rest is unresolved."""
    assert frozenset({IdentityConfidence.VERIFIED.value}) == RESOLVED_CONFIDENCES
    assert (
        frozenset({IdentityConfidence.INVENTORY_DERIVED.value, IdentityConfidence.UNMAPPED.value})
        == UNRESOLVED_CONFIDENCES
    )
    assert set(IdentityConfidence.values) == RESOLVED_CONFIDENCES | UNRESOLVED_CONFIDENCES
    assert set() == RESOLVED_CONFIDENCES & UNRESOLVED_CONFIDENCES


def test_the_first_digest_is_changed() -> None:
    """The matrix's first-digest row: no previous figures means `changed`."""
    assert has_changed(daily_figures(), None) is True


def test_equal_figures_are_unchanged_and_any_difference_is_changed() -> None:
    """The matrix's nothing-changed row, and its complement."""
    today = daily_figures()
    yesterday = daily_figures()

    assert has_changed(today, yesterday) is False

    yesterday[OVERALL_KEY][INVENTORY_KEY]["ingested"] += 1  # type: ignore[index]

    assert has_changed(today, yesterday) is True


def test_the_unchanged_text_is_one_sentence_naming_the_last_changed_digest_and_the_standing_problems() -> None:
    """Stored and delivered all the same; it says which digest it repeats and what is still wrong."""
    since = FIXED_INSTANT - timedelta(days=3)

    text = unchanged_text(now=FIXED_INSTANT, since=since, standing="feedstock 4 failed collections", trace_id=A_TRACE)

    subject, sentence, trace = text.split("\n")
    assert subject == f"conda-sentinel digest 2026-09-04{UNCHANGED_SUFFIX}"
    assert sentence == "Nothing changed since the digest of 2026-09-01 -- standing: feedstock 4 failed collections"
    assert trace == f"Trace: {A_TRACE}"


def test_standing_problems_name_only_the_collectors_still_failing_refused_or_behind() -> None:
    """The repeated-failure day: what the one line must still carry."""
    figures = daily_figures()

    standing = standing_problems(figures, SCOPES)

    assert standing == "source_release 2 failed collections, 1 rate-limited, 3 past target, 2 never observed"


def test_a_genuinely_quiet_day_has_nothing_standing() -> None:
    """No failure, no refusal, nothing behind: the line says so rather than trailing an empty clause."""
    figures = daily_figures()
    figures[COLLECTORS_KEY][SOURCE_RELEASE.name] = collector_figures(  # type: ignore[index]
        SOURCE_RELEASE, [a_dispatch(), a_collection()], freshness=FreshnessFigures(0, 0)
    )

    assert standing_problems(figures, SCOPES) == NOTHING_STANDING
    assert standing_problems({}, SCOPES) == NOTHING_STANDING
    assert "standing: nothing standing" in unchanged_text(
        now=FIXED_INSTANT, since=FIXED_INSTANT - timedelta(days=1), standing=NOTHING_STANDING, trace_id=""
    )


def test_one_failed_collection_is_singular() -> None:
    """`1 failed collection`, not `1 failed collections`."""
    figures = daily_figures()
    figures[COLLECTORS_KEY][SOURCE_RELEASE.name] = collector_figures(  # type: ignore[index]
        SOURCE_RELEASE, [a_collection(status=RunState.FAILED.value)]
    )

    assert standing_problems(figures, SCOPES) == "source_release 1 failed collection"


# ---------------------------------------------------------------------------
# The text.
# ---------------------------------------------------------------------------


def test_the_subject_is_dated_by_the_day_composed_and_says_when_nothing_changed() -> None:
    """`conda-sentinel digest YYYY-MM-DD`, and ` (unchanged)` on a quiet day so an inbox tells the two apart."""
    assert digest_subject(FIXED_INSTANT) == "conda-sentinel digest 2026-09-04"
    assert digest_subject(FIXED_INSTANT, changed=False) == "conda-sentinel digest 2026-09-04 (unchanged)"


def test_the_full_text_names_every_collector_every_overall_figure_and_the_trace() -> None:
    """The matrix's daily row, rendered: per collector by state, the refusals, the two stale counts, overall, trace."""
    text = rendered()

    lines = text.split("\n")
    assert lines[0] == digest_subject(FIXED_INSTANT)
    assert lines[1] == "Window: 2026-09-03 12:00Z to 2026-09-04 12:00Z"
    assert lines[2] == f"Trace: {A_TRACE}"
    assert "  inventory: collections 1 succeeded; 0 rate-limited" in lines
    assert (
        "  source_release: dispatches 1 succeeded; collections 1 succeeded, 2 failed; 1 rate-limited; "
        "3 past the freshness target of 2 days, 2 never observed"
    ) in lines
    assert "  prune runs: 1 succeeded" in lines
    assert "  inventory: 120 ingested, 3 absent" in lines
    assert "  packages: 98 resolved (98 verified), 22 unresolved (2 inventory-derived, 20 unmapped)" in lines
    assert "  policy run: version 2026.09.4 finished 2026-09-04 02:00Z, 10 hours ago, succeeded" in lines


def test_a_failed_newest_policy_run_reads_as_failed() -> None:
    """A newest run that failed must not read as a healthy one."""
    figures = daily_figures()
    figures[OVERALL_KEY][POLICY_RUN_KEY][STATUS_KEY] = RunState.FAILED.value  # type: ignore[index]

    assert "  policy run: version 2026.09.4 finished 2026-09-04 02:00Z, 10 hours ago, failed" in rendered(figures)


def test_the_text_says_when_no_policy_run_has_finished() -> None:
    """The matrix's no-policy-run row: the sentence says none, and carries no age."""
    figures = daily_figures()
    figures[OVERALL_KEY][POLICY_RUN_KEY] = None  # type: ignore[index]

    text = rendered(figures)

    assert "  policy run: no policy run has finished" in text.split("\n")
    assert " ago" not in text


def test_a_state_nothing_hit_is_left_out_of_the_sentence_and_none_means_none() -> None:
    """Zero counts are stored for comparison and not read out loud."""
    figures = daily_figures()
    figures[COLLECTORS_KEY][SOURCE_RELEASE.name] = collector_figures(SOURCE_RELEASE, [])  # type: ignore[index]

    text = rendered(figures)

    assert "  source_release: dispatches none; collections none; 0 rate-limited" in text.split("\n")
    assert "0 skipped" not in text
    assert states_as_words({}) == "none"


def test_the_trace_line_says_none_outside_a_span() -> None:
    """A digest composed with no span active still says so, rather than printing an empty id."""
    assert "Trace: none" in rendered(trace="").split("\n")


@pytest.mark.parametrize(
    ("ending", "expected"),
    [
        (FIXED_INSTANT - timedelta(days=3), "3 days ago, succeeded"),
        (FIXED_INSTANT - timedelta(days=1), "1 day ago, succeeded"),
        (FIXED_INSTANT - timedelta(hours=1), "1 hour ago, succeeded"),
        (FIXED_INSTANT - timedelta(minutes=5), "5 minutes ago, succeeded"),
        (FIXED_INSTANT + timedelta(minutes=5), "0 minutes ago, succeeded"),
    ],
    ids=["days", "one-day", "one-hour", "minutes", "future-reads-as-now"],
)
def test_the_policy_runs_age_is_said_in_the_largest_whole_unit(ending: datetime, expected: str) -> None:
    """A person says `3 days ago`, not `3 days, 4:00:00`.

    Args:
        ending: When the run finished.
        expected: What the sentence ends with.

    """
    figures = daily_figures()
    figures[OVERALL_KEY][POLICY_RUN_KEY]["finished_at"] = ending.isoformat()  # type: ignore[index]

    assert rendered(figures).endswith(expected)


def test_the_text_is_rendered_from_the_clock_it_is_handed() -> None:
    """`CPM-AD-26`: the same figures at another instant are dated by that instant, and nothing else moves."""
    later = FixedClock(instant=FIXED_INSTANT + timedelta(days=1)).now()

    text = rendered(now=later)

    assert text.startswith("conda-sentinel digest 2026-09-05")
    assert "Window: 2026-09-04 12:00Z to 2026-09-05 12:00Z" in text
    assert "1 day ago" in text


# ---------------------------------------------------------------------------
# The window.
# ---------------------------------------------------------------------------


def test_the_first_digests_window_is_the_day_ending_now() -> None:
    """No previous digest: twenty-four hours."""
    assert window_start_for(now=FIXED_INSTANT, previous_window_end=None) == WINDOW_START


def test_a_window_continues_from_the_previous_digest_when_it_is_recent() -> None:
    """A late tick loses no rows and a by-hand run double-counts nothing."""
    late = FIXED_INSTANT - timedelta(hours=30)
    early = FIXED_INSTANT - timedelta(hours=2)

    assert window_start_for(now=FIXED_INSTANT, previous_window_end=late) == late
    assert window_start_for(now=FIXED_INSTANT, previous_window_end=early) == early
    assert window_start_for(now=FIXED_INSTANT, previous_window_end=FIXED_INSTANT - CONTINUATION_LIMIT) == (
        FIXED_INSTANT - CONTINUATION_LIMIT
    )


def test_a_window_is_not_continued_from_a_stale_or_future_digest() -> None:
    """Beat down for days, or a clock that went backwards: one day, not a week and not a negative window."""
    stale = FIXED_INSTANT - CONTINUATION_LIMIT - timedelta(seconds=1)
    future = FIXED_INSTANT + timedelta(minutes=1)

    assert window_start_for(now=FIXED_INSTANT, previous_window_end=stale) == WINDOW_START
    assert window_start_for(now=FIXED_INSTANT, previous_window_end=future) == WINDOW_START


def test_the_window_is_the_beat_entrys_interval() -> None:
    """`DIGEST_WINDOW` and the `cpm-digest` schedule are one day, and the two are pinned together."""
    from django.conf import settings  # noqa: PLC0415 - the live declaration, read once

    assert DIGEST_WINDOW == settings.CELERY_BEAT_SCHEDULE["cpm-digest"]["schedule"] == timedelta(hours=24)
    assert CONTINUATION_LIMIT == 2 * DIGEST_WINDOW


# ---------------------------------------------------------------------------
# The declarations.
# ---------------------------------------------------------------------------


def test_nothing_declared_means_no_delivery() -> None:
    """The matrix's nothing-declared row: both empty, and the digest is stored only."""
    with override_settings(**{WEBHOOK_URL_SETTING: "", EMAIL_SETTING: ""}):
        assert declared_deliveries() == ()


def test_both_declared_means_both_delivered_webhook_first() -> None:
    """Two channels, and the row's `target` is the host and the address -- never the URL."""
    with override_settings(**{WEBHOOK_URL_SETTING: A_CREDENTIALED_WEBHOOK_URL, EMAIL_SETTING: A_DIGEST_EMAIL}):
        declared = declared_deliveries()

    assert [(entry.channel, entry.target) for entry in declared] == [
        ("webhook", A_DIGEST_WEBHOOK_HOST),
        ("email", A_DIGEST_EMAIL),
    ]
    assert declared[0].destination == A_CREDENTIALED_WEBHOOK_URL


def test_a_declared_delivery_never_prints_its_destination() -> None:
    """The matrix's credentialed-URL row, at the object: a logged or asserted `DeclaredDelivery` shows the host only."""
    declared = DeclaredDelivery(channel="webhook", target=A_DIGEST_WEBHOOK_HOST, destination=A_CREDENTIALED_WEBHOOK_URL)

    assert A_WEBHOOK_SECRET not in repr(declared)
    assert A_WEBHOOK_SECRET not in str(declared)
    assert A_DIGEST_WEBHOOK_HOST in repr(declared)


def test_the_declarations_are_read_at_call_time_and_stripped() -> None:
    """A case declares through `override_settings` and the composer sees it; a stray newline is not part of a host."""
    with override_settings(**{WEBHOOK_URL_SETTING: f" {A_DIGEST_WEBHOOK_URL}\n", EMAIL_SETTING: " "}):
        (declared,) = declared_deliveries()

    assert declared.destination == A_DIGEST_WEBHOOK_URL
    assert declared.target == A_DIGEST_WEBHOOK_HOST


@pytest.mark.parametrize(
    "value",
    [
        "http://hooks.example.test/digest",
        "hooks.example.test/digest",
        "https:///digest",
        "https://a b/x",
        "https://hooks.example.test:notaport/digest",
        5,
    ],
    ids=["http", "no-scheme", "no-host", "whitespace", "bad-port", "int"],
)
def test_a_webhook_url_that_is_not_https_with_a_host_is_a_fault_naming_the_setting_only(value: object) -> None:
    """The matrix's malformed row, the URL half: the setting's name, never its value.

    Args:
        value: The declaration.

    """
    reason = webhook_url_fault(value)

    assert WEBHOOK_URL_SETTING in reason
    assert "hooks.example.test" not in reason
    assert "/digest" not in reason
    assert "a b" not in reason
    assert "notaport" not in reason


@pytest.mark.parametrize(
    "value", ["", "  ", A_DIGEST_WEBHOOK_URL, A_CREDENTIALED_WEBHOOK_URL, f" {A_DIGEST_WEBHOOK_URL} "]
)
def test_an_empty_or_https_webhook_url_is_usable(value: str) -> None:
    """Empty is the shipped state; a credentialed https URL is what the story admits.

    Args:
        value: The declaration.

    """
    assert webhook_url_fault(value) == ""


@pytest.mark.parametrize(
    "value",
    [
        "ops.example.test",
        "@example.test",
        "ops@",
        "ops @example.test",
        "ops@example.test\nbcc: x@y",
        "ops@example.test@other.test",
        "ops@example.test,sec@example.test",
        "ops@example.test;sec@example.test",
        7,
    ],
    ids=["no-at", "no-local-part", "no-domain", "space", "line-break", "two-ats", "comma", "semicolon", "int"],
)
def test_an_address_without_a_local_part_an_at_and_a_domain_is_a_fault_naming_the_setting_only(
    value: object,
) -> None:
    """The matrix's malformed row, the address half.

    Args:
        value: The declaration.

    """
    reason = email_fault(value)

    assert EMAIL_SETTING in reason
    assert "example.test" not in reason


@pytest.mark.parametrize("value", ["", A_DIGEST_EMAIL, f" {A_DIGEST_EMAIL}\n"])
def test_an_empty_or_well_formed_address_is_usable(value: str) -> None:
    """Empty is the shipped state.

    Args:
        value: The declaration.

    """
    assert email_fault(value) == ""


def test_the_boot_rule_refuses_an_undeclared_setting_by_name_and_accepts_the_shipped_state() -> None:
    """Absent is a dropped assignment; empty boots; the first fault found is the one reported."""
    assert declaration_fault(SimpleNamespace(**{WEBHOOK_URL_SETTING: "", EMAIL_SETTING: ""})) == ""

    missing = declaration_fault(SimpleNamespace(**{EMAIL_SETTING: ""}))
    assert WEBHOOK_URL_SETTING in missing
    assert "not configured" in missing

    missing_address = declaration_fault(SimpleNamespace(**{WEBHOOK_URL_SETTING: ""}))
    assert EMAIL_SETTING in missing_address

    bad = declaration_fault(SimpleNamespace(**{WEBHOOK_URL_SETTING: "http://x/", EMAIL_SETTING: "nobody"}))
    assert WEBHOOK_URL_SETTING in bad
    assert EMAIL_SETTING not in bad


def test_the_two_setting_names_are_the_ones_settings_assigns() -> None:
    """A rename on either side fails here rather than booting a component that stores only, silently."""
    from config.settings import base  # noqa: PLC0415 - the declaration, read beside the names

    assert DIGEST_SETTINGS == ("CPM_DIGEST_WEBHOOK_URL", "CPM_DIGEST_EMAIL")
    for setting in DIGEST_SETTINGS:
        assert hasattr(base, setting), setting


# ---------------------------------------------------------------------------
# The boot hook.
# ---------------------------------------------------------------------------


@pytest.fixture
def _slot_restored() -> Iterator[None]:
    """Start the boot cases from an empty inventory adapter slot, and leave it empty.

    Yields:
        None. The two withdrawals are the effect.

    """
    if declared_inventory_adapter() is not None:
        withdraw_inventory_adapter()
    yield
    if declared_inventory_adapter() is not None:
        withdraw_inventory_adapter()


@pytest.mark.usefixtures("_slot_restored")
@pytest.mark.parametrize(
    ("setting", "value", "secret"),
    [
        (WEBHOOK_URL_SETTING, "http://hooks.example.test/digest", "hooks.example.test"),
        (WEBHOOK_URL_SETTING, f"https://operator:{A_WEBHOOK_SECRET}@ hooks.example.test/x", A_WEBHOOK_SECRET),
        (EMAIL_SETTING, "ops.example.test", "ops.example.test"),
    ],
    ids=["http-url", "whitespace-in-a-credentialed-url", "address-without-an-at"],
)
def test_a_malformed_declaration_refuses_boot_naming_the_setting_and_never_the_value(
    setting: str, value: str, secret: str
) -> None:
    """The matrix's malformed row at the hook: `ImproperlyConfigured`, before the inventory adapter is declared.

    Args:
        setting: Which declaration is malformed.
        value: The declaration.
        secret: A substring of the value that must not reach the refusal.

    """
    with override_settings(**{setting: value}), pytest.raises(ImproperlyConfigured) as refused:
        apps.get_app_config(COLLECTORS_APP_LABEL).ready()

    assert setting in str(refused.value)
    assert secret not in str(refused.value)
    assert declared_inventory_adapter() is None


@pytest.mark.usefixtures("_slot_restored")
@pytest.mark.parametrize("setting", DIGEST_SETTINGS)
def test_a_settings_module_declaring_no_destination_refuses_boot_by_name(setting: str) -> None:
    """Absent from the *settings module* is a dropped assignment, not the empty default.

    Args:
        setting: The declaration deleted.

    """
    from django.conf import settings  # noqa: PLC0415 - deleted through Django's own override machinery

    with override_settings():
        delattr(settings, setting)

        with pytest.raises(ImproperlyConfigured) as refused:
            apps.get_app_config(COLLECTORS_APP_LABEL).ready()

    assert setting in str(refused.value)


@pytest.mark.usefixtures("_slot_restored")
@pytest.mark.parametrize(
    ("url", "address"),
    [("", ""), (A_DIGEST_WEBHOOK_URL, ""), (A_CREDENTIALED_WEBHOOK_URL, A_DIGEST_EMAIL)],
    ids=["nothing", "webhook-only", "both"],
)
def test_empty_or_well_formed_declarations_boot(url: str, address: str) -> None:
    """Every usable state lets the component start; the hook completes and declares the adapter as before.

    Args:
        url: The webhook declaration.
        address: The address declaration.

    """
    with override_settings(**{WEBHOOK_URL_SETTING: url, EMAIL_SETTING: address}):
        apps.get_app_config(COLLECTORS_APP_LABEL).ready()

    assert declared_inventory_adapter() is not None


# ---------------------------------------------------------------------------
# The task's name, the events' keys, and the seam.
# ---------------------------------------------------------------------------


def test_the_task_is_named_under_the_policy_namespace() -> None:
    """`cpm.policy.digest`, composed from `core/queues.py`'s parts: the name is the route."""
    assert DIGEST_TASK_NAME == task_name(Queue.POLICY, "digest") == "cpm.policy.digest"


def test_every_event_declares_its_keys() -> None:
    """The three events carry fixed keys, and the delivery ones never carry a URL key."""
    assert COMPOSED_EVENT_KEYS == ("digest_id", "window_start", "window_end", "changed", "collectors")
    assert DELIVERY_EVENT_KEYS == ("channel", "target", "detail")
    assert "url" not in DELIVERY_EVENT_KEYS
    assert "address" not in DELIVERY_EVENT_KEYS


def test_the_recorded_deliverer_and_the_real_one_both_satisfy_the_seam() -> None:
    """`runtime_checkable`: a substitute is a deliverer by its one method."""
    assert isinstance(RecordedWebhookDeliverer(), WebhookDeliverer)
    assert isinstance(RequestsWebhookDeliverer(), WebhookDeliverer)


@dataclass(slots=True)
class _Answer:
    """What a substituted `requests.post` returns: a status, `raise_for_status`, and a body that must stay unread."""

    status_code: int
    body_read: bool = False

    def raise_for_status(self) -> None:
        if self.status_code >= 400:  # noqa: PLR2004 - HTTP's own boundary
            error = requests.HTTPError(f"{self.status_code} Server Error for url: {A_CREDENTIALED_WEBHOOK_URL}")
            error.response = self  # type: ignore[assignment]
            raise error

    @property
    def content(self) -> bytes:
        self.body_read = True
        return b"a body nobody should read"

    @property
    def text(self) -> str:
        self.body_read = True
        return "a body nobody should read"

    def json(self) -> object:
        self.body_read = True
        return {}

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


@dataclass(slots=True)
class _ScriptedPost:
    """A substitute for `requests.post`: answers one script and remembers every call.

    Attributes:
        answer: The status to answer, or the exception to raise.
        calls: Every call's URL and keywords, in order.

    """

    answer: _Answer | Exception = field(default_factory=lambda: _Answer(200))
    calls: list[dict[str, object]] = field(default_factory=list)

    def __call__(self, url: str, **keywords: object) -> _Answer:
        self.calls.append({"url": url, **keywords})
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


@pytest.fixture
def scripted_post(monkeypatch: pytest.MonkeyPatch) -> _ScriptedPost:
    """Substitute `requests.post` for the body of one case, restored afterwards.

    Args:
        monkeypatch: Restores the library after the case.

    Returns:
        The script, so a case can set its answer and read its calls.

    """
    script = _ScriptedPost()
    monkeypatch.setattr(requests, "post", script)
    return script


def _post_through(deliverer: RequestsWebhookDeliverer) -> DeliveryOutcome:
    """Drive the seam once, against the credentialed URL.

    Args:
        deliverer: The seam.

    Returns:
        The outcome.

    """
    return deliverer.post(A_CREDENTIALED_WEBHOOK_URL, body={"subject": "x"}, timeout=DEFAULT_WEBHOOK_TIMEOUT)


def test_a_two_hundred_is_delivered_and_the_post_carries_the_body_the_timeout_and_no_redirect(
    scripted_post: _ScriptedPost,
) -> None:
    """One `requests.post`, JSON body, the stated timeout, redirects refused."""
    outcome = _post_through(RequestsWebhookDeliverer())

    assert outcome == DeliveryOutcome(delivered=True, detail="", status_code=200)
    (call,) = scripted_post.calls
    assert call["url"] == A_CREDENTIALED_WEBHOOK_URL
    assert call["json"] == {"subject": "x"}
    assert call["timeout"] == DEFAULT_WEBHOOK_TIMEOUT
    assert call["allow_redirects"] is False
    assert call["stream"] is True


def test_the_response_body_is_never_read(scripted_post: _ScriptedPost) -> None:
    """A hook that trickles a body cannot hold the worker: nothing a hook says back is read, on any status."""
    for status in (200, 302, 500):
        scripted_post.answer = _Answer(status)
        _post_through(RequestsWebhookDeliverer())
        assert scripted_post.answer.body_read is False, status


def test_an_informational_status_is_not_a_delivery(scripted_post: _ScriptedPost) -> None:
    """A `1xx` is not an acceptance: only `2xx` is delivered."""
    scripted_post.answer = _Answer(102)

    outcome = _post_through(RequestsWebhookDeliverer())

    assert outcome == DeliveryOutcome(delivered=False, detail="HTTP 102", status_code=102)


def test_a_refused_status_is_failed_with_the_status_and_never_the_url(scripted_post: _ScriptedPost) -> None:
    """The matrix's webhook-refuses row at the seam: `HTTP 500`, and the library's URL-bearing message is dropped."""
    scripted_post.answer = _Answer(500)

    outcome = _post_through(RequestsWebhookDeliverer())

    assert outcome.delivered is False
    assert outcome.detail == "HTTP 500"
    assert outcome.status_code == 500  # noqa: PLR2004 - the status scripted above
    assert A_WEBHOOK_SECRET not in outcome.detail


def test_a_redirect_is_not_a_delivery(scripted_post: _ScriptedPost) -> None:
    """A hook that answers 302 has not accepted the digest, and the credential is not replayed elsewhere."""
    scripted_post.answer = _Answer(302)

    outcome = _post_through(RequestsWebhookDeliverer())

    assert outcome == DeliveryOutcome(delivered=False, detail="HTTP 302", status_code=302)


def test_a_connection_failure_is_failed_with_the_exception_class_and_never_the_url(
    scripted_post: _ScriptedPost,
) -> None:
    """No answer came back: the class name is the whole of the detail."""
    scripted_post.answer = requests.ConnectionError(f"failed to reach {A_CREDENTIALED_WEBHOOK_URL}")

    outcome = _post_through(RequestsWebhookDeliverer())

    assert outcome == DeliveryOutcome(delivered=False, detail="ConnectionError", status_code=None)
    assert A_WEBHOOK_SECRET not in outcome.detail


def test_the_default_timeout_is_ten_seconds() -> None:
    """The whole allowance a receiving hook gets, per connect and per read."""
    assert DEFAULT_WEBHOOK_TIMEOUT == 10.0  # noqa: PLR2004 - the declared value


def test_the_fixture_window_start_is_a_day_before_the_stopped_clock() -> None:
    """The module's own arithmetic, pinned."""
    assert datetime(2026, 9, 3, 12, 0, tzinfo=UTC) == WINDOW_START


def _scopes_named(figures: Mapping[str, object]) -> list[str]:
    """Return the collector names a figures mapping carries, for the assertions above.

    Args:
        figures: The figures.

    Returns:
        The names, in order.

    """
    return list(figures[COLLECTORS_KEY])  # type: ignore[arg-type]


def test_the_daily_figures_fixture_names_the_scopes_in_order() -> None:
    """The fixture this module renders from carries both collectors, in registry order."""
    assert _scopes_named(daily_figures()) == [INVENTORY.name, SOURCE_RELEASE.name]
