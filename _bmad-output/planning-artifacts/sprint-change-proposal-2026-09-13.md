---
title: 'Sprint Change Proposal — Operating the Inventory'
date: 2026-09-13
sourceWorkflow: 'appended directly, on the product owner's instruction, in the form bmad-correct-course produces'
status: 'approved-pending-application'
scope: 'Moderate'
triggeredBy: 'Stakeholder questions — how a package is added, whether a task starts the collectors, what maintenance the platform lacks'
inputDocuments:
  - _bmad-output/planning-artifacts/architecture/architecture-conda-package-supply-chain-monitor-2026-09-02/ARCHITECTURE-SPINE.md
  - _bmad-output/planning-artifacts/epics.md
  - _bmad-output/planning-artifacts/sprint-change-proposal-2026-09-04.md
  - _bmad-output/implementation-artifacts/stories/cpm-identity-s08-resolution-finds-its-mappings.md
  - _bmad-output/implementation-artifacts/stories/cpm-platform-s08-the-demo-asserts-nothing-it-never-observed.md
---

# Sprint Change Proposal — Operating the Inventory

## 1. Issue Summary

On 2026-09-13, with `CPM-IDENTITY-S08` and `CPM-PLATFORM-S08` merged, the product owner
asked three questions: how a package is added to the inventory, whether there is a task
that starts the collectors, and what maintenance the platform still lacks. Every answer
was a `manage.py shell -c` snippet. Measured on the local stack that morning:

- The inventory is a CSV (`collectors/data/watchlist-development.csv`, 107 rows;
  `watchlist.csv` deployed, header only). Ingestion, a sweep and a policy run each exist
  only as a Celery task with no management command and no pixi task. The only management
  command in the product is `replay_policy_run`.
- A shell aimed at the stack must carry `DATABASE_URL`, `REDIS_URL` and
  `CELERY_TASK_ALWAYS_EAGER=0` or it silently talks to SQLite and runs the task inline —
  which is exactly what happened on 2026-09-12.
- The demo seeder is a second inventory keyed `pypi:<name>`; 45 of the 107 watchlist
  names overlap it, so ingesting the watchlist onto a seeded database collides on
  `Package.canonical_name`.
- `django_celery_beat`'s interval entries start their clock when created: a fresh stack
  fires no daily sweep for a day and no weekly one for a week (`last_run None`, `total 0`
  on every entry after a seed).
- `source_release` reads GitHub unauthenticated at 60 calls an hour, charged four per
  collection: about fifteen packages an hour. `feedstock` is ten a minute. No token
  setting exists.
- `CPM_MONITORED_CHANNELS` is empty locally, so `conda_package` and `license` select
  nothing on the stack and published-conda currency never appears.
- Evidence is append-only and nothing prunes any table; `identity_resolution_snapshots`
  grows one row per package per day.

None of these is a collector defect. They are the gap between a product that collects
and one that somebody operates.

## 2. Impact Analysis

### Epic impact

No existing epic changes. A new epic, `CPM-EP-OPERATE`, is added after `CPM-EP-DOCS` in
the Epic List and as the last story section, on the terms `CPM-EP-DOCS` established: raised
by the product owner, covering no new FR, with every acceptance criterion drafted by the
implementing agent from measured behaviour.

### Story impact

Ten stories, `CPM-OPERATE-S01`–`S10`, ordered by dependency: the stack shell first (every
later story is used through it), then the commands, then one inventory, then day-one
observation, then the three declarations (GitHub token, local channel default, retention),
then the per-package re-run from the page, which lands after `S02` so both enqueue the
same tasks. `S08` depends on `CPM-EP-APP`. `S09` (operator digest) and `S10` (the evidence tables
measured at scale) were added the same day at the product owner's direction, and `S07` was
set to ninety days purged nightly rather than keep-everything, on the same instruction.

### Artifact conflicts

- `docs/conda-sentinel/running-it.md` and the operator runbook carry environment-prefixed
  shell recipes that `S01` and `S02` replace.
- `CPM-IDENTITY-S07`'s deferred item (the deployed watchlist is changed by review and
  shipped by release) is what `S03` resolves: the inventory becomes a governed table with an
  audited write, the file becomes its first-run seed, and the CSV adapter stays for the
  import and for a deployment that declares no database inventory. Decided by the product
  owner on 2026-09-13 after asking how a file in the wheel is updated.
- `CPM-AD-2`'s append-only rule is not softened by `S07`: the base gains one audited door
  for one command, the retention is ninety days purged nightly, a package's newest row per
  table is always kept, and replay inside the window must reproduce.

### Technical impact

Management commands become `[[admin_processes]]` in `component.toml`, which
`tests/unit/test_process_model.py` reconciles; `CPM_SWEEP_ON_BEAT_START`,
`CPM_GITHUB_TOKEN` and the local `CPM_MONITORED_CHANNELS` default are declared settings
read at settings time, failing closed toward deployment.

## 3. Recommended Approach

Append, do not re-plan. The epic and its stories are written into `epics.md` in the
file's own conventions and the sprint status gains a `backlog` block; no existing epic or
story is edited. Implementation proceeds one story per pull request through `bmad-build`,
as every story since `CPM-PLATFORM-S03` has.

## 4. Detailed Change Proposals

### 4a. epics.md

Epic List: `### `CPM-EP-OPERATE`: Operating the inventory` added before "Build order";
the build-order graph gains `ID --> OP`, `CUR --> OP`, `APP --> OP`. Stories: a new
`## CPM-EP-OPERATE` section before "Test design integration" with `S01`–`S08`.

### 4b. sprint-status.yaml

A `# CPM-EP-OPERATE` block after `CPM-EP-NL`: `epic-operate: backlog`, ten story keys at
`backlog`, `epic-operate-retrospective: optional`.

### 4c. Not changed

Architecture spine, UX designs, memlogs. The PRD is not changed here, but `S03` will
propose an amendment to `CPM-FR-3` ("the only human write that mutates governed reference
data") to name the inventory write beside the identity override; that amendment travels
with the story's pull request. No architecture decision is amended. Should `S07` need an amendment to `CPM-AD-2`'s wording ("one audited
door"), that is the story's to propose when it is built.

## 5. Implementation Handoff

Stories are ready for `bmad-build` in order. `S01` first; it is a morning's work and every
later story's verification runs through it.

## 6. Not Resolved, and Why

- **Cross-ecosystem naming.** A name-keyed resolution reads whatever PyPI project shares
  a conda package's name (`nodejs`, `ffmpeg`, `ripgrep`). Recorded in `deferred-work.md`
  by `CPM-PLATFORM-S08`; it belongs to `CPM-EP-IDENTITY` (the `CROSS_ECOSYSTEM` mapping),
  not to operating the inventory.
- **The override correcting a mapping** (`numba`'s repository by hand). A `CPM-IDENTITY-S09`
  candidate; it widens `CPM-IDENTITY-S05`'s `Correction` and is that epic's to sequence.
