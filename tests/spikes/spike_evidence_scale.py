"""CPM-OPERATE-S10: what do the evidence tables cost at ten thousand packages?

`CPM-NFR-1` sizes the product at ten thousand packages, and `CPM-OPERATE-S07`'s
nightly purge, the rollup's per-package reads and the package page were all
designed and tested against tables of a few hundred rows. This module seeds a
throwaway PostgreSQL 17 with ninety-one days of evidence for ten thousand
packages on every table the purge touches -- one row per package per day per
reader key on the eleven evidence tables, one finished run per package per
swept collector per day on the collection ledger, ninety policy runs with
their derived rows and the rollup -- and then asks the database, with
`EXPLAIN (ANALYZE, BUFFERS)`, what the product's own queries cost against it.

**Not a unit test and not part of the gate.** It runs only through
`pixi run spike-scale`, which starts `postgres:17` in a container, points
`DATABASE_URL` at it and names this module on the command line; the module is
`spike_*.py`, which `[tool.pytest.ini_options] python_files` does not match, so
`pytest tests/` -- what `test-cov` runs -- never collects it. It refuses to run
against anything but PostgreSQL 17, naming what it found and the command that
provides the right one, because a plan from SQLite or from another major
version says nothing about the database the gate and the deployment use. The
seeder alone is exercised by the gate at three packages by two days
(`tests/integration/django_apps/test_evidence_scale_seed.py`), so a renamed
column fails in CI's PostgreSQL job rather than after a Docker run.

**What is proven and what is bounded.** The spike *asserts*, so a re-run is a
regression check rather than a printout: that the database was empty and every
table then holds exactly the rows the scale rule says it should (a
`CheckConstraint` refusing the seed would show here, not as a quietly smaller
table); that every per-package read stays within a buffer ceiling, so a
regression to a whole-index walk fails even where the plan still says "index";
that no measured plan sequentially scans an evidence, ledger or derived table
except the queries `WHOLE_TABLE_BY_NATURE` names and the citer-hash shape
`RECORDED_CITER_HASH_SELECTIONS` records, each in the story with its reason; and that the steady-state nightly purge
removes exactly one day's rows from every roster table while keeping every
package's newest row -- including the newest row of the packages absent since
day 0, which is the floor rule's keep path -- and every row a surviving policy
run reads. Timings are *printed and recorded*, never asserted: a wall-clock
number is a fact about the laptop that produced it, a sequential scan is a
defect on any machine.

**What the seed is not.** One row per package per day on every table is
pessimistic for the fourteen- and thirty-day-target collectors, which observe a
package a few times a window, not daily; one platform and two channels, two
advisories and two series per package is optimistic on key cardinality against
an inventory whose packages carry many advisories and platforms; the
`protected` path (a citation committed between selection and delete) is not
exercised. The story states these with their direction.

**Where the verdict lives.** The story
(`_bmad-output/implementation-artifacts/stories/cpm-operate-s10-...md`) carries
every plan and timing this module prints, verbatim, and the partitioning
verdict drawn from them by the rule the story states before the numbers; the
operations page repeats the numbers and the verdict under "Measuring at scale".
What retires this spike is a change to what it measures -- an evidence index,
the floor rule, the purge's batching, or the scale `CPM-NFR-1` names -- each of
which re-runs it and re-records the outcome rather than inheriting the old one.
"""

# ruff: noqa: S608 - every statement below is composed from module constants and roster table names; nothing here is input
from __future__ import annotations

import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from dataclasses import field
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import TYPE_CHECKING
from typing import Any
from typing import Final

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from conda_sentinel.collectors import digest
from conda_sentinel.collectors.models import snapshot_as_of
from conda_sentinel.collectors.recollection import in_flight_window
from conda_sentinel.core import retention
from conda_sentinel.core.clock import FixedClock
from conda_sentinel.core.freshness import latest_observation
from conda_sentinel.core.ledger import runs_in_flight
from conda_sentinel.core.models import CollectionRun
from conda_sentinel.core.models import PackageHealth
from conda_sentinel.core.models import PolicyRun
from conda_sentinel.core.pagination import DEFAULT_PAGE_SIZE
from conda_sentinel.core.policy_run import PolicyRunError
from conda_sentinel.core.policy_run import choose_evidence_cutoff
from conda_sentinel.core.registry import swept_collectors
from conda_sentinel.core.retention import DELETED_LEG
from conda_sentinel.core.retention import EVIDENCE_ROSTER
from conda_sentinel.core.retention import PRUNE_COLLECTOR
from conda_sentinel.core.retention import TablePurge
from conda_sentinel.core.retention import derived_tables
from conda_sentinel.core.retention import floor_removable_evidence
from conda_sentinel.core.retention import purge_evidence
from conda_sentinel.core.retention import purgeable_policy_runs
from conda_sentinel.core.retention import removable_evidence
from conda_sentinel.core.retention import retention_cutoff
from conda_sentinel.identity.models import Package
from conda_sentinel.policies import currency
from conda_sentinel.policies import feedstock
from conda_sentinel.policies import licence
from conda_sentinel.policies import py314_readiness
from conda_sentinel.policies import remediation
from conda_sentinel.policies import vulnerability
from conda_sentinel.surface.coverage import collector_health
from conda_sentinel.surface.coverage import coverage_of
from conda_sentinel.surface.detail import last_recollection
from conda_sentinel.surface.detail import recent_runs
from conda_sentinel.surface.detail import traces_for
from conda_sentinel.surface.listing import health_queryset

if TYPE_CHECKING:
    from collections.abc import Callable
    from collections.abc import Iterator

# Every test here is a spike, and every one touches a real database. The
# per-test `django_db` markers below are ordering as much as access:
# pytest-django reorders a session so plain `django_db` tests run first,
# transactional ones second and unmarked ones last, keeping file order within
# each group. So the measuring tests carry the plain marker, the purge carries
# `transaction=True` (its batches commit as the nightly purge's do, and its
# teardown flush is the last thing to touch the seed), and the printing test
# carries none, so it runs after everything it prints.
pytestmark = [pytest.mark.spike, pytest.mark.integration]

#: The scale `CPM-NFR-1` names, and one day past the retention so the purge has
#: a real day to remove: days 1..90 lie inside the window, day 0 outside it.
#: Overridable from the environment for the gate's seeder smoke and for a quick
#: local run; the recorded numbers are the defaults' and say so.
SCALE_PACKAGES: Final[int] = int(os.environ.get("SPIKE_SCALE_PACKAGES", "10000"))
SCALE_DAYS: Final[int] = int(os.environ.get("SPIKE_SCALE_DAYS", "91"))
RETENTION_DAYS: Final[int] = 90

#: One package in this many is *absent* after `ABSENT_AFTER_DAY`: observed on
#: that day and never again, on every roster table and the ledger. Their
#: newest row is older than the cut-off, which is the one case the floor rule
#: keeps a row for -- so the purge's keep path runs and is asserted rather than
#: vacuous. (A package absent after a day *inside* the window would not do it:
#: its newest row is not older than the cut-off, so nothing about it is kept
#: by rule.)
ABSENT_MODULUS: Final[int] = 100
ABSENT_AFTER_DAY: Final[int] = 0

#: The one major version a verdict here is a statement about: the gate's image
#: and the deployment's. A plan from another major is a different planner.
REQUIRED_MAJOR_VERSION: Final[int] = 17

#: The spike's fixed instant. Every seeded timestamp is derived from it, so a
#: re-run seeds byte-identical tables and the cut-off falls where the story
#: says it does.
NOW: Final[datetime] = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)

#: Day 0 begins here; day `d`'s rows are observed at `BASE + d days + (package
#: id mod 24) hours`, spread across the day so ties do not hide the ordering the
#: readers depend on. The retention cut-off is `NOW - 90 days == BASE + 1 day`,
#: so every day-0 row is older than it and no later row is.
BASE: Final[datetime] = NOW - timedelta(days=SCALE_DAYS)

#: The multi-key tables get two keys per package, so the newest-per-key rule
#: is exercised rather than trivially satisfied.
KEYS_PER_PACKAGE: Final[dict[str, int]] = {
    "inventory_snapshots": 2,
    "conda_package_snapshots": 2,
    "kev_findings": 2,
    "vulnerability_findings": 2,
    "license_findings": 2,
    "python_readiness_assessments": 2,
    "python_verification_results": 2,
}

#: The policy runs: one per day for days 0..(SCALE_DAYS - 2), each reading as
#: of the end of its day. The newest day of evidence has not been evaluated
#: yet, which is what a database looks like between a sweep and the night's
#: policy run. Run 0 finished before the cut-off and pins nothing, so it is the
#: one run the steady-state purge removes; the newest run is what the rollup
#: cites. Every run has its derived rows, as it does in production.
POLICY_RUN_DAYS: Final[int] = SCALE_DAYS - 1
PINNED_RUN_DAY: Final[int] = POLICY_RUN_DAYS - 1
PURGED_RUN_DAY: Final[int] = 0
POLICY_VERSION: Final[str] = "spike-2026-09-14"

#: The derived tables the seeder writes, by `db_table`; asserted equal to
#: `derived_tables()` so a ninth pass table fails loudly rather than seeding
#: nothing.
SEEDED_DERIVED_TABLES: Final[frozenset[str]] = frozenset(
    {
        "package_currency",
        "package_feedstock_presence",
        "package_vulnerability",
        "package_license",
        "package_remediation",
        "package_python_readiness",
        "package_priority",
        "package_work_type",
    },
)

#: The package every per-package read is measured on: the middle of the id
#: range, which is neither the first row of every index nor the last, and not
#: an absent one (`pkg-05001` at the default scale).
MEASURED_PACKAGE: Final[str] = f"pkg-{SCALE_PACKAGES // 2 + 1:05d}"

#: How many shared buffers (hit + read) a per-package read may touch. The
#: `*_pkg_observed` probes touch four or five; the package page's history slice
#: two dozen. A regression to walking the whole of an index -- which still
#: reads "Index Scan" in a plan -- fails here.
PER_PACKAGE_BUFFER_CEILING: Final[int] = 64

#: How long one statement may run before PostgreSQL abandons it, and how long
#: the purge test may run before the alarm below abandons it. Neither is a
#: verdict number: they exist so a regression hangs for minutes, not for ever.
STATEMENT_TIMEOUT: Final[str] = "600s"
PURGE_DEADLINE_SECONDS: Final[int] = 1800

#: The measured queries that ask a question about every row of a table, and
#: for which a sequential scan is the planner being right. Each name is
#: recorded in the story with the reason. The coverage screen's ledger
#: group-by is the one the story admitted in advance; the cut-off choice's
#: count of endings runs only on its refusal path, to say how many endings were
#: unusable, and counts the whole ledger to say it.
WHOLE_TABLE_BY_NATURE: Final[frozenset[str]] = frozenset(
    {
        "coverage.collector_health: ledger group-by",
        "rollup.choose_evidence_cutoff (refusal path)",
    },
)

#: The purge selections whose plans may sequentially scan the *citing* side
#: of their anti-join, recorded in the story as deferred work rather than fixed
#: here. `removable_evidence` narrows the floor rule by "no surviving row cites
#: this one", one correlated `NOT EXISTS` per citing relation; the derived
#: tables cite through foreign keys that carry Django's own index, and the
#: planner still hashes the citing table -- its rows for every surviving run,
#: nearly the whole table -- because the candidates are one per cent of it and
#: the estimate prefers one hash to twenty thousand probes. For the two
#: advisory tables the citing side is also kev's own floor rule, whose key
#: `(package, vulnerability_finding__advisory_id)` holds a column of the joined
#: table, so every `kev_findings LEFT JOIN vulnerability_findings` row is
#: hashed. Neither is an index this story can add: one exists and is declined,
#: the other cannot carry a joined column. The allowance is by *shape* -- these
#: selections, these tables as the citing side -- so a plan that sequentially
#: scans the table being selected, or the ledger, still fails.
RECORDED_CITER_HASH_SELECTIONS: Final[tuple[str, ...]] = (
    "purge.removable_evidence first batch: ",
    "purge.dry-run count: ",
)
RECORDED_CITER_HASH_TABLES: Final[frozenset[str]] = frozenset(
    SEEDED_DERIVED_TABLES | {"kev_findings", "vulnerability_findings"},
)

#: The tables a sequential scan is a defect on: the roster, the collection
#: ledger and the derived tables -- the tables that grow by the inventory every
#: day or every run. `policy_runs` is not among them: it holds one row per
#: policy run inside the retention -- ninety here, and never more than the
#: retention allows -- and the planner is right to read ninety rows without
#: an index, on every machine.
GUARDED_TABLES: Final[frozenset[str]] = frozenset(
    {entry.table for entry in EVIDENCE_ROSTER} | {"collection_runs"} | SEEDED_DERIVED_TABLES,
)

#: What is read off a plan: the scans on guarded tables, the two times, and the
#: statement's buffer line.
_SEQ_SCAN: Final[re.Pattern[str]] = re.compile(r"Seq Scan on (\w+)")
_EXECUTION_TIME: Final[re.Pattern[str]] = re.compile(r"Execution Time: ([\d.]+) ms")
_PLANNING_TIME: Final[re.Pattern[str]] = re.compile(r"Planning Time: ([\d.]+) ms")
_BUFFERS: Final[re.Pattern[str]] = re.compile(r"Buffers: (shared[^\n]*)")
_SHARED_HIT: Final[re.Pattern[str]] = re.compile(r"shared hit=(\d+)")
_SHARED_READ: Final[re.Pattern[str]] = re.compile(r"shared[^\n]* read=(\d+)")

#: The PostgreSQL settings the plans depend on, printed with the environment.
PLANNER_SETTINGS: Final[tuple[str, ...]] = (
    "server_version",
    "shared_buffers",
    "work_mem",
    "max_parallel_workers_per_gather",
    "effective_cache_size",
    "random_page_cost",
)


def _say(line: str = "") -> None:
    """Write one line to the terminal; `pytest -s` is what lets it through."""
    sys.stdout.write(f"{line}\n")
    sys.stdout.flush()


def _literal(instant: datetime) -> str:
    """Return an aware instant as a PostgreSQL literal."""
    return f"'{instant.isoformat()}'::timestamptz"


@dataclass(frozen=True)
class Scale:
    """How much to seed.

    Attributes:
        packages: How many packages.
        days: How many days of evidence, the oldest first.

    """

    packages: int
    days: int

    @property
    def base(self) -> datetime:
        """Return the instant day 0 begins."""
        return NOW - timedelta(days=self.days)

    @property
    def absent(self) -> int:
        """Return how many packages are absent after `ABSENT_AFTER_DAY`."""
        return self.packages // ABSENT_MODULUS

    @property
    def policy_runs(self) -> int:
        """Return how many policy runs are seeded: one per day but the newest."""
        return self.days - 1

    def evidence_rows(self, table: str) -> int:
        """Return how many rows one roster table holds after seeding.

        Args:
            table: The table.

        Returns:
            `keys x (packages x days - absent x (days - ABSENT_AFTER_DAY - 1))`.

        """
        present = self.packages * self.days - self.absent * (self.days - ABSENT_AFTER_DAY - 1)
        return KEYS_PER_PACKAGE.get(table, 1) * present

    def ledger_rows(self, collectors: int) -> int:
        """Return how many `collection_runs` rows the seed writes.

        Args:
            collectors: How many swept collectors.

        Returns:
            One finished run per present package-day per collector, one dispatch
            per collector per day, and one stale open row per collector.

        """
        present = self.packages * self.days - self.absent * (self.days - ABSENT_AFTER_DAY - 1)
        return present * collectors + self.days * collectors + collectors


#: The default scale, from the constants above.
SCALE: Final[Scale] = Scale(packages=SCALE_PACKAGES, days=SCALE_DAYS)


def _observed(scale: Scale) -> str:
    """Return day `d.day`'s observation instant for package `p`, as SQL over the seed's joins."""
    return f"{_literal(scale.base)} + make_interval(days => d.day, hours => (p.id % 24)::int)"


def _present(scale: Scale) -> str:
    """Return the SQL predicate that keeps a package-day: every day for most packages, day 0 for the absent."""
    del scale
    return f"(p.id % {ABSENT_MODULUS} <> 0 OR d.day <= {ABSENT_AFTER_DAY})"


def _run_instants(scale: Scale, day: int) -> tuple[datetime, datetime, datetime]:
    """Return `(started_at, finished_at, evidence_cutoff)` for the policy run of one day."""
    end_of_day = scale.base + timedelta(days=day, hours=23)
    return end_of_day + timedelta(minutes=30), end_of_day + timedelta(minutes=45), end_of_day


# ---------------------------------------------------------------------------
# The refusal, and the seed.
# ---------------------------------------------------------------------------


def require_postgresql_17() -> None:
    """Refuse any backend but PostgreSQL 17, naming what was found and how to get the right one.

    The vendor is *asserted* rather than branched on: `tests/unit/test_suite_policy.py`
    bans `if connection.vendor ...` in every module under `tests/` -- the shape a
    dodged gate failure takes -- and names `assert connection.vendor == expected`
    as the sanctioned opposite. A failed assertion is the same loud refusal
    `pytest.fail` would be, with the same message, and needs no exemption.
    """
    assert connection.vendor == "postgresql", (
        f"the scale spike measures plans on PostgreSQL {REQUIRED_MAJOR_VERSION} and found {connection.vendor!r}. "
        "Run it through `pixi run spike-scale`, which starts postgres:17 in a container and sets DATABASE_URL."
    )
    major = int(connection.pg_version) // 10_000
    if major != REQUIRED_MAJOR_VERSION:
        pytest.fail(
            f"the scale spike measures plans on PostgreSQL {REQUIRED_MAJOR_VERSION} and found major version {major} "
            f"(pg_version {connection.pg_version}). Run it through `pixi run spike-scale`.",
        )


def count_rows(table: str) -> int:
    """Return `SELECT count(*)` of one table."""
    with connection.cursor() as cursor:
        cursor.execute(f"SELECT count(*) FROM {table}")
        return int(cursor.fetchone()[0])


def seeded_tables() -> tuple[str, ...]:
    """Return every table the seed writes, in seeding order."""
    return (
        "packages",
        *(entry.table for entry in EVIDENCE_ROSTER),
        "collection_runs",
        "policy_runs",
        "package_health",
        *sorted(SEEDED_DERIVED_TABLES),
    )


def require_empty_database() -> None:
    """Refuse to seed over rows: the counts below are exact, and a second seed would double every one."""
    counts = {table: count_rows(table) for table in seeded_tables()}
    occupied = {table: count for table, count in counts.items() if count}
    assert occupied == {}, (
        f"the database already holds rows in {occupied}; the seed needs an empty one. "
        "`pixi run spike-scale` starts a fresh container every time -- run it that way."
    )


def _timed(label: str, sql: str) -> None:
    """Execute one seeding statement and print how long it took."""
    started = time.perf_counter()
    with connection.cursor() as cursor:
        cursor.execute(sql)
    _say(f"  seeded {label:<52s} {time.perf_counter() - started:8.1f} s")


def seed_packages(scale: Scale) -> None:
    """Verified packages with unique names and source keys."""
    _timed(
        "packages",
        f"""
        INSERT INTO packages (canonical_name, display_name, source_repository_url, primary_purl, primary_type,
                              conda_purl, alternative_purls, cpes, version_authority_order, identity_source,
                              associator_key, resolved_at, confidence)
        SELECT 'pkg-' || lpad(n::text, 5, '0'), 'pkg-' || lpad(n::text, 5, '0'),
               'https://github.com/example/pkg-' || lpad(n::text, 5, '0'),
               'pkg:pypi/pkg-' || lpad(n::text, 5, '0'), 'pypi', 'pkg:conda/pkg-' || lpad(n::text, 5, '0'),
               '[]', '[]', '[]', 'spike', 'pkg-' || lpad(n::text, 5, '0'), {_literal(scale.base)}, 'verified'
        FROM generate_series(1, {scale.packages}) AS n
        ORDER BY n
        """,
    )


def evidence_statements(scale: Scale) -> tuple[tuple[str, str], ...]:
    """Return one `INSERT ... SELECT` per roster table, honouring every `CheckConstraint`.

    Day-major and package-minor, as production inserts: every statement orders
    by `(day, package)`, so the oldest day is contiguous and lowest in the
    primary key and the purge's keyset walk meets it first.

    Args:
        scale: How much to seed.

    Returns:
        `(table, sql)` pairs in roster order; `kev_findings` is derived from the
        advisory rows it cites, so it comes after them.

    """
    observed = _observed(scale)
    present = _present(scale)
    days = f"generate_series(0, {scale.days - 1}) AS d(day)"
    source = "'https://example.test/' || p.canonical_name"
    return (
        (
            "inventory_snapshots",
            f"""
            INSERT INTO inventory_snapshots (observed_at, package_id, source_package_key, state,
                internal_component_count, internal_lob_count, apps, platforms, downloads, versions, detail, trace_id)
            SELECT {observed}, p.id, p.canonical_name || k.suffix, 'ok', 3, 1, 2, 3, 1000 + d.day, 4, '', ''
            FROM packages p CROSS JOIN {days} CROSS JOIN (VALUES ('-a'), ('-b')) AS k(suffix)
            WHERE {present}
            ORDER BY d.day, p.id, k.suffix
            """,
        ),
        (
            "source_release_snapshots",
            f"""
            INSERT INTO source_release_snapshots (observed_at, package_id, source, state, latest_version,
                released_at, last_activity_at, releases_seen, detail, trace_id)
            SELECT {observed}, p.id, {source}, 'ok', '1.' || d.day || '.0',
                   {observed} - interval '1 day', {observed} - interval '1 day', 10, '', ''
            FROM packages p CROSS JOIN {days}
            WHERE {present}
            ORDER BY d.day, p.id
            """,
        ),
        (
            "pypi_release_snapshots",
            f"""
            INSERT INTO pypi_release_snapshots (observed_at, package_id, source, state, latest_version,
                released_at, requires_python, detail, trace_id)
            SELECT {observed}, p.id, {source}, 'ok', '1.' || d.day || '.0', {observed} - interval '1 day',
                   '>=3.9', '', ''
            FROM packages p CROSS JOIN {days}
            WHERE {present}
            ORDER BY d.day, p.id
            """,
        ),
        (
            "feedstock_snapshots",
            f"""
            INSERT INTO feedstock_snapshots (observed_at, package_id, source, state, feedstock_name, feedstock_url,
                recipe_version, recipe_build_number, recipe_metadata_url, last_recipe_activity_at,
                staged_recipe_url, absence_established, detail, trace_id)
            SELECT {observed}, p.id, {source}, 'ok', p.canonical_name || '-feedstock', {source},
                   '1.' || d.day || '.0', 0, {source}, {observed} - interval '2 days', '', false, '', ''
            FROM packages p CROSS JOIN {days}
            WHERE {present}
            ORDER BY d.day, p.id
            """,
        ),
        (
            "conda_package_snapshots",
            f"""
            INSERT INTO conda_package_snapshots (observed_at, package_id, source, state, channel, platform,
                published_version, build_string, build_number, detail, trace_id)
            SELECT {observed}, p.id, {source}, 'ok', k.channel, 'noarch', '1.' || d.day || '.0', 'py_0', 0, '', ''
            FROM packages p CROSS JOIN {days} CROSS JOIN (VALUES ('conda-forge'), ('bioconda')) AS k(channel)
            WHERE {present}
            ORDER BY d.day, p.id, k.channel
            """,
        ),
        (
            "vulnerability_findings",
            f"""
            INSERT INTO vulnerability_findings (observed_at, package_id, source, state, advisory_id, severity,
                affected_range, fixed_range, matched_version, match_confidence, detail, trace_id)
            SELECT {observed}, p.id, {source}, 'matched', k.advisory, 'HIGH', '<1.0.0', '1.0.0', '0.9.0',
                   'exact-version', '', ''
            FROM packages p CROSS JOIN {days} CROSS JOIN (VALUES ('GHSA-spike-a'), ('GHSA-spike-b')) AS k(advisory)
            WHERE {present}
            ORDER BY d.day, p.id, k.advisory
            """,
        ),
        (
            "kev_findings",
            """
            INSERT INTO kev_findings (observed_at, package_id, vulnerability_finding_id, source, state,
                catalog_date_added, detail, trace_id)
            SELECT v.observed_at, v.package_id, v.id, 'https://www.cisa.gov/kev', 'listed',
                   v.observed_at - interval '30 days', '', ''
            FROM vulnerability_findings v
            ORDER BY v.id
            """,
        ),
        (
            "license_findings",
            f"""
            INSERT INTO license_findings (observed_at, package_id, source, state, channel, raw_license,
                normalized_license, detection_method, detail, trace_id)
            SELECT {observed}, p.id, {source}, 'normalized', k.channel, 'MIT', 'MIT', 'spdx-identifier', '', ''
            FROM packages p CROSS JOIN {days} CROSS JOIN (VALUES ('conda-forge'), ('bioconda')) AS k(channel)
            WHERE {present}
            ORDER BY d.day, p.id, k.channel
            """,
        ),
        (
            "python_readiness_assessments",
            f"""
            INSERT INTO python_readiness_assessments (observed_at, package_id, source, state, python_series,
                requires_python, matching_classifier, deciding_signal, detail, trace_id)
            SELECT {observed}, p.id, {source}, 'inferred_compatible', k.series, '>=3.9',
                   'Programming Language :: Python :: ' || k.series, 'requires-python', '', ''
            FROM packages p CROSS JOIN {days} CROSS JOIN (VALUES ('3.14'), ('3.13')) AS k(series)
            WHERE {present}
            ORDER BY d.day, p.id, k.series
            """,
        ),
        (
            "python_verification_results",
            f"""
            INSERT INTO python_verification_results (observed_at, package_id, source, state, python_series,
                platform, architecture, log_reference, detail, trace_id)
            SELECT {observed}, p.id, {source}, 'verified_compatible', k.series, 'linux', 'x86_64',
                   'https://logs.example.test/' || p.id || '/' || d.day || '/' || k.series, '', ''
            FROM packages p CROSS JOIN {days} CROSS JOIN (VALUES ('3.14'), ('3.13')) AS k(series)
            WHERE {present}
            ORDER BY d.day, p.id, k.series
            """,
        ),
        (
            "identity_resolution_snapshots",
            f"""
            INSERT INTO identity_resolution_snapshots (observed_at, package_id, source, state, repository_url,
                repository_key, pypi_asked, pypi_found, pypi_source, feedstocks, confidence_recorded,
                downgrade_refused, detail, trace_id)
            SELECT {observed}, p.id, {source}, 'ok', p.source_repository_url, 'example/' || p.canonical_name,
                   true, true, 'https://pypi.org/project/' || p.canonical_name, '[]', 'verified', false, '', ''
            FROM packages p CROSS JOIN {days}
            WHERE {present}
            ORDER BY d.day, p.id
            """,
        ),
    )


def seed_ledger(scale: Scale, collectors: tuple[str, ...]) -> None:
    """One finished run per present package-day per swept collector, one dispatch per collector per day, stale rows."""
    names = ", ".join(f"('{name}')" for name in collectors)
    days = f"generate_series(0, {scale.days - 1}) AS d(day)"
    observed = _observed(scale)
    _timed(
        "collection_runs (per package)",
        f"""
        INSERT INTO collection_runs (started_at, finished_at, status, trace_id, detail, collector, package_id)
        SELECT {observed} - interval '5 minutes', {observed}, 'succeeded', '', '', c.name, p.id
        FROM packages p CROSS JOIN {days} CROSS JOIN (VALUES {names}) AS c(name)
        WHERE {_present(scale)}
        ORDER BY d.day, p.id, c.name
        """,
    )
    _timed(
        "collection_runs (dispatches)",
        f"""
        INSERT INTO collection_runs (started_at, finished_at, status, trace_id, detail, collector, package_id)
        SELECT {_literal(scale.base)} + make_interval(days => d.day),
               {_literal(scale.base)} + make_interval(days => d.day, mins => 1), 'succeeded', '', '', c.name, NULL
        FROM {days} CROSS JOIN (VALUES {names}) AS c(name)
        ORDER BY d.day, c.name
        """,
    )
    # A killed worker's rows: one per collector, started on day 0 and never
    # finalized. They are what the purge's `unfinished` leg removes and what
    # bounds `choose_evidence_cutoff` until it does.
    _timed(
        "collection_runs (stale, unfinished)",
        f"""
        INSERT INTO collection_runs (started_at, finished_at, status, trace_id, detail, collector, package_id)
        SELECT {_literal(scale.base + timedelta(hours=12))}, NULL, 'running', '', '', c.name, NULL
        FROM (VALUES {names}) AS c(name)
        """,
    )


def seed_policy_runs(scale: Scale) -> None:
    """Finished policy runs, one per day but the newest, each reading as of the end of its day."""
    rows = ", ".join(
        f"({_literal(started)}, {_literal(finished)}, 'succeeded', '', '', '{POLICY_VERSION}', {_literal(cutoff)})"
        for started, finished, cutoff in (_run_instants(scale, day) for day in range(scale.policy_runs))
    )
    _timed(
        "policy_runs",
        f"""
        INSERT INTO policy_runs (started_at, finished_at, status, trace_id, detail, policy_version, evidence_cutoff)
        VALUES {rows}
        """,
    )


def seed_rollup_and_derived(scale: Scale) -> None:
    """The rollup for every package from the newest run, and every run's derived rows.

    Every derived row cites the evidence row its run read -- that day's row for
    its package under the first key -- where the package has one; an absent
    package's later runs get the sentinel row the pass writes when it read
    nothing, with no citation, as the constraints require. `ORDER BY (run,
    package)` so each run's rows are contiguous, as a run writes them.
    """
    _started, _finished, pinned = _run_instants(scale, scale.policy_runs - 1)
    _timed(
        "package_health",
        f"""
        INSERT INTO package_health (package_id, policy_run_id, computed_at, evidence_cutoff, confidence,
            policy_versions, currency_status, feedstock_presence_status, priority_status, work_type_status)
        SELECT p.id, r.id, r.finished_at, r.evidence_cutoff, 'verified', '{{}}',
               'current', 'present_and_maintained', 'p3', 'update_feedstock'
        FROM packages p CROSS JOIN (SELECT id, finished_at, evidence_cutoff FROM policy_runs
                                    WHERE evidence_cutoff = {_literal(pinned)}) AS r
        ORDER BY p.id
        """,
    )
    # The evidence row run `r` read for package `p`: that day's row, whose day
    # begins twenty-three hours before the run's cut-off.
    at = "r.evidence_cutoff - interval '23 hours' + make_interval(hours => (p.id % 24)::int)"
    runs = "packages p CROSS JOIN policy_runs r"
    _timed(
        "package_currency (every run)",
        f"""
        INSERT INTO package_currency (package_id, policy_run_id, source_status, pypi_status, feedstock_status,
            conda_package_status, overall_status, detail, chosen_authority, authority_order,
            authority_order_source, source_snapshot_id, pypi_snapshot_id, feedstock_snapshot_id,
            conda_package_snapshot_id)
        SELECT p.id, r.id,
               CASE WHEN e.id IS NULL THEN 'unknown' ELSE 'current' END,
               CASE WHEN e.id IS NULL THEN 'unknown' ELSE 'current' END,
               CASE WHEN e.id IS NULL THEN 'unknown' ELSE 'current' END,
               CASE WHEN e.id IS NULL THEN 'unknown' ELSE 'current' END,
               CASE WHEN e.id IS NULL THEN 'unknown' ELSE 'current' END,
               '', CASE WHEN e.id IS NULL THEN '' ELSE 'pypi' END, '[]', 'default', NULL, e.id, NULL, NULL
        FROM {runs}
        LEFT JOIN pypi_release_snapshots e ON e.package_id = p.id AND e.observed_at = {at}
        ORDER BY r.id, p.id
        """,
    )
    _timed(
        "package_feedstock_presence (every run)",
        f"""
        INSERT INTO package_feedstock_presence (package_id, policy_run_id, presence_status, inactivity_threshold,
            last_recipe_activity_at, activity_age, confidence, feedstock_snapshot_id, detail)
        SELECT p.id, r.id, CASE WHEN e.id IS NULL THEN 'unknown' ELSE 'present_and_maintained' END,
               interval '180 days', e.last_recipe_activity_at,
               CASE WHEN e.id IS NULL THEN NULL ELSE interval '2 days' END, 'verified', e.id, ''
        FROM {runs}
        LEFT JOIN feedstock_snapshots e ON e.package_id = p.id AND e.observed_at = {at}
        ORDER BY r.id, p.id
        """,
    )
    _timed(
        "package_vulnerability (every run)",
        f"""
        INSERT INTO package_vulnerability (package_id, policy_run_id, vulnerability_status, kev_membership,
            risk_level, policy_version, evidence_cutoff, vulnerability_finding_id, kev_finding_id, detail)
        SELECT p.id, r.id, CASE WHEN v.id IS NULL THEN 'unknown' ELSE 'advisories_matched' END,
               CASE WHEN k.id IS NULL THEN 'not_established' ELSE 'listed' END,
               CASE WHEN v.id IS NULL THEN '' ELSE 'high' END, '{POLICY_VERSION}', r.evidence_cutoff, v.id, k.id, ''
        FROM {runs}
        LEFT JOIN vulnerability_findings v
               ON v.package_id = p.id AND v.observed_at = {at} AND v.advisory_id = 'GHSA-spike-a'
        LEFT JOIN kev_findings k ON k.vulnerability_finding_id = v.id
        ORDER BY r.id, p.id
        """,
    )
    _timed(
        "package_license (every run)",
        f"""
        INSERT INTO package_license (package_id, policy_run_id, license_outcome, matched_rule, policy_version,
            evidence_cutoff, license_finding_id, detail)
        SELECT p.id, r.id, CASE WHEN e.id IS NULL THEN 'unknown' ELSE 'allowed' END,
               CASE WHEN e.id IS NULL THEN '' ELSE 'MIT' END, '{POLICY_VERSION}', r.evidence_cutoff, e.id, ''
        FROM {runs}
        LEFT JOIN license_findings e ON e.package_id = p.id AND e.observed_at = {at} AND e.channel = 'conda-forge'
        ORDER BY r.id, p.id
        """,
    )
    _timed(
        "package_remediation (every run)",
        f"""
        INSERT INTO package_remediation (package_id, policy_run_id, readiness_status, source_fix, pypi_fix,
            feedstock_fix, conda_package_fix, fixed_version, evidence_stale, policy_version, evidence_cutoff,
            vulnerability_finding_id, source_snapshot_id, pypi_snapshot_id, feedstock_snapshot_id,
            conda_package_snapshot_id, detail)
        SELECT p.id, r.id, 'unknown', 'not_read', 'not_read', 'not_read', 'not_read', '', false,
               '{POLICY_VERSION}', r.evidence_cutoff, NULL, NULL, NULL, NULL, NULL, ''
        FROM {runs}
        ORDER BY r.id, p.id
        """,
    )
    _timed(
        "package_python_readiness (every run)",
        f"""
        INSERT INTO package_python_readiness (package_id, policy_run_id, readiness, evidence_type, python_series,
            assessment_id, verification_id, evidence_stale, policy_version, evidence_cutoff, detail)
        SELECT p.id, r.id, CASE WHEN e.id IS NULL THEN 'unknown' ELSE 'inferred_ready' END,
               CASE WHEN e.id IS NULL THEN 'none' ELSE 'inferred' END, '3.14', e.id, NULL, false,
               '{POLICY_VERSION}', r.evidence_cutoff, ''
        FROM {runs}
        LEFT JOIN python_readiness_assessments e
               ON e.package_id = p.id AND e.observed_at = {at} AND e.python_series = '3.14'
        ORDER BY r.id, p.id
        """,
    )
    _timed(
        "package_priority (every run)",
        f"""
        INSERT INTO package_priority (package_id, policy_run_id, bucket, bucket_description, matched_rule, reason,
            score, policy_version, evidence_cutoff, detail)
        SELECT p.id, r.id, 'p3', 'a spike bucket', 'spike', 'seeded', 50, '{POLICY_VERSION}', r.evidence_cutoff, ''
        FROM {runs}
        ORDER BY r.id, p.id
        """,
    )
    _timed(
        "package_work_type (every run)",
        f"""
        INSERT INTO package_work_type (package_id, policy_run_id, work_type, detail, policy_version, evidence_cutoff)
        SELECT p.id, r.id, 'update_feedstock', '', '{POLICY_VERSION}', r.evidence_cutoff
        FROM {runs}
        ORDER BY r.id, p.id
        """,
    )


#: Where a derived or evidence row's state requires a citation: `(table, state
#: column, states, cited column)`. Asserted after seeding, so a seed the
#: constraints let through with a `NULL` where a real pass writes a key --
#: which no constraint refuses for `kev_findings` in every state -- is caught.
REQUIRED_CITATIONS: Final[tuple[tuple[str, str, tuple[str, ...], str], ...]] = (
    ("kev_findings", "state", ("listed", "not_listed"), "vulnerability_finding_id"),
    ("package_currency", "chosen_authority", ("pypi",), "pypi_snapshot_id"),
    ("package_feedstock_presence", "presence_status", ("present_and_maintained",), "feedstock_snapshot_id"),
    ("package_vulnerability", "vulnerability_status", ("advisories_matched",), "vulnerability_finding_id"),
    ("package_vulnerability", "kev_membership", ("listed",), "kev_finding_id"),
    ("package_license", "license_outcome", ("allowed",), "license_finding_id"),
    ("package_python_readiness", "readiness", ("inferred_ready",), "assessment_id"),
)


def uncited_rows() -> dict[str, int]:
    """Return, per required citation, how many rows carry the state and no citation; empty when the seed is whole."""
    found: dict[str, int] = {}
    with connection.cursor() as cursor:
        for table, column, states, cited in REQUIRED_CITATIONS:
            values = ", ".join(f"'{state}'" for state in states)
            cursor.execute(f"SELECT count(*) FROM {table} WHERE {column} IN ({values}) AND {cited} IS NULL")
            missing = int(cursor.fetchone()[0])
            if missing:
                found[f"{table}.{cited} where {column} in {states}"] = missing
    return found


def seed_everything(scale: Scale, collectors: tuple[str, ...]) -> None:
    """Seed every table at the given scale, in dependency order, without vacuuming.

    Args:
        scale: How much to seed.
        collectors: The swept collectors the ledger is seeded with.

    """
    require_postgresql_17()
    require_empty_database()
    seed_packages(scale)
    for table, sql in evidence_statements(scale):
        _timed(table, sql)
    seed_ledger(scale, collectors)
    seed_policy_runs(scale)
    seed_rollup_and_derived(scale)


def expected_counts(scale: Scale, collectors: int) -> dict[str, int]:
    """Return what every seeded table must hold, by the scale rule."""
    return {
        "packages": scale.packages,
        **{entry.table: scale.evidence_rows(entry.table) for entry in EVIDENCE_ROSTER},
        "collection_runs": scale.ledger_rows(collectors),
        "policy_runs": scale.policy_runs,
        "package_health": scale.packages,
        **dict.fromkeys(SEEDED_DERIVED_TABLES, scale.packages * scale.policy_runs),
    }


def _vacuum_analyze(tables: tuple[str, ...]) -> None:
    """`VACUUM (ANALYZE)` every seeded table, so the planner sees the statistics a live table has.

    `VACUUM` as well as `ANALYZE`, and said: a bulk-loaded table has no
    visibility map until a vacuum builds one, and without it the planner can
    never choose an index-only scan -- which is what autovacuum gives every
    production table between one night and the next.
    """
    started = time.perf_counter()
    with connection.cursor() as cursor:
        for table in tables:
            cursor.execute(f"VACUUM (ANALYZE) {table}")
    _say(f"  vacuum (analyze) over {len(tables)} tables {time.perf_counter() - started:8.1f} s")


def _set_statement_timeout() -> None:
    """Bound every statement of this connection, so a regression fails in minutes rather than hanging."""
    with connection.cursor() as cursor:
        cursor.execute(f"SET statement_timeout = '{STATEMENT_TIMEOUT}'")


def _environment() -> list[str]:
    """Return the lines that say what machine and what server produced the numbers."""
    lines = [f"python {platform.python_version()} on {platform.system()} {platform.release()} ({platform.machine()})"]
    sysctl = shutil.which("sysctl")
    if sysctl is not None:
        for key in ("machdep.cpu.brand_string", "hw.ncpu", "hw.memsize"):
            probe = subprocess.run([sysctl, "-n", key], capture_output=True, text=True, check=False)  # noqa: S603 - a fixed binary, fixed arguments
            if probe.returncode == 0:
                lines.append(f"host {key} = {probe.stdout.strip()}")
    docker = shutil.which("docker")
    if docker is not None:
        probe = subprocess.run(  # noqa: S603 - a fixed binary, fixed arguments
            [docker, "info", "--format", "NCPU={{.NCPU}} MemTotal={{.MemTotal}} ServerVersion={{.ServerVersion}}"],
            capture_output=True,
            text=True,
            check=False,
        )
        if probe.returncode == 0:
            lines.append(f"docker {probe.stdout.strip()}")
    with connection.cursor() as cursor:
        for setting in PLANNER_SETTINGS:
            cursor.execute(f"SHOW {setting}")
            lines.append(f"postgresql {setting} = {cursor.fetchone()[0]}")
    return lines


@dataclass(frozen=True)
class Seed:
    """What the seed established, for the measurements to compare against.

    Attributes:
        scale: How much was seeded.
        collectors: The swept collector names the ledger was seeded with.
        counts: Row count per table after seeding.
        cutoff: The retention cut-off `purge_evidence` will draw.
        pinned_cutoff: The newest policy run's `evidence_cutoff`.

    """

    scale: Scale
    collectors: tuple[str, ...]
    counts: dict[str, int]
    cutoff: datetime
    pinned_cutoff: datetime


@pytest.fixture(scope="module")
def seed(django_db_setup: None, django_db_blocker: Any) -> Iterator[Seed]:
    """Seed the scale once for the module, with the database unblocked for every test after it.

    A module fixture rather than `django_db(transaction=True)` on each test:
    the transactional marker flushes the database after every test, and the
    seed is the expensive part. The `unblock()` stays open across the yield so
    the measuring tests read what it seeded without a marker of their own; only
    the purge test carries the transactional marker, because it is what makes
    pytest-django create the test database at all (the aliases to set up are
    read off the session's `django_db` markers) and because its batches must
    commit one at a time, as the nightly purge's do.

    Args:
        django_db_setup: pytest-django's test database, created and migrated.
        django_db_blocker: pytest-django's access guard.

    Yields:
        What was seeded.

    """
    del django_db_setup
    with django_db_blocker.unblock():
        require_postgresql_17()
        _set_statement_timeout()
        collectors = tuple(collector.name for collector in swept_collectors())
        _say("")
        _say("environment:")
        for line in _environment():
            _say(f"  {line}")
        _say("")
        _say(f"seeding {SCALE.packages} packages x {SCALE.days} days on postgres {connection.pg_version}")
        started = time.perf_counter()
        seed_everything(SCALE, collectors)
        tables = seeded_tables()
        _vacuum_analyze(tables)
        counts = {table: count_rows(table) for table in tables}
        _say(f"  seeding took {time.perf_counter() - started:8.1f} s in total")
        _say("")
        _say("  row counts after seeding:")
        for table, count in counts.items():
            _say(f"    {table:<32s} {count:>12,d}")
        yield Seed(
            scale=SCALE,
            collectors=collectors,
            counts=counts,
            cutoff=retention_cutoff(now=NOW, days=RETENTION_DAYS),
            pinned_cutoff=_run_instants(SCALE, PINNED_RUN_DAY)[2],
        )


# ---------------------------------------------------------------------------
# The measurement.
# ---------------------------------------------------------------------------


@dataclass
class Plan:
    """One measured statement and what `EXPLAIN (ANALYZE, BUFFERS)` said about it.

    Attributes:
        name: The query, as the story names it.
        sql: The statement Django issued, parameters bound.
        text: The plan, verbatim.
        execution_ms: The plan's `Execution Time`.
        planning_ms: The plan's `Planning Time`.
        top_node: The plan's first line.
        buffers: The first `Buffers:` line, which is the whole statement's.
        shared_buffers: The statement's shared buffers, hit plus read.
        seq_scans: The guarded tables the plan sequentially scans.

    """

    name: str
    sql: str
    text: str
    execution_ms: float
    planning_ms: float
    top_node: str
    buffers: str
    shared_buffers: int
    seq_scans: tuple[str, ...]

    @property
    def base_name(self) -> str:
        """Return the name without the `#n` a multi-statement call appends."""
        return self.name.split(" #")[0]


@dataclass
class Measurements:
    """Every plan the module produced, in order, shared across the tests."""

    plans: list[Plan] = field(default_factory=list)
    wall_clock: dict[str, float] = field(default_factory=dict)
    removed_before_purge: dict[str, int] = field(default_factory=dict)

    def named(self, prefix: str) -> list[Plan]:
        """Return the plans whose name starts with `prefix`."""
        return [plan for plan in self.plans if plan.name.startswith(prefix)]


@pytest.fixture(scope="module")
def measurements() -> Measurements:
    """The module's plans, accumulated by the tests and printed by the last."""
    return Measurements()


def _explain(name: str, sql: str) -> Plan:
    """Run `EXPLAIN (ANALYZE, BUFFERS)` on one statement and parse what matters.

    Args:
        name: The story's name for the query.
        sql: A `SELECT`, parameters already bound.

    Returns:
        The plan. A plan the parser cannot read fails here, naming the query,
        rather than becoming a `NaN` the summary would print as a number.

    """
    assert sql.lstrip().upper().startswith("SELECT"), f"{name}: only SELECT statements are explained here: {sql[:80]}"
    with connection.cursor() as cursor:
        cursor.execute(f"EXPLAIN (ANALYZE, BUFFERS) {sql}")
        text = "\n".join(str(row[0]) for row in cursor.fetchall())
    execution = _EXECUTION_TIME.search(text)
    planning = _PLANNING_TIME.search(text)
    buffers = _BUFFERS.search(text)
    assert execution is not None, f"{name}: no `Execution Time` in the plan:\n{text}"
    assert planning is not None, f"{name}: no `Planning Time` in the plan:\n{text}"
    assert buffers is not None, f"{name}: no `Buffers:` line in the plan:\n{text}"
    hit = _SHARED_HIT.search(buffers.group(1))
    read = _SHARED_READ.search(buffers.group(1))
    return Plan(
        name=name,
        sql=sql,
        text=text,
        execution_ms=float(execution.group(1)),
        planning_ms=float(planning.group(1)),
        top_node=text.splitlines()[0].strip(),
        buffers=buffers.group(1),
        shared_buffers=(int(hit.group(1)) if hit else 0) + (int(read.group(1)) if read else 0),
        seq_scans=tuple(sorted({table for table in _SEQ_SCAN.findall(text) if table in GUARDED_TABLES})),
    )


def _measure(
    measurements: Measurements,
    name: str,
    call: Callable[[], object],
    *,
    may_issue_nothing: bool = False,
) -> list[Plan]:
    """Run one product call, capture every statement it issued, and explain each.

    Args:
        measurements: Where the plans accumulate.
        name: The story's name for the call; a call issuing several statements
            gets `name #n` per statement.
        call: The product code.
        may_issue_nothing: Whether a call that touched the database not at all
            is an answer rather than a broken measurement -- the digest asks
            nothing about a collector that selects no package.

    Returns:
        The plans, in issue order.

    """
    started = time.perf_counter()
    with CaptureQueriesContext(connection) as captured:
        call()
    measurements.wall_clock[name] = time.perf_counter() - started
    statements = [str(query["sql"]) for query in captured.captured_queries]
    if not statements and may_issue_nothing:
        _say(f"  {name}: issued no statement (the collector selects no package here)")
        return []
    assert statements, f"{name} issued no statement"
    plans = [
        _explain(name if len(statements) == 1 else f"{name} #{index}", sql) for index, sql in enumerate(statements, 1)
    ]
    measurements.plans.extend(plans)
    return plans


def _package() -> Package:
    """Return the package every per-package read is measured on."""
    return Package.objects.get(canonical_name=MEASURED_PACKAGE)


def _first_batch(select: Callable[[], Any]) -> list[int]:
    """Return the first batch the purge would take, through the purge's own keyset walk."""
    return next(retention._batches(select, batch_size=retention.DEFAULT_BATCH_SIZE), [])  # noqa: SLF001 - the product's own walk


@pytest.mark.django_db
def test_the_runtime_is_the_postgresql_the_verdict_is_about(seed: Seed) -> None:
    """The verdict is a statement about PostgreSQL 17; the fixture refused anything else, and this says so."""
    require_postgresql_17()
    assert connection.vendor == "postgresql"
    assert seed.collectors


@pytest.mark.django_db
def test_every_table_holds_exactly_the_rows_the_scale_rule_says(seed: Seed) -> None:
    """`keys x present package-days` per roster table; a `CheckConstraint` refusing the seed would show here."""
    expected = expected_counts(seed.scale, len(seed.collectors))
    wrong = {table: (seed.counts[table], count) for table, count in expected.items() if seed.counts[table] != count}
    assert wrong == {}, f"seeded counts off the scale rule, as (found, expected): {wrong}"
    assert set(seed.counts) == set(expected)
    assert seed.scale.absent > 0, "the keep path needs absent packages; raise SPIKE_SCALE_PACKAGES past the modulus"
    assert uncited_rows() == {}
    seeded = {str(model._meta.db_table) for model, _relation in derived_tables()}  # noqa: SLF001 - the schema name
    assert seeded == SEEDED_DERIVED_TABLES, "a derived table joined or left the registry; teach the seeder about it"


@pytest.mark.django_db
def test_the_rollup_reads_for_one_package(seed: Seed, measurements: Measurements) -> None:
    """Every per-package read a policy run makes, on every roster table, and the cut-off choice."""
    package = _package()
    cutoff = seed.pinned_cutoff

    # The generic shape, on every roster table: the newest row at or before the cut-off.
    for entry in EVIDENCE_ROSTER:
        model = entry.model
        _measure(
            measurements,
            f"rollup.snapshot_as_of shape: {entry.table}",
            lambda model=model: (
                model.objects.filter(package_id=package.pk, observed_at__lte=cutoff)
                .order_by("-observed_at", "-pk")
                .first()
            ),
        )
    _measure(
        measurements, "rollup.snapshot_as_of (inventory)", lambda: snapshot_as_of(package_id=package.pk, cutoff=cutoff)
    )
    for reader in currency.SURFACE_READERS:
        _measure(
            measurements,
            f"rollup.currency.observed_surface ranked: {reader.surface}",
            lambda reader=reader: currency.observed_surface(reader, package_id=package.pk, cutoff=cutoff),
        )
    _measure(
        measurements,
        "rollup.feedstock.observed_feedstock",
        lambda: feedstock.observed_feedstock(package_id=package.pk, cutoff=cutoff),
    )
    _measure(
        measurements,
        "rollup.py314.current_assessment (package, series)",
        lambda: py314_readiness.current_assessment(package_id=package.pk, cutoff=cutoff),
    )
    _measure(
        measurements,
        "rollup.py314.current_verification (package, series)",
        lambda: py314_readiness.current_verification(package_id=package.pk, cutoff=cutoff),
    )
    _measure(
        measurements,
        "rollup.vulnerability.current_findings",
        lambda: vulnerability.current_findings(package_id=package.pk, cutoff=cutoff),
    )
    _measure(
        measurements,
        "rollup.vulnerability.current_cross_references",
        lambda: vulnerability.current_cross_references(package_id=package.pk, cutoff=cutoff),
    )
    _measure(
        measurements,
        "rollup.licence.current_findings",
        lambda: licence.current_findings(package_id=package.pk, cutoff=cutoff),
    )
    _measure(
        measurements,
        "rollup.remediation.current_findings",
        lambda: remediation.current_findings(package_id=package.pk, cutoff=cutoff),
    )
    for reader in remediation.SURFACE_READERS:
        _measure(
            measurements,
            f"rollup.remediation.read_surface: {reader.surface}",
            lambda reader=reader: remediation.read_surface(reader, package_id=package.pk, cutoff=cutoff),
        )
    for entry in EVIDENCE_ROSTER:
        model = entry.model
        _measure(
            measurements,
            f"rollup.freshness.latest_observation: {entry.table}",
            lambda model=model: latest_observation(model, package_id=package.pk),
        )

    # The cut-off choice, through the product: its two statements on the
    # success path, and all three on the refusal path -- reached by opening a
    # run that started before every ending, so no ending is usable and the
    # `PolicyRunError` is worded with the count. The row is removed after.
    _measure(measurements, "rollup.choose_evidence_cutoff", choose_evidence_cutoff)
    blocker = CollectionRun.objects.create(
        started_at=seed.scale.base - timedelta(days=1),
        collector=seed.collectors[0],
        status="running",
    )
    try:

        def refused() -> None:
            with pytest.raises(PolicyRunError):
                choose_evidence_cutoff()

        _measure(measurements, "rollup.choose_evidence_cutoff (refusal path)", refused)
    finally:
        CollectionRun.objects.filter(pk=blocker.pk).delete()


@pytest.mark.django_db
def test_the_package_page_and_the_listing(seed: Seed, measurements: Measurements) -> None:
    """The package page's reads, the listing's first page, the coverage screen and the digest's freshness figures."""
    package = _package()
    row = PackageHealth.objects.get(package_id=package.pk)

    _measure(measurements, "page.traces_for", lambda: traces_for(row))
    _measure(measurements, "page.recent_runs", lambda: recent_runs(package))
    _measure(
        measurements, "page.runs_in_flight", lambda: runs_in_flight(package.pk, now=NOW, window=in_flight_window())
    )
    _measure(measurements, "page.last_recollection", lambda: last_recollection(package))

    listing = health_queryset({}, sort="")
    _measure(measurements, "listing.health_queryset count", listing.count)
    _measure(measurements, "listing.health_queryset first page", lambda: list(listing[:DEFAULT_PAGE_SIZE]))

    _measure(measurements, "coverage.collector_health: ledger group-by", lambda: collector_health(now=NOW))
    _measure(measurements, "coverage.coverage_of", coverage_of)

    # The digest's freshness figures, through the digest's own functions: the
    # selection `core.registry.selected_package_ids` reads, then the "ever
    # observed" and "observed within target" sets per collector.
    for collector in swept_collectors():
        scope, selection = digest._ask(collector)  # noqa: SLF001 - the digest's own reading of a collector
        if scope.freshness_target is None:
            continue
        _measure(
            measurements,
            f"digest._freshness: {collector.name}",
            lambda collector=collector, selection=selection: digest._freshness(collector, selection, now=NOW),  # noqa: SLF001 - as above
            may_issue_nothing=True,
        )
    assert seed.counts["packages"] == seed.scale.packages


@pytest.mark.django_db
def test_the_purge_selections(seed: Seed, measurements: Measurements) -> None:
    """The cut-off count, the first batch the purge's own walk takes (floor rule and full selection), dry-run counts."""
    cutoff = seed.cutoff
    _measure(measurements, "purge.purgeable_policy_runs", lambda: list(purgeable_policy_runs(cutoff=cutoff)))
    for entry in EVIDENCE_ROSTER:
        _measure(measurements, f"purge._older: {entry.table}", retention._older(entry, cutoff=cutoff))  # noqa: SLF001 - the product's own selection
        _measure(
            measurements,
            f"purge.floor_removable_evidence first batch: {entry.table}",
            lambda entry=entry: _first_batch(lambda: floor_removable_evidence(entry, cutoff=cutoff)),
        )
        _measure(
            measurements,
            f"purge.removable_evidence first batch: {entry.table}",
            lambda entry=entry: _first_batch(retention._removable(entry, cutoff=cutoff)),  # noqa: SLF001 - as above
        )
    # What `prune_evidence --dry-run` does per table: the unbatched count of
    # the full selection. Measured on the table whose selection is the costly
    # one, and on one single-key table for the ordinary case.
    for table in ("vulnerability_findings", "source_release_snapshots"):
        entry = next(entry for entry in EVIDENCE_ROSTER if entry.table == table)
        _measure(
            measurements,
            f"purge.dry-run count: {table}",
            lambda entry=entry: removable_evidence(entry, cutoff=cutoff).count(),
        )


@pytest.mark.django_db
def test_every_per_package_read_stays_within_the_buffer_ceiling(measurements: Measurements) -> None:
    """A per-package read that walks an index end to end still says `Index Scan`; the buffers say otherwise."""
    per_package = [
        plan
        for plan in measurements.named("rollup.") + measurements.named("page.")
        if plan.base_name not in WHOLE_TABLE_BY_NATURE
    ]
    assert per_package, "nothing per-package was measured"
    over = {plan.name: plan.buffers for plan in per_package if plan.shared_buffers > PER_PACKAGE_BUFFER_CEILING}
    assert over == {}, (
        f"these per-package reads touched more than {PER_PACKAGE_BUFFER_CEILING} shared buffers: {over}. "
        "A read that grows with the table is one the index no longer serves."
    )


def _unrecorded_scans(plan: Plan) -> tuple[str, ...]:
    """Return the guarded tables a plan sequentially scans that nothing records."""
    if plan.base_name in WHOLE_TABLE_BY_NATURE:
        return ()
    if plan.name.startswith(RECORDED_CITER_HASH_SELECTIONS):
        return tuple(table for table in plan.seq_scans if table not in RECORDED_CITER_HASH_TABLES)
    return plan.seq_scans


@pytest.mark.django_db
def test_no_measured_plan_sequentially_scans_a_guarded_table(measurements: Measurements) -> None:
    """The one assertion about plan shape: a Seq Scan on an evidence, ledger or derived table is a defect anywhere."""
    offenders = {plan.name: _unrecorded_scans(plan) for plan in measurements.plans if _unrecorded_scans(plan)}
    assert offenders == {}, (
        f"these product queries sequentially scan an evidence, ledger or derived table at {SCALE.packages} packages: "
        f"{offenders}. "
        "Either the read is not served by an index (declare one on the model, add its migration, re-run), or the "
        "query asks about every row (name it in WHOLE_TABLE_BY_NATURE), or no index can serve it (extend the "
        "recorded citer-hash shape and record it as deferred work) -- and record why in the story either way."
    )


class _Deadline:
    """Abandon the block after a number of seconds, with `SIGALRM`, naming what was running."""

    def __init__(self, seconds: int, what: str) -> None:
        self.seconds = seconds
        self.what = what

    def __enter__(self) -> None:
        def expired(_signum: int, _frame: object) -> None:
            message = f"{self.what} did not finish within {self.seconds} s"
            raise TimeoutError(message)

        signal.signal(signal.SIGALRM, expired)
        signal.alarm(self.seconds)

    def __exit__(self, *_exc: object) -> None:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, signal.SIG_DFL)


def _time_one_batch_deletion(seed: Seed, measurements: Measurements) -> None:
    """Remove one batch from three tables through the purge's own remover, and time each on its own.

    In the purge's own order, so each batch is uncited when its turn comes:
    first the purgeable run's `package_vulnerability` rows -- the one derived
    table citing both advisory tables -- every batch, as step 1 of the purge
    removes them; then one batch of `kev_findings`, then the
    `vulnerability_findings` rows it cited, then one single-key table. What
    went is subtracted from what the purge is expected to remove afterwards.
    """
    cutoff = seed.cutoff
    for model, relation in derived_tables():
        table = str(model._meta.db_table)  # noqa: SLF001 - the schema name
        if table != "package_vulnerability":
            continue
        leg = retention._Leg(  # noqa: SLF001 - the purge's own leg
            DELETED_LEG,
            model,
            retention._derived_rows_of_purgeable_runs(model, relation, cutoff=cutoff),  # noqa: SLF001 - as above
            retention._delete(model),  # noqa: SLF001 - as above
        )
        removed = 0
        started = time.perf_counter()
        for ids in retention._batches(leg.select, batch_size=retention.DEFAULT_BATCH_SIZE):  # noqa: SLF001 - as above
            went, protected = retention._remove_batch(leg, ids)  # noqa: SLF001 - as above
            assert protected == 0, (table, protected)
            removed += went
        measurements.wall_clock[f"purge.derived rows of the purgeable run deleted: {table}"] = (
            time.perf_counter() - started
        )
        measurements.removed_before_purge[table] = removed
    for table in ("kev_findings", "vulnerability_findings", "source_release_snapshots"):
        entry = next(entry for entry in EVIDENCE_ROSTER if entry.table == table)
        leg = retention._Leg(  # noqa: SLF001 - the purge's own leg
            DELETED_LEG,
            entry.model,
            retention._removable(entry, cutoff=cutoff),  # noqa: SLF001 - as above
            retention._retire(entry.model),  # noqa: SLF001 - as above
        )
        ids = _first_batch(leg.select)
        one_day = (seed.scale.packages - seed.scale.absent) * KEYS_PER_PACKAGE.get(table, 1)
        assert len(ids) == min(retention.DEFAULT_BATCH_SIZE, one_day), (table, len(ids))
        started = time.perf_counter()
        removed, protected = retention._remove_batch(leg, ids)  # noqa: SLF001 - as above
        measurements.wall_clock[f"purge.one batch deleted: {table}"] = time.perf_counter() - started
        assert (removed, protected) == (len(ids), 0), (table, removed, protected)
        measurements.removed_before_purge[table] = removed


def _purge_with_timings(monkeypatch: pytest.MonkeyPatch) -> tuple[tuple[TablePurge, ...], dict[str, float]]:
    """Run the steady-state purge end to end, timing every table's `TablePurge`."""
    timings: dict[str, float] = {}
    original = retention._purge_table  # noqa: SLF001 - timed around, never changed

    def timed(plan: Any, **kwargs: Any) -> TablePurge:
        started = time.perf_counter()
        result = original(plan, **kwargs)
        timings[result.table] = time.perf_counter() - started
        return result

    monkeypatch.setattr(retention, "_purge_table", timed)
    started = time.perf_counter()
    with _Deadline(PURGE_DEADLINE_SECONDS, "purge_evidence"):
        results = purge_evidence(clock=FixedClock(NOW), retention_days=RETENTION_DAYS)
    timings["(end to end)"] = time.perf_counter() - started
    return results, timings


@pytest.mark.django_db(transaction=True)
def test_the_steady_state_purge_removes_one_day_and_keeps_what_the_rule_keeps(
    seed: Seed,
    measurements: Measurements,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`purge_evidence` at the cut-off: one day's rows per roster table go; newest and read rows stay; timed."""
    cutoff = seed.cutoff
    scale = seed.scale
    before = {entry.table: seed.counts[entry.table] for entry in EVIDENCE_ROSTER}
    inside_before = {
        entry.table: entry.model.objects.filter(observed_at__gte=cutoff).count() for entry in EVIDENCE_ROSTER
    }
    absent_newest = {
        entry.table: entry.model.objects.filter(package_id__in=_absent_package_ids(), observed_at__lt=cutoff).count()
        for entry in EVIDENCE_ROSTER
    }

    _set_statement_timeout()
    _time_one_batch_deletion(seed, measurements)
    results, timings = _purge_with_timings(monkeypatch)

    _say("")
    _say("  deleted on their own, through the purge's remover, before the purge (seconds):")
    for name, seconds in measurements.wall_clock.items():
        if name.startswith(("purge.one batch deleted", "purge.derived rows")):
            _say(f"    {name:<70s} {seconds:>9.3f}")
    _say("")
    _say("  the steady-state purge, one table at a time:")
    _say(f"    {'table':<32s} {'deleted':>9s} {'kept':>6s} {'prot':>5s} {'seconds':>9s}  status")
    for result in results:
        status = "failed" if result.error else "ok"
        _say(
            f"    {result.table:<32s} {result.deleted:>9,d} {result.kept_by_rule:>6d} {result.protected:>5d} "
            f"{timings[result.table]:>9.2f}  {status} {result.error or ''}".rstrip(),
        )
    _say(f"    {'(end to end)':<32s} {'':>9s} {'':>6s} {'':>5s} {timings['(end to end)']:>9.2f}")
    _say("")

    by_table = {result.table: result for result in results}
    failed = {table: result.error for table, result in by_table.items() if result.error}
    assert failed == {}, failed

    keys = {entry.table: KEYS_PER_PACKAGE.get(entry.table, 1) for entry in EVIDENCE_ROSTER}
    for entry in EVIDENCE_ROSTER:
        table = entry.table
        result = by_table[table]
        # One day's rows go: the day-0 rows of every package that was seen
        # again, less the batch already removed above. The absent packages' day-0
        # rows are their newest and stay, counted as kept by rule.
        one_day = (scale.packages - scale.absent) * keys[table]
        kept = scale.absent * keys[table]
        assert result.deleted == one_day - measurements.removed_before_purge.get(table, 0), (table, result.detail)
        assert result.kept_by_rule == kept, (table, result.detail)
        assert result.kept_by_rule > 0, table
        assert result.protected == 0, (table, result.detail)
        assert entry.model.objects.filter(observed_at__lt=cutoff).count() == kept, table
        assert entry.model.objects.filter(observed_at__gte=cutoff).count() == inside_before[table], table
        assert entry.model.objects.count() == before[table] - one_day, table
        # Every package still has its newest row: the day-90 sweep of the
        # present packages is intact, and the absent packages' day-0 rows are.
        newest_day = entry.model.objects.filter(observed_at__gte=scale.base + timedelta(days=scale.days - 1)).count()
        assert newest_day == one_day, table
        still_absent = entry.model.objects.filter(package_id__in=_absent_package_ids(), observed_at__lt=cutoff).count()
        assert still_absent == absent_newest[table] == kept, table

    # The oldest run and its derived rows went; the pinned run and every run
    # inside the window stayed, with their rows.
    assert PolicyRun.objects.count() == scale.policy_runs - 1
    assert not PolicyRun.objects.filter(evidence_cutoff=_run_instants(scale, PURGED_RUN_DAY)[2]).exists()
    assert PolicyRun.objects.filter(evidence_cutoff=seed.pinned_cutoff).exists()
    for model, _relation in derived_tables():
        table = str(model._meta.db_table)  # noqa: SLF001 - the schema name
        assert model._default_manager.count() == scale.packages * (scale.policy_runs - 1), table  # noqa: SLF001 - Django's own accessor
        assert by_table[table].deleted == scale.packages - measurements.removed_before_purge.get(table, 0), table

    # The ledger: day 0's finished rows and the stale open rows went.
    ledger = by_table["collection_runs"]
    assert dict(ledger.removed) == {
        "finished": scale.packages * len(seed.collectors) + len(seed.collectors),
        "unfinished": len(seed.collectors),
    }, ledger.detail
    assert CollectionRun.objects.exclude(collector=PRUNE_COLLECTOR).unfinished().count() == 0

    measurements.plans.append(
        Plan(
            name="purge.purge_evidence end to end",
            sql="(not a statement: purge_evidence(clock=FixedClock(NOW), retention_days=90))",
            text="\n".join(f"{table}: {seconds:.2f} s" for table, seconds in timings.items()),
            execution_ms=timings["(end to end)"] * 1000,
            planning_ms=0.0,
            top_node=f"{len(results)} tables, {sum(result.deleted for result in results):,d} rows removed",
            buffers="",
            shared_buffers=0,
            seq_scans=(),
        ),
    )


def _absent_package_ids() -> list[int]:
    """Return the ids of the packages absent after `ABSENT_AFTER_DAY`."""
    return [pk for pk in Package.objects.order_by("pk").values_list("pk", flat=True) if pk % ABSENT_MODULUS == 0]


def test_print_every_plan_and_the_summary_table(measurements: Measurements) -> None:
    """Print the plans verbatim and the query -> plan node -> actual time -> buffers table the story records."""
    assert measurements.plans, "nothing was measured; the measuring tests run first, the purge second, this last"
    _say("")
    _say("=" * 100)
    _say("PLANS (verbatim)")
    _say("=" * 100)
    for plan in measurements.plans:
        _say("")
        _say(f"--- {plan.name}")
        _say(f"SQL: {plan.sql}")
        _say(plan.text)
    _say("")
    _say("=" * 100)
    _say("SUMMARY: query -> plan node -> actual time -> buffers")
    _say("=" * 100)
    _say(f"{'query':<66s} | {'top plan node':<52s} | {'exec ms':>10s} | {'plan ms':>8s} | buffers | seq scan")
    for plan in measurements.plans:
        seq = ",".join(plan.seq_scans) or "-"
        _say(
            f"{plan.name[:66]:<66s} | {plan.top_node[:52]:<52s} | {plan.execution_ms:>10.3f} | "
            f"{plan.planning_ms:>8.3f} | {plan.buffers} | {seq}",
        )
    _say("")
    _say("WALL CLOCK of each product call as issued (seconds, Django round trips included):")
    for name, seconds in measurements.wall_clock.items():
        _say(f"  {name:<66s} {seconds:>9.3f}")
    per_package = [plan for plan in measurements.named("rollup.") if plan.base_name not in WHOLE_TABLE_BY_NATURE]
    budget_ms = sum(plan.execution_ms for plan in per_package)
    _say("")
    _say(
        f"rollup read budget: {len(per_package)} per-package statements, {budget_ms:.3f} ms of execution for one "
        f"package; x {SCALE.packages:,d} packages = {budget_ms * SCALE.packages / 1000:.1f} s of database execution "
        "(CPM-AD-23: one transaction per package, N+1 by design)",
    )
    _say("=" * 100)
