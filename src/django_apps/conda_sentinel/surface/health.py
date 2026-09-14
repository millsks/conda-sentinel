"""`CPM-FR-23`'s projection: the rollup row plus the evidence behind every status.

`CPM-AD-11` gives current health one row per package, and that row carries four
contributed statuses -- currency, feedstock presence, priority bucket and work type
-- plus identity confidence and the run's freshness stamps. It deliberately does not
carry the rest. `CPM-AD-21` keys each pass's own derived table on `(package,
policy_run)` and `core/policy_run.py` only lets a pass contribute a *status* to the
rollup, so vulnerability, KEV membership, licence and Python readiness live where the
pass that produced them wrote them.

The health view has to show all of them, and `CPM-APP-S02`'s first acceptance
criterion says with **the observation timestamp behind each**. So this module is the
join: it takes a page of rollup rows and returns, per package, one cell per column
carrying the status verbatim, what observed it, and when.

**Verbatim is the rule and not a preference.** `CPM-AD-24`: a derived status is
emitted as its `OutcomeState` value on every surface, and "blank is reserved for a
field with no value and is never used for a status". A cell whose derived row is
missing therefore says `unknown` and says *why* -- never an empty string, never the
column left out. `EMPTY_STATUS` exists to be asserted against rather than used.

**The query count is bounded and does not depend on the page size.** One query per
derived table for the whole page, each pulling its evidence rows along by
`select_related`, indexed by `(package, policy_run)` in memory. Seven for a full page
of fifty rows and seven for a page of one, which is what
`CPM-APP-S02`'s AC 5 asks a test to bound -- and it is the shape that stops the
most-read screen in the product being an N+1 nobody notices until ten thousand
packages.

**The four statuses this module reads out of a derived table go through
`CPM-AD-4`'s gate on the way to the screen, and finding that out is what this module
was nearly shipped without.** The rollup's own columns are already gated --
`core/rollup.py` applies `gated_status` on the way in -- but a derived table is what
the pass wrote, ungated, because a pass computes its verdict without knowing anything
about identity confidence. Reading `package_vulnerability.vulnerability_status`
straight onto the row would therefore have put a confident claim about a package
nobody has identified next to five columns correctly saying `unknown`, which is the
exact failure `CPM-AD-4` exists to prevent -- and it would have been *invisible*,
because every other column on the row would have looked right.

The gate is `core/confidence.py`'s, called rather than reimplemented. This is a
second *application* of the one gate, not a second gate: the rollup applies it to
what a pass contributes and this applies it to what a pass stored, and
`tests/unit/django_apps/test_confidence_gate_audit.py` is what keeps the distinction
from becoming a second implementation.

**Every derived row is read at the health row's own `policy_run`.** Not the latest
run, and not "the row for this package": `CPM-AD-21` writes one derived row per
package *per run*, so reading without the run would return a row from whichever run
happened to be last and could pair a status from one run with a freshness stamp from
another. The lookup key is the pair, always.

**On the `AD-` prefix.** A bare `AD-n` in this repository is an *inherited* platform
decision; a decision from this product's own architecture spine always carries the
`CPM-` prefix.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from typing import Final

from django.db import models

from conda_sentinel.core.confidence import gated_status
from conda_sentinel.core.outcomes import OutcomeState
from conda_sentinel.policies.models import SURFACE_STATUS_FIELDS
from conda_sentinel.policies.models import PackageCurrency
from conda_sentinel.policies.models import PackageFeedstockPresence
from conda_sentinel.policies.models import PackageLicense
from conda_sentinel.policies.models import PackagePriority
from conda_sentinel.policies.models import PackagePythonReadiness
from conda_sentinel.policies.models import PackageVulnerability
from conda_sentinel.policies.outcomes import ReadinessEvidence

if TYPE_CHECKING:
    from collections.abc import Iterable
    from collections.abc import Sequence
    from datetime import datetime

    from conda_sentinel.core.models import PackageHealth

__all__ = [
    "COLUMNS",
    "EMPTY_STATUS",
    "NO_ROW_NOTE",
    "Cell",
    "Column",
    "HealthRow",
    "health_rows",
]

#: What a status must never be. `CPM-AD-24` reserves blank for a field with no
#: value, and a status always has one -- `unknown` is a value, and it is the one a
#: missing derived row means. Declared so the audit can assert against it rather
#: than against a literal written twice.
EMPTY_STATUS: Final[str] = ""

#: What a cell says when the pass wrote no row for this package at this run.
#:
#: A real state rather than a defensive branch: `CPM-AD-4`'s confidence gate stops
#: an unmapped package's evidence being evaluated at all, and a pass registered after
#: a run began has no row for any package in it. Both are "nothing concluded this",
#: which is exactly `unknown`, and saying so beats a blank cell that reads as clean.
NO_ROW_NOTE: Final[str] = "no evaluation recorded at this run"

#: Which snapshot column holds the evidence behind each version surface.
#:
#: Built by walking `SURFACE_STATUS_FIELDS` rather than written out, on the terms
#: that table sets for itself: a fifth surface joins this map at the moment it
#: acquires a column, and a renamed surface fails here rather than quietly producing
#: a cell with no timestamp. The naming convention -- `<surface>_snapshot` beside
#: `<surface>_status` -- is `PackageCurrency`'s own and is reconciled by
#: `tests/unit/django_apps/test_health_projection.py`.
SURFACE_SNAPSHOT_FIELDS: Final[dict[str, str]] = {surface: f"{surface}_snapshot" for surface in SURFACE_STATUS_FIELDS}


@dataclass(frozen=True, slots=True)
class Cell:
    """One derived status, as a read surface shows it.

    Three fields and no rendering. `CPM-AD-24` puts formatting decisions on the
    surface and values here, so this carries the status as the policy engine wrote
    it and the template decides what a chip looks like.
    """

    #: The status verbatim, as its `OutcomeState` value. Never blank.
    status: str

    #: What observed it, in the words a reader recognises -- the version surface
    #: that decided currency, `advisory` for a vulnerability finding, the kind of
    #: evidence a readiness verdict rests on. Not the evidence row's `source`
    #: locator, which is a URL and answers a different question.
    note: str

    #: When the evidence behind this status was observed. `None` when there is no
    #: evidence behind it -- an `unknown` from the confidence gate, a `not_found`
    #: from a lookup that matched nothing -- which is a field with no value and is
    #: therefore correctly blank, unlike the status beside it.
    observed_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class Column:
    """One column of the health table, named once so every surface agrees on it.

    `CPM-AD-24` requires that every derived-status column in the rollup appears in
    the governed views, and the failure it prevents -- "a new derived status reaching
    the API but not the governed view" -- is a failure of two lists disagreeing. This
    is the one list.
    """

    #: How the column is addressed in a query string and in a `Cell` lookup.
    key: str

    #: What the header says.
    label: str

    #: Where the value comes from, for the audit that reconciles this roster against
    #: the rollup's own columns: the rollup column's name for a contributed status,
    #: or the derived model's for one this module joins.
    origin: str

    #: The `core/filters.py` facet that narrows by this column.
    #:
    #: Declared here rather than reconciled by a test matching names, because the
    #: two vocabularies genuinely differ -- the column is `python_readiness` and the
    #: facet is `py314` -- and a test that guessed at the correspondence would
    #: either miss a real omission or invent one. A column shown but not filterable
    #: is the omission that matters: a reader can see a status and cannot narrow by
    #: it. `tests/unit/django_apps/test_health_filters.py` walks this.
    facet: str


#: The health table's status columns, in the order the view renders them.
#:
#: The order is the mockup's and is not arbitrary: currency, vulnerability, KEV,
#: licence, Python readiness, feedstock. It runs from the question asked most often
#: to the one asked least, and it keeps vulnerability and KEV adjacent because the
#: second qualifies the first and a reader who saw them apart would read
#: `not_listed` as reassurance about a critical advisory rather than about its
#: exploitation.
#:
#: Priority bucket, work type and identity confidence are **not** here. They are
#: columns of the table but not of this roster: each is read straight off the rollup
#: row and none of them has evidence behind it in the sense the others do -- a
#: priority bucket is derived from other verdicts, not observed from a source.
COLUMNS: Final[tuple[Column, ...]] = (
    Column(key="currency", label="Currency", origin="package_health.currency_status", facet="currency"),
    Column(
        key="vulnerability",
        label="Vulnerability",
        origin="package_vulnerability.vulnerability_status",
        facet="vuln",
    ),
    Column(key="kev", label="KEV", origin="package_vulnerability.kev_membership", facet="kev"),
    Column(key="licence", label="Licence", origin="package_license.license_outcome", facet="licence"),
    Column(
        key="python_readiness",
        label="Py 3.14",
        origin="package_python_readiness.readiness",
        facet="py314",
    ),
    Column(
        key="feedstock",
        label="Feedstock",
        origin="package_health.feedstock_presence_status",
        facet="feedstock",
    ),
)


@dataclass(frozen=True, slots=True)
class HealthRow:
    """One package's current health, with everything a row of the table shows.

    A view model rather than a model instance, and deliberately: `CPM-AD-10` gives
    the application layer no write path to a derived status, and a frozen dataclass
    is a shape a template cannot save through even by accident.
    """

    #: The package, for the link to its detail view.
    package_id: int
    canonical_name: str

    #: The three statuses read straight off the rollup row, plus the score that
    #: ranks it. The score is `None` for a package no priority row was written for.
    confidence: str
    priority: str
    work_type: str
    score: int | None

    #: The freshness `CPM-AD-11` requires every view to display, per row -- because
    #: a replayed run leaves rows computed at different instants, and a single
    #: header stamp would then be wrong for some of them.
    computed_at: datetime
    evidence_cutoff: datetime
    policy_versions: dict[str, str]

    #: One cell per `COLUMNS` entry, **in that order**.
    #:
    #: A tuple rather than a mapping, because the template renders headers from
    #: `COLUMNS` and cells from here, and a mapping would need a lookup filter
    #: taking a variable key -- which Django's template language does not have, and
    #: which every codebase that wants one grows a `dictkey` filter for. The
    #: alignment is by construction below and asserted by
    #: `tests/unit/django_apps/test_health_projection.py`.
    cells: tuple[Cell, ...]

    #: What the inventory said about this package at the run's cut-off
    #: (`CPM-OPERATE-S11`): when it stopped listing it, and when it last did. Both
    #: `None` for a listed package. Read off the rollup row, so a list of ten
    #: thousand rows labels its absent packages at no extra query; the template
    #: prints `surface/labels.py`'s tag from the pair and nothing else changes --
    #: every status on the row is computed exactly as it was.
    inventory_absent_since: datetime | None = None
    inventory_last_listed: datetime | None = None


def health_rows(page: Sequence[PackageHealth]) -> tuple[HealthRow, ...]:
    """Return the rows a page of the health table renders, evidence included.

    Args:
        page: The rollup rows for this page, already sliced by the paginator. A
            sequence rather than a queryset because it must not be re-evaluated:
            the whole point is a bounded number of queries against a settled page,
            and a lazy queryset handed in here would be evaluated again per lookup.

    Returns:
        One `HealthRow` per input row, in the order given. Empty for an empty page,
        which costs no queries at all.

    """
    if not page:
        return ()

    keys = [(row.package_id, row.policy_run_id) for row in page]
    currency = _by_key(_derived(PackageCurrency, keys, *SURFACE_SNAPSHOT_FIELDS.values()))
    vulnerability = _by_key(_derived(PackageVulnerability, keys, "vulnerability_finding", "kev_finding"))
    licence = _by_key(_derived(PackageLicense, keys, "license_finding"))
    readiness = _by_key(_derived(PackagePythonReadiness, keys, "assessment", "verification"))
    feedstock = _by_key(_derived(PackageFeedstockPresence, keys, "feedstock_snapshot"))
    priority = _by_key(_derived(PackagePriority, keys))

    return tuple(
        HealthRow(
            package_id=row.package_id,
            canonical_name=row.package.canonical_name,
            confidence=row.confidence,
            priority=row.priority_status,
            work_type=row.work_type_status,
            score=getattr(priority.get(key), "score", None),
            computed_at=row.computed_at,
            evidence_cutoff=row.evidence_cutoff,
            policy_versions=row.policy_versions,
            # In `COLUMNS` order, which is what the template relies on.
            cells=(
                _currency_cell(row, currency.get(key)),
                _vulnerability_cell(row, vulnerability.get(key)),
                _kev_cell(row, vulnerability.get(key)),
                _licence_cell(row, licence.get(key)),
                _readiness_cell(row, readiness.get(key)),
                _feedstock_cell(row, feedstock.get(key)),
            ),
            inventory_absent_since=row.inventory_absent_since,
            inventory_last_listed=row.inventory_last_listed,
        )
        for row, key in zip(page, keys, strict=True)
    )


def _derived[DerivedRow: models.Model](
    model: type[DerivedRow],
    keys: Sequence[tuple[int, int]],
    *evidence: str,
) -> Iterable[DerivedRow]:
    """Return one derived table's rows for a page, with their evidence attached.

    One query per table however many packages the page holds, which is the whole of
    AC 5's query bound. The filter is over the two id columns separately rather than
    over the pairs, because a pair-wise `IN` is not portable and the surplus rows a
    separated filter can return -- a package on this page at a run that is on the
    page for a *different* package -- are dropped by the pair-keyed lookup below.

    Args:
        model: The derived model to read.
        keys: The `(package_id, policy_run_id)` pairs on this page.
        *evidence: Foreign keys to follow, so the observation timestamps come back
            in the same query rather than one per cell.

    Returns:
        The matching rows.

    """
    # `_default_manager` rather than `objects`, which is the manager a *named*
    # model has and a type variable over `Model` does not. Same object here --
    # none of these declares a second manager -- and it is what lets one function
    # read six tables rather than six near-identical ones.
    queryset = model._default_manager.filter(  # noqa: SLF001 - see above
        package_id__in={package_id for package_id, _ in keys},
        policy_run_id__in={run_id for _, run_id in keys},
    )
    return queryset.select_related(*evidence) if evidence else queryset


def _by_key[DerivedRow: models.Model](rows: Iterable[DerivedRow]) -> dict[tuple[int, int], DerivedRow]:
    """Index derived rows by the pair `CPM-AD-21` keys them on.

    Args:
        rows: The rows to index.

    Returns:
        One entry per `(package_id, policy_run_id)`. The pair is unique per derived
        table by constraint, so nothing is silently dropped here.

    """
    return {(row.package_id, row.policy_run_id): row for row in rows}  # type: ignore[attr-defined]


def _observed_at(row: models.Model | None, field: str) -> datetime | None:
    """Return when a piece of evidence was observed, or nothing.

    Args:
        row: The derived row, or `None` when the pass wrote none.
        field: The evidence foreign key to read.

    Returns:
        The evidence row's `observed_at`, or `None` when there is no derived row or
        the derived row cites no evidence -- which is an ordinary state for a
        verdict of `not_found` or one the confidence gate produced.

    """
    evidence = getattr(row, field, None) if row is not None else None
    return getattr(evidence, "observed_at", None)


def _currency_cell(row: PackageHealth, derived: models.Model | None) -> Cell:
    """Return the currency cell: the rollup's verdict, dated by its chosen authority.

    The status is the rollup's rather than the derived row's, and that is
    `CPM-AD-11` rather than an optimisation: the contributed column has been through
    `CPM-AD-4`'s confidence gate and the derived row has not, so an unmapped package
    reads `unknown` here and whatever the pass computed there. The two disagreeing is
    the gate working.

    Args:
        row: The rollup row.
        derived: The `PackageCurrency` row for this package at this run, if any.

    Returns:
        The cell, noting which version surface decided it.

    """
    authority = getattr(derived, "chosen_authority", "") or ""
    snapshot = SURFACE_SNAPSHOT_FIELDS.get(authority)
    return Cell(
        status=row.currency_status,
        note=authority or (NO_ROW_NOTE if derived is None else "no authority chosen"),
        observed_at=_observed_at(derived, snapshot) if snapshot else None,
    )


def _gated(status: str, confidence: str) -> str:
    """Return what this surface may claim, given how certain the package's identity is.

    `CPM-AD-4`, applied to a status read out of a derived table rather than off the
    rollup. See the module docstring: the rollup's columns are gated on the way in
    and a derived table holds what the pass wrote, so a surface reading the second
    has to gate it itself or it will contradict the first on the same row.

    Args:
        status: The verdict the pass stored.
        confidence: The package's identity confidence, off the rollup row.

    Returns:
        `unknown` for an unmapped package, and the verdict unchanged otherwise.

    """
    return gated_status(status, confidence=confidence)


def _vulnerability_cell(row: PackageHealth, derived: models.Model | None) -> Cell:
    """Return the vulnerability cell, dated by the advisory finding behind it.

    Args:
        row: The rollup row, for the identity confidence the verdict is gated on.
        derived: The `PackageVulnerability` row, if any.

    Returns:
        The cell. `risk_level` is the note rather than a second column: it is what
        the status means -- `advisories_matched` is not a severity -- and the mockup
        shows the two together for that reason.

    """
    if derived is None:
        return Cell(status=OutcomeState.UNKNOWN.value, note=NO_ROW_NOTE)
    return Cell(
        status=_gated(derived.vulnerability_status, row.confidence),  # type: ignore[attr-defined]
        note=derived.risk_level or "advisory",  # type: ignore[attr-defined]
        observed_at=_observed_at(derived, "vulnerability_finding"),
    )


def _kev_cell(row: PackageHealth, derived: models.Model | None) -> Cell:
    """Return the KEV membership cell, dated by the catalogue finding behind it.

    A column of its own rather than a decoration on the vulnerability cell: `CPM-FR-11`
    makes known exploitation a separate fact from severity, and a reader filtering for
    it needs something to filter.

    Args:
        row: The rollup row, for the identity confidence the verdict is gated on.
        derived: The `PackageVulnerability` row, if any -- KEV membership is written
            on the same row, by the same pass.

    Returns:
        The cell.

    """
    if derived is None:
        return Cell(status=OutcomeState.UNKNOWN.value, note=NO_ROW_NOTE)
    return Cell(
        status=_gated(derived.kev_membership, row.confidence),  # type: ignore[attr-defined]
        note="kev",
        observed_at=_observed_at(derived, "kev_finding"),
    )


def _licence_cell(row: PackageHealth, derived: models.Model | None) -> Cell:
    """Return the licence cell, dated by the finding behind it.

    Args:
        row: The rollup row, for the identity confidence the verdict is gated on.
        derived: The `PackageLicense` row, if any.

    Returns:
        The cell, noting the rule that matched -- which is what makes an `allowed`
        verdict checkable rather than merely reassuring.

    """
    if derived is None:
        return Cell(status=OutcomeState.UNKNOWN.value, note=NO_ROW_NOTE)
    return Cell(
        status=_gated(derived.license_outcome, row.confidence),  # type: ignore[attr-defined]
        note=derived.matched_rule or "licence",  # type: ignore[attr-defined]
        observed_at=_observed_at(derived, "license_finding"),
    )


def _readiness_cell(row: PackageHealth, derived: models.Model | None) -> Cell:
    """Return the Python readiness cell, dated by whichever evidence produced it.

    `CPM-PY314-S03` made the *kind* of evidence part of the verdict: a `verified_`
    readiness rests on a build that ran and an `inferred_` one on static metadata,
    and the row names which in `evidence_type`. So the timestamp comes from the
    matching relation rather than from a fixed one -- reading `assessment` for a
    verified verdict would date a proof by the metadata it superseded.

    Args:
        row: The rollup row, for the identity confidence the verdict is gated on.
        derived: The `PackagePythonReadiness` row, if any.

    Returns:
        The cell.

    """
    if derived is None:
        return Cell(status=OutcomeState.UNKNOWN.value, note=NO_ROW_NOTE)
    evidence_type = derived.evidence_type  # type: ignore[attr-defined]
    relation = {
        ReadinessEvidence.VERIFIED.value: "verification",
        ReadinessEvidence.INFERRED.value: "assessment",
    }.get(evidence_type)
    return Cell(
        status=_gated(derived.readiness, row.confidence),  # type: ignore[attr-defined]
        note=evidence_type or "no evidence",
        observed_at=_observed_at(derived, relation) if relation else None,
    )


def _feedstock_cell(row: PackageHealth, derived: models.Model | None) -> Cell:
    """Return the feedstock-presence cell, dated by the feedstock snapshot behind it.

    The status is the rollup's for the reason `_currency_cell` gives: the contributed
    column is the gated one.

    Args:
        row: The rollup row.
        derived: The `PackageFeedstockPresence` row, if any.

    Returns:
        The cell.

    """
    return Cell(
        status=row.feedstock_presence_status,
        note="feedstock" if derived is not None else NO_ROW_NOTE,
        observed_at=_observed_at(derived, "feedstock_snapshot"),
    )
