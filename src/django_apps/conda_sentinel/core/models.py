"""The append-only base every evidence model inherits, and the refusals it carries.

`CPM-AD-2`: "evidence models inherit an abstract base in `core` whose `save()`
refuses when `pk` is set, and whose manager exposes no `update()` or `delete()`.
Re-observation always inserts." `CPM-FR-36` is the requirement and `R-06` is the
risk: once a collector updates an evidence row instead of inserting one, what the
system knew at a point in time is gone, and no later fix reconstructs it. There
is no migration back from an overwritten history, which is why the guard is a
refusal at the write rather than a review convention.

**What this base is not.** It declares no table. `Meta.abstract = True`, so
`makemigrations` produces nothing for it, and it is deliberately not accompanied
by a concrete evidence model: `CPM-AD-7` gives each collector its own evidence
table, in that collector's own application. The first of them is
`collectors.InventorySnapshot`, the `inventory_snapshots` table `CPM-AD-25`
names, and it landed with `CPM-IDENTITY-S06` rather than with `CPM-EP-CURRENCY`
-- the inventory is observed like any other source, so the first collector to
ship is the one that acquires the packages every later collector observes. The
run ledger --
`collection_runs` and `policy_runs` -- is explicitly *exempt* from this base by
`CPM-AD-2` and belongs to `CPM-EVIDENCE-S03`: a run row is created before the
first outbound call and finalized afterwards, so it is mutable by construction
and is not evidence.

**The run ledger is in this module, below, and the exemption is declared at its
definition.** `CPM-EVIDENCE-S03`'s first acceptance criterion asks for exactly
that, and `tests/model_registry.py` gives the declaration a machine-readable
form: `not_evidence = True` on the model. It is not a quiet door --
`tests/unit/django_apps/test_evidence_inheritance_audit.py` reconciles every
model taking it against a recorded table in both directions, so a third user of
the escape fails the gate until somebody records the decision. `RunLedgerModel`
inherits nothing from `AppendOnlyModel`, declares no `observed_at`, and takes no
`Meta.base_manager_name`: a run row is not an observation, and every refusal
above would be wrong on it.

**`observed_at` carries no default, and that is forced rather than chosen.** The
two idiomatic Django spellings -- `default=timezone.now` and `auto_now_add=True`
-- are both failures of `EVIDENCE.01-AUDIT-002`, because both read the process
wall clock where the row is written. `CPM-AD-26` wants the instant injected, so
that freshness targets and observation windows are testable without waiting or
freezing time process-wide. The writer therefore supplies the instant from its
`Clock`, and `save()` refuses a row that has none -- the omission is loud rather
than a silent epoch-zero row that a later staleness query reads as ancient.

**Why "`pk` is set" is the whole rule.** It is what `CPM-AD-2` says, and it is
also the widest form of the accident: an instance loaded from the database and
saved again is the path nobody writes on purpose, and it is indistinguishable
from a constructed one except by its primary key. The consequence, stated rather
than discovered: a model whose primary key the *writer* assigns before the first
save cannot use this base. Nothing in this product does that -- `CPM-AD-3` fixes
every table on a surrogate key, which Django assigns at insert -- and a model
that needed one would be asking for a natural key on evidence, which is a
separate decision.

**What the guard cannot see, and what closes it.** `save()` is one write path of
several, and the others do not construct an instance at all:

* `queryset.update()`, `bulk_update()`, `QuerySet.delete()` and `_raw_delete()`
  are refused by the queryset below, and reach the *manager* spellings through
  it. `Meta.base_manager_name` is what stops `_base_manager` being a plain,
  unguarded manager Django builds on the model's behalf.
* `bulk_create(update_conflicts=True)` is an `UPDATE` compiled into an insert;
  the queryset refuses it, and the plain insert stays untouched.
* **Cascade deletion goes past every refusal here.** Django's deletion collector
  issues its `DELETE` through `sql.DeleteQuery`, never through `QuerySet.delete()`
  or `Model.delete()`, so a `ForeignKey(..., on_delete=CASCADE)` from an evidence
  row destroys observations when the parent goes -- and `SET_NULL` and
  `SET_DEFAULT` rewrite them in place. Nothing in this class can see that; what
  closes it is `EVIDENCE.02-AUDIT-001`, which requires every relation on an
  evidence model to use `PROTECT`, `RESTRICT` or `DO_NOTHING`.
* Raw SQL is closed by `tests/unit/django_apps/test_mutation_path_audit.py`
  (`EVIDENCE.02-AUDIT-002`), which sweeps the product's own source for every
  bypass form plus cursor-level `UPDATE`, `DELETE` and `ON CONFLICT`.

**The one audited door (`CPM-OPERATE-S07`).** Retention is the one exception
`CPM-AD-2` admits: `AppendOnlyQuerySet.retire(*, door)` performs a delete only
when handed the token `core/retention.py` alone constructs, and refuses
anything else with `AppendOnlyError`. It is a method beside the refusals rather
than a softening of any of them -- `delete()`, `_raw_delete()`, `update()`,
`bulk_update()` and the instance `delete()` refuse exactly as before -- and the
mutation-path audit licenses its one raw delete by count, so a second door
fails the gate. What goes through it is decided in `core/retention.py`, by a
declared retention and a floor rule, and recorded per table in the run ledger.

Two audits hold the shape of the models themselves:
`test_evidence_inheritance_audit.py` (`EVIDENCE.02-AUDIT-001`), that every
evidence model inherits this base, keeps its managers and cannot be deleted
through a relation; and `test_evidence_constraint_audit.py`
(`EVIDENCE.02-AUDIT-003`), that none carries a unique constraint -- a constraint
spanning the observed fact would turn a re-observation into an `IntegrityError`,
which is the same history loss arriving as a crash instead of an overwrite.

**Inserts are left alone, deliberately.** `create()`, `acreate()` and plain
`bulk_create()` stay exactly as Django wrote them: an insert is not a mutation,
and a base that made bulk insertion awkward would push collectors toward raw SQL,
which is the one write path nothing here can guard.

**On the `AD-` prefix.** A bare `AD-n` in this repository is an *inherited*
platform decision; a decision from this product's own architecture spine always
carries the `CPM-` prefix.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from typing import Any
from typing import Final
from typing import TypeVar
from typing import override

from django.conf import settings
from django.db import models
from django.db.models.deletion import Collector
from django.utils.translation import gettext_lazy as _

from conda_sentinel.core.clock import is_aware
from conda_sentinel.core.jobs import IN_PROGRESS_STATES
from conda_sentinel.core.jobs import KIND_LENGTH
from conda_sentinel.core.jobs import STATE_LENGTH
from conda_sentinel.core.jobs import JobState
from conda_sentinel.core.runs import RunState
from conda_sentinel.identity.confidence import IdentityConfidence
from conda_sentinel.policies.outcomes import CURRENCY_STATE_LENGTH
from conda_sentinel.policies.outcomes import FEEDSTOCK_STATE_LENGTH
from conda_sentinel.policies.outcomes import FEEDSTOCK_UNKNOWN
from conda_sentinel.policies.outcomes import PRIORITY_BUCKET_LENGTH
from conda_sentinel.policies.outcomes import PRIORITY_STATUS_UNKNOWN
from conda_sentinel.policies.outcomes import UNKNOWN as CURRENCY_UNKNOWN
from conda_sentinel.policies.outcomes import WORK_TYPE_LENGTH
from conda_sentinel.policies.outcomes import WORK_TYPE_UNKNOWN
from conda_sentinel.policies.outcomes import CurrencyOutcome
from conda_sentinel.policies.outcomes import FeedstockOutcome
from conda_sentinel.policies.outcomes import PriorityBucket
from conda_sentinel.policies.outcomes import WorkType

if TYPE_CHECKING:
    from collections.abc import Collection
    from collections.abc import Iterable

    from django.db.models.base import ModelBase

__all__ = [
    "COLLECTION_RUN_FINISHED_INDEX",
    "COLLECTION_RUN_STARTED_INDEX",
    "FINISHED_AT_FIELD",
    "POLICY_RUN_CUTOFF_INDEX",
    "POLICY_RUN_FINISHED_INDEX",
    "AppendOnlyError",
    "AppendOnlyManager",
    "AppendOnlyModel",
    "AppendOnlyQuerySet",
    "BackgroundJob",
    "CollectionRun",
    "PackageHealth",
    "PolicyRun",
    "RunLedgerModel",
    "RunLedgerQuerySet",
]

#: The model an append-only queryset or manager is bound to. Bound to
#: `models.Model` rather than to `AppendOnlyModel` so that the queryset can be
#: constructed against a subclass without the manager's type collapsing to the
#: abstract base.
_EvidenceModel = TypeVar("_EvidenceModel", bound=models.Model)

#: The model a run-ledger queryset is bound to. Bound to `models.Model` for the
#: same reason `_EvidenceModel` is: the queryset is constructed against a
#: concrete subclass, and binding it to the abstract base would collapse the
#: manager's type back to that base.
_RunModel = TypeVar("_RunModel", bound=models.Model)

#: How wide the ledger's short string columns are. `status` holds one member of
#: `RunState`, whose longest value is nine characters; `trace_id` holds exactly
#: 32 hexadecimal digits, because `CPM-AD-15` takes it from the active span
#: formatted `032x` and the product adds no correlation scheme of its own. The
#: name and version columns are wider because they hold identifiers a person
#: chose.
_STATUS_LENGTH: Final[int] = 16
_TRACE_ID_LENGTH: Final[int] = 32
_NAME_LENGTH: Final[int] = 128

#: How wide the rollup's mirrored confidence column is.
#:
#: The same 32 `identity/models.py` argues for, because it holds the same
#: vocabulary: `IdentityConfidence`, whose longest value is `inventory-derived`.
#: It is a second declaration rather than an import because the identity module's
#: constant is private to it and a cross-module private read is exactly what
#: `SLF001` forbids -- and because a width is a property of a column, so the day
#: the two columns legitimately differ this is where that is said. What stops the
#: two drifting is not this comment: `tests/unit/django_apps/test_identity_models.py`
#: reconciles the two fields' `max_length` directly, so a widened
#: `Package.confidence` fails until this follows it.
_CONFIDENCE_LENGTH: Final[int] = 32

#: The column the recorder's `finally` writes, and therefore the one that exists
#: exactly when a run has an ending. Both queryset methods below read it --
#: `unfinished()` for its absence and `failed()` to order by it -- and a name
#: spelled at each of them is one typo away from ordering a failure page by the
#: primary key instead, which looks identical until the ids stop matching the
#: chronology.
FINISHED_AT_FIELD: Final[str] = "finished_at"

#: The four time-leading indexes on the two ledgers (`CPM-OPERATE-S07`). Each
#: names a column the nightly purge scans by cut-off -- `finished_at` and
#: `started_at` on `collection_runs`, `finished_at` on `policy_runs` -- or that
#: the floor rule joins on for every candidate evidence row (`evidence_cutoff`).
#: Named here, as `package_health_cutoff` is on the rollup, so the migration and
#: the model spell one name; `tests/unit/django_apps/test_retention.py` asserts
#: each purged table declares an index whose first field is its cut-off column.
COLLECTION_RUN_FINISHED_INDEX: Final[str] = "collection_runs_finished"
COLLECTION_RUN_STARTED_INDEX: Final[str] = "collection_runs_started"
POLICY_RUN_FINISHED_INDEX: Final[str] = "policy_runs_finished"
POLICY_RUN_CUTOFF_INDEX: Final[str] = "policy_runs_cutoff"


class AppendOnlyError(Exception):
    """An operation would have changed or removed evidence that is already written.

    One type rather than a hierarchy, on the same terms as
    `config/authorization/exceptions.py`'s `ClaimsRejected`: no caller branches on
    *which* mutating path was attempted -- every one of them is a defect to be
    fixed at the call site, not a condition to be handled -- so the detail lives
    in the message and in the attributes rather than in the class.

    It raises rather than warning or logging-and-continuing, which inherited
    `CG-3` requires: a warning on an overwritten observation is a warning nobody
    reads until the history is already gone.

    Attributes:
        model_label: The `app_label.ModelName` of the table the operation would
            have touched, so a failure names the model without the reader parsing
            the message.
        pk: The primary key of the row, where one operation was aimed at a
            single row. `None` for a queryset-wide operation, which has no one
            row to name.

    """

    def __init__(self, message: str, *, model_label: str, pk: object = None) -> None:
        """Record the message and the row it names.

        Args:
            message: What was attempted and why it was refused.
            model_label: The `app_label.ModelName` of the model.
            pk: The primary key of the row, or `None` for a set-wide operation.

        """
        super().__init__(message)
        self.model_label = model_label
        self.pk = pk


def _label(model: type[models.Model]) -> str:
    """Return a model's `app_label.ModelName`.

    Args:
        model: The model to name.

    Returns:
        The label Django itself uses in system checks and migrations, so a
        refusal message and a `makemigrations` message name the model the same
        way.

    """
    return str(model._meta.label)  # noqa: SLF001 - `_meta` is Django's own public-by-convention API


class AppendOnlyQuerySet(models.QuerySet[_EvidenceModel]):
    """A queryset that offers no way to change or remove a row.

    Five overrides, and each is a path that reaches the table without ever
    constructing an instance -- which is exactly why `AppendOnlyModel.save()`
    cannot see any of them. `update()` and `bulk_update()` issue `UPDATE`;
    `delete()` runs Django's collector rather than the instance's `delete()`;
    `_raw_delete()` is the collector's own statement, reachable directly;
    `bulk_create()` is refused *only* in its conflict-handling forms.

    **`bulk_create` is the subtle one.** The plain call is an insert and stays
    exactly as Django wrote it. `update_conflicts=True` compiles to
    `INSERT ... ON CONFLICT DO UPDATE`, which is an overwrite wearing an insert's
    name, and `ignore_conflicts=True` compiles to `DO NOTHING`, which drops the
    observation instead of recording it -- and `CPM-AD-7` says re-observation is
    never a no-op any more than it is an update. Both are refused; neither can be
    reached without the corresponding keyword, so the permitted path is
    unaffected.

    The async spellings are `sync_to_async` wrappers around these, so `aupdate`,
    `adelete` and `abulk_update` refuse through the same guard rather than
    through a second copy of it that could drift. That is a fact about Django's
    implementation rather than about this class, so
    `tests/unit/django_apps/test_append_only_model.py` calls all three and
    asserts the refusal -- a future native async implementation is then a failing
    test rather than a silently reopened path.

    Every other method is Django's own. Filtering, `values_list`, `create` and
    `acreate` are untouched, because reading evidence and inserting evidence are
    what evidence is for.

    **One method is added rather than overridden: `retire(*, door)`**, the
    audited door `CPM-OPERATE-S07` gives the retention purge. It opens only for
    the token `core/retention.py` constructs, consults Django's collector so a
    `PROTECT` relation still refuses, and then issues the parent's raw delete
    over exactly the selection. Its docstring says the rest; what matters here is
    that none of the five refusals above changed to make room for it.

    **`raw()` is left in place here and banned by the audit**, and the two are
    not in conflict: Django's `raw()` executes the SQL it is handed, and while it
    is documented for `SELECT` there is nothing in it that refuses an `UPDATE`.
    A runtime refusal would have to parse the SQL, which is the audit's job and
    is done better there;
    `tests/unit/django_apps/test_mutation_path_audit.py` says why it bans the
    form outright rather than trying to tell the two apart.
    """

    @override
    def update(self, **kwargs: Any) -> int:
        """Refuse an `UPDATE` against the whole set.

        Args:
            kwargs: The column assignments Django would have written. Named in
                the message so the refusal says what was being attempted.

        Raises:
            AppendOnlyError: Always. Re-observation inserts a new row carrying
                its own `observed_at`; there is no correct `update()` on
                evidence.

        """
        label = _label(self.model)
        message = (
            f"{label} is append-only, so update({', '.join(sorted(kwargs))}) is refused. "
            f"Observe the fact again and insert a new row with its own observed_at (CPM-AD-2)."
        )
        raise AppendOnlyError(message, model_label=label)

    @override
    def bulk_update(
        self,
        objs: Iterable[_EvidenceModel],
        fields: Iterable[str],
        batch_size: int | None = None,
    ) -> int:
        """Refuse a batched `UPDATE`.

        Args:
            objs: The instances Django would have written back.
            fields: The columns it would have written.
            batch_size: How many rows per statement. Unused; the refusal is
                unconditional.

        Raises:
            AppendOnlyError: Always. `bulk_update` is the mutation that most
                looks like a write path a collector may legitimately use, which
                is why it is refused here *and* swept for by
                `EVIDENCE.02-AUDIT-002`.

        """
        label = _label(self.model)
        message = (
            f"{label} is append-only, so bulk_update({', '.join(sorted(fields))}) is refused. "
            f"Insert the re-observed rows with bulk_create instead (CPM-AD-2)."
        )
        raise AppendOnlyError(message, model_label=label)

    @override
    def delete(self) -> tuple[int, dict[str, int]]:
        """Refuse a set-wide `DELETE`.

        Raises:
            AppendOnlyError: Always. Retention is a decision for an
                administrative process against a named window, not something a
                queryset in the product's own source may take.

        """
        label = _label(self.model)
        message = (
            f"{label} is append-only, so delete() on a queryset is refused. "
            f"An observation is removed by a declared retention process, never by product code (CPM-AD-2)."
        )
        raise AppendOnlyError(message, model_label=label)

    @override
    def _raw_delete(self, using: str | None) -> int:
        """Refuse the collector's own `DELETE`.

        `QuerySet.delete()` above is what product code calls; this is what
        Django's deletion collector calls underneath it, and it is reachable
        directly. Overriding only the public spelling would leave the private one
        as a one-underscore bypass -- which is the kind of hole the audit was
        written for, so it is closed here as well.

        Args:
            using: The database alias Django would have deleted from.

        Raises:
            AppendOnlyError: Always.

        """
        label = _label(self.model)
        message = (
            f"{label} is append-only, so _raw_delete() is refused. "
            f"An observation is removed by a declared retention process, never by product code (CPM-AD-2)."
        )
        raise AppendOnlyError(message, model_label=label)

    def retire(self, *, door: object) -> int:
        """Remove the selected observations through the one audited door (`CPM-OPERATE-S07`).

        Retention is the one exception `CPM-AD-2` admits, and this is its whole
        surface: a method that performs the delete only when handed the token
        `core/retention.py` alone constructs, so the purge -- a declared window,
        a floor rule, a run record per table -- is the only code that can reach
        it. It is not a softening of `delete()` or `_raw_delete()`, both of which
        refuse exactly as before; `tests/unit/django_apps/test_append_only_model.py`
        pins each refusal and pins that this method refuses anything but the token.

        **`PROTECT` is consulted first, then the parent's raw delete issues the
        statement.** Django's deletion collector is what knows the relations
        pointing at this table -- a derived row citing the observation it was
        computed from, a KEV row citing its finding -- and it raises
        `ProtectedError` before any SQL when one of them cites a selected row.
        The statement itself is the parent queryset's `_raw_delete`: one
        `DELETE ... WHERE` over exactly the selection, with no cascade, which
        `EVIDENCE.02-AUDIT-001` has already made impossible on an evidence model.
        The mutation-path audit licenses that one call by count.

        **The four shapes `QuerySet.delete()` refuses are refused here too, with
        its own `TypeError`s**, and one of them is why the check exists at all:
        the parent's `_raw_delete` compiles the query as it stands and a slice
        is silently dropped from a `DELETE`, so a `[:1000]` handed in would
        remove every selected row rather than a thousand of them. A
        `.values()`/`.values_list()` selection, a `.distinct(*fields)` and a
        combined query are refused on the same terms Django refuses them.

        Args:
            door: The token. Anything else is refused.

        Returns:
            How many rows the statement removed.

        Raises:
            AppendOnlyError: When `door` is not the retention module's token.
            TypeError: When the queryset is sliced, projected with `.values()` or
                `.values_list()`, distinct on fields, or combined with
                `.union()`, `.intersection()` or `.difference()` -- Django's own
                sentences, because they are Django's own refusals.
            ProtectedError: When a retained row cites a selected one. The purge
                catches it per batch, retries without the cited rows, and counts
                what stays.

        """
        # Imported here rather than at module scope: `core/retention.py` imports
        # this module for the base and the ledger, so a module-scope import
        # would be a cycle.
        from conda_sentinel.core.retention import is_retention_door  # noqa: PLC0415 - see above

        label = _label(self.model)
        if not is_retention_door(door):
            message = (
                f"{label} is append-only, so retire(door={door!r}) is refused. The one door out of an evidence "
                f"table opens for the retention token core/retention.py constructs and for nothing else "
                f"(CPM-AD-2, CPM-OPERATE-S07)."
            )
            raise AppendOnlyError(message, model_label=label)
        # The same four shapes `QuerySet.delete()` refuses, read off the same
        # two attributes it reads; django-stubs declares neither, so both are
        # reached as Django's own undeclared internals.
        if self.query.combinator:
            message = "Cannot call retire() after .union(), .intersection(), or .difference()."
            raise TypeError(message)
        if self.query.is_sliced:
            message = "Cannot use 'limit' or 'offset' with retire(): a raw DELETE drops the slice silently."
            raise TypeError(message)
        if self.query.distinct_fields:
            message = "Cannot call retire() after .distinct(*fields)."
            raise TypeError(message)
        if getattr(self, "_fields", None) is not None:
            message = "Cannot call retire() after .values() or .values_list()"
            raise TypeError(message)
        # The selection is handed over as instances rather than as the queryset:
        # the collector fetches them itself for any model something points at,
        # which is every evidence model a pass cites, and a fetched batch is
        # what lets it ask each `PROTECT` relation about exactly these rows.
        Collector(using=self.db).collect(list(self))
        return super()._raw_delete(self.db)

    @override
    def bulk_create(
        self,
        objs: Iterable[_EvidenceModel],
        batch_size: int | None = None,
        # Both keep Django's own name, position and boolean type: a signature
        # that renamed or reordered them would refuse the very calls it is
        # meant to intercept with a TypeError instead of this model's error.
        ignore_conflicts: bool = False,
        update_conflicts: bool = False,
        update_fields: Collection[str] | None = None,
        unique_fields: Collection[str] | None = None,
    ) -> list[_EvidenceModel]:
        """Insert rows, refusing the two forms that are not inserts.

        Args:
            objs: The rows to insert.
            batch_size: How many rows per statement, passed through.
            ignore_conflicts: Refused. `INSERT ... ON CONFLICT DO NOTHING`
                silently drops the observation.
            update_conflicts: Refused. `INSERT ... ON CONFLICT DO UPDATE`
                overwrites the earlier observation.
            update_fields: Passed through; meaningful only with
                `update_conflicts`, which is refused.
            unique_fields: Passed through; meaningful only with
                `update_conflicts`, which is refused.

        Returns:
            The inserted rows, from Django's own implementation.

        Raises:
            AppendOnlyError: When either conflict-handling flag is set. The
                message names the flag, because the caller asked for a specific
                behaviour and needs to be told which one is unavailable.

        """
        asked = [
            name
            for name, wanted in (("ignore_conflicts", ignore_conflicts), ("update_conflicts", update_conflicts))
            if wanted
        ]
        if asked:
            label = _label(self.model)
            message = (
                f"{label} is append-only, so bulk_create({'=True, '.join(asked)}=True) is refused. "
                f"Conflict handling turns an insert into an overwrite or into a dropped observation; "
                f"a re-observed fact is inserted as a new row (CPM-AD-2, CPM-AD-7)."
            )
            raise AppendOnlyError(message, model_label=label)
        return super().bulk_create(
            objs,
            batch_size=batch_size,
            update_fields=update_fields,
            unique_fields=unique_fields,
        )


class AppendOnlyManager(models.Manager[_EvidenceModel]):
    """The default manager for every evidence model.

    It overrides `get_queryset` and nothing else, which is the whole trick:
    Django builds `Manager` from `QuerySet`, so `Model.objects.update(...)` and
    `Model.objects.bulk_update(...)` are thin delegations to the queryset this
    returns and refuse through the overrides above rather than through a second
    set of guards here. `Model.objects.delete()` does not exist at all --
    `QuerySet.delete` is marked `queryset_only`, so it is never copied onto a
    manager -- and raises `AttributeError`.

    `tests/unit/django_apps/test_append_only_model.py` pins both spellings, so a
    future Django that started copying `delete` onto managers is a failing test
    rather than a silent hole.
    """

    #: Declared for the type checker only. Django's `BaseManager.__init__` sets
    #: it and `Manager.get_queryset` reads it; django-stubs does not carry it, and
    #: an annotation is the narrow way to say so without a blanket ignore.
    _hints: dict[str, models.Model]

    @override
    def get_queryset(self) -> AppendOnlyQuerySet[_EvidenceModel]:
        """Return the refusing queryset.

        Returns:
            An `AppendOnlyQuerySet` bound to this manager's model, database alias
            and routing hints. The hints are carried rather than dropped because
            `CPM-AD-16` adds a second database alias for analytics: a queryset
            built without them asks the router a question with less information
            than the manager was given, and the router can then answer with a
            different alias.

        """
        return AppendOnlyQuerySet(self.model, using=self._db, hints=self._hints)


class AppendOnlyModel(models.Model):
    """The abstract base every evidence model inherits.

    See the module docstring for why the guard is "`pk` is set", why
    `observed_at` has no default, and what the three audits close that this class
    cannot.
    """

    #: The instant *this* observation was made, supplied by the writer from an
    #: injected `Clock` (`CPM-AD-26`). No `default` and no `auto_now_add`: both
    #: read the wall clock where the row is written, which
    #: `EVIDENCE.01-AUDIT-002` fails. Non-null, so a row with no instant is a
    #: database error even if it somehow reached an insert path that skipped
    #: `save()`.
    observed_at = models.DateTimeField(_("observed at"))

    objects = AppendOnlyManager()

    class Meta:
        """Abstract, so this declaration creates no table and no migration."""

        abstract = True
        # `_base_manager` is the guard's largest hole if it is left unset. Django
        # builds one itself when no name is given -- a plain `Manager` returning
        # a plain `QuerySet` -- so `Evidence._base_manager.filter(...).update(...)`
        # would compile and run an UPDATE with every refusal above bypassed, and
        # it is exactly what somebody reaches for once `objects.update()` has
        # refused. Naming `objects` here makes the base manager the append-only
        # one. It reaches concrete subclasses that declare a `Meta` of their own
        # -- which is every one of them, because they must declare `app_label`
        # or an application -- through `Options.base_manager`, which walks the
        # MRO for a parent whose base manager is named. Django uses
        # `_base_manager` internally for related-object lookups, and it stays a
        # working queryset for those: only the mutating methods refuse.
        base_manager_name = "objects"

    @override
    def save(
        self,
        *args: Any,
        force_insert: bool | tuple[ModelBase, ...] = False,
        force_update: bool = False,
        using: str | None = None,
        update_fields: Iterable[str] | None = None,
    ) -> None:
        """Insert the observation, or refuse if it would change one already written.

        **The rule for both write methods is the same: accept exactly what Django
        accepts, and refuse with this model's own error.** Django 5.2 still takes
        `save()`'s four arguments positionally, under a deprecation, and
        `delete()` takes both of its own positionally; a signature here that
        narrowed either would answer a positional caller with a bare `TypeError`
        naming an argument, which is precisely the unhelpful message the
        `force_update` refusal exists to replace. So `*args` is accepted and
        refused by name.

        Args:
            args: The deprecated positional spelling of the four arguments below.
                Refused rather than mapped: an append-only model has one
                permitted call, `save()`, and a positional caller is asking for
                one of the three that are not.
            force_insert: Passed through to Django unchanged. An insert is
                always permitted.
            force_update: Always refused. It is the one argument whose entire
                purpose is the operation this model forbids.
            using: The database alias, passed through.
            update_fields: Passed through. With no primary key set there is
                nothing to update, so Django's own error covers the misuse.

        Raises:
            AppendOnlyError: When the call is positional, when `force_update` is
                asked for, when the row already exists (`pk` is set), or when
                `observed_at` is absent or naive. Each names the model, and the
                row wherever there is one.

        """
        label = _label(type(self))
        if args:
            message = (
                f"{label} is append-only and takes save()'s arguments by keyword only; "
                f"{len(args)} positional argument(s) were given. The deprecated positional spelling can only "
                f"ask for force_insert, force_update, using or update_fields, and three of those are refused."
            )
            raise AppendOnlyError(message, model_label=label, pk=self.pk)
        if force_update:
            message = (
                f"{label} is append-only, so save(force_update=True) is refused for pk={self.pk!r}. "
                f"Observe the fact again and insert a new row (CPM-AD-2)."
            )
            raise AppendOnlyError(message, model_label=label, pk=self.pk)
        if self.pk is not None:
            message = (
                f"{label} row pk={self.pk!r} is already written and this model is append-only, so save() is "
                f"refused. Re-observation inserts a new row with its own observed_at (CPM-AD-2)."
            )
            raise AppendOnlyError(message, model_label=label, pk=self.pk)
        if self.observed_at is None:
            message = (
                f"{label} was saved with no observed_at. The instant comes from the writer's injected "
                f"Clock (CPM-AD-26); this field has no default, deliberately."
            )
            raise AppendOnlyError(message, model_label=label)
        if not is_aware(self.observed_at):
            # `FixedClock` refuses a naive instant and `SystemClock` cannot
            # produce one, so a naive `observed_at` means the writer went around
            # the clock -- and the consequence lands far from here: `USE_TZ` is
            # on, so Django warns and stores the value as if it were UTC, and
            # every freshness comparison, observation window and "what did we
            # know at" query is then silently wrong by the writer's offset. This
            # is the one place every evidence write passes through, so it is the
            # one place the check is worth making.
            message = (
                f"{label} was saved with a naive observed_at ({self.observed_at!r}). "
                f"The instant comes from a Clock, which always answers in UTC (CPM-AD-26); a naive value has "
                f"no offset to interpret and would make every freshness comparison wrong rather than failing."
            )
            raise AppendOnlyError(message, model_label=label)
        super().save(
            force_insert=force_insert,
            force_update=force_update,
            using=using,
            update_fields=update_fields,
        )

    @override
    def delete(self, using: Any | None = None, keep_parents: bool = False) -> tuple[int, dict[str, int]]:
        """Refuse to remove the observation.

        Args:
            using: The database alias Django would have deleted from.
            keep_parents: Whether Django would have kept parent rows.

        Raises:
            AppendOnlyError: Always. An observation that was made cannot be
                un-made; a retention process removes rows by a declared window,
                and it is not this model's to offer.

        """
        label = _label(type(self))
        message = (
            f"{label} is append-only, so delete() is refused for pk={self.pk!r}. "
            f"An observation is removed by a declared retention process, never by product code (CPM-AD-2)."
        )
        raise AppendOnlyError(message, model_label=label, pk=self.pk)


class RunLedgerQuerySet(models.QuerySet[_RunModel]):
    """The run ledger's queryset, and the one place two operational questions are spelled.

    Two methods. `unfinished()` is the whole reason the ledger exists in a
    database rather than in a log line: a worker killed between the outbound call
    and the insert leaves a row that is `running` with no `finished_at`, and the
    question "which runs started and never finished" has to be *askable* rather
    than reconstructable by a person reading two log streams side by side.

    Declared as a queryset method and installed as the default manager, so the
    filter is written once and every caller -- a coverage view, an operational
    report, a test -- asks the same question. A caller writing
    `CollectionRun.objects.filter(finished_at__isnull=True)` by hand is one
    keystroke from `status=RunState.RUNNING`, which is a *different* set: a row
    finalized to `failed` never leaves `finished_at` null, but a row whose status
    was never advanced and whose `finished_at` was somehow written would be
    counted by one query and not the other. `finished_at` is the authority
    because it is what the recorder's `finally` writes.

    `failed()` is the other half, and it is `CPM-FR-38`'s: a collection failure
    has to be *answerable in the application layer* rather than only by reading
    two log streams side by side. `CPM-NFR-3` says the system "degrades to stale
    evidence, never to a clean result", and a coverage view can only say what the
    monitor cannot see if the failures are queryable -- with the `detail` that
    says what went wrong and the `trace_id` that leads to the span it went wrong
    in (`CPM-AD-15`). Declared here for the reason `unfinished()` is: written at
    each call site, "which runs failed" is one keystroke from
    `status=RunState.ERROR`, which is not a value this vocabulary has and which
    would silently return nothing at all.

    Nothing is refused here. The ledger is mutable by construction (`CPM-AD-2`),
    so `update()` and `delete()` stay exactly as Django wrote them -- the
    finalization path deliberately does not use them, and
    `EVIDENCE.02-AUDIT-002` is what keeps it that way, but that is a rule about
    the product's own source rather than a refusal this class makes.
    """

    def unfinished(self) -> RunLedgerQuerySet[_RunModel]:
        """Return the runs that started and have not been finalized.

        Returns:
            Every row whose `finished_at` is NULL, package-scoped or not. A run
            with no package reference is returned alongside one that has it: the
            column being NULL says the run was not scoped to a single package,
            and says nothing at all about whether it finished.

        """
        return self.filter(**{f"{FINISHED_AT_FIELD}__isnull": True})

    def finished(self) -> RunLedgerQuerySet[_RunModel]:
        """Return the runs that have an ending, newest ending first.

        The exact mirror of `unfinished()`, and it is here rather than spelled at
        its one caller for the reason that method is: `CPM-AD-21` makes a policy
        run's cut-off the `finished_at` of a *completed* collection run, and a
        caller writing `filter(status=RunState.SUCCEEDED)` by hand would ask a
        different question -- one that excludes a run which failed after writing
        some evidence, and therefore chooses a cut-off *earlier* than evidence
        the ledger actually holds. `finished_at` is the authority here for the
        same reason it is there: it is what the recorder's `finally` writes, on
        every exit path.

        Returns:
            Every row whose `finished_at` is not NULL, ordered by it descending,
            whatever ending the row records. Ordered here rather than at the call
            site because "the newest ending" is the only question this set is
            asked, and an unordered read answers it differently on every call.

        """
        return self.filter(**{f"{FINISHED_AT_FIELD}__isnull": False}).order_by(f"-{FINISHED_AT_FIELD}")

    def failed(self) -> RunLedgerQuerySet[_RunModel]:
        """Return the runs that ended in failure, newest ending first.

        `CPM-FR-38`: a collection failure is retrievable from the application
        layer, not only from the logs, and each one exposes its error detail and
        its `trace_id` -- both of which are columns on the row, so this is a
        query rather than a projection.

        Returns:
            Every row whose `status` is `failed`, ordered by `finished_at`
            descending. Ordered here rather than left to the database's own
            arbitrary order, because the question a collector-health surface asks
            is "what has broken *lately*", and an unordered page of failures is a
            different answer on every read.

            A row whose `trace_id` is blank is returned like any other: the
            column is empty when no span was active, which `RunLedgerModel` says
            "never blocks the run", and omitting the row would hide exactly the
            failures that happened outside a traced path.

            `partial` is deliberately not included. A run that did some of its
            work is a different operational fact from one that did none, and
            `core/runs.py` keeps the four endings distinct precisely so a reader
            is never asked to infer which happened. A surface that wants both
            asks for both.

        """
        return self.filter(status=RunState.FAILED).order_by(f"-{FINISHED_AT_FIELD}")


class RunLedgerModel(models.Model):
    """The abstract base both run-ledger tables inherit. Mutable, and not evidence.

    **The exemption, declared here at the definition.** `CPM-AD-2` says
    `collection_runs` and `policy_runs` are run-ledger models owned by `core`,
    are not evidence, and are mutable -- a row is created *before* the first
    outbound call with status `running` and finalized in a `finally`. That is the
    only way a process killed mid-run stays visible, which `CPM-FR-38` and
    `CPM-UJ-3` both require. `not_evidence = True` below is that exemption in the
    form the audits read; `tests/model_registry.py` explains why the escape
    exists and `tests/unit/django_apps/test_evidence_inheritance_audit.py`
    records every model that takes it.

    **What it deliberately does not inherit.** Not `AppendOnlyModel`: every
    refusal there is wrong on a row whose whole purpose is to be written twice.
    Not `observed_at`: a run row is not an observation, and `CPM-AD-7` fixes that
    column's meaning as "the moment of *this* observation", which a run's
    lifecycle does not have. Not `Meta.base_manager_name`: the ledger's managers
    guard nothing, so there is nothing for a base manager to bypass.

    **`started_at` carries no default, exactly as `observed_at` does not.** Both
    idiomatic spellings read the process wall clock where the row is written,
    which `EVIDENCE.01-AUDIT-002` fails; the recorder in `core/ledger.py` takes
    the instant from an injected `Clock` (`CPM-AD-26`) and passes it in.

    Attributes:
        not_evidence: The declared exemption, read by `tests/model_registry.py`.
            Exactly `True`; any other value is not a declaration.

    """

    #: `CPM-AD-2`'s run-ledger exemption, machine-readable. Read by
    #: `tests/model_registry.py`'s `declares_not_evidence`, which accepts only
    #: `True` -- an exemption this consequential should not be reachable by a
    #: truthy accident.
    not_evidence = True

    #: When the run began, supplied by the recorder from an injected `Clock`.
    #: Written before the first outbound call, which is what makes a killed
    #: worker's row exist at all.
    started_at = models.DateTimeField(_("started at"))

    #: When the run was finalized, or NULL while it is still running. This is the
    #: column `unfinished()` reads, and the one a killed worker leaves behind.
    finished_at = models.DateTimeField(_("finished at"), null=True, blank=True, default=None)

    #: What happened to the run, over `RunState` -- *not* `OutcomeState`.
    #: `core/runs.py` says at length why the two vocabularies are separate and
    #: why this column keeps the name `status` rather than dodging the
    #: derived-status audit by being called something else.
    status = models.CharField(
        _("status"),
        max_length=_STATUS_LENGTH,
        choices=RunState.choices,
        default=RunState.RUNNING,
    )

    #: The `trace_id` of the request or task that performed the run, formatted
    #: `032x` exactly as `config/observability/logging.py` formats it for every
    #: log line (`CPM-AD-15`). Empty when no span was active, which never blocks
    #: the run: a run recorded outside a span is still a run that happened.
    trace_id = models.CharField(_("trace id"), max_length=_TRACE_ID_LENGTH, blank=True, default="")

    #: Why the run ended the way it did -- the exception's type and message for a
    #: failure, the caller's own words for a partial or a skip. Empty for a plain
    #: success, which needs no explanation.
    detail = models.TextField(_("detail"), blank=True, default="")

    objects = RunLedgerQuerySet.as_manager()

    class Meta:
        """Abstract, so this declaration creates no table and no migration."""

        abstract = True


class CollectionRun(RunLedgerModel):
    """One collector's run against zero or one package. Table `collection_runs`.

    Named by `CPM-AD-2` in so many words, and written by
    `core/ledger.py`'s `collection_run` recorder.

    **The package reference is a real relation, and `CPM-AD-3` is now met by
    every table rather than by all but one.** `CPM-EVIDENCE-S03` declared this
    column an integer and said `CPM-EP-IDENTITY` would convert it "when the model
    lands"; the model landed with `CPM-IDENTITY-S01` and packages first existed
    with `CPM-IDENTITY-S06`, and both stories declined the conversion for the
    same sound reason -- that it is not a field swap but a change to
    `core/ledger.py`'s recorder contract. Two hand-offs is where a deferral stops
    being one, so `CPM-EVIDENCE-S09` made the change and paid that cost: the
    recorder now refuses a key that names no package, and every case that used to
    pass a literal key creates the package first.

    **The attribute is `package` and the column is still `package_id`.** Django
    names a `ForeignKey`'s column by its `attname`, so every reader and writer
    spelling `row.package_id`, `filter(package_id=...)` or
    `CollectionRun(package_id=...)` goes on working unchanged -- which is what
    let `core/collection.py`'s window query and `core/freshness.py`'s
    `PACKAGE_FIELD` stay exactly as they were.

    **`PROTECT` is `CPM-AD-25`'s, not `EVIDENCE.02-AUDIT-001`'s.** This is a
    run-ledger model rather than evidence (`not_evidence = True`), so that
    audit's cascade rule does not bind it. The behaviour is still right, for a
    different reason: `CPM-AD-25` says no package row is ever deleted -- absence
    is recorded as an observation -- and `PROTECT` is what makes that true rather
    than merely intended.

    NULL means "this run was not scoped to one package" and nothing else. A
    sweep across the whole inventory writes no package reference rather than a
    placeholder, and it stays answerable by `unfinished()` exactly as a
    package-scoped run does -- and it is an ordinary state rather than a refused
    one, which is why the relation stays nullable.
    """

    #: Which collector ran. A name rather than a relation: collectors are code,
    #: declared and never discovered (inherited `AD-8`), so there is no table for
    #: this to point at.
    collector = models.CharField(_("collector"), max_length=_NAME_LENGTH)

    #: The package this run was scoped to, by the integer primary key `CPM-AD-3`
    #: fixes, or NULL for a run that was not scoped to one. Indexed because "what
    #: has been collected for this package" is the question the coverage view
    #: asks -- and a `ForeignKey` carries `db_index=True` by itself, so the index
    #: the integer column declared explicitly is still there and is not declared
    #: twice.
    #:
    #: `PROTECT` for `CPM-AD-25`'s reason rather than
    #: `EVIDENCE.02-AUDIT-001`'s, and nullable because an inventory-wide sweep
    #: genuinely has no package -- see the class docstring for both.
    #: Named lazily rather than imported, for the reason `PackageHealth.package`
    #: is: `CPM-IDENTITY-S05` gave `identity/models.py` an `AppendOnlyModel`
    #: subclass, so that module now imports this one, and an import of `Package`
    #: here would close the cycle. The string resolves through Django's app
    #: registry and deconstructs identically, so no migration follows from it.
    package = models.ForeignKey(
        "identity.Package",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        default=None,
        related_name="collection_runs",
        verbose_name=_("package"),
    )

    class Meta:
        """The table `CPM-AD-2` names, rather than the `core_collectionrun` Django would derive."""

        db_table = "collection_runs"
        verbose_name = _("collection run")
        verbose_name_plural = _("collection runs")
        indexes = [
            # The two cut-off scans the nightly purge makes of this table
            # (`CPM-OPERATE-S07`): rows that ended before the retention, and
            # rows that never ended and started before it. Neither column is
            # indexed by anything else, and at ten thousand packages this table
            # grows by the inventory every day.
            models.Index(fields=[FINISHED_AT_FIELD], name=COLLECTION_RUN_FINISHED_INDEX),
            models.Index(fields=["started_at"], name=COLLECTION_RUN_STARTED_INDEX),
        ]

    def __str__(self) -> str:
        """Return the collector, its scope and its state.

        Returns:
            A one-line summary naming the collector, the package the run was
            scoped to (or that it was not), and the state the row currently
            holds.

        """
        scope = "all packages" if self.package_id is None else f"package {self.package_id}"
        return f"{self.collector} over {scope}: {self.status}"


class PolicyRun(RunLedgerModel):
    """One policy run, at one stated evidence cut-off. Table `policy_runs`.

    Named by `CPM-AD-2`, and written by `core/ledger.py`'s `policy_run`
    recorder. The *orchestration* -- the ordered list of passes, the registry
    that makes the single-writer rule auditable -- is `CPM-EVIDENCE-S07`'s and is
    deliberately not here; this is the ledger row that records that a run
    happened and how it ended.

    **The cut-off is required, not nullable.** `CPM-AD-8` says a pass reads
    evidence "at a stated cut-off", and `CPM-FR-22`'s replay guarantee is that
    re-running a version against a cut-off reproduces identical output. A run
    with no cut-off is a run whose output cannot be reproduced, so the column
    refuses one rather than recording a run that is unreplayable by construction.
    """

    #: The policy version this run applied. `CPM-AD-8` makes rule sets and
    #: scoring functions versioned *data*, so this is the identifier of that
    #: data, recorded on the run that used it.
    policy_version = models.CharField(_("policy version"), max_length=_NAME_LENGTH)

    #: The instant this run read evidence as of. `CPM-AD-21` makes it the
    #: `finished_at` of a completed collection run, so a pass never reads
    #: evidence written by a run that is still `running`.
    evidence_cutoff = models.DateTimeField(_("evidence cutoff"))

    class Meta:
        """The table `CPM-AD-2` names, rather than the `core_policyrun` Django would derive."""

        db_table = "policy_runs"
        verbose_name = _("policy run")
        verbose_name_plural = _("policy runs")
        indexes = [
            # `finished_at` is the purge's cut-off scan of this table
            # (`CPM-OPERATE-S07`); `evidence_cutoff` is what its floor rule asks
            # of every surviving run for every candidate evidence row -- "does a
            # cut-off fall between this row and the next" -- which is a range
            # probe of this column, once per row considered.
            models.Index(fields=[FINISHED_AT_FIELD], name=POLICY_RUN_FINISHED_INDEX),
            models.Index(fields=["evidence_cutoff"], name=POLICY_RUN_CUTOFF_INDEX),
        ]

    def __str__(self) -> str:
        """Return the policy version, its cut-off and its state.

        The cut-off is guarded the same way `CollectionRun` guards its package
        reference, and for a plainer reason: the column is non-null, but an
        *unsaved* instance holds `None`, and a `__str__` that raised
        `AttributeError` there would break the two places a half-built object is
        most likely to be rendered -- a debugger and a traceback.

        Returns:
            A one-line summary naming the version that ran, the cut-off it read
            evidence at, and the state the row currently holds.

        """
        cutoff = "no cut-off" if self.evidence_cutoff is None else self.evidence_cutoff.isoformat()
        return f"{self.policy_version} at {cutoff}: {self.status}"


class PackageHealth(models.Model):
    """The current derived health of one package. Table `package_health`.

    `CPM-AD-11`: current package health is a **Django-managed rollup table in the
    migration graph**, written only by the orchestrating policy run, carrying
    `computed_at` and a per-domain policy version map. Every clause of that
    sentence is load-bearing and each one rules something out.

    **Django-managed, never a materialized view.** A view is refreshed by a
    schedule nothing in the migration graph describes, so "when was this
    computed" becomes a property of a cron entry rather than of the row, and a
    replay (`CPM-FR-22`) has nothing to compare against. A table with
    `computed_at` and a `policy_run` reference answers both questions from the
    row itself.

    **Exactly one row per `identity.Package`, unmapped ones included.** That is
    what `unique=True` on the package reference makes a database rule rather than
    a convention the writer is trusted to keep. `CPM-AD-4`'s gate is expressed as
    *writing* `unknown` rather than as suppressing a row precisely so this stays
    true: a missing row would be ambiguous between "not computed yet" and "not
    confident enough to compute", and no read surface can tell those apart.

    **A real relation, as every table in this product now has.** This one was
    declared as a relation from the start rather than converted into one: it was
    a table being created with a writer built in the same story to satisfy the
    constraint, so neither reason `CollectionRun.package` was deferred over --
    a shipping recorder that did not require the key, and a column whose rows a
    remove-and-add would have lost -- ever reached it. Taking an integer "for
    consistency" would have left the one table whose whole purpose is *one row
    per inventory package* unable to say which packages those were.
    `CPM-EVIDENCE-S09` has since closed the gap from the other end, so `CPM-AD-3`
    is met by every table.

    **`PROTECT` on both relations.** `EVIDENCE.02-AUDIT-001`'s cascade rule binds
    evidence models and this is not one, so the choice is argued rather than
    inherited: a rollup row is the only statement this product makes about a
    package's current health, and `CASCADE` would make deleting a policy run --
    an operational tidy-up somebody will one day write -- silently empty the
    table every read surface reads. `SET_NULL` is worse: the row would survive
    claiming a health nothing can say where it came from.

    **Two domain status columns, and the table grows a column per pass.**
    `epics.md` says the rollup "composes whatever derived tables exist, so it
    grows as passes are added". `CPM-CURRENCY-S06` added the first,
    `currency_status`, and `CPM-CURRENCY-S07` the second,
    `feedstock_presence_status` -- each in the same story as the pass that
    produces it, which is the rule this table follows and not a coincidence: a
    column added ahead of its pass is one nothing writes and one every read
    surface reports `unknown` for forever. `CPM-AD-5` forbids the alternative of a
    JSON map keyed by domain, because a map would evade every audit that reads
    column names.

    The *mechanism* is unchanged and is what the remaining passes use:
    `core/policy.py` validates a declared contribution against this model's real
    fields, and `core/rollup.py` is the one writer that applies it after
    `CPM-AD-4`'s gate.

    **Every column a pass contributes is `editable=False`.**
    `tests/unit/django_apps/test_derived_status_writability_audit.py` recognises
    this model as derived state by `computed_at` and fails any field named
    `status`/`outcome`, or ending `_status`/`_outcome`, that is still editable.
    That audit was written before this table existed, deliberately, so the rule
    would be shaped by `CPM-AD-11` rather than by whatever this writer happened
    to do -- and `currency_status` is the first column it actually inspects.

    **It declares no `not_evidence`, and must not.** It carries none of the three
    marks `tests/model_registry.py` reads -- it does not inherit
    `AppendOnlyModel`, its app label is `core`, and it declares `computed_at`
    rather than `observed_at`. The escape hatch is `CPM-AD-2`'s exemption *for a
    model that carries a mark*, and
    `tests/unit/django_apps/test_evidence_inheritance_audit.py` fails an unused
    declaration. Derived state is not evidence and is not exempt from being
    evidence; it is a third thing.
    """

    #: The package this row is the current health of. One row per package, which
    #: the relation itself makes the database's rule rather than the writer's
    #: promise.
    #:
    #: A `OneToOneField` rather than `ForeignKey(unique=True)`. The two build the
    #: same column and the same unique index, and Django says so in
    #: `fields.W342`; what differs is what they *say*. A `ForeignKey` gives
    #: `package.health` a related manager, so every read surface projecting the
    #: rollup would write `package.health.first()` and would have to decide what a
    #: second row means -- a question `CPM-AD-11` has already answered. The
    #: one-to-one gives them the row.
    #:
    #: `PROTECT` because a package is never deleted in this product (`CPM-AD-25`
    #: records absence as an observation), so a cascade here would only ever fire
    #: on an accident.
    #: `"identity.Package"` as a string rather than the imported class, which is
    #: Django's own lazy reference and here is load-bearing rather than stylistic:
    #: `identity/models.py` now reads `AppendOnlyModel` from this module for
    #: `CPM-IDENTITY-S05`'s audit row, so an import of `identity.models` here
    #: would close a cycle and fail at start-up. The lazy form asks the app
    #: registry for the model when the relation is first resolved, long after both
    #: modules are loaded, and it deconstructs to exactly the `identity.package`
    #: the existing migration already records -- so nothing about the schema
    #: changes. `identity/confidence.py` exists for the other half of the same
    #: problem, and says so.
    package = models.OneToOneField(
        "identity.Package",
        on_delete=models.PROTECT,
        related_name="health",
        verbose_name=_("package"),
    )

    #: The policy run that computed this row. `CPM-FR-22`'s replay guarantee is
    #: "re-run this version against this cut-off and get identical output", and a
    #: row that cannot name the run it came from is a row no replay can be
    #: compared against.
    policy_run = models.ForeignKey(
        PolicyRun,
        on_delete=models.PROTECT,
        related_name="rollup_rows",
        verbose_name=_("policy run"),
    )

    #: When this row was computed, from the run's injected `Clock` (`CPM-AD-26`).
    #: No `default` and no `auto_now_add`, for the reason `observed_at` and
    #: `started_at` carry neither: both read the process wall clock where the row
    #: is written, which `EVIDENCE.01-AUDIT-002` fails.
    #:
    #: It is also the mark by which `test_derived_status_writability_audit.py`
    #: recognises a model as holding derived state, which `CPM-AD-11` asks for by
    #: name -- so the column is not merely a timestamp, it is the declaration.
    computed_at = models.DateTimeField(_("computed at"))

    #: The instant the run read evidence as of, copied from the run rather than
    #: joined to it. Copied because this is the column a read surface filters and
    #: sorts on -- "what does the monitor currently believe, and how old is the
    #: evidence behind it" is one question -- and a join to `policy_runs` for a
    #: value that can never change once written buys nothing.
    evidence_cutoff = models.DateTimeField(_("evidence cutoff"))

    #: How certain the package's identity was when this row was computed
    #: (`CPM-AD-4`). Recorded here rather than read through the relation because
    #: it is the *provenance of this computation*: `identity.Package.confidence`
    #: is mutable and a later resolution changes it, at which point the row would
    #: claim to have been gated at a confidence it was not.
    confidence = models.CharField(
        _("confidence"),
        max_length=_CONFIDENCE_LENGTH,
        choices=IdentityConfidence.choices,
    )

    #: The policy version each domain's verdict was computed under
    #: (`CPM-AD-11`, "a per-domain version map, not a scalar"). A scalar would
    #: force every domain to be re-run whenever any one of them changed version,
    #: or would lie about the ones that were not.
    #:
    #: Empty for a run in which no pass was registered.
    policy_versions = models.JSONField(_("policy versions"), default=dict, blank=True)

    #: What `CPM-FR-16`'s currency pass concluded about this package, gated by
    #: `CPM-AD-4` on the way in. The first domain status column on this table,
    #: added by `CPM-CURRENCY-S06` with the pass that produces it, and the one
    #: `contributable_columns()` now offers.
    #:
    #: **`editable=False`, which is `CPM-FR-37` as a declaration rather than a
    #: convention.** `tests/unit/django_apps/test_derived_status_writability_audit.py`
    #: recognises this model as derived state by `computed_at` and fails any
    #: field named `status`/`outcome`, or ending `_status`/`_outcome`, that is
    #: still editable. The declaration takes the field out of every `ModelForm`,
    #: out of the admin and out of `full_clean()`'s validation of user-supplied
    #: data -- the half of "only the rollup writer writes it" that no source scan
    #: can reach.
    #:
    #: **The vocabulary is `policies`', not `core`'s, and it is read from there
    #: rather than restated.** `CPM-AD-5` requires `choices` on every derived
    #: status and `core/rollup.py`'s `permitted_values` checks a contribution
    #: against exactly this declaration, so a copy of the six values here could
    #: drift from what the pass returns and would refuse a verdict the pass is
    #: entitled to produce. `policies/outcomes.py` is a leaf module holding only
    #: the composed type, which is what keeps this import from being a cycle --
    #: `identity/confidence.py` exists for the same reason and says so.
    #:
    #: **The default is `unknown`, and it goes through the gate like any
    #: contribution.** A rollup row is written for every package on every run,
    #: including one no pass evaluated, and `core/rollup.py` writes the field's
    #: own default there. `ok` would make every un-evaluated package read clean,
    #: which is the claim `CPM-FR-5` forbids; `unknown` says what is true.
    currency_status = models.CharField(
        _("currency"),
        max_length=CURRENCY_STATE_LENGTH,
        choices=CurrencyOutcome.choices,
        default=CURRENCY_UNKNOWN,
        editable=False,
    )

    #: What `CPM-FR-40`'s feedstock presence pass concluded about this package,
    #: gated by `CPM-AD-4` on the way in. The second domain status column, added
    #: by `CPM-CURRENCY-S07` with the pass that produces it, on exactly the terms
    #: `currency_status` above states -- `editable=False`, the vocabulary read
    #: from `policies/outcomes.py` rather than restated, and a default of
    #: `unknown` that goes through the gate like any contribution.
    #:
    #: **Named `feedstock_presence_status` and not `feedstock_status`, which is
    #: not fussiness.** `package_currency.feedstock_status` already exists and
    #: means something else entirely: whether the conda-forge *recipe* pins the
    #: authoritative version. This column is whether a feedstock exists at all and
    #: whether anybody is pushing to it. Two columns in one schema spelled the
    #: same and meaning different things is a query somebody writes correctly and
    #: reads wrongly.
    feedstock_presence_status = models.CharField(
        _("feedstock presence"),
        max_length=FEEDSTOCK_STATE_LENGTH,
        choices=FeedstockOutcome.choices,
        default=FEEDSTOCK_UNKNOWN,
        editable=False,
    )

    #: Which priority bucket `CPM-FR-20`'s pass put this package in, gated by
    #: `CPM-AD-4` on the way in. The third domain status column, added by
    #: `CPM-PRIORITY-S01` with the pass that produces it, on exactly the terms the
    #: two above state.
    #:
    #: **The bucket is here and the score is not.** A contribution is a
    #: `Mapping[str, str]` of *status* values -- `core/policy_run.py` checks each
    #: against the column's own `choices` -- so a numeric score has no way through
    #: that seam and no business in a column of statuses. The score, the rank, the
    #: bucket description, the rule that matched and the reason all live on
    #: `package_priority`, and this column is what lets the most-read table in the
    #: product filter and sort by bucket without a join.
    #:
    #: **The default is `unknown` and never `p10`.** A package no rule matched, a
    #: run at a version recording no rule set, and a package whose identity was
    #: never established all reach it. `p10` as a default would be a claim about a
    #: package's importance that nobody made, wearing the one value that looks
    #: harmless.
    priority_status = models.CharField(
        _("priority"),
        max_length=PRIORITY_BUCKET_LENGTH,
        choices=PriorityBucket.choices,
        default=PRIORITY_STATUS_UNKNOWN,
        editable=False,
    )

    #: Which of `CPM-FR-21`'s eight work types this package's state recommends,
    #: gated by `CPM-AD-4` on the way in. The fourth domain status column, added by
    #: `CPM-PRIORITY-S02` with the pass that produces it.
    #:
    #: **It is not derived from `priority_status` and the two are not coupled**,
    #: which is `CPM-PRIORITY-S02`'s AC 1. They sit side by side on this row because
    #: a queue shows both, and that adjacency is the whole hazard: a low-priority
    #: package still has a recommended action, and a reader who inferred one column
    #: from the other would lose exactly that. `policies/work_type.py` reads neither
    #: the bucket nor the priority table, and is registered *before* the priority
    #: pass so it could not.
    work_type_status = models.CharField(
        _("work type"),
        max_length=WORK_TYPE_LENGTH,
        choices=WorkType.choices,
        default=WORK_TYPE_UNKNOWN,
        editable=False,
    )

    class Meta:
        """The table the architecture names, not the `core_packagehealth` Django derives.

        Rejected for the reason `CollectionRun.Meta` and `Package.Meta` reject
        theirs: the schema is named by the architecture rather than by which
        application happens to declare the model.
        """

        db_table = "package_health"
        verbose_name = _("package health")
        verbose_name_plural = _("package health")
        indexes = [
            # The two columns this model's own field comments call the ones a
            # read surface reads. `evidence_cutoff` is what "how old is the
            # evidence behind this" filters and sorts on -- it is copied onto the
            # row rather than joined precisely so it can be -- and `computed_at`
            # is the staleness column, the one "which packages has nothing
            # recomputed lately" scans. Neither is indexed by anything else:
            # `package` gets its index from the one-to-one and `policy_run` from
            # the foreign key, and an argument in a docstring for a column nobody
            # can query cheaply is an argument that stops being true at the first
            # full inventory.
            models.Index(fields=["evidence_cutoff"], name="package_health_cutoff"),
            models.Index(fields=["computed_at"], name="package_health_computed"),
        ]

    def __str__(self) -> str:
        """Return the package this row is about and when it was computed.

        Returns:
            A one-line summary naming the package and the instant the row was
            computed at, or saying that either is absent. Read off `package_id`
            rather than `package`, for the reason `Feedstock.__str__` is: the
            related object of an unsaved instance raises
            `RelatedObjectDoesNotExist`, and a `__str__` that raises is what a
            failure message would have been.

        """
        scope = "no package" if self.package_id is None else f"package {self.package_id}"
        computed = "not computed" if self.computed_at is None else self.computed_at.isoformat()
        return f"health of {scope} at {computed}"


class BackgroundJob(models.Model):
    """One piece of work a request handed off. Table `background_jobs`.

    `CPM-AD-9` splits the product at the request boundary and `CPM-APP-S08` adds what
    the request returns instead of the work: an in-progress state, which has to be a
    row somebody can point at. A boundary a request can cross but not point back
    across is one where the work simply disappears.

    **Not append-only, and it is the only table here that is not.** `CPM-AD-2` is
    about evidence -- what a source said at an instant, which cannot stop being true.
    A job is not evidence about anything; it is a piece of work with a lifecycle, and
    a second row per state change would make "is this done" a query rather than a
    read. What it keeps instead is its three timestamps, which is what an operator
    asking why something took four minutes actually reads.

    **The artifact is stored in the row rather than in a file, and that is a v1
    decision with a reason rather than a shortcut.** `STORAGES` names
    `FileSystemStorage` in production: a worker writing to its own container's disk
    produces a file the web replica serving the download cannot see, and the failure
    is a 404 that reproduces on some requests and not others. Object storage would
    fix it and is not configured. Until it is, the database is the one place both
    processes can reach -- and `CPM-NFR-1`'s ten thousand packages put the worst case
    at a couple of megabytes of text, which Postgres stores out of line without being
    asked. A later story that configures object storage moves this column and nothing
    else.

    **`kind` is a registry key, not a choice list.** `core` may not know that an
    export exists -- `surface` owns reports and `core` may not import it -- so the
    kinds are whatever the applications adopted at `ready()` have registered. See
    `core/jobs.py`.
    """

    #: What kind of work this is, and therefore which registered runner runs it.
    kind = models.CharField(_("kind"), max_length=KIND_LENGTH)

    #: What it is work *on*. A mapping rather than columns, because `core` cannot
    #: name a report slug without knowing what a report is -- and a job kind added
    #: later would need its own columns here, which is a migration per kind.
    parameters = models.JSONField(_("parameters"), default=dict, blank=True)

    #: Where it has got to. Never blank: `queued` is the state a job is created in.
    state = models.CharField(
        _("state"),
        max_length=STATE_LENGTH,
        choices=JobState.choices,
        default=JobState.QUEUED.value,
    )

    #: Who asked. `SET_NULL` so a job survives the account that requested it -- the
    #: record of what ran is not the record of who is still employed -- and nullable
    #: for exactly that reason rather than because a job may be anonymous. Nothing
    #: creates one without an actor.
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="background_jobs",
        verbose_name=_("requested by"),
    )

    #: The three stamps that are this row's history. `requested_at` is set when the
    #: request hands off, the other two by the worker at each end of the work.
    requested_at = models.DateTimeField(_("requested at"))
    started_at = models.DateTimeField(_("started at"), null=True, blank=True)
    finished_at = models.DateTimeField(_("finished at"), null=True, blank=True)

    #: What it produced. Empty until it succeeds, and empty for ever if it fails.
    artifact = models.TextField(_("artifact"), blank=True)

    #: How many rows the artifact covers, so a reader can tell a complete export from
    #: one that a filter narrowed to nothing without opening the file.
    row_count = models.PositiveIntegerField(_("row count"), default=0)

    #: Why it failed. **The presence of this is what makes the state `failed`**, so a
    #: failed job with nothing to read is not expressible -- which is the report an
    #: operator can do nothing with.
    detail = models.TextField(_("detail"), blank=True)

    class Meta:
        """Naming, ordering and the constraint that keeps a state honest."""

        db_table = "background_jobs"
        verbose_name = _("background job")
        verbose_name_plural = _("background jobs")
        # Newest first: a job list is read to find the one just requested.
        ordering = ("-requested_at", "-id")
        constraints = [
            # A finished job has a finish stamp and an unfinished one does not. The
            # failure this refuses is a page that says "running" about work that
            # ended an hour ago, which is indistinguishable from work that hung.
            models.CheckConstraint(
                condition=(
                    models.Q(state__in=(JobState.QUEUED.value, JobState.RUNNING.value), finished_at__isnull=True)
                    | models.Q(
                        state__in=(JobState.SUCCEEDED.value, JobState.FAILED.value),
                        finished_at__isnull=False,
                    )
                ),
                name="job_finish_stamp_matches_its_state",
            ),
            # A failed job says why, and a succeeded one has nothing to explain.
            models.CheckConstraint(
                condition=(
                    models.Q(state=JobState.FAILED.value, detail__gt="")
                    | ~models.Q(state=JobState.FAILED.value) & models.Q(detail="")
                ),
                name="a_failed_job_says_why",
            ),
        ]
        indexes = [
            # The one query a status page makes: this person's jobs, newest first.
            models.Index(fields=["requested_by", "-requested_at"], name="background_jobs_by_requester"),
        ]

    def __str__(self) -> str:
        """Return what this job is and where it has got to.

        Returns:
            A one-line summary. Reads `kind` and `state` only, both of which are
            non-null with defaults, so this cannot raise on an unsaved instance --
            the reason `Feedstock.__str__` reads an id rather than a relation.

        """
        return f"{self.kind} job ({self.state})"

    def in_progress(self) -> bool:
        """Report whether this job is still working.

        Returns:
            Whether the state is one a surface should keep polling.

        """
        return self.state in IN_PROGRESS_STATES
