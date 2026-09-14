---
title: 'CPM-OPERATE-S07: Ninety days of evidence, purged nightly'
type: 'feature'
created: '2026-09-13'
status: 'done'
review_loop_iteration: 0
followup_review_recommended: false
warnings: [oversized]
deferred:
  - 'A non-integer CPM_EVIDENCE_RETENTION_DAYS in the environment is refused at settings import by django-environ with a ValueError naming the value, not the setting; the hook names the setting only for a parsed value below one or a hand-assigned wrong type (deferred-work.md)'
  - 'The purge floor protects what a policy run read; the observation-window check reads collection_runs and a retention shorter than a declared window would purge the row it reads -- harmless (collect again), unrefused at boot (deferred-work.md)'
baseline_revision: '916ee38fe3db5764333d50aa7f6345896ad52669'
context:
  - _bmad-output/implementation-artifacts/epic-operate-context.md
  - _bmad-output/implementation-artifacts/stories/cpm-operate-s02-ingest-sweep-and-run-are-commands.md
---

<intent-contract>

## Intent

**Problem:** Evidence is append-only and nothing has ever pruned any table. At ten thousand
packages the daily tables grow ~70k rows a day each and the collection ledger the same, with
no time-leading index on any of them; the product owner's decision is ninety days, purged
nightly.

**Approach:** A declared retention, `CPM_EVIDENCE_RETENTION_DAYS` (default 90, refused below
1 -- there is no "forever" except by a number), and a `prune_evidence` admin process the
deployment schedules nightly. It deletes, in bounded batches through one audited door the
append-only base exposes for it alone, evidence rows and run-ledger rows older than the
retention -- never a package's newest row per table, and never the row a retained policy
run read at its cut-off -- writing a run record per table naming the cut-off and the count.
A replay reproduces inside the window and is refused with a reason outside it. Every
cut-off scan has an index that leads with the time column.

Two deviations from the epic's letter, recorded: retention is a setting, not a versioned
policy parameter (parameters are verdict rule data under `CPM-AD-8`; a per-deployment
duration belongs with the other declared `CPM_*` settings, and "which version's retention"
is unanswerable for a replay of an older run); and the two human-audit tables
(`identity_overrides`, `inventory_changes`) are excluded from the purge -- one row per
human act, the audit trail of governed writes -- which the product owner may reverse.

## Boundaries & Constraints

**Always:**
- Setting: `base.py` `CPM_EVIDENCE_RETENTION_DAYS = env.int("CPM_EVIDENCE_RETENTION_DAYS", default=90)`;
  the name a constant in `core/retention.py`; `CollectorsConfig.ready()` (or the core app's)
  refuses `< 1` or a non-integer at boot naming the setting.
- **The door.** `core/models.py`: `AppendOnlyQuerySet` gains one method, `retire(*, door)`, that
  performs the delete only when handed the `RetentionDoor` token `core/retention.py` alone
  constructs (a module-private class; the method refuses anything else with
  `AppendOnlyError`); it delegates to the parent queryset's raw delete. `delete()`, `update()`,
  `_raw_delete()`, `bulk_update()` and the instance `delete()` keep refusing exactly as today
  (every existing case in `test_append_only_model.py` unchanged). The mutation-path audit
  licenses the door with a **counted** `RECORDED_EXEMPTIONS` entry for `core/models.py` and
  one for `core/retention.py` (its derived-row `objects.delete(...)`), with a comment in the
  audit naming this story; no other module gains an exemption.
- **What is purged, in this order, each inside the run recorder and each batch in its own
  `transaction.atomic()` (never around the recorder):**
  1. Derived pass rows (`policies` models) of `PolicyRun`s whose `finished_at` is older than
     the retention **and** that no `package_health` row cites (the rollup is never purged
     and pins its run); then those `PolicyRun` rows.
  2. Evidence rows, per table in the roster below, with `observed_at < cut-off`, excluding:
     the newest row per package (per `(package, advisory)` for `vulnerability_findings` and
     `kev_findings`; per `(package, channel, platform)` for `conda_package_snapshots`; per
     `(package, channel)` for `license_findings`); and the newest row at-or-before
     `oldest_retained_cutoff` = min `evidence_cutoff` over surviving `PolicyRun`s, under the
     same keys. A `ProtectedError` on a batch falls back to per-row deletion, skipping and
     counting the protected rows, never aborting.
  3. `CollectionRun` rows with `finished_at` older than the retention, and unfinished rows
     whose `started_at` is older than the retention (a killed worker's row, which would
     otherwise pin the cut-off choice forever) -- counted separately.
- **Roster** (in `core/retention.py`, reconciled by a unit test against the evidence models
  the registry classifies): `inventory_snapshots`, `source_release_snapshots`,
  `pypi_release_snapshots`, `feedstock_snapshots`, `conda_package_snapshots`,
  `vulnerability_findings` (after `kev_findings`, which cite them), `kev_findings`,
  `license_findings`, `python_readiness_assessments`, `python_verification_results`,
  `identity_resolution_snapshots`. Excluded and named: `identity_overrides`,
  `inventory_changes`, `package_health`, every `packages`/`identity`/`workflow` table.
- **Command** `manage.py prune_evidence [--dry-run] [--batch N]` (default 1000) in
  `core/management/commands/`, taking `SystemClock()`; one `CollectionRun` per table under
  the unregistered collector name `prune_evidence` (`collection_run(collector=..., clock=...)`)
  whose `detail` names the table, the cut-off, deleted, protected-skipped, and `dry-run`;
  a summary line and one structlog event per table. Admin process `prune-evidence`
  (`prune` is the accelerator's), root task, no env, `schedule = "deployment-repository"`;
  `ADMIN_PROCESSES` in the S02 unit test grows by one.
- **Replay**: `replay_policy_run` refuses a subject whose `evidence_cutoff` is older than
  `now - retention` with a `CommandError` naming the cut-off, the retention and the earliest
  instant still replayable; a run that was purged is "no such run" as today.
- **Indexes**: a bare `observed_at` index on each of the eleven purged evidence tables
  (`collectors` migration `0014`), `finished_at` and `started_at` on `collection_runs`,
  `finished_at` and `evidence_cutoff` on `policy_runs` (`core` migration), named per the
  module constants; a unit test asserts every purged model and both ledgers declare an
  index whose first field is its cut-off column. The plans are proved by hand: run
  `EXPLAIN (ANALYZE, BUFFERS)` on the stack's PostgreSQL for one table's cut-off scan and the
  newest-per-package exclusion after `stack-seed`, and record both plans in the story's
  Auto Run Result (the suite bans vendor branches, and an empty table's plan is a seq scan
  whatever the index).
- **Docs**: `operations.md` -- the settings table (+1), the admin-process table (+1), the
  "nothing prunes it" sentence and every "there is no retention path" passage on the eight
  derived tables rewritten, the replay section's caveat and refusal; `asynchronous-work.md`
  schedule note; `the-policy-run.md` replay paragraph; `the-queues.md` (the purge touches no
  workflow row); `ARCHITECTURE-SPINE.md` `CPM-AD-2` gains the "one audited door" sentence
  under its exemptions, and the change proposal a §8 recording both deviations.
- **Tests**: unit -- setting default and refusal, the door refuses a non-token, the roster
  reconciliation, the index declarations, the exclusion keys; integration -- a seeded
  history (day -120, -100, -30, today) proving: rows older than the cut-off go, the newest
  per package stays, the row a retained run read at its cut-off stays even when older than
  the retention, a protected row is skipped and counted, batches are bounded (`--batch 2`
  over 5 rows → 3 batches), `--dry-run` deletes nothing and records counts, derived rows and
  their run go before the evidence they cite, a run pinned by `package_health` survives,
  an unfinished ledger row past retention goes, replay inside the window reproduces after
  a purge and outside it is refused with the wording; the command through `call_command`.
- Sprint status: `cpm-operate-s07-ninety-days-of-evidence-purged-nightly: done`.

**Block If:** the door cannot be built without loosening a refusal every existing case
pins; a purge order that satisfies the PROTECT graph cannot be found (report the cycle);
the audit exemption would have to be uncounted.

**Never:** a purge of `packages`, `package_health`, identity, workflow or the two audit
tables; an unbounded `DELETE`; `timezone.now()`; a second deletion path; a rule-version
bump (retention is not rule data).

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|---|---|---|---|
| Old rows | evidence at day -120 and -30, retention 90 | day -120 rows deleted unless newest-per-key; day -30 kept | N/A |
| Newest kept | a package's only rows at day -120 | kept; run record counts it as protected-by-rule | N/A |
| Replay floor | run R at cut-off day -30 inside window; package's newest row before day -30 is day -120 | that row kept; replay of R reproduces byte-identical after the purge | N/A |
| Outside window | run at cut-off day -100 | its derived rows and the run deleted; `replay_policy_run --of-run` → "no such run"; a surviving run older than retention (pinned) → refused naming cut-off, retention, earliest replayable | `CommandError` |
| Pinned run | `package_health` cites a run older than retention | run and its derived rows kept; counted | N/A |
| Protected | an evidence row cited by a retained derived row | skipped, counted; batch continues | `ProtectedError` caught per row |
| Ledger | `collection_runs` finished at day -120; one unfinished started day -120 | both deleted; separately counted | N/A |
| Batches | `--batch 2`, 5 deletable rows | 3 atomic batches; run record says 5 | N/A |
| Dry run | `--dry-run` | nothing deleted; record and summary say what would be | N/A |
| Setting | `CPM_EVIDENCE_RETENTION_DAYS=0` / `abc` | boot refused naming the setting | `ImproperlyConfigured` |
| Door | `AppendOnlyQuerySet.retire(door=object())` | refused | `AppendOnlyError` |

</intent-contract>

## Code Map

- Base: `core/models.py:241-426` `AppendOnlyQuerySet` (`delete` :332, `_raw_delete` :349 -- the
  parent's raw delete is the delegate), :429 manager, :466 `AppendOnlyModel` (`delete` :591),
  docstrings :336-338, :599-601 already say "a declared retention process". Pins:
  `tests/unit/django_apps/test_append_only_model.py:303,369,414-440`;
  `test_evidence_inheritance_audit.py:80,128-137,207`.
- Audit: `tests/unit/django_apps/test_mutation_path_audit.py:156-197` forms, :249-257
  `RECORDED_EXEMPTIONS` shape, :1086-1133 exact-count check; `test_clock_audit.py:152`.
- Ledgers: `core/models.py:713-925` (`RunLedgerModel`, `CollectionRun` :789 no indexes,
  `PolicyRun` :877 no indexes); `core/ledger.py:550` `collection_run(*, collector, clock, package_id=None)`,
  :371-421 `RunHandle`; `core/policy_run.py:200-256` `choose_evidence_cutoff` (min `started_at`
  over unfinished -- why stale unfinished rows are purged), :259 `execute_policy_run`.
- PROTECT graph: `core/models.py:1039` `PackageHealth.policy_run`; `policies/models.py` eight
  derived tables' `policy_run` (:370,726,1062,1418,1854,2308,2539,2739) and their evidence
  FKs (:497-546,806,1153,1167,1488,1993-2053,2344,2357); `collectors/models.py:2272`
  `KevFinding.vulnerability_finding`.
- Readers (why the floor rule is exact): every pass reads newest at-or-before the cut-off --
  `policies/currency.py:410`, `feedstock.py:311`, `vulnerability.py:469,506`, `licence.py:545`,
  `remediation.py:748,1040`, `py314_readiness.py:338,376`; `kev.py:1137` folds by advisory.
- Replay: `core/management/commands/replay_policy_run.py:161-195` `_subject`, :221-228 refusal
  shape; `core/replay.py:150-227` compares by attname (a re-picked row id is a difference).
- Evidence roster: `tests/unit/test_model_registry.py:102-118` (13 labels); `tests/model_registry.py:237-278`.
- Precedents: `prune_expired_state.py` (events :132-138, unbounded delete :150-170 -- the
  shape to avoid), `tests/integration/test_prune_command.py`; the seeder's synthetic
  collector `local-dev-demo-seed` (`demo_data.py:157,1212-1220`) for the run-record shape;
  `surface/coverage.py:347` (unregistered names never show); admin processes
  `component.toml:198-246`, `pixi.toml:501-522`, `test_operator_commands.py:64-73,243-250`.
- Settings precedent: `base.py:370` `CPM_SYNC_EXPORT_MAX_ROWS = env.int(...)`; `test_settings.py:1618-1689`.
- Index naming precedent: `core/models.py:1207-1208` (`package_health_cutoff`).
- Docs: `operations.md:1358-1360,1490-1500,1999-2012,2227,2416,2625,2843-2852,2948,3084,3168,3174-3250`;
  `asynchronous-work.md:288-296`; `the-policy-run.md:127-135`; `the-queues.md:163-164`;
  `docs/accelerator/deployment.md:616-630` (cite, read-only); spine `:141-154`.
- Read-only: `policies/*.py` passes, `core/replay.py`, `core/policy_run.py` (the refusal lives
  in the command), the accelerator's `prune_expired_state`.

## Tasks & Acceptance

**Execution:**
- [x] `core/models.py` -- the door; `core/retention.py` -- token, roster, keys, the purge; `core/management/commands/prune_evidence.py`.
- [x] `base.py` -- the setting; boot refusal; `collectors/migrations/0014_evidence_time_indexes.py`; `core/migrations/0012_ledger_time_indexes.py`.
- [x] `replay_policy_run.py` -- the window refusal.
- [x] `component.toml`, `pixi.toml` -- `prune-evidence`; audit exemptions.
- [x] Tests listed in Boundaries; `test_operator_commands.py` roster.
- [x] Docs, spine sentence, change proposal §8.
- [x] `sprint-status.yaml` -- story `done`.

**Acceptance Criteria:**
- Given the stack seeded and its evidence back-dated (a test fixture or a hand `UPDATE` in the
  stack DB for the check), when `pixi run stack-run prune_evidence --dry-run` then without it
  run, then the run records name each table's cut-off and count, rows older than ninety days
  are gone, every package still has its newest row per table, and `EXPLAIN (ANALYZE, BUFFERS)`
  for one table's cut-off scan uses the new index (plan recorded in the story).
- Given a policy run inside the window, when replayed after a purge, then it reproduces; given
  one outside, then it is refused with the reason.
- Given `pixi run ci`, when it runs, then it exits 0.

## Spec Change Log

- **The replay floor is kept for every surviving run's cut-off, not only the oldest.** The
  Boundaries say "the newest row at-or-before `oldest_retained_cutoff` = min `evidence_cutoff`
  over surviving `PolicyRun`s". With two surviving runs at two cut-offs -- a pinned run at
  day -100 reading the day -110 row and an in-window run at day -95 reading the day -97 row --
  the min rule keeps day -110 and removes day -97, and the second run's replay then reads a
  different row. The Intent's sentence ("never the row a retained policy run read at its
  cut-off") is the invariant, so the rule implemented is: a row is removable only when a
  strictly newer same-key row exists and no surviving run's `evidence_cutoff` lies in
  `[row.observed_at, next.observed_at)`. `test_every_surviving_run_protects_the_row_it_reads_not_only_the_oldest_cutoff`
  pins the case the min rule fails. The min rule's answers are a subset of this one's.
- **Two more reader keys.** `python_readiness_assessments` and `python_verification_results`
  are keyed per `(package, python_series)`, because `policies/py314_readiness.py` filters by
  series before taking the newest row; per package alone would have removed the row a replay
  reads whenever a newer row for another series existed. Ties at the newest instant are kept
  together under every key, because every reader takes the whole sweep at that instant.
- **Unfinished policy runs age by `started_at`.** Step 1 names runs "whose `finished_at` is
  older than the retention"; a killed worker's `policy_runs` row has none, and under either
  floor rule its cut-off would pin evidence for ever. It is purged when its `started_at` is
  older than the retention, on the terms step 3 gives for `collection_runs`.
- **The retention module's exemption is `_default_manager.delete(...)`, not
  `objects.delete(...)`.** The derived tables arrive as `type[Model]` from `PolicyRun`'s
  reverse relations, `objects` is not an attribute mypy knows on that type, and
  `_default_manager` is `objects` on every model the remover serves. Same form class in the
  audit (a manager marker), same count, same licence.
- **`retire` consults Django's collector before the raw delete.** The parent's `_raw_delete`
  is the statement, as specified; on its own it would meet a `PROTECT` relation as a deferred
  foreign-key violation at commit rather than as the `ProtectedError` the Boundaries catch per
  row. The collector is what raises `ProtectedError` before any SQL, so the door does that and
  then issues the raw delete. One counted `_raw_delete(...)` in `core/models.py`, as specified.
- **The replay rule is the ledger, not a window (review).** The Boundaries' "refuses a
  subject whose `evidence_cutoff` is older than `now - retention`" was replaced with the
  invariant the purge actually keeps: `--of-run` of any run still in the ledger is
  replayable, however old (the floor rule kept every row it read; a pinned run at day
  -100 reproduces after a purge); a stated `--evidence-cutoff` is admitted only when a
  surviving `PolicyRun` holds exactly that instant, else refused ("no retained run read at
  this cut-off"). The replay command no longer reads the retention at all, so there was
  nothing to wrap as `CommandError` there.
- **Rows a surviving row cites are not selected (review).** The selection excludes rows
  cited by a derived row of a surviving run or by another purged table's row that is not
  itself selected, through the model's reverse relations; the door's `PROTECT` check is
  the safety net for a citation committed between selection and `DELETE`, not the steady
  state. A rehearsal therefore counts exactly what the real run does; `kept_by_rule` is
  `older - deleted - protected`.
- **`inventory_snapshots` is keyed per `(package, source_package_key)` (review).**
- **The purge's own ledger rows never reach `choose_evidence_cutoff` (review).** Excluded
  by name, and only that name; a purge closes any `prune_evidence` row an earlier purge
  left running as `failed`, through the ledger's own `abandon`.
- **A non-integer environment value fails at settings import, not in the hook.** `env.int`
  is what the Boundaries specify and django-environ raises `ValueError` naming the value;
  `ready()` names the setting for a parsed value below one and for a settings module that
  assigned the wrong type. Recorded in `deferred-work.md`.

## Design Notes

**Why the floor rule and not "cited rows".** The derived tables cite the rows their passes
chose, but not every row a pass read (the vulnerability pass reads a set, cites one). "Newest
at-or-before the oldest retained cut-off, per reader key" is exactly what every reader
would see again, because rows are append-only and readers take the newest at-or-before;
`PROTECT` then guards the citations as a safety net rather than as the rule.

**Why derived rows and their runs go first.** They cite evidence with `PROTECT`; purging
evidence first would skip every row an out-of-window run still cited and the ledger would
keep shrinking-resistant runs. Order is part of the correctness.

**Why one `CollectionRun` per table and not a third ledger.** The spine names two ledgers;
a third is a spine amendment for a record whose only reader is an operator. The ledger row
under an unregistered name never appears on Coverage, and `detail` carries the numbers.

## Verification

**Commands:**
- `pixi run test`; `pixi run -e dev python -m pytest tests/integration/django_apps/test_retention.py tests/integration/django_apps/test_policy_replay.py tests/unit/django_apps/test_append_only_model.py -q` -- green.
- `pixi run docker-up` then, in the stack DB, back-date a handful of rows by hand (`stack-run shell -c`), `pixi run stack-run prune_evidence --dry-run`, then without; `EXPLAIN (ANALYZE, BUFFERS)` via `stack-run dbshell` for `SELECT id FROM pypi_release_snapshots WHERE observed_at < now() - interval '90 days'` -- record the plan.
- `pixi run ci` (stack down) -- exit 0.

## Auto Run Result

Status: done (the gate is the caller's to run; every step but `pixi run ci` itself was run
here -- see below).

**What was built.** `core/retention.py` (the token, the roster, the reader keys, the floor
rule as one queryset, the purge in bounded batches with one `CollectionRun` per table);
`AppendOnlyQuerySet.retire(*, door)` in `core/models.py`; `prune_evidence` with `--dry-run`
and `--batch`; the `CPM_EVIDENCE_RETENTION_DAYS` setting and its boot refusal; the window
refusal in `replay_policy_run`; eleven `observed_at` indexes (`collectors` `0014`) and four
ledger indexes (`core` `0012`); the `prune-evidence` admin process; two counted audit
exemptions; 46 unit cases (`test_retention.py`, plus one in `test_append_only_model.py` and
three in `test_settings.py`) and 27 integration cases (`test_retention.py`), the floor rule
first; the docs, the spine sentence and change proposal §8.

**Verification, stack down.** `pixi run test` (9326 passed); the integration suite bar
`test_image_payload.py` (1973 passed, 8 skipped -- the module that hangs on this machine);
`ruff check`, `ruff format` and `mypy src/` clean; `makemigrations --check` clean;
`mkdocs build --strict` clean. The touched modules were also run against the stack's
PostgreSQL 17 (`test_retention.py`, `test_policy_replay.py`, `test_operator_commands.py`,
the two unit modules: 166 passed, `connection.vendor == "postgresql"` confirmed).

**Live check, on the stack.** `docker-up`, `stack-migrate` (both migrations applied). The
stack was already seeded (148 packages, 237/241/444 PyPI/source/inventory rows, one policy
run). By hand in the stack DB: every evidence row but each package's newest moved 120 days
back; half the ended `collection_runs` and one synthetic unfinished row moved 120 days back;
100,000 synthetic PyPI rows spread over the last 91 days, so the cut-off scan selects about
one per cent as a steady-state nightly purge does. `stack-run prune_evidence --dry-run`:
21 records, `pypi_release_snapshots` would purge 1088 / keep 94 by rule, `inventory_snapshots`
148 / 148, `collection_runs` finished 804 + unfinished 1, nothing removed. Then without:
1087 PyPI rows went and **one was protected** -- a row a `package_currency` row of the seeded
run cited, which the floor rule had let go because the synthetic backfill had written
newer rows with `observed_at` *behind* that run's cut-off (something real collection never
does: `choose_evidence_cutoff` bounds the cut-off to before any in-flight run). `PROTECT`
did exactly what the design notes say it is for -- the safety net caught what the rule,
correctly on the data, did not -- and the record says `protected=1`. Every package kept
its newest row per table; no row older than the cut-off remained except the 95/103/148
the rule kept and the one protected; every record names the table, the cut-off and the
counts (`table=pypi_release_snapshots cutoff=2026-06-16T02:33:44+00:00 deleted=1087
kept_by_rule=94 protected=1 dry_run=false`).

The replay, cleanly: a fresh inline policy run (run 3, 148 rollup rows), one more inline
inventory ingest on top of it, the 148 inventory rows run 3 read moved 120 days back by
hand, `prune_evidence` (148 older rows went, **148 kept by rule**), then
`stack-run replay_policy_run --of-run 3 --no-input` -- "reproduced it exactly, over 1184
row(s)". Run 1's `evidence_cutoff` moved 120 days back by hand: refused with
`evidence cut-off 2026-05-16T21:41:29+00:00 is outside the retention window:
CPM_EVIDENCE_RETENTION_DAYS is 90 day(s), so the nightly purge has been free to remove
evidence older than 2026-06-16T02:36:25+00:00, which is the earliest cut-off still
replayable`. Its `finished_at` moved back too, `prune_evidence` (eight derived tables x 148
rows and the run), then `--of-run 1`: `no policy run has id 1.` `docker-down` afterwards.

**Plan 1 -- the cut-off scan** (`EXPLAIN (ANALYZE, BUFFERS)`, 100,237 rows, 1,182 older
than 90 days):

```
Bitmap Heap Scan on pypi_release_snapshots  (cost=29.49..1879.94 rows=1186 width=8) (actual time=0.199..1.879 rows=1182 loops=1)
  Recheck Cond: (observed_at < (now() - '90 days'::interval))
  Heap Blocks: exact=830
  Buffers: shared hit=835
  ->  Bitmap Index Scan on pypi_release_observed  (cost=0.00..29.19 rows=1186 width=0) (actual time=0.128..0.128 rows=1182 loops=1)
        Index Cond: (observed_at < (now() - '90 days'::interval))
        Buffers: shared hit=5
Planning Time: 0.546 ms
Execution Time: 1.964 ms
```

**Plan 2 -- the newest-per-package exclusion**, the exact statement Django issues for one
batch of `removable_evidence` (`.order_by("pk").values_list("pk")[:1000]`, via
`QuerySet.explain(ANALYZE=True, BUFFERS=True)`):

```
Limit  (cost=3611.63..3612.45 rows=329 width=8) (actual time=15.897..15.958 rows=1000 loops=1)
  ->  Sort  (cost=3611.63..3612.45 rows=329 width=8) (actual time=15.895..15.919 rows=1000 loops=1)
        Sort Key: pypi_release_snapshots.id
        ->  Nested Loop Anti Join  (cost=39.42..3597.87 rows=329 width=8) (actual time=0.620..15.745 rows=1088 loops=1)
              Join Filter: ((w0.evidence_cutoff >= pypi_release_snapshots.observed_at) AND (NOT EXISTS(SubPlan 2)))
              Rows Removed by Join Filter: 1088
              ->  Nested Loop Semi Join  (cost=29.90..2397.87 rows=395 width=24) (actual time=0.358..8.719 rows=1182 loops=1)
                    ->  Bitmap Heap Scan on pypi_release_snapshots  (cost=29.48..1874.00 rows=1186 width=24) (actual time=0.331..2.598 rows=1182 loops=1)
                          Recheck Cond: (observed_at < '2026-06-16 02:33:22.694798+00'::timestamp with time zone)
                          ->  Bitmap Index Scan on pypi_release_observed  (cost=0.00..29.19 rows=1186 width=0) (actual time=0.250..0.250 rows=1182 loops=1)
                                Index Cond: (observed_at < '2026-06-16 02:33:22.694798+00'::timestamp with time zone)
                    ->  Index Only Scan using pypi_release_pkg_observed on pypi_release_snapshots u0  (cost=0.42..19.53 rows=530 width=16) (actual time=0.005..0.005 rows=1 loops=1182)
                          Index Cond: ((package_id = pypi_release_snapshots.package_id) AND (observed_at > pypi_release_snapshots.observed_at))
              ->  Materialize  (cost=9.52..10.54 rows=1 width=8) (actual time=0.000..0.000 rows=1 loops=1182)
                    ->  Seq Scan on policy_runs w0  (cost=9.52..10.53 rows=1 width=8) (actual time=0.052..0.053 rows=1 loops=1)
                          Filter: (NOT (ANY (id = (hashed SubPlan 1).col1)))
                          SubPlan 1
                            ->  Nested Loop Anti Join  (cost=0.00..9.52 rows=1 width=8) (actual time=0.003..0.003 rows=0 loops=1)
                                  ->  Seq Scan on policy_runs v0  (cost=0.00..1.01 rows=1 width=8) (actual time=0.003..0.003 rows=0 loops=1)
                                        Filter: ((finished_at < '2026-06-16 02:33:22.694798+00'::timestamp with time zone) OR ((finished_at IS NULL) AND (started_at < '2026-06-16 02:33:22.694798+00'::timestamp with time zone)))
                                  ->  Seq Scan on package_health u0_1  (cost=0.00..8.48 rows=148 width=8) (never executed)
              SubPlan 2
                ->  Index Only Scan using pypi_release_pkg_observed on pypi_release_snapshots u0_2  (cost=0.42..21.05 rows=8 width=0) (actual time=0.005..0.005 rows=1 loops=1182)
                      Index Cond: ((package_id = pypi_release_snapshots.package_id) AND (observed_at > pypi_release_snapshots.observed_at) AND (observed_at <= w0.evidence_cutoff))
Planning Time: 3.019 ms
Execution Time: 16.120 ms
```

The cut-off scan is the new `pypi_release_observed` index (bitmap, 5 index buffers); the
newest-per-package exclusion and the floor rule's inner probe are both index-only scans on
the existing `pypi_release_pkg_observed`; `policy_runs` and `package_health` are sequential
because the tables hold one and 148 rows -- the planner being right, not the index being
absent. Sixteen milliseconds for a batch over a hundred thousand rows.

**Review: fourteen items, all applied.** (1) `choose_evidence_cutoff` excludes
`prune_evidence` by name and a purge abandons stale purge rows as `failed`; (2) the
replay rule is the ledger; (3) `retire` refuses sliced, projected, distinct-on-fields and
combined querysets with Django's own `TypeError`s; (4) a `ProtectedError` subtracts the
cited keys and retries once, `IntegrityError` and `RestrictedError` fall to one row at a
time, and the docs say not to overlap `policy-run`; (5) a `DatabaseError` fails one
table's record with what went and the night carries on, the command exits non-zero naming
the tables, `protected > 0` is `partial`; (6) a rehearsal counts what the real run does
(the citation predicate is the pre-check -- a collector pre-check in a rehearsal would
flag citations from runs step 1 removes, which is exactly the mismatch the item named);
(7) `kept_by_rule` is derived; (8) surviving citers are excluded from the selection;
(9) the inventory key; (10) batch ceiling 10,000, retention ceiling 3,650, a naive `now`
refused citing `CPM-AD-26`, `retention.cutoff_moved` warned when the cut-off moved further
than the time since the last purge; (11) the floor cases are parametrised over every
roster table with the boundary, unfinished-run, idempotence and ledger-leg cases;
(12) `retire` is an unmistakable form in the mutation audit, counted once for
`core/retention.py`, with a probe-module case; (13) the docs and comments; (14) the two
migrations depend only on their own application's previous migration. 9332 unit cases,
2023 integration cases (bar the module that hangs locally), 566 cases of the touched
modules against the stack's PostgreSQL 17, ruff/mypy/`makemigrations --check`/mkdocs
strict all clean; the stack's own migrations re-applied cleanly with the trimmed
dependencies and a rehearsal on it recorded zeros.

**Risks and what was left.** The mutation audit's exemption for `core/retention.py` is
spelled `_default_manager.delete(...)`; see the Spec Change Log. Two entries were appended to
`deferred-work.md` (the non-integer environment value; the observation-window read of the
ledger under a retention shorter than a window). `stack-seed` was not re-run: the stack was
already seeded from the earlier stories and re-seeding resolves identity live against
conda-forge and PyPI. The synthetic PyPI volume was removed from the stack DB by hand after
the plans were taken; the hand back-dating of the stack's own rows was left, so run 1 is
gone and runs 3-5 stand.

## Auto Run Result

Status: done
Deviations recorded: retention is a declared setting, not a versioned policy parameter; the
two human-audit tables are excluded from the purge; the floor rule is per surviving run
rather than the minimum cut-off (a case proves the minimum would delete a row a second
retained run read); unfinished policy runs age by `started_at`.
Review: three layers; fourteen patches. The ones that changed behaviour: the purge's own
ledger rows no longer feed `choose_evidence_cutoff` and a killed purge's open row is
closed at the next start; the replay refusal now enforces the invariant the purge keeps
(any run still in the ledger replays; a stated cut-off only if a surviving run holds it);
rows a surviving citer protects are excluded from the selection rather than left to the
`PROTECT` net; a failure on one table no longer ends the night; a second user of the door
fails the mutation-path gate.
Plans (stack PostgreSQL 17, 100k synthetic PyPI rows): cut-off scan a Bitmap Index Scan on
`pypi_release_observed` (1.9 ms); newest-per-package and floor probes Index Only Scans on
`pypi_release_pkg_observed` (16 ms per 1000-row batch).

## Suggested Review Order

**The door**

- Entry point: one deletion path, refusing everything but its token and every unsafe queryset shape
  [`models.py:406`](../../../src/django_apps/conda_sentinel/core/models.py#L406)

**What may go, and what never may**

- The roster and each table's reader key -- a conservative superset of what the passes read
  [`retention.py:440`](../../../src/django_apps/conda_sentinel/core/retention.py#L440)

- Removable: older than the cut-off, not newest per key, not read by any surviving run, cited by no survivor
  [`retention.py:636`](../../../src/django_apps/conda_sentinel/core/retention.py#L636)

- Derived rows and their runs first, then evidence, then the ledger; each batch its own transaction
  [`retention.py:1165`](../../../src/django_apps/conda_sentinel/core/retention.py#L1165)

**What the purge must not disturb**

- The cut-off chooser ignores the purge's own records
  [`policy_run.py:201`](../../../src/django_apps/conda_sentinel/core/policy_run.py#L201)

- Replay: any run still in the ledger; a stated cut-off only if a run holds it
  [`replay_policy_run.py:173`](../../../src/django_apps/conda_sentinel/core/management/commands/replay_policy_run.py#L173)

**Tests**

- The floor rule: a row older than the retention survives because a retained run read it
  [`test_retention.py:406`](../../../tests/integration/django_apps/test_retention.py#L406)

- Order is correctness: derived rows and their run before the evidence they cite
  [`test_retention.py:647`](../../../tests/integration/django_apps/test_retention.py#L647)

- A second user of the door fails the gate
  [`test_mutation_path_audit.py:165`](../../../tests/unit/django_apps/test_mutation_path_audit.py#L165)
