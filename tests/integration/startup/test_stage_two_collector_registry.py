"""Conditions 10 and 11: what a registered collector must declare, and what must agree with it.

**Two conditions, one registry, and they are in one module because they sweep the
same thing.** Condition 10 is `CPM-AD-28` -- a collector that declares no
freshness target. Condition 11 is `CPM-AD-20`'s reconciliation, added by
`CPM-CURRENCY-S05`: a collector's declared cadence against the
`CELERY_BEAT_SCHEDULE` entry that fires it, in both directions, plus the rule that
a freshness target must be strictly greater than the cadence it was derived from.
They are separate conditions because they are separate architecture decisions
about separate artefacts, which is the argument
`tests/unit/startup/forbidden_states.py` makes for condition 10 not being folded
into one of the inherited nine.

**Condition 11's cases add to the shipped schedule rather than replacing it.**
The six swept collectors are registered in this process, so a case that swapped
`CELERY_BEAT_SCHEDULE` for a fixture-only one would refuse for six reasons it was
not written about. What each case configures is one *additional* disagreement, and
the last case asserts the shipped pair reconciles with no fixture in sight.

**Condition 10, and `EVIDENCE.06-AUDIT-001`.** An unset per-collector freshness target
behaves as "fresh forever", so evidence collected six months ago reads as current
on every surface -- the `CPM-SM-C1` failure this product exists to prevent, and
one that nothing at run time would report: the collector collects, the rows are
written, and every view shows a clean result derived from an observation nobody
made this year. So the process refuses to start instead.

**Why this lives in the integration tier rather than beside the URLconf cases.**
The condition needs no database and opens no socket, so the *shape* of its
refusal is asserted in `tests/unit/startup/test_no_softening.py` like the other
thirteen. What is asserted here is the behaviour through the real dispatch and the
real, process-global registry: `run_stage_two()` evaluating its whole roster in
order, over the module-level mapping every other process in this repository
shares -- which is the state a deployed boot is actually in, and which a
monkeypatched mapping deliberately is not. That is the same division
`tests/integration/startup/test_stage_two_database_conditions.py` makes for
conditions 5 and 7.

**Nothing is left behind, and that clause is load-bearing here.** The registry is
global and lives for the whole session, so a registration that survived a case
would be a collector every later boot sweep in the run refuses over -- and the
failure would land in whichever case happened to run next rather than in this
file. `tests/collectors.py`'s `registered_collector` withdraws in a `finally`,
which is the only ordering that survives a case whose whole body raises.

**The registry this component really boots with is the other half.**
`CPM-IDENTITY-S06` adopted the first collector, `CPM-CURRENCY-S01` the second,
`CPM-CURRENCY-S02` the third, `CPM-CURRENCY-S03` the fourth and
`CPM-CURRENCY-S04` the fifth, `CPM-SECURITY-S01` the sixth, `CPM-SECURITY-S02`
the seventh and `CPM-SECURITY-S03` the eighth,
so a deployed boot now sweeps a real roster rather than nothing, and the case that
asserts it is what proves the condition passes over *real* declarations and not
only over fixtures. The empty case it replaces made the opposite claim and was
worth making while it was true; what it cannot do any more is be true. A condition
that refused the roster below would stop every process in this repository, and it
would do so while every refusal case above still passed -- which is why the roster
is asserted rather than assumed.

`tests/integration/conftest.py` marks everything under `tests/integration/` as an
integration test; the marker is not re-applied by hand.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

import pytest
from django.apps import apps
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings

from conda_sentinel.collectors.conda_package import COLLECTOR_NAME as CONDA_PACKAGE_NAME
from conda_sentinel.collectors.feedstock import COLLECTOR_NAME as FEEDSTOCK_NAME
from conda_sentinel.collectors.kev import COLLECTOR_NAME as KEV_NAME
from conda_sentinel.collectors.license import COLLECTOR_NAME as LICENSE_NAME
from conda_sentinel.collectors.py314_verification import COLLECTOR_NAME as PY314_VERIFICATION_NAME
from conda_sentinel.collectors.pypi_release import COLLECTOR_NAME as PYPI_RELEASE_NAME
from conda_sentinel.collectors.python_readiness import COLLECTOR_NAME as PYTHON_READINESS_NAME
from conda_sentinel.collectors.resolve_identity import COLLECTOR_NAME as RESOLVE_IDENTITY_NAME
from conda_sentinel.collectors.source_release import COLLECTOR_NAME as SOURCE_RELEASE_NAME
from conda_sentinel.collectors.sweep import COLLECTOR_KWARG
from conda_sentinel.collectors.sweep import SWEEP_TASK_NAME
from conda_sentinel.collectors.tasks import COLLECTOR_NAME as INVENTORY_COLLECTOR_NAME
from conda_sentinel.collectors.vulnerability import COLLECTOR_NAME as VULNERABILITY_NAME
from conda_sentinel.core import registry
from conda_sentinel.core.registry import registrations
from config.locality import PROCESS_ENV_VAR
from config.locality import RUNTIME_ENV_VAR
from config.startup import run_stage_two
from tests.collectors import FIXTURE_CADENCE
from tests.collectors import FIXTURE_FRESHNESS_TARGET
from tests.collectors import collector_class
from tests.collectors import fixture_evidence_model
from tests.collectors import registered_collector
from tests.collectors import selectable_collector_class
from tests.conftest import deployed_url_patterns
from tests.conftest import temporary_root_urlconf

if TYPE_CHECKING:
    from conda_sentinel.core.collection import Collector


@pytest.fixture(autouse=True)
def _deployed_and_not_serving(monkeypatch: pytest.MonkeyPatch) -> None:
    """Put every case in a deployed component that is not a serving process.

    Deployed by *deleting* `COMPONENT_RUNTIME` rather than by setting it, because
    locality fails closed (AD-13) and absent therefore means deployed.

    `COMPONENT_PROCESS` goes for a different reason, and it is what keeps these
    cases from needing a database: conditions 5 and 7 gate on
    `config.locality.is_serving_process()`, which reads that variable, so a shell
    or a CI runner holding `COMPONENT_PROCESS=web` would turn every case here
    into one that opens a connection and iterates every configured alias.
    """
    monkeypatch.delenv(RUNTIME_ENV_VAR, raising=False)
    monkeypatch.delenv(PROCESS_ENV_VAR, raising=False)


def _collector_declaring(target: timedelta | None) -> type[Collector]:
    """Build a fixture collector whose freshness target is the one under test.

    Args:
        target: The declared target, or `None` for the collector that declares
            none.

    Returns:
        A concrete `Collector` subclass, unconstructed -- which is what the boot
        sweep reads, and deliberately: constructing one would build a transport
        and its connection pool inside `django.setup()`.

    """
    return collector_class(
        declared_model=fixture_evidence_model(),
        declared_freshness_target=target,
    )


@pytest.mark.forbidden_state("collector-without-freshness-target")
def test_a_registered_collector_with_no_freshness_target_refuses_the_boot() -> None:
    """`EVIDENCE.06-AUDIT-001`: startup raises rather than defaulting to fresh-forever.

    The message has to name the class, because "a collector has no freshness
    target" tells an operator nothing they can act on while naming the class
    tells them which file to open -- the same standard
    `_refuse_unapplied_migrations` is held to when it names the alias and the
    pending migrations.
    """
    undeclared = _collector_declaring(None)

    with (
        registered_collector(undeclared),
        temporary_root_urlconf(*deployed_url_patterns()),
        pytest.raises(ImproperlyConfigured) as refused,
    ):
        run_stage_two()

    assert undeclared.__name__ in str(refused.value)
    assert "freshness_target=None" in str(refused.value)


@pytest.mark.parametrize(
    "target",
    [timedelta(0), -FIXTURE_FRESHNESS_TARGET],
    ids=["zero", "negative"],
)
def test_a_registered_collector_with_an_unusable_target_refuses_the_boot(target: timedelta) -> None:
    """Absence is not the only way to declare nothing usable.

    A zero target says "evidence is stale the instant it is written", which
    nobody means and which would make every surface permanently amber; a negative
    one says something that has no reading at all. Both would pass a check that
    only asked whether the attribute was set, which is the check somebody writes
    after seeing the case above.

    `NO_WINDOW` is the deliberate contrast: a zero *observation window* is
    accepted, because "observe on every run" is a thing an operator means. The
    two sentinels are the same interval and the opposite decision.
    """
    unusable = _collector_declaring(target)

    with (
        registered_collector(unusable),
        temporary_root_urlconf(*deployed_url_patterns()),
        pytest.raises(ImproperlyConfigured, match="freshness_target"),
    ):
        run_stage_two()


def test_every_offender_is_named_in_one_refusal() -> None:
    """One boot tells the operator about all of them, not the first one alphabetically.

    Raising on the first offender costs a restart per collector: fix the target
    the message named, redeploy, meet the next refusal. With the eight collectors
    `CPM-EP-CURRENCY` is bringing, that is eight boots to learn what one could
    have said -- and each one looks like a fresh problem rather than the
    remainder of a known one.

    Both names are asserted, and the count with them: a message that happened to
    interpolate a list would satisfy a substring check for either name on its
    own.
    """
    first = collector_class(
        declared_model=fixture_evidence_model(),
        declared_name="cpm-fixture-first-undeclared",
        declared_freshness_target=None,
    )
    second = collector_class(
        declared_model=fixture_evidence_model(),
        declared_name="cpm-fixture-second-undeclared",
        declared_freshness_target=timedelta(0),
    )

    with (
        registered_collector(first),
        registered_collector(second),
        temporary_root_urlconf(*deployed_url_patterns()),
        pytest.raises(ImproperlyConfigured) as refused,
    ):
        run_stage_two()

    reported = str(refused.value)
    assert "cpm-fixture-first-undeclared" in reported
    assert "cpm-fixture-second-undeclared" in reported
    assert reported.startswith("2 registered collector(s)")


def test_a_registered_collector_that_declares_a_target_starts() -> None:
    """The negative control, without which the refusals above prove nothing.

    A condition that refused every registered collector would satisfy all three
    cases above and would stop the first component that adopted one.
    """
    declared = _collector_declaring(FIXTURE_FRESHNESS_TARGET)

    with registered_collector(declared), temporary_root_urlconf(*deployed_url_patterns()):
        run_stage_two()


def test_the_registry_this_repository_ships_passes_condition_ten() -> None:
    """The roster this repository ships, swept by condition 10 with no fixture in sight.

    **The name of this case is deliberately not "the registry this component
    actually boots with".** It was, and that was a claim about a deployment which
    this case does not exercise: stage two runs before `CollectorsConfig.ready()`
    registers anything, so a *real* boot sweeps an empty registry here. What is
    exercised is the roster after `django.setup()` has finished, which is the
    state the suite is in -- worth asserting, and not the same thing.
    `tests/integration/startup/test_collector_boot_refusal.py` is where the
    deployment claim is made, in a child process.


    `CPM-IDENTITY-S06` adopted the first real collector, `CPM-CURRENCY-S01` the
    second, `CPM-CURRENCY-S02` the third, `CPM-CURRENCY-S03` the fourth,
    `CPM-CURRENCY-S04` the fifth, `CPM-SECURITY-S01` the sixth,
    `CPM-SECURITY-S02` the seventh, `CPM-SECURITY-S03` the eighth,
    `CPM-PY314-S01` the ninth, `CPM-PY314-S02` the tenth and `CPM-IDENTITY-S08`
    the eleventh, so the registry a deployed boot sweeps is no longer empty:
    `CollectorsConfig.ready()` registers inventory ingestion, upstream release
    collection, PyPI release collection, feedstock collection,
    published-conda-package collection, vulnerability collection, KEV
    cross-referencing, licence collection, static Python-readiness assessment,
    Python 3.14 verification and identity resolution during `django.setup()`,
    and the sweep meets all eleven on every boot in this tree.
    That is asserted rather than assumed, and both halves matter -- the roster is
    what it is meant to be, and stage two passes over it without a fixture in
    sight.

    **The tenth is registered and is swept by nothing**, which is the shape
    condition 10 has to pass over rather than refuse. `CPM-PY314-S02`'s
    verification collector is triggered rather than scheduled, so it declares no
    cadence and answers `selectable_packages` with `None` -- and it still declares
    a freshness target, because `CPM-AD-28` demands one of every registered
    collector whether or not anything schedules it. The loop below asserts exactly
    that, over the whole roster, so a triggered collector that quietly dropped its
    target would fail here rather than at the first boot that swept it.

    The roster is asserted as a whole rather than as "contains", which is the
    difference between a test that notices an adoption disappearing and one that
    does not: a `ready()` that stopped registering a collector would leave that
    collector unscheduled, unswept by `CPM-AD-28`'s refusal, and invisible in
    every report, with nothing else in the suite the poorer for it.

    Asserted by *name* rather than by identity, and the reason is what the
    registry is *for* rather than an import rule -- this module imports the
    collector's module either way, because a constant lives in it. The name is
    the registry's key, it is what a ledger row carries (`CPM-FR-39`) and what a
    rate-limit allowance is counted against, so "which collectors is this
    component running" is a question about names. A roster compared by identity
    would still pass if two classes had come to share one.
    """
    adopted = sorted(
        [
            INVENTORY_COLLECTOR_NAME,
            SOURCE_RELEASE_NAME,
            PYPI_RELEASE_NAME,
            FEEDSTOCK_NAME,
            CONDA_PACKAGE_NAME,
            VULNERABILITY_NAME,
            KEV_NAME,
            LICENSE_NAME,
            PYTHON_READINESS_NAME,
            PY314_VERIFICATION_NAME,
            RESOLVE_IDENTITY_NAME,
        ],
    )

    assert sorted(registrations()) == adopted
    for name in adopted:
        assert registrations()[name].freshness_target is not None

    with temporary_root_urlconf(*deployed_url_patterns()):
        run_stage_two()


# ---------------------------------------------------------------------------
# Condition 11: the cadence a collector declares and the schedule that fires it.
# ---------------------------------------------------------------------------


def _sweepable(
    *,
    name: str,
    cadence: object = FIXTURE_CADENCE,
    freshness_target: timedelta | None = FIXTURE_FRESHNESS_TARGET,
    selection: object = (),
) -> type[Collector]:
    """Build a fixture collector that declares a cadence, so condition 11 has something to sweep.

    Args:
        name: The declared name, which is the key a schedule entry has to match.
        cadence: The declared cadence, or `None` for a collector nothing sweeps
            per package -- which condition 11 must pass over rather than refuse.
        freshness_target: The declared target, so a case can drive the refusal
            that compares the two.
        selection: What `selectable_packages` answers over, or `None` for a
            collector that is not swept per package.

    Returns:
        A concrete `Collector` subclass, unconstructed -- which is what the boot
        sweep reads.

    """
    return selectable_collector_class(
        declared_model=fixture_evidence_model(),
        declared_name=name,
        declared_cadence=cadence,  # type: ignore[arg-type]
        declared_freshness_target=freshness_target,
        selection=selection,  # type: ignore[arg-type]
    )


def _schedule_with(*entries: tuple[str, timedelta | None]) -> dict[str, dict[str, object]]:
    """Return the shipped `CELERY_BEAT_SCHEDULE` plus one dispatch entry per pair.

    **Built on top of the shipped schedule rather than replacing it**, and that is
    forced rather than tidy: all eleven real collectors are registered in this
    process and nine of them declare a cadence, so a schedule that dropped their
    entries would make every case here refuse for nine reasons it was not written
    about. What each case configures is
    one *additional* disagreement.

    Composed from `collectors/sweep.py`'s own task name and keyword rather than
    from literals, for the reason the settings module's own literals are
    reconciled against them: a case built on a third spelling would go green while
    the schedule fired into nothing.

    Args:
        *entries: One `(collector name, interval)` pair per entry to add.

    Returns:
        A schedule carrying the shipped entries and the added ones.

    """
    schedule = dict(settings.CELERY_BEAT_SCHEDULE)
    for name, cadence in entries:
        schedule[f"cpm-sweep-{name}"] = {
            "task": SWEEP_TASK_NAME,
            "schedule": cadence,
            "kwargs": {COLLECTOR_KWARG: name},
        }
    return schedule


@pytest.mark.forbidden_state("collector-cadence-not-reconciled")
def test_a_collector_whose_cadence_does_not_match_its_schedule_entry_refuses_the_boot() -> None:
    """`CPM-AD-20`: the two statements of one number are reconciled, and the message names both.

    The failure this prevents is silent and total: a weekly schedule against a
    daily-derived two-day target makes the whole inventory read stale five days
    out of seven, with every gate green and every collection succeeding. So both
    numbers are in the message -- an operator told only that "the cadence does not
    match" has to go and find out which two things disagreed.
    """
    declared = _sweepable(name="cpm-fixture-mismatched-cadence")
    slower = FIXTURE_CADENCE * 7

    with (
        registered_collector(declared),
        override_settings(CELERY_BEAT_SCHEDULE=_schedule_with((declared.name, slower))),
        temporary_root_urlconf(*deployed_url_patterns()),
        pytest.raises(ImproperlyConfigured) as refused,
    ):
        run_stage_two()

    reported = str(refused.value)
    assert repr(FIXTURE_CADENCE) in reported
    assert repr(slower) in reported


@pytest.mark.parametrize(
    "target",
    [FIXTURE_CADENCE, FIXTURE_CADENCE / 2],
    ids=["equal", "shorter"],
)
def test_a_freshness_target_at_or_below_the_cadence_refuses_the_boot(target: timedelta) -> None:
    """Evidence would read stale exactly when its next run is due, with nothing having failed.

    `core/freshness.py` reports stale when `observed_at < now - target`, so a
    target *equal* to the cadence makes every package amber at the moment the next
    collection is scheduled. Both the equal and the shorter case are driven,
    because a check written with `<` rather than `<=` passes the first and is
    wrong.
    """
    declared = _sweepable(name="cpm-fixture-target-under-cadence", freshness_target=target)

    with (
        registered_collector(declared),
        override_settings(CELERY_BEAT_SCHEDULE=_schedule_with((declared.name, FIXTURE_CADENCE))),
        temporary_root_urlconf(*deployed_url_patterns()),
        pytest.raises(ImproperlyConfigured, match="strictly greater"),
    ):
        run_stage_two()


@pytest.mark.parametrize(
    "cadence",
    [timedelta(0), "daily"],
    ids=["zero", "mistyped"],
)
def test_a_declared_cadence_that_is_not_a_positive_interval_refuses_the_boot(cadence: object) -> None:
    """The declaration is refused before it is compared against anything.

    A zero cadence would say "fire continuously", which is a worker that never
    stops enqueueing, and a string is a cadence nobody wrote. Both are reported as
    the declaration's own fault rather than as a schedule disagreement -- the
    schedule below matches the collector's *name* and could not match a number
    that is not one, so a condition that compared first would report the wrong
    thing.
    """
    declared = _sweepable(name="cpm-fixture-unusable-cadence", cadence=cadence)  # type: ignore[arg-type]

    with (
        registered_collector(declared),
        override_settings(CELERY_BEAT_SCHEDULE=_schedule_with((declared.name, FIXTURE_CADENCE))),
        temporary_root_urlconf(*deployed_url_patterns()),
        pytest.raises(ImproperlyConfigured, match="cadence"),
    ):
        run_stage_two()


def test_a_schedule_entry_that_fires_another_task_is_not_this_conditions_business() -> None:
    """The schedule may hold entries this reconciliation says nothing about.

    `CELERY_BEAT_SCHEDULE` is one dictionary and this product will schedule more
    than sweeps -- the session-pruning admin task among them. A condition that
    read every entry as a dispatch would demand a registered collector named after
    whatever else an operator scheduled, which would refuse a boot that is
    entirely correct.
    """
    unrelated = {
        "cpm-something-else": {
            "task": "cpm.policy.currency",
            "schedule": FIXTURE_CADENCE,
            "kwargs": {COLLECTOR_KWARG: "not-a-collector"},
        },
    }

    with (
        override_settings(CELERY_BEAT_SCHEDULE={**_schedule_with(), **unrelated}),
        temporary_root_urlconf(*deployed_url_patterns()),
    ):
        run_stage_two()


def test_a_component_that_has_adopted_no_collector_is_not_refused_over_the_schedule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The empty-registry guard, and it is what keeps a real boot from refusing.

    `tests/unit/startup/test_installed_apps_ordering.py` requires every adopted
    application to be installed *after* the stage-two owner, so in a deployed
    process this condition runs before `CollectorsConfig.ready()` has registered
    anything -- and the backward direction would then find six schedule entries
    naming six collectors the registry does not yet hold. An empty registry is a
    component that has adopted nothing, which has forgotten nothing.

    The schedule is asserted to be non-empty first, because without that this case
    would pass just as happily against a component with nothing scheduled either --
    which is the one arrangement in which the guard is doing no work.
    """
    monkeypatch.setattr(registry, "_REGISTERED", {})

    assert settings.CELERY_BEAT_SCHEDULE != {}

    with temporary_root_urlconf(*deployed_url_patterns()):
        run_stage_two()


def test_a_collector_with_a_cadence_and_no_schedule_entry_refuses_the_boot() -> None:
    """A collector nothing sweeps, whose freshness target is derived from a number nothing fires.

    This is the state every one of the six swept collectors was in before
    `CPM-CURRENCY-S05`: a cadence constant the target was computed from, and no
    schedule. It was not a refusal then because nothing declared the cadence; it
    is one now, and the reconciliation runs forward as well as backward.
    """
    declared = _sweepable(name="cpm-fixture-unscheduled")

    with (
        registered_collector(declared),
        temporary_root_urlconf(*deployed_url_patterns()),
        pytest.raises(ImproperlyConfigured, match="0 entry"),
    ):
        run_stage_two()


def test_a_schedule_entry_naming_a_collector_nothing_registered_refuses_the_boot() -> None:
    """The backward direction, without which the reconciliation runs one way only.

    A renamed collector leaves a beat entry firing into nothing, on every tick,
    and the surface it was meant to observe goes quietly unobserved -- which is
    exactly the class of failure a reconciliation exists to catch, arrived at from
    the other side.
    """
    declared = _sweepable(name="cpm-fixture-renamed")
    schedule = _schedule_with(
        (declared.name, FIXTURE_CADENCE),
        ("cpm-fixture-gone", FIXTURE_CADENCE),
    )

    with (
        registered_collector(declared),
        override_settings(CELERY_BEAT_SCHEDULE=schedule),
        temporary_root_urlconf(*deployed_url_patterns()),
        pytest.raises(ImproperlyConfigured, match="cpm-fixture-gone"),
    ):
        run_stage_two()


def test_a_collector_that_declares_neither_a_cadence_nor_a_selection_is_not_asked_for_an_entry() -> None:
    """The negative control the run-scoped collector depends on.

    Inventory ingestion reads one document naming many packages and is not swept
    per package, so it declares **neither** -- and a condition that demanded an
    entry for every registered collector would stop every component in this
    repository. The pair is what makes it a control rather than a gap: a
    collector declaring only one of the two is refused, which the two cases below
    drive.
    """
    declared = _sweepable(name="cpm-fixture-unscheduled-by-design", cadence=None, selection=None)

    with registered_collector(declared), temporary_root_urlconf(*deployed_url_patterns()):
        run_stage_two()


@pytest.mark.parametrize(
    ("cadence", "selection", "expected"),
    [
        (None, (), "nothing sweeps it"),
        (FIXTURE_CADENCE, None, "no selectable_packages"),
    ],
    ids=["selection-without-cadence", "cadence-without-selection"],
)
def test_a_collector_declaring_one_of_the_pair_without_the_other_refuses_the_boot(
    cadence: timedelta | None,
    selection: tuple[int, ...] | None,
    expected: str,
) -> None:
    """The two declarations are one decision, and half of it is worse than neither.

    A cadence without a selection is a beat entry firing at a dispatch that
    refuses on every tick; a selection without a cadence is a collector nothing
    ever sweeps while every surface reads its evidence as ageing normally. Both
    are quiet, and neither is caught by any other check in this repository.
    """
    declared = _sweepable(name="cpm-fixture-half-declared", cadence=cadence, selection=selection)

    with (
        registered_collector(declared),
        override_settings(CELERY_BEAT_SCHEDULE=_schedule_with((declared.name, FIXTURE_CADENCE))),
        temporary_root_urlconf(*deployed_url_patterns()),
        pytest.raises(ImproperlyConfigured, match=expected),
    ):
        run_stage_two()


def test_the_shipped_schedule_and_the_shipped_collectors_reconcile() -> None:
    """The six swept collectors, the six entries, and the two reconciling.

    **Named for what it asserts rather than for a deployment.** A real boot meets
    this reconciliation in `CollectorsConfig.ready()`, not here; what this case
    proves is that the shipped pair agrees, which is the half a refusal case
    cannot show. A condition that refused the shipped pair would stop every
    process in this repository while every refusal case above still passed.
    `tests/integration/startup/test_collector_boot_refusal.py` boots a child
    against a *broken* pair and asserts the process does not start.
    """
    sweepable = {name: registered.cadence for name, registered in registrations().items() if registered.cadence}
    scheduled = {
        entry["kwargs"][COLLECTOR_KWARG]: entry["schedule"]
        for entry in settings.CELERY_BEAT_SCHEDULE.values()
        if entry["task"] == SWEEP_TASK_NAME
    }

    assert sweepable == scheduled
    assert INVENTORY_COLLECTOR_NAME not in sweepable

    with temporary_root_urlconf(*deployed_url_patterns()):
        run_stage_two()


# ---------------------------------------------------------------------------
# The hook a deployed process actually meets both refusals in.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("fault", "expected"),
    [
        ("freshness", "freshness_target=None"),
        ("cadence", "cadence disagreement"),
    ],
)
def test_the_collectors_app_ready_hook_refuses_a_misdeclared_collector(fault: str, expected: str) -> None:
    """The enforcement point, called the way `django.setup()` calls it.

    **This is the case the two conditions above cannot be.** Stage two runs from
    the platform owner's `ready()`, and every adopted application is installed
    after that owner -- so in a deployed process conditions 10 and 11 sweep a
    registry this hook has not populated yet, and both pass over nothing. Calling
    the same two public rules immediately after registering is what makes the
    refusals real, and this drives the hook rather than the rules.

    `tests/integration/startup/test_collector_boot_refusal.py` makes the same
    claim through a real `django.setup()` in a child process, which is the only
    place it can be made end to end; what this adds is the in-process case, where
    the raise itself is observable.
    """
    misdeclared = (
        _sweepable(name="cpm-fixture-ready-hook", freshness_target=None)
        if fault == "freshness"
        else _sweepable(name="cpm-fixture-ready-hook", cadence=FIXTURE_CADENCE * 7)
    )
    hook = apps.get_app_config("collectors")

    with (
        registered_collector(misdeclared),
        override_settings(CELERY_BEAT_SCHEDULE=_schedule_with((misdeclared.name, FIXTURE_CADENCE))),
        pytest.raises(ImproperlyConfigured, match=expected),
    ):
        hook.ready()


def test_the_collectors_app_ready_hook_accepts_the_roster_it_registers() -> None:
    """The negative control, and the one every process in this repository depends on.

    A hook that refused what it had just registered would stop the component
    outright -- and it would do so while both refusal cases above still passed.
    Calling `ready()` again is a no-op by construction: the registration guard
    compares identity and the adapter guard compares the path.
    """
    apps.get_app_config("collectors").ready()
