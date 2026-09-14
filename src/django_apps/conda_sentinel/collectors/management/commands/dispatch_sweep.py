"""A collector sweep as a command an operator runs and a deployment schedules (`CPM-OPERATE-S02`).

Beat fires one `cpm.collect.sweep` per swept collector at that collector's
cadence, and a fresh stack therefore sweeps nothing for a day and weekly surfaces
for a week: interval entries start their clock when they are created. Until this
command the way round that was a `stack-shell -c` loop over
`collect_sweep.delay(...)`. This is the same enqueue as a command --
`pixi run stack-run dispatch_sweep pypi_release feedstock` by hand,
`pixi run sweep` (`dispatch_sweep --all`) as `component.toml`'s `sweep` admin
process, which a deployment repository can run once after a first deploy.

**It enqueues the dispatch; it dispatches nothing itself and collects nothing.**
`CPM-AD-9`. Each name becomes one `collect_sweep.delay(collector=name)`, the
task's own keyword contract, and the dispatch -- select, enqueue per package,
finalize one ledger row scoped to no package -- happens wherever settings put it:
on the `collect` queue under the stack's `CELERY_TASK_ALWAYS_EAGER=0`, inline
here under the default environment and the suite. The flag is read to choose
which report line to print, never to change what the command does.

**It validates before it enqueues, in the dispatcher's own vocabulary.**
`collectors/sweep.py`'s `dispatch()` refuses a blank, reserved, unregistered or
unswept name, and one whose derived per-package task Celery does not hold -- but
inside the run recorder, leaving a `failed` ledger row behind. A typo on a
command line should not be a run on the record, so every name is checked against
the registry and against Celery's task registry first, with the same words and a
list of the names that *are* swept. A name given twice is refused too: the second
dispatch would only ever be finalised `skipped` behind the first. Validation
makes no outbound call and writes nothing.

**`--all` is the swept set, derived rather than listed.** Every registered
collector whose `selectable_packages()` is not `None`, in the registry's own
deterministic order -- `core/registry.py`'s `swept_collectors()`, the same rule
the dispatcher applies, so whatever it refuses is left out here.
`tests/unit/django_apps/test_operator_commands.py` reconciles that set against
the collectors `CELERY_BEAT_SCHEDULE` names, so the two cannot drift. A registry
with nothing swept is refused rather than reported as an empty success.

**The count comes from the ledger, and from this call's row.** `collect_sweep`
returns the run state as a string; how many packages the dispatch enqueued lives
in the dispatch row's `detail`. After an eager run the highest dispatch id for
that collector recorded *before* the call is the watermark, and only a row above
it is read -- on a stack with history, or beside a concurrent worker, "the newest
row" could belong to another run. No such row means the count is reported as
unrecorded, never as zero.

**A broker that refuses mid-way is reported as far as it got.** Under a worker's
settings each `.delay()` is one publish; if the broker refuses the third of nine,
the first two are enqueued and stay enqueued, and the refusal says so by count
and by name rather than leaving the operator to reconstruct it from the log.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING
from typing import Any
from typing import Final

import structlog
from celery import current_app
from django.core.management.base import BaseCommand
from django.core.management.base import CommandError
from django.db.models import Max
from kombu.exceptions import OperationalError

from conda_sentinel.collectors.sweep import COLLECTOR_KWARG
from conda_sentinel.collectors.sweep import RESERVED_COLLECTOR_NAME
from conda_sentinel.collectors.sweep import SWEEP_TASK_NAME
from conda_sentinel.collectors.sweep import collection_task_name
from conda_sentinel.collectors.tasks import collect_sweep
from conda_sentinel.core.models import CollectionRun
from conda_sentinel.core.operator_commands import WHERE_TO_WATCH_A_COLLECTION
from conda_sentinel.core.operator_commands import runs_eagerly
from conda_sentinel.core.registry import registrations
from conda_sentinel.core.registry import swept_collectors

if TYPE_CHECKING:
    from argparse import ArgumentParser

__all__ = [
    "SWEEP_ENQUEUED_EVENT",
    "SWEEP_RAN_EVENT",
    "UNRECORDED",
    "Command",
    "offered_count",
    "swept_collector_names",
]

logger: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

#: The two events, one per outcome the command can report for one collector.
#: Named for the command rather than under `collectors/sweep.py`'s `sweep.`
#: prefix: those three are the dispatch's own account of what it did, and this
#: is the account of who asked for it.
SWEEP_ENQUEUED_EVENT: Final[str] = "dispatch_sweep.enqueued"
SWEEP_RAN_EVENT: Final[str] = "dispatch_sweep.ran"

#: The name the positional arguments are parsed under.
COLLECTORS_ARGUMENT: Final[str] = "collectors"

#: What the report says for a run id or a count the ledger does not hold. A word
#: rather than `0`: zero is a claim about the dispatch, and this is a statement
#: that the dispatch left no row this command can see.
UNRECORDED: Final[str] = "unrecorded"

#: How the dispatch row's `detail` states the enqueued count. Every ending
#: `collectors/sweep.py` writes that enqueued anything says `enqueued N` -- `N
#: selected package(s)`, `N of M selected package(s)`, `N package(s) ... and then
#: stopped` -- and every ending that enqueued nothing says `no packages`,
#: `nothing`, `any of` or `enqueued nothing rather than`, none of which this
#: matches. So the absence of a match on a row that exists is a count of zero.
#: `tests/integration/django_apps/test_operator_commands.py` proves it against a
#: row the dispatcher actually wrote, not against a copy of its sentence.
ENQUEUED_COUNT: Final[re.Pattern[str]] = re.compile(r"\benqueued (\d+)\b")


def swept_collector_names() -> tuple[str, ...]:
    """Return the names `--all` dispatches, in the order it dispatches them.

    Returns:
        The declared names of `core/registry.py`'s `swept_collectors()` -- the
        predicate lived here until `CPM-OPERATE-S08` moved it beside the registry,
        so the page's "Collect now" and this command derive the swept set from one
        function.

    """
    return tuple(collector.name for collector in swept_collectors())


def offered_count(detail: str) -> int:
    """Return how many packages a dispatch row's `detail` says were enqueued.

    Args:
        detail: The ledger row's `detail`, exactly as `collectors/sweep.py` wrote it.

    Returns:
        The count, or zero for an ending that enqueued nothing.

    """
    found = ENQUEUED_COUNT.search(detail)
    return int(found.group(1)) if found else 0


def _highest_dispatch_id(collector: str) -> int:
    """Return the highest run-scoped ledger id for one collector, or zero when there is none.

    Args:
        collector: The collector's declared name.

    Returns:
        The watermark a row written by this call must sit above.

    """
    highest = CollectionRun.objects.filter(collector=collector, package__isnull=True).aggregate(Max("pk"))
    return int(highest["pk__max"] or 0)


def _dispatch_row_above(collector: str, watermark: int) -> CollectionRun | None:
    """Return the newest run-scoped ledger row for one collector written after the watermark.

    Args:
        collector: The collector's declared name.
        watermark: The highest id that existed before this call's `.delay()`.

    Returns:
        The row, or `None` when the call left none -- which is reported as
        unrecorded rather than invented.

    """
    return (
        CollectionRun.objects.filter(collector=collector, package__isnull=True, pk__gt=watermark)
        .order_by("-pk")
        .first()
    )


class Command(BaseCommand):
    """Enqueue one dispatch per named collector, or per swept collector with `--all`."""

    help = "Admin process: enqueue a full-inventory dispatch for the named collectors, or --all (cpm.collect.sweep)."

    def add_arguments(self, parser: ArgumentParser) -> None:
        """Declare the names, and the switch that stands for all of them.

        Args:
            parser: The parser Django hands every management command.

        """
        parser.add_argument(
            COLLECTORS_ARGUMENT,
            nargs="*",
            metavar="COLLECTOR",
            help="Registered per-package collector names, dispatched in this order.",
        )
        parser.add_argument(
            "--all",
            action="store_true",
            dest="all_swept",
            help="Dispatch every registered collector that is swept per package, in the registry's order.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        """Validate every name, then enqueue one dispatch per name and report each.

        Args:
            *args: Unused; Django's management interface passes none.
            **options: The parsed options: the positional names and `all_swept`.

        Raises:
            CommandError: Before anything is enqueued, when the arguments name
                nothing, name something and also say `--all`, repeat a name, or
                name a collector the dispatch would refuse; after some were
                enqueued, when the broker refused a later one, saying how many
                went and which did not.

        """
        names = self._selected(list(options.get(COLLECTORS_ARGUMENT) or ()), all_swept=bool(options.get("all_swept")))
        for position, name in enumerate(names):
            try:
                self._dispatch(name)
            except OperationalError as refusal:
                message = (
                    f"the broker refused {SWEEP_TASK_NAME} for {name!r} ({type(refusal).__name__}: {refusal}). "
                    f"{position} of {len(names)} dispatch(es) had been enqueued before it and stay enqueued: "
                    f"{list(names[:position])}. Not enqueued: {list(names[position:])}."
                )
                raise CommandError(message) from refusal

    def _selected(self, names: list[str], *, all_swept: bool) -> tuple[str, ...]:
        """Return the names to dispatch, refusing anything the dispatcher would.

        Args:
            names: The positional arguments, in order.
            all_swept: Whether `--all` was passed.

        Returns:
            The names, stripped, in the order they will be dispatched.

        Raises:
            CommandError: When `--all` is combined with names or finds nothing
                swept; when neither is given; when a name repeats; or when a name
                is blank, reserved, unregistered, not swept per package, or has
                no per-package task in Celery's registry. Each refusal lists the
                swept names.

        """
        swept = swept_collector_names()
        if all_swept and names:
            message = (
                f"--all dispatches every swept collector, so do not also name {names}. "
                f"Name collectors or pass --all, not both. The swept collectors are {list(swept)}."
            )
            raise CommandError(message)
        if all_swept:
            if not swept:
                message = (
                    "--all found no collector to dispatch: nothing registered declares selectable_packages, so "
                    "nothing is swept per package. A component with no swept collector has nothing for this "
                    "command to enqueue."
                )
                raise CommandError(message)
            return swept
        if not names:
            message = f"name at least one collector to dispatch, or pass --all. The swept collectors are {list(swept)}."
            raise CommandError(message)

        stripped = [name.strip() for name in names]
        for name in stripped:
            self._validate(name, swept=swept)
        repeated = sorted({name for name in stripped if stripped.count(name) > 1})
        if repeated:
            message = (
                f"{repeated} named more than once. A second dispatch of a collector whose first is still "
                f"draining is finalised skipped, so name each collector once."
            )
            raise CommandError(message)
        return tuple(stripped)

    @staticmethod
    def _validate(name: str, *, swept: tuple[str, ...]) -> None:
        """Refuse one name on any of the dispatcher's own grounds.

        Args:
            name: The stripped name.
            swept: The names `--all` would dispatch, for the refusal's listing.

        Raises:
            CommandError: On the first ground the name fails.

        """
        if not name:
            message = f"a blank collector name dispatches nothing. The swept collectors are {list(swept)}."
            raise CommandError(message)
        if name == RESERVED_COLLECTOR_NAME:
            message = (
                f"collector={name!r} is the reserved name: it derives the dispatch task's own name "
                f"{SWEEP_TASK_NAME!r}, so dispatching it would enqueue the dispatch once per package. "
                f"The swept collectors are {list(swept)}."
            )
            raise CommandError(message)
        registered = registrations()
        if name not in registered:
            message = (
                f"no collector is registered under name={name!r}, so there is nothing to dispatch. "
                f"The swept collectors are {list(swept)}."
            )
            raise CommandError(message)
        if name not in swept:
            message = (
                f"{registered[name].__name__} (name={name!r}) declares no selectable_packages, so it is not "
                f"swept one package at a time and cannot be dispatched (CPM-AD-25). "
                f"The swept collectors are {list(swept)}."
            )
            raise CommandError(message)
        task_name = collection_task_name(name)
        if task_name not in current_app.tasks:
            message = (
                f"{registered[name].__name__} (name={name!r}) would be collected by task {task_name!r}, and "
                f"Celery's registry does not hold it, so a dispatch would be refused on the ledger. Every "
                f"per-package collection task is declared in collectors/tasks.py. "
                f"The swept collectors are {list(swept)}."
            )
            raise CommandError(message)

    def _dispatch(self, name: str) -> None:
        """Enqueue one dispatch and report where it went.

        Args:
            name: A validated collector name.

        Raises:
            CommandError: When the task ran eagerly and failed without
                propagating, so the result carries its exception.

        """
        watermark = _highest_dispatch_id(name)
        result = collect_sweep.delay(**{COLLECTOR_KWARG: name})

        if not runs_eagerly():
            logger.info(SWEEP_ENQUEUED_EVENT, task=SWEEP_TASK_NAME, collector=name, task_id=result.id)
            self.stdout.write(f"{name}: enqueued {SWEEP_TASK_NAME} as task {result.id}; {WHERE_TO_WATCH_A_COLLECTION}")
            return

        if result.failed():
            message = f"{SWEEP_TASK_NAME} for {name!r} ran inline and failed: {result.result!r}"
            raise CommandError(message)

        state = str(result.result)
        row = _dispatch_row_above(name, watermark)
        run_id: int | str = row.pk if row is not None else UNRECORDED
        offered: int | str = offered_count(row.detail) if row is not None else UNRECORDED
        logger.info(
            SWEEP_RAN_EVENT,
            task=SWEEP_TASK_NAME,
            collector=name,
            task_id=result.id,
            run_id=run_id,
            state=state,
            offered=offered,
        )
        self.stdout.write(f"{name}: run {run_id} {state}, offered {offered} package(s)")
