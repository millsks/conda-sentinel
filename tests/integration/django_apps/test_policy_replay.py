"""`CPM-FR-22`: a replay reproduces the answer, records what it was, and touches no evidence.

`CPM-PRIORITY-S03` is three claims and every one of them is only true or false once
two runs exist, so this module is the whole story. The per-domain passes each already
have a replay case of their own; what none of them can say is whether *a whole run*
reproduces, which is what the requirement is actually about.

**The comparison is the subject, not the mechanism.** `execute_policy_run` has taken
a cut-off since the orchestration was built, so a replay has always been *possible*.
What `CPM-FR-22` asks is whether it *reproduces*, and a guarantee nobody can evaluate
after the fact is a guarantee about the code rather than about the data. So these
cases run two real runs over eight real passes and compare every column of every
derived row.

**The case that matters most is the one where evidence moved.** A replay that
reproduces because nothing changed proves only that the passes are not random. The
load-bearing case writes *new evidence after the cut-off* between the two runs and
asserts the replay still reproduces — which is what "against historical evidence"
means, and what would break the moment a pass read a row the cut-off excludes.

**A stated cut-off has to be one a run in the ledger read at (`CPM-OPERATE-S07`).**
The nightly purge keeps what a run still in the ledger read at its cut-off and
nothing else past the retention, so the command admits `--evidence-cutoff` only at
such an instant; the case that states one runs a policy run at it first. The rule
itself -- and `--of-run` reaching any run the ledger holds, however old -- is
`tests/integration/django_apps/test_retention.py`'s subject.

Every test here rolls back: `@pytest.mark.django_db` wraps each in a transaction.
"""

from __future__ import annotations

from io import StringIO
from typing import TYPE_CHECKING
from typing import Final

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from conda_sentinel.collectors.models import PyPIReleaseSnapshot
from conda_sentinel.collectors.models import SourceReleaseSnapshot
from conda_sentinel.core.clock import FixedClock
from conda_sentinel.core.management.commands import replay_policy_run as replay_command
from conda_sentinel.core.models import CollectionRun
from conda_sentinel.core.models import PackageHealth
from conda_sentinel.core.models import PolicyRun
from conda_sentinel.core.outcomes import OutcomeState
from conda_sentinel.core.policy import registered_passes
from conda_sentinel.core.policy_run import execute_policy_run
from conda_sentinel.core.replay import EXCLUDED_COLUMNS
from conda_sentinel.core.replay import compare_runs
from conda_sentinel.core.runs import RunState
from conda_sentinel.identity.confidence import IdentityConfidence
from conda_sentinel.identity.models import Package
from conda_sentinel.policies.models import PackageWorkType
from tests.clocks import FIXED_INSTANT
from tests.clocks import LATER_INSTANT
from tests.clocks import OBSERVATION_GAP
from tests.passes import A_RECORDED_POLICY_VERSION

if TYPE_CHECKING:
    from datetime import datetime

#: The collector name the fixture collection runs are recorded under.
A_COLLECTOR: Final[str] = "inventory"

#: How many runs the comparison cases execute.
TWO_RUNS: Final[int] = 2


def an_ended_collection_run(finished_at: datetime = FIXED_INSTANT) -> CollectionRun:
    """Record a collection run that has ended, which is what supplies a cut-off.

    Args:
        finished_at: When the run ended.

    Returns:
        The saved row.

    """
    return CollectionRun.objects.create(
        collector=A_COLLECTOR,
        started_at=FIXED_INSTANT,
        finished_at=finished_at,
        status=RunState.SUCCEEDED,
    )


def a_package(name: str = "numpy", *, confidence: str = IdentityConfidence.VERIFIED) -> Package:
    """Create one package with a resolved identity.

    Args:
        name: Its canonical name, which is unique.
        confidence: How certain its identity is.

    Returns:
        The saved row.

    """
    return Package.objects.create(canonical_name=name, resolved_at=FIXED_INSTANT, confidence=confidence)


def a_release(package: Package, *, version: str, observed_at: datetime = FIXED_INSTANT) -> None:
    """Record what the upstream source and PyPI stated about a package.

    Two surfaces, so the currency pass has something to compare and the derived rows
    a replay reproduces are not all sentinels.

    Args:
        package: The package observed.
        version: The version the source states. PyPI is left one behind, so the
            currency pass reaches a determinate verdict.
        observed_at: When the observation was made.

    """
    SourceReleaseSnapshot.objects.create(
        observed_at=observed_at,
        package=package,
        source="https://example.invalid/releases",
        state=OutcomeState.OK.value,
        latest_version=version,
        released_at=observed_at,
    )
    PyPIReleaseSnapshot.objects.create(
        observed_at=observed_at,
        package=package,
        source="https://pypi.org/pypi/numpy/json",
        state=OutcomeState.OK.value,
        latest_version="0.0.1",
        released_at=observed_at,
    )


def a_run(*, at: datetime = LATER_INSTANT, cutoff: datetime = FIXED_INSTANT) -> PolicyRun:
    """Execute one policy run at a stated cut-off.

    Args:
        at: The instant the run's clock answers.
        cutoff: The instant to read evidence as of.

    Returns:
        The run's ledger row.

    """
    return execute_policy_run(
        policy_version=A_RECORDED_POLICY_VERSION,
        clock=FixedClock(instant=at),
        evidence_cutoff=cutoff,
    ).policy_run


# ---------------------------------------------------------------------------
# AC 1: a replay reproduces, and needs no recollection.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_replaying_a_version_at_a_cutoff_reproduces_every_derived_row() -> None:
    """AC 1 over a whole run: eight passes, every column of every row.

    The per-domain cases each assert their own table; this is the one that says the
    *run* reproduced, which is what `CPM-FR-22` states and what a compliance reviewer
    is actually asking.
    """
    an_ended_collection_run()
    package = a_package()
    a_release(package, version="2.0.0")

    original = a_run()
    replayed = a_run(at=LATER_INSTANT + OBSERVATION_GAP)

    report = compare_runs(original, replayed)
    assert report.reproduced, [difference.describe() for difference in report.differences]
    assert report.compared_rows > 0, "a report over no rows is not a reproduction"


@pytest.mark.django_db
def test_a_replay_reproduces_even_after_new_evidence_arrives() -> None:
    """The load-bearing case: "against historical evidence" means the cut-off bounds it.

    A replay that reproduces because nothing changed proves only that the passes are
    not random. Here the world moves between the two runs -- a later observation
    states a different version -- and the replay still reproduces, because every pass
    reads `observed_at <= cutoff`. A pass that read the newest row instead would pass
    every other case in this file and fail this one.
    """
    an_ended_collection_run()
    package = a_package()
    a_release(package, version="2.0.0")
    original = a_run()

    a_release(package, version="9.9.9", observed_at=FIXED_INSTANT + OBSERVATION_GAP)
    replayed = a_run(at=LATER_INSTANT + OBSERVATION_GAP)

    report = compare_runs(original, replayed)
    assert report.reproduced, [difference.describe() for difference in report.differences]


@pytest.mark.django_db
def test_a_replay_collects_nothing() -> None:
    """AC 1's second half: it requires no recollection.

    Asserted as the evidence tables being untouched *and* as no collection run being
    opened. Evidence is append-only, so a collector that ran would leave a new row
    rather than change one -- which a count catches and an equality check would not.
    """
    an_ended_collection_run()
    package = a_package()
    a_release(package, version="2.0.0")
    a_run()
    before = (SourceReleaseSnapshot.objects.count(), PyPIReleaseSnapshot.objects.count())
    runs_before = CollectionRun.objects.count()

    a_run(at=LATER_INSTANT + OBSERVATION_GAP)

    assert (SourceReleaseSnapshot.objects.count(), PyPIReleaseSnapshot.objects.count()) == before
    assert CollectionRun.objects.count() == runs_before


@pytest.mark.django_db
def test_a_run_at_a_different_cutoff_is_not_a_replay_and_the_comparison_says_so() -> None:
    """The negative control, without which "reproduced" proves nothing.

    A comparison that always reported success would satisfy every case above. Two
    runs over *different* cut-offs read different evidence and must differ -- and the
    difference names a column and a package, because that is what a reviewer acts on.
    """
    an_ended_collection_run()
    an_ended_collection_run(finished_at=FIXED_INSTANT + OBSERVATION_GAP)
    package = a_package()
    a_release(package, version="2.0.0")
    original = a_run()

    a_release(package, version="9.9.9", observed_at=FIXED_INSTANT + OBSERVATION_GAP)
    moved = a_run(at=LATER_INSTANT + OBSERVATION_GAP, cutoff=FIXED_INSTANT + OBSERVATION_GAP)

    report = compare_runs(original, moved)
    assert not report.reproduced
    assert any(difference.package_id == package.pk for difference in report.differences)
    assert all(difference.column not in EXCLUDED_COLUMNS for difference in report.differences)


@pytest.mark.django_db
def test_the_comparison_covers_every_registered_pass() -> None:
    """A comparison that read six of eight tables would report success over the two it skipped.

    Asserted as the set of tables the report *could* name, taken from the registry
    itself, so a ninth pass is covered the day it is adopted.
    """
    an_ended_collection_run()
    package = a_package()
    a_release(package, version="2.0.0")
    original = a_run()
    moved = a_run(at=LATER_INSTANT + OBSERVATION_GAP)

    report = compare_runs(original, moved)
    expected = sum(
        policy_pass.derived_model._default_manager.filter(policy_run=original).count()  # noqa: SLF001 - Django's API
        for policy_pass in registered_passes()
    )

    assert report.compared_rows == expected
    assert expected >= len(registered_passes()), "every pass writes a row per package, so one package is one each"


# ---------------------------------------------------------------------------
# AC 2 and AC 3.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_a_completed_run_records_its_version_its_instants_its_cutoff_and_its_status() -> None:
    """AC 2, asserted as the four the requirement names on one finished row."""
    an_ended_collection_run()
    a_package()

    run = a_run()

    assert run.policy_version == A_RECORDED_POLICY_VERSION
    assert run.evidence_cutoff == FIXED_INSTANT
    assert run.started_at is not None
    assert run.finished_at is not None
    assert run.status == RunState.SUCCEEDED.value


@pytest.mark.django_db
def test_a_policy_run_mutates_no_evidence() -> None:
    """AC 3, asserted over the rows themselves rather than over a count.

    A count catches an insert and a delete; this catches an *update*, which is the
    one a policy pass could plausibly perform by reaching for `save()` on a row it
    read. The primary keys and every compared value are captured before and after.
    """
    an_ended_collection_run()
    package = a_package()
    a_release(package, version="2.0.0")
    before = list(
        SourceReleaseSnapshot.objects.order_by("pk").values_list("pk", "state", "latest_version", "observed_at"),
    )

    a_run()

    assert (
        list(
            SourceReleaseSnapshot.objects.order_by("pk").values_list(
                "pk",
                "state",
                "latest_version",
                "observed_at",
            ),
        )
        == before
    )


@pytest.mark.django_db
def test_a_replay_leaves_the_run_it_replayed_alone() -> None:
    """`CPM-AD-21` keys a derived table by run, so a replay adds rows rather than editing them.

    Without this, "reproduced" could mean "the second run overwrote the first and
    then agreed with itself".
    """
    an_ended_collection_run()
    package = a_package()
    a_release(package, version="2.0.0")
    original = a_run()
    original_finished = original.finished_at

    a_run(at=LATER_INSTANT + OBSERVATION_GAP)

    original.refresh_from_db()
    assert original.finished_at == original_finished
    assert PolicyRun.objects.count() == TWO_RUNS


@pytest.mark.django_db
def test_a_replay_overwrites_current_health_which_is_why_the_command_warns() -> None:
    """The operational consequence, asserted rather than only documented.

    `CPM-AD-11` gives `package_health` one row per package and the writer replaces
    it, so a replay of an older cut-off leaves current health showing what was true
    then. The row says so -- it carries the replay's `computed_at` and the replayed
    `evidence_cutoff` -- and that visibility is the whole reason this is acceptable
    rather than a defect. The command's confirmation is what makes it a choice.
    """
    an_ended_collection_run()
    package = a_package()
    a_release(package, version="2.0.0")
    a_run()

    replayed = a_run(at=LATER_INSTANT + OBSERVATION_GAP)

    row = PackageHealth.objects.get(package=package)
    assert PackageHealth.objects.count() == 1
    assert row.policy_run_id == replayed.pk
    assert row.evidence_cutoff == FIXED_INSTANT


# ---------------------------------------------------------------------------
# The command a reviewer runs.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_the_command_replays_a_named_run_and_reports_that_it_reproduced() -> None:
    """The front door, end to end, on the form a reviewer actually uses."""
    an_ended_collection_run()
    package = a_package()
    a_release(package, version="2.0.0")
    original = a_run()
    output = StringIO()

    call_command("replay_policy_run", "--of-run", str(original.pk), "--no-input", stdout=output)

    assert "reproduced it exactly" in output.getvalue()
    assert PolicyRun.objects.count() == TWO_RUNS


@pytest.mark.django_db
def test_the_command_warns_about_current_health_before_it_runs() -> None:
    """The one consequence a reviewer would not predict, said before it happens."""
    an_ended_collection_run()
    a_package()
    original = a_run()
    output = StringIO()

    call_command("replay_policy_run", "--of-run", str(original.pk), "--no-input", stdout=output)

    assert "package_health" in output.getvalue()


@pytest.mark.django_db
def test_the_command_fails_when_the_replay_did_not_reproduce() -> None:
    """A non-zero exit is the reviewer's answer, and the report is why.

    Driven by replaying a run whose derived rows were tampered with, which is the
    only way to make a deterministic pass disagree with itself -- and is exactly the
    shape of the defect the command exists to catch.
    """
    an_ended_collection_run()
    package = a_package()
    a_release(package, version="2.0.0")
    original = a_run()
    PackageWorkType.objects.filter(policy_run=original).update(detail="something nobody derived")

    with pytest.raises(CommandError, match="did NOT reproduce"):
        call_command("replay_policy_run", "--of-run", str(original.pk), "--no-input", stdout=StringIO())


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        ([], "name what to replay"),
        (["--policy-version", A_RECORDED_POLICY_VERSION], "name what to replay"),
        (["--of-run", "1", "--policy-version", "x"], "do not also pass them"),
        (["--of-run", "999999"], "no policy run has id"),
        (["--policy-version", "x", "--evidence-cutoff", "not-a-date"], "not an ISO 8601 datetime"),
        (["--policy-version", "x", "--evidence-cutoff", "2026-09-04T12:00:00"], "carries no timezone"),
    ],
    ids=["nothing", "half-stated", "both-forms", "no-such-run", "unparseable", "naive"],
)
def test_the_command_refuses_arguments_that_name_no_single_run(arguments: list[str], expected: str) -> None:
    """Every way the two forms can be got wrong, refused before anything is run.

    The naive cut-off is the one worth naming: every instant this product records is
    aware, so a naive one would read a window nobody chose -- silently, and only
    visibly in the results.

    Args:
        arguments: What the reviewer typed.
        expected: What the refusal says.

    """
    with pytest.raises(CommandError, match=expected):
        call_command("replay_policy_run", *arguments, "--no-input", stdout=StringIO())


@pytest.mark.django_db
def test_a_stated_version_and_cutoff_run_without_a_comparison_and_say_so() -> None:
    """The second form: there is nothing to compare against, and the output does not pretend.

    A command that reported success here would be reporting that a run happened,
    which is not what a reviewer reads it for.
    """
    an_ended_collection_run()
    a_package()
    a_run()
    output = StringIO()

    call_command(
        "replay_policy_run",
        "--policy-version",
        A_RECORDED_POLICY_VERSION,
        "--evidence-cutoff",
        FIXED_INSTANT.isoformat(),
        "--no-input",
        stdout=output,
    )

    assert "nothing was compared" in output.getvalue()


@pytest.mark.django_db
def test_declining_the_confirmation_runs_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """The confirmation is what turns rewriting current health into a choice.

    Asserted as *nothing happened* rather than as the message alone: a prompt that
    refused and then ran anyway would print the same thing.

    Args:
        monkeypatch: Answers the prompt with something that is not the confirmation.

    """
    an_ended_collection_run()
    a_package()
    original = a_run()
    monkeypatch.setattr("builtins.input", lambda _prompt: "no")

    with pytest.raises(CommandError, match="was not confirmed"):
        call_command("replay_policy_run", "--of-run", str(original.pk), stdout=StringIO())

    assert PolicyRun.objects.count() == 1


@pytest.mark.django_db
def test_a_report_longer_than_the_printed_limit_says_how_many_more(monkeypatch: pytest.MonkeyPatch) -> None:
    """A replay that differs everywhere prints the first few and counts the rest.

    Twenty thousand lines is not a report. The first few are what a reviewer
    diagnoses from and the count is what tells them how bad it is, so both halves
    are asserted.

    Args:
        monkeypatch: Shrinks the printed limit so the case needs two packages rather
            than twenty-one -- the limit is a presentation choice, and a case that
            depended on its value would break the day somebody tuned it.

    """
    monkeypatch.setattr(replay_command, "REPORTED_DIFFERENCES", 1)
    an_ended_collection_run()
    for name in ("aaa", "bbb"):
        a_release(a_package(name), version="2.0.0")
    original = a_run()
    PackageWorkType.objects.filter(policy_run=original).update(detail="something nobody derived")
    output = StringIO()

    with pytest.raises(CommandError, match="did NOT reproduce"):
        call_command("replay_policy_run", "--of-run", str(original.pk), "--no-input", stdout=output)

    printed = output.getvalue()
    assert "package_work_type" in printed
    assert "and 1 more" in printed
