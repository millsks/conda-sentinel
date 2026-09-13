"""A watchlist import as a command an operator runs and a deployment schedules (`CPM-OPERATE-S03`).

The inventory lives in a governed table (`collectors.InventoryEntry`) and the
reviewed CSV inside the wheel is its first-run seed. This command is how the
file reaches the table: `component.toml`'s `import-watchlist` admin process
(`pixi run import-watchlist`), and the operator's
`pixi run stack-run import_watchlist [<path>] [--replace] [--reason TEXT]`.

**It parses through the file adapter's own parser, so every refusal about the
file still applies.** `collectors/watchlist.py`'s `records_from` refuses a bad
header, a blank line, a ragged row, a missing required count, a count the column
will not hold, an over-long key or name and a repeated key -- naming the file
and the line -- before a single row is written. What reaches the service is
what would have reached the ingestion collector.

**It writes, and it is the one admin process that does.** `ingest`, `sweep` and
`policy-run` enqueue a task; this one calls `import_watchlist` inline, one
transaction per row, because an import is governed reference data changing and
`CPM-AD-14` puts that write behind a permission, a reason and an audit row in
the same transaction -- none of which a task could carry for an operator at a
terminal. Unattended, so `actor=None`: the command line is the gate, as it is
for every admin process, and every audit row it writes names the file as its
`origin`.

**`--replace` retires; nothing deletes; nothing reactivates.** A row the file no
longer names is retired with a reason naming the file, the next ingestion records
the package absent (`CPM-AD-25`), and the row, its audit trail and the package
all stay. Without `--replace`, rows the file does not name are left as they are,
which is what an import of a *partial* file means. A retired row the file still
names is left retired either way and counted `retired kept`: a person retired it
with a reason, and reactivating it is a person's decision on the inventory page.
The scheduled admin process runs **with** `--replace`, because the reviewed file
is the whole inventory and a package review removed from it must be retired.

**The path is resolved before it is recorded**, so two runs over one file --
one given a relative path, one an absolute -- write one `origin` on their audit
rows rather than two spellings of it.

Reporting is on two channels, as `prune_expired_state` does: one structlog event
carrying the counts, and one human line on stdout.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from typing import Any
from typing import Final

import structlog
from django.conf import settings
from django.core.management.base import BaseCommand
from django.core.management.base import CommandError

from conda_sentinel.collectors.apps import WATCHLIST_PATH_SETTING
from conda_sentinel.collectors.inventory import InventoryChangeError
from conda_sentinel.collectors.inventory import import_watchlist
from conda_sentinel.collectors.watchlist import WATCHLIST_ENCODING
from conda_sentinel.collectors.watchlist import WatchlistError
from conda_sentinel.collectors.watchlist import records_from
from conda_sentinel.core.clock import SystemClock

if TYPE_CHECKING:
    from argparse import ArgumentParser

__all__ = ["IMPORTED_EVENT", "Command"]

logger: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

#: The one event, carrying the counts.
IMPORTED_EVENT: Final[str] = "import_watchlist.imported"


class Command(BaseCommand):
    """Import a watchlist file into the governed inventory table."""

    help = (
        "Admin process: import a watchlist CSV into the inventory table, one audited transaction per row; "
        "defaults to the file locality selects. --replace retires every active row the file no longer names."
    )

    def add_arguments(self, parser: ArgumentParser) -> None:
        """Declare the path, the replace flag and the reason.

        Args:
            parser: The parser Django hands every management command.

        """
        parser.add_argument(
            "path",
            nargs="?",
            default=None,
            help="The watchlist CSV to import. Defaults to the file locality selects (INVENTORY_WATCHLIST_PATH).",
        )
        parser.add_argument(
            "--replace",
            action="store_true",
            help="Retire every active inventory row the file does not name. Nothing is deleted, nothing reactivated.",
        )
        parser.add_argument(
            "--reason",
            default="",
            help="Why, recorded on every audit row this import writes. Defaults to naming the file.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        """Parse the file, import it, and report the counts.

        Args:
            *args: Unused; Django's management interface passes none.
            **options: The parsed options: `path`, `replace`, `reason`.

        Raises:
            CommandError: When the file cannot be read as a watchlist (every
                refusal `records_from` makes, naming the file and the line), or
                when a row is refused by the service. Rows before a refused one
                stay committed, each with its audit row, and the message says so.

        """
        path = Path(options["path"]) if options.get("path") else Path(getattr(settings, WATCHLIST_PATH_SETTING))
        path = path.resolve()
        replace = bool(options.get("replace"))
        reason = str(options.get("reason") or "")

        try:
            text = path.read_text(encoding=WATCHLIST_ENCODING)
        except (OSError, UnicodeDecodeError) as unreadable:
            message = f"the watchlist at {path} could not be read: {type(unreadable).__name__}: {unreadable}"
            raise CommandError(message) from unreadable
        try:
            records = records_from(text, watchlist=path)
        except WatchlistError as refused:
            raise CommandError(str(refused)) from refused

        try:
            outcome = import_watchlist(
                records,
                replace=replace,
                actor=None,
                origin=str(path),
                clock=SystemClock(),
                reason=reason,
            )
        except InventoryChangeError as refused:
            message = (
                f"{refused} Rows imported before this one stay committed, each with its audit row; correct the "
                f"file and import again."
            )
            raise CommandError(message) from refused

        logger.info(
            IMPORTED_EVENT,
            watchlist=str(path),
            replace=replace,
            added=outcome.added,
            changed=outcome.changed,
            unchanged=outcome.unchanged,
            retired=outcome.retired,
            retired_kept=outcome.retired_kept,
        )
        self.stdout.write(
            f"imported {path}: {outcome.added} added, {outcome.changed} changed, {outcome.unchanged} unchanged, "
            f"{outcome.retired} retired{' (--replace)' if replace else ''}, {outcome.retired_kept} retired kept"
        )
