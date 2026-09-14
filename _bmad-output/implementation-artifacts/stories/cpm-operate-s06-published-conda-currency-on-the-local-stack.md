---
title: 'CPM-OPERATE-S06: Published-conda currency on the local stack'
type: 'feature'
created: '2026-09-13'
status: 'done'
review_loop_iteration: 1
followup_review_recommended: false
warnings: []
deferred: []
baseline_revision: 'f685e13b60a2f7d513085bb317e056168d353914'
context:
  - _bmad-output/implementation-artifacts/epic-operate-context.md
---

<intent-contract>

## Intent

**Problem:** `CPM_MONITORED_CHANNELS` and `CPM_MONITORED_PLATFORMS` ship empty, so on the
local stack `conda_package` and `license` select nothing and the published-conda surface
never appears -- the one column the docs call "unreachable locally".

**Approach:** Declare a local default in `config/settings/local.py` -- `("conda-forge",)` and
`("noarch", "linux-64")` -- so every local run (`manage.py`, the stack, `seed-demo`) observes
conda-forge; `base.py`, `production.py` and `test.py` stay empty and fail closed. The
declaration remains a reviewed code literal, never an environment read: the epic said
"via the dev feature's activation env", but the settings' own rule is that which surfaces
the product records evidence about is a decision worth a pull request, not an unreviewed
export, and `local.py` is the module only local runs load -- the outcome is met without
reversing that rule. Recorded as a deviation.

## Boundaries & Constraints

**Always:**
- `local.py` assigns both tuples after `from .base import *`, with a comment saying why the
  local module and not the environment, and that the two values are what the local stack
  observes and nothing else; the shape rules (`declaration_fault`) apply unchanged.
- `base.py`'s declaration and its comment are unchanged except one sentence pointing at
  `local.py` for the local default; `production.py` and `test.py` inherit empty.
- `pixi.toml` gains no variable; no task `env` and no activation env carries either name.
- Tests: `test_settings.py`'s "declared and ship empty" case is parametrised so `base`,
  `production` and `test` stay empty and `local` declares exactly the two tuples; a case
  that `local`'s values pass `declaration_fault` and fit `MAX_MONITORED_CHANNELS`; the
  manifest scan asserts neither name appears in any pixi table.
- Docs: `operations.md:440-480` -- the "ship empty" section says empty *deployed*, declared
  locally in `local.py`, and the declare-by-pull-request paragraph names both modules;
  `asynchronous-work.md:245` demo-table row (`conda_package`, `license` now observe on the
  stack); `running-it.md`'s "unreachable on the local stack" list loses published-conda
  currency (3.14 verification stays); the story's own note about the activation env in
  the epic (`epics.md` S06 block) gets a one-line "built as a local settings declaration
  instead; see the story" under its Constrained text.
- Sprint status: `cpm-operate-s06-published-conda-currency-on-the-local-stack: done`.

**Block If:** the local default breaks any integration test that relies on the empty
declaration under `config.settings.test` (it must not -- `test.py` stays empty; if a test
imports `local`, stop and report).

**Never:** an environment read for either setting; a non-empty default in `base.py`,
`production.py` or `test.py`; a change to `conda_package.py` or `license.py`.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|---|---|---|---|
| Local | `DJANGO_SETTINGS_MODULE=config.settings.local` | `CPM_MONITORED_CHANNELS == ("conda-forge",)`, `CPM_MONITORED_PLATFORMS == ("noarch", "linux-64")` | N/A |
| Deployed | `config.settings.production` | both `()`; collections fail closed naming the setting, as today | N/A |
| Suite | `config.settings.test` | both `()`; every existing case unchanged | N/A |
| Shape | the local values | `declaration_fault` returns `""`; `len(channels) <= MAX_MONITORED_CHANNELS` | N/A |
| Manifest | `pixi.toml` | neither name in any table | test lists the site |

</intent-contract>

## Code Map

- `src/config/settings/base.py:455-472` the declaration and its comment (the "worth a pull
  request" rule); `local.py:12-14` the import shape to append after.
- `collectors/conda_package.py:592` `declaration_fault(values, *, setting, what)`, :265 `MAX_MONITORED_CHANNELS`,
  :363 why `noarch` is a real subdir; `collectors/apps.py:257-291` the boot validation (unchanged).
- `tests/unit/test_settings.py:1280,1491-1540` the two monitored-surface cases (parametrised over
  `EVERY_SETTINGS_MODULE` :1269).
- `tests/integration/django_apps/test_conda_package.py:897-927` the ceilings case (declares its own channels).
- Docs: `operations.md:440-480`, `asynchronous-work.md:245`, `running-it.md` (the "unreachable" bullet), `epics.md` S06.
- Read-only: `conda_package.py`, `license.py`, `apps.py`, `pixi.toml`.

## Tasks & Acceptance

**Execution:**
- [x] `src/config/settings/local.py` -- the two tuples and the comment; `base.py` -- one pointer sentence.
- [x] `tests/unit/test_settings.py` -- the parametrised expectation, the shape case, the manifest scan.
- [x] Docs and the epic note.
- [x] `sprint-status.yaml` -- story `done`.

**Acceptance Criteria:**
- Given the stack up, when `pixi run stack-run dispatch_sweep conda_package license` runs, then
  both dispatches offer every package and their collections write rows naming `conda-forge`.
- Given `config.settings.production`, when settings load, then both tuples are empty.
- Given `pixi run ci`, when it runs, then it exits 0.

## Spec Change Log

- 2026-09-13: the local override is written without a type annotation. Annotating a name that
  `from .base import *` already brought in is a `no-redef` under strict mypy; the leaf modules
  override every other inherited value the same way, and `base.py` keeps the annotation.
- 2026-09-13: `stack-run` pins `CELERY_TASK_ALWAYS_EAGER = "0"` in its task `env`, which
  overrides the caller's, so the inline live check carried the stack's three variables
  directly (`stack-seed`'s shape: `CELERY_TASK_ALWAYS_EAGER=1 REDIS_URL=... DATABASE_URL=...
  pixi run -e dev python manage.py dispatch_sweep conda_package license`). Observed: both
  dispatches `succeeded, offered 148 package(s)`; 296 `conda_package_snapshots` rows, 148 per
  platform, every one naming `conda-forge` (14 `ok`, 12 `not_found`, the rest the thirty-a-minute
  allowance refusing an inline full sweep, as the docs predict); 238 licence findings naming
  `conda-forge`. `config.settings.production` reads `() ()`.
- 2026-09-13 (review 1): **the currency pass's pair selection was a policy defect any
  two-platform declaration would hit, and it is fixed.** `policies/currency.py`'s
  `observed_surface` ordered one instant's `(channel, platform)` rows on channel then platform
  alone, so with `("noarch", "linux-64")` declared a noarch-only package (most pure-Python
  packages on conda-forge) was judged by its `linux-64` `not_found` row while its `noarch` row
  said `ok` -- proven by a case against the unchanged code before the fix. The selection now
  ranks a row carrying `ok` before any sentinel within one `observed_at`, then the existing
  channel/platform/pk ordering; `not_found` is the verdict only when no pair at that instant
  answered `ok`, and the preference never reaches across sweeps. The choice is selection code,
  not a versioned rule (`policy-parameters.toml` carries no pair-selection rule), so no
  rule-version bump. The case that pinned the old limitation
  (`test_the_conda_verdict_can_be_about_a_channel_that_simply_does_not_carry_the_package`) is
  rewritten to the new behaviour; four cases pin it (noarch-only, compiled-only, all-`not_found`,
  newer-`not_found`-beats-older-`ok`). `operations.md`'s "which channel it is about" section and
  the `local.py` rationale say what the policy now does.
- 2026-09-13 (review 1): the manifest scan is one shared walker, `tests/pixi_manifest.py`'s
  `variable_sites` over `activation_envs` (all four table shapes, non-dict `activation`
  guarded), `activation_scripts` (each script's text read) and every task `env`;
  `test_locality_declaration.py` reads the same walkers rather than its own copies.
- 2026-09-13 (review 1): the FR-12 guarantee cited in `local.py` is
  `config/startup/stage_one.py`'s `_refuse_the_local_settings_module` -- the review's
  `forbidden_states.py:247` does not exist in this tree.

## Auto Run Notes

**Block-If check (tests importing `config.settings.local`).** Sixteen modules import it
(fifteen pre-existing plus this story's `tests/integration/startup/test_local_monitored_surface.py`):
`test_collector_boot_refusal.py`, `test_stage_two_fires.py`, `test_stage_two_served_path.py`,
`test_import_resolution.py`, `test_local_dev_bearer_flow.py`, `test_collector_base_audit.py`,
`test_rate_limit.py`, `test_response_cache.py`, `test_refusal_coverage_audit.py`,
`test_stage_one_escape_route.py`, `test_stage_two_urlconf.py`, `test_local_dev_urls.py`,
`test_no_network_at_boot.py`, `test_payload_properties.py`, `test_settings.py`. All sixteen pass
with the declaration (560 tests): they compose probe settings from `local` or read other values
from it; none asserted the empty tuple, and the one that did (`test_settings.py`) is the case this
story parametrised.

## Design Notes

**Why `local.py` and not the activation env.** `pixi.toml`'s dev activation env is where
S03 and S04 put their switches, and both were switches. This is a declaration of *what the
product observes*, which the settings module's own comment says must be reviewed code. The
local module is reviewed code that only local runs load; that is the same governance with
the outcome the epic asked for.

## Verification

**Commands:**
- `pixi run test` -- green.
- `pixi run docker-up && pixi run stack-run dispatch_sweep conda_package license` (eager, inline) -- two dispatch rows offering every package; the resulting evidence rows name `conda-forge`.
- `DJANGO_SETTINGS_MODULE=config.settings.production pixi run -e dev python -c 'from django.conf import settings; import django; django.setup(); print(settings.CPM_MONITORED_CHANNELS)'` -- `()` (may need the production module's required env; if it refuses to load for unrelated reasons, the settings test covers it).
- `pixi run ci` (stack down) -- exit 0.

## Auto Run Result

Status: done
Deviation: built as a reviewed declaration in `config/settings/local.py` rather than the
epic's "dev activation env" -- the settings' own rule is that what the product observes is
worth a pull request, not an export; the outcome (on locally, empty deployed and in the
suite) is the same, and the FR-12 stage-one refusal guarantees the local module cannot reach
a deployment.
Review: three layers; the verification-gap layer found nothing; eight patches. One was a
real defect beyond this story: the currency policy read the published-conda rows in
`(channel, platform)` order, so with two platforms declared a noarch-only package was judged
by its `linux-64` `not_found` row while its `noarch` row said `ok`. Proven with a failing
case first, then fixed in `policies/currency.py` (an `ok` pair wins within one observation
instant; selection code, not a versioned rule); both directions pinned.

## Suggested Review Order

**The declaration**

- Entry point: the local module declares conda-forge and two platforms; why here and not the environment
  [`local.py:336`](../../../src/config/settings/local.py#L336)

**The defect the declaration exposed**

- An `ok` pair wins over a sentinel within one instant; newest observation still first
  [`currency.py:337`](../../../src/django_apps/conda_sentinel/policies/currency.py#L337)

- The case that failed on the unchanged policy
  [`test_currency_policy.py:943`](../../../tests/integration/django_apps/test_currency_policy.py#L943)

**Tests**

- A real boot from `config.settings.local` through `ready()`; both collectors offer the seeded package
  [`test_local_monitored_surface.py:1`](../../../tests/integration/startup/test_local_monitored_surface.py#L1)

- One manifest walker for every place a variable can be exported
  [`pixi_manifest.py:204`](../../../tests/pixi_manifest.py#L204)
