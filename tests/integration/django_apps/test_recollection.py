"""`CPM-OPERATE-S08`: a collector re-run for one package, from the page.

`CPM-UJ-1`'s manual recollection had no surface: after an override or a fix a
reviewer waited a day for the sweep or asked an operator for a shell. This is the
surface -- "Collect now" on the package page -- and the service behind it, through
real requests and real rows.

**Nothing here runs a collector.** The service publishes by task name in
`transaction.on_commit`, and every case that reaches a publish patches
`current_app.send_task`. What is asserted is what was published -- which names,
with which keywords -- never what a worker did with it.

**The cases that reach the service's transaction run under `transaction=True`.**
The service refuses to run inside a transaction somebody else opened (its
publish is `on_commit` and its receipt is read straight after), and the
ordinary `django_db` mark opens exactly such a transaction around every case. So
those cases commit for real, on the terms
`tests/integration/django_apps/test_identity_resolution.py`'s rollback case
sets, and the `on_commit` callback runs where it runs in production: inside the
service call. Such a case flushes every table when it ends, the role groups the
migrations provisioned included, so each case that needs a role re-provisions
the groups through the migrations' own writer first. The cases that are refused
before the transaction -- the permission, nothing offered, the role at the
mixin -- keep the ordinary mark.

**The selection helper is asserted over every shape a selection takes**, because
that is the whole of what it knows: a queryset over `identity.Package`, a queryset
over `identity.PackageMapping`, the empty generator an undeclared vulnerability
or KEV source answers with, and the shapes it refuses. A helper that filtered
the wrong column would offer every collector to every package, or none to any,
and either reads as a healthy page.
"""

from __future__ import annotations

import inspect
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from http import HTTPStatus
from importlib import import_module
from typing import TYPE_CHECKING
from typing import Any
from typing import Final

import pytest
from celery import current_app
from django.apps import apps
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.contrib.auth.models import Group
from django.contrib.auth.models import Permission
from django.contrib.contenttypes.models import ContentType
from django.core.management.sql import emit_post_migrate_signal
from django.db import DEFAULT_DB_ALIAS
from django.db import connection
from django.db import connections
from django.db import transaction
from django.db.models.query import QuerySet
from django.test import Client
from django.urls import reverse
from django.utils.module_loading import module_has_submodule
from kombu.exceptions import OperationalError
from opentelemetry import trace

from conda_sentinel.collectors import recollection as recollection_module
from conda_sentinel.collectors import tasks as tasks_module
from conda_sentinel.collectors.advisories import declared_advisory_source
from conda_sentinel.collectors.models import PackageRecollection
from conda_sentinel.collectors.py314_verification import Py314VerificationCollector
from conda_sentinel.collectors.pypi_release import PyPIReleaseCollector
from conda_sentinel.collectors.recollection import FORCE_KWARG
from conda_sentinel.collectors.recollection import IN_FLIGHT_WINDOW_FACTOR
from conda_sentinel.collectors.recollection import PACKAGE_KWARG
from conda_sentinel.collectors.recollection import RECOLLECT_PERMISSION_MISSING
from conda_sentinel.collectors.recollection import RECOLLECTION_IN_FLIGHT_EVENT
from conda_sentinel.collectors.recollection import RECOLLECTION_NOTHING_OFFERED_EVENT
from conda_sentinel.collectors.recollection import RECOLLECTION_REFUSED_EVENT
from conda_sentinel.collectors.recollection import RECOLLECTION_UNPUBLISHED_EVENT
from conda_sentinel.collectors.recollection import RecollectionError
from conda_sentinel.collectors.recollection import RecollectionInFlightError
from conda_sentinel.collectors.recollection import RecollectionNothingOfferedError
from conda_sentinel.collectors.recollection import RecollectionNotPermittedError
from conda_sentinel.collectors.recollection import RecollectionReceipt
from conda_sentinel.collectors.recollection import can_request
from conda_sentinel.collectors.recollection import in_flight_window
from conda_sentinel.collectors.recollection import pending_recollection
from conda_sentinel.collectors.recollection import recollection_task_name
from conda_sentinel.collectors.recollection import request_recollection
from conda_sentinel.collectors.recollection import stamp
from conda_sentinel.collectors.resolve_identity import IdentityResolutionCollector
from conda_sentinel.collectors.source_release import SourceReleaseCollector
from conda_sentinel.collectors.sweep import PACKAGE_KWARG as THE_DISPATCHERS_PACKAGE_KWARG
from conda_sentinel.collectors.sweep import collection_task_name
from conda_sentinel.collectors.vulnerability import NO_ADVISORY_SOURCE_EVENT
from conda_sentinel.collectors.vulnerability import VulnerabilityCollector
from conda_sentinel.core.clock import FixedClock
from conda_sentinel.core.clock import SystemClock
from conda_sentinel.core.ledger import TRACE_ID_FORMAT
from conda_sentinel.core.ledger import runs_in_flight
from conda_sentinel.core.models import AppendOnlyError
from conda_sentinel.core.models import CollectionRun
from conda_sentinel.core.models import PackageHealth
from conda_sentinel.core.models import PolicyRun
from conda_sentinel.core.registry import SELECTION_NOT_ASKED_EVENT
from conda_sentinel.core.registry import CollectorRegistryError
from conda_sentinel.core.registry import selects
from conda_sentinel.core.registry import swept_collectors
from conda_sentinel.core.roles import PACKAGING_ENGINEER
from conda_sentinel.core.roles import RECOLLECT_APP_LABEL
from conda_sentinel.core.roles import RECOLLECT_CODENAME
from conda_sentinel.core.roles import RECOLLECT_PERMISSION
from conda_sentinel.core.roles import SECURITY_REVIEWER
from conda_sentinel.core.roles import role_group_permissions
from conda_sentinel.core.runs import RunState
from conda_sentinel.identity.models import ESTABLISHED
from conda_sentinel.identity.models import IdentityConfidence
from conda_sentinel.identity.models import MappingKind
from conda_sentinel.identity.models import Package
from conda_sentinel.identity.models import PackageMapping
from conda_sentinel.surface.views import PackageRecollectView
from conda_sentinel.surface.views import recollection_message
from django_service.users.provisioning import provision_groups
from tests.collectors import collector_class
from tests.collectors import fixture_evidence_model
from tests.factories import UserFactory

if TYPE_CHECKING:
    from collections.abc import Callable

    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from django_service.users.models import User

pytestmark = pytest.mark.integration

NOW: Final[datetime] = datetime(2026, 9, 14, 9, 30, tzinfo=UTC)
A_POLICY_VERSION: Final[str] = "cpm-operate-s08-fixture-policy"
A_PACKAGE: Final[str] = "django"
A_REPOSITORY: Final[str] = "https://github.com/django/django"

#: The two collectors whose selection is a queryset over `identity.Package` and
#: contains a package with a source repository and an unverified identity.
SELECTED_BY_PACKAGE_ROW: Final[frozenset[str]] = frozenset(
    {SourceReleaseCollector.name, IdentityResolutionCollector.name}
)

#: A run started this long ago is in flight; one started twice the window ago is a killed worker's.
RECENTLY: Final[timedelta] = timedelta(minutes=2)


def _long_ago() -> timedelta:
    """Return an age past the window, so a row that old is a killed worker's."""
    return in_flight_window() * 2


def _a_package(
    name: str = A_PACKAGE,
    *,
    repository: str = A_REPOSITORY,
    confidence: str = IdentityConfidence.INVENTORY_DERIVED,
) -> Package:
    """Return one package with a rollup row, so the detail page can open it.

    Args:
        name: Its canonical name.
        repository: Its source repository, or blank for none.
        confidence: Its identity confidence; `verified` puts it outside the
            resolver's selection.

    Returns:
        The saved package.

    """
    package = Package.objects.create(
        canonical_name=name,
        resolved_at=NOW,
        confidence=confidence,
        identity_source="inventory",
        associator_key=f"conda-forge/{name}",
        source_repository_url=repository,
    )
    run = PolicyRun.objects.create(
        policy_version=A_POLICY_VERSION, started_at=NOW, finished_at=NOW, evidence_cutoff=NOW
    )
    PackageHealth.objects.create(
        package=package,
        policy_run=run,
        computed_at=NOW,
        evidence_cutoff=NOW,
        confidence=package.confidence,
        policy_versions={"fixture": A_POLICY_VERSION},
    )
    return package


def _provisioned() -> None:
    """Put back what a `transaction=True` case's flush took: permissions, content types, role groups.

    The flush empties `auth_permission` and `django_content_type` along with
    everything else; the `post_migrate` that follows re-creates both, but it
    reads content types through a cache that still holds the ids the flush
    removed, so the permissions it writes can reference content types that no
    longer exist. The role groups come from a migration and never come back.
    So the cache is cleared, the two are re-created the way `migrate` creates
    them when the recollect permission cannot be found through its content
    type, and the groups are provisioned through the writer the grant
    migrations use, with `preserve_existing=True` so a call on an intact
    database changes nothing.
    """
    # Two repairs before `post_migrate` can do its work. The content-type cache
    # still holds the ids the flush removed, and `create_permissions` reads it,
    # so without clearing it the re-created permissions would reference content
    # types that no longer exist. And `core/0005` and `core/0011`, when
    # `tests/integration/django_apps/test_role_groups.py` runs their `forward`
    # against the live registry, leave the `identity` and `collectors` app
    # configs with `models_module = None` -- their `finally` puts back `None`
    # rather than what was there -- after which `post_migrate` skips both apps
    # for the rest of the process. `core/0013` puts back what it found;
    # the older two are not this story's to edit, so the module is restored here.
    ContentType.objects.clear_cache()
    for app_config in apps.get_app_configs():
        if app_config.models_module is None and module_has_submodule(app_config.module, "models"):
            app_config.models_module = import_module(f"{app_config.name}.models")
    if not Permission.objects.filter(codename=RECOLLECT_CODENAME, content_type__app_label=RECOLLECT_APP_LABEL).exists():
        emit_post_migrate_signal(verbosity=0, interactive=False, db=DEFAULT_DB_ALIAS)
    provision_groups(
        role_group_permissions(settings.ROLE_CONTRACT), declared_by="role_contract", preserve_existing=True
    )


def _in_role(role: str, *, username: str, **traits: Any) -> User:
    """Return a user in one role group, re-read so `has_perm` is not answered from a stale cache.

    The role groups are re-provisioned first, through the same writer the
    grant migrations use: a `transaction=True` case flushes every table when it
    ends, the migrations' rows included, and the next such case would otherwise
    find no group to put the user in. `preserve_existing=True` makes the call a
    no-op when the rows are still there.

    Args:
        role: The role slot.
        username: The account name.
        **traits: Further user fields, `is_superuser` say.

    Returns:
        The user.

    """
    _provisioned()
    user: User = UserFactory.create(username=username, idp_subject=f"urn:example:principal:{username}", **traits)
    user.groups.add(Group.objects.get(name=getattr(settings.ROLE_CONTRACT, role)))
    refreshed: User = get_user_model().objects.get(pk=user.pk)
    return refreshed


def _a_run(
    package: Package,
    *,
    started: timedelta,
    finished: bool = False,
    collector: str = "feedstock",
    before: datetime = NOW,
) -> CollectionRun:
    """Return one ledger row on a package.

    Args:
        package: The package.
        started: How long before `before` it started.
        finished: Whether it has an ending.
        collector: Which collector.
        before: The instant the age is measured back from: the stopped clock's
            `NOW` for a service case, the system clock's instant for a page case
            -- the view reads the real clock, so a row aged against a fixed
            instant in the future would read as in flight whatever its age.

    Returns:
        The saved row.

    """
    started_at = before - started
    return CollectionRun.objects.create(
        collector=collector,
        package=package,
        started_at=started_at,
        finished_at=started_at if finished else None,
        status=RunState.SUCCEEDED if finished else RunState.RUNNING,
    )


def _answer(row: PackageRecollection, *, at: datetime) -> None:
    """Finish one run per collector the press asked for, started at the press.

    Args:
        row: The press.
        at: When each run starts and finishes; at or after the row's instant.

    """
    for name in row.collectors:
        CollectionRun.objects.create(
            collector=name,
            package_id=row.package_id,
            started_at=at,
            finished_at=at,
            status=RunState.SUCCEEDED,
        )


def _events(caplog: pytest.LogCaptureFixture, event: str) -> list[str]:
    """Return the captured messages carrying one event name.

    Args:
        caplog: pytest's capture.
        event: The event.

    Returns:
        The messages.

    """
    return [record.getMessage() for record in caplog.records if event in record.getMessage()]


def _recording_send_task(
    monkeypatch: pytest.MonkeyPatch,
    *,
    refusing: frozenset[str] = frozenset(),
    raising: Callable[[str], Exception] | None = None,
) -> list[dict[str, Any]]:
    """Patch `send_task` to record every publish, refusing the named tasks.

    Args:
        monkeypatch: pytest's.
        refusing: Task names the fake broker refuses.
        raising: What it raises for them; the broker's `OperationalError` by
            default.

    Returns:
        The list the publishes land in, in order.

    """
    published: list[dict[str, Any]] = []

    def record(name: str, **kwargs: Any) -> None:
        if name in refusing:
            raise (raising or (lambda _name: OperationalError("connection refused")))(name)
        published.append({"name": name, **kwargs})

    monkeypatch.setattr(current_app, "send_task", record)
    return published


def _selection_of(selection: Any) -> type[Any]:
    """Return a fixture collector class whose selection is the given object.

    Args:
        selection: What `selectable_packages()` answers.

    Returns:
        The class, unregistered.

    """
    built = collector_class(declared_model=fixture_evidence_model(), declared_name="a-shape")
    built.selectable_packages = classmethod(lambda cls: selection)  # type: ignore[method-assign]
    return built


@pytest.fixture
def stopped_clock() -> FixedClock:
    """A clock stopped at `NOW`."""
    return FixedClock(instant=NOW)


@pytest.fixture
def a_reviewer(db: None) -> User:
    """A security reviewer, who holds the recollect permission by membership.

    Args:
        db: pytest-django's per-test transaction.

    Returns:
        The user.

    """
    user = _in_role(SECURITY_REVIEWER, username="a-reviewer")
    assert user.has_perm(RECOLLECT_PERMISSION), (
        "the reviewer group must confer the permission for these cases to mean anything"
    )
    return user


@pytest.fixture
def reviewer(a_reviewer: User) -> Client:
    """A client signed in as the reviewer."""
    client = Client()
    client.force_login(a_reviewer)
    return client


@pytest.fixture
def packaging(db: None) -> Client:
    """A client signed in as a packaging engineer, who holds no recollect permission."""
    client = Client()
    client.force_login(_in_role(PACKAGING_ENGINEER, username="a-packager"))
    return client


def _recollect_url(name: str = A_PACKAGE) -> str:
    """Return the route the button posts to."""
    return reverse("conda_sentinel:package-recollect", kwargs={"canonical_name": name})


def _detail_url(name: str = A_PACKAGE) -> str:
    """Return the detail page's path."""
    return reverse("conda_sentinel:package-detail", kwargs={"canonical_name": name})


# ---------------------------------------------------------------------------
# The declarations: the permission, the task names, the keyword contract, the window.
# ---------------------------------------------------------------------------


def test_the_permission_is_declared_on_the_audit_model_under_the_codename_roles_spells() -> None:
    """One spelling, reconciled in both halves against the model's `_meta`."""
    options = PackageRecollection._meta  # noqa: SLF001 - Django's own public-by-convention API

    assert options.app_label == RECOLLECT_APP_LABEL
    assert RECOLLECT_CODENAME in {codename for codename, _label in options.permissions}
    assert f"{options.app_label}.{RECOLLECT_CODENAME}" == RECOLLECT_PERMISSION


def test_the_task_name_and_keywords_are_the_dispatchers_own() -> None:
    """The service publishes by a name it derives without importing the dispatcher; the two must agree.

    And every swept collector's task must take both keywords, keyword-only, or a
    forced recollection would be a `TypeError` inside a worker.
    """
    assert PACKAGE_KWARG == THE_DISPATCHERS_PACKAGE_KWARG
    assert swept_collectors() != (), "the registry must hold swept collectors for the sweep below to mean anything"
    for collector in swept_collectors():
        name = recollection_task_name(collector.name)
        assert name == collection_task_name(collector.name)
        task = current_app.tasks.get(name)
        assert task is not None, name
        parameters = inspect.signature(task.run).parameters
        for keyword in (PACKAGE_KWARG, FORCE_KWARG):
            assert keyword in parameters, (name, keyword)
            assert parameters[keyword].kind is inspect.Parameter.KEYWORD_ONLY, (name, keyword)


def test_the_verification_build_is_never_swept_and_so_never_offered() -> None:
    """The block condition: its task takes no `force`, and it is not swept per package, so `selects` says no."""
    verification = tasks_module.verify_py314_build
    assert FORCE_KWARG not in inspect.signature(verification.run).parameters
    assert all(collector is not Py314VerificationCollector for collector in swept_collectors())
    assert Py314VerificationCollector.selectable_packages() is None
    assert selects(Py314VerificationCollector, 1) is False


def test_the_service_imports_neither_the_dispatcher_nor_the_tasks_nor_celery_at_module_top() -> None:
    """The request-boundary rule, and the lazy broker import, asserted at the module."""
    source = inspect.getsource(recollection_module)
    top_level_imports = [
        line for line in source.splitlines() if line.startswith(("from ", "import ")) and "celery" in line
    ]

    assert "collectors.sweep" not in source.replace("`collectors/sweep.py`", "")
    assert "collectors.tasks" not in source.replace("`collectors/tasks.py`", "")
    assert top_level_imports == [], top_level_imports
    assert "kombu" not in source.replace("`kombu`", "")


def test_the_view_is_exempt_from_atomic_requests_for_every_alias_that_declares_it() -> None:
    """The service's own transaction must be the outermost, so the receipt can name what the broker refused."""
    view = PackageRecollectView.as_view()
    exempted = getattr(view, "_non_atomic_requests", set())
    atomic = {alias for alias, config in connections.settings.items() if config.get("ATOMIC_REQUESTS")}

    assert atomic, "the component must still declare ATOMIC_REQUESTS somewhere for the exemption to mean anything"
    assert atomic - exempted == set()


def test_the_window_is_derived_from_the_task_time_limit_and_never_shorter_than_it() -> None:
    """No live run outlasts the hard limit, so a row older than the window is a killed worker's."""
    limit = timedelta(seconds=settings.CELERY_TASK_TIME_LIMIT)

    assert in_flight_window() >= limit
    assert in_flight_window() == limit * IN_FLIGHT_WINDOW_FACTOR


def test_the_window_follows_the_setting_at_call_time(settings: Any) -> None:
    """Raise the limit and the window widens with it; nothing is fixed at import."""
    before = in_flight_window()
    settings.CELERY_TASK_TIME_LIMIT = settings.CELERY_TASK_TIME_LIMIT * 3

    assert in_flight_window() == before * 3


def test_the_literal_z_the_page_and_the_refusal_print_is_honest() -> None:
    """Every panel prints `|date:"Y-m-d H:i"` followed by a literal `Z`, and so does the service's refusal."""
    assert settings.TIME_ZONE == "UTC"
    assert settings.USE_TZ is True
    assert stamp(datetime(2026, 9, 14, 12, 34, 56, tzinfo=UTC)) == "2026-09-14 12:34Z"


# ---------------------------------------------------------------------------
# The selection helper, over every shape.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_a_package_queryset_selection_is_asked_by_primary_key() -> None:
    """`source_release` selects over `identity.Package`: a repository in, a blank one out."""
    with_repository = _a_package()
    without = _a_package("attrs", repository="")

    assert selects(SourceReleaseCollector, with_repository.pk) is True
    assert selects(SourceReleaseCollector, without.pk) is False


@pytest.mark.django_db
def test_a_mapping_queryset_selection_is_asked_by_package_id_and_not_by_the_mappings_own_key() -> None:
    """`pypi_release` selects over `identity.PackageMapping`, and the helper filters its `package_id` column.

    Built so no mapping's own primary key equals the target package's: two
    mappings of other kinds on another package first, so the target's mapping
    gets a third key. A helper filtering `pk` by mistake would then answer
    `False` for the target and `True` for the other package, whose key the first
    mapping happens to share.
    """
    other = _a_package("attrs")
    target = _a_package()
    for kind in (MappingKind.FEEDSTOCK.value, MappingKind.SOURCE_REPOSITORY.value):
        PackageMapping.objects.create(package=other, kind=kind, outcome=ESTABLISHED, resolved_at=NOW)
    mapping = PackageMapping.objects.create(
        package=target,
        kind=MappingKind.RELEASE_ECOSYSTEM.value,
        outcome=ESTABLISHED,
        resolved_at=NOW,
    )
    assert mapping.pk != target.pk
    assert PackageMapping.objects.filter(pk=other.pk).exists(), "a mapping shares the other package's key"

    assert selects(PyPIReleaseCollector, target.pk) is True
    assert selects(PyPIReleaseCollector, other.pk) is False


@pytest.mark.django_db
def test_an_undeclared_source_generator_is_answered_false_without_being_iterated(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """With no advisory source declared `vulnerability` answers a generator that warns on first use; it is not used."""
    assert declared_advisory_source() is None, "the suite declares no advisory source"
    package = _a_package()

    with caplog.at_level("INFO"):
        assert selects(VulnerabilityCollector, package.pk) is False

    assert _events(caplog, NO_ADVISORY_SOURCE_EVENT) == []
    assert len(_events(caplog, SELECTION_NOT_ASKED_EVENT)) == 1


def test_an_iterable_of_keys_is_searched_and_an_iterable_of_anything_else_is_not(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A sequence of ints is a selection; a sequence of tuples or instances is not one, and says so."""
    assert selects(_selection_of([7, 9]), 7) is True
    assert selects(_selection_of((7, 9)), 8) is False
    assert selects(_selection_of(iter([7])), 7) is True, "an iterator that is not a generator is materialised"

    with caplog.at_level("WARNING"):
        assert selects(_selection_of([(7, "pk")]), 7) is False
        assert selects(_selection_of([True]), 1) is False, "a bool is not a key, whatever `int` says"

    assert len(_events(caplog, SELECTION_NOT_ASKED_EVENT)) == 2  # noqa: PLR2004 - one per refused shape


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("shape", "reason"),
    [
        (lambda: Package.objects.order_by("pk").values_list("pk", flat=True)[:5], "sliced"),
        (
            lambda: (
                Package.objects.filter(pk=1)
                .values_list("pk", flat=True)
                .union(Package.objects.filter(pk=2).values_list("pk", flat=True))
            ),
            "combined",
        ),
        (lambda: PolicyRun.objects.values_list("pk", flat=True), "neither"),
    ],
    ids=["sliced", "combined", "no-package-column"],
)
def test_a_queryset_shape_that_cannot_be_narrowed_is_refused_rather_than_answered(
    shape: Callable[[], QuerySet[Any]], reason: str
) -> None:
    """A `FieldError` out of a request is a 500; a `CollectorRegistryError` names the collector and the shape.

    Args:
        shape: Builds the selection.
        reason: A word the refusal must carry.

    """
    package = _a_package()

    with pytest.raises(CollectorRegistryError, match="a-shape") as refused:
        selects(_selection_of(shape()), package.pk)

    assert reason in str(refused.value)


@pytest.mark.django_db
@pytest.mark.parametrize("collector", swept_collectors(), ids=lambda collector: str(collector.name))
def test_every_swept_collector_answers_a_bool_for_a_real_package(collector: type[Any]) -> None:
    """The shapes the product actually declares, each answered without raising.

    Args:
        collector: One swept collector.

    """
    package = _a_package()

    assert isinstance(selects(collector, package.pk), bool)


# ---------------------------------------------------------------------------
# The service: what it refuses before its transaction.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_an_actor_without_the_permission_is_refused_logged_and_writes_nothing(
    stopped_clock: FixedClock, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The matrix's `No permission` row: refused, logged naming the actor, nothing written, nothing published."""
    package = _a_package()
    engineer = _in_role(PACKAGING_ENGINEER, username="a-packager")
    published = _recording_send_task(monkeypatch)

    with (
        caplog.at_level("WARNING", logger=recollection_module.logger.name),
        pytest.raises(RecollectionNotPermittedError, match=RECOLLECT_PERMISSION),
    ):
        request_recollection(package_id=package.pk, actor=engineer, clock=stopped_clock)

    assert PackageRecollection.objects.count() == 0
    assert published == []
    refused = _events(caplog, RECOLLECTION_REFUSED_EVENT)
    assert len(refused) == 1
    assert RECOLLECT_PERMISSION_MISSING in refused[0]
    assert "urn:example:principal:a-packager" in refused[0]


@pytest.mark.django_db
def test_the_permission_is_checked_before_the_ledger_is_read(
    stopped_clock: FixedClock, caplog: pytest.LogCaptureFixture
) -> None:
    """An unpermitted actor with an open run gets the permission refusal, and learns nothing about the ledger."""
    package = _a_package()
    _a_run(package, started=RECENTLY)
    engineer = _in_role(PACKAGING_ENGINEER, username="a-packager")

    with (
        caplog.at_level("INFO", logger=recollection_module.logger.name),
        pytest.raises(RecollectionNotPermittedError),
    ):
        request_recollection(package_id=package.pk, actor=engineer, clock=stopped_clock)

    assert _events(caplog, RECOLLECTION_IN_FLIGHT_EVENT) == []


@pytest.mark.django_db
def test_an_anonymous_actor_is_refused_and_still_logged(
    stopped_clock: FixedClock, caplog: pytest.LogCaptureFixture
) -> None:
    """`AnonymousUser` has no `idp_subject`; the refusal is recorded anyway."""
    package = _a_package()

    with (
        caplog.at_level("WARNING", logger=recollection_module.logger.name),
        pytest.raises(RecollectionNotPermittedError),
    ):
        request_recollection(package_id=package.pk, actor=AnonymousUser(), clock=stopped_clock)  # type: ignore[arg-type]

    assert len(_events(caplog, RECOLLECTION_REFUSED_EVENT)) == 1
    assert can_request(AnonymousUser()) is False


@pytest.mark.django_db
def test_a_package_no_selection_contains_is_refused_before_anything_is_written(
    a_reviewer: User, stopped_clock: FixedClock, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A verified package with no repository and no mappings: every swept selection excludes it."""
    package = _a_package(repository="", confidence=IdentityConfidence.VERIFIED)
    published = _recording_send_task(monkeypatch)

    with (
        caplog.at_level("INFO", logger=recollection_module.logger.name),
        pytest.raises(RecollectionNothingOfferedError, match="excludes it") as refused,
    ):
        request_recollection(package_id=package.pk, actor=a_reviewer, clock=stopped_clock)

    assert PackageRecollection.objects.count() == 0
    assert published == []
    assert PyPIReleaseCollector.name in str(refused.value)
    assert len(_events(caplog, RECOLLECTION_NOTHING_OFFERED_EVENT)) == 1


@pytest.mark.django_db
def test_a_registry_with_nothing_swept_is_refused_naming_that_cause(
    a_reviewer: User, stopped_clock: FixedClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other way to have nothing to ask: no swept collector registered at all."""
    package = _a_package()
    monkeypatch.setattr(recollection_module, "swept_collectors", lambda: ())

    with pytest.raises(RecollectionNothingOfferedError, match="no collector is registered"):
        request_recollection(package_id=package.pk, actor=a_reviewer, clock=stopped_clock)

    assert PackageRecollection.objects.count() == 0


@pytest.mark.django_db
def test_the_service_refuses_to_run_inside_a_callers_transaction(a_reviewer: User, stopped_clock: FixedClock) -> None:
    """The guard on the message's honesty: inside an outer block the publish would run after the receipt was read."""
    package = _a_package()

    with transaction.atomic(), pytest.raises(RecollectionError, match="inside an open transaction"):
        request_recollection(package_id=package.pk, actor=a_reviewer, clock=stopped_clock)

    assert PackageRecollection.objects.count() == 0


# ---------------------------------------------------------------------------
# The service: what it writes and publishes, with the commit real.
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_a_reviewer_gets_an_audit_row_and_one_forced_bounded_publish_per_selected_collector(
    stopped_clock: FixedClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The matrix's first row: the row names the actor; each asked task is published forced, bounded, unretried."""
    a_reviewer = _in_role(SECURITY_REVIEWER, username="a-reviewer")
    package = _a_package()
    published = _recording_send_task(monkeypatch)

    receipt = request_recollection(package_id=package.pk, actor=a_reviewer, clock=stopped_clock)

    row = PackageRecollection.objects.get()
    assert row.actor_id == a_reviewer.pk
    assert row.package_id == package.pk
    assert row.observed_at == NOW
    assert set(row.collectors) == SELECTED_BY_PACKAGE_ROW
    assert row.collectors == [c.name for c in swept_collectors() if c.name in SELECTED_BY_PACKAGE_ROW], "registry order"
    assert set(row.not_offered) == {c.name for c in swept_collectors()} - SELECTED_BY_PACKAGE_ROW
    assert receipt.recollection == row
    assert receipt.collectors == tuple(row.collectors)
    assert receipt.not_offered == tuple(row.not_offered)
    assert receipt.unpublished == []

    assert [entry["name"] for entry in published] == [recollection_task_name(name) for name in row.collectors]
    for entry in published:
        assert entry["kwargs"] == {PACKAGE_KWARG: package.pk, FORCE_KWARG: True}
        assert entry["ignore_result"] is True
        assert entry["expires"] == int(in_flight_window().total_seconds())
        assert entry["retry"] is False


@pytest.mark.django_db(transaction=True)
def test_the_publish_happens_after_the_row_has_committed_and_inside_the_service_call(
    stopped_clock: FixedClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`on_commit`, with the service's own transaction the outermost: the callback sees the committed row."""
    a_reviewer = _in_role(SECURITY_REVIEWER, username="a-reviewer")
    package = _a_package()
    seen: list[tuple[bool, bool]] = []

    def record(name: str, **kwargs: Any) -> None:
        seen.append((connection.in_atomic_block, PackageRecollection.objects.filter(package=package).exists()))

    monkeypatch.setattr(current_app, "send_task", record)

    receipt = request_recollection(package_id=package.pk, actor=a_reviewer, clock=stopped_clock)

    assert seen == [(False, True)] * len(receipt.collectors)


@pytest.mark.django_db(transaction=True)
def test_a_package_with_no_pypi_mapping_is_not_offered_the_two_pypi_collectors(
    stopped_clock: FixedClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The matrix's `Not selectable` row, named on the row rather than failed on the ledger."""
    a_reviewer = _in_role(SECURITY_REVIEWER, username="a-reviewer")
    package = _a_package()
    published = _recording_send_task(monkeypatch)

    receipt = request_recollection(package_id=package.pk, actor=a_reviewer, clock=stopped_clock)

    assert {"pypi_release", "python_readiness"} <= set(receipt.not_offered)
    assert not {"pypi_release", "python_readiness"} & set(receipt.collectors)
    assert recollection_task_name("pypi_release") not in {entry["name"] for entry in published}


@pytest.mark.django_db(transaction=True)
def test_the_audit_row_carries_the_trace_id_the_platform_would_log(
    stopped_clock: FixedClock, monkeypatch: pytest.MonkeyPatch, recorded_spans: InMemorySpanExporter
) -> None:
    """`CPM-AD-15`, inside a real recording span rather than outside every span."""
    a_reviewer = _in_role(SECURITY_REVIEWER, username="a-reviewer")
    package = _a_package()
    _recording_send_task(monkeypatch)
    tracer = trace.get_tracer(__name__)

    with tracer.start_as_current_span("recollect") as span:
        expected = format(span.get_span_context().trace_id, TRACE_ID_FORMAT)
        receipt = request_recollection(package_id=package.pk, actor=a_reviewer, clock=stopped_clock)

    assert expected != ""
    assert receipt.recollection.trace_id == expected
    assert PackageRecollection.objects.get(pk=receipt.recollection.pk).trace_id == expected
    assert "recollect" in [recorded.name for recorded in recorded_spans.get_finished_spans()]


@pytest.mark.django_db(transaction=True)
def test_the_audit_row_is_append_only(stopped_clock: FixedClock, monkeypatch: pytest.MonkeyPatch) -> None:
    """`CPM-AD-2` on the real table: a second save and a delete are both refused, and the row stands."""
    a_reviewer = _in_role(SECURITY_REVIEWER, username="a-reviewer")
    package = _a_package()
    _recording_send_task(monkeypatch)
    row = request_recollection(package_id=package.pk, actor=a_reviewer, clock=stopped_clock).recollection

    row.collectors = []
    with pytest.raises(AppendOnlyError):
        row.save()
    with pytest.raises(AppendOnlyError):
        row.delete()
    with pytest.raises(AppendOnlyError):
        PackageRecollection.objects.filter(pk=row.pk).delete()

    assert PackageRecollection.objects.get(pk=row.pk).collectors != []


# ---------------------------------------------------------------------------
# The in-flight guard: both questions, the lock, the killed-worker bound.
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_a_run_started_within_the_window_refuses_naming_it_and_writes_nothing(
    stopped_clock: FixedClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The matrix's `In flight` row, first question: an open run."""
    a_reviewer = _in_role(SECURITY_REVIEWER, username="a-reviewer")
    package = _a_package()
    _a_run(package, started=RECENTLY, collector="feedstock")
    published = _recording_send_task(monkeypatch)

    with pytest.raises(RecollectionInFlightError) as refused:
        request_recollection(package_id=package.pk, actor=a_reviewer, clock=stopped_clock)

    assert "feedstock started at" in str(refused.value)
    assert stamp(NOW - RECENTLY) in str(refused.value)
    assert PackageRecollection.objects.count() == 0
    assert published == []


@pytest.mark.django_db(transaction=True)
def test_a_press_the_ledger_has_not_answered_refuses_a_second_press_naming_the_press(
    stopped_clock: FixedClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The second question: right after a publish no run row exists yet, and the press itself is what is in flight."""
    a_reviewer = _in_role(SECURITY_REVIEWER, username="a-reviewer")
    package = _a_package()
    published = _recording_send_task(monkeypatch)
    first = request_recollection(package_id=package.pk, actor=a_reviewer, clock=stopped_clock)
    assert CollectionRun.objects.filter(package=package).count() == 0

    with pytest.raises(RecollectionInFlightError) as refused:
        request_recollection(
            package_id=package.pk, actor=a_reviewer, clock=FixedClock(instant=NOW + timedelta(seconds=1))
        )

    assert f"asked for at {stamp(NOW)}" in str(refused.value)
    assert ", ".join(first.collectors) in str(refused.value)
    assert PackageRecollection.objects.count() == 1
    assert len(published) == len(first.collectors), "nothing more was published"


@pytest.mark.django_db(transaction=True)
def test_a_press_whose_asked_collectors_have_all_finished_is_answered_and_a_new_press_is_allowed(
    stopped_clock: FixedClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Answered means every asked collector has a finished run on the package started at or after the press."""
    a_reviewer = _in_role(SECURITY_REVIEWER, username="a-reviewer")
    package = _a_package()
    _recording_send_task(monkeypatch)
    first = request_recollection(package_id=package.pk, actor=a_reviewer, clock=stopped_clock).recollection
    later = FixedClock(instant=NOW + timedelta(minutes=1))

    # A run that started *before* the press does not answer it, and one asked
    # collector still missing keeps it pending.
    _a_run(package, started=timedelta(minutes=1), finished=True, collector=first.collectors[0])
    _answer(first, at=NOW + timedelta(seconds=10))
    stale = CollectionRun.objects.filter(collector=first.collectors[-1], started_at__gte=NOW).get()
    stale.delete()
    pending = pending_recollection(package.pk, now=later.now())
    assert pending is not None
    assert pending.awaiting == (first.collectors[-1],)
    with pytest.raises(RecollectionInFlightError):
        request_recollection(package_id=package.pk, actor=a_reviewer, clock=later)

    CollectionRun.objects.create(
        collector=first.collectors[-1],
        package=package,
        started_at=NOW + timedelta(seconds=20),
        finished_at=NOW + timedelta(seconds=21),
        status=RunState.FAILED,
    )
    assert pending_recollection(package.pk, now=later.now()) is None
    second = request_recollection(package_id=package.pk, actor=a_reviewer, clock=later)

    assert second.recollection.pk != first.pk
    assert PackageRecollection.objects.count() == 2  # noqa: PLR2004 - the first press and the second


@pytest.mark.django_db(transaction=True)
def test_a_press_older_than_the_window_stops_counting_as_pending(
    stopped_clock: FixedClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A press whose tasks were never consumed is bounded by the same window its messages expire on."""
    a_reviewer = _in_role(SECURITY_REVIEWER, username="a-reviewer")
    package = _a_package()
    _recording_send_task(monkeypatch)
    request_recollection(package_id=package.pk, actor=a_reviewer, clock=stopped_clock)
    much_later = FixedClock(instant=NOW + _long_ago())

    assert pending_recollection(package.pk, now=much_later.now()) is None
    request_recollection(package_id=package.pk, actor=a_reviewer, clock=much_later)

    assert PackageRecollection.objects.count() == 2  # noqa: PLR2004 - the first press and the second


@pytest.mark.django_db(transaction=True)
def test_an_open_run_older_than_the_window_is_a_killed_workers_and_does_not_wedge_the_page(
    stopped_clock: FixedClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The matrix's `Stale open run` row: allowed, and the stale row is not what the panel lists."""
    a_reviewer = _in_role(SECURITY_REVIEWER, username="a-reviewer")
    package = _a_package()
    stale = _a_run(package, started=_long_ago())
    _a_run(package, started=RECENTLY, finished=True)
    _recording_send_task(monkeypatch)

    assert runs_in_flight(package.pk, now=NOW, window=in_flight_window()) == []
    receipt = request_recollection(package_id=package.pk, actor=a_reviewer, clock=stopped_clock)

    assert receipt.recollection.pk is not None
    assert CollectionRun.objects.get(pk=stale.pk).finished_at is None, "nothing here finalises a killed worker's row"


@pytest.mark.django_db(transaction=True)
def test_the_package_row_is_locked_before_the_in_flight_check_and_the_insert(
    stopped_clock: FixedClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two presses arriving together are serialised on the package row.

    SQLite, which the suite runs on, has no `FOR UPDATE` and Django omits the
    clause on it, so two real threads could not demonstrate the serialisation
    here; what can be shown is that the lock is taken, on the package row, inside
    the service's transaction, and before the ledger is read -- which is the
    order that makes the second of two presses see the first's row under
    PostgreSQL, where the clause is real.
    """
    a_reviewer = _in_role(SECURITY_REVIEWER, username="a-reviewer")
    package = _a_package()
    _recording_send_task(monkeypatch)
    order: list[str] = []
    original_lock = QuerySet.select_for_update
    original_read = recollection_module.runs_in_flight

    def locking(self: QuerySet[Any], *args: Any, **kwargs: Any) -> QuerySet[Any]:
        assert connection.in_atomic_block, "a lock outside a transaction is refused by Django and holds nothing"
        order.append(f"lock:{self.model._meta.label}")  # noqa: SLF001 - Django's own public-by-convention API
        return original_lock(self, *args, **kwargs)

    def reading(*args: Any, **kwargs: Any) -> list[CollectionRun]:
        order.append("read-ledger")
        return original_read(*args, **kwargs)

    monkeypatch.setattr(QuerySet, "select_for_update", locking)
    monkeypatch.setattr(recollection_module, "runs_in_flight", reading)

    request_recollection(package_id=package.pk, actor=a_reviewer, clock=stopped_clock)

    assert order == ["lock:identity.Package", "read-ledger"]


@pytest.mark.django_db(transaction=True)
def test_a_package_that_does_not_exist_is_refused_by_the_service_too(
    stopped_clock: FixedClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The surface 404s first; the service still refuses a caller that did not.

    Every real selection excludes a key that names no package, so the
    nothing-offered refusal would answer first; the selection is stubbed to
    offer everything so the lock's own refusal is what is reached.
    """
    a_reviewer = _in_role(SECURITY_REVIEWER, username="a-reviewer")
    _a_package()
    missing = int(Package.objects.order_by("-pk").values_list("pk", flat=True).first() or 0) + 1
    monkeypatch.setattr(recollection_module, "selects", lambda _collector, _package_id: True)

    with pytest.raises(RecollectionError, match="no package has primary key"):
        request_recollection(package_id=missing, actor=a_reviewer, clock=stopped_clock)

    assert PackageRecollection.objects.count() == 0


# ---------------------------------------------------------------------------
# The publish boundary.
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_a_broker_that_refuses_one_name_is_logged_and_named_on_the_receipt_and_the_others_still_go(
    stopped_clock: FixedClock, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The matrix's `Broker down` row: the audit row stands, the refusal is logged, the receipt says which."""
    a_reviewer = _in_role(SECURITY_REVIEWER, username="a-reviewer")
    package = _a_package()
    refused_name = recollection_task_name(SourceReleaseCollector.name)
    published = _recording_send_task(monkeypatch, refusing=frozenset({refused_name}))

    with caplog.at_level("ERROR", logger=recollection_module.logger.name):
        receipt = request_recollection(package_id=package.pk, actor=a_reviewer, clock=stopped_clock)

    assert PackageRecollection.objects.count() == 1
    assert receipt.unpublished == [SourceReleaseCollector.name]
    assert SourceReleaseCollector.name in receipt.collectors, "the row records what was asked for"
    assert [entry["name"] for entry in published] == [recollection_task_name(IdentityResolutionCollector.name)]
    unpublished = _events(caplog, RECOLLECTION_UNPUBLISHED_EVENT)
    assert len(unpublished) == 1
    assert SourceReleaseCollector.name in unpublished[0]
    message = recollection_message(receipt, name=A_PACKAGE)
    assert "The hand-off failed for source_release" in message
    assert IdentityResolutionCollector.name in message


@pytest.mark.django_db(transaction=True)
def test_anything_raised_on_one_name_is_caught_at_the_boundary_and_the_next_name_is_still_tried(
    stopped_clock: FixedClock, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A `RuntimeError` on the second of three names: first and third published, second named, no 500."""
    a_reviewer = _in_role(SECURITY_REVIEWER, username="a-reviewer")
    package = _a_package()
    PackageMapping.objects.create(
        package=package, kind=MappingKind.RELEASE_ECOSYSTEM.value, outcome=ESTABLISHED, resolved_at=NOW
    )
    asked = [name for name in (c.name for c in swept_collectors()) if selects_name(name, package.pk)]
    assert len(asked) >= 3, asked  # noqa: PLR2004 - a first, a second and a third
    second = recollection_task_name(asked[1])
    published = _recording_send_task(
        monkeypatch, refusing=frozenset({second}), raising=lambda name: RuntimeError(f"unexpected on {name}")
    )

    with caplog.at_level("ERROR", logger=recollection_module.logger.name):
        receipt = request_recollection(package_id=package.pk, actor=a_reviewer, clock=stopped_clock)

    assert receipt.collectors == tuple(asked)
    assert receipt.unpublished == [asked[1]]
    assert [entry["name"] for entry in published] == [recollection_task_name(n) for n in asked if n != asked[1]]
    logged = [record for record in caplog.records if RECOLLECTION_UNPUBLISHED_EVENT in record.getMessage()]
    assert len(logged) == 1
    assert logged[0].exc_info is not None, "logged with the traceback, not only the name"
    assert isinstance(logged[0].exc_info[1], RuntimeError)
    assert "unexpected on" in str(logged[0].exc_info[1])


def selects_name(name: str, package_id: int) -> bool:
    """Return whether the swept collector of one name selects a package.

    Args:
        name: The collector's declared name.
        package_id: The package.

    Returns:
        What `selects` answers for it.

    """
    return any(selects(collector, package_id) for collector in swept_collectors() if collector.name == name)


def test_the_message_names_what_was_asked_and_what_was_not() -> None:
    """The success message the page shows, composed from the receipt and nothing else."""
    receipt = RecollectionReceipt(
        recollection=PackageRecollection(),
        collectors=("feedstock", "source_release"),
        not_offered=("pypi_release",),
    )

    message = recollection_message(receipt, name=A_PACKAGE)

    assert "asked feedstock, source_release to re-run on django" in message
    assert "Not offered" in message
    assert "pypi_release" in message
    assert "hand-off" not in message


# ---------------------------------------------------------------------------
# The page.
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_a_reviewer_sees_the_button_and_a_packaging_engineer_does_not(reviewer: Client, packaging: Client) -> None:
    """The button is drawn on the service's boolean and the view's role, so the two roles see two pages."""
    _a_package()

    with_button = reviewer.get(_detail_url())
    without = packaging.get(_detail_url())

    assert with_button.status_code == HTTPStatus.OK
    assert with_button.context["can_recollect"] is True
    assert b"Collect now" in with_button.content
    assert _recollect_url().encode() in with_button.content
    assert without.status_code == HTTPStatus.OK
    assert without.context["can_recollect"] is False
    assert b"Collect now" not in without.content


@pytest.mark.django_db
def test_a_superuser_holding_only_the_engineers_role_sees_no_button_and_is_403_on_a_press() -> None:
    """`has_perm` admits a superuser; the recollect view's mixin does not admit the role; the page says the latter."""
    _a_package()
    client = Client()
    client.force_login(_in_role(PACKAGING_ENGINEER, username="a-super-packager", is_superuser=True))

    page = client.get(_detail_url())

    assert page.context["can_recollect"] is False
    assert b"Collect now" not in page.content
    assert client.post(_recollect_url()).status_code == HTTPStatus.FORBIDDEN


@pytest.mark.django_db
def test_a_get_on_the_route_is_405_and_the_detail_view_still_offers_no_write_method(reviewer: Client) -> None:
    """A `GET` that enqueued would make a prefetch spend an allowance; and the read surface keeps its 405."""
    _a_package()

    assert reviewer.get(_recollect_url()).status_code == HTTPStatus.METHOD_NOT_ALLOWED
    assert reviewer.post(_detail_url()).status_code == HTTPStatus.METHOD_NOT_ALLOWED


@pytest.mark.django_db
def test_a_packaging_engineers_post_is_403_at_the_mixin(packaging: Client, monkeypatch: pytest.MonkeyPatch) -> None:
    """The role is refused before the service is reached; nothing is written."""
    _a_package()
    published = _recording_send_task(monkeypatch)

    assert packaging.post(_recollect_url()).status_code == HTTPStatus.FORBIDDEN
    assert PackageRecollection.objects.count() == 0
    assert published == []


@pytest.mark.django_db
def test_a_reviewer_whose_group_lost_the_permission_is_403_at_the_service(monkeypatch: pytest.MonkeyPatch) -> None:
    """The service's own gate, answered as the provisioning fault it is -- and the button is not drawn either."""
    _a_package()
    # The user first: `_in_role` re-provisions the groups with their grants, so
    # the permission is taken away after it, and the user re-read afterwards.
    user = _in_role(SECURITY_REVIEWER, username="an-ungranted-reviewer")
    group = Group.objects.get(name=settings.ROLE_CONTRACT.security_reviewer)
    group.permissions.remove(
        Permission.objects.get(content_type__app_label=RECOLLECT_APP_LABEL, codename=RECOLLECT_CODENAME)
    )
    client = Client()
    client.force_login(get_user_model().objects.get(pk=user.pk))
    published = _recording_send_task(monkeypatch)

    assert client.post(_recollect_url()).status_code == HTTPStatus.FORBIDDEN
    assert PackageRecollection.objects.count() == 0
    assert published == []
    assert b"Collect now" not in client.get(_detail_url()).content


@pytest.mark.django_db
def test_an_anonymous_post_is_sent_to_sign_in_and_then_to_the_page_not_back_to_this_route() -> None:
    """A redirect to sign-in whose `next` is the page the button is on: a `POST`-only `next` would land on a 405."""
    _a_package()

    response = Client().post(_recollect_url())

    assert response.status_code == HTTPStatus.FOUND
    assert "login" in response["Location"]
    assert _detail_url() in response["Location"]
    assert "recollect" not in response["Location"]


@pytest.mark.django_db
def test_a_press_on_a_package_nothing_can_be_asked_about_is_400_with_the_refusal_shown(
    reviewer: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The view's 400 branch, produced by the nothing-offered refusal."""
    _a_package(repository="", confidence=IdentityConfidence.VERIFIED)
    published = _recording_send_task(monkeypatch)

    response = reviewer.post(_recollect_url())

    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert b"Refused" in response.content
    assert b"excludes it" in response.content
    assert PackageRecollection.objects.count() == 0
    assert published == []


@pytest.mark.django_db
def test_an_unknown_package_is_404(reviewer: Client) -> None:
    """A name resolving nothing is not a package somebody lacks a role for."""
    assert reviewer.post(_recollect_url("no-such-package")).status_code == HTTPStatus.NOT_FOUND
    assert PackageRecollection.objects.count() == 0


@pytest.mark.django_db(transaction=True)
def test_a_reviewers_press_writes_the_row_publishes_and_redirects_with_a_success_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The acceptance criterion through the page: 303 back to the detail, the row, one forced publish per collector."""
    a_reviewer = _in_role(SECURITY_REVIEWER, username="a-reviewer")
    client = Client()
    client.force_login(a_reviewer)
    package = _a_package()
    published = _recording_send_task(monkeypatch)

    response = client.post(_recollect_url())

    assert response.status_code == HTTPStatus.SEE_OTHER
    assert response["Location"] == _detail_url()
    row = PackageRecollection.objects.get()
    assert row.actor_id == a_reviewer.pk
    assert set(row.collectors) == SELECTED_BY_PACKAGE_ROW
    assert [entry["name"] for entry in published] == [recollection_task_name(name) for name in row.collectors]
    assert all(entry["kwargs"] == {PACKAGE_KWARG: package.pk, FORCE_KWARG: True} for entry in published)

    landed = client.get(_detail_url())
    body = landed.content.decode()
    assert 'class="toast toast-success"' in body
    assert "Collect now: asked" in body
    assert "Not offered" in body
    assert "pypi_release" in body
    assert "Last recollection" in body
    assert a_reviewer.username in body
    # The press is pending until its runs finish, so the button is withheld and
    # the panel says what is awaited.
    assert landed.context["in_flight"].pending is not None
    assert "still awaiting" in body
    assert "Collect now is waiting" in body
    assert f'action="{_recollect_url()}"' not in body


@pytest.mark.django_db(transaction=True)
def test_a_hand_off_that_fails_for_one_name_is_named_on_the_page_as_a_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End to end: the message the redirected page shows names the collector the broker refused, styled as a warning."""
    client = Client()
    client.force_login(_in_role(SECURITY_REVIEWER, username="a-reviewer"))
    _a_package()
    _recording_send_task(monkeypatch, refusing=frozenset({recollection_task_name(SourceReleaseCollector.name)}))

    response = client.post(_recollect_url())
    assert response.status_code == HTTPStatus.SEE_OTHER
    body = client.get(_detail_url()).content.decode()

    assert 'class="toast toast-warning"' in body
    assert "The hand-off failed for source_release" in body
    assert PackageRecollection.objects.count() == 1


@pytest.mark.django_db(transaction=True)
def test_a_second_press_while_a_run_is_open_is_409_naming_the_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """The acceptance criterion's second half: refused with 409, the page re-rendered with the refusal."""
    client = Client()
    client.force_login(_in_role(SECURITY_REVIEWER, username="a-reviewer"))
    package = _a_package()
    _a_run(package, started=RECENTLY, collector="feedstock", before=SystemClock().now())
    published = _recording_send_task(monkeypatch)

    response = client.post(_recollect_url())

    assert response.status_code == HTTPStatus.CONFLICT
    body = response.content.decode()
    assert "Refused" in body
    assert "feedstock started at" in body
    assert "In flight" in body
    assert response.context["in_flight"].runs[0].collector == "feedstock"
    assert PackageRecollection.objects.count() == 0
    assert published == []


@pytest.mark.django_db(transaction=True)
def test_a_second_press_right_after_a_publish_is_409_naming_the_press(monkeypatch: pytest.MonkeyPatch) -> None:
    """Queue latency: no run row yet, and the press itself is what the page says to wait for."""
    client = Client()
    client.force_login(_in_role(SECURITY_REVIEWER, username="a-reviewer"))
    _a_package()
    published = _recording_send_task(monkeypatch)
    assert client.post(_recollect_url()).status_code == HTTPStatus.SEE_OTHER
    first_publishes = len(published)

    response = client.post(_recollect_url())

    assert response.status_code == HTTPStatus.CONFLICT
    body = response.content.decode()
    assert "a recollection asked for at" in body
    assert "still awaiting" in body
    assert PackageRecollection.objects.count() == 1
    assert len(published) == first_publishes


@pytest.mark.django_db(transaction=True)
def test_a_press_is_allowed_when_the_only_open_run_is_older_than_the_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """A killed worker's row neither refuses the press nor appears in the in-flight panel."""
    client = Client()
    client.force_login(_in_role(SECURITY_REVIEWER, username="a-reviewer"))
    package = _a_package()
    _a_run(package, started=_long_ago(), collector="kev", before=SystemClock().now())
    _recording_send_task(monkeypatch)

    response = client.post(_recollect_url())

    assert response.status_code == HTTPStatus.SEE_OTHER
    assert PackageRecollection.objects.count() == 1
    page = client.get(_detail_url())
    assert page.context["in_flight"].runs == ()
    assert [entry.collector for entry in page.context["runs"]] == ["kev"], "the stale row is still on the ledger panel"


@pytest.mark.django_db(transaction=True)
def test_the_redirect_names_the_package_as_the_database_names_it_after_the_press(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A correction landing between the fetch and the redirect must not send the reader to a URL that now 404s."""
    client = Client()
    client.force_login(_in_role(SECURITY_REVIEWER, username="a-reviewer"))
    package = _a_package()
    _recording_send_task(monkeypatch)
    original = recollection_module.request_recollection

    def renaming(**kwargs: Any) -> RecollectionReceipt:
        receipt = original(**kwargs)
        Package.objects.filter(pk=package.pk).update(canonical_name="django-renamed")
        return receipt

    monkeypatch.setattr("conda_sentinel.surface.views.request_recollection", renaming)

    response = client.post(_recollect_url())

    assert response.status_code == HTTPStatus.SEE_OTHER
    assert response["Location"] == _detail_url("django-renamed")
