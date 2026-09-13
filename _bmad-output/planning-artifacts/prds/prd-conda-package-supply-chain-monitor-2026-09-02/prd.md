---
title: "Product Requirements Document — Conda Package Supply Chain Monitor"
status: final
created: 2026-09-02
updated: 2026-09-02
supersedes: "PRD — Conda-Forge Package Health Monitor (same path, pre-import revision)"
---

# PRD: Conda Package Supply Chain Monitor

## 0. Document Purpose

This PRD is for the engineers building the monitor and for the architecture and
epic-breakdown work that consumes it. It builds on the product brief and its addendum at
`_bmad-output/planning-artifacts/briefs/brief-conda-package-supply-chain-monitor-2026-09-02/`
and does not restate them: the brief owns the problem, the roles, the scope boundary, and
the non-goals. Vocabulary is fixed in §3 Glossary and used verbatim throughout.
Requirements are grouped by feature and numbered globally as CPM-FR-1 through CPM-FR-41, so
reorganizing a feature never renumbers a requirement. Epics in §9 carry non-positional
keys for the same reason. The data model is Appendix A — it is evidence supporting the
requirements, not a prelude to them.

**Identifier namespace.** Every identifier this document defines carries a `CPM-` prefix:
`CPM-FR-n`, `CPM-NFR-n`, `CPM-SM-n`, `CPM-UJ-n`, `CPM-EP-*`. This is not decoration. The
imported service platform brought its own vocabulary — bare `AD-1`–`AD-31`, `FR-4`–`FR-44`,
`NFR-1`–`NFR-7`, `Epic 2`–`Epic 9`, `Story x.y`, `CG-3`, `R-2`/`R-3`/`R-5` — referenced in
comments across 45 files under `src/`. Those are the platform's and are never renumbered
here. A bare `FR-17` in this repository means the platform's local-sign-in refusal
condition; `CPM-FR-17` means this product's vulnerability rollup policy. Downstream
artifacts must carry the prefix.

Two things this document deliberately does not decide: how anything is implemented, and
which products are adopted. Both belong to the architecture pass that follows.

## 1. Vision

A ~10,000-package conda inventory has no unified, evidence-based view of its health. This
system builds one. It continuously collects evidence from source repositories, PyPI, and
conda-forge; evaluates that evidence against explicit deterministic policies; preserves
every observation with its source and timestamp; and presents the result — with its
provenance — to the people who have to act on it.

The authenticated web application is the primary human surface. Reviewers work the
identity and remediation queues there, read current package health, and pull operational
reports. A governed API serves integration and automation against the same evidence. A
governed, read-only natural-language capability sits inside the application for
investigation and reporting.

Deterministic policy is the source of truth throughout. A language model may explain,
summarize, or draft; it never decides whether a package is compliant, vulnerable,
current, or high priority. v1 targets the Python subset of the inventory and delivers
detection, evidence, and prioritization. It does not remediate.

## 2. Target User

Three account-holding roles, authenticated through the organization's identity provider
and authorized by the group claims it asserts. The brief's role table is the source; this
section states what each role is trying to get done.

### 2.1 Jobs To Be Done

**Security and compliance reviewer**

- Know which vulnerability, KEV, and license findings are real, current, and mine to act on.
- Distinguish a package that is genuinely clean from one nobody has assessed.
- Clear or accept a finding with the evidence attached, without opening a database console.

**Packaging engineer**

- Know what to fix next and why, ranked by something other than who is asking loudest.
- See where a package sits across source, PyPI, recipe, and published conda versions at once.
- Find the packages with no feedstock, or a feedstock nobody is maintaining.

**Platform and engineering leadership**

- Know what the monitor cannot see — coverage gaps, stale evidence, failed collectors.
- Report prioritized risk across the inventory with the evidence behind each number.
- Correct a package identity the collectors got wrong, on the record.

### 2.2 Key User Journeys

- **CPM-UJ-1. Dana clears a KEV finding before standup.**
  Dana, a security and compliance reviewer, opens the application already authenticated
  from yesterday's session. Their queue leads with three KEV findings ranked P1. They open
  the top one and see the advisory ID, the affected and fixed ranges, the version that
  matched, the source the finding came from, and when it was observed. The fixed range is
  already published to conda-forge, so they mark the finding for remediation, which routes
  it to the packaging engineer's work queue with the evidence attached. **Edge case:** if
  the vulnerability evidence is older than its freshness target, the finding shows as
  stale rather than actionable and Dana can trigger a recollection instead of acting on it.

- **CPM-UJ-2. Ravi files the feedstock that never existed.**
  Ravi, a packaging engineer, opens the feedstock-gap report. It lists packages the
  inventory depends on with no conda-forge feedstock and no staged recipe, ranked by
  internal usage breadth. They open the highest-impact package, confirm from the identity
  panel that the mapping is `verified` rather than inferred, and see that no feedstock URL
  exists on any monitored channel. They draft a tracking issue from the package detail
  view; the draft carries the package identity, the usage counts, and the evidence rows
  behind them. Ravi files it in the issue tracker and the package's tracking URL is
  recorded. **Edge case:** an `unmapped` package never appears in this report, because
  absence of a feedstock cannot be claimed for a package whose identity is unresolved.

- **CPM-UJ-3. Sam sees where the coverage gaps are.**
  Sam, a platform lead, opens the coverage view before a monthly review. It shows what
  fraction of the inventory has a resolved identity, how much evidence is inside its
  freshness target per collector, and which collection runs failed and why. One collector
  has been failing against a rate-limited source for two days; its packages show `error`,
  not clean. Sam exports the current-inventory view for the review deck, and the export
  carries the same freshness and confidence columns the application shows. **Edge case:**
  when Sam spots a package mapped to the wrong upstream repository, they correct it from
  the package detail view — the one write that changes governed reference data, recorded with their
  identity and a required reason.

## 3. Glossary

Downstream artifacts must use these terms exactly. "Identity" alone is never used.

- **Package** — one unit of the monitored inventory. Has exactly one **package identity**.
- **Package identity** — the resolved mapping of a package to its canonical name, source
  repository, release ecosystem, and conda-forge feedstock. Distinct from **user identity**.
- **User identity** — a person's authenticated account, resolved from the organization's
  identity provider. Never abbreviated to "identity".
- **Canonical name** — the unique human-readable name of a package. Unique, indexed, and
  correctable; it is not the database primary key.
- **Inventory** — the one-row-per-package snapshot of package identity and stable
  workflow metadata. Not a store of historical observations.
- **Evidence** — one timestamped, sourced observation about a package, written by exactly
  one collector into exactly one evidence table. Append-only.
- **Evidence table** — a typed store of one kind of evidence (see Appendix A.2).
- **Collector** — an independently schedulable job that writes one kind of evidence.
- **Collection run** — one execution of one collector against one package, with status,
  timing, and correlation identifiers.
- **Policy** — a versioned deterministic rule set that reads evidence and produces a
  derived status. Never an LLM.
- **Policy run** — one execution of the policy engine at a stated version against an
  evidence cut-off.
- **Current package health** — the derived, per-package rollup of all policy outputs,
  with the evidence timestamps behind them.
- **Work type** — the recommended action for a package, from a closed set (Appendix A.1).
- **Priority bucket** — `P1`–`P10`, assigned by top-down first-match rules.
- **Score** — 1–100, ranking a package within its priority bucket from usage signals.
- **Confidence** — `verified`, `inventory-derived`, or `unmapped`; the certainty of a
  **package identity**, and the gate on what automation may claim about a package. Used
  in no other sense.
- **Match confidence** — the separate, unrelated certainty that a vulnerability advisory
  applies to a given package and version. Never abbreviated to "confidence".
- **Governed reference data** — package identity and its provenance, and the inventory
  (which packages are watched, and their usage signals). Mutated by exactly two human
  paths (CPM-FR-3: the identity override, and the inventory change), each carrying a
  permission, a required reason and an audit row in the same transaction, and
  otherwise only by resolution and by ingestion's reading of the inventory.
- **Workflow state** — what a human decided about a finding or a queue item. Written by
  queue actions (CPM-FR-25); never alters evidence or governed reference data.
- **Freshness target** — the per-collector age beyond which evidence is stale, not clean.
- **Governed view** — a read-only database projection that analytics may query. Never a
  raw table.
- **Role** — one of the three account-holding roles in §2, resolved from a group claim.

## 4. Features

### 4.1 Package identity and inventory

**Description:** Every package resolves to a stable identity with recorded provenance and
confidence, or it goes to a review queue. Nothing infers an external identity it cannot
evidence, and no automated collection downgrades a human's correction. Realizes CPM-UJ-2, CPM-UJ-3.

#### CPM-FR-1: Resolve package identity

The system resolves each inventory package to a canonical name, a source repository, its
release ecosystem identity (PyPI for the Python packages v1 targets), and zero or more
conda-forge feedstocks.

**Consequences (testable):**
- A resolution that cannot establish a mapping records `unmapped`, never a guess.
- A package whose type makes a mapping inapplicable records `not_applicable` for that
  mapping, distinct from `unmapped` and from a successful empty result.
- Cross-ecosystem identifiers (package URLs, CPEs) are recorded when derivable.

#### CPM-FR-2: Persist identity provenance and confidence

Every resolution records where it came from and how confident it is.

**Consequences (testable):**
- Every package row carries an identity source, an associator key, and a confidence of
  `verified`, `inventory-derived`, or `unmapped`.
- A resolution never overwrites a `verified` confidence with a lower one.
- The timestamp of the resolution is recorded and exposed wherever the identity is shown.

#### CPM-FR-3: Manual package-identity override, and the inventory change

A platform lead can correct a package identity from the application, on the record, and
can add, change or retire an inventory row from the application, on the record. These
are the **two** human writes that mutate **governed reference data** — package identity
and the inventory — and each carries the same three obligations: a permission, a reason
the actor must supply, and an audit row (actor, timestamp, prior value, new value,
reason) written in the same transaction as the change. Queue actions (CPM-FR-25) write
**workflow state**, which is a separate class: it records what a human decided about a
finding, and never alters the package identity, the inventory or the evidence beneath
them. A wrong identity is corrected through the override, never through the inventory.
*(Amended by `sprint-change-proposal-2026-09-13.md` §7, CPM-OPERATE-S03.)*

**Consequences (testable):**
- Each write requires its permission (`identity.override_package_identity`,
  `collectors.change_inventory`); the other two roles are refused.
- Every override, and every inventory change, records actor, timestamp, prior value, new
  value, and a reason the actor must supply. The write is rejected without a reason.
- An override survives every subsequent automated collection and is downgraded only by
  an explicit re-resolution.
- Overrides, and inventory changes, are queryable as a set, so an auditor can review
  every human correction.
- Retiring an inventory row is a column write; the next ingestion records the package
  absent (CPM-FR-42). No package row and no inventory row is ever deleted. An unattended
  import from a reviewed file writes the same audit rows, naming the file in place of an
  actor.

#### CPM-FR-4: Package-identity review queue

Unmapped and low-confidence packages surface as a worked queue, not a report.

**Consequences (testable):**
- The queue lists every `unmapped` and `inventory-derived` package, ranked by internal
  usage breadth.
- A package leaves the queue only when its confidence reaches `verified`.
- The queue shows candidate mappings and the evidence for each where any exist.

#### CPM-FR-5: Confidence gates what automation may claim

Confidence constrains what the system asserts about a package.

**Consequences (testable):**
- `verified` — automated comparisons and recommendations are shown normally.
- `inventory-derived` — recommendations are shown labeled lower confidence.
- `unmapped` — the system never reports the package as current, clean, or lacking a
  feedstock; it routes to the review queue instead.

#### CPM-FR-6: Not-applicable is a distinct outcome

A check that does not apply to a package is never folded into clean or unknown.

**Consequences (testable):**
- `not_applicable`, `unknown`, `not_found`, `error`, and a successful clean result are
  five distinct, separately displayable states everywhere they appear.
- No rollup, view, export, or generated answer collapses them.

#### CPM-FR-42: Inventory ingestion

The system acquires the package inventory from a declared inventory source, together with
the internal usage signals that later rank and score it. The v1 source is a curated
watchlist of tracked packages, versioned alongside the system and changed by review
(Open Question 3a).

**Consequences (testable):**
- Ingestion is an observation: each run writes append-only rows and never updates a prior one.
- A package named by the source for the first time gains an identity row at `unmapped`
  confidence; ingestion never asserts a mapping (CPM-FR-1).
- A package absent from a later run is recorded as absent with a timestamp; no package is
  deleted, and it keeps its row in the current-health rollup.
- Every record carries `internal_component_count` and `internal_lob_count`. `apps`,
  `platforms`, `downloads` and `versions` are optional and may be blank (Open Question 3b).
- A blank signal means missing, not zero, and the two remain distinguishable in what is
  stored (Appendix A.1 data rules).
- A record missing a required field, carrying a non-numeric count, or repeating a source
  package key fails the run. No run partially ingests a malformed source.
- The internal usage signals are read at a stated evidence cut-off, so a policy replay
  reproduces identical results (CPM-FR-22).
- The source location is configuration; a source that requires credentials takes them from
  the environment with no default (CPM-NFR-10).

### 4.2 Evidence collection

**Description:** Independent collectors observe one surface each and write one evidence
table each. They are separately schedulable, retryable, and rate-limited; one failing
never blocks another. Realizes CPM-UJ-1, CPM-UJ-3.

**A surface is not an inventory.** These collectors observe external surfaces — a source
repository, PyPI, conda-forge, an advisory feed — *about packages the inventory already
names*. None of the eight introduces a package. Only CPM-FR-42's inventory ingestion does,
and only from the declared inventory source. conda-forge in particular is observed by
CPM-FR-9 and CPM-FR-10 and is never an inventory source: a public channel carries none of
the internal usage signals CPM-FR-4 ranks by and CPM-FR-20 scores with, and its package
count exceeds CPM-NFR-1's 10,000-package collection sizing several times over.

#### CPM-FR-7: Source release collector

Obtains latest release or tag, its date, and a repository activity signal from the
package's source repository.

**Consequences (testable):**
- Records lookup status explicitly, including `not_found` and `error`.
- A repository that publishes no releases records that fact rather than reporting stale.

#### CPM-FR-8: PyPI collector

Obtains project existence, latest version and date, and `Requires-Python` metadata.

**Consequences (testable):**
- A package with no PyPI presence records `not_found`, and for a non-Python package
  records `not_applicable`.
- A package is never marked stale against PyPI merely for not being published there.

#### CPM-FR-9: Conda-forge feedstock collector

Obtains feedstock existence, recipe version, recipe metadata, and recent recipe activity.

**Consequences (testable):**
- Absence of a feedstock is recorded as an observation with a timestamp, not as a null.
- Staged-recipe state is recorded separately from an existing feedstock.

#### CPM-FR-10: Published conda package collector

Obtains published version, build string, and channel for each monitored channel.

**Consequences (testable):**
- Each monitored channel produces its own observation; channels are never merged.

#### CPM-FR-11: Vulnerability collector

Matches package and version against advisory sources and records affected and fixed ranges.

**Consequences (testable):**
- Every finding records advisory ID, severity, affected range, fixed range, matched
  version, source, and match confidence.
- A package the collector could not match records `unknown`, never clean.

#### CPM-FR-12: KEV collector

Cross-references vulnerability findings against the KEV catalog.

**Consequences (testable):**
- A KEV finding links to the vulnerability finding it derives from and records the
  catalog date added.

#### CPM-FR-13: License collector

Extracts and normalizes license metadata and records the raw value alongside the
normalized expression.

**Consequences (testable):**
- Records raw license, normalized SPDX expression, and detection method.
- An unparseable license records `unknown` and routes to manual review, never `allowed`.

#### CPM-FR-14: Python 3.14 readiness collector

Performs static metadata assessment by default; build and import verification is a
separate, optionally triggered capability.

**Consequences (testable):**
- Inferred compatibility and verified compatibility are distinct recorded states.
- Verification records the platform and architecture it ran on and a log reference.
- For a package where Python compatibility does not apply, records `not_applicable`.

#### CPM-FR-15: Collector independence and run records

Each collector writes only its own evidence table plus the collection-run record.

**Consequences (testable):**
- A collector failure leaves every other collector's run unaffected and the overall run
  reported as partial success, not failure.
- Every run records collector, package, status, error detail, start and finish times,
  and the correlation identifiers of the process that performed it (see CPM-FR-39).
- Runs are idempotent: re-running a collector for the same package, source, and
  observation window does not duplicate evidence.

### 4.3 Deterministic policy and scoring

**Description:** Versioned deterministic rules read evidence and produce every derived
status in the product. Policy is re-runnable against historical evidence without
recollection. Realizes CPM-UJ-1, CPM-UJ-2.

#### CPM-FR-16: Version currency policy

Compares source, PyPI, feedstock recipe, and published conda versions using a documented
release-authority order recorded per package.

**Consequences (testable):**
- The authority decision and the evidence supporting it are stored with the result.
- Currency is computed per surface, so source-current and feedstock-stale is expressible.

#### CPM-FR-17: Vulnerability and KEV rollup policy

Derives the per-package vulnerability status and risk level from vulnerability and KEV
evidence.

**Consequences (testable):**
- A package with no vulnerability evidence at all is `unknown`, not clean.
- KEV membership is always distinguishable in the rollup, never averaged into severity.

#### CPM-FR-18: License policy

Derives allowed, restricted, forbidden, unknown, or manual-review from a versioned,
data-driven policy.

**Consequences (testable):**
- The policy is data, not code branches, and its version is recorded on every result.
- Changing the policy and re-running reproduces new results from unchanged evidence.

#### CPM-FR-19: Python 3.14 readiness policy

Derives readiness status, keeping inferred and verified evidence distinguishable.

**Consequences (testable):**
- A readiness claim states which evidence type produced it.

#### CPM-FR-20: Priority bucket and score

Assigns `P1`–`P10` by top-down first-match rules and computes a 1–100 score from internal
usage signals to rank within a bucket.

**Consequences (testable):**
- Every assignment records the bucket description, the rule that matched, and the reason,
  so the result is explainable without reading the rule set.
- Rank is derived from bucket and score and is stable for a given policy run.
- The rule set and the scoring function are versioned data, not code branches, so both
  can change without a deployment and every result records the version that produced it.

**Notes:** the bucket rules and the score function are *undefined* at this revision, the
same way the license policy is (Open Question 2). They seed from an organizational risk
posture that does not exist yet. `[ASSUMPTION: what CPM-FR-20 requires is that the rules are
versioned data and the output is explainable; their content is Open Question 8 and blocks
`CPM-EP-PRIORITY`, not this PRD.]`

#### CPM-FR-21: Work type derivation

Derives the recommended work type independently of the priority bucket.

**Consequences (testable):**
- Work type comes from the closed set in Appendix A.1.
- Work type is computable for a package in any priority bucket; the two are not coupled.

#### CPM-FR-40: Feedstock presence and maintenance policy

Derives whether a conda-forge feedstock exists for a package and, where one does, whether
it appears maintained. Realizes CPM-UJ-2.

**Consequences (testable):**
- Presence is derived only for packages at `verified` or `inventory-derived` confidence;
  an `unmapped` package reports `unknown`, never absent (CPM-FR-5).
- Absent, present-and-maintained, present-and-inactive, and staged-recipe-pending are
  distinct outcomes.
- The inactivity threshold is a versioned policy parameter, not a constant in code.

#### CPM-FR-41: Remediation readiness policy

Derives whether a package with an open finding can actually be acted on now. Realizes CPM-UJ-1.

**Consequences (testable):**
- Readiness is derived from whether a fixed version exists and where it is available —
  upstream, on PyPI, in the recipe, or published to a monitored channel.
- A finding whose fix exists nowhere yet is `blocked`, distinct from `ready` and from
  `unknown`.
- Readiness never asserts a fix is available on evidence past its freshness target;
  it reports stale instead (CPM-FR-38).

#### CPM-FR-22: Policy versioning and replay

Every policy run is versioned and re-runnable against historical evidence.

**Consequences (testable):**
- A run records policy version, run timestamp, evidence cut-off, and status.
- Re-running a stated version against a stated cut-off reproduces identical results.
- Policy results never mutate evidence.

### 4.4 The application surface

**Description:** The authenticated web application is where the three roles work. It
presents current package health, the queues each role acts on, and the operational
reports, always with the evidence and freshness behind them. A governed API exposes the
same reads for integration. Realizes CPM-UJ-1, CPM-UJ-2, CPM-UJ-3.

#### CPM-FR-23: Current package-health view

An authenticated user can browse, filter, and sort current package health across the
inventory.

**Consequences (testable):**
- The view carries the derived statuses in Appendix A.3 and the observation timestamp
  behind each.
- Results are paginated; no request returns the unbounded inventory.
- Filtering by any derived status, confidence, priority bucket, and work type is supported.
- The view states its own freshness — when the underlying rollup was last recomputed.

#### CPM-FR-24: Package detail with evidence provenance

An authenticated user can open one package and see every current status traced to the
evidence that produced it.

**Consequences (testable):**
- Each status links to the evidence rows behind it, with source, observation timestamp,
  and confidence.
- Package identity, its provenance, and its confidence are shown, including any override
  and the reason recorded with it.
- Superseded evidence remains reachable; the view shows current values without deleting
  history.

#### CPM-FR-25: Role-scoped work queues

Each role reaches its own queue: the identity review queue (CPM-FR-4), the remediation work
queue, and the compliance review queue.

**Consequences (testable):**
- A queue is ranked by priority bucket then score.
- Acting on an item records who acted, when, and the resulting state.
- A role cannot reach another role's queue (CPM-FR-31).

#### CPM-FR-26: Operational reports

The application produces the recurring reports the roles depend on: daily KEV, weekly
feedstock lag, Python 3.14 readiness, license exceptions, unmapped identities, and
stale-evidence and collector failures.

**Consequences (testable):**
- Every report states the evidence cut-off and policy version it was produced from.
- Every report is exportable, and the export carries the same freshness and confidence
  columns the application shows.

#### CPM-FR-27: Governed API

A documented HTTP API exposes the current-health, package-detail, and report reads.

**Consequences (testable):**
- A machine-readable API schema is published and generated from the implementation, not
  maintained by hand.
- The API enforces the same role scoping as the application (CPM-FR-31).
- Every collection endpoint is paginated with a maximum page size.
- The API's only writes in v1 are the package-identity override (CPM-FR-3) and queue actions
  (CPM-FR-25). No endpoint writes evidence, and no endpoint writes a derived status.

#### CPM-FR-28: Operational probes

Liveness and readiness endpoints report process and dependency health.

**Consequences (testable):**
- Probes carry no credential and are reachable without authentication.
- Readiness fails when a required dependency is unavailable; liveness does not.
- Probe endpoints are excluded from the authenticated surface and from rate limiting.

### 4.5 Authentication and authorization

**Description:** People authenticate through the organization's identity provider; the
group claims it asserts determine what they can reach. The brief's role table is the
product-level source for that mapping. Realizes CPM-UJ-1, CPM-UJ-2, CPM-UJ-3.

#### CPM-FR-29: Authenticate through the organization's identity provider

Users authenticate via OIDC against the organization's provider.

**Consequences (testable):**
- No local password authentication is offered in any deployed environment.
- Token lifetime and refresh behavior are configured, not left to defaults.
- An authentication carrying no group claim is refused, and is distinguishable from an
  authentication asserting zero groups.

#### CPM-FR-30: Map group claims to roles

Asserted group claims resolve to the three roles.

**Consequences (testable):**
- A group asserted at the provider grants its role without a manual step.
- A group revoked at the provider removes its role at the next resolution.
- The mapping is configuration, not code.

#### CPM-FR-31: Role-scoped surfaces

Each role reaches the evidence, queues, and reports it is responsible for.

**Consequences (testable):**
- Every view, report, and API endpoint declares the role it requires.
- A request from a role without that grant is refused, and the refusal is logged.
- Read access to evidence is available to all three roles; queues are role-exclusive.

#### CPM-FR-32: Privileged writes are audited

Every write that changes governed data records who did it and why.

**Consequences (testable):**
- The override write (CPM-FR-3) and every queue action (CPM-FR-25) record actor, timestamp, and
  prior state.
- The audit record is append-only and independently queryable.

### 4.6 Reporting and the natural-language capability

**Description:** A governed, read-only natural-language capability lets a user investigate
the evidence store conversationally. It is a capability within the product, not the
product. It reads governed views only, and every answer cites the evidence behind it.
Realizes CPM-UJ-1, CPM-UJ-3.

#### CPM-FR-33: Governed read-only analytics access

Analytics and natural-language components reach the data through a read-only role over
approved views.

**Consequences (testable):**
- The component holds no write privilege on any table.
- It reaches governed views only, never raw tables.
- Query timeout, row limit, and request audit logging are enforced and configurable.
- Access to sensitive internal usage fields is restricted and separately grantable.

#### CPM-FR-34: Evidence-cited natural-language answers

A user can ask a question in natural language and receive an answer traceable to evidence.

**Consequences (testable):**
- Every answer cites the evidence table, evidence identifier, and observation timestamp
  it used.
- An answer never states a figure not present in a cited row.
- Missing, stale, failed, and not-applicable states are reported as themselves, never as
  clean or omitted.
- The capability never asserts a compliance, severity, currency, or priority conclusion
  that policy did not produce.

#### CPM-FR-35: Repeatable investigation and reporting compositions

The recurring investigations and reports are composable and repeatable rather than
re-prompted each time.

**Consequences (testable):**
- A composition orchestrates deterministic services and governed queries; it embeds no
  business rule that policy does not own.
- Running the same composition against the same evidence cut-off yields the same result.

### 4.7 Auditability and evidence integrity

**Description:** The system must be able to explain what was known, when, from where, and
why a policy decision changed. Realizes CPM-UJ-1, CPM-UJ-3.

#### CPM-FR-36: Evidence is append-only

No finding is overwritten in place; each observation is a new row.

**Consequences (testable):**
- No collector issues an update or delete against an evidence table.
- Re-observing an unchanged fact is still a new row with a new observation timestamp.

#### CPM-FR-37: Current values are derived and timestamped

Current status is computed from the latest eligible evidence and exposes that evidence's
timestamp.

**Consequences (testable):**
- No current-status field is directly writable.
- Every displayed status is accompanied by the observation timestamp behind it.

#### CPM-FR-38: Staleness and failure are visible

Evidence past its freshness target, failed collection, and unavailable sources are shown
as themselves.

**Consequences (testable):**
- A freshness target is defined per collector, and evidence past it displays as stale.
- Collection failures are visible in the application, not only in logs.
- Stale never displays as clean.

#### CPM-FR-39: Observations carry correlation identifiers

Every collection run and privileged write is traceable to the process that performed it.

**Consequences (testable):**
- Each collection-run record carries the trace identifier of the request or task that
  produced it.
- Log lines emitted during a run carry the same identifier, so an `error` state is
  investigable rather than merely recorded.

## 5. Cross-Cutting Non-Functional Requirements

**Scale and scheduling**

- **CPM-NFR-1:** Full-inventory collection at 10,000 packages completes without manual batching.
- **CPM-NFR-2:** Collector cadences are configured independently — daily for security and KEV,
  daily to weekly for version currency, on demand for Python 3.14 build verification.
- **CPM-NFR-3:** External calls apply rate limiting, retries with backoff, request timeouts,
  and caching. A rate-limited source degrades to stale evidence, never to a clean result.

**The application tier**

- **CPM-NFR-4:** Every collection response is paginated with an enforced maximum page size.
  No view or endpoint can return the unbounded inventory.
- **CPM-NFR-5:** The current-health view and its API equivalent respond within a stated p95
  latency budget at full inventory size with filters applied. `[ASSUMPTION: budget to be
  set during architecture; the requirement is that one exists and is enforced.]`
- **CPM-NFR-6:** Work that cannot complete inside a request — recollection, verification
  builds, policy runs, exports over a stated size — is asynchronous, and the user is told
  it is in progress rather than left waiting.
- **CPM-NFR-7:** Readiness and liveness probes answer independently of application load and
  are excluded from rate limiting.

**Security and data governance**

- **CPM-NFR-8:** All LLM-facing components have read-only access, row limits, and query timeouts.
- **CPM-NFR-9:** Sensitive internal usage fields are not transmitted to an external model API
  without explicit approval; a private or self-hosted deployment must be supportable.
- **CPM-NFR-10:** Configuration comes from the environment; no credential, endpoint, or
  secret is committed or defaulted to a production value.
- **CPM-NFR-11:** Refused authorization, privileged writes, and analytics queries are logged
  with the acting user identity.

**Observability**

- **CPM-NFR-12:** Logs are structured and machine-parseable, and carry request, user, and
  trace identifiers.
- **CPM-NFR-13:** Requests, background tasks, database queries, and cache calls are traced,
  and traces correlate to the collection runs and writes they performed.

## 6. Non-Goals

The brief owns the product-level non-goals and this PRD does not restate them. Two
requirement-level exclusions are stated here because they bound FRs above:

- No write API beyond the package-identity override (CPM-FR-3) and queue actions (CPM-FR-25).
  Nothing writes evidence or a derived status through the API in v1.
- No automated remediation triggered by a policy result. CPM-FR-25 routes work to a human;
  it never acts.

## 7. MVP Scope

**In scope**

- Inventory ingestion, package identity resolution, provenance, confidence, the review
  queue, and the audited override (CPM-FR-1 – CPM-FR-6, CPM-FR-42).
- All eight collectors and the collection-run record (CPM-FR-7 – CPM-FR-15).
- All eight policy areas, priority, score, and work type, versioned and replayable
  (CPM-FR-16 – CPM-FR-22, CPM-FR-40, CPM-FR-41).
- The application surface, governed API, and probes (CPM-FR-23 – CPM-FR-28).
- OIDC authentication, group-claim role mapping, role scoping, and write auditing
  (CPM-FR-29 – CPM-FR-32).
- Auditability and evidence integrity (CPM-FR-36 – CPM-FR-39).

**Out of scope for MVP**

- The natural-language capability (CPM-FR-33 – CPM-FR-35) ships after the evidence store and
  policy engine are producing trustworthy results. There is nothing to govern, cite, or
  explain until they do. Its candidate stack has not passed an evaluation gate.
- Python 3.14 build and import verification at scale. CPM-FR-14's static assessment is in;
  compute-backed verification is triggered per package, not run across the inventory.
- Non-Python conda artifacts. Modeled from v1 via `not_applicable` (CPM-FR-6); exercised in
  the later phase.
- Historical trend dashboards beyond queryable history.

## 8. Success Metrics

**Primary**

- **CPM-SM-1:** At least 95% of targeted packages hold a `verified` or `inventory-derived`
  package identity after the first full collection cycle. Validates CPM-FR-1, CPM-FR-2, CPM-FR-4.
- **CPM-SM-2:** Every vulnerability, KEV, license, and Python 3.14 finding carries source,
  observation timestamp, and confidence; zero findings present an unknown as clean.
  Validates CPM-FR-6, CPM-FR-11 – CPM-FR-14, CPM-FR-17, CPM-FR-38.
- **CPM-SM-3:** A reviewer carries an unmapped package from the review queue to a resolved,
  attributed package identity entirely within the application. Validates CPM-FR-3, CPM-FR-4, CPM-FR-25.

**Secondary**

- **CPM-SM-4:** A weekly full-inventory refresh completes unattended, and partial failures are
  visible in the application without reading logs. Validates CPM-FR-15, CPM-FR-38, CPM-NFR-1.
- **CPM-SM-5:** Each role reaches its own queue and current-health view without being granted
  another role's surface. Validates CPM-FR-30, CPM-FR-31.
- **CPM-SM-6:** A stated policy version re-run against a stated evidence cut-off reproduces
  identical results. Validates CPM-FR-22.

**Counter-metrics (do not optimize)**

- **CPM-SM-C1:** Proportion of packages showing a clean result. Counterbalances CPM-SM-1 and CPM-SM-2.
  Driving this up by resolving `unknown` or `unmapped` into clean is the primary failure
  mode of the entire product.
- **CPM-SM-C2:** Time to close a queue item. Counterbalances CPM-SM-3. Faster is not better if
  overrides are recorded without a substantive reason.
- **CPM-SM-C3:** Natural-language answer volume. Counterbalances CPM-SM-2. The capability
  succeeds by being traceable, not by being used often.

## 9. Epics

Non-positional keys. Adding an epic never renumbers another.

| Key | Delivers | Requirements | Depends on |
|---|---|---|---|
| `CPM-EP-PLATFORM` | Django service platform: settings, OIDC authorization, probes, Celery, observability, deployment contract. **Largely complete — imported.** | CPM-FR-28, CPM-FR-29, CPM-FR-30, CPM-FR-39, CPM-NFR-10, CPM-NFR-12, CPM-NFR-13 | — |
| `CPM-EP-IDENTITY` | Package identity resolution, the inventory, provenance, confidence, review queue, audited override | CPM-FR-1 – CPM-FR-6, CPM-FR-32, CPM-FR-42 | `CPM-EP-PLATFORM` |
| `CPM-EP-CURRENCY` | Source, PyPI, feedstock, and published-conda collectors; version currency and feedstock presence policies | CPM-FR-7 – CPM-FR-10, CPM-FR-15, CPM-FR-16, CPM-FR-40 | `CPM-EP-IDENTITY` |
| `CPM-EP-SECURITY` | Vulnerability, KEV, and license collectors and their policies; remediation readiness | CPM-FR-11 – CPM-FR-13, CPM-FR-17, CPM-FR-18, CPM-FR-41 | `CPM-EP-IDENTITY`, `CPM-EP-CURRENCY` |
| `CPM-EP-RENAME` | Renames the product to Conda-Sentinel and the import root to `conda_sentinel`. Changes no behaviour and satisfies no requirement. | — | `CPM-EP-SECURITY` |
| `CPM-EP-PY314` | Python 3.14 static assessment, then optional build and import verification | CPM-FR-14, CPM-FR-19 | `CPM-EP-IDENTITY` |
| `CPM-EP-PRIORITY` | Priority bucket, score, rank, work type; policy versioning and replay | CPM-FR-20 – CPM-FR-22 | `CPM-EP-CURRENCY`, `CPM-EP-SECURITY` |
| `CPM-EP-APP` | Current-health view, package detail, role-scoped queues, reports, governed API, role scoping | CPM-FR-23 – CPM-FR-27, CPM-FR-31, CPM-NFR-4 – CPM-NFR-6 | `CPM-EP-PRIORITY` |
| `CPM-EP-EVIDENCE` | Append-only evidence store, derived current values, freshness and failure visibility | CPM-FR-36 – CPM-FR-38 | `CPM-EP-PLATFORM` |
| `CPM-EP-NL` | Governed read-only analytics access, evidence-cited answers, repeatable compositions | CPM-FR-33 – CPM-FR-35 | `CPM-EP-APP` |

`CPM-EP-EVIDENCE` is listed after the collectors that write into it but is a dependency of
them; it is sequenced first in delivery. `CPM-EP-NL` is post-MVP (§7).

## 10. Open Questions

1. Which advisory and KEV data sources are available and licensed for use? Blocks `CPM-EP-SECURITY`.
2. What license allow/deny policy seeds CPM-FR-18? Blocks `CPM-EP-SECURITY`.
3. **Resolved 2026-09-04.** Kept numbered rather than struck: downstream artifacts cite
   "Open Question 3", and this list never reuses a number.

   **3a — the inventory source.** *What inventory source carries the package inventory
   (CPM-FR-42)?* **Answer:** a curated watchlist of tracked packages, versioned in this
   repository and changed by pull request. A smaller subset of the same file shape serves
   local development, selected by locality (CPM-AD-29). An internal-inventory-system
   integration, should one arrive, is a second adapter behind the same contract rather
   than a redesign.

   conda-forge is **not** an inventory source. It is a surface the feedstock collector
   (CPM-FR-9) and the published-package collector (CPM-FR-10) observe. A public channel
   carries no internal usage signals, so an inventory sourced from it leaves CPM-FR-4's
   usage-breadth ranking and CPM-FR-20's score with no input at all, and its package
   count exceeds CPM-NFR-1's 10,000-package collection sizing several times over.
   Recorded here so the question is not reopened by a later reader who reads "inventory"
   as "channel".

   **3b — the internal usage-signal field set.** *Which signals are observed per package?*
   **Answer:** `internal_component_count` and `internal_lob_count` are required on every
   inventory record — together they are the "internal usage breadth" CPM-FR-4 ranks by.
   `apps`, `platforms`, `downloads` and `versions` are present in the schema and nullable:
   they are score inputs for CPM-FR-20, whose score function is itself undecided (Open
   Question 8), and no hand-authored watchlist can state them credibly. A source that can
   supply them populates them. Blank means missing, per Appendix A.1's data rules, and is
   never invented.
4. Which conda channels and platforms are monitored (CPM-FR-10)? Blocks `CPM-EP-CURRENCY`.
5. What is the p95 latency budget for CPM-NFR-5, and at what inventory size is it measured?
6. Does the internal-data handling requirement force a private or self-hosted model
   deployment for CPM-FR-34? Blocks `CPM-EP-NL`.
7. **Resolved 2026-09-05.** Kept numbered rather than struck, as Open Question 3 was.
   *What are the per-collector freshness targets for CPM-FR-38?* Answered at the moment
   this list said it would be needed: `CPM-IDENTITY-S06` ships the first collector.

   **7a — how a target is chosen.** **Answer:** derived, not picked. A freshness target is

   ```
   freshness_target = cadence x (1 + tolerated_missed_runs) + one sweep duration
   ```

   Cadence is not open — CPM-NFR-2 already fixes it per signal class, and CPM-AD-20 makes
   it data in `django_celery_beat` rather than code. One sweep duration is a measurement,
   not a judgement. So the only judgement is `tolerated_missed_runs`: how many consecutive
   missed collections may pass before the product stops calling an answer current. That is
   a risk posture, it is per **signal class** rather than per collector, and it is three
   numbers rather than nine.

   | Signal class | Cadence (CPM-NFR-2) | Tolerated missed runs | Freshness target |
   |---|---|---|---|
   | Vulnerability, KEV | daily | 1 | 2 days |
   | Licence | daily | 2 | 3 days |
   | Version currency — source, PyPI, feedstock, published conda | daily to weekly | 1 | 2x the configured cadence |
   | Inventory ingestion | daily | 1 | 2 days |
   | Python 3.14 verification | on demand | — | see 7c |

   **The target must be strictly greater than the cadence, and that is the rule the table
   exists to enforce.** `core/freshness.py` reports stale when `observed_at < now - target`,
   so a target equal to its cadence makes evidence go stale at exactly the moment the next
   run is due: any delay in scheduling, and the whole inventory reads stale without a single
   collection having failed. A target below its cadence is worse — evidence is stale in the
   ordinary case, and CPM-FR-38's "stale never displays as clean" stops carrying information
   because everything is stale.

   The values above are the starting point and are expected to move once the sweep durations
   in 7b are measured. What is settled is the derivation, not the arithmetic's inputs.

   **7b — the measurement the targets still owe.** *How long does a full sweep take per
   collector at realistic inventory size, under CPM-NFR-3's rate limiting?* Not answerable
   before a populated inventory exists, which is what `CPM-IDENTITY-S06` and
   `CPM-IDENTITY-S07` deliver. A target must exceed one sweep's wall-clock duration plus its
   cadence, or evidence goes stale in the gap between successful runs. The table's values
   assume a sweep completes well inside its cadence; the first full run at CPM-NFR-1's
   10,000 packages is what confirms or refutes that, and CPM-EP-CURRENCY is where it is
   measured.

   **7c — the collector with no cadence.** *What target does the Python 3.14 verification
   collector take?* CPM-NFR-2 runs it on demand, so there is no cadence to derive from, and
   CPM-AD-28 still requires a strictly positive target. Open. It is one collector and it is
   in `CPM-EP-PY314`, so it blocks `CPM-PY314-S02` and nothing before it. The two candidate
   readings are a target measured from the *request* rather than from a schedule, or a
   verification result that is treated as durable evidence about an immutable artifact and
   therefore never stale — which would need CPM-AD-28 amending rather than a number.

   **7d — whether a target is code or data.** *Should `freshness_target` move out of the
   collector class?* Open, and the asymmetry is worth naming: CPM-AD-20 makes cadence data
   in the `DatabaseScheduler`, while CPM-AD-28 makes the target a `ClassVar` the collector
   base validates at construction. So tuning a target is a code change and a deployment,
   while tuning the cadence it is derived from is a database edit. 7b says these values will
   be tuned. This is an architecture decision rather than a product one, so it is recorded
   here and owned by the architecture pass; until it is answered, the `ClassVar` stands and
   CPM-AD-28's construction-time refusal is unchanged.
8. What seeds the CPM-FR-20 priority rule set and the score function? Both are undefined —
   they encode an organizational risk posture that does not exist yet. Blocks
   `CPM-EP-PRIORITY`; does not block the collectors or the application.
9. How does a separate analytics service fit the deployment contract? `component.toml`
   declares process types and per-database release-stage migration steps, and nothing has
   examined what a second service adds to either. Owned by the architecture pass; blocks
   `CPM-EP-NL`.
10. What is the inactivity threshold that makes a feedstock "unmaintained" (CPM-FR-40), and
    what counts as recipe activity? Blocks `CPM-EP-CURRENCY`.

This list supersedes the divergent open-questions list in the README, which should link
here rather than restate.

## 11. Assumptions Index

- **§5 CPM-NFR-5** — the p95 latency budget is stated as a requirement to exist; its value is
  set during the architecture pass. Tracked as Open Question 5.
- **§4.3 CPM-FR-20** — the requirement is that the priority rules and score function are
  versioned data and that the output is explainable; their *content* is deliberately not
  specified here. Tracked as Open Question 8.

---

## Appendix A. Data Model

Supporting detail for §4. The architecture pass owns how any of it is implemented.

### A.1 The inventory

One row per package. Stable package identity and workflow metadata only; every volatile
observation belongs in an evidence table (CPM-FR-36).

**Key.** The primary key is a surrogate integer. **Canonical name** is a unique, indexed,
correctable column — not the key. A Python-specific natural key does not survive the
non-Python later phase, and a surrogate keeps evidence foreign keys narrow and makes
correcting a canonical name non-cascading.

**Two contracts.** The stored field names and the export column names are different
contracts and must not be conflated. Stored fields are valid identifiers in snake_case.
Export columns preserve the historical report headings the existing consumers read; the
reporting layer owns the projection between them (CPM-FR-26).

| Stored field (contract) | Export column | Role |
|---|---|---|
| `canonical_name` | `Core_Python_Package_Name` | Unique package name |
| `display_name` | `Package` | Display name |
| `priority_bucket` | `P` | `P1`–`P10`, derived |
| `rank` | `Rank` | 1-based rank within snapshot |
| `score` | `Score` | 1–100 within a bucket |
| `work_type` | `Work` | Derived work type |
| `platforms`, `apps`, `downloads`, `versions` | `Platforms`, `Apps`, `Downloads`, `Versions` | Internal usage signals |
| `vulnerability_rollup` | `Vuln` | Derived vulnerability rollup |
| `primary_purl`, `primary_type`, `alternative_purls`, `cpes`, `conda_purl` | same | Cross-ecosystem identifiers |
| `source_repository_url` | `source_repository_url` | Upstream VCS identity |
| `feedstock_url`, `feedstock_metadata_url`, `staged_recipe_pr_url` | `Conda-Forge_FeedStock_URL`, `Conda-Forge_Metadata_URL`, `Staged_Recipes_PR_URL` | Conda-forge state |
| `local_recipe_url`, `local_build_status`, `verified_at` | `Local_Recipes_URL`, `Local_Build_Status`, `Verification_Timestamp_UTC` | Internal packaging state |
| `identity_source`, `associator_key`, `confidence` | `identity_source`, `associator_key`, `associator_status` | Package-identity provenance (CPM-FR-2) |
| `priority_description`, `priority_source`, `priority_reason` | `Priority_Bucket_Description`, `Priority_Source`, `Priority_Reason` | Explainable priority (CPM-FR-20) |
| `risk_level`, `latest_vuln_count` | `JFROG_risk_level`, `JFROG_latest_vuln_count` | Vulnerability rollups |
| `internal_component_count`, `internal_lob_count` | same | Internal impact breadth |
| `tracking_title`, `tracking_issue_url` | `OpenTeams_Title`, issue URL fields | Issue-tracker links |

**Data rules.** Blank means missing; values are never invented. Multi-value export
columns separate with `;`. A `verified` confidence is never overwritten by a
lower-confidence resolution (CPM-FR-2).

**Closed work-type set** (CPM-FR-21): fix vulnerability · create recipe · file tracking issue ·
already tracked · update feedstock · validate Python 3.14 · review license · resolve identity.

### A.2 Evidence tables

Append-only, keyed by package, source, and observation time (CPM-FR-36). An
`inventory_snapshots` row is keyed the same way; the package shell it references is created
by resolution in the same transaction, never by the collector (`CPM-AD-25`).

| Table | Records |
|---|---|
| `source_release_snapshots` | Upstream latest version, release date, repository activity, lookup status |
| `pypi_release_snapshots` | PyPI existence, latest version and date, `Requires-Python` |
| `feedstock_snapshots` | Feedstock existence, recipe version, recipe activity, build/test outputs |
| `conda_package_snapshots` | Published version, channel, build string |
| `vulnerability_findings` | Advisory ID, affected and fixed ranges, severity, matched version, source, match confidence |
| `kev_findings` | Link to the vulnerability finding, KEV catalog date added |
| `license_findings` | Raw license, normalized SPDX expression, detection method, policy result |
| `python314_findings` | Status, evidence type, log reference, tested platform and architecture |
| `collection_runs` | Collector, package, status, error detail, start and finish times, correlation identifiers |
| `policy_runs` | Policy version, run timestamp, evidence cut-off, status |
| `identity_overrides` | Actor, timestamp, prior value, new value, reason (CPM-FR-3) |
| `inventory_snapshots` | Source package key; `internal_component_count` and `internal_lob_count`, required; `apps`, `platforms`, `downloads`, `versions`, nullable; presence or absence; observation time (CPM-FR-42, Open Question 3b) |
| `inventory_changes` | Actor or the file an unattended import read; timestamp; prior and new value of every changeable inventory field, including retired; reason (CPM-FR-3 as amended, CPM-OPERATE-S03) |

`inventory` — one mutable row per source package key, carrying the eight watchlist
columns, `retired_at`, `changed_at` and the last reason — is governed reference data
rather than evidence: it is what ingestion reads (`CPM_INVENTORY_SOURCE=database`), and
every change to it is recorded in `inventory_changes` in the same transaction.

### A.3 Derived statuses

Computed by policy (§4.3), exposed by the current-health view (CPM-FR-23), and carried into
every export and generated answer with the evidence timestamp behind each.

| Derived status | Produced by |
|---|---|
| Source currency, PyPI currency, feedstock currency | CPM-FR-16 |
| Feedstock presence and maintenance | CPM-FR-40 |
| Python 3.14 compatibility | CPM-FR-19 |
| License compliance | CPM-FR-18 |
| Vulnerability status, KEV status | CPM-FR-17 |
| Remediation readiness | CPM-FR-41 |
| Evidence freshness | CPM-FR-38 |
| Recommended work | CPM-FR-21 |
| Priority bucket, score, rank | CPM-FR-20 |

Each is independently capable of `not_applicable`, `unknown`, `not_found`, `error`, or a
determinate result (CPM-FR-6).
