"""This application's Celery tasks, and inventory ingestion's collector and adapter.

**Every task this application registers is declared here and nowhere else.**
Celery's autodiscovery imports each installed application's `tasks` module and no
other (`config/celery_app.py`), so a `@shared_task` in a sibling module is
registered by whatever happens to import it -- which is the suite, and not a
worker. `CPM-CURRENCY-S01`'s collector therefore lives in
`collectors/source_release.py` and `CPM-CURRENCY-S02`'s in
`collectors/pypi_release.py`, while their tasks live at the foot of this file: a
collector is code a reader wants beside the source it reads, and a task is a
line that has to be *here* to run at all.

`CPM-FR-42` acquires the package inventory from a declared source, and
`CPM-AD-25` fixes how: as an **observation**, through the shared collector base,
on the `collect` queue, written to an append-only log. This module is that
collector. It is the first concrete collector in the repository and the only one
that introduces a package -- `CPM-FR-7` through `CPM-FR-10` observe surfaces
*about packages the inventory already names*.

**The collector never writes the package table**, and that is the sentence this
module is arranged around. A record naming a package with no row calls
`identity`'s resolution service, which is the only creator of a package row
(`CPM-AD-14`, `CPM-AD-25`). `tests/unit/django_apps/test_inventory_ingestion.py`
sweeps this module's own source for a `Package` write, because the rule is only
worth what a reviewer who has not read this paragraph is stopped by.

**The atomic unit is one package** (`CPM-AD-23`). The shell and its snapshot
commit together, in a `transaction.atomic()` nested inside the base's run
recorder and never around it, so a later package's failure never rolls back an
earlier package's rows and the run finalizes `partial` (`CPM-FR-15`).

**The source is reached through the base's transport seam and nothing else**
(`CPM-AD-29`). An inventory source adapter *is* a `Transport`, so this collector
carries no branch on which source is active and the seam needs no second
protocol. `CPM-IDENTITY-S07` declares the watchlist adapter, its columns, its
refusals and the rule that selects a file by locality; what is here is the one
declared point an adapter is bound to, and the refusal when none is.

**Absence is a row, not a deletion.** `CPM-AD-25`: "a package present in an
earlier run and absent from a later one is recorded as absent with a timestamp.
No package row is ever deleted." So a sweep that no longer sees a key it has seen
before writes a `not_found` snapshot carrying *this* run's `observed_at`, and the
package keeps its row and its place in the rollup. An absence is written on every
run it is still true for, which is why this collector declares `NO_WINDOW`:
suppressing a run would suppress the absence observations too, and absence is the
signal that decays.

**No run partially ingests a malformed source** (`CPM-FR-42`, inherited `CG-3`).
The whole document is decoded into records before the first row is written, so a
missing required signal, a non-numeric count, a repeated source package key or a
field the record contract does not define fails the run with nothing written --
rather than leaving half an inventory behind and an operator to work out which
half.

**There is no per-package path here, and the three hooks say so.** `source_for`,
`translate` and `sentinel_evidence` are the base's per-package contract: one
locator per package, one payload about one package, one sentinel row about one
package. Inventory ingestion reads one document naming many packages and has none
of those things, so each refuses rather than inventing an answer. A `source_for`
returning the document's locator would make `collect(package_id=...)` re-read the
whole inventory to observe one package, which is a defect that would look like it
worked.

**On the `AD-` prefix.** A bare `AD-n` in this repository is an *inherited*
platform decision; a decision from this product's own architecture spine always
carries the `CPM-` prefix.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from datetime import timedelta
from types import MappingProxyType
from typing import TYPE_CHECKING
from typing import ClassVar
from typing import Final
from typing import NoReturn

from celery import shared_task
from django.db import DatabaseError
from django.db import transaction

from conda_sentinel.collectors.advisories import advisory_source
from conda_sentinel.collectors.conda_package import CondaPackageCollector
from conda_sentinel.collectors.feedstock import FeedstockCollector
from conda_sentinel.collectors.kev import COLLECTOR_NAME as KEV_COLLECTOR_NAME
from conda_sentinel.collectors.kev import KevCollector
from conda_sentinel.collectors.kev import declared_kev_source
from conda_sentinel.collectors.kev import kev_source
from conda_sentinel.collectors.license import LicenseCollector
from conda_sentinel.collectors.models import InventorySnapshot
from conda_sentinel.collectors.py314_verification import Py314VerificationCollector
from conda_sentinel.collectors.pypi_release import PyPIReleaseCollector
from conda_sentinel.collectors.python_readiness import PythonReadinessCollector
from conda_sentinel.collectors.resolve_identity import IdentityResolutionCollector
from conda_sentinel.collectors.source_release import SourceReleaseCollector
from conda_sentinel.collectors.sweep import SWEEP_TASK_NAME
from conda_sentinel.collectors.sweep import dispatch
from conda_sentinel.collectors.verification import verification_backend
from conda_sentinel.collectors.vulnerability import VulnerabilityCollector
from conda_sentinel.core.clock import SystemClock
from conda_sentinel.core.collection import NO_CACHE
from conda_sentinel.core.collection import NO_WINDOW
from conda_sentinel.core.collection import Collector
from conda_sentinel.core.collection import CollectorConfigurationError
from conda_sentinel.core.collection import SweepOutcome
from conda_sentinel.core.ledger import collection_run
from conda_sentinel.core.ledger import current_trace_id
from conda_sentinel.core.outcomes import OutcomeState
from conda_sentinel.core.rate_limit import RateLimit
from conda_sentinel.core.transport import MAX_TIMEOUT
from conda_sentinel.core.transport import Transport
from conda_sentinel.identity.services import ASSOCIATOR_KEY_LENGTH
from conda_sentinel.identity.services import CANONICAL_NAME_LENGTH
from conda_sentinel.identity.services import ResolutionError
from conda_sentinel.identity.services import resolve_package_shell

if TYPE_CHECKING:
    from collections.abc import Mapping
    from collections.abc import Sequence
    from datetime import datetime

    from conda_sentinel.core.models import AppendOnlyModel
    from conda_sentinel.core.transport import Payload

__all__ = [
    "ABSENT_DETAIL",
    "COLLECTOR_NAME",
    "COLLECT_CONDA_PACKAGE_TASK_NAME",
    "COLLECT_FEEDSTOCK_TASK_NAME",
    "COLLECT_KEV_TASK_NAME",
    "COLLECT_LICENSE_TASK_NAME",
    "COLLECT_PYPI_RELEASE_TASK_NAME",
    "COLLECT_PYTHON_READINESS_TASK_NAME",
    "COLLECT_RESOLVE_IDENTITY_TASK_NAME",
    "COLLECT_SOURCE_RELEASE_TASK_NAME",
    "COLLECT_VULNERABILITY_TASK_NAME",
    "INGEST_TASK_NAME",
    "INVENTORY_SOURCE",
    "MAX_COUNT",
    "OPTIONAL_SIGNALS",
    "PACKAGE_NAME",
    "REQUIRED_SIGNALS",
    "SOURCE_PACKAGE_KEY",
    "SWEEP_TASK_NAME",
    "VERIFY_PY314_TASK_NAME",
    "InventoryAdapterError",
    "InventoryIngestionCollector",
    "InventoryRecord",
    "InventoryRecordError",
    "collect_conda_package",
    "collect_feedstock",
    "collect_kev",
    "collect_license",
    "collect_pypi_release",
    "collect_python_readiness",
    "collect_source_release",
    "collect_sweep",
    "collect_vulnerability",
    "declare_inventory_adapter",
    "declared_inventory_adapter",
    "ingest_inventory",
    "inventory_adapter",
    "records_in",
    "resolve_identity",
    "verify_py314_build",
    "withdraw_inventory_adapter",
]

#: This module declares no logger and emits no events of its own, deliberately.
#: `core/collection.py` owns the seven a collection can emit -- skipped, refused,
#: failed, partial, not-modified, not-remembered, not-applicable -- and fixes the
#: keys every one of them carries so a log query does not have to know which path
#: produced it.
#: An eighth emitted from here would be a second schema for the same run, and a
#: "sweep completed" line would say what the run ledger row already records
#: durably.

#: What this collector is called, on its ledger rows and in its cache keys. It is
#: also what the shells it triggers record as their `identity_source`
#: (`CPM-FR-2`), which is the same name a run is traced to (`CPM-FR-39`).
COLLECTOR_NAME: Final[str] = "inventory"

#: The task's declared name. `cpm.collect.` is what `core/queues.py`'s derived
#: route table sends to the `collect` queue, and the workload class lives in the
#: name rather than in the module for the reason that module records: this
#: package will hold `verify` collectors too, so a route keyed on module path
#: could not tell a compute-backed build from a file read (`R-11`).
INGEST_TASK_NAME: Final[str] = "cpm.collect.inventory"

#: The upstream-release collection task's declared name, on the same terms
#: (`CPM-CURRENCY-S01`, `CPM-FR-7`). The `cpm.collect.` namespace is what routes
#: it to the `collect` queue with no edit to `core/queues.py`; it names the
#: *collector* rather than the module, because `CPM-EP-PY314` will put `verify`
#: work in this same package and a route keyed on module path could not tell a
#: compute-backed build from an HTTP read (`R-11`).
COLLECT_SOURCE_RELEASE_TASK_NAME: Final[str] = "cpm.collect.source_release"

#: The PyPI-release collection task's declared name, on the same terms
#: (`CPM-CURRENCY-S02`, `CPM-FR-8`): the `cpm.collect.` namespace routes it, and
#: the name is the collector's.
COLLECT_PYPI_RELEASE_TASK_NAME: Final[str] = "cpm.collect.pypi_release"

#: The feedstock-collection task's declared name, on the same terms
#: (`CPM-CURRENCY-S03`, `CPM-FR-9`): the `cpm.collect.` namespace routes it, and
#: the name is the collector's.
COLLECT_FEEDSTOCK_TASK_NAME: Final[str] = "cpm.collect.feedstock"

#: The published-conda-package collection task's declared name, on the same
#: terms (`CPM-CURRENCY-S04`, `CPM-FR-10`): the `cpm.collect.` namespace routes
#: it, and the name is the collector's.
COLLECT_CONDA_PACKAGE_TASK_NAME: Final[str] = "cpm.collect.conda_package"

#: The vulnerability-collection task's declared name, on the same terms
#: (`CPM-SECURITY-S01`, `CPM-FR-11`): the `cpm.collect.` namespace routes it to
#: the `collect` queue, and the name is the collector's.
COLLECT_VULNERABILITY_TASK_NAME: Final[str] = "cpm.collect.vulnerability"

#: The KEV-cross-reference task's declared name, on the same terms
#: (`CPM-SECURITY-S02`, `CPM-FR-12`): the `cpm.collect.` namespace routes it to
#: the `collect` queue, and the name is the collector's.
COLLECT_KEV_TASK_NAME: Final[str] = "cpm.collect.kev"

#: The licence-collection task's declared name, on the same terms
#: (`CPM-SECURITY-S03`, `CPM-FR-13`): the `cpm.collect.` namespace routes it to
#: the `collect` queue, and the name is the collector's.
COLLECT_LICENSE_TASK_NAME: Final[str] = "cpm.collect.license"

#: The static Python-readiness task's declared name, on the same terms
#: (`CPM-PY314-S01`, `CPM-FR-14`): the `cpm.collect.` namespace routes it to the
#: `collect` queue, and the name is the collector's.
#:
#: **`cpm.collect.` and emphatically not `cpm.verify.`**, which is the one place
#: this constant carries a decision rather than a convention. `CPM-FR-14`'s other
#: half is a Python 3.14 *build*, which `CPM-AD-20` puts on the `verify` queue
#: precisely so a five-minute compute job cannot starve the daily sweeps (`R-11`).
#: This task makes one HTTP request and reads a specifier, so it is collection
#: work and belongs where collection work goes; `CPM-PY314-S02` declares the
#: `cpm.verify.` name.
COLLECT_PYTHON_READINESS_TASK_NAME: Final[str] = "cpm.collect.python_readiness"

#: The identity-resolution task's declared name, on the same terms
#: (`CPM-IDENTITY-S08`, `CPM-FR-1`): the `cpm.collect.` namespace routes it to
#: the `collect` queue, and the name is the collector's. It reads two public
#: documents and hands what it found to `identity`'s recorder, which is
#: collection work whatever it writes afterwards -- and `CPM-AD-20` puts every
#: automated resolver that calls out on this queue through the shared base.
COLLECT_RESOLVE_IDENTITY_TASK_NAME: Final[str] = "cpm.collect.resolve_identity"

#: The Python 3.14 verification task's declared name, and the first task this
#: module declares outside the `cpm.collect.` namespace (`CPM-PY314-S02`,
#: `CPM-FR-14`). `core/tasks.py` already declares the one `cpm.policy.` name, so
#: what is new here is a third namespace reaching a third queue rather than a
#: second namespace existing at all.
#:
#: **`cpm.verify.` is what routes it, and the name is `core/queues.py`'s own worked
#: example** -- spelled there, verbatim, before this task existed, because that
#: module's whole argument is that a task's workload class lives in its declared
#: name rather than in its module: `CPM-EP-PY314`'s verification and
#: `CPM-EP-CURRENCY`'s collection land in one package, so a route keyed on module
#: path could not tell a compute-backed build from an HTTP read, and getting that
#: wrong *is* `R-11`. Declaring this name is the whole of what puts verification on
#: the `verify` queue: there is no route to add and no setting to edit.
#:
#: **`py314_build` and not the collector's own name**, which is the one place this
#: constant departs from the nine above. Those are derived --
#: `collectors/sweep.py` builds `cpm.collect.<collector name>` to enqueue a swept
#: collection -- so their last segment *must* be the collector's name. Nothing
#: sweeps this collector (`CPM-PY314-S02` AC 3), so nothing derives this name, and
#: it is free to say what the task does. `tests/unit/django_apps/
#: test_py314_verification.py` pins the string against `core/queues.py`'s example so
#: the two cannot drift.
VERIFY_PY314_TASK_NAME: Final[str] = "cpm.verify.py314_build"

#: `SWEEP_TASK_NAME` is the one task name in this module that is **not** declared
#: here. `CPM-CURRENCY-S05`'s dispatch task names no collector, because it takes
#: one, and `config/startup/stage_two.py` reconciles the beat schedule against
#: the same string -- so it is declared in `collectors/sweep.py` beside the
#: dispatch it fires and imported above, and it is re-exported in `__all__` so a
#: reader looking for this application's task names finds all nine here -- one
#: per registered collector, plus the dispatch. The
#: task itself must still be *declared* in this module: Celery's autodiscovery
#: imports each application's `tasks` module and no other.

#: The locator handed to the adapter, and it is deliberately opaque.
#:
#: `CPM-AD-29` makes an inventory source a transport substitution, so the adapter
#: already knows which file or endpoint it reads -- the locator's job here is to
#: name the *run* in the ledger's `detail` and in every log line, not to address a
#: resource. A path spelled here would be this module choosing the inventory
#: source, which is `CPM-IDENTITY-S07`'s and not this story's.
INVENTORY_SOURCE: Final[str] = "inventory://declared-adapter"

#: The record field naming the package, and the two signals every record carries.
#: `CPM-FR-42` and PRD Open Question 3b: together the counts are the "internal
#: usage breadth" `CPM-FR-4` ranks by, so a record without both is a record that
#: cannot be ranked and is refused rather than stored half-observed.
SOURCE_PACKAGE_KEY: Final[str] = "source_package_key"
REQUIRED_SIGNALS: Final[tuple[str, ...]] = ("internal_component_count", "internal_lob_count")

#: The record field naming the *package*, as against the key the source filed it
#: under, and every record carries one.
#:
#: The two are different facts and were the same value until `CPM-IDENTITY-S07`.
#: `source_package_key` is what the inventory calls this entry -- stable, the
#: thing a later sweep matches on, and never corrected -- while the name is what
#: the package is called, which `CPM-IDENTITY-S02` corrects when it establishes a
#: real identity. Writing one value into both `canonical_name` and
#: `associator_key` made the correction invisible: a lookup keyed on the
#: corrected name no longer matches the key the source still sends, and the next
#: sweep creates a second shell for the same package. Separating them does not by
#: itself close that trap -- resolution still has to match on
#: `(identity_source, associator_key)` -- but it stops the two values being
#: indistinguishable, which is what hid it.
PACKAGE_NAME: Final[str] = "package_name"

#: The signals a record may omit. Open Question 3b: they are score inputs for
#: `CPM-FR-20`, whose function is itself undecided, and no hand-authored source
#: can state them credibly -- so missing is an ordinary state and is stored as
#: NULL, which stays distinguishable from a stored `0`.
OPTIONAL_SIGNALS: Final[tuple[str, ...]] = ("apps", "platforms", "downloads", "versions")

#: Every field a record may carry, and nothing else is accepted. A record naming
#: a repository URL, a purl or a confidence is refused rather than having the
#: field ignored, because ingestion never asserts a mapping (`CPM-FR-42`,
#: `CPM-FR-1`) and a silently dropped column is a source that believes it is
#: supplying one.
RECORD_FIELDS: Final[frozenset[str]] = frozenset(
    {SOURCE_PACKAGE_KEY, PACKAGE_NAME, *REQUIRED_SIGNALS, *OPTIONAL_SIGNALS},
)

#: What an absence row says in its own words. Named so the row the sweep writes
#: and the case that reads it back cannot drift.
ABSENT_DETAIL: Final[str] = "the inventory source no longer lists this package"

#: The largest count a usage signal may carry.
#:
#: `PositiveIntegerField`'s ceiling, which is a signed 32-bit maximum on every
#: backend Django supports. Declared rather than left to the column because the
#: column enforces it on PostgreSQL and not on SQLite;
#: `tests/unit/django_apps/test_inventory_ingestion.py` reconciles this number
#: against the field's own validator, so a widened column is a failing test
#: rather than a silently unenforced bound.
MAX_COUNT: Final[int] = 2_147_483_647

#: How hard this collector may push its source. The base offers no opt-out, and a
#: generously declared allowance is the honest form for a source that is not rate
#: limited at all -- a fabricated small number would be a limit nobody measured.
INVENTORY_RATE_LIMIT: Final[RateLimit] = RateLimit(calls=60, per=timedelta(seconds=60))

#: How long this collector's evidence may be read as current (`CPM-AD-28`).
#:
#: Two days, and it is derived rather than picked. PRD Open Question 7 was
#: resolved on 2026-09-05 and fixes the rule:
#:
#:     freshness_target = cadence x (1 + tolerated_missed_runs) + one sweep duration
#:
#: Inventory ingestion's cadence is daily (`CPM-NFR-2`) and its signal class
#: tolerates one missed run, which gives two days.
#:
#: **The target is strictly greater than the cadence, and that is the rule rather
#: than the arithmetic.** `core/freshness.py` reports stale when
#: `observed_at < now - target`, so a target *equal* to the cadence makes evidence
#: go stale at exactly the moment the next run is due -- any delay in scheduling
#: and the whole inventory reads stale without one collection having failed. That
#: is why this is two days and not the one a "daily sweep, daily target" reading
#: would give.
#:
#: What is still owed is Open Question 7b's measurement: a target must also exceed
#: one sweep's wall-clock duration, and no sweep has run at `CPM-NFR-1`'s ten
#: thousand packages yet. This value assumes a sweep finishes well inside its
#: cadence, which `CPM-EP-CURRENCY` is where it is confirmed.
INVENTORY_FRESHNESS_TARGET: Final[timedelta] = timedelta(days=2)

#: The declared inventory source adapter, by the one slot there is.
#:
#: A module-level mapping rather than a rebound global, for the reason
#: `core/registry.py`'s is one: ruff `PLW0603` forbids the `global` statement,
#: and a `from ... import` of a rebound name would bind a copy that never
#: observes a later write.
_DECLARED: Final[dict[str, Transport]] = {}

#: The key `_DECLARED` holds the adapter under. One slot, because `CPM-AD-29`
#: says ingestion "reads exactly one inventory source adapter": two would make
#: "which source is this component's inventory" a question answered by import
#: order.
_ADAPTER_SLOT: Final[str] = COLLECTOR_NAME


class InventoryAdapterError(ValueError):
    """No usable inventory source adapter is declared, or two are.

    A `ValueError` subclass on the same terms as `core/registry.py`'s
    `CollectorRegistryError`, which it is deliberately shaped after: adapters are
    declared, never discovered (inherited `AD-8`), and a duplicate is refused
    rather than overwritten because the second one silently replacing the first
    is how a deployed component comes to ingest a development subset -- and every
    package outside that subset is then recorded absent, permanently, in a log
    nothing may update (`CPM-AD-29`).
    """


class InventoryRecordError(ValueError):
    """A source document could not be read as inventory records.

    Raised before any row is written, which is the whole of `CPM-FR-42`'s "no run
    partially ingests a malformed source". The alternative -- skipping the bad
    record and ingesting the rest -- writes an absence observation for every
    package the truncated document failed to name, into an append-only log that
    nothing may correct.
    """


@dataclass(frozen=True, slots=True)
class InventoryRecord:
    """One package as the inventory source describes it.

    The contract `CPM-AD-29` calls "yield records, or fail", as data. It is
    deliberately not the watchlist's shape: `CPM-IDENTITY-S07` owns the file, its
    columns and its delimiter, and an adapter's job is to turn one into these.
    Frozen and slotted, so a record cannot be edited between being read and being
    written.

    Attributes:
        source_package_key: The key the source used for this package. It becomes
            the shell's `associator_key`: the stable thing a later sweep, and a
            later resolution, matches this package back on.
        package_name: What the source calls the package. It becomes the shell's
            `canonical_name`, which is the one *correctable* name --
            `CPM-IDENTITY-S02` rewrites it when it establishes a real identity,
            and the key above is what survives that rewrite.
        internal_component_count: How many internal components use the package.
        internal_lob_count: How many internal lines of business use it.
        apps: How many applications name it, or `None` where the source did not
            say. `None` is *missing*, and stays distinguishable from `0`.
        platforms: How many platforms it is used on, or `None`.
        downloads: How many internal downloads it has, or `None`.
        versions: How many versions of it are in use, or `None`.

    """

    source_package_key: str
    package_name: str
    internal_component_count: int
    internal_lob_count: int
    apps: int | None = None
    platforms: int | None = None
    downloads: int | None = None
    versions: int | None = None


def declare_inventory_adapter(adapter: Transport) -> Transport:
    """Adopt one inventory source adapter for this process.

    Args:
        adapter: The `Transport` the ingestion task reads its document through.

    Returns:
        The adapter, unchanged, so a caller can bind it in one statement.

    Raises:
        InventoryAdapterError: When the object is not a `Transport`, or when one
            is already declared. `Transport` is `runtime_checkable`, so this
            check sees method *names* only -- which is the same bound
            `core/transport.py` records, and the reason a case pins what `fetch`
            returns as well as that it exists.

    """
    if not isinstance(adapter, Transport):
        message = (
            f"{adapter!r} is not a Transport and cannot be the inventory source adapter. An adapter is a "
            f"transport substitution at the collector base's seam (CPM-AD-29), so it answers fetch() with a "
            f"recorded Payload and nothing else is required of it."
        )
        raise InventoryAdapterError(message)
    existing = _DECLARED.get(_ADAPTER_SLOT)
    if existing is not None:
        message = (
            f"{type(adapter).__name__} cannot be declared: {type(existing).__name__} is already this "
            f"component's inventory source adapter. Ingestion reads exactly one (CPM-AD-29); a second one "
            f"silently replacing the first is how a deployed component ingests a development subset and "
            f"records every package outside it as absent."
        )
        raise InventoryAdapterError(message)
    _DECLARED[_ADAPTER_SLOT] = adapter
    return adapter


def withdraw_inventory_adapter() -> None:
    """Withdraw the declared inventory source adapter.

    Symmetric with `declare_inventory_adapter` rather than a test hook bolted on,
    for the reason `core/registry.py`'s `unregister` is: the declaration is
    process-global, so a case that could only add to it could never measure the
    refusal when nothing is declared, and one that left an adapter behind would
    change what every later case ingests.

    Raises:
        InventoryAdapterError: When nothing is declared. Refused rather than
            ignored, because a silent no-op turns a mistaken withdrawal into a
            declaration that stays live and a caller that believes it does not.

    """
    if _ADAPTER_SLOT not in _DECLARED:
        message = (
            "no inventory source adapter is declared, so there is nothing to withdraw. "
            "Declare one with declare_inventory_adapter (CPM-AD-29)."
        )
        raise InventoryAdapterError(message)
    del _DECLARED[_ADAPTER_SLOT]


def declared_inventory_adapter() -> Transport | None:
    """Return the declared inventory source adapter, or `None` when there is none.

    The read `inventory_adapter` below refuses on, without the refusal. It exists
    for one caller shape and is shaped after `core/registry.py`'s `registrations`,
    which `CollectorsConfig.ready()` already reads for the same reason: a boot
    hook has to be able to *ask* whether the declaration it is about to make has
    already been made, because `AppConfig.ready` is Django's to call and a second
    `django.setup()` in one process calls it again. Asking through
    `inventory_adapter` would mean catching the refusal as control flow, and
    declaring unconditionally would abort boot over a declaration that had
    already succeeded.

    Returns:
        The adapter this component reads its inventory through, or `None` when
        nothing is declared. `None` is an answer here and never a default: a
        caller that wants the run refused calls `inventory_adapter`.

    """
    return _DECLARED.get(_ADAPTER_SLOT)


def inventory_adapter() -> Transport:
    """Return the declared inventory source adapter.

    Returns:
        The adapter this component reads its inventory through.

    Raises:
        InventoryAdapterError: When none is declared. The run is refused here,
            before the recorder opens and therefore before any row exists --
            which is the matrix row that says ingestion with no source leaves no
            trace of a run that could not have observed anything.

    """
    adapter = _DECLARED.get(_ADAPTER_SLOT)
    if adapter is None:
        message = (
            "no inventory source adapter is declared, so there is nothing to ingest. Adapters are declared "
            "and never discovered (AD-8, CPM-AD-29): CPM-IDENTITY-S07 declares the watchlist adapter, and "
            "until one is bound the run is refused rather than recorded as an empty inventory."
        )
        raise InventoryAdapterError(message)
    return adapter


def records_in(payload: Payload) -> tuple[InventoryRecord, ...]:
    """Decode a whole source document into records, or refuse the document.

    The record contract `CPM-AD-29` names, applied to what the adapter recorded.
    The document is decoded in full before this returns, which is what makes
    "no run partially ingests a malformed source" true: the caller has either
    every record or none, and never a prefix.

    Args:
        payload: What the adapter said, recorded. Its `body` is a JSON array of
            record objects -- a neutral encoding chosen here rather than the
            source's own, because the file format belongs to the adapter
            (`CPM-IDENTITY-S07`) and the collector must be unchanged by which one
            is active.

    Returns:
        One record per entry, in the document's own order.

    Raises:
        InventoryRecordError: When the body is not a JSON array of objects, when
            a record omits a required signal or carries one that is not a
            non-negative integer, when it carries a field the contract does not
            define, or when two records share a source package key.

    """
    try:
        document = json.loads(payload.body)
    except json.JSONDecodeError as unreadable:
        message = (
            f"{payload.source} did not yield a readable inventory document: "
            f"{type(unreadable).__name__}: {unreadable}. The run is refused rather than treated as an empty "
            f"inventory, which would record every package it should have named as absent (CPM-FR-42)."
        )
        raise InventoryRecordError(message) from unreadable
    if not isinstance(document, list):
        message = (
            f"{payload.source} yielded {type(document).__name__} rather than a list of inventory records. "
            f"An adapter yields records or fails (CPM-AD-29)."
        )
        raise InventoryRecordError(message)
    if not document:
        # A well-formed empty array is the most dangerous document there is, and
        # it is the one a reader most expects to be harmless. It is *not* an
        # inventory of no packages: it is a source that told this run nothing,
        # and a sweep that accepted it would record every package the system has
        # ever seen as departed -- permanently, in an append-only log, and
        # replayable at every later cut-off. A source with genuinely nothing to
        # say has stopped being an inventory source (CPM-FR-42, CPM-AD-29).
        message = (
            f"{payload.source} yielded an inventory naming no packages at all. That is refused rather than "
            f"ingested: an empty document is indistinguishable from a source that broke, and accepting one "
            f"would record every package the inventory has ever named as absent, in a log nothing may "
            f"correct (CPM-FR-42, CPM-AD-25)."
        )
        raise InventoryRecordError(message)

    records = tuple(_record(entry, position=position) for position, entry in enumerate(document))
    _refuse_repeated_keys(records, source=payload.source)
    return records


def _record(entry: object, *, position: int) -> InventoryRecord:
    """Turn one decoded entry into a record, refusing anything it cannot be.

    Args:
        entry: One element of the decoded document.
        position: Where it sat, so a refusal names the record a reader can find
            rather than only saying that one of them was wrong.

    Returns:
        The record.

    Raises:
        InventoryRecordError: When the entry is not an object, carries an
            undefined field, names no package, gives that package no name, or
            carries a signal that is not a non-negative integer.

    """
    if not isinstance(entry, dict):
        message = (
            f"inventory record {position} is {type(entry).__name__} rather than an object. Every record "
            f"names a package and its usage signals (CPM-FR-42)."
        )
        raise InventoryRecordError(message)
    undefined = sorted(set(entry) - RECORD_FIELDS)
    if undefined:
        message = (
            f"inventory record {position} carries the field(s) {undefined}, which the record contract does "
            f"not define. The run is refused rather than the field ignored: ingestion never asserts a "
            f"mapping (CPM-FR-42, CPM-FR-1), and a silently dropped field is a source that believes it "
            f"supplied one."
        )
        raise InventoryRecordError(message)
    key = entry.get(SOURCE_PACKAGE_KEY)
    if not isinstance(key, str) or not key.strip():
        message = (
            f"inventory record {position} declares {SOURCE_PACKAGE_KEY}={key!r} and names no package. "
            f"A record that cannot be traced back to a package key is one nothing can re-derive (CPM-FR-2)."
        )
        raise InventoryRecordError(message)
    name = key.strip()
    if len(name) > ASSOCIATOR_KEY_LENGTH:
        # Refused *here*, in the contract, rather than left to resolution.
        # `resolve_package_shell` refuses the same key, but it refuses it one
        # package at a time and halfway through a sweep -- which is a `partial`
        # run over a source that was malformed from the start, and `CPM-FR-42`
        # says no run partially ingests a malformed source. A document is either
        # ingestable whole or refused whole.
        #
        # The bound is `associator_key`'s, which is the column the key lands in
        # since `CPM-IDENTITY-S07` separated it from the name. The name has its
        # own, narrower bound below.
        message = (
            f"inventory record {position} declares a {SOURCE_PACKAGE_KEY} of {len(name)} characters, and "
            f"the column that holds it takes {ASSOCIATOR_KEY_LENGTH}. The document is refused rather than "
            f"ingested until this record: no run partially ingests a malformed source (CPM-FR-42)."
        )
        raise InventoryRecordError(message)
    package_name = _require_package_name(entry.get(PACKAGE_NAME), position=position)
    # Written out rather than unpacked from two mappings built over
    # `REQUIRED_SIGNALS` and `OPTIONAL_SIGNALS`. The mapping form needed a
    # blanket `# type: ignore[arg-type]` -- `dict[str, int | None]` cannot be
    # unpacked into parameters typed `int` -- and an ignore that wide would have
    # hidden a real signature change as readily as the one it was there for.
    # `test_the_record_fields_are_exactly_the_declared_signals` reconciles these
    # names against the two tuples in both directions, which is what a mapping
    # bought and is cheaper than what it cost.
    return InventoryRecord(
        source_package_key=name,
        package_name=package_name,
        internal_component_count=_required_count(
            entry.get("internal_component_count"),
            field="internal_component_count",
            key=name,
        ),
        internal_lob_count=_required_count(entry.get("internal_lob_count"), field="internal_lob_count", key=name),
        apps=_optional_count(entry.get("apps"), field="apps", key=name),
        platforms=_optional_count(entry.get("platforms"), field="platforms", key=name),
        downloads=_optional_count(entry.get("downloads"), field="downloads", key=name),
        versions=_optional_count(entry.get("versions"), field="versions", key=name),
    )


def _require_package_name(value: object, *, position: int) -> str:
    """Return the name the record gives its package, or refuse the record.

    Required of every record, and refused *here* for the reason the key's own
    bound is refused here: the document is decoded whole before the first row is
    written, so a record that could not produce a usable identity fails the run
    with nothing ingested rather than one package at a time halfway through a
    sweep (`CPM-FR-42`).

    The bound is `canonical_name`'s, because that is the column the name lands
    in. It is the same number the key is measured against above and it is checked
    against a different column: SQLite ignores `max_length` and PostgreSQL
    refuses, so an over-long name is a stored row on a developer's machine and a
    failed run in the gate unless it is refused where the value enters (`R-5`).

    Args:
        value: What the record carried for the name, or `None` when it carried
            nothing.
        position: Where the record sat, for the message.

    Returns:
        The name with surrounding whitespace removed.

    Raises:
        InventoryRecordError: When the record names no package, or names one
            longer than the column that has to hold it.

    """
    if not isinstance(value, str) or not value.strip():
        message = (
            f"inventory record {position} declares {PACKAGE_NAME}={value!r} and gives its package no name. "
            f"The name is what the shell's canonical name is created from, and a package with no name "
            f"cannot be corrected, exported or found again (CPM-FR-2)."
        )
        raise InventoryRecordError(message)
    name = value.strip()
    if len(name) > CANONICAL_NAME_LENGTH:
        message = (
            f"inventory record {position} declares a {PACKAGE_NAME} of {len(name)} characters, and a "
            f"package name holds {CANONICAL_NAME_LENGTH}. The document is refused rather than ingested "
            f"until this record: no run partially ingests a malformed source (CPM-FR-42)."
        )
        raise InventoryRecordError(message)
    return name


def _required_count(value: object, *, field: str, key: str) -> int:
    """Return one of the two counts every record must carry, or refuse the record.

    Args:
        value: What the record carried for this signal, or `None` when it carried
            nothing.
        field: Which signal, for the message.
        key: The record's source package key, for the message.

    Returns:
        The count.

    Raises:
        InventoryRecordError: When the signal is absent, or is not a count.

    """
    if value is None:
        message = (
            f"inventory record {key!r} declares no {field}. Both {' and '.join(REQUIRED_SIGNALS)} are "
            f"required on every record: together they are the internal usage breadth CPM-FR-4 ranks by, "
            f"and a record without them cannot be ranked (PRD Open Question 3b)."
        )
        raise InventoryRecordError(message)
    return _require_count(value, field=field, key=key)


def _optional_count(value: object, *, field: str, key: str) -> int | None:
    """Return one of the four nullable score inputs, or `None` for a missing one.

    Args:
        value: What the record carried for this signal, or `None` when it carried
            nothing.
        field: Which signal, for the message.
        key: The record's source package key, for the message.

    Returns:
        The count, or `None` when the source did not supply one. `None` means
        *missing* and is stored as NULL, which stays distinguishable from a
        stored `0` -- so a `0` here comes back as `0` and is never collapsed
        (PRD Appendix A.1 data rules, Open Question 3b).

    Raises:
        InventoryRecordError: When a present value is not a count.

    """
    if value is None:
        return None
    return _require_count(value, field=field, key=key)


def _require_count(value: object, *, field: str, key: str) -> int:
    """Refuse a usage signal that is not a count this schema can hold.

    Args:
        value: What the record carried, already known not to be `None`.
        field: Which signal, for the message.
        key: The record's source package key, for the message.

    Returns:
        The count, unchanged.

    Raises:
        InventoryRecordError: When the value is not a whole number, is negative,
            or exceeds what the column holds.

            `bool` is refused explicitly: it is a subclass of `int`, so `true` in
            a document would otherwise be a component count of one.

            Both bounds are refused *here* rather than left to the column, and
            for one reason: `PositiveIntegerField` is a check constraint on
            PostgreSQL and a suggestion on SQLite, so a negative or an oversized
            count is a stored row on a developer's machine and a failed run in
            the gate. That is `R-5`'s parity gap arriving through the one input
            this collector takes from outside, and it is closed where the value
            enters rather than where it lands.

    """
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > MAX_COUNT:
        message = (
            f"inventory record {key!r} declares {field}={value!r}, which is not a count this schema can "
            f"hold. A usage signal is a whole number between 0 and {MAX_COUNT}; omit it entirely to record "
            f"that the source did not say, which is stored as missing and stays distinguishable from zero "
            f"(PRD Appendix A.1 data rules)."
        )
        raise InventoryRecordError(message)
    return value


def _refuse_repeated_keys(records: Sequence[InventoryRecord], *, source: str) -> None:
    """Refuse a document that names one package twice.

    `CPM-FR-42`: a record "repeating a source package key fails the run". Two
    records for one key are two different claims about the same package's usage
    in one observation, and there is no rule for choosing between them that is
    not an invention -- so the document is refused rather than the last one
    winning by arriving second.

    Args:
        records: Every record the document yielded.
        source: The locator, for the message.

    Raises:
        InventoryRecordError: When any source package key appears more than once.

    """
    # `Counter` rather than `list.count` inside a comprehension over the same
    # list. The latter is one pass per record over every record, which at
    # `CPM-NFR-1`'s ten thousand packages is a hundred million comparisons on
    # every sweep, for a check that has to be linear to be worth having.
    repeated = sorted(key for key, seen in Counter(record.source_package_key for record in records).items() if seen > 1)
    if repeated:
        message = (
            f"{source} names the package key(s) {repeated} more than once. One observation records one fact "
            f"per package (CPM-AD-7); two records for one key are two claims about the same package, and "
            f"choosing between them would be an invention (CPM-FR-42)."
        )
        raise InventoryRecordError(message)


class InventoryIngestionCollector(Collector):
    """The collector that observes the internal inventory. Writes `inventory_snapshots`.

    See the module docstring for why it never writes the package table, why the
    transaction is per package, and why its three per-package hooks refuse.
    """

    #: The nine declarations the base checks at construction. Every one is
    #: written out, including the two that would otherwise inherit a usable
    #: default, because a declaration a reader has to go and look up in the base
    #: is one they cannot check against the source this collector reads.
    name: ClassVar[str] = COLLECTOR_NAME

    evidence_model: ClassVar[type[AppendOnlyModel] | None] = InventorySnapshot

    #: `NO_WINDOW`: observe on every run. A sweep that was scheduled has been
    #: asked to run, and suppressing it would suppress the absence observations
    #: too -- absence is the signal that decays, so it is the one that must not
    #: be skipped.
    observation_window: ClassVar[timedelta | None] = NO_WINDOW

    #: The cap `core/transport.py` allows, rather than a smaller number pretending
    #: to have been measured. A timeout is required of every collector and means
    #: nothing to a file adapter, so the honest declaration is the bound.
    timeout: ClassVar[float | None] = MAX_TIMEOUT

    #: None. A malformed or unreachable local document does not become
    #: well-formed on a second read, and `CPM-IDENTITY-S07` refuses it outright.
    retries: ClassVar[int] = 0

    rate_limit: ClassVar[RateLimit] = INVENTORY_RATE_LIMIT

    #: Empty, and declared empty. A file adapter ignores request headers, and the
    #: base refuses a conditional one from any collector anyway.
    headers: ClassVar[Mapping[str, str]] = MappingProxyType({})

    freshness_target: ClassVar[timedelta | None] = INVENTORY_FRESHNESS_TARGET

    #: `NO_CACHE`: short-circuits the cache read, the cache write and the
    #: conditional headers, so no `ETag` machinery runs against a source that has
    #: no validators to offer.
    response_cache_ttl: ClassVar[timedelta | None] = NO_CACHE

    def sweep_source(self) -> str:
        """Return the locator this run names in its ledger row and its logs.

        Returns:
            `INVENTORY_SOURCE`, which is opaque on purpose -- see its own
            comment. The adapter knows which file or endpoint it reads
            (`CPM-AD-29`); this collector must not.

        """
        return INVENTORY_SOURCE

    def persist_sweep(self, payload: Payload, *, observed_at: datetime) -> SweepOutcome:
        """Write one snapshot per package the document names, and one per absence.

        The whole of `CPM-AD-25`'s write path. The document is decoded in full
        first, so a malformed source leaves nothing behind; then each record gets
        one transaction of its own, in which resolution creates the shell if
        there is none and the snapshot is inserted beside it. A package that
        cannot be persisted is recorded as a failure and the sweep continues,
        which is what makes the run `partial` rather than losing the packages
        that worked (`CPM-AD-23`, `CPM-FR-15`).

        **Absences are recorded only by a run that observed something**, and that
        is the guard rather than an optimisation. An absence row asserts "the
        source no longer lists this package", which is a claim about a document
        this run read and acted on. A run whose every record failed read a
        document it could not act on at all, and writing absences off the back of
        it would record every package the source *did* still list as departed --
        permanently, in a log nothing may correct. `records_in` refuses an empty
        document for the same reason, one step earlier.

        Args:
            payload: What the adapter said, recorded.
            observed_at: The instant every row must carry, from the base's
                injected clock. Handed to the absence rows too: they are
                observations made by *this* run (`CPM-AD-7`).

        Returns:
            How many rows the records produced, how many their absence implied,
            and which packages could not be written.

        Raises:
            InventoryRecordError: When the document cannot be read as records.
                Raised before the first write, so the run fails with nothing
                ingested.

        """
        records = records_in(payload)
        trace_id = current_trace_id()
        observed = 0
        failures: list[str] = []
        for record in records:
            try:
                observed += self._observe(record, observed_at=observed_at, trace_id=trace_id)
            # Narrow, and both are reachable: resolution refuses a record it
            # cannot make an identity from, and the database refuses a row this
            # schema will not take. Neither is swallowed -- each becomes a
            # failure the ledger row names, which is what `partial` means.
            except (ResolutionError, DatabaseError) as unwritable:
                failures.append(f"{record.source_package_key}: {type(unwritable).__name__}: {unwritable}")
        derived = 0
        if observed:
            derived = self._observe_absences(
                named={record.source_package_key for record in records},
                observed_at=observed_at,
                trace_id=trace_id,
            )
        return SweepOutcome(observed_rows=observed, derived_rows=derived, failures=tuple(failures))

    def _observe(self, record: InventoryRecord, *, observed_at: datetime, trace_id: str) -> int:
        """Commit one package's shell and its snapshot, together.

        `transaction.atomic()` is *here* -- around one package -- and nowhere
        else in this module but its sibling below. It is nested inside the base's
        run recorder and never around it (`CPM-AD-23`), which
        `tests/unit/django_apps/test_inventory_ingestion.py` asserts structurally
        over this file.

        The row goes to `_write_evidence` rather than to `bulk_create`, which is
        what applies the base's declared-model and `observed_at` checks and puts
        it in the tally `Collector._require_counted` reconciles on the way out.

        Args:
            record: The package as the source described it.
            observed_at: The instant the row carries.
            trace_id: The run's correlation identifier (`CPM-AD-15`).

        Returns:
            How many rows were inserted, which is one.

        Raises:
            ResolutionError: When the record cannot produce a usable identity.
            DatabaseError: When the row is one this schema will not take.

        """
        with transaction.atomic():
            package = resolve_package_shell(
                source_package_key=record.source_package_key,
                package_name=record.package_name,
                identity_source=COLLECTOR_NAME,
                clock=self._clock,
            )
            return self._write_evidence(
                [
                    InventorySnapshot(
                        observed_at=observed_at,
                        package=package,
                        source_package_key=record.source_package_key,
                        state=OutcomeState.OK.value,
                        internal_component_count=record.internal_component_count,
                        internal_lob_count=record.internal_lob_count,
                        apps=record.apps,
                        platforms=record.platforms,
                        downloads=record.downloads,
                        versions=record.versions,
                        detail="",
                        trace_id=trace_id,
                    ),
                ],
                observed_at=observed_at,
            )

    def _observe_absences(self, *, named: set[str], observed_at: datetime, trace_id: str) -> int:
        """Record the packages that have just stopped being named, as observations.

        `CPM-AD-25`: absence is a row and never a deletion. The set is derived
        from the evidence rather than from the package table, and that is the
        narrower question: a package the *inventory* once listed and no longer
        does is what this collector observed, while a package row created by some
        other path was never this source's to say anything about.

        **Absence is recorded on the transition, not on every run, and the
        difference is the difference between an observation and a leak.** A
        package the source drops is absent from then on, so a rule of "write a
        row whenever the key is missing" writes one per sweep for ever, into a
        table nothing may prune -- ten thousand packages a day for one package
        that left. What is worth recording is that it *changed*: the run in which
        a package's latest observation stops being `ok` is the run that observed
        something new. So the latest state per package is what is read, and only a
        package whose latest is `ok` is recorded absent.

        That still leaves the absence re-readable at any later cut-off, which is
        what `snapshot_as_of` is for: the row's `observed_at` is when the package
        went, and no row after it means nothing has changed since.

        **A refused absence row fails the run rather than becoming a per-package
        failure**, which is the one place this method departs from `_observe`
        above. A record's failure is a fact about what the source supplied and
        the sweep can carry on past it; an absence row is built entirely from
        rows this table already holds, so a database that refuses one is refusing
        a row it accepted the makings of -- there is nothing to carry on from,
        and a run that swallowed it would report `partial` over a schema defect.

        Args:
            named: Every source package key the document carried, including the
                ones whose write failed -- a package that could not be persisted
                was named, so recording it absent would be false.
            observed_at: The instant the absence rows carry -- this run's, which
                is what makes "absent as of when" answerable.
            trace_id: The run's correlation identifier.

        Returns:
            How many absence rows were written.

        Raises:
            DatabaseError: When an absence row is refused. See above.

        """
        # Ordered ascending and folded into a mapping, so what survives per
        # package is its *latest* state and the key it was last seen under.
        # `distinct(*fields)` would do this in the database in one row per
        # package, and it is PostgreSQL-only while this suite runs on SQLite
        # locally -- so the fold is here. Rows for a key this document still
        # names are excluded first: a package keeps one key for its whole life
        # (the key is what its `associator_key` was created from, and unlike its
        # `canonical_name` nothing corrects it), so a named key is a package that
        # cannot be absent.
        latest: dict[int, tuple[str, str]] = {
            package_id: (state, key)
            for package_id, state, key in InventorySnapshot.objects.exclude(source_package_key__in=named)
            .order_by("observed_at", "pk")
            .values_list("package_id", "state", SOURCE_PACKAGE_KEY)
        }

        written = 0
        for package_id, (state, key) in latest.items():
            if state != OutcomeState.OK.value:
                continue
            # One transaction per package here too (`CPM-AD-23`), so an absence
            # row commits with the packages around it rather than with the whole
            # sweep. The row goes through the base for the reason `_observe`'s
            # does: it is what stamps-checks it and puts it in the tally.
            with transaction.atomic():
                written += self._write_evidence(
                    [
                        InventorySnapshot(
                            observed_at=observed_at,
                            package_id=package_id,
                            source_package_key=key,
                            state=OutcomeState.NOT_FOUND.value,
                            detail=ABSENT_DETAIL,
                            trace_id=trace_id,
                        ),
                    ],
                    observed_at=observed_at,
                )
        return written

    def source_for(self, *, package_id: int) -> str:
        """Refuse: this collector reads one document, not one locator per package.

        Args:
            package_id: The package a per-package caller asked about.

        Raises:
            CollectorConfigurationError: Always. Returning the document's own
                locator would make `collect(package_id=...)` re-read the whole
                inventory to observe one package -- a defect that would look
                like it worked.

        """
        self._no_per_package_path("source_for", package_id=package_id)

    def translate(self, payload: Payload, *, package_id: int, observed_at: datetime) -> Sequence[AppendOnlyModel]:
        """Refuse: a run-scoped document is not one package's payload.

        Args:
            payload: What a per-package caller recorded.
            package_id: The package it asked about.
            observed_at: The instant it would have stamped.

        Raises:
            CollectorConfigurationError: Always. `persist_sweep` is this
                collector's translation, and it writes many packages' rows in
                many transactions rather than returning one package's.

        """
        self._no_per_package_path("translate", package_id=package_id)

    def sentinel_evidence(
        self,
        *,
        state: OutcomeState,
        package_id: int,
        observed_at: datetime,
        detail: str,
    ) -> AppendOnlyModel:
        """Refuse: a failing sweep has no one package to write a sentinel row about.

        Args:
            state: The sentinel a per-package caller decided on.
            package_id: The package it asked about.
            observed_at: The instant it would have stamped.
            detail: What it says happened.

        Raises:
            CollectorConfigurationError: Always. The `not_found` rows this
                collector *does* write are absence observations about packages
                the source named before, written by `persist_sweep` where the key
                each was last seen under is known -- which this signature has no
                way to carry.

        """
        self._no_per_package_path("sentinel_evidence", package_id=package_id)

    def _no_per_package_path(self, hook: str, *, package_id: int) -> NoReturn:
        """Refuse for all three per-package hooks, in one place.

        Written once because the three refusals are one decision, and three
        separately worded messages would read as three different limitations.
        It raises rather than returning the exception for the caller to raise:
        `NoReturn` is what tells the type checker that a hook whose body is only
        this call still honours its own return annotation.

        Args:
            hook: Which hook was called, so the message names it.
            package_id: The package the caller asked about.

        Raises:
            CollectorConfigurationError: Always.

        """
        message = (
            f"{type(self).__name__}.{hook} was asked about package {package_id}, and this collector has no "
            f"per-package path. Inventory ingestion reads one document naming many packages (CPM-AD-25); it "
            f"is run through sweep(), and collect(package_id=...) belongs to the collectors that observe a "
            f"surface about a package the inventory already names."
        )
        raise CollectorConfigurationError(message)


@shared_task(name=INGEST_TASK_NAME)  # type: ignore[untyped-decorator]
def ingest_inventory(*, force: bool = False) -> str:
    """Ingest the inventory once, through the declared adapter.

    The `cpm.collect.` name is what routes this to the `collect` queue, derived
    by `core/queues.py` with no edit there. It declares **no schedule and no time
    limit**: cadence is data in `django_celery_beat` (`CPM-AD-20`, `CPM-NFR-2`),
    and the inherited limits are settings' (`CPM-AD-9`) --
    `tests/unit/django_apps/test_task_declaration_audit.py` is the gate on both.

    Args:
        force: Bypass the observation window, for `CPM-UJ-1`'s manually
            triggered recollection. Keyword-only, so a caller can never enqueue a
            forced run by getting an argument's position wrong.

            **It is inert for this collector today, and it is plumbed through
            anyway.** The window is `NO_WINDOW`, which the base short-circuits
            rather than querying, so nothing is being bypassed. What the
            parameter is for is the manual-recollection path itself: an operator
            triggering a sweep by hand goes through this task, and a task that
            could not carry the flag would have to grow one the day the window
            became non-zero -- at which point every existing caller would be
            silently subject to a window they had not asked for.
            `tests/integration/django_apps/test_inventory_ingestion.py` pins that
            it reaches the base rather than being dropped here.

    Returns:
        How the run ended, as the `RunState` value the ledger row carries. A
        string rather than the `CollectionResult`, because a task's return value
        is serialized into the result backend and the durable record of the run
        is the ledger row.

    Raises:
        InventoryAdapterError: When no adapter is declared. Raised before the
            recorder opens, so a component with no inventory source leaves no run
            row claiming to have observed an empty inventory.
        InventoryRecordError: When the declared adapter yields a document that is
            not readable as records. It escapes the task rather than being turned
            into a failed return value, which is what makes the whole document
            refused rather than half-ingested (`CPM-FR-42`); the ledger row is
            finalized to `failed` by the recorder on the way out, so the run is
            on the record even though nothing else is.
        ImproperlyConfigured: When the declared adapter refuses its *source*
            before it has produced a document at all -- `CPM-IDENTITY-S07`'s
            watchlist adapter raises `WatchlistError`, an `ImproperlyConfigured`,
            for a file that is missing, malformed or awaiting review. It leaves
            by the same route and with the same consequence as the line above:
            the base's transport handling catches `TransportError` and nothing
            else, so a misconfigured *source file* is not a recorded transport
            failure, it is a refused run. The distinction is the point --
            `CPM-AD-14` makes a bad watchlist a misconfigured deployment, and an
            operator is sent to the file rather than to a broken remote.
        CollectorConfigurationError: When the rows this collector reports writing
            are not the rows the base wrote. A defect in this class rather than
            in a source, and refused rather than recorded.

    """
    with InventoryIngestionCollector(clock=SystemClock(), transport=inventory_adapter()) as collector:
        return str(collector.sweep(force=force).state.value)


@shared_task(name=COLLECT_SOURCE_RELEASE_TASK_NAME)  # type: ignore[untyped-decorator]
def collect_source_release(*, package_id: int, force: bool = False) -> str:
    """Observe one package's upstream releases (`CPM-FR-7`).

    Package-scoped, where ingestion above is run-scoped, and that is the whole
    difference between them: this collector reads one locator per package
    (`CPM-AD-7`), so the unit of work is one package and the transaction and the
    ledger row are one package's too (`CPM-AD-23`). No transport is passed --
    the base builds one from the collector's declared timeout and retry count,
    which is the only place either becomes a call setting.

    It declares **no schedule and no time limit**: cadence is data in
    `django_celery_beat` (`CPM-AD-20`, `CPM-NFR-2`) and the inherited limits are
    settings' (`CPM-AD-9`). Nothing schedules this yet -- `CPM-CURRENCY-S05` owns
    the full-inventory sweep and the selection of the packages that have a source
    repository at all.

    Args:
        package_id: The package to observe, by the integer primary key
            `CPM-AD-3` fixes. Keyword only, so a caller can never enqueue a
            collection for the wrong package by getting an argument's position
            wrong.
        force: Bypass the observation window, for `CPM-UJ-1`'s manually triggered
            recollection.

    Returns:
        How the run ended, as the `RunState` value the ledger row carries. A
        string rather than the `CollectionResult`, because a task's return value
        is serialized into the result backend and the durable record of the run
        is the ledger row.

    Raises:
        RunLedgerError: When `package_id` names no package. The recorder checks
            the key before it writes the opening row (`CPM-EVIDENCE-S09`), so
            this leaves nothing behind at all.
        SourceLocatorError: When the package has no source repository, or has one
            this collector cannot read. The ledger row is finalized `failed`
            carrying the reason; see that class for why it is not an evidence row.
        SourceReleaseDocumentError: When the source served something that is not a
            release document. An `error` evidence row is written first and the
            ledger row is `failed`, so the run is on the record either way.

    """
    with SourceReleaseCollector(clock=SystemClock()) as collector:
        return str(collector.collect(package_id=package_id, force=force).state.value)


@shared_task(name=COLLECT_PYPI_RELEASE_TASK_NAME)  # type: ignore[untyped-decorator]
def collect_pypi_release(*, package_id: int, force: bool = False) -> str:
    """Observe one package's PyPI project (`CPM-FR-8`).

    Package-scoped on the same terms as `collect_source_release`: one locator per
    package (`CPM-AD-7`), one package's transaction and ledger row (`CPM-AD-23`),
    and no transport passed -- the base builds one from the collector's declared
    timeout and retry count.

    What is different is the third answer. A package whose release-ecosystem
    identity says it is not a Python package is never asked about on PyPI: the
    collector says so before any locator is built, the base writes the
    `not_applicable` row itself, and the run is `succeeded` with the reason --
    which is what keeps `CPM-FR-8`'s "never marked stale against PyPI for not
    being published there" true through `core/freshness.py`.

    It declares **no schedule and no time limit**: cadence is data in
    `django_celery_beat` (`CPM-AD-20`, `CPM-NFR-2`) and the inherited limits are
    settings' (`CPM-AD-9`). Nothing schedules this yet -- `CPM-CURRENCY-S05` owns
    the full-inventory sweep.

    Args:
        package_id: The package to observe, by the integer primary key
            `CPM-AD-3` fixes. Keyword only, so a caller can never enqueue a
            collection for the wrong package by getting an argument's position
            wrong.
        force: Bypass the observation window, for `CPM-UJ-1`'s manually triggered
            recollection.

    Returns:
        How the run ended, as the `RunState` value the ledger row carries. A
        string rather than the `CollectionResult`, because a task's return value
        is serialized into the result backend and the durable record of the run
        is the ledger row.

    Raises:
        RunLedgerError: When `package_id` names no package. The recorder checks
            the key before it writes the opening row (`CPM-EVIDENCE-S09`), so
            this leaves nothing behind at all.
        PyPILocatorError: When the package's release-ecosystem identity is not
            established, or its purl cannot be read as a PyPI project. The
            ledger row is finalized `failed` carrying the reason; see that class
            for why it is not an evidence row.
        PyPIDocumentError: When the source served something that is not a
            project document. An `error` evidence row is written first and the
            ledger row is `failed`, so the run is on the record either way.

    """
    with PyPIReleaseCollector(clock=SystemClock()) as collector:
        return str(collector.collect(package_id=package_id, force=force).state.value)


@shared_task(name=COLLECT_FEEDSTOCK_TASK_NAME)  # type: ignore[untyped-decorator]
def collect_feedstock(*, package_id: int, force: bool = False) -> str:
    """Observe one package's conda-forge feedstock (`CPM-FR-9`).

    Package-scoped on the same terms as the two release tasks: one question per
    package (`CPM-AD-7`), one package's transaction and ledger row (`CPM-AD-23`),
    and no transport passed -- the base builds one from the collector's declared
    timeout and retry count.

    What is different is that the *question* is chosen from identity before any
    call is made. A package resolution established a feedstock for is asked about
    that feedstock and its recipe; a package resolution established none for is
    asked about the staged-recipes queue and has the conventional feedstock
    confirmed absent. Both branches make at most two calls, and both land on one
    row -- which is what keeps `CPM-FR-9`'s "staged-recipe state is recorded
    separately from an existing feedstock" a property of the row rather than of
    two.

    It declares **no schedule and no time limit**: cadence is data in
    `django_celery_beat` (`CPM-AD-20`, `CPM-NFR-2`) and the inherited limits are
    settings' (`CPM-AD-9`). Nothing schedules this yet -- `CPM-CURRENCY-S05` owns
    the full-inventory sweep.

    Args:
        package_id: The package to observe, by the integer primary key
            `CPM-AD-3` fixes. Keyword only, so a caller can never enqueue a
            collection for the wrong package by getting an argument's position
            wrong.
        force: Bypass the observation window, for `CPM-UJ-1`'s manually triggered
            recollection.

    Returns:
        How the run ended, as the `RunState` value the ledger row carries. A
        string rather than the `CollectionResult`, because a task's return value
        is serialized into the result backend and the durable record of the run
        is the ledger row.

    Raises:
        RunLedgerError: When `package_id` names no package. The recorder checks
            the key before it writes the opening row (`CPM-EVIDENCE-S09`), so
            this leaves nothing behind at all.
        FeedstockLocatorError: When resolution has not reached the package's
            feedstock mapping, or the name it recorded is not a repository this
            collector could ask about. The ledger row is finalized `failed`
            carrying the reason; see that class for why `CPM-UJ-2` makes this a
            refusal rather than an absence row.
        FeedstockDocumentError: When the source served something that is not the
            document the branch asked for. An `error` evidence row is written
            first and the ledger row is `failed`, so the run is on the record
            either way.

    """
    with FeedstockCollector(clock=SystemClock()) as collector:
        return str(collector.collect(package_id=package_id, force=force).state.value)


@shared_task(name=COLLECT_CONDA_PACKAGE_TASK_NAME)  # type: ignore[untyped-decorator]
def collect_conda_package(*, package_id: int, force: bool = False) -> str:
    """Observe what each monitored conda channel publishes for one package (`CPM-FR-10`).

    Package-scoped on the same terms as the three collection tasks above: one
    question per package (`CPM-AD-7`), one package's transaction and ledger row
    (`CPM-AD-23`), and no transport passed -- the base builds one from the
    collector's declared timeout and retry count.

    What is different is that one run writes **several** rows: one per monitored
    `(channel, platform)` pair, because `CPM-FR-10` forbids merging channels and a
    build string is a property of a build, which is per platform. One channel's
    failure becomes `error` rows for that channel and never discards another
    channel's answer, which is `CPM-FR-15`'s partial success on the per-package
    path.

    It declares **no schedule and no time limit**: cadence is data in
    `django_celery_beat` (`CPM-AD-20`, `CPM-NFR-2`) and the inherited limits are
    settings' (`CPM-AD-9`). Nothing schedules this yet -- `CPM-CURRENCY-S05` owns
    the full-inventory sweep.

    **A misconfiguration leaves this task the same way a transient failure does,
    and Celery cannot tell the two apart.** `CondaChannelError` is permanent by
    construction: no retry will make an undeclared channel declared, and with the
    shipped empty default *every* enqueue of this task raises it. Nothing here
    declares `autoretry_for`, so a raised task is retried only by whatever policy
    a caller or the worker sets -- and under one that retries, an unconfigured
    component would spend its allowance on an unbounded run of identical failed
    collections, each writing a ledger row saying the same thing. Separating a
    permanent refusal from a transient failure at the task boundary is a decision
    about every collector's task rather than this one's, and is a `deferred` entry
    on `CPM-CURRENCY-S04`.

    Args:
        package_id: The package to observe, by the integer primary key
            `CPM-AD-3` fixes. Keyword only, so a caller can never enqueue a
            collection for the wrong package by getting an argument's position
            wrong.
        force: Bypass the observation window, for `CPM-UJ-1`'s manually triggered
            recollection.

    Returns:
        How the run ended, as the `RunState` value the ledger row carries. A
        string rather than the `CollectionResult`, because a task's return value
        is serialized into the result backend and the durable record of the run
        is the ledger row.

    Raises:
        RunLedgerError: When `package_id` names no package. The recorder checks
            the key before it writes the opening row (`CPM-EVIDENCE-S09`), so
            this leaves nothing behind at all.
        CondaChannelError: When no channel or no platform is declared -- which is
            what ships, because the choice is PRD Open Question 4's -- when a
            declared entry is not one this collector could ask about, or when the
            package's row went between the ledger's key check and the name read.
            The ledger row is finalized `failed` carrying the reason and no
            evidence row is written; see that class for why, and the paragraph
            above for what a retry policy would do with it.
        CondaDocumentError: When the *first* monitored channel served something
            that is not a package document. An `error` evidence row is written
            first and the ledger row is `failed`, so the run is on the record
            either way. A later channel's unreadable answer never reaches here.

    """
    with CondaPackageCollector(clock=SystemClock()) as collector:
        return str(collector.collect(package_id=package_id, force=force).state.value)


@shared_task(name=COLLECT_VULNERABILITY_TASK_NAME)  # type: ignore[untyped-decorator]
def collect_vulnerability(*, package_id: int, force: bool = False) -> str:
    """Match one package and its version against the declared advisory source (`CPM-FR-11`).

    Package-scoped on the same terms as the four collection tasks above: one
    question per package (`CPM-AD-7`), one package's transaction and ledger row
    (`CPM-AD-23`).

    **What is different is that the transport is passed rather than built**, and
    it is the *declared advisory source adapter* (`CPM-AD-29`). The four tasks
    above let the base build a `RequestsTransport` from their declarations
    because they each read one known public API; which advisory source this
    component reads is PRD Open Question 1, so it is substituted at the base's
    seam exactly as inventory ingestion's source is -- which is what makes AC 3
    true, that changing the source touches neither this collector nor any policy.

    **This component ships with no adapter declared**, so every enqueue of this
    task raises `AdvisorySourceError` until an operator declares one. That is
    deliberate: a source nobody chose would produce security findings an
    organisation never agreed to act on. The refusal happens before the recorder
    opens, so an unconfigured component leaves no ledger row claiming to have
    looked -- and `VulnerabilityCollector.selectable_packages` offers the sweep
    nothing at all while nothing is declared, so the scheduled dispatch enqueues
    no such task rather than ten thousand raising ones.

    It declares **no schedule and no time limit**: cadence is data in
    `django_celery_beat` (`CPM-AD-20`, `CPM-NFR-2`) and the inherited limits are
    settings' (`CPM-AD-9`).

    **A misconfiguration leaves this task the same way a transient failure does,
    and Celery cannot tell the two apart** -- the hazard
    `collect_conda_package` records above, reached here by a second route:
    `AdvisorySourceError` is permanent by construction, and with nothing declared
    *every* enqueue raises it. Nothing here declares `autoretry_for`. It is the
    same `deferred` entry `CPM-CURRENCY-S04` recorded against every collector
    task at once.

    Args:
        package_id: The package to observe, by the integer primary key
            `CPM-AD-3` fixes. Keyword only, so a caller can never enqueue a
            collection for the wrong package by getting an argument's position
            wrong.
        force: Bypass the observation window, for `CPM-UJ-1`'s manually triggered
            recollection.

    Returns:
        How the run ended, as the `RunState` value the ledger row carries. A
        string rather than the `CollectionResult`, because a task's return value
        is serialized into the result backend and the durable record of the run
        is the ledger row.

    Raises:
        AdvisorySourceError: When no advisory source adapter is declared -- which
            is what ships. Raised before the collector is constructed and
            therefore before the recorder opens, so it leaves nothing behind at
            all.
        RunLedgerError: When `package_id` names no package. The recorder checks
            the key before it writes the opening row (`CPM-EVIDENCE-S09`), so
            this leaves nothing behind either.
        AdvisoryLookupError: When the package's row went between the ledger's key
            check and the identity read, or when its purl builds a locator wider
            than the column that records it. The ledger row is finalized `failed`
            carrying the reason and no evidence row is written; a package with no
            *version* is not one of these -- it is an `unknown` row saying so.
        VulnerabilityDocumentError: When the declared adapter served something
            that is not an advisory document. An `error` evidence row is written
            first and the ledger row is `failed`, so the run is on the record
            either way.

    """
    with VulnerabilityCollector(clock=SystemClock(), transport=advisory_source()) as collector:
        return str(collector.collect(package_id=package_id, force=force).state.value)


@shared_task(name=COLLECT_KEV_TASK_NAME)  # type: ignore[untyped-decorator]
def collect_kev(*, package_id: int, force: bool = False) -> str:
    """Cross-reference one package's current advisories against the KEV catalog (`CPM-FR-12`).

    Package-scoped on the same terms as the five collection tasks above: one
    question per package (`CPM-AD-7`), one package's transaction and ledger row
    (`CPM-AD-23`).

    **The transport is passed rather than built**, and it is the *declared KEV
    source adapter* (`CPM-AD-29`) -- a second slot beside the advisory source's,
    not a second use of it. Which KEV source this component reads is PRD Open
    Question 1, so it is substituted at the base's seam exactly as the advisory
    source is, and the two are declared, withdrawn and refused independently: an
    operator may licence an advisory database and no catalog, and that is a state
    worth being able to be in.

    **This component ships with no adapter declared**, so every enqueue of this
    task raises `KevSourceError` until an operator declares one --
    and `KevCollector.selectable_packages` offers the sweep nothing at all while
    nothing is declared, so a scheduled run enqueues no such task.

    **The refusal happens inside an open ledger row**, which is the one place this
    task's shape differs from `collect_vulnerability`'s. A source withdrawn
    *between* a dispatch drawing its selection and its tasks running leaves ten
    thousand enqueued tasks that each refuse; refusing before the recorder opened
    would leave no trace of any of them -- a day on which nothing was observed and
    nothing anywhere says so. So a ledger row is opened first and the recorder
    finalizes it `failed` carrying the reason. **No evidence row is written**: a
    component with no catalog has not looked, and a row recording that it had would
    be an observation nobody made.

    **What the collection reads is not only what the catalog said.**
    `collectors/kev.py` reads `vulnerability_findings` for the advisories to
    cross-reference, which `CPM-AD-7` does not grant; that module's docstring
    argues the exception and `CPM-SECURITY-S02`'s Spec Change Log records it. The
    read is one table, read-only, and never a write.

    It declares **no schedule and no time limit**: cadence is data in
    `django_celery_beat` (`CPM-AD-20`, `CPM-NFR-2`) and the inherited limits are
    settings' (`CPM-AD-9`).

    **A misconfiguration leaves this task the same way a transient failure does,
    and Celery cannot tell the two apart** -- the hazard `collect_conda_package`
    records above, reached here by a third route: `KevSourceError` is permanent by
    construction, and with nothing declared *every* enqueue raises it. Nothing here
    declares `autoretry_for`. It is the same `deferred` entry `CPM-CURRENCY-S04`
    recorded against every collector task at once.

    Args:
        package_id: The package to observe, by the integer primary key
            `CPM-AD-3` fixes. Keyword only, so a caller can never enqueue a
            collection for the wrong package by getting an argument's position
            wrong.
        force: Bypass the observation window, for `CPM-UJ-1`'s manually triggered
            recollection.

    Returns:
        How the run ended, as the `RunState` value the ledger row carries. A
        string rather than the `CollectionResult`, because a task's return value
        is serialized into the result backend and the durable record of the run
        is the ledger row.

    Raises:
        KevSourceError: When no KEV source adapter is declared -- which is what
            ships, and what a withdrawal returns a running component to. Raised
            inside an open ledger row, which the recorder finalizes `failed`; no
            evidence row is written.
        KevEvidenceError: When this package's own advisory evidence cannot be
            cross-referenced -- more current advisories than one collection may
            record, or a matched finding naming no advisory. An `error` evidence
            row is written first and the ledger row is `failed`.
        RunLedgerError: When `package_id` names no package. The recorder checks
            the key before it writes the opening row (`CPM-EVIDENCE-S09`), so this
            leaves nothing behind either.
        KevDocumentError: When the declared adapter served something that is not a
            KEV catalog. An `error` evidence row is written first and the ledger
            row is `failed`, so the run is on the record either way.

    """
    declared = declared_kev_source()
    if declared is None:
        # Opened first and raised inside, so a withdrawal met by an already-enqueued
        # task records the run rather than vanishing. `kev_source()` is what raises,
        # rather than a second message here that could drift from it.
        with collection_run(collector=KEV_COLLECTOR_NAME, clock=SystemClock(), package_id=package_id):
            kev_source()
    with KevCollector(clock=SystemClock(), transport=declared) as collector:
        return str(collector.collect(package_id=package_id, force=force).state.value)


@shared_task(name=COLLECT_LICENSE_TASK_NAME)  # type: ignore[untyped-decorator]
def collect_license(*, package_id: int, force: bool = False) -> str:
    """Record what each monitored channel says one package is licensed under (`CPM-FR-13`).

    Package-scoped on the same terms as the six collection tasks above: one
    question per package (`CPM-AD-7`), one package's transaction and ledger row
    (`CPM-AD-23`).

    **The transport is the base's own**, which is the one place this task's shape
    differs from the two security tasks above it. Which advisory and KEV sources
    this component reads is PRD Open Question 1, so those two are declared adapters
    (`CPM-AD-29`); a licence is stated by the *channels an operator already
    declared* (`CPM-CURRENCY-S04`), so there is nothing here to substitute and this
    task is shaped like `collect_conda_package` rather than like its epic siblings.

    **This component ships monitoring no channel**, so `LicenseCollector`'s
    selection offers the sweep nothing at all until an operator declares
    `CPM_MONITORED_CHANNELS`, and a direct enqueue raises `LicenseChannelError`
    naming the setting. That is the same shipped state `collect_conda_package`
    documents and the same one PRD Open Question 4 leaves an operator to change.

    It declares **no schedule and no time limit**: cadence is data in
    `django_celery_beat` (`CPM-AD-20`, `CPM-NFR-2`) and the inherited limits are
    settings' (`CPM-AD-9`).

    **A misconfiguration leaves this task the same way a transient failure does,
    and Celery cannot tell the two apart** -- the hazard `collect_conda_package`
    records, reached here by a fourth route: `LicenseChannelError` is permanent by
    construction, and with nothing declared *every* enqueue raises it. Nothing here
    declares `autoretry_for`. It is the same `deferred` entry `CPM-CURRENCY-S04`
    recorded against every collector task at once.

    Args:
        package_id: The package to observe, by the integer primary key
            `CPM-AD-3` fixes. Keyword only, so a caller can never enqueue a
            collection for the wrong package by getting an argument's position
            wrong.
        force: Bypass the observation window, for `CPM-UJ-1`'s manually triggered
            recollection.

    Returns:
        How the run ended, as the `RunState` value the ledger row carries. A
        string rather than the `CollectionResult`, because a task's return value
        is serialized into the result backend and the durable record of the run
        is the ledger row.

    Raises:
        RunLedgerError: When `package_id` names no package. The recorder checks
            the key before it writes the opening row (`CPM-EVIDENCE-S09`), so this
            leaves nothing behind at all.
        LicenseChannelError: When no channel is declared -- which is what ships --
            when a declared entry is not one this collector could ask about, or when
            the package's row went between the ledger's key check and the name read.
            The ledger row is finalized `failed` carrying the reason and no evidence
            row is written; see that class for why.
        LicenseDocumentError: When the *first* monitored channel served something
            that is not a package document. An `error` evidence row is written first
            and the ledger row is `failed`, so the run is on the record either way.
            A later channel's unreadable answer never reaches here.

    """
    with LicenseCollector(clock=SystemClock()) as collector:
        return str(collector.collect(package_id=package_id, force=force).state.value)


@shared_task(name=COLLECT_PYTHON_READINESS_TASK_NAME)  # type: ignore[untyped-decorator]
def collect_python_readiness(*, package_id: int, force: bool = False) -> str:
    """Record what one package's declared metadata claims about Python 3.14 (`CPM-FR-14`).

    Package-scoped on the same terms as the seven collection tasks above: one
    question per package (`CPM-AD-7`), one package's transaction and ledger row
    (`CPM-AD-23`).

    **The transport is the base's own** and there is nothing here to substitute:
    the source is the release ecosystem `identity` already established, so unlike
    the two security tasks this one declares no adapter, and it is shaped like
    `collect_pypi_release` rather than like its epic sibling will be.

    **It is on the `collect` queue and `CPM-PY314-S02`'s verification will not be.**
    `CPM-FR-14` splits readiness into a cheap static pass and an expensive build, and
    `CPM-AD-20` puts the two on different queues precisely so a five-minute compute
    job cannot starve a scheduled sweep (`R-11`). This task makes one HTTP request
    and reads a specifier.

    **It infers and never verifies.** Nothing here builds, imports or starts a
    subprocess, and the row it writes says so in its own state value: the determinate
    values name the inference (`collectors/outcomes.py`), so a reader on a queue
    cannot mistake a metadata claim for a build that ran.

    It declares **no schedule and no time limit**: cadence is data in
    `django_celery_beat` (`CPM-AD-20`, `CPM-NFR-2`) and the inherited limits are
    settings' (`CPM-AD-9`).

    **A misconfiguration leaves this task the same way a transient failure does, and
    Celery cannot tell the two apart** -- the hazard `collect_conda_package` records,
    reached here by a fifth route: `PythonReadinessIdentityError` is permanent for as
    long as the package's identity stays unresolved. Nothing here declares
    `autoretry_for`. It is the same `deferred` entry `CPM-CURRENCY-S04` recorded
    against every collector task at once.

    Args:
        package_id: The package to assess, by the integer primary key `CPM-AD-3`
            fixes. Keyword only, so a caller can never enqueue a collection for the
            wrong package by getting an argument's position wrong.
        force: Bypass the observation window, for `CPM-UJ-1`'s manually triggered
            recollection.

    Returns:
        How the run ended, as the `RunState` value the ledger row carries. A string
        rather than the `CollectionResult`, because a task's return value is
        serialized into the result backend and the durable record of the run is the
        ledger row.

    Raises:
        RunLedgerError: When `package_id` names no package. The recorder checks the
            key before it writes the opening row (`CPM-EVIDENCE-S09`), so this leaves
            nothing behind at all.
        PythonReadinessIdentityError: When identity has established nothing about the
            package's release ecosystem, or established one this collector does not
            read. The ledger row is finalized `failed` carrying the reason, no
            evidence row is written, and the reason says the compatibility question
            is *unanswered* rather than inapplicable. The scheduled sweep never
            enqueues such a package; a forced recollection can.
        PythonReadinessDocumentError: When the source served something that is not a
            project document. An `error` evidence row is written first and the ledger
            row is `failed`, so the run is on the record either way.

    """
    with PythonReadinessCollector(clock=SystemClock()) as collector:
        return str(collector.collect(package_id=package_id, force=force).state.value)


@shared_task(name=COLLECT_RESOLVE_IDENTITY_TASK_NAME)  # type: ignore[untyped-decorator]
def resolve_identity(*, package_id: int, force: bool = False) -> str:
    """Resolve one package's mappings from conda-forge's index and PyPI (`CPM-FR-1`).

    Package-scoped on the same terms as the collection tasks above: one package
    per task (`CPM-AD-7`), one package's ledger row (`CPM-AD-23`), and no
    transport passed -- the base builds one from the collector's declared
    timeout and retry count.

    What is different is what the run *writes*. Every other collector's evidence
    is its own table and nothing else; this one hands what it found to
    `identity`'s `record_resolution` -- the one door `CPM-AD-14` leaves open --
    and its evidence row is the record of what each source said and what was
    chosen. The recording and the row's construction share one per-package
    transaction the collector opens inside `translate`, nested inside the run
    recorder and never around it.

    It declares **no schedule and no time limit**: cadence is data in
    `django_celery_beat` (`CPM-AD-20`, `CPM-NFR-2`) and the inherited limits are
    settings' (`CPM-AD-9`). `cpm-sweep-resolve-identity` is what fires it.

    Args:
        package_id: The package to resolve, by the integer primary key
            `CPM-AD-3` fixes. Keyword only, as every per-package task's is.
        force: Bypass the observation window, for `CPM-UJ-1`'s manually triggered
            recollection.

    Returns:
        How the run ended, as the `RunState` value the ledger row carries.

    Raises:
        RunLedgerError: When `package_id` names no package. The recorder checks
            the key before it writes the opening row (`CPM-EVIDENCE-S09`), so
            this leaves nothing behind at all.
        ResolutionLocatorError: When the package's row cannot be resolved through
            the recorder -- a blank source or key -- or its name cannot be turned
            into a locator. The ledger row is finalized `failed` carrying the
            reason.
        ResolutionDocumentError: When conda-forge's index served something that
            is not an index entry. An `error` evidence row is written first and
            the ledger row is `failed`; nothing is recorded on the package.
        ResolutionError: When the recorder refuses what it was handed. The
            per-package transaction is rolled back, an `error` row is written
            and the ledger row is `failed`.

    """
    with IdentityResolutionCollector(clock=SystemClock()) as collector:
        return str(collector.collect(package_id=package_id, force=force).state.value)


@shared_task(name=VERIFY_PY314_TASK_NAME)  # type: ignore[untyped-decorator]
def verify_py314_build(*, package_id: int) -> str:
    """Build and import one package under Python 3.14 and record what happened (`CPM-FR-14`).

    Package-scoped on the same terms as the nine collection tasks above: one
    question per package (`CPM-AD-7`), one package's transaction and ledger row
    (`CPM-AD-23`). Everything else about it is different, and each difference is an
    acceptance criterion.

    **It is on the `verify` queue, and the name is the whole of why.** `cpm.verify.`
    is what `core/queues.py`'s derived route table sends there, so AC 1's queue
    requirement is a property of the string above rather than of a route somebody
    could get wrong or a setting somebody could edit. `CPM-AD-20` puts a
    compute-backed build on its own queue precisely so a five-minute job cannot
    starve the daily security sweep (`R-11`), and this is the first task in this
    component that is such a job.

    **Nothing schedules it, and nothing may.** `CPM-FR-14` makes verification "a
    separate, optionally triggered capability" and AC 3 says it is not run across the
    inventory by default. `Py314VerificationCollector.selectable_packages` answers
    `None`, so `collectors/sweep.py` refuses to dispatch it by name and no
    `CELERY_BEAT_SCHEDULE` entry names it: a beat entry added later would fail at its
    first tick rather than quietly verifying ten thousand packages.

    **The transport is passed rather than built, and it is the declared execution
    backend** (`CPM-AD-29`) -- the same shape `collect_vulnerability` takes, for a
    stronger reason. What is substituted there is which advisory database is read;
    what is substituted here is **what code runs on which machine**.
    `collectors/verification.py` is the slot and argues why this component ships
    with none.

    **This component ships with no backend declared**, so every enqueue of this task
    raises `VerificationBackendError` until an operator declares one. The refusal
    happens before the collector is constructed and therefore before the recorder
    opens, so an unconfigured component leaves no ledger row and no evidence row
    claiming to have verified anything -- and because nothing sweeps this collector,
    there is no scheduled dispatch enqueueing raising tasks either.

    **There is no `force`, and its absence is a decision.** Every collection task
    above takes one, because every one of them is swept and a window is what stops a
    sweep re-reading a source it read an hour ago. This collector declares
    `NO_WINDOW`: a run happens because somebody asked for it, so there is nothing to
    bypass, and a parameter that provably did nothing would read as a switch that
    might.

    It declares **no schedule and no time limit**: cadence is data in
    `django_celery_beat` (`CPM-AD-20`, `CPM-NFR-2`) and the inherited limits are
    settings' (`CPM-AD-9`). That second one bites harder here than anywhere else in
    this module -- a backend whose `fetch` blocks for the length of a real build
    meets the inherited soft limit and is killed with no row written, so a backend
    drives the work elsewhere and answers about a run that has already finished.
    `collectors/verification.py` states it as part of the adapter contract,
    `docs/conda-sentinel/operations.md` states it to an operator, and `CPM-PY314-S02` records it as
    deferred work because resolving it means changing a limit `CPM-AD-9` owns.

    **A misconfiguration leaves this task the same way a transient failure does, and
    Celery cannot tell the two apart** -- the hazard `collect_conda_package` records,
    reached here by a sixth route: `VerificationBackendError` is permanent by
    construction, and with nothing declared *every* enqueue raises it. Nothing here
    declares `autoretry_for`, and an automatic retry would be worse here than
    anywhere else in this module: it would re-run somebody's build.

    Args:
        package_id: The package to verify, by the integer primary key `CPM-AD-3`
            fixes. Keyword only, so a caller can never enqueue a build for the wrong
            package by getting an argument's position wrong.

    Returns:
        How the run ended, as the `RunState` value the ledger row carries. A string
        rather than the `CollectionResult`, because a task's return value is
        serialized into the result backend and the durable record of the run is the
        ledger row -- and the durable record of what the build *did* is the evidence
        row, which outlives both.

    Raises:
        VerificationBackendError: When no execution backend is declared -- which is
            what ships. Raised before the collector is constructed and therefore
            before the recorder opens, so it leaves nothing behind at all.
        RunLedgerError: When `package_id` names no package. The recorder checks the
            key before it writes the opening row (`CPM-EVIDENCE-S09`), so this leaves
            nothing behind either.
        Py314VerificationIdentityError: When identity has established nothing about
            the package's release ecosystem, or established it and recorded no purl.
            The ledger row is finalized `failed` carrying the reason, no evidence row
            is written, and the reason says the verification question is *unanswered*
            rather than inapplicable.
        Py314VerificationDocumentError: When the backend answered something that is
            not a verification result -- including one that reached a verdict without
            saying where it ran. An `error` evidence row is written first and the
            ledger row is `failed`, so the run is on the record either way.

    """
    with Py314VerificationCollector(clock=SystemClock(), transport=verification_backend()) as collector:
        return str(collector.collect(package_id=package_id).state.value)


@shared_task(name=SWEEP_TASK_NAME)  # type: ignore[untyped-decorator]
def collect_sweep(*, collector: str) -> str:
    """Enqueue one per-package collection for every package one collector can be asked about.

    The task `django_celery_beat` fires (`CPM-AD-20`), one entry per per-package
    collector, at the cadence that collector declares -- and the only task in this
    module that takes a *collector* rather than a package. What it does is
    `collectors/sweep.py`'s dispatch: select, enqueue in chunks, and finalize one
    run-ledger row scoped to no package. It collects nothing itself and makes no
    outbound call, so nothing about the nine collectors' guarantees changes: every
    observation is still written by the per-package task through the collector
    base, in that package's own transaction and under that package's own ledger
    row (`CPM-AD-23`).

    **One dispatch per collector is what makes `CPM-FR-15` structural.** A
    dispatch that raises finalizes its own row `failed` and no other collector's
    dispatch is in its call stack, so one rate-limited or misconfigured source
    cannot cost a day of monitoring everywhere else -- which is this story's whole
    subject. A dispatch that enqueued some of its packages and not all records
    `partial`, never `failed`.

    It declares **no schedule and no time limit** for the reason the seven
    collection tasks do not: cadence is data in `django_celery_beat`
    (`CPM-AD-20`, `CPM-NFR-2`) and the inherited limits are settings'
    (`CPM-AD-9`). The schedule that fires it lives in
    `config/settings/base.py`'s `CELERY_BEAT_SCHEDULE`, and a component whose
    schedule and whose collectors disagree about a cadence refuses to start --
    from `collectors/apps.py`'s `ready()`, which is the hook that registers the
    collectors and therefore the only one positioned to see them in a deployed
    process; `config/startup/stage_two.py` evaluates the same rule as condition
    11 for the contract and for the suite.

    **The inherited soft limit applies to this task, and it is handled rather
    than met.** A dispatch of `CPM-NFR-1`'s ten thousand packages is ten thousand
    broker round trips inside one task; `SoftTimeLimitExceeded` is caught by name,
    the packages already enqueued stay enqueued, and the row is finalized
    `partial` saying so. Nothing raises the limit (`CPM-AD-9` forbids it) and the
    next tick offers the whole selection again.

    Args:
        collector: The collector to dispatch, by the declared name
            `core/registry.py` keys it under. Keyword only, as every argument in
            this module is -- and here the keyword is also what a
            `CELERY_BEAT_SCHEDULE` entry passes, which
            `collectors/sweep.py`'s `COLLECTOR_KWARG` names and
            `tests/unit/django_apps/test_sweep.py` reconciles against this
            signature.

    Returns:
        How the dispatch ended, as the `RunState` value the ledger row carries. A
        string rather than the `DispatchOutcome`, because a task's return value
        is serialized into the result backend and the durable record of the run is
        the ledger row.

    Raises:
        SweepDispatchError: When the name is blank, names no registered
            collector, names one that is not swept per package, or names one
            whose derived per-package task Celery does not hold. The ledger row is
            finalized `failed` carrying the reason and nothing was enqueued -- and
            like `CondaChannelError` next door, it is permanent by construction:
            no retry will make an unregistered collector registered. Nothing here
            declares `autoretry_for`, and the hazard is the `deferred` entry
            `CPM-CURRENCY-S04` recorded against every collector task at once.

    """
    return str(dispatch(collector=collector, clock=SystemClock()).state.value)
