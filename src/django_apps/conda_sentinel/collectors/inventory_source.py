"""Which inventory source this component reads, and the adapter that reads the table.

`CPM-AD-29` makes the inventory source a **declared adapter** behind the
collector base's one `Transport` seam. `CPM-IDENTITY-S07` declared the first
adapter, which reads a reviewed CSV; `CPM-OPERATE-S03` moves the inventory into
a governed table (`collectors.InventoryEntry`) and declares a second adapter that
reads it. This module is the second adapter and the setting that selects
between the two.

**The selection is a declared setting, and it fails closed toward the file.**
`CPM_INVENTORY_SOURCE` is `"watchlist"` -- the reviewed file, selected by
locality exactly as before -- unless an operator declares `"database"`. A
component that has never imported its watchlist keeps reading the file it
ships; switching is an operator's declaration *after* `import-watchlist` has
filled the table. An unrecognised value is refused at boot, naming the setting,
by `CollectorsConfig.ready()`: a component that silently read the file when it
was told to read the table would ingest a stale inventory and record every
package added since as absent -- permanently, in a log nothing may correct.

**The vocabulary is here and this module is model-free at import**, on the
terms `collectors/watchlist.py` states for itself: `config/settings/base.py`
imports the default from here, long before the app registry exists, and a
model definition at that point raises `AppRegistryNotReady`. The one read of
the table is inside `DatabaseInventoryAdapter.fetch`, where the import is
deferred, and `tests/unit/django_apps/test_inventory_source.py` asserts the
module imports nothing that reaches a model.

**The adapter answers exactly the document the CSV adapter answers.** One JSON
list, one object per active entry, the required columns always present and an
optional column present only where the row states it -- so `collectors/tasks.py`
carries no branch on which source is active and the record contract refuses
the same things from both (`CPM-AD-27`). The unit suite parses the shipped
development watchlist through `records_from` and through a table filled from the
same rows, and asserts the two documents are equal.

**Retired rows are absent from the document, so ingestion records them absent.**
`CPM-AD-25`: a package present in an earlier run and absent from a later one is
recorded as absent with a timestamp; no package row is deleted. Retiring an entry
is a column write (`collectors/inventory.py`), the adapter filters on that
column, and the next ingestion's absence derivation does the rest with no code
of its own. An empty table -- no active rows -- yields an empty list, which
`records_in` refuses as it refuses any empty inventory, and nothing is marked
absent.

**On the `AD-` prefix.** A bare `AD-n` in this repository is an *inherited*
platform decision; a decision from this product's own architecture spine always
carries the `CPM-` prefix.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from typing import Final

from conda_sentinel.core.transport import Payload

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = [
    "DATABASE_SOURCE",
    "DEFAULT_INVENTORY_SOURCE",
    "INVENTORY_SOURCES",
    "INVENTORY_SOURCE_SETTING",
    "WATCHLIST_SOURCE",
    "DatabaseInventoryAdapter",
    "active_records",
    "inventory_source_fault",
]

#: The setting `config/settings/base.py` assigns and `CollectorsConfig.ready()`
#: reads. Spelled once, here, so the boot refusal and the read name the same
#: thing.
INVENTORY_SOURCE_SETTING: Final[str] = "CPM_INVENTORY_SOURCE"

#: The two sources, by the value the setting carries. `watchlist` is the
#: reviewed file selected by locality (`CPM-AD-29`); `database` is the
#: governed table `CPM-OPERATE-S03` declares.
WATCHLIST_SOURCE: Final[str] = "watchlist"
DATABASE_SOURCE: Final[str] = "database"
INVENTORY_SOURCES: Final[frozenset[str]] = frozenset({WATCHLIST_SOURCE, DATABASE_SOURCE})

#: What an absent declaration means: the file. See the module docstring for why
#: this direction is the one that fails closed.
DEFAULT_INVENTORY_SOURCE: Final[str] = WATCHLIST_SOURCE


def inventory_source_fault(declared: object, *, setting: str = INVENTORY_SOURCE_SETTING) -> str | None:
    """Return why a declared inventory source is unusable, or `None` when it is one of the two.

    Pure, so the boot hook and a case can ask the same question of the same
    value. Written as a fault-returning rule rather than a raise, on the terms
    `collectors/conda_package.py`'s `declaration_fault` sets: the caller decides
    what to raise, and here the caller is a boot hook that raises
    `ImproperlyConfigured`.

    Args:
        declared: Whatever the settings module assigned.
        setting: What to call the setting in the message.

    Returns:
        A message naming the setting, the value and the two accepted values, or
        `None` when the value is exactly one of them. This is a rule about the
        *settings value*: a value of another type or a differently cased
        spelling is refused rather than guessed, because the setting is a
        declaration an operator makes once and a component that guessed what
        `Database` meant would be a component whose inventory source was chosen
        by a typo. Surrounding whitespace never reaches here from the
        environment -- `config/settings/base.py` strips the variable before
        assigning it -- so a value carrying any is a settings module that
        assigned it by hand, and is refused as any other misspelling is.

    """
    if isinstance(declared, str) and declared in INVENTORY_SOURCES:
        return None
    return (
        f"{setting}={declared!r} names no inventory source this component reads. The two are "
        f"{sorted(INVENTORY_SOURCES)}: {WATCHLIST_SOURCE!r} reads the reviewed file locality selects "
        f"(CPM-AD-29), and {DATABASE_SOURCE!r} reads the governed inventory table after import-watchlist "
        f"has filled it (CPM-OPERATE-S03). Absent means {DEFAULT_INVENTORY_SOURCE!r}; anything else is "
        f"refused rather than guessed, because a component reading the wrong source records every package "
        f"the other one names as absent."
    )


class DatabaseInventoryAdapter:
    """Reads the governed inventory table and answers the record document.

    The second `Transport` substitution at the collector base's seam, beside
    `collectors/watchlist.py`'s `WatchlistAdapter`, and structurally the same:
    one `fetch`, one `Payload`, `status_code=None` because this source does not
    speak HTTP. It holds nothing -- no path, no connection -- because the table
    is wherever Django's default database is, and a second declaration of that
    would be a second thing to get wrong.

    The table is read on every `fetch` and nothing is cached: an operator who
    added a row through the surface is believed by the next ingestion without a
    restart, which is the whole point of the table over the file.
    """

    def fetch(self, source: str, *, headers: Mapping[str, str] | None = None) -> Payload:
        """Read the active entries and record them as the record document.

        Args:
            source: The locator the collector declared, recorded on the `Payload`
                and otherwise ignored, exactly as `WatchlistAdapter.fetch` ignores
                it: `INVENTORY_SOURCE` names the run, not a resource.
            headers: Accepted and ignored; the parameter is keyword-only and
                defaulted in `Transport` for a substituted transport that speaks
                no HTTP.

        Returns:
            A `Payload` carrying `found=True`, one record per active entry in key
            order as its body, and `status_code=None`.

        """
        return Payload(source=source, found=True, body=json.dumps(active_records()), status_code=None)


def active_records() -> list[dict[str, str | int]]:
    """Return every active inventory entry as the record `records_from` would have produced.

    The shape is the CSV adapter's, deliberately: required columns always
    present, optional columns present only where the row states one, so a blank
    cell in the file and a NULL in the table reach `records_in` as the same
    omission. The order is the table's declared ordering, by key, so two reads
    of one table are one document.

    Returns:
        The records, in key order. Empty when no entry is active.

    """
    # Deferred: this module is imported from `config/settings/base.py` before the
    # app registry exists, and the model cannot be imported at module scope.
    from conda_sentinel.collectors.models import INVENTORY_KEY_FIELD  # noqa: PLC0415 - see above
    from conda_sentinel.collectors.models import INVENTORY_NAME_FIELD  # noqa: PLC0415 - see above
    from conda_sentinel.collectors.models import INVENTORY_OPTIONAL_SIGNALS  # noqa: PLC0415 - see above
    from conda_sentinel.collectors.models import INVENTORY_REQUIRED_SIGNALS  # noqa: PLC0415 - see above
    from conda_sentinel.collectors.models import InventoryEntry  # noqa: PLC0415 - see above

    columns = (INVENTORY_KEY_FIELD, INVENTORY_NAME_FIELD, *INVENTORY_REQUIRED_SIGNALS, *INVENTORY_OPTIONAL_SIGNALS)
    records: list[dict[str, str | int]] = []
    for row in InventoryEntry.objects.filter(retired_at__isnull=True).order_by(INVENTORY_KEY_FIELD).values(*columns):
        record: dict[str, str | int] = {
            INVENTORY_KEY_FIELD: row[INVENTORY_KEY_FIELD],
            INVENTORY_NAME_FIELD: row[INVENTORY_NAME_FIELD],
        }
        for signal in INVENTORY_REQUIRED_SIGNALS:
            # A required count the service always writes; a row an import handed
            # over without one is refused by `records_in` naming the key, which
            # is the same refusal the file gets, from the same place.
            if row[signal] is not None:
                record[signal] = row[signal]
        for signal in INVENTORY_OPTIONAL_SIGNALS:
            if row[signal] is not None:
                record[signal] = row[signal]
        records.append(record)
    return records
