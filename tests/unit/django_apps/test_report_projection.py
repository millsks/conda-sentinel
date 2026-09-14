"""`surface/reports.py`'s projection, on the two questions a database cannot ask.

`tests/integration/django_apps/test_reports.py` runs every report against real rows,
which is where a wrong ORM path or a wrong filter shows. What it cannot reach is what
the projection does with a value the database *could* hand it but does not today --
and `EMPTY` exists precisely for that value.

**Blank is reserved, and the reservation is what needs a test.** `CPM-AD-24` gives
blank one meaning -- a field with no value -- and forbids it for a status. Every
status column in this product is non-null with a sentinel default, so the `None`
branch is unreachable through any report on the roster today. A branch nobody can
reach is a branch that quietly stops being right, and the day a nullable column joins
a report is the day it matters. So it is exercised here directly, against
`report_rows`, which takes settled tuples and needs no database at all.
"""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from typing import Final

from conda_sentinel.surface.labels import display_label
from conda_sentinel.surface.reports import ABSENT_FROM_THE_INVENTORY
from conda_sentinel.surface.reports import EMPTY
from conda_sentinel.surface.reports import REPORTS
from conda_sentinel.surface.reports import REPORTS_BY_SLUG
from conda_sentinel.surface.reports import report_rows

#: The report the cases below project. Any would do; this one is named rather than
#: taken from the front of the roster, so a reordering does not silently change what
#: is under test.
A_REPORT: Final[str] = "kev"

NOW: Final[datetime] = datetime(2026, 9, 8, 9, 30, tzinfo=UTC)
A_VERSION_MAP: Final[dict[str, str]] = {"vulnerability": "2026.09.4"}


def a_row(*values: object) -> tuple[object, ...]:
    """Return one raw row as the values queryset would hand it over.

    Args:
        *values: The column values, in `all_columns()` order.

    Returns:
        The values with the row's version map and the two inventory-absence
        instants appended -- `None` for a listed package -- which is the shape
        `report_values` selects (`CPM-OPERATE-S11` added the pair).

    """
    return (*values, A_VERSION_MAP, None, None)


def test_a_field_with_no_value_renders_blank() -> None:
    """The half of `CPM-AD-24` that blank is *for*.

    A column the database answered `None` for is a field with no value, and blank is
    the honest rendering of that -- unlike the status beside it, which is never
    absent.
    """
    report = REPORTS_BY_SLUG[A_REPORT]

    produced = report_rows(report, [a_row("aiohttp", "verified", NOW, NOW, "advisories_matched", None)])

    assert produced.rows[0][-1] == EMPTY


def test_every_other_value_is_rendered_as_itself() -> None:
    """So the case above is about `None` rather than about the renderer being lossy.

    The statuses in particular come out as the policy engine wrote them, which is the
    whole of `CPM-AD-24` reaching the artifact that leaves the system.
    """
    report = REPORTS_BY_SLUG[A_REPORT]

    produced = report_rows(report, [a_row("aiohttp", "verified", NOW, NOW, "advisories_matched", "critical")])

    assert produced.rows[0][0] == "aiohttp"
    assert produced.rows[0][1] == "verified"
    assert produced.rows[0][-2] == "advisories_matched"
    assert produced.rows[0][-1] == "critical"


def test_a_report_of_no_rows_was_produced_from_nothing() -> None:
    """`None` rather than a stamp borrowed from somewhere else.

    An empty report inventing a cut-off is the one lie it is able to tell, and it is
    the lie a reader has no way to check.
    """
    produced = report_rows(REPORTS_BY_SLUG[A_REPORT], [])

    assert produced.rows == ()
    assert produced.evidence_cutoff is None
    assert produced.policy_versions == ()


def test_a_page_names_every_policy_version_its_rows_carry() -> None:
    """`CPM-AD-11` stamps a map per row, and a replay leaves rows from two runs.

    A report claiming a single version over rows produced at two would be stating
    something false about its own provenance -- to the compliance reviewer, who is
    the reader most likely to check it.
    """
    report = REPORTS_BY_SLUG[A_REPORT]
    older = (
        "aiohttp",
        "verified",
        NOW,
        NOW,
        "advisories_matched",
        "critical",
        {"vulnerability": "2026.09.3"},
        None,
        None,
    )
    newer = (
        "cryptography",
        "verified",
        NOW,
        NOW,
        "advisories_matched",
        "high",
        {"vulnerability": "2026.09.4"},
        None,
        None,
    )

    produced = report_rows(report, [older, newer])

    assert produced.policy_versions == ("vulnerability@2026.09.3", "vulnerability@2026.09.4")


def test_the_cut_off_is_the_newest_the_rows_carry() -> None:
    """The freshest, so a page of mixed rows is not dated by its oldest member.

    Every row carries its own stamps besides, which is what keeps a mixed page
    visible rather than averaged away.
    """
    report = REPORTS_BY_SLUG[A_REPORT]
    older = datetime(2026, 9, 1, tzinfo=UTC)

    produced = report_rows(
        report,
        [
            a_row("aiohttp", "verified", older, older, "advisories_matched", "critical"),
            a_row("cryptography", "verified", NOW, NOW, "advisories_matched", "high"),
        ],
    )

    assert produced.evidence_cutoff == NOW


def test_an_absent_package_is_tagged_in_the_name_cell_on_the_screen_and_not_in_the_values() -> None:
    """`CPM-OPERATE-S11`: the report labels an absent package where a reader sees it.

    The tag is built from the two instants the query carries after the version
    map, appended to the name cell by `readable_rows` -- and *only* there. `rows`
    is what the CSV and the API carry, and it is unchanged: the tag is a label,
    not a column, on the terms `CPM-AD-24` keeps a status verbatim.
    """
    report = REPORTS_BY_SLUG[A_REPORT]
    since = datetime(2026, 9, 6, 2, 0, tzinfo=UTC)
    last_listed = datetime(2026, 9, 5, 2, 0, tzinfo=UTC)
    row = ("aiohttp", "verified", NOW, NOW, "advisories_matched", "critical", A_VERSION_MAP, since, last_listed)

    produced = report_rows(report, [row, a_row("cryptography", "verified", NOW, NOW, "advisories_matched", "high")])

    assert produced.rows[0][0] == "aiohttp"
    assert produced.absence_tags == (
        "absent from the inventory since 2026-09-06 (last listed 2026-09-05)",
        "",
    )
    readable = produced.readable_rows()
    assert readable[0][0] == "aiohttp \u00b7 absent from the inventory since 2026-09-06 (last listed 2026-09-05)"
    assert readable[1][0] == "cryptography"
    assert readable[0][1:] == tuple(
        display_label(cell) if col.is_status else cell
        for cell, col in zip(produced.rows[0][1:], produced.columns[1:], strict=True)
    )


def test_only_the_feedstock_gap_report_declares_an_exclusion() -> None:
    """The epic's rule as data: one surface may exclude a package, and it says so.

    Every other report carries absent packages and labels them. A second report
    growing an `excludes` is a decision this case makes somebody take on purpose.
    """
    excluding = [report.slug for report in REPORTS if report.excludes is not None]

    assert excluding == ["feedstock-lag"]
    assert REPORTS_BY_SLUG["feedstock-lag"].excludes is ABSENT_FROM_THE_INVENTORY
    assert ABSENT_FROM_THE_INVENTORY.reason == "absent from the inventory at the run's cut-off"
