"""`CPM-OPERATE-S07`: ninety days of evidence, purged nightly, and a replay that still reproduces.

The story has one correctness invariant at its centre and everything else is
plumbing around it: **no purge may remove a row a retained policy run read at its
cut-off**, because `CPM-FR-22`'s replay reads the newest row at-or-before that
cut-off and would otherwise pick a different one. The first block of cases is that
rule, asked of `removable_evidence` directly, before any row is deleted -- so a
purge that removed the wrong row would fail here on the selection, not later on a
replay whose difference report has to be read backwards.

The seeded history is relative to the system clock rather than to a fixed instant,
because the replay command reads `SystemClock()` and the window is measured from
now: a history pinned to a date would walk out of the window ninety days after it
was written and turn every replay case red for no reason anybody changed.

Every case rolls back: `@pytest.mark.django_db` wraps each in a transaction, and
the purge's per-batch `transaction.atomic()` nests as a savepoint inside it.
"""

from __future__ import annotations

from datetime import timedelta
from io import StringIO
from typing import TYPE_CHECKING
from typing import Final

import pytest
import structlog.testing
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import DatabaseError
from django.db import IntegrityError
from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext

from conda_sentinel.collectors.match_confidence import MatchConfidence
from conda_sentinel.collectors.models import KevFinding
from conda_sentinel.collectors.models import LicenseFinding
from conda_sentinel.collectors.models import PyPIReleaseSnapshot
from conda_sentinel.collectors.models import PythonReadinessAssessment
from conda_sentinel.collectors.models import SourceReleaseSnapshot
from conda_sentinel.collectors.models import VulnerabilityFinding
from conda_sentinel.collectors.outcomes import KEV_UNKNOWN
from conda_sentinel.collectors.outcomes import LICENSE_UNKNOWN
from conda_sentinel.collectors.outcomes import LISTED
from conda_sentinel.collectors.outcomes import MATCHED
from conda_sentinel.collectors.outcomes import READINESS_UNKNOWN
from conda_sentinel.collectors.outcomes import VERIFICATION_UNKNOWN
from conda_sentinel.core import retention
from conda_sentinel.core.clock import FixedClock
from conda_sentinel.core.clock import SystemClock
from conda_sentinel.core.ledger import abandon
from conda_sentinel.core.models import AppendOnlyModel
from conda_sentinel.core.models import CollectionRun
from conda_sentinel.core.models import PackageHealth
from conda_sentinel.core.models import PolicyRun
from conda_sentinel.core.outcomes import OutcomeState
from conda_sentinel.core.policy_run import choose_evidence_cutoff
from conda_sentinel.core.policy_run import execute_policy_run
from conda_sentinel.core.replay import compare_runs
from conda_sentinel.core.retention import CUTOFF_MOVED_EVENT
from conda_sentinel.core.retention import EVIDENCE_ROSTER
from conda_sentinel.core.retention import PRUNE_COLLECTOR
from conda_sentinel.core.retention import RETENTION_SETTING
from conda_sentinel.core.retention import SUPERSEDED_DETAIL
from conda_sentinel.core.retention import EvidenceTable
from conda_sentinel.core.retention import floor_removable_evidence
from conda_sentinel.core.retention import purge_evidence
from conda_sentinel.core.retention import removable_evidence
from conda_sentinel.core.retention import retention_cutoff
from conda_sentinel.core.runs import RunLedgerError
from conda_sentinel.core.runs import RunState
from conda_sentinel.identity.confidence import IdentityConfidence
from conda_sentinel.identity.models import Package
from conda_sentinel.policies.models import PackageCurrency
from tests.passes import A_RECORDED_POLICY_VERSION

if TYPE_CHECKING:
    from datetime import datetime

    from django.db import models

#: The instant every case measures from. Read once, at import, from the same clock
#: the replay command reads, so "day -120" here and "now minus the retention" there
#: fall on the same side of the window.
NOW: Final[datetime] = SystemClock().now()

#: The retention the cases purge at, and the cut-off it implies.
RETENTION: Final[int] = 90
CUTOFF: Final[datetime] = retention_cutoff(now=NOW, days=RETENTION)

#: The collector name the fixture collection runs are recorded under.
A_COLLECTOR: Final[str] = "inventory"

#: One advisory, so the advisory-keyed tables have a key to share.
AN_ADVISORY: Final[str] = "GHSA-xxxx-yyyy-zzzz"

#: The roster entries the floor cases read, by label.
PYPI: Final[EvidenceTable] = next(entry for entry in EVIDENCE_ROSTER if entry.label == "collectors.PyPIReleaseSnapshot")
LICENSE: Final[EvidenceTable] = next(entry for entry in EVIDENCE_ROSTER if entry.label == "collectors.LicenseFinding")
KEV: Final[EvidenceTable] = next(entry for entry in EVIDENCE_ROSTER if entry.label == "collectors.KevFinding")
VULNERABILITY: Final[EvidenceTable] = next(
    entry for entry in EVIDENCE_ROSTER if entry.label == "collectors.VulnerabilityFinding"
)
READINESS: Final[EvidenceTable] = next(
    entry for entry in EVIDENCE_ROSTER if entry.label == "collectors.PythonReadinessAssessment"
)

#: `--batch 2` over five rows is three batches.
FIVE_ROWS: Final[int] = 5
THREE_BATCHES: Final[int] = 3

#: The demo seeder's synthetic collector name, which must keep counting as a
#: collection for the cut-off choice: a fresh seed relies on it.
THE_SEED_COLLECTOR: Final[str] = "local-dev-demo-seed"

#: An indeterminate row for every purged table: the state that lets every facts
#: constraint pass with the facts blank, plus whatever the key needs. Enough to
#: drive the floor rule over every key shape without a collector.
INDETERMINATE_ROW: Final[dict[str, dict[str, object]]] = {
    "collectors.InventorySnapshot": {"state": OutcomeState.UNKNOWN.value, "source_package_key": "pypi:numpy"},
    "collectors.SourceReleaseSnapshot": {"state": OutcomeState.UNKNOWN.value},
    "collectors.PyPIReleaseSnapshot": {"state": OutcomeState.UNKNOWN.value},
    "collectors.FeedstockSnapshot": {"state": OutcomeState.UNKNOWN.value},
    "collectors.CondaPackageSnapshot": {
        "state": OutcomeState.UNKNOWN.value,
        "channel": "conda-forge",
        "platform": "noarch",
    },
    "collectors.KevFinding": {"state": KEV_UNKNOWN},
    "collectors.VulnerabilityFinding": {"state": MATCHED, "advisory_id": AN_ADVISORY, "affected_range": "<2"},
    "collectors.LicenseFinding": {"state": LICENSE_UNKNOWN, "channel": "conda-forge"},
    "collectors.PythonReadinessAssessment": {"state": READINESS_UNKNOWN, "python_series": "3.14"},
    "collectors.PythonVerificationResult": {"state": VERIFICATION_UNKNOWN, "python_series": "3.14"},
    "collectors.IdentityResolutionSnapshot": {"state": OutcomeState.UNKNOWN.value},
}

#: A second value for each table's non-package key column, so the per-key
#: cases can seed a neighbour under another key. Tables keyed by package alone
#: have none.
ANOTHER_KEY: Final[dict[str, dict[str, object]]] = {
    "collectors.InventorySnapshot": {"source_package_key": "pypi:scipy"},
    "collectors.CondaPackageSnapshot": {"platform": "linux-64"},
    "collectors.VulnerabilityFinding": {"advisory_id": "GHSA-aaaa-bbbb-cccc"},
    "collectors.LicenseFinding": {"channel": "bioconda"},
    "collectors.PythonReadinessAssessment": {"python_series": "3.13"},
    "collectors.PythonVerificationResult": {"python_series": "3.13"},
}


def a_row(entry: EvidenceTable, package: Package, *, at: datetime, **columns: object) -> AppendOnlyModel:
    """Insert one indeterminate row into a purged table.

    Args:
        entry: The roster entry.
        package: The package the row is about.
        at: When it was observed.
        **columns: Overrides, for a row under another key.

    Returns:
        The saved row.

    """
    values: dict[str, object] = {**INDETERMINATE_ROW[entry.label], **columns}
    if entry.label == "collectors.VulnerabilityFinding":
        values.setdefault("matched_version", "1.0")
        values.setdefault("match_confidence", MatchConfidence.EXACT_RANGE.value)
    return entry.model.objects.create(observed_at=at, package=package, **values)


#: How many run records one purge writes: the eight derived tables, `policy_runs`,
#: the eleven evidence tables, `collection_runs`.
DERIVED_TABLES: Final[int] = 8
LEDGERS: Final[int] = 2
RECORDS_PER_PURGE: Final[int] = DERIVED_TABLES + LEDGERS + len(EVIDENCE_ROSTER)


def days_ago(count: int) -> datetime:
    """Return the instant `count` days before `NOW`.

    Args:
        count: How many days back.

    Returns:
        The instant.

    """
    return NOW - timedelta(days=count)


def a_package(name: str = "numpy") -> Package:
    """Create one package with a resolved identity.

    Args:
        name: Its canonical name.

    Returns:
        The saved row.

    """
    return Package.objects.create(
        canonical_name=name, resolved_at=days_ago(200), confidence=IdentityConfidence.VERIFIED
    )


def a_pypi_release(package: Package, *, version: str, at: datetime) -> PyPIReleaseSnapshot:
    """Record what PyPI stated about a package at one instant.

    Args:
        package: The package observed.
        version: The version PyPI states.
        at: When the observation was made.

    Returns:
        The saved row.

    """
    return PyPIReleaseSnapshot.objects.create(
        observed_at=at,
        package=package,
        source="https://pypi.org/pypi/numpy/json",
        state=OutcomeState.OK.value,
        latest_version=version,
        released_at=at,
    )


def a_release(package: Package, *, version: str, at: datetime) -> None:
    """Record both release surfaces, so the currency pass reaches a determinate verdict.

    Args:
        package: The package observed.
        version: The version the source states; PyPI is left one behind.
        at: When the observation was made.

    """
    SourceReleaseSnapshot.objects.create(
        observed_at=at,
        package=package,
        source="https://example.invalid/releases",
        state=OutcomeState.OK.value,
        latest_version=version,
        released_at=at,
    )
    a_pypi_release(package, version="0.0.1", at=at)


def an_ended_collection_run(*, finished_at: datetime, started_at: datetime | None = None) -> CollectionRun:
    """Record a collection run that has ended.

    Args:
        finished_at: When it ended.
        started_at: When it began; just before it ended by default.

    Returns:
        The saved row.

    """
    return CollectionRun.objects.create(
        collector=A_COLLECTOR,
        started_at=started_at or finished_at - timedelta(minutes=1),
        finished_at=finished_at,
        status=RunState.SUCCEEDED,
    )


def a_run(*, at: datetime, cutoff: datetime) -> PolicyRun:
    """Execute one policy run at a stated cut-off, finishing at a stated instant.

    The newest run always holds every `package_health` row (`CPM-AD-11`), so it is
    pinned whatever its age; an earlier run is pinned only until a later one
    replaces the rollup.

    Args:
        at: When the run's clock says it ran, which is its `finished_at`.
        cutoff: The instant it reads evidence as of.

    Returns:
        The run's ledger row.

    """
    return execute_policy_run(
        policy_version=A_RECORDED_POLICY_VERSION,
        clock=FixedClock(instant=at),
        evidence_cutoff=cutoff,
    ).policy_run


def purge(*, dry_run: bool = False, batch_size: int = 1000, retention: int = RETENTION) -> dict[str, object]:
    """Run the purge as of `NOW` and return each table's result by name.

    Args:
        dry_run: Whether to count rather than remove.
        batch_size: The bound on one `DELETE`.
        retention: The retention, in days.

    Returns:
        The per-table results, keyed by table name.

    """
    results = purge_evidence(
        clock=FixedClock(instant=NOW),
        retention_days=retention,
        batch_size=batch_size,
        dry_run=dry_run,
    )
    return {result.table: result for result in results}


def removable(entry: EvidenceTable) -> set[int]:
    """Return the primary keys the purge selects from one table, as of `NOW`.

    Args:
        entry: The roster entry.

    Returns:
        The keys: the floor rule, less what a surviving row cites.

    """
    return set(removable_evidence(entry, cutoff=CUTOFF).values_list("pk", flat=True))


def floor_removable(entry: EvidenceTable) -> set[int]:
    """Return the primary keys the floor rule alone lets go from one table, as of `NOW`.

    Args:
        entry: The roster entry.

    Returns:
        The keys, before the citation exclusion.

    """
    return set(floor_removable_evidence(entry, cutoff=CUTOFF).values_list("pk", flat=True))


def a_purge_record(*, finished_at: datetime | None, started_at: datetime, detail: str = "") -> CollectionRun:
    """Record a `prune_evidence` row by hand, as an earlier purge would have left it.

    Args:
        finished_at: Its ending, or `None` for one a killed purge left running.
        started_at: When it began.
        detail: What it recorded.

    Returns:
        The saved row.

    """
    return CollectionRun.objects.create(
        collector=PRUNE_COLLECTOR,
        started_at=started_at,
        finished_at=finished_at,
        status=RunState.RUNNING if finished_at is None else RunState.SUCCEEDED,
        detail=detail,
    )


def rows(model: type[models.Model]) -> set[int]:
    """Return every primary key a table holds.

    Args:
        model: The table.

    Returns:
        The keys.

    """
    return set(model._default_manager.values_list("pk", flat=True))  # noqa: SLF001 - Django's own accessor


def purge_records() -> list[CollectionRun]:
    """Return the run records the purge wrote, oldest first.

    Returns:
        Every `collection_runs` row under the purge's collector name.

    """
    return list(CollectionRun.objects.filter(collector=PRUNE_COLLECTOR).order_by("pk"))


# ---------------------------------------------------------------------------
# The floor rule, before any row goes.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_a_row_older_than_the_cutoff_with_a_newer_row_under_its_key_is_removable() -> None:
    """Matrix row `Old rows`: day -120 goes when day -30 has superseded it; day -30 stays."""
    package = a_package()
    old = a_pypi_release(package, version="1.0", at=days_ago(120))
    a_pypi_release(package, version="2.0", at=days_ago(30))

    assert removable(PYPI) == {old.pk}


@pytest.mark.django_db
def test_the_newest_row_per_key_is_never_removable_however_old() -> None:
    """Matrix row `Newest kept`: a package whose only rows are at day -120 keeps them."""
    package = a_package()
    a_pypi_release(package, version="1.0", at=days_ago(120))

    assert removable(PYPI) == set()


@pytest.mark.django_db
def test_the_row_a_surviving_run_reads_at_its_cutoff_is_kept_even_when_older_than_the_retention() -> None:
    """Matrix row `Replay floor`, as the selection.

    Run R's cut-off is day -30; the package's newest row at or before that is day
    -120, and a newer row exists at day -10. A replay of R reads the day -120 row,
    so it is kept. Were a row to land between them -- day -40, written behind R's
    cut-off, which real collection never does -- the floor rule alone would let
    the day -120 row go, and R's derived rows, which cite it, still keep it.
    """
    an_ended_collection_run(finished_at=days_ago(30))
    package = a_package()
    old = a_pypi_release(package, version="1.0", at=days_ago(120))
    a_pypi_release(package, version="3.0", at=days_ago(10))
    a_run(at=days_ago(30), cutoff=days_ago(30))

    assert floor_removable(PYPI) == set()
    assert removable(PYPI) == set()

    between = a_pypi_release(package, version="2.0", at=days_ago(40))

    assert floor_removable(PYPI) == {old.pk}
    assert removable(PYPI) == set()
    assert between.pk not in floor_removable(PYPI)


@pytest.mark.django_db
def test_every_surviving_run_protects_the_row_it_reads_not_only_the_oldest_cutoff() -> None:
    """Two surviving runs at two cut-offs, and each keeps its own row.

    Run B (cut-off day -95, finished inside the window) reads the day -97 row; run
    A (cut-off day -100, pinned by the rollup because it ran last) reads the day
    -110 row. A rule that protected only the newest row at or before the *oldest*
    surviving cut-off would keep day -110 and remove day -97, and B's replay would
    then read day -110 instead. Only the day -120 row, which no surviving run
    reads, is removable.
    """
    an_ended_collection_run(finished_at=days_ago(95))
    package = a_package()
    unread = a_pypi_release(package, version="1.0", at=days_ago(120))
    read_by_a = a_pypi_release(package, version="2.0", at=days_ago(110))
    read_by_b = a_pypi_release(package, version="3.0", at=days_ago(97))
    a_pypi_release(package, version="4.0", at=days_ago(5))
    a_run(at=days_ago(89), cutoff=days_ago(95))
    a_run(at=days_ago(100), cutoff=days_ago(100))

    assert removable(PYPI) == {unread.pk}
    assert {read_by_a.pk, read_by_b.pk} & removable(PYPI) == set()


@pytest.mark.django_db
def test_rows_sharing_the_newest_instant_are_kept_together_as_one_sweep() -> None:
    """Readers take every row at the newest instant, so a tie is one sweep, never one row.

    Two advisory findings observed at the same instant are what one sweep writes.
    Superseded at day -30 and read by no run, both are removable; once a run at
    day -60 has read them, neither may go.
    """
    an_ended_collection_run(finished_at=days_ago(60))
    package = a_package()
    sweep = days_ago(120)
    first = VulnerabilityFinding.objects.create(
        observed_at=sweep,
        package=package,
        state=MATCHED,
        advisory_id=AN_ADVISORY,
        affected_range="<2",
        matched_version="1.0",
        match_confidence=MatchConfidence.EXACT_RANGE.value,
    )
    second = VulnerabilityFinding.objects.create(
        observed_at=sweep,
        package=package,
        state=MATCHED,
        advisory_id=AN_ADVISORY,
        affected_range="<2",
        matched_version="1.0",
        match_confidence=MatchConfidence.EXACT_RANGE.value,
    )
    VulnerabilityFinding.objects.create(
        observed_at=days_ago(30),
        package=package,
        state=MATCHED,
        advisory_id=AN_ADVISORY,
        affected_range="<2",
        matched_version="1.0",
        match_confidence=MatchConfidence.EXACT_RANGE.value,
    )
    assert removable(VULNERABILITY) == {first.pk, second.pk}

    a_run(at=days_ago(60), cutoff=days_ago(60))

    assert removable(VULNERABILITY) == set()


@pytest.mark.django_db
def test_license_findings_are_keyed_per_channel() -> None:
    """Matrix row `Newest kept`, under a reader key wider than the package.

    Channel A has a newer row, so its day -120 row may go; channel B's only row is
    its newest and stays, however old.
    """
    package = a_package()
    superseded = LicenseFinding.objects.create(
        observed_at=days_ago(120),
        package=package,
        channel="conda-forge",
        state=LICENSE_UNKNOWN,
        raw_license="MIT",
    )
    LicenseFinding.objects.create(
        observed_at=days_ago(30),
        package=package,
        channel="conda-forge",
        state=LICENSE_UNKNOWN,
        raw_license="MIT",
    )
    LicenseFinding.objects.create(
        observed_at=days_ago(120),
        package=package,
        channel="bioconda",
        state=LICENSE_UNKNOWN,
        raw_license="MIT",
    )

    assert removable(LICENSE) == {superseded.pk}


@pytest.mark.django_db
def test_kev_findings_that_link_to_no_advisory_share_one_key() -> None:
    """The KEV key is the linked finding's advisory, coalesced: no link is one key, not one per row.

    Most packages have nothing to cross-reference and get an `unknown` KEV row per
    sweep with no link. Keyed by the link's advisory those rows would each be
    their own newest and the table would never shrink; coalesced, the older one
    is superseded by the newer.
    """
    package = a_package()
    superseded = KevFinding.objects.create(observed_at=days_ago(120), package=package, state=KEV_UNKNOWN)
    KevFinding.objects.create(observed_at=days_ago(30), package=package, state=KEV_UNKNOWN)

    assert removable(KEV) == {superseded.pk}


@pytest.mark.django_db
def test_python_readiness_is_keyed_per_series() -> None:
    """The readiness reader filters by series before taking the newest, so the key carries the series."""
    package = a_package()
    PythonReadinessAssessment.objects.create(
        observed_at=days_ago(120),
        package=package,
        state=READINESS_UNKNOWN,
        python_series="3.14",
    )
    PythonReadinessAssessment.objects.create(
        observed_at=days_ago(30),
        package=package,
        state=READINESS_UNKNOWN,
        python_series="3.13",
    )

    assert removable(READINESS) == set()


# ---------------------------------------------------------------------------
# The purge.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_rows_older_than_the_cutoff_go_and_every_table_gets_a_run_record() -> None:
    """Matrix row `Old rows`, end to end, and the record each table's purge leaves."""
    package = a_package()
    old = a_pypi_release(package, version="1.0", at=days_ago(120))
    kept = a_pypi_release(package, version="2.0", at=days_ago(30))

    results = purge()

    assert rows(PyPIReleaseSnapshot) == {kept.pk}
    assert old.pk not in rows(PyPIReleaseSnapshot)
    pypi = results["pypi_release_snapshots"]
    assert pypi.deleted == 1
    assert pypi.protected == 0
    assert pypi.dry_run is False
    records = purge_records()
    assert {record.detail.split(" ")[0] for record in records} == {f"table={result}" for result in results}
    record = CollectionRun.objects.get(pk=pypi.run_id)
    assert record.collector == PRUNE_COLLECTOR
    assert record.status == RunState.SUCCEEDED
    assert f"cutoff={CUTOFF.isoformat()}" in record.detail
    assert "deleted=1" in record.detail
    assert "protected=0" in record.detail
    assert "dry_run=false" in record.detail


@pytest.mark.django_db
def test_a_packages_only_rows_are_kept_and_counted_as_kept_by_rule() -> None:
    """Matrix row `Newest kept`: kept, and the record says the rule kept it."""
    package = a_package()
    only = a_pypi_release(package, version="1.0", at=days_ago(120))

    results = purge()

    assert rows(PyPIReleaseSnapshot) == {only.pk}
    assert results["pypi_release_snapshots"].deleted == 0
    assert results["pypi_release_snapshots"].kept_by_rule == 1
    assert "kept_by_rule=1" in CollectionRun.objects.get(pk=results["pypi_release_snapshots"].run_id).detail


@pytest.mark.django_db
def test_the_row_a_retained_run_read_survives_and_the_replay_reproduces() -> None:
    """Matrix row `Replay floor`, end to end: purge, then replay byte-identical.

    Package A's day -120 rows are what run R read at its day -30 cut-off, so they
    stay. Package B's day -120 rows were superseded at day -100 -- before R's
    cut-off -- so they go, and the purge is shown to have removed something.
    """
    an_ended_collection_run(finished_at=days_ago(30))
    read_at_cutoff = a_package("alpha")
    a_release(read_at_cutoff, version="2.0.0", at=days_ago(120))
    a_release(read_at_cutoff, version="9.9.9", at=days_ago(10))
    superseded = a_package("beta")
    a_release(superseded, version="1.0.0", at=days_ago(120))
    a_release(superseded, version="1.5.0", at=days_ago(100))
    before = rows(PyPIReleaseSnapshot) | rows(SourceReleaseSnapshot)
    original = a_run(at=days_ago(30), cutoff=days_ago(30))

    results = purge()

    after = rows(PyPIReleaseSnapshot) | rows(SourceReleaseSnapshot)
    assert after < before, "the purge removed nothing, so the replay proves nothing"
    assert results["pypi_release_snapshots"].deleted == 1
    assert results["source_release_snapshots"].deleted == 1
    assert PyPIReleaseSnapshot.objects.filter(package=read_at_cutoff, observed_at=days_ago(120)).exists()

    replayed = a_run(at=days_ago(1), cutoff=original.evidence_cutoff)
    report = compare_runs(original, replayed)
    assert report.reproduced, [difference.describe() for difference in report.differences]
    assert report.compared_rows > 0


@pytest.mark.django_db
def test_derived_rows_and_their_run_go_before_the_evidence_they_cite() -> None:
    """Matrix row `Outside window`: the run, its derived rows and then the evidence it cited.

    Run A (cut-off and finish at day -100) cited the day -120 rows. A later run
    took over the rollup, so A is unpinned and outside the window. Were the
    evidence purged first, A's citations would protect the day -120 rows and A
    would keep them for ever; in the right order all three go.
    """
    an_ended_collection_run(finished_at=days_ago(100))
    package = a_package()
    a_release(package, version="2.0.0", at=days_ago(120))
    out_of_window = a_run(at=days_ago(100), cutoff=days_ago(100))
    assert PackageCurrency.objects.filter(policy_run=out_of_window, source_snapshot__isnull=False).exists()
    a_release(package, version="3.0.0", at=days_ago(10))
    inside = a_run(at=days_ago(1), cutoff=days_ago(1))

    results = purge()

    assert not PolicyRun.objects.filter(pk=out_of_window.pk).exists()
    assert not PackageCurrency.objects.filter(policy_run_id=out_of_window.pk).exists()
    assert PolicyRun.objects.filter(pk=inside.pk).exists()
    assert not PyPIReleaseSnapshot.objects.filter(observed_at=days_ago(120)).exists()
    assert not SourceReleaseSnapshot.objects.filter(observed_at=days_ago(120)).exists()
    assert results["policy_runs"].deleted == 1
    assert results["package_currency"].deleted >= 1
    assert results["pypi_release_snapshots"].protected == 0


@pytest.mark.django_db
def test_a_run_pinned_by_package_health_survives_with_its_derived_rows_and_what_it_read() -> None:
    """Matrix row `Pinned run`: the rollup cites it, so it, its rows and its evidence stay."""
    an_ended_collection_run(finished_at=days_ago(100))
    package = a_package()
    a_release(package, version="2.0.0", at=days_ago(120))
    a_release(package, version="3.0.0", at=days_ago(10))
    pinned = a_run(at=days_ago(100), cutoff=days_ago(100))
    assert PackageHealth.objects.filter(policy_run=pinned).exists()

    results = purge()

    assert PolicyRun.objects.filter(pk=pinned.pk).exists()
    assert PackageCurrency.objects.filter(policy_run=pinned).exists()
    assert PyPIReleaseSnapshot.objects.filter(observed_at=days_ago(120)).exists()
    assert results["policy_runs"].deleted == 0
    assert results["package_currency"].deleted == 0
    assert results["pypi_release_snapshots"].kept_by_rule == 1


@pytest.mark.django_db
def test_a_finding_a_retained_kev_row_cites_is_selected_nowhere_and_protected_stays_zero() -> None:
    """A citation under another key is excluded by the selection, not refused every night.

    The KEV row at day -10 is the newest for its package and stays; it links to the
    day -120 finding, which a day -5 finding has superseded and no run reads. The
    floor rule alone would let the finding go; the citation keeps it out of the
    selection, so nothing is offered to the door only to be refused, and the
    record is `succeeded` with `protected=0`. The uncited superseded finding goes.
    """
    package = a_package()
    cited = VulnerabilityFinding.objects.create(
        observed_at=days_ago(120),
        package=package,
        state=MATCHED,
        advisory_id=AN_ADVISORY,
        affected_range="<2",
        matched_version="1.0",
        match_confidence=MatchConfidence.EXACT_RANGE.value,
    )
    uncited = VulnerabilityFinding.objects.create(
        observed_at=days_ago(110),
        package=package,
        state=MATCHED,
        advisory_id=AN_ADVISORY,
        affected_range="<2",
        matched_version="1.0",
        match_confidence=MatchConfidence.EXACT_RANGE.value,
    )
    newest = VulnerabilityFinding.objects.create(
        observed_at=days_ago(5),
        package=package,
        state=MATCHED,
        advisory_id=AN_ADVISORY,
        affected_range="<2",
        matched_version="1.0",
        match_confidence=MatchConfidence.EXACT_RANGE.value,
    )
    KevFinding.objects.create(
        observed_at=days_ago(10),
        package=package,
        state=LISTED,
        vulnerability_finding=cited,
        catalog_date_added=days_ago(200),
    )

    assert floor_removable(VULNERABILITY) == {cited.pk, uncited.pk}
    assert removable(VULNERABILITY) == {uncited.pk}

    results = purge()

    assert rows(VulnerabilityFinding) == {cited.pk, newest.pk}
    assert results["vulnerability_findings"].deleted == 1
    assert results["vulnerability_findings"].protected == 0
    assert results["vulnerability_findings"].kept_by_rule == 1
    record = CollectionRun.objects.get(pk=results["vulnerability_findings"].run_id)
    assert record.status == RunState.SUCCEEDED
    assert "protected=0" in record.detail


@pytest.mark.django_db
def test_a_kev_row_that_is_itself_going_protects_nothing() -> None:
    """A citer selected for removal is not a surviving citer: the finding it cites goes with it.

    Both KEV rows link findings for the same advisory, so the older one is
    superseded under its key and selected; the finding it cites is then cited by
    nothing that survives, and goes in the same purge, after it.
    """
    package = a_package()
    cited = VulnerabilityFinding.objects.create(
        observed_at=days_ago(120),
        package=package,
        state=MATCHED,
        advisory_id=AN_ADVISORY,
        affected_range="<2",
        matched_version="1.0",
        match_confidence=MatchConfidence.EXACT_RANGE.value,
    )
    newest = VulnerabilityFinding.objects.create(
        observed_at=days_ago(5),
        package=package,
        state=MATCHED,
        advisory_id=AN_ADVISORY,
        affected_range="<2",
        matched_version="1.0",
        match_confidence=MatchConfidence.EXACT_RANGE.value,
    )
    going = KevFinding.objects.create(
        observed_at=days_ago(120),
        package=package,
        state=LISTED,
        vulnerability_finding=cited,
        catalog_date_added=days_ago(200),
    )
    KevFinding.objects.create(
        observed_at=days_ago(5),
        package=package,
        state=LISTED,
        vulnerability_finding=newest,
        catalog_date_added=days_ago(200),
    )

    assert removable(KEV) == {going.pk}
    assert removable(VULNERABILITY) == {cited.pk}

    results = purge()

    assert rows(VulnerabilityFinding) == {newest.pk}
    assert results["kev_findings"].deleted == 1
    assert results["vulnerability_findings"].deleted == 1
    assert results["vulnerability_findings"].protected == 0


@pytest.mark.django_db
def test_a_citation_that_arrives_between_the_selection_and_the_delete_is_skipped_counted_and_partial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Matrix row `Protected`: the door's `PROTECT` check is the safety net for the race.

    The selection cannot see a citation committed after it was made, so the race
    is simulated by selecting on the floor rule alone: the KEV-cited finding
    reaches the door, the door refuses it, the batch is retried without it and
    the uncited row goes. The record counts it and is `partial`, not `succeeded`.

    Args:
        monkeypatch: Swaps the selection for the floor rule alone.

    """
    package = a_package()
    cited = VulnerabilityFinding.objects.create(
        observed_at=days_ago(120),
        package=package,
        state=MATCHED,
        advisory_id=AN_ADVISORY,
        affected_range="<2",
        matched_version="1.0",
        match_confidence=MatchConfidence.EXACT_RANGE.value,
    )
    uncited = VulnerabilityFinding.objects.create(
        observed_at=days_ago(110),
        package=package,
        state=MATCHED,
        advisory_id=AN_ADVISORY,
        affected_range="<2",
        matched_version="1.0",
        match_confidence=MatchConfidence.EXACT_RANGE.value,
    )
    newest = VulnerabilityFinding.objects.create(
        observed_at=days_ago(5),
        package=package,
        state=MATCHED,
        advisory_id=AN_ADVISORY,
        affected_range="<2",
        matched_version="1.0",
        match_confidence=MatchConfidence.EXACT_RANGE.value,
    )
    KevFinding.objects.create(
        observed_at=days_ago(10),
        package=package,
        state=LISTED,
        vulnerability_finding=cited,
        catalog_date_added=days_ago(200),
    )
    monkeypatch.setattr(retention, "removable_evidence", floor_removable_evidence)

    with CaptureQueriesContext(connection) as captured:
        results = purge()

    assert rows(VulnerabilityFinding) == {cited.pk, newest.pk}
    assert uncited.pk not in rows(VulnerabilityFinding)
    assert results["vulnerability_findings"].deleted == 1
    assert results["vulnerability_findings"].protected == 1
    record = CollectionRun.objects.get(pk=results["vulnerability_findings"].run_id)
    assert record.status == RunState.PARTIAL
    assert "protected=1" in record.detail
    deletes = [
        query["sql"]
        for query in captured.captured_queries
        if query["sql"].lstrip().upper().startswith("DELETE") and "vulnerability_findings" in query["sql"]
    ]
    assert len(deletes) == 1, "the batch was retried once without the cited row, not row by row"


@pytest.mark.django_db
def test_a_foreign_key_refusal_at_commit_falls_to_one_row_at_a_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """`IntegrityError` names no row -- a deferred foreign key on PostgreSQL -- so each row is tried alone.

    Args:
        monkeypatch: Makes the batch remover refuse a whole batch once, and one
            named row every time.

    """
    package = a_package()
    old = [a_pypi_release(package, version=f"1.{day}", at=days_ago(day)) for day in (120, 115, 110)]
    a_pypi_release(package, version="2.0", at=days_ago(30))
    stubborn = old[1].pk
    real_retire = retention._retire  # noqa: SLF001 - the seam under test

    def refusing_retire(model: type[AppendOnlyModel]) -> object:
        remove = real_retire(model)

        def refuse(ids: list[int]) -> int:
            if len(ids) > 1 or ids == [stubborn]:
                message = "injected: a foreign key committed between the selection and the DELETE"
                raise IntegrityError(message)
            return remove(ids)

        return refuse

    monkeypatch.setattr(retention, "_retire", refusing_retire)

    results = purge()

    assert results["pypi_release_snapshots"].deleted == 2  # noqa: PLR2004 - the two rows the stub let through
    assert results["pypi_release_snapshots"].protected == 1
    assert stubborn in rows(PyPIReleaseSnapshot)
    assert CollectionRun.objects.get(pk=results["pypi_release_snapshots"].run_id).status == RunState.PARTIAL


@pytest.mark.django_db
def test_batches_are_bounded() -> None:
    """Matrix row `Batches`: `--batch 2` over five rows is three `DELETE`s, and the record says five."""
    package = a_package()
    for day in (120, 115, 110, 105, 100):
        a_pypi_release(package, version=f"1.{day}", at=days_ago(day))
    a_pypi_release(package, version="2.0", at=days_ago(30))

    with CaptureQueriesContext(connection) as captured:
        results = purge(batch_size=2)

    deletes = [
        query["sql"]
        for query in captured.captured_queries
        if query["sql"].lstrip().upper().startswith("DELETE") and "pypi_release_snapshots" in query["sql"]
    ]
    assert len(deletes) == THREE_BATCHES
    assert results["pypi_release_snapshots"].deleted == FIVE_ROWS
    assert PyPIReleaseSnapshot.objects.count() == 1


@pytest.mark.django_db
def test_a_dry_run_removes_nothing_and_records_what_would_go() -> None:
    """Matrix row `Dry run`: the counts are real, the rows are all still there, the record says so."""
    package = a_package()
    a_pypi_release(package, version="1.0", at=days_ago(120))
    a_pypi_release(package, version="2.0", at=days_ago(30))
    an_ended_collection_run(finished_at=days_ago(120))
    before = rows(PyPIReleaseSnapshot)

    results = purge(dry_run=True)

    assert rows(PyPIReleaseSnapshot) == before
    assert results["pypi_release_snapshots"].deleted == 1
    assert results["pypi_release_snapshots"].dry_run is True
    assert results["collection_runs"].removed == (("finished", 1), ("unfinished", 0))
    assert "dry_run=true" in CollectionRun.objects.get(pk=results["pypi_release_snapshots"].run_id).detail
    assert len(purge_records()) == len(results)


@pytest.mark.django_db
def test_the_collection_ledger_is_purged_with_finished_and_stale_unfinished_rows_counted_separately() -> None:
    """Matrix row `Ledger`: both go; a recent unfinished row and a recent ended one stay."""
    an_ended_collection_run(finished_at=days_ago(120))
    CollectionRun.objects.create(collector=A_COLLECTOR, started_at=days_ago(120), status=RunState.RUNNING)
    recent = an_ended_collection_run(finished_at=days_ago(30))
    running = CollectionRun.objects.create(collector=A_COLLECTOR, started_at=days_ago(1), status=RunState.RUNNING)

    results = purge()

    assert results["collection_runs"].removed == (("finished", 1), ("unfinished", 1))
    assert results["collection_runs"].deleted == LEDGERS
    remaining = set(CollectionRun.objects.filter(collector=A_COLLECTOR).values_list("pk", flat=True))
    assert remaining == {recent.pk, running.pk}
    detail = CollectionRun.objects.get(pk=results["collection_runs"].run_id).detail
    assert "finished=1" in detail
    assert "unfinished=1" in detail


@pytest.mark.django_db
def test_the_purge_never_touches_the_excluded_tables_or_the_rollup() -> None:
    """Never: `package_health`, packages, identity, workflow, the human-audit tables and the digests."""
    an_ended_collection_run(finished_at=days_ago(100))
    package = a_package()
    a_release(package, version="2.0.0", at=days_ago(120))
    a_run(at=days_ago(100), cutoff=days_ago(100))

    results = purge()

    assert set(results) == {
        *(entry.table for entry in EVIDENCE_ROSTER),
        "policy_runs",
        "collection_runs",
        "package_currency",
        "package_feedstock_presence",
        "package_vulnerability",
        "package_license",
        "package_remediation",
        "package_python_readiness",
        "package_priority",
        "package_work_type",
    }
    assert "package_health" not in results
    assert "identity_overrides" not in results
    assert "inventory_changes" not in results
    assert "package_recollections" not in results
    assert "operator_digests" not in results
    assert Package.objects.filter(pk=package.pk).exists()
    assert PackageHealth.objects.filter(package=package).exists()


# ---------------------------------------------------------------------------
# The replay, inside and outside the window.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_a_replay_inside_the_window_reproduces_through_the_command_after_a_purge() -> None:
    """AC 2, first half, on the form a reviewer uses."""
    an_ended_collection_run(finished_at=days_ago(30))
    package = a_package()
    a_release(package, version="2.0.0", at=days_ago(120))
    a_release(package, version="9.9.9", at=days_ago(10))
    original = a_run(at=days_ago(30), cutoff=days_ago(30))
    purge()
    output = StringIO()

    call_command("replay_policy_run", "--of-run", str(original.pk), "--no-input", stdout=output)

    assert "reproduced it exactly" in output.getvalue()


@pytest.mark.django_db
def test_a_pinned_run_older_than_the_retention_is_replayable() -> None:
    """AC 2: the ledger is the rule. A run the rollup still cites reproduces at any age.

    The purge keeps every row a surviving run read at its cut-off, so a run at day
    -100 that is still in the ledger is replayable after the purge -- there is no
    window narrower than the ledger.
    """
    an_ended_collection_run(finished_at=days_ago(100))
    package = a_package()
    a_release(package, version="2.0.0", at=days_ago(120))
    a_release(package, version="3.0.0", at=days_ago(10))
    pinned = a_run(at=days_ago(100), cutoff=days_ago(100))
    purge()
    output = StringIO()

    call_command("replay_policy_run", "--of-run", str(pinned.pk), "--no-input", stdout=output)

    assert "reproduced it exactly" in output.getvalue()


@pytest.mark.django_db
def test_a_stated_cutoff_no_run_holds_is_refused_naming_the_reason() -> None:
    """The second form is admitted only at an instant a run still in the ledger read at."""
    an_ended_collection_run(finished_at=days_ago(30))
    a_package()
    a_run(at=days_ago(30), cutoff=days_ago(30))

    with pytest.raises(CommandError, match="no retained run read at this cut-off") as refused:
        call_command(
            "replay_policy_run",
            "--policy-version",
            A_RECORDED_POLICY_VERSION,
            "--evidence-cutoff",
            days_ago(31).isoformat(),
            "--no-input",
            stdout=StringIO(),
        )

    assert "the nightly purge was free to remove what a run there would read" in str(refused.value)
    assert PolicyRun.objects.count() == 1


@pytest.mark.django_db
def test_a_stated_cutoff_a_surviving_run_holds_is_admitted_however_old() -> None:
    """The instant, not the window: a pinned run's cut-off at day -100 admits the stated form too."""
    an_ended_collection_run(finished_at=days_ago(100))
    package = a_package()
    a_release(package, version="2.0.0", at=days_ago(120))
    pinned = a_run(at=days_ago(100), cutoff=days_ago(100))
    purge()
    output = StringIO()

    call_command(
        "replay_policy_run",
        "--policy-version",
        A_RECORDED_POLICY_VERSION,
        "--evidence-cutoff",
        pinned.evidence_cutoff.isoformat(),
        "--no-input",
        stdout=output,
    )

    assert "nothing was compared" in output.getvalue()


@pytest.mark.django_db
def test_a_purged_run_is_no_such_run() -> None:
    """Matrix row `Outside window`: the run went with its derived rows, and `--of-run` says so."""
    an_ended_collection_run(finished_at=days_ago(100))
    package = a_package()
    a_release(package, version="2.0.0", at=days_ago(120))
    gone = a_run(at=days_ago(100), cutoff=days_ago(100))
    a_run(at=days_ago(1), cutoff=days_ago(1))
    purge()

    with pytest.raises(CommandError, match="no policy run has id"):
        call_command("replay_policy_run", "--of-run", str(gone.pk), "--no-input", stdout=StringIO())


# ---------------------------------------------------------------------------
# The command.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_the_command_reports_each_table_and_a_summary() -> None:
    """The front door: one human line per table, a summary, and the same records the purge writes."""
    package = a_package()
    a_pypi_release(package, version="1.0", at=days_ago(120))
    a_pypi_release(package, version="2.0", at=days_ago(30))
    output = StringIO()

    call_command("prune_evidence", stdout=output)

    printed = output.getvalue()
    assert "pypi_release_snapshots: purged 1 row(s)" in printed
    assert f"at a retention of {RETENTION} day(s)" in printed
    assert PyPIReleaseSnapshot.objects.count() == 1
    assert len(purge_records()) == RECORDS_PER_PURGE


@pytest.mark.django_db
def test_the_command_rehearses_with_dry_run() -> None:
    """`--dry-run`: the line says "would purge", and the rows are all still there."""
    package = a_package()
    a_pypi_release(package, version="1.0", at=days_ago(120))
    a_pypi_release(package, version="2.0", at=days_ago(30))
    output = StringIO()

    call_command("prune_evidence", "--dry-run", stdout=output)

    assert "pypi_release_snapshots: would purge 1 row(s)" in output.getvalue()
    assert {row.latest_version for row in PyPIReleaseSnapshot.objects.all()} == {"1.0", "2.0"}


@pytest.mark.django_db
def test_the_command_refuses_an_unbounded_batch() -> None:
    """`--batch 0` is the one statement the purge exists not to issue."""
    with pytest.raises(CommandError, match="--batch must be at least 1"):
        call_command("prune_evidence", "--batch", "0", stdout=StringIO())

    assert purge_records() == []


@pytest.mark.django_db
def test_the_command_refuses_an_unusable_retention_before_touching_anything() -> None:
    """A retention that bypassed the boot hook still never becomes a cut-off."""
    with override_settings(**{RETENTION_SETTING: 0}), pytest.raises(CommandError, match=RETENTION_SETTING):
        call_command("prune_evidence", stdout=StringIO())

    assert purge_records() == []


# ---------------------------------------------------------------------------
# The floor rule over every table, and at every boundary.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
@pytest.mark.parametrize("entry", EVIDENCE_ROSTER, ids=lambda entry: entry.table)
def test_every_table_lets_a_superseded_old_row_go_and_keeps_its_newest(entry: EvidenceTable) -> None:
    """Matrix rows `Old rows` and `Newest kept`, under every key shape the roster declares.

    Args:
        entry: The roster entry.

    """
    package = a_package()
    old = a_row(entry, package, at=days_ago(120))

    assert removable(entry) == set(), "a package's only row is its newest"

    a_row(entry, package, at=days_ago(30))

    assert removable(entry) == {old.pk}


@pytest.mark.django_db
@pytest.mark.parametrize(
    "entry",
    [entry for entry in EVIDENCE_ROSTER if entry.label in ANOTHER_KEY],
    ids=lambda entry: entry.table,
)
def test_a_newer_row_under_another_key_supersedes_nothing(entry: EvidenceTable) -> None:
    """The key is the reader's: a row under another channel, platform, advisory, series or source key is a neighbour.

    Args:
        entry: A roster entry whose key is wider than the package.

    """
    package = a_package()
    old = a_row(entry, package, at=days_ago(120))
    a_row(entry, package, at=days_ago(30), **ANOTHER_KEY[entry.label])

    assert removable(entry) == set()
    assert old.pk in rows(entry.model)


@pytest.mark.django_db
@pytest.mark.parametrize("entry", EVIDENCE_ROSTER, ids=lambda entry: entry.table)
def test_every_table_keeps_the_row_a_surviving_run_reads_at_its_cutoff(entry: EvidenceTable) -> None:
    """Matrix row `Replay floor`, under every key shape.

    Args:
        entry: The roster entry.

    """
    an_ended_collection_run(finished_at=days_ago(30))
    package = a_package()
    read = a_row(entry, package, at=days_ago(120))
    a_row(entry, package, at=days_ago(10))
    a_run(at=days_ago(30), cutoff=days_ago(30))

    assert removable(entry) == set()
    assert read.pk in rows(entry.model)


@pytest.mark.django_db
def test_a_row_observed_exactly_at_the_cutoff_is_not_older_than_it() -> None:
    """The window is `observed_at < cut-off`: the boundary instant stays."""
    package = a_package()
    at_the_cutoff = a_pypi_release(package, version="1.0", at=CUTOFF)
    just_before = a_pypi_release(package, version="1.1", at=CUTOFF - timedelta(seconds=1))
    a_pypi_release(package, version="2.0", at=days_ago(30))

    assert removable(PYPI) == {just_before.pk}
    assert at_the_cutoff.pk not in removable(PYPI)


@pytest.mark.django_db
def test_a_row_observed_exactly_at_a_surviving_runs_cutoff_is_what_it_reads() -> None:
    """Readers take `observed_at <= cutoff`, so the row at the instant is the one kept."""
    an_ended_collection_run(finished_at=days_ago(100))
    package = a_package()
    before = a_pypi_release(package, version="1.0", at=days_ago(120))
    at_the_instant = a_pypi_release(package, version="1.5", at=days_ago(100))
    a_pypi_release(package, version="2.0", at=days_ago(5))
    a_run(at=days_ago(100), cutoff=days_ago(100))

    assert removable(PYPI) == {before.pk}
    assert at_the_instant.pk not in removable(PYPI)


@pytest.mark.django_db
def test_a_ledger_row_that_ended_exactly_at_the_cutoff_stays() -> None:
    """`finished_at < cut-off`, strictly, on both ledgers."""
    at_the_cutoff = an_ended_collection_run(finished_at=CUTOFF)
    just_before = an_ended_collection_run(finished_at=CUTOFF - timedelta(seconds=1))
    run_at_the_cutoff = PolicyRun.objects.create(
        policy_version=A_RECORDED_POLICY_VERSION,
        evidence_cutoff=CUTOFF,
        started_at=CUTOFF - timedelta(minutes=1),
        finished_at=CUTOFF,
        status=RunState.SUCCEEDED,
    )

    results = purge()

    assert results["collection_runs"].removed == (("finished", 1), ("unfinished", 0))
    assert CollectionRun.objects.filter(pk=at_the_cutoff.pk).exists()
    assert not CollectionRun.objects.filter(pk=just_before.pk).exists()
    assert PolicyRun.objects.filter(pk=run_at_the_cutoff.pk).exists()
    assert results["policy_runs"].deleted == 0


@pytest.mark.django_db
def test_an_unfinished_policy_run_started_before_the_cutoff_goes_and_pins_nothing() -> None:
    """The second branch of `purgeable_policy_runs`: a killed policy worker's row ages by `started_at`.

    Left in place it would read as a surviving run at its cut-off and keep the
    row it would have read for ever.
    """
    package = a_package()
    old = a_pypi_release(package, version="1.0", at=days_ago(120))
    a_pypi_release(package, version="2.0", at=days_ago(5))
    killed = PolicyRun.objects.create(
        policy_version=A_RECORDED_POLICY_VERSION,
        evidence_cutoff=days_ago(100),
        started_at=days_ago(100),
        status=RunState.RUNNING,
    )

    assert removable(PYPI) == {old.pk}

    results = purge()

    assert not PolicyRun.objects.filter(pk=killed.pk).exists()
    assert results["policy_runs"].deleted == 1
    assert old.pk not in rows(PyPIReleaseSnapshot)


@pytest.mark.django_db
def test_a_second_purge_straight_after_the_first_removes_nothing_and_records_zeros() -> None:
    """Idempotence: the selection is empty once it has been applied, and the records say so."""
    an_ended_collection_run(finished_at=days_ago(120))
    package = a_package()
    a_release(package, version="1.0.0", at=days_ago(120))
    a_release(package, version="2.0.0", at=days_ago(30))
    first = purge()
    assert sum(result.deleted for result in first.values()) > 0

    second = purge()

    assert all(result.deleted == 0 for result in second.values()), {t: r.deleted for t, r in second.items()}
    assert all(result.protected == 0 for result in second.values())
    assert [result.kept_by_rule for result in second.values()] == [result.kept_by_rule for result in first.values()]
    assert all("deleted=0" in CollectionRun.objects.get(pk=result.run_id).detail for result in second.values())


@pytest.mark.django_db
def test_a_run_something_still_cites_is_skipped_on_the_policy_runs_leg(monkeypatch: pytest.MonkeyPatch) -> None:
    """`protected` on the ledger leg: a run whose derived rows were not removed first is refused, counted, `partial`.

    Args:
        monkeypatch: Leaves the derived tables out of the purge, so their rows
            still cite the run when its leg runs.

    """
    an_ended_collection_run(finished_at=days_ago(100))
    package = a_package()
    a_release(package, version="2.0.0", at=days_ago(120))
    cited = a_run(at=days_ago(100), cutoff=days_ago(100))
    a_run(at=days_ago(1), cutoff=days_ago(1))
    monkeypatch.setattr(retention, "derived_tables", lambda: ())

    results = purge()

    assert PolicyRun.objects.filter(pk=cited.pk).exists()
    assert results["policy_runs"].deleted == 0
    assert results["policy_runs"].protected == 1
    assert CollectionRun.objects.get(pk=results["policy_runs"].run_id).status == RunState.PARTIAL


# ---------------------------------------------------------------------------
# The purge's own ledger rows.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_the_cutoff_choice_never_reads_the_purges_own_rows() -> None:
    """A purge's ending is not a collection's, and a killed purge bounds nothing.

    The newest collection ended at day -1; a purge record ended after it and a
    purge a killed worker left running started before it. Counting either, the
    cut-off would be the purge's ending or bounded to before the collection's;
    excluding both by name, it is the collection's.
    """
    collection = an_ended_collection_run(finished_at=days_ago(1))
    a_purge_record(finished_at=NOW - timedelta(hours=1), started_at=NOW - timedelta(hours=2))
    a_purge_record(finished_at=None, started_at=days_ago(2))

    assert choose_evidence_cutoff() == collection.finished_at


@pytest.mark.django_db
def test_the_seeders_synthetic_collection_still_supplies_a_cutoff() -> None:
    """Only the purge's name is excluded: a fresh seed's `local-dev-demo-seed` run is a collection."""
    seed = CollectionRun.objects.create(
        collector=THE_SEED_COLLECTOR,
        started_at=days_ago(1),
        finished_at=days_ago(1) + timedelta(minutes=1),
        status=RunState.SUCCEEDED,
    )
    a_purge_record(finished_at=NOW - timedelta(hours=1), started_at=NOW - timedelta(hours=2))

    assert choose_evidence_cutoff() == seed.finished_at


@pytest.mark.django_db
def test_a_purge_closes_the_rows_an_earlier_purge_left_running() -> None:
    """A killed purge's rows are finalized `failed`, saying so, by the next purge."""
    stale = a_purge_record(finished_at=None, started_at=days_ago(1))
    recent = a_purge_record(finished_at=None, started_at=NOW - timedelta(hours=1))

    results = purge()

    for row in (stale, recent):
        row.refresh_from_db()
        assert row.status == RunState.FAILED
        assert row.detail == SUPERSEDED_DETAIL
        assert row.finished_at == NOW
    assert not CollectionRun.objects.filter(collector=PRUNE_COLLECTOR).unfinished().exists()
    assert all(CollectionRun.objects.get(pk=result.run_id).finished_at is not None for result in results.values())


@pytest.mark.django_db
def test_a_cutoff_that_moved_further_than_the_night_is_logged_naming_both_instants() -> None:
    """An operator lowering ninety days to thirty sees the sixty days about to go, once, in the log."""
    a_purge_record(
        finished_at=days_ago(1),
        started_at=days_ago(1),
        detail=f"table=collection_runs cutoff={days_ago(91).isoformat()} deleted=0",
    )

    with structlog.testing.capture_logs() as quiet:
        purge(retention=RETENTION)
    with structlog.testing.capture_logs() as moved:
        purge(retention=30)

    assert [event for event in quiet if event["event"] == CUTOFF_MOVED_EVENT] == []
    warnings = [event for event in moved if event["event"] == CUTOFF_MOVED_EVENT]
    assert len(warnings) == 1
    assert warnings[0]["log_level"] == "warning"
    assert warnings[0]["cutoff"] == retention_cutoff(now=NOW, days=30).isoformat()
    assert warnings[0]["previous_cutoff"]


# ---------------------------------------------------------------------------
# A table that fails, and a rehearsal that tells the truth.
# ---------------------------------------------------------------------------


def _failing_on(table_model: type[AppendOnlyModel], *, after_batches: int) -> object:
    """Return a `_retire` replacement whose remover for one table fails after some batches.

    Args:
        table_model: The table to fail.
        after_batches: How many batches to let through first.

    Returns:
        The replacement.

    """
    real_retire = retention._retire  # noqa: SLF001 - the seam under test
    seen = {"batches": 0}

    def retire(model: type[AppendOnlyModel]) -> object:
        remove = real_retire(model)
        if model is not table_model:
            return remove

        def failing(ids: list[int]) -> int:
            if seen["batches"] >= after_batches:
                message = "injected: the connection went away mid-purge"
                raise DatabaseError(message)
            seen["batches"] += 1
            return remove(ids)

        return failing

    return retire


@pytest.mark.django_db
def test_a_failure_on_one_table_records_it_failed_with_what_went_and_the_others_carry_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Item: per table, not per night. Table 2 of 3 fails on its second batch; 1 and 3 are purged.

    Args:
        monkeypatch: Fails `source_release_snapshots` after one batch of one.

    """
    package = a_package()
    for day in (120, 110):
        a_release(package, version=f"1.{day}", at=days_ago(day))
    a_release(package, version="2.0", at=days_ago(30))
    monkeypatch.setattr(retention, "_retire", _failing_on(SourceReleaseSnapshot, after_batches=1))

    results = purge(batch_size=1)

    failed = results["source_release_snapshots"]
    assert failed.error is not None
    assert failed.deleted == 1
    record = CollectionRun.objects.get(pk=failed.run_id)
    assert record.status == RunState.FAILED
    assert "deleted=1" in record.detail
    assert "error=DatabaseError: injected" in record.detail
    assert SourceReleaseSnapshot.objects.count() == 2  # noqa: PLR2004 - one of the two old rows went before the failure
    assert results["pypi_release_snapshots"].deleted == 2  # noqa: PLR2004 - the table after the failed one was purged
    assert results["pypi_release_snapshots"].error is None
    assert results["collection_runs"].error is None
    assert all(CollectionRun.objects.get(pk=result.run_id).finished_at is not None for result in results.values())


@pytest.mark.django_db
def test_the_command_exits_non_zero_naming_the_tables_that_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    """The schedule sees the failure; the other tables were still purged.

    Args:
        monkeypatch: Fails `source_release_snapshots` outright.

    """
    package = a_package()
    a_release(package, version="1.0", at=days_ago(120))
    a_release(package, version="2.0", at=days_ago(30))
    monkeypatch.setattr(retention, "_retire", _failing_on(SourceReleaseSnapshot, after_batches=0))
    output = StringIO()

    with pytest.raises(CommandError, match="failed part-way") as refused:
        call_command("prune_evidence", stdout=output)

    assert "['source_release_snapshots']" in str(refused.value)

    assert "source_release_snapshots: purged 0 row(s)" in output.getvalue()
    assert "FAILED: DatabaseError: injected" in output.getvalue()
    assert PyPIReleaseSnapshot.objects.count() == 1


@pytest.mark.django_db
def test_a_rehearsal_reports_exactly_what_the_real_run_then_does() -> None:
    """Matrix row `Dry run`, made strict: the derived-rows-then-run history, once dry and once real, counts equal.

    The rehearsal removes nothing, so the out-of-window run's derived rows still
    cite the evidence its selection counts; the selection has to see through
    that -- a citer that will go is not a surviving citer -- or the rehearsal
    understates the purge.
    """
    an_ended_collection_run(finished_at=days_ago(100))
    package = a_package()
    a_release(package, version="2.0.0", at=days_ago(120))
    a_run(at=days_ago(100), cutoff=days_ago(100))
    a_release(package, version="3.0.0", at=days_ago(10))
    a_run(at=days_ago(1), cutoff=days_ago(1))

    rehearsal = purge(dry_run=True)
    real = purge()

    for table, rehearsed in rehearsal.items():
        assert real[table].removed == rehearsed.removed, table
        assert real[table].protected == rehearsed.protected == 0, table
        assert real[table].kept_by_rule == rehearsed.kept_by_rule, table
    assert real["pypi_release_snapshots"].deleted == 1
    assert real["policy_runs"].deleted == 1


# ---------------------------------------------------------------------------
# The door's shape refusals.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("shape", "expected"),
    [
        (lambda qs: qs[:5], "limit"),
        (lambda qs: qs.values("pk"), "values"),
        (lambda qs: qs.values_list("pk", flat=True), "values"),
        (lambda qs: qs.distinct("package_id"), "distinct"),
        (lambda qs: qs.union(qs), "union"),
    ],
    ids=["sliced", "values", "values-list", "distinct-fields", "combined"],
)
def test_the_door_refuses_the_shapes_a_raw_delete_would_get_wrong(
    shape: object,
    expected: str,
) -> None:
    """Item: `_raw_delete` drops a slice silently, so the four shapes `delete()` refuses are refused here too.

    Args:
        shape: How the selection is bent.
        expected: The word the refusal carries.

    """
    package = a_package()
    a_pypi_release(package, version="1.0", at=days_ago(120))
    bent = shape(PyPIReleaseSnapshot.objects.all())  # type: ignore[operator]

    with pytest.raises(TypeError, match=expected):
        bent.retire(door=retention._DOOR)  # noqa: SLF001 - the token is the subject

    assert PyPIReleaseSnapshot.objects.count() == 1


@pytest.mark.django_db
def test_a_row_that_already_ended_cannot_be_abandoned() -> None:
    """`abandon` closes a row a killed purge left running and refuses one that ended."""
    ended = a_purge_record(finished_at=days_ago(1), started_at=days_ago(1))

    with pytest.raises(RunLedgerError, match="already ended"):
        abandon(ended, clock=FixedClock(instant=NOW), detail=SUPERSEDED_DETAIL)

    ended.refresh_from_db()
    assert ended.status == RunState.SUCCEEDED
