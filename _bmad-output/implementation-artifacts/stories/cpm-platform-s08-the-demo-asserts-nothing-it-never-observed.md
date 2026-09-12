---
title: 'CPM-PLATFORM-S08: The demo asserts nothing it never observed'
type: 'feature'
created: '2026-09-12'
status: 'done'
baseline_commit: '0204ccf1447bf517619931efd41fb2e66592a209'
review_loop_iteration: 0
context:
  - _bmad-output/implementation-artifacts/epic-platform-context.md
  - _bmad-output/implementation-artifacts/stories/cpm-platform-s06-the-stack-seeds-what-it-serves.md
  - _bmad-output/implementation-artifacts/stories/cpm-identity-s08-resolution-finds-its-mappings.md
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** The demo seeder invents what it never observed: every identified package is `verified`
with `source_repository_url=https://github.com/demo/<name>`, purls asserted for packages with no
PyPI project, a feedstock asserted or denied by a hand-kept roster flag (conda-forge disagrees for
`flask-cors` and `shap`), and evidence rows whose sources are fictions -- a `github.com/demo`
release, a feedstock with an invented idle age, a conda build `py312h0`, Python-readiness verdicts
and `demo://builds/...` verification logs. A reviewer who checks any of it finds nothing, and learns
to stop checking. Product owner, verbatim: "instead of dummy/demo data I want it to be empty or
UNKNOWN" -- "all of the seeded data that we see in the UI to be factual".

**Approach:** The seeder seeds only what has a real source -- the roster's name, version pair,
OSV advisory, CISA KEV listing and licence -- and creates each package as the inventory does, a
shell at `unmapped` with no mapping rows. It then runs the `resolve_identity` collector inline for
every package, so a fresh `stack-seed` shows real repositories, feedstocks and honest `not_found`s
on first paint, and the stack's own sweeps observe everything else.

## Boundaries & Constraints

**Always:**
- Identity is written only through `identity`'s two doors: `resolve_package_shell` for the shell,
  `IdentityResolutionCollector.collect` (which calls `record_resolution`) for the mappings. The
  seeder itself never calls `record_resolution` and never sets a confidence.
- `DemoPackage` keeps exactly: `name`, `upstream_version`, `installed_version` (positional, as the
  roster memory pins), `advisory`, `kev_listed`, `kev_catalogued`, `licence`. `identified`,
  `feedstock`, `feedstock_idle_days`, `fix_reached`, `python_evidence`, `errored` are removed with
  every roster use of them.
- Evidence seeded per package is exactly: `PyPIReleaseSnapshot` (source `https://pypi.org/project/<name>/`,
  `latest_version=upstream_version`), the advisory's `VulnerabilityFinding` (real OSV id, `MATCHED`,
  or the `unknown` nothing-matched row when the roster has none), `KevFinding`, `LicenseFinding`.
  No `SourceReleaseSnapshot`, `FeedstockSnapshot`, `CondaPackageSnapshot`,
  `PythonReadinessAssessment`, `PythonVerificationResult`, and no invented `error` rows.
- The resolver runs after every package is seeded and before the policy run, once per package with
  `force=True`, through the collector class constructed with an unmetered `RateLimiter` (a local-dev
  class in `demo_data.py` whose `permit` always allows -- the daily sweep keeps the real allowance)
  and a `transport` the caller may substitute (`seed_demo_inventory(*, transport=None)`), so the
  integration suite never reaches the network.
- A resolution that fails -- no network, index absent, PyPI unreadable -- leaves that package
  `unmapped` with its `error`/`not_found` snapshot and is logged with the package name; the seed
  continues and its summary counts `resolved`, `unresolved`. The seed never fails whole for it.
- The seeder's own run stays filed under `local-dev-demo-seed`; the resolver's runs are real runs
  under `resolve_identity`, and the Coverage screen showing them is the screen telling the truth.
- Every test that asserted an invented fact now asserts the factual one; the integration suite
  seeds through a scripted transport that answers the index and PyPI per roster name.
- Sprint status: `cpm-platform-s08-the-demo-asserts-nothing-it-never-observed: done` under
  `epic-platform`.

**Ask First:**
- Running any collector other than `resolve_identity` at seed time.
- Any change to `resolve_identity.py`, `identity/`, or the roster's advisory rows.

**Never:**
- Hand-enter a repository, feedstock or purl for any package (the seven no public source names
  stay `not_found`); seed at `verified`; write `Package`, `PackageMapping` or `Feedstock` directly.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|---|---|---|---|
| Fresh seed, sources answer | empty stack DB; transport answers index and PyPI for a name | shell created `unmapped`; after the inline run: `inventory-derived`, repository/purls/feedstocks established, one `identity_resolution_snapshots` row `ok` | N/A |
| Not on conda-forge | index 404 for `internal-telemetry-sdk` | stays `unmapped`, snapshot `not_found`, policy gate writes `unknown`; counted `unresolved` | N/A |
| No network | transport raises `TransportError` for every locator | every package stays `unmapped` with an `error` snapshot; seed completes; summary `resolved=0, unresolved=100`; one log line per package | logged, never raised |
| Seed twice | run again the same day | packages reused, evidence appended, resolver runs again (`force=True`) and records nothing new; a second policy run | N/A |
| Roster row | any `DemoPackage` | no field names a repository, feedstock, readiness or build; `installed <= upstream` guard still holds | N/A |
| Deployed | `COMPONENT_RUNTIME` unset | refused before any write, as today | `ImproperlyConfigured` |

</frozen-after-approval>

## Code Map

- `src/config/local_dev/demo_data.py` -- `DemoPackage` :117-196 (remove `identified` :135, `feedstock`
  :156, `feedstock_idle_days` :157, `fix_reached` :172, `python_evidence` :191, `errored` :196);
  roster :253-532 (100 rows; `feedstock=False` at 458, 479, 484, 486, 493, 510, 512; `errored=` at
  476, 498, 509; `identified=False` at 530-531 -- keep both rows, drop the flag); `seed_demo_inventory`
  :535-574 (add the resolver pass between `_seed_evidence` and `_run_policy`; widen the summary);
  `_seeded_package` :577-633 (keep :600-605, delete :606-632); `_feedstocks` :636 (delete);
  `_seed_evidence` :659-706 (keep PyPI :685, advisory :770-849, KEV :842, licence :868-906; delete
  source :678, conda :692, `_seed_feedstock` :709-746, `_seed_python` :909-951, verification :953);
  `_surface_version` :749 (delete); `"advisory" in demo.errored` :788 (delete the error branch);
  module docstring :21-24 (rewrite the premise); `DEMO_COLLECTOR` :89 (keep).
- `src/django_apps/conda_sentinel/collectors/resolve_identity.py:1071` `IdentityResolutionCollector(clock=, transport=None, limiter=None, response_cache=None)`;
  `collect(package_id=, force=)` :`core/collection.py:1168` returns `CollectionResult` with `.state`;
  transport failure -> `FAILED` + `error` row, never raises; index 404 -> `SUCCEEDED` + `not_found`
  row; `ResolutionLocatorError` from `source_for` escapes -- catch it per package.
- `src/django_apps/conda_sentinel/core/rate_limit.py:248` `RateLimiter` Protocol -- implement it
  locally (`permit` signature there); `:281` `CacheRateLimiter` is the default to bypass.
- `src/django_apps/conda_sentinel/identity/services.py:620` `resolve_package_shell` (the only identity
  call left); `:1293` `_require_confidence_is_earned` is why no confidence is seeded.
- `src/config/local_dev/seed_demo.py:24-44` `main()` logs the summary -- carries the new counts.
- `pixi.toml:687` `stack-seed` (already `CELERY_TASK_ALWAYS_EAGER="1"`; add "resolves identity
  live" to its description); `:592` `seed-demo`.
- Tests: `tests/unit/test_local_dev_demo_data.py` -- :78-87 (`_ESTABLISHED` vestigial), :114-128
  (variety: drop feedstock/identified lines, add "every row has a real-sourced field set only"),
  :234-243 and :246-256 (delete: feedstock), :189-200 and :280-318 (keep). `tests/integration/test_local_dev_demo_seeding.py`
  -- :42-51 `THE_UNMAPPED_PACKAGES` (derive from the scripted transport's 404 list), :145-158
  (`verified` -> `inventory-derived` for resolved, `unmapped` for the two), :162-169 (collectors
  == {`local-dev-demo-seed`, `resolve_identity`}), :178-317 verdict-variety cases (re-derive from
  what PyPI + advisory evidence at `inventory-derived` factually yields; check `core`'s gate treats
  `inventory-derived` as label-only), :336-352 (twice), the `seeded` fixture (pass the transport).
  Transport doubles: `tests/collectors.py:443` `ScriptedTransport`, `:501` `recorded_payload`;
  build answers from `resolve_identity.index_locator(name)` / `project_locator(name)`.
- Docs: `docs/conda-sentinel/asynchronous-work.md:198-216` demo table + danger admonition (the
  `github.com/demo` sentence is gone; the resolver row now says all 100 resolve at seed time);
  `development.md:43-59` ("everything around them is a fixture", "seven of the ten fire");
  `running-it.md:11,214,238-252` (needs network at seed; what a fresh seed shows and what stays
  `unknown` until the sweeps run); `onboarding.md:53-54,172-174` (the exercise: `internal-telemetry-sdk`
  is `unmapped` because conda-forge has no such package -- still true); `index.md:70`.
- Read-only: `collectors/resolve_identity.py`, `identity/`, `collectors/data/watchlist-development.csv`.

## Tasks & Acceptance

**Execution:**
- [x] `src/config/local_dev/demo_data.py` -- shrink `DemoPackage` and the roster; shells only;
  evidence to the four real-sourced rows; `_Unmetered` limiter; `_resolve_identities(packages, *,
  clock, transport)` returning `(resolved, unresolved)`; summary widened; docstrings rewritten.
- [x] `src/config/local_dev/seed_demo.py` -- log the new counts.
- [x] `pixi.toml` -- `stack-seed` description.
- [x] `tests/unit/test_local_dev_demo_data.py` -- roster shape (`__dataclass_fields__` is exactly the
  seven), no string in the module contains `github.com/demo`, no `verified` literal, limiter permits.
- [x] `tests/integration/test_local_dev_demo_seeding.py` -- scripted transport fixture; every matrix
  row; the resolver ran once per package (`CollectionRun` count for `resolve_identity` == 100);
  no-network run; verdict variety re-derived.
- [x] Docs listed in the Code Map.
- [x] `_bmad-output/implementation-artifacts/sprint-status.yaml` -- the story line.

**Acceptance Criteria:**
- Given an empty stack database and network, when `pixi run -e dev stack-seed` runs, then zero
  packages carry `github.com/demo`, none is `verified`, `django` carries
  `https://github.com/django/django`, and the two `internal-*` packages are `unmapped`.
- Given the integration suite, when it runs, then it makes no network call.
- Given `pixi run ci`, when it runs, then it exits 0.

## Spec Change Log

- **2026-09-12, review pass 1 (patch, recorded here because it narrows the frozen evidence list).**
  Triggering finding: the seed still asserted two observations it never made -- a
  `PyPIReleaseSnapshot` for the nine names PyPI has no project for, and a `not_found` licence row
  for eight packages whose roster licence is merely blank. Amendment: the resolver runs *before*
  evidence; the PyPI snapshot is seeded only where the resolver has just established the PyPI
  ecosystem, and a licence row only where the roster states a licence. Known-bad state avoided:
  `pypi.org/project/nodejs/` "stating" 24.10.0, conda-forge "declaring" no licence for `quart`.
  KEEP: shells through `resolve_package_shell` only; advisory and KEV rows for every package;
  the unmetered limiter at seed time; the transport seam.

## Design Notes

**Why shells and not all-`unknown` resolutions.** `record_resolution` at `inventory-derived` with
nothing established is refused, and at `unmapped` with five `unknown` rows it would claim "a resolver
ran and concluded nothing" -- which the seeder did not do. `resolve_package_shell` is the inventory's
own door and says exactly what is true: this package exists and nothing has been resolved.

**Why the limiter is bypassed at seed time and nowhere else.** The resolver's allowance charges
`1 + retries` per collection, so 100 inline collections would fail 85. The seed is a one-off of
200 requests to two public hosts; the daily sweep keeps the courtesy allowance.

**What the demo shows after this.** On first paint: identity real; advisories real; the PyPI
surface of currency `current` by construction (PyPI is the only surface seeded, so the authority is
compared with itself). Feedstock presence, upstream-release currency and Python readiness read
`unknown` until the stack's sweeps observe them, and that takes longer than minutes: `source_release`
is daily at 60 requests an hour charged `1 + retries` per collection, so about fifteen packages an
hour and several daily sweeps to cover 98; `pypi_release` is daily at 60 a minute on the same charge;
`feedstock` and `python_readiness` are weekly. **No sweep fires at stack start** -- beat's interval entries start their clock when created (verified: `last_run None`, `total 0` on a fresh stack), so day one needs a hand dispatch, which `running-it.md` now gives.
Published-conda and 3.14 verification stay `unknown` locally -- the first needs
`CPM_MONITORED_CHANNELS`, the second is only ever triggered by hand -- so `not_applicable`,
`awaiting_build` and priority `p8`/`p9` are never reached on the local stack. That is the product's
real shape, stated on the screen rather than painted over.

**Review patches (2026-09-12).** The seed order is shells, resolver, evidence, so the PyPI snapshot
is written only where the resolver has just established the `release_ecosystem` mapping, and a
blank roster licence seeds nothing. The summary counts `resolved`, `not_on_conda_forge`,
`unreachable` and `verified_kept`, read off the package's confidence after each run rather than off
the run's state; a package a person has set `verified` is never offered (the collector's own
`selectable_packages` decides); three consecutive `unreachable` results stop the resolver and count
the rest without asking. A healthy live seed prints
`resolved=98 not_on_conda_forge=2 unreachable=0 verified_kept=0`.

## Verification

**Commands:**
- `pixi run test` -- expected: green.
- `pixi run -e dev python -m pytest tests/integration/test_local_dev_demo_seeding.py -q` -- expected: green, no network.
- `pixi run local-stack-down && pixi run ci` -- expected: exit 0.
- `pixi run docker-down-v && pixi run -e dev stack-seed && pixi run local-stack` then the AC query
  against `DATABASE_URL=postgres://conda_sentinel:local-development-only@localhost:5433/conda_sentinel`.

## Suggested Review Order

**What the seed asserts, and what it leaves to observation**

- Entry point: shells, then the resolver, then only the evidence the resolver's answer licenses
  [`demo_data.py:480`](../../../src/config/local_dev/demo_data.py#L480)

- The roster is seven real-sourced fields; every identity and readiness flag is gone
  [`demo_data.py:175`](../../../src/config/local_dev/demo_data.py#L175)

- The inventory's own door and nothing else: a shell at `unmapped`, no mapping rows
  [`demo_data.py:544`](../../../src/config/local_dev/demo_data.py#L544)

- A PyPI row only where the resolver has just established the PyPI ecosystem
  [`demo_data.py:601`](../../../src/config/local_dev/demo_data.py#L601)

- A licence row only where the roster states one; blank seeds nothing
  [`demo_data.py:722`](../../../src/config/local_dev/demo_data.py#L722)

**Running the resolver inline**

- One collection per package; `verified` skipped; three-way count from the confidence after the run
  [`demo_data.py:784`](../../../src/config/local_dev/demo_data.py#L784)

- The seed-time courtesy exception: an unmetered limiter, and why the sweep keeps the real one
  [`demo_data.py:453`](../../../src/config/local_dev/demo_data.py#L453)

- Three consecutive unreachable results end the loop rather than an hour of timeouts
  [`demo_data.py:171`](../../../src/config/local_dev/demo_data.py#L171)

- What the summary reports and what the healthy numbers are
  [`demo_data.py:761`](../../../src/config/local_dev/demo_data.py#L761)

**Operator surface**

- What a fresh seed shows, what is delayed, what is unreachable locally
  [`running-it.md:145`](../../../docs/conda-sentinel/running-it.md#L145)

**Tests**

- The acceptance criterion end to end, through a scripted transport, no socket
  [`test_local_dev_demo_seeding.py:264`](../../../tests/integration/test_local_dev_demo_seeding.py#L264)

- A recorder refusal is one package's failure, never the seed's
  [`test_local_dev_demo_seeding.py:634`](../../../tests/integration/test_local_dev_demo_seeding.py#L634)

- A package a person verified is left alone by a second seed
  [`test_local_dev_demo_seeding.py:674`](../../../tests/integration/test_local_dev_demo_seeding.py#L674)
