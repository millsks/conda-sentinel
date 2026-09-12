"""What the demo seeder actually produces, against a real database, a real run and a scripted network.

The claim `config/local_dev/demo_data.py` makes about itself is that **nothing it
shows is fabricated and nothing it asserts was unobserved**: it writes evidence with
a real source behind each row, hands identity to the real `resolve_identity`
collector, and lets the policy engine conclude. That claim is only worth as much as
a test of it, because the shortcut it refuses -- write `verified` and a repository
URL per package, insert rows into `package_health` directly -- produces prettier
screens faster and would pass any test that only checked the screens were populated.

So the cases below check the *provenance* of what appears: every rollup row belongs
to the run this seeder executed, every derived status has a pass's row behind it,
every repository on a package is the one the scripted PyPI document stated and not
one the roster knew, and the two unmapped packages are unmapped because the scripted
index answered `404` for them rather than because a field said so.

**The network is a script.** `seed_demo_inventory(transport=...)` is the seam
`CPM-AD-27` opens, and every case here passes a `ScriptedTransport` that answers
conda-forge's index and PyPI's project document per roster name. A locator nothing
scripted raises, so a seed that reached for anything else fails here rather than
opening a socket. One case scripts a failure for every locator, which is what a
laptop on an aeroplane looks like to the resolver, and asserts the seed completes
with every package honestly `unmapped`.

**The variety cases are re-derived from what the seeded evidence factually
yields.** With a PyPI snapshot only where the resolver found the project, an
advisory, a KEV finding and a licence only where the roster states one -- and
nothing about a source release, a feedstock, a conda build or Python readiness --
the currency column reads `unknown` overall on first paint, and the PyPI surface
alone reads `current`; the vulnerability column separates matched from
nothing-matched, and the gate blanks the two unmapped rows. That is the product's
real shape, and the cases pin it rather than the prettier one the first draft
painted.

Every test rolls back: `@pytest.mark.django_db` wraps each in a transaction.
"""

from __future__ import annotations

import functools
import json
from typing import TYPE_CHECKING
from typing import Final

import pytest

from conda_sentinel.collectors import resolve_identity as resolve_identity_module
from conda_sentinel.collectors.models import CondaPackageSnapshot
from conda_sentinel.collectors.models import FeedstockSnapshot
from conda_sentinel.collectors.models import IdentityResolutionSnapshot
from conda_sentinel.collectors.models import KevFinding
from conda_sentinel.collectors.models import LicenseFinding
from conda_sentinel.collectors.models import PyPIReleaseSnapshot
from conda_sentinel.collectors.models import PythonReadinessAssessment
from conda_sentinel.collectors.models import PythonVerificationResult
from conda_sentinel.collectors.models import SourceReleaseSnapshot
from conda_sentinel.collectors.models import VulnerabilityFinding
from conda_sentinel.collectors.resolve_identity import COLLECTOR_NAME
from conda_sentinel.collectors.resolve_identity import index_locator
from conda_sentinel.collectors.resolve_identity import project_locator
from conda_sentinel.core.models import CollectionRun
from conda_sentinel.core.models import PackageHealth
from conda_sentinel.core.models import PolicyRun
from conda_sentinel.core.outcomes import OutcomeState
from conda_sentinel.core.runs import RunState
from conda_sentinel.core.transport import TransportError
from conda_sentinel.identity.models import ESTABLISHED
from conda_sentinel.identity.models import Feedstock
from conda_sentinel.identity.models import IdentityConfidence
from conda_sentinel.identity.models import MappingKind
from conda_sentinel.identity.models import Package
from conda_sentinel.identity.models import PackageMapping
from conda_sentinel.identity.services import ResolutionError
from conda_sentinel.policies.models import PackageCurrency
from conda_sentinel.policies.models import PackageVulnerability
from config.local_dev import demo_data
from config.local_dev.demo_data import DEMO_COLLECTOR
from config.local_dev.demo_data import DEMO_PACKAGES
from config.local_dev.demo_data import RESOLUTION_ABANDONED_EVENT
from config.local_dev.demo_data import UNREACHABLE_STREAK_LIMIT
from config.local_dev.demo_data import UNRESOLVED_EVENT
from config.local_dev.demo_data import seed_demo_inventory
from config.locality import RUNTIME_ENV_VAR
from tests.collectors import ScriptedTransport
from tests.collectors import recorded_payload

if TYPE_CHECKING:
    from collections.abc import Iterable

pytestmark = pytest.mark.integration

#: The names the scripted index answers `404` for -- the two that are not packages.
#:
#: This is the script's declaration and everything else is derived from it: the
#: roster no longer says which packages are unmapped, because a roster that did was
#: asserting something only conda-forge can answer. What the cases below assert is
#: that these two come out `unmapped` *because the index said so*, and that
#: everything the index answered for was resolved.
NOT_ON_CONDA_FORGE: Final[frozenset[str]] = frozenset({"internal-telemetry-sdk", "internal-feature-flags"})

#: The package the exercise in `docs/conda-sentinel/onboarding.md` sends a reader to.
THE_UNMAPPED_PACKAGE: Final[str] = "internal-telemetry-sdk"

#: Every package the seed leaves unresolved, derived from the script's `404` list.
THE_UNMAPPED_PACKAGES: Final[frozenset[str]] = NOT_ON_CONDA_FORGE

#: The package the acceptance criterion names, and the owner the script files it under.
THE_NAMED_PACKAGE: Final[str] = "django"
THE_SCRIPTED_OWNER: Final[str] = "example"

#: How many roster rows carry an advisory, which is how many KEV findings the seed writes.
WITH_ADVISORY: Final[int] = sum(1 for demo in DEMO_PACKAGES if demo.advisory)

#: How many roster rows state a licence, which is how many licence findings the seed writes.
WITH_LICENCE: Final[int] = sum(1 for demo in DEMO_PACKAGES if demo.licence)

#: How many packages the script lets the resolver resolve -- and so how many PyPI rows the seed writes.
RESOLVABLE: Final[int] = len(DEMO_PACKAGES) - len(NOT_ON_CONDA_FORGE)

#: The healthy summary against the script, which is also the healthy one against the network.
A_HEALTHY_SUMMARY: Final[dict[str, int]] = {
    "resolved": RESOLVABLE,
    "not_on_conda_forge": len(NOT_ON_CONDA_FORGE),
    "unreachable": 0,
    "verified_kept": 0,
}

#: The evidence tables the seeder must leave empty: each is a sweep's to observe.
NEVER_SEEDED: Final[tuple[type, ...]] = (
    SourceReleaseSnapshot,
    FeedstockSnapshot,
    CondaPackageSnapshot,
    PythonReadinessAssessment,
    PythonVerificationResult,
)

#: The mapping kinds the scripted documents let the resolver establish.
ESTABLISHED_KINDS: Final[frozenset[str]] = frozenset(
    {
        MappingKind.SOURCE_REPOSITORY.value,
        MappingKind.RELEASE_ECOSYSTEM.value,
        MappingKind.CONDA_ARTIFACT.value,
        MappingKind.FEEDSTOCK.value,
    },
)


def _repository_for(name: str) -> str:
    """Return the repository the scripted PyPI document names for a package.

    Args:
        name: The roster name.

    Returns:
        A `github.com` owner/repository the resolver's normalisation accepts.

    """
    return f"https://github.com/{THE_SCRIPTED_OWNER}/{name}"


def _scripted(*, absent: Iterable[str] = NOT_ON_CONDA_FORGE) -> ScriptedTransport:
    """Return a transport answering conda-forge's index and PyPI per roster name.

    Args:
        absent: The names the index answers `404` for. PyPI is never scripted for
            them, because the resolver never asks it about a package the index has
            no entry for -- and a case that made it ask would raise here.

    Returns:
        The scripted transport.

    """
    missing = frozenset(absent)
    answers = {}
    for demo in DEMO_PACKAGES:
        index = index_locator(demo.name)
        if demo.name in missing:
            answers[index] = recorded_payload(source=index, found=False, body="")
            continue
        answers[index] = recorded_payload(source=index, body=json.dumps({"feedstocks": [demo.name]}))
        project = project_locator(demo.name)
        document = {
            "info": {
                "name": demo.name,
                "version": demo.upstream_version,
                "project_urls": {"Source": _repository_for(demo.name)},
            },
        }
        answers[project] = recorded_payload(source=project, body=json.dumps(document))
    return ScriptedTransport(answers=answers)


def _unreachable() -> ScriptedTransport:
    """Return a transport on which every locator fails, which is what no network looks like.

    Returns:
        The scripted transport.

    """
    failures = {}
    for demo in DEMO_PACKAGES:
        for locator in (index_locator(demo.name), project_locator(demo.name)):
            failures[locator] = TransportError("connection refused", source=locator)
    return ScriptedTransport(failures=failures)


@pytest.fixture
def transport() -> ScriptedTransport:
    """Return the transport a seed reads the two sources through.

    Returns:
        The scripted transport, answering per roster name.

    """
    return _scripted()


@pytest.fixture
def seeded(transport: ScriptedTransport) -> dict[str, object]:
    """Seed the demo inventory once for a case to read.

    Args:
        transport: The scripted network.

    Returns:
        What the seeder reported.

    """
    return seed_demo_inventory(transport=transport)


def _counts(seeded: dict[str, object]) -> dict[str, object]:
    """Return the four resolution counts the seeder reported.

    Args:
        seeded: What the seeder reported.

    Returns:
        The counts, keyed as the summary keys them.

    """
    return {key: seeded[key] for key in A_HEALTHY_SUMMARY}


def _outcomes(package: Package) -> dict[str, str]:
    """Return the package's mapping outcomes by kind.

    Args:
        package: The package to read.

    Returns:
        The outcomes.

    """
    return dict(PackageMapping.objects.filter(package=package).values_list("kind", "outcome"))


# ---------------------------------------------------------------------------
# Provenance: every row belongs to a run, every status to a pass.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_every_declared_package_reaches_the_rollup(seeded: dict[str, object]) -> None:
    """`CPM-AD-11`: one row per package, and the seeder gets all of them there.

    Args:
        seeded: What the seeder reported.

    """
    assert Package.objects.count() == len(DEMO_PACKAGES)
    assert PackageHealth.objects.count() == len(DEMO_PACKAGES)
    assert seeded["rollup_rows"] == len(DEMO_PACKAGES)


@pytest.mark.django_db
def test_every_rollup_row_belongs_to_the_run_the_seeder_executed(seeded: dict[str, object]) -> None:
    """The provenance claim, and the case the shortcut would fail.

    A seeder that wrote `package_health` directly would produce rows with no run, or
    with a run nothing executed. Every row here names the one policy run this seeding
    performed -- which is what makes the screens a picture of the engine's output
    rather than of the fixture's intent.

    Args:
        seeded: What the seeder reported.

    """
    run = PolicyRun.objects.get(policy_version=seeded["policy_version"])

    assert run.finished_at is not None
    assert set(PackageHealth.objects.values_list("policy_run_id", flat=True)) == {run.pk}


@pytest.mark.django_db
def test_every_status_has_a_pass_row_behind_it(seeded: dict[str, object]) -> None:
    """A rollup status with no derived row behind it would be a fabricated verdict.

    `CPM-AD-21` writes one derived row per package per run, so a package with a
    vulnerability status and no `package_vulnerability` row would mean the value came
    from somewhere other than the pass.

    Args:
        seeded: What the seeder reported.

    """
    run = PolicyRun.objects.get(policy_version=seeded["policy_version"])

    assert PackageVulnerability.objects.filter(policy_run=run).count() == len(DEMO_PACKAGES)


@pytest.mark.django_db
def test_the_seeder_writes_exactly_the_four_real_sourced_kinds_of_evidence(seeded: dict[str, object]) -> None:
    """The matrix's evidence rule: four tables written, five left to the sweeps.

    An advisory finding per package (the collector's own nothing-matched row where
    the roster has none) and a KEV finding per advisory; a licence finding only
    where the roster states a licence -- a blank one seeds nothing rather than a
    `not_found` claiming conda-forge was asked; and a PyPI release only for a
    package the resolver has just found on PyPI -- the two the index has no entry
    for get none, because PyPI never stated a version for them. Nothing about a
    source release, a feedstock, a conda build, readiness or verification -- every
    one of those was an invention in the first draft, and every one is a sweep's to
    observe now. And no invented `error`: the state a happy-path fixture never
    produces is one a fixture must not paint.

    Args:
        seeded: What the seeder reported.

    """
    assert PyPIReleaseSnapshot.objects.count() == RESOLVABLE
    assert PyPIReleaseSnapshot.objects.filter(package__canonical_name__in=NOT_ON_CONDA_FORGE).count() == 0
    assert VulnerabilityFinding.objects.count() == len(DEMO_PACKAGES)
    assert KevFinding.objects.count() == WITH_ADVISORY
    assert LicenseFinding.objects.count() == WITH_LICENCE
    assert LicenseFinding.objects.filter(state=OutcomeState.NOT_FOUND.value).count() == 0
    assert VulnerabilityFinding.objects.filter(state=OutcomeState.ERROR.value).count() == 0
    for model in NEVER_SEEDED:
        assert model.objects.count() == 0, f"the seeder wrote {model.__name__} rows, which only a sweep may observe"


@pytest.mark.django_db
def test_every_pypi_snapshot_names_the_project_page_it_was_read_from(seeded: dict[str, object]) -> None:
    """A source a reader can open, on every row the seeder writes.

    Args:
        seeded: What the seeder reported.

    """
    sources = set(PyPIReleaseSnapshot.objects.values_list("source", flat=True))

    assert sources == {
        f"https://pypi.org/project/{demo.name}/" for demo in DEMO_PACKAGES if demo.name not in NOT_ON_CONDA_FORGE
    }


@pytest.mark.django_db
def test_a_pypi_snapshot_is_written_only_where_the_resolver_found_the_project(seeded: dict[str, object]) -> None:
    """The PyPI row follows the `release_ecosystem` mapping, not the roster.

    Every package with a PyPI row has an `established` release-ecosystem mapping
    written by the resolver minutes earlier, and every package with that mapping
    has a PyPI row. The seed is ordered shells, resolver, evidence for exactly
    this: the evidence is chosen after the resolver has said what PyPI has.

    Args:
        seeded: What the seeder reported.

    """
    on_pypi = set(
        PackageMapping.objects.filter(kind=MappingKind.RELEASE_ECOSYSTEM.value, outcome=ESTABLISHED).values_list(
            "package_id", flat=True
        ),
    )
    with_row = set(PyPIReleaseSnapshot.objects.values_list("package_id", flat=True))

    assert with_row == on_pypi
    assert len(with_row) == RESOLVABLE


# ---------------------------------------------------------------------------
# Identity: observed by the resolver, never asserted by the roster.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_the_resolver_ran_once_per_package_under_its_own_name(seeded: dict[str, object]) -> None:
    """The resolver's runs are real runs, and the Coverage screen showing them is telling the truth.

    Two collectors in the ledger and no more: the seeder's own run under a name that
    is not registered, and a hundred `resolve_identity` runs -- one per package,
    every one finished rather than left `running`.

    Args:
        seeded: What the seeder reported.

    """
    assert set(CollectionRun.objects.values_list("collector", flat=True)) == {DEMO_COLLECTOR, COLLECTOR_NAME}
    resolutions = CollectionRun.objects.filter(collector=COLLECTOR_NAME)
    assert resolutions.count() == len(DEMO_PACKAGES)
    assert set(resolutions.values_list("status", flat=True)) == {RunState.SUCCEEDED.value}
    assert CollectionRun.objects.filter(collector=DEMO_COLLECTOR).count() == 1


@pytest.mark.django_db
def test_the_seed_reports_how_many_resolved_and_how_many_did_not(seeded: dict[str, object]) -> None:
    """The summary's four counts, and the pairing that makes them worth reading.

    `not_on_conda_forge=2` is the healthy number: it is the two `internal-*` names,
    which the index has no entry for, told apart from a source that could not be
    reached. Read off the package's confidence after each run rather than off the
    run's state, because the base files "the index has no entry" as `succeeded`
    too.

    Args:
        seeded: What the seeder reported.

    """
    assert _counts(seeded) == A_HEALTHY_SUMMARY


@pytest.mark.django_db
def test_the_seed_asks_the_script_and_nothing_else(seeded: dict[str, object], transport: ScriptedTransport) -> None:
    """No socket: every call the seed made is one the script answered, and there are as many as the roster implies.

    Two per package the index answered for, one per package it did not -- because
    the resolver never asks PyPI about a package conda-forge has no entry for.

    Args:
        seeded: What the seeder reported.
        transport: The script, which recorded every call.

    """
    assert seeded["rollup_rows"] == len(DEMO_PACKAGES)
    expected = 2 * (len(DEMO_PACKAGES) - len(NOT_ON_CONDA_FORGE)) + len(NOT_ON_CONDA_FORGE)
    assert len(transport.calls) == expected
    assert set(transport.calls) <= set(transport.answers)


@pytest.mark.django_db
def test_the_unmapped_package_was_never_promoted(seeded: dict[str, object]) -> None:
    """It is unmapped because the index had no entry, not because a field says so.

    `CPM-AD-14` gives identity one write path and `CPM-AD-25` gives creation to
    `resolve_package_shell`, which creates at `unmapped`. The resolver asked
    conda-forge, conda-forge said there is no such package, and the run recorded
    exactly that: a `not_found` snapshot, no mapping row of any kind, and the gate
    below blanking the rollup row is the gate doing its job.

    Args:
        seeded: What the seeder reported.

    """
    package = Package.objects.get(canonical_name=THE_UNMAPPED_PACKAGE)
    row = PackageHealth.objects.get(package=package)
    snapshot = IdentityResolutionSnapshot.objects.get(package=package)

    assert package.confidence == IdentityConfidence.UNMAPPED
    assert PackageMapping.objects.filter(package=package).count() == 0
    assert snapshot.state == OutcomeState.NOT_FOUND.value
    assert snapshot.source == index_locator(THE_UNMAPPED_PACKAGE)
    assert row.confidence == IdentityConfidence.UNMAPPED
    assert row.currency_status == OutcomeState.UNKNOWN.value
    assert row.feedstock_presence_status == OutcomeState.UNKNOWN.value
    assert row.priority_status == OutcomeState.UNKNOWN.value
    assert row.work_type_status == OutcomeState.UNKNOWN.value


@pytest.mark.django_db
def test_every_package_the_index_answered_for_was_resolved_from_what_it_said(seeded: dict[str, object]) -> None:
    """The other side: the resolver ran and established them, at the confidence it may claim.

    `inventory-derived`, never `verified` -- a person establishes `verified` and
    nobody has looked. Each carries the repository the scripted PyPI document named,
    the purls the resolver derives from the name, one feedstock row the scripted
    index listed, and one `ok` snapshot saying so.

    Args:
        seeded: What the seeder reported.

    """
    resolved = Package.objects.exclude(canonical_name__in=THE_UNMAPPED_PACKAGES)

    assert THE_UNMAPPED_PACKAGES, "the script answers for everything, so the gate case above proves nothing"
    assert resolved.count() == len(DEMO_PACKAGES) - len(THE_UNMAPPED_PACKAGES)
    assert set(resolved.values_list("confidence", flat=True)) == {IdentityConfidence.INVENTORY_DERIVED}
    for package in resolved:
        assert package.source_repository_url == _repository_for(package.canonical_name), package.canonical_name
        assert package.primary_type == "pypi", package.canonical_name
        assert package.primary_purl.startswith("pkg:pypi/"), package.canonical_name
        assert package.conda_purl == f"pkg:conda/{package.canonical_name}", package.canonical_name
        outcomes = _outcomes(package)
        assert {kind for kind, outcome in outcomes.items() if outcome == ESTABLISHED} == ESTABLISHED_KINDS
        assert Feedstock.objects.filter(package=package).count() == 1, package.canonical_name
        snapshot = IdentityResolutionSnapshot.objects.get(package=package)
        assert snapshot.state == OutcomeState.OK.value
        assert snapshot.confidence_recorded == IdentityConfidence.INVENTORY_DERIVED.value


@pytest.mark.django_db
def test_the_named_package_carries_the_repository_the_source_stated(seeded: dict[str, object]) -> None:
    """The acceptance criterion's shape: `django` carries what PyPI said, and nothing invented.

    Against the live sources that is `https://github.com/django/django`; against the
    script it is whatever the script's document stated. What the case pins is that
    the value came from the document and not from the roster, which knows no
    repository at all.

    Args:
        seeded: What the seeder reported.

    """
    package = Package.objects.get(canonical_name=THE_NAMED_PACKAGE)

    assert package.source_repository_url == _repository_for(THE_NAMED_PACKAGE)
    assert "github.com/demo" not in package.source_repository_url
    assert not any("github.com/demo" in url for url in Package.objects.values_list("source_repository_url", flat=True))


@pytest.mark.django_db
def test_the_named_package_is_offered_to_the_sweeps_that_select_on_its_mappings(seeded: dict[str, object]) -> None:
    """What a resolved identity is *for*: the currency and readiness sweeps now select it.

    Which is the whole reason the seed resolves inline -- the first paint after a
    seed used to be the last honest one, because nothing downstream could ever
    select a package whose mappings were fictions.

    Args:
        seeded: What the seeder reported.

    """
    from conda_sentinel.collectors.feedstock import FeedstockCollector  # noqa: PLC0415 - read beside the claim
    from conda_sentinel.collectors.pypi_release import PyPIReleaseCollector  # noqa: PLC0415 - as above
    from conda_sentinel.collectors.source_release import SourceReleaseCollector  # noqa: PLC0415 - as above

    package = Package.objects.get(canonical_name=THE_NAMED_PACKAGE)
    unmapped = Package.objects.get(canonical_name=THE_UNMAPPED_PACKAGE)

    for collector in (SourceReleaseCollector, PyPIReleaseCollector, FeedstockCollector):
        selection = collector.selectable_packages()
        offered = set(selection) if selection is not None else set()
        assert package.pk in offered, collector.__name__
        assert unmapped.pk not in offered, collector.__name__


# ---------------------------------------------------------------------------
# No network: the seed completes, and every package says why it is unmapped.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_no_network_leaves_every_package_unmapped_and_the_seed_complete(caplog: pytest.LogCaptureFixture) -> None:
    """The matrix's no-network row: a laptop on an aeroplane seeds, honestly, and does not wait out a hundred timeouts.

    Every locator fails. The first three resolutions are `failed` runs with an
    `error` snapshot and nothing recorded on the package; after three in a row the
    resolver stops asking, counts the rest `unreachable` without a run, and says so
    once. The seed carries on to the policy run, every rollup row is written and
    gated `unknown`, no PyPI row is seeded because nothing confirmed a project, and
    the summary says `unreachable=100`. What it never does is fail whole: a seed
    that raised on the first refused connection would leave a hundred shells and no
    policy run, which is a half-seeded database nobody can sign in and diagnose.

    Args:
        caplog: pytest's log capture, for the per-package and the abandonment lines.

    """
    transport = _unreachable()

    with caplog.at_level("WARNING", logger=demo_data.logger.name):
        seeded = seed_demo_inventory(transport=transport)

    assert _counts(seeded) == {
        "resolved": 0,
        "not_on_conda_forge": 0,
        "unreachable": len(DEMO_PACKAGES),
        "verified_kept": 0,
    }
    assert seeded["rollup_rows"] == len(DEMO_PACKAGES)
    assert len(transport.calls) == UNREACHABLE_STREAK_LIMIT, (
        "the resolver kept asking a network that had already failed"
    )
    assert set(Package.objects.values_list("confidence", flat=True)) == {IdentityConfidence.UNMAPPED}
    assert PackageMapping.objects.count() == 0
    assert PyPIReleaseSnapshot.objects.count() == 0
    assert set(IdentityResolutionSnapshot.objects.values_list("state", flat=True)) == {OutcomeState.ERROR.value}
    assert IdentityResolutionSnapshot.objects.count() == UNREACHABLE_STREAK_LIMIT
    runs = CollectionRun.objects.filter(collector=COLLECTOR_NAME)
    assert runs.count() == UNREACHABLE_STREAK_LIMIT
    assert set(runs.values_list("status", flat=True)) == {RunState.FAILED.value}
    assert set(PackageHealth.objects.values_list("currency_status", flat=True)) == {OutcomeState.UNKNOWN.value}
    warned = [record.getMessage() for record in caplog.records if UNRESOLVED_EVENT in record.getMessage()]
    assert len(warned) == UNREACHABLE_STREAK_LIMIT
    for demo in DEMO_PACKAGES[:UNREACHABLE_STREAK_LIMIT]:
        assert any(demo.name in message for message in warned), demo.name
    abandoned = [record.getMessage() for record in caplog.records if RESOLUTION_ABANDONED_EVENT in record.getMessage()]
    assert len(abandoned) == 1
    assert f"'skipped': {len(DEMO_PACKAGES) - UNREACHABLE_STREAK_LIMIT}" in abandoned[0]


@pytest.mark.django_db
def test_an_index_that_cannot_be_read_is_one_package_s_failure_and_resets_nothing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A malformed index document raises out of the collector, and the seed catches it per package.

    The base writes that package's `error` row and re-raises whatever `translate`
    raised; a seed that let it through would fail whole for one malformed document.
    Scripted for one name: the other ninety-seven resolve, the one is logged as
    `unreachable` and counted, and the policy run still covers all hundred.

    Args:
        caplog: pytest's log capture.

    """
    transport = _scripted()
    malformed = index_locator(THE_NAMED_PACKAGE)
    transport.answers[malformed] = recorded_payload(source=malformed, body=json.dumps({"feedstocks": "not a list"}))

    with caplog.at_level("WARNING", logger=demo_data.logger.name):
        seeded = seed_demo_inventory(transport=transport)

    assert _counts(seeded) == {**A_HEALTHY_SUMMARY, "resolved": RESOLVABLE - 1, "unreachable": 1}
    assert seeded["rollup_rows"] == len(DEMO_PACKAGES)
    package = Package.objects.get(canonical_name=THE_NAMED_PACKAGE)
    assert package.confidence == IdentityConfidence.UNMAPPED
    assert IdentityResolutionSnapshot.objects.get(package=package).state == OutcomeState.ERROR.value
    assert PyPIReleaseSnapshot.objects.filter(package=package).count() == 0
    warned = [record.getMessage() for record in caplog.records if UNRESOLVED_EVENT in record.getMessage()]
    assert any(THE_NAMED_PACKAGE in message and "ResolutionDocumentError" in message for message in warned)


@pytest.mark.django_db
def test_a_resolution_the_recorder_refuses_is_one_package_s_failure(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The recorder refusing what it was handed rolls that package back and fails only it.

    `ResolutionError` escapes `translate` after the base has written the `error`
    row; the seed catches it, logs it by name and type, counts it `unreachable`,
    and the other ninety-nine are untouched. Patched on the resolver module, which
    is where the recorder is looked up at call time.

    Args:
        caplog: pytest's log capture.
        monkeypatch: pytest's patcher, which restores the recorder.

    """
    real = resolve_identity_module.record_resolution

    def refusing(*, resolution: object, clock: object) -> object:
        if getattr(resolution, "associator_key", "") == f"pypi:{THE_NAMED_PACKAGE}":
            message = "refused by the case, to see what the seed does with a refusal"
            raise ResolutionError(message)
        return real(resolution=resolution, clock=clock)  # type: ignore[arg-type]

    monkeypatch.setattr(resolve_identity_module, "record_resolution", refusing)

    with caplog.at_level("WARNING", logger=demo_data.logger.name):
        seeded = seed_demo_inventory(transport=_scripted())

    assert _counts(seeded) == {**A_HEALTHY_SUMMARY, "resolved": RESOLVABLE - 1, "unreachable": 1}
    assert seeded["rollup_rows"] == len(DEMO_PACKAGES)
    package = Package.objects.get(canonical_name=THE_NAMED_PACKAGE)
    assert package.confidence == IdentityConfidence.UNMAPPED
    assert PackageMapping.objects.filter(package=package).count() == 0
    assert IdentityResolutionSnapshot.objects.get(package=package).state == OutcomeState.ERROR.value
    warned = [record.getMessage() for record in caplog.records if UNRESOLVED_EVENT in record.getMessage()]
    assert any(THE_NAMED_PACKAGE in message and "ResolutionError" in message for message in warned)


@pytest.mark.django_db
def test_a_package_a_person_verified_is_left_alone_by_a_second_seed() -> None:
    """`force=True` bypasses the window, never a person's decision.

    A re-seed offers the resolver only what the collector itself would select, so a
    package set `verified` between seeds keeps its mappings and its `resolved_at`,
    is counted `verified_kept`, and gets no second resolution run.
    """
    seed_demo_inventory(transport=_scripted())
    package = Package.objects.get(canonical_name=THE_NAMED_PACKAGE)
    Package.objects.filter(pk=package.pk).update(
        confidence=IdentityConfidence.VERIFIED,
        source_repository_url="https://github.com/verified-by-a-person/django",
    )
    before_mappings = _outcomes(package)
    before_runs = CollectionRun.objects.filter(collector=COLLECTOR_NAME, package=package).count()
    before = Package.objects.values("pk", "resolved_at", "source_repository_url", "confidence").get(pk=package.pk)

    seeded = seed_demo_inventory(transport=_scripted())

    assert _counts(seeded) == {**A_HEALTHY_SUMMARY, "resolved": RESOLVABLE - 1, "verified_kept": 1}
    assert (
        Package.objects.values("pk", "resolved_at", "source_repository_url", "confidence").get(pk=package.pk) == before
    )
    assert _outcomes(package) == before_mappings
    assert CollectionRun.objects.filter(collector=COLLECTOR_NAME, package=package).count() == before_runs


# ---------------------------------------------------------------------------
# The variety, re-derived from what the seeded evidence factually yields.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_currency_is_current_against_pypi_and_unknown_overall_until_the_sweeps_run(seeded: dict[str, object]) -> None:
    """What the currency column honestly says on first paint, and why.

    The seed writes one PyPI release per package and no other surface, so PyPI is
    the authority and agrees with itself -- `current` on that surface -- while the
    source, feedstock and conda surfaces are `unknown` because nothing observed
    them, and the worst of those is what the package reads. The first draft painted
    `behind` here from a conda build nobody performed; the honest column reads
    `unknown` until `source_release`, `feedstock` and `conda_package` sweep.

    Args:
        seeded: What the seeder reported.

    """
    run = PolicyRun.objects.get(policy_version=seeded["policy_version"])
    resolved = PackageCurrency.objects.filter(policy_run=run).exclude(package__canonical_name__in=THE_UNMAPPED_PACKAGES)

    assert set(resolved.values_list("pypi_status", flat=True)) == {"current"}
    assert set(resolved.values_list("source_status", flat=True)) == {OutcomeState.UNKNOWN.value}
    assert set(resolved.values_list("feedstock_status", flat=True)) == {OutcomeState.UNKNOWN.value}
    assert set(resolved.values_list("overall_status", flat=True)) == {OutcomeState.UNKNOWN.value}
    assert set(PackageHealth.objects.values_list("currency_status", flat=True)) == {OutcomeState.UNKNOWN.value}


@pytest.mark.django_db
def test_the_inventory_separates_an_advisory_matched_from_nothing_matched(seeded: dict[str, object]) -> None:
    """The two verdicts the seeded advisories factually yield, and no third painted on.

    The first draft seeded three `429 rate limited` rows so that `error` would
    appear on the screen. Nothing rate-limited anybody; the row was a fiction, and it
    is gone. What the pass concludes now is what OSV's advisories and the
    collector's own nothing-matched detail support: one matched verdict per roster
    advisory, and nothing-matched for the rest. The pass rows are not gated --
    `CPM-AD-4`'s gate is applied on the way into the rollup, and the case above
    reads it there.

    Args:
        seeded: What the seeder reported.

    """
    run = PolicyRun.objects.get(policy_version=seeded["policy_version"])
    rows = PackageVulnerability.objects.filter(policy_run=run)
    verdicts = set(rows.values_list("vulnerability_status", flat=True))

    assert verdicts == {"advisories_matched", "no_advisory_matched"}
    assert rows.filter(vulnerability_status="advisories_matched").count() == WITH_ADVISORY


@pytest.mark.django_db
def test_the_kev_column_carries_a_listing_and_a_non_listing(seeded: dict[str, object]) -> None:
    """The KEV column qualifies the one beside it, so both values have to appear.

    Args:
        seeded: What the seeder reported.

    """
    run = PolicyRun.objects.get(policy_version=seeded["policy_version"])
    memberships = set(PackageVulnerability.objects.filter(policy_run=run).values_list("kev_membership", flat=True))

    assert {"listed", "not_listed"} <= memberships


@pytest.mark.django_db
def test_the_inventory_produces_more_than_one_work_type(seeded: dict[str, object]) -> None:
    """The column a queue is built from, so a demo where it is constant is a poor demo.

    Args:
        seeded: What the seeder reported.

    """
    assert len(set(PackageHealth.objects.values_list("work_type_status", flat=True))) > 1


@pytest.mark.django_db
def test_the_seeder_reports_what_the_version_it_ran_at_leaves_empty(seeded: dict[str, object]) -> None:
    """A column that comes out inert is explained, and the explanation is derived.

    It used to be a fixed sentence saying priority *and* licence were empty.
    Recording a priority rule set at a newer version made that sentence false the
    moment the seeder picked the newer version up -- a demo confidently explaining a
    state it was no longer in, which is worse than one that says nothing. So the
    report is read from the parameters the run actually applied, and this case checks
    it against the same source rather than against a remembered string.

    Args:
        seeded: What the seeder reported.

    """
    from conda_sentinel.policies.parameters import parameters_for  # noqa: PLC0415 - read beside the claim

    recorded = parameters_for(str(seeded["policy_version"]))
    reported = str(seeded["unconfigured"])

    assert ("license_rules" in reported) == (not recorded.license_rules)
    assert ("priority_rules" in reported) == (not recorded.priority_rules)


@pytest.mark.django_db
def test_the_demo_runs_at_a_version_that_records_priority_rules(seeded: dict[str, object]) -> None:
    """The precondition of the case below, asserted rather than skipped around.

    `tests/unit/test_suite_policy.py` bans `pytest.skip` in a test body, and is right
    to: a skipped case reads in a report as a gate that ran. So the precondition is
    its own assertion.

    **If the proposed rule set is withdrawn, this is the case to delete** -- together
    with the one below it. Both exist because the seeded demo currently runs at a
    version that records rules, which is what makes the ranking checkable at all.

    Args:
        seeded: What the seeder reported.

    """
    assert parameters_for_run(seeded), (
        f"the demo ran at {seeded['policy_version']}, which records no priority rules -- so the priority "
        f"column is inert and the ranking case below has nothing to check"
    )


@pytest.mark.django_db
def test_a_vulnerable_package_never_falls_through_to_the_backlog(seeded: dict[str, object]) -> None:
    """The hole the demo found in the proposed rule set, kept closed.

    Every priority rule that conditions on a *second* domain is implicitly a
    condition on that domain having reached a verdict, and every domain can be
    `unknown`. The first draft's vulnerability rules all named
    `remediation_readiness`, so a package with a critical advisory and no remediation
    verdict matched none of them and landed in "behind upstream".

    Whatever the rule set says, a package the product knows is vulnerable must not
    rank below one that is merely out of date.

    Args:
        seeded: What the seeder reported.

    """
    from conda_sentinel.policies.outcomes import PRIORITY_BUCKETS  # noqa: PLC0415 - read beside the claim

    run_id = PackageHealth.objects.values_list("policy_run_id", flat=True).first()
    vulnerable = set(
        PackageVulnerability.objects.filter(
            policy_run_id=run_id,
            vulnerability_status="advisories_matched",
        ).values_list("package_id", flat=True),
    )
    assert vulnerable, "the demo seeded no vulnerable package, so this proves nothing"

    order = {bucket: rank for rank, bucket in enumerate(PRIORITY_BUCKETS)}
    worst_allowed = order["p3"]
    for row in PackageHealth.objects.filter(package_id__in=vulnerable):
        assert row.priority_status in order, row.priority_status
        assert order[row.priority_status] <= worst_allowed, (
            f"{row.package.canonical_name} is vulnerable and ranked {row.priority_status}"
        )


def parameters_for_run(seeded: dict[str, object]) -> tuple[object, ...]:
    """Return the priority rules the seeded run applied.

    Args:
        seeded: What the seeder reported.

    Returns:
        The recorded rules, empty when the version records none.

    """
    from conda_sentinel.policies.parameters import parameters_for  # noqa: PLC0415 - after django.setup()

    return tuple(parameters_for(str(seeded["policy_version"])).priority_rules)


@pytest.mark.django_db
def test_seeding_twice_appends_evidence_and_re_resolves_without_recording_anything_new() -> None:
    """`CPM-AD-2`: a re-observation inserts, and the demo is honest about that.

    Packages are reused rather than recreated; evidence is appended rather than
    replaced; the resolver runs again for every package -- `force=True`, so the
    observation window does not suppress it -- and, reading the same documents,
    records nothing new: the same feedstock rows, `resolved_at` where it was. A
    second policy run writes a second set of derived rows.
    """
    seed_demo_inventory(transport=_scripted())
    first_derived = PackageVulnerability.objects.count()
    first_findings = VulnerabilityFinding.objects.count()
    first_feedstocks = Feedstock.objects.count()
    first_resolved_at = dict(Package.objects.values_list("canonical_name", "resolved_at"))

    seeded = seed_demo_inventory(transport=_scripted())

    assert _counts(seeded) == A_HEALTHY_SUMMARY
    assert Package.objects.count() == len(DEMO_PACKAGES), "a second run created packages rather than reusing them"
    assert VulnerabilityFinding.objects.count() > first_findings, "a second run replaced evidence rather than appending"
    assert PackageVulnerability.objects.count() > first_derived, "the second policy run wrote no derived rows"
    assert CollectionRun.objects.filter(collector=COLLECTOR_NAME).count() == 2 * len(DEMO_PACKAGES)
    assert IdentityResolutionSnapshot.objects.count() == 2 * len(DEMO_PACKAGES)
    assert Feedstock.objects.count() == first_feedstocks, "re-reading the same index grew the feedstock rows"
    assert dict(Package.objects.values_list("canonical_name", "resolved_at")) == first_resolved_at


# ---------------------------------------------------------------------------
# The runnable form.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_the_entry_point_seeds_and_reports(monkeypatch: pytest.MonkeyPatch) -> None:
    """`python -m config.local_dev.seed_demo` is what the pixi task runs, and it runs.

    Called as a function rather than as a subprocess, on the terms
    `tests/integration/test_local_dev_seeding.py` sets for the persona seeder: a
    subprocess would resolve a different pixi environment and a different database,
    and what is worth pinning is that the module's `main` sets Django up and drives
    the same seeding the task promises. The transport is bound through the seam
    rather than the function replaced, so what runs is the real seeder against the
    script and never the network.

    Args:
        monkeypatch: pytest's patcher, which restores the seeder.

    """
    from config.local_dev import seed_demo  # noqa: PLC0415 - imported here for the same reason `main` defers its own

    monkeypatch.setattr(demo_data, "seed_demo_inventory", functools.partial(seed_demo_inventory, transport=_scripted()))

    reported = seed_demo.main()

    assert reported["rollup_rows"] == len(DEMO_PACKAGES)
    assert _counts(reported) == A_HEALTHY_SUMMARY
    assert PackageHealth.objects.count() == len(DEMO_PACKAGES)


@pytest.mark.django_db
def test_the_entry_point_adds_no_escape_hatch_around_the_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    """The refusal is the seeding function's, and the entry point does not soften it.

    Args:
        monkeypatch: pytest's patcher, which restores the runtime variable.

    """
    from django.core.exceptions import ImproperlyConfigured  # noqa: PLC0415 - local to this case

    from config.local_dev import seed_demo  # noqa: PLC0415 - as above

    monkeypatch.setenv(RUNTIME_ENV_VAR, "production")

    with pytest.raises(ImproperlyConfigured, match=r"append-only"):
        seed_demo.main()
