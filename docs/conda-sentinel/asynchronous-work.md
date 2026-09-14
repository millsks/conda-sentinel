# Asynchronous work: Celery, beat, and every task

What runs outside a request, what schedules it, and what happens when it fails.

---

## Why anything is asynchronous at all

`CPM-AD-9` splits the product at the request boundary: **a request may not do work
whose duration depends on a source, an inventory size, or a build.** Asking ten
thousand packages about their advisories is not a request; neither is producing a
CSV of a ten-thousand-row report.

So a request that needs such work **hands it off and returns something to look at** —
a job row it can point back at. A boundary a request can cross but not point back
across is one where work simply disappears.

---

## The processes

| Process | Command | Replicas | Notes |
|---|---|---|---|
| `web` | `pixi run web` | many | gunicorn + a draining uvicorn worker |
| `worker` | `pixi run worker` | many | consumes `celery,collect,policy,verify,export` |
| `beat` | `pixi run beat` | **exactly one** | the scheduler |
| `flower` | `pixi run flower` | dev only | monitor, bound to `127.0.0.1` |

!!! danger "`beat` is one replica, and the deployment enforces it"

    A second beat replica double-enqueues every periodic task. `component.toml`
    fixes the count at one and stops the old process before starting the new, rather
    than replacing it rolling — because a rolling replacement *is* a second replica
    for the length of the overlap.

Locally, `pixi run local-stack` runs web, worker, beat and flower together with Redis
and PostgreSQL in Docker. [What it costs and why the ports are
non-default](running-it.md#the-full-local-stack).

Redis is the broker **and** the result backend. A database broker was considered and
rejected: it needs a second ORM beside Django's against the same database, it polls
rather than being pushed to, and the full-inventory sweep enqueues one task per
package against ten thousand.

---

## Four queues, because there are four workload classes

| Queue | Work | Shape |
|---|---|---|
| `collect` | Asking sources things | I/O-bound, rate-limited, slow |
| `policy` | Running the policy engine | CPU- and database-bound |
| `verify` | Building a package against Python 3.14 | Very slow, needs a build backend |
| `export` | Producing a CSV a person asked for | Database-heavy, latency-tolerant |

Plus the inherited default queue, `celery`, which is where anything not named `cpm.*`
lands.

They are four because they are **four things you would size and scale differently**.
A ten-thousand-row export sitting in `policy` would delay the nightly sweep behind
somebody's download; a Python 3.14 build in `collect` would occupy a worker for
minutes while advisories waited.

**Routing is by task-name namespace, derived rather than written out:**

```
cpm.collect.*  →  collect
cpm.policy.*   →  policy
cpm.verify.*   →  verify
cpm.export.*   →  export
```

There is deliberately **no catch-all route and no default-queue override**, so a task
that declares no `cpm.` name keeps landing on the inherited default rather than being
captured.

---

## Every task

Fourteen, and the name is the routing:

| Task | Queue | What it does |
|---|---|---|
| `cpm.collect.inventory` | `collect` | Ingests the inventory -- the governed table, or the watchlist file; creates package shells and inventory snapshots |
| `cpm.collect.sweep` | `collect` | **The dispatcher.** Takes a collector name, selects the packages it can be asked about, enqueues one per-package task each |
| `cpm.collect.source_release` | `collect` | One package's upstream releases |
| `cpm.collect.pypi_release` | `collect` | One package's PyPI releases |
| `cpm.collect.feedstock` | `collect` | One package's conda-forge feedstock and recipe |
| `cpm.collect.conda_package` | `collect` | What the declared channels publish for one package |
| `cpm.collect.vulnerability` | `collect` | Advisories matching one package |
| `cpm.collect.kev` | `collect` | Whether those advisories are in the KEV catalogue |
| `cpm.collect.license` | `collect` | One package's declared licence |
| `cpm.collect.python_readiness` | `collect` | Static Python 3.14 assessment from metadata |
| `cpm.collect.resolve_identity` | `collect` | Resolves one package's mappings from conda-forge's feedstock-outputs index and PyPI, and records them through `identity` |
| `cpm.verify.py314_build` | `verify` | Actually builds one package against 3.14 |
| `cpm.policy.run` | `policy` | One policy run over the whole inventory |
| `cpm.export.run` | `export` | Produces one report export a person asked for |

Every per-package task takes `package_id` and a keyword-only `force`, which bypasses
the observation window for a manual recollection. Keyword-only so a caller can never
trigger a forced run by getting an argument's position wrong.

**The atomic unit is one package.** No task holds the inventory. That is what makes a
partial failure partial: one package failing leaves every other package's collection
untouched, and the run's ledger row says `partial` rather than losing the ones that
worked.

---

## What beat actually fires

Nine entries, one per **per-package** collector, each firing the one dispatcher:

| Entry | Collector | Every | Offset |
|---|---|---|---|
| `cpm-sweep-resolve-identity` | `resolve_identity` | 1 day | — |
| `cpm-sweep-source-release` | `source_release` | 1 day | — |
| `cpm-sweep-pypi-release` | `pypi_release` | 1 day | — |
| `cpm-sweep-conda-package` | `conda_package` | 1 day | — |
| `cpm-sweep-vulnerability` | `vulnerability` | 1 day | — |
| `cpm-sweep-kev` | `kev` | 1 day | +1 hour |
| `cpm-sweep-license` | `license` | 1 day | +2 hours |
| `cpm-sweep-feedstock` | `feedstock` | 7 days | — |
| `cpm-sweep-python-readiness` | `python_readiness` | 7 days | +3 hours |

The three offsets exist for different reasons — KEV reads what the vulnerability
collector wrote, and the other two share a host with a sweep on the same tick.
The identity resolver carries none, deliberately: the four sweeps that select on
the mappings it records — `source_release`, `pypi_release` and `feedstock` on the
same tick, `python_readiness` three hours later — select nothing it has not yet
resolved, so on the first day they select nothing and on the next they select
everything it resolved. A one-cadence lag for the three, and the same lag or a
same-day catch-up for `python_readiness` depending on how fast the resolver's
tasks drain, stated here rather than closed with a fourth offset.
[The full argument, and what the offsets do not
buy](operations.md#the-schedule-is-data-and-it-is-reconciled-against-the-collectors-at-start-up).

Each entry's interval is reconciled at start-up against the cadence its collector
declares, **in both directions**. A mismatch, an entry naming an unregistered
collector, or a freshness target that is not strictly greater than its cadence is an
`ImproperlyConfigured` and the process does not start. Without that check, a weekly
schedule against a daily-derived target would make the whole inventory read stale five
days out of seven with every gate green.

### A running beat does not mean anything has run

The nine entries above are **intervals, not clock times**. `django_celery_beat` gives a
new entry a `last_run_at` of "now" when it first registers it, so **the first fire is one
whole interval later** — a day for the seven daily sweeps, a week for the two weekly ones.

So on a stack you started a minute ago:

```
cpm-sweep-source-release   enabled=True  interval=every 86400 seconds  last_run=None  total_runs=0
cpm-sweep-vulnerability    enabled=True  interval=every 86400 seconds  last_run=None  total_runs=0
…
```

Everything is healthy, and the entries have not ticked. **What fires at start is the
`CPM_SWEEP_ON_BEAT_START` switch**, which the `dev` pixi environment turns on and
nothing production-bound does: when beat starts with it on, a receiver on Celery's
`beat_init` signal enqueues one `cpm.collect.sweep` per entry in the table above, in
that order and with each entry's own options — so `kev` arrives one hour in,
`license` two and `python_readiness` three, and the other six at once. The nine entries
themselves are untouched; their first tick is still one interval away. The receiver
reads `CELERY_BEAT_SCHEDULE` rather than the scheduler's tables because the
`DatabaseScheduler` rewrites those tables from settings on every beat start, so an
entry disabled in the admin does not survive a restart either way.

On the stack, then, the Coverage screen stops saying **never run** for all nine
swept collectors — a dispatch row exists whether or not the selection was empty —
six of them within the first minute and the three phased ones one, two and three
hours in. What stays **never run** on a fresh stack is `inventory`, until
`pixi run stack-run ingest_inventory` or the `ingest` admin process runs, and
`py314_verification`, always, because it is triggered by hand and never swept. The
only other runs in the ledger are the seeder's, filed under `local-dev-demo-seed`,
which is deliberately not a registered collector name so that seeding cannot make a
real collector look healthy.

Restart beat the same day and the nine are enqueued again. That is harmless rather
than a second sweep, and it is not free: the ledger gains nine more dispatch rows,
and under each one a `skipped` collection row per package still inside that
collector's observation window — the dispatcher's overlap guard covers only a
previous dispatch that is *still draining*; a finished one is re-offered whole and
each collection then skips itself. One narrow edge is documented rather than
guarded: a restart within seconds of a start dispatch, with two workers, can have
both dispatches see the other `running` and both record `skipped`, in which case
that collector waits for its tick. Deployed, the switch is **off** — the reason is
the comment on `CPM_SWEEP_ON_BEAT_START` in `src/config/settings/base.py` — so a
fresh deployment's first sweep is still one interval after its first beat, or one
`pixi run sweep` by hand. A bare `pixi run -e dev beat` outside the stack refuses the
start dispatch with one logged event: under the local default
`CELERY_TASK_ALWAYS_EAGER=1` the beat process has no worker to hand a sweep to, and
would otherwise run nine collectors inline inside the scheduler.

To confirm the worker actually works without waiting for any of that, enqueue something by hand,
through a command that carries the stack's environment — `pixi run stack-run`, see
[Running it](running-it.md#a-shell-against-the-stack). A bare `manage.py shell` talks to SQLite
and runs the task inline — `src/config/settings/local.py:121` defaults
`CELERY_TASK_ALWAYS_EAGER` to true — so nothing would reach the worker:

```sh
pixi run stack-run run_policy
```

That is the `run_policy` management command: it reads the newest version the
parameter file records (`--version 2026.09.1` names another; an unrecorded one is
refused before anything is enqueued), calls `cpm.policy.run`'s `.delay()` with it,
and prints the task id. A policy run is the right thing to send: it is pure
computation over evidence that is already there, it makes no outbound call, and it
is one of the two tasks nothing fires anyway. Watch it in flower, then look at the
home page's "rollup computed" stamp.

### Making the collectors actually run

Waiting a day is one option. The other is to enqueue a dispatch yourself:

```sh
pixi run stack-run dispatch_sweep pypi_release            # one collector
pixi run stack-run dispatch_sweep pypi_release feedstock  # several, in this order
pixi run stack-run dispatch_sweep --all                   # every swept collector
```

`dispatch_sweep` enqueues one `cpm.collect.sweep` per name — the same task beat
fires — and prints one line per collector with the task id. It refuses a name the
dispatch would refuse (blank, the reserved `sweep`, unregistered, one that is not
swept per package such as run-scoped `inventory`, or one whose per-package task
Celery does not hold) *before* anything is enqueued, listing the swept names, so
a typo is not a `failed` run on the ledger. `--all` is every registered collector
that is swept per package, in the registry's order — the same set the beat table
above fires, which the suite reconciles.

**On a component you have configured with a real watchlist, that is the answer.** Each
dispatch selects the packages its collector can be asked about, enqueues one
per-package task each, and the Coverage screen stops saying `never run` for it.

On a *demo* component it is mostly not, and it is worth knowing why before you try it:

| Collector | Against the seeded demo inventory |
|---|---|
| `inventory` | **Works, and creates nothing new.** `stack-seed` imported the development watchlist into the `inventory` table and ingested it, and the stack reads that table (`CPM_INVENTORY_SOURCE=database`), so `pixi run stack-run ingest_inventory` appends one snapshot per package and records nothing absent. Retire a row on `/conda-sentinel/inventory/` first and the next ingestion records that package `not_found` |
| `resolve_identity` | **Already ran, at seed time, for all 148.** `stack-seed` runs it inline over every package the watchlist names against the real conda-forge index and the real PyPI project document, so a fresh seed already carries the repository, purls and feedstocks each source stated — and a `not_found` row for the two `internal-*` packages conda-forge has no entry for. The daily sweep then re-offers all 146 `inventory-derived` packages at once, and under the real allowance (60 requests a minute, charged four per collection) about fifteen get through and the rest are refused as rate-limited — they show as `failed` runs on the Coverage screen that day, which is expected rather than a fault, and each is offered again the next day. The ones that do run read the same documents and record nothing new |
| `source_release`, `pypi_release`, `feedstock`, `python_readiness` | Genuinely work, because the mappings they select on were observed rather than seeded: `source_release` reads the repository PyPI named, `feedstock` reads the feedstock conda-forge listed. These are the sweeps that turn the `unknown` a fresh seed shows for upstream currency, feedstock presence and readiness into real verdicts |
| `conda_package`, `license` | **Work on the stack.** Both read `CPM_MONITORED_CHANNELS`, which `config/settings/local.py` declares as `conda-forge` (on `noarch` and `linux-64`) for every local run and which stays empty deployed. Each offers all 148 at once, and the allowances set the pace: `conda_package` is 30 requests a minute, which at the `1 + retries` the base charges is about seven packages a minute, and `license` (30 a minute, charged two per collection) about fifteen; the rest are refused as rate-limited and show as `failed` runs on the Coverage screen that day, which is expected rather than a fault, and each is offered again by the next sweep. The rows that are written name `conda-forge` — one per platform for `conda_package`, `not_found` on the platform a package has no file for, and the currency pass reads the `ok` one |
| `vulnerability`, `kev`, `py314_verification` | Observe nothing. Each needs a source you declare |

So a sweep over the demo gives you a Coverage screen that is honest for the
collectors that ran and `never run` for the ones that need a source you have not
declared — and an evidence log in which every row names a source somebody can open.

!!! danger "A collector sweep writes permanent observations"

    Every one of those rows writes **real observations into an append-only log**, on
    top of the seeded evidence you were looking at. `CPM-AD-2` means none of it can be
    taken back: what a `source_release` sweep records about `django`'s repository
    today is permanent, and every replayed policy run reads it. That is the product
    working as designed — the seed no longer plants anything a sweep would contradict
    — but it is worth knowing before you trigger one by hand that you are adding to
    the record rather than refreshing it.

    If you want to see collection work over an inventory of your own, the honest way
    is a **real watchlist** — even a three-row one — imported into the table
    (`pixi run stack-run import_watchlist your.csv --replace`) and an ingestion
    (`pixi run stack-run ingest_inventory`, which enqueues `cpm.collect.inventory`)
    before any sweep. If you only wanted to prove the worker is alive, run
    `pixi run stack-run run_policy` instead: it computes, and it writes no evidence
    at all.

### Two tasks nothing fires

!!! warning "This is the most surprising thing in the system"

    **`cpm.policy.run` and `cpm.collect.inventory` are registered, routed and
    runnable — and nothing enqueues either of them on a schedule.** There is no beat
    entry and no chained call at the end of a sweep. What there is, since
    `CPM-OPERATE-S02`, is a management command for each — `run_policy` and
    `ingest_inventory` — and an `[[admin_processes]]` entry in `component.toml`
    (`policy-run`, `ingest`) that a deployment repository can put on a cadence.

    So a freshly deployed component collects evidence on schedule and **never
    computes a verdict from it**, until an operator arranges for the policy run to
    happen — by scheduling `pixi run policy-run`, or by running it by hand.

The reason it is like this rather than broken: cadence is data. `django_celery_beat`'s
`DatabaseScheduler` rewrites the nine entries above from settings on every beat start
— those nine are a *declaration*, and changing one is a pull request. But a schedule
entry that settings does not declare lives in the database tables and survives. So the
intended path is:

**Schedule the admin process** — `pixi run policy-run` (`run_policy` at the newest
recorded version) at whatever cadence your collection schedule makes sensible, after
the daily sweeps have had time to land, and `pixi run ingest` (`ingest_inventory`) at
the cadence you re-read the watchlist. The cadence is the deployment repository's, as
it is for `prune`; `component.toml` declares only that the processes exist. The other
route, a periodic task added in the Django admin (`Periodic Tasks` → add) naming
`cpm.policy.run` with the version as its argument, still works — but it is a version
pinned in a database row, which stops being the newest the day somebody records
another.

Locally you do not need to: `seed-demo` executes a policy run inline, and you can run
one by hand. [How](running-it.md#running-a-policy-pass-yourself).

---

## Handing work off from a request

The one path a *person* triggers: exporting a report bigger than the synchronous cap.

```
POST /conda-sentinel/reports/<slug>/export/
   │  creates a BackgroundJob (state: queued)
   │  enqueues cpm.export.run on commit
   ▼
redirect to /conda-sentinel/exports/<id>/
   │  the page polls while the job is running and stops when it is not
   ▼
GET /conda-sentinel/exports/<id>/download/
```

Four job states: `queued`, `running`, `succeeded`, `failed`. `BackgroundJob` is the
**only** table in this product that is not append-only — a job is not evidence about
anything, it is work with a lifecycle, and "is this done" should be a read rather than
a query. It keeps three timestamps, which is what an operator asking why something took
four minutes actually reads.

`GET` on that URL streams the file directly when the report is under the cap; `POST`
is the asynchronous path. The split is deliberate: **a `GET` that enqueued work would
let a bookmark, a prefetch or a link checker create jobs.**

Over the cap the export is **refused, never truncated** — 409, naming the path that
will produce it. `CPM-APP-S06` shipped a truncated file with a header saying so, and
that was replaced, because a CSV in somebody's downloads folder outlives the response
header that qualified it.

### Two failures this path taught us

**An unreachable broker used to 500 the request and leave a phantom `queued` job.**
`on_commit` fires after the commit, so the row was already there when the enqueue
failed. It now catches the broker's operational error and fails the job with the
reason on its own page — which is the page the person was just sent to.

**The result backend broke the hand-off under eager execution.** Export tasks declare
`ignore_result=True`: the job row is the result, and a second copy of it in Redis was
an inconsistency waiting to happen.

---

## When something goes wrong

| Symptom | Look at |
|---|---|
| Nothing is being collected | Coverage screen; then whether `beat` is running at all |
| A collector is stale | Its run in the ledger, and its freshness target |
| The worker starts but consumes nothing | Its banner — see below |
| A job is stuck `queued` | flower; is a worker draining `export`? |
| A sweep ran but enqueued nothing | The dispatch's log — it names what it refused and why |

!!! bug "The worker banner is worth reading every time"

    A worker resolving the wrong settings falls back to Celery's built-in
    `amqp://guest@localhost:5672`. It **starts cleanly**, prints a banner listing the
    right queues, and consumes nothing — because it is connected to a RabbitMQ that
    is not running.

    ```
    - ** ---------- .> transport:   redis://localhost:6380/0
    ```

    That line is the check. It was `amqp://` once, and the tests were all green.

Locally, Celery runs **eagerly** by default: tasks execute inline in the calling
process, so the product works with nothing running. That is the right default for
reading screens and it hides every consequence of the request boundary — a developer
never sees a worker, never sees a queue, and never sees what a job looks like while it
is still queued. `pixi run local-stack` sets `CELERY_TASK_ALWAYS_EAGER=0` and is how
you see it work for real.
