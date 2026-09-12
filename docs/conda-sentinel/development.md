# Developing Conda-Sentinel

The product's own surfaces: the screens, the API, the reports, and the boundary
between what a request may do and what has to leave it.

For the platform underneath — the environment, the database, the task runner and
the harness — see [developing on the platform](../accelerator/development.md).
## Local personas

The fourth substitution is the identity provider. There is none locally, so
identities are **declared as configuration** in `src/config/local_dev/personas.py`
and materialized by a task. Six are declared, with deliberately different
authorization:

| Persona | Identity key (`idp_subject`) | Groups | Reaches |
| --- | --- | --- | --- |
| `staff` | `local-dev:persona:staff` | the designated **staff** group | the Django admin |
| `reader` | `local-dev:persona:reader` | none | nothing — the zero-groups case |
| `reviewer` | `local-dev:persona:reviewer` | the **security reviewer** role group | the product's read surfaces |
| `engineer` | `local-dev:persona:engineer` | the **packaging engineer** role group | the product's read surfaces |
| `leader` | `local-dev:persona:leader` | the **leadership** role group | the product's read surfaces |
| `operations` | `local-dev:persona:operations` | all three role groups **and** the designated staff group | every screen the product has, and the Django admin |

### Seeding something to look at

```console
pixi run -e dev seed-demo
```

A hundred packages with evidence behind them, and one real policy run over both.
Without it the screens render `unknown` everywhere — correct, and useless for judging
a design, because every cell then has the value it would also have if the projection
were broken.

A hundred rather than ten since `CPM-PLATFORM-S05`, and the number is the point: at
ten rows nothing paginates, every table fits above the fold, and a queue holding two
items looks like a queue — so a reviewer asking whether a screen is usable was being
shown one that could not be unusable. The roster mixes web frameworks, data science
packages and ordinary utilities, well known and less so, plus seven things conda-forge
ships that are not Python at all, which is where `not_applicable` comes from.

**The advisories are real**: identifier, severity and affected range come from
OSV.dev, and the single KEV listing is a real CISA catalogue entry with its real date.
Everything around them is a fixture.

**It writes evidence, never a verdict.** Every status the seeded screens show was
concluded by the pass that owns it, from the parameter file that ships. Identity
goes through `resolve_package_shell` and `record_resolution` (`CPM-AD-14`,
`CPM-AD-25`), never `Package.objects.create` — so the unmapped package in the demo
is genuinely unmapped and the confidence gate blanking its row is the gate working,
not a fixture imitating it.

Two consequences worth expecting:

- **Licence is `manual_review` for every package.** The shipped parameter file
  records `license_rules = []` deliberately — PRD Open Question 4 — so that column is
  inert until someone records a rule set at a new version. `priority_rules` was empty
  the same way until `2026.09.4` recorded ten, so priority buckets are real from that
  version on and seven of the ten fire on this roster. The seeder reads which
  parameters are still empty off the version it ran, rather than saying it in prose.
- **Running it twice appends.** Evidence is append-only (`CPM-AD-2`), so a second
  run adds a second observation of each fact rather than replacing the first. That
  is realistic, and it is what gives the package detail view's superseded-evidence
  list something to show.

It refuses outside a local run, more firmly than the persona seeder: a fictional
observation written by a deployed component cannot be deleted, and every replayed
policy run would read it afterwards.

**Sign in as `reviewer`, `engineer` or `leader` to reach the product's own
screens.** Every surface declares the role it requires (`CPM-AD-13`), and neither
`staff` nor `reader` holds one — `staff` reaches the Django admin and `reader`
reaches nothing. Before the three role personas existed, running the server and
signing in got you refused by every screen the product has, with no way forward:
`sync_authorization` reconciles group membership to the claims, so a group granted
by hand in the admin or the shell is erased at the next sign-in.

One role each, deliberately. `CPM-FR-31` scopes queues per role and `CPM-APP-S05`
builds three of them; a persona holding all three reaches every queue and proves
nothing about the scoping. None of the three is also staff, for the same reason.

`operations` is the one exception and `CPM-PLATFORM-S04` added it knowingly: it holds
all three roles and the staff group, for somebody running the platform rather than
working one of its queues — one sign-in, every screen. It does not replace the three,
because what they are for is unchanged: signed in as `operations` every surface admits
you, so a surface checking the wrong thing looks exactly like one checking the right
thing. It is also **not a fourth product role** — `core/roles.py` still declares three
slots, there is no new environment variable, and a deployment provisions nothing new.

No persona names a group. A declaration lists the sentinel `DESIGNATED_STAFF` or
`DESIGNATED_SUPERUSER`, and the *configured* name — `COMPONENT_STAFF_GROUP`,
`COMPONENT_SUPERUSER_GROUP` — is substituted when the claims are built, so the
personas are correct in a component pointed at any IdP's taxonomy. No persona
carries `DESIGNATED_SUPERUSER`, `operations` included: a superuser bypasses every
permission check, so a superuser persona would make every local authorization check
pass and prove nothing — which is the difference between it and `operations`, whose
access is granted by groups the surfaces genuinely check.

Seed them with:

```console
pixi run -e dev seed-personas
```

**The `-e dev` is required.** Locality is declared once, in
`[feature.dev.activation.env]`, so the `dev` environment is what carries
`COMPONENT_RUNTIME=local`. A bare `pixi run seed-personas` resolves in `default`,
which declares nothing and therefore reads *deployed*, and the task refuses with
`ImproperlyConfigured` before it touches the database. That refusal is the
feature, not a bug: **persona seeding never creates a local account in a deployed
environment**, and locality fails closed, so a declaration lost anywhere between
here and production leaves the refusal armed. The same form applies to the other
`[tasks]` entries — see [Locality is declared by the environment](../accelerator/development.md#locality-is-declared-by-the-environment).

Two properties of the seeding are worth knowing:

- **It calls the component's own group provisioning** —
  `django_service.users.provisioning.provision_designated_groups()`, the same
  callable the data migration invokes — rather than creating groups of its own.
  A seeding task that created groups itself would pass every local check while
  leaving every deployed component ungovernable: its IdP asserts groups no
  `Group` row matches, so nobody gets any authorization and nobody can reach the
  admin to fix it. See [Authentication](../accelerator/authentication.md).
- **It drives the real mapper.** Each persona's declaration is turned into a
  synthetic claims payload keyed by the configured claim names, and that payload
  goes through the same `resolve_user` and `sync_for_interactive` an IdP login
  does. So changing a persona's declared groups and re-authenticating produces
  the corresponding membership change — including the *removal* of a group it no
  longer declares — and signing in twice resolves to the same user, because
  resolution is by the identity key and by nothing else.

### Signing in as a persona

Seeding creates the accounts; signing in as one is a **URL route and nothing
else**. It is mounted at `_local/`:

| Path | Method | What it does |
| --- | --- | --- |
| `/_local/` | `GET` | Lists the declared personas, one form each |
| `/_local/<persona>/` | `POST` | Signs in as that persona and redirects to `LOGIN_REDIRECT_URL` |

Four properties are deliberate and none of them is incidental:

- **`POST` only.** A `GET` to the sign-in path answers `405` and establishes no
  session. A credential path you can reach by following a link is a drive-by
  session — a prefetch, an image tag or a link in a chat message would sign you
  in — so listing is a `GET` and the act is a `POST`. The persona is selected by
  a **path segment**, never a query parameter.
- **Mounted only when `COMPONENT_RUNTIME=local`.** The module ships in every
  component; the route is mounted only where locality is local, and the gate is
  `config.locality.is_local()` rather than `DEBUG` — see
  [Locality is declared by the environment](../accelerator/development.md#locality-is-declared-by-the-environment).
  Shipping is not mounting. The views also refuse a non-local run themselves,
  with `404` rather than a configuration error, so a route that became reachable
  by a hand edit still answers nothing.
- **It drives the real mapper.** The view builds the same synthetic claims
  payload the seeding task does, hands it to `resolve_user` and then to
  `sync_for_interactive`, and contains no mapping logic of its own: no group
  assignment, no `is_staff` write, no permission decision. That is why the
  `staff` persona reaches `/admin/` and the `reader` persona is refused it — the
  difference is produced by the mapper reading the claims, exactly as it is for
  an identity the IdP asserted. If the claims cannot be mapped, the page
  re-renders with the mapper's reason and status `400`; on a fresh clone with no
  `COMPONENT_IDENTITY_CLAIM` configured, that is the first thing you will see.
- **It adds no authentication backend.** `AUTHENTICATION_BACKENDS` is unchanged;
  the session names the already-declared `ModelBackend`. The route prefix is the
  one new entry on the component's credential surface. That surface is not yet
  enumerated anywhere — the allowlist that will enumerate it is a later epic's,
  and until it lands the prefix is guarded by the locality gate alone.

The route's name and prefix are fixed constants declared once, in
`src/config/local_dev/constants.py`, and they move into `accelerator.toml` in a
later epic without changing their meaning.

**What the route is not.** Signing in as a persona calls
`django.contrib.auth.login` directly; it does not go through allauth. The
authorization you see is the deployed authorization — that is the whole point of
driving the real mapper — but the *session* is not the deployed session: it
carries no `EmailAddress`, no `SocialAccount`, and none of allauth's own state,
so email verification, logout and re-authentication behave differently here than
they do against a real identity provider. This is one more face of R-5 below.

**This route will be refused at startup in a deployed component — that refusal
does not exist yet.** Its reachability is one of the startup refusal conditions a
later epic adds, and that refusal will resolve the view callable's owning module
rather than match the URL name or the prefix, so a rename cannot evade it. It is
the backstop for a route that is reachable anyway, not the expected path. Until
it lands, the locality gate above is the only thing keeping the route unmounted,
so a `COMPONENT_RUNTIME=local` that leaked into a deployed environment would
serve it rather than fail closed at boot.

**R-5, said plainly: the local personas are not a mitigation.** The product's own
risk register puts it that way — there is no break-glass account, and "the local
personas are not a mitigation; they exist only where the refusals do not apply."
Synthetic claims never exercise JWKS retrieval, signature verification against a
rotating key, discovery, or anything else an IdP actually does; they exercise the
mapping and nothing below it. A persona signing in locally is evidence about this
component's authorization logic, never evidence that its identity provider
integration works.

### Minting a development token

The browser path above signs a persona in. The programmatic path mints that same
persona a **Bearer token the real authentication class genuinely verifies**:

```sh
pixi run -e dev mint-token staff
```

The `-e dev` is required, for the same reason it is required for
`seed-personas`: locality is declared once in the `dev` feature's activation
env, and a bare `pixi run mint-token` resolves in `default`, reads *deployed*,
and is refused before a key is generated. Present the token as
`Authorization: Bearer <token>` against any API route.

**Nothing is stubbed.** There is no development authentication class, no
`verify_signature=False` path, and no settings flag that relaxes audience
checking. What makes the token acceptable is that it is correctly signed by a key
the component's configured JWKS location publishes. `config/authorization/authentication.py`
verifies its signature, `iss`, `aud` and `exp` exactly as it verifies a token
issued by a real identity provider, and a tampered, expired, wrong-issuer,
wrong-audience or unknown-`kid` token is refused with 401.

Three pieces make that work, all of them in `config/settings/local.py`:

| Setting | Local value | Why |
| --- | --- | --- |
| `OIDC_JWKS_URL` | a `file://` URL under `.local-dev-keys/` | there is no IdP running locally to serve a JWKS endpoint |
| `OIDC_ISSUER` | a reserved `.invalid` URL | `base.py` defaults it to the empty string, and an empty issuer verifies nothing |
| `OIDC_AUDIENCE` | a local audience name | PyJWT refuses a token whose `aud` is empty, so with this unset *every* minted token is rejected |

All three fill only what the environment left unset, so pointing a local run at a
real identity realm still works through the `COMPONENT_OIDC_*` variables.
`config/authorization/jwks.py` accepts the `file://` scheme **only where locality
is local**; deployed, the same location is refused there, and once the startup
refusal contract lands it is refused again at boot by AD-23's trust-anchor
condition.

**The keypair is generated on demand and is never committed.** The first
`mint-token` writes an RSA-2048 private key to `.local-dev-keys/signing-key.pem`
at mode `0o600`, publishes its public half as `.local-dev-keys/jwks.json`, and
reuses both from then on. The directory is gitignored, and
`tests/unit/test_gitignore_covers_dev_keys.py` fails the gate if that entry is
ever dropped.

That guard matters more here than the same rule would in an ordinary repository.
This tree is a template: a key committed to it would ship inside *every component
generated from it*, so one published private key would be shared by every service
the accelerator ever produces. Delete the directory to rotate; the next mint
generates a fresh keypair.

**Rotating against a running server costs up to a minute.** The new keypair
publishes a new `kid`, and a running process holds its JWKS cache behind the same
refetch rate limit a deployed component uses — `COMPONENT_JWKS_MIN_REFETCH_SECONDS`,
sixty seconds by default. Until that window passes, requests carrying the new
token are refused with `refetch refused by the rate limit`. Restart the server
and it clears immediately. The rate limit is deliberately *not* relaxed for local
runs: the point of this whole section is that what you exercise locally is what
production does, and a local-only exemption would hide exactly the behaviour a
rotation at the real IdP would show you.

**R-5 applies to this path too, and is not softened by any of it.** The token is
locally signed, so synthetic claims still never exercise JWKS retrieval over the
network, discovery, or key rotation at the identity provider. What is proven
locally is the *verification*; the *retrieval* is proven only against a real IdP.

## Themes

Three states, and the third is the one that matters: **light** (the default), **dark**,
and **auto**, which follows the reader's operating system.

`auto` is a choice somebody makes, not the state they are left in by making none.
Collapsing three into a light/dark toggle is the common mistake and it is lossy — a
laptop that switches at sunset should take the product with it, and a two-state
control can only record where somebody was when they last touched it.

### How it works

`surface/theming.py` owns the vocabulary. A cookie holds the choice;
`surface/context_processors.py` reads it onto every page; `conda_sentinel/base.html`
writes `data-theme` on the document element:

```html
<html lang="en" data-theme="light">   <!-- light or dark: asserted -->
<html lang="en">                      <!-- auto: nothing asserted -->
```

**`auto` renders no attribute, and that absence is the mechanism.** The stylesheet's
`prefers-color-scheme: dark` block is guarded by `:root:not([data-theme="light"])`, so
it only decides when nothing has overridden it. Writing `data-theme="auto"` would take
the decision away from the machine and hand it to a value the CSS has no rule for.

**The default asserts itself, for the same reason in reverse.** A default of light that
rendered *nothing* would still give a dark-desktop reader the dark palette. If you
change the default, change what is written, not just what is returned.

### Why a cookie and not `localStorage`

The product ships no JavaScript, and here that is what makes the requirement
satisfiable rather than a cost. A client-side toggle cannot know the choice before the
document loads, so it paints the default and corrects it — the flash of the wrong theme
every such implementation has. A cookie is on the request, so the server renders the
right attribute on the first paint.

### Why not on the `User`

A theme is a property of the screen somebody is looking at, not of who they are: the
same person on a bright monitor and a dark laptop wants different answers, and a column
on `User` would make those one answer. It also has to work before anybody signs in,
because the sign-in page is a screen too.

### Adding a fourth state

Add it to `THEMES` and `THEME_LABELS` — both, and the template loops over the roster so
nothing else changes. `THEME_LABELS` exists precisely so a new value cannot reach the
page without a name: a template titling raw values would render whatever it was given.

Then add the CSS branch. A value in `THEMES` with no rule in the stylesheet is the
worst outcome, because it is selectable and does nothing.

### Two things the view gets right

**`POST` only.** A `GET` that set a cookie would let a prefetch, a link checker or a
shared URL change somebody's preference — and the last one actually happens.

**The return path is validated.** `next` comes from a form field on whatever page the
reader was on, and a form field is attacker-supplied.
`url_has_allowed_host_and_scheme` is what keeps it from being an open redirect
somebody can hang a phishing page off.

## The request boundary

`CPM-AD-9` splits the product in two. A request may read derived state and evidence,
and may write workflow state or an identity override. Four kinds of work leave it:

- an outbound call
- a collector
- a policy pass
- an export beyond `CPM_SYNC_EXPORT_MAX_ROWS`

**Three of those four have no request path at all**, and the way it stays that way is
`tests/unit/django_apps/test_request_boundary_audit.py` rather than habit. The audit
walks the *import closure* of every registered view — a view importing
`core/transport.py` would be caught in review, but a view importing a helper
importing a service importing the client is three files apart with each edit
reasonable on its own.

The failure it prevents is a page that hangs, not a page that is wrong. An outbound
call in a request is fine locally, fine in CI, and a thirty-second page the first
time an upstream service is slow. Nothing goes red; the page just stops coming back.

### Handing work off

```python
job = request_job(
    kind=EXPORT_JOB_KIND,
    parameters={REPORT_SLUG_PARAMETER: report.slug},
    requested_by=request.user,
    clock=SystemClock(),
)
return redirect("conda_sentinel:export-job", pk=job.pk)
```

The redirect *is* the in-progress state. A 202 with a body would leave a reader on a
page that never changes — work handed off and, as far as they can tell, dropped. A
boundary a request can cross but not point back across is one where work disappears.

Two things `request_job` gets right that are invisible when they are wrong:

**It publishes `on_commit`.** A task published inside the transaction that created
the row reaches a worker that may read the database before the commit lands, and
finds nothing. The symptom is a job queued for ever while a worker log says the id
does not exist — on some requests and not others.

**It publishes by name, never by importing the task.** `core/tasks.py` imports the
policy-run orchestrator and, through it, the collectors, so `run_job.delay` would
pull every one of them into the web process's import graph — which is the thing the
audit refuses. `EXPORT_JOB_TASK_NAME` lives in `core/queues.py`, which is import-safe
at settings time, and `send_task` needs nothing else.

If the broker will not take it, the job is **failed with the reason on the row**, not
raised. The row is already committed when `on_commit` fires, so raising produced a
500 that told the reader nothing and a job nothing would ever pick up. The refusal is
recorded where the reader is already being sent.

### Adding a job kind

`core` declares the seam; the app that owns the work fills it at `ready()` — the same
inversion the pass registry and the after-run registry use, and for the same reason:
`core` may not import `surface`.

```python
# surface/apps.py
def ready(self) -> None:
    from conda_sentinel.core.jobs import register_job_runner
    from conda_sentinel.surface.exports import EXPORT_JOB_KIND, run_export_job

    register_job_runner(EXPORT_JOB_KIND, run_export_job)
```

A runner takes `job` and `clock` and returns `(artifact, rows)`. It does **not** touch
the job's state: `start_job` and `finish_job` own that, so every kind records its
lifecycle the same way rather than each remembering to.

`cpm.export.run` is generic — it runs whatever kind it is handed — but its *name* is
not, because a name is a route. A job kind whose workload class differs gets its own
task name under its own namespace calling the same dispatch.

### The four states

`queued`, `running`, `succeeded`, `failed`. **`failed` is a state, not an absence.** A
job that broke has to be distinguishable from one still running, or the status page
says "in progress" for ever — the same problem `CPM-FR-5` makes load-bearing
everywhere else, arriving somewhere new. Two check constraints enforce it: a finished
job has a finish stamp, and a failed job says why.

`BackgroundJob` is the one table in `core` that is **not** append-only. `CPM-AD-2` is
about evidence — what a source said at an instant, which cannot stop being true. A
job is a piece of work with a lifecycle, and a row per state change would make "is
this done" a query rather than a read.

The artifact is a `TextField`. Production's `STORAGES` names `FileSystemStorage`: a
worker writing to its own container's disk produces a file the web replica serving
the download cannot see, and the failure is a 404 that reproduces on some requests
and not others. Object storage would fix it and is not configured; a later story that
configures it moves that one column.

### The export cap

`CPM_SYNC_EXPORT_MAX_ROWS` (PROVISIONAL, 5,000) is the boundary of a request, **not a
limit on what the product will export**. A job's export is unbounded — work that left
the request and then truncated itself would have taken the cost of the boundary
without the benefit.

`surface/exports.py`'s `over_the_cap()` is the only thing that compares anything
against it, and the audit counts how many modules read the setting at all. Three
paths each reading it are three chances to compare it slightly differently, and the
result is a download that is simply short with nothing saying so.

The synchronous `GET` **refuses** over the cap; it never truncates. `POST` hands the
work off. The split of methods is the point: a `GET` that enqueued would let a
bookmark, a prefetch or a link checker create jobs.

### Local development

The hand-off needs a reachable broker. Without one the job is created and immediately
failed with the connection error on its own page — which is the intended behaviour,
and also what you will see locally until Redis is running and authenticated.

## The API

`CPM-FR-27` exposes the same reads over HTTP with a published schema. Everything is
routed from one file — `src/config/api_router.py` — and that is deliberate: the
API's shape has to be legible in one place, because one of its acceptance criteria is
an *enumeration*.

Every path below is under `/conda-sentinel/api/v1/` — see **Addressing and versions**.

| Method | Path | What |
|---|---|---|
| GET | `…/packages/` | Current health, filtered and ordered exactly as the screen |
| GET | `…/packages/<name>/` | One package, every status traced to its evidence |
| GET | `…/queues/<queue>/` | One queue, ranked, scoped to the role that owns it |
| GET | `…/reports/` | The report roster |
| GET | `…/reports/<slug>/` | One report, paginated, with its provenance |
| POST | `…/packages/<id>/identity-override/` | **Write.** Correct an identity |
| POST | `…/workflow-items/<id>/transition/` | **Write.** Move a queue item |

The schema is at `/conda-sentinel/api/v1/schema/` and Swagger UI at
`/conda-sentinel/api/v1/docs/`, both admin-only. They are generated from the
implementation — there is no hand-kept document.

### Addressing and versions

Two API roots, and the split is the same boundary the pages use:

| Root | Whose | Contract |
|---|---|---|
| `/conda-sentinel/api/v1/` | this application | `/conda-sentinel/api/v1/schema/` |
| `/api/` | the platform (the accelerator's user endpoint) | `/api/schema/` |

`/api/schema/` describes the whole service; `/conda-sentinel/api/v1/schema/` describes
**only this application**. An integrator wants the second — the first is this product
plus half of somebody's platform.

Both rosters are declared in `config/api_router.py`, because `CPM-AD-19` makes routing
central. They are *mounted* apart, from `config/urls.py`, because they belong to
different things.

**Namespaces follow the mount.** This application's endpoints reverse through
`conda_sentinel_api:`, the platform's through `api:`. A namespace whose name says
`api` while reversing to somebody else's prefix is the kind of small untruth that
costs an afternoon.

```python
reverse("conda_sentinel_api:package-health")  # /conda-sentinel/api/v1/packages/
reverse("api:user-me")  # /api/users/me/
```

### An unknown version says so

```
GET /conda-sentinel/api/v2/packages/
404 {"detail": "this API serves v1; 'v2' is not a version of it. …"}
```

A bare 404 is indistinguishable from a missing endpoint, and the two send an
integrator looking in different places — "this endpoint does not exist" sends them to
the schema, "this *version* does not exist" sends them to change one segment.

`config/api_versions.py` holds the roster. **Every verb answers the same way**: a
caller who posts to a version that does not exist should not be told the method is
wrong, because their verb is the one thing about the request that was fine.

### Adding a version

Add it to `SERVED_VERSIONS`, mount the new roster beside the old, and publish its
schema at `/conda-sentinel/api/<version>/schema/`. The refusal message names both
without being rewritten.

Versioning is a **path segment plus a refusal**, not DRF's `URLPathVersioning`. That
class reads the version from a URL keyword argument, which would mean `<str:version>`
in every mounted pattern and a `version` argument in every one of the thirty-odd
`reverse()` calls naming these routes. Nothing branches on `request.version` yet.
Adopting the class later changes no path — which is what makes this the cheap order.

### Two settings that must move with the mount

`CORS_URLS_REGEX` covers **both** roots. A rule naming only one leaves every
browser-based caller of the other failing preflight — silently, because a CORS rule
that matches nothing raises nothing.

`SCHEMA_PATH_PREFIX` drives tag and operation-id derivation. It does **not** trim the
prefix from paths; that is `SCHEMA_PATH_PREFIX_TRIM`, deliberately off, so a client
generated from the document reaches the right URL without also being handed a base
path to prepend.

### The two writes

v1 has exactly two, and `tests/unit/django_apps/test_api_contract_audit.py` walks the
URL resolver to prove it. The failure it guards against is not somebody deliberately
adding a third; it is somebody registering a `ModelViewSet`, which brings `POST`,
`PUT`, `PATCH` and `DELETE` with it and looks like one line in a diff.

Neither write implements anything. `identity/api/` hands a `Correction` to
`override_identity` and `workflow/api/` hands a move to `apply_transition`; the
permission check, the required reason, the row lock, the expected-state check and the
audit row written in the same transaction all live in those services. A view that
re-implemented any of it would be a second door into governed data, and
`CPM-AD-14`'s guarantee is that there is one.

**Refusals are typed, not matched on message.** `override_identity` raises
`OverrideNotPermittedError` (403), `OverrideTargetMissingError` (404), or plain
`OverrideError` (400); `apply_transition` raises `WorkflowError`, which becomes a
**409**. That last split is the useful one: a body the API cannot read is the
client's mistake, and a move the machine will not make on an item in the state it is
actually in is a fact about the world that probably changed under the caller.

### Adding a read endpoint

Put it in the owning app's `api/` subpackage, route it in `api_router.py`, and
**declare nothing about pagination** — `CPM-AD-12` puts the bound in
`REST_FRAMEWORK` and a `ListAPIView` inherits it. The audit sweeps for a read that is
not a generic view, because a hand-rolled `APIView` returning a list is how a global
pagination setting stops reaching anything.

Reads belong in `surface/`; writes belong in the app that owns the data. `surface` is
the read layer and sits above the rest, so a write there would make that framing
false — and `workflow` importing `surface.queues` would invert the layering
`test_app_layering_audit.py` exists to fix.

### Serialize the projection, never the model

```python
# yes -- the same dataclasses the templates render
health_rows(page) -> HealthRowSerializer

# no -- a second projection with no confidence gate and its own column list
class Health(serializers.ModelSerializer):
    class Meta:
        model = PackageHealth
```

`CPM-AD-24` names the failure: a new derived status reaching the API but not the
governed view. A `ModelSerializer` over the rollup skips `_gated()`, carries no
evidence timestamps, and falls a column behind the screen the day a pass adds one.
Reading the same dataclass means the API cannot be more or less than the screen,
because there is nothing else for it to read.

The list queryset is `surface/listing.py`'s, for the same reason — the screen and the
API call `health_queryset` rather than each building one.

### Statuses: `StatusField`, always

```python
from conda_sentinel.core.serializer_fields import StatusField


class CellSerializer(serializers.Serializer):
    status = StatusField()  # never null, never blank, never absent
    note = serializers.CharField(allow_blank=True)  # a field with no value: blank is fine
```

`unknown` is one of `CPM-FR-5`'s five outcomes and it is the load-bearing one — it is
how this product says *nobody established anything here*. Every ordinary
serialization habit destroys it: `allow_null=True` makes it `null`, a `BooleanField`
over "is this vulnerable" turns five states into two and loses the three that mean
*we do not know*, and `required=False` drops the key so the client defaults it.

`StatusField` refuses all three as a `TypeError` when the class is built, and
**raises** rather than emitting a falsy value. Raising is the right severity: every
status column here is non-null with a sentinel default, so a blank one is a defect
upstream, and a silently empty status in a response is the failure somebody notices a
quarter later as a package that looked fine.

The audit asserts every status-named field in every serializer is one. A field that
is genuinely not a derived status — `ItemState` on a workflow serializer is a
position in a workflow, not a verdict about a package — goes in
`RECORDED_NON_STATUSES`, spelled out and checked to still exist.

### Two traps

**`get_permissions` is called during schema generation**, with no URL kwargs. A
`NotFound` raised there makes `/api/schema/` answer 404 for the entire document.
Introspection is not a request: refuse in `initial()`, and let `get_permissions`
return something that fails closed.

**Django's `BadRequest` is not a DRF exception.** It reaches Django's handler, which
renders an HTML debug page — with the correct status code, from a JSON API. Translate
it to `ValidationError` at the API boundary.

## Reports and exports

Six recurring reports live in `surface/reports.py`. They are not six views. A report
is a `Q` and a list of columns, and `report_page()` is the only thing that runs one:

```python
Report(
    slug="kev",
    title="Known-exploited vulnerabilities",
    asks="Which packages carry an advisory the CISA catalogue lists as exploited?",
    cadence=DAILY,
    condition=Q(package__vulnerability_policy_findings__kev_membership="listed"),
    columns=(ReportColumn(heading="Risk", source="...risk_level"),),
)
```

### Adding a report

Add an entry to `REPORTS`. Do not add a view, a URL or a template — the slug routes
itself through `ReportView`, and the export comes with it. Two rules make that safe,
and both are structural rather than remembered:

**Provenance is composed on, not written in.** `COMMON_COLUMNS` — package,
confidence, evidence cut-off, computed at — is prepended to every report's own
columns by `Report.all_columns()`. A report cannot omit them. This is `CPM-APP-S06`
AC 2 and `CPM-AD-11`: a report that did not say what it was produced from would be
read as current a month after its cut-off.

The page names **every** policy version its rows carry, not one. `CPM-AD-11` stamps a
version map per row and a replay leaves rows from two runs behind; a report claiming
a single version over rows produced at two would be stating something false about
itself, to the reader most likely to check.

An empty report has `evidence_cutoff = None` and shows no cut-off. That is honest
rather than a gap — a report of nothing was produced from nothing.

**A column is an ORM path, never a callable.** `CPM-AD-10` gives verdicts to the
policy engine. A report that computed one would be a second opinion about a package
that nobody could reconcile with the health view. If a report seems to need a
computed column, the value belongs on the rollup and the policy pass should write it.

Check the reverse accessors when you add one. `packagelicense` and
`packagepythonreadiness` look right and are wrong — the declared names are
`license_policy_findings` and `python_readiness_policy_findings`, and the difference
surfaces as `FieldError` at request time rather than at import. The parameterized
case in `tests/integration/django_apps/test_reports.py` renders every report against
real rows for exactly this reason.

### Exports

`reports/<slug>/export/` streams the same rows as the page, from the same projection
with a different bound. Not a second query — that is how an export comes to disagree
with the screen it was exported from, and the disagreement is only noticed once it is
in a board pack.

**Statuses go out verbatim.** `CPM-AD-24` reserves blank for a field with no value
and forbids it for a status. `unknown` is one of `CPM-FR-5`'s five outcomes, and an
export rendering it as an empty cell destroys the distinction in the one artifact
that leaves the system. `EMPTY` in `reports.py` exists to be asserted against.

Provenance travels in `X-Conda-Sentinel-Provenance`, not in a row. A row is data a
spreadsheet sorts into the middle of the file; a CSV in somebody's downloads folder
next week still has to be datable.

### The row cap

`CPM_SYNC_EXPORT_MAX_ROWS` (default 5,000) bounds what an export will do inside a
request. It is **PROVISIONAL** — one of PRD Open Question 5's two numbers — and is
deliberately below `CPM-NFR-1`'s ten thousand packages, so the cap genuinely bites
and the asynchronous path `CPM-AD-9` requires is one this product takes rather than
one that ships untested until the day it matters.

`CPM-APP-S08` moved the work beyond it out of the request. The synchronous `GET`
refuses over the cap and never truncates; `POST` hands the export to a worker. See
**The request boundary** above.
