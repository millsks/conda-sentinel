"""`CPM-FR-26`'s six recurring reports, declared once so none can forget its provenance.

A report is a question somebody asks on a schedule: what is exploited today, what has
drifted this week, what nobody has identified. The six `CPM-APP-S06` names are
different questions over the same rollup, and the temptation is to write six views.

**They are one mechanism and a roster, and the reason is AC 2.** Every report has to
state the evidence cut-off and the policy version it was produced from -- and a rule
that six views each have to remember is a rule five of them keep. Here the freshness
comes from the projection rather than from the report, so a seventh report added
tomorrow states its provenance because it cannot do otherwise.

**A report is a queryset and a column list, and nothing else.** No report computes a
verdict: `CPM-AD-10` gives that to the policy engine, and a report that derived
anything would be a second opinion nobody could reconcile with the health view. What
a report chooses is *which rows* and *which columns* -- a filter and a projection.

**Statuses are emitted verbatim, and blank is reserved.** `CPM-AD-24` says a derived
status appears as its `OutcomeState` value on every surface, and names the failure it
prevents: "the export rendering `unknown` as a blank cell -- destroying the five
states in the one artifact that leaves the system". An export is the artifact that
leaves, so this is where that rule matters most. `EMPTY` exists to be asserted
against rather than used.

**The export is the same rows as the page.** Not a second query with its own filters
-- that is how an export comes to disagree with the screen somebody exported it from,
which is the disagreement nobody notices until it is in a board pack.

**One report may exclude packages, and it says so.** `CPM-OPERATE-S11`: the
feedstock-gap report is the only surface allowed to leave a package out -- a
package the inventory no longer lists has no feedstock anybody needs maintained --
and it declares the exclusion as data (`Report.excludes`) so the page can state
"N packages excluded" with the reason, from the same query. Every other report
carries absent packages and labels them; nothing else in the product excludes one.

**On the `AD-` prefix.** A bare `AD-n` in this repository is an *inherited* platform
decision; a decision from this product's own architecture spine always carries the
`CPM-` prefix.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import replace
from typing import TYPE_CHECKING
from typing import Final
from typing import cast

from django.db.models import Q
from django.utils.translation import gettext_lazy as _

from conda_sentinel.core.models import PackageHealth
from conda_sentinel.core.outcomes import OutcomeState
from conda_sentinel.identity.confidence import IdentityConfidence
from conda_sentinel.surface.labels import absence_tag
from conda_sentinel.surface.labels import display_label
from conda_sentinel.surface.search import name_condition

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime

    from django.db.models import QuerySet
    from django.utils.functional import _StrPromise


__all__ = [
    "ABSENT_FROM_THE_INVENTORY",
    "EMPTY",
    "REPORTS",
    "REPORTS_BY_SLUG",
    "Exclusion",
    "Report",
    "ReportColumn",
    "ReportPage",
    "excluded_count",
    "exclusion_sentence",
    "report_page",
    "report_rows",
    "report_values",
]

#: Where the version map sits in a raw row: after the report's columns, before
#: the two absence instants `report_values` appends.
VERSIONS_POSITION: Final[int] = -3

#: What a *field with no value* renders as, and what a status never renders as.
#:
#: `CPM-AD-24` reserves blank for the first and forbids it for the second. Declared so
#: `tests/unit/django_apps/test_report_projection.py` can assert against it rather than
#: against a literal written twice.
EMPTY: Final[str] = ""

#: How often each report is meant to be read, as the schedule `CPM-FR-26` names.
#:
#: Carried on the report rather than in a scheduler, because it is the *reader's*
#: cadence and not the system's: nothing here fires on a timer. `CPM-AD-20` puts
#: cadences in `django_celery_beat` for collection and policy runs; a report is
#: produced when somebody opens it, and this is what the page tells them it is for.
DAILY: Final[str] = "daily"
WEEKLY: Final[str] = "weekly"
ON_DEMAND: Final[str] = "on demand"


@dataclass(frozen=True, slots=True)
class ReportColumn:
    """One column of a report: a heading, and where the value comes from.

    `source` is an ORM field path read with `values_list`, never a callable. A
    callable column would let a report compute something, and a report that computed
    anything would be a second opinion about a package that nobody could reconcile
    with the health view.
    """

    heading: str
    source: str

    #: Whether the value is a derived status. Statuses are emitted verbatim and are
    #: never blank; everything else may legitimately have no value. The flag is what
    #: lets one assertion cover every report rather than one per column.
    is_status: bool = False


#: Where a row's package name comes from -- the column the screen tags when the
#: inventory no longer lists the package (`CPM-OPERATE-S11`).
NAME_SOURCE: Final[str] = "package__canonical_name"

#: The columns every report carries, whatever else it shows.
#:
#: `CPM-APP-S06`'s AC 3: an export "carries the same freshness and confidence columns
#: the application shows". Prepended to every report's own columns by construction --
#: a report cannot omit them, which is the point. `CPM-AD-11` requires the same of
#: every view, so this is that rule reaching the artifact that leaves the system.
COMMON_COLUMNS: Final[tuple[ReportColumn, ...]] = (
    ReportColumn(heading="Package", source=NAME_SOURCE),
    ReportColumn(heading="Confidence", source="confidence", is_status=True),
    ReportColumn(heading="Evidence cut-off", source="evidence_cutoff"),
    ReportColumn(heading="Computed at", source="computed_at"),
)


@dataclass(frozen=True, slots=True)
class Exclusion:
    """What a report leaves out, and the sentence the page states for it.

    Declared beside the report's condition rather than folded into it, because
    the two are read differently: the condition is what the report is *about*,
    and the exclusion is what it is deliberately not about -- which the page has
    to say, with a count, or a reader concludes the excluded packages are fine.
    """

    #: The rows to leave out, among those the condition would otherwise select.
    condition: Q

    #: Why, in the words the page prints after the count. A translation promise:
    #: this is a label a reader sees, not stored text, and it is rendered through
    #: `str()` wherever it leaves the process -- the page, the CSV's provenance
    #: line, the API.
    reason: _StrPromise


#: The one exclusion any report declares (`CPM-OPERATE-S11`): packages the
#: inventory no longer listed at the run's cut-off, read off the rollup's own
#: column. Spelled once so the report and the test that pins the sentence agree.
ABSENT_FROM_THE_INVENTORY: Final[Exclusion] = Exclusion(
    condition=Q(inventory_absent_since__isnull=False),
    reason=_("absent from the inventory at the run's cut-off"),
)


@dataclass(frozen=True, slots=True)
class Report:
    """One recurring question, as a filter and a set of columns."""

    #: What the URL calls it.
    slug: str

    #: What the heading says.
    title: str

    #: What question it answers, in a sentence a reader who did not commission it
    #: can act on. Shown on the page: a report nobody can interpret is a report
    #: somebody exports and misreads.
    asks: str

    #: How often it is meant to be read.
    cadence: str

    #: Which rollup rows it selects.
    condition: Q

    #: Its own columns, after the common ones.
    columns: tuple[ReportColumn, ...] = ()

    #: What it leaves out, if anything. `None` for five of the six: only the
    #: feedstock-gap report excludes a package, and it is the only surface in the
    #: product allowed to (`CPM-OPERATE-S11`).
    excludes: Exclusion | None = None

    def all_columns(self) -> tuple[ReportColumn, ...]:
        """Return every column, common ones first.

        Returns:
            The freshness and confidence columns, then the report's own. Composed
            here rather than written into each report, so none can omit them.

        """
        return COMMON_COLUMNS + self.columns


#: The six reports `CPM-APP-S06` names.
#:
#: Six, and the list is the acceptance criterion restated as data -- so a report
#: dropped in a refactor fails a test that names the requirement rather than one that
#: counts entries.
REPORTS: Final[tuple[Report, ...]] = (
    Report(
        slug="kev",
        title="Known-exploited vulnerabilities",
        asks="Which packages carry an advisory the CISA catalogue lists as exploited?",
        cadence=DAILY,
        condition=Q(package__vulnerability_policy_findings__kev_membership="listed"),
        columns=(
            ReportColumn(
                heading="Vulnerability",
                source="package__vulnerability_policy_findings__vulnerability_status",
                is_status=True,
            ),
            ReportColumn(heading="Risk", source="package__vulnerability_policy_findings__risk_level"),
        ),
    ),
    Report(
        slug="feedstock-lag",
        title="Feedstock lag and gaps",
        asks="Which packages are behind upstream, or have a feedstock nobody is maintaining?",
        cadence=WEEKLY,
        condition=Q(currency_status="behind") | ~Q(feedstock_presence_status="present_and_maintained"),
        columns=(
            ReportColumn(heading="Currency", source="currency_status", is_status=True),
            ReportColumn(heading="Feedstock", source="feedstock_presence_status", is_status=True),
        ),
        # A package the organisation no longer runs has no feedstock anybody needs
        # to maintain; listing it here would be a gap nobody should fill. Stated
        # on the page with its count, never silent.
        excludes=ABSENT_FROM_THE_INVENTORY,
    ),
    Report(
        slug="python-314",
        title="Python 3.14 readiness",
        asks="What is not ready for Python 3.14, and did a build say so or did metadata?",
        cadence=ON_DEMAND,
        condition=Q(
            package__python_readiness_policy_findings__readiness__in=("verified_not_ready", "inferred_not_ready"),
        ),
        columns=(
            ReportColumn(
                heading="Readiness",
                source="package__python_readiness_policy_findings__readiness",
                is_status=True,
            ),
            ReportColumn(heading="Evidence", source="package__python_readiness_policy_findings__evidence_type"),
        ),
    ),
    Report(
        slug="licence-exceptions",
        title="Licence exceptions pending",
        asks="Which licences the policy did not clear are waiting on somebody?",
        cadence=WEEKLY,
        condition=Q(package__license_policy_findings__license_outcome__in=("manual_review", "restricted", "forbidden")),
        columns=(
            ReportColumn(
                heading="Licence",
                source="package__license_policy_findings__license_outcome",
                is_status=True,
            ),
            ReportColumn(heading="Rule", source="package__license_policy_findings__matched_rule"),
        ),
    ),
    Report(
        slug="unmapped-identities",
        title="Unmapped identities",
        asks="Which packages has nothing identified, so every verdict on them is unknown?",
        cadence=DAILY,
        condition=Q(confidence=IdentityConfidence.UNMAPPED),
        columns=(ReportColumn(heading="Resolved from", source="package__identity_source"),),
    ),
    Report(
        slug="stale-evidence",
        title="Stale evidence and collector failures",
        asks="What has the monitor been unable to see, and where did a lookup break?",
        cadence=DAILY,
        # The four sentinels across the rollup's own columns. A report of what the
        # product *could not establish*, which is the one `CPM-FR-5` makes
        # load-bearing and the one a happy dashboard never shows.
        condition=(
            Q(currency_status__in=(OutcomeState.UNKNOWN.value, OutcomeState.ERROR.value))
            | Q(feedstock_presence_status__in=(OutcomeState.UNKNOWN.value, OutcomeState.ERROR.value))
        ),
        columns=(
            ReportColumn(heading="Currency", source="currency_status", is_status=True),
            ReportColumn(heading="Feedstock", source="feedstock_presence_status", is_status=True),
        ),
    ),
)

#: The roster by slug, for a URL.
REPORTS_BY_SLUG: Final[dict[str, Report]] = {report.slug: report for report in REPORTS}


@dataclass(frozen=True, slots=True)
class ReportPage:
    """One report, produced: its rows and the provenance they came from."""

    report: Report
    columns: tuple[ReportColumn, ...]

    #: One tuple per row, in `columns` order, already rendered to strings.
    rows: tuple[tuple[str, ...], ...]

    #: AC 2. Read off the rows rather than passed in, so a report cannot be produced
    #: without them -- and `None` where there are no rows at all, which is honest: a
    #: report of nothing was produced from nothing.
    evidence_cutoff: datetime | None
    policy_versions: tuple[str, ...]

    #: The text tag each row's package carries when the inventory no longer lists
    #: it (`CPM-OPERATE-S11`), in `rows` order; `""` for a listed package. Read
    #: off the same query as the rows, and printed by `readable_rows` in the name
    #: cell. Not a column: the CSV and the API carry the row as its columns, and
    #: the tag is how the *screen* labels the package -- the health table's own
    #: arrangement, one page over.
    absence_tags: tuple[str, ...] = ()

    #: How many rows the report's declared exclusion left out of this report,
    #: over the same condition and search as `rows`. Zero when the report
    #: declares none. Per report rather than per page: the sentence is about the
    #: report a reader is looking at, not about the page they are on.
    excluded: int = 0

    def exclusion(self) -> str:
        """Return the sentence this page states for what the report left out, or nothing.

        Returns:
            `exclusion_sentence` over this page's report and count.

        """
        return exclusion_sentence(self.report, excluded=self.excluded)

    def readable_rows(self) -> tuple[tuple[str, ...], ...]:
        """Return the rows as the **screen** shows them, statuses labelled.

        `rows` stays what it is -- the values, in `columns` order -- because that is
        what the CSV writes and what `CPM-AD-24` binds. This is the HTML's view of the
        same rows, and the two differ in exactly one way: a cell in a column declared
        `is_status` is rendered through `display_label`.

        **Only status columns.** A report's other cells are package names, timestamps
        and policy versions, and sentence-casing those would turn `django` into
        `Django` and a version into prose -- which is why this pairs each cell with
        its column rather than mapping over the row.

        **And the name cell carries the absence tag**, appended after the name
        and a middle dot, so a report row about a package the inventory no longer
        lists says so on the screen as the health table's name cell does. The CSV
        is unchanged: the tag is a label, and `rows` is the values.

        Returns:
            One tuple per row, in `columns` order, ready to print.

        """
        tags = self.absence_tags or ("",) * len(self.rows)
        return tuple(
            tuple(
                _readable_cell(cell, column, tag=tag if column.source == NAME_SOURCE else "")
                for cell, column in zip(row, self.columns, strict=True)
            )
            for row, tag in zip(self.rows, tags, strict=True)
        )


def report_values(report: Report, *, search: str = "") -> QuerySet[PackageHealth, tuple[object, ...]]:
    """Return one report's raw rows, ordered and unbounded, as a values queryset.

    Split out from `report_page` by `CPM-APP-S07`, which paginates a report over
    HTTP: a paginator needs something it can count and slice, and it must be the
    *same* thing the page and the export read. A second queryset built for the API
    is `CPM-AD-24`'s named failure with an extra step.

    `CPM-APP-S18` adds the name search **here** for that same reason. The export view
    states the principle plainly -- "the same rows as the page, from the same
    projection with a different bound, not a second query with its own filters" -- so
    a search the page honoured and the export did not would be exactly the
    disagreement that module was written to prevent, in the one artifact that leaves
    the system.

    Args:
        report: Which report.
        search: A package-name fragment, already normalised by `search_term`. An
            empty one narrows nothing.

    Returns:
        One tuple per row -- the report's columns in order, then the row's version
        map, then the two inventory-absence instants the screen's tag is built
        from. Not evaluated: the caller slices it.

    """
    columns = report.all_columns()
    return (
        _selected(report, search=search)
        .order_by("package__canonical_name", "pk")
        .values_list(
            *(column.source for column in columns),
            "policy_versions",
            "inventory_absent_since",
            "inventory_last_listed",
        )
        .distinct()
    )


def _asked_for(report: Report, *, search: str) -> QuerySet[PackageHealth]:
    """Return the rollup rows one report's condition and search select, exclusion not yet applied.

    The one place the condition and the search are combined, so the rows, the
    export, the API and the exclusion's count all read the same selection and
    cannot disagree about which rows were asked for.

    Args:
        report: Which report.
        search: A package-name fragment, already normalised.

    Returns:
        The matching rows, unordered and unbounded.

    """
    return PackageHealth.objects.filter(report.condition & name_condition(search, field=NAME_SOURCE))


def _selected(report: Report, *, search: str) -> QuerySet[PackageHealth]:
    """Return the rollup rows one report shows: what was asked for, less what it excludes.

    Args:
        report: Which report.
        search: A package-name fragment, already normalised.

    Returns:
        The rows, unordered and unbounded.

    """
    selected = _asked_for(report, search=search)
    if report.excludes is not None:
        selected = selected.exclude(report.excludes.condition)
    return selected


def excluded_count(report: Report, *, search: str = "") -> int:
    """Return how many packages the report's declared exclusion leaves out.

    The rows `_asked_for` selects *and* the exclusion matches -- the complement
    of `_selected` over the same condition and the same search, so a searched
    page counts what the search would have shown. Zero for a report that
    declares none, which is the honest answer rather than a special case.

    Args:
        report: Which report.
        search: The same fragment the rows were narrowed by, so the count is about
            the report the reader is looking at.

    Returns:
        The count of excluded packages.

    """
    if report.excludes is None:
        return 0
    return _asked_for(report, search=search).filter(report.excludes.condition).values("package_id").distinct().count()


def exclusion_sentence(report: Report, *, excluded: int) -> str:
    """Return the one sentence a report states for what it left out, or nothing.

    Spelled once so the page, the CSV's provenance line and the API's
    `excluded.reason` say the same thing.

    Args:
        report: Which report.
        excluded: How many packages the exclusion left out.

    Returns:
        `"N packages excluded: <reason>"`, singular at one, or `""` for a report
        that declares no exclusion.

    """
    if report.excludes is None:
        return ""
    if excluded == 1:
        return str(_("%(total)d package excluded: %(reason)s") % {"total": excluded, "reason": report.excludes.reason})
    return str(_("%(total)d packages excluded: %(reason)s") % {"total": excluded, "reason": report.excludes.reason})


def report_rows(report: Report, values: Sequence[tuple[object, ...]]) -> ReportPage:
    """Project settled rows into the report the surfaces render.

    Args:
        report: Which report.
        values: The rows, already sliced by a paginator or a cap. A sequence rather
            than a queryset because it must not be re-evaluated -- the shape
            `health_rows` takes, and for the same reason.

    Returns:
        The rendered rows and the provenance *of these rows*. Per page rather than
        per report, which is the honest read when a caller is walking one: the
        envelope says what the rows in this response were produced from, and every
        row carries its own stamps besides.

    """
    columns = report.all_columns()
    return ReportPage(
        report=report,
        columns=columns,
        rows=tuple(
            tuple(_rendered(value, column) for value, column in zip(row[: len(columns)], columns, strict=True))
            for row in values
        ),
        evidence_cutoff=_cutoff(values, columns),
        policy_versions=_versions(values),
        absence_tags=tuple(
            absence_tag(cast("datetime | None", row[-2]), cast("datetime | None", row[-1])) for row in values
        ),
    )


def report_page(report: Report, *, limit: int | None = None, search: str = "") -> ReportPage:
    """Produce one report.

    Args:
        report: Which report.
        limit: How many rows to take, for a page. `None` for all of them, which is
            what an export wants -- and what `CPM-APP-S08` bounds with a row cap.
        search: A package-name fragment, already normalised. An empty one narrows
            nothing.

    Returns:
        The rows and the provenance.

    """
    values = report_values(report, search=search)
    produced = report_rows(report, list(values[:limit] if limit is not None else values))
    return replace(produced, excluded=excluded_count(report, search=search))


def _rendered(value: object, column: ReportColumn) -> str:
    """Return one cell as the report emits it.

    Args:
        value: What the database returned.
        column: Which column it is.

    Returns:
        The status verbatim for a status column -- `CPM-AD-24` -- and the value's
        string form otherwise. `None` becomes blank, which is what blank is *for*: a
        field with no value. A status is never `None`, because every status column
        this product declares is non-null with a default.

    """
    if value is None:
        return EMPTY
    return str(value)


def _cutoff(values: Sequence[tuple[object, ...]], columns: Sequence[ReportColumn]) -> datetime | None:
    """Return the evidence cut-off the rows were produced from.

    Args:
        values: The raw rows.
        columns: The columns, to find the cut-off's position.

    Returns:
        The newest cut-off among the rows, or `None` for an empty report -- which is
        honest rather than a gap: a report of nothing was produced from nothing.

    """
    position = next(index for index, column in enumerate(columns) if column.source == "evidence_cutoff")
    instants = [cast("datetime", row[position]) for row in values if row[position] is not None]
    return max(instants) if instants else None


def _versions(values: Sequence[tuple[object, ...]]) -> tuple[str, ...]:
    """Return the policy versions the rows were produced at.

    Args:
        values: The raw rows, whose element at `VERSIONS_POSITION` is each row's
            version map.

    Returns:
        Every distinct `domain@version`, sorted. **Every** one rather than a single
        version, because `CPM-AD-11` stamps a *map* per domain and a replay can leave
        rows from more than one run -- a report claiming one version over rows
        produced at two would be stating something false about its own provenance.

    """
    seen = {
        f"{domain}@{version}"
        for row in values
        for domain, version in cast("dict[str, str]", row[VERSIONS_POSITION] or {}).items()
    }
    return tuple(sorted(seen))


def _readable_cell(cell: str, column: ReportColumn, *, tag: str) -> str:
    """Return one cell as the screen prints it.

    Args:
        cell: The value, as `rows` carries it.
        column: Which column it is.
        tag: The absence tag to append, or `""`.

    Returns:
        The label for a status column, the value otherwise -- with the tag after
        a separator when there is one.

    """
    readable = display_label(cell) if column.is_status else cell
    return f"{readable} \u00b7 {tag}" if tag else readable
