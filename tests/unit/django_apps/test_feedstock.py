"""What the feedstock collector declares, what a locator is, what a document means, and which question it asks.

`CPM-FR-9` is several questions and only the last needs a run. Turning a
feedstock name into a repository, reading a repository document, reading a recipe
the way conda-forge writes it, deciding which of the staged-recipes queue's
answers is *this* package's, and deciding from what resolution recorded which of
the two branches a package takes -- all are pure functions of data, and all are
here with no database, no socket and no clock (`CPM-AD-27`). What needs a run --
the rows, the ledger, both branches end to end, AC 1's absence read back through
`core/freshness.py`, AC 2's separation as a database rule -- is in
`tests/integration/django_apps/test_feedstock.py`.

**The declarations are asserted against their derivations rather than against
themselves**, on the terms `tests/unit/django_apps/test_pypi_release.py` sets:
the target is the cadence times one plus the tolerated misses, the window is
shorter than the cadence, and one collection's worst case fits inside the
inherited Celery soft limit read from the settings module.

No database, no network: nothing here saves a row, no queryset is evaluated, and
every payload is a literal.
"""

from __future__ import annotations

import ast
import json
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

from conda_sentinel.collectors import feedstock as feedstock_module
from conda_sentinel.collectors.agent import USER_AGENT
from conda_sentinel.collectors.feedstock import ABSENT_BRANCH
from conda_sentinel.collectors.feedstock import ABSENT_FEEDSTOCK_DETAIL
from conda_sentinel.collectors.feedstock import AMBIGUOUS_STAGED_RECIPE_DETAIL
from conda_sentinel.collectors.feedstock import COLLECTOR_NAME
from conda_sentinel.collectors.feedstock import CONDA_FORGE_ORG
from conda_sentinel.collectors.feedstock import FEEDSTOCK_CACHE_TTL
from conda_sentinel.collectors.feedstock import FEEDSTOCK_CADENCE
from conda_sentinel.collectors.feedstock import FEEDSTOCK_FRESHNESS_TARGET
from conda_sentinel.collectors.feedstock import FEEDSTOCK_HEADERS
from conda_sentinel.collectors.feedstock import FEEDSTOCK_OBSERVATION_WINDOW
from conda_sentinel.collectors.feedstock import FEEDSTOCK_RATE_LIMIT
from conda_sentinel.collectors.feedstock import FEEDSTOCK_RETRIES
from conda_sentinel.collectors.feedstock import FEEDSTOCK_SUFFIX
from conda_sentinel.collectors.feedstock import FEEDSTOCK_TIMEOUT
from conda_sentinel.collectors.feedstock import GITHUB_API_HOST
from conda_sentinel.collectors.feedstock import GITHUB_RAW_HOST
from conda_sentinel.collectors.feedstock import HTML_URL_FIELD
from conda_sentinel.collectors.feedstock import ITEMS_FIELD
from conda_sentinel.collectors.feedstock import MAPPED_BRANCH
from conda_sentinel.collectors.feedstock import MAX_BUILD_NUMBER
from conda_sentinel.collectors.feedstock import MAX_DOCUMENT_CHARACTERS
from conda_sentinel.collectors.feedstock import MAX_RECIPE_CHARACTERS
from conda_sentinel.collectors.feedstock import NAME_FIELD
from conda_sentinel.collectors.feedstock import NEITHER_DETAIL
from conda_sentinel.collectors.feedstock import NO_STAGED_RECIPE_DETAIL
from conda_sentinel.collectors.feedstock import OVERFULL_QUEUE_DETAIL
from conda_sentinel.collectors.feedstock import PUSHED_AT_FIELD
from conda_sentinel.collectors.feedstock import SEARCH_RESULTS_PER_PAGE
from conda_sentinel.collectors.feedstock import STAGED_RECIPES_REPOSITORY
from conda_sentinel.collectors.feedstock import TITLE_FIELD
from conda_sentinel.collectors.feedstock import TOLERATED_MISSED_RUNS
from conda_sentinel.collectors.feedstock import TOTAL_COUNT_FIELD
from conda_sentinel.collectors.feedstock import UNCHECKED_FEEDSTOCK_DETAIL
from conda_sentinel.collectors.feedstock import UNCHECKED_QUEUE_DETAIL
from conda_sentinel.collectors.feedstock import UNREADABLE_RECIPE_DETAIL
from conda_sentinel.collectors.feedstock import UNUSABLE_PUSH_DETAIL
from conda_sentinel.collectors.feedstock import FeedstockCollector
from conda_sentinel.collectors.feedstock import FeedstockDocumentError
from conda_sentinel.collectors.feedstock import FeedstockIdentity
from conda_sentinel.collectors.feedstock import FeedstockLocatorError
from conda_sentinel.collectors.feedstock import asks_about
from conda_sentinel.collectors.feedstock import branch_of
from conda_sentinel.collectors.feedstock import feedstock_repository
from conda_sentinel.collectors.feedstock import inapplicability_of
from conda_sentinel.collectors.feedstock import recipe_facts
from conda_sentinel.collectors.feedstock import recipe_locator
from conda_sentinel.collectors.feedstock import repository_facts
from conda_sentinel.collectors.feedstock import repository_locator
from conda_sentinel.collectors.feedstock import staged_recipe
from conda_sentinel.collectors.feedstock import staged_recipes_locator
from conda_sentinel.collectors.github import AUTHENTICATED_SEARCH_ALLOWANCE
from conda_sentinel.collectors.github import AUTHORIZATION_HEADER
from conda_sentinel.collectors.github import BEARER_SCHEME
from conda_sentinel.collectors.github import GITHUB_TOKEN_SETTING
from conda_sentinel.collectors.models import FeedstockSnapshot
from conda_sentinel.collectors.tasks import COLLECT_FEEDSTOCK_TASK_NAME
from conda_sentinel.collectors.tasks import collect_feedstock
from conda_sentinel.core.clock import FixedClock
from conda_sentinel.core.collection import CONDITIONAL_HEADERS
from conda_sentinel.core.collection import CollectorConfigurationError
from conda_sentinel.core.outcomes import OutcomeState
from conda_sentinel.core.queues import Queue
from conda_sentinel.core.queues import queue_for
from conda_sentinel.core.transport import MAX_TIMEOUT
from conda_sentinel.core.transport import worst_case_call_seconds
from conda_sentinel.identity.models import ESTABLISHED
from conda_sentinel.identity.models import Feedstock
from tests.clocks import FIXED_INSTANT
from tests.collectors import A_GITHUB_TOKEN as A_TOKEN
from tests.collectors import ScriptedTransport
from tests.source_scan import SRC_ROOT
from tests.source_scan import dotted_name
from tests.source_scan import parse

if TYPE_CHECKING:
    from pathlib import Path

#: The module this file's source sweeps are about, relative to `src/`.
FEEDSTOCK_MODULE: Final[str] = "django_apps/conda_sentinel/collectors/feedstock.py"

#: The identity models this collector reads, and the write methods it may not
#: reach for. `CPM-AD-7` says a collector "reads only `identity`", and this one
#: reads it through the mapping row and the feedstock child rows -- so the module
#: may *name* both and may write neither.
MAPPING_MODEL_NAME: Final[str] = "PackageMapping"
FEEDSTOCK_MODEL_NAME: Final[str] = "Feedstock"
WRITE_METHODS: Final[frozenset[str]] = frozenset(
    {"abulk_create", "acreate", "asave", "bulk_create", "create", "get_or_create", "save", "update_or_create"},
)

#: The package the cases ask about, and the three locators its name produces.
#: Written out rather than composed from the module's own constants, because a
#: locator assembled the same way twice would agree with itself however wrong it
#: was.
A_NAME: Final[str] = "numpy"
THE_REPOSITORY: Final[str] = "numpy-feedstock"
THE_REPOSITORY_LOCATOR: Final[str] = "https://api.github.com/repos/conda-forge/numpy-feedstock"
THE_RECIPE_LOCATOR: Final[str] = "https://raw.githubusercontent.com/conda-forge/numpy-feedstock/HEAD/recipe/meta.yaml"

#: The locator a payload claims to have come from, for the document cases.
A_SOURCE: Final[str] = "https://api.github.com/repos/conda-forge/a-feedstock"
A_SEARCH_SOURCE: Final[str] = "https://api.github.com/search/issues?q=a-package"

#: What the repository document says, and the same instant to assert against.
THE_FEEDSTOCK_URL: Final[str] = "https://github.com/conda-forge/numpy-feedstock"
PUSHED: Final[str] = "2026-04-11T14:00:00Z"
PUSHED_INSTANT: Final[datetime] = datetime(2026, 4, 11, 14, 0, tzinfo=UTC)

#: What the recipe says.
A_RECIPE_VERSION: Final[str] = "2.1.3"
A_BUILD_NUMBER: Final[int] = 2

#: Where a staged recipe lives, and a second one for the ambiguous case.
A_STAGED_URL: Final[str] = "https://github.com/conda-forge/staged-recipes/pull/101"
ANOTHER_STAGED_URL: Final[str] = "https://github.com/conda-forge/staged-recipes/pull/202"

#: How much of the inherited soft limit one whole *collection* may spend -- three
#: quarters, so the claim is "with room for the ledger writes around it" rather
#: than "by a hair". Applied to the retried call plus the un-retried second one,
#: which is what this collector's collection actually is.
SOFT_LIMIT_SHARE: Final[float] = 0.75

#: A package key the cases that need one use. This tier never reads a row.
A_PACKAGE: Final[int] = 7

#: How many staged-recipe matches the ambiguous case arranges, and how many
#: distinct sentences the queue can produce for a blank URL. Named because
#: `PLR2004` is right about a bare number in an assertion -- and named
#: *separately*, because they count different things and one changing is not a
#: reason to move the other.
TWO_MATCHES: Final[int] = 2
THREE_SENTENCES: Final[int] = 3

#: The marker the document builders treat as "the source did not send this field
#: at all", as distinct from `None`, which is an explicit JSON `null`.
OMITTED: Final[object] = object()


def _repository(
    name: str | object | None = THE_REPOSITORY,
    url: str | object | None = THE_FEEDSTOCK_URL,
    pushed_at: str | object | None = PUSHED,
    **overrides: Any,
) -> str:
    """Return the body a source would serve for a feedstock repository.

    Args:
        name: The repository's `name`.
        url: Its `html_url`.
        pushed_at: Its `pushed_at`.
        **overrides: Further fields to add or replace.

    Returns:
        The JSON body.

    """
    document: dict[str, Any] = {
        NAME_FIELD: name,
        HTML_URL_FIELD: url,
        PUSHED_AT_FIELD: pushed_at,
        "full_name": f"{CONDA_FORGE_ORG}/{THE_REPOSITORY}",
    }
    document.update(overrides)
    return json.dumps({key: value for key, value in document.items() if value is not OMITTED})


def _search(*titles_and_urls: tuple[str, str], **overrides: Any) -> str:
    """Return the body a source would serve for a staged-recipes search.

    Args:
        *titles_and_urls: One `(title, html_url)` pair per open pull request.
        **overrides: Top-level fields to add or replace -- `total_count` for the
            queue that overflowed its page.

    Returns:
        The JSON body.

    """
    document: dict[str, Any] = {
        TOTAL_COUNT_FIELD: len(titles_and_urls),
        ITEMS_FIELD: [{TITLE_FIELD: title, HTML_URL_FIELD: url} for title, url in titles_and_urls],
    }
    document.update(overrides)
    return json.dumps({key: value for key, value in document.items() if value is not OMITTED})


def _recipe(version: str = A_RECIPE_VERSION, *, templated: bool = True, build: int | None = A_BUILD_NUMBER) -> str:
    """Return a conda-forge recipe, in either of the two shapes this collector reads.

    Args:
        version: The version the recipe pins.
        templated: Whether it is set by a `set version` assignment (which is what
            conda-forge writes) or spelled literally under `package:`.
        build: The build number, or `None` for a recipe declaring none.

    Returns:
        The recipe text.

    """
    header = f'{{% set version = "{version}" %}}\n\n' if templated else ""
    pinned = "{{ version }}" if templated else version
    lines = [f"{header}package:", "  name: numpy", f"  version: {pinned}", "", "source:", "  url: https://example.test"]
    if build is not None:
        lines += ["", "build:", f"  number: {build}"]
    return "\n".join(lines) + "\n"


def _stopped_clock() -> FixedClock:
    """Return the clock the collector cases inject.

    Returns:
        A `FixedClock` at `tests.clocks.FIXED_INSTANT`.

    """
    return FixedClock(instant=FIXED_INSTANT)


def _feedstock_module() -> Path:
    """Return the collector module this file's source sweeps read.

    Returns:
        Its path, resolved from `SRC_ROOT`.

    """
    return SRC_ROOT / FEEDSTOCK_MODULE


def _identity(
    outcome: str = ESTABLISHED,
    names: tuple[str, ...] = (THE_REPOSITORY,),
    canonical_name: str = A_NAME,
) -> FeedstockIdentity:
    """Return what resolution might have recorded about a package.

    Args:
        outcome: The `feedstock` mapping's outcome.
        names: The feedstocks the mapping holds.
        canonical_name: The package's own name.

    Returns:
        The identity.

    """
    return FeedstockIdentity(outcome=outcome, canonical_name=canonical_name, feedstock_names=names)


# ---------------------------------------------------------------------------
# The declarations, and the arithmetic behind them.
# ---------------------------------------------------------------------------


def test_the_collector_declares_every_value_the_base_checks() -> None:
    """All nine, written out on the class and carrying the module's own constants.

    Compared by *value* rather than by presence, for the reason the two release
    collectors' cases give: a class attribute rebound to the wrong constant
    declares nine names and behaves like something nobody wrote down.
    """
    declared = vars(FeedstockCollector)

    assert FeedstockCollector.name == COLLECTOR_NAME
    assert FeedstockCollector.evidence_model is FeedstockSnapshot
    assert FeedstockCollector.observation_window == FEEDSTOCK_OBSERVATION_WINDOW
    assert FeedstockCollector.timeout == FEEDSTOCK_TIMEOUT
    assert FeedstockCollector.retries == FEEDSTOCK_RETRIES
    assert FeedstockCollector.rate_limit == FEEDSTOCK_RATE_LIMIT
    assert FeedstockCollector.headers == FEEDSTOCK_HEADERS
    assert FeedstockCollector.freshness_target == FEEDSTOCK_FRESHNESS_TARGET
    assert FeedstockCollector.response_cache_ttl == FEEDSTOCK_CACHE_TTL
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


def test_the_cadence_is_the_slow_end_of_the_range_the_prd_gives() -> None:
    """`CPM-NFR-2` gives version currency "daily to weekly"; a recipe repository is the weekly one.

    Asserted as a comparison against the range's fast end rather than against
    itself, so a cadence quietly shortened to match the release collectors would
    fail here rather than pass by agreeing with a constant beside it.
    """
    assert timedelta(days=7) == FEEDSTOCK_CADENCE
    assert timedelta(days=1) < FEEDSTOCK_CADENCE


def test_the_freshness_target_is_the_arithmetic_open_question_7_settled() -> None:
    """`cadence x (1 + tolerated_missed_runs)`, and strictly greater than the cadence.

    `core/freshness.py` reports stale when `observed_at < now - target`, so a
    target *equal* to the cadence makes every package read stale at exactly the
    moment its next run is due, without a single collection having failed.
    """
    assert FEEDSTOCK_FRESHNESS_TARGET == FEEDSTOCK_CADENCE * (1 + TOLERATED_MISSED_RUNS)
    assert FEEDSTOCK_FRESHNESS_TARGET > FEEDSTOCK_CADENCE


def test_the_observation_window_cannot_suppress_a_scheduled_run() -> None:
    """Shorter than the cadence, which is the property rather than the halving."""
    assert FEEDSTOCK_OBSERVATION_WINDOW < FEEDSTOCK_CADENCE
    assert timedelta(0) < FEEDSTOCK_OBSERVATION_WINDOW


def test_the_response_cache_outlives_the_cadence() -> None:
    """An entry that expired between runs would make the cache inert."""
    assert FEEDSTOCK_CACHE_TTL > FEEDSTOCK_CADENCE


def test_one_whole_collection_fits_inside_the_inherited_soft_limit_with_room_to_spare() -> None:
    """The bound `core/transport.py` computes, plus the second call it does not cover.

    `worst_case_call_seconds` bounds the *retried* call the base makes. This
    collector then makes a bounded second call inside `translate`, outside that
    retry policy, so the figure a reconciliation has to be made against is the
    computed worst case plus one un-retried connect and read -- which is what the
    declared timeout was lowered to four seconds to keep inside three quarters of
    the limit. Read from the settings module rather than repeated here, so
    lowering the limit there fails this rather than passing quietly.
    """
    worst_case = worst_case_call_seconds(timeout=FEEDSTOCK_TIMEOUT, retries=FEEDSTOCK_RETRIES)
    soft_limit = settings.CELERY_TASK_SOFT_TIME_LIMIT

    assert worst_case + 2 * FEEDSTOCK_TIMEOUT <= SOFT_LIMIT_SHARE * soft_limit
    assert FEEDSTOCK_TIMEOUT <= MAX_TIMEOUT


def test_the_declared_headers_carry_what_the_source_expects_and_nothing_conditional() -> None:
    """Headers reach the socket only through the base (`CPM-AD-20`, `CPM-AD-27`).

    GitHub asks for an identifying `User-Agent`, the versioned JSON
    representation is asked for by name, and the API version is pinned so a
    future default cannot silently change the shape `repository_facts` reads.
    What is not declared is a validator, which the base composes from the
    response cache and refuses at construction.
    """
    lowered = {name.lower(): value for name, value in FEEDSTOCK_HEADERS.items()}

    assert lowered["user-agent"] == USER_AGENT
    assert lowered["accept"] == "application/vnd.github+json"
    assert lowered["x-github-api-version"] == "2022-11-28"
    assert set(lowered).isdisjoint({header.lower() for header in CONDITIONAL_HEADERS})


def test_the_declared_allowance_is_githubs_search_allowance_and_a_collection_fits_inside_it() -> None:
    """The tighter of the two allowances, because the base charges one number for both branches.

    The absent branch reads `/search/issues`, which GitHub limits to ten a minute
    unauthenticated. Two things are worth pinning: a single collection fits
    inside it -- an allowance smaller than `1 + retries` would refuse every call
    -- and it really is the *search* number rather than the core API's sixty an
    hour, which is what would be declared if the branch had been forgotten.
    """
    assert FEEDSTOCK_RATE_LIMIT.calls >= 1 + FEEDSTOCK_RETRIES
    assert FEEDSTOCK_RATE_LIMIT.per == timedelta(minutes=1)
    assert FEEDSTOCK_RATE_LIMIT.calls / FEEDSTOCK_RATE_LIMIT.per.total_seconds() < 1


def test_the_collector_is_constructed_from_its_declarations_alone() -> None:
    """The base's nine refusals, run against the real class rather than a fixture."""
    collector = FeedstockCollector(clock=_stopped_clock())

    try:
        assert collector.request_cost == 1 + FEEDSTOCK_RETRIES
        assert FEEDSTOCK_RATE_LIMIT.calls >= collector.request_cost
    finally:
        collector.close()


def test_without_a_token_the_instance_sends_the_class_declarations_exactly() -> None:
    """`CPM-OPERATE-S05`'s `No token` row: nothing about an unauthenticated collector changed."""
    with override_settings(**{GITHUB_TOKEN_SETTING: ""}):
        collector = FeedstockCollector(clock=_stopped_clock())

    try:
        assert dict(collector.headers) == dict(FEEDSTOCK_HEADERS)
        assert collector.rate_limit == FEEDSTOCK_RATE_LIMIT
        assert not any(name.lower() == AUTHORIZATION_HEADER.lower() for name in collector.headers)
    finally:
        collector.close()


def test_with_a_token_the_instance_declares_the_bearer_and_the_authenticated_search_allowance() -> None:
    """`CPM-OPERATE-S05`: the bearer on the instance, the *search* pool's authenticated number, the class untouched.

    Still the search number rather than the core one, for the reason the class
    declaration gives: the base charges one allowance before it knows which
    branch a package takes, and thirty a minute is the tighter of GitHub's two
    authenticated pools.
    """
    with override_settings(**{GITHUB_TOKEN_SETTING: A_TOKEN}):
        collector = FeedstockCollector(clock=_stopped_clock())

    try:
        assert dict(collector.headers) == {**FEEDSTOCK_HEADERS, AUTHORIZATION_HEADER: f"{BEARER_SCHEME} {A_TOKEN}"}
        assert collector.rate_limit == AUTHENTICATED_SEARCH_ALLOWANCE
        assert collector.rate_limit.calls >= collector.request_cost
        assert collector.rate_limit.per == timedelta(minutes=1)
    finally:
        collector.close()

    assert FeedstockCollector.headers == FEEDSTOCK_HEADERS
    assert FeedstockCollector.rate_limit == FEEDSTOCK_RATE_LIMIT


def test_a_malformed_token_refuses_construction_without_echoing_it() -> None:
    """The base never sees a header built from a value the fault rule refuses."""
    with (
        override_settings(**{GITHUB_TOKEN_SETTING: f"{A_TOKEN} {A_TOKEN}"}),
        pytest.raises(ImproperlyConfigured) as refused,
    ):
        FeedstockCollector(clock=_stopped_clock())

    assert GITHUB_TOKEN_SETTING in str(refused.value)
    assert A_TOKEN[:8] not in str(refused.value)


def test_the_task_name_routes_to_the_collect_queue() -> None:
    """`cpm.collect.*` is what puts external I/O on the `collect` queue (`R-11`).

    The Celery binding is asserted too: the constant is what routes, and a task
    registered under a name the constant does not spell would route nowhere.
    """
    assert queue_for(COLLECT_FEEDSTOCK_TASK_NAME) == Queue.COLLECT
    assert COLLECT_FEEDSTOCK_TASK_NAME.endswith(COLLECTOR_NAME)
    assert collect_feedstock.name == COLLECT_FEEDSTOCK_TASK_NAME


# ---------------------------------------------------------------------------
# The locators (`feedstock_repository`, and the three built on it).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("numpy", "numpy-feedstock"),
        ("numpy-feedstock", "numpy-feedstock"),
        ("NumPy", "numpy-feedstock"),
        ("NumPy-Feedstock", "numpy-feedstock"),
        ("  numpy  ", "numpy-feedstock"),
        ("zope.interface", "zope.interface-feedstock"),
        ("ruamel_yaml", "ruamel_yaml-feedstock"),
        ("feedstock", "feedstock-feedstock"),
        ("_openmp_mutex", "_openmp_mutex-feedstock"),
        ("_libgcc_mutex", "_libgcc_mutex-feedstock"),
    ],
    ids=[
        "bare-name",
        "already-suffixed",
        "mixed-case",
        "mixed-case-and-suffixed",
        "surrounded-by-space",
        "dotted",
        "underscored",
        "named-feedstock",
        "leading-underscore",
        "leading-underscore-again",
    ],
)
def test_every_spelling_of_one_feedstock_reaches_one_repository(name: str, expected: str) -> None:
    """The suffix is added once and the case is one case, because every key built from this is exact.

    A mapping may store `numpy` or `numpy-feedstock` and both name one
    repository; GitHub treats the two spellings of the case as one and this
    product's cache keys, `source` column and ledger locator do not. The last
    case is the one that shows "once" is a rule rather than a substring test: a
    package genuinely called `feedstock` has a repository called
    `feedstock-feedstock`, and a check for "already contains the suffix" would
    get it wrong.

    The two leading-underscore names are real conda-forge packages with real
    feedstocks. GitHub permits a name to begin with an underscore, so a grammar
    that refused them would make `_openmp_mutex` and `_libgcc_mutex` permanently
    uncollectable -- a `failed` run on every sweep, for ever, over a spelling
    conda-forge chose.
    """
    assert feedstock_repository(name) == expected


def test_the_golden_example_reaches_the_golden_locator() -> None:
    """The story's own example, verbatim, and the anti-vacuity half of the case above.

    Every case above compares against an expected *repository*; this one
    reconciles the whole locator against the module's declared hosts and paths, so
    a constant that was wrong would fail here rather than agree with itself.
    """
    assert repository_locator("numpy") == THE_REPOSITORY_LOCATOR
    assert repository_locator("numpy-feedstock") == THE_REPOSITORY_LOCATOR
    assert recipe_locator("numpy") == THE_RECIPE_LOCATOR
    assert THE_REPOSITORY_LOCATOR.startswith(f"https://{GITHUB_API_HOST}/repos/{CONDA_FORGE_ORG}/")
    assert THE_RECIPE_LOCATOR.startswith(f"https://{GITHUB_RAW_HOST}/{CONDA_FORGE_ORG}/")
    assert THE_REPOSITORY_LOCATOR.endswith(FEEDSTOCK_SUFFIX)


def test_the_staged_recipes_locator_searches_the_queue_for_open_pull_requests() -> None:
    """The absent branch's question, and the reason the declared allowance is the search one.

    Asserted through the encoded query rather than against a whole spelled-out
    URL, because what matters is *which* question is asked: the staged-recipes
    repository, open pull requests only, and the package's own name.
    """
    locator = staged_recipes_locator(A_NAME)

    assert locator.startswith(f"https://{GITHUB_API_HOST}/search/issues?q=")
    assert f"repo%3A{CONDA_FORGE_ORG}%2F{STAGED_RECIPES_REPOSITORY}" in locator
    assert "is%3Apr" in locator
    assert "is%3Aopen" in locator
    assert "in%3Atitle" in locator
    assert A_NAME in locator
    # The queue is searched for the *package*, not for the feedstock repository:
    # a staged recipe is a proposal to create one, so the suffix would name
    # something that does not exist yet.
    assert FEEDSTOCK_SUFFIX not in locator
    # The page size is asserted because it is what makes "more than one match"
    # reachable at all: at one result a page the ambiguity this collector refuses
    # to resolve would silently become a pick, and every other case here would
    # still pass.
    assert f"per_page={SEARCH_RESULTS_PER_PAGE}" in locator
    assert SEARCH_RESULTS_PER_PAGE > 1


@pytest.mark.parametrize(
    "name",
    ["", "   ", ".", "..", "-numpy", "num py", "num/py", "num:py", "..git", "#numpy"],
    ids=[
        "blank",
        "whitespace",
        "dot",
        "dot-dot",
        "leading-hyphen",
        "space",
        "slash",
        "colon",
        "dot-dot-git",
        "hash",
    ],
)
def test_a_name_that_is_not_a_repository_segment_is_refused(name: str) -> None:
    """Refused rather than repaired, and refused before any call is made.

    Every one of these could be turned into *some* locator by enough string
    surgery, and every such locator would be a request aimed at a repository
    nobody established (`CPM-FR-1`). The two relative references are the ones
    worth naming: a locator built from them would carry a path the source is
    entitled to resolve somewhere else, against the one organisation this
    collector asks about, and percent-encoding does not close it -- `.` and `..`
    are unreserved. They are refused by the segment grammar itself, which permits
    a name to *begin* only with a letter, a digit or an underscore, so no
    separate traversal check exists to disagree with it.
    """
    with pytest.raises(FeedstockLocatorError):
        feedstock_repository(name)


@pytest.mark.parametrize(
    "build",
    [repository_locator, recipe_locator, staged_recipes_locator],
    ids=["repository", "recipe", "staged-recipes"],
)
def test_every_locator_refuses_the_same_unusable_name(build: Any) -> None:
    """One unusable name is refused identically whichever branch reaches it.

    The staged-recipes locator names the *package* rather than the feedstock, so
    it could have been written without the refusals -- and would then have let a
    name the mapped branch rejects reach the search. All three share the segment
    grammar precisely so the two branches cannot come to disagree about which
    names they will ask about.
    """
    with pytest.raises(FeedstockLocatorError):
        build("num py")


def test_the_staged_recipes_locator_is_not_bound_by_a_width_its_row_never_fills() -> None:
    """The one refusal the search locator does *not* borrow, and the reason is what the row records.

    `feedstock_repository` refuses a name that would build a repository wider than
    the `feedstock_name` column. The absent branch's first call names the package
    rather than the feedstock and its `not_found` row records no feedstock name at
    all, so borrowing that bound would refuse a package this collector could
    otherwise say something true about -- and would refuse it for ever, on every
    sweep. The mapped branch, whose row *does* record the name, still refuses it.
    """
    width = FeedstockSnapshot._meta.get_field("feedstock_name").max_length  # noqa: SLF001 - Django's own public-by-convention API
    assert width is not None
    too_long_once_suffixed = "a" * (width - len(FEEDSTOCK_SUFFIX) + 1)

    assert staged_recipes_locator(too_long_once_suffixed).endswith(f"per_page={SEARCH_RESULTS_PER_PAGE}")

    with pytest.raises(FeedstockLocatorError):
        repository_locator(too_long_once_suffixed)


def test_a_name_that_is_not_a_string_is_refused_rather_than_crashing() -> None:
    """The column is a `CharField` and the read is a `values_list`, but the guard is stated anyway."""
    with pytest.raises(FeedstockLocatorError, match="name="):
        feedstock_repository(None)


def test_the_name_column_holds_every_name_identity_can_store_once_it_is_suffixed() -> None:
    """The relation the code needs, asserted rather than the number the comment states.

    This table stores the *repository*, which is `identity.Feedstock.name` plus
    conda-forge's suffix. A column merely equal to `identity`'s would leave a band
    of names -- 119 to 128 characters -- that `feedstocks` accepts and this
    collector can never record: a legal mapping and a `failed` run on every sweep
    for ever. The widths are declared in two modules that cannot import each
    other's private constant, so what keeps them in step is this assertion and the
    case below it.
    """
    stored = Feedstock._meta.get_field("name").max_length  # noqa: SLF001 - Django's own public-by-convention API
    recorded = FeedstockSnapshot._meta.get_field("feedstock_name").max_length  # noqa: SLF001 - as above
    assert stored is not None
    assert recorded is not None

    assert recorded >= stored + len(FEEDSTOCK_SUFFIX)
    # The anti-vacuity half: the widest name identity can hold really does build
    # a repository this table can record, rather than merely satisfying an
    # inequality about two numbers.
    assert len(feedstock_repository("a" * stored)) <= recorded


def test_a_repository_wider_than_the_name_column_is_refused_and_one_that_fits_is_not() -> None:
    """`R-5`'s parity gap, closed where the value enters.

    A name wider than the column, once suffixed, builds a row PostgreSQL refuses
    at insert -- after the call was spent -- and SQLite stores. Both sides of the
    boundary are asserted.
    """
    width = FeedstockSnapshot._meta.get_field("feedstock_name").max_length  # noqa: SLF001 - Django's own public-by-convention API
    assert width is not None

    widest = feedstock_repository("a" * (width - len(FEEDSTOCK_SUFFIX)))
    assert len(widest) == width

    with pytest.raises(FeedstockLocatorError, match=str(width)):
        feedstock_repository("a" * (width - len(FEEDSTOCK_SUFFIX) + 1))


# ---------------------------------------------------------------------------
# Applicability, and which branch (`asks_about`, `inapplicability_of`).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("outcome", "names"),
    [
        (ESTABLISHED, (THE_REPOSITORY,)),
        (ESTABLISHED, ()),
        (OutcomeState.NOT_FOUND.value, ()),
    ],
    ids=["established-with-rows", "established-with-none", "not-found"],
)
def test_a_mapping_resolution_reached_is_a_question_this_collector_asks(outcome: str, names: tuple[str, ...]) -> None:
    """All three are askable, and the middle one is the case `CPM-FR-6` exists for.

    `MAPPED_FIELDS[FEEDSTOCK]` is empty because the mapping *is* the child rows,
    so `established` with none is a *successful empty result* rather than a
    missing answer -- and it is the same question as `not_found` for this
    collector: resolution looked and named nothing, so what is worth asking is
    the staged-recipes queue.
    """
    identity = _identity(outcome=outcome, names=names)

    assert asks_about(identity)
    assert inapplicability_of(identity) == ""


def test_a_mapping_recorded_not_applicable_is_not_a_question_and_says_why() -> None:
    """Decided where `CPM-FR-1` says it was decided: by resolution, not by a name."""
    reason = inapplicability_of(_identity(outcome=OutcomeState.NOT_APPLICABLE.value, names=()))

    assert reason != ""
    assert OutcomeState.NOT_APPLICABLE.value in reason
    assert not asks_about(_identity(outcome=OutcomeState.NOT_APPLICABLE.value, names=()))


@pytest.mark.parametrize(
    "outcome",
    [OutcomeState.UNKNOWN.value, OutcomeState.ERROR.value],
    ids=["unknown", "error"],
)
def test_an_unresolved_mapping_is_neither_applicable_nor_inapplicable(outcome: str) -> None:
    """`CPM-UJ-2`: absence of a feedstock cannot be claimed for a package whose identity is unresolved.

    Answering a reason here would turn every unresolved package into a
    `not_applicable` observation nobody made; answering a locator would turn it
    into an absence nobody established. So the hook says nothing and `source_for`
    refuses -- a `failed` run and no row.
    """
    identity = _identity(outcome=outcome, names=())

    assert inapplicability_of(identity) == ""
    assert not asks_about(identity)


@pytest.mark.parametrize(
    ("outcome", "names", "expected"),
    [
        (ESTABLISHED, (THE_REPOSITORY,), MAPPED_BRANCH),
        (ESTABLISHED, (), ABSENT_BRANCH),
        (OutcomeState.NOT_FOUND.value, (), ABSENT_BRANCH),
        (OutcomeState.NOT_FOUND.value, (THE_REPOSITORY,), ""),
        (OutcomeState.UNKNOWN.value, (), ""),
        (OutcomeState.UNKNOWN.value, (THE_REPOSITORY,), ""),
    ],
    ids=[
        "established-with-rows",
        "established-with-none",
        "not-found-with-none",
        "not-found-with-rows",
        "unknown-with-none",
        "unknown-with-rows",
    ],
)
def test_the_branch_is_decided_from_the_outcome_and_the_rows_together(
    outcome: str,
    names: tuple[str, ...],
    expected: str,
) -> None:
    """Both halves, because either alone gets a real identity wrong.

    Reading the *rows* alone takes the mapped branch for a mapping recorded
    `not_found` that still carries stale `Feedstock` rows -- and then observes a
    feedstock resolution says is not there. Reading the *outcome* alone cannot
    tell `established` with rows from `established` with none, which is
    `CPM-FR-6`'s successful empty result and the whole reason the absent branch
    exists. The contradiction is refused rather than resolved either way, on the
    terms `CPM-CURRENCY-S02` refuses an established mapping with a blank primary
    type.
    """
    assert branch_of(_identity(outcome=outcome, names=names)) == expected


def test_the_two_branches_are_distinct_and_neither_is_blank() -> None:
    """The anti-vacuity half: a `branch_of` that answered one thing would satisfy half the table above."""
    assert MAPPED_BRANCH != ABSENT_BRANCH
    assert MAPPED_BRANCH
    assert ABSENT_BRANCH


# ---------------------------------------------------------------------------
# The repository document (`repository_facts`).
# ---------------------------------------------------------------------------


def test_a_repository_document_records_the_feedstock_its_url_and_its_last_push() -> None:
    """AC 1's first three facts, from the document that carries them all."""
    facts = repository_facts(_repository(), source=A_SOURCE, fallback_name=THE_REPOSITORY)

    assert facts.name == THE_REPOSITORY
    assert facts.url == THE_FEEDSTOCK_URL
    assert facts.pushed_at == PUSHED_INSTANT
    assert facts.detail == ""


@pytest.mark.parametrize(
    "pushed_at",
    [OMITTED, None, "", "   ", "not-a-date", "2026-04-11T14:00:00"],
    ids=["omitted", "explicit-null", "empty", "whitespace", "unparseable", "naive"],
)
def test_a_repository_whose_push_instant_is_unusable_records_no_activity_and_says_so(
    pushed_at: str | object | None,
) -> None:
    """Missing rather than invented, and never assumed to be UTC (`CPM-AD-26`).

    The feedstock still exists -- which is what the row's `state` claims -- and
    the activity column is NULL with `detail` saying why, rather than carrying an
    instant shifted by a guess into a row nothing may correct.
    """
    facts = repository_facts(_repository(pushed_at=pushed_at), source=A_SOURCE, fallback_name=THE_REPOSITORY)

    assert facts.name == THE_REPOSITORY
    assert facts.pushed_at is None
    assert facts.detail == UNUSABLE_PUSH_DETAIL


@pytest.mark.parametrize("name", [OMITTED, None, "", "   "], ids=["omitted", "explicit-null", "empty", "whitespace"])
def test_a_repository_naming_itself_nothing_is_recorded_under_the_name_this_run_asked(
    name: str | object | None,
) -> None:
    """A document that answered at all establishes the feedstock; blanking the name would lose that.

    The row's constraint requires a determinate observation to name a feedstock,
    and the honest name for one is the repository this run asked about -- which
    is a fact, where a blank column would be a row saying conda-forge has a
    feedstock it declines to identify.
    """
    facts = repository_facts(_repository(name=name), source=A_SOURCE, fallback_name=THE_REPOSITORY)

    assert facts.name == THE_REPOSITORY


@pytest.mark.parametrize(
    "body",
    ["", "not json", "[]", "42", '"a string"', "null"],
    ids=["empty", "not-json", "list", "number", "string", "null"],
)
def test_a_repository_document_that_is_not_an_object_is_refused(body: str) -> None:
    """Refused rather than read for whatever still parses.

    The base answers the refusal by writing an `error` row and re-raising, so the
    run is on the record either way.
    """
    with pytest.raises(FeedstockDocumentError):
        repository_facts(body, source=A_SOURCE, fallback_name=THE_REPOSITORY)


@pytest.mark.parametrize(
    "document",
    [
        _repository(name=7),
        _repository(name=[THE_REPOSITORY]),
        _repository(url={"href": THE_FEEDSTOCK_URL}),
        _repository(pushed_at=1_760_000_000),
    ],
    ids=["name-number", "name-list", "url-object", "pushed-at-number"],
)
def test_a_repository_document_whose_shape_has_changed_is_refused(document: str) -> None:
    """One rule for every field this collector reads: the wrong *type* is the shape changing.

    A push instant that is a string this collector cannot read is a missing
    activity signal; one that is a number is a source this collector no longer
    understands, and reading past it would record a feedstock assembled from a
    fragment, permanently.
    """
    with pytest.raises(FeedstockDocumentError):
        repository_facts(document, source=A_SOURCE, fallback_name=THE_REPOSITORY)


@pytest.mark.parametrize(
    ("field", "build"),
    [("feedstock_name", lambda wide: _repository(name=wide)), ("feedstock_url", lambda wide: _repository(url=wide))],
    ids=["name", "url"],
)
def test_a_repository_value_wider_than_its_column_is_refused_rather_than_truncated(field: str, build: Any) -> None:
    """`R-5`'s parity gap, closed where the value enters, on both text columns the document fills."""
    width = FeedstockSnapshot._meta.get_field(field).max_length  # noqa: SLF001 - Django's own public-by-convention API
    assert width is not None

    with pytest.raises(FeedstockDocumentError, match="characters"):
        repository_facts(build("a" * (width + 1)), source=A_SOURCE, fallback_name=THE_REPOSITORY)


def test_a_document_larger_than_the_ceiling_is_refused_before_it_is_decoded(monkeypatch: pytest.MonkeyPatch) -> None:
    """A worker has a sixty-second soft limit, and the decode is where a huge body would spend it (`CPM-AD-9`).

    The bound is lowered for the case rather than met, so the suite does not
    allocate four mebibytes to prove a comparison; the function reads the module
    constant at call time, and the message is asserted to name the bound it was
    refused against.
    """
    small_bound = 64
    monkeypatch.setattr(feedstock_module, "MAX_DOCUMENT_CHARACTERS", small_bound)

    with pytest.raises(FeedstockDocumentError, match=rf"at most {small_bound}\b"):
        repository_facts("{" + " " * small_bound, source=A_SOURCE, fallback_name=THE_REPOSITORY)

    assert small_bound < MAX_DOCUMENT_CHARACTERS


def test_a_deeply_nested_document_is_refused_rather_than_crashing_the_worker() -> None:
    """`json.loads` recurses per level, and raises `RecursionError` rather than a decode error."""
    with pytest.raises(FeedstockDocumentError):
        repository_facts("[" * 200_000, source=A_SOURCE, fallback_name=THE_REPOSITORY)


def test_the_document_refusals_name_the_source_they_are_about() -> None:
    """A failure an operator can act on names what was being read."""
    with pytest.raises(FeedstockDocumentError, match="a-feedstock"):
        repository_facts("not json", source=A_SOURCE, fallback_name=THE_REPOSITORY)


# ---------------------------------------------------------------------------
# The recipe (`recipe_facts`).
# ---------------------------------------------------------------------------


def test_a_recipe_records_the_version_the_way_conda_forge_writes_it() -> None:
    """The `{% set version = "..." %}` assignment every conda-forge recipe opens with.

    Read rather than rendered: the `version:` under `package:` in a real recipe
    is `{{ version }}`, and resolving it would mean executing recipe-authored
    template code inside a collector.
    """
    facts = recipe_facts(_recipe(), source=THE_RECIPE_LOCATOR)

    assert facts.version == A_RECIPE_VERSION
    assert facts.build_number == A_BUILD_NUMBER
    assert facts.metadata_url == THE_RECIPE_LOCATOR
    assert facts.detail == ""


def test_a_recipe_that_spells_its_version_literally_is_read_too() -> None:
    """The fallback: a literal `version:` under `package:`, for the recipes that use no template."""
    facts = recipe_facts(_recipe(templated=False), source=THE_RECIPE_LOCATOR)

    assert facts.version == A_RECIPE_VERSION
    assert facts.build_number == A_BUILD_NUMBER
    assert facts.detail == ""


@pytest.mark.parametrize(
    "body",
    [
        "",
        "package:\n  name: numpy\n",
        "package:\n  name: numpy\n  version: {{ computed_somehow }}\n",
        "build:\n  version: 1.0\n",
        "  version: 1.0\n",
        "not a recipe at all",
    ],
    ids=[
        "empty",
        "no-version",
        "computed-version",
        "version-in-the-wrong-section",
        "version-outside-any-section",
        "not-a-recipe",
    ],
)
def test_a_recipe_whose_version_cannot_be_read_is_blank_and_says_why(body: str) -> None:
    """The Block-If's answer: present with an unreadable version, never rendered and never guessed.

    The feedstock's existence is what the row's `state` claims, and it was
    established by the *repository*; the recipe is a second document, so a recipe
    this collector cannot read is a blank column with a reason rather than a
    failed collection. The `version-in-the-wrong-section` case is the one that
    shows the walk is a walk: a `version:` under `build:` is not this package's
    version.
    """
    facts = recipe_facts(body, source=THE_RECIPE_LOCATOR)

    assert facts.version == ""
    assert facts.metadata_url == THE_RECIPE_LOCATOR
    assert UNREADABLE_RECIPE_DETAIL in facts.detail


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("package:\n  version: 1.0  # first build\n", "1.0"),
        ("package:\n  version: 1.0 #first build\n", "1.0"),
        ('package:\n  version: "1.0 # not a comment"\n', "1.0 # not a comment"),
        ("package:\n  version: 1.0#2\n", "1.0#2"),
        ("package:\n  version: '1.0'  # quoted then commented\n", "1.0"),
    ],
    ids=["commented", "commented-tightly", "hash-inside-quotes", "hash-inside-a-word", "quoted-and-commented"],
)
def test_a_literal_version_is_read_without_the_comment_beside_it(body: str, expected: str) -> None:
    """A trailing YAML comment is not part of the version, and a `#` inside the value is.

    Recipes carry comments -- `version: 1.0  # bumped for the rebuild` -- and a
    reader that stripped only whitespace and quotes would store the comment as
    part of the version, permanently, in a row nothing may correct and against
    which `CPM-FR-16` will later compare. YAML opens an inline comment at a `#`
    preceded by whitespace or at the start of a value, so a `#` inside a quoted
    scalar and one in the middle of a bare word both survive: the second is what
    makes this a rule rather than "delete everything after the first hash".
    """
    assert recipe_facts(body, source=THE_RECIPE_LOCATOR).version == expected


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (_recipe(build=None), None),
        (_recipe(build=0), 0),
        ("build:\n  number: {{ build }}\n", None),
        ("build:\n  number: two\n", None),
        ("build:\n  number: -1\n", None),
        ("build:\n  number: 1  # a rebuild\n", 1),
        ("build:\n  number: \u0662\n", None),
        (f"build:\n  number: {MAX_BUILD_NUMBER}\n", MAX_BUILD_NUMBER),
        (f"build:\n  number: {MAX_BUILD_NUMBER + 1}\n", None),
    ],
    ids=[
        "declared-none",
        "zero",
        "templated",
        "not-a-number",
        "negative",
        "commented",
        "non-ascii-digit",
        "at-the-ceiling",
        "above-the-ceiling",
    ],
)
def test_a_build_number_is_read_only_when_it_is_a_plain_non_negative_integer(body: str, expected: int | None) -> None:
    """NULL means missing and `0` means zero -- PRD Appendix A.1, on the one integer where both are reachable.

    A first build of a version really is `0`, so a reader that treated it as
    "none" would lose the commonest value; a templated or negative one is a build
    number this collector cannot read rather than one it may guess at, and the
    column is nullable so it can say so.

    The last two pairs are the ones that would fail somewhere worse. An Arabic-
    Indic digit is a digit to `str.isdigit` and may not be one to `int`, so a
    reader that trusted the first would raise a `ValueError` out of a function
    this module documents as total -- from the *second* call, on a branch whose
    first call had already established the feedstock exists. A number past the
    column's ceiling would pass every check here and be refused at the insert,
    outside `translate`'s own `try`.
    """
    assert recipe_facts(body, source=THE_RECIPE_LOCATOR).build_number == expected


def test_a_recipe_version_wider_than_its_column_leaves_the_version_blank_rather_than_failing() -> None:
    """The width guard on the one column a *second* call fills, and it degrades rather than refuses.

    Everywhere else in this product an over-wide value is refused where it
    enters. Here refusing would fail a collection whose first call already
    established the feedstock exists, so the recipe is recorded as unreadable and
    the reason names the bound -- which is the same information, without
    discarding the fact the run did establish.
    """
    width = FeedstockSnapshot._meta.get_field("recipe_version").max_length  # noqa: SLF001 - Django's own public-by-convention API
    assert width is not None

    facts = recipe_facts(_recipe(version="9" * (width + 1)), source=THE_RECIPE_LOCATOR)

    assert facts.version == ""
    assert str(width) in facts.detail


def test_a_recipe_larger_than_the_ceiling_is_left_unread_rather_than_scanned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A recipe is kilobytes; a bound is a refusal to be surprised, and here it degrades too."""
    small_bound = 32
    monkeypatch.setattr(feedstock_module, "MAX_RECIPE_CHARACTERS", small_bound)

    facts = recipe_facts(_recipe(), source=THE_RECIPE_LOCATOR)

    assert facts.version == ""
    assert str(small_bound) in facts.detail
    assert small_bound < MAX_RECIPE_CHARACTERS


# ---------------------------------------------------------------------------
# The staged-recipes queue (`staged_recipe`).
# ---------------------------------------------------------------------------


def test_one_open_pull_request_naming_the_package_is_the_staged_recipe() -> None:
    """AC 2's fact: a package with no feedstock may still have one on the way."""
    found = staged_recipe(_search(("Add numpy", A_STAGED_URL)), name=A_NAME, source=A_SEARCH_SOURCE)

    assert found.url == A_STAGED_URL
    assert found.matched == 1


@pytest.mark.parametrize(
    "title",
    ["Add numpy", "add numpy recipe", "Add NumPy", "numpy", "Add numpy.", "WIP: add numpy"],
    ids=["plain", "with-suffix", "mixed-case", "bare", "punctuated", "prefixed"],
)
def test_a_title_naming_the_package_as_a_whole_word_matches_however_it_is_spelled(title: str) -> None:
    """GitHub's `in:title` is a word match, so the titles are compared after both sides are normalised."""
    assert staged_recipe(_search((title, A_STAGED_URL)), name=A_NAME, source=A_SEARCH_SOURCE).matched == 1


@pytest.mark.parametrize(
    "title",
    ["Add numpy-quaternion", "Add pynumpy", "Add numpyx", "Add scipy"],
    ids=["longer-name", "prefixed-name", "suffixed-name", "another-package"],
)
def test_a_title_that_merely_contains_the_name_is_not_this_packages_staged_recipe(title: str) -> None:
    """A substring match would record `Add numpy-quaternion` as the recipe that creates `numpy`.

    The search is deliberately wide -- GitHub's word match returns neighbours --
    so the narrowing happens here, bounded by hyphens or by the ends of the
    normalised title.
    """
    assert staged_recipe(_search((title, A_STAGED_URL)), name=A_NAME, source=A_SEARCH_SOURCE).matched == 0


@pytest.mark.parametrize(
    "body",
    [_search(), _search(**{ITEMS_FIELD: OMITTED}), _search(("Add scipy", A_STAGED_URL))],
    ids=["empty-items", "no-items-field", "no-match"],
)
def test_a_queue_that_names_this_package_nowhere_records_nothing(body: str) -> None:
    """Blank means missing, and the count says which kind of blank it is."""
    found = staged_recipe(body, name=A_NAME, source=A_SEARCH_SOURCE)

    assert found.url == ""
    assert found.matched == 0


def test_the_declared_build_ceiling_is_one_no_backend_this_product_runs_will_refuse() -> None:
    """The number is written down in one module and enforced by the database; the two must agree.

    **The column's own ceiling is backend-dependent, and that is why the
    declaration is the narrow one.** Django derives a `PositiveIntegerField`'s
    validators from the running backend's integer range: PostgreSQL maps it to
    `integer` and stops at `2**31 - 1`, SQLite stores a 64-bit value and stops
    nine orders of magnitude later. A collector that read the ceiling off the
    field would accept, on a developer's machine, a build number the deployed
    database refuses at insert -- the `R-5` parity gap this module closes
    everywhere else, in an integer column.

    So the constant is PostgreSQL's ceiling, computed rather than retyped, and it
    is asserted never to exceed what the backend under test would take. Both
    halves matter: the first is the number that has to be right in production,
    and the second fails the day a backend narrower than PostgreSQL appears.
    """
    field = FeedstockSnapshot._meta.get_field("recipe_build_number")  # noqa: SLF001 - Django's own public-by-convention API
    ceilings = [
        validator.limit_value
        for validator in field.validators
        if isinstance(getattr(validator, "limit_value", None), int) and validator.limit_value > 0
    ]

    assert ceilings
    assert MAX_BUILD_NUMBER == 2**31 - 1
    assert min(ceilings) >= MAX_BUILD_NUMBER


def test_a_queue_that_overflowed_its_page_does_not_read_as_an_absence() -> None:
    """One page is read, so "nothing matched here" is not "there is nothing".

    `total_count` says how many results the search matched; only
    `SEARCH_RESULTS_PER_PAGE` of them are served. A queue with more word-matching
    titles than that can put this package's genuine pull request on a page nobody
    asks for, and a row that then said "the queue holds no open pull request
    naming it" would be recording an absence this run did not establish -- which
    is the failure this whole collector is shaped around.
    """
    found = staged_recipe(
        _search(("Add scipy", A_STAGED_URL), **{TOTAL_COUNT_FIELD: SEARCH_RESULTS_PER_PAGE + 1}),
        name=A_NAME,
        source=A_SEARCH_SOURCE,
    )

    assert found.url == ""
    assert found.matched == 0
    assert found.truncated is True
    assert found.total == SEARCH_RESULTS_PER_PAGE + 1


def test_a_queue_that_fits_its_page_and_matches_nothing_is_an_absence() -> None:
    """The negative control: the overflow flag is a flag rather than always set."""
    found = staged_recipe(_search(("Add scipy", A_STAGED_URL)), name=A_NAME, source=A_SEARCH_SOURCE)

    assert found.truncated is False
    assert found.matched == 0


def test_a_match_found_on_the_page_read_stands_however_many_results_lay_behind_it() -> None:
    """Truncation threatens absence, not presence: a pull request that was read was read."""
    found = staged_recipe(
        _search(("Add numpy", A_STAGED_URL), **{TOTAL_COUNT_FIELD: SEARCH_RESULTS_PER_PAGE + 5}),
        name=A_NAME,
        source=A_SEARCH_SOURCE,
    )

    assert found.url == A_STAGED_URL
    assert found.truncated is True


@pytest.mark.parametrize(
    "total",
    ["many", 1.5, [1], True],
    ids=["string", "float", "list", "boolean"],
)
def test_a_search_whose_total_count_is_not_an_integer_is_refused(total: Any) -> None:
    """The count decides whether an absence may be claimed, so a mistyped one is the shape changing.

    `bool` is refused with the rest deliberately: it is an `int` in Python and is
    not a count in any document, and reading `True` as one would make a
    single-result page look like an overflow of one.
    """
    with pytest.raises(FeedstockDocumentError):
        staged_recipe(_search(**{TOTAL_COUNT_FIELD: total}), name=A_NAME, source=A_SEARCH_SOURCE)


def test_more_than_one_open_pull_request_is_refused_rather_than_picked() -> None:
    """Two proposals to create one feedstock is a real state of the queue, and choosing is not observing.

    The count survives so `detail` can say how many there were: a blank URL means
    the opposite thing here from what it means when the queue is empty.
    """
    found = staged_recipe(
        _search(("Add numpy", A_STAGED_URL), ("add numpy recipe", ANOTHER_STAGED_URL)),
        name=A_NAME,
        source=A_SEARCH_SOURCE,
    )

    assert found.url == ""
    assert found.matched == TWO_MATCHES


def test_a_blank_name_matches_nothing_rather_than_everything_that_normalises_to_blank() -> None:
    """The guard that stops an empty name matching a title's punctuation.

    `staged_recipe` is a public pure function and the branch above it refuses a
    blank name before it ever gets here -- but without the guard, a name that
    normalised to nothing would equal the normalisation of a lone hyphen, and a
    title like `Add - numpy` would be recorded as this package's staged recipe.
    A refusal that only holds because of where it is called from is not one.
    """
    found = staged_recipe(_search(("Add - numpy", A_STAGED_URL)), name="", source=A_SEARCH_SOURCE)

    assert found.url == ""
    assert found.matched == 0


def test_a_matching_pull_request_with_no_url_is_not_a_staged_recipe() -> None:
    """A row that could not say *where* the recipe is would be a claim nothing could check."""
    found = staged_recipe(_search(("Add numpy", "")), name=A_NAME, source=A_SEARCH_SOURCE)

    assert found.url == ""
    assert found.matched == 0


@pytest.mark.parametrize(
    "body",
    [
        "not json",
        "[]",
        json.dumps({ITEMS_FIELD: "one"}),
        json.dumps({ITEMS_FIELD: ["a title"]}),
        json.dumps({ITEMS_FIELD: [{TITLE_FIELD: 7, HTML_URL_FIELD: A_STAGED_URL}]}),
        json.dumps({ITEMS_FIELD: [{TITLE_FIELD: "Add numpy", HTML_URL_FIELD: 7}]}),
    ],
    ids=["not-json", "list", "items-string", "item-string", "title-number", "url-number"],
)
def test_a_search_document_whose_shape_has_changed_is_refused(body: str) -> None:
    """The search is the *first* call on its branch, so its document is refused like any other primary one."""
    with pytest.raises(FeedstockDocumentError):
        staged_recipe(body, name=A_NAME, source=A_SEARCH_SOURCE)


def test_a_staged_recipe_url_wider_than_its_column_is_refused_rather_than_truncated() -> None:
    """`R-5` again, on the one column the search fills."""
    width = FeedstockSnapshot._meta.get_field("staged_recipe_url").max_length  # noqa: SLF001 - Django's own public-by-convention API
    assert width is not None

    with pytest.raises(FeedstockDocumentError, match="characters"):
        staged_recipe(_search(("Add numpy", "h" * (width + 1))), name=A_NAME, source=A_SEARCH_SOURCE)


# ---------------------------------------------------------------------------
# The rows (`sentinel_evidence`, `__str__`).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "state",
    [OutcomeState.ERROR, OutcomeState.NOT_FOUND, OutcomeState.NOT_APPLICABLE],
    ids=lambda state: state.value,
)
def test_a_sentinel_row_carries_the_state_and_no_fact(state: OutcomeState) -> None:
    """Three shapes, one per sentinel the base may ask for, and every fact absent.

    The staged-recipe column is blank on all three, and that is the point rather
    than an oversight: the search that would fill it is the *first* call on the
    branch that makes it, so a sentinel row is one where it was never asked. The
    locator is blank on a fresh instance -- no `source_for` has answered -- which
    is what a `not_applicable` row records.
    """
    collector = FeedstockCollector(clock=_stopped_clock())
    try:
        row = collector.sentinel_evidence(
            state=state,
            package_id=A_PACKAGE,
            observed_at=FIXED_INSTANT,
            detail="a reason",
        )
    finally:
        collector.close()

    assert isinstance(row, FeedstockSnapshot)
    assert row.state == state.value
    assert row.observed_at == FIXED_INSTANT
    assert row.package_id == A_PACKAGE
    assert row.source == ""
    assert row.feedstock_name == ""
    assert row.feedstock_url == ""
    assert row.recipe_version == ""
    assert row.recipe_build_number is None
    assert row.recipe_metadata_url == ""
    assert row.last_recipe_activity_at is None
    assert row.staged_recipe_url == ""
    assert row.detail == "a reason"


def test_a_not_found_row_on_the_mapped_branch_says_which_feedstock_is_absent() -> None:
    """The fact that makes it interesting, and one the state cannot carry.

    A `404` from a repository resolution *named* is a statement about identity as
    much as about conda-forge: the feedstock a resolver established is gone. The
    row names it, and says the queue was not searched -- because resolution had
    already answered the question the queue would have.
    """
    collector = FeedstockCollector(clock=_stopped_clock())
    try:
        collector._branch = MAPPED_BRANCH  # noqa: SLF001 - the branch the base sets through `source_for`
        collector._repository = THE_REPOSITORY  # noqa: SLF001 - as above
        row = collector.sentinel_evidence(
            state=OutcomeState.NOT_FOUND,
            package_id=A_PACKAGE,
            observed_at=FIXED_INSTANT,
            detail="a reason",
        )
    finally:
        collector.close()

    assert "a reason" in row.detail  # type: ignore[attr-defined]
    assert THE_REPOSITORY in row.detail  # type: ignore[attr-defined]
    assert "staged-recipes" in row.detail  # type: ignore[attr-defined]


def test_a_not_found_row_on_the_absent_branch_says_the_queue_itself_could_not_be_read() -> None:
    """The other branch's `404` is about the *search endpoint*, and says nothing about the package.

    On the absent branch the first call is the staged-recipes search, so a `404`
    or `410` there makes the base write a `not_found` sentinel whose own sentence
    is "the locator reports the resource does not exist". Left at that, the row
    would read as an absence of a feedstock -- which this run never looked for,
    because the call that failed was the queue read and no repository was checked
    at all. That is the claim `CPM-UJ-2` forbids, arriving through the base rather
    than through `translate`.
    """
    collector = FeedstockCollector(clock=_stopped_clock())
    try:
        collector._branch = ABSENT_BRANCH  # noqa: SLF001 - the branch the base sets through `source_for`
        row = collector.sentinel_evidence(
            state=OutcomeState.NOT_FOUND,
            package_id=A_PACKAGE,
            observed_at=FIXED_INSTANT,
            detail="a reason",
        )
    finally:
        collector.close()

    assert "a reason" in row.detail  # type: ignore[attr-defined]
    assert UNCHECKED_QUEUE_DETAIL in row.detail  # type: ignore[attr-defined]
    assert ABSENT_FEEDSTOCK_DETAIL not in row.detail  # type: ignore[attr-defined]


def test_a_not_found_row_from_a_run_that_asked_neither_question_carries_the_bases_own_reason() -> None:
    """Three states, not two, which is why the branch is a value rather than a boolean.

    A caller reaching the hook without a branch -- and a run whose question did
    not apply -- has established nothing about either question, so there is
    nothing to add. A boolean would make this indistinguishable from the absent
    branch and would append a sentence about a queue nobody searched.
    """
    collector = FeedstockCollector(clock=_stopped_clock())
    try:
        row = collector.sentinel_evidence(
            state=OutcomeState.NOT_FOUND,
            package_id=A_PACKAGE,
            observed_at=FIXED_INSTANT,
            detail="a reason",
        )
    finally:
        collector.close()

    assert row.detail == "a reason"  # type: ignore[attr-defined]


def test_the_conventional_call_answers_rather_than_raises_when_it_cannot_even_name_a_repository() -> None:
    """Nothing in the absent branch's second call may raise, and the locator is part of "nothing".

    Every other way of not finding out -- a transport failure, a `304`, an
    unreadable document -- is caught and returned as a reason. So is this one, and
    it is the least likely and the worst if it were not: an exception from here
    would leave `translate` and turn an absence the search had already established
    into an `error` row and a `failed` run, which is precisely the invariant the
    module docstring and `docs/conda-sentinel/operations.md` both state.

    It is unreachable through `collect()` today -- `feedstock_name` is wide enough
    to hold any `canonical_name` once suffixed, which is what
    `test_the_name_column_holds_every_name_identity_can_store_once_it_is_suffixed`
    pins -- so it is asserted here, against the method, rather than left to be a
    claim in a docstring that nothing checks.
    """
    width = FeedstockSnapshot._meta.get_field("feedstock_name").max_length  # noqa: SLF001 - Django's own public-by-convention API
    assert width is not None
    unnameable = "a" * (width - len(FEEDSTOCK_SUFFIX) + 1)
    transport = ScriptedTransport()
    collector = FeedstockCollector(clock=_stopped_clock(), transport=transport)

    try:
        answer = collector._conventional_instead(name=unnameable)  # noqa: SLF001 - the bounded second call under test
    finally:
        collector.close()

    assert answer.facts is None
    assert answer.source == ""
    assert UNCHECKED_FEEDSTOCK_DETAIL in answer.detail
    assert ABSENT_FEEDSTOCK_DETAIL not in answer.detail
    # And no call was made, because there was no locator to make one to.
    assert transport.calls == []


@pytest.mark.parametrize("state", [OutcomeState.OK, OutcomeState.UNKNOWN], ids=lambda state: state.value)
def test_a_sentinel_state_this_collector_has_no_row_for_is_refused(state: OutcomeState) -> None:
    """Refused at the call rather than at the insert, which is where it would land.

    `ok` because a row carrying it and no feedstock name is refused by the
    table's own constraint; `unknown` because it is what a package with *no* row
    reads as, and writing it would be recording "we have not looked" as an
    observation.
    """
    collector = FeedstockCollector(clock=_stopped_clock())
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


def test_the_detail_a_package_with_neither_carries_is_composed_from_its_two_halves() -> None:
    """One sentence per fact, so a row that found a staged recipe does not claim there was none.

    The four sentences the queue can produce are asserted distinct as well: a
    blank `staged_recipe_url` means "none matched", "more than one matched", "the
    page overflowed" or "the queue was not read", and only `detail` can say which.
    """
    assert NEITHER_DETAIL.startswith(ABSENT_FEEDSTOCK_DETAIL)
    assert NEITHER_DETAIL.endswith(NO_STAGED_RECIPE_DETAIL)
    assert AMBIGUOUS_STAGED_RECIPE_DETAIL not in NEITHER_DETAIL
    assert OVERFULL_QUEUE_DETAIL not in NEITHER_DETAIL
    assert len({NO_STAGED_RECIPE_DETAIL, AMBIGUOUS_STAGED_RECIPE_DETAIL, OVERFULL_QUEUE_DETAIL}) == THREE_SENTENCES


def test_an_unsaved_snapshot_renders_its_absences_rather_than_raising() -> None:
    """A `__str__` that raises is what a failure message would have been."""
    rendered = str(FeedstockSnapshot())

    assert "no feedstock" in rendered
    assert "no package" in rendered
    assert "never" in rendered


def test_a_saved_snapshots_rendering_names_what_it_observed() -> None:
    """The anti-vacuity half: the placeholders are placeholders rather than the answer."""
    rendered = str(
        FeedstockSnapshot(
            observed_at=FIXED_INSTANT,
            package_id=A_PACKAGE,
            state=OutcomeState.OK.value,
            feedstock_name=THE_REPOSITORY,
        ),
    )

    assert THE_REPOSITORY in rendered
    assert str(A_PACKAGE) in rendered
    assert OutcomeState.OK.value in rendered
    assert FIXED_INSTANT.isoformat() in rendered


# ---------------------------------------------------------------------------
# The module's own source.
# ---------------------------------------------------------------------------


def test_the_collector_module_writes_no_row_of_any_kind() -> None:
    """`CPM-AD-7` and `CPM-AD-14`: this collector reads `identity` and writes evidence through the base."""
    tree = parse(_feedstock_module())
    written = sorted(
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and dotted_name(node.func).rpartition(".")[2] in WRITE_METHODS
    )

    assert written == [], f"the collector reaches a table directly at lines {written}"


def test_the_collector_module_reads_the_two_identity_models_it_is_allowed_to_read() -> None:
    """The anti-vacuity half: a module that named no model would sweep clean.

    `CPM-AD-7` is "a collector writes its own evidence table and reads only
    `identity`", and this collector's reads are the `feedstock` mapping row and
    the `Feedstock` child rows it answers for. Both are named; `Package` is not,
    because the canonical name is reached through the mapping's join rather than
    as a model of its own -- which keeps the read to the columns this collector
    is about.
    """
    named = {node.id for node in ast.walk(parse(_feedstock_module())) if isinstance(node, ast.Name)}

    assert MAPPING_MODEL_NAME in named
    assert FEEDSTOCK_MODEL_NAME in named
    assert "Package" not in named


def test_the_collector_module_opens_no_transaction_of_its_own() -> None:
    """The per-package transaction is the base's, around the evidence write (`CPM-AD-23`)."""
    tree = parse(_feedstock_module())
    opened = sorted(
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and dotted_name(node.func).endswith("transaction.atomic")
    )

    assert opened == []


def test_the_collector_module_imports_no_other_collector() -> None:
    """`CPM-AD-7`: "never imports another collector", asserted rather than assumed.

    The shared pieces this collector needs -- the `User-Agent` identity -- live in
    `collectors/agent.py` so that this assertion can be true; a later edit that
    reached for `source_release` or `pypi_release` for a locator helper would
    fail here.
    """
    imported = {
        node.module
        for node in ast.walk(parse(_feedstock_module()))
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }

    assert not any(module.endswith((".source_release", ".pypi_release", ".tasks")) for module in imported), imported
    assert any(module.endswith(".collectors.agent") for module in imported)
