---
title: 'CPM-OPERATE-S05: An authenticated GitHub allowance'
type: 'feature'
created: '2026-09-13'
status: 'done'
review_loop_iteration: 0
followup_review_recommended: false
warnings: []
deferred:
  - 'With a token the feedstock collector still declares the search allowance for both branches, so mapped-branch packages are throttled to ~450/h against the core pool (deferred-work.md)'
  - 'A restart with a new token is judged under the new allowance until the current window turns, because window_key names collector and window only -- documented, not changed (deferred-work.md)'
baseline_revision: '8f18f3c9bf8bd2b4b957d7aa692e5cd2ac664255'
context:
  - _bmad-output/implementation-artifacts/epic-operate-context.md
  - _bmad-output/implementation-artifacts/stories/cpm-operate-s04-day-one-is-observed.md
---

<intent-contract>

## Intent

**Problem:** `source_release` reads GitHub unauthenticated at 60 calls an hour, charged
four per collection: fifteen packages an hour, so a 10,000-package inventory cannot be
swept inside its two-day freshness target (`CPM-NFR-1`). `feedstock` reads GitHub's search
at ten a minute. No token setting exists.

**Approach:** A `CPM_GITHUB_TOKEN` setting. When present, the two GitHub-reading collectors
send it as a bearer header to `api.github.com` and declare GitHub's authenticated
allowances; the token appears in no log line, ledger row, evidence row or `detail`; a
credential GitHub refuses is a `failed` run whose detail says so and names neither the
credential nor its prefix; with no token, nothing changes.

## Boundaries & Constraints

**Always:**
- Setting: `base.py` `CPM_GITHUB_TOKEN = env.str("CPM_GITHUB_TOKEN", default="").strip()`;
  never in `pixi.toml` (no activation env, no task `env`), never in `compose.yaml`, never
  in any test fixture as a literal that could be mistaken for a real token (use
  `"token-for-tests"`). A developer exports it in their shell before `pixi run local-stack`.
- One model-free module `collectors/github.py` (importable at settings time; neither
  collector imports the other -- `CPM-AD-7`): `GITHUB_TOKEN_SETTING`, `github_token()` (reads
  `django.conf.settings` at call time, stripped, `""` when absent), `token_fault(value) -> str`
  (non-string, CR/LF, embedded whitespace, non-ASCII, or wider than 512 refused with a
  sentence that never echoes the value), `authenticated_headers(declared, token)` (adds
  `Authorization: Bearer <token>`), `AUTHENTICATED_CORE_ALLOWANCE = RateLimit(5000, 1 h)`,
  `AUTHENTICATED_SEARCH_ALLOWANCE = RateLimit(30, 1 min)`.
- `CollectorsConfig.ready()` refuses a malformed token at boot with `ImproperlyConfigured`
  naming the setting (the `CPM_MONITORED_*` shape), never the value.
- Both collectors set `self.headers` and `self.rate_limit` as instance attributes in an
  `__init__` override before `super().__init__()`, from `github_token()`; the ClassVars keep
  the unauthenticated declarations so every existing declaration test holds when no token
  is set. The base's `_require_headers` still validates the assembled headers.
- The bearer header reaches `api.github.com` only: `feedstock`'s recipe read against
  `raw.githubusercontent.com` builds its headers without it (a helper in `github.py`,
  `headers_for(locator, *, declared)`, strips `Authorization` for any host other than the
  API host). `source_release`'s tag fallback is the API host and keeps it.
- `core/collection.py`: when the one fetch fails with `TransportError` and
  `status_code == 401`, the run's `detail` and the warning event read
  `"the declared credential was refused by <host>: <original message>"` -- the host and
  status, never a header; every other status is as today. A refused credential is never
  retried (401 is not in `DEFAULT_RETRY_STATUSES`).
- Hygiene, pinned by tests: no log line, `CollectionRun.detail`, evidence `detail`, or
  `TransportError` message contains the token or its first eight characters, on the
  success, 401, 403, 404 and transport-failure paths of both collectors, driven through
  `ScriptedTransport` with the token set; `sent_headers` proves the API host received the
  bearer and the raw host did not.
- Rate-limit keys name the collector and window only; a token appearing or disappearing
  mid-window judges the rest of that window under the new allowance -- documented, not
  changed.
- Docs: `operations.md` settings table (+1 row: how to obtain a fine-grained PAT with no
  scopes or a GitHub App installation token, where to set it, that it is never logged),
  the two "reads GitHub unauthenticated" sections rewritten around with/without a token
  with the `CPM-NFR-1` arithmetic (authenticated: 1,250 collections an hour, a 10,000-package
  inventory in 8 h against a 2-day target; feedstock 450 an hour, ~22 h against 14 days;
  unauthenticated: ~28 days and ~67 h -- the feedstock shortfall the old prose overstated);
  the local-spend caveat (the uncharged second call, and GitHub's separate core and search
  pools). `running-it.md` one paragraph on setting it locally.
- Tests: `test_settings.py` (empty default in every module; the manifest carries it
  nowhere); unit for `github.py` (every fault, header assembly, host stripping); the two
  collectors' declaration tests extended with a with-token variant (`override_settings`);
  integration through `ScriptedTransport` for the hygiene matrix; the base's 401 wording.
- Sprint status: `cpm-operate-s05-an-authenticated-github-allowance: done`.

**Block If:** the base needs to know which collector declared a credential (it does not --
401 wording is generic); a second credential kind (App JWT exchange) is asked for.

**Never:** the token in a URL, a query string, a ledger row, evidence, a log, an exception
message, a test literal resembling a real `ghp_`/`github_pat_` value, or a pixi/compose
file; a retry on 401; a change to `DEFAULT_RETRY_STATUSES`.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|---|---|---|---|
| No token | setting empty | headers and allowances exactly as today; no `Authorization` sent | N/A |
| Token, API | token set; `source_release` fetch | `Authorization: Bearer <token>` on the releases and tags calls; allowance 5000/h | N/A |
| Token, raw host | token set; `feedstock` recipe read | no `Authorization` on `raw.githubusercontent.com`; present on `api.github.com` | N/A |
| Refused | API answers 401 | run `failed`; detail "the declared credential was refused by api.github.com: …"; no header in detail or log; no retry | `TransportError` as today |
| Quota | API answers 403 | as today (a quota refusal looks like any other 403) | N/A |
| Malformed | token with a newline / a space / non-ASCII | boot refused naming `CPM_GITHUB_TOKEN`, value never echoed | `ImproperlyConfigured` |
| Hygiene | any path with the token set | token and its first eight characters absent from every log line, `detail`, evidence row | test greps captured logs and rows |
| Mid-window change | token appears mid-window | the counter continues under the new allowance until the window turns | documented |

</intent-contract>

## Code Map

- `collectors/source_release.py:197-284` cadence, retries, timeout, `SOURCE_RELEASE_RATE_LIMIT` :266
  (docblock :256-265 to rewrite), `SOURCE_RELEASE_HEADERS` :278; :299 `GITHUB_API_HOST`; class :1053-1077
  (`rate_limit` :1063, `headers` :1065); tag fallback :1194-1241 (`request_headers(declared=self._headers, entry=None)` :1227).
- `collectors/feedstock.py:198-263` (`FEEDSTOCK_RATE_LIMIT` :246, docblock :234-245, headers :257); :275 `GITHUB_API_HOST`,
  :276 `GITHUB_RAW_HOST`; class :1352-1377; second calls :1742-1773 (recipe, raw host) and :1807-1836 (conventional, API host).
- `collectors/agent.py:80` `USER_AGENT` -- the one shared collector module today; `github.py` sits beside it.
- `core/collection.py:847-906` `__init__` (`self.rate_limit`/`self.headers` are attribute lookups, so instance
  attributes set before `super().__init__()` are honoured); :620-649 `request_headers`; :944 `request_cost`;
  :1265-1274 limiter charge and refusal detail; :1294-1303 the `TransportError` catch (`detail = f"{type}: {failure}"`)
  -- the 401 wording goes here; :2665-2741 `_require_headers` (CR/LF refusal).
- `core/transport.py:203` `DEFAULT_RETRY_STATUSES`, :353 `TransportError(message, *, source, status_code)`,
  :664-670 non-OK raise (URL + status, no headers).
- `core/rate_limit.py:210-245` `window_key` (collector + window; not the limit).
- Boot: `collectors/apps.py:264-288` the `CPM_MONITORED_*` refusal shape.
- Settings: `base.py:404,419` the S03/S04 reads; `tests/unit/test_settings.py:1550-1600` the pins to copy;
  `pixi.toml:443-455` activation env (must not gain the token).
- Precedents for "never the credential": `config/authorization/authentication.py:326-345`,
  `config/startup/stage_one.py:643` `_redacted`, `tests/unit/startup/test_stage_one_conditions.py:627`.
- Tests: `tests/unit/django_apps/test_source_release.py:302-333,428-454,506-521`; `test_feedstock.py:285-314,367-406`;
  integration `test_source_release.py:299,342,595`, `test_feedstock.py:419,1133,1213,1241`;
  `tests/collectors.py:382-498` `sent_headers`; `tests/integration/django_apps/test_collection.py:1338` (403 case).
- Docs: `operations.md:101-161` (upstream section), :217-260 (feedstock), :1298-1306 (settings table).
- Read-only: `core/transport.py`, `core/rate_limit.py`, every other collector.

## Tasks & Acceptance

**Execution:**
- [x] `collectors/github.py` -- new.
- [x] `base.py` -- the setting; `collectors/apps.py` -- boot refusal.
- [x] `collectors/source_release.py`, `collectors/feedstock.py` -- `__init__` overrides, host-aware headers on the second calls, docblocks.
- [x] `core/collection.py` -- the 401 wording.
- [x] Tests listed in Boundaries.
- [x] Docs listed in Boundaries.
- [x] `sprint-status.yaml` -- story `done`.

**Acceptance Criteria:**
- Given `CPM_GITHUB_TOKEN` exported and the stack up, when `pixi run stack-run dispatch_sweep source_release`
  runs, then the collections proceed past fifteen an hour and no ledger row, evidence row or
  worker log line contains the token.
- Given a deliberately wrong token, when a `source_release` collection runs, then its run is
  `failed` with a detail beginning "the declared credential was refused by api.github.com".
- Given no token, when the suite runs, then every pre-existing declaration test passes unchanged.
- Given `pixi run ci`, when it runs, then it exits 0.

## Spec Change Log

## Design Notes

**Why instance attributes and not a settings read per call.** The base validates headers
and allowance once at construction; reading the token then keeps that contract, keeps the
ClassVars as the honest unauthenticated declaration every audit reads, and means a token
rotated in the environment is picked up by the next task (each task constructs a fresh
collector).

**Why the token stays off the raw host.** GitHub does not count `raw.githubusercontent.com`
against the API allowance and does not need the credential there; a credential should
reach exactly the host it is for.

## Verification

**Commands:**
- `pixi run test`; `pixi run -e dev python -m pytest tests/integration/django_apps/test_source_release.py tests/integration/django_apps/test_feedstock.py tests/integration/django_apps/test_collection.py -q` -- green.
- With a real fine-grained PAT exported: `pixi run docker-up` then `pixi run stack-run shell -c '...'` running one `SourceReleaseCollector().collect(package_id=<django>)` inline and grepping the run's detail and the captured log for the token -- absent; with `CPM_GITHUB_TOKEN=wrong`, the refused wording.
- `pixi run ci` (stack down) -- exit 0.

## Auto Run Result

Status: done
Review: three layers; the verification-gap layer found the hygiene matrix pinned through the
real seam; seventeen patches from the three. The ones that mattered: structlog's traceback
renderer printed frame locals, so any exception through a frame holding the token would
have written it to the JSON log (now `show_locals=False`, with a control test); a 401 was
worded as a refused credential even for collectors that sent none; "rotate without a
restart" was false; nothing kept a developer's real token out of the suite; and a wrong
token would have let a sweep burn a thousand failed runs an hour (now a per-window refusal
memo that fails fast). The credential-host gate moved into the base so no `source_for` can
leak the bearer to another host. Left for the product owner: the with-a-real-token live
check, which needs a personal access token this session does not hold.

## Suggested Review Order

**Where the credential may and may not go**

- Entry point: the frame-locals leak closed at the renderer
  [`logging.py:137`](../../../src/config/observability/logging.py#L137)

- One rule for which locator gets the bearer: `https`, the declared host, nothing else
  [`credentials.py:101`](../../../src/django_apps/conda_sentinel/core/credentials.py#L101)

- The base applies it at the one place it composes headers
  [`collection.py:1757`](../../../src/django_apps/conda_sentinel/core/collection.py#L1757)

- A 401 is a refused credential only if one was sent
  [`collection.py:680`](../../../src/django_apps/conda_sentinel/core/collection.py#L680)

- Refused once, fail fast for the rest of the window
  [`rate_limit.py:379`](../../../src/django_apps/conda_sentinel/core/rate_limit.py#L379)

**The token itself**

- What is refused at boot, naming the setting and never the value
  [`github.py:159`](../../../src/django_apps/conda_sentinel/collectors/github.py#L159)

- Read once per process; a rotation is a restart
  [`github.py:229`](../../../src/django_apps/conda_sentinel/collectors/github.py#L229)

- The core allowance minus feedstock's share of the same pool
  [`github.py:147`](../../../src/django_apps/conda_sentinel/collectors/github.py#L147)

**Tests**

- The renderer control: stock chain leaks, configured chain does not
  [`test_observability_logging.py:182`](../../../tests/unit/test_observability_logging.py#L182)

- The hygiene matrix, through the real transport seam and every logger
  [`test_source_release.py:1123`](../../../tests/integration/django_apps/test_source_release.py#L1123)
