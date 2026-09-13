---
title: 'CPM-OPERATE-S03: The inventory is a governed table'
type: 'feature'
created: '2026-09-13'
status: 'done'
review_loop_iteration: 0
followup_review_recommended: false
warnings: [oversized]
deferred: []
baseline_revision: '0594aa1e16e5aef6e2f86a2deb8b342a1a3266a2'
context:
  - _bmad-output/implementation-artifacts/epic-operate-context.md
  - _bmad-output/implementation-artifacts/stories/cpm-operate-s02-ingest-sweep-and-run-are-commands.md
  - _bmad-output/implementation-artifacts/stories/cpm-platform-s08-the-demo-asserts-nothing-it-never-observed.md
---

<intent-contract>

## Intent

**Problem:** The inventory source is a CSV inside the wheel: adding a package is a review
and a release, and the demo seeder is a second inventory keyed `pypi:<name>` that collides
with the watchlist on 59 names, so a seeded stack cannot ingest.

**Approach:** Watchlist rows live in a database table, changed only through an audited
write (permission, reason, prior and new value in one transaction -- the identity
override's shape) or a CSV import command; ingestion reads the table through a second
adapter behind the same `Transport` contract, selected by a declared, fail-closed setting;
the wheel's CSV becomes the first-run seed; and the demo seeder seeds that table and
*ingests it* rather than filing its own shells, so the collision cannot exist.

## Boundaries & Constraints

**Always:**
- **Model**, in the `collectors` app beside the adapter: `InventoryEntry` (mutable, table
  `inventory`): `source_package_key` (512, unique), `package_name` (128), the six signals as
  `PositiveIntegerField(null=True)` -- blank is missing, never zero -- `retired_at` nullable,
  `changed_at`, `reason` (non-blank). No field named `observed_at`, `status` or `computed_at`
  (the evidence/outcome/derived audits classify on those names). Audit `InventoryChange(AppendOnlyModel)`
  (table `inventory_changes`): `entry` FK PROTECT, `actor` FK nullable (an import runs
  unattended; then `origin` names the file), prior/new columns for every changeable field
  including `retired`, `reason`, `trace_id`; added to `EVIDENCE_MODEL_LABELS`; no unique
  constraint. Constraint and index names are module `Final` constants; `db_table` snake-case.
- **Service** `collectors/inventory.py`: `add_entry`, `change_entry`, `retire_entry`,
  `import_watchlist(records, *, replace, actor, origin, clock)` -- each row's write and its
  audit row in one `transaction.atomic()`, `select_for_update` on the entry, instance
  `save(update_fields=)` only (the mutation-path audit bans manager `update`/`delete`);
  retire is a column write, never a delete; `--replace` retires every active row the file
  no longer names. Permission `INVENTORY_CHANGE_PERMISSION` (`collectors.change_inventory`)
  declared in `core/roles.py`, attached on `InventoryChange.Meta.permissions`, granted to
  `LEADERSHIP` by a `core` data migration mirroring `0005_grant_identity_override`; the
  service refuses an actor without it, logging the refusal with the actor, and refuses a
  blank reason; an unattended import passes `actor=None` and is not permission-gated (the
  command line is the gate, as for every admin process).
- **Adapter** `DatabaseInventoryAdapter` in `collectors/inventory_source.py`, implementing
  `fetch(source, *, headers=None) -> Payload` and answering the same JSON record list the
  CSV adapter answers, from active entries only (retired rows are absent, so ingestion
  records them absent). Selected by `CPM_INVENTORY_SOURCE` -- `"watchlist"` (default, the
  file by locality as today) or `"database"`; an unrecognised value refuses at boot naming
  the setting; the constant vocabulary lives in a model-free module importable at settings
  time; the branch is in `CollectorsConfig.ready()` with its own idempotency guard; the
  collector stays branch-free. The `dev` feature's activation env sets `database`.
- **Command** `manage.py import_watchlist [<path>] [--replace] [--reason TEXT]` in
  `collectors/management/commands/`, defaulting to the file locality selects; parses through
  `records_from` (every existing refusal applies); admin process `import-watchlist` beside
  the three from S02 (root task, no env; `ADMIN_PROCESSES` in the S02 unit test grows by one).
- **Seeder** (`demo_data.py`): imports the development watchlist into the table (origin the
  file, reason "local development seed"), then runs `InventoryIngestionCollector` inline
  through the database adapter so shells and `InventorySnapshot` rows are the product's own,
  then the resolver, then the roster overlay (advisory, KEV, licence, PyPI row where
  established), then the policy run. Before any write it refuses a roster name the
  watchlist does not carry, naming it. The 41 roster-only names are added to
  `watchlist-development.csv` as rows (the two `internal-*` under key `internal/<name>`);
  `DemoPackage` keeps exactly its seven fields. `_shipped_policy_version` uses
  `policies.parameters.newest_recorded_version()`, lifted there from the `run_policy`
  command's `version_key`/`recorded_versions` (the command imports it back).
- **Surface**: a minimal server-rendered page `inventory/` (list; add, edit, retire forms
  posting to a `RoleRequiredMixin` view gated on `LEADERSHIP`, post/redirect/get, delegating
  to the service) in the `ReportExportView` shape; labels through `display_label`; a
  `retired` chip gets a tone; strings translated; chrome inherited. No DRF endpoint (the
  API contract audit pins two writes).
- **Docs**: `managing-the-inventory.md` rewritten around the table (add a package end to
  end: row from the surface or `import-watchlist`, `ingest`, `sweep resolve_identity`, then
  the sweeps; retire and what absence does today; the file as first-run seed; the setting);
  `running-it.md` stack section; `operations.md` admin-process table (+1) and the setting;
  `authorization.md` (the new permission on the leadership role); `onboarding.md`; README;
  `collectors/data/README.md`. Every page still names the eight columns.
- **Planning**: append "§7. CPM-FR-3 amendment" to
  `_bmad-output/planning-artifacts/sprint-change-proposal-2026-09-13.md` and apply it: the
  PRD's FR-3 sentence and glossary line name two governed human writes -- package identity
  and the inventory -- each with the three obligations; `epics.md:47` the same; PRD
  Appendix A.2 gains `inventory` and `inventory_changes`. Sprint status: story `done`.
- **Tests**: unit for the parser-to-adapter equivalence, the setting vocabulary, the roles
  contract (update `test_roles.py`'s tuples), the permission-audit exemption for the new
  service, the model registry roster; integration for every service refusal and the
  one-transaction rule (mirror `test_identity_overrides.py`), the import (fresh, repeat,
  `--replace` retiring), the database adapter ingesting into an empty database and then
  a seeded one with no `IntegrityError`, retire → next ingestion records `not_found`,
  the seeder end to end with a scripted resolver transport, the surface page and its
  refusals, and the command.

**Block If:** the mutation-path audit cannot be satisfied without a recorded exemption
(stop and report which line); a fourth governed write path appears; any change to
`identity/services.py`'s override.

**Never:** a DRF write; `objects.delete()`/`update()` in `src/`; a `Package` write
outside `resolve_package_shell`; a seeder shell keyed `pypi:`; queue or rollup changes for
absence (documented as it is; a follow-up).

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|---|---|---|---|
| Add | leadership actor, reason, valid row | entry + one audit row (prior blank) in one transaction | N/A |
| Add, no permission | packaging engineer | refused, logged with actor, nothing written | `InventoryNotPermittedError` |
| Add, blank reason | leadership | refused, nothing written | `InventoryChangeError` |
| Change | existing key, new counts | entry updated, audit row with prior/new, `changed_at` advanced | N/A |
| Retire | active key | `retired_at` set, audit row; next ingestion records `not_found` for the package | N/A |
| Retire twice | retired key | refused as already retired | `InventoryChangeError` |
| Duplicate key | add an existing key | refused before write | `InventoryChangeError` |
| Import fresh | empty table, 148-row dev file | 148 entries, 148 audit rows with `actor=None`, origin the file | parser refusals propagate |
| Import repeat | same file | no change rows for unchanged entries; changed counts audited | N/A |
| Import replace | `--replace` with a shorter file | rows absent from the file retired with reason; none deleted | N/A |
| Adapter | `CPM_INVENTORY_SOURCE=database` | ingestion sees active rows as records identical in shape to the CSV adapter's | N/A |
| Adapter, empty table | no active rows | ingestion refuses the empty document as today (nothing marked absent) | `InventoryRecordError` |
| Setting bad | `CPM_INVENTORY_SOURCE=nope` | boot refused naming the setting | `ImproperlyConfigured` |
| Seed then ingest | `stack-seed` then `ingest` | no `IntegrityError`; every package has an inventory snapshot; keys `("inventory", "conda-forge/<name>")` | N/A |
| Roster name missing | roster name absent from the dev file | seeder refuses before any write, naming it | `ImproperlyConfigured` |

</intent-contract>

## Code Map

- Adapter contract: `collectors/watchlist.py:644` `WatchlistAdapter`, :667 `fetch` (body = JSON
  list from `records_from`), :294 `records_from`, :124-151 columns, :270 `watchlist_path`; the
  module is imported at settings time and must stay model-free. `collectors/tasks.py:184`
  `COLLECTOR_NAME`, :294 `INVENTORY_SOURCE`, :417 `InventoryRecord`, :454/:491/:515/:537 the slot,
  :561 `records_in`, :869 `InventoryIngestionCollector` (:923 `persist_sweep`, :981 `_observe`
  → `resolve_package_shell(identity_source=COLLECTOR_NAME)`, :1034 `_observe_absences` writes
  `not_found`). `collectors/apps.py:283-295` the binding to replace with a branch;
  `tests/unit/django_apps/test_watchlist.py:771-865` pins it. `tests/conftest.py:667-700` withdraws.
- Audit template: `identity/services.py:1603` `override_identity`, :1740 `_require_permitted`,
  :1811 `_require_reason`, :337-339 events, :351-401 exceptions, :1699 the one block;
  `identity/models.py:1010-1176` `IdentityOverride` (prior/new columns, `Meta.permissions`);
  `core/roles.py:95-97,139-143`; `core/migrations/0005_grant_identity_override.py`;
  tests `test_identity_overrides.py` (integration :263-1346; unit :368-972, :852 the AST
  one-transaction case, :534 the permission declaration).
- Audits: `tests/model_registry.py:237-275` (evidence iff `AppendOnlyModel`/`observed_at`),
  `test_model_registry.py:102-116`; `test_outcome_field_audit.py:121`; `test_mutation_path_audit.py:156-253`;
  `test_permission_audit.py:107`; `test_roles.py:238,375`; `test_role_groups.py`;
  `test_api_contract_audit.py:278-291`; `test_operator_commands.py:62-66,237-275`;
  `test_documented_subsystems.py:98,297-318`; `test_documentation_commands.py:120,171`.
- Seeder: `demo_data.py:175` `DemoPackage`, :262 roster, :480 `seed_demo_inventory`, :544
  `_seeded_package` (the `pypi:` shell to delete), :784 `_resolve_identities`, :991
  `_shipped_policy_version`; tests `test_local_dev_demo_data.py:68,149,189,205`,
  `test_local_dev_demo_seeding.py:129-135,271,313-339,653,674`. Roster-only names (41) and
  CSV-only (48) listed in the investigation; `test_the_roster_is_the_mixture_it_was_asked_to_be`
  :488 pins twenty names that must stay.
- CSV: `collectors/data/watchlist-development.csv` (107 rows, keys `conda-forge/<name>`),
  `watchlist.csv` (header), `data/README.md`; `test_watchlist_ingestion.py:191-347`.
- Surface: `surface/views.py:550` `ReportExportView` (mixin :575, `post` :618, 303 :660),
  `core/permissions.py:211,301`, `surface/urls.py:48-78`, `surface/tone.py:156`,
  `surface/labels.py:200`, `templates/conda_sentinel/package_detail.html:141-169` (audit panel).
- Version helper: `policies/management/commands/run_policy.py:100` `version_key`, :117
  `recorded_versions` → move to `policies/parameters.py`; `tests/passes.py` oracle.
- Absence today: `collectors/selection.py:132-138,380`, `workflow/opening.py:126`,
  `core/rollup.py:166` -- read-only; `managing-the-inventory.md:90-124`.
- PRD: `prd.md:186-199` FR-3, :147 glossary, :956 A.2; `epics.md:47`.

## Tasks & Acceptance

**Execution:**
- [x] `collectors/models.py` + migration -- `InventoryEntry`, `InventoryChange`.
- [x] `core/roles.py`, `core/migrations/0011_grant_inventory_change.py` -- the permission.
- [x] `collectors/inventory.py` -- the service; `collectors/inventory_source.py` -- the adapter;
  the setting vocabulary; `src/config/settings/base.py` `CPM_INVENTORY_SOURCE`; `collectors/apps.py` branch;
  `pixi.toml` dev activation env `CPM_INVENTORY_SOURCE=database`.
- [x] `collectors/management/commands/import_watchlist.py`; `component.toml`, `pixi.toml` `import-watchlist`.
- [x] `policies/parameters.py` `version_key`/`newest_recorded_version`; `run_policy.py` imports them.
- [x] `demo_data.py` -- seed the table, ingest, resolve, overlay; `watchlist-development.csv` +41 rows.
- [x] `surface/` inventory page, view, urls, template, tone; `surface` tests.
- [x] Tests listed in Boundaries; roster updates (`test_roles`, `test_model_registry`,
  `test_permission_audit`, `test_operator_commands`, `test_local_dev_*`).
- [x] Docs listed in Boundaries; the change proposal §7 and the PRD/epics amendment.
- [x] `sprint-status.yaml` -- story `done`.

**Acceptance Criteria:**
- Given a fresh volume, when `pixi run docker-down-v && pixi run -e dev stack-seed && pixi run stack-run ingest_inventory`
  runs, then the ingestion succeeds with no `IntegrityError`, every package has an
  `InventorySnapshot`, and every `Package` is keyed `("inventory", …)`.
- Given the stack, when a leadership persona adds a row on `/inventory/` with a reason and
  `pixi run stack-run ingest_inventory` runs, then the new package exists at `unmapped`
  with an audit row naming the actor.
- Given `pixi run stack-run import_watchlist --replace` with a shorter file, when the next
  ingestion runs, then the missing packages carry a `not_found` inventory snapshot.
- Given `pixi run ci`, when it runs, then it exits 0.

## Spec Change Log

## Design Notes

**Why the seeder ingests instead of filing shells.** Two writers of package shells under
two key schemes was the whole collision. With the seeder importing the watchlist and then
running the product's own ingestion, there is one key scheme, one shell writer
(`resolve_package_shell` via the collector), and the demo's first snapshot rows are real.

**Why absence stays as it is.** The epic's line "leaves the queues" was written before the
survey; nothing downstream reads inventory absence today. Changing the identity queue and
workflow opening is a policy decision with its own story; this one documents what happens
(the package keeps its rows and its rollup row, sorts last in the identity queue for want
of breadth) and records the follow-up in `deferred-work.md`.

**Why `CPM_INVENTORY_SOURCE` defaults to the file.** Fail closed toward the deployment as it
is: a component that has never imported its watchlist keeps reading the reviewed file;
switching is an operator's declaration after `import-watchlist`.

## Verification

**Commands:**
- `pixi run test`; `pixi run -e dev python -m pytest tests/integration/django_apps/test_inventory*.py tests/integration/test_local_dev_demo_seeding.py tests/integration/django_apps/test_watchlist_ingestion.py -q` -- green.
- `pixi run docker-down-v && pixi run -e dev stack-seed && pixi run stack-run ingest_inventory` -- succeeds; the AC query.
- `pixi run stack-run import_watchlist --help`; the surface page as `leadership-persona` (200) and `packaging-persona` (403).
- `pixi run ci` (stack down) -- exit 0.

## Auto Run Result

Status: done
Review: three layers; twenty patches applied. Four were high -- active rows could share a
`package_name` (re-creating the collision the story closes; now refused and constrained), a
refused change re-rendered as an add form, an import silently reactivated a person's
retirement (now never; reactivation is an audited surface change), and the scheduled
`import-watchlist` process could never retire (now `--replace`). The epic's "leaves the
queues" line was found to describe behaviour that does not exist and became
`CPM-OPERATE-S11` at the product owner's direction; `core/0005` was narrowed in place so each
grant migration provisions only its own codename (tests pin the narrowing and the
rollback-then-reapply order).

## Suggested Review Order

**The write path: one door, three obligations**

- Entry point: every change, add, retire and import row goes through one block -- lock, refuse, write, audit
  [`inventory.py:618`](../../../src/django_apps/conda_sentinel/collectors/inventory.py#L618)

- A name another active row carries is refused before the write, so ingestion cannot collide
  [`inventory.py:765`](../../../src/django_apps/conda_sentinel/collectors/inventory.py#L765)

- An import adds and changes, retires what the file no longer names under `--replace`, and never reactivates
  [`inventory.py:454`](../../../src/django_apps/conda_sentinel/collectors/inventory.py#L454)

**The table and its audit**

- Mutable governed data: no `observed_at`, a partial unique name constraint, a non-blank reason
  [`models.py:3277`](../../../src/django_apps/conda_sentinel/collectors/models.py#L3277)

- Append-only evidence of every change: prior/new columns, exactly one of actor or origin
  [`models.py:3383`](../../../src/django_apps/conda_sentinel/collectors/models.py#L3383)

**The adapter behind the same contract**

- Active rows answered as the same JSON document the CSV adapter answers; selected by `CPM_INVENTORY_SOURCE`
  [`inventory_source.py:127`](../../../src/django_apps/conda_sentinel/collectors/inventory_source.py#L127)

**The seeder seeds the inventory and lets the product ingest it**

- Refuse a legacy volume, import the file, ingest inline, resolve, overlay, run policy
  [`demo_data.py:540`](../../../src/config/local_dev/demo_data.py#L540)

**Surface**

- Leadership-only page; a refused change stays a change; counts render NULL as an em dash
  [`views.py:869`](../../../src/django_apps/conda_sentinel/surface/views.py#L869)

**Planning**

- The `CPM-FR-3` amendment: two governed human writes, each with the three obligations
  [`sprint-change-proposal-2026-09-13.md:138`](../../planning-artifacts/sprint-change-proposal-2026-09-13.md#L138)

**Tests**

- An import never reactivates a retired row
  [`test_inventory_service.py:695`](../../../tests/integration/django_apps/test_inventory_service.py#L695)

- A rename onto another active row's name is refused
  [`test_inventory_service.py:876`](../../../tests/integration/django_apps/test_inventory_service.py#L876)

- A refused change re-renders as the change form for that row
  [`test_inventory_page.py:333`](../../../tests/integration/django_apps/test_inventory_page.py#L333)
