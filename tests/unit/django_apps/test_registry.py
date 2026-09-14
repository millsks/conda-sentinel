"""The collector registry: explicit adoption, and the two things it refuses.

`core/registry.py` is what `config/startup/stage_two.py` sweeps to make
`CPM-AD-28` enforceable -- "every *registered* collector declares a freshness
target" needs a list of registered collectors to be a sentence about anything.
Adoption is explicit (inherited `AD-8`: declared, never discovered), so the whole
of this module's behaviour is three calls and two refusals.

**Every case leaves the registry as it found it.** The registry is process-global
and the suite is not: a registration left behind is a collector every later boot
sweep in the session refuses over, and the failure lands in whichever case ran
next rather than in the one that caused it. `tests/collectors.py`'s
`registered_collector` context manager is what withdraws it, in a `finally`, and
one case here asserts that the withdrawal actually happens rather than trusting
it -- a leak-proofing helper that had stopped working would be invisible in every
other case in this file.

This is a unit test: it builds collector classes and a mapping. No database, no
network, and no collector is ever constructed, because registration reads a class
attribute rather than building a transport.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from conda_sentinel.core.collection import Collector
from conda_sentinel.core.registry import CollectorRegistryError
from conda_sentinel.core.registry import register
from conda_sentinel.core.registry import registered_collectors
from conda_sentinel.core.registry import registrations
from conda_sentinel.core.registry import selects
from conda_sentinel.core.registry import swept_collectors
from conda_sentinel.core.registry import unregister
from tests.collectors import FIXTURE_COLLECTOR
from tests.collectors import OTHER_FIXTURE_COLLECTOR
from tests.collectors import collector_class
from tests.collectors import fixture_evidence_model
from tests.collectors import registered_collector

if TYPE_CHECKING:
    from collections.abc import Iterator


def _a_collector(name: str = FIXTURE_COLLECTOR) -> type[Collector]:
    """Build a fully declared fixture collector class.

    Args:
        name: The name it declares, which is what the registry keys on.

    Returns:
        A concrete `Collector` subclass, unconstructed.

    """
    return collector_class(declared_model=fixture_evidence_model(), declared_name=name)


def test_a_registered_collector_is_returned_by_the_sweep() -> None:
    """The whole point: startup can ask what this component has adopted."""
    built = _a_collector()

    with registered_collector(built):
        assert built in registered_collectors()


def test_withdrawing_a_collector_removes_it_again() -> None:
    """The leak-proofing itself, asserted rather than relied on.

    Every other case in this file trusts `registered_collector` to put the
    registry back. If it had stopped doing so, all of them would still pass and
    the damage would surface somewhere else entirely -- in whichever later case
    happened to run a boot sweep.
    """
    built = _a_collector()

    with registered_collector(built):
        pass

    assert built not in registered_collectors()
    assert FIXTURE_COLLECTOR not in registrations()


def test_a_second_class_under_the_same_name_is_refused() -> None:
    """Two collectors sharing a name are indistinguishable everywhere it matters.

    The name is what ledger rows carry (`CPM-FR-39`) and what the rate-limit
    cache key is built from, so a silent overwrite would give two collectors one
    allowance, one run history and one identity in every report -- while only one
    of them is what the registry returns, and which one depends on import order.
    """
    first = _a_collector()
    second = _a_collector()

    with registered_collector(first), pytest.raises(CollectorRegistryError, match="already registered"):
        register(second)


def test_two_collectors_with_different_names_both_register() -> None:
    """The negative control, without which the refusal above proves nothing.

    A registry that refused every second registration would satisfy the case
    above and would make a component with two collectors impossible.
    """
    first = _a_collector()
    second = _a_collector(OTHER_FIXTURE_COLLECTOR)

    with registered_collector(first), registered_collector(second):
        assert set(registered_collectors()) >= {first, second}


def test_the_sweep_returns_collectors_in_a_fixed_order() -> None:
    """A component meets the same refusal first however its `ready()` is written.

    `AD-26` asks the stage-2 roster for "one location, one owner, and a fixed
    order" because which refusal an operator meets is the diagnostic they read.
    A registry answering in insertion order would make that a property of the
    sequence somebody happened to write their registrations in.
    """
    first = _a_collector(OTHER_FIXTURE_COLLECTOR)
    second = _a_collector(FIXTURE_COLLECTOR)

    with registered_collector(first), registered_collector(second):
        registered = [collector for collector in registered_collectors() if collector in {first, second}]

    assert [collector.name for collector in registered] == sorted({FIXTURE_COLLECTOR, OTHER_FIXTURE_COLLECTOR})


@pytest.mark.parametrize(
    "candidate",
    [object(), object, "a-collector", None],
    ids=["an-instance", "a-plain-class", "a-name", "nothing"],
)
def test_something_that_is_not_a_collector_is_refused(candidate: object) -> None:
    """A registered class that does not inherit the base carries none of its rules.

    Every external-call rule this product has -- the timeout, the retry policy,
    the rate limit, the conditional request -- lives in `Collector`
    (`CPM-AD-20`, `CPM-AD-27`). A registry that accepted anything callable would
    let a collector opt out of all of them by not inheriting, and the boot sweep
    would then read a `freshness_target` attribute that nothing had ever checked.
    """
    with pytest.raises(CollectorRegistryError, match="not a Collector subclass"):
        register(candidate)  # type: ignore[arg-type]


def test_a_collector_declaring_no_name_is_refused() -> None:
    """A registry keyed on a name cannot accept a class that has none.

    `core/collection.py` refuses a blank name at construction because a run has
    to be traceable to the code that performed it; the same blank name here would
    be a key nothing can be looked up by and a refusal message naming ``''``.
    This is also what keeps the abstract base itself out of the registry -- it
    declares `name = ""`.
    """
    anonymous = collector_class(declared_model=fixture_evidence_model(), declared_name="")

    with pytest.raises(CollectorRegistryError, match="cannot be registered under it"):
        register(anonymous)


def test_the_base_itself_cannot_be_registered() -> None:
    """The abstract base is not a collector anybody adopted.

    Registering it would put a class with no `evidence_model`, no window and no
    target into the boot sweep, which would then refuse every deployed component
    for a collector nobody wrote.
    """
    with pytest.raises(CollectorRegistryError):
        register(Collector)


def test_withdrawing_something_that_was_never_registered_is_refused() -> None:
    """A silent no-op turns a misspelled withdrawal into a registration nobody sees.

    The caller believes it withdrew a collector; the registry still holds one,
    and the next boot sweep refuses over a class the caller thinks is gone.
    """
    with pytest.raises(CollectorRegistryError, match="nothing to withdraw"):
        unregister("no-such-collector")


def test_the_registry_cannot_be_widened_through_what_it_hands_back() -> None:
    """A caller holding a copy cannot adopt a collector by mutating it.

    The same reason `core/collection.py` returns declared headers as a read-only
    mapping: a reader that could write is a second, undocumented registration
    path, and one that bypasses every refusal above.
    """
    built = _a_collector()

    with registered_collector(built):
        held = registrations()
        held.clear()  # type: ignore[attr-defined]

        assert built in registered_collectors()


def test_the_refusal_is_a_value_error() -> None:
    """One family of declaration defects, so a caller catching one catches them all."""
    assert issubclass(CollectorRegistryError, ValueError)


def test_the_swept_set_is_the_registered_collectors_declaring_a_selection() -> None:
    """`swept_collectors()`: `None` means not swept, and anything else -- an empty generator included -- means swept."""
    unswept = _a_collector()
    swept = collector_class(declared_model=fixture_evidence_model(), declared_name=OTHER_FIXTURE_COLLECTOR)
    swept.selectable_packages = classmethod(lambda cls: iter(()))  # type: ignore[method-assign]

    with registered_collector(unswept), registered_collector(swept):
        assert unswept.selectable_packages() is None
        assert unswept not in swept_collectors()
        assert swept in swept_collectors()


def test_selects_materialises_a_sequence_selection_never_iterates_a_generator_and_refuses_an_unswept_collector() -> (
    None
):
    """The three shapes a unit case can build: a sequence, a generator, and `None`.

    A generator is answered `False` without being drawn: the two the product
    declares are the empty ones an undeclared source answers with, and each
    warns on first use. The queryset shapes need a database and are asserted in
    `tests/integration/django_apps/test_recollection.py`.
    """
    unswept = _a_collector()
    swept = collector_class(declared_model=fixture_evidence_model(), declared_name=OTHER_FIXTURE_COLLECTOR)
    swept.selectable_packages = classmethod(lambda cls: [7, 9])  # type: ignore[method-assign]
    drawn: list[int] = []

    def a_generator() -> Iterator[int]:
        drawn.append(7)
        yield 7

    lazy = collector_class(declared_model=fixture_evidence_model(), declared_name="lazy")
    lazy.selectable_packages = classmethod(lambda cls: a_generator())  # type: ignore[method-assign]

    assert selects(unswept, 7) is False
    assert selects(swept, 7) is True
    assert selects(swept, 8) is False
    assert selects(lazy, 7) is False
    assert drawn == [], "the generator was never iterated"
