"""The one writer of current package health, and the only module that composes it.

`CPM-AD-11`: current package health is a Django-managed rollup table written by
the orchestrating policy run, and **only the rollup writer writes it**. That
sentence needs a place to point at, and this module is it -- one file, so "who
writes the rollup" has a one-file answer and so the write exemption in
`tests/unit/django_apps/test_derived_status_writability_audit.py` names one file
rather than a directory.

**Full-row replace, one `transaction.atomic()` per package (`CPM-AD-23`).** Every
column the rollup declares is written on every compose: the stamps from the run,
the confidence from the package, and each contributable column either from the
pass that owns it or from the field's own default. A *merge* would leave a column
holding a verdict from a policy run that is no longer named on the row, which is
the one thing a row carrying `policy_run` and `computed_at` promises cannot
happen. The transaction is around one package because `CPM-AD-23` fixes one
package as the atomic unit: a later package failing must never take an earlier
package's row away, and a run-wide transaction would do exactly that.

**The package set is read, never cached.** A `Package` created between two runs
gets a rollup row from the next compose without anything being told about it,
which is what "exactly one row per package, always" means in a system where
`CPM-AD-25` creates package rows from the inventory continuously.

**The gate is applied here, once, on the way in.** `core/confidence.py` is
`CPM-AD-4`'s single function; a pass never sees a confidence and never applies
it. That is what makes the rule hold for the seven passes nobody has written:
they cannot get it wrong, because they are not asked.

**Nothing here imports `core/policy.py`.** The contributions arrive as an
argument, prepared by the orchestration in `core/policy_run.py`. The dependency
runs the other way -- `core/policy.py` calls `contributable_columns()` here to
refuse a pass claiming a column the rollup does not declare -- and a cycle
between the registry and the writer would make either of them unimportable
alone, which is precisely what the ownership audit needs to be able to do.

**Two columns are contributable, and the table grows a column per pass.**
`epics.md` says the rollup "grows as passes are added"; `CPM-CURRENCY-S06` added
the first, `currency_status`, and `CPM-CURRENCY-S07` the second,
`feedstock_presence_status` -- each in the same story as the pass that produces
it. So `contributable_columns()` returns exactly those two, the contribution loop
below writes both on every row, and the mechanism is proved end to end by
`tests/integration/django_apps/test_currency_policy.py` and
`tests/integration/django_apps/test_feedstock_policy.py` as well as by the
fixture passes in `tests/passes.py` and the registry's refusals. The fixtures
still exist and still contribute nothing: each real column has a real owner, so a
fixture claiming one would be measuring that collision rather than the mechanism.

**On the `AD-` prefix.** A bare `AD-n` in this repository is an *inherited*
platform decision; a decision from this product's own architecture spine always
carries the `CPM-` prefix.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from typing import Any
from typing import Final
from typing import cast

import structlog
from django.db import models
from django.db import transaction

from conda_sentinel.core.confidence import gated_status
from conda_sentinel.core.confidence import require_known_confidence
from conda_sentinel.core.models import PackageHealth
from conda_sentinel.core.outcomes import OutcomeVocabularyError
from conda_sentinel.identity.models import Package

if TYPE_CHECKING:
    from collections.abc import Collection
    from collections.abc import Mapping
    from collections.abc import Sequence
    from datetime import datetime

    from conda_sentinel.core.clock import Clock
    from conda_sentinel.core.models import PolicyRun

__all__ = [
    "AFTER_RUN_COLUMNS",
    "ROLLUP_MODEL",
    "ROLLUP_WRITE_FAILED_EVENT",
    "ROLLUP_WRITTEN_EVENT",
    "STAMP_COLUMNS",
    "compose_rollup",
    "contributable_columns",
    "packages_for_rollup",
    "permitted_values",
]

logger = structlog.get_logger(__name__)

#: The model this module writes, named once so every reader -- the registry's
#: "a pass may not claim the rollup" refusal, the ownership audit, and this
#: writer -- is talking about the same table.
ROLLUP_MODEL: Final = PackageHealth

#: The columns the writer owns outright, which no pass may contribute.
#:
#: These are the row's *identity and provenance*: which package it is about,
#: which run computed it, when, at what evidence cut-off, and how certain the
#: identity was. A pass contributing one of them would be a pass rewriting the
#: statement about where the row came from, which is the single thing `CPM-AD-11`
#: puts in one writer's hands.
STAMP_COLUMNS: Final[frozenset[str]] = frozenset(
    {
        "confidence",
        "computed_at",
        "evidence_cutoff",
        "package",
        "policy_run",
        "policy_versions",
    },
)

#: The rollup columns an after-run step writes, which no pass may contribute.
#:
#: `CPM-OPERATE-S11`: `inventory_absent_since` and `inventory_last_listed` are
#: stamped by `collectors/absence.py`'s `mark_inventory_absence` after the
#: compose, because `core` may not read the inventory's evidence table. They are
#: neither stamps -- the compose does not know their values -- nor contributions
#: -- a pass returns statuses, and these are instants with no vocabulary -- so
#: they are a third kind: written `NULL` by the compose, so a row is a full
#: replacement, and filled in afterwards by the step that can read them.
#: `contributable_columns()` subtracts them exactly as it subtracts the stamps,
#: which is what keeps a pass from declaring one.
AFTER_RUN_COLUMNS: Final[frozenset[str]] = frozenset({"inventory_absent_since", "inventory_last_listed"})

#: The event one compose is logged under. Named so the case that asserts the log
#: and the code that emits it cannot drift --
#: `tests/integration/django_apps/test_rollup.py` reads it.
ROLLUP_WRITTEN_EVENT: Final[str] = "package_health_written"

#: The event a package whose rollup write failed is logged under. This is the
#: only operational record of *which* package did not compose: the run's ledger
#: row carries a count, and a count with no names sends an operator through the
#: whole inventory by hand. Asserted by
#: `tests/integration/django_apps/test_rollup.py`.
ROLLUP_WRITE_FAILED_EVENT: Final[str] = "package_health_write_failed"


def contributable_columns() -> frozenset[str]:
    """Return every rollup column a pass may declare a contribution to.

    Returns:
        The rollup's concrete field names, less the primary key, less
        `STAMP_COLUMNS` and less `AFTER_RUN_COLUMNS`. The first two were
        `currency_status` and `feedstock_presence_status`, which
        `CPM-CURRENCY-S06` and `CPM-CURRENCY-S07` added with the passes that own
        them. It is computed from the model's real fields rather than listed,
        which is why each became contributable without an edit here -- and why a
        column *removed* stops being contributable at the same moment, which a
        hand-written list would not manage.

    """
    meta = ROLLUP_MODEL._meta  # noqa: SLF001 - `_meta` is Django's own public-by-convention API
    return frozenset(
        field.name
        for field in meta.concrete_fields
        if field.name not in STAMP_COLUMNS and field.name not in AFTER_RUN_COLUMNS and not field.primary_key
    )


def packages_for_rollup() -> list[Package]:
    """Return every package the rollup must hold a row for.

    Called **once per run**, by the orchestration in `core/policy_run.py`, which
    then hands the same sequence to the pass phase and to `compose_rollup`. Two
    reads inside one run is the defect that shape exists to prevent: a `Package`
    inserted between them gets a rollup row no pass evaluated.

    Returns:
        Every `identity.Package`, ordered by primary key. Ordered so that two
        runs over the same inventory write in the same sequence, which is what
        makes a partial run's "these packages got a row and those did not"
        reproducible rather than dependent on whatever order the database
        returned. Read on every call, never cached across runs: a package created
        between two runs gets a row from the next compose without anything being
        told it exists.

    """
    return list(Package.objects.order_by("pk"))


def permitted_values(column: str) -> frozenset[str]:
    """Return the values a contributed rollup column may hold.

    `CPM-AD-5` requires every derived status to be a `CharField(choices=...)` and
    fixes the vocabulary it is drawn from, and **Django validates neither on
    `save()`**: `choices` is a form and `full_clean()` rule, so a pass returning
    `"clean"` where the column offers `ok` writes `"clean"` into the database and
    every read surface then emits a value `CPM-AD-24` says is emitted verbatim
    and that no consumer recognises. This is what the orchestration checks a
    contribution against before it reaches the writer.

    The column's *own* declared choices rather than `OutcomeState`'s five values,
    because a per-status type composed by `core.outcomes.outcome_type` adds
    determinate verdicts of its own -- a licence outcome's `violation` is a
    perfectly legitimate value that a check against the base vocabulary would
    refuse.

    Args:
        column: A contributable rollup column.

    Returns:
        Every value the column's declared choices offer, as stored strings.

    Raises:
        OutcomeVocabularyError: When the column declares no choices at all.
            `CPM-AD-5` requires them, and a column without them is one no
            contribution can be checked against -- so the refusal is on the
            declaration rather than on whatever value happened to arrive.

    """
    field = cast("models.Field[Any, Any]", ROLLUP_MODEL._meta.get_field(column))  # noqa: SLF001 - `_meta` is Django's own public-by-convention API
    choices = field.choices
    if not choices:
        message = (
            f"the rollup column {column!r} declares no choices, so no contribution to it can be checked. "
            f"CPM-AD-5 requires a CharField(choices=...) per derived status precisely so the vocabulary is "
            f"declared where the column is."
        )
        raise OutcomeVocabularyError(message)
    return frozenset(str(value) for value, _label in choices)


def compose_rollup(  # noqa: PLR0913 - one keyword per stamp the row carries; a bundle would hide them
    *,
    policy_run: PolicyRun,
    evidence_cutoff: datetime,
    packages: Sequence[Package],
    contributions: Mapping[int, Mapping[str, str]],
    policy_versions: Mapping[str, str],
    clock: Clock,
    skipped: Collection[int] = (),
) -> tuple[int, list[int]]:
    """Write one rollup row per package, replacing whatever was there.

    **The package set is handed in rather than read here, and that changed for a
    reason worth recording.** This function read it itself, and so did the
    orchestration for the pass phase -- two reads of a table `CPM-AD-25` inserts
    into continuously. A `Package` created between them got a rollup row no pass
    had evaluated, stamped with a run that never looked at it, and the run's own
    "n of m packages failed" then counted against a different m. One read, passed
    through, is what makes the two phases talk about the same inventory.

    Args:
        policy_run: The run this compose belongs to, stamped on every row.
        evidence_cutoff: The instant the run read evidence as of (`CPM-AD-21`),
            copied onto every row.
        packages: The inventory this run is about, read once by the
            orchestration.
        contributions: Package primary key to the columns the registered passes
            produced for it. A package absent from the mapping contributed
            nothing, which is not an error: it is what every package looks like
            while no pass is registered.
        policy_versions: The per-domain version map (`CPM-AD-11`), stamped on
            every row of this run.
        clock: The clock `computed_at` is read from (`CPM-AD-26`). Read once, so
            every row of one run carries the same instant -- a run whose rows
            were stamped microseconds apart would make "the rollup as of T" a
            question with no single answer.
        skipped: The packages this run must not write. A package whose pass
            raised is here: `CPM-AD-23` commits the packages that worked, and the
            one that did not keeps the row it had rather than being overwritten
            with a health computed from a pass that never finished.

    Returns:
        How many rows were written, and the primary keys of the packages whose
        write failed. The second is not the same set as `skipped`: those were
        never attempted, and these were attempted and did not land.

    """
    computed_at = clock.now()
    excluded = frozenset(skipped)
    written = 0
    failed: list[int] = []
    for package in packages:
        if package.pk in excluded:
            continue
        try:
            # `CPM-AD-23`: one package, one transaction. Nested inside the run
            # recorder and never around it -- `core/ledger.py` says the ordering
            # guarantee depends on the `running` row committing first, and
            # `tests/unit/django_apps/test_collector_base_audit.py` sweeps for the
            # inversion in both directions.
            with transaction.atomic():
                ROLLUP_MODEL.objects.update_or_create(
                    package=package,
                    defaults=_replacement(
                        package,
                        policy_run=policy_run,
                        evidence_cutoff=evidence_cutoff,
                        computed_at=computed_at,
                        policy_versions=policy_versions,
                        contributed=contributions.get(package.pk, {}),
                    ),
                )
        # Contained per package for exactly the reason the pass phase is
        # (`CPM-AD-23`), and it was not: one row that will not compose -- a
        # confidence value from outside the vocabulary, a constraint the database
        # refuses -- took every package after it down with it and finalized the
        # run `failed`, which is the opposite of "degrades to stale evidence,
        # never to a clean result" (`CPM-NFR-3`). Never swallowed: the package and
        # the traceback are logged, and the caller is handed the key so the run's
        # ending says so.
        except Exception:
            logger.exception(ROLLUP_WRITE_FAILED_EVENT, policy_run_pk=policy_run.pk, package_pk=package.pk)
            failed.append(package.pk)
            continue
        written += 1
    logger.info(
        ROLLUP_WRITTEN_EVENT,
        policy_run_pk=policy_run.pk,
        rows=written,
        skipped=len(excluded),
        failed=len(failed),
    )
    return written, failed


def _replacement(  # noqa: PLR0913 - one keyword per stamp the row carries; a bundle would hide them
    package: Package,
    *,
    policy_run: PolicyRun,
    evidence_cutoff: datetime,
    computed_at: datetime,
    policy_versions: Mapping[str, str],
    contributed: Mapping[str, str],
) -> dict[str, object]:
    """Build the complete row for one package.

    Every column the rollup declares appears in the result, which is what makes
    the write a replace rather than a merge. A contributable column nobody
    contributed is written as the field's own default, so a pass that has been
    withdrawn takes its verdict with it instead of leaving a value on a row that
    no longer names the run which produced it.

    **The default goes through the gate too, and that is the bug this sentence
    exists to keep fixed.** A field default is a *claim about the package* just as
    much as a pass's verdict is: `currency_status` declared `default=CURRENT`
    would make every package no pass evaluated read as up to date -- the exact
    claim `CPM-FR-5` forbids about a package whose identity was never established
    -- through the one path no pass touches and no contribution case would
    notice. It declares `default=unknown` instead, and that default reaches this
    line on every run for every package a registered pass did not contribute for.

    Args:
        package: The package the row is about. Its `confidence` is recorded on the
            row and gates every value written into a contributable column
            (`CPM-AD-4`), contributed or defaulted.
        policy_run: The run being stamped.
        evidence_cutoff: The run's cut-off, copied onto the row.
        computed_at: The instant every row of this run carries.
        policy_versions: The per-domain version map.
        contributed: The columns the registered passes produced for this package.

    Returns:
        The column values, ready to be handed to the writer as a full row. The
        package itself is deliberately absent: it is the key the row is matched
        on, not part of what is replaced.

    """
    # Checked before anything is gated with it: `confidence` is a
    # `CharField(choices=...)` and Django validates neither on `save()`, so a
    # value from outside the vocabulary would be written straight into the column
    # a read surface reads the row's identity provenance from -- and would then
    # decide, wrongly, whether `currency_status` was gated.
    confidence = require_known_confidence(package.confidence)
    row: dict[str, object] = {
        "policy_run": policy_run,
        "computed_at": computed_at,
        "evidence_cutoff": evidence_cutoff,
        "confidence": confidence,
        "policy_versions": dict(policy_versions),
    }
    # The after-run columns are written `NULL` here, so a compose is a full
    # replacement of the row rather than a merge that leaves last run's absence
    # behind; `collectors/absence.py` fills them in after every compose.
    row.update(dict.fromkeys(AFTER_RUN_COLUMNS))
    meta = ROLLUP_MODEL._meta  # noqa: SLF001 - `_meta` is Django's own public-by-convention API
    for column in sorted(contributable_columns()):
        verdict = contributed.get(column)
        if verdict is None:
            # `get_field` is typed as returning a relation or a descriptor as
            # well as a field, and only a field carries `get_default`. The cast
            # names the one this can be: `contributable_columns()` is built from
            # `concrete_fields`, so every column reaching here is one.
            field = cast("models.Field[Any, Any]", meta.get_field(column))
            verdict = str(field.get_default())
        row[column] = gated_status(verdict, confidence=confidence)
    return row
