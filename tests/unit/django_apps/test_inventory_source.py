"""`CPM-OPERATE-S03`: the inventory source setting, and the adapter that reads the table.

Two claims, and both are about the seam rather than the table.

**The setting is a closed vocabulary that fails closed toward the file.** Absent
reads `watchlist`; `database` selects the table; anything else is refused at boot
naming the setting. Measured against the pure rule and against `ready()` itself,
because the rule is only worth what the hook does with it.

**The database adapter answers the CSV adapter's document.** The shipped
development watchlist parsed through `records_from`, and the same rows written
to the table and read back through `active_records`, are one list -- so
`collectors/tasks.py` carries no branch on which source is active and the
record contract refuses the same things from both (`CPM-AD-27`). That one is a
database case and lives in `tests/integration/django_apps/test_inventory_service.py`;
what is here is the model-free import and the vocabulary.
"""

from __future__ import annotations

import ast
import inspect
from typing import TYPE_CHECKING
from typing import Final

import pytest
from django.apps import apps
from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings

from conda_sentinel.collectors import inventory_source
from conda_sentinel.collectors.inventory_source import DATABASE_SOURCE
from conda_sentinel.collectors.inventory_source import DEFAULT_INVENTORY_SOURCE
from conda_sentinel.collectors.inventory_source import INVENTORY_SOURCE_SETTING
from conda_sentinel.collectors.inventory_source import INVENTORY_SOURCES
from conda_sentinel.collectors.inventory_source import WATCHLIST_SOURCE
from conda_sentinel.collectors.inventory_source import DatabaseInventoryAdapter
from conda_sentinel.collectors.inventory_source import inventory_source_fault
from conda_sentinel.collectors.tasks import InventoryAdapterError
from conda_sentinel.collectors.tasks import declare_inventory_adapter
from conda_sentinel.collectors.tasks import declared_inventory_adapter
from conda_sentinel.collectors.tasks import withdraw_inventory_adapter
from conda_sentinel.collectors.watchlist import WatchlistAdapter
from conda_sentinel.core.transport import Transport
from tests.collectors import RecordedTransport

if TYPE_CHECKING:
    from collections.abc import Iterator

COLLECTORS_APP_LABEL: Final[str] = "collectors"

#: Values the setting must refuse: the right word in the wrong case, with
#: whitespace, a third word, a non-string.
UNUSABLE: Final[tuple[object, ...]] = ("Database", " database", "file", "", None, 1, ["database"])


@pytest.fixture
def _slot_restored() -> Iterator[None]:
    """Leave the adapter slot as the case found it: empty.

    Yields:
        None. The restoration is the effect.

    """
    yield
    if declared_inventory_adapter() is not None:
        withdraw_inventory_adapter()


# ---------------------------------------------------------------------------
# The vocabulary.
# ---------------------------------------------------------------------------


def test_the_two_sources_are_the_vocabulary_and_the_default_is_the_file() -> None:
    """Fail closed toward the deployment as it is: a component that never imported keeps reading the file."""
    assert {WATCHLIST_SOURCE, DATABASE_SOURCE} == INVENTORY_SOURCES
    assert DEFAULT_INVENTORY_SOURCE == WATCHLIST_SOURCE
    assert INVENTORY_SOURCE_SETTING == "CPM_INVENTORY_SOURCE"


@pytest.mark.parametrize("declared", sorted(INVENTORY_SOURCES))
def test_each_source_is_accepted(declared: str) -> None:
    """The rule answers `None` for exactly the two values.

    Args:
        declared: One of the two.

    """
    assert inventory_source_fault(declared) is None


@pytest.mark.parametrize("declared", UNUSABLE, ids=repr)
def test_anything_else_is_refused_naming_the_setting_and_the_two_values(declared: object) -> None:
    """A typo is refused rather than guessed, and the message says what to write.

    Args:
        declared: The unusable value.

    """
    fault = inventory_source_fault(declared)

    assert fault is not None
    assert INVENTORY_SOURCE_SETTING in fault
    assert WATCHLIST_SOURCE in fault
    assert DATABASE_SOURCE in fault


def test_the_module_is_model_free_at_import() -> None:
    """It is imported from `config/settings/base.py`, before the app registry exists.

    Swept off the syntax tree: no module-scope import of `collectors.models`, and
    the one model import sits inside a function.
    """
    tree = ast.parse(inspect.getsource(inventory_source))
    top_level = [node.module for node in tree.body if isinstance(node, ast.ImportFrom) and node.module is not None]

    assert not any(module.endswith(".models") for module in top_level), top_level


def test_the_adapter_is_a_transport() -> None:
    """Structurally, as the CSV adapter is: one `fetch`, and `runtime_checkable` sees it."""
    assert isinstance(DatabaseInventoryAdapter(), Transport)


# ---------------------------------------------------------------------------
# The declaration `CollectorsConfig.ready()` makes, on the `database` branch.
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("_slot_restored")
def test_the_database_source_declares_the_database_adapter() -> None:
    """`CPM_INVENTORY_SOURCE=database` binds the table's adapter at the one seam."""
    with override_settings(**{INVENTORY_SOURCE_SETTING: DATABASE_SOURCE}):
        apps.get_app_config(COLLECTORS_APP_LABEL).ready()

    assert isinstance(declared_inventory_adapter(), DatabaseInventoryAdapter)


@pytest.mark.usefixtures("_slot_restored")
def test_the_database_declaration_is_idempotent_across_a_second_ready() -> None:
    """The branch's own guard: a second `django.setup()` must not abort boot."""
    config = apps.get_app_config(COLLECTORS_APP_LABEL)
    with override_settings(**{INVENTORY_SOURCE_SETTING: DATABASE_SOURCE}):
        config.ready()
        first = declared_inventory_adapter()
        config.ready()

    assert declared_inventory_adapter() is first
    assert isinstance(first, DatabaseInventoryAdapter)


@pytest.mark.usefixtures("_slot_restored")
def test_the_watchlist_source_declares_the_file_adapter() -> None:
    """The other branch, driven from here so the two are measured side by side."""
    with override_settings(**{INVENTORY_SOURCE_SETTING: WATCHLIST_SOURCE}):
        apps.get_app_config(COLLECTORS_APP_LABEL).ready()

    assert isinstance(declared_inventory_adapter(), WatchlistAdapter)


@pytest.mark.usefixtures("_slot_restored")
def test_a_file_adapter_already_in_the_slot_aborts_a_database_boot() -> None:
    """The guard discriminates by kind: the wrong adapter in the slot is a refusal, not a keep."""
    with override_settings(**{INVENTORY_SOURCE_SETTING: WATCHLIST_SOURCE}):
        apps.get_app_config(COLLECTORS_APP_LABEL).ready()

    with (
        override_settings(**{INVENTORY_SOURCE_SETTING: DATABASE_SOURCE}),
        pytest.raises(InventoryAdapterError),
    ):
        apps.get_app_config(COLLECTORS_APP_LABEL).ready()


@pytest.mark.usefixtures("_slot_restored")
def test_a_foreign_adapter_already_in_the_slot_aborts_a_database_boot() -> None:
    """On the file branch's terms: somebody else declared this component's source."""
    declare_inventory_adapter(RecordedTransport())

    with (
        override_settings(**{INVENTORY_SOURCE_SETTING: DATABASE_SOURCE}),
        pytest.raises(InventoryAdapterError),
    ):
        apps.get_app_config(COLLECTORS_APP_LABEL).ready()


@pytest.mark.usefixtures("_slot_restored")
@pytest.mark.parametrize("declared", ["nope", "Database", ""], ids=repr)
def test_an_unrecognised_source_refuses_boot_naming_the_setting(declared: str) -> None:
    """The matrix's `Setting bad` row: `ImproperlyConfigured`, before anything is declared.

    Args:
        declared: The unusable value.

    """
    with (
        override_settings(**{INVENTORY_SOURCE_SETTING: declared}),
        pytest.raises(ImproperlyConfigured) as refused,
    ):
        apps.get_app_config(COLLECTORS_APP_LABEL).ready()

    assert INVENTORY_SOURCE_SETTING in str(refused.value)
    assert declared_inventory_adapter() is None


@pytest.mark.usefixtures("_slot_restored")
def test_a_settings_module_declaring_no_source_refuses_by_name() -> None:
    """Absent from the *settings module* is a dropped assignment, not the default.

    The default lives in `config/settings/base.py`'s `env.str(..., default=)`;
    a settings object with no attribute at all is a module that lost the line.
    """
    from django.conf import settings  # noqa: PLC0415 - deleted through Django's own override machinery

    with override_settings():
        delattr(settings, INVENTORY_SOURCE_SETTING)

        with pytest.raises(ImproperlyConfigured) as refused:
            apps.get_app_config(COLLECTORS_APP_LABEL).ready()

    assert INVENTORY_SOURCE_SETTING in str(refused.value)


def test_the_dev_environment_declares_the_database_and_the_default_environment_declares_nothing() -> None:
    """`pixi.toml`: the local stack reads the table the seeder fills; a deployment reads the file until told."""
    import tomllib  # noqa: PLC0415 - one manifest read, local to this case

    from tests.source_scan import SRC_ROOT  # noqa: PLC0415 - as above

    manifest = tomllib.loads((SRC_ROOT.parent / "pixi.toml").read_text(encoding="utf-8"))

    assert manifest["feature"]["dev"]["activation"]["env"][INVENTORY_SOURCE_SETTING] == DATABASE_SOURCE
    assert INVENTORY_SOURCE_SETTING not in manifest.get("activation", {}).get("env", {})
