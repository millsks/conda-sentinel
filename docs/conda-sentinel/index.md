# What Conda-Sentinel does

It watches a Conda/Python package inventory and answers five questions per package —
**is it current, is it exposed, is its licence acceptable, is it ready for Python
3.14, and is anybody maintaining its feedstock** — then ranks what to do about it.

The interesting part is not the questions. It is what happens when it cannot answer
one.

## Silence is the dangerous answer

Most monitoring tools have two: a problem, or nothing. Nothing is the one that hurts,
because it looks identical to health and is usually absence — the source was down, the
catalogue was never configured, the package was never identified at all.

This product has **five** answers and always says which it means:

| | | |
|---|---|---|
| <span class="cs-state ok">ok</span> | a verdict | the check ran and this is what it found |
| <span class="cs-state unknown">not_applicable</span> | a verdict | the question does not apply to this package |
| <span class="cs-state unknown">not_found</span> | a verdict | it was looked for and is not there |
| <span class="cs-state unknown">unknown</span> | **a gap** | nobody established anything here |
| <span class="cs-state crit">error</span> | **a gap** | something broke while trying |

`not_found` is an answer. <span class="cs-state unknown">unknown</span> is the absence
of one — and it is **written into the row** rather than left blank, so it survives
into the API, into a CSV somebody exports, and into a board pack six weeks later. A
package nobody has looked at and a package that is fine do not render the same.

!!! note "Why this matters more than it sounds"

    A coverage number that *falls* as the product learns more is measuring the wrong
    thing. Separating "we looked and it is fine" from "we have not looked" is what
    makes the [coverage screen](#the-screens) honest, and it is why
    `unknown` is a value the product asserts rather than a `NULL` it tolerates.

When several statuses have to be combined, the worst wins, in one fixed order:

```text
error  →  unknown  →  not_found  →  not_applicable  →  ok
```

An `error` anywhere in a package's evidence surfaces as `error` for the package. The
product does not average, and it does not round up.

## Evidence, then verdicts, then screens

Three stages, and each writes to tables the next only reads:

```text
collectors ──▶ evidence ──▶ policy passes ──▶ derived tables ──▶ rollup ──▶ screens
                (append-only)                   (per run)      (one row     (read-only)
                                                                per package)
```

**Collectors observe.** Each asks one upstream — GitHub, PyPI, conda-forge, an
advisory database, the CISA KEV catalogue — and writes what it saw, with a timestamp
and a source. Evidence is append-only: a later observation is a new row, never an
edit, so what the product believed last Tuesday is still recoverable.

**Policy passes conclude.** A run reads evidence at a stated cut-off and writes a
verdict per package per domain, stamped with the version of the rules that produced
it. A verdict is never edited either — a re-run writes new rows.

**Screens read.** They project the rollup and never write to it. A page cannot
disagree with the API, because both call the same projection.

The consequence worth knowing before you deploy: **nothing is derived until a policy
run happens.** A freshly seeded database shows `unknown` everywhere, correctly, until
one does — and even the local demo seed, which runs one, shows `unknown` for anything
its sweeps have not yet observed rather than a value nobody measured.

## Identity gates everything

Before any of that, a package has to be *identified*: matched to a source repository,
a release ecosystem, a feedstock. That match carries a confidence, and confidence
**gates every outward claim**.

If a package is `unmapped`, every verdict about it is written as
<span class="cs-state unknown">unknown</span> — not suppressed, not omitted, written.
An advisory that matched a *guess* at which project this is would be worse than no
advisory at all, so the product declines to claim it and says so on the row.

This is the single most surprising behaviour for a new operator: a screen full of
`unknown` usually means identity, not a broken collector. The
[coverage screen](#the-screens) counts exactly that.

## Three roles, three queues

| Role | Works | Because |
|---|---|---|
| Security review | compliance review | licence exceptions and risk acceptance |
| Packaging engineering | remediation | upgrades, rebuilds, feedstock work |
| Platform and engineering leadership | identity review | the identity override is the one governed human write |

Queue items are keyed on a **finding key** — a stable identifier for *what the item is
about* — so re-running the policy does not open a second item for the same finding,
and a resolved item stays resolved across a replay.

A queue that is not yours is **refused**, never shown empty. An empty queue says there
is no work, and a reviewer who reads that goes away satisfied.

## The screens

| Screen | Answers |
|---|---|
| Home | how current is the picture, and how much is missing from it |
| Packages | the whole estate, filtered and ranked |
| Package detail | every status on one package, traced to the evidence behind it |
| Queues | what is left to do, ranked, scoped to your role |
| Reports | six recurring questions — KEV, feedstock lag, Python 3.14, licences, unmapped identities, stale evidence |
| Coverage | what the product **cannot** see |

Everything on them is also on the [API](development.md#the-api), from the same
projection — so a number in a dashboard and a number on a screen cannot disagree.

## Where to go next

- **[Onboarding](onboarding.md) — start here.** The guided path: a sequence to work
  through over your first week, with something to run at each step. Every page below
  is a reference you dip into; that one is a curriculum.
- [How it is built](architecture.md) — the domain model and the decisions a
  maintainer has to know
- [A policy run, end to end](the-policy-run.md) — from an upstream request to a
  status on a screen
- [Identity and authorization](authorization.md) — from an OIDC claim to a refused
  queue, in seven environment variables
- [Asynchronous work](asynchronous-work.md) — the four queues, all fourteen tasks,
  and the two nothing fires
- [The queues](the-queues.md) — how work opens, who may move it, and why `blocked`
  is not a state
- [Managing the inventory](managing-the-inventory.md) — adding and removing packages,
  and correcting an identity
- [Operating it](operations.md) — what each collector needs before it observes
  anything
- [Developing it](development.md) — the screens, the API, the reports
