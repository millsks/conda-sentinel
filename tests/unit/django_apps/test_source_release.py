"""What the upstream-release collector declares, what a locator is, and what a document means.

`CPM-FR-7` is two questions, and only one of them needs anything but data. What a
release or tag document *means* -- which entry is the latest, what an empty list
says, where the activity signal comes from -- is a pure function of a string, and
so is turning a repository URL into the locator that answers it. Both are here,
with no database, no socket and no clock, which is the split `CPM-AD-27` exists
to make: "parse, `not_found`, `error` and `not_applicable` handling -- the
majority of this product's behaviour -- stays in the fast unit tier instead of
behind the network."

What is *not* here is everything that needs a run or a row: `collect()` opens
`core/ledger.py`'s recorder, whose first act is to insert a `running` row, and the
tag fallback reads the package's repository URL. Those are in
`tests/integration/django_apps/test_source_release.py`, which is the same split
`CPM-EVIDENCE-S05` recorded for its own matrix and resolved the same way.

**The declarations are asserted against their derivations rather than against
themselves.** A case reading `SOURCE_RELEASE_FRESHNESS_TARGET == timedelta(days=2)`
asserts that somebody typed two days twice. What is worth pinning is the
arithmetic PRD Open Question 7 settled -- the target is the cadence times one plus
the tolerated misses, and it is strictly greater than the cadence -- and the bound
`core/transport.py` states in prose and nothing computed: one collection's worst
case has to fit inside the inherited Celery soft limit (`CPM-AD-9`), with room to
spare, read from the settings module rather than repeated here.

No database, no network: nothing here saves a row, no queryset is evaluated, and
every payload is a literal.
"""

from __future__ import annotations

import ast
import importlib.metadata
import json
import re
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import TYPE_CHECKING
from typing import Any
from typing import Final

import pytest
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings

from conda_sentinel.collectors import agent
from conda_sentinel.collectors.github import AUTHENTICATED_CORE_ALLOWANCE
from conda_sentinel.collectors.github import AUTHORIZATION_HEADER
from conda_sentinel.collectors.github import BEARER_SCHEME
from conda_sentinel.collectors.github import GITHUB_TOKEN_SETTING
from conda_sentinel.collectors.models import SourceReleaseSnapshot
from conda_sentinel.collectors.source_release import ABSENT_CAVEAT
from conda_sentinel.collectors.source_release import ACTIVITY_FIELDS
from conda_sentinel.collectors.source_release import COLLECTOR_NAME
from conda_sentinel.collectors.source_release import DISTRIBUTION_NAME
from conda_sentinel.collectors.source_release import DRAFT_FIELD
from conda_sentinel.collectors.source_release import GITHUB_API_HOST
from conda_sentinel.collectors.source_release import GITHUB_WEB_HOSTS
from conda_sentinel.collectors.source_release import MAX_DOCUMENT_CHARACTERS
from conda_sentinel.collectors.source_release import NO_LATEST_RELEASE_DETAIL
from conda_sentinel.collectors.source_release import NO_RELEASES_DETAIL
from conda_sentinel.collectors.source_release import NO_TAGS_DETAIL
from conda_sentinel.collectors.source_release import PRERELEASE_FIELD
from conda_sentinel.collectors.source_release import PROJECT_URL
from conda_sentinel.collectors.source_release import PUBLISHED_AT_FIELD
from conda_sentinel.collectors.source_release import RELEASES_PER_PAGE
from conda_sentinel.collectors.source_release import SOURCE_RELEASE_CACHE_TTL
from conda_sentinel.collectors.source_release import SOURCE_RELEASE_CADENCE
from conda_sentinel.collectors.source_release import SOURCE_RELEASE_FRESHNESS_TARGET
from conda_sentinel.collectors.source_release import SOURCE_RELEASE_HEADERS
from conda_sentinel.collectors.source_release import SOURCE_RELEASE_OBSERVATION_WINDOW
from conda_sentinel.collectors.source_release import SOURCE_RELEASE_RATE_LIMIT
from conda_sentinel.collectors.source_release import SOURCE_RELEASE_RETRIES
from conda_sentinel.collectors.source_release import SOURCE_RELEASE_TIMEOUT
from conda_sentinel.collectors.source_release import TAG_FIELD
from conda_sentinel.collectors.source_release import TAG_NAME_FIELD
from conda_sentinel.collectors.source_release import TAGGED_DETAIL
from conda_sentinel.collectors.source_release import TAGS_PER_PAGE
from conda_sentinel.collectors.source_release import TOLERATED_MISSED_RUNS
from conda_sentinel.collectors.source_release import UNDATED_RELEASE
from conda_sentinel.collectors.source_release import UNKNOWN_VERSION
from conda_sentinel.collectors.source_release import UNPUBLISHED_RELEASE
from conda_sentinel.collectors.source_release import UNTAGGED_RELEASE
from conda_sentinel.collectors.source_release import USER_AGENT
from conda_sentinel.collectors.source_release import SourceLocatorError
from conda_sentinel.collectors.source_release import SourceReleaseCollector
from conda_sentinel.collectors.source_release import SourceReleaseDocumentError
from conda_sentinel.collectors.source_release import distribution_version
from conda_sentinel.collectors.source_release import release_facts
from conda_sentinel.collectors.source_release import releases_locator
from conda_sentinel.collectors.source_release import tag_facts
from conda_sentinel.collectors.source_release import tags_locator
from conda_sentinel.collectors.source_release import worst_case_call_seconds
from conda_sentinel.collectors.tasks import COLLECT_SOURCE_RELEASE_TASK_NAME
from conda_sentinel.core.clock import FixedClock
from conda_sentinel.core.collection import CONDITIONAL_HEADERS
from conda_sentinel.core.collection import CollectorConfigurationError
from conda_sentinel.core.outcomes import OutcomeState
from conda_sentinel.core.queues import Queue
from conda_sentinel.core.queues import queue_for
from conda_sentinel.core.transport import ALLOWED_SCHEMES
from conda_sentinel.core.transport import DEFAULT_BACKOFF_FACTOR
from conda_sentinel.core.transport import DEFAULT_RETRIES
from conda_sentinel.core.transport import MAX_TIMEOUT
from tests.clocks import FIXED_INSTANT
from tests.collectors import A_GITHUB_TOKEN as A_TOKEN
from tests.collectors import recorded_payload
from tests.source_scan import SRC_ROOT
from tests.source_scan import dotted_name
from tests.source_scan import parse

if TYPE_CHECKING:
    from pathlib import Path

    from conda_sentinel.core.transport import Payload

#: The module this file's source sweeps are about, relative to `src/`.
RELEASE_MODULE: Final[str] = "django_apps/conda_sentinel/collectors/source_release.py"

#: The model name a collector module may not construct itself, and the write
#: methods it may not reach for. `CPM-AD-7` says a collector "reads only
#: `identity`", and `CPM-AD-14` says identity is mutated by resolution or the
#: override path and by nothing else -- so this module may *name* `Package`,
#: which it does to read one column, and may not write one.
PACKAGE_MODEL_NAME: Final[str] = "Package"
WRITE_METHODS: Final[frozenset[str]] = frozenset(
    {"abulk_create", "acreate", "asave", "bulk_create", "create", "get_or_create", "save", "update_or_create"},
)

#: A repository URL the cases build locators from, and the two locators it
#: produces. Written out rather than composed from the module's own constants,
#: because a locator assembled the same way twice would agree with itself however
#: wrong it was.
A_REPOSITORY: Final[str] = "https://github.com/conda-forge/numpy-feedstock"
THE_LOCATOR: Final[str] = "https://api.github.com/repos/conda-forge/numpy-feedstock/releases?per_page=30"
THE_TAGS_LOCATOR: Final[str] = "https://api.github.com/repos/conda-forge/numpy-feedstock/tags?per_page=30"

#: The locator a payload claims to have come from, for the document cases. They
#: never build one, so it is a fixed string rather than the value above.
A_SOURCE: Final[str] = "https://api.github.com/repos/an-owner/a-repository/releases?per_page=30"

#: Three instants a document places releases at, oldest first. Spelled as the
#: source spells them -- a `Z` offset, seconds included -- because that is the
#: string the readers have to read.
FIRST_PUBLISHED: Final[str] = "2026-01-05T09:30:00Z"
SECOND_PUBLISHED: Final[str] = "2026-04-11T14:00:00Z"
THIRD_PUBLISHED: Final[str] = "2026-08-22T08:15:00Z"

#: The same three as instants, for the assertions.
FIRST_INSTANT: Final[datetime] = datetime(2026, 1, 5, 9, 30, tzinfo=UTC)
SECOND_INSTANT: Final[datetime] = datetime(2026, 4, 11, 14, 0, tzinfo=UTC)
THIRD_INSTANT: Final[datetime] = datetime(2026, 8, 22, 8, 15, tzinfo=UTC)

#: How many entries the multi-entry documents carry. Named because `PLR2004` is
#: right about a bare number in an assertion: `== 3` says nothing about which
#: three.
THREE_RELEASES: Final[int] = 3
TWO_RELEASES: Final[int] = 2

#: What a single unretried call costs, in units of the declared timeout: one
#: connect phase and one read phase. Named because it is the fact the arithmetic
#: rests on rather than a number that happens to be two.
PHASES_PER_ATTEMPT: Final[int] = 2

#: `urllib3`'s backoff doubles per retry, and the two coefficients the declared
#: three-retry schedule actually uses. Named separately from `PHASES_PER_ATTEMPT`
#: -- they are equal and unrelated, and sharing one constant would tell the next
#: reader that a backoff coefficient is a request phase.
SECOND_RETRY_COEFFICIENT: Final[int] = 2
THIRD_RETRY_COEFFICIENT: Final[int] = 4

#: How much of the inherited soft limit one call may spend.
#:
#: Three quarters, so the claim is "with room for the ledger writes around it"
#: rather than "by a hair": a worst case of 59.9 seconds satisfies a bare `<` and
#: leaves a run that is killed between the call and the evidence write.
SOFT_LIMIT_SHARE: Final[float] = 0.75

#: A package key the cases that need one use. This tier never reads a row, so it
#: names nothing.
A_PACKAGE: Final[int] = 7

#: The marker `_release` treats as "the source did not send this field at all",
#: as distinct from `None`, which is the explicit JSON `null` the source really
#: sends for an unpublished release. The two are different documents and the
#: helper has to be able to write both.
OMITTED: Final[object] = object()


def _release(
    tag: str | None = "v1.0.0", published_at: str | object | None = OMITTED, **overrides: Any
) -> dict[str, Any]:
    """Return one release entry, with any field replaced, nulled or omitted.

    Args:
        tag: The tag the entry carries.
        published_at: When it was published. `OMITTED` leaves the field out;
            `None` writes an explicit JSON `null`, which is what the source sends
            for an entry it never published.
        **overrides: Fields to add or replace; `OMITTED` omits one.

    Returns:
        The release object a source would list.

    """
    entry: dict[str, Any] = {
        TAG_NAME_FIELD: tag,
        PUBLISHED_AT_FIELD: published_at,
        DRAFT_FIELD: False,
        PRERELEASE_FIELD: False,
    }
    entry.update(overrides)
    return {name: value for name, value in entry.items() if value is not OMITTED}


def _published(tag: str, published_at: str, **overrides: Any) -> dict[str, Any]:
    """Return one ordinary published release.

    Args:
        tag: The tag it carries.
        published_at: When it was published.
        **overrides: Fields to add or replace.

    Returns:
        The release object.

    """
    return _release(tag, published_at, **overrides)


def _tag(name: Any = "v1.0.0", **overrides: Any) -> dict[str, Any]:
    """Return one tag entry.

    Args:
        name: The tag's name.
        **overrides: Fields to add or replace; `OMITTED` omits one.

    Returns:
        The tag object a source would list, carrying the one field this collector
        reads and the commit reference it ignores.

    """
    entry: dict[str, Any] = {TAG_FIELD: name, "commit": {"sha": "0" * 40}}
    entry.update(overrides)
    return {key: value for key, value in entry.items() if value is not OMITTED}


def _document(*entries: dict[str, Any]) -> str:
    """Return the body a source would serve for these entries.

    Args:
        *entries: The releases or tags the document lists, newest first as the
            source lists them.

    Returns:
        The JSON body.

    """
    return json.dumps(list(entries))


def _payload(body: str) -> Payload:
    """Return a recorded payload carrying this body.

    Args:
        body: What the source said.

    Returns:
        The `Payload` a transport would have recorded. Built through the shared
        helper rather than by constructing one here, so a case in this file and a
        case in the integration tier are handed the same shape.

    """
    return recorded_payload(source=A_SOURCE, body=body)


def _stopped_clock() -> FixedClock:
    """Return the clock the collector cases inject.

    Returns:
        A `FixedClock` at `tests.clocks.FIXED_INSTANT`. Constructed rather than
        taken from the `fixed_clock` fixture because most of the cases here need
        no clock at all, and a fixture parameter on the few that do would read as
        though the instant mattered to them.

    """
    return FixedClock(instant=FIXED_INSTANT)


def _release_module() -> Path:
    """Return the collector module this file's source sweeps read.

    Returns:
        Its path, resolved from `SRC_ROOT` so the sweep follows the tree rather
        than this file's own location.

    """
    return SRC_ROOT / RELEASE_MODULE


# ---------------------------------------------------------------------------
# The declarations, and the arithmetic behind them.
# ---------------------------------------------------------------------------


def test_the_collector_declares_every_value_the_base_checks() -> None:
    """All nine, written out on the class and carrying the module's own constants.

    Compared by *value* rather than by presence: a class attribute rebound to the
    wrong constant -- the cache lifetime where the window belongs, the target
    where the cadence does -- declares nine names and behaves like something
    nobody wrote down. Two of the nine have usable defaults in the base
    (`retries` and `headers`), and those are the two a reader would otherwise have
    to guess at.
    """
    declared = vars(SourceReleaseCollector)

    assert SourceReleaseCollector.name == COLLECTOR_NAME
    assert SourceReleaseCollector.evidence_model is SourceReleaseSnapshot
    assert SourceReleaseCollector.observation_window == SOURCE_RELEASE_OBSERVATION_WINDOW
    assert SourceReleaseCollector.timeout == SOURCE_RELEASE_TIMEOUT
    assert SourceReleaseCollector.retries == SOURCE_RELEASE_RETRIES
    assert SourceReleaseCollector.rate_limit == SOURCE_RELEASE_RATE_LIMIT
    assert SourceReleaseCollector.headers == SOURCE_RELEASE_HEADERS
    assert SourceReleaseCollector.freshness_target == SOURCE_RELEASE_FRESHNESS_TARGET
    assert SourceReleaseCollector.response_cache_ttl == SOURCE_RELEASE_CACHE_TTL
    assert {
        "name",
        "evidence_model",
        "observation_window",
        "timeout",
        "retries",
        "rate_limit",
        "headers",
        "freshness_target",
        "response_cache_ttl",
    } <= set(declared)


def test_the_freshness_target_is_the_arithmetic_open_question_7_settled() -> None:
    """`cadence x (1 + tolerated_missed_runs)`, and strictly greater than the cadence.

    PRD Open Question 7 was resolved by deriving the target rather than picking
    it, and the second half of the assertion is the rule the table exists to
    enforce: `core/freshness.py` reports stale when `observed_at < now - target`,
    so a target *equal* to the cadence makes every package read stale at exactly
    the moment its next run is due, without a single collection having failed.
    """
    assert SOURCE_RELEASE_FRESHNESS_TARGET == SOURCE_RELEASE_CADENCE * (1 + TOLERATED_MISSED_RUNS)
    assert SOURCE_RELEASE_FRESHNESS_TARGET > SOURCE_RELEASE_CADENCE


def test_the_observation_window_cannot_suppress_a_scheduled_run() -> None:
    """Shorter than the cadence, which is the property rather than the halving.

    `CPM-AD-7` makes the freshness target a window's *default*, and taking that
    literally here would be wrong in a way that is easy to miss: the window
    compares `finished_at >= now - window` inclusively, so a window at or above
    the cadence suppresses the next scheduled run, and one at the target
    suppresses every run after it for ever. What the window is actually for is
    the second run of one package inside one day, which any positive value
    shorter than the cadence still catches.
    """
    assert SOURCE_RELEASE_OBSERVATION_WINDOW < SOURCE_RELEASE_CADENCE
    assert timedelta(0) < SOURCE_RELEASE_OBSERVATION_WINDOW


def test_the_response_cache_outlives_the_cadence() -> None:
    """An entry that expired between runs would make the cache inert.

    The point of remembering a body is that the *next scheduled* collection can
    revalidate it and be answered `304`. A lifetime shorter than the cadence
    means every run is a full read against an entry that is never there, which is
    a cache that is configured, working, and permanently empty.
    """
    assert SOURCE_RELEASE_CACHE_TTL > SOURCE_RELEASE_CADENCE


def test_one_collection_fits_inside_the_inherited_soft_limit_with_room_to_spare() -> None:
    """The bound `core/transport.py` states in prose and nothing computed.

    The declared timeout is spent per connect *and* per read *and* again per
    attempt, so a collector that declared a comfortable-looking eight seconds at
    three retries would exceed the inherited 60-second soft limit (`CPM-AD-9`)
    and be killed mid-call -- after the `running` ledger row was committed and
    before any evidence was written. The limit is read from the settings module
    rather than repeated here, so lowering it there fails this rather than passing
    quietly.

    The margin is asserted rather than a bare `<`, because the call is not the
    whole task: the recorder's two writes and the evidence write happen around it,
    and a worst case that merely fitted would leave them nowhere to go.
    """
    worst_case = worst_case_call_seconds(timeout=SOURCE_RELEASE_TIMEOUT, retries=SOURCE_RELEASE_RETRIES)

    assert worst_case <= SOFT_LIMIT_SHARE * settings.CELERY_TASK_SOFT_TIME_LIMIT
    assert SOURCE_RELEASE_TIMEOUT <= MAX_TIMEOUT


def test_the_worst_case_is_the_retry_schedule_the_transport_documents() -> None:
    """The anti-vacuity half: the arithmetic is measured against a stated series.

    `core/transport.py` writes out both the formula and the series it produces,
    and says they disagree at the first term because `urllib3` treats the first
    retry's delay as zero. A computation that had dropped the backoff entirely,
    or that had started the series one term early, would still pass the bound
    above -- it would only be optimistic -- so the series is pinned here.
    """
    attempts = 1 + DEFAULT_RETRIES
    backoff = DEFAULT_BACKOFF_FACTOR * SECOND_RETRY_COEFFICIENT + DEFAULT_BACKOFF_FACTOR * THIRD_RETRY_COEFFICIENT

    assert worst_case_call_seconds(timeout=1.0, retries=DEFAULT_RETRIES) == attempts * PHASES_PER_ATTEMPT + backoff
    assert worst_case_call_seconds(timeout=1.0, retries=0) == PHASES_PER_ATTEMPT


@pytest.mark.parametrize(
    ("timeout", "retries", "backoff_factor"),
    [(0.0, 3, 0.5), (-1.0, 3, 0.5), (5.0, -1, 0.5), (5.0, 3, -0.5)],
    ids=["zero-timeout", "negative-timeout", "negative-retries", "negative-backoff"],
)
def test_a_worst_case_that_cannot_be_computed_is_refused(timeout: float, retries: int, backoff_factor: float) -> None:
    """An answer of zero would satisfy any ceiling it were compared against.

    That is the whole reason this refuses rather than answering: the one caller is
    a reconciliation against the inherited soft limit, and a reconciliation that
    passes because its input was nonsense is worse than no reconciliation at all.
    """
    with pytest.raises(ValueError, match=r"timeout|retr|backoff"):
        worst_case_call_seconds(timeout=timeout, retries=retries, backoff_factor=backoff_factor)


def test_the_declared_headers_carry_what_the_source_requires_and_nothing_conditional() -> None:
    """Headers reach the socket only through the base (`CPM-AD-20`, `CPM-AD-27`).

    What a collector may declare is what its source expects of every request; what
    it may not declare is a validator, which the base composes from the response
    cache. The base refuses a conditional declaration at construction, so this is
    the earlier statement of the same rule: nothing in the declaration is one.
    """
    lowered = {name.lower() for name in SOURCE_RELEASE_HEADERS}

    assert "user-agent" in lowered
    assert lowered.isdisjoint({header.lower() for header in CONDITIONAL_HEADERS})


def test_the_user_agent_says_which_deployment_is_calling() -> None:
    """A source owner deciding whether to block a caller needs more than a product name.

    GitHub requires a `User-Agent` and asks that it identify the caller. A bare
    literal identifies the *product*, which tells neither an operator reading an
    access log nor a source owner reading their own which deployment issued a
    call, or how to reach whoever runs it. So it carries the distribution name,
    the version the running build reports, and a way to get in touch.
    """
    assert USER_AGENT.startswith(f"{DISTRIBUTION_NAME}/")
    assert distribution_version() in USER_AGENT
    assert PROJECT_URL in USER_AGENT
    assert SOURCE_RELEASE_HEADERS["User-Agent"] == USER_AGENT


def test_the_distribution_name_is_one_that_is_actually_installed() -> None:
    """`DISTRIBUTION_NAME` is a metadata key, and a wrong key fails silently forever.

    `distribution_version()` looks the installed metadata up by `DISTRIBUTION_NAME`
    and catches `PackageNotFoundError`, so a spelling that drifts from
    `pyproject.toml`'s `[project] name` -- a rename that misses this constant, a
    typo in the next one -- does not raise. It reports `UNKNOWN_VERSION` on every
    outbound request to GitHub, PyPI, anaconda.org, OSV and KEV, indefinitely, and
    every other assertion in this module stays green: the case above is satisfied
    by `"<anything>/0.0.0 (+<url>)"` just as well as by the real identity.

    So the lookup is made once here *without* the catch. `importlib.metadata.version`
    raises when the name is not installed, and that raise is the whole guard --
    mirroring `tests/unit/test_package_version.py`, which pins `django_service`'s
    sibling lookup the same way. The equality and the `!=` are what make the
    failure legible rather than merely present.

    A unit test: it reads the import system's own metadata. No database, no
    network.
    """
    installed = importlib.metadata.version(DISTRIBUTION_NAME)

    assert distribution_version() == installed
    assert distribution_version() != UNKNOWN_VERSION


def test_the_version_falls_back_when_the_distribution_is_not_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bare checkout has no distribution metadata, and that is a real state.

    `tests/unit/test_package_version.py` makes the same argument for
    `django_service`'s own lookup: the branch is reachable -- a source tree
    imported without an editable install has no metadata -- so it is exercised
    rather than pragma'd out, which `tests/unit/test_coverage_policy.py` bans
    anyway.

    Patched on `collectors/agent.py`, which is where the lookup lives since
    `CPM-CURRENCY-S02` moved the shared `User-Agent` identity there: this module
    still imports `distribution_version` from `source_release`, which re-exports
    it, and the re-export is what the assertion below reaches through.
    """

    def _missing(name: str) -> str:
        raise agent.PackageNotFoundError(name)

    monkeypatch.setattr(agent, "version", _missing)

    assert distribution_version() == UNKNOWN_VERSION


def test_the_collector_is_constructed_from_its_declarations_alone() -> None:
    """The base's nine refusals, run against the real class rather than a fixture.

    Every one of them raises `CollectorConfigurationError` at construction, so a
    class that declared a string where an interval belongs, or omitted the rate
    limit, fails here rather than in a worker with a ledger row already
    `running`. Constructing it is the whole assertion.
    """
    collector = SourceReleaseCollector(clock=_stopped_clock())

    try:
        assert collector.request_cost == 1 + SOURCE_RELEASE_RETRIES
        assert SOURCE_RELEASE_RATE_LIMIT.calls >= collector.request_cost
    finally:
        collector.close()


def test_without_a_token_the_instance_sends_the_class_declarations_exactly() -> None:
    """`CPM-OPERATE-S05`'s `No token` row: nothing about an unauthenticated collector changed.

    The instance's `headers` and `rate_limit` are set in `__init__` from the
    setting, so with the setting empty they have to be *equal* to the class's
    declarations and carry no `Authorization` -- which is what keeps every
    declaration test above, and every request a deployment without a token
    sends, exactly as they were.
    """
    with override_settings(**{GITHUB_TOKEN_SETTING: ""}):
        collector = SourceReleaseCollector(clock=_stopped_clock())

    try:
        assert dict(collector.headers) == dict(SOURCE_RELEASE_HEADERS)
        assert collector.rate_limit == SOURCE_RELEASE_RATE_LIMIT
        assert not any(name.lower() == AUTHORIZATION_HEADER.lower() for name in collector.headers)
    finally:
        collector.close()


def test_with_a_token_the_instance_declares_the_bearer_and_the_authenticated_allowance() -> None:
    """`CPM-OPERATE-S05`'s `Token, API` row, at construction.

    The instance carries `Authorization: Bearer <token>` on top of the declared
    headers and GitHub's authenticated core allowance in place of the
    unauthenticated one; the *class* still declares the unauthenticated pair,
    because that is the honest statement about a collector nobody has given a
    credential, and every audit reads the class.
    """
    with override_settings(**{GITHUB_TOKEN_SETTING: A_TOKEN}):
        collector = SourceReleaseCollector(clock=_stopped_clock())

    try:
        assert dict(collector.headers) == {**SOURCE_RELEASE_HEADERS, AUTHORIZATION_HEADER: f"{BEARER_SCHEME} {A_TOKEN}"}
        assert collector.rate_limit == AUTHENTICATED_CORE_ALLOWANCE
        assert collector.rate_limit.calls >= collector.request_cost
    finally:
        collector.close()

    assert SourceReleaseCollector.headers == SOURCE_RELEASE_HEADERS
    assert SourceReleaseCollector.rate_limit == SOURCE_RELEASE_RATE_LIMIT


def test_a_malformed_token_refuses_construction_without_echoing_it() -> None:
    """The base never sees a header built from a value the fault rule refuses."""
    with (
        override_settings(**{GITHUB_TOKEN_SETTING: f"{A_TOKEN}\nX-Injected: yes"}),
        pytest.raises(ImproperlyConfigured) as refused,
    ):
        SourceReleaseCollector(clock=_stopped_clock())

    assert GITHUB_TOKEN_SETTING in str(refused.value)
    assert A_TOKEN[:8] not in str(refused.value)


def test_the_task_name_routes_to_the_collect_queue() -> None:
    """`cpm.collect.*` is what puts external I/O on the `collect` queue (`R-11`).

    Asserted through `queue_for` rather than against the literal `"collect"`, so
    the claim is about the resolver `core/queues.py` owns rather than about a
    string this module spells a second time.
    """
    assert queue_for(COLLECT_SOURCE_RELEASE_TASK_NAME) == Queue.COLLECT


# ---------------------------------------------------------------------------
# The locators.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "repository_url",
    [
        A_REPOSITORY,
        f"{A_REPOSITORY}/",
        f"{A_REPOSITORY}.git",
        "http://github.com/conda-forge/numpy-feedstock",
        "https://www.github.com/conda-forge/numpy-feedstock",
        "https://GitHub.com/conda-forge/numpy-feedstock",
        "https://github.com/Conda-Forge/NumPy-Feedstock",
        f"  {A_REPOSITORY}  ",
    ],
    ids=[
        "plain",
        "trailing-slash",
        "clone-url",
        "http",
        "www",
        "mixed-case-host",
        "mixed-case-segments",
        "surrounded-by-space",
    ],
)
def test_every_spelling_of_one_repository_reaches_one_locator(repository_url: str) -> None:
    """The same repository, however a resolution happened to store it.

    `Package.source_repository_url` is whatever a resolver wrote, and resolvers
    read registries: a clone URL, a trailing slash, a `www.` host and a
    differently-cased owner are all the same repository to the source, and reading
    them as several would mean several cache keys, several observation histories
    and several answers to "when did this last release".
    """
    assert releases_locator(repository_url) == THE_LOCATOR


def test_the_locator_asks_the_api_host_for_one_page() -> None:
    """The anti-vacuity half of the case above: the expected locator is the right one.

    Every case above compares against one constant, so a constant that was wrong
    would make all eight agree about the wrong answer. This one reconciles it
    against the module's own declarations instead.
    """
    assert THE_LOCATOR.startswith(f"https://{GITHUB_API_HOST}/")
    assert THE_LOCATOR.endswith(f"per_page={RELEASES_PER_PAGE}")


def test_the_tags_locator_asks_the_same_repository_a_different_question() -> None:
    """AC 1's "or tag": one repository, two endpoints, one parse of the stored URL.

    Both locators are built from the same refusals and the same normalisation, so
    a URL either produces both or produces neither -- which is what stops the
    fallback reading a repository the primary read would have refused.
    """
    assert tags_locator(A_REPOSITORY) == THE_TAGS_LOCATOR
    assert THE_TAGS_LOCATOR.endswith(f"per_page={TAGS_PER_PAGE}")
    assert tags_locator("https://github.com/Conda-Forge/NumPy-Feedstock.git") == THE_TAGS_LOCATOR


@pytest.mark.parametrize(
    "repository_url",
    [
        "",
        "   ",
        "git@github.com:conda-forge/numpy-feedstock.git",
        "file:///etc/passwd",
        "ftp://github.com/conda-forge/numpy-feedstock",
        "https:///conda-forge/numpy-feedstock",
        "https://gitlab.com/an-owner/a-repository",
        "https://example.invalid/an-owner/a-repository",
        "https://github.com/conda-forge",
        "https://github.com/conda-forge/numpy-feedstock/tree/main",
        "https://github.com/conda-forge/.git",
        "https://github.com/an-owner/..",
        "https://github.com/../numpy-feedstock",
        "https://github.com/an-owner/..git",
        "https://[github.com/an-owner/a-repository",
        "https://github.com:notaport/an-owner/a-repository",
    ],
    ids=[
        "blank",
        "whitespace",
        "scp-syntax",
        "file-scheme",
        "ftp-scheme",
        "no-host",
        "another-host",
        "unknown-host",
        "account-only",
        "path-inside-a-repository",
        "nothing-but-a-suffix",
        "parent-as-repository",
        "parent-as-owner",
        "parent-behind-a-git-suffix",
        "unclosed-ipv6-bracket",
        "port-that-is-not-a-number",
    ],
)
@pytest.mark.parametrize("build", [releases_locator, tags_locator], ids=["releases", "tags"])
def test_a_url_that_is_not_a_readable_repository_is_refused(build: Any, repository_url: str) -> None:
    """Refused rather than guessed at, and refused where the answer already exists.

    Every one of these could be turned into *some* locator by enough string
    surgery, and every such locator would be a request aimed at something nobody
    established. `CPM-FR-1` says a resolution that cannot establish a mapping
    records nothing rather than a guess; this is the reading half of the same
    rule. Both locators are run against every case, because the fallback must not
    be a second, laxer door into the same host.

    Two of them are the order-dependent ones. `..git` becomes `..` only *after*
    the clone suffix is stripped, so a traversal check that ran first would pass
    it; and a malformed authority makes `urlsplit` raise a bare `ValueError`,
    which would escape this module's stated contract that every refusal here is a
    `SourceLocatorError`.
    """
    with pytest.raises(SourceLocatorError):
        build(repository_url)


def test_the_locator_percent_encodes_what_it_was_handed() -> None:
    """A stored URL is data a registry supplied, and a path segment is not a template.

    A segment carrying a space, a slash or a percent sign would otherwise reshape
    the locator's path or bolt a second query onto it -- against the one host this
    collector is allowed to ask. The transport's scheme allowlist does not help
    here: the request would be perfectly well-formed and aimed at the wrong
    resource. The two relative references are refused rather than encoded, which
    is the case above; the rest are encoded, which is this one.
    """
    locator = releases_locator("https://github.com/an owner/a%2Frepo")

    assert "%20" in locator
    assert "an owner" not in locator
    assert locator.count("?") == 1
    assert locator.endswith(f"per_page={RELEASES_PER_PAGE}")


def test_the_locator_refuses_a_scheme_the_transport_would_refuse() -> None:
    """The refusal is the transport's rule, stated where the answer already exists.

    `core/transport.py` would refuse a `file://` locator at the call, after the
    ledger row is `running` and the allowance is spent. Refusing it here costs
    neither, and the two agree because this reads `ALLOWED_SCHEMES` rather than
    restating it.
    """
    assert "file" not in ALLOWED_SCHEMES

    with pytest.raises(SourceLocatorError, match="scheme"):
        releases_locator("file://github.com/an-owner/a-repository")


def test_every_recognised_host_is_actually_recognised() -> None:
    """The anti-vacuity half of the refusals: the host table is reachable.

    A refusal table that had come to refuse everything would pass every case
    above and would fail no case anywhere -- the collector would simply never
    observe anything, which is the failure mode this whole story exists to make
    visible.
    """
    for host in GITHUB_WEB_HOSTS:
        assert releases_locator(f"https://{host}/an-owner/a-repository").startswith(f"https://{GITHUB_API_HOST}/")


# ---------------------------------------------------------------------------
# The release document (`release_facts`).
# ---------------------------------------------------------------------------


def test_a_document_naming_releases_records_the_newest_published_one() -> None:
    """AC 1: the latest release, its date, and the activity signal beside them.

    The source lists newest first, and the newest here is a prerelease -- which is
    the case the two instants exist for. `released_at` is the newest *published*
    release, because that is what a currency comparison is made against
    (`CPM-FR-16`); `last_activity_at` is the prerelease's, because the repository
    plainly did something more recently than it released.
    """
    facts = release_facts(
        _document(
            _published("v3.0.0rc1", THIRD_PUBLISHED, prerelease=True),
            _published("v2.1.0", SECOND_PUBLISHED),
            _published("v2.0.0", FIRST_PUBLISHED),
        ),
        source=A_SOURCE,
    )

    assert facts.state == OutcomeState.OK
    assert facts.latest_version == "v2.1.0"
    assert facts.released_at == SECOND_INSTANT
    assert facts.last_activity_at == THIRD_INSTANT
    assert facts.releases_seen == THREE_RELEASES
    assert facts.detail == ""
    assert facts.source == A_SOURCE


def test_a_repository_that_publishes_no_releases_records_that_fact() -> None:
    """The empty release list, which is what the tag fallback hangs off.

    An empty list is an *answer*: the source was read, and what it said is that
    there is no release. Returning no rows for it would make the base write an
    `error` row and finalize `failed`, and a package with no evidence reads as
    unobserved and ages into stale -- which is "reporting the package stale"
    wearing a different word. `releases_seen` is zero rather than missing, because
    this run counted, and it is what the collector then reads to decide whether to
    ask about tags.
    """
    facts = release_facts("[]", source=A_SOURCE)

    assert facts.state == OutcomeState.NOT_FOUND
    assert facts.latest_version == ""
    assert facts.released_at is None
    assert facts.last_activity_at is None
    assert facts.releases_seen == 0
    assert facts.detail == NO_RELEASES_DETAIL


@pytest.mark.parametrize("flag", [DRAFT_FIELD, PRERELEASE_FIELD], ids=["draft", "prerelease"])
def test_a_repository_with_only_rehearsals_has_no_latest_release_and_is_still_active(flag: str) -> None:
    """A second way to have no latest release, and it is not the same fact.

    A project whose every listed entry is a draft or a prerelease has published
    nothing a currency comparison can use, and is obviously not dormant. Both
    halves are recorded: `not_found` with no version, and the activity signal and
    the count that say the repository is alive -- which is what stops a policy
    reading "no release" as "no project". `releases_seen` is *not* zero, so the
    tag fallback does not fire: the repository does publish releases, and this run
    could not read one of them as its latest.
    """
    facts = release_facts(
        _document(
            _published("v1.0.0rc2", SECOND_PUBLISHED, **{flag: True}),
            _published("v1.0.0rc1", FIRST_PUBLISHED, **{flag: True}),
        ),
        source=A_SOURCE,
    )

    assert facts.state == OutcomeState.NOT_FOUND
    assert facts.latest_version == ""
    assert facts.released_at is None
    assert facts.last_activity_at == SECOND_INSTANT
    assert facts.releases_seen == TWO_RELEASES
    assert facts.detail.startswith(NO_LATEST_RELEASE_DETAIL)
    assert f"{TWO_RELEASES} {UNPUBLISHED_RELEASE}" in facts.detail


def test_the_reason_a_document_names_no_latest_release_is_the_reason_it_actually_had() -> None:
    """A false reason in an append-only table is as permanent as a false fact.

    Three different things stop an entry being the latest release, and a row that
    said "all drafts and prereleases" about a document of undated ones would be
    recording something nobody could later correct. The reasons are counted and
    only the ones that occurred are named.
    """
    facts = release_facts(
        _document(
            _published("v3.0.0", THIRD_PUBLISHED, draft=True),
            _published("", SECOND_PUBLISHED),
            _release("v1.0.0", None),
        ),
        source=A_SOURCE,
    )

    assert facts.state == OutcomeState.NOT_FOUND
    assert facts.detail.startswith(NO_LATEST_RELEASE_DETAIL)
    assert f"1 {UNPUBLISHED_RELEASE}" in facts.detail
    assert f"1 {UNTAGGED_RELEASE}" in facts.detail
    assert f"1 {UNDATED_RELEASE}" in facts.detail


def test_a_reason_that_did_not_occur_is_not_named() -> None:
    """The anti-vacuity half: the detail is a report rather than a list of headings.

    A `detail` that named all three reasons whatever happened would pass the case
    above and would say nothing. This is the same document minus two of its
    entries.
    """
    facts = release_facts(_document(_published("v3.0.0", THIRD_PUBLISHED, draft=True)), source=A_SOURCE)

    assert f"1 {UNPUBLISHED_RELEASE}" in facts.detail
    assert UNTAGGED_RELEASE not in facts.detail
    assert UNDATED_RELEASE not in facts.detail


def test_the_latest_release_is_the_newest_rather_than_the_first_listed() -> None:
    """The source's order is a convention and the instants are the fact.

    A document listed out of order -- which a source is entitled to serve, and
    which a `per_page` boundary can produce -- would otherwise make the first
    entry the answer. The comparison is over the publication instants, so the
    order the entries arrive in changes nothing.
    """
    facts = release_facts(
        _document(
            _published("v1.0.0", FIRST_PUBLISHED),
            _published("v3.0.0", THIRD_PUBLISHED),
            _published("v2.0.0", SECOND_PUBLISHED),
        ),
        source=A_SOURCE,
    )

    assert facts.latest_version == "v3.0.0"
    assert facts.released_at == THIRD_INSTANT


def test_two_releases_at_one_instant_resolve_to_the_one_listed_first() -> None:
    """Ties are decided here rather than by whichever order a reader iterates in.

    Two releases can share a publication instant, and an answer that depended on
    iteration order would be a different answer on a document served in a
    different order. The source lists newest first, so the first of a tie is the
    one it considers newest.
    """
    facts = release_facts(
        _document(_published("v2.0.0", SECOND_PUBLISHED), _published("v1.9.9", SECOND_PUBLISHED)),
        source=A_SOURCE,
    )

    assert facts.latest_version == "v2.0.0"


@pytest.mark.parametrize(
    "published_at",
    [OMITTED, None, "", "   ", "not-a-date", "2026-04-11T14:00:00"],
    ids=["omitted", "explicit-null", "empty", "whitespace", "unparseable", "naive"],
)
def test_a_release_whose_date_is_unusable_is_not_the_latest_release(published_at: str | object | None) -> None:
    """Missing rather than fatal, and never invented.

    An entry this collector cannot date cannot be compared against another, so it
    is not the latest release and it does not move the activity signal. Refusing
    the document for it would turn one odd row into a failed observation of the
    whole repository; assuming an instant for it -- the naive case in particular
    -- would write a date shifted by a guess into a row nothing may correct
    (`CPM-AD-26`). The first two cases are different documents rather than two
    spellings of one: an omitted field and an explicit `null` are both things this
    source really sends.
    """
    facts = release_facts(
        _document(_release("v9.9.9", published_at), _published("v2.0.0", SECOND_PUBLISHED)),
        source=A_SOURCE,
    )

    assert facts.latest_version == "v2.0.0"
    assert facts.released_at == SECOND_INSTANT
    assert facts.last_activity_at == SECOND_INSTANT
    assert facts.releases_seen == TWO_RELEASES


def test_an_untagged_release_is_not_the_latest_release() -> None:
    """The tag is the version a currency comparison is made against (`CPM-FR-16`).

    An entry with no usable tag has no version to record, and recording a blank
    one on a determinate row is refused by the table anyway -- so it is not the
    latest release, and the entry below it is.
    """
    facts = release_facts(
        _document(_published("   ", THIRD_PUBLISHED), _published("v2.0.0", SECOND_PUBLISHED)),
        source=A_SOURCE,
    )

    assert facts.latest_version == "v2.0.0"
    assert facts.last_activity_at == THIRD_INSTANT


def test_an_unpublished_release_still_moves_the_activity_signal() -> None:
    """A draft says when the project did something, which is what the signal is.

    `ACTIVITY_FIELDS` holds both date fields: a published release carries
    `published_at` and an entry that was never published carries only
    `created_at`, and reading the first alone would report a repository mid-release
    as dormant.
    """
    facts = release_facts(
        _document(
            _release("v3.0.0", None, draft=True, created_at=THIRD_PUBLISHED),
            _published("v2.0.0", SECOND_PUBLISHED),
        ),
        source=A_SOURCE,
    )

    assert set(ACTIVITY_FIELDS) == {PUBLISHED_AT_FIELD, "created_at"}
    assert facts.latest_version == "v2.0.0"
    assert facts.last_activity_at == THIRD_INSTANT


def test_the_activity_signal_is_the_later_of_an_entrys_two_instants() -> None:
    """The maximum rather than the first available, and the difference is real.

    A release edited after publication carries a `created_at` later than its
    `published_at`. Taking the first field that parses would report the earlier
    instant and understate a live project's activity -- which is the direction
    that matters, because the signal exists to tell a project that stopped from
    one that did not.
    """
    facts = release_facts(
        _document(_published("v2.0.0", SECOND_PUBLISHED, created_at=THIRD_PUBLISHED)),
        source=A_SOURCE,
    )

    assert facts.released_at == SECOND_INSTANT
    assert facts.last_activity_at == THIRD_INSTANT


@pytest.mark.parametrize(
    "body",
    ["", "not json", "{}", '{"releases": []}', "[1, 2]", '["v1.0.0"]'],
    ids=["empty", "not-json", "object", "wrapped", "numbers", "strings"],
)
def test_a_document_that_is_not_a_list_of_entries_is_refused(body: str) -> None:
    """Refused rather than read for whatever still parses.

    A source whose shape has changed is a source this collector no longer
    understands, and reading the entries it happens to recognise would record a
    latest release chosen from a fragment -- permanently, in a log nothing may
    correct. The base answers the refusal by writing an `error` row and
    re-raising, so the run is on the record either way.
    """
    with pytest.raises(SourceReleaseDocumentError):
        release_facts(body, source=A_SOURCE)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        (DRAFT_FIELD, "false"),
        (PRERELEASE_FIELD, 1),
        (TAG_NAME_FIELD, 12),
        (PUBLISHED_AT_FIELD, 1_760_000_000),
        ("created_at", {"at": SECOND_PUBLISHED}),
    ],
    ids=["draft-as-text", "prerelease-as-number", "tag-as-number", "date-as-number", "date-as-object"],
)
def test_a_release_whose_fields_are_mistyped_is_refused(field: str, value: object) -> None:
    """One rule for every field this collector reads, rather than one per field.

    A truthiness test on a flag would be worse than a refusal -- the string
    `"false"` is truthy, so a flag arriving as text would silently exclude every
    release and the repository would read as publishing nothing, a clean-looking
    `not_found` produced by a bug. The dates are held to the same rule and for the
    same reason: a value of the wrong *type* is the document's shape changing,
    while a date that is a string this collector cannot read is one entry's
    problem and is treated as missing.
    """
    entry = _published("v2.0.0", SECOND_PUBLISHED)
    entry[field] = value

    with pytest.raises(SourceReleaseDocumentError):
        release_facts(_document(entry), source=A_SOURCE)


def test_a_version_wider_than_its_column_is_refused_rather_than_truncated() -> None:
    """`R-5`'s parity gap, closed where the value enters.

    `max_length` is a constraint on PostgreSQL and a suggestion on SQLite, so an
    over-long tag is a stored row on a developer's machine and a failed run in the
    gate. Truncating it would be worse than either: a stored version that is not
    the version the source published is a comparison that will be wrong for ever.
    """
    width = SourceReleaseSnapshot._meta.get_field("latest_version").max_length  # noqa: SLF001 - Django's own public-by-convention API
    assert width is not None

    with pytest.raises(SourceReleaseDocumentError, match="characters"):
        release_facts(_document(_published("v" * (width + 1), SECOND_PUBLISHED)), source=A_SOURCE)


def test_a_document_larger_than_the_ceiling_is_refused_before_it_is_decoded() -> None:
    """A worker has a memory budget and a sixty-second soft limit (`CPM-AD-9`).

    The ceiling is not a guess at how big a release page is -- it is two orders of
    magnitude above one. It is a refusal to be surprised: an unbounded body
    decoded inside a worker holding several collections is the failure that takes
    the whole worker with it, and the check has to happen before `json.loads`
    rather than after.
    """
    with pytest.raises(SourceReleaseDocumentError, match="characters"):
        release_facts("[" + " " * MAX_DOCUMENT_CHARACTERS, source=A_SOURCE)


def test_a_deeply_nested_document_is_refused_rather_than_crashing_the_worker() -> None:
    """`json.loads` recurses per level, and raises `RecursionError` rather than a decode error.

    Caught beside the decode error because an uncaught one escapes as a crash
    naming this module's own stack rather than the source that caused it -- and
    because a source can choose the nesting depth of what it serves.
    """
    with pytest.raises(SourceReleaseDocumentError):
        release_facts("[" * 200_000, source=A_SOURCE)


def test_the_refusals_name_the_source_they_are_about() -> None:
    """A failure an operator can act on names what was being read.

    The detail these messages become is the ledger row's, and a row saying only
    that a document was unreadable sends a reader to the code rather than to the
    source.
    """
    with pytest.raises(SourceReleaseDocumentError, match=re.escape(A_SOURCE)):
        release_facts("not json", source=A_SOURCE)


# ---------------------------------------------------------------------------
# The tag document (`tag_facts`).
# ---------------------------------------------------------------------------


def test_a_tagged_repository_records_its_newest_tag_and_no_date() -> None:
    """AC 1's "or tag": a version, honestly dated by nothing.

    Publishing a GitHub Release is a deliberate act many projects never perform.
    Reading releases alone would record `not_found` for every one of them, which
    is a false fact in a log nothing may correct. What a tag cannot supply is a
    date -- the endpoint carries none -- so the row names the version, leaves both
    instants NULL, and says in its own `detail` that the ordering is the source's
    rather than this collector's.

    `releases_seen` stays zero, because the fallback is reached only from an empty
    release list and that is still the fact the column records.
    """
    facts = tag_facts(_document(_tag("v2.1.0"), _tag("v2.0.0")), source=THE_TAGS_LOCATOR)

    assert facts.state == OutcomeState.OK
    assert facts.latest_version == "v2.1.0"
    assert facts.released_at is None
    assert facts.last_activity_at is None
    assert facts.releases_seen == 0
    assert facts.detail == TAGGED_DETAIL
    assert facts.source == THE_TAGS_LOCATOR


def test_a_repository_with_neither_releases_nor_tags_records_that_fact() -> None:
    """AC 2, at the point it is actually reached.

    Once the fallback exists, "a repository that publishes no releases at all" is
    a narrower claim than "the release list was empty": it is a repository with
    nothing tagged either. That is the row -- `not_found`, no version, and
    `releases_seen` of zero -- and it is what a currency policy reads instead of
    calling the package stale against a source that never released anything.
    """
    facts = tag_facts("[]", source=THE_TAGS_LOCATOR)

    assert facts.state == OutcomeState.NOT_FOUND
    assert facts.latest_version == ""
    assert facts.releases_seen == 0
    assert facts.detail == NO_TAGS_DETAIL


def test_a_tag_with_no_usable_name_is_passed_over_rather_than_recorded() -> None:
    """A blank name is not a version, and the next entry is the answer.

    Recording it would put an empty `latest_version` on a determinate row, which
    the table's own constraint refuses -- so the alternative to passing over it is
    an `IntegrityError` several frames from the document that caused it.
    """
    facts = tag_facts(_document(_tag("  "), _tag("v1.4.0")), source=THE_TAGS_LOCATOR)

    assert facts.latest_version == "v1.4.0"


@pytest.mark.parametrize("value", [12, {"name": "v1"}, [1]], ids=["number", "object", "list"])
def test_a_tag_whose_name_is_mistyped_is_refused(value: object) -> None:
    """The tag document is held to the shape rule the release document is.

    A source whose tag list has changed shape is refused rather than read for
    whatever still parses, which is the same argument and the same failure mode.
    """
    with pytest.raises(SourceReleaseDocumentError):
        tag_facts(_document(_tag(value)), source=THE_TAGS_LOCATOR)


def test_a_tag_wider_than_its_column_is_refused() -> None:
    """The bound the release path carries, on the path that reaches the same column.

    A refusal that covered only one of the two writers would be a column bound
    enforced on half the rows that reach it.
    """
    width = SourceReleaseSnapshot._meta.get_field("latest_version").max_length  # noqa: SLF001 - Django's own public-by-convention API
    assert width is not None

    with pytest.raises(SourceReleaseDocumentError, match="characters"):
        tag_facts(_document(_tag("v" * (width + 1))), source=THE_TAGS_LOCATOR)


# ---------------------------------------------------------------------------
# The rows (`translate`, `sentinel_evidence`, `__str__`).
# ---------------------------------------------------------------------------


def test_a_translated_row_carries_the_facts_and_the_instant_it_was_handed() -> None:
    """One row, stamped with *this* observation's moment (`CPM-AD-7`).

    The base refuses a row stamped with anything else, because `bulk_create` does
    not call `save()` and would otherwise walk around the naive-instant refusal
    `core/models.py` calls "the one place every evidence write passes through".
    The locator travels onto the row too, which is what lets an append-only
    history say which repository -- and which endpoint -- each observation came
    from.
    """
    collector = SourceReleaseCollector(clock=_stopped_clock())
    try:
        rows = collector.translate(
            _payload(_document(_published("v2.1.0", SECOND_PUBLISHED))),
            package_id=A_PACKAGE,
            observed_at=FIXED_INSTANT,
        )
    finally:
        collector.close()

    assert len(rows) == 1
    row = rows[0]
    assert isinstance(row, SourceReleaseSnapshot)
    assert row.observed_at == FIXED_INSTANT
    assert row.package_id == A_PACKAGE
    assert row.source == A_SOURCE
    assert row.state == OutcomeState.OK.value
    assert row.latest_version == "v2.1.0"
    assert row.released_at == SECOND_INSTANT
    assert row.releases_seen == 1


def test_translation_never_returns_nothing() -> None:
    """An empty translation is a failure to the base, and this document is not one.

    The base reads a translation that returns no rows as a parser that no longer
    matches its source: it writes an `error` row and finalizes `failed`. A
    repository that lists releases none of which can be its latest is not that, so
    it comes back as a row.
    """
    collector = SourceReleaseCollector(clock=_stopped_clock())
    try:
        rows = collector.translate(
            _payload(_document(_published("v1.0.0rc1", SECOND_PUBLISHED, prerelease=True))),
            package_id=A_PACKAGE,
            observed_at=FIXED_INSTANT,
        )
    finally:
        collector.close()

    assert len(rows) == 1
    assert rows[0].state == OutcomeState.NOT_FOUND.value  # type: ignore[attr-defined]


@pytest.mark.parametrize("state", [OutcomeState.ERROR, OutcomeState.NOT_FOUND], ids=lambda state: state.value)
def test_a_sentinel_row_carries_the_state_and_counts_nothing(state: OutcomeState) -> None:
    """The base checks the first half; the second is this collector's own rule.

    `releases_seen` is NULL rather than zero, and the distinction is the one the
    column exists for: a sentinel row was written for a call that produced no
    document, so nothing was counted -- which is a different statement from a
    document that listed none, and the two have to stay apart on the surface that
    will compare them.
    """
    collector = SourceReleaseCollector(clock=_stopped_clock())
    try:
        row = collector.sentinel_evidence(
            state=state,
            package_id=A_PACKAGE,
            observed_at=FIXED_INSTANT,
            detail="a reason",
        )
    finally:
        collector.close()

    assert isinstance(row, SourceReleaseSnapshot)
    assert row.state == state.value
    assert row.observed_at == FIXED_INSTANT
    assert row.latest_version == ""
    assert row.released_at is None
    assert row.last_activity_at is None
    assert row.releases_seen is None
    assert row.detail.startswith("a reason")


def test_an_absent_row_records_what_a_404_can_and_cannot_prove() -> None:
    """A `404` from an unauthenticated read is not proof of absence.

    GitHub answers `404` identically for an absent repository, a private one, one
    that has moved and one that is blocked -- deliberately, so an unauthenticated
    reader cannot enumerate private repositories. This collector sends no
    credential, so it cannot tell them apart and does not pretend to: the state
    stays `not_found`, because that is what the source said, and the row carries
    the caveat a reader needs to weigh it.
    """
    collector = SourceReleaseCollector(clock=_stopped_clock())
    try:
        absent = collector.sentinel_evidence(
            state=OutcomeState.NOT_FOUND,
            package_id=A_PACKAGE,
            observed_at=FIXED_INSTANT,
            detail="a reason",
        )
        failed = collector.sentinel_evidence(
            state=OutcomeState.ERROR,
            package_id=A_PACKAGE,
            observed_at=FIXED_INSTANT,
            detail="a reason",
        )
    finally:
        collector.close()

    assert ABSENT_CAVEAT in absent.detail  # type: ignore[attr-defined]
    assert failed.detail == "a reason"  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    "state",
    [OutcomeState.OK, OutcomeState.UNKNOWN, OutcomeState.NOT_APPLICABLE],
    ids=lambda state: state.value,
)
def test_a_sentinel_state_this_collector_has_no_row_for_is_refused(state: OutcomeState) -> None:
    """Refused at the call rather than at the insert, which is where it would land.

    The base calls this with two states and documents that it does, so the check
    is about the method being public. The alternative failure is the bad one: a
    row carrying `ok` and no version is refused by the table's own constraint, as
    an `IntegrityError` naming a constraint several frames from the call that was
    wrong.
    """
    collector = SourceReleaseCollector(clock=_stopped_clock())
    try:
        with pytest.raises(CollectorConfigurationError, match=state.value):
            collector.sentinel_evidence(
                state=state,
                package_id=A_PACKAGE,
                observed_at=FIXED_INSTANT,
                detail="a reason",
            )
    finally:
        collector.close()


def test_an_unsaved_snapshot_renders_its_absences_rather_than_raising() -> None:
    """A `__str__` that raises is what a failure message would have been.

    The related object of an unsaved instance raises `RelatedObjectDoesNotExist`,
    and the two places a half-built row is most likely to be rendered are a
    debugger and a traceback -- so reaching for `self.package` there turns a
    diagnosable failure into a second, unrelated one. Every sibling model pins
    this the same way.
    """
    rendered = str(SourceReleaseSnapshot())

    assert "no release" in rendered
    assert "no package" in rendered
    assert "never" in rendered


def test_a_saved_snapshots_rendering_names_what_it_observed() -> None:
    """The anti-vacuity half: the placeholders are placeholders rather than the answer.

    A `__str__` that always said "no release for no package" would pass the case
    above. This one is built rather than saved, which is enough: the rendering
    reads attributes, and `package_id` is one of them.
    """
    rendered = str(
        SourceReleaseSnapshot(
            observed_at=FIXED_INSTANT,
            package_id=A_PACKAGE,
            state=OutcomeState.OK.value,
            latest_version="v2.1.0",
        ),
    )

    assert "v2.1.0" in rendered
    assert str(A_PACKAGE) in rendered
    assert OutcomeState.OK.value in rendered
    assert FIXED_INSTANT.isoformat() in rendered


# ---------------------------------------------------------------------------
# The module's own source.
# ---------------------------------------------------------------------------


def test_the_collector_module_writes_no_row_of_any_kind() -> None:
    """`CPM-AD-7` and `CPM-AD-14`: this collector reads `identity` and writes evidence.

    Structural because nothing at run time would report it. A `Package.objects.create`
    here would work -- a shell would appear, the run would succeed -- and the
    product would have acquired a second creator of governed reference data
    beside resolution, which is what `CPM-AD-14` exists to forbid. The evidence
    rows are the base's to insert for the same class of reason: a row written
    here would be unstamped, unchecked against the declared model, and invisible
    to the base's own checks.
    """
    tree = parse(_release_module())
    written = sorted(
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and dotted_name(node.func).rpartition(".")[2] in WRITE_METHODS
    )

    assert written == [], f"the collector reaches a table directly at lines {written}"


def test_the_collector_module_reads_the_package_row_it_is_allowed_to_read() -> None:
    """The anti-vacuity half: a module that named no model would sweep clean.

    `CPM-AD-7` is not "a collector touches no model" -- it is "a collector writes
    its own evidence table and reads only `identity`", and only this half says
    the permitted read is actually being made.
    """
    named = {node.id for node in ast.walk(parse(_release_module())) if isinstance(node, ast.Name)}

    assert PACKAGE_MODEL_NAME in named


def test_the_collector_module_opens_no_transaction_of_its_own() -> None:
    """The per-package transaction is the base's, around the evidence write.

    `CPM-AD-23` puts the atomic unit at one package, and on the per-package path
    that is exactly what `Collector._write_evidence` opens. A second one here
    would either nest pointlessly or -- if it were opened around the recorder --
    take the `running` ledger row away from a killed worker, which is the
    constraint `CPM-EVIDENCE-S03` deferred and `CPM-EVIDENCE-S05` made
    enforceable.
    """
    tree = parse(_release_module())
    opened = sorted(
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and dotted_name(node.func).endswith("transaction.atomic")
    )

    assert opened == []
