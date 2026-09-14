---
title: 'CPM-OPERATE-S10: The evidence tables at ten thousand packages'
type: 'feature'
created: '2026-09-14'
status: 'done'
review_loop_iteration: 1
followup_review_recommended: true
warnings: [oversized]
deferred:
  - summary: >-
      The purge's batch selection walks the primary key with the cut-off as a filter and probes the surviving runs per candidate (fixed cost per batch, independent of batch size); a keyset on (observed_at, id) is the candidate to measure.
    evidence: |-
      Recorded plans: Index Scan using <table>_pkey, Filter observed_at < cutoff, SubPlan at loops=89000 over a Materialize of policy_runs; _batches' keyset is a product-query semantic this story does not change.
    location: >-
      src/django_apps/conda_sentinel/core/retention.py _batches
    severity: medium
  - summary: >-
      removable_evidence's anti-join hashes the citing side whole (kev_findings/vulnerability_findings for vulnerability_findings; three derived tables cited through nullable FKs) and the planner declines the FK index at a 1,000-vs-900,000 ratio; only a change to the citation keys could remove those scans.
    evidence: |-
      First-batch selections 11245 ms (vulnerability_findings), 1747/388/298 ms (derived citers) in the recorded run; named in the spike's RECORDED_SCANS.
    location: >-
      src/django_apps/conda_sentinel/core/retention.py _surviving_citers
    severity: medium
  - summary: >-
      The spike's plan-shape and purge assertions run only through pixi run spike-scale (Docker, ~7 minutes); the gate smoke-runs the seeder at 3x2 but never the measurements.
    evidence: |-
      tests/integration/django_apps/test_evidence_scale_seed.py covers the seed; nothing in ci runs EXPLAIN.
    location: >-
      tests/spikes/spike_evidence_scale.py
    severity: low
baseline_revision: '263bb63400546a3de6d42d4f77e983851b42f07b'
context:
  - _bmad-output/implementation-artifacts/epic-operate-context.md
  - _bmad-output/implementation-artifacts/stories/cpm-operate-s09-an-operator-digest.md
---

<intent-contract>

## Intent

**Problem:** Nothing has measured the evidence tables at CPM-NFR-1's scale (ten thousand
packages, ninety days, about nine million evidence rows plus a ledger of the same order); the
purge's floor rule, the rollup's per-package reads and the package page were designed against
tables under a million rows, and `operations.md` already claims their plans were measured.

**Approach:** A re-runnable spike against a throwaway PostgreSQL 17 seeds that scale with SQL,
runs `EXPLAIN (ANALYZE, BUFFERS)` on the product's own queries, asserts no sequential scan on an
evidence table, records every plan and timing verbatim in this story, adds only the indexes a
plan demands (model + constant + migration), re-measures, and records the partitioning verdict
-- not adopted with the numbers, or a proposed spine amendment appended to the change proposal,
never implemented here.

## Boundaries & Constraints

**Always:**
- **The spike:** `tests/spikes/spike_evidence_scale.py`, `pytestmark = [pytest.mark.spike,
  pytest.mark.integration]`, module docstring in `spike_django_storages_fitness.py`'s shape
  (what it proves, what it bounds, where the verdict is recorded, what retires it). It refuses
  with `pytest.fail` (never `pytest.skip`, which needs a `test_suite_policy.py` exemption) unless
  `connection.vendor == "postgresql"` and the server is major version 17, naming what it found.
  Run through `scripts/spike-scale.sh` (`gate-postgres.sh`'s shape: container `pg-spike`, port
  55433, `postgres://spike:spike@localhost:55433/spike`, `pg_isready` wait, always `docker rm -f`
  on exit) which sets `DATABASE_URL` and runs
  `pixi run -e dev pytest tests/spikes/spike_evidence_scale.py -m spike -s -p no:randomly`;
  pixi task `spike-scale = { cmd = "bash scripts/spike-scale.sh", default-environment = "dev",
  description = "CPM-OPERATE-S10: measure the evidence tables at ten thousand packages on postgres:17" }`.
  Never part of `ci`/`test-cov` (`python_files` does not match `spike_*.py`; `test_gate_contract.py`
  keeps holding that -- generalise its `SPIKE_*` constants to a roster of spikes if a second one
  cannot otherwise be admitted).
- **Scale and seeding:** `SCALE_PACKAGES = 10_000`, `SCALE_DAYS = 91` (ninety days inside the
  retention plus one day past it, so the purge has a real day to remove), one row per package per
  day in every table of `core/retention.py` `EVIDENCE_ROSTER` (eleven tables), `collection_runs`
  one finished row per package per per-package collector per day (nine collectors), ninety
  `policy_runs` (one per day, `evidence_cutoff` = that day) with `package_health` for every package
  from the newest run and each run's derived-table rows only where cheap. Seeded with raw SQL on
  `connection.cursor()` (`INSERT ... SELECT ... FROM generate_series(...)`), honouring every
  `CheckConstraint` the models declare (state `ok`/`matched`/`listed`/`normalized`/
  `inferred_compatible`/`verified_compatible` with the columns those states require; the
  multi-key tables get two keys per package -- two channels, two series, two advisories -- so the
  newest-per-key rule is exercised); `observed_at` spread hourly within the day so ties do not
  hide ordering. `ANALYZE` after seeding. Seeding prints per-table row counts and elapsed time; a
  count that is not `packages × days (× keys)` fails the spike.
- **What is measured**, each as the SQL the ORM actually issues (captured with
  `CaptureQueriesContext`, then `EXPLAIN (ANALYZE, BUFFERS)` on that statement with the same
  parameters), the plan text and wall-clock recorded verbatim in this story's `## Measurement`:
  1. rollup reads for one package on every roster table: the `snapshot_as_of` shape
     (`WHERE package_id = ? AND observed_at <= ? ORDER BY observed_at DESC, id DESC LIMIT 1`),
     `policies/currency.py`'s ranked variant, the `(package, python_series)` variant,
     `core/freshness.latest_observation`; and `choose_evidence_cutoff`'s three ledger queries;
     multiply the per-package cost by 10,000 to state the rollup's read budget;
  2. the package page: `surface/detail.py` `_trace` (the `[:20]` slice and the `count()`),
     `recent_runs`, `core/ledger.runs_in_flight`, `last_recollection`, `traces_for`;
     `surface/listing.health_queryset` first page; `surface/coverage.collector_health` and
     `coverage_of`; the digest's `values_list("package_id").distinct()` pair per collector;
  3. the purge: `core/retention._older` per table, `floor_removable_evidence` (the first batch's
     `values_list("pk")[:1000]` selection) per table, `purgeable_policy_runs`, and then
     `purge_evidence(cutoff=now - 90 days, dry_run=False)` end to end, timing every table's
     `TablePurge` -- the steady-state nightly purge (one day's rows).
- **Verdict rule (stated in the story before the numbers):** the nightly window is assumed to
  be one hour, because `operations.md` bounds the purge only by "do not overlap `policy-run`"
  and the deployment repository schedules it nightly; the purge is a management command with no
  Celery time limit. Partitioning is **not adopted** when the end-to-end steady-state purge
  finishes inside that hour with at least a 4× margin and no measured plan sequentially scans an
  evidence table after the indexes; otherwise the story appends `## 9. CPM-OPERATE-S10:
  monthly range partitioning by observed_at, proposed` to
  `_bmad-output/planning-artifacts/sprint-change-proposal-2026-09-13.md` in §8's style (what
  changes in `CPM-AD-2`'s door bullet, the purge as a partition drop, the migration audit's
  new obligation) and records "awaits acceptance" -- and implements nothing of it.
- **Indexes:** only where a measured plan shows a sequential scan on an evidence or ledger table
  on a product query, or a per-package read that is not served by an index; each is a
  `Final[str]` constant exported in the model module's `__all__`, declared in `Meta.indexes`,
  added by one migration per app (`collectors/0017_...`, `core/0014_...` in
  `0014_evidence_time_indexes.py`'s shape), re-measured, and the after-plan recorded next to the
  before-plan. Candidates the investigation flagged, to be confirmed or dismissed by the plans:
  composite key indexes on the multi-key tables (`license_findings (package, channel,
  -observed_at)`, `vulnerability_findings (package, advisory_id, -observed_at)`,
  `python_readiness_assessments`/`python_verification_results (package, python_series,
  -observed_at)`, `inventory_snapshots (package, source_package_key, -observed_at)`) and
  `collection_runs (package, -started_at)`. `test_migration_completeness`,
  `test_evidence_constraint_audit`, `test_retention.py`'s leading-`observed_at` rule and the
  registry stay green.
- **The spike asserts** (so a re-run is a regression check, not a printout): the seeded counts;
  that after the indexes no measured plan contains `Seq Scan on <evidence or ledger table>`
  except where the story records why (the coverage group-by over the whole ledger is allowed and
  said); that `purge_evidence` deleted exactly one day's rows per roster table and kept every
  package's newest row and every row a surviving run read; and it prints a table of
  query → plan node → actual time → buffers. Timings are printed and recorded, never asserted.
- **Docs:** `docs/conda-sentinel/operations.md` purge section gains the measured numbers and the
  verdict sentence (replace the unmeasured claim in "The door, and the indexes"), and a
  "### Measuring at scale" subsection naming `pixi run spike-scale`, what it seeds, how long it
  takes and that it needs Docker; `docs/accelerator/development.md`'s spike list gains this
  spike; `tests/unit/test_documentation_commands.py` and `test_documentation_accuracy.py` stay
  green. Deferred ledger: append anything the plans expose that this story does not fix (the
  per-package N+1 of the rollup is by design, `CPM-AD-23` -- record its measured budget, do not
  change it).
- Sprint status: `cpm-operate-s10-the-evidence-tables-at-ten-thousand-packages: done`.

**Block If:** seeding nine million rows cannot complete on this machine within thirty minutes
(record what was reached and halt rather than scale the numbers down silently); a plan can be
fixed only by changing a product query's semantics (record it as deferred instead).

**Never:** implement partitioning; change what the purge removes or the floor rule; seed
through the ORM; add an index no recorded plan justifies; invent or round a number that was not
printed by the spike; run the spike in the gate; start `pixi run local-stack`.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|---|---|---|---|
| Spike on SQLite | `pixi run -e dev pytest tests/spikes/... -m spike` without `DATABASE_URL` | fails naming the vendor found and `pixi run spike-scale` | `pytest.fail` |
| Spike on PostgreSQL 17 | `scripts/spike-scale.sh` | seeds, measures, asserts, prints the table, exit 0; container removed | N/A |
| Container never ready | Docker down or port busy | script exits non-zero naming the container; nothing seeded | script |
| Seeded count wrong | a CheckConstraint refused rows | spike fails naming the table and the count | assertion |
| Seq scan after indexes | a product query on an evidence table plans a Seq Scan | spike fails naming the query and table | assertion |
| Purge at scale | `purge_evidence(cutoff=now-90d)` | one day's rows gone per table, newest rows kept, timings printed | assertion |
| Verdict | purge inside the hour with 4× margin, no seq scan | "not adopted" recorded in story and docs; no amendment | N/A |
| Verdict, otherwise | purge too slow or an unfixable scan | §9 appended to the change proposal; story says "awaits acceptance" | N/A |

</intent-contract>

## Code Map

- Spike shape: `tests/spikes/spike_django_storages_fitness.py:1-79,:392-411` (docstring, `pytestmark`,
  runtime refusal); `tests/spikes/__init__.py`; `pyproject.toml:276-313` (markers, `python_files`, `--reuse-db`);
  `pixi.toml:244,:390,:858-859` (`spike-storage`), :760 `stack-run`, :820 `gate-postgres`;
  `scripts/gate-postgres.sh` (container, `pg_isready` loop, `DATABASE_URL`); `tests/unit/test_gate_contract.py:41-44,:205,:282,:315,:373-409`
  (SPIKE_* constants, every task pins an environment, spike dir rules); `tests/unit/test_suite_policy.py:165-181`
  (`pytest.skip` exemptions); `tests/unit/test_dependency_policy.py:1377`; `tests/unit/test_documentation_commands.py:50-71`.
- Roster and purge: `core/retention.py:204-238` constants, :433-461 `EVIDENCE_ROSTER`/`EXCLUDED_EVIDENCE`,
  :500-530 policy-run survivors, :546-597 `_keyed`/`floor_removable_evidence`, :611-655 citers/`removable_evidence`,
  :772-793 `_batches`, :888-953 `_purge_table`, :964-1003 `_delete`/`_retire`, :1040-1073 `_older`/ledger scans,
  :1177-1260 `purge_evidence`; door `core/models.py:290,:406`.
- Models/indexes/constraints: `collectors/models.py` (`InventorySnapshot` :856, `SourceReleaseSnapshot` :1070,
  `PyPIReleaseSnapshot` :1292, `FeedstockSnapshot` :1473, `CondaPackageSnapshot` :1760, `VulnerabilityFinding` :1967,
  `KevFinding` :2244, `LicenseFinding` :2541, `PythonReadinessAssessment` :2778, `PythonVerificationResult` :2983,
  `IdentityResolutionSnapshot` :3196; index constants :291-293,:320-321,:336-337,:370-371,:406-419,:514-515,:535-536,
  :605-606,:680-681,:757-758,:788-789; `__all__` :163-220); `core/models.py:904` `CollectionRun` (:210-211,:984-985),
  :1001 `PolicyRun`, :1061 `PackageHealth` (:1340-1341); `identity/models.py:555` `Package` (`packages`);
  `collectors/migrations/0014_evidence_time_indexes.py:20-23` (AddIndex shape); newest: collectors `0016`, core `0013`.
- Readers measured: `collectors/models.py:1017-1066` `snapshot_as_of`; `policies/currency.py:409-414`;
  `policies/feedstock.py:311-314`; `policies/py314_readiness.py:336-343,:374-381`; `policies/vulnerability.py:468-511`;
  `policies/licence.py:544-552`; `policies/remediation.py:747-755,:1039-1046`; `core/freshness.py:238`;
  `core/policy_run.py:201-266` `choose_evidence_cutoff`, :388-459 per-package loop (`CPM-AD-23`); `core/rollup.py:148-166,:262-272`.
- Surface: `surface/detail.py:128` `HISTORY_LIMIT`, :237 `TRACES`, :324-345 `traces_for`, :389-400 `_trace`, :513 `identity_of`,
  :582 `last_recollection`, :602 `recent_runs`; `core/ledger.py:129-155` `runs_in_flight`; `surface/listing.py:146-171`;
  `surface/health.py:302-333`; `surface/coverage.py:278-357`; `collectors/digest.py:1056-1058` distinct pair.
- Audits: `tests/unit/django_apps/test_migration_completeness.py:47-90`; `test_evidence_constraint_audit.py:274`;
  `test_evidence_inheritance_audit.py`; `tests/unit/django_apps/test_retention.py:127,:290,:341,:374-397`;
  `tests/unit/django_apps/test_mutation_path_audit.py:285` (cursor licence scope -- check it does not sweep `tests/spikes`).
- Plumbing: `src/config/settings/base.py:126-151` (`DATABASE_URL` → postgres, `ATOMIC_REQUESTS`); `pixi.toml:109` psycopg 3;
  `tests/conftest.py:634-667` (no custom `django_db_setup`; `no_network` not autouse); `.github/workflows/ci.yml:106`.
- Docs: `operations.md:3373-3505` (purge; "The door, and the indexes" :3489-3505 with the unmeasured claim at :3503-3505),
  scale mentions :2116-2118,:2152-2157; `docs/accelerator/development.md:31-32,:209-277` (spike prose);
  change proposal `sprint-change-proposal-2026-09-13.md` §8 :173 (style to copy for a §9); spine `ARCHITECTURE-SPINE.md:141-166` (`CPM-AD-2`, the door bullet).
- Read-only: every collector and policy pass, `core/policy_run.py`, `core/retention.py` semantics.

## Tasks & Acceptance

**Execution:**
- [x] `scripts/spike-scale.sh` -- container lifecycle, `DATABASE_URL`, pytest invocation -- one command, always cleans up.
- [x] `pixi.toml` -- `spike-scale` task (dev environment) -- `test_gate_contract` rules (environment pinned; not a gate step).
- [x] `tests/spikes/spike_evidence_scale.py` -- refusal, SQL seeding, `CaptureQueriesContext` + `EXPLAIN`, assertions, the printed table, the purge run.
- [x] `tests/unit/test_gate_contract.py` -- the second spike admitted by a test of its own (a script-backed task, on `gate-redis`'s shape); the storage spike's `SPIKE_*` constants stay single-valued because nothing about them needed to widen.
- [x] `core/models.py` + `core/0014_collection_run_package_index` -- the one index a plan demanded, `collection_runs (package, -started_at)` as `COLLECTION_RUN_PACKAGE_INDEX`, in `__all__`; no `collectors` index was demanded, so no `collectors/0017`.
- [x] `tests/unit/django_apps/test_retention.py` and any index-name pin -- the ledger pins are `<=`; nothing to reconcile. `tests/integration/django_apps/test_evidence_scale_seed.py` runs the seeder in the gate at 3 x 2.
- [x] This story: `## Measurement` (verbatim plans, timings, before/after), `## Verdict` (rule, numbers, decision).
- [x] `docs/conda-sentinel/operations.md`, `docs/accelerator/development.md` -- numbers, verdict, "Measuring at scale".
- [x] `sprint-change-proposal-2026-09-13.md` §9 -- not appended: the verdict does not propose partitioning.
- [x] `deferred-work.md` -- append what the plans exposed and this story left; `sprint-status.yaml` -- `done`.

**Acceptance Criteria:**
- Given Docker running, when `pixi run spike-scale` runs, then it seeds 10,000 packages × 91 days
  on every roster table, measures the listed queries, asserts no sequential scan on an evidence
  table after this story's indexes, purges one day and asserts what it kept, and exits 0 with the
  container removed.
- Given the story, when read, then every measured query has its before-plan, its after-plan
  where an index was added, its timing, and the verdict follows the stated rule from the numbers.
- Given `pixi run ci`, when it runs, then it exits 0 and `test_migration_completeness` is green.

## Measurement

Run on 2026-09-14 with `pixi run spike-scale` (`scripts/spike-scale.sh` → `postgres:17` in
container `pg-spike`, server version 170011; `pixi run -e dev pytest
tests/spikes/spike_evidence_scale.py -m spike -s`; the run is copied to
`.spike-runs/20260914T100410Z.log`). Everything below is what the spike printed, unedited,
from the recorded run (`9 passed in 447.32s`). One earlier run at the same scale, seeded the
same way but **before** the ledger index this story adds, is the source of exactly one thing
here: the before-plan of `page.recent_runs`, quoted where the index is decided. Two runs
before that, from a review draft that seeded package-major, cited two derived tables' rows
for two runs only and listed the digest's whole-table reads as such, are superseded and not
recorded.

### The environment

```text
environment:
  python 3.14.6 on Darwin 25.2.0 (arm64)
  host machdep.cpu.brand_string = Apple M4
  host hw.ncpu = 10
  host hw.memsize = 25769803776
  docker NCPU=10 MemTotal=14638108672 ServerVersion=29.6.1
  postgresql server_version = 17.11 (Debian 17.11-1.pgdg13+2)
  postgresql shared_buffers = 128MB
  postgresql work_mem = 4MB
  postgresql max_parallel_workers_per_gather = 2
  postgresql effective_cache_size = 4GB
  postgresql random_page_cost = 4
```

Docker Desktop's VM has the ten host cores and 14,638,108,672 bytes of memory; PostgreSQL
runs the image's defaults (`shared_buffers = 128MB`, `work_mem = 4MB`, two parallel workers
per gather), which is smaller than any deployment's database and is why the plans below
show `temp read=… written=…` on the one hash that does not fit `work_mem`. Every plan is
`EXPLAIN (ANALYZE, BUFFERS)`, which instruments each node; the first-batch selections run
once, against a cache the seed and `VACUUM (ANALYZE)` just wrote, so their `read=` counts
include cold pages and their times carry the instrumentation. A per-batch figure multiplied
by the number of batches is therefore an upper bound on a table's purge, not its total: the
purge's own per-table wall clock, below, is the total.

**The scale as seeded** -- `SCALE_PACKAGES = 10_000`, `SCALE_DAYS = 91`, one row per package
per day per reader key (two keys on each of the seven multi-key tables: `inventory_snapshots`,
`conda_package_snapshots`, `kev_findings`, `vulnerability_findings`, `license_findings`,
`python_readiness_assessments`, `python_verification_results`), one package in a hundred
absent after day 0 (observed that day and never again, on every roster table and the
ledger), nine swept collectors on the ledger, ninety policy runs (days 0..89,
`evidence_cutoff` at the end of each day; day 90's evidence is not yet evaluated, as between
a sweep and the night's run), the rollup from run 89, and **every run's rows on every
derived table**, citing that day's evidence row where the package has one and the pass's
sentinel row where it does not. Seeded day-major -- every statement orders by `(day,
package)` -- so the oldest day is contiguous and lowest in every primary key, as a ledger
written night by night is. `VACUUM (ANALYZE)` after seeding, so the planner sees the
statistics and the visibility map a live table has. Summing the printed counts: 16,218,000
evidence rows across the eleven roster tables, 8,109,828 ledger rows, 7,200,000 derived rows
-- not the "about nine million evidence rows plus a ledger of the same order" the
intent-contract estimated (the contract is read-only; the estimate under-counted the seven
two-key tables).

**What the seed is not, and in which direction.** One row per package per day on every
table is *pessimistic* for `feedstock_snapshots`, `python_readiness_assessments` (fourteen-day
targets) and `python_verification_results` (thirty days), which observe a package a few times
a window; those tables and their purges are over-stated here. One platform and two channels,
two advisories and two series per package is *optimistic* on key cardinality: an inventory
whose packages carry many advisories and platforms holds more newest-per-key rows and more
`SubPlan 2` probes per candidate than this. The `protected` path -- a citation committed
between the selection and the `DELETE` -- is not exercised; every table records
`protected=0`.

```text
seeding 10000 packages x 91 days on postgres 170011
  seeded packages                                                  0.1 s
  seeded inventory_snapshots                                      15.3 s
  seeded source_release_snapshots                                  6.2 s
  seeded pypi_release_snapshots                                    5.4 s
  seeded feedstock_snapshots                                       5.9 s
  seeded conda_package_snapshots                                  16.2 s
  seeded vulnerability_findings                                   12.1 s
  seeded kev_findings                                             16.6 s
  seeded license_findings                                         11.3 s
  seeded python_readiness_assessments                             11.3 s
  seeded python_verification_results                              12.2 s
  seeded identity_resolution_snapshots                             6.4 s
  seeded collection_runs (per package)                            53.1 s
  seeded collection_runs (dispatches)                              0.0 s
  seeded collection_runs (stale, unfinished)                       0.0 s
  seeded policy_runs                                               0.0 s
  seeded package_health                                            0.1 s
  seeded package_currency (every run)                             13.0 s
  seeded package_feedstock_presence (every run)                   11.0 s
  seeded package_vulnerability (every run)                        16.0 s
  seeded package_license (every run)                              10.7 s
  seeded package_remediation (every run)                          11.1 s
  seeded package_python_readiness (every run)                     12.7 s
  seeded package_priority (every run)                              7.3 s
  seeded package_work_type (every run)                             7.2 s
  vacuum (analyze) over 23 tables     10.2 s
  seeding took    272.4 s in total
  row counts after seeding:
    packages                               10,000
    inventory_snapshots                 1,802,000
    source_release_snapshots              901,000
    pypi_release_snapshots                901,000
    feedstock_snapshots                   901,000
    conda_package_snapshots             1,802,000
    kev_findings                        1,802,000
    vulnerability_findings              1,802,000
    license_findings                    1,802,000
    python_readiness_assessments        1,802,000
    python_verification_results         1,802,000
    identity_resolution_snapshots         901,000
    collection_runs                     8,109,828
    policy_runs                                90
    package_health                         10,000
    package_currency                      900,000
    package_feedstock_presence            900,000
    package_license                       900,000
    package_priority                      900,000
    package_python_readiness              900,000
    package_remediation                   900,000
    package_vulnerability                 900,000
    package_work_type                     900,000
...  digest._freshness: kev: issued no statement (the collector selects no package here)
  digest._freshness: vulnerability: issued no statement (the collector selects no package here)
  deleted on their own, through the purge's remover, before the purge (seconds):
    purge.derived rows of the purgeable run deleted: package_vulnerability     0.086
    purge.one batch deleted: kev_findings                                      0.039
    purge.one batch deleted: vulnerability_findings                            0.049
    purge.one batch deleted: source_release_snapshots                          0.044
```

**What is measured.** Every statement below is the SQL Django issued for the product call named,
captured with `CaptureQueriesContext` around the product's own function and then run through
`EXPLAIN (ANALYZE, BUFFERS)` with the same bound parameters; `#n` numbers the statements of a
call that issues several. The per-package reads are on package `pkg-05001` (id 5001) at run
89's cut-off; the purge selections at the retention cut-off `NOW - 90 days`; the first batch
of each selection is the one `retention._batches` itself yields.

### The steady-state purge, end to end

Before the purge, through the purge's own remover and in the purge's own order: every batch of
the purgeable run's `package_vulnerability` rows (step 1 of the purge for the one derived
table citing both advisory tables), then one batch of 1,000 from `kev_findings`, the 1,000
`vulnerability_findings` rows those cited, and 1,000 from `source_release_snapshots`, each
timed on its own. Then `purge_evidence(clock=FixedClock(2026-09-14T12:00Z), retention_days=90,
batch_size=1000)`, timed around every table's `TablePurge`; the counts show what those
pre-removed batches left it:

```text
  the steady-state purge, one table at a time:
    table                              deleted   kept  prot   seconds  status
    package_currency                    10,000      0     0      0.05  ok
    package_feedstock_presence          10,000      0     0      0.05  ok
    package_vulnerability                    0      0     0      0.00  ok
    package_license                     10,000      0     0      0.06  ok
    package_remediation                 10,000      0     0      0.08  ok
    package_python_readiness            10,000      0     0      0.09  ok
    package_priority                    10,000      0     0      0.12  ok
    package_work_type                   10,000      0     0      0.13  ok
    policy_runs                              1      0     0      0.02  ok
    inventory_snapshots                 19,800    200     0      4.21  ok
    source_release_snapshots             8,900    100     0      2.86  ok
    pypi_release_snapshots               9,900    100     0      2.97  ok
    feedstock_snapshots                  9,900    100     0     10.56  ok
    conda_package_snapshots             19,800    200     0      7.36  ok
    kev_findings                        18,800    200     0     17.51  ok
    vulnerability_findings              18,800    200     0     21.43  ok
    license_findings                    19,800    200     0      4.20  ok
    python_readiness_assessments        19,800    200     0      4.55  ok
    python_verification_results         19,800    200     0      4.98  ok
    identity_resolution_snapshots        9,900    100     0      1.62  ok
    collection_runs                     90,018      0     0      1.74  ok
    (end to end)                                                84.96

.
```

### Summary: query → plan node → actual time → buffers, and each call's wall clock

```text
====================================================================================================
SUMMARY: query -> plan node -> actual time -> buffers
====================================================================================================
query                                                              | top plan node                                        |    exec ms |  plan ms | buffers | seq scan
rollup.snapshot_as_of shape: inventory_snapshots                   | Limit  (cost=4.64..8.71 rows=1 width=65) (actual tim |      0.029 |    0.052 | shared hit=5 | -
rollup.snapshot_as_of shape: source_release_snapshots              | Limit  (cost=4.59..8.65 rows=1 width=86) (actual tim |      0.009 |    0.025 | shared hit=5 | -
rollup.snapshot_as_of shape: pypi_release_snapshots                | Limit  (cost=4.59..8.65 rows=1 width=80) (actual tim |      0.008 |    0.024 | shared hit=5 | -
rollup.snapshot_as_of shape: feedstock_snapshots                   | Limit  (cost=4.59..8.65 rows=1 width=162) (actual ti |      0.008 |    0.022 | shared hit=5 | -
rollup.snapshot_as_of shape: conda_package_snapshots               | Limit  (cost=4.64..8.71 rows=1 width=92) (actual tim |      0.010 |    0.027 | shared hit=5 | -
rollup.snapshot_as_of shape: kev_findings                          | Limit  (cost=4.64..8.70 rows=1 width=74) (actual tim |      0.009 |    0.023 | shared hit=5 | -
rollup.snapshot_as_of shape: vulnerability_findings                | Limit  (cost=4.64..8.71 rows=1 width=116) (actual ti |      0.009 |    0.022 | shared hit=5 | -
rollup.snapshot_as_of shape: license_findings                      | Limit  (cost=4.64..8.71 rows=1 width=102) (actual ti |      0.010 |    0.022 | shared hit=5 | -
rollup.snapshot_as_of shape: python_readiness_assessments          | Limit  (cost=4.64..8.71 rows=1 width=143) (actual ti |      0.017 |    0.021 | shared hit=5 | -
rollup.snapshot_as_of shape: python_verification_results           | Limit  (cost=4.64..8.70 rows=1 width=133) (actual ti |      0.008 |    0.020 | shared hit=5 | -
rollup.snapshot_as_of shape: identity_resolution_snapshots         | Limit  (cost=4.59..8.65 rows=1 width=167) (actual ti |      0.009 |    0.025 | shared hit=5 | -
rollup.snapshot_as_of (inventory)                                  | Limit  (cost=4.64..8.71 rows=1 width=65) (actual tim |      0.008 |    0.021 | shared hit=5 | -
rollup.currency.observed_surface ranked: source                    | Limit  (cost=4.59..8.66 rows=1 width=90) (actual tim |      0.011 |    0.029 | shared hit=5 | -
rollup.currency.observed_surface ranked: pypi                      | Limit  (cost=4.59..8.66 rows=1 width=84) (actual tim |      0.008 |    0.022 | shared hit=5 | -
rollup.currency.observed_surface ranked: feedstock                 | Limit  (cost=4.59..8.66 rows=1 width=166) (actual ti |      0.009 |    0.023 | shared hit=5 | -
rollup.currency.observed_surface ranked: conda_package             | Limit  (cost=4.65..8.71 rows=1 width=96) (actual tim |      0.011 |    0.030 | shared hit=5 | -
rollup.feedstock.observed_feedstock                                | Limit  (cost=4.59..8.65 rows=1 width=162) (actual ti |      0.008 |    0.022 | shared hit=5 | -
rollup.py314.current_assessment (package, series)                  | Limit  (cost=8.66..16.66 rows=1 width=143) (actual t |      0.008 |    0.023 | shared hit=5 | -
rollup.py314.current_verification (package, series)                | Limit  (cost=8.71..16.75 rows=1 width=133) (actual t |      0.008 |    0.023 | shared hit=5 | -
rollup.vulnerability.current_findings #1                           | Limit  (cost=4.64..8.71 rows=1 width=16) (actual tim |      0.011 |    0.019 | shared hit=5 | -
rollup.vulnerability.current_findings #2                           | Sort  (cost=8.46..8.46 rows=1 width=116) (actual tim |      0.006 |    0.015 | shared hit=4 | -
rollup.vulnerability.current_cross_references #1                   | Limit  (cost=4.64..8.70 rows=1 width=16) (actual tim |      0.007 |    0.019 | shared hit=5 | -
rollup.vulnerability.current_cross_references #2                   | Sort  (cost=8.46..8.46 rows=1 width=74) (actual time |      0.007 |    0.017 | shared hit=4 | -
rollup.licence.current_findings #1                                 | Limit  (cost=4.64..8.71 rows=1 width=16) (actual tim |      0.007 |    0.019 | shared hit=5 | -
rollup.licence.current_findings #2                                 | Sort  (cost=8.46..8.46 rows=1 width=102) (actual tim |      0.006 |    0.015 | shared hit=4 | -
rollup.remediation.current_findings #1                             | Limit  (cost=4.64..8.71 rows=1 width=16) (actual tim |      0.007 |    0.019 | shared hit=5 | -
rollup.remediation.current_findings #2                             | Sort  (cost=8.46..8.46 rows=1 width=116) (actual tim |      0.006 |    0.015 | shared hit=4 | -
rollup.remediation.read_surface: source #1                         | Limit  (cost=4.59..8.65 rows=1 width=16) (actual tim |      0.006 |    0.018 | shared hit=5 | -
rollup.remediation.read_surface: source #2                         | Sort  (cost=8.46..8.46 rows=1 width=86) (actual time |      0.006 |    0.015 | shared hit=4 | -
rollup.remediation.read_surface: pypi #1                           | Limit  (cost=4.59..8.65 rows=1 width=16) (actual tim |      0.006 |    0.017 | shared hit=5 | -
rollup.remediation.read_surface: pypi #2                           | Sort  (cost=8.46..8.46 rows=1 width=80) (actual time |      0.006 |    0.015 | shared hit=4 | -
rollup.remediation.read_surface: feedstock #1                      | Limit  (cost=4.59..8.65 rows=1 width=16) (actual tim |      0.007 |    0.020 | shared hit=5 | -
rollup.remediation.read_surface: feedstock #2                      | Sort  (cost=8.46..8.46 rows=1 width=162) (actual tim |      0.007 |    0.016 | shared hit=4 | -
rollup.remediation.read_surface: conda_package #1                  | Limit  (cost=4.64..8.71 rows=1 width=16) (actual tim |      0.008 |    0.023 | shared hit=5 | -
rollup.remediation.read_surface: conda_package #2                  | Sort  (cost=8.46..8.46 rows=1 width=92) (actual time |      0.008 |    0.019 | shared hit=4 | -
rollup.freshness.latest_observation: inventory_snapshots           | Result  (cost=0.47..0.48 rows=1 width=8) (actual tim |      0.008 |    0.025 | shared hit=4 | -
rollup.freshness.latest_observation: source_release_snapshots      | Result  (cost=0.49..0.50 rows=1 width=8) (actual tim |      0.007 |    0.020 | shared hit=4 | -
rollup.freshness.latest_observation: pypi_release_snapshots        | Result  (cost=0.49..0.50 rows=1 width=8) (actual tim |      0.006 |    0.021 | shared hit=4 | -
rollup.freshness.latest_observation: feedstock_snapshots           | Result  (cost=0.49..0.50 rows=1 width=8) (actual tim |      0.006 |    0.019 | shared hit=4 | -
rollup.freshness.latest_observation: conda_package_snapshots       | Result  (cost=0.47..0.48 rows=1 width=8) (actual tim |      0.007 |    0.021 | shared hit=4 | -
rollup.freshness.latest_observation: kev_findings                  | Result  (cost=0.47..0.48 rows=1 width=8) (actual tim |      0.007 |    0.019 | shared hit=4 | -
rollup.freshness.latest_observation: vulnerability_findings        | Result  (cost=0.47..0.48 rows=1 width=8) (actual tim |      0.006 |    0.017 | shared hit=4 | -
rollup.freshness.latest_observation: license_findings              | Result  (cost=0.47..0.48 rows=1 width=8) (actual tim |      0.006 |    0.016 | shared hit=4 | -
rollup.freshness.latest_observation: python_readiness_assessments  | Result  (cost=0.47..0.48 rows=1 width=8) (actual tim |      0.007 |    0.019 | shared hit=4 | -
rollup.freshness.latest_observation: python_verification_results   | Result  (cost=0.47..0.48 rows=1 width=8) (actual tim |      0.006 |    0.018 | shared hit=4 | -
rollup.freshness.latest_observation: identity_resolution_snapshots | Result  (cost=0.49..0.50 rows=1 width=8) (actual tim |      0.005 |    0.017 | shared hit=4 | -
rollup.choose_evidence_cutoff #1                                   | Aggregate  (cost=4.46..4.47 rows=1 width=8) (actual  |      0.007 |    0.019 | shared hit=4 | -
rollup.choose_evidence_cutoff #2                                   | Limit  (cost=0.43..0.48 rows=1 width=8) (actual time |      0.005 |    0.018 | shared hit=4 | -
rollup.choose_evidence_cutoff (refusal path) #1                    | Aggregate  (cost=4.46..4.47 rows=1 width=8) (actual  |      0.007 |    0.019 | shared hit=4 | -
rollup.choose_evidence_cutoff (refusal path) #2                    | Limit  (cost=0.43..4.46 rows=1 width=8) (actual time |      0.003 |    0.018 | shared hit=3 | -
rollup.choose_evidence_cutoff (refusal path) #3                    | Aggregate  (cost=4.47..4.48 rows=1 width=8) (actual  |      0.004 |    0.016 | shared hit=3 | -
page.traces_for #1                                                 | Limit  (cost=8.46..8.46 rows=1 width=114) (actual ti |      0.013 |    0.035 | shared hit=4 | -
page.traces_for #2                                                 | Limit  (cost=8.46..8.46 rows=1 width=95) (actual tim |      0.013 |    0.027 | shared hit=4 | -
page.traces_for #3                                                 | Limit  (cost=8.46..8.46 rows=1 width=95) (actual tim |      0.007 |    0.018 | shared hit=4 | -
page.traces_for #4                                                 | Limit  (cost=8.46..8.46 rows=1 width=69) (actual tim |      0.020 |    0.021 | shared hit=4 | -
page.traces_for #5                                                 | Limit  (cost=8.46..8.46 rows=1 width=94) (actual tim |      0.010 |    0.023 | shared hit=4 | -
page.traces_for #6                                                 | Limit  (cost=8.46..8.46 rows=1 width=104) (actual ti |      0.011 |    0.022 | shared hit=4 | -
page.traces_for #7                                                 | Limit  (cost=8.46..8.46 rows=1 width=83) (actual tim |      0.010 |    0.030 | shared hit=4 | -
page.traces_for #8                                                 | Limit  (cost=8.46..8.46 rows=1 width=67) (actual tim |      0.009 |    0.019 | shared hit=4 | -
page.traces_for #9                                                 | Limit  (cost=0.42..8.44 rows=1 width=80) (actual tim |      0.008 |    0.014 | shared hit=4 | -
page.traces_for #10                                                | Limit  (cost=4.59..85.77 rows=20 width=80) (actual t |      0.032 |    0.014 | shared hit=24 | -
page.traces_for #11                                                | Aggregate  (cost=6.25..6.25 rows=1 width=8) (actual  |      0.014 |    0.012 | shared hit=4 | -
page.traces_for #12                                                | Limit  (cost=0.43..8.45 rows=1 width=116) (actual ti |      0.008 |    0.015 | shared hit=4 | -
page.traces_for #13                                                | Limit  (cost=4.64..85.83 rows=20 width=116) (actual  |      0.027 |    0.016 | shared hit=14 | -
page.traces_for #14                                                | Aggregate  (cost=8.05..8.06 rows=1 width=8) (actual  |      0.018 |    0.010 | shared hit=4 | -
page.traces_for #15                                                | Limit  (cost=0.43..8.45 rows=1 width=74) (actual tim |      0.007 |    0.015 | shared hit=4 | -
page.traces_for #16                                                | Limit  (cost=4.64..85.83 rows=20 width=74) (actual t |      0.023 |    0.015 | shared hit=14 | -
page.traces_for #17                                                | Aggregate  (cost=8.09..8.10 rows=1 width=8) (actual  |      0.018 |    0.010 | shared hit=4 | -
page.traces_for #18                                                | Limit  (cost=0.43..8.45 rows=1 width=102) (actual ti |      0.007 |    0.013 | shared hit=4 | -
page.traces_for #19                                                | Limit  (cost=4.64..85.84 rows=20 width=102) (actual  |      0.024 |    0.014 | shared hit=14 | -
page.traces_for #20                                                | Aggregate  (cost=8.07..8.08 rows=1 width=8) (actual  |      0.018 |    0.010 | shared hit=4 | -
page.traces_for #21                                                | Limit  (cost=0.43..8.45 rows=1 width=143) (actual ti |      0.008 |    0.015 | shared hit=4 | -
page.traces_for #22                                                | Limit  (cost=4.64..85.83 rows=20 width=143) (actual  |      0.025 |    0.014 | shared hit=14 | -
page.traces_for #23                                                | Aggregate  (cost=8.07..8.08 rows=1 width=8) (actual  |      0.018 |    0.009 | shared hit=5 | -
page.traces_for #24                                                | Limit  (cost=0.42..8.44 rows=1 width=162) (actual ti |      0.008 |    0.014 | shared hit=4 | -
page.traces_for #25                                                | Limit  (cost=4.59..85.77 rows=20 width=162) (actual  |      0.033 |    0.015 | shared hit=24 | -
page.traces_for #26                                                | Aggregate  (cost=6.25..6.25 rows=1 width=8) (actual  |      0.012 |    0.009 | shared hit=4 | -
page.recent_runs                                                   | Limit  (cost=5.24..86.16 rows=20 width=56) (actual t |      0.014 |    0.015 | shared hit=7 | -
page.runs_in_flight                                                | Incremental Sort  (cost=4.47..4.52 rows=2 width=56)  |      0.004 |    0.019 | shared hit=3 | -
page.last_recollection                                             | Limit  (cost=11.81..17.68 rows=1 width=2345) (actual |      0.019 |    0.046 | shared hit=2 | -
listing.health_queryset count                                      | Aggregate  (cost=215.28..215.29 rows=1 width=8) (act |      0.616 |    0.011 | shared hit=10 | -
listing.health_queryset first page                                 | Limit  (cost=1.04..450.47 rows=50 width=268) (actual |      0.147 |    0.131 | shared hit=357 | -
coverage.collector_health: ledger group-by                         | Finalize GroupAggregate  (cost=165180.79..165183.16  |    438.641 |    0.105 | shared hit=118 read=88048 | collection_runs
coverage.coverage_of #1                                            | HashAggregate  (cost=383.00..383.01 rows=1 width=17) |      1.363 |    0.033 | shared hit=233 | -
coverage.coverage_of #2                                            | Aggregate  (cost=723.00..723.01 rows=1 width=80) (ac |      2.578 |    0.028 | shared hit=173 | -
digest._freshness: conda_package #1                                | Unique  (cost=1000.45..28726.57 rows=9950 width=8) ( |     73.119 |    0.035 | shared hit=1743 | -
digest._freshness: conda_package #2                                | HashAggregate  (cost=1752.64..1850.81 rows=9817 widt |      8.731 |    0.057 | shared hit=15060 | -
digest._freshness: feedstock #1                                    | Sort  (cost=12.28..12.29 rows=1 width=8) (actual tim |      0.010 |    0.049 | shared hit=1 | -
digest._freshness: feedstock #2                                    | HashAggregate  (cost=15852.76..15952.01 rows=9925 wi |     33.509 |    0.019 | shared hit=905 | -
digest._freshness: feedstock #3                                    | HashAggregate  (cost=6758.06..6857.31 rows=9925 widt |     41.317 |    0.056 | shared hit=83191 | -
digest._freshness: license #1                                      | Unique  (cost=1000.45..28711.71 rows=9887 width=8) ( |     66.558 |    0.041 | shared hit=1743 | -
digest._freshness: license #2                                      | HashAggregate  (cost=1663.80..1760.95 rows=9715 widt |      8.018 |    0.049 | shared hit=15534 | -
digest._freshness: pypi_release #1                                 | Sort  (cost=12.28..12.28 rows=1 width=8) (actual tim |      0.011 |    0.044 | shared hit=1 | -
digest._freshness: pypi_release #2                                 | HashAggregate  (cost=15845.99..15944.91 rows=9892 wi |     26.755 |    0.018 | shared hit=905 | -
digest._freshness: pypi_release #3                                 | HashAggregate  (cost=782.19..867.99 rows=8580 width= |      5.067 |    0.052 | shared hit=6843 | -
digest._freshness: python_readiness #1                             | Sort  (cost=20.61..20.61 rows=1 width=8) (actual tim |      0.017 |    0.116 | shared hit=1 | -
digest._freshness: python_readiness #2                             | Unique  (cost=1000.45..28713.28 rows=9894 width=8) ( |     67.685 |    0.027 | shared hit=21541 | -
digest._freshness: python_readiness #3                             | HashAggregate  (cost=12953.85..13052.79 rows=9894 wi |     59.796 |    0.050 | shared hit=140531 | -
digest._freshness: resolve_identity #1                             | Sort  (cost=408.01..408.01 rows=1 width=8) (actual t |      0.666 |    0.049 | shared hit=233 | -
digest._freshness: resolve_identity #2                             | HashAggregate  (cost=15848.86..15947.92 rows=9906 wi |     29.284 |    0.030 | shared hit=905 | -
digest._freshness: resolve_identity #3                             | HashAggregate  (cost=960.35..1045.56 rows=8521 width |      6.234 |    0.051 | shared hit=11607 | -
digest._freshness: source_release #1                               | Index Scan using packages_pkey on packages  (cost=0. |      1.274 |    0.038 | shared hit=262 | -
digest._freshness: source_release #2                               | HashAggregate  (cost=15851.32..15950.50 rows=9918 wi |     27.027 |    0.018 | shared hit=905 | -
digest._freshness: source_release #3                               | HashAggregate  (cost=833.60..920.67 rows=8707 width= |      6.108 |    0.047 | shared hit=7365 | -
purge.purgeable_policy_runs                                        | Merge Anti Join  (cost=3.64..219.76 rows=1 width=61) |      0.014 |    0.042 | shared hit=5 | -
purge._older: inventory_snapshots                                  | Aggregate  (cost=481.09..481.10 rows=1 width=8) (act |      1.156 |    0.021 | shared hit=27 | -
purge.floor_removable_evidence first batch: inventory_snapshots    | Limit  (cost=1220.64..409341.33 rows=1000 width=8) ( |    144.780 |    0.274 | shared hit=371660 | -
purge.removable_evidence first batch: inventory_snapshots          | Limit  (cost=1220.64..409341.33 rows=1000 width=8) ( |    145.073 |    0.378 | shared hit=371658 | -
purge._older: source_release_snapshots                             | Aggregate  (cost=238.09..238.09 rows=1 width=8) (act |      0.529 |    0.016 | shared hit=12 | -
purge.floor_removable_evidence first batch: source_release_snapsho | Limit  (cost=220.62..220060.16 rows=1000 width=8) (a |    110.319 |    0.209 | shared hit=270152 | -
purge.removable_evidence first batch: source_release_snapshots     | Limit  (cost=56216.35..260985.17 rows=1000 width=8)  |    387.720 |    0.580 | shared hit=270532 read=29935 | package_currency,package_remediation
purge._older: pypi_release_snapshots                               | Aggregate  (cost=238.59..238.59 rows=1 width=8) (act |      0.609 |    0.049 | shared hit=12 | -
purge.floor_removable_evidence first batch: pypi_release_snapshots | Limit  (cost=220.62..219656.46 rows=1000 width=8) (a |    110.653 |    0.226 | shared hit=270151 | -
purge.removable_evidence first batch: pypi_release_snapshots       | Limit  (cost=55434.01..260206.98 rows=1000 width=8)  |    297.728 |    0.495 | shared hit=270907 read=29551 | package_currency,package_remediation
purge._older: feedstock_snapshots                                  | Aggregate  (cost=254.91..254.92 rows=1 width=8) (act |      0.600 |    0.032 | shared hit=12 | -
purge.floor_removable_evidence first batch: feedstock_snapshots    | Limit  (cost=220.62..222076.91 rows=1000 width=8) (a |    111.201 |    0.195 | shared hit=270163 | -
purge.removable_evidence first batch: feedstock_snapshots          | Limit  (cost=382732.70..382735.20 rows=1000 width=8) |   1747.635 |    0.666 | shared hit=2681147 read=44781 | package_currency,package_feedstock_presence,package_remediation
purge._older: conda_package_snapshots                              | Aggregate  (cost=483.25..483.26 rows=1 width=8) (act |      1.177 |    0.025 | shared hit=27 | -
purge.floor_removable_evidence first batch: conda_package_snapshot | Limit  (cost=220.62..220742.58 rows=1000 width=8) (a |    279.064 |    0.232 | shared hit=270417 | -
purge.removable_evidence first batch: conda_package_snapshots      | Limit  (cost=661.31..265817.08 rows=1000 width=8) (a |    296.950 |    0.532 | shared hit=270790 | -
purge._older: kev_findings                                         | Aggregate  (cost=481.21..481.22 rows=1 width=8) (act |      1.103 |    0.032 | shared hit=27 | -
purge.floor_removable_evidence first batch: kev_findings           | Limit  (cost=1221.64..677506.06 rows=1000 width=8) ( |    482.967 |    0.340 | shared hit=935155 | -
purge.removable_evidence first batch: kev_findings                 | Limit  (cost=205508.14..725956.85 rows=1000 width=8) |   1049.440 |    0.937 | shared hit=978238 read=67595, temp read=29078 written=29348 | kev_findings,package_vulnerability,vulnerability_findings
purge._older: vulnerability_findings                               | Aggregate  (cost=503.23..503.24 rows=1 width=8) (act |      1.139 |    0.026 | shared hit=27 | -
purge.floor_removable_evidence first batch: vulnerability_findings | Limit  (cost=1220.64..409433.88 rows=1000 width=8) ( |    144.972 |    0.237 | shared hit=368982 read=16 | -
purge.removable_evidence first batch: vulnerability_findings       | Limit  (cost=1245460.90..1828275.28 rows=1000 width= |  11245.349 |    1.625 | shared hit=15083741 read=3129288, temp read=29074 written=29340 | kev_findings,package_vulnerability,vulnerability_findings
purge._older: license_findings                                     | Aggregate  (cost=487.75..487.76 rows=1 width=8) (act |      1.097 |    0.027 | shared hit=27 | -
purge.floor_removable_evidence first batch: license_findings       | Limit  (cost=1220.64..408798.48 rows=1000 width=8) ( |    142.188 |    0.251 | shared hit=368912 | -
purge.removable_evidence first batch: license_findings             | Limit  (cost=1444.24..437737.03 rows=1000 width=8) ( |    151.945 |    0.416 | shared hit=369084 | -
purge._older: python_readiness_assessments                         | Aggregate  (cost=480.05..480.06 rows=1 width=8) (act |      1.122 |    0.015 | shared hit=27 | -
purge.floor_removable_evidence first batch: python_readiness_asses | Limit  (cost=1220.64..412723.07 rows=1000 width=8) ( |    150.278 |    0.211 | shared hit=369460 | -
purge.removable_evidence first batch: python_readiness_assessments | Limit  (cost=1443.95..444015.05 rows=1000 width=8) ( |    159.935 |    0.382 | shared hit=369662 | -
purge._older: python_verification_results                          | Aggregate  (cost=463.55..463.56 rows=1 width=8) (act |      1.075 |    0.032 | shared hit=27 | -
purge.floor_removable_evidence first batch: python_verification_re | Limit  (cost=1220.64..412912.17 rows=1000 width=8) ( |    151.752 |    0.205 | shared hit=369418 | -
purge.removable_evidence first batch: python_verification_results  | Limit  (cost=1440.99..436040.52 rows=1000 width=8) ( |    158.244 |    0.310 | shared hit=369600 | -
purge._older: identity_resolution_snapshots                        | Aggregate  (cost=270.52..270.53 rows=1 width=8) (act |      0.581 |    0.015 | shared hit=12 | -
purge.floor_removable_evidence first batch: identity_resolution_sn | Limit  (cost=220.62..221020.73 rows=1000 width=8) (a |    109.010 |    0.149 | shared hit=270162 | -
purge.removable_evidence first batch: identity_resolution_snapshot | Limit  (cost=220.62..221020.73 rows=1000 width=8) (a |    113.916 |    0.153 | shared hit=270162 | -
purge.dry-run count: vulnerability_findings                        | Aggregate  (cost=2213318.57..2213318.58 rows=1 width |  15635.661 |    1.569 | shared hit=20911070 read=4153997, temp read=29079 written=29364 | kev_findings,package_remediation,package_vulnerability,vulnerability_findings
purge.dry-run count: source_release_snapshots                      | Aggregate  (cost=606931.05..606931.06 rows=1 width=8 |   1531.379 |    0.455 | shared hit=2677772 read=30063 | package_currency,package_remediation
purge.purge_evidence end to end                                    | 21 tables, 335,219 rows removed                      |  84964.606 |    0.000 |  | -

WALL CLOCK of each product call as issued (seconds, Django round trips included):
  rollup.snapshot_as_of shape: inventory_snapshots                       0.005
  rollup.snapshot_as_of shape: source_release_snapshots                  0.003
  rollup.snapshot_as_of shape: pypi_release_snapshots                    0.006
  rollup.snapshot_as_of shape: feedstock_snapshots                       0.001
  rollup.snapshot_as_of shape: conda_package_snapshots                   0.001
  rollup.snapshot_as_of shape: kev_findings                              0.001
  rollup.snapshot_as_of shape: vulnerability_findings                    0.001
  rollup.snapshot_as_of shape: license_findings                          0.001
  rollup.snapshot_as_of shape: python_readiness_assessments              0.001
  rollup.snapshot_as_of shape: python_verification_results               0.001
  rollup.snapshot_as_of shape: identity_resolution_snapshots             0.002
  rollup.snapshot_as_of (inventory)                                      0.001
  rollup.currency.observed_surface ranked: source                        0.003
  rollup.currency.observed_surface ranked: pypi                          0.001
  rollup.currency.observed_surface ranked: feedstock                     0.001
  rollup.currency.observed_surface ranked: conda_package                 0.001
  rollup.feedstock.observed_feedstock                                    0.001
  rollup.py314.current_assessment (package, series)                      0.001
  rollup.py314.current_verification (package, series)                    0.001
  rollup.vulnerability.current_findings                                  0.001
  rollup.vulnerability.current_cross_references                          0.001
  rollup.licence.current_findings                                        0.001
  rollup.remediation.current_findings                                    0.001
  rollup.remediation.read_surface: source                                0.001
  rollup.remediation.read_surface: pypi                                  0.001
  rollup.remediation.read_surface: feedstock                             0.001
  rollup.remediation.read_surface: conda_package                         0.001
  rollup.freshness.latest_observation: inventory_snapshots               0.001
  rollup.freshness.latest_observation: source_release_snapshots          0.001
  rollup.freshness.latest_observation: pypi_release_snapshots            0.000
  rollup.freshness.latest_observation: feedstock_snapshots               0.000
  rollup.freshness.latest_observation: conda_package_snapshots           0.000
  rollup.freshness.latest_observation: kev_findings                      0.000
  rollup.freshness.latest_observation: vulnerability_findings            0.000
  rollup.freshness.latest_observation: license_findings                  0.000
  rollup.freshness.latest_observation: python_readiness_assessments      0.000
  rollup.freshness.latest_observation: python_verification_results       0.000
  rollup.freshness.latest_observation: identity_resolution_snapshots     0.000
  rollup.choose_evidence_cutoff                                          0.002
  rollup.choose_evidence_cutoff (refusal path)                           0.001
  page.traces_for                                                        0.021
  page.recent_runs                                                       0.001
  page.runs_in_flight                                                    0.001
  page.last_recollection                                                 0.004
  listing.health_queryset count                                          0.001
  listing.health_queryset first page                                     0.004
  coverage.collector_health: ledger group-by                             0.518
  coverage.coverage_of                                                   0.017
  digest._freshness: conda_package                                       0.072
  digest._freshness: feedstock                                           0.090
  digest._freshness: kev                                                 0.001
  digest._freshness: license                                             0.061
  digest._freshness: pypi_release                                        0.041
  digest._freshness: python_readiness                                    0.138
  digest._freshness: resolve_identity                                    0.049
  digest._freshness: source_release                                      0.122
  digest._freshness: vulnerability                                       0.000
  purge.purgeable_policy_runs                                            0.002
  purge._older: inventory_snapshots                                      0.002
  purge.floor_removable_evidence first batch: inventory_snapshots        0.188
  purge.removable_evidence first batch: inventory_snapshots              0.142
  purge._older: source_release_snapshots                                 0.004
  purge.floor_removable_evidence first batch: source_release_snapshots     0.127
  purge.removable_evidence first batch: source_release_snapshots         0.299
  purge._older: pypi_release_snapshots                                   0.005
  purge.floor_removable_evidence first batch: pypi_release_snapshots     0.144
  purge.removable_evidence first batch: pypi_release_snapshots           0.275
  purge._older: feedstock_snapshots                                      0.002
  purge.floor_removable_evidence first batch: feedstock_snapshots        0.125
  purge.removable_evidence first batch: feedstock_snapshots              1.519
  purge._older: conda_package_snapshots                                  0.003
  purge.floor_removable_evidence first batch: conda_package_snapshots     0.282
  purge.removable_evidence first batch: conda_package_snapshots          0.314
  purge._older: kev_findings                                             0.003
  purge.floor_removable_evidence first batch: kev_findings               0.537
  purge.removable_evidence first batch: kev_findings                     1.719
  purge._older: vulnerability_findings                                   0.006
  purge.floor_removable_evidence first batch: vulnerability_findings     0.172
  purge.removable_evidence first batch: vulnerability_findings          11.744
  purge._older: license_findings                                         0.006
  purge.floor_removable_evidence first batch: license_findings           0.174
  purge.removable_evidence first batch: license_findings                 0.162
  purge._older: python_readiness_assessments                             0.003
  purge.floor_removable_evidence first batch: python_readiness_assessments     0.154
  purge.removable_evidence first batch: python_readiness_assessments     0.161
  purge._older: python_verification_results                              0.002
  purge.floor_removable_evidence first batch: python_verification_results     0.151
  purge.removable_evidence first batch: python_verification_results      0.161
  purge._older: identity_resolution_snapshots                            0.003
  purge.floor_removable_evidence first batch: identity_resolution_snapshots     0.114
  purge.removable_evidence first batch: identity_resolution_snapshots     0.110
  purge.dry-run count: vulnerability_findings                           15.770
  purge.dry-run count: source_release_snapshots                          1.737
  purge.derived rows of the purgeable run deleted: package_vulnerability     0.086
  purge.one batch deleted: kev_findings                                  0.039
  purge.one batch deleted: vulnerability_findings                        0.049
  purge.one batch deleted: source_release_snapshots                      0.044

rollup read budget: 48 per-package statements, 0.391 ms of execution for one package; x 10,000 packages = 3.9 s of database execution (CPM-AD-23: one transaction per package, N+1 by design)
```

### The plans, verbatim

```text
====================================================================================================
PLANS (verbatim)
====================================================================================================

--- rollup.snapshot_as_of shape: inventory_snapshots
SQL: SELECT "inventory_snapshots"."id", "inventory_snapshots"."observed_at", "inventory_snapshots"."package_id", "inventory_snapshots"."source_package_key", "inventory_snapshots"."state", "inventory_snapshots"."internal_component_count", "inventory_snapshots"."internal_lob_count", "inventory_snapshots"."apps", "inventory_snapshots"."platforms", "inventory_snapshots"."downloads", "inventory_snapshots"."versions", "inventory_snapshots"."detail", "inventory_snapshots"."trace_id" FROM "inventory_snapshots" WHERE ("inventory_snapshots"."observed_at" <= '2026-09-13 11:00:00+00:00'::timestamptz AND "inventory_snapshots"."package_id" = 5001) ORDER BY "inventory_snapshots"."observed_at" DESC, "inventory_snapshots"."id" DESC LIMIT 1
Limit  (cost=4.64..8.71 rows=1 width=65) (actual time=0.012..0.012 rows=1 loops=1)
  Buffers: shared hit=5
  ->  Incremental Sort  (cost=4.64..731.71 rows=179 width=65) (actual time=0.012..0.012 rows=1 loops=1)
        Sort Key: observed_at DESC, id DESC
        Presorted Key: observed_at
        Full-sort Groups: 1  Sort Method: quicksort  Average Memory: 25kB  Peak Memory: 25kB
        Buffers: shared hit=5
        ->  Index Scan using inv_snapshot_pkg_observed on inventory_snapshots  (cost=0.43..723.90 rows=179 width=65) (actual time=0.005..0.006 rows=3 loops=1)
              Index Cond: ((package_id = 5001) AND (observed_at <= '2026-09-13 11:00:00+00'::timestamp with time zone))
              Buffers: shared hit=5
Planning:
  Buffers: shared hit=4
Planning Time: 0.052 ms
Execution Time: 0.029 ms

--- rollup.snapshot_as_of shape: source_release_snapshots
SQL: SELECT "source_release_snapshots"."id", "source_release_snapshots"."observed_at", "source_release_snapshots"."package_id", "source_release_snapshots"."source", "source_release_snapshots"."state", "source_release_snapshots"."latest_version", "source_release_snapshots"."released_at", "source_release_snapshots"."last_activity_at", "source_release_snapshots"."releases_seen", "source_release_snapshots"."detail", "source_release_snapshots"."trace_id" FROM "source_release_snapshots" WHERE ("source_release_snapshots"."observed_at" <= '2026-09-13 11:00:00+00:00'::timestamptz AND "source_release_snapshots"."package_id" = 5001) ORDER BY "source_release_snapshots"."observed_at" DESC, "source_release_snapshots"."id" DESC LIMIT 1
Limit  (cost=4.59..8.65 rows=1 width=86) (actual time=0.005..0.006 rows=1 loops=1)
  Buffers: shared hit=5
  ->  Incremental Sort  (cost=4.59..370.19 rows=90 width=86) (actual time=0.005..0.005 rows=1 loops=1)
        Sort Key: observed_at DESC, id DESC
        Presorted Key: observed_at
        Full-sort Groups: 1  Sort Method: quicksort  Average Memory: 25kB  Peak Memory: 25kB
        Buffers: shared hit=5
        ->  Index Scan using src_release_pkg_observed on source_release_snapshots  (cost=0.42..366.21 rows=90 width=86) (actual time=0.003..0.004 rows=2 loops=1)
              Index Cond: ((package_id = 5001) AND (observed_at <= '2026-09-13 11:00:00+00'::timestamp with time zone))
              Buffers: shared hit=5
Planning:
  Buffers: shared hit=4
Planning Time: 0.025 ms
Execution Time: 0.009 ms

--- rollup.snapshot_as_of shape: pypi_release_snapshots
SQL: SELECT "pypi_release_snapshots"."id", "pypi_release_snapshots"."observed_at", "pypi_release_snapshots"."package_id", "pypi_release_snapshots"."source", "pypi_release_snapshots"."state", "pypi_release_snapshots"."latest_version", "pypi_release_snapshots"."released_at", "pypi_release_snapshots"."requires_python", "pypi_release_snapshots"."detail", "pypi_release_snapshots"."trace_id" FROM "pypi_release_snapshots" WHERE ("pypi_release_snapshots"."observed_at" <= '2026-09-13 11:00:00+00:00'::timestamptz AND "pypi_release_snapshots"."package_id" = 5001) ORDER BY "pypi_release_snapshots"."observed_at" DESC, "pypi_release_snapshots"."id" DESC LIMIT 1
Limit  (cost=4.59..8.65 rows=1 width=80) (actual time=0.005..0.005 rows=1 loops=1)
  Buffers: shared hit=5
  ->  Incremental Sort  (cost=4.59..370.15 rows=90 width=80) (actual time=0.004..0.004 rows=1 loops=1)
        Sort Key: observed_at DESC, id DESC
        Presorted Key: observed_at
        Full-sort Groups: 1  Sort Method: quicksort  Average Memory: 25kB  Peak Memory: 25kB
        Buffers: shared hit=5
        ->  Index Scan using pypi_release_pkg_observed on pypi_release_snapshots  (cost=0.42..366.17 rows=90 width=80) (actual time=0.003..0.003 rows=2 loops=1)
              Index Cond: ((package_id = 5001) AND (observed_at <= '2026-09-13 11:00:00+00'::timestamp with time zone))
              Buffers: shared hit=5
Planning:
  Buffers: shared hit=4
Planning Time: 0.024 ms
Execution Time: 0.008 ms

--- rollup.snapshot_as_of shape: feedstock_snapshots
SQL: SELECT "feedstock_snapshots"."id", "feedstock_snapshots"."observed_at", "feedstock_snapshots"."package_id", "feedstock_snapshots"."source", "feedstock_snapshots"."state", "feedstock_snapshots"."feedstock_name", "feedstock_snapshots"."feedstock_url", "feedstock_snapshots"."recipe_version", "feedstock_snapshots"."recipe_build_number", "feedstock_snapshots"."recipe_metadata_url", "feedstock_snapshots"."last_recipe_activity_at", "feedstock_snapshots"."staged_recipe_url", "feedstock_snapshots"."absence_established", "feedstock_snapshots"."detail", "feedstock_snapshots"."trace_id" FROM "feedstock_snapshots" WHERE ("feedstock_snapshots"."observed_at" <= '2026-09-13 11:00:00+00:00'::timestamptz AND "feedstock_snapshots"."package_id" = 5001) ORDER BY "feedstock_snapshots"."observed_at" DESC, "feedstock_snapshots"."id" DESC LIMIT 1
Limit  (cost=4.59..8.65 rows=1 width=162) (actual time=0.005..0.005 rows=1 loops=1)
  Buffers: shared hit=5
  ->  Incremental Sort  (cost=4.59..370.15 rows=90 width=162) (actual time=0.004..0.005 rows=1 loops=1)
        Sort Key: observed_at DESC, id DESC
        Presorted Key: observed_at
        Full-sort Groups: 1  Sort Method: quicksort  Average Memory: 25kB  Peak Memory: 25kB
        Buffers: shared hit=5
        ->  Index Scan using feedstock_pkg_observed on feedstock_snapshots  (cost=0.42..366.17 rows=90 width=162) (actual time=0.003..0.003 rows=2 loops=1)
              Index Cond: ((package_id = 5001) AND (observed_at <= '2026-09-13 11:00:00+00'::timestamp with time zone))
              Buffers: shared hit=5
Planning:
  Buffers: shared hit=4
Planning Time: 0.022 ms
Execution Time: 0.008 ms

--- rollup.snapshot_as_of shape: conda_package_snapshots
SQL: SELECT "conda_package_snapshots"."id", "conda_package_snapshots"."observed_at", "conda_package_snapshots"."package_id", "conda_package_snapshots"."source", "conda_package_snapshots"."state", "conda_package_snapshots"."channel", "conda_package_snapshots"."platform", "conda_package_snapshots"."published_version", "conda_package_snapshots"."build_string", "conda_package_snapshots"."build_number", "conda_package_snapshots"."detail", "conda_package_snapshots"."trace_id" FROM "conda_package_snapshots" WHERE ("conda_package_snapshots"."observed_at" <= '2026-09-13 11:00:00+00:00'::timestamptz AND "conda_package_snapshots"."package_id" = 5001) ORDER BY "conda_package_snapshots"."observed_at" DESC, "conda_package_snapshots"."id" DESC LIMIT 1
Limit  (cost=4.64..8.71 rows=1 width=92) (actual time=0.006..0.007 rows=1 loops=1)
  Buffers: shared hit=5
  ->  Incremental Sort  (cost=4.64..731.77 rows=179 width=92) (actual time=0.006..0.006 rows=1 loops=1)
        Sort Key: observed_at DESC, id DESC
        Presorted Key: observed_at
        Full-sort Groups: 1  Sort Method: quicksort  Average Memory: 25kB  Peak Memory: 25kB
        Buffers: shared hit=5
        ->  Index Scan using conda_pkg_pkg_observed on conda_package_snapshots  (cost=0.43..723.96 rows=179 width=92) (actual time=0.004..0.004 rows=3 loops=1)
              Index Cond: ((package_id = 5001) AND (observed_at <= '2026-09-13 11:00:00+00'::timestamp with time zone))
              Buffers: shared hit=5
Planning:
  Buffers: shared hit=4
Planning Time: 0.027 ms
Execution Time: 0.010 ms

--- rollup.snapshot_as_of shape: kev_findings
SQL: SELECT "kev_findings"."id", "kev_findings"."observed_at", "kev_findings"."package_id", "kev_findings"."vulnerability_finding_id", "kev_findings"."source", "kev_findings"."state", "kev_findings"."catalog_date_added", "kev_findings"."detail", "kev_findings"."trace_id" FROM "kev_findings" WHERE ("kev_findings"."observed_at" <= '2026-09-13 11:00:00+00:00'::timestamptz AND "kev_findings"."package_id" = 5001) ORDER BY "kev_findings"."observed_at" DESC, "kev_findings"."id" DESC LIMIT 1
Limit  (cost=4.64..8.70 rows=1 width=74) (actual time=0.006..0.006 rows=1 loops=1)
  Buffers: shared hit=5
  ->  Incremental Sort  (cost=4.64..735.81 rows=180 width=74) (actual time=0.005..0.005 rows=1 loops=1)
        Sort Key: observed_at DESC, id DESC
        Presorted Key: observed_at
        Full-sort Groups: 1  Sort Method: quicksort  Average Memory: 25kB  Peak Memory: 25kB
        Buffers: shared hit=5
        ->  Index Scan using kev_finding_pkg_observed on kev_findings  (cost=0.43..727.95 rows=180 width=74) (actual time=0.003..0.004 rows=3 loops=1)
              Index Cond: ((package_id = 5001) AND (observed_at <= '2026-09-13 11:00:00+00'::timestamp with time zone))
              Buffers: shared hit=5
Planning:
  Buffers: shared hit=4
Planning Time: 0.023 ms
Execution Time: 0.009 ms

--- rollup.snapshot_as_of shape: vulnerability_findings
SQL: SELECT "vulnerability_findings"."id", "vulnerability_findings"."observed_at", "vulnerability_findings"."package_id", "vulnerability_findings"."source", "vulnerability_findings"."state", "vulnerability_findings"."advisory_id", "vulnerability_findings"."severity", "vulnerability_findings"."affected_range", "vulnerability_findings"."fixed_range", "vulnerability_findings"."matched_version", "vulnerability_findings"."match_confidence", "vulnerability_findings"."detail", "vulnerability_findings"."trace_id" FROM "vulnerability_findings" WHERE ("vulnerability_findings"."observed_at" <= '2026-09-13 11:00:00+00:00'::timestamptz AND "vulnerability_findings"."package_id" = 5001) ORDER BY "vulnerability_findings"."observed_at" DESC, "vulnerability_findings"."id" DESC LIMIT 1
Limit  (cost=4.64..8.71 rows=1 width=116) (actual time=0.006..0.006 rows=1 loops=1)
  Buffers: shared hit=5
  ->  Incremental Sort  (cost=4.64..731.78 rows=179 width=116) (actual time=0.005..0.005 rows=1 loops=1)
        Sort Key: observed_at DESC, id DESC
        Presorted Key: observed_at
        Full-sort Groups: 1  Sort Method: quicksort  Average Memory: 25kB  Peak Memory: 25kB
        Buffers: shared hit=5
        ->  Index Scan using vuln_finding_pkg_observed on vulnerability_findings  (cost=0.43..723.97 rows=179 width=116) (actual time=0.003..0.004 rows=3 loops=1)
              Index Cond: ((package_id = 5001) AND (observed_at <= '2026-09-13 11:00:00+00'::timestamp with time zone))
              Buffers: shared hit=5
Planning:
  Buffers: shared hit=4
Planning Time: 0.022 ms
Execution Time: 0.009 ms

--- rollup.snapshot_as_of shape: license_findings
SQL: SELECT "license_findings"."id", "license_findings"."observed_at", "license_findings"."package_id", "license_findings"."source", "license_findings"."state", "license_findings"."channel", "license_findings"."raw_license", "license_findings"."normalized_license", "license_findings"."detection_method", "license_findings"."detail", "license_findings"."trace_id" FROM "license_findings" WHERE ("license_findings"."observed_at" <= '2026-09-13 11:00:00+00:00'::timestamptz AND "license_findings"."package_id" = 5001) ORDER BY "license_findings"."observed_at" DESC, "license_findings"."id" DESC LIMIT 1
Limit  (cost=4.64..8.71 rows=1 width=102) (actual time=0.006..0.006 rows=1 loops=1)
  Buffers: shared hit=5
  ->  Incremental Sort  (cost=4.64..735.87 rows=180 width=102) (actual time=0.006..0.006 rows=1 loops=1)
        Sort Key: observed_at DESC, id DESC
        Presorted Key: observed_at
        Full-sort Groups: 1  Sort Method: quicksort  Average Memory: 25kB  Peak Memory: 25kB
        Buffers: shared hit=5
        ->  Index Scan using license_finding_pkg_observed on license_findings  (cost=0.43..728.02 rows=180 width=102) (actual time=0.004..0.004 rows=3 loops=1)
              Index Cond: ((package_id = 5001) AND (observed_at <= '2026-09-13 11:00:00+00'::timestamp with time zone))
              Buffers: shared hit=5
Planning:
  Buffers: shared hit=4
Planning Time: 0.022 ms
Execution Time: 0.010 ms

--- rollup.snapshot_as_of shape: python_readiness_assessments
SQL: SELECT "python_readiness_assessments"."id", "python_readiness_assessments"."observed_at", "python_readiness_assessments"."package_id", "python_readiness_assessments"."source", "python_readiness_assessments"."state", "python_readiness_assessments"."python_series", "python_readiness_assessments"."requires_python", "python_readiness_assessments"."matching_classifier", "python_readiness_assessments"."deciding_signal", "python_readiness_assessments"."detail", "python_readiness_assessments"."trace_id" FROM "python_readiness_assessments" WHERE ("python_readiness_assessments"."observed_at" <= '2026-09-13 11:00:00+00:00'::timestamptz AND "python_readiness_assessments"."package_id" = 5001) ORDER BY "python_readiness_assessments"."observed_at" DESC, "python_readiness_assessments"."id" DESC LIMIT 1
Limit  (cost=4.64..8.71 rows=1 width=143) (actual time=0.005..0.006 rows=1 loops=1)
  Buffers: shared hit=5
  ->  Incremental Sort  (cost=4.64..731.75 rows=179 width=143) (actual time=0.005..0.005 rows=1 loops=1)
        Sort Key: observed_at DESC, id DESC
        Presorted Key: observed_at
        Full-sort Groups: 1  Sort Method: quicksort  Average Memory: 25kB  Peak Memory: 25kB
        Buffers: shared hit=5
        ->  Index Scan using py_readiness_pkg_observed on python_readiness_assessments  (cost=0.43..723.94 rows=179 width=143) (actual time=0.003..0.004 rows=3 loops=1)
              Index Cond: ((package_id = 5001) AND (observed_at <= '2026-09-13 11:00:00+00'::timestamp with time zone))
              Buffers: shared hit=5
Planning:
  Buffers: shared hit=4
Planning Time: 0.021 ms
Execution Time: 0.017 ms

--- rollup.snapshot_as_of shape: python_verification_results
SQL: SELECT "python_verification_results"."id", "python_verification_results"."observed_at", "python_verification_results"."package_id", "python_verification_results"."source", "python_verification_results"."state", "python_verification_results"."python_series", "python_verification_results"."platform", "python_verification_results"."architecture", "python_verification_results"."log_reference", "python_verification_results"."detail", "python_verification_results"."trace_id" FROM "python_verification_results" WHERE ("python_verification_results"."observed_at" <= '2026-09-13 11:00:00+00:00'::timestamptz AND "python_verification_results"."package_id" = 5001) ORDER BY "python_verification_results"."observed_at" DESC, "python_verification_results"."id" DESC LIMIT 1
Limit  (cost=4.64..8.70 rows=1 width=133) (actual time=0.005..0.005 rows=1 loops=1)
  Buffers: shared hit=5
  ->  Incremental Sort  (cost=4.64..735.77 rows=180 width=133) (actual time=0.005..0.005 rows=1 loops=1)
        Sort Key: observed_at DESC, id DESC
        Presorted Key: observed_at
        Full-sort Groups: 1  Sort Method: quicksort  Average Memory: 25kB  Peak Memory: 25kB
        Buffers: shared hit=5
        ->  Index Scan using py_verify_pkg_observed on python_verification_results  (cost=0.43..727.92 rows=180 width=133) (actual time=0.003..0.003 rows=3 loops=1)
              Index Cond: ((package_id = 5001) AND (observed_at <= '2026-09-13 11:00:00+00'::timestamp with time zone))
              Buffers: shared hit=5
Planning:
  Buffers: shared hit=4
Planning Time: 0.020 ms
Execution Time: 0.008 ms

--- rollup.snapshot_as_of shape: identity_resolution_snapshots
SQL: SELECT "identity_resolution_snapshots"."id", "identity_resolution_snapshots"."observed_at", "identity_resolution_snapshots"."package_id", "identity_resolution_snapshots"."source", "identity_resolution_snapshots"."state", "identity_resolution_snapshots"."repository_url", "identity_resolution_snapshots"."repository_key", "identity_resolution_snapshots"."pypi_asked", "identity_resolution_snapshots"."pypi_found", "identity_resolution_snapshots"."pypi_source", "identity_resolution_snapshots"."feedstocks", "identity_resolution_snapshots"."confidence_recorded", "identity_resolution_snapshots"."downgrade_refused", "identity_resolution_snapshots"."detail", "identity_resolution_snapshots"."trace_id" FROM "identity_resolution_snapshots" WHERE ("identity_resolution_snapshots"."observed_at" <= '2026-09-13 11:00:00+00:00'::timestamptz AND "identity_resolution_snapshots"."package_id" = 5001) ORDER BY "identity_resolution_snapshots"."observed_at" DESC, "identity_resolution_snapshots"."id" DESC LIMIT 1
Limit  (cost=4.59..8.65 rows=1 width=167) (actual time=0.006..0.006 rows=1 loops=1)
  Buffers: shared hit=5
  ->  Incremental Sort  (cost=4.59..370.18 rows=90 width=167) (actual time=0.006..0.006 rows=1 loops=1)
        Sort Key: observed_at DESC, id DESC
        Presorted Key: observed_at
        Full-sort Groups: 1  Sort Method: quicksort  Average Memory: 25kB  Peak Memory: 25kB
        Buffers: shared hit=5
        ->  Index Scan using id_resolution_pkg_observed on identity_resolution_snapshots  (cost=0.42..366.20 rows=90 width=167) (actual time=0.004..0.004 rows=2 loops=1)
              Index Cond: ((package_id = 5001) AND (observed_at <= '2026-09-13 11:00:00+00'::timestamp with time zone))
              Buffers: shared hit=5
Planning:
  Buffers: shared hit=4
Planning Time: 0.025 ms
Execution Time: 0.009 ms

--- rollup.snapshot_as_of (inventory)
SQL: SELECT "inventory_snapshots"."id", "inventory_snapshots"."observed_at", "inventory_snapshots"."package_id", "inventory_snapshots"."source_package_key", "inventory_snapshots"."state", "inventory_snapshots"."internal_component_count", "inventory_snapshots"."internal_lob_count", "inventory_snapshots"."apps", "inventory_snapshots"."platforms", "inventory_snapshots"."downloads", "inventory_snapshots"."versions", "inventory_snapshots"."detail", "inventory_snapshots"."trace_id" FROM "inventory_snapshots" WHERE ("inventory_snapshots"."observed_at" <= '2026-09-13 11:00:00+00:00'::timestamptz AND "inventory_snapshots"."package_id" = 5001) ORDER BY "inventory_snapshots"."observed_at" DESC, "inventory_snapshots"."id" DESC LIMIT 1
Limit  (cost=4.64..8.71 rows=1 width=65) (actual time=0.005..0.005 rows=1 loops=1)
  Buffers: shared hit=5
  ->  Incremental Sort  (cost=4.64..731.71 rows=179 width=65) (actual time=0.005..0.005 rows=1 loops=1)
        Sort Key: observed_at DESC, id DESC
        Presorted Key: observed_at
        Full-sort Groups: 1  Sort Method: quicksort  Average Memory: 25kB  Peak Memory: 25kB
        Buffers: shared hit=5
        ->  Index Scan using inv_snapshot_pkg_observed on inventory_snapshots  (cost=0.43..723.90 rows=179 width=65) (actual time=0.003..0.003 rows=3 loops=1)
              Index Cond: ((package_id = 5001) AND (observed_at <= '2026-09-13 11:00:00+00'::timestamp with time zone))
              Buffers: shared hit=5
Planning:
  Buffers: shared hit=4
Planning Time: 0.021 ms
Execution Time: 0.008 ms

--- rollup.currency.observed_surface ranked: source
SQL: SELECT "source_release_snapshots"."id", "source_release_snapshots"."observed_at", "source_release_snapshots"."package_id", "source_release_snapshots"."source", "source_release_snapshots"."state", "source_release_snapshots"."latest_version", "source_release_snapshots"."released_at", "source_release_snapshots"."last_activity_at", "source_release_snapshots"."releases_seen", "source_release_snapshots"."detail", "source_release_snapshots"."trace_id", CASE WHEN "source_release_snapshots"."state" = 'ok' THEN 0 ELSE 1 END AS "determinate_rank" FROM "source_release_snapshots" WHERE ("source_release_snapshots"."observed_at" <= '2026-09-13 11:00:00+00:00'::timestamptz AND "source_release_snapshots"."package_id" = 5001) ORDER BY "source_release_snapshots"."observed_at" DESC, 12 ASC, "source_release_snapshots"."id" DESC LIMIT 1
Limit  (cost=4.59..8.66 rows=1 width=90) (actual time=0.007..0.007 rows=1 loops=1)
  Buffers: shared hit=5
  ->  Incremental Sort  (cost=4.59..370.41 rows=90 width=90) (actual time=0.006..0.006 rows=1 loops=1)
        Sort Key: observed_at DESC, (CASE WHEN ((state)::text = 'ok'::text) THEN 0 ELSE 1 END), id DESC
        Presorted Key: observed_at
        Full-sort Groups: 1  Sort Method: quicksort  Average Memory: 25kB  Peak Memory: 25kB
        Buffers: shared hit=5
        ->  Index Scan using src_release_pkg_observed on source_release_snapshots  (cost=0.42..366.43 rows=90 width=90) (actual time=0.004..0.004 rows=2 loops=1)
              Index Cond: ((package_id = 5001) AND (observed_at <= '2026-09-13 11:00:00+00'::timestamp with time zone))
              Buffers: shared hit=5
Planning:
  Buffers: shared hit=4
Planning Time: 0.029 ms
Execution Time: 0.011 ms

--- rollup.currency.observed_surface ranked: pypi
SQL: SELECT "pypi_release_snapshots"."id", "pypi_release_snapshots"."observed_at", "pypi_release_snapshots"."package_id", "pypi_release_snapshots"."source", "pypi_release_snapshots"."state", "pypi_release_snapshots"."latest_version", "pypi_release_snapshots"."released_at", "pypi_release_snapshots"."requires_python", "pypi_release_snapshots"."detail", "pypi_release_snapshots"."trace_id", CASE WHEN "pypi_release_snapshots"."state" = 'ok' THEN 0 ELSE 1 END AS "determinate_rank" FROM "pypi_release_snapshots" WHERE ("pypi_release_snapshots"."observed_at" <= '2026-09-13 11:00:00+00:00'::timestamptz AND "pypi_release_snapshots"."package_id" = 5001) ORDER BY "pypi_release_snapshots"."observed_at" DESC, 11 ASC, "pypi_release_snapshots"."id" DESC LIMIT 1
Limit  (cost=4.59..8.66 rows=1 width=84) (actual time=0.005..0.005 rows=1 loops=1)
  Buffers: shared hit=5
  ->  Incremental Sort  (cost=4.59..370.38 rows=90 width=84) (actual time=0.005..0.005 rows=1 loops=1)
        Sort Key: observed_at DESC, (CASE WHEN ((state)::text = 'ok'::text) THEN 0 ELSE 1 END), id DESC
        Presorted Key: observed_at
        Full-sort Groups: 1  Sort Method: quicksort  Average Memory: 25kB  Peak Memory: 25kB
        Buffers: shared hit=5
        ->  Index Scan using pypi_release_pkg_observed on pypi_release_snapshots  (cost=0.42..366.40 rows=90 width=84) (actual time=0.003..0.003 rows=2 loops=1)
              Index Cond: ((package_id = 5001) AND (observed_at <= '2026-09-13 11:00:00+00'::timestamp with time zone))
              Buffers: shared hit=5
Planning:
  Buffers: shared hit=4
Planning Time: 0.022 ms
Execution Time: 0.008 ms

--- rollup.currency.observed_surface ranked: feedstock
SQL: SELECT "feedstock_snapshots"."id", "feedstock_snapshots"."observed_at", "feedstock_snapshots"."package_id", "feedstock_snapshots"."source", "feedstock_snapshots"."state", "feedstock_snapshots"."feedstock_name", "feedstock_snapshots"."feedstock_url", "feedstock_snapshots"."recipe_version", "feedstock_snapshots"."recipe_build_number", "feedstock_snapshots"."recipe_metadata_url", "feedstock_snapshots"."last_recipe_activity_at", "feedstock_snapshots"."staged_recipe_url", "feedstock_snapshots"."absence_established", "feedstock_snapshots"."detail", "feedstock_snapshots"."trace_id", CASE WHEN "feedstock_snapshots"."state" = 'ok' THEN 0 ELSE 1 END AS "determinate_rank" FROM "feedstock_snapshots" WHERE ("feedstock_snapshots"."observed_at" <= '2026-09-13 11:00:00+00:00'::timestamptz AND "feedstock_snapshots"."package_id" = 5001) ORDER BY "feedstock_snapshots"."observed_at" DESC, 16 ASC, "feedstock_snapshots"."id" DESC LIMIT 1
Limit  (cost=4.59..8.66 rows=1 width=166) (actual time=0.005..0.005 rows=1 loops=1)
  Buffers: shared hit=5
  ->  Incremental Sort  (cost=4.59..370.37 rows=90 width=166) (actual time=0.005..0.005 rows=1 loops=1)
        Sort Key: observed_at DESC, (CASE WHEN ((state)::text = 'ok'::text) THEN 0 ELSE 1 END), id DESC
        Presorted Key: observed_at
        Full-sort Groups: 1  Sort Method: quicksort  Average Memory: 25kB  Peak Memory: 25kB
        Buffers: shared hit=5
        ->  Index Scan using feedstock_pkg_observed on feedstock_snapshots  (cost=0.42..366.39 rows=90 width=166) (actual time=0.003..0.003 rows=2 loops=1)
              Index Cond: ((package_id = 5001) AND (observed_at <= '2026-09-13 11:00:00+00'::timestamp with time zone))
              Buffers: shared hit=5
Planning:
  Buffers: shared hit=4
Planning Time: 0.023 ms
Execution Time: 0.009 ms

--- rollup.currency.observed_surface ranked: conda_package
SQL: SELECT "conda_package_snapshots"."id", "conda_package_snapshots"."observed_at", "conda_package_snapshots"."package_id", "conda_package_snapshots"."source", "conda_package_snapshots"."state", "conda_package_snapshots"."channel", "conda_package_snapshots"."platform", "conda_package_snapshots"."published_version", "conda_package_snapshots"."build_string", "conda_package_snapshots"."build_number", "conda_package_snapshots"."detail", "conda_package_snapshots"."trace_id", CASE WHEN "conda_package_snapshots"."state" = 'ok' THEN 0 ELSE 1 END AS "determinate_rank" FROM "conda_package_snapshots" WHERE ("conda_package_snapshots"."observed_at" <= '2026-09-13 11:00:00+00:00'::timestamptz AND "conda_package_snapshots"."package_id" = 5001) ORDER BY "conda_package_snapshots"."observed_at" DESC, 13 ASC, "conda_package_snapshots"."channel" ASC, "conda_package_snapshots"."platform" ASC, "conda_package_snapshots"."id" DESC LIMIT 1
Limit  (cost=4.65..8.71 rows=1 width=96) (actual time=0.007..0.007 rows=1 loops=1)
  Buffers: shared hit=5
  ->  Incremental Sort  (cost=4.65..732.22 rows=179 width=96) (actual time=0.007..0.007 rows=1 loops=1)
        Sort Key: observed_at DESC, (CASE WHEN ((state)::text = 'ok'::text) THEN 0 ELSE 1 END), channel, platform, id DESC
        Presorted Key: observed_at
        Full-sort Groups: 1  Sort Method: quicksort  Average Memory: 25kB  Peak Memory: 25kB
        Buffers: shared hit=5
        ->  Index Scan using conda_pkg_pkg_observed on conda_package_snapshots  (cost=0.43..724.41 rows=179 width=96) (actual time=0.003..0.004 rows=3 loops=1)
              Index Cond: ((package_id = 5001) AND (observed_at <= '2026-09-13 11:00:00+00'::timestamp with time zone))
              Buffers: shared hit=5
Planning:
  Buffers: shared hit=4
Planning Time: 0.030 ms
Execution Time: 0.011 ms

--- rollup.feedstock.observed_feedstock
SQL: SELECT "feedstock_snapshots"."id", "feedstock_snapshots"."observed_at", "feedstock_snapshots"."package_id", "feedstock_snapshots"."source", "feedstock_snapshots"."state", "feedstock_snapshots"."feedstock_name", "feedstock_snapshots"."feedstock_url", "feedstock_snapshots"."recipe_version", "feedstock_snapshots"."recipe_build_number", "feedstock_snapshots"."recipe_metadata_url", "feedstock_snapshots"."last_recipe_activity_at", "feedstock_snapshots"."staged_recipe_url", "feedstock_snapshots"."absence_established", "feedstock_snapshots"."detail", "feedstock_snapshots"."trace_id" FROM "feedstock_snapshots" WHERE ("feedstock_snapshots"."observed_at" <= '2026-09-13 11:00:00+00:00'::timestamptz AND "feedstock_snapshots"."package_id" = 5001) ORDER BY "feedstock_snapshots"."observed_at" DESC, "feedstock_snapshots"."id" DESC LIMIT 1
Limit  (cost=4.59..8.65 rows=1 width=162) (actual time=0.005..0.005 rows=1 loops=1)
  Buffers: shared hit=5
  ->  Incremental Sort  (cost=4.59..370.15 rows=90 width=162) (actual time=0.004..0.004 rows=1 loops=1)
        Sort Key: observed_at DESC, id DESC
        Presorted Key: observed_at
        Full-sort Groups: 1  Sort Method: quicksort  Average Memory: 25kB  Peak Memory: 25kB
        Buffers: shared hit=5
        ->  Index Scan using feedstock_pkg_observed on feedstock_snapshots  (cost=0.42..366.17 rows=90 width=162) (actual time=0.003..0.003 rows=2 loops=1)
              Index Cond: ((package_id = 5001) AND (observed_at <= '2026-09-13 11:00:00+00'::timestamp with time zone))
              Buffers: shared hit=5
Planning:
  Buffers: shared hit=4
Planning Time: 0.022 ms
Execution Time: 0.008 ms

--- rollup.py314.current_assessment (package, series)
SQL: SELECT "python_readiness_assessments"."id", "python_readiness_assessments"."observed_at", "python_readiness_assessments"."package_id", "python_readiness_assessments"."source", "python_readiness_assessments"."state", "python_readiness_assessments"."python_series", "python_readiness_assessments"."requires_python", "python_readiness_assessments"."matching_classifier", "python_readiness_assessments"."deciding_signal", "python_readiness_assessments"."detail", "python_readiness_assessments"."trace_id" FROM "python_readiness_assessments" WHERE ("python_readiness_assessments"."observed_at" <= '2026-09-13 11:00:00+00:00'::timestamptz AND "python_readiness_assessments"."package_id" = 5001 AND "python_readiness_assessments"."python_series" = '3.14') ORDER BY "python_readiness_assessments"."observed_at" DESC, "python_readiness_assessments"."id" DESC LIMIT 1
Limit  (cost=8.66..16.66 rows=1 width=143) (actual time=0.005..0.005 rows=1 loops=1)
  Buffers: shared hit=5
  ->  Incremental Sort  (cost=8.66..728.37 rows=90 width=143) (actual time=0.005..0.005 rows=1 loops=1)
        Sort Key: observed_at DESC, id DESC
        Presorted Key: observed_at
        Full-sort Groups: 1  Sort Method: quicksort  Average Memory: 25kB  Peak Memory: 25kB
        Buffers: shared hit=5
        ->  Index Scan using py_readiness_pkg_observed on python_readiness_assessments  (cost=0.43..724.39 rows=90 width=143) (actual time=0.003..0.004 rows=2 loops=1)
              Index Cond: ((package_id = 5001) AND (observed_at <= '2026-09-13 11:00:00+00'::timestamp with time zone))
              Filter: ((python_series)::text = '3.14'::text)
              Rows Removed by Filter: 2
              Buffers: shared hit=5
Planning:
  Buffers: shared hit=4
Planning Time: 0.023 ms
Execution Time: 0.008 ms

--- rollup.py314.current_verification (package, series)
SQL: SELECT "python_verification_results"."id", "python_verification_results"."observed_at", "python_verification_results"."package_id", "python_verification_results"."source", "python_verification_results"."state", "python_verification_results"."python_series", "python_verification_results"."platform", "python_verification_results"."architecture", "python_verification_results"."log_reference", "python_verification_results"."detail", "python_verification_results"."trace_id" FROM "python_verification_results" WHERE ("python_verification_results"."observed_at" <= '2026-09-13 11:00:00+00:00'::timestamptz AND "python_verification_results"."package_id" = 5001 AND "python_verification_results"."python_series" = '3.14') ORDER BY "python_verification_results"."observed_at" DESC, "python_verification_results"."id" DESC LIMIT 1
Limit  (cost=8.71..16.75 rows=1 width=133) (actual time=0.005..0.005 rows=1 loops=1)
  Buffers: shared hit=5
  ->  Incremental Sort  (cost=8.71..732.35 rows=90 width=133) (actual time=0.005..0.005 rows=1 loops=1)
        Sort Key: observed_at DESC, id DESC
        Presorted Key: observed_at
        Full-sort Groups: 1  Sort Method: quicksort  Average Memory: 25kB  Peak Memory: 25kB
        Buffers: shared hit=5
        ->  Index Scan using py_verify_pkg_observed on python_verification_results  (cost=0.43..728.37 rows=90 width=133) (actual time=0.004..0.004 rows=2 loops=1)
              Index Cond: ((package_id = 5001) AND (observed_at <= '2026-09-13 11:00:00+00'::timestamp with time zone))
              Filter: ((python_series)::text = '3.14'::text)
              Rows Removed by Filter: 2
              Buffers: shared hit=5
Planning:
  Buffers: shared hit=4
Planning Time: 0.023 ms
Execution Time: 0.008 ms

--- rollup.vulnerability.current_findings #1
SQL: SELECT "vulnerability_findings"."observed_at" AS "observed_at" FROM "vulnerability_findings" WHERE ("vulnerability_findings"."observed_at" <= '2026-09-13 11:00:00+00:00'::timestamptz AND "vulnerability_findings"."package_id" = 5001) ORDER BY 1 DESC, "vulnerability_findings"."id" DESC LIMIT 1
Limit  (cost=4.64..8.71 rows=1 width=16) (actual time=0.009..0.009 rows=1 loops=1)
  Buffers: shared hit=5
  ->  Incremental Sort  (cost=4.64..731.78 rows=179 width=16) (actual time=0.009..0.009 rows=1 loops=1)
        Sort Key: observed_at DESC, id DESC
        Presorted Key: observed_at
        Full-sort Groups: 1  Sort Method: quicksort  Average Memory: 25kB  Peak Memory: 25kB
        Buffers: shared hit=5
        ->  Index Scan using vuln_finding_pkg_observed on vulnerability_findings  (cost=0.43..723.97 rows=179 width=16) (actual time=0.004..0.004 rows=3 loops=1)
              Index Cond: ((package_id = 5001) AND (observed_at <= '2026-09-13 11:00:00+00'::timestamp with time zone))
              Buffers: shared hit=5
Planning:
  Buffers: shared hit=4
Planning Time: 0.019 ms
Execution Time: 0.011 ms

--- rollup.vulnerability.current_findings #2
SQL: SELECT "vulnerability_findings"."id", "vulnerability_findings"."observed_at", "vulnerability_findings"."package_id", "vulnerability_findings"."source", "vulnerability_findings"."state", "vulnerability_findings"."advisory_id", "vulnerability_findings"."severity", "vulnerability_findings"."affected_range", "vulnerability_findings"."fixed_range", "vulnerability_findings"."matched_version", "vulnerability_findings"."match_confidence", "vulnerability_findings"."detail", "vulnerability_findings"."trace_id" FROM "vulnerability_findings" WHERE ("vulnerability_findings"."observed_at" = '2026-09-12 21:00:00+00:00'::timestamptz AND "vulnerability_findings"."package_id" = 5001) ORDER BY "vulnerability_findings"."id" ASC
Sort  (cost=8.46..8.46 rows=1 width=116) (actual time=0.004..0.004 rows=2 loops=1)
  Sort Key: id
  Sort Method: quicksort  Memory: 25kB
  Buffers: shared hit=4
  ->  Index Scan using vuln_finding_pkg_observed on vulnerability_findings  (cost=0.43..8.45 rows=1 width=116) (actual time=0.002..0.003 rows=2 loops=1)
        Index Cond: ((package_id = 5001) AND (observed_at = '2026-09-12 21:00:00+00'::timestamp with time zone))
        Buffers: shared hit=4
Planning Time: 0.015 ms
Execution Time: 0.006 ms

--- rollup.vulnerability.current_cross_references #1
SQL: SELECT "kev_findings"."observed_at" AS "observed_at" FROM "kev_findings" WHERE ("kev_findings"."observed_at" <= '2026-09-13 11:00:00+00:00'::timestamptz AND "kev_findings"."package_id" = 5001) ORDER BY 1 DESC, "kev_findings"."id" DESC LIMIT 1
Limit  (cost=4.64..8.70 rows=1 width=16) (actual time=0.004..0.004 rows=1 loops=1)
  Buffers: shared hit=5
  ->  Incremental Sort  (cost=4.64..735.81 rows=180 width=16) (actual time=0.004..0.004 rows=1 loops=1)
        Sort Key: observed_at DESC, id DESC
        Presorted Key: observed_at
        Full-sort Groups: 1  Sort Method: quicksort  Average Memory: 25kB  Peak Memory: 25kB
        Buffers: shared hit=5
        ->  Index Scan using kev_finding_pkg_observed on kev_findings  (cost=0.43..727.95 rows=180 width=16) (actual time=0.003..0.003 rows=3 loops=1)
              Index Cond: ((package_id = 5001) AND (observed_at <= '2026-09-13 11:00:00+00'::timestamp with time zone))
              Buffers: shared hit=5
Planning:
  Buffers: shared hit=4
Planning Time: 0.019 ms
Execution Time: 0.007 ms

--- rollup.vulnerability.current_cross_references #2
SQL: SELECT "kev_findings"."id", "kev_findings"."observed_at", "kev_findings"."package_id", "kev_findings"."vulnerability_finding_id", "kev_findings"."source", "kev_findings"."state", "kev_findings"."catalog_date_added", "kev_findings"."detail", "kev_findings"."trace_id" FROM "kev_findings" WHERE ("kev_findings"."observed_at" = '2026-09-12 21:00:00+00:00'::timestamptz AND "kev_findings"."package_id" = 5001) ORDER BY "kev_findings"."id" ASC
Sort  (cost=8.46..8.46 rows=1 width=74) (actual time=0.004..0.004 rows=2 loops=1)
  Sort Key: id
  Sort Method: quicksort  Memory: 25kB
  Buffers: shared hit=4
  ->  Index Scan using kev_finding_pkg_observed on kev_findings  (cost=0.43..8.45 rows=1 width=74) (actual time=0.002..0.003 rows=2 loops=1)
        Index Cond: ((package_id = 5001) AND (observed_at = '2026-09-12 21:00:00+00'::timestamp with time zone))
        Buffers: shared hit=4
Planning Time: 0.017 ms
Execution Time: 0.007 ms

--- rollup.licence.current_findings #1
SQL: SELECT "license_findings"."observed_at" AS "observed_at" FROM "license_findings" WHERE ("license_findings"."observed_at" <= '2026-09-13 11:00:00+00:00'::timestamptz AND "license_findings"."package_id" = 5001) ORDER BY 1 DESC, "license_findings"."id" DESC LIMIT 1
Limit  (cost=4.64..8.71 rows=1 width=16) (actual time=0.005..0.005 rows=1 loops=1)
  Buffers: shared hit=5
  ->  Incremental Sort  (cost=4.64..735.87 rows=180 width=16) (actual time=0.004..0.005 rows=1 loops=1)
        Sort Key: observed_at DESC, id DESC
        Presorted Key: observed_at
        Full-sort Groups: 1  Sort Method: quicksort  Average Memory: 25kB  Peak Memory: 25kB
        Buffers: shared hit=5
        ->  Index Scan using license_finding_pkg_observed on license_findings  (cost=0.43..728.02 rows=180 width=16) (actual time=0.003..0.003 rows=3 loops=1)
              Index Cond: ((package_id = 5001) AND (observed_at <= '2026-09-13 11:00:00+00'::timestamp with time zone))
              Buffers: shared hit=5
Planning:
  Buffers: shared hit=4
Planning Time: 0.019 ms
Execution Time: 0.007 ms

--- rollup.licence.current_findings #2
SQL: SELECT "license_findings"."id", "license_findings"."observed_at", "license_findings"."package_id", "license_findings"."source", "license_findings"."state", "license_findings"."channel", "license_findings"."raw_license", "license_findings"."normalized_license", "license_findings"."detection_method", "license_findings"."detail", "license_findings"."trace_id" FROM "license_findings" WHERE ("license_findings"."observed_at" = '2026-09-12 21:00:00+00:00'::timestamptz AND "license_findings"."package_id" = 5001) ORDER BY "license_findings"."id" ASC
Sort  (cost=8.46..8.46 rows=1 width=102) (actual time=0.004..0.004 rows=2 loops=1)
  Sort Key: id
  Sort Method: quicksort  Memory: 25kB
  Buffers: shared hit=4
  ->  Index Scan using license_finding_pkg_observed on license_findings  (cost=0.43..8.45 rows=1 width=102) (actual time=0.002..0.003 rows=2 loops=1)
        Index Cond: ((package_id = 5001) AND (observed_at = '2026-09-12 21:00:00+00'::timestamp with time zone))
        Buffers: shared hit=4
Planning Time: 0.015 ms
Execution Time: 0.006 ms

--- rollup.remediation.current_findings #1
SQL: SELECT "vulnerability_findings"."observed_at" AS "observed_at" FROM "vulnerability_findings" WHERE ("vulnerability_findings"."observed_at" <= '2026-09-13 11:00:00+00:00'::timestamptz AND "vulnerability_findings"."package_id" = 5001) ORDER BY 1 DESC, "vulnerability_findings"."id" DESC LIMIT 1
Limit  (cost=4.64..8.71 rows=1 width=16) (actual time=0.005..0.005 rows=1 loops=1)
  Buffers: shared hit=5
  ->  Incremental Sort  (cost=4.64..731.78 rows=179 width=16) (actual time=0.004..0.005 rows=1 loops=1)
        Sort Key: observed_at DESC, id DESC
        Presorted Key: observed_at
        Full-sort Groups: 1  Sort Method: quicksort  Average Memory: 25kB  Peak Memory: 25kB
        Buffers: shared hit=5
        ->  Index Scan using vuln_finding_pkg_observed on vulnerability_findings  (cost=0.43..723.97 rows=179 width=16) (actual time=0.003..0.003 rows=3 loops=1)
              Index Cond: ((package_id = 5001) AND (observed_at <= '2026-09-13 11:00:00+00'::timestamp with time zone))
              Buffers: shared hit=5
Planning:
  Buffers: shared hit=4
Planning Time: 0.019 ms
Execution Time: 0.007 ms

--- rollup.remediation.current_findings #2
SQL: SELECT "vulnerability_findings"."id", "vulnerability_findings"."observed_at", "vulnerability_findings"."package_id", "vulnerability_findings"."source", "vulnerability_findings"."state", "vulnerability_findings"."advisory_id", "vulnerability_findings"."severity", "vulnerability_findings"."affected_range", "vulnerability_findings"."fixed_range", "vulnerability_findings"."matched_version", "vulnerability_findings"."match_confidence", "vulnerability_findings"."detail", "vulnerability_findings"."trace_id" FROM "vulnerability_findings" WHERE ("vulnerability_findings"."observed_at" = '2026-09-12 21:00:00+00:00'::timestamptz AND "vulnerability_findings"."package_id" = 5001) ORDER BY "vulnerability_findings"."id" ASC
Sort  (cost=8.46..8.46 rows=1 width=116) (actual time=0.004..0.004 rows=2 loops=1)
  Sort Key: id
  Sort Method: quicksort  Memory: 25kB
  Buffers: shared hit=4
  ->  Index Scan using vuln_finding_pkg_observed on vulnerability_findings  (cost=0.43..8.45 rows=1 width=116) (actual time=0.002..0.002 rows=2 loops=1)
        Index Cond: ((package_id = 5001) AND (observed_at = '2026-09-12 21:00:00+00'::timestamp with time zone))
        Buffers: shared hit=4
Planning Time: 0.015 ms
Execution Time: 0.006 ms

--- rollup.remediation.read_surface: source #1
SQL: SELECT "source_release_snapshots"."observed_at" AS "observed_at" FROM "source_release_snapshots" WHERE ("source_release_snapshots"."observed_at" <= '2026-09-13 11:00:00+00:00'::timestamptz AND "source_release_snapshots"."package_id" = 5001) ORDER BY 1 DESC, "source_release_snapshots"."id" DESC LIMIT 1
Limit  (cost=4.59..8.65 rows=1 width=16) (actual time=0.004..0.004 rows=1 loops=1)
  Buffers: shared hit=5
  ->  Incremental Sort  (cost=4.59..370.19 rows=90 width=16) (actual time=0.004..0.004 rows=1 loops=1)
        Sort Key: observed_at DESC, id DESC
        Presorted Key: observed_at
        Full-sort Groups: 1  Sort Method: quicksort  Average Memory: 25kB  Peak Memory: 25kB
        Buffers: shared hit=5
        ->  Index Scan using src_release_pkg_observed on source_release_snapshots  (cost=0.42..366.21 rows=90 width=16) (actual time=0.003..0.003 rows=2 loops=1)
              Index Cond: ((package_id = 5001) AND (observed_at <= '2026-09-13 11:00:00+00'::timestamp with time zone))
              Buffers: shared hit=5
Planning:
  Buffers: shared hit=4
Planning Time: 0.018 ms
Execution Time: 0.006 ms

--- rollup.remediation.read_surface: source #2
SQL: SELECT "source_release_snapshots"."id", "source_release_snapshots"."observed_at", "source_release_snapshots"."package_id", "source_release_snapshots"."source", "source_release_snapshots"."state", "source_release_snapshots"."latest_version", "source_release_snapshots"."released_at", "source_release_snapshots"."last_activity_at", "source_release_snapshots"."releases_seen", "source_release_snapshots"."detail", "source_release_snapshots"."trace_id" FROM "source_release_snapshots" WHERE ("source_release_snapshots"."observed_at" = '2026-09-12 21:00:00+00:00'::timestamptz AND "source_release_snapshots"."package_id" = 5001) ORDER BY "source_release_snapshots"."id" ASC
Sort  (cost=8.46..8.46 rows=1 width=86) (actual time=0.004..0.004 rows=1 loops=1)
  Sort Key: id
  Sort Method: quicksort  Memory: 25kB
  Buffers: shared hit=4
  ->  Index Scan using src_release_pkg_observed on source_release_snapshots  (cost=0.42..8.45 rows=1 width=86) (actual time=0.003..0.003 rows=1 loops=1)
        Index Cond: ((package_id = 5001) AND (observed_at = '2026-09-12 21:00:00+00'::timestamp with time zone))
        Buffers: shared hit=4
Planning Time: 0.015 ms
Execution Time: 0.006 ms

--- rollup.remediation.read_surface: pypi #1
SQL: SELECT "pypi_release_snapshots"."observed_at" AS "observed_at" FROM "pypi_release_snapshots" WHERE ("pypi_release_snapshots"."observed_at" <= '2026-09-13 11:00:00+00:00'::timestamptz AND "pypi_release_snapshots"."package_id" = 5001) ORDER BY 1 DESC, "pypi_release_snapshots"."id" DESC LIMIT 1
Limit  (cost=4.59..8.65 rows=1 width=16) (actual time=0.004..0.004 rows=1 loops=1)
  Buffers: shared hit=5
  ->  Incremental Sort  (cost=4.59..370.15 rows=90 width=16) (actual time=0.004..0.004 rows=1 loops=1)
        Sort Key: observed_at DESC, id DESC
        Presorted Key: observed_at
        Full-sort Groups: 1  Sort Method: quicksort  Average Memory: 25kB  Peak Memory: 25kB
        Buffers: shared hit=5
        ->  Index Scan using pypi_release_pkg_observed on pypi_release_snapshots  (cost=0.42..366.17 rows=90 width=16) (actual time=0.003..0.003 rows=2 loops=1)
              Index Cond: ((package_id = 5001) AND (observed_at <= '2026-09-13 11:00:00+00'::timestamp with time zone))
              Buffers: shared hit=5
Planning:
  Buffers: shared hit=4
Planning Time: 0.017 ms
Execution Time: 0.006 ms

--- rollup.remediation.read_surface: pypi #2
SQL: SELECT "pypi_release_snapshots"."id", "pypi_release_snapshots"."observed_at", "pypi_release_snapshots"."package_id", "pypi_release_snapshots"."source", "pypi_release_snapshots"."state", "pypi_release_snapshots"."latest_version", "pypi_release_snapshots"."released_at", "pypi_release_snapshots"."requires_python", "pypi_release_snapshots"."detail", "pypi_release_snapshots"."trace_id" FROM "pypi_release_snapshots" WHERE ("pypi_release_snapshots"."observed_at" = '2026-09-12 21:00:00+00:00'::timestamptz AND "pypi_release_snapshots"."package_id" = 5001) ORDER BY "pypi_release_snapshots"."id" ASC
Sort  (cost=8.46..8.46 rows=1 width=80) (actual time=0.003..0.003 rows=1 loops=1)
  Sort Key: id
  Sort Method: quicksort  Memory: 25kB
  Buffers: shared hit=4
  ->  Index Scan using pypi_release_pkg_observed on pypi_release_snapshots  (cost=0.42..8.45 rows=1 width=80) (actual time=0.002..0.002 rows=1 loops=1)
        Index Cond: ((package_id = 5001) AND (observed_at = '2026-09-12 21:00:00+00'::timestamp with time zone))
        Buffers: shared hit=4
Planning Time: 0.015 ms
Execution Time: 0.006 ms

--- rollup.remediation.read_surface: feedstock #1
SQL: SELECT "feedstock_snapshots"."observed_at" AS "observed_at" FROM "feedstock_snapshots" WHERE ("feedstock_snapshots"."observed_at" <= '2026-09-13 11:00:00+00:00'::timestamptz AND "feedstock_snapshots"."package_id" = 5001) ORDER BY 1 DESC, "feedstock_snapshots"."id" DESC LIMIT 1
Limit  (cost=4.59..8.65 rows=1 width=16) (actual time=0.004..0.004 rows=1 loops=1)
  Buffers: shared hit=5
  ->  Incremental Sort  (cost=4.59..370.15 rows=90 width=16) (actual time=0.004..0.004 rows=1 loops=1)
        Sort Key: observed_at DESC, id DESC
        Presorted Key: observed_at
        Full-sort Groups: 1  Sort Method: quicksort  Average Memory: 25kB  Peak Memory: 25kB
        Buffers: shared hit=5
        ->  Index Scan using feedstock_pkg_observed on feedstock_snapshots  (cost=0.42..366.17 rows=90 width=16) (actual time=0.003..0.003 rows=2 loops=1)
              Index Cond: ((package_id = 5001) AND (observed_at <= '2026-09-13 11:00:00+00'::timestamp with time zone))
              Buffers: shared hit=5
Planning:
  Buffers: shared hit=4
Planning Time: 0.020 ms
Execution Time: 0.007 ms

--- rollup.remediation.read_surface: feedstock #2
SQL: SELECT "feedstock_snapshots"."id", "feedstock_snapshots"."observed_at", "feedstock_snapshots"."package_id", "feedstock_snapshots"."source", "feedstock_snapshots"."state", "feedstock_snapshots"."feedstock_name", "feedstock_snapshots"."feedstock_url", "feedstock_snapshots"."recipe_version", "feedstock_snapshots"."recipe_build_number", "feedstock_snapshots"."recipe_metadata_url", "feedstock_snapshots"."last_recipe_activity_at", "feedstock_snapshots"."staged_recipe_url", "feedstock_snapshots"."absence_established", "feedstock_snapshots"."detail", "feedstock_snapshots"."trace_id" FROM "feedstock_snapshots" WHERE ("feedstock_snapshots"."observed_at" = '2026-09-12 21:00:00+00:00'::timestamptz AND "feedstock_snapshots"."package_id" = 5001) ORDER BY "feedstock_snapshots"."id" ASC
Sort  (cost=8.46..8.46 rows=1 width=162) (actual time=0.004..0.004 rows=1 loops=1)
  Sort Key: id
  Sort Method: quicksort  Memory: 25kB
  Buffers: shared hit=4
  ->  Index Scan using feedstock_pkg_observed on feedstock_snapshots  (cost=0.42..8.45 rows=1 width=162) (actual time=0.002..0.003 rows=1 loops=1)
        Index Cond: ((package_id = 5001) AND (observed_at = '2026-09-12 21:00:00+00'::timestamp with time zone))
        Buffers: shared hit=4
Planning Time: 0.016 ms
Execution Time: 0.007 ms

--- rollup.remediation.read_surface: conda_package #1
SQL: SELECT "conda_package_snapshots"."observed_at" AS "observed_at" FROM "conda_package_snapshots" WHERE ("conda_package_snapshots"."observed_at" <= '2026-09-13 11:00:00+00:00'::timestamptz AND "conda_package_snapshots"."package_id" = 5001) ORDER BY 1 DESC, "conda_package_snapshots"."id" DESC LIMIT 1
Limit  (cost=4.64..8.71 rows=1 width=16) (actual time=0.005..0.005 rows=1 loops=1)
  Buffers: shared hit=5
  ->  Incremental Sort  (cost=4.64..731.77 rows=179 width=16) (actual time=0.005..0.005 rows=1 loops=1)
        Sort Key: observed_at DESC, id DESC
        Presorted Key: observed_at
        Full-sort Groups: 1  Sort Method: quicksort  Average Memory: 25kB  Peak Memory: 25kB
        Buffers: shared hit=5
        ->  Index Scan using conda_pkg_pkg_observed on conda_package_snapshots  (cost=0.43..723.96 rows=179 width=16) (actual time=0.003..0.004 rows=3 loops=1)
              Index Cond: ((package_id = 5001) AND (observed_at <= '2026-09-13 11:00:00+00'::timestamp with time zone))
              Buffers: shared hit=5
Planning:
  Buffers: shared hit=4
Planning Time: 0.023 ms
Execution Time: 0.008 ms

--- rollup.remediation.read_surface: conda_package #2
SQL: SELECT "conda_package_snapshots"."id", "conda_package_snapshots"."observed_at", "conda_package_snapshots"."package_id", "conda_package_snapshots"."source", "conda_package_snapshots"."state", "conda_package_snapshots"."channel", "conda_package_snapshots"."platform", "conda_package_snapshots"."published_version", "conda_package_snapshots"."build_string", "conda_package_snapshots"."build_number", "conda_package_snapshots"."detail", "conda_package_snapshots"."trace_id" FROM "conda_package_snapshots" WHERE ("conda_package_snapshots"."observed_at" = '2026-09-12 21:00:00+00:00'::timestamptz AND "conda_package_snapshots"."package_id" = 5001) ORDER BY "conda_package_snapshots"."id" ASC
Sort  (cost=8.46..8.46 rows=1 width=92) (actual time=0.004..0.005 rows=2 loops=1)
  Sort Key: id
  Sort Method: quicksort  Memory: 25kB
  Buffers: shared hit=4
  ->  Index Scan using conda_pkg_pkg_observed on conda_package_snapshots  (cost=0.43..8.45 rows=1 width=92) (actual time=0.003..0.003 rows=2 loops=1)
        Index Cond: ((package_id = 5001) AND (observed_at = '2026-09-12 21:00:00+00'::timestamp with time zone))
        Buffers: shared hit=4
Planning Time: 0.019 ms
Execution Time: 0.008 ms

--- rollup.freshness.latest_observation: inventory_snapshots
SQL: SELECT MAX("inventory_snapshots"."observed_at") AS "newest" FROM "inventory_snapshots" WHERE "inventory_snapshots"."package_id" = 5001
Result  (cost=0.47..0.48 rows=1 width=8) (actual time=0.005..0.005 rows=1 loops=1)
  Buffers: shared hit=4
  InitPlan 1
    ->  Limit  (cost=0.43..0.47 rows=1 width=8) (actual time=0.005..0.005 rows=1 loops=1)
          Buffers: shared hit=4
          ->  Index Only Scan using inv_snapshot_pkg_observed on inventory_snapshots  (cost=0.43..7.60 rows=181 width=8) (actual time=0.004..0.004 rows=1 loops=1)
                Index Cond: (package_id = 5001)
                Heap Fetches: 0
                Buffers: shared hit=4
Planning Time: 0.025 ms
Execution Time: 0.008 ms

--- rollup.freshness.latest_observation: source_release_snapshots
SQL: SELECT MAX("source_release_snapshots"."observed_at") AS "newest" FROM "source_release_snapshots" WHERE "source_release_snapshots"."package_id" = 5001
Result  (cost=0.49..0.50 rows=1 width=8) (actual time=0.004..0.004 rows=1 loops=1)
  Buffers: shared hit=4
  InitPlan 1
    ->  Limit  (cost=0.42..0.49 rows=1 width=8) (actual time=0.003..0.003 rows=1 loops=1)
          Buffers: shared hit=4
          ->  Index Only Scan using src_release_pkg_observed on source_release_snapshots  (cost=0.42..6.02 rows=91 width=8) (actual time=0.003..0.003 rows=1 loops=1)
                Index Cond: (package_id = 5001)
                Heap Fetches: 0
                Buffers: shared hit=4
Planning Time: 0.020 ms
Execution Time: 0.007 ms

--- rollup.freshness.latest_observation: pypi_release_snapshots
SQL: SELECT MAX("pypi_release_snapshots"."observed_at") AS "newest" FROM "pypi_release_snapshots" WHERE "pypi_release_snapshots"."package_id" = 5001
Result  (cost=0.49..0.50 rows=1 width=8) (actual time=0.004..0.004 rows=1 loops=1)
  Buffers: shared hit=4
  InitPlan 1
    ->  Limit  (cost=0.42..0.49 rows=1 width=8) (actual time=0.003..0.004 rows=1 loops=1)
          Buffers: shared hit=4
          ->  Index Only Scan using pypi_release_pkg_observed on pypi_release_snapshots  (cost=0.42..6.02 rows=91 width=8) (actual time=0.003..0.003 rows=1 loops=1)
                Index Cond: (package_id = 5001)
                Heap Fetches: 0
                Buffers: shared hit=4
Planning Time: 0.021 ms
Execution Time: 0.006 ms

--- rollup.freshness.latest_observation: feedstock_snapshots
SQL: SELECT MAX("feedstock_snapshots"."observed_at") AS "newest" FROM "feedstock_snapshots" WHERE "feedstock_snapshots"."package_id" = 5001
Result  (cost=0.49..0.50 rows=1 width=8) (actual time=0.004..0.004 rows=1 loops=1)
  Buffers: shared hit=4
  InitPlan 1
    ->  Limit  (cost=0.42..0.49 rows=1 width=8) (actual time=0.003..0.003 rows=1 loops=1)
          Buffers: shared hit=4
          ->  Index Only Scan using feedstock_pkg_observed on feedstock_snapshots  (cost=0.42..6.02 rows=91 width=8) (actual time=0.003..0.003 rows=1 loops=1)
                Index Cond: (package_id = 5001)
                Heap Fetches: 0
                Buffers: shared hit=4
Planning Time: 0.019 ms
Execution Time: 0.006 ms

--- rollup.freshness.latest_observation: conda_package_snapshots
SQL: SELECT MAX("conda_package_snapshots"."observed_at") AS "newest" FROM "conda_package_snapshots" WHERE "conda_package_snapshots"."package_id" = 5001
Result  (cost=0.47..0.48 rows=1 width=8) (actual time=0.004..0.005 rows=1 loops=1)
  Buffers: shared hit=4
  InitPlan 1
    ->  Limit  (cost=0.43..0.47 rows=1 width=8) (actual time=0.004..0.004 rows=1 loops=1)
          Buffers: shared hit=4
          ->  Index Only Scan using conda_pkg_pkg_observed on conda_package_snapshots  (cost=0.43..7.60 rows=181 width=8) (actual time=0.004..0.004 rows=1 loops=1)
                Index Cond: (package_id = 5001)
                Heap Fetches: 0
                Buffers: shared hit=4
Planning Time: 0.021 ms
Execution Time: 0.007 ms

--- rollup.freshness.latest_observation: kev_findings
SQL: SELECT MAX("kev_findings"."observed_at") AS "newest" FROM "kev_findings" WHERE "kev_findings"."package_id" = 5001
Result  (cost=0.47..0.48 rows=1 width=8) (actual time=0.004..0.004 rows=1 loops=1)
  Buffers: shared hit=4
  InitPlan 1
    ->  Limit  (cost=0.43..0.47 rows=1 width=8) (actual time=0.004..0.004 rows=1 loops=1)
          Buffers: shared hit=4
          ->  Index Only Scan using kev_finding_pkg_observed on kev_findings  (cost=0.43..7.63 rows=183 width=8) (actual time=0.004..0.004 rows=1 loops=1)
                Index Cond: (package_id = 5001)
                Heap Fetches: 0
                Buffers: shared hit=4
Planning Time: 0.019 ms
Execution Time: 0.007 ms

--- rollup.freshness.latest_observation: vulnerability_findings
SQL: SELECT MAX("vulnerability_findings"."observed_at") AS "newest" FROM "vulnerability_findings" WHERE "vulnerability_findings"."package_id" = 5001
Result  (cost=0.47..0.48 rows=1 width=8) (actual time=0.004..0.004 rows=1 loops=1)
  Buffers: shared hit=4
  InitPlan 1
    ->  Limit  (cost=0.43..0.47 rows=1 width=8) (actual time=0.003..0.004 rows=1 loops=1)
          Buffers: shared hit=4
          ->  Index Only Scan using vuln_finding_pkg_observed on vulnerability_findings  (cost=0.43..7.60 rows=181 width=8) (actual time=0.003..0.003 rows=1 loops=1)
                Index Cond: (package_id = 5001)
                Heap Fetches: 0
                Buffers: shared hit=4
Planning Time: 0.017 ms
Execution Time: 0.006 ms

--- rollup.freshness.latest_observation: license_findings
SQL: SELECT MAX("license_findings"."observed_at") AS "newest" FROM "license_findings" WHERE "license_findings"."package_id" = 5001
Result  (cost=0.47..0.48 rows=1 width=8) (actual time=0.003..0.003 rows=1 loops=1)
  Buffers: shared hit=4
  InitPlan 1
    ->  Limit  (cost=0.43..0.47 rows=1 width=8) (actual time=0.003..0.003 rows=1 loops=1)
          Buffers: shared hit=4
          ->  Index Only Scan using license_finding_pkg_observed on license_findings  (cost=0.43..7.61 rows=182 width=8) (actual time=0.003..0.003 rows=1 loops=1)
                Index Cond: (package_id = 5001)
                Heap Fetches: 0
                Buffers: shared hit=4
Planning Time: 0.016 ms
Execution Time: 0.006 ms

--- rollup.freshness.latest_observation: python_readiness_assessments
SQL: SELECT MAX("python_readiness_assessments"."observed_at") AS "newest" FROM "python_readiness_assessments" WHERE "python_readiness_assessments"."package_id" = 5001
Result  (cost=0.47..0.48 rows=1 width=8) (actual time=0.005..0.005 rows=1 loops=1)
  Buffers: shared hit=4
  InitPlan 1
    ->  Limit  (cost=0.43..0.47 rows=1 width=8) (actual time=0.004..0.004 rows=1 loops=1)
          Buffers: shared hit=4
          ->  Index Only Scan using py_readiness_pkg_observed on python_readiness_assessments  (cost=0.43..7.61 rows=182 width=8) (actual time=0.004..0.004 rows=1 loops=1)
                Index Cond: (package_id = 5001)
                Heap Fetches: 0
                Buffers: shared hit=4
Planning Time: 0.019 ms
Execution Time: 0.007 ms

--- rollup.freshness.latest_observation: python_verification_results
SQL: SELECT MAX("python_verification_results"."observed_at") AS "newest" FROM "python_verification_results" WHERE "python_verification_results"."package_id" = 5001
Result  (cost=0.47..0.48 rows=1 width=8) (actual time=0.004..0.004 rows=1 loops=1)
  Buffers: shared hit=4
  InitPlan 1
    ->  Limit  (cost=0.43..0.47 rows=1 width=8) (actual time=0.004..0.004 rows=1 loops=1)
          Buffers: shared hit=4
          ->  Index Only Scan using py_verify_pkg_observed on python_verification_results  (cost=0.43..7.61 rows=182 width=8) (actual time=0.004..0.004 rows=1 loops=1)
                Index Cond: (package_id = 5001)
                Heap Fetches: 0
                Buffers: shared hit=4
Planning Time: 0.018 ms
Execution Time: 0.006 ms

--- rollup.freshness.latest_observation: identity_resolution_snapshots
SQL: SELECT MAX("identity_resolution_snapshots"."observed_at") AS "newest" FROM "identity_resolution_snapshots" WHERE "identity_resolution_snapshots"."package_id" = 5001
Result  (cost=0.49..0.50 rows=1 width=8) (actual time=0.003..0.003 rows=1 loops=1)
  Buffers: shared hit=4
  InitPlan 1
    ->  Limit  (cost=0.42..0.49 rows=1 width=8) (actual time=0.003..0.003 rows=1 loops=1)
          Buffers: shared hit=4
          ->  Index Only Scan using id_resolution_pkg_observed on identity_resolution_snapshots  (cost=0.42..6.02 rows=91 width=8) (actual time=0.003..0.003 rows=1 loops=1)
                Index Cond: (package_id = 5001)
                Heap Fetches: 0
                Buffers: shared hit=4
Planning Time: 0.017 ms
Execution Time: 0.005 ms

--- rollup.choose_evidence_cutoff #1
SQL: SELECT MIN("collection_runs"."started_at") AS "started_at__min" FROM "collection_runs" WHERE (NOT ("collection_runs"."collector" = 'prune_evidence') AND "collection_runs"."finished_at" IS NULL)
Aggregate  (cost=4.46..4.47 rows=1 width=8) (actual time=0.004..0.004 rows=1 loops=1)
  Buffers: shared hit=4
  ->  Index Scan using collection_runs_finished on collection_runs  (cost=0.43..4.46 rows=1 width=8) (actual time=0.002..0.003 rows=9 loops=1)
        Index Cond: (finished_at IS NULL)
        Filter: ((collector)::text <> 'prune_evidence'::text)
        Buffers: shared hit=4
Planning Time: 0.019 ms
Execution Time: 0.007 ms

--- rollup.choose_evidence_cutoff #2
SQL: SELECT "collection_runs"."finished_at" AS "finished_at" FROM "collection_runs" WHERE (NOT ("collection_runs"."collector" = 'prune_evidence') AND "collection_runs"."finished_at" IS NOT NULL AND "collection_runs"."finished_at" <= '2026-06-16 00:00:00+00:00'::timestamptz) ORDER BY 1 DESC LIMIT 1
Limit  (cost=0.43..0.48 rows=1 width=8) (actual time=0.003..0.003 rows=1 loops=1)
  Buffers: shared hit=4
  ->  Index Scan Backward using collection_runs_finished on collection_runs  (cost=0.43..2349.09 rows=54564 width=8) (actual time=0.003..0.003 rows=1 loops=1)
        Index Cond: ((finished_at IS NOT NULL) AND (finished_at <= '2026-06-16 00:00:00+00'::timestamp with time zone))
        Filter: ((collector)::text <> 'prune_evidence'::text)
        Buffers: shared hit=4
Planning:
  Buffers: shared hit=4
Planning Time: 0.018 ms
Execution Time: 0.005 ms

--- rollup.choose_evidence_cutoff (refusal path) #1
SQL: SELECT MIN("collection_runs"."started_at") AS "started_at__min" FROM "collection_runs" WHERE (NOT ("collection_runs"."collector" = 'prune_evidence') AND "collection_runs"."finished_at" IS NULL)
Aggregate  (cost=4.46..4.47 rows=1 width=8) (actual time=0.004..0.004 rows=1 loops=1)
  Buffers: shared hit=4
  ->  Index Scan using collection_runs_finished on collection_runs  (cost=0.43..4.46 rows=1 width=8) (actual time=0.002..0.003 rows=10 loops=1)
        Index Cond: (finished_at IS NULL)
        Filter: ((collector)::text <> 'prune_evidence'::text)
        Buffers: shared hit=4
Planning Time: 0.019 ms
Execution Time: 0.007 ms

--- rollup.choose_evidence_cutoff (refusal path) #2
SQL: SELECT "collection_runs"."finished_at" AS "finished_at" FROM "collection_runs" WHERE (NOT ("collection_runs"."collector" = 'prune_evidence') AND "collection_runs"."finished_at" IS NOT NULL AND "collection_runs"."finished_at" <= '2026-06-14 12:00:00+00:00'::timestamptz) ORDER BY 1 DESC LIMIT 1
Limit  (cost=0.43..4.46 rows=1 width=8) (actual time=0.001..0.001 rows=0 loops=1)
  Buffers: shared hit=3
  ->  Index Scan Backward using collection_runs_finished on collection_runs  (cost=0.43..4.46 rows=1 width=8) (actual time=0.001..0.001 rows=0 loops=1)
        Index Cond: ((finished_at IS NOT NULL) AND (finished_at <= '2026-06-14 12:00:00+00'::timestamp with time zone))
        Filter: ((collector)::text <> 'prune_evidence'::text)
        Buffers: shared hit=3
Planning:
  Buffers: shared hit=4
Planning Time: 0.018 ms
Execution Time: 0.003 ms

--- rollup.choose_evidence_cutoff (refusal path) #3
SQL: SELECT COUNT(*) AS "__count" FROM "collection_runs" WHERE (NOT ("collection_runs"."collector" = 'prune_evidence') AND "collection_runs"."finished_at" IS NOT NULL AND "collection_runs"."finished_at" <= '2026-06-14 12:00:00+00:00'::timestamptz)
Aggregate  (cost=4.47..4.48 rows=1 width=8) (actual time=0.001..0.001 rows=1 loops=1)
  Buffers: shared hit=3
  ->  Index Scan using collection_runs_finished on collection_runs  (cost=0.43..4.46 rows=1 width=0) (actual time=0.001..0.001 rows=0 loops=1)
        Index Cond: ((finished_at IS NOT NULL) AND (finished_at <= '2026-06-14 12:00:00+00'::timestamp with time zone))
        Filter: ((collector)::text <> 'prune_evidence'::text)
        Buffers: shared hit=3
Planning:
  Buffers: shared hit=4
Planning Time: 0.016 ms
Execution Time: 0.004 ms

--- page.traces_for #1
SQL: SELECT "package_currency"."id", "package_currency"."package_id", "package_currency"."policy_run_id", "package_currency"."source_status", "package_currency"."pypi_status", "package_currency"."feedstock_status", "package_currency"."conda_package_status", "package_currency"."overall_status", "package_currency"."detail", "package_currency"."chosen_authority", "package_currency"."authority_order", "package_currency"."authority_order_source", "package_currency"."source_snapshot_id", "package_currency"."pypi_snapshot_id", "package_currency"."feedstock_snapshot_id", "package_currency"."conda_package_snapshot_id" FROM "package_currency" WHERE ("package_currency"."package_id" = 5001 AND "package_currency"."policy_run_id" = 90) ORDER BY "package_currency"."id" ASC LIMIT 1
Limit  (cost=8.46..8.46 rows=1 width=114) (actual time=0.007..0.007 rows=1 loops=1)
  Buffers: shared hit=4
  ->  Sort  (cost=8.46..8.46 rows=1 width=114) (actual time=0.007..0.007 rows=1 loops=1)
        Sort Key: id
        Sort Method: quicksort  Memory: 25kB
        Buffers: shared hit=4
        ->  Index Scan using one_currency_row_per_package_per_run on package_currency  (cost=0.42..8.45 rows=1 width=114) (actual time=0.006..0.006 rows=1 loops=1)
              Index Cond: ((package_id = 5001) AND (policy_run_id = 90))
              Buffers: shared hit=4
Planning Time: 0.035 ms
Execution Time: 0.013 ms

--- page.traces_for #2
SQL: SELECT "package_vulnerability"."id", "package_vulnerability"."package_id", "package_vulnerability"."policy_run_id", "package_vulnerability"."vulnerability_status", "package_vulnerability"."kev_membership", "package_vulnerability"."risk_level", "package_vulnerability"."policy_version", "package_vulnerability"."evidence_cutoff", "package_vulnerability"."vulnerability_finding_id", "package_vulnerability"."kev_finding_id", "package_vulnerability"."detail" FROM "package_vulnerability" WHERE ("package_vulnerability"."package_id" = 5001 AND "package_vulnerability"."policy_run_id" = 90) ORDER BY "package_vulnerability"."id" ASC LIMIT 1
Limit  (cost=8.46..8.46 rows=1 width=95) (actual time=0.009..0.009 rows=1 loops=1)
  Buffers: shared hit=4
  ->  Sort  (cost=8.46..8.46 rows=1 width=95) (actual time=0.008..0.008 rows=1 loops=1)
        Sort Key: id
        Sort Method: quicksort  Memory: 25kB
        Buffers: shared hit=4
        ->  Index Scan using one_vulnerability_row_per_package_per_run on package_vulnerability  (cost=0.42..8.45 rows=1 width=95) (actual time=0.007..0.007 rows=1 loops=1)
              Index Cond: ((package_id = 5001) AND (policy_run_id = 90))
              Buffers: shared hit=4
Planning Time: 0.027 ms
Execution Time: 0.013 ms

--- page.traces_for #3
SQL: SELECT "package_vulnerability"."id", "package_vulnerability"."package_id", "package_vulnerability"."policy_run_id", "package_vulnerability"."vulnerability_status", "package_vulnerability"."kev_membership", "package_vulnerability"."risk_level", "package_vulnerability"."policy_version", "package_vulnerability"."evidence_cutoff", "package_vulnerability"."vulnerability_finding_id", "package_vulnerability"."kev_finding_id", "package_vulnerability"."detail" FROM "package_vulnerability" WHERE ("package_vulnerability"."package_id" = 5001 AND "package_vulnerability"."policy_run_id" = 90) ORDER BY "package_vulnerability"."id" ASC LIMIT 1
Limit  (cost=8.46..8.46 rows=1 width=95) (actual time=0.004..0.004 rows=1 loops=1)
  Buffers: shared hit=4
  ->  Sort  (cost=8.46..8.46 rows=1 width=95) (actual time=0.004..0.004 rows=1 loops=1)
        Sort Key: id
        Sort Method: quicksort  Memory: 25kB
        Buffers: shared hit=4
        ->  Index Scan using one_vulnerability_row_per_package_per_run on package_vulnerability  (cost=0.42..8.45 rows=1 width=95) (actual time=0.003..0.003 rows=1 loops=1)
              Index Cond: ((package_id = 5001) AND (policy_run_id = 90))
              Buffers: shared hit=4
Planning Time: 0.018 ms
Execution Time: 0.007 ms

--- page.traces_for #4
SQL: SELECT "package_license"."id", "package_license"."package_id", "package_license"."policy_run_id", "package_license"."license_outcome", "package_license"."matched_rule", "package_license"."policy_version", "package_license"."evidence_cutoff", "package_license"."license_finding_id", "package_license"."detail" FROM "package_license" WHERE ("package_license"."package_id" = 5001 AND "package_license"."policy_run_id" = 90) ORDER BY "package_license"."id" ASC LIMIT 1
Limit  (cost=8.46..8.46 rows=1 width=69) (actual time=0.017..0.017 rows=1 loops=1)
  Buffers: shared hit=4
  ->  Sort  (cost=8.46..8.46 rows=1 width=69) (actual time=0.008..0.008 rows=1 loops=1)
        Sort Key: id
        Sort Method: quicksort  Memory: 25kB
        Buffers: shared hit=4
        ->  Index Scan using one_license_row_per_package_per_run on package_license  (cost=0.42..8.45 rows=1 width=69) (actual time=0.006..0.007 rows=1 loops=1)
              Index Cond: ((package_id = 5001) AND (policy_run_id = 90))
              Buffers: shared hit=4
Planning Time: 0.021 ms
Execution Time: 0.020 ms

--- page.traces_for #5
SQL: SELECT "package_python_readiness"."id", "package_python_readiness"."package_id", "package_python_readiness"."policy_run_id", "package_python_readiness"."readiness", "package_python_readiness"."evidence_type", "package_python_readiness"."python_series", "package_python_readiness"."assessment_id", "package_python_readiness"."verification_id", "package_python_readiness"."evidence_stale", "package_python_readiness"."policy_version", "package_python_readiness"."evidence_cutoff", "package_python_readiness"."detail" FROM "package_python_readiness" WHERE ("package_python_readiness"."package_id" = 5001 AND "package_python_readiness"."policy_run_id" = 90) ORDER BY "package_python_readiness"."id" ASC LIMIT 1
Limit  (cost=8.46..8.46 rows=1 width=94) (actual time=0.007..0.007 rows=1 loops=1)
  Buffers: shared hit=4
  ->  Sort  (cost=8.46..8.46 rows=1 width=94) (actual time=0.007..0.007 rows=1 loops=1)
        Sort Key: id
        Sort Method: quicksort  Memory: 25kB
        Buffers: shared hit=4
        ->  Index Scan using one_python_readiness_row_per_package_per_run on package_python_readiness  (cost=0.42..8.45 rows=1 width=94) (actual time=0.006..0.006 rows=1 loops=1)
              Index Cond: ((package_id = 5001) AND (policy_run_id = 90))
              Buffers: shared hit=4
Planning Time: 0.023 ms
Execution Time: 0.010 ms

--- page.traces_for #6
SQL: SELECT "package_feedstock_presence"."id", "package_feedstock_presence"."package_id", "package_feedstock_presence"."policy_run_id", "package_feedstock_presence"."presence_status", "package_feedstock_presence"."inactivity_threshold", "package_feedstock_presence"."last_recipe_activity_at", "package_feedstock_presence"."activity_age", "package_feedstock_presence"."confidence", "package_feedstock_presence"."feedstock_snapshot_id", "package_feedstock_presence"."detail" FROM "package_feedstock_presence" WHERE ("package_feedstock_presence"."package_id" = 5001 AND "package_feedstock_presence"."policy_run_id" = 90) ORDER BY "package_feedstock_presence"."id" ASC LIMIT 1
Limit  (cost=8.46..8.46 rows=1 width=104) (actual time=0.008..0.008 rows=1 loops=1)
  Buffers: shared hit=4
  ->  Sort  (cost=8.46..8.46 rows=1 width=104) (actual time=0.007..0.007 rows=1 loops=1)
        Sort Key: id
        Sort Method: quicksort  Memory: 25kB
        Buffers: shared hit=4
        ->  Index Scan using one_feedstock_presence_row_per_package_per_run on package_feedstock_presence  (cost=0.42..8.45 rows=1 width=104) (actual time=0.006..0.007 rows=1 loops=1)
              Index Cond: ((package_id = 5001) AND (policy_run_id = 90))
              Buffers: shared hit=4
Planning Time: 0.022 ms
Execution Time: 0.011 ms

--- page.traces_for #7
SQL: SELECT "package_priority"."id", "package_priority"."package_id", "package_priority"."policy_run_id", "package_priority"."bucket", "package_priority"."bucket_description", "package_priority"."matched_rule", "package_priority"."reason", "package_priority"."score", "package_priority"."policy_version", "package_priority"."evidence_cutoff", "package_priority"."detail" FROM "package_priority" WHERE ("package_priority"."package_id" = 5001 AND "package_priority"."policy_run_id" = 90) ORDER BY "package_priority"."id" ASC LIMIT 1
Limit  (cost=8.46..8.46 rows=1 width=83) (actual time=0.007..0.007 rows=1 loops=1)
  Buffers: shared hit=4
  ->  Sort  (cost=8.46..8.46 rows=1 width=83) (actual time=0.006..0.006 rows=1 loops=1)
        Sort Key: id
        Sort Method: quicksort  Memory: 25kB
        Buffers: shared hit=4
        ->  Index Scan using one_priority_row_per_package_per_run on package_priority  (cost=0.42..8.45 rows=1 width=83) (actual time=0.005..0.006 rows=1 loops=1)
              Index Cond: ((package_id = 5001) AND (policy_run_id = 90))
              Buffers: shared hit=4
Planning Time: 0.030 ms
Execution Time: 0.010 ms

--- page.traces_for #8
SQL: SELECT "package_work_type"."id", "package_work_type"."package_id", "package_work_type"."policy_run_id", "package_work_type"."work_type", "package_work_type"."detail", "package_work_type"."policy_version", "package_work_type"."evidence_cutoff" FROM "package_work_type" WHERE ("package_work_type"."package_id" = 5001 AND "package_work_type"."policy_run_id" = 90) ORDER BY "package_work_type"."id" ASC LIMIT 1
Limit  (cost=8.46..8.46 rows=1 width=67) (actual time=0.007..0.007 rows=1 loops=1)
  Buffers: shared hit=4
  ->  Sort  (cost=8.46..8.46 rows=1 width=67) (actual time=0.007..0.007 rows=1 loops=1)
        Sort Key: id
        Sort Method: quicksort  Memory: 25kB
        Buffers: shared hit=4
        ->  Index Scan using one_work_type_row_per_package_per_run on package_work_type  (cost=0.42..8.45 rows=1 width=67) (actual time=0.006..0.006 rows=1 loops=1)
              Index Cond: ((package_id = 5001) AND (policy_run_id = 90))
              Buffers: shared hit=4
Planning Time: 0.019 ms
Execution Time: 0.009 ms

--- page.traces_for #9
SQL: SELECT "pypi_release_snapshots"."id", "pypi_release_snapshots"."observed_at", "pypi_release_snapshots"."package_id", "pypi_release_snapshots"."source", "pypi_release_snapshots"."state", "pypi_release_snapshots"."latest_version", "pypi_release_snapshots"."released_at", "pypi_release_snapshots"."requires_python", "pypi_release_snapshots"."detail", "pypi_release_snapshots"."trace_id" FROM "pypi_release_snapshots" WHERE "pypi_release_snapshots"."id" = 886151 LIMIT 21
Limit  (cost=0.42..8.44 rows=1 width=80) (actual time=0.006..0.006 rows=1 loops=1)
  Buffers: shared hit=4
  ->  Index Scan using pypi_release_snapshots_pkey on pypi_release_snapshots  (cost=0.42..8.44 rows=1 width=80) (actual time=0.005..0.006 rows=1 loops=1)
        Index Cond: (id = 886151)
        Buffers: shared hit=4
Planning Time: 0.014 ms
Execution Time: 0.008 ms

--- page.traces_for #10
SQL: SELECT "pypi_release_snapshots"."id", "pypi_release_snapshots"."observed_at", "pypi_release_snapshots"."package_id", "pypi_release_snapshots"."source", "pypi_release_snapshots"."state", "pypi_release_snapshots"."latest_version", "pypi_release_snapshots"."released_at", "pypi_release_snapshots"."requires_python", "pypi_release_snapshots"."detail", "pypi_release_snapshots"."trace_id" FROM "pypi_release_snapshots" WHERE "pypi_release_snapshots"."package_id" = 5001 ORDER BY "pypi_release_snapshots"."observed_at" DESC, "pypi_release_snapshots"."id" DESC LIMIT 20
Limit  (cost=4.59..85.77 rows=20 width=80) (actual time=0.028..0.029 rows=20 loops=1)
  Buffers: shared hit=24
  ->  Incremental Sort  (cost=4.59..373.99 rows=91 width=80) (actual time=0.028..0.028 rows=20 loops=1)
        Sort Key: observed_at DESC, id DESC
        Presorted Key: observed_at
        Full-sort Groups: 1  Sort Method: quicksort  Average Memory: 27kB  Peak Memory: 27kB
        Buffers: shared hit=24
        ->  Index Scan using pypi_release_pkg_observed on pypi_release_snapshots  (cost=0.42..369.96 rows=91 width=80) (actual time=0.006..0.024 rows=21 loops=1)
              Index Cond: (package_id = 5001)
              Buffers: shared hit=24
Planning Time: 0.014 ms
Execution Time: 0.032 ms

--- page.traces_for #11
SQL: SELECT COUNT(*) AS "__count" FROM "pypi_release_snapshots" WHERE "pypi_release_snapshots"."package_id" = 5001
Aggregate  (cost=6.25..6.25 rows=1 width=8) (actual time=0.010..0.010 rows=1 loops=1)
  Buffers: shared hit=4
  ->  Index Only Scan using pypi_release_pkg_observed on pypi_release_snapshots  (cost=0.42..6.02 rows=91 width=0) (actual time=0.004..0.007 rows=91 loops=1)
        Index Cond: (package_id = 5001)
        Heap Fetches: 0
        Buffers: shared hit=4
Planning Time: 0.012 ms
Execution Time: 0.014 ms

--- page.traces_for #12
SQL: SELECT "vulnerability_findings"."id", "vulnerability_findings"."observed_at", "vulnerability_findings"."package_id", "vulnerability_findings"."source", "vulnerability_findings"."state", "vulnerability_findings"."advisory_id", "vulnerability_findings"."severity", "vulnerability_findings"."affected_range", "vulnerability_findings"."fixed_range", "vulnerability_findings"."matched_version", "vulnerability_findings"."match_confidence", "vulnerability_findings"."detail", "vulnerability_findings"."trace_id" FROM "vulnerability_findings" WHERE "vulnerability_findings"."id" = 1772301 LIMIT 21
Limit  (cost=0.43..8.45 rows=1 width=116) (actual time=0.006..0.006 rows=1 loops=1)
  Buffers: shared hit=4
  ->  Index Scan using vulnerability_findings_pkey on vulnerability_findings  (cost=0.43..8.45 rows=1 width=116) (actual time=0.005..0.006 rows=1 loops=1)
        Index Cond: (id = 1772301)
        Buffers: shared hit=4
Planning Time: 0.015 ms
Execution Time: 0.008 ms

--- page.traces_for #13
SQL: SELECT "vulnerability_findings"."id", "vulnerability_findings"."observed_at", "vulnerability_findings"."package_id", "vulnerability_findings"."source", "vulnerability_findings"."state", "vulnerability_findings"."advisory_id", "vulnerability_findings"."severity", "vulnerability_findings"."affected_range", "vulnerability_findings"."fixed_range", "vulnerability_findings"."matched_version", "vulnerability_findings"."match_confidence", "vulnerability_findings"."detail", "vulnerability_findings"."trace_id" FROM "vulnerability_findings" WHERE "vulnerability_findings"."package_id" = 5001 ORDER BY "vulnerability_findings"."observed_at" DESC, "vulnerability_findings"."id" DESC LIMIT 20
Limit  (cost=4.64..85.83 rows=20 width=116) (actual time=0.022..0.023 rows=20 loops=1)
  Buffers: shared hit=14
  ->  Incremental Sort  (cost=4.64..739.45 rows=181 width=116) (actual time=0.022..0.022 rows=20 loops=1)
        Sort Key: observed_at DESC, id DESC
        Presorted Key: observed_at
        Full-sort Groups: 1  Sort Method: quicksort  Average Memory: 30kB  Peak Memory: 30kB
        Buffers: shared hit=14
        ->  Index Scan using vuln_finding_pkg_observed on vulnerability_findings  (cost=0.43..731.55 rows=181 width=116) (actual time=0.008..0.017 rows=21 loops=1)
              Index Cond: (package_id = 5001)
              Buffers: shared hit=14
Planning Time: 0.016 ms
Execution Time: 0.027 ms

--- page.traces_for #14
SQL: SELECT COUNT(*) AS "__count" FROM "vulnerability_findings" WHERE "vulnerability_findings"."package_id" = 5001
Aggregate  (cost=8.05..8.06 rows=1 width=8) (actual time=0.015..0.015 rows=1 loops=1)
  Buffers: shared hit=4
  ->  Index Only Scan using vuln_finding_pkg_observed on vulnerability_findings  (cost=0.43..7.60 rows=181 width=0) (actual time=0.004..0.010 rows=182 loops=1)
        Index Cond: (package_id = 5001)
        Heap Fetches: 0
        Buffers: shared hit=4
Planning Time: 0.010 ms
Execution Time: 0.018 ms

--- page.traces_for #15
SQL: SELECT "kev_findings"."id", "kev_findings"."observed_at", "kev_findings"."package_id", "kev_findings"."vulnerability_finding_id", "kev_findings"."source", "kev_findings"."state", "kev_findings"."catalog_date_added", "kev_findings"."detail", "kev_findings"."trace_id" FROM "kev_findings" WHERE "kev_findings"."id" = 1772301 LIMIT 21
Limit  (cost=0.43..8.45 rows=1 width=74) (actual time=0.005..0.006 rows=1 loops=1)
  Buffers: shared hit=4
  ->  Index Scan using kev_findings_pkey on kev_findings  (cost=0.43..8.45 rows=1 width=74) (actual time=0.005..0.005 rows=1 loops=1)
        Index Cond: (id = 1772301)
        Buffers: shared hit=4
Planning Time: 0.015 ms
Execution Time: 0.007 ms

--- page.traces_for #16
SQL: SELECT "kev_findings"."id", "kev_findings"."observed_at", "kev_findings"."package_id", "kev_findings"."vulnerability_finding_id", "kev_findings"."source", "kev_findings"."state", "kev_findings"."catalog_date_added", "kev_findings"."detail", "kev_findings"."trace_id" FROM "kev_findings" WHERE "kev_findings"."package_id" = 5001 ORDER BY "kev_findings"."observed_at" DESC, "kev_findings"."id" DESC LIMIT 20
Limit  (cost=4.64..85.83 rows=20 width=74) (actual time=0.018..0.020 rows=20 loops=1)
  Buffers: shared hit=14
  ->  Incremental Sort  (cost=4.64..747.54 rows=183 width=74) (actual time=0.018..0.019 rows=20 loops=1)
        Sort Key: observed_at DESC, id DESC
        Presorted Key: observed_at
        Full-sort Groups: 1  Sort Method: quicksort  Average Memory: 27kB  Peak Memory: 27kB
        Buffers: shared hit=14
        ->  Index Scan using kev_finding_pkg_observed on kev_findings  (cost=0.43..739.55 rows=183 width=74) (actual time=0.006..0.015 rows=21 loops=1)
              Index Cond: (package_id = 5001)
              Buffers: shared hit=14
Planning Time: 0.015 ms
Execution Time: 0.023 ms

--- page.traces_for #17
SQL: SELECT COUNT(*) AS "__count" FROM "kev_findings" WHERE "kev_findings"."package_id" = 5001
Aggregate  (cost=8.09..8.10 rows=1 width=8) (actual time=0.015..0.015 rows=1 loops=1)
  Buffers: shared hit=4
  ->  Index Only Scan using kev_finding_pkg_observed on kev_findings  (cost=0.43..7.63 rows=183 width=0) (actual time=0.004..0.010 rows=182 loops=1)
        Index Cond: (package_id = 5001)
        Heap Fetches: 0
        Buffers: shared hit=4
Planning Time: 0.010 ms
Execution Time: 0.018 ms

--- page.traces_for #18
SQL: SELECT "license_findings"."id", "license_findings"."observed_at", "license_findings"."package_id", "license_findings"."source", "license_findings"."state", "license_findings"."channel", "license_findings"."raw_license", "license_findings"."normalized_license", "license_findings"."detection_method", "license_findings"."detail", "license_findings"."trace_id" FROM "license_findings" WHERE "license_findings"."id" = 1772302 LIMIT 21
Limit  (cost=0.43..8.45 rows=1 width=102) (actual time=0.005..0.005 rows=1 loops=1)
  Buffers: shared hit=4
  ->  Index Scan using license_findings_pkey on license_findings  (cost=0.43..8.45 rows=1 width=102) (actual time=0.005..0.005 rows=1 loops=1)
        Index Cond: (id = 1772302)
        Buffers: shared hit=4
Planning Time: 0.013 ms
Execution Time: 0.007 ms

--- page.traces_for #19
SQL: SELECT "license_findings"."id", "license_findings"."observed_at", "license_findings"."package_id", "license_findings"."source", "license_findings"."state", "license_findings"."channel", "license_findings"."raw_license", "license_findings"."normalized_license", "license_findings"."detection_method", "license_findings"."detail", "license_findings"."trace_id" FROM "license_findings" WHERE "license_findings"."package_id" = 5001 ORDER BY "license_findings"."observed_at" DESC, "license_findings"."id" DESC LIMIT 20
Limit  (cost=4.64..85.84 rows=20 width=102) (actual time=0.019..0.021 rows=20 loops=1)
  Buffers: shared hit=14
  ->  Incremental Sort  (cost=4.64..743.55 rows=182 width=102) (actual time=0.019..0.020 rows=20 loops=1)
        Sort Key: observed_at DESC, id DESC
        Presorted Key: observed_at
        Full-sort Groups: 1  Sort Method: quicksort  Average Memory: 27kB  Peak Memory: 27kB
        Buffers: shared hit=14
        ->  Index Scan using license_finding_pkg_observed on license_findings  (cost=0.43..735.60 rows=182 width=102) (actual time=0.007..0.016 rows=21 loops=1)
              Index Cond: (package_id = 5001)
              Buffers: shared hit=14
Planning Time: 0.014 ms
Execution Time: 0.024 ms

--- page.traces_for #20
SQL: SELECT COUNT(*) AS "__count" FROM "license_findings" WHERE "license_findings"."package_id" = 5001
Aggregate  (cost=8.07..8.08 rows=1 width=8) (actual time=0.015..0.015 rows=1 loops=1)
  Buffers: shared hit=4
  ->  Index Only Scan using license_finding_pkg_observed on license_findings  (cost=0.43..7.61 rows=182 width=0) (actual time=0.004..0.010 rows=182 loops=1)
        Index Cond: (package_id = 5001)
        Heap Fetches: 0
        Buffers: shared hit=4
Planning Time: 0.010 ms
Execution Time: 0.018 ms

--- page.traces_for #21
SQL: SELECT "python_readiness_assessments"."id", "python_readiness_assessments"."observed_at", "python_readiness_assessments"."package_id", "python_readiness_assessments"."source", "python_readiness_assessments"."state", "python_readiness_assessments"."python_series", "python_readiness_assessments"."requires_python", "python_readiness_assessments"."matching_classifier", "python_readiness_assessments"."deciding_signal", "python_readiness_assessments"."detail", "python_readiness_assessments"."trace_id" FROM "python_readiness_assessments" WHERE "python_readiness_assessments"."id" = 1772302 LIMIT 21
Limit  (cost=0.43..8.45 rows=1 width=143) (actual time=0.006..0.006 rows=1 loops=1)
  Buffers: shared hit=4
  ->  Index Scan using python_readiness_assessments_pkey on python_readiness_assessments  (cost=0.43..8.45 rows=1 width=143) (actual time=0.005..0.006 rows=1 loops=1)
        Index Cond: (id = 1772302)
        Buffers: shared hit=4
Planning Time: 0.015 ms
Execution Time: 0.008 ms

--- page.traces_for #22
SQL: SELECT "python_readiness_assessments"."id", "python_readiness_assessments"."observed_at", "python_readiness_assessments"."package_id", "python_readiness_assessments"."source", "python_readiness_assessments"."state", "python_readiness_assessments"."python_series", "python_readiness_assessments"."requires_python", "python_readiness_assessments"."matching_classifier", "python_readiness_assessments"."deciding_signal", "python_readiness_assessments"."detail", "python_readiness_assessments"."trace_id" FROM "python_readiness_assessments" WHERE "python_readiness_assessments"."package_id" = 5001 ORDER BY "python_readiness_assessments"."observed_at" DESC, "python_readiness_assessments"."id" DESC LIMIT 20
Limit  (cost=4.64..85.83 rows=20 width=143) (actual time=0.021..0.022 rows=20 loops=1)
  Buffers: shared hit=14
  ->  Incremental Sort  (cost=4.64..743.49 rows=182 width=143) (actual time=0.021..0.021 rows=20 loops=1)
        Sort Key: observed_at DESC, id DESC
        Presorted Key: observed_at
        Full-sort Groups: 1  Sort Method: quicksort  Average Memory: 30kB  Peak Memory: 30kB
        Buffers: shared hit=14
        ->  Index Scan using py_readiness_pkg_observed on python_readiness_assessments  (cost=0.43..735.55 rows=182 width=143) (actual time=0.006..0.017 rows=21 loops=1)
              Index Cond: (package_id = 5001)
              Buffers: shared hit=14
Planning Time: 0.014 ms
Execution Time: 0.025 ms

--- page.traces_for #23
SQL: SELECT COUNT(*) AS "__count" FROM "python_readiness_assessments" WHERE "python_readiness_assessments"."package_id" = 5001
Aggregate  (cost=8.07..8.08 rows=1 width=8) (actual time=0.015..0.015 rows=1 loops=1)
  Buffers: shared hit=5
  ->  Index Only Scan using py_readiness_pkg_observed on python_readiness_assessments  (cost=0.43..7.61 rows=182 width=0) (actual time=0.004..0.010 rows=182 loops=1)
        Index Cond: (package_id = 5001)
        Heap Fetches: 0
        Buffers: shared hit=5
Planning Time: 0.009 ms
Execution Time: 0.018 ms

--- page.traces_for #24
SQL: SELECT "feedstock_snapshots"."id", "feedstock_snapshots"."observed_at", "feedstock_snapshots"."package_id", "feedstock_snapshots"."source", "feedstock_snapshots"."state", "feedstock_snapshots"."feedstock_name", "feedstock_snapshots"."feedstock_url", "feedstock_snapshots"."recipe_version", "feedstock_snapshots"."recipe_build_number", "feedstock_snapshots"."recipe_metadata_url", "feedstock_snapshots"."last_recipe_activity_at", "feedstock_snapshots"."staged_recipe_url", "feedstock_snapshots"."absence_established", "feedstock_snapshots"."detail", "feedstock_snapshots"."trace_id" FROM "feedstock_snapshots" WHERE "feedstock_snapshots"."id" = 886151 LIMIT 21
Limit  (cost=0.42..8.44 rows=1 width=162) (actual time=0.005..0.006 rows=1 loops=1)
  Buffers: shared hit=4
  ->  Index Scan using feedstock_snapshots_pkey on feedstock_snapshots  (cost=0.42..8.44 rows=1 width=162) (actual time=0.005..0.005 rows=1 loops=1)
        Index Cond: (id = 886151)
        Buffers: shared hit=4
Planning Time: 0.014 ms
Execution Time: 0.008 ms

--- page.traces_for #25
SQL: SELECT "feedstock_snapshots"."id", "feedstock_snapshots"."observed_at", "feedstock_snapshots"."package_id", "feedstock_snapshots"."source", "feedstock_snapshots"."state", "feedstock_snapshots"."feedstock_name", "feedstock_snapshots"."feedstock_url", "feedstock_snapshots"."recipe_version", "feedstock_snapshots"."recipe_build_number", "feedstock_snapshots"."recipe_metadata_url", "feedstock_snapshots"."last_recipe_activity_at", "feedstock_snapshots"."staged_recipe_url", "feedstock_snapshots"."absence_established", "feedstock_snapshots"."detail", "feedstock_snapshots"."trace_id" FROM "feedstock_snapshots" WHERE "feedstock_snapshots"."package_id" = 5001 ORDER BY "feedstock_snapshots"."observed_at" DESC, "feedstock_snapshots"."id" DESC LIMIT 20
Limit  (cost=4.59..85.77 rows=20 width=162) (actual time=0.028..0.029 rows=20 loops=1)
  Buffers: shared hit=24
  ->  Incremental Sort  (cost=4.59..373.99 rows=91 width=162) (actual time=0.028..0.028 rows=20 loops=1)
        Sort Key: observed_at DESC, id DESC
        Presorted Key: observed_at
        Full-sort Groups: 1  Sort Method: quicksort  Average Memory: 30kB  Peak Memory: 30kB
        Buffers: shared hit=24
        ->  Index Scan using feedstock_pkg_observed on feedstock_snapshots  (cost=0.42..369.96 rows=91 width=162) (actual time=0.006..0.024 rows=21 loops=1)
              Index Cond: (package_id = 5001)
              Buffers: shared hit=24
Planning Time: 0.015 ms
Execution Time: 0.033 ms

--- page.traces_for #26
SQL: SELECT COUNT(*) AS "__count" FROM "feedstock_snapshots" WHERE "feedstock_snapshots"."package_id" = 5001
Aggregate  (cost=6.25..6.25 rows=1 width=8) (actual time=0.009..0.009 rows=1 loops=1)
  Buffers: shared hit=4
  ->  Index Only Scan using feedstock_pkg_observed on feedstock_snapshots  (cost=0.42..6.02 rows=91 width=0) (actual time=0.003..0.006 rows=91 loops=1)
        Index Cond: (package_id = 5001)
        Heap Fetches: 0
        Buffers: shared hit=4
Planning Time: 0.009 ms
Execution Time: 0.012 ms

--- page.recent_runs
SQL: SELECT "collection_runs"."id", "collection_runs"."started_at", "collection_runs"."finished_at", "collection_runs"."status", "collection_runs"."trace_id", "collection_runs"."detail", "collection_runs"."collector", "collection_runs"."package_id" FROM "collection_runs" WHERE "collection_runs"."package_id" = 5001 ORDER BY "collection_runs"."started_at" DESC, "collection_runs"."id" DESC LIMIT 20
Limit  (cost=5.24..86.16 rows=20 width=56) (actual time=0.010..0.011 rows=20 loops=1)
  Buffers: shared hit=7
  ->  Incremental Sort  (cost=5.24..3302.36 rows=815 width=56) (actual time=0.009..0.010 rows=20 loops=1)
        Sort Key: started_at DESC, id DESC
        Presorted Key: started_at
        Full-sort Groups: 1  Sort Method: quicksort  Average Memory: 28kB  Peak Memory: 28kB
        Buffers: shared hit=7
        ->  Index Scan using collection_runs_pkg_started on collection_runs  (cost=0.43..3270.38 rows=815 width=56) (actual time=0.004..0.006 rows=28 loops=1)
              Index Cond: (package_id = 5001)
              Buffers: shared hit=7
Planning Time: 0.015 ms
Execution Time: 0.014 ms

--- page.runs_in_flight
SQL: SELECT "collection_runs"."id", "collection_runs"."started_at", "collection_runs"."finished_at", "collection_runs"."status", "collection_runs"."trace_id", "collection_runs"."detail", "collection_runs"."collector", "collection_runs"."package_id" FROM "collection_runs" WHERE ("collection_runs"."finished_at" IS NULL AND "collection_runs"."package_id" = 5001 AND "collection_runs"."started_at" >= '2026-09-14 11:50:00+00:00'::timestamptz) ORDER BY "collection_runs"."started_at" DESC, "collection_runs"."id" DESC
Incremental Sort  (cost=4.47..4.52 rows=2 width=56) (actual time=0.001..0.001 rows=0 loops=1)
  Sort Key: started_at DESC, id DESC
  Presorted Key: started_at
  Full-sort Groups: 1  Sort Method: quicksort  Average Memory: 25kB  Peak Memory: 25kB
  Buffers: shared hit=3
  ->  Index Scan Backward using collection_runs_started on collection_runs  (cost=0.43..4.46 rows=1 width=56) (actual time=0.001..0.001 rows=0 loops=1)
        Index Cond: (started_at >= '2026-09-14 11:50:00+00'::timestamp with time zone)
        Filter: ((finished_at IS NULL) AND (package_id = 5001))
        Buffers: shared hit=3
Planning:
  Buffers: shared hit=4
Planning Time: 0.019 ms
Execution Time: 0.004 ms

--- page.last_recollection
SQL: SELECT "package_recollections"."id", "package_recollections"."observed_at", "package_recollections"."package_id", "package_recollections"."actor_id", "package_recollections"."collectors", "package_recollections"."not_offered", "package_recollections"."trace_id", "users_user"."id", "users_user"."password", "users_user"."last_login", "users_user"."is_superuser", "users_user"."username", "users_user"."email", "users_user"."is_staff", "users_user"."is_active", "users_user"."date_joined", "users_user"."name", "users_user"."idp_subject" FROM "package_recollections" INNER JOIN "users_user" ON ("package_recollections"."actor_id" = "users_user"."id") WHERE "package_recollections"."package_id" = 5001 ORDER BY "package_recollections"."observed_at" DESC, "package_recollections"."id" DESC LIMIT 1
Limit  (cost=11.81..17.68 rows=1 width=2345) (actual time=0.003..0.003 rows=0 loops=1)
  Buffers: shared hit=2
  ->  Incremental Sort  (cost=11.81..23.55 rows=2 width=2345) (actual time=0.003..0.003 rows=0 loops=1)
        Sort Key: package_recollections.observed_at DESC, package_recollections.id DESC
        Presorted Key: package_recollections.observed_at
        Full-sort Groups: 1  Sort Method: quicksort  Average Memory: 25kB  Peak Memory: 25kB
        Buffers: shared hit=2
        ->  Nested Loop  (cost=0.15..23.46 rows=2 width=2345) (actual time=0.001..0.002 rows=0 loops=1)
              Join Filter: (package_recollections.actor_id = users_user.id)
              Buffers: shared hit=2
              ->  Index Scan using recollection_pkg_observed on package_recollections  (cost=0.15..12.18 rows=2 width=178) (actual time=0.001..0.001 rows=0 loops=1)
                    Index Cond: (package_id = 5001)
                    Buffers: shared hit=2
              ->  Materialize  (cost=0.00..10.45 rows=30 width=2167) (never executed)
                    ->  Seq Scan on users_user  (cost=0.00..10.30 rows=30 width=2167) (never executed)
Planning:
  Buffers: shared hit=9
Planning Time: 0.046 ms
Execution Time: 0.019 ms

--- listing.health_queryset count
SQL: SELECT COUNT(*) AS "__count" FROM "package_health"
Aggregate  (cost=215.28..215.29 rows=1 width=8) (actual time=0.612..0.612 rows=1 loops=1)
  Buffers: shared hit=10
  ->  Index Only Scan using package_health_computed on package_health  (cost=0.29..190.28 rows=10000 width=0) (actual time=0.003..0.367 rows=10000 loops=1)
        Heap Fetches: 0
        Buffers: shared hit=10
Planning Time: 0.011 ms
Execution Time: 0.616 ms

--- listing.health_queryset first page
SQL: SELECT "package_health"."id", "package_health"."package_id", "package_health"."policy_run_id", "package_health"."computed_at", "package_health"."evidence_cutoff", "package_health"."confidence", "package_health"."policy_versions", "package_health"."currency_status", "package_health"."feedstock_presence_status", "package_health"."priority_status", "package_health"."work_type_status", CASE WHEN "package_health"."priority_status" = 'p1' THEN 0 WHEN "package_health"."priority_status" = 'p2' THEN 1 WHEN "package_health"."priority_status" = 'p3' THEN 2 WHEN "package_health"."priority_status" = 'p4' THEN 3 WHEN "package_health"."priority_status" = 'p5' THEN 4 WHEN "package_health"."priority_status" = 'p6' THEN 5 WHEN "package_health"."priority_status" = 'p7' THEN 6 WHEN "package_health"."priority_status" = 'p8' THEN 7 WHEN "package_health"."priority_status" = 'p9' THEN 8 WHEN "package_health"."priority_status" = 'p10' THEN 9 ELSE 10 END AS "bucket_rank", (SELECT U0."score" AS "score" FROM "package_priority" U0 WHERE (U0."package_id" = ("package_health"."package_id") AND U0."policy_run_id" = ("package_health"."policy_run_id")) LIMIT 1) AS "priority_score", "packages"."id", "packages"."canonical_name", "packages"."display_name", "packages"."source_repository_url", "packages"."primary_purl", "packages"."primary_type", "packages"."conda_purl", "packages"."alternative_purls", "packages"."cpes", "packages"."version_authority_order", "packages"."identity_source", "packages"."associator_key", "packages"."resolved_at", "packages"."confidence" FROM "package_health" INNER JOIN "packages" ON ("package_health"."package_id" = "packages"."id") ORDER BY "packages"."canonical_name" ASC, "package_health"."package_id" ASC LIMIT 50
Limit  (cost=1.04..450.47 rows=50 width=268) (actual time=0.048..0.119 rows=50 loops=1)
  Buffers: shared hit=357
  ->  Result  (cost=1.04..89887.19 rows=10000 width=268) (actual time=0.048..0.117 rows=50 loops=1)
        Buffers: shared hit=357
        ->  Incremental Sort  (cost=1.04..5087.19 rows=10000 width=266) (actual time=0.044..0.064 rows=50 loops=1)
              Sort Key: packages.canonical_name, package_health.package_id
              Presorted Key: packages.canonical_name
              Full-sort Groups: 2  Sort Method: quicksort  Average Memory: 41kB  Peak Memory: 41kB
              Buffers: shared hit=157
              ->  Nested Loop  (cost=0.57..4637.19 rows=10000 width=266) (actual time=0.007..0.039 rows=51 loops=1)
                    Buffers: shared hit=157
                    ->  Index Scan using packages_canonical_name_key on packages  (cost=0.29..550.28 rows=10000 width=157) (actual time=0.004..0.009 rows=51 loops=1)
                          Buffers: shared hit=4
                    ->  Index Scan using package_health_package_id_key on package_health  (cost=0.29..0.38 rows=1 width=105) (actual time=0.000..0.000 rows=1 loops=51)
                          Index Cond: (package_id = packages.id)
                          Buffers: shared hit=153
        SubPlan 1
          ->  Limit  (cost=0.42..8.45 rows=1 width=2) (actual time=0.001..0.001 rows=1 loops=50)
                Buffers: shared hit=200
                ->  Index Scan using one_priority_row_per_package_per_run on package_priority u0  (cost=0.42..8.45 rows=1 width=2) (actual time=0.001..0.001 rows=1 loops=50)
                      Index Cond: ((package_id = package_health.package_id) AND (policy_run_id = package_health.policy_run_id))
                      Buffers: shared hit=200
Planning:
  Buffers: shared hit=12
Planning Time: 0.131 ms
Execution Time: 0.147 ms

--- coverage.collector_health: ledger group-by
SQL: SELECT "collection_runs"."collector" AS "collector", MAX("collection_runs"."finished_at") AS "last_finished_at", COUNT("collection_runs"."id") AS "runs", COUNT("collection_runs"."id") FILTER (WHERE "collection_runs"."status" = 'failed') AS "failures" FROM "collection_runs" GROUP BY 1
Finalize GroupAggregate  (cost=165180.79..165183.16 rows=9 width=36) (actual time=436.271..438.138 rows=9 loops=1)
  Group Key: collector
  Buffers: shared hit=118 read=88048
  ->  Gather Merge  (cost=165180.79..165182.89 rows=18 width=36) (actual time=436.262..438.126 rows=27 loops=1)
        Workers Planned: 2
        Workers Launched: 2
        Buffers: shared hit=118 read=88048
        ->  Sort  (cost=164180.77..164180.79 rows=9 width=36) (actual time=428.969..428.970 rows=9 loops=3)
              Sort Key: collector
              Sort Method: quicksort  Memory: 25kB
              Buffers: shared hit=118 read=88048
              Worker 0:  Sort Method: quicksort  Memory: 25kB
              Worker 1:  Sort Method: quicksort  Memory: 25kB
              ->  Partial HashAggregate  (cost=164180.53..164180.62 rows=9 width=36) (actual time=428.943..428.944 rows=9 loops=3)
                    Group Key: collector
                    Batches: 1  Memory Usage: 24kB
                    Buffers: shared hit=102 read=88048
                    Worker 0:  Batches: 1  Memory Usage: 24kB
                    Worker 1:  Batches: 1  Memory Usage: 24kB
                    ->  Parallel Seq Scan on collection_runs  (cost=0.00..121941.35 rows=3379135 width=38) (actual time=0.020..114.263 rows=2703276 loops=3)
                          Buffers: shared hit=102 read=88048
Planning Time: 0.105 ms
JIT:
  Functions: 21
  Options: Inlining false, Optimization false, Expressions true, Deforming true
  Timing: Generation 0.899 ms (Deform 0.315 ms), Inlining 0.000 ms, Optimization 0.543 ms, Emission 10.231 ms, Total 11.673 ms
Execution Time: 438.641 ms

--- coverage.coverage_of #1
SQL: SELECT "packages"."confidence" AS "confidence", COUNT("packages"."id") AS "count" FROM "packages" GROUP BY 1
HashAggregate  (cost=383.00..383.01 rows=1 width=17) (actual time=1.351..1.351 rows=1 loops=1)
  Group Key: confidence
  Batches: 1  Memory Usage: 24kB
  Buffers: shared hit=233
  ->  Seq Scan on packages  (cost=0.00..333.00 rows=10000 width=17) (actual time=0.004..0.706 rows=10000 loops=1)
        Buffers: shared hit=233
Planning Time: 0.033 ms
Execution Time: 1.363 ms

--- coverage.coverage_of #2
SQL: SELECT MAX("package_health"."computed_at") AS "computed_at", MAX("package_health"."evidence_cutoff") AS "evidence_cutoff", COUNT("package_health"."id") FILTER (WHERE "package_health"."currency_status" IN ('error', 'unknown')) AS "currency_status__gap", COUNT("package_health"."id") FILTER (WHERE "package_health"."currency_status" IN ('not_applicable', 'not_found')) AS "currency_status__negative", COUNT("package_health"."id") FILTER (WHERE "package_health"."feedstock_presence_status" IN ('error', 'unknown')) AS "feedstock_presence_status__gap", COUNT("package_health"."id") FILTER (WHERE "package_health"."feedstock_presence_status" IN ('not_applicable', 'not_found')) AS "feedstock_presence_status__negative", COUNT("package_health"."id") FILTER (WHERE "package_health"."priority_status" IN ('error', 'unknown')) AS "priority_status__gap", COUNT("package_health"."id") FILTER (WHERE "package_health"."priority_status" IN ('not_applicable', 'not_found')) AS "priority_status__negative", COUNT("package_health"."id") FILTER (WHERE "package_health"."work_type_status" IN ('error', 'unknown')) AS "work_type_status__gap", COUNT("package_health"."id") FILTER (WHERE "package_health"."work_type_status" IN ('not_applicable', 'not_found')) AS "work_type_status__negative" FROM "package_health"
Aggregate  (cost=723.00..723.01 rows=1 width=80) (actual time=2.570..2.571 rows=1 loops=1)
  Buffers: shared hit=173
  ->  Seq Scan on package_health  (cost=0.00..273.00 rows=10000 width=75) (actual time=0.001..0.626 rows=10000 loops=1)
        Buffers: shared hit=173
Planning Time: 0.028 ms
Execution Time: 2.578 ms

--- digest._freshness: conda_package #1
SQL: SELECT DISTINCT "conda_package_snapshots"."package_id" AS "package_id" FROM "conda_package_snapshots"
Unique  (cost=1000.45..28726.57 rows=9950 width=8) (actual time=14.063..72.901 rows=10000 loops=1)
  Buffers: shared hit=1743
  ->  Gather Merge  (cost=1000.45..28676.82 rows=19900 width=8) (actual time=14.063..72.334 rows=10000 loops=1)
        Workers Planned: 2
        Workers Launched: 2
        Buffers: shared hit=1743
        ->  Unique  (cost=0.43..25379.84 rows=9950 width=8) (actual time=0.014..42.614 rows=3333 loops=3)
              Buffers: shared hit=1743
              ->  Parallel Index Only Scan using conda_package_snapshots_package_id_e1e54a6c on conda_package_snapshots  (cost=0.43..23502.76 rows=750833 width=8) (actual time=0.013..22.277 rows=600667 loops=3)
                    Heap Fetches: 0
                    Buffers: shared hit=1743
Planning Time: 0.035 ms
Execution Time: 73.119 ms

--- digest._freshness: conda_package #2
SQL: SELECT DISTINCT "conda_package_snapshots"."package_id" AS "package_id" FROM "conda_package_snapshots" WHERE "conda_package_snapshots"."observed_at" >= '2026-09-12 12:00:00+00:00'::timestamptz
HashAggregate  (cost=1752.64..1850.81 rows=9817 width=8) (actual time=8.098..8.531 rows=9900 loops=1)
  Group Key: package_id
  Batches: 1  Memory Usage: 913kB
  Buffers: shared hit=15060
  ->  Index Scan using conda_pkg_observed on conda_package_snapshots  (cost=0.43..1646.66 rows=42394 width=8) (actual time=0.006..4.208 rows=39600 loops=1)
        Index Cond: (observed_at >= '2026-09-12 12:00:00+00'::timestamp with time zone)
        Buffers: shared hit=15060
Planning Time: 0.057 ms
Execution Time: 8.731 ms

--- digest._freshness: feedstock #1
SQL: SELECT "package_mappings"."package_id" AS "package_id" FROM "package_mappings" WHERE ("package_mappings"."kind" = 'feedstock' AND "package_mappings"."outcome" IN ('established', 'not_found', 'not_applicable')) ORDER BY 1 ASC
Sort  (cost=12.28..12.29 rows=1 width=8) (actual time=0.005..0.005 rows=0 loops=1)
  Sort Key: package_id
  Sort Method: quicksort  Memory: 25kB
  Buffers: shared hit=1
  ->  Bitmap Heap Scan on package_mappings  (cost=6.92..12.27 rows=1 width=8) (actual time=0.003..0.003 rows=0 loops=1)
        Recheck Cond: ((kind)::text = 'feedstock'::text)
        Filter: ((outcome)::text = ANY ('{established,not_found,not_applicable}'::text[]))
        Buffers: shared hit=1
        ->  Bitmap Index Scan on one_outcome_per_package_mapping  (cost=0.00..6.92 rows=2 width=0) (actual time=0.002..0.002 rows=0 loops=1)
              Index Cond: ((kind)::text = 'feedstock'::text)
              Buffers: shared hit=1
Planning:
  Buffers: shared hit=3
Planning Time: 0.049 ms
Execution Time: 0.010 ms

--- digest._freshness: feedstock #2
SQL: SELECT DISTINCT "feedstock_snapshots"."package_id" AS "package_id" FROM "feedstock_snapshots"
HashAggregate  (cost=15852.76..15952.01 rows=9925 width=8) (actual time=32.018..33.281 rows=10000 loops=1)
  Group Key: package_id
  Batches: 1  Memory Usage: 913kB
  Buffers: shared hit=905
  ->  Gather  (cost=1000.42..15803.13 rows=19850 width=8) (actual time=0.097..31.077 rows=10000 loops=1)
        Workers Planned: 2
        Workers Launched: 2
        Buffers: shared hit=905
        ->  Unique  (cost=0.42..12818.13 rows=9925 width=8) (actual time=0.016..21.482 rows=3333 loops=3)
              Buffers: shared hit=905
              ->  Parallel Index Only Scan using feedstock_snapshots_package_id_dc78ce69 on feedstock_snapshots  (cost=0.42..11879.59 rows=375417 width=8) (actual time=0.016..11.597 rows=300333 loops=3)
                    Heap Fetches: 0
                    Buffers: shared hit=905
Planning Time: 0.019 ms
Execution Time: 33.509 ms

--- digest._freshness: feedstock #3
SQL: SELECT DISTINCT "feedstock_snapshots"."package_id" AS "package_id" FROM "feedstock_snapshots" WHERE "feedstock_snapshots"."observed_at" >= '2026-08-31 12:00:00+00:00'::timestamptz
HashAggregate  (cost=6758.06..6857.31 rows=9925 width=8) (actual time=40.687..41.115 rows=9900 loops=1)
  Group Key: package_id
  Batches: 1  Memory Usage: 913kB
  Buffers: shared hit=83191
  ->  Index Scan using feedstock_observed on feedstock_snapshots  (cost=0.42..6417.75 rows=136124 width=8) (actual time=0.006..30.872 rows=138600 loops=1)
        Index Cond: (observed_at >= '2026-08-31 12:00:00+00'::timestamp with time zone)
        Buffers: shared hit=83191
Planning Time: 0.056 ms
Execution Time: 41.317 ms

--- digest._freshness: license #1
SQL: SELECT DISTINCT "license_findings"."package_id" AS "package_id" FROM "license_findings"
Unique  (cost=1000.45..28711.71 rows=9887 width=8) (actual time=15.214..66.334 rows=10000 loops=1)
  Buffers: shared hit=1743
  ->  Gather Merge  (cost=1000.45..28662.28 rows=19774 width=8) (actual time=15.214..65.768 rows=10000 loops=1)
        Workers Planned: 2
        Workers Launched: 2
        Buffers: shared hit=1743
        ->  Unique  (cost=0.43..25379.84 rows=9887 width=8) (actual time=0.018..37.165 rows=3333 loops=3)
              Buffers: shared hit=1743
              ->  Parallel Index Only Scan using license_findings_package_id_d469f4d0 on license_findings  (cost=0.43..23502.76 rows=750833 width=8) (actual time=0.018..21.082 rows=600667 loops=3)
                    Heap Fetches: 0
                    Buffers: shared hit=1743
Planning Time: 0.041 ms
Execution Time: 66.558 ms

--- digest._freshness: license #2
SQL: SELECT DISTINCT "license_findings"."package_id" AS "package_id" FROM "license_findings" WHERE "license_findings"."observed_at" >= '2026-09-12 12:00:00+00:00'::timestamptz
HashAggregate  (cost=1663.80..1760.95 rows=9715 width=8) (actual time=7.382..7.817 rows=9900 loops=1)
  Group Key: package_id
  Batches: 1  Memory Usage: 913kB
  Buffers: shared hit=15534
  ->  Index Scan using license_finding_observed on license_findings  (cost=0.43..1564.75 rows=39622 width=8) (actual time=0.007..3.684 rows=39600 loops=1)
        Index Cond: (observed_at >= '2026-09-12 12:00:00+00'::timestamp with time zone)
        Buffers: shared hit=15534
Planning Time: 0.049 ms
Execution Time: 8.018 ms

--- digest._freshness: pypi_release #1
SQL: SELECT "package_mappings"."package_id" AS "package_id" FROM "package_mappings" WHERE ("package_mappings"."kind" = 'release_ecosystem' AND "package_mappings"."outcome" IN ('established', 'not_applicable')) ORDER BY 1 ASC
Sort  (cost=12.28..12.28 rows=1 width=8) (actual time=0.005..0.005 rows=0 loops=1)
  Sort Key: package_id
  Sort Method: quicksort  Memory: 25kB
  Buffers: shared hit=1
  ->  Bitmap Heap Scan on package_mappings  (cost=6.92..12.27 rows=1 width=8) (actual time=0.003..0.003 rows=0 loops=1)
        Recheck Cond: ((kind)::text = 'release_ecosystem'::text)
        Filter: ((outcome)::text = ANY ('{established,not_applicable}'::text[]))
        Buffers: shared hit=1
        ->  Bitmap Index Scan on one_outcome_per_package_mapping  (cost=0.00..6.92 rows=2 width=0) (actual time=0.001..0.001 rows=0 loops=1)
              Index Cond: ((kind)::text = 'release_ecosystem'::text)
              Buffers: shared hit=1
Planning:
  Buffers: shared hit=3
Planning Time: 0.044 ms
Execution Time: 0.011 ms

--- digest._freshness: pypi_release #2
SQL: SELECT DISTINCT "pypi_release_snapshots"."package_id" AS "package_id" FROM "pypi_release_snapshots"
HashAggregate  (cost=15845.99..15944.91 rows=9892 width=8) (actual time=25.448..26.532 rows=10000 loops=1)
  Group Key: package_id
  Batches: 1  Memory Usage: 913kB
  Buffers: shared hit=905
  ->  Gather  (cost=1000.42..15796.53 rows=19784 width=8) (actual time=0.079..25.059 rows=10000 loops=1)
        Workers Planned: 2
        Workers Launched: 2
        Buffers: shared hit=905
        ->  Unique  (cost=0.42..12818.13 rows=9892 width=8) (actual time=0.019..19.487 rows=3333 loops=3)
              Buffers: shared hit=905
              ->  Parallel Index Only Scan using pypi_release_snapshots_package_id_d8b41783 on pypi_release_snapshots  (cost=0.42..11879.59 rows=375417 width=8) (actual time=0.019..10.285 rows=300333 loops=3)
                    Heap Fetches: 0
                    Buffers: shared hit=905
Planning Time: 0.018 ms
Execution Time: 26.755 ms

--- digest._freshness: pypi_release #3
SQL: SELECT DISTINCT "pypi_release_snapshots"."package_id" AS "package_id" FROM "pypi_release_snapshots" WHERE "pypi_release_snapshots"."observed_at" >= '2026-09-12 12:00:00+00:00'::timestamptz
HashAggregate  (cost=782.19..867.99 rows=8580 width=8) (actual time=4.410..4.867 rows=9900 loops=1)
  Group Key: package_id
  Batches: 1  Memory Usage: 913kB
  Buffers: shared hit=6843
  ->  Index Scan using pypi_release_observed on pypi_release_snapshots  (cost=0.42..732.78 rows=19767 width=8) (actual time=0.008..2.497 rows=19800 loops=1)
        Index Cond: (observed_at >= '2026-09-12 12:00:00+00'::timestamp with time zone)
        Buffers: shared hit=6843
Planning Time: 0.052 ms
Execution Time: 5.067 ms

--- digest._freshness: python_readiness #1
SQL: SELECT "package_mappings"."package_id" AS "package_id" FROM "package_mappings" INNER JOIN "packages" ON ("package_mappings"."package_id" = "packages"."id") WHERE ("package_mappings"."kind" = 'release_ecosystem' AND ("package_mappings"."outcome" = 'not_applicable' OR ("package_mappings"."outcome" = 'established' AND "packages"."primary_purl"::text LIKE 'pkg:pypi/%' AND UPPER("packages"."primary_type"::text) = UPPER('pypi')))) ORDER BY 1 ASC
Sort  (cost=20.61..20.61 rows=1 width=8) (actual time=0.007..0.007 rows=0 loops=1)
  Sort Key: package_mappings.package_id
  Sort Method: quicksort  Memory: 25kB
  Buffers: shared hit=1
  ->  Nested Loop  (cost=7.21..20.60 rows=1 width=8) (actual time=0.004..0.004 rows=0 loops=1)
        Buffers: shared hit=1
        ->  Bitmap Heap Scan on package_mappings  (cost=6.92..12.27 rows=1 width=90) (actual time=0.004..0.004 rows=0 loops=1)
              Recheck Cond: ((kind)::text = 'release_ecosystem'::text)
              Filter: (((outcome)::text = 'not_applicable'::text) OR ((outcome)::text = 'established'::text))
              Buffers: shared hit=1
              ->  Bitmap Index Scan on one_outcome_per_package_mapping  (cost=0.00..6.92 rows=2 width=0) (actual time=0.002..0.002 rows=0 loops=1)
                    Index Cond: ((kind)::text = 'release_ecosystem'::text)
                    Buffers: shared hit=1
        ->  Index Scan using packages_pkey on packages  (cost=0.29..8.32 rows=1 width=32) (never executed)
              Index Cond: (id = package_mappings.package_id)
              Filter: (((package_mappings.outcome)::text = 'not_applicable'::text) OR (((package_mappings.outcome)::text = 'established'::text) AND ((primary_purl)::text ~~ 'pkg:pypi/%'::text) AND (upper((primary_type)::text) = 'PYPI'::text)))
Planning:
  Buffers: shared hit=3
Planning Time: 0.116 ms
Execution Time: 0.017 ms

--- digest._freshness: python_readiness #2
SQL: SELECT DISTINCT "python_readiness_assessments"."package_id" AS "package_id" FROM "python_readiness_assessments"
Unique  (cost=1000.45..28713.28 rows=9894 width=8) (actual time=16.005..67.385 rows=10000 loops=1)
  Buffers: shared hit=21541
  ->  Gather Merge  (cost=1000.45..28663.81 rows=19788 width=8) (actual time=16.005..66.830 rows=10000 loops=1)
        Workers Planned: 2
        Workers Launched: 2
        Buffers: shared hit=21541
        ->  Unique  (cost=0.43..25379.76 rows=9894 width=8) (actual time=0.018..37.299 rows=3333 loops=3)
              Buffers: shared hit=21541
              ->  Parallel Index Only Scan using python_readiness_assessments_package_id_d2dd394a on python_readiness_assessments  (cost=0.43..23502.69 rows=750830 width=8) (actual time=0.018..22.411 rows=600667 loops=3)
                    Heap Fetches: 0
                    Buffers: shared hit=21541
Planning Time: 0.027 ms
Execution Time: 67.685 ms

--- digest._freshness: python_readiness #3
SQL: SELECT DISTINCT "python_readiness_assessments"."package_id" AS "package_id" FROM "python_readiness_assessments" WHERE "python_readiness_assessments"."observed_at" >= '2026-08-31 12:00:00+00:00'::timestamptz
HashAggregate  (cost=12953.85..13052.79 rows=9894 width=8) (actual time=59.157..59.590 rows=9900 loops=1)
  Group Key: package_id
  Batches: 1  Memory Usage: 913kB
  Buffers: shared hit=140531
  ->  Index Scan using py_readiness_observed on python_readiness_assessments  (cost=0.43..12254.22 rows=279850 width=8) (actual time=0.007..33.750 rows=277200 loops=1)
        Index Cond: (observed_at >= '2026-08-31 12:00:00+00'::timestamp with time zone)
        Buffers: shared hit=140531
Planning Time: 0.050 ms
Execution Time: 59.796 ms

--- digest._freshness: resolve_identity #1
SQL: SELECT "packages"."id" AS "pk" FROM "packages" WHERE (NOT ("packages"."confidence" IN ('verified')) AND NOT ("packages"."identity_source" = '') AND NOT ("packages"."associator_key" = '')) ORDER BY 1 ASC
Sort  (cost=408.01..408.01 rows=1 width=8) (actual time=0.659..0.659 rows=0 loops=1)
  Sort Key: id
  Sort Method: quicksort  Memory: 25kB
  Buffers: shared hit=233
  ->  Seq Scan on packages  (cost=0.00..408.00 rows=1 width=8) (actual time=0.655..0.656 rows=0 loops=1)
        Filter: (((confidence)::text <> 'verified'::text) AND ((identity_source)::text <> ''::text) AND ((associator_key)::text <> ''::text))
        Rows Removed by Filter: 10000
        Buffers: shared hit=233
Planning Time: 0.049 ms
Execution Time: 0.666 ms

--- digest._freshness: resolve_identity #2
SQL: SELECT DISTINCT "identity_resolution_snapshots"."package_id" AS "package_id" FROM "identity_resolution_snapshots"
HashAggregate  (cost=15848.86..15947.92 rows=9906 width=8) (actual time=27.853..29.057 rows=10000 loops=1)
  Group Key: package_id
  Batches: 1  Memory Usage: 913kB
  Buffers: shared hit=905
  ->  Gather  (cost=1000.42..15799.33 rows=19812 width=8) (actual time=0.110..27.446 rows=10000 loops=1)
        Workers Planned: 2
        Workers Launched: 2
        Buffers: shared hit=905
        ->  Unique  (cost=0.42..12818.13 rows=9906 width=8) (actual time=0.026..21.259 rows=3333 loops=3)
              Buffers: shared hit=905
              ->  Parallel Index Only Scan using identity_resolution_snapshots_package_id_6a9a9bb0 on identity_resolution_snapshots  (cost=0.42..11879.59 rows=375417 width=8) (actual time=0.026..12.019 rows=300333 loops=3)
                    Heap Fetches: 0
                    Buffers: shared hit=905
Planning Time: 0.030 ms
Execution Time: 29.284 ms

--- digest._freshness: resolve_identity #3
SQL: SELECT DISTINCT "identity_resolution_snapshots"."package_id" AS "package_id" FROM "identity_resolution_snapshots" WHERE "identity_resolution_snapshots"."observed_at" >= '2026-09-12 12:00:00+00:00'::timestamptz
HashAggregate  (cost=960.35..1045.56 rows=8521 width=8) (actual time=5.593..6.034 rows=9900 loops=1)
  Group Key: package_id
  Batches: 1  Memory Usage: 913kB
  Buffers: shared hit=11607
  ->  Index Scan using id_resolution_observed on identity_resolution_snapshots  (cost=0.42..912.16 rows=19277 width=8) (actual time=0.009..3.121 rows=19800 loops=1)
        Index Cond: (observed_at >= '2026-09-12 12:00:00+00'::timestamp with time zone)
        Buffers: shared hit=11607
Planning Time: 0.051 ms
Execution Time: 6.234 ms

--- digest._freshness: source_release #1
SQL: SELECT "packages"."id" AS "pk" FROM "packages" WHERE NOT ("packages"."source_repository_url" = '') ORDER BY 1 ASC
Index Scan using packages_pkey on packages  (cost=0.29..531.28 rows=9999 width=8) (actual time=0.008..1.060 rows=10000 loops=1)
  Filter: ((source_repository_url)::text <> ''::text)
  Buffers: shared hit=262
Planning Time: 0.038 ms
Execution Time: 1.274 ms

--- digest._freshness: source_release #2
SQL: SELECT DISTINCT "source_release_snapshots"."package_id" AS "package_id" FROM "source_release_snapshots"
HashAggregate  (cost=15851.32..15950.50 rows=9918 width=8) (actual time=25.318..26.806 rows=10000 loops=1)
  Group Key: package_id
  Batches: 1  Memory Usage: 913kB
  Buffers: shared hit=905
  ->  Gather  (cost=1000.42..15801.73 rows=19836 width=8) (actual time=0.099..25.168 rows=10000 loops=1)
        Workers Planned: 2
        Workers Launched: 2
        Buffers: shared hit=905
        ->  Unique  (cost=0.42..12818.13 rows=9918 width=8) (actual time=0.022..19.028 rows=3333 loops=3)
              Buffers: shared hit=905
              ->  Parallel Index Only Scan using source_release_snapshots_package_id_705a5583 on source_release_snapshots  (cost=0.42..11879.59 rows=375417 width=8) (actual time=0.022..10.581 rows=300333 loops=3)
                    Heap Fetches: 0
                    Buffers: shared hit=905
Planning Time: 0.018 ms
Execution Time: 27.027 ms

--- digest._freshness: source_release #3
SQL: SELECT DISTINCT "source_release_snapshots"."package_id" AS "package_id" FROM "source_release_snapshots" WHERE "source_release_snapshots"."observed_at" >= '2026-09-12 12:00:00+00:00'::timestamptz
HashAggregate  (cost=833.60..920.67 rows=8707 width=8) (actual time=5.435..5.904 rows=9900 loops=1)
  Group Key: package_id
  Batches: 1  Memory Usage: 913kB
  Buffers: shared hit=7365
  ->  Index Scan using src_release_observed on source_release_snapshots  (cost=0.42..782.07 rows=20614 width=8) (actual time=0.008..3.486 rows=19800 loops=1)
        Index Cond: (observed_at >= '2026-09-12 12:00:00+00'::timestamp with time zone)
        Buffers: shared hit=7365
Planning Time: 0.047 ms
Execution Time: 6.108 ms

--- purge.purgeable_policy_runs
SQL: SELECT "policy_runs"."id", "policy_runs"."started_at", "policy_runs"."finished_at", "policy_runs"."status", "policy_runs"."trace_id", "policy_runs"."detail", "policy_runs"."policy_version", "policy_runs"."evidence_cutoff" FROM "policy_runs" WHERE (("policy_runs"."finished_at" < '2026-06-16 12:00:00+00:00'::timestamptz OR ("policy_runs"."finished_at" IS NULL AND "policy_runs"."started_at" < '2026-06-16 12:00:00+00:00'::timestamptz)) AND NOT EXISTS(SELECT 1 AS "a" FROM "package_health" U0 WHERE U0."policy_run_id" = ("policy_runs"."id") LIMIT 1))
Merge Anti Join  (cost=3.64..219.76 rows=1 width=61) (actual time=0.008..0.008 rows=1 loops=1)
  Merge Cond: (policy_runs.id = u0.policy_run_id)
  Buffers: shared hit=5
  ->  Sort  (cost=3.36..3.36 rows=1 width=61) (actual time=0.005..0.005 rows=1 loops=1)
        Sort Key: policy_runs.id
        Sort Method: quicksort  Memory: 25kB
        Buffers: shared hit=2
        ->  Seq Scan on policy_runs  (cost=0.00..3.35 rows=1 width=61) (actual time=0.002..0.004 rows=1 loops=1)
              Filter: ((finished_at < '2026-06-16 12:00:00+00'::timestamp with time zone) OR ((finished_at IS NULL) AND (started_at < '2026-06-16 12:00:00+00'::timestamp with time zone)))
              Rows Removed by Filter: 89
              Buffers: shared hit=2
  ->  Index Only Scan using package_health_policy_run_id_d0e7bf06 on package_health u0  (cost=0.29..190.28 rows=10000 width=8) (actual time=0.002..0.002 rows=1 loops=1)
        Heap Fetches: 0
        Buffers: shared hit=3
Planning:
  Buffers: shared hit=6
Planning Time: 0.042 ms
Execution Time: 0.014 ms

--- purge._older: inventory_snapshots
SQL: SELECT COUNT(*) AS "__count" FROM "inventory_snapshots" WHERE "inventory_snapshots"."observed_at" < '2026-06-16 12:00:00+00:00'::timestamptz
Aggregate  (cost=481.09..481.10 rows=1 width=8) (actual time=1.151..1.151 rows=1 loops=1)
  Buffers: shared hit=27
  ->  Index Only Scan using inv_snapshot_observed on inventory_snapshots  (cost=0.43..433.00 rows=19233 width=0) (actual time=0.004..0.654 rows=20000 loops=1)
        Index Cond: (observed_at < '2026-06-16 12:00:00+00'::timestamp with time zone)
        Heap Fetches: 0
        Buffers: shared hit=27
Planning Time: 0.021 ms
Execution Time: 1.156 ms

--- purge.floor_removable_evidence first batch: inventory_snapshots
SQL: SELECT "inventory_snapshots"."id" AS "pk" FROM "inventory_snapshots" WHERE ("inventory_snapshots"."observed_at" < '2026-06-16 12:00:00+00:00'::timestamptz AND EXISTS(SELECT 1 AS "a" FROM "inventory_snapshots" U0 WHERE (U0."package_id" = ("inventory_snapshots"."package_id") AND U0."source_package_key" = ("inventory_snapshots"."source_package_key") AND U0."observed_at" > ("inventory_snapshots"."observed_at")) LIMIT 1) AND NOT EXISTS(SELECT 1 AS "a" FROM "policy_runs" W0 WHERE (NOT (W0."id" IN (SELECT V0."id" AS "pk" FROM "policy_runs" V0 WHERE ((V0."finished_at" < '2026-06-16 12:00:00+00:00'::timestamptz OR (V0."finished_at" IS NULL AND V0."started_at" < '2026-06-16 12:00:00+00:00'::timestamptz)) AND NOT EXISTS(SELECT 1 AS "a" FROM "package_health" U0 WHERE U0."policy_run_id" = (V0."id") LIMIT 1)))) AND W0."evidence_cutoff" >= ("inventory_snapshots"."observed_at") AND NOT EXISTS(SELECT 1 AS "a" FROM "inventory_snapshots" U0 WHERE (U0."package_id" = ("inventory_snapshots"."package_id") AND U0."source_package_key" = ("inventory_snapshots"."source_package_key") AND U0."observed_at" > ("inventory_snapshots"."observed_at") AND U0."observed_at" <= (W0."evidence_cutoff")) LIMIT 1)) LIMIT 1) AND "inventory_snapshots"."id" > 0) ORDER BY 1 ASC LIMIT 1000
Limit  (cost=1220.64..409341.33 rows=1000 width=8) (actual time=23.801..144.034 rows=1000 loops=1)
  Buffers: shared hit=371660
  ->  Nested Loop Anti Join  (cost=1220.64..2181401.32 rows=5342 width=8) (actual time=16.049..136.229 rows=1000 loops=1)
        Join Filter: ((w0.evidence_cutoff >= inventory_snapshots.observed_at) AND (NOT EXISTS(SubPlan 2)))
        Rows Removed by Join Filter: 89000
        Buffers: shared hit=371660
        ->  Gather Merge  (cost=1000.88..127601.57 rows=6411 width=36) (actual time=15.621..18.595 rows=1000 loops=1)
              Workers Planned: 2
              Workers Launched: 2
              Buffers: shared hit=14797
              ->  Nested Loop Semi Join  (cost=0.85..125861.55 rows=2671 width=36) (actual time=3.250..5.626 rows=975 loops=3)
                    Buffers: shared hit=14797
                    ->  Parallel Index Scan using inventory_snapshots_pkey on inventory_snapshots  (cost=0.43..64922.84 rows=8014 width=36) (actual time=3.233..3.324 rows=984 loops=3)
                          Index Cond: (id > 0)
                          Filter: (observed_at < '2026-06-16 12:00:00+00'::timestamp with time zone)
                          Buffers: shared hit=58
                    ->  Index Scan using inv_snapshot_source_key on inventory_snapshots u0  (cost=0.43..7.59 rows=1 width=28) (actual time=0.002..0.002 rows=1 loops=2953)
                          Index Cond: ((source_package_key)::text = (inventory_snapshots.source_package_key)::text)
                          Filter: ((observed_at > inventory_snapshots.observed_at) AND (inventory_snapshots.package_id = package_id))
                          Rows Removed by Filter: 1
                          Buffers: shared hit=14739
        ->  Materialize  (cost=219.77..223.12 rows=45 width=8) (actual time=0.000..0.003 rows=89 loops=1000)
              Buffers: shared hit=7
              ->  Seq Scan on policy_runs w0  (cost=219.77..222.89 rows=45 width=8) (actual time=0.231..0.241 rows=89 loops=1)
                    Filter: (NOT (ANY (id = (hashed SubPlan 1).col1)))
                    Rows Removed by Filter: 1
                    Buffers: shared hit=7
                    SubPlan 1
                      ->  Merge Anti Join  (cost=3.64..219.76 rows=1 width=8) (actual time=0.089..0.090 rows=1 loops=1)
                            Merge Cond: (v0.id = u0_1.policy_run_id)
                            Buffers: shared hit=5
                            ->  Sort  (cost=3.36..3.36 rows=1 width=8) (actual time=0.079..0.079 rows=1 loops=1)
                                  Sort Key: v0.id
                                  Sort Method: quicksort  Memory: 25kB
                                  Buffers: shared hit=2
                                  ->  Seq Scan on policy_runs v0  (cost=0.00..3.35 rows=1 width=8) (actual time=0.070..0.073 rows=1 loops=1)
                                        Filter: ((finished_at < '2026-06-16 12:00:00+00'::timestamp with time zone) OR ((finished_at IS NULL) AND (started_at < '2026-06-16 12:00:00+00'::timestamp with time zone)))
                                        Rows Removed by Filter: 89
                                        Buffers: shared hit=2
                            ->  Index Only Scan using package_health_policy_run_id_d0e7bf06 on package_health u0_1  (cost=0.29..190.28 rows=10000 width=8) (actual time=0.006..0.006 rows=1 loops=1)
                                  Heap Fetches: 0
                                  Buffers: shared hit=3
        SubPlan 2
          ->  Index Scan using inv_snapshot_pkg_observed on inventory_snapshots u0_2  (cost=0.43..8.45 rows=1 width=0) (actual time=0.001..0.001 rows=1 loops=89000)
                Index Cond: ((package_id = inventory_snapshots.package_id) AND (observed_at > inventory_snapshots.observed_at) AND (observed_at <= w0.evidence_cutoff))
                Filter: ((source_package_key)::text = (inventory_snapshots.source_package_key)::text)
                Rows Removed by Filter: 0
                Buffers: shared hit=356856
Planning:
  Buffers: shared hit=42
Planning Time: 0.274 ms
JIT:
  Functions: 92
  Options: Inlining false, Optimization false, Expressions true, Deforming true
  Timing: Generation 1.719 ms (Deform 0.783 ms), Inlining 0.000 ms, Optimization 0.919 ms, Emission 16.595 ms, Total 19.232 ms
Execution Time: 144.780 ms

--- purge.removable_evidence first batch: inventory_snapshots
SQL: SELECT "inventory_snapshots"."id" AS "pk" FROM "inventory_snapshots" WHERE ("inventory_snapshots"."observed_at" < '2026-06-16 12:00:00+00:00'::timestamptz AND EXISTS(SELECT 1 AS "a" FROM "inventory_snapshots" U0 WHERE (U0."package_id" = ("inventory_snapshots"."package_id") AND U0."source_package_key" = ("inventory_snapshots"."source_package_key") AND U0."observed_at" > ("inventory_snapshots"."observed_at")) LIMIT 1) AND NOT EXISTS(SELECT 1 AS "a" FROM "policy_runs" W0 WHERE (NOT (W0."id" IN (SELECT V0."id" AS "pk" FROM "policy_runs" V0 WHERE ((V0."finished_at" < '2026-06-16 12:00:00+00:00'::timestamptz OR (V0."finished_at" IS NULL AND V0."started_at" < '2026-06-16 12:00:00+00:00'::timestamptz)) AND NOT EXISTS(SELECT 1 AS "a" FROM "package_health" U0 WHERE U0."policy_run_id" = (V0."id") LIMIT 1)))) AND W0."evidence_cutoff" >= ("inventory_snapshots"."observed_at") AND NOT EXISTS(SELECT 1 AS "a" FROM "inventory_snapshots" U0 WHERE (U0."package_id" = ("inventory_snapshots"."package_id") AND U0."source_package_key" = ("inventory_snapshots"."source_package_key") AND U0."observed_at" > ("inventory_snapshots"."observed_at") AND U0."observed_at" <= (W0."evidence_cutoff")) LIMIT 1)) LIMIT 1) AND "inventory_snapshots"."id" > 0) ORDER BY 1 ASC LIMIT 1000
Limit  (cost=1220.64..409341.33 rows=1000 width=8) (actual time=23.490..144.186 rows=1000 loops=1)
  Buffers: shared hit=371658
  ->  Nested Loop Anti Join  (cost=1220.64..2181401.32 rows=5342 width=8) (actual time=15.512..136.162 rows=1000 loops=1)
        Join Filter: ((w0.evidence_cutoff >= inventory_snapshots.observed_at) AND (NOT EXISTS(SubPlan 2)))
        Rows Removed by Join Filter: 89000
        Buffers: shared hit=371658
        ->  Gather Merge  (cost=1000.88..127601.57 rows=6411 width=36) (actual time=14.972..18.107 rows=1000 loops=1)
              Workers Planned: 2
              Workers Launched: 2
              Buffers: shared hit=14795
              ->  Nested Loop Semi Join  (cost=0.85..125861.55 rows=2671 width=36) (actual time=3.223..5.863 rows=975 loops=3)
                    Buffers: shared hit=14795
                    ->  Parallel Index Scan using inventory_snapshots_pkey on inventory_snapshots  (cost=0.43..64922.84 rows=8014 width=36) (actual time=3.200..3.307 rows=984 loops=3)
                          Index Cond: (id > 0)
                          Filter: (observed_at < '2026-06-16 12:00:00+00'::timestamp with time zone)
                          Buffers: shared hit=56
                    ->  Index Scan using inv_snapshot_source_key on inventory_snapshots u0  (cost=0.43..7.59 rows=1 width=28) (actual time=0.002..0.002 rows=1 loops=2953)
                          Index Cond: ((source_package_key)::text = (inventory_snapshots.source_package_key)::text)
                          Filter: ((observed_at > inventory_snapshots.observed_at) AND (inventory_snapshots.package_id = package_id))
                          Rows Removed by Filter: 1
                          Buffers: shared hit=14739
        ->  Materialize  (cost=219.77..223.12 rows=45 width=8) (actual time=0.000..0.003 rows=89 loops=1000)
              Buffers: shared hit=7
              ->  Seq Scan on policy_runs w0  (cost=219.77..222.89 rows=45 width=8) (actual time=0.330..0.340 rows=89 loops=1)
                    Filter: (NOT (ANY (id = (hashed SubPlan 1).col1)))
                    Rows Removed by Filter: 1
                    Buffers: shared hit=7
                    SubPlan 1
                      ->  Merge Anti Join  (cost=3.64..219.76 rows=1 width=8) (actual time=0.133..0.134 rows=1 loops=1)
                            Merge Cond: (v0.id = u0_1.policy_run_id)
                            Buffers: shared hit=5
                            ->  Sort  (cost=3.36..3.36 rows=1 width=8) (actual time=0.118..0.118 rows=1 loops=1)
                                  Sort Key: v0.id
                                  Sort Method: quicksort  Memory: 25kB
                                  Buffers: shared hit=2
                                  ->  Seq Scan on policy_runs v0  (cost=0.00..3.35 rows=1 width=8) (actual time=0.108..0.111 rows=1 loops=1)
                                        Filter: ((finished_at < '2026-06-16 12:00:00+00'::timestamp with time zone) OR ((finished_at IS NULL) AND (started_at < '2026-06-16 12:00:00+00'::timestamp with time zone)))
                                        Rows Removed by Filter: 89
                                        Buffers: shared hit=2
                            ->  Index Only Scan using package_health_policy_run_id_d0e7bf06 on package_health u0_1  (cost=0.29..190.28 rows=10000 width=8) (actual time=0.008..0.008 rows=1 loops=1)
                                  Heap Fetches: 0
                                  Buffers: shared hit=3
        SubPlan 2
          ->  Index Scan using inv_snapshot_pkg_observed on inventory_snapshots u0_2  (cost=0.43..8.45 rows=1 width=0) (actual time=0.001..0.001 rows=1 loops=89000)
                Index Cond: ((package_id = inventory_snapshots.package_id) AND (observed_at > inventory_snapshots.observed_at) AND (observed_at <= w0.evidence_cutoff))
                Filter: ((source_package_key)::text = (inventory_snapshots.source_package_key)::text)
                Rows Removed by Filter: 0
                Buffers: shared hit=356856
Planning:
  Buffers: shared hit=42
Planning Time: 0.378 ms
JIT:
  Functions: 92
  Options: Inlining false, Optimization false, Expressions true, Deforming true
  Timing: Generation 1.685 ms (Deform 0.758 ms), Inlining 0.000 ms, Optimization 0.974 ms, Emission 16.683 ms, Total 19.341 ms
Execution Time: 145.073 ms

--- purge._older: source_release_snapshots
SQL: SELECT COUNT(*) AS "__count" FROM "source_release_snapshots" WHERE "source_release_snapshots"."observed_at" < '2026-06-16 12:00:00+00:00'::timestamptz
Aggregate  (cost=238.09..238.09 rows=1 width=8) (actual time=0.525..0.525 rows=1 loops=1)
  Buffers: shared hit=12
  ->  Index Only Scan using src_release_observed on source_release_snapshots  (cost=0.42..213.88 rows=9683 width=0) (actual time=0.003..0.292 rows=10000 loops=1)
        Index Cond: (observed_at < '2026-06-16 12:00:00+00'::timestamp with time zone)
        Heap Fetches: 0
        Buffers: shared hit=12
Planning Time: 0.016 ms
Execution Time: 0.529 ms

--- purge.floor_removable_evidence first batch: source_release_snapshots
SQL: SELECT "source_release_snapshots"."id" AS "pk" FROM "source_release_snapshots" WHERE ("source_release_snapshots"."observed_at" < '2026-06-16 12:00:00+00:00'::timestamptz AND EXISTS(SELECT 1 AS "a" FROM "source_release_snapshots" U0 WHERE (U0."package_id" = ("source_release_snapshots"."package_id") AND U0."observed_at" > ("source_release_snapshots"."observed_at")) LIMIT 1) AND NOT EXISTS(SELECT 1 AS "a" FROM "policy_runs" W0 WHERE (NOT (W0."id" IN (SELECT V0."id" AS "pk" FROM "policy_runs" V0 WHERE ((V0."finished_at" < '2026-06-16 12:00:00+00:00'::timestamptz OR (V0."finished_at" IS NULL AND V0."started_at" < '2026-06-16 12:00:00+00:00'::timestamptz)) AND NOT EXISTS(SELECT 1 AS "a" FROM "package_health" U0 WHERE U0."policy_run_id" = (V0."id") LIMIT 1)))) AND W0."evidence_cutoff" >= ("source_release_snapshots"."observed_at") AND NOT EXISTS(SELECT 1 AS "a" FROM "source_release_snapshots" U0 WHERE (U0."package_id" = ("source_release_snapshots"."package_id") AND U0."observed_at" > ("source_release_snapshots"."observed_at") AND U0."observed_at" <= (W0."evidence_cutoff")) LIMIT 1)) LIMIT 1) AND "source_release_snapshots"."id" > 0) ORDER BY 1 ASC LIMIT 1000
Limit  (cost=220.62..220060.16 rows=1000 width=8) (actual time=5.744..109.694 rows=1000 loops=1)
  Buffers: shared hit=270152
  ->  Nested Loop Anti Join  (cost=220.62..591589.00 rows=2690 width=8) (actual time=0.292..104.202 rows=1000 loops=1)
        Join Filter: ((w0.evidence_cutoff >= source_release_snapshots.observed_at) AND (NOT EXISTS(SubPlan 2)))
        Rows Removed by Join Filter: 89000
        Buffers: shared hit=270152
        ->  Nested Loop Semi Join  (cost=0.85..46483.10 rows=3228 width=24) (actual time=0.026..2.072 rows=1000 loops=1)
              Buffers: shared hit=3051
              ->  Index Scan using source_release_snapshots_pkey on source_release_snapshots  (cost=0.42..41668.43 rows=9683 width=24) (actual time=0.010..0.106 rows=1010 loops=1)
                    Index Cond: (id > 0)
                    Filter: (observed_at < '2026-06-16 12:00:00+00'::timestamp with time zone)
                    Buffers: shared hit=20
              ->  Index Only Scan using src_release_pkg_observed on source_release_snapshots u0  (cost=0.42..2.81 rows=30 width=16) (actual time=0.002..0.002 rows=1 loops=1010)
                    Index Cond: ((package_id = source_release_snapshots.package_id) AND (observed_at > source_release_snapshots.observed_at))
                    Heap Fetches: 0
                    Buffers: shared hit=3031
        ->  Materialize  (cost=219.77..223.12 rows=45 width=8) (actual time=0.000..0.002 rows=89 loops=1000)
              Buffers: shared hit=7
              ->  Seq Scan on policy_runs w0  (cost=219.77..222.89 rows=45 width=8) (actual time=0.137..0.146 rows=89 loops=1)
                    Filter: (NOT (ANY (id = (hashed SubPlan 1).col1)))
                    Rows Removed by Filter: 1
                    Buffers: shared hit=7
                    SubPlan 1
                      ->  Merge Anti Join  (cost=3.64..219.76 rows=1 width=8) (actual time=0.065..0.066 rows=1 loops=1)
                            Merge Cond: (v0.id = u0_1.policy_run_id)
                            Buffers: shared hit=5
                            ->  Sort  (cost=3.36..3.36 rows=1 width=8) (actual time=0.057..0.058 rows=1 loops=1)
                                  Sort Key: v0.id
                                  Sort Method: quicksort  Memory: 25kB
                                  Buffers: shared hit=2
                                  ->  Seq Scan on policy_runs v0  (cost=0.00..3.35 rows=1 width=8) (actual time=0.052..0.055 rows=1 loops=1)
                                        Filter: ((finished_at < '2026-06-16 12:00:00+00'::timestamp with time zone) OR ((finished_at IS NULL) AND (started_at < '2026-06-16 12:00:00+00'::timestamp with time zone)))
                                        Rows Removed by Filter: 89
                                        Buffers: shared hit=2
                            ->  Index Only Scan using package_health_policy_run_id_d0e7bf06 on package_health u0_1  (cost=0.29..190.28 rows=10000 width=8) (actual time=0.005..0.005 rows=1 loops=1)
                                  Heap Fetches: 0
                                  Buffers: shared hit=3
        SubPlan 2
          ->  Index Only Scan using src_release_pkg_observed on source_release_snapshots u0_2  (cost=0.42..4.45 rows=1 width=0) (actual time=0.001..0.001 rows=1 loops=89000)
                Index Cond: ((package_id = source_release_snapshots.package_id) AND (observed_at > source_release_snapshots.observed_at) AND (observed_at <= w0.evidence_cutoff))
                Heap Fetches: 0
                Buffers: shared hit=267094
Planning:
  Buffers: shared hit=26
Planning Time: 0.209 ms
JIT:
  Functions: 46
  Options: Inlining false, Optimization false, Expressions true, Deforming true
  Timing: Generation 0.551 ms (Deform 0.206 ms), Inlining 0.000 ms, Optimization 0.276 ms, Emission 5.238 ms, Total 6.066 ms
Execution Time: 110.319 ms

--- purge.removable_evidence first batch: source_release_snapshots
SQL: SELECT "source_release_snapshots"."id" AS "pk" FROM "source_release_snapshots" WHERE ("source_release_snapshots"."observed_at" < '2026-06-16 12:00:00+00:00'::timestamptz AND EXISTS(SELECT 1 AS "a" FROM "source_release_snapshots" U0 WHERE (U0."package_id" = ("source_release_snapshots"."package_id") AND U0."observed_at" > ("source_release_snapshots"."observed_at")) LIMIT 1) AND NOT EXISTS(SELECT 1 AS "a" FROM "policy_runs" W0 WHERE (NOT (W0."id" IN (SELECT V0."id" AS "pk" FROM "policy_runs" V0 WHERE ((V0."finished_at" < '2026-06-16 12:00:00+00:00'::timestamptz OR (V0."finished_at" IS NULL AND V0."started_at" < '2026-06-16 12:00:00+00:00'::timestamptz)) AND NOT EXISTS(SELECT 1 AS "a" FROM "package_health" U0 WHERE U0."policy_run_id" = (V0."id") LIMIT 1)))) AND W0."evidence_cutoff" >= ("source_release_snapshots"."observed_at") AND NOT EXISTS(SELECT 1 AS "a" FROM "source_release_snapshots" U0 WHERE (U0."package_id" = ("source_release_snapshots"."package_id") AND U0."observed_at" > ("source_release_snapshots"."observed_at") AND U0."observed_at" <= (W0."evidence_cutoff")) LIMIT 1)) LIMIT 1) AND NOT EXISTS(SELECT 1 AS "a" FROM "package_currency" X0 WHERE (X0."source_snapshot_id" = ("source_release_snapshots"."id") AND X0."policy_run_id" IN (SELECT W0."id" AS "pk" FROM "policy_runs" W0 WHERE NOT (W0."id" IN (SELECT V0."id" AS "pk" FROM "policy_runs" V0 WHERE ((V0."finished_at" < '2026-06-16 12:00:00+00:00'::timestamptz OR (V0."finished_at" IS NULL AND V0."started_at" < '2026-06-16 12:00:00+00:00'::timestamptz)) AND NOT EXISTS(SELECT 1 AS "a" FROM "package_health" U0 WHERE U0."policy_run_id" = (V0."id") LIMIT 1)))))) LIMIT 1) AND NOT EXISTS(SELECT 1 AS "a" FROM "package_remediation" X0 WHERE (X0."source_snapshot_id" = ("source_release_snapshots"."id") AND X0."policy_run_id" IN (SELECT W0."id" AS "pk" FROM "policy_runs" W0 WHERE NOT (W0."id" IN (SELECT V0."id" AS "pk" FROM "policy_runs" V0 WHERE ((V0."finished_at" < '2026-06-16 12:00:00+00:00'::timestamptz OR (V0."finished_at" IS NULL AND V0."started_at" < '2026-06-16 12:00:00+00:00'::timestamptz)) AND NOT EXISTS(SELECT 1 AS "a" FROM "package_health" U0 WHERE U0."policy_run_id" = (V0."id") LIMIT 1)))))) LIMIT 1) AND "source_release_snapshots"."id" > 0) ORDER BY 1 ASC LIMIT 1000
Limit  (cost=56216.35..260985.17 rows=1000 width=8) (actual time=214.217..385.947 rows=1000 loops=1)
  Buffers: shared hit=270532 read=29935
  ->  Nested Loop Anti Join  (cost=56216.35..607044.48 rows=2690 width=8) (actual time=196.834..368.433 rows=1000 loops=1)
        Join Filter: ((w0.evidence_cutoff >= source_release_snapshots.observed_at) AND (NOT EXISTS(SubPlan 2)))
        Rows Removed by Join Filter: 89000
        Buffers: shared hit=270532 read=29935
        ->  Nested Loop Semi Join  (cost=55996.59..61938.59 rows=3228 width=24) (actual time=195.294..203.886 rows=1000 loops=1)
              Buffers: shared hit=3431 read=29935
              ->  Gather Merge  (cost=55996.16..57123.91 rows=9683 width=24) (actual time=195.193..200.059 rows=1010 loops=1)
                    Workers Planned: 2
                    Workers Launched: 2
                    Buffers: shared hit=400 read=29935
                    ->  Sort  (cost=54996.14..55006.23 rows=4035 width=24) (actual time=184.605..184.775 rows=683 loops=3)
                          Sort Key: source_release_snapshots.id
                          Sort Method: quicksort  Memory: 25kB
                          Buffers: shared hit=400 read=29935
                          Worker 0:  Sort Method: quicksort  Memory: 25kB
                          Worker 1:  Sort Method: quicksort  Memory: 775kB
                          ->  Parallel Hash Right Anti Join  (cost=34661.61..54754.48 rows=4035 width=24) (actual time=183.055..183.663 rows=3333 loops=3)
                                Hash Cond: (x0.source_snapshot_id = source_release_snapshots.id)
                                Buffers: shared hit=390 read=29935
                                ->  Hash Join  (cost=223.45..19613.19 rows=187500 width=8) (actual time=0.990..92.948 rows=296667 loops=3)
                                      Hash Cond: (x0.policy_run_id = w0_1.id)
                                      Buffers: shared hit=117 read=14513
                                      ->  Parallel Seq Scan on package_currency x0  (cost=0.00..18359.00 rows=375000 width=16) (actual time=0.015..30.316 rows=300000 loops=3)
                                            Buffers: shared hit=96 read=14513
                                      ->  Hash  (cost=222.89..222.89 rows=45 width=8) (actual time=0.296..0.301 rows=89 loops=3)
                                            Buckets: 1024  Batches: 1  Memory Usage: 12kB
                                            Buffers: shared hit=21
                                            ->  Seq Scan on policy_runs w0_1  (cost=219.77..222.89 rows=45 width=8) (actual time=0.282..0.292 rows=89 loops=3)
                                                  Filter: (NOT (ANY (id = (hashed SubPlan 3).col1)))
                                                  Rows Removed by Filter: 1
                                                  Buffers: shared hit=21
                                                  SubPlan 3
                                                    ->  Merge Anti Join  (cost=3.64..219.76 rows=1 width=8) (actual time=0.117..0.120 rows=1 loops=3)
                                                          Merge Cond: (v0_1.id = u0_3.policy_run_id)
                                                          Buffers: shared hit=15
                                                          ->  Sort  (cost=3.36..3.36 rows=1 width=8) (actual time=0.106..0.107 rows=1 loops=3)
                                                                Sort Key: v0_1.id
                                                                Sort Method: quicksort  Memory: 25kB
                                                                Buffers: shared hit=6
                                                                Worker 0:  Sort Method: quicksort  Memory: 25kB
                                                                Worker 1:  Sort Method: quicksort  Memory: 25kB
                                                                ->  Seq Scan on policy_runs v0_1  (cost=0.00..3.35 rows=1 width=8) (actual time=0.100..0.103 rows=1 loops=3)
                                                                      Filter: ((finished_at < '2026-06-16 12:00:00+00'::timestamp with time zone) OR ((finished_at IS NULL) AND (started_at < '2026-06-16 12:00:00+00'::timestamp with time zone)))
                                                                      Rows Removed by Filter: 89
                                                                      Buffers: shared hit=6
                                                          ->  Index Only Scan using package_health_policy_run_id_d0e7bf06 on package_health u0_3  (cost=0.29..190.28 rows=10000 width=8) (actual time=0.006..0.006 rows=1 loops=3)
                                                                Heap Fetches: 0
                                                                Buffers: shared hit=9
                                ->  Parallel Hash  (cost=34387.72..34387.72 rows=4035 width=24) (actual time=78.437..78.454 rows=3333 loops=3)
                                      Buckets: 16384  Batches: 1  Memory Usage: 704kB
                                      Buffers: shared hit=273 read=15422
                                      ->  Parallel Hash Right Anti Join  (cost=13596.80..34387.72 rows=4035 width=24) (actual time=77.263..77.465 rows=3333 loops=3)
                                            Hash Cond: (x0_1.source_snapshot_id = source_release_snapshots.id)
                                            Buffers: shared hit=273 read=15422
                                            ->  Hash Join  (cost=223.45..20522.19 rows=187500 width=8) (actual time=11.202..68.989 rows=296667 loops=3)
                                                  Hash Cond: (x0_1.policy_run_id = w0_2.id)
                                                  Buffers: shared hit=119 read=15422
                                                  ->  Parallel Seq Scan on package_remediation x0_1  (cost=0.00..19268.00 rows=375000 width=16) (actual time=0.019..17.633 rows=300000 loops=3)
                                                        Buffers: shared hit=96 read=15422
                                                  ->  Hash  (cost=222.89..222.89 rows=45 width=8) (actual time=10.629..10.631 rows=89 loops=3)
                                                        Buckets: 1024  Batches: 1  Memory Usage: 12kB
                                                        Buffers: shared hit=23
                                                        ->  Seq Scan on policy_runs w0_2  (cost=219.77..222.89 rows=45 width=8) (actual time=10.616..10.624 rows=89 loops=3)
                                                              Filter: (NOT (ANY (id = (hashed SubPlan 4).col1)))
                                                              Rows Removed by Filter: 1
                                                              Buffers: shared hit=23
                                                              SubPlan 4
                                                                ->  Merge Anti Join  (cost=3.64..219.76 rows=1 width=8) (actual time=0.102..0.103 rows=1 loops=3)
                                                                      Merge Cond: (v0_2.id = u0_4.policy_run_id)
                                                                      Buffers: shared hit=17
                                                                      ->  Sort  (cost=3.36..3.36 rows=1 width=8) (actual time=0.082..0.082 rows=1 loops=3)
                                                                            Sort Key: v0_2.id
                                                                            Sort Method: quicksort  Memory: 25kB
                                                                            Buffers: shared hit=6
                                                                            Worker 0:  Sort Method: quicksort  Memory: 25kB
                                                                            Worker 1:  Sort Method: quicksort  Memory: 25kB
                                                                            ->  Seq Scan on policy_runs v0_2  (cost=0.00..3.35 rows=1 width=8) (actual time=0.076..0.079 rows=1 loops=3)
                                                                                  Filter: ((finished_at < '2026-06-16 12:00:00+00'::timestamp with time zone) OR ((finished_at IS NULL) AND (started_at < '2026-06-16 12:00:00+00'::timestamp with time zone)))
                                                                                  Rows Removed by Filter: 89
                                                                                  Buffers: shared hit=6
                                                                      ->  Index Only Scan using package_health_policy_run_id_d0e7bf06 on package_health u0_4  (cost=0.29..190.28 rows=10000 width=8) (actual time=0.015..0.015 rows=1 loops=3)
                                                                            Heap Fetches: 0
                                                                            Buffers: shared hit=11
                                            ->  Parallel Hash  (cost=13322.91..13322.91 rows=4035 width=24) (actual time=0.672..0.674 rows=3333 loops=3)
                                                  Buckets: 16384  Batches: 1  Memory Usage: 704kB
                                                  Buffers: shared hit=154
                                                  ->  Parallel Bitmap Heap Scan on source_release_snapshots  (cost=119.47..13322.91 rows=4035 width=24) (actual time=0.161..1.207 rows=10000 loops=1)
                                                        Recheck Cond: (observed_at < '2026-06-16 12:00:00+00'::timestamp with time zone)
                                                        Filter: (id > 0)
                                                        Heap Blocks: exact=143
                                                        Buffers: shared hit=154
                                                        ->  Bitmap Index Scan on src_release_observed  (cost=0.00..117.05 rows=9683 width=0) (actual time=0.139..0.139 rows=10000 loops=1)
                                                              Index Cond: (observed_at < '2026-06-16 12:00:00+00'::timestamp with time zone)
                                                              Buffers: shared hit=11
              ->  Index Only Scan using src_release_pkg_observed on source_release_snapshots u0  (cost=0.42..2.81 rows=30 width=16) (actual time=0.003..0.003 rows=1 loops=1010)
                    Index Cond: ((package_id = source_release_snapshots.package_id) AND (observed_at > source_release_snapshots.observed_at))
                    Heap Fetches: 0
                    Buffers: shared hit=3031
        ->  Materialize  (cost=219.77..223.12 rows=45 width=8) (actual time=0.001..0.006 rows=89 loops=1000)
              Buffers: shared hit=7
              ->  Seq Scan on policy_runs w0  (cost=219.77..222.89 rows=45 width=8) (actual time=1.099..1.122 rows=89 loops=1)
                    Filter: (NOT (ANY (id = (hashed SubPlan 1).col1)))
                    Rows Removed by Filter: 1
                    Buffers: shared hit=7
                    SubPlan 1
                      ->  Merge Anti Join  (cost=3.64..219.76 rows=1 width=8) (actual time=0.467..0.479 rows=1 loops=1)
                            Merge Cond: (v0.id = u0_1.policy_run_id)
                            Buffers: shared hit=5
                            ->  Sort  (cost=3.36..3.36 rows=1 width=8) (actual time=0.429..0.440 rows=1 loops=1)
                                  Sort Key: v0.id
                                  Sort Method: quicksort  Memory: 25kB
                                  Buffers: shared hit=2
                                  ->  Seq Scan on policy_runs v0  (cost=0.00..3.35 rows=1 width=8) (actual time=0.410..0.420 rows=1 loops=1)
                                        Filter: ((finished_at < '2026-06-16 12:00:00+00'::timestamp with time zone) OR ((finished_at IS NULL) AND (started_at < '2026-06-16 12:00:00+00'::timestamp with time zone)))
                                        Rows Removed by Filter: 89
                                        Buffers: shared hit=2
                            ->  Index Only Scan using package_health_policy_run_id_d0e7bf06 on package_health u0_1  (cost=0.29..190.28 rows=10000 width=8) (actual time=0.025..0.025 rows=1 loops=1)
                                  Heap Fetches: 0
                                  Buffers: shared hit=3
        SubPlan 2
          ->  Index Only Scan using src_release_pkg_observed on source_release_snapshots u0_2  (cost=0.42..4.45 rows=1 width=0) (actual time=0.002..0.002 rows=1 loops=89000)
                Index Cond: ((package_id = source_release_snapshots.package_id) AND (observed_at > source_release_snapshots.observed_at) AND (observed_at <= w0.evidence_cutoff))
                Heap Fetches: 0
                Buffers: shared hit=267094
Planning:
  Buffers: shared hit=46
Planning Time: 0.580 ms
JIT:
  Functions: 305
  Options: Inlining false, Optimization false, Expressions true, Deforming true
  Timing: Generation 5.085 ms (Deform 2.425 ms), Inlining 0.000 ms, Optimization 2.771 ms, Emission 46.990 ms, Total 54.846 ms
Execution Time: 387.720 ms

--- purge._older: pypi_release_snapshots
SQL: SELECT COUNT(*) AS "__count" FROM "pypi_release_snapshots" WHERE "pypi_release_snapshots"."observed_at" < '2026-06-16 12:00:00+00:00'::timestamptz
Aggregate  (cost=238.59..238.59 rows=1 width=8) (actual time=0.599..0.599 rows=1 loops=1)
  Buffers: shared hit=12
  ->  Index Only Scan using pypi_release_observed on pypi_release_snapshots  (cost=0.42..214.31 rows=9708 width=0) (actual time=0.007..0.333 rows=10000 loops=1)
        Index Cond: (observed_at < '2026-06-16 12:00:00+00'::timestamp with time zone)
        Heap Fetches: 0
        Buffers: shared hit=12
Planning Time: 0.049 ms
Execution Time: 0.609 ms

--- purge.floor_removable_evidence first batch: pypi_release_snapshots
SQL: SELECT "pypi_release_snapshots"."id" AS "pk" FROM "pypi_release_snapshots" WHERE ("pypi_release_snapshots"."observed_at" < '2026-06-16 12:00:00+00:00'::timestamptz AND EXISTS(SELECT 1 AS "a" FROM "pypi_release_snapshots" U0 WHERE (U0."package_id" = ("pypi_release_snapshots"."package_id") AND U0."observed_at" > ("pypi_release_snapshots"."observed_at")) LIMIT 1) AND NOT EXISTS(SELECT 1 AS "a" FROM "policy_runs" W0 WHERE (NOT (W0."id" IN (SELECT V0."id" AS "pk" FROM "policy_runs" V0 WHERE ((V0."finished_at" < '2026-06-16 12:00:00+00:00'::timestamptz OR (V0."finished_at" IS NULL AND V0."started_at" < '2026-06-16 12:00:00+00:00'::timestamptz)) AND NOT EXISTS(SELECT 1 AS "a" FROM "package_health" U0 WHERE U0."policy_run_id" = (V0."id") LIMIT 1)))) AND W0."evidence_cutoff" >= ("pypi_release_snapshots"."observed_at") AND NOT EXISTS(SELECT 1 AS "a" FROM "pypi_release_snapshots" U0 WHERE (U0."package_id" = ("pypi_release_snapshots"."package_id") AND U0."observed_at" > ("pypi_release_snapshots"."observed_at") AND U0."observed_at" <= (W0."evidence_cutoff")) LIMIT 1)) LIMIT 1) AND "pypi_release_snapshots"."id" > 0) ORDER BY 1 ASC LIMIT 1000
Limit  (cost=220.62..219656.46 rows=1000 width=8) (actual time=6.235..109.991 rows=1000 loops=1)
  Buffers: shared hit=270151
  ->  Nested Loop Anti Join  (cost=220.62..592039.09 rows=2697 width=8) (actual time=0.302..104.021 rows=1000 loops=1)
        Join Filter: ((w0.evidence_cutoff >= pypi_release_snapshots.observed_at) AND (NOT EXISTS(SubPlan 2)))
        Rows Removed by Join Filter: 89000
        Buffers: shared hit=270151
        ->  Nested Loop Semi Join  (cost=0.85..45518.78 rows=3236 width=24) (actual time=0.020..2.039 rows=1000 loops=1)
              Buffers: shared hit=3050
              ->  Index Scan using pypi_release_snapshots_pkey on pypi_release_snapshots  (cost=0.42..40693.43 rows=9708 width=24) (actual time=0.012..0.120 rows=1010 loops=1)
                    Index Cond: (id > 0)
                    Filter: (observed_at < '2026-06-16 12:00:00+00'::timestamp with time zone)
                    Buffers: shared hit=19
              ->  Index Only Scan using pypi_release_pkg_observed on pypi_release_snapshots u0  (cost=0.42..2.80 rows=30 width=16) (actual time=0.002..0.002 rows=1 loops=1010)
                    Index Cond: ((package_id = pypi_release_snapshots.package_id) AND (observed_at > pypi_release_snapshots.observed_at))
                    Heap Fetches: 0
                    Buffers: shared hit=3031
        ->  Materialize  (cost=219.77..223.12 rows=45 width=8) (actual time=0.000..0.002 rows=89 loops=1000)
              Buffers: shared hit=7
              ->  Seq Scan on policy_runs w0  (cost=219.77..222.89 rows=45 width=8) (actual time=0.155..0.164 rows=89 loops=1)
                    Filter: (NOT (ANY (id = (hashed SubPlan 1).col1)))
                    Rows Removed by Filter: 1
                    Buffers: shared hit=7
                    SubPlan 1
                      ->  Merge Anti Join  (cost=3.64..219.76 rows=1 width=8) (actual time=0.076..0.077 rows=1 loops=1)
                            Merge Cond: (v0.id = u0_1.policy_run_id)
                            Buffers: shared hit=5
                            ->  Sort  (cost=3.36..3.36 rows=1 width=8) (actual time=0.068..0.069 rows=1 loops=1)
                                  Sort Key: v0.id
                                  Sort Method: quicksort  Memory: 25kB
                                  Buffers: shared hit=2
                                  ->  Seq Scan on policy_runs v0  (cost=0.00..3.35 rows=1 width=8) (actual time=0.063..0.066 rows=1 loops=1)
                                        Filter: ((finished_at < '2026-06-16 12:00:00+00'::timestamp with time zone) OR ((finished_at IS NULL) AND (started_at < '2026-06-16 12:00:00+00'::timestamp with time zone)))
                                        Rows Removed by Filter: 89
                                        Buffers: shared hit=2
                            ->  Index Only Scan using package_health_policy_run_id_d0e7bf06 on package_health u0_1  (cost=0.29..190.28 rows=10000 width=8) (actual time=0.004..0.005 rows=1 loops=1)
                                  Heap Fetches: 0
                                  Buffers: shared hit=3
        SubPlan 2
          ->  Index Only Scan using pypi_release_pkg_observed on pypi_release_snapshots u0_2  (cost=0.42..4.45 rows=1 width=0) (actual time=0.001..0.001 rows=1 loops=89000)
                Index Cond: ((package_id = pypi_release_snapshots.package_id) AND (observed_at > pypi_release_snapshots.observed_at) AND (observed_at <= w0.evidence_cutoff))
                Heap Fetches: 0
                Buffers: shared hit=267094
Planning:
  Buffers: shared hit=26
Planning Time: 0.226 ms
JIT:
  Functions: 46
  Options: Inlining false, Optimization false, Expressions true, Deforming true
  Timing: Generation 0.627 ms (Deform 0.224 ms), Inlining 0.000 ms, Optimization 0.317 ms, Emission 5.688 ms, Total 6.633 ms
Execution Time: 110.653 ms

--- purge.removable_evidence first batch: pypi_release_snapshots
SQL: SELECT "pypi_release_snapshots"."id" AS "pk" FROM "pypi_release_snapshots" WHERE ("pypi_release_snapshots"."observed_at" < '2026-06-16 12:00:00+00:00'::timestamptz AND EXISTS(SELECT 1 AS "a" FROM "pypi_release_snapshots" U0 WHERE (U0."package_id" = ("pypi_release_snapshots"."package_id") AND U0."observed_at" > ("pypi_release_snapshots"."observed_at")) LIMIT 1) AND NOT EXISTS(SELECT 1 AS "a" FROM "policy_runs" W0 WHERE (NOT (W0."id" IN (SELECT V0."id" AS "pk" FROM "policy_runs" V0 WHERE ((V0."finished_at" < '2026-06-16 12:00:00+00:00'::timestamptz OR (V0."finished_at" IS NULL AND V0."started_at" < '2026-06-16 12:00:00+00:00'::timestamptz)) AND NOT EXISTS(SELECT 1 AS "a" FROM "package_health" U0 WHERE U0."policy_run_id" = (V0."id") LIMIT 1)))) AND W0."evidence_cutoff" >= ("pypi_release_snapshots"."observed_at") AND NOT EXISTS(SELECT 1 AS "a" FROM "pypi_release_snapshots" U0 WHERE (U0."package_id" = ("pypi_release_snapshots"."package_id") AND U0."observed_at" > ("pypi_release_snapshots"."observed_at") AND U0."observed_at" <= (W0."evidence_cutoff")) LIMIT 1)) LIMIT 1) AND NOT EXISTS(SELECT 1 AS "a" FROM "package_currency" X0 WHERE (X0."pypi_snapshot_id" = ("pypi_release_snapshots"."id") AND X0."policy_run_id" IN (SELECT W0."id" AS "pk" FROM "policy_runs" W0 WHERE NOT (W0."id" IN (SELECT V0."id" AS "pk" FROM "policy_runs" V0 WHERE ((V0."finished_at" < '2026-06-16 12:00:00+00:00'::timestamptz OR (V0."finished_at" IS NULL AND V0."started_at" < '2026-06-16 12:00:00+00:00'::timestamptz)) AND NOT EXISTS(SELECT 1 AS "a" FROM "package_health" U0 WHERE U0."policy_run_id" = (V0."id") LIMIT 1)))))) LIMIT 1) AND NOT EXISTS(SELECT 1 AS "a" FROM "package_remediation" X0 WHERE (X0."pypi_snapshot_id" = ("pypi_release_snapshots"."id") AND X0."policy_run_id" IN (SELECT W0."id" AS "pk" FROM "policy_runs" W0 WHERE NOT (W0."id" IN (SELECT V0."id" AS "pk" FROM "policy_runs" V0 WHERE ((V0."finished_at" < '2026-06-16 12:00:00+00:00'::timestamptz OR (V0."finished_at" IS NULL AND V0."started_at" < '2026-06-16 12:00:00+00:00'::timestamptz)) AND NOT EXISTS(SELECT 1 AS "a" FROM "package_health" U0 WHERE U0."policy_run_id" = (V0."id") LIMIT 1)))))) LIMIT 1) AND "pypi_release_snapshots"."id" > 0) ORDER BY 1 ASC LIMIT 1000
Limit  (cost=55434.01..260206.98 rows=1000 width=8) (actual time=190.068..296.267 rows=1000 loops=1)
  Buffers: shared hit=270907 read=29551
  ->  Nested Loop Anti Join  (cost=55434.01..334539.57 rows=1363 width=8) (actual time=172.875..279.033 rows=1000 loops=1)
        Join Filter: ((w0.evidence_cutoff >= pypi_release_snapshots.observed_at) AND (NOT EXISTS(SubPlan 2)))
        Rows Removed by Join Filter: 89000
        Buffers: shared hit=270907 read=29551
        ->  Nested Loop Semi Join  (cost=55214.25..58225.58 rows=1636 width=24) (actual time=172.441..176.391 rows=1000 loops=1)
              Buffers: shared hit=3806 read=29551
              ->  Gather Merge  (cost=55213.82..55785.56 rows=4909 width=24) (actual time=172.409..174.377 rows=1010 loops=1)
                    Workers Planned: 2
                    Workers Launched: 2
                    Buffers: shared hit=775 read=29551
                    ->  Sort  (cost=54213.80..54218.91 rows=2045 width=24) (actual time=163.826..163.883 rows=683 loops=3)
                          Sort Key: pypi_release_snapshots.id
                          Sort Method: quicksort  Memory: 25kB
                          Buffers: shared hit=775 read=29551
                          Worker 0:  Sort Method: quicksort  Memory: 25kB
                          Worker 1:  Sort Method: quicksort  Memory: 775kB
                          ->  Parallel Hash Right Anti Join  (cost=34000.16..54101.35 rows=2045 width=24) (actual time=163.238..163.404 rows=3333 loops=3)
                                Hash Cond: (x0.pypi_snapshot_id = pypi_release_snapshots.id)
                                Buffers: shared hit=765 read=29551
                                ->  Hash Join  (cost=223.45..19613.19 rows=187500 width=8) (actual time=0.937..57.124 rows=296667 loops=3)
                                      Hash Cond: (x0.policy_run_id = w0_1.id)
                                      Buffers: shared hit=309 read=14321
                                      ->  Parallel Seq Scan on package_currency x0  (cost=0.00..18359.00 rows=375000 width=16) (actual time=0.015..15.727 rows=300000 loops=3)
                                            Buffers: shared hit=288 read=14321
                                      ->  Hash  (cost=222.89..222.89 rows=45 width=8) (actual time=0.256..0.258 rows=89 loops=3)
                                            Buckets: 1024  Batches: 1  Memory Usage: 12kB
                                            Buffers: shared hit=21
                                            ->  Seq Scan on policy_runs w0_1  (cost=219.77..222.89 rows=45 width=8) (actual time=0.242..0.250 rows=89 loops=3)
                                                  Filter: (NOT (ANY (id = (hashed SubPlan 3).col1)))
                                                  Rows Removed by Filter: 1
                                                  Buffers: shared hit=21
                                                  SubPlan 3
                                                    ->  Merge Anti Join  (cost=3.64..219.76 rows=1 width=8) (actual time=0.101..0.102 rows=1 loops=3)
                                                          Merge Cond: (v0_1.id = u0_3.policy_run_id)
                                                          Buffers: shared hit=15
                                                          ->  Sort  (cost=3.36..3.36 rows=1 width=8) (actual time=0.090..0.090 rows=1 loops=3)
                                                                Sort Key: v0_1.id
                                                                Sort Method: quicksort  Memory: 25kB
                                                                Buffers: shared hit=6
                                                                Worker 0:  Sort Method: quicksort  Memory: 25kB
                                                                Worker 1:  Sort Method: quicksort  Memory: 25kB
                                                                ->  Seq Scan on policy_runs v0_1  (cost=0.00..3.35 rows=1 width=8) (actual time=0.084..0.087 rows=1 loops=3)
                                                                      Filter: ((finished_at < '2026-06-16 12:00:00+00'::timestamp with time zone) OR ((finished_at IS NULL) AND (started_at < '2026-06-16 12:00:00+00'::timestamp with time zone)))
                                                                      Rows Removed by Filter: 89
                                                                      Buffers: shared hit=6
                                                          ->  Index Only Scan using package_health_policy_run_id_d0e7bf06 on package_health u0_3  (cost=0.29..190.28 rows=10000 width=8) (actual time=0.007..0.007 rows=1 loops=3)
                                                                Heap Fetches: 0
                                                                Buffers: shared hit=9
                                ->  Parallel Hash  (cost=33726.14..33726.14 rows=4045 width=24) (actual time=89.743..89.747 rows=3333 loops=3)
                                      Buckets: 16384  Batches: 1  Memory Usage: 704kB
                                      Buffers: shared hit=456 read=15230
                                      ->  Parallel Hash Right Anti Join  (cost=12935.22..33726.14 rows=4045 width=24) (actual time=88.386..88.593 rows=3333 loops=3)
                                            Hash Cond: (x0_1.pypi_snapshot_id = pypi_release_snapshots.id)
                                            Buffers: shared hit=456 read=15230
                                            ->  Hash Join  (cost=223.45..20522.19 rows=187500 width=8) (actual time=16.860..79.545 rows=296667 loops=3)
                                                  Hash Cond: (x0_1.policy_run_id = w0_2.id)
                                                  Buffers: shared hit=311 read=15230
                                                  ->  Parallel Seq Scan on package_remediation x0_1  (cost=0.00..19268.00 rows=375000 width=16) (actual time=0.020..21.139 rows=300000 loops=3)
                                                        Buffers: shared hit=288 read=15230
                                                  ->  Hash  (cost=222.89..222.89 rows=45 width=8) (actual time=16.281..16.283 rows=89 loops=3)
                                                        Buckets: 1024  Batches: 1  Memory Usage: 12kB
                                                        Buffers: shared hit=23
                                                        ->  Seq Scan on policy_runs w0_2  (cost=219.77..222.89 rows=45 width=8) (actual time=16.263..16.272 rows=89 loops=3)
                                                              Filter: (NOT (ANY (id = (hashed SubPlan 4).col1)))
                                                              Rows Removed by Filter: 1
                                                              Buffers: shared hit=23
                                                              SubPlan 4
                                                                ->  Merge Anti Join  (cost=3.64..219.76 rows=1 width=8) (actual time=0.186..0.187 rows=1 loops=3)
                                                                      Merge Cond: (v0_2.id = u0_4.policy_run_id)
                                                                      Buffers: shared hit=17
                                                                      ->  Sort  (cost=3.36..3.36 rows=1 width=8) (actual time=0.134..0.134 rows=1 loops=3)
                                                                            Sort Key: v0_2.id
                                                                            Sort Method: quicksort  Memory: 25kB
                                                                            Buffers: shared hit=6
                                                                            Worker 0:  Sort Method: quicksort  Memory: 25kB
                                                                            Worker 1:  Sort Method: quicksort  Memory: 25kB
                                                                            ->  Seq Scan on policy_runs v0_2  (cost=0.00..3.35 rows=1 width=8) (actual time=0.126..0.130 rows=1 loops=3)
                                                                                  Filter: ((finished_at < '2026-06-16 12:00:00+00'::timestamp with time zone) OR ((finished_at IS NULL) AND (started_at < '2026-06-16 12:00:00+00'::timestamp with time zone)))
                                                                                  Rows Removed by Filter: 89
                                                                                  Buffers: shared hit=6
                                                                      ->  Index Only Scan using package_health_policy_run_id_d0e7bf06 on package_health u0_4  (cost=0.29..190.28 rows=10000 width=8) (actual time=0.046..0.046 rows=1 loops=3)
                                                                            Heap Fetches: 0
                                                                            Buffers: shared hit=11
                                            ->  Parallel Hash  (cost=12661.21..12661.21 rows=4045 width=24) (actual time=0.757..0.758 rows=3333 loops=3)
                                                  Buckets: 16384  Batches: 1  Memory Usage: 704kB
                                                  Buffers: shared hit=145
                                                  ->  Parallel Bitmap Heap Scan on pypi_release_snapshots  (cost=119.66..12661.21 rows=4045 width=24) (actual time=0.155..1.200 rows=10000 loops=1)
                                                        Recheck Cond: (observed_at < '2026-06-16 12:00:00+00'::timestamp with time zone)
                                                        Filter: (id > 0)
                                                        Heap Blocks: exact=134
                                                        Buffers: shared hit=145
                                                        ->  Bitmap Index Scan on pypi_release_observed  (cost=0.00..117.23 rows=9708 width=0) (actual time=0.125..0.125 rows=10000 loops=1)
                                                              Index Cond: (observed_at < '2026-06-16 12:00:00+00'::timestamp with time zone)
                                                              Buffers: shared hit=11
              ->  Index Only Scan using pypi_release_pkg_observed on pypi_release_snapshots u0  (cost=0.42..2.80 rows=30 width=16) (actual time=0.002..0.002 rows=1 loops=1010)
                    Index Cond: ((package_id = pypi_release_snapshots.package_id) AND (observed_at > pypi_release_snapshots.observed_at))
                    Heap Fetches: 0
                    Buffers: shared hit=3031
        ->  Materialize  (cost=219.77..223.12 rows=45 width=8) (actual time=0.000..0.003 rows=89 loops=1000)
              Buffers: shared hit=7
              ->  Seq Scan on policy_runs w0  (cost=219.77..222.89 rows=45 width=8) (actual time=0.286..0.296 rows=89 loops=1)
                    Filter: (NOT (ANY (id = (hashed SubPlan 1).col1)))
                    Rows Removed by Filter: 1
                    Buffers: shared hit=7
                    SubPlan 1
                      ->  Merge Anti Join  (cost=3.64..219.76 rows=1 width=8) (actual time=0.112..0.113 rows=1 loops=1)
                            Merge Cond: (v0.id = u0_1.policy_run_id)
                            Buffers: shared hit=5
                            ->  Sort  (cost=3.36..3.36 rows=1 width=8) (actual time=0.103..0.104 rows=1 loops=1)
                                  Sort Key: v0.id
                                  Sort Method: quicksort  Memory: 25kB
                                  Buffers: shared hit=2
                                  ->  Seq Scan on policy_runs v0  (cost=0.00..3.35 rows=1 width=8) (actual time=0.096..0.100 rows=1 loops=1)
                                        Filter: ((finished_at < '2026-06-16 12:00:00+00'::timestamp with time zone) OR ((finished_at IS NULL) AND (started_at < '2026-06-16 12:00:00+00'::timestamp with time zone)))
                                        Rows Removed by Filter: 89
                                        Buffers: shared hit=2
                            ->  Index Only Scan using package_health_policy_run_id_d0e7bf06 on package_health u0_1  (cost=0.29..190.28 rows=10000 width=8) (actual time=0.005..0.005 rows=1 loops=1)
                                  Heap Fetches: 0
                                  Buffers: shared hit=3
        SubPlan 2
          ->  Index Only Scan using pypi_release_pkg_observed on pypi_release_snapshots u0_2  (cost=0.42..4.45 rows=1 width=0) (actual time=0.001..0.001 rows=1 loops=89000)
                Index Cond: ((package_id = pypi_release_snapshots.package_id) AND (observed_at > pypi_release_snapshots.observed_at) AND (observed_at <= w0.evidence_cutoff))
                Heap Fetches: 0
                Buffers: shared hit=267094
Planning:
  Buffers: shared hit=62
Planning Time: 0.495 ms
JIT:
  Functions: 305
  Options: Inlining false, Optimization false, Expressions true, Deforming true
  Timing: Generation 5.543 ms (Deform 2.758 ms), Inlining 0.000 ms, Optimization 3.088 ms, Emission 62.675 ms, Total 71.306 ms
Execution Time: 297.728 ms

--- purge._older: feedstock_snapshots
SQL: SELECT COUNT(*) AS "__count" FROM "feedstock_snapshots" WHERE "feedstock_snapshots"."observed_at" < '2026-06-16 12:00:00+00:00'::timestamptz
Aggregate  (cost=254.91..254.92 rows=1 width=8) (actual time=0.596..0.596 rows=1 loops=1)
  Buffers: shared hit=12
  ->  Index Only Scan using feedstock_observed on feedstock_snapshots  (cost=0.42..229.10 rows=10324 width=0) (actual time=0.003..0.334 rows=10000 loops=1)
        Index Cond: (observed_at < '2026-06-16 12:00:00+00'::timestamp with time zone)
        Heap Fetches: 0
        Buffers: shared hit=12
Planning Time: 0.032 ms
Execution Time: 0.600 ms

--- purge.floor_removable_evidence first batch: feedstock_snapshots
SQL: SELECT "feedstock_snapshots"."id" AS "pk" FROM "feedstock_snapshots" WHERE ("feedstock_snapshots"."observed_at" < '2026-06-16 12:00:00+00:00'::timestamptz AND EXISTS(SELECT 1 AS "a" FROM "feedstock_snapshots" U0 WHERE (U0."package_id" = ("feedstock_snapshots"."package_id") AND U0."observed_at" > ("feedstock_snapshots"."observed_at")) LIMIT 1) AND NOT EXISTS(SELECT 1 AS "a" FROM "policy_runs" W0 WHERE (NOT (W0."id" IN (SELECT V0."id" AS "pk" FROM "policy_runs" V0 WHERE ((V0."finished_at" < '2026-06-16 12:00:00+00:00'::timestamptz OR (V0."finished_at" IS NULL AND V0."started_at" < '2026-06-16 12:00:00+00:00'::timestamptz)) AND NOT EXISTS(SELECT 1 AS "a" FROM "package_health" U0 WHERE U0."policy_run_id" = (V0."id") LIMIT 1)))) AND W0."evidence_cutoff" >= ("feedstock_snapshots"."observed_at") AND NOT EXISTS(SELECT 1 AS "a" FROM "feedstock_snapshots" U0 WHERE (U0."package_id" = ("feedstock_snapshots"."package_id") AND U0."observed_at" > ("feedstock_snapshots"."observed_at") AND U0."observed_at" <= (W0."evidence_cutoff")) LIMIT 1)) LIMIT 1) AND "feedstock_snapshots"."id" > 0) ORDER BY 1 ASC LIMIT 1000
Limit  (cost=220.62..222076.91 rows=1000 width=8) (actual time=5.849..110.360 rows=1000 loops=1)
  Buffers: shared hit=270163
  ->  Nested Loop Anti Join  (cost=220.62..636504.47 rows=2868 width=8) (actual time=0.286..104.745 rows=1000 loops=1)
        Join Filter: ((w0.evidence_cutoff >= feedstock_snapshots.observed_at) AND (NOT EXISTS(SubPlan 2)))
        Rows Removed by Join Filter: 89000
        Buffers: shared hit=270163
        ->  Nested Loop Semi Join  (cost=0.85..55540.45 rows=3441 width=24) (actual time=0.018..2.227 rows=1000 loops=1)
              Buffers: shared hit=3062
              ->  Index Scan using feedstock_snapshots_pkey on feedstock_snapshots  (cost=0.42..50440.43 rows=10324 width=24) (actual time=0.012..0.245 rows=1010 loops=1)
                    Index Cond: (id > 0)
                    Filter: (observed_at < '2026-06-16 12:00:00+00'::timestamp with time zone)
                    Buffers: shared hit=31
              ->  Index Only Scan using feedstock_pkg_observed on feedstock_snapshots u0  (cost=0.42..2.69 rows=30 width=16) (actual time=0.002..0.002 rows=1 loops=1010)
                    Index Cond: ((package_id = feedstock_snapshots.package_id) AND (observed_at > feedstock_snapshots.observed_at))
                    Heap Fetches: 0
                    Buffers: shared hit=3031
        ->  Materialize  (cost=219.77..223.12 rows=45 width=8) (actual time=0.000..0.002 rows=89 loops=1000)
              Buffers: shared hit=7
              ->  Seq Scan on policy_runs w0  (cost=219.77..222.89 rows=45 width=8) (actual time=0.143..0.153 rows=89 loops=1)
                    Filter: (NOT (ANY (id = (hashed SubPlan 1).col1)))
                    Rows Removed by Filter: 1
                    Buffers: shared hit=7
                    SubPlan 1
                      ->  Merge Anti Join  (cost=3.64..219.76 rows=1 width=8) (actual time=0.070..0.072 rows=1 loops=1)
                            Merge Cond: (v0.id = u0_1.policy_run_id)
                            Buffers: shared hit=5
                            ->  Sort  (cost=3.36..3.36 rows=1 width=8) (actual time=0.062..0.063 rows=1 loops=1)
                                  Sort Key: v0.id
                                  Sort Method: quicksort  Memory: 25kB
                                  Buffers: shared hit=2
                                  ->  Seq Scan on policy_runs v0  (cost=0.00..3.35 rows=1 width=8) (actual time=0.057..0.060 rows=1 loops=1)
                                        Filter: ((finished_at < '2026-06-16 12:00:00+00'::timestamp with time zone) OR ((finished_at IS NULL) AND (started_at < '2026-06-16 12:00:00+00'::timestamp with time zone)))
                                        Rows Removed by Filter: 89
                                        Buffers: shared hit=2
                            ->  Index Only Scan using package_health_policy_run_id_d0e7bf06 on package_health u0_1  (cost=0.29..190.28 rows=10000 width=8) (actual time=0.004..0.005 rows=1 loops=1)
                                  Heap Fetches: 0
                                  Buffers: shared hit=3
        SubPlan 2
          ->  Index Only Scan using feedstock_pkg_observed on feedstock_snapshots u0_2  (cost=0.42..4.45 rows=1 width=0) (actual time=0.001..0.001 rows=1 loops=89000)
                Index Cond: ((package_id = feedstock_snapshots.package_id) AND (observed_at > feedstock_snapshots.observed_at) AND (observed_at <= w0.evidence_cutoff))
                Heap Fetches: 0
                Buffers: shared hit=267094
Planning:
  Buffers: shared hit=26
Planning Time: 0.195 ms
JIT:
  Functions: 47
  Options: Inlining false, Optimization false, Expressions true, Deforming true
  Timing: Generation 0.580 ms (Deform 0.214 ms), Inlining 0.000 ms, Optimization 0.297 ms, Emission 5.335 ms, Total 6.211 ms
Execution Time: 111.201 ms

--- purge.removable_evidence first batch: feedstock_snapshots
SQL: SELECT "feedstock_snapshots"."id" AS "pk" FROM "feedstock_snapshots" WHERE ("feedstock_snapshots"."observed_at" < '2026-06-16 12:00:00+00:00'::timestamptz AND EXISTS(SELECT 1 AS "a" FROM "feedstock_snapshots" U0 WHERE (U0."package_id" = ("feedstock_snapshots"."package_id") AND U0."observed_at" > ("feedstock_snapshots"."observed_at")) LIMIT 1) AND NOT EXISTS(SELECT 1 AS "a" FROM "policy_runs" W0 WHERE (NOT (W0."id" IN (SELECT V0."id" AS "pk" FROM "policy_runs" V0 WHERE ((V0."finished_at" < '2026-06-16 12:00:00+00:00'::timestamptz OR (V0."finished_at" IS NULL AND V0."started_at" < '2026-06-16 12:00:00+00:00'::timestamptz)) AND NOT EXISTS(SELECT 1 AS "a" FROM "package_health" U0 WHERE U0."policy_run_id" = (V0."id") LIMIT 1)))) AND W0."evidence_cutoff" >= ("feedstock_snapshots"."observed_at") AND NOT EXISTS(SELECT 1 AS "a" FROM "feedstock_snapshots" U0 WHERE (U0."package_id" = ("feedstock_snapshots"."package_id") AND U0."observed_at" > ("feedstock_snapshots"."observed_at") AND U0."observed_at" <= (W0."evidence_cutoff")) LIMIT 1)) LIMIT 1) AND NOT EXISTS(SELECT 1 AS "a" FROM "package_currency" X0 WHERE (X0."feedstock_snapshot_id" = ("feedstock_snapshots"."id") AND X0."policy_run_id" IN (SELECT W0."id" AS "pk" FROM "policy_runs" W0 WHERE NOT (W0."id" IN (SELECT V0."id" AS "pk" FROM "policy_runs" V0 WHERE ((V0."finished_at" < '2026-06-16 12:00:00+00:00'::timestamptz OR (V0."finished_at" IS NULL AND V0."started_at" < '2026-06-16 12:00:00+00:00'::timestamptz)) AND NOT EXISTS(SELECT 1 AS "a" FROM "package_health" U0 WHERE U0."policy_run_id" = (V0."id") LIMIT 1)))))) LIMIT 1) AND NOT EXISTS(SELECT 1 AS "a" FROM "package_feedstock_presence" X0 WHERE (X0."feedstock_snapshot_id" = ("feedstock_snapshots"."id") AND X0."policy_run_id" IN (SELECT W0."id" AS "pk" FROM "policy_runs" W0 WHERE NOT (W0."id" IN (SELECT V0."id" AS "pk" FROM "policy_runs" V0 WHERE ((V0."finished_at" < '2026-06-16 12:00:00+00:00'::timestamptz OR (V0."finished_at" IS NULL AND V0."started_at" < '2026-06-16 12:00:00+00:00'::timestamptz)) AND NOT EXISTS(SELECT 1 AS "a" FROM "package_health" U0 WHERE U0."policy_run_id" = (V0."id") LIMIT 1)))))) LIMIT 1) AND NOT EXISTS(SELECT 1 AS "a" FROM "package_remediation" X0 WHERE (X0."feedstock_snapshot_id" = ("feedstock_snapshots"."id") AND X0."policy_run_id" IN (SELECT W0."id" AS "pk" FROM "policy_runs" W0 WHERE NOT (W0."id" IN (SELECT V0."id" AS "pk" FROM "policy_runs" V0 WHERE ((V0."finished_at" < '2026-06-16 12:00:00+00:00'::timestamptz OR (V0."finished_at" IS NULL AND V0."started_at" < '2026-06-16 12:00:00+00:00'::timestamptz)) AND NOT EXISTS(SELECT 1 AS "a" FROM "package_health" U0 WHERE U0."policy_run_id" = (V0."id") LIMIT 1)))))) LIMIT 1) AND "feedstock_snapshots"."id" > 0) ORDER BY 1 ASC LIMIT 1000
Limit  (cost=382732.70..382735.20 rows=1000 width=8) (actual time=1745.355..1745.432 rows=1000 loops=1)
  Buffers: shared hit=2681147 read=44781
  ->  Sort  (cost=382732.70..382736.32 rows=1450 width=8) (actual time=1721.943..1721.983 rows=1000 loops=1)
        Sort Key: feedstock_snapshots.id
        Sort Method: top-N heapsort  Memory: 49kB
        Buffers: shared hit=2681147 read=44781
        ->  Hash Right Anti Join  (cost=353977.30..382656.56 rows=1450 width=8) (actual time=1721.035..1721.432 rows=9900 loops=1)
              Hash Cond: (x0_2.feedstock_snapshot_id = feedstock_snapshots.id)
              Buffers: shared hit=2681147 read=44781
              ->  Hash Join  (cost=223.45..27215.20 rows=450000 width=8) (actual time=2.135..162.890 rows=890000 loops=1)
                    Hash Cond: (x0_2.policy_run_id = w0_3.id)
                    Buffers: shared hit=423 read=15102
                    ->  Seq Scan on package_remediation x0_2  (cost=0.00..24518.00 rows=900000 width=16) (actual time=0.037..52.092 rows=900000 loops=1)
                          Buffers: shared hit=416 read=15102
                    ->  Hash  (cost=222.89..222.89 rows=45 width=8) (actual time=0.412..0.414 rows=89 loops=1)
                          Buckets: 1024  Batches: 1  Memory Usage: 12kB
                          Buffers: shared hit=7
                          ->  Seq Scan on policy_runs w0_3  (cost=219.77..222.89 rows=45 width=8) (actual time=0.397..0.404 rows=89 loops=1)
                                Filter: (NOT (ANY (id = (hashed SubPlan 5).col1)))
                                Rows Removed by Filter: 1
                                Buffers: shared hit=7
                                SubPlan 5
                                  ->  Merge Anti Join  (cost=3.64..219.76 rows=1 width=8) (actual time=0.193..0.194 rows=1 loops=1)
                                        Merge Cond: (v0_3.id = u0_5.policy_run_id)
                                        Buffers: shared hit=5
                                        ->  Sort  (cost=3.36..3.36 rows=1 width=8) (actual time=0.164..0.164 rows=1 loops=1)
                                              Sort Key: v0_3.id
                                              Sort Method: quicksort  Memory: 25kB
                                              Buffers: shared hit=2
                                              ->  Seq Scan on policy_runs v0_3  (cost=0.00..3.35 rows=1 width=8) (actual time=0.149..0.152 rows=1 loops=1)
                                                    Filter: ((finished_at < '2026-06-16 12:00:00+00'::timestamp with time zone) OR ((finished_at IS NULL) AND (started_at < '2026-06-16 12:00:00+00'::timestamp with time zone)))
                                                    Rows Removed by Filter: 89
                                                    Buffers: shared hit=2
                                        ->  Index Only Scan using package_health_policy_run_id_d0e7bf06 on package_health u0_5  (cost=0.29..190.28 rows=10000 width=8) (actual time=0.022..0.022 rows=1 loops=1)
                                              Heap Fetches: 0
                                              Buffers: shared hit=3
              ->  Hash  (cost=353735.72..353735.72 rows=1450 width=8) (actual time=1535.938..1535.952 rows=9900 loops=1)
                    Buckets: 16384 (originally 2048)  Batches: 1 (originally 1)  Memory Usage: 515kB
                    Buffers: shared hit=2680724 read=29679
                    ->  Nested Loop Anti Join  (cost=28713.85..353735.72 rows=1450 width=8) (actual time=376.875..1534.773 rows=9900 loops=1)
                          Join Filter: ((w0.evidence_cutoff >= feedstock_snapshots.observed_at) AND (NOT EXISTS(SubPlan 2)))
                          Rows Removed by Join Filter: 881100
                          Buffers: shared hit=2680724 read=29679
                          ->  Nested Loop Semi Join  (cost=28494.08..59802.61 rows=1740 width=24) (actual time=376.436..408.958 rows=9900 loops=1)
                                Buffers: shared hit=36474 read=29679
                                ->  Hash Right Anti Join  (cost=28493.66..57223.95 rows=5220 width=24) (actual time=376.412..377.074 rows=10000 loops=1)
                                      Hash Cond: (x0_1.feedstock_snapshot_id = feedstock_snapshots.id)
                                      Buffers: shared hit=6473 read=29679
                                      ->  Hash Join  (cost=223.45..27215.20 rows=450000 width=8) (actual time=1.739..140.827 rows=890000 loops=1)
                                            Hash Cond: (x0_1.policy_run_id = w0_2.id)
                                            Buffers: shared hit=39 read=15486
                                            ->  Seq Scan on package_feedstock_presence x0_1  (cost=0.00..24518.00 rows=900000 width=16) (actual time=0.012..46.347 rows=900000 loops=1)
                                                  Buffers: shared hit=32 read=15486
                                            ->  Hash  (cost=222.89..222.89 rows=45 width=8) (actual time=0.363..0.364 rows=89 loops=1)
                                                  Buckets: 1024  Batches: 1  Memory Usage: 12kB
                                                  Buffers: shared hit=7
                                                  ->  Seq Scan on policy_runs w0_2  (cost=219.77..222.89 rows=45 width=8) (actual time=0.351..0.357 rows=89 loops=1)
                                                        Filter: (NOT (ANY (id = (hashed SubPlan 4).col1)))
                                                        Rows Removed by Filter: 1
                                                        Buffers: shared hit=7
                                                        SubPlan 4
                                                          ->  Merge Anti Join  (cost=3.64..219.76 rows=1 width=8) (actual time=0.147..0.147 rows=1 loops=1)
                                                                Merge Cond: (v0_2.id = u0_4.policy_run_id)
                                                                Buffers: shared hit=5
                                                                ->  Sort  (cost=3.36..3.36 rows=1 width=8) (actual time=0.132..0.132 rows=1 loops=1)
                                                                      Sort Key: v0_2.id
                                                                      Sort Method: quicksort  Memory: 25kB
                                                                      Buffers: shared hit=2
                                                                      ->  Seq Scan on policy_runs v0_2  (cost=0.00..3.35 rows=1 width=8) (actual time=0.124..0.127 rows=1 loops=1)
                                                                            Filter: ((finished_at < '2026-06-16 12:00:00+00'::timestamp with time zone) OR ((finished_at IS NULL) AND (started_at < '2026-06-16 12:00:00+00'::timestamp with time zone)))
                                                                            Rows Removed by Filter: 89
                                                                            Buffers: shared hit=2
                                                                ->  Index Only Scan using package_health_policy_run_id_d0e7bf06 on package_health u0_4  (cost=0.29..190.28 rows=10000 width=8) (actual time=0.010..0.010 rows=1 loops=1)
                                                                      Heap Fetches: 0
                                                                      Buffers: shared hit=3
                                      ->  Hash  (cost=28141.16..28141.16 rows=10324 width=24) (actual time=193.879..193.882 rows=10000 loops=1)
                                            Buckets: 16384  Batches: 1  Memory Usage: 675kB
                                            Buffers: shared hit=6434 read=14193
                                            ->  Hash Right Anti Join  (cost=877.16..28141.16 rows=10324 width=24) (actual time=192.837..193.354 rows=10000 loops=1)
                                                  Hash Cond: (x0.feedstock_snapshot_id = feedstock_snapshots.id)
                                                  Buffers: shared hit=6434 read=14193
                                                  ->  Hash Join  (cost=223.45..26306.20 rows=450000 width=8) (actual time=1.913..168.404 rows=890000 loops=1)
                                                        Hash Cond: (x0.policy_run_id = w0_1.id)
                                                        Buffers: shared hit=423 read=14193
                                                        ->  Seq Scan on package_currency x0  (cost=0.00..23609.00 rows=900000 width=16) (actual time=0.019..52.007 rows=900000 loops=1)
                                                              Buffers: shared hit=416 read=14193
                                                        ->  Hash  (cost=222.89..222.89 rows=45 width=8) (actual time=0.239..0.241 rows=89 loops=1)
                                                              Buckets: 1024  Batches: 1  Memory Usage: 12kB
                                                              Buffers: shared hit=7
                                                              ->  Seq Scan on policy_runs w0_1  (cost=219.77..222.89 rows=45 width=8) (actual time=0.228..0.235 rows=89 loops=1)
                                                                    Filter: (NOT (ANY (id = (hashed SubPlan 3).col1)))
                                                                    Rows Removed by Filter: 1
                                                                    Buffers: shared hit=7
                                                                    SubPlan 3
                                                                      ->  Merge Anti Join  (cost=3.64..219.76 rows=1 width=8) (actual time=0.112..0.113 rows=1 loops=1)
                                                                            Merge Cond: (v0_1.id = u0_3.policy_run_id)
                                                                            Buffers: shared hit=5
                                                                            ->  Sort  (cost=3.36..3.36 rows=1 width=8) (actual time=0.102..0.102 rows=1 loops=1)
                                                                                  Sort Key: v0_1.id
                                                                                  Sort Method: quicksort  Memory: 25kB
                                                                                  Buffers: shared hit=2
                                                                                  ->  Seq Scan on policy_runs v0_1  (cost=0.00..3.35 rows=1 width=8) (actual time=0.096..0.100 rows=1 loops=1)
                                                                                        Filter: ((finished_at < '2026-06-16 12:00:00+00'::timestamp with time zone) OR ((finished_at IS NULL) AND (started_at < '2026-06-16 12:00:00+00'::timestamp with time zone)))
                                                                                        Rows Removed by Filter: 89
                                                                                        Buffers: shared hit=2
                                                                            ->  Index Only Scan using package_health_policy_run_id_d0e7bf06 on package_health u0_3  (cost=0.29..190.28 rows=10000 width=8) (actual time=0.006..0.006 rows=1 loops=1)
                                                                                  Heap Fetches: 0
                                                                                  Buffers: shared hit=3
                                                  ->  Hash  (cost=524.65..524.65 rows=10324 width=24) (actual time=2.950..2.950 rows=10000 loops=1)
                                                        Buckets: 16384  Batches: 1  Memory Usage: 675kB
                                                        Buffers: shared hit=6011
                                                        ->  Index Scan using feedstock_observed on feedstock_snapshots  (cost=0.42..524.65 rows=10324 width=24) (actual time=0.021..2.381 rows=10000 loops=1)
                                                              Index Cond: (observed_at < '2026-06-16 12:00:00+00'::timestamp with time zone)
                                                              Filter: (id > 0)
                                                              Buffers: shared hit=6011
                                ->  Index Only Scan using feedstock_pkg_observed on feedstock_snapshots u0  (cost=0.42..2.69 rows=30 width=16) (actual time=0.003..0.003 rows=1 loops=10000)
                                      Index Cond: ((package_id = feedstock_snapshots.package_id) AND (observed_at > feedstock_snapshots.observed_at))
                                      Heap Fetches: 0
                                      Buffers: shared hit=30001
                          ->  Materialize  (cost=219.77..223.12 rows=45 width=8) (actual time=0.000..0.002 rows=89 loops=9900)
                                Buffers: shared hit=7
                                ->  Seq Scan on policy_runs w0  (cost=219.77..222.89 rows=45 width=8) (actual time=0.310..0.324 rows=89 loops=1)
                                      Filter: (NOT (ANY (id = (hashed SubPlan 1).col1)))
                                      Rows Removed by Filter: 1
                                      Buffers: shared hit=7
                                      SubPlan 1
                                        ->  Merge Anti Join  (cost=3.64..219.76 rows=1 width=8) (actual time=0.140..0.146 rows=1 loops=1)
                                              Merge Cond: (v0.id = u0_1.policy_run_id)
                                              Buffers: shared hit=5
                                              ->  Sort  (cost=3.36..3.36 rows=1 width=8) (actual time=0.130..0.135 rows=1 loops=1)
                                                    Sort Key: v0.id
                                                    Sort Method: quicksort  Memory: 25kB
                                                    Buffers: shared hit=2
                                                    ->  Seq Scan on policy_runs v0  (cost=0.00..3.35 rows=1 width=8) (actual time=0.121..0.125 rows=1 loops=1)
                                                          Filter: ((finished_at < '2026-06-16 12:00:00+00'::timestamp with time zone) OR ((finished_at IS NULL) AND (started_at < '2026-06-16 12:00:00+00'::timestamp with time zone)))
                                                          Rows Removed by Filter: 89
                                                          Buffers: shared hit=2
                                              ->  Index Only Scan using package_health_policy_run_id_d0e7bf06 on package_health u0_1  (cost=0.29..190.28 rows=10000 width=8) (actual time=0.005..0.006 rows=1 loops=1)
                                                    Heap Fetches: 0
                                                    Buffers: shared hit=3
                          SubPlan 2
                            ->  Index Only Scan using feedstock_pkg_observed on feedstock_snapshots u0_2  (cost=0.42..4.45 rows=1 width=0) (actual time=0.001..0.001 rows=1 loops=881100)
                                  Index Cond: ((package_id = feedstock_snapshots.package_id) AND (observed_at > feedstock_snapshots.observed_at) AND (observed_at <= w0.evidence_cutoff))
                                  Heap Fetches: 0
                                  Buffers: shared hit=2644243
Planning:
  Buffers: shared hit=72
Planning Time: 0.666 ms
JIT:
  Functions: 161
  Options: Inlining false, Optimization false, Expressions true, Deforming true
  Timing: Generation 2.411 ms (Deform 1.083 ms), Inlining 0.000 ms, Optimization 1.343 ms, Emission 22.671 ms, Total 26.425 ms
Execution Time: 1747.635 ms

--- purge._older: conda_package_snapshots
SQL: SELECT COUNT(*) AS "__count" FROM "conda_package_snapshots" WHERE "conda_package_snapshots"."observed_at" < '2026-06-16 12:00:00+00:00'::timestamptz
Aggregate  (cost=483.25..483.26 rows=1 width=8) (actual time=1.172..1.172 rows=1 loops=1)
  Buffers: shared hit=27
  ->  Index Only Scan using conda_pkg_observed on conda_package_snapshots  (cost=0.43..434.89 rows=19341 width=0) (actual time=0.004..0.700 rows=20000 loops=1)
        Index Cond: (observed_at < '2026-06-16 12:00:00+00'::timestamp with time zone)
        Heap Fetches: 0
        Buffers: shared hit=27
Planning Time: 0.025 ms
Execution Time: 1.177 ms

--- purge.floor_removable_evidence first batch: conda_package_snapshots
SQL: SELECT "conda_package_snapshots"."id" AS "pk" FROM "conda_package_snapshots" WHERE ("conda_package_snapshots"."observed_at" < '2026-06-16 12:00:00+00:00'::timestamptz AND EXISTS(SELECT 1 AS "a" FROM "conda_package_snapshots" U0 WHERE (U0."channel" = ("conda_package_snapshots"."channel") AND U0."package_id" = ("conda_package_snapshots"."package_id") AND U0."platform" = ("conda_package_snapshots"."platform") AND U0."observed_at" > ("conda_package_snapshots"."observed_at")) LIMIT 1) AND NOT EXISTS(SELECT 1 AS "a" FROM "policy_runs" W0 WHERE (NOT (W0."id" IN (SELECT V0."id" AS "pk" FROM "policy_runs" V0 WHERE ((V0."finished_at" < '2026-06-16 12:00:00+00:00'::timestamptz OR (V0."finished_at" IS NULL AND V0."started_at" < '2026-06-16 12:00:00+00:00'::timestamptz)) AND NOT EXISTS(SELECT 1 AS "a" FROM "package_health" U0 WHERE U0."policy_run_id" = (V0."id") LIMIT 1)))) AND W0."evidence_cutoff" >= ("conda_package_snapshots"."observed_at") AND NOT EXISTS(SELECT 1 AS "a" FROM "conda_package_snapshots" U0 WHERE (U0."channel" = ("conda_package_snapshots"."channel") AND U0."package_id" = ("conda_package_snapshots"."package_id") AND U0."platform" = ("conda_package_snapshots"."platform") AND U0."observed_at" > ("conda_package_snapshots"."observed_at") AND U0."observed_at" <= (W0."evidence_cutoff")) LIMIT 1)) LIMIT 1) AND "conda_package_snapshots"."id" > 0) ORDER BY 1 ASC LIMIT 1000
Limit  (cost=220.62..220742.58 rows=1000 width=8) (actual time=7.025..278.341 rows=1000 loops=1)
  Buffers: shared hit=270417
  ->  Nested Loop Anti Join  (cost=220.62..1184864.61 rows=5372 width=8) (actual time=0.532..271.799 rows=1000 loops=1)
        Join Filter: ((w0.evidence_cutoff >= conda_package_snapshots.observed_at) AND (NOT EXISTS(SubPlan 2)))
        Rows Removed by Join Filter: 89000
        Buffers: shared hit=270417
        ->  Nested Loop Semi Join  (cost=0.85..94469.83 rows=6447 width=41) (actual time=0.026..5.328 rows=1000 loops=1)
              Buffers: shared hit=3052
              ->  Index Scan using conda_package_snapshots_pkey on conda_package_snapshots  (cost=0.43..84322.43 rows=19341 width=41) (actual time=0.012..0.160 rows=1010 loops=1)
                    Index Cond: (id > 0)
                    Filter: (observed_at < '2026-06-16 12:00:00+00'::timestamp with time zone)
                    Buffers: shared hit=21
              ->  Index Only Scan using conda_pkg_pair_observed on conda_package_snapshots u0  (cost=0.43..3.65 rows=30 width=33) (actual time=0.005..0.005 rows=1 loops=1010)
                    Index Cond: ((package_id = conda_package_snapshots.package_id) AND (channel = (conda_package_snapshots.channel)::text) AND (platform = (conda_package_snapshots.platform)::text) AND (observed_at > conda_package_snapshots.observed_at))
                    Heap Fetches: 0
                    Buffers: shared hit=3031
        ->  Materialize  (cost=219.77..223.12 rows=45 width=8) (actual time=0.000..0.002 rows=89 loops=1000)
              Buffers: shared hit=7
              ->  Seq Scan on policy_runs w0  (cost=219.77..222.89 rows=45 width=8) (actual time=0.171..0.181 rows=89 loops=1)
                    Filter: (NOT (ANY (id = (hashed SubPlan 1).col1)))
                    Rows Removed by Filter: 1
                    Buffers: shared hit=7
                    SubPlan 1
                      ->  Merge Anti Join  (cost=3.64..219.76 rows=1 width=8) (actual time=0.086..0.088 rows=1 loops=1)
                            Merge Cond: (v0.id = u0_1.policy_run_id)
                            Buffers: shared hit=5
                            ->  Sort  (cost=3.36..3.36 rows=1 width=8) (actual time=0.078..0.078 rows=1 loops=1)
                                  Sort Key: v0.id
                                  Sort Method: quicksort  Memory: 25kB
                                  Buffers: shared hit=2
                                  ->  Seq Scan on policy_runs v0  (cost=0.00..3.35 rows=1 width=8) (actual time=0.071..0.074 rows=1 loops=1)
                                        Filter: ((finished_at < '2026-06-16 12:00:00+00'::timestamp with time zone) OR ((finished_at IS NULL) AND (started_at < '2026-06-16 12:00:00+00'::timestamp with time zone)))
                                        Rows Removed by Filter: 89
                                        Buffers: shared hit=2
                            ->  Index Only Scan using package_health_policy_run_id_d0e7bf06 on package_health u0_1  (cost=0.29..190.28 rows=10000 width=8) (actual time=0.005..0.006 rows=1 loops=1)
                                  Heap Fetches: 0
                                  Buffers: shared hit=3
        SubPlan 2
          ->  Index Only Scan using conda_pkg_pair_observed on conda_package_snapshots u0_2  (cost=0.43..4.46 rows=1 width=0) (actual time=0.003..0.003 rows=1 loops=89000)
                Index Cond: ((package_id = conda_package_snapshots.package_id) AND (channel = (conda_package_snapshots.channel)::text) AND (platform = (conda_package_snapshots.platform)::text) AND (observed_at > conda_package_snapshots.observed_at) AND (observed_at <= w0.evidence_cutoff))
                Heap Fetches: 0
                Buffers: shared hit=267358
Planning:
  Buffers: shared hit=26
Planning Time: 0.232 ms
JIT:
  Functions: 52
  Options: Inlining false, Optimization false, Expressions true, Deforming true
  Timing: Generation 0.633 ms (Deform 0.215 ms), Inlining 0.000 ms, Optimization 0.302 ms, Emission 6.273 ms, Total 7.208 ms
Execution Time: 279.064 ms

--- purge.removable_evidence first batch: conda_package_snapshots
SQL: SELECT "conda_package_snapshots"."id" AS "pk" FROM "conda_package_snapshots" WHERE ("conda_package_snapshots"."observed_at" < '2026-06-16 12:00:00+00:00'::timestamptz AND EXISTS(SELECT 1 AS "a" FROM "conda_package_snapshots" U0 WHERE (U0."channel" = ("conda_package_snapshots"."channel") AND U0."package_id" = ("conda_package_snapshots"."package_id") AND U0."platform" = ("conda_package_snapshots"."platform") AND U0."observed_at" > ("conda_package_snapshots"."observed_at")) LIMIT 1) AND NOT EXISTS(SELECT 1 AS "a" FROM "policy_runs" W0 WHERE (NOT (W0."id" IN (SELECT V0."id" AS "pk" FROM "policy_runs" V0 WHERE ((V0."finished_at" < '2026-06-16 12:00:00+00:00'::timestamptz OR (V0."finished_at" IS NULL AND V0."started_at" < '2026-06-16 12:00:00+00:00'::timestamptz)) AND NOT EXISTS(SELECT 1 AS "a" FROM "package_health" U0 WHERE U0."policy_run_id" = (V0."id") LIMIT 1)))) AND W0."evidence_cutoff" >= ("conda_package_snapshots"."observed_at") AND NOT EXISTS(SELECT 1 AS "a" FROM "conda_package_snapshots" U0 WHERE (U0."channel" = ("conda_package_snapshots"."channel") AND U0."package_id" = ("conda_package_snapshots"."package_id") AND U0."platform" = ("conda_package_snapshots"."platform") AND U0."observed_at" > ("conda_package_snapshots"."observed_at") AND U0."observed_at" <= (W0."evidence_cutoff")) LIMIT 1)) LIMIT 1) AND NOT EXISTS(SELECT 1 AS "a" FROM "package_currency" X0 WHERE (X0."conda_package_snapshot_id" = ("conda_package_snapshots"."id") AND X0."policy_run_id" IN (SELECT W0."id" AS "pk" FROM "policy_runs" W0 WHERE NOT (W0."id" IN (SELECT V0."id" AS "pk" FROM "policy_runs" V0 WHERE ((V0."finished_at" < '2026-06-16 12:00:00+00:00'::timestamptz OR (V0."finished_at" IS NULL AND V0."started_at" < '2026-06-16 12:00:00+00:00'::timestamptz)) AND NOT EXISTS(SELECT 1 AS "a" FROM "package_health" U0 WHERE U0."policy_run_id" = (V0."id") LIMIT 1)))))) LIMIT 1) AND NOT EXISTS(SELECT 1 AS "a" FROM "package_remediation" X0 WHERE (X0."conda_package_snapshot_id" = ("conda_package_snapshots"."id") AND X0."policy_run_id" IN (SELECT W0."id" AS "pk" FROM "policy_runs" W0 WHERE NOT (W0."id" IN (SELECT V0."id" AS "pk" FROM "policy_runs" V0 WHERE ((V0."finished_at" < '2026-06-16 12:00:00+00:00'::timestamptz OR (V0."finished_at" IS NULL AND V0."started_at" < '2026-06-16 12:00:00+00:00'::timestamptz)) AND NOT EXISTS(SELECT 1 AS "a" FROM "package_health" U0 WHERE U0."policy_run_id" = (V0."id") LIMIT 1)))))) LIMIT 1) AND "conda_package_snapshots"."id" > 0) ORDER BY 1 ASC LIMIT 1000
Limit  (cost=661.31..265817.08 rows=1000 width=8) (actual time=17.680..295.630 rows=1000 loops=1)
  Buffers: shared hit=270790
  ->  Merge Anti Join  (cost=661.31..1425078.10 rows=5372 width=8) (actual time=4.732..282.635 rows=1000 loops=1)
        Merge Cond: (conda_package_snapshots.id = x0.conda_package_snapshot_id)
        Buffers: shared hit=270790
        ->  Merge Anti Join  (cost=440.96..1306789.36 rows=5372 width=8) (actual time=2.673..280.518 rows=1000 loops=1)
              Merge Cond: (conda_package_snapshots.id = x0_1.conda_package_snapshot_id)
              Buffers: shared hit=270608
              ->  Nested Loop Anti Join  (cost=220.62..1184864.61 rows=5372 width=8) (actual time=0.507..278.289 rows=1000 loops=1)
                    Join Filter: ((w0.evidence_cutoff >= conda_package_snapshots.observed_at) AND (NOT EXISTS(SubPlan 2)))
                    Rows Removed by Join Filter: 89000
                    Buffers: shared hit=270417
                    ->  Nested Loop Semi Join  (cost=0.85..94469.83 rows=6447 width=41) (actual time=0.024..5.410 rows=1000 loops=1)
                          Buffers: shared hit=3052
                          ->  Index Scan using conda_package_snapshots_pkey on conda_package_snapshots  (cost=0.43..84322.43 rows=19341 width=41) (actual time=0.012..0.211 rows=1010 loops=1)
                                Index Cond: (id > 0)
                                Filter: (observed_at < '2026-06-16 12:00:00+00'::timestamp with time zone)
                                Buffers: shared hit=21
                          ->  Index Only Scan using conda_pkg_pair_observed on conda_package_snapshots u0  (cost=0.43..3.65 rows=30 width=33) (actual time=0.005..0.005 rows=1 loops=1010)
                                Index Cond: ((package_id = conda_package_snapshots.package_id) AND (channel = (conda_package_snapshots.channel)::text) AND (platform = (conda_package_snapshots.platform)::text) AND (observed_at > conda_package_snapshots.observed_at))
                                Heap Fetches: 0
                                Buffers: shared hit=3031
                    ->  Materialize  (cost=219.77..223.12 rows=45 width=8) (actual time=0.000..0.002 rows=89 loops=1000)
                          Buffers: shared hit=7
                          ->  Seq Scan on policy_runs w0  (cost=219.77..222.89 rows=45 width=8) (actual time=0.147..0.156 rows=89 loops=1)
                                Filter: (NOT (ANY (id = (hashed SubPlan 1).col1)))
                                Rows Removed by Filter: 1
                                Buffers: shared hit=7
                                SubPlan 1
                                  ->  Merge Anti Join  (cost=3.64..219.76 rows=1 width=8) (actual time=0.071..0.071 rows=1 loops=1)
                                        Merge Cond: (v0.id = u0_1.policy_run_id)
                                        Buffers: shared hit=5
                                        ->  Sort  (cost=3.36..3.36 rows=1 width=8) (actual time=0.064..0.064 rows=1 loops=1)
                                              Sort Key: v0.id
                                              Sort Method: quicksort  Memory: 25kB
                                              Buffers: shared hit=2
                                              ->  Seq Scan on policy_runs v0  (cost=0.00..3.35 rows=1 width=8) (actual time=0.058..0.062 rows=1 loops=1)
                                                    Filter: ((finished_at < '2026-06-16 12:00:00+00'::timestamp with time zone) OR ((finished_at IS NULL) AND (started_at < '2026-06-16 12:00:00+00'::timestamp with time zone)))
                                                    Rows Removed by Filter: 89
                                                    Buffers: shared hit=2
                                        ->  Index Only Scan using package_health_policy_run_id_d0e7bf06 on package_health u0_1  (cost=0.29..190.28 rows=10000 width=8) (actual time=0.004..0.004 rows=1 loops=1)
                                              Heap Fetches: 0
                                              Buffers: shared hit=3
                    SubPlan 2
                      ->  Index Only Scan using conda_pkg_pair_observed on conda_package_snapshots u0_2  (cost=0.43..4.46 rows=1 width=0) (actual time=0.003..0.003 rows=1 loops=89000)
                            Index Cond: ((package_id = conda_package_snapshots.package_id) AND (channel = (conda_package_snapshots.channel)::text) AND (platform = (conda_package_snapshots.platform)::text) AND (observed_at > conda_package_snapshots.observed_at) AND (observed_at <= w0.evidence_cutoff))
                            Heap Fetches: 0
                            Buffers: shared hit=267358
              ->  Nested Loop  (cost=220.34..120786.30 rows=450000 width=8) (actual time=2.162..2.164 rows=1 loops=1)
                    Buffers: shared hit=191
                    ->  Index Scan using package_remediation_conda_package_snapshot_id_24a4efce on package_remediation x0_1  (cost=0.42..78416.43 rows=900000 width=16) (actual time=0.005..0.611 rows=10001 loops=1)
                          Buffers: shared hit=183
                    ->  Memoize  (cost=219.92..219.94 rows=1 width=8) (actual time=0.000..0.000 rows=0 loops=10001)
                          Cache Key: x0_1.policy_run_id
                          Cache Mode: logical
                          Hits: 9999  Misses: 2  Evictions: 0  Overflows: 0  Memory Usage: 1kB
                          Buffers: shared hit=8
                          ->  Index Only Scan using policy_runs_pkey on policy_runs w0_2  (cost=219.91..219.93 rows=1 width=8) (actual time=0.067..0.068 rows=0 loops=2)
                                Index Cond: (id = x0_1.policy_run_id)
                                Filter: (NOT (ANY (id = (hashed SubPlan 4).col1)))
                                Rows Removed by Filter: 0
                                Heap Fetches: 0
                                Buffers: shared hit=8
                                SubPlan 4
                                  ->  Merge Anti Join  (cost=3.64..219.76 rows=1 width=8) (actual time=0.067..0.068 rows=1 loops=1)
                                        Merge Cond: (v0_2.id = u0_4.policy_run_id)
                                        Buffers: shared hit=5
                                        ->  Sort  (cost=3.36..3.36 rows=1 width=8) (actual time=0.061..0.061 rows=1 loops=1)
                                              Sort Key: v0_2.id
                                              Sort Method: quicksort  Memory: 25kB
                                              Buffers: shared hit=2
                                              ->  Seq Scan on policy_runs v0_2  (cost=0.00..3.35 rows=1 width=8) (actual time=0.049..0.051 rows=1 loops=1)
                                                    Filter: ((finished_at < '2026-06-16 12:00:00+00'::timestamp with time zone) OR ((finished_at IS NULL) AND (started_at < '2026-06-16 12:00:00+00'::timestamp with time zone)))
                                                    Rows Removed by Filter: 89
                                                    Buffers: shared hit=2
                                        ->  Index Only Scan using package_health_policy_run_id_d0e7bf06 on package_health u0_4  (cost=0.29..190.28 rows=10000 width=8) (actual time=0.003..0.003 rows=1 loops=1)
                                              Heap Fetches: 0
                                              Buffers: shared hit=3
        ->  Nested Loop  (cost=220.34..117150.30 rows=450000 width=8) (actual time=2.056..2.057 rows=1 loops=1)
              Buffers: shared hit=182
              ->  Index Scan using package_currency_conda_package_snapshot_id_68049453 on package_currency x0  (cost=0.42..74780.43 rows=900000 width=16) (actual time=0.004..0.590 rows=10001 loops=1)
                    Buffers: shared hit=174
              ->  Memoize  (cost=219.92..219.94 rows=1 width=8) (actual time=0.000..0.000 rows=0 loops=10001)
                    Cache Key: x0.policy_run_id
                    Cache Mode: logical
                    Hits: 9999  Misses: 2  Evictions: 0  Overflows: 0  Memory Usage: 1kB
                    Buffers: shared hit=8
                    ->  Index Only Scan using policy_runs_pkey on policy_runs w0_1  (cost=219.91..219.93 rows=1 width=8) (actual time=0.056..0.056 rows=0 loops=2)
                          Index Cond: (id = x0.policy_run_id)
                          Filter: (NOT (ANY (id = (hashed SubPlan 3).col1)))
                          Rows Removed by Filter: 0
                          Heap Fetches: 0
                          Buffers: shared hit=8
                          SubPlan 3
                            ->  Merge Anti Join  (cost=3.64..219.76 rows=1 width=8) (actual time=0.047..0.048 rows=1 loops=1)
                                  Merge Cond: (v0_1.id = u0_3.policy_run_id)
                                  Buffers: shared hit=5
                                  ->  Sort  (cost=3.36..3.36 rows=1 width=8) (actual time=0.042..0.043 rows=1 loops=1)
                                        Sort Key: v0_1.id
                                        Sort Method: quicksort  Memory: 25kB
                                        Buffers: shared hit=2
                                        ->  Seq Scan on policy_runs v0_1  (cost=0.00..3.35 rows=1 width=8) (actual time=0.039..0.041 rows=1 loops=1)
                                              Filter: ((finished_at < '2026-06-16 12:00:00+00'::timestamp with time zone) OR ((finished_at IS NULL) AND (started_at < '2026-06-16 12:00:00+00'::timestamp with time zone)))
                                              Rows Removed by Filter: 89
                                              Buffers: shared hit=2
                                  ->  Index Only Scan using package_health_policy_run_id_d0e7bf06 on package_health u0_3  (cost=0.29..190.28 rows=10000 width=8) (actual time=0.002..0.003 rows=1 loops=1)
                                        Heap Fetches: 0
                                        Buffers: shared hit=3
Planning:
  Buffers: shared hit=46
Planning Time: 0.532 ms
JIT:
  Functions: 110
  Options: Inlining false, Optimization false, Expressions true, Deforming true
  Timing: Generation 1.286 ms (Deform 0.442 ms), Inlining 0.000 ms, Optimization 0.695 ms, Emission 12.427 ms, Total 14.408 ms
Execution Time: 296.950 ms

--- purge._older: kev_findings
SQL: SELECT COUNT(*) AS "__count" FROM "kev_findings" WHERE "kev_findings"."observed_at" < '2026-06-16 12:00:00+00:00'::timestamptz
Aggregate  (cost=481.21..481.22 rows=1 width=8) (actual time=1.098..1.098 rows=1 loops=1)
  Buffers: shared hit=27
  ->  Index Only Scan using kev_finding_observed on kev_findings  (cost=0.43..433.11 rows=19239 width=0) (actual time=0.003..0.638 rows=20000 loops=1)
        Index Cond: (observed_at < '2026-06-16 12:00:00+00'::timestamp with time zone)
        Heap Fetches: 0
        Buffers: shared hit=27
Planning Time: 0.032 ms
Execution Time: 1.103 ms

--- purge.floor_removable_evidence first batch: kev_findings
SQL: SELECT "kev_findings"."id" AS "pk" FROM "kev_findings" LEFT OUTER JOIN "vulnerability_findings" ON ("kev_findings"."vulnerability_finding_id" = "vulnerability_findings"."id") WHERE ("kev_findings"."observed_at" < '2026-06-16 12:00:00+00:00'::timestamptz AND EXISTS(SELECT 1 AS "a" FROM "kev_findings" U0 LEFT OUTER JOIN "vulnerability_findings" U1 ON (U0."vulnerability_finding_id" = U1."id") WHERE (U0."package_id" = ("kev_findings"."package_id") AND COALESCE(U1."advisory_id", '') = (COALESCE("vulnerability_findings"."advisory_id", '')) AND U0."observed_at" > ("kev_findings"."observed_at")) LIMIT 1) AND NOT EXISTS(SELECT 1 AS "a" FROM "policy_runs" W0 WHERE (NOT (W0."id" IN (SELECT V0."id" AS "pk" FROM "policy_runs" V0 WHERE ((V0."finished_at" < '2026-06-16 12:00:00+00:00'::timestamptz OR (V0."finished_at" IS NULL AND V0."started_at" < '2026-06-16 12:00:00+00:00'::timestamptz)) AND NOT EXISTS(SELECT 1 AS "a" FROM "package_health" U0 WHERE U0."policy_run_id" = (V0."id") LIMIT 1)))) AND W0."evidence_cutoff" >= ("kev_findings"."observed_at") AND NOT EXISTS(SELECT 1 AS "a" FROM "kev_findings" U0 LEFT OUTER JOIN "vulnerability_findings" U1 ON (U0."vulnerability_finding_id" = U1."id") WHERE (U0."package_id" = ("kev_findings"."package_id") AND COALESCE(U1."advisory_id", '') = (COALESCE("vulnerability_findings"."advisory_id", '')) AND U0."observed_at" > ("kev_findings"."observed_at") AND U0."observed_at" <= (W0."evidence_cutoff")) LIMIT 1)) LIMIT 1) AND "kev_findings"."id" > 0) ORDER BY 1 ASC LIMIT 1000
Limit  (cost=1221.64..677506.06 rows=1000 width=8) (actual time=242.345..481.915 rows=1000 loops=1)
  Buffers: shared hit=935155
  ->  Nested Loop Anti Join  (cost=1221.64..1808253.61 rows=2672 width=8) (actual time=113.746..353.256 rows=1000 loops=1)
        Join Filter: (NOT EXISTS(SubPlan 2))
        Rows Removed by Join Filter: 89000
        Buffers: shared hit=935155
        ->  Gather Merge  (cost=1001.73..417665.69 rows=3206 width=37) (actual time=107.283..111.772 rows=1000 loops=1)
              Workers Planned: 2
              Workers Launched: 2
              Buffers: shared hit=41225
              ->  Nested Loop Semi Join  (cost=1.71..416295.61 rows=1336 width=37) (actual time=62.745..66.708 rows=975 loops=3)
                    Join Filter: ((COALESCE(vulnerability_findings.advisory_id, ''::character varying))::text = (COALESCE(u1.advisory_id, ''::character varying))::text)
                    Rows Removed by Join Filter: 487
                    Buffers: shared hit=41225
                    ->  Nested Loop Left Join  (cost=0.85..103295.60 rows=8016 width=37) (actual time=62.724..63.518 rows=984 loops=3)
                          Buffers: shared hit=11872
                          ->  Parallel Index Scan using kev_findings_pkey on kev_findings  (cost=0.43..66702.84 rows=8016 width=32) (actual time=62.698..62.807 rows=984 loops=3)
                                Index Cond: (id > 0)
                                Filter: (observed_at < '2026-06-16 12:00:00+00'::timestamp with time zone)
                                Buffers: shared hit=58
                          ->  Index Scan using vulnerability_findings_pkey on vulnerability_findings  (cost=0.43..4.56 rows=1 width=21) (actual time=0.001..0.001 rows=1 loops=2953)
                                Index Cond: (id = kev_findings.vulnerability_finding_id)
                                Buffers: shared hit=11814
                    ->  Nested Loop Left Join  (cost=0.85..40.27 rows=61 width=29) (actual time=0.003..0.003 rows=1 loops=2953)
                          Buffers: shared hit=29353
                          ->  Index Scan using kev_finding_pkg_observed on kev_findings u0  (cost=0.43..8.02 rows=61 width=24) (actual time=0.002..0.002 rows=1 loops=2953)
                                Index Cond: ((package_id = kev_findings.package_id) AND (observed_at > kev_findings.observed_at))
                                Buffers: shared hit=11805
                          ->  Index Scan using vulnerability_findings_pkey on vulnerability_findings u1  (cost=0.43..0.53 rows=1 width=21) (actual time=0.000..0.000 rows=1 loops=4387)
                                Index Cond: (id = u0.vulnerability_finding_id)
                                Buffers: shared hit=17548
        ->  Index Scan using policy_runs_cutoff on policy_runs w0  (cost=219.91..220.51 rows=15 width=8) (actual time=0.007..0.015 rows=89 loops=1000)
              Index Cond: (evidence_cutoff >= kev_findings.observed_at)
              Filter: (NOT (ANY (id = (hashed SubPlan 1).col1)))
              Rows Removed by Filter: 1
              Buffers: shared hit=3005
              SubPlan 1
                ->  Merge Anti Join  (cost=3.64..219.76 rows=1 width=8) (actual time=5.869..5.870 rows=1 loops=1)
                      Merge Cond: (v0.id = u0_1.policy_run_id)
                      Buffers: shared hit=5
                      ->  Sort  (cost=3.36..3.36 rows=1 width=8) (actual time=5.854..5.854 rows=1 loops=1)
                            Sort Key: v0.id
                            Sort Method: quicksort  Memory: 25kB
                            Buffers: shared hit=2
                            ->  Seq Scan on policy_runs v0  (cost=0.00..3.35 rows=1 width=8) (actual time=5.837..5.841 rows=1 loops=1)
                                  Filter: ((finished_at < '2026-06-16 12:00:00+00'::timestamp with time zone) OR ((finished_at IS NULL) AND (started_at < '2026-06-16 12:00:00+00'::timestamp with time zone)))
                                  Rows Removed by Filter: 89
                                  Buffers: shared hit=2
                      ->  Index Only Scan using package_health_policy_run_id_d0e7bf06 on package_health u0_1  (cost=0.29..190.28 rows=10000 width=8) (actual time=0.007..0.007 rows=1 loops=1)
                            Heap Fetches: 0
                            Buffers: shared hit=3
        SubPlan 2
          ->  Nested Loop Left Join  (cost=0.85..16.91 rows=1 width=0) (actual time=0.002..0.002 rows=1 loops=89000)
                Filter: ((COALESCE(u1_1.advisory_id, ''::character varying))::text = (COALESCE(vulnerability_findings.advisory_id, ''::character varying))::text)
                Rows Removed by Filter: 0
                Buffers: shared hit=890925
                ->  Index Scan using kev_finding_pkg_observed on kev_findings u0_2  (cost=0.43..8.45 rows=1 width=8) (actual time=0.001..0.001 rows=2 loops=89000)
                      Index Cond: ((package_id = kev_findings.package_id) AND (observed_at > kev_findings.observed_at) AND (observed_at <= w0.evidence_cutoff))
                      Buffers: shared hit=356925
                ->  Index Scan using vulnerability_findings_pkey on vulnerability_findings u1_1  (cost=0.43..8.45 rows=1 width=21) (actual time=0.001..0.001 rows=1 loops=133500)
                      Index Cond: (id = u0_2.vulnerability_finding_id)
                      Buffers: shared hit=534000
Planning:
  Buffers: shared hit=74
Planning Time: 0.340 ms
JIT:
  Functions: 129
  Options: Inlining true, Optimization true, Expressions true, Deforming true
  Timing: Generation 2.272 ms (Deform 1.052 ms), Inlining 74.192 ms, Optimization 129.629 ms, Emission 118.705 ms, Total 324.799 ms
Execution Time: 482.967 ms

--- purge.removable_evidence first batch: kev_findings
SQL: SELECT "kev_findings"."id" AS "pk" FROM "kev_findings" LEFT OUTER JOIN "vulnerability_findings" ON ("kev_findings"."vulnerability_finding_id" = "vulnerability_findings"."id") WHERE ("kev_findings"."observed_at" < '2026-06-16 12:00:00+00:00'::timestamptz AND EXISTS(SELECT 1 AS "a" FROM "kev_findings" U0 LEFT OUTER JOIN "vulnerability_findings" U1 ON (U0."vulnerability_finding_id" = U1."id") WHERE (U0."package_id" = ("kev_findings"."package_id") AND COALESCE(U1."advisory_id", '') = (COALESCE("vulnerability_findings"."advisory_id", '')) AND U0."observed_at" > ("kev_findings"."observed_at")) LIMIT 1) AND NOT EXISTS(SELECT 1 AS "a" FROM "policy_runs" W0 WHERE (NOT (W0."id" IN (SELECT V0."id" AS "pk" FROM "policy_runs" V0 WHERE ((V0."finished_at" < '2026-06-16 12:00:00+00:00'::timestamptz OR (V0."finished_at" IS NULL AND V0."started_at" < '2026-06-16 12:00:00+00:00'::timestamptz)) AND NOT EXISTS(SELECT 1 AS "a" FROM "package_health" U0 WHERE U0."policy_run_id" = (V0."id") LIMIT 1)))) AND W0."evidence_cutoff" >= ("kev_findings"."observed_at") AND NOT EXISTS(SELECT 1 AS "a" FROM "kev_findings" U0 LEFT OUTER JOIN "vulnerability_findings" U1 ON (U0."vulnerability_finding_id" = U1."id") WHERE (U0."package_id" = ("kev_findings"."package_id") AND COALESCE(U1."advisory_id", '') = (COALESCE("vulnerability_findings"."advisory_id", '')) AND U0."observed_at" > ("kev_findings"."observed_at") AND U0."observed_at" <= (W0."evidence_cutoff")) LIMIT 1)) LIMIT 1) AND NOT EXISTS(SELECT 1 AS "a" FROM "package_vulnerability" X0 WHERE (X0."kev_finding_id" = ("kev_findings"."id") AND X0."policy_run_id" IN (SELECT W0."id" AS "pk" FROM "policy_runs" W0 WHERE NOT (W0."id" IN (SELECT V0."id" AS "pk" FROM "policy_runs" V0 WHERE ((V0."finished_at" < '2026-06-16 12:00:00+00:00'::timestamptz OR (V0."finished_at" IS NULL AND V0."started_at" < '2026-06-16 12:00:00+00:00'::timestamptz)) AND NOT EXISTS(SELECT 1 AS "a" FROM "package_health" U0 WHERE U0."policy_run_id" = (V0."id") LIMIT 1)))))) LIMIT 1) AND "kev_findings"."id" > 0) ORDER BY 1 ASC LIMIT 1000
Limit  (cost=205508.14..725956.85 rows=1000 width=8) (actual time=771.678..1047.591 rows=1000 loops=1)
  Buffers: shared hit=978238 read=67595, temp read=29078 written=29348
  ->  Nested Loop Anti Join  (cost=205508.14..1252650.94 rows=2012 width=8) (actual time=591.952..867.797 rows=1000 loops=1)
        Join Filter: (NOT EXISTS(SubPlan 2))
        Rows Removed by Join Filter: 89000
        Buffers: shared hit=978238 read=67595, temp read=29078 written=29348
        ->  Gather Merge  (cost=205288.24..205569.39 rows=2414 width=37) (actual time=585.585..614.293 rows=1000 loops=1)
              Workers Planned: 2
              Workers Launched: 2
              Buffers: shared hit=84308 read=67595, temp read=29078 written=29348
              ->  Sort  (cost=204288.21..204290.73 rows=1006 width=37) (actual time=561.886..561.978 rows=988 loops=3)
                    Sort Key: kev_findings.id
                    Sort Method: quicksort  Memory: 593kB
                    Buffers: shared hit=84308 read=67595, temp read=29078 written=29348
                    Worker 0:  Sort Method: quicksort  Memory: 500kB
                    Worker 1:  Sort Method: quicksort  Memory: 567kB
                    ->  Parallel Hash Semi Join  (cost=178230.01..204238.04 rows=1006 width=37) (actual time=542.558..561.217 rows=6600 loops=3)
                          Hash Cond: ((kev_findings.package_id = u0.package_id) AND ((COALESCE(vulnerability_findings.advisory_id, ''::character varying))::text = (COALESCE(u1.advisory_id, ''::character varying))::text))
                          Join Filter: (u0.observed_at > kev_findings.observed_at)
                          Rows Removed by Join Filter: 89
                          Buffers: shared hit=84298 read=67595, temp read=29078 written=29348
                          ->  Parallel Hash Right Anti Join  (cost=61070.52..81317.63 rows=6035 width=37) (actual time=81.797..82.152 rows=6667 loops=3)
                                Hash Cond: (x0.kev_finding_id = kev_findings.id)
                                Buffers: shared hit=80415 read=14659
                                ->  Hash Join  (cost=223.45..19759.19 rows=187500 width=8) (actual time=7.172..54.061 rows=296667 loops=3)
                                      Hash Cond: (x0.policy_run_id = w0_1.id)
                                      Buffers: shared hit=119 read=14659
                                      ->  Parallel Seq Scan on package_vulnerability x0  (cost=0.00..18505.00 rows=375000 width=16) (actual time=0.017..20.454 rows=300000 loops=3)
                                            Buffers: shared hit=96 read=14659
                                      ->  Hash  (cost=222.89..222.89 rows=45 width=8) (actual time=6.559..6.563 rows=89 loops=3)
                                            Buckets: 1024  Batches: 1  Memory Usage: 12kB
                                            Buffers: shared hit=23
                                            ->  Seq Scan on policy_runs w0_1  (cost=219.77..222.89 rows=45 width=8) (actual time=6.542..6.551 rows=89 loops=3)
                                                  Filter: (NOT (ANY (id = (hashed SubPlan 3).col1)))
                                                  Rows Removed by Filter: 1
                                                  Buffers: shared hit=23
                                                  SubPlan 3
                                                    ->  Merge Anti Join  (cost=3.64..219.76 rows=1 width=8) (actual time=6.292..6.294 rows=1 loops=3)
                                                          Merge Cond: (v0_1.id = u0_3.policy_run_id)
                                                          Buffers: shared hit=17
                                                          ->  Sort  (cost=3.36..3.36 rows=1 width=8) (actual time=6.255..6.256 rows=1 loops=3)
                                                                Sort Key: v0_1.id
                                                                Sort Method: quicksort  Memory: 25kB
                                                                Buffers: shared hit=6
                                                                Worker 0:  Sort Method: quicksort  Memory: 25kB
                                                                Worker 1:  Sort Method: quicksort  Memory: 25kB
                                                                ->  Seq Scan on policy_runs v0_1  (cost=0.00..3.35 rows=1 width=8) (actual time=6.245..6.249 rows=1 loops=3)
                                                                      Filter: ((finished_at < '2026-06-16 12:00:00+00'::timestamp with time zone) OR ((finished_at IS NULL) AND (started_at < '2026-06-16 12:00:00+00'::timestamp with time zone)))
                                                                      Rows Removed by Filter: 89
                                                                      Buffers: shared hit=6
                                                          ->  Index Only Scan using package_health_policy_run_id_d0e7bf06 on package_health u0_3  (cost=0.29..190.28 rows=10000 width=8) (actual time=0.031..0.031 rows=1 loops=3)
                                                                Heap Fetches: 0
                                                                Buffers: shared hit=11
                                ->  Parallel Hash  (cost=60746.87..60746.87 rows=8016 width=37) (actual time=9.188..9.201 rows=6667 loops=3)
                                      Buckets: 32768  Batches: 1  Memory Usage: 1696kB
                                      Buffers: shared hit=80296
                                      ->  Nested Loop Left Join  (cost=245.96..60746.87 rows=8016 width=37) (actual time=0.394..8.255 rows=6667 loops=3)
                                            Buffers: shared hit=80296
                                            ->  Parallel Bitmap Heap Scan on kev_findings  (cost=245.53..24154.12 rows=8016 width=32) (actual time=0.364..0.949 rows=6667 loops=3)
                                                  Recheck Cond: (observed_at < '2026-06-16 12:00:00+00'::timestamp with time zone)
                                                  Filter: (id > 0)
                                                  Heap Blocks: exact=84
                                                  Buffers: shared hit=294
                                                  ->  Bitmap Index Scan on kev_finding_observed  (cost=0.00..240.72 rows=19239 width=0) (actual time=0.290..0.290 rows=20000 loops=1)
                                                        Index Cond: (observed_at < '2026-06-16 12:00:00+00'::timestamp with time zone)
                                                        Buffers: shared hit=27
                                            ->  Index Scan using vulnerability_findings_pkey on vulnerability_findings  (cost=0.43..4.56 rows=1 width=21) (actual time=0.001..0.001 rows=1 loops=20000)
                                                  Index Cond: (id = kev_findings.vulnerability_finding_id)
                                                  Buffers: shared hit=80002
                          ->  Parallel Hash  (cost=100763.99..100763.99 rows=750833 width=29) (actual time=454.088..454.091 rows=600667 loops=3)
                                Buckets: 131072  Batches: 16  Memory Usage: 8352kB
                                Buffers: shared hit=3855 read=52936, temp read=18818 written=29156
                                ->  Parallel Hash Left Join  (cost=54057.72..100763.99 rows=750833 width=29) (actual time=272.539..382.435 rows=600667 loops=3)
                                      Hash Cond: (u0.vulnerability_finding_id = u1.id)
                                      Buffers: shared hit=3855 read=52936, temp read=18818 written=18968
                                      ->  Parallel Seq Scan on kev_findings u0  (cost=0.00..31535.33 rows=750833 width=24) (actual time=0.012..37.540 rows=600667 loops=3)
                                            Buffers: shared hit=1649 read=22378
                                      ->  Parallel Hash  (cost=40272.32..40272.32 rows=750832 width=21) (actual time=186.881..186.882 rows=600667 loops=3)
                                            Buckets: 131072  Batches: 16  Memory Usage: 7232kB
                                            Buffers: shared hit=2206 read=30558, temp written=9184
                                            ->  Parallel Seq Scan on vulnerability_findings u1  (cost=0.00..40272.32 rows=750832 width=21) (actual time=107.205..139.968 rows=600667 loops=3)
                                                  Buffers: shared hit=2206 read=30558
        ->  Index Scan using policy_runs_cutoff on policy_runs w0  (cost=219.91..220.51 rows=15 width=8) (actual time=0.007..0.016 rows=89 loops=1000)
              Index Cond: (evidence_cutoff >= kev_findings.observed_at)
              Filter: (NOT (ANY (id = (hashed SubPlan 1).col1)))
              Rows Removed by Filter: 1
              Buffers: shared hit=3005
              SubPlan 1
                ->  Merge Anti Join  (cost=3.64..219.76 rows=1 width=8) (actual time=5.710..5.711 rows=1 loops=1)
                      Merge Cond: (v0.id = u0_1.policy_run_id)
                      Buffers: shared hit=5
                      ->  Sort  (cost=3.36..3.36 rows=1 width=8) (actual time=5.697..5.697 rows=1 loops=1)
                            Sort Key: v0.id
                            Sort Method: quicksort  Memory: 25kB
                            Buffers: shared hit=2
                            ->  Seq Scan on policy_runs v0  (cost=0.00..3.35 rows=1 width=8) (actual time=5.690..5.693 rows=1 loops=1)
                                  Filter: ((finished_at < '2026-06-16 12:00:00+00'::timestamp with time zone) OR ((finished_at IS NULL) AND (started_at < '2026-06-16 12:00:00+00'::timestamp with time zone)))
                                  Rows Removed by Filter: 89
                                  Buffers: shared hit=2
                      ->  Index Only Scan using package_health_policy_run_id_d0e7bf06 on package_health u0_1  (cost=0.29..190.28 rows=10000 width=8) (actual time=0.009..0.009 rows=1 loops=1)
                            Heap Fetches: 0
                            Buffers: shared hit=3
        SubPlan 2
          ->  Nested Loop Left Join  (cost=0.85..16.91 rows=1 width=0) (actual time=0.003..0.003 rows=1 loops=89000)
                Filter: ((COALESCE(u1_1.advisory_id, ''::character varying))::text = (COALESCE(vulnerability_findings.advisory_id, ''::character varying))::text)
                Rows Removed by Filter: 0
                Buffers: shared hit=890925
                ->  Index Scan using kev_finding_pkg_observed on kev_findings u0_2  (cost=0.43..8.45 rows=1 width=8) (actual time=0.001..0.001 rows=2 loops=89000)
                      Index Cond: ((package_id = kev_findings.package_id) AND (observed_at > kev_findings.observed_at) AND (observed_at <= w0.evidence_cutoff))
                      Buffers: shared hit=356925
                ->  Index Scan using vulnerability_findings_pkey on vulnerability_findings u1_1  (cost=0.43..8.45 rows=1 width=21) (actual time=0.001..0.001 rows=1 loops=133500)
                      Index Cond: (id = u0_2.vulnerability_finding_id)
                      Buffers: shared hit=534000
Planning:
  Buffers: shared hit=100
Planning Time: 0.937 ms
JIT:
  Functions: 264
  Options: Inlining true, Optimization true, Expressions true, Deforming true
  Timing: Generation 4.963 ms (Deform 2.419 ms), Inlining 84.741 ms, Optimization 248.779 ms, Emission 192.466 ms, Total 530.949 ms
Execution Time: 1049.440 ms

--- purge._older: vulnerability_findings
SQL: SELECT COUNT(*) AS "__count" FROM "vulnerability_findings" WHERE "vulnerability_findings"."observed_at" < '2026-06-16 12:00:00+00:00'::timestamptz
Aggregate  (cost=503.23..503.24 rows=1 width=8) (actual time=1.134..1.134 rows=1 loops=1)
  Buffers: shared hit=27
  ->  Index Only Scan using vuln_finding_observed on vulnerability_findings  (cost=0.43..452.88 rows=20140 width=0) (actual time=0.005..0.649 rows=20000 loops=1)
        Index Cond: (observed_at < '2026-06-16 12:00:00+00'::timestamp with time zone)
        Heap Fetches: 0
        Buffers: shared hit=27
Planning Time: 0.026 ms
Execution Time: 1.139 ms

--- purge.floor_removable_evidence first batch: vulnerability_findings
SQL: SELECT "vulnerability_findings"."id" AS "pk" FROM "vulnerability_findings" WHERE ("vulnerability_findings"."observed_at" < '2026-06-16 12:00:00+00:00'::timestamptz AND EXISTS(SELECT 1 AS "a" FROM "vulnerability_findings" U0 WHERE (U0."advisory_id" = ("vulnerability_findings"."advisory_id") AND U0."package_id" = ("vulnerability_findings"."package_id") AND U0."observed_at" > ("vulnerability_findings"."observed_at")) LIMIT 1) AND NOT EXISTS(SELECT 1 AS "a" FROM "policy_runs" W0 WHERE (NOT (W0."id" IN (SELECT V0."id" AS "pk" FROM "policy_runs" V0 WHERE ((V0."finished_at" < '2026-06-16 12:00:00+00:00'::timestamptz OR (V0."finished_at" IS NULL AND V0."started_at" < '2026-06-16 12:00:00+00:00'::timestamptz)) AND NOT EXISTS(SELECT 1 AS "a" FROM "package_health" U0 WHERE U0."policy_run_id" = (V0."id") LIMIT 1)))) AND W0."evidence_cutoff" >= ("vulnerability_findings"."observed_at") AND NOT EXISTS(SELECT 1 AS "a" FROM "vulnerability_findings" U0 WHERE (U0."advisory_id" = ("vulnerability_findings"."advisory_id") AND U0."package_id" = ("vulnerability_findings"."package_id") AND U0."observed_at" > ("vulnerability_findings"."observed_at") AND U0."observed_at" <= (W0."evidence_cutoff")) LIMIT 1)) LIMIT 1) AND "vulnerability_findings"."id" > 0) ORDER BY 1 ASC LIMIT 1000
Limit  (cost=1220.64..409433.88 rows=1000 width=8) (actual time=22.397..144.042 rows=1000 loops=1)
  Buffers: shared hit=368982 read=16
  ->  Nested Loop Anti Join  (cost=1220.64..2284765.50 rows=5594 width=8) (actual time=14.623..136.228 rows=1000 loops=1)
        Join Filter: ((w0.evidence_cutoff >= vulnerability_findings.observed_at) AND (NOT EXISTS(SubPlan 2)))
        Rows Removed by Join Filter: 89000
        Buffers: shared hit=368982 read=16
        ->  Gather Merge  (cost=1000.88..134480.42 rows=6713 width=37) (actual time=14.137..17.039 rows=1000 loops=1)
              Workers Planned: 2
              Workers Launched: 2
              Buffers: shared hit=11872 read=16
              ->  Nested Loop Semi Join  (cost=0.85..132705.55 rows=2797 width=37) (actual time=3.179..5.790 rows=975 loops=3)
                    Buffers: shared hit=11872 read=16
                    ->  Parallel Index Scan using vulnerability_findings_pkey on vulnerability_findings  (cost=0.43..75439.82 rows=8392 width=37) (actual time=3.159..3.297 rows=984 loops=3)
                          Index Cond: (id > 0)
                          Filter: (observed_at < '2026-06-16 12:00:00+00'::timestamp with time zone)
                          Buffers: shared hit=75
                    ->  Index Scan using vuln_finding_pkg_observed on vulnerability_findings u0  (cost=0.43..9.62 rows=30 width=29) (actual time=0.002..0.002 rows=1 loops=2953)
                          Index Cond: ((package_id = vulnerability_findings.package_id) AND (observed_at > vulnerability_findings.observed_at))
                          Filter: ((vulnerability_findings.advisory_id)::text = (advisory_id)::text)
                          Rows Removed by Filter: 0
                          Buffers: shared hit=11797 read=16
        ->  Materialize  (cost=219.77..223.12 rows=45 width=8) (actual time=0.000..0.003 rows=89 loops=1000)
              Buffers: shared hit=7
              ->  Seq Scan on policy_runs w0  (cost=219.77..222.89 rows=45 width=8) (actual time=0.272..0.282 rows=89 loops=1)
                    Filter: (NOT (ANY (id = (hashed SubPlan 1).col1)))
                    Rows Removed by Filter: 1
                    Buffers: shared hit=7
                    SubPlan 1
                      ->  Merge Anti Join  (cost=3.64..219.76 rows=1 width=8) (actual time=0.104..0.105 rows=1 loops=1)
                            Merge Cond: (v0.id = u0_1.policy_run_id)
                            Buffers: shared hit=5
                            ->  Sort  (cost=3.36..3.36 rows=1 width=8) (actual time=0.092..0.093 rows=1 loops=1)
                                  Sort Key: v0.id
                                  Sort Method: quicksort  Memory: 25kB
                                  Buffers: shared hit=2
                                  ->  Seq Scan on policy_runs v0  (cost=0.00..3.35 rows=1 width=8) (actual time=0.084..0.087 rows=1 loops=1)
                                        Filter: ((finished_at < '2026-06-16 12:00:00+00'::timestamp with time zone) OR ((finished_at IS NULL) AND (started_at < '2026-06-16 12:00:00+00'::timestamp with time zone)))
                                        Rows Removed by Filter: 89
                                        Buffers: shared hit=2
                            ->  Index Only Scan using package_health_policy_run_id_d0e7bf06 on package_health u0_1  (cost=0.29..190.28 rows=10000 width=8) (actual time=0.007..0.007 rows=1 loops=1)
                                  Heap Fetches: 0
                                  Buffers: shared hit=3
        SubPlan 2
          ->  Index Scan using vuln_finding_pkg_observed on vulnerability_findings u0_2  (cost=0.43..8.45 rows=1 width=0) (actual time=0.001..0.001 rows=1 loops=89000)
                Index Cond: ((package_id = vulnerability_findings.package_id) AND (observed_at > vulnerability_findings.observed_at) AND (observed_at <= w0.evidence_cutoff))
                Filter: ((advisory_id)::text = (vulnerability_findings.advisory_id)::text)
                Rows Removed by Filter: 0
                Buffers: shared hit=357103
Planning:
  Buffers: shared hit=26
Planning Time: 0.237 ms
JIT:
  Functions: 95
  Options: Inlining false, Optimization false, Expressions true, Deforming true
  Timing: Generation 1.750 ms (Deform 0.806 ms), Inlining 0.000 ms, Optimization 0.907 ms, Emission 16.412 ms, Total 19.069 ms
Execution Time: 144.972 ms

--- purge.removable_evidence first batch: vulnerability_findings
SQL: SELECT "vulnerability_findings"."id" AS "pk" FROM "vulnerability_findings" WHERE ("vulnerability_findings"."observed_at" < '2026-06-16 12:00:00+00:00'::timestamptz AND EXISTS(SELECT 1 AS "a" FROM "vulnerability_findings" U0 WHERE (U0."advisory_id" = ("vulnerability_findings"."advisory_id") AND U0."package_id" = ("vulnerability_findings"."package_id") AND U0."observed_at" > ("vulnerability_findings"."observed_at")) LIMIT 1) AND NOT EXISTS(SELECT 1 AS "a" FROM "policy_runs" W0 WHERE (NOT (W0."id" IN (SELECT V0."id" AS "pk" FROM "policy_runs" V0 WHERE ((V0."finished_at" < '2026-06-16 12:00:00+00:00'::timestamptz OR (V0."finished_at" IS NULL AND V0."started_at" < '2026-06-16 12:00:00+00:00'::timestamptz)) AND NOT EXISTS(SELECT 1 AS "a" FROM "package_health" U0 WHERE U0."policy_run_id" = (V0."id") LIMIT 1)))) AND W0."evidence_cutoff" >= ("vulnerability_findings"."observed_at") AND NOT EXISTS(SELECT 1 AS "a" FROM "vulnerability_findings" U0 WHERE (U0."advisory_id" = ("vulnerability_findings"."advisory_id") AND U0."package_id" = ("vulnerability_findings"."package_id") AND U0."observed_at" > ("vulnerability_findings"."observed_at") AND U0."observed_at" <= (W0."evidence_cutoff")) LIMIT 1)) LIMIT 1) AND NOT EXISTS(SELECT 1 AS "a" FROM "kev_findings" Z0 WHERE (Z0."vulnerability_finding_id" = ("vulnerability_findings"."id") AND NOT (Z0."id" IN (SELECT Y0."id" AS "pk" FROM "kev_findings" Y0 LEFT OUTER JOIN "vulnerability_findings" Y1 ON (Y0."vulnerability_finding_id" = Y1."id") WHERE (Y0."observed_at" < '2026-06-16 12:00:00+00:00'::timestamptz AND EXISTS(SELECT 1 AS "a" FROM "kev_findings" U0 LEFT OUTER JOIN "vulnerability_findings" U1 ON (U0."vulnerability_finding_id" = U1."id") WHERE (U0."package_id" = (Y0."package_id") AND COALESCE(U1."advisory_id", '') = (COALESCE(Y1."advisory_id", '')) AND U0."observed_at" > (Y0."observed_at")) LIMIT 1) AND NOT EXISTS(SELECT 1 AS "a" FROM "policy_runs" W0 WHERE (NOT (W0."id" IN (SELECT V0."id" AS "pk" FROM "policy_runs" V0 WHERE ((V0."finished_at" < '2026-06-16 12:00:00+00:00'::timestamptz OR (V0."finished_at" IS NULL AND V0."started_at" < '2026-06-16 12:00:00+00:00'::timestamptz)) AND NOT EXISTS(SELECT 1 AS "a" FROM "package_health" U0 WHERE U0."policy_run_id" = (V0."id") LIMIT 1)))) AND W0."evidence_cutoff" >= (Y0."observed_at") AND NOT EXISTS(SELECT 1 AS "a" FROM "kev_findings" U0 LEFT OUTER JOIN "vulnerability_findings" U1 ON (U0."vulnerability_finding_id" = U1."id") WHERE (U0."package_id" = (Y0."package_id") AND COALESCE(U1."advisory_id", '') = (COALESCE(Y1."advisory_id", '')) AND U0."observed_at" > (Y0."observed_at") AND U0."observed_at" <= (W0."evidence_cutoff")) LIMIT 1)) LIMIT 1) AND NOT EXISTS(SELECT 1 AS "a" FROM "package_vulnerability" X0 WHERE (X0."kev_finding_id" = (Y0."id") AND X0."policy_run_id" IN (SELECT W0."id" AS "pk" FROM "policy_runs" W0 WHERE NOT (W0."id" IN (SELECT V0."id" AS "pk" FROM "policy_runs" V0 WHERE ((V0."finished_at" < '2026-06-16 12:00:00+00:00'::timestamptz OR (V0."finished_at" IS NULL AND V0."started_at" < '2026-06-16 12:00:00+00:00'::timestamptz)) AND NOT EXISTS(SELECT 1 AS "a" FROM "package_health" U0 WHERE U0."policy_run_id" = (V0."id") LIMIT 1)))))) LIMIT 1))))) LIMIT 1) AND NOT EXISTS(SELECT 1 AS "a" FROM "package_vulnerability" X0 WHERE (X0."vulnerability_finding_id" = ("vulnerability_findings"."id") AND X0."policy_run_id" IN (SELECT W0."id" AS "pk" FROM "policy_runs" W0 WHERE NOT (W0."id" IN (SELECT V0."id" AS "pk" FROM "policy_runs" V0 WHERE ((V0."finished_at" < '2026-06-16 12:00:00+00:00'::timestamptz OR (V0."finished_at" IS NULL AND V0."started_at" < '2026-06-16 12:00:00+00:00'::timestamptz)) AND NOT EXISTS(SELECT 1 AS "a" FROM "package_health" U0 WHERE U0."policy_run_id" = (V0."id") LIMIT 1)))))) LIMIT 1) AND NOT EXISTS(SELECT 1 AS "a" FROM "package_remediation" X0 WHERE (X0."vulnerability_finding_id" = ("vulnerability_findings"."id") AND X0."policy_run_id" IN (SELECT W0."id" AS "pk" FROM "policy_runs" W0 WHERE NOT (W0."id" IN (SELECT V0."id" AS "pk" FROM "policy_runs" V0 WHERE ((V0."finished_at" < '2026-06-16 12:00:00+00:00'::timestamptz OR (V0."finished_at" IS NULL AND V0."started_at" < '2026-06-16 12:00:00+00:00'::timestamptz)) AND NOT EXISTS(SELECT 1 AS "a" FROM "package_health" U0 WHERE U0."policy_run_id" = (V0."id") LIMIT 1)))))) LIMIT 1) AND "vulnerability_findings"."id" > 0) ORDER BY 1 ASC LIMIT 1000
Limit  (cost=1245460.90..1828275.28 rows=1000 width=8) (actual time=11098.092..11241.472 rows=1000 loops=1)
  Buffers: shared hit=15083741 read=3129288, temp read=29074 written=29340
  ->  Merge Anti Join  (cost=1245460.90..2472867.98 rows=2106 width=8) (actual time=10778.520..10921.846 rows=1000 loops=1)
        Merge Cond: (vulnerability_findings.id = x0_1.vulnerability_finding_id)
        Buffers: shared hit=15083741 read=3129288, temp read=29074 written=29340
        ->  Nested Loop Anti Join  (cost=1245240.56..2350951.41 rows=2106 width=8) (actual time=10771.482..10914.740 rows=1000 loops=1)
              Join Filter: ((w0.evidence_cutoff >= vulnerability_findings.observed_at) AND (NOT EXISTS(SubPlan 2)))
              Rows Removed by Join Filter: 89000
              Buffers: shared hit=15083733 read=3129105, temp read=29074 written=29340
              ->  Nested Loop Semi Join  (cost=1245020.80..1541288.79 rows=2527 width=37) (actual time=10766.032..10792.160 rows=1000 loops=1)
                    Buffers: shared hit=14728121 read=3127607, temp read=29074 written=29340
                    ->  Merge Anti Join  (cost=1245020.37..1489559.99 rows=7581 width=37) (actual time=10766.001..10789.429 rows=1000 loops=1)
                          Merge Cond: (vulnerability_findings.id = x0.vulnerability_finding_id)
                          Buffers: shared hit=14724301 read=3127418, temp read=29074 written=29340
                          ->  Merge Anti Join  (cost=1244800.03..1400304.62 rows=10070 width=37) (actual time=10755.942..10779.299 rows=1000 loops=1)
                                Merge Cond: (vulnerability_findings.id = z0.vulnerability_finding_id)
                                Buffers: shared hit=14724296 read=3127221, temp read=29074 written=29340
                                ->  Gather Merge  (cost=1000.45..78764.50 rows=20140 width=37) (actual time=96.340..99.169 rows=1010 loops=1)
                                      Workers Planned: 2
                                      Workers Launched: 2
                                      Buffers: shared hit=10 read=62
                                      ->  Parallel Index Scan using vulnerability_findings_pkey on vulnerability_findings  (cost=0.43..75439.82 rows=8392 width=37) (actual time=54.006..54.273 rows=976 loops=3)
                                            Index Cond: (id > 0)
                                            Filter: (observed_at < '2026-06-16 12:00:00+00'::timestamp with time zone)
                                            Buffers: shared hit=10 read=62
                                ->  Index Scan using kev_findings_vulnerability_finding_id_e5a7c678 on kev_findings z0  (cost=1243799.57..1319136.57 rows=901000 width=8) (actual time=10659.574..10680.006 rows=11 loops=1)
                                      Filter: (NOT (ANY (id = (hashed SubPlan 6).col1)))
                                      Rows Removed by Filter: 1188
                                      Buffers: shared hit=14724286 read=3127159, temp read=29074 written=29340
                                      SubPlan 6
                                        ->  Nested Loop Anti Join  (cost=142837.55..1243794.12 rows=2012 width=8) (actual time=608.606..10671.619 rows=19800 loops=1)
                                              Join Filter: (NOT EXISTS(SubPlan 4))
                                              Rows Removed by Join Filter: 1762200
                                              Buffers: shared hit=14724282 read=3127141, temp read=29074 written=29340
                                              ->  Gather  (cost=142617.65..196712.56 rows=2414 width=37) (actual time=601.228..625.355 rows=19800 loops=1)
                                                    Workers Planned: 2
                                                    Workers Launched: 2
                                                    Buffers: shared hit=91032 read=60870, temp read=29074 written=29340
                                                    ->  Parallel Hash Semi Join  (cost=141617.65..195471.16 rows=1006 width=37) (actual time=590.364..609.884 rows=6600 loops=3)
                                                          Hash Cond: ((y0.package_id = u0_6.package_id) AND ((COALESCE(y1.advisory_id, ''::character varying))::text = (COALESCE(u1_1.advisory_id, ''::character varying))::text))
                                                          Join Filter: (u0_6.observed_at > y0.observed_at)
                                                          Rows Removed by Join Filter: 85
                                                          Buffers: shared hit=91032 read=60870, temp read=29074 written=29340
                                                          ->  Nested Loop Left Join  (cost=24458.16..72550.75 rows=6035 width=37) (actual time=105.847..111.329 rows=6667 loops=3)
                                                                Buffers: shared hit=79861 read=15222
                                                                ->  Parallel Hash Right Anti Join  (cost=24457.73..45001.19 rows=6035 width=32) (actual time=105.826..106.226 rows=6667 loops=3)
                                                                      Hash Cond: (x0_2.kev_finding_id = y0.id)
                                                                      Buffers: shared hit=213 read=14869
                                                                      ->  Hash Join  (cost=223.45..19759.19 rows=187500 width=8) (actual time=11.620..74.912 rows=296667 loops=3)
                                                                            Hash Cond: (x0_2.policy_run_id = w0_4.id)
                                                                            Buffers: shared hit=196 read=14592
                                                                            ->  Parallel Seq Scan on package_vulnerability x0_2  (cost=0.00..18505.00 rows=375000 width=16) (actual time=0.005..31.712 rows=300000 loops=3)
                                                                                  Buffers: shared hit=164 read=14591
                                                                            ->  Hash  (cost=222.89..222.89 rows=45 width=8) (actual time=11.214..11.218 rows=89 loops=3)
                                                                                  Buckets: 1024  Batches: 1  Memory Usage: 12kB
                                                                                  Buffers: shared hit=32 read=1
                                                                                  ->  Seq Scan on policy_runs w0_4  (cost=219.77..222.89 rows=45 width=8) (actual time=11.194..11.204 rows=89 loops=3)
                                                                                        Filter: (NOT (ANY (id = (hashed SubPlan 5).col1)))
                                                                                        Rows Removed by Filter: 1
                                                                                        Buffers: shared hit=32 read=1
                                                                                        SubPlan 5
                                                                                          ->  Merge Anti Join  (cost=3.64..219.76 rows=1 width=8) (actual time=10.969..10.971 rows=1 loops=3)
                                                                                                Merge Cond: (v0_2.id = u0_5.policy_run_id)
                                                                                                Buffers: shared hit=26 read=1
                                                                                                ->  Sort  (cost=3.36..3.36 rows=1 width=8) (actual time=10.912..10.913 rows=1 loops=3)
                                                                                                      Sort Key: v0_2.id
                                                                                                      Sort Method: quicksort  Memory: 25kB
                                                                                                      Buffers: shared hit=16
                                                                                                      Worker 0:  Sort Method: quicksort  Memory: 25kB
                                                                                                      Worker 1:  Sort Method: quicksort  Memory: 25kB
                                                                                                      ->  Seq Scan on policy_runs v0_2  (cost=0.00..3.35 rows=1 width=8) (actual time=10.889..10.894 rows=1 loops=3)
                                                                                                            Filter: ((finished_at < '2026-06-16 12:00:00+00'::timestamp with time zone) OR ((finished_at IS NULL) AND (started_at < '2026-06-16 12:00:00+00'::timestamp with time zone)))
                                                                                                            Rows Removed by Filter: 89
                                                                                                            Buffers: shared hit=6
                                                                                                ->  Index Only Scan using package_health_policy_run_id_d0e7bf06 on package_health u0_5  (cost=0.29..190.28 rows=10000 width=8) (actual time=0.045..0.045 rows=1 loops=3)
                                                                                                      Heap Fetches: 0
                                                                                                      Buffers: shared hit=10 read=1
                                                                      ->  Parallel Hash  (cost=24134.08..24134.08 rows=8016 width=32) (actual time=3.567..3.568 rows=6667 loops=3)
                                                                            Buckets: 32768  Batches: 1  Memory Usage: 1536kB
                                                                            Buffers: shared hit=17 read=277
                                                                            ->  Parallel Bitmap Heap Scan on kev_findings y0  (cost=245.53..24134.08 rows=8016 width=32) (actual time=2.238..3.006 rows=6667 loops=3)
                                                                                  Recheck Cond: (observed_at < '2026-06-16 12:00:00+00'::timestamp with time zone)
                                                                                  Heap Blocks: exact=95
                                                                                  Buffers: shared hit=17 read=277
                                                                                  ->  Bitmap Index Scan on kev_finding_observed  (cost=0.00..240.72 rows=19239 width=0) (actual time=2.180..2.180 rows=20000 loops=1)
                                                                                        Index Cond: (observed_at < '2026-06-16 12:00:00+00'::timestamp with time zone)
                                                                                        Buffers: shared read=27
                                                                ->  Index Scan using vulnerability_findings_pkey on vulnerability_findings y1  (cost=0.43..4.56 rows=1 width=21) (actual time=0.001..0.001 rows=1 loops=20000)
                                                                      Index Cond: (id = y0.vulnerability_finding_id)
                                                                      Buffers: shared hit=79648 read=353
                                                          ->  Parallel Hash  (cost=100763.99..100763.99 rows=750833 width=29) (actual time=460.882..460.886 rows=600667 loops=3)
                                                                Buckets: 131072  Batches: 16  Memory Usage: 8320kB
                                                                Buffers: shared hit=11144 read=45647, temp read=18814 written=29148
                                                                ->  Parallel Hash Left Join  (cost=54057.72..100763.99 rows=750833 width=29) (actual time=311.319..403.133 rows=600667 loops=3)
                                                                      Hash Cond: (u0_6.vulnerability_finding_id = u1_1.id)
                                                                      Buffers: shared hit=11144 read=45647, temp read=18814 written=18960
                                                                      ->  Parallel Seq Scan on kev_findings u0_6  (cost=0.00..31535.33 rows=750833 width=24) (actual time=0.034..50.255 rows=600667 loops=3)
                                                                            Buffers: shared hit=4067 read=19960
                                                                      ->  Parallel Hash  (cost=40272.32..40272.32 rows=750832 width=21) (actual time=202.631..202.631 rows=600667 loops=3)
                                                                            Buckets: 131072  Batches: 16  Memory Usage: 7232kB
                                                                            Buffers: shared hit=7077 read=25687, temp written=9168
                                                                            ->  Parallel Seq Scan on vulnerability_findings u1_1  (cost=0.00..40272.32 rows=750832 width=21) (actual time=123.639..158.321 rows=600667 loops=3)
                                                                                  Buffers: shared hit=7077 read=25687
                                              ->  Index Scan using policy_runs_cutoff on policy_runs w0_3  (cost=219.91..220.51 rows=15 width=8) (actual time=0.001..0.011 rows=89 loops=19800)
                                                    Index Cond: (evidence_cutoff >= y0.observed_at)
                                                    Filter: (NOT (ANY (id = (hashed SubPlan 3).col1)))
                                                    Rows Removed by Filter: 1
                                                    Buffers: shared hit=59405
                                                    SubPlan 3
                                                      ->  Merge Anti Join  (cost=3.64..219.76 rows=1 width=8) (actual time=5.821..5.825 rows=1 loops=1)
                                                            Merge Cond: (v0_1.id = u0_3.policy_run_id)
                                                            Buffers: shared hit=5
                                                            ->  Sort  (cost=3.36..3.36 rows=1 width=8) (actual time=5.799..5.803 rows=1 loops=1)
                                                                  Sort Key: v0_1.id
                                                                  Sort Method: quicksort  Memory: 25kB
                                                                  Buffers: shared hit=2
                                                                  ->  Seq Scan on policy_runs v0_1  (cost=0.00..3.35 rows=1 width=8) (actual time=5.787..5.792 rows=1 loops=1)
                                                                        Filter: ((finished_at < '2026-06-16 12:00:00+00'::timestamp with time zone) OR ((finished_at IS NULL) AND (started_at < '2026-06-16 12:00:00+00'::timestamp with time zone)))
                                                                        Rows Removed by Filter: 89
                                                                        Buffers: shared hit=2
                                                            ->  Index Only Scan using package_health_policy_run_id_d0e7bf06 on package_health u0_3  (cost=0.29..190.28 rows=10000 width=8) (actual time=0.013..0.013 rows=1 loops=1)
                                                                  Heap Fetches: 0
                                                                  Buffers: shared hit=3
                                              SubPlan 4
                                                ->  Nested Loop Left Join  (cost=0.85..16.91 rows=1 width=0) (actual time=0.005..0.005 rows=1 loops=1762200)
                                                      Filter: ((COALESCE(u1.advisory_id, ''::character varying))::text = (COALESCE(y1.advisory_id, ''::character varying))::text)
                                                      Rows Removed by Filter: 0
                                                      Buffers: shared hit=14573845 read=3066271
                                                      ->  Index Scan using kev_finding_pkg_observed on kev_findings u0_4  (cost=0.43..8.45 rows=1 width=8) (actual time=0.002..0.002 rows=2 loops=1762200)
                                                            Index Cond: ((package_id = y0.package_id) AND (observed_at > y0.observed_at) AND (observed_at <= w0_3.evidence_cutoff))
                                                            Buffers: shared hit=5650786 read=1416130
                                                      ->  Index Scan using vulnerability_findings_pkey on vulnerability_findings u1  (cost=0.43..8.45 rows=1 width=21) (actual time=0.002..0.002 rows=1 loops=2643300)
                                                            Index Cond: (id = u0_4.vulnerability_finding_id)
                                                            Buffers: shared hit=8923059 read=1650141
                          ->  Nested Loop  (cost=220.34..88080.30 rows=450000 width=8) (actual time=10.054..10.057 rows=1 loops=1)
                                Buffers: shared hit=5 read=197
                                ->  Index Scan using package_vulnerability_vulnerability_finding_id_74fed2f3 on package_vulnerability x0  (cost=0.42..45710.43 rows=900000 width=16) (actual time=0.048..1.689 rows=10001 loops=1)
                                      Buffers: shared read=194
                                ->  Memoize  (cost=219.92..219.94 rows=1 width=8) (actual time=0.001..0.001 rows=0 loops=10001)
                                      Cache Key: x0.policy_run_id
                                      Cache Mode: logical
                                      Hits: 9999  Misses: 2  Evictions: 0  Overflows: 0  Memory Usage: 1kB
                                      Buffers: shared hit=5 read=3
                                      ->  Index Only Scan using policy_runs_pkey on policy_runs w0_1  (cost=219.91..219.93 rows=1 width=8) (actual time=3.531..3.532 rows=0 loops=2)
                                            Index Cond: (id = x0.policy_run_id)
                                            Filter: (NOT (ANY (id = (hashed SubPlan 7).col1)))
                                            Rows Removed by Filter: 0
                                            Heap Fetches: 0
                                            Buffers: shared hit=5 read=3
                                            SubPlan 7
                                              ->  Merge Anti Join  (cost=3.64..219.76 rows=1 width=8) (actual time=6.738..6.739 rows=1 loops=1)
                                                    Merge Cond: (v0_3.id = u0_7.policy_run_id)
                                                    Buffers: shared hit=4 read=1
                                                    ->  Sort  (cost=3.36..3.36 rows=1 width=8) (actual time=6.712..6.712 rows=1 loops=1)
                                                          Sort Key: v0_3.id
                                                          Sort Method: quicksort  Memory: 25kB
                                                          Buffers: shared hit=2
                                                          ->  Seq Scan on policy_runs v0_3  (cost=0.00..3.35 rows=1 width=8) (actual time=6.703..6.706 rows=1 loops=1)
                                                                Filter: ((finished_at < '2026-06-16 12:00:00+00'::timestamp with time zone) OR ((finished_at IS NULL) AND (started_at < '2026-06-16 12:00:00+00'::timestamp with time zone)))
                                                                Rows Removed by Filter: 89
                                                                Buffers: shared hit=2
                                                    ->  Index Only Scan using package_health_policy_run_id_d0e7bf06 on package_health u0_7  (cost=0.29..190.28 rows=10000 width=8) (actual time=0.019..0.019 rows=1 loops=1)
                                                          Heap Fetches: 0
                                                          Buffers: shared hit=2 read=1
                    ->  Index Scan using vuln_finding_pkg_observed on vulnerability_findings u0  (cost=0.43..9.62 rows=30 width=29) (actual time=0.003..0.003 rows=1 loops=1000)
                          Index Cond: ((package_id = vulnerability_findings.package_id) AND (observed_at > vulnerability_findings.observed_at))
                          Filter: ((vulnerability_findings.advisory_id)::text = (advisory_id)::text)
                          Rows Removed by Filter: 0
                          Buffers: shared hit=3820 read=189
              ->  Materialize  (cost=219.77..223.12 rows=45 width=8) (actual time=0.005..0.007 rows=89 loops=1000)
                    Buffers: shared hit=7
                    ->  Seq Scan on policy_runs w0  (cost=219.77..222.89 rows=45 width=8) (actual time=5.178..5.188 rows=89 loops=1)
                          Filter: (NOT (ANY (id = (hashed SubPlan 1).col1)))
                          Rows Removed by Filter: 1
                          Buffers: shared hit=7
                          SubPlan 1
                            ->  Merge Anti Join  (cost=3.64..219.76 rows=1 width=8) (actual time=5.072..5.073 rows=1 loops=1)
                                  Merge Cond: (v0.id = u0_1.policy_run_id)
                                  Buffers: shared hit=5
                                  ->  Sort  (cost=3.36..3.36 rows=1 width=8) (actual time=5.058..5.058 rows=1 loops=1)
                                        Sort Key: v0.id
                                        Sort Method: quicksort  Memory: 25kB
                                        Buffers: shared hit=2
                                        ->  Seq Scan on policy_runs v0  (cost=0.00..3.35 rows=1 width=8) (actual time=5.050..5.052 rows=1 loops=1)
                                              Filter: ((finished_at < '2026-06-16 12:00:00+00'::timestamp with time zone) OR ((finished_at IS NULL) AND (started_at < '2026-06-16 12:00:00+00'::timestamp with time zone)))
                                              Rows Removed by Filter: 89
                                              Buffers: shared hit=2
                                  ->  Index Only Scan using package_health_policy_run_id_d0e7bf06 on package_health u0_1  (cost=0.29..190.28 rows=10000 width=8) (actual time=0.008..0.008 rows=1 loops=1)
                                        Heap Fetches: 0
                                        Buffers: shared hit=3
              SubPlan 2
                ->  Index Scan using vuln_finding_pkg_observed on vulnerability_findings u0_2  (cost=0.43..8.45 rows=1 width=0) (actual time=0.001..0.001 rows=1 loops=89000)
                      Index Cond: ((package_id = vulnerability_findings.package_id) AND (observed_at > vulnerability_findings.observed_at) AND (observed_at <= w0.evidence_cutoff))
                      Filter: ((advisory_id)::text = (vulnerability_findings.advisory_id)::text)
                      Rows Removed by Filter: 0
                      Buffers: shared hit=355605 read=1498
        ->  Nested Loop  (cost=220.34..120786.30 rows=450000 width=8) (actual time=7.029..7.031 rows=1 loops=1)
              Buffers: shared hit=8 read=183
              ->  Index Scan using package_remediation_vulnerability_finding_id_75f84ff6 on package_remediation x0_1  (cost=0.42..78416.43 rows=900000 width=16) (actual time=0.016..0.914 rows=10001 loops=1)
                    Buffers: shared read=183
              ->  Memoize  (cost=219.92..219.94 rows=1 width=8) (actual time=0.001..0.001 rows=0 loops=10001)
                    Cache Key: x0_1.policy_run_id
                    Cache Mode: logical
                    Hits: 9999  Misses: 2  Evictions: 0  Overflows: 0  Memory Usage: 1kB
                    Buffers: shared hit=8
                    ->  Index Only Scan using policy_runs_pkey on policy_runs w0_2  (cost=219.91..219.93 rows=1 width=8) (actual time=2.387..2.387 rows=0 loops=2)
                          Index Cond: (id = x0_1.policy_run_id)
                          Filter: (NOT (ANY (id = (hashed SubPlan 8).col1)))
                          Rows Removed by Filter: 0
                          Heap Fetches: 0
                          Buffers: shared hit=8
                          SubPlan 8
                            ->  Merge Anti Join  (cost=3.64..219.76 rows=1 width=8) (actual time=4.687..4.688 rows=1 loops=1)
                                  Merge Cond: (v0_4.id = u0_8.policy_run_id)
                                  Buffers: shared hit=5
                                  ->  Sort  (cost=3.36..3.36 rows=1 width=8) (actual time=4.671..4.671 rows=1 loops=1)
                                        Sort Key: v0_4.id
                                        Sort Method: quicksort  Memory: 25kB
                                        Buffers: shared hit=2
                                        ->  Seq Scan on policy_runs v0_4  (cost=0.00..3.35 rows=1 width=8) (actual time=4.667..4.669 rows=1 loops=1)
                                              Filter: ((finished_at < '2026-06-16 12:00:00+00'::timestamp with time zone) OR ((finished_at IS NULL) AND (started_at < '2026-06-16 12:00:00+00'::timestamp with time zone)))
                                              Rows Removed by Filter: 89
                                              Buffers: shared hit=2
                                  ->  Index Only Scan using package_health_policy_run_id_d0e7bf06 on package_health u0_8  (cost=0.29..190.28 rows=10000 width=8) (actual time=0.007..0.007 rows=1 loops=1)
                                        Heap Fetches: 0
                                        Buffers: shared hit=3
Planning:
  Buffers: shared hit=146 read=28
Planning Time: 1.625 ms
JIT:
  Functions: 545
  Options: Inlining true, Optimization true, Expressions true, Deforming true
  Timing: Generation 10.922 ms (Deform 4.493 ms), Inlining 165.122 ms, Optimization 421.736 ms, Emission 320.761 ms, Total 918.541 ms
Execution Time: 11245.349 ms

--- purge._older: license_findings
SQL: SELECT COUNT(*) AS "__count" FROM "license_findings" WHERE "license_findings"."observed_at" < '2026-06-16 12:00:00+00:00'::timestamptz
Aggregate  (cost=487.75..487.76 rows=1 width=8) (actual time=1.092..1.092 rows=1 loops=1)
  Buffers: shared hit=27
  ->  Index Only Scan using license_finding_observed on license_findings  (cost=0.43..438.83 rows=19566 width=0) (actual time=0.005..0.628 rows=20000 loops=1)
        Index Cond: (observed_at < '2026-06-16 12:00:00+00'::timestamp with time zone)
        Heap Fetches: 0
        Buffers: shared hit=27
Planning Time: 0.027 ms
Execution Time: 1.097 ms

--- purge.floor_removable_evidence first batch: license_findings
SQL: SELECT "license_findings"."id" AS "pk" FROM "license_findings" WHERE ("license_findings"."observed_at" < '2026-06-16 12:00:00+00:00'::timestamptz AND EXISTS(SELECT 1 AS "a" FROM "license_findings" U0 WHERE (U0."channel" = ("license_findings"."channel") AND U0."package_id" = ("license_findings"."package_id") AND U0."observed_at" > ("license_findings"."observed_at")) LIMIT 1) AND NOT EXISTS(SELECT 1 AS "a" FROM "policy_runs" W0 WHERE (NOT (W0."id" IN (SELECT V0."id" AS "pk" FROM "policy_runs" V0 WHERE ((V0."finished_at" < '2026-06-16 12:00:00+00:00'::timestamptz OR (V0."finished_at" IS NULL AND V0."started_at" < '2026-06-16 12:00:00+00:00'::timestamptz)) AND NOT EXISTS(SELECT 1 AS "a" FROM "package_health" U0 WHERE U0."policy_run_id" = (V0."id") LIMIT 1)))) AND W0."evidence_cutoff" >= ("license_findings"."observed_at") AND NOT EXISTS(SELECT 1 AS "a" FROM "license_findings" U0 WHERE (U0."channel" = ("license_findings"."channel") AND U0."package_id" = ("license_findings"."package_id") AND U0."observed_at" > ("license_findings"."observed_at") AND U0."observed_at" <= (W0."evidence_cutoff")) LIMIT 1)) LIMIT 1) AND "license_findings"."id" > 0) ORDER BY 1 ASC LIMIT 1000
Limit  (cost=1220.64..408798.48 rows=1000 width=8) (actual time=23.599..141.114 rows=1000 loops=1)
  Buffers: shared hit=368912
  ->  Nested Loop Anti Join  (cost=1220.64..2216406.18 rows=5435 width=8) (actual time=15.610..133.076 rows=1000 loops=1)
        Join Filter: ((w0.evidence_cutoff >= license_findings.observed_at) AND (NOT EXISTS(SubPlan 2)))
        Rows Removed by Join Filter: 89000
        Buffers: shared hit=368912
        ->  Gather Merge  (cost=1000.88..127236.20 rows=6522 width=34) (actual time=15.102..18.001 rows=1000 loops=1)
              Workers Planned: 2
              Workers Launched: 2
              Buffers: shared hit=11874
              ->  Nested Loop Semi Join  (cost=0.85..125483.37 rows=2718 width=34) (actual time=3.309..5.767 rows=975 loops=3)
                    Buffers: shared hit=11874
                    ->  Parallel Index Scan using license_findings_pkey on license_findings  (cost=0.43..72216.84 rows=8152 width=34) (actual time=3.292..3.405 rows=984 loops=3)
                          Index Cond: (id > 0)
                          Filter: (observed_at < '2026-06-16 12:00:00+00'::timestamp with time zone)
                          Buffers: shared hit=63
                    ->  Index Scan using license_finding_pkg_observed on license_findings u0  (cost=0.43..9.19 rows=30 width=26) (actual time=0.002..0.002 rows=1 loops=2953)
                          Index Cond: ((package_id = license_findings.package_id) AND (observed_at > license_findings.observed_at))
                          Filter: ((license_findings.channel)::text = (channel)::text)
                          Rows Removed by Filter: 0
                          Buffers: shared hit=11811
        ->  Materialize  (cost=219.77..223.12 rows=45 width=8) (actual time=0.000..0.003 rows=89 loops=1000)
              Buffers: shared hit=7
              ->  Seq Scan on policy_runs w0  (cost=219.77..222.89 rows=45 width=8) (actual time=0.300..0.310 rows=89 loops=1)
                    Filter: (NOT (ANY (id = (hashed SubPlan 1).col1)))
                    Rows Removed by Filter: 1
                    Buffers: shared hit=7
                    SubPlan 1
                      ->  Merge Anti Join  (cost=3.64..219.76 rows=1 width=8) (actual time=0.108..0.109 rows=1 loops=1)
                            Merge Cond: (v0.id = u0_1.policy_run_id)
                            Buffers: shared hit=5
                            ->  Sort  (cost=3.36..3.36 rows=1 width=8) (actual time=0.096..0.096 rows=1 loops=1)
                                  Sort Key: v0.id
                                  Sort Method: quicksort  Memory: 25kB
                                  Buffers: shared hit=2
                                  ->  Seq Scan on policy_runs v0  (cost=0.00..3.35 rows=1 width=8) (actual time=0.088..0.091 rows=1 loops=1)
                                        Filter: ((finished_at < '2026-06-16 12:00:00+00'::timestamp with time zone) OR ((finished_at IS NULL) AND (started_at < '2026-06-16 12:00:00+00'::timestamp with time zone)))
                                        Rows Removed by Filter: 89
                                        Buffers: shared hit=2
                            ->  Index Only Scan using package_health_policy_run_id_d0e7bf06 on package_health u0_1  (cost=0.29..190.28 rows=10000 width=8) (actual time=0.008..0.008 rows=1 loops=1)
                                  Heap Fetches: 0
                                  Buffers: shared hit=3
        SubPlan 2
          ->  Index Scan using license_finding_pkg_observed on license_findings u0_2  (cost=0.43..8.45 rows=1 width=0) (actual time=0.001..0.001 rows=1 loops=89000)
                Index Cond: ((package_id = license_findings.package_id) AND (observed_at > license_findings.observed_at) AND (observed_at <= w0.evidence_cutoff))
                Filter: ((channel)::text = (license_findings.channel)::text)
                Rows Removed by Filter: 0
                Buffers: shared hit=357031
Planning:
  Buffers: shared hit=26
Planning Time: 0.251 ms
JIT:
  Functions: 95
  Options: Inlining false, Optimization false, Expressions true, Deforming true
  Timing: Generation 1.986 ms (Deform 0.886 ms), Inlining 0.000 ms, Optimization 0.973 ms, Emission 16.982 ms, Total 19.940 ms
Execution Time: 142.188 ms

--- purge.removable_evidence first batch: license_findings
SQL: SELECT "license_findings"."id" AS "pk" FROM "license_findings" WHERE ("license_findings"."observed_at" < '2026-06-16 12:00:00+00:00'::timestamptz AND EXISTS(SELECT 1 AS "a" FROM "license_findings" U0 WHERE (U0."channel" = ("license_findings"."channel") AND U0."package_id" = ("license_findings"."package_id") AND U0."observed_at" > ("license_findings"."observed_at")) LIMIT 1) AND NOT EXISTS(SELECT 1 AS "a" FROM "policy_runs" W0 WHERE (NOT (W0."id" IN (SELECT V0."id" AS "pk" FROM "policy_runs" V0 WHERE ((V0."finished_at" < '2026-06-16 12:00:00+00:00'::timestamptz OR (V0."finished_at" IS NULL AND V0."started_at" < '2026-06-16 12:00:00+00:00'::timestamptz)) AND NOT EXISTS(SELECT 1 AS "a" FROM "package_health" U0 WHERE U0."policy_run_id" = (V0."id") LIMIT 1)))) AND W0."evidence_cutoff" >= ("license_findings"."observed_at") AND NOT EXISTS(SELECT 1 AS "a" FROM "license_findings" U0 WHERE (U0."channel" = ("license_findings"."channel") AND U0."package_id" = ("license_findings"."package_id") AND U0."observed_at" > ("license_findings"."observed_at") AND U0."observed_at" <= (W0."evidence_cutoff")) LIMIT 1)) LIMIT 1) AND NOT EXISTS(SELECT 1 AS "a" FROM "package_license" X0 WHERE (X0."license_finding_id" = ("license_findings"."id") AND X0."policy_run_id" IN (SELECT W0."id" AS "pk" FROM "policy_runs" W0 WHERE NOT (W0."id" IN (SELECT V0."id" AS "pk" FROM "policy_runs" V0 WHERE ((V0."finished_at" < '2026-06-16 12:00:00+00:00'::timestamptz OR (V0."finished_at" IS NULL AND V0."started_at" < '2026-06-16 12:00:00+00:00'::timestamptz)) AND NOT EXISTS(SELECT 1 AS "a" FROM "package_health" U0 WHERE U0."policy_run_id" = (V0."id") LIMIT 1)))))) LIMIT 1) AND "license_findings"."id" > 0) ORDER BY 1 ASC LIMIT 1000
Limit  (cost=1444.24..437737.03 rows=1000 width=8) (actual time=29.914..150.635 rows=1000 loops=1)
  Buffers: shared hit=369084
  ->  Nested Loop Anti Join  (cost=1444.24..1786318.03 rows=4091 width=8) (actual time=19.293..139.966 rows=1000 loops=1)
        Join Filter: ((w0.evidence_cutoff >= license_findings.observed_at) AND (NOT EXISTS(SubPlan 2)))
        Rows Removed by Join Filter: 89000
        Buffers: shared hit=369084
        ->  Merge Anti Join  (cost=1224.48..213718.94 rows=4909 width=34) (actual time=18.933..21.878 rows=1000 loops=1)
              Merge Cond: (license_findings.id = x0.license_finding_id)
              Buffers: shared hit=12046
              ->  Gather Merge  (cost=1000.88..127236.20 rows=6522 width=34) (actual time=16.623..19.483 rows=1000 loops=1)
                    Workers Planned: 2
                    Workers Launched: 2
                    Buffers: shared hit=11874
                    ->  Nested Loop Semi Join  (cost=0.85..125483.37 rows=2718 width=34) (actual time=3.809..6.307 rows=975 loops=3)
                          Buffers: shared hit=11874
                          ->  Parallel Index Scan using license_findings_pkey on license_findings  (cost=0.43..72216.84 rows=8152 width=34) (actual time=3.790..3.911 rows=984 loops=3)
                                Index Cond: (id > 0)
                                Filter: (observed_at < '2026-06-16 12:00:00+00'::timestamp with time zone)
                                Buffers: shared hit=63
                          ->  Index Scan using license_finding_pkg_observed on license_findings u0  (cost=0.43..9.19 rows=30 width=26) (actual time=0.002..0.002 rows=1 loops=2953)
                                Index Cond: ((package_id = license_findings.package_id) AND (observed_at > license_findings.observed_at))
                                Filter: ((license_findings.channel)::text = (channel)::text)
                                Rows Removed by Filter: 0
                                Buffers: shared hit=11811
              ->  Nested Loop  (cost=220.34..85325.30 rows=450000 width=8) (actual time=2.299..2.301 rows=1 loops=1)
                    Buffers: shared hit=172
                    ->  Index Scan using package_license_license_finding_id_efd89f7c on package_license x0  (cost=0.42..42955.43 rows=900000 width=16) (actual time=0.010..0.697 rows=10001 loops=1)
                          Buffers: shared hit=164
                    ->  Memoize  (cost=219.92..219.94 rows=1 width=8) (actual time=0.000..0.000 rows=0 loops=10001)
                          Cache Key: x0.policy_run_id
                          Cache Mode: logical
                          Hits: 9999  Misses: 2  Evictions: 0  Overflows: 0  Memory Usage: 1kB
                          Buffers: shared hit=8
                          ->  Index Only Scan using policy_runs_pkey on policy_runs w0_1  (cost=219.91..219.93 rows=1 width=8) (actual time=0.135..0.136 rows=0 loops=2)
                                Index Cond: (id = x0.policy_run_id)
                                Filter: (NOT (ANY (id = (hashed SubPlan 3).col1)))
                                Rows Removed by Filter: 0
                                Heap Fetches: 0
                                Buffers: shared hit=8
                                SubPlan 3
                                  ->  Merge Anti Join  (cost=3.64..219.76 rows=1 width=8) (actual time=0.100..0.101 rows=1 loops=1)
                                        Merge Cond: (v0_1.id = u0_3.policy_run_id)
                                        Buffers: shared hit=5
                                        ->  Sort  (cost=3.36..3.36 rows=1 width=8) (actual time=0.090..0.091 rows=1 loops=1)
                                              Sort Key: v0_1.id
                                              Sort Method: quicksort  Memory: 25kB
                                              Buffers: shared hit=2
                                              ->  Seq Scan on policy_runs v0_1  (cost=0.00..3.35 rows=1 width=8) (actual time=0.083..0.086 rows=1 loops=1)
                                                    Filter: ((finished_at < '2026-06-16 12:00:00+00'::timestamp with time zone) OR ((finished_at IS NULL) AND (started_at < '2026-06-16 12:00:00+00'::timestamp with time zone)))
                                                    Rows Removed by Filter: 89
                                                    Buffers: shared hit=2
                                        ->  Index Only Scan using package_health_policy_run_id_d0e7bf06 on package_health u0_3  (cost=0.29..190.28 rows=10000 width=8) (actual time=0.006..0.006 rows=1 loops=1)
                                              Heap Fetches: 0
                                              Buffers: shared hit=3
        ->  Materialize  (cost=219.77..223.12 rows=45 width=8) (actual time=0.000..0.003 rows=89 loops=1000)
              Buffers: shared hit=7
              ->  Seq Scan on policy_runs w0  (cost=219.77..222.89 rows=45 width=8) (actual time=0.165..0.175 rows=89 loops=1)
                    Filter: (NOT (ANY (id = (hashed SubPlan 1).col1)))
                    Rows Removed by Filter: 1
                    Buffers: shared hit=7
                    SubPlan 1
                      ->  Merge Anti Join  (cost=3.64..219.76 rows=1 width=8) (actual time=0.061..0.062 rows=1 loops=1)
                            Merge Cond: (v0.id = u0_1.policy_run_id)
                            Buffers: shared hit=5
                            ->  Sort  (cost=3.36..3.36 rows=1 width=8) (actual time=0.054..0.055 rows=1 loops=1)
                                  Sort Key: v0.id
                                  Sort Method: quicksort  Memory: 25kB
                                  Buffers: shared hit=2
                                  ->  Seq Scan on policy_runs v0  (cost=0.00..3.35 rows=1 width=8) (actual time=0.050..0.053 rows=1 loops=1)
                                        Filter: ((finished_at < '2026-06-16 12:00:00+00'::timestamp with time zone) OR ((finished_at IS NULL) AND (started_at < '2026-06-16 12:00:00+00'::timestamp with time zone)))
                                        Rows Removed by Filter: 89
                                        Buffers: shared hit=2
                            ->  Index Only Scan using package_health_policy_run_id_d0e7bf06 on package_health u0_1  (cost=0.29..190.28 rows=10000 width=8) (actual time=0.003..0.003 rows=1 loops=1)
                                  Heap Fetches: 0
                                  Buffers: shared hit=3
        SubPlan 2
          ->  Index Scan using license_finding_pkg_observed on license_findings u0_2  (cost=0.43..8.45 rows=1 width=0) (actual time=0.001..0.001 rows=1 loops=89000)
                Index Cond: ((package_id = license_findings.package_id) AND (observed_at > license_findings.observed_at) AND (observed_at <= w0.evidence_cutoff))
                Filter: ((channel)::text = (license_findings.channel)::text)
                Rows Removed by Filter: 0
                Buffers: shared hit=357031
Planning:
  Buffers: shared hit=52
Planning Time: 0.416 ms
JIT:
  Functions: 142
  Options: Inlining false, Optimization false, Expressions true, Deforming true
  Timing: Generation 2.335 ms (Deform 1.006 ms), Inlining 0.000 ms, Optimization 1.148 ms, Emission 20.984 ms, Total 24.467 ms
Execution Time: 151.945 ms

--- purge._older: python_readiness_assessments
SQL: SELECT COUNT(*) AS "__count" FROM "python_readiness_assessments" WHERE "python_readiness_assessments"."observed_at" < '2026-06-16 12:00:00+00:00'::timestamptz
Aggregate  (cost=480.05..480.06 rows=1 width=8) (actual time=1.118..1.118 rows=1 loops=1)
  Buffers: shared hit=27
  ->  Index Only Scan using py_readiness_observed on python_readiness_assessments  (cost=0.43..432.10 rows=19181 width=0) (actual time=0.004..0.655 rows=20000 loops=1)
        Index Cond: (observed_at < '2026-06-16 12:00:00+00'::timestamp with time zone)
        Heap Fetches: 0
        Buffers: shared hit=27
Planning Time: 0.015 ms
Execution Time: 1.122 ms

--- purge.floor_removable_evidence first batch: python_readiness_assessments
SQL: SELECT "python_readiness_assessments"."id" AS "pk" FROM "python_readiness_assessments" WHERE ("python_readiness_assessments"."observed_at" < '2026-06-16 12:00:00+00:00'::timestamptz AND EXISTS(SELECT 1 AS "a" FROM "python_readiness_assessments" U0 WHERE (U0."package_id" = ("python_readiness_assessments"."package_id") AND U0."python_series" = ("python_readiness_assessments"."python_series") AND U0."observed_at" > ("python_readiness_assessments"."observed_at")) LIMIT 1) AND NOT EXISTS(SELECT 1 AS "a" FROM "policy_runs" W0 WHERE (NOT (W0."id" IN (SELECT V0."id" AS "pk" FROM "policy_runs" V0 WHERE ((V0."finished_at" < '2026-06-16 12:00:00+00:00'::timestamptz OR (V0."finished_at" IS NULL AND V0."started_at" < '2026-06-16 12:00:00+00:00'::timestamptz)) AND NOT EXISTS(SELECT 1 AS "a" FROM "package_health" U0 WHERE U0."policy_run_id" = (V0."id") LIMIT 1)))) AND W0."evidence_cutoff" >= ("python_readiness_assessments"."observed_at") AND NOT EXISTS(SELECT 1 AS "a" FROM "python_readiness_assessments" U0 WHERE (U0."package_id" = ("python_readiness_assessments"."package_id") AND U0."python_series" = ("python_readiness_assessments"."python_series") AND U0."observed_at" > ("python_readiness_assessments"."observed_at") AND U0."observed_at" <= (W0."evidence_cutoff")) LIMIT 1)) LIMIT 1) AND "python_readiness_assessments"."id" > 0) ORDER BY 1 ASC LIMIT 1000
Limit  (cost=1220.64..412723.07 rows=1000 width=8) (actual time=23.789..149.382 rows=1000 loops=1)
  Buffers: shared hit=369460
  ->  Nested Loop Anti Join  (cost=1220.64..2193705.56 rows=5328 width=8) (actual time=16.022..141.566 rows=1000 loops=1)
        Join Filter: ((w0.evidence_cutoff >= python_readiness_assessments.observed_at) AND (NOT EXISTS(SubPlan 2)))
        Rows Removed by Join Filter: 89000
        Buffers: shared hit=369460
        ->  Gather Merge  (cost=1000.88..145654.50 rows=6394 width=29) (actual time=15.557..18.548 rows=1000 loops=1)
              Workers Planned: 2
              Workers Launched: 2
              Buffers: shared hit=12204
              ->  Nested Loop Semi Join  (cost=0.85..143916.45 rows=2664 width=29) (actual time=3.105..5.658 rows=999 loops=3)
                    Buffers: shared hit=12204
                    ->  Parallel Index Scan using python_readiness_assessments_pkey on python_readiness_assessments  (cost=0.43..81016.74 rows=7992 width=29) (actual time=3.090..3.213 rows=1009 loops=3)
                          Index Cond: (id > 0)
                          Filter: (observed_at < '2026-06-16 12:00:00+00'::timestamp with time zone)
                          Buffers: shared hit=89
                    ->  Index Scan using py_readiness_pkg_observed on python_readiness_assessments u0  (cost=0.43..11.17 rows=30 width=21) (actual time=0.002..0.002 rows=1 loops=3028)
                          Index Cond: ((package_id = python_readiness_assessments.package_id) AND (observed_at > python_readiness_assessments.observed_at))
                          Filter: ((python_readiness_assessments.python_series)::text = (python_series)::text)
                          Rows Removed by Filter: 0
                          Buffers: shared hit=12115
        ->  Materialize  (cost=219.77..223.12 rows=45 width=8) (actual time=0.000..0.003 rows=89 loops=1000)
              Buffers: shared hit=7
              ->  Seq Scan on policy_runs w0  (cost=219.77..222.89 rows=45 width=8) (actual time=0.258..0.269 rows=89 loops=1)
                    Filter: (NOT (ANY (id = (hashed SubPlan 1).col1)))
                    Rows Removed by Filter: 1
                    Buffers: shared hit=7
                    SubPlan 1
                      ->  Merge Anti Join  (cost=3.64..219.76 rows=1 width=8) (actual time=0.100..0.101 rows=1 loops=1)
                            Merge Cond: (v0.id = u0_1.policy_run_id)
                            Buffers: shared hit=5
                            ->  Sort  (cost=3.36..3.36 rows=1 width=8) (actual time=0.087..0.088 rows=1 loops=1)
                                  Sort Key: v0.id
                                  Sort Method: quicksort  Memory: 25kB
                                  Buffers: shared hit=2
                                  ->  Seq Scan on policy_runs v0  (cost=0.00..3.35 rows=1 width=8) (actual time=0.078..0.083 rows=1 loops=1)
                                        Filter: ((finished_at < '2026-06-16 12:00:00+00'::timestamp with time zone) OR ((finished_at IS NULL) AND (started_at < '2026-06-16 12:00:00+00'::timestamp with time zone)))
                                        Rows Removed by Filter: 89
                                        Buffers: shared hit=2
                            ->  Index Only Scan using package_health_policy_run_id_d0e7bf06 on package_health u0_1  (cost=0.29..190.28 rows=10000 width=8) (actual time=0.007..0.007 rows=1 loops=1)
                                  Heap Fetches: 0
                                  Buffers: shared hit=3
        SubPlan 2
          ->  Index Scan using py_readiness_pkg_observed on python_readiness_assessments u0_2  (cost=0.43..8.45 rows=1 width=0) (actual time=0.001..0.001 rows=1 loops=89000)
                Index Cond: ((package_id = python_readiness_assessments.package_id) AND (observed_at > python_readiness_assessments.observed_at) AND (observed_at <= w0.evidence_cutoff))
                Filter: ((python_series)::text = (python_readiness_assessments.python_series)::text)
                Rows Removed by Filter: 0
                Buffers: shared hit=357249
Planning:
  Buffers: shared hit=26
Planning Time: 0.211 ms
JIT:
  Functions: 95
  Options: Inlining false, Optimization false, Expressions true, Deforming true
  Timing: Generation 1.748 ms (Deform 0.771 ms), Inlining 0.000 ms, Optimization 0.890 ms, Emission 16.222 ms, Total 18.860 ms
Execution Time: 150.278 ms

--- purge.removable_evidence first batch: python_readiness_assessments
SQL: SELECT "python_readiness_assessments"."id" AS "pk" FROM "python_readiness_assessments" WHERE ("python_readiness_assessments"."observed_at" < '2026-06-16 12:00:00+00:00'::timestamptz AND EXISTS(SELECT 1 AS "a" FROM "python_readiness_assessments" U0 WHERE (U0."package_id" = ("python_readiness_assessments"."package_id") AND U0."python_series" = ("python_readiness_assessments"."python_series") AND U0."observed_at" > ("python_readiness_assessments"."observed_at")) LIMIT 1) AND NOT EXISTS(SELECT 1 AS "a" FROM "policy_runs" W0 WHERE (NOT (W0."id" IN (SELECT V0."id" AS "pk" FROM "policy_runs" V0 WHERE ((V0."finished_at" < '2026-06-16 12:00:00+00:00'::timestamptz OR (V0."finished_at" IS NULL AND V0."started_at" < '2026-06-16 12:00:00+00:00'::timestamptz)) AND NOT EXISTS(SELECT 1 AS "a" FROM "package_health" U0 WHERE U0."policy_run_id" = (V0."id") LIMIT 1)))) AND W0."evidence_cutoff" >= ("python_readiness_assessments"."observed_at") AND NOT EXISTS(SELECT 1 AS "a" FROM "python_readiness_assessments" U0 WHERE (U0."package_id" = ("python_readiness_assessments"."package_id") AND U0."python_series" = ("python_readiness_assessments"."python_series") AND U0."observed_at" > ("python_readiness_assessments"."observed_at") AND U0."observed_at" <= (W0."evidence_cutoff")) LIMIT 1)) LIMIT 1) AND NOT EXISTS(SELECT 1 AS "a" FROM "package_python_readiness" X0 WHERE (X0."assessment_id" = ("python_readiness_assessments"."id") AND X0."policy_run_id" IN (SELECT W0."id" AS "pk" FROM "policy_runs" W0 WHERE NOT (W0."id" IN (SELECT V0."id" AS "pk" FROM "policy_runs" V0 WHERE ((V0."finished_at" < '2026-06-16 12:00:00+00:00'::timestamptz OR (V0."finished_at" IS NULL AND V0."started_at" < '2026-06-16 12:00:00+00:00'::timestamptz)) AND NOT EXISTS(SELECT 1 AS "a" FROM "package_health" U0 WHERE U0."policy_run_id" = (V0."id") LIMIT 1)))))) LIMIT 1) AND "python_readiness_assessments"."id" > 0) ORDER BY 1 ASC LIMIT 1000
Limit  (cost=1443.95..444015.05 rows=1000 width=8) (actual time=31.385..158.762 rows=1000 loops=1)
  Buffers: shared hit=369662
  ->  Nested Loop Anti Join  (cost=1443.95..1776596.62 rows=4011 width=8) (actual time=20.855..148.181 rows=1000 loops=1)
        Join Filter: ((w0.evidence_cutoff >= python_readiness_assessments.observed_at) AND (NOT EXISTS(SubPlan 2)))
        Rows Removed by Join Filter: 89000
        Buffers: shared hit=369662
        ->  Merge Anti Join  (cost=1224.18..234745.60 rows=4813 width=29) (actual time=20.492..23.567 rows=1000 loops=1)
              Merge Cond: (python_readiness_assessments.id = x0.assessment_id)
              Buffers: shared hit=12406
              ->  Gather Merge  (cost=1000.88..145654.50 rows=6394 width=29) (actual time=18.179..21.174 rows=1000 loops=1)
                    Workers Planned: 2
                    Workers Launched: 2
                    Buffers: shared hit=12204
                    ->  Nested Loop Semi Join  (cost=0.85..143916.45 rows=2664 width=29) (actual time=3.756..6.327 rows=999 loops=3)
                          Buffers: shared hit=12204
                          ->  Parallel Index Scan using python_readiness_assessments_pkey on python_readiness_assessments  (cost=0.43..81016.74 rows=7992 width=29) (actual time=3.739..3.863 rows=1009 loops=3)
                                Index Cond: (id > 0)
                                Filter: (observed_at < '2026-06-16 12:00:00+00'::timestamp with time zone)
                                Buffers: shared hit=89
                          ->  Index Scan using py_readiness_pkg_observed on python_readiness_assessments u0  (cost=0.43..11.17 rows=30 width=21) (actual time=0.002..0.002 rows=1 loops=3028)
                                Index Cond: ((package_id = python_readiness_assessments.package_id) AND (observed_at > python_readiness_assessments.observed_at))
                                Filter: ((python_readiness_assessments.python_series)::text = (python_series)::text)
                                Rows Removed by Filter: 0
                                Buffers: shared hit=12115
              ->  Nested Loop  (cost=220.34..87934.30 rows=450000 width=8) (actual time=2.299..2.301 rows=1 loops=1)
                    Buffers: shared hit=202
                    ->  Index Scan using package_python_readiness_assessment_id_a29f10c0 on package_python_readiness x0  (cost=0.42..45564.43 rows=900000 width=16) (actual time=0.011..0.654 rows=10001 loops=1)
                          Buffers: shared hit=194
                    ->  Memoize  (cost=219.92..219.94 rows=1 width=8) (actual time=0.000..0.000 rows=0 loops=10001)
                          Cache Key: x0.policy_run_id
                          Cache Mode: logical
                          Hits: 9999  Misses: 2  Evictions: 0  Overflows: 0  Memory Usage: 1kB
                          Buffers: shared hit=8
                          ->  Index Only Scan using policy_runs_pkey on policy_runs w0_1  (cost=219.91..219.93 rows=1 width=8) (actual time=0.134..0.134 rows=0 loops=2)
                                Index Cond: (id = x0.policy_run_id)
                                Filter: (NOT (ANY (id = (hashed SubPlan 3).col1)))
                                Rows Removed by Filter: 0
                                Heap Fetches: 0
                                Buffers: shared hit=8
                                SubPlan 3
                                  ->  Merge Anti Join  (cost=3.64..219.76 rows=1 width=8) (actual time=0.105..0.106 rows=1 loops=1)
                                        Merge Cond: (v0_1.id = u0_3.policy_run_id)
                                        Buffers: shared hit=5
                                        ->  Sort  (cost=3.36..3.36 rows=1 width=8) (actual time=0.096..0.097 rows=1 loops=1)
                                              Sort Key: v0_1.id
                                              Sort Method: quicksort  Memory: 25kB
                                              Buffers: shared hit=2
                                              ->  Seq Scan on policy_runs v0_1  (cost=0.00..3.35 rows=1 width=8) (actual time=0.088..0.091 rows=1 loops=1)
                                                    Filter: ((finished_at < '2026-06-16 12:00:00+00'::timestamp with time zone) OR ((finished_at IS NULL) AND (started_at < '2026-06-16 12:00:00+00'::timestamp with time zone)))
                                                    Rows Removed by Filter: 89
                                                    Buffers: shared hit=2
                                        ->  Index Only Scan using package_health_policy_run_id_d0e7bf06 on package_health u0_3  (cost=0.29..190.28 rows=10000 width=8) (actual time=0.006..0.006 rows=1 loops=1)
                                              Heap Fetches: 0
                                              Buffers: shared hit=3
        ->  Materialize  (cost=219.77..223.12 rows=45 width=8) (actual time=0.000..0.003 rows=89 loops=1000)
              Buffers: shared hit=7
              ->  Seq Scan on policy_runs w0  (cost=219.77..222.89 rows=45 width=8) (actual time=0.163..0.172 rows=89 loops=1)
                    Filter: (NOT (ANY (id = (hashed SubPlan 1).col1)))
                    Rows Removed by Filter: 1
                    Buffers: shared hit=7
                    SubPlan 1
                      ->  Merge Anti Join  (cost=3.64..219.76 rows=1 width=8) (actual time=0.061..0.062 rows=1 loops=1)
                            Merge Cond: (v0.id = u0_1.policy_run_id)
                            Buffers: shared hit=5
                            ->  Sort  (cost=3.36..3.36 rows=1 width=8) (actual time=0.055..0.055 rows=1 loops=1)
                                  Sort Key: v0.id
                                  Sort Method: quicksort  Memory: 25kB
                                  Buffers: shared hit=2
                                  ->  Seq Scan on policy_runs v0  (cost=0.00..3.35 rows=1 width=8) (actual time=0.050..0.053 rows=1 loops=1)
                                        Filter: ((finished_at < '2026-06-16 12:00:00+00'::timestamp with time zone) OR ((finished_at IS NULL) AND (started_at < '2026-06-16 12:00:00+00'::timestamp with time zone)))
                                        Rows Removed by Filter: 89
                                        Buffers: shared hit=2
                            ->  Index Only Scan using package_health_policy_run_id_d0e7bf06 on package_health u0_1  (cost=0.29..190.28 rows=10000 width=8) (actual time=0.002..0.003 rows=1 loops=1)
                                  Heap Fetches: 0
                                  Buffers: shared hit=3
        SubPlan 2
          ->  Index Scan using py_readiness_pkg_observed on python_readiness_assessments u0_2  (cost=0.43..8.45 rows=1 width=0) (actual time=0.001..0.001 rows=1 loops=89000)
                Index Cond: ((package_id = python_readiness_assessments.package_id) AND (observed_at > python_readiness_assessments.observed_at) AND (observed_at <= w0.evidence_cutoff))
                Filter: ((python_series)::text = (python_readiness_assessments.python_series)::text)
                Rows Removed by Filter: 0
                Buffers: shared hit=357249
Planning:
  Buffers: shared hit=52
Planning Time: 0.382 ms
JIT:
  Functions: 142
  Options: Inlining false, Optimization false, Expressions true, Deforming true
  Timing: Generation 2.142 ms (Deform 0.917 ms), Inlining 0.000 ms, Optimization 1.110 ms, Emission 20.762 ms, Total 24.015 ms
Execution Time: 159.935 ms

--- purge._older: python_verification_results
SQL: SELECT COUNT(*) AS "__count" FROM "python_verification_results" WHERE "python_verification_results"."observed_at" < '2026-06-16 12:00:00+00:00'::timestamptz
Aggregate  (cost=463.55..463.56 rows=1 width=8) (actual time=1.070..1.070 rows=1 loops=1)
  Buffers: shared hit=27
  ->  Index Only Scan using py_verify_observed on python_verification_results  (cost=0.43..417.16 rows=18556 width=0) (actual time=0.004..0.616 rows=20000 loops=1)
        Index Cond: (observed_at < '2026-06-16 12:00:00+00'::timestamp with time zone)
        Heap Fetches: 0
        Buffers: shared hit=27
Planning Time: 0.032 ms
Execution Time: 1.075 ms

--- purge.floor_removable_evidence first batch: python_verification_results
SQL: SELECT "python_verification_results"."id" AS "pk" FROM "python_verification_results" WHERE ("python_verification_results"."observed_at" < '2026-06-16 12:00:00+00:00'::timestamptz AND EXISTS(SELECT 1 AS "a" FROM "python_verification_results" U0 WHERE (U0."package_id" = ("python_verification_results"."package_id") AND U0."python_series" = ("python_verification_results"."python_series") AND U0."observed_at" > ("python_verification_results"."observed_at")) LIMIT 1) AND NOT EXISTS(SELECT 1 AS "a" FROM "policy_runs" W0 WHERE (NOT (W0."id" IN (SELECT V0."id" AS "pk" FROM "policy_runs" V0 WHERE ((V0."finished_at" < '2026-06-16 12:00:00+00:00'::timestamptz OR (V0."finished_at" IS NULL AND V0."started_at" < '2026-06-16 12:00:00+00:00'::timestamptz)) AND NOT EXISTS(SELECT 1 AS "a" FROM "package_health" U0 WHERE U0."policy_run_id" = (V0."id") LIMIT 1)))) AND W0."evidence_cutoff" >= ("python_verification_results"."observed_at") AND NOT EXISTS(SELECT 1 AS "a" FROM "python_verification_results" U0 WHERE (U0."package_id" = ("python_verification_results"."package_id") AND U0."python_series" = ("python_verification_results"."python_series") AND U0."observed_at" > ("python_verification_results"."observed_at") AND U0."observed_at" <= (W0."evidence_cutoff")) LIMIT 1)) LIMIT 1) AND "python_verification_results"."id" > 0) ORDER BY 1 ASC LIMIT 1000
Limit  (cost=1220.64..412912.17 rows=1000 width=8) (actual time=25.205..150.822 rows=1000 loops=1)
  Buffers: shared hit=369418
  ->  Nested Loop Anti Join  (cost=1220.64..2123078.79 rows=5154 width=8) (actual time=17.263..142.831 rows=1000 loops=1)
        Join Filter: ((w0.evidence_cutoff >= python_verification_results.observed_at) AND (NOT EXISTS(SubPlan 2)))
        Rows Removed by Join Filter: 89000
        Buffers: shared hit=369418
        ->  Gather Merge  (cost=1000.88..141908.10 rows=6185 width=29) (actual time=16.794..19.863 rows=1000 loops=1)
              Workers Planned: 2
              Workers Launched: 2
              Buffers: shared hit=12199
              ->  Nested Loop Semi Join  (cost=0.85..140194.17 rows=2577 width=29) (actual time=3.114..5.750 rows=999 loops=3)
                    Buffers: shared hit=12199
                    ->  Parallel Index Scan using python_verification_results_pkey on python_verification_results  (cost=0.43..79451.78 rows=7732 width=29) (actual time=3.097..3.231 rows=1009 loops=3)
                          Index Cond: (id > 0)
                          Filter: (observed_at < '2026-06-16 12:00:00+00'::timestamp with time zone)
                          Buffers: shared hit=85
                    ->  Index Scan using py_verify_pkg_observed on python_verification_results u0  (cost=0.43..11.15 rows=30 width=21) (actual time=0.002..0.002 rows=1 loops=3028)
                          Index Cond: ((package_id = python_verification_results.package_id) AND (observed_at > python_verification_results.observed_at))
                          Filter: ((python_verification_results.python_series)::text = (python_series)::text)
                          Rows Removed by Filter: 0
                          Buffers: shared hit=12114
        ->  Materialize  (cost=219.77..223.12 rows=45 width=8) (actual time=0.000..0.003 rows=89 loops=1000)
              Buffers: shared hit=7
              ->  Seq Scan on policy_runs w0  (cost=219.77..222.89 rows=45 width=8) (actual time=0.255..0.266 rows=89 loops=1)
                    Filter: (NOT (ANY (id = (hashed SubPlan 1).col1)))
                    Rows Removed by Filter: 1
                    Buffers: shared hit=7
                    SubPlan 1
                      ->  Merge Anti Join  (cost=3.64..219.76 rows=1 width=8) (actual time=0.094..0.095 rows=1 loops=1)
                            Merge Cond: (v0.id = u0_1.policy_run_id)
                            Buffers: shared hit=5
                            ->  Sort  (cost=3.36..3.36 rows=1 width=8) (actual time=0.082..0.083 rows=1 loops=1)
                                  Sort Key: v0.id
                                  Sort Method: quicksort  Memory: 25kB
                                  Buffers: shared hit=2
                                  ->  Seq Scan on policy_runs v0  (cost=0.00..3.35 rows=1 width=8) (actual time=0.075..0.079 rows=1 loops=1)
                                        Filter: ((finished_at < '2026-06-16 12:00:00+00'::timestamp with time zone) OR ((finished_at IS NULL) AND (started_at < '2026-06-16 12:00:00+00'::timestamp with time zone)))
                                        Rows Removed by Filter: 89
                                        Buffers: shared hit=2
                            ->  Index Only Scan using package_health_policy_run_id_d0e7bf06 on package_health u0_1  (cost=0.29..190.28 rows=10000 width=8) (actual time=0.007..0.007 rows=1 loops=1)
                                  Heap Fetches: 0
                                  Buffers: shared hit=3
        SubPlan 2
          ->  Index Scan using py_verify_pkg_observed on python_verification_results u0_2  (cost=0.43..8.45 rows=1 width=0) (actual time=0.001..0.001 rows=1 loops=89000)
                Index Cond: ((package_id = python_verification_results.package_id) AND (observed_at > python_verification_results.observed_at) AND (observed_at <= w0.evidence_cutoff))
                Filter: ((python_series)::text = (python_verification_results.python_series)::text)
                Rows Removed by Filter: 0
                Buffers: shared hit=357212
Planning:
  Buffers: shared hit=26
Planning Time: 0.205 ms
JIT:
  Functions: 95
  Options: Inlining false, Optimization false, Expressions true, Deforming true
  Timing: Generation 1.857 ms (Deform 0.810 ms), Inlining 0.000 ms, Optimization 0.963 ms, Emission 16.336 ms, Total 19.156 ms
Execution Time: 151.752 ms

--- purge.removable_evidence first batch: python_verification_results
SQL: SELECT "python_verification_results"."id" AS "pk" FROM "python_verification_results" WHERE ("python_verification_results"."observed_at" < '2026-06-16 12:00:00+00:00'::timestamptz AND EXISTS(SELECT 1 AS "a" FROM "python_verification_results" U0 WHERE (U0."package_id" = ("python_verification_results"."package_id") AND U0."python_series" = ("python_verification_results"."python_series") AND U0."observed_at" > ("python_verification_results"."observed_at")) LIMIT 1) AND NOT EXISTS(SELECT 1 AS "a" FROM "policy_runs" W0 WHERE (NOT (W0."id" IN (SELECT V0."id" AS "pk" FROM "policy_runs" V0 WHERE ((V0."finished_at" < '2026-06-16 12:00:00+00:00'::timestamptz OR (V0."finished_at" IS NULL AND V0."started_at" < '2026-06-16 12:00:00+00:00'::timestamptz)) AND NOT EXISTS(SELECT 1 AS "a" FROM "package_health" U0 WHERE U0."policy_run_id" = (V0."id") LIMIT 1)))) AND W0."evidence_cutoff" >= ("python_verification_results"."observed_at") AND NOT EXISTS(SELECT 1 AS "a" FROM "python_verification_results" U0 WHERE (U0."package_id" = ("python_verification_results"."package_id") AND U0."python_series" = ("python_verification_results"."python_series") AND U0."observed_at" > ("python_verification_results"."observed_at") AND U0."observed_at" <= (W0."evidence_cutoff")) LIMIT 1)) LIMIT 1) AND NOT EXISTS(SELECT 1 AS "a" FROM "package_python_readiness" X0 WHERE (X0."verification_id" = ("python_verification_results"."id") AND X0."policy_run_id" IN (SELECT W0."id" AS "pk" FROM "policy_runs" W0 WHERE NOT (W0."id" IN (SELECT V0."id" AS "pk" FROM "policy_runs" V0 WHERE ((V0."finished_at" < '2026-06-16 12:00:00+00:00'::timestamptz OR (V0."finished_at" IS NULL AND V0."started_at" < '2026-06-16 12:00:00+00:00'::timestamptz)) AND NOT EXISTS(SELECT 1 AS "a" FROM "package_health" U0 WHERE U0."policy_run_id" = (V0."id") LIMIT 1)))))) LIMIT 1) AND "python_verification_results"."id" > 0) ORDER BY 1 ASC LIMIT 1000
Limit  (cost=1440.99..436040.52 rows=1000 width=8) (actual time=28.216..157.101 rows=1000 loops=1)
  Buffers: shared hit=369600
  ->  Merge Anti Join  (cost=1440.99..2241366.99 rows=5154 width=8) (actual time=17.455..146.294 rows=1000 loops=1)
        Merge Cond: (python_verification_results.id = x0.verification_id)
        Buffers: shared hit=369600
        ->  Nested Loop Anti Join  (cost=1220.64..2123078.79 rows=5154 width=8) (actual time=15.333..144.110 rows=1000 loops=1)
              Join Filter: ((w0.evidence_cutoff >= python_verification_results.observed_at) AND (NOT EXISTS(SubPlan 2)))
              Rows Removed by Join Filter: 89000
              Buffers: shared hit=369418
              ->  Gather Merge  (cost=1000.88..141908.10 rows=6185 width=29) (actual time=14.880..17.974 rows=1000 loops=1)
                    Workers Planned: 2
                    Workers Launched: 2
                    Buffers: shared hit=12199
                    ->  Nested Loop Semi Join  (cost=0.85..140194.17 rows=2577 width=29) (actual time=3.695..6.327 rows=999 loops=3)
                          Buffers: shared hit=12199
                          ->  Parallel Index Scan using python_verification_results_pkey on python_verification_results  (cost=0.43..79451.78 rows=7732 width=29) (actual time=3.677..3.813 rows=1009 loops=3)
                                Index Cond: (id > 0)
                                Filter: (observed_at < '2026-06-16 12:00:00+00'::timestamp with time zone)
                                Buffers: shared hit=85
                          ->  Index Scan using py_verify_pkg_observed on python_verification_results u0  (cost=0.43..11.15 rows=30 width=21) (actual time=0.002..0.002 rows=1 loops=3028)
                                Index Cond: ((package_id = python_verification_results.package_id) AND (observed_at > python_verification_results.observed_at))
                                Filter: ((python_verification_results.python_series)::text = (python_series)::text)
                                Rows Removed by Filter: 0
                                Buffers: shared hit=12114
              ->  Materialize  (cost=219.77..223.12 rows=45 width=8) (actual time=0.000..0.003 rows=89 loops=1000)
                    Buffers: shared hit=7
                    ->  Seq Scan on policy_runs w0  (cost=219.77..222.89 rows=45 width=8) (actual time=0.245..0.256 rows=89 loops=1)
                          Filter: (NOT (ANY (id = (hashed SubPlan 1).col1)))
                          Rows Removed by Filter: 1
                          Buffers: shared hit=7
                          SubPlan 1
                            ->  Merge Anti Join  (cost=3.64..219.76 rows=1 width=8) (actual time=0.090..0.092 rows=1 loops=1)
                                  Merge Cond: (v0.id = u0_1.policy_run_id)
                                  Buffers: shared hit=5
                                  ->  Sort  (cost=3.36..3.36 rows=1 width=8) (actual time=0.080..0.080 rows=1 loops=1)
                                        Sort Key: v0.id
                                        Sort Method: quicksort  Memory: 25kB
                                        Buffers: shared hit=2
                                        ->  Seq Scan on policy_runs v0  (cost=0.00..3.35 rows=1 width=8) (actual time=0.072..0.076 rows=1 loops=1)
                                              Filter: ((finished_at < '2026-06-16 12:00:00+00'::timestamp with time zone) OR ((finished_at IS NULL) AND (started_at < '2026-06-16 12:00:00+00'::timestamp with time zone)))
                                              Rows Removed by Filter: 89
                                              Buffers: shared hit=2
                                  ->  Index Only Scan using package_health_policy_run_id_d0e7bf06 on package_health u0_1  (cost=0.29..190.28 rows=10000 width=8) (actual time=0.006..0.006 rows=1 loops=1)
                                        Heap Fetches: 0
                                        Buffers: shared hit=3
              SubPlan 2
                ->  Index Scan using py_verify_pkg_observed on python_verification_results u0_2  (cost=0.43..8.45 rows=1 width=0) (actual time=0.001..0.001 rows=1 loops=89000)
                      Index Cond: ((package_id = python_verification_results.package_id) AND (observed_at > python_verification_results.observed_at) AND (observed_at <= w0.evidence_cutoff))
                      Filter: ((python_series)::text = (python_verification_results.python_series)::text)
                      Rows Removed by Filter: 0
                      Buffers: shared hit=357212
        ->  Nested Loop  (cost=220.34..117150.30 rows=450000 width=8) (actual time=2.119..2.121 rows=1 loops=1)
              Buffers: shared hit=182
              ->  Index Scan using package_python_readiness_verification_id_6eddb09b on package_python_readiness x0  (cost=0.42..74780.43 rows=900000 width=16) (actual time=0.006..0.587 rows=10001 loops=1)
                    Buffers: shared hit=174
              ->  Memoize  (cost=219.92..219.94 rows=1 width=8) (actual time=0.000..0.000 rows=0 loops=10001)
                    Cache Key: x0.policy_run_id
                    Cache Mode: logical
                    Hits: 9999  Misses: 2  Evictions: 0  Overflows: 0  Memory Usage: 1kB
                    Buffers: shared hit=8
                    ->  Index Only Scan using policy_runs_pkey on policy_runs w0_1  (cost=219.91..219.93 rows=1 width=8) (actual time=0.086..0.087 rows=0 loops=2)
                          Index Cond: (id = x0.policy_run_id)
                          Filter: (NOT (ANY (id = (hashed SubPlan 3).col1)))
                          Rows Removed by Filter: 0
                          Heap Fetches: 0
                          Buffers: shared hit=8
                          SubPlan 3
                            ->  Merge Anti Join  (cost=3.64..219.76 rows=1 width=8) (actual time=0.068..0.069 rows=1 loops=1)
                                  Merge Cond: (v0_1.id = u0_3.policy_run_id)
                                  Buffers: shared hit=5
                                  ->  Sort  (cost=3.36..3.36 rows=1 width=8) (actual time=0.061..0.062 rows=1 loops=1)
                                        Sort Key: v0_1.id
                                        Sort Method: quicksort  Memory: 25kB
                                        Buffers: shared hit=2
                                        ->  Seq Scan on policy_runs v0_1  (cost=0.00..3.35 rows=1 width=8) (actual time=0.057..0.060 rows=1 loops=1)
                                              Filter: ((finished_at < '2026-06-16 12:00:00+00'::timestamp with time zone) OR ((finished_at IS NULL) AND (started_at < '2026-06-16 12:00:00+00'::timestamp with time zone)))
                                              Rows Removed by Filter: 89
                                              Buffers: shared hit=2
                                  ->  Index Only Scan using package_health_policy_run_id_d0e7bf06 on package_health u0_3  (cost=0.29..190.28 rows=10000 width=8) (actual time=0.003..0.003 rows=1 loops=1)
                                        Heap Fetches: 0
                                        Buffers: shared hit=3
Planning:
  Buffers: shared hit=36
Planning Time: 0.310 ms
JIT:
  Functions: 142
  Options: Inlining false, Optimization false, Expressions true, Deforming true
  Timing: Generation 2.066 ms (Deform 0.890 ms), Inlining 0.000 ms, Optimization 1.084 ms, Emission 20.839 ms, Total 23.989 ms
Execution Time: 158.244 ms

--- purge._older: identity_resolution_snapshots
SQL: SELECT COUNT(*) AS "__count" FROM "identity_resolution_snapshots" WHERE "identity_resolution_snapshots"."observed_at" < '2026-06-16 12:00:00+00:00'::timestamptz
Aggregate  (cost=270.52..270.53 rows=1 width=8) (actual time=0.578..0.578 rows=1 loops=1)
  Buffers: shared hit=12
  ->  Index Only Scan using id_resolution_observed on identity_resolution_snapshots  (cost=0.42..243.26 rows=10905 width=0) (actual time=0.003..0.310 rows=10000 loops=1)
        Index Cond: (observed_at < '2026-06-16 12:00:00+00'::timestamp with time zone)
        Heap Fetches: 0
        Buffers: shared hit=12
Planning Time: 0.015 ms
Execution Time: 0.581 ms

--- purge.floor_removable_evidence first batch: identity_resolution_snapshots
SQL: SELECT "identity_resolution_snapshots"."id" AS "pk" FROM "identity_resolution_snapshots" WHERE ("identity_resolution_snapshots"."observed_at" < '2026-06-16 12:00:00+00:00'::timestamptz AND EXISTS(SELECT 1 AS "a" FROM "identity_resolution_snapshots" U0 WHERE (U0."package_id" = ("identity_resolution_snapshots"."package_id") AND U0."observed_at" > ("identity_resolution_snapshots"."observed_at")) LIMIT 1) AND NOT EXISTS(SELECT 1 AS "a" FROM "policy_runs" W0 WHERE (NOT (W0."id" IN (SELECT V0."id" AS "pk" FROM "policy_runs" V0 WHERE ((V0."finished_at" < '2026-06-16 12:00:00+00:00'::timestamptz OR (V0."finished_at" IS NULL AND V0."started_at" < '2026-06-16 12:00:00+00:00'::timestamptz)) AND NOT EXISTS(SELECT 1 AS "a" FROM "package_health" U0 WHERE U0."policy_run_id" = (V0."id") LIMIT 1)))) AND W0."evidence_cutoff" >= ("identity_resolution_snapshots"."observed_at") AND NOT EXISTS(SELECT 1 AS "a" FROM "identity_resolution_snapshots" U0 WHERE (U0."package_id" = ("identity_resolution_snapshots"."package_id") AND U0."observed_at" > ("identity_resolution_snapshots"."observed_at") AND U0."observed_at" <= (W0."evidence_cutoff")) LIMIT 1)) LIMIT 1) AND "identity_resolution_snapshots"."id" > 0) ORDER BY 1 ASC LIMIT 1000
Limit  (cost=220.62..221020.73 rows=1000 width=8) (actual time=5.934..108.408 rows=1000 loops=1)
  Buffers: shared hit=270162
  ->  Nested Loop Anti Join  (cost=220.62..669024.15 rows=3029 width=8) (actual time=0.263..102.697 rows=1000 loops=1)
        Join Filter: ((w0.evidence_cutoff >= identity_resolution_snapshots.observed_at) AND (NOT EXISTS(SubPlan 2)))
        Rows Removed by Join Filter: 89000
        Buffers: shared hit=270162
        ->  Nested Loop Semi Join  (cost=0.85..55249.11 rows=3635 width=24) (actual time=0.015..2.009 rows=1000 loops=1)
              Buffers: shared hit=3061
              ->  Index Scan using identity_resolution_snapshots_pkey on identity_resolution_snapshots  (cost=0.42..49891.43 rows=10905 width=24) (actual time=0.009..0.106 rows=1010 loops=1)
                    Index Cond: (id > 0)
                    Filter: (observed_at < '2026-06-16 12:00:00+00'::timestamp with time zone)
                    Buffers: shared hit=30
              ->  Index Only Scan using id_resolution_pkg_observed on identity_resolution_snapshots u0  (cost=0.42..2.61 rows=30 width=16) (actual time=0.002..0.002 rows=1 loops=1010)
                    Index Cond: ((package_id = identity_resolution_snapshots.package_id) AND (observed_at > identity_resolution_snapshots.observed_at))
                    Heap Fetches: 0
                    Buffers: shared hit=3031
        ->  Materialize  (cost=219.77..223.12 rows=45 width=8) (actual time=0.000..0.002 rows=89 loops=1000)
              Buffers: shared hit=7
              ->  Seq Scan on policy_runs w0  (cost=219.77..222.89 rows=45 width=8) (actual time=0.123..0.132 rows=89 loops=1)
                    Filter: (NOT (ANY (id = (hashed SubPlan 1).col1)))
                    Rows Removed by Filter: 1
                    Buffers: shared hit=7
                    SubPlan 1
                      ->  Merge Anti Join  (cost=3.64..219.76 rows=1 width=8) (actual time=0.058..0.058 rows=1 loops=1)
                            Merge Cond: (v0.id = u0_1.policy_run_id)
                            Buffers: shared hit=5
                            ->  Sort  (cost=3.36..3.36 rows=1 width=8) (actual time=0.051..0.052 rows=1 loops=1)
                                  Sort Key: v0.id
                                  Sort Method: quicksort  Memory: 25kB
                                  Buffers: shared hit=2
                                  ->  Seq Scan on policy_runs v0  (cost=0.00..3.35 rows=1 width=8) (actual time=0.047..0.050 rows=1 loops=1)
                                        Filter: ((finished_at < '2026-06-16 12:00:00+00'::timestamp with time zone) OR ((finished_at IS NULL) AND (started_at < '2026-06-16 12:00:00+00'::timestamp with time zone)))
                                        Rows Removed by Filter: 89
                                        Buffers: shared hit=2
                            ->  Index Only Scan using package_health_policy_run_id_d0e7bf06 on package_health u0_1  (cost=0.29..190.28 rows=10000 width=8) (actual time=0.003..0.003 rows=1 loops=1)
                                  Heap Fetches: 0
                                  Buffers: shared hit=3
        SubPlan 2
          ->  Index Only Scan using id_resolution_pkg_observed on identity_resolution_snapshots u0_2  (cost=0.42..4.45 rows=1 width=0) (actual time=0.001..0.001 rows=1 loops=89000)
                Index Cond: ((package_id = identity_resolution_snapshots.package_id) AND (observed_at > identity_resolution_snapshots.observed_at) AND (observed_at <= w0.evidence_cutoff))
                Heap Fetches: 0
                Buffers: shared hit=267094
Planning:
  Buffers: shared hit=26
Planning Time: 0.149 ms
JIT:
  Functions: 46
  Options: Inlining false, Optimization false, Expressions true, Deforming true
  Timing: Generation 0.573 ms (Deform 0.227 ms), Inlining 0.000 ms, Optimization 0.265 ms, Emission 5.463 ms, Total 6.302 ms
Execution Time: 109.010 ms

--- purge.removable_evidence first batch: identity_resolution_snapshots
SQL: SELECT "identity_resolution_snapshots"."id" AS "pk" FROM "identity_resolution_snapshots" WHERE ("identity_resolution_snapshots"."observed_at" < '2026-06-16 12:00:00+00:00'::timestamptz AND EXISTS(SELECT 1 AS "a" FROM "identity_resolution_snapshots" U0 WHERE (U0."package_id" = ("identity_resolution_snapshots"."package_id") AND U0."observed_at" > ("identity_resolution_snapshots"."observed_at")) LIMIT 1) AND NOT EXISTS(SELECT 1 AS "a" FROM "policy_runs" W0 WHERE (NOT (W0."id" IN (SELECT V0."id" AS "pk" FROM "policy_runs" V0 WHERE ((V0."finished_at" < '2026-06-16 12:00:00+00:00'::timestamptz OR (V0."finished_at" IS NULL AND V0."started_at" < '2026-06-16 12:00:00+00:00'::timestamptz)) AND NOT EXISTS(SELECT 1 AS "a" FROM "package_health" U0 WHERE U0."policy_run_id" = (V0."id") LIMIT 1)))) AND W0."evidence_cutoff" >= ("identity_resolution_snapshots"."observed_at") AND NOT EXISTS(SELECT 1 AS "a" FROM "identity_resolution_snapshots" U0 WHERE (U0."package_id" = ("identity_resolution_snapshots"."package_id") AND U0."observed_at" > ("identity_resolution_snapshots"."observed_at") AND U0."observed_at" <= (W0."evidence_cutoff")) LIMIT 1)) LIMIT 1) AND "identity_resolution_snapshots"."id" > 0) ORDER BY 1 ASC LIMIT 1000
Limit  (cost=220.62..221020.73 rows=1000 width=8) (actual time=5.903..113.125 rows=1000 loops=1)
  Buffers: shared hit=270162
  ->  Nested Loop Anti Join  (cost=220.62..669024.15 rows=3029 width=8) (actual time=0.260..107.433 rows=1000 loops=1)
        Join Filter: ((w0.evidence_cutoff >= identity_resolution_snapshots.observed_at) AND (NOT EXISTS(SubPlan 2)))
        Rows Removed by Join Filter: 89000
        Buffers: shared hit=270162
        ->  Nested Loop Semi Join  (cost=0.85..55249.11 rows=3635 width=24) (actual time=0.015..2.260 rows=1000 loops=1)
              Buffers: shared hit=3061
              ->  Index Scan using identity_resolution_snapshots_pkey on identity_resolution_snapshots  (cost=0.42..49891.43 rows=10905 width=24) (actual time=0.009..0.190 rows=1010 loops=1)
                    Index Cond: (id > 0)
                    Filter: (observed_at < '2026-06-16 12:00:00+00'::timestamp with time zone)
                    Buffers: shared hit=30
              ->  Index Only Scan using id_resolution_pkg_observed on identity_resolution_snapshots u0  (cost=0.42..2.61 rows=30 width=16) (actual time=0.002..0.002 rows=1 loops=1010)
                    Index Cond: ((package_id = identity_resolution_snapshots.package_id) AND (observed_at > identity_resolution_snapshots.observed_at))
                    Heap Fetches: 0
                    Buffers: shared hit=3031
        ->  Materialize  (cost=219.77..223.12 rows=45 width=8) (actual time=0.000..0.002 rows=89 loops=1000)
              Buffers: shared hit=7
              ->  Seq Scan on policy_runs w0  (cost=219.77..222.89 rows=45 width=8) (actual time=0.114..0.126 rows=89 loops=1)
                    Filter: (NOT (ANY (id = (hashed SubPlan 1).col1)))
                    Rows Removed by Filter: 1
                    Buffers: shared hit=7
                    SubPlan 1
                      ->  Merge Anti Join  (cost=3.64..219.76 rows=1 width=8) (actual time=0.055..0.056 rows=1 loops=1)
                            Merge Cond: (v0.id = u0_1.policy_run_id)
                            Buffers: shared hit=5
                            ->  Sort  (cost=3.36..3.36 rows=1 width=8) (actual time=0.049..0.049 rows=1 loops=1)
                                  Sort Key: v0.id
                                  Sort Method: quicksort  Memory: 25kB
                                  Buffers: shared hit=2
                                  ->  Seq Scan on policy_runs v0  (cost=0.00..3.35 rows=1 width=8) (actual time=0.045..0.048 rows=1 loops=1)
                                        Filter: ((finished_at < '2026-06-16 12:00:00+00'::timestamp with time zone) OR ((finished_at IS NULL) AND (started_at < '2026-06-16 12:00:00+00'::timestamp with time zone)))
                                        Rows Removed by Filter: 89
                                        Buffers: shared hit=2
                            ->  Index Only Scan using package_health_policy_run_id_d0e7bf06 on package_health u0_1  (cost=0.29..190.28 rows=10000 width=8) (actual time=0.003..0.003 rows=1 loops=1)
                                  Heap Fetches: 0
                                  Buffers: shared hit=3
        SubPlan 2
          ->  Index Only Scan using id_resolution_pkg_observed on identity_resolution_snapshots u0_2  (cost=0.42..4.45 rows=1 width=0) (actual time=0.001..0.001 rows=1 loops=89000)
                Index Cond: ((package_id = identity_resolution_snapshots.package_id) AND (observed_at > identity_resolution_snapshots.observed_at) AND (observed_at <= w0.evidence_cutoff))
                Heap Fetches: 0
                Buffers: shared hit=267094
Planning:
  Buffers: shared hit=26
Planning Time: 0.153 ms
JIT:
  Functions: 46
  Options: Inlining false, Optimization false, Expressions true, Deforming true
  Timing: Generation 0.510 ms (Deform 0.208 ms), Inlining 0.000 ms, Optimization 0.268 ms, Emission 5.431 ms, Total 6.209 ms
Execution Time: 113.916 ms

--- purge.dry-run count: vulnerability_findings
SQL: SELECT COUNT(*) AS "__count" FROM "vulnerability_findings" WHERE ("vulnerability_findings"."observed_at" < '2026-06-16 12:00:00+00:00'::timestamptz AND EXISTS(SELECT 1 AS "a" FROM "vulnerability_findings" U0 WHERE (U0."advisory_id" = ("vulnerability_findings"."advisory_id") AND U0."package_id" = ("vulnerability_findings"."package_id") AND U0."observed_at" > ("vulnerability_findings"."observed_at")) LIMIT 1) AND NOT EXISTS(SELECT 1 AS "a" FROM "policy_runs" W0 WHERE (NOT (W0."id" IN (SELECT V0."id" AS "pk" FROM "policy_runs" V0 WHERE ((V0."finished_at" < '2026-06-16 12:00:00+00:00'::timestamptz OR (V0."finished_at" IS NULL AND V0."started_at" < '2026-06-16 12:00:00+00:00'::timestamptz)) AND NOT EXISTS(SELECT 1 AS "a" FROM "package_health" U0 WHERE U0."policy_run_id" = (V0."id") LIMIT 1)))) AND W0."evidence_cutoff" >= ("vulnerability_findings"."observed_at") AND NOT EXISTS(SELECT 1 AS "a" FROM "vulnerability_findings" U0 WHERE (U0."advisory_id" = ("vulnerability_findings"."advisory_id") AND U0."package_id" = ("vulnerability_findings"."package_id") AND U0."observed_at" > ("vulnerability_findings"."observed_at") AND U0."observed_at" <= (W0."evidence_cutoff")) LIMIT 1)) LIMIT 1) AND NOT EXISTS(SELECT 1 AS "a" FROM "kev_findings" Z0 WHERE (Z0."vulnerability_finding_id" = ("vulnerability_findings"."id") AND NOT (Z0."id" IN (SELECT Y0."id" AS "pk" FROM "kev_findings" Y0 LEFT OUTER JOIN "vulnerability_findings" Y1 ON (Y0."vulnerability_finding_id" = Y1."id") WHERE (Y0."observed_at" < '2026-06-16 12:00:00+00:00'::timestamptz AND EXISTS(SELECT 1 AS "a" FROM "kev_findings" U0 LEFT OUTER JOIN "vulnerability_findings" U1 ON (U0."vulnerability_finding_id" = U1."id") WHERE (U0."package_id" = (Y0."package_id") AND COALESCE(U1."advisory_id", '') = (COALESCE(Y1."advisory_id", '')) AND U0."observed_at" > (Y0."observed_at")) LIMIT 1) AND NOT EXISTS(SELECT 1 AS "a" FROM "policy_runs" W0 WHERE (NOT (W0."id" IN (SELECT V0."id" AS "pk" FROM "policy_runs" V0 WHERE ((V0."finished_at" < '2026-06-16 12:00:00+00:00'::timestamptz OR (V0."finished_at" IS NULL AND V0."started_at" < '2026-06-16 12:00:00+00:00'::timestamptz)) AND NOT EXISTS(SELECT 1 AS "a" FROM "package_health" U0 WHERE U0."policy_run_id" = (V0."id") LIMIT 1)))) AND W0."evidence_cutoff" >= (Y0."observed_at") AND NOT EXISTS(SELECT 1 AS "a" FROM "kev_findings" U0 LEFT OUTER JOIN "vulnerability_findings" U1 ON (U0."vulnerability_finding_id" = U1."id") WHERE (U0."package_id" = (Y0."package_id") AND COALESCE(U1."advisory_id", '') = (COALESCE(Y1."advisory_id", '')) AND U0."observed_at" > (Y0."observed_at") AND U0."observed_at" <= (W0."evidence_cutoff")) LIMIT 1)) LIMIT 1) AND NOT EXISTS(SELECT 1 AS "a" FROM "package_vulnerability" X0 WHERE (X0."kev_finding_id" = (Y0."id") AND X0."policy_run_id" IN (SELECT W0."id" AS "pk" FROM "policy_runs" W0 WHERE NOT (W0."id" IN (SELECT V0."id" AS "pk" FROM "policy_runs" V0 WHERE ((V0."finished_at" < '2026-06-16 12:00:00+00:00'::timestamptz OR (V0."finished_at" IS NULL AND V0."started_at" < '2026-06-16 12:00:00+00:00'::timestamptz)) AND NOT EXISTS(SELECT 1 AS "a" FROM "package_health" U0 WHERE U0."policy_run_id" = (V0."id") LIMIT 1)))))) LIMIT 1))))) LIMIT 1) AND NOT EXISTS(SELECT 1 AS "a" FROM "package_vulnerability" X0 WHERE (X0."vulnerability_finding_id" = ("vulnerability_findings"."id") AND X0."policy_run_id" IN (SELECT W0."id" AS "pk" FROM "policy_runs" W0 WHERE NOT (W0."id" IN (SELECT V0."id" AS "pk" FROM "policy_runs" V0 WHERE ((V0."finished_at" < '2026-06-16 12:00:00+00:00'::timestamptz OR (V0."finished_at" IS NULL AND V0."started_at" < '2026-06-16 12:00:00+00:00'::timestamptz)) AND NOT EXISTS(SELECT 1 AS "a" FROM "package_health" U0 WHERE U0."policy_run_id" = (V0."id") LIMIT 1)))))) LIMIT 1) AND NOT EXISTS(SELECT 1 AS "a" FROM "package_remediation" X0 WHERE (X0."vulnerability_finding_id" = ("vulnerability_findings"."id") AND X0."policy_run_id" IN (SELECT W0."id" AS "pk" FROM "policy_runs" W0 WHERE NOT (W0."id" IN (SELECT V0."id" AS "pk" FROM "policy_runs" V0 WHERE ((V0."finished_at" < '2026-06-16 12:00:00+00:00'::timestamptz OR (V0."finished_at" IS NULL AND V0."started_at" < '2026-06-16 12:00:00+00:00'::timestamptz)) AND NOT EXISTS(SELECT 1 AS "a" FROM "package_health" U0 WHERE U0."policy_run_id" = (V0."id") LIMIT 1)))))) LIMIT 1))
Aggregate  (cost=2213318.57..2213318.58 rows=1 width=8) (actual time=15612.493..15631.530 rows=1 loops=1)
  Buffers: shared hit=20911070 read=4153997, temp read=29079 written=29364
  ->  Nested Loop Anti Join  (cost=1302135.53..2213313.31 rows=2106 width=0) (actual time=11671.362..15630.156 rows=19800 loops=1)
        Join Filter: ((w0.evidence_cutoff >= vulnerability_findings.observed_at) AND (NOT EXISTS(SubPlan 2)))
        Rows Removed by Join Filter: 1762200
        Buffers: shared hit=20911070 read=4153997, temp read=29079 written=29364
        ->  Nested Loop Semi Join  (cost=1301915.76..1403650.69 rows=2527 width=29) (actual time=11662.179..11783.995 rows=19800 loops=1)
              Buffers: shared hit=14792045 read=3201827, temp read=29079 written=29364
              ->  Hash Right Anti Join  (cost=1301915.33..1351921.89 rows=7581 width=29) (actual time=11662.118..11683.082 rows=19800 loops=1)
                    Hash Cond: (z0.vulnerability_finding_id = vulnerability_findings.id)
                    Buffers: shared hit=14741214 read=3173278, temp read=29079 written=29364
                    ->  Seq Scan on kev_findings z0  (cost=1243799.15..1290351.15 rows=901000 width=8) (actual time=10642.500..10812.582 rows=1782200 loops=1)
                          Filter: (NOT (ANY (id = (hashed SubPlan 6).col1)))
                          Rows Removed by Filter: 19800
                          Buffers: shared hit=14732837 read=3142612, temp read=29079 written=29364
                          SubPlan 6
                            ->  Nested Loop Anti Join  (cost=142837.55..1243794.12 rows=2012 width=8) (actual time=625.001..10653.550 rows=19800 loops=1)
                                  Join Filter: (NOT EXISTS(SubPlan 4))
                                  Rows Removed by Join Filter: 1762200
                                  Buffers: shared hit=14727710 read=3123712, temp read=29079 written=29364
                                  ->  Gather  (cost=142617.65..196712.56 rows=2414 width=37) (actual time=618.158..641.445 rows=19800 loops=1)
                                        Workers Planned: 2
                                        Workers Launched: 2
                                        Buffers: shared hit=95179 read=56722, temp read=29079 written=29364
                                        ->  Parallel Hash Semi Join  (cost=141617.65..195471.16 rows=1006 width=37) (actual time=605.391..622.752 rows=6600 loops=3)
                                              Hash Cond: ((y0.package_id = u0_6.package_id) AND ((COALESCE(y1.advisory_id, ''::character varying))::text = (COALESCE(u1_1.advisory_id, ''::character varying))::text))
                                              Join Filter: (u0_6.observed_at > y0.observed_at)
                                              Rows Removed by Join Filter: 124
                                              Buffers: shared hit=95179 read=56722, temp read=29079 written=29364
                                              ->  Nested Loop Left Join  (cost=24458.16..72550.75 rows=6035 width=37) (actual time=90.255..95.526 rows=6667 loops=3)
                                                    Buffers: shared hit=80013 read=15069
                                                    ->  Parallel Hash Right Anti Join  (cost=24457.73..45001.19 rows=6035 width=32) (actual time=90.231..90.619 rows=6667 loops=3)
                                                          Hash Cond: (x0_2.kev_finding_id = y0.id)
                                                          Buffers: shared hit=67 read=15014
                                                          ->  Hash Join  (cost=223.45..19759.19 rows=187500 width=8) (actual time=8.931..62.625 rows=296667 loops=3)
                                                                Hash Cond: (x0_2.policy_run_id = w0_4.id)
                                                                Buffers: shared hit=63 read=14725
                                                                ->  Parallel Seq Scan on package_vulnerability x0_2  (cost=0.00..18505.00 rows=375000 width=16) (actual time=0.028..25.222 rows=300000 loops=3)
                                                                      Buffers: shared hit=32 read=14723
                                                                ->  Hash  (cost=222.89..222.89 rows=45 width=8) (actual time=8.272..8.276 rows=89 loops=3)
                                                                      Buckets: 1024  Batches: 1  Memory Usage: 12kB
                                                                      Buffers: shared hit=31 read=2
                                                                      ->  Seq Scan on policy_runs w0_4  (cost=219.77..222.89 rows=45 width=8) (actual time=8.252..8.264 rows=89 loops=3)
                                                                            Filter: (NOT (ANY (id = (hashed SubPlan 5).col1)))
                                                                            Rows Removed by Filter: 1
                                                                            Buffers: shared hit=31 read=2
                                                                            SubPlan 5
                                                                              ->  Merge Anti Join  (cost=3.64..219.76 rows=1 width=8) (actual time=7.985..7.987 rows=1 loops=3)
                                                                                    Merge Cond: (v0_2.id = u0_5.policy_run_id)
                                                                                    Buffers: shared hit=25 read=2
                                                                                    ->  Sort  (cost=3.36..3.36 rows=1 width=8) (actual time=7.881..7.883 rows=1 loops=3)
                                                                                          Sort Key: v0_2.id
                                                                                          Sort Method: quicksort  Memory: 25kB
                                                                                          Buffers: shared hit=16
                                                                                          Worker 0:  Sort Method: quicksort  Memory: 25kB
                                                                                          Worker 1:  Sort Method: quicksort  Memory: 25kB
                                                                                          ->  Seq Scan on policy_runs v0_2  (cost=0.00..3.35 rows=1 width=8) (actual time=7.848..7.861 rows=1 loops=3)
                                                                                                Filter: ((finished_at < '2026-06-16 12:00:00+00'::timestamp with time zone) OR ((finished_at IS NULL) AND (started_at < '2026-06-16 12:00:00+00'::timestamp with time zone)))
                                                                                                Rows Removed by Filter: 89
                                                                                                Buffers: shared hit=6
                                                                                    ->  Index Only Scan using package_health_policy_run_id_d0e7bf06 on package_health u0_5  (cost=0.29..190.28 rows=10000 width=8) (actual time=0.088..0.088 rows=1 loops=3)
                                                                                          Heap Fetches: 0
                                                                                          Buffers: shared hit=9 read=2
                                                          ->  Parallel Hash  (cost=24134.08..24134.08 rows=8016 width=32) (actual time=1.984..1.986 rows=6667 loops=3)
                                                                Buckets: 32768  Batches: 1  Memory Usage: 1568kB
                                                                Buffers: shared hit=4 read=289
                                                                ->  Parallel Bitmap Heap Scan on kev_findings y0  (cost=245.53..24134.08 rows=8016 width=32) (actual time=0.512..1.372 rows=6667 loops=3)
                                                                      Recheck Cond: (observed_at < '2026-06-16 12:00:00+00'::timestamp with time zone)
                                                                      Heap Blocks: exact=100
                                                                      Buffers: shared hit=4 read=289
                                                                      ->  Bitmap Index Scan on kev_finding_observed  (cost=0.00..240.72 rows=19239 width=0) (actual time=0.450..0.450 rows=20000 loops=1)
                                                                            Index Cond: (observed_at < '2026-06-16 12:00:00+00'::timestamp with time zone)
                                                                            Buffers: shared read=26
                                                    ->  Index Scan using vulnerability_findings_pkey on vulnerability_findings y1  (cost=0.43..4.56 rows=1 width=21) (actual time=0.001..0.001 rows=1 loops=20000)
                                                          Index Cond: (id = y0.vulnerability_finding_id)
                                                          Buffers: shared hit=79946 read=55
                                              ->  Parallel Hash  (cost=100763.99..100763.99 rows=750833 width=29) (actual time=493.360..493.364 rows=600667 loops=3)
                                                    Buckets: 131072  Batches: 16  Memory Usage: 8352kB
                                                    Buffers: shared hit=15139 read=41652, temp read=18815 written=29172
                                                    ->  Parallel Hash Left Join  (cost=54057.72..100763.99 rows=750833 width=29) (actual time=316.673..426.014 rows=600667 loops=3)
                                                          Hash Cond: (u0_6.vulnerability_finding_id = u1_1.id)
                                                          Buffers: shared hit=15139 read=41652, temp read=18815 written=18976
                                                          ->  Parallel Seq Scan on kev_findings u0_6  (cost=0.00..31535.33 rows=750833 width=24) (actual time=0.043..37.105 rows=600667 loops=3)
                                                                Buffers: shared hit=3 read=24024
                                                          ->  Parallel Hash  (cost=40272.32..40272.32 rows=750832 width=21) (actual time=222.699..222.700 rows=600667 loops=3)
                                                                Buckets: 131072  Batches: 16  Memory Usage: 7264kB
                                                                Buffers: shared hit=15136 read=17628, temp written=9196
                                                                ->  Parallel Seq Scan on vulnerability_findings u1_1  (cost=0.00..40272.32 rows=750832 width=21) (actual time=135.261..172.231 rows=600667 loops=3)
                                                                      Buffers: shared hit=15136 read=17628
                                  ->  Index Scan using policy_runs_cutoff on policy_runs w0_3  (cost=219.91..220.51 rows=15 width=8) (actual time=0.001..0.010 rows=89 loops=19800)
                                        Index Cond: (evidence_cutoff >= y0.observed_at)
                                        Filter: (NOT (ANY (id = (hashed SubPlan 3).col1)))
                                        Rows Removed by Filter: 1
                                        Buffers: shared hit=59404 read=1
                                        SubPlan 3
                                          ->  Merge Anti Join  (cost=3.64..219.76 rows=1 width=8) (actual time=5.824..5.829 rows=1 loops=1)
                                                Merge Cond: (v0_1.id = u0_3.policy_run_id)
                                                Buffers: shared hit=5
                                                ->  Sort  (cost=3.36..3.36 rows=1 width=8) (actual time=5.809..5.812 rows=1 loops=1)
                                                      Sort Key: v0_1.id
                                                      Sort Method: quicksort  Memory: 25kB
                                                      Buffers: shared hit=2
                                                      ->  Seq Scan on policy_runs v0_1  (cost=0.00..3.35 rows=1 width=8) (actual time=5.801..5.804 rows=1 loops=1)
                                                            Filter: ((finished_at < '2026-06-16 12:00:00+00'::timestamp with time zone) OR ((finished_at IS NULL) AND (started_at < '2026-06-16 12:00:00+00'::timestamp with time zone)))
                                                            Rows Removed by Filter: 89
                                                            Buffers: shared hit=2
                                                ->  Index Only Scan using package_health_policy_run_id_d0e7bf06 on package_health u0_3  (cost=0.29..190.28 rows=10000 width=8) (actual time=0.009..0.010 rows=1 loops=1)
                                                      Heap Fetches: 0
                                                      Buffers: shared hit=3
                                  SubPlan 4
                                    ->  Nested Loop Left Join  (cost=0.85..16.91 rows=1 width=0) (actual time=0.005..0.005 rows=1 loops=1762200)
                                          Filter: ((COALESCE(u1.advisory_id, ''::character varying))::text = (COALESCE(y1.advisory_id, ''::character varying))::text)
                                          Rows Removed by Filter: 0
                                          Buffers: shared hit=14573127 read=3066989
                                          ->  Index Scan using kev_finding_pkg_observed on kev_findings u0_4  (cost=0.43..8.45 rows=1 width=8) (actual time=0.002..0.002 rows=2 loops=1762200)
                                                Index Cond: ((package_id = y0.package_id) AND (observed_at > y0.observed_at) AND (observed_at <= w0_3.evidence_cutoff))
                                                Buffers: shared hit=5650352 read=1416564
                                          ->  Index Scan using vulnerability_findings_pkey on vulnerability_findings u1  (cost=0.43..8.45 rows=1 width=21) (actual time=0.002..0.002 rows=1 loops=2643300)
                                                Index Cond: (id = u0_4.vulnerability_finding_id)
                                                Buffers: shared hit=8922775 read=1650425
                    ->  Hash  (cost=57926.66..57926.66 rows=15162 width=37) (actual time=765.623..765.646 rows=20000 loops=1)
                          Buckets: 32768 (originally 16384)  Batches: 1 (originally 1)  Memory Usage: 1663kB
                          Buffers: shared hit=8377 read=30666
                          ->  Hash Right Anti Join  (cost=29960.63..57926.66 rows=15162 width=37) (actual time=763.828..764.547 rows=20000 loops=1)
                                Hash Cond: (x0.vulnerability_finding_id = vulnerability_findings.id)
                                Buffers: shared hit=8377 read=30666
                                ->  Hash Join  (cost=223.45..26452.20 rows=450000 width=8) (actual time=6.757..124.504 rows=890000 loops=1)
                                      Hash Cond: (x0.policy_run_id = w0_1.id)
                                      Buffers: shared hit=7 read=14755
                                      ->  Seq Scan on package_vulnerability x0  (cost=0.00..23755.00 rows=900000 width=16) (actual time=0.020..53.251 rows=900000 loops=1)
                                            Buffers: shared read=14755
                                      ->  Hash  (cost=222.89..222.89 rows=45 width=8) (actual time=5.707..5.717 rows=89 loops=1)
                                            Buckets: 1024  Batches: 1  Memory Usage: 12kB
                                            Buffers: shared hit=7
                                            ->  Seq Scan on policy_runs w0_1  (cost=219.77..222.89 rows=45 width=8) (actual time=5.694..5.708 rows=89 loops=1)
                                                  Filter: (NOT (ANY (id = (hashed SubPlan 7).col1)))
                                                  Rows Removed by Filter: 1
                                                  Buffers: shared hit=7
                                                  SubPlan 7
                                                    ->  Merge Anti Join  (cost=3.64..219.76 rows=1 width=8) (actual time=5.500..5.506 rows=1 loops=1)
                                                          Merge Cond: (v0_3.id = u0_7.policy_run_id)
                                                          Buffers: shared hit=5
                                                          ->  Sort  (cost=3.36..3.36 rows=1 width=8) (actual time=5.487..5.488 rows=1 loops=1)
                                                                Sort Key: v0_3.id
                                                                Sort Method: quicksort  Memory: 25kB
                                                                Buffers: shared hit=2
                                                                ->  Seq Scan on policy_runs v0_3  (cost=0.00..3.35 rows=1 width=8) (actual time=5.480..5.483 rows=1 loops=1)
                                                                      Filter: ((finished_at < '2026-06-16 12:00:00+00'::timestamp with time zone) OR ((finished_at IS NULL) AND (started_at < '2026-06-16 12:00:00+00'::timestamp with time zone)))
                                                                      Rows Removed by Filter: 89
                                                                      Buffers: shared hit=2
                                                          ->  Index Only Scan using package_health_policy_run_id_d0e7bf06 on package_health u0_7  (cost=0.29..190.28 rows=10000 width=8) (actual time=0.008..0.008 rows=1 loops=1)
                                                                Heap Fetches: 0
                                                                Buffers: shared hit=3
                                ->  Hash  (cost=29485.43..29485.43 rows=20140 width=37) (actual time=596.064..596.076 rows=20000 loops=1)
                                      Buckets: 32768  Batches: 1  Memory Usage: 1663kB
                                      Buffers: shared hit=8370 read=15911
                                      ->  Hash Right Anti Join  (cost=1312.43..29485.43 rows=20140 width=37) (actual time=594.222..595.031 rows=20000 loops=1)
                                            Hash Cond: (x0_1.vulnerability_finding_id = vulnerability_findings.id)
                                            Buffers: shared hit=8370 read=15911
                                            ->  Hash Join  (cost=223.45..27215.20 rows=450000 width=8) (actual time=8.190..138.216 rows=890000 loops=1)
                                                  Hash Cond: (x0_1.policy_run_id = w0_2.id)
                                                  Buffers: shared hit=3 read=15522
                                                  ->  Seq Scan on package_remediation x0_1  (cost=0.00..24518.00 rows=900000 width=16) (actual time=0.024..58.823 rows=900000 loops=1)
                                                        Buffers: shared read=15518
                                                  ->  Hash  (cost=222.89..222.89 rows=45 width=8) (actual time=6.406..6.413 rows=89 loops=1)
                                                        Buckets: 1024  Batches: 1  Memory Usage: 12kB
                                                        Buffers: shared hit=3 read=4
                                                        ->  Seq Scan on policy_runs w0_2  (cost=219.77..222.89 rows=45 width=8) (actual time=6.394..6.406 rows=89 loops=1)
                                                              Filter: (NOT (ANY (id = (hashed SubPlan 8).col1)))
                                                              Rows Removed by Filter: 1
                                                              Buffers: shared hit=3 read=4
                                                              SubPlan 8
                                                                ->  Merge Anti Join  (cost=3.64..219.76 rows=1 width=8) (actual time=5.941..5.945 rows=1 loops=1)
                                                                      Merge Cond: (v0_4.id = u0_8.policy_run_id)
                                                                      Buffers: shared hit=3 read=2
                                                                      ->  Sort  (cost=3.36..3.36 rows=1 width=8) (actual time=5.925..5.925 rows=1 loops=1)
                                                                            Sort Key: v0_4.id
                                                                            Sort Method: quicksort  Memory: 25kB
                                                                            Buffers: shared hit=1 read=1
                                                                            ->  Seq Scan on policy_runs v0_4  (cost=0.00..3.35 rows=1 width=8) (actual time=5.907..5.919 rows=1 loops=1)
                                                                                  Filter: ((finished_at < '2026-06-16 12:00:00+00'::timestamp with time zone) OR ((finished_at IS NULL) AND (started_at < '2026-06-16 12:00:00+00'::timestamp with time zone)))
                                                                                  Rows Removed by Filter: 89
                                                                                  Buffers: shared hit=1 read=1
                                                                      ->  Index Only Scan using package_health_policy_run_id_d0e7bf06 on package_health u0_8  (cost=0.29..190.28 rows=10000 width=8) (actual time=0.012..0.012 rows=1 loops=1)
                                                                            Heap Fetches: 0
                                                                            Buffers: shared hit=2 read=1
                                            ->  Hash  (cost=837.23..837.23 rows=20140 width=37) (actual time=436.661..436.662 rows=20000 loops=1)
                                                  Buckets: 32768  Batches: 1  Memory Usage: 1663kB
                                                  Buffers: shared hit=8367 read=389
                                                  ->  Index Scan using vuln_finding_observed on vulnerability_findings  (cost=0.43..837.23 rows=20140 width=37) (actual time=430.615..435.236 rows=20000 loops=1)
                                                        Index Cond: (observed_at < '2026-06-16 12:00:00+00'::timestamp with time zone)
                                                        Buffers: shared hit=8367 read=389
              ->  Index Scan using vuln_finding_pkg_observed on vulnerability_findings u0  (cost=0.43..9.62 rows=30 width=29) (actual time=0.005..0.005 rows=1 loops=19800)
                    Index Cond: ((package_id = vulnerability_findings.package_id) AND (observed_at > vulnerability_findings.observed_at))
                    Filter: ((vulnerability_findings.advisory_id)::text = (advisory_id)::text)
                    Rows Removed by Filter: 0
                    Buffers: shared hit=50831 read=28549
        ->  Materialize  (cost=219.77..223.12 rows=45 width=8) (actual time=0.000..0.003 rows=89 loops=19800)
              Buffers: shared hit=6 read=1
              ->  Seq Scan on policy_runs w0  (cost=219.77..222.89 rows=45 width=8) (actual time=8.845..8.857 rows=89 loops=1)
                    Filter: (NOT (ANY (id = (hashed SubPlan 1).col1)))
                    Rows Removed by Filter: 1
                    Buffers: shared hit=6 read=1
                    SubPlan 1
                      ->  Merge Anti Join  (cost=3.64..219.76 rows=1 width=8) (actual time=8.064..8.067 rows=1 loops=1)
                            Merge Cond: (v0.id = u0_1.policy_run_id)
                            Buffers: shared hit=4 read=1
                            ->  Sort  (cost=3.36..3.36 rows=1 width=8) (actual time=8.038..8.041 rows=1 loops=1)
                                  Sort Key: v0.id
                                  Sort Method: quicksort  Memory: 25kB
                                  Buffers: shared hit=2
                                  ->  Seq Scan on policy_runs v0  (cost=0.00..3.35 rows=1 width=8) (actual time=8.016..8.021 rows=1 loops=1)
                                        Filter: ((finished_at < '2026-06-16 12:00:00+00'::timestamp with time zone) OR ((finished_at IS NULL) AND (started_at < '2026-06-16 12:00:00+00'::timestamp with time zone)))
                                        Rows Removed by Filter: 89
                                        Buffers: shared hit=2
                            ->  Index Only Scan using package_health_policy_run_id_d0e7bf06 on package_health u0_1  (cost=0.29..190.28 rows=10000 width=8) (actual time=0.018..0.018 rows=1 loops=1)
                                  Heap Fetches: 0
                                  Buffers: shared hit=2 read=1
        SubPlan 2
          ->  Index Scan using vuln_finding_pkg_observed on vulnerability_findings u0_2  (cost=0.43..8.45 rows=1 width=0) (actual time=0.002..0.002 rows=1 loops=1762200)
                Index Cond: ((package_id = vulnerability_findings.package_id) AND (observed_at > vulnerability_findings.observed_at) AND (observed_at <= w0.evidence_cutoff))
                Filter: ((advisory_id)::text = (vulnerability_findings.advisory_id)::text)
                Rows Removed by Filter: 0
                Buffers: shared hit=6119019 read=952169
Planning:
  Buffers: shared hit=131 read=39
Planning Time: 1.569 ms
JIT:
  Functions: 464
  Options: Inlining true, Optimization true, Expressions true, Deforming true
  Timing: Generation 9.262 ms (Deform 3.889 ms), Inlining 105.475 ms, Optimization 438.638 ms, Emission 341.514 ms, Total 894.889 ms
Execution Time: 15635.661 ms

--- purge.dry-run count: source_release_snapshots
SQL: SELECT COUNT(*) AS "__count" FROM "source_release_snapshots" WHERE ("source_release_snapshots"."observed_at" < '2026-06-16 12:00:00+00:00'::timestamptz AND EXISTS(SELECT 1 AS "a" FROM "source_release_snapshots" U0 WHERE (U0."package_id" = ("source_release_snapshots"."package_id") AND U0."observed_at" > ("source_release_snapshots"."observed_at")) LIMIT 1) AND NOT EXISTS(SELECT 1 AS "a" FROM "policy_runs" W0 WHERE (NOT (W0."id" IN (SELECT V0."id" AS "pk" FROM "policy_runs" V0 WHERE ((V0."finished_at" < '2026-06-16 12:00:00+00:00'::timestamptz OR (V0."finished_at" IS NULL AND V0."started_at" < '2026-06-16 12:00:00+00:00'::timestamptz)) AND NOT EXISTS(SELECT 1 AS "a" FROM "package_health" U0 WHERE U0."policy_run_id" = (V0."id") LIMIT 1)))) AND W0."evidence_cutoff" >= ("source_release_snapshots"."observed_at") AND NOT EXISTS(SELECT 1 AS "a" FROM "source_release_snapshots" U0 WHERE (U0."package_id" = ("source_release_snapshots"."package_id") AND U0."observed_at" > ("source_release_snapshots"."observed_at") AND U0."observed_at" <= (W0."evidence_cutoff")) LIMIT 1)) LIMIT 1) AND NOT EXISTS(SELECT 1 AS "a" FROM "package_currency" X0 WHERE (X0."source_snapshot_id" = ("source_release_snapshots"."id") AND X0."policy_run_id" IN (SELECT W0."id" AS "pk" FROM "policy_runs" W0 WHERE NOT (W0."id" IN (SELECT V0."id" AS "pk" FROM "policy_runs" V0 WHERE ((V0."finished_at" < '2026-06-16 12:00:00+00:00'::timestamptz OR (V0."finished_at" IS NULL AND V0."started_at" < '2026-06-16 12:00:00+00:00'::timestamptz)) AND NOT EXISTS(SELECT 1 AS "a" FROM "package_health" U0 WHERE U0."policy_run_id" = (V0."id") LIMIT 1)))))) LIMIT 1) AND NOT EXISTS(SELECT 1 AS "a" FROM "package_remediation" X0 WHERE (X0."source_snapshot_id" = ("source_release_snapshots"."id") AND X0."policy_run_id" IN (SELECT W0."id" AS "pk" FROM "policy_runs" W0 WHERE NOT (W0."id" IN (SELECT V0."id" AS "pk" FROM "policy_runs" V0 WHERE ((V0."finished_at" < '2026-06-16 12:00:00+00:00'::timestamptz OR (V0."finished_at" IS NULL AND V0."started_at" < '2026-06-16 12:00:00+00:00'::timestamptz)) AND NOT EXISTS(SELECT 1 AS "a" FROM "package_health" U0 WHERE U0."policy_run_id" = (V0."id") LIMIT 1)))))) LIMIT 1))
Aggregate  (cost=606931.05..606931.06 rows=1 width=8) (actual time=1529.340..1529.350 rows=1 loops=1)
  Buffers: shared hit=2677772 read=30063
  ->  Nested Loop Anti Join  (cost=28544.68..606924.32 rows=2690 width=0) (actual time=510.798..1529.043 rows=9900 loops=1)
        Join Filter: ((w0.evidence_cutoff >= source_release_snapshots.observed_at) AND (NOT EXISTS(SubPlan 2)))
        Rows Removed by Join Filter: 881100
        Buffers: shared hit=2677772 read=30063
        ->  Nested Loop Semi Join  (cost=28324.92..61818.43 rows=3228 width=16) (actual time=504.949..529.751 rows=9900 loops=1)
              Buffers: shared hit=33522 read=30063
              ->  Hash Right Anti Join  (cost=28324.49..57003.75 rows=9683 width=16) (actual time=504.932..505.365 rows=10000 loops=1)
                    Hash Cond: (x0_1.source_snapshot_id = source_release_snapshots.id)
                    Buffers: shared hit=3521 read=30063
                    ->  Hash Join  (cost=223.45..27215.20 rows=450000 width=8) (actual time=7.071..129.088 rows=890000 loops=1)
                          Hash Cond: (x0_1.policy_run_id = w0_2.id)
                          Buffers: shared hit=39 read=15486
                          ->  Seq Scan on package_remediation x0_1  (cost=0.00..24518.00 rows=900000 width=16) (actual time=0.011..48.241 rows=900000 loops=1)
                                Buffers: shared hit=32 read=15486
                          ->  Hash  (cost=222.89..222.89 rows=45 width=8) (actual time=5.906..5.908 rows=89 loops=1)
                                Buckets: 1024  Batches: 1  Memory Usage: 12kB
                                Buffers: shared hit=7
                                ->  Seq Scan on policy_runs w0_2  (cost=219.77..222.89 rows=45 width=8) (actual time=5.893..5.900 rows=89 loops=1)
                                      Filter: (NOT (ANY (id = (hashed SubPlan 4).col1)))
                                      Rows Removed by Filter: 1
                                      Buffers: shared hit=7
                                      SubPlan 4
                                        ->  Merge Anti Join  (cost=3.64..219.76 rows=1 width=8) (actual time=5.699..5.700 rows=1 loops=1)
                                              Merge Cond: (v0_2.id = u0_4.policy_run_id)
                                              Buffers: shared hit=5
                                              ->  Sort  (cost=3.36..3.36 rows=1 width=8) (actual time=5.687..5.687 rows=1 loops=1)
                                                    Sort Key: v0_2.id
                                                    Sort Method: quicksort  Memory: 25kB
                                                    Buffers: shared hit=2
                                                    ->  Seq Scan on policy_runs v0_2  (cost=0.00..3.35 rows=1 width=8) (actual time=5.679..5.682 rows=1 loops=1)
                                                          Filter: ((finished_at < '2026-06-16 12:00:00+00'::timestamp with time zone) OR ((finished_at IS NULL) AND (started_at < '2026-06-16 12:00:00+00'::timestamp with time zone)))
                                                          Rows Removed by Filter: 89
                                                          Buffers: shared hit=2
                                              ->  Index Only Scan using package_health_policy_run_id_d0e7bf06 on package_health u0_4  (cost=0.29..190.28 rows=10000 width=8) (actual time=0.008..0.008 rows=1 loops=1)
                                                    Heap Fetches: 0
                                                    Buffers: shared hit=3
                    ->  Hash  (cost=27980.00..27980.00 rows=9683 width=24) (actual time=355.236..355.240 rows=10000 loops=1)
                          Buckets: 16384  Batches: 1  Memory Usage: 675kB
                          Buffers: shared hit=3482 read=14577
                          ->  Hash Right Anti Join  (cost=716.00..27980.00 rows=9683 width=24) (actual time=354.371..354.769 rows=10000 loops=1)
                                Hash Cond: (x0.source_snapshot_id = source_release_snapshots.id)
                                Buffers: shared hit=3482 read=14577
                                ->  Hash Join  (cost=223.45..26306.20 rows=450000 width=8) (actual time=6.913..140.258 rows=890000 loops=1)
                                      Hash Cond: (x0.policy_run_id = w0_1.id)
                                      Buffers: shared hit=39 read=14577
                                      ->  Seq Scan on package_currency x0  (cost=0.00..23609.00 rows=900000 width=16) (actual time=0.016..46.398 rows=900000 loops=1)
                                            Buffers: shared hit=32 read=14577
                                      ->  Hash  (cost=222.89..222.89 rows=45 width=8) (actual time=5.600..5.602 rows=89 loops=1)
                                            Buckets: 1024  Batches: 1  Memory Usage: 12kB
                                            Buffers: shared hit=7
                                            ->  Seq Scan on policy_runs w0_1  (cost=219.77..222.89 rows=45 width=8) (actual time=5.588..5.596 rows=89 loops=1)
                                                  Filter: (NOT (ANY (id = (hashed SubPlan 3).col1)))
                                                  Rows Removed by Filter: 1
                                                  Buffers: shared hit=7
                                                  SubPlan 3
                                                    ->  Merge Anti Join  (cost=3.64..219.76 rows=1 width=8) (actual time=5.474..5.476 rows=1 loops=1)
                                                          Merge Cond: (v0_1.id = u0_3.policy_run_id)
                                                          Buffers: shared hit=5
                                                          ->  Sort  (cost=3.36..3.36 rows=1 width=8) (actual time=5.461..5.462 rows=1 loops=1)
                                                                Sort Key: v0_1.id
                                                                Sort Method: quicksort  Memory: 25kB
                                                                Buffers: shared hit=2
                                                                ->  Seq Scan on policy_runs v0_1  (cost=0.00..3.35 rows=1 width=8) (actual time=5.454..5.457 rows=1 loops=1)
                                                                      Filter: ((finished_at < '2026-06-16 12:00:00+00'::timestamp with time zone) OR ((finished_at IS NULL) AND (started_at < '2026-06-16 12:00:00+00'::timestamp with time zone)))
                                                                      Rows Removed by Filter: 89
                                                                      Buffers: shared hit=2
                                                          ->  Index Only Scan using package_health_policy_run_id_d0e7bf06 on package_health u0_3  (cost=0.29..190.28 rows=10000 width=8) (actual time=0.008..0.008 rows=1 loops=1)
                                                                Heap Fetches: 0
                                                                Buffers: shared hit=3
                                ->  Hash  (cost=371.51..371.51 rows=9683 width=24) (actual time=194.848..194.848 rows=10000 loops=1)
                                      Buckets: 16384  Batches: 1  Memory Usage: 675kB
                                      Buffers: shared hit=3443
                                      ->  Index Scan using src_release_observed on source_release_snapshots  (cost=0.42..371.51 rows=9683 width=24) (actual time=192.811..194.284 rows=10000 loops=1)
                                            Index Cond: (observed_at < '2026-06-16 12:00:00+00'::timestamp with time zone)
                                            Buffers: shared hit=3443
              ->  Index Only Scan using src_release_pkg_observed on source_release_snapshots u0  (cost=0.42..2.81 rows=30 width=16) (actual time=0.002..0.002 rows=1 loops=10000)
                    Index Cond: ((package_id = source_release_snapshots.package_id) AND (observed_at > source_release_snapshots.observed_at))
                    Heap Fetches: 0
                    Buffers: shared hit=30001
        ->  Materialize  (cost=219.77..223.12 rows=45 width=8) (actual time=0.001..0.003 rows=89 loops=9900)
              Buffers: shared hit=7
              ->  Seq Scan on policy_runs w0  (cost=219.77..222.89 rows=45 width=8) (actual time=5.718..5.728 rows=89 loops=1)
                    Filter: (NOT (ANY (id = (hashed SubPlan 1).col1)))
                    Rows Removed by Filter: 1
                    Buffers: shared hit=7
                    SubPlan 1
                      ->  Merge Anti Join  (cost=3.64..219.76 rows=1 width=8) (actual time=5.531..5.533 rows=1 loops=1)
                            Merge Cond: (v0.id = u0_1.policy_run_id)
                            Buffers: shared hit=5
                            ->  Sort  (cost=3.36..3.36 rows=1 width=8) (actual time=5.519..5.519 rows=1 loops=1)
                                  Sort Key: v0.id
                                  Sort Method: quicksort  Memory: 25kB
                                  Buffers: shared hit=2
                                  ->  Seq Scan on policy_runs v0  (cost=0.00..3.35 rows=1 width=8) (actual time=5.511..5.514 rows=1 loops=1)
                                        Filter: ((finished_at < '2026-06-16 12:00:00+00'::timestamp with time zone) OR ((finished_at IS NULL) AND (started_at < '2026-06-16 12:00:00+00'::timestamp with time zone)))
                                        Rows Removed by Filter: 89
                                        Buffers: shared hit=2
                            ->  Index Only Scan using package_health_policy_run_id_d0e7bf06 on package_health u0_1  (cost=0.29..190.28 rows=10000 width=8) (actual time=0.008..0.008 rows=1 loops=1)
                                  Heap Fetches: 0
                                  Buffers: shared hit=3
        SubPlan 2
          ->  Index Only Scan using src_release_pkg_observed on source_release_snapshots u0_2  (cost=0.42..4.45 rows=1 width=0) (actual time=0.001..0.001 rows=1 loops=881100)
                Index Cond: ((package_id = source_release_snapshots.package_id) AND (observed_at > source_release_snapshots.observed_at) AND (observed_at <= w0.evidence_cutoff))
                Heap Fetches: 0
                Buffers: shared hit=2644243
Planning:
  Buffers: shared hit=42
Planning Time: 0.455 ms
JIT:
  Functions: 122
  Options: Inlining true, Optimization true, Expressions true, Deforming true
  Timing: Generation 2.180 ms (Deform 0.987 ms), Inlining 7.258 ms, Optimization 119.546 ms, Emission 82.706 ms, Total 211.690 ms
Execution Time: 1531.379 ms

--- purge.purge_evidence end to end
SQL: (not a statement: purge_evidence(clock=FixedClock(NOW), retention_days=90))
package_currency: 0.05 s
package_feedstock_presence: 0.05 s
package_vulnerability: 0.00 s
package_license: 0.06 s
package_remediation: 0.08 s
package_python_readiness: 0.09 s
package_priority: 0.12 s
package_work_type: 0.13 s
policy_runs: 0.02 s
inventory_snapshots: 4.21 s
source_release_snapshots: 2.86 s
pypi_release_snapshots: 2.97 s
feedstock_snapshots: 10.56 s
conda_package_snapshots: 7.36 s
kev_findings: 17.51 s
vulnerability_findings: 21.43 s
license_findings: 4.20 s
python_readiness_assessments: 4.55 s
python_verification_results: 4.98 s
identity_resolution_snapshots: 1.62 s
collection_runs: 1.74 s
(end to end): 84.96 s
```

### The one index this story adds, before and after

`page.recent_runs` -- the package page's newest twenty runs, `ORDER BY started_at DESC, id
DESC LIMIT 20` -- **before** `collection_runs_pkg_started`, from the earlier run at the same
scale and seed:

```text
--- page.recent_runs
SQL: SELECT "collection_runs"."id", "collection_runs"."started_at", "collection_runs"."finished_at", "collection_runs"."status", "collection_runs"."trace_id", "collection_runs"."detail", "collection_runs"."collector", "collection_runs"."package_id" FROM "collection_runs" WHERE "collection_runs"."package_id" = 5001 ORDER BY "collection_runs"."started_at" DESC, "collection_runs"."id" DESC LIMIT 20
Limit  (cost=3068.05..3068.10 rows=20 width=56) (actual time=0.158..0.160 rows=20 loops=1)
  Buffers: shared hit=102
  ->  Sort  (cost=3068.05..3070.10 rows=817 width=56) (actual time=0.158..0.159 rows=20 loops=1)
        Sort Key: started_at DESC, id DESC
        Sort Method: top-N heapsort  Memory: 30kB
        Buffers: shared hit=102
        ->  Bitmap Heap Scan on collection_runs  (cost=14.76..3046.31 rows=817 width=56) (actual time=0.021..0.082 rows=819 loops=1)
              Recheck Cond: (package_id = 5001)
              Heap Blocks: exact=99
              Buffers: shared hit=102
              ->  Bitmap Index Scan on collection_runs_package_id_eafda4fb  (cost=0.00..14.56 rows=817 width=0) (actual time=0.013..0.013 rows=819 loops=1)
                    Index Cond: (package_id = 5001)
                    Buffers: shared hit=3
Planning Time: 0.016 ms
Execution Time: 0.163 ms
```

The foreign key's own index found the package's 819 ledger rows and the plan read all 99 heap
pages they lie on -- one per day, because a ledger written day by day scatters a package's
rows -- to keep twenty: `shared hit=102`, 0.163 ms. **After** (the recorded run, in the
plans above): `Index Scan using collection_runs_pkg_started`, twenty-eight index entries,
`shared hit=7`, 0.014 ms. The read is served by the index it now has; the cost was never
large, and a read that grows with the retention and the collector count is what the
per-package buffer ceiling exists to catch. Declared as `COLLECTION_RUN_PACKAGE_INDEX` on
`CollectionRun`, added by `core/0014_collection_run_package_index`.

### What the plans exposed

- **Every per-package read is one index probe, and stays under the ceiling.** The forty-eight
  statements a policy run issues for one package (the eleven `snapshot_as_of` shapes, the
  inventory reader, the four ranked currency surfaces, the feedstock reader, the two
  `(package, python_series)` reads, the four two-statement sweep readers, the four
  remediation surfaces, the eleven `latest_observation` aggregates, the cut-off choice's
  two) execute in 0.005-0.029 ms on `shared hit=4` or `5`, all on the `*_pkg_observed`
  `(package, -observed_at)` index; the spike asserts every per-package read at or under 64
  shared buffers. The rollup's read budget, as printed: 0.391 ms of execution for one
  package, `x 10,000 packages = 3.9 s of database execution` per run -- `CPM-AD-23`'s N+1 by
  design, and the reads are not the cost of a run.
- **Composite key indexes: dismissed.** The `(package, python_series)` reads use
  `py_readiness_pkg_observed`/`py_verify_pkg_observed` on `shared hit=5` in 0.008 ms; the
  advisory, channel and source-key readers take the sweep at one instant from the same
  index. The purge's per-row `SubPlan 2` probe on the multi-key tables is an `Index Only
  Scan` on the same index. An index the plan would only *not need* is not one the plan
  demands; none is added.
- **`collection_runs (package, -started_at)`: confirmed by the plan and added** (above).
  `runs_in_flight` reads backward on `collection_runs_started` in 0.004 ms on
  `shared hit=3` and needs nothing.
- **The cut-off choice**, through `choose_evidence_cutoff()`: two statements on the ledger's
  `started_at`/`finished_at` indexes (0.007 and 0.005 ms) on the success path; on the
  refusal path -- reached by opening a run started before every ending -- the same two and
  the count the `PolicyRunError` is worded with, which the planner answered from
  `collection_runs_finished` in 0.004 ms because the boundary bounded it to nothing. Named
  in `WHOLE_TABLE_BY_NATURE` because it is a count of every usable ending, whatever the
  boundary leaves.
- **The package page** issues 26 statements for `traces_for` (eight derived reads on the
  `(package, policy_run)` unique index at 900,000 rows per derived table, six history
  slices of `[:20]`, six counts), none above 0.033 ms or `shared hit=24`; the listing's
  first page is 0.147 ms and its count 0.616 ms over 10,000 rollup rows.
- **The coverage screen's ledger group-by** scans the whole ledger (438.641 ms, `shared
  hit=118 read=88048`) -- the one sequential scan the story admitted in advance, a
  per-collector aggregate over every run there is.
- **The digest's freshness figures**, through `collectors.digest._freshness` per collector:
  the selection `selected_package_ids` reads (0.010-1.274 ms, or none where the collector's
  selection was already a list), the "ever observed" `DISTINCT package_id` as an `Index Only
  Scan` or a parallel index-only `HashAggregate` (26.755-73.119 ms on `shared hit=905` to
  `1743`), and the "within target" set as an index range on `*_observed` (5.067-59.796 ms,
  the fourteen-day windows costing the most at `shared hit=140531` for
  `python_readiness_assessments`). No sequential scan: these are index-only reads, and the
  spike's guard now covers them, so a regression to a heap scan fails. `kev` selects no
  package here (no KEV source is declared) and the digest asks nothing about it.
- **The purge's selections, as the plans show them.** `_older` is an index-range count on
  `*_observed` (0.529-1.177 ms) -- the only product query the bare `observed_at` index serves
  at the cut-off; the batch selections do not use it. Each batch selection is the keyset walk
  `_batches` chooses -- `Index Scan using <table>_pkey`, `id > watermark`, the cut-off as a
  `Filter` -- which on a day-major table meets the oldest day first (`rows=1010` read for the
  1,000 selected: the ten absent packages' newest rows are skipped by the semi join); then a
  `Nested Loop Semi Join` probing `*_pkg_observed` once per candidate for a strictly newer
  row; then a `Nested Loop Anti Join` over the 89 surviving runs, read once from
  `policy_runs` (`Seq Scan` + `Materialize`, ninety rows) and joined to every candidate
  (`Rows Removed by Join Filter: 89000`), with `SubPlan 2` -- the "superseded before that
  run's cut-off" probe on `*_pkg_observed` -- executed `loops=89000`. **The per-batch cost is
  therefore fixed by the batch size times the surviving runs: 89,000 index probes per 1,000
  rows, whatever the table holds**, 109.010-151.752 ms per table, 279.064 ms for
  `conda_package_snapshots`, 482.967 ms for `kev_findings` (its key crosses a join: each probe is
  a `kev_findings` index scan plus a `vulnerability_findings_pkey` lookup). The walk's cost
  depends on insertion order -- an older day that is not lowest in the primary key is
  filtered past, one page at a time -- which is a property of `_batches`' choice of keyset
  and is recorded as deferred work, not changed.
- **`removable_evidence` hashes its citers, and the plans say so.** The full selection adds
  one `NOT EXISTS` per citing relation. For the three tables the derived rows cite through a
  nullable foreign key (`source_release_snapshots`, `pypi_release_snapshots`,
  `feedstock_snapshots`), the planner hashes the citing derived table's rows for every
  surviving run -- `Parallel Seq Scan on package_currency`, `package_remediation`,
  `package_feedstock_presence`, 900,000 rows each -- rather than probing the foreign key's
  own index 1,000 times: 387.720, 297.728 and 1747.635 ms for the first batch against
  110-111 ms for the floor rule alone. For `kev_findings` the citing side is
  `package_vulnerability` (hashed) plus its own floor rule's joined key: 1049.440 ms
  (`temp read=29078 written=29348`: the hash of `kev LEFT JOIN vuln` spills `work_mem`). For
  `vulnerability_findings` the citing side is kev's floor rule *and* `package_vulnerability`
  and `package_remediation`: `Parallel Seq Scan` on `kev_findings` and
  `vulnerability_findings` (1,802,000 rows each) hashed, 11245.349 ms and
  `shared hit=15083741 read=3129288` for the first batch's selection; and `prune_evidence
  --dry-run`'s unbatched `count()` of that selection is 15635.661 ms (`shared hit=20911070
  read=4153997`), against 1531.379 ms for `source_release_snapshots`. Every one of these is
  a planner choice at this ratio -- the candidates are one per cent of the citing table, the
  foreign-key index exists and is declined -- or a key no index can carry (kev's
  `vulnerability_finding__advisory_id`). Neither is an index this story can add; both are
  recorded as deferred work, and the spike's guard admits sequential scans on the *citing*
  side of these selections only, so a scan of the selected table itself, or of the ledger,
  still fails.
- **What that costs the night, from the purge's own clock:** `vulnerability_findings` 21.43 s,
  `kev_findings` 17.51 s, `feedstock_snapshots` 10.56 s, `conda_package_snapshots` 7.36 s, the
  other seven roster tables 1.62-4.98 s, the derived tables 0.05-0.13 s each (0.00 s for
  `package_vulnerability`, whose purgeable rows went before), `collection_runs` 1.74 s; **84.96 s end to end**. One batch's `DELETE` on its own, through
  `_remove_batch`: 0.039 s (`kev_findings`), 0.049 s (`vulnerability_findings`), 0.044 s
  (`source_release_snapshots`) -- the deletes are cheap; the selections are the purge.
- **The keep path ran.** Every roster table records `kept_by_rule=100` or `200`: the absent
  packages' day-0 rows, their newest, older than the cut-off and kept, asserted per table
  along with `protected=0`.
- **`--batch 250` and a lowered retention, from the fixed per-batch cost.** A batch of 250
  costs the same 89 probes per candidate but a quarter of the candidates, so a table's
  selection cost is unchanged per row and the walk restarts four times as often: the
  hashed-citer selections (a fixed hash per batch) cost four times more per table.
  Lowering `CPM_EVIDENCE_RETENTION_DAYS` by *N* days makes the next purge remove *N* days'
  rows: *N* times the one-day figures above, in the same batches, and the floor rule's
  "surviving runs" set shrinks by *N* as it goes.

## Verdict

**The rule, stated before the numbers (Boundaries):** the nightly window is one hour;
partitioning is not adopted when the end-to-end steady-state purge finishes inside that hour
with at least a 4× margin -- fifteen minutes -- **and** no measured plan sequentially scans an
evidence table after this story's indexes; otherwise a §9 is appended to the change proposal.

**Clause one is met.** One day's rows on every roster table (9,900 or 19,800 per table after
the absent packages' kept rows), one policy run and its 10,000 rows on each derived table,
90,018 ledger rows, plus the 3,000 rows and the 10,000 derived rows removed batch-by-batch
before it: **84.96 s** end to end against a 3,600 s window and the 900 s a 4× margin leaves.

**Clause two is not met as worded.** Two evidence tables are sequentially scanned by a
product query: `kev_findings` and `vulnerability_findings`, as the hashed citing side of the
`vulnerability_findings` and `kev_findings` selections and of `prune_evidence --dry-run`'s
count. Three derived tables are scanned the same way as the citing side of three other
selections. The ledger's two sequential scans (the coverage group-by, admitted in advance;
the cut-off choice's refusal-path count) ask about every row. The guard that fails on a
sequential scan was written before the first run and failed on it; the allowance for the
citing side was added afterwards, and this section says so rather than presenting it as
pre-admitted.

**The decision, and the clause it rests on.** The Boundaries' "no measured plan contains
`Seq Scan on <evidence table>` **except where the story records why**" and the Block-If's "a
plan can be fixed only by changing a product query's semantics (record it as deferred
instead)" are the clauses applied: the two evidence-table scans arise from kev's roster key
crossing a join, and the derived-table scans from the planner declining an index that
exists at a one-per-cent ratio; the first is a change to `CPM-OPERATE-S07`'s key, the second
is not an index at all. Both are recorded in `deferred-work.md` with the numbers above.
With that recorded, the purge is inside the hour with more than the margin required, the
one index a plan demanded is added and measured, and **monthly range partitioning by
`observed_at` is not adopted.** Nothing is appended to `sprint-change-proposal-2026-09-13.md`;
`CPM-AD-2`'s door bullet stands as written. What would re-open the question is recorded in
`operations.md` ("Measuring at scale"): a retention that lets `policy_runs` grow past a few
hundred rows (every candidate row is joined to every surviving run), the citer hashes
growing with the derived tables faster than the night allows, or `CPM-NFR-1`'s scale moving
-- each is `pixi run spike-scale` again, not a number carried forward.

## Spec Change Log

- 2026-09-14, implementation (patch, not a re-derivation): the vendor refusal is an
  `assert connection.vendor == "postgresql"` carrying the message the Boundaries asked
  `pytest.fail` to carry, because `tests/unit/test_suite_policy.py` bans
  `if connection.vendor ...` in every module under `tests/` (the shape of a dodged gate
  failure) and names the assertion as the sanctioned opposite; the major-version refusal
  stays `pytest.fail`. Also on the way: `VACUUM (ANALYZE)` rather than bare `ANALYZE`
  after seeding, said in the spike, because a bulk-loaded table has no visibility map and
  the planner could never choose the index-only scans a live table gets; `policy_runs` is
  not among the tables the seq-scan assertion guards (ninety rows, bounded by the
  retention, the planner is right to read them without an index); and the measuring
  tests carry the plain `django_db` marker because pytest-django orders unmarked tests
  after transactional ones, which would have run them after the purge's teardown flush.
  KEEP: everything else in the Boundaries as written.
- 2026-09-14, review loop 1 (patch): the seed is day-major with every policy run's derived
  rows and one package in a hundred absent after day 0, so the purge's keep path runs and is
  asserted; the measurements go through the product's own calls (`choose_evidence_cutoff()`
  on both paths, `digest._freshness`, `retention._batches` for the first batch) and add the
  dry-run count, one batch's own `DELETE`, a per-package buffer ceiling, a statement
  timeout and a purge deadline; the derived tables join the guarded set and the digest's
  whole-table reads leave the allowance (they are index-only scans); the sequential scans
  the plans do show are recorded by *shape* (the citing side of `removable_evidence`'s
  anti-join) and the verdict says clause two is not met as worded; `collection_runs
  (package, -started_at)` is added on a measured before/after plan; the seeder is smoke-run
  by the gate; the script traps INT/TERM, refuses a running container, checks `docker run`
  and tees to `.spike-runs/`. KEEP: the verdict rule, the roster keys, the purge's semantics.

## Review Triage Log

### 2026-09-14 — Review pass
- intent_gap: 0
- bad_spec: 0
- patch: 17: (high 7, medium 7, low 3)
- defer: 3: (high 0, medium 2, low 1)
- reject: 2
- addressed_findings:
  - `[high]` `[patch]` batch-selection prose and docs rewritten to the recorded plans; the fixed per-batch probe cost recorded
  - `[high]` `[patch]` derived tables seeded at steady state (every run) and guarded; their scans recorded
  - `[high]` `[patch]` verdict says clause two is not met as worded and how the decision stands
  - `[high]` `[patch]` sequential-scan miscount corrected; digest reads leave the allowance
  - `[high]` `[patch]` every number from the recorded run; environment recorded; instrumentation caveat
  - `[high]` `[patch]` measurements through the product's own calls (`choose_evidence_cutoff`, `snapshot_as_of`, digest, `_batches`); one batch's DELETE timed
  - `[high]` `[patch]` `collection_runs (package, -started_at)` index on a measured before/after plan
  - `[medium]` `[patch]` day-major seed, biases stated; absent packages so the keep path runs
  - `[medium]` `[patch]` dry-run count, `--batch 250` arithmetic, lowered-retention cost measured or stated
  - `[medium]` `[patch]` buffer ceiling, parse-miss failure, statement timeout, purge deadline
  - `[medium]` `[patch]` seed preconditions and citation checks; derived list reconciled with `derived_tables()`
  - `[medium]` `[patch]` gate-side seeder smoke at 3×2 on PostgreSQL
  - `[medium]` `[patch]` script: INT/TERM trap, running-container refusal, docker run check, tee to `.spike-runs/`
  - `[medium]` `[patch]` `operations.md` stale daily-growth estimate replaced by the measured figure
  - `[low]` `[patch]` gate-contract test pins `pytestmark`, `-s`, `-m`; stale comments; docstring counts

## Design Notes

**Why SQL seeding.** Nine million append-only rows through the ORM is hours; `generate_series`
is seconds per table. The CheckConstraints are the contract the seeder must honour, so the seed
is realistic in the columns the readers touch.

**Why the plans are asserted, not the timings.** A timing depends on the laptop; a Seq Scan on
an evidence table is a defect on any machine. The spike prints timings for the story and fails
only on plan shape and on what the purge kept.

**Why the rollup stays N+1.** `CPM-AD-23` makes one transaction per package; the spike measures
one package's reads and states the multiplied budget. Making the reads set-based is recorded
deferred work already (`operations.md:2152-2157`).

## Verification

**Commands:**
- `pixi run spike-scale` -- green on postgres:17; plans and timings printed and copied to `.spike-runs/`; pasted into `## Measurement`.
- `pixi run test`; `pixi run -e dev python -m pytest tests/unit/test_gate_contract.py tests/unit/django_apps/test_migration_completeness.py tests/unit/django_apps/test_retention.py tests/unit/django_apps/test_evidence_constraint_audit.py tests/unit/test_documentation_commands.py tests/unit/django_apps/test_documentation_accuracy.py tests/integration/django_apps/test_evidence_scale_seed.py -q` -- green (the seeder smoke on sqlite asserts the refusal; `pixi run gate-postgres` runs it on PostgreSQL).
- `pixi run ci` (stack down) -- exit 0.

## Auto Run Result

- **Summary:** `pixi run spike-scale` seeds a throwaway PostgreSQL 17 to 10,000 packages × 91 days
  on every roster table (16.2 M evidence, 8.1 M ledger, 7.2 M derived rows), measures the rollup's
  reads, the package page, the coverage and digest aggregates and the purge with
  `EXPLAIN (ANALYZE, BUFFERS)`, asserts plan shape and what the purge kept, and records the
  verdict: partitioning **not adopted** (steady-state nightly purge 84.96 s against a one-hour
  window with a 4× margin); one index added on a measured plan (`collection_runs (package,
  -started_at)`, `core/0014`).
- **Files:** `scripts/spike-scale.sh`, `tests/spikes/spike_evidence_scale.py`, `pixi.toml`
  (`spike-scale`), `pyproject.toml` (marker text), `.gitignore` (`.spike-runs/`),
  `core/models.py` + `core/migrations/0014_collection_run_package_index.py`,
  `tests/unit/test_gate_contract.py`, `tests/integration/django_apps/test_evidence_scale_seed.py`,
  retention tests reconciled, `docs/conda-sentinel/operations.md` (purge numbers, verdict,
  "Measuring at scale"), `docs/accelerator/development.md`, `deferred-work.md`, this story's
  `## Measurement` and `## Verdict`, `sprint-status.yaml`.
- **Review:** 17 patched (high 7, medium 7, low 3), 3 deferred, 2 rejected. Follow-up review
  recommended: **true** (high-severity patches; score 3×7 + 3 = 24).
- **Verification:** `pixi run ci` exit 0 on 2026-09-14 (foreground, stack down): 11729 passed,
  2 skipped, coverage 99.08%. `pixi run spike-scale`: 9 passed in 447 s on postgres:17, container
  removed, log at `.spike-runs/20260914T100410Z.log`. Named modules and audits: 1510 passed.
- **Residual risks:** the measurement is one laptop and one PostgreSQL image's defaults; the
  purge's `protected` (citation) path is not exercised at scale; the plan-shape assertions run
  only on demand.

## Suggested Review Order

1. `## Verdict` and `## Measurement`'s summary and purge tables (the numbers the docs carry).
2. `tests/spikes/spike_evidence_scale.py` -- seed SQL against the CheckConstraints, the guarded set and allowances, the assertions.
3. `core/models.py` `COLLECTION_RUN_PACKAGE_INDEX` + `core/migrations/0014`; before/after plans for `page.recent_runs`.
4. `scripts/spike-scale.sh`, `pixi.toml`, `tests/unit/test_gate_contract.py`, `tests/integration/django_apps/test_evidence_scale_seed.py`.
5. `docs/conda-sentinel/operations.md` purge section and "Measuring at scale"; `deferred-work.md` entries.
