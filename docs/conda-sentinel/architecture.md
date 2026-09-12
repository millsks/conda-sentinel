# How Conda-Sentinel is built

A distillation. The full record is the architecture spine in
`_bmad-output/planning-artifacts/architecture/`, and it stays the record — a second
complete copy here would be a second thing to keep true, and the one that drifts is
always the copy. Identifiers are preserved so the full text stays findable: every
`CPM-AD-n` below is a section there.

!!! note "On the `AD-` prefix"

    A bare `AD-n` in this repository is an **inherited** platform decision. A decision
    from this product's own spine always carries `CPM-`.

## Six domains, one app each

`CPM-AD-19` — one app per domain, routed centrally.

| App | Owns | Tables |
|---|---|---|
| `identity` | who a package **is** | `packages`, `package_mappings`, `feedstocks`, `identity_overrides` |
| `collectors` | what upstreams **said** | eleven evidence tables |
| `policies` | what the product **concludes** | eight derived tables |
| `core` | the run ledger, the rollup, the shared kernel | `policy_runs`, `collection_runs`, `package_health`, `background_jobs` |
| `workflow` | work items and their history | `workflow_items`, `workflow_transitions` |
| `surface` | the screens and the API | **none** |

`surface` declaring no model is the point rather than an omission: `CPM-AD-10` gives
the application layer no write path to a derived status, so a read surface with a
model would declare the one thing the decision forbids it.

Routing is central — `config/urls.py` and `config/api_router.py` — which is why moving
the whole application under `/conda-sentinel/` was two lines.

## The rules that shape everything else

### Evidence cannot be edited — `CPM-AD-2`

An observation is a row with a timestamp and a source. A later observation is a **new
row**. Nothing updates one, and the base model refuses an update rather than trusting
callers to remember.

Run ledgers are explicitly *not* evidence: a run starts, ends and has a state, so it
mutates. Keeping the two apart is what lets everything else be append-only without
awkward exceptions.

### Confidence gates every claim, by writing a value — `CPM-AD-4`

If identity confidence is `unmapped`, every derived status for that package is written
as <span class="cs-state unknown">unknown</span>.

**By writing, never by suppressing.** Every inventory package always gets a rollup row
(`CPM-AD-11`), so a package the product cannot speak about is visible as a package it
cannot speak about — rather than missing from a list, which reads as "not a problem".

The gate lives in one place, in the rollup composition, and is not re-implemented per
pass. `tests/unit/django_apps/test_confidence_gate_audit.py` holds that.

### Five states, one precedence order — `CPM-AD-5`

```text
error  →  unknown  →  not_found  →  not_applicable  →  ok
```

Total, declared once, and asserted to be total: a state added without being placed in
the order fails rather than acquiring a rank by accident. Every aggregation anywhere
uses it.

### Policy is a separate versioned pass — `CPM-AD-8`, `CPM-AD-21`

Collectors never conclude. Passes never observe. A pass reads evidence at a stated
cut-off and writes a verdict stamped with the rule version that produced it, so
"why did this change?" is answerable: either the evidence moved or the rules did, and
the row says which.

One orchestrated run owns the rollup. Passes are **registered**, not imported — the
orchestrator in `core` never imports `policies`, which is what lets a pass be added
without touching it.

### Derived state is read-only to the application — `CPM-AD-10`

The application layer has exactly two writes: **workflow state** (`CPM-AD-22`) and
**the package-identity override** (`CPM-AD-14`). Nothing else. No view, no serializer,
no management command edits a verdict — a verdict is changed by re-running the policy
that produced it.

`tests/unit/django_apps/test_derived_status_writability_audit.py` sweeps for a way
around this.

### One row per package — `CPM-AD-11`

`package_health` is a refreshed rollup, one row per inventory package, carrying every
domain status plus `computed_at`, `evidence_cutoff` and a per-domain version map.

Not a materialized view: it is written by the policy run, inside the same transaction
discipline as everything else, and it carries freshness **per row** — because a
replayed run leaves rows computed at different instants, and a single header stamp
would be wrong for some of them.

### Every read surface projects the same values — `CPM-AD-24`

The screen, the API and the CSV export call the same projection functions. The
decision names the failure it prevents: *an export rendering `unknown` as a blank
cell, destroying the five states in the one artifact that leaves the system.*

That is why the API serializes the same frozen dataclasses the templates render, and
why `StatusField` refuses `null`, blank and boolean.

### Governed reference data has one write path — `CPM-AD-14`

Package identity is corrected by **one** door: `override_identity`. It requires a
permission, requires a reason, and writes an audit row in the same transaction as the
correction. Every refusal happens before the first write, so a refused override leaves
nothing behind.

### Long work leaves the request — `CPM-AD-9`

Four kinds: an outbound call, a collector, a policy pass, an export beyond the row
cap. `tests/unit/django_apps/test_request_boundary_audit.py` walks the **import
closure** of every registered view, because the violation that actually arrives is a
view importing a helper importing a service importing the client.

The failure it prevents is a page that *hangs*, not a page that is wrong — which is
why nothing goes red when it is broken.

### Time comes from an injected clock — `CPM-AD-26`

No module calls `timezone.now()`. An audit row's instant is what an auditor reads, and
a service that reached for a wall clock would make every assertion about it a
statement about how long the test took.

## The audits are part of the design

A dozen sweeps enforce rules no requirement asked for, because **a rule that is only
written down decays**. Each names the failure it prevents rather than the rule it
applies:

| Audit | Prevents |
|---|---|
| confidence gate | a pass claiming something about a package nobody identified |
| derived-status writability | a view editing a verdict instead of re-running the policy |
| app layering | `core` importing a domain app's tables, inverting the registry |
| pagination | one endpoint returning ten thousand packages |
| permission | a surface deciding authorization for itself |
| request boundary | a page that hangs on a rate-limited third party |
| API contract | a `ModelViewSet` quietly adding a third write |
| display vocabulary | a database column reaching a reader |
| clock | a wall-clock read making an audit row untestable |

## What is deliberately *not* here

- **No soft deletes.** Evidence is append-only; nothing is removed, so nothing needs a
  flag saying it was.
- **No status recomputed on read.** A screen shows what a run concluded, not what the
  code would conclude now — otherwise two readers at different moments see different
  answers with no run to point at.
- **No per-view authorization logic.** `CPM-AD-13`: declared per surface, enforced
  centrally, in one implementation.

## Next

[A policy run, end to end](the-policy-run.md) walks one package from an upstream
request to a status on a screen.
