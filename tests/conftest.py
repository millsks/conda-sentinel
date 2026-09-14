"""Fixtures shared by the unit suite and the integration suite.

`no_network` lives here rather than in `tests/unit/conftest.py` because both
halves of FR-23 need it: the boot assertions are unit tests and the persona
seeding assertion is an integration test against a real database.

`valid_deployed_settings_namespace` is here for the same reason. Every module
that constructs a stage-1 or stage-2 case needs a settings namespace that every
condition accepts, and they are split across both suites -- `tests/unit/startup/`
holds most of them and `tests/integration/startup/` holds the ones that need a
real connection -- so a `tests/unit/` home would have to be copied. One builder
is what keeps "valid" meaning the same thing everywhere: the moment a story adds
a condition, the namespace that satisfies it is edited once and every caller
inherits the change rather than a fixture per module drifting until some of them
assert over a namespace that refuses.

The callers are deliberately not listed here. A roster in a docstring goes stale
without anything failing -- this one already had, naming three modules when there
were seven -- and `grep valid_deployed_settings_namespace` answers the question
accurately at the moment it is asked.

`pytest_collection_modifyitems` below is the collection side of FR-16's coverage
audit. It reads the `forbidden_state` markers off collected items -- pytest's own
machinery, never a source scan -- and writes them out when a run is asked to
report them.

`subprocess_env` is here for the shared-home reason the two URLconf builders are.
It began in `tests/integration/startup/conftest.py`, which is where the boot
probes live; `tests/unit/startup/test_refusal_coverage_audit.py` then needed the
same environment for its collection child, and a unit module importing an
integration package's conftest is the cross-import that file exists to prevent.
One builder is what keeps "the environment a child probe runs in" meaning the
same thing on both sides.
"""

from __future__ import annotations

import json
import os
import socket
import sys
from collections import defaultdict
from contextlib import contextmanager
from importlib.util import module_from_spec
from importlib.util import spec_from_file_location
from itertools import count
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING
from typing import Any
from typing import Final
from typing import NoReturn

import pytest
import structlog
from django.conf import settings
from django.contrib import admin
from django.db import connections
from django.test import override_settings
from django.urls import include
from django.urls import path

from conda_sentinel.collectors.tasks import declared_inventory_adapter
from conda_sentinel.collectors.tasks import withdraw_inventory_adapter
from conda_sentinel.core import collection as core_collection
from conda_sentinel.identity import services as identity_services
from config import urls as config_urls
from config.authorization.claims import ClaimsContract
from config.locality import RUNTIME_ENV_VAR
from config.startup.stage_one import PRODUCTION_SETTINGS_MODULE
from tests.factories import UserFactory

if TYPE_CHECKING:
    from collections.abc import Iterator

    from django.db.backends.base.base import BaseDatabaseWrapper
    from django.urls import URLPattern
    from django.urls import URLResolver

    from django_service.users.models import User


#: The marker a refusal test claims a forbidden state with, registered in
#: `pyproject.toml [tool.pytest.ini_options] markers`. The name lives here rather
#: than as a literal in each reader so the audit and the collector cannot spell
#: it differently.
FORBIDDEN_STATE_MARKER: Final = "forbidden_state"

#: Names the file `pytest_collection_modifyitems` writes the claim report to.
#: Absent in an ordinary run, in which the hook records the claims and does
#: nothing with them; set by `tests/unit/startup/test_refusal_coverage_audit.py`
#: when it drives a collection-only child.
#:
#: A child process rather than this session's own collection, and that is the
#: whole reason the variable exists. `pixi run test` collects `tests/unit/` alone
#: and `pixi run test-integration` collects `tests/integration/` alone, so an
#: audit reading whatever the running session happened to collect would report
#: every integration-claimed state as unclaimed under the first and every
#: unit-claimed state as unclaimed under the second. The audit asks for the whole
#: suite, once, whatever invoked it.
FORBIDDEN_STATE_REPORT_ENV_VAR: Final = "FORBIDDEN_STATE_CLAIM_REPORT"

#: The two top-level keys of the claim report. Claims that a disabling marker
#: would stop from ever executing are reported separately rather than dropped: an
#: audit told only "this state is unclaimed" sends its reader looking for a
#: missing marker, when the marker is there and the test is skipped.
FORBIDDEN_STATE_CLAIMS_KEY: Final = "claims"
FORBIDDEN_STATE_DISABLED_KEY: Final = "disabled"

#: The markers that stop a test from being a claim. A test carrying any of them
#: may never execute -- `skipif` and `xfail` conditionally, `skip`
#: unconditionally -- and a state whose only claim is a test that never runs is
#: not tested at all, which is precisely the reassurance FR-16 exists to
#: withhold. `xfail` is included even in its non-strict form: a test expected to
#: fail asserts nothing about a refusal firing.
DISABLING_MARKERS: Final[tuple[str, ...]] = ("skip", "skipif", "xfail")


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Record which test claims which forbidden state, and report it when asked.

    Collected through `item.iter_markers` rather than by parsing source text
    (FR-16, Story 4.5 Task 2): a marker applied through `pytest.param(...,
    marks=...)`, through a class-level `pytestmark`, or by another hook is
    invisible to a grep and is exactly as valid a claim.

    `trylast` for the same reason: another `pytest_collection_modifyitems` -- a
    plugin's or a package conftest's -- may apply markers of its own, and a hook
    that ran before it would report the claims that existed at the time rather
    than the claims the session actually carries. Running last is what makes the
    sentence above true of hook-applied markers as well.

    Args:
        items: Every item this session collected.

    """
    claimed: defaultdict[str, set[str]] = defaultdict(set)
    disabled: defaultdict[str, set[str]] = defaultdict(set)
    for item in items:
        skipped = any(item.get_closest_marker(name) is not None for name in DISABLING_MARKERS)
        destination = disabled if skipped else claimed
        for marker in item.iter_markers(FORBIDDEN_STATE_MARKER):
            for state_id in marker.args:
                destination[str(state_id)].add(item.nodeid)

    report = os.environ.get(FORBIDDEN_STATE_REPORT_ENV_VAR)
    if report:
        payload = {
            FORBIDDEN_STATE_CLAIMS_KEY: {state_id: sorted(nodeids) for state_id, nodeids in claimed.items()},
            FORBIDDEN_STATE_DISABLED_KEY: {state_id: sorted(nodeids) for state_id, nodeids in disabled.items()},
        }
        destination_path = Path(report)
        # The variable names a path rather than a directory pytest owns, and a
        # parent that does not exist would raise out of collection -- taking the
        # whole session with it rather than failing the one case that asked for
        # the report.
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        destination_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


#: The environment names `subprocess_env` drops, and why each one goes.
#:
#: `PYTHONPATH` because a child must resolve `config` through the editable
#: install exactly as a deployed process does (AD-7). `DJANGO_SETTINGS_MODULE`
#: because it is the variable under test: pytest-django set it to
#: `config.settings.test` in this process from the `--ds` in `addopts`, and a
#: child that inherited it would never exercise the `os.environ.setdefault` in
#: `config/asgi.py` that a server process relies on.
#:
#: The database-selection variables go for a reason that cost a day to find: a
#: probe supplies its own throwaway database and must boot the same way whatever
#: the developer's shell holds. Inherited, a `DATABASE_URL` pointing at
#: PostgreSQL makes `base.py` select the postgres engine, and a probe's sqlite
#: *filename* is then handed to it as a database NAME -- which fails on
#: PostgreSQL's 63-character limit rather than on anything the test is about. The
#: suite passed only on machines with no database configured.
#:
#: The `COV_CORE_*` set is pytest-cov's subprocess activation, read by the
#: `pytest-cov.pth` file in site-packages. Left in place, every child probe
#: writes a `.coverage.<host>.<pid>.<random>` file into its working directory --
#: the repository root for most of them -- and measures a process whose coverage
#: nothing combines. Dropping them is what keeps a probe from leaving artifacts
#: behind, which is a claim several of these modules make in their docstrings.
SUBPROCESS_ENV_DROPPED: Final[tuple[str, ...]] = (
    "PYTHONPATH",
    "DJANGO_SETTINGS_MODULE",
    "DATABASE_URL",
    "POSTGRES_DB",
    "POSTGRES_USER",
    "POSTGRES_PASSWORD",
    "POSTGRES_HOST",
    "POSTGRES_PORT",
    "COV_CORE_SOURCE",
    "COV_CORE_CONFIG",
    "COV_CORE_DATAFILE",
    "COV_CORE_CONTEXT",
)


def subprocess_env() -> dict[str, str]:
    """Return an environment in which only the editable install can resolve `src/`.

    `PYTHONSAFEPATH` keeps the interpreter from prepending the working directory
    to `sys.path` for `-c`, and everything in `SUBPROCESS_ENV_DROPPED` is
    removed outright, for the reasons recorded there.

    `COMPONENT_RUNTIME` is inherited, so a child boots local exactly as a
    developer's `pixi run web` would -- which is why the sentinel is written
    before the locality check rather than after it. A probe that needs a deployed
    run drops the variable *after* boot rather than before it, so that the boot
    itself is still the one a developer performs.

    Returns:
        A copy of the current environment with those adjustments.

    """
    env = dict(os.environ)
    for name in SUBPROCESS_ENV_DROPPED:
        env.pop(name, None)
    env["PYTHONSAFEPATH"] = "1"
    return env


@pytest.fixture(autouse=True)
def _media_storage(settings, tmpdir) -> None:
    settings.MEDIA_ROOT = tmpdir.strpath


@pytest.fixture
def user(db) -> User:
    return UserFactory.create()


#: The issuer the valid namespace below is anchored to. `.invalid` is reserved by
#: RFC 2606 and resolves nowhere, which is the point: stage 1's trust-anchor
#: condition is syntactic (AD-23), so nothing ever fetches this and a value that
#: could be fetched would only invite a test that did.
DEPLOYED_OIDC_ISSUER: Final = "https://idp.example.invalid/realms/component"

#: The one backend a deployed component keeps. It is a `ModelBackend` *subclass*,
#: which is why refusal condition 2a compares identity rather than ancestry --
#: see `config/startup/stage_one.py`, `_MODEL_BACKEND`.
DEPLOYED_AUTHENTICATION_BACKEND: Final = "allauth.account.auth_backends.AuthenticationBackend"


def valid_deployed_settings_namespace(name: str = PRODUCTION_SETTINGS_MODULE) -> ModuleType:
    """Build a settings namespace that every stage-1 condition accepts.

    A real `ModuleType` rather than a `SimpleNamespace`, because FR-12's escape
    route reads `__name__` off the object that is executing and a namespace with
    no `__name__` would pass that condition for the wrong reason.

    What *valid* means here, condition by condition: the settings module is the
    deployed one; every configured database names a real backend; no local
    credential path is live -- allauth's backend alone, no declared login method,
    the admin forced through allauth, and neither half of the static-token
    surface; the issuer is set and the JWKS location derives from it, left unset
    so the conventional derivation is what is exercised; and the claims contract
    carries all four names.

    One thing this cannot do for its caller: `OTEL_SDK_DISABLED` is an
    environment variable rather than a setting, so a caller running with it set
    is refused by condition 3 whatever this namespace holds. Every caller
    deletes it with `monkeypatch.delenv(..., raising=False)`.

    Args:
        name: The settings module this namespace stands in for. Defaults to the
            deployed one; the local one is what FR-12's escape route refuses.

    Returns:
        A module object carrying every name stage 1 reads, each set to a value
        no condition objects to. Callers mutate one name at a time to construct
        exactly one forbidden state.

    """
    namespace = ModuleType(name)
    namespace.DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": "component",
            "HOST": "postgres",
            "PORT": "5432",
        },
    }
    namespace.AUTHENTICATION_BACKENDS = [DEPLOYED_AUTHENTICATION_BACKEND]
    namespace.ACCOUNT_LOGIN_METHODS = set()
    namespace.DJANGO_ADMIN_FORCE_ALLAUTH = True
    namespace.INSTALLED_APPS = [
        "django.contrib.auth",
        "allauth",
        "allauth.account",
        "allauth.socialaccount",
        "allauth.socialaccount.providers.openid_connect",
        "rest_framework",
        "django_service.users",
    ]
    namespace.REST_FRAMEWORK = {
        "DEFAULT_AUTHENTICATION_CLASSES": (
            "config.authorization.authentication.OIDCBearerAuthentication",
            "rest_framework.authentication.SessionAuthentication",
        ),
    }
    namespace.OIDC_ISSUER = DEPLOYED_OIDC_ISSUER
    namespace.OIDC_JWKS_URL = ""
    namespace.CLAIMS_CONTRACT = ClaimsContract(
        identity_key_claim="sub",
        group_claim="groups",
        staff_group="platform-staff",
        superuser_group="platform-superuser",
    )
    return namespace


#: Makes every throwaway URL configuration a module name nothing has used
#: before.
#:
#: **Not because a reused name would go stale.** `override_settings` sends
#: `setting_changed`, and `django/test/signals.py`'s receiver calls
#: `clear_url_caches()`, which empties `_get_cached_resolver` -- so a second
#: configuration installed under a name an earlier block used would still be
#: resolved freshly. The docstring below says exactly that, and a comment here
#: claiming the opposite would leave two adjacent statements of the same
#: mechanic disagreeing.
#:
#: What the serial buys is *identity*. The module is registered in `sys.modules`
#: and unregistered again on the way out, so two installations that overlap --
#: one nested inside another, or one leaked by a failed teardown -- would
#: clobber each other's entry under a shared name, and the first to exit would
#: unregister the other's configuration. A distinct name per use also makes a
#: route tree attributable: a failure naming `tests._throwaway_urlconf_7` points
#: at one block rather than at whichever block happened to run last.
_URLCONF_SERIAL: Final = count()


@contextmanager
def temporary_root_urlconf(*patterns: URLPattern | URLResolver) -> Iterator[str]:
    """Install a throwaway root URL configuration for the duration of a block.

    A real module object registered in `sys.modules`, because that is what
    `ROOT_URLCONF` names and what `include()` resolves; a list of patterns
    handed to a resolver directly would not exercise the import Django performs.

    `override_settings` is what makes it live: it sends `setting_changed`, whose
    receiver clears Django's URL caches on the way in and again on the way out,
    so neither the block nor anything after it sees a resolver built from the
    other configuration.

    Args:
        *patterns: The `urlpatterns` of the configuration to install.

    Yields:
        The dotted name the configuration is registered under, for a caller that
        wants to pass it explicitly rather than rely on `ROOT_URLCONF`.

    """
    name = f"tests._throwaway_urlconf_{next(_URLCONF_SERIAL)}"
    module = ModuleType(name)
    module.urlpatterns = list(patterns)
    sys.modules[name] = module
    try:
        with override_settings(ROOT_URLCONF=name):
            yield name
    finally:
        # `pop(..., None)` rather than `del`: the block under test may itself
        # remove or replace the entry, and a `KeyError` raised out of teardown
        # would replace the case's real result with a failure about cleanup.
        sys.modules.pop(name, None)


def deployed_url_patterns() -> list[URLPattern | URLResolver]:
    """Build the routes a correctly configured deployed component serves.

    The admin and the identity provider's own sign-in flow, and nothing else.
    Both stage-2 URLconf conditions have to pass over this: without a
    configuration they accept, a predicate that refused every route would satisfy
    every refusal assertion in the suite and nobody would notice.

    `accounts/` is deliberately the real allauth mount rather than a stand-in.
    AD-21's worked evasion is a local sign-in route hidden under that prefix, so
    a negative case that did not actually route allauth there would not be the
    negative case the evasion tests are measured against.

    Returns:
        A fresh list on every call, safe to extend with the route a caller is
        constructing a forbidden state out of.

    """
    return [
        path("admin/", admin.site.urls),
        path("accounts/", include("allauth.urls")),
    ]


@contextmanager
def deployed_component_urlconf() -> Iterator[str]:
    """Install this component's **own** `config/urls.py`, as a deployed run builds it.

    `deployed_url_patterns` above is a two-route stand-in, and a stand-in cannot
    answer the question a refusal contract most needs answered: does the URL
    configuration that actually ships pass its own conditions? The real tree
    routes `config.api_router`'s DRF router, drf-spectacular's schema and Swagger
    views, `django_service.users.urls`, allauth and the admin -- several hundred
    view candidates, none of which any synthetic configuration exercises. A
    forbidden route added under `api/` would be caught by nothing in the suite
    without this.

    **Why `config/urls.py` is executed again rather than installed as it stands.**
    The whole suite runs in the `dev` pixi environment, which declares
    `COMPONENT_RUNTIME=local`, so the `config.urls` this process imported at
    startup has the local persona sign-in route mounted -- and stage 2 refuses
    that route, correctly, the moment a case declares the run deployed. What is
    wanted is the same file's *deployed* branch. Re-executing the module source
    under a popped `COMPONENT_RUNTIME` produces exactly that: every `include()`
    is by dotted string and resolves through `sys.modules`, so only
    `config/urls.py` itself runs again and every configuration it includes is the
    real, already-imported one.

    `importlib.reload` is what this deliberately is not. Reloading would rebind
    the attributes of the shared `config.urls` module object that every later
    test in the session resolves through -- the mutation `config/urls.py`'s own
    `local_signin_urlpatterns` docstring exists to avoid.

    Yields:
        The dotted name the deployed-shaped configuration is registered under,
        live as `ROOT_URLCONF` for the duration of the block.

    """
    name = f"tests._deployed_component_urlconf_{next(_URLCONF_SERIAL)}"
    source = Path(str(config_urls.__file__))
    spec = spec_from_file_location(name, source)
    if spec is None or spec.loader is None:  # pragma: no cover - a source file Python cannot load
        message = f"config/urls.py could not be loaded from {source}"
        raise RuntimeError(message)
    module = module_from_spec(spec)

    declared = os.environ.pop(RUNTIME_ENV_VAR, None)
    try:
        sys.modules[name] = module
        spec.loader.exec_module(module)
    finally:
        if declared is not None:
            os.environ[RUNTIME_ENV_VAR] = declared

    try:
        with override_settings(ROOT_URLCONF=name):
            yield name
    finally:
        sys.modules.pop(name, None)


#: The engine a never-migrated alias runs, and only the engine. sqlite, because
#: the fault being constructed is "this schema was never migrated" and an empty
#: sqlite database is the purest form of it: nothing to provision, no server, and
#: `django_migrations` genuinely absent rather than emptied.
#:
#: The `NAME` is the caller's, which is why it is not folded in here. The
#: context manager below pairs the engine with `:memory:`; the sqlite condition
#: in `tests/integration/startup/test_stage_two_database_conditions.py` pairs it
#: with a path that is never opened, because the refusal it drives reads
#: `DATABASES` and never connects.
NEVER_MIGRATED_ENGINE: Final = "django.db.backends.sqlite3"


@contextmanager
def never_migrated_database_alias(alias: str) -> Iterator[None]:
    """Configure an additional, never-migrated database alias for the duration of a block.

    Shared for the reason the two URLconf builders above are shared. Two modules
    construct this state -- `tests/integration/startup/` drives FR-16's condition
    7 with it, and `tests/integration/test_release_stage.py` drives AD-22's
    release-stage contract with it -- and the setup has a silent failure mode
    that a second copy would eventually get wrong.

    That failure mode: `override_settings(DATABASES=...)` alone is not enough.
    `django.db.connections` caches the mapping it was configured with, and no
    `setting_changed` receiver refreshes it, so the override makes
    `settings.DATABASES` name the extra alias while `connections[alias]` still
    raises `ConnectionDoesNotExist` -- a state in which the subject cannot be
    exercised at all, and one that presents as a passing test rather than as an
    error. The handler is reconfigured through its own public
    `configure_settings` and restored to the exact mapping it held before.

    The alias is never `default`. A condition that read `DATABASES["default"]`
    and stopped would pass a case whose fault sat there, and AD-9's whole point
    is that a schema nobody migrated is the same defect under any alias.

    `:memory:` for the same reason the engine is sqlite: an in-memory database is
    gone the moment its connection closes, so a case built on this leaves no
    artifact anywhere, whatever it did inside the block.

    Every mutation of global state happens *inside* the `try`, and the restore
    runs whatever happens to the teardown of the connection itself. Both
    orderings matter and neither is cosmetic: `connections[alias]` raising with
    the replaced handler already installed would leak that handler into every
    later test in the session, and a close that raised would otherwise skip the
    restore entirely -- each producing a suite whose remaining failures are
    reported against the tests that inherit the damage rather than against this
    one. That is the same silent-failure shape the paragraph above describes, and
    a fixture that warns about one while carrying the other is not much of a
    warning.

    Args:
        alias: The alias to configure. Must not be `default`.

    Yields:
        None. The configuration is the effect.

    """
    if alias == "default":
        message = f"{alias!r} is the alias every caller already has; the fault belongs on a second one (AD-9)"
        raise ValueError(message)

    databases = {**settings.DATABASES, alias: {"ENGINE": NEVER_MIGRATED_ENGINE, "NAME": ":memory:"}}
    with override_settings(DATABASES=databases):
        restored = connections.__dict__.get("settings")
        configured: BaseDatabaseWrapper | None = None
        try:
            connections.settings = connections.configure_settings(dict(databases))
            # Materialized here rather than on first use so that teardown always
            # has a connection to close, whether or not the block opened one.
            configured = connections[alias]
            yield
        finally:
            try:
                if configured is not None:
                    configured.close()
                    del connections[alias]
            finally:
                connections.__dict__.pop("settings", None)
                if restored is not None:
                    connections.settings = restored


class NetworkAccessAttempted(BaseException):
    """Something running under `no_network` tried to open an outbound connection.

    Narrow and named rather than a bare `RuntimeError`, so a test that fails
    because the code under test reached the network is distinguishable at a
    glance from a test that failed for any other reason.

    Derived from `BaseException` rather than `Exception` so that it cannot be
    swallowed. Boot-time code that wraps a call in `except Exception` would
    otherwise absorb the refusal, boot would complete, and every negative
    assertion in `tests/unit/test_no_network_at_boot.py` would pass while boot
    actually reached the network -- the one failure mode a guard like this must
    not have.

    Named for what happened rather than with the `Error` suffix ruff's N818
    prefers, matching `config/authorization/exceptions.py`: what a reader needs
    from the name is the event, and "an access was attempted" is the finding.
    """


#: The methods on `socket.socket` the guard replaces. Both are *inherited* from
#: `_socket.socket` rather than defined on `socket.socket`, which is why the
#: guard below restores them by hand: `monkeypatch.setattr` would record the
#: inherited implementation and "undo" by binding it as an own attribute of the
#: subclass -- leaving the class in a state it was never in.
_GUARDED_SOCKET_METHODS: Final = ("connect", "connect_ex")

#: The module-level `socket` functions the guard replaces. `create_connection`
#: is the connect chokepoint; `getaddrinfo` and `gethostbyname` are the resolver
#: chokepoints, and a hostname lookup is a network round trip that reaches no
#: `socket.socket` at all.
_GUARDED_SOCKET_FUNCTIONS: Final = ("create_connection", "getaddrinfo", "gethostbyname")

#: Distinguishes "the class had no own attribute of that name" from "it had one
#: whose value happened to be None". `delattr` is the correct teardown for the
#: first; `setattr` is the correct teardown for the second.
_ABSENT: Final = object()


def _refuse(address: object) -> NoReturn:
    """Refuse one outbound connection, naming where it was headed.

    Args:
        address: Whatever the caller passed as the destination.

    Raises:
        NetworkAccessAttempted: Always. The address is in the message because
            "something opened a socket" is not a finding a reader can act on,
            and the host it was opened to usually is.

    """
    message = f"a network connection to {address!r} was attempted while the no_network guard was installed"
    raise NetworkAccessAttempted(message)


def _refuse_socket_method(_self: socket.socket, address: object, *_args: Any, **_kwargs: Any) -> NoReturn:
    """Stand in for `socket.socket.connect` and `socket.socket.connect_ex`."""
    _refuse(address)


def _refuse_socket_function(address: object, *_args: Any, **_kwargs: Any) -> NoReturn:
    """Stand in for the module-level `socket` functions, whose first argument is the destination."""
    _refuse(address)


@contextmanager
def _network_guard() -> Iterator[None]:
    """Install the refusals, then restore exactly what was installed.

    Each replacement is recorded only once it has actually been made, and the
    restore walks that record rather than the declared name lists -- so a
    failure part-way through installation leaves nothing behind process-wide.

    Yields:
        None. The guard is the effect.

    """
    saved_methods: dict[str, Any] = {}
    saved_functions: dict[str, Any] = {}
    try:
        for name in _GUARDED_SOCKET_METHODS:
            original = socket.socket.__dict__.get(name, _ABSENT)
            setattr(socket.socket, name, _refuse_socket_method)
            saved_methods[name] = original
        for name in _GUARDED_SOCKET_FUNCTIONS:
            original = getattr(socket, name)
            setattr(socket, name, _refuse_socket_function)
            saved_functions[name] = original
        yield
    finally:
        for name, original in saved_methods.items():
            if original is _ABSENT:
                delattr(socket.socket, name)
            else:
                setattr(socket.socket, name, original)
        for name, original in saved_functions.items():
            setattr(socket, name, original)


@pytest.fixture
def no_network() -> Iterator[None]:
    """Fail the test, loudly and by address, if anything opens a socket.

    Guarded at the socket layer rather than at `requests`, `urllib` or `httpx`:
    a guard installed on one library proves nothing about the library the code
    actually reached for, and OpenTelemetry's exporters, allauth and PyJWT do
    not all use the same one. `socket.socket.connect`, `socket.socket.connect_ex`,
    `socket.create_connection`, `socket.getaddrinfo` and `socket.gethostbyname`
    are the chokepoints every one of them goes through.

    **What it cannot see.** Connectionless UDP -- `sendto` and `sendmsg` on an
    unconnected socket -- is not guarded, because nothing in this stack sends
    UDP. Network I/O performed inside a C extension is outside its reach
    altogether: libpq above all does its own socket work in C and never touches
    Python's `socket` module, so a PostgreSQL connection is invisible here.

    **Never autouse.** `tests/integration/test_import_resolution.py` opens real
    `AF_INET` sockets to find a free port and to wait for a served subprocess to
    accept connections; a global guard would fail those outright, and blocking a
    test's own infrastructure masks a real dependency rather than asserting one.
    Loopback is blocked along with everything else, and nothing that requests
    this fixture needs an exemption: the Django test client opens no socket, and
    sqlite opens no socket.

    Yields:
        None. The guard is the effect.

    """
    with _network_guard():
        yield


@pytest.fixture(scope="session", autouse=True)
def _boot_declared_adapter_withdrawn() -> None:
    """Withdraw the inventory source adapter `CollectorsConfig.ready()` declared at boot.

    `CPM-IDENTITY-S07` makes the watchlist adapter a *boot* declaration: the
    `AppConfig.ready()` hook binds one, once, for the process. `django.setup()`
    happens before the first test, so without this every case that declares an
    adapter of its own would meet `declare_inventory_adapter`'s refusal -- which
    is the refusal doing its job, and which would leave the whole record contract
    and every ingestion case unable to arrange a source.

    So the session starts from the same empty slot the process did, and the boot
    declaration is asserted where it belongs rather than by accident:
    `tests/unit/django_apps/test_watchlist.py` calls `ready()` and reads the slot.
    Withdrawal is session-scoped and one-way -- nothing re-declares afterwards --
    because a re-declaration at the end of the session would only be visible to a
    later session, and the value would be an adapter reading whichever watchlist
    the developer's `COMPONENT_RUNTIME` happened to select.

    Autouse and session-scoped rather than a fixture each case requests: a case
    that forgot to request it would fail on a declaration made by a hook it never
    called, which reads as a defect in the case rather than as the shared state
    it is.

    Returns:
        None. The withdrawal is the effect, and there is deliberately no
        teardown: re-declaring at the end of the session would bind an adapter
        reading whichever watchlist the developer's `COMPONENT_RUNTIME` selected,
        and nothing after the last test would ever read it.

    """
    if declared_inventory_adapter() is not None:
        withdraw_inventory_adapter()


#: The control event the identity-service capture below emits to prove the
#: capture is live. Named rather than written as a literal in each module for the
#: reason the fixture is shared at all: two spellings of a guard's own control
#: value is how one of them stops being checked.
IDENTITY_SERVICE_CAPTURE_CONTROL: Final[str] = "identity-service-capture-control"


@pytest.fixture
def captured_identity_service_logs(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[dict[str, Any]]]:
    """Capture what `identity/services.py` logs, with the two guards the plain helper lacks.

    Shared by `tests/unit/django_apps/test_identity_overrides.py` and
    `tests/integration/django_apps/test_identity_overrides.py`, which both assert
    over the override's authorization events. It lived in both modules verbatim
    first, which is the duplication `tests/model_registry.py` and
    `tests/source_scan.py` were extracted to prevent: two copies of a *guard* that
    can drift look exactly like two passing tests, and the guard here is the
    control event -- the half that stops every assertion over an empty list from
    passing vacuously.

    The reasoning for each guard is `tests/unit/test_drain.py`'s and is not
    restated: the module-scope logger is rebound so `capture_logs` binds a fresh
    proxy inside its own processor chain, and the control event proves the capture
    is live before a case relies on what it holds.

    It sits in this file rather than in a tier-local conftest because both tiers
    use it, and rather than in a helper module because it is a *pytest* fixture --
    it depends on `monkeypatch`, which only the plugin can supply.

    Args:
        monkeypatch: pytest's patcher, which restores the module's own logger.

    Yields:
        The captured events, in order, with the control event already cleared.

    """
    monkeypatch.setattr(identity_services, "logger", structlog.get_logger(identity_services.__name__))
    with structlog.testing.capture_logs() as captured:
        identity_services.logger.warning(IDENTITY_SERVICE_CAPTURE_CONTROL)
        assert [event["event"] for event in captured] == [IDENTITY_SERVICE_CAPTURE_CONTROL], (
            "structlog.testing.capture_logs() cannot see identity.services' logger, so every assertion "
            "over what it logged would be vacuous"
        )
        captured.clear()
        yield captured


#: The control event the collector-base capture below emits, on the terms the
#: identity-service one is named.
COLLECTION_CAPTURE_CONTROL: Final[str] = "collection-capture-control"


@pytest.fixture
def captured_collection_logs(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[dict[str, Any]]]:
    """Capture what `core/collection.py` logs, with the same two guards as the identity capture.

    Shared by the `CPM-OPERATE-S05` hygiene cases in
    `tests/integration/django_apps/test_source_release.py` and
    `tests/integration/django_apps/test_feedstock.py`, which grep every event
    the base emitted for a credential that must not be there -- and a grep
    over an empty list passes for the wrong reason, which is what the control
    event rules out. `core/collection.py` is the one module on the collection
    path that logs a run's `detail`; the collectors themselves log nothing.

    Args:
        monkeypatch: pytest's patcher, which restores the module's own logger.

    Yields:
        The captured events, in order, with the control event already cleared.

    """
    monkeypatch.setattr(core_collection, "logger", structlog.get_logger(core_collection.__name__))
    with structlog.testing.capture_logs() as captured:
        core_collection.logger.warning(COLLECTION_CAPTURE_CONTROL)
        assert [event["event"] for event in captured] == [COLLECTION_CAPTURE_CONTROL], (
            "structlog.testing.capture_logs() cannot see core/collection.py's logger, so every assertion "
            "over what it logged would be vacuous"
        )
        captured.clear()
        yield captured


@pytest.fixture
def captured_every_structlog_logger(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[dict[str, Any]]]:
    """Capture what *every* first-party structlog logger emits, for the `CPM-OPERATE-S05` task-path hygiene cases.

    The collection-only capture above rebinds one module's logger; the task
    path crosses several -- the task module, the ledger, the base, the rate
    limiter, the response cache -- and a credential printed by any of them is
    the same leak. So every already-imported module under `conda_sentinel` and
    `config` that holds a module-scope structlog `logger` is rebound to a fresh
    proxy inside the capture's processor chain (`cache_logger_on_first_use` is
    on, so a proxy bound before the capture would keep the old chain), and the
    control event is emitted through the collector base's, which is the one
    the assertions cannot be vacuous without.

    Args:
        monkeypatch: pytest's patcher, which restores every module's own logger.

    Yields:
        The captured events, in order, with the control event already cleared.

    """
    for name, module in list(sys.modules.items()):
        if module is None or not (name.startswith(("conda_sentinel.", "config."))):
            continue
        logger = getattr(module, "logger", None)
        if logger is not None and type(logger).__module__.startswith("structlog"):
            monkeypatch.setattr(module, "logger", structlog.get_logger(name))
    with structlog.testing.capture_logs() as captured:
        core_collection.logger.warning(COLLECTION_CAPTURE_CONTROL)
        assert [event["event"] for event in captured] == [COLLECTION_CAPTURE_CONTROL], (
            "structlog.testing.capture_logs() cannot see the rebound loggers, so every assertion over what "
            "the task path logged would be vacuous"
        )
        captured.clear()
        yield captured
