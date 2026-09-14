"""`CPM-FR-24`: every status on a package traced to the evidence that produced it.

The health view answers "what does this product think about ten thousand packages".
This answers the question a reviewer asks next, and it is a different question:
*why*. `CPM-APP-S03`'s reviewer wants "to check the reasoning rather than trusting
the conclusion", so a status here is never a value on its own -- it comes with the
rows behind it, what observed them, when, and how sure the source was.

**The evidence is read off the derived row's own foreign keys, never re-derived.**
Every pass cites what it used: `PackageVulnerability` names its `vulnerability_finding`
and its `kev_finding`, `PackageCurrency` names the snapshot for each version surface,
`PackagePythonReadiness` names the assessment or the verification. So "which row
produced this status" is a fact the policy engine already recorded, and reading it is
the only answer that cannot drift. Working it out again here -- newest row at or
before the cut-off, say -- would be the application layer deciding something a pass
decided, which `CPM-AD-10` forbids in as many words, and would quietly disagree with
the verdict the moment a pass's selection rule changed.

**Superseded evidence stays reachable, and that falls out of `CPM-AD-2` rather than
needing machinery.** Evidence is append-only: a re-observation inserts, so the row a
run cited last quarter is still there and still says what it said. This module lists
every observation of a fact and marks the one the run cited, so the history is
visible without anything being deleted and without the current value being in doubt.
`CPM-APP-S03`'s AC 3 asks for exactly that pair.

**Two statuses cite nothing, and say so.** `PackagePriority` and `PackageWorkType`
are derived from other verdicts rather than observed from a source -- `CPM-PRIORITY-S01`
and `CPM-PRIORITY-S02` both say so -- so they have no evidence foreign keys at all.
Rendering them with an empty evidence list would read as "nothing was found", which is
a different and alarming claim. They carry `DERIVED_FROM_VERDICTS` instead.

**The two confidences are never conflated.** Identity confidence is how sure the
product is *which package this is*; a vulnerability finding's match confidence is how
sure the advisory source was that the advisory *applies to the version installed*. The
UX contract requires they "are labelled differently everywhere they appear", so this
module carries the second as `match_confidence` and never as a bare `confidence`, and
the first never appears on an observation at all.

**On the `AD-` prefix.** A bare `AD-n` in this repository is an *inherited* platform
decision; a decision from this product's own architecture spine always carries the
`CPM-` prefix.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from typing import Final
from typing import cast

from conda_sentinel.collectors.models import PackageRecollection
from conda_sentinel.collectors.recollection import in_flight_window
from conda_sentinel.collectors.recollection import pending_recollection
from conda_sentinel.core.confidence import gated_status
from conda_sentinel.core.ledger import runs_in_flight
from conda_sentinel.core.outcomes import OutcomeState
from conda_sentinel.identity.models import IdentityOverride
from conda_sentinel.identity.models import PackageMapping
from conda_sentinel.policies.models import PackageCurrency
from conda_sentinel.policies.models import PackageFeedstockPresence
from conda_sentinel.policies.models import PackageLicense
from conda_sentinel.policies.models import PackagePriority
from conda_sentinel.policies.models import PackagePythonReadiness
from conda_sentinel.policies.models import PackageVulnerability
from conda_sentinel.policies.models import PackageWorkType
from conda_sentinel.workflow.models import WorkflowItem
from conda_sentinel.workflow.models import WorkflowTransition

if TYPE_CHECKING:
    from collections.abc import Iterable
    from datetime import datetime

    from django.db import models

    from conda_sentinel.collectors.recollection import PendingRecollection
    from conda_sentinel.core.models import CollectionRun
    from conda_sentinel.core.models import PackageHealth
    from conda_sentinel.identity.models import Package

__all__ = [
    "DERIVED_FROM_VERDICTS",
    "HISTORY_LIMIT",
    "NO_OBSERVATION_CITED",
    "TRACES",
    "Identity",
    "InFlight",
    "Observation",
    "StatusTrace",
    "Trace",
    "WorkItem",
    "identity_of",
    "in_flight",
    "last_recollection",
    "recent_runs",
    "traces_for",
]

#: What a status produced without evidence says instead of listing none.
#:
#: `CPM-PRIORITY-S01` and `CPM-PRIORITY-S02` both put their pass downstream of other
#: passes rather than of a collector, so there is no observation to cite -- and an
#: empty evidence list on a screen about evidence reads as "we looked and found
#: nothing", which is a claim about the world rather than about the derivation.
DERIVED_FROM_VERDICTS: Final[str] = "derived from other verdicts, not from an observation"

#: What a status says when it *could* rest on an observation and this run's row cites
#: none.
#:
#: A third state, and keeping it apart from `DERIVED_FROM_VERDICTS` is a correctness
#: matter rather than a nicety. Currency is observed from a version surface and
#: Python readiness from an assessment or a build -- but each picks its relation per
#: row, and each has a legitimate state where it picked none: an indeterminate
#: currency verdict chose no authority, and an undecided readiness rests on neither
#: kind of evidence. Rendering those as "derived from other verdicts" would say
#: something false about how the product works, which is the one thing a screen built
#: to be checked cannot afford.
NO_OBSERVATION_CITED: Final[str] = "no observation cited at this run"

#: How many observations of one fact the view shows.
#:
#: Evidence is append-only (`CPM-AD-2`) and a daily collector produces a row a day, so
#: a package watched for a year has three hundred of them and the tenth is already
#: past the point anybody scrolls. Twenty is roughly three weeks of daily observation
#: -- enough to see a status change and what it changed from, which is the question
#: this screen is for. It is a *display* bound and not a retention one: nothing is
#: deleted, the count of what is held is shown beside the list, and `CPM-APP-S09`'s
#: investigation surface is where a full history belongs.
HISTORY_LIMIT: Final[int] = 20


@dataclass(frozen=True, slots=True)
class Observation:
    """One evidence row, as the detail view shows it.

    A view model rather than a model instance: `CPM-AD-10` gives the application
    layer no write path to evidence, and a frozen dataclass is a shape a template
    cannot save through even by accident.
    """

    #: The evidence table this came from, by its schema name -- the name a reviewer
    #: would use to go and look, and the one `CPM-AD-24`'s governed views carry.
    table: str

    #: What the observation concluded, verbatim as its `OutcomeState` value.
    state: str

    #: Where it came from: the source's own locator, which is usually a URL.
    source: str

    #: When it was observed. Never absent -- `AppendOnlyModel` requires it, which is
    #: what makes "the timestamp behind each status" answerable at all.
    observed_at: datetime

    #: How sure the *source* was that this applies to the version installed. Empty
    #: for every table but advisories. **Not identity confidence**, which is a
    #: different question about a different thing; see the module docstring.
    match_confidence: str

    #: What the row says about itself, where it says anything.
    detail: str

    #: The run's own citation. Exactly one observation per fact carries this, and it
    #: is the one the verdict rests on; the rest are the history `CPM-AD-2` keeps.
    cited: bool


@dataclass(frozen=True, slots=True)
class StatusTrace:
    """One derived status, with everything behind it."""

    #: What the row is called on screen.
    label: str

    #: The verdict, gated by `CPM-AD-4` exactly as the health view gates it -- so the
    #: two screens cannot disagree about the same package.
    status: str

    #: Where it came from, in one phrase: the evidence table, or
    #: `DERIVED_FROM_VERDICTS`.
    produced_from: str

    #: What the pass recorded about its own reasoning, where it recorded anything.
    detail: str

    #: The observations behind it, newest first, capped at `HISTORY_LIMIT`.
    observations: tuple[Observation, ...]

    #: How many observations exist in total, which is what makes the cap honest: a
    #: list of twenty beside "of 314" is a bounded view of a complete record, and a
    #: list of twenty alone is indistinguishable from the whole of it.
    observation_count: int


@dataclass(frozen=True, slots=True)
class Trace:
    """Which derived table holds a status, and which evidence it cites.

    Declared rather than discovered. A registry walk would give the models and not
    the *correspondence* -- which column is the verdict, which foreign key is the
    evidence behind that particular verdict, and what a reader should call it -- and
    every one of those is a per-domain fact somebody has to write down.
    """

    #: What the row is called on screen.
    label: str

    #: The derived model, keyed `(package, policy_run)` per `CPM-AD-21`.
    model: type[models.Model]

    #: The column holding the verdict.
    status_field: str

    #: The foreign key naming the evidence behind it, or `""` where the relation is
    #: not fixed. Two things wear that empty string and they are not the same: a
    #: status genuinely derived from other verdicts (`observed` is `False`), and one
    #: whose relation is chosen per row (`observed` is `True`; see
    #: `_currency_relation` and `_readiness_relation`).
    evidence_field: str = ""

    #: Whether this status rests on an observation at all.
    #:
    #: `False` only for `PackagePriority` and `PackageWorkType`, which
    #: `CPM-PRIORITY-S01` and `CPM-PRIORITY-S02` put downstream of other passes
    #: rather than of a collector. It is what tells `DERIVED_FROM_VERDICTS` from
    #: `NO_OBSERVATION_CITED`, which look alike on screen and mean different things.
    observed: bool = True

    #: The column carrying the pass's own note, where it has one.
    detail_field: str = "detail"


#: Every status the detail view traces, in the order it renders them.
#:
#: The order is the health view's, deliberately: a reviewer arrives here from that
#: table and reads down the same list. Vulnerability and KEV stay adjacent because
#: the second qualifies the first.
TRACES: Final[tuple[Trace, ...]] = (
    Trace(
        label="Currency",
        model=PackageCurrency,
        status_field="overall_status",
        evidence_field="",  # chosen per row; see `_currency_relation`.
    ),
    Trace(
        label="Vulnerability",
        model=PackageVulnerability,
        status_field="vulnerability_status",
        evidence_field="vulnerability_finding",
    ),
    Trace(
        label="KEV",
        model=PackageVulnerability,
        status_field="kev_membership",
        evidence_field="kev_finding",
    ),
    Trace(
        label="Licence",
        model=PackageLicense,
        status_field="license_outcome",
        evidence_field="license_finding",
    ),
    Trace(
        label="Py 3.14",
        model=PackagePythonReadiness,
        status_field="readiness",
        evidence_field="",  # assessment or verification; see `_readiness_relation`.
    ),
    Trace(
        label="Feedstock",
        model=PackageFeedstockPresence,
        status_field="presence_status",
        evidence_field="feedstock_snapshot",
    ),
    Trace(
        label="Priority",
        model=PackagePriority,
        status_field="bucket",
        observed=False,
    ),
    Trace(
        label="Work type",
        model=PackageWorkType,
        status_field="work_type",
        observed=False,
    ),
)


@dataclass(frozen=True, slots=True)
class Identity:
    """`CPM-FR-24`'s second criterion: who this package is, and how that was decided.

    `CPM-AD-14` makes identity the product's one governed reference datum with a
    single write path, so what a reviewer needs is not only the current value but
    where it came from -- and, when a person corrected it, that a person did and why.
    """

    #: The package's current identity, as `identity/models.py` holds it.
    package: Package

    #: How the identity was established, and what the resolver matched on.
    identity_source: str
    associator_key: str
    resolved_at: datetime | None

    #: How certain it is. `CPM-AD-4` reads this, which is why every status above can
    #: be `unknown` while this row is fully populated.
    confidence: str

    #: The most recent human correction, if there has been one.
    #:
    #: **Displayed rather than hidden**, which the UX contract states as its own
    #: annotation: a corrected identity that looked automatic would leave a reviewer
    #: unable to tell a resolver's confident match from a colleague's judgement call,
    #: and `CPM-AD-14`'s whole point is that the second is attributable.
    override: IdentityOverride | None

    #: Every mapping the resolver recorded, newest first: what it tried and what came
    #: back. `CPM-AD-12`'s "a claim naming no row is ignored, never created" means an
    #: absent mapping is itself informative.
    mappings: tuple[PackageMapping, ...]


def traces_for(row: PackageHealth) -> tuple[StatusTrace, ...]:
    """Return every derived status on a package, with the evidence behind each.

    Args:
        row: The package's rollup row, which names the run every derived row is read
            at. Read at the row's own `policy_run` and never at the latest one: a
            derived row from a different run would pair a verdict with freshness
            stamps that are not its own.

    Returns:
        One `StatusTrace` per entry in `TRACES`, in that order.

    """
    derived = {
        trace.model: trace.model._default_manager.filter(  # noqa: SLF001 - the manager a type-annotated model has
            package_id=row.package_id,
            policy_run_id=row.policy_run_id,
        ).first()
        for trace in TRACES
    }
    return tuple(_trace(row, trace, derived[trace.model]) for trace in TRACES)


def _trace(row: PackageHealth, trace: Trace, derived: models.Model | None) -> StatusTrace:
    """Return one status and its evidence.

    Args:
        row: The rollup row, for the identity confidence the verdict is gated on.
        trace: What to read and what to call it.
        derived: The derived row, or `None` where the pass wrote none.

    Returns:
        The trace.

    """
    if derived is None:
        return StatusTrace(
            label=trace.label,
            status=OutcomeState.UNKNOWN.value,
            produced_from="no evaluation recorded at this run",
            detail="",
            observations=(),
            observation_count=0,
        )

    relation = _evidence_relation(trace, derived)
    status = gated_status(getattr(derived, trace.status_field), confidence=row.confidence)
    detail = getattr(derived, trace.detail_field, "") or ""
    if not relation:
        return StatusTrace(
            label=trace.label,
            status=status,
            # The two reasons a row cites nothing, told apart. See the constants.
            produced_from=NO_OBSERVATION_CITED if trace.observed else DERIVED_FROM_VERDICTS,
            detail=detail,
            observations=(),
            observation_count=0,
        )

    cited = getattr(derived, relation, None)
    # `related_model` is `type[Model] | "self" | None` to django-stubs because a
    # foreign key may be self-referential or lazy. None of these is: every one names
    # an evidence table in `collectors/models.py`, which `_evidence_relation`
    # guarantees by only ever returning a relation the `Trace` declared.
    evidence_model = cast("type[models.Model]", trace.model._meta.get_field(relation).related_model)  # noqa: SLF001 - a field's own metadata
    history = evidence_model._default_manager.filter(package_id=row.package_id).order_by(  # noqa: SLF001 - as above
        "-observed_at",
        "-id",
    )
    return StatusTrace(
        label=trace.label,
        status=status,
        produced_from=evidence_model._meta.db_table,  # noqa: SLF001 - the schema name a reviewer would look under
        detail=detail,
        observations=_observations(history[:HISTORY_LIMIT], cited_pk=getattr(cited, "pk", None)),
        observation_count=history.count(),
    )


def _evidence_relation(trace: Trace, derived: models.Model) -> str:
    """Return which foreign key holds the evidence behind *this* row's verdict.

    Two passes cite more than one relation and pick between them per package, so the
    answer is a property of the row rather than of the domain.

    Args:
        trace: The status being traced.
        derived: The derived row.

    Returns:
        The relation name, or `""` where the status rests on no observation.

    """
    if trace.model is PackageCurrency:
        return _currency_relation(derived)
    if trace.model is PackagePythonReadiness:
        return _readiness_relation(derived)
    return trace.evidence_field


def _currency_relation(derived: models.Model) -> str:
    """Return the snapshot behind a currency verdict, by the authority the pass chose.

    `PackageCurrency` carries a snapshot per version surface and records which one it
    treated as authoritative. Showing all four would be showing three observations
    the verdict does not rest on, next to the one it does.

    Args:
        derived: The `PackageCurrency` row.

    Returns:
        The snapshot relation, or `""` when the pass chose no authority -- which is
        what an indeterminate verdict looks like and is a state the model's own
        constraint permits.

    """
    authority = getattr(derived, "chosen_authority", "") or ""
    return f"{authority}_snapshot" if authority else ""


def _readiness_relation(derived: models.Model) -> str:
    """Return whichever evidence a readiness verdict actually rests on.

    `CPM-PY314-S03` made the kind of evidence part of the verdict: a `verified_`
    readiness rests on a build that ran and an `inferred_` one on static metadata.
    Reading `assessment` for a verified verdict would trace a proof to the metadata
    it superseded -- and would produce a plausible-looking answer, which is what
    makes it worth branching on rather than defaulting.

    Args:
        derived: The `PackagePythonReadiness` row.

    Returns:
        `"verification"`, `"assessment"`, or `""` for a verdict resting on neither.

    """
    from conda_sentinel.policies.outcomes import ReadinessEvidence  # noqa: PLC0415 - avoids a vocabulary import cycle

    return {
        ReadinessEvidence.VERIFIED.value: "verification",
        ReadinessEvidence.INFERRED.value: "assessment",
    }.get(getattr(derived, "evidence_type", ""), "")


def _observations(rows: Iterable[models.Model], *, cited_pk: int | None) -> tuple[Observation, ...]:
    """Return evidence rows as the view shows them, marking the one the run cited.

    Args:
        rows: The evidence rows, newest first.
        cited_pk: The primary key of the row the derived verdict names, if any.

    Returns:
        One `Observation` per row.

    """
    return tuple(
        Observation(
            table=row._meta.db_table,  # noqa: SLF001 - the schema name a reviewer would look under
            state=getattr(row, "state", ""),
            source=getattr(row, "source", ""),
            observed_at=row.observed_at,  # type: ignore[attr-defined]
            # Named for what it is. Identity confidence is a different question about
            # a different thing, and the UX contract requires the two be labelled
            # differently everywhere they appear.
            match_confidence=getattr(row, "match_confidence", ""),
            detail=getattr(row, "detail", ""),
            cited=cited_pk is not None and row.pk == cited_pk,
        )
        for row in rows
    )


def identity_of(package: Package) -> Identity:
    """Return who a package is, and how that was decided.

    Args:
        package: The package.

    Returns:
        Its identity, the most recent human correction if there has been one, and
        every mapping the resolver recorded.

    """
    return Identity(
        package=package,
        identity_source=package.identity_source,
        associator_key=package.associator_key,
        resolved_at=package.resolved_at,
        confidence=package.confidence,
        override=IdentityOverride.objects.filter(package=package).order_by("-observed_at", "-id").first(),
        mappings=tuple(PackageMapping.objects.filter(package=package).order_by("-resolved_at", "-id")),
    )


@dataclass(frozen=True, slots=True)
class InFlight:
    """What the "In flight" panel shows: the open runs, and the press still awaiting its runs.

    Both halves of the recollection's own rule, read here so the panel never
    lists a run the service would not refuse over and never omits a press it
    would. Empty on both counts means the button may be pressed.
    """

    #: The unfinished runs on the package started within the window, newest first.
    runs: tuple[CollectionRun, ...]

    #: The newest press within the window the ledger has not answered in full,
    #: with the collectors it still awaits, or `None`.
    pending: PendingRecollection | None

    def __bool__(self) -> bool:
        """Report whether anything is in flight.

        Returns:
            True when a run is open or a press is unanswered.

        """
        return bool(self.runs) or self.pending is not None


def in_flight(package: Package, *, now: datetime) -> InFlight:
    """Return what is in flight on this package, by the recollection's own rule.

    The "In flight" panel `CPM-OPERATE-S08` puts above the run ledger: what a
    reviewer who has just pressed "Collect now" watches, and what the service
    refuses a second press over. One rule, read from `core/ledger.py` and
    `collectors/recollection.py`, so the panel and the refusal cannot disagree
    -- an open row older than the window is a killed worker's, is not listed
    here, and still appears in `recent_runs` as `running`.

    Args:
        package: The package.
        now: The instant the window is measured back from.

    Returns:
        The open runs and the pending press, either of which may be empty.

    """
    return InFlight(
        runs=tuple(runs_in_flight(package.pk, now=now, window=in_flight_window())),
        pending=pending_recollection(package.pk, now=now),
    )


def last_recollection(package: Package) -> PackageRecollection | None:
    """Return the newest recollection somebody asked for on this package, if any.

    Shown beneath the run ledger: who pressed the button, when, and which
    collectors were published -- the audit row, read as the record of a human act
    rather than as evidence about the package.

    Args:
        package: The package.

    Returns:
        The newest `PackageRecollection`, with its actor joined, or `None`.

    """
    return PackageRecollection.objects.filter(package=package).select_related("actor").first()


def recent_runs(package: Package) -> tuple[CollectionRun, ...]:
    """Return the collection runs that touched this package, newest first.

    **The run ledger is not evidence and the screen says so.** A run records that a
    collector was asked, how long it took and whether it finished; the evidence is
    what it wrote. They are shown together because the question "why is this status
    stale" is usually answered by a run that failed rather than by any row of
    evidence -- and `CPM-AD-2` exempts the ledger from append-only precisely so a run
    killed mid-call still leaves a row saying `running`.

    Args:
        package: The package.

    Returns:
        Up to `HISTORY_LIMIT` runs.

    """
    return tuple(package.collection_runs.order_by("-started_at", "-id")[:HISTORY_LIMIT])


@dataclass(frozen=True, slots=True)
class WorkItem:
    """One queue item on this package, with who moved it and when.

    `CPM-APP-S05`'s AC 4 -- "who acted, when, and the resulting state are recorded" --
    is satisfied by `workflow_transitions` and made *visible* here. A record nobody
    can read from the screen the work is discussed on is a record somebody has to be
    told exists.
    """

    item: WorkflowItem

    #: Every move, newest first, capped on the same terms the evidence history is.
    history: tuple[WorkflowTransition, ...]


def work_on(package: Package) -> tuple[WorkItem, ...]:
    """Return the queue items open or finished on a package, with their history.

    Args:
        package: The package.

    Returns:
        One entry per item, open ones first -- a reader looking at a package wants
        what is outstanding before what was settled.

    """
    items = list(
        WorkflowItem.objects.filter(package=package).order_by("state", "-changed_at").select_related("claimed_by"),
    )
    if not items:
        return ()

    # One read for every item's history rather than one per item: a package with four
    # queue items would otherwise cost four queries on a screen that already reads
    # eight derived tables.
    moves: dict[int, list[WorkflowTransition]] = {}
    for move in (
        WorkflowTransition.objects.filter(item__in=items)
        .order_by("-occurred_at", "-id")
        .select_related("actor")[: HISTORY_LIMIT * len(items)]
    ):
        moves.setdefault(move.item_id, []).append(move)
    return tuple(WorkItem(item=item, history=tuple(moves.get(item.pk, ()))) for item in items)
