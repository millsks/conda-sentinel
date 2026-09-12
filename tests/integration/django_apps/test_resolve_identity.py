"""Identity resolution against real tables: what a run records, on the package and beside it.

`CPM-FR-1` is a claim about what a package row and its mapping rows say after a
resolver has run, and every one of those facts is only true or false once a run
exists. Which is why this module sits beside
`tests/unit/django_apps/test_resolve_identity.py`: the locators, the readers,
the precedence and `resolution_for` are decided before a run does, and are
asserted there.

**The acceptance criterion is asserted through the selections the other
collectors make.** "Then `source_release`, `pypi_release` and `feedstock` each
select it" is a claim about *their* `selectable_packages`, and a case that
stopped at "the mapping rows exist" would pass identically if those selections
read a different table. So every end-to-end case asks the real classes whether
they would now offer the package -- those three and `python_readiness`, which
selects on the same release-ecosystem mapping.

**Every fetch is scripted and its order asserted.** The index is the base's call
and PyPI is the bounded second one, and the whole design of this collector is
which of the two ends a run: the cases about an absent index assert that PyPI
was *never* asked, because that is the stated cost of the ordering.

**No socket is opened.** Every case substitutes the transport at the base's seam.
Every test here rolls back: `@pytest.mark.django_db` wraps each in a transaction.
`tests/integration/conftest.py` marks everything under `tests/integration/` as an
integration test; the marker is not re-applied by hand.
"""

from __future__ import annotations

import json
from datetime import datetime
from datetime import timedelta
from typing import TYPE_CHECKING
from typing import ClassVar
from typing import Final

import pytest
from django.db import IntegrityError
from django.db import transaction

from conda_sentinel.collectors import resolve_identity as resolve_identity_module
from conda_sentinel.collectors import tasks as collector_tasks
from conda_sentinel.collectors.feedstock import FeedstockCollector
from conda_sentinel.collectors.models import IDENTITY_RESOLUTION_FACTS_CONSTRAINT
from conda_sentinel.collectors.models import IdentityResolutionSnapshot
from conda_sentinel.collectors.pypi_release import PyPIReleaseCollector
from conda_sentinel.collectors.python_readiness import PythonReadinessCollector
from conda_sentinel.collectors.resolve_identity import CARRIED_FORWARD_DETAIL
from conda_sentinel.collectors.resolve_identity import COLLECTOR_NAME
from conda_sentinel.collectors.resolve_identity import INDEX_LISTS_NONE_DETAIL
from conda_sentinel.collectors.resolve_identity import NO_PYPI_PROJECT_DETAIL
from conda_sentinel.collectors.resolve_identity import PRIOR_ROWS_KEPT_DETAIL
from conda_sentinel.collectors.resolve_identity import PYPI_UNREADABLE_DETAIL
from conda_sentinel.collectors.resolve_identity import RESOLUTION_FRESHNESS_TARGET
from conda_sentinel.collectors.resolve_identity import RESOLUTION_HEADERS
from conda_sentinel.collectors.resolve_identity import IdentityResolutionCollector
from conda_sentinel.collectors.resolve_identity import ResolutionDocumentError
from conda_sentinel.collectors.resolve_identity import ResolutionLocatorError
from conda_sentinel.collectors.resolve_identity import index_locator
from conda_sentinel.collectors.resolve_identity import project_locator
from conda_sentinel.collectors.source_release import SourceReleaseCollector
from conda_sentinel.collectors.tasks import resolve_identity
from conda_sentinel.core.clock import Clock
from conda_sentinel.core.clock import FixedClock
from conda_sentinel.core.models import CollectionRun
from conda_sentinel.core.outcomes import OutcomeState
from conda_sentinel.core.runs import RunLedgerError
from conda_sentinel.core.runs import RunState
from conda_sentinel.core.transport import Payload
from conda_sentinel.core.transport import TransportError
from conda_sentinel.identity.models import ESTABLISHED
from conda_sentinel.identity.models import Feedstock
from conda_sentinel.identity.models import IdentityConfidence
from conda_sentinel.identity.models import MappingKind
from conda_sentinel.identity.models import Package
from conda_sentinel.identity.models import PackageMapping
from conda_sentinel.identity.services import ResolutionError
from conda_sentinel.identity.services import resolve_package_shell
from tests.clocks import FIXED_INSTANT
from tests.collectors import FixedLimiter
from tests.collectors import RecordingResponseCache
from tests.collectors import ScriptedTransport
from tests.collectors import recorded_payload

if TYPE_CHECKING:
    from conda_sentinel.core.collection import CollectionResult
    from conda_sentinel.core.rate_limit import RateLimiter
    from conda_sentinel.core.response_cache import ResponseCache
    from conda_sentinel.core.transport import Transport

#: The package the cases resolve, and the two locators its name produces.
A_NAME: Final[str] = "requests"
THE_INDEX_LOCATOR: Final[str] = index_locator(A_NAME)
THE_PROJECT_LOCATOR: Final[str] = project_locator(A_NAME)
THE_REPOSITORY: Final[str] = "https://github.com/psf/requests"
THE_FEEDSTOCK: Final[str] = "requests-feedstock"

#: What ingestion files the package under, which the recorder finds it by.
THE_IDENTITY_SOURCE: Final[str] = "inventory"
THE_KEY: Final[str] = "watchlist:requests"

#: Counts the cases assert, named because `PLR2004` is right about a bare number.
TWO_ROWS: Final[int] = 2
TWO_CALLS: Final[int] = 2
ONE_CALL: Final[int] = 1
FIVE_KINDS: Final[int] = len(MappingKind.values)

#: A gap longer than the observation window, so a second run is a re-observation.
A_DAY: Final[timedelta] = timedelta(days=1)

#: A primary key no row in this module holds.
NO_SUCH_PACKAGE: Final[int] = 9_999_999


def _index(*names: str) -> str:
    """Return the body conda-forge's index serves.

    Args:
        *names: The feedstocks it lists.

    Returns:
        The JSON body.

    """
    return json.dumps({"feedstocks": list(names)})


def _project(**urls: str) -> str:
    """Return the body PyPI serves for a project.

    Args:
        **urls: The project's `project_urls`, by key.

    Returns:
        The JSON body.

    """
    return json.dumps({"info": {"name": A_NAME, "version": "2.32.0", "project_urls": urls}})


def _answering(*, index: str | Payload | None = None, project: str | Payload | None = None) -> ScriptedTransport:
    """Return a transport answering the two locators with a body or a whole payload.

    Args:
        index: What the index answers, or `None` for a `404`.
        project: What PyPI answers, or `None` for a `404`.

    Returns:
        The scripted transport.

    """
    answers: dict[str, Payload] = {}
    for locator, answer in ((THE_INDEX_LOCATOR, index), (THE_PROJECT_LOCATOR, project)):
        if answer is None:
            answers[locator] = recorded_payload(source=locator, found=False, body="")
        elif isinstance(answer, Payload):
            answers[locator] = answer
        else:
            answers[locator] = recorded_payload(source=locator, body=answer)
    return ScriptedTransport(answers=answers)


def _a_shell(name: str = A_NAME, *, key: str = THE_KEY) -> Package:
    """Return a package as ingestion leaves it: `unmapped`, filed under its pair.

    Through `resolve_package_shell` rather than `Package.objects.create`, because
    what this module is about is the package *ingestion* made -- the row every
    real resolution starts from.

    Args:
        name: The canonical name.
        key: The associator key.

    Returns:
        The shell.

    """
    return resolve_package_shell(
        source_package_key=key,
        package_name=name,
        identity_source=THE_IDENTITY_SOURCE,
        clock=FixedClock(instant=FIXED_INSTANT),
    )


def _collect(
    package: Package,
    *,
    transport: ScriptedTransport,
    at: datetime = FIXED_INSTANT,
    force: bool = False,
    cache: RecordingResponseCache | None = None,
) -> CollectionResult:
    """Run one collection through a scripted transport.

    Args:
        package: The package to resolve.
        transport: The transport substituted at the base's seam (`CPM-AD-27`).
        at: The instant the run's clock is stopped at.
        force: Whether to bypass the observation window.
        cache: The response cache to use, or a fresh recording one.

    Returns:
        What the run did.

    """
    collector = IdentityResolutionCollector(
        clock=FixedClock(instant=at),
        transport=transport,
        limiter=FixedLimiter(permitted=True),
        response_cache=cache if cache is not None else RecordingResponseCache(),
    )
    try:
        return collector.collect(package_id=package.pk, force=force)
    finally:
        collector.close()


def _rows(package: Package) -> list[IdentityResolutionSnapshot]:
    """Return this package's snapshots, oldest first.

    Args:
        package: The package to read.

    Returns:
        The rows, ordered by primary key.

    """
    return list(IdentityResolutionSnapshot.objects.filter(package=package).order_by("pk"))


def _run(package: Package) -> CollectionRun:
    """Return the most recent ledger row for this collector and package.

    Args:
        package: The package the run was scoped to.

    Returns:
        The row.

    """
    return CollectionRun.objects.filter(collector=COLLECTOR_NAME, package=package).order_by("-pk").first()  # type: ignore[return-value]


def _outcomes(package: Package) -> dict[str, str]:
    """Return the package's mapping outcomes by kind.

    Args:
        package: The package to read.

    Returns:
        The outcomes.

    """
    return dict(PackageMapping.objects.filter(package=package).values_list("kind", "outcome"))


def _offered_by(
    collector: type[SourceReleaseCollector | PyPIReleaseCollector | FeedstockCollector | PythonReadinessCollector],
) -> set[int]:
    """Return the packages one of the mapping-selecting collectors would now offer.

    Args:
        collector: The collector class to ask.

    Returns:
        The primary keys.

    """
    selection = collector.selectable_packages()
    return set(selection) if selection is not None else set()


# ---------------------------------------------------------------------------
# AC 1: an unmapped package, two documents, three collectors that now select it.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_both_found_resolves_the_package_and_the_four_mapping_collectors_now_select_it() -> None:
    """The matrix's first row, end to end, and the acceptance criterion through the selections."""
    package = _a_shell()
    transport = _answering(index=_index("requests"), project=_project(Source=THE_REPOSITORY))

    result = _collect(package, transport=transport)

    assert result.state is RunState.SUCCEEDED
    assert transport.calls == [THE_INDEX_LOCATOR, THE_PROJECT_LOCATOR]
    assert all(
        headers is not None and headers["User-Agent"] == RESOLUTION_HEADERS["User-Agent"]
        for headers in transport.sent_headers
    )

    package.refresh_from_db()
    assert package.confidence == IdentityConfidence.INVENTORY_DERIVED.value
    assert package.source_repository_url == THE_REPOSITORY
    assert package.primary_purl == "pkg:pypi/requests"
    assert package.primary_type == "pypi"
    assert package.conda_purl == "pkg:conda/requests"
    assert package.canonical_name == A_NAME
    assert (package.identity_source, package.associator_key) == (THE_IDENTITY_SOURCE, THE_KEY)
    assert package.resolved_at == FIXED_INSTANT
    assert list(Feedstock.objects.filter(package=package).values_list("name", "url")) == [
        (THE_FEEDSTOCK, "https://github.com/conda-forge/requests-feedstock"),
    ]
    assert _outcomes(package) == {
        MappingKind.SOURCE_REPOSITORY.value: ESTABLISHED,
        MappingKind.RELEASE_ECOSYSTEM.value: ESTABLISHED,
        MappingKind.CONDA_ARTIFACT.value: ESTABLISHED,
        MappingKind.FEEDSTOCK.value: ESTABLISHED,
        MappingKind.CROSS_ECOSYSTEM.value: OutcomeState.NOT_FOUND.value,
    }

    (row,) = _rows(package)
    assert row.state == OutcomeState.OK.value
    assert row.source == THE_INDEX_LOCATOR
    assert row.repository_url == THE_REPOSITORY
    assert row.repository_key == "Source"
    assert row.pypi_asked is True
    assert row.pypi_found is True
    assert row.pypi_source == THE_PROJECT_LOCATOR
    assert row.feedstocks == ["requests"]
    assert row.confidence_recorded == IdentityConfidence.INVENTORY_DERIVED.value
    assert row.downgrade_refused is False
    assert row.detail == ""
    assert row.observed_at == FIXED_INSTANT
    assert _run(package).status == RunState.SUCCEEDED.value

    assert package.pk in _offered_by(SourceReleaseCollector)
    assert package.pk in _offered_by(PyPIReleaseCollector)
    assert package.pk in _offered_by(FeedstockCollector)
    assert package.pk in _offered_by(PythonReadinessCollector)


@pytest.mark.django_db
def test_no_pypi_project_records_two_informative_negatives_and_the_feedstock_still_established() -> None:
    """The matrix's PyPI-404 row: the second fetch's absence is a fact, not a failure."""
    package = _a_shell()
    transport = _answering(index=_index("requests"), project=None)

    result = _collect(package, transport=transport)

    assert result.state is RunState.SUCCEEDED
    assert transport.calls == [THE_INDEX_LOCATOR, THE_PROJECT_LOCATOR]
    package.refresh_from_db()
    assert package.confidence == IdentityConfidence.INVENTORY_DERIVED.value
    assert package.primary_purl == ""
    assert package.source_repository_url == ""
    assert package.conda_purl == "pkg:conda/requests"
    outcomes = _outcomes(package)
    assert outcomes[MappingKind.RELEASE_ECOSYSTEM.value] == OutcomeState.NOT_FOUND.value
    assert outcomes[MappingKind.SOURCE_REPOSITORY.value] == OutcomeState.NOT_FOUND.value
    assert outcomes[MappingKind.FEEDSTOCK.value] == ESTABLISHED
    (row,) = _rows(package)
    assert row.state == OutcomeState.OK.value
    assert row.pypi_found is False
    assert NO_PYPI_PROJECT_DETAIL in row.detail
    assert package.pk in _offered_by(FeedstockCollector)
    assert package.pk not in _offered_by(PyPIReleaseCollector)
    assert package.pk not in _offered_by(SourceReleaseCollector)


@pytest.mark.django_db
def test_an_absent_index_ends_the_run_at_the_bases_sentinel_and_pypi_is_never_asked() -> None:
    """The matrix's no-feedstock row, and the stated cost of asking the index first."""
    package = _a_shell()
    transport = _answering(index=None, project=_project(Source=THE_REPOSITORY))

    result = _collect(package, transport=transport)

    assert result.state is RunState.SUCCEEDED
    assert transport.calls == [THE_INDEX_LOCATOR]
    package.refresh_from_db()
    assert package.confidence == IdentityConfidence.UNMAPPED.value
    assert package.source_repository_url == ""
    assert _outcomes(package) == {}
    assert not Feedstock.objects.filter(package=package).exists()
    (row,) = _rows(package)
    assert row.state == OutcomeState.NOT_FOUND.value
    assert row.source == THE_INDEX_LOCATOR
    assert row.confidence_recorded == ""
    assert "PyPI was not asked" in row.detail
    assert _run(package).status == RunState.SUCCEEDED.value
    assert package.pk in set(IdentityResolutionCollector.selectable_packages())


@pytest.mark.django_db
def test_both_absent_is_indistinguishable_from_an_absent_index_because_pypi_is_never_reached() -> None:
    """The both-404 row: the second document's absence is never observed, and nothing is recorded."""
    package = _a_shell()
    transport = _answering(index=None, project=None)

    result = _collect(package, transport=transport)

    assert result.state is RunState.SUCCEEDED
    assert transport.calls == [THE_INDEX_LOCATOR]
    assert _outcomes(package) == {}
    assert _rows(package)[0].state == OutcomeState.NOT_FOUND.value


@pytest.mark.django_db
def test_an_index_that_lists_nothing_and_no_pypi_project_lands_at_unmapped_with_both_absences_recorded() -> None:
    """The matrix's nothing-established row: the recorder is reached and records that nothing was learned."""
    package = _a_shell()
    transport = _answering(index=_index(), project=None)

    result = _collect(package, transport=transport)

    assert result.state is RunState.SUCCEEDED
    package.refresh_from_db()
    assert package.confidence == IdentityConfidence.UNMAPPED.value
    assert package.resolved_at == FIXED_INSTANT
    outcomes = _outcomes(package)
    assert len(outcomes) == FIVE_KINDS
    assert set(outcomes.values()) == {OutcomeState.NOT_FOUND.value}
    (row,) = _rows(package)
    assert row.state == OutcomeState.OK.value
    assert row.confidence_recorded == IdentityConfidence.UNMAPPED.value
    assert row.feedstocks == []
    assert NO_PYPI_PROJECT_DETAIL in row.detail
    assert INDEX_LISTS_NONE_DETAIL in row.detail
    # The feedstock collector *does* select a `not_found` mapping: resolution
    # looked and found none, and confirming that absence against the conventional
    # repository and the staged-recipes queue is `CPM-FR-9`'s own branch. The two
    # release collectors do not, because neither has a locator to build.
    assert package.pk in _offered_by(FeedstockCollector)
    assert package.pk not in _offered_by(PyPIReleaseCollector) | _offered_by(SourceReleaseCollector)


@pytest.mark.django_db
def test_a_malformed_index_fails_the_run_with_an_error_row_and_records_nothing() -> None:
    """The matrix's malformed row: `ResolutionDocumentError` escapes, the base writes `error`."""
    package = _a_shell()
    transport = _answering(index=json.dumps({"feedstocks": "x"}), project=_project(Source=THE_REPOSITORY))

    with pytest.raises(ResolutionDocumentError):
        _collect(package, transport=transport)

    assert transport.calls == [THE_INDEX_LOCATOR]
    package.refresh_from_db()
    assert package.confidence == IdentityConfidence.UNMAPPED.value
    assert _outcomes(package) == {}
    (row,) = _rows(package)
    assert row.state == OutcomeState.ERROR.value
    assert row.confidence_recorded == ""
    assert _run(package).status == RunState.FAILED.value


@pytest.mark.django_db
def test_a_pypi_call_that_fails_never_fails_the_run_and_records_error_on_the_two_mappings() -> None:
    """The bounded second call: its failure is a sentence in `detail` beside what the index established."""
    package = _a_shell()
    transport = _answering(index=_index("requests"))
    transport.failures[THE_PROJECT_LOCATOR] = TransportError("connection refused", source=THE_PROJECT_LOCATOR)

    result = _collect(package, transport=transport)

    assert result.state is RunState.SUCCEEDED
    assert transport.calls == [THE_INDEX_LOCATOR, THE_PROJECT_LOCATOR]
    outcomes = _outcomes(package)
    assert outcomes[MappingKind.RELEASE_ECOSYSTEM.value] == OutcomeState.ERROR.value
    assert outcomes[MappingKind.SOURCE_REPOSITORY.value] == OutcomeState.ERROR.value
    assert outcomes[MappingKind.FEEDSTOCK.value] == ESTABLISHED
    (row,) = _rows(package)
    assert row.state == OutcomeState.OK.value
    assert PYPI_UNREADABLE_DETAIL in row.detail
    assert "connection refused" in row.detail
    assert package.pk not in _offered_by(PyPIReleaseCollector)


@pytest.mark.django_db
def test_pypi_that_could_not_be_asked_never_lowers_what_an_earlier_run_established() -> None:
    """A transient failure of the second call re-asserts the established mappings and their values.

    Without this the recorder would rewrite both outcome rows to `error` while the
    package row still held the earlier finding, and the two PyPI-selecting sweeps
    would stop offering the package until a later run happened to succeed.
    """
    package = _a_shell()
    _collect(package, transport=_answering(index=_index("requests"), project=_project(Source=THE_REPOSITORY)))
    failing = _answering(index=_index("requests"))
    failing.failures[THE_PROJECT_LOCATOR] = TransportError("connection refused", source=THE_PROJECT_LOCATOR)

    result = _collect(package, transport=failing, at=FIXED_INSTANT + A_DAY)

    assert result.state is RunState.SUCCEEDED
    package.refresh_from_db()
    assert package.primary_purl == "pkg:pypi/requests"
    assert package.primary_type == "pypi"
    assert package.source_repository_url == THE_REPOSITORY
    assert package.confidence == IdentityConfidence.INVENTORY_DERIVED.value
    outcomes = _outcomes(package)
    assert outcomes[MappingKind.RELEASE_ECOSYSTEM.value] == ESTABLISHED
    assert outcomes[MappingKind.SOURCE_REPOSITORY.value] == ESTABLISHED
    latest = _rows(package)[-1]
    assert latest.state == OutcomeState.OK.value
    assert (latest.pypi_asked, latest.pypi_found, latest.pypi_source) == (False, False, THE_PROJECT_LOCATOR)
    assert latest.repository_url == THE_REPOSITORY
    assert CARRIED_FORWARD_DETAIL in latest.detail
    assert PYPI_UNREADABLE_DETAIL in latest.detail
    assert package.pk in _offered_by(PyPIReleaseCollector)
    assert package.pk in _offered_by(SourceReleaseCollector)
    assert package.pk in _offered_by(PythonReadinessCollector)


@pytest.mark.django_db
def test_a_recorder_refusal_rolls_the_resolution_back_and_fails_the_run_with_an_error_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`ResolutionError` from inside `translate`: nothing on the package, an `error` row, a `failed` ledger row."""
    package = _a_shell()

    def refusing(**_kwargs: object) -> object:
        message = "the recorder refused this resolution"
        raise ResolutionError(message)

    monkeypatch.setattr(resolve_identity_module, "record_resolution", refusing)

    with pytest.raises(ResolutionError):
        _collect(package, transport=_answering(index=_index("requests"), project=_project(Source=THE_REPOSITORY)))

    package.refresh_from_db()
    assert package.confidence == IdentityConfidence.UNMAPPED.value
    assert package.source_repository_url == ""
    assert _outcomes(package) == {}
    assert not Feedstock.objects.filter(package=package).exists()
    (row,) = _rows(package)
    assert row.state == OutcomeState.ERROR.value
    assert row.confidence_recorded == ""
    assert "refused this resolution" in row.detail
    assert _run(package).status == RunState.FAILED.value


@pytest.mark.django_db
def test_two_feedstocks_in_the_index_become_two_rows_under_conda_forge() -> None:
    """The matrix's two-feedstock row."""
    package = _a_shell()

    _collect(package, transport=_answering(index=_index("a", "b"), project=None))

    assert list(Feedstock.objects.filter(package=package).order_by("name").values_list("name", "url")) == [
        ("a-feedstock", "https://github.com/conda-forge/a-feedstock"),
        ("b-feedstock", "https://github.com/conda-forge/b-feedstock"),
    ]
    assert _rows(package)[0].feedstocks == ["a", "b"]


@pytest.mark.django_db
def test_a_rejected_source_link_is_a_not_found_repository_with_the_url_and_why_on_the_row() -> None:
    """The matrix's unreadable-URL row, on a real package: nothing repaired, and `source_release` does not select."""
    package = _a_shell()

    _collect(package, transport=_answering(index=_index("requests"), project=_project(Source="https://gitlab.com/x/y")))

    package.refresh_from_db()
    assert package.source_repository_url == ""
    assert package.primary_purl == "pkg:pypi/requests"
    assert _outcomes(package)[MappingKind.SOURCE_REPOSITORY.value] == OutcomeState.NOT_FOUND.value
    (row,) = _rows(package)
    assert row.repository_url == ""
    assert row.repository_key == "Source"
    assert "https://gitlab.com/x/y" in row.detail
    assert package.pk not in _offered_by(SourceReleaseCollector)
    assert package.pk in _offered_by(PyPIReleaseCollector)


# ---------------------------------------------------------------------------
# AC 2: the same package twice, nothing changed.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_the_same_package_twice_adds_a_snapshot_and_nothing_else() -> None:
    """No second `Feedstock` row, the package's `resolved_at` does not advance, and a second snapshot row exists.

    Both halves of the recorder's timestamp rule are pinned: `Package.resolved_at`
    is when the identity last *changed* and stays put, while
    `PackageMapping.resolved_at` is when a resolver last *looked* and advances on
    every call.
    """
    package = _a_shell()
    transport = _answering(index=_index("requests"), project=_project(Source=THE_REPOSITORY))

    first = _collect(package, transport=transport)
    second = _collect(package, transport=transport, at=FIXED_INSTANT + A_DAY)

    assert (first.state, second.state) == (RunState.SUCCEEDED, RunState.SUCCEEDED)
    package.refresh_from_db()
    assert package.resolved_at == FIXED_INSTANT
    assert Feedstock.objects.filter(package=package).count() == 1
    assert PackageMapping.objects.filter(package=package).count() == FIVE_KINDS
    assert set(PackageMapping.objects.filter(package=package).values_list("resolved_at", flat=True)) == {
        FIXED_INSTANT + A_DAY,
    }
    rows = _rows(package)
    assert len(rows) == TWO_ROWS
    assert [row.observed_at for row in rows] == [FIXED_INSTANT, FIXED_INSTANT + A_DAY]


@pytest.mark.django_db
def test_a_second_run_inside_the_window_is_skipped_and_force_bypasses_it() -> None:
    """`CPM-AD-7`'s window, and `CPM-UJ-1`'s bypass, on this collector."""
    package = _a_shell()
    transport = _answering(index=_index("requests"), project=_project(Source=THE_REPOSITORY))

    _collect(package, transport=transport)
    suppressed = _collect(package, transport=transport)
    forced = _collect(package, transport=transport, force=True)

    assert suppressed.state is RunState.SKIPPED
    assert forced.state is RunState.SUCCEEDED
    assert len(_rows(package)) == TWO_ROWS


@pytest.mark.django_db
def test_an_index_gone_silent_keeps_the_rows_already_recorded_and_says_so() -> None:
    """The prior-rows-kept row: the recorder removes no feedstock, and the mapping stays established."""
    package = _a_shell()
    _collect(package, transport=_answering(index=_index("requests"), project=None))

    result = _collect(package, transport=_answering(index=_index(), project=None), at=FIXED_INSTANT + A_DAY)

    assert result.state is RunState.SUCCEEDED
    assert list(Feedstock.objects.filter(package=package).values_list("name", flat=True)) == [THE_FEEDSTOCK]
    assert _outcomes(package)[MappingKind.FEEDSTOCK.value] == ESTABLISHED
    assert _outcomes(package)[MappingKind.CONDA_ARTIFACT.value] == ESTABLISHED
    package.refresh_from_db()
    assert package.confidence == IdentityConfidence.INVENTORY_DERIVED.value
    assert package.conda_purl == "pkg:conda/requests"
    latest = _rows(package)[-1]
    assert latest.feedstocks == []
    assert PRIOR_ROWS_KEPT_DETAIL in latest.detail
    assert package.pk in _offered_by(FeedstockCollector)


@pytest.mark.django_db
def test_a_name_pypi_could_not_hold_is_an_informative_negative_without_a_call() -> None:
    """A conda name that is not a PyPI name has no project to find; the index still answers."""
    package = _a_shell("_libgcc_mutex", key="k:mutex")
    transport = ScriptedTransport(
        answers={index_locator("_libgcc_mutex"): recorded_payload(source="", body=_index("ctng-compilers"))},
    )

    result = _collect(package, transport=transport)

    assert result.state is RunState.SUCCEEDED
    assert transport.calls == [index_locator("_libgcc_mutex")]
    outcomes = _outcomes(package)
    assert outcomes[MappingKind.RELEASE_ECOSYSTEM.value] == OutcomeState.NOT_FOUND.value
    assert outcomes[MappingKind.FEEDSTOCK.value] == ESTABLISHED
    (row,) = _rows(package)
    assert NO_PYPI_PROJECT_DETAIL in row.detail
    assert (row.pypi_asked, row.pypi_found, row.pypi_source) == (True, False, "")


@pytest.mark.django_db
def test_a_304_to_the_unconditional_pypi_request_is_could_not_ask_rather_than_an_absence() -> None:
    """The second call carries no validator, so a `304` is the source answering a question nobody asked."""
    package = _a_shell()
    not_modified = recorded_payload(source=THE_PROJECT_LOCATOR, body="", not_modified=True)

    result = _collect(package, transport=_answering(index=_index("requests"), project=not_modified))

    assert result.state is RunState.SUCCEEDED
    assert _outcomes(package)[MappingKind.RELEASE_ECOSYSTEM.value] == OutcomeState.ERROR.value
    assert "nothing had changed" in _rows(package)[0].detail


@pytest.mark.django_db
def test_an_unreadable_pypi_document_is_could_not_ask_and_the_index_answer_stands() -> None:
    """A malformed second document never fails the run: the reason lands in `detail`."""
    package = _a_shell()

    result = _collect(package, transport=_answering(index=_index("requests"), project="not json"))

    assert result.state is RunState.SUCCEEDED
    assert _outcomes(package)[MappingKind.RELEASE_ECOSYSTEM.value] == OutcomeState.ERROR.value
    assert _outcomes(package)[MappingKind.FEEDSTOCK.value] == ESTABLISHED
    (row,) = _rows(package)
    assert row.state == OutcomeState.OK.value
    assert PYPI_UNREADABLE_DETAIL in row.detail


@pytest.mark.django_db
def test_the_hooks_refuse_a_package_that_has_no_row_rather_than_inventing_one() -> None:
    """Reached directly, because `collect()` refuses such a key before either hook is asked."""
    collector = IdentityResolutionCollector(
        clock=FixedClock(instant=FIXED_INSTANT),
        transport=_answering(),
        limiter=FixedLimiter(permitted=True),
        response_cache=RecordingResponseCache(),
    )
    try:
        with pytest.raises(ResolutionLocatorError, match="no identity row"):
            collector.source_for(package_id=NO_SUCH_PACKAGE)
        with pytest.raises(ResolutionLocatorError, match="no identity row"):
            collector.translate(
                recorded_payload(source=THE_INDEX_LOCATOR, body=_index()),
                package_id=NO_SUCH_PACKAGE,
                observed_at=FIXED_INSTANT,
            )
    finally:
        collector.close()


# ---------------------------------------------------------------------------
# The selection, and the verified package it never offers.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_a_verified_package_is_never_selected_and_every_other_confidence_is() -> None:
    """A person's identity is never offered a downgrade; `unmapped` and `inventory-derived` are re-resolved."""
    unmapped = _a_shell("unmapped-one", key="k:unmapped")
    derived = _a_shell("derived-one", key="k:derived")
    derived.confidence = IdentityConfidence.INVENTORY_DERIVED.value
    derived.save(update_fields=["confidence"])
    verified = _a_shell("verified-one", key="k:verified")
    verified.confidence = IdentityConfidence.VERIFIED.value
    verified.save(update_fields=["confidence"])

    orphan = Package.objects.create(canonical_name="orphan-one", resolved_at=FIXED_INSTANT)

    selected = list(IdentityResolutionCollector.selectable_packages())

    assert selected == sorted([unmapped.pk, derived.pk])
    assert verified.pk not in selected
    # A shell no source claims is not offered either: the recorder finds a package
    # by its pair and would refuse the blank half at `source_for` on every sweep.
    assert orphan.pk not in selected


@pytest.mark.django_db
def test_a_manual_run_over_a_verified_package_records_its_findings_and_the_held_back_claim() -> None:
    """The recorder's guard, reached only by hand, and visible on the row rather than inferred."""
    package = _a_shell()
    package.confidence = IdentityConfidence.VERIFIED.value
    package.save(update_fields=["confidence"])

    result = _collect(package, transport=_answering(index=_index("requests"), project=_project(Source=THE_REPOSITORY)))

    assert result.state is RunState.SUCCEEDED
    package.refresh_from_db()
    assert package.confidence == IdentityConfidence.VERIFIED.value
    assert package.source_repository_url == THE_REPOSITORY
    (row,) = _rows(package)
    assert row.downgrade_refused is True
    assert row.confidence_recorded == IdentityConfidence.VERIFIED.value


@pytest.mark.django_db
def test_a_package_no_source_claims_is_refused_before_any_call_and_the_run_fails() -> None:
    """The recorder finds a package by its pair; a blank pair cannot be resolved through it."""
    package = Package.objects.create(canonical_name="orphan", resolved_at=FIXED_INSTANT)
    transport = _answering(index=_index("orphan"), project=None)

    with pytest.raises(ResolutionLocatorError):
        _collect(package, transport=transport)

    assert transport.calls == []
    assert _rows(package) == []
    assert _run(package).status == RunState.FAILED.value


@pytest.mark.django_db
def test_an_unknown_package_leaves_nothing_behind() -> None:
    """The recorder checks the key before the opening row (`CPM-EVIDENCE-S09`)."""
    collector = IdentityResolutionCollector(
        clock=FixedClock(instant=FIXED_INSTANT),
        transport=_answering(),
        limiter=FixedLimiter(permitted=True),
        response_cache=RecordingResponseCache(),
    )
    try:
        with pytest.raises(RunLedgerError):
            collector.collect(package_id=NO_SUCH_PACKAGE)
    finally:
        collector.close()

    assert not CollectionRun.objects.filter(collector=COLLECTOR_NAME).exists()


# ---------------------------------------------------------------------------
# The freshness read, the cache, and the row's own rules.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_the_snapshot_is_what_the_freshness_read_answers_from() -> None:
    """`CPM-AD-28`: the target declared here is what the read compares against."""
    package = _a_shell()
    _collect(package, transport=_answering(index=_index("requests"), project=None))
    collector = IdentityResolutionCollector(clock=FixedClock(instant=FIXED_INSTANT))
    try:
        fresh = collector.freshness(package_id=package.pk, now=FIXED_INSTANT + RESOLUTION_FRESHNESS_TARGET / 2)
        stale = collector.freshness(package_id=package.pk, now=FIXED_INSTANT + RESOLUTION_FRESHNESS_TARGET * 2)
    finally:
        collector.close()

    assert fresh.observed_at == FIXED_INSTANT
    assert fresh.stale is False
    assert stale.stale is True


@pytest.mark.django_db
def test_the_index_answer_is_remembered_only_after_the_resolution_is_recorded() -> None:
    """The base's ordering, on this collector: the cache is written last, and the index is what it holds."""
    package = _a_shell()
    cache = RecordingResponseCache()
    index = recorded_payload(source=THE_INDEX_LOCATOR, body=_index("requests"), etag='"abc"')

    _collect(package, transport=_answering(index=index, project=None), cache=cache)

    assert [source for _collector, source, _response, _ttl in cache.writes] == [THE_INDEX_LOCATOR]
    assert len(_rows(package)) == 1


@pytest.mark.django_db
def test_an_ok_row_without_a_recorded_confidence_is_refused_by_the_database() -> None:
    """The first conjunct of the biconditional, isolated."""
    package = _a_shell()

    with pytest.raises(IntegrityError, match=IDENTITY_RESOLUTION_FACTS_CONSTRAINT), transaction.atomic():
        IdentityResolutionSnapshot.objects.create(
            observed_at=FIXED_INSTANT,
            package=package,
            state=OutcomeState.OK.value,
            confidence_recorded="",
        )


@pytest.mark.django_db
def test_a_sentinel_row_carrying_a_recorders_fact_is_refused_by_the_database() -> None:
    """The second conjunct: a row that never reached the recorder may not claim what it would have written."""
    package = _a_shell()

    with pytest.raises(IntegrityError, match=IDENTITY_RESOLUTION_FACTS_CONSTRAINT), transaction.atomic():
        IdentityResolutionSnapshot.objects.create(
            observed_at=FIXED_INSTANT,
            package=package,
            state=OutcomeState.NOT_FOUND.value,
            repository_url=THE_REPOSITORY,
        )


# ---------------------------------------------------------------------------
# The task.
# ---------------------------------------------------------------------------


class SubstitutedCollector(IdentityResolutionCollector):
    """The collector the task builds, with every seam already filled.

    The same move `tests/integration/django_apps/test_feedstock.py` makes and for
    the same reason: a task takes a package key and nothing else, so the collector
    it constructs is the only seam a case about the task has.
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
def test_the_task_lets_an_unknown_package_out_before_the_ledger_opens() -> None:
    """The real task, the real transport constructed, and no call made -- because there is no package."""
    with pytest.raises(RunLedgerError):
        resolve_identity(package_id=NO_SUCH_PACKAGE)

    assert not CollectionRun.objects.filter(collector=COLLECTOR_NAME).exists()


@pytest.mark.django_db
def test_the_task_carries_force_through_to_the_base_and_returns_how_the_run_ended(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`CPM-UJ-1`'s manual recollection is this task, and the flag is live here."""
    monkeypatch.setattr(SubstitutedCollector, "fixed_clock", FixedClock(instant=FIXED_INSTANT))
    monkeypatch.setattr(
        SubstitutedCollector,
        "fixed_transport",
        _answering(index=_index("requests"), project=_project(Source=THE_REPOSITORY)),
    )
    monkeypatch.setattr(collector_tasks, "IdentityResolutionCollector", SubstitutedCollector)
    package = _a_shell()

    first = resolve_identity(package_id=package.pk)
    suppressed = resolve_identity(package_id=package.pk)
    forced = resolve_identity(package_id=package.pk, force=True)

    assert first == RunState.SUCCEEDED.value
    assert suppressed == RunState.SKIPPED.value
    assert forced == RunState.SUCCEEDED.value
    assert len(_rows(package)) == TWO_ROWS
    package.refresh_from_db()
    assert package.confidence == IdentityConfidence.INVENTORY_DERIVED.value
