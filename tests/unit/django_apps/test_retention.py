"""`CPM-OPERATE-S07`: the retention's declarations, reconciled against what they describe.

Nothing here touches a database. The cases are about the *declarations* the
purge rests on -- the setting and its refusal, the token the door opens for, the
roster of tables and the keys their readers take the newest row under, the
indexes every cut-off scan needs -- each reconciled against the models and the
registry rather than restated, so a new evidence table, a renamed column or a
dropped index fails here before it fails on the first nightly purge.
"""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import TYPE_CHECKING
from typing import Final

import pytest
from django.apps import apps
from django.core.exceptions import ImproperlyConfigured
from django.db import models
from django.test import override_settings
from django.test.utils import isolate_apps

from conda_sentinel.collectors.tasks import declared_inventory_adapter
from conda_sentinel.collectors.tasks import withdraw_inventory_adapter
from conda_sentinel.core.clock import FixedClock
from conda_sentinel.core.models import COLLECTION_RUN_FINISHED_INDEX
from conda_sentinel.core.models import COLLECTION_RUN_STARTED_INDEX
from conda_sentinel.core.models import POLICY_RUN_CUTOFF_INDEX
from conda_sentinel.core.models import POLICY_RUN_FINISHED_INDEX
from conda_sentinel.core.models import AppendOnlyError
from conda_sentinel.core.models import AppendOnlyModel
from conda_sentinel.core.models import CollectionRun
from conda_sentinel.core.models import PackageHealth
from conda_sentinel.core.models import PolicyRun
from conda_sentinel.core.registry import registrations
from conda_sentinel.core.retention import DEFAULT_BATCH_SIZE
from conda_sentinel.core.retention import DEFAULT_RETENTION_DAYS
from conda_sentinel.core.retention import EVIDENCE_ROSTER
from conda_sentinel.core.retention import EXCLUDED_EVIDENCE
from conda_sentinel.core.retention import MAXIMUM_BATCH_SIZE
from conda_sentinel.core.retention import MAXIMUM_RETENTION_DAYS
from conda_sentinel.core.retention import MINIMUM_RETENTION_DAYS
from conda_sentinel.core.retention import PACKAGE_KEY
from conda_sentinel.core.retention import PRUNE_COLLECTOR
from conda_sentinel.core.retention import RETENTION_SETTING
from conda_sentinel.core.retention import ROLLUP_LABEL
from conda_sentinel.core.retention import TablePurge
from conda_sentinel.core.retention import derived_tables
from conda_sentinel.core.retention import is_retention_door
from conda_sentinel.core.retention import purge_evidence
from conda_sentinel.core.retention import retention_cutoff
from conda_sentinel.core.retention import retention_days
from conda_sentinel.core.retention import retention_fault
from tests.clocks import FIXED_INSTANT
from tests.model_registry import FIXTURE_APP
from tests.model_registry import FIXTURE_LABEL
from tests.model_registry import evidence_models

if TYPE_CHECKING:
    from collections.abc import Iterator

#: The application whose `ready()` refuses the setting.
COLLECTORS_APP_LABEL: Final[str] = "collectors"

#: The eight derived pass tables, by label, and the one other citer of a run.
DERIVED_LABELS: Final[frozenset[str]] = frozenset(
    {
        "policies.PackageCurrency",
        "policies.PackageFeedstockPresence",
        "policies.PackageVulnerability",
        "policies.PackageLicense",
        "policies.PackageRemediation",
        "policies.PackagePythonReadiness",
        "policies.PackagePriority",
        "policies.PackageWorkType",
    },
)

#: The keys the story states for the four tables whose readers key wider than the
#: package, the two whose readers filter by Python series first, and the
#: inventory table whose absence reader folds by source key.
STATED_KEYS: Final[dict[str, tuple[str, ...]]] = {
    "collectors.InventorySnapshot": (PACKAGE_KEY, "source_package_key"),
    "collectors.CondaPackageSnapshot": (PACKAGE_KEY, "channel", "platform"),
    "collectors.LicenseFinding": (PACKAGE_KEY, "channel"),
    "collectors.VulnerabilityFinding": (PACKAGE_KEY, "advisory_id"),
    "collectors.KevFinding": (PACKAGE_KEY, "vulnerability_finding__advisory_id"),
    "collectors.PythonReadinessAssessment": (PACKAGE_KEY, "python_series"),
    "collectors.PythonVerificationResult": (PACKAGE_KEY, "python_series"),
}

#: How many purged evidence tables the story names.
PURGED_TABLES: Final[int] = 11


@pytest.fixture
def _slot_restored() -> Iterator[None]:
    """Start the boot cases from an empty inventory adapter slot, and leave it empty.

    Yields:
        None. The two withdrawals are the effect.

    """
    if declared_inventory_adapter() is not None:
        withdraw_inventory_adapter()
    yield
    if declared_inventory_adapter() is not None:
        withdraw_inventory_adapter()


def _label(model: type[models.Model]) -> str:
    """Return a model's `app_label.ModelName`.

    Args:
        model: The model.

    Returns:
        The label.

    """
    return str(model._meta.label)  # noqa: SLF001 - `_meta` is Django's own public-by-convention API


def _leading_index_fields(model: type[models.Model]) -> set[str]:
    """Return the first field of every index a model declares.

    Args:
        model: The model.

    Returns:
        The leading field names, without any `-` ordering prefix.

    """
    return {index.fields[0].lstrip("-") for index in model._meta.indexes}  # noqa: SLF001 - as above


# ---------------------------------------------------------------------------
# The setting.
# ---------------------------------------------------------------------------


def test_the_default_is_ninety_days_between_one_and_ten_years() -> None:
    """The product owner's decision, and "no forever except by a number"."""
    assert DEFAULT_RETENTION_DAYS == 90  # noqa: PLR2004 - the decision the story records
    assert MINIMUM_RETENTION_DAYS == 1
    assert MAXIMUM_RETENTION_DAYS == 3650  # noqa: PLR2004 - ten years
    assert RETENTION_SETTING == "CPM_EVIDENCE_RETENTION_DAYS"


@pytest.mark.parametrize("value", [1, 90, 365, 3650], ids=["one", "ninety", "a-year", "ten-years"])
def test_a_whole_number_of_days_at_or_above_one_is_usable(value: int) -> None:
    """Every usable value reads back as itself."""
    assert retention_fault(value) == ""
    with override_settings(**{RETENTION_SETTING: value}):
        assert retention_days() == value


@pytest.mark.parametrize(
    "value",
    [0, -1, 3651, 10_000, "abc", "90", 1.5, True, None, [90]],
    ids=["zero", "negative", "over-ten-years", "far-over", "text", "numeric-text", "float", "bool", "none", "list"],
)
def test_below_one_above_ten_years_or_not_an_integer_is_refused_naming_the_setting(value: object) -> None:
    """Matrix row `Setting`: refused, and the refusal names the setting.

    `True` is refused although it is an `int`, and `"90"` although it would
    parse: neither is a declaration anybody meant. Above ten years is refused
    because a value that large is a unit mistake, not a decision.

    Args:
        value: The declaration.

    """
    if value is not None:
        assert RETENTION_SETTING in retention_fault(value)
    with override_settings(**{RETENTION_SETTING: value}), pytest.raises(ImproperlyConfigured) as refused:
        retention_days()
    assert RETENTION_SETTING in str(refused.value)


@pytest.mark.usefixtures("_slot_restored")
@pytest.mark.parametrize("value", [0, "abc"], ids=["zero", "text"])
def test_boot_refuses_an_unusable_retention_naming_the_setting(value: object) -> None:
    """Matrix row `Setting`, at the hook: `ImproperlyConfigured` before the inventory adapter is declared.

    Args:
        value: The declaration.

    """
    with override_settings(**{RETENTION_SETTING: value}), pytest.raises(ImproperlyConfigured) as refused:
        apps.get_app_config(COLLECTORS_APP_LABEL).ready()

    assert RETENTION_SETTING in str(refused.value)
    assert declared_inventory_adapter() is None


@pytest.mark.usefixtures("_slot_restored")
def test_a_settings_module_declaring_no_retention_refuses_boot_by_name() -> None:
    """Absent from the settings module is a dropped assignment, not the default."""
    from django.conf import settings  # noqa: PLC0415 - deleted through Django's own override machinery

    with override_settings():
        delattr(settings, RETENTION_SETTING)

        with pytest.raises(ImproperlyConfigured) as refused:
            apps.get_app_config(COLLECTORS_APP_LABEL).ready()

    assert RETENTION_SETTING in str(refused.value)


@pytest.mark.usefixtures("_slot_restored")
def test_the_default_boots() -> None:
    """Ninety days lets the component start; the hook completes and declares the adapter as before."""
    with override_settings(**{RETENTION_SETTING: DEFAULT_RETENTION_DAYS}):
        apps.get_app_config(COLLECTORS_APP_LABEL).ready()

    assert declared_inventory_adapter() is not None


def test_the_cutoff_is_now_minus_the_retention() -> None:
    """The arithmetic, once, so every reader of the cut-off agrees on the instant."""
    now = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)

    assert retention_cutoff(now=now, days=90) == now - timedelta(days=90)


def test_a_naive_now_is_refused_citing_the_clock_decision() -> None:
    """`CPM-AD-26`: a naive instant has no offset to draw the window from."""
    with pytest.raises(ValueError, match="CPM-AD-26"):
        retention_cutoff(now=datetime(2026, 9, 13, 12, 0), days=90)  # noqa: DTZ001 - the naive instant is the subject


# ---------------------------------------------------------------------------
# The door.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("candidate", [object(), None, "door", 1, type("RetentionDoor", (), {})()])
def test_only_the_modules_own_token_is_the_door(candidate: object) -> None:
    """Matrix row `Door`, at the predicate: a look-alike is not the token.

    Args:
        candidate: What a caller might hand in.

    """
    assert is_retention_door(candidate) is False


@pytest.fixture
def observation() -> Iterator[type[AppendOnlyModel]]:
    """A concrete evidence model, registered only for the duration of one case.

    Yields:
        The model.

    """
    with isolate_apps(FIXTURE_APP):

        class Observation(AppendOnlyModel):
            fact = models.CharField(max_length=32)

            class Meta:
                app_label = FIXTURE_LABEL

        yield Observation


def test_the_door_refuses_anything_but_the_token(observation: type[AppendOnlyModel]) -> None:
    """Matrix row `Door`: `retire(door=object())` is refused before any statement.

    Args:
        observation: The evidence model.

    """
    with pytest.raises(AppendOnlyError, match="retire") as refused:
        observation.objects.all().retire(door=object())

    assert refused.value.model_label == _label(observation)
    assert "core/retention.py" in str(refused.value)


# ---------------------------------------------------------------------------
# The roster.
# ---------------------------------------------------------------------------


def test_the_roster_and_the_exclusions_are_exactly_the_evidence_models() -> None:
    """Every evidence model the registry classifies is purged or named excluded; nothing else is."""
    classified = {_label(model) for model in evidence_models()}
    purged = {entry.label for entry in EVIDENCE_ROSTER}

    assert len(EVIDENCE_ROSTER) == PURGED_TABLES
    assert purged & EXCLUDED_EVIDENCE == set()
    assert purged | EXCLUDED_EVIDENCE == classified
    assert {
        "collectors.InventoryChange",
        "collectors.OperatorDigest",
        "collectors.PackageRecollection",
        "identity.IdentityOverride",
    } == EXCLUDED_EVIDENCE


def test_every_roster_entry_resolves_to_an_append_only_model_with_a_table() -> None:
    """The labels are real, and every one is evidence."""
    for entry in EVIDENCE_ROSTER:
        assert issubclass(entry.model, AppendOnlyModel), entry.label
        assert entry.table == entry.model._meta.db_table, entry.label  # noqa: SLF001 - Django's own public-by-convention API


def test_the_purge_order_removes_a_citing_evidence_table_before_the_table_it_cites() -> None:
    """`kev_findings` before `vulnerability_findings`, derived from the `PROTECT` graph rather than assumed."""
    position = {entry.label: index for index, entry in enumerate(EVIDENCE_ROSTER)}
    for entry in EVIDENCE_ROSTER:
        for relation in entry.model._meta.related_objects:  # noqa: SLF001 - as above
            citing = _label(relation.related_model)
            if citing in position:
                assert position[citing] < position[entry.label], f"{citing} cites {entry.label} and must precede it"
    assert position["collectors.KevFinding"] < position["collectors.VulnerabilityFinding"]


def test_every_key_leads_with_the_package_and_names_a_column_the_model_has() -> None:
    """The keys are the readers', and each resolves on the model it is declared for."""
    for entry in EVIDENCE_ROSTER:
        assert entry.keys[0] == PACKAGE_KEY, entry.label
        for key in entry.keys:
            model: type[models.Model] = entry.model
            path = key.split("__")
            for step in path[:-1]:
                field = model._meta.get_field(step)  # noqa: SLF001 - as above
                assert field.is_relation, (entry.label, key)
                assert isinstance(field.related_model, type), (entry.label, key)
                model = field.related_model
            # `package_id` is the column; the field is `package`. Resolving it is
            # the assertion: `get_field` raises on a name the model lacks.
            model._meta.get_field("package" if path[-1] == PACKAGE_KEY else path[-1])  # noqa: SLF001 - as above


def test_the_exclusion_keys_are_the_ones_the_story_states_and_the_readers_take() -> None:
    """Matrix rows on keys: per channel and platform, per channel, per advisory, per series; else per package."""
    keys = {entry.label: entry.keys for entry in EVIDENCE_ROSTER}

    for label, stated in STATED_KEYS.items():
        assert keys[label] == stated, label
    for label, declared in keys.items():
        if label not in STATED_KEYS:
            assert declared == (PACKAGE_KEY,), label


def test_the_derived_tables_are_the_eight_policies_tables_and_the_rollup_is_the_only_other_citer() -> None:
    """Derived rows go with their run; the rollup pins it; nothing else cites a run."""
    derived = derived_tables()

    assert {_label(model) for model, _relation in derived} == DERIVED_LABELS
    assert {relation for _model, relation in derived} == {"policy_run"}
    citers = {_label(relation.related_model) for relation in PolicyRun._meta.related_objects}  # noqa: SLF001
    assert citers == DERIVED_LABELS | {ROLLUP_LABEL}
    assert _label(PackageHealth) == ROLLUP_LABEL


def test_the_purge_collector_name_is_registered_by_nothing() -> None:
    """The run records never appear on the Coverage screen, which reads the ledger by registered name."""
    assert PRUNE_COLLECTOR not in registrations()


# ---------------------------------------------------------------------------
# The indexes.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("entry", EVIDENCE_ROSTER, ids=lambda entry: entry.table)
def test_every_purged_evidence_table_declares_an_index_leading_with_observed_at(entry: object) -> None:
    """Every cut-off scan has an index that leads with the time column.

    Args:
        entry: The roster entry.

    """
    assert "observed_at" in _leading_index_fields(entry.model)  # type: ignore[attr-defined]


def test_both_ledgers_declare_indexes_leading_with_their_cutoff_columns() -> None:
    """`finished_at` and `started_at` on `collection_runs`; `finished_at` and `evidence_cutoff` on `policy_runs`."""
    assert {"finished_at", "started_at"} <= _leading_index_fields(CollectionRun)
    assert {"finished_at", "evidence_cutoff"} <= _leading_index_fields(PolicyRun)
    names = {index.name for index in CollectionRun._meta.indexes} | {index.name for index in PolicyRun._meta.indexes}  # noqa: SLF001
    assert {
        COLLECTION_RUN_FINISHED_INDEX,
        COLLECTION_RUN_STARTED_INDEX,
        POLICY_RUN_FINISHED_INDEX,
        POLICY_RUN_CUTOFF_INDEX,
    } <= names


# ---------------------------------------------------------------------------
# The purge's own refusals and its record.
# ---------------------------------------------------------------------------


def test_an_unbounded_batch_is_refused_before_anything_is_touched() -> None:
    """Never: an unbounded `DELETE`."""
    with pytest.raises(ValueError, match="unbounded"):
        purge_evidence(clock=FixedClock(instant=FIXED_INSTANT), retention_days=90, batch_size=0)


def test_the_default_batch_is_a_thousand_and_the_ceiling_ten_thousand() -> None:
    """The bound the command defaults to, and the most it accepts."""
    assert DEFAULT_BATCH_SIZE == 1000  # noqa: PLR2004 - the story's number
    assert MAXIMUM_BATCH_SIZE == 10_000  # noqa: PLR2004 - below SQLite's bound-parameter limit
    assert MAXIMUM_BATCH_SIZE < 32_766  # noqa: PLR2004 - SQLite's default SQLITE_MAX_VARIABLE_NUMBER


def test_a_batch_above_the_ceiling_is_refused_before_anything_is_touched() -> None:
    """Each key is one bound parameter; a batch past the ceiling is a statement some backend refuses."""
    with pytest.raises(ValueError, match="at most"):
        purge_evidence(clock=FixedClock(instant=FIXED_INSTANT), retention_days=90, batch_size=MAXIMUM_BATCH_SIZE + 1)


def test_the_record_detail_names_the_table_the_cutoff_and_every_count() -> None:
    """What an operator reads off the ledger row."""
    result = TablePurge(
        table="pypi_release_snapshots",
        cutoff=FIXED_INSTANT,
        removed=(("deleted", 12),),
        kept_by_rule=3,
        protected=1,
        dry_run=True,
        run_id=7,
    )

    assert result.deleted == 12  # noqa: PLR2004 - the one leg's count
    assert result.detail == (
        f"table=pypi_release_snapshots cutoff={FIXED_INSTANT.isoformat()} deleted=12 "
        f"kept_by_rule=3 protected=1 dry_run=true"
    )
    two_legs = TablePurge(
        table="collection_runs",
        cutoff=FIXED_INSTANT,
        removed=(("finished", 5), ("unfinished", 2)),
        kept_by_rule=0,
        protected=0,
        dry_run=False,
        run_id=8,
    )
    assert two_legs.deleted == 7  # noqa: PLR2004 - the two legs' sum
    assert "deleted=7 finished=5 unfinished=2" in two_legs.detail
    failed = TablePurge(
        table="kev_findings",
        cutoff=FIXED_INSTANT,
        removed=(("deleted", 4),),
        kept_by_rule=0,
        protected=0,
        dry_run=False,
        run_id=9,
        error="OperationalError: connection lost",
    )
    assert failed.detail.endswith("dry_run=false error=OperationalError: connection lost")
