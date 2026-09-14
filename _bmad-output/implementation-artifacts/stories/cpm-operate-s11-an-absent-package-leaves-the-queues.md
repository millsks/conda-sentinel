---
title: 'CPM-OPERATE-S11: An absent package leaves the queues'
type: 'feature'
created: '2026-09-14'
status: 'done'
review_loop_iteration: 1
followup_review_recommended: true
warnings: [oversized]
deferred:
  - summary: >-
      The report's CSV and JSON rows carry no absence tag for the five non-excluding reports (the screen does), so an integrator reading stale-evidence rows sees an inventory-absent package with no indication.
    evidence: |-
      surface/reports.py readable_rows tags the name cell on the screen only; the API/CSV values are the raw statuses by design (CPM-AD-24 rows agree, the label is a screen affordance).
    location: >-
      src/django_apps/conda_sentinel/surface/reports.py readable_rows
    severity: low
  - summary: >-
      Between the compose (which writes the absence columns NULL) and the mark_inventory_absence step, a reader sees an absent package as listed for the length of the after-run steps.
    evidence: |-
      core/rollup.py writes the columns NULL as part of the full-row replacement; collectors/absence.py stamps them in the after-run step; a failing step fails the run, which the docs state.
    location: >-
      src/django_apps/conda_sentinel/collectors/absence.py mark_inventory_absence
    severity: low
baseline_revision: '3e980f99c3352b1d3d4134d3430dd11627aaed03'
context:
  - _bmad-output/implementation-artifacts/epic-operate-context.md
  - _bmad-output/implementation-artifacts/stories/cpm-operate-s10-the-evidence-tables-at-ten-thousand-packages.md
---

<intent-contract>

## Intent

**Problem:** Nothing downstream reads inventory absence: the identity review selection ranks an
absent package last instead of leaving it out, workflow opening still opens items for it, its
open items never close, and no surface says it is gone. The queues carry packages the
organisation no longer runs.

**Approach:** One cut-off-bound reader of inventory absence (`collectors/absence.py`) feeds
four consumers: the identity review selection leaves absent packages out and says how many; an
after-run step stamps absence on the rollup row (one row per package stays); workflow opening
skips absent packages and closes their open items as a `system` write with a reason; the surface
labels absent packages wherever they appear, and the feedstock-gap report excludes them and
states the exclusion. A later listing reverses all of it with no manual step.

## Boundaries & Constraints

**Always:**
- **The reader** (`CPM-AD-25`): `collectors/absence.py` `absence_at(*, cutoff, package_ids=None)
  -> dict[int, Absence]` with `Absence(absent: bool, since: datetime | None, last_listed:
  datetime | None, listed_since: datetime | None)` -- one iterator query over
  `InventorySnapshot` rows `observed_at <= cutoff` in `_breadth_at`'s shape (`:256-349`) carrying
  `state`, folded per package to the newest `(observed_at, pk)`: `absent` is "newest is
  `not_found`", `since` that row's `observed_at`, `last_listed` the newest `ok` row's
  `observed_at`, `listed_since` the first `ok` row after the most recent `not_found` (None when
  never absent). `_require_usable_cutoff`'s rules. Never reads `InventoryEntry.retired_at`
  (governed data, not cut-off bound). `_breadth_at` gains `state` and shares the fold.
- **Selection** (`CPM-FR-4`): `collectors/selection.py` `select_unresolved(*, cutoff) ->
  IdentityReviewSelection(packages: tuple[UnresolvedPackage, ...], left_out_for_absence: int)`;
  `unresolved_packages(*, cutoff)` becomes `select_unresolved(...).packages` (its callers in
  tests keep working). Absent packages are not offered; `QUEUE_SELECTED_EVENT` gains
  `left_out_for_absence`. Docstring :384-386 ("last rather than absent") rewritten. Tests
  `test_selection.py:527,:560,:606` inverted; `:451` (departs after the cut-off keeps its place)
  stays.
- **The rollup keeps its row** (`CPM-AD-11`): `PackageHealth` gains two nullable, non-editable
  columns `inventory_absent_since` and `inventory_last_listed` (`core/migrations/0015_...`);
  they are written by an after-run step `collectors.mark_inventory_absence` (registered in
  `CollectorsConfig.ready()` through `register_after_run_step`, before `workflow`'s step -- pin
  the order with a test on `INSTALLED_APPS`/registration order) that reads
  `absence_at(cutoff=run.evidence_cutoff)` and `update()`s the run's rows: `since` and
  `last_listed` for absent packages, both `NULL` otherwise. `core` imports nothing of
  `collectors` (`CPM-AD-4`); the columns are not `*_status`/`*_outcome`, and `gated_status` is
  untouched: an absent package's statuses are computed exactly as before (assert a `verified`
  absent package keeps its determinate statuses, and an `unmapped` absent one is gated by its
  confidence, never by its absence).
- **Opening** (`workflow/opening.py`): `_open_identity_items(*, run, clock)` uses
  `select_unresolved(cutoff=run.evidence_cutoff)` (the selection's first production caller; the
  opening becomes cut-off bound and therefore replayable); `_open_remediation_items` and
  `_open_compliance_items` skip packages absent at `run.evidence_cutoff` (`absence_at` keyed by
  the run's package ids). Identity finding keys gain `("listed_since", <iso>)` **only** when
  `Absence.listed_since` is set, so a re-listed package opens a fresh item while never-absent
  packages keep their keys and nothing already resolved re-opens.
- **Closing is a `system` write** (`CPM-AD-22`): `workflow/migrations/0003_...` makes
  `WorkflowTransition.actor` nullable and adds `origin` (`CharField`, `""` for a person,
  `"system"` for the product) with a check constraint that exactly one of `actor`/`origin` names
  the author (`InventoryChange`'s shape, `collectors/models.py:3619-3624`). `workflow/states.py`
  declares `SYSTEM_TRANSITIONS`: every non-terminal state → `resolved`, `describes` "closed by
  the product: the inventory no longer lists the package", `requires_justification=True`;
  `TRANSITIONS` (the human table) is unchanged so `test_documented_subsystems.py:225,:247` keep
  their pins and gain a sibling for the system table. `workflow/services.py`
  `close_for_absence(*, item_id, absence: Absence, clock) -> WorkflowItem`: `select_for_update`,
  skips terminal items (idempotent on replay), sets `state=resolved`, clears `claimed_by`
  (`CLAIMED_ONLY_IN_PROGRESS`), writes the transition with `origin="system"` and a
  `justification` naming the absence and the last-listed date, logs
  `workflow.item_closed_for_absence` (item, package, queue, from_state, since). `open_queue_items`
  runs it for every open item of every package absent at the cut-off, after the openers, and
  returns opened + closed (the after-run event's `count` says both). `apply_transition` stays
  a person's path (`actor` required there); no role check applies to the product.
- **Surface** (`CPM-FR-38`): a text-only tag "absent from the inventory since YYYY-MM-DD (last
  listed YYYY-MM-DD)" beside the confidence tag on the package page header
  (`package_detail.html:9-13`), in the health list name cell (`package_health.html:99-101`,
  `HealthRow` gains `inventory_absent_since`/`inventory_last_listed`), on queue rows
  (`QueueRow` via `PackageHealth`), in the report rows' name cell, and in the API
  (`HealthRowSerializer`, `IdentitySerializer` or the row serializer -- `CPM-AD-24`). Read from
  the rollup columns only (cheap, cut-off bound); `surface/tone.py` has the `INVENTORY_RETIRED`
  chip precedent but this is a `conf`-style text tag. The identity review queue page states
  "N packages absent from the inventory are not offered" from the rollup
  (`PackageHealth` rows with `inventory_absent_since` set and confidence unresolved).
- **The feedstock-gap report**: `Report` gains an `excludes` declaration; `feedstock-lag`
  excludes rows with `inventory_absent_since` set and `report.html` states "N packages excluded:
  absent from the inventory at the run's cut-off" (count from the same query). The story records
  that the epic's "already states its other exclusion" has no precedent in code (the `unmapped`
  exclusion `CPM-APP-S05` describes lives on `package-health?feedstock=absent` by gate
  construction and is stated nowhere; the report includes `unknown` rows) -- left as is and
  appended to the deferred ledger, not changed here.
- **Replay** (`CPM-FR-22`): absence is read at `run.evidence_cutoff` everywhere above, so a
  replay selects, stamps, skips and closes the same; `close_for_absence`'s terminal skip makes
  it idempotent. `compare_runs` still compares derived tables only -- say so in the docs.
- **Tests:** `tests/unit/django_apps/test_absence.py` (fold: never listed, listed, absent,
  re-listed, absent again; `listed_since`; cut-off boundaries; naive cut-off refused),
  `tests/integration/django_apps/test_absence.py` (selection leaves out and counts; rollup
  columns after a run and cleared after re-listing; opening skips, closes with the system
  transition and reason, re-lists and opens a fresh item; replay reproduces the queue and
  closes nothing twice; a claimed in-progress item closes and is unclaimed; statuses unchanged;
  page tag, list tag, queue line, report exclusion statement, API fields), plus the amended
  selection/transition/documented-subsystems/after-run tests, `test_migration_completeness`,
  `test_derived_status_writability_audit`, `test_permission_audit`, `test_mutation_path_audit`.
- **Docs:** `the-queues.md` (":162 Nothing in the product closes an item; a person must" →
  the one exception, the system table, the queue line), `managing-the-inventory.md:143-190`
  ("What absence does today" rewritten to what it does now), `the-policy-run.md:106-145`
  (after-run steps and their order; replay note), `authorization.md:211-252` (the system
  write path in the table), `operations.md` inventory section, `running-it.md` if it lists the
  tags. Deferred ledger: the resolved S03 entry (:20-21) marked RESOLVED by an appended line;
  the report's `unmapped` exclusion precedent entry.
- Sprint status: `cpm-operate-s11-an-absent-package-leaves-the-queues: done`,
  `epic-operate: done`, `epic-operate-retrospective: optional` (the finished-epic convention).

**Block If:** the `WorkflowTransition` actor change is refused by an audit that cannot be
satisfied by the exactly-one constraint (then halt with the audit's name).

**Never:** delete or update an evidence row; read `InventoryEntry.retired_at` for absence;
gate a status on absence; let the product take a human transition (`apply_transition` with a
fake user); re-open a resolved item for a package that was never absent; exclude absent
packages from any surface other than the feedstock-gap report; suppress the rollup row.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|---|---|---|---|
| Absent at cut-off | newest snapshot ≤ cut-off is `not_found` | not selected; counted; rollup stamped; items closed by `system` with reason | N/A |
| Absent after cut-off | `not_found` row after the cut-off | treated as listed for this run | N/A |
| Never listed | no snapshot | not absent; selection and opening as today | N/A |
| Re-listed | `ok` row after the `not_found`, ≤ cut-off | not absent; rollup columns NULL; identity key carries `listed_since`; fresh item opens | N/A |
| Claimed item | absent package with an `in_progress` claimed item | resolved, `claimed_by` NULL, transition `origin=system` | N/A |
| Already resolved | absent package with a resolved item | untouched | N/A |
| Replay | same version and cut-off | same selection, same closes, none twice | N/A |
| Verified absent | absent package at `verified` | statuses computed as before; labelled absent | N/A |
| Report | absent packages match `feedstock-lag` | excluded; header states count and reason | N/A |
| Person transition unchanged | `apply_transition` without an actor | refused as today | `TypeError`/`WorkflowError` |
| Naive cut-off | `absence_at(cutoff=naive)` | refused | `InventoryReadError` |

</intent-contract>

## Code Map

- Absence source: `collectors/models.py:856-1014` `InventorySnapshot` (`state` :896, `COUNTS_PRESENT_CONSTRAINT` :984),
  :1017-1067 `snapshot_as_of` (per-package; `InventoryReadError`); absence writer `collectors/tasks.py:1051-1135`
  `_observe_absences` (`ABSENT_DETAIL` :353, written once on the transition; re-listing writes an `ok` row via `_observe` :998).
- Selection: `collectors/selection.py` (:92 event and keys :436-441, :104/:119 partitions, :141 `UnresolvedPackage`,
  :215 `_require_usable_cutoff`, :256-349 `_breadth_at` iterator + fold, :352-442 `unresolved_packages`, docstring :384-386);
  tests `tests/integration/django_apps/test_selection.py:451,:470,:527,:560,:606`, `tests/unit/django_apps/test_selection.py`.
- Workflow: `workflow/models.py:71-160` `WorkflowItem` (`finding_key` :82, `claimed_by` :113, `CLAIMED_ONLY_IN_PROGRESS` :149),
  :178-230 `WorkflowTransition` (`actor` :214 NOT NULL PROTECT, `justification` :223, `occurred_at` :227);
  `workflow/states.py:64,:94,:128,:144,:153,:181-233,:236`; `workflow/services.py:83-85` events, :111 `open_item`,
  :135 `open_keyed_item` (`get_or_create` on key), :183-262 `apply_transition`, :421 `_move`; `workflow/opening.py:58,:66,:85-200`;
  `workflow/apps.py:30-45`; `core/after_run.py:93,:117,:191-217`; `core/models.py:1040` `PolicyRun.evidence_cutoff`;
  tests `test_workflow_transitions.py:446,:470,:645,:694`, `test_after_run_seam.py:246`, `test_documented_subsystems.py:225,:247`.
- Exactly-one author shape: `collectors/models.py:3535-3547,:3619-3624` (`InventoryChange` actor/origin constraint);
  `docs/conda-sentinel/authorization.md:250-252`.
- Rollup: `core/rollup.py:148,:262-300`; `core/models.py:1076-1240` `PackageHealth` (`confidence` :1216; every `*_status`
  `editable=False`); newest migrations core `0014`, workflow `0002`, collectors `0016`; audits `test_derived_status_writability_audit.py`,
  `test_confidence_gate_audit.py`, `test_migration_completeness.py`; `core/confidence.py:138-168` `gated_status`.
- Surface: `surface/labels.py:184,:200`; `surface/tone.py:68-69,:201`; `surface/detail.py:290,:496-513,:621`;
  `surface/listing.py:97-172`; `surface/health.py:189,:215,:252-300`; `surface/queues.py:77,:90,:146`; `surface/views.py:710-814` `QueueView`,
  :817-866 `ReportView`; `surface/reports.py:120,:175-185,:250,:291`; `surface/api/serializers.py:66,:149`;
  templates `package_detail.html:9-13,:162-163`, `package_health.html:99-122`, `queue.html:11-15,:61-63`, `report.html:9-11,:107-116`.
- Replay: `core/policy_run.py:201-265,:268-370` (`run_after_run_steps` :360, no replay flag);
  `core/management/commands/replay_policy_run.py:154,:190-205,:245`; `core/replay.py:16-19,:150`.
- Layering/boundary: `test_app_layering_audit.py:70` (only `core` is constrained); `test_request_boundary_audit.py:59-72`
  (`collectors.selection` imports only `collectors.models`, view-reachable); `test_permission_audit.py:114-116`; `test_mutation_path_audit.py`.
- Users: `src/django_service/users/models.py:12`; no service accounts (`users/provisioning.py`, `config/local_dev/personas.py:155-247`).
- Docs: `the-queues.md:1-184` (:66-76 table pinned, :162), `managing-the-inventory.md:143-190`, `the-policy-run.md:106-145`,
  `authorization.md:211-252`, `operations.md:14-64`; deferred ledger `deferred-work.md:20-21`.
- Events/clock: `services.py:83-85`, `after_run.py:68`, `selection.py:92`, `rollup.py:119`; `core/clock.py:82-140`; `test_clock_audit.py`.
- Read-only: `core/confidence.py`, `core/rollup.py`'s status composition, the inventory collector's writes, `apply_transition`'s checks.

## Tasks & Acceptance

**Execution:**
- [x] `collectors/absence.py` -- `Absence`, `absence_at`, `histories_at`/`fold_observations` (the shared fold); `collectors/selection.py` -- reads breadth off that fold, `select_unresolved`, `IdentityReviewSelection`, event key `left_out_for_absence`.
- [x] `core/models.py` + `core/migrations/0015_package_health_inventory_absence.py` -- the two columns; `core/rollup.py` `AFTER_RUN_COLUMNS` keeps them out of `contributable_columns()` and the compose writes them `NULL`.
- [x] `collectors/apps.py` + `collectors/absence.py` -- the `mark_inventory_absence` after-run step; order pinned in `test_after_run_seam.py` and `test_absence.py`.
- [x] `workflow/models.py` + `workflow/migrations/0003_transition_origin.py` -- nullable `actor`, `origin`, `EXACTLY_ONE_AUTHOR`.
- [x] `workflow/states.py` -- `SYSTEM_TRANSITIONS`, `system_transition_for`; `workflow/services.py` -- `close_for_absence`, `ITEM_CLOSED_FOR_ABSENCE_EVENT`; `workflow/opening.py` -- cut-off-bound selection, skips, `listed_since` keys, closing after the openers.
- [x] `surface/health.py`, `surface/queues.py`, `surface/reports.py`, `surface/views.py`, `surface/labels.py` (`absence_tag`), `surface/templatetags/health.py`, `surface/api/serializers.py`, `surface/api/views.py`, templates -- the tag, the queue line, the report exclusion; `surface/listing.py` and `surface/detail.py` needed no change (the tag reads the rollup row both already hand over).
- [x] Tests listed in Boundaries; docs listed in Boundaries (`running-it.md` lists no tags and is unchanged); `deferred-work.md`; `sprint-status.yaml` (story, epic; the retrospective line already read `optional`).

**Acceptance Criteria:**
- Given a package whose newest snapshot at the run's cut-off is `not_found` with two open items,
  when a policy run completes, then the identity selection leaves it out and logs the count, its
  rollup row carries `inventory_absent_since`/`inventory_last_listed`, its items are `resolved`
  by a transition with `origin=system` and a justification naming the absence, and the package
  page shows the absent tag with both dates.
- Given the same package listed again by a later ingestion, when the next run completes, then
  the columns are NULL, the tag is gone, it is offered and a fresh identity item opens.
- Given the feedstock-gap report, when rendered with absent packages matching it, then they are
  excluded and the header states the count and reason.
- Given `pixi run ci`, when it runs, then it exits 0.

## Spec Change Log

- 2026-09-14, implementation (patch, not a re-derivation): the two rollup columns are kept
  out of `contributable_columns()` by a third enumeration, `core/rollup.py`
  `AFTER_RUN_COLUMNS`, and the compose writes them `NULL` -- without that,
  `_replacement` would have gated `str(None)` into a datetime column on every row, and
  `test_policy_contribution`'s exact-four pin would have moved. The identity opener
  compares `entry.package.confidence` to `unmapped` in Python (the selection offers
  `inventory-derived` too, and the queue was always `unmapped`'s alone); that is a
  comparison `test_confidence_gate_audit` sees where the old queryset filter was not, so
  it is recorded there with its reason. The report's tag is appended to the name cell by
  `ReportPage.readable_rows()` only -- `rows`, the CSV and the API rows are unchanged
  (`CPM-AD-24`) -- and `report_values` carries the two instants after the version map;
  `ReportPage.excluded` is filled by `report_page` from `excluded_count` over the same
  condition and search. The fold's pure half is `fold_observations`, so the unit tier
  covers every arrangement without a database; the cut-off boundary itself is SQL and is
  asserted in the integration module. `mark_inventory_absence` spells its two `update()`s
  on the manager so `test_mutation_path_audit` counts them (recorded: two). The package
  page's history line prints `origin` where a move has no actor. KEEP: everything else
  in the Boundaries as written.

- 2026-09-14, review patch (23 findings, all applied): "absent" is now "a `not_found`
  with no `ok` after it" -- `since` the *first* such row, an `error`/`unknown`/
  `not_applicable` row after it keeps the absence -- rather than "newest is
  `not_found`"; the health facet `feedstock=absent` excludes inventory-absent packages
  too and states both exclusions (its own, and the `unmapped` one the gate made by
  construction); the report's exclusion travels on the API (`excluded: {count, reason}`
  on the roster and the page) and the CSV provenance line; `WorkflowTransitionSerializer`
  carries `origin`; the closer reaches every package with open work (rollup rows ∪
  open items) so a failed compose still has its items closed; at most one open
  identity item per package; remediation and compliance keys carry the listing epoch
  too, because an evidence-backed key is the same row before and after a re-listing
  (proved: without it they never re-opened); `close_for_absence` returns
  `ClosedItem(item, written)` and refuses an undeclared stored state; one fold per run
  (`histories_at` narrows in SQL, 500 ids a chunk; `select_unresolved(histories=)`);
  the stamp is one `UPDATE` per `(since, last_listed)` pair with no clearing write;
  the queue line reads `Package.confidence` and says "across all packages as of the
  newest run"; CSS for the three classes; `Exclusion.reason` is a translation promise
  while the transition's `describes` and the justification are stored English;
  `_breadth_at` deleted; the S03 ledger resolution is its own entry; `operations.md`
  says the digest's counts and the queues can differ by one ingestion. KEEP:
  everything else as written.

## Review Triage Log

### 2026-09-14 — Review pass
- intent_gap: 0
- bad_spec: 0
- patch: 23: (high 9, medium 10, low 4)
- defer: 2: (high 0, medium 0, low 2)
- reject: 3
- addressed_findings:
  - `[high]` `[patch]` the feedstock-gap exclusion on both surfaces the epic could mean (the `feedstock-lag` report and the `?feedstock=absent` facet), each stating count and reason; the facet's `unmapped` exclusion finally stated
  - `[high]` `[patch]` the exclusion stated on the report's API and CSV, not only the page
  - `[high]` `[patch]` items closed for every absent package with open items, not only those with a rollup row this run
  - `[high]` `[patch]` at most one open identity item per package (a purged `not_found` row cannot create a duplicate)
  - `[high]` `[patch]` remediation and compliance items re-open on re-listing (epoch in their keys), proven by test
  - `[high]` `[patch]` `close_for_absence` reports whether it wrote; counts are writes; undeclared states refused
  - `[high]` `[patch]` `since` is the first `not_found` of the current absence; only an `ok` ends it
  - `[high]` `[patch]` fresh-finding skip, verified-absent count, never-listed render, searched count and inventory-derived pins added
  - `[high]` `[patch]` `WorkflowTransitionSerializer.origin`
  - `[medium]` `[patch]` one fold per run for the openers; ids pushed into SQL in chunks; empty input answered without a query
  - `[medium]` `[patch]` stamping by distinct `(since, last_listed)` pair; redundant clear dropped
  - `[medium]` `[patch]` queue line reads `Package.confidence`; three consumers agree by test
  - `[medium]` `[patch]` a raising step fails the run; `compare_runs` unaffected — both pinned
  - `[medium]` `[patch]` `last_listed` None renders without the clause everywhere
  - `[low]` `[patch]` CSS rules, i18n, docstrings, variable name, dead `_breadth_at`, ledger entry shape, digest docs sentence

## Design Notes

**Why a `system` origin rather than a system user.** `Transition.actor` is a person and every
check in `apply_transition` asks that person's roles; the product has no roles and should not
borrow a human's. `InventoryChange` already records an author as "exactly one of a person and a
file"; the same constraint with `origin="system"` keeps every transition attributable without a
fake account, and keeps the human table (`TRANSITIONS`) and its doc pin untouched.

**Why the rollup carries absence.** The surface must label absent packages in every list at
one query's cost and at the run's cut-off; the rollup row is the one place that is both cut-off
bound and one-per-package. The step lives in `collectors` because `core` may not read
`InventorySnapshot`.

**Why `listed_since` in the identity key.** `open_keyed_item` is `get_or_create` on the key so
resolved items never re-open; a re-listed package must open a *new* item, so the key names the
listing epoch -- and only for packages that have been absent, so every existing key is stable.

## Verification

**Commands:**
- `pixi run test`; `pixi run -e dev python -m pytest tests/unit/django_apps/test_absence.py tests/integration/django_apps/test_absence.py tests/integration/django_apps/test_selection.py tests/integration/django_apps/test_workflow_transitions.py tests/unit/django_apps/test_documented_subsystems.py tests/unit/django_apps/test_after_run_seam.py tests/unit/django_apps/test_migration_completeness.py -q` -- green.
- Stack up: retire one package on the inventory page as `leader-persona`, then `pixi run stack-run ingest` and `pixi run stack-run policy-run`; the package page shows the tag, its items are closed, the identity queue states the count; `pixi run local-stack-down`.
- `pixi run ci` (stack down) -- exit 0.

## Auto Run Result

- **Summary:** inventory absence at the run's cut-off now leaves the queues: `collectors/absence.py`
  folds the snapshot history once per consumer; the identity review selection leaves absent
  packages out and says how many; an after-run step stamps `inventory_absent_since`/
  `inventory_last_listed` on the rollup row; workflow opening skips absent packages and closes
  their open items with a `system`-origin transition and a reason; every list and the package
  page carry the absent tag; the feedstock-gap report and facet exclude absent packages and say
  so; re-listing reverses all of it. Epic CPM-EP-OPERATE is complete.
- **Files:** `collectors/absence.py`, `collectors/selection.py`, `collectors/apps.py`;
  `core/models.py` + `core/migrations/0015`, `core/rollup.py`; `workflow/models.py` +
  `workflow/migrations/0003`, `workflow/states.py`, `workflow/services.py`, `workflow/opening.py`,
  `workflow/api/serializers.py`; `surface/labels.py`, `surface/health.py`, `surface/listing.py`,
  `surface/queues.py`, `surface/reports.py`, `surface/views.py`, `surface/exports.py`,
  `surface/api/serializers.py`, templatetags, templates, CSS; tests (new unit + integration
  absence modules; selection, rollup, report projection, workflow transitions, after-run seam,
  queue views, documented subsystems and audits amended); docs (`the-queues.md`,
  `managing-the-inventory.md`, `the-policy-run.md`, `authorization.md`, `operations.md`);
  `deferred-work.md`; `sprint-status.yaml` (story `done`, `epic-operate: done`,
  `epic-operate-retrospective: optional`).
- **Review:** 23 patched (high 9, medium 10, low 4), 2 deferred, 3 rejected. Follow-up review
  recommended: **true** (high-severity patches; score 3×10 + 4 = 34).
- **Verification:** `pixi run ci` exit 0 on 2026-09-14 (foreground, stack down): 11828 passed,
  2 skipped, coverage 99.17%. Live before the patches: retired a package on the inventory page
  as the leader persona, ingested and ran the policy: worker logged `inventory_absence_marked`
  and `workflow.item_closed_for_absence`; package page, health list, identity queue line,
  feedstock-gap report and API all showed the absence; reactivated, ingested and ran again:
  tag gone, package offered, fresh item beside the resolved one; stack stopped.
- **Residual risks:** the labelled/closed state depends on a policy run having completed since
  the ingestion that recorded the absence; the report values (CSV/API) carry no tag.

## Suggested Review Order

1. `collectors/absence.py` -- the fold (`since`, `last_listed`, `listed_since`), `histories_at`, the after-run step.
2. `workflow/opening.py` and `workflow/services.py` `close_for_absence`; `workflow/states.py` `SYSTEM_TRANSITIONS`; `workflow/models.py` + `0003`.
3. `collectors/selection.py` `select_unresolved`; `core/models.py` + `0015`; `core/rollup.py` `AFTER_RUN_COLUMNS`.
4. `surface/reports.py` exclusions, `surface/listing.py` facet exclusion, `surface/queues.py` line, `surface/labels.py` tag, serializers, templates.
5. `tests/integration/django_apps/test_absence.py`, `tests/unit/django_apps/test_absence.py`.
6. Docs and the deferred ledger.
