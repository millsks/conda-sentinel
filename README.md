# Conda-Sentinel

[![CI](https://github.com/millsks/conda-sentinel/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/millsks/conda-sentinel/actions/workflows/ci.yml)
[![Quality Gate Status](https://sonarcloud.io/api/project_badges/measure?project=millsks_conda-sentinel&metric=alert_status)](https://sonarcloud.io/summary/new_code?id=millsks_conda-sentinel)
[![Coverage](https://sonarcloud.io/api/project_badges/measure?project=millsks_conda-sentinel&metric=coverage)](https://sonarcloud.io/summary/new_code?id=millsks_conda-sentinel)
[![Python](https://img.shields.io/badge/python-3.14-blue.svg)](https://www.python.org/)
[![Django](https://img.shields.io/badge/django-5.2%20LTS-092E20.svg)](https://www.djangoproject.com/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

An evidence-driven platform for monitoring the health, security, compliance, and maintenance status of packages distributed through the conda ecosystem.

The architecture is designed for inventories that include Python packages as well as native libraries, compilers, system libraries, Rust packages, R packages, and other artifacts available through conda-forge. **v1 targets the Python subset of that inventory**; the wider ecosystem is a stated later phase, not an exclusion.

## Why This Project Exists

Package inventories change continuously. A package may be current upstream but lagging on PyPI, conda-forge, or an internal channel. It may also have known vulnerabilities, a license-policy issue, incomplete Python 3.14 support, a missing feedstock, or an inactive source repository.

Conda-Sentinel collects evidence from these sources, evaluates it using deterministic and versioned policies, and produces an explainable package-health and remediation view.

## Capabilities

- Maintain a canonical package inventory and identity mapping.
- Map packages across conda, conda-forge, PyPI, source repositories, package URLs, CPEs, and feedstocks.
- Compare versions across upstream sources, PyPI where applicable, conda-forge recipes, and published conda packages.
- Detect and track CVEs and Known Exploited Vulnerabilities (KEVs).
- Evaluate package and recipe licenses against a configurable policy.
- Assess Python 3.14 readiness where Python compatibility is relevant.
- Determine whether a conda-forge feedstock exists and whether it appears to be maintained.
- Preserve timestamped evidence instead of overwriting historical observations.
- Calculate deterministic priority, ranking, score, and recommended work items.
- Provide a review queue for unmapped, ambiguous, stale, failed, or low-confidence records.
- Support natural-language investigation and reporting through governed AI workflows.

## Project Scope

The initial inventory may contain approximately 10,000 packages.

**v1 targets the Python subset**, where PyPI identity and Python version readiness both
apply and every check in the policy set is meaningful. Non-Python conda artifacts --
native libraries, compilers, system libraries, Rust packages, R packages -- are a stated
later phase. The architecture is intentionally broader than Python throughout, so that
phase is a data change rather than a schema migration.

Version currency, Python compatibility, and some metadata checks are package-type
dependent. The system must represent `not_applicable`, `unknown`, `not_found`, and
`error` separately from a successful clean result -- five distinct states, modeled from
v1 so the later phase exercises an existing path instead of introducing one.

## Requirements

[pixi](https://pixi.sh) is the only prerequisite. It provisions Python 3.14 and
every dependency from conda-forge; nothing is installed with `pip`.

PostgreSQL is the target database. The suite falls back to a sqlite substitution
when `DATABASE_URL` is unset, which is enough for unit tests but does not
exercise everything the PostgreSQL gate does.

## Quick start

```sh
pixi install         # runtime environment
pixi install -e dev  # development toolchain
pixi run bootstrap   # install the git hooks
pixi run migrate     # apply migrations (sqlite by default)
pixi run runserver   # http://127.0.0.1:8000/ -- one process, tasks run inline
```

## Development

```sh
pixi run test              # unit tests (fast, no database or network)
pixi run test-integration  # integration tests
pixi run test-cov          # full suite, 90% coverage floor
pixi run ci                # the full gate -- must pass before any change is done
pixi run docs-serve        # documentation with live reload

# The product the way it actually runs: Redis and PostgreSQL in containers, plus
# gunicorn, a worker, beat and the Celery monitor together. Its containers publish
# on 6380 and 5433, so they coexist with anything you already have running.
pixi run local-stack       # the whole stack, under honcho
pixi run docker-up         # just the infrastructure
pixi run docker-down       # stop it, keeping the data
pixi run local-stack-down  # after honcho died without Ctrl-C: free 8000 and 5555
pixi run stack-shell       # a Django shell against the stack, not the SQLite file
pixi run stack-run <cmd>   # any management command against the stack

# Operating it: the three admin processes a deployment schedules, each enqueueing
# an existing task. Through stack-run they land on the stack's worker; through
# bare `pixi run <task>` in the default local environment they run inline against
# the SQLite file, because that environment makes every task eager.
pixi run stack-run ingest_inventory     # re-read the watchlist   (deployed: pixi run ingest)
pixi run stack-run dispatch_sweep --all # sweep every collector   (deployed: pixi run sweep)
pixi run stack-run run_policy           # compute the verdicts    (deployed: pixi run policy-run)
```

`pixi run ci` is the gate, and it is the same sequence locally and in CI:
`precommit` -> `build` -> `typecheck` -> `lint` -> `test-cov`, fast-fail-first.
`.github/workflows/ci.yml` invokes exactly that one task and nothing else, so a
green local run and a green pipeline mean the same thing.

Dependencies live in `pixi.toml` and come from conda-forge; `pyproject.toml`
holds build metadata and tool configuration only. The `default` environment
carries runtime dependencies only; `dev` layers the toolchain on top. Tasks
resolve to whichever environment defines them, so `-e` is rarely needed.

See `docs/accelerator/development.md` for database configuration and the full task list.

## Architecture Overview

The system is organized into five primary layers:

1. **Inventory and identity layer** — stores stable package identity and mapping information.
2. **Evidence collection layer** — independently collects observations from external and internal sources.
3. **Policy and scoring layer** — evaluates evidence using deterministic, versioned rules.
4. **Reporting and query layer** — exposes current health views, reports, and controlled query tools.
5. **AI orchestration layer** — uses LangChain and LangFlow to investigate and explain evidence without becoming the source of truth.

```text
Package inventory
        |
        v
Identity resolution
        |
        +-------------------+-------------------+-------------------+
        v                   v                   v                   v
     Source              PyPI              conda-forge          Security /
   collectors          collector            collectors          compliance
        |                   |                   |                   |
        +-------------------+-------------------+-------------------+
                            v
                    Append-only evidence store
                            |
                            v
                 Policy and scoring engine
                            |
                            v
                 Current package-health view
                            |
              +-------------+--------------+
              v                            v
        Operational reports          AI query layer
                                     LangChain / LangFlow
```

## Core Design Principles

### Identity and evidence are separate

The canonical inventory is a relatively stable identity snapshot. Releases, vulnerabilities, licenses, feedstock activity, build results, and compatibility assessments are volatile observations and belong in timestamped evidence records.

### Deterministic policies are the source of truth

Security status, license results, version currency, Python 3.14 readiness, priority buckets, scores, ranks, and work types are calculated by versioned code or SQL policies. An LLM must not independently decide whether a package is compliant, vulnerable, current, or high priority.

### Unknown is not clean

A failed lookup, missing mapping, stale observation, or unavailable source must remain visible. The system must not convert incomplete evidence into a clean result.

### Evidence is auditable

Every finding should retain its source, observation timestamp, collection run, package identity, and confidence. Reports and AI-generated explanations should be traceable to the evidence used.

### Collectors are independent

Source, PyPI, conda-forge, vulnerability, KEV, license, and Python compatibility collectors should be separately schedulable, retryable, rate-limited, and observable. A failure in one source should not prevent unrelated collectors from running.

## Evidence Domains

The initial monitoring model includes the following evidence domains:

| Domain | Example observations |
|---|---|
| Source releases | Latest upstream version, release date, repository activity, lookup status |
| PyPI | Project existence, latest version, release date, `Requires-Python` |
| Conda-forge feedstock | Feedstock existence, recipe version, recipe activity, build/test state |
| Published conda packages | Channel, package version, build string, publication status |
| Vulnerabilities | CVE/advisory identifier, severity, affected range, fixed range, matched version |
| KEV | KEV catalog membership and date added |
| Licenses | Raw license, normalized SPDX expression, detection method, policy result |
| Python 3.14 | Metadata assessment, build verification, import verification, test result |
| Collection operations | Collector status, errors, retries, start and finish timestamps |

## Canonical Inventory

The inventory identity layer is keyed by `Core_Python_Package_Name`, based on the existing project identity schema. It includes fields for package display information, priority, ranking, internal usage, vulnerability rollups, package URLs, source repositories, feedstocks, staged recipes, local recipes, build status, and issue-tracking links.

Important identity and provenance fields include:

- `Core_Python_Package_Name`
- `Package`
- `primary_purl`
- `primary_type`
- `alternative_purls`
- `cpes`
- `conda_purl`
- `source_repository_url`
- `Conda-Forge_FeedStock_URL`
- `Conda-Forge_Metadata_URL`
- `Staged_Recipes_PR_URL`
- `identity_source`
- `associator_key`
- `associator_status`
- `Verification_Timestamp_UTC`

Derived workflow fields include:

- `P`
- `Rank`
- `Score`
- `Work`
- `Vuln`
- `JFROG_risk_level`
- `JFROG_latest_vuln_count`
- `Priority_Bucket_Description`
- `Priority_Source`
- `Priority_Reason`

The inventory is a reporting and identity contract. It is not the sole storage location for historical monitoring results.

## Policy and Scoring

The policy engine should be deterministic, versioned, and re-runnable against historical evidence.

Policy areas include:

- Version currency and source-to-feedstock lag.
- Vulnerability and KEV rollups.
- License allow, restrict, deny, and manual-review decisions.
- Python 3.14 readiness.
- Feedstock existence and maintenance status.
- Evidence freshness and collector health.
- Priority bucket assignment from top-down rules.
- Usage-weighted score and rank calculation.
- Recommended `Work` type.

Example `Work` values include:

- `Fix vulnerability`
- `Create recipe`
- `Update feedstock`
- `Validate Python 3.14`
- `Review license`
- `Resolve identity`
- `File tracking issue`
- `Already tracked`

AI may explain a policy outcome, summarize evidence, or draft a tracking issue. It must not replace the policy engine.

## LangChain and LangFlow

> **Status:** design only. Neither LangChain nor LangFlow is a dependency of this
> repository yet, and adoption is gated by a fitness spike (`CPM-AD-17`).
>
> **DB-GPT was evaluated and rejected.** It ships no Python 3.14 classifier and tests
> only 3.10 and 3.11 in CI, while this repository is pinned to `python = "3.14.*"`; its
> `sqlalchemy`/`fastapi` pins are mutually exclusive with LangFlow's; and its
> conda-forge availability is unestablished against the conda-forge-only supply-chain
> rule that `tests/unit/test_dependency_policy.py` enforces. Natural-language analytics
> is served instead by LangChain tools over the governed views.

### LangChain

LangChain provides the controlled application and tool layer for package investigations. Tools should invoke approved queries and return structured results rather than allowing an agent to invent facts.

Planned tools include:

- `get_package_health`
- `compare_versions`
- `get_vulnerability_findings`
- `get_license_policy_result`
- `list_feedstock_gaps`
- `create_remediation_ticket_draft`

Each tool should return the package identifier, current status, relevant evidence identifiers, source names, observation timestamps, and confidence or freshness information where available.

### LangFlow

LangFlow is used to compose repeatable workflows from collectors, policy outputs, database tools, and reporting components. Candidate flows include:

- Single-package investigation.
- Daily KEV report.
- Weekly feedstock-lag report.
- Python 3.14 readiness report.
- License exception report.
- Unmapped-identity review report.
- Stale-evidence and collector-failure report.

LangFlow flows should orchestrate existing deterministic services and LangChain tools. They should not embed undocumented business rules in prompts.

### Governed natural-language analytics

Natural-language querying runs as LangChain tools over the governed views, using a
read-only database role on a separate connection. There is no separate analytics product.

Required controls include:

- Read-only credentials.
- Approved reporting views instead of unrestricted raw-table access.
- Query timeouts and row limits.
- SQL and request audit logging.
- Restricted access to sensitive internal usage fields.
- Evidence citations in generated answers.
- Explicit handling of missing, stale, failed, and not-applicable states.

This is an analytics and explanation interface. Deterministic policy results remain authoritative.

## Observability

Structured logging (structlog) and distributed tracing (OpenTelemetry) are built
in, not optional. Every log line carries `request_id`, `user_id` and `trace_id`,
and requests, Celery tasks, queries and cache calls are traced. Spans export over
OTLP when `OTEL_EXPORTER_OTLP_ENDPOINT` is set, and are dropped when it is not --
so nothing retries against a collector that isn't there.

This matters for the evidence model: a collection run that fails partway is
traceable to the request and task that performed it, which is what makes an
`error` or `unknown` state auditable rather than merely recorded.
See `docs/accelerator/observability.md`.

## Data and Security Considerations

- Do not send sensitive internal usage data to an external model unless explicitly approved.
- Prefer private or self-hosted model deployment where organizational requirements demand it.
- Keep the AI-facing database role read-only.
- Record the evidence rows used to produce reports and explanations.
- Apply rate limits, caching, request timeouts, and retry/backoff to external collectors.
- Preserve source attribution and collection errors.
- Treat manually curated identity overrides as governed data with provenance.

## Technology Stack

### In place today

Pinned in `pixi.toml` and exercised by the gate:

- **Python 3.14** and **Django 5.2 LTS** (supported to April 2028; the pin is
  `>=5.2,<5.3` so a feature release cannot drift in).
- **PostgreSQL** via `psycopg` 3, with `libpq` held at 17 to match the server the
  gate runs against. A sqlite substitution covers the non-Linux test legs.
- **pixi** for environment and dependency management -- conda-forge is the single
  source for third-party packages, and `[pypi-dependencies]` holds only this
  project's own editable install.
- **Celery** with **django-celery-beat** for background collection and scheduled
  policy evaluation, and **Redis** as broker and cache.
- **Django REST Framework** with **drf-spectacular** for the API surface.
- **django-allauth** for authentication, including OIDC.
- **structlog** / **django-structlog** and **OpenTelemetry** for correlated logs
  and traces.
- **uvicorn** (ASGI) and **whitenoise** for serving.
- **django-crispy-forms** with **crispy-bootstrap5** for form rendering.

### Planned, not yet present

Named in the design but not yet dependencies of this repository:

- **htmx** and **Alpine.js** for dynamic HTML interactions and lightweight
  client-side reactivity.
- **LangChain** for the AI orchestration layer described below, and **LangFlow** as a
  separate deployment unit rather than a dependency.

The platform foundation was imported from
[django-15-factor-base](https://github.com/millsks/django-15-factor-base), an
accelerator built on 15-factor application principles -- environment-based
configuration, secure defaults, split settings for local/test/production, a
two-stage startup check, and health and drain endpoints.

## Repository Structure

```text
.
|-- manage.py                  # Django management entry point
|-- pixi.toml                  # environments, dependencies, tasks (the gate)
|-- pyproject.toml             # build metadata and tool configuration only
|-- component.toml             # deployment contract (databases, release steps)
|-- Dockerfile                 # container image
|-- mkdocs.yml                 # documentation site
|-- src/                       # import root -- deliberately NOT a package
|   |-- config/                # settings, urls, asgi/wsgi, celery, startup
|   |   |-- settings/          # base / local / test / production
|   |   |-- authorization/     # OIDC: JWKS, claims, mapper, adapters
|   |   |-- health/            # health and drain endpoints
|   |   |-- observability/     # structlog and OpenTelemetry wiring
|   |   |-- startup/           # two-stage boot checks
|   |   `-- local_dev/         # personas and token minting for local work
|   |-- django_service/        # the application package
|   |   |-- users/             # users app, provisioning, API, commands
|   |   |-- templates/
|   |   `-- static/
|   `-- django_apps/           # second import root -- deliberately NOT a package
|       `-- conda_sentinel/    # every domain application
|           |-- core/          # shared base models and utilities
|           |-- identity/      # planned -- see below
|           |-- collectors/
|           |-- evidence/
|           |-- policies/
|           |-- reporting/
|           `-- ai_integration/
|-- tests/
|   |-- unit/                  # no database, network, or filesystem
|   |-- integration/           # marked `integration`
|   `-- spikes/                # dependency fitness spikes
|-- docs/                      # mkdocs sources
`-- _bmad-output/
    `-- planning-artifacts/    # brief, PRD, architecture
```

`src/` and `src/django_apps/` are both import roots and neither is a package, so
`config`, `django_service` and `conda_sentinel` import as top-level names
while `django_apps` itself never appears in an import statement.
Both are declared in exactly one place -- the
`[tool.hatch.build.targets.wheel]` table in `pyproject.toml`, whose `sources`
mapping enumerates the three subtrees so that no key is a prefix of another.
`dev-mode-exact` makes the editable install a redirecting finder over exactly
those top-level names rather than a list of directories on `sys.path`; no
entrypoint, pixi task or test setting declares a root a second time.

### Planned domain applications

The evidence pipeline is largely unbuilt. `core/` exists; the rest land under
`src/django_apps/conda_sentinel/` -- one top-level package holding one pluggable
Django application per business domain, so every application shares a single
stable top-level name. It is not the distribution, which is `conda-sentinel` and
ships `config` and `django_service` alongside it; the two spellings coincide
because the product has one name, not because the package and the distribution
are the same thing:

- **identity/** -- package identity resolution, canonical inventory management, and mapping overrides.
- **collectors/** -- evidence collection from source repositories, PyPI, conda-forge, vulnerability sources, and other external systems.
- **evidence/** -- append-only evidence storage models, timestamped observations, and retrieval interfaces.
- **policies/** -- deterministic policy evaluation, scoring, priority assignment, and work-type recommendation.
- **reporting/** -- operational reports, views, dashboards, and export functionality.
- **ai_integration/** -- LangChain tools and governed AI query interfaces.
- **core/** -- shared utilities, base models, common middleware, and cross-cutting concerns. *(exists)*

Two further trees are planned alongside them:

- **flows/langflow/** -- LangFlow workflow definitions for orchestrating investigation, reporting, and operational workflows.
- **scripts/schedulers/** -- deployment scripts, scheduler integration configurations, and operational automation for Celery, cron, or other approved schedulers.

None of these directories exists yet. They land when they are built, not before.

## Identifiers: two vocabularies, one repository

Requirement and decision identifiers in this repository come from **two unrelated
sources**, and they collide across nearly their whole range. Read the prefix before you
resolve a reference.

| Form | Owner | Where it is defined |
|---|---|---|
| Bare `AD-1`–`AD-31`, `FR-4`–`FR-44`, `NFR-1`–`NFR-7`, `Epic 2`–`Epic 9`, `Story x.y`, `CG-3`, `R-2`/`R-3`/`R-5`, `SC-6` | The imported `django-15-factor-base` platform | Referenced in comments across 45 files under `src/` |
| `CPM-` prefixed: `CPM-FR-n`, `CPM-NFR-n`, `CPM-AD-n`, `CPM-SM-n`, `CPM-UJ-n`, `CPM-EP-*` | This product | `_bmad-output/planning-artifacts/` |

The two never overlap in meaning. A bare `FR-17` in `src/config/startup/allowlist.py` is
the platform's **authentication-surface allowlist**; `CPM-FR-17` in the PRD is this
product's **vulnerability rollup policy**. They share a number and nothing else.

Rules: never renumber or reuse a platform identifier — they are inherited and read-only.
Never write a product identifier without its `CPM-` prefix. When a planning document
cites a platform rule, it cites it bare and by its original id.

The planning artifacts themselves live in `_bmad-output/planning-artifacts/`:

- `briefs/` — the product brief and its addendum (problem, roles, scope boundary, non-goals)
- `prds/` — the PRD (`CPM-FR-*`, `CPM-NFR-*`, epics)
- `architecture/` — `ARCHITECTURE-SPINE.md` (the 24 `CPM-AD-*` invariants a build must
  obey), `solution-design.md` (the reasoning behind them, with C4 views), and
  `evidence-spine.html` (an interactive walkthrough of both)

Each workspace also carries a `.memlog.md` — the append-only decision trail, including
which earlier decisions were overridden and why.

## Initial Delivery Plan

### Phase 1 — Identity and inventory foundation

- Import the existing package inventory.
- Establish the canonical identity model.
- Add identity provenance and confidence.
- Implement manual mapping overrides.
- Create an unmapped and ambiguous identity review queue.

### Phase 2 — Version and feedstock monitoring

- Implement source, PyPI, feedstock, and published-conda collectors.
- Store append-only release and feedstock snapshots.
- Implement version-currency policies.
- Produce the current package-health view.

### Phase 3 — Security and compliance

- Add vulnerability and KEV evidence collection.
- Add license extraction, normalization, and policy evaluation.
- Implement vulnerability and license rollups.
- Add daily and weekly operational reports.

### Phase 4 — Python 3.14 readiness

- Implement metadata-based classification.
- Add optional build, import, and test verification.
- Track platform and architecture-specific results.
- Separate inferred compatibility from verified compatibility.

### Phase 5 — AI-assisted analytics

- Implement LangChain tools over governed views.
- Compose LangFlow investigation and reporting workflows.
- Serve read-only natural-language analytics through LangChain tools over governed views.
- Require evidence references in generated explanations.

## Current Non-Goals

The initial version does not aim to:

- Automatically upgrade packages.
- Automatically create or merge feedstock pull requests.
- Replace an existing issue tracker.
- Build a general-purpose package manager or Conda channel.
- Support every package ecosystem outside the conda ecosystem.
- Treat an LLM-generated answer as authoritative without underlying evidence.

## Open Questions

Before implementation is finalized, the project should confirm:

- Which vulnerability and KEV data sources are available and approved?
- Which license policy should be used as the initial organizational baseline?
- Where do internal platform, application, download, component, and LOB counts originate?
- Which conda channels and platforms are in scope?
- Which source ecosystems are authoritative for version currency on a package-by-package basis?
- Is private or self-hosted model deployment required for the natural-language layer?
- Which scheduler, database, deployment target, and authentication standards are required?

## Contributing

Contributions should preserve the project's core principles:

1. Keep identity data separate from historical evidence.
2. Make policy decisions deterministic and testable.
3. Preserve unknown, stale, failed, and not-applicable states.
4. Include provenance for external observations.
5. Prevent LLM-facing components from writing directly to operational data.
6. Add tests for collector behavior, policy rules, identity resolution, and evidence rollups.

Mechanically:

- Branch from `main` as `feature/`, `bugfix/`, or `hotfix/`; open a pull request rather than pushing to `main`.
- Commit messages follow [Conventional Commits](https://www.conventionalcommits.org/) -- the changelog is generated from them.
- `pixi run ci` must exit 0 before a change is done. Never use `--no-verify`.
- If a pre-commit hook auto-fixes a file, re-stage it before re-running; the fix is left unstaged.

## License

This project is licensed under the MIT License. See the `LICENSE` file for the full license text.
