---
title: 'CPM-OPERATE-S02: Ingest, sweep and run are commands, not shell snippets'
type: 'feature'
created: '2026-09-13'
status: 'done'
review_loop_iteration: 0
followup_review_recommended: false
warnings: [oversized]
deferred: []
baseline_revision: '0f71de33154b9001db4ca429dbf95e830be7ce5f'
context:
  - _bmad-output/implementation-artifacts/epic-operate-context.md
  - _bmad-output/implementation-artifacts/stories/cpm-operate-s01-a-shell-against-the-stack-carries-its-environment.md
---

<intent-contract>

## Intent

**Problem:** Ingestion, a collector sweep and a policy run exist only as Celery tasks; the
documented way to start one is a `stack-shell -c` snippet, and a deployment has no
process it can schedule for any of them. The product's one management command is
`replay_policy_run`.

**Approach:** Three management commands -- `ingest_inventory [--force]`,
`dispatch_sweep <collector>... | --all`, `run_policy [--version V]` -- each validating its
arguments, then enqueueing the existing task (or running it inline where settings make
every task eager) and reporting the ledger row; each declared as an `[[admin_processes]]`
entry with a root pixi task, so the deployment can schedule them and an operator runs
them on the stack through `stack-run`.

## Boundaries & Constraints

**Always:**
- Placement: `collectors/management/commands/{ingest_inventory,dispatch_sweep}.py` and
  `policies/management/commands/run_policy.py` (new packages with `__init__.py`); never
  under `core/` (the layering audit forbids `core` importing downstream apps).
- A command validates before it enqueues and refuses with `CommandError` (EM style:
  `message = (...)` then `raise`): a blank, reserved (`sweep`), unknown or unswept
  collector name (listing the swept names); a policy version the parameters file does not
  record (listing the recorded ones); `--all` combined with names. Validation makes no
  outbound call and writes nothing.
- Enqueue through the task object's `.delay()` with the task's own keyword/positional
  contract (`ingest_inventory(force=)`, `collect_sweep(collector=)`, `run_policy(version)`).
  The command opens no transaction, catches nothing the task raises when eager
  (`CELERY_TASK_EAGER_PROPAGATES`), and writes no ledger row itself.
- Reporting on two channels, as `prune_expired_state` does: one structlog event per
  action with the task name, arguments and (when known) task id or run state, and one
  human line on `self.stdout`. When the task ran eagerly the line carries the run's
  state and, for a dispatch, the count parsed from the newest dispatch row's `detail`
  (`CollectionRun` with `package IS NULL` for that collector); when it was enqueued, the
  line carries the task id and says where to watch it (the Coverage screen, flower).
- `dispatch_sweep --all` iterates `registered_collectors()` (the registry's deterministic
  order) filtered to `selectable_packages() is not None`; `inventory` and
  `py314_verification` are never dispatched. A unit case reconciles that set against the
  collectors named in `CELERY_BEAT_SCHEDULE`.
- `run_policy` without `--version` uses the newest recorded version exactly as
  `demo_data.py` and the docs derive it (`sorted(parameters_from(...))[-1]`); it never
  passes a default into `core/tasks.py`'s `run_policy`, which forbids one.
- `component.toml`: three `[[admin_processes]]` entries -- `ingest` (`python manage.py ingest_inventory`),
  `sweep` (`python manage.py dispatch_sweep --all`), `policy-run` (`python manage.py run_policy`)
  -- `schedule = "deployment-repository"`, each a root `[tasks]` task beside `prune`,
  `default-environment = "default"`, no `env`, outside the `# feature:celery` region.
- Clock: `SystemClock()` only; no `timezone.now()` (clock audit). No `print()`.
- Docs: `asynchronous-work.md` keeps the heading "Two tasks nothing fires" (a test pins it)
  but its "and no management command" sentence and both `send_task` recipes become the
  commands; `running-it.md` `:176-192` and `:227-250`; `operations.md` `:1668-1700`;
  `onboarding.md` operating table; `README.md` development block; a short admin-process
  note in `operations.md` naming the three. Every recipe on the stack is `pixi run stack-run <command>`;
  deployed, `pixi run <task>`.
- Tests: `tests/integration/django_apps/test_operator_commands.py` with `call_command`
  (eager under test settings): every refusal; each command's happy path writing the
  expected ledger row and printing its state; `--all` never names the two unswept
  collectors; a non-eager path with `.delay` patched, asserting one enqueue with the
  right arguments and no ledger row written by the command. Unit: the `--all` set versus
  the beat schedule; the three admin-process declarations (the existing process-model
  audits cover the rest).
- Sprint status: `cpm-operate-s02-ingest-sweep-and-run-are-commands: done`.

**Block If:** a command that collects in the caller when Celery is not eager; a fourth
command; any change to the tasks' signatures or to `core/tasks.py`.

**Never:** a default policy version inside `core/`; a command under `core/management/`;
a `stack-*` duplicate of the three tasks (`stack-run` is the stack variant); catching
`SweepDispatchError`/`PolicyParameterError` after enqueue to soften a failed run.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|---|---|---|---|
| Ingest, eager | `ingest_inventory` under test settings with an adapter declared | one `CollectionRun` `collector=inventory`; stdout names the state | task exceptions propagate |
| Ingest, no adapter | no adapter declared | `InventoryAdapterError` propagates; no row | not caught |
| Sweep one | `dispatch_sweep pypi_release` | one dispatch row (`package IS NULL`); stdout: name, state, "offered N" | N/A |
| Sweep many | `dispatch_sweep pypi_release feedstock` | one dispatch row each, in argument order | N/A |
| Sweep all | `dispatch_sweep --all` | one dispatch row per swept collector; never `inventory`, `py314_verification`, `sweep` | N/A |
| Sweep refusals | `sweep`; `nope`; `inventory`; `--all pypi_release`; no arguments | `CommandError` before any row, listing the swept names | exit 1 |
| Policy, default | `run_policy` | newest recorded version; one `PolicyRun`; stdout: version, rollup rows | N/A |
| Policy, named | `run_policy --version 2026.09.1` | that version | N/A |
| Policy, unrecorded | `run_policy --version 1999.01` | `CommandError` listing the recorded versions; nothing enqueued | exit 1 |
| Not eager | `.delay` patched | one call with the task's arguments; stdout carries the task id; no ledger row from the command | N/A |
| Admin processes | `component.toml` | `ingest`, `sweep`, `policy-run` resolve to Django commands through root tasks | process-model audit |

</intent-contract>

## Code Map

- Template: `src/django_service/users/management/commands/prune_expired_state.py` :99 logger,
  :105-108 event constants, :184 `Command`, :189 `add_arguments`, :201 `handle`, :265 the one
  human line; `tests/integration/test_prune_command.py:197-213` the two-channel rule and `_run`.
- House style: `src/django_apps/conda_sentinel/core/management/commands/replay_policy_run.py`
  :86 arguments, :119 `handle`, :139-192 `CommandError` refusals, :142 `SystemClock()`.
- Tasks: `collectors/tasks.py:191` `INGEST_TASK_NAME`, :1203 `ingest_inventory(*, force=False) -> str`;
  :99/147 `SWEEP_TASK_NAME` re-export, :1929 `collect_sweep(*, collector) -> str`;
  `core/tasks.py:70` `POLICY_RUN_TASK_NAME`, :80 `run_policy(policy_version) -> int` (positional).
- Registry/sweep: `core/registry.py:161` `registered_collectors()` (sorted by name);
  `collectors/sweep.py:173` `RESERVED_COLLECTOR_NAME`, :249 `SweepDispatchError`, :361 `_resolve`,
  :505 `dispatch`, :753 the `detail` wording "enqueued N selected package(s)"; swept iff
  `selectable_packages() is not None` (:868). Unswept: `inventory`, `py314_verification`.
- Version: `policies/parameters.py:661` `parameters_file()`, :679 `parameters_from(text, *, source)`,
  :468 `PolicyParameterError`; newest = `sorted(...)[-1]` (`demo_data.py:992-1010`).
- Ledger: `core/models.py:789` `CollectionRun` (:830 `collector`, :847 `package` nullable, :763
  `status`, :779 `detail`), :877 `PolicyRun` (:896 `policy_version`); `core/runs.py:77` `RunState`.
- Contract: `component.toml:184-199` admin block; `src/config/component/loader.py:130,189,562`;
  `pixi.toml:470-490` `prune` and its comment; `tests/unit/test_process_model.py:424,454,490,550,584`.
- Audits to satisfy: `test_app_layering_audit.py:139` (placement), `test_clock_audit.py:452`,
  `test_task_declaration_audit.py:653`, `test_documentation_commands.py:61` (every `pixi run x`
  is a task), `test_local_stack.py:549-583` (docs sweep rules), `test_documented_subsystems.py:187`
  (the heading), ruff `T20`.
- Docs: `asynchronous-work.md:229-253,165-193,199-206`; `running-it.md:176-192,227-250`;
  `operations.md:1668-1700,2895-2930`; `onboarding.md:344-355`; `README.md:70-85`;
  `docs/accelerator/deployment.md:701-707` (read-only, the admin-process concept).
- Test helpers: `tests/celery_tasks.py:96-123`, `tests/pixi_manifest.py:58-272`,
  `tests/integration/django_apps/test_policy_replay.py:31-37,367-474`.
- Read-only: `core/tasks.py`, `collectors/tasks.py`, `collectors/sweep.py`, `policies/parameters.py`.

## Tasks & Acceptance

**Execution:**
- [x] `src/django_apps/conda_sentinel/collectors/management/__init__.py`, `.../commands/__init__.py`,
  `.../commands/ingest_inventory.py`, `.../commands/dispatch_sweep.py` -- new.
- [x] `src/django_apps/conda_sentinel/policies/management/__init__.py`, `.../commands/__init__.py`,
  `.../commands/run_policy.py` -- new.
- [x] `component.toml` -- three `[[admin_processes]]`; `pixi.toml` -- `ingest`, `sweep`, `policy-run`
  in root `[tasks]` beside `prune`, with a comment.
- [x] `tests/integration/django_apps/test_operator_commands.py` -- new; `tests/unit/django_apps/test_operator_commands.py`
  -- the `--all`/schedule reconciliation and the declarations.
- [x] Docs listed in the Code Map; `README.md`.
- [x] `_bmad-output/implementation-artifacts/sprint-status.yaml` -- story `done`.

**Acceptance Criteria:**
- Given the stack up, when `pixi run stack-run dispatch_sweep --all` runs, then nine dispatch
  rows appear in the ledger, none for `inventory` or `py314_verification`, and the command
  prints one line per collector.
- Given the stack up, when `pixi run stack-run run_policy` runs, then a `PolicyRun` at the
  newest recorded version exists and the home page's rollup stamp moves.
- Given `pixi run stack-run dispatch_sweep sweep`, when it runs, then it exits 1 naming the
  reserved name and writes no row.
- Given `pixi run ci`, when it runs, then it exits 0.

## Spec Change Log

## Design Notes

**Eager or enqueued is settings' decision, not the command's.** Under the stack's env the
flag is `"0"` and the command enqueues; under the default environment or the tests it is
on and the task runs inline. The command reads `settings.CELERY_TASK_ALWAYS_EAGER` only to
choose which of its two report lines to print, never to change what it does.

**Why the count comes from the ledger.** `collect_sweep` returns the run state as a string;
the enqueued count lives in `DispatchOutcome` and in the dispatch row's `detail`. Reading
the newest dispatch row for that collector after an eager run is the one honest source
without changing the task's return contract (Block If).

**Why validation duplicates the dispatcher's refusals.** `dispatch()` refuses a bad name too,
but inside the run recorder, leaving a `failed` ledger row. A typo on the command line should
not be a run on the record; the command refuses first, with the same vocabulary.

## Verification

**Commands:**
- `pixi run test` and `pixi run -e dev python -m pytest tests/integration/django_apps/test_operator_commands.py -q` -- green.
- `pixi run docker-up && pixi run stack-run dispatch_sweep --all` -- nine lines; `pixi run stack-run dispatch_sweep sweep` -- exit 1.
- `pixi run stack-run run_policy` -- one line naming the version and rollup rows.
- `pixi run ci` (stack down) -- exit 0.

## Auto Run Result

Status: done
Review: three layers; fourteen patches applied. Two were high -- the "newest recorded
version" default was a lexicographic sort verified only against itself (now a numeric key
with a pinned oracle), and both eager report paths read "the newest row" rather than the row
this invocation produced (now a pk watermark taken before the enqueue). Deferred: the seeder's
duplicate of the version sort, to S03, which rewrites the seeder.

## Suggested Review Order

**What a command does, in order: validate, enqueue, report**

- Entry point: every refusal before any enqueue; the watermark; one line per collector
  [`dispatch_sweep.py:213`](../../../src/django_apps/conda_sentinel/collectors/management/commands/dispatch_sweep.py#L213)

- The newest recorded version under a numeric ordering, and why the string sort was wrong
  [`run_policy.py:100`](../../../src/django_apps/conda_sentinel/policies/management/commands/run_policy.py#L100)

- Only a row above the watermark is this run's; none is reported as unrecorded, never 0
  [`run_policy.py:211`](../../../src/django_apps/conda_sentinel/policies/management/commands/run_policy.py#L211)

- Eager or enqueued is the Celery app's decision; the command only picks its report line
  [`operator_commands.py:1`](../../../src/django_apps/conda_sentinel/core/operator_commands.py#L1)

**The deployment contract**

- Three admin processes beside `prune`; `sweep` is a one-shot, not a cadence
  [`component.toml:209`](../../../component.toml#L209)

- Root tasks, default environment, no env, outside the celery region
  [`pixi.toml:501`](../../../pixi.toml#L501)

**Tests**

- The oracle for "newest", derived from the TOML independently of the command
  [`test_operator_commands.py:207`](../../../tests/unit/django_apps/test_operator_commands.py#L207)

- A dispatch over a real selection: `offered` read off the row the dispatcher wrote
  [`test_operator_commands.py:`](../../../tests/integration/django_apps/test_operator_commands.py#L)

**Operator surface**

- Dispatching by hand, the `skipped` overlap, and what the three processes need
  [`operations.md:1274`](../../../docs/conda-sentinel/operations.md#L1274)
