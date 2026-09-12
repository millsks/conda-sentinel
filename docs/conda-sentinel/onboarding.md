# Onboarding: novice to professional

This is the guided path. Every other page on this site is a reference you dip into;
this one is a **sequence**, written to be read top to bottom over your first week or
two, with something to run at each step.

It assumes you know Python and Django and have never seen this codebase. By the end
you should be able to answer, without looking anything up: *what does this product
believe, where did it learn that, when did it last check, who is allowed to see it,
and what happens if the thing it asked stops answering.*

!!! tip "How to use this page"

    Do the exercises. Every section ends with something to run and a result to
    compare against. Reading this page without a terminal open will teach you the
    vocabulary and none of the product — and the vocabulary is the easy half.

---

## Part 0 — The one-paragraph version

Conda-Sentinel watches a list of packages your organisation depends on. It asks a
handful of public sources about each one — is there a newer release, is there an
advisory, what licence does it ship, will it build on Python 3.14, is anybody still
maintaining its conda-forge recipe — and it **writes down every answer it got, with
the time it got it**. Separately, and later, it runs a versioned set of rules over
those written-down answers and produces one row per package saying what it thinks.
People then read those rows on screens, and act on them through three queues.

Two sentences carry almost all of the design:

> **What a source said is a fact, and facts are never edited.**
> **What we think about those facts is a conclusion, and conclusions are recomputed.**

Nearly every rule in this codebase falls out of keeping those two things apart.

---

## Part 1 — Day one: get it running

### 1.1 Install and start the whole thing

You need Docker running. Three commands:

```console
pixi install
pixi run -e dev stack-seed
pixi run local-stack
```

That is the **real product**: Redis and PostgreSQL in containers, then gunicorn with
the deployed worker class, a Celery worker draining all four queues, beat scheduling
the sweeps, and flower watching it. `stack-seed` brings the containers up, migrates
the database and puts a hundred packages and six personas in it; `local-stack` starts
the four processes.

!!! tip "Start with the whole stack, not just the web process"

    `pixi run -e dev runserver` starts **only** the web process, against SQLite, with
    Celery running tasks inline. Everything renders, and you learn nothing about the
    request boundary — no worker, no queue, and no job that is ever visibly *queued*.

    Use it when you are editing code and want autoreload. Use `local-stack` when you
    want to understand what you are maintaining. Note that they are **two different
    databases**: `runserver` and the plain `migrate` / `seed-demo` tasks use SQLite,
    and the stack tasks use the container's PostgreSQL. Seed the one you are about to
    run.

Then open **<http://localhost:8000/_local/>** and sign in as **`operations`** — the
one persona that reaches every screen. (There is no identity provider locally; that
page is the substitute. Each row is labelled by the persona's *key*, so the row you
want reads `operations` and the account behind it is `operations-persona`. [Why, and
what the other five are for](development.md#local-personas).)

Now open each of these and look at it:

| Screen | What it is |
|---|---|
| <http://localhost:8000/conda-sentinel/> | Home — when the picture was last computed |
| <http://localhost:8000/conda-sentinel/packages/> | Every package, one row each |
| <http://localhost:8000/conda-sentinel/coverage/> | What the monitor **cannot** see |
| <http://localhost:8000/conda-sentinel/queues/remediation/> | Work somebody has to do |
| <http://localhost:8000/conda-sentinel/reports/kev/> | One recurring question |
| <http://127.0.0.1:5555/> | flower — the worker, its queues, and what has run |

**Exercise.** On the packages screen, find a row with a red chip and click the package
name. The detail page shows you *which observation* the verdict was computed from —
its source, the instant it was seen, and whether the run cited it. That trace is the
product's whole argument for itself. If you understand that page you understand the
system.

### 1.2 What you are looking at

The hundred packages are a **fixture** — but not entirely. The advisory identifiers,
severities and affected ranges are real, taken from OSV.dev, and the single KEV
listing is a real CISA catalogue entry. Look one up. The versions on packages with no
advisory are plausible rather than observed. [What the seeder does, and
how](running-it.md#what-the-seeder-does-and-how).

Nothing on those screens was written by the seeder directly. It wrote *evidence* and
then ran the real policy engine, which is the only thing in the system permitted to
write a verdict.

### 1.3 The five words you will see everywhere

Every status column in this product draws from one vocabulary, and three of its five
members are about **the absence of an answer** rather than a bad answer:

| State | Means |
|---|---|
| `ok` (or a domain word like `current`, `matched`) | The source answered and the answer is fine |
| `not_applicable` | The question does not apply to this package |
| `not_found` | We looked, and there is genuinely nothing there |
| `unknown` | We could not establish anything |
| `error` | The lookup broke |

When two of these have to be combined, the **worst wins**, in exactly this order:

```
error  →  unknown  →  not_found  →  not_applicable  →  ok
```

The reason this matters more than it looks: a monitor whose failure mode is a green
screen is worse than no monitor. `unknown` is not a rounding error on the way to
`ok` — it is the answer, and it is the one the design protects.
[The full argument](index.md#silence-is-the-dangerous-answer).

**Exercise.** Filter the packages screen to `?vuln=unknown` and look at the rows.
Then open <http://localhost:8000/conda-sentinel/coverage/>. Coverage is the screen
that exists to make gaps *impossible to miss* rather than merely visible.

---

## Part 2 — The mental model

Read [How Conda-Sentinel is built](architecture.md) now — it is short and it is the
spine. Then come back for the parts that are easiest to get wrong.

### 2.1 Six applications, one per domain

| App | Owns | Models |
|---|---|---|
| `core` | The shared kernel: outcome states, the clock, the ledger, the rollup | `PackageHealth`, `PolicyRun`, `CollectionRun`, `BackgroundJob` |
| `identity` | Which package is which, and how sure we are | `Package`, mappings |
| `collectors` | Asking sources things, and writing down what they said | 10 evidence tables |
| `policies` | Deciding what the evidence means | 8 derived tables |
| `workflow` | The three queues, and who may move an item | `WorkflowItem` |
| `surface` | Screens and the JSON API | **no models at all** |

`surface` owning no models is not an accident. The application layer has no write
path to a derived status; it can only read what the policy engine concluded.

### 2.2 The three-stage pipeline

```
inventory  →  identity  →  collection  →  policy run  →  rollup  →  screens
             (confidence)  (evidence)     (8 passes)    (1 row/pkg)
```

Walk it once end to end: [A policy run, end to end](the-policy-run.md).

### 2.3 The confidence gate

`identity` decides how sure it is that "the `django` we mean" is the `django` a source
answered about. If it is not sure — confidence `unmapped` — then **every verdict about
that package is written as `unknown`**, not left blank and not omitted.

That is worth sitting with. The gate does not suppress a row; it *writes a value*. A
blank cell is ambiguous — it could mean "nothing to report" — and this product refuses
to produce ambiguity in the column that says how worried to be.

**Exercise.** Find `internal-telemetry-sdk` on the packages screen. Every status on it
is `unknown` and its confidence chip says `unmapped`. It is also the only reason the
identity queue has anything in it.

### 2.4 Policy is versioned, and runs are replayable

The rules live in a **parameter file**, keyed by version:
`src/django_apps/conda_sentinel/policies/data/policy-parameters.toml`. A policy run
records which version it applied, per domain. Re-running the same version against the
same evidence cut-off produces the same answer — which is what makes "why did it say
that in March" a question with an answer.

**Exercise.** Open the parameter file and read the comments above
`vulnerability_risk_order` and `license_rules`. They are the clearest example of this
codebase's habit of arguing a decision where the decision lives.

---

## Part 3 — Where the data actually comes from

Eleven collectors, eleven evidence tables, one collector per table. Nothing else in
the system may write to an evidence table — and one collector, the identity
resolver, additionally writes *identity*, through the one door `CPM-AD-14` leaves
open.

| Collector | Evidence table | Reads | Freshness target |
|---|---|---|---|
| `inventory` | `InventorySnapshot` | A reviewed CSV shipped in the wheel | 2 days |
| `source_release` | `SourceReleaseSnapshot` | `https://api.github.com/repos/…` | 2 days |
| `pypi_release` | `PyPIReleaseSnapshot` | `https://pypi.org/pypi/…` | 2 days |
| `feedstock` | `FeedstockSnapshot` | `https://api.github.com/repos/conda-forge/…` and `https://raw.githubusercontent.com/conda-forge/…` | 14 days |
| `conda_package` | `CondaPackageSnapshot` | `https://api.anaconda.org/package/…` | 2 days |
| `vulnerability` | `VulnerabilityFinding` | **an advisory source you declare** | 2 days |
| `kev` | `KevFinding` | **a KEV catalogue source you declare** | 2 days |
| `license` | `LicenseFinding` | `https://api.anaconda.org/package/…` | 2 days |
| `python_readiness` | `PythonReadinessAssessment` | `https://pypi.org/pypi/…` | 14 days |
| `py314_verification` | `PythonVerificationResult` | **a build backend you declare** | 30 days |
| `resolve_identity` | `IdentityResolutionSnapshot` | `https://raw.githubusercontent.com/conda-forge/feedstock-outputs/…` and `https://pypi.org/pypi/…` | 2 days |

Three of those say *you declare*. **They ship with no source and observe nothing until
you configure one.** That is deliberate: this component will not pick a security feed
on your behalf, and a collector that invented one would be making a supply-chain
decision for you. Until you declare them, the vulnerability and KEV columns read
`unknown` for every package — honestly.

The `inventory` collector is also unpopulated on purpose: `watchlist.csv` ships with
a header and no rows, and ingestion **fails loudly** until you review packages in.

Read [Operating Conda-Sentinel](operations.md) for the per-collector detail — what
each one asks for, what it does when rate-limited, what its `User-Agent` says, and
what each of its refusals means. It is long because it is the reference; do not read
it end to end now.

**Exercise.** Run `pixi run -e dev python manage.py shell` and:

```python
from conda_sentinel.core.registry import registered_collectors

for collector in registered_collectors():
    print(collector.name, collector.evidence_model.__name__, collector.freshness_target)
```

The table above came from that. If it ever disagrees, the code is right.

---

## Part 4 — What runs, and when

Four process types. In production the platform runs them from
[`component.toml`](maintaining-it.md); locally one command runs all four.

| Process | Command | What it does |
|---|---|---|
| `web` | `pixi run web` | gunicorn + a draining uvicorn worker. Serves screens and the API. |
| `worker` | `pixi run worker` | Drains `celery,collect,policy,verify,export`. |
| `beat` | `pixi run beat` | The scheduler. **Exactly one replica, ever.** |
| `flower` | `pixi run flower` | A monitor, bound to `127.0.0.1` only. |

```console
pixi run local-stack
```

brings up Redis and PostgreSQL in Docker and then runs all four together.
[What that costs and why it uses non-default ports](running-it.md#the-full-local-stack).

Then read [Asynchronous work](asynchronous-work.md) — the four queues, all fourteen
tasks, what beat actually fires, and the two tasks **nothing fires**, which is the
single most surprising thing in this system.

**Exercise.** You started this in Part 1. Read the worker's banner in that terminal
— it names the queues it is consuming and, one line above them, its transport:

```
- ** ---------- .> transport:   redis://localhost:6380/0
 -------------- [queues]
                .> celery           exchange=celery(direct) key=celery
                .> collect          exchange=collect(direct) key=collect
                .> export           exchange=export(direct) key=export
                .> policy           exchange=policy(direct) key=policy
                .> verify           exchange=verify(direct) key=verify
```

If that transport line ever reads `amqp://guest@localhost:5672`, the worker has
resolved the wrong settings: it will start cleanly, print exactly the banner above,
and consume nothing, because it is connected to a RabbitMQ that is not running. That
took a session to diagnose once, with every test green.

!!! warning "Learn this one early"

    **Nothing fires `cpm.policy.run` or `cpm.collect.inventory`.** Both are registered
    and routable; no beat entry and no chained call enqueues either. A deployed
    component collects evidence on schedule and never computes a verdict from it until
    an operator adds a periodic task. It is the single most surprising fact in the
    system and it is not a bug — [why, and what to do about
    it](asynchronous-work.md#two-tasks-nothing-fires).

---

## Part 5 — Who is allowed to see what

Read [Identity and authorization](authorization.md) in full. It is the shortest of
these pages and the one where a mistake is a security bug rather than a wrong number.

The one-line version: an identity provider asserts group memberships; those groups are
mapped to **three product roles**; every screen and endpoint declares which roles it
requires; one central mechanism enforces it. Nothing invents its own check.

**Exercise.** Sign in as `reader-persona` and try to open
<http://localhost:8000/conda-sentinel/packages/>. You should be refused. Now read the
refusal message — it names the *requirement*, not your memberships, which is the thing
you can actually take to an administrator.

---

## Part 6 — The work: three queues

Read [The queues](the-queues.md).

The short version: after a policy run concludes, a step opens a queue item wherever a
verdict says there is work — a matched advisory becomes remediation work, an uncleared
licence becomes compliance work, an unidentified package becomes identity work. Items
are keyed, so re-running the policy every night does not open a second copy of
anything.

The thing to know early: **moving an item is an API operation, not a screen.** The
queue pages are read-only today.

---

## Part 7 — Changing what is watched

Adding or removing a package is a **pull request against a CSV**, not an admin action.
[Managing the inventory](managing-the-inventory.md) covers it, including why removal
does not delete anything and what "departed" means.

---

## Part 8 — Operating it

By now you can read the screens. Operating it is knowing what to check and what a
failure looks like.

| Question | Where you look |
|---|---|
| Is the service up? | `/livez` and `/readyz` |
| Why does Coverage say every collector has *never run*? | Expected on a fresh stack — [beat's first fire is one interval away](asynchronous-work.md#a-running-beat-does-not-mean-anything-has-run) |
| Is anything not being collected? | The **Coverage** screen |
| Did last night's sweeps run? | Coverage, and the run ledger |
| Is the queue backing up? | flower |
| Why does this package say that? | The package detail page |
| Why did it say that in March? | Replay: `pixi run manage replay_policy_run` |

The recurring lesson from this codebase, written down because it keeps being
relearned: **render it and run it.** Most defects found in this product were found by
opening the page or starting the process, not by a test. A green suite told us a label
was right when it was wrong, and told us a worker was consuming a queue it was not
connected to.

---

## Part 9 — Changing the code

Read [Maintaining it](maintaining-it.md) and [Developing](development.md).

Three rules that will bite you first:

1. **Pixi is the only runner.** Never `python`, `pytest`, `pip` or `uv` directly.
2. **`pixi run ci` must exit 0**, and every change carries a test.
3. **The audits are part of the design.** A dozen test modules sweep the codebase for
   a *class* of mistake rather than a specific bug. When one blocks you, it is usually
   right; read the failure message, which names the failure it prevents rather than the
   rule it enforces.

---

## Graduation checklist

You are through the primer when you can do all of these without looking them up:

- [ ] Explain why `unknown` is a first-class answer and not a missing value
- [ ] Name the five outcome states and their precedence order
- [ ] Say what the confidence gate does — and why it *writes* rather than hides
- [ ] Point at the file that decides what counts as a licence problem
- [ ] Name the four Celery queues and say why they are four and not one
- [ ] Say which two tasks nothing fires, and how you would fire them
- [ ] Trace a person from an OIDC claim to being refused a queue
- [ ] Add a package to the watchlist and say what happens on the next sweep
- [ ] Move a queue item from `open` to `resolved` and name every step
- [ ] Explain to somebody else why an export must never disagree with the screen
