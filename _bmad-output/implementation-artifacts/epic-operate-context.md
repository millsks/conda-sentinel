# Epic operate Context: Operating the inventory

<!-- Compiled from planning artifacts. Edit freely. Regenerate with compile-epic-context if planning docs change. -->

## Goal

Close the gap between a product that collects and one somebody operates. The epic turns those snippets into stack-aware pixi tasks and management commands, moves the watchlist from a CSV in the wheel to a governed table shared by every pod and the demo, makes a fresh stack observe on day one, declares the missing settings (GitHub token, local channel default, retention), adds a per-package re-run from the page and a daily digest, purges evidence older than ninety days nightly, measures the evidence tables at ten thousand packages before deciding on partitioning, and makes an absent package leave the queues. No new FR; it operates ingestion, run records, versioned policy runs and published-conda currency, and covers full-inventory scale and independent cadence.

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
- Story CPM-OPERATE-S11: An absent package leaves the queues

## Requirements & Constraints

- **Two governed human writes, three obligations each.** The identity override and the inventory row are the only human writes to governed reference data; each needs a permission, a non-empty reason, and an audit row (actor, timestamp, prior, new, reason) in the same transaction — never `on_commit`, never a follow-up task. The amendment naming the inventory write is applied. A wrong identity is corrected through the override, never the inventory; retiring is a column write.
- **Evidence is append-only; the purge is the one audited door.** The base refuses `save()` on a set `pk` and exposes no `update()`/`delete()`. Retention is the single exception: one method opened only by a token the retention module constructs, deleting in bounded batches with a run record per table naming cut-off and count. Never removed: a package's newest row per table, any row a surviving policy run read at its cut-off, package rows, rollup rows, or the human-audit tables (`identity_overrides`, `inventory_changes`). Replay inside the window is byte-identical; outside it is refused with a reason.
- **Requests and commands enqueue; tasks collect.** Nothing runs a collector, policy pass or outbound call in the caller unless settings already make every task eager; commands dispatch through the same task beat fires; digest delivery is a task.
- **Authorization declared per surface, enforced centrally.** Inventory and recollect permissions are `core` permission classes; every refusal is logged with the acting user identity.
- **Correlation, never a silent fallback.** Runs carry `trace_id`. A refused GitHub credential is a `failed` run whose `detail` names neither token nor prefix. No credential reaches any log, ledger, evidence row or `detail`.
- **Scheduling is data, reconciled at start-up.** Cadences live in the database scheduler via `CELERY_BEAT_SCHEDULE`, routed to `collect`/`policy`/`verify`. Day-one dispatch adds one dispatch, never a cadence, and never bypasses a collector's observation window; the raised GitHub allowance and the digest entry are declarations reconciled the same way.
- **Transactions per package.** No task holds a transaction across packages; the purge never holds a lock across the whole range.
- **The inventory is evidence; absence is an observation.** The collector never writes the package table — shells come from identity resolution at `unmapped`. A retired row is recorded absent with a timestamp; no package or rollup row is ever deleted; readers are cut-off bound, so absence at a policy run's cut-off replays to the same queue. An absent package is still gated by its confidence, never by its absence.
- **Every queue item is the workflow app's.** Closing an absent package's open items is a workflow-state write with actor `system` and a reason. The feedstock-gap report is the only surface allowed to exclude packages, and states each exclusion with count and reason.
- **Admin processes never set `COMPONENT_PROCESS`.** Each command is an `[[admin_processes]]` entry in `component.toml` with a pixi task and `schedule = "deployment-repository"`, reconciled both ways by the process-model test. The purge is the deployment repository's nightly job, not a beat entry.
- **Locality fails closed toward deployment.** `COMPONENT_RUNTIME=local` comes only from the `dev` feature's activation env; the locality test fails any task that declares it. New settings are read at settings time via `is_local()` and read *deployed* when absent. Malformed inventory input raises `ImproperlyConfigured` before any row is written.
- **Staleness and failure stay visible.** The digest delivers even when nothing changed; absent packages are labelled with their last-listed date on the package page and in every list.

## Technical Decisions

- **Stack tasks share one environment.** Compose is infrastructure only (Redis 6380, PostgreSQL 5433); every `stack-*` task carries the same `DATABASE_URL`/`REDIS_URL` and an explicit `CELERY_TASK_ALWAYS_EAGER`, and the `AGAINST_THE_STACK` roster test makes N copies of one URL safe.
- **One inventory adapter contract.** The database inventory is a second transport substitution behind the same `Transport` contract as the CSV adapter, with no branch in the collector. The CSV adapter stays for the import and for a deployment declaring no database inventory. The wheel's file is the first-run *seed*, not the source.
- **Demo and watchlist are one inventory.** The seeder writes the `inventory` table and files each shell under the inventory's own `(identity_source, associator_key)`; advisories, KEV and licences stay a name-keyed overlay.
- **Retention is a declared setting, not a policy parameter.** `CPM_EVIDENCE_RETENTION_DAYS` in base settings (default 90, refused below 1 at boot), spelled once in `core/retention.py`, which also names the excluded human-audit tables. The Python 3.14 tables keep their newest row per `(package, python_series)`.
- **Local defaults are settings declarations.** The local `CPM_MONITORED_CHANNELS = conda-forge` default lives in `config/settings/local.py`, reconciled against `MAX_MONITORED_CHANNELS`; `CPM_SWEEP_ON_BEAT_START` is off by default and on only locally.
- **GitHub token at the transport seam.** The bearer header is added in the collector base so parsers stay pure; authenticated is 5,000 core calls an hour and 30 searches a minute, versus 60 an hour unauthenticated charged four per collection.
- **Indexes declared on the model, measured with `EXPLAIN`, reconciled by the migration-completeness test.** Partitioning is a spine amendment, not a migration: S10 records a measured verdict, and monthly range partitioning by `observed_at` is proposed before anything is implemented.

## UX & Interaction Patterns

- A request that enqueues work returns `202` with a job id appended to the URL as `?job=`; job state is server-side, the toast carries job id and trace id and survives reload, and a "Meanwhile, what is known" panel shows current values unchanged and says a new evidence row will be inserted, nothing updated in place.
- Failed runs are rows in the collector-health table with a linked `trace_id`, never a page error.

## Cross-Story Dependencies

- S01 is the precondition for every later story being used by hand; each verification runs through `stack-run`.
- S02 lands before S08 so "Collect now" and `dispatch_sweep` enqueue the same tasks; S08 also needs the package page from CPM-EP-APP.
- S03 resolves CPM-IDENTITY-S07's deferred "changed by release" item, builds on CPM-PLATFORM-S08 and CPM-IDENTITY-S08, and hands absence semantics to S11, added while S03 was built because nothing downstream read inventory absence.
- S04, S05, S06 are independent declarations after S02.
- S07's purge is what S10 measures; any index S10 adds serves S07's cut-off scan; S10's verdict precedes any partitioning.
- S09 reads the ledger S02 and S04 populate and adds one schedule entry beside them.
- S11 touches identity selection, workflow opening, the rollup surface and the feedstock-gap report, all owned by earlier epics.
