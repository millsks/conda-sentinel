"""The run ledger's vocabulary: what happened to a run, which is not a package's status.

`CPM-AD-2` exempts `collection_runs` and `policy_runs` from the append-only base
and makes them mutable by construction: a row is created with `running` before
the first outbound call and finalized on every exit path. This module declares
the five values that column may hold, which of them end a run, and the error a
recorder raises when a caller tries to end one twice.

**This is not `CPM-AD-5`'s outcome vocabulary, and it must never be composed from
`outcome_type`.** `core/outcomes.py` says it is "the whole of `CPM-AD-5`'s
implementation", and that stays true because the two vocabularies answer
different questions. `OutcomeState` answers *what is this package's derived
status*; `RunState` answers *what happened to this run*. The four outcome
sentinels are meaningless here -- a run is never `not_applicable` and never
`not_found` -- and `running` is meaningless there, because a package's licence
verdict is not in progress. Building this type with `outcome_type` would put four
unreachable values in the `status` column of every ledger row and would make
`aggregate()` willing to rank a run's lifecycle against a licence verdict, which
is the incompatible-vocabulary failure `CPM-AD-5` exists to prevent, arrived at
by obedience to the letter of it.

`tests/unit/django_apps/test_runs.py` pins that separation from both sides: that
`RunState` carries none of the four sentinels, and that `verify_sentinels`
refuses it -- so a later edit that "unified" the two types fails rather than
quietly widening what a status column may hold.

**Why the column is still called `status`.**
`tests/unit/django_apps/test_outcome_field_audit.py` decides what is a derived
status by field *name*, and `status` is in its table, so the ledger's column
trips a sweep written for a rule it is not bound by. Renaming the column to
`state` would dodge a marker, which `tests/model_registry.py` names as the worse
option in the very passage that explains why the `not_evidence` escape exists.
So the name stays and the audit is amended by a recorded table whose entries are
checked against `RunState.choices` -- excluded from one vocabulary and checked
against the other, never simply unchecked.

**On the `AD-` prefix.** A bare `AD-n` in this repository is an *inherited*
platform decision; a decision from this product's own architecture spine always
carries the `CPM-` prefix.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from typing import Final

from django.db import models

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = [
    "NO_RUNS",
    "TERMINAL_STATES",
    "RunLedgerError",
    "RunState",
    "counts_by_state",
    "states_as_words",
]


class RunState(models.TextChoices):
    """The five states a run-ledger row's `status` may hold.

    Fixed lowercase string values, on the same terms as `OutcomeState`'s and for
    the same reason -- the value is what a person reads off a row when they are
    asking why a sweep produced nothing -- but drawn from a different vocabulary.
    See the module docstring for why the two are separate.

    `RUNNING` is the value the row is *created* with, before the first outbound
    call, and it is the only one that is not an ending. A row still carrying it
    after the process that wrote it is gone is exactly the observation
    `CPM-EVIDENCE-S03` exists to make possible: the run started and never
    finished, which is a different fact from the run never having started.

    The four endings are distinct because a collector needs all four.
    `SUCCEEDED` and `FAILED` are the obvious pair; `PARTIAL` is what
    `CPM-AD-23` requires when one package in a sweep fails and the rest commit;
    `SKIPPED` is what `CPM-AD-7`'s observation window writes when a second run
    inside the window declines to observe again, and it is emphatically not a
    failure.
    """

    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"
    SKIPPED = "skipped"


#: The states that end a run, which is every member but `RUNNING`.
#:
#: Derived by exclusion rather than written out, so a sixth ending added to
#: `RunState` is terminal the moment it is declared and cannot be forgotten here.
#: A frozenset rather than a tuple: nothing ranks these against each other, and a
#: sequence would invite somebody to try. `RunState` is not `OutcomeState`, so
#: there is no precedence order to reach for and none is declared -- the single
#: total order in `core/outcomes.py` is over the outcome vocabulary and is not
#: this one's to extend.
TERMINAL_STATES: Final[frozenset[RunState]] = frozenset(state for state in RunState if state is not RunState.RUNNING)


#: What a count per state reads as when every state is zero.
NO_RUNS: Final[str] = "none"


def counts_by_state(counted: Mapping[str, int]) -> dict[str, int]:
    """Return a count per `RunState`, every state present and zero where nothing was counted.

    The shape the operator digest stores (`CPM-OPERATE-S09`): every state
    rather than the non-zero ones, so two days' figures compare by value and a
    page renders one shape whatever the day held.

    Args:
        counted: What was tallied, by state value.

    Returns:
        The five states in declaration order.

    """
    return {state.value: int(counted.get(state.value, 0) or 0) for state in RunState}


def states_as_words(counted: Mapping[str, object]) -> str:
    """Return a count per state as a person reads it, non-zero states only.

    One spelling for the digest's text and the Digests page, which may not
    import each other: `330 succeeded, 10 failed`, or `none`.

    Args:
        counted: A count per state value; anything not a count reads as zero.

    Returns:
        The non-zero counts in declaration order, or `NO_RUNS`.

    """
    named = []
    for state in RunState:
        count = counted.get(state.value, 0)
        if isinstance(count, int) and not isinstance(count, bool) and count > 0:
            named.append(f"{count} {state.value}")
    return ", ".join(named) if named else NO_RUNS


class RunLedgerError(Exception):
    """A caller asked a run to end in two different ways.

    One type rather than a hierarchy, on the same terms as
    `core/models.py`'s `AppendOnlyError`: no caller branches on which pair of
    states collided -- every occurrence is a defect at the call site -- so the
    detail lives in the message rather than in the class.

    It raises rather than warning or taking the first answer, which inherited
    `CG-3` requires. A recorder that silently kept the first declaration would
    write a row saying `succeeded` for a run whose body went on to decide it had
    failed, and nothing downstream could tell that row from an honest one.
    """
