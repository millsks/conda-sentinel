"""`CPM-OPERATE-S09`: the digest task stores a row, delivers where declared, and the page shows it.

The unit module proves what the digest *says* from literal rows; this one proves
the function that reads and writes -- `compose_digest` over a seeded ledger,
inventory and package table -- the task around it, the two channels against the
locmem mail backend and a recorded webhook deliverer, the hygiene rule that a
credentialed URL reaches no row and no log line, the by-hand command, and the
Digests page for every role.

Every test rolls back: `@pytest.mark.django_db` wraps each in a transaction.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from datetime import timedelta
from http import HTTPStatus
from io import StringIO
from itertools import pairwise
from smtplib import SMTPException
from typing import TYPE_CHECKING
from typing import Any
from typing import Final

import pytest
from django.conf import settings
from django.contrib.auth.models import Group
from django.core import mail
from django.core.mail import BadHeaderError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import DatabaseError
from django.test import override_settings
from django.urls import reverse
from opentelemetry import trace
from rest_framework.test import APIClient
from structlog.testing import capture_logs

from conda_sentinel.collectors import digest as digest_module
from conda_sentinel.collectors.digest import CHANNEL_EMAIL
from conda_sentinel.collectors.digest import CHANNEL_WEBHOOK
from conda_sentinel.collectors.digest import CONTINUATION_LIMIT
from conda_sentinel.collectors.digest import DELIVERED
from conda_sentinel.collectors.digest import DIGEST_COMPOSED_EVENT
from conda_sentinel.collectors.digest import DIGEST_DELIVERED_EVENT
from conda_sentinel.collectors.digest import DIGEST_DELIVERY_FAILED_EVENT
from conda_sentinel.collectors.digest import DIGEST_STORE_FAILED_EVENT
from conda_sentinel.collectors.digest import DIGEST_STORED_ONLY_EVENT
from conda_sentinel.collectors.digest import DIGEST_WINDOW
from conda_sentinel.collectors.digest import EMAIL_SETTING
from conda_sentinel.collectors.digest import FAILED
from conda_sentinel.collectors.digest import NOTHING_STANDING
from conda_sentinel.collectors.digest import UNCHANGED_SUFFIX
from conda_sentinel.collectors.digest import WEBHOOK_URL_SETTING
from conda_sentinel.collectors.digest import compose_digest
from conda_sentinel.collectors.digest import digest_subject
from conda_sentinel.collectors.feedstock import COLLECTOR_NAME as FEEDSTOCK
from conda_sentinel.collectors.management.commands import compose_digest as digest_command
from conda_sentinel.collectors.models import InventoryEntry
from conda_sentinel.collectors.models import OperatorDigest
from conda_sentinel.collectors.models import SourceReleaseSnapshot
from conda_sentinel.collectors.py314_verification import COLLECTOR_NAME as PY314_VERIFICATION
from conda_sentinel.collectors.source_release import COLLECTOR_NAME as SOURCE_RELEASE
from conda_sentinel.collectors.source_release import SOURCE_RELEASE_FRESHNESS_TARGET
from conda_sentinel.collectors.source_release import SourceReleaseCollector
from conda_sentinel.collectors.tasks import COLLECTOR_NAME as INVENTORY
from conda_sentinel.collectors.tasks import DIGEST_TASK_NAME
from conda_sentinel.collectors.tasks import compose_operator_digest
from conda_sentinel.core.clock import FixedClock
from conda_sentinel.core.collection import ALLOWANCE_REFUSAL_MARKER
from conda_sentinel.core.delivery import DeliveryOutcome
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
from conda_sentinel.core.ledger import TRACE_ID_FORMAT
from conda_sentinel.core.models import AppendOnlyError
from conda_sentinel.core.models import CollectionRun
from conda_sentinel.core.models import PolicyRun
from conda_sentinel.core.outcomes import OutcomeState
from conda_sentinel.core.permissions import PRODUCT_ROLES
from conda_sentinel.core.registry import SELECTION_NOT_ASKED_EVENT
from conda_sentinel.core.registry import registered_collectors
from conda_sentinel.core.retention import PRUNE_COLLECTOR
from conda_sentinel.core.runs import RunState
from conda_sentinel.identity.confidence import IdentityConfidence
from conda_sentinel.identity.models import ESTABLISHED
from conda_sentinel.identity.models import MappingKind
from conda_sentinel.identity.models import Package
from conda_sentinel.identity.models import PackageMapping
from conda_sentinel.surface.digest import DIGEST_HISTORY_LIMIT
from conda_sentinel.surface.digest import NOT_COUNTED
from conda_sentinel.surface.labels import display_label
from config.celery_app import app
from tests.clocks import FIXED_INSTANT
from tests.collectors import A_CREDENTIALED_WEBHOOK_URL
from tests.collectors import A_DIGEST_EMAIL
from tests.collectors import A_DIGEST_WEBHOOK_HOST
from tests.collectors import A_DIGEST_WEBHOOK_URL
from tests.collectors import A_WEBHOOK_SECRET
from tests.collectors import FixedLimiter
from tests.collectors import RecordedTransport
from tests.collectors import RecordedWebhookDeliverer
from tests.collectors import cleared_cache
from tests.collectors import recorded_payload
from tests.factories import UserFactory

if TYPE_CHECKING:
    from collections.abc import Iterator

    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    from pytest_django.fixtures import SettingsWrapper

pytestmark = pytest.mark.integration

NOW: Final[datetime] = FIXED_INSTANT
A_DAY: Final[timedelta] = timedelta(days=1)
A_POLICY_VERSION: Final[str] = "cpm-operate-s09-fixture-policy"

#: The counts the daily scenario seeds, named so an assertion reads as a claim.
THREE: Final[int] = 3
FIVE: Final[int] = 5
TWO: Final[int] = 2


class ADelivererFaultError(RuntimeError):
    """What a substituted deliverer raises, so the refusal is about this case's reason."""


def a_clock(at: datetime = NOW) -> FixedClock:
    """Return a clock stopped at one instant.

    Args:
        at: The instant.

    Returns:
        The clock.

    """
    return FixedClock(instant=at)


def a_package(name: str, *, confidence: str = IdentityConfidence.VERIFIED, repository: str = "") -> Package:
    """Create one package.

    Args:
        name: Its canonical name.
        confidence: How certain its identity is.
        repository: Its source repository URL; non-empty puts it in the
            upstream-release collector's selection.

    Returns:
        The saved row.

    """
    return Package.objects.create(
        canonical_name=name, resolved_at=NOW - A_DAY, confidence=confidence, source_repository_url=repository
    )


def a_ledger_row(
    collector: str,
    *,
    package: Package | None = None,
    status: str = RunState.SUCCEEDED,
    detail: str = "",
    started_at: datetime | None = None,
) -> CollectionRun:
    """Record one run on the ledger, inside the window by default.

    Args:
        collector: Whose.
        package: The package, or `None` for a dispatch or a run-scoped run.
        status: Its state.
        detail: Its detail.
        started_at: When it began; an hour ago by default.

    Returns:
        The saved row.

    """
    began = started_at if started_at is not None else NOW - timedelta(hours=1)
    return CollectionRun.objects.create(
        collector=collector,
        package=package,
        started_at=began,
        finished_at=None if status == RunState.RUNNING else began + timedelta(minutes=1),
        status=status,
        detail=detail,
    )


def a_release_observation(package: Package, *, at: datetime) -> None:
    """Record one upstream-release observation.

    Args:
        package: The package.
        at: When.

    """
    SourceReleaseSnapshot.objects.create(
        observed_at=at,
        package=package,
        source="https://example.invalid/releases",
        state=OutcomeState.OK.value,
        latest_version="1.0.0",
        released_at=at,
    )


def an_inventory_entry(key: str, *, retired: bool = False) -> InventoryEntry:
    """Create one inventory entry.

    Args:
        key: Its source package key, which is also its name.
        retired: Whether it is retired.

    Returns:
        The saved row.

    """
    return InventoryEntry.objects.create(
        source_package_key=key,
        package_name=key,
        internal_component_count=1,
        internal_lob_count=1,
        changed_at=NOW - A_DAY,
        retired_at=NOW - A_DAY if retired else None,
        reason="seeded for the digest",
    )


def a_finished_policy_run(*, finished_at: datetime, status: str = RunState.SUCCEEDED) -> PolicyRun:
    """Record one finished policy run.

    Args:
        finished_at: When it ended.
        status: How it ended.

    Returns:
        The saved row.

    """
    return PolicyRun.objects.create(
        policy_version=A_POLICY_VERSION,
        started_at=finished_at - timedelta(minutes=5),
        finished_at=finished_at,
        evidence_cutoff=finished_at - timedelta(minutes=30),
        status=status,
    )


@pytest.fixture(autouse=True)
def _empty_cache() -> Iterator[None]:
    """Leave no rate-limit counter behind, in either direction.

    Yields:
        Nothing; the fixture is entirely its two side effects.

    """
    with cleared_cache():
        yield


def a_days_ledger() -> None:
    """Seed the daily scenario: dispatches, collections, refusals, stale packages, the tables, a run."""
    a_ledger_row(SOURCE_RELEASE)  # a dispatch: no package
    a_ledger_row(SOURCE_RELEASE, status=RunState.SKIPPED)
    selected = [a_package(f"pkg-{index}", repository=f"https://github.test/org/pkg-{index}") for index in range(FIVE)]
    for package in selected[:TWO]:
        a_release_observation(package, at=NOW - timedelta(hours=6))
    # One observed long ago (past the target), two never observed at all.
    a_release_observation(selected[TWO], at=NOW - SOURCE_RELEASE_FRESHNESS_TARGET - timedelta(hours=1))
    a_ledger_row(SOURCE_RELEASE, package=selected[0])
    a_ledger_row(SOURCE_RELEASE, package=selected[1], status=RunState.FAILED, detail="TransportError: 503")
    for package in selected[TWO:]:
        a_ledger_row(
            SOURCE_RELEASE,
            package=package,
            status=RunState.FAILED,
            detail=f"source_release has spent its allowance (CPM-AD-20). {ALLOWANCE_REFUSAL_MARKER}",
        )
    a_ledger_row(SOURCE_RELEASE, package=selected[0], started_at=NOW - DIGEST_WINDOW - timedelta(minutes=1))
    a_ledger_row(INVENTORY)
    a_ledger_row(PRUNE_COLLECTOR)
    a_ledger_row(PRUNE_COLLECTOR, status=RunState.FAILED, detail="a table refused")
    a_ledger_row("local-dev-demo-seed")
    a_package("unmapped-one", confidence=IdentityConfidence.UNMAPPED)
    a_package("derived-one", confidence=IdentityConfidence.INVENTORY_DERIVED)
    an_inventory_entry("active-a")
    an_inventory_entry("active-b")
    an_inventory_entry("retired-a", retired=True)
    a_finished_policy_run(finished_at=NOW - timedelta(hours=10))
    a_finished_policy_run(finished_at=NOW - timedelta(days=3))


def _events_named(events: list[dict[str, Any]], name: str) -> list[dict[str, Any]]:
    """Return the captured events with one name.

    Args:
        events: What `capture_logs` collected.
        name: The event name.

    Returns:
        The matching events, in order.

    """
    return [event for event in events if event["event"] == name]


@pytest.fixture
def not_eager(settings: SettingsWrapper) -> None:
    """Configure the process as a worker's settings would: `.delay()` publishes.

    Args:
        settings: pytest-django's settings wrapper, restored after the case.

    """
    settings.CELERY_TASK_ALWAYS_EAGER = False
    assert app.conf.task_always_eager is False


# ---------------------------------------------------------------------------
# Composition over a seeded ledger.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_a_days_ledger_composes_to_figures_per_collector_and_overall() -> None:
    """The matrix's daily row: every count the story names, from real tables, under a stopped clock."""
    a_days_ledger()

    digest = compose_digest(clock=a_clock(), deliverer=RecordedWebhookDeliverer())

    per_collector = digest.figures[COLLECTORS_KEY]
    assert list(per_collector) == [collector.name for collector in registered_collectors()]
    source_release = per_collector[SOURCE_RELEASE]
    assert source_release[DISPATCHES_KEY] == {"running": 0, "succeeded": 1, "partial": 0, "failed": 0, "skipped": 1}
    assert source_release[COLLECTIONS_KEY] == {"running": 0, "succeeded": 1, "partial": 0, "failed": 4, "skipped": 0}
    assert source_release[RATE_LIMITED_KEY] == THREE
    assert source_release[PAST_TARGET_KEY] == 1
    assert source_release[NEVER_OBSERVED_KEY] == TWO
    inventory = per_collector[INVENTORY]
    assert DISPATCHES_KEY not in inventory
    assert inventory[COLLECTIONS_KEY]["succeeded"] == 1
    assert "local-dev-demo-seed" not in per_collector

    overall = digest.figures[OVERALL_KEY]
    assert overall[PRUNE_RUNS_KEY]["succeeded"] == 1
    assert overall[PRUNE_RUNS_KEY]["failed"] == 1
    assert overall[INVENTORY_KEY] == {"ingested": 2, "absent": 1}
    assert overall[PACKAGES_KEY] == {
        "verified": FIVE,
        "inventory-derived": 1,
        "unmapped": 1,
        "resolved": FIVE,
        "unresolved": TWO,
    }
    assert overall[POLICY_RUN_KEY] == {
        "version": A_POLICY_VERSION,
        "finished_at": (NOW - timedelta(hours=10)).isoformat(),
        "status": RunState.SUCCEEDED.value,
    }
    assert digest.window_start == NOW - DIGEST_WINDOW
    assert digest.window_end == NOW
    assert digest.observed_at == NOW
    assert digest.changed is True
    assert "10 hours ago, succeeded" in digest.text
    assert (
        f"{SOURCE_RELEASE}: dispatches 1 succeeded, 1 skipped; collections 1 succeeded, 4 failed; 3 rate-limited; "
        f"1 past the freshness target of 2 days, 2 never observed"
    ) in digest.text
    assert "  inventory: 2 ingested, 1 absent" in digest.text
    assert "  packages: 5 resolved (5 verified), 2 unresolved (1 inventory-derived, 1 unmapped)" in digest.text


@pytest.mark.django_db
def test_the_freshness_figures_are_carried_by_exactly_the_collectors_with_a_target_and_a_readable_selection() -> None:
    """Which registered collectors carry the two figures, and which do not, pinned by name."""
    with capture_logs() as events:
        digest = compose_digest(clock=a_clock(), deliverer=RecordedWebhookDeliverer())

    carrying = {name for name, counted in digest.figures[COLLECTORS_KEY].items() if PAST_TARGET_KEY in counted}
    assert carrying == {
        "conda_package",
        FEEDSTOCK,
        "license",
        "pypi_release",
        "python_readiness",
        "resolve_identity",
        SOURCE_RELEASE,
    }
    assert set(digest.figures[COLLECTORS_KEY]) - carrying == {INVENTORY, PY314_VERIFICATION, "kev", "vulnerability"}
    # The two undeclared-source collectors answer a generator, which is not read:
    # the figure is absent and the event says so, once each.
    not_asked = [event["collector"] for event in _events_named(events, SELECTION_NOT_ASKED_EVENT)]
    assert sorted(not_asked) == ["kev", "vulnerability"]


@pytest.mark.django_db
def test_a_mapping_table_selection_is_read_by_its_package_column() -> None:
    """The `package_id` shape: a feedstock mapping puts the package in the selection; no snapshot leaves it behind."""
    package = a_package("mapped-one")
    PackageMapping.objects.create(
        package=package, kind=MappingKind.FEEDSTOCK.value, outcome=ESTABLISHED, resolved_at=NOW
    )

    digest = compose_digest(clock=a_clock(), deliverer=RecordedWebhookDeliverer())

    assert digest.figures[COLLECTORS_KEY][FEEDSTOCK][PAST_TARGET_KEY] == 0
    assert digest.figures[COLLECTORS_KEY][FEEDSTOCK][NEVER_OBSERVED_KEY] == 1


@pytest.mark.django_db
def test_a_real_allowance_refusal_is_counted_as_rate_limited() -> None:
    """End to end: the base's own refusal, through `Collector.collect`, then the digest -- no hand-written marker."""
    package = a_package("refused-one", repository="https://github.com/org/refused-one")
    transport = RecordedTransport(payload=recorded_payload())
    collector = SourceReleaseCollector(
        clock=a_clock(NOW - timedelta(hours=1)), transport=transport, limiter=FixedLimiter(permitted=False)
    )
    try:
        result = collector.collect(package_id=package.pk)
    finally:
        collector.close()
    assert result.state is RunState.FAILED
    assert transport.calls == []

    digest = compose_digest(clock=a_clock(), deliverer=RecordedWebhookDeliverer())

    assert digest.figures[COLLECTORS_KEY][SOURCE_RELEASE][RATE_LIMITED_KEY] == 1
    assert digest.figures[COLLECTORS_KEY][SOURCE_RELEASE][COLLECTIONS_KEY]["failed"] == 1


@pytest.mark.django_db
def test_a_failed_newest_policy_run_is_stored_and_said_as_failed() -> None:
    """`finished()` includes a failed run, and it must not read as healthy."""
    a_finished_policy_run(finished_at=NOW - timedelta(days=1))
    a_finished_policy_run(finished_at=NOW - timedelta(hours=2), status=RunState.FAILED)

    digest = compose_digest(clock=a_clock(), deliverer=RecordedWebhookDeliverer())

    assert digest.figures[OVERALL_KEY][POLICY_RUN_KEY]["status"] == RunState.FAILED.value
    assert "2 hours ago, failed" in digest.text


@pytest.mark.django_db
def test_an_empty_ledger_and_no_policy_run_compose_to_zeros_and_say_so() -> None:
    """The matrix's no-policy-run row, and a fresh stack's first digest."""
    digest = compose_digest(clock=a_clock(), deliverer=RecordedWebhookDeliverer())

    assert digest.figures[OVERALL_KEY][POLICY_RUN_KEY] is None
    assert "no policy run has finished" in digest.text
    assert all(sum(counted[COLLECTIONS_KEY].values()) == 0 for counted in digest.figures[COLLECTORS_KEY].values())
    assert digest.changed is True


@pytest.mark.django_db
def test_the_second_digest_of_a_quiet_day_is_unchanged_and_one_line_with_nothing_standing() -> None:
    """The matrix's nothing-changed row, the genuinely quiet case: equal figures, one sentence, nothing standing."""
    a_finished_policy_run(finished_at=NOW - timedelta(days=2))
    first = compose_digest(clock=a_clock(), deliverer=RecordedWebhookDeliverer())

    second = compose_digest(clock=a_clock(NOW + A_DAY), deliverer=RecordedWebhookDeliverer())

    assert first.changed is True
    assert second.changed is False
    assert second.figures == first.figures
    subject, sentence, trace = second.text.split("\n")
    assert subject == digest_subject(NOW + A_DAY, changed=False)
    assert sentence == f"Nothing changed since the digest of {NOW:%Y-%m-%d} -- standing: {NOTHING_STANDING}"
    assert trace.startswith("Trace: ")
    assert OperatorDigest.objects.count() == TWO


@pytest.mark.django_db
def test_a_repeated_failure_day_is_unchanged_but_still_names_what_is_standing() -> None:
    """The story's own case: a sweep failing on day two exactly as on day one is not silence."""
    package = a_package("failing-one", repository="https://github.com/org/failing-one")
    for day in range(TWO):
        at = NOW + day * A_DAY - timedelta(hours=1)
        a_ledger_row(SOURCE_RELEASE, started_at=at)
        a_ledger_row(
            SOURCE_RELEASE, package=package, status=RunState.FAILED, detail="TransportError: 503", started_at=at
        )
        a_ledger_row(
            SOURCE_RELEASE, package=package, status=RunState.FAILED, detail=ALLOWANCE_REFUSAL_MARKER, started_at=at
        )
    first = compose_digest(clock=a_clock(), deliverer=RecordedWebhookDeliverer())

    second = compose_digest(clock=a_clock(NOW + A_DAY), deliverer=RecordedWebhookDeliverer())

    assert first.changed is True
    assert second.changed is False
    assert second.figures == first.figures
    sentence = second.text.split("\n")[1]
    assert sentence == (
        f"Nothing changed since the digest of {NOW:%Y-%m-%d} -- standing: "
        f"{SOURCE_RELEASE} 2 failed collections, 1 rate-limited, 1 never observed"
    )


@pytest.mark.django_db
def test_three_quiet_days_all_name_the_last_digest_that_changed() -> None:
    """Day four names day one -- the digest whose figures these still equal -- not day three."""
    a_finished_policy_run(finished_at=NOW - timedelta(days=2))
    first = compose_digest(clock=a_clock(), deliverer=RecordedWebhookDeliverer())
    later = [
        compose_digest(clock=a_clock(NOW + day * A_DAY), deliverer=RecordedWebhookDeliverer()) for day in (1, 2, 3)
    ]

    assert [digest.changed for digest in later] == [False, False, False]
    for digest in later:
        assert f"since the digest of {first.observed_at:%Y-%m-%d}" in digest.text
    assert f"since the digest of {later[1].observed_at:%Y-%m-%d}" not in later[2].text


@pytest.mark.django_db
def test_a_figure_that_moved_makes_the_next_digest_changed_again() -> None:
    """An absent inventory entry is a change, and the full text comes back."""
    compose_digest(clock=a_clock(), deliverer=RecordedWebhookDeliverer())
    an_inventory_entry("late", retired=True)

    second = compose_digest(clock=a_clock(NOW + A_DAY), deliverer=RecordedWebhookDeliverer())

    assert second.changed is True
    assert "1 absent" in second.text


@pytest.mark.django_db
def test_a_package_crossing_its_target_on_a_quiet_day_is_a_change() -> None:
    """The freshness figures are measured from `now`, and that is the reading wanted."""
    package = a_package("ageing-one", repository="https://github.com/org/ageing-one")
    a_release_observation(package, at=NOW - SOURCE_RELEASE_FRESHNESS_TARGET + timedelta(hours=12))
    first = compose_digest(clock=a_clock(), deliverer=RecordedWebhookDeliverer())

    second = compose_digest(clock=a_clock(NOW + A_DAY), deliverer=RecordedWebhookDeliverer())

    assert first.figures[COLLECTORS_KEY][SOURCE_RELEASE][PAST_TARGET_KEY] == 0
    assert second.figures[COLLECTORS_KEY][SOURCE_RELEASE][PAST_TARGET_KEY] == 1
    assert second.changed is True


# ---------------------------------------------------------------------------
# The window.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_a_late_digest_continues_from_the_previous_window_and_loses_no_row() -> None:
    """Beat fired thirty hours after the last digest: the row at hour twenty-eight is counted once, by the late one."""
    compose_digest(clock=a_clock(), deliverer=RecordedWebhookDeliverer())
    a_ledger_row(SOURCE_RELEASE, started_at=NOW + timedelta(hours=2))

    late = compose_digest(clock=a_clock(NOW + timedelta(hours=30)), deliverer=RecordedWebhookDeliverer())

    assert late.window_start == NOW
    assert late.window_end == NOW + timedelta(hours=30)
    assert late.figures[COLLECTORS_KEY][SOURCE_RELEASE][DISPATCHES_KEY]["succeeded"] == 1


@pytest.mark.django_db
def test_a_by_hand_digest_continues_from_the_previous_window_and_double_counts_nothing() -> None:
    """Two hours after the daily digest, a by-hand one reports those two hours and nothing the daily one already did."""
    a_ledger_row(SOURCE_RELEASE, started_at=NOW - timedelta(hours=1))
    daily = compose_digest(clock=a_clock(), deliverer=RecordedWebhookDeliverer())

    by_hand = compose_digest(clock=a_clock(NOW + timedelta(hours=2)), deliverer=RecordedWebhookDeliverer())

    assert daily.figures[COLLECTORS_KEY][SOURCE_RELEASE][DISPATCHES_KEY]["succeeded"] == 1
    assert by_hand.window_start == NOW
    assert by_hand.figures[COLLECTORS_KEY][SOURCE_RELEASE][DISPATCHES_KEY]["succeeded"] == 0


@pytest.mark.django_db
def test_a_digest_after_a_long_silence_reports_one_day_not_the_whole_silence() -> None:
    """Beat down for three days: the window is the day ending now, and the days between are not folded in."""
    compose_digest(clock=a_clock(), deliverer=RecordedWebhookDeliverer())
    a_ledger_row(SOURCE_RELEASE, started_at=NOW + timedelta(hours=12))
    resumed_at = NOW + CONTINUATION_LIMIT + timedelta(hours=1)

    resumed = compose_digest(clock=a_clock(resumed_at), deliverer=RecordedWebhookDeliverer())

    assert resumed.window_start == resumed_at - DIGEST_WINDOW
    assert resumed.figures[COLLECTORS_KEY][SOURCE_RELEASE][DISPATCHES_KEY]["succeeded"] == 0


@pytest.mark.django_db
def test_the_row_is_append_only() -> None:
    """Never an `update()` or a `delete()` on a digest row."""
    digest = compose_digest(clock=a_clock(), deliverer=RecordedWebhookDeliverer())

    with pytest.raises(AppendOnlyError):
        OperatorDigest.objects.filter(pk=digest.pk).update(changed=False)
    with pytest.raises(AppendOnlyError):
        digest.delete()
    digest.changed = False
    with pytest.raises(AppendOnlyError):
        digest.save()


# ---------------------------------------------------------------------------
# The task.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_the_task_stores_a_row_and_returns_its_key() -> None:
    """`cpm.policy.digest`, eager: one row, the key back, the composed event with its keys."""
    with capture_logs() as events:
        result = compose_operator_digest.delay()

    (digest,) = OperatorDigest.objects.all()
    assert result.get() == digest.pk
    assert digest.deliveries == []
    (composed,) = _events_named(events, DIGEST_COMPOSED_EVENT)
    assert composed["digest_id"] == digest.pk
    assert composed["changed"] is True
    assert composed["collectors"] == len(registered_collectors())
    (stored_only,) = _events_named(events, DIGEST_STORED_ONLY_EVENT)
    assert stored_only["digest_id"] == digest.pk


@pytest.mark.django_db
def test_the_task_is_registered_under_its_declared_name() -> None:
    """Autodiscovery found it in `collectors/tasks.py`, so beat can reach it."""
    app.loader.import_default_modules()

    assert DIGEST_TASK_NAME in app.tasks
    assert app.tasks[DIGEST_TASK_NAME].run is compose_operator_digest.run


# ---------------------------------------------------------------------------
# Delivery.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_nothing_declared_stores_only_and_says_so() -> None:
    """The matrix's nothing-declared row."""
    deliverer = RecordedWebhookDeliverer()

    with capture_logs() as events:
        digest = compose_digest(clock=a_clock(), deliverer=deliverer)

    assert digest.deliveries == []
    assert deliverer.calls == []
    assert mail.outbox == []
    assert _events_named(events, DIGEST_STORED_ONLY_EVENT) != []
    assert _events_named(events, DIGEST_DELIVERED_EVENT) == []


@pytest.mark.django_db
@override_settings(**{WEBHOOK_URL_SETTING: A_DIGEST_WEBHOOK_URL, EMAIL_SETTING: A_DIGEST_EMAIL})
def test_both_declared_delivers_both_and_records_each() -> None:
    """Webhook first, then mail; the row's entries and the events agree; the mail carries the subject and the text."""
    a_finished_policy_run(finished_at=NOW - timedelta(hours=1))
    deliverer = RecordedWebhookDeliverer()

    with capture_logs() as events:
        digest = compose_digest(clock=a_clock(), deliverer=deliverer)

    assert digest.deliveries == [
        {"channel": CHANNEL_WEBHOOK, "target": A_DIGEST_WEBHOOK_HOST, "state": DELIVERED, "detail": ""},
        {"channel": CHANNEL_EMAIL, "target": A_DIGEST_EMAIL, "state": DELIVERED, "detail": ""},
    ]
    ((url, body, timeout),) = deliverer.calls
    assert url == A_DIGEST_WEBHOOK_URL
    assert timeout == 10.0  # noqa: PLR2004 - the declared timeout
    assert set(body) == {"subject", "text", "window_start", "window_end", "changed", "trace_id", "figures"}
    assert body["subject"] == digest_subject(NOW)
    assert body["text"] == digest.text
    assert body["window_start"] == digest.window_start.isoformat()
    assert body["window_end"] == digest.window_end.isoformat()
    assert body["changed"] is True
    assert body["trace_id"] == digest.trace_id
    assert body["figures"] == digest.figures
    json.dumps(body)  # the document the real deliverer would post is serialisable

    (message,) = mail.outbox
    assert message.subject == f"conda-sentinel digest {NOW:%Y-%m-%d}"
    assert message.body == digest.text
    assert message.to == [A_DIGEST_EMAIL]
    assert message.from_email == settings.DEFAULT_FROM_EMAIL

    delivered = _events_named(events, DIGEST_DELIVERED_EVENT)
    assert [(event["channel"], event["target"]) for event in delivered] == [
        (CHANNEL_WEBHOOK, A_DIGEST_WEBHOOK_HOST),
        (CHANNEL_EMAIL, A_DIGEST_EMAIL),
    ]
    assert _events_named(events, DIGEST_STORED_ONLY_EVENT) == []


@pytest.mark.django_db
@override_settings(**{EMAIL_SETTING: A_DIGEST_EMAIL})
def test_an_unchanged_digest_is_still_delivered() -> None:
    """Silence and health must not look the same: the one-line digest goes out too."""
    compose_digest(clock=a_clock(), deliverer=RecordedWebhookDeliverer())

    second = compose_digest(clock=a_clock(NOW + A_DAY), deliverer=RecordedWebhookDeliverer())

    assert second.changed is False
    assert [entry["state"] for entry in second.deliveries] == [DELIVERED]
    assert len(mail.outbox) == TWO
    assert mail.outbox[1].body == second.text
    assert mail.outbox[1].subject == f"conda-sentinel digest {NOW + A_DAY:%Y-%m-%d}{UNCHANGED_SUFFIX}"
    assert mail.outbox[0].subject == f"conda-sentinel digest {NOW:%Y-%m-%d}"


@pytest.mark.django_db(transaction=True)
@override_settings(**{WEBHOOK_URL_SETTING: A_DIGEST_WEBHOOK_URL, EMAIL_SETTING: A_DIGEST_EMAIL})
def test_the_row_the_body_and_the_text_carry_the_trace_id_of_the_span_they_were_composed_in(
    recorded_spans: InMemorySpanExporter,
) -> None:
    """`CPM-AD-15`, inside a real recording span: the id every log line of the run carries is on the record."""
    deliverer = RecordedWebhookDeliverer()
    tracer = trace.get_tracer(__name__)

    with tracer.start_as_current_span("digest") as span:
        expected = format(span.get_span_context().trace_id, TRACE_ID_FORMAT)
        digest = compose_digest(clock=a_clock(), deliverer=deliverer)

    assert expected != ""
    assert digest.trace_id == expected
    assert OperatorDigest.objects.get(pk=digest.pk).trace_id == expected
    ((_url, body, _timeout),) = deliverer.calls
    assert body["trace_id"] == expected
    assert f"Trace: {expected}" in digest.text.split("\n")
    assert f"Trace: {expected}" in mail.outbox[0].body
    assert "digest" in [recorded.name for recorded in recorded_spans.get_finished_spans()]

    response = a_reader().get(the_page())

    assert expected in response.content.decode()


@pytest.mark.django_db
@override_settings(**{WEBHOOK_URL_SETTING: A_CREDENTIALED_WEBHOOK_URL})
def test_a_webhook_that_refuses_is_a_failed_entry_naming_the_host_and_the_status() -> None:
    """The matrix's webhook-refuses and credentialed-URL rows: `failed`, `HTTP 500`, the host; the row still stored."""
    deliverer = RecordedWebhookDeliverer(outcome=DeliveryOutcome(delivered=False, detail="HTTP 500", status_code=500))

    with capture_logs() as events:
        digest = compose_digest(clock=a_clock(), deliverer=deliverer)

    assert digest.deliveries == [
        {"channel": CHANNEL_WEBHOOK, "target": A_DIGEST_WEBHOOK_HOST, "state": FAILED, "detail": "HTTP 500"},
    ]
    ((url, _body, _timeout),) = deliverer.calls
    assert url == A_CREDENTIALED_WEBHOOK_URL
    (failed,) = _events_named(events, DIGEST_DELIVERY_FAILED_EVENT)
    assert failed["channel"] == CHANNEL_WEBHOOK
    assert failed["target"] == A_DIGEST_WEBHOOK_HOST
    assert failed["detail"] == "HTTP 500"
    assert A_WEBHOOK_SECRET not in json.dumps(digest.deliveries)
    assert A_WEBHOOK_SECRET not in digest.text
    assert A_WEBHOOK_SECRET not in json.dumps(events, default=str)


@pytest.mark.django_db
@override_settings(**{WEBHOOK_URL_SETTING: A_CREDENTIALED_WEBHOOK_URL})
def test_a_deliverer_that_raises_is_a_failed_entry_naming_the_class_and_the_task_still_returns() -> None:
    """Never raised into the beat: the class name is the detail and the row is stored."""
    failure = ADelivererFaultError(f"could not post to {A_CREDENTIALED_WEBHOOK_URL}")
    deliverer = RecordedWebhookDeliverer(failure=failure)

    with capture_logs() as events:
        digest = compose_digest(clock=a_clock(), deliverer=deliverer)

    (entry,) = digest.deliveries
    assert entry["state"] == FAILED
    assert entry["detail"] == "ADelivererFaultError"
    assert entry["target"] == A_DIGEST_WEBHOOK_HOST
    assert A_WEBHOOK_SECRET not in json.dumps(events, default=str)
    assert A_WEBHOOK_SECRET not in json.dumps(digest.deliveries)


@pytest.mark.django_db
@override_settings(**{WEBHOOK_URL_SETTING: A_CREDENTIALED_WEBHOOK_URL})
def test_the_task_returns_the_key_when_the_webhook_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    """The acceptance criterion at the task boundary: a refused, credentialed webhook and the task returns."""
    refusing = RecordedWebhookDeliverer(outcome=DeliveryOutcome(delivered=False, detail="HTTP 500", status_code=500))
    monkeypatch.setattr(digest_module, "RequestsWebhookDeliverer", lambda: refusing)

    result = compose_operator_digest.delay()

    digest = OperatorDigest.objects.get(pk=result.get())
    assert digest.deliveries[0]["state"] == FAILED
    assert digest.deliveries[0]["target"] == A_DIGEST_WEBHOOK_HOST
    assert A_WEBHOOK_SECRET not in json.dumps(digest.deliveries)


@pytest.mark.django_db
@override_settings(**{WEBHOOK_URL_SETTING: A_DIGEST_WEBHOOK_URL, EMAIL_SETTING: A_DIGEST_EMAIL})
def test_a_mail_backend_that_refuses_is_a_failed_entry_and_the_webhook_still_goes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The matrix's mail-refuses row: `SMTPException` is caught, named, and costs the other channel nothing."""

    def _refuse(*_args: object, **_keywords: object) -> int:
        message = "451 try again later"
        raise SMTPException(message)

    monkeypatch.setattr(digest_module, "send_mail", _refuse)
    deliverer = RecordedWebhookDeliverer()

    with capture_logs() as events:
        digest = compose_digest(clock=a_clock(), deliverer=deliverer)

    assert digest.deliveries == [
        {"channel": CHANNEL_WEBHOOK, "target": A_DIGEST_WEBHOOK_HOST, "state": DELIVERED, "detail": ""},
        {"channel": CHANNEL_EMAIL, "target": A_DIGEST_EMAIL, "state": FAILED, "detail": "SMTPException"},
    ]
    assert len(deliverer.calls) == 1
    (failed,) = _events_named(events, DIGEST_DELIVERY_FAILED_EVENT)
    assert (failed["channel"], failed["target"], failed["detail"]) == (CHANNEL_EMAIL, A_DIGEST_EMAIL, "SMTPException")


@pytest.mark.django_db
@override_settings(**{EMAIL_SETTING: A_DIGEST_EMAIL})
@pytest.mark.parametrize(
    "failure",
    [ConnectionRefusedError(), BadHeaderError("a header refused"), ValueError("a backend's own refusal")],
    ids=["os-error", "bad-header", "value-error"],
)
def test_any_failure_at_the_mail_boundary_is_a_failed_entry_with_its_class(
    monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> None:
    """Not only `SMTPException`: whatever the backend raises is recorded, with its traceback logged, never raised.

    Args:
        monkeypatch: Substitutes `send_mail`.
        failure: What it raises.

    """

    def _refuse(*_args: object, **_keywords: object) -> int:
        raise failure

    monkeypatch.setattr(digest_module, "send_mail", _refuse)

    with capture_logs() as events:
        digest = compose_digest(clock=a_clock(), deliverer=RecordedWebhookDeliverer())

    (entry,) = digest.deliveries
    assert entry["state"] == FAILED
    assert entry["detail"] == type(failure).__name__
    (logged,) = _events_named(events, DIGEST_DELIVERY_FAILED_EVENT)
    assert logged["exc_info"] is True
    assert logged["target"] == A_DIGEST_EMAIL


@pytest.mark.django_db
@override_settings(**{WEBHOOK_URL_SETTING: A_CREDENTIALED_WEBHOOK_URL})
def test_a_store_that_fails_after_delivery_is_logged_with_the_outcomes_and_re_raised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Delivered and then not recorded: the one state the append-only order leaves open, and it is not silent."""
    deliverer = RecordedWebhookDeliverer()

    def _refuse(*_args: object, **_keywords: object) -> None:
        message = "the table is gone"
        raise DatabaseError(message)

    monkeypatch.setattr(OperatorDigest, "save", _refuse)

    with capture_logs() as events, pytest.raises(DatabaseError):
        compose_digest(clock=a_clock(), deliverer=deliverer)

    assert len(deliverer.calls) == 1
    (failed,) = _events_named(events, DIGEST_STORE_FAILED_EVENT)
    assert failed["deliveries"] == [
        {"channel": CHANNEL_WEBHOOK, "target": A_DIGEST_WEBHOOK_HOST, "state": DELIVERED, "detail": ""}
    ]
    assert failed["exc_info"] is True
    assert A_WEBHOOK_SECRET not in json.dumps(events, default=str)
    assert OperatorDigest.objects.count() == 0


# ---------------------------------------------------------------------------
# The command.
# ---------------------------------------------------------------------------


def _run(*arguments: str) -> str:
    """Invoke the command and return what it wrote to its own stdout.

    Args:
        *arguments: Its command-line arguments.

    Returns:
        Everything the command wrote.

    """
    output = StringIO()
    call_command("compose_digest", *arguments, stdout=output)
    return output.getvalue()


@pytest.mark.django_db
def test_the_command_composes_inline_under_eager_settings_and_reports_the_row() -> None:
    """`pixi run stack-run compose_digest`, eager: one row, and the report names it."""
    with capture_logs() as events:
        output = _run()

    (digest,) = OperatorDigest.objects.all()
    assert f"as row {digest.pk}: changed, 0 delivered, 0 failed" in output
    assert re.search(r"\bunchanged\b", output) is None
    (composed,) = _events_named(events, digest_command.COMMAND_COMPOSED_EVENT)
    assert composed["digest_id"] == digest.pk
    assert composed["task"] == DIGEST_TASK_NAME
    assert composed["state"] == "changed"

    again = _run()

    assert f"as row {digest.pk + 1}: unchanged, 0 delivered, 0 failed" in again


@pytest.mark.django_db
@override_settings(**{WEBHOOK_URL_SETTING: A_CREDENTIALED_WEBHOOK_URL})
def test_the_command_counts_a_failed_delivery_and_exits_clean(monkeypatch: pytest.MonkeyPatch) -> None:
    """A refused channel is on the row, not a non-zero exit: the row is the record."""
    refusing = RecordedWebhookDeliverer(outcome=DeliveryOutcome(delivered=False, detail="HTTP 503", status_code=503))
    monkeypatch.setattr(digest_module, "RequestsWebhookDeliverer", lambda: refusing)

    output = _run()

    assert "0 delivered, 1 failed" in output
    assert A_WEBHOOK_SECRET not in output


@pytest.mark.django_db
@pytest.mark.usefixtures("not_eager")
def test_the_command_enqueues_once_and_writes_nothing_under_a_workers_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`.delay()` publishes; the report carries the task id and where to look."""

    class _Receipt:
        id = "00000000-0000-4000-8000-000000000059"

    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def _delay(*args: object, **keywords: object) -> _Receipt:
        calls.append((args, keywords))
        return _Receipt()

    monkeypatch.setattr(compose_operator_digest, "delay", _delay)

    with capture_logs() as events:
        output = _run()

    assert calls == [((), {})]
    assert _Receipt.id in output
    assert "Digests page" in output
    assert OperatorDigest.objects.count() == 0
    (enqueued,) = _events_named(events, digest_command.DIGEST_ENQUEUED_EVENT)
    assert enqueued["task_id"] == _Receipt.id


@pytest.mark.django_db
def test_the_command_refuses_with_the_tasks_reason_when_eager_without_propagation(
    settings: SettingsWrapper, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The eager result carries the exception, and that is a refusal rather than a key."""
    settings.CELERY_TASK_EAGER_PROPAGATES = False

    def _fail(**_keywords: object) -> OperatorDigest:
        raise ADelivererFaultError

    monkeypatch.setattr("conda_sentinel.collectors.tasks.compose_digest", _fail)

    with pytest.raises(CommandError, match="ran inline and failed"):
        _run()


# ---------------------------------------------------------------------------
# The page.
# ---------------------------------------------------------------------------


def a_reader(*roles: str) -> APIClient:
    """Return a client signed in as somebody holding the given roles.

    Args:
        *roles: The role slots; leadership by default.

    Returns:
        An authenticated client.

    """
    user = UserFactory.create()
    for role in roles or ("leadership",):
        user.groups.add(Group.objects.get(name=getattr(settings.ROLE_CONTRACT, role)))
    client = APIClient()
    client.force_login(user)
    return client


def the_page() -> str:
    """Return the route.

    Returns:
        The URL.

    """
    return reverse("conda_sentinel:digest")


@pytest.mark.django_db
def test_the_page_shows_the_newest_digest_its_figures_its_deliveries_and_its_trace() -> None:
    """The acceptance criterion's page: text, figures per collector, overall, deliveries, trace -- as projected rows."""
    a_days_ledger()
    with override_settings(**{WEBHOOK_URL_SETTING: A_CREDENTIALED_WEBHOOK_URL, EMAIL_SETTING: A_DIGEST_EMAIL}):
        digest = compose_digest(
            clock=a_clock(),
            deliverer=RecordedWebhookDeliverer(outcome=DeliveryOutcome(delivered=False, detail="HTTP 500")),
        )

    response = a_reader().get(the_page())

    assert response.status_code == HTTPStatus.OK
    assert response.context["digest"].pk == digest.pk
    rows = {row.name: row for row in response.context["collector_rows"]}
    source_release = rows["Source release"]
    assert source_release.dispatches == "1 succeeded, 1 skipped"
    assert source_release.collections == "1 succeeded, 4 failed"
    assert source_release.rate_limited == "3"
    assert source_release.past_freshness_target == "1"
    assert source_release.never_observed == "2"
    assert rows["Inventory"].dispatches == NOT_COUNTED
    assert rows["Inventory"].past_freshness_target == NOT_COUNTED
    overall = {row.label: row.value for row in response.context["overall_rows"]}
    assert overall["Prune runs"] == "1 succeeded, 1 failed"
    assert overall["Inventory"] == "2 ingested, 1 absent"
    assert overall["Packages"].startswith("5 resolved, 2 unresolved (")
    assert overall["Newest policy run"] == (
        f"version {A_POLICY_VERSION}, finished {NOW - timedelta(hours=10):%Y-%m-%d %H:%M}Z, Succeeded"
    )
    body = response.content.decode()
    assert digest.text in body.replace("&#x27;", "'").replace("&quot;", '"')
    assert A_DIGEST_WEBHOOK_HOST in body
    assert "HTTP 500" in body
    assert A_DIGEST_EMAIL in body
    assert A_WEBHOOK_SECRET not in body
    assert "no earlier digest" in body


@pytest.mark.django_db
def test_the_page_orders_collectors_by_the_registry_whatever_order_the_row_holds() -> None:
    """PostgreSQL's `jsonb` keeps no key order: the projection walks the registry, unknown names last."""
    digest = compose_digest(clock=a_clock(), deliverer=RecordedWebhookDeliverer())
    figures = dict(digest.figures)
    per_collector = dict(reversed(list(figures[COLLECTORS_KEY].items())))
    per_collector["a_collector_nothing_registers"] = {"collections": {"succeeded": 1}, "rate_limited": 0}
    figures[COLLECTORS_KEY] = per_collector
    OperatorDigest(
        window_start=digest.window_start,
        window_end=digest.window_end + A_DAY,
        figures=figures,
        text="x",
        changed=True,
        deliveries=[],
        observed_at=digest.observed_at + A_DAY,
    ).save()

    response = a_reader().get(the_page())

    names = [row.name for row in response.context["collector_rows"]]
    assert names == [
        *(display_label(collector.name) for collector in registered_collectors()),
        "A collector nothing registers",
    ]


@pytest.mark.django_db
def test_a_malformed_row_renders_rather_than_failing() -> None:
    """A figure that is not a mapping reads as not counted; the page is 200."""
    OperatorDigest(
        window_start=NOW - DIGEST_WINDOW,
        window_end=NOW,
        figures={"collectors": {SOURCE_RELEASE: "not a mapping", INVENTORY: None}, "overall": "nothing"},
        text="a malformed row",
        changed=True,
        deliveries=[],
        observed_at=NOW,
    ).save()

    response = a_reader().get(the_page())

    assert response.status_code == HTTPStatus.OK
    rows = {row.name: row for row in response.context["collector_rows"]}
    assert rows["Source release"].collections == NOT_COUNTED
    assert rows["Inventory"].rate_limited == NOT_COUNTED
    overall = {row.label: row.value for row in response.context["overall_rows"]}
    assert overall["Prune runs"] == NOT_COUNTED
    assert overall["Newest policy run"] == "no policy run has finished"


@pytest.mark.django_db
def test_the_page_lists_the_previous_digests_newest_first_and_no_more_than_the_limit() -> None:
    """A month of history beneath the newest, each with its date, whether it changed, and its delivery states."""
    for day in range(DIGEST_HISTORY_LIMIT + 2):
        compose_digest(clock=a_clock(NOW + day * A_DAY), deliverer=RecordedWebhookDeliverer())
    an_inventory_entry("late")
    newest = compose_digest(
        clock=a_clock(NOW + (DIGEST_HISTORY_LIMIT + 2) * A_DAY), deliverer=RecordedWebhookDeliverer()
    )

    response = a_reader().get(the_page())

    assert response.status_code == HTTPStatus.OK
    history = response.context["history"]
    assert len(history) == DIGEST_HISTORY_LIMIT
    assert history[0].observed_at == newest.observed_at - A_DAY
    assert all(later.observed_at > earlier.observed_at for later, earlier in pairwise(history))
    assert response.context["digest"].pk == newest.pk
    body = response.content.decode()
    assert "unchanged" in body
    assert "stored only" in body


@pytest.mark.django_db
def test_the_page_says_when_no_digest_exists() -> None:
    """The matrix's page-no-digest row: an empty state, and 200."""
    response = a_reader().get(the_page())

    assert response.status_code == HTTPStatus.OK
    assert "no digest yet" in response.content.decode()
    assert response.context["digest"] is None


@pytest.mark.django_db
@pytest.mark.parametrize("role", sorted(PRODUCT_ROLES))
def test_every_product_role_may_read_the_page(role: str) -> None:
    """Every product role, as the coverage screen is.

    Args:
        role: The role slot.

    """
    assert a_reader(role).get(the_page()).status_code == HTTPStatus.OK


@pytest.mark.django_db
def test_a_reader_holding_no_role_is_refused() -> None:
    """`CPM-AD-13`: a digest is still a surface."""
    client = APIClient()
    client.force_login(UserFactory.create())

    assert client.get(the_page()).status_code == HTTPStatus.FORBIDDEN


@pytest.mark.django_db
def test_an_anonymous_visitor_is_sent_to_sign_in() -> None:
    """The matrix's page-anonymous row."""
    response = APIClient().get(the_page())

    assert response.status_code == HTTPStatus.FOUND
    assert response["Location"].startswith(f"{settings.LOGIN_URL}?")
    assert the_page() in response["Location"]


@pytest.mark.django_db
def test_the_page_offers_no_write_method() -> None:
    """`CPM-AD-10`, at the HTTP boundary: never a delivery from a request."""
    assert a_reader().post(the_page()).status_code == HTTPStatus.METHOD_NOT_ALLOWED


@pytest.mark.django_db
def test_the_nav_links_to_the_page_after_coverage() -> None:
    """The nav entry, and its position."""
    body = a_reader().get(reverse("conda_sentinel:coverage")).content.decode()

    nav = re.search(r'<nav class="app-nav">(.*?)</nav>', body, re.DOTALL)
    assert nav is not None
    links = re.findall(r'href="([^"]+)"', nav.group(1))
    assert links.index(reverse("conda_sentinel:coverage")) + 1 == links.index(the_page())
    assert links[-1] == the_page()
    assert "Digests" in nav.group(1)
