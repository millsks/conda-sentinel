"""The export that left the request, and the one number that decides whether it does.

`CPM-APP-S08` AC 2 asks that the row cap come from **one** settings constant used by
every export path, and this module is the reason that is a real requirement rather
than a tidiness note. There are three paths to a report's rows -- the page, the
synchronous CSV, and the job below -- and each has an obvious place to write a
number. Two of them disagreeing is not a visible bug: the download is simply short,
and nothing says so.

So `over_the_cap()` is the only thing that asks the question, and
`CPM_SYNC_EXPORT_MAX_ROWS` is the only thing it asks.

**A job's export is unbounded, and that is the point rather than an oversight.** The
cap is not a limit on how much this product will export; it is the boundary of what
it will do *inside a request*. `CPM-AD-9` says work beyond it leaves, and work that
left and then truncated itself would have taken the cost of the boundary without the
benefit.

**The artifact is produced from the same projection the screen renders.** Not a
second query -- `CPM-AD-24`, and the same argument the synchronous export already
makes. A background export that disagreed with the screen somebody requested it from
would be the worst version of that failure, because nobody is watching when it runs.

**On the `AD-` prefix.** A bare `AD-n` in this repository is an *inherited* platform
decision; a decision from this product's own architecture spine always carries the
`CPM-` prefix.
"""

from __future__ import annotations

import csv
import io
from typing import TYPE_CHECKING
from typing import Final

from django.conf import settings

from conda_sentinel.surface.reports import REPORTS_BY_SLUG
from conda_sentinel.surface.reports import excluded_count
from conda_sentinel.surface.reports import exclusion_sentence
from conda_sentinel.surface.reports import report_rows
from conda_sentinel.surface.reports import report_values
from conda_sentinel.surface.search import SEARCH_PARAM
from conda_sentinel.surface.search import search_term

if TYPE_CHECKING:
    from conda_sentinel.core.clock import Clock
    from conda_sentinel.core.models import BackgroundJob
    from conda_sentinel.surface.reports import Report

__all__ = [
    "EXPORT_JOB_KIND",
    "REPORT_SLUG_PARAMETER",
    "UNKNOWN_REPORT_SLUG",
    "export_csv",
    "over_the_cap",
    "run_export_job",
]

#: What a report export's job rows carry in `kind`.
#:
#: Namespaced, because `core`'s registry is shared by whatever a later story adds and
#: a bare `"export"` would be the first name a second kind collided with.
EXPORT_JOB_KIND: Final[str] = "surface.report_export"

#: The parameter a report export is enqueued with.
REPORT_SLUG_PARAMETER: Final[str] = "slug"

#: What a job naming no declared report is failed with.
#:
#: A report can be *removed* between a job being enqueued and a worker picking it up,
#: which is a real sequence and not a defensive branch -- a deploy is all it takes.
#: Failing with the reason beats a traceback in a worker log that nobody reads.
UNKNOWN_REPORT_SLUG: Final[str] = "no report is called {slug!r}; it may have been removed since this job was queued."


def over_the_cap(report: Report, *, search: str = "") -> bool:
    """Report whether this report is too large to export inside a request.

    **The only place the cap is compared against anything.** AC 2 asks for one
    constant used by every export path, and one comparison is what makes that true
    rather than hoped for: three paths each reading the setting are three chances to
    read it slightly differently.

    **Measured against the searched rows, not the whole report.** `CPM-APP-S18`: a
    narrowed report is genuinely smaller, so a search that brings a four-thousand-row
    report under the cap should get the direct download -- and the page and the
    export must agree about which control they are offering, or a reader is shown a
    link that then refuses them.

    Args:
        report: Which report.
        search: The package-name fragment in force, already normalised.

    Returns:
        Whether it has more rows than `CPM_SYNC_EXPORT_MAX_ROWS`.

    """
    # `count()` rather than fetching and measuring: the whole point is not to pull
    # the rows into a request in order to discover that they should not be.
    cap: int = settings.CPM_SYNC_EXPORT_MAX_ROWS
    return report_values(report, search=search).count() > cap


def export_csv(report: Report, *, limit: int | None = None, search: str = "") -> tuple[str, int, str]:
    """Return one report as CSV, with the provenance that has to travel with it.

    Args:
        report: Which report.
        limit: How many rows, or `None` for all of them -- which is what a job wants,
            because a job's export is bounded by nothing: the cap is the boundary of
            a *request*, not a limit on what this product will produce.
        search: The package-name fragment in force, already normalised. The file has
            to hold the rows the screen was showing; see `report_values`.

    Returns:
        The CSV, how many rows it holds, and the provenance line.

    """
    values = report_values(report, search=search)
    produced = report_rows(report, list(values[:limit] if limit is not None else values))

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow([column.heading for column in produced.columns])
    writer.writerows(produced.rows)

    provenance = (
        f"evidence_cutoff={produced.evidence_cutoff or 'none'}; policy_versions={' '.join(produced.policy_versions)}"
    )
    # `CPM-OPERATE-S11`: a report that leaves packages out says so on the file
    # that leaves the system, in the same sentence the page states, so the CSV
    # cannot be read as complete by somebody who never saw the screen.
    if report.excludes is not None:
        provenance += f"; excluded={exclusion_sentence(report, excluded=excluded_count(report, search=search))}"
    return buffer.getvalue(), len(produced.rows), provenance


def run_export_job(*, job: BackgroundJob, clock: Clock) -> tuple[str, int]:
    """Produce the artifact for one report-export job.

    Registered by `surface/apps.py` at `ready()` rather than imported by `core`,
    which may not import this application at all -- the same inversion the pass
    registry and the after-run registry use.

    Args:
        job: The job, carrying the report slug.
        clock: The clock, unused here and taken anyway: `CPM-AD-26` makes the clock
            part of the runner contract, and a runner that quietly dropped it would
            be the one that later reaches for a wall clock.

    Returns:
        The CSV and its row count.

    Raises:
        JobRunnerError: When the job names no slug, or names a report that no longer
            exists. Both are failures worth stating: a job that produced an empty
            file instead would look like a report with nothing in it.

    """
    from conda_sentinel.core.jobs import JobRunnerError  # noqa: PLC0415 - raised here only
    from conda_sentinel.core.jobs import job_parameters  # noqa: PLC0415 - as above

    (slug,) = job_parameters(job, REPORT_SLUG_PARAMETER)
    report = REPORTS_BY_SLUG.get(slug)
    if report is None:
        raise JobRunnerError(UNKNOWN_REPORT_SLUG.format(slug=slug))

    # Read directly rather than through `job_parameters`, and the difference is the
    # point: that helper *refuses* a parameter that is missing or empty, because a
    # runner reading `None` for the slug would produce an artifact for the wrong
    # thing. A search is genuinely optional -- most exports have none -- so absent
    # means "the whole report", which is also what every job enqueued before
    # `CPM-APP-S18` carries.
    search = search_term(str(job.parameters.get(SEARCH_PARAM, "")))

    content, rows, _provenance = export_csv(report, search=search)
    return content, rows
