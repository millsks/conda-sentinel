"""Where a package's own source says its latest release is, observed and recorded.

`CPM-FR-7`: obtain "latest release or tag, its date, and a repository activity
signal from the package's source repository", record the lookup status
explicitly, and record a repository that publishes no releases as *that fact*
rather than as staleness. This module is that collector.

**It is the first collector on the per-package path, and that is why it is
small.** `CPM-EVIDENCE-S05` put every external-call rule in one base -- the
timeout, the retry policy, the rate limit, the conditional request, the run
ledger, the per-package transaction, the sentinel row on every failing path -- and
`CPM-AD-27` put the transport boundary there so that a collector is "a pure
translation from a recorded payload to evidence rows". Inventory ingestion
(`CPM-IDENTITY-S06`) reads one document naming many packages and refuses all three
per-package hooks; this is the first class to implement them.

**"Or tag" is the half of AC 1 that decides the shape.** Publishing a GitHub
Release is a deliberate act many projects never perform: they tag, and the
release feed is empty for ever. Reading releases alone would record `not_found`
for every one of them -- a false fact, in a log nothing may correct -- and would
also collapse AC 2 into a restatement of AC 1's absence case. So an empty release
list falls back to the repository's tags, and only a repository with **neither**
records `not_found`. That is what makes AC 2 a narrower, genuinely distinct case:
"publishes no releases at all" is now a statement about a repository that also
has nothing tagged.

**The fallback costs a second call, and it is the one place this collector
departs from one-collection-one-call.** It fires only on an empty release list,
so a repository that publishes releases never pays for it, and the base's
allowance is charged once per collection rather than twice -- which is recorded as
deferred rather than hidden here. A tag carries **no date**: the tags endpoint
supplies none, and GitHub's ordering of that list is the source's rather than
this collector's. So a tagged row records the version and leaves `released_at`
NULL, its `detail` says the ordering is the source's, and its `source` column
names the tags locator -- which is what lets a reader tell a tagged row from a
released one without a second status vocabulary.

**Two of the three hooks reduce to module functions, and that is deliberate.**
`releases_locator`, `tags_locator`, `release_facts` and `tag_facts` take data and
return data. They are the whole of this story's behaviour and they are reachable
with no database, no socket and no clock, which is what `CPM-AD-27` is for.

**GitHub, declared rather than assumed.** `Package.source_repository_url` is
whatever URL a resolution established, and turning one into an API locator is
host-specific work: there is no cross-host "latest release" endpoint. This story
ships the one host conda-forge's sources overwhelmingly live on, and refuses the
rest by name rather than guessing at a URL shape.

**A `404` is not proof of absence to an unauthenticated reader, and the row says
so.** `core/transport.py` maps `404` and `410` to "the source says this does not
exist", which is right in general and is *ambiguous* against GitHub in
particular: it answers `404` identically for a repository that is absent, one
that is private, one that has moved, and one that is blocked -- deliberately, so
that an unauthenticated reader cannot enumerate private repositories. Without
`CPM_GITHUB_TOKEN` this collector sends no credential, so it cannot tell those
apart, and the honest response is not to invent a state it cannot justify: the
row still carries `not_found`, and its `detail` records that a `404` may mean
either. With a token (`CPM-OPERATE-S05`) the same row is written -- the
credential buys the allowance, and a `404` still says nothing about *why* -- and
it is the credential's own reach, not this module, that decides which private
repositories stop answering `404`.

**The credential, when there is one, is one setting and one header.**
`collectors/github.py` reads `CPM_GITHUB_TOKEN`, and `__init__` sets this
instance's `headers` and `rate_limit` from it before the base validates
either: `Authorization: Bearer` to `api.github.com`, and GitHub's authenticated
core allowance in place of the unauthenticated one the class declares. The
token reaches no log line, ledger row, evidence row or `detail`; a credential
GitHub refuses is a `failed` run whose `detail` names the host and nothing
else, worded by the base.

**The `error` and `not_found` rows the base writes go through
`sentinel_evidence`, and this module invents neither.** The base decides which
sentinel and that there is always one (`CPM-NFR-3`: never a clean result and
never no row); this module decides what a row in `source_release_snapshots` looks
like, and refuses a state it has no row shape for. It checks the result too -- a
subclass that ignored the state and wrote a determinate verdict would type-check
perfectly -- which is why the state's value is carried verbatim in `state`
(`CPM-AD-24`).

**Which failure a row records is `detail`'s job, and the base fixes the wording.**
Five terminal paths reach a sentinel row and each declares its own `detail`,
which is the same string the ledger row carries: a spent allowance says so and
names the allowance (`collection.refused_by_rate_limit`), a transport failure
carries the exception's type and message, a translation that raised carries its
own, and the two above carry the sentences named in this module. An operator
separating "we never got to look" from "the source is failing" reads that column
rather than inferring from the state, which is deliberate -- `OutcomeState` says
*what may be claimed about the package*, not *why the run went the way it did*.

**On the `AD-` prefix.** A bare `AD-n` in this repository is an *inherited*
platform decision; a decision from this product's own architecture spine always
carries the `CPM-` prefix.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from dataclasses import replace
from datetime import datetime
from datetime import timedelta
from types import MappingProxyType
from typing import TYPE_CHECKING
from typing import ClassVar
from typing import Final
from urllib.parse import quote
from urllib.parse import urlsplit

# The User-Agent identity and the worst-case arithmetic were written here first
# and moved to shared homes when the second collector arrived: `CPM-AD-7` says no
# collector imports another, so the pieces two of them need live in neither.
# They stay in this module's `__all__` so every existing importer is untouched.
from conda_sentinel.collectors.agent import DISTRIBUTION_NAME
from conda_sentinel.collectors.agent import PROJECT_URL
from conda_sentinel.collectors.agent import UNKNOWN_VERSION
from conda_sentinel.collectors.agent import USER_AGENT
from conda_sentinel.collectors.agent import distribution_version
from conda_sentinel.collectors.github import AUTHENTICATED_CORE_ALLOWANCE
from conda_sentinel.collectors.github import GITHUB_API_HOST
from conda_sentinel.collectors.github import authenticated_headers
from conda_sentinel.collectors.github import github_token
from conda_sentinel.collectors.models import SourceReleaseSnapshot
from conda_sentinel.core.clock import is_aware
from conda_sentinel.core.collection import Collector
from conda_sentinel.core.collection import CollectorConfigurationError
from conda_sentinel.core.collection import failure_detail
from conda_sentinel.core.collection import request_headers
from conda_sentinel.core.credentials import carries_credential
from conda_sentinel.core.ledger import current_trace_id
from conda_sentinel.core.outcomes import OutcomeState
from conda_sentinel.core.rate_limit import RateLimit
from conda_sentinel.core.transport import ALLOWED_SCHEMES
from conda_sentinel.core.transport import DEFAULT_RETRIES
from conda_sentinel.core.transport import TransportError
from conda_sentinel.core.transport import worst_case_call_seconds
from conda_sentinel.identity.models import Package

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
    "ABSENT_CAVEAT",
    "ACTIVITY_FIELDS",
    "COLLECTOR_NAME",
    "DISTRIBUTION_NAME",
    "DRAFT_FIELD",
    "GITHUB_API_HOST",
    "GITHUB_WEB_HOSTS",
    "MAX_DOCUMENT_CHARACTERS",
    "NO_LATEST_RELEASE_DETAIL",
    "NO_RELEASES_DETAIL",
    "NO_TAGS_DETAIL",
    "PRERELEASE_FIELD",
    "PROJECT_URL",
    "PUBLISHED_AT_FIELD",
    "RELEASES_PER_PAGE",
    "SOURCE_RELEASE_CACHE_TTL",
    "SOURCE_RELEASE_CADENCE",
    "SOURCE_RELEASE_FRESHNESS_TARGET",
    "SOURCE_RELEASE_HEADERS",
    "SOURCE_RELEASE_OBSERVATION_WINDOW",
    "SOURCE_RELEASE_RATE_LIMIT",
    "SOURCE_RELEASE_RETRIES",
    "SOURCE_RELEASE_TIMEOUT",
    "TAGGED_DETAIL",
    "TAGS_PER_PAGE",
    "TAG_NAME_FIELD",
    "TOLERATED_MISSED_RUNS",
    "UNDATED_RELEASE",
    "UNKNOWN_VERSION",
    "UNPUBLISHED_RELEASE",
    "UNTAGGED_RELEASE",
    "USER_AGENT",
    "ReleaseFacts",
    "SourceLocatorError",
    "SourceReleaseCollector",
    "SourceReleaseDocumentError",
    "distribution_version",
    "release_facts",
    "releases_locator",
    "tag_facts",
    "tags_locator",
    "worst_case_call_seconds",
]

#: What this collector is called, on its ledger rows, in its cache keys and in
#: the registry `config/startup/stage_two.py` sweeps. It is the `collect` half of
#: its task name too, which is what routes it (`core/queues.py`).
COLLECTOR_NAME: Final[str] = "source_release"

#: How often this collector is meant to run, and the number every other interval
#: below is derived from.
#:
#: `CPM-NFR-2` gives version currency "daily to weekly"; this is the fast end,
#: which is the safe end to derive from -- a target derived from a weekly cadence
#: and applied to a daily one would call fresh evidence stale a week late rather
#: than a day late. The cadence itself is **not** declared here: `CPM-AD-20` makes
#: it data in `django_celery_beat`'s scheduler, and this constant is the number the
#: arithmetic below assumes rather than the number that schedules anything. The
#: two are reconciled at start-up by `collectors/apps.py`'s `ready()`, which
#: `CPM-CURRENCY-S05` added: the collector declares this number as `cadence`, the
#: schedule entry declares its own, and a component whose two disagree refuses to
#: start rather than reading a whole inventory as stale with every gate green.
#: `config/startup/stage_two.py` evaluates the same rule as condition 11, but it
#: runs before this application registers anything, so the application's own hook
#: is where a deployed process meets the refusal.
SOURCE_RELEASE_CADENCE: Final[timedelta] = timedelta(days=1)

#: How many consecutive missed collections may pass before this product stops
#: calling an answer current. One, which is PRD Open Question 7's risk posture for
#: the version-currency signal class.
TOLERATED_MISSED_RUNS: Final[int] = 1

#: How long this collector's evidence may be read as current (`CPM-AD-28`).
#:
#: Derived, not picked. PRD Open Question 7 was resolved on 2026-09-05 and fixes
#: the rule:
#:
#:     freshness_target = cadence x (1 + tolerated_missed_runs) + one sweep duration
#:
#: which gives two days here. **The target is strictly greater than the cadence,
#: and that is the rule rather than the arithmetic**: `core/freshness.py` reports
#: stale when `observed_at < now - target`, so a target equal to the cadence makes
#: every package read stale at exactly the moment its next run is due.
#:
#: What is still owed is Open Question 7b's measurement -- a target must also
#: exceed one sweep's wall-clock duration, and no sweep has run at `CPM-NFR-1`'s
#: ten thousand packages. This value assumes a sweep finishes well inside its
#: cadence, and `CPM-EP-CURRENCY` is where that is confirmed.
SOURCE_RELEASE_FRESHNESS_TARGET: Final[timedelta] = SOURCE_RELEASE_CADENCE * (1 + TOLERATED_MISSED_RUNS)

#: How long a successful observation suppresses the next one (`CPM-AD-7`).
#:
#: Half the cadence, and the halving is the whole decision. `CPM-AD-7` says the
#: freshness target is a window's *default*, and taking that literally here would
#: be wrong in a way that is easy to miss: with a window equal to a target of two
#: cadences, the run scheduled one cadence later is suppressed, the run after that
#: is suppressed by the inclusive boundary, and the collector never observes
#: anything again. A window shorter than the cadence cannot suppress a scheduled
#: run at all, and still suppresses the thing the window is actually for -- a
#: second run of the same package inside the same day, which spends a rate limit
#: re-asking a question that has been answered.
#:
#: `CPM-UJ-1`'s manually triggered recollection bypasses this entirely, through the
#: base's `force`.
SOURCE_RELEASE_OBSERVATION_WINDOW: Final[timedelta] = SOURCE_RELEASE_CADENCE / 2

#: How many times a failed request is retried. The transport's own default, which
#: is also the number the rate limiter is charged against per collection.
SOURCE_RELEASE_RETRIES: Final[int] = DEFAULT_RETRIES

#: Seconds any single connect or read phase may take.
#:
#: Five, and it is bounded from above by the inherited Celery limits rather than
#: chosen for comfort. `core/transport.py` states what the value does and does not
#: bound: it is spent per connect *and* per read *and* again per attempt, so one
#: collection's worst case is `(1 + retries) x 2 x timeout` plus the backoff
#: schedule -- `core/transport.py`'s `worst_case_call_seconds()` computes it. At five seconds that
#: is 43, which fits inside the inherited 60-second soft limit (`CPM-AD-9`) with
#: room for the ledger writes around it; at eight it would not.
#: `tests/unit/django_apps/test_source_release.py` reconciles the arithmetic
#: against the settings module's own declared limit rather than against a number
#: repeated here.
SOURCE_RELEASE_TIMEOUT: Final[float] = 5.0

#: How hard this collector may push its source **with no credential declared**
#: (`CPM-AD-20`).
#:
#: Sixty requests an hour, which is GitHub's documented allowance for
#: unauthenticated requests and therefore the honest declaration for a collector
#: that sends no credential. The base charges `1 + retries` per collection, so
#: this is fifteen packages an hour -- which is **not** enough to sweep
#: `CPM-NFR-1`'s ten thousand packages inside their two-day target, and is
#: recorded as such rather than inflated to a number nobody measured.
#:
#: This is the class's declaration and it stays the unauthenticated number,
#: because it is what every audit and every declaration test reads. With
#: `CPM_GITHUB_TOKEN` set (`CPM-OPERATE-S05`) the *instance* declares
#: `collectors/github.py`'s `AUTHENTICATED_CORE_ALLOWANCE` instead -- five
#: thousand an hour, 1,250 collections an hour, a ten-thousand-package inventory
#: in about eight hours -- and sends the credential to the API host. The number
#: and the credential are one decision, made once in `__init__`.
SOURCE_RELEASE_RATE_LIMIT: Final[RateLimit] = RateLimit(calls=60, per=timedelta(hours=1))

#: What this collector's source expects on every request (`CPM-AD-20`,
#: `CPM-AD-27`): declared here, merged and sent by the base, never by this module.
#:
#: `Accept` selects the versioned JSON representation and `X-GitHub-Api-Version`
#: pins the API version, so a future default at the source cannot silently change
#: the shape `release_facts` reads. The `User-Agent` is the one identity every
#: collector shares (`collectors/agent.py`): GitHub requires one and asks that it
#: identify the caller. Nothing conditional is declared -- `If-None-Match` and
#: `If-Modified-Since` are the base's, composed from what the response cache
#: holds, and a collector declaring one is refused at construction. Nothing
#: *credential* is declared here either: `Authorization` is added per instance
#: by `collectors/github.py`'s `authenticated_headers` when `CPM_GITHUB_TOKEN`
#: is set, so this mapping is exactly what an unauthenticated request carries.
SOURCE_RELEASE_HEADERS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": USER_AGENT,
    },
)

#: How long a remembered response may be replayed before it is re-read.
#:
#: A week, so a run of scheduled collections revalidates rather than re-transfers:
#: an entry that expired inside the cadence would make every run a full read and
#: the cache inert. GitHub serves an `ETag` on this endpoint, which is what makes
#: a `304` reachable at all. Note what this does and does not save -- the base
#: charges the allowance before the call, conditional or not, so the saving is
#: bandwidth rather than rate limit (`CPM-EVIDENCE-S08` records that as deferred).
SOURCE_RELEASE_CACHE_TTL: Final[timedelta] = timedelta(days=7)

#: The API host this collector reads, and the web hosts it recognises a repository
#: URL by. Separate constants because they are separate facts: one is where the
#: question is asked and the other is what a resolution may have stored. The API
#: host is `collectors/github.py`'s spelling, re-exported here so every existing
#: importer is untouched: it is the one host the credential is sent to, and the
#: comparison that decides that has to read the same string this module builds
#: its locators from.
GITHUB_WEB_HOSTS: Final[frozenset[str]] = frozenset({"github.com", "www.github.com"})

#: How many path segments a repository URL's path must have: the owner and the
#: repository, and nothing else. A deeper path is somewhere *inside* a repository
#: -- a tree, a blob, an issue -- and is not a repository identity.
_REPOSITORY_SEGMENTS: Final[int] = 2

#: The suffix a clone URL carries and a repository name does not.
_GIT_SUFFIX: Final[str] = ".git"

#: The path segments that are a navigation instruction rather than a name.
#: Refused rather than encoded -- both are unreserved characters, so
#: percent-encoding leaves them untouched -- and refused **after** the `.git`
#: suffix is stripped, because `..git` is a segment that becomes one of these.
_TRAVERSAL_SEGMENTS: Final[frozenset[str]] = frozenset({".", ".."})

#: How many releases and how many tags one page asks for.
#:
#: One page is all a collection reads, because one collection is one call. The
#: bound is worth stating: a repository whose thirty most recent releases are
#: every one of them a draft or a prerelease records no latest release while
#: recording its activity, which is a true statement about what this run could see
#: rather than a guess about what lies past the page.
RELEASES_PER_PAGE: Final[int] = 30
TAGS_PER_PAGE: Final[int] = 30

#: The longest document this collector will decode, in characters.
#:
#: A response body arrives as a decoded string before anything here sees it, and
#: `json.loads` over a very large or very deeply nested one is both memory and CPU
#: this worker does not have: the inherited soft limit is sixty seconds
#: (`CPM-AD-9`) and a Celery worker holds several collections at once. Four
#: mebibytes is two orders of magnitude above a thirty-entry release page and well
#: below anything that could hurt, which is what a ceiling is for -- it is not a
#: guess at the document's size, it is a refusal to be surprised by one.
MAX_DOCUMENT_CHARACTERS: Final[int] = 4 * 1024 * 1024

#: The fields of a release this collector reads, named rather than spelled at the
#: call sites so the reader and the cases that build documents cannot drift.
#:
#: `ACTIVITY_FIELDS` holds both date fields rather than a preference order: the
#: activity signal is the *most recent* instant an entry carries, and an entry
#: whose `created_at` is later than its `published_at` -- a release edited after
#: publication, a tag moved -- would otherwise understate a live project.
TAG_NAME_FIELD: Final[str] = "tag_name"
PUBLISHED_AT_FIELD: Final[str] = "published_at"
CREATED_AT_FIELD: Final[str] = "created_at"
DRAFT_FIELD: Final[str] = "draft"
PRERELEASE_FIELD: Final[str] = "prerelease"
ACTIVITY_FIELDS: Final[tuple[str, ...]] = (PUBLISHED_AT_FIELD, CREATED_AT_FIELD)

#: The field a *tag* carries its name in. A different document with a different
#: shape, which is the whole reason it has its own reader.
TAG_FIELD: Final[str] = "name"

#: The two flags that decide whether an entry is a *release* rather than a
#: rehearsal of one. Read together because they are refused together.
_FLAG_FIELDS: Final[tuple[str, ...]] = (DRAFT_FIELD, PRERELEASE_FIELD)

#: What each document is called in a refusal, so a message says which of the two
#: was being read.
_RELEASE_KIND: Final[str] = "release"
_TAG_KIND: Final[str] = "tag"

#: Why an entry could not be the latest release, in the order a `detail` reports
#: them. Named because the row records the reason, and a row that said "no
#: published releases" about a document full of undated ones would be a false
#: reason in a table nothing may correct.
UNPUBLISHED_RELEASE: Final[str] = "draft or prerelease"
UNTAGGED_RELEASE: Final[str] = "carrying no tag"
UNDATED_RELEASE: Final[str] = "carrying no usable publication date"
_EXCLUSION_ORDER: Final[tuple[str, ...]] = (UNPUBLISHED_RELEASE, UNTAGGED_RELEASE, UNDATED_RELEASE)

#: What a row says in its own words, in each of the ways it is reachable. Named so
#: the row a run writes and the case that reads it back cannot drift, and kept
#: apart because they are different facts about the same repository.
NO_RELEASES_DETAIL: Final[str] = "the source repository publishes no releases"
NO_TAGS_DETAIL: Final[str] = "the source repository publishes no releases and lists no tags"
NO_LATEST_RELEASE_DETAIL: Final[str] = "the source repository lists no release this collector can read as its latest"
TAGGED_DETAIL: Final[str] = (
    "the source repository publishes no releases; this is the newest tag it lists, in the source's own order, "
    "and a tag carries no publication date"
)

#: What a `not_found` sentinel row records beside the base's own reason.
#:
#: `core/transport.py` reads `404` and `410` as "the source says this does not
#: exist", which is right in general and ambiguous against this source in
#: particular: GitHub answers `404` identically for an absent repository, a
#: private one, one that has moved and one that is blocked, precisely so an
#: unauthenticated reader cannot enumerate private repositories. The row says
#: what it can actually support rather than claiming the stronger fact -- and it
#: says it the same way with or without `CPM_GITHUB_TOKEN` (`CPM-OPERATE-S05`),
#: because a credential changes *which* repositories answer `404` and nothing
#: about what a `404` means; a row that named the credential's presence would be
#: a permanent statement about a setting that rotates.
ABSENT_CAVEAT: Final[str] = (
    "a 404 from this source cannot tell an absent repository from a private, moved or blocked one: it "
    "answers 404 to all four, and this row records only that it did"
)


class SourceLocatorError(ValueError):
    """A package's source repository cannot be turned into a locator to read.

    A `ValueError` subclass, matching `core/collection.py`'s
    `CollectorConfigurationError` and `collectors/models.py`'s
    `InventoryReadError`: every "this input cannot describe what it claims to" in
    this product is a `ValueError`, so a caller catching one catches them all.

    **It escapes `collect()` rather than becoming an evidence row, and that is a
    decision rather than an omission.** `source_for` is called before the window,
    the allowance and the transport, so the run ledger row exists and is finalized
    `failed` carrying this message -- which is the honest record of what happened:
    the collector was asked to observe a source repository that this package does
    not have, or that this collector cannot read. Writing an evidence row instead
    would mean the base offering a `not_applicable` write path, which it does not
    and which no story has asked it for; inventing one here would put a second
    evidence writer beside the base's (`CPM-AD-7`).

    What follows from that is a selection obligation rather than a gap: the caller
    collects for packages whose source repository a resolution has established.
    `collectors/selection.py` selects packages for *identity review*
    (`CPM-IDENTITY-S04`) and is not that selection; the one this collector needs
    does not exist yet, and `CPM-CURRENCY-S05`'s full-inventory sweep is the story
    that owns it.
    """


class SourceReleaseDocumentError(ValueError):
    """A source's release or tag document could not be read as what it claims to be.

    Raised from `translate`, which the base answers by writing an `error` row and
    re-raising unchanged -- so the guarantee `CPM-NFR-3` states holds on this path
    too: never a clean result, and never no row.

    Refused rather than partially read, on the terms `collectors/tasks.py`'s
    record contract states: an entry this collector cannot understand is a source
    whose shape has changed, and reading the entries around it would record a
    latest release chosen from whatever happened to still parse -- permanently, in
    a log nothing may correct.
    """


@dataclass(frozen=True, slots=True)
class ReleaseFacts:
    """What one document says, as the facts the evidence row holds.

    Frozen and slotted like every other value object in this product, so what a
    document was read to mean cannot be edited between being read and being
    written.

    Attributes:
        state: What the lookup concluded. `ok` for a document naming a latest
            release or tag, `not_found` for one that names none. Never `error` --
            a document that could not be read raises rather than returning, and a
            call that produced no document never reaches here.
        latest_version: The tag the latest release carries, or the newest tag the
            source listed. Exactly as the source spelled it. Empty when there is
            none.
        released_at: When that release was published, or `None` -- both when there
            is no release and when the version came from a *tag*, which carries no
            date. The row's `source` column is what tells those apart.
        last_activity_at: The most recent instant the source showed any release
            activity, prereleases and drafts included, or `None` when it showed
            none. Always `None` on the tag path, which has no instants at all.
        releases_seen: How many *releases* the document listed. Zero is an
            observation -- see `SourceReleaseSnapshot` -- and stays zero on the
            tag path, because the fallback fires only when the release list was
            empty and that is the fact the column records.
        detail: Why there is no latest release, or where the version came from
            when it did not come from a release. Empty for a determinate
            observation of a published release, which needs no explanation.
        source: The locator this answer came from, recorded on the row so an
            append-only history can say which endpoint -- and which repository --
            produced each observation.

    """

    state: OutcomeState
    latest_version: str
    released_at: datetime | None
    last_activity_at: datetime | None
    releases_seen: int
    detail: str
    source: str


def releases_locator(repository_url: str) -> str:
    """Return the locator naming a source repository's releases.

    Args:
        repository_url: The package's `source_repository_url`, as `identity`
            stored it.

    Returns:
        The API locator naming this repository's releases, one page of
        `RELEASES_PER_PAGE`.

    Raises:
        SourceLocatorError: On every refusal `_repository_segments` makes.

    """
    owner, repository = _repository_segments(repository_url)
    return f"https://{GITHUB_API_HOST}/repos/{owner}/{repository}/releases?per_page={RELEASES_PER_PAGE}"


def tags_locator(repository_url: str) -> str:
    """Return the locator naming a source repository's tags.

    The fallback AC 1's "or tag" asks for. Read only when the release list came
    back empty -- see the module docstring for why a tagged answer is weaker than
    a released one and what the row records instead of a date.

    Args:
        repository_url: The package's `source_repository_url`, as `identity`
            stored it.

    Returns:
        The API locator naming this repository's tags, one page of
        `TAGS_PER_PAGE`.

    Raises:
        SourceLocatorError: On every refusal `_repository_segments` makes.

    """
    owner, repository = _repository_segments(repository_url)
    return f"https://{GITHUB_API_HOST}/repos/{owner}/{repository}/tags?per_page={TAGS_PER_PAGE}"


def _repository_segments(repository_url: str) -> tuple[str, str]:
    """Return the owner and repository a stored URL names, or refuse it.

    Pure: no database, no clock, no network. The whole of the host-specific work
    is here, which is what makes every refusal below reachable in the fast tier,
    and it is shared by both locators so the two cannot come to disagree about
    which URLs they will read.

    Both segments are lower-cased and percent-encoded. **Case** because GitHub
    treats an owner and a repository case-insensitively while every key built from
    the result is exact: `Conda-Forge/NumPy-Feedstock` and
    `conda-forge/numpy-feedstock` are one repository, and reading them as two
    would mean two response-cache entries and two spellings of `source` in an
    append-only log. **Encoding** because a stored URL is data a resolution wrote
    and a resolution reads registries: a segment carrying a space, a slash or a
    percent sign would otherwise reshape the locator's path.

    Args:
        repository_url: The package's `source_repository_url`.

    Returns:
        The owner and the repository, lower-cased and percent-encoded.

    Raises:
        SourceLocatorError: When the package has no source repository; when the
            URL cannot be parsed at all; when it names a scheme this product does
            not read; when it names no host, or a host this collector cannot ask;
            when its path is not an `owner/repository` pair; when the repository
            segment is nothing but a `.git` suffix; or when either segment is a
            relative reference.

    """
    if not isinstance(repository_url, str) or not repository_url.strip():
        message = (
            f"a source release cannot be looked up for source_repository_url={repository_url!r}: this package "
            f"has no source repository. CPM-FR-7 observes the repository a resolution established, and a "
            f"package that has none has nothing for this collector to read (CPM-FR-1)."
        )
        raise SourceLocatorError(message)

    try:
        parts = urlsplit(repository_url.strip())
        host = (parts.hostname or "").lower()
        # Read and discarded: this locator is rebuilt from the owner and the
        # repository, so a port never reaches it. What the access is for is the
        # refusal -- `.port` is where a non-numeric one raises, and a stored URL
        # this malformed is one a resolution should not have written.
        _ = parts.port
    except ValueError as malformed:
        # `urlsplit`, `.hostname` and `.port` each raise a bare `ValueError` on a
        # malformed authority -- an unclosed IPv6 bracket, a port that is not a
        # number. Letting it out would break this module's stated contract that
        # every refusal here is a `SourceLocatorError`, and would reach the run
        # recorder as a message about parsing rather than about a package.
        message = (
            f"{repository_url!r} cannot be read as a URL: {type(malformed).__name__}: {malformed}. A stored "
            f"source repository is data a resolution wrote, so a malformed one is refused rather than repaired."
        )
        raise SourceLocatorError(message) from malformed

    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        message = (
            f"{repository_url!r} names the scheme {parts.scheme!r}, which is not one this product reads. The "
            f"transport allows {sorted(ALLOWED_SCHEMES)} and nothing else, so a locator built from this would "
            f"be refused at the call rather than here (CPM-AD-27)."
        )
        raise SourceLocatorError(message)

    if not host:
        message = (
            f"{repository_url!r} names no host, so there is nothing to ask. A repository URL addresses a "
            f"server; a path on its own addresses this process's filesystem."
        )
        raise SourceLocatorError(message)

    if host not in GITHUB_WEB_HOSTS:
        message = (
            f"{repository_url!r} is hosted at {host!r}, and this collector reads {sorted(GITHUB_WEB_HOSTS)}. "
            f"There is no cross-host latest-release endpoint, so a second host is a second document reader "
            f"rather than a second entry in a table -- refused rather than guessed at."
        )
        raise SourceLocatorError(message)

    segments = [segment.lower() for segment in parts.path.split("/") if segment]
    if len(segments) != _REPOSITORY_SEGMENTS:
        message = (
            f"{repository_url!r} has the path {parts.path!r}, which is not an owner and a repository. A deeper "
            f"path names something inside a repository rather than the repository itself, and a shorter one "
            f"names an account."
        )
        raise SourceLocatorError(message)

    owner, repository = segments
    # Stripped *before* the traversal check, and the order is the check. A
    # repository segment of `..git` becomes `..` here, so a check that ran first
    # would pass it and the locator would carry a path the source is entitled to
    # resolve a level up -- against the one host this collector may ask.
    repository = repository.removesuffix(_GIT_SUFFIX)
    if not repository:
        message = f"{repository_url!r} names a repository that is nothing once its {_GIT_SUFFIX} suffix is removed."
        raise SourceLocatorError(message)
    if owner in _TRAVERSAL_SEGMENTS or repository in _TRAVERSAL_SEGMENTS:
        # Percent-encoding does not close this one: `.` and `..` are unreserved,
        # so `quote` leaves them exactly as they are.
        message = (
            f"{repository_url!r} has the path {parts.path!r}, whose segments include a relative reference. A "
            f"repository is named, not navigated to, and a locator built from this would be a path the source "
            f"is entitled to resolve somewhere else."
        )
        raise SourceLocatorError(message)

    return quote(owner, safe=""), quote(repository, safe="")


def release_facts(body: str, *, source: str) -> ReleaseFacts:
    """Read one release document into the facts the evidence row holds.

    Pure: no database, no clock, no network. See the module docstring for why an
    empty document is `not_found` rather than nothing, and `ReleaseFacts` for what
    each fact means.

    **Which entry is the latest release.** The newest entry that is neither a
    draft nor a prerelease and that carries both a tag and a usable publication
    instant -- which is what the source's own "latest release" means and what a
    currency comparison (`CPM-FR-16`) has to be made against. Ties are broken in
    favour of the entry the document listed first, because the source lists newest
    first; the comparison is written as "take it when it is strictly newer" so
    that a tie is decided here rather than by whichever order a later reader
    happens to iterate in.

    **When there is no latest release, the row says why there is not.** An entry
    can be excluded for three different reasons, and a row that reported "no
    published releases" about a document full of undated ones would be recording a
    false reason in a table nothing may correct. The reasons are counted and the
    `detail` names the ones that actually occurred.

    **An unusable date is missing rather than fatal, and a *mistyped* one is
    neither.** A date field arriving as a number or an object is the document's
    shape changing and is refused with the rest of the shape checks. A date that
    is a string this collector cannot read -- a format it does not parse, an
    instant with no offset -- is one entry's problem: it cannot be the latest
    release and it does not move the activity signal, and inventing an instant for
    it is the guess PRD Appendix A.1's data rules forbid.

    Args:
        body: The document the source served.
        source: The locator it was served from, recorded on the row and named in
            the refusal messages.

    Returns:
        The facts. `state` is `ok` when a latest release was found and
        `not_found` otherwise.

    Raises:
        SourceReleaseDocumentError: When the body is longer than
            `MAX_DOCUMENT_CHARACTERS`, is not JSON, is not a list, has an element
            that is not an object, has an element whose flags, tag or dates are of
            the wrong type, or names a latest release whose tag is wider than the
            column that has to hold it.

    """
    entries = _entries_in(body, source=source, kind=_RELEASE_KIND)
    seen = len(entries)
    if not entries:
        return ReleaseFacts(
            state=OutcomeState.NOT_FOUND,
            latest_version="",
            released_at=None,
            last_activity_at=None,
            releases_seen=0,
            detail=NO_RELEASES_DETAIL,
            source=source,
        )

    last_activity_at: datetime | None = None
    latest_version = ""
    released_at: datetime | None = None
    excluded: Counter[str] = Counter()
    for position, entry in enumerate(entries):
        _require_readable_release(entry, position=position, source=source)
        activity = _activity_instant(entry)
        if activity is not None and (last_activity_at is None or activity > last_activity_at):
            last_activity_at = activity
        reason = _excluded_because(entry)
        if reason is not None:
            excluded[reason] += 1
            continue
        instant = _instant(entry.get(PUBLISHED_AT_FIELD))
        tag = str(entry[TAG_NAME_FIELD]).strip()
        # Strictly newer, so the first entry of a tie survives -- see the
        # docstring. Written this way rather than as "skip it when it is not
        # newer" because the two differ only when the comparison itself is wrong,
        # and this spelling is the one whose failure a case can see.
        if instant is not None and (released_at is None or instant > released_at):
            released_at = instant
            latest_version = tag

    if released_at is None:
        return ReleaseFacts(
            state=OutcomeState.NOT_FOUND,
            latest_version="",
            released_at=None,
            last_activity_at=last_activity_at,
            releases_seen=seen,
            detail=_no_latest_detail(excluded),
            source=source,
        )

    _require_storable_version(latest_version, source=source)
    return ReleaseFacts(
        state=OutcomeState.OK,
        latest_version=latest_version,
        released_at=released_at,
        last_activity_at=last_activity_at,
        releases_seen=seen,
        detail="",
        source=source,
    )


def tag_facts(body: str, *, source: str) -> ReleaseFacts:
    """Read one tag document into the facts the evidence row holds.

    AC 1's "or tag", reached only when the release list came back empty. It is a
    weaker answer than a release in two stated ways, and the row carries both:
    the tags endpoint supplies **no date**, so `released_at` and
    `last_activity_at` are NULL; and the order of that list is the source's rather
    than this collector's, so "newest" means "first listed". `releases_seen` stays
    zero, because the fact it records -- that the release list was empty -- is
    still true and is what AC 2 turns on.

    Args:
        body: The document the source served.
        source: The tags locator it was served from.

    Returns:
        The facts. `state` is `ok` when the source listed a usable tag and
        `not_found` when it listed none -- which, reached from an empty release
        list, is AC 2's repository: one that publishes no releases *and* has
        nothing tagged.

    Raises:
        SourceReleaseDocumentError: On the same shape refusals `release_facts`
            makes, plus a tag whose name is not a string, and a tag wider than the
            column that has to hold it.

    """
    entries = _entries_in(body, source=source, kind=_TAG_KIND)
    for position, entry in enumerate(entries):
        _require_readable_tag(entry, position=position, source=source)
        name = str(entry.get(TAG_FIELD, "")).strip()
        if not name:
            continue
        _require_storable_version(name, source=source)
        return ReleaseFacts(
            state=OutcomeState.OK,
            latest_version=name,
            released_at=None,
            last_activity_at=None,
            releases_seen=0,
            detail=TAGGED_DETAIL,
            source=source,
        )
    return ReleaseFacts(
        state=OutcomeState.NOT_FOUND,
        latest_version="",
        released_at=None,
        last_activity_at=None,
        releases_seen=0,
        detail=NO_TAGS_DETAIL,
        source=source,
    )


def _entries_in(body: str, *, source: str, kind: str) -> tuple[dict[str, object], ...]:
    """Decode a document and refuse anything that is not a list of objects.

    Args:
        body: The document the source served.
        source: The locator it was served from, for the messages.
        kind: What the document was expected to hold, for the messages.

    Returns:
        One mapping per entry, in the document's own order.

    Raises:
        SourceReleaseDocumentError: When the body is too long to decode, is not
            JSON, is not a list, or holds an element that is not an object.

    """
    if len(body) > MAX_DOCUMENT_CHARACTERS:
        message = (
            f"{source} served {len(body)} characters, and this collector decodes at most "
            f"{MAX_DOCUMENT_CHARACTERS}. A {kind} page is three orders of magnitude smaller than that, so a "
            f"document this size is a source doing something else -- and decoding it would spend a worker's "
            f"memory and its soft time limit finding out (CPM-AD-9)."
        )
        raise SourceReleaseDocumentError(message)
    try:
        document = json.loads(body)
    except (json.JSONDecodeError, RecursionError) as unreadable:
        # `RecursionError` beside the decode error deliberately: `json.loads`
        # recurses per level of nesting, so a deeply nested document raises it
        # rather than a decode error, and an uncaught one escapes as a crash
        # naming this module's own stack rather than the source that caused it.
        message = (
            f"{source} did not serve a readable {kind} document: {type(unreadable).__name__}: {unreadable}. "
            f"The observation is refused rather than recorded empty, which would say the repository publishes "
            f"nothing (CPM-FR-7)."
        )
        raise SourceReleaseDocumentError(message) from unreadable

    if not isinstance(document, list):
        message = (
            f"{source} served {type(document).__name__} rather than a list of {kind}s. A source whose shape "
            f"has changed is refused rather than read for whatever still parses."
        )
        raise SourceReleaseDocumentError(message)

    entries: list[dict[str, object]] = []
    for position, entry in enumerate(document):
        if not isinstance(entry, dict):
            message = f"{source} lists {type(entry).__name__} at position {position} rather than a {kind} object."
            raise SourceReleaseDocumentError(message)
        entries.append(entry)
    return tuple(entries)


def _require_readable_release(entry: dict[str, object], *, position: int, source: str) -> None:
    """Refuse a release whose flags, tag or dates are not what they claim to be.

    The two flags decide whether an entry is a release at all, the tag is what a
    currency comparison is made against, and the dates are what order the entries.
    A value of the wrong *type* in any of them is a source this collector no
    longer understands rather than a field to read past -- and a truthiness test
    on a flag would be worse than a refusal, because the string `"false"` is
    truthy and would silently exclude every release.

    Args:
        entry: One decoded release.
        position: Where it sat, so a refusal names the entry a reader can find.
        source: The locator, for the message.

    Raises:
        SourceReleaseDocumentError: When a flag is present and is not a boolean,
            when the tag is present and is not a string, or when a date is present
            and is neither a string nor null.

    """
    mistyped_flags = sorted(field for field in _FLAG_FIELDS if field in entry and not isinstance(entry[field], bool))
    if mistyped_flags:
        message = (
            f"{source} lists a release at position {position} whose {mistyped_flags} is not a boolean. Whether "
            f"an entry is a draft or a prerelease decides whether it is a release at all, and a value this "
            f"collector has to interpret is one it would interpret wrongly."
        )
        raise SourceReleaseDocumentError(message)
    _require_string(entry, field=TAG_NAME_FIELD, position=position, source=source, kind=_RELEASE_KIND)
    for field in ACTIVITY_FIELDS:
        _require_string(entry, field=field, position=position, source=source, kind=_RELEASE_KIND)


def _require_readable_tag(entry: dict[str, object], *, position: int, source: str) -> None:
    """Refuse a tag whose name is not a string.

    Args:
        entry: One decoded tag.
        position: Where it sat, for the message.
        source: The locator, for the message.

    Raises:
        SourceReleaseDocumentError: When the name is present and is not a string.

    """
    _require_string(entry, field=TAG_FIELD, position=position, source=source, kind=_TAG_KIND)


def _require_string(entry: dict[str, object], *, field: str, position: int, source: str, kind: str) -> None:
    """Refuse a field that is present and is neither a string nor null.

    Null is permitted throughout: a source saying "this entry has no publication
    date" is a source answering, and the entry is then simply not a candidate.
    What is refused is a *type* nobody could read, which is the document's shape
    changing.

    Args:
        entry: The decoded entry.
        field: Which field to check.
        position: Where the entry sat, for the message.
        source: The locator, for the message.
        kind: What the document holds, for the message.

    Raises:
        SourceReleaseDocumentError: When the field is present, is not null, and is
            not a string.

    """
    value = entry.get(field)
    if field in entry and value is not None and not isinstance(value, str):
        message = (
            f"{source} lists a {kind} at position {position} whose {field} is {type(value).__name__} rather "
            f"than a string. A source whose shape has changed is refused rather than read past."
        )
        raise SourceReleaseDocumentError(message)


def _require_storable_version(version_string: str, *, source: str) -> None:
    """Refuse a version wider than the column that has to hold it.

    Refused where the value enters rather than where it lands, on the terms
    `collectors/tasks.py` states for the inventory's own bounds: `max_length` is
    enforced by PostgreSQL and ignored by SQLite, so an over-long tag is a stored
    row on a developer's machine and a failed run in the gate (`R-5`).

    Args:
        version_string: The tag the latest release or tag carries.
        source: The locator, for the message.

    Raises:
        SourceReleaseDocumentError: When it is wider than the column.

    """
    width = SourceReleaseSnapshot._meta.get_field("latest_version").max_length  # noqa: SLF001 - Django's own public-by-convention API
    if width is not None and len(version_string) > width:
        message = (
            f"{source} names a latest version tagged with {len(version_string)} characters, and the column "
            f"that holds it takes {width}. The observation is refused rather than truncated: a stored version "
            f"that is not the version the source published is a comparison that will be wrong for ever."
        )
        raise SourceReleaseDocumentError(message)


def _excluded_because(entry: dict[str, object]) -> str | None:
    """Return why an entry cannot be the latest release, or `None` when it can.

    Args:
        entry: One decoded release, already type-checked.

    Returns:
        One of `_EXCLUSION_ORDER`, or `None` for an entry that is a published,
        tagged, dated release. The reasons are checked in the order a reader would
        ask them, so an entry excluded for two of them is reported under the first
        -- which keeps the counts a partition rather than a multiset.

    """
    if any(entry.get(field) is True for field in _FLAG_FIELDS):
        return UNPUBLISHED_RELEASE
    tag = entry.get(TAG_NAME_FIELD)
    if not isinstance(tag, str) or not tag.strip():
        return UNTAGGED_RELEASE
    if _instant(entry.get(PUBLISHED_AT_FIELD)) is None:
        return UNDATED_RELEASE
    return None


def _no_latest_detail(excluded: Counter[str]) -> str:
    """Return the reason a document naming releases still names no latest one.

    Args:
        excluded: How many entries were excluded for each reason.

    Returns:
        The reason, naming only the causes that actually occurred and in a fixed
        order. A row saying "all drafts and prereleases" about a document full of
        undated entries would be a false reason in a table nothing may correct,
        which is what this exists to prevent.

    """
    counted = [f"{excluded[reason]} {reason}" for reason in _EXCLUSION_ORDER if excluded[reason]]
    return f"{NO_LATEST_RELEASE_DETAIL}: {', '.join(counted)}"


def _activity_instant(entry: dict[str, object]) -> datetime | None:
    """Return the most recent instant one entry carries.

    Args:
        entry: One decoded release.

    Returns:
        The later of its publication and creation instants, or whichever of them
        is usable, or `None` when neither is. The *maximum* rather than the first
        available: a release edited after publication carries a `created_at` that
        can be later than its `published_at`, and taking the first would understate
        a live project's activity.

    """
    instants = [instant for field in ACTIVITY_FIELDS if (instant := _instant(entry.get(field))) is not None]
    return max(instants) if instants else None


def _instant(value: object) -> datetime | None:
    """Return an aware instant a source stated, or `None` where it stated none.

    Args:
        value: What the entry carried for a date field, already known to be a
            string or absent.

    Returns:
        The parsed instant, or `None` when the value is absent, is blank, does not
        parse, or parses to a naive one. A naive value is discarded rather than
        assumed to be UTC: there is no offset to convert from, and an instant
        shifted by a guess would be written into a row nothing may correct
        (`CPM-AD-26`).

    """
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return parsed if is_aware(parsed) else None


class SourceReleaseCollector(Collector):
    """The collector that observes a package's upstream releases. Writes `source_release_snapshots`.

    Three methods and nine declarations. See the module docstring for what the
    base owns, why an empty release list falls back to tags, and what a `404`
    from an unauthenticated read can and cannot be read to mean.
    """

    #: The nine declarations the base checks at construction. Every one is written
    #: out, including the two that would otherwise inherit a usable default,
    #: because a declaration a reader has to go and look up in the base is one they
    #: cannot check against the source this collector reads.
    name: ClassVar[str] = COLLECTOR_NAME

    evidence_model: ClassVar[type[AppendOnlyModel] | None] = SourceReleaseSnapshot

    observation_window: ClassVar[timedelta | None] = SOURCE_RELEASE_OBSERVATION_WINDOW

    timeout: ClassVar[float | None] = SOURCE_RELEASE_TIMEOUT

    retries: ClassVar[int] = SOURCE_RELEASE_RETRIES

    rate_limit: ClassVar[RateLimit] = SOURCE_RELEASE_RATE_LIMIT

    headers: ClassVar[Mapping[str, str]] = SOURCE_RELEASE_HEADERS

    #: The one host the credential in `headers` belongs to (`CPM-OPERATE-S05`).
    #: The base strips `Authorization` from any fetch aimed elsewhere, and the
    #: tag fallback below asks the base for its headers on the same terms.
    credential_host: ClassVar[str | None] = GITHUB_API_HOST

    freshness_target: ClassVar[timedelta | None] = SOURCE_RELEASE_FRESHNESS_TARGET

    response_cache_ttl: ClassVar[timedelta | None] = SOURCE_RELEASE_CACHE_TTL

    #: How often the full-inventory sweep dispatches this collector
    #: (`CPM-CURRENCY-S05`). Bound to the module constant the freshness target
    #: and the observation window are already derived from, so the number moves
    #: nowhere: what changes is that something now *reads* it.
    #: `config/startup/stage_two.py` reconciles it against this collector's
    #: `CELERY_BEAT_SCHEDULE` entry at boot, in both directions.
    cadence: ClassVar[timedelta | None] = SOURCE_RELEASE_CADENCE

    #: The locator this run asked for, remembered when `source_for` answered.
    #:
    #: Held on the instance because `sentinel_evidence` has to record it and is
    #: not handed it: the base's hook carries the state, the package, the instant
    #: and the reason, and the locator is the one thing the row needs that the
    #: signature does not offer. Recomputing it there would mean a second database
    #: read on a path that is already failing -- and a refusal raised from *inside*
    #: `sentinel_evidence` would replace the reason the run is failing for, which
    #: is the exact hazard `CollectionWriteError` exists to prevent.
    #:
    #: Safe because the base asks for the locator first on both entry points and
    #: never reaches a sentinel row without it. Blank until it has, which is what a
    #: row records if a caller ever invokes the hook on its own -- blank means
    #: missing, and inventing a locator would be worse.
    _locator: str = ""

    def __init__(
        self,
        *,
        clock: Clock,
        transport: Transport | None = None,
        limiter: RateLimiter | None = None,
        response_cache: ResponseCache | None = None,
    ) -> None:
        """Declare the headers and the allowance this instance sends, from the credential setting.

        The base reads `headers` and `rate_limit` once, at construction, and
        validates both; so the credential is read here, once, and the two
        declarations are set on the *instance* before the base looks. The class
        attributes above stay the unauthenticated declarations -- what every
        audit and declaration test reads, and what this instance sends when no
        token is set. The setting is evaluated once per process, so a token
        rotated in the environment reaches the next worker restart, and the
        first collection after it is the first to send it (`CPM-OPERATE-S05`).

        Args:
            clock: The clock every instant in this run is read from.
            transport: The seam `CPM-AD-27` opens, or `None` for the real one.
            limiter: The rate limiter, or `None` for the shared cache-backed one.
            response_cache: The response cache, or `None` for the shared one.

        Raises:
            ImproperlyConfigured: When `CPM_GITHUB_TOKEN` holds a value that can
                never become a header -- refused at boot already, and refused
                again here so nothing that bypassed the hook can send it.
            CollectorConfigurationError: The base's own refusals, unchanged;
                `_require_headers` still validates the assembled mapping.

        """
        token = github_token()
        # Instance attributes shadowing the two ClassVars, which is the one
        # arrangement that keeps the class's declarations honest *and* lets the
        # base's constructor read what this instance actually sends. mypy reads
        # an assignment to a ClassVar through `self` as a mistake, and here it
        # is the point.
        self.headers = authenticated_headers(SOURCE_RELEASE_HEADERS, token)  # type: ignore[misc]
        self.rate_limit = AUTHENTICATED_CORE_ALLOWANCE if token else SOURCE_RELEASE_RATE_LIMIT  # type: ignore[misc]
        super().__init__(clock=clock, transport=transport, limiter=limiter, response_cache=response_cache)

    @classmethod
    def selectable_packages(cls) -> Iterable[int]:
        """Return the packages this collector can be asked about: the ones with a source repository.

        The complement of `source_for`'s own refusals, as far as a query can
        express it. `_repository_url` reads exactly one column -- and
        `MAPPED_FIELDS[SOURCE_REPOSITORY]` makes that column the whole of what an
        `established` source-repository mapping records -- so a non-blank
        `source_repository_url` *is* "resolution established a source
        repository", and a blank one is the refusal `CPM-CURRENCY-S01` recorded
        as deferred: offered every package, this collector would fail every run
        for every package nobody has resolved.

        **What the selection does not filter, and why that is right.** A URL
        this collector cannot read -- another host, an unparseable authority, a
        path that is not `owner/repository` -- is still selected and still fails
        its run. That is deliberate: those are refusals about the *value*
        resolution stored, they are not expressible as a query, and a `failed`
        ledger row naming the URL is how an operator learns that identity
        recorded something this collector cannot use. Hiding them would make the
        sweep quieter and the inventory no better observed.

        Returns:
            The primary keys, as a lazy queryset ordered by key -- streamed by
            `collectors/sweep.py` rather than materialised, so `CPM-NFR-1`'s ten
            thousand never exist as a list. Ordered so that two dispatches of one
            inventory enqueue in the same sequence, which is what makes a
            partially enqueued sweep's `detail` mean something.

        """
        return Package.objects.exclude(source_repository_url="").order_by("pk").values_list("pk", flat=True)

    def source_for(self, *, package_id: int) -> str:
        """Return the locator this collector reads for one package.

        Args:
            package_id: The package being collected, by the integer primary key
                `CPM-AD-3` fixes.

        Returns:
            The API locator naming this package's upstream releases. Remembered
            on the instance as well as returned -- see `_locator`.

        Raises:
            SourceLocatorError: When the package has no source repository, or has
                one this collector cannot read. See that class for why this
                escapes rather than becoming an evidence row.

        """
        self._locator = releases_locator(self._repository_url(package_id))
        return self._locator

    def translate(self, payload: Payload, *, package_id: int, observed_at: datetime) -> Sequence[AppendOnlyModel]:
        """Turn one release document into the one row it is worth.

        One row and never none: the base reads an empty translation as a parser
        that no longer matches its source, writes an `error` row and finalizes
        `failed` -- which is not what a repository publishing nothing means.

        An empty release list falls back to the repository's tags, which is AC 1's
        "or tag" and the one place this collector issues a second call. See the
        module docstring.

        Args:
            payload: What the source said, recorded. Reached only for a call the
                source answered -- absence and failure are the base's to record.
            package_id: The package the observation is about.
            observed_at: The instant to stamp the row with, from the injected
                clock. The base refuses a row stamped with anything else.

        Returns:
            One unsaved `SourceReleaseSnapshot`.

        Raises:
            SourceReleaseDocumentError: When the document cannot be read as
                releases. The base writes an `error` row and re-raises.
            SourceLocatorError: When the tag fallback is reached for a package
                whose repository URL has stopped being readable between the two
                calls.

        """
        facts = release_facts(payload.body, source=payload.source)
        if facts.state is OutcomeState.NOT_FOUND and facts.releases_seen == 0:
            facts = self._tagged_instead(facts, package_id=package_id)
        return [
            SourceReleaseSnapshot(
                observed_at=observed_at,
                package_id=package_id,
                source=facts.source,
                state=facts.state.value,
                latest_version=facts.latest_version,
                released_at=facts.released_at,
                last_activity_at=facts.last_activity_at,
                releases_seen=facts.releases_seen,
                detail=facts.detail,
                trace_id=current_trace_id(),
            ),
        ]

    def _tagged_instead(self, releases: ReleaseFacts, *, package_id: int) -> ReleaseFacts:
        """Read the repository's tags, for a repository that publishes no releases.

        **The one second call this collector makes**, and it is bounded three
        ways: it is reached only from an empty release list, it is one page, and a
        failure of it never fails the collection -- the release answer stands,
        with the reason appended, because "publishes no releases" is a fact this
        run did establish.

        The base's allowance was charged once, before the first call, so this
        request is not counted against it. That is recorded as deferred rather
        than corrected here: charging it would mean reaching past the base's
        orchestration into the limiter, and the arithmetic belongs with the story
        that first sweeps at volume.

        Its headers come from the base's `headers_for`, which keeps the
        credential for this locator because the tags endpoint is the declared
        credential host -- the same call the recipe read in
        `collectors/feedstock.py` makes, where the answer is to strip it. A
        failure of it is worded by the base's `failure_detail`, so a `401` to
        a credentialed request says so inside the succeeded row's detail.

        Args:
            releases: What the release document said, which is the answer this
                falls back from and returns to on any failure.
            package_id: The package the observation is about.

        Returns:
            The tag document's facts, or the release facts with the reason the
            fallback could not be read appended.

        Raises:
            SourceLocatorError: When the package's repository URL cannot be read
                as a locator. Unreachable in practice -- `source_for` built one
                from the same column moments earlier -- and raised rather than
                swallowed if the row changed underneath.

        """
        locator = tags_locator(self._repository_url(package_id))
        sent = request_headers(declared=self.headers_for(locator), entry=None)
        try:
            payload = self._transport.fetch(locator, headers=sent)
        except TransportError as failure:
            detail = failure_detail(failure, credentialed=carries_credential(sent))
            return replace(releases, detail=f"{releases.detail}; its tags could not be read: {detail}")
        if not payload.found:
            return replace(releases, detail=f"{releases.detail}; {locator} reports that it lists no tags")
        if payload.not_modified:
            # This request carried no validator, so a `304` is the source
            # answering a question nobody asked and there is no body behind it.
            # Left to fall through it would read as a tag document that is not
            # JSON, which is a refusal describing the wrong problem.
            return replace(
                releases,
                detail=f"{releases.detail}; {locator} answered that nothing had changed, to an unconditional request",
            )
        return tag_facts(payload.body, source=locator)

    def _repository_url(self, package_id: int) -> str:
        """Return what a resolution established as this package's source repository.

        The one database read in this module, and it reads `identity` -- which is
        the only application a collector may read (`CPM-AD-7`). Only the column is
        fetched: the row's other fields are none of this collector's business, and
        a full instance would be an invitation to reach for one of them.

        Args:
            package_id: The package being collected.

        Returns:
            The stored `source_repository_url`, which may be blank -- the locator
            functions are what refuse that, and they say so in their own words.

        Raises:
            SourceLocatorError: When no package holds that key.

        """
        repository = Package.objects.filter(pk=package_id).values_list("source_repository_url", flat=True).first()
        if repository is None:
            message = (
                f"package {package_id} has no identity row, so it has no source repository to read. The run "
                f"recorder refuses an unknown package before this is reached (CPM-EVIDENCE-S09), so this is "
                f"the row having gone between the two."
            )
            raise SourceLocatorError(message)
        return repository

    def sentinel_evidence(
        self,
        *,
        state: OutcomeState,
        package_id: int,
        observed_at: datetime,
        detail: str,
    ) -> AppendOnlyModel:
        """Return one row carrying the sentinel the base decided on.

        Every observed fact is absent, and `releases_seen` is NULL rather than
        zero: this run read no document, so it counted nothing -- which is a
        different statement from a document that listed none, and the two have to
        stay apart on the surface that will compare them.

        A `not_found` row carries the caveat an unauthenticated reader owes it:
        the source answers `404` to an absent repository and to a private, moved
        or blocked one alike, and this collector sends no credential.

        Args:
            state: `OutcomeState.ERROR` or `OutcomeState.NOT_FOUND`, decided by
                the base.
            package_id: The package the observation is about.
            observed_at: The instant to stamp the row with.
            detail: What happened, in words worth storing beside the state.

        Returns:
            One unsaved `SourceReleaseSnapshot` carrying the state's value
            verbatim in `state`, which is what lets the base check that it does
            (`CPM-AD-24`).

        Raises:
            CollectorConfigurationError: When asked for a state this collector has
                no row shape for -- anything but `error` and `not_found`. The base
                calls this with those two and documents that it does; the check is
                here because the method is public and because the alternative
                failure is worse than a refusal: a row carrying `ok` and no version
                is refused by the table's own constraint at insert, as an
                `IntegrityError` several frames from the call that was wrong.

        """
        # Written as two comparisons rather than as membership of a declared pair,
        # and that is `tests/unit/django_apps/test_single_ordering_audit.py`'s
        # doing rather than a style choice: a literal holding two `OutcomeState`
        # members outside `core/outcomes.py` is what that audit calls a second
        # precedence order, and it is right to -- a pair of states written down
        # anywhere else is one keystroke from being read as a ranking. The two
        # states this collector shapes a row for are a *membership* claim, so it
        # is spelled as one.
        if state is not OutcomeState.ERROR and state is not OutcomeState.NOT_FOUND:
            message = (
                f"{type(self).__name__}.sentinel_evidence was asked for {state.value!r}, and this collector "
                f"shapes a sentinel row for {OutcomeState.ERROR.value!r} and {OutcomeState.NOT_FOUND.value!r} "
                f"only. A row carrying any other state would have no version and would be refused by "
                f"source_release_snapshots' own constraint at insert."
            )
            raise CollectorConfigurationError(message)
        return SourceReleaseSnapshot(
            observed_at=observed_at,
            package_id=package_id,
            source=self._locator,
            state=state.value,
            latest_version="",
            released_at=None,
            last_activity_at=None,
            releases_seen=None,
            detail=f"{detail} -- {ABSENT_CAVEAT}" if state is OutcomeState.NOT_FOUND else detail,
            trace_id=current_trace_id(),
        )
