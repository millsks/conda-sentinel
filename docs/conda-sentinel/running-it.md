# Running it

From a fresh checkout to signed in and looking at real data, with **no external
service running** — no Postgres, no Redis, no identity provider.

## Five commands

```bash
pixi install                    # the environment
pixi run migrate                # SQLite, locally
# a hundred packages, their evidence, a live identity resolution, and a policy run
pixi run -e dev seed-demo
pixi run runserver              # http://localhost:8000/
```

Then open **<http://localhost:8000/>** — it redirects to `/conda-sentinel/`.

!!! warning "`-e dev` on the seeder is not optional"

    `pixi run seed-demo` refuses:

    > seed_demo_inventory writes inventory and evidence and must never run outside a
    > local run. Evidence is append-only (`CPM-AD-2`): a fictional observation cannot
    > be deleted, and every replayed policy run would read it.

    Only the dev environment declares `COMPONENT_RUNTIME=local`. The refusal is the
    product protecting an append-only log from a fixture, which is worth reading
    once.

## Signing in without an identity provider

Authentication is delegated to an OIDC provider, and you do not have one locally. The
local-dev sign-in gives you six personas instead, at
**<http://localhost:8000/_local/>**:

| Persona | Holds | Can reach |
|---|---|---|
| `leader-persona` | platform and engineering leadership | identity review; the identity override |
| `reviewer-persona` | security review | compliance review |
| `engineer-persona` | packaging engineering | remediation |
| `reader-persona` | no product role | nothing — the state the `IsAuthenticated` floor lets through |
| `staff-persona` | Django staff | the admin |
| `operations-persona` | all three roles, and Django staff | every screen, and the admin |

The three role personas reach Home, Packages, Reports and Coverage as well: those
four are open to anybody holding *a* product role, and the queues are where the roles
diverge. `reader-persona` and `staff-persona` hold no product role, so neither opens
any of them.

Each row has a **Sign in** button; one click and you are that persona. If the page
lists none, run `pixi run -e dev seed-personas` first — or `pixi run -e dev
stack-seed` if you are running the full stack, which uses a different database.

!!! note "The page labels a row by the persona's *key*"

    The table above names the **accounts** — `operations-persona`. The sign-in page
    shows the keys, so the row you click reads `operations`. Same persona, two
    spellings, and the shorter one is what is on screen.

`reader-persona` exists to be refused. Somebody signed in and holding no role is a
real state — the zero-groups sign-in — and it is worth being able to see what they
see.

`operations-persona` is the opposite: it stands for somebody running the platform
rather than working one of its queues, so it is the one sign-in that opens the whole
navigation bar. Reach for it when you want to see every screen; reach for one of the
three single-role personas when the question is whether a screen refuses the wrong
person, because signed in as operations every screen lets you in and that proves
nothing.

!!! warning "It is a local persona, not a fourth role"

    The product declares three roles and this persona holds all three of them. There
    is no operations role to provision, no fourth environment variable, and nothing
    here reaches a deployment — the local sign-in fixture is the only place it exists.

!!! note "Groups granted by hand will not survive"

    Sign-in runs `sync_authorization`, which makes the account's groups match the
    claim. A group added in the admin is erased on the next sign-in. Use a persona.

## What the seeder does, and how

It writes a hundred packages as `unmapped` shells, runs the real `resolve_identity`
collector over every one of them — live, against conda-forge's index and PyPI — and
then writes **evidence** with a real source behind each row: advisories, a KEV
cross-reference, licence findings where the roster states a licence, and a PyPI
release snapshot for each package the resolver has just found on PyPI. Then it
**runs a real policy pass** over all of it. A hundred rollup rows, at the shipped
policy version, in about half a minute online.

The distinction that matters: **it writes no derived status directly.** `CPM-AD-10`
gives the application layer no write path to one, so the seeder produces evidence and
lets the passes that own each domain conclude from it. What the screens show is
something the product actually derived — not a fixture arranged to look like one.

Its own comment puts it best:

> The step that makes the seeded screens honest: every status they show is concluded
> here, by the passes that own it, from the parameter file that ships.

It goes through the real ledger too, opening a collection run before the work and
finalising it after, so the coverage screen reads the shape it would read in
production.

### What the hundred are

A mixture, on purpose: **web frameworks** (django, flask, fastapi, tornado, litestar),
**data science** (numpy, pandas, scikit-learn, pytorch, hdbscan), the **utilities every
environment carries** (setuptools, pytest, boto3, sqlalchemy, cattrs), and seven things
conda-forge ships that are **not Python at all** (git, nodejs, cmake, ffmpeg, sqlite) —
well known and less so, because a roster of household names would not show you what an
unfamiliar package looks like on these screens.

They are chosen to put something in every state the seeded evidence can honestly
reach: advisories matched and none matched, a KEV listing and its absence, a licence
needing review and one nothing declared, and two internal packages conda-forge has no
entry for, so you can see what <span class="cs-state unknown">unknown</span> looks
like across a whole row.

A hundred rather than ten so the screens are judged as screens: at ten rows nothing
paginates, every table fits above the fold, sorting is instant on any design, and a
queue holding two items looks like a queue.

!!! tip "The advisories are real"

    Every advisory identifier, severity and affected range in the roster comes from
    **OSV.dev**, and the one KEV listing is a real entry in CISA's catalogue with the
    date the catalogue states — `git` / `CVE-2025-48384`, added 2025-08-25. Look one
    up and you will find it.

    One KEV row out of twenty-eight advisories is not a thin demo. That catalogue
    lists software known to be exploited in the wild, and almost nothing on PyPI is in
    it.

    The versions and licences beside them are what PyPI and conda-forge stated when
    the roster was written. **Nothing else in the roster is asserted.** Which
    repository a package lives in, what its purls are and whether conda-forge has a
    feedstock for it are *observed* at seed time: the seeder runs the real
    `resolve_identity` collector over every package, live against conda-forge's
    `feedstock-outputs` index and PyPI's project document, so `django` carries
    `https://github.com/django/django` because PyPI said so and the two `internal-*`
    packages are `unmapped` because conda-forge said there is no such package.

!!! note "A fresh seed needs the network, and shows `unknown` where nothing has looked yet"

    `seed-demo` and `stack-seed` reach `raw.githubusercontent.com` and `pypi.org`
    for every package — about two hundred back-to-back requests to two public hosts,
    because the seed runs the resolver **unmetered** (the daily sweep keeps the real
    allowance). A healthy seed prints

    ```
    resolved=98 not_on_conda_forge=2 unreachable=0 verified_kept=0
    ```

    `not_on_conda_forge=2` is the two `internal-*` names, which the index has no
    entry for; it is the healthy number. Anything `unreachable` is a source that
    could not be asked or that refused — a `429` from either host lands a package
    there — and that package stays `unmapped` until a **second seed** resolves it
    (which appends a second observation of everything else, as any re-seed does).
    Offline, the seed still completes: after three refused connections in a row the
    resolver stops asking, the summary reports `unreachable=100`, one warning names
    each package it asked about and one says how many it did not, and every package
    stays `unmapped` with every verdict gated
    <span class="cs-state unknown">unknown</span>. `verified_kept` counts packages a
    person has set `verified` since the last seed, which a re-seed leaves alone.

    What a fresh seed shows on first paint is exactly what has been observed: identity
    real, advisories real. The PyPI surface of currency reads
    <span class="cs-state ok">current</span> **by construction**: PyPI is the only
    surface seeded, so the authority is being compared with itself; the comparison
    means something only once another surface has been observed. Two kinds of
    `unknown` follow, and they are worth telling apart:

    - **Delayed** — observed by the stack's own sweeps, on their own cadence and
      under their own allowances. `pypi_release` is daily at 60 requests a minute,
      charged four per collection, so a sweep gets through about fifteen packages a
      minute and the rest are refused as rate-limited until the next day.
      `source_release` is daily at **60 an hour**, charged four per collection — about
      fifteen packages an hour, so the 98 resolved packages take several daily sweeps
      to cover. `feedstock` and `python_readiness` are **weekly**; their first sweep
      fires when the stack starts. Until each has reached a package, feedstock
      presence, upstream-release currency and Python 3.14 readiness read
      <span class="cs-state unknown">unknown</span>.
    - **Unreachable on the local stack at all** — published-conda currency needs
      `CPM_MONITORED_CHANNELS`, which is empty; 3.14 verification is only ever
      triggered by hand. So `not_applicable` on the readiness column, `awaiting_build`
      on remediation and priority buckets `p8`/`p9` never appear locally, whatever you
      wait for.

!!! note "One column still comes out flat, deliberately"

    The seeder reports which, and reads it off the version it ran rather than saying
    it in prose — so this stays true as versions are added.

    `license_rules` is **empty in the shipped parameter file**, so every licence comes
    out <span class="cs-state warn">manual_review</span>. That is PRD Open Question 4
    and the file says so. Record a rule set at a new version to see the column work.

    `priority_rules` was empty too until `2026.09.4` recorded ten of them (PRD Open
    Question 8), so priority buckets and scores are real from that version on. A run
    at an *older* version still produces
    <span class="cs-state unknown">unknown</span> buckets — which is the point of
    versioning the rules rather than the code.

    On a fresh seed most of the ten cannot fire anyway: every rule that conditions
    on remediation readiness, feedstock presence or Python readiness is conditioning
    on a surface nothing has observed yet, so the 28 vulnerable packages land in
    `p3` and the other 72 read <span class="cs-state unknown">unknown</span> until
    the sweeps above have run.

## Running a policy pass yourself

There is no management command for a first run, **and nothing schedules one**. The
task `cpm.policy.run` is registered and routable, but no beat entry fires it and no
sweep chains it — so a deployed component collects evidence and never computes a
verdict from it until an operator arranges the run. See
[Asynchronous work](asynchronous-work.md#two-tasks-nothing-fires) for what to do about
that; locally the seeder executes a run inline, and you can run one by hand:

```python
# pixi run -e dev python manage.py shell
from conda_sentinel.core.clock import SystemClock
from conda_sentinel.core.policy_run import execute_policy_run
from conda_sentinel.policies.parameters import parameters_file, parameters_from

source = parameters_file()
newest = sorted(parameters_from(source.read_text(encoding="utf-8"), source=source))[-1]

execute_policy_run(policy_version=newest, clock=SystemClock())
```

**Read rather than written down**, which is what the seeder does and for the same
reason: a version pinned in prose is one that stops being the newest the day somebody
records another, and this page would then be telling you to run the old rules.

The version must be one the parameter file **records**. An unrecorded version fails
every package rather than falling back to a default — a verdict whose rules nobody
wrote down is not a verdict this product will produce.

To reproduce what a past run concluded, pass its cut-off as well; see
[Operating Conda-Sentinel](operations.md#replaying-a-run).

## Why a fresh deployment sees nothing

**Every collector ships inert.** None names an upstream, a channel, a catalogue or an
advisory source. On a real deployment nothing is observed until you declare where to
look — see [Operating Conda-Sentinel](operations.md).

The seeder exists so you do not have to do that to see the product work.

## Background work

The export of a large report, and every collector and policy run, leave the request
(`CPM-AD-9`). Locally, tasks run **inline** by default — so those work with nothing
running, and you never see a worker.

To run them for real, use [the full local stack](#the-full-local-stack) below.

Without a broker, a queued job is **failed with the reason on its own page** rather
than disappearing — the row says the broker refused the connection. That is the
intended behaviour, and it is what you would see if you turned eager mode off without
starting one.

## The full local stack

The commands at the top of this page give you the product with tasks running
**inline** — no broker, no worker. That is the right default for reading screens, and it is not the
product's real shape: `CPM-AD-9` sends collectors, policy runs and large exports out
of the request, and inline execution hides every consequence of that.

For the real shape:

```bash
pixi run -e dev stack-seed    # containers, migrations, personas, demo data, live identity resolution
pixi run local-stack          # web, worker, beat, flower
```

`local-stack` brings up Redis and PostgreSQL in containers, waits for both to be
healthy, **migrates**, and then runs four processes together under
[honcho](https://github.com/nickstenning/honcho):

| Process | Is |
|---|---|
| `web` | **gunicorn**, the deployed command, on <http://localhost:8000/> |
| `worker` | drains `celery,collect,policy,verify,export` |
| `beat` | the scheduler, reading its cadences from the database |
| `flower` | the Celery monitor, on <http://127.0.0.1:5555> |

`Ctrl-C` stops all four. The containers keep running — `pixi run docker-down` stops
them, `pixi run docker-down-v` also discards their data.

If honcho ended some other way — the terminal closed, `kill -9` — its four children
outlive it: gunicorn keeps 8000, flower keeps 5555, and the next `local-stack` fails
on both. `pixi run local-stack-down` finds those stragglers by this checkout's
environment path, stops them (TERM, then KILL if a worker's warm shutdown does not
finish), and then runs `docker-down`.

!!! warning "The stack is a different database, and it has to be seeded separately"

    `migrate`, `seed-personas` and `seed-demo` run against whatever the **default**
    environment resolves, which is SQLite. The stack runs against the container's
    **PostgreSQL** on 5433. They are two databases.

    Seeding one and starting the other gives you a product with no packages and — more
    confusingly — **no personas**, so there is no way to sign in and discover that it
    is empty. That is what `stack-seed` is for: it migrates and seeds the database the
    stack is about to serve, in one command, and resolves every package's identity
    live on the way — so it needs the network, and a fresh stack shows real
    repositories and feedstocks on first paint and `unknown` for what its sweeps have
    not yet observed.

    `local-stack` migrates on its own (`depends-on`), so a fresh clone finds a schema.
    It deliberately does **not** seed: evidence is append-only, so a stack that seeded
    on every start would append a second observation of every seeded fact each time
    you restarted it.

### It uses its own containers, on its own ports

| Service | Container port | Published on |
|---|---|---|
| Redis | 6379 | **6380** |
| PostgreSQL | 5432 | **5433** |

Not the defaults, and that is a safety property rather than a preference. A developer
machine very often already has a Redis on 6379 — and this product calls
`cache.clear()`, which flushes an **entire Redis database**. A stack that talked to
whatever was already listening would be one bad afternoon from flushing another
project's cache.

So `local-stack` points at `localhost:6380` and `localhost:5433` explicitly. It will
not use your existing Redis or PostgreSQL, and it does not need you to stop them.

!!! warning "The stack's database is a different database"

    It is the PostgreSQL container, not the SQLite file the five commands above use.
    Its first run needs its own migrate and seed:

    ```bash
    pixi run docker-up
    export DATABASE_URL="postgres://conda_sentinel:local-development-only@localhost:5433/conda_sentinel"
    pixi run -e dev python manage.py migrate
    pixi run -e dev seed-demo
    ```

    That is also the more production-shaped place to work: SQLite serialises writes
    behind one lock, and a worker, beat and a web process writing at once is exactly
    where that shows.

### Docker on its own

| Task | Does |
|---|---|
| `pixi run docker-up` | start both, wait until healthy |
| `pixi run docker-down` | stop them, keep the data |
| `pixi run local-stack-down` | stop the stack's processes however honcho ended, then the containers |
| `pixi run docker-down-v` | stop them, discard the data |
| `pixi run docker-ps` | what is running |
| `pixi run docker-logs` | follow both logs |
| `pixi run docker-psql` | a `psql` shell in the container |
| `pixi run docker-redis` | a `redis-cli` shell in the container |

**`compose.yaml` brings up infrastructure only.** The application runs from the pixi
environment, because that environment *is* the runtime — putting the app in a
container as well would mean a rebuild on every edit and a second, slower way to run
what pixi already runs. `Dockerfile` is what builds the deployable image.

### Two things that make it work, and fail quietly without

**Every `Procfile` line runs `pixi run -e dev …`.** `COMPONENT_RUNTIME=local` comes
from `[feature.dev.activation.env]` and nowhere else — a task may not declare it, and
`tests/unit/test_locality_declaration.py` fails the gate on any that tries. A bare
`pixi run worker` therefore resolves in `default`, reads *deployed* settings, finds no
broker and falls back to Celery's built-in `amqp://guest@localhost:5672`. The worker
then starts, prints a banner listing the right queue names, and consumes nothing.

**`local-stack` sets `CELERY_TASK_ALWAYS_EAGER=0`.** Local settings run tasks inline by
default, which is right for `runserver` alone and would leave the worker, beat and
flower idle here while the web process quietly did their work.

Both failures look like a working stack. That is why
`tests/unit/test_local_stack.py` asserts them.

### The web process is the deployed one

`local-stack` runs `pixi run web` — gunicorn with `config.workers.DrainingUvicornWorker`,
the same line the `Dockerfile` runs. That is the point of the stack: it serves the
product through the path production serves it through, so ASGI behaviour, worker
lifecycle and shutdown draining are things you can see rather than things you assume.

Two costs, both real:

**No autoreload.** `--reload` is not on the deployed command and does not belong
there, so an edit needs a restart. For working on a template or a view, run
`pixi run runserver` on its own instead — the stack is for seeing the product's real
shape, not for a tight edit loop.

**Gunicorn is Unix-only.** The stack's `web` line does not run on Windows;
`pixi run -e dev runserver` remains the cross-platform way to serve one process, and
the worker, beat and flower lines are unaffected.

## Reading the screens

| Screen | Path | Read it when |
|---|---|---|
| Home | `/conda-sentinel/` | you want to know how current the picture is |
| Packages | `/conda-sentinel/packages/` | you are looking a package up, or working down a ranking |
| Package detail | `/conda-sentinel/packages/<name>/` | you want to know *why* a status says that |
| Queues | `/conda-sentinel/queues/<queue>/` | you are doing the work |
| Reports | `/conda-sentinel/reports/<slug>/` | a recurring question — KEV, feedstock lag, licences |
| Coverage | `/conda-sentinel/coverage/` | you want to know what the product **cannot** see |

Start at **Coverage** on an unfamiliar deployment. It is the screen that says how much
of what you are looking at is a conclusion and how much is a gap.

## The API

Same data, same projection, under `/conda-sentinel/api/v1/`. The contract is at
`/conda-sentinel/api/v1/schema/` and a browser for it at `.../docs/` — both
admin-only, both generated from the implementation.

## When something looks wrong

| Symptom | Usually |
|---|---|
| every screen empty | no policy run has completed |
| every status `unknown` for one package | its identity is `unmapped` — check the detail screen |
| one domain `unknown` everywhere | that collector has never run, or its source is undeclared |
| one domain `error` | the upstream failed; the row says when it was tried |
| a queue refuses you | it is not your role's — see the table above |
| an export never finishes | no worker, or no broker; check the job's page |
