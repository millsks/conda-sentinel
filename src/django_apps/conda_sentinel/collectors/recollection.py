"""The one door for a manual recollection: `CPM-UJ-1`'s re-run, asked for from the page.

After an override or a fix, a reviewer used to wait a day for the daily sweep or
ask an operator for a shell. `CPM-OPERATE-S08` gives the request a surface --
"Collect now" on the package page -- and this module is what the surface calls.
One function does the whole of it: refuse an actor without the permission,
decide which collectors can be asked about the package and refuse when none
can, lock the package row, refuse while the package is in flight, write one
`PackageRecollection` audit row, and then, after that row commits, publish one
per-package collection task per collector with `force=True`.

**It publishes by name and imports no collector.** `CPM-AD-9`: the web process
never runs a collector and never imports one. `core/jobs.py` already publishes
exports by task name in `transaction.on_commit`; a recollection is the same shape
with a different name and `force=True`. So this module imports neither
`collectors/sweep.py` nor `collectors/tasks.py` --
`tests/unit/django_apps/test_request_boundary_audit.py` refuses both from
anything a view reaches -- and derives each task's name from `core/queues.py`'s
`task_name(Queue.COLLECT, <collector name>)`, which is the rule
`collectors/sweep.py`'s `collection_task_name` applies; the two are reconciled
by test rather than shared by import. `celery` and `kombu` are imported inside
the publish, as `core/jobs.py` imports them, so the read surface that imports
this module for `can_request` and `pending_recollection` loads neither.

**Only the collectors whose own selection contains the package are offered, and
none offered is a refusal.** A forced collection of a collector that cannot ask
about the package writes a `failed` run with a refusal on the record -- a
`pypi_release` for a package with no PyPI mapping, a `source_release` for one
with no repository. Each collector declares its precondition beside its refusals
in `selectable_packages()`, and `core/registry.py`'s `selects` asks it; nothing
here restates any of them. The names left out are on the audit row as
`not_offered`, so the receipt and the page can say why a collector a reviewer
expected is missing. When nothing at all is offered -- every selection excludes
the package, or no swept collector is registered -- the request is refused
before anything is written: an audit row recording that nobody was asked would
answer the question it exists to answer with nothing. `verify_py314_build` is
never offered: it is not swept per package at all, and its task takes no
`force`.

**"In flight" is two questions, and the answer is bounded by the task time
limit.** A run row exists only once a worker *starts* a task, so a ledger-only
guard admits every press made during queue latency and lets two concurrent
presses both pass. The rule therefore reads two tables: the package is in flight
when the ledger holds an unfinished run on it started within the window
(`core/ledger.py`'s `runs_in_flight`), **or** when a recollection of it within
the window has not yet been *answered* -- answered meaning every collector that
press asked for has a run on the package started at or after the row's
`observed_at` and finished. And the check and the insert happen with the
package row locked (`select_for_update`), so two presses arriving together are
serialised: the second sees the first's row and is refused. The window is
`in_flight_window()`, twice `CELERY_TASK_TIME_LIMIT` read from settings at call
time: no live run can outlast the hard limit, so a row older than twice it is a
killed worker's -- `CPM-AD-2` exempts the ledger from append-only precisely so
such a row is left behind -- and must not lock a reviewer out of a package for
the ninety days the purge keeps it. A press whose tasks were never consumed is
bounded the same way: each is published with `expires` set to the window, so a
message nobody took inside it is dropped rather than firing days later, and the
row it belongs to stops counting as pending when the window passes.
`docs/conda-sentinel/operations.md` says the same to the operator.

**The audit row records what was asked for; the receipt records what was
handed off.** The row is written before the publish and is append-only, so its
`collectors` column can only ever mean "asked": the publish is `on_commit`, for
the reason `core/jobs.py` gives -- a task published inside the transaction
reaches a worker that may read the database before the commit lands -- and a
refusal there cannot be written back. Any exception on one name is caught at
the publish boundary, logged, and named on the receipt's `unpublished`; the
next name is still tried. A row that has committed must never become a 500
with later names untried, and the broker is asked with `retry=False` so a down
broker fails fast rather than stalling the request for `N x retries`.

**The service refuses to run inside somebody else's transaction.** The page's
message names what the broker refused, which is only true if the publish has
run by the time the view composes it -- so `PackageRecollectView` is exempt from
`ATOMIC_REQUESTS` and this module's own `atomic()` must be the outermost one.
Rather than trust that, `request_recollection` checks `connection.in_atomic_block`
before opening its own and refuses if a caller has already opened one: a future
request-wide transaction fails loudly instead of making the message lie.
`tests/integration/django_apps/test_recollection.py` therefore runs its service
and page cases under `transaction=True`, where the commit is real.

**The permission is read once, here.** `tests/unit/django_apps/test_permission_audit.py`
licenses exactly one `has_perm` in this module, and both `can_request` -- which
the page reads to decide whether to draw the button -- and `request_recollection`
go through it. The view computes nothing about authorization itself; it reads a
boolean this module answers, intersected with its own role requirement.

**The trace travels.** The row's `trace_id` is `current_trace_id()`; the Celery
instrumentor propagates the request's span into every task `send_task` publishes,
so each run's ledger row carries the same id (`CPM-AD-15`) -- which is how the
runs a press produced are told from the sweep's on the page.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from datetime import UTC
from datetime import timedelta
from typing import TYPE_CHECKING
from typing import Final

import structlog
from django.conf import settings
from django.db import connection
from django.db import transaction

from conda_sentinel.collectors.inventory import IDENTITY_FIELD
from conda_sentinel.collectors.models import PackageRecollection
from conda_sentinel.core.ledger import current_trace_id
from conda_sentinel.core.ledger import runs_in_flight
from conda_sentinel.core.models import CollectionRun
from conda_sentinel.core.queues import Queue
from conda_sentinel.core.queues import task_name
from conda_sentinel.core.registry import selects
from conda_sentinel.core.registry import swept_collectors
from conda_sentinel.core.roles import RECOLLECT_PERMISSION
from conda_sentinel.identity.models import Package

if TYPE_CHECKING:
    from datetime import datetime

    from django.contrib.auth.models import AnonymousUser

    from conda_sentinel.core.clock import Clock
    from django_service.users.models import User

__all__ = [
    "FORCE_KWARG",
    "IN_FLIGHT_WINDOW_FACTOR",
    "PACKAGE_KWARG",
    "RECOLLECTION_IN_FLIGHT_EVENT",
    "RECOLLECTION_NOTHING_OFFERED_EVENT",
    "RECOLLECTION_PUBLISHED_EVENT",
    "RECOLLECTION_REFUSED_EVENT",
    "RECOLLECTION_REQUESTED_EVENT",
    "RECOLLECTION_UNPUBLISHED_EVENT",
    "RECOLLECT_PERMISSION_MISSING",
    "PendingRecollection",
    "RecollectionError",
    "RecollectionInFlightError",
    "RecollectionNotPermittedError",
    "RecollectionNothingOfferedError",
    "RecollectionReceipt",
    "can_request",
    "in_flight_window",
    "pending_recollection",
    "recollection_task_name",
    "request_recollection",
    "stamp",
]

logger: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

#: The events. The refusal is under `authorization.`, beside
#: `authorization.inventory_change_refused` and the mixin's
#: `authorization.refused`, because that prefix is what an operator alerts on
#: (`CPM-AD-13`); the other five are this module's own account of what it did.
RECOLLECTION_REFUSED_EVENT: Final[str] = "authorization.recollection_refused"
RECOLLECTION_IN_FLIGHT_EVENT: Final[str] = "recollection.in_flight"
RECOLLECTION_NOTHING_OFFERED_EVENT: Final[str] = "recollection.nothing_offered"
RECOLLECTION_REQUESTED_EVENT: Final[str] = "recollection.requested"
RECOLLECTION_PUBLISHED_EVENT: Final[str] = "recollection.published"
RECOLLECTION_UNPUBLISHED_EVENT: Final[str] = "recollection.unpublished"

#: The reason the refusal log line carries, on `INVENTORY_PERMISSION_MISSING`'s terms.
RECOLLECT_PERMISSION_MISSING: Final[str] = "recollect_permission_missing"

#: How many task time limits an open row may be old before it is a killed
#: worker's. Two: one limit is the longest a live run can be, and the second is
#: the margin for a worker that was mid-finalization when the limit fired.
IN_FLIGHT_WINDOW_FACTOR: Final[int] = 2

#: The two keywords every per-package collection task takes:
#: `def collect_x(*, package_id: int, force: bool = False)`. Restated here rather
#: than imported from `collectors/sweep.py` (`PACKAGE_KWARG`), which a request may
#: not reach; `tests/integration/django_apps/test_recollection.py` reconciles both
#: against the dispatcher's own constant and against the tasks' signatures.
PACKAGE_KWARG: Final[str] = "package_id"
FORCE_KWARG: Final[str] = "force"

#: How an instant is written into a refusal: the page's own `Y-m-d H:i` with the
#: literal `Z` every panel carries, so a refusal and the panel beside it agree.
#: The `Z` is honest because `TIME_ZONE` is UTC, which the suite asserts.
_STAMP_FORMAT: Final[str] = "%Y-%m-%d %H:%MZ"


class RecollectionError(ValueError):
    """A recollection was refused.

    One type for every refusal about the *request* -- nothing to offer, a run
    or a press still in flight, a caller that opened a transaction -- on the
    terms `collectors/inventory.py`'s `InventoryChangeError` states: the detail
    is in the message, and a caller branches on the subclasses below and on
    nothing else.
    """


class RecollectionNotPermittedError(RecollectionError):
    """The actor does not hold `RECOLLECT_PERMISSION`.

    The refusal about *who asked*, which is why it is its own type: the surface
    answers it 403 and every other refusal otherwise. A subclass, so a caller
    catching the parent still catches this.
    """


class RecollectionInFlightError(RecollectionError):
    """The package is in flight: a run on it, or a press for it, has not been answered.

    Answered 409 by the surface: the request is well-formed and the package is
    simply being collected already. The message names what is in flight and
    when it started, so the page can say what to wait for.
    """


class RecollectionNothingOfferedError(RecollectionError):
    """No swept collector's selection contains the package, so there is nothing to ask.

    Answered 400 by the surface. Refused before anything is written: an audit
    row naming no collector would record a request that asked for nothing.
    """


@dataclass(frozen=True, slots=True)
class RecollectionReceipt:
    """What one request did: the row, the names asked, the names left out, and the names the broker refused.

    `unpublished` is filled in *after* the row commits, by the `on_commit`
    callback that publishes -- which is why it is a list the callback appends to
    rather than a tuple fixed at construction. The service's own transaction is
    the outermost one (it refuses to run inside another), so the callback has run
    by the time the receipt reaches the caller.
    """

    #: The audit row, committed.
    recollection: PackageRecollection

    #: The collector names asked for, in registry order. The row's `collectors`.
    collectors: tuple[str, ...]

    #: The swept collectors whose selection did not contain the package. The
    #: row's `not_offered`.
    not_offered: tuple[str, ...]

    #: The names whose publish raised, in the order they were tried: a subset of
    #: `collectors`, empty when every hand-off landed. The handoff outcome lives
    #: here and in the log, never on the row.
    unpublished: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class PendingRecollection:
    """A press within the window that the ledger has not yet answered in full."""

    #: The audit row.
    recollection: PackageRecollection

    #: The collectors it asked for that have no finished run on the package
    #: started at or after the row's `observed_at`, in the row's order.
    awaiting: tuple[str, ...]


def in_flight_window() -> timedelta:
    """Return how far back an open run or an unanswered press still counts as in flight.

    Read from settings at call time rather than fixed at import, so a deployment
    that raises `CELERY_TASK_TIME_LIMIT` widens the window with it: no live run
    can outlast the hard limit, so a row older than `IN_FLIGHT_WINDOW_FACTOR`
    limits is a killed worker's.

    Returns:
        Twice the hard task time limit.

    """
    return timedelta(seconds=int(settings.CELERY_TASK_TIME_LIMIT) * IN_FLIGHT_WINDOW_FACTOR)


def stamp(instant: datetime) -> str:
    """Return an instant the way the page's panels print one.

    Args:
        instant: An aware instant.

    Returns:
        `YYYY-MM-DD HH:MMZ`, in UTC.

    """
    return instant.astimezone(UTC).strftime(_STAMP_FORMAT)


def recollection_task_name(collector: str) -> str:
    """Return the declared name of one collector's per-package task.

    Args:
        collector: The collector's declared name.

    Returns:
        `cpm.collect.<collector>` -- `collectors/sweep.py`'s `collection_task_name`
        rule, applied through `core/queues.py` so nothing here imports the
        dispatcher.

    """
    return task_name(Queue.COLLECT, collector)


def can_request(actor: User | AnonymousUser) -> bool:
    """Report whether an actor may ask for a recollection.

    What the package page reads to decide whether to draw the button. The one
    `has_perm` in this module, shared with `request_recollection` so the audit
    licenses a single read.

    Args:
        actor: The person on the request, or Django's anonymous user.

    Returns:
        True when the actor is authenticated and holds `RECOLLECT_PERMISSION`. A
        superuser is admitted because `has_perm` admits one -- the mitigation
        being the audit row, which names them.

    """
    return bool(actor.is_authenticated and actor.has_perm(RECOLLECT_PERMISSION))


def pending_recollection(package_id: int, *, now: datetime) -> PendingRecollection | None:
    """Return the newest press for one package within the window that the ledger has not answered.

    The second half of "in flight" -- see the module docstring. Read by the
    service to refuse and by the page to say what a reviewer is waiting for.

    Args:
        package_id: The package.
        now: The instant the window is measured back from.

    Returns:
        The newest unanswered press and the collectors it is still awaiting, or
        `None` when every press within the window has been answered.

    """
    since = now - in_flight_window()
    rows = PackageRecollection.objects.filter(package_id=package_id, observed_at__gte=since).order_by(
        "-observed_at", "-id"
    )
    for row in rows:
        answered = set(
            CollectionRun.objects.filter(
                package_id=package_id,
                started_at__gte=row.observed_at,
                finished_at__isnull=False,
            ).values_list("collector", flat=True),
        )
        awaiting = tuple(str(name) for name in row.collectors if name not in answered)
        if awaiting:
            return PendingRecollection(recollection=row, awaiting=awaiting)
    return None


def request_recollection(*, package_id: int, actor: User, clock: Clock) -> RecollectionReceipt:
    """Ask for every applicable collector to re-run on one package, and record who asked.

    Args:
        package_id: The package, by the integer primary key `CPM-AD-3` fixes.
        actor: The person asking. Must hold `RECOLLECT_PERMISSION`.
        clock: The clock the audit row's instant and the in-flight window are
            read from (`CPM-AD-26`).

    Returns:
        The receipt: the row, the names asked and not offered, and the names
        whose publish raised.

    Raises:
        RecollectionNotPermittedError: When the actor does not hold the
            permission. Logged with the actor first; nothing is written. Checked
            before anything else, so an unpermitted actor learns nothing about
            the package's ledger.
        RecollectionNothingOfferedError: When no swept collector's selection
            contains the package. Nothing is written.
        RecollectionInFlightError: When a run on the package, or a press for
            it, started within the window and has not been answered. Nothing is
            written and nothing is published.
        RecollectionError: When the package does not exist, or when a caller
            has already opened a transaction -- see the module docstring.

    """
    _require_permitted(actor)
    now = clock.now()
    offered, not_offered = _selection(package_id)
    _require_something_offered(package_id, offered=offered, not_offered=not_offered)
    _require_no_outer_transaction()

    with transaction.atomic():
        _lock(package_id)
        _require_nothing_in_flight(package_id, now=now)
        row = PackageRecollection.objects.create(
            package_id=package_id,
            actor=actor,
            collectors=list(offered),
            not_offered=list(not_offered),
            trace_id=current_trace_id(),
            observed_at=now,
        )
        receipt = RecollectionReceipt(recollection=row, collectors=tuple(offered), not_offered=tuple(not_offered))
        logger.info(
            RECOLLECTION_REQUESTED_EVENT,
            recollection_id=row.pk,
            package_id=package_id,
            user_id=actor.pk,
            idp_subject=getattr(actor, IDENTITY_FIELD, ""),
            collectors=list(offered),
            not_offered=list(not_offered),
            trace_id=row.trace_id,
        )
        transaction.on_commit(lambda: _publish(receipt))
    return receipt


def _selection(package_id: int) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return the swept collectors whose selection contains the package, and those whose does not.

    Args:
        package_id: The package.

    Returns:
        `(offered, not_offered)`, each in registry order.

    """
    offered: list[str] = []
    not_offered: list[str] = []
    for collector in swept_collectors():
        (offered if selects(collector, package_id) else not_offered).append(collector.name)
    return tuple(offered), tuple(not_offered)


def _require_permitted(actor: User) -> None:
    """Refuse an actor who may not ask, and say so in the log.

    On `collectors/inventory.py`'s terms: the log record is written for an
    anonymous user too, and both identity fields are read defensively.

    Args:
        actor: The person asking.

    Raises:
        RecollectionNotPermittedError: When `can_request` answers False. Raised
            after the log record.

    """
    if can_request(actor):
        return
    logger.warning(
        RECOLLECTION_REFUSED_EVENT,
        reason=RECOLLECT_PERMISSION_MISSING,
        user_id=getattr(actor, "pk", None),
        idp_subject=getattr(actor, IDENTITY_FIELD, ""),
    )
    message = (
        f"user {getattr(actor, 'pk', None)!r} does not hold {RECOLLECT_PERMISSION!r}, so this recollection is "
        f"refused. A manual recollection spends a source's allowance on somebody's say-so (CPM-AD-13): asking "
        f"for one needs the permission core/roles.py grants to the security-reviewer and leadership role groups."
    )
    raise RecollectionNotPermittedError(message)


def _require_something_offered(package_id: int, *, offered: tuple[str, ...], not_offered: tuple[str, ...]) -> None:
    """Refuse a request that would ask nobody, before anything is written.

    Args:
        package_id: The package.
        offered: The names whose selection contains the package.
        not_offered: The names whose selection does not.

    Raises:
        RecollectionNothingOfferedError: When `offered` is empty -- because every
            swept collector's selection excludes the package, or because nothing
            swept is registered at all. The message says which.

    """
    if offered:
        return
    logger.info(RECOLLECTION_NOTHING_OFFERED_EVENT, package_id=package_id, not_offered=list(not_offered))
    if not_offered:
        message = (
            f"no collector can be asked about package {package_id}: each swept collector's own selection "
            f"excludes it ({list(not_offered)}). A forced run of any of them would only write a failed row "
            f"with a refusal on the record. Resolve the package's identity first, or declare the source the "
            f"collector is waiting on (docs/conda-sentinel/operations.md)."
        )
    else:
        message = (
            f"no collector is registered that is swept per package, so nothing can be asked about package "
            f"{package_id}. A component with no swept collector has nothing for a recollection to enqueue."
        )
    raise RecollectionNothingOfferedError(message)


def _require_no_outer_transaction() -> None:
    """Refuse to run inside a transaction somebody else opened.

    Raises:
        RecollectionError: When `connection.in_atomic_block` is already True.
            The publish is `on_commit` and the caller reads the receipt straight
            after; inside an outer block the callback would fire after the
            caller had composed its message, and the message would say every
            collector was handed off whether or not the broker took it.

    """
    if not connection.in_atomic_block:
        return
    message = (
        "request_recollection was called inside an open transaction, and refuses: its publish runs on commit "
        "and its receipt is read straight after, so an outer transaction would make the receipt -- and the "
        "page's message -- claim a hand-off that has not happened. The view that calls it is exempt from "
        "ATOMIC_REQUESTS for exactly this reason (surface/views.py, PackageRecollectView)."
    )
    raise RecollectionError(message)


def _lock(package_id: int) -> None:
    """Lock the package row for the rest of the transaction, so concurrent presses are serialised.

    Args:
        package_id: The package.

    Raises:
        RecollectionError: When no such package exists. The surface 404s first;
            this is the service's own refusal for a caller that did not.

    """
    if Package.objects.select_for_update().filter(pk=package_id).exists():
        return
    message = f"no package has primary key {package_id}, so there is nothing to recollect."
    raise RecollectionError(message)


def _require_nothing_in_flight(package_id: int, *, now: datetime) -> None:
    """Refuse while a run on the package, or a press for it, has not been answered.

    Args:
        package_id: The package.
        now: The instant the window is measured back from.

    Raises:
        RecollectionInFlightError: Naming the newest open run's collector and
            start, or the pending press, whichever the ledger shows.

    """
    window = in_flight_window()
    running = runs_in_flight(package_id, now=now, window=window)
    if running:
        newest = running[0]
        logger.info(
            RECOLLECTION_IN_FLIGHT_EVENT,
            package_id=package_id,
            collector=newest.collector,
            run_id=newest.pk,
            started_at=newest.started_at.isoformat(),
            in_flight=len(running),
        )
        message = (
            f"package {package_id} is being collected already: {newest.collector} started at "
            f"{stamp(newest.started_at)} and has not finished ({len(running)} run(s) in flight). A second "
            f"recollection would enqueue a second set of runs behind the first; wait for the page to show them "
            f"finalised. An open run older than {window} is a killed worker's and does not count."
        )
        raise RecollectionInFlightError(message)

    pending = pending_recollection(package_id, now=now)
    if pending is None:
        return
    row = pending.recollection
    logger.info(
        RECOLLECTION_IN_FLIGHT_EVENT,
        package_id=package_id,
        recollection_id=row.pk,
        asked_at=row.observed_at.isoformat(),
        awaiting=list(pending.awaiting),
    )
    message = (
        f"package {package_id} is being collected already: a recollection asked for at {stamp(row.observed_at)} "
        f"is still awaiting {', '.join(pending.awaiting)}. Its tasks are queued behind whatever the collect "
        f"queue is draining and run when a worker reaches them; wait for the page to show them finalised. A "
        f"press older than {window} no longer counts."
    )
    raise RecollectionInFlightError(message)


def _publish(receipt: RecollectionReceipt) -> None:
    """Publish one per-package collection per asked collector, after the row has committed.

    **By name, never by importing the task** -- see the module docstring and
    `core/jobs.py`'s `_publish`, whose shape this is. Anything that raises on one
    name is logged and named on the receipt; the others are still tried, and the
    row already stands.

    Args:
        receipt: The receipt to publish for, and to record refusals on.

    """
    from celery import current_app  # noqa: PLC0415 - after the settings are loaded, as core/jobs.py imports it

    row = receipt.recollection
    expires = int(in_flight_window().total_seconds())
    for collector in receipt.collectors:
        name = recollection_task_name(collector)
        try:
            # `ignore_result=True` because **the run ledger is the result**: each
            # task opens a `CollectionRun` row the page reads, and a Celery result
            # beside it would be a second record in a store nothing consults.
            # `expires` bounds the message to the in-flight window, and
            # `retry=False` makes a down broker fail here, now, rather than stall
            # the request for the publisher's retry policy.
            current_app.send_task(
                name,
                kwargs={PACKAGE_KWARG: row.package_id, FORCE_KWARG: True},
                ignore_result=True,
                expires=expires,
                retry=False,
            )
        # The publish boundary: the row has committed, and a raise here is a 500
        # with later names untried. `logger.exception` keeps the traceback.
        except Exception:
            logger.exception(
                RECOLLECTION_UNPUBLISHED_EVENT,
                recollection_id=row.pk,
                package_id=row.package_id,
                collector=collector,
                task=name,
            )
            receipt.unpublished.append(collector)
            continue
        logger.info(
            RECOLLECTION_PUBLISHED_EVENT,
            recollection_id=row.pk,
            package_id=row.package_id,
            collector=collector,
            task=name,
            expires=expires,
            trace_id=row.trace_id,
        )
