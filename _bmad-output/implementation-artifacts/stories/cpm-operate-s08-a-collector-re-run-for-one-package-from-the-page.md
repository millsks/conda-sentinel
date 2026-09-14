---
title: 'CPM-OPERATE-S08: A collector re-run for one package, from the page'
type: 'feature'
created: '2026-09-13'
status: 'done'
review_loop_iteration: 1
followup_review_recommended: false
warnings: [oversized]
deferred: []
baseline_revision: '71c381ffaff1c97bcfb7ecf98da98b7d6259cfee'
context:
  - _bmad-output/implementation-artifacts/epic-operate-context.md
  - _bmad-output/implementation-artifacts/stories/cpm-operate-s03-the-inventory-is-a-governed-table.md
---

<intent-contract>

## Intent

**Problem:** After an override or a fix, a reviewer waits a day for the daily sweep or asks an
operator for a shell; `CPM-UJ-1`'s manual recollection has no surface.

**Approach:** "Collect now" on the package page for a user holding the recollect permission:
the request writes one audit row and enqueues, by task name, one per-package collection with
`force=True` for every swept collector that can be asked about that package; it is refused
while any of the package's runs is still in flight; the page shows the runs as they finalise.

## Boundaries & Constraints

**Always:**
- **Route and view**: `packages/<str:canonical_name>/recollect/`, `PackageRecollectView(RoleRequiredMixin, View)`
  in the `ReportExportView` shape -- `post` only (`get` → 405), `required_roles = {SECURITY_REVIEWER, LEADERSHIP}`;
  the detail view gains no write method (its 405 case stays). Success: `messages.success` naming
  the collectors offered and any not offered, then `303` back to the detail page; a service
  refusal → `PermissionDenied` (403) for the permission, else the detail page re-rendered with
  the refusal and status 409 for "in flight" / 400 otherwise.
- **Permission** `collectors.recollect_package` (`RECOLLECT_PERMISSION` in `core/roles.py`),
  attached on the audit model's `Meta.permissions`, granted to `SECURITY_REVIEWER` and
  `LEADERSHIP` by `core/migrations/0013_grant_recollect` in `0011`'s shape (provision only its
  own codename); `test_roles.py` tuples updated; the service's `has_perm` recorded in
  `test_permission_audit.py`'s exemptions.
- **Service** `collectors/recollection.py` `request_recollection(*, package_id, actor, clock) -> RecollectionReceipt`:
  refuses an actor without the permission (logged with the actor, `authorization.recollection_refused`);
  refuses when the package has an unfinished `CollectionRun` started within the last hour
  (`RECOLLECTION_IN_FLIGHT_WINDOW`; an older open row is a killed worker's and does not wedge
  the page -- say so in the docstring and docs); selects the collectors as every registered
  one that is swept per package **and** whose `selectable_packages()` contains this package
  (a helper in `core/registry.py`, `selects(collector, package_id) -> bool`, that filters the
  selection queryset by `pk` when its model is `Package` and by `package_id` otherwise, and
  materialises a generator only when it is one); writes `PackageRecollection` inside one
  `transaction.atomic()`; then, in `transaction.on_commit`, publishes each
  `current_app.send_task(task_name(Queue.COLLECT, name), kwargs={"package_id": package_id, "force": True}, ignore_result=True)`,
  catching `kombu.exceptions.OperationalError` per task and logging it (the audit row stands;
  the receipt names what could not be published). The service imports neither
  `collectors.sweep` nor `collectors.tasks` (the request-boundary audit); `swept_collectors()`
  moves from the `dispatch_sweep` command to `core/registry.py` and the command imports it.
- **Audit model** `PackageRecollection(AppendOnlyModel)` in `collectors/models.py` (table
  `package_recollections`): `package` FK PROTECT, `actor` FK PROTECT (non-null), `collectors`
  JSONField (the names published, in registry order), `not_offered` JSONField (names whose
  selection did not contain the package), `trace_id`, `observed_at`; index `(package, -observed_at)`;
  added to `EVIDENCE_MODEL_LABELS`, to `core/retention.py`'s `EXCLUDED_EVIDENCE` (one row per
  human act) and to `test_retention.py`'s literal exclusion set.
- **Page**: the detail template gains a "Collect now" form (CSRF, `{% url %}`) shown only when
  `request.user.has_perm(RECOLLECT_PERMISSION)` is exposed by the view context as a boolean
  (the template never calls `has_perm` -- the view computes `can_recollect`; the permission
  audit licenses views? it licenses the *service*; the view reads a context flag the service
  module computes, `can_request(actor) -> bool`, so `has_perm` stays in one licensed module);
  an "In flight" panel above the runs panel listing the package's unfinished runs (collector,
  started); the last recollection (actor, when, collectors) beneath the runs panel;
  `RunState` joins `TONED_VOCABULARIES` with five tones (`running` info, `succeeded` ok,
  `partial` warn, `failed` critical, `skipped` plain) so the chips stop rendering plain.
- **Trace**: the audit row's `trace_id` is `current_trace_id()`; the Celery instrumentor
  propagates the request's trace into the published tasks, so each run's `trace_id` matches;
  say so in the docs (`CPM-AD-15`).
- **Docs**: `the-queues.md`/`running-it.md` screens table (the button), `operations.md` manual
  recollection section (what is offered, what a refused collector's `failed` row means, the
  in-flight rule and the killed-worker hour), `authorization.md` role table (+ recollect on
  two roles) and the refusal event, `asynchronous-work.md` (the recollection publishes the
  same per-package tasks with `force`).
- **Tests**: page -- 200 with the button for a reviewer, no button for a packaging engineer,
  405 on GET, 403 for a packaging engineer's POST and for a reviewer whose group lost the
  permission, 302 anonymous, 303 + audit row + one `send_task` per selected collector with
  `force=True` (patch `current_app.send_task`, execute on-commit callbacks), 409 while a run
  started five minutes ago is unfinished, allowed when the only open run is two hours old,
  the in-flight panel and the last-recollection panel render; service -- every refusal, the
  selection helper over each selection shape (`Package` queryset, `PackageMapping` queryset,
  the empty generator), publish failure logged and named in the receipt; `test_tone.py`'s
  five tones; registry/retention/roles/permission-audit rosters.
- Sprint status: `cpm-operate-s08-a-collector-re-run-for-one-package-from-the-page: done`.

**Block If:** a per-package task lacks `force` (`verify_py314_build` -- it is not swept, so it
is never selected; if the selection helper ever offers it, stop); the request-boundary audit
cannot be satisfied without an exemption.

**Never:** a write method on `PackageDetailView`; importing `collectors.sweep`/`collectors.tasks`
from anything a view reaches; a collection in the request; a change to any collector's
selection; bypassing the in-flight refusal.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|---|---|---|---|
| Reviewer collects | reviewer POST, no open run | audit row; one `send_task` per selected collector with `force=True`; 303; message names them | N/A |
| Not selectable | package with no PyPI mapping | `pypi_release`, `python_readiness` not published; named in `not_offered` and the message | N/A |
| In flight | an unfinished run started 5 min ago | 409, refusal names the collector and when it started; no row, nothing published | `RecollectionInFlightError` |
| Stale open run | only open run started 2 h ago | allowed; docs say why | N/A |
| No permission | packaging engineer / reviewer without the grant | 403; logged with the actor; nothing written | `RecollectionNotPermittedError` |
| Anonymous | no session | 302 to sign-in | N/A |
| GET | GET on the route | 405 | N/A |
| Broker down | `send_task` raises `OperationalError` for one name | audit row stands; that name in the receipt's `unpublished`; message says so; others published | logged |
| Unknown package | name resolves nothing | 404 | N/A |

</intent-contract>

## Code Map

- Detail view: `surface/views.py:293` `PackageDetailView` (:336-355 context: `runs=recent_runs(...)`);
  `surface/urls.py:55`; template `templates/conda_sentinel/package_detail.html:7-19` header, :228-249 runs panel;
  `surface/detail.py:510-527` `recent_runs`, :120 `HISTORY_LIMIT`. Pinned 405: `test_package_detail_view.py:800-814`.
- POST shapes: `surface/views.py:589` `ReportExportView` (`View` with post), :869-1036 `InventoryView`
  (`_refused`, 303, messages); templates `inventory.html:28-55`.
- Boundary: `tests/unit/django_apps/test_request_boundary_audit.py:60-95` (forbidden: `core.transport`,
  `collectors.tasks`, `core.policy_run`, `collectors.sweep`; recorded edge `core.registry -> core.collection`).
  Publish precedent: `core/jobs.py:280-322` (`send_task` in `on_commit`, `OperationalError` caught);
  `core/queues.py:141` `task_name(queue, verb)`.
- Selection: `core/registry.py:161` `registered_collectors()`; `collectors/management/commands/dispatch_sweep.py:119-131`
  `swept_collectors()` (to relocate); selection shapes: `source_release.py:1210`/`resolve_identity.py:1224`
  (`Package` pk), `pypi_release.py:896`/`python_readiness.py:1443`/`feedstock.py:1532` (`PackageMapping`
  `package_id`), `vulnerability.py:1333`/`kev.py:1949` (empty generator when unsourced),
  `license.py:1287`/`conda_package.py:1338` (`none()` or all). Base `collect(*, package_id, force)`
  `core/collection.py:1245`, window bypass :1324.
- Permission: `core/roles.py:98-112,160-164`; `core/migrations/0011_grant_inventory_change.py` (next: `0013`);
  `collectors/inventory.py:872-900` `_require_permitted` and its event; `tests/unit/django_apps/test_roles.py:246-275`;
  `test_permission_audit.py:107-110`; `core/permissions.py:264-350` `RoleRequiredMixin`.
- Audit model template: `collectors/models.py:3472` `InventoryChange`; JSON precedent `core/models.py` `BackgroundJob.parameters`;
  rosters `tests/unit/test_model_registry.py:102-118`, `core/retention.py:456` `EXCLUDED_EVIDENCE`,
  `tests/unit/django_apps/test_retention.py:290-298`.
- In flight: `core/models.py:904-986` `CollectionRun` (indexes on `package_id`, `finished_at`, `started_at`),
  :764 `unfinished()`; `core/ledger.py:127` `current_trace_id`.
- Tones: `surface/tone.py:90-182`; `core/runs.py:55-94` `RunState`; `tests/unit/django_apps/test_tone.py:69,147`.
- Trace propagation: `src/config/observability/telemetry.py:205-206` (Django + Celery instrumentors).
- Tests: `tests/integration/django_apps/test_inventory_page.py:75-114,154-194,363-376`;
  `test_package_detail_view.py:103-212,819-843`; `test_background_exports.py:520-560` (`send_task` patched
  + `django_capture_on_commit_callbacks`); `tests/celery_tasks.py`.
- Docs: `asynchronous-work.md:100-102`; `operations.md:1162,1388-1404`; `running-it.md:536-544`;
  `authorization.md:100-104,127-131,170-175`; `index.md:110`; `test_documented_subsystems.py:111` (refusal event).
- Read-only: every collector, `collectors/sweep.py`, `collectors/tasks.py`, `core/collection.py`.

## Tasks & Acceptance

**Execution:**
- [x] `core/registry.py` -- `swept_collectors()`, `selects(collector, package_id)`; `dispatch_sweep.py` imports the former.
- [x] `collectors/models.py` + migration -- `PackageRecollection`; `core/roles.py` + `core/migrations/0013_grant_recollect`.
- [x] `collectors/recollection.py` -- the service, `can_request`, errors, events.
- [x] `surface/views.py`, `surface/urls.py`, `surface/detail.py` (in-flight and last-recollection helpers), template, `surface/tone.py`.
- [x] Tests listed in Boundaries; rosters.
- [x] Docs listed in Boundaries.
- [x] `sprint-status.yaml` -- story `done`.

**Acceptance Criteria:**
- Given the stack up and `security-persona` signed in on `django`'s page, when "Collect now" is
  pressed, then an audit row names the actor, the worker runs the selected collectors with
  `force=True`, and the page shows them in flight then finalised with the same trace id.
- Given a second press within a minute, when the runs are still open, then the page refuses
  with 409 and names the run in flight.
- Given `pixi run ci`, when it runs, then it exits 0.

## Spec Change Log

- 2026-09-14, review loop 1: the in-flight refusal reads `package_recollections` as well as the
  run ledger (the queue-latency gap) under a `select_for_update` on the package row; the window is
  `2 x CELERY_TASK_TIME_LIMIT` read at call time (was a fixed hour) and is passed to `send_task` as
  `expires` with `retry=False`; the publish boundary catches `Exception` (was `OperationalError`
  only) and `celery` is imported inside it; `RecollectionNothingOfferedError` refuses before any
  write when no collector offers the package; `runs_in_flight` lives in `core/ledger.py`;
  `can_recollect` is the intersection of the service's `can_request` and the view's roles; the
  audit row's `collectors` means "asked for"; an anonymous POST is sent to sign-in with the detail
  page as `next`; the service refuses to run inside an outer transaction.

## Design Notes

**Why publish by name.** The web process must never import the collectors (`CPM-AD-9`; the
request-boundary audit enforces it). `core/jobs.py` already publishes exports by task name
in `on_commit`; a recollection is the same shape with a different name and `force=True`.

**Why only selectable collectors.** A forced recollection of a collector that cannot ask
about the package writes a `failed` run with a refusal on the record. Offering only what
each collector's own selection contains keeps the ledger honest and the message useful; the
names left out are on the audit row.

**Why twice the task time limit bounds "in flight".** A killed worker leaves a `running` row
by design and no live run can outlast `CELERY_TASK_TIME_LIMIT`, so a row older than twice that
limit is a killed worker's and must not lock a reviewer out of the package. The window is read
from settings at call time and every published task carries it as `expires`.

**Why "in flight" asks two questions.** A run row exists only once a worker opens the task; a
press whose tasks are still queued has no run to find. So a press is refused when a run is
unfinished within the window *or* when a `package_recollections` row within the window has not
been answered by a finished run per asked collector. The check and the insert run with the
package row locked, so two concurrent presses serialise.

## Verification

**Commands:**
- `pixi run test`; `pixi run -e dev python -m pytest tests/integration/django_apps/test_recollection.py tests/integration/django_apps/test_package_detail_view.py tests/unit/django_apps/test_tone.py -q` -- green.
- Stack up (background honcho), sign in as `security-persona`, press the button on `django`, watch the in-flight panel and the ledger; press again → 409; `pixi run local-stack-down`.
- `pixi run ci` (stack down) -- exit 0.

## Auto Run Result

- **Gate:** `pixi run ci` exit 0 on 2026-09-14 (stack down, foreground): pre-commit, build,
  typecheck, lint, then 11479 passed, 2 skipped, coverage 99.10% (floor 90).
- **Review loop 1:** three reviewers (blind hunter, edge-case hunter, verification-gap); fifteen
  patches applied, all recorded in the Spec Change Log. The queue-latency gap in the in-flight
  guard is closed and its deferred entry marked RESOLVED; the `core/0005`/`0011`
  `models_module` leak found on the way is a new deferred entry.
- **Live:** stack up, `security-persona` on `django`'s page: press → audit row, forced runs with
  the page's trace id, in-flight panel then finalised; second press → 409 naming the press.
- **Environment note:** two gate runs exited 3 on coverage's own save with every test passed;
  traced to the global Stop hook's concurrent `pixi run ci`, not to this story. Recorded in
  memory; the gate above ran alone.

## Suggested Review Order

1. `collectors/recollection.py` -- the service: two-question in-flight rule, row lock, window,
   publish boundary (`expires`, `retry=False`, `except Exception`), nothing-offered refusal.
2. `core/registry.py` `swept_collectors()` / `selects()` -- shapes accepted and refused.
3. `surface/views.py` `PackageRecollectView` + `can_recollect`; `surface/urls.py`;
   `surface/detail.py` `InFlight`, `last_recollection`; `core/ledger.py` `runs_in_flight`.
4. `collectors/models.py` `PackageRecollection` + `collectors/migrations/0015`;
   `core/roles.py` + `core/migrations/0013_grant_recollect`; `core/retention.py` exclusion.
5. `templates/conda_sentinel/package_detail.html`, `surface/tone.py`, stylesheet.
6. `tests/integration/django_apps/test_recollection.py` (the transactional cases and
   `_provisioned()`), `test_package_detail_view.py`, `tests/unit/django_apps/test_registry.py`.
7. Docs: `operations.md`, `authorization.md`, `asynchronous-work.md`, `the-queues.md`,
   `running-it.md`; `deferred-work.md` entries.
