"""`CPM-FR-27`'s reads: current health, one package's detail, and the reports.

Three endpoints over the projections `surface/` already renders, so the API and the
screens are the same product read twice rather than two products. `CPM-AD-24` names
the failure that prevents -- "a new derived status reaching the API but not the
governed view" -- and the reason it cannot happen here is structural: these views
call `health_queryset`, `health_rows`, `traces_for` and `report_page`, which are the
functions the templates are rendered from. There is no query in this module.

**Paginated because the settings say so, not because these do.** `CPM-AD-12` puts
pagination in `REST_FRAMEWORK` globally and
`tests/unit/django_apps/test_pagination_audit.py` sweeps for a view that opted out,
so a `ListAPIView` here inherits the bound and the maximum page size by declaring
nothing at all. That is AC 2, and the fact that these classes are silent about it is
the point.

**Authorization is declared, never decided.** `CPM-AD-13`, and the same three roles
the screens require: `AnyProductRole` refuses an authenticated user who holds none of
them, which is the state the platform's `IsAuthenticated` floor lets through. The
declaration is a class attribute, so the audit can read it without running a request.

**Nothing here writes.** No `POST`, no `PUT`, no serializer with a `create`. AC 3's
two writes are `identity/api/` and `workflow/api/`, and
`tests/unit/django_apps/test_api_contract_audit.py` sweeps the resolver for a third.

**On the `AD-` prefix.** A bare `AD-n` in this repository is an *inherited* platform
decision; a decision from this product's own architecture spine always carries the
`CPM-` prefix.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from typing import Any
from typing import Final

from django.core.exceptions import BadRequest
from drf_spectacular.utils import extend_schema
from drf_spectacular.utils import extend_schema_view
from rest_framework.exceptions import NotFound
from rest_framework.exceptions import ValidationError
from rest_framework.generics import ListAPIView
from rest_framework.generics import RetrieveAPIView

from conda_sentinel.core.models import PackageHealth
from conda_sentinel.core.permissions import AnyProductRole
from conda_sentinel.surface.api.serializers import HealthRowSerializer
from conda_sentinel.surface.api.serializers import PackageDetailSerializer
from conda_sentinel.surface.api.serializers import ReportPageSerializer
from conda_sentinel.surface.api.serializers import ReportSerializer
from conda_sentinel.surface.api.serializers import report_exclusion
from conda_sentinel.surface.detail import identity_of
from conda_sentinel.surface.detail import traces_for
from conda_sentinel.surface.detail import work_on
from conda_sentinel.surface.health import health_rows
from conda_sentinel.surface.listing import SORT_PARAM
from conda_sentinel.surface.listing import health_queryset
from conda_sentinel.surface.queues import queue_items
from conda_sentinel.surface.reports import REPORTS
from conda_sentinel.surface.reports import REPORTS_BY_SLUG
from conda_sentinel.surface.reports import report_rows
from conda_sentinel.surface.reports import report_values
from conda_sentinel.workflow.api.permissions import queue_permission
from conda_sentinel.workflow.api.permissions import require_known_queue
from conda_sentinel.workflow.api.serializers import WorkflowItemSerializer

if TYPE_CHECKING:
    from collections.abc import Sequence

    from django.db.models import QuerySet
    from rest_framework.permissions import BasePermission
    from rest_framework.request import Request
    from rest_framework.response import Response

    from conda_sentinel.surface.reports import Report
    from conda_sentinel.workflow.models import WorkflowItem

__all__ = [
    "PackageDetailAPIView",
    "PackageHealthListAPIView",
    "QueueListAPIView",
    "ReportAPIView",
    "ReportRosterAPIView",
]

#: What a URL naming no declared report is told, on the HTML view's own terms.
#:
#: Imported rather than restated so a mistyped slug reads identically on both
#: surfaces -- an integrator and the reviewer who sent them the link are debugging
#: the same typo.
UNKNOWN_REPORT: Final[str] = "no report is called {slug!r}. The reports are {known}."


class PackageHealthListAPIView(ListAPIView):  # type: ignore[type-arg]
    """`CPM-FR-23` over HTTP: current health across the inventory, filtered and ranked.

    The same rows as `/packages/`, from the same queryset builder, projected by the
    same function. `?sort=` and every facet parameter mean here exactly what they
    mean there, which is what makes a URL a reviewer sends an integrator work.
    """

    permission_classes = (AnyProductRole,)
    serializer_class = HealthRowSerializer

    def get_queryset(self) -> QuerySet[PackageHealth]:
        """Return the rollup rows this request asked for.

        Returns:
            Whatever `surface/listing.py` builds.

        Raises:
            ValidationError: When a filter value is outside its vocabulary, carrying
                the vocabulary itself so a client can correct the call.

                **Translated rather than left to propagate**, which took running the
                endpoint to notice. `health_queryset` raises Django's `BadRequest`,
                because the HTML view wants Django's 400 page; DRF does not handle
                that exception, so it fell through to Django's handler and an
                integrator asking for `?vuln=nonsense` got the right status code
                wrapped in a page of HTML from a JSON API. The status was never
                wrong, which is exactly why no test would have found it.

        """
        try:
            return health_queryset(dict(self.request.GET.lists()), sort=self.request.GET.get(SORT_PARAM, ""))
        except BadRequest as refusal:
            raise ValidationError(str(refusal)) from refusal

    def get_serializer(self, *args: Any, **kwargs: Any) -> Any:
        """Return the serializer over the projected page, not over the rollup rows.

        The one method this class overrides, and the reason is `CPM-AD-24`. DRF hands
        a `ListAPIView` its page of model instances; serializing those would be a
        second projection -- no confidence gate, no evidence timestamps, and six
        columns that fall out of step with the screen the day a pass adds a seventh.
        `health_rows` is what the template renders from, so it is what this renders
        from.

        Args:
            *args: DRF's positional arguments, whose first is the page.
            **kwargs: DRF's keyword arguments.

        Returns:
            The serializer, over `HealthRow`s.

        """
        if args:
            page: Sequence[PackageHealth] = args[0]
            args = (health_rows(list(page)), *args[1:])
        return super().get_serializer(*args, **kwargs)


class PackageDetailAPIView(RetrieveAPIView):  # type: ignore[type-arg]
    """`CPM-FR-24` over HTTP: every status on one package, traced to its evidence.

    Keyed on the canonical name, like the screen -- so the URL an integrator stores
    beside a ticket says which package it is about. `CPM-AD-3` keeps the surrogate
    integer as the key everything else references, which is exactly what makes a
    corrected name a changed URL and not a broken foreign key.
    """

    permission_classes = (AnyProductRole,)
    serializer_class = PackageDetailSerializer
    lookup_field = "package__canonical_name"
    lookup_url_kwarg = "canonical_name"

    def get_queryset(self) -> QuerySet[PackageHealth]:
        """Return the rollup rows a detail URL may name.

        Returns:
            Every rollup row, with its package joined. `CPM-AD-11` gives every
            inventory package a row *including unmapped ones*, so there is no package
            in the inventory this cannot answer for -- which matters, because the
            unmapped ones are what an integrator automating the identity queue asks
            about.

        """
        return PackageHealth.objects.select_related("package")

    def get_serializer(self, *args: Any, **kwargs: Any) -> Any:
        """Return the serializer over the detail projection, not over the rollup row.

        Args:
            *args: DRF's positional arguments, whose first is the rollup row.
            **kwargs: DRF's keyword arguments.

        Returns:
            The serializer, over the same traces the screen shows.

        """
        if args:
            row: PackageHealth = args[0]
            args = (
                {
                    "canonical_name": row.package.canonical_name,
                    "computed_at": row.computed_at,
                    "evidence_cutoff": row.evidence_cutoff,
                    "policy_versions": row.policy_versions,
                    "inventory_absent_since": row.inventory_absent_since,
                    "inventory_last_listed": row.inventory_last_listed,
                    "identity": identity_of(row.package),
                    "traces": traces_for(row),
                    "work": work_on(row.package),
                },
                *args[1:],
            )
        return super().get_serializer(*args, **kwargs)


# Two report endpoints, and drf-spectacular derives both operation ids from the URL --
# which makes the roster and one report's rows collide on `reports_list`. Named here
# rather than left to a numeral suffix, because an operation id is what a generated
# client calls its method: `reports_list_2` is a name nobody can read.
#
# `extend_schema_view(get=...)` rather than `@extend_schema` on the class, which
# spectacular refuses for a generic view with a warning that says the schema will
# most likely be broken.
@extend_schema_view(get=extend_schema(operation_id="report_roster"))
class ReportRosterAPIView(ListAPIView):  # type: ignore[type-arg]
    """Which reports exist, what each asks, and how often it is meant to be read.

    Published rather than left to the schema, because the roster is *data*: a seventh
    report appears here the moment `REPORTS` grows one, and an integrator polling
    this discovers it without a client release.

    **Paginated, over six fixed entries.** Not because six needs paging, but because
    AC 2 says *any* collection endpoint is paginated and an endpoint exempted for
    being small today is the one that is not small in a year. Declaring nothing means
    it inherits the global bound, and `test_api_contract_audit.py` needs no exception
    for it.
    """

    permission_classes = (AnyProductRole,)
    serializer_class = ReportSerializer

    # The stubs type `get_queryset` as returning a `QuerySet`, which DRF's own
    # runtime does not require -- `ListAPIView` passes whatever it gets to the
    # paginator, and Django's paginator takes any sized sequence. The roster is
    # code rather than rows, so a list is the honest return and the ignore is the
    # narrow cost of saying so.
    def get_queryset(self) -> Sequence[Report]:  # type: ignore[override]
        """Return the roster.

        Returns:
            The reports, in the order `surface/reports.py` declares them. A list
            rather than a queryset -- the roster is code, not rows -- which Django's
            paginator handles the same way.

        """
        return list(REPORTS)


@extend_schema_view(get=extend_schema(operation_id="report_rows"))
class ReportAPIView(ListAPIView):  # type: ignore[type-arg]
    """One report, paginated, with the provenance `CPM-APP-S06` AC 2 requires.

    The same rows the screen shows and the export writes, from `report_values` --
    which exists because this endpoint needed something a paginator could count and
    slice, and it had to be the same thing the other two read.

    **Paginated rather than capped**, which the HTML page is not and the export is
    not. A cap on a screen is fine: a reader scrolls two hundred rows and refines the
    filter. A cap on an API is a wall -- an integrator with a report of four thousand
    rows can reach the first two hundred and has no way to ask for the rest, which is
    worse than either paging or refusing. `count` tells them how far it goes.

    **The columns are the contract.** They come out as a list, and each row as a list
    against it -- the CSV's shape. Keying rows by heading would make a renamed
    heading a breaking change to every client.

    **The provenance is the page's, not the report's.** An envelope claiming the
    whole report's cut-off over one page of it would be a statement about rows that
    are not in the response. Every row carries its own stamps besides, because
    `COMMON_COLUMNS` puts them on every report.
    """

    permission_classes = (AnyProductRole,)
    serializer_class = ReportPageSerializer

    def get_queryset(self) -> QuerySet[PackageHealth, tuple[object, ...]]:
        """Return the report's raw rows, unbounded, for the paginator to slice.

        Returns:
            The values queryset `report_values` builds.

        """
        return report_values(self.report())

    def report(self) -> Report:
        """Return the report this URL names.

        Returns:
            The report.

        Raises:
            NotFound: When the slug names none, with a message listing those that
                exist -- the same refusal the HTML view gives, so a mistyped slug
                reads the same on both surfaces.

        """
        slug = str(self.kwargs.get("slug", ""))
        report = REPORTS_BY_SLUG.get(slug)
        if report is None:
            raise NotFound(UNKNOWN_REPORT.format(slug=slug, known=sorted(REPORTS_BY_SLUG)))
        return report

    @extend_schema(responses=ReportPageSerializer)
    def list(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        """Return one page of the report, with the report's own metadata beside it.

        Args:
            request: The request.
            *args: DRF's positional arguments.
            **kwargs: DRF's keyword arguments.

        Returns:
            The paginator's envelope -- `count`, `next`, `previous`, `results` -- with
            the rows as `results` and the report, its columns and this page's
            provenance alongside.

        """
        report = self.report()
        page = self.paginate_queryset(self.get_queryset())
        produced = report_rows(report, list(page or ()))
        body = ReportPageSerializer(
            {
                "report": produced.report,
                "columns": [column.heading for column in produced.columns],
                "rows": [list(row) for row in produced.rows],
                "evidence_cutoff": produced.evidence_cutoff,
                "policy_versions": list(produced.policy_versions),
                "excluded": report_exclusion(report),
            },
        ).data
        response = self.get_paginated_response(body["rows"])
        response.data.update({key: value for key, value in body.items() if key != "rows"})
        return response


class QueueListAPIView(ListAPIView):  # type: ignore[type-arg]
    """One of `CPM-AD-22`'s three queues, ranked, scoped to the role that owns it.

    The same items in the same order as the screen, from `surface/queues.py` -- the
    ranking is the priority pass's own, and re-deriving one here would be a second
    ordering rule that disagreed with the screen the first time somebody changed one.

    **A queue that is not yours is refused, never returned empty.** The UX contract
    says so for the screen and it matters more here: an empty queue says there is no
    work, and an integrator whose credential quietly lost a role would report calm
    where there is a backlog.

    **The role comes from the queue, not from this class.** Which role may work a
    queue is a property of the queue; `workflow/api/permissions.py` reads
    `QUEUE_OWNERS` and this reads that.
    """

    serializer_class = WorkflowItemSerializer

    def initial(self, request: Request, *args: Any, **kwargs: Any) -> None:
        """Refuse a URL naming no queue before anything asks about roles.

        Args:
            request: The request.
            *args: DRF's positional URL arguments.
            **kwargs: DRF's keyword URL arguments, carrying the queue.

        """
        require_known_queue(str(kwargs.get("queue", "")))
        super().initial(request, *args, **kwargs)

    def get_permissions(self) -> list[BasePermission]:
        """Return the permission the queue in the URL requires.

        Returns:
            One permission, requiring the queue's owning role -- or a deny-all for a
            name `initial` has already refused. It does not raise: a schema generator
            calls this with no URL kwargs, and introspection must not decide what a
            request gets.

        """
        return [queue_permission(str(self.kwargs.get("queue", "")))()]

    def get_queryset(self) -> QuerySet[WorkflowItem]:
        """Return the queue named in the URL.

        Returns:
            The ranked, open items -- `get_permissions` has already refused an
            unknown queue.

        """
        return queue_items(str(self.kwargs["queue"]))
