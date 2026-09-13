"""The watchlist contract: its columns, its refusals, and the rule that selects the file.

`CPM-AD-29` makes the inventory source a declared adapter reading a versioned
watchlist, with locality selecting the file and failing closed toward production.
Everything decided before a run exists is decided here.

**No file is read.** `records_from` is pure -- it takes text and answers records
-- so the whole column contract and every content refusal is measured against
strings held in memory. What is left for the adapter is opening a file, and the
three cases that are genuinely about files (missing, undecodable, re-read on
every fetch) write into `tmp_path`. The *shipped* files are the integration
tier's: they are data, they are read off disk, and asserting their contents here
would make a unit module depend on the repository's working tree.

**The selection rule is asserted three ways because it fails closed in three
ways.** `COMPONENT_RUNTIME` absent, empty, and set to something unrecognized all
mean *deployed*, and each is a separate case rather than a parametrized shrug --
the failure they prevent is a deployed component reading the development subset,
finding every package outside it missing, and recording each one as absent,
permanently, in a log nothing may correct. The composition
`watchlist_path(local=is_local())` runs the real `is_local()` under
`monkeypatch.setenv`/`delenv` with nothing mocked, which is also how the two
halves of `AD-4`'s split are proved to fit: the pure function lives in the
application, the environment read lives in `config`, and only a case that
composes them can say the pair behaves as `CPM-AD-29` describes.

**Every bound the record contract applies is applied here first.** A count past
the column's ceiling, an over-long key, an over-long name: each is refused by
`records_in` too, one layer later, in a message naming neither the file nor the
line. The story's rule is that every refusal names the file and, where a row is
at fault, the line -- so each of those has a case here as well as there.

**The tree-wide `AD-4` sweep is not here.** It is in
`tests/unit/test_import_roots.py`, beside the other rules about what may import
what, because the next story to want an environment value from a domain
application will not be this one.
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import TYPE_CHECKING
from typing import Final

import pytest
from django.apps import apps
from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings

from conda_sentinel.collectors.apps import WATCHLIST_PATH_SETTING
from conda_sentinel.collectors.inventory_source import INVENTORY_SOURCE_SETTING
from conda_sentinel.collectors.inventory_source import WATCHLIST_SOURCE
from conda_sentinel.collectors.tasks import INVENTORY_SOURCE
from conda_sentinel.collectors.tasks import MAX_COUNT
from conda_sentinel.collectors.tasks import OPTIONAL_SIGNALS
from conda_sentinel.collectors.tasks import PACKAGE_NAME
from conda_sentinel.collectors.tasks import RECORD_FIELDS
from conda_sentinel.collectors.tasks import REQUIRED_SIGNALS
from conda_sentinel.collectors.tasks import SOURCE_PACKAGE_KEY
from conda_sentinel.collectors.tasks import InventoryAdapterError
from conda_sentinel.collectors.tasks import declare_inventory_adapter
from conda_sentinel.collectors.tasks import declared_inventory_adapter
from conda_sentinel.collectors.tasks import records_in
from conda_sentinel.collectors.tasks import withdraw_inventory_adapter
from conda_sentinel.collectors.watchlist import DEVELOPMENT_WATCHLIST
from conda_sentinel.collectors.watchlist import MAX_PACKAGE_NAME
from conda_sentinel.collectors.watchlist import MAX_SIGNAL
from conda_sentinel.collectors.watchlist import MAX_SOURCE_PACKAGE_KEY
from conda_sentinel.collectors.watchlist import OPTIONAL_COLUMNS
from conda_sentinel.collectors.watchlist import PRODUCTION_WATCHLIST
from conda_sentinel.collectors.watchlist import REQUIRED_COLUMNS
from conda_sentinel.collectors.watchlist import WATCHLIST_COLUMNS
from conda_sentinel.collectors.watchlist import WATCHLIST_DIRECTORY
from conda_sentinel.collectors.watchlist import WatchlistAdapter
from conda_sentinel.collectors.watchlist import WatchlistError
from conda_sentinel.collectors.watchlist import _watchlist_directory
from conda_sentinel.collectors.watchlist import records_from
from conda_sentinel.collectors.watchlist import watchlist_path
from conda_sentinel.core.transport import Transport
from conda_sentinel.identity.services import ASSOCIATOR_KEY_LENGTH
from conda_sentinel.identity.services import CANONICAL_NAME_LENGTH
from config.locality import RUNTIME_ENV_VAR
from config.locality import is_local
from tests.collectors import RecordedTransport

if TYPE_CHECKING:
    from collections.abc import Iterator

#: The application whose `ready()` declares the adapter, by its derived label.
COLLECTORS_APP_LABEL: Final[str] = "collectors"

#: A locator that is not `INVENTORY_SOURCE`, for the case that proves the adapter
#: ignores what it is handed. Deliberately a path: if the adapter read its
#: locator at all, this is what it would read.
A_MISLEADING_LOCATOR: Final[str] = "file:///etc/passwd"

#: What a refusal calls the text these cases parse. `records_from` puts it in
#: every message, and a name that is obviously not a real path is what makes the
#: assertions about the message rather than about a path.
A_WATCHLIST: Final[str] = "<the watchlist under test>"

#: The header the shipped files carry, in their own order.
A_HEADER: Final[str] = ",".join([*REQUIRED_COLUMNS, *OPTIONAL_COLUMNS])

#: One well-formed row, every column populated, matching that header.
A_ROW: Final[str] = "conda-forge/numpy,numpy,12,3,7,4,1200,5"

#: The same row with every optional cell blank.
A_SPARSE_ROW: Final[str] = "conda-forge/numpy,numpy,12,3,,,,"

#: The same row again, stating a genuine zero where the sparse one states
#: nothing. The pair is what makes "missing is distinguishable from zero" an
#: assertion rather than a claim.
A_ZERO_ROW: Final[str] = "conda-forge/numpy,numpy,12,3,7,4,0,5"

#: How many records a file naming two packages under one name yields: both of
#: them. Named because `PLR2004` is right about bare numbers in an assertion --
#: what this one means is "the parser refuses neither row".
TWO_PACKAGES_ONE_NAME: Final[int] = 2

#: What the well-formed row says, as the record contract reads it.
A_COMPONENT_COUNT: Final[int] = 12
A_LOB_COUNT: Final[int] = 3
A_DOWNLOADS: Final[int] = 1200


def _text(*lines: str) -> str:
    """Return a watchlist as text, with the newlines a real file would carry.

    Args:
        *lines: The lines, without trailing newlines. An empty call returns the
            empty string, which is the no-header case.

    Returns:
        The text `records_from` parses.

    """
    return "\n".join(lines) + ("\n" if lines else "")


def _records(*lines: str) -> list[dict[str, object]]:
    """Parse a watchlist held in memory.

    Args:
        *lines: The file's lines.

    Returns:
        The records it yields.

    """
    return list(records_from(_text(*lines), watchlist=A_WATCHLIST))


def _refused(*lines: str) -> str:
    """Parse a watchlist expected to be refused, and return the refusal's message.

    Args:
        *lines: The file's lines.

    Returns:
        The message, so a case can assert what it names.

    """
    with pytest.raises(WatchlistError) as refused:
        records_from(_text(*lines), watchlist=A_WATCHLIST)
    message = str(refused.value)
    assert A_WATCHLIST in message, message
    return message


def _written(path: Path, *lines: str) -> Path:
    """Write a watchlist and return its path, for the cases that are about files.

    Args:
        path: Where to write it.
        *lines: The lines, without trailing newlines.

    Returns:
        The path, so a case can construct an adapter in one expression.

    """
    path.write_text(_text(*lines), encoding="utf-8")
    return path


class WatchlistAdapterDouble(WatchlistAdapter):
    """A watchlist adapter whose file is a string held in memory.

    The one seam these cases need. `WatchlistAdapter` is `_read` plus
    `records_from`, and every case about what a watchlist *says* wants the second
    without the first. Substituting the read rather than mocking `Path` keeps the
    case measuring the class that ships: `fetch`, the `Payload` it builds and the
    parser it calls are all the real ones, and only the file is not there.
    """

    def __init__(self, text: str) -> None:
        """Hold the text this adapter answers instead of opening a file.

        Args:
            text: What the file would have contained.

        """
        super().__init__(path=Path(A_WATCHLIST))
        self._text = text

    def _read(self) -> str:
        """Return the text, without touching a filesystem.

        Returns:
            What this double was constructed with.

        """
        return self._text


@pytest.fixture
def _slot_restored() -> Iterator[None]:
    """Leave the adapter slot as the case found it: empty.

    The declaration is process-global, so an adapter left behind by a case that
    calls `ready()` would be the source every later case in the session ingests
    through -- the same reason `tests/conftest.py` withdraws the boot declaration
    for the session in the first place.

    Yields:
        None. The restoration is the effect.

    """
    # The file is what these cases are about, and the `dev` environment the
    # suite runs in declares the *table* (`CPM-OPERATE-S03`), so the setting is
    # pinned to the file here; `test_inventory_source.py` drives the other branch.
    with override_settings(**{INVENTORY_SOURCE_SETTING: WATCHLIST_SOURCE}):
        yield
    if declared_inventory_adapter() is not None:
        withdraw_inventory_adapter()


# ---------------------------------------------------------------------------
# The column contract, and the bounds it borrows.
# ---------------------------------------------------------------------------


def test_the_header_is_exactly_the_record_contracts_fields() -> None:
    """The one duplication in this story, reconciled in both directions.

    `collectors/watchlist.py` cannot import `collectors/tasks.py`: it is imported
    from `config/settings/base.py`, before the app registry exists, and
    `tasks.py` imports models. So the column names are written out there and
    compared here -- a column added to the header and not to the record contract
    would be refused by `records_in` as undefined, and a field added to the
    record contract and not to the header would be missing from every row.
    """
    assert WATCHLIST_COLUMNS == RECORD_FIELDS


def test_the_required_columns_are_the_key_the_name_and_the_two_counts() -> None:
    """`CPM-FR-42`: the key, the package name, and the breadth `CPM-FR-4` ranks by."""
    assert set(REQUIRED_COLUMNS) == {SOURCE_PACKAGE_KEY, PACKAGE_NAME, *REQUIRED_SIGNALS}


def test_the_optional_columns_are_the_four_nullable_signals() -> None:
    """Open Question 3b: these four are score inputs no hand-authored source can state."""
    assert set(OPTIONAL_COLUMNS) == set(OPTIONAL_SIGNALS)


def test_no_column_is_declared_both_required_and_optional() -> None:
    """The two tuples partition the header rather than merely covering it.

    A column in both would be required by one loop and read as blank-able by the
    other, and which behaviour won would depend on the order the loops happen to
    run in.
    """
    assert not set(REQUIRED_COLUMNS) & set(OPTIONAL_COLUMNS)


def test_the_signal_ceiling_is_the_one_the_record_contract_holds() -> None:
    """The second restated number, reconciled the same way as the columns.

    `MAX_SIGNAL` exists so the *adapter* can refuse an oversized count and name
    the line. It is only worth having while it is the same number `MAX_COUNT`
    refuses one layer later; two ceilings would be a file the adapter accepts and
    the contract rejects, in a message naming neither the file nor the row.
    """
    assert MAX_SIGNAL == MAX_COUNT


def test_the_two_text_bounds_are_the_columns_they_land_in_and_are_different() -> None:
    """The whole point of separating the name from the key, as two numbers.

    The key lands in `associator_key` and the name in `canonical_name`, and the
    two columns are different widths. Fusing them would refuse a legitimately
    long source key for a column it does not occupy -- which is the confusion
    `CPM-IDENTITY-S07` exists to remove, reappearing as a bound.
    """
    assert MAX_SOURCE_PACKAGE_KEY == ASSOCIATOR_KEY_LENGTH
    assert MAX_PACKAGE_NAME == CANONICAL_NAME_LENGTH
    assert MAX_SOURCE_PACKAGE_KEY != MAX_PACKAGE_NAME


# ---------------------------------------------------------------------------
# What a row yields.
# ---------------------------------------------------------------------------


def test_a_well_formed_row_yields_every_column() -> None:
    """All eight columns, through the parser and the record contract together."""
    records = records_in(
        WatchlistAdapterDouble(_text(A_HEADER, A_ROW)).fetch(INVENTORY_SOURCE),
    )

    assert len(records) == 1
    record = records[0]
    assert record.source_package_key == "conda-forge/numpy"
    assert record.package_name == "numpy"
    assert record.internal_component_count == A_COMPONENT_COUNT
    assert record.internal_lob_count == A_LOB_COUNT
    assert (record.apps, record.platforms, record.downloads, record.versions) == (7, 4, A_DOWNLOADS, 5)


def test_a_blank_optional_cell_is_omitted_from_the_record() -> None:
    """Blank means *the source did not say*, and is stored as NULL.

    Which is the whole of Open Question 3b: these four are score inputs for a
    function that is itself undecided, so a fabricated value would be an
    invention and a coerced zero would be a claim. The key is *omitted* rather
    than written as null, which is what the record contract reads as missing.
    """
    record = _records(A_HEADER, A_SPARSE_ROW)[0]

    assert not set(record) & set(OPTIONAL_COLUMNS)


def test_a_zero_optional_cell_is_a_stated_zero() -> None:
    """The other half of the pair, and the one a falsy test would lose.

    `0` and blank are both nothing to a careless reader. They are different
    observations: one says the source counted none, the other says the source did
    not count.
    """
    assert _records(A_HEADER, A_ZERO_ROW)[0]["downloads"] == 0


def test_the_header_may_declare_its_columns_in_any_order() -> None:
    """ "Exactly the declared columns, in any order" is a claim, so it is tested.

    The rows are read positionally against the header, so a permuted header has
    to permute the cells with it. A parser that had quietly fixed the order would
    pass every case above and mis-assign every cell here.
    """
    reversed_header = ",".join(reversed([*REQUIRED_COLUMNS, *OPTIONAL_COLUMNS]))
    reversed_row = ",".join(reversed(A_ROW.split(",")))

    assert _records(reversed_header, reversed_row) == _records(A_HEADER, A_ROW)


def test_incidental_whitespace_in_the_header_is_normalized() -> None:
    """The shape hand-editing produces, and the shape that used to crash.

    `source_package_key, package_name, ...` is what a person writes. The header
    check strips, so it passed; the rows were then keyed by the *unstripped*
    names, so the lookup raised `KeyError` -- out of this module, past
    `Collector.sweep`, which catches only `TransportError`, and out of the Celery
    task as a crash rather than as the refusal naming the file that everything
    else here promises.
    """
    spaced = ", ".join([*REQUIRED_COLUMNS, *OPTIONAL_COLUMNS])

    assert _records(spaced, A_ROW) == _records(A_HEADER, A_ROW)


# ---------------------------------------------------------------------------
# Every refusal.
# ---------------------------------------------------------------------------


def test_a_column_the_contract_does_not_define_is_refused() -> None:
    """`CPM-FR-42`, `CPM-FR-1`: ingestion never asserts a mapping.

    A `feedstock_url` column is a reviewer supplying a mapping, and ignoring it
    would be worse than refusing it -- the file would say something the product
    never reads and nothing would ever report the discrepancy.
    """
    assert "feedstock_url" in _refused(f"{A_HEADER},feedstock_url", f"{A_ROW},https://example.invalid")


def test_a_missing_required_column_is_refused_by_name() -> None:
    """The same set comparison from the other side, which is why the rule is one rule."""
    header = ",".join(name for name in [*REQUIRED_COLUMNS, *OPTIONAL_COLUMNS] if name != "internal_lob_count")

    assert "internal_lob_count" in _refused(header, "conda-forge/numpy,numpy,12,7,4,1200,5")


def test_a_repeated_column_is_refused() -> None:
    """A column named twice makes every cell under it ambiguous.

    Keeping the last one silently is a value chosen by column order rather than
    by anybody.
    """
    assert "apps" in _refused(f"{A_HEADER},apps", f"{A_ROW},99")


def test_a_file_with_no_header_at_all_is_refused() -> None:
    """An empty file is not an empty inventory; it is a file nobody wrote."""
    assert "header" in _refused()


def test_a_non_numeric_count_is_refused_naming_the_file_the_line_and_the_column() -> None:
    """The refusal a reviewer acts on, so it says all three things.

    A message naming only the file sends a reviewer to read the whole watchlist;
    one naming only the column says nothing about which row.
    """
    message = _refused(A_HEADER, A_ROW, "conda-forge/pandas,pandas,many,3,,,,")

    assert "internal_component_count" in message
    assert "line 3" in message


@pytest.mark.parametrize(
    "cell",
    [
        pytest.param("-1", id="negative"),
        pytest.param("1.5", id="decimal"),
        pytest.param("1_000", id="underscored"),
        pytest.param("١٢", id="non-ascii-digits"),
        pytest.param("+3", id="signed"),
    ],
)
def test_a_count_that_is_not_written_as_digits_is_refused(cell: str) -> None:
    """`int()` accepts a surprising amount of this, and a watchlist is reviewed by reading it.

    An underscored or signed literal is a number Python would read and a reviewer
    would not, and a decimal is a count that is not one.
    """
    assert "not a count" in _refused(A_HEADER, f"conda-forge/numpy,numpy,{cell},3,,,,")


def test_a_count_larger_than_the_column_holds_is_refused_with_its_line() -> None:
    """The bound the record contract also applies, applied where the line is known.

    `records_in` refuses the same value one layer later in a message naming
    neither the file nor the row, and the story's rule is that every refusal
    names both. It is a real parity gap as well as a message: PostgreSQL refuses
    an oversized `PositiveIntegerField` and SQLite stores it, so an unrefused
    value is a row on a developer's machine and a failed run in the gate (`R-5`).
    """
    message = _refused(A_HEADER, f"conda-forge/numpy,numpy,{MAX_SIGNAL + 1},3,,,,")

    assert "line 2" in message
    assert str(MAX_SIGNAL) in message


@pytest.mark.parametrize(
    ("column", "limit"),
    [
        pytest.param("source_package_key", MAX_SOURCE_PACKAGE_KEY, id="key"),
        pytest.param("package_name", MAX_PACKAGE_NAME, id="name"),
    ],
)
def test_a_text_cell_wider_than_its_column_is_refused_with_its_line(column: str, limit: int) -> None:
    """Both text bounds, each against the column it actually lands in.

    Refused here rather than left to the record contract for the message, and
    rather than left to the database for the parity: SQLite truncates and
    PostgreSQL raises.
    """
    cells = dict(zip([*REQUIRED_COLUMNS, *OPTIONAL_COLUMNS], A_ROW.split(","), strict=True))
    cells[column] = "n" * (limit + 1)
    message = _refused(A_HEADER, ",".join(cells.values()))

    assert column in message
    assert "line 2" in message
    assert str(limit) in message


@pytest.mark.parametrize("column", sorted(REQUIRED_COLUMNS))
def test_a_blank_required_cell_is_refused(column: str) -> None:
    """All four are required on every row, and each is refused by name.

    Parametrized over the roster rather than over four literals, so a column
    promoted to required gets a case without one being written.
    """
    cells = dict(zip([*REQUIRED_COLUMNS, *OPTIONAL_COLUMNS], A_ROW.split(","), strict=True))
    cells[column] = ""

    assert column in _refused(A_HEADER, ",".join(cells.values()))


@pytest.mark.parametrize(
    "row",
    [
        pytest.param("conda-forge/numpy,numpy,12,3,7,4", id="short"),
        pytest.param(f"{A_ROW},surplus", id="long"),
    ],
)
def test_a_ragged_row_is_refused(row: str) -> None:
    """A row with the wrong number of cells puts every value under the wrong column.

    Both directions, because a reader that filled a short row's unreached cells
    with nothing and collected a long row's surplus under a key nobody reads
    would accept both silently.
    """
    assert "line 2" in _refused(A_HEADER, row)


def test_a_blank_line_is_refused_rather_than_skipped() -> None:
    """The decision, made and asserted rather than inherited from `csv`.

    A reader that skips blank lines while refusing short rows treats two
    unfinished edits differently for no reason anybody can state. Both are now a
    line that is not a record, and the refusal says which line.
    """
    assert "line 3" in _refused(A_HEADER, A_ROW, "", "conda-forge/pandas,pandas,4,2,,,,")


def test_a_repeated_source_package_key_is_refused_naming_the_key() -> None:
    """`CPM-FR-42`: two rows for one key are two claims about one package.

    Refused here as well as by the record contract, deliberately: this refusal
    names the file and the key a reviewer is editing, and the contract's still
    refuses a duplicate from any future adapter.
    """
    assert "conda-forge/numpy" in _refused(A_HEADER, A_ROW, A_ROW)


def test_two_rows_sharing_a_name_are_not_refused_here() -> None:
    """The deliberate asymmetry, asserted so it cannot be tightened by accident.

    Two rows with different keys are two packages, and the source is entitled to
    call them the same thing. Refusing the file would fail a whole sweep for a
    collision that belongs to one record -- so it is left to resolution, where
    `canonical_name`'s unique constraint refuses the second, the sweep carries on,
    and the run finalizes `partial`.
    """
    records = _records(A_HEADER, A_ROW, "internal/numpy,numpy,4,2,,,,")

    assert len(records) == TWO_PACKAGES_ONE_NAME


def test_a_file_that_is_not_readable_as_csv_is_refused() -> None:
    """A `csv.Error` used to escape this module as a crash rather than as a refusal.

    Provoked with a field past `csv.field_size_limit()` -- the one `csv.Error`
    that can be reached deterministically, and the one a real watchlist reaches
    by way of a quote somebody opened and never closed, which swallows the rest
    of the file into a single cell. Nothing between here and the Celery task
    turns that into a message naming the file: `Collector.sweep` catches
    `TransportError` and a `csv.Error` is not one.
    """
    runaway = "n" * (csv.field_size_limit() + 1)

    assert "CSV" in _refused(A_HEADER, f"conda-forge/numpy,{runaway},12,3,,,,")


def test_a_header_only_file_is_refused() -> None:
    """An inventory naming nothing is a misconfiguration, not an empty inventory.

    This is what makes shipping the production watchlist unpopulated safe: a
    component deployed before it is reviewed fails its ingestion loudly, rather
    than sweeping an empty document and recording every package as departed.
    """
    assert "no rows" in _refused(A_HEADER)


def test_every_refusal_is_an_improperly_configured() -> None:
    """`CPM-AD-14`: the watchlist is governed reference data, so a bad one is a misconfiguration.

    The boundary matters because the other one exists: `records_in` raises a
    `ValueError` about a *document*, which would send an operator looking for a
    broken source. This sends them to the file.
    """
    with pytest.raises(ImproperlyConfigured):
        records_from("", watchlist=A_WATCHLIST)


# ---------------------------------------------------------------------------
# The adapter: a Transport substitution, and the file it opens.
# ---------------------------------------------------------------------------


def test_the_adapter_satisfies_the_transport_protocol(tmp_path: Path) -> None:
    """`CPM-AD-29`: the adapter is a substitution at the collector base's seam.

    `runtime_checkable` sees method *names* only, which is why the cases below
    pin what `fetch` answers as well as that it exists.
    """
    assert isinstance(WatchlistAdapter(path=_written(tmp_path / "w.csv", A_HEADER, A_ROW)), Transport)


def test_the_payload_says_the_source_does_not_speak_http() -> None:
    """`status_code=None` is what "this source does not speak HTTP" means.

    `core/transport.py` documents that meaning and names this adapter as the
    first case of it. `found=True` is the other half: a file that was read is a
    source that answered, and an unreadable one is refused rather than recorded
    as an absence.
    """
    payload = WatchlistAdapterDouble(_text(A_HEADER, A_ROW)).fetch(INVENTORY_SOURCE)

    assert payload.status_code is None
    assert payload.found is True
    assert payload.not_modified is False
    assert payload.etag is None


def test_the_locator_is_recorded_and_otherwise_ignored() -> None:
    """`INVENTORY_SOURCE` names the run, not a resource.

    The adapter reads the file it was constructed with, whatever it is handed.
    This is what stops a caller redirecting the inventory source by passing a
    different locator -- which would put "which file is this component's
    inventory" back in the hands of whoever calls `fetch`.
    """
    payload = WatchlistAdapterDouble(_text(A_HEADER, A_ROW)).fetch(A_MISLEADING_LOCATOR)

    assert payload.source == A_MISLEADING_LOCATOR
    assert records_in(payload)[0].source_package_key == "conda-forge/numpy"


def test_headers_are_accepted_and_ignored() -> None:
    """The parameter exists in `Transport` for exactly this case.

    Keyword-only and defaulted, so a transport that speaks no HTTP can accept and
    discard conditional-request headers without the seam growing a second method.
    """
    adapter = WatchlistAdapterDouble(_text(A_HEADER, A_ROW))

    assert adapter.fetch(INVENTORY_SOURCE, headers={"If-None-Match": '"x"'}) == adapter.fetch(INVENTORY_SOURCE)


def test_a_missing_file_is_refused_naming_the_path_it_looked_for(tmp_path: Path) -> None:
    """The refusal that says which file to go and create.

    Not "the inventory is empty", which is what a swallowed `FileNotFoundError`
    would amount to and which would record every package the source has ever
    named as absent.
    """
    absent = tmp_path / "no-such-watchlist.csv"

    with pytest.raises(WatchlistError) as refused:
        WatchlistAdapter(path=absent).fetch(INVENTORY_SOURCE)

    assert str(absent) in str(refused.value)


def test_a_file_that_is_not_text_is_refused(tmp_path: Path) -> None:
    """A watchlist is reviewed in a pull-request diff, and is text.

    Refused explicitly rather than left to the `UnicodeDecodeError` a read would
    raise: that is a `ValueError`, so it would travel past the collector's own
    handling as a crash rather than as a misconfiguration naming the file.
    """
    path = tmp_path / "w.csv"
    path.write_bytes(b"source_package_key\n\xff\xfe\x00binary")

    with pytest.raises(WatchlistError) as refused:
        WatchlistAdapter(path=path).fetch(INVENTORY_SOURCE)

    assert str(path) in str(refused.value)


def test_a_byte_order_mark_is_read_rather_than_refused(tmp_path: Path) -> None:
    """A reviewed CSV that has been through Excel, which is where these files live.

    Under plain `utf-8` the mark becomes part of the first header cell, and the
    refusal that follows names one undefined column and one missing column whose
    printed names are indistinguishable -- the least actionable message this
    module could produce.
    """
    path = tmp_path / "w.csv"
    path.write_text(_text(A_HEADER, A_ROW), encoding="utf-8-sig")

    assert len(records_in(WatchlistAdapter(path=path).fetch(INVENTORY_SOURCE))) == 1


def test_the_file_is_re_read_on_every_fetch(tmp_path: Path) -> None:
    """Nothing is cached, so an operator who corrects the watchlist is believed.

    A cached parse would mean a correction took effect only after the worker was
    restarted, which is the kind of staleness nobody suspects because nothing
    reports it.
    """
    path = _written(tmp_path / "w.csv", A_HEADER, A_ROW)
    adapter = WatchlistAdapter(path=path)
    first = records_in(adapter.fetch(INVENTORY_SOURCE))

    _written(path, A_HEADER, A_ROW, "conda-forge/pandas,pandas,4,2,,,,")

    assert len(first) == 1
    assert len(records_in(adapter.fetch(INVENTORY_SOURCE))) == len(first) + 1


def test_an_unresolvable_module_location_refuses_rather_than_raising_an_oserror(tmp_path: Path) -> None:
    """The directory is computed at import, and import is `config/settings/base.py`'s.

    An installation that shipped the modules and dropped the data tree would
    otherwise raise a bare `OSError` in the middle of settings import -- a
    traceback with no file name in it, during the one phase of startup that has
    nothing else to say.
    """
    with pytest.raises(WatchlistError) as refused:
        _watchlist_directory(str(tmp_path / "not-a-module.py"))

    assert "not-a-module.py" in str(refused.value)


# ---------------------------------------------------------------------------
# Locality selects the file, and fails closed.
# ---------------------------------------------------------------------------


def test_the_pure_function_selects_the_development_subset_when_local() -> None:
    """`watchlist_path` takes a boolean and knows nothing about the environment.

    That is what lets it live in a domain application under `AD-4` while
    `CPM-AD-29`'s `config.locality.is_local()` still decides.
    """
    assert watchlist_path(local=True) == WATCHLIST_DIRECTORY / DEVELOPMENT_WATCHLIST


def test_the_pure_function_selects_the_production_watchlist_otherwise() -> None:
    """The other half, and the default every fail-closed case below arrives at."""
    assert watchlist_path(local=False) == WATCHLIST_DIRECTORY / PRODUCTION_WATCHLIST


def test_a_declared_local_runtime_selects_the_development_subset(monkeypatch: pytest.MonkeyPatch) -> None:
    """The composition `config/settings/base.py` performs, with nothing mocked.

    The real `is_local()`, which reads `os.environ` at call time, and the real
    `watchlist_path`. Only a case that composes them can say the pair behaves as
    `CPM-AD-29` describes -- each half alone is trivially correct.
    """
    monkeypatch.setenv(RUNTIME_ENV_VAR, "local")

    assert watchlist_path(local=is_local()) == WATCHLIST_DIRECTORY / DEVELOPMENT_WATCHLIST


def test_an_absent_runtime_selects_the_production_watchlist(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail closed, one: a declaration that never arrived is deployed.

    Asserted separately from the two below rather than parametrized with them,
    because they are three different ways for the declaration to be lost and the
    consequence of getting any one of them wrong is a deployed component reading
    the development subset.
    """
    monkeypatch.delenv(RUNTIME_ENV_VAR, raising=False)

    assert watchlist_path(local=is_local()) == WATCHLIST_DIRECTORY / PRODUCTION_WATCHLIST


def test_an_empty_runtime_selects_the_production_watchlist(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail closed, two: a variable set to nothing is a ConfigMap key with no value."""
    monkeypatch.setenv(RUNTIME_ENV_VAR, "")

    assert watchlist_path(local=is_local()) == WATCHLIST_DIRECTORY / PRODUCTION_WATCHLIST


def test_an_unrecognized_runtime_selects_the_production_watchlist(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail closed, three: `dev` is a deployed development environment, and is deployed.

    `config/locality.py` says so in as many words -- a platform is likely to set a
    generic value for a development *deployment*, and only the exact word `local`
    is a local run.
    """
    monkeypatch.setenv(RUNTIME_ENV_VAR, "dev")

    assert watchlist_path(local=is_local()) == WATCHLIST_DIRECTORY / PRODUCTION_WATCHLIST


# ---------------------------------------------------------------------------
# The declaration `CollectorsConfig.ready()` makes.
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("_slot_restored")
def test_the_app_config_declares_the_watchlist_adapter() -> None:
    """`CPM-AD-29`, inherited `AD-8`: declared in one visible call, never discovered.

    Reached by calling `ready()` rather than by reading the slot the boot left
    behind, because `tests/conftest.py` withdraws that one for the session -- and
    because calling the hook is what proves the hook is where the declaration
    lives, rather than an import side effect somewhere.
    """
    apps.get_app_config(COLLECTORS_APP_LABEL).ready()

    assert isinstance(declared_inventory_adapter(), WatchlistAdapter)


@pytest.mark.usefixtures("_slot_restored")
def test_declaring_the_adapter_twice_does_not_abort_boot() -> None:
    """`AppConfig.ready` is Django's to call, and a second `django.setup()` calls it again.

    `declare_inventory_adapter` refuses a second declaration -- rightly, because a
    second adapter silently replacing the first is how a deployed component
    ingests a development subset. The guard is what stops that refusal firing on
    a declaration that had already succeeded.
    """
    config = apps.get_app_config(COLLECTORS_APP_LABEL)

    config.ready()
    config.ready()

    assert isinstance(declared_inventory_adapter(), WatchlistAdapter)


@pytest.mark.usefixtures("_slot_restored")
def test_the_declared_adapter_reads_the_file_settings_selected() -> None:
    """The two halves of the split meet here, and this is where they are checked.

    `config/settings/base.py` composes `watchlist_path(local=is_local())` and the
    hook reads the result. A hook that computed its own path would pass every
    case above and still be a second selection rule.
    """
    from django.conf import settings  # noqa: PLC0415 - read at call time, as `ready()` does

    apps.get_app_config(COLLECTORS_APP_LABEL).ready()
    adapter = declared_inventory_adapter()

    assert isinstance(adapter, WatchlistAdapter)
    assert adapter.path == settings.INVENTORY_WATCHLIST_PATH
    assert adapter.path in {watchlist_path(local=True), watchlist_path(local=False)}


@pytest.mark.usefixtures("_slot_restored")
def test_a_foreign_adapter_already_declared_aborts_boot_rather_than_being_kept() -> None:
    """The guard discriminates, which is the only reason it is not `is None`.

    An adapter of another kind in the slot means somebody else declared this
    component's inventory source, and "which source is this component's" would
    then be answered by import order. `CPM-AD-29` says exactly one, so boot stops.
    """
    declare_inventory_adapter(RecordedTransport())

    with pytest.raises(InventoryAdapterError):
        apps.get_app_config(COLLECTORS_APP_LABEL).ready()


@pytest.mark.usefixtures("_slot_restored")
def test_a_watchlist_adapter_on_another_path_aborts_boot(tmp_path: Path) -> None:
    """The half a type check alone would miss, and the dangerous half.

    A `WatchlistAdapter` reading some *other* file is precisely the failure
    `CPM-AD-29` describes -- a deployed component ingesting a development subset
    and recording every package outside it as absent. It is also the one an
    `isinstance` guard would wave through.
    """
    declare_inventory_adapter(WatchlistAdapter(path=tmp_path / "somewhere-else.csv"))

    with pytest.raises(InventoryAdapterError):
        apps.get_app_config(COLLECTORS_APP_LABEL).ready()


@pytest.mark.usefixtures("_slot_restored")
def test_a_settings_module_declaring_no_watchlist_path_refuses_by_name() -> None:
    """A misconfiguration says which setting is missing and what assigns it.

    The alternative is an `AttributeError` out of a boot hook, which names the
    attribute and nothing else -- no file, no rule, and no indication that the
    value is composed from locality.
    """
    from django.conf import settings  # noqa: PLC0415 - deleted through Django's own override machinery

    with override_settings():
        delattr(settings, WATCHLIST_PATH_SETTING)

        with pytest.raises(ImproperlyConfigured) as refused:
            apps.get_app_config(COLLECTORS_APP_LABEL).ready()

    assert WATCHLIST_PATH_SETTING in str(refused.value)
