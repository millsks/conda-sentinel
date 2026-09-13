"""The full-inventory dispatch: one collector, its selectable packages, one task each.

`CPM-NFR-1` asks for "full-inventory collection at 10,000 packages ... without
manual batching" and `CPM-FR-15` for "a collector failure leaves every other
collector's run unaffected and the overall run reported as partial success, not
failure". **Eleven collectors are registered and nine of them are swept one
package at a time** -- inventory ingestion is run-scoped and reads one document
naming many packages (`CPM-AD-25`), so a dispatch refuses it by name, and Python
3.14 verification is triggered rather than swept and declares no cadence. Those
nine are what this module runs, and it is deliberately the smallest thing that
can: **a dispatch never collects.**

Wherever a count appears below, "registered" means the eleven `collectors/apps.py`
adopts and "swept" means the nine that declare a cadence and a selection.
`tests/integration/startup/test_stage_two_collector_registry.py` asserts both
rosters.

**Why a dispatch rather than a sweep.** `core/collection.py` already has a
run-scoped `sweep()` for the one collector that reads a document naming many
packages. The nine swept here read one locator *per package*, so their sweep is
not a
bigger read -- it is *many runs*. Doing it inside one task would put ten thousand
collections under one soft time limit and one ledger row, which is exactly what
`CPM-AD-23` forbids ("a collector task never holds a transaction across
packages") and what makes `CPM-FR-15`'s partial success unreachable. Fanning out
to the per-package tasks that already exist keeps every guarantee the four
collector stories built, unchanged, and makes the two acceptance criteria
structural rather than defended:

* **One collector failing never stops another.** There is one dispatch per
  collector and they share nothing but the ledger table. A dispatch that raises
  finalizes its own row `failed` and no other dispatch is in its call stack.
* **A package failing never rolls back an earlier package's evidence.** Each
  package is its own task, its own run-ledger row and its own transaction,
  because that is what `Collector.collect` already is.

**What `partial` means here, and it is not what it means on a collection.** On a
dispatch row, `succeeded` means every selected package was enqueued, `partial`
means some were and some were not, and `failed` means none were. It is a
statement about *enqueueing*, never about what those packages then observed --
the collections have not run yet when this row is finalized, and a dispatch that
waited for them would be the retry storm this story forbids. A sweep in which
every package was enqueued and every collection then failed leaves one
`succeeded` dispatch row above ten thousand `failed` collection rows, and that is
the honest shape: the dispatch did its whole job.

**An empty selection is a success.** A collector whose selection matches no
package has been asked and has answered; recording that as a failure would make a
component with an empty inventory indistinguishable from one whose broker is
down.

**Nothing here holds the inventory.** `Collector.selectable_packages` answers
with a lazy queryset and this module reads it through `.iterator()`, so the rows
are fetched from the database `SELECTION_CHUNK` at a time and no list of ten
thousand primary keys ever exists. A hook that answered with a materialised
sequence is refused rather than accepted, because it would defeat that silently.

**Three things bound a dispatch, and none of them is a retry.** The soft time
limit is *handled* rather than caught by accident -- `SoftTimeLimitExceeded` ends
the run `partial` with an honest reason instead of being counted as one more
broker refusal. Every enqueued task carries `expires` set to the collector's own
cadence, so a message that has not been consumed by the time the next tick fires
is dropped rather than queued behind it. And a dispatch whose previous run for the
same collector is still `running` records `skipped` rather than enqueueing a
second inventory on top of the first. Together those are what stop a source that
cannot be drained inside its cadence from accumulating an unbounded queue.

**Which packages a collector can be asked about is the collector's own
knowledge.** Each of the nine swept collectors already refuses, from `source_for`,
the packages it cannot answer about -- or, for the two security collectors', offers
the whole inventory because each owes every package a row either way -- and each
recorded that as a deferred item saying the selection belonged here. It is
declared on the collector all the same (`Collector.selectable_packages`), because
the set a collector can answer about is the complement of its own refusals: a
table of nine preconditions in *this* module would be a second place to edit
whenever a collector's refusals changed,
and the two would diverge silently. What this module owns is the dispatch, not
the predicate.

**The task name is derived, never configured.** `cpm.collect.<name>` is the
namespace `core/queues.py` routes to the `collect` queue, and every per-package
collection task in this product is already spelled that way. Deriving it means a
dispatch cannot be pointed at a task that is not this collector's; refusing a
derived name Celery does not hold means a dispatch never enqueues into a name
nothing consumes, which is silent non-delivery rather than an error.

**The cadence reconciliation lives here too, and it is here rather than in
`config/startup/` because both of its callers need it.**
`cadence_reconciliation_fault` is the whole of `CPM-AD-20`'s rule as a pure
function over the registered classes and a schedule declaration, so
`collectors/apps.py` can enforce it in the `ready()` that *populates* the
registry -- which is the only hook that runs after registration in a deployed
process -- while `config/startup/stage_two.py` keeps evaluating it as condition
11. Inherited `AD-4` forbids anything under `src/django_apps/` importing
`config`, so a rule owned by the startup package could not have been called from
an application's `ready()` at all.

**Day one is observed when the operator says so (`CPM-OPERATE-S04`).** Beat's
interval entries start their clock when they are created, so a fresh component
fires nothing for a day. `dispatch_scheduled_collectors_on_start` is what the
`beat_init` receiver in `config/celery_app.py` calls when
`CPM_SWEEP_ON_BEAT_START` is on: one enqueue of the dispatch task per scheduled
collector, in the schedule's own order and with the schedule's own countdown,
touching neither the entries nor the ledger. It lives here because it reads the
schedule through the same walk the reconciliation does, and because it is a
dispatch of dispatches -- what beat's first tick would be, brought forward.

**On the `AD-` prefix.** A bare `AD-n` in this repository is an *inherited*
platform decision; a decision from this product's own architecture spine always
carries the `CPM-` prefix.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING
from typing import Any
from typing import Final

import structlog
from celery import current_app
from celery.exceptions import SoftTimeLimitExceeded
from django.db.models import QuerySet
from kombu.exceptions import OperationalError

from conda_sentinel.core.collection import CollectorConfigurationError
from conda_sentinel.core.collection import require_cadence
from conda_sentinel.core.ledger import collection_run
from conda_sentinel.core.models import CollectionRun
from conda_sentinel.core.operator_commands import runs_eagerly
from conda_sentinel.core.queues import NAME_SEPARATOR
from conda_sentinel.core.queues import TASK_NAMESPACE_PREFIX
from conda_sentinel.core.queues import Queue
from conda_sentinel.core.registry import registrations
from conda_sentinel.core.runs import RunState

if TYPE_CHECKING:
    from collections.abc import Iterable
    from collections.abc import Iterator
    from collections.abc import Mapping
    from collections.abc import Sequence

    from conda_sentinel.core.clock import Clock
    from conda_sentinel.core.collection import Collector
    from conda_sentinel.core.ledger import RunHandle

__all__ = [
    "COLLECTOR_KWARG",
    "EVENT_KEYS",
    "PACKAGE_EVENT_KEYS",
    "PACKAGE_KWARG",
    "SELECTION_CHUNK",
    "SWEEP_DISPATCHED_EVENT",
    "SWEEP_ON_BEAT_START_SETTING",
    "SWEEP_PACKAGE_REFUSED_EVENT",
    "SWEEP_REFUSED_EVENT",
    "SWEEP_SKIPPED_EVENT",
    "SWEEP_START_DISPATCHED_EVENT",
    "SWEEP_START_REFUSED_EVENT",
    "SWEEP_START_SUMMARY_EVENT",
    "SWEEP_START_UNENQUEUED_EVENT",
    "SWEEP_TASK_NAME",
    "DispatchOutcome",
    "ScheduledDispatch",
    "SweepDispatchError",
    "cadence_reconciliation_fault",
    "collection_task_name",
    "dispatch",
    "dispatch_scheduled_collectors_on_start",
    "scheduled_dispatches",
]

logger = structlog.get_logger(__name__)

#: The dispatch task's own declared name, and the one the beat schedule fires.
#:
#: `cpm.collect.` because a dispatch *is* collection work -- it runs on the
#: `collect` queue beside the collections it enqueues, which is what stops a
#: schedule tick competing for the `verify` queue's compute slots (`R-11`). The
#: name is composed from `core/queues.py`'s own parts rather than spelled out, so
#: the routing audit and this module cannot come to disagree about the namespace.
#:
#: `collectors/tasks.py` declares the task under this name, because Celery's
#: autodiscovery imports each application's `tasks` module and no other.
SWEEP_TASK_NAME: Final[str] = f"{TASK_NAMESPACE_PREFIX}{NAME_SEPARATOR}{Queue.COLLECT.value}{NAME_SEPARATOR}sweep"

#: The last segment of the dispatch task's own name, and therefore the one
#: collector name nothing may be registered under: `collection_task_name("sweep")`
#: is `SWEEP_TASK_NAME` itself, so a dispatch of such a collector would enqueue
#: *the dispatch* once per package -- each of which would do the same. Refused at
#: boot and again at dispatch.
RESERVED_COLLECTOR_NAME: Final[str] = SWEEP_TASK_NAME.rpartition(NAME_SEPARATOR)[2]

#: The keyword the dispatch task takes its collector's name under, and the same
#: keyword a `CELERY_BEAT_SCHEDULE` entry passes it. Declared here because both
#: sides read it: the reconciliation below matches a schedule entry to a
#: registered collector through this key, so a literal spelled in the settings
#: module and a literal spelled in a startup module would be two names that could
#: drift apart while every gate stayed green.
COLLECTOR_KWARG: Final[str] = "collector"

#: The keyword every per-package collection task takes its package under. Every
#: one of the nine swept collectors declares `def collect_x(*, package_id: int,
#: force: bool = False)`, keyword-only and deliberately so -- a dispatch that
#: enqueued positionally could not be wrong about *which* argument, but it could
#: be wrong about a task whose signature grew, and the nine tasks say in as many
#: words
#: that keyword-only is what stops a collection being enqueued for the wrong
#: package.
PACKAGE_KWARG: Final[str] = "package_id"

#: How many rows the database is asked for at a time while the selection is read.
#:
#: It is `.iterator(chunk_size=...)`'s argument and **nothing else**, which is
#: worth stating because an earlier version of this module also grouped the stream
#: into batches of this size and claimed the grouping bounded memory. It did not:
#: the stream is already lazy, so a batch is five hundred *additional* resident
#: integers rather than five hundred instead of ten thousand. The grouping is
#: gone; what remains is the server-side fetch size, which is a real bound -- it
#: is how many rows the database driver materialises per round trip.
#:
#: Five hundred, and deliberately not tuned against a broker: enqueueing is one
#: round trip per task whatever this number is, because Celery offers no batch
#: publish on the path these tasks take. `collectors/selection.py`'s
#: `_SNAPSHOT_CHUNK` is the precedent for a chunked read over the inventory; it is
#: four times this because it is folding rows rather than reading ids.
SELECTION_CHUNK: Final[int] = 500

#: The event a completed dispatch is logged under. Dotted, as every event
#: `core/collection.py` emits is, and named here so the case that asserts the log
#: and the code that emits it cannot drift.
SWEEP_DISPATCHED_EVENT: Final[str] = "sweep.dispatched"

#: The event a dispatch that could not enqueue anything at all is logged under.
#: Distinct from the above for the reason `collection.refused_by_rate_limit` is
#: distinct from `collection.failed`: an operator reading "why did nothing
#: collect" needs to tell a dispatch that refused from one that ran.
SWEEP_REFUSED_EVENT: Final[str] = "sweep.refused"

#: The event a dispatch suppressed by its own previous run is logged under.
#: Distinct from both above for the reason `collection.skipped_inside_window` is
#: distinct from `collection.failed`: nothing went wrong, and the previous sweep
#: of this collector is simply still draining.
SWEEP_SKIPPED_EVENT: Final[str] = "sweep.skipped_overlapping"

#: The keys the three run-level events above carry. Fixed for the reason
#: `EVENT_KEYS` in `core/collection.py` is fixed: a `detail` that is a sentence on
#: one path and an exception's text on another is two schemas wearing one key --
#: which is why `detail` is one of the keys rather than an extra a reader has to
#: know about.
EVENT_KEYS: Final[tuple[str, ...]] = ("collector", "task", "enqueued", "refused", "detail")

#: The event one refused package is logged under, and the reason it exists.
#:
#: The ledger row records *how many* packages the broker would not take and the
#: first reason; it cannot record ten thousand primary keys, and a `detail` column
#: is not a queue. So each refusal is logged as it happens, with the package on
#: the line -- which is what gives an operator reading a `partial` sweep a
#: recovery path rather than a count.
SWEEP_PACKAGE_REFUSED_EVENT: Final[str] = "sweep.package_refused"

#: The keys the per-package event carries. It names a package where the three
#: run-level events name counts, so it is a different schema and says so with a
#: different key set rather than by omitting keys from the shared one.
PACKAGE_EVENT_KEYS: Final[tuple[str, ...]] = ("collector", "task", "package_id", "detail")

#: The setting `config/settings/base.py` assigns and the `beat_init` receiver in
#: `config/celery_app.py` reads (`CPM-OPERATE-S04`). Spelled once, here, on the
#: terms `inventory_source.py` spells `INVENTORY_SOURCE_SETTING`: the receiver
#: and the settings test name the same thing. Off by default; the `dev` pixi
#: feature's activation env turns it on, and no production-bound table does.
SWEEP_ON_BEAT_START_SETTING: Final[str] = "CPM_SWEEP_ON_BEAT_START"

#: The event one start-of-beat dispatch is logged under, naming the collector,
#: the interval the brought-forward tick stands in for, the countdown it was
#: enqueued with and the task id the broker handed back.
SWEEP_START_DISPATCHED_EVENT: Final[str] = "sweep.dispatched_on_start"

#: The event the start dispatch is refused under, as a whole, before anything
#: is enqueued. One reason today: the Celery app runs every task inline, so the
#: beat process has no worker to hand a sweep to and would run nine collectors
#: inside the scheduler.
SWEEP_START_REFUSED_EVENT: Final[str] = "sweep.start_refused"

#: The event one collector's start dispatch is logged under when the broker
#: would not take it, carrying `not_offered`: how many later entries were left
#: to the tick because the broker could not be reached at all, or zero when the
#: refusal was this entry's own and the next was still offered.
SWEEP_START_UNENQUEUED_EVENT: Final[str] = "sweep.start_dispatch_unenqueued"

#: The one summary event a start dispatch ends with: out of how many the
#: schedule declares, how many were enqueued, how many the broker refused, and
#: how many were never offered because the broker could not be reached.
SWEEP_START_SUMMARY_EVENT: Final[str] = "sweep.start_summary"


class SweepDispatchError(ValueError):
    """A dispatch was asked for something it cannot dispatch.

    A `ValueError` subclass, matching `core/collection.py`'s
    `CollectorConfigurationError`, `core/registry.py`'s `CollectorRegistryError`
    and the collectors' locator errors: every "this declaration is unusable"
    in this product is a `ValueError`, so a caller catching one catches them all.

    **It escapes the dispatch rather than becoming a row**, on the terms
    `SourceLocatorError` set in `CPM-CURRENCY-S01`: a dispatch writes no evidence
    at all, so there is no row for it to become. What it leaves behind is the
    ledger row the recorder finalizes `failed` on the way out, carrying the
    message -- which is the whole durable record of a dispatch that was asked to
    fire for a collector nothing registered, for one that is not swept per
    package, for one whose selection is not something that can be streamed, or
    for a task Celery does not hold.
    """


@dataclass(frozen=True, slots=True)
class DispatchOutcome:
    """What one dispatch did, for the caller that asked for it.

    The dispatch's counterpart to `CollectionResult`, and it is deliberately not
    that class: `CollectionResult.evidence_rows` counts rows a dispatch never
    writes, and a zero there would read as "this run observed nothing" rather
    than "this run observes nothing by construction". Frozen, because it is a
    report rather than a workspace.

    Attributes:
        state: How the dispatch ended, over `RunState` -- the same value the
            ledger row carries. `SUCCEEDED` when every selected package was
            enqueued (including when none was selected), `PARTIAL` when some were
            and some were not or when the run was cut short after enqueueing
            some, `FAILED` when none of a non-empty selection was, and `SKIPPED`
            when this collector's previous dispatch is still draining.
        enqueued: How many per-package tasks reached the broker.
        refused: How many the broker would not take. Zero on every path but the
            two that report it.
        detail: Why the dispatch ended this way, in the same words the ledger
            row's `detail` carries -- every terminal path declares the same
            string to both.

    """

    state: RunState
    enqueued: int
    refused: int
    detail: str


@dataclass(frozen=True, slots=True)
class ScheduledDispatch:
    """One `CELERY_BEAT_SCHEDULE` entry that fires the dispatch, as the start dispatch reads it.

    What beat itself does on a tick, read off the declaration rather than off
    beat: the collector the entry names, the interval it fires at, and the
    countdown its `options` carry -- which is the phase three entries declare
    so that they do not fire on the tick, and which a start dispatch honours
    for the same reasons the tick does. Frozen, because it is a reading of
    settings rather than a workspace.

    Attributes:
        collector: The name the entry passes under `COLLECTOR_KWARG`.
        interval: The entry's declared interval, or `None` for a schedule this
            module cannot read as one (`_as_interval`).
        countdown: The entry's `options.countdown` in seconds -- a positive
            finite number kept as declared, or `0` for an entry that declares
            none or declares something that is not one (`_as_countdown`).
        options: The entry's whole `options` mapping with `countdown` normalised
            to the value above, which is exactly what beat's own tick hands
            `apply_async` and therefore what the start dispatch hands it.

    """

    collector: str
    interval: timedelta | None
    countdown: float
    options: dict[str, Any]


def collection_task_name(collector: str) -> str:
    """Return the per-package collection task's declared name for one collector.

    Derived rather than configured, and that is what makes a dispatch unable to
    fire the wrong task: `cpm.collect.<name>` is the namespace `core/queues.py`
    routes to the `collect` queue, and every per-package collection task in this
    product already declares its name that way.

    Args:
        collector: The collector's declared name, as `core/registry.py` keys it.

    Returns:
        The task name, whether or not Celery holds it -- `dispatch` is what
        refuses a name the registry has never seen.

    """
    return f"{TASK_NAMESPACE_PREFIX}{NAME_SEPARATOR}{Queue.COLLECT.value}{NAME_SEPARATOR}{collector}"


def _streamed(collector: type[Collector], selection: Iterable[int]) -> Iterator[int]:
    """Return the selection as a stream, refusing one that has already been materialised.

    A `QuerySet` caches every row it yields, so iterating one directly would put
    `CPM-NFR-1`'s ten thousand primary keys in memory whatever else this module
    does -- the cache is the queryset's, not the loop's. `.iterator()` is what
    turns it into a server-side stream, and it is applied here rather than inside
    each collector so that the nine selections stay ordinary querysets a case can
    read and compare.

    **Anything that is already an iterator is taken as it is, and anything else is
    refused.** A hook answering with a `list` or a `tuple` has done the read this
    module exists not to do, and it would do it silently: every count would match,
    every case would pass, and the streaming property would be gone. So the
    refusal is by construction -- a selection is a queryset or an iterator, and a
    materialised sequence is neither.

    Args:
        collector: The registered class, for the message.
        selection: What `Collector.selectable_packages` answered.

    Returns:
        An iterator over the primary keys, to be consumed once.

    Raises:
        SweepDispatchError: When the selection is neither a queryset nor an
            iterator.

    """
    if isinstance(selection, QuerySet):
        return iter(selection.iterator(chunk_size=SELECTION_CHUNK))
    if iter(selection) is selection:
        return iter(selection)
    message = (
        f"{collector.__name__} (name={collector.name!r}) answered selectable_packages with "
        f"{type(selection).__name__}, which has already been read into memory. A selection is streamed rather "
        f"than materialised (CPM-NFR-1): answer with a queryset, which this module reads through .iterator(), "
        f"or with an iterator the dispatch can consume one key at a time."
    )
    raise SweepDispatchError(message)


def _resolve(collector: str) -> type[Collector]:
    """Return the registered collector class one dispatch is for.

    Args:
        collector: The collector's declared name.

    Returns:
        The registered class.

    Raises:
        SweepDispatchError: When the name is the dispatch task's own last segment
            -- a collector registered as `sweep` derives `cpm.collect.sweep`, so
            dispatching it would enqueue the dispatch once per package and each of
            those would do the same -- or when nothing is registered under that
            name. The second is refused rather than skipped: a schedule entry
            naming a collector this component has not adopted is a
            misconfiguration, and a dispatch that shrugged at it would leave a
            surface silently unobserved. The message names what *is* registered,
            because "no such collector" tells an operator nothing they can act on.

    """
    if collector == RESERVED_COLLECTOR_NAME:
        message = (
            f"collector={collector!r} derives the dispatch task's own name {SWEEP_TASK_NAME!r}, so dispatching "
            f"it would enqueue this task once per package and every one of those would do the same. The name "
            f"is reserved; register the collector under another one."
        )
        raise SweepDispatchError(message)

    registered = registrations()
    found = registered.get(collector)
    if found is None:
        message = (
            f"no collector is registered under name={collector!r}, so there is nothing to dispatch. The "
            f"registered names are {sorted(registered)}; a CELERY_BEAT_SCHEDULE entry naming a collector this "
            f"component has not adopted is refused rather than skipped (CPM-AD-20)."
        )
        raise SweepDispatchError(message)
    return found


def _selection_of(collector: type[Collector]) -> Iterable[int]:
    """Return what one collector says it can be asked about, refusing one that says nothing.

    Args:
        collector: The registered class.

    Returns:
        The selection, exactly as the collector declared it.

    Raises:
        SweepDispatchError: When the collector answers `None`, which means it is
            not swept one package at a time. Inventory ingestion is the case:
            it reads one document naming many packages (`CPM-AD-25`) and its
            run-scoped `sweep()` is the whole of its schedule, so dispatching it
            per package would be asking it for a locator it refuses to name.
            Refused **by name** rather than silently skipped, because a schedule
            entry firing a per-package dispatch at a run-scoped collector is a
            misconfiguration that would otherwise look like a collector nothing
            ever ran.

    """
    selection = collector.selectable_packages()
    if selection is None:
        message = (
            f"{collector.__name__} (name={collector.name!r}) declares no selectable_packages, so it is not a "
            f"per-package collector and cannot be swept one package at a time. A run-scoped collector reads "
            f"one document naming many packages (CPM-AD-25) and is scheduled on its own sweep task instead."
        )
        raise SweepDispatchError(message)
    return selection


def _task_for(collector: type[Collector]) -> Any:
    """Return the Celery task one dispatch enqueues, refusing a name Celery does not hold.

    Typed `Any` because celery is, and narrowing it here would be a claim this
    module cannot back: `current_app.tasks` is an untyped registry, so a stated
    return type would be a cast wearing an annotation. What bounds the looseness
    is that exactly one attribute of it is ever reached -- `apply_async`, from
    one call site.

    Args:
        collector: The registered class.

    Returns:
        The registered task object, ready to be asked for `apply_async`.

    Raises:
        SweepDispatchError: When the derived name is not in Celery's registry.
            A dispatch never invents a task name it cannot find: publishing into
            a name nothing consumes is silent non-delivery, which looks exactly
            like a source that answered nothing and is the failure `CPM-NFR-3`'s
            "never a clean result" is about, moved one layer out.

    """
    name = collection_task_name(collector.name)
    task = current_app.tasks.get(name)
    if task is None:
        message = (
            f"{collector.__name__} (name={collector.name!r}) would be collected by task {name!r}, and Celery's "
            f"registry does not hold it. Every per-package collection task is declared in "
            f"collectors/tasks.py, which is the only module Celery's autodiscovery imports; a task declared "
            f"anywhere else is registered by whatever happened to import it and by no worker."
        )
        raise SweepDispatchError(message)
    return task


def _already_draining(*, collector: str, run: RunHandle) -> bool:
    """Report whether an earlier dispatch of this collector has not finished.

    The overlap guard, and it exists because the declared allowances cannot drain
    `CPM-NFR-1`'s inventory inside a cadence: without it every missed or slow tick
    enqueues a second whole inventory behind the first, and the queue grows
    without bound while each collection is refused by the very rate limiter that
    made the sweep slow. `django_celery_beat` fires on a schedule and has no
    notion of a previous run, so the guard is the dispatch's.

    **Read off the run ledger rather than a lock**, because the ledger is already
    the durable record of what is in flight and a lock would be a second one that
    can disagree with it. Its own row is excluded -- the recorder inserted it
    `running` before this was reached.

    Args:
        collector: The collector's declared name.
        run: This dispatch's own ledger handle, whose row is not an overlap with
            itself.

    Returns:
        True when another package-less run of this collector is still `running`.

    """
    return (
        CollectionRun.objects.filter(
            collector=collector,
            package__isnull=True,
            status=RunState.RUNNING.value,
        )
        .exclude(pk=run.run.pk)
        .exists()
    )


def dispatch(*, collector: str, clock: Clock) -> DispatchOutcome:
    """Enqueue one per-package collection for every package one collector can be asked about.

    The whole of this story's run-time behaviour, and it makes no outbound call
    of its own. The recorder is opened first and nothing wraps it, exactly as
    `Collector.collect` does and for the same reason (`CPM-EVIDENCE-S03`): the
    row exists before anything can fail, so a dispatch that raises is on the
    record.

    **The row is opened with no package reference.** A dispatch is not scoped to
    one package -- `core/ledger.py` writes NULL for such a run and it stays
    answerable by `unfinished()`, which is the same shape the run-scoped sweep
    uses.

    **Nothing is retried, waited for or chained.** `apply_async` hands the task
    to the broker and returns; what happens next is the per-package task's own
    ledger row. A dispatch that polled for its children would hold a worker slot
    for the length of a rate-limited sweep, which is the failure `CPM-AD-9`'s
    time limits and `CPM-AD-23`'s per-package boundary both exist to prevent.

    **Every enqueued message expires at one cadence.** A collection that has not
    been consumed by the time the next tick fires is stale by construction -- the
    next dispatch will offer the same package again -- so it is dropped rather
    than queued in front of the fresher one. That, and the overlap skip above it,
    are what keep a source that cannot be drained inside its cadence from
    accumulating a queue nobody can measure.

    Args:
        collector: The collector to dispatch, by the declared name
            `core/registry.py` keys it under and `CELERY_BEAT_SCHEDULE` passes.
        clock: The clock the ledger row's two instants are read from
            (`CPM-AD-26`). No default: a component's dependence on time belongs
            in its signature.

    Returns:
        What the dispatch did, mirroring the ledger row it wrote.

    Raises:
        SweepDispatchError: When the name is blank or reserved, names no
            registered collector, names one that is not swept per package, names
            one whose selection is not something that can be streamed, or names
            one whose derived task Celery does not hold. The first is raised
            before the recorder opens -- there is no name to record a run against
            -- and the rest from inside it, so the ledger row is finalized
            `failed` carrying the reason and nothing was enqueued.

    """
    if not isinstance(collector, str) or not collector.strip():
        message = (
            f"a dispatch was asked for collector={collector!r}, which names nothing. The name is what a run is "
            f"traced to (CPM-FR-39) and what the collection task is derived from; a blank one has neither."
        )
        raise SweepDispatchError(message)

    with collection_run(collector=collector, clock=clock) as run:
        registered = _resolve(collector)
        stream = _streamed(registered, _selection_of(registered))
        task = _task_for(registered)
        task_name = collection_task_name(collector)

        if _already_draining(collector=collector, run=run):
            detail = (
                f"{collector}'s previous dispatch has not finished, so this tick enqueued nothing rather than "
                f"offering the inventory a second time. A sweep that cannot be drained inside its cadence "
                f"grows a queue nobody can measure (CPM-NFR-1); the previous run's packages are still due."
            )
            logger.info(
                SWEEP_SKIPPED_EVENT,
                collector=collector,
                task=task_name,
                enqueued=0,
                refused=0,
                detail=detail,
            )
            run.skipped(detail=detail)
            return DispatchOutcome(state=RunState.SKIPPED, enqueued=0, refused=0, detail=detail)

        enqueued, refused, first_refusal, interruption = _enqueue_each(
            stream,
            collector=collector,
            task=task,
            task_name=task_name,
            expires=registered.cadence,
        )
        return _finalized(
            collector=collector,
            task_name=task_name,
            enqueued=enqueued,
            refused=refused,
            first_refusal=first_refusal,
            interruption=interruption,
            run=run,
        )


def _enqueue_each(
    stream: Iterator[int],
    *,
    collector: str,
    task: Any,
    task_name: str,
    expires: timedelta | None,
) -> tuple[int, int, str, str]:
    """Offer every selected package to the broker, and never abandon the rest over one.

    Three ways this loop can end, and each is a different fact the ledger row has
    to be able to state. The stream runs out, which is the ordinary case. The
    soft time limit fires, which is `SoftTimeLimitExceeded` and is **not** a
    broker refusal -- catching it in the broad handler below would count the
    signal as one more failed package, keep looping until the hard limit killed
    the worker, and leave the row `running` for ever. Or the *selection itself*
    raises while it is being read, which is a database failure partway through a
    stream and is not attributable to any package.

    Args:
        stream: The streamed selection.
        collector: The collector's declared name, for the log lines.
        task: The Celery task to enqueue.
        task_name: Its declared name, for the log lines.
        expires: How long an enqueued message stays worth running, or `None` for
            a collector that declares no cadence.

    Returns:
        How many were enqueued, how many the broker refused, the first refusal's
        type and message, and why the loop stopped early -- the last being the
        empty string for a loop that read its whole selection.

    """
    seconds = None if expires is None else expires.total_seconds()
    enqueued = 0
    refused = 0
    first_refusal = ""
    try:
        for package_id in stream:
            try:
                task.apply_async(kwargs={PACKAGE_KWARG: package_id}, expires=seconds)
            except SoftTimeLimitExceeded:
                # By name and before the broad handler, because it is not a
                # refusal of this package: it is the worker telling this task to
                # stop. Re-raised so the outer handler records it once, rather
                # than counted here and met again on the next package.
                raise
            except Exception as unenqueued:  # noqa: BLE001 - see below
                # Caught this widely and deliberately, and nothing is swallowed:
                # the package is logged, the reason is counted, the first one is
                # carried into the ledger row's `detail`, and the run is
                # finalized `partial` or `failed` on the counts. A broker refuses
                # in as many ways as a network can, and the guarantee being
                # defended -- that the packages already enqueued stay enqueued
                # (`CPM-FR-15`) -- does not depend on which. Letting it escape
                # would abandon the rest of the selection over one package, which
                # is the failure this story is named for.
                reason = f"{type(unenqueued).__name__}: {unenqueued}"
                refused += 1
                first_refusal = first_refusal or reason
                logger.warning(
                    SWEEP_PACKAGE_REFUSED_EVENT,
                    collector=collector,
                    task=task_name,
                    package_id=package_id,
                    detail=reason,
                )
            else:
                enqueued += 1
    except SoftTimeLimitExceeded:
        interruption = (
            f"the soft time limit fired after {enqueued} package(s) had been enqueued, so the rest of the "
            f"selection was not offered. The ones that were enqueued stay enqueued (CPM-FR-15) and the next "
            f"tick offers the whole selection again (CPM-AD-9)."
        )
    except Exception as unreadable:  # noqa: BLE001 - see below
        # The *selection* failed while it was being read, which is a different
        # fact from a package the broker would not take: it is attributable to no
        # package and it ends the stream. Caught for the same reason the handler
        # above is broad -- what a database can do to a cursor mid-stream is not
        # a fixed list -- and recorded rather than raised, because raising here
        # would finalize `failed` a run that had already enqueued thousands.
        interruption = (
            f"the selection could not be read to the end after {enqueued} package(s) had been enqueued "
            f"({type(unreadable).__name__}: {unreadable}). The ones that were enqueued stay enqueued "
            f"(CPM-FR-15)."
        )
    else:
        interruption = ""
    return enqueued, refused, first_refusal, interruption


def _finalized(  # noqa: PLR0913 - one parameter per fact the message is built from
    *,
    collector: str,
    task_name: str,
    enqueued: int,
    refused: int,
    first_refusal: str,
    interruption: str,
    run: RunHandle,
) -> DispatchOutcome:
    """Declare the dispatch's ending on the ledger row and return the matching report.

    Five endings, and each declares its own `detail` -- which is the string the
    ledger row and the returned outcome both carry, on the terms
    `Collector.collect` sets for its eight.

    **`partial` is never reached by a dispatch that enqueued nothing**, and
    `failed` is never reached by one that enqueued something. That is the whole
    of `CPM-FR-15` on this path: "some of the work was done" and "none of it was"
    are different operational facts, and a reader must never have to infer which
    from a count. It is why an interruption is read against the count rather than
    being an ending of its own.

    Args:
        collector: The collector's declared name.
        task_name: The derived per-package task name, for the message.
        enqueued: How many tasks reached the broker.
        refused: How many the broker would not take.
        first_refusal: The first refusal's type and message, or `""`.
        interruption: Why the loop stopped before the selection ran out, or `""`.
        run: The ledger handle to declare the ending on.

    Returns:
        The report, carrying the same state and the same words the row does.

    """
    if interruption and enqueued:
        state = RunState.PARTIAL
        detail = f"{collector} enqueued {enqueued} package(s) as {task_name} and then stopped: {interruption}"
        run.partial(detail=detail)
    elif interruption:
        state = RunState.FAILED
        detail = f"{collector} enqueued nothing as {task_name} and stopped: {interruption}"
        run.failed(detail=detail)
    elif refused and enqueued:
        state = RunState.PARTIAL
        detail = (
            f"{collector} enqueued {enqueued} of {enqueued + refused} selected package(s) as {task_name}; "
            f"{refused} could not be enqueued ({first_refusal}). The ones that were enqueued stay enqueued "
            f"(CPM-FR-15); each refused package is on the {SWEEP_PACKAGE_REFUSED_EVENT} log line."
        )
        run.partial(detail=detail)
    elif refused:
        state = RunState.FAILED
        detail = (
            f"{collector} could not enqueue any of {refused} selected package(s) as {task_name} "
            f"({first_refusal}). Nothing was dispatched; each refused package is on the "
            f"{SWEEP_PACKAGE_REFUSED_EVENT} log line."
        )
        run.failed(detail=detail)
    elif enqueued:
        state = RunState.SUCCEEDED
        detail = f"{collector} enqueued {enqueued} selected package(s) as {task_name}."
        run.succeeded(detail=detail)
    else:
        state = RunState.SUCCEEDED
        detail = (
            f"{collector} selected no packages, so nothing was enqueued as {task_name}. An empty selection is "
            f"a collector that was asked and answered, not a sweep that failed."
        )
        run.succeeded(detail=detail)

    logger.info(
        SWEEP_REFUSED_EVENT if state is RunState.FAILED else SWEEP_DISPATCHED_EVENT,
        collector=collector,
        task=task_name,
        enqueued=enqueued,
        refused=refused,
        detail=detail,
    )
    return DispatchOutcome(state=state, enqueued=enqueued, refused=refused, detail=detail)


# ---------------------------------------------------------------------------
# `CPM-AD-20`'s reconciliation, as a pure function both boot points call.
# ---------------------------------------------------------------------------


def _as_interval(declared: object) -> timedelta | None:
    """Return a schedule entry's declared interval, or `None` when it is not one.

    Celery accepts three shapes in a beat entry's `schedule`: a `timedelta`, a
    bare number of seconds, and a `crontab`/`solar` object. The first two are the
    same statement written two ways and are reconciled against a collector's
    declared cadence; the third is a *calendar*, which a `timedelta` cannot be
    compared against without inventing an answer.

    Args:
        declared: Whatever the entry put under `schedule`.

    Returns:
        The interval, or `None` for a schedule this reconciliation cannot read.
        `bool` is excluded explicitly: it is a subclass of `int`, so `True` would
        otherwise be a one-second cadence somebody meant as a flag.

    """
    if isinstance(declared, timedelta):
        return declared
    if isinstance(declared, bool):
        return None
    if isinstance(declared, int | float):
        return timedelta(seconds=declared)
    return None


def _dispatch_entries(schedule: object) -> Iterator[tuple[str | None, dict[str, Any]]]:
    """Yield every schedule entry that fires the dispatch, in the schedule's own order.

    The one walk over `CELERY_BEAT_SCHEDULE` this module makes, shared by the
    reconciliation below and by the start dispatch: two walks would be two
    readings of which entries are dispatches and which keyword names the
    collector, and they would disagree the first time one of them changed.

    Args:
        schedule: Whatever `CELERY_BEAT_SCHEDULE` holds. Read defensively rather
            than trusted: a settings module that assigned a list, or an entry
            that is not a mapping, is a misconfiguration and must meet the
            reconciliation's named refusal rather than an `AttributeError` out of
            a boot hook.

    Yields:
        The collector name each dispatch entry passes -- or `None` for one that
        names nothing usable -- and the entry itself, in declaration order.
        Entries firing any other task are not this module's business and are
        not yielded.

    """
    if not isinstance(schedule, dict):
        return
    for entry in schedule.values():
        if not isinstance(entry, dict) or entry.get("task") != SWEEP_TASK_NAME:
            continue
        keywords = entry.get("kwargs")
        named = keywords.get(COLLECTOR_KWARG) if isinstance(keywords, dict) else None
        yield (named if isinstance(named, str) and named.strip() else None), entry


def _scheduled_dispatches(schedule: object) -> tuple[dict[str, list[object]], int]:
    """Return what a beat schedule says each collector's cadence is.

    Args:
        schedule: Whatever `CELERY_BEAT_SCHEDULE` holds, read through
            `_dispatch_entries`.

    Returns:
        The declared schedule of every entry firing the dispatch, by the collector
        name that entry passes -- as a **list** per collector, because two entries
        naming one collector is one of the disagreements this reconciliation
        exists to find and a mapping keyed by name would silently keep the last of
        them -- and how many dispatch entries name no collector at all.

    """
    by_collector: dict[str, list[object]] = {}
    unnamed = 0
    for named, entry in _dispatch_entries(schedule):
        if named is None:
            unnamed += 1
            continue
        by_collector.setdefault(named, []).append(entry.get("schedule"))
    return by_collector, unnamed


def _as_countdown(entry: Mapping[str, Any]) -> float:
    """Return the countdown an entry's `options` declare, in seconds, or zero.

    Args:
        entry: One dispatch entry.

    Returns:
        `options.countdown` when it is a positive finite number, kept as the
        number it is -- a float is not truncated, because `apply_async` takes a
        float and beat would pass it through unchanged -- else `0`. `bool` is
        excluded for the reason `_as_interval` excludes it; `nan`, `inf`, a
        negative or non-numeric value read as no countdown rather than as an
        error: beat passes `options` to `apply_async` unread, and the start
        dispatch does not refuse what beat would accept.

    """
    options = entry.get("options")
    declared = options.get("countdown") if isinstance(options, dict) else None
    if isinstance(declared, bool) or not isinstance(declared, int | float):
        return 0
    if not math.isfinite(declared) or declared <= 0:
        return 0
    return declared


def _as_options(entry: Mapping[str, Any], countdown: float) -> dict[str, Any]:
    """Return the entry's `options` as `apply_async` keywords, with the countdown normalised.

    Args:
        entry: One dispatch entry.
        countdown: What `_as_countdown` read off it.

    Returns:
        A copy of `options` when it is a mapping, else an empty one, with
        `countdown` set to the normalised value in either case.

    """
    options = entry.get("options")
    forwarded: dict[str, Any] = dict(options) if isinstance(options, dict) else {}
    forwarded["countdown"] = countdown
    return forwarded


def scheduled_dispatches(schedule: object) -> tuple[ScheduledDispatch, ...]:
    """Return every dispatch the schedule declares, in its own order, with its phase.

    What a start dispatch enqueues (`CPM-OPERATE-S04`): one per entry firing
    `SWEEP_TASK_NAME`, each carrying the collector the entry names, the interval
    it declares and the countdown its `options` carry. Read off the declaration
    and nothing else -- neither the registry nor the scheduler's tables -- so
    the answer is the same in the beat process, in the suite and in a shell.

    An entry naming no collector is left out rather than reported: it is one of
    the faults `cadence_reconciliation_fault` refuses at boot, so a process that
    reaches a start dispatch has none.

    Args:
        schedule: Whatever `CELERY_BEAT_SCHEDULE` holds, read through
            `_dispatch_entries`.

    Returns:
        The declared dispatches, in the schedule's declaration order. Empty for
        a schedule that is not a mapping or declares no dispatch.

    """
    return tuple(
        ScheduledDispatch(
            collector=named,
            interval=_as_interval(entry.get("schedule")),
            countdown=countdown,
            options=_as_options(entry, countdown),
        )
        for named, entry in _dispatch_entries(schedule)
        if named is not None
        for countdown in (_as_countdown(entry),)
    )


def dispatch_scheduled_collectors_on_start(schedule: object) -> int:
    """Enqueue one dispatch per scheduled collector, as beat's first tick would, now.

    `CPM-OPERATE-S04`'s whole run-time behaviour. `django_celery_beat` starts an
    interval entry's clock when it creates it, so a fresh component fires no
    daily sweep for a day and no weekly one for a week; this is what the
    `beat_init` receiver in `config/celery_app.py` calls, with the setting on,
    so that day one is observed. It **enqueues and nothing else**: each entry is
    one `apply_async` of the same task beat fires, with the same keyword and the
    entry's own `options`, and the schedule's own entries are not touched -- a
    cadence is data (`CPM-AD-20`) and "once, at start" is not a cadence.

    **Nothing here is a second overlap guard or a second window.** Each dispatch
    records `skipped` for itself when the collector's previous dispatch is still
    draining, and each collection skips itself inside its collector's
    observation window, so a beat restart on the same day re-enqueues nine
    dispatches that then do nothing -- which is what makes the restart harmless
    without this function knowing anything about the ledger.

    **Refused under eager Celery.** With `task_always_eager` on, `apply_async`
    runs the task in the caller, and the caller is the beat process: a bare
    `pixi run -e dev beat` outside the stack would run nine collectors inline
    inside the scheduler before it ticked once. Refused with one event, and
    nothing is enqueued.

    **A broker that cannot be reached ends the start dispatch, once.** Kombu's
    `OperationalError` is a connection that could not be made after its own
    retry window, and a broker that refused the first entry that way will refuse
    the next eight the same way -- offering them would pay nine retry windows
    before beat's first tick. So the first one is logged, naming the collector
    and how many were not offered, and the rest are left to the tick. Any other
    exception is that entry's own and costs only it. Nothing is raised: beat is
    starting, and a start dispatch that failed beat's start would turn a
    day-one convenience into an outage.

    Args:
        schedule: Whatever `CELERY_BEAT_SCHEDULE` holds.

    Returns:
        How many dispatches reached the broker.

    """
    declared = scheduled_dispatches(schedule)
    if runs_eagerly():
        logger.info(
            SWEEP_START_REFUSED_EVENT,
            task=SWEEP_TASK_NAME,
            declared=len(declared),
            detail=(
                "the Celery app runs every task inline (task_always_eager), so the beat process has no worker "
                "to hand a sweep to and would run every scheduled collector inside the scheduler; nothing was "
                "enqueued. The start dispatch is for a beat with a broker and a worker behind it."
            ),
        )
        return 0

    task = current_app.tasks.get(SWEEP_TASK_NAME)
    if task is None:
        logger.warning(
            SWEEP_START_REFUSED_EVENT,
            task=SWEEP_TASK_NAME,
            declared=len(declared),
            detail=(
                f"Celery's registry does not hold {SWEEP_TASK_NAME!r}, so there is nothing to enqueue; the task is "
                f"declared in collectors/tasks.py, which the beat process imports through autodiscovery."
            ),
        )
        return 0

    enqueued = 0
    refused = 0
    not_offered = 0
    for position, scheduled in enumerate(declared, start=1):
        try:
            result = task.apply_async(kwargs={COLLECTOR_KWARG: scheduled.collector}, **scheduled.options)
        except OperationalError as unreachable:
            # The broker could not be reached, after kombu's own connection
            # retries. The next entries would meet the same broker, so they are
            # left to the tick rather than each paying that window again.
            refused += 1
            not_offered = len(declared) - position
            logger.warning(
                SWEEP_START_UNENQUEUED_EVENT,
                collector=scheduled.collector,
                task=SWEEP_TASK_NAME,
                countdown=scheduled.countdown,
                not_offered=not_offered,
                detail=f"{type(unreachable).__name__}: {unreachable}",
            )
            break
        except Exception as unenqueued:  # noqa: BLE001 - see below
            # Caught this widely and deliberately, on the terms `_enqueue_each`
            # catches a broker's refusal of one package: this entry is logged,
            # the next is still offered, and beat's start is not failed by a
            # dispatch that exists to make day one convenient. Not a connection
            # failure -- that is the branch above -- so nothing says the next
            # entry will meet it too.
            refused += 1
            logger.warning(
                SWEEP_START_UNENQUEUED_EVENT,
                collector=scheduled.collector,
                task=SWEEP_TASK_NAME,
                countdown=scheduled.countdown,
                not_offered=0,
                detail=f"{type(unenqueued).__name__}: {unenqueued}",
            )
            continue
        enqueued += 1
        logger.info(
            SWEEP_START_DISPATCHED_EVENT,
            collector=scheduled.collector,
            task=SWEEP_TASK_NAME,
            interval=None if scheduled.interval is None else scheduled.interval.total_seconds(),
            countdown=scheduled.countdown,
            task_id=result.id,
        )

    logger.info(
        SWEEP_START_SUMMARY_EVENT,
        task=SWEEP_TASK_NAME,
        declared=len(declared),
        enqueued=enqueued,
        refused=refused,
        not_offered=not_offered,
    )
    return enqueued


def _collector_faults(  # noqa: PLR0911 - one return per fault a collector can carry; see below
    collector: type[Collector],
    scheduled: Mapping[str, list[object]],
) -> list[str]:
    """Return everything wrong with one collector's cadence and selection, or nothing.

    Args:
        collector: The registered collector class.
        scheduled: What the beat schedule says, from `_scheduled_dispatches`.

    Returns:
        One message per fault, in a fixed order: a name that is reserved, a
        declaration pair that contradicts itself, an unusable cadence, a freshness
        target that is not strictly greater than it, and a schedule that does not
        carry exactly one readable entry at exactly that interval. Empty for a
        collector whose declarations and schedule agree.

    """
    label = f"{collector.__name__} (name={collector.name!r})"
    if collector.name == RESERVED_COLLECTOR_NAME:
        reserved = (
            f"{label} is registered under the dispatch task's own last segment, so "
            f"collection_task_name({collector.name!r}) is {SWEEP_TASK_NAME!r} -- dispatching it would enqueue "
            f"the dispatch once per package. Register it under another name."
        )
        return [reserved]

    swept = collector.selectable_packages() is not None
    if collector.cadence is None:
        if not swept:
            return []
        unswept = (
            f"{label} declares selectable_packages and no cadence, so nothing sweeps it: its evidence "
            f"would only ever be written by a manual recollection (CPM-UJ-1) while every surface read it "
            f"as ageing normally. Declare the cadence its freshness target was derived from."
        )
        return [unswept]
    if not swept:
        unselectable = (
            f"{label} declares cadence={collector.cadence!r} and no selectable_packages, so a schedule entry "
            f"would dispatch it and the dispatch would refuse it on every tick. A run-scoped collector "
            f"(CPM-AD-25) declares neither."
        )
        return [unselectable]

    faults: list[str] = []
    try:
        cadence = require_cadence(collector.cadence, label=collector.__name__)
    except CollectorConfigurationError as refusal:
        return [f"{label}: {refusal}"]

    target = collector.freshness_target
    if not isinstance(target, timedelta) or target <= cadence:
        faults.append(
            f"{label} declares cadence={cadence!r} and freshness_target={target!r}. The target must be "
            f"strictly greater than the cadence: core/freshness.py reports stale when "
            f"observed_at < now - target, so a target at or below the cadence makes every package read "
            f"stale at exactly the moment its next run is due, with no collection having failed.",
        )

    entries = scheduled.get(collector.name, [])
    if len(entries) != 1:
        faults.append(
            f"{label} declares cadence={cadence!r} and CELERY_BEAT_SCHEDULE carries {len(entries)} entry(ies) "
            f"dispatching it. Exactly one is expected: none means nothing sweeps this collector and its "
            f"freshness target is derived from a number nothing schedules, and two would sweep the whole "
            f"inventory twice a cadence (CPM-AD-20).",
        )
        return faults

    interval = _as_interval(entries[0])
    if interval is None:
        faults.append(
            f"{label} declares cadence={cadence!r} and its CELERY_BEAT_SCHEDULE entry declares "
            f"schedule={entries[0]!r}, which this reconciliation cannot read. It understands an interval -- a "
            f"timedelta, or a number of seconds -- and a crontab or solar schedule is a calendar rather than "
            f"one, so this is not a claim that the two disagree: it is that they cannot be compared. Declare "
            f"the entry as an interval, or the collector's cadence stops being checked against anything.",
        )
    elif interval != cadence:
        faults.append(
            f"{label} declares cadence={cadence!r} and its CELERY_BEAT_SCHEDULE entry fires every "
            f"{interval!r}. The two are the same decision written in two places and they disagree: a "
            f"schedule slower than the declared cadence makes this collector's evidence read stale between "
            f"runs with every gate green (CPM-AD-20).",
        )
    return faults


def cadence_reconciliation_fault(collectors: Sequence[type[Collector]], schedule: object) -> str:
    """Return why the registered collectors and a beat schedule disagree, or nothing.

    `CPM-AD-20`'s reconciliation, as a pure function so that both places that
    enforce it call one rule rather than restating it. `collectors/apps.py` calls
    it from the `ready()` that registers the collectors -- the only hook in a
    deployed process that runs *after* the registry is populated -- and
    `config/startup/stage_two.py` calls it as condition 11.

    It sweeps in both directions. Forward: every registered collector that
    declares a cadence must have exactly one readable schedule entry, at exactly
    that interval, and a freshness target strictly greater than it; and a
    collector must declare a cadence and a selection together or neither, because
    one without the other is either a schedule that refuses on every tick or a
    collector nothing ever sweeps. Backward: every schedule entry firing the
    dispatch must name a collector this component actually registered -- otherwise
    a renamed collector leaves a beat entry firing into nothing, every tick, and
    the surface it was meant to observe goes quietly unobserved.

    **An empty roster reconciles with anything.** A component that has adopted no
    collector has forgotten nothing, and the backward direction in particular must
    not fire over one: `config/startup/stage_two.py` runs before this application's
    `ready()` in a deployed process, so it meets exactly that state.

    Args:
        collectors: The registered collector classes.
        schedule: Whatever `CELERY_BEAT_SCHEDULE` holds, read defensively.

    Returns:
        One message naming every disagreement, or the empty string when they
        agree. Every offender is reported together, for the reason
        `config/startup/stage_two.py`'s freshness sweep reports all of its: an
        operator who fixes one and redeploys should not meet the next as if it
        were a fresh problem.

    """
    if not collectors:
        return ""

    scheduled, unnamed = _scheduled_dispatches(schedule)
    faults: list[str] = []
    if not isinstance(schedule, dict):
        faults.append(
            f"CELERY_BEAT_SCHEDULE is {type(schedule).__name__}, and celery reads it as a mapping of entry "
            f"name to entry. Nothing in it could be reconciled against a collector, so every registered "
            f"collector's cadence is unchecked and unscheduled.",
        )
    if unnamed:
        faults.append(
            f"{unnamed} CELERY_BEAT_SCHEDULE entry(ies) fire {SWEEP_TASK_NAME!r} and name no collector under "
            f"kwargs[{COLLECTOR_KWARG!r}]. The dispatch takes the collector it is for as that keyword, so such "
            f"an entry fails on every tick with a ledger row saying the name is blank.",
        )

    for collector in collectors:
        faults.extend(_collector_faults(collector, scheduled))

    swept = {collector.name for collector in collectors if collector.cadence is not None}
    faults.extend(
        f"CELERY_BEAT_SCHEDULE dispatches collector={named!r}, and no registered collector declares that name "
        f"with a cadence. The collectors this component sweeps per package are {sorted(swept)}; an entry "
        f"naming anything else fires into nothing on every tick and the surface it was meant to observe is "
        f"never observed (CPM-AD-20)."
        for named in sorted(scheduled)
        if named not in swept
    )

    if not faults:
        return ""
    offenders = "; ".join(faults)
    return (
        f"{len(faults)} cadence disagreement(s) between the registered collectors and "
        f"CELERY_BEAT_SCHEDULE -- {offenders} Cadence is data (CPM-AD-20) and a collector's freshness "
        "target is derived from it, so the two are reconciled rather than left to whoever edits one "
        "of them: unreconciled, they make a whole inventory read stale between runs with every gate green."
    )
