"""The recording seam: a run row exists before the first outbound call, and after it.

`CPM-AD-2`'s exemption is only worth what the write order makes it worth. A row
written when a run *finishes* records nothing about a run that did not, so a
worker killed between the outbound call and the insert is indistinguishable from
one that never started -- and "started and never finished", which `CPM-FR-38`
and `CPM-UJ-3` both need, cannot be asked at all. `R-05` is that failure. So the
order is fixed here rather than left to each collector: insert `running`, yield,
finalize in a `finally`.

**Every exit path finalizes, and an exception still propagates unchanged.** The
`finally` runs for a return, for a `raise`, and for a generator being closed. An
exception leaving the body finalizes the row to `failed` with the exception's
type and message in `detail`, and is then re-raised exactly as it was: the
recorder never swallows and never logs-and-continues, which inherited `CG-3`
requires. `EVIDENCE.03-INT-002` is the case that proves both halves at once --
the row is there, and the caller still sees its own exception.

**The recorder reads before it writes.** Since `CPM-EVIDENCE-S09` made the
ledger's package reference a real relation, `collection_run` issues a `SELECT`
against `packages` before it inserts anything, to refuse a key that names no
package with a message rather than with the constraint's. So this module is no
longer callable without a database even for a run it is going to reject, and its
validation can fail with a `DatabaseError` of its own -- which is a widening of
the contract rather than a detail, and both recorders' docstrings say so.

**Why the guarantee is really about autocommit, and why that is stated rather
than enforced.** The row survives a killed worker only because the insert is
committed before the outbound call. A caller that wraps a recorder in
`transaction.atomic()` and is then killed loses the row, and the ledger is back
to recording nothing. A runtime guard on `connection.in_atomic_block` is not
available to state it: pytest's `django_db` runs every test inside exactly such a
block, so the guard would refuse the entire suite. The constraint is therefore
written down here, and it belongs to whichever story first writes a collector
that could break it (`CPM-EVIDENCE-S05`).

**Time and correlation both come from outside.** The instants are the injected
`Clock`'s (`CPM-AD-26`) -- this module calls no wall clock. The `trace_id` is the
active span's, formatted `032x`, which is exactly what
`config/observability/logging.py` puts on every log line (`CPM-AD-15`); the
product adds no correlation scheme of its own, so a row and the logs of the run
that wrote it join on the same value. `core` reads `opentelemetry.trace`
directly and must not import `config`, which would invert the dependency
direction inherited `AD-4` fixes.

**Shape of a recorded run.**

```python
with collection_run(collector="pypi", clock=clock, package_id=package.pk) as run:
    payload = client.fetch(name)          # the row is already `running`
    if payload.partial:
        run.partial(detail="3 of 5 sources answered")
```

**On the `AD-` prefix.** A bare `AD-n` in this repository is an *inherited*
platform decision; a decision from this product's own architecture spine always
carries the `CPM-` prefix.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING

import structlog
from django.db import DatabaseError
from opentelemetry import trace

from conda_sentinel.core.clock import is_aware
from conda_sentinel.core.models import CollectionRun
from conda_sentinel.core.models import PolicyRun
from conda_sentinel.core.runs import RunLedgerError
from conda_sentinel.core.runs import RunState
from conda_sentinel.identity.models import Package

if TYPE_CHECKING:
    from collections.abc import Iterator
    from datetime import datetime

    from conda_sentinel.core.clock import Clock
    from conda_sentinel.core.models import RunLedgerModel

__all__ = [
    "RunHandle",
    "abandon",
    "collection_run",
    "current_trace_id",
    "policy_run",
]

logger = structlog.get_logger(__name__)

#: The width `CPM-AD-15`'s correlation identifier is formatted to.
#:
#: `config/observability/logging.py:52` carries its own `"032x"` literal and
#: always will: `core` may not import `config` (inherited `AD-4`), so there is no
#: constant the two can share. This name is therefore local convenience, and it
#: is emphatically *not* what keeps the two in step -- what does that is
#: `tests/integration/django_apps/test_run_ledger.py`, which records a run inside
#: a real span and asserts the row's `trace_id` equals what `add_otel_context`
#: itself emitted. A change to either format fails there.
TRACE_ID_FORMAT = "032x"

#: What `current_trace_id` answers when the active span carries no valid context.
#: The empty string rather than `None`: the column is a non-null `CharField`, and
#: a run performed outside an instrumented request or task is still a run that
#: happened.
NO_TRACE_ID = ""

#: The fields finalization writes back, and the only ones it may.
#: `instance.save(update_fields=...)` rather than `queryset.update()` or
#: `bulk_update()`: `EVIDENCE.02-AUDIT-002` sweeps all of `src/` for those forms
#: regardless of which table they touch, and taking an exemption there to
#: finalize a ledger row would license the shape for the whole file.
#:
#: It is also the narrower write. The recorder holds an instance from *before*
#: the run, so a full `save()` would write every column back from a stale copy
#: and undo anything the body changed on the row -- which is why
#: `test_finalization_writes_only_the_fields_it_names` exists.
FINALIZED_FIELDS = ("status", "detail", "finished_at")

#: The event a failed finalization is logged under. Named so the case that
#: asserts the log and the code that emits it cannot drift.
FINALIZATION_FAILED_EVENT = "run_ledger_finalization_failed"


def current_trace_id() -> str:
    """Return the active span's trace id, formatted as the platform formats it.

    The branch is on the span context being **valid**, not on the span
    recording, and that is deliberate: it is the same test `add_otel_context`
    makes, so a row and the log lines of the run that wrote it agree about when
    there is an id to carry. A `NonRecordingSpan` propagated from an upstream
    caller has a perfectly valid context and its id belongs on the row.

    Returns:
        The 32-character lowercase hexadecimal trace id of the active span, or
        `NO_TRACE_ID` when its context is invalid -- outside an instrumented
        request or task, or with the SDK disabled. Never raises and never blocks
        a run: an uncorrelated row is worth more than no row.

    """
    span_context = trace.get_current_span().get_span_context()
    if not span_context.is_valid:
        return NO_TRACE_ID
    return format(span_context.trace_id, TRACE_ID_FORMAT)


def _require_named(value: str, *, field: str) -> str:
    """Refuse a run that does not say what performed it.

    `CPM-FR-39` needs every run traceable to the process that performed it, and a
    row whose `collector` is `""` is traceable to nothing. The refusal is here
    rather than at the column because a blank `CharField` is perfectly valid SQL:
    the row would be written, would be counted, and would be useless.

    Args:
        value: The collector or policy name the caller supplied.
        field: Which one, for the message.

    Returns:
        The value, unchanged.

    Raises:
        RunLedgerError: When the value is empty or is only whitespace.

    """
    if not value.strip():
        message = (
            f"a run needs a {field}; {value!r} names nothing. Every run is traceable to the code that "
            f"performed it (CPM-FR-39), and a blank name is a row nothing can be traced to."
        )
        raise RunLedgerError(message)
    return value


def _require_aware(instant: datetime, *, field: str) -> datetime:
    """Refuse a naive instant, on the same terms `AppendOnlyModel.save()` does.

    The consequence lands far from here and is the reason this is a refusal
    rather than a conversion: `USE_TZ` is on, so Django warns and stores a naive
    value as if it were UTC, and a policy run's cut-off silently shifted by the
    writer's offset selects a different set of evidence on every replay --
    `CPM-FR-22` promises the opposite.

    Args:
        instant: The value the caller supplied.
        field: Which field it was, for the message.

    Returns:
        The instant, unchanged.

    Raises:
        RunLedgerError: When the instant carries no usable offset.

    """
    if not is_aware(instant):
        message = (
            f"a run was recorded with a naive {field} ({instant!r}). The instant comes from a Clock, which "
            f"always answers in UTC (CPM-AD-26); a naive value has no offset to interpret and would make "
            f"every replay read a different evidence set rather than failing."
        )
        raise RunLedgerError(message)
    return instant


def _require_package_key(package_id: int | None) -> int | None:
    """Refuse a package reference that cannot be, or is not, a package's primary key.

    Two refusals, and they are different failures. A **negative** key cannot be a
    primary key at all: Django declared the old column `PositiveBigIntegerField`,
    PostgreSQL enforced that through a check constraint and SQLite did not
    enforce it at all, so a negative value was a row on a developer machine and
    an `IntegrityError` in the gate. An **unknown** key is a well-formed integer
    that names no package -- `CPM-EVIDENCE-S09` made the column a real
    `ForeignKey`, so a row carrying one is a row the database refuses.

    **The unknown key is refused here rather than left to the constraint**, for
    the reason the negative one is: the two backends do not agree about when the
    refusal arrives. Django declares its SQLite foreign keys
    `DEFERRABLE INITIALLY DEFERRED`, so a violation surfaces at `COMMIT` -- which
    inside a test's transaction, or a task that goes on to do more work, is a
    long way from the call that caused it. Refusing before the insert makes both
    backends answer at the same moment, with a message naming the key rather than
    with the backend's own text naming a constraint.

    It costs one `SELECT` per package-scoped run, which is the right trade: a run
    is already about to make an outbound call, and a ledger row pointing at a
    package that does not exist is exactly what `CPM-AD-3` exists to prevent.

    **It is a check-then-act and it is not serialized**, which is worth saying
    rather than waving at. `CPM-AD-25` says no package row is ever deleted, but
    that is a rule about what this product's writers may do -- nothing enforces
    it, and `Package.delete()` is a method Django will happily run. A package
    removed between this query and the insert would leave the row to the foreign
    key, which refuses it at the constraint: the failure is then an
    `IntegrityError` naming a constraint instead of this message naming the key,
    which is worse to read and still correct. The relation is the backstop; this
    is the message.

    Args:
        package_id: The key the caller supplied, or `None` for a run that is not
            scoped to one package.

    Returns:
        The key, unchanged. `None` stays `None`: it means the run was not scoped
        to one package, which is a legitimate run and not a missing key, so it is
        never looked up.

    Raises:
        RunLedgerError: When the key is negative, or when it names no package.
        DatabaseError: From the lookup itself, unchanged. This function is
            validation that reads, so a database that is unreachable or refusing
            fails here rather than at the insert -- earlier than before, and
            still before any row is written.

    """
    if package_id is None:
        return None
    if package_id < 0:
        message = (
            f"package_id={package_id!r} is not a package primary key. `CPM-AD-3` fixes the reference as an "
            f"integer primary key; omit it entirely for a run that is not scoped to one package."
        )
        raise RunLedgerError(message)
    if not Package.objects.filter(pk=package_id).exists():
        message = (
            f"package_id={package_id!r} names no package, so the run cannot be recorded against it. "
            f"`CPM-AD-3` makes this reference a real relation; resolve the package first (identity's "
            f"resolution service creates the shell), or omit the key for a run that is not scoped to one "
            f"package."
        )
        raise RunLedgerError(message)
    return package_id


class RunHandle:
    """What the body of a recorded run says about how it went.

    Yielded by the recorders below. A body that declares nothing has succeeded --
    that is the ordinary path and it should cost nothing to write -- and a body
    that ends the run some other way says so once.

    **Once is the rule, and it is enforced.** `succeeded()` followed by
    `failed()` is a defect at the call site: the row can hold one state, and a
    recorder that silently kept the first would write `succeeded` for a run whose
    body went on to decide it had failed. So the second declaration raises
    `RunLedgerError` naming both states, at the call site, where the mistake is.

    **An exception overrides whatever was declared**, through `_raised()`. That
    is deliberate and it is the one case where a second state is accepted rather
    than refused: a body that declared `partial` and then raised did not complete
    partially, it failed, and the exception is the more truthful record of what
    happened. The refusal above is about a caller contradicting itself; this is
    about reality contradicting the caller.

    `_raised` is private because it is the *recorder's* half of this object, not
    the body's: it is the one method that overwrites a declaration instead of
    refusing to, so a body able to reach it could quietly replace its own ending
    -- and could do it twice. `_recorded` below is the only caller, and it says
    so where it calls it.
    """

    def __init__(self, *, run: RunLedgerModel | None = None) -> None:
        """Start with nothing declared, which finalizes as a success.

        Args:
            run: The ledger row this handle is for, supplied by `_recorded`. It
                is what a body needs to *reference* the run it is inside --
                `CPM-EVIDENCE-S07`'s passes key their derived rows
                `(package, policy_run)` and the rollup is stamped with the run --
                and the row is not otherwise reachable: it is created inside the
                recorder, so a caller that went looking for it would have to
                guess at "the newest running row", which is a different row the
                moment two runs overlap.

                Optional, and `None` by default, so a case can construct a bare
                handle to exercise the declaration rules without a database.

        """
        self._run = run
        self._state: RunState | None = None
        self._detail: str = ""

    @property
    def run(self) -> RunLedgerModel:
        """Return the ledger row this run is being recorded against.

        Returns:
            The saved row. It carries a primary key: `_recorded` inserts it
            before yielding, which is the whole ordering guarantee this module
            exists for.

        Raises:
            RunLedgerError: When this handle was constructed without a row --
                which only a test does. Refused rather than answering `None`,
                because every product caller reaches this through a recorder and
                a `None` would push all of them into a check none of them needs.

        """
        if self._run is None:
            message = (
                "this handle was constructed without a ledger row, so there is no run to reference. "
                "A run's row is created by the recorders in this module; a bare RunHandle() exists only "
                "to exercise the declaration rules."
            )
            raise RunLedgerError(message)
        return self._run

    @property
    def state(self) -> RunState | None:
        """Return the state the body declared, or `None` if it declared none.

        Returns:
            One of `TERMINAL_STATES`, or `None` for a body that has not declared
            an ending. The recorder reads `None` as `RunState.SUCCEEDED`.

        """
        return self._state

    @property
    def detail(self) -> str:
        """Return the explanation the body gave, if it gave one.

        Returns:
            The detail passed with the declaration, or the empty string. Empty is
            the ordinary case for a plain success, which needs no explanation.

        """
        return self._detail

    def succeeded(self, *, detail: str = "") -> None:
        """Declare that the run completed.

        Rarely needed -- a body that returns normally is already a success -- and
        offered anyway so that a branch which succeeds *with something to say*
        does not have to choose between saying it and staying on the ordinary
        path.

        Args:
            detail: Anything worth recording about the success.

        Raises:
            RunLedgerError: When an ending has already been declared.

        """
        self._declare(RunState.SUCCEEDED, detail)

    def partial(self, *, detail: str = "") -> None:
        """Declare that the run did some of its work.

        `CPM-AD-23`'s partial success: a sweep whose atomic unit is one package
        commits the packages that worked and records `partial` rather than
        rolling back the ones that did.

        Args:
            detail: What was and was not done. Worth supplying -- "partial" with
                no detail is a state nobody can act on.

        Raises:
            RunLedgerError: When an ending has already been declared.

        """
        self._declare(RunState.PARTIAL, detail)

    def skipped(self, *, detail: str = "") -> None:
        """Declare that the run declined to do its work.

        `CPM-AD-7`'s observation window: a second run inside the window writes a
        ledger row with status `skipped` and no evidence. It is not a failure,
        and a reader must not have to infer the difference from an empty result.

        Args:
            detail: Why the run declined.

        Raises:
            RunLedgerError: When an ending has already been declared.

        """
        self._declare(RunState.SKIPPED, detail)

    def failed(self, *, detail: str = "") -> None:
        """Declare that the run did not do its work.

        For a failure the body *handled* -- a source answering an error the
        collector understands. A failure that raises needs no call: the recorder
        finalizes it and re-raises.

        Args:
            detail: Why the run failed.

        Raises:
            RunLedgerError: When an ending has already been declared.

        """
        self._declare(RunState.FAILED, detail)

    def _raised(self, error: BaseException) -> None:
        """Record that an exception left the body, overriding any declared ending.

        The recorder's half of this object, not the body's -- private for the
        reason the class docstring gives. See it also for why this is the one
        path permitted to replace a declaration rather than refusing it.

        Args:
            error: The exception on its way out. Its type and message become the
                row's detail, because a `failed` row whose detail is empty sends
                a reader to the logs for the one fact the row could have carried.

        """
        self._state = RunState.FAILED
        self._detail = f"{type(error).__name__}: {error}"

    def _declare(self, state: RunState, detail: str) -> None:
        """Record an ending, refusing a second one.

        Args:
            state: The terminal state being declared.
            detail: The explanation to record with it.

        Raises:
            RunLedgerError: When an ending has already been declared. The message
                names both states, because "this run was already finalized" sends
                the reader looking for the first call and naming it does not.

        """
        if self._state is not None:
            message = (
                f"this run was already declared {self._state.value} and cannot also be declared {state.value}. "
                f"A run ends once; the row holds one state (CPM-AD-2)."
            )
            raise RunLedgerError(message)
        self._state = state
        self._detail = detail


@contextmanager
def _recorded(run: RunLedgerModel, clock: Clock) -> Iterator[RunHandle]:
    """Insert the row, yield a handle, and finalize on every exit path.

    The whole of the ordering guarantee, in one place so that the two public
    recorders below cannot come to differ about it.

    Args:
        run: The unsaved ledger row, already carrying its `started_at`, its
            `trace_id` and status `running`.
        clock: The clock the finalizing instant is read from (`CPM-AD-26`).

    Yields:
        The handle the body declares its ending on.

    Raises:
        BaseException: Whatever the body raised, re-raised unchanged after the
            row has been finalized to `failed`. Caught this widely on purpose:
            `SystemExit` and Celery's soft-time-limit signal are how a worker is
            asked to stop, and a run stopped that way is exactly the run this
            ledger exists to leave a record of.
        DatabaseError: When the finalizing write itself fails **and no body
            exception is in flight**. See below for why the two cases differ.

    """
    run.save()
    handle = RunHandle(run=run)
    body_error: BaseException | None = None
    try:
        yield handle
    # Caught this widely, and re-raised unchanged. See this function's docstring
    # for why `SystemExit` and a soft time limit are exactly the endings worth
    # recording; nothing is swallowed, because the `raise` below is
    # unconditional and re-raises the same object (inherited `CG-3`).
    except BaseException as error:
        body_error = error
        handle._raised(error)  # noqa: SLF001 - the recorder's own half of the handle; RunHandle says why it is private
        raise
    finally:
        try:
            _write_ending(
                run,
                state=RunState.SUCCEEDED if handle.state is None else handle.state,
                detail=handle.detail,
                clock=clock,
            )
        except DatabaseError as failure:
            # **The finalizing write is the one that most plausibly fails, and
            # exactly when it matters most.** A body raising `IntegrityError`
            # marks the connection for rollback, so this `save()` then raises
            # `TransactionManagementError` -- and an exception raised in a
            # `finally` *replaces* the one propagating, which would make this
            # module's "re-raised exactly as it was" promise false in precisely
            # the mid-call-death case the ledger exists for.
            #
            # So it is logged either way -- never swallowed, and the row's pk is
            # in the record so an operator can go and look at what is left of it
            # -- and re-raised only when it is not standing on somebody else's
            # exception. With a body error in flight the caller keeps its own
            # exception, which is the more useful of the two: it says why the run
            # died, where this one only says the epitaph could not be written.
            logger.exception(
                FINALIZATION_FAILED_EVENT,
                run_pk=run.pk,
                run_model=type(run).__name__,
                run_status=str(run.status),
                error=str(failure),
                body_error=None if body_error is None else type(body_error).__name__,
            )
            if body_error is None:
                raise


def _write_ending(run: RunLedgerModel, *, state: RunState, detail: str, clock: Clock) -> None:
    """Write a run's ending onto its row: the one place a ledger row's status is assigned.

    Shared by the recorder's `finally` and by `abandon`, so the audit in
    `tests/unit/django_apps/test_derived_status_writability_audit.py` records
    one `status =` assignment in this module rather than two that could drift.

    Args:
        run: The saved ledger row.
        state: The terminal state.
        detail: What to record with it.
        clock: The clock the finalizing instant is read from (`CPM-AD-26`).

    Raises:
        DatabaseError: From the write.

    """
    run.status = state
    run.detail = detail
    run.finished_at = clock.now()
    # `instance.save(update_fields=...)`, never `queryset.update()`. See
    # `FINALIZED_FIELDS` for both reasons the distinction is load-bearing.
    run.save(update_fields=FINALIZED_FIELDS)


def abandon(run: RunLedgerModel, *, clock: Clock, detail: str) -> None:
    """Finalize a run some other process left `running` as `failed`, with a reason.

    The recorder finalizes its own row on every exit path; the one row it cannot
    reach is the one a killed worker left behind, which stays `running` with no
    `finished_at` for ever and bounds `choose_evidence_cutoff` while it does. The
    nightly purge (`CPM-OPERATE-S07`) uses this on its own stale rows at start --
    a `prune_evidence` row still unfinished when the next purge begins was a purge
    that died -- and nothing else does: a row another collector's worker is still
    writing is that worker's to finalize.

    Args:
        run: The unfinished row. Refused when it already has an ending.
        clock: The clock the finalizing instant is read from (`CPM-AD-26`).
        detail: Why it is being closed, recorded as the row's detail.

    Raises:
        RunLedgerError: When the row already carries an ending.
        DatabaseError: From the write.

    """
    if run.finished_at is not None:
        message = (
            f"run {run.pk} already ended {run.status} at {run.finished_at.isoformat()} and cannot be abandoned; "
            f"only a row still running has no ending to write (CPM-AD-2)."
        )
        raise RunLedgerError(message)
    _write_ending(run, state=RunState.FAILED, detail=detail, clock=clock)


@contextmanager
def collection_run(
    *,
    collector: str,
    clock: Clock,
    package_id: int | None = None,
) -> Iterator[RunHandle]:
    """Record one collector's run, from before its first outbound call until after it.

    Args:
        collector: The collector's name, recorded on the row so a run is
            traceable to the code that performed it (`CPM-FR-39`).
        clock: The clock both instants are read from (`CPM-AD-26`).
        package_id: The package this run is scoped to, by the integer primary key
            `CPM-AD-3` fixes -- and, since `CPM-EVIDENCE-S09`, by a real relation
            that the package must already exist for. Omitted for a run that is
            not scoped to one package, which writes NULL rather than a
            placeholder and stays answerable by `unfinished()`.

    Yields:
        The handle the body declares `partial`, `skipped`, `failed` or
        `succeeded` on. A body that declares nothing and returns finalizes as
        `succeeded`.

    Raises:
        RunLedgerError: When `collector` names nothing, when `package_id` is not
            a possible primary key, or when it names no package. Refused before
            the row is written, so a misrecorded run leaves no row rather than an
            unusable one.
        DatabaseError: From either database operation -- the lookup that checks
            the package key, or the insert. The lookup is the new one: a
            package-scoped run cannot be opened without a database now, not even
            to be refused.

    """
    run = CollectionRun(
        collector=_require_named(collector, field="collector"),
        package_id=_require_package_key(package_id),
        started_at=clock.now(),
        status=RunState.RUNNING,
        trace_id=current_trace_id(),
    )
    with _recorded(run, clock) as handle:
        yield handle


@contextmanager
def policy_run(
    *,
    policy_version: str,
    evidence_cutoff: datetime,
    clock: Clock,
) -> Iterator[RunHandle]:
    """Record one policy run, from before it reads evidence until after it stops.

    Args:
        policy_version: The version of the rule data this run applied
            (`CPM-AD-8`), recorded so a derived row is traceable to it.
        evidence_cutoff: The instant this run reads evidence as of
            (`CPM-AD-21`). Required: a run with no stated cut-off cannot be
            replayed, which is what `CPM-FR-22` promises.
        clock: The clock both lifecycle instants are read from (`CPM-AD-26`).
            Separate from `evidence_cutoff`, which is a property of the evidence
            rather than of the moment this run happens to execute.

    Yields:
        The handle the body declares its ending on, exactly as for a collection
        run.

    Raises:
        RunLedgerError: When `policy_version` names nothing, or when
            `evidence_cutoff` is naive. Refused before the row is written: a run
            recorded against an uninterpretable cut-off is one whose replay reads
            a different evidence set every time. Both checks are pure -- a policy
            run carries no package reference, so nothing here looks one up.
        DatabaseError: From the insert. Not from validation, which is the one
            way this recorder differs from `collection_run`.

    """
    run = PolicyRun(
        policy_version=_require_named(policy_version, field="policy_version"),
        evidence_cutoff=_require_aware(evidence_cutoff, field="evidence_cutoff"),
        started_at=clock.now(),
        status=RunState.RUNNING,
        trace_id=current_trace_id(),
    )
    with _recorded(run, clock) as handle:
        yield handle
