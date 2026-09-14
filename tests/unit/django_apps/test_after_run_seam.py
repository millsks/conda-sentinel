"""The seam `core` offers a policy run's aftermath, and the four ways it refuses.

`core/after_run.py` exists so that `core/policy_run.py` never imports
`workflow.services` -- the same inversion the pass registry uses, and the reason the
layering audit covers `workflow`. What it *is*, though, is a mutable module-level
registry, and every one of those has the same three failure modes: a thing registered
twice, a thing registered under nothing, and a thing that fails and is swallowed.

So the cases here are mostly refusals. The happy path is one line and is exercised by
every policy run in the integration suite; what needs saying out loud is what happens
when somebody registers a second step under one name, because the symptom is a run
that goes on succeeding with half its work not done.

**Every case restores the registry.** A test that registered a step and left it there
would change what every later policy run in the session does -- and the failure would
land in an unrelated module, which is exactly the shape of the defect
`test_finding_keys.py` caused earlier in this epic by declaring a model in a test
body.

**No database and no policy run**, which took one correction to be true: the
registry is already filled by adoption, so an early version of this module ran the
*real* opening step and failed with "Database access not allowed". The autouse
fixture empties the registry for each case and puts back exactly what was there.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from typing import Final

import pytest

from conda_sentinel.core.after_run import AfterRunStepError
from conda_sentinel.core.after_run import register_after_run_step
from conda_sentinel.core.after_run import registered_after_run_steps
from conda_sentinel.core.after_run import run_after_run_steps
from conda_sentinel.core.after_run import step_registrations
from conda_sentinel.core.after_run import unregister_after_run_step

if TYPE_CHECKING:
    from collections.abc import Iterator
    from collections.abc import Mapping

    from conda_sentinel.core.clock import Clock
    from conda_sentinel.core.models import PolicyRun

#: A name no adopted application uses, so registering it cannot collide with the real
#: step `conda_sentinel.workflow` registers at `ready()`.
A_FIXTURE_STEP: Final[str] = "tests.after_run_fixture"
ANOTHER_FIXTURE_STEP: Final[str] = "tests.after_run_fixture_two"

#: What a step reports having done. Any number; what matters is that it comes back.
A_COUNT: Final[int] = 7


class _Run:
    """The one attribute the seam reads off a run, so no database is needed.

    `run_after_run_steps` passes the run through to each step and names its primary
    key in a refusal. Nothing else about a `PolicyRun` is touched, so a stand-in is
    honest here rather than a shortcut -- a real row would prove nothing extra and
    would make this an integration module.
    """

    pk = 1


@pytest.fixture(autouse=True)
def adopted_steps() -> Iterator[Mapping[str, object]]:
    """Empty the registry for each case, hand over what was there, and put it back.

    Named for what it yields rather than with a leading underscore: an autouse
    fixture that also produces a value is both, and `_`-prefixed names read as "this
    is injected for its side effect only".

    **Emptying it, not just cleaning up after.** The registry is already filled by
    adoption -- `conda_sentinel.workflow` registers its opening step at `ready()` --
    so a case that registered a fixture step and called `run_after_run_steps` would
    run the *real* step too, which reads the database. The first version of this
    module did exactly that and failed with "Database access not allowed", which was
    a true report of a test asserting something it had not arranged.

    Restoring rather than clearing, because the registry outlives this module: a
    later policy run in the same session opens no queue items if the real step is
    gone, and the failure lands somewhere else entirely.

    Yields:
        What adoption had registered before this case emptied the registry, so the
        case below can assert against it rather than re-registering a real name and
        colliding with its own teardown.

    """
    adopted = dict(step_registrations())
    for name in adopted:
        unregister_after_run_step(name)
    try:
        yield adopted
    finally:
        for name in (A_FIXTURE_STEP, ANOTHER_FIXTURE_STEP):
            unregister_after_run_step(name)
        for name, step in adopted.items():
            register_after_run_step(name, step)


def a_step(count: int = A_COUNT) -> object:
    """Return a step that reports a count and records that it ran.

    Args:
        count: What it reports.

    Returns:
        The step, carrying a `calls` list a case can read.

    """

    def step(*, run: PolicyRun, clock: Clock) -> int:
        step.calls.append(run)  # type: ignore[attr-defined]
        return count

    step.calls = []  # type: ignore[attr-defined]
    return step


def test_a_registered_step_runs_and_reports_what_it_did() -> None:
    """The happy path, and the count is the half worth asserting.

    A step that ran and reported nothing leaves a run's log saying it happened and
    not whether it found anything, which is the log line an operator reads on the
    morning after a quiet night.
    """
    register_after_run_step(A_FIXTURE_STEP, a_step())  # type: ignore[arg-type]

    reported = run_after_run_steps(run=_Run(), clock=None)  # type: ignore[arg-type]

    assert reported == {A_FIXTURE_STEP: A_COUNT}


def test_steps_run_in_registration_order() -> None:
    """Declaration order, on the terms `core/policy.py` sets for passes.

    A later step may depend on an earlier one having run, so the order is part of
    what was declared rather than an implementation detail of a dictionary.
    """
    register_after_run_step(A_FIXTURE_STEP, a_step(1))  # type: ignore[arg-type]
    register_after_run_step(ANOTHER_FIXTURE_STEP, a_step(2))  # type: ignore[arg-type]

    names = [name for name, _step in registered_after_run_steps()]

    assert names == [A_FIXTURE_STEP, ANOTHER_FIXTURE_STEP]


def test_a_second_step_under_one_name_is_refused() -> None:
    """The failure this refusal exists for is a run that goes on succeeding.

    Replacing the first silently would leave half the work undone and nothing to
    read: the run finishes, the log looks ordinary, and a queue is simply empty.
    """
    register_after_run_step(A_FIXTURE_STEP, a_step())  # type: ignore[arg-type]

    with pytest.raises(AfterRunStepError, match=r"already registered"):
        register_after_run_step(A_FIXTURE_STEP, a_step())  # type: ignore[arg-type]


def test_a_step_registered_under_no_name_is_refused() -> None:
    """The name is what a refusal, a log line and a duplicate registration all say.

    An unnamed step is one nobody can find when a run reports it failed.
    """
    with pytest.raises(AfterRunStepError, match=r"under no name"):
        register_after_run_step("", a_step())  # type: ignore[arg-type]


def test_a_failing_step_fails_the_run() -> None:
    """`CG-3` applied here, and the alternative is the expensive one.

    Logging it and finishing `succeeded` produces a run that reports success while
    the queues it was supposed to fill are empty -- nobody looking at work nobody
    knows exists.
    """

    def broken(*, run: PolicyRun, clock: Clock) -> int:
        message = "the step could not do its work"
        raise RuntimeError(message)

    register_after_run_step(A_FIXTURE_STEP, broken)

    with pytest.raises(AfterRunStepError, match=A_FIXTURE_STEP):
        run_after_run_steps(run=_Run(), clock=None)  # type: ignore[arg-type]


def test_a_failing_step_names_the_run_it_failed() -> None:
    """So an operator reading the exception knows which run to look at.

    A message naming only the step leaves them grepping a night's logs for it.
    """

    def broken(*, run: PolicyRun, clock: Clock) -> int:
        message = "no"
        raise RuntimeError(message)

    register_after_run_step(A_FIXTURE_STEP, broken)

    with pytest.raises(AfterRunStepError, match=r"policy run 1"):
        run_after_run_steps(run=_Run(), clock=None)  # type: ignore[arg-type]


def test_a_failing_step_keeps_the_original_error_as_its_cause() -> None:
    """The wrapper says which step; the cause says what actually went wrong.

    Losing the cause would leave a traceback that names a step and explains nothing.
    """

    def broken(*, run: PolicyRun, clock: Clock) -> int:
        message = "the underlying thing that broke"
        raise RuntimeError(message)

    register_after_run_step(A_FIXTURE_STEP, broken)

    with pytest.raises(AfterRunStepError) as failure:
        run_after_run_steps(run=_Run(), clock=None)  # type: ignore[arg-type]

    assert isinstance(failure.value.__cause__, RuntimeError)
    assert "the underlying thing that broke" in str(failure.value.__cause__)


def test_withdrawing_a_step_that_was_never_registered_is_not_an_error() -> None:
    """A teardown should not have to know whether its setup got that far."""
    unregister_after_run_step("tests.a-step-nobody-registered")


def test_the_registrations_mapping_is_a_copy() -> None:
    """A caller cannot widen or empty the registry by mutating what it was handed.

    The same reason `core/policy.py`'s registry returns one -- and the failure a
    shared dictionary produces is a step that disappears mid-session with nothing
    naming the culprit.
    """
    register_after_run_step(A_FIXTURE_STEP, a_step())  # type: ignore[arg-type]

    handed = step_registrations()
    handed.clear()  # type: ignore[attr-defined]

    assert A_FIXTURE_STEP in step_registrations()


def test_the_real_step_is_registered_by_adoption(adopted_steps: Mapping[str, object]) -> None:
    """The seam is filled, which is what makes every case above about something.

    `conda_sentinel.workflow` registers its opening step at `ready()`. If adoption
    stopped registering it, every case here would still pass and no policy run would
    open a queue item -- so the fact of registration is asserted rather than assumed.

    Reads what the autouse fixture *saved* rather than what is registered now, since
    the fixture empties the registry for the duration of each case. That is also the
    honest read: the question is what adoption produced, not what this module left
    behind.

    Args:
        adopted_steps: What adoption had registered, from the autouse fixture.

    """
    from conda_sentinel.workflow.opening import OPENING_STEP_NAME  # noqa: PLC0415 - read beside the claim

    assert OPENING_STEP_NAME in adopted_steps


def test_the_absence_step_is_registered_by_adoption_and_runs_before_the_opening_step(
    adopted_steps: Mapping[str, object],
) -> None:
    """`CPM-OPERATE-S11`: two adopted steps, in the order the queues need.

    `conda_sentinel.collectors` registers the step that stamps inventory absence
    on the rollup, and it must run before `workflow`'s opening step reads the
    rollup -- steps run in registration order, and registration order is
    `INSTALLED_APPS` order. Both facts are pinned here: the adoption, and the
    order it produced, so a reordering of the settings list fails a named case
    rather than labelling every absent package one run late.

    Args:
        adopted_steps: What adoption had registered, from the autouse fixture,
            in registration order.

    """
    from django.conf import settings  # noqa: PLC0415 - read beside the claim

    from conda_sentinel.collectors.absence import ABSENCE_STEP_NAME  # noqa: PLC0415 - read beside the claim
    from conda_sentinel.collectors.absence import mark_inventory_absence  # noqa: PLC0415 - read beside the claim
    from conda_sentinel.workflow.opening import OPENING_STEP_NAME  # noqa: PLC0415 - read beside the claim

    registered = list(adopted_steps)
    installed = list(settings.INSTALLED_APPS)

    assert adopted_steps[ABSENCE_STEP_NAME] is mark_inventory_absence
    assert registered.index(ABSENCE_STEP_NAME) < registered.index(OPENING_STEP_NAME)
    assert installed.index("conda_sentinel.collectors") < installed.index("conda_sentinel.workflow")
