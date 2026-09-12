# Epic identity Context: Every package resolved, or visibly not

<!-- Compiled from planning artifacts. Edit freely. Regenerate with compile-epic-context if planning docs change. -->

## Goal

Deliver the package identity layer: every inventory package resolves to a canonical name, a source repository, its release ecosystem identity (PyPI for v1) and zero or more conda-forge feedstocks, with the provenance and confidence of that resolution recorded on the row. Confidence then gates what automation may claim about the package, unresolved and low-confidence packages are selectable as a ranked review set, and a platform lead can correct a wrong package identity through the one audited human write in the product. The user outcome is CPM-UJ-3 (and CPM-SM-1 / CPM-SM-3): a platform lead can carry an unmapped package to a resolved, attributed package identity and correct a wrong one on the record. The counter-metric that outranks everything (CPM-SM-C1): nothing may resolve `unknown` or `unmapped` into clean.

## Stories

- Story CPM-IDENTITY-S01: The package identity model
- Story CPM-IDENTITY-S02: Resolution that records where it came from
- Story CPM-IDENTITY-S03: Confidence gates what the system will claim
- Story CPM-IDENTITY-S04: Unresolved packages are selectable and ranked
- Story CPM-IDENTITY-S05: The one audited human write
- Story CPM-IDENTITY-S06: The inventory arrives, and arrives as evidence
- Story CPM-IDENTITY-S07: The watchlist is the inventory source
- Story CPM-IDENTITY-S08: Resolution that finds its mappings

## Requirements & Constraints

- **Resolution** (CPM-FR-1): each package maps to canonical name, source repository, release-ecosystem identity and zero or more feedstocks; cross-ecosystem identifiers (purls, CPEs) are recorded when derivable. A mapping that cannot be established records `unmapped`, never a guess. A mapping that does not apply to the package type records `not_applicable`, distinct from `unmapped` and from a successful empty result.
- **Provenance** (CPM-FR-2): every package row carries `identity_source`, `associator_key` and a `confidence` of `verified` / `inventory-derived` / `unmapped`, plus the resolution timestamp. A resolution never overwrites `verified` with a lower confidence.
- **Override** (CPM-FR-3, CPM-FR-32): the only human write to governed reference data. Requires the override permission (platform lead), a non-empty reason, and an append-only, independently queryable audit row (actor, timestamp, prior value, new value, reason). It survives every later automated resolution and is downgraded only by explicit re-resolution. Refusals are logged with the acting user identity.
- **Review set** (CPM-FR-4): every `unmapped` and `inventory-derived` package, ranked by internal usage breadth (`internal_component_count`, `internal_lob_count`); candidate mappings and their evidence shown where any exist; a package exits only on reaching `verified`.
- **Confidence gate** (CPM-FR-5, ADR-004): `verified` shows normally; `inventory-derived` shows with a label and the value is not degraded; `unmapped` is never reported current, clean or lacking a feedstock.
- **Five-state vocabulary** (CPM-FR-6): `not_applicable`, `unknown`, `not_found`, `error` and a clean determinate result are five distinct states; nothing collapses them.
- **Inventory ingestion** (CPM-FR-42): the inventory is observed as append-only evidence; a first-seen package gains a row at `unmapped`; ingestion never asserts a mapping; absence is recorded, never deleted; malformed sources fail the run whole.
- Vocabulary is fixed: never bare "identity" or bare "confidence" — always "package identity" and "package-identity confidence"; "match confidence" is a different thing.

## Technical Decisions

- **Layering:** `identity` sits on `core` and below `collectors`, `policies` and `workflow`. It may not host a queue table or workflow state (those belong to `workflow`, built in CPM-EP-APP); it exposes a selection/ranking service instead.
- **Package row is identity only** (CPM-AD-1): canonical name, cross-ecosystem mappings, provenance, confidence. No derived status, observation, workflow state or usage signal. The PRD export contract (Appendix A.1) is a projection performed by `reporting`, never a field list.
- **Keys** (CPM-AD-3): surrogate integer pk; `canonical_name` unique, indexed, correctable, never an FK target. Resolution and ingestion find an existing row by the `(identity_source, associator_key)` unique pair, never by the correctable name, and neither resolution nor override touches that pair while correcting a name — otherwise a rename orphans the package from its inventory evidence.
- **One write path** (CPM-AD-14, CPM-AD-25): identity is mutated by resolution or the audited override, nothing else. Package-shell creation is resolution: the ingestion collector calls `identity`'s resolution service rather than writing the table. Workflow state never touches identity or evidence.
- **Confidence gate is one function in `core`** (CPM-AD-4), called by the orchestrating policy run and never re-implemented per pass. It gates by writing a value (`unknown`) so every package always has a rollup row; it never suppresses a row. `inventory-derived` sets a label only.
- **Atomicity** (CPM-AD-23): override and its audit row commit in one service-function transaction — never `on_commit`, never a follow-up task. Ingestion commits shell plus snapshot per package; a later package's failure never rolls back an earlier one.
- **Correlation** (CPM-AD-15): `identity_overrides` and run-ledger rows persist the platform's `trace_id`; no product-level correlation scheme.
- **Collector conventions inherited from CPM-EP-EVIDENCE** (CPM-AD-20, CPM-AD-27, CPM-AD-28, CPM-AD-9): any collector or automated resolver that calls out runs on the `collect` queue through the shared collector base, which owns timeout, retry, backoff, rate limiting, the observation window and the transport seam, so parsing is unit-testable from a recorded payload. Cadence is scheduler data, not a decorator. Each registered collector declares a freshness target or startup refuses. Work is chunked per package under the inherited 5-minute task limit. A failed or rate-limited call yields `error` / `not_found`, never clean and never no row. A web request may write an override but may never make an outbound call or run a resolver.
- **Inventory source** (CPM-AD-29): one declared adapter (never discovered), locality selects the file and fails closed toward the production watchlist; unreadable or malformed input raises `ImproperlyConfigured` before any row is written.
- **Time** (CPM-AD-26): the injected clock, never `timezone.now()` directly.
- **Status fields** (CPM-AD-5): `CharField(choices=...)` over an `OutcomeState`-derived type; never boolean.
- **Do not invent:** watchlist content, and any mapping the source did not evidence. Blank means missing, never zero.

## UX & Interaction Patterns

- Package-identity confidence renders as a text-only tag, never as a status chip; `inventory-derived` is a label on a shown value, never a dimming or a hiding.
- The view never implements the gate; gated statuses arrive already written as `unknown` in the rollup. An `unmapped` package still appears in every list with one rollup row; the only permitted exclusion is the feedstock-gap report, stated on the surface with count and reason.
- The override is a server-validated form in a modal: empty reason returns the bound form with the error; on success the override stays displayed on the package permanently with actor, timestamp, prior value and reason. The review queue page is CPM-EP-APP's; the override write and its form are this epic's.
- Do not optimise the override for speed (CPM-SM-C2).

## Cross-Story Dependencies

- Depends on CPM-EP-EVIDENCE: the append-only base, `OutcomeState`, run-ledger models, the three queues and the collector base.
- S06 and S07 are built first and together; every later story assumes packages exist. S02 closes the shell-rename trap S06 introduced; S05's override must respect the same `(identity_source, associator_key)` rule.
- S08 builds on S02's resolution service and provenance fields and on the collector base; its mappings land at `inventory-derived`, so S03's gate and S04's ranked selection already apply to its output, and it must never lower a `verified` or override-set confidence.
- S03's gate function is consumed by the policy runs of CPM-EP-CURRENCY, CPM-EP-SECURITY and CPM-EP-PY314; S04's selection is rendered by CPM-APP-S05. CPM-EVIDENCE-S09 converts the run ledger's package reference to a real FK once packages exist.
