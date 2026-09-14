"""The one health queryset, so the screen and the API cannot become two projections.

`CPM-AD-24` requires that "every read surface projects the same values", and names
the failure it prevents: a new derived status reaching the API but not the governed
view. The obvious way to build `CPM-APP-S07`'s API is to write a second queryset with
the same filters and the same annotations, and the two would agree on the day they
were written and drift on the first day somebody changed one.

So there is one, here, and both surfaces call it. `surface/views.py` re-exports
`ORDERINGS` and `SORT_PARAM` because they were declared there first and tests and
templates name them; the definitions live here now, beside the queryset that uses
them.

**Filtering and ordering are the whole of what a surface may choose.** Which rows,
and in what order. Everything else -- the statuses, the freshness, the evidence -- is
`health_rows()`'s, and a surface that wanted a different value for a package would
have to go through that.

**The feedstock-gap surface excludes, and says so** (`CPM-OPERATE-S11`). The health
list filtered to `feedstock=absent` is the "feedstock gap" surface `CPM-APP-S05`
built, and it is one of the two surfaces the epic allows to leave a package out: a
package the inventory no longer listed at the run's cut-off is not a gap anybody
should fill, so it is excluded there -- on both the screen and the API, because
they read this one queryset -- and the page states the count with the reason. The
same page states the exclusion that was always there by construction: an
`unmapped` package reports `unknown` rather than `absent` (`CPM-AD-4`), so the facet
cannot list it, and until this story nothing said so.

**On the `AD-` prefix.** A bare `AD-n` in this repository is an *inherited* platform
decision; a decision from this product's own architecture spine always carries the
`CPM-` prefix.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from typing import Final

from django.core.exceptions import BadRequest
from django.db.models import Case
from django.db.models import IntegerField
from django.db.models import OuterRef
from django.db.models import Q
from django.db.models import Subquery
from django.db.models import Value
from django.db.models import When

from conda_sentinel.core.models import PackageHealth
from conda_sentinel.identity.confidence import IdentityConfidence
from conda_sentinel.policies.models import PackagePriority
from conda_sentinel.policies.outcomes import ABSENT
from conda_sentinel.policies.outcomes import PRIORITY_BUCKETS
from conda_sentinel.surface.filters import UnknownFacetValueError
from conda_sentinel.surface.filters import applied_filters
from conda_sentinel.surface.filters import filter_condition
from conda_sentinel.surface.reports import ABSENT_FROM_THE_INVENTORY
from conda_sentinel.surface.search import SEARCH_PARAM
from conda_sentinel.surface.search import name_condition
from conda_sentinel.surface.search import search_term

if TYPE_CHECKING:
    from collections.abc import Mapping
    from collections.abc import Sequence

    from django.db.models import QuerySet

__all__ = [
    "DEFAULT_ORDERING",
    "FEEDSTOCK_GAP_FACET",
    "ORDERINGS",
    "SORT_PARAM",
    "FeedstockGapExclusions",
    "feedstock_gap_exclusions",
    "health_queryset",
    "ordering_key",
]

#: The facet selection that makes the health list the feedstock-gap surface: the
#: `feedstock` facet with `absent` among its values.
FEEDSTOCK_GAP_FACET: Final[tuple[str, str]] = ("feedstock", ABSENT)


@dataclass(frozen=True, slots=True)
class FeedstockGapExclusions:
    """What the feedstock-gap surface left out of the list it shows, and why.

    Two counts, two reasons, stated above the list: packages the inventory no
    longer listed at the run's cut-off, which this surface excludes on purpose;
    and `unmapped` packages, which the confidence gate reports `unknown` rather
    than `absent`, so the facet never lists them -- true since `CPM-APP-S05` and
    said since `CPM-OPERATE-S11`.
    """

    absent_from_inventory: int
    absent_reason: str
    unmapped: int


#: The query-string parameter naming an ordering. The same spelling on both surfaces,
#: because a bookmark a reviewer sends an integrator should mean the same thing.
SORT_PARAM: Final[str] = "sort"

#: How the table may be ordered, and the first entry is the default.
#:
#: **By name first, and rank second, which is the opposite of what the mockup
#: shows.** The mockup's screenshot is of a reviewer who arrived from their queue and
#: has already chosen rank; a reader arriving at the URL with no opinion is usually
#: looking *up* a package rather than being handed one, and an unranked default is
#: also the cheap one -- `canonical_name` is uniquely indexed and rank is two
#: annotations. `?sort=rank` is one click and the applied-filters bar says which is
#: in force, so nothing is hidden.
#:
#: `package_id` terminates both orderings. `CPM-NFR-4`'s ten thousand rows are read a
#: page at a time, and a non-deterministic ordering means a row can appear on two
#: pages or on none -- the quietest paging bug there is, and one that only shows up
#: at a size nobody tests by hand.
ORDERINGS: Final[dict[str, tuple[str, ...]]] = {
    "name": ("package__canonical_name", "package_id"),
    "rank": ("bucket_rank", "-priority_score", "package_id"),
}

#: What an unrecognised `?sort=` falls back to. Silently, and unlike a bad facet
#: value: an ordering cannot make a result set wrong, only differently sorted, so
#: refusing a stale bookmark's sort key would cost a reader their page for nothing.
DEFAULT_ORDERING: Final[str] = next(iter(ORDERINGS))


def ordering_key(requested: str) -> str:
    """Return which ordering a request asked for.

    Args:
        requested: The raw `?sort=` value, or the empty string.

    Returns:
        The requested key when it names one, otherwise `DEFAULT_ORDERING`.

    """
    return requested if requested in ORDERINGS else DEFAULT_ORDERING


def health_queryset(params: Mapping[str, Sequence[str]], *, sort: str = "") -> QuerySet[PackageHealth]:
    """Return the rollup rows a filtered, ordered read of current health asks for.

    The **name search is read out of `params`** rather than taken as an argument, and
    that is deliberately unlike `sort` beside it. `sort` is a keyword because the view
    needs the resolved ordering for the template as well, so it resolves it once and
    passes it in. Nothing needs `?q=` resolved before the query runs -- so reading it
    here means both surfaces get it from the one line that builds the queryset, and
    there is no way for the screen and the API to disagree about whether a request was
    a search. `CPM-AD-24` in the small.

    Args:
        params: The query string as a multi-value mapping -- `request.GET.lists()` on
            either surface. A mapping rather than a `QueryDict` so this module needs
            nothing from Django's request layer and both a Django view and a DRF one
            can call it with what they already hold. `?q=` is read from here.
        sort: The requested ordering key. Unrecognised values fall back rather than
            refusing; see `DEFAULT_ORDERING`.

    Returns:
        The filtered, ordered queryset. `select_related("package")` is the one join a
        list query needs -- the canonical name is on every row -- and everything else
        is deferred to `health_rows()`'s bounded reads against the settled page.

    Raises:
        BadRequest: When a filter value is outside its vocabulary. Rendered as a 400
            rather than silently returning the whole inventory under a URL that
            claims to be filtered -- on both surfaces, because an integrator reading
            an unfiltered ten thousand rows as a filtered result is the worse half of
            that failure.

            **A name fragment that matches nothing is not one of these.** See
            `surface/search.py`: a closed vocabulary and an open one want opposite
            answers to a value nobody recognises, and both are right.

    """
    condition = _asked_for(params)
    if _feedstock_gap_asked(params):
        # The one exclusion this surface makes, stated on the page by
        # `feedstock_gap_exclusions` from the same condition.
        condition &= ~ABSENT_FROM_THE_INVENTORY.condition

    return (
        PackageHealth.objects.select_related("package")
        .filter(condition)
        .annotate(
            # The priority pass's own order, reproduced rather than re-derived.
            # `PRIORITY_BUCKETS` is worst-first, so its index *is* the rank and a
            # bucket outside it -- the four sentinels, `unknown` among them -- sorts
            # after every real bucket rather than among them.
            bucket_rank=Case(
                *(When(priority_status=bucket, then=Value(rank)) for rank, bucket in enumerate(PRIORITY_BUCKETS)),
                default=Value(len(PRIORITY_BUCKETS)),
                output_field=IntegerField(),
            ),
            # The score lives on `package_priority` and not on the rollup, by
            # `CPM-PRIORITY-S01`'s argument that a contribution carries statuses and
            # not numbers. A correlated subquery is what reads it without a join that
            # would multiply rows and break the page count.
            priority_score=Subquery(
                PackagePriority.objects.filter(
                    package_id=OuterRef("package_id"),
                    policy_run_id=OuterRef("policy_run_id"),
                ).values("score")[:1],
                output_field=IntegerField(),
            ),
        )
        .order_by(*ORDERINGS[ordering_key(sort)])
    )


def _asked_for(params: Mapping[str, Sequence[str]]) -> Q:
    """Return the condition a request's facets and search select, before any exclusion.

    Args:
        params: The query string, as `health_queryset` takes it.

    Returns:
        The conjunction of every selected facet and the name search.

    Raises:
        BadRequest: When a facet value is outside its vocabulary.

    """
    try:
        condition = filter_condition(applied_filters(dict(params)))
    except UnknownFacetValueError as refusal:
        raise BadRequest(str(refusal)) from refusal

    # AND, so a search and a set of facets narrow the same result rather than
    # replacing one another -- and so the paginator counts matches, which is what
    # `CPM-AD-12` means by pagination being structural.
    return condition & name_condition(
        search_term(next(iter(params.get(SEARCH_PARAM, ())), "")),
        field="package__canonical_name",
    )


def _feedstock_gap_asked(params: Mapping[str, Sequence[str]]) -> bool:
    """Report whether the request is the feedstock-gap surface.

    Args:
        params: The query string.

    Returns:
        Whether `FEEDSTOCK_GAP_FACET`'s value is among the facet's selected values.

    """
    param, value = FEEDSTOCK_GAP_FACET
    return value in applied_filters(dict(params)).get(param, ())


def feedstock_gap_exclusions(params: Mapping[str, Sequence[str]]) -> FeedstockGapExclusions | None:
    """Return what the feedstock-gap surface left out of this request's list, or `None` off that surface.

    Both counts come from the condition `health_queryset` built, less the
    exclusion turned round: the inventory-absent count is the rows the facets and
    the search select *and* the exclusion matches; the `unmapped` count is the
    rows every facet but the feedstock one selects whose identity the gate
    blanked, which is exactly the set the facet cannot list.

    Args:
        params: The query string, as `health_queryset` takes it.

    Returns:
        The two counts and the reason, or `None` when the request did not ask for
        `feedstock=absent` and nothing was excluded.

    """
    if not _feedstock_gap_asked(params):
        return None
    asked = _asked_for(params)
    without_the_facet = {param: values for param, values in params.items() if param != FEEDSTOCK_GAP_FACET[0]}
    return FeedstockGapExclusions(
        absent_from_inventory=PackageHealth.objects.filter(asked & ABSENT_FROM_THE_INVENTORY.condition).count(),
        absent_reason=str(ABSENT_FROM_THE_INVENTORY.reason),
        unmapped=PackageHealth.objects.filter(
            _asked_for(without_the_facet),
            confidence=IdentityConfidence.UNMAPPED,
        ).count(),
    )
