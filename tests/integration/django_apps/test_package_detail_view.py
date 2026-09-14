"""`CPM-APP-S03`: every status traced to its evidence, through a real request.

The health view tells a reviewer *what*. This screen answers *why*, and the story
says what that has to mean: "check the reasoning rather than trusting the
conclusion". So a status here is never a value on its own -- it carries the rows
behind it, what observed them, when, and how sure the source was.

**The evidence is built by hand and the reasons are the same as
`test_health_projection.py`'s.** A real policy run over packages with no collector
evidence produces sentinels and cites nothing, which is honest and proves nothing
about tracing. What a fixture buys here is an observation at a *known* instant, and
more than one observation of the same fact -- which is the only way to test AC 3 at
all, since "superseded evidence remains reachable" needs something to have been
superseded.

**AC 3 is two claims and they pull against each other.** The history must be
reachable *and* the current value must stay unambiguous. A screen that showed only
the newest row would fail the first; one that showed all of them undifferentiated
would fail the second, and would be worse -- a reviewer reading three contradictory
advisory states with nothing marking which one the verdict rests on has been given
less than they started with. So every case below asserts both halves.

Every test rolls back: `@pytest.mark.django_db` wraps each in a transaction.
"""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import TYPE_CHECKING
from typing import Final

import pytest
from django.conf import settings
from django.contrib.auth.models import Group
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from conda_sentinel.collectors.match_confidence import MatchConfidence
from conda_sentinel.collectors.models import LicenseFinding
from conda_sentinel.collectors.models import PackageRecollection
from conda_sentinel.collectors.models import PythonReadinessAssessment
from conda_sentinel.collectors.models import PythonVerificationResult
from conda_sentinel.collectors.models import VulnerabilityFinding
from conda_sentinel.collectors.outcomes import MATCHED
from conda_sentinel.collectors.outcomes import VERIFIED_COMPATIBLE
from conda_sentinel.collectors.recollection import in_flight_window
from conda_sentinel.core.clock import SystemClock
from conda_sentinel.core.models import CollectionRun
from conda_sentinel.core.models import PackageHealth
from conda_sentinel.core.models import PolicyRun
from conda_sentinel.core.outcomes import OutcomeState
from conda_sentinel.core.runs import RunState
from conda_sentinel.identity.models import IdentityConfidence
from conda_sentinel.identity.models import IdentityOverride
from conda_sentinel.identity.models import Package
from conda_sentinel.policies.models import PackageCurrency
from conda_sentinel.policies.models import PackageLicense
from conda_sentinel.policies.models import PackagePriority
from conda_sentinel.policies.models import PackagePythonReadiness
from conda_sentinel.policies.models import PackageVulnerability
from conda_sentinel.policies.outcomes import ReadinessEvidence
from conda_sentinel.surface.detail import DERIVED_FROM_VERDICTS
from conda_sentinel.surface.detail import HISTORY_LIMIT
from conda_sentinel.surface.detail import NO_OBSERVATION_CITED
from conda_sentinel.surface.detail import TRACES
from tests.factories import UserFactory

if TYPE_CHECKING:
    from django.http import HttpResponse

pytestmark = pytest.mark.integration

RUN_AT: Final[datetime] = datetime(2026, 9, 4, 6, 12, tzinfo=UTC)
CUTOFF: Final[datetime] = RUN_AT - timedelta(minutes=32)
NEWEST: Final[datetime] = CUTOFF - timedelta(hours=1)
MIDDLE: Final[datetime] = CUTOFF - timedelta(days=1)
OLDEST: Final[datetime] = CUTOFF - timedelta(days=7)

A_POLICY_VERSION: Final[str] = "cpm-app-s03-fixture-policy"
A_PACKAGE: Final[str] = "aiohttp"
THE_SERIES: Final[str] = "3.14"

#: A canonical name carrying a dot, which is why the route takes `<str:>` rather
#: than `<slug:>`: `slug` matches neither dots nor underscores, so exactly the
#: packages with awkward names would be unreachable.
AN_AWKWARD_NAME: Final[str] = "ruamel.yaml"

#: How many observations of one fact the history cases record. One more than the
#: display cap, so the cap is exercised rather than merely declared.
MORE_THAN_FITS: Final[int] = HISTORY_LIMIT + 1

#: How many observations of one fact the superseding cases record. Three is the
#: smallest history where "the cited one" is neither simply the newest nor simply the
#: oldest, which is what makes the marking meaningful rather than coincidental.
A_SHORT_HISTORY: Final[int] = 3

#: Two, where a case needs a fact observed twice and nothing more.
OBSERVED_TWICE: Final[int] = 2


def a_run() -> PolicyRun:
    """Return one finished policy run.

    Returns:
        The saved run.

    """
    return PolicyRun.objects.create(
        policy_version=A_POLICY_VERSION,
        started_at=RUN_AT,
        finished_at=RUN_AT,
        evidence_cutoff=CUTOFF,
    )


def a_package(name: str = A_PACKAGE, *, confidence: str = IdentityConfidence.VERIFIED) -> Package:
    """Return one package at a stated identity confidence.

    Args:
        name: Its canonical name.
        confidence: How certain its identity is.

    Returns:
        The saved package.

    """
    return Package.objects.create(
        canonical_name=name,
        resolved_at=CUTOFF,
        confidence=confidence,
        identity_source="pypi",
        # `(identity_source, associator_key)` is unique: one upstream record maps to
        # one package, which is the invariant that stops two rows claiming the same
        # PyPI project. Derived from the name so a case can build a second package.
        associator_key=f"pypi:{name}",
    )


def a_rollup_row(package: Package, run: PolicyRun, **columns: str) -> PackageHealth:
    """Return one rollup row for a package at a run.

    Args:
        package: The package.
        run: The run.
        **columns: Contributed columns to override.

    Returns:
        The saved row.

    """
    return PackageHealth.objects.create(
        package=package,
        policy_run=run,
        computed_at=RUN_AT,
        evidence_cutoff=CUTOFF,
        confidence=package.confidence,
        policy_versions={"fixture": A_POLICY_VERSION},
        **columns,
    )


def an_advisory(package: Package, observed_at: datetime) -> VulnerabilityFinding:
    """Return one advisory finding that matched, with every fact its table requires.

    Args:
        package: The package the advisory is about.
        observed_at: When it was observed.

    Returns:
        The saved finding.

    """
    return VulnerabilityFinding.objects.create(
        package=package,
        observed_at=observed_at,
        state=MATCHED,
        advisory_id="CVE-2024-23334",
        severity="critical",
        affected_range="<3.9.2",
        matched_version="3.9.1",
        match_confidence=MatchConfidence.EXACT_VERSION,
        source="https://osv.dev/vulnerability/CVE-2024-23334",
    )


def a_reader() -> APIClient:
    """Return a client signed in as somebody who may read evidence.

    Returns:
        An authenticated client holding the security-reviewer role.

    """
    user = UserFactory.create()
    user.groups.add(Group.objects.get(name=settings.ROLE_CONTRACT.security_reviewer))
    client = APIClient()
    client.force_login(user)
    return client


def detail_url(name: str = A_PACKAGE) -> str:
    """Return a package's detail URL.

    Args:
        name: The canonical name.

    Returns:
        The path.

    """
    return reverse("conda_sentinel:package-detail", kwargs={"canonical_name": name})


def body_of(response: HttpResponse) -> str:
    """Return a response's rendered HTML.

    Args:
        response: The response.

    Returns:
        The decoded body.

    """
    return response.content.decode()


def traced(response: HttpResponse, label: str) -> object:
    """Return one status trace off a rendered response.

    Args:
        response: The response.
        label: The trace's label.

    Returns:
        The `StatusTrace`.

    """
    (trace,) = [entry for entry in response.context["traces"] if entry.label == label]
    return trace


# ---------------------------------------------------------------------------
# AC 1: each status links to the evidence behind it.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_a_status_carries_the_observation_that_produced_it() -> None:
    """AC 1: source, observation timestamp and confidence, on the row the run cited.

    All four asserted together, because each is a different question a reviewer
    checking the reasoning asks: what did it conclude, where did that come from, when
    was it seen, and how sure was the source.
    """
    run, package = a_run(), a_package()
    a_rollup_row(package, run)
    finding = an_advisory(package, NEWEST)
    PackageVulnerability.objects.create(
        package=package,
        policy_run=run,
        vulnerability_status="advisories_matched",
        kev_membership="not_established",
        risk_level="critical",
        policy_version=A_POLICY_VERSION,
        evidence_cutoff=CUTOFF,
        vulnerability_finding=finding,
    )

    trace = traced(a_reader().get(detail_url()), "Vulnerability")

    assert trace.status == "advisories_matched"  # type: ignore[attr-defined]
    assert trace.produced_from == "vulnerability_findings"  # type: ignore[attr-defined]
    (observation,) = trace.observations  # type: ignore[attr-defined]
    assert observation.observed_at == NEWEST
    assert observation.source == finding.source
    assert observation.match_confidence == MatchConfidence.EXACT_VERSION
    assert observation.cited is True


@pytest.mark.django_db
def test_the_evidence_shown_is_the_row_the_run_cited_rather_than_the_newest() -> None:
    """The claim `surface/detail.py` rests on, and the case that would catch its loss.

    The pass cites the *middle* observation while a newer one exists, which is a real
    state -- a collector that ran after the policy run's cut-off writes exactly this.
    A view that worked out the current row for itself would mark the newest, and would
    then be showing evidence the verdict does not rest on: `CPM-AD-10` forbids the
    application deciding what a pass decided, and this is what that would look like.
    """
    run, package = a_run(), a_package()
    a_rollup_row(package, run)
    an_advisory(package, NEWEST)
    cited = an_advisory(package, MIDDLE)
    PackageVulnerability.objects.create(
        package=package,
        policy_run=run,
        vulnerability_status="advisories_matched",
        kev_membership="not_established",
        policy_version=A_POLICY_VERSION,
        evidence_cutoff=CUTOFF,
        vulnerability_finding=cited,
    )

    trace = traced(a_reader().get(detail_url()), "Vulnerability")
    marked = [observation for observation in trace.observations if observation.cited]  # type: ignore[attr-defined]

    assert [observation.observed_at for observation in marked] == [MIDDLE]


@pytest.mark.django_db
def test_a_readiness_verdict_is_traced_to_whichever_evidence_it_rests_on() -> None:
    """`CPM-PY314-S03`: the kind of evidence is part of the verdict, and picks the trace.

    The row cites both relations and the assessment is the older, so a view that read
    `assessment` regardless would trace a proof to the metadata it superseded -- and
    would produce a plausible-looking answer rather than an empty one.
    """
    run, package = a_run(), a_package()
    a_rollup_row(package, run)
    assessment = PythonReadinessAssessment.objects.create(
        package=package,
        observed_at=OLDEST,
        state=OutcomeState.OK.value,
        python_series=THE_SERIES,
    )
    verification = PythonVerificationResult.objects.create(
        package=package,
        observed_at=NEWEST,
        state=VERIFIED_COMPATIBLE,
        python_series=THE_SERIES,
        platform="linux-64",
        architecture="x86_64",
        log_reference="builds/aiohttp/3.14/linux-64",
    )
    PackagePythonReadiness.objects.create(
        package=package,
        policy_run=run,
        readiness="verified_ready",
        evidence_type=ReadinessEvidence.VERIFIED,
        python_series=THE_SERIES,
        assessment=assessment,
        verification=verification,
        policy_version=A_POLICY_VERSION,
        evidence_cutoff=CUTOFF,
    )

    trace = traced(a_reader().get(detail_url()), "Py 3.14")

    assert trace.produced_from == "python_verification_results"  # type: ignore[attr-defined]
    assert [observation.observed_at for observation in trace.observations] == [NEWEST]  # type: ignore[attr-defined]


@pytest.mark.django_db
def test_a_status_derived_from_other_verdicts_says_so_rather_than_showing_nothing() -> None:
    """An empty evidence list on a screen about evidence reads as "we found nothing".

    `CPM-PRIORITY-S01` puts the priority pass downstream of other passes rather than
    of a collector, so there is genuinely no observation to cite -- which is a claim
    about the derivation, not about the world, and the two must not look alike.
    """
    run, package = a_run(), a_package()
    a_rollup_row(package, run, priority_status="p1")
    PackagePriority.objects.create(
        package=package,
        policy_run=run,
        bucket="p1",
        bucket_description="urgent",
        matched_rule="kev-listed",
        reason="listed in the KEV catalogue",
        score=96,
        policy_version=A_POLICY_VERSION,
        evidence_cutoff=CUTOFF,
    )

    trace = traced(a_reader().get(detail_url()), "Priority")

    assert trace.status == "p1"  # type: ignore[attr-defined]
    assert trace.produced_from == DERIVED_FROM_VERDICTS  # type: ignore[attr-defined]
    assert trace.observations == ()  # type: ignore[attr-defined]


@pytest.mark.django_db
def test_a_status_that_cited_no_observation_does_not_claim_to_be_derived() -> None:
    """The three states a "produced from" can be in, and the two that look alike.

    Currency is observed from a version surface. A row that reached no determinate
    verdict chose no authority and therefore cites no snapshot -- which is *not* the
    same as being derived from other verdicts, and saying so would be a false
    statement about how the product works on the one screen built to be checked.
    """
    run, package = a_run(), a_package()
    a_rollup_row(package, run)
    # A currency row that reached no determinate verdict, so it chose no authority
    # and cites no snapshot -- which the model's own constraint permits and the pass
    # writes for a package no version surface answered for.
    PackageCurrency.objects.create(
        package=package,
        policy_run=run,
        source_status=OutcomeState.UNKNOWN.value,
        pypi_status=OutcomeState.UNKNOWN.value,
        feedstock_status=OutcomeState.UNKNOWN.value,
        conda_package_status=OutcomeState.UNKNOWN.value,
        overall_status=OutcomeState.UNKNOWN.value,
    )
    # And a readiness row resting on neither kind of evidence.
    PackagePythonReadiness.objects.create(
        package=package,
        policy_run=run,
        readiness=OutcomeState.UNKNOWN.value,
        evidence_type=ReadinessEvidence.NONE,
        python_series=THE_SERIES,
        policy_version=A_POLICY_VERSION,
        evidence_cutoff=CUTOFF,
    )
    PackagePriority.objects.create(
        package=package,
        policy_run=run,
        bucket="p1",
        bucket_description="urgent",
        matched_rule="kev-listed",
        reason="listed in the KEV catalogue",
        score=96,
        policy_version=A_POLICY_VERSION,
        evidence_cutoff=CUTOFF,
    )

    response = a_reader().get(detail_url())

    assert traced(response, "Currency").produced_from == NO_OBSERVATION_CITED  # type: ignore[attr-defined]
    assert traced(response, "Py 3.14").produced_from == NO_OBSERVATION_CITED  # type: ignore[attr-defined]
    assert traced(response, "Priority").produced_from == DERIVED_FROM_VERDICTS  # type: ignore[attr-defined]


@pytest.mark.django_db
def test_every_status_the_health_view_shows_is_traced_here() -> None:
    """`CPM-AD-24`: every read surface projects the same values.

    A detail screen missing a status the table shows would send a reviewer looking
    for reasoning that is not there -- and the omission would be invisible, because
    the remaining rows would all look complete.
    """
    run, package = a_run(), a_package()
    a_rollup_row(package, run)

    response = a_reader().get(detail_url())

    assert len(response.context["traces"]) == len(TRACES)
    for trace in response.context["traces"]:
        assert trace.status != ""
        assert trace.produced_from != ""


# ---------------------------------------------------------------------------
# AC 2: the identity, its provenance, and any override.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_the_identity_shows_where_it_came_from() -> None:
    """AC 2's first half: provenance and confidence, not just a name.

    `CPM-AD-14` makes identity the product's one governed reference datum, so what a
    reviewer needs is how it was established -- and `CPM-AD-4` reads the confidence,
    which is why a fully populated identity panel can sit above statuses that are all
    `unknown`.
    """
    run, package = a_run(), a_package()
    a_rollup_row(package, run)

    response = a_reader().get(detail_url())
    identity = response.context["identity"]
    body = body_of(response)

    assert identity.confidence == IdentityConfidence.VERIFIED
    assert identity.identity_source == "pypi"
    assert identity.associator_key in body
    assert identity.identity_source in body


@pytest.mark.django_db
def test_a_human_correction_is_displayed_with_its_reason() -> None:
    """AC 2's second half, and the UX contract's own annotation: displayed, not hidden.

    A corrected identity that looked automatic would leave a reviewer unable to tell
    a resolver's confident match from a colleague's judgement call, and `CPM-AD-14`'s
    whole point is that the second is attributable. The reason is asserted in the
    body because it is the field that makes the correction reviewable rather than
    merely recorded.
    """
    run, package = a_run(), a_package()
    a_rollup_row(package, run)
    override = IdentityOverride.objects.create(
        package=package,
        observed_at=MIDDLE,
        actor=UserFactory.create(username="sam.ibarra"),
        prior_canonical_name="aiohttp-internal",
        new_canonical_name=A_PACKAGE,
        prior_confidence=IdentityConfidence.UNMAPPED,
        new_confidence=IdentityConfidence.VERIFIED,
        reason="Internal fork of the upstream package; same source repository.",
    )

    response = a_reader().get(detail_url())
    body = body_of(response)

    assert response.context["identity"].override == override
    assert override.actor.username in body
    assert override.reason in body
    assert override.prior_canonical_name in body


@pytest.mark.django_db
def test_a_package_nobody_corrected_shows_no_correction_panel() -> None:
    """The absence is meaningful and must not be furnished with an empty panel.

    A "Corrected by a human" heading over blank fields reads as a correction whose
    details were lost, which is worse than no panel at all.
    """
    run, package = a_run(), a_package()
    a_rollup_row(package, run)

    response = a_reader().get(detail_url())

    assert response.context["identity"].override is None
    assert "Corrected by a human" not in body_of(response)


@pytest.mark.django_db
def test_the_most_recent_correction_is_the_one_shown() -> None:
    """Two corrections are a real history, and the current identity rests on the last.

    Showing the first would attribute the current name to whoever happened to touch
    it earliest.
    """
    run, package = a_run(), a_package()
    a_rollup_row(package, run)
    IdentityOverride.objects.create(
        package=package,
        observed_at=OLDEST,
        actor=UserFactory.create(username="first.reviewer"),
        prior_canonical_name="wrong",
        new_canonical_name="also-wrong",
        prior_confidence=IdentityConfidence.UNMAPPED,
        new_confidence=IdentityConfidence.VERIFIED,
        reason="An earlier correction.",
    )
    latest = IdentityOverride.objects.create(
        package=package,
        observed_at=NEWEST,
        actor=UserFactory.create(username="second.reviewer"),
        prior_canonical_name="also-wrong",
        new_canonical_name=A_PACKAGE,
        prior_confidence=IdentityConfidence.VERIFIED,
        new_confidence=IdentityConfidence.VERIFIED,
        reason="The correction that stuck.",
    )

    assert a_reader().get(detail_url()).context["identity"].override == latest


# ---------------------------------------------------------------------------
# AC 3: superseded evidence stays reachable, and current stays unambiguous.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_superseded_observations_remain_reachable() -> None:
    """AC 3's first half. `CPM-AD-2` keeps them; this is what makes them visible.

    Three observations of one advisory, the newest cited. All three reach the screen
    -- a reviewer asking "what did this say last week" is asking the question this
    screen exists for.
    """
    run, package = a_run(), a_package()
    a_rollup_row(package, run)
    for observed_at in (OLDEST, MIDDLE):
        an_advisory(package, observed_at)
    cited = an_advisory(package, NEWEST)
    PackageVulnerability.objects.create(
        package=package,
        policy_run=run,
        vulnerability_status="advisories_matched",
        kev_membership="not_established",
        policy_version=A_POLICY_VERSION,
        evidence_cutoff=CUTOFF,
        vulnerability_finding=cited,
    )

    trace = traced(a_reader().get(detail_url()), "Vulnerability")

    assert [observation.observed_at for observation in trace.observations] == [NEWEST, MIDDLE, OLDEST]  # type: ignore[attr-defined]
    assert trace.observation_count == A_SHORT_HISTORY  # type: ignore[attr-defined]


@pytest.mark.django_db
def test_the_current_value_stays_unambiguous_among_them() -> None:
    """AC 3's second half, which pulls against the first.

    Three contradictory observations with nothing marking which the verdict rests on
    would leave a reviewer with less than they started with. Exactly one is marked,
    and it is the one the pass cited.
    """
    run, package = a_run(), a_package()
    a_rollup_row(package, run)
    for observed_at in (OLDEST, NEWEST):
        an_advisory(package, observed_at)
    cited = an_advisory(package, MIDDLE)
    PackageVulnerability.objects.create(
        package=package,
        policy_run=run,
        vulnerability_status="advisories_matched",
        kev_membership="not_established",
        policy_version=A_POLICY_VERSION,
        evidence_cutoff=CUTOFF,
        vulnerability_finding=cited,
    )

    trace = traced(a_reader().get(detail_url()), "Vulnerability")

    assert [observation.cited for observation in trace.observations].count(True) == 1  # type: ignore[attr-defined]
    assert next(o for o in trace.observations if o.cited).observed_at == MIDDLE  # type: ignore[attr-defined]


@pytest.mark.django_db
def test_nothing_is_deleted_to_show_the_current_value() -> None:
    """`CPM-AD-2` restated as a request: rendering the screen writes nothing.

    Asserted over the rows themselves rather than a count, because the mutation a
    read surface could plausibly perform is an *update* -- reaching for `save()` on a
    row it read -- and a count catches an insert or a delete but not that.
    """
    run, package = a_run(), a_package()
    a_rollup_row(package, run)
    for observed_at in (OLDEST, MIDDLE, NEWEST):
        an_advisory(package, observed_at)
    before = list(VulnerabilityFinding.objects.order_by("id").values())

    a_reader().get(detail_url())

    assert list(VulnerabilityFinding.objects.order_by("id").values()) == before


@pytest.mark.django_db
def test_a_long_history_is_bounded_and_says_how_much_it_is_showing() -> None:
    """The display cap, and the count beside it that makes the cap honest.

    A list of twenty alone is indistinguishable from the whole of a record. A list of
    twenty beside "21 observations held" is a bounded view of a complete one, which
    is what lets a reviewer know there is more rather than assume there is not.
    """
    run, package = a_run(), a_package()
    a_rollup_row(package, run)
    findings = [an_advisory(package, NEWEST - timedelta(days=index)) for index in range(MORE_THAN_FITS)]
    PackageVulnerability.objects.create(
        package=package,
        policy_run=run,
        vulnerability_status="advisories_matched",
        kev_membership="not_established",
        policy_version=A_POLICY_VERSION,
        evidence_cutoff=CUTOFF,
        vulnerability_finding=findings[0],
    )

    trace = traced(a_reader().get(detail_url()), "Vulnerability")

    assert len(trace.observations) == HISTORY_LIMIT  # type: ignore[attr-defined]
    assert trace.observation_count == MORE_THAN_FITS  # type: ignore[attr-defined]


@pytest.mark.django_db
def test_the_licence_history_is_traced_too() -> None:
    """One more domain, so the tracing is a mechanism rather than one special case."""
    run, package = a_run(), a_package()
    a_rollup_row(package, run)
    LicenseFinding.objects.create(
        package=package,
        observed_at=OLDEST,
        state=OutcomeState.OK.value,
        channel="conda-forge",
        raw_license="GPL-3.0",
    )
    cited = LicenseFinding.objects.create(
        package=package,
        observed_at=NEWEST,
        state=OutcomeState.OK.value,
        channel="conda-forge",
        raw_license="Apache-2.0",
    )
    PackageLicense.objects.create(
        package=package,
        policy_run=run,
        license_outcome="allowed",
        matched_rule="permissive",
        policy_version=A_POLICY_VERSION,
        evidence_cutoff=CUTOFF,
        license_finding=cited,
    )

    trace = traced(a_reader().get(detail_url()), "Licence")

    assert trace.observation_count == OBSERVED_TWICE  # type: ignore[attr-defined]
    assert next(o for o in trace.observations if o.cited).observed_at == NEWEST  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# The screen is still a read surface.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_an_unmapped_package_is_reachable_and_its_verdicts_are_gated() -> None:
    """The package a reviewer most needs to open, and the one a gate could hide.

    `CPM-AD-11` gives every inventory package a rollup row "including unmapped ones",
    so there is no package this view cannot open -- and the statuses are gated here
    exactly as on the health view, so the two screens cannot disagree.
    """
    run = a_run()
    package = a_package("internal-telemetry-sdk", confidence=IdentityConfidence.UNMAPPED)
    a_rollup_row(package, run)
    finding = an_advisory(package, NEWEST)
    PackageVulnerability.objects.create(
        package=package,
        policy_run=run,
        vulnerability_status="advisories_matched",
        kev_membership="not_established",
        policy_version=A_POLICY_VERSION,
        evidence_cutoff=CUTOFF,
        vulnerability_finding=finding,
    )

    response = a_reader().get(detail_url("internal-telemetry-sdk"))

    assert response.status_code == status.HTTP_200_OK
    assert traced(response, "Vulnerability").status == OutcomeState.UNKNOWN.value  # type: ignore[attr-defined]
    assert response.context["identity"].confidence == IdentityConfidence.UNMAPPED


@pytest.mark.django_db
def test_the_evidence_behind_a_gated_verdict_is_still_listed() -> None:
    """`CPM-AD-4` suppresses a claim, not the record that a lookup happened.

    "We read this advisory at 05:38" is not an assertion about the package's
    identity, and keeping it is what lets a reviewer working the identity queue see
    there is something waiting behind the gate.
    """
    run = a_run()
    package = a_package("internal-telemetry-sdk", confidence=IdentityConfidence.UNMAPPED)
    a_rollup_row(package, run)
    finding = an_advisory(package, NEWEST)
    PackageVulnerability.objects.create(
        package=package,
        policy_run=run,
        vulnerability_status="advisories_matched",
        kev_membership="not_established",
        policy_version=A_POLICY_VERSION,
        evidence_cutoff=CUTOFF,
        vulnerability_finding=finding,
    )

    trace = traced(a_reader().get(detail_url("internal-telemetry-sdk")), "Vulnerability")

    assert [observation.observed_at for observation in trace.observations] == [NEWEST]  # type: ignore[attr-defined]


@pytest.mark.django_db
def test_a_canonical_name_carrying_a_dot_is_reachable() -> None:
    """Why the route takes `<str:>` and not `<slug:>`.

    `slug` matches neither dots nor underscores, so `ruamel.yaml` and
    `backports.zoneinfo` -- ordinary conda-forge packages -- would 404 while their
    rows sat in the table, and the health view would link straight at them.
    """
    run, package = a_run(), a_package(AN_AWKWARD_NAME)
    a_rollup_row(package, run)

    response = a_reader().get(detail_url(AN_AWKWARD_NAME))

    assert response.status_code == status.HTTP_200_OK
    assert response.context["package"].canonical_name == AN_AWKWARD_NAME


@pytest.mark.django_db
def test_a_package_that_does_not_exist_is_a_404() -> None:
    """Not a blank screen, and not an empty trace list that reads as "nothing known"."""
    assert a_reader().get(detail_url("no-such-package")).status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.django_db
def test_a_reader_holding_no_role_is_refused() -> None:
    """`CPM-AD-13`. The evidence screen is a read surface, and read surfaces still check."""
    run, package = a_run(), a_package()
    a_rollup_row(package, run)
    client = APIClient()
    client.force_login(UserFactory.create())

    assert client.get(detail_url()).status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.django_db
@pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
def test_the_view_offers_no_write_method(method: str) -> None:
    """`CPM-AD-10`, asserted at the HTTP boundary where a mixin would open one.

    Args:
        method: The HTTP method to attempt.

    """
    run, package = a_run(), a_package()
    a_rollup_row(package, run)

    response = getattr(a_reader(), method)(detail_url())

    assert response.status_code == status.HTTP_405_METHOD_NOT_ALLOWED


@pytest.mark.django_db
def test_the_run_ledger_is_shown_and_is_labelled_as_not_evidence() -> None:
    """Why "is this stale" is usually answered by a failed run rather than by evidence.

    `CPM-AD-2` exempts the ledger from append-only precisely so a run killed mid-call
    leaves a row saying `running`; showing it beside the evidence is what makes that
    exemption useful, and labelling it is what stops it being read as evidence.
    """
    run, package = a_run(), a_package()
    a_rollup_row(package, run)
    CollectionRun.objects.create(
        collector="vulnerability",
        package=package,
        started_at=MIDDLE,
        finished_at=MIDDLE,
        status=RunState.FAILED,
        detail="429 rate limited",
    )

    response = a_reader().get(detail_url())
    body = body_of(response)

    assert len(response.context["runs"]) == 1
    assert "429 rate limited" in body
    assert "not evidence" in body


@pytest.mark.django_db
def test_runs_in_flight_are_listed_above_the_ledger_and_a_killed_workers_row_is_not() -> None:
    """`CPM-OPERATE-S08`'s panel, on the recollection's own rule.

    Two open rows: one started minutes ago is in flight; one started twice the
    window ago is a killed worker's, is not in the panel, and still appears in
    the ledger as `running` -- which is the observation `CPM-AD-2`'s exemption
    exists to make possible.
    """
    run, package = a_run(), a_package()
    a_rollup_row(package, run)
    now = SystemClock().now()
    CollectionRun.objects.create(
        collector="feedstock", package=package, started_at=now - timedelta(minutes=3), status=RunState.RUNNING
    )
    CollectionRun.objects.create(
        collector="kev",
        package=package,
        started_at=now - in_flight_window() * 2,
        status=RunState.RUNNING,
    )

    response = a_reader().get(detail_url())
    body = body_of(response)

    assert [entry.collector for entry in response.context["in_flight"].runs] == ["feedstock"]
    assert response.context["in_flight"].pending is None
    assert [entry.collector for entry in response.context["runs"]] == ["feedstock", "kev"]
    assert "In flight" in body
    assert "1 run open" in body
    # Both open rows are drawn as running, in the ledger and -- for the one in
    # flight -- in the panel above it too.
    assert body.count('class="chip tone-info"') == len(response.context["runs"]) + len(
        response.context["in_flight"].runs
    )


@pytest.mark.django_db
def test_the_last_recollection_is_shown_beneath_the_ledger_naming_who_asked() -> None:
    """The audit row, read as the record of a human act: actor, when, collectors, and what was not offered."""
    run, package = a_run(), a_package()
    a_rollup_row(package, run)
    actor = UserFactory.create(username="who-pressed")
    PackageRecollection.objects.create(
        package=package,
        actor=actor,
        observed_at=OLDEST,
        collectors=["feedstock"],
        not_offered=["pypi_release"],
        trace_id="0" * 31 + "1",
    )
    PackageRecollection.objects.create(
        package=package,
        actor=actor,
        observed_at=MIDDLE,
        collectors=["source_release", "resolve_identity"],
        not_offered=[],
        trace_id="",
    )

    response = a_reader().get(detail_url())
    body = body_of(response)

    assert response.context["recollection"].observed_at == MIDDLE, "the newest, not the first"
    assert "Last recollection" in body
    assert "who-pressed" in body
    assert "<dt>asked</dt>" in body, "asked for, not published: the row is written before the hand-off"
    assert "source_release, resolve_identity" in body
    assert "not offered" not in body, "an empty list draws no row"
    # Both presses are older than the window, so neither is pending and the
    # button is offered.
    assert response.context["in_flight"].pending is None


@pytest.mark.django_db
def test_a_package_nobody_has_recollected_shows_neither_panel() -> None:
    """Absence draws nothing: no empty in-flight panel, no blank last-recollection panel."""
    run, package = a_run(), a_package()
    a_rollup_row(package, run)

    response = a_reader().get(detail_url())
    body = body_of(response)

    assert not response.context["in_flight"]
    assert response.context["recollection"] is None
    assert "In flight" not in body
    assert "Last recollection" not in body


@pytest.mark.django_db
def test_the_query_count_does_not_grow_with_the_evidence() -> None:
    """Not an acceptance criterion, and worth bounding anyway.

    The screen reads eight derived rows and, for each that cites evidence, a page of
    that evidence and a count of it. That is a fixed number of reads. What it must
    not be is a number that grows with *how much* evidence there is -- a package
    watched for a year has three hundred advisory rows, and a screen that cost a
    query per row would be unusable on exactly the packages a reviewer opens most.

    Compared at two very different histories rather than asserted against a
    constant: the absolute number is an implementation detail that a later story may
    reasonably change, while "the same for one row as for twenty-one" is the property
    that has to hold.
    """
    run, package = a_run(), a_package()
    a_rollup_row(package, run)
    findings = [an_advisory(package, NEWEST - timedelta(days=index)) for index in range(MORE_THAN_FITS)]
    PackageVulnerability.objects.create(
        package=package,
        policy_run=run,
        vulnerability_status="advisories_matched",
        kev_membership="not_established",
        policy_version=A_POLICY_VERSION,
        evidence_cutoff=CUTOFF,
        vulnerability_finding=findings[0],
    )
    client = a_reader()

    with CaptureQueriesContext(connection) as long_history:
        client.get(detail_url())

    other = a_package("orjson")
    a_rollup_row(other, run)
    lone = an_advisory(other, NEWEST)
    PackageVulnerability.objects.create(
        package=other,
        policy_run=run,
        vulnerability_status="advisories_matched",
        kev_membership="not_established",
        policy_version=A_POLICY_VERSION,
        evidence_cutoff=CUTOFF,
        vulnerability_finding=lone,
    )
    with CaptureQueriesContext(connection) as short_history:
        client.get(detail_url("orjson"))

    assert len(long_history) == len(short_history)
