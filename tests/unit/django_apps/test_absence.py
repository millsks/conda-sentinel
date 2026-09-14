"""`CPM-OPERATE-S11`: the absence fold and its refusals, without a database.

`collectors/absence.py` answers "was this package absent at the cut-off" from an
ordered stream of inventory snapshot rows, and the answer is decided in Python:
which row is newest, when the package was last listed, and when its current
listing began. `fold_observations` is the pure half of that read, so every
arrangement the story names -- never listed, listed, absent, re-listed, absent
again -- is assertable here as a tuple stream, in the tier a developer runs on
every change.

**The refusals need no database and that is itself asserted.** `absence_at`
checks the cut-off before it opens a query, so the refusal cases run in a module
with no `django_db` marker: were the check to move below the read, pytest-django
would fail the case for touching the database rather than let it pass for the
wrong reason.

**The cut-off boundary itself is a SQL filter** (`observed_at__lte`), so it is
asserted against a real table in `tests/integration/django_apps/test_absence.py`,
where a row at, before and after the cut-off can be written.
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING
from typing import Final

import pytest

from conda_sentinel.collectors.absence import Absence
from conda_sentinel.collectors.absence import absence_at
from conda_sentinel.collectors.absence import fold_observations
from conda_sentinel.collectors.absence import require_usable_cutoff
from conda_sentinel.collectors.models import InventoryReadError
from conda_sentinel.core.outcomes import OutcomeState
from conda_sentinel.surface.labels import absence_tag
from tests.clocks import FIXED_INSTANT
from tests.clocks import OBSERVATION_GAP

if TYPE_CHECKING:
    from datetime import datetime

#: One package, and a second so membership can be asserted.
A_PACKAGE: Final[int] = 7
ANOTHER_PACKAGE: Final[int] = 8

#: Four instants in order, one sweep apart.
FIRST: Final[datetime] = FIXED_INSTANT - 3 * OBSERVATION_GAP
SECOND: Final[datetime] = FIXED_INSTANT - 2 * OBSERVATION_GAP
THIRD: Final[datetime] = FIXED_INSTANT - OBSERVATION_GAP
FOURTH: Final[datetime] = FIXED_INSTANT

#: A breadth pair a present row carries.
A_BREADTH: Final[tuple[int, int]] = (12, 3)

_ROW = tuple[int, "datetime", int, str, int | None, int | None]


def listed(package: int, at: datetime, pk: int, breadth: tuple[int, int] = A_BREADTH) -> _ROW:
    """Return one `ok` row.

    Args:
        package: The package.
        at: When it was observed.
        pk: The row's primary key.
        breadth: The two counts the row carries.

    Returns:
        The tuple `fold_observations` reads.

    """
    return (package, at, pk, OutcomeState.OK.value, breadth[0], breadth[1])


def not_listed(package: int, at: datetime, pk: int, state: str = OutcomeState.NOT_FOUND.value) -> _ROW:
    """Return one row that carries no counts -- `not_found` unless another state is asked for.

    Args:
        package: The package.
        at: When it was observed.
        pk: The row's primary key.
        state: The row's state.

    Returns:
        The tuple `fold_observations` reads.

    """
    return (package, at, pk, state, None, None)


def absence_of(rows: list[_ROW], package: int = A_PACKAGE) -> Absence:
    """Fold the rows and return one package's absence reading.

    Args:
        rows: The stream, in `(observed_at, pk)` order.
        package: Which package to read.

    Returns:
        The `Absence`.

    """
    return fold_observations(rows)[package].absence()


# ---------------------------------------------------------------------------
# The fold, arrangement by arrangement.
# ---------------------------------------------------------------------------


def test_a_package_the_inventory_never_observed_has_no_history() -> None:
    """No row is not an absence: nothing has observed the package either way."""
    assert fold_observations([]) == {}
    assert A_PACKAGE not in fold_observations([listed(ANOTHER_PACKAGE, FIRST, 1)])


def test_a_listed_package_is_not_absent_and_carries_its_breadth() -> None:
    """The ordinary case: the newest row is `ok`, and both readings come off it."""
    history = fold_observations([listed(A_PACKAGE, FIRST, 1), listed(A_PACKAGE, SECOND, 2, (20, 4))])[A_PACKAGE]

    assert history.absence() == Absence(absent=False, since=None, last_listed=SECOND, listed_since=None)
    assert history.breadth() == (20, 4)


def test_a_package_whose_newest_row_is_not_found_is_absent_since_that_row() -> None:
    """`since` is the absence row's instant and `last_listed` the newest `ok` before it."""
    reading = absence_of([listed(A_PACKAGE, FIRST, 1), listed(A_PACKAGE, SECOND, 2), not_listed(A_PACKAGE, THIRD, 3)])

    assert reading == Absence(absent=True, since=THIRD, last_listed=SECOND, listed_since=None)


def test_only_not_found_means_absent() -> None:
    """An `error`, an `unknown` or a `not_applicable` newest row is a look that said nothing.

    The story's edge-case matrix is explicit: absent is "newest is `not_found`".
    The other sentinels carry no counts by the table's constraint and rank last in
    the selection for want of breadth, but the inventory did not say the package
    was gone, and the product must not either.
    """
    for state in (OutcomeState.ERROR.value, OutcomeState.UNKNOWN.value, OutcomeState.NOT_APPLICABLE.value):
        reading = absence_of([listed(A_PACKAGE, FIRST, 1), not_listed(A_PACKAGE, SECOND, 2, state)])

        assert reading == Absence(absent=False, since=None, last_listed=FIRST, listed_since=None), state


def test_a_re_listed_package_is_not_absent_and_carries_the_start_of_its_new_listing() -> None:
    """`listed_since` is the first `ok` after the most recent `not_found` -- the epoch the identity key names."""
    reading = absence_of(
        [
            listed(A_PACKAGE, FIRST, 1),
            not_listed(A_PACKAGE, SECOND, 2),
            listed(A_PACKAGE, THIRD, 3),
            listed(A_PACKAGE, FOURTH, 4),
        ],
    )

    assert reading == Absence(absent=False, since=None, last_listed=FOURTH, listed_since=THIRD)


def test_a_package_absent_again_after_a_re_listing_has_no_current_epoch() -> None:
    """A second absence closes the epoch: `listed_since` is `None` again while it is absent."""
    reading = absence_of(
        [
            listed(A_PACKAGE, FIRST, 1),
            not_listed(A_PACKAGE, SECOND, 2),
            listed(A_PACKAGE, THIRD, 3),
            not_listed(A_PACKAGE, FOURTH, 4),
        ],
    )

    assert reading == Absence(absent=True, since=FOURTH, last_listed=THIRD, listed_since=None)


def test_a_package_never_absent_has_no_listing_epoch() -> None:
    """`listed_since` is `None` for a package that was never absent, so its identity key never changes."""
    reading = absence_of([listed(A_PACKAGE, FIRST, 1), listed(A_PACKAGE, SECOND, 2), listed(A_PACKAGE, THIRD, 3)])

    assert reading.listed_since is None


def test_a_package_whose_first_row_is_an_absence_was_never_listed() -> None:
    """Reachable when the purge has removed every `ok` row and the `not_found` survives: `last_listed` is `None`.

    The tag and the justification then read "absent from the inventory since
    <date>" with no last-listed clause -- asserted on the tag here, on the
    justification in the integration module.
    """
    reading = absence_of([not_listed(A_PACKAGE, FIRST, 1)])

    assert reading == Absence(absent=True, since=FIRST, last_listed=None, listed_since=None)
    assert (
        absence_tag(reading.since, reading.last_listed) == f"absent from the inventory since {FIRST.date().isoformat()}"
    )
    assert absence_tag(None, None) == ""


def test_two_rows_at_one_instant_are_resolved_by_the_highest_key() -> None:
    """One sweep stamps every row with the run's one instant (`CPM-AD-7`), so ties are the normal case.

    The stream is ascending by `(observed_at, pk)` -- the query asks for that
    order and the epoch bookkeeping walks it -- so the row with the higher key is
    the later observation, on the same terms `snapshot_as_of` applies with
    `-observed_at, -pk`. Both orders of the two states are asserted, so a fold
    that read the *lower* key as later fails one of them.
    """
    listed_then_gone = absence_of([listed(A_PACKAGE, FIRST, 1), not_listed(A_PACKAGE, FIRST, 2)])
    gone_then_listed = absence_of([not_listed(A_PACKAGE, FIRST, 1), listed(A_PACKAGE, FIRST, 2)])
    breadth = fold_observations([listed(A_PACKAGE, FIRST, 1, (1, 1)), listed(A_PACKAGE, FIRST, 2, (2, 2))])

    assert listed_then_gone.absent is True
    assert gone_then_listed.absent is False
    assert gone_then_listed.listed_since == FIRST
    assert breadth[A_PACKAGE].breadth() == (2, 2)


def test_since_is_the_first_absence_row_and_a_repeated_absence_does_not_move_it() -> None:
    """A source that repeats its absence row night after night keeps the date the package left."""
    reading = absence_of(
        [
            listed(A_PACKAGE, FIRST, 1),
            not_listed(A_PACKAGE, SECOND, 2),
            not_listed(A_PACKAGE, THIRD, 3),
            not_listed(A_PACKAGE, FOURTH, 4),
        ],
    )

    assert reading == Absence(absent=True, since=SECOND, last_listed=FIRST, listed_since=None)


@pytest.mark.parametrize(
    "state",
    [OutcomeState.ERROR.value, OutcomeState.UNKNOWN.value, OutcomeState.NOT_APPLICABLE.value],
)
def test_only_a_listing_ends_an_absence(state: str) -> None:
    """A look that said nothing after a `not_found` is not a re-listing: absent until an `ok` row.

    Args:
        state: A non-`ok`, non-`not_found` state for the newest row.

    """
    reading = absence_of(
        [listed(A_PACKAGE, FIRST, 1), not_listed(A_PACKAGE, SECOND, 2), not_listed(A_PACKAGE, THIRD, 3, state)]
    )

    assert reading == Absence(absent=True, since=SECOND, last_listed=FIRST, listed_since=None)


def test_membership_comes_from_the_caller() -> None:
    """Rows for packages the caller did not name are discarded before they are folded."""
    rows = [listed(A_PACKAGE, FIRST, 1), not_listed(ANOTHER_PACKAGE, FIRST, 2)]

    assert set(fold_observations(rows, package_ids=[ANOTHER_PACKAGE])) == {ANOTHER_PACKAGE}
    assert set(fold_observations(rows)) == {A_PACKAGE, ANOTHER_PACKAGE}


# ---------------------------------------------------------------------------
# The refusals, before anything is read.
# ---------------------------------------------------------------------------


def test_a_naive_cut_off_is_refused_before_anything_is_read() -> None:
    """The same refusal `snapshot_as_of` makes, at this door -- with no `django_db` marker on purpose."""
    with pytest.raises(InventoryReadError, match=r"inventory absence"):
        absence_at(cutoff=FIXED_INSTANT.replace(tzinfo=None))


def test_a_value_that_is_not_an_instant_is_refused_the_same_way() -> None:
    """`None` and a `date` are refused by name rather than reaching `is_aware` as an `AttributeError`."""
    with pytest.raises(InventoryReadError):
        absence_at(cutoff=None)  # type: ignore[arg-type]
    with pytest.raises(InventoryReadError):
        absence_at(cutoff=date(2026, 9, 4))  # type: ignore[arg-type]


def test_the_refusal_names_the_subject_and_the_value() -> None:
    """One rule, two readers: the message says which read refused and what it was given."""
    naive = FIXED_INSTANT.replace(tzinfo=None)

    with pytest.raises(InventoryReadError) as refused:
        require_usable_cutoff(naive, subject="the unresolved package queue")

    assert "the unresolved package queue" in str(refused.value)
    assert repr(naive) in str(refused.value)
