---
title: 'CPM-OPERATE-S09: An operator digest'
type: 'feature'
created: '2026-09-14'
status: 'done'
review_loop_iteration: 1
followup_review_recommended: true
warnings: [oversized]
deferred:
  - summary: >-
      Older digests have no detail route; the page shows the newest in full and the previous thirty as a list of date, changed and delivery states only.
    evidence: |-
      surface/views.py DigestView renders latest + history; the story asks for "a row an operator can read", which the newest satisfies, but yesterday's text is unreachable from the page once a new row lands.
    location: >-
      src/django_apps/conda_sentinel/surface/views.py DigestView
    severity: low
baseline_revision: 'a184fc90697183bfb23203c094947d859f9a2fe8'
context:
  - _bmad-output/implementation-artifacts/epic-operate-context.md
  - _bmad-output/implementation-artifacts/stories/cpm-operate-s08-a-collector-re-run-for-one-package-from-the-page.md
---

<intent-contract>

## Intent

**Problem:** A sweep that fails for three days is found on the Coverage screen when a reviewer
asks; nothing tells the operator. Silence and health look the same.

**Approach:** A daily `cpm.policy.digest` task (one more `CELERY_BEAT_SCHEDULE` entry, cadence as
data) composes a digest from the run ledger, the inventory and the package table, stores it as an
append-only row an operator reads on a `/digests/` page, and delivers it to a declared webhook
URL and/or email address -- or stores it only when none is declared. A day in which nothing
changed still delivers, in one line.

## Boundaries & Constraints

**Always:**
- **Where it lives** (`CPM-AD-4`): composition, storage and delivery in `collectors/digest.py`
  (it reads `collectors`, `identity` and `core`; `core` may import none of them); the task in
  `collectors/tasks.py` as `DIGEST_TASK_NAME = task_name(Queue.POLICY, "digest")` (the name the
  epic fixes; routing follows the namespace), `@shared_task(name=DIGEST_TASK_NAME)`, no
  cadence/time-limit keywords, `def compose_operator_digest() -> int` returning the row's pk,
  constructing `SystemClock()` at the boundary and passing `clock=` down.
- **Window:** the 24 hours ending at `now`. Per registered collector (`registered_collectors()`
  order): for per-package collectors, ledger rows with `package IS NULL` are *dispatches* and rows
  with a package are *collections*; for run-scoped collectors every row is a collection; both
  counted by `RunState`. `PRUNE_COLLECTOR` rows are reported once under "overall" as prune runs by
  state, never as a collector. *Rate-limited*: failed collections whose `detail` carries a marker
  constant `ALLOWANCE_REFUSAL_MARKER` newly declared in `core/collection.py` and used by
  `_call_refusal` for both the exhausted-allowance and credential-refused-this-window texts (no
  substring guessed twice). *Past freshness target unobserved*: for each collector with a
  `freshness_target`, the count of its selectable packages (`selects`/`selectable_packages()`,
  the `Package`-or-`package_id` shapes S08's `core/registry.py` already handles) with no row of
  its `evidence_model` observed at or after `now - target`. Overall: inventory entries active and
  retired (`InventoryEntry.retired_at`), packages by confidence (resolved = verified +
  inventory-derived, unresolved = unmapped), and the newest finished `PolicyRun`'s version and
  age (or "no policy run has finished").
- **Row:** `OperatorDigest(AppendOnlyModel)` in `collectors/models.py`, table `operator_digests`:
  `window_start`, `window_end`, `figures` JSONField (the numbers above, keyed by collector name),
  `text` (the rendered digest), `changed` boolean, `deliveries` JSONField (a list of
  `{channel, target, state, detail}` with `channel` in webhook/email, `state` in
  delivered/failed, `target` the webhook's host or the address -- never the URL), `trace_id`,
  `observed_at`; index `(-observed_at)`. Registered in `EVIDENCE_MODEL_LABELS` and in
  `core/retention.py EXCLUDED_EVIDENCE` (one row a day, an operator record, never purged; say so
  next to the audit tables). Migration `collectors/0016_operator_digests`.
- **Changed?** `changed` is whether `figures` differs from the previous digest's `figures`
  (`True` when there is none). When unchanged the `text` is one line naming the previous
  digest's date and the fact that nothing changed; it is still stored and delivered.
- **Delivery declaration:** `CPM_DIGEST_WEBHOOK_URL` and `CPM_DIGEST_EMAIL`, `env.str(...,
  default="").strip()` in `base.py` beside `CPM_GITHUB_TOKEN`, cleared to `""` in
  `config/settings/test.py`, read at call time by `declared_deliveries()` in `collectors/digest.py`,
  refused at boot by `CollectorsConfig.ready()` when the URL is not `https://` or the address has
  no `@` (a fault string that never echoes the value). A webhook URL may carry a credential:
  logs, rows, details and exceptions name only `host_of(url)`. The `test_settings.py`
  "no checked-in file declares" case (`variable_sites`, compose, Dockerfile, workflows) covers
  both names; test literals use `https://hooks.example.test/digest` and `ops@example.test`.
- **Delivery:** email through `django.core.mail.send_mail` (subject
  `conda-sentinel digest YYYY-MM-DD`, `DEFAULT_FROM_EMAIL`, `fail_silently=False`, `SMTPException`
  and `OSError` caught); webhook through a new seam `core/delivery.py` -- `WebhookDeliverer`
  Protocol with `post(url, *, body: dict, timeout) -> DeliveryOutcome`, `RequestsWebhookDeliverer`
  posting JSON `{subject, text, window_start, window_end, changed, figures}` with a 10-second
  timeout and **no retry**, `raise_for_status`; every failure becomes a `failed` entry in
  `deliveries` with the exception class or status code, never raised into the beat. Both declared
  → both delivered; none → `deliveries == []` and the docs say "stored only". The webhook
  deliverer is substituted in tests by a recorded fake (`tests/collectors.py` shape); mail by
  the locmem backend (`mail.outbox`).
- **Events:** `digest.composed` (pk, window, changed, collectors counted),
  `digest.delivered` / `digest.delivery_failed` (channel, target, detail), all with `EVENT_KEYS`
  tuples; logged through `structlog.get_logger(__name__)`.
- **Schedule:** one entry `"cpm-digest": {"task": "cpm.policy.digest", "schedule":
  timedelta(days=1), "options": {"countdown": 3 * 60 * 60}}` (after the last sweep offset, so
  the first digest sees the day's dispatches). `tests/unit/test_settings.py`: the sweep-only
  assertions (`EXPECTED_SWEEP_ENTRIES`, "every entry fires the dispatch task", "exactly once",
  "phased dispatches", "only option is a countdown", "visibility timeout") iterate the sweep
  entries and a new case pins the digest entry (task name composed from `Queue.POLICY`, daily, no
  kwargs, countdown under the visibility timeout). Boot cadence reconciliation and
  `scheduled_dispatches` already skip non-sweep entries -- assert that in `test_sweep.py`.
- **Page:** `surface/views.py` `DigestView(RoleRequiredMixin, TemplateView)`, `required_roles =
  PRODUCT_ROLES`, route `digests/` named `digest`, template `conda_sentinel/digest.html`: the
  newest digest's text, figures table per collector, its deliveries and trace id, and the
  previous `DIGEST_HISTORY_LIMIT = 30` digests as a list (date, changed, delivery states); an
  empty state when none exists. Nav link in `_chrome.html` after Coverage. Reads through
  `surface/digest.py` helpers only (`latest_digest`, `digest_history`); nothing on the request
  path imports `celery`, `core.transport` or `core.delivery` (the request-boundary audit).
- **Command and process:** `collectors/management/commands/compose_digest.py` enqueuing the task
  (eager reports inline, in `run_policy.py`'s shape and `core/operator_commands.py`'s wording);
  pixi task `digest`, `component.toml [[admin_processes]]` `digest` in `policy-run`'s shape;
  `tests/unit/test_process_model.py` reconciles.
- **Tests:** `tests/unit/django_apps/test_digest.py` (composition with `FixedClock` over a
  seeded ledger: every scenario in the matrix; unchanged detection; marker constant used by
  `_call_refusal`; freshness count; text rendering), `tests/integration/django_apps/test_digest.py`
  (task stores a row; deliveries recorded with locmem mail and the recorded webhook; a refused
  webhook never surfaces the URL; page renders newest and history; empty state; roles),
  `test_settings.py` (declarations, test module empties, no checked-in file), routing/declaration
  audits, model registry and retention rosters, `test_documented_subsystems.py`,
  `test_documentation_commands.py`.
- **Docs:** `asynchronous-work.md` (task catalogue row, beat table row, the digest under
  background work), `operations.md` (a "The daily digest" section: what it counts, where it is
  read, delivery declaration, "stored only" when none, the credential rule, the by-hand
  `pixi run digest`; settings table rows; `operator_digests` in the never-purged list),
  `running-it.md` (screens list), `the-queues.md` if it lists policy-queue tasks, `index.md`.
- Sprint status: `cpm-operate-s09-an-operator-digest: done`.

**Block If:** delivering the digest would require a new runtime dependency (`requests` and
Django's mail are already present); the process model's `schedule` field admits no value that
describes a beat-fired process (then declare it as `policy-run` is and halt only if the
reconciliation test refuses it).

**Never:** a delivery from a request; a retry loop on the webhook; the URL, the address or any
credential in a log line, a row, a detail or an exception message; a digest that is skipped
because nothing changed; a change to what the sweeps or the policy run do; a `crontab` entry
(interval only, as the schedule comment requires); an `update()`/`delete()` on a digest row.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|---|---|---|---|
| Daily digest | 24 h of dispatches, collections, one finished policy run | row stored; per-collector counts by state; overall counts; `changed=True`; delivered where declared | N/A |
| Nothing changed | figures equal the previous digest's | row stored with `changed=False`, one-line text naming the previous date; still delivered | N/A |
| First digest | no previous row | `changed=True`, full text | N/A |
| Rate-limited | 3 failed collections carrying the marker for `source_release` | `rate_limited: 3` for that collector | N/A |
| Stale packages | a collector with a 2-day target, 5 selectable packages, 2 observed within it | `past_freshness_target: 3` | N/A |
| No policy run | no finished `PolicyRun` | overall says none has finished; no age | N/A |
| Nothing declared | both settings empty | row stored, `deliveries == []`, log says stored only | N/A |
| Webhook refuses | deliverer raises / 500 | entry `failed` with class or status, target is the host; task still returns the pk | logged `digest.delivery_failed` |
| Mail refuses | backend raises `SMTPException` | entry `failed`; task still returns the pk | logged |
| Credentialed URL | `https://user:secret@hooks.example.test/x` | target and logs carry `hooks.example.test` only | N/A |
| Malformed declaration | `http://...` URL or an address without `@` | boot refuses with the setting's name, never its value | `ImproperlyConfigured` |
| Page, no digest | no row | empty state, 200 | N/A |
| Page, anonymous | no session | redirect to sign-in | N/A |

</intent-contract>

## Code Map

- Schedule: `src/config/settings/base.py:754-912` (`CELERY_BEAT_SCHEDULE` at :811, rules :755-810,
  `options.countdown` :853/:871/:893); `BROKER_VISIBILITY_TIMEOUT_SECONDS` :928; `CELERY_TASK_TIME_LIMIT` :726.
  Boot reconciliation skips non-sweep entries: `collectors/sweep.py:887-906` `_dispatch_entries`, :987
  `scheduled_dispatches`, :1239 `cadence_reconciliation_fault`. Pinning tests: `tests/unit/test_settings.py:1315-1325`
  `EXPECTED_SWEEP_ENTRIES`, :1380, :1410, :1431, :1465, :1913, :1934; `tests/unit/django_apps/test_documented_subsystems.py:187,:207`.
- Tasks: `core/tasks.py:67-92` (`POLICY_RUN_TASK_NAME` composed, `@shared_task(name=...)`, `SystemClock()` at the
  boundary); `collectors/tasks.py` (collector tasks; add the digest there); `core/queues.py:94,:141,:212`;
  `config/celery_app.py:137` autodiscover. Audits: `tests/unit/django_apps/test_task_routing_audit.py:538` (sibling case),
  `test_task_declaration_audit.py:182,:702,:889`; `tests/celery_tasks.py`.
- Ledger: `core/models.py:828` `RunLedgerModel`, :904 `CollectionRun` (`collector` :945, nullable `package` :962),
  :1001 `PolicyRun` (`policy_version` :1020); `core/runs.py:55` `RunState`; dispatch rows: `collectors/sweep.py:585`
  (package NULL), `PRUNE_COLLECTOR` `core/retention.py:223`, excluded by name in `core/policy_run.py:201`.
  Per-collector aggregation shape to reuse: `surface/coverage.py:320-375` `collector_health`.
- Refusals: `core/collection.py:1799-1841` `_call_refusal` (allowance text), :1858-1868 (credential-refused text),
  events :304/:319 -- add `ALLOWANCE_REFUSAL_MARKER` and use it in both texts.
- Freshness: `core/collection.py:841` `freshness_target`; `core/freshness.py:157,:238`; selection shapes
  `core/registry.py` `selects`/`swept_collectors`/`registered_collectors` (:190-208).
- Inventory/identity: `collectors/models.py:3374` `InventoryEntry` (`retired_at`, `is_active` :3470);
  `identity/confidence.py:43`; `identity/models.py:555` `Package.confidence` :707; `surface/coverage.py:264-277`.
- Policy version: `PolicyRun.finished()`; `policies/parameters.py:1468` `newest_recorded_version`.
- Transport/mail: `core/transport.py:428` (GET-only `fetch`; do not widen it -- new `core/delivery.py`);
  `RequestsTransport` :478 for the session/timeout idiom; `tests/collectors.py:395` `RecordedTransport` shape;
  `base.py:640` `EMAIL_BACKEND`, :646 `EMAIL_TIMEOUT`; `test.py:146` locmem; `production.py:93` `DEFAULT_FROM_EMAIL`.
- Credentials: `base.py:420-444` + `test.py:133-141` (`CPM_GITHUB_TOKEN`); `collectors/github.py:159` `token_fault`,
  :229 read at call time; boot refusal `collectors/apps.py:305-316`; `core/credentials.py:47` `host_of`;
  tests `test_settings.py:1732-1777`, `tests/pixi_manifest.py:204` `variable_sites`; docs `operations.md:101,:1418`.
- Append-only/rosters: `core/models.py:581` `AppendOnlyModel`; `tests/unit/test_model_registry.py:102`;
  `core/retention.py:440-457`; `tests/unit/django_apps/test_retention.py:290-302`; newest migration
  `collectors/0015_package_recollections`; audits `test_evidence_constraint_audit.py`, `test_evidence_inheritance_audit.py:80`,
  `test_clock_audit.py:96-152` (no `auto_now`, no `timezone.now`).
- Surface: `surface/views.py:595` `CoverageView` (shape), :631 `HomeView`, :1106 `InventoryView`; `surface/urls.py:50-88`;
  `core/permissions.py:116` `PRODUCT_ROLES`, :264 mixin; nav `src/django_service/templates/conda_sentinel/_chrome.html:31-53`;
  `surface/context_processors.py:34`; templates `conda_sentinel/coverage.html`, `inventory.html`; boundary audit
  `tests/unit/django_apps/test_request_boundary_audit.py:59`.
- Command/process: `policies/management/commands/run_policy.py:117-200`; `core/operator_commands.py:29-37`;
  `component.toml:196-253`; `pixi.toml:512-528`; `tests/unit/test_process_model.py:424-550`;
  `tests/unit/test_documentation_commands.py:120`.
- Docs: `asynchronous-work.md:83-125,:270,:390`; `operations.md:1375-1418,:3271`; `the-queues.md:12,:68,:159`;
  `running-it.md:305,:535`; events pinned `test_documented_subsystems.py:49-111`.
- Events/clock: `collectors/sweep.py:234-296`, `core/collection.py:298-364` (`*_EVENT`, `EVENT_KEYS`);
  `core/clock.py:82-140`.
- Read-only: every collector, `collectors/sweep.py`, `core/policy_run.py`, `core/transport.py`.

## Tasks & Acceptance

**Execution:**
- [x] `core/collection.py` -- `ALLOWANCE_REFUSAL_MARKER` used by both refusal texts -- one phrase, counted once.
- [x] `core/delivery.py` -- `WebhookDeliverer` Protocol, `DeliveryOutcome`, `RequestsWebhookDeliverer` (10 s, no retry).
- [x] `collectors/models.py` + `collectors/migrations/0016_operator_digests.py` -- `OperatorDigest`.
- [x] `collectors/digest.py` -- `compose_digest(*, clock, deliverer=None)`, figures, text, `changed`, `declared_deliveries`, delivery, events.
- [x] `collectors/tasks.py` -- `DIGEST_TASK_NAME`, `compose_operator_digest`.
- [x] `config/settings/base.py`, `config/settings/test.py`, `collectors/apps.py` -- declarations, empties, boot faults.
- [x] `config/settings/base.py` -- the `cpm-digest` schedule entry; `tests/unit/test_settings.py` amended and extended.
- [x] `core/retention.py`, `tests/unit/test_model_registry.py`, `tests/unit/django_apps/test_retention.py` -- rosters.
- [x] `surface/digest.py`, `surface/views.py`, `surface/urls.py`, `templates/conda_sentinel/digest.html`, `_chrome.html` -- the page.
- [x] `collectors/management/commands/compose_digest.py`, `pixi.toml`, `component.toml` -- the by-hand trigger.
- [x] Tests listed in Boundaries; docs listed in Boundaries; `sprint-status.yaml`.

**Acceptance Criteria:**
- Given the stack up with a day of sweeps, when `pixi run stack-run python manage.py compose_digest`
  runs, then `/digests/` shows per-collector dispatches and collections by state, rate-limited
  refusals, packages past target, inventory active/retired, packages by confidence and the newest
  policy run's version and age, and the row carries the trace id.
- Given nothing changed since the previous digest, when the task runs, then a row with
  `changed=False` and a one-line text is stored and delivered.
- Given `CPM_DIGEST_WEBHOOK_URL` carrying a credential and a webhook that refuses, when the task
  runs, then the row's delivery entry is `failed` with the host only and the task returns.
- Given `pixi run ci`, when it runs, then it exits 0.

## Spec Change Log

- 2026-09-14, review loop 1 (patch, not a re-derivation): the Boundaries' "resolved = verified +
  inventory-derived" contradicted the product's own partition in `collectors/selection.py`
  (`RESOLVED_CONFIDENCES = {verified}`; a package leaves the review set only on `verified`), which
  is the one reading consistent with the codebase; the digest now imports that partition. Also
  amended by patch: the window starts at the previous digest's `window_end` when within twice the
  window (was a fixed 24 h); the freshness figure is split into `past_freshness_target` and
  `never_observed`; the unchanged line names the last changed digest and carries the standing
  problems; the newest policy run's `status` is stored and said; the countdown is 3.5 h and pinned
  strictly above the largest sweep offset; figure keys live in `core/digest_keys.py`; the mail
  boundary catches `Exception` like the webhook's. KEEP: the `collectors/digest.py` placement,
  `core/delivery.py` as the POST seam, the marker constant, deliver-then-store with
  `digest.store_failed` on a save failure.

## Review Triage Log

### 2026-09-14 — Review pass
- intent_gap: 0
- bad_spec: 0
- patch: 26: (high 6, medium 13, low 7)
- defer: 1: (high 0, medium 0, low 1)
- reject: 7
- addressed_findings:
  - `[high]` `[patch]` resolved/unresolved partition matched to `collectors/selection.py`
  - `[high]` `[patch]` newest policy run's status stored and said (a failed run no longer reads healthy)
  - `[high]` `[patch]` unchanged line carries the standing problems (the repeated-failure day)
  - `[high]` `[patch]` unchanged line names the last changed digest
  - `[high]` `[patch]` mail boundary catches `Exception`; store failure logs `digest.store_failed`
  - `[high]` `[patch]` mapping-table selection shape tested; shape reading shared via `core/registry.selected_package_ids`
  - `[medium]` `[patch]` window starts at the previous `window_end`; window pinned to the schedule interval
  - `[medium]` `[patch]` countdown strictly above the largest sweep offset
  - `[medium]` `[patch]` freshness split into past-target and never-observed; docs corrected
  - `[medium]` `[patch]` keys module shared by composer and page; `counts_by_state` in `core/runs.py`
  - `[medium]` `[patch]` page order by registry, shape guards, one-slice newest+history
  - `[medium]` `[patch]` webhook: 2xx only, streamed and unread body, trace id in body and mail
  - `[medium]` `[patch]` declaration faults: one `@`, no list separators, unparsable port
  - `[medium]` `[patch]` trace id asserted inside a recording span; page rows asserted via context
  - `[medium]` `[patch]` end-to-end allowance refusal through `collect()` into the digest
  - `[low]` `[patch]` subject `(unchanged)`; `ingested`/`absent` words; command event renamed; README; test renames and tighter assertions

## Design Notes

**Why the collectors app.** The digest reads the ledger (`core`), the inventory (`collectors`)
and the package table (`identity`); `core` may import neither downstream app, and the
layering audit records exactly which `core` modules touch `identity`. `collectors` already
holds the S08 service that reads all three.

**Why a second seam for POST.** `Transport.fetch` is GET-only by contract and every substitute
in the suite implements only that; widening it would touch every collector's test double for
one caller. A `WebhookDeliverer` is the declared adapter for the one outbound POST.

**Why a marker constant.** Counting refusals by matching prose in `detail` would drift the
first time the sentence is edited; the marker is declared once and both the writer and the
counter use it.

## Auto Run Result

- **Gate:** on 2026-09-14 (stack down): `pixi run precommit`, `build`, `typecheck` and `lint`
  green; `pixi run test-cov` 11668 passed, 2 skipped, coverage 99.04% (floor 90). One
  combined `pixi run ci` earlier exited 3 on coverage's own save (`no such table: context`,
  the concurrent-run corruption already in memory) with the same suite otherwise green.
- **Live:** stack up, `pixi run stack-run compose_digest` enqueued `cpm.policy.digest`; the
  worker logged `digest.composed` (11 collectors) and `digest.stored_only`; `/digests/` as
  `reviewer` showed every collector's dispatches and collections by state, 23 rate-limited
  refusals for `feedstock` counted by the marker, 141 packages past the readiness target,
  148 active inventory entries, packages by confidence, the newest policy run's version and
  age, and the trace id the worker's log lines carried. `pixi run local-stack-down`.
- **Design notes on the way:** figures store the newest policy run's *ending*, not its age,
  so a quiet day compares equal to the day before; the digest is delivered *then* stored,
  because the row is append-only and the outcome cannot be written back; the resolved and
  unresolved halves are summed from two tuples rather than tested, because the
  confidence-gate audit reads a membership test against a confidence as a second gate;
  `core/delivery.py` and the digest's stated timeout are recorded in the collector-base
  audit; `core.delivery` joins the request-boundary audit's forbidden list; the no-slug
  sweep reads past the one `<pre class="record">` block that reproduces the delivered text.

## Verification

**Commands:**
- `pixi run test`; `pixi run -e dev python -m pytest tests/unit/django_apps/test_digest.py tests/integration/django_apps/test_digest.py tests/unit/test_settings.py tests/unit/test_process_model.py tests/unit/django_apps/test_documented_subsystems.py -q` -- green.
- Stack up, `pixi run stack-run python manage.py compose_digest`, open `/digests/` as `security-persona`; `pixi run local-stack-down`.
- `pixi run ci` (stack down) -- exit 0.

## Auto Run Result

- **Summary:** a daily `cpm.policy.digest` task composes per-collector and overall figures from the
  ledger, inventory and package table, stores them as an append-only `operator_digests` row, and
  delivers to a declared webhook and/or address (or stores only); `/digests/` renders the newest
  and a history; `pixi run digest` triggers it by hand.
- **Files:** `core/collection.py` (marker), `core/delivery.py` (POST seam), `core/digest_keys.py`,
  `core/runs.py` (`counts_by_state`), `core/registry.py` (`selected_package_ids`),
  `core/retention.py` (exclusion), `core/operator_commands.py`; `collectors/digest.py`,
  `collectors/models.py` + `0016_operator_digests`, `collectors/tasks.py`, `collectors/apps.py`,
  `collectors/management/commands/compose_digest.py`; `config/settings/base.py`/`test.py`;
  `surface/digest.py`, `surface/views.py`, `surface/urls.py`, `digest.html`, `_chrome.html`, CSS;
  `pixi.toml`, `component.toml`, README, docs (`operations.md`, `asynchronous-work.md`,
  `running-it.md`, `index.md`); tests (new unit + integration digest modules; settings, sweep,
  collection, retention, registry, process-model, boundary and slug audits amended).
- **Review:** 26 patched (high 6, medium 13, low 7), 1 deferred, 7 rejected. Follow-up review
  recommended: **true** (high-severity patches; score 3×13 + 7 = 46).
- **Verification:** `pixi run ci` exit 0 on 2026-09-14 (foreground, stack down): 11715 passed,
  2 skipped, coverage 99.08%. Named modules 946 passed. Live before the patches: stack up,
  `pixi run stack-run compose_digest` → `digest.composed`/`digest.stored_only` in the worker log,
  `/conda-sentinel/digests/` rendered figures, deliveries and the worker's trace id.
- **Residual risks:** ledger rows written before this deploy carry no refusal marker, so the
  first digest under-counts rate-limited refusals for its window; a digest row from before the
  patch renders `—`/0 for the figures it lacks; the webhook deliverer's real HTTP path is
  exercised only through a scripted `requests.post`.

## Suggested Review Order

1. `collectors/digest.py` -- composition, window, unchanged line and standing problems, delivery, store.
2. `core/delivery.py`, `core/digest_keys.py`, `core/registry.py` `selected_package_ids`, `core/collection.py` marker.
3. `collectors/models.py` `OperatorDigest` + migration; `core/retention.py`; `collectors/tasks.py`; `collectors/apps.py`.
4. `config/settings/base.py` (declarations, `cpm-digest` entry) and `tests/unit/test_settings.py`.
5. `surface/digest.py`, `surface/views.py` `DigestView`, `digest.html`, `_chrome.html`.
6. `tests/integration/django_apps/test_digest.py`, `tests/unit/django_apps/test_digest.py`.
7. Docs and README; `deferred-work.md` entry.
