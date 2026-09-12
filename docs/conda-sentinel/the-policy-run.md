# A policy run, end to end

One package, from an upstream request to a status somebody reads. Each stage names the
table it writes and the rule that governs it.

## The shape

```text
1  inventory      declared source          ──▶  inventory_snapshots      CPM-AD-25
2  identity       resolution               ──▶  packages, package_mappings
3  collection     one collector, one ask   ──▶  <domain>_findings        CPM-AD-2, CPM-AD-7
4  policy run     eight passes, one cut-off──▶  package_<domain>         CPM-AD-8, CPM-AD-21
5  rollup         compose + gate           ──▶  package_health           CPM-AD-4, CPM-AD-11
6  after-run      open work                ──▶  workflow_items           CPM-AD-22
7  read           project                  ──▶  screen, API, CSV         CPM-AD-24
```

Nothing skips a stage, and no stage writes backwards.

## 1 — The inventory arrives as evidence

`CPM-AD-25`. A watchlist is not configuration: it is an observation of what the estate
contains at a moment, so it lands in `inventory_snapshots` like anything else.

**It ships empty.** A product that guessed at your inventory would be inventing the
thing every later verdict is about.

## 2 — Identity, and the gate it arms

Resolution matches a package to a source repository, a release ecosystem and a
feedstock, writing `package_mappings` — one row per mapping *attempted*, including the
ones that found nothing, because "we looked and there is nothing" is a finding
(`CPM-FR-6`).

The package row gets a **confidence**. That value arms `CPM-AD-4`: if it is
`unmapped`, every verdict at stage 5 is written as
<span class="cs-state unknown">unknown</span>, whatever the evidence says.

`resolve_package_shell` is the only creator of a package row. The identity override
(`CPM-AD-14`) corrects one and never creates one — a stale package id is refused, not
filled in.

## 3 — Collection

One collector, one upstream, one question. Eleven of them, each writing its own
evidence table.

Three properties, and each is a decision:

**They share nothing but the log** (`CPM-AD-7`). A collector that failed does not stop
the others. GitHub being down leaves currency `error` and every other domain
untouched — a single upstream cannot blank the estate.

**Evidence always inserts.** A collector that reached the transport writes a row,
always. There is no path that observes and writes nothing, because "nothing was
written" is indistinguishable from "the collector never ran".

**A collector with no freshness target refuses to start** (`CPM-AD-28`). It has to
declare how stale its own evidence may be, or the coverage screen cannot say whether
it is behind.

Every collector ships **inert** — see [Operating Conda-Sentinel](operations.md).

## 4 — The policy run

`execute_policy_run` picks one **evidence cut-off** and every pass reads at it. That
single choice is what makes a run reproducible: two passes reading at different
instants could disagree about the same package and neither would be wrong.

The eight passes run in dependency order:

```text
currency → feedstock-presence → vulnerability → licence
        → remediation → py314-readiness → work-type → priority
```

`remediation` reads the four above it — what you can act on now depends on what is
wrong. `priority` runs last because it ranks everything else.

**A pass returns its verdict; it does not write the rollup.** It writes its own derived
table, keyed `(package, policy_run)` (`CPM-AD-21`), and *returns* the status it
contributes. Composition is stage 5's, in one place.

**Passes are registered, not imported.** `policies/apps.py` registers them at
`ready()`; `core` never imports `policies`. That inversion is why a ninth pass touches
no orchestrator code — and why `test_app_layering_audit.py` exists to keep it.

**One package, one transaction** (`CPM-AD-23`). A run over ten thousand packages that
failed at nine thousand leaves nine thousand complete, correct packages — not a
half-written estate.

## 5 — Composition, and the gate

The rollup writes one row per inventory package, **always** — including packages the
product cannot speak about.

Two things happen here and nowhere else:

- **The confidence gate.** Each contributed status is passed through it. An `unmapped`
  package gets <span class="cs-state unknown">unknown</span> across the row, written
  rather than omitted.
- **Freshness is stamped per row**: `computed_at`, `evidence_cutoff`, and a map of
  `domain → rule version`. Per row, because a replay leaves rows from two runs and a
  single header would be wrong for some of them.

## 6 — After the run, work opens

`CPM-AD-22`. Findings that need a person become `workflow_items`, keyed on a **finding
key** — a stable identifier for what the item is *about*.

That key is what makes a re-run safe: the same finding does not open a second item,
and an item somebody resolved stays resolved. Without it, every nightly run would
reopen last night's work.

`core` declares the seam and `workflow` fills it at `ready()` — the same inversion the
pass registry uses, and for the same reason.

## 7 — Reading it back

Every surface calls the same projection (`CPM-AD-24`). The screen, the API and the CSV
export cannot disagree, because there is nothing else for them to read.

Statuses are emitted **verbatim**. `unknown` is `unknown` in HTML, in JSON and in a
CSV — the decision names the failure: *an export rendering `unknown` as a blank cell,
destroying the five states in the one artifact that leaves the system.*

## Replay

`CPM-FR-22`. A run can be replayed at a recorded rule version to reproduce what the
product concluded — which is what makes "why did this change?" answerable: the
evidence moved, or the rules did.

Because a replay writes new rows rather than editing old ones, current health can
legitimately hold rows from two runs at once. That is why freshness is per row, and
why every screen shows it.

## Where this shows up when something looks wrong

| Symptom | Usually |
|---|---|
| every status `unknown` | no policy run has completed, **or** identity is `unmapped` |
| one domain `unknown`, others fine | that collector has never run, or its source is undeclared |
| one domain `error` | the upstream failed — the row says when it was tried |
| a screen looks stale | check `computed_at` on the row; a replay may have left mixed runs |
| the coverage screen shows a gap | correct — that is what it is for |
