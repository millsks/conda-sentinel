---
title: 'CPM-IDENTITY-S08: Resolution that finds its mappings'
type: 'feature'
created: '2026-09-12'
status: 'done'
baseline_commit: '0c2a0b466dd959277fea202d2570c6c5bdef6a7e'
review_loop_iteration: 0
context:
  - _bmad-output/implementation-artifacts/epic-identity-context.md
  - _bmad-output/implementation-artifacts/stories/cpm-identity-s02-resolution-records-where-came-from.md
  - _bmad-output/implementation-artifacts/stories/cpm-currency-s03-feedstock-evidence.md
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** `CPM-FR-1` says the system resolves each package to a source repository, its PyPI
identity and its conda-forge feedstocks, and nothing does: `record_resolution` is a recorder with
no production caller, so on a real watchlist every package stays `unmapped` with every mapping
`unknown`, and `source_release`, `pypi_release` and `feedstock` select nothing. The architecture
names the missing piece -- "batch resolution jobs" -- and the demo seeder has been standing in for it.

**Approach:** An eleventh collector, `resolve_identity`, on the collect queue through the shared
base. Per package it reads PyPI's project document and conda-forge's `feedstock-outputs` index,
chooses a source repository from `project_urls` by a documented precedence, and hands what it found
to `record_resolution` at `inventory-derived` confidence. What each source said and what was chosen
is one append-only `identity_resolution_snapshots` row, so the review queue's `PackageMapping`
outcomes finally have evidence behind them.

## Boundaries & Constraints

**Always:**
- Identity is mutated only through `record_resolution` (`CPM-AD-14`); the collector never touches
  `Package`, `PackageMapping` or `Feedstock` directly, and passes `identity_source` and
  `associator_key` verbatim from the stored row.
- Confidence claimed is `IdentityConfidence.INVENTORY_DERIVED`; `canonical_name=""` (no correction).
  A `verified` package is never selected, so an override is never even offered a downgrade.
- Choosing and normalising the repository URL, building both locators, and reading both documents
  are pure functions (`CPM-AD-27`), reachable with no database, socket or clock.
- The normalised URL is one `source_release._repository_segments` accepts:
  `https://github.com/<owner>/<repo>`, lower-cased, `.git` and any `/tree|blob/...` tail stripped.
- Every `MappingKind` is answered on every run; `CROSS_ECOSYSTEM` is `not_found`.
- `FEEDSTOCK` is `not_found` only when the package holds no `Feedstock` rows; with rows already
  recorded and the index now silent, keep `established` and say so in `detail`.
- The second fetch follows `feedstock.py`'s bounded pattern: inside `translate`, never raises,
  its failure is a sentence in `detail` beside what the first document established.
- Each `record_resolution` call and its snapshot row share one per-package `transaction.atomic()`
  opened in `translate`, never around the recorder.
- Every roster in §Code Map gains the eleventh collector; stale "seven"/"ten"/"eight" prose is
  corrected where the roster is edited.
- Sprint status: add `cpm-identity-s08-resolution-finds-its-mappings: done` under `epic-identity`.

**Ask First:**
- Any GitHub API call, any second host beyond `pypi.org` and `raw.githubusercontent.com`.
- Any beat offset (the settings test pins the phased entries at exactly three).

**Never:**
- Change the demo seeder (`CPM-PLATFORM-S08`), correct a canonical name, compare an
  `IdentityConfidence` value anywhere in `src/` (the gate audit), or name another collector's
  evidence model in the new module.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|---|---|---|---|
| Both found | PyPI 200 with `project_urls.Source=https://github.com/psf/requests`; index 200 `{"feedstocks":["requests"]}` | SOURCE_REPOSITORY, RELEASE_ECOSYSTEM (`pkg:pypi/requests`, `pypi`), CONDA_ARTIFACT (`pkg:conda/requests`), FEEDSTOCK established with one `requests-feedstock` row; snapshot `ok` | N/A |
| Precedence | keys `Homepage=https://pyyaml.org/`, `Source Code=https://github.com/yaml/pyyaml` | `Source Code` wins; `Homepage` used only when no higher key and it is a github.com owner/repo | N/A |
| Repo URL unreadable | `Source=https://gitlab.com/x/y` or `https://github.com/only-owner` | SOURCE_REPOSITORY `not_found`; the rejected URL and why in `detail` | N/A |
| No PyPI project | index 200; PyPI 404 (the second fetch) | RELEASE_ECOSYSTEM and SOURCE_REPOSITORY `not_found`; feedstock still established | N/A |
| No feedstock | index 404 (the first fetch), package has no `Feedstock` rows | base writes the `not_found` sentinel row; nothing recorded on the package; re-offered next cadence | see Design Notes |
| Index says two feedstocks | `{"feedstocks":["a","b"]}` | two `Feedstock` rows, names `a-feedstock`, `b-feedstock`, urls under `conda-forge/` | N/A |
| Short name | `qt` | index locator `outputs/q/t/z/qt.json` (shard fill `z`) | N/A |
| Index malformed | 200 with `{"feedstocks": "x"}` or non-JSON | run `failed`, `error` sentinel row, nothing recorded | `ResolutionDocumentError` escapes `translate` |
| Nothing established | PyPI 404 and index 404, no prior rows | `record_resolution` at `unmapped` (confidence must be earned); snapshot `ok` with the two absences in `detail` | N/A |
| Already verified | `confidence=verified` | not selected | N/A |

</frozen-after-approval>

## Code Map

- `src/django_apps/conda_sentinel/core/collection.py:661` `Collector` -- ClassVars `name` :719,
  `evidence_model` :726, `observation_window` :731, `timeout` :737, `retries` :743, `rate_limit`
  :748, `freshness_target` :765, `response_cache_ttl` :772, `cadence` :802. Hooks
  `selectable_packages` :804 (lazy queryset), `inapplicability` :996, `source_for` :1029,
  `translate` :1042, `sentinel_evidence` :1067. `collect` :1168 makes exactly one fetch; a second
  one is the collector's own (`feedstock.py:1744,1816`, `source_release.py:1227`: `self._transport.fetch(locator, headers=request_headers(declared=self._headers, entry=None))`,
  catching `TransportError`, handling `not payload.found`, never raising).
- `collectors/feedstock.py` -- the template: two-call budget :222-232 (`FEEDSTOCK_TIMEOUT` sized so
  `worst_case_call_seconds` + one un-retried call fits the soft limit), `GITHUB_RAW_HOST` :276,
  cadence/freshness/window constants :198-213, `FEEDSTOCK_RATE_LIMIT` :246, `feedstock_repository`
  :624 (name with or without `-feedstock`, `_REPOSITORY_SEGMENT` :368), class :1377.
- `collectors/pypi_release.py` -- reuse `project_name(purl)` :366 (PEP 503), `PYPI_HOST` :230,
  `PURL_SCHEME`/`PURL_TYPE` :235, `_document_in` :547 with `MAX_DOCUMENT_CHARACTERS` :261 (copy the
  bound; do not import a private). `ReleaseIdentity` :313 shows how a mapping is read.
- `collectors/source_release.py:527` `_repository_segments` -- the acceptance rule the normalised
  URL must satisfy (`GITHUB_WEB_HOSTS` :300, two segments, `.git` stripped, lower-cased).
- `collectors/selection.py:104` `RESOLVED_CONFIDENCES` -- select with
  `Package.objects.exclude(confidence__in=RESOLVED_CONFIDENCES)`; a kwarg, not a comparison.
- `identity/services.py:457` `Resolution`, :432 `FeedstockMapping`, :702 `record_resolution`
  (refusals :1032-1391; `not_found` never blanks a column :891; feedstocks additive :965;
  caller owns the transaction :124). `identity/models.py:221` `MappingKind`, :289 `MappingOutcome`
  (`established` + the four sentinels; `ok` is not legal), :308 `ESTABLISHED`, :333 `MAPPED_FIELDS`,
  lengths :158-183 (URL/purl 512, name 128).
- `core/outcomes.py:110` `OutcomeState` values for the sentinel outcomes.
- `collectors/models.py:1134` `PyPIReleaseSnapshot` -- the row template (`package` PROTECT FK,
  `source`, `state`, `detail`, `trace_id`, `Meta.db_table`, read index, ok-biconditional check
  constraint; name constants :221-696 exported in `__all__`). Latest migration `0011_...`; new
  is `0012_identity_resolution_snapshots.py`.
- `collectors/tasks.py:206` task-name constant pattern (`*_TASK_NAME`, listed in `__all__` :127),
  task body :1352-1405; already imports `identity.services` :114.
- `collectors/apps.py:233` registration tuple; `core/registry.py:89` `register`.
- `src/config/settings/base.py:735` `CELERY_BEAT_SCHEDULE` -- add `cpm-sweep-resolve-identity`
  at `timedelta(days=1)`, no `options`.
- Rosters to extend: `tests/unit/django_apps/test_sweep.py:122` `PER_PACKAGE_COLLECTORS` and :196;
  `tests/unit/test_settings.py:1414` `dispatched`; `tests/integration/startup/test_stage_two_collector_registry.py:291`;
  `tests/unit/django_apps/test_collection.py:947,1002`; `tests/unit/test_model_registry.py:103`
  `EVIDENCE_MODEL_LABELS`; `tests/unit/django_apps/test_collector_base_audit.py:354-378`
  `THE_NEW_MODULES`; `tests/unit/django_apps/test_documented_subsystems.py:136,341`.
- Docs to extend: `docs/conda-sentinel/asynchronous-work.md:85` task table, :112 beat table
  ("Eight entries"), :189 demo table; `onboarding.md:192` ("Ten collectors" + row);
  `operations.md:1175`; `architecture.md:21` and `the-policy-run.md:45` ("ten evidence tables");
  `sweep.py:6-19` and `base.py:691` stale counts.
- Doubles: `tests/collectors.py:443` `ScriptedTransport` (per-locator answers and failures),
  :501 `recorded_payload`, :629 `FixedLimiter`, :563 `RecordingResponseCache`; template test
  `tests/integration/django_apps/test_feedstock.py:233-376`.
- Read-only: `identity/` (no change), `source_release.py`, `pypi_release.py`, `config/local_dev/`.

## Tasks & Acceptance

**Execution:**
- [x] `src/django_apps/conda_sentinel/collectors/resolve_identity.py` -- new. Constants
  (`COLLECTOR_NAME = "resolve_identity"`, cadence 1 day, freshness, window, timeout sized for two
  calls, rate limit, `FEEDSTOCK_OUTPUTS_HOST`), pure functions `index_locator(name)`,
  `project_locator(name)`, `repository_from(project_urls) -> ChosenRepository`,
  `normalised_repository(url) -> str | None`, `feedstocks_in(body, *, source)`,
  `project_urls_in(body, *, source)`, `resolution_for(...) -> Resolution`; `ResolutionDocumentError`;
  `IdentityResolutionCollector` with `translate` doing the second fetch, one atomic block per
  package around `record_resolution` + the returned row.
- [x] `collectors/models.py` -- `IdentityResolutionSnapshot`: `package`, `observed_at`, `state`,
  `source` (the first locator), `detail`, `trace_id`, plus `repository_url`, `repository_key`
  (which `project_urls` key won, blank when none), `pypi_found`, `feedstocks` (JSON list as the
  index stated), `confidence_recorded`, `downgrade_refused`; `db_table = "identity_resolution_snapshots"`.
- [x] `collectors/migrations/0012_identity_resolution_snapshots.py` -- the table.
- [x] `collectors/tasks.py` -- `COLLECT_RESOLVE_IDENTITY_TASK_NAME` and `resolve_identity(*, package_id, force=False)`.
- [x] `collectors/apps.py`, `src/config/settings/base.py` -- register; beat entry.
- [x] Rosters and docs listed in the Code Map -- add the eleventh; fix stale counts.
- [x] `tests/unit/django_apps/test_resolve_identity.py` -- every I/O row that is pure: precedence,
  normalisation table (`.git`, trailing slash, `tree/main/x`, `www.github.com`, gitlab, one segment,
  `http://`), locator sharding (`numpy`, `qt`, `r`, `zope.interface`), document refusals.
- [x] `tests/integration/django_apps/test_resolve_identity.py` -- `ScriptedTransport` end to end
  for both-found, PyPI-404, index-404, both-404, malformed index, prior-rows-kept, verified-not-selected;
  asserts the `PackageMapping` rows `pypi_release`/`feedstock`/`source_release` select on.
- [x] `_bmad-output/implementation-artifacts/sprint-status.yaml` -- the story line.

**Acceptance Criteria:**
- Given an `unmapped` package from the watchlist, when `cpm.collect.resolve_identity` runs against
  the two documents, then `source_release`, `pypi_release` and `feedstock` each select it.
- Given the same package run twice, when nothing changed, then no second `Feedstock` row and
  `resolved_at` does not advance, and a second snapshot row exists.
- Given beat starts, when the schedule is reconciled at boot, then exactly one entry names
  `resolve_identity` at its cadence and startup does not refuse.
- Given `pixi run ci`, when it runs, then it exits 0.

## Spec Change Log

## Design Notes

**The index is the first fetch, PyPI the second.** The base makes exactly one fetch and, when the
document is absent, writes the `not_found` sentinel itself -- `translate` is never reached, and
`sentinel_evidence` must not fetch (`feedstock.py` records why: a call from a row-shaping hook
loses its reason). So whichever source goes first, its absence ends the run. On a conda watchlist
the common case is a package on conda-forge and not on PyPI, not the reverse, so the index goes
first and PyPI is the bounded second call inside `translate`. The cost is stated rather than
hidden: **a package absent from conda-forge's index has no PyPI identity resolved by this story**,
its snapshot row says `not_found` and it is offered again next cadence. That is the same trade
`feedstock.py` made for its second call, recorded as deferred in the module docstring and the docs.

**On the matrix's "Nothing established" row.** It names *index 404 and PyPI 404*, which the
"No feedstock" row above it and the paragraph above this one already rule out: an index 404 ends
the run at the base's sentinel and PyPI is never asked. The recorder-at-`unmapped` path is reached
by an index that answers `200` with an empty list and a PyPI 404, and that is what the tests pin.
Recorded here (review, 2026-09-12) rather than by editing the frozen block.

**Could-not-ask never lowers what was established** (review, 2026-09-12). The resolver re-resolves
every non-`verified` package daily, and `record_resolution` writes an outcome row for every kind on
every call -- so a transient PyPI failure recorded as `error` would un-select a package from
`pypi_release` and `python_readiness` until the next good day. When PyPI could not be asked, the
two PyPI-backed kinds re-assert what the package currently holds; only a real 404 or a readable
document changes them.

**Labels are matched the way PyPI matches them, and a repository's own tracker counts** (run
against the stack, 2026-09-12). The first live run left nine of a hundred unresolved; two were the
resolver's: `xarray` labels its repository `source-code`, which PEP 753's normalisation (lower-case,
strip everything but letters and digits) makes the same label as `Source Code`; and `sqlalchemy`
publishes only `Issue Tracker: github.com/sqlalchemy/sqlalchemy/`, and a github.com owner/repo -- root or `/issues` -- under a tracker label names
the repository without inference. So keys are compared by normalised label, and the well-known
tracker labels are consulted last, only for a github.com owner/repo or its `/issues` page. The other seven
(`numba`, `protobuf`, `hypercorn` publish no repository; `git`, `sqlite`, `redis-py`, `pytorch` have
no PyPI project under the conda name) are honest `not_found`s.

**Repository choice.** `_PRECEDENCE = ("Source", "Source Code", "Repository", "Code", "GitHub")`,
matched case-insensitively on the stripped key; then `Homepage` only if it normalises. Keys outside
the list never win, however github-shaped their value.

**Both-absent lands at `unmapped`**: `_require_confidence_is_earned` refuses `inventory-derived`
with nothing established, and the recorder's own rule is the right one -- nothing was learned.

**Sweep order.** No offset: `source_release` and friends select nothing the first day and everything
the second; adding a fourth phased entry is a settings-test change this story does not make.

## Verification

**Commands:**
- `pixi run test` -- expected: green, including every extended roster.
- `pixi run -e dev python -m pytest tests/integration/django_apps/test_resolve_identity.py -q` -- expected: green.
- `pixi run ci` -- expected: exit 0, coverage >= 90%.

## Suggested Review Order

**The resolution: what is claimed, from what**

- Entry point: two documents in, one `Resolution` out; carry-forward when PyPI could not be asked
  [`resolve_identity.py:892`](../../../src/django_apps/conda_sentinel/collectors/resolve_identity.py#L892)

- The index is the base's fetch; PyPI is the bounded second call; one atomic block around the recorder
  [`resolve_identity.py:1194`](../../../src/django_apps/conda_sentinel/collectors/resolve_identity.py#L1194)

- The second call never raises: every failure becomes a `ProjectAnswer` with `asked` and a sentence
  [`resolve_identity.py:1261`](../../../src/django_apps/conda_sentinel/collectors/resolve_identity.py#L1261)

- What the recorder is handed verbatim: the stored pair plus the two outcomes it may re-assert
  [`resolve_identity.py:396`](../../../src/django_apps/conda_sentinel/collectors/resolve_identity.py#L396)

**Choosing a repository, and the locators**

- Precedence over `project_urls`, first occurrence wins, `Homepage` only when it normalises
  [`resolve_identity.py:726`](../../../src/django_apps/conda_sentinel/collectors/resolve_identity.py#L726)

- The form `source_release` can read: github.com, two segments, `.git` and `tree|blob` tails stripped
  [`resolve_identity.py:700`](../../../src/django_apps/conda_sentinel/collectors/resolve_identity.py#L700)

- conda-forge's sharding, alphanumerics only, `z`-filled, verified against the live index
  [`resolve_identity.py:531`](../../../src/django_apps/conda_sentinel/collectors/resolve_identity.py#L531)

- Feedstock names de-duplicated on repository so `x` and `x-feedstock` are one row
  [`resolve_identity.py:781`](../../../src/django_apps/conda_sentinel/collectors/resolve_identity.py#L781)

**Selection and budget**

- Not `verified`, and addressable by the recorder's pair
  [`resolve_identity.py:1113`](../../../src/django_apps/conda_sentinel/collectors/resolve_identity.py#L1113)

- Both calls go through the mounted retry policy, so the ceiling is twice the worst case
  [`resolve_identity.py:243`](../../../src/django_apps/conda_sentinel/collectors/resolve_identity.py#L243)

**The evidence row**

- `identity_resolution_snapshots`: what each source said, what was chosen, what confidence landed
  [`models.py:3032`](../../../src/django_apps/conda_sentinel/collectors/models.py#L3032)

- Depends on `identity.0001` for the package FK
  [`0012_identity_resolution_snapshots.py:39`](../../../src/django_apps/conda_sentinel/collectors/migrations/0012_identity_resolution_snapshots.py#L39)

**Wiring**

- The task, same shape as the ten before it
  [`tasks.py:1783`](../../../src/django_apps/conda_sentinel/collectors/tasks.py#L1783)

- Registered eleventh
  [`apps.py:245`](../../../src/django_apps/conda_sentinel/collectors/apps.py#L245)

- Daily, no offset: the four dependent sweeps select nothing the first day and everything the second
  [`base.py:820`](../../../src/config/settings/base.py#L820)

**Operator surface**

- The one collector that writes identity, its two hosts, and what `downgrade_refused` means
  [`operations.md:1173`](../../../docs/conda-sentinel/operations.md#L1173)

**Tests**

- The acceptance criterion: after one run, four collectors select the package
  [`test_resolve_identity.py:281`](../../../tests/integration/django_apps/test_resolve_identity.py#L281)

- The carry-forward rule under a transient PyPI failure
  [`test_resolve_identity.py:473`](../../../tests/integration/django_apps/test_resolve_identity.py#L473)

- The stated trade: an absent index ends the run and PyPI is never asked
  [`test_resolve_identity.py:365`](../../../tests/integration/django_apps/test_resolve_identity.py#L365)

- A recorder refusal rolls back, writes `error`, fails the run
  [`test_resolve_identity.py:508`](../../../tests/integration/django_apps/test_resolve_identity.py#L508)

- The doubled worst case fits the soft limit
  [`test_resolve_identity.py:271`](../../../tests/unit/django_apps/test_resolve_identity.py#L271)
