"""The read API's shapes, over the projections the screens already render.

**These serialize `surface/health.py` and `surface/detail.py`'s frozen dataclasses,
not models.** That is the whole design and it is `CPM-AD-24`: "every read surface
projects the same values", whose named failure is "a new derived status reaching the
API but not the governed view". A `ModelSerializer` over `PackageHealth` would be a
second projection -- it would miss the confidence gate that `_gated()` applies, it
would not carry the evidence timestamps the detail view shows, and it would grow a
seventh column the moment a pass added one while the screen stayed at six. Reading
the same dataclass means the API cannot be more or less than the screen, because
there is nothing else for it to read.

**Every status goes through `StatusField`.** See `core/serializer_fields.py` for the
argument; the short version is that `unknown` is a state and every ordinary
serialization habit turns it into an absence.

**Nothing here writes.** `Serializer` rather than `ModelSerializer` means there is no
`create` and no `update` to inherit, so a read endpoint cannot acquire a write path
by having a serializer that happens to know how to save. AC 3's enumeration is swept
by `tests/unit/django_apps/test_api_contract_audit.py`, but the shape comes first.

**On the `AD-` prefix.** A bare `AD-n` in this repository is an *inherited* platform
decision; a decision from this product's own architecture spine always carries the
`CPM-` prefix.
"""

from __future__ import annotations

from typing import Any

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from conda_sentinel.core.serializer_fields import StatusField
from conda_sentinel.surface.reports import Report
from conda_sentinel.surface.reports import excluded_count

__all__ = [
    "CellSerializer",
    "ExclusionSerializer",
    "HealthRowSerializer",
    "IdentitySerializer",
    "ObservationSerializer",
    "PackageDetailSerializer",
    "PackageMappingSerializer",
    "ReportPageSerializer",
    "ReportSerializer",
    "StatusTraceSerializer",
    "WorkItemSerializer",
    "report_exclusion",
]


class CellSerializer(serializers.Serializer[Any]):
    """One derived status as the health table shows it: the value, and what saw it."""

    #: `CPM-AD-24`, and `APP.07-API-001`. Never null, never blank, never a boolean.
    status = StatusField()

    #: What observed it, in the words a reader recognises. Blank is legitimate here
    #: -- it is a field with no value, unlike the status beside it.
    note = serializers.CharField(allow_blank=True)

    #: When the evidence behind the status was observed, or `null` where there is
    #: none: an `unknown` from the confidence gate rests on nothing, and saying so is
    #: the point of the pair. The *status* still says `unknown`; only the timestamp
    #: is absent, which is the distinction AC 5 exists to keep.
    observed_at = serializers.DateTimeField(allow_null=True)


class HealthRowSerializer(serializers.Serializer[Any]):
    """One package's current health, exactly as the health table renders it."""

    package_id = serializers.IntegerField()
    canonical_name = serializers.CharField()

    confidence = StatusField()
    priority = StatusField()
    work_type = StatusField()

    #: `null` for a package the priority pass reached no conclusion about. Zero is a
    #: score somebody could have been given and the absence of one is a different
    #: thing -- which is why this is nullable where the statuses above are not.
    score = serializers.IntegerField(allow_null=True)

    #: The freshness `CPM-AD-11` requires every view to display, **per row**: a
    #: replayed run leaves rows computed at different instants, and a single stamp on
    #: the envelope would be wrong for some of them.
    computed_at = serializers.DateTimeField()
    evidence_cutoff = serializers.DateTimeField()
    policy_versions = serializers.DictField(child=serializers.CharField())

    #: One per `surface/health.py`'s `COLUMNS`, in that order. The order is the
    #: contract: an integrator zipping these against the column roster the schema
    #: publishes gets the same alignment the template relies on.
    cells = CellSerializer(many=True)

    #: What the inventory said about the package at the run's cut-off
    #: (`CPM-OPERATE-S11`, `CPM-AD-24`): both `null` for a listed package, both set
    #: for one the inventory no longer lists. The same two rollup columns the
    #: screen's tag is built from, so an integrator sees what a reader sees --
    #: and never a boolean, because "absent" without "since when" is the half of
    #: the fact that goes stale.
    inventory_absent_since = serializers.DateTimeField(allow_null=True)
    inventory_last_listed = serializers.DateTimeField(allow_null=True)


class ObservationSerializer(serializers.Serializer[Any]):
    """One evidence row behind a status."""

    table = serializers.CharField()

    #: What the observation itself concluded -- a status, and gated by the same rule.
    state = StatusField()

    #: Where it came from: the source's own locator, usually a URL.
    #:
    #: `source` here, and `label` on `StatusTraceSerializer`, are both attribute
    #: names DRF's own `Field` uses. Declaring them is safe -- `SerializerMetaclass`
    #: *pops* declared fields out of the class namespace before `Field.__init__`
    #: ever sets its own -- but the stubs cannot see that. The ignore is narrower
    #: than renaming two fields readers of the evidence tables know by name.
    source = serializers.CharField(allow_blank=True)  # type: ignore[assignment]
    observed_at = serializers.DateTimeField()
    match_confidence = serializers.CharField(allow_blank=True)
    detail = serializers.CharField(allow_blank=True)

    #: Whether this is the observation the verdict rests on. Exactly one per fact
    #: carries it; the rest are the history `CPM-AD-2` keeps.
    cited = serializers.BooleanField()


class StatusTraceSerializer(serializers.Serializer[Any]):
    """One derived status with the evidence behind it, as the detail screen traces it."""

    #: See `ObservationSerializer.source` for why this ignore is here.
    label = serializers.CharField()  # type: ignore[assignment]
    status = StatusField()
    produced_from = serializers.CharField()
    detail = serializers.CharField(allow_blank=True)
    observations = ObservationSerializer(many=True)

    #: How many exist in total, which is what makes the cap honest: twenty beside
    #: "of 314" is a bounded view of a complete record, and twenty alone is
    #: indistinguishable from the whole of it.
    observation_count = serializers.IntegerField()


class PackageMappingSerializer(serializers.Serializer[Any]):
    """One mapping a resolver recorded: which one, and what came back.

    A mapping the resolver looked for and did not find is as informative as one it
    established -- `CPM-FR-6` is explicit about it -- so `outcome` is a status like
    any other and goes through `StatusField`. A client that saw `not_found` as a
    missing row could not tell "we looked and there is nothing" from "nobody looked".
    """

    kind = serializers.CharField()
    outcome = StatusField()
    resolved_at = serializers.DateTimeField(allow_null=True)


class IdentitySerializer(serializers.Serializer[Any]):
    """Who a package is, and how that was decided."""

    canonical_name = serializers.CharField(source="package.canonical_name")
    display_name = serializers.CharField(source="package.display_name", allow_blank=True)
    identity_source = serializers.CharField(allow_blank=True)
    associator_key = serializers.CharField(allow_blank=True)
    resolved_at = serializers.DateTimeField(allow_null=True)

    #: `CPM-AD-4` reads this, which is why every status on the package can be
    #: `unknown` while this row is fully populated.
    confidence = StatusField()

    #: Whether a person corrected this identity, and why. Surfaced rather than
    #: hidden: a corrected identity that looked automatic would leave a reader unable
    #: to tell a resolver's confident match from a colleague's judgement call, and
    #: `CPM-AD-14`'s whole point is that the second is attributable.
    overridden_at = serializers.DateTimeField(source="override.observed_at", allow_null=True, default=None)
    override_reason = serializers.CharField(source="override.reason", allow_blank=True, default="")

    mappings = PackageMappingSerializer(many=True)


class WorkItemSerializer(serializers.Serializer[Any]):
    """One queue item open or finished on a package."""

    id = serializers.IntegerField(source="item.pk")
    queue = serializers.CharField(source="item.queue")

    #: A workflow state, not an `OutcomeState`, so an ordinary `CharField`. The
    #: distinction is deliberate: `StatusField` carries the rule `CPM-AD-24` puts on
    #: *derived* statuses, and widening it to every enumerated string would make the
    #: audit's sweep meaningless.
    state = serializers.CharField(source="item.state")

    finding_key = serializers.CharField(source="item.finding_key")
    changed_at = serializers.DateTimeField(source="item.changed_at")
    claimed_by = serializers.CharField(source="item.claimed_by.username", allow_null=True, default=None)


class PackageDetailSerializer(serializers.Serializer[Any]):
    """`CPM-FR-24` over HTTP: every status on one package, traced to its evidence."""

    canonical_name = serializers.CharField()
    computed_at = serializers.DateTimeField()
    evidence_cutoff = serializers.DateTimeField()
    policy_versions = serializers.DictField(child=serializers.CharField())

    #: The rollup row's two inventory-absence instants, on `HealthRowSerializer`'s
    #: terms: beside the provenance rather than under `identity`, because absence
    #: is an observation about the inventory at this row's cut-off and not a fact
    #: about who the package is.
    inventory_absent_since = serializers.DateTimeField(allow_null=True)
    inventory_last_listed = serializers.DateTimeField(allow_null=True)

    identity = IdentitySerializer()
    traces = StatusTraceSerializer(many=True)
    work = WorkItemSerializer(many=True)


class ExclusionSerializer(serializers.Serializer[Any]):
    """What a report leaves out: how many packages, and why (`CPM-OPERATE-S11`).

    Carried on the report and on every page of it rather than stated on the
    screen alone, because the rows an integrator reads are the rows the exclusion
    was applied to, and a JSON body that omitted the sentence would read as
    complete to somebody who never saw the page.
    """

    count = serializers.IntegerField()
    reason = serializers.CharField()


class ReportSerializer(serializers.Serializer[Any]):
    """One entry in the report roster: what it asks, how often it is read, and what it leaves out."""

    slug = serializers.CharField()
    title = serializers.CharField()
    asks = serializers.CharField()
    cadence = serializers.CharField()

    #: `null` for the five reports that exclude nothing; the count and the reason
    #: for the one that does, over the whole report -- the roster has no search.
    excluded = serializers.SerializerMethodField()

    @extend_schema_field(ExclusionSerializer(allow_null=True))
    def get_excluded(self, report: Report) -> dict[str, object] | None:
        """Return the report's exclusion with its count, or `None`.

        Args:
            report: The roster entry.

        Returns:
            `{"count": N, "reason": "..."}` or `None`.

        """
        return report_exclusion(report)


class ReportPageSerializer(serializers.Serializer[Any]):
    """One report, produced -- with the provenance `CPM-APP-S06` AC 2 requires.

    The rows come out as lists of strings rather than as objects keyed by heading,
    which is the same shape the CSV export writes and for the same reason: the
    columns are the contract, `columns` publishes them in order, and a row is a
    tuple against that order. Keying by heading would make a renamed heading a
    breaking change to every client's field access.
    """

    report = ReportSerializer()
    columns = serializers.ListField(child=serializers.CharField())
    rows = serializers.ListField(child=serializers.ListField(child=serializers.CharField(allow_blank=True)))

    #: `null` for an empty report, which is honest rather than a gap: a report of
    #: nothing was produced from nothing.
    evidence_cutoff = serializers.DateTimeField(allow_null=True)

    #: **Every** version the rows carry, not one. `CPM-AD-11` stamps a map per row
    #: and a replay leaves rows from two runs behind.
    policy_versions = serializers.ListField(child=serializers.CharField())

    #: What the report left out of these rows, or `null` for a report that
    #: declares no exclusion. The count is over the report the page belongs to,
    #: on the terms `ReportPage.excluded` states.
    excluded = ExclusionSerializer(allow_null=True)


def report_exclusion(report: Report, *, search: str = "") -> dict[str, object] | None:
    """Return the exclusion a report declares, with its count, in the API's shape.

    Args:
        report: Which report.
        search: The fragment the rows were narrowed by, for a page; the roster
            passes none.

    Returns:
        `{"count": N, "reason": "..."}`, or `None` for a report that excludes
        nothing.

    """
    if report.excludes is None:
        return None
    return {"count": excluded_count(report, search=search), "reason": str(report.excludes.reason)}
