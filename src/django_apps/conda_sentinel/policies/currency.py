"""`CPM-FR-16`: which surfaces state which version, judged against the right authority.

The first real policy pass. It reads the four evidence tables `CPM-EP-CURRENCY`'s
collectors write, as of the run's stated cut-off; picks the authoritative surface
by the order recorded on the package (`CPM-AD-6`); records a verdict for each
surface and one overall; writes its own derived row; and returns one rollup
column. It writes no evidence, makes no outbound call, and never writes the
rollup.

**The comparison is equality against the authority, and that is a decision with
a stated limit.** Version ordering across four ecosystems is a genuinely hard
problem -- PEP 440, conda's own ordering and a recipe's Jinja-set string do not
share a grammar -- and neither the PRD nor the architecture spine fixes a rule
for it. What `CPM-FR-16` asks for is that a package be compared "using a
documented release-authority order recorded per package", and what `CPM-SM-C1` is
about is a package being *called stale against a registry it never published to*.
Equality answers both: a surface stating the authority's version is `current`,
one stating a different version is `behind`, one stating nothing is `unknown`,
and one the package is not published on is `not_applicable` and is never compared
at all.

**What equality cannot do -- and this paragraph is the one statement of it.** The
same limits are load-bearing in five other places (the `detail` column, the
outcome vocabulary's missing `ahead` member, `docs/conda-sentinel/operations.md`'s operator
section, and both test modules), and each of those points here rather than
restating, because a rule stated six times is a rule that holds in one of them.

* `behind` means *different from the authority*, never *older than it*. A surface
  that has moved ahead reads `behind`, because deciding otherwise needs the
  ordering rule this pass does not have -- which is why `CurrencyOutcome` has no
  `ahead` member.
* Two spellings of one version read as two versions: an epoch, a build suffix, a
  PEP 440 normalisation, `1.0` against `1.0.0`.
* The **one** spelling difference reconciled is a leading `v` or `V` followed by
  a digit, plus surrounding whitespace. `CPM-FR-7` records "the latest release
  **or tag**", a Git tag is conventionally `v1.2.3` while a conda recipe and a
  PyPI project both state `1.2.3`, and left alone the default authority order
  would make almost every feedstock and every published package read `behind`
  against its own source -- a false staleness claim at inventory scale, which is
  the one outcome `CPM-SM-C1` names. Stripping the prefix is a statement about
  how a *tag* is spelled, not a rule about which of two versions is newer, so it
  does not cross into the ordering scheme this story declines to invent.
* A bare `v` names no version at all, so it can never be chosen as the authority
  and two surfaces both storing it never read as agreeing.

`comparable_version` below is the whole of the normalisation, and
`PackageCurrency.detail` is what lets a reader tell a real discrepancy from a
spelling one without re-deriving the comparison.

**One conda verdict for a table with one row per `(channel, platform)`.** This
pass reads the newest published-package observation at the cut-off and, among
the pairs one sweep wrote at that instant, prefers a pair that answered `ok`
over one that did not (`CPM-OPERATE-S06`); the row it writes references that
exact observation -- so the channel and platform the verdict is about are
readable from the finding rather than merged away. What that costs is real and
is recorded here: a package current on one pair and behind on another gets the
verdict of whichever `ok` pair the ordering names first. A verdict per pair is a
bigger table than `CPM-AD-21`'s `(package, policy_run)` key describes and is not
this story's to build.

**Nothing here reads the current time.** Every instant is the run's: the cut-off
arrives as an argument (`CPM-AD-21`) and the run row carries the rest. That is
what makes `CPM-FR-22`'s replay reproduce identical rows -- re-running one policy
version against one cut-off reads exactly the evidence the first run read,
because the evidence at or before a fixed instant does not change.

**A pass that cannot compute refuses rather than guessing.** An authority order
nothing can apply, and an evidence row carrying a state outside the vocabulary,
both raise. `core/policy_run.py` contains that per package (`CPM-AD-23`): this
package's derived rows roll back, every other package commits, the run finalizes
`partial` and the failure is logged with the package and the traceback. Falling
back to the default order, or reading an unrecognised state as an absence, would
each turn broken data into a clean-looking answer -- the opposite of
`CPM-NFR-3`'s "degrades to stale evidence, never to a clean result".

**On the `AD-` prefix.** A bare `AD-n` in this repository is an *inherited*
platform decision; a decision from this product's own architecture spine always
carries the `CPM-` prefix.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from typing import ClassVar
from typing import Final

from django.db.models import Case
from django.db.models import Value
from django.db.models import When

from conda_sentinel.collectors.models import CondaPackageSnapshot
from conda_sentinel.collectors.models import FeedstockSnapshot
from conda_sentinel.collectors.models import PyPIReleaseSnapshot
from conda_sentinel.collectors.models import SourceReleaseSnapshot
from conda_sentinel.core.clock import is_aware
from conda_sentinel.core.outcomes import SENTINEL_MEMBERS
from conda_sentinel.core.outcomes import OutcomeState
from conda_sentinel.core.policy import PolicyPass
from conda_sentinel.identity.models import VersionSurface
from conda_sentinel.identity.models import applied_authority_order
from conda_sentinel.policies.models import AuthorityOrderSource
from conda_sentinel.policies.models import PackageCurrency
from conda_sentinel.policies.outcomes import BEHIND
from conda_sentinel.policies.outcomes import CURRENT
from conda_sentinel.policies.outcomes import UNKNOWN
from conda_sentinel.policies.outcomes import worst_currency

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import datetime

    from django.db import models

    from conda_sentinel.core.models import PolicyRun
    from conda_sentinel.identity.models import Package

__all__ = [
    "DISCREPANCY_DETAIL",
    "POLICY_NAME",
    "ROLLUP_COLUMN",
    "SENTINEL_VALUES",
    "SURFACE_READERS",
    "CurrencyPass",
    "CurrencyPolicyError",
    "SnapshotModel",
    "SurfaceReader",
    "SurfaceReading",
    "authoritative_version",
    "comparable_version",
    "discrepancy_detail",
    "observed_surface",
    "overall_verdict",
    "surface_verdict",
]

#: The four evidence rows this pass reads, as one type.
#:
#: A union rather than `models.Model`, and it earns its place twice: `objects` is
#: declared on a concrete model rather than on the base, so a generic read would
#: not type-check at all, and `state` is a real column on each of the four -- so
#: the verdict below asks for it by name instead of through a `getattr` that
#: would answer for a model that does not have one.
SnapshotModel = SourceReleaseSnapshot | PyPIReleaseSnapshot | FeedstockSnapshot | CondaPackageSnapshot

#: What this pass is called. It keys `core/policy.py`'s registry, keys the
#: rollup's per-domain `policy_versions` map (`CPM-AD-11`) and appears in every
#: refusal about this pass, so it is spelled once and imported.
POLICY_NAME: Final[str] = "currency"

#: The rollup column this pass owns and the only one it contributes.
#: `core/policy.py` refuses a contribution to a column `core/rollup.py` does not
#: offer, so a misspelling here is a refusal at registration rather than a column
#: contributed nowhere for as long as the pass ran.
ROLLUP_COLUMN: Final[str] = "currency_status"

#: The four sentinel values, as strings, for recognising an evidence state that
#: is not the determinate one.
#:
#: A set of *values* built from `core.outcomes.SENTINEL_MEMBERS` rather than a
#: table keyed by `OutcomeState` members, and both halves of that are deliberate.
#: A mapping from each state to its verdict would be a lookup table this module
#: does not need -- `outcome_type` guarantees the two vocabularies spell the four
#: sentinels identically -- and a module-scope literal holding several
#: `OutcomeState` members is indistinguishable from a second precedence order to
#: `tests/unit/django_apps/test_single_ordering_audit.py`, which is a false
#: positive that module's docstring records and would rightly refuse.
SENTINEL_VALUES: Final[frozenset[str]] = frozenset(value for _, value in SENTINEL_MEMBERS)

#: The prefix a Git tag conventionally carries in front of a version. See the
#: module docstring for why this one spelling difference is reconciled and no
#: other is.
_TAG_PREFIX: Final[str] = "v"

#: How one surface's discrepancy is written into the row's `detail`.
#:
#: Both the *compared* form and the *stored* form, for each side, because the
#: difference between them is the whole of what this pass normalises: a reader
#: looking at a `behind` verdict needs to see whether the two surfaces disagree
#: about the version or only about how to spell it. A line naming the compared
#: forms alone would hide exactly the case a false `behind` is.
DISCREPANCY_DETAIL: Final[str] = (
    "{surface} states {stated!r} (compared as {stated_form!r}); {authority} states {authoritative!r} (compared as {authority_form!r})"  # noqa: E501 - one line per discrepancy, and splitting it would hide the shape
)


class CurrencyPolicyError(ValueError):
    """The currency pass met evidence or an argument it cannot compute from.

    A `ValueError` subclass, matching `core/policy.py`'s `PolicyPassError`,
    `core/outcomes.py`'s `OutcomeVocabularyError` and
    `identity/models.py`'s `AuthorityOrderError`: every "this is unusable" in
    this product is a `ValueError`, so a caller catching one catches them all.

    Raised rather than absorbed. `core/policy_run.py` catches whatever a pass
    raises and rolls that package back, which is `CPM-AD-23`'s partial success --
    so a refusal here costs one package's row and leaves every other package's
    committed, while a silent fallback would cost the truth of the whole column.
    """


@dataclass(frozen=True)
class SurfaceReader:
    """How to read one version surface: which table, which column, which reference.

    A record rather than four `if` branches, because the four surfaces differ in
    exactly these three ways and in nothing else -- so a fifth surface is an
    entry in `SURFACE_READERS` below rather than a fifth branch in every function
    here.

    Attributes:
        surface: The `VersionSurface` value this reads for.
        model: The evidence table it reads. Read only: this pass writes no
            evidence and changes no collector (`CPM-AD-8`).
        version_field: The column on that table holding the version the surface
            states. The four tables spell it three different ways --
            `latest_version` twice, `recipe_version` and `published_version` --
            because each collector named it after what its own source calls it.
        reference_field: The column on `PackageCurrency` that stores the
            observation this reading came from.
        tie_break: The columns that decide between two observations sharing an
            `observed_at` *and* a determinate-or-not state, applied ascending
            before the primary key. Empty for a surface whose table holds one row
            per package per sweep, which is three of the four;
            `conda_package_snapshots` holds one row per `(channel, platform)` and
            every row of one sweep carries that run's single instant
            (`CPM-AD-7`), so *every* row ties and something has to decide. See
            `observed_surface` for which pair wins and what that costs.

    """

    surface: str
    model: type[SnapshotModel]
    version_field: str
    reference_field: str
    tie_break: tuple[str, ...] = ()


@dataclass(frozen=True)
class SurfaceReading:
    """What one surface said at the cut-off, before anything is compared.

    Separated from the verdict on purpose: reading is a query and judging is
    arithmetic, and keeping them apart is what lets every comparison rule below
    be exercised without a database.

    Attributes:
        surface: The `VersionSurface` value this is about.
        observation: The newest evidence row at or before the cut-off, or `None`
            where the surface had none by then. `None` is not an error -- a
            cut-off earlier than a package's first observation is an ordinary
            question with an ordinary answer.
        version: The version the observation states, as stored, or `""` where it
            states none. A determinate observation may legitimately state no
            version: `FeedstockSnapshot` records a feedstock that exists whose
            recipe names its version in a way the collector does not read.

    """

    surface: str
    observation: SnapshotModel | None
    version: str


#: The annotation `observed_surface` orders on before a reader's own tie-break:
#: `0` for a row carrying `ok`, `1` for any sentinel. Named so the ordering key
#: reads as the rule it is; `tests/integration/django_apps/test_currency_policy.py`
#: pins it from both directions and across sweeps.
DETERMINATE_RANK: Final[str] = "determinate_rank"

#: How to read each of the four surfaces, in `VersionSurface`'s declared order.
#:
#: **This is not an authority order.** It is the set of surfaces that exist and
#: how to read each one; which of them is authoritative for a given package is
#: `identity.applied_authority_order`'s answer and differs per package. The two
#: are kept apart because collapsing them would make `CPM-AD-6`'s per-package
#: data a constant again.
SURFACE_READERS: Final[tuple[SurfaceReader, ...]] = (
    SurfaceReader(
        surface=VersionSurface.SOURCE.value,
        model=SourceReleaseSnapshot,
        version_field="latest_version",
        reference_field="source_snapshot",
    ),
    SurfaceReader(
        surface=VersionSurface.PYPI.value,
        model=PyPIReleaseSnapshot,
        version_field="latest_version",
        reference_field="pypi_snapshot",
    ),
    SurfaceReader(
        surface=VersionSurface.FEEDSTOCK.value,
        model=FeedstockSnapshot,
        version_field="recipe_version",
        reference_field="feedstock_snapshot",
    ),
    SurfaceReader(
        surface=VersionSurface.CONDA_PACKAGE.value,
        model=CondaPackageSnapshot,
        version_field="published_version",
        reference_field="conda_package_snapshot",
        tie_break=("channel", "platform"),
    ),
)


def comparable_version(stated: str) -> str:
    """Return the form of a stated version two surfaces are compared on.

    The whole of this pass's normalisation, and deliberately almost nothing:
    surrounding whitespace, and a single leading `v` or `V` where a digit follows
    it. See the module docstring for why the tag prefix is reconciled and why
    nothing else is.

    The stripped forms are compared; the *stored* strings are untouched, and the
    row this pass writes references the evidence rows so a reader always sees
    what each surface actually said.

    Args:
        stated: The version as the evidence row stores it.

    Returns:
        The form to compare on. `""` for a surface stating nothing, and `""` for
        a value that is nothing but the prefix: a bare `"v"` names no version,
        and letting it survive as one would let it be chosen as the authority and
        would make two surfaces that both state nothing read as agreeing.

    """
    trimmed = stated.strip()
    if trimmed in {_TAG_PREFIX, _TAG_PREFIX.upper()}:
        return ""
    if len(trimmed) > 1 and trimmed[0] in {_TAG_PREFIX, _TAG_PREFIX.upper()} and trimmed[1].isdigit():
        return trimmed[1:]
    return trimmed


def observed_surface(reader: SurfaceReader, *, package_id: int, cutoff: datetime) -> SurfaceReading:
    """Return what one surface said about one package at or before a stated cut-off.

    The cut-off-bound read `collectors/models.py`'s `snapshot_as_of` establishes,
    applied to the four release surfaces: newest `observed_at` first, then the
    reader's own tie-break columns ascending, then descending primary key. The
    tie-break is what makes a replay a replay -- one sweep writes every row it
    produces with the run's one instant (`CPM-AD-7`), so an unordered tie would
    let the database's arbitrary row order decide the verdict.

    **A separate implementation from `snapshot_as_of`, deliberately.** That
    helper is bound to `InventorySnapshot` and to one column, and this story's
    Never list forbids changing `collectors/models.py`'s code -- but the real
    reason the two are not one function is the paragraph above: this read needs a
    per-surface tie-break that helper has no notion of, because the table it
    reads holds at most one row per package per sweep and one of these tables
    does not. The rule the two share is stated in each and reconciled by
    `tests/unit/django_apps/test_currency_policy.py`, which asserts the ordering
    key directly rather than trusting either prose.

    **Which `(channel, platform)` pair the published-package verdict is about.**
    Every row of one sweep ties on `observed_at`, so without a stated key the
    answer would be whichever row happened to be inserted last -- a verdict that
    changes when the collector's channel list is reordered, with nothing saying
    so. The key is, first, whether the row is determinate: among the pairs of
    one instant an `ok` row is read before any sentinel, so a package published
    only as `noarch` is judged by its `noarch` row and not by the `not_found`
    the compiled platform beside it wrote (`CPM-OPERATE-S06`). A sentinel is the
    verdict only when no pair of that instant answered `ok`. Then the channel,
    then the platform, both ascending: among equals the pair whose channel sorts
    first alphabetically wins, then its first platform, then that pair's newest
    row. Alphabetical is arbitrary and is chosen only because it is *fixed*; what
    matters is that the same evidence produces the same verdict on every replay,
    and that the row references the observation so a reader can see which pair
    it was.

    The preference is *within* one instant, never across sweeps: the newest
    observation still comes first, so a package withdrawn from a channel reads
    today's `not_found` rather than yesterday's `ok`. What remains a limitation is
    that a package current on one pair and behind on another gets the verdict of
    whichever `ok` pair sorts first. The per-pair verdict that would fix that is a
    larger table than `CPM-AD-21`'s `(package, policy_run)` key describes.

    Args:
        reader: Which surface to read, and how.
        package_id: The package being asked about, by the integer primary key
            `CPM-AD-3` fixes.
        cutoff: The instant to read as of, aware. `CPM-AD-21` makes it the
            `finished_at` of a completed collection run, so a pass never reads
            evidence written by a run that is still `running`.

    Returns:
        The observation and the version it states, or an empty reading where the
        surface had no observation by then.

    Raises:
        CurrencyPolicyError: When `cutoff` is naive. Refused rather than
            converted, on the same terms `snapshot_as_of` refuses one: there is
            no offset to convert from, `USE_TZ` is on so Django would read it as
            if it were UTC, and a cut-off silently shifted by the reader's offset
            selects a different evidence set on every replay -- which is the
            opposite of what `CPM-FR-22` promises.

    """
    if not is_aware(cutoff):
        message = (
            f"the {reader.surface} surface cannot be read as of the naive cutoff {cutoff!r}. Every instant "
            f"comes from a Clock, which always answers in UTC (CPM-AD-26); a naive value has no offset to "
            f"interpret, so the read would be silently shifted by whichever offset the reader happened to be "
            f"in and the replay CPM-FR-22 promises would return a different set each time."
        )
        raise CurrencyPolicyError(message)
    observation = (
        reader.model.objects.filter(package_id=package_id, observed_at__lte=cutoff)
        .annotate(**{DETERMINATE_RANK: Case(When(state=OutcomeState.OK, then=Value(0)), default=Value(1))})
        .order_by("-observed_at", DETERMINATE_RANK, *reader.tie_break, "-pk")
        .first()
    )
    version = "" if observation is None else str(getattr(observation, reader.version_field))
    return SurfaceReading(surface=reader.surface, observation=observation, version=version)


def _states_a_version(reading: SurfaceReading) -> bool:
    """Report whether a reading is a determinate observation naming a version.

    The one predicate "can this surface be the authority" and "is this surface
    comparable" both ask, so the two cannot come to disagree: an authority that
    named no version would have nothing for anything to be compared against.

    Args:
        reading: What the surface said.

    Returns:
        True when the observation exists, carries `ok`, and states a version once
        it has been through `comparable_version`. A determinate feedstock
        observation whose recipe version the collector could not read states
        none, and is the case this predicate exists for.

    """
    observation = reading.observation
    if observation is None or observation.state != OutcomeState.OK:
        return False
    return comparable_version(reading.version) != ""


def authoritative_version(
    readings: Mapping[str, SurfaceReading],
    order: tuple[str, ...],
) -> tuple[str, str]:
    """Return the surface that is authoritative for this package, and the version it states.

    `CPM-AD-6`'s choice: the first surface in the applied order that actually
    stated a version at the cut-off. A surface earlier in the order that was
    unobserved, that errored, that answered `not_found`, or that is
    `not_applicable` to this package is passed over -- which is the whole point
    of the order being a *ranking* rather than a single choice, and is what stops
    a package being judged against a registry it never published to.

    Args:
        readings: What every surface said, by `VersionSurface` value.
        order: The applied authority order, best first. May legitimately be
            shorter than the full set of surfaces: only what it names can be
            chosen, and a surface it leaves out is still read and still recorded
            but can never be the authority.

    Returns:
        The chosen surface and its comparable version. `("", "")` when no entry
        of the order stated a version -- an ordinary answer for a package nothing
        has observed yet, and the state in which no surface can be `current` or
        `behind` because there is nothing to compare against.

    """
    for surface in order:
        reading = readings.get(surface)
        if reading is not None and _states_a_version(reading):
            return surface, comparable_version(reading.version)
    return "", ""


def surface_verdict(reading: SurfaceReading, *, authority_version: str) -> str:
    """Return one surface's currency verdict.

    The five states stay un-collapsed (`CPM-FR-6`): each of the four sentinels an
    observation can carry becomes the same sentinel here, and only a determinate
    observation naming a version is compared. In particular a surface with no
    observation is `unknown` and never `ok`, and a surface the package is not
    published on is `not_applicable` and never makes the package `behind`.

    **A sentinel state is returned unchanged, and that is a guarantee rather than
    a coincidence.** `core.outcomes.outcome_type` builds every per-status type
    from `SENTINEL_MEMBERS`, so `CurrencyOutcome.ERROR` carries exactly the string
    `OutcomeState.ERROR` does -- `verify_sentinels` refuses to build a type where
    it does not, and that check runs at import in `policies/outcomes.py`. So the
    four cases are one line rather than four, and
    `tests/unit/django_apps/test_currency_policy.py` pins each sentinel against
    this module's own named constant so the identity is asserted rather than
    assumed.

    What is *not* passed through is `ok`. The generic determinate value is the one
    a per-status type refines (`CPM-AD-5`), so it is never a verdict here: it
    becomes `current` or `behind`, or `unknown` where there is nothing to compare.

    Args:
        reading: What the surface said at the cut-off.
        authority_version: The comparable version the chosen authority states, or
            `""` where no authority could be chosen.

    Returns:
        A `CurrencyOutcome` value. `unknown` where nothing was observed, where
        the observation states no version, or where there is no authority to
        compare against -- the last because a version nothing can be measured
        against is a fact about the surface and not a verdict about the package.

    Raises:
        CurrencyPolicyError: When the observation carries a state outside
            `OutcomeState`. `choices` is a form rule Django does not enforce on
            `save()`, so such a row is reachable; reading it as an absence would
            let a value nobody recognises become a clean-looking answer.

    """
    observation = reading.observation
    if observation is None:
        return UNKNOWN
    state = observation.state
    if state in SENTINEL_VALUES:
        return state
    if state != OutcomeState.OK:
        message = (
            f"the {reading.surface} observation {observation.pk} carries state {state!r}, which is not one "
            f"of {sorted(OutcomeState.values)}. CPM-AD-5 fixes the vocabulary an evidence state is drawn "
            f"from; a currency verdict derived from a value outside it would be a claim about the package "
            f"resting on a value nothing in this product recognises."
        )
        raise CurrencyPolicyError(message)

    stated = comparable_version(reading.version)
    if not stated or not authority_version:
        return UNKNOWN
    return CURRENT if stated == authority_version else BEHIND


def overall_verdict(verdicts: Mapping[str, str]) -> str:
    """Reduce the per-surface verdicts to the one this pass contributes to the rollup.

    One line, because the reduction is `policies/outcomes.py`'s: the order is
    declared as data beside the vocabulary it ranks, and `worst_currency` applies
    it. Both the ranking and the reason `not_applicable` is excluded from it are
    argued there, in the one place they are stated.

    This wrapper exists because the pass thinks in surfaces and the reducer
    thinks in verdicts, and the mapping's *keys* are what every case and every
    column here is keyed by. It is deliberately not the place any rule lives.

    Args:
        verdicts: Each surface's verdict, by `VersionSurface` value.

    Returns:
        A `CurrencyOutcome` value.

    """
    return worst_currency(verdicts.values())


def discrepancy_detail(
    readings: Mapping[str, SurfaceReading],
    verdicts: Mapping[str, str],
    *,
    authority: str,
) -> str:
    """Return the row's account of every surface it called `behind`, or `""`.

    **The column this fills is what makes a `behind` verdict checkable without
    re-deriving it.** The row already references the evidence, so a reader *can*
    join four tables and compare two strings by eye -- which is the work the row
    exists to remove. The module docstring states what `behind` does and does not
    mean; the consequence for this line is that the commonest false one is two
    surfaces spelling one version differently, so it records both what each
    surface stored and what it was compared as.

    Empty when nothing is behind, which is the ordinary case: `detail` is an
    explanation, and an explanation of an unremarkable row is noise. That is the
    same rule every evidence table in this product applies to its own `detail`.

    Args:
        readings: What every surface said, by `VersionSurface` value.
        verdicts: Each surface's verdict, by the same key.
        authority: The chosen authoritative surface, or `""` where none was.

    Returns:
        One line per surface reading `behind`, in `SURFACE_READERS` order so two
        runs over one package produce byte-identical text. Empty where no surface
        is behind -- which includes every row with no authority, because nothing
        can be behind when nothing was compared.

    """
    authoritative = readings[authority].version if authority else ""
    return "\n".join(
        DISCREPANCY_DETAIL.format(
            surface=reader.surface,
            stated=readings[reader.surface].version,
            stated_form=comparable_version(readings[reader.surface].version),
            authority=authority,
            authoritative=authoritative,
            authority_form=comparable_version(authoritative),
        )
        for reader in SURFACE_READERS
        if verdicts[reader.surface] == BEHIND
    )


class CurrencyPass(PolicyPass):
    """`CPM-FR-16` as a `PolicyPass`: read four surfaces, judge them, write one row.

    Three declarations and one method, exactly as `core/policy.py` asks. The
    derived table is `PackageCurrency` and the one rollup column is
    `currency_status`; this class never writes the rollup and never sees a
    confidence, because `CPM-AD-4`'s gate is applied by the one rollup writer on
    the way in and a pass is not asked (`CPM-AD-21`).
    """

    name: ClassVar[str] = POLICY_NAME
    derived_model: ClassVar[type[models.Model] | None] = PackageCurrency
    contributes: ClassVar[tuple[str, ...]] = (ROLLUP_COLUMN,)

    def evaluate(
        self,
        package: Package,
        *,
        policy_run: PolicyRun,
        evidence_cutoff: datetime,
    ) -> Mapping[str, str]:
        """Judge one package's version currency and write its derived row.

        Called once per package, inside that package's transaction
        (`CPM-AD-23`), so a refusal here rolls this package's row back and leaves
        every other package's committed.

        Args:
            package: The package to judge. Its `version_authority_order` is read
                off the instance the run is holding rather than re-queried.
            policy_run: The run this evaluation belongs to. Written onto the
                derived row, where together with the package it is `CPM-AD-21`'s
                key.
            evidence_cutoff: The instant to read evidence as of. Nothing here
                reads the current time.

        Returns:
            The one rollup column this pass declared, carrying the overall
            verdict. Ungated: `core/rollup.py` applies `CPM-AD-4` to it, and an
            `unmapped` package's rollup column reads `unknown` however this pass
            judged it.

        Raises:
            AuthorityOrderError: When the package records an authority order
                nothing can apply.
            CurrencyPolicyError: When an evidence row carries a state outside the
                outcome vocabulary, or when the cut-off is naive.

        """
        order, is_default = applied_authority_order(package)
        readings = {
            reader.surface: observed_surface(reader, package_id=package.pk, cutoff=evidence_cutoff)
            for reader in SURFACE_READERS
        }
        authority, authority_version = authoritative_version(readings, order)
        verdicts = {
            surface: surface_verdict(reading, authority_version=authority_version)
            for surface, reading in readings.items()
        }
        overall = overall_verdict(verdicts)
        detail = discrepancy_detail(readings, verdicts, authority=authority)
        # Written with one keyword per column rather than through a `defaults`
        # mapping. `tests/unit/django_apps/test_derived_status_writability_audit.py`
        # reads keyword names, so the mapping form would take this write out of
        # that audit's view -- which is the `**kwargs` dodge that module names.
        # The visible form plus a recorded exemption is the honest shape, and the
        # exemption records what this write is: the currency pass writing its own
        # per-domain table (`CPM-AD-21`), never current package health.
        PackageCurrency.objects.create(
            package=package,
            policy_run=policy_run,
            source_status=verdicts[VersionSurface.SOURCE.value],
            pypi_status=verdicts[VersionSurface.PYPI.value],
            feedstock_status=verdicts[VersionSurface.FEEDSTOCK.value],
            conda_package_status=verdicts[VersionSurface.CONDA_PACKAGE.value],
            overall_status=overall,
            detail=detail,
            chosen_authority=authority,
            authority_order=list(order),
            authority_order_source=(
                AuthorityOrderSource.DEFAULT.value if is_default else AuthorityOrderSource.PACKAGE.value
            ),
            **{reader.reference_field: readings[reader.surface].observation for reader in SURFACE_READERS},
        )
        return {ROLLUP_COLUMN: overall}
