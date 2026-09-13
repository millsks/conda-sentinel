"""The shipped watchlists, and what a run does with one.

`CPM-IDENTITY-S07`'s acceptance criteria that cannot be decided before a run
exists: every row of the file this repository ships becomes a `Package` at
`unmapped` confidence carrying one `InventorySnapshot` with both required
signals, and a malformed file fails the run with nothing written.

**These are the real files, read through the real adapter, on the real task.**
`tests/unit/django_apps/test_watchlist.py` proves the column contract against
text held in memory; what is left over is whether the *shipped* data satisfies
it once every other piece is in the path -- the declared-adapter resolution, the
collector base's run recorder, resolution creating shells, and the per-package
transaction. A subset that named a package twice, or carried a count the schema
will not hold, would pass every unit case and fail here.

**The chain from the environment to a row is composed once, end to end.**
`COMPONENT_RUNTIME` selects through `is_local()`, settings assigns
`INVENTORY_WATCHLIST_PATH`, `CollectorsConfig.ready()` declares an adapter
reading it, and the task ingests. Every link has a unit case; none of them says
the links are joined, and a mis-wired one would leave the whole product reading
no inventory at all.

**A malformed *file* is a different failure path from a malformed *document*.**
`WatchlistError` is an `ImproperlyConfigured`, not a `TransportError`, so the
base's transport handling never sees it: it escapes the task, the ledger row is
finalized `failed` by the recorder on the way out, and nothing is written. That
is the path a component deployed with the shipped header-only `watchlist.csv`
takes on its very first sweep, so it is asserted rather than reasoned about.

Every test here rolls back: `@pytest.mark.django_db` wraps each in a transaction,
which is what leaves the database as found.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from typing import Final

import pytest
from django.core.exceptions import ImproperlyConfigured

from conda_sentinel.collectors.models import InventorySnapshot
from conda_sentinel.collectors.tasks import COLLECTOR_NAME
from conda_sentinel.collectors.tasks import INVENTORY_SOURCE
from conda_sentinel.collectors.tasks import declare_inventory_adapter
from conda_sentinel.collectors.tasks import declared_inventory_adapter
from conda_sentinel.collectors.tasks import ingest_inventory
from conda_sentinel.collectors.tasks import withdraw_inventory_adapter
from conda_sentinel.collectors.watchlist import OPTIONAL_COLUMNS
from conda_sentinel.collectors.watchlist import REQUIRED_COLUMNS
from conda_sentinel.collectors.watchlist import WatchlistAdapter
from conda_sentinel.collectors.watchlist import WatchlistError
from conda_sentinel.collectors.watchlist import records_from
from conda_sentinel.collectors.watchlist import watchlist_path
from conda_sentinel.core.models import CollectionRun
from conda_sentinel.core.outcomes import OutcomeState
from conda_sentinel.core.runs import RunState
from conda_sentinel.identity.models import IdentityConfidence
from conda_sentinel.identity.models import Package
from config.locality import RUNTIME_ENV_VAR
from config.locality import is_local
from tests.settings_import import BASE_SETTINGS
from tests.settings_import import evicted_settings_modules
from tests.settings_import import import_settings

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

#: The application whose `ready()` declares the adapter, by its derived label.
COLLECTORS_APP_LABEL: Final[str] = "collectors"

#: The header both shipped files carry, and the one the contract declares.
THE_HEADER: Final[str] = ",".join([*REQUIRED_COLUMNS, *OPTIONAL_COLUMNS])

#: How many packages the development subset is expected to be *of the order of*.
#: A floor rather than an exact count, because the subset's content is editable
#: and an exact number would make every edit a failing test -- but a subset that
#: had shrunk to a handful would stop being the realistic thing `CPM-AD-29` asks
#: a developer to run against.
A_REALISTIC_SUBSET: Final[int] = 50

#: How many observations one package carries after two runs. Named because
#: `PLR2004` is right about bare numbers in an assertion: `== 2` says nothing
#: about which two, and what this one means is "the second run observed it again
#: rather than creating a second package".
TWO_OBSERVATIONS: Final[int] = 2


def _shipped(*, local: bool) -> Path:
    """Return one of the two shipped watchlists.

    Args:
        local: True for the development subset, False for the production file.

    Returns:
        Its path.

    """
    return watchlist_path(local=local)


def _subset_records() -> list[dict[str, object]]:
    """Return the development subset as records, read through the adapter.

    Through `fetch` rather than through `records_from` directly, so the shipped
    file is measured against the same path a run takes.

    Returns:
        The decoded document.

    """
    decoded = json.loads(WatchlistAdapter(path=_shipped(local=True)).fetch(INVENTORY_SOURCE).body)
    assert isinstance(decoded, list)
    return decoded


def _the_run() -> CollectionRun:
    """Return the one ledger row this session's run wrote.

    Returns:
        The `CollectionRun`.

    """
    runs = list(CollectionRun.objects.all())
    assert len(runs) == 1, runs
    return runs[0]


@pytest.fixture
def _the_development_subset_declared() -> Iterator[None]:
    """Declare an adapter reading the shipped development subset, and withdraw it after.

    The subset explicitly rather than `settings.INVENTORY_WATCHLIST_PATH`: what
    settings selected depends on the developer's `COMPONENT_RUNTIME`, and a case
    whose subject changed with the environment would assert something different
    on a machine that had not declared one. That the settings module selects
    correctly is asserted on its own, further down.

    The declaration is process-global, so withdrawal is in the teardown rather
    than at the end of a body -- an adapter left behind would be the source every
    later case in the session ingests through.

    Yields:
        None. The declaration is the effect.

    """
    declare_inventory_adapter(WatchlistAdapter(path=_shipped(local=True)))
    try:
        yield
    finally:
        withdraw_inventory_adapter()


@pytest.fixture
def _slot_restored() -> Iterator[None]:
    """Leave the adapter slot empty, whatever the case put in it.

    Yields:
        None. The restoration is the effect.

    """
    yield
    if declared_inventory_adapter() is not None:
        withdraw_inventory_adapter()


@pytest.fixture
def _evicted_settings() -> Iterator[None]:
    """Let a case import a settings module fresh, and put structlog back afterwards.

    `tests/settings_import.py` owns both halves and says why the second one
    matters: `config/settings/base.py` calls `configure_structlog()` at module
    scope, so importing it reconfigures the process-wide pipeline and a module
    that restored nothing would blind `capture_logs()` in every module sorting
    after it.

    Yields:
        Control to the case, with the settings modules evicted.

    """
    yield from evicted_settings_modules()


# ---------------------------------------------------------------------------
# The two shipped files.
# ---------------------------------------------------------------------------


def test_the_production_watchlist_ships_its_header_and_no_rows() -> None:
    """Which packages are tracked is an organizational decision, not this story's.

    The file exists, carries the contract's header, and names nothing -- and the
    refusal case below is what makes shipping it that way safe rather than quiet.
    """
    assert _shipped(local=False).read_text(encoding="utf-8").strip() == THE_HEADER


def test_both_shipped_files_declare_the_same_header() -> None:
    """One contract, two files, and the production one is the template for the other.

    A subset whose header had drifted would be a file a reviewer copies rows into
    and then cannot ingest -- and only the *development* one is exercised by a
    run, so nothing else would notice.
    """
    headers = {
        path.read_text(encoding="utf-8").splitlines()[0].split(",")
        and tuple(path.read_text(encoding="utf-8").splitlines()[0].split(","))
        for path in (_shipped(local=True), _shipped(local=False))
    }

    assert headers == {tuple(THE_HEADER.split(","))}


def test_the_development_subset_is_a_realistic_inventory_the_adapter_reads() -> None:
    """`CPM-AD-29`: a developer can run the product against something real.

    Read through the adapter, so the shipped file is measured against the same
    contract every refusal is about -- a subset that violated it would otherwise
    fail for the first time inside a run.
    """
    records = _subset_records()

    assert len(records) >= A_REALISTIC_SUBSET
    assert len({str(record["source_package_key"]) for record in records}) == len(records)
    assert all(str(record["package_name"]) != str(record["source_package_key"]) for record in records)


def test_the_development_subset_names_every_package_once() -> None:
    """Two rows with different keys and one name would fail one record of every run.

    The collision is handled -- `canonical_name`'s unique constraint refuses the
    second and the run finalizes `partial` -- but a *shipped* file that provoked
    it would make a partial run the normal outcome of a clean checkout.
    """
    names = [str(record["package_name"]) for record in _subset_records()]

    assert len(set(names)) == len(names)


def test_the_development_subset_states_both_a_missing_signal_and_a_zero() -> None:
    """The shipped subset exercises the distinction the contract is built around.

    A subset in which every optional signal was populated would let a component
    that collapsed missing onto zero pass everything a developer runs locally.
    """
    records = _subset_records()

    assert any("downloads" not in record for record in records)
    assert any(record.get("downloads") == 0 or record.get("apps") == 0 for record in records)


def test_the_production_watchlist_is_refused_as_shipped() -> None:
    """A header and no rows is a watchlist awaiting review, and it says so.

    Asserted against the file this repository actually ships, not against a
    fixture: the message is what a deployed operator sees on the component's
    first sweep, and it has to name the file they are being sent to.
    """
    shipped = _shipped(local=False)

    with pytest.raises(WatchlistError) as refused:
        WatchlistAdapter(path=shipped).fetch(INVENTORY_SOURCE)

    assert str(shipped) in str(refused.value)


# ---------------------------------------------------------------------------
# A run over the shipped subset.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
@pytest.mark.usefixtures("_the_development_subset_declared")
def test_the_development_subset_ingests_into_an_empty_database() -> None:
    """Every row becomes a package and a snapshot, and the run succeeds.

    One package per row and one snapshot per package, counted against each other
    rather than against a number written here: the subset's content is editable,
    and a case pinning its length would fail on every edit while saying nothing
    about whether the ingestion worked.
    """
    expected = len(_subset_records())

    assert ingest_inventory() == RunState.SUCCEEDED.value

    packages = Package.objects.count()
    assert packages == expected
    assert InventorySnapshot.objects.count() == packages


@pytest.mark.django_db
@pytest.mark.usefixtures("_the_development_subset_declared")
def test_every_shell_the_subset_creates_is_unmapped_and_names_its_resolver() -> None:
    """`CPM-AD-25`: ingestion creates the shell and asserts no mapping.

    `unmapped` is the honest value for a package nothing has resolved yet, and
    `CPM-FR-1` is explicit that a resolution which cannot establish a mapping
    records nothing rather than a guess -- so no shipped row may arrive carrying
    a repository, a purl or a confidence above `unmapped`.
    """
    ingest_inventory()

    assert not Package.objects.exclude(confidence=IdentityConfidence.UNMAPPED).exists()
    assert not Package.objects.exclude(identity_source=COLLECTOR_NAME).exists()
    assert not Package.objects.exclude(source_repository_url="").exclude(primary_purl="").exists()


@pytest.mark.django_db
@pytest.mark.usefixtures("_the_development_subset_declared")
def test_every_snapshot_the_subset_writes_carries_both_required_signals() -> None:
    """`CPM-FR-42`, PRD Open Question 3b: together the counts are the breadth `CPM-FR-4` ranks by.

    A snapshot missing either is a package that cannot be ranked, which is what
    the model's own check constraint says and what this asserts against the rows
    the shipped file actually produced.
    """
    ingest_inventory()

    observations = InventorySnapshot.objects.filter(state=OutcomeState.OK.value)
    assert observations.count() == InventorySnapshot.objects.count()
    assert not observations.filter(internal_component_count__isnull=True).exists()
    assert not observations.filter(internal_lob_count__isnull=True).exists()


@pytest.mark.django_db
@pytest.mark.usefixtures("_the_development_subset_declared")
def test_a_packages_canonical_name_is_the_rows_name_and_its_key_is_the_rows_key() -> None:
    """The two columns carry different values, which is what this story changed.

    While the source key was written into both, a lookup on either matched and
    nothing could tell the correctable name from the stable key -- which is what
    hid the trap `CPM-IDENTITY-S02` closed, by making the pair unique and by
    never writing it while correcting a name.
    """
    ingest_inventory()

    assert not Package.objects.filter(canonical_name__startswith="conda-forge/").exists()
    # Every key names its origin as a prefix: `conda-forge/` for what conda-forge
    # ships, `internal/` for the two names that are not packages at all
    # (`CPM-OPERATE-S03` added those rows so the demo roster is a subset of the file).
    assert not Package.objects.exclude(associator_key__contains="/").exists()
    for package in Package.objects.all():
        assert package.associator_key.rsplit("/", 1)[-1] == package.canonical_name


@pytest.mark.django_db
@pytest.mark.usefixtures("_the_development_subset_declared")
def test_a_second_run_over_the_same_subset_adds_no_second_package() -> None:
    """The daily sweep, twice, which is what the source actually does.

    Resolution is get-or-create on `(identity_source, associator_key)`, so the
    second run finds the rows the first made. Running it twice is also what
    proves the shipped subset carries no two rows that resolve to one package.
    """
    ingest_inventory()
    packages = Package.objects.count()

    assert ingest_inventory() == RunState.SUCCEEDED.value
    assert Package.objects.count() == packages
    assert InventorySnapshot.objects.count() == packages * 2


# ---------------------------------------------------------------------------
# A malformed file fails the run and writes nothing.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
@pytest.mark.usefixtures("_slot_restored")
def test_a_watchlist_awaiting_review_fails_the_run_and_writes_nothing() -> None:
    """The path a component deployed before its watchlist is reviewed actually takes.

    The shipped `watchlist.csv`, through the declared adapter, through the task.
    `WatchlistError` is an `ImproperlyConfigured` and not a `TransportError`, so
    the base's transport handling never sees it: it escapes the task, and the run
    recorder finalizes the ledger row `failed` on the way out. Nothing is written,
    which is the whole of "no run partially ingests a malformed source" for a
    source that could not produce a document at all.
    """
    declare_inventory_adapter(WatchlistAdapter(path=_shipped(local=False)))

    with pytest.raises(ImproperlyConfigured):
        ingest_inventory()

    assert Package.objects.count() == 0
    assert InventorySnapshot.objects.count() == 0
    assert _the_run().status == RunState.FAILED.value


@pytest.mark.django_db
@pytest.mark.usefixtures("_slot_restored")
def test_a_missing_watchlist_fails_the_run_and_writes_nothing(tmp_path: Path) -> None:
    """The other file-level refusal, on the same path and to the same effect.

    A run that treated an unreadable file as an empty inventory would record
    every package the source has ever named as absent -- permanently, in a log
    nothing may correct.
    """
    declare_inventory_adapter(WatchlistAdapter(path=tmp_path / "never-written.csv"))

    with pytest.raises(ImproperlyConfigured):
        ingest_inventory()

    assert Package.objects.count() == 0
    assert InventorySnapshot.objects.count() == 0
    assert _the_run().status == RunState.FAILED.value


# ---------------------------------------------------------------------------
# Two rows, one name: the collision resolution is what catches.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
@pytest.mark.usefixtures("_slot_restored")
def test_two_rows_sharing_a_name_make_two_packages_worth_of_attempt_and_a_partial_run(
    tmp_path: Path,
) -> None:
    """Distinct keys and one name: one package, one failure, and a `partial` run.

    The failure this story could have introduced and did not. `package_name`
    carries no uniqueness guarantee from any layer -- the adapter and the record
    contract both fold on the *key* -- so a shell looked up by name would collapse
    the two rows onto one `Package`, discard the second key silently, hang both
    keys' evidence off the first shell, and report `SUCCEEDED`.

    Keyed on `(identity_source, associator_key)` the two rows are two lookups,
    both reach the create, and `canonical_name`'s unique constraint refuses the
    second. The record fails, the sweep carries on, and the run says `partial` --
    which is a collision an operator can see rather than one nothing reports.
    """
    watchlist = tmp_path / "collision.csv"
    watchlist.write_text(
        f"{THE_HEADER}\nconda-forge/numpy,numpy,12,3,,,,\ninternal/numpy,numpy,4,2,,,,\n",
        encoding="utf-8",
    )
    declare_inventory_adapter(WatchlistAdapter(path=watchlist))

    assert ingest_inventory() == RunState.PARTIAL.value

    assert Package.objects.count() == 1
    assert InventorySnapshot.objects.count() == 1
    survivor = Package.objects.get()
    assert survivor.canonical_name == "numpy"
    assert survivor.associator_key == "conda-forge/numpy"


@pytest.mark.django_db
@pytest.mark.usefixtures("_slot_restored")
def test_a_renamed_package_keeps_its_row_and_its_original_canonical_name(tmp_path: Path) -> None:
    """The other half of keying on the key: a name change finds the same package.

    `CPM-AD-25` -- ingestion asserts nothing -- so `canonical_name` is create-only
    and the second run does *not* rewrite it. That is deliberate: the name is the
    correctable one, `CPM-IDENTITY-S02` and a reviewer are what correct it, and a
    sweep that overwrote their work with whatever the source is calling the
    package this morning would undo a correction nightly and silently.
    """
    watchlist = tmp_path / "renamed.csv"
    watchlist.write_text(f"{THE_HEADER}\nconda-forge/numpy,numpy,12,3,,,,\n", encoding="utf-8")
    declare_inventory_adapter(WatchlistAdapter(path=watchlist))
    ingest_inventory()
    first = Package.objects.get()

    watchlist.write_text(f"{THE_HEADER}\nconda-forge/numpy,numpy2,12,3,,,,\n", encoding="utf-8")

    assert ingest_inventory() == RunState.SUCCEEDED.value

    assert Package.objects.count() == 1
    unchanged = Package.objects.get()
    assert unchanged.pk == first.pk
    assert unchanged.canonical_name == "numpy"
    assert InventorySnapshot.objects.filter(package_id=unchanged.pk).count() == TWO_OBSERVATIONS


# ---------------------------------------------------------------------------
# The whole chain: environment, settings, declaration, run.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
@pytest.mark.usefixtures("_evicted_settings", "_slot_restored")
def test_a_local_runtime_ingests_the_development_subset_end_to_end(monkeypatch: pytest.MonkeyPatch) -> None:
    """`COMPONENT_RUNTIME` to rows, with every link real and none of them stubbed.

    The settings module is imported *fresh* against the environment this case
    declares, which is what makes the middle of the chain genuine: `is_local()`
    reads `os.environ` at call time while the assignment freezes at import, so a
    case that only set the variable would be asserting against the value the
    session started with.

    Each link has a case of its own and none of them says the links are joined.
    A hook that computed its own path, or a settings module that assigned the
    wrong end of the selection, would pass every one of those and leave the
    product reading no inventory at all.
    """
    settings_module = import_settings(
        BASE_SETTINGS,
        monkeypatch,
        environment={},
        runtime_variable=RUNTIME_ENV_VAR,
        runtime="local",
    )

    assert is_local()
    selected = settings_module.INVENTORY_WATCHLIST_PATH
    assert selected == _shipped(local=True)

    declare_inventory_adapter(WatchlistAdapter(path=selected))

    assert ingest_inventory() == RunState.SUCCEEDED.value
    assert Package.objects.count() == len(_subset_records())


@pytest.mark.usefixtures("_evicted_settings")
def test_a_deployed_runtime_selects_the_production_watchlist_through_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same chain's fail-closed half, as far as the file it would have read.

    Deliberately not run to completion: what a deployed component reads is the
    header-only production file, and the run that follows is the refusal asserted
    above. What is asserted here is the link this case exists for -- that a
    settings module imported with *no* locality declaration selects the
    production watchlist and not the subset.
    """
    settings_module = import_settings(
        BASE_SETTINGS,
        monkeypatch,
        environment={},
        runtime_variable=RUNTIME_ENV_VAR,
        runtime=None,
    )

    assert not is_local()
    assert _shipped(local=False) == settings_module.INVENTORY_WATCHLIST_PATH


def test_the_subset_the_chain_selects_is_the_one_the_parser_accepts() -> None:
    """The shipped file, parsed by the pure function, with no adapter in the way.

    An anti-vacuity guard for everything above: every case in this module reaches
    the subset through the adapter, so a `fetch` that had started answering a
    fixed document would satisfy all of them.
    """
    text = _shipped(local=True).read_text(encoding="utf-8")

    assert records_from(text, watchlist=_shipped(local=True)) == _subset_records()
