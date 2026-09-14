"""The currency pass through a real policy run, against real evidence tables.

`tests/unit/django_apps/test_currency_policy.py` holds the rules: the
comparison, the authority choice, the reduction and every declaration. None of
those needs a database. What does need one is everything the rules are *wrapped
in* -- the cut-off-bound read, the row the pass writes, the replay that must
reproduce it, and the rollup column arriving through the orchestration rather
than from the pass.

**The pass is never called directly.** Every case here runs `execute_policy_run`,
because what `CPM-AD-21` promises is a property of the orchestration: the cut-off
comes from the run ledger, the pass runs inside the package's transaction, and
`core/rollup.py` writes the column after `CPM-AD-4`'s gate. A case that called
`CurrencyPass().evaluate(...)` would prove the arithmetic a second time and the
arrangement not at all.

**The pass is already registered, and nothing here registers it.**
`policies/apps.py` adopts it during `django.setup()`, which is the arrangement a
deployed process is in -- so a case that registered it would be measuring its own
fixture. `tests/unit/django_apps/test_policies_app.py` is where the adoption
itself is asserted.

**Time comes from stopped clocks, never from the wall.** `tests/clocks.py` owns
the instants and derives the later ones from the earlier, so the ordering the
cut-off cases assert cannot drift.

Every case rolls back: `@pytest.mark.django_db` wraps each in a transaction,
which is what leaves the database as found.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from typing import Any
from typing import Final

import pytest
from django.db import IntegrityError
from django.db import transaction

from conda_sentinel.collectors.models import CondaPackageSnapshot
from conda_sentinel.collectors.models import FeedstockSnapshot
from conda_sentinel.collectors.models import PyPIReleaseSnapshot
from conda_sentinel.collectors.models import SourceReleaseSnapshot
from conda_sentinel.core.clock import FixedClock
from conda_sentinel.core.confidence import GATED_VALUE
from conda_sentinel.core.models import CollectionRun
from conda_sentinel.core.models import PackageHealth
from conda_sentinel.core.models import PolicyRun
from conda_sentinel.core.outcomes import OutcomeState
from conda_sentinel.core.policy_run import choose_evidence_cutoff
from conda_sentinel.core.policy_run import execute_policy_run
from conda_sentinel.core.runs import RunState
from conda_sentinel.identity.models import DEFAULT_AUTHORITY_ORDER
from conda_sentinel.identity.models import IdentityConfidence
from conda_sentinel.identity.models import Package
from conda_sentinel.identity.models import VersionSurface
from conda_sentinel.policies.currency import POLICY_NAME
from conda_sentinel.policies.currency import CurrencyPass
from conda_sentinel.policies.models import AUTHORITY_IS_A_KNOWN_SURFACE
from conda_sentinel.policies.models import DETERMINATE_VERDICT_NEEDS_AN_AUTHORITY
from conda_sentinel.policies.models import SURFACE_STATUS_FIELDS
from conda_sentinel.policies.models import AuthorityOrderSource
from conda_sentinel.policies.models import PackageCurrency
from conda_sentinel.policies.outcomes import BEHIND
from conda_sentinel.policies.outcomes import CURRENT
from conda_sentinel.policies.outcomes import ERROR
from conda_sentinel.policies.outcomes import NOT_APPLICABLE
from conda_sentinel.policies.outcomes import NOT_FOUND
from conda_sentinel.policies.outcomes import UNKNOWN
from tests.clocks import FIXED_INSTANT
from tests.clocks import LATER_INSTANT
from tests.clocks import OBSERVATION_GAP
from tests.passes import A_RECORDED_POLICY_VERSION

if TYPE_CHECKING:
    from datetime import datetime

#: The policy version every case records.
#:
#: It was any stable string until `CPM-CURRENCY-S07`, because `CPM-AD-8` makes the
#: version data an operator supplies rather than a constant this product ships.
#: It is now the version the shipped parameter file records, because the second
#: adopted pass looks its threshold up by the run's version and refuses one the
#: file does not record -- so a run here at a fixture version would fail every
#: package, and every case in this module would be measuring that instead of the
#: currency pass. `tests/passes.py` carries the constant and says why.
A_POLICY_VERSION: Final[str] = A_RECORDED_POLICY_VERSION

#: The collector name the fixture collection runs carry. Prefixed so it cannot be
#: confused with a real collector's.
A_COLLECTOR: Final[str] = "cpm-fixture-collector"

#: The version the authoritative surface states, and a different one for the
#: surface that is behind. Which versions they are does not matter: this pass
#: compares for equality and deliberately does not order.
AN_AUTHORITY_VERSION: Final[str] = "2.4.0"
A_DIFFERENT_VERSION: Final[str] = "2.3.1"

#: A feedstock name, because `feedstock_snapshots` requires one of every
#: determinate row -- "a feedstock exists" and "this is which one" are one fact.
A_FEEDSTOCK_NAME: Final[str] = "numpy-feedstock"

#: The channel and platform a published-package observation is about.
#: `conda_package_snapshots` requires both of every row, sentinel rows included.
#: The two channel names order, which is what the tie-break case is about: the
#: pass reads the pair whose channel sorts first, so `A_CHANNEL` is the one whose
#: verdict a package with rows on both gets.
A_CHANNEL: Final[str] = "conda-forge"
A_LATER_CHANNEL: Final[str] = "internal"
A_PLATFORM: Final[str] = "linux-64"
#: The subdir a pure-Python package is published under, and the one that sorts
#: *after* `A_PLATFORM` -- which is the whole point of the cases that use it.
A_NOARCH_PLATFORM: Final[str] = "noarch"

#: The source's tagged spelling of `AN_AUTHORITY_VERSION`, and a version any
#: ordering rule would call newer than it. The first is the one spelling
#: difference this pass reconciles; the second is what separates an equality rule
#: from a smuggled-in ordering.
A_TAGGED_VERSION: Final[str] = f"v{AN_AUTHORITY_VERSION}"
A_LATER_VERSION: Final[str] = "2.5.0"

#: A surface name no `VersionSurface` member carries. The database refuses it --
#: `chosen_authority` declares `choices`, which Django does not enforce on
#: `save()`, so the value is reachable in Python and a row built directly is the
#: only way to show the constraint refusing one.
AN_INVENTED_SURFACE: Final[str] = "deployed"

#: How many evidence queries this pass issues per package: one per surface. Named
#: so the query-count case reads as the claim it makes rather than as a magic
#: four, and so a fifth surface changes one number.
SURFACE_QUERIES_PER_PACKAGE: Final[int] = 4

#: What one `evaluate` costs in total: the four reads plus the one insert of the
#: derived row. Derived rather than written out, so the two halves of the cost
#: stay legible and a change to either says which one moved.
QUERIES_PER_PACKAGE: Final[int] = SURFACE_QUERIES_PER_PACKAGE + 1

#: An instant after the run's cut-off, for the case about evidence the cut-off
#: excludes. Derived from `OBSERVATION_GAP` rather than written out, so the two
#: instants cannot drift into an ordering nobody intended.
AFTER_THE_CUTOFF: Final = FIXED_INSTANT + OBSERVATION_GAP

#: How many rows two runs over one package leave behind, and how many packages
#: the multi-package case is over. Named because the two cases mean different
#: things by it: one counts runs, the other counts packages, and a shared
#: literal would hide that they are two facts that happen to agree.
REPLAYED_RUNS: Final[int] = 2
PACKAGES_IN_THE_INVENTORY: Final[int] = 2


def an_ended_collection_run(finished_at: datetime = FIXED_INSTANT) -> CollectionRun:
    """Record a collection run that has ended, which is what supplies the cut-off.

    `CPM-AD-21` makes the cut-off the `finished_at` of a completed collection run
    and forbids the current time, so a run with nothing completed behind it
    refuses outright. Written directly rather than through `core/ledger.py`'s
    recorder, because what these cases need is a row with a *chosen*
    `finished_at`: the recorder reads its own clock.

    Args:
        finished_at: When the run ended, and therefore the cut-off every pass in
            the policy run reads evidence as of.

    Returns:
        The saved row.

    """
    return CollectionRun.objects.create(
        collector=A_COLLECTOR,
        started_at=FIXED_INSTANT,
        finished_at=finished_at,
        status=RunState.SUCCEEDED,
    )


def a_package(
    name: str = "numpy",
    *,
    confidence: str = IdentityConfidence.VERIFIED,
    authority_order: list[str] | None = None,
) -> Package:
    """Create one package with a resolved identity.

    Args:
        name: Its canonical name, which is unique.
        confidence: How certain its identity is. `verified` unless a case is
            about `CPM-AD-4`'s gate.
        authority_order: What to record in `version_authority_order`. `None`
            leaves the column at its own default, which is the empty list every
            package carries today and what "no authority is explicitly set"
            means.

    Returns:
        The saved `Package`. `resolved_at` comes from `tests.clocks.FIXED_INSTANT`
        rather than from the wall clock, exactly as `CPM-AD-26` requires of every
        writer.

    """
    return Package.objects.create(
        canonical_name=name,
        resolved_at=FIXED_INSTANT,
        confidence=confidence,
        version_authority_order=[] if authority_order is None else authority_order,
    )


def a_source_observation(
    package: Package,
    *,
    version: str = AN_AUTHORITY_VERSION,
    state: str = OutcomeState.OK,
    observed_at: datetime = FIXED_INSTANT,
) -> SourceReleaseSnapshot:
    """Record one upstream-release observation.

    Written directly rather than through the collector: what these cases are
    about is the pass reading evidence, and driving a collection would make each
    of them depend on a transport none of them is about.

    Args:
        package: The package observed.
        version: The version the source states. Ignored for a sentinel state,
            which the table's own constraint requires to carry none.
        state: What the lookup concluded.
        observed_at: The instant of this observation.

    Returns:
        The saved row.

    """
    return SourceReleaseSnapshot.objects.create(
        package=package,
        observed_at=observed_at,
        state=state,
        latest_version=version if state == OutcomeState.OK else "",
    )


def a_pypi_observation(
    package: Package,
    *,
    version: str = AN_AUTHORITY_VERSION,
    state: str = OutcomeState.OK,
    observed_at: datetime = FIXED_INSTANT,
) -> PyPIReleaseSnapshot:
    """Record one PyPI observation, on the terms `a_source_observation` states.

    Args:
        package: The package observed.
        version: The version PyPI states.
        state: What the lookup concluded. `not_applicable` is `CPM-FR-8`'s row
            for a package that is not a Python package at all.
        observed_at: The instant of this observation.

    Returns:
        The saved row.

    """
    return PyPIReleaseSnapshot.objects.create(
        package=package,
        observed_at=observed_at,
        state=state,
        latest_version=version if state == OutcomeState.OK else "",
    )


def a_feedstock_observation(
    package: Package,
    *,
    version: str = AN_AUTHORITY_VERSION,
    state: str = OutcomeState.OK,
    observed_at: datetime = FIXED_INSTANT,
) -> FeedstockSnapshot:
    """Record one feedstock observation, on the terms `a_source_observation` states.

    Args:
        package: The package observed.
        version: The version the recipe pins.
        state: What the lookup concluded.
        observed_at: The instant of this observation.

    Returns:
        The saved row.

    """
    return FeedstockSnapshot.objects.create(
        package=package,
        observed_at=observed_at,
        state=state,
        feedstock_name=A_FEEDSTOCK_NAME if state == OutcomeState.OK else "",
        recipe_version=version if state == OutcomeState.OK else "",
    )


def a_published_package_observation(  # noqa: PLR0913 - one parameter per column a case may need to vary
    package: Package,
    *,
    version: str = AN_AUTHORITY_VERSION,
    state: str = OutcomeState.OK,
    observed_at: datetime = FIXED_INSTANT,
    channel: str = A_CHANNEL,
    platform: str = A_PLATFORM,
) -> CondaPackageSnapshot:
    """Record one published-package observation, on the terms `a_source_observation` states.

    Args:
        package: The package observed.
        version: The version the channel states as latest.
        state: What the lookup concluded.
        observed_at: The instant of this observation.
        channel: Which channel answered. Named because
            `conda_package_snapshots` holds one row per `(channel, platform)`,
            and the case about which of them a verdict is against needs two.
        platform: Which subdir the row is about, for the cases that need one
            channel observed on two.

    Returns:
        The saved row.

    """
    return CondaPackageSnapshot.objects.create(
        package=package,
        observed_at=observed_at,
        state=state,
        channel=channel,
        platform=platform,
        published_version=version if state == OutcomeState.OK else "",
    )


def a_policy_run(*, at: datetime = LATER_INSTANT) -> None:
    """Execute one policy run over the whole inventory.

    Args:
        at: The instant the run's clock answers, which becomes every rollup row's
            `computed_at`. Separate from the evidence cut-off, which is a
            property of the evidence rather than of when the run happens.

    """
    execute_policy_run(policy_version=A_POLICY_VERSION, clock=FixedClock(instant=at))


def a_policy_run_row() -> PolicyRun:
    """Record a policy run directly, for the cases that do not execute one.

    The constraint cases need a `policy_run` to key a hand-built row to, and the
    query-count case needs one to hand a pass without measuring the
    orchestration's own queries around it. Written directly rather than through
    `core/ledger.py`'s recorder for the reason `an_ended_collection_run` is: the
    recorder reads its own clock, and none of these cases is about when the run
    happened.

    Returns:
        The saved row.

    """
    return PolicyRun.objects.create(
        policy_version=A_POLICY_VERSION,
        evidence_cutoff=FIXED_INSTANT,
        started_at=FIXED_INSTANT,
        finished_at=LATER_INSTANT,
        status=RunState.SUCCEEDED,
    )


def the_finding(package: Package) -> PackageCurrency:
    """Return the one currency row the latest run wrote for a package.

    Args:
        package: The package whose finding is wanted.

    Returns:
        Its `PackageCurrency` row, newest run first. `get()` on the package alone
        would raise once a case has run twice, and the replay case needs both
        rows -- so this names the newest explicitly rather than relying on the
        table holding one.

    """
    return PackageCurrency.objects.filter(package=package).order_by("-policy_run_id")[0]


@pytest.mark.django_db
def test_a_package_agreeing_on_every_surface_is_current_everywhere() -> None:
    """The matrix's first row, end to end.

    All four surfaces state one version, the first entry of the default order is
    the authority, and every surface compared against it agrees. The row names
    the authority and references the observation it was read from, which is
    `CPM-FR-16`'s "the authority decision and the evidence supporting it are
    stored with the result".
    """
    an_ended_collection_run()
    package = a_package()
    source = a_source_observation(package)
    pypi = a_pypi_observation(package)
    feedstock = a_feedstock_observation(package)
    published = a_published_package_observation(package)

    a_policy_run()

    finding = the_finding(package)
    assert finding.source_status == CURRENT
    assert finding.pypi_status == CURRENT
    assert finding.feedstock_status == CURRENT
    assert finding.conda_package_status == CURRENT
    assert finding.overall_status == CURRENT
    assert finding.chosen_authority == VersionSurface.SOURCE.value
    assert finding.source_snapshot_id == source.pk
    assert finding.pypi_snapshot_id == pypi.pk
    assert finding.feedstock_snapshot_id == feedstock.pk
    assert finding.conda_package_snapshot_id == published.pk


@pytest.mark.django_db
def test_current_at_source_and_behind_on_the_feedstock_are_two_separate_facts() -> None:
    """AC 3, and the property the whole per-surface shape exists for.

    `CPM-FR-16` says "currency is computed per surface, so source-current and
    feedstock-stale is expressible". Expressible means readable off the row
    without inference: two columns holding two different verdicts about one
    package, and an overall verdict beside them that is neither of them read
    twice.
    """
    an_ended_collection_run()
    package = a_package()
    a_source_observation(package, version=AN_AUTHORITY_VERSION)
    a_pypi_observation(package, version=AN_AUTHORITY_VERSION)
    a_feedstock_observation(package, version=A_DIFFERENT_VERSION)
    a_published_package_observation(package, version=A_DIFFERENT_VERSION)

    a_policy_run()

    finding = the_finding(package)
    assert finding.source_status == CURRENT
    assert finding.feedstock_status == BEHIND
    assert finding.conda_package_status == BEHIND
    assert finding.overall_status == BEHIND
    assert finding.source_status != finding.feedstock_status


@pytest.mark.django_db
def test_a_package_with_no_recorded_authority_gets_the_default_and_the_row_says_so() -> None:
    """AC 2, end to end: the documented default applied, and recorded as the default.

    The order is stored on the row rather than left to be recovered:
    `Package.version_authority_order` is mutable, so re-reading it later would
    answer about the package as it is now rather than as this run found it, and
    `DEFAULT_AUTHORITY_ORDER` is a constant this product may change, so a row
    that merely said "the default" would silently come to mean a different order.
    """
    an_ended_collection_run()
    package = a_package()
    a_source_observation(package)

    a_policy_run()

    finding = the_finding(package)
    assert finding.authority_order_source == AuthorityOrderSource.DEFAULT
    assert finding.authority_order == list(DEFAULT_AUTHORITY_ORDER)
    assert finding.chosen_authority == VersionSurface.SOURCE.value


@pytest.mark.django_db
def test_a_package_carrying_an_authority_order_is_judged_by_it() -> None:
    """AC 1: the order recorded on the package decides, and the row names which it chose.

    The recorded order puts the feedstock first, and the feedstock and the source
    state different versions -- so a pass that had ignored the column would
    choose the source, call the feedstock `behind`, and produce exactly the
    opposite pair of verdicts. That is what makes this case about the column
    rather than about the comparison.
    """
    an_ended_collection_run()
    recorded = [VersionSurface.FEEDSTOCK.value, VersionSurface.SOURCE.value]
    package = a_package(authority_order=recorded)
    a_source_observation(package, version=A_DIFFERENT_VERSION)
    a_feedstock_observation(package, version=AN_AUTHORITY_VERSION)

    a_policy_run()

    finding = the_finding(package)
    assert finding.authority_order_source == AuthorityOrderSource.PACKAGE
    assert finding.authority_order == recorded
    assert finding.chosen_authority == VersionSurface.FEEDSTOCK.value
    assert finding.feedstock_status == CURRENT
    assert finding.source_status == BEHIND


@pytest.mark.django_db
def test_an_unobserved_first_authority_is_passed_over_and_the_row_records_which_was_chosen() -> None:
    """The matrix's "authority surface unobserved": never `ok` from an absent observation.

    The source has no observation at the cut-off at all, so it cannot be the
    authority however high the order ranks it -- and the row says both halves:
    the surface that *was* chosen, and the source's own verdict of `unknown`,
    which is what records that it was passed over rather than agreed with.
    """
    an_ended_collection_run()
    package = a_package()
    a_pypi_observation(package)

    a_policy_run()

    finding = the_finding(package)
    assert finding.chosen_authority == VersionSurface.PYPI.value
    assert finding.source_status == UNKNOWN
    assert finding.source_snapshot_id is None
    assert finding.pypi_status == CURRENT


@pytest.mark.django_db
def test_a_package_pypi_does_not_apply_to_is_never_judged_against_pypi() -> None:
    """`CPM-SM-C1`: never called stale against a registry it never published to.

    `CPM-FR-8` records a non-Python package as `not_applicable` to PyPI. That
    surface's verdict is `not_applicable`, it is not the chosen authority even
    though the default order ranks it above the feedstock, and it does not make
    the package `behind`.
    """
    an_ended_collection_run()
    package = a_package()
    a_pypi_observation(package, state=OutcomeState.NOT_APPLICABLE)
    a_feedstock_observation(package)

    a_policy_run()

    finding = the_finding(package)
    assert finding.pypi_status == NOT_APPLICABLE
    assert finding.chosen_authority == VersionSurface.FEEDSTOCK.value
    assert finding.overall_status != BEHIND


@pytest.mark.django_db
def test_an_errored_surface_is_an_error_rather_than_an_absence() -> None:
    """The matrix's "an error is not an absence", against a real row.

    A lookup that failed and a surface nobody looked at are two different facts,
    and a status column that could hold only one of them is the boolean
    `CPM-AD-5` bans. The overall verdict is not `current`, which is the half that
    matters operationally: a package one of whose surfaces could not be read is
    not a package this product is willing to call up to date.
    """
    an_ended_collection_run()
    package = a_package()
    a_source_observation(package)
    a_pypi_observation(package)
    a_feedstock_observation(package)
    a_published_package_observation(package, state=OutcomeState.ERROR)

    a_policy_run()

    finding = the_finding(package)
    assert finding.conda_package_status == ERROR
    assert finding.overall_status != CURRENT
    assert finding.overall_status == ERROR


@pytest.mark.django_db
def test_a_package_nothing_observed_reads_unknown_on_every_surface_and_gets_a_row() -> None:
    """The matrix's "a package nobody observed is not current", and it still gets a row.

    A missing row would be ambiguous between "not computed" and "nothing to say",
    and no read surface can tell those apart. So the row exists, every surface
    reads `unknown`, no authority is named, and the rollup column reads `unknown`
    too -- which is what a fresh inventory looks like before the first collection
    sweep.
    """
    an_ended_collection_run()
    package = a_package()

    a_policy_run()

    finding = the_finding(package)
    assert {
        finding.source_status,
        finding.pypi_status,
        finding.feedstock_status,
        finding.conda_package_status,
        finding.overall_status,
    } == {UNKNOWN}
    assert finding.chosen_authority == ""
    assert PackageHealth.objects.get(package=package).currency_status == UNKNOWN


@pytest.mark.django_db
def test_evidence_written_after_the_cutoff_is_not_read() -> None:
    """`CPM-AD-21`: a pass reads evidence as of the run's stated instant and no later.

    The two observations disagree, and only the earlier one is at or before the
    cut-off -- so the verdict is what the cut-off's evidence supports, and the
    referenced row is the earlier one. Without this the later observation would
    make the feedstock read `current`, which is the difference between a replay
    that reproduces and one that answers differently every time it runs.
    """
    an_ended_collection_run()
    package = a_package()
    a_source_observation(package, version=AN_AUTHORITY_VERSION)
    at_the_cutoff = a_feedstock_observation(package, version=A_DIFFERENT_VERSION)
    later = a_feedstock_observation(package, version=AN_AUTHORITY_VERSION, observed_at=AFTER_THE_CUTOFF)

    a_policy_run()

    finding = the_finding(package)
    assert finding.feedstock_snapshot_id == at_the_cutoff.pk
    assert finding.feedstock_snapshot_id != later.pk
    assert finding.feedstock_status == BEHIND


@pytest.mark.django_db
def test_the_newest_observation_at_the_cutoff_is_the_one_read() -> None:
    """The other half of the read: newer evidence at or before the cut-off wins.

    Together with the case above this pins the boundary from both sides. A read
    that took the *oldest* row would satisfy the exclusion case for the wrong
    reason -- it would also ignore the later row -- and would report a package as
    behind long after the recipe had caught up.
    """
    an_ended_collection_run(finished_at=AFTER_THE_CUTOFF)
    package = a_package()
    a_source_observation(package, version=AN_AUTHORITY_VERSION)
    a_feedstock_observation(package, version=A_DIFFERENT_VERSION)
    newest = a_feedstock_observation(package, version=AN_AUTHORITY_VERSION, observed_at=AFTER_THE_CUTOFF)

    a_policy_run()

    finding = the_finding(package)
    assert finding.feedstock_snapshot_id == newest.pk
    assert finding.feedstock_status == CURRENT


@pytest.mark.django_db
def test_replaying_a_version_at_a_cutoff_reproduces_the_row_and_leaves_the_first_alone() -> None:
    """`CPM-AD-8` and `CPM-FR-22`: same version, same cut-off, identical output.

    **The second run is a real replay, not a repetition.** A collection run
    finishes between the two, which moves the boundary `choose_evidence_cutoff`
    would pick -- so a second run that chose its own cut-off would read a
    different evidence set and this case would be asserting determinism, which is
    a weaker property than the one `CPM-FR-22` promises. The replay passes the
    first run's `evidence_cutoff` explicitly, which is the operation the
    parameter on `execute_policy_run` exists to make expressible: an operator
    replaying a recorded run has the instant on the `PolicyRun` row in front of
    them.

    New evidence lands after the original cut-off too, and it must not be read:
    that is what makes the replay a comparison of *rules* rather than of when the
    two runs happened to start.

    The second run's row carries the same verdicts, the same authority, the same
    order and the same evidence references as the first -- and the first's row is
    still there, keyed to its own run, which is what `(package, policy_run)` is
    for. A table keyed by the package alone would have overwritten it, and a
    replay would then have nothing to be compared against.

    Every column that is not the key is compared, rather than a chosen few: the
    guarantee is about the whole row, and a case naming three columns would pass
    while a fourth drifted.
    """
    an_ended_collection_run()
    package = a_package()
    a_source_observation(package)
    a_feedstock_observation(package, version=A_DIFFERENT_VERSION)

    first = execute_policy_run(policy_version=A_POLICY_VERSION, clock=FixedClock(instant=LATER_INSTANT))

    # The world moves on between the two runs, in both the ways that would break
    # a replay that chose its own cut-off: a later collection ends, and it writes
    # evidence that would change the feedstock's verdict.
    an_ended_collection_run(finished_at=AFTER_THE_CUTOFF)
    a_feedstock_observation(package, version=AN_AUTHORITY_VERSION, observed_at=AFTER_THE_CUTOFF)

    assert choose_evidence_cutoff() != first.evidence_cutoff, (
        "the cut-off must have moved between the two runs, or this case is a repetition rather than a replay"
    )

    execute_policy_run(
        policy_version=A_POLICY_VERSION,
        clock=FixedClock(instant=LATER_INSTANT + OBSERVATION_GAP),
        evidence_cutoff=first.evidence_cutoff,
    )

    rows = list(PackageCurrency.objects.filter(package=package).order_by("policy_run_id"))
    compared = [
        field.name
        for field in PackageCurrency._meta.concrete_fields  # noqa: SLF001 - `_meta` is Django's own public-by-convention API
        if field.name not in {"id", "policy_run"}
    ]

    assert len(rows) == REPLAYED_RUNS
    assert rows[0].policy_run_id != rows[1].policy_run_id
    for column in compared:
        assert getattr(rows[0], column) == getattr(rows[1], column), column


@pytest.mark.django_db
def test_the_rollup_column_arrives_through_the_orchestration_rather_than_from_the_pass() -> None:
    """`CPM-AD-21`: a pass returns its columns and never writes the rollup.

    The verdict on the rollup row and the verdict on the pass's own table agree,
    and the pass wrote only one of them -- `core/rollup.py` wrote the other, after
    `CPM-AD-4`'s gate, from the mapping the pass returned. The run's per-domain
    version map naming this pass is what says the contribution travelled that
    route rather than some other one.
    """
    an_ended_collection_run()
    package = a_package()
    a_source_observation(package)
    a_feedstock_observation(package, version=A_DIFFERENT_VERSION)

    a_policy_run()

    health = PackageHealth.objects.get(package=package)
    assert health.currency_status == BEHIND
    assert health.currency_status == the_finding(package).overall_status
    assert health.policy_versions[POLICY_NAME] == A_POLICY_VERSION
    assert health.evidence_cutoff == FIXED_INSTANT


@pytest.mark.django_db
def test_an_unmapped_packages_rollup_column_is_gated_while_its_finding_is_not() -> None:
    """`CPM-AD-4`: the gate is the rollup writer's, and it is not this pass's.

    The pass computes and returns as usual for a package at `unmapped`
    confidence -- its own derived row records the verdict it reached -- and the
    *rollup* column reads `unknown`, because the writer gated it on the way in.
    Both halves matter: a pass that applied the gate itself would lose the record
    of what it actually computed, and a writer that did not would let the rollup
    claim something about a package whose identity was never established
    (`CPM-FR-5`).
    """
    an_ended_collection_run()
    package = a_package(confidence=IdentityConfidence.UNMAPPED)
    a_source_observation(package)
    a_feedstock_observation(package, version=A_DIFFERENT_VERSION)

    a_policy_run()

    assert the_finding(package).overall_status == BEHIND
    assert PackageHealth.objects.get(package=package).currency_status == GATED_VALUE
    assert GATED_VALUE != BEHIND, "the gate must change the value, or this case asserts nothing"


@pytest.mark.django_db
def test_one_row_per_package_per_run_and_one_row_per_package_in_the_inventory() -> None:
    """`CPM-AD-21`'s key, and `CPM-AD-23`'s atomic unit, over more than one package.

    Two packages with different evidence, one run: two findings, each about its
    own package, neither carrying the other's verdict. A pass that had computed
    once and written the same row twice would satisfy every single-package case
    in this module.

    Both packages get all four surfaces, and the one that reads `current` needs
    them: an unobserved surface outranks the determinate value in `core`'s single
    order, so a package current on the two surfaces somebody looked at and
    unobserved on the other two reads `unknown` overall -- correctly, and not
    what this case is trying to tell apart.
    """
    an_ended_collection_run()
    current = a_package("numpy")
    behind = a_package("scipy")
    for package in (current, behind):
        a_source_observation(package, version=AN_AUTHORITY_VERSION)
        a_pypi_observation(package, version=AN_AUTHORITY_VERSION)
        a_published_package_observation(package, version=AN_AUTHORITY_VERSION)
    a_feedstock_observation(current, version=AN_AUTHORITY_VERSION)
    a_feedstock_observation(behind, version=A_DIFFERENT_VERSION)

    a_policy_run()

    assert PackageCurrency.objects.count() == PACKAGES_IN_THE_INVENTORY
    assert the_finding(current).overall_status == CURRENT
    assert the_finding(behind).overall_status == BEHIND


@pytest.mark.django_db
def test_a_channel_that_does_not_carry_the_package_does_not_mask_one_that_does() -> None:
    """A first-sorting channel's `not_found` no longer becomes the verdict over a later channel's `ok`.

    `conda_package_snapshots` holds one row per `(channel, platform)` and this
    pass produces one verdict. Before `CPM-OPERATE-S06` the pair was chosen on
    channel then platform alone, so a channel that answered `not_found` because
    it does not carry the package at all became the package's conda verdict even
    where a later-sorting channel published the authority's exact version -- a
    limitation this case used to pin. The selection now prefers a determinate
    row among one instant's pairs, and the row still references the observation
    so the channel the verdict came from is readable rather than inferred. What
    remains unbuilt is a verdict per pair, which is a larger table than
    `CPM-AD-21`'s `(package, policy_run)` key describes.
    """
    an_ended_collection_run()
    package = a_package()
    a_source_observation(package, version=AN_AUTHORITY_VERSION)
    a_published_package_observation(package, state=OutcomeState.NOT_FOUND, channel=A_CHANNEL)
    published = a_published_package_observation(package, version=AN_AUTHORITY_VERSION, channel=A_LATER_CHANNEL)

    a_policy_run()

    assert A_CHANNEL < A_LATER_CHANNEL, "the absent channel must sort first, or this case proves nothing"
    finding = the_finding(package)
    assert finding.conda_package_snapshot_id == published.pk
    assert finding.conda_package_snapshot.channel == A_LATER_CHANNEL
    assert finding.conda_package_status == CURRENT


@pytest.mark.django_db
def test_a_package_whose_authority_order_cannot_be_applied_fails_only_itself() -> None:
    """`CPM-AD-23`: one package rolls back and every other package commits.

    The broken package's authority order names a surface this product observes
    nothing for, so `applied_authority_order` refuses -- and the refusal is the
    right behaviour rather than a fallback, because quietly applying the default
    would write a row claiming an order the package's own data contradicts.

    What the orchestration then does is the part this case is about: the failed
    package gets no currency row from the failed run and **keeps the rollup row it
    already had**, the run finalizes `partial`, and the healthy package is
    computed and composed exactly as if nothing had gone wrong.

    A first, clean run is what makes the "keeps the row it had" half assertable:
    a package that never had one cannot be shown to have kept it, and a case
    asserting only that the row is absent would pass just as well against a
    writer that had deleted it. The order is then broken through a queryset
    `update()`, which is the only route to a bad value this product has -- nothing
    writes the column, so the hand-written `UPDATE` in the operator documentation
    is the shape being simulated.
    """
    an_ended_collection_run()
    healthy = a_package("numpy")
    broken = a_package("scipy")
    a_source_observation(healthy)
    a_source_observation(broken)
    a_feedstock_observation(broken, version=A_DIFFERENT_VERSION)

    clean = execute_policy_run(policy_version=A_POLICY_VERSION, clock=FixedClock(instant=LATER_INSTANT))
    before = PackageHealth.objects.get(package=broken)
    assert before.currency_status == BEHIND, "the first run must leave a verdict worth keeping"

    Package.objects.filter(pk=broken.pk).update(version_authority_order=["deployed"])
    second = execute_policy_run(
        policy_version=A_POLICY_VERSION,
        clock=FixedClock(instant=LATER_INSTANT + OBSERVATION_GAP),
    )

    after = PackageHealth.objects.get(package=broken)
    assert PackageCurrency.objects.filter(package=broken, policy_run=second.policy_run).count() == 0
    assert PackageCurrency.objects.filter(package=healthy, policy_run=second.policy_run).count() == 1
    assert after.policy_run_id == clean.policy_run.pk, "the broken package's row was rewritten by the failed run"
    assert after.currency_status == BEHIND
    assert PackageHealth.objects.get(package=healthy).currency_status == UNKNOWN
    assert PolicyRun.objects.get(pk=second.policy_run.pk).status == RunState.PARTIAL
    assert second.failed_packages == (broken.pk,)


@pytest.mark.django_db
def test_the_derived_table_refuses_a_second_row_for_one_package_and_run() -> None:
    """`CPM-AD-21`'s key as a database rule, proven by the database refusing.

    A unique constraint is only genuinely proven by the second write failing.
    The row is built by copying the one the run wrote, so it differs from it in
    nothing but the primary key -- which is exactly the duplicate a pass that ran
    twice would produce.
    """
    an_ended_collection_run()
    package = a_package()
    a_source_observation(package)

    a_policy_run()

    duplicate = the_finding(package)
    duplicate.pk = None
    duplicate._state.adding = True  # noqa: SLF001 - Django's own way to make a copied instance an insert

    # Contained in its own atomic block: an IntegrityError marks the surrounding
    # transaction broken, and `@pytest.mark.django_db` wraps the whole case in
    # one -- so a raise outside a savepoint would make the rollback at teardown
    # the error a reader sees instead of this one.
    with pytest.raises(IntegrityError), transaction.atomic():
        duplicate.save()


@pytest.mark.django_db
def test_a_tagged_source_and_a_bare_recipe_are_the_same_version() -> None:
    """The one deviation from the spec's stated comparison rule, exercised through the pass.

    `policies/currency.py`'s module docstring states the comparison and its one
    reconciliation once; this is the arrangement that made the reconciliation
    necessary, in the shape a real inventory produces it: the source collector
    stores a tag exactly as the source spelled it, and a conda recipe pins a bare
    version.

    Driven end to end rather than only through `comparable_version`, because the
    unit case proves the function and this proves the *rule reaches the verdict*:
    a pass that compared the stored strings would still pass every unit case
    about the helper.

    The stored spellings are asserted to differ, or the case would be comparing a
    version with itself.
    """
    an_ended_collection_run()
    package = a_package()
    tagged = a_source_observation(package, version=A_TAGGED_VERSION)
    bare = a_feedstock_observation(package, version=AN_AUTHORITY_VERSION)

    a_policy_run()

    finding = the_finding(package)
    assert tagged.latest_version != bare.recipe_version, "the two spellings must differ, or nothing is reconciled"
    assert finding.chosen_authority == VersionSurface.SOURCE.value
    assert finding.source_status == CURRENT
    assert finding.feedstock_status == CURRENT
    assert finding.detail == "", "nothing is behind, so there is nothing to explain"


@pytest.mark.django_db
def test_a_surface_ahead_of_the_authority_is_behind_through_the_pass_too() -> None:
    """Equality, not ordering, asserted where a smuggled-in ordering would first show.

    Every other `behind` in this module compares a lower version against the
    authority, so all of them would pass against an implementation that had
    quietly started comparing order. Here the feedstock states a version any
    ordering rule would call newer, and the verdict is still `behind`.

    The `detail` is asserted to name both, because this is exactly the row an
    operator would look at to decide whether a `behind` is real: the two compared
    forms differ, so it is.
    """
    an_ended_collection_run()
    package = a_package()
    a_source_observation(package, version=AN_AUTHORITY_VERSION)
    a_feedstock_observation(package, version=A_LATER_VERSION)

    a_policy_run()

    finding = the_finding(package)
    assert A_LATER_VERSION > AN_AUTHORITY_VERSION, "the fixture versions must order, or this case proves nothing"
    assert finding.feedstock_status == BEHIND
    assert A_LATER_VERSION in finding.detail
    assert AN_AUTHORITY_VERSION in finding.detail


@pytest.mark.django_db
def test_a_noarch_only_package_is_current_when_the_compiled_platform_has_no_file() -> None:
    """`CPM-OPERATE-S06`: among one sweep's pairs, a determinate row wins over `not_found`.

    Most pure-Python packages on conda-forge publish only `noarch`. With
    `("noarch", "linux-64")` declared, one sweep writes two rows for such a
    package: `noarch` `ok` at the upstream version and `linux-64` `not_found`.
    Both carry the run's instant, so the tie-break decides -- and on channel then
    platform alone `linux-64` sorts first, and the package would be judged
    `not_found` while its own `noarch` row said it was current. That is a false
    verdict any two-platform declaration would produce, so the selection prefers
    an `ok` row across the declared pairs before it falls back to the ordering.
    """
    an_ended_collection_run()
    package = a_package()
    a_source_observation(package, version=AN_AUTHORITY_VERSION)
    a_published_package_observation(package, state=OutcomeState.NOT_FOUND, platform=A_PLATFORM)
    published = a_published_package_observation(package, version=AN_AUTHORITY_VERSION, platform=A_NOARCH_PLATFORM)

    a_policy_run()

    assert A_PLATFORM < A_NOARCH_PLATFORM, "the compiled subdir must sort first, or this case proves nothing"
    finding = the_finding(package)
    assert finding.conda_package_snapshot_id == published.pk
    assert finding.conda_package_snapshot.platform == A_NOARCH_PLATFORM
    assert finding.conda_package_status == CURRENT


@pytest.mark.django_db
def test_a_compiled_package_is_current_when_noarch_has_no_file() -> None:
    """The reverse arrangement: `linux-64` `ok`, `noarch` `not_found`, the same answer.

    Here the ordering alone would already have chosen the right row, so this is
    the case that shows the preference is for the *determinate* row and not for
    `noarch`: a compiled package that publishes no `noarch` file is current on the
    platform it does publish.
    """
    an_ended_collection_run()
    package = a_package()
    a_source_observation(package, version=AN_AUTHORITY_VERSION)
    a_published_package_observation(package, state=OutcomeState.NOT_FOUND, platform=A_NOARCH_PLATFORM)
    published = a_published_package_observation(package, version=AN_AUTHORITY_VERSION, platform=A_PLATFORM)

    a_policy_run()

    finding = the_finding(package)
    assert finding.conda_package_snapshot_id == published.pk
    assert finding.conda_package_snapshot.platform == A_PLATFORM
    assert finding.conda_package_status == CURRENT


@pytest.mark.django_db
def test_the_published_package_verdict_is_not_found_only_when_every_pair_is() -> None:
    """`not_found` survives as the verdict when no declared pair carries the package.

    The preference for a determinate row must not manufacture one: with both
    platforms `not_found` there is nothing to prefer, the ordering decides which
    row the verdict references, and the verdict is `not_found`.
    """
    an_ended_collection_run()
    package = a_package()
    a_source_observation(package, version=AN_AUTHORITY_VERSION)
    a_published_package_observation(package, state=OutcomeState.NOT_FOUND, platform=A_NOARCH_PLATFORM)
    first = a_published_package_observation(package, state=OutcomeState.NOT_FOUND, platform=A_PLATFORM)

    a_policy_run()

    finding = the_finding(package)
    assert finding.conda_package_snapshot_id == first.pk
    assert finding.conda_package_status == NOT_FOUND


@pytest.mark.django_db
def test_a_newer_not_found_still_beats_an_older_ok_row() -> None:
    """The preference is among rows of one instant only; the newest observation still comes first.

    A package that was published and has since been withdrawn from a channel
    reads `not_found` today and `ok` yesterday, and today's row is the verdict --
    the cut-off reads the newest evidence, and an `ok` preference that reached
    across sweeps would keep a package current for ever on the strength of one
    old observation.
    """
    an_ended_collection_run(finished_at=LATER_INSTANT)
    package = a_package()
    a_source_observation(package, version=AN_AUTHORITY_VERSION)
    a_published_package_observation(package, version=AN_AUTHORITY_VERSION, observed_at=FIXED_INSTANT)
    withdrawn = a_published_package_observation(package, state=OutcomeState.NOT_FOUND, observed_at=LATER_INSTANT)

    a_policy_run()

    finding = the_finding(package)
    assert finding.conda_package_snapshot_id == withdrawn.pk
    assert finding.conda_package_status == NOT_FOUND


@pytest.mark.django_db
def test_the_published_package_verdict_does_not_depend_on_insertion_order() -> None:
    """The tie-break, against the arrangement one sweep actually produces.

    `conda_package_snapshots` holds one row per `(channel, platform)` and one
    sweep stamps every row with the run's single instant, so every row ties on
    `observed_at` and only a stated key decides which pair the package's conda
    verdict is about. Without one it is whichever row was inserted last, which
    changes when the collector's channel list is reordered -- silently, with the
    verdict flipping and nothing on the row saying why.

    Two packages with identical evidence written in opposite orders is what
    separates a stated key from an accident: a read ordered on the primary key
    alone gives them different verdicts, and this asserts they get the same one,
    from the channel the key names.
    """
    an_ended_collection_run()
    forwards = a_package("numpy")
    backwards = a_package("scipy")
    for package in (forwards, backwards):
        a_source_observation(package, version=AN_AUTHORITY_VERSION)
    a_published_package_observation(forwards, version=AN_AUTHORITY_VERSION, channel=A_CHANNEL)
    a_published_package_observation(forwards, version=A_DIFFERENT_VERSION, channel=A_LATER_CHANNEL)
    a_published_package_observation(backwards, version=A_DIFFERENT_VERSION, channel=A_LATER_CHANNEL)
    a_published_package_observation(backwards, version=AN_AUTHORITY_VERSION, channel=A_CHANNEL)

    a_policy_run()

    assert A_CHANNEL < A_LATER_CHANNEL, "the fixture channels must order, or the stated key decides nothing"
    for package in (forwards, backwards):
        finding = the_finding(package)
        assert finding.conda_package_snapshot.channel == A_CHANNEL, package.canonical_name
        assert finding.conda_package_status == CURRENT, package.canonical_name


@pytest.mark.django_db
@pytest.mark.parametrize("column", sorted(SURFACE_STATUS_FIELDS.values()), ids=str)
def test_a_determinate_verdict_without_an_authority_is_refused_by_the_database(column: str) -> None:
    """`currency_verdict_names_its_authority`, refused by the database rather than named.

    `current` and `behind` are verdicts *against an authority*: they are reached
    by comparing a surface's version with the authority's, so a row carrying
    either while naming no authority is a comparison against nothing. The pass
    cannot build such a row, which is exactly why the constraint exists -- the
    model's own docstring says it is for the hand-written `UPDATE` the pass cannot
    make -- and it is also why a case asserting the constraint by name would pass
    against a weakened one.

    Parametrised over all four surface columns because each is a separate conjunct
    of the condition: a constraint that had lost one would still refuse the other
    three, and a single-column case would not notice.
    """
    package = a_package()
    row = dict.fromkeys(SURFACE_STATUS_FIELDS.values(), UNKNOWN)
    row[column] = CURRENT

    with pytest.raises(IntegrityError, match=DETERMINATE_VERDICT_NEEDS_AN_AUTHORITY), transaction.atomic():
        PackageCurrency.objects.create(
            package=package,
            policy_run=a_policy_run_row(),
            overall_status=CURRENT,
            chosen_authority="",
            authority_order=list(DEFAULT_AUTHORITY_ORDER),
            authority_order_source=AuthorityOrderSource.DEFAULT.value,
            **row,
        )


@pytest.mark.django_db
def test_a_row_naming_an_authority_is_permitted_by_that_constraint() -> None:
    """The other side, so the constraint is not simply one that refuses every row.

    The same row, differing only in the column under test. A check that had been
    written as "refuse everything" would satisfy all four cases above and would
    make the table unwritable, which the pass's own cases would then report as a
    failure somewhere else entirely.
    """
    package = a_package()
    row = dict.fromkeys(SURFACE_STATUS_FIELDS.values(), UNKNOWN)
    row[SURFACE_STATUS_FIELDS[VersionSurface.SOURCE.value]] = CURRENT

    written = PackageCurrency.objects.create(
        package=package,
        policy_run=a_policy_run_row(),
        overall_status=CURRENT,
        chosen_authority=VersionSurface.SOURCE.value,
        authority_order=list(DEFAULT_AUTHORITY_ORDER),
        authority_order_source=AuthorityOrderSource.DEFAULT.value,
        **row,
    )

    assert written.pk is not None


@pytest.mark.django_db
def test_an_authority_outside_the_surface_vocabulary_is_refused_by_the_database() -> None:
    """`currency_authority_is_a_known_surface`, refused by the database rather than named.

    `choices` is a form and `full_clean()` rule and Django enforces neither on
    `save()`, so without this constraint a misspelled surface reaches the column
    and every later read is about an authority that does not exist -- including
    the reverse lookup from `chosen_authority` to the reference column holding its
    evidence, which would simply find nothing.

    Built directly rather than through the pass, for the reason the constraint
    exists: the pass writes only `VersionSurface` values, so no path through it
    can produce the row this refuses.
    """
    package = a_package()

    with pytest.raises(IntegrityError, match=AUTHORITY_IS_A_KNOWN_SURFACE), transaction.atomic():
        PackageCurrency.objects.create(
            package=package,
            policy_run=a_policy_run_row(),
            **dict.fromkeys(SURFACE_STATUS_FIELDS.values(), UNKNOWN),
            overall_status=UNKNOWN,
            chosen_authority=AN_INVENTED_SURFACE,
            authority_order=list(DEFAULT_AUTHORITY_ORDER),
            authority_order_source=AuthorityOrderSource.DEFAULT.value,
        )


@pytest.mark.django_db
@pytest.mark.parametrize("packages", [1, 2, 4], ids=str)
def test_the_query_count_per_package_does_not_grow_with_the_inventory(
    django_assert_num_queries: Any,
    packages: int,
) -> None:
    """Four evidence queries per package, and the cost is pinned even though it is not fixed.

    This pass reads its four surfaces separately inside the orchestration's
    per-package loop, so a run over `CPM-NFR-1`'s ten thousand packages is forty
    thousand round trips for this pass alone -- multiplying as the remaining seven
    passes land. `collectors/selection.py` already established the set-based read
    for exactly this shape, and the optimisation belongs with the story that first
    runs a policy pass at inventory scale; it is recorded as deferred.

    What this asserts is the thing a regression would break silently: the count
    is *linear* in the inventory and the per-package constant is five -- four
    reads and the one insert of the derived row. A read that started issuing one
    query per surface *per channel*, or a write that re-read the row it had just
    made, would change the constant and show up here at three cardinalities
    rather than at none.

    Only the pass phase is measured. `execute_policy_run` also opens a ledger row,
    reads the package set and composes the rollup, and those are the
    orchestration's queries rather than this pass's -- counting them would make
    this case fail whenever `core` changed something it does not own.
    """
    an_ended_collection_run()
    inventory = [a_package(f"package-{index}") for index in range(packages)]
    for package in inventory:
        a_source_observation(package)
    cutoff = choose_evidence_cutoff()
    run = a_policy_run_row()

    with django_assert_num_queries(QUERIES_PER_PACKAGE * packages):
        for package in inventory:
            CurrencyPass().evaluate(package, policy_run=run, evidence_cutoff=cutoff)
