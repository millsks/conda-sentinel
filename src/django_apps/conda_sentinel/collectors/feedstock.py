"""Whether conda-forge has a feedstock for a package, what its recipe pins, and who has touched it.

`CPM-FR-9`: obtain "feedstock existence, recipe version, recipe metadata, and
recent recipe activity", record absence as an observation with a timestamp rather
than as a null, and record staged-recipe state separately from an existing
feedstock. This module is that collector.

**Which question is asked is decided from identity, before any call is made.**
`CPM-FR-1` resolves "zero or more conda-forge feedstocks", so resolution has
already answered "does this package have a feedstock" -- and `CPM-UJ-2` requires
that absence of a feedstock is never claimed for a package whose identity is
unresolved. The branch follows from that and from nothing else:

- a `feedstock` mapping recorded `established` **with rows** -> ask the
  repository those rows name. Its `404` is a real fact: the feedstock resolution
  named is gone.
- a mapping recorded `established` with **no** rows, or recorded `not_found` ->
  resolution looked and found none. `CPM-AD-1`'s successful empty result and the
  informative negative are the same question here, and the interesting one is
  the staged-recipes queue -- so that is the call the base makes, and `translate`
  then confirms the *conventional* feedstock's absence with its own bounded
  second call. Both facts land on one row.
- a mapping recorded `not_applicable` -> the base's `inapplicability` hook, one
  `not_applicable` row, no call and no allowance.
- a mapping that is `unknown` or `error`, or absent -> refused (`failed` run, no
  row), on the terms `CPM-CURRENCY-S02` set: "resolution has not decided" is not
  "does not apply", and recording it as one would be the guess `CPM-FR-1`
  forbids.

**Why the staged-recipe question is never asked from `sentinel_evidence`.** The
base's `not_found` branch writes its row through `sentinel_evidence` and never
reaches `translate`, so a collector that wanted to look something up on the
absent path would have to make a call from a row-shaping hook -- on a path where
a raised exception replaces the reason the run is recording. Choosing the branch
from identity puts the always-succeeding call first exactly where absence is
expected, so every fetch this collector makes happens in `translate`, where a
failure is a `detail` rather than a lost reason.

**Two calls at most, on either branch, and the second never fails the run.** The
mapped branch reads the repository and then its recipe; the absent branch reads
the staged-recipes search and then the conventional repository. Each second call
is bounded the way `collectors/source_release.py` bounds its tag fallback: one
locator, one page, and any failure of it becomes a sentence in `detail` beside
an answer the first call already established.

**The declared allowance is GitHub's *search* allowance, and that is the tighter
of the two.** The absent branch reads `/search/issues`, which GitHub limits far
below its core API, and the base charges one allowance per collection without
knowing which branch a package will take. Declaring the tighter number is the
only honest option; what it costs at `CPM-NFR-1` volume is recorded as deferred
rather than hidden. With `CPM_GITHUB_TOKEN` set (`CPM-OPERATE-S05`) the instance
declares the authenticated search allowance instead and sends the credential
to `api.github.com` -- and to that host only: the recipe read against
`raw.githubusercontent.com` carries no `Authorization`, because that host
neither needs it nor counts against the allowance it buys.

**Recipe activity is the feedstock repository's last push.** PRD Open Question 10
asks what counts; this module answers "a push to the feedstock" and records the
instant. The threshold that makes a gap *inactivity* is `CPM-FR-40`'s versioned
policy parameter (`CPM-CURRENCY-S07`), and deriving one here would be writing a
derived status into an append-only row (`CPM-AD-8`).

**The recipe version is read the way conda-forge writes it, and no Jinja is
rendered.** Every conda-forge recipe opens with `{% set version = "x.y.z" %}` and
interpolates it into `package: version:`; the fallback is a literal `version:`
under `package:`. A recipe that computes its version some other way records the
feedstock as *present* -- which is what `state` claims -- with the version blank
and `detail` saying it could not be read. Rendering a template to find out would
mean executing recipe-authored code in a collector, and is out of scope.

**The pure functions are the whole of what this module decides.**
`feedstock_repository`, the three locators, `repository_facts`, `recipe_facts`,
`staged_recipe`, `asks_about` and `inapplicability_of` take data and return data,
reachable with no database, no socket and no clock (`CPM-AD-27`).

**The `error`, `not_found` and `not_applicable` rows the base writes go through
`sentinel_evidence`, and this module invents none of them.** The base decides
which sentinel and that there is always one (`CPM-NFR-3`); this module decides
what a row in `feedstock_snapshots` looks like, and refuses a state it has no row
shape for.

**On the `AD-` prefix.** A bare `AD-n` in this repository is an *inherited*
platform decision; a decision from this product's own architecture spine always
carries the `CPM-` prefix.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from datetime import timedelta
from types import MappingProxyType
from typing import TYPE_CHECKING
from typing import ClassVar
from typing import Final
from urllib.parse import quote

from django.db import models

from conda_sentinel.collectors.agent import USER_AGENT
from conda_sentinel.collectors.github import AUTHENTICATED_SEARCH_ALLOWANCE
from conda_sentinel.collectors.github import GITHUB_API_HOST
from conda_sentinel.collectors.github import authenticated_headers
from conda_sentinel.collectors.github import github_token
from conda_sentinel.collectors.models import FeedstockSnapshot
from conda_sentinel.core.clock import is_aware
from conda_sentinel.core.collection import Collector
from conda_sentinel.core.collection import CollectorConfigurationError
from conda_sentinel.core.collection import failure_detail
from conda_sentinel.core.collection import request_headers
from conda_sentinel.core.credentials import carries_credential
from conda_sentinel.core.ledger import current_trace_id
from conda_sentinel.core.outcomes import OutcomeState
from conda_sentinel.core.rate_limit import RateLimit
from conda_sentinel.core.transport import DEFAULT_RETRIES
from conda_sentinel.core.transport import TransportError
from conda_sentinel.identity.models import ESTABLISHED
from conda_sentinel.identity.models import Feedstock
from conda_sentinel.identity.models import MappingKind
from conda_sentinel.identity.models import PackageMapping

if TYPE_CHECKING:
    from collections.abc import Iterable
    from collections.abc import Mapping
    from collections.abc import Sequence

    from conda_sentinel.core.clock import Clock
    from conda_sentinel.core.models import AppendOnlyModel
    from conda_sentinel.core.rate_limit import RateLimiter
    from conda_sentinel.core.response_cache import ResponseCache
    from conda_sentinel.core.transport import Payload
    from conda_sentinel.core.transport import Transport

__all__ = [
    "ABSENT_BRANCH",
    "ABSENT_FEEDSTOCK_DETAIL",
    "AMBIGUOUS_STAGED_RECIPE_DETAIL",
    "COLLECTOR_NAME",
    "CONDA_FORGE_ORG",
    "FEEDSTOCK_CACHE_TTL",
    "FEEDSTOCK_CADENCE",
    "FEEDSTOCK_FRESHNESS_TARGET",
    "FEEDSTOCK_HEADERS",
    "FEEDSTOCK_OBSERVATION_WINDOW",
    "FEEDSTOCK_RATE_LIMIT",
    "FEEDSTOCK_RETRIES",
    "FEEDSTOCK_SUFFIX",
    "FEEDSTOCK_TIMEOUT",
    "GITHUB_API_HOST",
    "GITHUB_RAW_HOST",
    "HTML_URL_FIELD",
    "ITEMS_FIELD",
    "MAPPED_BRANCH",
    "MAX_BUILD_NUMBER",
    "MAX_DOCUMENT_CHARACTERS",
    "MAX_RECIPE_CHARACTERS",
    "NAME_FIELD",
    "NEITHER_DETAIL",
    "NO_STAGED_RECIPE_DETAIL",
    "OVERFULL_QUEUE_DETAIL",
    "PUSHED_AT_FIELD",
    "RECIPE_PATH",
    "SEARCH_RESULTS_PER_PAGE",
    "STAGED_RECIPES_REPOSITORY",
    "TITLE_FIELD",
    "TOLERATED_MISSED_RUNS",
    "TOTAL_COUNT_FIELD",
    "UNCHECKED_FEEDSTOCK_DETAIL",
    "UNCHECKED_QUEUE_DETAIL",
    "UNESTABLISHED_FEEDSTOCK_DETAIL",
    "UNREADABLE_RECIPE_DETAIL",
    "UNUSABLE_PUSH_DETAIL",
    "ConventionalAnswer",
    "FeedstockCollector",
    "FeedstockDocumentError",
    "FeedstockIdentity",
    "FeedstockLocatorError",
    "RecipeFacts",
    "RepositoryFacts",
    "StagedRecipe",
    "asks_about",
    "branch_of",
    "feedstock_repository",
    "inapplicability_of",
    "recipe_facts",
    "recipe_locator",
    "repository_facts",
    "repository_locator",
    "staged_recipe",
    "staged_recipes_locator",
]

#: What this collector is called, on its ledger rows, in its cache keys and in
#: the registry `config/startup/stage_two.py` sweeps. It is the `collect` half of
#: its task name too, which is what routes it (`core/queues.py`).
COLLECTOR_NAME: Final[str] = "feedstock"

#: How often this collector is meant to run, and the number every other interval
#: below is derived from.
#:
#: `CPM-NFR-2` gives version currency "daily to weekly", and this is the **slow**
#: end -- the opposite choice from the two release collectors, and it is a choice
#: about the surface rather than a relaxation. A feedstock is a *recipe
#: repository*: it changes when a maintainer opens a pull request, not when an
#: upstream project publishes, so a daily read spends six of every seven calls
#: confirming a fact that has not moved. The cadence itself is data in
#: `django_celery_beat` (`CPM-AD-20`); this is the number the arithmetic below
#: assumes, and `CPM-CURRENCY-S05` reconciles the two at start-up, in both
#: directions, from `collectors/apps.py`'s `ready()`.
FEEDSTOCK_CADENCE: Final[timedelta] = timedelta(days=7)

#: How many consecutive missed collections may pass before this product stops
#: calling an answer current -- PRD Open Question 7's risk posture for the
#: version-currency signal class, which this surface belongs to.
TOLERATED_MISSED_RUNS: Final[int] = 1

#: How long this collector's evidence may be read as current (`CPM-AD-28`):
#: `cadence x (1 + tolerated_missed_runs)`, strictly greater than the cadence so
#: a package does not read stale at exactly the moment its next run is due.
FEEDSTOCK_FRESHNESS_TARGET: Final[timedelta] = FEEDSTOCK_CADENCE * (1 + TOLERATED_MISSED_RUNS)

#: How long a successful observation suppresses the next one (`CPM-AD-7`). Half
#: the cadence, so a scheduled run is never suppressed by the previous one and a
#: second run of one package inside half a week still is.
FEEDSTOCK_OBSERVATION_WINDOW: Final[timedelta] = FEEDSTOCK_CADENCE / 2

#: How many times a failed request is retried, and therefore what the rate
#: limiter is charged against per collection.
FEEDSTOCK_RETRIES: Final[int] = DEFAULT_RETRIES

#: Seconds any single connect or read phase may take.
#:
#: **Four rather than the five the two release collectors declare, and the
#: difference is the second call.** `core/transport.py`'s
#: `worst_case_call_seconds()` bounds the *retried* call the base makes; this
#: collector then makes a bounded second one inside `translate`, outside that
#: policy, so one collection's real ceiling is the computed worst case plus one
#: un-retried connect and read. At five seconds that pair comes to 53 of the
#: inherited 60-second soft limit (`CPM-AD-9`), which leaves the ledger writes
#: around it seven; at four it comes to 43 and leaves seventeen.
#: `tests/unit/django_apps/test_feedstock.py` reconciles the whole of it against
#: the settings module's own declared limit rather than against a number repeated
#: here.
FEEDSTOCK_TIMEOUT: Final[float] = 4.0

#: How hard this collector may push its source (`CPM-AD-20`).
#:
#: **This is GitHub's search allowance, not its core allowance, and the
#: difference is the decision.** The absent branch reads `/search/issues`, which
#: GitHub limits to ten requests a minute for an unauthenticated caller -- far
#: below the sixty an hour its core API allows, but counted per minute rather
#: than per hour. The base charges one allowance per collection before it knows
#: which branch a package will take, so a single number has to cover both, and
#: the tighter of the two is the only one that cannot be exceeded by accident.
#: At `1 + retries` per collection that is two and a half packages a minute:
#: about 67 hours for `CPM-NFR-1`'s ten thousand, which a weekly cadence and a
#: fourteen-day target absorb, slowly.
#:
#: This is the class's declaration and it stays the unauthenticated number,
#: because it is what every audit and every declaration test reads. With
#: `CPM_GITHUB_TOKEN` set (`CPM-OPERATE-S05`) the *instance* declares
#: `collectors/github.py`'s `AUTHENTICATED_SEARCH_ALLOWANCE` instead -- thirty a
#: minute, still the search pool's number and still the tighter of the two, so
#: about 450 collections an hour and ten thousand packages in about 22 hours --
#: and sends the credential to the API host and to no other. The number and the
#: credential are one decision, made once in `__init__`; and the setting is
#: read once per process, so a rotated token reaches the next worker restart.
FEEDSTOCK_RATE_LIMIT: Final[RateLimit] = RateLimit(calls=10, per=timedelta(minutes=1))

#: What this collector's source expects on every request (`CPM-AD-20`,
#: `CPM-AD-27`): declared here, merged and sent by the base, never by this module.
#:
#: `Accept` and `X-GitHub-Api-Version` are what the API host asks for and pin the
#: representation `repository_facts` and `staged_recipe` read. The raw host this
#: collector reads recipes from ignores both and serves the file, which is why a
#: single declaration serves all three locators. The `User-Agent` is the one
#: identity every collector shares (`collectors/agent.py`); GitHub requires one.
#: Nothing conditional is declared -- the validators are the base's. Nothing
#: *credential* is declared here either: `Authorization` is added per instance
#: by `collectors/github.py`'s `authenticated_headers` when `CPM_GITHUB_TOKEN`
#: is set, and stripped again by the base for the one locator on the raw host
#: (`credential_host`), so this mapping is exactly what an unauthenticated
#: request carries.
FEEDSTOCK_HEADERS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": USER_AGENT,
    },
)

#: How long a remembered response may be replayed before it is re-read. Longer
#: than the cadence, so a scheduled collection revalidates rather than
#: re-transfers; an entry that expired inside the cadence would make the cache
#: inert. GitHub serves an `ETag` on the repository endpoint, which is what makes
#: a `304` reachable at all.
FEEDSTOCK_CACHE_TTL: Final[timedelta] = timedelta(days=30)

#: The two hosts this collector reads. Separate constants because they are
#: separate facts: one answers questions about a repository, the other serves a
#: file out of one -- and only the first is sent the credential
#: (`CPM-OPERATE-S05`). The API host is `collectors/github.py`'s spelling,
#: re-exported here so every existing importer is untouched: the comparison
#: that decides which locator carries `Authorization` has to read the same
#: string this module builds its locators from.
GITHUB_RAW_HOST: Final[str] = "raw.githubusercontent.com"

#: The organisation every conda-forge feedstock and the staged-recipes queue live
#: under, and the repository that queue is.
CONDA_FORGE_ORG: Final[str] = "conda-forge"
STAGED_RECIPES_REPOSITORY: Final[str] = "staged-recipes"

#: The suffix conda-forge gives every feedstock repository. Added once -- a
#: mapping may store `numpy` or `numpy-feedstock`, and both name one repository.
FEEDSTOCK_SUFFIX: Final[str] = "-feedstock"

#: The two questions this collector asks, named so that "which one did this run
#: ask" is a value rather than a boolean. A boolean has only two states and this
#: has three: a run may also have asked *neither*, which is what a collection
#: whose question did not apply -- or a caller reaching a hook directly -- is in,
#: and a row shaped as though it had taken the absent branch would say something
#: that run never established.
MAPPED_BRANCH: Final[str] = "mapped"
ABSENT_BRANCH: Final[str] = "absent"

#: Where a feedstock keeps its recipe, and the reference this collector reads it
#: at. `HEAD` rather than a branch name: conda-forge feedstocks are overwhelmingly
#: on `main` and a long tail is still on `master`, and the raw host resolves
#: `HEAD` to whichever the repository's default branch is.
RECIPE_PATH: Final[str] = "HEAD/recipe/meta.yaml"

#: How many search results one page asks for. One page is all a collection reads;
#: the count is what decides "one open pull request" from "more than one", and a
#: handful is enough to tell those apart without paging.
SEARCH_RESULTS_PER_PAGE: Final[int] = 20

#: The fields of the repository and search documents this collector reads, named
#: rather than spelled at the call sites so the reader and the cases that build
#: documents cannot drift.
NAME_FIELD: Final[str] = "name"
HTML_URL_FIELD: Final[str] = "html_url"
PUSHED_AT_FIELD: Final[str] = "pushed_at"
ITEMS_FIELD: Final[str] = "items"
TITLE_FIELD: Final[str] = "title"

#: How many results the search says it matched in total, which is not the same as
#: how many this collector was served. One page is read, so a queue with more
#: word-matching titles than fit on it can push this package's genuine pull
#: request onto a page nobody asks for -- and a row that then said "there is no
#: staged recipe" would be claiming an absence this run did not establish. Read so
#: that case can be recorded as what it is.
TOTAL_COUNT_FIELD: Final[str] = "total_count"

#: The longest repository or search document this collector will hand to
#: `json.loads`, in characters.
#:
#: A repository object is a few kilobytes and a page of search results a few tens;
#: four mebibytes is orders of magnitude above either. **What the bound protects
#: is the parse and nothing earlier**: by the time a body arrives here the
#: transport has already transferred it and decoded it to a string, so this saves
#: neither the transfer nor the memory of holding it -- what it refuses is handing
#: `json.loads` a document no honest source serves, which is where a worker's soft
#: time limit would be spent (`CPM-AD-9`). Bounding the transfer needs a streamed
#: read in `core/transport.py` and is recorded as deferred.
MAX_DOCUMENT_CHARACTERS: Final[int] = 4 * 1024 * 1024

#: The largest build number this collector will record: PostgreSQL's `integer`
#: ceiling, which is what `PositiveIntegerField` maps to on the database this
#: product deploys against.
#:
#: **Declared rather than read off the column, and that is the `R-5` parity
#: argument in an integer.** Django derives a `PositiveIntegerField`'s validators
#: from the *running* backend's range: PostgreSQL stops at `2**31 - 1` and SQLite
#: stores a 64-bit value, so a reader that took the ceiling from the field would
#: accept, on a developer's machine, a number the deployed database refuses at
#: insert -- and it would refuse it *outside* `translate`'s own `try`, on a branch
#: whose first call had already established the feedstock exists. The narrow
#: number is the honest one. `tests/unit/django_apps/test_feedstock.py` asserts it
#: never exceeds what the backend under test would take.
MAX_BUILD_NUMBER: Final[int] = 2_147_483_647

#: The longest recipe this collector will scan, in characters. A conda-forge
#: `meta.yaml` is a few kilobytes; one mebibyte is a bound rather than an
#: expectation. Exceeding it leaves the recipe unread with `detail` saying so,
#: because a recipe this collector could not read never fails the collection.
MAX_RECIPE_CHARACTERS: Final[int] = 1024 * 1024

#: What a repository segment may be spelled with, once this collector has
#: lowercased it.
#:
#: GitHub permits letters, digits, hyphens, underscores and dots, and permits a
#: name to *begin* with any of the first three. The leading underscore is not a
#: liberty: `_openmp_mutex` and `_libgcc_mutex` are real conda-forge packages with
#: real feedstocks, and a grammar that refused them would make those two
#: permanently uncollectable. What the first character may not be is `-` or `.`,
#: which is also what refuses `.` and `..` -- a navigation instruction rather than
#: a name, and one percent-encoding does not close, because both are unreserved.
_REPOSITORY_SEGMENT: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9_][a-z0-9._-]*$")

#: The assignment every conda-forge recipe opens with, and the one this collector
#: reads first. Written `{% set version = "1.2.3" %}` -- with or without the
#: whitespace-control hyphens, and with either quote.
_SET_VERSION: Final[re.Pattern[str]] = re.compile(
    r"\{%-?\s*set\s+version\s*=\s*[\"']([^\"'\n]*)[\"']\s*-?%\}",
)

#: A top-level YAML key -- a section name at column zero -- and an indented
#: `key: value` beneath one. The fallback reader walks the recipe with these two
#: rather than parsing YAML: a recipe is a Jinja *template*, so it is not valid
#: YAML until it has been rendered, and rendering it is out of scope.
_SECTION: Final[re.Pattern[str]] = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*)\s*:")
_ENTRY: Final[re.Pattern[str]] = re.compile(r"^\s+([A-Za-z_][A-Za-z0-9_-]*)\s*:\s*(.*?)\s*$")

#: The recipe sections and keys the fallback reads.
_PACKAGE_SECTION: Final[str] = "package"
_BUILD_SECTION: Final[str] = "build"
_VERSION_KEY: Final[str] = "version"
_NUMBER_KEY: Final[str] = "number"

#: What marks a recipe value as an unrendered template rather than a literal.
_TEMPLATE_MARKER: Final[str] = "{{"

#: How a pull-request title is cut into candidate names, and how a name is
#: normalised once it has been.
#:
#: The split is on everything a package name *cannot* contain -- whitespace, a
#: colon, a bracket -- and deliberately not on `-`, `_` or `.`, which names do
#: contain. That is the whole of what keeps `Add numpy-quaternion` from reading
#: as a recipe for `numpy`: split on hyphens too and the two titles produce the
#: same token. What is left is normalised the way PEP 503 normalises a project
#: name, so `Add zope.interface`, `add zope_interface` and `Add zope-interface`
#: all name one package.
_TITLE_TOKENS: Final[re.Pattern[str]] = re.compile(r"[^A-Za-z0-9_.-]+")
_NAME_SEPARATORS: Final[re.Pattern[str]] = re.compile(r"[-_.]+")

#: What a row says in its own words, in each of the ways it is reachable. Named
#: so the row a run writes and the case that reads it back cannot drift.
ABSENT_FEEDSTOCK_DETAIL: Final[str] = (
    "conda-forge has no feedstock for this package: resolution established none and the conventional feedstock "
    "repository is absent"
)
NO_STAGED_RECIPE_DETAIL: Final[str] = "the staged-recipes queue holds no open pull request naming it either"
#: What the row records when both were looked for and neither was found -- the
#: matrix's "no feedstock, no staged recipe". Composed from the two sentences
#: rather than written a third time, so the case that reads it back and the run
#: that writes it cannot come to disagree about either half.
NEITHER_DETAIL: Final[str] = f"{ABSENT_FEEDSTOCK_DETAIL}; {NO_STAGED_RECIPE_DETAIL}"
AMBIGUOUS_STAGED_RECIPE_DETAIL: Final[str] = (
    "the staged-recipes queue holds more than one open pull request naming this package, so none of them is "
    "recorded: which one would create this feedstock is not a question this collector may answer by picking"
)
#: What a row records when the conventional feedstock could not be *read* rather
#: than found absent. Kept apart from `ABSENT_FEEDSTOCK_DETAIL` because the two
#: are opposite claims: one says the repository is not there, and this one says
#: nobody looked successfully -- and writing the first where the second is true
#: would put an absence nobody established into a log nothing may correct.
UNCHECKED_FEEDSTOCK_DETAIL: Final[str] = (
    "the conventional conda-forge feedstock repository could not be read, so this row records that resolution "
    "established no feedstock and not that none exists"
)
#: What a `not_found` sentinel records on the absent branch, where the call that
#: failed was the staged-recipes search itself. The base's own sentence says the
#: locator reports the resource does not exist -- which is true of the *search
#: endpoint* and says nothing whatever about this package's feedstock.
UNCHECKED_QUEUE_DETAIL: Final[str] = (
    "the staged-recipes queue itself could not be read and no feedstock repository was checked, so this row "
    "records that resolution established no feedstock and not that none exists"
)
#: What a row records when the search matched more results than one page holds.
OVERFULL_QUEUE_DETAIL: Final[str] = (
    "the staged-recipes search matched more results than one page holds, so whether an open pull request names "
    "this package is not established by this run"
)
UNESTABLISHED_FEEDSTOCK_DETAIL: Final[str] = (
    "resolution had not established this feedstock: it is the conventional conda-forge repository for the "
    "package's canonical name, and it answered"
)
UNREADABLE_RECIPE_DETAIL: Final[str] = "the recipe's version could not be read"
UNUSABLE_PUSH_DETAIL: Final[str] = "the repository stated no push instant this collector could read"


class FeedstockLocatorError(ValueError):
    """A package's feedstock identity cannot be turned into a locator to read.

    A `ValueError` subclass, matching `collectors/source_release.py`'s
    `SourceLocatorError` and `collectors/pypi_release.py`'s `PyPILocatorError`:
    every "this input cannot describe what it claims to" in this product is a
    `ValueError`.

    **It escapes `collect()` rather than becoming an evidence row, and that is a
    decision rather than an omission.** `source_for` is called before the window,
    the allowance and the transport, so the run ledger row exists and is finalized
    `failed` carrying this message. What it refuses is a package whose feedstock
    mapping resolution has not reached -- `unknown`, `error`, or no mapping row at
    all -- and a mapping whose feedstock name is not a repository this collector
    could ask about. Neither is `not_applicable`: that state is for a mapping
    resolution *did* reach and found inapplicable, and it goes through the base's
    `inapplicability` hook.

    `CPM-UJ-2` is the reason it is a refusal rather than an absence row: "absence
    of a feedstock cannot be claimed for a package whose identity is unresolved",
    and a `not_found` row is exactly that claim. The selection that offers only
    askable packages is `CPM-CURRENCY-S05`'s.
    """


class FeedstockDocumentError(ValueError):
    """A repository or search document could not be read as what it claims to be.

    Raised from `translate`, which the base answers by writing an `error` row and
    re-raising unchanged -- so `CPM-NFR-3`'s guarantee holds on this path too:
    never a clean result, and never no row. Refused rather than partially read: a
    document this collector cannot understand is a source whose shape has changed,
    and reading around it would record a feedstock chosen from whatever happened
    to still parse, permanently.

    A *recipe* never raises this. The recipe is the second document on the mapped
    branch, read by a call whose failure may not fail the collection, so a recipe
    this collector cannot read leaves the version blank and says so in `detail`.
    """


@dataclass(frozen=True, slots=True)
class FeedstockIdentity:
    """What resolution recorded about one package's conda-forge feedstocks.

    The identity read this collector makes, held as a value so the two hooks that
    need it -- `inapplicability` and `source_for` -- read it once and agree about
    what it said.

    Attributes:
        outcome: The `feedstock` mapping's outcome, as `PackageMapping` spells
            it: `established` or one of `core`'s four sentinels.
        canonical_name: The package's own name, which is what the conventional
            conda-forge repository is derived from where resolution established
            no feedstock.
        feedstock_names: Every feedstock the mapping holds, by name, in name
            order. Empty for a mapping that established none -- which is
            `CPM-FR-6`'s successful empty result and not an error.

    """

    outcome: str
    canonical_name: str
    feedstock_names: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RepositoryFacts:
    """What one feedstock repository document says.

    Attributes:
        name: The repository's own name, which is the feedstock's name.
        url: Where a person can open it -- the repository's web URL, not the API
            locator this collector asked.
        pushed_at: When it was last pushed to, or `None` when the source stated
            no instant this collector could read.
        detail: Why there is no activity instant, where there is not. Empty for
            an ordinary answer.

    """

    name: str
    url: str
    pushed_at: datetime | None
    detail: str


@dataclass(frozen=True, slots=True)
class RecipeFacts:
    """What one recipe says, as the facts the evidence row holds.

    Never a failure: a recipe this collector could not read is a blank version
    and a `detail`, because the feedstock's existence is what the row's `state`
    claims and the recipe is a second document.

    Attributes:
        version: The version the recipe pins, exactly as spelled, or blank.
        build_number: The build number it declares, or `None`.
        metadata_url: Where the recipe was read from, or blank when it was not
            read at all.
        detail: Why the version is blank, where it is. Empty for an ordinary
            answer.

    """

    version: str
    build_number: int | None
    metadata_url: str
    detail: str


@dataclass(frozen=True, slots=True)
class StagedRecipe:
    """What the staged-recipes queue holds for one package.

    Attributes:
        url: The one open pull request that would create this feedstock, or
            blank when the search matched none -- or matched more than one, which
            is refused rather than picked.
        matched: How many open pull requests named this package. Recorded as well
            as the URL because `0` and `2` produce the same blank URL for
            opposite reasons, and `detail` has to say which.
        total: How many results the search says it matched in total, which may be
            more than were served on the one page this collector reads.
        truncated: Whether the queue held more results than one page. When it did,
            a blank URL is not evidence of absence -- the match may be on a page
            nobody asked for -- and the row says so instead of claiming there is
            no staged recipe.

    """

    url: str
    matched: int
    total: int
    truncated: bool


@dataclass(frozen=True, slots=True)
class ConventionalAnswer:
    """What asking about the conventional conda-forge repository established, and what it did not.

    The value the absent branch's second call returns, and it exists because
    "asked, and it is not there" and "could not ask" are opposite claims that a
    bare `RepositoryFacts | None` cannot tell apart -- so a row built from one
    would say the feedstock is absent when nobody had looked.

    Attributes:
        facts: What the repository said, or `None` when it said nothing this
            collector could read.
        source: The locator that was asked, recorded on a determinate row because
            that is where its facts came from.
        detail: What this call established. `ABSENT_FEEDSTOCK_DETAIL` for a
            repository that answered "absent"; a sentence beginning
            `UNCHECKED_FEEDSTOCK_DETAIL` for every way of not finding out; and the
            facts' own reason when it answered.
        established: Whether the repository *answered that it is not there*, as
            opposed to this run failing to find out. True on exactly one path --
            a `404` from the conventional repository -- and False on every way of
            not asking successfully. It says the same thing `detail` does and it
            says it structurally, which is what lets a reader outside this module
            tell an absence from a failure to look without matching on a
            sentence. `False` when `facts` is not None, because a repository that
            answered established a *presence*.

    """

    facts: RepositoryFacts | None
    source: str
    detail: str
    established: bool = False


def feedstock_repository(name: str) -> str:
    """Return the conda-forge repository a feedstock name addresses, or refuse it.

    Pure: no database, no clock, no network. The name is lower-cased, because
    GitHub treats a repository name case-insensitively while every key built from
    the result is exact -- `NumPy-Feedstock` and `numpy-feedstock` are one
    repository, and reading them as two would mean two response-cache entries and
    two spellings of `source` in an append-only log. The `-feedstock` suffix is
    added exactly once: a mapping may legitimately store either spelling.

    Args:
        name: A feedstock's name as resolution stored it, or a package's
            canonical name where resolution established no feedstock.

    Returns:
        The repository segment, suffixed once and lower-cased.

    Raises:
        FeedstockLocatorError: When the name is not a string, is blank, is a
            relative reference, or is not a repository segment once lower-cased
            -- and when the result is wider than the column that has to record
            it. Refused rather than repaired, because a stored feedstock name is
            data a resolution wrote (`CPM-FR-1`) and a locator built from a
            repaired one would ask about a repository nobody established.

    """
    segment = _repository_segment(name)
    repository = segment if segment.endswith(FEEDSTOCK_SUFFIX) else f"{segment}{FEEDSTOCK_SUFFIX}"
    width = _column_width("feedstock_name")
    if width is not None and len(repository) > width:
        message = (
            f"{name!r} names the repository {repository!r}, which is {len(repository)} characters, and the "
            f"feedstock_name column that records it takes {width}. Refused rather than truncated: a row that "
            f"cannot say which feedstock it observed is a row an append-only history cannot tell from its "
            f"neighbours."
        )
        raise FeedstockLocatorError(message)
    return repository


def _repository_segment(name: str) -> str:
    """Return a name as a lower-cased repository segment, or refuse it.

    The half of `feedstock_repository` that is about the *name* rather than about
    the feedstock, so the staged-recipes locator -- which names the package and
    not its feedstock -- can refuse an unusable name on the same terms without
    being bound by a width the row it produces never fills.

    Args:
        name: A feedstock's name as resolution stored it, or a package's
            canonical name.

    Returns:
        The segment, stripped and lower-cased.

    Raises:
        FeedstockLocatorError: When the name is not a string, is blank, or is not
            a repository segment once lower-cased -- which is also what refuses
            `.` and `..`, because neither may begin a repository name and
            percent-encoding leaves both untouched.

    """
    if not isinstance(name, str) or not name.strip():
        message = (
            f"a conda-forge feedstock cannot be located for name={name!r}: this package's feedstock identity "
            f"names nothing to ask about. CPM-FR-9 observes the feedstock a resolution established, and "
            f"CPM-UJ-2 forbids claiming absence for a package whose identity is unresolved."
        )
        raise FeedstockLocatorError(message)

    segment = name.strip().lower()
    if not _REPOSITORY_SEGMENT.match(segment):
        message = (
            f"{name!r} is {segment!r} once lower-cased, which is not a repository segment GitHub could hold a "
            f"feedstock under. Refused rather than encoded: a locator built from it would ask about nothing, "
            f"or would be a path the source is entitled to resolve somewhere else."
        )
        raise FeedstockLocatorError(message)
    return segment


def repository_locator(name: str) -> str:
    """Return the locator naming a feedstock repository.

    Args:
        name: A feedstock's name, or a package's canonical name.

    Returns:
        `https://api.github.com/repos/conda-forge/<name>-feedstock`.

    Raises:
        FeedstockLocatorError: On every refusal `feedstock_repository` makes.

    """
    return f"https://{GITHUB_API_HOST}/repos/{CONDA_FORGE_ORG}/{feedstock_repository(name)}"


def recipe_locator(name: str) -> str:
    """Return the locator naming a feedstock's recipe.

    The raw host rather than the API's contents endpoint, and that is a decision:
    the contents endpoint answers with the file base64-encoded inside a JSON
    envelope, so reading it would mean a decode step whose only purpose is to
    undo an encoding the raw host never applies. What the raw host serves is the
    recipe as the maintainer wrote it, which is what `recipe_facts` reads.

    Args:
        name: A feedstock's name, or a package's canonical name.

    Returns:
        `https://raw.githubusercontent.com/conda-forge/<name>-feedstock/HEAD/recipe/meta.yaml`.

    Raises:
        FeedstockLocatorError: On every refusal `feedstock_repository` makes.

    """
    return f"https://{GITHUB_RAW_HOST}/{CONDA_FORGE_ORG}/{feedstock_repository(name)}/{RECIPE_PATH}"


def staged_recipes_locator(name: str) -> str:
    """Return the locator naming the open staged-recipes pull requests for a package.

    The question asked on the branch where resolution established no feedstock:
    "is somebody already creating one". It is a *search*, which is why this
    collector's declared allowance is GitHub's search allowance rather than its
    core one.

    Args:
        name: The package's canonical name. Not suffixed -- a staged recipe is a
            proposal to create a feedstock, so what is searched for is the
            package.

    Returns:
        The search locator, with the query percent-encoded.

    Raises:
        FeedstockLocatorError: When the name is not a string, is blank, or is not
            a repository segment -- the refusals `_repository_segment` makes, so
            one unusable name is refused identically whichever branch it reaches.
            **Not** the width refusal `feedstock_repository` adds on top: this
            locator names the package rather than the feedstock, and the row it
            produces records no feedstock name at all, so a package refused here
            for a bound it never reaches would be a package this collector could
            never say anything about.

    """
    query = f"repo:{CONDA_FORGE_ORG}/{STAGED_RECIPES_REPOSITORY} is:pr is:open in:title {_repository_segment(name)}"
    encoded = quote(query, safe="")
    return f"https://{GITHUB_API_HOST}/search/issues?q={encoded}&per_page={SEARCH_RESULTS_PER_PAGE}"


def repository_facts(body: str, *, source: str, fallback_name: str) -> RepositoryFacts:
    """Read one feedstock repository document into the facts the evidence row holds.

    Pure: no database, no clock, no network.

    Args:
        body: The document the source served.
        source: The locator it was served from, recorded in the refusal messages.
        fallback_name: The repository this collector asked about, used when the
            document names none. A document that answered at all establishes the
            feedstock's existence, and the name this run asked for is a truer
            record of what was observed than a blank column would be.

    Returns:
        The facts. `pushed_at` is `None` where the source stated no usable
        instant, and `detail` then says so.

    Raises:
        FeedstockDocumentError: When the body is longer than
            `MAX_DOCUMENT_CHARACTERS`, is not JSON, is not an object, carries a
            `name`, `html_url` or `pushed_at` of the wrong type, or names a
            feedstock or URL wider than the column that has to hold it.

    """
    document = _document_in(body, source=source)
    name = _optional_string(document, field=NAME_FIELD, source=source) or fallback_name
    url = _optional_string(document, field=HTML_URL_FIELD, source=source)
    pushed = _instant(_optional_string(document, field=PUSHED_AT_FIELD, source=source))

    _require_storable(name, field="feedstock_name", source=source, what="feedstock name")
    _require_storable(url, field="feedstock_url", source=source, what="feedstock URL")
    return RepositoryFacts(
        name=name,
        url=url,
        pushed_at=pushed,
        detail="" if pushed is not None else UNUSABLE_PUSH_DETAIL,
    )


def recipe_facts(body: str, *, source: str) -> RecipeFacts:
    """Read one conda-forge recipe into the facts the evidence row holds.

    Pure: no database, no clock, no network. **Total**: every unreadable recipe
    is a blank version and a `detail`, never an exception -- see the module
    docstring for why the recipe may not fail a collection the repository has
    already answered.

    The version is read the way conda-forge writes it: the
    `{% set version = "..." %}` assignment every feedstock's recipe opens with,
    falling back to a literal `version:` under `package:` for the recipes that do
    not use one. A value that is still a Jinja expression when it is read is
    *not* a version -- rendering it would mean executing recipe-authored code --
    so it is recorded as unreadable, which is what `detail` then says.

    Args:
        body: The recipe the source served.
        source: The locator it was served from, recorded on the row as
            `recipe_metadata_url`.

    Returns:
        The facts. `version` is blank when nothing readable was found, and
        `detail` then names the reason.

    """
    if len(body) > MAX_RECIPE_CHARACTERS:
        return RecipeFacts(
            version="",
            build_number=None,
            metadata_url=source,
            detail=(
                f"{UNREADABLE_RECIPE_DETAIL}: {source} served {len(body)} characters and this collector scans at "
                f"most {MAX_RECIPE_CHARACTERS}"
            ),
        )

    version = _templated_version(body)
    if not version:
        version = _sectioned_value(body, section=_PACKAGE_SECTION, key=_VERSION_KEY)
    build_number = _build_number(body)
    if not version:
        return RecipeFacts(
            version="",
            build_number=build_number,
            metadata_url=source,
            detail=(
                f"{UNREADABLE_RECIPE_DETAIL}: {source} sets it neither as a `set version` assignment nor as a "
                f"literal `version:` under `package:`"
            ),
        )

    width = _column_width("recipe_version")
    if width is not None and len(version) > width:
        return RecipeFacts(
            version="",
            build_number=build_number,
            metadata_url=source,
            detail=(
                f"{UNREADABLE_RECIPE_DETAIL}: {source} names one of {len(version)} characters and the column "
                f"that holds it takes {width}"
            ),
        )
    return RecipeFacts(version=version, build_number=build_number, metadata_url=source, detail="")


def staged_recipe(body: str, *, name: str, source: str) -> StagedRecipe:
    """Read one staged-recipes search document into the one pull request it names, or none.

    Pure: no database, no clock, no network.

    **The search is narrowed here as well as in the query.** GitHub's
    `in:title <name>` is a word match, so a search for `numpy` also returns
    `Add numpy-quaternion`. Every returned title is therefore cut into the tokens
    a package name could be -- splitting on everything a name cannot contain, and
    on nothing it can -- and only a token that normalises to exactly this
    package's name counts as naming it.

    **More than one match is refused rather than picked.** Two open pull requests
    naming one package is a real state of the queue, and choosing between them
    would record a claim about which one will create this feedstock that nobody
    made. The count is returned so `detail` can say how many there were.

    **One page is read, so a queue that overflows it cannot be read as an
    absence.** `total_count` says how many results the search matched, and only
    `SEARCH_RESULTS_PER_PAGE` of them are served; a queue with more word-matching
    titles than that can push this package's genuine pull request onto a page
    nobody asks for. The overflow is carried out rather than swallowed, so a row
    built from it says "not established" instead of "there is none".

    Args:
        body: The document the source served.
        name: The package's canonical name, which the titles are matched against.
        source: The locator it was served from, for the refusal messages.

    Returns:
        The one matching pull request's URL, how many matched, how many the search
        says it found in total, and whether that overflowed the one page read.

    Raises:
        FeedstockDocumentError: When the body is longer than
            `MAX_DOCUMENT_CHARACTERS`, is not JSON, is not an object, has `items`
            that is not a list or `total_count` that is not an integer, holds an
            entry that is not an object, or an entry whose title or URL is not a
            string -- or names a URL wider than the column that has to hold it.

    """
    document = _document_in(body, source=source)
    entries = document.get(ITEMS_FIELD)
    if entries is None:
        entries = []
    if not isinstance(entries, list):
        message = (
            f"{source} served a document whose {ITEMS_FIELD!r} is {type(entries).__name__} rather than a list. A "
            f"source whose shape has changed is refused rather than read for whatever still parses."
        )
        raise FeedstockDocumentError(message)
    total = _optional_count(document, field=TOTAL_COUNT_FIELD, source=source)

    target = _normalised(name)
    matched: list[str] = []
    for position, entry in enumerate(entries):
        if not isinstance(entry, dict):
            message = f"{source} lists {type(entry).__name__} at position {position} rather than a pull request."
            raise FeedstockDocumentError(message)
        title = _optional_string(entry, field=TITLE_FIELD, source=source)
        url = _optional_string(entry, field=HTML_URL_FIELD, source=source)
        if url and _names(target, in_title=title):
            _require_storable(url, field="staged_recipe_url", source=source, what="staged recipe URL")
            matched.append(url)
    return StagedRecipe(
        url=matched[0] if len(matched) == 1 else "",
        matched=len(matched),
        total=max(total, len(entries)),
        truncated=max(total, len(entries)) > SEARCH_RESULTS_PER_PAGE,
    )


def asks_about(identity: FeedstockIdentity) -> bool:
    """Report whether this collector asks conda-forge about a package with this identity.

    The one spelling of the rule. Resolution has to have *reached* the feedstock
    mapping: `established` -- with rows or without, which are two different
    questions and both askable -- or `not_found`, which is resolution saying it
    looked and there is none. Everything else is unresolved, and `CPM-UJ-2`
    forbids claiming absence for an unresolved identity.

    Args:
        identity: What resolution recorded.

    Returns:
        True for a mapping resolution reached.

    """
    if identity.outcome == ESTABLISHED:
        return True
    return identity.outcome == OutcomeState.NOT_FOUND.value


def branch_of(identity: FeedstockIdentity) -> str:
    """Return which question this identity produces, or nothing when it produces none.

    Pure, and the whole of the branch rule -- the outcome **and** the rows
    together, which is what `CPM-FR-6` needs and what reading the rows alone gets
    wrong. `MAPPED_FIELDS[FEEDSTOCK]` is empty because the mapping *is* the child
    rows, so the two halves can contradict each other: a mapping recorded
    `not_found` means "resolution looked and found none", and one that carries
    `Feedstock` rows anyway is a resolution defect rather than a fact about the
    package. Reading the rows alone would take the mapped branch for it and record
    an observation of a feedstock resolution says is not there.

    Args:
        identity: What resolution recorded.

    Returns:
        `MAPPED_BRANCH` for an `established` mapping carrying rows,
        `ABSENT_BRANCH` for a mapping resolution reached that carries none, and
        the empty string for an identity that is unresolved or contradicts
        itself -- both of which `source_for` refuses, with its own message for
        each.

    """
    if not asks_about(identity):
        return ""
    if identity.outcome == OutcomeState.NOT_FOUND.value:
        return "" if identity.feedstock_names else ABSENT_BRANCH
    return MAPPED_BRANCH if identity.feedstock_names else ABSENT_BRANCH


def inapplicability_of(identity: FeedstockIdentity) -> str:
    """Return why conda-forge is not a question about this package, or nothing when it is -- or might be.

    Pure: the whole of the applicability rule, over what resolution recorded and
    nothing else, so every branch of it is reachable with no database
    (`CPM-AD-27`). `FeedstockCollector.inapplicability` reads the identity and
    hands it here.

    Args:
        identity: What resolution recorded.

    Returns:
        The reason when the mapping was recorded `not_applicable`. The empty
        string otherwise -- for a mapping resolution reached, and for an
        unresolved one, because "resolution has not decided" is not "does not
        apply". Both of the latter are `source_for`'s to answer, and only the
        first is answered with a locator.

    """
    if identity.outcome == OutcomeState.NOT_APPLICABLE.value:
        return (
            f"resolution recorded this package's feedstock identity as {identity.outcome!r}: the package's type "
            f"gives it no conda-forge feedstock to ask about (CPM-FR-1, CPM-FR-9)."
        )
    return ""


def _document_in(body: str, *, source: str) -> dict[str, object]:
    """Decode a document and refuse anything that is not an object.

    Args:
        body: The document the source served.
        source: The locator it was served from, for the messages.

    Returns:
        The decoded object.

    Raises:
        FeedstockDocumentError: When the body is too long to decode, is not JSON,
            or is not an object.

    """
    if len(body) > MAX_DOCUMENT_CHARACTERS:
        message = (
            f"{source} served {len(body)} characters, and this collector decodes at most "
            f"{MAX_DOCUMENT_CHARACTERS}. A repository object is kilobytes and a page of search results tens of "
            f"them, so a document this size is a source doing something else -- and parsing it would spend a "
            f"worker's soft time limit finding out (CPM-AD-9)."
        )
        raise FeedstockDocumentError(message)
    try:
        document = json.loads(body)
    except (json.JSONDecodeError, RecursionError) as unreadable:
        # `RecursionError` beside the decode error deliberately: `json.loads`
        # recurses per level of nesting, and an uncaught one escapes as a crash
        # naming this module's own stack rather than the source that caused it.
        message = (
            f"{source} did not serve a readable document: {type(unreadable).__name__}: {unreadable}. The "
            f"observation is refused rather than recorded empty, which would say conda-forge has nothing "
            f"(CPM-FR-9)."
        )
        raise FeedstockDocumentError(message) from unreadable
    if not isinstance(document, dict):
        message = (
            f"{source} served {type(document).__name__} rather than an object. A source whose shape has changed "
            f"is refused rather than read for whatever still parses."
        )
        raise FeedstockDocumentError(message)
    return document


def _optional_string(mapping: dict[str, object], *, field: str, source: str) -> str:
    """Return a field that may be absent or null, stripped, or refuse a mistyped one.

    Args:
        mapping: The decoded object the field sits in.
        field: Which field to read.
        source: The locator, for the message.

    Returns:
        The stripped string, or the empty string when the field is absent or
        null -- both of which mean "the source stated none".

    Raises:
        FeedstockDocumentError: When the field is present, is not null, and is
            not a string.

    """
    value = mapping.get(field)
    if value is None:
        return ""
    if not isinstance(value, str):
        message = (
            f"{source} served a document whose {field!r} is {type(value).__name__} rather than a string. A "
            f"source whose shape has changed is refused rather than read past."
        )
        raise FeedstockDocumentError(message)
    return value.strip()


def _optional_count(mapping: dict[str, object], *, field: str, source: str) -> int:
    """Return a non-negative count that may be absent, or refuse a mistyped one.

    Args:
        mapping: The decoded object the field sits in.
        field: Which field to read.
        source: The locator, for the message.

    Returns:
        The count, or `0` when the field is absent or null. A negative one is read
        as `0`: a source that states a negative count has said nothing usable, and
        the only decision the value feeds is "was there more than one page".

    Raises:
        FeedstockDocumentError: When the field is present, is not null, and is not
            an integer. `bool` is refused with the rest: it is an `int` in Python
            and is not a count in any document.

    """
    value = mapping.get(field)
    if value is None:
        return 0
    if not isinstance(value, int) or isinstance(value, bool):
        message = (
            f"{source} served a document whose {field!r} is {type(value).__name__} rather than an integer. A "
            f"source whose shape has changed is refused rather than read past."
        )
        raise FeedstockDocumentError(message)
    return max(value, 0)


def _require_storable(value: str, *, field: str, source: str, what: str) -> None:
    """Refuse a value wider than the column that has to hold it.

    Refused where the value enters rather than where it lands, on the terms
    `collectors/pypi_release.py` states: `max_length` is enforced by PostgreSQL
    and ignored by SQLite, so an over-long value is a stored row on a developer's
    machine and a failed run in the gate (`R-5`).

    Args:
        value: What the document said.
        field: The column it would land in.
        source: The locator, for the message.
        what: What the value is, for the message.

    Raises:
        FeedstockDocumentError: When it is wider than the column.

    """
    width = _column_width(field)
    if width is not None and len(value) > width:
        message = (
            f"{source} names a {what} of {len(value)} characters, and the column that holds it takes {width}. "
            f"The observation is refused rather than truncated: a stored value that is not the value the source "
            f"published is a comparison that will be wrong for ever."
        )
        raise FeedstockDocumentError(message)


def _column_width(field: str) -> int | None:
    """Return how wide one of the evidence table's text columns is.

    Read off the model rather than restated, so the bound a value is refused
    against is the bound the table actually enforces.

    Args:
        field: The column's name.

    Returns:
        Its `max_length`, or `None` for a column that declares none.

    """
    column = FeedstockSnapshot._meta.get_field(field)  # noqa: SLF001 - Django's own public-by-convention API
    return column.max_length if isinstance(column, models.CharField) else None


def _templated_version(body: str) -> str:
    """Return the version a recipe's `set version` assignment names, or nothing.

    Args:
        body: The recipe.

    Returns:
        The first assignment's value, stripped, or the empty string when the
        recipe carries none.

    """
    found = _SET_VERSION.search(body)
    return found.group(1).strip() if found is not None else ""


def _sectioned_value(body: str, *, section: str, key: str) -> str:
    """Return a literal `key: value` beneath a top-level section, or nothing.

    Walked line by line rather than parsed as YAML, because a conda-forge recipe
    is a Jinja template and is not valid YAML until it has been rendered.

    Args:
        body: The recipe.
        section: The top-level key to look beneath.
        key: The entry to read.

    Returns:
        The value with any trailing comment removed and then stripped of
        surrounding whitespace and of one layer of quoting, or the empty string
        when the section, the key or a readable value is absent -- which includes
        a value that is still an unrendered template.

        **The comment comes off before the quoting does**, and the order is the
        rule: `version: 1.0  # first build` names the version `1.0`, and a reader
        that stripped only whitespace and quotes would store the comment as part
        of the version, permanently, in a row nothing may correct.

    """
    inside = False
    for line in body.splitlines():
        heading = _SECTION.match(line)
        if heading is not None:
            inside = heading.group(1) == section
            continue
        if not inside:
            continue
        entry = _ENTRY.match(line)
        if entry is None or entry.group(1) != key:
            continue
        value = _uncommented(entry.group(2)).strip().strip("\"'").strip()
        return "" if _TEMPLATE_MARKER in value else value
    return ""


def _uncommented(value: str) -> str:
    """Return a YAML scalar with any trailing comment removed.

    Args:
        value: One `key:` line's value, as the recipe spelled it.

    Returns:
        The value up to an unquoted `#` that opens a comment, or the whole value
        when it carries none. YAML opens an inline comment at a `#` preceded by
        whitespace or at the start of the value, so a `#` inside a quoted scalar
        -- or one in the middle of a bare word, as a version legitimately may
        carry -- is part of the value and survives.

    """
    quote = ""
    for index, character in enumerate(value):
        if quote:
            if character == quote:
                quote = ""
            continue
        if character in "\"'":
            quote = character
            continue
        if character == "#" and (index == 0 or value[index - 1].isspace()):
            return value[:index]
    return value


def _build_number(body: str) -> int | None:
    """Return the build number a recipe declares, or `None`.

    Args:
        body: The recipe.

    Returns:
        The `number:` under `build:` where it is a plain ASCII decimal integer
        the column can hold, and `None` otherwise. Three things are refused
        rather than guessed at, and each would fail somewhere worse:

        - a Jinja expression, which is a build number this collector cannot read;
        - a non-ASCII decimal digit, which `str.isdigit` calls a digit and `int`
          may refuse -- a `ValueError` escaping a function this module documents
          as total, from the *second* call, on a branch whose first call has
          already established the feedstock exists;
        - a number above `MAX_BUILD_NUMBER`, which this reader would accept and
          the column would refuse at insert, outside `translate`'s own `try`.

    """
    stated = _sectioned_value(body, section=_BUILD_SECTION, key=_NUMBER_KEY)
    if not stated.isascii() or not stated.isdigit():
        return None
    number = int(stated)
    return number if number <= MAX_BUILD_NUMBER else None


def _normalised(name: str) -> str:
    """Return a name reduced to hyphen-separated lowercase, as PEP 503 normalises one.

    Args:
        name: A package name, or one token of a pull-request title.

    Returns:
        The lowercased value with every run of `-`, `_` and `.` collapsed to one
        hyphen and the ends trimmed -- so a trailing full stop in a title does
        not stop a name matching itself.

    """
    return _NAME_SEPARATORS.sub("-", name.lower()).strip("-")


def _names(target: str, *, in_title: str) -> bool:
    """Report whether a pull-request title names this package rather than a neighbour of it.

    Args:
        target: The package's name, already normalised.
        in_title: The title as the source spelled it.

    Returns:
        True when one of the title's own tokens normalises to exactly the name.
        Whole tokens rather than a substring or a hyphen-bounded search: `Add
        numpy-quaternion` is one token, and both of the looser rules would read
        it as a recipe for `numpy`.

    """
    if not target:
        return False
    return any(_normalised(token) == target for token in _TITLE_TOKENS.split(in_title))


def _instant(value: str) -> datetime | None:
    """Return an aware instant a source stated, or `None` where it stated none.

    Args:
        value: What the document carried, already known to be a string.

    Returns:
        The parsed instant, or `None` when the value is blank, does not parse, or
        parses to a naive one. A naive value is discarded rather than assumed to
        be UTC (`CPM-AD-26`).

    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if is_aware(parsed) else None


class FeedstockCollector(Collector):
    """The collector that observes a package's conda-forge feedstock. Writes `feedstock_snapshots`.

    Four hooks and nine declarations. See the module docstring for how the branch
    is chosen from identity, why each branch's second call is bounded, and why an
    unresolved feedstock mapping is refused rather than recorded as absence.
    """

    #: The nine declarations the base checks at construction, every one written
    #: out on the terms `SourceReleaseCollector` gives.
    name: ClassVar[str] = COLLECTOR_NAME

    evidence_model: ClassVar[type[AppendOnlyModel] | None] = FeedstockSnapshot

    observation_window: ClassVar[timedelta | None] = FEEDSTOCK_OBSERVATION_WINDOW

    timeout: ClassVar[float | None] = FEEDSTOCK_TIMEOUT

    retries: ClassVar[int] = FEEDSTOCK_RETRIES

    rate_limit: ClassVar[RateLimit] = FEEDSTOCK_RATE_LIMIT

    headers: ClassVar[Mapping[str, str]] = FEEDSTOCK_HEADERS

    #: The one host the credential in `headers` belongs to (`CPM-OPERATE-S05`).
    #: The base strips `Authorization` from any fetch aimed elsewhere -- which
    #: is what keeps it off `raw.githubusercontent.com` -- and both second
    #: calls below ask the base for their headers on the same terms.
    credential_host: ClassVar[str | None] = GITHUB_API_HOST

    freshness_target: ClassVar[timedelta | None] = FEEDSTOCK_FRESHNESS_TARGET

    response_cache_ttl: ClassVar[timedelta | None] = FEEDSTOCK_CACHE_TTL

    #: How often the full-inventory sweep dispatches this collector
    #: (`CPM-CURRENCY-S05`). Bound to the module constant the target and the
    #: window are already derived from, so no number moves; what changes is that
    #: `config/startup/stage_two.py` now reconciles it against this collector's
    #: `CELERY_BEAT_SCHEDULE` entry at boot. It is the one weekly entry, which is
    #: the reconciliation's whole point -- a schedule that fired this daily would
    #: pass every gate and spend seven times the declared allowance.
    cadence: ClassVar[timedelta | None] = FEEDSTOCK_CADENCE

    #: The identity read, remembered on the instance for the run in progress, on
    #: the terms `PyPIReleaseCollector._identity` states: the base asks
    #: `inapplicability` and then `source_for` about one package in one run, both
    #: need the same answer, and reading it twice would be a second pair of
    #: queries on every collection and a window in which the two could disagree.
    #: Forgotten at the start of every run, so an instance that collects the same
    #: package twice reads fresh each time; keyed by package as well, so a caller
    #: reaching `source_for` directly for another package is not answered about
    #: the last.
    _identity: FeedstockIdentity | None = None
    _identity_package: int | None = None

    def __init__(
        self,
        *,
        clock: Clock,
        transport: Transport | None = None,
        limiter: RateLimiter | None = None,
        response_cache: ResponseCache | None = None,
    ) -> None:
        """Declare the headers and the allowance this instance sends, from the credential setting.

        The shape `SourceReleaseCollector.__init__` gives and for the same
        reasons: the base reads and validates `headers` and `rate_limit` once,
        at construction, so the credential is read once, here, and both are set
        on the instance before the base looks. The class attributes stay the
        unauthenticated declarations. The setting itself is evaluated once per
        process, so a rotated token reaches the next worker restart, not the
        next task. With a token the allowance is still the
        *search* pool's number -- the tighter of GitHub's two authenticated
        pools, for the reason the class-level declaration gives -- and the
        credential reaches the API host only: `credential_host` names it, and
        the base strips the header from `_recipe_facts`' read of the raw host.

        Args:
            clock: The clock every instant in this run is read from.
            transport: The seam `CPM-AD-27` opens, or `None` for the real one.
            limiter: The rate limiter, or `None` for the shared cache-backed one.
            response_cache: The response cache, or `None` for the shared one.

        Raises:
            ImproperlyConfigured: When `CPM_GITHUB_TOKEN` holds a value that can
                never become a header -- refused at boot already, and refused
                again here so nothing that bypassed the hook can send it.
            CollectorConfigurationError: The base's own refusals, unchanged.

        """
        token = github_token()
        # Instance attributes shadowing the two ClassVars; see the same lines in
        # `SourceReleaseCollector.__init__` for why mypy's objection is the point.
        self.headers = authenticated_headers(FEEDSTOCK_HEADERS, token)  # type: ignore[misc]
        self.rate_limit = AUTHENTICATED_SEARCH_ALLOWANCE if token else FEEDSTOCK_RATE_LIMIT  # type: ignore[misc]
        super().__init__(clock=clock, transport=transport, limiter=limiter, response_cache=response_cache)

    #: The locator this run asked for, remembered when `source_for` answered, for
    #: the reason `SourceReleaseCollector._locator` gives: `sentinel_evidence`
    #: records it and is not handed it. Blank on the `not_applicable` path,
    #: because no locator was ever built.
    _locator: str = ""

    #: Which question this run asked, and what it asked about.
    #:
    #: `_branch` is `MAPPED_BRANCH`, `ABSENT_BRANCH`, or blank for a run that
    #: asked neither -- the three states a boolean could not hold, and the reason
    #: `sentinel_evidence` can tell "the feedstock resolution named is gone" from
    #: "the staged-recipes queue could not be read" from "no question was asked".
    #: `_asked` is the name the locators were built from and `_repository` is that
    #: name as a repository segment, which is what a row records when the document
    #: names none -- normalised on **both** branches, so one repository cannot
    #: come to be recorded under two spellings depending on which question found
    #: it. Together they let `translate` and `sentinel_evidence` say which
    #: question was answered without asking identity a second time on a path that
    #: is already failing.
    _branch: str = ""
    _asked: str = ""
    _repository: str = ""

    #: How many feedstocks the mapping held, so a row can say that more than one
    #: was named and only the first was observed.
    _mapped_count: int = 0

    @classmethod
    def selectable_packages(cls) -> Iterable[int]:
        """Return the packages this collector can be asked about: the ones resolution reached.

        The complement of `source_for`'s refusal, expressed as the query that
        refusal is written against. `asks_about` needs a `feedstock` mapping
        recorded `established` -- with rows or without, which are two different
        questions and both askable -- or `not_found`, which is resolution saying
        it looked and there is none; and `inapplicability_of` answers for one
        recorded `not_applicable`. Those three outcomes are the whole of what
        this collector can say anything about. Everything else is unresolved, and
        `CPM-UJ-2` forbids claiming absence for an unresolved identity -- which
        is the refusal `CPM-CURRENCY-S03` recorded as deferred: offered every
        package, this collector would fail every run for every mapping nothing
        has written.

        **`not_found` is a selection and not a skip.** It is the branch that asks
        the staged-recipes queue and confirms the conventional feedstock absent,
        which is `CPM-FR-9` AC 2's whole subject; a selection that offered only
        `established` mappings would make the staged-recipe row unreachable in a
        real deployment.

        **A contradiction is still selected.** A `not_found` mapping carrying
        `Feedstock` rows anyway is refused by `source_for` as an identity that
        contradicts itself, and it stays in the selection so that the
        contradiction surfaces as a `failed` ledger row naming it rather than as
        a package the sweep quietly passed over.

        Returns:
            The primary keys, as a lazy queryset ordered by key -- streamed by
            `collectors/sweep.py` rather than materialised. One row per
            `(package, kind)` means one key per package, so no key repeats.

        """
        return (
            PackageMapping.objects.filter(
                kind=MappingKind.FEEDSTOCK.value,
                outcome__in=(
                    ESTABLISHED,
                    OutcomeState.NOT_FOUND.value,
                    OutcomeState.NOT_APPLICABLE.value,
                ),
            )
            .order_by("package_id")
            .values_list("package_id", flat=True)
        )

    def inapplicability(self, *, package_id: int) -> str:
        """Say whether conda-forge is a question about this package, from what resolution recorded.

        Args:
            package_id: The package being collected, by the integer primary key
                `CPM-AD-3` fixes.

        Returns:
            The reason the question does not apply -- a `feedstock` mapping
            recorded `not_applicable` -- or the empty string when it applies or
            when resolution has not decided, in which case `source_for` refuses.

        """
        # A new run: forget the last one's locator, branch and identity, so none
        # is answered from a package this instance collected before -- or from
        # this package as it was before a resolution changed it.
        self._locator = ""
        self._branch = ""
        self._asked = ""
        self._repository = ""
        self._mapped_count = 0
        self._identity = None
        self._identity_package = None
        identity = self._feedstock_identity(package_id)
        return "" if identity is None else inapplicability_of(identity)

    def source_for(self, *, package_id: int) -> str:
        """Return the locator this collector reads for one package, and remember which question it is.

        **The branch is decided here, before either call is made**, which is what
        keeps every fetch inside `translate`: a package resolution gave a
        feedstock is asked about that feedstock, and a package resolution gave
        none is asked about the staged-recipes queue.

        Args:
            package_id: The package being collected.

        Returns:
            The feedstock repository's locator, or the staged-recipes search's.
            Remembered on the instance as well as returned -- see `_locator`.

        Raises:
            FeedstockLocatorError: When the package has no `feedstock` mapping
                row; when the mapping is `unknown` or `error`; when the mapping
                is `not_found` and carries `Feedstock` rows anyway, which is an
                identity that contradicts itself; or when the name the branch
                would ask about is not a repository segment. See that class for
                why this escapes rather than becoming an evidence row.

        """
        identity = self._feedstock_identity(package_id)
        if identity is None:
            message = (
                f"package {package_id} has no feedstock mapping row, so resolution has recorded nothing about "
                f"whether conda-forge has one. CPM-FR-9 observes the feedstock a resolution established "
                f"(CPM-IDENTITY-S02), and CPM-UJ-2 forbids claiming absence for a package nobody has resolved."
            )
            raise FeedstockLocatorError(message)
        if not asks_about(identity):
            message = (
                f"package {package_id}'s feedstock mapping is {identity.outcome!r}, and this collector asks "
                f"about a package only when resolution has reached the mapping -- {ESTABLISHED!r}, with rows or "
                f"without, or {OutcomeState.NOT_FOUND.value!r}. An unresolved mapping is refused rather than "
                f"recorded as an absence nobody established (CPM-UJ-2); the selection that offers only askable "
                f"packages is CPM-CURRENCY-S05's."
            )
            raise FeedstockLocatorError(message)

        branch = branch_of(identity)
        if not branch:
            message = (
                f"package {package_id}'s feedstock mapping is {identity.outcome!r} and yet carries "
                f"{len(identity.feedstock_names)} feedstock row(s): resolution recorded both that it looked and "
                f"found none and that it found these. An identity row that contradicts itself is refused rather "
                f"than read either way, on the terms CPM-CURRENCY-S02 refuses an established mapping with a "
                f"blank primary type -- observing the rows would record a feedstock resolution says is not "
                f"there, and ignoring them would claim an absence beside rows that deny it (CPM-FR-1)."
            )
            raise FeedstockLocatorError(message)

        self._branch = branch
        self._mapped_count = len(identity.feedstock_names)
        if branch == MAPPED_BRANCH:
            self._asked = identity.feedstock_names[0]
            self._repository = feedstock_repository(self._asked)
            self._locator = repository_locator(self._asked)
        else:
            self._asked = identity.canonical_name
            # Deliberately not normalised to a repository here: the absent
            # branch's first call names the *package*, and the repository is
            # named only if the bounded second call finds one -- where a width
            # this row would never fill must not refuse the question.
            self._locator = staged_recipes_locator(self._asked)
        return self._locator

    def translate(self, payload: Payload, *, package_id: int, observed_at: datetime) -> Sequence[AppendOnlyModel]:
        """Turn one answer, plus this branch's bounded second call, into the one row it is worth.

        One row and never none: the base reads an empty translation as a parser
        that no longer matches its source.

        Args:
            payload: What the source said, recorded. Reached only for a call the
                source answered -- absence and failure are the base's to record.
            package_id: The package the observation is about.
            observed_at: The instant to stamp the row with, from the injected
                clock. The base refuses a row stamped with anything else.

        Returns:
            One unsaved `FeedstockSnapshot`.

        Raises:
            FeedstockDocumentError: When the document cannot be read as what the
                branch asked for. The base writes an `error` row and re-raises.
            FeedstockLocatorError: When the recipe's locator cannot be built for
                a name the repository locator was already built from --
                unreachable in practice, and raised rather than swallowed. The
                *other* branch's second call cannot raise it: see
                `_conventional_instead`.

        """
        row = self._observed(payload) if self._branch == MAPPED_BRANCH else self._absent(payload)
        return [
            FeedstockSnapshot(
                observed_at=observed_at,
                package_id=package_id,
                trace_id=current_trace_id(),
                **row,
            ),
        ]

    def _observed(self, payload: Payload) -> dict[str, object]:
        """Read the feedstock resolution named, and then its recipe.

        The mapped branch. The repository's answer is what `state` rests on: the
        feedstock exists, which is the fact `CPM-FR-9` asks for first. The recipe
        is the bounded second call, and a failure of it leaves the version blank
        with the reason in `detail` rather than failing a collection that has
        already established the more important fact.

        Args:
            payload: What the repository said.

        Returns:
            The row's fields, ready to be stamped.

        Raises:
            FeedstockDocumentError: When the repository document cannot be read.
            FeedstockLocatorError: When the recipe's locator cannot be built.

        """
        repository = repository_facts(payload.body, source=payload.source, fallback_name=self._repository)
        recipe = self._recipe_instead(name=self._asked)
        reasons = [repository.detail, recipe.detail]
        if self._mapped_count > 1:
            reasons.append(
                f"resolution named {self._mapped_count} feedstocks for this package and this is the first by "
                f"name; the others were not observed by this run",
            )
        return {
            "source": payload.source,
            "state": OutcomeState.OK.value,
            "feedstock_name": repository.name,
            "feedstock_url": repository.url,
            "recipe_version": recipe.version,
            "recipe_build_number": recipe.build_number,
            "recipe_metadata_url": recipe.metadata_url,
            "last_recipe_activity_at": repository.pushed_at,
            "staged_recipe_url": "",
            "detail": _reasons(reasons),
        }

    def _absent(self, payload: Payload) -> dict[str, object]:
        """Read the staged-recipes queue, and then confirm the conventional feedstock's absence.

        The branch for a package resolution established no feedstock for. The
        search is the always-succeeding call, which is why it is first; the
        conventional repository is the bounded second one, and it is what turns
        "resolution established none" into an observation rather than a
        restatement of identity.

        **A conventional feedstock that answers makes this an `ok` row**, and the
        staged recipe is then dropped rather than recorded beside it: AC 2 keeps
        the two apart, and `staged_recipe_only_when_absent` is the database
        saying so. What the queue held is not lost -- `detail` records it.

        Args:
            payload: What the staged-recipes search said.

        Returns:
            The row's fields, ready to be stamped.

        Raises:
            FeedstockDocumentError: When the search document cannot be read.
            FeedstockLocatorError: When the conventional repository's locator
                cannot be built.

        """
        staged = staged_recipe(payload.body, name=self._asked, source=payload.source)
        queue = _queue_detail(staged)
        conventional = self._conventional_instead(name=self._asked)
        if conventional.facts is not None:
            return {
                # The locator the facts came from, not the one the base fetched.
                # Every fact on this row was read from the conventional
                # repository, and `SourceReleaseSnapshot` sets the precedent: a
                # tag-fallback row names the tags locator, because a `source`
                # column naming an endpoint none of the row's facts came from is
                # a provenance claim that is simply false.
                "source": conventional.source,
                "state": OutcomeState.OK.value,
                "feedstock_name": conventional.facts.name,
                "feedstock_url": conventional.facts.url,
                "recipe_version": "",
                "recipe_build_number": None,
                "recipe_metadata_url": "",
                "last_recipe_activity_at": conventional.facts.pushed_at,
                "staged_recipe_url": "",
                "detail": _reasons(
                    [
                        f"{UNESTABLISHED_FEEDSTOCK_DETAIL}; the recipe was not read on this branch",
                        conventional.detail,
                        queue,
                    ],
                ),
            }
        return {
            "source": payload.source,
            "state": OutcomeState.NOT_FOUND.value,
            "feedstock_name": "",
            "feedstock_url": "",
            "recipe_version": "",
            "recipe_build_number": None,
            "recipe_metadata_url": "",
            "last_recipe_activity_at": None,
            "staged_recipe_url": staged.url,
            # Both halves, structurally, and both are needed. The repository must
            # have *answered* that it is not there, and the queue must have
            # answered conclusively -- one match recorded, or none out of a page
            # that held them all. A blank URL from an overfull page, or from a
            # queue holding two candidates, establishes nothing about whether a
            # recipe is already on its way, and `CPM-CURRENCY-S07`'s policy pass
            # reports the difference as work somebody should go and do.
            "absence_established": conventional.established and _queue_answered(staged),
            # `conventional.detail` rather than a fixed sentence: it says
            # "the repository is absent" only when the repository said so, and
            # says "nobody could look" otherwise. A row that claimed the first
            # where the second was true would put an absence this run never
            # established into a log nothing may correct.
            "detail": _reasons([conventional.detail, queue]),
        }

    def _recipe_instead(self, *, name: str) -> RecipeFacts:
        """Read a feedstock's recipe, for a feedstock this run has already found.

        **One of this collector's two bounded second calls**, and it is bounded
        three ways: it is reached only from the mapped branch, it is one file, and
        a failure of it never fails the collection -- the repository's answer
        stands, with the reason in `detail`, because "this feedstock exists" is a
        fact this run did establish.

        The base's allowance was charged once, before the first call, so this
        request is not counted against it -- recorded as deferred on the terms
        `collectors/source_release.py` records the same gap.

        Args:
            name: The feedstock the recipe belongs to.

        Returns:
            The recipe's facts, or a blank version carrying the reason it could
            not be read.

        Raises:
            FeedstockLocatorError: When the recipe's locator cannot be built from
                a name the repository locator was already built from.

        """
        locator = recipe_locator(name)
        # The raw host: the base's `headers_for` strips the credential, because
        # this host neither needs it nor counts against the allowance it buys.
        sent = request_headers(declared=self.headers_for(locator), entry=None)
        try:
            payload = self._transport.fetch(locator, headers=sent)
        except TransportError as failure:
            detail = failure_detail(failure, credentialed=carries_credential(sent))
            return RecipeFacts(
                version="",
                build_number=None,
                metadata_url="",
                detail=f"{UNREADABLE_RECIPE_DETAIL}: {locator} could not be read: {detail}",
            )
        if not payload.found:
            return RecipeFacts(
                version="",
                build_number=None,
                metadata_url="",
                detail=f"{UNREADABLE_RECIPE_DETAIL}: {locator} reports that there is no such recipe",
            )
        if payload.not_modified:
            # This request carried no validator, so a `304` is the source
            # answering a question nobody asked and there is no body behind it.
            # Left to fall through it would read as a recipe that sets no
            # version, which is a reason describing the wrong problem.
            return RecipeFacts(
                version="",
                build_number=None,
                metadata_url="",
                detail=(
                    f"{UNREADABLE_RECIPE_DETAIL}: {locator} answered that nothing had changed, to an "
                    f"unconditional request"
                ),
            )
        return recipe_facts(payload.body, source=locator)

    def _conventional_instead(self, *, name: str) -> ConventionalAnswer:
        """Ask whether the conventional feedstock exists, for a package resolution gave none.

        **The other bounded second call**, bounded on the same three terms. Its
        answer is the difference between "identity says there is none" and "there
        is none": `CPM-FR-9` asks for feedstock *existence*, and a row that
        merely restated the mapping would be recording identity as evidence.

        **"Asked and it is not there" and "could not ask" are different answers,
        and this returns both.** Only the first is evidence of absence; the second
        is the run failing to find out, and a row that recorded it as absence
        would be claiming a fact nobody established, permanently. So every way of
        not finding out -- a transport failure, a `304` to a request that carried
        no validator, a document whose shape has changed, and a canonical name
        that cannot be turned into a repository at all -- comes back as facts of
        `None` with a reason that begins `UNCHECKED_FEEDSTOCK_DETAIL`.

        **Nothing here raises**, and that is the invariant rather than an
        omission: this call is made after the search has already established what
        resolution recorded, and an exception escaping it would turn an answer
        the run *did* have into an `error` row and a `failed` run.

        Args:
            name: The package's canonical name, which the conventional
                conda-forge repository is derived from.

        Returns:
            The answer: the repository's facts when it answered readably, and
            otherwise `None` facts with the reason -- either that the repository
            is absent, or that nobody could look.

        """
        try:
            locator = repository_locator(name)
        except FeedstockLocatorError as unnameable:
            return ConventionalAnswer(
                facts=None,
                source="",
                detail=f"{UNCHECKED_FEEDSTOCK_DETAIL}: {unnameable}",
            )
        # The API host: the base's `headers_for` keeps the credential for this
        # locator, and a `401` to it is worded as a refused credential.
        sent = request_headers(declared=self.headers_for(locator), entry=None)
        try:
            payload = self._transport.fetch(locator, headers=sent)
        except TransportError as failure:
            detail = failure_detail(failure, credentialed=carries_credential(sent))
            return ConventionalAnswer(
                facts=None,
                source=locator,
                detail=f"{UNCHECKED_FEEDSTOCK_DETAIL}: {locator} could not be read: {detail}",
            )
        if payload.not_modified:
            # This request carried no validator, so a `304` is the source
            # answering a question nobody asked and there is no body behind it.
            # Left to fall through it would read as a repository document that is
            # not JSON, which is a refusal describing the wrong problem -- and
            # would be read as an absence, which is the wrong answer.
            return ConventionalAnswer(
                facts=None,
                source=locator,
                detail=(
                    f"{UNCHECKED_FEEDSTOCK_DETAIL}: {locator} answered that nothing had changed, to an "
                    f"unconditional request"
                ),
            )
        if not payload.found:
            # The one path that *establishes* an absence: conda-forge answered
            # that the conventional repository is not there. Every other return
            # in this method leaves `established` at its default, which is what
            # keeps "could not ask" from being read as "asked, and it is not
            # there" by anything downstream.
            return ConventionalAnswer(
                facts=None,
                source=locator,
                detail=ABSENT_FEEDSTOCK_DETAIL,
                established=True,
            )
        try:
            facts = repository_facts(payload.body, source=locator, fallback_name=feedstock_repository(name))
        except FeedstockDocumentError as unreadable:
            # Inside the `try` deliberately. Raised, it would leave `translate`,
            # and the base would write an `error` row over an absence the search
            # had already established -- exactly what this method exists not to
            # do.
            return ConventionalAnswer(
                facts=None,
                source=locator,
                detail=f"{UNCHECKED_FEEDSTOCK_DETAIL}: {unreadable}",
            )
        return ConventionalAnswer(facts=facts, source=locator, detail=facts.detail)

    def sentinel_evidence(
        self,
        *,
        state: OutcomeState,
        package_id: int,
        observed_at: datetime,
        detail: str,
    ) -> AppendOnlyModel:
        """Return one row carrying the sentinel the base decided on.

        Every observed fact is absent: a sentinel row is written for a call that
        produced no document, or for a question that was never asked. The
        staged-recipe column is blank too, because the search that would have
        filled it is the *first* call on the branch that makes it -- so a
        sentinel row is one where it was never asked.

        A `not_found` row on the mapped branch carries the fact that makes it
        interesting: the feedstock resolution named is the one that is absent,
        which is a statement about identity as much as about conda-forge.

        Args:
            state: `OutcomeState.ERROR`, `OutcomeState.NOT_FOUND` or
                `OutcomeState.NOT_APPLICABLE`, decided by the base.
            package_id: The package the observation is about.
            observed_at: The instant to stamp the row with.
            detail: What happened, in words worth storing beside the state.

        Returns:
            One unsaved `FeedstockSnapshot` carrying the state's value verbatim
            in `state` (`CPM-AD-24`).

        Raises:
            CollectorConfigurationError: When asked for a state this collector has
                no row shape for -- `ok` or `unknown`. The alternative failure is
                worse than a refusal: a row carrying `ok` and no feedstock name is
                refused by the table's own constraint at insert, several frames
                from the call that was wrong.

        """
        # Written as three comparisons rather than as membership of a declared
        # set, for the reason `SourceReleaseCollector.sentinel_evidence` gives:
        # `tests/unit/django_apps/test_single_ordering_audit.py` reads a literal
        # holding two `OutcomeState` members outside `core/outcomes.py` as a
        # second precedence order, and it is right to.
        if (
            state is not OutcomeState.ERROR
            and state is not OutcomeState.NOT_FOUND
            and state is not OutcomeState.NOT_APPLICABLE
        ):
            message = (
                f"{type(self).__name__}.sentinel_evidence was asked for {state.value!r}, and this collector "
                f"shapes a sentinel row for {OutcomeState.ERROR.value!r}, {OutcomeState.NOT_FOUND.value!r} and "
                f"{OutcomeState.NOT_APPLICABLE.value!r} only. A row carrying any other state would name no "
                f"feedstock and would be refused by feedstock_snapshots' own constraint at insert."
            )
            raise CollectorConfigurationError(message)
        return FeedstockSnapshot(
            observed_at=observed_at,
            package_id=package_id,
            source=self._locator,
            state=state.value,
            feedstock_name="",
            feedstock_url="",
            recipe_version="",
            recipe_build_number=None,
            recipe_metadata_url="",
            last_recipe_activity_at=None,
            staged_recipe_url="",
            detail=self._sentinel_detail(state, detail),
            trace_id=current_trace_id(),
        )

    def _sentinel_detail(self, state: OutcomeState, detail: str) -> str:
        """Return what a sentinel row records beside the base's own reason.

        Args:
            state: The sentinel the base decided on.
            detail: The base's reason.

        Returns:
            The reason, with this branch's `not_found` caveat appended.

            The base's own sentence says the locator reports the resource does
            not exist, and what that means about *the package* is different on
            each branch and recoverable from neither the state nor the locator.
            On the mapped branch the locator was the feedstock repository, so the
            feedstock resolution named is the thing that is absent. On the absent
            branch it was the staged-recipes **search endpoint**, so what failed
            is the queue read -- no feedstock repository was checked at all, and a
            row left carrying only the base's sentence would read as an absence of
            a feedstock nobody looked for. A run that asked neither question gets
            the base's sentence unchanged, because there is nothing to add.

        """
        if state is not OutcomeState.NOT_FOUND:
            return detail
        if self._branch == MAPPED_BRANCH:
            return (
                f"{detail} -- the feedstock resolution established for this package, {self._repository!r}, is "
                f"absent from conda-forge; the staged-recipes queue was not searched, because resolution had "
                f"already named a feedstock"
            )
        if self._branch == ABSENT_BRANCH:
            return f"{detail} -- {UNCHECKED_QUEUE_DETAIL}"
        return detail

    def _feedstock_identity(self, package_id: int) -> FeedstockIdentity | None:
        """Return what resolution recorded about this package's conda-forge feedstocks.

        The only database reads in this module, and they read `identity` -- the
        only application a collector may read (`CPM-AD-7`). **Two queries, and
        that is the mapping's own shape rather than a missed join**:
        `MAPPED_FIELDS[FEEDSTOCK]` is empty because the mapping *is* the child
        rows, so the outcome and the rows it answers for live in two tables and
        an `established` mapping with none is a successful empty result rather
        than a missing row. Joining them would return one row per feedstock and
        no row at all for the empty result, which is precisely the case this
        collector has to tell apart.

        Args:
            package_id: The package being collected.

        Returns:
            The identity, or `None` when no `feedstock` mapping row exists for
            the package -- which is also what a package with no identity row at
            all produces, and the two are refused together.

        """
        if self._identity_package != package_id:
            recorded = (
                PackageMapping.objects.filter(package_id=package_id, kind=MappingKind.FEEDSTOCK.value)
                .values_list("outcome", "package__canonical_name")
                .first()
            )
            if recorded is None:
                self._identity = None
            else:
                outcome, canonical_name = recorded
                names = Feedstock.objects.filter(package_id=package_id).order_by("name").values_list("name", flat=True)
                self._identity = FeedstockIdentity(
                    outcome=outcome,
                    canonical_name=canonical_name,
                    feedstock_names=tuple(names),
                )
            self._identity_package = package_id
        return self._identity


def _reasons(parts: Sequence[str]) -> str:
    """Join the reasons a row carries into one sentence, dropping the empty ones.

    Args:
        parts: The reasons, in the order a reader would want them, any of which
            may be empty.

    Returns:
        The reasons separated by `; `, or the empty string when there are none --
        which is what an ordinary determinate observation carries.

    """
    return "; ".join(part for part in parts if part)


def _queue_answered(staged: StagedRecipe) -> bool:
    """Report whether the staged-recipes search settled what the queue holds.

    The structural half of `_queue_detail` below, which says the same thing in
    prose. Both are here rather than one derived from the other because a
    sentence is for a person and a boolean is for a policy pass, and reading the
    second out of the first is the string matching this pair exists to remove.

    Args:
        staged: What the search matched.

    Returns:
        True when exactly one open pull request was recorded, or when none
        matched on a page that held every result. False when more than one
        matched -- so none was recorded and which one would create the feedstock
        is not this collector's to pick -- and False when the search overflowed
        its page, where a blank match may simply be on a page nobody asked for.

    """
    if staged.url:
        return True
    return staged.matched == 0 and not staged.truncated


def _queue_detail(staged: StagedRecipe) -> str:
    """Return what the staged-recipes search found, in words worth storing.

    Args:
        staged: What the search matched.

    Returns:
        The sentence for none, for exactly one, for more than one, and for a
        queue that overflowed the one page read -- because a blank URL means four
        different things and only the row's `detail` can say which.

        The one-match sentence names the pull request as well as the fact, which
        is redundant beside the `staged_recipe_url` column on a `not_found` row
        and is the *only* record of it on an `ok` row, where the column may not
        carry it. The overflow sentence is checked before the empty one and after
        the matches: a match found on the page read is a match however many
        results lay behind it, but a page that found none out of more results
        than it held has established nothing about absence.

    """
    if staged.url:
        return f"one open staged-recipes pull request names this package: {staged.url}"
    if staged.matched > 1:
        return f"{AMBIGUOUS_STAGED_RECIPE_DETAIL} ({staged.matched} matched)"
    if staged.truncated:
        return f"{OVERFULL_QUEUE_DETAIL} ({staged.total} results, of which {SEARCH_RESULTS_PER_PAGE} were read)"
    return NO_STAGED_RECIPE_DETAIL
