---
title: 'CPM-OPERATE-S04: Day one is observed'
type: 'feature'
created: '2026-09-13'
status: 'done'
review_loop_iteration: 0
followup_review_recommended: false
warnings: []
deferred: []
baseline_revision: '71946272c0d593f5e1224b18b54e960bf918554c'
context:
  - _bmad-output/implementation-artifacts/epic-operate-context.md
  - _bmad-output/implementation-artifacts/stories/cpm-operate-s02-ingest-sweep-and-run-are-commands.md
---

<intent-contract>

## Intent

**Problem:** Beat's interval entries start their clock when they are created, so a fresh
stack fires no daily sweep for a day and no weekly one for a week; day one is blank
unless somebody dispatches by hand.

**Approach:** A declared setting, `CPM_SWEEP_ON_BEAT_START`, off by default and on in the
dev feature's activation env. When beat starts with it on, one `cpm.collect.sweep` is
enqueued per scheduled collector, in the schedule's own order and with the schedule's own
`countdown`, and nothing about the interval entries changes; each collector's observation
window is what skips packages observed too recently.

## Boundaries & Constraints

**Always:**
- Setting: `base.py` `CPM_SWEEP_ON_BEAT_START = env.bool("CPM_SWEEP_ON_BEAT_START", default=False)`;
  the name is a constant in the app (`collectors/sweep.py` `SWEEP_ON_BEAT_START_SETTING`);
  `pixi.toml` `[feature.dev.activation.env]` declares `"1"`; no production-bound table or task
  `env` carries it; every settings module other than the activation env resolves `False`.
- Receiver: `@beat_init.connect` in `src/config/celery_app.py`, same shape as the
  `worker_ready` one -- function-local imports, `# type: ignore[untyped-decorator]`; it
  reads `settings.CPM_SWEEP_ON_BEAT_START` and `settings.CELERY_BEAT_SCHEDULE`, and delegates
  to `collectors.sweep.dispatch_scheduled_collectors_on_start(schedule)`.
- `sweep.py`: a public `scheduled_dispatches(schedule) -> tuple[ScheduledDispatch, ...]`
  (collector name, interval, countdown seconds or 0) in the schedule's declaration order,
  reading only entries whose `task == SWEEP_TASK_NAME` (share the walk with
  `_scheduled_dispatches`); and `dispatch_scheduled_collectors_on_start(schedule)` that,
  for each, calls `collect_sweep.apply_async(kwargs={COLLECTOR_KWARG: name}, countdown=offset)`
  and logs one event per dispatch naming the collector, the countdown and the task id, and
  one summary event.
- Under eager Celery (`current_app.conf.task_always_eager`) the start dispatch is refused
  with one logged event saying the beat process has no worker to hand a sweep to, and
  nothing is enqueued -- a bare `pixi run -e dev beat` outside the stack must not run nine
  collectors inline inside the scheduler.
- The receiver enqueues; it never calls `dispatch()` directly, opens no transaction, and
  writes no ledger row. The dispatch's own `skipped` for an overlapping previous dispatch
  and each collection's observation-window skip are what make a restart harmless.
- Reconciliation at boot (`cadence_reconciliation_fault`) is untouched and still refuses
  drift; `CELERY_BEAT_SCHEDULE` is not modified by this story (the settings test pins it).
- Docs: `asynchronous-work.md` "No sweep fires when the stack starts" → what fires at start
  with the setting on, that a beat restart re-dispatches and the windows skip, and that
  deployed it is off; `running-it.md` the same sentence; `operations.md` settings table
  (+1) and the hand-dispatch section says day one on the stack no longer needs it.
- Tests: `tests/unit/test_celery_app.py` -- the receiver is connected to `beat_init`; called
  with the setting off it enqueues nothing; on and eager it refuses with the event and
  enqueues nothing; on and not eager (`apply_async` patched) it enqueues once per scheduled
  entry, in schedule order, with each entry's countdown and `kwargs`; `test_settings.py` --
  the default in every settings module and the activation env; `tests/unit/django_apps/test_sweep.py`
  -- `scheduled_dispatches` over the real schedule equals the nine with the three offsets,
  and ignores an entry whose task is not the sweep.
- Sprint status: `cpm-operate-s04-day-one-is-observed: done`.

**Block If:** the only way to enqueue with a countdown changes the task's signature; a
second beat replica is needed.

**Never:** a change to `CELERY_BEAT_SCHEDULE`; a cadence anywhere but the scheduler; a
dispatch that bypasses a collector's observation window; the receiver importing anything
at module scope.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|---|---|---|---|
| Off | setting False, beat starts | receiver returns; nothing enqueued; no event | N/A |
| On, worker | setting True, eager False | nine `apply_async` calls in schedule order; kev +3600, license +7200, python_readiness +10800, others 0; one event each + summary | N/A |
| On, eager | setting True, eager True | refused with one event; nothing enqueued | logged, never raised |
| Broker down | `apply_async` raises `OperationalError` | logged per collector, remaining entries still attempted, beat still starts | never raised out of the receiver |
| Restart same day | previous dispatch rows finished | new dispatches; collections inside their window write `skipped` | N/A |
| Foreign entry | a schedule entry whose task is not the sweep | ignored by `scheduled_dispatches` | N/A |

</intent-contract>

## Code Map

- `src/config/celery_app.py:29-32,61-80` -- the `worker_ready` receiver: decorator form, deferred
  imports and why (`config/__init__.py` imports the app). `tests/unit/test_celery_app.py:25-91` --
  `_connected_receivers`, the template cases.
- `collectors/sweep.py:166` `SWEEP_TASK_NAME`, :181 `COLLECTOR_KWARG`, :470 `_already_draining`,
  :505 `dispatch`, :807 `_scheduled_dispatches(schedule)` (walks the schedule; extend to carry
  `options.countdown`), :930 `cadence_reconciliation_fault`. `collectors/tasks.py:1929` `collect_sweep`.
- `core/collection.py:1737` `_inside_window`; windows are `CADENCE / 2` on every swept collector;
  `tests/integration/django_apps/test_sweep.py:733` proves the in-window skip.
- `core/operator_commands.py:37` `runs_eagerly()` -- reuse for the eager guard.
- Settings: `base.py:404` the `CPM_INVENTORY_SOURCE` read (the shape to copy), :749-828 the
  schedule; `pixi.toml:443-453` activation env; `tests/unit/test_settings.py:1279-1470` the pins;
  `tests/unit/django_apps/test_inventory_source.py:224-233` the activation-env test shape;
  `tests/unit/test_locality_declaration.py:305-504`.
- Audits: `test_task_declaration_audit.py:130-177` (no assignment to the schedule outside
  settings -- reading is fine; `countdown` is not banned).
- Docs: `asynchronous-work.md:111-181`; `running-it.md:176-192`; `operations.md:14-60,1266-1372`.
- Read-only: `CELERY_BEAT_SCHEDULE`, `collectors/tasks.py`, the collectors.

## Tasks & Acceptance

**Execution:**
- [x] `collectors/sweep.py` -- `SWEEP_ON_BEAT_START_SETTING`, `ScheduledDispatch`, `scheduled_dispatches`,
  `dispatch_scheduled_collectors_on_start`; the shared schedule walk.
- [x] `src/config/celery_app.py` -- the `beat_init` receiver.
- [x] `src/config/settings/base.py` -- the setting; `pixi.toml` -- activation env.
- [x] Tests listed in Boundaries.
- [x] Docs listed in Boundaries.
- [x] `sprint-status.yaml` -- story `done`.

**Acceptance Criteria:**
- Given a fresh stack, when `pixi run local-stack` starts, then within the first minute the
  ledger holds one dispatch row per scheduled collector (kev, license, python_readiness
  arriving after their offsets) and none for `inventory` or `py314_verification`.
- Given `pixi run -e dev beat` alone (eager), when it starts, then the log carries the
  refusal event and the ledger gains no row.
- Given `pixi run ci`, when it runs, then it exits 0.

## Spec Change Log

## Design Notes

**Why a receiver and not a schedule entry.** Cadence is data (`CPM-AD-20`) and the
reconciliation refuses an entry that is not a collector's cadence; a "once at start" is
not a cadence. The receiver adds one dispatch per collector at the moment beat has a
broker and a schedule, and touches neither.

**Why off by default.** Deployed, beat restarts on every deploy; a dispatch on each would
be a second sweep a day on top of the tick, harmless because of the windows but noisy on
the Coverage screen. Locally a fresh volume is the common case and the day-one gap is the
whole complaint.

## Verification

**Commands:**
- `pixi run test` -- green.
- `pixi run local-stack` then, after ~60 s, `pixi run stack-run shell -c 'from conda_sentinel.core.models import CollectionRun; print(sorted(CollectionRun.objects.filter(package__isnull=True).order_by("-pk")[:9].values_list("collector", flat=True)))'` -- the nine (offsets still pending for three).
- `pixi run ci` (stack down) -- exit 0.

## Auto Run Result

Status: done
Review: three layers; the verification-gap layer found nothing; thirteen patches from the
other two. Notable: the receiver now fails closed on an absent schedule or a non-boolean
setting; an entry's whole `options` are forwarded, not only `countdown`; a broker
refusal ends the start dispatch rather than paying nine retry windows; and
`CELERY_BROKER_TRANSPORT_OPTIONS.visibility_timeout` is set above the largest countdown,
closing a Redis redelivery of the +2 h and +3 h dispatches that predates this story via
beat's own tick.

## Suggested Review Order

**One dispatch per scheduled collector, at the moment beat has a broker**

- Entry point: the `beat_init` receiver -- fail closed on the setting and the schedule, delegate
  [`celery_app.py:94`](../../../src/config/celery_app.py#L94)

- The schedule read in its own order with its own options; refuse under eager; stop on a broker refusal
  [`sweep.py:1022`](../../../src/django_apps/conda_sentinel/collectors/sweep.py#L1022)

- What a schedule entry becomes, and what a countdown is allowed to be
  [`sweep.py:987`](../../../src/django_apps/conda_sentinel/collectors/sweep.py#L987)

**The declarations**

- Off by default, and the one canonical statement of why
  [`base.py:419`](../../../src/config/settings/base.py#L419)

- A visibility timeout above every declared countdown
  [`base.py:881`](../../../src/config/settings/base.py#L881)

**Tests**

- The real signal, through Celery's `sender=`
  [`test_celery_app.py:194`](../../../tests/unit/test_celery_app.py#L194)

- Nine enqueues in schedule order with each entry's options
  [`test_celery_app.py:315`](../../../tests/unit/test_celery_app.py#L315)

- The timeout exceeds every countdown in every settings module
  [`test_settings.py:1625`](../../../tests/unit/test_settings.py#L1625)
