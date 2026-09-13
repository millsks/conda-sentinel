"""`CPM-OPERATE-S03`: the inventory service and the two models, as properties of the source.

The behavioural half -- every refusal, the one-transaction rule made to fail on
each side, the import in its three modes -- is
`tests/integration/django_apps/test_inventory_service.py`. What is here is what
no run can show: that the module opens exactly one `transaction.atomic()` and
both writes sit inside it (`CPM-AD-23`, on the terms
`tests/unit/django_apps/test_identity_overrides.py` states for the first governed
write), that neither write is deferred to a callback or a task, that the three
spellings of the inventory vocabulary agree, that the permission the model
declares is the one `core/roles.py` grants, and that the exception hierarchy a
surface branches on is the one it thinks it is.

Reads source and model metadata: no database.
"""

from __future__ import annotations

import ast
import inspect
from typing import Final

from conda_sentinel.collectors import inventory
from conda_sentinel.collectors.inventory import MAX_SIGNAL
from conda_sentinel.collectors.inventory import ImportOutcome
from conda_sentinel.collectors.inventory import InventoryChangeError
from conda_sentinel.collectors.inventory import InventoryEntryMissingError
from conda_sentinel.collectors.inventory import InventoryNotPermittedError
from conda_sentinel.collectors.inventory import InventoryRow
from conda_sentinel.collectors.models import INVENTORY_ACTIVE_NAME_CONSTRAINT
from conda_sentinel.collectors.models import INVENTORY_CHANGE_AUTHOR_CONSTRAINT
from conda_sentinel.collectors.models import INVENTORY_CHANGE_READ_INDEX
from conda_sentinel.collectors.models import INVENTORY_CHANGE_REASON_CONSTRAINT
from conda_sentinel.collectors.models import INVENTORY_KEY_CONSTRAINT
from conda_sentinel.collectors.models import INVENTORY_KEY_FIELD
from conda_sentinel.collectors.models import INVENTORY_NAME_FIELD
from conda_sentinel.collectors.models import INVENTORY_OPTIONAL_SIGNALS
from conda_sentinel.collectors.models import INVENTORY_REASON_CONSTRAINT
from conda_sentinel.collectors.models import INVENTORY_REQUIRED_SIGNALS
from conda_sentinel.collectors.models import INVENTORY_RETIRED_INDEX
from conda_sentinel.collectors.models import INVENTORY_SIGNALS
from conda_sentinel.collectors.models import InventoryChange
from conda_sentinel.collectors.models import InventoryEntry
from conda_sentinel.collectors.tasks import MAX_COUNT
from conda_sentinel.collectors.tasks import OPTIONAL_SIGNALS
from conda_sentinel.collectors.tasks import PACKAGE_NAME
from conda_sentinel.collectors.tasks import REQUIRED_SIGNALS
from conda_sentinel.collectors.tasks import SOURCE_PACKAGE_KEY
from conda_sentinel.collectors.watchlist import KEY_COLUMN
from conda_sentinel.collectors.watchlist import MAX_PACKAGE_NAME
from conda_sentinel.collectors.watchlist import MAX_SIGNAL as WATCHLIST_MAX_SIGNAL
from conda_sentinel.collectors.watchlist import MAX_SOURCE_PACKAGE_KEY
from conda_sentinel.collectors.watchlist import NAME_COLUMN
from conda_sentinel.collectors.watchlist import OPTIONAL_COLUMNS
from conda_sentinel.collectors.watchlist import REQUIRED_COLUMNS
from conda_sentinel.core.models import AppendOnlyModel
from conda_sentinel.core.roles import INVENTORY_APP_LABEL
from conda_sentinel.core.roles import INVENTORY_CHANGE_CODENAME
from conda_sentinel.core.roles import INVENTORY_CHANGE_PERMISSION
from tests.model_registry import is_evidence_model
from tests.source_scan import dotted_name

#: The one function that opens the transaction, and the two writes it encloses:
#: `_save` (the only caller of `entry.save`) and `_audit` (the only caller of the
#: audit insert).
THE_WRITER: Final[str] = "_apply"
ENTRY_SAVE: Final[str] = "_save"
ENTRY_WRITE_FORMS: Final[frozenset[str]] = frozenset({"entry.save"})
AUDIT_INSERT: Final[str] = "_audit"
AUDIT_WRITE_FORM: Final[str] = "InventoryChange.objects.create"

#: The four doors, every one of which must reach the writer.
THE_DOORS: Final[tuple[str, ...]] = ("add_entry", "change_entry", "retire_entry", "import_watchlist")

#: The spellings of "defer this": a callback on the transaction, or a task.
DEFERRALS: Final[frozenset[str]] = frozenset({"on_commit", "delay", "apply_async"})


def _tree() -> ast.Module:
    """Return the service module, parsed.

    Returns:
        The tree.

    """
    return ast.parse(inspect.getsource(inventory))


def _function(tree: ast.Module, name: str) -> ast.FunctionDef:
    """Return one top-level function of the module.

    Args:
        tree: The parsed module.
        name: The function's name.

    Returns:
        Its node.

    """
    return next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == name)


def _atomic_blocks(node: ast.AST) -> list[ast.With]:
    """Return every `with transaction.atomic():` under a node.

    Args:
        node: Where to look.

    Returns:
        The blocks, in source order.

    """
    return [
        candidate
        for candidate in ast.walk(node)
        if isinstance(candidate, ast.With)
        and any(
            isinstance(item.context_expr, ast.Call) and dotted_name(item.context_expr.func) == "transaction.atomic"
            for item in candidate.items
        )
    ]


def _calls_in(node: ast.AST) -> set[str]:
    """Return the dotted spelling of every call under a node.

    Args:
        node: Where to look.

    Returns:
        The names.

    """
    return {dotted_name(call.func) for call in ast.walk(node) if isinstance(call, ast.Call)}


# ---------------------------------------------------------------------------
# One transaction, both writes inside it, nothing deferred (CPM-AD-23).
# ---------------------------------------------------------------------------


def test_the_module_opens_exactly_one_transaction_and_it_is_the_writers() -> None:
    """Every door reaches the table through one block; a second one anywhere fails here."""
    tree = _tree()

    assert len(_atomic_blocks(tree)) == 1
    assert len(_atomic_blocks(_function(tree, THE_WRITER))) == 1


def test_both_writes_sit_inside_the_one_transaction() -> None:
    """The entry's `save` and the audit insert are enclosed, and both are present at all."""
    tree = _tree()
    block = _atomic_blocks(_function(tree, THE_WRITER))[0]
    enclosed = _calls_in(block)

    assert ENTRY_SAVE in enclosed
    assert AUDIT_INSERT in enclosed
    assert _calls_in(_function(tree, ENTRY_SAVE)) & ENTRY_WRITE_FORMS
    assert AUDIT_WRITE_FORM in _calls_in(_function(tree, AUDIT_INSERT))
    # And nowhere else: the two write forms appear in exactly their one function.
    everywhere = {node.name: _calls_in(node) for node in tree.body if isinstance(node, ast.FunctionDef)}
    assert [name for name, calls in everywhere.items() if calls & ENTRY_WRITE_FORMS] == [ENTRY_SAVE]
    assert [name for name, calls in everywhere.items() if AUDIT_WRITE_FORM in calls] == [AUDIT_INSERT]


def test_every_door_reaches_the_one_writer() -> None:
    """Four callers, one write path: none of them writes the table on its own."""
    tree = _tree()

    for door in THE_DOORS:
        called = _calls_in(_function(tree, door))
        assert THE_WRITER in called or "_retire_unnamed" in called, door
        assert not (called & ENTRY_WRITE_FORMS), door
        assert AUDIT_WRITE_FORM not in called, door


def test_nothing_is_deferred_to_a_callback_or_a_task() -> None:
    """Never `on_commit`, never a follow-up task: the audit row lands with the change or not at all."""
    tree = _tree()
    deferred = {
        call.func.attr
        for call in ast.walk(tree)
        if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute) and call.func.attr in DEFERRALS
    }

    assert deferred == set()


def test_no_manager_update_or_delete_is_written() -> None:
    """Retire is a column write and every write is an instance `save`; the mutation-path audit's rule, restated here."""
    tree = _tree()
    forms = {name for name in _calls_in(tree) if name.endswith((".update", ".delete", ".bulk_update"))}

    assert forms == set()


# ---------------------------------------------------------------------------
# The vocabulary: three spellings, reconciled.
# ---------------------------------------------------------------------------


def test_the_table_vocabulary_is_the_watchlist_column_contract() -> None:
    """`collectors/models.py` and `collectors/watchlist.py` spell the same names."""
    assert INVENTORY_KEY_FIELD == KEY_COLUMN
    assert INVENTORY_NAME_FIELD == NAME_COLUMN
    assert (INVENTORY_KEY_FIELD, INVENTORY_NAME_FIELD, *INVENTORY_REQUIRED_SIGNALS) == REQUIRED_COLUMNS
    assert INVENTORY_OPTIONAL_SIGNALS == OPTIONAL_COLUMNS


def test_the_table_vocabulary_is_the_record_contract() -> None:
    """`collectors/models.py` and `collectors/tasks.py` spell the same names, and the same ceiling."""
    assert INVENTORY_KEY_FIELD == SOURCE_PACKAGE_KEY
    assert INVENTORY_NAME_FIELD == PACKAGE_NAME
    assert INVENTORY_REQUIRED_SIGNALS == REQUIRED_SIGNALS
    assert INVENTORY_OPTIONAL_SIGNALS == OPTIONAL_SIGNALS
    assert (*REQUIRED_SIGNALS, *OPTIONAL_SIGNALS) == INVENTORY_SIGNALS
    assert MAX_SIGNAL == MAX_COUNT == WATCHLIST_MAX_SIGNAL


def test_the_row_carries_exactly_the_name_and_the_six_signals() -> None:
    """`InventoryRow` is the record less its key; a seventh field is a failing case here."""
    assert tuple(InventoryRow.__dataclass_fields__) == (INVENTORY_NAME_FIELD, *INVENTORY_SIGNALS)
    assert tuple(ImportOutcome.__dataclass_fields__) == ("added", "changed", "unchanged", "retired", "retired_kept")


def test_from_record_reads_a_parsed_watchlist_row() -> None:
    """The mapping `records_from` yields becomes a row, optional columns absent read as missing."""
    row = InventoryRow.from_record(
        {
            INVENTORY_KEY_FIELD: "conda-forge/numpy",
            INVENTORY_NAME_FIELD: "numpy",
            "internal_component_count": 312,
            "internal_lob_count": 9,
            "apps": 148,
        },
    )

    assert row == InventoryRow(package_name="numpy", internal_component_count=312, internal_lob_count=9, apps=148)
    assert row.as_columns() == {
        INVENTORY_NAME_FIELD: "numpy",
        "internal_component_count": 312,
        "internal_lob_count": 9,
        "apps": 148,
        "platforms": None,
        "downloads": None,
        "versions": None,
    }


def test_the_entry_columns_are_as_wide_as_the_file_contract_and_the_shell() -> None:
    """A key or a name the file accepts fits the table, and a table row fits the shell it becomes."""
    meta = InventoryEntry._meta  # noqa: SLF001 - Django's own public-by-convention API

    assert meta.get_field(INVENTORY_KEY_FIELD).max_length == MAX_SOURCE_PACKAGE_KEY
    assert meta.get_field(INVENTORY_NAME_FIELD).max_length == MAX_PACKAGE_NAME
    assert inventory._KEY_LENGTH == MAX_SOURCE_PACKAGE_KEY  # noqa: SLF001 - the constant under test
    assert inventory._NAME_LENGTH == MAX_PACKAGE_NAME  # noqa: SLF001 - the constant under test


# ---------------------------------------------------------------------------
# The two models: which is evidence, what each declares.
# ---------------------------------------------------------------------------


def test_the_entry_is_not_evidence_and_the_change_is() -> None:
    """The table is mutable reference data; its audit trail is append-only evidence."""
    assert not issubclass(InventoryEntry, AppendOnlyModel)
    assert not is_evidence_model(InventoryEntry)
    assert issubclass(InventoryChange, AppendOnlyModel)
    assert is_evidence_model(InventoryChange)


def test_the_entry_declares_none_of_the_names_the_audits_classify_on() -> None:
    """No `observed_at`, `status` or `computed_at`: each is a mark for a kind of table this is not."""
    names = {field.name for field in InventoryEntry._meta.concrete_fields}  # noqa: SLF001 - Django's own accessor

    assert names.isdisjoint({"observed_at", "status", "computed_at"})
    assert {"retired_at", "changed_at", "reason", INVENTORY_KEY_FIELD, INVENTORY_NAME_FIELD, *INVENTORY_SIGNALS} < names


def test_the_entry_declares_its_constraints_and_index_by_the_module_constants() -> None:
    """One row per key, one active row per name, a reason on every row, and the active-rows index."""
    meta = InventoryEntry._meta  # noqa: SLF001 - Django's own accessor

    assert meta.db_table == "inventory"
    assert sorted(constraint.name for constraint in meta.constraints) == sorted(
        [INVENTORY_KEY_CONSTRAINT, INVENTORY_ACTIVE_NAME_CONSTRAINT, INVENTORY_REASON_CONSTRAINT],
    )
    by_name = {constraint.name: constraint for constraint in meta.constraints}
    partial = by_name[INVENTORY_ACTIVE_NAME_CONSTRAINT]
    assert partial.fields == ("package_name",)
    assert partial.condition is not None, "the name rule is over active rows only; a retired row keeps its name"
    assert [(index.name, index.fields) for index in meta.indexes] == [(INVENTORY_RETIRED_INDEX, ["retired_at"])]


def test_the_change_declares_prior_and_new_for_every_changeable_field() -> None:
    """Pairs, including `retired`, and no unique constraint of any kind."""
    meta = InventoryChange._meta  # noqa: SLF001 - Django's own accessor
    names = {field.name for field in meta.concrete_fields}

    for changeable in (INVENTORY_NAME_FIELD, *INVENTORY_SIGNALS, "retired"):
        assert f"prior_{changeable}" in names, changeable
        assert f"new_{changeable}" in names, changeable
    assert {"entry", "actor", "origin", "reason", "trace_id", "observed_at"} < names
    assert meta.db_table == "inventory_changes"
    # Two check constraints and no unique one: a reason on every row, and
    # exactly one of a person and a file. `test_evidence_constraint_audit.py`
    # sweeps the unique half across every evidence model.
    assert sorted(constraint.name for constraint in meta.constraints) == sorted(
        [INVENTORY_CHANGE_REASON_CONSTRAINT, INVENTORY_CHANGE_AUTHOR_CONSTRAINT],
    )
    assert meta.get_field("actor").null is True
    assert [(index.name, index.fields) for index in meta.indexes] == [
        (INVENTORY_CHANGE_READ_INDEX, ["entry", "-observed_at"]),
    ]


def test_the_change_declares_the_permission_the_role_contract_grants() -> None:
    """One spelling, declared in `core/roles.py` and attached here, reconciled in both halves."""
    declared = dict(InventoryChange._meta.permissions)  # noqa: SLF001 - Django's own accessor

    assert list(declared) == [INVENTORY_CHANGE_CODENAME]
    assert declared[INVENTORY_CHANGE_CODENAME]
    assert InventoryChange._meta.app_label == INVENTORY_APP_LABEL  # noqa: SLF001 - Django's own accessor
    assert f"{InventoryChange._meta.app_label}.{INVENTORY_CHANGE_CODENAME}" == INVENTORY_CHANGE_PERMISSION  # noqa: SLF001 - Django's own accessor


# ---------------------------------------------------------------------------
# The refusals a surface branches on.
# ---------------------------------------------------------------------------


def test_the_two_narrow_refusals_are_subclasses_of_the_one_change_error() -> None:
    """A 403 and a 404 that a caller catching the parent still catches."""
    assert issubclass(InventoryChangeError, ValueError)
    assert issubclass(InventoryNotPermittedError, InventoryChangeError)
    assert issubclass(InventoryEntryMissingError, InventoryChangeError)
    assert not issubclass(InventoryNotPermittedError, InventoryEntryMissingError)
    assert not issubclass(InventoryEntryMissingError, InventoryNotPermittedError)
