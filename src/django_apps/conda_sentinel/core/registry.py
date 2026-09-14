"""The collectors this process knows about, declared one by one and never discovered.

`CPM-AD-28` puts a refusal at boot: a *registered* collector that declares no
freshness target stops the process. That sentence needs something to sweep, and
this is it -- the list of collector classes a component has adopted, which
`config/startup/stage_two.py` walks before a worker picks up any work.

**Declared, never discovered** (inherited `AD-8`). There is no entry-point scan
and no module walk: adoption in this product is already two explicit lines, and
a registry populated by import side effects would make "which collectors does
this component run" a question answered by whatever happened to be imported. A
collector arrives here because somebody wrote `register(TheCollector)` in an
`AppConfig.ready()`, where a reader can see it.

**It is not in `core/collection.py`, and that is the whole reason this module
exists.** `collection.py` is imported by anything that defines a collector --
every one of the eight, plus the fixtures that measure the base -- so a registry
living there would mean that importing the base populates a global. Keeping it
separate lets startup sweep a registry without the base importing one, and lets
a test build a collector class without that class becoming something boot will
refuse over.

**A duplicate name is refused rather than overwritten.** The name is what ledger
rows carry (`CPM-FR-39`) and what rate-limit cache keys are built from
(`core/rate_limit.py`), so two classes registered under one name share an
allowance, share a run history and are indistinguishable in every report --
while the second one silently replaces the first in whatever this module
returns. `core/collection.py` already refuses a blank name for the same class of
reason; this is the other half of making a name identify something.

**`unregister` exists because registration is process-global.** A registry that
can only grow cannot be measured: the refusal `CPM-AD-28` asks for is
"a registered collector declaring no target", so a case has to be able to put
one there and take it away again, and a case that left one behind would refuse
every case that ran after it. It is symmetric with `register` rather than a test
hook bolted on, and nothing on a product path calls it.

**On the `AD-` prefix.** A bare `AD-n` in this repository is an *inherited*
platform decision; a decision from this product's own architecture spine always
carries the `CPM-` prefix. `AD-8` above is the platform's declared-not-discovered
rule and `CPM-AD-28` is this product's freshness refusal -- two registers, not a
typo.
"""

from __future__ import annotations

from types import GeneratorType
from typing import TYPE_CHECKING
from typing import Any
from typing import Final

import structlog
from django.db.models import QuerySet

from conda_sentinel.core.collection import Collector

if TYPE_CHECKING:
    from collections.abc import Iterable
    from collections.abc import Mapping

__all__ = [
    "PACKAGE_COLUMN",
    "PACKAGE_MODEL_LABEL",
    "SELECTION_NOT_ASKED_EVENT",
    "CollectorRegistryError",
    "register",
    "registered_collectors",
    "registrations",
    "selected_package_ids",
    "selects",
    "swept_collectors",
    "unregister",
]

#: The label of the model a selection over packages themselves is a queryset of.
#:
#: A label rather than the class: `core` may not import `identity`'s models
#: outside the four modules `tests/unit/django_apps/test_app_layering_audit.py`
#: records, and `core/models.py` names the same relation the same way
#: (`"identity.Package"`) for the same reason. `selects` below reads it off the
#: queryset's own `_meta` to decide which column names the package.
PACKAGE_MODEL_LABEL: Final[str] = "identity.Package"

#: The column a selection over any other table names the package by: the
#: `attname` Django gives a `ForeignKey` called `package`.
PACKAGE_COLUMN: Final[str] = "package_id"

#: What `selects` logs when it answers `False` without asking the selection:
#: a generator it will not iterate, or items that are not package keys.
SELECTION_NOT_ASKED_EVENT: Final[str] = "registry.selection_not_asked"

logger: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

#: The registered collector classes, by the name each declares.
#:
#: A module-level mapping rather than a rebound global, for the reason
#: `config/startup/stage_two.py`'s boot sentinel is one: ruff `PLW0603` forbids
#: the `global` statement, and a `from ... import` of a rebound name would bind a
#: copy that never observes a later write. Read through `registrations()` rather
#: than directly, so no caller performs the cross-module private read `SLF001`
#: forbids.
_REGISTERED: Final[dict[str, type[Collector]]] = {}


class CollectorRegistryError(ValueError):
    """A collector could not be added to, or removed from, the registry.

    A `ValueError` subclass, matching `core/collection.py`'s
    `CollectorConfigurationError` and `core/freshness.py`'s `FreshnessError`:
    every "this declaration is unusable" in this product is a `ValueError`, so a
    caller catching one catches them all.

    Raised at registration, which is the moment the answer exists and is
    reachable at import time in the `AppConfig.ready()` that performs it -- not
    at the boot sweep, and certainly not in a worker that has already opened a
    run ledger row.
    """


def register(collector: type[Collector]) -> type[Collector]:
    """Adopt one collector class, under the name it declares.

    Args:
        collector: The collector class to adopt. A class, not an instance: the
            sweep at boot asks what a collector *declares*, and constructing one
            to find out would build a transport and a connection pool during
            `django.setup()`.

    Returns:
        The class, unchanged, so the call can be written as a decorator on the
        class it adopts where that reads better than a separate line.

    Raises:
        CollectorRegistryError: When the argument is not a `Collector` subclass;
            when it declares no name -- `core/collection.py` refuses a blank one
            at construction and a registry keyed on it cannot accept one either;
            or when another class is already registered under that name.

    """
    if not (isinstance(collector, type) and issubclass(collector, Collector)):
        message = (
            f"{collector!r} is not a Collector subclass and cannot be registered. Every external-call rule "
            f"this product has lives in that base (CPM-AD-20, CPM-AD-27), so a registered class that does "
            f"not inherit it carries none of them."
        )
        raise CollectorRegistryError(message)

    name = collector.name
    if not isinstance(name, str) or not name.strip():
        message = (
            f"{collector.__name__} declares name={name!r} and cannot be registered under it. The name is what "
            f"a run is traced to (CPM-FR-39) and what its rate-limit allowance is keyed on; a blank one names "
            f"nothing."
        )
        raise CollectorRegistryError(message)

    existing = _REGISTERED.get(name)
    if existing is not None:
        message = (
            f"{collector.__name__} declares name={name!r}, which {existing.__name__} is already registered "
            f"under. Two collectors sharing a name share one rate-limit allowance and one run history, and "
            f"neither can be told from the other in any report."
        )
        raise CollectorRegistryError(message)

    _REGISTERED[name] = collector
    return collector


def unregister(name: str) -> None:
    """Withdraw the collector registered under one name.

    Args:
        name: The declared name the class was registered under.

    Raises:
        CollectorRegistryError: When nothing is registered under that name.
            Refused rather than ignored, because a silent no-op turns a
            misspelled withdrawal into a registration that stays live and a
            caller that believes it does not.

    """
    if name not in _REGISTERED:
        message = (
            f"no collector is registered under name={name!r}, so there is nothing to withdraw. "
            f"The registered names are {sorted(_REGISTERED)}."
        )
        raise CollectorRegistryError(message)
    del _REGISTERED[name]


def registered_collectors() -> tuple[type[Collector], ...]:
    """Return every registered collector class, in a fixed order.

    Returns:
        The classes, ordered by declared name. Ordered rather than in insertion
        order so that a component whose `AppConfig.ready()` adopts collectors in
        a different sequence meets the same refusal first -- which is the
        property `AD-26` asks of the stage-2 roster, applied to what that roster
        sweeps.

        Empty until `CPM-EP-CURRENCY` declares the first collector, and an empty
        sweep is not a failure: a component with no collectors is a component
        that has adopted none, not one that has forgotten a target.

    """
    return tuple(_REGISTERED[name] for name in sorted(_REGISTERED))


def swept_collectors() -> tuple[type[Collector], ...]:
    """Return every registered collector that is swept one package at a time, in registry order.

    The predicate `collectors/sweep.py` applies before it dispatches, applied to
    the whole registry: a `selectable_packages()` that answers `None` says the
    collector is not swept per package (`CPM-AD-25`), and everything else is.
    Declared here rather than in the `dispatch_sweep` command, where
    `CPM-OPERATE-S02` first wrote it, because `CPM-OPERATE-S08`'s recollection
    asks the same question from the web process and may import neither the
    command nor the dispatcher.

    Returns:
        The registered classes whose selection is not `None`, ordered as
        `registered_collectors()` orders them. A collector this leaves out is
        one the dispatch would refuse by name.

    """
    return tuple(collector for collector in registered_collectors() if collector.selectable_packages() is not None)


def selects(collector: type[Collector], package_id: int) -> bool:
    """Report whether one collector's own selection contains one package.

    The question `CPM-OPERATE-S08`'s "Collect now" asks before it publishes: a
    forced collection of a collector that cannot ask about the package writes a
    `failed` run with a refusal on the record, so only the collectors whose
    `selectable_packages()` holds the package are offered.

    **The selection is asked, never re-derived.** Each collector declares its
    precondition beside its refusals, and restating any of them here would be
    the second table that hook's docstring exists to prevent. What this function
    knows is only the *shapes* a selection takes -- the same three
    `selected_package_ids` reads whole -- and it answers each without reading
    ten thousand keys:

    * A lazy queryset over `identity.Package` (`pk` names the package) or over a
      table that references one (`package_id` does) is narrowed by a filter and
      asked whether a row exists. A queryset this cannot narrow honestly -- one
      already sliced, one combined with `union`/`intersection`/`difference`, or
      one over a model with neither the package label nor a `package_id` column
      -- is refused with `CollectorRegistryError` rather than answered by a
      `FieldError` out of a request.
    * A generator is answered `False` without being iterated. The two the
      product declares are the empty ones an undeclared advisory or KEV source
      answers with, and each says why *on first use*; iterating one here would
      emit that warning on every press. A collector that wants to be offered
      from the page answers a queryset or a sequence, never a generator.
    * Any other iterable is materialised and searched, provided every item is
      an `int`; an item of another shape -- a tuple, a model instance -- means
      the selection is not one of package keys, and it is answered `False`
      with a logged event rather than by a coincidental `==`.

    Args:
        collector: The registered class.
        package_id: The package, by the integer primary key `CPM-AD-3` fixes.

    Returns:
        True when the selection contains the package. False when it does not,
        when the collector is not swept per package at all -- `None` is not a
        selection, and a collector that is never dispatched is never offered --
        or when the selection is a generator or carries items that are not keys.

    Raises:
        CollectorRegistryError: When the selection is a queryset of a shape
            this cannot narrow. See above.

    """
    selection = collector.selectable_packages()
    if selection is None:
        return False
    if isinstance(selection, QuerySet):
        column = _package_column(collector, selection)
        return bool(selection.filter(**{column: package_id}).exists())
    keys = _package_keys(collector, selection, package_id=package_id)
    return keys is not None and package_id in keys


def selected_package_ids(
    collector: type[Collector], *, selection: Iterable[int] | None = None
) -> frozenset[int] | None:
    """Return one collector's whole selection as package keys, or `None` for a shape this cannot read.

    The operator digest's question (`CPM-OPERATE-S09`): which packages can this
    collector be asked about, so that the ones its evidence table has not
    observed inside its freshness target can be counted. The same three shapes
    `selects` narrows, read whole:

    * A queryset over `identity.Package` or over a table with a `package_id`
      column is read by that column. A sliced or combined queryset is refused
      exactly as `selects` refuses it.
    * A generator is not iterated -- see `selects` -- and answers `None` with
      `SELECTION_NOT_ASKED_EVENT` logged once, so the figure is absent rather
      than a zero that reads as fresh.
    * Any other iterable is materialised, provided every item is an `int`.

    Args:
        collector: The registered class.
        selection: What `selectable_packages()` answered, when the caller has
            already asked -- the digest asks once per collector and reads the
            per-package/run-scoped decision off the same answer. `None` means
            ask here.

    Returns:
        The keys, or `None` when the collector is not swept per package or the
        selection is a shape this cannot read as keys.

    Raises:
        CollectorRegistryError: When the selection is a queryset of a shape
            this cannot read. See `selects`.

    """
    answered = collector.selectable_packages() if selection is None else selection
    if answered is None:
        return None
    if isinstance(answered, QuerySet):
        column = _package_column(collector, answered)
        return frozenset(int(key) for key in answered.values_list(column, flat=True))
    return _package_keys(collector, answered)


def _package_column(collector: type[Collector], selection: QuerySet[Any]) -> str:
    """Return which column of a queryset selection names the package, refusing a shape that cannot be narrowed.

    Args:
        collector: The class, for the refusal's wording.
        selection: The queryset it answered.

    Returns:
        `pk` for a queryset over `identity.Package`, `package_id` for one over
        a table that references it.

    Raises:
        CollectorRegistryError: For a sliced or combined queryset, or one over a
            model that names a package neither by label nor by `package_id`.

    """
    query = selection.query
    if query.is_sliced or query.combinator:
        message = (
            f"{collector.__name__} (name={collector.name!r}) answered a selection that is "
            f"{'sliced' if query.is_sliced else 'combined with ' + str(query.combinator)}, which cannot be "
            f"narrowed to one package: a filter on a sliced or combined queryset is refused by Django, and "
            f"materialising it would read the whole inventory. A selection is a lazy, unsliced queryset "
            f"(core/collection.py, selectable_packages)."
        )
        raise CollectorRegistryError(message)
    options = selection.model._meta  # noqa: SLF001 - `_meta` is Django's own public-by-convention API
    if options.label == PACKAGE_MODEL_LABEL:
        return "pk"
    if any(field.attname == PACKAGE_COLUMN for field in options.concrete_fields):
        return PACKAGE_COLUMN
    message = (
        f"{collector.__name__} (name={collector.name!r}) answered a selection over {options.label}, which "
        f"is neither {PACKAGE_MODEL_LABEL} nor a table with a {PACKAGE_COLUMN!r} column, so nothing here "
        f"knows which column names the package."
    )
    raise CollectorRegistryError(message)


def _package_keys(
    collector: type[Collector], selection: Iterable[int], *, package_id: int | None = None
) -> frozenset[int] | None:
    """Return a non-queryset selection as package keys, or `None` with the reason logged.

    Args:
        collector: The class, for the log line.
        selection: A generator, or any other iterable.
        package_id: The package a `selects` call was asking about, for the log
            line; `None` when the whole selection is being read.

    Returns:
        The keys, or `None` for a generator or for items that are not keys.

    """
    if isinstance(selection, GeneratorType):
        logger.info(SELECTION_NOT_ASKED_EVENT, collector=collector.name, reason="generator", package_id=package_id)
        return None
    items = list(selection)
    if not all(isinstance(item, int) and not isinstance(item, bool) for item in items):
        logger.warning(
            SELECTION_NOT_ASKED_EVENT,
            collector=collector.name,
            reason="items_are_not_keys",
            package_id=package_id,
            sample=repr(items[:3]),
        )
        return None
    return frozenset(items)


def registrations() -> Mapping[str, type[Collector]]:
    """Return what is registered, by name.

    Returns:
        A copy, so a caller cannot widen or empty the registry by mutating what
        it was handed -- the same reason `core/collection.py` returns declared
        headers as a read-only mapping.

    """
    return dict(_REGISTERED)
