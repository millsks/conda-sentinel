"""`CPM-CURRENCY-S05`: the dispatch's declarations, its derivations and its chunking.

Everything about `collectors/sweep.py` that is decidable with no database, no
broker and no clock. The dispatch itself opens a run-ledger row, so every case
that *runs* one is in `tests/integration/django_apps/test_sweep.py`; what is here
is the arithmetic and the shapes -- the derived task name, the chunking, the
declared constants, the nine swept collectors' cadences, and the three places one
name is spelled in two modules and has to agree. "Swept" is the nine that declare
a cadence and a selection; of the other two registered collectors, inventory
ingestion is run-scoped and a dispatch refuses it by name, and Python 3.14
verification is triggered rather than swept.

**The nine swept collectors' selections are asserted here as queries rather than
as results.** `selectable_packages` answers with a lazy queryset, and a queryset's
`model` and its `query` are readable without a database -- which is what lets the
unit tier pin *which table each collector selects from and on what condition*,
where the integration tier pins which packages come back. Both matter and neither
is the other: a selection that read the right table with the wrong filter would
pass a results case written against a fixture that happened not to distinguish
them.

Reads and parses repository files, imports modules and builds querysets. No
database, no network, no subprocess.
"""

from __future__ import annotations

import ast
import inspect
from dataclasses import FrozenInstanceError
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING
from typing import Final

import pytest
from celery.schedules import crontab
from django.db.models import QuerySet
from django.test import override_settings

from conda_sentinel.collectors.conda_package import CHANNELS_SETTING
from conda_sentinel.collectors.conda_package import COLLECTOR_NAME as CONDA_PACKAGE_NAME
from conda_sentinel.collectors.conda_package import CONDA_PACKAGE_CADENCE
from conda_sentinel.collectors.conda_package import PLATFORMS_SETTING
from conda_sentinel.collectors.conda_package import CondaPackageCollector
from conda_sentinel.collectors.feedstock import COLLECTOR_NAME as FEEDSTOCK_NAME
from conda_sentinel.collectors.feedstock import FEEDSTOCK_CADENCE
from conda_sentinel.collectors.feedstock import FeedstockCollector
from conda_sentinel.collectors.kev import COLLECTOR_NAME as KEV_NAME
from conda_sentinel.collectors.kev import KEV_CADENCE
from conda_sentinel.collectors.kev import KevCollector
from conda_sentinel.collectors.license import COLLECTOR_NAME as LICENSE_NAME
from conda_sentinel.collectors.license import LICENSE_CADENCE
from conda_sentinel.collectors.license import LicenseCollector
from conda_sentinel.collectors.pypi_release import COLLECTOR_NAME as PYPI_RELEASE_NAME
from conda_sentinel.collectors.pypi_release import PYPI_RELEASE_CADENCE
from conda_sentinel.collectors.pypi_release import PyPIReleaseCollector
from conda_sentinel.collectors.python_readiness import COLLECTOR_NAME as PYTHON_READINESS_NAME
from conda_sentinel.collectors.python_readiness import READINESS_CADENCE
from conda_sentinel.collectors.python_readiness import PythonReadinessCollector
from conda_sentinel.collectors.resolve_identity import COLLECTOR_NAME as RESOLVE_IDENTITY_NAME
from conda_sentinel.collectors.resolve_identity import RESOLUTION_CADENCE
from conda_sentinel.collectors.resolve_identity import IdentityResolutionCollector
from conda_sentinel.collectors.source_release import COLLECTOR_NAME as SOURCE_RELEASE_NAME
from conda_sentinel.collectors.source_release import SOURCE_RELEASE_CADENCE
from conda_sentinel.collectors.source_release import SourceReleaseCollector
from conda_sentinel.collectors.sweep import COLLECTOR_KWARG
from conda_sentinel.collectors.sweep import EVENT_KEYS
from conda_sentinel.collectors.sweep import PACKAGE_EVENT_KEYS
from conda_sentinel.collectors.sweep import PACKAGE_KWARG
from conda_sentinel.collectors.sweep import RESERVED_COLLECTOR_NAME
from conda_sentinel.collectors.sweep import SELECTION_CHUNK
from conda_sentinel.collectors.sweep import SWEEP_TASK_NAME
from conda_sentinel.collectors.sweep import DispatchOutcome
from conda_sentinel.collectors.sweep import SweepDispatchError
from conda_sentinel.collectors.sweep import _as_interval
from conda_sentinel.collectors.sweep import _scheduled_dispatches
from conda_sentinel.collectors.sweep import _streamed
from conda_sentinel.collectors.sweep import cadence_reconciliation_fault
from conda_sentinel.collectors.sweep import collection_task_name
from conda_sentinel.collectors.tasks import COLLECT_CONDA_PACKAGE_TASK_NAME
from conda_sentinel.collectors.tasks import COLLECT_FEEDSTOCK_TASK_NAME
from conda_sentinel.collectors.tasks import COLLECT_KEV_TASK_NAME
from conda_sentinel.collectors.tasks import COLLECT_LICENSE_TASK_NAME
from conda_sentinel.collectors.tasks import COLLECT_PYPI_RELEASE_TASK_NAME
from conda_sentinel.collectors.tasks import COLLECT_PYTHON_READINESS_TASK_NAME
from conda_sentinel.collectors.tasks import COLLECT_RESOLVE_IDENTITY_TASK_NAME
from conda_sentinel.collectors.tasks import COLLECT_SOURCE_RELEASE_TASK_NAME
from conda_sentinel.collectors.tasks import COLLECT_VULNERABILITY_TASK_NAME
from conda_sentinel.collectors.tasks import COLLECTOR_NAME as INVENTORY_COLLECTOR_NAME
from conda_sentinel.collectors.tasks import InventoryIngestionCollector
from conda_sentinel.collectors.tasks import collect_sweep
from conda_sentinel.collectors.vulnerability import COLLECTOR_NAME as VULNERABILITY_NAME
from conda_sentinel.collectors.vulnerability import VULNERABILITY_CADENCE
from conda_sentinel.collectors.vulnerability import VulnerabilityCollector
from conda_sentinel.core.collection import NO_CADENCE
from conda_sentinel.core.collection import Collector
from conda_sentinel.core.collection import CollectorConfigurationError
from conda_sentinel.core.collection import require_cadence
from conda_sentinel.core.outcomes import OutcomeState
from conda_sentinel.core.queues import Queue
from conda_sentinel.core.queues import queue_for
from conda_sentinel.core.runs import RunState
from conda_sentinel.identity.models import ESTABLISHED
from conda_sentinel.identity.models import MappingKind
from conda_sentinel.identity.models import Package
from conda_sentinel.identity.models import PackageMapping
from tests.collectors import FIXTURE_CADENCE
from tests.collectors import FIXTURE_FRESHNESS_TARGET
from tests.collectors import collector_class
from tests.collectors import fixture_evidence_model
from tests.collectors import selectable_collector_class
from tests.source_scan import project_files

if TYPE_CHECKING:
    from collections.abc import Iterator

#: The module the source sweeps at the foot of this file read.
SWEEP_MODULE: Final[Path] = (
    Path(__file__).resolve().parents[3] / "src" / "django_apps" / "conda_sentinel" / "collectors" / "sweep.py"
)

#: The nine per-package collectors and the cadence each declares, as one table
#: the cases below parametrize over. A tuple of triples rather than nine cases,
#: because every one of the assertions is the same sentence about a different
#: collector and writing it out nine times is how eight of them stop being
#: updated.
PER_PACKAGE_COLLECTORS: Final[tuple[tuple[type[Collector], str, timedelta], ...]] = (
    (SourceReleaseCollector, SOURCE_RELEASE_NAME, SOURCE_RELEASE_CADENCE),
    (PyPIReleaseCollector, PYPI_RELEASE_NAME, PYPI_RELEASE_CADENCE),
    (FeedstockCollector, FEEDSTOCK_NAME, FEEDSTOCK_CADENCE),
    (CondaPackageCollector, CONDA_PACKAGE_NAME, CONDA_PACKAGE_CADENCE),
    (VulnerabilityCollector, VULNERABILITY_NAME, VULNERABILITY_CADENCE),
    (KevCollector, KEV_NAME, KEV_CADENCE),
    (LicenseCollector, LICENSE_NAME, LICENSE_CADENCE),
    (PythonReadinessCollector, PYTHON_READINESS_NAME, READINESS_CADENCE),
    (IdentityResolutionCollector, RESOLVE_IDENTITY_NAME, RESOLUTION_CADENCE),
)

#: The calls a dispatch may not make, and each is a different rule.
#: `atomic` would be a boundary held across packages (`CPM-AD-23`); the four
#: writers would be a second evidence writer beside the base's (`CPM-AD-7`);
#: `fetch` would be a call outside the transport seam (`CPM-AD-27`).
FORBIDDEN_CALLS: Final[frozenset[str]] = frozenset(
    {"atomic", "save", "bulk_create", "create", "update", "delete", "fetch"},
)

#: `CPM-NFR-1`'s inventory, and what `SELECTION_CHUNK` is bounded against: a
#: fetch size at or above it would have the database driver materialise the whole
#: of one per round trip, which is precisely what the constant exists to prevent.
AN_INVENTORY: Final[int] = 10_000

#: How many keys the streaming cases offer, chosen so that it is more than one
#: database fetch: a count at or below `SELECTION_CHUNK` would leave the case
#: about reading no further ahead than necessary satisfied by a selection that
#: fitted in one read.
A_RAGGED_COUNT: Final[int] = SELECTION_CHUNK * 2 + 1


# ---------------------------------------------------------------------------
# The declarations, and the two names spelled in two modules.
# ---------------------------------------------------------------------------


def test_the_dispatch_task_name_routes_to_the_collect_queue() -> None:
    """`CPM-AD-20`: a dispatch is collection work and lands on the collection queue.

    Composed from `core/queues.py`'s own parts rather than spelled out, so the
    assertion is that the composition reaches the queue rather than that two
    literals match. A dispatch routed anywhere else would tick against the
    `verify` queue's compute slots, which is `R-11` exactly.
    """
    assert SWEEP_TASK_NAME == "cpm.collect.sweep"
    assert queue_for(SWEEP_TASK_NAME) is Queue.COLLECT


def test_the_dispatch_task_is_registered_under_the_name_the_module_declares() -> None:
    """The decorator and the constant, reconciled rather than assumed.

    A renamed constant with an unchanged decorator would route correctly in the
    queue case above while the worker registered something else, and the beat
    schedule would fire into nothing on every tick.
    """
    assert collect_sweep.name == SWEEP_TASK_NAME


def test_the_dispatch_task_takes_its_collector_under_the_keyword_the_module_names() -> None:
    """`COLLECTOR_KWARG` is what settings passes; this is what the task accepts.

    A parameter name cannot be written as a constant, so the two are spelled
    separately by necessity -- and a rename of either one alone would leave the
    beat schedule passing a keyword no task takes, which Celery reports at
    *execution* time, once per tick, in a worker log nobody is reading.
    """
    parameters = inspect.signature(collect_sweep.run).parameters

    assert set(parameters) == {COLLECTOR_KWARG}
    assert parameters[COLLECTOR_KWARG].kind is inspect.Parameter.KEYWORD_ONLY


@pytest.mark.parametrize(
    ("collector", "task_name"),
    [
        (SourceReleaseCollector, COLLECT_SOURCE_RELEASE_TASK_NAME),
        (PyPIReleaseCollector, COLLECT_PYPI_RELEASE_TASK_NAME),
        (FeedstockCollector, COLLECT_FEEDSTOCK_TASK_NAME),
        (CondaPackageCollector, COLLECT_CONDA_PACKAGE_TASK_NAME),
        (VulnerabilityCollector, COLLECT_VULNERABILITY_TASK_NAME),
        (KevCollector, COLLECT_KEV_TASK_NAME),
        (LicenseCollector, COLLECT_LICENSE_TASK_NAME),
        (PythonReadinessCollector, COLLECT_PYTHON_READINESS_TASK_NAME),
        (IdentityResolutionCollector, COLLECT_RESOLVE_IDENTITY_TASK_NAME),
    ],
    ids=lambda value: getattr(value, "__name__", value),
)
def test_the_derived_task_name_is_the_one_each_collector_actually_declares(
    collector: type[Collector],
    task_name: str,
) -> None:
    """The derivation is not a convention this module hopes holds; it is asserted.

    `collection_task_name` builds `cpm.collect.<name>` from the registry's key.
    If any collector's task were declared under some other name the dispatch
    would refuse it at run time -- which is the safe failure -- but it would
    refuse it *every* tick, so the surface would go unobserved with a `failed`
    ledger row nobody reads. Reconciled here, where a rename fails a pull request.
    """
    assert collection_task_name(collector.name) == task_name


@pytest.mark.parametrize(
    ("collector", "name", "cadence"),
    PER_PACKAGE_COLLECTORS,
    ids=lambda value: getattr(value, "__name__", str(value)),
)
def test_each_per_package_collector_declares_the_cadence_its_targets_were_derived_from(
    collector: type[Collector],
    name: str,
    cadence: timedelta,
) -> None:
    """The tenth declaration, bound to the module constant rather than to a new number.

    `CPM-CURRENCY-S05` moves no number: each collector's freshness target and
    observation window were already derived from a module-level cadence constant,
    and what changes is that the class now *declares* it so something can read it.
    Asserted by identity against that constant, because a class attribute rebound
    to the target -- twice the cadence, and the same type -- would declare a
    plausible number and make every schedule entry disagree.
    """
    assert collector.name == name
    assert collector.cadence is cadence
    assert collector.freshness_target is not None
    assert collector.freshness_target > cadence


def test_the_run_scoped_collector_declares_no_cadence_and_no_selection() -> None:
    """Inventory ingestion is not swept per package, and says so by declaring nothing.

    Both defaults, and both matter. `cadence is None` is what keeps the boot
    reconciliation from demanding a `CELERY_BEAT_SCHEDULE` entry for a collector
    whose schedule is its own run-scoped sweep; `selectable_packages() is None` is
    what makes a dispatch refuse it *by name* rather than enqueue one task per
    package for a collector that refuses all three per-package hooks.
    """
    assert InventoryIngestionCollector.name == INVENTORY_COLLECTOR_NAME
    assert InventoryIngestionCollector.cadence is None
    assert InventoryIngestionCollector.selectable_packages() is None


def test_the_base_defaults_leave_a_collector_that_declares_neither_untouched() -> None:
    """The additive half: a collector written before this story declares nothing new.

    Asserted on the base itself rather than on a fixture, because what the story
    promises is that the *default* is "not swept", not that some particular
    subclass chose it.
    """
    assert Collector.cadence is None
    assert Collector.selectable_packages() is None


def test_the_event_keys_are_the_same_on_both_events_the_dispatch_emits() -> None:
    """One schema per key, on the terms `core/collection.py` fixes for its own.

    A `detail` that is a sentence on one event and an exception's text on another
    is two schemas wearing one key, and a log query written against either would
    be wrong half the time -- which is why `detail` is *in* the key set rather
    than an extra beside it. The per-package event has its own set because it
    names a package where the run-level three name counts; two schemas are two
    declarations, not one with holes in it.
    """
    assert EVENT_KEYS == ("collector", "task", "enqueued", "refused", "detail")
    assert PACKAGE_EVENT_KEYS == ("collector", "task", "package_id", "detail")
    # `detail` is on both, and that is the point rather than an overlap: it is the
    # one key whose *schema* the constants exist to fix.
    assert "detail" in set(EVENT_KEYS) & set(PACKAGE_EVENT_KEYS)


# ---------------------------------------------------------------------------
# `require_cadence`, the boot sweep's half of the declaration.
# ---------------------------------------------------------------------------


def test_a_usable_cadence_is_returned_unchanged() -> None:
    """The positive control, without which every refusal below proves nothing."""
    declared = timedelta(days=1)

    assert require_cadence(declared, label="ACollector") is declared


@pytest.mark.parametrize(
    "cadence",
    [None, NO_CADENCE, -timedelta(days=1), 86400, "1 day", True],
    ids=["absent", "zero", "negative", "seconds", "string", "flag"],
)
def test_a_cadence_that_is_not_a_positive_interval_is_refused(cadence: object) -> None:
    """Absent, zero, negative and mistyped, each for its own reason.

    Zero is the one worth naming: `NO_WINDOW` is the same interval and is
    *accepted*, because "observe on every run" is something an operator means,
    while "fire continuously" is not. The three sentinels look alike and behave
    differently, so the asymmetry is asserted rather than assumed.

    `True` is here because `bool` is a subclass of `int` and neither is a
    `timedelta`; a flag that reached this would be a cadence nobody wrote.
    """
    with pytest.raises(CollectorConfigurationError, match="cadence"):
        require_cadence(cadence, label="ACollector")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# The streaming, which is the whole of AC 4's "without manual batching".
# ---------------------------------------------------------------------------


def test_the_selection_chunk_is_a_database_read_size_rather_than_a_batch() -> None:
    """A positive number, and smaller than `CPM-NFR-1`'s inventory.

    The second half is the one that means something: a fetch size at or above ten
    thousand would satisfy every other case here and would have the driver
    materialise the whole inventory in one round trip, which is precisely what the
    constant exists to prevent.

    The constant is `.iterator(chunk_size=...)`'s argument and nothing else. An
    earlier version of this module also grouped the stream into batches of this
    size and called the grouping a memory bound; it was not one, because the
    stream is already lazy -- a batch was five hundred *additional* resident
    integers. The grouping is gone.
    """
    assert SELECTION_CHUNK > 0
    assert SELECTION_CHUNK < AN_INVENTORY


def test_a_queryset_selection_is_read_through_an_iterator_rather_than_its_cache() -> None:
    """The one transformation `_streamed` makes, and the reason it exists.

    A `QuerySet` caches every row it yields, so iterating one directly would hold
    the whole inventory whatever else this module did -- the cache is the
    queryset's, not the loop's. Asserted by identity: what comes back is not the
    queryset, and consuming it leaves the queryset's cache empty.
    """
    selection = CondaPackageCollector.selectable_packages()

    streamed = _streamed(CondaPackageCollector, selection)

    assert streamed is not selection
    assert iter(streamed) is streamed
    assert selection._result_cache is None  # noqa: SLF001 - the empty cache is the claim


def test_an_iterator_selection_is_taken_exactly_as_it_was_answered() -> None:
    """A hook that answered lazily has already decided; this module does not decide again."""
    answered = iter((7, 11))

    assert list(_streamed(CondaPackageCollector, answered)) == [7, 11]


@pytest.mark.parametrize(
    "selection",
    [[1, 2, 3], (1, 2, 3), {1, 2, 3}, frozenset({1, 2, 3})],
    ids=["list", "tuple", "set", "frozenset"],
)
def test_a_selection_that_has_already_been_read_into_memory_is_refused(selection: object) -> None:
    """The guard that keeps the streaming property from being defeated silently.

    A hook answering with a `list` has done the read this module exists not to do,
    and it would do it invisibly: every count would match, every behavioural case
    would pass, and `CPM-NFR-1`'s ten thousand keys would be resident. So it is
    refused by construction rather than asserted about somewhere.
    """
    built = collector_class(declared_model=fixture_evidence_model())

    with pytest.raises(SweepDispatchError, match="already been read into memory"):
        _streamed(built, selection)  # type: ignore[arg-type]


def test_the_stream_is_never_read_further_ahead_than_the_dispatch_has_enqueued() -> None:
    """AC 4 as a property of the code rather than of a comment.

    A dispatch that materialised its selection would pass every count in the
    integration tier and would hold ten thousand keys. Measured by handing
    `_streamed` a generator that records how far it has been read and consuming
    one key: at that point exactly one key has been produced, which nothing that
    read the selection into a list could manage.
    """
    consumed: list[int] = []

    def _counting() -> Iterator[int]:
        for key in range(A_RAGGED_COUNT):
            consumed.append(key)
            yield key

    stream = _streamed(CondaPackageCollector, _counting())
    first = next(stream)

    assert first == 0
    assert consumed == [0]


# ---------------------------------------------------------------------------
# The four selections, read as queries.
# ---------------------------------------------------------------------------


def test_the_source_release_selection_is_the_packages_with_a_source_repository() -> None:
    """`CPM-CURRENCY-S01`'s deferred precondition, expressed as the query that closes it.

    `source_for` reads exactly one column, so the selection is the complement of
    the one refusal a query can express: a blank `source_repository_url`. Read off
    the queryset rather than by running it, which is what pins *which* column the
    exclusion is on -- a selection filtered on the wrong blank string would return
    the same rows against a fixture with one package and different rows in
    production.
    """
    selection = SourceReleaseCollector.selectable_packages()
    rendered = str(selection.query)

    assert selection.model is Package
    assert "source_repository_url" in rendered
    assert "NOT" in rendered.upper()
    assert selection.query.order_by == ("pk",)
    # The negative control: the *other* blank-able identity columns are not what
    # this collector reads, and a selection filtered on one of them would return
    # the same rows against a package that has neither and different rows against
    # a real inventory.
    assert "primary_purl" not in rendered
    assert "conda_purl" not in rendered


@pytest.mark.parametrize(
    ("collector", "kind", "outcomes"),
    [
        (
            PyPIReleaseCollector,
            MappingKind.RELEASE_ECOSYSTEM.value,
            (ESTABLISHED, OutcomeState.NOT_APPLICABLE.value),
        ),
        (
            FeedstockCollector,
            MappingKind.FEEDSTOCK.value,
            (ESTABLISHED, OutcomeState.NOT_FOUND.value, OutcomeState.NOT_APPLICABLE.value),
        ),
    ],
    ids=["pypi_release", "feedstock"],
)
def test_a_mapping_backed_selection_reads_its_own_kind_and_its_own_outcomes(
    collector: type[Collector],
    kind: str,
    outcomes: tuple[str, ...],
) -> None:
    """The two collectors whose precondition is what resolution recorded.

    Both halves are asserted because both have failed in this repository before:
    `CPM-CURRENCY-S03`'s review found a read that dropped the `kind` filter and
    stayed green because every test package carried one mapping row. A selection
    that read *any* mapping would offer the PyPI collector packages whose feedstock
    resolution happened to be established, and every one of those runs would fail.
    """
    selection = collector.selectable_packages()
    rendered = str(selection.query)

    assert selection.model is PackageMapping
    assert kind in rendered
    for outcome in outcomes:
        assert outcome in rendered
    assert selection.query.order_by == ("package_id",)


def test_the_conda_package_selection_is_every_package_when_the_surfaces_are_declared() -> None:
    """`CPM-FR-10` applies to every package, so a declared component narrows nothing.

    "It is not there" is the observation the criterion asks for rather than a
    reason not to look, which is why this collector's `inapplicability` never
    answers a reason. A selection that filtered anything would make the absence
    rows the criterion is about unreachable for whatever it filtered out.
    """
    with override_settings(**{CHANNELS_SETTING: ("conda-forge",), PLATFORMS_SETTING: ("linux-64",)}):
        selection = CondaPackageCollector.selectable_packages()

        assert selection.model is Package
        assert "WHERE" not in str(selection.query).upper()
        assert selection.query.order_by == ("pk",)


def test_the_license_selection_is_every_package_when_a_channel_is_declared() -> None:
    """`CPM-FR-13` applies to every package, and this selection had no unit-tier case at all.

    Every sibling's selection is read here as a *query* -- the model it is against,
    that it filters nothing, and the ordering the chunked `.iterator()` in
    `collectors/sweep.py` depends on. The licence collector's was asserted only
    through an integration case that ran it, which cannot see an ordering: dropping
    `.order_by("pk")` left that case green and left a full-inventory sweep streaming
    an unordered queryset in chunks, which is a selection that may repeat a key and
    skip another.
    """
    with override_settings(**{CHANNELS_SETTING: ("conda-forge",)}):
        selection = LicenseCollector.selectable_packages()

        assert selection.model is Package
        assert "WHERE" not in str(selection.query).upper()
        assert selection.query.order_by == ("pk",)


@pytest.mark.parametrize(
    "channels",
    [(), "conda-forge", 7],
    ids=["nothing-declared", "mistyped-channel", "a-number"],
)
def test_the_license_selection_is_empty_until_a_channel_is_declared(channels: object) -> None:
    """The shipped state, on the terms the published-package selection answers it.

    `CPM_MONITORED_CHANNELS` ships empty (PRD Open Question 4) and an undeclared
    component refuses every package equally, so a selection that offered the inventory
    anyway would have the scheduled sweep write one `failed` collection per package
    per day out of the box. A *mistyped* declaration is here for the same reason it is
    on the sibling: a bare string is eleven one-character channels to Python.
    """
    with override_settings(**{CHANNELS_SETTING: channels}):
        selection = LicenseCollector.selectable_packages()

        assert list(selection) == []
        assert selection.model is Package


@pytest.mark.parametrize(
    ("channels", "platforms"),
    [
        ((), ()),
        (("conda-forge",), ()),
        ((), ("linux-64",)),
        ("conda-forge", ("linux-64",)),
    ],
    ids=["neither", "no-platform", "no-channel", "mistyped-channel"],
)
def test_the_conda_package_selection_is_empty_until_the_surfaces_are_declared(
    channels: object,
    platforms: object,
) -> None:
    """The shipped state, and the one selection that had to be narrowed rather than widened.

    Both settings ship empty (PRD Open Question 4), and an undeclared component
    refuses *every* package equally -- so a selection that offered the inventory
    anyway would have the scheduled sweep write one `failed` collection per
    package per day out of the box, which is exactly the "ledger fills with failed
    runs" shape the other three selections exist to prevent. An empty selection
    records one `succeeded` dispatch saying nothing was selectable instead.

    A mistyped declaration is here too: `CPM_MONITORED_CHANNELS = "conda-forge"`
    is eleven one-character channels to Python, and it is refused rather than
    swept, on the same rule `collectors/apps.py` refuses the shape with.
    """
    with override_settings(**{CHANNELS_SETTING: channels, PLATFORMS_SETTING: platforms}):
        selection = CondaPackageCollector.selectable_packages()

        assert list(selection) == []
        assert selection.model is Package


@pytest.mark.parametrize(
    ("collector", "name", "cadence"),
    PER_PACKAGE_COLLECTORS,
    ids=lambda value: getattr(value, "__name__", str(value)),
)
def test_every_per_package_selection_is_lazy_rather_than_a_list(
    collector: type[Collector],
    name: str,
    cadence: timedelta,
) -> None:
    """AC 4 from the collector's side: nothing is read until the dispatch streams it.

    A `selectable_packages` that returned `list(queryset)` would satisfy every
    behavioural case in the integration suite and would put ten thousand primary
    keys in memory before the first task was enqueued. Nothing having been read
    yet is the property, and it is asserted for both shapes a lazy selection
    takes.

    **Seven of the nine answer with a queryset and two may answer with a
    generator.** The two collectors that read a *declared adapter* return an empty
    *generator* when their own source is not declared, so that the warning naming
    the missing source is emitted where a dispatch draws the selection rather than
    where a start-up reconciliation merely asks whether there is one. The licence
    collector is not one of them: it declares no adapter, so an undeclared
    component is an empty *queryset* on the terms `CondaPackageCollector` answers
    with one. A generator is at least as
    lazy as a queryset -- it has read nothing and holds nothing -- so what is
    asserted is the shared property first, and the queryset's own empty result
    cache where there is a queryset to ask.
    """
    assert cadence > NO_CADENCE
    assert collector.name == name

    selection = collector.selectable_packages()

    assert not isinstance(selection, (list, tuple, set, frozenset)), (
        f"{collector.__name__} answers with a materialised {type(selection).__name__}, which reads the whole "
        f"inventory before the first task is enqueued"
    )
    if isinstance(selection, QuerySet):
        assert selection._result_cache is None  # noqa: SLF001 - the private cache is the property under test
        assert selection.query.values_select or selection.query.default_cols is False
    else:
        assert iter(selection) is selection, (
            f"{collector.__name__} answers with a {type(selection).__name__}, which is neither a queryset nor an "
            f"iterator, so collectors/sweep.py refuses it"
        )


# ---------------------------------------------------------------------------
# The refusals' shapes, and the report.
# ---------------------------------------------------------------------------


def test_a_dispatch_refusal_is_a_value_error() -> None:
    """Every "this declaration is unusable" in this product is a `ValueError`.

    So a caller catching `CollectorConfigurationError`, `CollectorRegistryError`
    or one of the four locator errors catches this one on the same terms.
    """
    assert issubclass(SweepDispatchError, ValueError)


def test_the_dispatch_report_is_frozen() -> None:
    """A report rather than a workspace, on the terms `CollectionResult` is."""
    enqueued = 3
    outcome = DispatchOutcome(state=RunState.SUCCEEDED, enqueued=enqueued, refused=0, detail="a detail")

    with pytest.raises(FrozenInstanceError):
        outcome.enqueued = 1  # type: ignore[misc]

    assert outcome.state is RunState.SUCCEEDED
    assert outcome.enqueued == enqueued


def test_the_package_keyword_is_the_one_every_collection_task_takes() -> None:
    """`PACKAGE_KWARG` is what the dispatch enqueues with; this is what the four accept.

    Asserted against the real task functions, so a task whose signature grew a
    positional parameter -- or lost the keyword -- fails here rather than
    producing ten thousand `TypeError`s in a worker.
    """
    from conda_sentinel.collectors.tasks import collect_conda_package  # noqa: PLC0415
    from conda_sentinel.collectors.tasks import collect_feedstock  # noqa: PLC0415
    from conda_sentinel.collectors.tasks import collect_pypi_release  # noqa: PLC0415
    from conda_sentinel.collectors.tasks import collect_source_release  # noqa: PLC0415

    for task in (collect_source_release, collect_pypi_release, collect_feedstock, collect_conda_package):
        parameters = inspect.signature(task.run).parameters
        assert PACKAGE_KWARG in parameters
        assert parameters[PACKAGE_KWARG].kind is inspect.Parameter.KEYWORD_ONLY


# ---------------------------------------------------------------------------
# The module's own source sweeps.
# ---------------------------------------------------------------------------


def test_the_dispatch_module_is_in_view_of_the_scan_the_repository_shares() -> None:
    """The anti-vacuity guard: the file this story wrote is one the audits reach.

    A scan that had stopped reaching it -- an exclusion widened, a walk that lost
    a directory -- would report a clean repository and prove nothing.
    """
    scanned = {path.resolve() for path in project_files(SWEEP_MODULE.parents[3], skip_migrations=True)}

    assert SWEEP_MODULE.resolve() in scanned


def test_the_dispatch_writes_no_evidence_and_opens_no_transaction() -> None:
    """ "A dispatch never collects" as a property of the source rather than of a docstring.

    Three checks, and each is a different rule. `FORBIDDEN_CALLS` is a call this
    module may not make: `atomic` would be a transaction held across packages,
    which `CPM-AD-23` forbids outright; the writers would be a second
    evidence writer beside the base's (`CPM-AD-7`); and `fetch` would be a call
    outside the transport seam (`CPM-AD-27`). `transaction` is checked as a *name*
    as well, because `transaction.atomic` reached through an alias would not be an
    attribute call this scan recognises. And no import of `core/transport.py` is
    permitted at all -- a dispatch that acquired a body would be a collector,
    which is the one thing this module says it is not.
    """
    tree = ast.parse(SWEEP_MODULE.read_text(encoding="utf-8"))

    called = {
        node.func.attr for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    named = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    imported = {
        f"{node.module}.{alias.name}"
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
        for alias in node.names
    }

    assert not called & FORBIDDEN_CALLS, called & FORBIDDEN_CALLS
    assert "transaction" not in named
    assert not {name for name in imported if name.endswith(".transaction")}
    # The transport is not reached either, and `Payload` is not imported: a
    # dispatch that acquired a body would be a collector, which is the one thing
    # this module says it is not.
    assert not {name for name in imported if "transport" in name}


def test_the_dispatch_names_no_collector_module() -> None:
    """The selection is the collector's, and this module holds no table of preconditions.

    `CPM-AD-7`'s no-collector-imports-another rule, applied to the module that
    dispatches all of them: a dispatch that imported `source_release` to ask what
    it can be asked about would be the second place to edit whenever a
    collector's refusals changed, which is exactly what putting the hook on the
    collector avoids.
    """
    source = SWEEP_MODULE.read_text(encoding="utf-8")
    tree = ast.parse(source)

    imported = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module is not None}

    assert not {name for name in imported if ".collectors." in name}


# ---------------------------------------------------------------------------
# `cadence_reconciliation_fault`, the rule both boot points ask.
# ---------------------------------------------------------------------------


def _sweepable(
    *,
    name: str = "cpm-fixture-sweepable",
    cadence: object = FIXTURE_CADENCE,
    freshness_target: timedelta | None = FIXTURE_FRESHNESS_TARGET,
    selection: object = (),
) -> type[Collector]:
    """Build a fixture collector with exactly the declarations one case is about.

    Args:
        name: The declared name, which the schedule entry has to match.
        cadence: The declared cadence, or `None` for a collector nothing sweeps.
        freshness_target: The declared target.
        selection: What `selectable_packages` answers, or `None` for a
            run-scoped collector.

    Returns:
        A concrete `Collector` subclass, unconstructed.

    """
    return selectable_collector_class(
        declared_model=fixture_evidence_model(),
        declared_name=name,
        declared_cadence=cadence,  # type: ignore[arg-type]
        declared_freshness_target=freshness_target,
        selection=selection,  # type: ignore[arg-type]
    )


def _entry(name: str, schedule: object, *, task: str = SWEEP_TASK_NAME, kwargs: object = None) -> dict[str, object]:
    """Return one `CELERY_BEAT_SCHEDULE` entry.

    Args:
        name: The collector the entry dispatches.
        schedule: What the entry declares under `schedule`.
        task: The task it fires, so a case can build one that is not a dispatch.
        kwargs: What it passes, or `None` for the ordinary `{collector: name}`.

    Returns:
        The entry, in the shape celery reads.

    """
    return {
        "task": task,
        "schedule": schedule,
        "kwargs": {COLLECTOR_KWARG: name} if kwargs is None else kwargs,
    }


def test_a_collector_and_its_entry_that_agree_produce_no_fault() -> None:
    """The positive control, without which every refusal below proves nothing."""
    declared = _sweepable()
    schedule = {"an-entry": _entry(declared.name, FIXTURE_CADENCE)}

    assert cadence_reconciliation_fault([declared], schedule) == ""


def test_an_empty_roster_reconciles_with_any_schedule() -> None:
    """The guard a deployed stage two depends on.

    `config/startup/stage_two.py` runs before the collectors application's
    `ready()`, so it meets a populated schedule and an empty registry on every
    real boot. A backward direction that fired there would refuse every deployed
    process over four entries naming collectors nothing had registered *yet*.
    """
    schedule = {"an-entry": _entry("nobody", FIXTURE_CADENCE)}

    assert cadence_reconciliation_fault([], schedule) == ""


def test_a_bare_number_of_seconds_is_read_as_the_interval_it_is() -> None:
    """Celery accepts three schedule shapes and two of them are the same statement.

    A `timedelta` and a number of seconds say the identical thing, so a component
    that spelled its entry the second way must not be told its schedule
    "disagrees" with a cadence it matches exactly.
    """
    declared = _sweepable()
    schedule = {"an-entry": _entry(declared.name, FIXTURE_CADENCE.total_seconds())}

    assert cadence_reconciliation_fault([declared], schedule) == ""


def test_a_schedule_this_reconciliation_cannot_read_says_so_rather_than_claiming_a_mismatch() -> None:
    """A crontab is a calendar, and a calendar cannot be compared against an interval.

    The distinction is the whole finding: reporting "these two disagree" about a
    `crontab(hour=2)` and a one-day cadence would be a claim nobody can act on --
    they may agree exactly. What the message says instead is that they cannot be
    compared, and what to do about it.
    """
    declared = _sweepable()
    schedule = {"an-entry": _entry(declared.name, crontab(hour=2, minute=0))}

    fault = cadence_reconciliation_fault([declared], schedule)

    assert "cannot read" in fault
    assert "not a claim that the two disagree" in fault
    assert "crontab" in fault
    # The negative control: the mismatch message this one must not be, asserted by
    # its own distinguishing sentence rather than by the word "disagree", which
    # the explanation above legitimately contains.
    assert "fires every" not in fault


@pytest.mark.parametrize("declared", [True, False], ids=["true", "false"])
def test_a_boolean_schedule_is_not_a_number_of_seconds(*, declared: bool) -> None:
    """`bool` is a subclass of `int`, so `True` would otherwise be a one-second cadence."""
    assert _as_interval(declared) is None


def test_a_schedule_that_is_not_a_mapping_is_refused_by_name() -> None:
    """`CELERY_BEAT_SCHEDULE = [...]` meets this condition rather than an `AttributeError`.

    A boot hook that died with an attribute error would say nothing about which
    setting was wrong or what shape it should have, which is the difference every
    other refusal in this product is written for.
    """
    declared = _sweepable()

    fault = cadence_reconciliation_fault([declared], ["not", "a", "mapping"])

    assert "CELERY_BEAT_SCHEDULE is list" in fault


def test_an_entry_that_is_not_a_mapping_is_skipped_rather_than_read() -> None:
    """One malformed entry does not stop the others being reconciled."""
    declared = _sweepable()
    schedule = {"malformed": "not an entry", "an-entry": _entry(declared.name, FIXTURE_CADENCE)}

    assert cadence_reconciliation_fault([declared], schedule) == ""


@pytest.mark.parametrize(
    "kwargs",
    [None, {}, {"something": "else"}, "not a mapping", {COLLECTOR_KWARG: "   "}],
    ids=["absent", "empty", "other-keyword", "not-a-mapping", "blank"],
)
def test_a_dispatch_entry_that_names_no_collector_is_refused_as_such(kwargs: object) -> None:
    """The entry a `CELERY_BEAT_SCHEDULE` edit is most likely to produce.

    An entry firing the dispatch and passing no collector fails on every tick,
    with a ledger row saying the name is blank -- and the reconciliation must say
    *that* rather than reporting it as an entry naming a collector called `''`,
    which is a message about a name nobody wrote.
    """
    declared = _sweepable()
    entry = _entry(declared.name, FIXTURE_CADENCE)
    if kwargs is not None:
        entry["kwargs"] = kwargs
    else:
        del entry["kwargs"]
    schedule = {"nameless": entry, "an-entry": _entry(declared.name, FIXTURE_CADENCE)}

    fault = cadence_reconciliation_fault([declared], schedule)

    assert "name no collector" in fault


def test_two_entries_naming_one_collector_are_refused() -> None:
    """The duplicate branch, which would otherwise sweep the inventory twice a cadence.

    A mapping keyed by collector name would have kept the last of the two and
    reported nothing, which is why the reconciliation collects a list per name.
    """
    declared = _sweepable()
    schedule = {
        "first": _entry(declared.name, FIXTURE_CADENCE),
        "second": _entry(declared.name, FIXTURE_CADENCE),
    }

    fault = cadence_reconciliation_fault([declared], schedule)

    assert "2 entry(ies)" in fault


def test_an_entry_firing_another_task_is_not_this_reconciliations_business() -> None:
    """`CELERY_BEAT_SCHEDULE` is one dictionary and this product will schedule more than sweeps."""
    declared = _sweepable()
    schedule = {
        "an-entry": _entry(declared.name, FIXTURE_CADENCE),
        "elsewhere": _entry("not-a-collector", FIXTURE_CADENCE, task="cpm.policy.currency"),
    }

    assert cadence_reconciliation_fault([declared], schedule) == ""


def test_a_collector_declaring_a_cadence_and_no_selection_is_refused() -> None:
    """It would be scheduled and refused by the dispatch on every tick.

    The pair is the declaration: a run-scoped collector (`CPM-AD-25`) declares
    neither, and one that declares only the cadence has a beat entry firing at a
    dispatch that cannot serve it -- a `failed` ledger row a day, for ever.
    """
    declared = _sweepable(selection=None)
    schedule = {"an-entry": _entry(declared.name, FIXTURE_CADENCE)}

    fault = cadence_reconciliation_fault([declared], schedule)

    assert "no selectable_packages" in fault


def test_a_collector_declaring_a_selection_and_no_cadence_is_refused() -> None:
    """It is never swept, and every surface reads its evidence as ageing normally.

    The mirror of the case above, and the quieter of the two: nothing fails, and
    the collector's evidence is simply never collected while its freshness target
    goes on being compared against.
    """
    declared = _sweepable(cadence=None)

    fault = cadence_reconciliation_fault([declared], {})

    assert "nothing sweeps it" in fault


def test_a_collector_declaring_neither_is_not_this_reconciliations_business() -> None:
    """Inventory ingestion's shape: run-scoped, and correct."""
    declared = _sweepable(cadence=None, selection=None)

    assert cadence_reconciliation_fault([declared], {}) == ""


def test_a_collector_registered_under_the_dispatch_tasks_own_name_is_refused() -> None:
    """`collection_task_name("sweep")` is the dispatch itself.

    Dispatching such a collector would enqueue this task once per package, and
    every one of those would do the same -- an exponential fan-out from one beat
    tick. Refused at boot as well as at dispatch, because the boot refusal is the
    one that fires before a schedule exists to trip it.
    """
    declared = _sweepable(name=RESERVED_COLLECTOR_NAME)

    fault = cadence_reconciliation_fault([declared], {})

    assert "reserved" in fault or "own last segment" in fault
    assert collection_task_name(RESERVED_COLLECTOR_NAME) == SWEEP_TASK_NAME


def test_every_disagreement_is_reported_in_one_message() -> None:
    """One boot tells an operator about all of them, not the first one alphabetically.

    The same argument condition 10's own sweep is written for: fixing one and
    redeploying to meet the next makes each look like a fresh problem.
    """
    first = _sweepable(name="cpm-fixture-one")
    second = _sweepable(name="cpm-fixture-two")
    schedule = {"only-one": _entry(first.name, FIXTURE_CADENCE * 7)}

    fault = cadence_reconciliation_fault([first, second], schedule)

    assert "cpm-fixture-one" in fault
    assert "cpm-fixture-two" in fault
    assert fault.startswith("2 cadence disagreement(s)")


def test_the_scheduled_dispatches_reader_returns_what_it_could_and_counts_what_it_could_not() -> None:
    """The reader's own contract, asserted where the composed message cannot show it."""
    schedule = {
        "named": _entry("alpha", FIXTURE_CADENCE),
        "nameless": _entry("beta", FIXTURE_CADENCE, kwargs={}),
        "elsewhere": _entry("gamma", FIXTURE_CADENCE, task="cpm.policy.currency"),
    }

    by_collector, unnamed = _scheduled_dispatches(schedule)

    assert by_collector == {"alpha": [FIXTURE_CADENCE]}
    assert unnamed == 1
