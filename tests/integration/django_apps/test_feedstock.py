"""Feedstock collection against real tables: both branches, the rows, the ledger, and the two constraints.

`CPM-FR-9` asks for facts a row can say -- a feedstock exists and this is which
one, its recipe pins this version, somebody pushed to it then, there is none and
here is the recipe queued to create one -- and every one of them is only true or
false once a run exists. Which is why this module sits beside
`tests/unit/django_apps/test_feedstock.py`: the locators, the documents, the
declarations and the branch rule are decided before a run does, and are asserted
there.

**Both branches are proved end to end, because which one a package takes is the
whole design.** A package resolution gave a feedstock is asked about that
feedstock and then its recipe; a package resolution gave none is asked about the
staged-recipes queue and then about the conventional repository. The scripted
transport is what shows it: each case asserts *which* locators were fetched and
in which order, and a collector that asked the wrong question would fail there
rather than quietly record a plausible row.

**AC 1's second clause is asserted through the read every surface asks.** "Absence
of a feedstock is an observation with a timestamp, not a null" is a claim about
what a *later* reader sees, and a case that stopped at "a `not_found` row exists"
would pass identically if the row were never written at all. So the absence is
read back through `core/freshness.py`, with the paired never-observed case
showing that read answering the other thing.

**AC 2 is a database rule here, not a convention.** `staged_recipe_only_when_absent`
is asserted by writing the row the collector may not write and watching PostgreSQL
-- or SQLite -- refuse it.

**No socket is opened.** Every case substitutes the transport at the base's seam.
The task cases follow `test_pypi_release.py`'s two moves: one reaches a terminal
state before any call is made, and the other substitutes the collector the task
constructs.

Every test here rolls back: `@pytest.mark.django_db` wraps each in a transaction.
`tests/integration/conftest.py` marks everything under `tests/integration/` as an
integration test; the marker is not re-applied by hand.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import TYPE_CHECKING
from typing import Any
from typing import ClassVar
from typing import Final

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.db import IntegrityError
from django.db import transaction
from django.test import override_settings

from conda_sentinel.collectors import feedstock as feedstock_module
from conda_sentinel.collectors import tasks as collector_tasks
from conda_sentinel.collectors.feedstock import ABSENT_FEEDSTOCK_DETAIL
from conda_sentinel.collectors.feedstock import COLLECTOR_NAME
from conda_sentinel.collectors.feedstock import FEEDSTOCK_FRESHNESS_TARGET
from conda_sentinel.collectors.feedstock import FEEDSTOCK_HEADERS
from conda_sentinel.collectors.feedstock import FEEDSTOCK_RATE_LIMIT
from conda_sentinel.collectors.feedstock import FEEDSTOCK_RETRIES
from conda_sentinel.collectors.feedstock import NEITHER_DETAIL
from conda_sentinel.collectors.feedstock import NO_STAGED_RECIPE_DETAIL
from conda_sentinel.collectors.feedstock import OVERFULL_QUEUE_DETAIL
from conda_sentinel.collectors.feedstock import SEARCH_RESULTS_PER_PAGE
from conda_sentinel.collectors.feedstock import UNCHECKED_FEEDSTOCK_DETAIL
from conda_sentinel.collectors.feedstock import UNCHECKED_QUEUE_DETAIL
from conda_sentinel.collectors.feedstock import UNREADABLE_RECIPE_DETAIL
from conda_sentinel.collectors.feedstock import FeedstockCollector
from conda_sentinel.collectors.feedstock import FeedstockDocumentError
from conda_sentinel.collectors.feedstock import FeedstockLocatorError
from conda_sentinel.collectors.feedstock import recipe_locator
from conda_sentinel.collectors.feedstock import repository_locator
from conda_sentinel.collectors.feedstock import staged_recipes_locator
from conda_sentinel.collectors.github import AUTHENTICATED_SEARCH_ALLOWANCE
from conda_sentinel.collectors.github import BEARER_SCHEME
from conda_sentinel.collectors.github import GITHUB_TOKEN_SETTING
from conda_sentinel.collectors.models import ESTABLISHED_ABSENCE_CONSTRAINT
from conda_sentinel.collectors.models import FEEDSTOCK_FACTS_CONSTRAINT
from conda_sentinel.collectors.models import STAGED_RECIPE_CONSTRAINT
from conda_sentinel.collectors.models import FeedstockSnapshot
from conda_sentinel.collectors.tasks import collect_feedstock
from conda_sentinel.core.clock import Clock
from conda_sentinel.core.clock import FixedClock
from conda_sentinel.core.collection import REFUSED_CREDENTIAL_STATUS
from conda_sentinel.core.credentials import AUTHORIZATION_HEADER
from conda_sentinel.core.freshness import UNOBSERVED_STATUS
from conda_sentinel.core.models import CollectionRun
from conda_sentinel.core.outcomes import OutcomeState
from conda_sentinel.core.runs import RunLedgerError
from conda_sentinel.core.runs import RunState
from conda_sentinel.core.transport import Payload
from conda_sentinel.core.transport import TransportError
from conda_sentinel.identity.models import ESTABLISHED
from conda_sentinel.identity.models import Feedstock
from conda_sentinel.identity.models import MappingKind
from conda_sentinel.identity.models import Package
from conda_sentinel.identity.models import PackageMapping
from tests.clocks import FIXED_INSTANT
from tests.collectors import A_GITHUB_TOKEN
from tests.collectors import FixedLimiter
from tests.collectors import RecordingResponseCache
from tests.collectors import ScriptedTransport
from tests.collectors import cached_response
from tests.collectors import credential_sightings
from tests.collectors import recorded_payload

if TYPE_CHECKING:
    from conda_sentinel.core.collection import CollectionResult
    from conda_sentinel.core.rate_limit import RateLimiter
    from conda_sentinel.core.response_cache import ResponseCache
    from conda_sentinel.core.transport import Transport

#: The package the cases ask about, and the three locators its name produces.
#: Derived rather than written out: a case here is about what a *run* does with a
#: locator, and the unit tier is where their spelling is pinned.
A_NAME: Final[str] = "numpy"
THE_REPOSITORY: Final[str] = "numpy-feedstock"
THE_REPOSITORY_LOCATOR: Final[str] = repository_locator(A_NAME)
THE_RECIPE_LOCATOR: Final[str] = recipe_locator(A_NAME)
THE_SEARCH_LOCATOR: Final[str] = staged_recipes_locator(A_NAME)

#: What the repository document says, and the same instant to assert against.
THE_FEEDSTOCK_URL: Final[str] = "https://github.com/conda-forge/numpy-feedstock"
PUSHED: Final[str] = "2026-04-11T14:00:00Z"
PUSHED_INSTANT: Final[datetime] = datetime(2026, 4, 11, 14, 0, tzinfo=UTC)

#: What the recipe says.
A_RECIPE_VERSION: Final[str] = "2.1.3"
A_LATER_RECIPE_VERSION: Final[str] = "2.2.0"
A_BUILD_NUMBER: Final[int] = 2

#: Where a staged recipe lives, and a second one for the ambiguous case.
A_STAGED_URL: Final[str] = "https://github.com/conda-forge/staged-recipes/pull/101"
ANOTHER_STAGED_URL: Final[str] = "https://github.com/conda-forge/staged-recipes/pull/202"

#: The entity tag a source hands back, for the caching cases.
AN_ETAG: Final[str] = '"f33d"'

#: The header the credential travels in, as the API host must receive it.
THE_BEARER: Final[str] = f"{BEARER_SCHEME} {A_GITHUB_TOKEN}"

#: The counts the cases assert against, one named constant per concept. Named
#: because `PLR2004` is right about a bare number in an assertion, and kept apart
#: because they count different things: two evidence rows, two identity queries,
#: two feedstocks on one mapping, two open pull requests in the queue, and the two
#: requests one collection issues. One of them changing is not a reason to move
#: another, and a single `TWO` shared between them would make that invisible.
TWO_ROWS: Final[int] = 2
TWO_IDENTITY_QUERIES: Final[int] = 2
TWO_FEEDSTOCKS: Final[int] = 2
TWO_MATCHES: Final[int] = 2
TWO_REQUESTS: Final[int] = 2
ONE_ALLOWANCE_ASK: Final[int] = 1

#: The gap between two observations in the re-observation case, longer than the
#: declared window so the second collection is about re-observation.
A_WEEK: Final[timedelta] = timedelta(days=7)

#: The mapping kind every package in this module also carries, so a read that
#: forgot to filter by kind would answer from it. See `_a_package`.
DISTRACTOR_MAPPING_KIND: Final[str] = MappingKind.RELEASE_ECOSYSTEM.value

#: How many distinct reasons the three ways of failing to read the conventional
#: repository must produce.
THREE_REASONS: Final[int] = 3

#: The document ceiling the size case lowers the real one to, so nothing here
#: allocates four mebibytes to prove a comparison.
SMALL_DOCUMENT_BOUND: Final[int] = 64

#: A primary key no row in this module holds.
NO_SUCH_PACKAGE: Final[int] = 9_999_999


def _repository_document(
    url: str = THE_FEEDSTOCK_URL,
    pushed_at: str | None = PUSHED,
    name: str | None = THE_REPOSITORY,
) -> str:
    """Return the body GitHub would serve for a feedstock repository.

    Args:
        url: The repository's web URL.
        pushed_at: When it was last pushed to, or `None` for a source stating
            none.
        name: The repository's own name, or `None` for a document naming none --
            which is what makes the row fall back to the name this run asked
            about.

    Returns:
        The JSON body.

    """
    return json.dumps(
        {
            "name": name,
            "full_name": f"conda-forge/{THE_REPOSITORY}",
            "html_url": url,
            "pushed_at": pushed_at,
        },
    )


def _search_document(*titles_and_urls: tuple[str, str], total: int | None = None) -> str:
    """Return the body GitHub would serve for a staged-recipes search.

    Args:
        *titles_and_urls: One `(title, html_url)` pair per open pull request.
        total: What the search says it matched in total, for the queue that
            overflowed the one page this collector reads. Defaults to the number
            served, which is an ordinary answer.

    Returns:
        The JSON body.

    """
    return json.dumps(
        {
            "total_count": len(titles_and_urls) if total is None else total,
            "items": [{"title": title, "html_url": url} for title, url in titles_and_urls],
        },
    )


def _recipe_document(version: str = A_RECIPE_VERSION, *, build: int | None = A_BUILD_NUMBER) -> str:
    """Return a conda-forge recipe as the raw host would serve it.

    Args:
        version: The version the recipe pins.
        build: The build number, or `None` for a recipe declaring none.

    Returns:
        The recipe text.

    """
    lines = [f'{{% set version = "{version}" %}}', "", "package:", "  name: numpy", "  version: {{ version }}"]
    if build is not None:
        lines += ["", "build:", f"  number: {build}"]
    return "\n".join(lines) + "\n"


def _answering(**scripted: str | Payload) -> ScriptedTransport:
    """Return a transport answering each named locator with a body or a whole payload.

    Args:
        **scripted: `repository`, `recipe` and `search` -- each either the body
            the source serves or a whole `Payload` for the cases that need one.

    Returns:
        The scripted transport.

    """
    locators = {
        "repository": THE_REPOSITORY_LOCATOR,
        "recipe": THE_RECIPE_LOCATOR,
        "search": THE_SEARCH_LOCATOR,
    }
    answers: dict[str, Payload] = {}
    for which, answer in scripted.items():
        locator = locators[which]
        answers[locator] = answer if isinstance(answer, Payload) else recorded_payload(source=locator, body=answer)
    return ScriptedTransport(answers=answers)


def _a_package(
    name: str = A_NAME,
    *,
    outcome: str | None = ESTABLISHED,
    feedstocks: tuple[str, ...] = (THE_REPOSITORY,),
) -> Package:
    """Return a saved package with a recorded feedstock identity.

    Created directly rather than through `identity`'s resolution service, because
    what this module is about starts *after* an identity exists.

    Args:
        name: The canonical name, unique per case.
        outcome: The `feedstock` mapping's outcome, or `None` to record no
            mapping row at all -- a package no resolver has reached.
        feedstocks: The feedstocks the mapping holds, by name.

    Returns:
        The saved row.

    """
    package = Package.objects.create(canonical_name=name, resolved_at=FIXED_INSTANT)
    # A mapping of *another* kind, carrying an outcome that contradicts every
    # expectation in this module, on every package. Production writes one row per
    # `MappingKind` and `PackageMapping` declares no `Meta.ordering`, so a read
    # that dropped `kind=feedstock` from its filter would take whichever row the
    # database happened to return first -- and with one mapping per package, as
    # this module had before, the whole suite would stay green while the collector
    # answered from another kind's outcome. `not_applicable` is chosen because it
    # is the loudest possible wrong answer: every case here would become a
    # `not_applicable` row and no call would be made.
    PackageMapping.objects.create(
        package=package,
        kind=DISTRACTOR_MAPPING_KIND,
        outcome=OutcomeState.NOT_APPLICABLE.value,
        resolved_at=FIXED_INSTANT,
    )
    if outcome is not None:
        PackageMapping.objects.create(
            package=package,
            kind=MappingKind.FEEDSTOCK.value,
            outcome=outcome,
            resolved_at=FIXED_INSTANT,
        )
    for feedstock in feedstocks:
        Feedstock.objects.create(package=package, name=feedstock)
    return package


def _collect(  # noqa: PLR0913 - one parameter per seam the base takes; a bundle would hide the one under test
    package: Package,
    *,
    transport: ScriptedTransport,
    at: datetime = FIXED_INSTANT,
    force: bool = False,
    permitted: bool = True,
    cache: RecordingResponseCache | None = None,
    limiter: FixedLimiter | None = None,
    document_bound: int | None = None,
) -> CollectionResult:
    """Run one collection through a scripted transport.

    Args:
        package: The package to observe.
        transport: The transport substituted at the base's seam (`CPM-AD-27`).
        at: The instant the run's clock is stopped at.
        force: Whether to bypass the observation window (`CPM-UJ-1`).
        permitted: What the substituted limiter answers, when none is passed.
        cache: The response cache to use, or a fresh recording one.
        limiter: The limiter to use, so a case can read what it was asked.
        document_bound: A smaller `MAX_DOCUMENT_CHARACTERS` for the case about the
            ceiling, so the suite does not build a real one to prove a comparison.

    Returns:
        What the run did.

    """
    if document_bound is not None:
        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(feedstock_module, "MAX_DOCUMENT_CHARACTERS", document_bound)
    collector = FeedstockCollector(
        clock=FixedClock(instant=at),
        transport=transport,
        limiter=limiter if limiter is not None else FixedLimiter(permitted=permitted),
        response_cache=cache if cache is not None else RecordingResponseCache(),
    )
    try:
        return collector.collect(package_id=package.pk, force=force)
    finally:
        collector.close()
        if document_bound is not None:
            monkeypatch.undo()


def _rows(package: Package) -> list[FeedstockSnapshot]:
    """Return this package's observations, oldest first.

    Args:
        package: The package to read.

    Returns:
        The rows, ordered by primary key.

    """
    return list(FeedstockSnapshot.objects.filter(package=package).order_by("pk"))


def _run(package: Package) -> CollectionRun:
    """Return the most recent ledger row for this collector and package.

    Args:
        package: The package the run was scoped to.

    Returns:
        The row, newest first.

    """
    return CollectionRun.objects.filter(collector=COLLECTOR_NAME, package=package).order_by("-pk").first()  # type: ignore[return-value]


def _freshness(package: Package, *, status: str | None = None, now: datetime = FIXED_INSTANT) -> Any:
    """Read this collector's freshness for one package, as a read surface would.

    Args:
        package: The package to ask about.
        status: The status the evidence carries, or `None` for a caller holding
            no observation.
        now: The instant staleness is measured from.

    Returns:
        The `FreshnessReport`.

    """
    collector = FeedstockCollector(clock=FixedClock(instant=FIXED_INSTANT))
    try:
        if status is None:
            return collector.freshness(package_id=package.pk, now=now)
        return collector.freshness(package_id=package.pk, now=now, status=status)
    finally:
        collector.close()


# ---------------------------------------------------------------------------
# AC 1: the mapped branch -- a feedstock resolution established.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_a_mapped_feedstock_is_recorded_with_its_recipe_and_its_last_push() -> None:
    """AC 1: existence, recipe version, recipe metadata and recent recipe activity, on one row.

    Also the things a case about the row alone would not see: the two locators
    were asked in the order the branch names them, the requests carried the
    declared headers, and the repository locator reached the row's `source`.
    """
    package = _a_package()
    transport = _answering(repository=_repository_document(), recipe=_recipe_document())

    result = _collect(package, transport=transport)

    assert result.state == RunState.SUCCEEDED
    assert result.evidence_rows == 1
    assert transport.calls == [THE_REPOSITORY_LOCATOR, THE_RECIPE_LOCATOR]
    assert dict(FEEDSTOCK_HEADERS).items() <= dict(transport.sent_headers[0] or {}).items()
    assert dict(FEEDSTOCK_HEADERS).items() <= dict(transport.sent_headers[1] or {}).items()

    row = _rows(package)[0]
    assert row.source == THE_REPOSITORY_LOCATOR
    assert row.state == OutcomeState.OK.value
    assert row.feedstock_name == THE_REPOSITORY
    assert row.feedstock_url == THE_FEEDSTOCK_URL
    assert row.recipe_version == A_RECIPE_VERSION
    assert row.recipe_build_number == A_BUILD_NUMBER
    assert row.recipe_metadata_url == THE_RECIPE_LOCATOR
    assert row.last_recipe_activity_at == PUSHED_INSTANT
    assert row.staged_recipe_url == ""
    assert row.detail == ""
    assert row.observed_at == FIXED_INSTANT
    assert _run(package).status == RunState.SUCCEEDED.value


@pytest.mark.django_db
@pytest.mark.parametrize(
    "recipe",
    [
        recorded_payload(source=THE_RECIPE_LOCATOR, body="", found=False),
        recorded_payload(source=THE_RECIPE_LOCATOR, body="package:\n  name: numpy\n"),
        recorded_payload(source=THE_RECIPE_LOCATOR, body="", not_modified=True),
    ],
    ids=["recipe-absent", "recipe-sets-no-readable-version", "recipe-answered-304-unconditionally"],
)
def test_a_recipe_that_cannot_be_read_leaves_the_feedstock_recorded_and_says_why(recipe: Payload) -> None:
    """The second call's failure never fails the run: the feedstock exists, which is what `state` says.

    A recipe absent, a recipe that computes its version some way this collector
    does not read, and a source answering `304` to a request that carried no
    validator are three different ways to get no version, and none of them
    unmakes the fact the first call established.
    """
    package = _a_package()

    result = _collect(package, transport=_answering(repository=_repository_document(), recipe=recipe))

    assert result.state == RunState.SUCCEEDED
    row = _rows(package)[0]
    assert row.state == OutcomeState.OK.value
    assert row.feedstock_name == THE_REPOSITORY
    assert row.recipe_version == ""
    assert UNREADABLE_RECIPE_DETAIL in row.detail
    assert _run(package).status == RunState.SUCCEEDED.value


@pytest.mark.django_db
def test_a_recipe_call_that_fails_outright_leaves_the_feedstock_recorded_and_says_why() -> None:
    """A transport failure on the second call is a sentence in `detail`, not a failed collection."""
    package = _a_package()
    transport = _answering(repository=_repository_document())
    transport.failures[THE_RECIPE_LOCATOR] = TransportError("the raw host did not answer", source=THE_RECIPE_LOCATOR)

    result = _collect(package, transport=transport)

    assert result.state == RunState.SUCCEEDED
    row = _rows(package)[0]
    assert row.state == OutcomeState.OK.value
    assert row.recipe_version == ""
    assert row.recipe_metadata_url == ""
    assert UNREADABLE_RECIPE_DETAIL in row.detail


@pytest.mark.django_db
def test_a_repository_that_states_no_push_instant_records_no_activity_and_says_so() -> None:
    """Never invented: the feedstock exists and nobody can say when it was last touched."""
    package = _a_package()

    _collect(
        package,
        transport=_answering(repository=_repository_document(pushed_at=None), recipe=_recipe_document()),
    )

    row = _rows(package)[0]
    assert row.state == OutcomeState.OK.value
    assert row.last_recipe_activity_at is None
    assert row.detail != ""


@pytest.mark.django_db
def test_a_mapped_feedstock_that_is_gone_is_a_not_found_row_naming_it() -> None:
    """The mapped branch's `404` is a fact about identity as much as about conda-forge.

    `succeeded` rather than `failed`: the source answered, and the answer was "no
    such repository". No staged-recipes lookup is made -- resolution had already
    named a feedstock, so the queue is not the question -- and the row says which
    feedstock is the one that is absent.
    """
    package = _a_package()
    transport = _answering(repository=recorded_payload(source=THE_REPOSITORY_LOCATOR, body="", found=False))

    result = _collect(package, transport=transport)

    assert result.state == RunState.SUCCEEDED
    assert transport.calls == [THE_REPOSITORY_LOCATOR]
    row = _rows(package)[0]
    assert row.state == OutcomeState.NOT_FOUND.value
    assert row.feedstock_name == ""
    assert row.staged_recipe_url == ""
    assert THE_REPOSITORY in row.detail
    assert _run(package).status == RunState.SUCCEEDED.value


@pytest.mark.django_db
@pytest.mark.parametrize(
    "stored",
    ["numpy", "NumPy", "numpy-feedstock", "NumPy-Feedstock"],
    ids=["unsuffixed", "mixed-case", "suffixed", "mixed-case-and-suffixed"],
)
def test_a_repository_that_names_itself_nothing_is_recorded_under_one_spelling_whatever_was_stored(
    stored: str,
) -> None:
    """Four legal spellings of one mapping, one row, one feedstock name.

    A resolution may store `numpy` or `NumPy-Feedstock`; both name one repository
    and both reach one locator. The name a row falls back to when the document
    names none must therefore be the *normalised* one on both branches -- pass the
    stored spelling on one and the normalised one on the other, and the same
    feedstock ends up recorded under two names depending on which question found
    it, in a table nothing may correct and which `CPM-FR-40` will later group by.
    """
    package = _a_package(feedstocks=(stored,))
    transport = _answering(
        repository=_repository_document(name=None),
        recipe=_recipe_document(),
    )

    _collect(package, transport=transport)

    assert transport.calls == [THE_REPOSITORY_LOCATOR, THE_RECIPE_LOCATOR]
    assert _rows(package)[0].feedstock_name == THE_REPOSITORY


@pytest.mark.django_db
def test_more_than_one_mapped_feedstock_observes_the_first_by_name_and_says_how_many() -> None:
    """`CPM-FR-1` resolves "zero or more", and a row that observed one of three must say so.

    The first *by name* rather than by insertion order, so which feedstock a
    package's history is about does not depend on which resolver wrote its rows
    first.
    """
    package = _a_package(feedstocks=("zzz-feedstock", THE_REPOSITORY))
    transport = _answering(repository=_repository_document(), recipe=_recipe_document())

    _collect(package, transport=transport)

    assert transport.calls == [THE_REPOSITORY_LOCATOR, THE_RECIPE_LOCATOR]
    row = _rows(package)[0]
    assert row.feedstock_name == THE_REPOSITORY
    assert str(TWO_FEEDSTOCKS) in row.detail


# ---------------------------------------------------------------------------
# AC 2: the absent branch -- resolution established no feedstock.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
@pytest.mark.parametrize(
    "outcome",
    [ESTABLISHED, OutcomeState.NOT_FOUND.value],
    ids=["established-with-none", "not-found"],
)
def test_a_package_with_a_staged_recipe_and_no_feedstock_records_them_apart(outcome: str) -> None:
    """AC 2, end to end and on both spellings of "resolution found none".

    The queue is asked first -- it is the call that always answers -- and the
    conventional repository second. The row carries `not_found`, the staged
    recipe's URL, and no feedstock fact whatever: the separation AC 2 asks for is
    the difference between those two columns, and here only one of them is
    filled.
    """
    package = _a_package(outcome=outcome, feedstocks=())
    transport = _answering(
        search=_search_document(("Add numpy", A_STAGED_URL)),
        repository=recorded_payload(source=THE_REPOSITORY_LOCATOR, body="", found=False),
    )

    result = _collect(package, transport=transport)

    assert result.state == RunState.SUCCEEDED
    assert transport.calls == [THE_SEARCH_LOCATOR, THE_REPOSITORY_LOCATOR]
    row = _rows(package)[0]
    assert row.source == THE_SEARCH_LOCATOR
    assert row.state == OutcomeState.NOT_FOUND.value
    assert row.staged_recipe_url == A_STAGED_URL
    assert row.feedstock_name == ""
    assert row.feedstock_url == ""
    assert row.recipe_version == ""
    assert row.recipe_build_number is None
    assert row.recipe_metadata_url == ""
    assert row.last_recipe_activity_at is None
    assert ABSENT_FEEDSTOCK_DETAIL in row.detail
    # The repository answered absent and the queue answered with exactly one
    # match, so this run established the absence it recorded -- which is what
    # lets `CPM-CURRENCY-S07`'s policy read `staged_recipe_pending` from it
    # rather than `unknown`.
    assert row.absence_established is True
    assert _run(package).status == RunState.SUCCEEDED.value


@pytest.mark.django_db
def test_a_package_with_neither_records_that_both_were_looked_for() -> None:
    """The absence AC 1's second clause is about, with the reason a reader needs.

    A blank `staged_recipe_url` means the opposite thing here from what it means
    when the queue held two, so the row says which: both were looked for and
    neither was found.
    """
    package = _a_package(feedstocks=())
    transport = _answering(
        search=_search_document(),
        repository=recorded_payload(source=THE_REPOSITORY_LOCATOR, body="", found=False),
    )

    _collect(package, transport=transport)

    row = _rows(package)[0]
    assert row.state == OutcomeState.NOT_FOUND.value
    assert row.staged_recipe_url == ""
    assert row.detail == NEITHER_DETAIL
    assert row.absence_established is True, "both were looked for and neither found, which is an absence"


@pytest.mark.django_db
def test_an_ambiguous_staged_recipe_is_refused_rather_than_picked_and_the_row_says_how_many() -> None:
    """Two proposals to create one feedstock is a real state of the queue; choosing is not observing."""
    package = _a_package(feedstocks=())
    transport = _answering(
        search=_search_document(("Add numpy", A_STAGED_URL), ("add numpy recipe", ANOTHER_STAGED_URL)),
        repository=recorded_payload(source=THE_REPOSITORY_LOCATOR, body="", found=False),
    )

    _collect(package, transport=transport)

    row = _rows(package)[0]
    assert row.state == OutcomeState.NOT_FOUND.value
    assert row.staged_recipe_url == ""
    assert f"({TWO_MATCHES} matched)" in row.detail
    # The repository is absent, but the queue was not settled: two candidates and
    # neither recorded. A reader that took this for an established absence would
    # report a gap to fill for a package with two recipes already proposed.
    assert row.absence_established is False


@pytest.mark.django_db
def test_a_conventional_feedstock_that_exists_is_recorded_even_though_resolution_named_none() -> None:
    """`CPM-FR-9` asks for feedstock *existence*, which is not the same as what identity says.

    A row that merely restated the mapping would be recording identity as
    evidence. The bounded second call is what makes this an observation -- and
    the staged recipe the queue held is dropped from its column rather than
    recorded beside a feedstock that exists, which is AC 2's separation seen from
    the other side. It is not lost: `detail` names it.
    """
    package = _a_package(feedstocks=())
    transport = _answering(
        search=_search_document(("Add numpy", A_STAGED_URL)),
        repository=_repository_document(),
    )

    result = _collect(package, transport=transport)

    assert result.state == RunState.SUCCEEDED
    assert transport.calls == [THE_SEARCH_LOCATOR, THE_REPOSITORY_LOCATOR]
    row = _rows(package)[0]
    assert row.state == OutcomeState.OK.value
    assert row.feedstock_name == THE_REPOSITORY
    assert row.feedstock_url == THE_FEEDSTOCK_URL
    assert row.last_recipe_activity_at == PUSHED_INSTANT
    assert row.staged_recipe_url == ""
    assert row.recipe_version == ""
    assert "resolution had not established this feedstock" in row.detail
    assert A_STAGED_URL in row.detail
    # `source` names the locator these facts came from, not the one the base
    # fetched. Every fact on this row was read from the conventional repository;
    # a column naming the search endpoint would be a provenance claim that is
    # simply false, and `SourceReleaseSnapshot` sets the precedent the other way
    # -- a tag-fallback row names the tags locator.
    assert row.source == THE_REPOSITORY_LOCATOR


@pytest.mark.django_db
@pytest.mark.parametrize(
    "conventional",
    ["failure", "absent", "not-modified", "unreadable"],
    ids=["unreachable", "absent", "answered-a-question-nobody-asked", "shape-changed"],
)
def test_a_conventional_repository_this_run_could_not_read_is_not_recorded_as_an_absence(
    conventional: str,
) -> None:
    """ "Asked and it is not there" and "could not ask" are opposite claims, and only one is evidence.

    The run is `succeeded` either way -- resolution established no feedstock, and
    that is a fact this collection did establish -- so the difference lands in
    `detail`, which is the only place it can. A row that said the conventional
    repository is *absent* because nobody could reach it would put an absence
    nobody established into a log nothing may correct, which is what `CPM-UJ-2`
    forbids and what the whole shape of this collector is built around.

    The three ways of not finding out mirror
    `tests/integration/django_apps/test_source_release.py`'s fallback cases, with
    a fourth this collector adds: a document whose shape has changed, which would
    otherwise raise out of `translate` and turn the absence into an `error` row
    and a `failed` run.
    """
    package = _a_package(feedstocks=())
    transport = _answering(search=_search_document())
    if conventional == "failure":
        transport.failures[THE_REPOSITORY_LOCATOR] = TransportError("no answer", source=THE_REPOSITORY_LOCATOR)
    elif conventional == "absent":
        transport.answers[THE_REPOSITORY_LOCATOR] = recorded_payload(
            source=THE_REPOSITORY_LOCATOR,
            body="",
            found=False,
        )
    elif conventional == "not-modified":
        transport.answers[THE_REPOSITORY_LOCATOR] = recorded_payload(
            source=THE_REPOSITORY_LOCATOR,
            body="",
            not_modified=True,
        )
    else:
        transport.answers[THE_REPOSITORY_LOCATOR] = recorded_payload(
            source=THE_REPOSITORY_LOCATOR,
            body="not a repository document",
        )

    result = _collect(package, transport=transport)

    assert result.state == RunState.SUCCEEDED
    row = _rows(package)[0]
    assert row.state == OutcomeState.NOT_FOUND.value
    assert row.source == THE_SEARCH_LOCATOR
    if conventional == "absent":
        assert row.detail == NEITHER_DETAIL
        assert row.absence_established is True
    else:
        assert UNCHECKED_FEEDSTOCK_DETAIL in row.detail
        assert ABSENT_FEEDSTOCK_DETAIL not in row.detail
        # The structural half of the same claim, and the half a policy can read:
        # nobody looked successfully, so nothing was established.
        assert row.absence_established is False
    assert _run(package).status == RunState.SUCCEEDED.value


@pytest.mark.django_db
def test_the_three_ways_of_not_reading_the_conventional_repository_say_three_different_things() -> None:
    """The anti-vacuity half of the case above: the reasons are reasons, not one string.

    A `detail` that said "could not be read" and nothing more would satisfy every
    branch of the parametrised case while telling an operator nothing about which
    of the three happened -- and which happened is the difference between a
    network fault, a caching surprise and a source whose shape has changed.
    """
    reasons = set()
    for name, arrangement in (
        ("unreachable", "failure"),
        ("not-modified", "not-modified"),
        ("unreadable", "unreadable"),
    ):
        # One package per way of failing, each with its own canonical name -- so
        # each has its own pair of locators, built here the way the collector
        # builds them rather than reused from the module's constants.
        package = _a_package(name, feedstocks=())
        searched = staged_recipes_locator(name)
        conventional = repository_locator(name)
        transport = ScriptedTransport(
            answers={searched: recorded_payload(source=searched, body=_search_document())},
        )
        if arrangement == "failure":
            transport.failures[conventional] = TransportError("no answer", source=conventional)
        elif arrangement == "not-modified":
            transport.answers[conventional] = recorded_payload(source=conventional, body="", not_modified=True)
        else:
            transport.answers[conventional] = recorded_payload(source=conventional, body="not a repository document")
        _collect(package, transport=transport)
        reasons.add(_rows(package)[0].detail)

    assert len(reasons) == THREE_REASONS


@pytest.mark.django_db
def test_a_staged_recipes_search_that_answers_404_does_not_record_an_absence_of_a_feedstock() -> None:
    """The base's `not_found` sentinel on the absent branch, and what it may and may not claim.

    On this branch the first call is the staged-recipes **search endpoint**, so a
    `404` or `410` from it makes the base write its `not_found` row without ever
    reaching `translate` -- and no feedstock repository is checked at all. The
    base's own sentence says "the locator reports that the resource does not
    exist", which is true of the search endpoint and says nothing whatever about
    this package's feedstock; left at that, the row would read as an absence
    established by nothing, which is exactly what `CPM-UJ-2` forbids.
    """
    package = _a_package(feedstocks=())
    transport = _answering(search=recorded_payload(source=THE_SEARCH_LOCATOR, body="", found=False))

    result = _collect(package, transport=transport)

    assert result.state == RunState.SUCCEEDED
    assert transport.calls == [THE_SEARCH_LOCATOR]
    row = _rows(package)[0]
    assert row.state == OutcomeState.NOT_FOUND.value
    assert row.source == THE_SEARCH_LOCATOR
    assert row.staged_recipe_url == ""
    assert UNCHECKED_QUEUE_DETAIL in row.detail
    assert ABSENT_FEEDSTOCK_DETAIL not in row.detail
    assert row.absence_established is False
    assert _run(package).status == RunState.SUCCEEDED.value


@pytest.mark.django_db
def test_a_queue_that_overflowed_its_page_does_not_record_an_absence_of_a_staged_recipe() -> None:
    """One page is read, so "nothing matched here" is not "there is nothing".

    The row still carries `not_found` -- the conventional repository really is
    absent, which this run did establish -- and its `detail` says the queue was
    not read to the end rather than that it holds nothing.
    """
    package = _a_package(feedstocks=())
    transport = _answering(
        search=_search_document(("Add scipy", A_STAGED_URL), total=SEARCH_RESULTS_PER_PAGE + 1),
        repository=recorded_payload(source=THE_REPOSITORY_LOCATOR, body="", found=False),
    )

    _collect(package, transport=transport)

    row = _rows(package)[0]
    assert row.state == OutcomeState.NOT_FOUND.value
    assert row.staged_recipe_url == ""
    assert OVERFULL_QUEUE_DETAIL in row.detail
    assert NO_STAGED_RECIPE_DETAIL not in row.detail
    # The repository's absence was established and the queue's was not, and the
    # column records the conjunction: a match may be on a page nobody asked for,
    # so this row is not evidence that nothing is queued.
    assert row.absence_established is False


# ---------------------------------------------------------------------------
# AC 1's second clause: the absence is an observation a reader can find.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_an_absence_is_read_back_as_an_observation_with_its_instant_rather_than_as_staleness() -> None:
    """AC 1: "absence of a feedstock is an observation with a timestamp, not a null".

    Asserted through `core/freshness.py` -- the module every read surface asks --
    because that is where the difference between "we have not looked" and "we
    looked and conda-forge has nothing" actually shows.
    """
    package = _a_package(feedstocks=())
    _collect(
        package,
        transport=_answering(
            search=_search_document(),
            repository=recorded_payload(source=THE_REPOSITORY_LOCATOR, body="", found=False),
        ),
    )

    row = _rows(package)[0]
    report = _freshness(package, status=row.state)

    assert row.observed_at == FIXED_INSTANT
    assert report.observed_at == FIXED_INSTANT
    assert report.stale is False
    assert report.status == OutcomeState.NOT_FOUND.value


@pytest.mark.django_db
def test_a_package_this_collector_has_not_observed_reads_as_unobserved() -> None:
    """The anti-vacuity half: the freshness read can say the other thing."""
    package = _a_package()

    report = _freshness(package)

    assert report.observed_at is None
    assert report.status == UNOBSERVED_STATUS


@pytest.mark.django_db
def test_an_absence_ages_like_any_other_observation() -> None:
    """The negative control for the case above: a read that always answered "not stale" would pass both.

    `CPM-FR-9` asks for absence to be *recorded*, not for it to be exempt from
    ageing. The row is fresh when written and stale a second past the declared
    target, with its status carried, exactly as a determinate one would be.
    """
    package = _a_package(feedstocks=())
    _collect(
        package,
        transport=_answering(
            search=_search_document(),
            repository=recorded_payload(source=THE_REPOSITORY_LOCATOR, body="", found=False),
        ),
    )
    past_target = FIXED_INSTANT + FEEDSTOCK_FRESHNESS_TARGET + timedelta(seconds=1)

    report = _freshness(package, status=_rows(package)[0].state, now=past_target)

    assert report.stale is True
    assert report.status == OutcomeState.NOT_FOUND.value


# ---------------------------------------------------------------------------
# The question that does not apply, and the ones that are refused.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_a_package_whose_feedstock_mapping_is_not_applicable_is_recorded_without_a_call() -> None:
    """`CPM-FR-6`'s third state, decided from identity and written by the base.

    No transport call, no limiter ask (a limiter that refuses everything changes
    nothing), no cache read, and a `succeeded` run carrying the reason as its
    `detail` -- the same string the row carries. The row's `source` is blank,
    because no locator was ever built.
    """
    package = _a_package(outcome=OutcomeState.NOT_APPLICABLE.value, feedstocks=())
    transport = _answering(search=_search_document())
    cache = RecordingResponseCache()
    limiter = FixedLimiter(permitted=False)

    result = _collect(package, transport=transport, cache=cache, limiter=limiter)

    assert result.state == RunState.SUCCEEDED
    assert result.evidence_rows == 1
    assert transport.calls == []
    assert limiter.asks == []
    assert cache.reads == []
    row = _rows(package)[0]
    assert row.state == OutcomeState.NOT_APPLICABLE.value
    assert row.source == ""
    assert row.feedstock_name == ""
    assert row.staged_recipe_url == ""
    assert row.detail == result.detail
    assert row.detail != ""
    run = _run(package)
    assert run.status == RunState.SUCCEEDED.value
    assert run.detail == result.detail


@pytest.mark.django_db
@pytest.mark.parametrize(
    "outcome",
    [OutcomeState.UNKNOWN.value, OutcomeState.ERROR.value, None],
    ids=["unknown", "error", "no-mapping-row"],
)
def test_a_package_whose_feedstock_mapping_is_unresolved_fails_the_run_and_writes_nothing(
    outcome: str | None,
) -> None:
    """`CPM-UJ-2`: absence of a feedstock cannot be claimed for a package whose identity is unresolved.

    `source_for` is asked before the window, the allowance and the transport, so
    the refusal leaves a ledger row saying why and no evidence. Writing a
    `not_found` row here would be exactly the claim `CPM-UJ-2` forbids, and a
    `not_applicable` one would be a fact about the package nobody established.
    """
    package = _a_package(outcome=outcome, feedstocks=())
    transport = _answering(search=_search_document())

    with pytest.raises(FeedstockLocatorError):
        _collect(package, transport=transport)

    assert _rows(package) == []
    assert transport.calls == []
    assert _run(package).status == RunState.FAILED.value


@pytest.mark.django_db
def test_a_mapping_that_found_none_while_carrying_feedstock_rows_fails_the_run_and_writes_nothing() -> None:
    """An identity that contradicts itself is refused, not read either way.

    `not_found` means "resolution looked and found none"; `Feedstock` rows say it
    found these. A collector that decided the branch from the rows alone would
    take the mapped branch here and record an observation of a feedstock
    resolution says is not there -- and one that decided from the outcome alone
    would claim an absence beside rows that deny it. Both are the guess
    `CPM-FR-1` forbids, so the run fails and writes nothing, on the terms
    `CPM-CURRENCY-S02` refuses an established mapping with a blank primary type.
    """
    package = _a_package(outcome=OutcomeState.NOT_FOUND.value, feedstocks=(THE_REPOSITORY,))
    transport = _answering(repository=_repository_document(), search=_search_document())

    with pytest.raises(FeedstockLocatorError):
        _collect(package, transport=transport)

    assert _rows(package) == []
    assert transport.calls == []
    assert _run(package).status == RunState.FAILED.value


@pytest.mark.django_db
def test_a_mapping_whose_feedstock_name_is_not_a_repository_fails_the_run_and_writes_nothing() -> None:
    """A stored feedstock name is data a resolution wrote, and an unusable one is refused rather than repaired."""
    package = _a_package(feedstocks=("not a repository",))
    transport = _answering(repository=_repository_document())

    with pytest.raises(FeedstockLocatorError):
        _collect(package, transport=transport)

    assert _rows(package) == []
    assert transport.calls == []
    assert _run(package).status == RunState.FAILED.value


@pytest.mark.django_db
def test_asking_about_a_package_that_is_not_there_is_refused_rather_than_answered() -> None:
    """`source_for` is public, and a caller reaching it directly is not the base."""
    collector = FeedstockCollector(clock=FixedClock(instant=FIXED_INSTANT))

    try:
        with pytest.raises(FeedstockLocatorError, match="mapping row"):
            collector.source_for(package_id=NO_SUCH_PACKAGE)
    finally:
        collector.close()


@pytest.mark.django_db
def test_collecting_a_package_that_is_not_there_leaves_nothing_behind_at_all() -> None:
    """The base's actual surface for "no package row": the recorder refuses before any hook runs."""
    transport = _answering(search=_search_document())
    collector = FeedstockCollector(
        clock=FixedClock(instant=FIXED_INSTANT),
        transport=transport,
        limiter=FixedLimiter(permitted=True),
        response_cache=RecordingResponseCache(),
    )

    try:
        with pytest.raises(RunLedgerError):
            collector.collect(package_id=NO_SUCH_PACKAGE)
    finally:
        collector.close()

    assert transport.calls == []
    assert not FeedstockSnapshot.objects.filter(package_id=NO_SUCH_PACKAGE).exists()
    assert not CollectionRun.objects.filter(collector=COLLECTOR_NAME, package_id=NO_SUCH_PACKAGE).exists()


@pytest.mark.django_db
def test_one_instance_collecting_twice_reads_identity_fresh_on_the_second_run() -> None:
    """The identity read is remembered for one run, not for the instance's lifetime.

    A resolution can change between two runs of one long-lived instance -- the
    task constructs a fresh collector today, but a future sweep may not -- and a
    collector answering the second run from the first's read would refuse a
    package that has since been resolved, or ask the wrong branch's question. The
    first run here fails on an `unknown` mapping; the mapping is then established
    with a feedstock; the second run collects. The window does not intervene,
    because a `failed` run never suppresses.
    """
    package = _a_package(outcome=OutcomeState.UNKNOWN.value, feedstocks=())
    transport = _answering(repository=_repository_document(), recipe=_recipe_document())
    collector = FeedstockCollector(
        clock=FixedClock(instant=FIXED_INSTANT),
        transport=transport,
        limiter=FixedLimiter(permitted=True),
        response_cache=RecordingResponseCache(),
    )

    try:
        with pytest.raises(FeedstockLocatorError):
            collector.collect(package_id=package.pk)

        mapping = PackageMapping.objects.get(package=package, kind=MappingKind.FEEDSTOCK.value)
        mapping.outcome = ESTABLISHED
        mapping.save(update_fields=["outcome"])
        Feedstock.objects.create(package=package, name=THE_REPOSITORY)

        result = collector.collect(package_id=package.pk)
    finally:
        collector.close()

    assert result.state == RunState.SUCCEEDED
    assert transport.calls == [THE_REPOSITORY_LOCATOR, THE_RECIPE_LOCATOR]
    assert _rows(package)[0].state == OutcomeState.OK.value


@pytest.mark.django_db
def test_one_collection_reads_identity_once_for_both_hooks(django_assert_num_queries: Any) -> None:
    """The identity read is remembered between `inapplicability` and `source_for`.

    Two queries rather than one, and that is the mapping's own shape:
    `MAPPED_FIELDS[FEEDSTOCK]` is empty because the mapping *is* the child rows,
    so the outcome and the rows it answers for live in two tables. Counted rather
    than trusted -- a second pair would be a window in which the two hooks could
    disagree as well as a cost on every collection.
    """
    package = _a_package()
    collector = FeedstockCollector(
        clock=FixedClock(instant=FIXED_INSTANT),
        transport=_answering(repository=_repository_document()),
        limiter=FixedLimiter(permitted=True),
        response_cache=RecordingResponseCache(),
    )

    try:
        with django_assert_num_queries(TWO_IDENTITY_QUERIES):
            assert collector.inapplicability(package_id=package.pk) == ""
            assert collector.source_for(package_id=package.pk) == THE_REPOSITORY_LOCATOR
    finally:
        collector.close()


# ---------------------------------------------------------------------------
# The failing paths.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_an_unreachable_source_is_an_error_row_and_a_failed_run() -> None:
    """`CPM-NFR-3`: never a clean result, never no row."""
    package = _a_package()
    transport = _answering()
    transport.failures[THE_REPOSITORY_LOCATOR] = TransportError("no answer", source=THE_REPOSITORY_LOCATOR)

    result = _collect(package, transport=transport)

    assert result.state == RunState.FAILED
    assert result.evidence_rows == 1
    row = _rows(package)[0]
    assert row.state == OutcomeState.ERROR.value
    assert row.source == THE_REPOSITORY_LOCATOR
    assert row.detail != ""
    assert _run(package).status == RunState.FAILED.value


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("feedstocks", "which"),
    [((THE_REPOSITORY,), "repository"), ((), "search")],
    ids=["mapped-branch", "absent-branch"],
)
def test_a_document_that_cannot_be_read_writes_an_error_row_before_it_raises(
    feedstocks: tuple[str, ...],
    which: str,
) -> None:
    """A parser that no longer matches its source is an `error`, on the record first -- on either branch."""
    package = _a_package(feedstocks=feedstocks)

    with pytest.raises(FeedstockDocumentError):
        _collect(package, transport=_answering(**{which: "not a document"}))

    row = _rows(package)[0]
    assert row.state == OutcomeState.ERROR.value
    assert _run(package).status == RunState.FAILED.value


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("feedstocks", "which"),
    [((THE_REPOSITORY,), "repository"), ((), "search")],
    ids=["mapped-branch", "absent-branch"],
)
def test_a_first_document_over_the_ceiling_writes_an_error_row_before_it_raises(
    feedstocks: tuple[str, ...],
    which: str,
) -> None:
    """The size ceiling on the branch's *first* document, which is the one that may fail a run.

    A body past `MAX_DOCUMENT_CHARACTERS` is refused before it is decoded, on
    both branches, and the refusal travels the ordinary `translate` path: an
    `error` row first, then the exception. The bound is lowered for the case
    rather than met, so the suite does not build four mebibytes to prove a
    comparison.
    """
    package = _a_package(feedstocks=feedstocks)
    oversized = "{" + " " * SMALL_DOCUMENT_BOUND

    with pytest.raises(FeedstockDocumentError, match=str(SMALL_DOCUMENT_BOUND)):
        _collect(
            package,
            transport=_answering(**{which: oversized}),
            document_bound=SMALL_DOCUMENT_BOUND,
        )

    row = _rows(package)[0]
    assert row.state == OutcomeState.ERROR.value
    assert _run(package).status == RunState.FAILED.value


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("feedstocks", "scripted"),
    [
        ((THE_REPOSITORY,), {"repository": _repository_document(), "recipe": _recipe_document()}),
        ((), {"search": _search_document(), "repository": _repository_document()}),
    ],
    ids=["mapped-branch", "absent-branch"],
)
def test_one_collection_asks_the_allowance_once_and_issues_two_requests(
    feedstocks: tuple[str, ...],
    scripted: dict[str, str],
) -> None:
    """The property the module docstring, the operator documentation and two deferred entries all rest on.

    The base charges `1 + retries` once, before the first call; the second call
    is issued from `translate`, after that charge, on **both** branches. So one
    collection spends more of the remote budget than the local counter believes,
    every time rather than in a tail -- which is why it is written down as
    deferred rather than left to be discovered at sweep volume. Asserted rather
    than described: a change that started charging the second call, or that
    stopped making it, would fail here.
    """
    package = _a_package(feedstocks=feedstocks)
    transport = _answering(**scripted)
    limiter = FixedLimiter(permitted=True)

    _collect(package, transport=transport, limiter=limiter)

    assert len(limiter.asks) == ONE_ALLOWANCE_ASK
    assert len(transport.calls) == TWO_REQUESTS
    # And the charge really is the retry budget rather than one request, which is
    # the half of `CPM-AD-20` the count alone would not show.
    assert limiter.asks[0][3] == 1 + FEEDSTOCK_RETRIES


@pytest.mark.django_db
def test_a_spent_allowance_refuses_the_call_and_records_it() -> None:
    """`CPM-AD-20`: never issued unlimited, and never silently not issued either."""
    package = _a_package()
    transport = _answering(repository=_repository_document())

    result = _collect(package, transport=transport, permitted=False)

    assert result.state == RunState.FAILED
    assert transport.calls == []
    row = _rows(package)[0]
    assert row.state == OutcomeState.ERROR.value
    assert "allowance" in row.detail


# ---------------------------------------------------------------------------
# The window and the cache, as this collector declares them.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_a_second_collection_inside_the_window_is_skipped_without_a_call_and_force_writes_again() -> None:
    """`CPM-AD-7`'s window, and the only assertion that shows it: no call was made."""
    package = _a_package()
    _collect(package, transport=_answering(repository=_repository_document(), recipe=_recipe_document()))

    suppressed_transport = _answering(repository=_repository_document(), recipe=_recipe_document())
    suppressed = _collect(package, transport=suppressed_transport)
    forced = _collect(
        package,
        transport=_answering(repository=_repository_document(), recipe=_recipe_document()),
        force=True,
    )

    assert suppressed.state == RunState.SKIPPED
    assert suppressed_transport.calls == []
    assert forced.state == RunState.SUCCEEDED
    assert len(_rows(package)) == TWO_ROWS


@pytest.mark.django_db
def test_an_answer_carrying_a_validator_is_remembered_after_its_evidence_is_written() -> None:
    """This collector declares a cache lifetime, so the base's caching is live for it.

    Only the *first* call is cached: the second is made inside `translate` with
    no validator and outside the base's cache, which is what keeps a bounded
    fallback from quietly acquiring a second remembered entry.
    """
    package = _a_package()
    cache = RecordingResponseCache()

    _collect(
        package,
        transport=_answering(
            repository=recorded_payload(source=THE_REPOSITORY_LOCATOR, body=_repository_document(), etag=AN_ETAG),
            recipe=_recipe_document(),
        ),
        cache=cache,
    )

    assert cache.reads == [(COLLECTOR_NAME, THE_REPOSITORY_LOCATOR)]
    assert [source for _, source, _, _ in cache.writes] == [THE_REPOSITORY_LOCATOR]
    assert cache.entries[(COLLECTOR_NAME, THE_REPOSITORY_LOCATOR)].etag == AN_ETAG


@pytest.mark.django_db
def test_a_revalidated_answer_writes_the_same_evidence_a_body_would_have() -> None:
    """The `304` replay: the same row a `200` would have written, from the remembered body."""
    package = _a_package()
    cache = RecordingResponseCache(
        entries={
            (COLLECTOR_NAME, THE_REPOSITORY_LOCATOR): cached_response(body=_repository_document(), etag=AN_ETAG),
        },
    )
    transport = _answering(
        repository=recorded_payload(source=THE_REPOSITORY_LOCATOR, body="", not_modified=True, etag=AN_ETAG),
        recipe=_recipe_document(),
    )

    result = _collect(package, transport=transport, cache=cache, force=True)

    assert result.state == RunState.SUCCEEDED
    row = _rows(package)[0]
    assert row.state == OutcomeState.OK.value
    assert row.feedstock_name == THE_REPOSITORY
    assert row.last_recipe_activity_at == PUSHED_INSTANT
    assert row.recipe_version == A_RECIPE_VERSION
    assert transport.sent_headers[0] is not None
    assert AN_ETAG in dict(transport.sent_headers[0]).values()


@pytest.mark.django_db
def test_re_observation_inserts_rather_than_updating() -> None:
    """`CPM-AD-2`, on the real table: two observations of one package are two rows."""
    package = _a_package()
    _collect(package, transport=_answering(repository=_repository_document(), recipe=_recipe_document()))

    later = FIXED_INSTANT + A_WEEK
    _collect(
        package,
        transport=_answering(
            repository=_repository_document(),
            recipe=_recipe_document(A_LATER_RECIPE_VERSION),
        ),
        at=later,
    )

    rows = _rows(package)
    assert [row.recipe_version for row in rows] == [A_RECIPE_VERSION, A_LATER_RECIPE_VERSION]
    assert [row.observed_at for row in rows] == [FIXED_INSTANT, later]


# ---------------------------------------------------------------------------
# The task.
# ---------------------------------------------------------------------------


class SubstitutedCollector(FeedstockCollector):
    """The collector the task builds, with every seam already filled.

    The same move `tests/integration/django_apps/test_pypi_release.py` makes and
    for the same reason: a task takes a package key and nothing else, so the
    collector it constructs is the only seam a case about the task has.
    """

    fixed_transport: ClassVar[Transport | None] = None
    fixed_clock: ClassVar[Clock | None] = None

    def __init__(
        self,
        *,
        clock: Clock,
        transport: Transport | None = None,
        limiter: RateLimiter | None = None,
        response_cache: ResponseCache | None = None,
    ) -> None:
        """Build the collector the task asked for, on the case's own seams.

        Args:
            clock: What the task passed, replaced by the case's stopped one.
            transport: What the task passed, which is nothing.
            limiter: What the task passed, which is nothing.
            response_cache: What the task passed, which is nothing.

        """
        super().__init__(
            clock=type(self).fixed_clock or clock,
            transport=type(self).fixed_transport or transport,
            limiter=FixedLimiter(permitted=True) if limiter is None else limiter,
            response_cache=RecordingResponseCache() if response_cache is None else response_cache,
        )


@pytest.mark.django_db
def test_the_task_records_an_inapplicable_package_without_a_socket() -> None:
    """The real task, the real transport constructed, and no call made -- because none is due."""
    package = _a_package(outcome=OutcomeState.NOT_APPLICABLE.value, feedstocks=())

    returned = collect_feedstock(package_id=package.pk)

    assert returned == RunState.SUCCEEDED.value
    assert _rows(package)[0].state == OutcomeState.NOT_APPLICABLE.value
    assert _run(package).status == RunState.SUCCEEDED.value


@pytest.mark.django_db
def test_the_task_lets_an_unresolved_identity_out_as_a_failed_run() -> None:
    """The task wires `package_id` through to the base and lets the refusal out."""
    package = _a_package(outcome=None, feedstocks=())

    with pytest.raises(FeedstockLocatorError):
        collect_feedstock(package_id=package.pk)

    assert _run(package).status == RunState.FAILED.value
    assert _rows(package) == []


@pytest.mark.django_db
def test_the_task_carries_force_through_to_the_base_and_returns_how_the_run_ended(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`CPM-UJ-1`'s manual recollection is this task, and the flag is live here."""
    monkeypatch.setattr(SubstitutedCollector, "fixed_clock", FixedClock(instant=FIXED_INSTANT))
    monkeypatch.setattr(
        SubstitutedCollector,
        "fixed_transport",
        _answering(repository=_repository_document(), recipe=_recipe_document()),
    )
    monkeypatch.setattr(collector_tasks, "FeedstockCollector", SubstitutedCollector)
    package = _a_package()

    first = collect_feedstock(package_id=package.pk)
    suppressed = collect_feedstock(package_id=package.pk)
    forced = collect_feedstock(package_id=package.pk, force=True)

    assert first == RunState.SUCCEEDED.value
    assert suppressed == RunState.SKIPPED.value
    assert forced == RunState.SUCCEEDED.value
    assert len(_rows(package)) == TWO_ROWS


# ---------------------------------------------------------------------------
# The table's own rules, on a migrated schema.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_a_determinate_row_without_a_feedstock_name_is_refused_by_the_database() -> None:
    """The first conjunct, isolated: a determinate row names what it observed."""
    package = _a_package()

    with pytest.raises(IntegrityError, match=FEEDSTOCK_FACTS_CONSTRAINT), transaction.atomic():
        FeedstockSnapshot.objects.create(
            observed_at=FIXED_INSTANT,
            package=package,
            state=OutcomeState.OK.value,
            feedstock_name="",
            feedstock_url=THE_FEEDSTOCK_URL,
        )


@pytest.mark.django_db
@pytest.mark.parametrize(
    "fact",
    [
        {"feedstock_name": THE_REPOSITORY},
        {"feedstock_url": THE_FEEDSTOCK_URL},
        {"recipe_version": A_RECIPE_VERSION},
        {"recipe_build_number": A_BUILD_NUMBER},
        {"recipe_metadata_url": THE_RECIPE_LOCATOR},
        {"last_recipe_activity_at": PUSHED_INSTANT},
    ],
    ids=["name", "url", "recipe-version", "build-number", "metadata-url", "activity"],
)
def test_a_sentinel_row_carrying_any_feedstock_fact_is_refused_by_the_database(fact: dict[str, Any]) -> None:
    """Every remaining conjunct, one case each, so none of them is load-bearing only in prose."""
    package = _a_package()

    with pytest.raises(IntegrityError, match=FEEDSTOCK_FACTS_CONSTRAINT), transaction.atomic():
        FeedstockSnapshot.objects.create(
            observed_at=FIXED_INSTANT,
            package=package,
            state=OutcomeState.NOT_FOUND.value,
            **fact,
        )


@pytest.mark.django_db
@pytest.mark.parametrize(
    "state",
    [OutcomeState.OK.value, OutcomeState.ERROR.value, OutcomeState.NOT_APPLICABLE.value],
    ids=["ok", "error", "not-applicable"],
)
def test_a_row_that_is_not_an_absence_may_not_carry_a_staged_recipe(state: str) -> None:
    """AC 2, as the database enforces it rather than as a convention the collector observes.

    A staged recipe is a proposal to create a feedstock that does not exist, so
    it belongs only on the row that says one does not. An `ok` row carrying one
    would say both things at once; an `error` or `not_applicable` row carrying
    one would claim a search this run never made.
    """
    package = _a_package()
    facts: dict[str, Any] = {"feedstock_name": THE_REPOSITORY} if state == OutcomeState.OK.value else {}

    with pytest.raises(IntegrityError, match=STAGED_RECIPE_CONSTRAINT), transaction.atomic():
        FeedstockSnapshot.objects.create(
            observed_at=FIXED_INSTANT,
            package=package,
            state=state,
            staged_recipe_url=A_STAGED_URL,
            **facts,
        )


@pytest.mark.django_db
def test_an_absence_row_may_carry_a_staged_recipe_and_a_determinate_row_may_carry_no_recipe() -> None:
    """The two permissions the constraints have to grant, without which the refusals above prove nothing."""
    package = _a_package()
    other = _a_package("scipy", feedstocks=())

    FeedstockSnapshot.objects.create(
        observed_at=FIXED_INSTANT,
        package=package,
        state=OutcomeState.OK.value,
        feedstock_name=THE_REPOSITORY,
    )
    FeedstockSnapshot.objects.create(
        observed_at=FIXED_INSTANT,
        package=other,
        state=OutcomeState.NOT_FOUND.value,
        staged_recipe_url=A_STAGED_URL,
    )

    assert _rows(package)[0].feedstock_name == THE_REPOSITORY
    assert _rows(other)[0].staged_recipe_url == A_STAGED_URL


@pytest.mark.django_db
def test_only_a_row_recording_an_absence_may_claim_to_have_established_one() -> None:
    """`absence_established_only_on_an_absence`, refused by the database rather than named.

    An `ok` row establishes a feedstock's *presence*; an `error` or
    `not_applicable` row says nobody found out. A row claiming to have
    established an absence beside any of those is two opposite statements about
    one observation, and `CPM-CURRENCY-S07`'s policy reads that column to decide
    whether to report a gap somebody should go and fill.

    The collector cannot produce such a row -- it sets the flag on exactly one
    branch -- which is why the constraint exists and why asserting it by name
    would pass against one weakened to `1 = 1`. Built directly, on the terms
    every other constraint case in this module is.
    """
    package = _a_package()

    with pytest.raises(IntegrityError, match=ESTABLISHED_ABSENCE_CONSTRAINT), transaction.atomic():
        FeedstockSnapshot.objects.create(
            package=package,
            observed_at=FIXED_INSTANT,
            state=OutcomeState.OK.value,
            feedstock_name=THE_REPOSITORY,
            absence_established=True,
        )


@pytest.mark.django_db
def test_an_absence_row_may_claim_to_have_established_one() -> None:
    """The other side, so the constraint is not one that refuses every row.

    The same row differing only in `state`, which is the column the rule is
    about. A check written as "refuse every claim" would satisfy the case above
    and would make the collector's own absence branch unwritable -- which its
    cases would then report as a failure somewhere else entirely.
    """
    package = _a_package()

    written = FeedstockSnapshot.objects.create(
        package=package,
        observed_at=FIXED_INSTANT,
        state=OutcomeState.NOT_FOUND.value,
        absence_established=True,
    )

    assert written.pk is not None


# ---------------------------------------------------------------------------
# The credential (`CPM-OPERATE-S05`): which host gets it, and where it never appears.
# ---------------------------------------------------------------------------


def _refusal(locator: str, status: int | None) -> TransportError:
    """Return the failure the transport raises for a status that is neither success nor absence.

    Args:
        locator: What was being read.
        status: The status the source answered, or `None` for no answer.

    Returns:
        The error, worded as `core/transport.py` words it: the locator and the
        status, and no header.

    """
    message = f"{locator} answered {status}, which is neither a success nor an absence."
    if status is None:
        message = f"the call to {locator} produced no answer"
    return TransportError(message, source=locator, status_code=status)


def _collect_with_token(package: Package, *, transport: ScriptedTransport) -> tuple[CollectionResult, FixedLimiter]:
    """Run one collection with the credential declared, and hand back what the limiter was asked.

    Args:
        package: The package to observe.
        transport: The scripted transport.

    Returns:
        The result, and the limiter whose `asks` say which allowance the run
        was charged against.

    """
    limiter = FixedLimiter(permitted=True)
    with override_settings(**{GITHUB_TOKEN_SETTING: A_GITHUB_TOKEN}):
        collector = FeedstockCollector(
            clock=FixedClock(instant=FIXED_INSTANT),
            transport=transport,
            limiter=limiter,
            response_cache=RecordingResponseCache(),
        )
    try:
        return collector.collect(package_id=package.pk), limiter
    finally:
        collector.close()


def _authorization_sent(transport: ScriptedTransport) -> dict[str, str | None]:
    """Return what `Authorization` each locator was sent, by locator.

    Args:
        transport: The transport after a collection.

    Returns:
        The header's value per call, `None` where the call carried none.

    """
    return {
        locator: dict(sent or {}).get(AUTHORIZATION_HEADER)
        for locator, sent in zip(transport.calls, transport.sent_headers, strict=True)
    }


@pytest.mark.django_db
def test_on_the_mapped_branch_the_api_host_receives_the_bearer_and_the_raw_host_does_not() -> None:
    """The matrix's `Token, raw host` row: the repository read carries the credential, the recipe read does not.

    The recipe lives on `raw.githubusercontent.com`, which GitHub does not count
    against the API allowance and does not need the credential for; a
    credential reaches exactly the host it is for. Everything else about the
    recipe read is unchanged -- the declared headers still go.
    """
    package = _a_package()
    transport = _answering(repository=_repository_document(), recipe=_recipe_document())

    result, limiter = _collect_with_token(package, transport=transport)

    assert result.state == RunState.SUCCEEDED
    assert transport.calls == [THE_REPOSITORY_LOCATOR, THE_RECIPE_LOCATOR]
    assert _authorization_sent(transport) == {THE_REPOSITORY_LOCATOR: THE_BEARER, THE_RECIPE_LOCATOR: None}
    assert dict(FEEDSTOCK_HEADERS).items() <= dict(transport.sent_headers[1] or {}).items()
    assert [limit for _, limit, _, _ in limiter.asks] == [AUTHENTICATED_SEARCH_ALLOWANCE]
    assert FeedstockCollector.rate_limit == FEEDSTOCK_RATE_LIMIT
    assert FeedstockCollector.headers == FEEDSTOCK_HEADERS


@pytest.mark.django_db
def test_on_the_absent_branch_both_api_calls_carry_the_bearer() -> None:
    """The search and the conventional-repository read are both the API host, so both carry the credential."""
    package = _a_package(feedstocks=())
    transport = _answering(search=_search_document(), repository=_repository_document())

    result, _ = _collect_with_token(package, transport=transport)

    assert result.state == RunState.SUCCEEDED
    assert transport.calls == [THE_SEARCH_LOCATOR, THE_REPOSITORY_LOCATOR]
    assert _authorization_sent(transport) == {THE_SEARCH_LOCATOR: THE_BEARER, THE_REPOSITORY_LOCATOR: THE_BEARER}


@pytest.mark.django_db
def test_without_a_token_no_call_carries_a_credential() -> None:
    """The matrix's `No token` row, on the wire: exactly the declared headers on both calls."""
    package = _a_package()
    transport = _answering(repository=_repository_document(), recipe=_recipe_document())

    with override_settings(**{GITHUB_TOKEN_SETTING: ""}):
        result = _collect(package, transport=transport)

    assert result.state == RunState.SUCCEEDED
    assert [dict(sent or {}) for sent in transport.sent_headers] == [dict(FEEDSTOCK_HEADERS), dict(FEEDSTOCK_HEADERS)]


@pytest.mark.django_db
def test_a_refused_credential_is_a_failed_run_whose_detail_names_the_host_and_never_the_header(
    captured_collection_logs: list[dict[str, Any]],
) -> None:
    """The matrix's `Refused` row: `401` from the API host, worded by the base; nothing retried, nothing leaked."""
    package = _a_package()
    transport = _answering(recipe=_recipe_document())
    transport.failures[THE_REPOSITORY_LOCATOR] = _refusal(THE_REPOSITORY_LOCATOR, REFUSED_CREDENTIAL_STATUS)

    result, _ = _collect_with_token(package, transport=transport)

    assert result.state == RunState.FAILED
    assert result.detail.startswith("the declared credential was refused by api.github.com: ")
    assert transport.calls == [THE_REPOSITORY_LOCATOR]
    row = _rows(package)[0]
    run = _run(package)
    assert row.state == OutcomeState.ERROR.value
    assert row.detail == result.detail
    assert run.status == RunState.FAILED.value
    assert run.detail == result.detail
    assert credential_sightings(rows=[row, run], events=captured_collection_logs, details=[result.detail]) == []
    assert [event["detail"] for event in captured_collection_logs] == [result.detail]


@dataclass(frozen=True, slots=True)
class _Expected:
    """What one hygiene path is expected to produce: the run's end, the row's claim, and how `detail` opens."""

    state: RunState
    outcome: OutcomeState
    detail_prefix: str = ""


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("scripted", "failing", "expected"),
    [
        pytest.param(
            {"repository": _repository_document(), "recipe": _recipe_document()},
            None,
            _Expected(RunState.SUCCEEDED, OutcomeState.OK),
            id="success-mapped",
        ),
        pytest.param(
            {"search": _search_document(), "repository": _repository_document()},
            None,
            _Expected(RunState.SUCCEEDED, OutcomeState.OK),
            id="success-absent",
        ),
        pytest.param(
            {"recipe": _recipe_document()},
            REFUSED_CREDENTIAL_STATUS,
            _Expected(RunState.FAILED, OutcomeState.ERROR, "the declared credential was refused by api.github.com: "),
            id="401",
        ),
        pytest.param(
            {"recipe": _recipe_document()},
            403,
            _Expected(RunState.FAILED, OutcomeState.ERROR, "TransportError: "),
            id="403",
        ),
        pytest.param(
            {"repository": recorded_payload(source=THE_REPOSITORY_LOCATOR, found=False, body="")},
            None,
            _Expected(RunState.SUCCEEDED, OutcomeState.NOT_FOUND),
            id="404",
        ),
        pytest.param(
            {"recipe": _recipe_document()},
            -1,
            _Expected(RunState.FAILED, OutcomeState.ERROR, "TransportError: "),
            id="transport-failure",
        ),
        pytest.param(
            {"repository": _repository_document()},
            "recipe",
            _Expected(RunState.SUCCEEDED, OutcomeState.OK),
            id="recipe-401-on-the-raw-host",
        ),
    ],
)
def test_the_token_reaches_no_row_log_line_or_detail_on_any_path(
    captured_collection_logs: list[dict[str, Any]],
    scripted: dict[str, str | Payload],
    failing: int | str | None,
    expected: _Expected,
) -> None:
    """The matrix's `Hygiene` row, over every path a collection can take with the credential set.

    Every evidence row, every ledger row and every captured event is rendered
    whole and grepped for the token and for its first eight characters; the
    only places the credential may be are the headers the API host received.
    The recipe read on the raw host never carries it, on any path -- including
    the one where that host answers `401`, which is then a sentence in
    `detail` beside a feedstock the first call established, and still carries
    no header.

    Args:
        scripted: What each named locator answers.
        failing: The status the repository locator fails with, `-1` for a call
            that gets no answer, `"recipe"` for a `401` on the recipe read, or
            `None` for no failure.
        expected: How the run ends, what the row claims, how `detail` opens.

    """
    package = _a_package(feedstocks=() if "search" in scripted else (THE_REPOSITORY,))
    transport = _answering(**scripted)
    if failing == "recipe":
        transport.failures[THE_RECIPE_LOCATOR] = _refusal(THE_RECIPE_LOCATOR, REFUSED_CREDENTIAL_STATUS)
    elif failing is not None:
        transport.failures[THE_REPOSITORY_LOCATOR] = _refusal(
            THE_REPOSITORY_LOCATOR, None if failing == -1 else failing
        )

    result, _ = _collect_with_token(package, transport=transport)

    assert result.state == expected.state
    assert result.detail.startswith(expected.detail_prefix)
    rows = _rows(package)
    run = _run(package)
    assert [row.state for row in rows] == [expected.outcome.value]
    assert run.status == expected.state.value
    sent = _authorization_sent(transport)
    assert sent
    assert all(value == THE_BEARER for locator, value in sent.items() if locator != THE_RECIPE_LOCATOR)
    assert sent.get(THE_RECIPE_LOCATOR, None) is None
    assert credential_sightings(rows=[*rows, run], events=captured_collection_logs, details=[result.detail]) == []


@pytest.mark.django_db
def test_a_refused_credential_on_the_conventional_read_is_worded_inside_the_succeeded_rows_detail(
    captured_collection_logs: list[dict[str, Any]],
) -> None:
    """The absent branch's second call is the API host, so a `401` to it is a refused credential -- in the row.

    The search answer stands, the conventional repository is recorded as
    unchecked, and the reason carries the base's own wording; the recipe read
    on the raw host, by contrast, never carries the credential, so a `401`
    there is worded as any other failure -- the hygiene matrix's
    `recipe-401-on-the-raw-host` case.
    """
    package = _a_package(feedstocks=())
    transport = _answering(search=_search_document())
    transport.failures[THE_REPOSITORY_LOCATOR] = _refusal(THE_REPOSITORY_LOCATOR, REFUSED_CREDENTIAL_STATUS)

    result, _ = _collect_with_token(package, transport=transport)

    assert result.state == RunState.SUCCEEDED
    row = _rows(package)[0]
    assert UNCHECKED_FEEDSTOCK_DETAIL in row.detail
    assert f"{THE_REPOSITORY_LOCATOR} could not be read: the declared credential was refused by api.github.com: " in (
        row.detail
    )
    assert _authorization_sent(transport) == {THE_SEARCH_LOCATOR: THE_BEARER, THE_REPOSITORY_LOCATOR: THE_BEARER}
    assert (
        credential_sightings(rows=[row, _run(package)], events=captured_collection_logs, details=[result.detail]) == []
    )


@pytest.mark.django_db
def test_a_401_from_the_raw_host_is_not_a_refused_credential_because_none_was_sent() -> None:
    """The recipe read carries no bearer, so its `401` keeps the transport's own words."""
    package = _a_package()
    transport = _answering(repository=_repository_document())
    transport.failures[THE_RECIPE_LOCATOR] = _refusal(THE_RECIPE_LOCATOR, REFUSED_CREDENTIAL_STATUS)

    result, _ = _collect_with_token(package, transport=transport)

    assert result.state == RunState.SUCCEEDED
    row = _rows(package)[0]
    assert f"{THE_RECIPE_LOCATOR} could not be read: TransportError: " in row.detail
    assert "refused by" not in row.detail
    assert _authorization_sent(transport)[THE_RECIPE_LOCATOR] is None


# ---------------------------------------------------------------------------
# The task path, with every first-party logger captured (`CPM-OPERATE-S05`).
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_the_task_sends_the_bearer_to_the_api_host_only_and_prints_the_token_nowhere_on_success(
    monkeypatch: pytest.MonkeyPatch,
    captured_every_structlog_logger: list[dict[str, Any]],
) -> None:
    """Through `collect_feedstock` with the setting declared: the API host gets the bearer, nothing else does."""
    transport = _answering(repository=_repository_document(), recipe=_recipe_document())
    monkeypatch.setattr(SubstitutedCollector, "fixed_clock", FixedClock(instant=FIXED_INSTANT))
    monkeypatch.setattr(SubstitutedCollector, "fixed_transport", transport)
    monkeypatch.setattr(collector_tasks, "FeedstockCollector", SubstitutedCollector)
    package = _a_package()

    with override_settings(**{GITHUB_TOKEN_SETTING: A_GITHUB_TOKEN}):
        ended = collect_feedstock(package_id=package.pk)

    assert ended == RunState.SUCCEEDED.value
    assert _authorization_sent(transport) == {THE_REPOSITORY_LOCATOR: THE_BEARER, THE_RECIPE_LOCATOR: None}
    assert (
        credential_sightings(rows=[*_rows(package), _run(package)], events=captured_every_structlog_logger, details=[])
        == []
    )


@pytest.mark.django_db
def test_the_task_refuses_a_malformed_token_at_construction_and_prints_it_nowhere(
    monkeypatch: pytest.MonkeyPatch,
    captured_every_structlog_logger: list[dict[str, Any]],
) -> None:
    """A malformed value raises `ImproperlyConfigured` out of the task before any ledger row, and names the setting."""
    monkeypatch.setattr(collector_tasks, "FeedstockCollector", SubstitutedCollector)
    package = _a_package()
    malformed = f"{A_GITHUB_TOKEN}\n{A_GITHUB_TOKEN}"

    with (
        override_settings(**{GITHUB_TOKEN_SETTING: malformed}),
        pytest.raises(ImproperlyConfigured) as refused,
    ):
        collect_feedstock(package_id=package.pk)

    assert GITHUB_TOKEN_SETTING in str(refused.value)
    assert credential_sightings(rows=[], events=captured_every_structlog_logger, details=[str(refused.value)]) == []
    assert CollectionRun.objects.filter(collector=COLLECTOR_NAME, package=package).count() == 0
    assert _rows(package) == []
