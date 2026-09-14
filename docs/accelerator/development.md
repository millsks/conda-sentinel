# Developing on the platform

The service platform this component was built from: how to run it, how to change
it, and what the harness checks before a change is allowed to land.

For Conda-Sentinel's own surfaces — the theme, the request boundary, the API and
the reports — see [developing the product](../conda-sentinel/development.md).
## Environment

Dependencies are declared in `pixi.toml` and resolved from **conda-forge**.
`pyproject.toml` carries build metadata and tool configuration only — it does
not declare dependencies.

The build backend is **hatchling**, with **hatch-vcs** deriving the version
from git tags. `[project]` therefore declares `dynamic = ["version"]` and has
no hardcoded version; `django_service.__version__` reads it back from the
installed distribution metadata. With no tag reachable the version resolves to
a development version such as `0.0.1.dev6+g<sha>`; tag a release (`v0.1.0`) to
get a clean one. `fallback-version` covers shallow CI clones with no tags at
all.

```sh
pixi install         # create the runtime environment
pixi install -e dev  # add the development toolchain
pixi run bootstrap   # install the git hooks
```

There are three environments. **`default`** holds runtime dependencies only —
Django, Celery, uvicorn and so on — and is what a production image would
install. **`dev`** layers the toolchain (ruff, mypy, pytest, mkdocs, git-cliff)
on top. **`spike-storage`** is `dev` plus `django-storages`, and exists only to
run R-1's fitness spike — see [Object storage fitness (R-1)](#object-storage-fitness-r-1);
it goes away when Epic 7 has acted on the verdict. All three share a
solve-group, so packages common to them resolve to identical versions.

There are two spikes under `tests/spikes/`, both named `spike_*.py` so the
gate never collects them, both marked `spike`, each with its own task:
`pixi run spike-storage` (R-1, above) and `pixi run spike-scale`
(`CPM-OPERATE-S10`: seeds a throwaway `postgres:17` with ten thousand packages
times ninety-one days on every purged evidence table, the ledger and every
policy run's derived rows, `EXPLAIN (ANALYZE, BUFFERS)` over the product's own
queries, the nightly purge end to end; needs Docker, takes about seven and a
half minutes, copies its run to `.spike-runs/`; verdict and numbers in
`docs/conda-sentinel/operations.md`, "Measuring at scale"). The second runs in
the `dev` environment, because it needs nothing the runtime set lacks — what
keeps it out of the gate is its name and its container, not its environment;
its seeder alone runs in the gate at three packages by two days.

**You never need `-e` for a task.** `pixi run <task>` resolves without a flag
and without prompting. `pixi task list` shows each task with its description.

Two different mechanisms deliver that, and the difference matters as soon as a
new environment is added:

- **Every task that declares a `cmd` pins `default-environment`.** That is what
  keeps it unambiguous however many environments carry the feature declaring it.
  `tests/unit/test_gate_contract.py::test_every_task_with_a_command_pins_its_environment`
  holds it for all of them, not just the ones the gate runs.
- **`ci` cannot pin one, and does not.** pixi rejects `default-environment` on a
  task that declares only `depends-on`. So `ci` stays unambiguous the only other
  way available: the feature that declares it — the dependency-free `gate`
  feature — belongs to **exactly one** environment. That rule is written beside
  `[feature.gate.tasks]` in `pixi.toml` and asserted by
  `test_the_gate_task_is_reachable_from_exactly_one_environment`.

The second rule is not decoration. `spike-storage` layers the `dev` *feature*,
which made `ci` visible from two environments and aborted `pixi run ci` with
`the task 'ci' is ambiguous` before a single step ran. Epic 8's six-environment
matrix is the same change six times over, so a new environment that carries the
`dev` toolchain must not also carry `gate`.

Operational commands — `manage`, `migrate`, `collectstatic`, `createsuperuser` —
run in `default`, because a deployment runs them too. `serve` is **not** among
them: a deployment runs `pixi run web`, and `serve` is the developer's
cross-platform ASGI server — see
[Process model](deployment.md#process-model) for what a deployment actually
invokes. Development-only commands — `runserver`, `serve-reload`,
`makemigrations` — and the whole quality
harness run in `dev`. That partition is now **load-bearing rather than
incidental**: it is what carries locality — see
[Locality is declared by the environment](#locality-is-declared-by-the-environment)
below.

An *ad-hoc* command still needs the flag: `pixi run -- pytest` would use
`default` and fail on the missing test dependencies. Use `pixi run -e dev --`.

### Locality is declared by the environment

A component is **deployed** unless it says otherwise. The declaration is
`COMPONENT_RUNTIME=local`, and it lives exactly once — in
`[feature.dev.activation.env]` in `pixi.toml`, beside `DJANGO_DEBUG_APPS`. It is
committed, so a freshly cloned component is local from the first command and no
untracked file has to be created to make it so. `src/config/locality.py` is the
one place that reads it.

**Every developer path inherits it, because every developer path runs in `dev`.**
`runserver`, `serve-reload`, `makemigrations`, `test`, `test-integration`,
`test-cov`, `typecheck`, `precommit`, `ruff-report` and `spike-storage` all
resolve to a `dev`-carrying environment, and so do the ad-hoc routes above —
`pixi run -e dev -- <cmd>` and `pixi shell -e dev` both activate the same env
and are local too. There is no list of "local tasks" to maintain.

**The `default` environment declares nothing and therefore reads *deployed*.**
That is what the golden base runs, and it is what the release stage invokes:
bare `pixi run migrate` and `pixi run collectstatic` resolve in `default` and
correctly read deployed. It also leaves the deployment platform's own
configuration — an OpenShift configmap or equivalent — in sole control of the
variable, because nothing in `default` overrides it.

**The one thing to know as a developer:** the operational commands in `[tasks]` —
`manage`, `migrate`, `collectstatic`, `createsuperuser`, `serve`,
`seed-personas`, `mint-token` — live in `default`, so run them as
`pixi run -e dev migrate` (and so on) when you want them to behave locally. Bare
`pixi run migrate` is the *deployed* invocation, and that is deliberate: it is
the one the release stage uses. `seed-personas` and `mint-token` are the two that
*refuse* rather than merely behaving differently — see
[Local personas](../conda-sentinel/development.md#local-personas).

**Absent or unrecognized means deployed.** Locality fails closed on purpose:
local development is the exception that must declare itself, so a declaration
lost anywhere between here and production leaves the refusals built on it armed
rather than disarmed. The reader normalizes before it matches, so `LOCAL`,
`Local` and `" local "` all read as local; `dev`, `1`, `true` and an empty value
do not. The manifest is held to the single canonical spelling `local` by
`tests/unit/test_locality_declaration.py`, which is stricter than the reader on
purpose.

**No task declares it, and it must never reach `[activation.env]`.** A task's
`env` *overrides* the caller's environment, so a task-level
`COMPONENT_RUNTIME=local` on `migrate` could not be corrected by the platform's
configmap and would make the production release stage read *local*. The unscoped
`[activation.env]` is worse still: the golden base runs pixi, so that table is
evaluated in production. `COMPONENT_PROCESS` — which Epic 5's `web`, `worker`
and `beat` tasks set in their own `env` — is banned from *every* activation env,
feature-scoped ones included, because there it would make every management
command declare itself a serving process and deadlock the release stage on the
migrations refusal. `tests/unit/test_locality_declaration.py` asserts all of
this over the parsed `pixi.toml`, platform-scoped tables included, and asserts
that the declaration is in force in the running process rather than merely
written down.

### Debug apps

`django-debug-toolbar` and `django-extensions` ship only in the `dev` feature,
so `config/settings/local.py` gates them behind `DJANGO_DEBUG_APPS`:

```python
DEBUG_APPS = env.bool("DJANGO_DEBUG_APPS", default=False)
```

`[feature.dev.activation.env]` sets it to `True`, so the toolbar is on in `dev`
and absent everywhere else. Without this gate the local settings import
`debug_toolbar` unconditionally and Django cannot start in the runtime
environment at all.

`hatchling` and `hatch-vcs` are the exception — they sit in `[dependencies]`
rather than the dev feature, because `[pypi-options] no-build-isolation`
requires the build backend in whichever environment installs the editable
package, including the runtime-only `default`.

The project pins **pixi 0.70.2**: `requires-pixi = ">=0.70.2"` in `pixi.toml`
sets the local floor, and every workflow passes `pixi-version: v0.70.2` to
`setup-pixi`. `pixi.lock` is lock-file format v7, which pixi 0.67.x cannot
read at all, so the floor is a hard requirement rather than a preference.

Every dependency resolves from conda-forge. `pixi.lock` holds exactly one
package-index entry — the editable install of this project itself. See
[Supply chain](#supply-chain) below.

## Supply chain

**conda-forge is the single channel.** `[workspace] channels` names it and
nothing else, and every third-party package the environment installs comes from
there. Adding a second channel changes what the project trusts, so it is a
deliberate change rather than one more word in a list.

**`[pypi-dependencies]` carries the editable self-install and nothing else.**
The one entry, `conda-sentinel = { path = ".", editable = true }`, is how
the source tree reaches the environment — it is not a supply-chain exception. A
third-party package appearing there *is* one, and it needs its reasoning and an
exit condition recorded beside it in `pixi.toml`. The project carries **zero**
exceptions today: the one it used to have, `django-celery-beat`, was fixed
upstream and now resolves from conda-forge like everything else.

**Transitive availability is not declaration.** A package the code imports
directly is declared directly, even when something else already pulls it in.
`django-timezone-field`, `python-crontab` and `cron-descriptor` are declared
although `django-celery-beat` requires them; `pyyaml` is declared although it
already reaches the environment behind pre-commit. Otherwise the dependency
survives only for as long as the other package happens to want it.

**Reasoning lives beside the configuration it constrains.** A declaration whose
presence is not obvious from its name — anything pinned `"*"`, anything present
because a *different* package needs it, anything that is a C library rather than
a Python one — carries a `#` comment saying why, either directly above it or
after the version specifier. Directly is meant literally: a comment heading a
group covers only the line immediately beneath it, so a new declaration cannot
inherit a reason written about its neighbour.

**A reusable app must reach conda-forge before a component may depend on it.**
The channel is a precondition, not a step to work around by reaching for the
package index.

**Channel presence alone is not fitness — and this is a standing rule, not an
observation.** FR-50: before any feature is committed to, confirm **both**
channel availability **and** fitness against the pinned runtime. Presence
answers *where a package comes from*, not *whether it works here*. Presence
alone is explicitly insufficient.

The failure mode is concrete rather than theoretical, and it has a name in this
repository. `django-storages` **is** on conda-forge, which is the availability
test, and its 1.14.6 release (2025-04-02) declares support for neither Python
3.14 nor any Django this project has pinned: not the 6.0 the gap was found
against, and not the 5.2 LTS pinned since Story 1.9. Availability passed;
fitness was unknown. That gap is risk **R-1**, and closing it is what [Object
storage fitness (R-1)](#object-storage-fitness-r-1) below records.

Committing to a feature means declaring its package in `[dependencies]`, so the
way to obey the rule is to stage the package somewhere else first — a pixi
feature of its own, sharing the solve-group — prove it there, record the
verdict beside the declaration, and only then move it. That is exactly the shape
`[feature.spike-storage]` has in `pixi.toml`.

**This half of the rule is a documented obligation, not an automated
assertion.** There is no mechanical test for "a future feature was proposed", so
nothing in the suite can fail when someone skips the fitness check. What *is*
asserted is the outcome of the check that was run: that the spiked package stays
out of the runtime set, that its environment shares the solve-group, and that a
verdict from the closed set of three is recorded beside the declaration
(`tests/unit/test_dependency_policy.py`). Saying so plainly is better than
writing a test that appears to enforce the rule and does not.

`tests/unit/test_dependency_policy.py` asserts all of this against `pixi.toml`
and `pixi.lock`: the channel list, the single package-index entry, the rationale
comments, the exit-condition rule, and that every declared dependency resolves
in the lock to a concrete version *satisfying* what the manifest declares, for
every environment and every platform `[workspace] platforms` names. `libpq` —
the C library `psycopg` links against, and the one the application would
otherwise assume the host provides — is asserted individually. The rest of the C
surface is not enumerated; the general rule is the declaration policy above, not
a test that knows every library by name.

`pixi.lock` is generated. Re-solve it with `pixi install`; never hand-edit it.

### Object storage fitness (R-1)

FR-25's object storage attaches an S3-compatible backend through
`django-storages` and `boto3`. It appears in three of the six selectable
combinations and is expected to be selected by most components, so dropping it
was never an available answer — the risk had to be carried rather than avoided.
Hence the spike, run in Epic 1 rather than in Epic 7, on FR-50's own rule that
fitness is proven before a feature is committed to.

**Verdict: proven with a stated bound.** Recorded 2026-08-16.
Tested against: django-storages 1.14.6, boto3 1.43.65, Django 5.2, Python 3.14 —
the versions the spike reads back from the installed distributions and asserts,
so a bump to any of them invalidates this verdict rather than inheriting it.
That listing is reconciled against the one in `pixi.toml`, so this copy cannot
be left behind when the spike is re-run.

**Re-run against the LTS runtime.** Story 1.9 moved the pin off the Django 6.0
feature release onto the 5.2 LTS series, which invalidated the verdict as first
recorded — deliberately, since a verdict is a statement about specific versions.
`pixi run spike-storage` was re-run on 2026-08-16 against Django 5.2.15 and all
29 assertions passed unchanged, so the verdict holds on LTS and the
"Out of scope" disclaimer that stood here has been deleted. That deletion is
not tidying: `test_the_recorded_verdict_names_the_versions_the_lock_resolves`
fails on a disclaimer that no longer contradicts the verdict, so the disclaimer
could not have been left behind.

**Upstream is moving again, but has not released.** As of 2026-08-16 the
`django-storages` master branch declares `Framework :: Django` 4.2, 5.2 and 6.0
and Python 3.10–3.14 (commit #1545, 2026-08-02) — covering the runtime this
project ships. None of it is released: master still reports `__version__`
1.14.6 and the newest tag is 1.14.6, so conda-forge has nothing newer to
package. Treat a release as unscheduled rather than imminent — the Django 5.2
support commit (#1520) landed 2025-06-17 and has sat unreleased since. The
watch signal is a tag above 1.14.6; if one lands, the declared-support gap
closes on a version bump and the feedstock-push rung of the ladder below is
never needed.

The spike itself runs in no automated path: `.github/workflows/ci.yml` pins
`environments: dev`, and nothing in `pixi run ci` reaches the `spike-storage`
environment. So the version assertion inside the spike is not what holds this
verdict to its runtime — `pixi run ci` is. The gate reads those four versions
back out of the comment in `pixi.toml` and reconciles them against `pixi.lock`
(`test_the_recorded_verdict_names_the_versions_the_lock_resolves`), which is
what fails when a bump is solved but never re-spiked.

What the spike proved, all without a network:

- `storages.backends.s3.S3Storage` imports and instantiates. The legacy
  `storages.backends.s3boto3.S3Boto3Storage` also ships in 1.14.6 and is a
  subclass of it; Epic 7 names the former.
- `STORAGES["default"]` resolves through both
  `django.core.files.storage.storages["default"]` and `default_storage`.
- `save`, `open`, `exists`, `delete`, `url`, `size`, `listdir` and
  `get_available_name` are all present, and each accepts the positional call
  Django 6.0's `Storage` base declares — the minimal one and the maximal one.
  The two keyword calls Django actually makes — `save(…, max_length=…)` from
  `django/db/models/fields/files.py` and `get_available_name(…, max_length=…)`
  from `django/core/files/storage/base.py` — bind as well.
- The private hooks behind the two inherited methods, `_save` and `_open`,
  accept the two-argument call Django's own base class makes to them.
- `boto3` builds its session, resolves the endpoint and assembles a client,
  offline, against the configured `endpoint_url`.
- No `DeprecationWarning` and no `RemovedInDjango*Warning` on import or
  instantiation, checked against freshly re-imported `storages`, `boto3` and
  `botocore`.

**Two of those eight methods say nothing about `django-storages`, and the
verdict says so.** `S3Storage.save` and `S3Storage.open` are the *identical
function objects* as Django 6.0's `Storage.save` and `Storage.open` — the
package overrides `_save` and `_open`, not the public methods. A signature check
of those two therefore compares Django with itself and cannot fail. The set of
inherited methods is frozen in the spike, and the overrides where the real
behaviour lives are checked separately, which is what keeps the remaining six
meaningful.

**`run_checks()` is a weak signal, not proof.** Django does not instantiate a
storage backend during system checks. Setting `STORAGES["default"]["BACKEND"]`
to a module that does not exist and calling `run_checks()` returns an empty
list — verified rather than reasoned about; the only check that reads `STORAGES`
is `staticfiles.checks.check_storages`, and it inspects the `staticfiles` alias.
The leg is kept because the spec mandates it, and it shows that configuring the
backend introduces no *other* check error. It does not show the backend is
check-clean.

**FR-38 is the application's job, and this is the finding Epic 7 Story 7.5 has
to act on.** `django-storages` 1.14.6 reads only two of the five values from the
process environment on its own — `access_key` and `secret_key`, via its internal
`lookup_env` on `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY`. `endpoint_url`,
`region_name` and `bucket_name` come from **Django settings alone**. The spike
proves it by withholding each option in turn and watching those three come back
`None`, and by putting a value in `settings.AWS_STORAGE_BUCKET_NAME` and
watching it arrive. So "configured from environment variables alone" is
something the *settings module* delivers, by routing those three from
`os.environ` into `STORAGES["default"]["OPTIONS"]` — not something the package
delivers for free. Story 7.5 reproduces that routing, and raises
`django.core.exceptions.ImproperlyConfigured` naming the missing variable rather
than the bare `KeyError` the spike's own helper raises.

**The bound — what is *not* proven.** The round-trip leg (`save` → `exists` →
`open` → `size` → `url` → `delete` against a live S3-compatible endpoint) did
not run. It is armed by `SPIKE_STORAGE_ROUND_TRIP` and no endpoint was stood up.
So the wire protocol against a real bucket is unproven. Set
`SPIKE_STORAGE_ROUND_TRIP=1` together with `AWS_S3_ENDPOINT_URL` and the four
other `AWS_*` variables pointing at a MinIO or S3 endpoint you are willing to
have written to, then re-run `pixi run spike-storage` to close it.

The opt-in is deliberate and it is not a formality. Without it the spike ignores
ambient `AWS_*` values entirely and uses unreachable `.invalid` fallbacks, so
running the documented command cannot reach a bucket a developer's shell or AWS
profile happens to name. When it *is* armed, the leg writes one object under a
key carrying a fresh UUID, having first asserted that key is free, with
`file_overwrite` disabled so a collision is renamed rather than silently
overwritten; teardown deletes both the key it meant to write and whatever name
`save()` returned, so an object created by a `save()` that then raised is still
removed. It touches nothing it did not create. That — rather than "it deletes
what it creates" — is what "leaves the bucket as it found it" means here.

**One divergence, recorded rather than hidden.** `S3Storage` spells Django's
`Storage.listdir(self, path)` as `listdir(self, name)`. It is harmless because
`listdir` is not one of the methods Django calls by keyword — *not* because
Django calls positionally only, which is false: the two `max_length=` call sites
above are keyword calls into this same contract. A rename touching `save`,
`get_available_name` or either `max_length` parameter would break Django itself.
`listdir(path=...)` still raises `TypeError`. The spike freezes the set of such
renames, so a second one fails instead of passing unremarked.

**The escalation ladder, in R-1's own order.** It is written down whether or not
it is triggered, so that the next person meets the order rather than inventing
one:

1. **Spike `1.14.6` against the locked Django and Python** — first, because
   `django-storages` is a thin wrapper over a `boto3` already in the lock and
   Django's `Storage` API has been stable. *This is the step that ran, and it
   passed.*
2. **If that fails, push the conda-forge feedstock**, as was done for
   `django-celery-beat`, under a **time-boxed** package-index exception whose
   exit condition is that build landing. Not triggered.
3. **A component-owned S3 backend** against `django.core.files.storage.Storage`
   is the **last resort**, because a platform product owning its own storage
   backend is a permanent maintenance and security cost. Not triggered.

**A permanent supply-chain exception is not on the list.** Steps 2 and 3 are not
interchangeable and step 2 is not skippable: reaching for a component-owned
backend without first attempting the feedstock push takes on permanent
maintenance to avoid a temporary one.

**Where the verdict lives.** Beside the declaration, in `pixi.toml` under
`[feature.spike-storage.dependencies]`, which is what AC #1 means by "recorded
where the dependency is declared" and what lets Epic 7 Story 7.5 act on it
without re-deriving anything. `django-storages` is deliberately **not** in
`[dependencies]`: a passing verdict authorises Story 7.5 to build the feature,
and moving the declaration into the runtime set is that story's act.

**Removing it, and what that costs.** Epic 7 Story 7.5 moves the
`django-storages` declaration and its verdict comment into the `storage`
feature; Epic 8 Story 8.1 then deletes `[feature.spike-storage]` and the
`spike-storage` environment with it. That deletion is not a one-line edit. Nine
assertions across three test modules name this feature, this directory or this
task and have to move in the same change: six in
`tests/unit/test_dependency_policy.py` (staging, single-table declaration,
solve-group, verdict recorded beside the declaration, verdict-versus-lock
versions, docs-versus-manifest verdict), three in
`tests/unit/test_gate_contract.py` (not a gate step, the task's target file
exists, the gate cannot collect the spike — which asserts `tests/spikes/`
exists), plus the `RECORDED_EXEMPTIONS` entry in
`tests/unit/test_suite_policy.py`, which fails both when its `pytest.skip`
disappears and when a second one appears. The same list is recorded beside the
declaration in `pixi.toml`, which is where the person doing the removal will be
looking.

**Running it.** `pixi run spike-storage`. The spike is not part of the gate — it
needs the `spike-storage` environment, which `pixi run ci` does not use. It
stays out of collection by name: its module is `tests/spikes/spike_*.py`, which
`[tool.pytest.ini_options] python_files` does not match, so the `pytest tests/`
the gate runs never reaches it while a command line that names the file does.
The alternative — `-m "not spike"` on `test-cov` — is closed by AD-20, which
bans narrowing flags on the floor-carrying task.

## Running with no external services

Nothing has to be running alongside the application to develop against it — no
database server, no cache, no broker, no collector. Every deployed dependency
has a local stand-in, and each one is a deliberate choice rather than a default
that happens to work:

| Deployed | Local | Set by |
| --- | --- | --- |
| PostgreSQL | sqlite at `db.sqlite3` | `config/settings/base.py`, when no `DATABASE_URL` or `POSTGRES_DB` is set |
| Redis cache | `LocMemCache` | `config/settings/local.py` |
| Celery and its broker | eager, in-process execution | `CELERY_TASK_ALWAYS_EAGER` in `config/settings/local.py` |

Observability is the exception: it is not substituted at all. The tracer
provider, the instrumentors and the structlog pipeline run locally exactly as
they run deployed — only the export step is absent, so spans are discarded when
they end. Three consequences follow, and each of them is tested:

- **With no OTLP endpoint configured** — the ambient state of every local run
  and every test run — the tracer provider is still installed and all four
  instrumentors still instrument. Spans are still created and ended, and
  `trace_id` and `span_id` still reach every log line emitted while a span is
  active — every line a request or a task produces.
- **Spans are discarded at the processor.** No endpoint means no span processor
  is attached, and that absence is the discard. Nothing in the default
  configuration points a batch processor at a collector that is not there, so no
  retry cycle floods stderr through a test run. (Setting
  `OTEL_TRACES_EXPORTER=otlp` by hand with no endpoint is the deliberate opt-in
  that does attach one.)
- **`OTEL_TRACES_EXPORTER=console` attaches a console exporter**, which prints
  spans to stdout, and changes nothing else: the same instrumentors, the same
  resource, the same log lines.

See [Observability](observability.md).

**The broker constraint is a statement about deployment only.** Elsewhere in
this product you will read that background task processing requires a broker.
That is a deployment requirement and nothing more: locally, *every* valid
combination runs with no broker at all — including the combinations that
selected background task processing. Nothing has to be started, and no
combination is excluded from local development for want of a Redis. Calling
`.delay()` on a task runs its body in the calling process and raises its
exceptions into the caller, so the code path a background task takes is
exercised locally even though no delivery happens. Deployed, the same absence is
a misconfiguration the component refuses to start on, rather than a convenience.

The fifth substitution is filesystem-backed object storage, and it is not here
yet: it arrives with the storage feature in Epic 7. Until then a generated
component has no local object-storage stand-in, and nothing in this section
should be read as promising one.

Each stand-in trades away something real, and the trade is knowing rather than
accidental. sqlite accepts schemas and queries PostgreSQL rejects — a migration
that applies cleanly here can be refused by the database you deploy against;
`LocMemCache` never evicts or shares state across processes; eager Celery never
exercises delivery, retries, or argument serialization, because there is no
queue for a message to be serialized onto, lost on, or retried from. **Local
success is not by itself evidence that a change works deployed.** When a change
touches schema, cache eviction, or task delivery, run it against the real
services before believing it — point `DATABASE_URL` at a PostgreSQL instance
(see [The parity gap between local runs and the gate](#the-parity-gap-between-local-runs-and-the-gate))
rather than treating a green local run as the answer.

### Nothing on the start path reaches the network

The stand-ins above are why nothing has to be *running* alongside the
application. This is the stronger statement beside them: **nothing on the local
start path calls the identity provider, a registry or a package index at boot.**
Importing the settings, `configure_observability()` and `django.setup()` are
environment reads, computation and app loading — a component starts with no
route to the identity provider and starts anyway. Configured datastores are the
exception this deliberately excludes: a component reaches its database when it
uses it, over TCP like any other client.

The two calls that would otherwise break that are both lazy:

- **OIDC discovery.** The provider is configured from `SOCIALACCOUNT_PROVIDERS`,
  populated from `COMPONENT_OIDC_ISSUER` (`config/settings/base.py`). The issuer
  arrives as a string and stays one; the discovery document is fetched by
  allauth's request-time `openid_config`, so the first fetch is a sign-in, never
  configuration.
- **JWKS retrieval.** `KEY_STORE` is constructed at import and empty
  (`config/authorization/jwks.py`). The first fetch is the first Bearer request
  that needs a key, cached by `kid` thereafter. This is also why the
  trust-anchor check is a string derivation over the configured issuer rather
  than a comparison against the issuer's published `jwks_uri`: confirming a
  location against a discovery document means fetching it, at boot.

The two local operations this section is about are local in the same sense:
generating the development keypair is computation, and seeding the personas is a
database write. Neither reaches a registry, the identity provider, or a package
index.

**The claim begins once the environment exists.** Environment installation
downloads packages by definition — `pixi install` is out of scope for it, and so
is anything else that resolves or fetches a dependency.

**One deliberate opt-in breaks it, and it is the one documented above:** setting
`OTEL_TRACES_EXPORTER=otlp` by hand with no endpoint configured attaches a batch
processor to an exporter that defaults to `http://localhost:4318`. The
attachment is what happens at `configure_observability()` time; the outbound
connection is made by the exporter's own background thread shortly after boot.
That is a choice, not the default — see
[Running with no external services](#running-with-no-external-services) above,
where the same exception is stated as the reason nothing points a batch
processor at a collector that is not there.

`tests/unit/test_no_network_at_boot.py` is what holds this. It boots the
component in a fresh interpreter with `socket.socket.connect`,
`socket.socket.connect_ex`, `socket.create_connection`, `socket.getaddrinfo` and
`socket.gethostbyname` all refusing, and asserts both that boot completed and
that the key store is still empty afterwards.

The guard covers Python's socket layer, which is where its blind spots are:
connectionless UDP and any I/O performed inside a C extension — libpq above all —
are outside what it can see.

## Database

`config/settings/base.py` selects a backend in this order:

1. `DATABASE_URL`, if set
2. `POSTGRES_DB` (with `POSTGRES_USER`, `POSTGRES_PASSWORD`, and optional
   `POSTGRES_HOST` / `POSTGRES_PORT`)
3. sqlite at `db.sqlite3` in the repository root

`config/settings/production.py` raises `ImproperlyConfigured` if step 3 is
reached in production, so a deployment can never silently come up on sqlite.
Point `DATABASE_URL` at a real PostgreSQL instance whenever you need to check
behaviour the sqlite backend cannot show you.

### The parity gap between local runs and the gate

Local runs use sqlite. **The gate uses PostgreSQL** — `.github/workflows/ci.yml`
declares a `postgres:17` service on the `gate` job and sets `DATABASE_URL` at
job level, so all five steps of `pixi run ci` see it. Nothing in the settings
selects the backend beyond that URL.

That divergence is deliberate. It is risk **R-5** — *local development proves
less than running suggests* — a knowingly traded gap, not a defect. sqlite
accepts schemas and queries PostgreSQL rejects, so a green local suite is
weaker evidence than the same suite green in CI.

The consequence is worth stating plainly: **a failure that reproduces only in
CI is the expected behaviour of this trade.** It is fixed at its source — the
model, the view, the form, or a new migration — never by narrowing the gate.
Skipping such a test, marking it `xfail`, or branching on the engine inside it
would convert a refusal into a warning, which the project forbids.

To reproduce a CI-only failure, run the suite against a real PostgreSQL:

```sh
pixi run gate-postgres
```

That task is `scripts/gate-postgres.sh`, and it is the recipe below made
runnable — it starts the container, waits for it, runs `test-cov` against it and
stops it again, reporting the suite's own exit status. Reach for it before
pushing anything that touches a model, a migration, a query or the
instrumentation around one. It needs Docker running and it runs `test-cov`
rather than the whole gate, because `precommit`, `build`, `typecheck` and `lint`
never open a connection.

The recipe it automates is kept here because a task you cannot read is a task you
cannot debug. The host port is deliberately not 5432 — that one is often already
taken by a local PostgreSQL — and `--rm` means the container disposes of itself
on stop:

```sh
docker rm -f pg-local >/dev/null 2>&1 || true
docker run -d --rm --name pg-local -e POSTGRES_USER=gateuser \
  -e POSTGRES_PASSWORD=gatepass -e POSTGRES_DB=gatedb -p 55432:5432 postgres:17

ready=""
for _ in $(seq 30); do
  docker exec pg-local pg_isready -h localhost -U gateuser -d gatedb && { ready=1; break; }
  sleep 1
done

if [ -n "$ready" ]; then
  DATABASE_URL=postgres://gateuser:gatepass@localhost:55432/gatedb pixi run test-cov
  status=$?
else
  echo "pg-local never became ready; see 'docker logs pg-local'" >&2
  status=1
fi

docker stop pg-local >/dev/null 2>&1
echo "exit status: $status"
```

Three details are load-bearing, and each is there because its absence bites in
the failing case — which is the only case this recipe exists for:

- **The readiness loop records whether it succeeded.** Falling out of it after 30
  seconds and running anyway would test against a database that never came up,
  producing a connection-refused failure that looks nothing like the one you came
  to reproduce.
- **The container is stopped explicitly, on both paths, and nothing calls
  `exit`.** A `trap … EXIT` is wrong here: pasted into an interactive shell it
  fires when the *shell* exits rather than when the run finishes, so it leaves
  the container holding port 55432 for the rest of the session — and leaves the
  trap installed too. A bare `exit` is wrong for the mirror-image reason: it
  would close the shell you pasted into.
- **`pg_isready -h localhost` rather than the bare form**, for the same reason
  the gate uses it: the postgres image's init phase runs a socket-only server
  that answers the bare check while TCP is still closed.

The recipe runs `pixi run test-cov`, not `pixi run ci`. The database behaviour
is what you are reproducing, and `test-cov` is the gate step that exercises it;
the gate's first step is `pre-commit run --all-files`, which reformats and
auto-fixes your working tree, which is not something a debugging recipe should
do behind your back. Run the full `pixi run ci` against the same URL when you
want the gate itself.

`--reuse-db` is set in `pyproject.toml`, which is right for a CI service
container recreated on every run but hides schema drift across repeated local
runs. **After changing a migration, rebuild the test database** rather than
reusing it:

```sh
DATABASE_URL=postgres://gateuser:gatepass@localhost:55432/gatedb \
  pixi run test-cov --create-db
```

Every command here goes through `pixi run`; invoking `pytest` directly picks up
a different environment than the gate uses.

### The same gap for the cache, and the one test that cannot run without closing it

Local runs use `LocMemCache`. **The gate uses Redis** — `.github/workflows/ci.yml`
declares a `redis:7` service on the `gate` job and sets `CPM_TEST_REDIS_URL` at
job level, and `config/settings/test.py` selects `django_redis.cache.RedisCache`
when that variable is set. As with the database, the variable is the whole
mechanism; nothing else in the settings chooses a backend.

Most of the suite does not care. One test does, and it is the reason this exists:

```sh
pixi run gate-redis
```

`core/rate_limit.py` writes `add`-then-`incr` rather than read-compare-write so
that two worker **processes** racing a new window both increment one counter
instead of one of them resetting the other. Under `LocMemCache` each process has
its own counter, so that property *cannot fail* — every case in
`tests/unit/django_apps/test_rate_limit.py` passes against a limiter that loses
the race, and the reasoning behind the sequence is unverified prose.
`tests/integration/django_apps/test_shared_allowance.py` starts two real
interpreters against a real Redis and is the case that can fail; without the
variable it skips, and the skip message says so.

The task is `scripts/gate-redis.sh` and it is the shape of `gate-postgres.sh`:
it starts a throwaway `redis:7` on port **56379** — deliberately not 6379, which
is often already taken by a local Redis that the suite's `cache.clear()` would
then empty — waits for `redis-cli ping`, runs `test-cov` against it and stops the
container again. It needs Docker running.

A run that reports the shared-allowance case as **skipped** means the variable
never reached pytest; the proof did not run. `pixi run gate-redis` is also worth
reaching for after any change to the rate limiter or the response cache, since
both read `django.core.cache` and neither may branch on which backend is behind
it (`tests/unit/django_apps/test_rate_limit.py` and
`tests/unit/django_apps/test_response_cache.py` assert that against the source,
because a behavioural case never can).

## Tasks

| Task | What it does |
| --- | --- |
| `pixi run runserver` | Django development server |
| `pixi run -e dev serve` | Local ASGI server (uvicorn, all platforms) — a deployment runs `web` |
| `pixi run serve-reload` | The same with autoreload |
| `pixi run migrate` | Apply migrations |
| `pixi run makemigrations` | Generate migrations |
| `pixi run createsuperuser` | Create an admin user |
| `pixi run -e dev seed-personas` | Seed the local development personas — refused without `-e dev` |
| `pixi run -e dev mint-token <persona>` | Mint a development JWT for a persona — refused without `-e dev` |
| `pixi run collectstatic` | Collect static files into `staticfiles/` |
| `pixi run format` | `ruff format` |
| `pixi run lint` | `ruff check` |
| `pixi run typecheck` | `mypy src/` |
| `pixi run test` | Unit tests only (fast) |
| `pixi run test-integration` | Integration tests only |
| `pixi run test-cov` | Full suite, fails under 90% coverage |
| `pixi run gate-postgres` | Full suite against a throwaway `postgres:17` — needs Docker, not a gate step |
| `pixi run gate-redis` | Full suite against a throwaway `redis:7` — needs Docker, not a gate step |
| `pixi run spike-storage` | R-1's `django-storages` fitness spike — not part of the gate |
| `pixi run build` | Build the wheel and sdist |
| `pixi run docs` | Build the documentation (`--strict`) |
| `pixi run docs-serve` | Serve the documentation with live reload |
| `pixi run changelog` | Regenerate `CHANGELOG.md` with git-cliff |
| `pixi run ci` | The gate — see below |

`pixi task list` prints every task with its description straight from
`pixi.toml`, which is the authority; the table above is a reading guide and has
drifted from it before. Run the command rather than trusting the table when the
question is "does this task exist".

## The gate

`pixi run ci` is the single entry point to the quality gate. It runs five steps
in this order, stopping at the first failure:

| # | Step | What it checks |
| --- | --- | --- |
| 1 | `precommit` | `ruff format`, `ruff check --fix` and `mypy` over every file |
| 2 | `build` | the package is distributable — catches import and packaging errors |
| 3 | `typecheck` | `mypy` over the whole `src/` tree with the strict `pyproject.toml` settings |
| 4 | `lint` | `ruff` over everything, zero findings |
| 5 | `test-cov` | the full suite, coverage at or above 90% including templates |

The order is fast-fail-first: the static checks run before the suite, so a type
or lint error surfaces without paying to run the tests.

**CI runs exactly this task and nothing else.** The `gate` job in
`.github/workflows/ci.yml` invokes `pixi run ci` and no other step, so the
sequence a developer runs locally and the sequence the pipeline runs are the
same sequence — no step exists only in one of them. No other workflow may run a
gate step on its own: `sonarqube.yml` consumes the gate's `coverage.xml` rather
than measuring the suite again, and `release.yml` runs no quality checks because
the commit it releases has already passed the gate on `main`.

`tests/unit/test_gate_contract.py` asserts all of this against `pixi.toml` and
the workflow files, so the contract fails the build rather than drifting.

The gate job declares a `postgres:17` service and runs against it, so the five
steps above execute against the database the immovable core actually names —
see [the parity gap](#the-parity-gap-between-local-runs-and-the-gate).

It also declares a `redis:7` service and sets `CPM_TEST_REDIS_URL`, so the suite
runs against a real cache there too. That is not only parity: the
shared-allowance case skips without it, and a skipped case proves nothing — see
[the same gap for the cache](#the-same-gap-for-the-cache-and-the-one-test-that-cannot-run-without-closing-it).

A second job in `ci.yml` runs `pixi run test` and then `pixi run test-integration`
across ubuntu, windows and macos. That job claims the reference application runs
on all three platforms; it is not a second gate, and it stays on the sqlite
substitution. The integration leg is there because the unit tests never open a
database connection at all — once the gate moved to PostgreSQL, that leg became
the only place in CI where sqlite is actually exercised rather than merely
configured. The gate itself is ubuntu-only because GitHub Actions `services:`
containers — which the PostgreSQL gate needs — run only on Linux runners, so the
database could not exist on two of that matrix's three legs.

`pixi run ci` must exit 0 before any change is considered done.

## Logging and tracing

Logs are structured via structlog and carry `request_id`, `user_id` and
`trace_id`; OpenTelemetry traces requests, Celery tasks, queries and cache
calls. Both are always on. Use `structlog.get_logger(__name__)` and pass data
as keyword arguments — never the standard library's `logging`.

See [Observability](observability.md) for the environment variables and for how
export behaves without a collector.

## Tests

- `tests/unit/` — no database or network access, and no filesystem access
  beyond reading the repository's own checked-in configuration (`pyproject.toml`,
  `pixi.toml`, `sonar-project.properties` and the like), which several tests
  assert against directly.
- `tests/integration/` — everything else. `tests/integration/conftest.py`
  applies the `integration` marker automatically, so
  `pytest -m "not integration"` selects the fast suite.
- `tests/spikes/` — fitness probes, not part of the suite. Modules are named
  `spike_*.py` so `pytest tests/` does not collect them, each runs in a pixi
  environment of its own, and each is deleted once its question stops
  mattering. There is one today: [Object storage fitness
  (R-1)](#object-storage-fitness-r-1). Its disposition is `machinery` — a spike
  is accelerator work and never travels to a component.

Shared fixtures live in `tests/conftest.py`; `UserFactory` lives in
`tests/factories.py`.

## Serving the application

`runserver` is Django's development server. `pixi run -e dev serve` runs uvicorn
against `config.asgi:application`, which is closer to the deployed ASGI stack and
works on Linux, macOS and Windows alike. It is still a *local* server: bare
`pixi run serve` resolves in `default`, reads *deployed*, and is refused at
settings import because `config/asgi.py`'s fallback loads `config.settings.local`.

A deployment runs `pixi run web`, not `serve` — see
[Process model](deployment.md#process-model).

Production uses gunicorn with the uvicorn worker class. gunicorn is POSIX-only
and has no conda-forge win-64 build, so `gunicorn` and `uvicorn-worker` are
declared under `[target.linux-64.dependencies]` and
`[target.osx-arm64.dependencies]` rather than in `[dependencies]`. Windows
developers get uvicorn instead; it speaks the same ASGI application, so the
only thing that differs locally is the process manager. If you ever need
multi-worker parity on Windows, `hypercorn` and `granian` are both on
conda-forge and cross-platform.

## Writing an API surface

Two things are decided globally and neither is a per-view choice. Both are swept
by audits, so getting them wrong is a failing test rather than a review comment.

### Every collection response is paginated

`REST_FRAMEWORK` installs `conda_sentinel.core.pagination.BoundedPageNumberPagination`
as `DEFAULT_PAGINATION_CLASS`, with `PAGE_SIZE` beside it. A view that declares
nothing about pagination is paginated; a view that declares *anything* about it —
`pagination_class = None`, a class of its own, a per-view `page_size` — fails
`tests/unit/django_apps/test_pagination_audit.py`, which sweeps this product's
source rather than the views that happen to be registered.

A client cannot ask for a bigger page. `page_size_query_param` is `None`, so
`?page_size=` is inert rather than refused: there is no request that asks for the
whole inventory and is turned down, because there is no request that asks.
`MAX_PAGE_SIZE` is declared anyway, as the ceiling that applies the day a story
opens that parameter.

If a caller genuinely needs more rows than a page holds, the answer is an export,
which is a task rather than an unpaginated response.

### The three queues, and what fills them

`pixi run -e dev seed-demo` fills them, because the policy run does: `core/after_run.py`
declares a seam, `conda_sentinel.workflow` registers a step into it at `ready()`, and
the run calls whatever is registered. `core/policy_run.py` has never heard of a
queue, which is the same inversion the pass registry uses and the reason the layering
audit covers `workflow`.

**A failing step fails the run.** A run that reported success with the queues it was
meant to fill still empty is nobody looking at work nobody knows exists.

Which role owns which queue is `QUEUE_OWNERS` in `workflow/states.py`:

| Queue | Owner |
|---|---|
| identity review | platform and engineering leadership |
| remediation | packaging engineer |
| compliance review | security and compliance reviewer |

**Identity review belonging to leadership is the surprising one**, and it follows
from `CPM-IDENTITY-S05`: the audited identity override is the product's one governed
human write, and `ROLE_GROUP_PERMISSIONS` grants its permission to leadership alone.
The queue is where that permission is exercised. A reviewer still *reads* every
package's identity and provenance — read access to evidence is granted to all three
roles — and acting on identity review is a different grant.

**A queue that is not yours is refused, never rendered empty.** An empty queue says
there is no work, and somebody who reads that goes away satisfied. The nav lists all
three to every role for the same reason the health view does not hide columns:
scoping happens below the nav, and a nav that differed per role would make a shared
link look broken to whoever received it.

`QueueView` is the one surface whose required roles depend on the request — which
queue decides which role. `RoleRequiredMixin.roles_required()` is the seam for that;
overriding a method rather than assigning to the class attribute, which two requests
could race on.

Queues rank by priority bucket then score, which is the priority pass's own order
rather than a second one. The bucket is ranked by its **index** in
`PRIORITY_BUCKETS`, not by its value — `p10` sorts before `p2` lexicographically —
and the ordering terminates on the finding key so a queue pages deterministically.

### Queue items and the finding key

`CPM-AD-22` puts all three queues — identity review, remediation, compliance review —
on one table in `conda_sentinel.workflow`, as filtered views rather than three
models. One row cannot diverge from itself; two can, and routing is an update to a
column rather than a create-and-delete that can half-happen.

**An item is keyed on the finding, never on the evidence row.** Evidence is
append-only, so tonight's collector run inserts a *new row* for the advisory it saw
yesterday. An item keyed on a row id would find no item for the new row, open a
second one, and put an accepted finding back at the top of a queue — with the
original still sitting there resolved and nothing failing.

So each table that can produce work inherits `FindingKeyed` and declares its own
key: `advisory_id + affected_range` for a vulnerability, `normalized_license +
channel` for a licence. Declared beside the evidence rather than in a central
registry, because a registry lets a table be added without one and the omission is
invisible until duplicates appear weeks later.

What a key must never include is anything that moves when the same fact is observed
again — `observed_at`, the primary key — and, less obviously, anything that moves
when the *world* changes without the finding changing: `matched_version` (a package
upgraded towards a fix), `severity` (a re-scored advisory), `raw_license` (a source
that tidied its metadata). Each of those would open a second item for one question.

**Every move goes through `apply_transition`**, and the four things it does are four
separate protections: it locks the row, checks the state the caller *believed* the
item was in, refuses on mismatch, and appends the audit row in the same transaction.
The second is the one that is easy to leave out — the lock alone does not catch a
stale move, because by the time the lock is held the item is simply in a different
state.

The machine itself is data in `workflow/states.py`. Nothing returns an item to
`open`, which is how "created by the policy run, never by a human" is enforced for
free; nothing leaves `resolved` or `accepted`; and one transition — accepting a risk
— is restricted to the security reviewer and requires a recorded reason, both from
the declaration rather than from each caller.

### The product's screens

`CPM-AD-19` gives every app under `src/django_apps/` two kinds of surface: an
app-level `urls.py` with `app_name` for HTML views, and an `api/` subpackage
routed centrally. The screens live in `conda_sentinel.surface`, which is the
read layer and has no models of its own — `CPM-AD-10` gives the application
layer no write path to a derived status, so a read surface that declared a model
would be declaring the one thing the decision forbids it.

**`surface` may import a derived model; `core` may not.** `core` reaches every
policy pass through the registry `policies/apps.py` fills at `ready()`, never by
importing one, which is what lets a pass be added without touching the
orchestrator. It may import a domain application's *vocabulary* — `PackageHealth`
takes its columns' `choices` from `policies.outcomes` — but never its tables.
A module that has to know that a vulnerability verdict lives on
`package_vulnerability.vulnerability_status` belongs in `surface`.
`tests/unit/django_apps/test_app_layering_audit.py` holds the line.

**The coverage screen counts absence, not health.** `surface/coverage.py` reports
how many packages have no established identity and how many carry no verdict in
each rollup column — never a percentage healthy. `CPM-FR-5` forbids presenting a
package as clean without evidence, and this is where the aggregate of that is
visible. Its collector roster comes from `core/registry.py`, never a list written
in the view: a hand-written roster gives a clean bill of health to a collector
nobody added to it, and the omission is invisible because the screen looks
complete. Each collector is judged against the `freshness_target` it declares for
itself — the targets genuinely differ, two days for an advisory sweep and thirty
for a Python 3.14 build — and one that has never run says so rather than rendering
blank.

Both that screen and the home view are `CPM-APP-S09` and `CPM-APP-S10`, added to
the epic after it was written to close design gaps `G-8` and `G-9`. No PRD
requirement commissions them and their acceptance criteria were drafted by the
implementing agent; each story file leads with that caveat. Treat the metric
definitions as a proposal.

**Evidence is read off the derived row's own citation, never re-derived.** Every
pass records what it used — `PackageVulnerability` names its `vulnerability_finding`,
`PackageCurrency` names the snapshot for the authority it chose, and
`PackagePythonReadiness` names the assessment *or* the verification depending on
which kind of evidence the verdict rests on. So "which row produced this status" is
a fact the policy engine already recorded. Working it out again on the read side —
newest row at or before the cut-off, say — is the application deciding something a
pass decided (`CPM-AD-10`), and it silently disagrees with the verdict the moment a
pass's selection rule changes.

Superseded evidence then costs nothing to keep reachable: it is append-only
(`CPM-AD-2`), so the row a run cited last quarter is still there. The detail view
lists every observation of a fact and marks the cited one. Both halves matter — a
screen showing only the newest row loses the history, and one showing all of them
undifferentiated leaves a reviewer with three contradictory states and nothing
saying which the verdict rests on.

**"Cites no observation" and "rests on no observation" are different things.**
Priority and work type are derived from other verdicts rather than observed from a
source, and say so. Currency and Python readiness *are* observed, and each has a
legitimate row that cites nothing — an indeterminate currency verdict chose no
authority; an undecided readiness rests on neither kind of evidence. Rendering those
as "derived from other verdicts" states something false about how the product works,
on the one screen built to be checked. `surface/detail.py` keeps them apart with the
`Trace.observed` flag.

**A status read out of a derived table has to be gated on the way to the screen.**
The rollup's own columns went through `CPM-AD-4`'s confidence gate when the run
wrote them; a derived table holds what the pass wrote, and a pass computes its
verdict without knowing anything about identity. `surface/health.py` calls
`core.confidence.gated_status` for the four statuses it reads that way. Skipping
it puts a confident claim about an unidentified package next to five columns
correctly saying `unknown`, and every other cell on the row looks right.

**Statuses render verbatim, and tones are prefixed so they cannot be mistaken for
them.** `CPM-AD-24` reserves blank for a field with no value and never for a
status, so a cell with no derived row says `unknown` and says why. How a status
is drawn is `surface/tone.py`, whose values are all prefixed `tone-`: the
mockups' chip variants are named `ok`, `warn`, `unknown` and `error`, four of
which are also `OutcomeState` values, and an unprefixed table would be
indistinguishable from a second confidence gate. Every value of every rendered
vocabulary must have a tone; `tests/unit/django_apps/test_tone.py` fails on one
that does not, so a new outcome is a failing test rather than a silently grey
chip.

**Multi-line `{# #}` is not a Django comment.** Django's lexer does not match
across newlines, so a multi-line `{# ... #}` renders as literal text and any
`{% %}` inside it is parsed. Use `{% comment %}` for anything longer than a line.

### Every surface declares the role it needs

Authorization is declared per view and decided in one place —
`conda_sentinel/core/permissions.py`. A view names a permission class and asks
nothing else:

```python
from conda_sentinel.core.permissions import AnyProductRole
from conda_sentinel.core.permissions import requires_roles
from conda_sentinel.core.roles import SECURITY_REVIEWER


class PackageHealthView(ListAPIView):
    permission_classes = (AnyProductRole,)


class IdentityQueueView(ListAPIView):
    permission_classes = (requires_roles(SECURITY_REVIEWER),)
```

`AnyProductRole` is what a read surface declares: all three roles may read
evidence. It is still a real check — it refuses an authenticated user who holds
none of the three roles, which the platform's global `IsAuthenticated` floor lets
straight through. `requires_roles(...)` mints a class for a scoped surface and
refuses at import if it is handed no role or a role the contract does not define,
because a surface nobody can reach looks exactly like a surface nobody uses.

Three rules follow, and each has a test:

- **Nothing else decides.** No module outside `core/permissions.py` may read
  `groups`, `has_perm`, `is_staff` or `is_superuser`. A view that declares a
  permission class and then filters its own queryset by group has reintroduced
  exactly the defect the declaration prevents.
- **Superusers are not exempt.** An operator who needs a queue joins the group
  that confers it, which is a change somebody can audit.
- **Domain code touches none of `DEFAULT_PERMISSION_CLASSES`,
  `AUTHENTICATION_BACKENDS`, `DEFAULT_AUTHENTICATION_CLASSES` or `MIDDLEWARE`.**
  The startup allowlist already refuses these four from an app's contributed
  settings; `tests/unit/django_apps/test_permission_audit.py` covers the rest of
  the ways in.

A refused request is logged at warning under `authorization.refused`, carrying
the acting user, the view, the path, the roles required and the roles held. That
last field is what distinguishes a user whose groups were never mapped (`held` is
empty) from somebody reaching for another role's queue.

## Protocols below the URL resolver

`config/asgi.py` exposes Django's ASGI application directly. There is no
scope-dispatching wrapper in front of it, and no protocol handler sits below
Django's URL resolver. That is deliberate, not an omission.

A handler reached beneath the resolver is invisible to any policy expressed
over the URLconf — the allowlist resolves the URLconf and refuses routes by
view callable, and nothing below the resolver can be named that way. An
inherited `websocket_application` that accepted every connection unauthenticated
is exactly the surface this rule exists to prevent.

**The middleware chain is the standing exception, and none of it is a protocol
handler.** Any middleware may answer from `__call__` or `process_request` and
return without calling the rest of the chain, and a response produced that way
never reaches the URL resolver. Four entries in the `MIDDLEWARE` declared in
`config/settings/base.py` can do it:

- `django.middleware.security.SecurityMiddleware` returns an SSL redirect from
  `process_request` when `SECURE_SSL_REDIRECT` is on — it is on in
  `config/settings/production.py`;
- `corsheaders.middleware.CorsMiddleware` answers a CORS preflight `OPTIONS`
  from `check_preflight()` with a bare 200. The short circuit is decided by
  `CORS_URLS_REGEX` (`^/api/.*$`) and the request method alone — not by the
  request's origin, and not by whether the path is a route. The configured
  origin policy is applied *afterwards*, by omitting
  `access-control-allow-origin` from a response that ships either way. So every
  path under `/api/` answers a preflight, including paths the URLconf does not
  define;
- `whitenoise.middleware.WhiteNoiseMiddleware` serves collected static assets
  from `STATIC_ROOT` under a single known prefix;
- `django.middleware.common.CommonMiddleware` raises `PermissionDenied` for a
  `DISALLOWED_USER_AGENTS` match and returns a redirect when `PREPEND_WWW` is
  set, both from `process_request`. Neither setting is set here, so it is inert
  today — but it is armed by a settings change, not by a change to `MIDDLEWARE`.

All four are accepted: they hold no credential or application state and serve no
application data. But they are served surfaces the URLconf does not describe, so
the list has **two** triggers for re-checking it, not one. `MIDDLEWARE` itself
can grow — `config/settings/local.py` already appends
`debug_toolbar.middleware.DebugToolbarMiddleware`, which was checked and does
not short-circuit — and the settings that arm an entry already in the list can
be set without the list changing at all. Any story that claims the URLconf is a
*complete* description of the network surface has to address these explicitly
rather than inherit the claim from this section.

The criterion above is *answering before the resolver runs*. Middleware that
replaces a response after resolution is a different shape and is deliberately
not listed: `allauth.account.middleware.AccountMiddleware`, for one, turns the
resolver's 404 on `/accounts/` into a redirect to the login route. It reaches
the resolver first, so what it acted on is still something the allowlist can
name.

So if this accelerator ever needs a protocol handled below the URL resolver —
WebSockets, raw TCP, a long-lived stream — it arrives as a **designed feature**,
never as an inherited handler:

- it carries its own authentication story, written down, not borrowed from
  Django's session middleware by accident;
- it carries its own entry in the carrier — `accelerator.toml`, the file that
  declares every path's disposition — so the surface is declared where the rest
  of the surface is declared;
- until both exist, `asgi.py` binds exactly one ASGI callable, and it is
  Django's own application, unwrapped.

## Coverage

The gate measures Python **and Django templates** under `src/`, via
`django_coverage_plugin`. Two things are required for template measurement and
both are configured:

- `TEMPLATES[0]["OPTIONS"]["debug"] = True`, set in `config/settings/test.py`.
- `COVERAGE_CORE=ctrace`, set in `[activation.env]` in `pixi.toml`. The plugin
  is a *dynamic* file tracer, which needs `sys.settrace`. On Python 3.12+
  coverage defaults to the `sysmon` core, which does not support such plugins —
  templates are discovered but never traced and silently report 0%.

`template_extensions` is narrowed to `html`; the plugin's default also includes
`txt`, which makes coverage treat stray text files as templates.

Deployment entrypoints (`wsgi.py`, `asgi.py`) are excluded — they carry no
application logic, only the process wiring the WSGI/ASGI server runs before the
first request. This sentence is a third carrier of the exclusion list, after
`[tool.coverage.run] omit` in `pyproject.toml` and `sonar.coverage.exclusions`
in `sonar-project.properties`; `tests/unit/test_asgi_surface.py` asserts that
the three agree, so an entry deleted from one cannot survive here.

Templates are covered by `tests/integration/test_template_rendering.py`, which
drives the real test client. `RequestFactory`-based view tests never render a
response, so without those tests the templates report 0% even though the views
pass.

## Pre-commit

Every hook is `repo: local` and runs the tools from the pixi `dev` feature — all
of them conda-forge packages — so pre-commit can never disagree with
`pixi run lint` / `pixi run typecheck` about versions, and no hook environments are
downloaded or built.

Commit messages are validated by `conventional-commit-hook` at the `commit-msg`
stage, which is what lets git-cliff build the changelog.
