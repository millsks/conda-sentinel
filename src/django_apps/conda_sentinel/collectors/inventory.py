"""The one door that changes the inventory table: add, change, retire, import.

`CPM-OPERATE-S03` moves the watchlist into `collectors.InventoryEntry`, and
`CPM-FR-3` as amended names the inventory the *second* governed human write in
the product -- so this module carries the three obligations `CPM-AD-14` puts on
the first (`identity/services.py`'s `override_identity`), and it is shaped after
that door on purpose: it requires a permission, it requires a reason, and it
writes an audit row **in the same transaction** as the change.

**Four callers, one write path.** `add_entry`, `change_entry` and `retire_entry`
are the surface's three forms; `import_watchlist` is the command's and the demo
seeder's. All four reach the table through `_apply`, which holds the module's
one `transaction.atomic()` -- `tests/unit/django_apps/test_inventory_service.py`
asserts structurally that there is exactly one and that both writes sit inside it.
Every row is one transaction (`CPM-AD-23`): an import of a thousand rows that
fails on the tenth leaves nine committed, each with its audit row, and the
refusal names the tenth.

**Every refusal precedes the first write.** The permission, the reason, the
values -- a blank name, a missing required count, a count the column will not
hold -- are all checked before the block opens, so a refused change leaves
nothing behind. Inside the block the entry is read under `select_for_update()`,
for the reason `override_identity` reads its package there: the audit row
records the values this change replaced, so the row it reads them from has to
be the row it then writes.

**Retirement is a column write, never a delete.** `retire_entry` sets
`retired_at`; the database adapter answers active rows only; the next ingestion
observes the package absent and writes its `not_found` snapshot (`CPM-AD-25`).
`import_watchlist(replace=True)` retires every active row the file no longer
names the same way. Nothing here calls `delete()` or a manager `update()`, and
`tests/unit/django_apps/test_mutation_path_audit.py` is what keeps it that way:
every write is an instance `save(update_fields=...)` or an insert.

**A change that changes nothing is refused for a person and skipped for a
file.** A person re-submitting a row as it stands is told so, because an audit
row saying "nothing changed" is not a decision worth recording; a file that
names a row as it already is is the ordinary case of a repeated import, and it
writes no audit row for that entry. `changed_at` therefore means what it says:
the last time something about the row was different.

**An import never reactivates a retired row; a person does.** A row somebody
retired on the page, with a reason, is a decision; a file that still names it is
a file that has not caught up, and an import that quietly un-retired it under the
import's reason would overwrite a person's decision with a default. So the import
counts those `retired_kept` and leaves them, and reactivation is `change_entry`
-- a person, a reason, and an audit row recording the `retired` transition.

**One active row per name, checked under the lock and enforced by the column.**
The name becomes `Package.canonical_name`, which is unique and create-only, so
two active rows carrying one name are two shells the next ingestion cannot both
create -- the `IntegrityError` this story exists to close, arriving one step
later. Every door refuses a name another active row carries, naming that row's
key, and `INVENTORY_ACTIVE_NAME_CONSTRAINT` refuses what a race gets past the
check; the constraint's `IntegrityError` is caught inside the block and re-raised
as the same refusal, so a losing concurrent add is a 400 and not a 500.

**The permission is checked here, and here is where it has to be.** The surface
gates its view on the leadership *role* (`CPM-AD-13`); this module gates the
write on the Django *permission* `core/roles.py` declares, so that no second
caller -- a shell, a future command -- reaches the table ungated. An unattended
import passes `actor=None` and is not permission-gated: the command line is the
gate, as it is for every admin process, and then `origin` names the file in
every audit row it writes.

**On the `AD-` prefix.** A bare `AD-n` in this repository is an *inherited*
platform decision; a decision from this product's own architecture spine always
carries the `CPM-` prefix.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import fields
from typing import TYPE_CHECKING
from typing import Final
from typing import Literal
from typing import cast

import structlog
from django.db import IntegrityError
from django.db import transaction

from conda_sentinel.collectors.models import INVENTORY_KEY_FIELD
from conda_sentinel.collectors.models import INVENTORY_NAME_FIELD
from conda_sentinel.collectors.models import INVENTORY_OPTIONAL_SIGNALS
from conda_sentinel.collectors.models import INVENTORY_REQUIRED_SIGNALS
from conda_sentinel.collectors.models import INVENTORY_SIGNALS
from conda_sentinel.collectors.models import InventoryChange
from conda_sentinel.collectors.models import InventoryEntry
from conda_sentinel.core.clock import is_aware
from conda_sentinel.core.ledger import current_trace_id
from conda_sentinel.core.roles import INVENTORY_CHANGE_PERMISSION

if TYPE_CHECKING:
    from collections.abc import Iterable
    from collections.abc import Mapping
    from datetime import datetime

    from conda_sentinel.core.clock import Clock
    from django_service.users.models import User

__all__ = [
    "IDENTITY_FIELD",
    "INVENTORY_CHANGED_EVENT",
    "INVENTORY_IMPORTED_EVENT",
    "INVENTORY_PERMISSION_MISSING",
    "INVENTORY_REFUSED_EVENT",
    "MAX_SIGNAL",
    "ImportOutcome",
    "InventoryChangeError",
    "InventoryEntryMissingError",
    "InventoryNotPermittedError",
    "InventoryRow",
    "add_entry",
    "change_entry",
    "import_watchlist",
    "retire_entry",
]

logger: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

#: The events this door emits, on the shape `identity/services.py` set for the
#: override: `authorization.`-prefixed because they are authorization decisions,
#: both directions recorded because the accepted ones are what an auditor asks
#: about first, and the reason a refusal carries drawn from a constant.
INVENTORY_REFUSED_EVENT: Final[str] = "authorization.inventory_change_refused"
INVENTORY_CHANGED_EVENT: Final[str] = "authorization.inventory_changed"
INVENTORY_IMPORTED_EVENT: Final[str] = "inventory.watchlist_imported"
INVENTORY_PERMISSION_MISSING: Final[str] = "inventory_permission_missing"

#: The attribute the acting identity is read from, matching
#: `identity/services.py`'s `IDENTITY_FIELD` and read through `getattr` with a
#: default for the reason given there: `AnonymousUser` does not declare it, and an
#: `AttributeError` raised while logging a refusal would lose the refusal.
IDENTITY_FIELD: Final[str] = "idp_subject"

#: The largest count a usage signal may carry: `PositiveIntegerField`'s ceiling.
#: Restated here rather than imported from `collectors/tasks.py` (which reaches
#: the transport a request may not) or `collectors/watchlist.py` (which imports
#: the same module), and reconciled against both by test.
MAX_SIGNAL: Final[int] = 2_147_483_647


def _column_length(field: str) -> int:
    """Return how wide one of the entry's columns is, read off the model rather than restated.

    `getattr` with a default, on `identity/services.py`'s terms: `get_field` is
    annotated as returning any of three field kinds, two of which have no
    `max_length`, and a `cast` would assert something about the schema this
    function is trying to read. `tests/unit/django_apps/test_inventory_service.py`
    reconciles both constants below against their fields.

    Args:
        field: The column's name.

    Returns:
        Its `max_length`, or `0` when it declares none.

    """
    return int(
        getattr(
            InventoryEntry._meta.get_field(field),  # noqa: SLF001 - `_meta` is Django's own public-by-convention API
            "max_length",
            0,
        )
        or 0,
    )


#: How wide the two text columns are, read off the model rather than restated.
_KEY_LENGTH: Final[int] = _column_length(INVENTORY_KEY_FIELD)
_NAME_LENGTH: Final[int] = _column_length(INVENTORY_NAME_FIELD)

#: How wide the audit row's `origin` column is, read off its model for the same
#: reason: an import naming a path wider than it would be refused by PostgreSQL
#: after the row it audits had been written, inside the block, as a bare
#: `DataError`.
_ORIGIN_LENGTH: Final[int] = int(
    getattr(
        InventoryChange._meta.get_field("origin"),  # noqa: SLF001 - `_meta` is Django's own public-by-convention API
        "max_length",
        0,
    )
    or 0,
)

#: The two columns every change stamps beside whatever else it wrote.
_STAMPED: Final[tuple[str, ...]] = ("changed_at", "reason")

#: What the audit row calls each kind of change, in its log line.
_ADDED: Final[str] = "added"
_CHANGED: Final[str] = "changed"
_RETIRED: Final[str] = "retired"


class InventoryChangeError(ValueError):
    """A change to the inventory was refused.

    One type for every refusal about the *change* -- a blank reason, a blank
    name, a missing required count, a count the column will not hold, a key
    added twice, a retirement of a row already retired, a change that changes
    nothing -- on the terms `identity/services.py`'s `OverrideError` states: the
    detail is in the message, and a caller branches on the two subclasses below
    and on nothing else.
    """


class InventoryNotPermittedError(InventoryChangeError):
    """The actor does not hold `INVENTORY_CHANGE_PERMISSION`.

    The refusal about *who asked* rather than what they asked for, which is why
    it is its own type: the surface answers it 403 and every other
    `InventoryChangeError` 400. A subclass, so a caller catching the parent
    still catches this.
    """


class InventoryEntryMissingError(InventoryChangeError):
    """The key names no entry.

    A change or a retirement of a row that does not exist. Its own type so the
    surface can answer a stale form with 404 rather than telling the person their
    values were bad -- and never a creation path: `add_entry` is the one way a
    row comes to exist, and it says so by name.
    """


@dataclass(frozen=True, slots=True)
class InventoryRow:
    """What one inventory row states, as a person or a file supplies it.

    Frozen, so a row cannot be edited between being checked and being written.
    The key is deliberately not here: it is what a row is *found by*, and the
    three doors take it as their own keyword so that a caller cannot change it
    by supplying a different one in the values.

    Attributes:
        package_name: What the package is called.
        internal_component_count: How many internal components use it. Required
            by the service, nullable in the column.
        internal_lob_count: How many internal lines of business use it. Required
            on the same terms.
        apps: How many applications name it, or `None` where nothing said.
        platforms: How many platforms it is used on, or `None`.
        downloads: How many internal downloads it has, or `None`.
        versions: How many versions of it are in use, or `None`.

    """

    package_name: str
    internal_component_count: int | None = None
    internal_lob_count: int | None = None
    apps: int | None = None
    platforms: int | None = None
    downloads: int | None = None
    versions: int | None = None

    @classmethod
    def from_record(cls, record: Mapping[str, object]) -> InventoryRow:
        """Build a row from one record as `collectors/watchlist.py`'s `records_from` yields it.

        Args:
            record: One parsed watchlist row: the required columns present, an
                optional column present only where the file stated it.

        Returns:
            The row. Nothing is checked here -- `_require_row` does that on the
            way in -- so a malformed mapping is refused by the door it reaches
            rather than by a constructor with no key to name.

        """
        # Asserted rather than checked: `_require_row` verifies every value on
        # the way into a door, so a mapping carrying something that is not a
        # count is refused there, naming the key, rather than here with none.
        values = cast("dict[str, int | None]", {name: record.get(name) for name in INVENTORY_SIGNALS})
        return cls(package_name=str(record.get(INVENTORY_NAME_FIELD, "")), **values)

    def as_columns(self) -> dict[str, object]:
        """Return the row as the entry's columns, by name.

        Returns:
            One entry per column this row supplies a value for, in field order.

        """
        return {field.name: getattr(self, field.name) for field in fields(self)}


@dataclass(frozen=True, slots=True)
class ImportOutcome:
    """What one import did, counted.

    Attributes:
        added: Rows the file named that the table did not hold.
        changed: Rows the file named with different values, or named while they
            were retired -- each reactivated and audited.
        unchanged: Rows the file named exactly as the table already held them.
            No audit row is written for these.
        retired: Active rows the file did not name, retired because `replace`
            was asked for. Always zero without it.
        retired_kept: Rows the file named that were retired and stay retired --
            an import never reactivates; a person does, through `change_entry`.

    """

    added: int
    changed: int
    unchanged: int
    retired: int
    retired_kept: int


@dataclass(frozen=True, slots=True)
class _Applied:
    """What one pass through `_apply` did, for the door that asked.

    Attributes:
        change: The audit row, or `None` when nothing was written.
        created: Whether the row was created rather than found.
        kept_retired: Whether a retired row was found and, this being an import,
            left as it was.

    """

    change: InventoryChange | None
    created: bool = False
    kept_retired: bool = False


def add_entry(*, source_package_key: str, row: InventoryRow, actor: User, reason: str, clock: Clock) -> InventoryChange:
    """Add one package to the inventory as a person, and record who did it and why.

    Args:
        source_package_key: What the inventory files the package under. Refused
            when blank, wider than its column, or already a row's.
        row: What the row states. Refused when the name is blank or too wide, a
            required count is missing, or any count is not one the column holds.
        actor: The person adding it. Must hold `INVENTORY_CHANGE_PERMISSION`.
        reason: Why. Refused when blank.
        clock: The clock the audit row's instant and the entry's `changed_at` are
            read from (`CPM-AD-26`).

    Returns:
        The audit row, with `entry` carrying the new row.

    Raises:
        InventoryNotPermittedError: When the actor does not hold the permission.
        InventoryChangeError: For every other refusal, all before the first write
            -- including a name another active row already carries, which the
            message names by key.

    """
    _require_permitted(actor)
    justification = _require_reason(reason)
    key = _require_key(source_package_key)
    _require_row(row, key=key)
    decided_at = _require_decided_at(clock.now())
    change = _apply(
        key, mode="add", values=row.as_columns(), actor=actor, origin="", reason=justification, at=decided_at
    ).change
    if change is None:
        # Unreachable by construction -- an add either creates or refuses -- and
        # refused rather than asserted so a defect in `_apply` cannot surface as
        # an `AssertionError` out of a surface.
        message = f"adding {key!r} to the inventory wrote nothing, which is a defect in collectors/inventory.py."
        raise InventoryChangeError(message)
    _log_changed(_ADDED, change, actor=actor)
    return change


def change_entry(
    *, source_package_key: str, row: InventoryRow, actor: User, reason: str, clock: Clock
) -> InventoryChange:
    """Change what one inventory row states, as a person, and record the prior and new values.

    A retired row named here is **reactivated**: the person is saying the package
    is in the inventory, and the audit row records `prior_retired=True,
    new_retired=False` beside whatever else changed.

    Args:
        source_package_key: The row to change. Refused when it names none.
        row: What the row should state, in full.
        actor: The person changing it. Must hold `INVENTORY_CHANGE_PERMISSION`.
        reason: Why. Refused when blank.
        clock: The clock the instants are read from.

    Returns:
        The audit row.

    Raises:
        InventoryNotPermittedError: When the actor does not hold the permission.
        InventoryEntryMissingError: When the key names no row.
        InventoryChangeError: For every other refusal -- including a change that
            changes nothing, which is refused rather than recorded.

    """
    _require_permitted(actor)
    justification = _require_reason(reason)
    key = _require_key(source_package_key)
    _require_row(row, key=key)
    decided_at = _require_decided_at(clock.now())
    change = _apply(
        key, mode="change", values=row.as_columns(), actor=actor, origin="", reason=justification, at=decided_at
    ).change
    if change is None:
        message = (
            f"the inventory row {key!r} already states exactly these values and is active, so there is nothing "
            f"to change. An audit row recording that nothing changed would answer the question it exists to "
            f"answer with nothing (CPM-AD-14)."
        )
        raise InventoryChangeError(message)
    _log_changed(_CHANGED, change, actor=actor)
    return change


def retire_entry(*, source_package_key: str, actor: User, reason: str, clock: Clock) -> InventoryChange:
    """Retire one inventory row as a person: a column write the next ingestion observes as absence.

    Args:
        source_package_key: The row to retire. Refused when it names none, or
            when the row is already retired.
        actor: The person retiring it. Must hold `INVENTORY_CHANGE_PERMISSION`.
        reason: Why. Refused when blank.
        clock: The clock `retired_at`, `changed_at` and the audit instant are
            read from.

    Returns:
        The audit row, its values the same on both sides and `new_retired=True`.

    Raises:
        InventoryNotPermittedError: When the actor does not hold the permission.
        InventoryEntryMissingError: When the key names no row.
        InventoryChangeError: When the row is already retired, or the reason is
            blank.

    """
    _require_permitted(actor)
    justification = _require_reason(reason)
    key = _require_key(source_package_key)
    decided_at = _require_decided_at(clock.now())
    change = _apply(key, mode="retire", values={}, actor=actor, origin="", reason=justification, at=decided_at).change
    if change is None:
        # `_apply` answers `None` for "nothing differed", and for a retirement the
        # only way nothing differs is a row that was already retired.
        message = (
            f"the inventory row {key!r} is already retired, so there is nothing to retire. Retirement is a "
            f"column write (CPM-AD-25); a second one would record a decision nobody made."
        )
        raise InventoryChangeError(message)
    _log_changed(_RETIRED, change, actor=actor)
    return change


def import_watchlist(  # noqa: PLR0913 - one keyword per fact an import states; a bundle would hide them
    records: Iterable[Mapping[str, object]],
    *,
    replace: bool,
    actor: User | None,
    origin: str,
    clock: Clock,
    reason: str = "",
) -> ImportOutcome:
    """Bring the table into line with a parsed watchlist, one audited transaction per row.

    Args:
        records: The rows, as `collectors/watchlist.py`'s `records_from` yields
            them -- every refusal about the *file* has already been made by the
            time they reach here, so the only refusals left are the column
            bounds this module applies to any row.
        replace: When true, every active row the file does not name is retired
            afterwards, with a reason naming the file. When false, rows the file
            does not name are left as they are. A file naming nothing at all is
            refused with `replace`: an inventory of nothing is indistinguishable
            from a source that broke, and retiring every row off the back of it
            would be the same disaster the empty-document refusal exists for.
        actor: The person importing, or `None` for an unattended import from the
            command line. A person must hold `INVENTORY_CHANGE_PERMISSION`; an
            unattended import is not permission-gated, and its audit rows carry
            `actor=NULL` and `origin`.
        origin: What the rows came from -- the file's path. Recorded on every
            audit row and named in the derived reasons. Refused when blank.
        clock: The clock every instant is read from.
        reason: Why, on every row this import touches. Blank derives one from
            the origin, which is what an unattended import has to say. A given
            reason is composed with the derived clause on a `replace`
            retirement, so the row says both why and that the file dropped it.

    Returns:
        What the import did, counted.

    Raises:
        InventoryNotPermittedError: When an actor is given and lacks the
            permission.
        InventoryChangeError: When the origin is blank or wider than its
            column, when `replace` is asked for over no records, or when a record
            carries a value the column will not hold or a name another active row
            carries. Rows before the refused one stay committed, each with its
            audit row; the message names the key.

    """
    if actor is not None:
        _require_permitted(actor)
    source = _require_origin(origin)
    stated = reason.strip()
    justification = stated or f"imported from {source}"
    decided_at = _require_decided_at(clock.now())
    rows = list(records)
    if replace and not rows:
        message = (
            f"{source} names no packages at all, and --replace would retire every active row off the back of it. "
            f"An inventory naming nothing is indistinguishable from a source that has broken, so it is refused "
            f"rather than imported (CPM-FR-42, CPM-AD-25)."
        )
        raise InventoryChangeError(message)

    added = changed = unchanged = retired_kept = 0
    named: set[str] = set()
    for record in rows:
        key = _require_key(str(record.get(INVENTORY_KEY_FIELD, "")))
        row = InventoryRow.from_record(record)
        _require_row(row, key=key)
        named.add(key)
        applied = _apply(
            key,
            mode="import",
            values=row.as_columns(),
            actor=actor,
            origin=source,
            reason=justification,
            at=decided_at,
        )
        if applied.kept_retired:
            retired_kept += 1
        elif applied.change is None:
            unchanged += 1
        elif applied.created:
            added += 1
        else:
            changed += 1

    retired = 0
    if replace:
        dropped = f"absent from {source}, imported with --replace"
        retired = _retire_unnamed(
            named, actor=actor, origin=source, reason=f"{stated}; {dropped}" if stated else dropped, at=decided_at
        )

    logger.info(
        INVENTORY_IMPORTED_EVENT,
        origin=source,
        replace=replace,
        user_id=getattr(actor, "pk", None),
        added=added,
        changed=changed,
        unchanged=unchanged,
        retired=retired,
        retired_kept=retired_kept,
    )
    return ImportOutcome(added=added, changed=changed, unchanged=unchanged, retired=retired, retired_kept=retired_kept)


def _require_origin(origin: str) -> str:
    """Refuse an origin that names nothing or that the audit row's column will not hold.

    Args:
        origin: What the caller supplied.

    Returns:
        The origin stripped.

    Raises:
        InventoryChangeError: When it is blank, or wider than `InventoryChange.origin`.

    """
    source = origin.strip()
    if not source:
        message = (
            "an inventory import must name where its rows came from; the origin is what every audit row an "
            "unattended import writes names in place of an actor (CPM-AD-14)."
        )
        raise InventoryChangeError(message)
    if len(source) > _ORIGIN_LENGTH:
        message = (
            f"the origin {source[:40]!r}... is {len(source)} characters and the audit row's origin column holds "
            f"{_ORIGIN_LENGTH}; PostgreSQL would refuse the audit row after the change it records had been "
            f"written, so the import is refused before either."
        )
        raise InventoryChangeError(message)
    return source


def _retire_unnamed(named: set[str], *, actor: User | None, origin: str, reason: str, at: datetime) -> int:
    """Retire every active row a `replace` import did not name, one transaction each.

    The difference is taken here rather than as an `exclude(key__in=named)`: a
    watchlist is hundreds of keys and SQLite caps the variables one statement may
    carry, so the query reads every active key and Python subtracts the file's.

    Args:
        named: Every key the file carried.
        actor: Who, or `None`.
        origin: The file.
        reason: Why, already composed.
        at: The instant every stamp carries.

    Returns:
        How many rows were retired.

    """
    active = InventoryEntry.objects.filter(retired_at__isnull=True).values_list(INVENTORY_KEY_FIELD, flat=True)
    retired = 0
    for key in sorted(set(active) - named):
        if _apply(key, mode="retire", values={}, actor=actor, origin=origin, reason=reason, at=at).change:
            retired += 1
    return retired


def _apply(  # noqa: PLR0913 - one keyword per stamp the audit row carries; a bundle would hide them
    key: str,
    *,
    mode: Literal["add", "change", "retire", "import"],
    values: Mapping[str, object],
    actor: User | None,
    origin: str,
    reason: str,
    at: datetime,
) -> _Applied:
    """Write one row and its audit row together, or write nothing.

    The module's one `transaction.atomic()` (`CPM-AD-23`), and every door comes
    through it. The read is *inside* the block under `select_for_update()` for
    the reason `override_identity` gives: the audit row records the values this
    change replaced, so the row it reads them from has to be the row it then
    writes; read outside the block, two concurrent changes of one row would both
    record the same priors and the second row would be wrong about the thing it
    exists to be right about. PostgreSQL takes the row lock; SQLite has none and
    Django emits nothing for it there, which the gate proves against PostgreSQL
    on the terms every other database guarantee here is proven.

    **The name check is inside the block too, and the constraint stands behind
    it.** Two concurrent adds of one key both read no row under the lock -- there
    is no row to lock -- and the loser's insert is what the unique constraints
    refuse. That `IntegrityError` is caught in `_save` and re-raised as the
    refusal the check would have made, so a surface answers it as a bad request
    rather than a fault.

    Args:
        key: The row's key, already validated.
        mode: What the caller means. `add` creates and refuses a row already
            there; `change` rewrites, reactivates a retired row, and refuses a
            key naming nothing; `retire` sets `retired_at` and refuses a key
            naming nothing; `import` creates a missing row, rewrites an active
            one, and leaves a retired one exactly as it was.
        values: What the row should state, by column, already validated -- empty
            for a retirement, which changes no value.
        actor: Who, or `None` for an unattended import.
        origin: The file, for an import; blank for a person.
        reason: Why, already non-blank.
        at: The instant every stamp carries.

    Returns:
        What was done: the audit row, or `None` when nothing differed -- a change
        to a row that already states these values and is active, a retirement of
        a row already retired, or an import naming a retired row.

    Raises:
        InventoryChangeError: When `add` finds the key already a row's, when the
            name is another active row's, or when the database refuses the write
            on either unique constraint.
        InventoryEntryMissingError: When `change` or `retire` finds no row.

    """
    with transaction.atomic():
        entry = InventoryEntry.objects.select_for_update().filter(source_package_key=key).first()
        if entry is None:
            if mode in ("change", "retire"):
                message = (
                    f"no inventory row is keyed {key!r}, so there is nothing to change or retire. A row comes to "
                    f"exist through add_entry or import_watchlist and through nothing else."
                )
                raise InventoryEntryMissingError(message)
            _require_name_unclaimed(str(values[INVENTORY_NAME_FIELD]), key=key)
            entry = InventoryEntry(source_package_key=key, changed_at=at, reason=reason, **values)
            _save(entry, key=key)
            audit = _audit(entry, prior=None, actor=actor, origin=origin, reason=reason, at=at)
            return _Applied(change=audit, created=True)
        if mode == "add":
            message = (
                f"the inventory already holds a row for {key!r}, so it cannot be added again. One row per key "
                f"(CPM-AD-7); change the row it has, or retire it."
            )
            raise InventoryChangeError(message)
        if mode == "import" and entry.retired_at is not None:
            # A person retired it, with a reason. A file that still names it has
            # not caught up; reactivation is a person's decision (`change_entry`).
            return _Applied(change=None, kept_retired=True)
        written = _difference(entry, mode=mode, values=values, at=at)
        if not written:
            return _Applied(change=None)
        prior = {name: getattr(entry, name) for name in (INVENTORY_NAME_FIELD, *INVENTORY_SIGNALS, "retired_at")}
        if mode != "retire" and (INVENTORY_NAME_FIELD in written or "retired_at" in written):
            # A renamed row, or a reactivated one, takes a name among the active
            # rows; the check is the same one an add makes.
            _require_name_unclaimed(str(values[INVENTORY_NAME_FIELD]), key=key)
        for name, value in written.items():
            setattr(entry, name, value)
        entry.changed_at = at
        entry.reason = reason
        _save(entry, key=key, update_fields=[*sorted(written), *_STAMPED])
        return _Applied(change=_audit(entry, prior=prior, actor=actor, origin=origin, reason=reason, at=at))


def _difference(entry: InventoryEntry, *, mode: str, values: Mapping[str, object], at: datetime) -> dict[str, object]:
    """Return what a change would write on a row that exists, by column.

    Args:
        entry: The row as it stands, locked.
        mode: What the caller means -- `retire` sets `retired_at`; anything else
            rewrites the values and, on a retired row, clears it.
        values: What the row should state.
        at: The instant a retirement carries.

    Returns:
        The columns that differ. Empty when nothing would change.

    """
    written: dict[str, object] = {name: value for name, value in values.items() if getattr(entry, name) != value}
    if mode == "retire":
        if entry.retired_at is None:
            written["retired_at"] = at
    elif entry.retired_at is not None:
        # A row named by a person is in the inventory: a retired row is
        # reactivated, and the audit row records the transition.
        written["retired_at"] = None
    return written


def _save(entry: InventoryEntry, *, key: str, update_fields: list[str] | None = None) -> None:
    """Save the row, turning a unique-constraint refusal into the service's own.

    Args:
        entry: The row to write.
        key: Its key, for the message.
        update_fields: The columns to write, or `None` for an insert.

    Raises:
        InventoryChangeError: When the database refuses the write on the key or
            the active-name constraint -- the race the in-block check cannot
            close, because two concurrent adds of one key both find no row to
            lock. Re-raised as the refusal the check would have made; the
            enclosing transaction is rolled back by the exception leaving it.

    """
    try:
        entry.save(update_fields=update_fields)
    except IntegrityError as refused:
        message = (
            f"the inventory already holds a row for {key!r}, or an active row carrying the name "
            f"{entry.package_name!r}, so this write is refused: {refused}. One row per key and one active row "
            f"per name (CPM-AD-7); change the row it has, or retire it."
        )
        raise InventoryChangeError(message) from refused


def _require_name_unclaimed(name: str, *, key: str) -> None:
    """Refuse a name another active row already carries, naming that row's key.

    Called inside the block, after the lock, so the answer is about the table as
    it stands. The constraint behind it catches what a concurrent writer gets
    past this read.

    Args:
        name: The name the row would carry.
        key: The row's own key, excluded so a row keeping its name is not a
            collision with itself.

    Raises:
        InventoryChangeError: When another active row carries the name.

    """
    other = (
        InventoryEntry.objects.filter(package_name=name, retired_at__isnull=True)
        .exclude(source_package_key=key)
        .values_list(INVENTORY_KEY_FIELD, flat=True)
        .first()
    )
    if other is None:
        return
    message = (
        f"the inventory already has an active row named {name!r}, keyed {other!r}, so {key!r} cannot carry that "
        f"name. The name becomes the package's canonical name, which is unique (CPM-AD-3): two active rows with "
        f"one name are two shells the next ingestion cannot both create. Retire {other!r} first, or name this row "
        f"differently."
    )
    raise InventoryChangeError(message)


def _audit(  # noqa: PLR0913 - one keyword per stamp the audit row carries; a bundle would hide them
    entry: InventoryEntry,
    *,
    prior: Mapping[str, object] | None,
    actor: User | None,
    origin: str,
    reason: str,
    at: datetime,
) -> InventoryChange:
    """Insert the audit row for a change just written, inside the caller's transaction.

    Args:
        entry: The row as it now stands.
        prior: What it stated before, by column, or `None` for an add -- which
            records blank priors: empty name, NULL counts, not retired.
        actor: Who, or `None`.
        origin: The file, or blank. Recorded only for an unattended write.
        reason: Why.
        at: The instant the row carries as `observed_at`.

    Returns:
        The saved audit row.

    """
    before = prior or {}
    # A person's write names the person; an unattended one names the file. Never
    # both: an import a person runs is that person's decision, and its reason
    # already names the file (`INVENTORY_CHANGE_AUTHOR_CONSTRAINT`).
    values: dict[str, object] = {
        "prior_package_name": before.get(INVENTORY_NAME_FIELD, ""),
        "new_package_name": entry.package_name,
        "prior_retired": before.get("retired_at") is not None,
        "new_retired": entry.retired_at is not None,
    }
    for signal in INVENTORY_SIGNALS:
        values[f"prior_{signal}"] = before.get(signal)
        values[f"new_{signal}"] = getattr(entry, signal)
    return InventoryChange.objects.create(
        observed_at=at,
        entry=entry,
        actor=actor,
        origin=origin if actor is None else "",
        reason=reason,
        # `current_trace_id()` never raises and answers the empty string outside
        # an instrumented request or task (`CPM-AD-15`).
        trace_id=current_trace_id(),
        **values,
    )


def _log_changed(action: str, change: InventoryChange, *, actor: User) -> None:
    """Record an accepted change, after its transaction has ended.

    Logged after the block rather than inside it, so the event describes a
    change that reached the end of its transaction rather than one that was
    merely attempted (`CPM-AD-13`).

    Args:
        action: Which kind of change.
        change: The audit row it wrote.
        actor: Who made it.

    """
    logger.info(
        INVENTORY_CHANGED_EVENT,
        action=action,
        user_id=actor.pk,
        idp_subject=getattr(actor, IDENTITY_FIELD, ""),
        entry_id=change.entry_id,
        source_package_key=change.entry.source_package_key,
        change_id=change.pk,
    )


def _require_permitted(actor: User) -> None:
    """Refuse an actor who does not hold the inventory permission, and say so in the log.

    On `identity/services.py`'s terms exactly: `is_authenticated` is tested
    first so the log record beside the refusal can be written for an anonymous
    user, both identity fields are read defensively, and a superuser is admitted
    because `has_perm` admits one -- the mitigation being the audit row, which
    names them, rather than a check that would make the product's audited path
    the one thing a superuser could not use.

    Args:
        actor: The person attempting the change.

    Raises:
        InventoryNotPermittedError: When the actor is not authenticated or does
            not hold the permission. Raised after the log record.

    """
    if actor.is_authenticated and actor.has_perm(INVENTORY_CHANGE_PERMISSION):
        return
    logger.warning(
        INVENTORY_REFUSED_EVENT,
        reason=INVENTORY_PERMISSION_MISSING,
        user_id=getattr(actor, "pk", None),
        idp_subject=getattr(actor, IDENTITY_FIELD, ""),
    )
    message = (
        f"user {getattr(actor, 'pk', None)!r} does not hold {INVENTORY_CHANGE_PERMISSION!r}, so this inventory "
        f"change is refused. The inventory is governed reference data (CPM-FR-3, CPM-AD-14): changing it needs "
        f"the permission core/roles.py grants to the leadership role group."
    )
    raise InventoryNotPermittedError(message)


def _require_reason(reason: str) -> str:
    """Refuse a change that does not say why it was made.

    Args:
        reason: What the actor gave.

    Returns:
        The reason stripped.

    Raises:
        InventoryChangeError: When it is empty or only whitespace.

    """
    justification = reason.strip()
    if not justification:
        message = (
            f"an inventory change needs a reason; {reason!r} says nothing. CPM-AD-14 requires the change to "
            f"record why it was made, and an audit row with a blank reason answers the one question it exists "
            f"to answer with nothing."
        )
        raise InventoryChangeError(message)
    return justification


def _require_key(source_package_key: str) -> str:
    """Refuse a key that is blank or wider than its column.

    Args:
        source_package_key: What the caller supplied.

    Returns:
        The key stripped.

    Raises:
        InventoryChangeError: When it is blank after stripping, or longer than
            the column holds.

    """
    key = source_package_key.strip()
    if not key:
        message = (
            "an inventory row needs a source package key; a blank one names nothing. The key becomes the "
            "shell's associator_key, the stable value a later ingestion matches on (CPM-FR-2)."
        )
        raise InventoryChangeError(message)
    if len(key) > _KEY_LENGTH:
        message = (
            f"the source package key {key[:40]!r}... is {len(key)} characters and the column holds {_KEY_LENGTH}. "
            f"SQLite would store it truncated and PostgreSQL would refuse it (R-5), so it is refused here."
        )
        raise InventoryChangeError(message)
    return key


def _require_row(row: InventoryRow, *, key: str) -> None:
    """Refuse a row whose values the table will not hold, naming the key.

    Args:
        row: What the row states.
        key: The row's key, for the message.

    Raises:
        InventoryChangeError: When the name is blank or wider than its column,
            a required count is missing, or any count is not a whole number
            between 0 and `MAX_SIGNAL`. `bool` is refused explicitly: it is a
            subclass of `int`, and `True` would otherwise be a count of one.

    """
    name = row.package_name.strip() if isinstance(row.package_name, str) else ""
    if not name:
        message = (
            f"the inventory row {key!r} gives its package no name. The name is what the shell's canonical name "
            f"is created from, and a package with no name cannot be corrected, exported or found again "
            f"(CPM-FR-2)."
        )
        raise InventoryChangeError(message)
    if len(name) > _NAME_LENGTH:
        message = (
            f"the inventory row {key!r} names its package in {len(name)} characters and the column holds "
            f"{_NAME_LENGTH}."
        )
        raise InventoryChangeError(message)
    if name != row.package_name:
        message = f"the inventory row {key!r} carries a package name with surrounding whitespace; strip it first."
        raise InventoryChangeError(message)
    for signal in INVENTORY_REQUIRED_SIGNALS:
        if getattr(row, signal) is None:
            message = (
                f"the inventory row {key!r} states no {signal}. Both {' and '.join(INVENTORY_REQUIRED_SIGNALS)} "
                f"are required on every row: together they are the internal usage breadth CPM-FR-4 ranks by "
                f"(PRD Open Question 3b), and an ingestion would refuse the whole document over one row without "
                f"them."
            )
            raise InventoryChangeError(message)
    for signal in (*INVENTORY_REQUIRED_SIGNALS, *INVENTORY_OPTIONAL_SIGNALS):
        value = getattr(row, signal)
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > MAX_SIGNAL:
            message = (
                f"the inventory row {key!r} states {signal}={value!r}, which is not a count this schema can hold. "
                f"A usage signal is a whole number between 0 and {MAX_SIGNAL}; leave it blank to record that "
                f"the source did not say, which is stored as missing and stays distinguishable from zero "
                f"(PRD Appendix A.1 data rules)."
            )
            raise InventoryChangeError(message)


def _require_decided_at(instant: datetime) -> datetime:
    """Refuse a clock that answered a naive instant.

    Args:
        instant: What the clock answered.

    Returns:
        The instant, unchanged.

    Raises:
        InventoryChangeError: When it carries no usable offset.

    """
    if not is_aware(instant):
        message = (
            f"an inventory change was stamped with a naive instant ({instant!r}). The instant comes from a Clock, "
            f"which always answers in UTC (CPM-AD-26); a naive value has no offset to interpret."
        )
        raise InventoryChangeError(message)
    return instant
