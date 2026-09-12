# Epic platform Context: The service platform

<!-- Compiled from planning artifacts. Edit freely. Regenerate with compile-epic-context if planning docs change. -->

## Goal

The Django service platform — settings and two-stage startup gates, OIDC authentication with group-claim sync, structured logging and tracing, health and drain probes, Celery, and the `component.toml` deployment contract — is imported from the accelerator and already running. This epic is the set of gaps between what the accelerator provides and what this product needs: a second import root for domain applications, the three product role groups, and a local development stack (containers, process supervision, personas, demo seeder, sign-in) that runs the product the way it actually runs, seeds the database it serves, and never puts anything into the evidence log that the product did not observe. It covers CPM-FR-28–30 and CPM-FR-39 (inherited, largely complete) and CPM-NFR-7, -10, -12, -13.

## Stories

- Story CPM-PLATFORM-S01: A second import root for domain applications
- Story CPM-PLATFORM-S02: Group claims resolve to the three product roles
- Story CPM-PLATFORM-S03: One command brings up the whole local stack
- Story CPM-PLATFORM-S04: One local persona reaches every screen
- Story CPM-PLATFORM-S05: A hundred packages, and real advisories behind them
- Story CPM-PLATFORM-S06: The local stack seeds the database it runs against
- Story CPM-PLATFORM-S07: A local run sends a sign-in somewhere that exists
- Story CPM-PLATFORM-S08: The demo asserts nothing it never observed

## Requirements & Constraints

- **Two import roots, two identifier namespaces.** The accelerator owns `src/config/` and the bare `AD-n` / `FR-n` identifiers; they are inherited, read-only, never renumbered. The product owns `src/django_apps/` (declared once in `pyproject.toml`) and the `CPM-AD-n` / `CPM-FR-n` namespace. A bare `FR-17` and `CPM-FR-17` share a number and nothing else.
- **Dependency direction is one-way (inherited AD-4).** `config` may import `django_service`; nothing under `django_service` or `django_apps` reaches back into `config`. This is why local-dev code (personas, seeders, sign-in) lives in `config.local_dev` and runs as `python -m` entry points rather than management commands.
- **Adoption is explicit and two-line (inherited AD-8).** Every domain app is a `pixi.toml` dependency plus an `adopted_apps` entry in `component.toml`, in that order; nothing self-registers; no entry-point discovery. Runtime and process rules live only in `component.toml` (AD-28).
- **Locality fails closed.** `COMPONENT_RUNTIME=local` is declared once in the `dev` feature's activation env; absent or unrecognized reads *deployed*. Anything that writes local fixtures refuses outside a local run — a deployed component running a seeder would leave permanent, replayable, fictional observations in a log nothing may update or delete (CPM-AD-2, CPM-AD-29). Every refusal raises `ImproperlyConfigured` (inherited CG-3).
- **Roles.** Three product roles, provisioned by migration, named from the environment with no baked-in default; group-claim sync is inherited (AD-10), so the product never re-implements resolution (CPM-FR-29, CPM-FR-30). An authentication with no group claim is refused, distinguishably from one asserting zero groups. Revocation is honoured at token expiry (R-2). Local personas are rows in a sign-in fixture, never a fourth role; exactly one holds all three roles plus admin, and an audit pins that count.
- **The demo seeder writes evidence, never verdicts.** It resolves shells through the identity service, appends evidence, then runs the real policy pass at the shipped version, so every status on screen was concluded by the pass that owns it (CPM-AD-2, CPM-AD-8, CPM-AD-10). Its runs are filed under `local-dev-demo-seed`, deliberately not a registered collector name. Evidence is append-only, so seeding twice appends — which is why seeding is a separate command and never part of stack start-up.
- **The advisories are real and everything around them is a fixture.** Identifier, severity, affected range and fixed version come from OSV.dev; a KEV row is a genuine CISA entry with its catalogue date; a reviewer who looks one up must find it. The roster's first three columns are positional (name, upstream, installed) and an audit compares every parsable pair, because a swap inverts a currency verdict silently. Never invent an advisory id.
- **Identity constraints the seeder must respect (S08).** Package identity is mutated only by resolution or the audited override — one write path (CPM-AD-14); a collector or seeder never touches the package table, it calls `record_resolution` (CPM-AD-25). A mapping that cannot be established is recorded `unmapped`, never a guess (CPM-FR-1). Confidence is a value written per package — `verified`, `inventory-derived`, `unmapped` — and gates what may be asserted; `inventory-derived` labels a determinate value rather than degrading it, while `unmapped` writes every gated status `unknown` (ADR-004, CPM-AD-4, CPM-FR-5). Every derived status uses the one five-state vocabulary with `not_applicable` distinct from `unknown`, `not_found` and `error` (CPM-AD-5, CPM-FR-6). A fictional source URL or invented feedstock at `verified` confidence violates all of these at once; the remedy is to seed identity the product can stand behind and let the `resolve_identity` collector establish the rest.

## Technical Decisions

- The compose file brings up **infrastructure only** (Redis on 6380, PostgreSQL on 5433 — ports a developer's own services do not commonly hold). The application runs from the pixi environment under honcho; the web process is the deployed gunicorn line, not `runserver`, at the cost of no autoreload and Unix-only. A database broker was considered and rejected.
- Locally Celery defaults to eager; the stack sets it off so the request/task boundary (CPM-AD-9) and the three queues (CPM-AD-20) are visible. `stack-seed` runs eager so a seed completes inline in one process.
- Every `stack-*` task carries the stack's own `DATABASE_URL`/`REDIS_URL`; `local-stack` migrates via `depends-on` but never seeds. Four copies of one URL is the shape the original defect had, so a test reconciles them and fails when a later task names another database.
- With no identity provider configured, `LOGIN_URL` names the local sign-in page, reversed from its URL name; the provider block is left alone (inherited AD-23 — the issuer stays the single trust anchor).
- Two shipped priority rules are unreachable (`p6` by construction, `p4`/`p7` because `license_rules` is empty by PRD open question). Changing one is a new policy version, not a fixture side effect.

## Cross-Story Dependencies

- S01 is the structural prerequisite for every later epic; S02 is required before any role-scoped surface can be checked.
- S04 personas and S05's roster are what S06's `stack-seed` writes; S07 is what makes the seeded stack reachable on first click.
- S08 depends on CPM-IDENTITY-S08's `resolve_identity` collector, which `stack-seed` runs inline after seeding so a fresh seed carries observed mappings rather than invented ones.
