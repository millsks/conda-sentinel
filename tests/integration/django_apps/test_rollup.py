"""`EVIDENCE.07-INT-001`: one row per package, replaced rather than accumulated, stamps intact.

`CPM-AD-11` is four promises about a table, and each of them is only true against
a real one. Exactly one row per `identity.Package`, including the packages nothing
has resolved. A *full-row replace* rather than a merge, so a column never holds a
verdict from a run the row no longer names. The stamps -- the run, the cut-off,
the instant and the per-domain version map -- written by the one writer. And two
domains surviving one compose, which is the property the single-writer rule
exists for: a writer that reset another domain's table would satisfy every
single-domain assertion there is.

**The contributed columns are real here, and so is the gate.** `epics.md` says
the rollup "grows as passes are added"; `CPM-CURRENCY-S06` added `currency_status`
and `CPM-CURRENCY-S07` added `feedstock_presence_status`, each adopting the pass
that produces it -- so every run in this module executes those two domains before
any fixture pass, and every row composed here carries two gated verdicts. What
the `unmapped` cases assert is both halves of what `CPM-AD-4` requires: the row
*exists* and records the confidence it was computed at, and the verdict in every
contributable column has been replaced. Expressing the gate as writing a value
rather than as suppressing a row is what makes the first half assertable at all.

**A policy version this component records is a precondition here now.**
`FeedstockPresencePass` refuses a version `policies/data/policy-parameters.toml`
does not record, so `_recorded_parameters` below substitutes a file naming this
module's two fixture versions. Without it every run here would fail every package
for a reason that has nothing to do with the rollup.

Every case here rolls back. `@pytest.mark.django_db` wraps each in a transaction;
the fixture derived tables are built once for the session by `conftest.py`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from typing import Final

import pytest
import structlog
from django.db import IntegrityError
from django.db import transaction
from django.db.models import ProtectedError

from conda_sentinel.collectors.models import FeedstockSnapshot
from conda_sentinel.collectors.models import SourceReleaseSnapshot
from conda_sentinel.core import rollup as rollup_module
from conda_sentinel.core.clock import FixedClock
from conda_sentinel.core.confidence import GATED_VALUE
from conda_sentinel.core.models import CollectionRun
from conda_sentinel.core.models import PackageHealth
from conda_sentinel.core.models import PolicyRun
from conda_sentinel.core.outcomes import OutcomeState
from conda_sentinel.core.policy_run import execute_policy_run
from conda_sentinel.core.rollup import AFTER_RUN_COLUMNS
from conda_sentinel.core.rollup import ROLLUP_WRITE_FAILED_EVENT
from conda_sentinel.core.rollup import ROLLUP_WRITTEN_EVENT
from conda_sentinel.core.rollup import STAMP_COLUMNS
from conda_sentinel.core.rollup import contributable_columns
from conda_sentinel.core.runs import RunState
from conda_sentinel.identity.models import IdentityConfidence
from conda_sentinel.identity.models import Package
from conda_sentinel.policies.currency import POLICY_NAME as CURRENCY_POLICY_NAME
from conda_sentinel.policies.currency import ROLLUP_COLUMN
from conda_sentinel.policies.feedstock import POLICY_NAME as FEEDSTOCK_POLICY_NAME
from conda_sentinel.policies.feedstock import ROLLUP_COLUMN as FEEDSTOCK_ROLLUP_COLUMN
from conda_sentinel.policies.licence import POLICY_NAME as LICENCE_POLICY_NAME
from conda_sentinel.policies.outcomes import BEHIND
from conda_sentinel.policies.outcomes import FILE_TRACKING_ISSUE
from conda_sentinel.policies.outcomes import PRESENT_AND_MAINTAINED
from conda_sentinel.policies.priority import POLICY_NAME as PRIORITY_POLICY_NAME
from conda_sentinel.policies.priority import ROLLUP_COLUMN as PRIORITY_ROLLUP_COLUMN
from conda_sentinel.policies.py314_readiness import POLICY_NAME as PY314_READINESS_POLICY_NAME
from conda_sentinel.policies.remediation import POLICY_NAME as REMEDIATION_POLICY_NAME
from conda_sentinel.policies.vulnerability import POLICY_NAME as VULNERABILITY_POLICY_NAME
from conda_sentinel.policies.work_type import POLICY_NAME as WORK_TYPE_POLICY_NAME
from conda_sentinel.policies.work_type import ROLLUP_COLUMN as WORK_TYPE_ROLLUP_COLUMN
from tests.clocks import FIXED_INSTANT
from tests.clocks import LATER_INSTANT
from tests.clocks import OBSERVATION_GAP
from tests.passes import FIRST_DOMAIN
from tests.passes import SECOND_DOMAIN
from tests.passes import failing_pass_class
from tests.passes import registered_pass
from tests.passes import working_pass_class
from tests.policy_parameters import recorded_policy_parameters

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from django.db import models
    from structlog.typing import EventDict

#: The policy version the cases record. Two of them, because the recompose case
#: is about the *newer* run's stamps replacing the older run's.
#:
#: Both are fixture versions rather than the one the shipped parameter file
#: records, and `_recorded_parameters` below is what makes that legal: since
#: `CPM-CURRENCY-S07` a run at a version nothing records fails every package, and
#: this module needs *two* versions where the reviewed file ships one. Recording
#: them into a substituted file is the honest way round -- a version in the
#: reviewed file is a reviewed decision, and a test fixture is not.
A_POLICY_VERSION: Final[str] = "cpm-fixture-policy-1"
A_NEWER_POLICY_VERSION: Final[str] = "cpm-fixture-policy-2"

#: The inactivity threshold the substituted file records for both versions, in
#: days. Identical for the two, deliberately: nothing in this module is about the
#: threshold, and two different ones would make the recompose case look as though
#: it depended on a rule change it has nothing to do with.
A_FIXTURE_INACTIVITY: Final[int] = 180

#: The two versions the fixture observations state, and the feedstock they are
#: about. Different on purpose: the gate case needs the currency pass to reach a
#: verdict that is not `unknown`, and a discrepancy between two observed surfaces
#: is the cheapest one to arrange. Which versions they are does not matter, only
#: that they differ.
A_RELEASED_VERSION: Final[str] = "2.4.0"
AN_EARLIER_VERSION: Final[str] = "2.3.1"
A_FEEDSTOCK_NAME: Final[str] = "numpy-feedstock"

#: The collector name the fixture collection runs carry.
A_COLLECTOR: Final[str] = "cpm-fixture-collector"

#: The instants the later runs are stopped at. Derived from `LATER_INSTANT`
#: rather than written out, so the runs' ordering cannot drift.
LATEST_INSTANT: Final = LATER_INSTANT + OBSERVATION_GAP
RECOVERY_INSTANT: Final = LATEST_INSTANT + OBSERVATION_GAP

#: How many packages the inventory holds once a second one appears between two
#: runs. Named because the assertion is about the *set being re-read*, not about
#: the number two.
PACKAGES_AFTER_THE_LATECOMER: Final[int] = 2

#: A confidence value `IdentityConfidence` does not declare, written straight into
#: the column because Django enforces `choices` on neither `save()` nor
#: `create()`. It stands for what a version skew or a hand-run data fix leaves
#: behind, and it is the fault the compose-containment case is built on.
AN_UNRECOGNISED_CONFIDENCE: Final[str] = "asserted"

#: The event the capture fixture logs to prove the capture is live before a case
#: asserts over what it caught. The pattern and its reason are
#: `tests/integration/django_apps/test_run_ledger.py`'s.
CAPTURE_CONTROL: Final[str] = "rollup-capture-control"


@pytest.fixture(autouse=True)
def _recorded_parameters(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """Record this module's two fixture policy versions, so a run at either can complete.

    Autouse because it is a precondition of every case here rather than a subject
    of any of them: `FeedstockPresencePass` is adopted at boot and refuses a
    policy version `policies/data/policy-parameters.toml` does not record, so
    without this every run below would fail every package and finalize `failed`.
    What each case is actually about -- the full-row replace, the stamps, the
    gate, the two domains surviving one compose -- would then be untestable for a
    reason that has nothing to do with the rollup.

    The substitution and its teardown are `tests/policy_parameters.py`'s, which
    argues why the file is substituted rather than the reviewed one extended.

    Args:
        monkeypatch: pytest's patcher, which restores the shipped path.
        tmp_path: Where the substituted file is written.

    Yields:
        Nothing; the substitution is the effect.

    """
    versions = dict.fromkeys((A_POLICY_VERSION, A_NEWER_POLICY_VERSION), A_FIXTURE_INACTIVITY)
    with recorded_policy_parameters(monkeypatch, tmp_path, versions):
        yield


@pytest.fixture
def captured_events(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[EventDict]]:
    """Capture what `core/rollup.py` logs, with the two guards the plain helper lacks.

    The reasoning is `tests/unit/test_drain.py`'s and is not restated: the
    module-scope logger is rebound so `capture_logs` binds a fresh proxy inside
    its own processor chain, and a control event proves the capture is live
    before the case runs.

    Args:
        monkeypatch: pytest's patcher, which restores the module's own logger.

    Yields:
        The captured events, in order.

    """
    monkeypatch.setattr(rollup_module, "logger", structlog.get_logger(rollup_module.__name__))
    with structlog.testing.capture_logs() as captured:
        rollup_module.logger.warning(CAPTURE_CONTROL)
        assert [event["event"] for event in captured] == [CAPTURE_CONTROL], (
            "structlog.testing.capture_logs() cannot see core.rollup's logger, so every assertion "
            "over what it logged would be vacuous"
        )
        captured.clear()
        yield captured


def an_ended_collection_run() -> None:
    """Record one ended collection run, so a policy run has a cut-off to choose."""
    CollectionRun.objects.create(
        collector=A_COLLECTOR,
        started_at=FIXED_INSTANT,
        finished_at=FIXED_INSTANT,
        status=RunState.SUCCEEDED,
    )


def a_package(name: str, *, confidence: str = IdentityConfidence.VERIFIED) -> Package:
    """Create one package at a stated identity confidence.

    Args:
        name: Its canonical name, which is unique.
        confidence: How certain its identity is (`CPM-AD-4`).

    Returns:
        The saved `Package`.

    """
    return Package.objects.create(canonical_name=name, resolved_at=FIXED_INSTANT, confidence=confidence)


@pytest.mark.django_db
def test_every_package_gets_exactly_one_row_carrying_the_runs_stamps() -> None:
    """`CPM-AD-11`: one row per package, stamped with the run, the cut-off and the version map.

    The version map is asserted as a *mapping* rather than as a scalar, which is
    the clause `CPM-AD-11` spells out: a scalar would force every domain to be
    re-run whenever any one of them changed version, or would lie about the ones
    that were not.

    It carries `CurrencyPass`'s domain as well as the fixture's, and that is
    asserted rather than filtered out: `policies/apps.py` adopts a real pass at
    `django.setup()`, so every run in this suite executes two domains, and a map
    naming only the fixture would mean the adopted pass had not run.
    """
    an_ended_collection_run()
    packages = [a_package("numpy"), a_package("scipy")]

    with registered_pass(working_pass_class(name=FIRST_DOMAIN)):
        summary = execute_policy_run(policy_version=A_POLICY_VERSION, clock=FixedClock(instant=LATER_INSTANT))

    assert summary.rollup_rows == len(packages)
    assert PackageHealth.objects.count() == len(packages)
    for package in packages:
        row = PackageHealth.objects.get(package=package)
        assert row.policy_run_id == summary.policy_run.pk
        assert row.evidence_cutoff == FIXED_INSTANT
        assert row.computed_at == LATER_INSTANT
        assert row.confidence == IdentityConfidence.VERIFIED
        assert row.policy_versions == {
            CURRENCY_POLICY_NAME: A_POLICY_VERSION,
            FEEDSTOCK_POLICY_NAME: A_POLICY_VERSION,
            VULNERABILITY_POLICY_NAME: A_POLICY_VERSION,
            LICENCE_POLICY_NAME: A_POLICY_VERSION,
            REMEDIATION_POLICY_NAME: A_POLICY_VERSION,
            PY314_READINESS_POLICY_NAME: A_POLICY_VERSION,
            PRIORITY_POLICY_NAME: A_POLICY_VERSION,
            WORK_TYPE_POLICY_NAME: A_POLICY_VERSION,
            FIRST_DOMAIN: A_POLICY_VERSION,
        }


@pytest.mark.django_db
def test_two_passes_in_two_domains_both_survive_the_compose(
    derived_tables: tuple[type[models.Model], type[models.Model]],
) -> None:
    """`EVIDENCE.07-INT-001`, and the property the single-writer rule exists for.

    A writer that reset another domain's table -- by truncating it, by deleting
    the run's rows, or by "cleaning up" before writing -- would satisfy every
    single-domain assertion in this file. Two domains is the smallest arrangement
    that can tell the difference, and the version map is asserted to carry both
    names for the same reason.
    """
    first_model, second_model = derived_tables
    an_ended_collection_run()
    package = a_package("numpy")

    with (
        registered_pass(working_pass_class(name=FIRST_DOMAIN, derived_model=first_model)),
        registered_pass(working_pass_class(name=SECOND_DOMAIN, derived_model=second_model)),
    ):
        summary = execute_policy_run(policy_version=A_POLICY_VERSION, clock=FixedClock(instant=LATER_INSTANT))

    assert first_model.objects.filter(package_id=package.pk).count() == 1
    assert second_model.objects.filter(package_id=package.pk).count() == 1
    assert PackageHealth.objects.get(package=package).policy_versions == {
        CURRENCY_POLICY_NAME: A_POLICY_VERSION,
        FEEDSTOCK_POLICY_NAME: A_POLICY_VERSION,
        VULNERABILITY_POLICY_NAME: A_POLICY_VERSION,
        LICENCE_POLICY_NAME: A_POLICY_VERSION,
        REMEDIATION_POLICY_NAME: A_POLICY_VERSION,
        PY314_READINESS_POLICY_NAME: A_POLICY_VERSION,
        PRIORITY_POLICY_NAME: A_POLICY_VERSION,
        WORK_TYPE_POLICY_NAME: A_POLICY_VERSION,
        FIRST_DOMAIN: A_POLICY_VERSION,
        SECOND_DOMAIN: A_POLICY_VERSION,
    }
    assert summary.rollup_rows == 1


@pytest.mark.django_db
def test_a_second_run_replaces_the_row_rather_than_adding_one() -> None:
    """Recompose, which is the difference between a rollup and an evidence table.

    Still exactly one row per package, and it carries the *newer* run's stamps.
    A writer that inserted would produce two rows and no read surface could say
    which was current; one that merged would leave the row naming the new run
    while holding a value the old one computed.
    """
    an_ended_collection_run()
    package = a_package("numpy")

    first = execute_policy_run(policy_version=A_POLICY_VERSION, clock=FixedClock(instant=LATER_INSTANT))
    second = execute_policy_run(policy_version=A_NEWER_POLICY_VERSION, clock=FixedClock(instant=LATEST_INSTANT))

    assert PackageHealth.objects.count() == 1
    row = PackageHealth.objects.get(package=package)
    assert row.policy_run_id == second.policy_run.pk
    assert row.policy_run_id != first.policy_run.pk
    assert row.computed_at == LATEST_INSTANT


@pytest.mark.django_db
def test_a_package_added_between_runs_gets_a_row_from_the_next_compose() -> None:
    """The package set is read, never cached.

    `CPM-AD-25` creates package rows from the inventory continuously, so "exactly
    one row per package" is only true if the writer asks the question again on
    every run. A cached set would leave the newest packages -- the ones somebody
    is most likely to be looking for -- absent from the rollup indefinitely.
    """
    an_ended_collection_run()
    a_package("numpy")

    execute_policy_run(policy_version=A_POLICY_VERSION, clock=FixedClock(instant=LATER_INSTANT))
    latecomer = a_package("scipy")
    second = execute_policy_run(policy_version=A_POLICY_VERSION, clock=FixedClock(instant=LATEST_INSTANT))

    assert second.rollup_rows == PackageHealth.objects.count() == PACKAGES_AFTER_THE_LATECOMER
    assert PackageHealth.objects.get(package=latecomer).computed_at == LATEST_INSTANT


@pytest.mark.django_db
def test_an_unmapped_package_still_gets_a_row_recording_that_it_is_unmapped() -> None:
    """`CPM-AD-4` expressed as writing a value, never as suppressing a row.

    A gate that skipped the package would leave the rollup with missing rows
    meaning two different things -- not yet computed, and not confident enough to
    compute -- which no read surface can tell apart. So the row exists and says
    what it is.

    **The gated status is asserted, and against a package the pass reached a
    determinate verdict about.** `CPM-CURRENCY-S06` gave the rollup
    `currency_status`, so there is a real column to gate -- but a package with no
    evidence makes the currency pass answer `unknown`, which is the same string
    `GATED_VALUE` is, and two columns agreeing by coincidence would prove nothing.
    So both packages get a source observation and a feedstock observation stating
    *different* versions, which the adopted pass reads as `behind`. The `unmapped`
    package's column then reads `unknown` because the gate replaced that verdict,
    and the `verified` package's reads `behind` because it did not. The difference
    between the two columns is the gate, and it is the only thing that differs
    between the two packages.

    `behind` rather than `current` because only two of the four surfaces are
    observed here: `policies/currency.py` lets a proven discrepancy outrank an
    unobserved surface, while `current` would be reduced to `unknown` by the two
    surfaces nothing has looked at. The whole of that reduction is
    `tests/unit/django_apps/test_currency_policy.py`'s; what this case needs is a
    verdict that is not `unknown`.

    The observations are written directly rather than through a collector: what
    is under test is the writer's gate, and driving a collection here would make
    the case depend on a transport nothing in it is about.
    """
    an_ended_collection_run()
    unmapped = a_package("unresolved", confidence=IdentityConfidence.UNMAPPED)
    verified = a_package("numpy", confidence=IdentityConfidence.VERIFIED)
    for package in (unmapped, verified):
        SourceReleaseSnapshot.objects.create(
            package=package,
            observed_at=FIXED_INSTANT,
            state=OutcomeState.OK,
            latest_version=A_RELEASED_VERSION,
        )
        FeedstockSnapshot.objects.create(
            package=package,
            observed_at=FIXED_INSTANT,
            state=OutcomeState.OK,
            feedstock_name=A_FEEDSTOCK_NAME,
            recipe_version=AN_EARLIER_VERSION,
            # Dated, and dated at the cut-off itself, so the feedstock pass
            # reaches `present_and_maintained` rather than the `unknown` an
            # undatable feedstock earns. That matters for the same reason
            # `behind` does above: `unknown` is the string `GATED_VALUE` carries,
            # so a gated column and an ungated one would agree by coincidence and
            # the assertion would prove nothing.
            last_recipe_activity_at=FIXED_INSTANT,
        )

    execute_policy_run(policy_version=A_POLICY_VERSION, clock=FixedClock(instant=LATER_INSTANT))

    assert PackageHealth.objects.get(package=unmapped).confidence == IdentityConfidence.UNMAPPED
    assert PackageHealth.objects.get(package=verified).confidence == IdentityConfidence.VERIFIED
    assert PackageHealth.objects.get(package=unmapped).currency_status == GATED_VALUE
    assert PackageHealth.objects.get(package=verified).currency_status == BEHIND
    assert GATED_VALUE != BEHIND, "the gate must change the value, or this case asserts nothing"
    # The second domain column, gated on the same row by the same writer.
    # `CPM-CURRENCY-S07` added it, and asserting it here is what says the gate is
    # applied per *contributable column* rather than to whichever one somebody
    # remembered: both packages' feedstocks were pushed to at the cut-off, so the
    # feedstock pass reaches `present_and_maintained` for each, and only the
    # unmapped one's is replaced.
    assert PackageHealth.objects.get(package=unmapped).feedstock_presence_status == GATED_VALUE
    assert PackageHealth.objects.get(package=verified).feedstock_presence_status == PRESENT_AND_MAINTAINED
    assert GATED_VALUE != PRESENT_AND_MAINTAINED, (
        "the gate must change this value too, or half this case asserts nothing"
    )
    # The third domain column, added by `CPM-PRIORITY-S01`, and it is asserted with
    # a caveat the two above do not need. The shipped rule set is **empty** -- PRD
    # Open Question 8 -- so the priority pass contributes `unknown` for every
    # package and the gate has nothing to replace. That is not the gate failing to
    # apply; it is the column's shipped value already being the one the gate
    # writes, and a case claiming this proves the gate would be asserting a
    # difference that does not exist.
    # `tests/integration/django_apps/test_priority_policy.py` records a real rule
    # set and asserts the gate replaces an actual bucket there, which is where that
    # claim belongs.
    assert PackageHealth.objects.get(package=unmapped).priority_status == GATED_VALUE
    # The fourth domain column, added by `CPM-PRIORITY-S02`, and gated for a reason
    # that story turns on: the work-type pass deliberately reads no identity
    # confidence -- `CPM-AD-4` gives that exactly one consumer and this writer is it
    # -- so whatever it derived for an unmapped package is replaced here and nowhere
    # else. Both packages here are behind on a surface, which the work-type pass
    # reads as `file_tracking_issue`, so the gate does have something to replace.
    assert PackageHealth.objects.get(package=unmapped).work_type_status == GATED_VALUE
    assert PackageHealth.objects.get(package=verified).work_type_status == FILE_TRACKING_ISSUE
    assert GATED_VALUE != FILE_TRACKING_ISSUE, (
        "the gate must change this value too, or a third of this case asserts nothing"
    )
    assert contributable_columns() == frozenset(
        {ROLLUP_COLUMN, FEEDSTOCK_ROLLUP_COLUMN, PRIORITY_ROLLUP_COLUMN, WORK_TYPE_ROLLUP_COLUMN},
    ), "the rollup declares a contributable column this case does not assert the gated value on"


@pytest.mark.django_db
def test_an_inventory_derived_package_records_its_label_undegraded() -> None:
    """The label travels with the row, which is what makes not degrading the value safe.

    `CPM-AD-4` says an inventory-derived identity does not degrade a verdict. That
    is only defensible because the provenance is on the row beside it: a reader
    can see the verdict *and* how certain the identity behind it was, without the
    two being folded into one column.
    """
    an_ended_collection_run()
    derived = a_package("numpy", confidence=IdentityConfidence.INVENTORY_DERIVED)

    execute_policy_run(policy_version=A_POLICY_VERSION, clock=FixedClock(instant=LATER_INSTANT))

    assert PackageHealth.objects.get(package=derived).confidence == IdentityConfidence.INVENTORY_DERIVED


@pytest.mark.django_db
def test_the_rollup_row_survives_and_is_replaced_by_the_run_that_follows_a_partial_one() -> None:
    """`CPM-NFR-3`: degrade to stale, never to a clean result -- and never permanently.

    Three runs, and the third is the half that makes the claim complete. A package
    whose pass raised keeps the row it had (the older answer, stamped with the
    older run) rather than being overwritten with a health computed from a pass
    that never finished. Then the *next* successful run replaces it, which is what
    stops "stale" becoming "permanent" -- a writer that skipped a package once and
    kept skipping it would satisfy the first two runs perfectly.
    """
    an_ended_collection_run()
    package = a_package("numpy")

    first = execute_policy_run(policy_version=A_POLICY_VERSION, clock=FixedClock(instant=LATER_INSTANT))
    with registered_pass(failing_pass_class(name=SECOND_DOMAIN, failing=[package.pk])):
        execute_policy_run(policy_version=A_NEWER_POLICY_VERSION, clock=FixedClock(instant=LATEST_INSTANT))

    stale = PackageHealth.objects.get(package=package)
    assert stale.policy_run_id == first.policy_run.pk
    assert stale.computed_at == LATER_INSTANT

    recovered = execute_policy_run(policy_version=A_NEWER_POLICY_VERSION, clock=FixedClock(instant=RECOVERY_INSTANT))

    assert PackageHealth.objects.count() == 1
    fresh = PackageHealth.objects.get(package=package)
    assert fresh.policy_run_id == recovered.policy_run.pk
    assert fresh.computed_at == RECOVERY_INSTANT


@pytest.mark.django_db
def test_a_package_whose_row_will_not_compose_does_not_take_the_others_with_it(
    captured_events: list[EventDict],
) -> None:
    """`CPM-AD-23` binds the compose phase too, and it did not.

    The pass phase contained per package from the start; the writer did not, so
    one row that would not compose aborted every package after it and finalized
    the run `failed` -- the opposite of "degrades to stale evidence, never to a
    clean result" (`CPM-NFR-3`), and over a fault in one row.

    **The fault is a real data condition rather than an injected exception.**
    `Package.confidence` is a `CharField(choices=...)` and Django enforces choices
    on neither `save()` nor `create()`, so a value from outside
    `IdentityConfidence` is a row the database accepts and the writer cannot
    honestly gate -- exactly what a version skew or a hand-run data fix leaves
    behind. The writer refuses that package and writes the rest.

    The log line is asserted for the reason `EVALUATION_FAILED_EVENT`'s is: the
    ledger row carries a count, and a count with no names sends an operator
    through the whole inventory by hand.
    """
    an_ended_collection_run()
    unwritable = a_package("numpy", confidence=AN_UNRECOGNISED_CONFIDENCE)
    healthy = a_package("scipy")

    summary = execute_policy_run(policy_version=A_POLICY_VERSION, clock=FixedClock(instant=LATER_INSTANT))

    assert summary.rollup_rows == 1
    assert summary.failed_packages == (unwritable.pk,)
    assert PackageHealth.objects.filter(package=unwritable).count() == 0
    assert PackageHealth.objects.filter(package=healthy).count() == 1
    assert PolicyRun.objects.get(pk=summary.policy_run.pk).status == RunState.PARTIAL

    failures = [event for event in captured_events if event["event"] == ROLLUP_WRITE_FAILED_EVENT]
    assert [event["package_pk"] for event in failures] == [unwritable.pk]
    assert failures[0]["exc_info"] is True, "the refusal's own traceback is the half a bare error() would lose"

    written = [event for event in captured_events if event["event"] == ROLLUP_WRITTEN_EVENT]
    assert [(event["rows"], event["failed"]) for event in written] == [(1, 1)]


@pytest.mark.django_db
def test_deleting_a_run_or_a_package_a_rollup_row_names_is_refused_by_the_database() -> None:
    """`PROTECT` is only worth what the schema built, and nothing asserted the schema.

    Both relations are argued at length in `PackageHealth`'s field comments -- a
    rollup row is the only statement this product makes about a package's current
    health, so a cascade would make an operational tidy-up of old policy runs
    silently empty the table every read surface reads. The argument is a
    docstring; this is the database refusing.
    """
    an_ended_collection_run()
    package = a_package("numpy")
    summary = execute_policy_run(policy_version=A_POLICY_VERSION, clock=FixedClock(instant=LATER_INSTANT))

    with pytest.raises(ProtectedError):
        PolicyRun.objects.get(pk=summary.policy_run.pk).delete()

    with pytest.raises(ProtectedError):
        package.delete()

    assert PackageHealth.objects.count() == 1


@pytest.mark.django_db
def test_a_second_rollup_row_for_one_package_is_refused_by_the_database() -> None:
    """ "Exactly one row per package" is a unique index, not a promise the writer keeps.

    `CPM-AD-11`'s whole shape depends on it: `package.health` is a single object
    rather than a set, the writer's `update_or_create` matches on it, and every
    read surface projects from it without asking which of two rows is current.
    Asserted by attempting the second write, because a `OneToOneField` that had
    been softened to a plain `ForeignKey` declares the same column and only the
    database says the difference.
    """
    an_ended_collection_run()
    package = a_package("numpy")
    summary = execute_policy_run(policy_version=A_POLICY_VERSION, clock=FixedClock(instant=LATER_INSTANT))

    with pytest.raises(IntegrityError), transaction.atomic():
        PackageHealth.objects.create(
            package=package,
            policy_run=summary.policy_run,
            computed_at=LATEST_INSTANT,
            evidence_cutoff=FIXED_INSTANT,
            confidence=IdentityConfidence.VERIFIED,
        )


@pytest.mark.django_db
def test_the_rollup_row_is_reachable_from_the_package_as_one_object() -> None:
    """What the `OneToOneField` buys, asserted so a softening back is a failing test.

    `ForeignKey(unique=True)` builds the same column and the same index; what it
    does *not* build is this accessor. Every read surface projecting the rollup
    would otherwise write `package.health.first()` and would have to decide what a
    second row means -- a question `CPM-AD-11` has already answered.
    """
    an_ended_collection_run()
    package = a_package("numpy")

    execute_policy_run(policy_version=A_POLICY_VERSION, clock=FixedClock(instant=LATER_INSTANT))

    package.refresh_from_db()
    assert package.health.computed_at == LATER_INSTANT
    assert package.health.pk == PackageHealth.objects.get().pk


@pytest.mark.django_db
def test_the_row_carries_every_column_the_rollup_declares() -> None:
    """A full-row replace writes the whole row, which is only checkable against the whole row.

    The stamps are enumerated by `core/rollup.py`, the contributable columns are
    derived from the model, and the after-run columns (`CPM-OPERATE-S11`) are the
    third enumeration, so the three together are every non-key field. A field
    added to the rollup and forgotten by the writer would leave a column the
    writer never sets -- which is exactly the merge this table must not do. The
    after-run columns are the one pair the compose writes as `NULL` on purpose:
    a listed package carries nothing there, and the step that fills them for an
    absent one is `collectors/absence.py`'s, asserted in `test_absence.py`.
    """
    an_ended_collection_run()
    a_package("numpy")

    execute_policy_run(policy_version=A_POLICY_VERSION, clock=FixedClock(instant=LATER_INSTANT))

    declared = {
        field.name
        for field in PackageHealth._meta.concrete_fields  # noqa: SLF001 - `_meta` is Django's own public-by-convention API
        if not field.primary_key
    }
    assert declared == STAMP_COLUMNS | contributable_columns() | AFTER_RUN_COLUMNS
    row = PackageHealth.objects.get()
    for column in declared - AFTER_RUN_COLUMNS:
        assert getattr(row, f"{column}_id" if column in {"package", "policy_run"} else column) is not None
    for column in AFTER_RUN_COLUMNS:
        assert getattr(row, column) is None
