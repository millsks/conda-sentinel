"""`CPM-IDENTITY-S08`: the resolver's declarations, locators, readers and decisions, with no run.

Everything about `collectors/resolve_identity.py` that is decidable before a run
exists: the nine declarations and the arithmetic behind them, the two locators
and how the index shards, the repository precedence and the normalisation table,
the two document readers' refusals, and `resolution_for` -- the pure function
that turns two answers into the resolution the recorder is handed. Every case
here takes data and returns data (`CPM-AD-27`); what a *run* does with these
answers -- the rows, the ledger, the recorder's writes -- is
`tests/integration/django_apps/test_resolve_identity.py`.

**Two rules are reconciled against sibling collectors rather than asserted from
literals.** The normalised repository URL must be one
`collectors/source_release.py` accepts, and the PEP 503 normalisation must be
the one `collectors/pypi_release.py` applies to a stored purl. Neither module may
import the other (`CPM-AD-7`), so each restates the rule and this file is what
keeps the restatements from drifting.

No database, no network, no clock.
"""

from __future__ import annotations

import ast
import json
from datetime import timedelta
from typing import TYPE_CHECKING
from typing import Any
from typing import Final

import pytest
from django.conf import settings

from conda_sentinel.collectors import pypi_release
from conda_sentinel.collectors import resolve_identity
from conda_sentinel.collectors import source_release
from conda_sentinel.collectors.agent import USER_AGENT
from conda_sentinel.collectors.models import IdentityResolutionSnapshot
from conda_sentinel.collectors.resolve_identity import CARRIED_FORWARD_DETAIL
from conda_sentinel.collectors.resolve_identity import COLLECTOR_NAME
from conda_sentinel.collectors.resolve_identity import FEEDSTOCK_OUTPUTS_HOST
from conda_sentinel.collectors.resolve_identity import GITHUB_WEB_HOSTS
from conda_sentinel.collectors.resolve_identity import HOMEPAGE_KEY
from conda_sentinel.collectors.resolve_identity import INDEX_LISTS_NONE_DETAIL
from conda_sentinel.collectors.resolve_identity import ISSUES_KEYS
from conda_sentinel.collectors.resolve_identity import MAX_DOCUMENT_CHARACTERS
from conda_sentinel.collectors.resolve_identity import NO_PYPI_PROJECT_DETAIL
from conda_sentinel.collectors.resolve_identity import NO_REPOSITORY_DETAIL
from conda_sentinel.collectors.resolve_identity import PRIOR_ROWS_KEPT_DETAIL
from conda_sentinel.collectors.resolve_identity import PYPI_HOST
from conda_sentinel.collectors.resolve_identity import PYPI_UNREADABLE_DETAIL
from conda_sentinel.collectors.resolve_identity import REPOSITORY_PRECEDENCE
from conda_sentinel.collectors.resolve_identity import RESOLUTION_CACHE_TTL
from conda_sentinel.collectors.resolve_identity import RESOLUTION_CADENCE
from conda_sentinel.collectors.resolve_identity import RESOLUTION_FRESHNESS_TARGET
from conda_sentinel.collectors.resolve_identity import RESOLUTION_HEADERS
from conda_sentinel.collectors.resolve_identity import RESOLUTION_OBSERVATION_WINDOW
from conda_sentinel.collectors.resolve_identity import RESOLUTION_RATE_LIMIT
from conda_sentinel.collectors.resolve_identity import RESOLUTION_RETRIES
from conda_sentinel.collectors.resolve_identity import RESOLUTION_TIMEOUT
from conda_sentinel.collectors.resolve_identity import TOLERATED_MISSED_RUNS
from conda_sentinel.collectors.resolve_identity import ChosenRepository
from conda_sentinel.collectors.resolve_identity import IdentityResolutionCollector
from conda_sentinel.collectors.resolve_identity import PackageIdentity
from conda_sentinel.collectors.resolve_identity import ProjectAnswer
from conda_sentinel.collectors.resolve_identity import ResolutionDocumentError
from conda_sentinel.collectors.resolve_identity import ResolutionLocatorError
from conda_sentinel.collectors.resolve_identity import conda_purl
from conda_sentinel.collectors.resolve_identity import feedstocks_in
from conda_sentinel.collectors.resolve_identity import index_locator
from conda_sentinel.collectors.resolve_identity import normalised_label
from conda_sentinel.collectors.resolve_identity import normalised_name
from conda_sentinel.collectors.resolve_identity import normalised_repository
from conda_sentinel.collectors.resolve_identity import project_locator
from conda_sentinel.collectors.resolve_identity import project_urls_in
from conda_sentinel.collectors.resolve_identity import pypi_purl
from conda_sentinel.collectors.resolve_identity import repository_from
from conda_sentinel.collectors.resolve_identity import resolution_for
from conda_sentinel.collectors.tasks import COLLECT_RESOLVE_IDENTITY_TASK_NAME
from conda_sentinel.collectors.tasks import resolve_identity as resolve_identity_task
from conda_sentinel.core.clock import FixedClock
from conda_sentinel.core.collection import CONDITIONAL_HEADERS
from conda_sentinel.core.collection import CollectorConfigurationError
from conda_sentinel.core.outcomes import OutcomeState
from conda_sentinel.core.queues import Queue
from conda_sentinel.core.queues import queue_for
from conda_sentinel.core.transport import MAX_TIMEOUT
from conda_sentinel.core.transport import worst_case_call_seconds
from conda_sentinel.identity.models import ESTABLISHED
from conda_sentinel.identity.models import IdentityConfidence
from conda_sentinel.identity.models import MappingKind
from conda_sentinel.identity.services import FEEDSTOCK_NAME_LENGTH
from tests.clocks import FIXED_INSTANT
from tests.source_scan import SRC_ROOT
from tests.source_scan import dotted_name
from tests.source_scan import parse

if TYPE_CHECKING:
    from pathlib import Path

#: The module the source sweeps at the foot of this file read.
RESOLUTION_MODULE: Final[str] = "django_apps/conda_sentinel/collectors/resolve_identity.py"

#: The ORM write methods a collector may not reach for (`CPM-AD-14`): identity is
#: written by `record_resolution` and evidence by the base.
WRITE_METHODS: Final[frozenset[str]] = frozenset(
    {"save", "create", "get_or_create", "update_or_create", "bulk_create", "bulk_update", "update", "delete"},
)

#: The package the cases ask about.
A_NAME: Final[str] = "requests"
THE_INDEX_LOCATOR: Final[str] = (
    "https://raw.githubusercontent.com/conda-forge/feedstock-outputs/main/outputs/r/e/q/requests.json"
)
THE_PROJECT_LOCATOR: Final[str] = "https://pypi.org/pypi/requests/json"
THE_REPOSITORY: Final[str] = "https://github.com/psf/requests"

#: What the recorder is handed by, verbatim.
A_SOURCE_OF_IDENTITY: Final[str] = "inventory"
A_KEY: Final[str] = "watchlist:requests"

#: How much of the inherited soft limit one collection may spend.
SOFT_LIMIT_SHARE: Final[float] = 0.75

#: A primary key no row holds; this tier reads none.
A_PACKAGE: Final[int] = 7

#: Counts the cases assert, named because `PLR2004` is right about a bare number.
TWO_FEEDSTOCKS: Final[int] = 2
ONE_TRANSACTION: Final[int] = 1


def _identity(name: str = A_NAME) -> PackageIdentity:
    """Return what the package row says, for the cases that build a resolution.

    Args:
        name: The canonical name.

    Returns:
        The identity.

    """
    return PackageIdentity(canonical_name=name, identity_source=A_SOURCE_OF_IDENTITY, associator_key=A_KEY)


def _project(*, found: bool = True, asked: bool = True, urls: dict[str, str] | None = None) -> ProjectAnswer:
    """Return what the bounded second call might have come back with.

    Args:
        found: Whether PyPI holds the project.
        asked: Whether a readable answer was obtained at all.
        urls: The project's URLs, when found.

    Returns:
        The answer, with a detail sentence on every path that is not a found
        project.

    """
    if found:
        return ProjectAnswer(asked=True, found=True, project_urls=urls or {}, source=THE_PROJECT_LOCATOR, detail="")
    if asked:
        return ProjectAnswer(
            asked=True,
            found=False,
            project_urls={},
            source=THE_PROJECT_LOCATOR,
            detail=f"{NO_PYPI_PROJECT_DETAIL}: {THE_PROJECT_LOCATOR} reports that the project does not exist",
        )
    return ProjectAnswer(
        asked=False,
        found=False,
        project_urls={},
        source=THE_PROJECT_LOCATOR,
        detail=f"{PYPI_UNREADABLE_DETAIL}: {THE_PROJECT_LOCATOR} could not be read",
    )


def _index(*names: str) -> str:
    """Return the body conda-forge's index serves for a package.

    Args:
        *names: The feedstocks it lists.

    Returns:
        The JSON body.

    """
    return json.dumps({"feedstocks": list(names)})


def _document(project_urls: object = None, **info: Any) -> str:
    """Return the body PyPI serves for a project.

    Args:
        project_urls: What `info.project_urls` holds.
        **info: Anything else under `info`.

    Returns:
        The JSON body.

    """
    return json.dumps({"info": {"project_urls": project_urls, **info}})


def _collector() -> IdentityResolutionCollector:
    """Return a collector built from its declarations alone.

    Returns:
        The collector, with a stopped clock.

    """
    return IdentityResolutionCollector(clock=FixedClock(instant=FIXED_INSTANT))


def _resolution_module() -> Path:
    """Return the collector module this file's source sweeps read.

    Returns:
        Its path, resolved from `SRC_ROOT`.

    """
    return SRC_ROOT / RESOLUTION_MODULE


# ---------------------------------------------------------------------------
# The declarations, and the arithmetic behind them.
# ---------------------------------------------------------------------------


def test_the_collector_declares_every_value_the_base_checks() -> None:
    """All nine, written out on the class and carrying the module's own constants."""
    declared = vars(IdentityResolutionCollector)

    assert IdentityResolutionCollector.name == COLLECTOR_NAME
    assert IdentityResolutionCollector.evidence_model is IdentityResolutionSnapshot
    assert IdentityResolutionCollector.observation_window == RESOLUTION_OBSERVATION_WINDOW
    assert IdentityResolutionCollector.timeout == RESOLUTION_TIMEOUT
    assert IdentityResolutionCollector.retries == RESOLUTION_RETRIES
    assert IdentityResolutionCollector.rate_limit == RESOLUTION_RATE_LIMIT
    assert IdentityResolutionCollector.headers == RESOLUTION_HEADERS
    assert IdentityResolutionCollector.freshness_target == RESOLUTION_FRESHNESS_TARGET
    assert IdentityResolutionCollector.response_cache_ttl == RESOLUTION_CACHE_TTL
    assert IdentityResolutionCollector.cadence is RESOLUTION_CADENCE
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
        "cadence",
    } <= set(declared)


def test_the_cadence_is_daily_and_the_targets_are_derived_from_it() -> None:
    """Daily, because the three sweeps that select on what this records are daily.

    The target is `cadence x (1 + tolerated_missed_runs)`, strictly greater than
    the cadence; the window is shorter than the cadence so a scheduled run is
    never suppressed; the cache outlives the cadence so a scheduled collection
    revalidates rather than re-transfers.
    """
    assert timedelta(days=1) == RESOLUTION_CADENCE
    assert RESOLUTION_FRESHNESS_TARGET == RESOLUTION_CADENCE * (1 + TOLERATED_MISSED_RUNS)
    assert RESOLUTION_FRESHNESS_TARGET > RESOLUTION_CADENCE
    assert timedelta(0) < RESOLUTION_OBSERVATION_WINDOW < RESOLUTION_CADENCE
    assert RESOLUTION_CACHE_TTL > RESOLUTION_CADENCE


def test_one_whole_collection_fits_inside_the_inherited_soft_limit_with_room_to_spare() -> None:
    """Two retried calls, against the settings module's limit.

    The second call reaches the same transport the base built, which mounts the
    collector's own retry policy -- so it is retried exactly as the first is, and
    the ceiling is twice the computed worst case, not the worst case plus one
    un-retried connect and read.
    """
    worst_case = worst_case_call_seconds(timeout=RESOLUTION_TIMEOUT, retries=RESOLUTION_RETRIES)
    soft_limit = settings.CELERY_TASK_SOFT_TIME_LIMIT

    assert 2 * worst_case <= SOFT_LIMIT_SHARE * soft_limit
    assert RESOLUTION_TIMEOUT <= MAX_TIMEOUT


def test_the_declared_headers_carry_the_shared_identity_and_nothing_conditional() -> None:
    """Headers reach the socket only through the base (`CPM-AD-20`, `CPM-AD-27`)."""
    lowered = {name.lower(): value for name, value in RESOLUTION_HEADERS.items()}

    assert lowered["user-agent"] == USER_AGENT
    assert lowered["accept"] == "application/json"
    assert set(lowered).isdisjoint({header.lower() for header in CONDITIONAL_HEADERS})


def test_the_declared_allowance_is_a_courtesy_bound_a_collection_fits_inside() -> None:
    """Neither host states a ceiling, so the bound is declared rather than unlimited by omission."""
    assert RESOLUTION_RATE_LIMIT.calls >= 1 + RESOLUTION_RETRIES
    assert RESOLUTION_RATE_LIMIT.per == timedelta(minutes=1)
    assert RESOLUTION_RATE_LIMIT.calls == pypi_release.PYPI_RELEASE_RATE_LIMIT.calls


def test_the_collector_is_constructed_from_its_declarations_alone() -> None:
    """The base's nine refusals, run against the real class rather than a fixture."""
    collector = _collector()

    try:
        assert collector.request_cost == 1 + RESOLUTION_RETRIES
    finally:
        collector.close()


def test_the_task_name_routes_to_the_collect_queue_and_is_the_collectors_own() -> None:
    """`cpm.collect.*` is what puts external I/O on the `collect` queue, and the dispatch derives the suffix."""
    assert queue_for(COLLECT_RESOLVE_IDENTITY_TASK_NAME) == Queue.COLLECT
    assert COLLECT_RESOLVE_IDENTITY_TASK_NAME.endswith(COLLECTOR_NAME)
    assert resolve_identity_task.name == COLLECT_RESOLVE_IDENTITY_TASK_NAME


def test_the_hosts_and_grammar_agree_with_the_siblings_that_read_the_same_sources() -> None:
    """Restating them is right under `CPM-AD-7`; nothing else would have compared them."""
    assert PYPI_HOST == pypi_release.PYPI_HOST
    assert MAX_DOCUMENT_CHARACTERS == pypi_release.MAX_DOCUMENT_CHARACTERS
    assert GITHUB_WEB_HOSTS == source_release.GITHUB_WEB_HOSTS
    assert FEEDSTOCK_OUTPUTS_HOST == "raw.githubusercontent.com"


# ---------------------------------------------------------------------------
# The two locators.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "shard", "file"),
    [
        ("numpy", "n/u/m", "numpy"),
        ("qt", "q/t/z", "qt"),
        ("r", "r/z/z", "r"),
        ("zope.interface", "z/o/p", "zope.interface"),
        ("r-base", "r/b/a", "r-base"),
        ("_libgcc_mutex", "l/i/b", "_libgcc_mutex"),
        ("NumPy", "n/u/m", "numpy"),
    ],
)
def test_the_index_locator_shards_by_the_first_three_alphanumerics_padded_with_z(
    name: str,
    shard: str,
    file: str,
) -> None:
    """conda-forge's own sharding: three alphanumeric directories, `z` where the name runs out.

    Args:
        name: The canonical name.
        shard: The three directories expected.
        file: The file name expected, lower-cased.

    """
    assert index_locator(name) == (
        f"https://raw.githubusercontent.com/conda-forge/feedstock-outputs/main/outputs/{shard}/{file}.json"
    )


@pytest.mark.parametrize("name", ["", "   ", None, 7, "..", ".", "a/b", "a b", "-leading", "über"])
def test_a_name_the_index_could_not_hold_is_refused_rather_than_encoded(name: Any) -> None:
    """Refused before the window and the allowance, so the run fails naming the package.

    Args:
        name: A name that is not a conda package name.

    """
    with pytest.raises(ResolutionLocatorError):
        index_locator(name)


@pytest.mark.parametrize(
    "name",
    ["requests", "Zope.Interface", "zope_interface", "zope-interface", "Django", "ruamel.yaml", "a"],
)
def test_the_pep_503_normalisation_is_the_one_the_pypi_collector_applies_to_a_purl(name: str) -> None:
    """The two spellings of one rule, reconciled: a purl built from the name reaches the same project.

    Args:
        name: A canonical name.

    """
    assert normalised_name(name) == pypi_release.project_name(f"pkg:pypi/{name}")
    assert project_locator(name) == pypi_release.project_locator(f"pkg:pypi/{name}")
    assert pypi_release.project_name(pypi_purl(name)) == normalised_name(name)


@pytest.mark.parametrize("name", ["", "  ", None, "_libgcc_mutex", "-x", "x-", "a/b"])
def test_a_name_pypi_could_not_hold_is_refused_by_the_project_locator(name: Any) -> None:
    """A conda name that is not a PyPI name has no project to find, and says so.

    Args:
        name: A name that is not a PyPI project name once normalised.

    """
    with pytest.raises(ResolutionLocatorError):
        project_locator(name)


def test_a_locator_wider_than_the_column_that_records_it_is_refused() -> None:
    """A conda name is bounded by nothing this collector reads, so the locator's width is checked here."""
    with pytest.raises(ResolutionLocatorError, match="source column"):
        index_locator("a" * 600)


def test_a_repository_that_normalises_wider_than_its_column_is_not_a_repository() -> None:
    """`R-5`: SQLite would store it and PostgreSQL would refuse it at insert, after the call was spent."""
    assert normalised_repository(f"https://github.com/psf/{'r' * 600}") is None
    assert "column that holds it" in repository_from({"Source": f"https://github.com/psf/{'r' * 600}"}).detail


def test_the_golden_example_reaches_both_golden_locators_and_both_purls() -> None:
    """One package, two locators, two purls -- the spellings the integration tier depends on."""
    assert index_locator(A_NAME) == THE_INDEX_LOCATOR
    assert project_locator(A_NAME) == THE_PROJECT_LOCATOR
    assert pypi_purl(A_NAME) == "pkg:pypi/requests"
    assert conda_purl("Requests") == "pkg:conda/requests"


# ---------------------------------------------------------------------------
# The repository: normalisation, and which key wins.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://github.com/psf/requests", THE_REPOSITORY),
        ("https://github.com/psf/requests.git", THE_REPOSITORY),
        ("https://github.com/psf/requests/", THE_REPOSITORY),
        ("https://github.com/psf/requests/tree/main/src", THE_REPOSITORY),
        ("https://github.com/psf/requests/blob/main/README.md", THE_REPOSITORY),
        ("https://www.github.com/psf/requests", THE_REPOSITORY),
        ("http://github.com/psf/requests", THE_REPOSITORY),
        ("https://github.com/PSF/Requests", THE_REPOSITORY),
        ("  https://github.com/psf/requests?tab=readme#top  ", THE_REPOSITORY),
        ("https://GitHub.com/psf/requests.git/", THE_REPOSITORY),
    ],
)
def test_a_readable_repository_url_normalises_to_the_one_form_the_release_collector_reads(
    url: str,
    expected: str,
) -> None:
    """The table the story asks for, and the result is accepted by `source_release`'s own rule.

    Args:
        url: What a project's `project_urls` might say.
        expected: The one spelling stored.

    """
    normalised = normalised_repository(url)

    assert normalised == expected
    assert source_release._repository_segments(normalised) == ("psf", "requests")  # noqa: SLF001 - the acceptance rule under test


@pytest.mark.parametrize(
    "url",
    [
        "https://gitlab.com/x/y",
        "https://github.com/only-owner",
        "https://github.com/psf/requests/issues",
        "https://github.com",
        "ftp://github.com/psf/requests",
        "github.com/psf/requests",
        "https://github.com/../requests",
        "https://github.com/psf/..git",
        "https://github.com/psf/",
        "https://[::1/psf/requests",
        "",
        "   ",
        None,
    ],
)
def test_a_url_that_is_not_a_github_repository_is_none_rather_than_repaired(url: Any) -> None:
    """Refused rather than guessed at, and never a URL `source_release` would then refuse.

    Args:
        url: A value that names no repository this product reads.

    """
    assert normalised_repository(url) is None


def test_the_precedence_is_the_documented_one_and_source_code_beats_homepage() -> None:
    """The matrix's precedence row: `Source Code` wins over a `Homepage` that is not a repository."""
    chosen = repository_from({"Homepage": "https://pyyaml.org/", "Source Code": "https://github.com/yaml/pyyaml"})

    assert chosen.key == "Source Code"
    assert chosen.normalised == "https://github.com/yaml/pyyaml"
    assert chosen.detail == ""
    assert REPOSITORY_PRECEDENCE == ("Source", "Source Code", "Repository", "Code", "GitHub")


def test_an_earlier_key_wins_over_a_later_one_however_the_document_orders_them() -> None:
    """`Source` before `Source Code` before `Repository`, matched case-insensitively on the stripped key."""
    chosen = repository_from(
        {
            "Repository": "https://github.com/c/c",
            " source code ": "https://github.com/b/b",
            "SOURCE": "https://github.com/a/a",
        },
    )

    assert chosen.key == "SOURCE"
    assert chosen.normalised == "https://github.com/a/a"


def test_homepage_is_used_only_when_no_higher_key_is_present_and_it_is_a_repository() -> None:
    """The one conditional key: consulted last and only counts when it normalises."""
    chosen = repository_from({HOMEPAGE_KEY: "https://github.com/psf/requests", "Documentation": "https://x.rtfd.io"})

    assert chosen.key == HOMEPAGE_KEY
    assert chosen.normalised == THE_REPOSITORY

    not_a_repository = repository_from({HOMEPAGE_KEY: "https://requests.readthedocs.io"})

    assert not_a_repository.key == ""
    assert not_a_repository.normalised == ""
    assert not_a_repository.detail.startswith(NO_REPOSITORY_DETAIL)
    assert "requests.readthedocs.io" in not_a_repository.detail


def test_a_key_outside_the_lists_never_wins_however_github_shaped_its_value() -> None:
    """A `Changelog` pointing into GitHub is not a source repository."""
    chosen = repository_from(
        {"Changelog": "https://github.com/psf/requests/blob/main/HISTORY.md", "Funding": "https://x.y"},
    )

    assert chosen == ChosenRepository(detail=chosen.detail)
    assert chosen.detail.startswith(NO_REPOSITORY_DETAIL)


@pytest.mark.parametrize("key", ["Source Code", "source-code", "Source_Code", "SOURCECODE", " source  code "])
def test_keys_are_matched_on_their_pep_753_label_the_way_pypi_reads_them(key: str) -> None:
    """`xarray` labels its repository `source-code`; PyPI treats that and `Source Code` as one label."""
    chosen = repository_from({"homepage": "https://xarray.dev/", key: "https://github.com/pydata/xarray"})

    assert chosen.key == key.strip()
    assert chosen.normalised == "https://github.com/pydata/xarray"
    assert normalised_label(key) == "sourcecode"


def test_a_repositorys_own_github_issue_tracker_names_it_when_nothing_else_does() -> None:
    """`sqlalchemy` publishes only an `Issue Tracker`, and it is the repository root; an `/issues` path counts too."""
    root = repository_from(
        {
            "Documentation": "https://docs.sqlalchemy.org",
            "Homepage": "https://www.sqlalchemy.org",
            "Issue Tracker": "https://github.com/sqlalchemy/sqlalchemy/",
        },
    )
    issues = repository_from({"Issues": "https://github.com/sqlalchemy/sqlalchemy/issues"})

    assert root.key == "Issue Tracker"
    assert root.normalised == "https://github.com/sqlalchemy/sqlalchemy"
    assert issues.normalised == "https://github.com/sqlalchemy/sqlalchemy"
    assert ISSUES_KEYS == ("Issues", "Issue Tracker", "Bug Tracker", "Tracker")


@pytest.mark.parametrize(
    "tracker",
    [
        "https://github.com/psf",
        "https://github.com/psf/requests/pulls",
        "https://gitlab.com/x/y/-/issues",
        "https://bugs.python.org/",
        "https://github.com/psf/requests/issues/42",
    ],
)
def test_an_issue_tracker_that_is_not_a_repository_or_its_own_github_issues_page_names_nothing(tracker: str) -> None:
    """Only `github.com/<owner>/<repo>` and its `/issues` page count; nothing else under the owner does."""
    chosen = repository_from({"Issues": tracker})

    assert chosen.normalised == ""
    assert chosen.detail.startswith(NO_REPOSITORY_DETAIL)


def test_a_higher_key_still_wins_over_an_issue_tracker_and_a_homepage_repository_beats_it() -> None:
    """The fallback is last: `Homepage` that normalises wins over `Issues`, and any precedence key wins over both."""
    homepage_first = repository_from(
        {"Issues": "https://github.com/a/a/issues", "Homepage": "https://github.com/b/b"},
    )
    source_first = repository_from(
        {
            "Issues": "https://github.com/a/a/issues",
            "Homepage": "https://github.com/b/b",
            "Source": "https://github.com/c/c",
        },
    )

    assert homepage_first.normalised == "https://github.com/b/b"
    assert source_first.normalised == "https://github.com/c/c"


def test_an_unreadable_source_link_is_recorded_as_rejected_with_the_url_and_why() -> None:
    """The matrix's unreadable row: the key won, its value did not, and a lower key is not tried instead."""
    chosen = repository_from({"Source": "https://gitlab.com/x/y", "Repository": "https://github.com/a/a"})

    assert chosen.key == "Source"
    assert chosen.url == "https://gitlab.com/x/y"
    assert chosen.normalised == ""
    assert chosen.detail.startswith(NO_REPOSITORY_DETAIL)
    assert "https://gitlab.com/x/y" in chosen.detail
    assert "gitlab.com" in chosen.detail


def test_two_keys_that_collapse_to_one_are_read_in_document_order() -> None:
    """`Source` and ` source ` are one key after stripping and lower-casing; the first occurrence wins."""
    chosen = repository_from({" Source ": "https://github.com/a/a", "source": "https://github.com/b/b"})

    assert chosen.key == "Source"
    assert chosen.normalised == "https://github.com/a/a"

    read = project_urls_in(
        json.dumps(
            {"info": {"project_urls": {"Source ": "https://github.com/a/a", "Source": "https://github.com/b/b"}}}
        ),
        source=THE_PROJECT_LOCATOR,
    )

    assert dict(read) == {"Source": "https://github.com/a/a"}


def test_a_project_declaring_no_urls_chooses_nothing_and_says_so() -> None:
    """Empty in, a reason out."""
    chosen = repository_from({})

    assert chosen.normalised == ""
    assert chosen.key == ""
    assert NO_REPOSITORY_DETAIL in chosen.detail


# ---------------------------------------------------------------------------
# The two document readers.
# ---------------------------------------------------------------------------


def test_an_index_entry_is_read_into_the_feedstocks_it_lists_in_order() -> None:
    """Two listed, two read, stripped and lower-cased, once each."""
    assert feedstocks_in(_index("a", " B ", "a"), source=THE_INDEX_LOCATOR) == ("a", "b")
    assert feedstocks_in(_index(), source=THE_INDEX_LOCATOR) == ()


def test_two_spellings_of_one_repository_are_one_feedstock() -> None:
    """`x` and `x-feedstock` name one repository, and two mappings sharing a name is a recorder refusal."""
    assert feedstocks_in(_index("x", "x-feedstock", "y-feedstock", "y"), source=THE_INDEX_LOCATOR) == (
        "x",
        "y-feedstock",
    )


@pytest.mark.parametrize(
    "body",
    [
        json.dumps({"feedstocks": "x"}),
        json.dumps({"feedstocks": None}),
        json.dumps({}),
        json.dumps({"feedstocks": [7]}),
        json.dumps({"feedstocks": [""]}),
        json.dumps({"feedstocks": ["a/b"]}),
        json.dumps({"feedstocks": ["a" * (FEEDSTOCK_NAME_LENGTH + 1)]}),
        json.dumps(["a"]),
        "not json",
        "[" * 100_000 + "]" * 100_000,
    ],
)
def test_an_index_entry_whose_shape_has_changed_is_refused_rather_than_read_past(body: str) -> None:
    """The matrix's malformed row: `ResolutionDocumentError`, which the base turns into an `error` row.

    Args:
        body: A body that is not an index entry.

    """
    with pytest.raises(ResolutionDocumentError) as refused:
        feedstocks_in(body, source=THE_INDEX_LOCATOR)

    assert THE_INDEX_LOCATOR in str(refused.value)


def test_a_feedstock_name_exactly_as_wide_as_the_column_once_suffixed_is_read() -> None:
    """The bound is on the repository the row records, suffix included."""
    name = "a" * (FEEDSTOCK_NAME_LENGTH - len("-feedstock"))

    assert feedstocks_in(_index(name), source=THE_INDEX_LOCATOR) == (name,)


def test_a_document_past_the_decode_bound_is_refused_before_it_is_parsed(monkeypatch: pytest.MonkeyPatch) -> None:
    """The bound protects the parse; lowered here so the case does not build 32 MiB to prove a comparison."""
    monkeypatch.setattr(resolve_identity, "MAX_DOCUMENT_CHARACTERS", 8)

    with pytest.raises(ResolutionDocumentError):
        feedstocks_in(_index("numpy"), source=THE_INDEX_LOCATOR)
    with pytest.raises(ResolutionDocumentError):
        project_urls_in(_document({}), source=THE_PROJECT_LOCATOR)


def test_a_project_document_is_read_into_its_urls_with_blank_and_mistyped_values_dropped() -> None:
    """Only `info.project_urls` is read; a null or absent one is a project declaring none."""
    read = project_urls_in(
        _document({"Source": " https://github.com/psf/requests ", "Blank": "", "Odd": 7, " Home ": "https://x.y"}),
        source=THE_PROJECT_LOCATOR,
    )

    assert dict(read) == {"Source": "https://github.com/psf/requests", "Home": "https://x.y"}
    assert dict(project_urls_in(_document(None), source=THE_PROJECT_LOCATOR)) == {}
    assert dict(project_urls_in(json.dumps({"info": {}}), source=THE_PROJECT_LOCATOR)) == {}


@pytest.mark.parametrize(
    "body",
    [json.dumps({"info": "x"}), json.dumps({}), json.dumps({"info": {"project_urls": ["x"]}}), "{", json.dumps(7)],
)
def test_a_project_document_whose_shape_has_changed_is_refused(body: str) -> None:
    """Refused from the reader; the collector's second call turns it into `detail` rather than a failure.

    Args:
        body: A body that is not a project document.

    """
    with pytest.raises(ResolutionDocumentError):
        project_urls_in(body, source=THE_PROJECT_LOCATOR)


# ---------------------------------------------------------------------------
# resolution_for: what two answers establish.
# ---------------------------------------------------------------------------


def test_both_found_establishes_four_mappings_at_inventory_derived_and_corrects_no_name() -> None:
    """The matrix's first row, as the resolution the recorder is handed."""
    facts = resolution_for(
        identity=_identity(),
        feedstocks=("requests",),
        project=_project(urls={"Source": THE_REPOSITORY}),
        chosen=repository_from({"Source": THE_REPOSITORY}),
        prior_feedstock_rows=False,
    )
    resolution = facts.resolution

    assert resolution.identity_source == A_SOURCE_OF_IDENTITY
    assert resolution.associator_key == A_KEY
    assert resolution.confidence == IdentityConfidence.INVENTORY_DERIVED.value
    assert resolution.canonical_name == ""
    assert resolution.source_repository_url == THE_REPOSITORY
    assert resolution.primary_purl == "pkg:pypi/requests"
    assert resolution.primary_type == "pypi"
    assert resolution.conda_purl == "pkg:conda/requests"
    assert [feedstock.name for feedstock in resolution.feedstocks] == ["requests-feedstock"]
    assert resolution.feedstocks[0].url == "https://github.com/conda-forge/requests-feedstock"
    assert resolution.outcomes == {
        MappingKind.SOURCE_REPOSITORY.value: ESTABLISHED,
        MappingKind.RELEASE_ECOSYSTEM.value: ESTABLISHED,
        MappingKind.CONDA_ARTIFACT.value: ESTABLISHED,
        MappingKind.FEEDSTOCK.value: ESTABLISHED,
        MappingKind.CROSS_ECOSYSTEM.value: OutcomeState.NOT_FOUND.value,
    }
    assert facts.repository_key == "Source"
    assert facts.repository_url == THE_REPOSITORY
    assert facts.pypi_found is True
    assert facts.feedstocks == ("requests",)
    assert facts.detail == ""


def test_every_mapping_kind_is_answered_on_every_path() -> None:
    """`CPM-FR-1`'s five kinds, once each, whatever the two sources said."""
    for project in (_project(), _project(found=False), _project(found=False, asked=False)):
        for feedstocks, prior in ((("a",), False), ((), False), ((), True)):
            facts = resolution_for(
                identity=_identity(),
                feedstocks=feedstocks,
                project=project,
                chosen=ChosenRepository(),
                prior_feedstock_rows=prior,
            )

            assert set(facts.resolution.outcomes) == set(MappingKind.values)


def test_index_listing_two_feedstocks_names_two_repositories_under_conda_forge() -> None:
    """The matrix's two-feedstock row."""
    facts = resolution_for(
        identity=_identity(),
        feedstocks=("a", "b"),
        project=_project(found=False),
        chosen=ChosenRepository(),
        prior_feedstock_rows=False,
    )

    assert len(facts.resolution.feedstocks) == TWO_FEEDSTOCKS
    assert [feedstock.name for feedstock in facts.resolution.feedstocks] == ["a-feedstock", "b-feedstock"]
    assert all(feedstock.url.startswith("https://github.com/conda-forge/") for feedstock in facts.resolution.feedstocks)
    assert facts.resolution.outcomes[MappingKind.FEEDSTOCK.value] == ESTABLISHED
    assert facts.resolution.outcomes[MappingKind.CONDA_ARTIFACT.value] == ESTABLISHED


def test_no_pypi_project_records_two_not_founds_and_the_feedstock_still_established() -> None:
    """The matrix's PyPI-404 row: the informative negative on both PyPI-derived mappings."""
    facts = resolution_for(
        identity=_identity(),
        feedstocks=("requests",),
        project=_project(found=False),
        chosen=ChosenRepository(),
        prior_feedstock_rows=False,
    )
    outcomes = facts.resolution.outcomes

    assert outcomes[MappingKind.RELEASE_ECOSYSTEM.value] == OutcomeState.NOT_FOUND.value
    assert outcomes[MappingKind.SOURCE_REPOSITORY.value] == OutcomeState.NOT_FOUND.value
    assert outcomes[MappingKind.FEEDSTOCK.value] == ESTABLISHED
    assert facts.resolution.confidence == IdentityConfidence.INVENTORY_DERIVED.value
    assert facts.resolution.primary_purl == ""
    assert facts.pypi_found is False
    assert NO_PYPI_PROJECT_DETAIL in facts.detail


def test_pypi_that_could_not_be_asked_records_error_rather_than_an_absence_nobody_established() -> None:
    """`CPM-FR-6`: "could not ask" is `error`, and it is not the informative negative."""
    facts = resolution_for(
        identity=_identity(),
        feedstocks=("requests",),
        project=_project(found=False, asked=False),
        chosen=ChosenRepository(),
        prior_feedstock_rows=False,
    )
    outcomes = facts.resolution.outcomes

    assert outcomes[MappingKind.RELEASE_ECOSYSTEM.value] == OutcomeState.ERROR.value
    assert outcomes[MappingKind.SOURCE_REPOSITORY.value] == OutcomeState.ERROR.value
    assert PYPI_UNREADABLE_DETAIL in facts.detail


def test_pypi_that_could_not_be_asked_re_asserts_what_an_earlier_run_established() -> None:
    """A transient failure never lowers an established mapping: the stored value is carried forward."""
    established = PackageIdentity(
        canonical_name=A_NAME,
        identity_source=A_SOURCE_OF_IDENTITY,
        associator_key=A_KEY,
        release_outcome=ESTABLISHED,
        source_outcome=ESTABLISHED,
        primary_purl="pkg:pypi/requests",
        primary_type="pypi",
        source_repository_url=THE_REPOSITORY,
    )

    facts = resolution_for(
        identity=established,
        feedstocks=(),
        project=_project(found=False, asked=False),
        chosen=ChosenRepository(),
        prior_feedstock_rows=False,
    )

    outcomes = facts.resolution.outcomes
    assert outcomes[MappingKind.RELEASE_ECOSYSTEM.value] == ESTABLISHED
    assert outcomes[MappingKind.SOURCE_REPOSITORY.value] == ESTABLISHED
    assert facts.resolution.primary_purl == "pkg:pypi/requests"
    assert facts.resolution.primary_type == "pypi"
    assert facts.resolution.source_repository_url == THE_REPOSITORY
    assert facts.resolution.confidence == IdentityConfidence.INVENTORY_DERIVED.value
    assert facts.repository_url == THE_REPOSITORY
    assert CARRIED_FORWARD_DETAIL in facts.detail
    assert PYPI_UNREADABLE_DETAIL in facts.detail

    half = PackageIdentity(
        canonical_name=A_NAME,
        identity_source=A_SOURCE_OF_IDENTITY,
        associator_key=A_KEY,
        release_outcome=ESTABLISHED,
        source_outcome=OutcomeState.NOT_FOUND.value,
        primary_purl="pkg:pypi/requests",
        primary_type="pypi",
    )
    partial = resolution_for(
        identity=half,
        feedstocks=(),
        project=_project(found=False, asked=False),
        chosen=ChosenRepository(),
        prior_feedstock_rows=False,
    )

    assert partial.resolution.outcomes[MappingKind.RELEASE_ECOSYSTEM.value] == ESTABLISHED
    assert partial.resolution.outcomes[MappingKind.SOURCE_REPOSITORY.value] == OutcomeState.ERROR.value
    assert partial.resolution.source_repository_url == ""


def test_a_readable_pypi_answer_decides_the_mappings_afresh_rather_than_carrying_forward() -> None:
    """A real `404` is the informative negative even for a package an earlier run established."""
    established = PackageIdentity(
        canonical_name=A_NAME,
        identity_source=A_SOURCE_OF_IDENTITY,
        associator_key=A_KEY,
        release_outcome=ESTABLISHED,
        source_outcome=ESTABLISHED,
        primary_purl="pkg:pypi/requests",
        primary_type="pypi",
        source_repository_url=THE_REPOSITORY,
    )

    facts = resolution_for(
        identity=established,
        feedstocks=("requests",),
        project=_project(found=False),
        chosen=ChosenRepository(),
        prior_feedstock_rows=False,
    )

    assert facts.resolution.outcomes[MappingKind.RELEASE_ECOSYSTEM.value] == OutcomeState.NOT_FOUND.value
    assert facts.resolution.outcomes[MappingKind.SOURCE_REPOSITORY.value] == OutcomeState.NOT_FOUND.value
    assert facts.resolution.primary_purl == ""
    assert CARRIED_FORWARD_DETAIL not in facts.detail


def test_nothing_established_claims_unmapped_because_confidence_must_be_earned() -> None:
    """The matrix's nothing-established row, with both absences in `detail`."""
    facts = resolution_for(
        identity=_identity(),
        feedstocks=(),
        project=_project(found=False),
        chosen=ChosenRepository(),
        prior_feedstock_rows=False,
    )

    assert facts.resolution.confidence == IdentityConfidence.UNMAPPED.value
    assert facts.resolution.outcomes[MappingKind.FEEDSTOCK.value] == OutcomeState.NOT_FOUND.value
    assert facts.resolution.outcomes[MappingKind.CONDA_ARTIFACT.value] == OutcomeState.NOT_FOUND.value
    assert facts.resolution.conda_purl == ""
    assert facts.resolution.feedstocks == ()
    assert NO_PYPI_PROJECT_DETAIL in facts.detail
    assert INDEX_LISTS_NONE_DETAIL in facts.detail
    assert PRIOR_ROWS_KEPT_DETAIL not in facts.detail


def test_an_index_gone_silent_keeps_an_established_mapping_for_a_package_with_rows_and_says_so() -> None:
    """`FEEDSTOCK` is `not_found` only when the package holds no rows; the recorder removes none."""
    facts = resolution_for(
        identity=_identity(),
        feedstocks=(),
        project=_project(found=False),
        chosen=ChosenRepository(),
        prior_feedstock_rows=True,
    )

    assert facts.resolution.outcomes[MappingKind.FEEDSTOCK.value] == ESTABLISHED
    assert facts.resolution.outcomes[MappingKind.CONDA_ARTIFACT.value] == ESTABLISHED
    assert facts.resolution.conda_purl == "pkg:conda/requests"
    assert facts.resolution.feedstocks == ()
    assert facts.resolution.confidence == IdentityConfidence.INVENTORY_DERIVED.value
    assert PRIOR_ROWS_KEPT_DETAIL in facts.detail


def test_a_rejected_repository_link_is_a_not_found_source_repository_with_the_reason_recorded() -> None:
    """The matrix's unreadable-URL row, on the resolution and on the row."""
    chosen = repository_from({"Source": "https://github.com/only-owner"})
    facts = resolution_for(
        identity=_identity(),
        feedstocks=("requests",),
        project=_project(urls={"Source": "https://github.com/only-owner"}),
        chosen=chosen,
        prior_feedstock_rows=False,
    )

    assert facts.resolution.outcomes[MappingKind.SOURCE_REPOSITORY.value] == OutcomeState.NOT_FOUND.value
    assert facts.resolution.source_repository_url == ""
    assert facts.repository_key == "Source"
    assert "https://github.com/only-owner" in facts.detail
    assert facts.resolution.outcomes[MappingKind.RELEASE_ECOSYSTEM.value] == ESTABLISHED


# ---------------------------------------------------------------------------
# The sentinel row, the selection, and the remembered state.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("state", [OutcomeState.ERROR, OutcomeState.NOT_FOUND, OutcomeState.NOT_APPLICABLE])
def test_a_sentinel_row_carries_the_state_and_no_fact(state: OutcomeState) -> None:
    """Every column the recorder would have filled is blank, because the recorder was never reached.

    Args:
        state: The sentinel the base decided on.

    """
    collector = _collector()
    try:
        row = collector.sentinel_evidence(state=state, package_id=A_PACKAGE, observed_at=FIXED_INSTANT, detail="why")
    finally:
        collector.close()

    assert isinstance(row, IdentityResolutionSnapshot)
    assert row.state == state.value
    assert row.package_id == A_PACKAGE
    assert row.observed_at == FIXED_INSTANT
    assert row.source == ""
    assert (row.repository_url, row.repository_key, row.confidence_recorded) == ("", "", "")
    assert (row.pypi_asked, row.pypi_found, row.pypi_source) == (False, False, "")
    assert (row.downgrade_refused, row.feedstocks) == (False, [])
    assert row.detail.startswith("why")


def test_a_not_found_row_says_the_index_had_no_entry_and_pypi_was_never_asked() -> None:
    """The stated cost of asking the index first, on the row rather than in a docstring."""
    collector = _collector()
    try:
        row = collector.sentinel_evidence(
            state=OutcomeState.NOT_FOUND,
            package_id=A_PACKAGE,
            observed_at=FIXED_INSTANT,
            detail="the base's reason",
        )
    finally:
        collector.close()

    assert row.detail.startswith("the base's reason")
    assert "PyPI was not asked" in row.detail
    assert "next cadence" in row.detail


@pytest.mark.parametrize("state", [OutcomeState.OK, OutcomeState.UNKNOWN])
def test_a_sentinel_state_this_collector_has_no_row_for_is_refused(state: OutcomeState) -> None:
    """`ok` is a recorded resolution and `unknown` is no row at all; neither is a sentinel.

    Args:
        state: A state the base never asks for.

    """
    collector = _collector()
    try:
        with pytest.raises(CollectorConfigurationError):
            collector.sentinel_evidence(state=state, package_id=A_PACKAGE, observed_at=FIXED_INSTANT, detail="")
    finally:
        collector.close()


def test_the_selection_is_every_package_not_verified_as_a_lazy_query_over_the_package_table() -> None:
    """A keyword to `exclude`, not a comparison (the gate audit), over `packages`, ordered by key."""
    selected = IdentityResolutionCollector.selectable_packages()

    assert selected.model.__name__ == "Package"  # type: ignore[union-attr]
    assert selected.query.order_by == ("pk",)  # type: ignore[union-attr]
    sql = str(selected.query)  # type: ignore[union-attr]
    assert "confidence" in sql
    assert "NOT" in sql
    assert IdentityConfidence.VERIFIED.value in sql
    assert "identity_source" in sql
    assert "associator_key" in sql


def test_an_instance_no_run_has_reached_remembers_nothing_and_the_question_applies() -> None:
    """The blanks a sentinel row carries when no locator was ever built, and the default applicability."""
    collector = _collector()
    try:
        assert collector._locator == ""  # noqa: SLF001 - the remembered state is the property under test
        assert collector._identity is None  # noqa: SLF001 - as above
        assert collector.inapplicability(package_id=A_PACKAGE) == ""
    finally:
        collector.close()


def test_an_unsaved_snapshot_renders_its_absences_rather_than_raising() -> None:
    """A debugger meets this row while it is being built."""
    described = str(IdentityResolutionSnapshot(state=OutcomeState.NOT_FOUND.value))

    assert "no package" in described
    assert "nothing recorded" in described
    assert described.count(" at ") == 1
    assert "never" in described


# ---------------------------------------------------------------------------
# The module's shape: what it may and may not reach.
# ---------------------------------------------------------------------------


def test_the_collector_module_writes_no_row_of_any_kind_itself() -> None:
    """`CPM-AD-14`: identity is written by `record_resolution` and evidence by the base."""
    tree = parse(_resolution_module())
    written = sorted(
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and dotted_name(node.func).rpartition(".")[2] in WRITE_METHODS
    )

    assert written == [], f"the collector reaches a table directly at lines {written}"


def test_the_collector_module_hands_its_findings_to_the_recorder_and_names_the_identity_it_reads() -> None:
    """The anti-vacuity half: the one write path is called, and the reads are the three identity models.

    `PackageMapping` is read as well as `Package` and `Feedstock`, because the
    carry-forward rule needs the two PyPI-derived outcomes as they currently stand.
    """
    tree = parse(_resolution_module())
    called = {dotted_name(node.func).rpartition(".")[2] for node in ast.walk(tree) if isinstance(node, ast.Call)}
    named = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}

    assert "record_resolution" in called
    assert {"Package", "Feedstock", "PackageMapping"} <= named


def test_the_collector_module_opens_exactly_one_transaction_and_it_is_inside_translate() -> None:
    """`CPM-AD-23`: one per package, around the recorder's write, never around the run recorder."""
    tree = parse(_resolution_module())
    opened = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and dotted_name(node.func).endswith("transaction.atomic")
    ]
    translate = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "translate")
    inside_translate = [node for node in ast.walk(translate) if node in opened]

    assert len(opened) == ONE_TRANSACTION
    assert inside_translate == opened
    assert "collection_run" not in {
        dotted_name(node.func).rpartition(".")[2] for node in ast.walk(tree) if isinstance(node, ast.Call)
    }


def test_the_collector_module_imports_no_other_collector() -> None:
    """`CPM-AD-7`: the shared pieces live in `collectors/agent.py` and `collectors/selection.py`."""
    imported = {
        node.module
        for node in ast.walk(parse(_resolution_module()))
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }

    assert not any(
        module.endswith(
            (
                ".source_release",
                ".pypi_release",
                ".feedstock",
                ".conda_package",
                ".vulnerability",
                ".kev",
                ".license",
                ".python_readiness",
                ".py314_verification",
                ".tasks",
            ),
        )
        for module in imported
    ), imported
    assert any(module.endswith(".collectors.agent") for module in imported)
    assert any(module.endswith(".collectors.selection") for module in imported)


def test_the_collector_module_reads_no_clock_and_compares_no_confidence() -> None:
    """`CPM-AD-26` and the gate audit, stated positively for this module."""
    tree = parse(_resolution_module())
    named = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    attributes = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}

    assert "timezone" not in named
    assert "now" not in attributes
    assert "print" not in named
    compared = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Compare)
        and any("IdentityConfidence" in ast.dump(operand) for operand in (node.left, *node.comparators))
    ]
    assert compared == []


def test_the_collector_module_declares_the_names_it_exports() -> None:
    """`__all__` is the module's contract, and both test tiers import through it."""
    assert set(resolve_identity.__all__) <= set(vars(resolve_identity))
