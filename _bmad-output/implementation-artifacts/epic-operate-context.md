# Epic operate Context: Operating the inventory

<!-- Compiled from planning artifacts. Edit freely. Regenerate with compile-epic-context if planning docs change. -->

## Goal

Close the gap between a product that collects and one somebody operates. Every operator answer on 2026-09-13 was a `manage.py shell -c` snippet that had to carry three environment variables or silently talk to SQLite and run inline. This epic turns those snippets into named tasks and management commands, moves the watchlist from a CSV in the wheel to a governed table shared by every pod and the demo, makes a fresh stack observe on day one, declares the missing settings (GitHub token, local channel default, retention), adds a per-package re-run from the page and a daily digest, purges evidence older than ninety days nightly, and measures the evidence tables at ten thousand packages before deciding on partitioning. No new FR; it operates CPM-FR-42, -15, -22, -10 and covers CPM-NFR-1, -2. Raised by `sprint-change-proposal-2026-09-13.md`.

## Stories

- Story CPM-OPERATE-S01: A shell against the stack carries the stack's environment
- Story CPM-OPERATE-S02: Ingest, sweep and run are commands, not shell snippets
- Story CPM-OPERATE-S03: The inventory is a governed table
- Story CPM-OPERATE-S04: Day one is observed
- Story CPM-OPERATE-S05: An authenticated GitHub allowance
- Story CPM-OPERATE-S06: Published-conda currency on the local stack
- Story CPM-OPERATE-S07: Ninety days of evidence, purged nightly
- Story CPM-OPERATE-S08: A collector re-run for one package, from the page
- Story CPM-OPERATE-S09: An operator digest
- Story CPM-OPERATE-S10: The evidence tables at ten thousand packages

## Requirements & Constraints

- **Evidence is append-only; run ledgers are not evidence (CPM-AD-2).** The `core` base refuses `save()` on a set `pk` and exposes no `update()`/`delete()`. The purge is the one exception: one audited door the base exposes for that command alone, each table's purge writing a run record with cut-off and count. A package's newest row per table, rollup rows (CPM-AD-11) and package rows are never purged. Replay inside the retention window is byte-identical; outside it is refused with a reason (CPM-FR-22).
- **Requests and commands enqueue; tasks collect (CPM-AD-9).** Nothing runs a collector, policy pass or outbound call in the caller unless settings already make every task eager; digest delivery is a task.
- **One governed write path, three obligations (CPM-AD-14, CPM-FR-32, CPM-AD-23).** Permission, non-empty reason, audit row (actor, timestamp, prior, new, reason) in the same transaction — never `on_commit`, never a follow-up task. Inventory rows are governed reference data, so S03 amends CPM-FR-3 ("the only human write") to name two writes; the amendment travels with S03's PR and is accepted before the surface write ships — until then `import_watchlist` is the only way rows change. A wrong identity is corrected through the override, never the inventory.
- **Authorization per surface, enforced centrally (CPM-AD-13).** Inventory and recollect permissions are `core` permission classes; refusals log the acting user identity.
- **Correlation, never a silent fallback (CPM-AD-15, CPM-NFR-12, -13).** Runs carry `trace_id`; a refused GitHub credential is a `failed` run whose `detail` names neither token nor prefix. No credential reaches any log, ledger, evidence row or `detail`; S05 and S09 each carry an audit test.
- **Scheduling is data, reconciled at start-up (CPM-AD-20).** Cadences live in `django_celery_beat`'s `DatabaseScheduler` via `CELERY_BEAT_SCHEDULE`; queues `collect`/`policy`/`verify`. S04 adds one dispatch, never a cadence; S05's allowance and S09's entry are declarations reconciled the same way.
- **Transactions per package (CPM-AD-23, CPM-FR-15).** No task holds a transaction across packages; the purge deletes in bounded batches, never a lock across the whole range.
- **The inventory is evidence; absence is an observation (CPM-AD-25, CPM-FR-42).** The collector never writes the package table — shells come from `identity`'s resolution service at `unmapped`. A retired row is recorded absent with a timestamp; no package row is deleted; readers are cut-off bound.
- **Pure parsers, transport seam (CPM-AD-27).** The bearer header is added at the seam; the database inventory is a transport substitution behind the same `Transport` contract the CSV adapter implements, with no branch in the collector.
- **Declared adapter; locality fails closed (CPM-AD-29, inherited AD-13).** `COMPONENT_RUNTIME=local` comes only from the `dev` feature's activation env; `test_locality_declaration.py` fails any task that declares it. New settings (`CPM_SWEEP_ON_BEAT_START`, the local `CPM_MONITORED_CHANNELS`) are read at settings time via `is_local()` and read *deployed* when absent. The CSV adapter stays: the import reads through it and a deployment with no database inventory selects it. Malformed input raises `ImproperlyConfigured` before any row is written.
- **Admin processes never set `COMPONENT_PROCESS` (inherited AD-13).** Each command is an `[[admin_processes]]` entry in `component.toml` with a pixi task and `schedule = "deployment-repository"`; `test_process_model.py` reconciles both directions. The accelerator's existing `prune` entry is the template.
- **Scale, cadence, visibility (CPM-NFR-1, -2, CPM-FR-38).** Full-inventory collection at ten thousand packages within cadence; independent cadences; the digest delivers even when nothing changed.

## Technical Decisions

- **Local-stack conventions carry over from `epic-platform-context.md`:** compose is infrastructure only (Redis 6380, PostgreSQL 5433), the application runs under honcho from the pixi environment, every `stack-*` task carries the same `DATABASE_URL`/`REDIS_URL` and an explicit `CELERY_TASK_ALWAYS_EAGER`, and `test_local_stack.py`'s `AGAINST_THE_STACK` roster with `test_every_stack_task_names_the_same_database` makes N copies of one URL safe. S01's `stack-shell`/`stack-run` are copies five and six and join the roster. No stack task declares `COMPONENT_PROCESS`.
- **Dispatch goes through what beat fires.** `dispatch(*, collector, clock)` in `collectors/sweep.py` resolves the registry, refuses an unregistered or unswept name, and enqueues `cpm.collect.sweep` (`collect_sweep(*, collector)`). Ingestion is `cpm.collect.inventory` (`ingest_inventory(*, force)`); the policy run is `cpm.policy.run` and needs a recorded version. S02, S04 and S08 all enqueue these same tasks.
- **Measured facts behind the stories:** beat's interval entries start their clock on creation, so a fresh stack sweeps nothing for a day and weekly surfaces for a week; unauthenticated GitHub is 60 calls/hour charged four per collection (about fifteen packages an hour for `source_release`, ten a minute for `feedstock`) against 5,000 core calls/hour and 30 searches/minute authenticated; the demo roster (`pypi:<name>`) and the 107-row development watchlist collide on 45 names, so ingesting onto a seeded database raises `IntegrityError` on `canonical_name`; `CPM_MONITORED_CHANNELS` is empty, so `conda_package` and `license` select nothing; nothing prunes any evidence table.
- **Settings are declared, off by default, reconciled:** the local channel default against `MAX_MONITORED_CHANNELS`; retention as one duration in the versioned policy parameters, default 90 days, "forever" only as a number.
- **Indexes are declared on the model, named per existing constants, reconciled by `test_migration_completeness`;** `EXPLAIN` is measured, not assumed. **Partitioning is a spine amendment, not a migration:** S10 records a measured verdict, and monthly range partitioning by `observed_at` is proposed to CPM-AD-2 and the migration audit, not implemented until accepted.

## Cross-Story Dependencies

- S01 is the precondition for every later story being usable by hand; every verification runs through `stack-run`.
- S02 lands before S08 so "Collect now" and `dispatch_sweep` enqueue the same tasks; S08 also depends on CPM-EP-APP's package page.
- S03 carries the CPM-FR-3 amendment and resolves CPM-IDENTITY-S07's deferred item; the surface write is gated on acceptance; the seeder change builds on CPM-PLATFORM-S08 and CPM-IDENTITY-S08.
- S04, S05, S06 are independent declarations after S02, each exercised on the stack through S01.
- S07's purge is what S10 measures; S10's verdict is recorded before any partitioning, and any index it adds serves S07's cut-off scan.
- S09 reads the ledger S02 and S04 populate and adds one schedule entry beside them.
