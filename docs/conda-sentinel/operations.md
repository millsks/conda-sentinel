# Operating Conda-Sentinel

**Every collector in this product ships inert.** None of them names an upstream,
a channel, a catalogue or an advisory source, and none invents one — so a fresh
deployment observes nothing until somebody declares where to look. That is the
design (`CPM-FR-5`: nothing is presented as clean without evidence), and it is
the first thing to know before reading any of what follows.

Each section below says what a collector or a policy does, what it refuses to
guess, and what you must declare to make it observe anything at all.

For the platform's own deployment mechanics — the process model, the release
stage, the probes — see [deploying a component](../accelerator/deployment.md).
## The inventory watchlist ships unpopulated

The inventory source is a reviewed CSV file the component ships
(`CPM-AD-29`), and which file it reads is selected by locality:
`COMPONENT_RUNTIME=local` reads `watchlist-development.csv` and **everything
else** — absent, empty, or a value like `dev` — reads `watchlist.csv`. Selection
fails closed toward production for the same reason locality itself does: a
deployed component that read the development subset would find every package
outside that subset missing and record each one as *absent*, permanently, in an
append-only log nothing may correct.

`watchlist.csv` ships with its header and **no rows**. Which packages your
organization tracks is your decision, not this component's, so nothing is
invented for you. The consequence to plan for: **inventory ingestion fails on
every run until that file is reviewed in.** The task raises an
`ImproperlyConfigured` naming the file, the run's ledger row finalizes `failed`,
and no package and no snapshot is written.

That failure is the intended behaviour rather than a gap. An inventory naming
nothing is indistinguishable from a source that has broken, and a sweep that
accepted one would record every package the inventory has ever named as departed.
A loud failure on day one is the alternative to a silently corrupted evidence log.

Populate it by pull request. The column contract, the bounds and the editing
rules are documented beside the files, in
`src/django_apps/conda_sentinel/collectors/data/README.md`. Both files ship
inside the wheel, under `conda_sentinel/collectors/data/`;
`tests/integration/test_import_resolution.py` asserts that against the built
artifact, because a build that dropped them fails nowhere else until the first
deployed sweep.

## What every collector calls itself on the wire

Every collector sends the same `User-Agent`, declared once in
`src/django_apps/conda_sentinel/collectors/agent.py`. It is built from the
**distribution name**, the version the running build reports and the project URL:

```text
conda-sentinel/<version> (+https://github.com/millsks/conda-sentinel)
```

!!! warning "This string changed at `CPM-RENAME-S02`, and it is an emitted value"

    The leading token was `conda-sentinel` before that story
    and is `conda-sentinel` after it, because the token *is*
    `pyproject.toml`'s `[project] name` — pixi validates the
    `[pypi-dependencies]` key against the built metadata name, so renaming the
    distribution and leaving this behind is not an available option. **If you
    have an allowlist, a rate-limit exemption or a log filter with a source owner
    keyed on the old string, it needs updating**, on GitHub, PyPI, anaconda.org,
    OSV and KEV alike.

    The URL half deliberately still carries the former name: it is the
    *repository*, which has not been renamed. `CPM-RENAME-S04` moves it.

**Two emitted identities, and only one of them moved.** The other is
`OTEL_SERVICE_NAME`, whose default is still
`conda-sentinel` — see
[Observability](../accelerator/observability.md#configuration). The decisions are opposite on
purpose and the difference is who is forced: the `User-Agent` token had no choice,
because packaging derives it from a name that had to change, and the cost lands on
external allowlists that a note like this one can reach. The trace `service.name`
had a choice, and moving it would silently rewrite the identity every dashboard
and alert is keyed on, with no equivalent note to catch it. A story that wants the
trace identity renamed owns that migration.

## The upstream-release collector reads GitHub unauthenticated

`cpm.collect.source_release` observes a package's own source repository
(`CPM-FR-7`) by asking GitHub's API for one page of its releases. It sends **no
credential**, and its declared allowance says so: sixty requests an hour, which
is GitHub's documented limit for unauthenticated requests, counted per source IP
rather than per component.

Two consequences to plan for.

**The allowance is spent in requests, not collections.** The collector base
charges `1 + retries` against the allowance before each call, because that is how
many requests the mounted retry policy may issue — so sixty an hour is fifteen
packages an hour on the declared retry count. That is enough to observe a small
inventory and is **not** enough to sweep the ten thousand packages `CPM-NFR-1`
sizes for. **The full-inventory sweep below now schedules it daily**, so the
arithmetic is live rather than latent: a sweep of an inventory this allowance
cannot drain records `skipped` dispatch rows and `error` collection rows rather
than exceeding GitHub's budget. Raising it means authenticating, which is recorded
as deferred work.

**A repository that publishes no releases costs a second call.** `CPM-FR-7` asks
for the latest release *or tag*, and many projects tag without ever publishing a
GitHub Release — so an empty release list falls back to the repository's tags. The
fallback fires only then, and it is **not** charged against the local allowance:
the base charges `1 + retries` once, before the first call, so a repository on the
tag path spends more of the *remote* budget than the local counter believes. That
matters only at sweep volume, which nothing reaches yet.

**A spent allowance is recorded, never queued.** The call is refused rather than
waited on — a worker blocked on a limiter holds a slot doing nothing against the
inherited Celery limits — and the refusal writes an evidence row carrying `error`
and finalizes the run `failed`. It is visible in the ledger and in the log line
`collection.refused_by_rate_limit`, which carries the collector, the package and
the locator.

**Telling "we never got to look" from "the source is failing" is `detail`'s job,
not the state's.** Both are `error` rows, deliberately: `OutcomeState` says what
may be claimed about a package, not why a run went the way it did. The row's
`detail` — the same string the ledger row carries — is what separates them. A
refusal names the allowance and the window it was spent in; a transport failure
carries the exception's type and message; a document that could not be read names
the locator and what was wrong with it.

One thing the local counter cannot currently see: GitHub signals an exhausted
anonymous quota with a `403`, which this product's transport reads as an ordinary
failure. So a remote refusal produces an `error` row that looks like any other,
and the local allowance keeps granting until its own window turns over.

**A `not_found` row means "absent **or** unreadable" while no credential is
configured.** GitHub answers `404` identically for a repository that is absent,
one that is private, one that has moved and one that is blocked — by design, so an
unauthenticated reader cannot enumerate private repositories. This collector
cannot tell them apart, so it records `not_found` (which is what the source said)
and writes the caveat into the row's `detail`. Do not read these rows as proof a
repository is gone; a private mirror or a moved upstream produces the same row.

Raising the real allowance, and resolving that ambiguity, both mean authenticating
— which needs a credential, a setting to carry it and a declared header to send it
in. None of the three exists yet; when they do, the allowance declaration moves
with them, because the number and the credential are one decision.

## The PyPI collector reads pypi.org unauthenticated, and asks only about Python packages

`cpm.collect.pypi_release` observes a package's PyPI project (`CPM-FR-8`) by
asking `https://pypi.org/pypi/<name>/json` for the one document that carries the
latest version, its upload dates and `Requires-Python`. It sends **no credential**
— PyPI's JSON API takes none — and its declared allowance is **sixty requests a
minute**.

**The allowance is a declared courtesy bound, not a number the source stated.**
PyPI publishes no numeric ceiling for this API; its guidance is to send an
identifying `User-Agent`, to cache, and to be reasonable. Sixty a minute is one
request a second, which at `1 + retries` per collection is fifteen packages a
minute — written down so it is a limit the base enforces rather than an allowance
that is unlimited by omission, and so an operator can see what "reasonable" was
taken to mean. The response cache is live for this collector: PyPI serves an
`ETag`, so a scheduled recollection revalidates a document that can run to several
mebibytes rather than transferring it again.

**Whether a package is asked about at all is read from its identity, never
guessed.** A package is asked about on PyPI only when resolution recorded its
`release_ecosystem` mapping as `established` with `primary_type = "pypi"`, and the
project name comes from `primary_purl` — PEP 503-normalised, so `Zope.Interface`
and `zope-interface` are one project — never from the canonical name. Nothing
infers "this is a Python package" from how a package is spelled.

**A `not_applicable` row is an observation, and it is what keeps a non-Python
package from reading stale against PyPI.** When resolution recorded the mapping as
`not_applicable`, or established it for some other ecosystem, the collector says
so before any locator is built and the base writes a row carrying
`not_applicable`: no call is made, no allowance is spent, no cache is read, and
the run is `succeeded` with the reason as its `detail`. The row carries this run's
`observed_at`, so the freshness read reports it fresh like any other observation
— `CPM-FR-8`'s "never marked stale against PyPI merely for not being published
there". It is visible in the log line `collection.not_applicable`, whose `source`
is empty because no locator exists on that path. The observation window applies to
it as to every other run, so a sweep does not write the same true fact for one
package more than once per window.

**A package whose release-ecosystem identity is not established fails the run
rather than being guessed at.** A mapping that is `unknown`, `not_found` or
`error` — or a package with no mapping row — cannot be turned into a PyPI
question, and is not the same as "does not apply": the ledger row is `failed`
carrying the reason and no evidence row is written. The same goes for an identity
that records the mapping as `established` with a blank primary type: that is an
inconsistent identity row, and it is refused rather than recorded as either kind of
observation. Until the full-inventory sweep selects only askable packages, expect
such `failed` runs for any package a resolver has not reached; they are a reporting
fact about identity, not about PyPI.

**A `not_found` row means what it says.** PyPI is a public index with no private
projects for an unauthenticated reader to be shut out of, so a `404` is a project
that does not exist or has never released — unlike the GitHub collector above,
this row carries no caveat.

## The feedstock collector reads conda-forge unauthenticated, and asks one of two questions

`cpm.collect.feedstock` observes whether conda-forge has a feedstock for a
package (`CPM-FR-9`). It sends **no credential**, and its declared allowance is
**ten requests a minute**.

**The declared allowance is GitHub's *search* allowance, and that is deliberate.**
One of the two questions this collector asks is a search of the staged-recipes
queue (`GET /search/issues`), which GitHub limits to ten a minute for an
unauthenticated caller — far below the sixty an hour its core API allows, but
counted per minute rather than per hour. The collector base charges one allowance
before it knows which question a package will produce, so a single number has to
cover both, and the tighter of the two is the only one that cannot be exceeded by
accident. At `1 + retries` per collection that is two packages a minute, which is
**not** a rate that sweeps `CPM-NFR-1`'s ten thousand packages. **The
full-inventory sweep below now schedules it weekly**, so the arithmetic is live:
expect `skipped` dispatch rows and `error` collection rows at that scale rather
than a sweep that quietly exceeds GitHub's search budget.

**Which question is asked is read from the package's identity, before any call is
made.**

- A package whose `feedstock` mapping resolution recorded as `established` **with
  feedstock rows** is asked about that feedstock's repository, and then about its
  recipe. Two calls.
- A package whose mapping is `established` with **no** rows, or is `not_found` —
  both of which mean resolution looked and found none — is asked about the
  staged-recipes queue, and then about the conventional `conda-forge/<name>-feedstock`
  repository. Two calls.
- A package whose mapping resolution recorded as `not_applicable` is asked
  nothing at all: the base writes a `not_applicable` row with no call made, no
  allowance spent and no cache read, and the run is `succeeded` with the reason as
  its `detail`.

**The second call on each branch is not charged against the local allowance.** The
base charges `1 + retries` once, before the first call, so every package spends
more of the *remote* budget than the local counter believes — the same gap the
upstream-release collector's tag fallback has, and it matters only at sweep
volume, which nothing reaches yet. The second call is also outside the retry
policy: a failure of it is recorded in the row's `detail` and never fails the
collection, because the first call has already established the fact the row's
`state` claims.

**A `not_found` row does not prove the same thing on both branches, and it does
not always prove absence at all — the row's `detail` is what says which.** On the
mapped branch it means "the feedstock a resolver established for this package is
absent from conda-forge", a statement about identity as much as about the channel,
and the `detail` names that feedstock. On the absent branch it means resolution
established none, and the `detail` then says one of four things: that the
conventional repository is absent too (the only reading that is evidence of
absence); that the conventional repository could not be *read*, so nobody found
out; that the staged-recipes queue itself could not be read and no repository was
checked at all; or that the queue held more results than one page and whether one
of them names this package is not established. Read the `detail`, not the state,
before treating a row as proof that conda-forge has nothing. And none of them is a
claim about *any* possible feedstock under some other name: this collector asks
about the feedstock resolution named, or the conventional one, and about nothing
else.

**A staged recipe is recorded only on a row that says there is no feedstock**, and
the database enforces it (`staged_recipe_only_when_absent`). That is `CPM-FR-9`'s
"staged-recipe state is recorded separately from an existing feedstock" as a rule
rather than a convention. Two open pull requests naming one package produce a row
with **no** staged-recipe URL and a `detail` saying how many matched: which one
would create the feedstock is not a question this collector answers by picking.

**The recipe version is read, never rendered.** A conda-forge recipe is a Jinja
template, and this collector reads the `{% set version = "..." %}` assignment
conda-forge's own recipes open with, falling back to a literal `version:` under
`package:`. A recipe that computes its version any other way records the feedstock
as **present** — which is what the row's `state` claims — with `recipe_version`
blank and `detail` saying it could not be read. Nothing renders a template, so no
recipe-authored code runs in a worker. Recipes are read from
`raw.githubusercontent.com`, which is a second host and has limits of its own that
this product's counter does not see.

**A mapping that holds several feedstocks has only the first by name observed.**
`CPM-FR-1` resolves "zero or more" feedstocks, and this collector asks about one
per collection: the first in name order, so which one a package's history is
about does not depend on which resolver wrote its rows first. The row's `detail`
names how many there were. Nothing about the others is recorded, and a reader
must not treat one row as covering a package's whole feedstock mapping.

**The local counter cannot see a remote refusal, and there are two of them here.**
GitHub signals an exhausted anonymous quota with a `403`, and its search endpoint
applies a *secondary* rate limit that also arrives as a `403`; this product's
transport reads both as ordinary failures. So a remote refusal produces an `error`
row that looks like any other, and the local allowance keeps granting until its own
window turns over. The search endpoint is the one to watch: its limit is the
tightest thing this collector touches, and the secondary limit fires on burst
rather than on rate.

**Egress must be open to two hosts.** `api.github.com` for the repository and the
staged-recipes search, and `raw.githubusercontent.com` for the recipe. A network
policy that allowed only the first would leave every determinate row carrying a
blank `recipe_version` with "the recipe's version could not be read" in `detail` --
a quiet, permanent degradation rather than a failure anything reports.

**A determinate row found on the absent branch carries no recipe facts.** When
resolution established no feedstock and the conventional repository turns out to
exist, the row is `ok` and names that feedstock — but the recipe is not read, so
`recipe_version`, `recipe_build_number` and `recipe_metadata_url` are all blank
and `detail` says the recipe was not read on that branch. Both calls a collection
may make were already spent, and reading the recipe would make it three. The next
run reaches the recipe once resolution has been corrected to name the feedstock
this run found.

**Recipe activity is the feedstock's last push, and it is an instant rather than a
verdict.** `last_recipe_activity_at` is what the repository stated; whether a gap
makes a feedstock "unmaintained" is a policy with a versioned threshold
(`CPM-FR-40`), and no such threshold exists yet.

**A package whose feedstock identity is unresolved fails the run rather than being
recorded absent.** A mapping that is `unknown` or `error` — or a package with no
mapping row — cannot be turned into a feedstock question, and recording it as
`not_found` would be exactly what `CPM-UJ-2` forbids: claiming absence of a
feedstock for a package whose identity nobody has resolved. The ledger row is
`failed` carrying the reason and no evidence row is written. The same goes for a
stored feedstock name that is not a repository segment. Until the full-inventory
sweep selects only askable packages, expect such `failed` runs for any package a
resolver has not reached; they are a reporting fact about identity, not about
conda-forge.

## The published-package collector observes nothing until you declare channels and platforms

`cpm.collect.conda_package` observes what each monitored conda channel actually
publishes for a package (`CPM-FR-10`) — the version the channel states as latest,
the build string and the build number — by asking `api.anaconda.org` for one
package document per channel. It sends **no credential**, and its declared
allowance is **thirty requests a minute**.

**A second collector reads the same host and the same declaration.**
`CPM-SECURITY-S03`'s licence collector asks for the same package document, for a
different fact, with its own allowance and its own daily sweep offset two hours
from this one — see "The licence collector reads the channels you already
declared" below. Size what you ask of `api.anaconda.org` for both.

**It ships monitoring nothing, and that is the intended behaviour rather than a
gap.** Two settings decide what it observes:

```python
# config/settings/base.py
CPM_MONITORED_CHANNELS: tuple[str, ...] = ()
CPM_MONITORED_PLATFORMS: tuple[str, ...] = ()
```

Both ship **empty**. Which conda channels and which platforms this product
watches is PRD Open Question 4 and is unresolved: a component that picked one for
you would record facts about a surface nobody chose, permanently, in an
append-only log nothing may correct — the same trade the inventory watchlist
makes above, and for the same reason.

**What the failure looks like until you declare them.** Every collection fails.
The task raises a `CondaChannelError` naming the setting, the run's ledger row
finalizes `failed` carrying that message, and **no evidence row is written at
all** — there is nothing honest to write, because every row must name the channel
and platform it is about and an empty declaration names neither. A component in
this state starts, serves and reports normally; it simply records no conda
package evidence.

**Declare them by pull request**, in `config/settings/base.py`, beside the
watchlist that ships unpopulated for the same reason. Each entry is a single
lower-case path segment: a channel is the segment `api.anaconda.org` serves a
package under (`conda-forge`, `bioconda`, an internal mirror's name), and a
platform is a conda subdir (`linux-64`, `osx-arm64`, `win-64`, `noarch`). The
declaration is read at **run** time, so a change takes effect on the next
collection.

**A declaration is refused whole rather than read for the entries that parse.** An
entry that is blank, is not a string, carries a path separator, or repeats another
entry once lower-cased fails the run naming the entry and its position. So does a
bare string where a list was expected — `CPM_MONITORED_CHANNELS = "conda-forge"`
is eleven one-character channels to Python and is refused rather than misread.

**At most four channels.** Every channel costs a full, *retried* call — this
component's HTTP retry policy is mounted on the session, so it applies to every
request the collector issues — and the inherited soft time limit is sixty seconds
(`CPM-AD-9`); a declaration whose worst case exceeds it is a task the platform
kills before it writes anything. Four channels, a 2.5-second per-phase timeout and
a retry budget of one come to forty seconds. Platforms are not bounded: a platform
costs a row rather than a call.

**Platform names are checked against conda's own subdir vocabulary.** `linux_64`
or `osx-arm-64` is refused at the declaration rather than observed: a subdir that
does not exist would otherwise record, for every package and for ever, that a
channel's latest version has no file on it — a false statement about the channel
that nothing may correct and nothing could tell from a true one. The refusal names
the permitted set.

**One row per `(channel, platform)`, always, and channels are never merged.** That
is `CPM-FR-10`'s acceptance criterion and the database enforces it
(`conda_package_names_channel_and_platform`): every row names both, sentinel rows
included. Two channels and two platforms is four rows from one run. A pair with
nothing published is a written `not_found` row carrying that run's instant, never
a missing one — which is the whole point, because "installable on `linux-64` but
not on `osx-arm64`" is only visible if the absence is recorded.

**One channel failing never discards another channel's answer.** The channels
after the first are read by bounded calls from inside the translation step: a
transport failure, an unreadable document, or a `304` to a request that carried no
validator becomes `error` rows for *that channel's* pairs, beside the rows the
channels that answered earned, and the run is still `succeeded`. That is
`CPM-FR-15`'s partial success on the per-package path. Read the row's `detail`
before treating an `error` row as a statement about the channel: it says whether
the channel answered or whether this run failed to find out.

**A `not_found` row means one of four things and `detail` says which.** The
channel does not serve the package at all — read from the channel's own `404`,
whether that channel was the one the base asked or one the run asked afterwards;
it serves the package but states no latest version at all; or it states one whose
files do not include this platform, in which case `detail` names the version that
exists elsewhere. None of them is a claim that the package is absent from conda
generally: this collector asks only the channels you declared.

**The recorded version is what the channel calls latest, not what `conda install`
resolves to.** anaconda.org's `latest_version` spans every label, so a package
whose newest upload is a release candidate on a `dev` or `rc` label records that
candidate — while a default install resolves to something older. That is
deliberate: `CPM-FR-10` asks for the version the channel states, and substituting
a different question would answer something nobody asked. What the row does is say
so: when the file it observed is served under labels that do not include `main`,
`detail` names them. Read it before comparing a published version against
anything.

**A build string is a choice where a platform carries several builds.** The
recorded one is the greatest by build number, ties broken by the greatest build
string, and `detail` says how many there were. Do not read the column as the only
build of that version on that platform.

**A channel that does not serve the package never stops the others being
asked.** The first declared channel is asked by the collector base and the rest
from inside the run, and a `404` from that first one is an ordinary answer: every
remaining channel is still asked and every pair still gets its row, so a package
absent from `conda-forge` and published on your internal mirror records both. The
declaration's order therefore does not change what is observed.

**A run that fails outright records `error` for every pair and calls nothing
further.** If the first channel is unreachable, serves a document that cannot be
read, or the local allowance refuses the call, the run is `failed` and each
monitored pair gets an `error` row carrying the reason. No further channel is
called, deliberately: the allowance may be exactly what refused the first call,
and issuing more would defeat it. Retry the run rather than reading those rows as
statements about the channels.

**Only the first channel's call is charged against the local allowance, and only
its answer is cached.** The base charges `1 + retries` once, before the first
call, so every package spends more of the *remote* budget than the local counter
believes — the same gap the upstream-release and feedstock collectors carry, and
it matters only at sweep volume, which nothing reaches yet. The response cache is
the base's too, and it covers that same one call: channels two onward carry no
validator and remember nothing, so **they re-transfer their whole document on
every run**, and a `304` from one of them is a source answering a question nobody
asked, which the row records as `error`. Both are recorded as deferred work on
`CPM-CURRENCY-S04`. **The full-inventory sweep below now schedules this collector
daily** -- but its selection is empty until the two settings above are declared,
so an undeclared component sweeps nothing rather than failing every package. See
"Which packages a sweep offers".

**A misconfiguration and a transient failure leave this task the same way.** An
undeclared channel raises out of `cpm.collect.conda_package` like any other
error, and Celery cannot tell a permanent refusal from something a retry would
fix — so under a retry policy an unconfigured component would run an unbounded
series of identical failed collections. Do not enable a retry policy for this
task before the channels are declared. Also recorded as deferred work.

**Egress must be open to `api.anaconda.org`**, and to nothing else for this
collector. `repodata.json` is deliberately never read: a channel's per-platform
index runs to hundreds of megabytes and answers a question about every package,
while the per-package document answers the question about one in kilobytes.

**Nothing here compares versions.** The published version is recorded exactly as
the channel spelled it. Whether it is *behind* anything is `CPM-FR-16`'s policy
with a versioned threshold, and no such policy exists yet.

## The vulnerability collector ships with no advisory source, and observes nothing until you declare one

`cpm.collect.vulnerability` matches a package and the version its identity names
against an advisory source, and records one row per matched advisory
(`CPM-FR-11`) — the advisory identifier, the severity as the source stated it,
the affected range, the fixed range, the version it was matched against, the
source, and the **match confidence**, which is how surely the source says the
advisory applies. Match confidence is not the package-identity confidence
elsewhere in this product; the two are unrelated and are never abbreviated to the
same word.

**No advisory source ships, and that is the intended behaviour rather than a
gap.** Which advisory sources are available and licensed for use is an open
product question, and a component that read a source nobody chose would record
security findings your organisation never agreed to act on — permanently, in a
log nothing may correct. So the mechanism ships and the source does not.

**Declaring one is a change to this repository, not a setting.** The source is an
*adapter* substituted at the collector base's transport seam, the same mechanism
the inventory watchlist uses. Three things have to happen together:

**1. Write the adapter.** It satisfies `Transport` — one method, `fetch(locator,
*, headers=None) -> Payload` — and owes rather more than that:

- **The locator it is handed** is `advisory://declared-source/<the package's
  primary purl>`, or
  `advisory://declared-source/unnameable-identity/<package key>` when this
  product's identity for that package names no usable package URL. The scheme is
  opaque on purpose: the adapter already knows which API or file it reads. Parse
  the purl out of it; answer the `unnameable-identity` form however you like,
  because the collector discards that answer.
- **The body is JSON in the collector's own schema**, not the source's. A
  top-level object with `identified` (boolean, **required**), `findings` (list,
  optional) and `detail` (string, optional) — **and no other field**, because a
  field the reader does not know is refused rather than dropped. Each finding
  carries `advisory_id`, `affected_range` and `match_confidence`, all required
  and non-blank, plus optional `severity` and `fixed_range`. `match_confidence`
  must be `exact-version`, `exact-range` or `name-only`. Every value must fit its
  column (advisory 128 characters, severity 256, each range 1024) and carry no
  control character; one that does not causes the **whole document** to be
  refused, so that package records `error` until the source changes.
- **`found` is yours to set.** `False` means the *locator* does not exist —
  a withdrawn or misconfigured source. "I have no record of this package" is
  **not** that: it is a document answering `identified: false`, which the
  collector records as `unknown`. Conflating them writes the one row on this
  table a reader could mistake for a clean answer.
- **Every failure is raised as `TransportError`.** The collector base catches
  that class and nothing else, so any other exception escapes **before an
  evidence row is written** — the one way to get no row at all out of this
  collector. Convert your client library's exceptions, including the ones raised
  while building a request.
- **Never answer `304`/`not_modified`.** This collector declares no response
  cache, so it sends no validator and holds no body to replay; a `304` fails the
  run with nothing to read.

**2. Declare it, guarded.** `AppConfig.ready()` is Django's to call and a second
`django.setup()` in one process calls it again, while a second declaration is
refused — so the unguarded form aborts boot. Use the shape the inventory adapter
already uses, in `collectors/apps.py`'s `ready()`:

```python
from conda_sentinel.collectors.advisories import declare_advisory_source
from conda_sentinel.collectors.advisories import declared_advisory_source

if not isinstance(declared_advisory_source(), YourAdvisoryAdapter):
    declare_advisory_source(YourAdvisoryAdapter())
```

A second declaration of a *different* adapter is still refused, so "which
advisory database does this component read" can never be answered by import
order.

**3. License the call in the audit.**
`tests/unit/django_apps/test_vulnerability.py` fails on a
`declare_advisory_source(...)` call anywhere under `src/` — that audit is what
proves *this* repository ships no source, and every application lives under
`src/`, so declaring one makes it fail by design. Add the declaring module to
`MODULES_PERMITTED_TO_DECLARE_AN_ADVISORY_SOURCE` in that file, in the same
change. The set ships empty; adding to it is the pull request that records which
source this deployment chose, which is the point.

**What an undeclared component looks like, and what to alert on.** The collector
is registered and scheduled like every other, but:

- its sweep **selects no package at all**, so the daily dispatch records one
  `succeeded` run saying the selection was empty rather than enqueueing ten
  thousand tasks that cannot run;
- a collection triggered by hand raises before the run ledger opens, with a
  message naming `declare_advisory_source`, so there is **no ledger row and no
  evidence row** — a component with no source has not looked, and nothing records
  that it did.

A `succeeded` dispatch over an empty selection is byte-identical to a healthy day
on which nothing needed collecting, so **the component says so in the log
instead**. Alert on the structured event:

```
event = "vulnerability.no_advisory_source"   level = warning
```

It is emitted wherever the selection is asked for — once at every process start,
because the boot reconciliation asks every registered collector for its
selection, and once per daily dispatch. It is the only signal that this component
has stopped looking for vulnerabilities, and it is also what fires if somebody
withdraws a declared source from a running process.

**Egress is whatever your adapter needs**, and this component names no host of
its own: nothing under `src/` spells an advisory API's name, which is the whole
point of the seam.

**The declared allowance is thirty requests a minute, and you will need to raise
it.** The base charges `1 + retries` per collection, which at the declared three
retries is **four requests per package** — so thirty a minute is **7.5 packages a
minute**, 450 an hour, and `CPM-NFR-1`'s ten thousand packages in about **22
hours of a 24-hour cadence**. That fits, with almost nothing spare and no margin
for a retry storm. It is a courtesy bound rather than a ceiling any source
published, because no source is chosen; reconcile it against what your source
actually publishes, in the same change that declares the adapter.

**A package the collector could not match records `unknown`, never clean.** Four
different things produce that row and each says which in `detail`: the source was
read and matched nothing; the source could not identify the package; this
package's identity names no version to match a range against; or its identity
names no package URL at all. A row saying "nothing matched" is **not** a
statement that the package is safe, and the one row that could be mistaken for
one — the source reporting that the locator itself does not exist — carries a
sentence saying so.

**Nothing here ranks or evaluates anything.** The severity is the string the
source stated, the ranges are the expressions the source wrote, and whether the
version sits inside a range is a question the *source* answered. Turning any of
that into a verdict is a policy pass with a versioned rule set, and that pass is
"The vulnerability policy" below: it reads these rows as of a run's cut-off and
derives one status, one KEV membership and one risk level per package.

**A misconfiguration and a transient failure leave this task the same way.** An
undeclared advisory source raises out of `cpm.collect.vulnerability` like any
other error, and Celery cannot tell a permanent refusal from something a retry
would fix. Do not enable a retry policy for this task before a source is
declared.

## The KEV collector ships with no catalog source, and cross-references nothing until you declare one

`cpm.collect.kev` takes the advisories the vulnerability collector already
recorded against a package and asks a KEV catalog which of them are known to be
exploited (`CPM-FR-12`). Each row it writes carries two facts and no others: a
**link to the vulnerability finding it derives from**, as a real foreign key, and
the **date the catalog says it added the advisory**.

**It is a second source and a second declaration.** An advisory database and a KEV
catalog are different products with different licences, so the two slots are
independent: declaring an advisory source does not declare a KEV source,
withdrawing one leaves the other collecting, and each fails under its own name.
Which KEV sources are available and licensed for use is the same open product
question that blocks the advisory source, so — as there — the mechanism ships and
the source does not.

**A KEV row does not say a package is being exploited, and it does not say it is
safe.** Read the three answers apart:

| The row says | What it means | Links to a finding? | Carries a date? |
|---|---|---|---|
| `listed` | this catalog lists an advisory we already recorded against this package | always | when the catalog stated a readable one |
| `not_listed` | this catalog was read, uses this identifier's scheme, and does not list that advisory — **not** that the advisory is harmless or the package is clear | always | never |
| `unknown` | nothing was established — either the catalog states no identifier in this advisory's scheme, or this package had no current advisory to cross-reference at all | only in the first of those two | never |
| `not_found` | the KEV source reports that the **catalog itself** does not exist — a withdrawn or misconfigured source, **not** a package with nothing exploited against it | never | never |
| `error` | the look failed: the adapter raised, the allowance was spent, or the catalog could not be read | never | never |
| `not_applicable` | never written; the table refuses it outright | — | — |

**`not_found` is the row to be most careful with.** The base finalizes that run
`succeeded`, because the source answered — so a withdrawn catalog produces a full
day of clean-looking runs. The row says so in its `detail` and the component emits
`kev.catalog_absent`; alert on it.

`unknown` is the other one. Its `detail` always says which of four things happened:
no advisory source is declared; the vulnerability collector has not observed this
package; it observed it and matched nothing; or everything it matched is now older
than this collector calls current. Only the third is a statement about the package.
It is **never** a statement that nothing against this package is being exploited.
What a KEV hit *means* for a package is a policy question, and "The vulnerability
policy" below is where it is answered: the derived row carries KEV membership in a
column of its own, over three values rather than this table's two, and it is never
an input to the risk level.

**Declaring a catalog source is a change to this repository, not a setting**, on
exactly the terms the advisory source is. Three things have to happen together:

**1. Write the adapter.** It satisfies `Transport` — one method, `fetch(locator,
*, headers=None) -> Payload` — and owes rather more than that:

- **The locator it is handed is `kev://declared-source/catalog`, and it is the same
  string every time.** The catalog is one document about advisories rather than a
  question about a package; which of *our* advisories get cross-referenced against
  it is this product's own evidence and is not something an adapter is told.
- **The body is JSON in the collector's own schema**, not the catalog's. A
  top-level object with `entries` (a list, optional) — **and no other field**,
  because a field the reader does not know is refused rather than dropped. There is
  no document-level `detail`: a catalog says nothing about our package. Each entry
  carries `advisory_id` (required, non-blank), an optional `aliases` (a list of the
  other identifiers the same advisory is issued under, at most 32), and an optional
  `date_added`. Every value must fit its column and carry no control character; one
  that does not causes the **whole document** to be refused, so every package
  records `error` until the source changes. One advisory reachable under two
  entries — by its own spelling or through an alias — is refused for the same
  reason: two entries could carry two dates, and choosing between them would be an
  invention.
- **State the aliases, or `not_listed` is not trustworthy.** Matching is an exact
  comparison of identifiers, folded for case, against the identifiers and aliases
  your catalog states. A finding this product recorded under `GHSA-…`, against an
  advisory your catalog lists as `CVE-…` with no alias stated, would otherwise read
  as an advisory the catalog does not list. The collector will not write that: where
  your catalog states **no identifier at all in the finding's scheme**, it records
  `unknown` and says why, rather than the reassuring value. That is the safe
  direction and it is also a quieter table than it should be — a CVE-only catalog
  against a GHSA-heavy advisory source answers `unknown` for everything. Stating
  aliases is how you turn those into real answers.
- **`date_added` must carry an offset, and fall inside 1999–2200.** A value that
  does not parse, that parses to a naive instant — which is what a bare
  `2024-02-06` does — or that falls outside that window is recorded as *missing*,
  with the row saying which of the three it was, rather than guessed at or stored
  as an instant nothing could read back. The advisory is still recorded as listed;
  only the date is absent. Emit `2024-02-06T00:00:00Z` rather than `2024-02-06` if
  you want the date kept.
- **`found` is yours to set.** `False` means the *catalog locator* does not exist —
  a withdrawn or misconfigured source — which the collector records as `not_found`
  with a caveat saying so. A catalog that exists and lists nothing is not that: it
  is a document with no entries, which records `not_listed` for every current
  advisory.
- **Every failure is raised as `TransportError`.** The collector base catches that
  class and nothing else, so any other exception escapes **before an evidence row is
  written** — the one way to get no row at all out of this collector.
- **Never answer `304`/`not_modified`.** This collector declares no response cache,
  so it sends no validator and holds no body to replay.
- **Hold the catalog yourself.** The base is per-package, so your adapter is asked
  once per package for the same document — ten thousand times a day at
  `CPM-NFR-1`'s inventory. Fetching the catalog over the network on each of those
  is what will hurt; caching it inside the adapter, with whatever freshness your
  source's own publication schedule justifies, is the intended shape. The collector
  deliberately does not cache it: a remembered security answer is the one this
  product should be slowest to replay.

**2. Declare it, guarded**, in `collectors/apps.py`'s `ready()`, exactly as the
advisory source is declared:

```python
from conda_sentinel.collectors.kev import declare_kev_source
from conda_sentinel.collectors.kev import declared_kev_source

if not isinstance(declared_kev_source(), YourKevAdapter):
    declare_kev_source(YourKevAdapter())
```

A second declaration of a *different* adapter is refused, so "which catalog does
this component read" can never be answered by import order.

**3. License the call in the audit.** `tests/unit/django_apps/test_kev.py` fails on
a `declare_kev_source(...)` call anywhere under `src/`. Add the declaring module to
`MODULES_PERMITTED_TO_DECLARE_A_KEV_SOURCE` in that file, in the same change. The
set ships empty; adding to it is the pull request that records which catalog this
deployment chose.

**What an undeclared component looks like, and what to alert on.** The collector is
registered and scheduled like every other, but its sweep **selects no package at
all**, and a collection triggered by hand raises before the run ledger opens with a
message naming `declare_kev_source` — so there is no ledger row and no evidence row.
A `succeeded` dispatch over an empty selection is byte-identical to a healthy day,
so the component says so in the log instead. Alert on the structured event:

```
event = "kev.no_kev_source"   level = warning
event = "kev.catalog_absent"  level = warning
```

The first is emitted once per daily dispatch, and it is also what fires if somebody
withdraws a declared source from a running process. The second is the *declared*
source's silence: an adapter answering `found=False` writes `not_found` under a
**`succeeded`** ledger row, because the source answered — so a withdrawn or
misconfigured catalog produces a full day of clean-looking runs with nothing else
in the log. It is emitted once per package rather than once per dispatch, which is
noisier by design: it is the failure a reader of the rows would most easily mistake
for an answer.

**The declared allowance is thirty requests a minute, and you will need to raise
it** — the same arithmetic the advisory source's section gives, and it applies
twice: the base charges four requests per package, which is 7.5 packages a minute
and about 22 hours for `CPM-NFR-1`'s ten thousand. The vulnerability and KEV sweeps
run on the same day -- the KEV one an hour behind -- and each spends its own
allowance against its own source.

**This collector reads the vulnerability collector's evidence table**, which is the
one place in this product where a collector reads a table it does not write. It is
read-only, it is one table, and it is what makes the link on every row possible:
`CPM-FR-12` is defined as a cross-reference of what `CPM-FR-11` recorded.

**"Current" is bounded in time as well as by advisory.** Only the newest
determinate finding per advisory is cross-referenced, and only while it is no older
than this collector's own freshness target — two days. That bound is what *retires*
an advisory: the vulnerability collector records "nothing matched today" as one row
naming no advisory, so nothing there ever says an advisory has gone, and without
the bound a single match would be cross-referenced for the life of the package. A
run that excluded anything says so, in the `detail` of every row it writes.

The practical consequence is worth stating: **if the vulnerability sweep stops
running, the KEV table goes to `unknown` within two days** rather than repeating
yesterday's answer indefinitely. That is the intended behaviour and it is what the
freshness read on `vulnerability_findings` is telling you at the same time.

At most 2,000 cross-references are recorded for one package in one collection; a
package with more current advisories than that records `error` rather than a
partial answer.

## The licence collector reads the channels you already declared, and refuses to guess

`cpm.collect.license` asks each monitored conda channel what licence it states for
a package and records **two columns side by side**: the raw string exactly as the
channel stated it, and the SPDX expression this product normalized it to
(`CPM-FR-13`). Beside them it records the *method* — how the expression was
arrived at — and the channel the answer came from.

**It needs no source declaration of its own.** Unlike the two security collectors
above it, this one has no adapter slot: a licence is stated by the channels
`CPM_MONITORED_CHANNELS` already names (see the published-package section above),
and it reads that same declaration. It ships observing nothing for exactly the
reason the published-package collector does — the setting is empty until you
declare it — and starts observing on the next tick once you do.
`CPM_MONITORED_PLATFORMS` is **not** read: a licence is a property of the package
a channel serves rather than of a build, so there is one row per channel and no
platform column.

**It asks the source itself rather than reading the published-package table.**
The document it reads is the same `https://api.anaconda.org/package/<channel>/<name>`
the published-package collector reads, for a different fact. A collector never
reads another collector's evidence table, so this is a **second call to the same
host** rather than a shared read. Practically: the two sweeps each spend their own
allowance against `api.anaconda.org`, and the licence dispatch is offset by two
hours so they do not spend them at the same instant.

**Read the five answers apart:**

| The row says | What it means | Raw column | Normalized column |
|---|---|---|---|
| `normalized` | this channel stated a licence this product recognises, and the expression beside it is what that licence is in SPDX | the channel's own words | the SPDX expression, with `detection_method` naming how |
| `unknown` | one of four things, and `detail` says which: the channel stated **no** licence at all; the document carried no `license` field at all; the channel stated one this product will not normalize without guessing; or it stated one carrying a line break or a tab, which this product will not read as an identifier | the channel's own words, whenever it stated any | always blank |
| `not_found` | this channel does not serve the package at all — an absence from *this channel*, not a package with no licence | blank | blank |
| `error` | the look failed: the channel raised, the allowance was spent, or the document could not be read | blank | blank |
| `not_applicable` | never written; the table refuses it outright | — | — |

**There is no compliance verdict on this table and there is no column for one.**
Nothing here says a licence is allowed, denied, permissive or acceptable. Whether
a licence is acceptable is a policy question over a rule set that is versioned
data, and no such policy exists yet. An `unknown` row is **not** a problem with the
package — it is a review item, and it is the row a licence review queue will
select when one exists.

**Normalization is data, and it refuses what it does not recognise.** The
recognised set lives in `collectors/spdx.py` as a readable table of identifiers and
the other spellings each one is written under — `MIT`, `mit`, `The MIT License`;
`Apache 2.0`, `Apache License, Version 2.0`; `BSD-3`, `new bsd`. Matching on a
*licence name* is case-insensitive and collapses runs of spaces. Two or more
recognised licences joined by a single `AND` or a single `OR` — in upper case, which
is what SPDX mandates — are normalized operand by operand into one expression on one
row; `MIT OR Apache-2.0` is a single statement about a single package and is never
split into two rows.

What it deliberately does **not** recognise is as important:

- **The bare family names.** `BSD`, `GPL`, `LGPL`, `Apache`, `Other`, `Public
  Domain`, `See LICENSE file`. `BSD` alone is two-clause or three-clause and the
  difference is whether an advertising clause binds; `GPL` names no version and no
  `-only`/`-or-later` disposition. Every one of these is really stated on conda
  channels, and every one of them is a review item rather than a licence.
- **A version with no disposition** — `GPL-2.0`, `GPLv3`, `LGPL-2.1`, `AGPL-3.0`.
  These state a version and still say nothing about whether the grant is that
  version *only* or that version *or later*, which is the whole of the difference
  between a licence a downstream may relicense forward and one it may not. The
  recognised GNU entries are the current unambiguous spellings — `GPL-3.0-only`,
  `GPL-3.0-or-later`, and the same pair for `GPL-2.0`, `LGPL-2.1`, `LGPL-3.0` and
  `AGPL-3.0` — and nothing else.
- **Abbreviations that name two licences** — `psf` is `Python-2.0` (the CPython
  licence, which is what a conda channel almost always means) or `PSF-2.0`, and both
  identifiers are recognised under their own names. `freebsd` is `BSD-2-Clause-Views`
  rather than `BSD-2-Clause` and carries an extra clause, so it is a review item
  rather than an alias for a licence with one fewer obligation.
- **Lower-case operators** — `MIT or Apache-2.0`, `MIT and Apache-2.0`. SPDX mandates
  upper case, a conda `license` field is prose at least as often as an expression,
  and prose "A and B" usually offers a *choice* while SPDX `AND` binds both sets of
  obligations at once. `MIT OR Apache-2.0` is recognised.
- **Parenthesised expressions** — `(MIT OR Apache-2.0) AND BSD-3-Clause` — because
  their meaning depends on a precedence this product would have to invent.
- **Mixed operators in one flat expression** — `MIT OR Apache-2.0 AND BSD-3-Clause` —
  for the same reason and no weaker one: it needs the same precedence, without the
  punctuation that announces it.
- **`WITH`** — `Apache-2.0 WITH LLVM-exception` — because the right operand is an
  *exception* identifier from a separate SPDX list this product does not carry.
- **Multi-token operands inside an expression** — `Apache 2.0 OR MIT` — because
  deciding where the first operand ends is a guess. `Apache-2.0 OR MIT` states the
  same thing unambiguously and is recognised.
- **A licence stated across two lines** — anything carrying a newline, a tab or
  another control character. PostgreSQL stores these perfectly well, so they are
  recorded verbatim as `unknown` review items rather than refused: refusing one would
  fail the whole package's run, write `error` rows for every *other* channel without
  asking them, and throw away the string a reviewer needs.

All of these record `unknown` with the raw string preserved and the tokens that
stopped it named in `detail`. **Extending the recognised set is a change to
`RECOGNISED_LICENSES` in `collectors/spdx.py` plus a test case** — a table a
reviewer reads rather than a chain of branches — and each addition is a decision
that one spelling means exactly one identifier.

**The raw string is recorded verbatim on every row that has one**, including the
rows normalization refused. That is the whole point of the pair of columns: a
reviewer compares them to judge what normalization did, and it matters most on the
rows that failed — an `unknown` row carrying the raw string is something somebody
can act on, while one carrying nothing is an absence of information. The table's
constraint is deliberately asymmetric to permit this: the expression and the
method are required on a `normalized` row and forbidden elsewhere, and the raw
column is permitted everywhere.

**Values are refused rather than truncated, and the refused set is narrow.** What
fails the run for that package with an `error` row is a licence wider than the
512-character column, one carrying a NUL byte, or one carrying a lone UTF-16
surrogate — the last two because PostgreSQL's driver rejects them from inside itself,
past every guard, so an unrefused one writes no row at all and repeats daily. A
truncated licence is a *different* licence, and this table is append-only. Everything
else a channel can state is recorded: a newline, a tab or another control character
makes the row a `unknown` review item carrying the raw string verbatim, not a failed
run.

**Channels are never merged.** Two monitored channels stating different licences
for one package are two rows, each naming its own channel and its own locator.
Which of them is right is not a question this collector answers. A channel that
fails is an `error` row for that channel beside the rows the channels that
answered earned; a channel that does not serve the package is a `not_found` row
beside them.

**What it costs, and what the allowance actually counts.** One call per monitored
channel per package per day, each retried once. The declared allowance is thirty
requests a minute — but read the next sentence before you size anything against it.

**Only the first channel's call is charged, and only the first channel's response is
cacheable.** The limiter is acquired once per collection, before the first channel,
and charged `1 + retries` = **2**; the calls for channels two onward are issued
afterwards and are not counted. So at the declared thirty a minute the limiter
permits **15 packages a minute**, and `CPM-NFR-1`'s ten thousand packages take about
**11 hours** — it does fit inside a day, with little room spare. The *real* outbound
load is higher than the charge: with four channels declared, one collection sends up
to **8 requests** to `api.anaconda.org` and is charged for 2. The same asymmetry
applies to the response cache: the declared seven-day TTL covers the base's one call,
so channels after the first carry no validator and re-transfer their whole document
every run. Both are recorded as deferred defects on `CPM-CURRENCY-S04` and
`CPM-SECURITY-S03`; the arithmetic is settled by the story that first sweeps at
volume.

Raise the allowance against what `api.anaconda.org` actually tolerates — remembering
that the real send rate is up to four times the charged one, and that the
published-package sweep is asking the same host on the same day. At most four
channels may be declared, because every one of them is a retried call inside a task
the platform kills at sixty seconds.

## The static Python 3.14 collector infers, and never verifies

`cpm.collect.python_readiness` reads a project's **published metadata** and records
what that metadata *claims* about Python 3.14 (`CPM-FR-14`). It is the cheap half of
a two-part requirement: this pass tells you where the expensive half is worth
spending, and the expensive half — a real build and import — is a separate,
optionally triggered capability on the `verify` queue that does not exist yet.

**Nothing here builds, imports or runs anything.** One HTTPS GET to
`https://pypi.org/pypi/<project>/json`, two fields read out of it, no subprocess.
Read every row on this table as "the project said so", never as "we tried it".

**The state values say so themselves, and that is deliberate.** The determinate
values are `inferred_compatible` and `inferred_incompatible` rather than
`compatible` and `incompatible`, because a verified result about the same package
and the same Python is coming and the two must be distinguishable wherever a status
is rendered. If you build a queue, a report or an export over this table, carry the
value verbatim — shortening it to `compatible` is exactly the confusion the naming
prevents.

**Read the six answers apart:**

| The row says | What it means |
|---|---|
| `inferred_compatible` | the project's own `Requires-Python`, or a `Programming Language :: Python :: 3.14` classifier, admits this Python. `deciding_signal` says which said so — `requires-python`, `classifier`, or `both` |
| `inferred_incompatible` | the project's `Requires-Python` **cannot** admit any 3.14 release. `requires_python` carries the specifier verbatim so you can check the claim |
| `unknown` | one of five things, and `detail` says which: the project declared **neither** signal (the commonest row on this table); it enumerated Python versions in its classifiers and did not name 3.14; its two signals disagree; it declared a specifier in a shape this product will not read; or it declared one too wide for the column that records it, in which case the row carries no specifier and says so |
| `not_found` | the release ecosystem does not know the project at all — an absence from the index, not a project that declared nothing |
| `error` | the look failed: the source raised, the allowance was spent, or the document could not be read |
| `not_applicable` | identity established that this package has **no release ecosystem**, so there is no Python metadata to assess. `detail` names identity as the reason; this is the only way the state is ever written |

**A silence is `unknown` and never `inferred_incompatible`. This is the sentence to
read twice.** Most projects have not declared 3.14 support. A project that says
nothing has not said "no", and treating it as though it had would mark most of your
inventory incompatible on no evidence — and would send verification effort exactly
where it is least warranted. Expect `unknown` to be the majority answer, and treat
it as "we do not know yet", which is what it says.

**A `Requires-Python` this product will not read is `unknown` too, with the
specifier preserved and the reason recorded.** What is read is the ordinary
grammar — `>=3.9`, `>=3.9,<4`, `==3.12.*`, `!=3.13.*`, `~=3.9`, and combinations of
them over plain dotted numeric releases. What is **not** read, because answering it
needs version-ordering rules this product has deliberately not decided:

- **`===` arbitrary equality**, which PEP 440 defines as a comparison of *strings*
  rather than of versions, so whether it admits 3.14 depends on how 3.14 happens to
  be spelled.
- **Epochs** — `>=1!3.9`.
- **Pre-, post-, development- and local-version segments** — `>=3.9rc1`,
  `>=3.9.post1`, `>=3.9.dev0`, `>=3.9+local`.
- A specifier carrying more than 32 clauses or a release of more than 8 segments,
  both of which are bounds rather than judgements.
- A specifier **longer than 128 characters**, which is what the column recording it
  holds. This one is `unknown` for a different reason from the rest: the shape is
  readable, but the row could not carry the claim it rested on. The specifier is
  left blank rather than truncated — a truncated specifier is a different specifier
  — and `detail` says so. It is deliberately **not** an `error`: the source answered
  perfectly well, and calling that a failed look would have the row say looking
  broke when only recording did.

Every one of these records `unknown` with the reason in `detail`, and emits a
`python_readiness.unreadable_specifier` log event so a shape that turns out to be
common is visible without aggregating a column.

**The question is about the 3.14 *series*, not one patch release.** `>3.14` records
`inferred_compatible`: it excludes `3.14.0` and admits `3.14.1`, and a project that
runs on 3.14.1 is ready for 3.14. `<3.14` records `inferred_incompatible`, because
nothing in the series satisfies it.

**A classifier list states what a project claims and never what it denies.** A
project that lists `3.10` through `3.13` and omits `3.14` has said nothing about
3.14 — so a classifier omission alone is never `inferred_incompatible`. Where a
specifier admits 3.14 and the classifiers enumerate versions without naming it, the
row is `unknown` and `detail` records the **disagreement**: this collector does not
rank one signal above the other, because doing so would be deciding a claim about
somebody else's package in the one column a policy reads first.

**`Programming Language :: Python :: 3` is not an enumeration.** The umbrella
classifier is a *superset* claim — "this project supports Python 3", which contains
3.14 — rather than a list of minor versions that left 3.14 out, so a project
declaring only it (with or without `:: 3 :: Only`) has enumerated nothing and its
specifier has nothing to disagree with. That is the commonest published shape there
is: `requires-python = ">=3.9"` beside `:: 3` and `:: 3 :: Only` records
`inferred_compatible` on the specifier, not a disagreement. Only a **dotted**
classifier — `:: 3.12` — makes the list an enumeration.

**It asks the source itself rather than reading the PyPI release table.** The
document it reads is the same `https://pypi.org/pypi/<project>/json` the PyPI
release collector reads, and that collector already stores a `requires_python`. A
collector never reads another collector's evidence table, so this is a **second call
to the same host** rather than a shared read — and it is necessary anyway, because
the *classifiers* are the second static signal and no collector stores them.
Practically: the two sweeps each spend their own allowance against `pypi.org`, and
the readiness dispatch is offset by three hours so they do not start at the same
instant.

**Which packages it asks about, and the one thing it will not do.** It asks about a
package whose release-ecosystem mapping identity recorded as `established` for
PyPI, and it writes AC 2's `not_applicable` row for one recorded `not_applicable`.
It asks about **nothing else** — a package whose mapping is `unknown`, `error` or
`not_found`, one with no mapping row at all, and one established for some other
ecosystem are all packages identity has not given this collector a project to read.
Those are **not** offered by the sweep, so your ledger does not fill with failed
runs; they simply have no readiness row, and every read surface reports them
`unknown` for want of an observation. A manual recollection of one of them fails
with a message saying the compatibility question is *unanswered rather than
inapplicable* — which is the distinction the `not_applicable` state exists to keep.

**No readiness verdict is on this table.** Whether a package is *ready* — and which
kind of evidence produced that answer — is a policy question over this table and the
verification table beside it, and no such policy exists yet.

**What it costs.** One call per package per **week**, retried per the shared retry
policy. The declared allowance is sixty requests a minute, which at `1 + retries`
per collection is fifteen packages a minute — so `CPM-NFR-1`'s ten thousand packages
take about eleven hours of wall time, comfortably inside a weekly cadence. The
response cache holds an answer for thirty days, which is longer than the cadence on
purpose: a TTL inside the cadence would make the cache inert and re-transfer a
document that lists every file of every release. Raise the allowance against what
`pypi.org` actually tolerates, remembering the PyPI release sweep is asking the same
host daily and spending its own.

## Python 3.14 verification runs nothing until you declare what runs it

`cpm.verify.py314_build` is the expensive half of `CPM-FR-14`: an actual build and an
actual import of a package under Python 3.14, recorded with the platform and the
architecture it ran on and a reference to its log. Everything the section above
records is what a project *claimed*; this is what a build *did*.

**Nothing ships that can run one.** Verification means executing somebody else's
build script and importing somebody else's code, and nothing in this product's
requirements or architecture decides how that is isolated — not a sandbox, not a
container, not a resource bound, not a network posture. So this component ships the
mechanism and no execution backend, exactly as it ships the vulnerability collector
with no advisory source. Every trigger raises until you declare one:

```
VerificationBackendError: no Python 3.14 execution backend is declared, so there is
nothing to build this package with.
```

That refusal happens before any run is recorded, so an unconfigured component leaves
no ledger row and no evidence row claiming to have verified anything. **This is the
shipped state and it is not a misconfiguration** — the component starts, every other
collector runs, and nothing about verification is quietly on.

**It is triggered, and it is never swept.** No beat entry names it, no schedule can
sweep it, and the dispatch refuses it by name if one tries. A verification happens
because somebody asked for it, one package at a time:

```python
from conda_sentinel.collectors.tasks import verify_py314_build

verify_py314_build.apply_async(kwargs={"package_id": 42})
```

There is no `force` argument, because there is no observation window to bypass: the
trigger *is* the decision to run, and asking twice writes two rows rather than
silently reusing the first.

**It runs on the `verify` queue.** That follows from the task's declared name and
from nothing else. The shipped `worker` process already drains it — its `-Q` names
`celery,collect,policy,verify,export` — so nothing needs configuring for a trigger to be
picked up. If you run a worker with a `-Q` of your own, keep `verify` in it: routing
without consumption is inert, and the component would accept triggers and never run
them. The whole reason the queue is separate is that a five-minute build must not
share a worker with the daily security sweep; if you split the processes, give
`verify` its own worker and that separation becomes real rather than nominal.

**Read the six answers apart:**

| The row says | What it means |
|---|---|
| `verified_compatible` | a build **and** an import succeeded, on the platform and architecture the row names. Proof, and proof about *that runner* — not a claim about every platform |
| `verification_failed` | verification ran and did not produce a working build, on the platform the row names, with `log_reference` pointing at why. A **result**, not a failure of this product — and deliberately not called `verified_incompatible`, because builds fail for reasons that are not the interpreter |
| `error` | the backend itself raised, the allowance was spent, or its answer could not be read. Nothing was verified and there is no log to open |
| `not_found` | the backend reports it cannot obtain the artifact at all — an absence from wherever it fetches from, not a package that fails to build |
| `unknown` | reserved by the shared vocabulary; no shipped path writes it |
| `not_applicable` | identity established that this package has **no release ecosystem**, so there is nothing to build. `detail` names identity as the reason; this is the only way the state is ever written |

**A failed build and a broken backend are different rows, on purpose.** The first
carries a log reference and leaves the run `succeeded` in the ledger — the
verification worked and the answer was negative. The second is an `error` row and a
`failed` run. Alert on the second; triage the first.

**The state values are not the static collector's, and that is the point.**
`verified_compatible` and `inferred_compatible` are deliberately different strings
because `CPM-FR-14` requires proof and inference to be distinguishable wherever a
status is rendered. If you build a queue, a report or an export over these tables,
carry the values verbatim — collapsing either to `compatible` is exactly the
confusion the naming prevents.

**Every determinate row says where it ran, and the database enforces it.** A backend
that answers a verdict without a platform, an architecture and a log reference has
its answer refused: the row is `error` and says what was missing. A build succeeds on
a platform, and a verdict with nowhere attached would be a claim about every platform
made from an execution on one.

### What declaring a backend commits you to

An execution backend is a `Transport` — the same substitution seam the inventory
adapter and the advisory source use — declared in an `AppConfig.ready()`:

```python
from conda_sentinel.collectors.verification import declare_verification_backend

declare_verification_backend(YourBuildRunner(...))
```

One slot. A second declaration is refused rather than allowed to replace the first,
because which machine runs somebody else's build is not a question that should be
answered by import order.

It is handed `py314-verify://declared-backend/<the package's purl>` and must answer a
JSON object carrying exactly `verified` (a boolean), `platform`, `architecture` and
`log_reference` (non-blank strings), plus an optional `detail`. Any other field is
refused rather than ignored — a backend that grows a `partial` flag has changed what
its verdict means. `found: false` means the artifact cannot be obtained; it does not
mean the build failed. Every failure must be raised as `TransportError`.

**The hard constraint to design around is the inherited soft time limit.** Celery
kills a task at `CELERY_TASK_SOFT_TIME_LIMIT`, the platform fixes that value, and
this product does not raise it. The `verify` queue keeps a long build from *starving*
the daily sweeps; it does not give the build more time. So a backend whose `fetch`
blocks for the length of a real build will be killed with no row written. A backend
that works drives the build somewhere else — a CI run, a build cluster, a container
scheduler — and answers about a run that has **already finished**. Plan the trigger
as two steps if you need to: something that starts the build, and a trigger of this
task once it has.

**What it costs.** One build per trigger, no automatic retry — a retry here would
re-run somebody's build over a failure nobody has looked at — and a declared allowance
of six verifications a minute across the whole component. That allowance is a bound
on a trigger loop rather than a throughput target: a build takes minutes, so reaching
it means triggers are arriving far faster than the backend can serve them. A
verification result is read as current for thirty days, which is a **provisional**
number: PRD Open Question 7c has no cadence to derive a freshness target from, and
this is a target measured from the request. Revisit it once you have a backend and
know how fast its answers actually go out of date.

**No readiness verdict is on this table.** Whether a package is *ready*, and which
kind of evidence produced that answer, is a policy over this table and the static one
beside it, and no such policy exists yet.

## The identity resolver reads two public indexes, and is the one collector that writes identity

`cpm.collect.resolve_identity` resolves a package's mappings (`CPM-FR-1`) by
asking two hosts, in a fixed order. First conda-forge's **feedstock-outputs index**
at `https://raw.githubusercontent.com/conda-forge/feedstock-outputs/main/outputs/…`,
one small JSON file per package naming the feedstocks that build it; then PyPI's
project document at `https://pypi.org/pypi/<name>/json`, for whether a project
exists under the package's name and which of its `project_urls` is the source
repository. It sends **no credential** to either — both are public and take none —
and its declared allowance is sixty requests a minute, the same courtesy bound the
PyPI collector declares.

**The index is the base's call and PyPI is the second, and the order has a cost
worth knowing.** The collector base makes exactly one fetch and, when that
document is absent, writes the `not_found` row itself without reaching the
collector's own code. On a conda watchlist a package is far more often on
conda-forge and absent from PyPI than the reverse, so the index goes first — which
means **a package absent from conda-forge's index has no PyPI identity resolved by
this collector**. Its run records `not_found`, nothing is written to the package,
and it is offered again next cadence. The `detail` on that row says PyPI was not
asked.

**The second call is neither cached nor charged to the allowance.** The base
remembers and revalidates the index answer only, and charges `1 + retries` once,
before the first call. The PyPI call inside the collector is retried by the same
transport, spends none of the counter, and is re-transferred on every run — the
same deferred gap `collectors/feedstock.py` records for its own second call. What
that call *cannot* do is fail the run: a transport failure, a `304` to a request
that carried no validator, or a document that cannot be read becomes a sentence in
`detail` beside what the index established, and the row's `pypi_asked` column
reads `False`.

**A failure to ask PyPI never lowers what an earlier run established.** When PyPI
could not be asked, the two PyPI-derived mappings — `release_ecosystem` and
`source_repository` — re-assert whatever the package currently holds: an
`established` outcome and its stored purl or URL are carried forward unchanged, and
only a mapping that was not established records `error`. A real `404`, or a
readable document naming no usable repository, still decides the mappings afresh.
So a transient outage at pypi.org cannot take a package out of the sweeps that
select on those mappings; `detail` says the mappings were re-asserted.

**This is the only collector that writes `identity`, and it does so through one
door.** Every other collector writes its own evidence table and nothing else. This
one hands what the two documents established to `identity`'s `record_resolution`
— the same function every automated resolution must use (`CPM-AD-14`) — which
writes the package's mappings, its feedstock rows and one outcome row per mapping
kind, and refuses to lower a `verified` package's confidence or to touch the pair
the package is found by. The collector itself never saves a package, mapping or
feedstock row. The confidence it claims is `inventory-derived` when anything was
established and `unmapped` when nothing was; it corrects no name.

**Its own table, `identity_resolution_snapshots`, is the evidence behind the
review queue.** One row per package per run: which locator was read, whether PyPI
answered and at which locator, which `project_urls` key won and what it normalised
to, the feedstock names the index listed, and the confidence the package held once
the recorder had finished. At a daily cadence that is **one row per package per
day, and nothing prunes it**, on the same terms every evidence table here
accumulates.

**`downgrade_refused` on a row means a `verified` package was recollected by
hand.** The sweep never offers a `verified` package — a person's identity is never
offered a downgrade — but a manual `cpm.collect.resolve_identity` for one still
runs, and the recorder records what was found while holding the confidence claim
back. The row then carries `confidence_recorded = verified` and
`downgrade_refused = True`: the findings landed, the person's verdict stood, and a
reader can tell that from a row where the claim was simply accepted.

**Which packages a sweep offers.** Every package whose package-identity confidence
is not `verified`, excluding a shell no source claims — a blank `identity_source`
or `associator_key` cannot be found by the recorder, so offering it would fail it on
every sweep.

## The full-inventory sweep: what beat fires, and what it does not do

**Eleven collectors are registered and nine of them are swept one package at a
time.** One of the other two is inventory ingestion, which reads one document naming
many packages and is deliberately absent from the schedule below. The other is Python
3.14 verification, which is *triggered* rather than swept and is absent from the
schedule for a different reason: it is not run across the inventory at all, by
design, and a schedule entry naming it is refused at the first tick. Every count in
this section is the nine unless it says otherwise. What runs those nine across the
whole inventory is one **dispatch** task, `cpm.collect.sweep`, fired by
`django_celery_beat` once per collector at the cadence that collector declares
(`CPM-NFR-1`, `CPM-FR-15`).

**A dispatch never collects.** It resolves the collector by name, asks it which
packages it can be asked about, and enqueues one ordinary per-package collection
task for each — `cpm.collect.source_release`, `cpm.collect.pypi_release`,
`cpm.collect.feedstock`, `cpm.collect.conda_package`,
`cpm.collect.vulnerability`, `cpm.collect.kev`, `cpm.collect.license`,
`cpm.collect.python_readiness` or `cpm.collect.resolve_identity`, exactly the tasks
a manual recollection uses. It makes no outbound call, writes no evidence and holds
no transaction. Every guarantee described in the sections above therefore holds
unchanged under a sweep: one package per task, one package per ledger row, one
package per transaction (`CPM-AD-23`).

**One dispatch per collector, and that is what keeps a failing source local.** A
rate-limited or misconfigured source fails its own collector's dispatch and no
other, so it costs you that surface for that cadence rather than a day of
monitoring everywhere else.

### A dispatch row's state is about enqueueing and nothing else

This is the sentence to read twice. A dispatch's ledger row says what the
dispatch did, which is *offer packages to the broker*. It says nothing about what
those collections then observed — they have not run when the row is finalized,
and a dispatch that waited for them would hold a worker slot for the length of a
rate-limited sweep.

| State | Meaning |
|---|---|
| `succeeded` | every selected package was enqueued — or the selection matched no package at all, which is a collector that was asked and answered |
| `partial` | some were enqueued and some were not; the ones that were **stay** enqueued, and `detail` names how many and why |
| `failed` | none of a non-empty selection was enqueued, or the dispatch was refused before it began |
| `skipped` | this collector's previous dispatch had not finished, so this tick offered nothing rather than queueing a second inventory |

So a sweep in which every package was enqueued and every collection then failed
leaves **one `succeeded` dispatch row above ten thousand `failed` collection
rows**, and that is the honest shape rather than a contradiction: the dispatch did
its whole job. Read the per-package rows for what was observed, and the dispatch
row only for whether the work was offered.

### The schedule is data, and it is reconciled against the collectors at start-up

`config/settings/base.py` declares one `CELERY_BEAT_SCHEDULE` entry per
per-package collector, and `django_celery_beat`'s `DatabaseScheduler` seeds its
tables from it. **What that does not buy you is changing one of these seven
intervals without a deploy**: the scheduler rewrites every entry it finds in
settings on each beat start, so a value edited in the admin lives only until beat
restarts. Cadence-as-data is what lets a *later*, unrelated schedule live in the
tables; these seven are the declaration, and changing one is a pull request.

Each collector separately declares the cadence its freshness target was derived
from. **If the two disagree, the component refuses to start**, naming both
numbers — because a weekly schedule against a daily-derived two-day target would
make the whole inventory read stale five days out of seven with every gate green
and every collection succeeding. The check runs in both directions: a schedule
entry naming a collector this component has not registered is refused too, since
it would fire into nothing on every tick; so is a collector that declares a
cadence without a selection, or a selection without a cadence.

The refusal fires from the collectors application's own `AppConfig.ready()`,
which is the hook that registers the collectors — `config/startup/stage_two.py`
evaluates the same rule as condition 11, but it runs earlier in `django.setup()`
than the registration does, so the application's own hook is the one a deployed
process meets. Either way the process does not start, and no worker picks up
work.

The shipped pairs are:

| Collector | Cadence |
|---|---|
| `source_release` | daily |
| `pypi_release` | daily |
| `feedstock` | weekly |
| `conda_package` | daily |
| `vulnerability` | daily |
| `kev` | daily, offset one hour |
| `license` | daily, offset two hours |
| `python_readiness` | weekly, offset three hours |
| `resolve_identity` | daily |

The daily entries that carry no offset fire together, from one instant, and that is
accepted rather than overlooked: a dispatch enqueues and returns, so what lands at
once is a handful of cheap tasks rather than a handful of inventories of I/O, and
the collections they enqueue are paced by each collector's own rate limiter.

**The identity resolver carries no offset, and the four sweeps that depend on it
run behind it.** `cpm-sweep-resolve-identity` is what records the mappings
`source_release`, `pypi_release`, `feedstock` and `python_readiness` select on.
The first three fire on the same daily tick it does, so on the day a package is
first ingested they select nothing for it, and on the next day they select it with
the mappings the resolver recorded the day before — one cadence behind.
`python_readiness` fires three hours later, so its lag is the same when the
resolver's tasks have not drained by then and nothing when they have. Closing the
lag would mean a fourth phased entry, which is a change to the start-up
reconciliation's own test and is not one this schedule makes. To resolve a fresh
watchlist in one sitting, dispatch `cpm.collect.sweep` with
`collector="resolve_identity"`, then **wait for the `collect` queue to drain** —
watch it in Flower, or wait until every `resolve_identity` run in the ledger has
left `running` — and only then dispatch the others. A dispatch only enqueues, so
dispatching the four straight after the resolver reproduces the race: their
selections run before the resolver's per-package tasks have recorded anything.

**Three entries carry an offset, and for different reasons.** The KEV entry is
offset by an hour because it cross-references what the vulnerability collector
wrote: firing them from one instant means a KEV run reads the previous day's
advisories. The licence entry is offset by two hours because it reads
`api.anaconda.org` — the same host the published-package sweep reads, on the same
tick, spending a separate allowance. The static-readiness entry is offset by three
hours because it reads `pypi.org` — the same host the PyPI release sweep reads,
which one day in seven falls on the same tick — and spends its own allowance
against it. The three offsets are deliberately different: two entries sharing a
phase would fire together again and buy nothing. Each offset
is a `countdown` on the entry rather than a different interval or a crontab,
because the start-up reconciliation compares an entry's interval with its
collector's declared cadence and cannot read a crontab as one.

**It reduces the window and does not close it.** At `CPM-NFR-1`'s ten thousand
packages the vulnerability sweep spends most of a day inside its own rate limit, so
an hour buys a small inventory and not a large one — a KEV row can still be
computed from an advisory observation up to a cadence old. Sequencing the two
properly would mean one dispatch chaining the other, which the sweep design
deliberately does not do (one collector failing must never stop another). Read a
KEV row's `observed_at` against the linked finding's when the lag matters.

Inventory ingestion is deliberately absent: it reads one document naming many
packages and is not swept one package at a time, so a dispatch refuses it by name.

### Which packages a sweep offers

The precondition is the collector's own, and it is what stops a sweep failing
every run. Each collector answers only about packages whose identity resolution
has reached the mapping it reads, so a dispatch offers:

| Collector | Packages offered |
|---|---|
| `source_release` | those with a source repository recorded |
| `pypi_release` | those whose release-ecosystem mapping is `established` or `not_applicable` |
| `feedstock` | those whose feedstock mapping is `established`, `not_found` or `not_applicable` |
| `conda_package` | every package — **or none at all, until you declare channels and platforms** |
| `vulnerability` | **every package** — or none at all, until you declare an advisory source |
| `kev` | **every package** — or none at all, until you declare a KEV source |
| `license` | **every package** — or none at all, until you declare channels |
| `python_readiness` | those whose release-ecosystem mapping is `established` **for PyPI** *and* whose recorded purl is a `pkg:pypi/…` one, or whose mapping is `not_applicable` |
| `resolve_identity` | every package whose package-identity confidence is not `verified` and that a source has filed under a non-blank `(identity_source, associator_key)` — a person's identity is never offered a downgrade, and a shell the recorder cannot find is never offered at all |

A package a collector would refuse is never enqueued, so its ledger does not fill
with `failed` runs for every package nobody has resolved. **Until the identity
resolver has populated those mappings, the two release sweeps and the feedstock
sweep will select few packages or none, and record that honestly as a `succeeded`
dispatch with nothing enqueued.** The resolver reads conda-forge's
`feedstock-outputs` index first and PyPI second, so a package absent from that
index has no PyPI identity resolved by it: its run records `not_found`, nothing is
written to the package, and it is offered again next cadence.

**The published-package sweep selects nothing until the two settings above are
declared**, and that is deliberate rather than a gap. Its question applies to
every package, so an undeclared component would otherwise enqueue the whole
inventory and fail every one of those collections naming the setting — ten
thousand `failed` rows a day, out of the box. Instead the selection is empty, the
dispatch records one `succeeded` row saying so, and the component says it once a
day. Declare `CPM_MONITORED_CHANNELS` and `CPM_MONITORED_PLATFORMS` and the sweep
starts observing on the next tick.

**The vulnerability and KEV sweeps select nothing until their own sources are
declared**, on the same terms and for a sharper reason: with no source, every
enqueued task raises *before* the ledger opens, so an undeclared component would
leave ten thousand tasks a day with no record at all that they ran. They are two
separate declarations: declaring an advisory source does not declare a KEV source,
and withdrawing either leaves the other collecting. The licence sweep is quiet for
a different reason -- it declares no source at all, and selects nothing until
`CPM_MONITORED_CHANNELS` is declared, exactly as the published-package sweep does.

**Once a source is declared it offers every package** — including packages whose
`primary_purl` names no version and packages with no package URL at all. That is
deliberate and it costs a collection a day for packages this product cannot
match. The alternative was worse: a narrower selection means those packages are
never enqueued, never write a row, and therefore read as *never observed* — which
is not stale, is not `unknown`, and is exactly the silence this collector's
`unknown` row exists to prevent. The row is only worth writing if a scheduled run
writes it. Those collections still spend the allowance above, so count the whole
inventory rather than the matchable part of it when you size the source's rate
limit.

### What bounds a sweep

**No manual batching, ever.** The selection is streamed from the database five
hundred rows at a time, so ten thousand packages reach the queue without ten
thousand keys existing anywhere in a worker's memory.

**Every enqueued collection expires at one cadence.** A message that has not been
consumed by the time the next tick fires is superseded — the next dispatch offers
the same package again — so it is dropped rather than queued in front of the
fresher one.

**A dispatch whose previous run is still `running` records `skipped` and offers
nothing.** Without that, a sweep that cannot be drained inside its cadence would
enqueue a second whole inventory behind the first on every tick and the queue
would grow without bound. If you see `skipped` dispatch rows accumulating, the
collector is not keeping up with its cadence: lengthen the cadence, or raise the
allowance (which means authenticating — see the deferred work on
`CPM-CURRENCY-S01` and `CPM-CURRENCY-S03`).

**The inherited sixty-second soft limit applies to the dispatch task.** Ten
thousand packages is ten thousand broker round trips inside one task, so a slow
broker can reach it. When it fires the packages already enqueued stay enqueued,
the row is finalized `partial` saying the soft limit stopped it, and the next tick
offers the whole selection again. Nothing raises the limit — `CPM-AD-9` chunks
work rather than lengthening limits.

**A dispatch does not wait, poll or chain.** It hands each task to the broker and
finishes.

**The worker must drain the `collect` queue.** `cpm.collect.sweep` routes there
along with the collections it enqueues, so a worker started without
`-Q celery,collect,policy,verify,export` accepts the schedule tick and runs nothing —
silently, because an unconsumed queue is not an error.

### Reading a partial sweep

A `partial` dispatch row names how many packages the broker refused and the first
reason. It cannot name ten thousand primary keys, so **each refusal is also a log
line** under `sweep.package_refused`, carrying the collector, the task and the
`package_id`. That is the recovery path: filter the logs for that event and that
collector to get the packages the sweep did not offer.

**Rate limits are per collector and are not yet a sweep rate.** Each of the seven
declares its own allowance, and at `1 + retries` per collection none of them
sweeps ten thousand packages inside its declared cadence today. The dispatch does
not change that arithmetic: it enqueues the work, and the per-collector limiter
refuses the calls it cannot afford, which records `error` rows rather than
exceeding the source's budget. Expect `skipped` dispatch rows and `error`
collection rows at that scale until the allowances are raised.

## The currency policy: what it compares, and what it will not

`CPM-CURRENCY-S06` adds the first policy pass. It runs inside the orchestrating
policy run rather than on a schedule of its own, so there is nothing here to
configure: no setting, no cadence, no allowance. What starts it is the
`cpm.policy.run` task (routed to the `policy` queue), and what it reads is the
evidence the four collectors above have already written. **Nothing in
`CELERY_BEAT_SCHEDULE` fires that task today** — a policy run is enqueued
explicitly, with the policy version it applies, and choosing a cadence for it is
not this component's decision to have made for you.

**It makes no outbound call of any kind.** A pass reads the evidence log and
writes derived rows (`CPM-AD-8`, `CPM-AD-9`). Nothing here is affected by a rate
limit, a credential, or a source being down — a source that was down when the
collector ran shows up as an `error` row the pass reports as `error`, and the
policy run itself is unaffected.

### It reads evidence as of the run's cut-off, never as of now

The cut-off is a `finished_at` from the collection-run ledger, chosen by
`choose_evidence_cutoff` — the newest ending that no still-running collection can
write evidence behind — and a ledger with nothing settled behind it makes the
policy run refuse rather than invent one. **A stuck collection run therefore
holds policy runs back**, deliberately: reading past it would include evidence
from a run that is still writing, and every replay would then read a different
set. Evidence written after the cut-off is not read.

### Replaying a run

`execute_policy_run` takes an optional `evidence_cutoff`. A scheduled run passes
nothing and gets the ledger's answer; **a replay passes the cut-off of the run it
is replaying**, which is on that run's `policy_runs` row and copied onto every
`package_health` row it wrote:

```python
from conda_sentinel.core.clock import SystemClock
from conda_sentinel.core.models import PolicyRun
from conda_sentinel.core.policy_run import execute_policy_run

original = PolicyRun.objects.get(pk=...)
execute_policy_run(
    policy_version=original.policy_version,
    clock=SystemClock(),
    evidence_cutoff=original.evidence_cutoff,
)
```

Passing the cut-off is what makes this a replay rather than a repetition. Without
it, any collection run that finished in between moves the boundary, the second
run reads a different evidence set, and the comparison is not the one you wanted.
A supplied cut-off is used as given and is **not** re-derived from the ledger:
the run being replayed may well sit behind a collection that has since started,
and refusing it would refuse the whole operation.

The replay writes its own rows — a new `policy_runs` row, a new
`package_currency` row per package keyed to it — and leaves the original's alone.
The rows to diff are `package_currency` filtered by the two `policy_run_id`s. A
difference means the *rules* changed, never that the sweep happened to run at a
different minute.

### The verdicts, and what each one means

Each package gets one row in `package_currency` per policy run, carrying a
verdict for each of the four surfaces and one overall. The vocabulary is `core`'s
four sentinels plus two verdicts of its own:

| Verdict | What it means |
|---|---|
| `current` | The surface states the same version as the chosen authority. |
| `behind` | The surface states a **different** version from the authority. |
| `unknown` | Nothing was observed for the surface at the cut-off; or the observation itself records `unknown`; or it is determinate but states no version (a feedstock whose recipe names its version in a way the collector does not read); or no authority could be chosen to compare against. |
| `not_found` | The source itself answered that it has no version for this package. |
| `error` | The lookup failed. Not the same as an absence, and never folded into one. |
| `not_applicable` | The surface is not one this package is published on at all — a non-Python package against PyPI, for instance. |

The overall verdict is the worst of the surfaces the question applied to, ranked
worst-first as `error`, `behind`, `unknown`, `not_found`, `current`. `error`
outranks `behind` on purpose: a surface that could not be read may be hiding a
worse discrepancy than the one that was found, so a read failure never disappears
behind a finding. Two consequences are worth knowing before you read a report:

- **A package current on the surfaces somebody looked at and unobserved on the
  rest reads `unknown` overall.** An unobserved surface outranks a determinate
  one, so a full inventory that has only ever run the source collector reads
  `unknown` across the board. That is the honest answer, not a defect: schedule
  all four collectors before treating the overall column as a health signal.
- **A surface that does not apply to the package does not take its verdict
  away.** A non-Python package reads `not_applicable` against PyPI and still
  reads `current` overall when the other three surfaces agree: an inapplicable
  surface is excluded from the package-level reduction rather than ranked in it.
  The surface column keeps `not_applicable`, which is what `CPM-FR-6` is about.
  Only a package where *every* surface is inapplicable reads `not_applicable`
  overall.

### `behind` means *different*, not *older*

This is the limit worth reading twice. The comparison is **equality against the
authoritative surface**, not a version ordering. Ordering across four ecosystems
— PEP 440, conda's own ordering, and a recipe's Jinja-set string — does not have
one grammar, and nothing in the product's requirements fixes a rule for it, so
none was invented. What follows from that:

- A surface that has moved **ahead** of the authority reads `behind`. There is no
  `ahead` verdict.
- Two spellings of one version read as two versions. `1.0` against `1.0.0`, an
  epoch, a build suffix, a PEP 440 normalisation: each reads `behind`.
- The **one** spelling difference that is reconciled is a leading `v` followed by
  a digit, plus surrounding whitespace. `v1.2.3` and `1.2.3` are the same version
  here, because `CPM-FR-7` records "the latest release **or tag**" and a Git tag
  is conventionally written that way — without this, almost every feedstock would
  read `behind` against its own source. Nothing else is normalised.

Expect false `behind` verdicts where an upstream project and a recipe spell one
version differently. The row references the exact evidence rows the verdict rests
on, so what each surface actually said is one join away.

### The authority order, and the default when a package records none

Which ecosystem is authoritative for a package is data on the package
(`CPM-AD-6`), in `packages.version_authority_order`. It is a JSON list of surface
names, best first, and the surfaces it may name are exactly:

```
source          the upstream repository's latest release or tag
pypi            the PyPI project's latest version
feedstock       the version the conda-forge recipe pins
conda_package   the version a monitored conda channel publishes
```

**Every package ships with an empty list, and that is the ordinary state.** An
empty list means no authority has been chosen, and the documented default order
is applied:

```
source -> pypi -> feedstock -> conda_package
```

The chosen authority is the **first entry of the applied order that actually
stated a version at the cut-off**. A surface earlier in the order that was
unobserved, that errored, that answered `not_found`, or that is `not_applicable`
to the package is passed over — which is what stops a package being judged
against a registry it never published to. The row records which order was applied
and whether it was the default, so a report can tell a package that chose the
default order from one that chose nothing.

`CPM-AD-6`'s stated default ends "→ internal deployed version". **This product
observes no such surface**: no collector reads a deployed inventory and no
evidence table holds one, so the entry is absent from both the vocabulary and the
default rather than present and unusable.

**A recorded order may name fewer than four surfaces**, and the consequence is
worth stating: a surface the order leaves out is still read and still recorded,
but can never be the authority. An order naming only `feedstock` on a package
with no feedstock observation therefore produces `unknown` everywhere, because
there is nothing to compare against.

**An order naming anything else fails that package's evaluation.** A misspelling
(`pypy`), a surface this product does not observe (`deployed`), a repeated entry,
or a value that is not a list at all is refused rather than quietly replaced by
the default — replacing it would write a row claiming an order the package's own
data contradicts. `CPM-AD-23` contains the refusal to one package: that package's
derived rows roll back and it keeps whatever rollup row it had, every other
package commits, the run finalizes `partial`, and the failure is logged under
`policy_pass_failed` with the package's primary key and the traceback. Nothing in
this product writes that column yet, so today it can only get a bad value from a
hand-written `UPDATE` or a data migration.

### The rollup column, and why an unmapped package always reads `unknown`

The pass contributes one column to `package_health`: `currency_status`, carrying
the overall verdict. It never writes that table — it returns the value and the
rollup writer writes it, after applying `CPM-AD-4`'s confidence gate. So a
package whose identity is `unmapped` reads `unknown` in `package_health`
**whatever the pass computed**, while `package_currency` still records the
verdict the pass actually reached. If those two disagree for a package, the
identity confidence is why, and the fix is resolution rather than anything here.

A package no policy run has evaluated reads `unknown` too: that is the column's
own default, and it is deliberately not a clean value.

### Running it, and where the result lands

There is no beat entry (see above). A run is enqueued as the `cpm.policy.run`
task on the `policy` queue, or executed in-process:

```sh
pixi run -e dev manage shell -c '
from conda_sentinel.core.tasks import run_policy
run_policy.delay("<your policy version>")
'
```

!!! warning "The quoting here is not interchangeable, and the environment is not either"

    **Outer single quotes, inner double quotes.** `pixi run` re-parses the task's
    arguments through its own task shell, which strips inner *single* quotes: the
    otherwise-natural `pixi run manage shell -c "print('hi')"` reaches Python as
    `print(hi)` and raises `NameError: name 'hi' is not defined`. Inverting the
    quotes survives the round trip; escaping the inner ones does not (they arrive
    as backslashes and raise `SyntaxError`). This is a `pixi run` quoting defect
    rather than anything about the command, and it applies to every
    `manage shell -c` invocation.

    **`-e dev`, locally.** In the `default` environment this block fails with
    `RuntimeError: Model class conda_sentinel.core.models.CollectionRun doesn't
    declare an explicit app_label and isn't in an application in INSTALLED_APPS`
    — which is the empty-settings state described under "One step per database"
    above, not a problem with the task. A deployed component needs neither
    adjustment: it supplies its own settings module, and `pixi run manage` is the
    right form there.

The `policy_version` is yours: `CPM-AD-8` makes it the version of the *rule data*
a run applies, not a version this component ships. It lands on the run's ledger
row and in every rollup row's per-domain version map, and it is the string a
replay must match.

Three places to read the result:

| Table | What it holds |
|---|---|
| `package_health.currency_status` | One value per package: the overall verdict, **after** the confidence gate. This is the read surface. |
| `package_currency` | One row per package **per run**: the four per-surface verdicts, the overall verdict ungated, the chosen authority, the order that was applied and where it came from, `detail` for anything called `behind`, and a foreign key to each of the four evidence rows the verdicts rest on. |
| `policy_runs` | One row per run: the version, the cut-off, the instants, and the ending — `succeeded`, or `partial` with a count when some packages could not be computed. |

A package that could not be computed is logged under `policy_pass_failed` with
its primary key and the traceback; the ledger row carries the count, and the log
is the only place the names are.

### `package_currency` accumulates, and nothing prunes it

One row per package per run, never updated and never deleted. At ten thousand
packages a daily run adds ten thousand rows a day, and **every relation on the
table is `PROTECT`** — to the package, to the policy run, and to each of the four
evidence rows — so nothing else can be deleted while its rows reference it.

That is deliberate: the rows are what a replay is compared against and what an
audit reads, and `CASCADE` would let an operational tidy-up of old policy runs
silently empty the evidence for every verdict still on the rollup. It also means
**there is no retention path here yet**. Deleting old runs is a decision nobody
has taken, it would have to delete `package_currency` rows before the
`policy_runs` rows they protect, and no story currently claims it. Size the
database accordingly, or run the policy less often than you collect.

### Nothing in this product can set a package's authority order

`packages.version_authority_order` is data (`CPM-AD-6`), and **no code path in
this component writes it**. Resolution does not set it, the identity override
does not cover it, and there is no admin surface for it. Every package therefore
holds `[]` and is judged on the documented default.

The only way to record an authority today is a hand-written `UPDATE`:

```sql
UPDATE packages
   SET version_authority_order = '["feedstock", "source"]'
 WHERE canonical_name = 'numpy';
```

Two things follow, and neither is guarded:

- **Such a change alters derived verdicts and leaves no audit row.**
  `CPM-IDENTITY-S05`'s identity override writes an append-only record of who
  changed what and why; this column has no equivalent, so a verdict that changed
  because somebody edited an order is indistinguishable from one that changed
  because the evidence did.
- **A bad value is not refused at write time.** The column carries a Django
  validator, which runs from a form and never from `save()` or from SQL — so an
  order naming a surface this product does not observe is stored happily and is
  refused later, when the pass reads it, which fails that one package's
  evaluation and finalizes the run `partial`.

Both are recorded as deferred work on `CPM-CURRENCY-S06`.

### What a run costs

This pass issues **five queries per package** — one read per surface, plus the
insert of the derived row — inside the orchestration's per-package loop. Nothing
batches them. At `CPM-NFR-1`'s ten thousand packages that is fifty thousand round
trips for this pass alone, and it multiplies as the remaining policy passes land.
The count is pinned by a test at three inventory sizes so a regression is
visible; making it set-based is recorded as deferred work.

### One published-package verdict, and which channel it is about

`conda_package_snapshots` holds one row per `(channel, platform)`, and this pass
produces **one** conda verdict per package. Which pair it is about is decided by
a stated key, not by luck: one sweep stamps every row it writes with the run's
single instant, so all of a package's rows tie on `observed_at`, and without a
key the answer would be whichever row was inserted last — which changes when you
reorder `CPM_MONITORED_CHANNELS`, silently.

**The key is the channel, then the platform, both ascending, then the newest row
for that pair.** Alphabetical is arbitrary and is chosen only because it is
*fixed*: the same evidence produces the same verdict on every replay. The row
references the observation, so the channel and platform the verdict concerns are
readable from it.

Two costs follow, and both are real:

- A package current on one channel and behind on another gets the
  first-sorting channel's verdict.
- **A channel that simply does not carry the package answers `not_found`**, and
  if that channel sorts first, `not_found` becomes the package's conda verdict
  even where a later-sorting channel publishes the authority's exact version. Read
  the referenced observation before acting on a conda `not_found`.

A verdict per pair is a larger table than this one's `(package, policy_run)` key
describes, and it is not built here.

## The feedstock presence policy: does it exist, and is anybody maintaining it

`CPM-CURRENCY-S07` adds the second policy pass. Like the currency pass it runs
inside the orchestrating policy run rather than on a schedule of its own, makes
no outbound call of any kind, and reads only the evidence a collector has already
written — here the `feedstock_snapshots` table alone. It answers the question
`CPM-UJ-2` asks: which packages have no feedstock worth filling, and which have
one nobody is maintaining.

### The verdicts

Each package gets one row in `package_feedstock_presence` per policy run, and one
value in `package_health.feedstock_presence_status`. The vocabulary is `core`'s
four sentinels plus four outcomes of its own:

| Verdict | What it means | What to do about it |
|---|---|---|
| `present_and_maintained` | A feedstock exists and was pushed to within the inactivity threshold of the run's evidence cut-off. | Nothing. |
| `present_and_inactive` | A feedstock exists and its last push is older than the threshold. | Somebody should look at it — or the threshold is wrong (see below). |
| `absent` | conda-forge answered that there is no feedstock, and no staged recipe was found. | This is the gap to fill: a recipe has to be written. |
| `staged_recipe_pending` | No feedstock, but an open staged-recipes pull request would create one. | Finish the review. **Never `absent`** — the work is already started, and reporting it as a gap sends a second person to redo the first person's. |
| `unknown` | Nothing was observed at the cut-off; **or** the observation records `unknown`; **or** a feedstock exists whose last push the collector could not date. | Look at the row's `detail` and its referenced observation — the last case says so explicitly. |
| `error` | The lookup failed. Not an absence, and never folded into one. | Check the collector's own `detail` and the source. |
| `not_applicable` | Resolution recorded the feedstock question as inapplicable to this package. | Nothing. |
| `not_found` | Reserved. This pass never produces it: `absent` and `staged_recipe_pending` are the two more specific answers it has instead. | — |

**A feedstock that exists but cannot be dated reads `unknown`, never `inactive`.**
The collector records an absent or unusable push instant honestly rather than
inventing one, and a threshold cannot be applied to nothing. Calling it inactive
would be a guess; calling it maintained would be worse. The row's `detail` says
which case it is, because a row for an undatable feedstock and a row for a
package nobody observed carry the same three empty columns otherwise.

### The inactivity threshold is versioned data, not a constant

This is the part with an operational consequence, so read it before enqueuing a
run.

The threshold lives in
`src/django_apps/conda_sentinel/policies/data/policy-parameters.toml`, which
ships inside the wheel and is changed **by pull request**. One entry per policy
version:

```toml
[versions."<policy version>"]
feedstock_inactivity_days = <positive whole number>
```

The pass looks the threshold up **by the policy version the run declares**, which
means three things:

1. **A policy run must name a version this file records, or the run fails
   outright.** There is no default and there must not be one: a defaulted verdict
   is indistinguishable from a reviewed one in every report that reads it. The
   version is a fact about the run rather than about any package, so it is
   established once before the first package: the run finalizes `failed`, the
   task raises with a message naming the file and the version, and **nothing is
   written at all** — no derived row of any pass, and no rollup row. Every
   package keeps whatever health it had.
2. **Changing the threshold changes verdicts, with no code change and no test
   failure.** That is the mechanism working as intended. Every row records the
   threshold it applied, so a report can always say what a verdict was measured
   against.
3. **A change means adding a version entry**, because a recorded run can be
   replayed at its own version (`CPM-FR-22`) and can only be replayed while that
   version's entry still says what it said. The one exception is a version no run
   has ever recorded, which has nothing to keep replayable and may be edited in
   place.

`policies/data/README.md` is the contract: the keys, the refusals, the editing
rule and what a change does not do by itself. Every refusal names the file.

**The shipped value is provisional.** PRD Open Question 10 asks what the
threshold should be and this component has not answered it; the file carries the
reasoning for the starting point beside the number, and the file is the only
place the number appears — repeating it here would be a second copy nothing keeps
in step.

**The file is read once per process.** A change takes effect at the next start,
which shipping a new artifact already is. This is deliberately unlike the
inventory watchlist, which is re-read on every sweep: a policy version means one
rule set (`CPM-AD-8`), and a file re-read per package would let an edit part-way
through a run judge half the inventory under one threshold and half under
another.

### The boundary

A feedstock pushed to **exactly** the threshold ago at the run's cut-off reads
`present_and_maintained`. The threshold is how long a feedstock may go without a
push, so inactivity begins strictly after it. It matters when choosing a number,
and it is asserted by a test rather than left to be discovered.

### Two things that stop a maintenance verdict being reported

**A feedstock nobody re-observed reads `unknown`, not `present_and_inactive`.**
If the observation the verdict would rest on is *itself* older than the applied
threshold, this pass will not say whether anybody is maintaining the recipe: a
collector that stopped running and a recipe that stopped moving produce the same
arithmetic, and only one of them is a finding. The row's `detail` says which
observation and how old it was. If a whole inventory suddenly reads `unknown`,
check that the feedstock collector is still running before changing the
threshold.

**An absence nobody established reads `unknown`, not `absent`.** `not_found` on a
feedstock snapshot is reachable four ways — conda-forge answered that the
repository is not there, the repository could not be read, the staged-recipes
queue could not be read, or the queue held more than one candidate or overflowed
its page — and only the first is evidence that there is nothing there. Only the
first produces `absent`. That matters because `absent` dispatches somebody to
write a recipe, and three of those four shapes are a GitHub outage or a busy
queue rather than a gap. The evidence row's own `detail` says which shape it was.

### Where the result lands

| Table | What it holds |
|---|---|
| `package_health.feedstock_presence_status` | One value per package: the verdict, **after** the confidence gate. This is the read surface. |
| `package_feedstock_presence` | One row per package **per run**: the verdict ungated, the threshold applied, the last recipe activity instant, the age that was measured against the threshold, the identity confidence the row was computed under, a `detail` for the two verdicts whose columns do not explain them, and a foreign key to the feedstock observation the verdict rests on. |

**`package_health.feedstock_presence_status` is not
`package_currency.feedstock_status`.** The first is whether a feedstock exists
and whether anybody is pushing to it; the second is whether the conda-forge
*recipe* pins the authoritative version. They are different questions with
different answers, and the names are deliberately not the same.

An `unmapped` package reads `unknown` in `package_health`
**whatever this pass computed**, exactly as the currency column does and for the
same reason (`CPM-AD-4`, `CPM-FR-5`) — and in particular it never reads `absent`,
so no unresolved package sends anybody to write a recipe. `package_feedstock_presence`
still records the verdict the pass reached, and it records the confidence beside
it, so the two together say why they differ. An `inventory-derived` package's
verdict is **not** degraded: that confidence is a label, and it travels on the
rollup row beside the value.

### What a run costs

This pass issues **two queries per package** — one evidence read and the insert of
the derived row — inside the orchestration's per-package loop, on top of the
currency pass's five. Looking the threshold up costs no query at all: it is a
memoized read of a file, established once per run rather than once per package.
The count is pinned by a test at three inventory sizes so a regression is
visible; making the reads set-based is recorded as deferred work on
**this story**, `CPM-CURRENCY-S07`, alongside `CPM-CURRENCY-S06`'s entry for the
currency pass's own five.

### `package_feedstock_presence` accumulates, and nothing prunes it

One row per package per run, never updated and never deleted, with **every
relation `PROTECT`** — to the package, to the policy run, and to the feedstock
observation. The reasoning and the consequences are exactly `package_currency`'s,
above: there is no retention path, deleting old runs would have to delete these
rows first, and no story currently claims it. Size the database accordingly.

## The vulnerability policy: one status per package, with KEV kept visible

`CPM-SECURITY-S04` adds the third policy pass. Like the two before it, it runs
inside the orchestrating policy run rather than on a schedule of its own, makes
no outbound call of any kind, and reads only the evidence a collector has already
written — here `vulnerability_findings` and `kev_findings`. It answers
`CPM-FR-17`: what does this run establish about one package's advisory exposure,
and is any of it known-exploited.

**It writes no rollup column, and that is deliberate.** `CPM-AD-21` says no pass
writes `package_health`, and the rollup offers no column for this domain. The
word "rollup" in this story's title means the reduction of many findings to one
per-package result, into this pass's own table. Nothing here changes
`package_health` except the `policy_versions` map, which gains a `vulnerability`
entry because the pass ran.

### The verdicts

Each package gets one row in `package_vulnerability` per policy run. The status
vocabulary is `core`'s four sentinels plus two of its own:

| Status | What it means | What to do about it |
|---|---|---|
| `advisories_matched` | At least one advisory matched this package at the version the run asked about. | The finding the row references, and the rest of that sweep, are what to look at. |
| `no_advisory_matched` | The advisory source was read and matched nothing. **Not clean**: this package may still carry an advisory the source does not know. | Nothing, but do not read it as "no vulnerabilities". |
| `unknown` | Nothing was established: no evidence at the cut-off; **or** evidence saying only that nothing was established; **or** the lookup failed; **or** the source did not know the locator. | Read the row's `detail` and its referenced finding — every case but the first says which it was, in words. |
| `error`, `not_found`, `not_applicable` | Reserved. This pass never produces them. | — |

**`unknown` and `no_advisory_matched` are two facts and never one value.** "We
read the source and matched nothing" is an established negative; "nobody looked",
"the look failed" and "the source did not know the package" are three ways of
establishing nothing. The evidence table records the first as `unknown` too — on
an advisory table `not_found` would read as *clean*, which no collector is in a
position to say — so the distinction is carried in the finding's own `detail`, and
this pass reads it there.

**A failed lookup makes the package `unknown`, not `error`.** That is a
reduction one level above `CPM-FR-6`: the evidence row keeps all five of its
states, and at the *package* level a failed look and an unknown locator both mean
this run established nothing about its exposure. Which one it was is on the row —
the referenced finding, plus a `detail` line saying it in words.

### KEV is a column, never a number

`package_vulnerability.kev_membership` holds one of three values, and it is
**never** an input to `risk_level`:

| Membership | What it means |
|---|---|
| `listed` | The KEV catalog lists at least one advisory this run recorded against the package. |
| `not_listed` | The catalog was read and lists none of them. Not a statement that the package is clean. |
| `not_established` | No cross-reference established anything — none was written by the cut-off, the ones that were say only that the catalog could not answer, or the KEV sweep read is not about the advisory sweep read (see below). |

**Three values, not two, and the third is the one that matters
operationally.** `kev_findings` has `listed` and `not_listed`; a package the KEV
collector never ran for is neither. Recording it as `not_listed` would claim an
absence the run never established — and `not_listed` is the value a read surface
is most likely to paint green. If a whole inventory reads `not_established`,
check that the KEV collector is running before concluding anything.

**A membership is a claim about the advisories *this run recorded*, so the two
sweeps have to line up — and at scale they often do not.** The advisory sweep and
the KEV sweep are read as two independent "newest sweep at or before the cut-off"
queries, and the KEV dispatch offset "reduces the window and does not close it":
at ten thousand packages the advisory sweep spends most of a day inside its own
allowance, so today's advisories beside yesterday's cross-references is normal.
The pass therefore reduces only the cross-references that name a finding of the
*current* advisory sweep, and counts a matched advisory nothing cross-referenced
as `not_established`. Both stale readings are closed by that: a `not_listed` that
was never asked about half of this run's advisories, and a `listed` about an
advisory this run did not record. The row's `detail` says which advisories and
which cross-references did not line up. **A rise in `not_established` beside
healthy collectors means the two sweeps are drifting apart, not that packages
changed.**

**A `kev_findings` row carrying a state nothing recognises is recorded, not
refused.** Django does not enforce `choices` on `save()`, so such a row is
reachable. It reads as `not_established` -- never `not_listed` -- and the derived
row's `detail` names the row and the state. It is deliberately not an error: one
package is the atomic unit of a policy run, so refusing would roll that package's
currency and feedstock rows back with it and leave the package with no
`package_health` row at all, reading as never evaluated. If you see that line,
the fault is in whatever wrote the evidence row, and the rest of the package's
verdicts are intact.

**Why a column rather than a contribution to the severity.** `CPM-FR-17`'s single
hazard is that a severity score is an average, and averaging is exactly how a
known-exploited advisory disappears: one KEV entry among nine moderate findings
comes out looking moderate. Any design in which KEV feeds the risk level can, for
some combination of findings, produce a level that does not distinguish a KEV
package from a non-KEV one. A separate column cannot, because you filter on it
directly. **Filter on `kev_membership = 'listed'`; never sort by `risk_level` and
expect KEV to be near the top.**

### The severity order is versioned data, not a constant

This is the part with an operational consequence, so read it before enqueuing a
run.

The order lives in the same reviewed file the inactivity threshold does,
`policies/data/policy-parameters.toml`, which ships inside the wheel and is
changed **by pull request**:

```toml
[versions."<policy version>"]
feedstock_inactivity_days = <positive whole number>
vulnerability_risk_order = ["<severity>", "<severity>", ...]
```

The risk level is the **worst-ranked severity among the advisories matched to the
package** — the first entry of that list any matched finding's `severity` names,
compared case-insensitively against the string the source stated. It is a
selection, never an arithmetic: nothing is summed, averaged or weighted.

A blank `risk_level` means **missing, not low**. Four things reach it: the
package matched nothing, its matched advisories state no severity, they state
severities this version does not rank, or this version records no order at all.
The row's `detail` says which, and names the version so you can read the entry
that produced it.

**`vulnerability_risk_order` is optional, and a version that omits it still
runs.** It is the one key an entry may legitimately omit — it arrived after versions had already
been recorded, and an old entry must keep saying what it said or a run at that
version stops replaying (`CPM-FR-22`). So:

1. **A run at a version that records no order still writes every row**, with a
   **blank `risk_level`** and a `detail` naming the version and the missing key.
   The status, the KEV membership and both evidence references are derived
   exactly as they otherwise would be, because no other verdict reads this
   parameter. There is no default and there must not be one: a risk level drawn
   from an order nobody reviewed is indistinguishable from a reviewed one in
   every report that reads it, and a blank is a missing measurement rather than a
   low one. A *malformed* order is a different matter and is refused at the read,
   naming the file — see `policies/data/README.md`.
2. **A run at a version the file does not record at all fails outright**, before
   any package, exactly as the feedstock pass's own missing threshold does.
3. **Enqueue policy runs at a version that records both parameters** if you want
   risk levels. The shipped file records `2026.09.1` for exactly that reason;
   `2026.09` remains, unedited, so a run recorded at it replays as it was —
   currency, feedstock, health and vulnerability rows, the last with no risk
   level, because that version predates the parameter.

**The shipped order is provisional.** `CPM-FR-17` names a risk level and the PRD
seeds no severity scale for it. The file carries the reasoning beside the value
and is the only place the labels appear, so this document does not repeat them.
It is changeable by review **without a code change**, which is the whole point.

**Never add a KEV label to that list.** KEV membership is its own column and must
never become a severity. An entry such as `"kev"` would never match a finding's
stated severity anyway — the order is read only over
`vulnerability_findings.severity` — so it would look to a reviewer as though it
did something while doing nothing.

### Where the result lands

| Table | What it holds |
|---|---|
| `package_vulnerability` | One row per package **per run**: the status, the KEV membership, the risk level, the policy version and evidence cut-off it was computed under, a `detail` for the shapes the columns do not explain, and foreign keys to the exact vulnerability finding and KEV cross-reference the verdict rests on. |
| `package_health` | **Nothing.** This pass contributes no rollup column. Its name appears in `policy_versions` because it ran. |

`package_vulnerability` copies the policy version and the cut-off onto every row,
where `package_currency` and `package_feedstock_presence` copy neither. That is
because a risk level is meaningless without the severity order that produced it,
and the order is keyed by version — so a report reading this table alone can say
what a value was drawn from, and a `CPM-FR-22` replay diff is a query over one
table.

### What a run costs

This pass issues **five queries per package** for a package with evidence in both
tables — two per evidence table, because "which sweep is current at the cut-off"
and "which rows belong to it" are two questions, plus the insert of the derived
row — on top of the currency pass's five and the feedstock pass's two. A package
with no evidence at all costs four, because each read stops at the first query
when there is no sweep to fetch. Looking the severity order up costs no query at
all: it is a memoized read of a file, established once per run.

Reading a *sweep* rather than a single row is what makes this a reduction: both
evidence tables hold one row per advisory, and reading only the newest row would
reduce nine advisories to whichever the database happened to return.

### `package_vulnerability` accumulates, and nothing prunes it

One row per package per run, never updated and never deleted, with **every
relation `PROTECT`** — to the package, to the policy run, and to both evidence
rows. The reasoning and the consequences are exactly `package_currency`'s and
`package_feedstock_presence`'s, above: there is no retention path, deleting old
runs would have to delete these rows first, and no story currently claims it.
Size the database accordingly, or run the policy less often than you collect.

## The licence policy: nothing is allowed, and that is the shipped answer

`CPM-SECURITY-S05` adds the fourth policy pass. Like the three before it, it runs
inside the orchestrating policy run rather than on a schedule of its own, makes
no outbound call of any kind, and reads only the evidence a collector has already
written — here `license_findings`. It answers `CPM-FR-18`: what does this product
say about the licence a monitored channel states for one package.

**Read this section before you read a licence report.** Out of the box, **no
package will ever read `allowed`**, and every package whose licence was
established will read `manual_review`. That is not a broken pass and it is not a
misconfiguration you can correct with a setting. It is the correct answer to a
question nobody has answered yet, and the rest of this section is about what to
do with it.

**It writes no rollup column.** `CPM-AD-21` says no pass writes `package_health`,
and the rollup offers no column for this domain. Nothing here changes
`package_health` except the `policy_versions` map, which gains a `licence` entry
because the pass ran.

### The verdicts

Each package gets one row in `package_license` per policy run. The vocabulary is
`core`'s four sentinels plus four of its own:

| Outcome | What it means | What to do about it |
|---|---|---|
| `allowed` | A recorded rule names this licence and permits it. **Reachable no other way.** | Nothing. |
| `restricted` | A recorded rule names it and permits it subject to conditions. | Read the rule, and the obligations it stands for. |
| `forbidden` | A recorded rule names it and refuses it. | The finding the row references says which channel stated it. |
| `manual_review` | The licence **is** known and no recorded rule names it — including because the run's version records no rule set at all, which is the shipped state. | Read the row's `detail`: it says whether no policy exists yet or an existing policy does not cover this licence. Those are different jobs. |
| `unknown` | The licence itself was **never established**: no evidence at the cut-off; **or** the channel stated none; **or** it stated one this product will not normalize without guessing; **or** the channel could not be read; **or** no monitored channel serves the package at all. A *single* channel answering `not_found` beside one that stated a licence does **not** produce this — that channel is left out of the reduction. | Read the row's `detail` and its referenced finding — every case but the first says which it was, in words, and the `detail` names every kind of nothing the sweep met rather than only the referenced row's. |
| `error`, `not_found`, `not_applicable` | Reserved. This pass never produces them. | — |

**`manual_review` and `unknown` are two facts and never one value.** One is a gap
in the *policy* and the other is a gap in the *evidence*, and they are fixed by
different people. A dashboard that merges them into "needs attention" is throwing
away the only thing that says which queue a package belongs in.

**`unknown` is not "no restrictions".** A channel that states no licence, or one
this product declines to guess at, has told you nothing — and on a licence table
that is the reading that costs money.

### `allowed` is never a default and never an absence

This is the property the whole pass is built around, and it is held in three
independent places rather than one:

1. **In the pass.** `policies/licence.py` returns a rule's own recorded
   disposition. There is no branch in it that spells `allowed`; the string comes
   out of the reviewed file.
2. **In the reduction.** `allowed` is last in the precedence order and the
   reduction takes the *worst* rank, so a package several channels disagree about
   cannot read `allowed` while any of them said anything else. Disagreement never
   resolves upward.
3. **In the database.** `package_license` carries a check constraint requiring an
   `allowed` row to name the rule that produced it. A hand-written `INSERT` that
   went round the pass entirely is refused by PostgreSQL.

Every other outcome is reachable by something not happening. `allowed` is the one
that must be reached by a positive statement, and it is the one whose appearance
in error would be least likely to be questioned, because it looks like good news.

### The rule set is versioned data, and it ships empty

The rules live in the same reviewed file the inactivity threshold and the
severity order do, `policies/data/policy-parameters.toml`, which ships inside the
wheel and is changed **by pull request**:

```toml
[versions."<policy version>"]
license_rules = [
  { expression = "MIT", disposition = "allowed" },
  { expression = "GPL-3.0-only", disposition = "forbidden" },
  { expression = "MPL-2.0", disposition = "restricted" },
]
```

`expression` is a normalized SPDX expression exactly as
`license_findings.normalized_license` stores it, spelled as SPDX spells it; the
match case-folds both sides. `disposition` is one of `allowed`, `restricted`, `forbidden` — and
never `manual_review`, which is what a licence no rule names already reaches.

**A compound expression is matched whole, and `AND` is decomposed only to
restrict.** `MIT OR Apache-2.0` is one expression, and deciding which of two
differently-ruled licences a package took is a compliance judgement rather than a
string operation. A rule naming `MIT` does not reach it, so it reads
`manual_review` — for a disjunction that *is* the conservative direction, because
the permission is withheld. A rule that should cover it names it in full.

A conjunction is different. `MIT AND GPL-3.0-only` binds both sets of obligations
at once, so a rule forbidding `GPL-3.0-only` forbids the compound — left matched
whole only, it would read `manual_review`, which ranks *below* `forbidden` and
below `unknown`, and a deny rule would be silently weakened by a conjunction. So
where no rule names a conjunction whole, a rule naming one of its operands
`forbidden` or `restricted` decides it, the least permissive one winning, and the
row's `detail` names the operand and says what happened.

**Decomposition never runs in the permissive direction.** A rule allowing one
operand does not allow the conjunction: `allowed` still requires a rule naming
the whole expression. A rule naming an expression `collectors/spdx.py` can never
produce — `GPL-3.0`, `GPLv3`, `Apache-2.0 WITH LLVM-exception`, anything
parenthesised — is refused when the file is read, because such a rule would be
permanently inert and every package it was meant to cover would read
`manual_review` with nothing saying why.

**Every shipped version records no rule.** `CPM-FR-18`'s content — which licences
are allowed — is PRD Open Question 2, which the PRD names as unanswered and as
blocking this epic, so `CPM-SECURITY-S05` shipped the mechanism and the schema
with no allow entries and no deny entries. The newest shipped entry,
`2026.09.2`, declares `license_rules = []` so an operator can see the key and
copy the entry; the older entries predate it entirely. All three mean the same
thing and produce the same rows.

**This is deliberately unlike the severity order, which ships provisional.**
There the PRD named a risk level and simply seeded no thresholds, so a starting
point marked as one in the file is a reviewable list. Here the PRD names the
decision itself as open, and a "conservative starting point" would be this
component deciding a compliance question it was told not to decide.

### Seeding the rule set once Open Question 2 is answered

This is the one operational task this section exists for. In order:

1. **Decide the policy.** Which SPDX expressions your organization allows, which
   it forbids, and which it permits subject to conditions. This component has no
   opinion and will not acquire one.
2. **Add a new `[versions."..."]` entry** to
   `policies/data/policy-parameters.toml` — **never edit an existing one**. Copy
   `2026.09.2`'s `feedstock_inactivity_days` and `vulnerability_risk_order`
   forward unless review is changing those too, and fill in `license_rules`. A
   run recorded while the rule set was empty must keep replaying to
   `manual_review` (`CPM-FR-22`), and it only can while that version's entry
   still says what it said.
3. **Write the expressions as SPDX spells them**, and write compound expressions
   in full. Check what `license_findings.normalized_license` actually holds for
   your inventory first — that column is the left-hand side of every comparison.
4. **Ship the artifact.** The file is read once per process, on purpose, so a
   change takes effect at the next process start.
5. **Enqueue a policy run at the new version.** Nothing re-derives on its own,
   and no evidence is re-collected: the same `license_findings` rows are read
   again as of the new run's cut-off and reduced under the new rules.
6. **Diff the two runs.** Both versions' rows survive, each recording the version
   that produced it, which is what `CPM-FR-22`'s replay is a comparison of.

The refusals you may meet at step 4 are in `policies/data/README.md`. The ones
worth knowing before you write the entry: a rule that is not a table declaring
exactly `expression` and `disposition` is refused; a disposition outside the
three is refused; and **two rules naming the same expression are refused whether
or not they agree**, because a licence one rule allows while another forbids it
has no verdict at all, only whichever rule a reader stopped at. Every fault in
the set is reported at once.

**A version that records no rule set does not fail anything.** The pass derives
`manual_review` for every package with an established licence, says so on the
row, and every other domain's row for that package still commits. That is not a
courtesy: `CPM-AD-23` puts one *package* in a transaction rather than one pass, so
a refusal here would roll back that package's currency, feedstock and
vulnerability rows too — and, the condition holding for every package, would
finalize the run `failed` and break the replay of every run recorded before this
pass existed.

**A run at a version the file does not record at all fails outright**, before any
package, exactly as the other three passes' own missing parameters do.

### Where the result lands

| Table | What it holds |
|---|---|
| `package_license` | One row per package **per run**: the outcome, the matched rule's expression (blank where no rule decided it), the policy version and evidence cut-off it was computed under, a `detail` for the shapes the columns do not explain, and a foreign key to the exact licence finding the verdict rests on. |
| `package_health` | **Nothing.** This pass contributes no rollup column. Its name appears in `policy_versions` because it ran. |

`package_license` copies the policy version and the cut-off onto every row, where
`package_currency` and `package_feedstock_presence` copy neither — for the reason
`package_vulnerability` does: the outcome is meaningless without the rule set that
produced it, and the same expression may read `manual_review` at one version and
`forbidden` at the next.

**One reference where several channels may have spoken.** The row names the
channel whose verdict it carries; the rest of that sweep is one query away on
`license_findings` filtered by the package and the row's `evidence_cutoff`. Where
the channels disagreed, the row's `detail` says so and names the expressions, so
the single verdict is never the only sign that there were several.

### What a run costs

This pass issues **three queries per package** for a package with evidence — two
because "which sweep is current at the cut-off" and "which rows belong to it" are
two questions, plus the insert of the derived row — on top of the currency pass's
five, the feedstock pass's two and the vulnerability pass's five. A package with
no evidence at all costs two, because the read stops at the first query when
there is no sweep to fetch. Looking the rule set up costs no query at all: it is
a memoized read of a file, established once per run.

Reading a *sweep* rather than a single row is what makes this a reduction:
`license_findings` holds one row per monitored channel, and reading only the
newest row would reduce four channels to whichever the database happened to
return — which on this table is the difference between seeing a disagreement and
not knowing there was one.

### `package_license` accumulates, and nothing prunes it

One row per package per run, never updated and never deleted, with **every
relation `PROTECT`** — to the package, to the policy run, and to the evidence row.
The reasoning and the consequences are exactly the three sibling tables', above:
there is no retention path, deleting old runs would have to delete these rows
first, and no story currently claims it. Size the database accordingly, or run
the policy less often than you collect.

## The remediation readiness policy: what you can do now, and what is waiting on somebody else

`CPM-SECURITY-S06` adds the fifth policy pass. Like the four before it, it runs
inside the orchestrating policy run rather than on a schedule of its own, makes no
outbound call of any kind, and reads only the evidence a collector has already
written — here `vulnerability_findings` for the fixed version an advisory names,
and the four currency-surface tables (`source_release_snapshots`,
`pypi_release_snapshots`, `feedstock_snapshots`, `conda_package_snapshots`) for
where that version has appeared. It answers `CPM-FR-41`: can a reviewer act on
this finding this morning, and if not, what is it waiting for.

**It reads no other pass's derived table.** Not `package_currency`, not
`package_vulnerability`, not `package_license`. A readiness derived from another
pass's verdict would depend on that pass's policy version as well as its own, and
`CPM-FR-22`'s replay could then be stated for neither.

**It reads no policy parameter.** There is no key for it in
`policies/data/policy-parameters.toml` and there is deliberately not going to be
one: the version comparison is the currency pass's, the freshness targets are the
collectors' own declarations, and the vocabulary is fixed. Nothing you can put in
that file changes what this pass says.

**It writes no rollup column.** `CPM-AD-21` says no pass writes `package_health`,
and the rollup offers no column for this domain. Nothing here changes
`package_health` except the `policy_versions` map, which gains a `remediation`
entry because the pass ran.

### The verdicts, and what each one tells you to do next

Each package gets one row in `package_remediation` per policy run. The vocabulary
is `core`'s four sentinels plus four of its own:

| Readiness | What it means | What to do about it |
|---|---|---|
| `ready` | A monitored channel publishes the fixed version. | **Install it.** This is the queue `CPM-UJ-1` opens with. The row names the exact `conda_package_snapshots` row — so you can see which channel and platform. |
| `awaiting_build` | The conda-forge recipe carries the fixed version and no monitored channel has built it. | A build is due. Nothing to install yet; check the feedstock's CI, or wait for the next migration wave. |
| `awaiting_packaging` | Upstream and/or PyPI has released the fixed version and the recipe has not been updated. | The recipe is the outstanding work. Open (or wait for) a feedstock version bump. The `source_fix` and `pypi_fix` columns say which of the two released it. |
| `blocked` | Every one of the four surfaces was read and none carries the fixed version. **No row this product currently writes reaches it** — see the section below. | Nothing you can do with a package manager: a fix exists and nobody has shipped it anywhere. Consider a pin, a patch, or a vendored build. |
| `not_applicable` | This run matched no advisory to the package, so there is no finding to be ready for. | Nothing. **This is not a claim that the package is clean** — whether this run established anything about its exposure is `package_vulnerability`'s verdict at the same cut-off. |
| `unknown` | This run could not say. Read the row's `detail`: it names which of the causes it was. | See the table below — the causes call for different work. |
| `error`, `not_found` | Reserved. This pass never produces them. | — |

**`unknown` is not "nothing to do", and this is the reading that costs the most.**
It has six causes and the row's `detail` names which:

| Cause | What the row says |
|---|---|
| At least one of the four surfaces was not read | Names the surfaces that did not answer, and says why the row is **not** `blocked`. |
| A surface answered with a version that is not the fix | Names the versions each surface stated. This is now the *most common* `unknown`: see "The comparison is equality" below. |
| The advisory names a fixed range this product cannot compare — `>=1.2.3`, `<2.0.0`, `1.0,<2.0` | Names the expression, and says that no architecture decision owns version ordering yet. |
| The finding records no fixed range | Says that a blank field and a source stating none are recorded identically, so this run cannot tell them apart. |
| The advisory sweep itself is past the advisory collector's freshness target | Says so, and says no surface was asked. `evidence_stale` is `true`. |
| There is no advisory evidence at all, or the only evidence establishes nothing | Says which. |

### `blocked` is an established absence, and never an unread surface

This is the property the whole pass is built around, and it is the mirror image of
the licence pass's `allowed`. A false `allowed` ships a forbidden licence; a false
`blocked` tells a security reviewer to give up on a package whose fix is sitting on
a surface nobody checked. It is held in three independent places:

1. **In the vocabulary.** Each of the four surface columns holds one of three
   values — `published`, `not_published`, `not_read` — and never two. "This surface
   was not read" is a value the schema can hold rather than a silence the reduction
   has to guess at.
2. **In the pass.** `blocked` is reached from four `not_published` readings and
   from nowhere else. Every other shape — a surface with no evidence, a surface
   that errored, a surface that does not carry the package, a surface whose
   evidence is stale, a surface whose collector declares no freshness target, a
   surface that stated a version that is not the fix — reads `not_read` and the
   readiness is `unknown`.
3. **In the database.** `package_remediation` carries two check constraints: a
   `blocked` row must have all four surfaces `not_published`, with no exception,
   and any surface that is not `not_read` must name the observation it was read
   from. A hand-written `INSERT` that went round the pass is refused by PostgreSQL.

### No row this product writes is `blocked`, and that is deliberate

**Applying the rule above honestly leaves the verdict unreachable.** Read this
before you conclude that nothing in your inventory is unfixable.

`not_published` is the only reading permitted to vote towards `blocked`, and a
surface may read `not_published` only where it *established* that the fix is not
there. The four surface tables store the version each surface states as its
**latest** — not the set of versions it carries. PyPI still hosts `1.5` when its
latest is `2.0`, and a conda channel still serves older builds. So "this surface's
latest is not the fix" establishes nothing in either direction, and the pass
records it as `not_read` with the versions named in the `detail`.

An earlier build recorded it as `not_published`. That made `blocked` the **steady
state** rather than an edge case: advisories name a fix, surfaces move past it, and
every package with an older advisory decayed into the one verdict that tells a
security reviewer to stop looking.

Two things would make `blocked` reachable, and neither exists yet:

* **A version-ordering rule** — given a fix and a version a surface states, decide
  whether the surface is at or past it. No architecture decision owns this. (It is
  *not* `CPM-AD-6`, which is *version authority is explicit per package*: it owns
  which surface is authoritative, not how two version strings compare. Earlier
  builds cited it here and on the rows themselves, and were wrong.)
* **A collector that records "the source stated there is no fix"** distinctly from
  "the field was absent". Today `collectors/vulnerability.py` writes one clause per
  field the source left blank, and blank means missing and is never inferred, so
  the two cannot be told apart from a row. A matched advisory with no fixed range
  is `unknown`.

The verdict stays in the vocabulary, both check constraints still guard it, and the
reduction still ranks it worst. Nothing about the schema or your queries changes on
the day either gap closes.

**What this means operationally:** an unfixable finding shows as `unknown` with a
`detail` naming what each surface stated. Do not read `unknown` as "nothing to do"
— it is the reading that costs the most, and the table above says which cause it
was.

### The comparison is equality, and what that costs you

The pass asks "does this surface state the fixed version", compared as
`policies/currency.py` compares versions: surrounding whitespace and a single
leading `v` before a digit are reconciled, and nothing else is.

**A surface that states any version other than the fix reads `not_read`.** If an
advisory names `1.2.3` as the fix and a channel's latest is `1.2.4`, this product
cannot decide that `1.2.4` contains the fix — and it equally cannot decide that
`1.2.4` means the channel does *not* serve `1.2.3`, because what the row records is
the channel's latest and not its contents. Both directions need a version-ordering
rule across four ecosystems, and no architecture decision owns one. The row records
what was compared: wherever any surface stated a version that is not the fix, the
`detail` names every version that surface stated.

The practical consequence: **a package whose fix has been superseded reads
`unknown`, not `ready` and not `blocked`.** Read the row's `detail` and the four
surface snapshots it references — the versions are all there. This is the story's
recorded gap, not a defect to file.

The same limit is why a fixed range that names a *set* of versions is never
compared at all: `>=1.2.3`, `<2.0.0`, `1.0,<2.0` and `[1.2.3,)` all read `unknown`
with the reason on the row, and the run does not fail.

### Stale evidence is never relied on, and never asserts a fix

`CPM-FR-38` and `CPM-AD-28` already decide when an observation has aged past its
collector's declared freshness target, and this pass consumes that answer rather
than restating it. A surface whose evidence is stale reads `not_read`:

* it cannot make a package `ready` — a fix "available" on evidence you no longer
  trust is exactly the claim `CPM-NFR-3` forbids;
* it cannot make a package `blocked` either, for the same reason it cannot be an
  absence.

**The advisory evidence's own age is measured too.** `CPM-UJ-1`'s stated edge case
is a finding older than its freshness target, and a pass that measured only the four
surfaces would report a month-old advisory sweep beside a channel refreshed this
morning as `ready`. Where the advisory sweep is stale the readiness is `unknown`,
**no surface is asked at all**, `fixed_version` is blank and the `detail` says so.

`package_remediation.evidence_stale` is `true` on any row where the advisory sweep
or at least one surface this run read was past its target, and the `detail` names
which. **A fresh surface beside a stale one still decides the row** — surface
freshness is per surface, so a fix on a fresh channel is `ready` whatever the
recipe's staleness. A stale *advisory* sweep is not per surface: it stops the row.

The targets are the collectors' own: two days for the upstream, PyPI and
published-package collectors, fourteen for the feedstock collector, and the
advisory collector's own cadence-derived target for `vulnerability_findings`. They
are declarations in code, not settings. A surface whose evidence table no
registered collector writes has no target at all — staleness could not be decided,
so the surface reads `not_read` and does not vote, and the `detail` says so.

### A package is only as actionable as its worst finding

A package with two matched advisories gets **one** row, carrying the least ready of
the two verdicts — `blocked` first, then `unknown`, then `awaiting_packaging`,
`awaiting_build`, `ready`. The four surface columns, `fixed_version` and the
referenced `vulnerability_finding` are about the finding that verdict came from and
about no other, because two advisories naming two fixed versions have two different
sets of surface answers. Where there were several, the `detail` says how many and
which one the columns describe.

### Where the result lands

| Table | What it holds |
|---|---|
| `package_remediation` | One row per package **per run**: the readiness, the four per-surface fix availabilities, the fixed version it looked for (blank where there was none), whether any surface's evidence was stale, the policy version and evidence cut-off it was computed under, a `detail` for the shapes the columns do not explain, and foreign keys to the advisory finding and to each surface observation the verdict rests on. |
| `package_health` | **Nothing.** This pass contributes no rollup column. Its name appears in `policy_versions` because it ran. |

### What a run costs

This pass issues **eleven queries per package** with a matched advisory — two for
the advisory sweep ("which sweep is current at the cut-off" and "which rows belong
to it"), two each for the four surfaces, and the insert — on top of the currency
pass's five, the feedstock pass's two, the vulnerability pass's five and the licence
pass's three. A package with **no** matched advisory costs three: there is nothing
to look for, so no surface is read at all. A package with **no advisory evidence at
all** costs two — `current_findings` short-circuits after the first query when
there is no sweep to read rows from. (An earlier note said three.) A package whose
advisory sweep is stale also costs three: no surface is asked.

The four surfaces are read once and reused across every matched finding, so a
package with nine advisories costs the same eleven as one with a single advisory.

Reading a *sweep* rather than a single row is what makes the published-package
surface honest: `conda_package_snapshots` holds one row per `(channel, platform)`
pair, and reading only one of them — as the currency pass does, by an alphabetical
tie-break — would report `not_published` for a package whose fix a later-sorting
channel publishes. Four such readings are `blocked`, which would be your channel
list telling a reviewer to give up.

### `package_remediation` accumulates, and nothing prunes it

One row per package per run, never updated and never deleted, with **every
relation `PROTECT`** — to the package, to the policy run, to the advisory finding,
and to each of the four surface observations. The reasoning and the consequences
are exactly the four sibling tables', above: there is no retention path, deleting
old runs would have to delete these rows first, and no story currently claims it.
This table references more evidence rows than any of its siblings, so it is also
the one that will most constrain a future retention story. Size the database
accordingly, or run the policy less often than you collect.

## The Python 3.14 readiness policy: ready, and on whose word

`CPM-PY314-S03` adds the sixth policy pass, and it is the one `CPM-EP-PY314` was
built toward. Like the five before it, it runs inside the orchestrating policy run
rather than on a schedule of its own, makes no outbound call of any kind, and reads
only evidence a collector has already written — here the epic's own two tables,
`python_readiness_assessments` for what a project's metadata *claims* and
`python_verification_results` for what a build *did*. It answers `CPM-FR-19`: is
this package ready for Python 3.14, and — the half the requirement is actually
about — **what kind of evidence says so**.

**Read the verdict and the evidence type as one answer.** Every row carries both,
deliberately:

| `readiness` | `evidence_type` | What it means |
|---|---|---|
| `verified_ready` | `verified` | a build **and** an import succeeded, on the platform the cited verification names. Proof |
| `verified_not_ready` | `verified` | verification ran and did not produce a working build. Also proof — of what happened on that runner, not of what the package can never do |
| `inferred_ready` | `inferred` | the project's published metadata admits 3.14 and **nobody has built it** |
| `inferred_not_ready` | `inferred` | the project's published metadata cannot admit 3.14, and nobody has built it |
| `unknown` | `none` | nothing was established. Six things reach it and `detail` says which |
| `not_applicable` | `none` | identity established this package has no release ecosystem, so there is nothing to assess and nothing to build |

**The evidence type is in the value on purpose, and it is not redundant.** A queue,
a report or an export that projects `readiness` alone still cannot mistake proof
for inference, because the string says which it is. That is the whole of
`CPM-FR-19`: a surface that forgot to join `evidence_type` is exactly the surface
the requirement is written about. Carry the value verbatim; shortening
`verified_ready` to `ready` in a view undoes three stories of work in one line.

**Verified outranks inferred, always.** When a package has both kinds of evidence,
the verification decides the verdict and the row cites **both** rows — the
assessment is not discarded. Where the two disagree (metadata that admits 3.14
beside a build that did not come out), the verdict is `verified_not_ready` and
`detail` says so in as many words. A build that ran outranks a claim a project
published; the claim is preserved beside it because a build fails for reasons that
are not the interpreter.

**`unknown` is the majority answer, and it is not bad news.** Verification is
triggered by hand and the static sweep runs weekly, so most of a real inventory has
one kind of evidence or neither. Six things reach `unknown`, and `detail`
distinguishes them: no evidence of either kind at the cut-off; evidence that
established nothing; a look that failed; a source that reported the package absent;
evidence older than its collector's freshness target; and evidence about a
different Python series. **None of them is a statement that the package is not
ready.** If you build a queue over this table, do not sort `unknown` beside
`inferred_not_ready` — one is a package to look at, the other is a package to
investigate.

### Stale evidence withholds the verdict, not the row

Evidence that has aged past its collector's declared freshness target cannot
produce a determinate verdict (`CPM-FR-38`: stale never displays as clean). The row
is still written, `evidence_stale` is `true`, and `detail` says the conclusion was
withheld rather than that the package failed. Staleness is measured from the run's
**cut-off**, never from a wall clock, so replaying a version at a cut-off gives the
same answer it gave the first time.

A stale *verification* beside a fresh assessment does not throw the whole answer
away: the verdict falls through to the inference, `evidence_stale` records that
something behind the row was old, and both rows are still cited.

### It judges one Python series, and reads evidence about no other

The pass judges `3.14`, and evidence rows about any other series are not read at
all — the filter is in the query rather than a comparison after it. A row about
3.15 is not evidence that disagrees; it is evidence about a different question.
When this product assesses a later Python, that is a new series, new evidence rows
and a decision about this pass — not a silent reinterpretation of the rows you
already have.

### Where the result lands

`package_python_readiness`, one row per package per policy run, keyed
`(package, policy_run)` exactly as `CPM-AD-21` requires. It contributes **no**
column to `package_health`: no pass writes the rollup, and which columns that table
grows belongs to `CPM-EP-PRIORITY`. The row copies the policy version and the
cut-off, so comparing two runs is a query over this table alone.

Three check constraints hold the requirement at the database rather than at the
pass's discretion: a verified verdict must cite a verification and declare
`verified`, an inferred verdict must cite an assessment and declare `inferred`, and
a row that decided nothing must declare `none`. A hand-written `INSERT` that went
round the pass is refused by PostgreSQL.

### What a run costs

Two indexed reads per package — one per evidence table, each bounded by the run's
cut-off and the series — and one insert. No outbound call, no parameter file, and
no rule set to review: unlike the currency, feedstock and licence passes, this one
reads no versioned parameter, because "verified outranks inferred" is this epic's
own semantics rather than a risk posture somebody has to choose. There is nothing
here to tune and nothing to fill in before it works.

### `package_python_readiness` accumulates, and nothing prunes it

One row per package per run, never updated and never deleted, with every relation
`PROTECT` — to the package, to the policy run, and to the two evidence rows the
verdict cites. The reasoning and the consequences are exactly the five sibling
tables', above: there is no retention path, deleting old runs would have to delete
these rows first, and no story currently claims it. Size the database accordingly,
or run the policy less often than you collect.

## The priority policy: nothing was prioritised until `2026.09.4`

`CPM-PRIORITY-S01` adds the seventh policy pass, and the first that reads what the
others concluded. It runs inside the orchestrating policy run, makes no outbound
call, and reads the six earlier passes' derived rows **for the same run** plus the
inventory's usage signals at the run's cut-off. It answers `CPM-FR-20`: which
priority bucket a package is in, why it is there, and how it scores within the
bucket.

**A rule set ships from `2026.09.4`, and not before it.** PRD Open Question 8 asked
what seeds the priority rules and the score function, and the answer for a long time
was that both encode an organizational risk posture that did not exist yet — so
`2026.09` through `2026.09.3` record an empty rule set and an empty score function,
every package reaches <span class="cs-state unknown">unknown</span>, and every row
says so in `detail`.

`2026.09.4` records **ten rules** and a score function. A run at that version produces
real buckets and scores; a run at an older one still produces
<span class="cs-state unknown">unknown</span>, unchanged, because the rules are
versioned data rather than code. That is the whole point of `CPM-AD-8` — the two
answers coexist, each attributable to the version that produced it, and a replay of an
old run still reproduces what that run concluded.

**A deployment that wants the older behaviour pins the older version.** Nothing here
is retroactive: rows already written keep the version map they were written with.

**Nothing reaches `p10` by *default*, and this is the sentence to read twice.** It is
as true at `2026.09.4` as before it: a bucket is assigned by a rule that matched, or
not at all. A default bucket is a claim about a package's importance that nobody made,
and `p10` is the one that would look harmless — "lowest priority" reads as a considered answer
rather than as an absence. A package this product has not prioritised is
`unknown`, and if you build a queue over this table, do not sort `unknown` beside
`p10`.

**Every assignment explains itself.** A row that names a bucket also carries what
that bucket means, which rule matched (`rule 3` — the position you count down to in
the file), and why. That is the whole point of the story: nobody should have to open
the rule set to understand why a package is `P1`. A database check constraint
refuses a bucket that arrives without all three, so a hand-written `INSERT` cannot
produce an unexplained assignment either.

### Filling in the rule set

Add a **new** `[versions."..."]` entry — never edit an existing one, or you break
the replay of every run recorded at it (`CPM-FR-22`):

```toml
priority_rules = [
  { bucket = "p1",
    description = "Known-exploited vulnerability with a published fix",
    reason = "A KEV-listed advisory matched and the fix is already packaged",
    when = { vulnerability_status = "advisories_matched", remediation_readiness = "ready" } },
]
```

Rules are matched **top down and the first match wins**, so the order is the whole
of the policy: a broad rule placed above a narrow one makes the narrow one
unreachable, and nothing will tell you. Write the most specific rules first.

`when` is a conjunction — every condition must hold — over the six domains the
earlier passes answer: `currency_status`, `feedstock_presence_status`,
`vulnerability_status`, `license_outcome`, `remediation_readiness`,
`python_readiness`. A domain outside that set is refused when the file is read,
because a rule matching on something no pass answers would match nothing, for ever
and silently. An empty `when` is refused too: it matches every package, which is a
default bucket wearing a condition.

`description` and `reason` are required and refused blank. A rule that assigns a
bucket and explains nothing produces exactly the row this story exists to prevent —
and it is the reviewer, not the code, who would have left the explanation out, so
the refusal reaches you at the file.

### Filling in the score function

```toml
priority_score_weights = { internal_component_count = 3, internal_lob_count = 2 }
```

Each weighted signal contributes its observed count times its weight, normalized
onto 1–100. The score ranks **within** a bucket; the bucket comes from the rules and
the two are read independently, so a version may record one and not the other.

**Weighting a nullable signal has a consequence to know before you write one.**
`internal_component_count` and `internal_lob_count` are required on every observed
inventory row; `apps`, `platforms`, `downloads` and `versions` are nullable, because
no hand-authored watchlist can state them credibly. If a weighted signal is blank on
a package's observation, that package gets **no score at all** and the row names the
signal. Blank means missing and is never invented — a score that read a missing
signal as zero would rank a package this product has observed nothing about *below*
one it has. Weighting only the two required signals is the shape that always
produces a score.

A score is never `0`: the database refuses it. `NULL` is "not scored" and `1` is the
bottom of `CPM-FR-20`'s range, and those are different facts.

### Rank is derived, not stored

`CPM-PRIORITY-S01`'s AC 1 asks that rank be derived from bucket and score and be
stable for a run, and `CPM-AD-1` lists rank among the fields *projected* from the
rollup. There is no `rank` column: `policies/priority.py`'s `ranking_order()` is the
one ordering every read surface applies — bucket, then score descending, then the
package key. The third term is not decoration: without it two packages with the same
bucket and score have no defined order, and two surfaces paginating the same run
would disagree about which comes first.

If you build a read surface over this, call `ranking_order()` rather than writing
your own `ORDER BY`. A row's rank is its position in that ordering.

### Where the result lands

`package_priority`, one row per package per policy run, and `package_health`'s
`priority_status` column — the third the rollup has grown, and the one a queue
filters on without a join. The bucket is on both; the score, the rank and the
explanation are only on `package_priority`, because a rollup contribution carries
status values and nothing else.

The bucket goes onto the rollup through `CPM-AD-4`'s confidence gate, so a package
whose identity was never established reads `unknown` there whatever rule matched it.
The derived row keeps what the pass computed — the gate is the rollup writer's.

### What a run costs

Six indexed reads per package against the derived tables, one inventory read, and
one insert. No outbound call. Every read is bounded by **both** the package and the
policy run, so one run's priority is made entirely of that run's conclusions —
which is what keeps a replay at a stated version and cut-off reproducible.

### `package_priority` accumulates, and nothing prunes it

One row per package per run, never updated and never deleted, every relation
`PROTECT`. The reasoning and the consequences are exactly the six sibling tables',
above.

## The work-type policy: what to do, decided without looking at the priority

`CPM-PRIORITY-S02` adds the eighth policy pass. It reads the six domain passes'
derived rows for the same run and recommends one of the eight actions PRD Appendix
A.1 names — `CPM-FR-21`. Unlike the priority rules, **this one ships working**: the
PRD fixes the closed set, and what each of the eight words means fixes when it
applies.

**It is deliberately not derived from the priority bucket, and it runs before the
pass that assigns one.** A low-priority package still has a recommended action, and
a queue that only told you what to do about `P1` rows would leave every other row
silent. That independence is made structural rather than promised: the work-type
pass is registered *first*, so there is no priority row for the run when it
executes.

This mattered more than it sounded while the shipped rule set was empty and **every**
package was <span class="cs-state unknown">unknown</span> for priority: if the two
were coupled at all, nothing would ever have been recommended. They are not, so the
queue was useful before any priority rule existed — and still is at a pinned older
version.

**The eight, and when each is recommended:**

| Work type | Recommended when |
|---|---|
| `fix_vulnerability` | an advisory matched this package. First, because it is the finding with a clock on it |
| `review_license` | the licence was forbidden, restricted, or no rule named it. Above the packaging actions because it can make them pointless |
| `create_recipe` | no conda-forge feedstock exists, so there is nothing to update |
| `update_feedstock` | a feedstock exists and needs work — nobody is pushing to it, or its recipe is still staged |
| `validate_python_314` | readiness rests on what the metadata *claims* rather than on a build that ran |
| `file_tracking_issue` | a 3.14 build ran and did not come out, or the package is behind on a surface, and no more specific action names it. Last, because a catch-all above anything else makes that thing unreachable |
| `already_tracked` | **never, today** — see below |
| `resolve_identity` | **never, today** — see below |

**Two of the eight are unreachable, on purpose, and the gap is recorded.**

`already_tracked` means a record exists and the work is somebody else's to progress.
Knowing that means reading the workflow queue, which belongs to an application this
product has not built. Nothing currently recorded distinguishes "nobody has filed
this" from "somebody has", so no derivation may claim the second.

`resolve_identity` is the sharper one. Its only signal is the package's identity
confidence, and this product gives that exactly one consumer: the rollup writer's
confidence gate. A second reader would be a second gate — and it would claim
something the product then erases, because the gate replaces every contributed value
for an unmapped package with `unknown`. So an unmapped package's `work_type_status`
reads `unknown` on the rollup whatever was derived, and the derived row keeps what
the pass computed.

Both values exist in the vocabulary and the database accepts them, so the day either
signal exists the value and its constraint are already in place.

**`unknown` is not "nothing to do".** The closed set offers no member for a package
in good order, so a package with nothing to act on and a package nothing was
established about both read `unknown`, and `detail` says so. That is a gap in
`CPM-FR-21`'s set rather than in the derivation — inventing a ninth value would be
this component extending a set the PRD closed.

**`validate_python_314` fires on an inference, not on every unverified package.** A
package whose static readiness is `unknown` — which includes every package nobody
has collected anything about — is **not** told to go and verify it. `CPM-FR-14` says
the static pass says where verification is worth spending, and it says so by reaching
an *inferred* verdict. A package with no assessment has made no claim to check.

### Where the result lands

`package_work_type`, one row per package per policy run, and `package_health`'s
`work_type_status` column — the fourth the rollup has grown. A check constraint holds
the column to the closed set: `choices` is enforced by neither `save()` nor a
migration, and the requirement says a value outside the set is *rejected*.

### What a run costs

Six indexed reads per package — the same rows the priority pass reads, read again
because the two passes are independent — and one insert. No outbound call, and no
parameter file: this derivation is code, because the PRD closed the set and nothing
about it is an open question.

### `package_work_type` accumulates, and nothing prunes it

One row per package per run, never updated and never deleted, every relation
`PROTECT`. The reasoning and the consequences are exactly the seven sibling tables',
above.

## Replaying a policy run: reproducing what the system concluded

`CPM-PRIORITY-S03` gives `CPM-FR-22` a front door. The capability has been there
since the orchestration was built — a policy run takes its evidence cut-off rather
than choosing one — but until now using it meant writing Python. A compliance
reviewer asking "what did this system conclude on 12 June, and can you show me
again?" runs:

```sh
pixi run -e dev manage replay_policy_run --of-run 417
```

That reads the policy version and the evidence cut-off off run 417, executes a new
run at both, compares every column of every derived row against what run 417
concluded, and **exits non-zero if anything differs**. The exit status is the answer;
the printed differences are why.

You can also state both directly, for a version and instant taken from a report:

```sh
pixi run -e dev manage replay_policy_run \
  --policy-version 2026.09.3 --evidence-cutoff 2026-06-12T02:00:00+00:00
```

The cut-off must carry a timezone — every instant this product records is aware, and
a naive one would read a window nobody chose. Stating a version and a cut-off with no
`--of-run` runs the policy but compares nothing, and the output says so rather than
reporting a success it did not check.

### It rewrites current health — know this before you run it

**This is the one consequence that surprises people.** `package_health` holds exactly
one row per package and a policy run *replaces* it. So replaying a three-month-old
cut-off leaves the current-health table showing what was true three months ago, and
every view, export and API read shows that, until the next scheduled policy run puts
it back.

Nothing is hidden: each row carries `computed_at` and the `evidence_cutoff` it was
computed at, so a reader can see the state is historical. But they have to look. The
command therefore prints the warning and asks for confirmation before doing anything;
`--no-input` skips the prompt for scripted use.

If that is unacceptable in your deployment, replay against a restored copy of the
database rather than against production. The derived tables — `package_currency`,
`package_vulnerability`, `package_priority` and the rest — are keyed by run and
accumulate, so both runs' conclusions survive there either way; it is only the rollup
that is replaced.

### What a replay does not do

**It collects nothing.** No collector runs, no outbound call is made, and no evidence
row is written or changed. Every pass reads evidence `observed_at <= cutoff`, and
evidence is append-only, so the rows the original run read are still there and still
say the same thing. That is what "re-runnable against historical evidence" means.

**It does not touch the run it replays.** Derived tables are keyed
`(package, policy_run)`, so the replay adds its own rows beside the original's. That
is what makes the comparison possible at all.

### When a replay does not reproduce

A difference is a real finding, and there are three things it can mean:

- **a pass is not deterministic** at a fixed cut-off — it read a clock, or the newest
  row rather than the newest row at or before the cut-off;
- **the reviewed parameter file changed for that version** — entries are supposed to
  be added and never edited, precisely so this cannot happen;
- **evidence was not append-only** — something updated or deleted a row the original
  run read.

The report names the table, the package and the column for each difference, which is
usually enough to tell which of the three it is.
