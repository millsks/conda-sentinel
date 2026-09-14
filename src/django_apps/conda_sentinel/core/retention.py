"""Ninety days of evidence, purged nightly (`CPM-OPERATE-S07`).

Evidence is append-only and, until this module, nothing had ever removed a row
from any table. At ten thousand packages the seven daily tables grow roughly
seventy thousand rows a day between them and the collection ledger the same, and
no index led with a time column. The product owner's decision is ninety days, purged nightly,
and this module is that decision as code: one declared retention, one purge, one
audited door.

**The retention is a setting, not a policy parameter.** `CPM_EVIDENCE_RETENTION_DAYS`
is read from the environment by `config/settings/base.py` (default 90) and
refused below one at boot by `CollectorsConfig.ready()`, naming the setting.
There is no "forever" except by a number. It is deliberately *not* a versioned
policy parameter under `CPM-AD-8`: parameters are verdict rule data, a
per-deployment duration belongs with the other declared `CPM_*` settings, and
"which version's retention" is a question with no answer for a replay of an
older run. `RETENTION_SETTING` is the one spelling of the name; the settings
module and `tests/unit/test_settings.py` pin it.

**The door.** `AppendOnlyQuerySet.retire(*, door)` in `core/models.py` performs
a delete only when handed the token this module alone constructs -- `_DOOR`, an
instance of a module-private class, compared by identity. Every other refusal on
the base stands exactly as before; `tests/unit/django_apps/test_append_only_model.py`
pins each one. The mutation-path audit licenses the door's one raw delete and
this module's one `objects.delete(...)` by count, so a second deletion path
anywhere fails the gate.

**What is purged, in this order, and why the order is part of the correctness.**

1. Derived pass rows -- the `policies` tables that cite a `PolicyRun` -- of runs
   whose `finished_at` is older than the retention (or which never finished and
   started before it) *and* that no `package_health` row cites, then those runs.
   The rollup is never purged and pins its run. Derived rows cite evidence with
   `PROTECT`, so purging evidence first would skip every row an out-of-window
   run still cited and the ledger would keep shrink-resistant runs.
2. Evidence rows, per table in `EVIDENCE_ROSTER`, with `observed_at` before the
   cut-off, **excluding** what a reader could still be asked for -- see the floor
   rule below. `kev_findings` precedes `vulnerability_findings` because a KEV row
   cites the finding it derives from. A `ProtectedError` on a batch falls back to
   row-by-row deletion, skipping and counting the protected rows, never aborting.
3. `collection_runs` rows with `finished_at` older than the retention, and
   unfinished rows whose `started_at` is -- a killed worker's row, which would
   otherwise bound `choose_evidence_cutoff` for ever -- counted separately.

**The floor rule, which is the invariant at the centre of this story.** Every
pass reads the newest row at-or-before its run's cut-off, per reader key
(`policies/currency.py`, `feedstock.py`, `vulnerability.py`, `licence.py`,
`remediation.py`, `py314_readiness.py`), and rows sharing that newest instant are
read together as one sweep. So a row is removable only when it is older than the
retention cut-off, a strictly newer row exists under the same key (the newest per
key is never purged, ties included), and **no surviving policy run's
`evidence_cutoff` falls at or after it and before that newer row** -- the
half-open interval in which a replay of that run would read it. `PROTECT` from
the derived tables then guards the rows a pass *cited* as a safety net rather
than as the rule, because a pass cites one row and may read a set.

The keys are the readers', not the tables', and they are a **deliberately
conservative superset** of what each reader distinguishes: `(package, channel,
platform)` for `conda_package_snapshots`, `(package, channel)` for
`license_findings`, `(package, advisory)` for the two advisory tables -- the KEV
table's advisory is reached through the finding it links to, coalesced so a row
that links to none shares one key with the others that link to none --
`(package, python_series)` for the two Python 3.14 tables, whose readers filter
by series before taking the newest, and `(package, source_package_key)` for
`inventory_snapshots`, whose absence reader folds by key. A finer key than the
reader's can only keep more: every reader takes the newest row at or before the
cut-off *per package* and reads the whole sweep at that instant, and a row that
is the newest under a finer key at that instant is in that sweep. What the
superset costs is stated rather than left to be found: the newest row per
advisory, per channel, per platform or per key ever seen is kept for ever, one
row each, however long ago the advisory closed or the channel stopped publishing.
`tests/unit/django_apps/test_retention.py` reconciles the roster against the
evidence models `tests/model_registry.py` classifies.

**Rows a retained row cites are not selected, and the collector is the safety
net rather than the steady state.** A derived row of a surviving run cites the
evidence it was computed from with `PROTECT`, and a KEV row cites the finding it
derives from; the floor rule keeps almost all of those already, but a citation
under a key the rule does not share -- a KEV row linking an old finding whose
advisory has since been re-observed -- would otherwise be re-selected every night
and refused every night. So the selection excludes rows cited by any surviving
citer, through the model's reverse relations, and `retire`'s `PROTECT` check
catches only what was cited between the selection and the `DELETE`.

**What the ledger purge bounds.** `collection_runs` rows older than the retention
go, so `CPM-FR-38`'s "which runs failed" reaches back ninety days and no further,
the Coverage screen's last-observed answer does too -- a collector silent for
ninety days reads as never run -- and the package page's observation history is
the retention's. `choose_evidence_cutoff` never reads this module's own rows: a
purge's ending is not a collection's, and the exclusion is by name in
`core/policy_run.py`.

**A purge that died is closed by the next one.** Each purge finalizes every
unfinished `prune_evidence` row it finds at start as `failed`, saying so, because
a row a killed purge left `running` would otherwise stay on the ledger for ninety
days. A cut-off that has moved further than the time since the last purge -- an
operator lowering the retention -- is logged as a warning naming both instants;
it is not refused, because the setting is the declaration.

**One `CollectionRun` per table and not a third ledger.** The spine names two
ledgers; a third for a record whose only reader is an operator is a spine
amendment. Each table's purge is recorded under the unregistered collector name
`PRUNE_COLLECTOR`, which never appears on the Coverage screen, and the row's
`detail` carries the table, the cut-off, the counts and whether it was a
rehearsal. Each batch is its own `transaction.atomic()`, never the recorder.

**What is never purged, and named:** `identity_overrides` and
`inventory_changes` (one row per human act, the audit trail of governed writes;
excluded by the product owner's decision, reversible), `package_health` (the
rollup, `CPM-AD-11`), and every `packages`, `identity` and `workflow` table.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from datetime import timedelta
from typing import TYPE_CHECKING
from typing import Any
from typing import Final
from typing import cast

import structlog
from django.apps import apps
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import DatabaseError
from django.db import IntegrityError
from django.db import models
from django.db import transaction
from django.db.models import Exists
from django.db.models import F
from django.db.models import OuterRef
from django.db.models import ProtectedError
from django.db.models import Q
from django.db.models import RestrictedError
from django.db.models import Value
from django.db.models.functions import Coalesce

from conda_sentinel.core.clock import is_aware
from conda_sentinel.core.ledger import abandon
from conda_sentinel.core.ledger import collection_run
from conda_sentinel.core.models import FINISHED_AT_FIELD
from conda_sentinel.core.models import AppendOnlyModel
from conda_sentinel.core.models import CollectionRun
from conda_sentinel.core.models import PackageHealth
from conda_sentinel.core.models import PolicyRun

if TYPE_CHECKING:
    from collections.abc import Callable
    from collections.abc import Iterable
    from collections.abc import Iterator

    from conda_sentinel.core.clock import Clock

__all__ = [
    "DEFAULT_BATCH_SIZE",
    "DEFAULT_RETENTION_DAYS",
    "DELETED_LEG",
    "EVIDENCE_ROSTER",
    "EXCLUDED_EVIDENCE",
    "MAXIMUM_BATCH_SIZE",
    "MAXIMUM_RETENTION_DAYS",
    "MINIMUM_RETENTION_DAYS",
    "PRUNE_COLLECTOR",
    "RETENTION_SETTING",
    "ROLLUP_LABEL",
    "EvidenceTable",
    "TablePurge",
    "derived_tables",
    "floor_removable_evidence",
    "is_retention_door",
    "purge_evidence",
    "purgeable_policy_runs",
    "removable_evidence",
    "retention_cutoff",
    "retention_days",
    "retention_fault",
]

logger: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

#: The setting `config/settings/base.py` assigns the retention to, in days.
#: Spelled once, here, so the boot refusal, the command and the replay's window
#: all name the same thing the settings module reads.
RETENTION_SETTING: Final[str] = "CPM_EVIDENCE_RETENTION_DAYS"

#: What ships: ninety days, the product owner's decision.
DEFAULT_RETENTION_DAYS: Final[int] = 90

#: The smallest retention the component will boot with. There is no "forever"
#: except by a number, and zero would purge every row the moment it was written.
MINIMUM_RETENTION_DAYS: Final[int] = 1

#: The largest: ten years. "Forever by a number" is still a number, and a value
#: past this is far more likely a unit mistake (seconds, hours) than a decision.
MAXIMUM_RETENTION_DAYS: Final[int] = 3650

#: How many rows one `DELETE` may remove. Never unbounded (`CPM-AD-23`): a first
#: run against a table nobody has pruned in months is otherwise one statement
#: holding one row lock per row until it ends, or exceeding `statement_timeout`
#: and rolling back having made no progress at all.
DEFAULT_BATCH_SIZE: Final[int] = 1000

#: The largest batch the command accepts. Each key in a batch is one bound
#: parameter of the `DELETE ... WHERE id IN (...)`, and SQLite -- the suite's
#: local backend -- refuses a statement past 32,766 of them; ten thousand keeps
#: every batch well inside that with room for the selection's own parameters.
MAXIMUM_BATCH_SIZE: Final[int] = 10_000

#: How far the cut-off may move beyond the time elapsed since the previous purge
#: before the move is logged. A nightly purge moves it by a night; a lowered
#: retention moves it by the difference, which an operator should see.
CUTOFF_DRIFT_TOLERANCE: Final[timedelta] = timedelta(days=1)

#: What a purge writes onto a `prune_evidence` row an earlier purge left running.
SUPERSEDED_DETAIL: Final[str] = "superseded by a later purge: this purge never finished"

#: The collector name every purge record is filed under. Deliberately not a
#: registered collector: the Coverage screen reads the ledger by registered name,
#: so these rows never appear there, exactly as the demo seeder's do not.
PRUNE_COLLECTOR: Final[str] = "prune_evidence"

#: The rollup that pins a policy run (`CPM-AD-11`): a run any `package_health`
#: row cites is never purged, whatever its age, and neither are its derived rows.
ROLLUP_LABEL: Final[str] = "core.PackageHealth"

#: The application whose tables hold derived pass rows. Reverse relations to
#: `PolicyRun` from any other application are left alone -- and left to
#: `PROTECT`, which skips and counts the run rather than removing what cites it.
DERIVED_APP_LABEL: Final[str] = "policies"

#: The column every reader key starts with.
PACKAGE_KEY: Final[str] = "package_id"

#: The evidence column every cut-off scan leads with.
OBSERVED_AT_FIELD: Final[str] = "observed_at"

#: The ledger column an unfinished row is aged by.
STARTED_AT_FIELD: Final[str] = "started_at"

#: The leg name every single-selection table counts under. A table with more
#: than one leg (`collection_runs`) names each; the record's `deleted=` is
#: always the total.
DELETED_LEG: Final[str] = "deleted"

#: The two structlog events, one per table per run. Two names rather than one
#: plus a flag, for the reason `prune_expired_state` gives: the name is what an
#: operator alerts on, and a rehearsal that emitted `...purged` would be counted
#: as a purge that happened.
TABLE_PURGED_EVENT: Final[str] = "retention.table_purged"
TABLE_PURGEABLE_EVENT: Final[str] = "retention.table_purgeable"

#: A table's purge that a `DatabaseError` stopped part-way, recorded and passed over.
TABLE_FAILED_EVENT: Final[str] = "retention.table_failed"

#: Stale `prune_evidence` rows closed at start, and a cut-off that moved further
#: than the time since the last purge.
STALE_PURGES_ABANDONED_EVENT: Final[str] = "retention.stale_purges_abandoned"
CUTOFF_MOVED_EVENT: Final[str] = "retention.cutoff_moved"


class _RetentionDoor:
    """The token `AppendOnlyQuerySet.retire` opens for, and nothing else.

    Module-private, with exactly one instance, compared by identity: the door is
    not a capability a caller can mint, it is this module's own key. A second
    instance -- however it were constructed -- opens nothing.
    """

    __slots__ = ()

    def __repr__(self) -> str:
        """Name the token without pretending it is a value.

        Returns:
            A fixed spelling, so a refusal message can say what was handed in.

        """
        return "<the retention door>"


_DOOR: Final[_RetentionDoor] = _RetentionDoor()


def is_retention_door(candidate: object) -> bool:
    """Report whether an object is the one token that opens the door.

    Args:
        candidate: Whatever `retire(door=...)` was handed.

    Returns:
        True for `_DOOR` itself and for nothing else -- not another instance of
        its class, not a subclass, not a look-alike.

    """
    return candidate is _DOOR


# ---------------------------------------------------------------------------
# The retention, as a setting.
# ---------------------------------------------------------------------------


def retention_fault(declared: object) -> str:
    """Return why a declared retention is unusable, or `""` when it is usable.

    The one rule, asked at boot by `CollectorsConfig.ready()` and at run time by
    `retention_days`, so the two cannot come to disagree.

    Args:
        declared: What the settings module assigned to `RETENTION_SETTING`.

    Returns:
        A sentence naming the setting and the rule it breaks, or the empty string.
        `bool` is refused although it is an `int`: `True` days is a mistake, not
        a declaration.

    """
    if isinstance(declared, bool) or not isinstance(declared, int):
        return (
            f"{RETENTION_SETTING} must be a whole number of days, not {type(declared).__name__}. "
            f"config/settings/base.py reads it from the environment as an integer, default "
            f"{DEFAULT_RETENTION_DAYS} (CPM-OPERATE-S07)."
        )
    if declared < MINIMUM_RETENTION_DAYS:
        return (
            f"{RETENTION_SETTING} is {declared}, below the minimum of {MINIMUM_RETENTION_DAYS}. There is no "
            f"'forever' except by a number, and a retention below one day would purge evidence the moment it "
            f"was observed (CPM-OPERATE-S07)."
        )
    if declared > MAXIMUM_RETENTION_DAYS:
        return (
            f"{RETENTION_SETTING} is {declared}, above the maximum of {MAXIMUM_RETENTION_DAYS} days (ten years). "
            f"A value that large is far more likely a unit mistake than a decision; state the retention in days "
            f"(CPM-OPERATE-S07)."
        )
    return ""


def retention_days() -> int:
    """Return the declared retention, in days.

    Returns:
        The value of `RETENTION_SETTING`.

    Raises:
        ImproperlyConfigured: When the setting is absent, not an integer, or below
            `MINIMUM_RETENTION_DAYS`. Refused here as well as at boot, so a value
            that reached a process around the hook still never becomes a cut-off.

    """
    declared = getattr(settings, RETENTION_SETTING, None)
    if declared is None:
        message = (
            f"{RETENTION_SETTING} is not configured, so this component cannot tell how long it keeps evidence. "
            f"config/settings/base.py assigns it -- {DEFAULT_RETENTION_DAYS} by default -- and a settings "
            f"module with no assignment at all is one that dropped the line (CPM-OPERATE-S07)."
        )
        raise ImproperlyConfigured(message)
    fault = retention_fault(declared)
    if fault:
        raise ImproperlyConfigured(fault)
    return cast("int", declared)


def retention_cutoff(*, now: datetime, days: int) -> datetime:
    """Return the instant before which a row is older than the retention.

    Args:
        now: The clock's answer (`CPM-AD-26`), never the wall clock read here.
        days: The retention.

    Returns:
        `now` minus the retention.

    Raises:
        ValueError: When `now` is naive. Every instant this product records is
            aware (`CPM-AD-26`); a naive `now` compared against them would draw
            the window at an offset nobody chose.

    """
    if not is_aware(now):
        message = (
            f"the retention cut-off cannot be drawn from the naive instant {now!r}. Every instant comes from a "
            f"Clock, which always answers in UTC (CPM-AD-26); a naive value has no offset to interpret and the "
            f"window would be shifted by whichever offset the caller happened to be in."
        )
        raise ValueError(message)
    return now - timedelta(days=days)


# ---------------------------------------------------------------------------
# The roster.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EvidenceTable:
    """One purged evidence table and the key its readers take the newest row under.

    Attributes:
        label: The model's `app_label.ModelName`, resolved through the app
            registry rather than imported: `core` may not import a downstream
            application's models (`tests/unit/django_apps/test_app_layering_audit.py`).
        keys: The reader key, always leading with `package_id`. A key containing
            `__` is reached through a relation and is coalesced to `""`, so rows
            with no related row share one key rather than each being its own
            newest.

    """

    label: str
    keys: tuple[str, ...]

    @property
    def model(self) -> type[AppendOnlyModel]:
        """Return the model, from the app registry.

        Returns:
            The concrete evidence model.

        """
        return cast("type[AppendOnlyModel]", apps.get_model(self.label))

    @property
    def table(self) -> str:
        """Return the table name.

        Returns:
            The model's `db_table`.

        """
        return str(self.model._meta.db_table)  # noqa: SLF001 - `_meta` is Django's own public-by-convention API


#: The eleven purged evidence tables, in purge order. `KevFinding` precedes
#: `VulnerabilityFinding` because it cites it (`PROTECT`).
EVIDENCE_ROSTER: Final[tuple[EvidenceTable, ...]] = (
    EvidenceTable("collectors.InventorySnapshot", (PACKAGE_KEY, "source_package_key")),
    EvidenceTable("collectors.SourceReleaseSnapshot", (PACKAGE_KEY,)),
    EvidenceTable("collectors.PyPIReleaseSnapshot", (PACKAGE_KEY,)),
    EvidenceTable("collectors.FeedstockSnapshot", (PACKAGE_KEY,)),
    EvidenceTable("collectors.CondaPackageSnapshot", (PACKAGE_KEY, "channel", "platform")),
    EvidenceTable("collectors.KevFinding", (PACKAGE_KEY, "vulnerability_finding__advisory_id")),
    EvidenceTable("collectors.VulnerabilityFinding", (PACKAGE_KEY, "advisory_id")),
    EvidenceTable("collectors.LicenseFinding", (PACKAGE_KEY, "channel")),
    EvidenceTable("collectors.PythonReadinessAssessment", (PACKAGE_KEY, "python_series")),
    EvidenceTable("collectors.PythonVerificationResult", (PACKAGE_KEY, "python_series")),
    EvidenceTable("collectors.IdentityResolutionSnapshot", (PACKAGE_KEY,)),
)

#: The three evidence tables the purge never touches: one row per human act --
#: the audit trail of the two governed writes, and of every manual recollection
#: (`CPM-OPERATE-S08`). The product owner may reverse this.
EXCLUDED_EVIDENCE: Final[frozenset[str]] = frozenset(
    {"collectors.InventoryChange", "collectors.PackageRecollection", "identity.IdentityOverride"},
)


def derived_tables() -> tuple[tuple[type[models.Model], str], ...]:
    """Return every derived pass table and the name of its `policy_run` relation.

    Derived from the reverse relations to `PolicyRun` rather than listed, so a
    ninth pass table is purged with its run rather than protecting it for ever;
    restricted to `policies` so nothing else that came to cite a run -- a
    `workflow` table, say -- could be purged by this rule. The rollup is the pin
    and is never here. `tests/unit/django_apps/test_retention.py` reconciles the
    result against the eight tables and asserts the rollup is the only other
    citer.

    Returns:
        `(model, relation name)` pairs, in registry order.

    """
    found: list[tuple[type[models.Model], str]] = []
    for relation in PolicyRun._meta.related_objects:  # noqa: SLF001 - `_meta` is Django's own public-by-convention API
        related = relation.related_model
        if isinstance(related, type) and related._meta.app_label == DERIVED_APP_LABEL:  # noqa: SLF001 - as above
            found.append((related, relation.field.name))
    return tuple(found)


# ---------------------------------------------------------------------------
# The selections.
# ---------------------------------------------------------------------------


def purgeable_policy_runs(*, cutoff: datetime) -> models.QuerySet[PolicyRun]:
    """Return the policy runs older than the retention that nothing pins.

    Args:
        cutoff: The retention cut-off.

    Returns:
        Runs that finished before the cut-off, or never finished and started
        before it, and that no `package_health` row cites.

    """
    older = Q(**{f"{FINISHED_AT_FIELD}__lt": cutoff}) | Q(
        **{f"{FINISHED_AT_FIELD}__isnull": True, f"{STARTED_AT_FIELD}__lt": cutoff},
    )
    pinned = PackageHealth.objects.filter(policy_run_id=OuterRef("pk"))
    return PolicyRun.objects.filter(older).filter(~Exists(pinned))


def _surviving_policy_runs(*, cutoff: datetime) -> models.QuerySet[PolicyRun]:
    """Return the policy runs the purge leaves in place, whether or not it has run yet.

    Spelled as the complement of `purgeable_policy_runs` rather than as "every
    run", so a rehearsal computes the same floor a real run does.

    Args:
        cutoff: The retention cut-off.

    Returns:
        Every run not selected for purging.

    """
    return PolicyRun.objects.exclude(pk__in=purgeable_policy_runs(cutoff=cutoff).values("pk"))


def _alias(key: str) -> str:
    """Return the annotation name a joined key is compared under.

    Args:
        key: A reader key.

    Returns:
        The key itself for a column; a flat spelling for a relation path.

    """
    return key.replace("__", "_") if "__" in key else key


def _keyed(entry: EvidenceTable) -> models.QuerySet[AppendOnlyModel]:
    """Return the table's rows with every joined key annotated, coalesced to `""`.

    Args:
        entry: The roster entry.

    Returns:
        The queryset, with one annotation per joined key.

    """
    joined = {_alias(key): Coalesce(F(key), Value("")) for key in entry.keys if "__" in key}
    return entry.model.objects.annotate(**joined)


def floor_removable_evidence(entry: EvidenceTable, *, cutoff: datetime) -> models.QuerySet[AppendOnlyModel]:
    """Return the rows of one evidence table the floor rule alone lets the purge remove.

    A row is removable when it is older than the cut-off, a strictly newer row
    exists under the same reader key, and no surviving policy run's cut-off lies
    in the half-open interval between it and that newer row -- the interval in
    which a replay of that run reads it. Rows sharing the newest instant are
    kept together, because readers take a sweep, not a row.

    Args:
        entry: The roster entry.
        cutoff: The retention cut-off.

    Returns:
        The rows, unordered and unbounded. `removable_evidence` narrows them
        further by what a surviving row cites.

    """
    aliases = tuple(_alias(key) for key in entry.keys)
    same_key = _keyed(entry).filter(**{alias: OuterRef(alias) for alias in aliases})
    newer = same_key.filter(**{f"{OBSERVED_AT_FIELD}__gt": OuterRef(OBSERVED_AT_FIELD)})
    # Two levels down: the row is the outer query's, the cut-off the run's.
    same_key_two_up = _keyed(entry).filter(**{alias: OuterRef(OuterRef(alias)) for alias in aliases})
    superseded_before_the_cutoff = same_key_two_up.filter(
        **{
            f"{OBSERVED_AT_FIELD}__gt": OuterRef(OuterRef(OBSERVED_AT_FIELD)),
            f"{OBSERVED_AT_FIELD}__lte": OuterRef("evidence_cutoff"),
        },
    )
    read_by_a_surviving_run = (
        _surviving_policy_runs(cutoff=cutoff)
        .filter(evidence_cutoff__gte=OuterRef(OBSERVED_AT_FIELD))
        .filter(~Exists(superseded_before_the_cutoff))
    )
    return (
        _keyed(entry)
        .filter(**{f"{OBSERVED_AT_FIELD}__lt": cutoff})
        .filter(Exists(newer))
        .filter(~Exists(read_by_a_surviving_run))
    )


def _roster_entry(model: type[models.Model]) -> EvidenceTable | None:
    """Return the roster entry for a model, or `None` when it is not purged.

    Args:
        model: Any model.

    Returns:
        The entry, or `None`.

    """
    label = str(model._meta.label)  # noqa: SLF001 - `_meta` is Django's own public-by-convention API
    return next((entry for entry in EVIDENCE_ROSTER if entry.label == label), None)


def _surviving_citers(entry: EvidenceTable, *, cutoff: datetime) -> list[models.QuerySet[Any]]:
    """Return, per relation pointing at the table, the citing rows that will still exist after this purge.

    One queryset per reverse relation, each correlated on the cited row's key. A
    derived table's rows survive with their run; another purged evidence
    table's rows survive when they are not themselves selected -- computed the
    same way, so a rehearsal and a real run agree; any other citer survives
    outright, because nothing here removes it.

    Args:
        entry: The roster entry.
        cutoff: The retention cut-off.

    Returns:
        The citers, each filtered to the cited row by `OuterRef("pk")`.

    """
    citers: list[models.QuerySet[Any]] = []
    for relation in entry.model._meta.related_objects:  # noqa: SLF001 - as above
        citing = relation.related_model
        if not isinstance(citing, type):
            continue
        rows = citing._default_manager.filter(**{relation.field.attname: OuterRef("pk")})  # noqa: SLF001 - Django's own accessor
        cited_by = _roster_entry(citing)
        if citing._meta.app_label == DERIVED_APP_LABEL:  # noqa: SLF001 - as above
            rows = rows.filter(policy_run_id__in=_surviving_policy_runs(cutoff=cutoff).values("pk"))
        elif cited_by is not None:
            rows = rows.exclude(pk__in=removable_evidence(cited_by, cutoff=cutoff).values("pk"))
        citers.append(rows)
    return citers


def removable_evidence(entry: EvidenceTable, *, cutoff: datetime) -> models.QuerySet[AppendOnlyModel]:
    """Return the rows of one evidence table the purge selects: the floor rule, less what a surviving row cites.

    Args:
        entry: The roster entry.
        cutoff: The retention cut-off.

    Returns:
        The removable rows, unordered and unbounded; the purge takes them in
        primary-key order, `batch_size` at a time.

    """
    selection = floor_removable_evidence(entry, cutoff=cutoff)
    for citer in _surviving_citers(entry, cutoff=cutoff):
        selection = selection.filter(~Exists(citer))
    return selection


# ---------------------------------------------------------------------------
# The purge.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Leg:
    """One counted selection within a table's purge.

    Attributes:
        name: What the count is called in the run record's `detail`.
        model: The table the keys belong to.
        select: The rows still to remove, re-asked after every batch.
        remove: Remove the rows with these primary keys and return how many went.

    """

    name: str
    model: type[models.Model]
    select: Callable[[], models.QuerySet[Any]]
    remove: Callable[[list[int]], int]


@dataclass(frozen=True)
class _Plan:
    """One table's purge, before it runs.

    Attributes:
        table: The table name, for the record.
        legs: The counted selections.
        older: How many rows are older than the cut-off before anything goes, for
            a table with a floor rule; `None` for one without.

    """

    table: str
    legs: tuple[_Leg, ...]
    older: Callable[[], int] | None = None


@dataclass(frozen=True)
class TablePurge:
    """What one table's purge did, or would have done.

    Attributes:
        table: The table name.
        cutoff: The retention cut-off the selection was made at.
        removed: The count per leg -- `deleted` for most tables; `finished` and
            `unfinished` for `collection_runs`. In a rehearsal, what would go.
        kept_by_rule: Rows older than the cut-off the floor rule kept: the newest
            per key, the row a surviving run reads at its cut-off, and the row a
            surviving row cites. Zero for the ledgers and the derived tables,
            whose selection has no such rule.
        protected: Rows the database refused to remove because a retained row
            came to cite them between the selection and the statement, skipped
            and left in place.
        dry_run: Whether nothing was removed.
        run_id: The `CollectionRun` recording this purge.
        error: What stopped this table's purge part-way, or `None`. The counts
            are what was committed before it.

    """

    table: str
    cutoff: datetime
    removed: tuple[tuple[str, int], ...]
    kept_by_rule: int
    protected: int
    dry_run: bool
    run_id: int
    error: str | None = None

    @property
    def deleted(self) -> int:
        """Return the total removed across the legs.

        Returns:
            The sum of the per-leg counts.

        """
        return sum(count for _name, count in self.removed)

    @property
    def detail(self) -> str:
        """Return the sentence the run record carries.

        Returns:
            `key=value` pairs naming the table, the cut-off, every count, whether
            this was a rehearsal, and the error where one stopped it.

        """
        parts = [
            f"table={self.table}",
            f"cutoff={self.cutoff.isoformat()}",
            f"deleted={self.deleted}",
            *(f"{name}={count}" for name, count in self.removed if name != DELETED_LEG),
            f"kept_by_rule={self.kept_by_rule}",
            f"protected={self.protected}",
            f"dry_run={str(self.dry_run).lower()}",
        ]
        if self.error is not None:
            parts.append(f"error={self.error}")
        return " ".join(parts)


def _batches(select: Callable[[], models.QuerySet[Any]], *, batch_size: int) -> Iterator[list[int]]:
    """Yield the primary keys still selected, `batch_size` at a time, until none are.

    Re-asked after every batch rather than read once: a batch that skipped
    protected rows would otherwise be offered them again for ever, and a
    selection whose predicate depends on rows already removed stays honest.

    Args:
        select: The selection.
        batch_size: The bound on one `DELETE`.

    Yields:
        Primary keys in ascending order, never more than `batch_size`.

    """
    watermark = 0
    while True:
        ids = list(select().filter(pk__gt=watermark).order_by("pk").values_list("pk", flat=True)[:batch_size])
        if not ids:
            return
        watermark = int(ids[-1])
        yield [int(pk) for pk in ids]


def _cited_keys(model: type[models.Model], citers: Iterable[models.Model]) -> set[int]:
    """Return the keys of `model`'s rows the given citing rows point at.

    `ProtectedError.protected_objects` names the *citing* rows, fetched with only
    their own keys; the cited keys are read back from each citing table in one
    query per table rather than one per row.

    Args:
        model: The table whose rows were refused.
        citers: The rows that cite them.

    Returns:
        The cited primary keys.

    """
    by_model: defaultdict[type[models.Model], list[object]] = defaultdict(list)
    for citer in citers:
        by_model[type(citer)].append(citer.pk)
    cited: set[int] = set()
    for citing, keys in by_model.items():
        attnames = [
            field.attname
            for field in citing._meta.concrete_fields  # noqa: SLF001 - as above
            if field.is_relation and field.related_model is model
        ]
        for attname in attnames:
            rows = citing._default_manager.filter(pk__in=keys).values_list(attname, flat=True)  # noqa: SLF001 - Django's own accessor
            cited.update(int(key) for key in rows if key is not None)
    return cited


def _remove_one_by_one(leg: _Leg, ids: list[int]) -> tuple[int, int]:
    """Remove rows one transaction at a time, skipping and counting each refused one.

    Args:
        leg: The leg being purged.
        ids: The keys.

    Returns:
        `(removed, protected)`.

    """
    removed = 0
    protected = 0
    for pk in ids:
        try:
            with transaction.atomic():
                removed += leg.remove([pk])
        except ProtectedError, RestrictedError, IntegrityError:
            protected += 1
    return removed, protected


def _remove_batch(leg: _Leg, ids: list[int]) -> tuple[int, int]:
    """Remove one batch atomically, retrying once without the rows a citer protected.

    Three refusals are handled. `ProtectedError` names the citing rows before
    any statement, so the cited keys are subtracted and the batch retried once;
    only a retry refused again falls to one row per transaction.
    `RestrictedError` names them too but is rarer, and `IntegrityError` is a
    citation committed between the selection and the `DELETE` -- on PostgreSQL a
    deferred foreign key raises it at commit, naming no row -- so both fall to
    one row at a time, where each refusal is one row.

    Args:
        leg: The leg being purged.
        ids: The batch.

    Returns:
        `(removed, protected)`.

    """
    try:
        with transaction.atomic():
            return leg.remove(ids), 0
    except ProtectedError as refusal:
        cited = _cited_keys(leg.model, refusal.protected_objects) & set(ids)
        remaining = [pk for pk in ids if pk not in cited]
        if not remaining:
            return 0, len(cited)
        try:
            with transaction.atomic():
                return leg.remove(remaining), len(cited)
        except ProtectedError, RestrictedError, IntegrityError:
            removed, protected = _remove_one_by_one(leg, remaining)
            return removed, protected + len(cited)
    except RestrictedError, IntegrityError:
        return _remove_one_by_one(leg, ids)


def _purge_table(plan: _Plan, *, cutoff: datetime, clock: Clock, batch_size: int, dry_run: bool) -> TablePurge:
    """Purge one table inside its own run record, one bounded batch per transaction.

    A `DatabaseError` stops this table and no other: the record is finalized
    `failed` carrying the counts committed before it, and the caller carries on
    to the next table. A table whose rows were partly refused is `partial`.

    Args:
        plan: The table, its selections and its rule.
        cutoff: The retention cut-off.
        clock: The clock the record's instants come from.
        batch_size: The bound on one `DELETE`.
        dry_run: When true, count and record, remove nothing.

    Returns:
        What happened.

    """
    with collection_run(collector=PRUNE_COLLECTOR, clock=clock) as handle:
        removed: dict[str, int] = {leg.name: 0 for leg in plan.legs}
        protected = 0
        older: int | None = None
        error: str | None = None
        try:
            older = plan.older() if plan.older is not None else None
            for leg in plan.legs:
                if dry_run:
                    removed[leg.name] = leg.select().count()
                    continue
                for ids in _batches(leg.select, batch_size=batch_size):
                    went, skipped = _remove_batch(leg, ids)
                    removed[leg.name] += went
                    protected += skipped
        except DatabaseError as failure:
            error = f"{type(failure).__name__}: {failure}"
        deleted = sum(removed.values())
        result = TablePurge(
            table=plan.table,
            cutoff=cutoff,
            removed=tuple(removed.items()),
            # Removability is monotone under deletion, so what the rule kept is
            # what was older and neither went nor was refused.
            kept_by_rule=0 if older is None else older - deleted - protected,
            protected=protected,
            dry_run=dry_run,
            run_id=int(handle.run.pk),
            error=error,
        )
        if error is not None:
            handle.failed(detail=result.detail)
        elif protected:
            handle.partial(detail=result.detail)
        else:
            handle.succeeded(detail=result.detail)
    if error is not None:
        event = TABLE_FAILED_EVENT
    elif dry_run:
        event = TABLE_PURGEABLE_EVENT
    else:
        event = TABLE_PURGED_EVENT
    logger.info(
        event,
        table=result.table,
        cutoff=result.cutoff.isoformat(),
        deleted=result.deleted,
        removed=dict(result.removed),
        kept_by_rule=result.kept_by_rule,
        protected=result.protected,
        dry_run=result.dry_run,
        run_id=result.run_id,
        error=result.error,
    )
    return result


def _delete(model: type[models.Model]) -> Callable[[list[int]], int]:
    """Return the remover for a table that is not append-only.

    The one manager `delete(...)` in this module, licensed by count in
    `tests/unit/django_apps/test_mutation_path_audit.py`: it serves the derived
    tables and both ledgers, none of which is evidence. Through
    `_default_manager` -- which is `objects` on every one of them -- because the
    derived tables arrive as `type[Model]` from the reverse relations and that
    is the accessor Django declares on the base.

    Args:
        model: A derived or run-ledger model.

    Returns:
        A callable removing the rows with the given keys through Django's
        collector, so a `PROTECT` relation still refuses.

    """

    def remove(ids: list[int]) -> int:
        _total, per_label = model._default_manager.filter(pk__in=ids).delete()  # noqa: SLF001 - Django's own accessor
        return int(per_label.get(str(model._meta.label), 0))  # noqa: SLF001 - `_meta` is Django's own public-by-convention API

    return remove


def _retire(model: type[AppendOnlyModel]) -> Callable[[list[int]], int]:
    """Return the remover for an evidence table: the door, and nothing else.

    The one `retire(...)` in this module, licensed by count in the mutation-path
    audit beside the door itself.

    Args:
        model: An evidence model.

    Returns:
        A callable removing the rows with the given keys through
        `AppendOnlyQuerySet.retire`.

    """

    def remove(ids: list[int]) -> int:
        return model.objects.get_queryset().filter(pk__in=ids).retire(door=_DOOR)

    return remove


def _derived_rows_of_purgeable_runs(
    derived: type[models.Model],
    relation: str,
    *,
    cutoff: datetime,
) -> Callable[[], models.QuerySet[Any]]:
    """Return the selection of one derived table's rows whose run is purgeable.

    Args:
        derived: The derived table.
        relation: The name of its `policy_run` relation.
        cutoff: The retention cut-off.

    Returns:
        The selection, re-askable.

    """

    def select() -> models.QuerySet[Any]:
        return derived._default_manager.filter(  # noqa: SLF001 - Django's own accessor
            **{f"{relation}__in": purgeable_policy_runs(cutoff=cutoff).values("pk")},
        )

    return select


def _removable(entry: EvidenceTable, *, cutoff: datetime) -> Callable[[], models.QuerySet[Any]]:
    """Return the selection of one evidence table's removable rows.

    Args:
        entry: The roster entry.
        cutoff: The retention cut-off.

    Returns:
        The selection, re-askable.

    """

    def select() -> models.QuerySet[Any]:
        return removable_evidence(entry, cutoff=cutoff)

    return select


def _older(entry: EvidenceTable, *, cutoff: datetime) -> Callable[[], int]:
    """Return the count of one evidence table's rows older than the cut-off.

    Args:
        entry: The roster entry.
        cutoff: The retention cut-off.

    Returns:
        A callable answering the count when asked.

    """

    def count() -> int:
        return entry.model.objects.filter(**{f"{OBSERVED_AT_FIELD}__lt": cutoff}).count()

    return count


def _ended_collection_runs(*, cutoff: datetime) -> Callable[[], models.QuerySet[Any]]:
    """Return the selection of collection runs that ended before the cut-off.

    Args:
        cutoff: The retention cut-off.

    Returns:
        The selection, re-askable.

    """

    def select() -> models.QuerySet[Any]:
        return CollectionRun.objects.filter(**{f"{FINISHED_AT_FIELD}__lt": cutoff})

    return select


def _stale_unfinished_collection_runs(*, cutoff: datetime) -> Callable[[], models.QuerySet[Any]]:
    """Return the selection of collection runs that never ended and started before the cut-off.

    A killed worker's row: `choose_evidence_cutoff` bounds every policy run to
    before the earliest unfinished start, so one of these left in place would
    hold the cut-off choice at that instant for ever.

    Args:
        cutoff: The retention cut-off.

    Returns:
        The selection, re-askable.

    """

    def select() -> models.QuerySet[Any]:
        return CollectionRun.objects.unfinished().filter(**{f"{STARTED_AT_FIELD}__lt": cutoff})

    return select


def _abandon_stale_purges(*, clock: Clock) -> int:
    """Finalize every `prune_evidence` row an earlier purge left running as `failed`.

    Args:
        clock: The clock the finalizing instants come from.

    Returns:
        How many were closed.

    """
    stale = list(CollectionRun.objects.filter(collector=PRUNE_COLLECTOR).unfinished().order_by("pk"))
    for run in stale:
        abandon(run, clock=clock, detail=SUPERSEDED_DETAIL)
    if stale:
        logger.warning(STALE_PURGES_ABANDONED_EVENT, run_ids=[run.pk for run in stale], detail=SUPERSEDED_DETAIL)
    return len(stale)


def _recorded_cutoff(record: CollectionRun) -> datetime | None:
    """Return the cut-off a purge record's detail names, or `None` when it names none.

    Args:
        record: A `prune_evidence` row.

    Returns:
        The parsed instant, or `None`.

    """
    for part in record.detail.split():
        if part.startswith("cutoff="):
            try:
                return datetime.fromisoformat(part.removeprefix("cutoff="))
            except ValueError:
                return None
    return None


def _warn_if_cutoff_moved(*, cutoff: datetime, now: datetime) -> None:
    """Log a warning when the cut-off moved further than the time since the last purge.

    A nightly purge moves the cut-off by a night; an operator lowering the
    retention from ninety days to thirty moves it by sixty, and the sixty days
    of evidence that are about to go deserve a line in the log. Not refused --
    the setting is the declaration -- and not asked about: an admin process has
    nobody to ask.

    Args:
        cutoff: This purge's cut-off.
        now: This purge's `now`.

    """
    previous = CollectionRun.objects.filter(collector=PRUNE_COLLECTOR).finished().first()
    if previous is None:
        return
    previous_cutoff = _recorded_cutoff(previous)
    if previous_cutoff is None:
        return
    expected = previous_cutoff + (now - previous.started_at)
    drift = cutoff - expected
    if abs(drift) > CUTOFF_DRIFT_TOLERANCE:
        logger.warning(
            CUTOFF_MOVED_EVENT,
            previous_cutoff=previous_cutoff.isoformat(),
            previous_run_id=previous.pk,
            cutoff=cutoff.isoformat(),
            moved_by=str(cutoff - previous_cutoff),
            elapsed=str(now - previous.started_at),
        )


def purge_evidence(
    *,
    clock: Clock,
    retention_days: int,
    batch_size: int = DEFAULT_BATCH_SIZE,
    dry_run: bool = False,
) -> tuple[TablePurge, ...]:
    """Purge everything older than the retention, in the order the module docstring gives.

    Args:
        clock: The clock `now` and every record's instants come from (`CPM-AD-26`).
        retention_days: The retention, already validated.
        batch_size: The bound on one `DELETE`.
        dry_run: When true, every table is counted and recorded and nothing is
            removed.

    Returns:
        One result per table, in purge order: the derived tables, `policy_runs`,
        the evidence roster, `collection_runs`. A table a `DatabaseError`
        stopped carries `error`; the tables after it were still purged.

    Raises:
        ValueError: When `batch_size` is below one or above `MAXIMUM_BATCH_SIZE`
            -- an unbounded batch is the one statement this module exists not to
            issue -- or when the clock answers a naive instant (`CPM-AD-26`).

    """
    if batch_size < 1:
        message = f"batch_size must be at least 1, not {batch_size}: the purge never issues an unbounded DELETE."
        raise ValueError(message)
    if batch_size > MAXIMUM_BATCH_SIZE:
        message = (
            f"batch_size must be at most {MAXIMUM_BATCH_SIZE}, not {batch_size}: each key is one bound parameter "
            f"of the DELETE, and the bound keeps every batch inside what every backend accepts."
        )
        raise ValueError(message)
    now = clock.now()
    cutoff = retention_cutoff(now=now, days=retention_days)
    _abandon_stale_purges(clock=clock)
    _warn_if_cutoff_moved(cutoff=cutoff, now=now)
    results: list[TablePurge] = []

    def purge(table: str, legs: tuple[_Leg, ...], older: Callable[[], int] | None = None) -> None:
        plan = _Plan(table=table, legs=legs, older=older)
        results.append(_purge_table(plan, cutoff=cutoff, clock=clock, batch_size=batch_size, dry_run=dry_run))

    # 1. Derived rows of out-of-window, unpinned runs; then the runs.
    for derived, relation in derived_tables():
        purge(
            str(derived._meta.db_table),  # noqa: SLF001 - `_meta` is Django's own public-by-convention API
            (
                _Leg(
                    DELETED_LEG,
                    derived,
                    _derived_rows_of_purgeable_runs(derived, relation, cutoff=cutoff),
                    _delete(derived),
                ),
            ),
        )
    purge(
        str(PolicyRun._meta.db_table),  # noqa: SLF001 - as above
        (_Leg(DELETED_LEG, PolicyRun, lambda: purgeable_policy_runs(cutoff=cutoff), _delete(PolicyRun)),),
    )

    # 2. Evidence, under the floor rule, less what a surviving row cites.
    for entry in EVIDENCE_ROSTER:
        purge(
            entry.table,
            (_Leg(DELETED_LEG, entry.model, _removable(entry, cutoff=cutoff), _retire(entry.model)),),
            older=_older(entry, cutoff=cutoff),
        )

    # 3. The collection ledger: ended rows, and rows a killed worker left.
    purge(
        str(CollectionRun._meta.db_table),  # noqa: SLF001 - as above
        (
            _Leg("finished", CollectionRun, _ended_collection_runs(cutoff=cutoff), _delete(CollectionRun)),
            _Leg(
                "unfinished",
                CollectionRun,
                _stale_unfinished_collection_runs(cutoff=cutoff),
                _delete(CollectionRun),
            ),
        ),
    )
    return tuple(results)
