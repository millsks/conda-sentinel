"""`CPM-OPERATE-S03`: the inventory table's one door, against a real database.

Every row of the story's matrix that is about the service or the adapter, made
to happen: each refusal leaving nothing behind, the two writes committing or
rolling back together, the import in its three modes, the database adapter
answering the CSV adapter's document, and a retirement becoming a `not_found`
inventory snapshot on the next ingestion with no code of its own.

Shaped after `test_identity_overrides.py`, because the inventory is the second
governed human write `CPM-FR-3` names and it carries the first's three
obligations: a permission held by membership and nothing else, a non-blank
reason, and an audit row in the same transaction.

Every test rolls back: `@pytest.mark.django_db` wraps each in a transaction.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from typing import Any
from typing import Final

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.contrib.auth.models import Group
from django.db import DatabaseError
from django.db import IntegrityError
from django.db import transaction

from conda_sentinel.collectors import inventory as inventory_module
from conda_sentinel.collectors.inventory import INVENTORY_CHANGED_EVENT
from conda_sentinel.collectors.inventory import INVENTORY_PERMISSION_MISSING
from conda_sentinel.collectors.inventory import INVENTORY_REFUSED_EVENT
from conda_sentinel.collectors.inventory import MAX_SIGNAL
from conda_sentinel.collectors.inventory import ImportOutcome
from conda_sentinel.collectors.inventory import InventoryChangeError
from conda_sentinel.collectors.inventory import InventoryEntryMissingError
from conda_sentinel.collectors.inventory import InventoryNotPermittedError
from conda_sentinel.collectors.inventory import InventoryRow
from conda_sentinel.collectors.inventory import add_entry
from conda_sentinel.collectors.inventory import change_entry
from conda_sentinel.collectors.inventory import import_watchlist
from conda_sentinel.collectors.inventory import retire_entry
from conda_sentinel.collectors.inventory_source import DatabaseInventoryAdapter
from conda_sentinel.collectors.inventory_source import active_records
from conda_sentinel.collectors.models import InventoryChange
from conda_sentinel.collectors.models import InventoryEntry
from conda_sentinel.collectors.models import InventorySnapshot
from conda_sentinel.collectors.tasks import COLLECTOR_NAME
from conda_sentinel.collectors.tasks import INVENTORY_SOURCE
from conda_sentinel.collectors.tasks import InventoryIngestionCollector
from conda_sentinel.collectors.tasks import InventoryRecordError
from conda_sentinel.collectors.tasks import records_in
from conda_sentinel.collectors.watchlist import WATCHLIST_ENCODING
from conda_sentinel.collectors.watchlist import WatchlistAdapter
from conda_sentinel.collectors.watchlist import records_from
from conda_sentinel.collectors.watchlist import watchlist_path
from conda_sentinel.core.clock import FixedClock
from conda_sentinel.core.models import CollectionRun
from conda_sentinel.core.outcomes import OutcomeState
from conda_sentinel.core.roles import INVENTORY_CHANGE_PERMISSION
from conda_sentinel.core.runs import RunState
from conda_sentinel.identity.models import Package
from tests.clocks import FIXED_INSTANT
from tests.clocks import LATER_INSTANT
from tests.factories import UserFactory

if TYPE_CHECKING:
    from datetime import datetime
    from pathlib import Path

    from django_service.users.models import User

pytestmark = pytest.mark.integration

A_KEY: Final[str] = "conda-forge/numpy"
ANOTHER_KEY: Final[str] = "conda-forge/pandas"
A_THIRD_KEY: Final[str] = "conda-forge/scipy"
A_REASON: Final[str] = "the platform team adopted it"
A_LEADER_SUBJECT: Final[str] = "urn:example:principal:a-leader"
AN_OUTSIDER_SUBJECT: Final[str] = "urn:example:principal:an-outsider"
AN_ORIGIN: Final[str] = "/reviewed/watchlist.csv"

#: The development watchlist, parsed once, for the equivalence case.
THE_WATCHLIST: Final[Path] = watchlist_path(local=True)


def _a_row(**changes: Any) -> InventoryRow:
    """Return a well-formed row, with any field replaced.

    Args:
        **changes: Fields to replace.

    Returns:
        The row.

    """
    values: dict[str, Any] = {
        "package_name": "numpy",
        "internal_component_count": 312,
        "internal_lob_count": 9,
        "apps": 148,
    }
    values.update(changes)
    return InventoryRow(**values)


def _a_record(key: str = A_KEY, **changes: Any) -> dict[str, Any]:
    """Return a parsed watchlist record, as `records_from` yields one.

    Args:
        key: The key.
        **changes: Fields to replace.

    Returns:
        The record.

    """
    record: dict[str, Any] = {
        "source_package_key": key,
        "package_name": key.rsplit("/", 1)[-1],
        "internal_component_count": 10,
        "internal_lob_count": 2,
    }
    record.update(changes)
    return record


@pytest.fixture
def stopped_clock() -> FixedClock:
    """A clock stopped at `FIXED_INSTANT`."""
    return FixedClock(instant=FIXED_INSTANT)


@pytest.fixture
def a_leader(db: None) -> User:
    """A user who holds the inventory permission by membership and nothing else.

    Args:
        db: pytest-django's per-test transaction.

    Returns:
        A user `has_perm(INVENTORY_CHANGE_PERMISSION)` answers True for, re-read
        after the membership was added because Django caches permissions on the
        instance.

    """
    user: User = UserFactory.create(username="a-leader", idp_subject=A_LEADER_SUBJECT)
    user.groups.add(Group.objects.get(name=settings.ROLE_CONTRACT.leadership))
    refreshed: User = get_user_model().objects.get(pk=user.pk)
    assert refreshed.has_perm(INVENTORY_CHANGE_PERMISSION), (
        "the leadership group must confer the inventory permission for these cases to mean anything"
    )
    return refreshed


@pytest.fixture
def an_outsider(db: None) -> User:
    """A real, active, saved user in no role group at all.

    Args:
        db: pytest-django's per-test transaction.

    Returns:
        A user holding no permission.

    """
    user: User = UserFactory.create(username="an-outsider", idp_subject=AN_OUTSIDER_SUBJECT)
    return user


@pytest.fixture
def an_entry(a_leader: User, stopped_clock: FixedClock) -> InventoryEntry:
    """One active entry, added through the door.

    Args:
        a_leader: Who adds it.
        stopped_clock: When.

    Returns:
        The entry.

    """
    return add_entry(source_package_key=A_KEY, row=_a_row(), actor=a_leader, reason=A_REASON, clock=stopped_clock).entry


def _events(caplog: pytest.LogCaptureFixture, event: str) -> list[str]:
    """Return the captured messages carrying one event name.

    Args:
        caplog: pytest's capture.
        event: The event.

    Returns:
        The messages.

    """
    return [record.getMessage() for record in caplog.records if event in record.getMessage()]


# ---------------------------------------------------------------------------
# Add.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_an_add_writes_the_entry_and_one_audit_row_with_blank_priors(
    a_leader: User, stopped_clock: FixedClock, caplog: pytest.LogCaptureFixture
) -> None:
    """The matrix's `Add` row: entry plus one audit row, prior blank, both stamped from the clock, logged."""
    with caplog.at_level("INFO", logger=inventory_module.logger.name):
        change = add_entry(
            source_package_key=A_KEY, row=_a_row(), actor=a_leader, reason=f"  {A_REASON}  ", clock=stopped_clock
        )

    entry = InventoryEntry.objects.get(source_package_key=A_KEY)
    assert entry.package_name == "numpy"
    assert (entry.internal_component_count, entry.internal_lob_count, entry.apps) == (312, 9, 148)
    assert (entry.platforms, entry.downloads, entry.versions) == (None, None, None)
    assert entry.retired_at is None
    assert entry.changed_at == FIXED_INSTANT
    assert entry.reason == A_REASON
    assert change.entry == entry
    assert change.actor == a_leader
    assert change.origin == ""
    assert change.observed_at == FIXED_INSTANT
    assert change.prior_package_name == ""
    assert change.new_package_name == "numpy"
    assert (change.prior_internal_component_count, change.new_internal_component_count) == (None, 312)
    assert (change.prior_apps, change.new_apps) == (None, 148)
    assert (change.prior_retired, change.new_retired) == (False, False)
    assert change.reason == A_REASON
    assert InventoryChange.objects.count() == 1
    logged = _events(caplog, INVENTORY_CHANGED_EVENT)
    assert len(logged) == 1
    assert A_LEADER_SUBJECT in logged[0]
    assert "'added'" in logged[0]


@pytest.mark.django_db
def test_an_add_without_the_permission_is_refused_logged_and_writes_nothing(
    an_outsider: User, stopped_clock: FixedClock, caplog: pytest.LogCaptureFixture
) -> None:
    """The matrix's `Add, no permission` row: refused, logged naming the actor, nothing written."""
    with (
        caplog.at_level("WARNING", logger=inventory_module.logger.name),
        pytest.raises(InventoryNotPermittedError, match=INVENTORY_CHANGE_PERMISSION),
    ):
        add_entry(source_package_key=A_KEY, row=_a_row(), actor=an_outsider, reason=A_REASON, clock=stopped_clock)

    assert InventoryEntry.objects.count() == 0
    assert InventoryChange.objects.count() == 0
    refused = _events(caplog, INVENTORY_REFUSED_EVENT)
    assert len(refused) == 1
    assert INVENTORY_PERMISSION_MISSING in refused[0]
    assert AN_OUTSIDER_SUBJECT in refused[0]


@pytest.mark.django_db
def test_an_anonymous_actor_is_refused_and_still_logged(
    stopped_clock: FixedClock, caplog: pytest.LogCaptureFixture
) -> None:
    """`AnonymousUser` has no `idp_subject`; the refusal is recorded anyway rather than lost to an `AttributeError`."""
    with (
        caplog.at_level("WARNING", logger=inventory_module.logger.name),
        pytest.raises(InventoryNotPermittedError),
    ):
        add_entry(source_package_key=A_KEY, row=_a_row(), actor=AnonymousUser(), reason=A_REASON, clock=stopped_clock)  # type: ignore[arg-type]

    assert len(_events(caplog, INVENTORY_REFUSED_EVENT)) == 1


@pytest.mark.django_db
def test_a_superuser_is_admitted_and_the_audit_row_names_them(stopped_clock: FixedClock) -> None:
    """The break-glass the override records as a decision: admitted by `has_perm`, and the row still says who."""
    superuser: User = UserFactory.create(username="root", is_superuser=True)

    change = add_entry(source_package_key=A_KEY, row=_a_row(), actor=superuser, reason=A_REASON, clock=stopped_clock)

    assert change.actor == superuser


@pytest.mark.django_db
@pytest.mark.parametrize("reason", ["", "   ", "\n\t"])
def test_a_blank_reason_is_refused_before_anything_is_written(
    a_leader: User, stopped_clock: FixedClock, reason: str
) -> None:
    """The matrix's `Add, blank reason` row.

    Args:
        a_leader: Who asks.
        stopped_clock: When.
        reason: The refused reason.

    """
    with pytest.raises(InventoryChangeError, match=r"needs a reason"):
        add_entry(source_package_key=A_KEY, row=_a_row(), actor=a_leader, reason=reason, clock=stopped_clock)

    assert InventoryEntry.objects.count() == 0
    assert InventoryChange.objects.count() == 0


@pytest.mark.django_db
def test_a_duplicate_key_is_refused_before_a_write(
    a_leader: User, stopped_clock: FixedClock, an_entry: InventoryEntry
) -> None:
    """The matrix's `Duplicate key` row: one row per key, and the second add leaves the first alone."""
    with pytest.raises(InventoryChangeError, match=r"cannot be added again"):
        add_entry(source_package_key=A_KEY, row=_a_row(apps=1), actor=a_leader, reason=A_REASON, clock=stopped_clock)

    an_entry.refresh_from_db()
    assert an_entry.apps == 148  # noqa: PLR2004 - the value the first add wrote
    assert InventoryChange.objects.count() == 1


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("row", "fragment"),
    [
        (_a_row(package_name=""), "no name"),
        (_a_row(package_name="   "), "no name"),
        (_a_row(package_name=" numpy"), "surrounding whitespace"),
        (_a_row(package_name="n" * 129), "column holds"),
        (_a_row(internal_component_count=None), "states no internal_component_count"),
        (_a_row(internal_lob_count=None), "states no internal_lob_count"),
        (_a_row(apps=-1), "not a count"),
        (_a_row(downloads=MAX_SIGNAL + 1), "not a count"),
        (_a_row(versions=True), "not a count"),
        (_a_row(platforms="4"), "not a count"),
    ],
    ids=[
        "blank name",
        "whitespace name",
        "unstripped name",
        "over-long name",
        "missing component count",
        "missing lob count",
        "negative",
        "over the ceiling",
        "a boolean",
        "a string",
    ],
)
def test_a_row_the_table_will_not_hold_is_refused_naming_the_key(
    a_leader: User, stopped_clock: FixedClock, row: InventoryRow, fragment: str
) -> None:
    """Every value refusal, before the first write, with the key in the message.

    Args:
        a_leader: Who asks.
        stopped_clock: When.
        row: The refused row.
        fragment: What the message must say.

    """
    with pytest.raises(InventoryChangeError, match=fragment) as refused:
        add_entry(source_package_key=A_KEY, row=row, actor=a_leader, reason=A_REASON, clock=stopped_clock)

    assert A_KEY in str(refused.value)
    assert InventoryEntry.objects.count() == 0


@pytest.mark.django_db
@pytest.mark.parametrize("key", ["", "   ", "k" * 513])
def test_a_key_the_table_will_not_hold_is_refused(a_leader: User, stopped_clock: FixedClock, key: str) -> None:
    """Blank, or wider than the column.

    Args:
        a_leader: Who asks.
        stopped_clock: When.
        key: The refused key.

    """
    with pytest.raises(InventoryChangeError):
        add_entry(source_package_key=key, row=_a_row(), actor=a_leader, reason=A_REASON, clock=stopped_clock)

    assert InventoryEntry.objects.count() == 0


@pytest.mark.django_db
def test_a_naive_clock_is_refused(a_leader: User) -> None:
    """`CPM-AD-26`: a clock answering a naive instant is a defect, not a timestamp."""

    class _Naive:
        """A stub: `FixedClock` refuses a naive instant itself, so this answers what no real clock would."""

        def now(self) -> datetime:
            return FIXED_INSTANT.replace(tzinfo=None)

    with pytest.raises(InventoryChangeError, match=r"naive"):
        add_entry(source_package_key=A_KEY, row=_a_row(), actor=a_leader, reason=A_REASON, clock=_Naive())

    assert InventoryEntry.objects.count() == 0


# ---------------------------------------------------------------------------
# Change.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_a_change_rewrites_the_row_and_records_prior_and_new(a_leader: User, an_entry: InventoryEntry) -> None:
    """The matrix's `Change` row: counts rewritten, `changed_at` advanced, both sides on the audit row."""
    later = FixedClock(instant=LATER_INSTANT)

    change = change_entry(
        source_package_key=A_KEY,
        row=_a_row(internal_component_count=400, apps=None, downloads=5),
        actor=a_leader,
        reason="recounted",
        clock=later,
    )

    an_entry.refresh_from_db()
    assert an_entry.internal_component_count == 400  # noqa: PLR2004 - the changed value
    assert an_entry.apps is None
    assert an_entry.downloads == 5  # noqa: PLR2004 - the changed value
    assert an_entry.changed_at == LATER_INSTANT
    assert an_entry.reason == "recounted"
    assert (change.prior_internal_component_count, change.new_internal_component_count) == (312, 400)
    assert (change.prior_apps, change.new_apps) == (148, None)
    assert (change.prior_downloads, change.new_downloads) == (None, 5)
    assert (change.prior_package_name, change.new_package_name) == ("numpy", "numpy")
    assert change.observed_at == LATER_INSTANT
    assert InventoryChange.objects.filter(entry=an_entry).count() == 2  # noqa: PLR2004 - the add and the change


@pytest.mark.django_db
def test_a_change_that_changes_nothing_is_refused_and_records_nothing(
    a_leader: User, an_entry: InventoryEntry, stopped_clock: FixedClock
) -> None:
    """A person re-submitting a row as it stands is told so; no audit row says nothing happened."""
    with pytest.raises(InventoryChangeError, match=r"nothing to change"):
        change_entry(source_package_key=A_KEY, row=_a_row(), actor=a_leader, reason="again", clock=stopped_clock)

    assert InventoryChange.objects.filter(entry=an_entry).count() == 1


@pytest.mark.django_db
def test_a_change_of_a_retired_row_reactivates_it_and_records_the_transition(
    a_leader: User, an_entry: InventoryEntry, stopped_clock: FixedClock
) -> None:
    """Naming a retired row is saying it is in the inventory: `retired_at` clears and the audit row says so."""
    retire_entry(source_package_key=A_KEY, actor=a_leader, reason="dropped", clock=stopped_clock)

    change = change_entry(
        source_package_key=A_KEY, row=_a_row(), actor=a_leader, reason="readopted", clock=stopped_clock
    )

    an_entry.refresh_from_db()
    assert an_entry.retired_at is None
    assert (change.prior_retired, change.new_retired) == (True, False)


@pytest.mark.django_db
def test_a_change_of_a_key_naming_nothing_is_refused_and_creates_nothing(
    a_leader: User, stopped_clock: FixedClock
) -> None:
    """Never a creation path: `add_entry` is the one way a row comes to exist."""
    with pytest.raises(InventoryEntryMissingError, match=r"nothing to change"):
        change_entry(source_package_key=A_KEY, row=_a_row(), actor=a_leader, reason=A_REASON, clock=stopped_clock)

    assert InventoryEntry.objects.count() == 0


# ---------------------------------------------------------------------------
# Retire.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_a_retirement_sets_the_column_keeps_the_values_and_records_the_transition(
    a_leader: User, an_entry: InventoryEntry
) -> None:
    """The matrix's `Retire` row: a column write, nothing deleted, the audit row's values equal on both sides."""
    later = FixedClock(instant=LATER_INSTANT)

    change = retire_entry(source_package_key=A_KEY, actor=a_leader, reason="no longer used", clock=later)

    an_entry.refresh_from_db()
    assert an_entry.retired_at == LATER_INSTANT
    assert an_entry.changed_at == LATER_INSTANT
    assert an_entry.internal_component_count == 312  # noqa: PLR2004 - unchanged by a retirement
    assert InventoryEntry.objects.count() == 1
    assert (change.prior_retired, change.new_retired) == (False, True)
    assert (change.prior_internal_component_count, change.new_internal_component_count) == (312, 312)
    assert change.reason == "no longer used"


@pytest.mark.django_db
def test_retiring_twice_is_refused(a_leader: User, an_entry: InventoryEntry, stopped_clock: FixedClock) -> None:
    """The matrix's `Retire twice` row."""
    retire_entry(source_package_key=A_KEY, actor=a_leader, reason="dropped", clock=stopped_clock)

    with pytest.raises(InventoryChangeError, match=r"already retired"):
        retire_entry(source_package_key=A_KEY, actor=a_leader, reason="dropped again", clock=stopped_clock)

    assert InventoryChange.objects.filter(entry=an_entry).count() == 2  # noqa: PLR2004 - the add and the one retirement


@pytest.mark.django_db
def test_retiring_a_key_naming_nothing_is_refused(a_leader: User, stopped_clock: FixedClock) -> None:
    """A stale key is a 404-shaped refusal, not a silent no-op."""
    with pytest.raises(InventoryEntryMissingError):
        retire_entry(source_package_key=A_KEY, actor=a_leader, reason="gone", clock=stopped_clock)


@pytest.mark.django_db
def test_a_retired_row_is_absent_from_the_adapters_document_and_the_next_ingestion_records_not_found(
    a_leader: User, stopped_clock: FixedClock
) -> None:
    """The matrix's `Retire` consequence, end to end, with no code of its own (`CPM-AD-25`).

    Two rows imported and ingested; one retired; the next ingestion through the
    database adapter sees one record, writes one `ok` snapshot, and derives one
    `not_found` for the package the table no longer names. The package row stays.
    """
    import_watchlist(
        [_a_record(A_KEY), _a_record(ANOTHER_KEY)], replace=False, actor=None, origin=AN_ORIGIN, clock=stopped_clock
    )
    with InventoryIngestionCollector(clock=stopped_clock, transport=DatabaseInventoryAdapter()) as collector:
        first = collector.sweep()
    assert first.state == RunState.SUCCEEDED
    assert Package.objects.count() == 2  # noqa: PLR2004 - the two rows

    retire_entry(source_package_key=ANOTHER_KEY, actor=a_leader, reason="dropped", clock=stopped_clock)

    assert [record["source_package_key"] for record in active_records()] == [A_KEY]
    later = FixedClock(instant=LATER_INSTANT)
    with InventoryIngestionCollector(clock=later, transport=DatabaseInventoryAdapter()) as collector:
        second = collector.sweep()
    assert second.state == RunState.SUCCEEDED
    assert Package.objects.count() == 2  # noqa: PLR2004 - nothing deleted
    absent = InventorySnapshot.objects.get(source_package_key=ANOTHER_KEY, observed_at=LATER_INSTANT)
    assert absent.state == OutcomeState.NOT_FOUND.value
    assert absent.package.associator_key == ANOTHER_KEY
    present = InventorySnapshot.objects.get(source_package_key=A_KEY, observed_at=LATER_INSTANT)
    assert present.state == OutcomeState.OK.value


# ---------------------------------------------------------------------------
# One transaction, made to fail on each side.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_an_audit_row_that_cannot_be_written_rolls_the_entry_back(
    a_leader: User, stopped_clock: FixedClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The entry and its audit row commit together or not at all (`CPM-AD-23`)."""

    def refusing(*args: object, **kwargs: object) -> object:
        message = "refused by the case, to see whether the entry survives its audit row"
        raise DatabaseError(message)

    monkeypatch.setattr(InventoryChange.objects, "create", refusing)

    with pytest.raises(DatabaseError, match=r"refused by the case"):
        add_entry(source_package_key=A_KEY, row=_a_row(), actor=a_leader, reason=A_REASON, clock=stopped_clock)

    assert InventoryEntry.objects.count() == 0


@pytest.mark.django_db
def test_an_entry_that_cannot_be_written_leaves_no_audit_row(
    a_leader: User, an_entry: InventoryEntry, stopped_clock: FixedClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other side: a refused `save` leaves no audit row claiming a change happened."""

    def refusing(self: InventoryEntry, *args: object, **kwargs: object) -> None:
        message = "refused by the case, to see whether the audit row survives its entry"
        raise DatabaseError(message)

    monkeypatch.setattr(InventoryEntry, "save", refusing)

    with pytest.raises(DatabaseError, match=r"refused by the case"):
        change_entry(
            source_package_key=A_KEY, row=_a_row(apps=1), actor=a_leader, reason="recounted", clock=stopped_clock
        )

    an_entry.refresh_from_db()
    assert an_entry.apps == 148  # noqa: PLR2004 - the value before the refused change
    assert InventoryChange.objects.filter(entry=an_entry).count() == 1


# ---------------------------------------------------------------------------
# Import: fresh, repeat, replace.
# ---------------------------------------------------------------------------


def _development_records() -> list[dict[str, str | int]]:
    """Return the shipped development watchlist, parsed.

    Returns:
        The records.

    """
    return records_from(THE_WATCHLIST.read_text(encoding=WATCHLIST_ENCODING), watchlist=THE_WATCHLIST)


@pytest.mark.django_db
def test_a_fresh_import_adds_every_row_with_an_unattended_audit_row_naming_the_file(stopped_clock: FixedClock) -> None:
    """The matrix's `Import fresh` row: the 148-row development file, 148 entries, 148 audit rows, `actor=None`."""
    records = _development_records()

    outcome = import_watchlist(records, replace=False, actor=None, origin=str(THE_WATCHLIST), clock=stopped_clock)

    assert outcome == ImportOutcome(added=len(records), changed=0, unchanged=0, retired=0, retired_kept=0)
    assert InventoryEntry.objects.count() == len(records)
    assert InventoryEntry.objects.filter(retired_at__isnull=False).count() == 0
    changes = InventoryChange.objects.all()
    assert changes.count() == len(records)
    assert set(changes.values_list("actor", flat=True)) == {None}
    assert set(changes.values_list("origin", flat=True)) == {str(THE_WATCHLIST)}
    assert set(changes.values_list("reason", flat=True)) == {f"imported from {THE_WATCHLIST}"}
    assert set(changes.values_list("observed_at", flat=True)) == {FIXED_INSTANT}


@pytest.mark.django_db
def test_a_repeated_import_writes_no_change_row_for_an_unchanged_entry_and_audits_a_changed_count(
    stopped_clock: FixedClock,
) -> None:
    """The matrix's `Import repeat` row."""
    import_watchlist(
        [_a_record(A_KEY), _a_record(ANOTHER_KEY)], replace=False, actor=None, origin=AN_ORIGIN, clock=stopped_clock
    )
    later = FixedClock(instant=LATER_INSTANT)

    outcome = import_watchlist(
        [_a_record(A_KEY), _a_record(ANOTHER_KEY, internal_lob_count=7)],
        replace=False,
        actor=None,
        origin=AN_ORIGIN,
        clock=later,
    )

    assert outcome == ImportOutcome(added=0, changed=1, unchanged=1, retired=0, retired_kept=0)
    assert InventoryChange.objects.count() == 3  # noqa: PLR2004 - two adds and one change
    unchanged = InventoryEntry.objects.get(source_package_key=A_KEY)
    assert unchanged.changed_at == FIXED_INSTANT
    changed = InventoryEntry.objects.get(source_package_key=ANOTHER_KEY)
    assert changed.changed_at == LATER_INSTANT
    audit = InventoryChange.objects.get(entry=changed, observed_at=LATER_INSTANT)
    assert (audit.prior_internal_lob_count, audit.new_internal_lob_count) == (2, 7)


@pytest.mark.django_db
def test_an_import_with_replace_retires_the_rows_the_file_no_longer_names_and_deletes_none(
    stopped_clock: FixedClock,
) -> None:
    """The matrix's `Import replace` row: retired with a reason naming the file; the rows stay."""
    import_watchlist(
        [_a_record(A_KEY), _a_record(ANOTHER_KEY), _a_record(A_THIRD_KEY)],
        replace=False,
        actor=None,
        origin=AN_ORIGIN,
        clock=stopped_clock,
    )
    later = FixedClock(instant=LATER_INSTANT)

    outcome = import_watchlist([_a_record(A_KEY)], replace=True, actor=None, origin=AN_ORIGIN, clock=later)

    assert outcome == ImportOutcome(added=0, changed=0, unchanged=1, retired=2, retired_kept=0)
    assert InventoryEntry.objects.count() == 3  # noqa: PLR2004 - nothing deleted
    retired = InventoryEntry.objects.filter(retired_at=LATER_INSTANT)
    assert set(retired.values_list("source_package_key", flat=True)) == {ANOTHER_KEY, A_THIRD_KEY}
    for entry in retired:
        assert AN_ORIGIN in entry.reason
        assert "--replace" in entry.reason
        audit = InventoryChange.objects.get(entry=entry, observed_at=LATER_INSTANT)
        assert (audit.prior_retired, audit.new_retired) == (False, True)
        assert audit.actor is None
        assert audit.origin == AN_ORIGIN
    assert [record["source_package_key"] for record in active_records()] == [A_KEY]


@pytest.mark.django_db
def test_an_import_without_replace_leaves_unnamed_rows_alone(stopped_clock: FixedClock) -> None:
    """A partial file is a partial file: rows it does not name are not retired."""
    import_watchlist(
        [_a_record(A_KEY), _a_record(ANOTHER_KEY)], replace=False, actor=None, origin=AN_ORIGIN, clock=stopped_clock
    )

    outcome = import_watchlist([_a_record(A_KEY)], replace=False, actor=None, origin=AN_ORIGIN, clock=stopped_clock)

    assert outcome == ImportOutcome(added=0, changed=0, unchanged=1, retired=0, retired_kept=0)
    assert InventoryEntry.objects.filter(retired_at__isnull=False).count() == 0


@pytest.mark.django_db
@pytest.mark.parametrize("replace", [False, True])
def test_an_import_never_reactivates_a_retired_row_and_counts_it_kept(
    a_leader: User,
    stopped_clock: FixedClock,
    replace: bool,  # noqa: FBT001 - a parametrized flag, as the service takes it
) -> None:
    """A person retired it with a reason; a file that still names it has not caught up.

    Left retired with or without `--replace`, counted `retired_kept`, no audit row
    written, and the person's reason still on the row.

    Args:
        a_leader: Who retired it.
        stopped_clock: When.
        replace: Whether the import replaces.

    """
    import_watchlist([_a_record(A_KEY)], replace=False, actor=None, origin=AN_ORIGIN, clock=stopped_clock)
    retire_entry(source_package_key=A_KEY, actor=a_leader, reason="dropped by a person", clock=stopped_clock)
    audit_rows = InventoryChange.objects.count()

    outcome = import_watchlist([_a_record(A_KEY)], replace=replace, actor=None, origin=AN_ORIGIN, clock=stopped_clock)

    assert outcome == ImportOutcome(added=0, changed=0, unchanged=0, retired=0, retired_kept=1)
    entry = InventoryEntry.objects.get(source_package_key=A_KEY)
    assert entry.retired_at is not None
    assert entry.reason == "dropped by a person"
    assert InventoryChange.objects.count() == audit_rows


@pytest.mark.django_db
def test_a_person_reactivates_a_retired_row_and_the_transition_is_audited(
    a_leader: User, stopped_clock: FixedClock
) -> None:
    """Reactivation is `change_entry`: a person, a reason, and `retired` True -> False on the audit row."""
    import_watchlist([_a_record(A_KEY)], replace=False, actor=None, origin=AN_ORIGIN, clock=stopped_clock)
    retire_entry(source_package_key=A_KEY, actor=a_leader, reason="dropped", clock=stopped_clock)

    change = change_entry(
        source_package_key=A_KEY,
        row=InventoryRow.from_record(_a_record(A_KEY)),
        actor=a_leader,
        reason="readopted",
        clock=stopped_clock,
    )

    assert InventoryEntry.objects.get(source_package_key=A_KEY).retired_at is None
    assert (change.prior_retired, change.new_retired) == (True, False)
    assert change.actor == a_leader


@pytest.mark.django_db
def test_an_import_by_a_person_is_permission_gated_and_names_them(
    a_leader: User, an_outsider: User, stopped_clock: FixedClock
) -> None:
    """An actor given is an actor checked; an actor admitted is the actor every audit row names."""
    with pytest.raises(InventoryNotPermittedError):
        import_watchlist([_a_record(A_KEY)], replace=False, actor=an_outsider, origin=AN_ORIGIN, clock=stopped_clock)
    assert InventoryEntry.objects.count() == 0

    import_watchlist([_a_record(A_KEY)], replace=False, actor=a_leader, origin=AN_ORIGIN, clock=stopped_clock)

    audit = InventoryChange.objects.get()
    assert audit.actor == a_leader
    # A person's write names the person, never the file as well: the reason
    # already names it, and the audit row carries exactly one author.
    assert audit.origin == ""
    assert AN_ORIGIN in audit.reason


@pytest.mark.django_db
def test_an_import_with_no_origin_is_refused(stopped_clock: FixedClock) -> None:
    """An unattended import's audit rows name the file in place of an actor, so a blank origin names nobody."""
    with pytest.raises(InventoryChangeError, match=r"origin"):
        import_watchlist([_a_record(A_KEY)], replace=False, actor=None, origin="  ", clock=stopped_clock)

    assert InventoryEntry.objects.count() == 0


@pytest.mark.django_db
def test_a_refused_record_leaves_the_rows_before_it_committed_and_names_the_key(stopped_clock: FixedClock) -> None:
    """One transaction per row (`CPM-AD-23`): the import stops at the bad row, and the message says which."""
    with pytest.raises(InventoryChangeError, match=ANOTHER_KEY):
        import_watchlist(
            [_a_record(A_KEY), _a_record(ANOTHER_KEY, apps=-1), _a_record(A_THIRD_KEY)],
            replace=False,
            actor=None,
            origin=AN_ORIGIN,
            clock=stopped_clock,
        )

    assert list(InventoryEntry.objects.values_list("source_package_key", flat=True)) == [A_KEY]
    assert InventoryChange.objects.count() == 1


# ---------------------------------------------------------------------------
# The database adapter: the CSV adapter's document, from the table.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_the_database_adapter_answers_exactly_the_documents_the_csv_adapter_answers(stopped_clock: FixedClock) -> None:
    """Parser-to-adapter equivalence: the shipped development file, through both, is one record list."""
    import_watchlist(_development_records(), replace=False, actor=None, origin=str(THE_WATCHLIST), clock=stopped_clock)

    from_file = WatchlistAdapter(path=THE_WATCHLIST).fetch(INVENTORY_SOURCE)
    from_table = DatabaseInventoryAdapter().fetch(INVENTORY_SOURCE)

    # The file's order is the file's; the table's is key order. Compared as
    # sorted lists of records, and then as the record contract reads them.
    by_key = lambda record: record["source_package_key"]  # noqa: E731 - a one-line key for two sorts
    assert sorted(json.loads(from_file.body), key=by_key) == sorted(json.loads(from_table.body), key=by_key)
    assert sorted(records_in(from_file), key=lambda r: r.source_package_key) == sorted(
        records_in(from_table), key=lambda r: r.source_package_key
    )
    assert from_table.source == INVENTORY_SOURCE
    assert from_table.found is True
    assert from_table.status_code is None


@pytest.mark.django_db
def test_an_empty_table_yields_the_empty_document_the_record_contract_refuses() -> None:
    """The matrix's `Adapter, empty table` row: refused as any empty inventory is, nothing marked absent."""
    payload = DatabaseInventoryAdapter().fetch(INVENTORY_SOURCE)

    assert json.loads(payload.body) == []
    with pytest.raises(InventoryRecordError, match=r"naming no packages"):
        records_in(payload)
    with (
        InventoryIngestionCollector(
            clock=FixedClock(instant=FIXED_INSTANT), transport=DatabaseInventoryAdapter()
        ) as collector,
        pytest.raises(InventoryRecordError),
    ):
        collector.sweep()
    assert InventorySnapshot.objects.count() == 0
    assert CollectionRun.objects.get(collector=COLLECTOR_NAME).status == RunState.FAILED.value


@pytest.mark.django_db
def test_the_database_adapter_ingests_into_an_empty_database_and_then_a_populated_one(
    stopped_clock: FixedClock,
) -> None:
    """The matrix's `Seed then ingest` row at the service level: no `IntegrityError`, one shell per row."""
    records = _development_records()
    import_watchlist(records, replace=False, actor=None, origin=str(THE_WATCHLIST), clock=stopped_clock)

    with InventoryIngestionCollector(clock=stopped_clock, transport=DatabaseInventoryAdapter()) as collector:
        first = collector.sweep()
    with InventoryIngestionCollector(
        clock=FixedClock(instant=LATER_INSTANT), transport=DatabaseInventoryAdapter()
    ) as collector:
        second = collector.sweep()

    assert first.state == RunState.SUCCEEDED
    assert second.state == RunState.SUCCEEDED
    assert Package.objects.count() == len(records)
    assert set(Package.objects.values_list("identity_source", flat=True)) == {COLLECTOR_NAME}
    assert set(Package.objects.values_list("associator_key", flat=True)) == {
        str(record["source_package_key"]) for record in records
    }
    assert InventorySnapshot.objects.count() == 2 * len(records)
    assert InventorySnapshot.objects.filter(state=OutcomeState.NOT_FOUND.value).count() == 0


# ---------------------------------------------------------------------------
# One active row per name (CPM-AD-3): refused at every door, enforced by the column.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_an_add_carrying_another_active_rows_name_is_refused_naming_that_key(
    a_leader: User, stopped_clock: FixedClock, an_entry: InventoryEntry
) -> None:
    """`pypi:numpy`/`numpy` beside `conda-forge/numpy`/`numpy` is the collision this story closes."""
    with pytest.raises(InventoryChangeError, match=A_KEY):
        add_entry(source_package_key="pypi:numpy", row=_a_row(), actor=a_leader, reason=A_REASON, clock=stopped_clock)

    assert InventoryEntry.objects.count() == 1


@pytest.mark.django_db
def test_a_rename_onto_another_active_rows_name_is_refused(
    a_leader: User, stopped_clock: FixedClock, an_entry: InventoryEntry
) -> None:
    """The same rule on `change_entry`, naming the other row's key."""
    add_entry(
        source_package_key=ANOTHER_KEY,
        row=_a_row(package_name="pandas"),
        actor=a_leader,
        reason=A_REASON,
        clock=stopped_clock,
    )

    with pytest.raises(InventoryChangeError, match=A_KEY):
        change_entry(
            source_package_key=ANOTHER_KEY,
            row=_a_row(package_name="numpy"),
            actor=a_leader,
            reason="x",
            clock=stopped_clock,
        )

    assert InventoryEntry.objects.get(source_package_key=ANOTHER_KEY).package_name == "pandas"


@pytest.mark.django_db
def test_a_reactivation_onto_a_name_another_active_row_took_meanwhile_is_refused(
    a_leader: User, stopped_clock: FixedClock, an_entry: InventoryEntry
) -> None:
    """Retire `numpy` under one key, add it under another, then try to bring the first back."""
    retire_entry(source_package_key=A_KEY, actor=a_leader, reason="moved", clock=stopped_clock)
    add_entry(source_package_key=ANOTHER_KEY, row=_a_row(), actor=a_leader, reason="re-keyed", clock=stopped_clock)

    with pytest.raises(InventoryChangeError, match=ANOTHER_KEY):
        change_entry(source_package_key=A_KEY, row=_a_row(), actor=a_leader, reason="back", clock=stopped_clock)

    assert InventoryEntry.objects.get(source_package_key=A_KEY).retired_at is not None


@pytest.mark.django_db
def test_an_import_naming_one_name_under_a_second_key_is_refused_naming_the_first(stopped_clock: FixedClock) -> None:
    """A file carrying `numpy` twice under two keys stops at the second, the first committed."""
    with pytest.raises(InventoryChangeError, match=A_KEY):
        import_watchlist(
            [_a_record(A_KEY, package_name="numpy"), _a_record(ANOTHER_KEY, package_name="numpy")],
            replace=False,
            actor=None,
            origin=AN_ORIGIN,
            clock=stopped_clock,
        )

    assert list(InventoryEntry.objects.values_list("source_package_key", flat=True)) == [A_KEY]


@pytest.mark.django_db
def test_a_retired_row_keeps_its_name_outside_the_rule(
    a_leader: User, stopped_clock: FixedClock, an_entry: InventoryEntry
) -> None:
    """Retire `numpy` under one key and add it under another: the constraint is over active rows only."""
    retire_entry(source_package_key=A_KEY, actor=a_leader, reason="moved", clock=stopped_clock)

    change = add_entry(
        source_package_key=ANOTHER_KEY, row=_a_row(), actor=a_leader, reason="re-keyed", clock=stopped_clock
    )

    assert change.entry.package_name == "numpy"
    assert InventoryEntry.objects.filter(package_name="numpy").count() == 2  # noqa: PLR2004 - one retired, one active


@pytest.mark.django_db
def test_the_database_refuses_a_second_active_row_with_one_name(an_entry: InventoryEntry) -> None:
    """The partial unique constraint, behind the service's check: a raw insert past the door is refused.

    Each refused insert sits in its own savepoint, so the case's transaction
    survives the refusal and the next statement can run.
    """
    with transaction.atomic(), pytest.raises(IntegrityError):
        InventoryEntry.objects.create(
            source_package_key=ANOTHER_KEY,
            package_name="numpy",
            internal_component_count=1,
            internal_lob_count=1,
            changed_at=FIXED_INSTANT,
            reason="a raw insert",
        )
    # Retired, the same name is one row the constraint does not cover.
    InventoryEntry.objects.create(
        source_package_key=A_THIRD_KEY,
        package_name="numpy",
        internal_component_count=1,
        internal_lob_count=1,
        changed_at=FIXED_INSTANT,
        retired_at=FIXED_INSTANT,
        reason="a raw insert, retired",
    )


@pytest.mark.django_db
def test_a_write_the_constraints_refuse_is_the_services_own_refusal_not_a_fault(
    a_leader: User, stopped_clock: FixedClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two concurrent adds of one key both find no row under the lock; the loser's insert is refused, not a 500."""

    def losing(self: InventoryEntry, *args: object, **kwargs: object) -> None:
        message = "UNIQUE constraint failed: inventory.source_package_key"
        raise IntegrityError(message)

    monkeypatch.setattr(InventoryEntry, "save", losing)

    with pytest.raises(InventoryChangeError, match=r"already holds a row"):
        add_entry(source_package_key=A_KEY, row=_a_row(), actor=a_leader, reason=A_REASON, clock=stopped_clock)

    assert InventoryEntry.objects.count() == 0
    assert InventoryChange.objects.count() == 0


# ---------------------------------------------------------------------------
# The audit row's own constraints, at the database.
# ---------------------------------------------------------------------------


def _an_audit_row(entry: InventoryEntry, **values: object) -> InventoryChange:
    """Return an unsaved audit row for an entry, with any column overridden.

    Args:
        entry: The entry.
        **values: Columns to set.

    Returns:
        The instance.

    """
    row = InventoryChange(observed_at=FIXED_INSTANT, entry=entry, new_package_name=entry.package_name, reason="x")
    for name, value in values.items():
        setattr(row, name, value)
    return row


@pytest.mark.django_db
def test_the_database_refuses_an_audit_row_with_no_reason(an_entry: InventoryEntry) -> None:
    """`INVENTORY_CHANGE_REASON_CONSTRAINT`: the service's refusal, enforced at the column."""
    with transaction.atomic(), pytest.raises(IntegrityError):
        _an_audit_row(an_entry, origin="a-file", reason="").save()


@pytest.mark.django_db
def test_the_database_refuses_an_audit_row_naming_neither_or_both_authors(
    an_entry: InventoryEntry, a_leader: User
) -> None:
    """`INVENTORY_CHANGE_AUTHOR_CONSTRAINT`: exactly one of a person and a file."""
    with transaction.atomic(), pytest.raises(IntegrityError):
        _an_audit_row(an_entry, actor=None, origin="").save()
    with transaction.atomic(), pytest.raises(IntegrityError):
        _an_audit_row(an_entry, actor=a_leader, origin="a-file").save()
    _an_audit_row(an_entry, actor=a_leader, origin="").save()
    _an_audit_row(an_entry, actor=None, origin="a-file").save()


# ---------------------------------------------------------------------------
# Import: the empty file, the composed reason, the origin's width.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_replace_over_no_records_is_refused_and_retires_nothing(stopped_clock: FixedClock) -> None:
    """An inventory naming nothing is indistinguishable from a broken source; --replace over it retires nothing."""
    import_watchlist([_a_record(A_KEY)], replace=False, actor=None, origin=AN_ORIGIN, clock=stopped_clock)

    with pytest.raises(InventoryChangeError, match=r"names no packages"):
        import_watchlist([], replace=True, actor=None, origin=AN_ORIGIN, clock=stopped_clock)

    assert InventoryEntry.objects.get(source_package_key=A_KEY).retired_at is None
    # Without `replace`, an empty file is an import of nothing and is not a refusal.
    assert import_watchlist([], replace=False, actor=None, origin=AN_ORIGIN, clock=stopped_clock) == ImportOutcome(
        added=0, changed=0, unchanged=0, retired=0, retired_kept=0
    )


@pytest.mark.django_db
def test_a_given_reason_reaches_the_replace_retirements_composed_with_the_derived_clause(
    stopped_clock: FixedClock,
) -> None:
    """`--reason` is why; the derived clause says the file dropped it; a retired row carries both."""
    import_watchlist(
        [_a_record(A_KEY), _a_record(ANOTHER_KEY)], replace=False, actor=None, origin=AN_ORIGIN, clock=stopped_clock
    )

    import_watchlist(
        [_a_record(A_KEY)], replace=True, actor=None, origin=AN_ORIGIN, clock=stopped_clock, reason="quarterly review"
    )

    retired = InventoryEntry.objects.get(source_package_key=ANOTHER_KEY)
    assert retired.retired_at is not None
    assert retired.reason.startswith("quarterly review; absent from ")
    assert AN_ORIGIN in retired.reason
    assert "--replace" in retired.reason
    assert InventoryChange.objects.get(entry=retired, new_retired=True).reason == retired.reason


@pytest.mark.django_db
def test_an_origin_wider_than_the_audit_column_is_refused_before_any_write(stopped_clock: FixedClock) -> None:
    """PostgreSQL would refuse the audit row after the entry had been written; the import refuses first."""
    with pytest.raises(InventoryChangeError, match=r"origin column holds"):
        import_watchlist([_a_record(A_KEY)], replace=False, actor=None, origin="/" + "p" * 600, clock=stopped_clock)

    assert InventoryEntry.objects.count() == 0
