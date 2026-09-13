---
title: 'CPM-OPERATE-S01: A shell against the stack carries the stack'"'"'s environment'
type: 'feature'
created: '2026-09-13'
status: 'done'
review_loop_iteration: 0
followup_review_recommended: false
warnings: []
deferred: []
baseline_revision: '19ee8b4167ddd2b31d4eb76898d91fcf961f4b52'
context:
  - _bmad-output/implementation-artifacts/epic-operate-context.md
  - _bmad-output/implementation-artifacts/stories/cpm-platform-s06-the-stack-seeds-what-it-serves.md
---

<intent-contract>

## Intent

**Problem:** Every hand-run action against the local stack is a `manage.py shell -c` that
must carry `DATABASE_URL`, `REDIS_URL` and `CELERY_TASK_ALWAYS_EAGER=0`, or it silently
talks to SQLite and runs the task inline in the shell -- which is what happened on
2026-09-12 when a resolver loop "succeeded" against the wrong database. The `stack-*`
tasks closed this for migrate and the seeders (`CPM-PLATFORM-S06`); a shell and an
arbitrary management command have no such task.

**Approach:** Two tasks in the `dev` feature carrying the stack's environment exactly as
`local-stack` does: `stack-shell` opens `manage.py shell`; `stack-run` is `manage.py`
with whatever follows appended, so `pixi run stack-run showmigrations collectors` runs
against the stack. The reconciliation test sweeps both, and every documented
environment-prefixed one-liner becomes one of them.

## Boundaries & Constraints

**Always:**
- Both tasks live in `[feature.dev.tasks]` beside `stack-migrate`, with `env` = the same
  three variables and values `local-stack` declares (`CELERY_TASK_ALWAYS_EAGER = "0"`), and
  `default-environment = "dev"`; neither declares `COMPONENT_RUNTIME` (the locality test)
  nor `COMPONENT_PROCESS` (the process-group test); neither carries `depends-on` -- an
  operator's shell must not start containers as a side effect.
- `tests/unit/test_local_stack.py`: both names join `OUTSIDE_THE_PROCESS_GROUP` and
  `AGAINST_THE_STACK`; a new case pins that every task in `AGAINST_THE_STACK` whose
  `env` carries `CELERY_TASK_ALWAYS_EAGER` agrees with `local-stack` on it, except
  `stack-seed`, whose `"1"` is deliberate and named in the assertion.
- Docs: every `pixi run -e dev python manage.py shell` and every
  `export DATABASE_URL=...` recipe aimed at the stack in `docs/conda-sentinel/*.md` and
  `README.md` becomes `pixi run stack-shell` / `pixi run stack-run ...`; a shell aimed at
  SQLite on purpose (the onboarding registry exercise) stays as it is and says so.
  `operations.md`'s quoting warning (outer single quotes for `shell -c`) is referenced,
  not repeated, from `running-it.md`'s stack section.
- `docs/conda-sentinel/running-it.md`'s "Docker on its own" table gains the two rows;
  the local-stack comment block in `pixi.toml` names them.
- Sprint status: `cpm-operate-s01-a-shell-against-the-stack-carries-its-environment: done`
  and `epic-operate: in-progress`.

**Block If:** a fourth environment variable on either task; any change to
`local-stack`'s own `env`.

**Never:** a task that opens a shell against SQLite under a `stack-` name; a
`depends-on` on either task; a change to `config/`.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|---|---|---|---|
| Declaration | `pixi.toml` parsed | `stack-shell.cmd == "python manage.py shell"`, `stack-run.cmd == "python manage.py"`, both `default-environment == "dev"` in `[feature.dev.tasks]` | test names the task |
| Environment | each task's `env` | `DATABASE_URL` names 5433, `REDIS_URL` names 6380, `CELERY_TASK_ALWAYS_EAGER == "0"` -- byte-equal to `local-stack`'s values | test names the differing key |
| No side effects | each task | no `depends-on`; no `COMPONENT_RUNTIME`; no `COMPONENT_PROCESS` | existing audits plus one new case |
| Eager agreement | every `AGAINST_THE_STACK` task with the flag | agrees with `local-stack` except `stack-seed`, whose `"1"` is named | test names the task |
| Docs | `docs/conda-sentinel/*.md`, `README.md` | no line contains `export DATABASE_URL`; no `manage.py shell` aimed at the stack without `stack-shell`/`stack-run`; the one SQLite-on-purpose exercise is annotated | test lists offending lines |

</intent-contract>

## Code Map

- `pixi.toml:463-465` `manage` -- the pattern: `cmd = "python manage.py"`, trailing arguments
  appended by pixi; `default` environment, which cannot see the domain apps (memory:
  management commands need the dev environment). `:686-696` the `stack-*` block and
  `local-stack`'s `env` (the values to copy verbatim); `:640-648` the comment block to extend.
- `tests/unit/test_local_stack.py:52` `OUTSIDE_THE_PROCESS_GROUP`, `:83` `AGAINST_THE_STACK`,
  `:255` `test_the_stack_points_at_the_containers_it_started` (the eager assertion to mirror),
  `:274` `test_every_stack_task_names_the_same_database`; `tasks()` helper reads every feature.
- `tests/unit/test_locality_declaration.py` -- refuses any task declaring `COMPONENT_RUNTIME`;
  `tests/unit/test_process_model.py` -- reconciles `COMPONENT_PROCESS` tasks.
- Docs to convert: `docs/conda-sentinel/running-it.md:183-192` (the env-prefixed dispatch
  recipe), `:236` (policy run by hand), `:355-357` (the manual PostgreSQL sequence: the
  `export` + `migrate` become `stack-migrate`, the seed becomes `stack-seed`), the "Docker on
  its own" table `:283`; `asynchronous-work.md:168,186`; `onboarding.md:236` (SQLite on
  purpose -- keep, annotate); `operations.md:1672-1699` (quoting warning -- reference it);
  `README.md:70-85` quick-start block. The runbook artifact is outside the repo.
- Read-only: `src/`, `compose.yaml`, `Procfile`, `scripts/`.

## Tasks & Acceptance

**Execution:**
- [x] `pixi.toml` -- `stack-shell` and `stack-run` in `[feature.dev.tasks]`; comment block.
- [x] `tests/unit/test_local_stack.py` -- rosters; the eager-flag agreement case; a case that
  neither new task carries `depends-on`.
- [x] `docs/conda-sentinel/running-it.md`, `asynchronous-work.md`, `onboarding.md`, `README.md`
  -- convert or annotate each snippet named in the Code Map.
- [x] `_bmad-output/implementation-artifacts/sprint-status.yaml` -- story `done`, epic
  `in-progress`.

**Acceptance Criteria:**
- Given the containers up, when `pixi run stack-run showmigrations collectors` runs, then it
  prints the stack database's state and exits with the command's status.
- Given `pixi run stack-run shell -c 'from django.conf import settings; print(settings.CELERY_TASK_ALWAYS_EAGER)'`,
  when it runs, then it prints `False`.
- Given `grep -rn 'export DATABASE_URL' docs README.md`, when it runs after the change, then
  it matches nothing.
- Given `pixi run ci`, when it runs, then it exits 0.

## Spec Change Log

## Design Notes

**Why `stack-run` is `manage.py` and not a generic runner.** Every hand-run action the
operator needs is a management command or a shell; a generic runner would also need the
env for `python -m ...` module runs, and the two that exist (`seed`, `seed_demo`) already
have their own `stack-*` tasks. Trailing arguments are what pixi appends, so no wrapper
script is needed and the Windows caveat that applies to `honcho` does not apply here.

**Why no `depends-on`.** `stack-migrate` depends on `docker-up` because a migration with
no database is meaningless; a shell against a stopped stack should fail with PostgreSQL's
refusal, which is the honest answer, rather than start containers the operator did not
ask for.

## Verification

**Commands:**
- `pixi run test` -- expected: green, `test_local_stack.py` sweeping seven stack tasks.
- `pixi run docker-up && pixi run stack-run showmigrations collectors` -- expected: `[X] 0012_...`.
- `pixi run stack-run shell -c 'from django.conf import settings; print(settings.CELERY_TASK_ALWAYS_EAGER)'` -- expected: `False`.
- `pixi run ci` -- expected: exit 0.

## Auto Run Result

Status: done
Review: three layers (blind, edge-case, verification-gap); eleven patches applied, none deferred.
One reject: an integration test spawning `pixi run` from inside pytest (fragile under the
pixi lock while the stack runs, and on Windows); the live checks are in Verification.
Deviation recorded: the eager flag is compared wherever a task declares it, except the
seeder -- `stack-migrate` and `stack-personas` declare none and enqueue nothing.

## Suggested Review Order

**The two tasks**

- Entry point: `manage.py` with trailing arguments, in the dev environment, with the stack's three variables
  [`pixi.toml:715`](../../../pixi.toml#L715)

**What the tests pin**

- Every stack task carries the same database and broker, byte for byte
  [`test_local_stack.py:413`](../../../tests/unit/test_local_stack.py#L413)

- The eager flag agrees with `local-stack` wherever a task declares it; the seeder is the named exception
  [`test_local_stack.py:381`](../../../tests/unit/test_local_stack.py#L381)

- A shell never starts containers as a side effect
  [`test_local_stack.py:482`](../../../tests/unit/test_local_stack.py#L482)

- No page hand-assembles the environment; every bare shell says it is aimed at SQLite
  [`test_local_stack.py:549`](../../../tests/unit/test_local_stack.py#L549)

**Operator surface**

- The section every hand-run recipe now points at
  [`running-it.md:386`](../../../docs/conda-sentinel/running-it.md#L386)
