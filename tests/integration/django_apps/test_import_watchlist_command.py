"""`CPM-OPERATE-S03`: `manage.py import_watchlist`, the fourth admin process, through `call_command`.

The command parses through `records_from` -- so every refusal about the file is
the parser's, naming the file and the line -- and hands the records to the
service unattended, one audited transaction per row. What is asserted here is
the command's own contract: the default path is the file locality selects, a
named path is read instead, `--replace` retires, `--reason` reaches every audit
row, a malformed file is a `CommandError` before any write, and the report line
carries the four counts.

Every test rolls back: `@pytest.mark.django_db` wraps each in a transaction.
"""

from __future__ import annotations

from io import StringIO
from typing import TYPE_CHECKING
from typing import Final

import pytest
from django.conf import settings
from django.core.management import call_command
from django.core.management.base import CommandError

from conda_sentinel.collectors.apps import WATCHLIST_PATH_SETTING
from conda_sentinel.collectors.models import InventoryChange
from conda_sentinel.collectors.models import InventoryEntry
from conda_sentinel.collectors.watchlist import WATCHLIST_ENCODING
from conda_sentinel.collectors.watchlist import records_from
from conda_sentinel.collectors.watchlist import watchlist_path

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.integration

COMMAND: Final[str] = "import_watchlist"

HEADER: Final[str] = (
    "source_package_key,package_name,internal_component_count,internal_lob_count,apps,platforms,downloads,versions\n"
)
TWO_ROWS: Final[str] = HEADER + "conda-forge/numpy,numpy,12,3,,,,\nconda-forge/pandas,pandas,8,2,5,,,\n"
ONE_ROW: Final[str] = HEADER + "conda-forge/numpy,numpy,12,3,,,,\n"
A_RAGGED_FILE: Final[str] = HEADER + "conda-forge/numpy,numpy,12\n"

#: The development watchlist, which is what the default path selects inside a
#: local run.
THE_DEVELOPMENT_WATCHLIST: Final[Path] = watchlist_path(local=True)


def _run(*arguments: str) -> str:
    """Invoke the command and return what it wrote to stdout.

    Args:
        *arguments: Its command-line arguments.

    Returns:
        Everything the command wrote.

    """
    output = StringIO()
    call_command(COMMAND, *arguments, stdout=output)
    return output.getvalue()


@pytest.fixture
def a_file(tmp_path: Path) -> Path:
    """Return a two-row watchlist on disk.

    Args:
        tmp_path: pytest's per-test directory.

    Returns:
        The file.

    """
    path = tmp_path / "watchlist.csv"
    path.write_text(TWO_ROWS, encoding="utf-8")
    return path


@pytest.mark.django_db
def test_the_default_path_is_the_file_locality_selects() -> None:
    """No argument: the development watchlist inside a local run, and the report names it."""
    assert settings.INVENTORY_WATCHLIST_PATH == THE_DEVELOPMENT_WATCHLIST
    expected = len(
        records_from(
            THE_DEVELOPMENT_WATCHLIST.read_text(encoding=WATCHLIST_ENCODING), watchlist=THE_DEVELOPMENT_WATCHLIST
        ),
    )

    line = _run()

    assert str(THE_DEVELOPMENT_WATCHLIST) in line
    assert f"{expected} added" in line
    assert InventoryEntry.objects.count() == expected
    assert set(InventoryChange.objects.values_list("origin", flat=True)) == {str(THE_DEVELOPMENT_WATCHLIST)}
    assert set(InventoryChange.objects.values_list("actor", flat=True)) == {None}


@pytest.mark.django_db
def test_a_named_path_is_imported_instead(a_file: Path) -> None:
    """The positional argument selects the file; the report carries the four counts."""
    line = _run(str(a_file))

    assert line.strip() == f"imported {a_file}: 2 added, 0 changed, 0 unchanged, 0 retired, 0 retired kept"
    assert InventoryEntry.objects.count() == 2  # noqa: PLR2004 - the two rows
    numpy = InventoryEntry.objects.get(source_package_key="conda-forge/numpy")
    assert (numpy.internal_component_count, numpy.internal_lob_count, numpy.apps) == (12, 3, None)


@pytest.mark.django_db
def test_a_repeated_import_reports_unchanged(a_file: Path) -> None:
    """The second run of the same file writes nothing and says so."""
    _run(str(a_file))

    line = _run(str(a_file))

    assert "0 added, 0 changed, 2 unchanged, 0 retired, 0 retired kept" in line
    assert InventoryChange.objects.count() == 2  # noqa: PLR2004 - the two adds, nothing more


@pytest.mark.django_db
def test_replace_retires_what_the_shorter_file_no_longer_names(a_file: Path, tmp_path: Path) -> None:
    """The acceptance criterion's `--replace`: retired, never deleted, and the line says `(--replace)`."""
    _run(str(a_file))
    shorter = tmp_path / "shorter.csv"
    shorter.write_text(ONE_ROW, encoding="utf-8")

    line = _run(str(shorter), "--replace")

    assert "0 added, 0 changed, 1 unchanged, 1 retired (--replace), 0 retired kept" in line
    assert InventoryEntry.objects.count() == 2  # noqa: PLR2004 - nothing deleted
    pandas = InventoryEntry.objects.get(source_package_key="conda-forge/pandas")
    assert pandas.retired_at is not None
    assert str(shorter) in pandas.reason


@pytest.mark.django_db
def test_the_reason_reaches_every_audit_row(a_file: Path) -> None:
    """`--reason` is what the rows say; without it, the file is named."""
    _run(str(a_file), "--reason", "quarterly review")

    assert set(InventoryChange.objects.values_list("reason", flat=True)) == {"quarterly review"}
    assert set(InventoryEntry.objects.values_list("reason", flat=True)) == {"quarterly review"}


@pytest.mark.django_db
def test_a_malformed_file_is_refused_naming_the_line_before_any_write(tmp_path: Path) -> None:
    """Every parser refusal applies: a ragged row is a `CommandError` naming the file and the line, nothing written."""
    ragged = tmp_path / "ragged.csv"
    ragged.write_text(A_RAGGED_FILE, encoding="utf-8")

    with pytest.raises(CommandError, match=r"ragged row on line 2") as refused:
        _run(str(ragged))

    assert str(ragged) in str(refused.value)
    assert InventoryEntry.objects.count() == 0


@pytest.mark.django_db
def test_a_missing_file_is_refused_naming_it(tmp_path: Path) -> None:
    """A path naming nothing is a `CommandError`, not a traceback."""
    missing = tmp_path / "nowhere.csv"

    with pytest.raises(CommandError, match=r"could not be read"):
        _run(str(missing))

    assert InventoryEntry.objects.count() == 0


@pytest.mark.django_db
def test_a_header_only_file_is_refused_as_awaiting_review(tmp_path: Path) -> None:
    """The production file's shipped shape: refused rather than imported as an inventory of nothing."""
    empty = tmp_path / "watchlist.csv"
    empty.write_text(HEADER, encoding="utf-8")

    with pytest.raises(CommandError, match=r"names no packages"):
        _run(str(empty))


@pytest.mark.django_db
def test_the_default_path_setting_is_the_one_the_command_reads(monkeypatch: pytest.MonkeyPatch, a_file: Path) -> None:
    """The command reads `INVENTORY_WATCHLIST_PATH` by the name `CollectorsConfig` reads it, not a second one.

    Args:
        monkeypatch: pytest's patcher.
        a_file: A file to point the setting at.

    """
    monkeypatch.setattr(settings, WATCHLIST_PATH_SETTING, a_file)

    line = _run()

    assert str(a_file) in line
    assert InventoryEntry.objects.count() == 2  # noqa: PLR2004 - the two rows


@pytest.mark.django_db
def test_replace_with_a_reason_composes_it_onto_the_retirements(a_file: Path, tmp_path: Path) -> None:
    """Both flags: the retired row says why and that the file dropped it."""
    _run(str(a_file))
    shorter = tmp_path / "shorter.csv"
    shorter.write_text(ONE_ROW, encoding="utf-8")

    _run(str(shorter), "--replace", "--reason", "quarterly review")

    pandas = InventoryEntry.objects.get(source_package_key="conda-forge/pandas")
    assert pandas.reason.startswith("quarterly review; absent from ")
    assert str(shorter) in pandas.reason


@pytest.mark.django_db
def test_a_retired_row_the_file_still_names_is_kept_retired_and_reported(a_file: Path) -> None:
    """The command's line carries `retired kept`, and the row is left as the person left it."""
    from django.contrib.auth.models import Group  # noqa: PLC0415 - local to this case

    from conda_sentinel.collectors.inventory import retire_entry  # noqa: PLC0415 - as above
    from conda_sentinel.core.clock import SystemClock  # noqa: PLC0415 - as above
    from tests.factories import UserFactory  # noqa: PLC0415 - as above

    _run(str(a_file))
    leader = UserFactory.create(username="a-leader")
    leader.groups.add(Group.objects.get(name=settings.ROLE_CONTRACT.leadership))
    retire_entry(source_package_key="conda-forge/pandas", actor=leader, reason="dropped", clock=SystemClock())

    line = _run(str(a_file), "--replace")

    assert "1 unchanged, 0 retired (--replace), 1 retired kept" in line
    assert InventoryEntry.objects.get(source_package_key="conda-forge/pandas").retired_at is not None


@pytest.mark.django_db
def test_a_relative_path_is_resolved_so_two_runs_record_one_origin(
    a_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Given relatively once and absolutely once, the file is one `origin` on every audit row."""
    monkeypatch.chdir(a_file.parent)

    _run(a_file.name)
    _run(str(a_file), "--reason", "again")

    origins = set(InventoryChange.objects.values_list("origin", flat=True))
    assert origins == {str(a_file.resolve())}
