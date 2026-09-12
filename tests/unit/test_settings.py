"""Tests for the settings modules themselves.

Each test imports a settings module fresh so its module-level environment
reads are re-evaluated. `config.settings.base` is evicted alongside the target
because the ``from .base import *`` in each module would otherwise reuse the
already-imported copy. Django's active settings are unaffected: they were
materialised at startup and hold no reference to these fresh module objects.
"""

from __future__ import annotations

import importlib

import pytest
from django.core.exceptions import ImproperlyConfigured

from conda_sentinel.collectors.conda_package import CHANNELS_SETTING
from conda_sentinel.collectors.conda_package import COLLECTOR_NAME as CONDA_PACKAGE_NAME
from conda_sentinel.collectors.conda_package import PLATFORMS_SETTING
from conda_sentinel.collectors.conda_package import CondaPackageCollector
from conda_sentinel.collectors.feedstock import COLLECTOR_NAME as FEEDSTOCK_NAME
from conda_sentinel.collectors.feedstock import FeedstockCollector
from conda_sentinel.collectors.kev import COLLECTOR_NAME as KEV_NAME
from conda_sentinel.collectors.kev import KEV_DISPATCH_OFFSET
from conda_sentinel.collectors.kev import KevCollector
from conda_sentinel.collectors.license import COLLECTOR_NAME as LICENSE_NAME
from conda_sentinel.collectors.license import LICENSE_DISPATCH_OFFSET
from conda_sentinel.collectors.license import LicenseCollector
from conda_sentinel.collectors.pypi_release import COLLECTOR_NAME as PYPI_RELEASE_NAME
from conda_sentinel.collectors.pypi_release import PyPIReleaseCollector
from conda_sentinel.collectors.python_readiness import COLLECTOR_NAME as PYTHON_READINESS_NAME
from conda_sentinel.collectors.python_readiness import READINESS_DISPATCH_OFFSET
from conda_sentinel.collectors.python_readiness import PythonReadinessCollector
from conda_sentinel.collectors.resolve_identity import COLLECTOR_NAME as RESOLVE_IDENTITY_NAME
from conda_sentinel.collectors.resolve_identity import IdentityResolutionCollector
from conda_sentinel.collectors.source_release import COLLECTOR_NAME as SOURCE_RELEASE_NAME
from conda_sentinel.collectors.source_release import SourceReleaseCollector
from conda_sentinel.collectors.sweep import COLLECTOR_KWARG
from conda_sentinel.collectors.sweep import SWEEP_TASK_NAME
from conda_sentinel.collectors.vulnerability import COLLECTOR_NAME as VULNERABILITY_NAME
from conda_sentinel.collectors.vulnerability import VulnerabilityCollector
from conda_sentinel.core import queues
from conda_sentinel.core import roles
from config.authorization import claims
from config.local_dev import keys
from config.locality import LOCAL as LOCAL_RUNTIME
from config.locality import RUNTIME_ENV_VAR
from config.startup.allowlist import CONTRIBUTABLE_KEYS
from tests.logging_config import assert_writes_no_files
from tests.pixi_manifest import REPO_ROOT
from tests.settings_import import evicted_settings_modules

# AD-23's declared windows, in seconds, and the values the environment-driven
# case overrides them with. Named rather than written at the assertion so the
# numbers read as the policy they are.
DEFAULT_JWKS_TTL_SECONDS = 3600.0
DEFAULT_JWKS_MIN_REFETCH_SECONDS = 60.0
OVERRIDDEN_JWKS_TTL_SECONDS = 900.0
OVERRIDDEN_JWKS_MIN_REFETCH_SECONDS = 30.0

# The floors both windows are clamped to. A zero or negative refetch window makes
# `now - last_attempt < window` false for every caller, which disables the rate
# limit outright -- so the floor is enforced rather than documented as advice.
FLOOR_JWKS_TTL_SECONDS = 60.0
FLOOR_JWKS_MIN_REFETCH_SECONDS = 1.0

# Clock-skew tolerance ships at zero: the lever is added, the posture is not
# moved. Anything above zero accepts a token past its own `exp` by that much.
DEFAULT_OIDC_LEEWAY_SECONDS = 0.0
OVERRIDDEN_OIDC_LEEWAY_SECONDS = 5.0

# The local development values `config/settings/local.py` fills unset role names
# with. Named here so the fill is asserted against a stated value rather than
# against "not empty", which a leaked ambient variable would also satisfy.
LOCAL_SECURITY_REVIEWER_GROUP = "cpm-security-reviewer"
LOCAL_PACKAGING_ENGINEER_GROUP = "cpm-packaging-engineer"
LOCAL_LEADERSHIP_GROUP = "cpm-leadership"

BASE = "config.settings.base"
LOCAL = "config.settings.local"
PRODUCTION = "config.settings.production"
TEST = "config.settings.test"

# The one in-process cache backend, named once. Both `local.py` and `test.py`
# declare it, and the assertions below compare against this rather than against
# each other so that a change to one of them is a failure rather than a drift.
LOCMEM_CACHE_BACKEND = "django.core.cache.backends.locmem.LocMemCache"

# The deployed cache backend, named once. `config/settings/production.py`
# declares it and `config/settings/test.py`'s Redis branch declares the same one,
# which is the point: a proof run against a different client than the deployed
# one proves something about a configuration nothing ships.
REDIS_CACHE_BACKEND = "django_redis.cache.RedisCache"

# The one variable that moves the suite off the in-process cache substitution,
# and the whole mechanism that does it -- the shape `DATABASE_URL` already has
# for the database (`tests/unit/test_database_selection.py`).
CACHE_URL_VARIABLE = "CPM_TEST_REDIS_URL"

# A URL naming a Redis nothing is listening on. These cases read the `CACHES`
# dict the module builds; none of them connects.
A_REDIS_URL = "redis://localhost:56379/0"


@pytest.fixture(autouse=True)
def _evict_settings_modules():
    """Drop freshly imported settings modules before and after each test.

    The body lives in `tests/settings_import.py` because
    `tests/unit/test_payload_properties.py` needs the identical thing and a
    second copy is a second answer waiting to drift -- the same argument
    `tests/pixi_manifest.py` and `tests/dockerfile.py` record for their readers.

    It also restores structlog, which this copy did not. `config/settings/
    base.py` calls `configure_structlog()` at module scope, so every fresh
    import reconfigures the process-wide pipeline; leaving the last one standing
    blinds `structlog.testing.capture_logs()` in whatever module sorts after this
    one, with no failure here to point at it.
    """
    yield from evicted_settings_modules()


@pytest.fixture
def no_database_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("POSTGRES_DB", raising=False)


@pytest.fixture
def no_cache_url_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear the cache selector so the substitution is what the module is read for.

    The sibling of `no_database_env`, and it exists for the identical reason:
    `config/settings/test.py` selects `django_redis.cache.RedisCache` when
    `CPM_TEST_REDIS_URL` is set, and `pixi run gate-redis` and the CI gate job
    both set it -- so a case asserting the *substitution* has to say which
    environment it is asserting about, or it passes locally and fails on the two
    runs that matter.
    """
    monkeypatch.delenv(CACHE_URL_VARIABLE, raising=False)


@pytest.fixture
def no_claims_contract_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear the four contract variables so the developer's shell cannot leak in."""
    for name in (
        "COMPONENT_IDENTITY_CLAIM",
        "COMPONENT_GROUP_CLAIM",
        "COMPONENT_STAFF_GROUP",
        "COMPONENT_SUPERUSER_GROUP",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def no_role_contract_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear the three role variables so the developer's shell cannot leak in.

    Read off `ROLE_ENVIRONMENT_VARIABLES` rather than respelled, so a rename in
    the role module cannot leave this fixture clearing names nothing reads while
    the real ones survive from the ambient environment.
    """
    for name in roles.ROLE_ENVIRONMENT_VARIABLES:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def no_oidc_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear the provider variables so the developer's shell cannot leak in."""
    for name in (
        "COMPONENT_OIDC_ISSUER",
        "COMPONENT_OIDC_CLIENT_ID",
        "COMPONENT_OIDC_CLIENT_SECRET",
        "COMPONENT_OIDC_PROVIDER_ID",
        "COMPONENT_OIDC_PROVIDER_NAME",
        "COMPONENT_OIDC_JWKS_URL",
        "COMPONENT_OIDC_AUDIENCE",
        "COMPONENT_OIDC_ALGORITHMS",
        "COMPONENT_JWKS_TTL_SECONDS",
        "COMPONENT_JWKS_MIN_REFETCH_SECONDS",
        "COMPONENT_OIDC_LEEWAY_SECONDS",
        "COMPONENT_SITE_DOMAIN",
        "COMPONENT_SITE_NAME",
        "DJANGO_ADMIN_FORCE_ALLAUTH",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def production_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DJANGO_SECRET_KEY", "x" * 50)
    monkeypatch.setenv("DJANGO_ADMIN_URL", "admin/")


@pytest.mark.usefixtures("no_database_env")
def test_dot_env_file_is_read_when_enabled(monkeypatch: pytest.MonkeyPatch):
    """DJANGO_READ_DOT_ENV_FILE toggles the .env read in base.py."""
    monkeypatch.setenv("DJANGO_READ_DOT_ENV_FILE", "True")
    base = importlib.import_module(BASE)
    assert base.READ_DOT_ENV_FILE is True


@pytest.mark.usefixtures("no_database_env")
def test_debug_apps_are_off_by_default(monkeypatch: pytest.MonkeyPatch):
    """The runtime environment lacks debug_toolbar, so local must not require it."""
    monkeypatch.delenv("DJANGO_DEBUG_APPS", raising=False)
    local = importlib.import_module(LOCAL)
    assert local.DEBUG_APPS is False
    assert "debug_toolbar" not in local.INSTALLED_APPS
    assert "django_extensions" not in local.INSTALLED_APPS
    assert not any("debug_toolbar" in mw for mw in local.MIDDLEWARE)


@pytest.mark.usefixtures("no_database_env")
def test_debug_apps_can_be_enabled(monkeypatch: pytest.MonkeyPatch):
    """The dev environment sets DJANGO_DEBUG_APPS, which wires the toolbar in."""
    monkeypatch.setenv("DJANGO_DEBUG_APPS", "True")
    local = importlib.import_module(LOCAL)
    assert local.DEBUG_APPS is True
    assert "debug_toolbar" in local.INSTALLED_APPS
    assert "django_extensions" in local.INSTALLED_APPS
    assert any("debug_toolbar" in mw for mw in local.MIDDLEWARE)


@pytest.mark.usefixtures("no_database_env")
def test_local_falls_back_to_sqlite():
    local = importlib.import_module(LOCAL)
    assert local.DATABASES["default"]["ENGINE"].endswith("sqlite3")
    assert local.DEBUG is True


def test_local_supplies_a_configured_claims_contract(monkeypatch: pytest.MonkeyPatch):
    """Local development gets a usable contract even when nothing declares one.

    `base.py` deliberately defaults none of the four claim names, so on a fresh
    clone the contract is empty. FR-19's persona seeding drives the real mapper,
    which rejects a payload whose identity-key claim it cannot find -- so without
    this, `pixi run -e dev seed-personas` fails with `identity key claim absent`.
    """
    for name in claims.CLAIMS_ENVIRONMENT_VARIABLES:
        monkeypatch.delenv(name, raising=False)

    local = importlib.import_module(LOCAL)

    assert local.CLAIMS_CONTRACT.is_configured
    assert local.CLAIMS_CONTRACT.identity_key_claim == "sub"


def test_local_does_not_override_a_declared_claims_contract(monkeypatch: pytest.MonkeyPatch):
    """Only unset fields are filled; the environment still wins.

    The fallback must not re-spell the `COMPONENT_*` variable names or shadow a
    local run pointed at a real identity realm.
    """
    monkeypatch.setenv("COMPONENT_IDENTITY_CLAIM", "oid")
    monkeypatch.setenv("COMPONENT_GROUP_CLAIM", "realm_access.roles")

    local = importlib.import_module(LOCAL)

    assert local.CLAIMS_CONTRACT.identity_key_claim == "oid"
    assert local.CLAIMS_CONTRACT.group_claim == "realm_access.roles"
    assert local.CLAIMS_CONTRACT.staff_group == "platform-staff"


@pytest.mark.usefixtures("no_role_contract_env")
def test_local_supplies_a_configured_role_contract():
    """A fresh clone still gets three role groups to develop against.

    `base.py` defaults none of the three names, so without this the local role
    contract is empty, `core/0001_provision_role_groups` provisions nothing, and
    a developer has no role group to assert in a synthetic token.
    """
    local = importlib.import_module(LOCAL)

    assert local.ROLE_CONTRACT.is_configured
    assert local.ROLE_CONTRACT.security_reviewer == LOCAL_SECURITY_REVIEWER_GROUP
    assert local.ROLE_CONTRACT.packaging_engineer == LOCAL_PACKAGING_ENGINEER_GROUP
    assert local.ROLE_CONTRACT.leadership == LOCAL_LEADERSHIP_GROUP


@pytest.mark.usefixtures("no_role_contract_env")
def test_local_does_not_override_a_declared_role_contract(monkeypatch: pytest.MonkeyPatch):
    """Only unset fields are filled; the environment still wins.

    The fallback must not re-spell the `CPM_*` variable names or shadow a local
    run pointed at a real identity realm's role groups. The other two fields are
    pinned to the local values rather than merely asserted non-empty: with the
    environment cleared first, "not empty" is only satisfiable by the fallback
    having actually run, and pinning it is what catches a fill that silently
    stopped filling.
    """
    monkeypatch.setenv("CPM_SECURITY_REVIEWER_GROUP", "ops-reviewers")

    local = importlib.import_module(LOCAL)

    assert local.ROLE_CONTRACT.security_reviewer == "ops-reviewers"
    assert local.ROLE_CONTRACT.packaging_engineer == LOCAL_PACKAGING_ENGINEER_GROUP
    assert local.ROLE_CONTRACT.leadership == LOCAL_LEADERSHIP_GROUP


@pytest.mark.usefixtures("no_role_contract_env")
def test_local_fills_a_role_name_the_environment_left_blank(monkeypatch: pytest.MonkeyPatch):
    """A whitespace-only variable is unset, so it is filled rather than honoured.

    Honouring it would create a `Group` whose name is whitespace: a row no claim
    can match, that nothing deletes, and that reports the contract as configured.
    """
    monkeypatch.setenv("CPM_LEADERSHIP_GROUP", "   ")

    local = importlib.import_module(LOCAL)

    assert local.ROLE_CONTRACT.leadership == LOCAL_LEADERSHIP_GROUP
    assert local.ROLE_CONTRACT.is_configured


def test_local_points_the_jwks_location_at_the_generated_keypair(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """AC #1: local settings point the JWKS location at the key the minting task generates.

    The directory is relocated into `tmp_path` first, so the second assertion is
    a real one: naming the location must not *create* anything. FR-23 makes a
    boot-time side effect a defect, and RSA generation from a settings import
    would make every `pixi run manage` invocation pay for a keypair.
    """
    monkeypatch.delenv("COMPONENT_OIDC_JWKS_URL", raising=False)
    # Also deleted: the file location is the fallback only when *neither*
    # variable was declared -- see the case below, which is the other half.
    monkeypatch.delenv("COMPONENT_OIDC_ISSUER", raising=False)
    key_dir = tmp_path / ".local-dev-keys"
    monkeypatch.setattr(keys, "DEV_KEY_DIR", key_dir)

    local = importlib.import_module(LOCAL)

    assert (key_dir / keys.JWKS_FILENAME).as_uri() == local.OIDC_JWKS_URL
    assert local.OIDC_JWKS_URL.startswith("file://")
    assert not key_dir.exists()


def test_a_declared_issuer_leaves_the_jwks_location_derived(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """Pointing a local run at a real realm must not silently retarget the trust anchor.

    `base.py` leaves `OIDC_JWKS_URL` empty on purpose: `configured_jwks_url()`
    falls back to `conventional_jwks_url(OIDC_ISSUER)`, so *unset* means derived
    from the issuer rather than unconfigured. Filling it unconditionally would
    destroy that -- a developer exporting only `COMPONENT_OIDC_ISSUER` would get
    this machine's own keypair as the trust anchor and every token that realm
    issued would be refused with `no signing key published for the presented
    kid`, with the documentation telling them the issuer variable was enough.
    """
    monkeypatch.setenv("COMPONENT_OIDC_ISSUER", "https://keycloak.example/realms/main")
    monkeypatch.delenv("COMPONENT_OIDC_JWKS_URL", raising=False)
    monkeypatch.setattr(keys, "DEV_KEY_DIR", tmp_path / ".local-dev-keys")

    local = importlib.import_module(LOCAL)

    assert local.OIDC_JWKS_URL == ""
    assert local.OIDC_ISSUER == "https://keycloak.example/realms/main"


@pytest.mark.parametrize(
    ("variable", "setting", "expected"),
    [
        pytest.param("COMPONENT_OIDC_ISSUER", "OIDC_ISSUER", "https://local-dev.invalid/realms/component", id="issuer"),
        pytest.param("COMPONENT_OIDC_AUDIENCE", "OIDC_AUDIENCE", "local-dev-component-api", id="audience"),
    ],
)
def test_a_whitespace_only_declaration_is_filled_rather_than_honoured(
    monkeypatch: pytest.MonkeyPatch,
    variable: str,
    setting: str,
    expected: str,
):
    """A variable exported as spaces is not a declaration.

    Whitespace is truthy, so an unstripped `or` treats it as declared and skips
    the fill -- and `authentication._audience()` strips before comparing, so the
    component would then refuse every token while every setting looked
    configured. The failure has no diagnostic: the value is present, non-empty
    and wrong.
    """
    for name in ("COMPONENT_OIDC_ISSUER", "COMPONENT_OIDC_CLIENT_ID", "COMPONENT_OIDC_AUDIENCE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(variable, "   ")

    local = importlib.import_module(LOCAL)

    assert getattr(local, setting) == expected


def test_local_supplies_an_issuer_and_an_audience_to_verify_against(monkeypatch: pytest.MonkeyPatch):
    """Both are load-bearing, not decoration.

    `base.py` defaults each to the empty string, and PyJWT refuses a token whose
    `aud` is empty with `MissingRequiredClaimError` -- `_validate_aud` tests
    `not payload["aud"]`, not merely its presence. With the audience unset, every
    locally minted token is rejected and FR-20's flow cannot work on a fresh
    clone.
    """
    for name in ("COMPONENT_OIDC_ISSUER", "COMPONENT_OIDC_CLIENT_ID", "COMPONENT_OIDC_AUDIENCE"):
        monkeypatch.delenv(name, raising=False)

    local = importlib.import_module(LOCAL)

    assert local.OIDC_ISSUER
    assert local.OIDC_AUDIENCE


def test_local_does_not_override_a_declared_issuer_audience_or_jwks_location(monkeypatch: pytest.MonkeyPatch):
    """Only unset values are filled; a local run pointed at a real realm still wins."""
    monkeypatch.setenv("COMPONENT_OIDC_ISSUER", "https://idp.example.com/realms/main")
    monkeypatch.setenv("COMPONENT_OIDC_AUDIENCE", "declared-api")
    monkeypatch.setenv("COMPONENT_OIDC_JWKS_URL", "https://idp.example.com/realms/main/certs")

    local = importlib.import_module(LOCAL)

    assert local.OIDC_ISSUER == "https://idp.example.com/realms/main"
    assert local.OIDC_AUDIENCE == "declared-api"
    assert local.OIDC_JWKS_URL == "https://idp.example.com/realms/main/certs"


@pytest.mark.usefixtures("production_env")
def test_no_other_settings_module_points_at_a_local_file(monkeypatch: pytest.MonkeyPatch):
    """The `file://` location is `local.py`'s alone.

    `base.py` and `production.py` leave the location empty, so the trust anchor a
    deployed component uses is derived from its issuer and from nothing else. A
    default that reached either of them would put a `file://` location one missing
    environment variable away from production.
    """
    monkeypatch.delenv("COMPONENT_OIDC_JWKS_URL", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgres://user:pw@db:5432/app")

    base = importlib.import_module(BASE)
    production = importlib.import_module(PRODUCTION)

    assert not base.OIDC_JWKS_URL
    assert not production.OIDC_JWKS_URL


def test_postgres_env_selects_postgres(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("POSTGRES_DB", "app")
    monkeypatch.setenv("POSTGRES_USER", "app")
    monkeypatch.setenv("POSTGRES_PASSWORD", "secret")
    base = importlib.import_module(BASE)
    assert base.DATABASES["default"]["ENGINE"].endswith("postgresql")
    assert base.DATABASES["default"]["NAME"] == "app"


@pytest.mark.usefixtures("no_database_env", "no_claims_contract_env")
def test_base_imports_cleanly_with_an_unconfigured_contract():
    """Importing base with no COMPONENT_ variables set must not raise.

    The refusal to start on an unusable claims contract is Epic 4's, gated on a
    locality signal that does not exist yet. A raise here would fire during the
    test suite and during every management command. What base owes today is an
    unconfigured contract that reports itself as one, with nothing defaulted.
    """
    base = importlib.import_module(BASE)
    contract = base.CLAIMS_CONTRACT
    assert contract.is_configured is False
    assert contract.identity_key_claim == ""
    assert contract.group_claim == ""
    assert contract.staff_group == ""
    assert contract.superuser_group == ""


@pytest.mark.usefixtures("no_database_env")
def test_base_reads_a_configured_contract_from_the_environment(monkeypatch: pytest.MonkeyPatch):
    """The four variables reach `settings.CLAIMS_CONTRACT`, not just the loader.

    Without this the settings line could be replaced by a hardcoded empty
    contract and nothing would fail: every deployment silently unconfigured.
    """
    monkeypatch.setenv("COMPONENT_IDENTITY_CLAIM", "oid")
    monkeypatch.setenv("COMPONENT_GROUP_CLAIM", "realm_access.roles")
    monkeypatch.setenv("COMPONENT_STAFF_GROUP", "ops-staff")
    monkeypatch.setenv("COMPONENT_SUPERUSER_GROUP", "ops-admin")

    base = importlib.import_module(BASE)
    contract = base.CLAIMS_CONTRACT

    assert contract.identity_key_claim == "oid"
    assert contract.group_claim == "realm_access.roles"
    assert contract.staff_group == "ops-staff"
    assert contract.superuser_group == "ops-admin"
    assert contract.is_configured is True


@pytest.mark.usefixtures("no_database_env", "no_role_contract_env")
def test_base_defaults_no_role_group_name():
    """AC #2: the role group names have no default baked into the settings module.

    Asserted on `base.ROLE_CONTRACT` rather than on the loader, because the
    defaulting this forbids would most naturally be written here -- an `or` on
    the settings line, or a `default=` handed to `load_role_contract`. Three
    groups provisioned under names nobody declared is worse than none: the
    operator's real groups still resolve to nothing, and the rows that do exist
    look like a working configuration.
    """
    base = importlib.import_module(BASE)
    contract = base.ROLE_CONTRACT

    assert contract.is_configured is False
    assert contract.security_reviewer == ""
    assert contract.packaging_engineer == ""
    assert contract.leadership == ""


@pytest.mark.usefixtures("no_database_env")
def test_base_reads_all_three_role_group_names_from_the_environment(monkeypatch: pytest.MonkeyPatch):
    """AC #2: the three variables reach `settings.ROLE_CONTRACT`, not just the loader.

    Without this the settings line could be replaced by a hardcoded empty
    contract and nothing would fail: every deployment silently provisioning no
    role group at all.
    """
    monkeypatch.setenv("CPM_SECURITY_REVIEWER_GROUP", "ops-reviewers")
    monkeypatch.setenv("CPM_PACKAGING_ENGINEER_GROUP", "ops-packagers")
    monkeypatch.setenv("CPM_LEADERSHIP_GROUP", "ops-leads")

    base = importlib.import_module(BASE)
    contract = base.ROLE_CONTRACT

    assert contract.security_reviewer == "ops-reviewers"
    assert contract.packaging_engineer == "ops-packagers"
    assert contract.leadership == "ops-leads"
    assert contract.is_configured is True


def _oidc_app(base: object) -> dict[str, object]:
    """Return the single provider app `SOCIALACCOUNT_PROVIDERS` declares."""
    apps = base.SOCIALACCOUNT_PROVIDERS["openid_connect"]["APPS"]  # type: ignore[attr-defined]
    assert len(apps) == 1, "one provider app: a second is what makes allauth's get_app ambiguous"
    return apps[0]  # type: ignore[no-any-return]


@pytest.mark.usefixtures("no_database_env", "no_oidc_env")
def test_the_oidc_provider_ships_with_the_installed_allauth():
    """AC #2: allauth's own provider, and no second OIDC framework beside it."""
    base = importlib.import_module(BASE)

    assert "allauth.socialaccount.providers.openid_connect" in base.INSTALLED_APPS
    # The entry sits with allauth's own apps rather than being appended to the
    # end, and it is never guarded (AD-24): no conditional import, no
    # settings-module inheritance, no try/except ImportError.
    assert base.INSTALLED_APPS.index("allauth.socialaccount.providers.openid_connect") == (
        base.INSTALLED_APPS.index("allauth.socialaccount") + 1
    )


@pytest.mark.usefixtures("no_database_env")
def test_the_provider_is_configured_from_the_environment(monkeypatch: pytest.MonkeyPatch):
    """AC #4: from `SOCIALACCOUNT_PROVIDERS`, populated from the environment."""
    monkeypatch.setenv("COMPONENT_OIDC_ISSUER", "https://idp.example.test/realms/component")
    monkeypatch.setenv("COMPONENT_OIDC_CLIENT_ID", "component-web")
    monkeypatch.setenv("COMPONENT_OIDC_CLIENT_SECRET", "s3cret")
    monkeypatch.setenv("COMPONENT_OIDC_PROVIDER_ID", "realm")
    monkeypatch.setenv("COMPONENT_OIDC_PROVIDER_NAME", "Component Realm")

    app = _oidc_app(importlib.import_module(BASE))

    assert app["settings"]["server_url"] == "https://idp.example.test/realms/component"  # type: ignore[index]
    assert app["client_id"] == "component-web"
    assert app["secret"] == "s3cret"  # noqa: S105 - a test fixture, not a credential
    assert app["provider_id"] == "realm"
    assert app["name"] == "Component Realm"


@pytest.mark.usefixtures("no_database_env", "no_oidc_env")
def test_the_provider_reads_but_never_defaults_the_issuer():
    """An unconfigured IdP imports cleanly; refusing it is Epic 4's stage 1, not this module's."""
    app = _oidc_app(importlib.import_module(BASE))

    assert app["settings"]["server_url"] == ""  # type: ignore[index]
    assert app["client_id"] == ""


@pytest.mark.usefixtures("no_database_env", "no_oidc_env")
def test_pkce_is_enabled_on_the_provider():
    """FR-4 specifies Authorization Code with PKCE; allauth's default without this key is off."""
    app = _oidc_app(importlib.import_module(BASE))

    assert app["settings"]["oauth_pkce_enabled"] is True  # type: ignore[index]


@pytest.mark.usefixtures("no_database_env", "no_oidc_env")
def test_email_is_never_an_authentication_key():
    """AD-11: `idp_subject` is the sole identity key, so a matching address may not sign anyone in."""
    base = importlib.import_module(BASE)

    assert base.SOCIALACCOUNT_EMAIL_AUTHENTICATION is False


@pytest.mark.usefixtures("no_database_env", "no_oidc_env")
def test_the_social_adapter_is_the_one_that_calls_the_mapper():
    base = importlib.import_module(BASE)

    assert base.SOCIALACCOUNT_ADAPTER == "config.authorization.adapters.OIDCSocialAccountAdapter"


@pytest.mark.usefixtures("no_database_env", "no_oidc_env")
def test_the_login_url_is_the_provider_route_rather_than_the_local_form():
    """AC #1: an unauthenticated request lands on the IdP redirect, never on allauth's form."""
    base = importlib.import_module(BASE)

    login_url = str(base.LOGIN_URL)

    assert login_url.endswith("/oidc/oidc/login/")
    assert login_url != "/accounts/login/"


@pytest.mark.usefixtures("no_database_env")
def test_the_login_url_follows_the_configured_provider_id(monkeypatch: pytest.MonkeyPatch):
    """One variable, one meaning: the route and the provider app read the same name."""
    monkeypatch.setenv("COMPONENT_OIDC_PROVIDER_ID", "realm")

    base = importlib.import_module(BASE)

    assert str(base.LOGIN_URL).endswith("/realm/login/")
    assert _oidc_app(base)["provider_id"] == "realm"


@pytest.mark.usefixtures("no_database_env", "no_oidc_env")
def test_the_admin_is_forced_through_allauth_by_default():
    """FR-7 / AC #6: true with the variable unset. It shipped false, which was the defect."""
    base = importlib.import_module(BASE)

    assert base.DJANGO_ADMIN_FORCE_ALLAUTH is True


@pytest.mark.usefixtures("no_database_env", "no_oidc_env")
def test_the_site_domain_defaults_to_localhost_rather_than_a_repository_domain():
    base = importlib.import_module(BASE)

    assert base.SITE_DOMAIN == "localhost"
    assert base.SITE_NAME == "localhost"


@pytest.mark.usefixtures("no_database_env")
def test_the_site_domain_is_environment_driven(monkeypatch: pytest.MonkeyPatch):
    """AC #5: the domain comes from the environment, never from a data migration."""
    monkeypatch.setenv("COMPONENT_SITE_DOMAIN", "component.example.test")
    monkeypatch.setenv("COMPONENT_SITE_NAME", "Component")

    base = importlib.import_module(BASE)

    assert base.SITE_DOMAIN == "component.example.test"
    assert base.SITE_NAME == "Component"


@pytest.mark.usefixtures("no_database_env", "production_env")
def test_production_refuses_sqlite():
    with pytest.raises(ImproperlyConfigured, match="requires a real database"):
        importlib.import_module(PRODUCTION)


@pytest.mark.usefixtures("production_env")
def test_production_accepts_a_real_database(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DATABASE_URL", "postgres://user:pw@db:5432/app")
    production = importlib.import_module(PRODUCTION)
    assert production.DATABASES["default"]["ENGINE"].endswith("postgresql")
    assert production.DEBUG is False


# ---------------------------------------------------------------------------
# Story 4.2 -- stage 1 evaluated through a real settings import.
#
# `tests/unit/startup/test_stage_one_conditions.py` owns the per-condition
# coverage and drives every case against a synthetic namespace. That is the
# right shape for asserting what each condition refuses, and it is structurally
# incapable of asserting the thing these two cases assert: that the call Story
# 4.1 placed as the last statement of `production.py` actually reaches the
# roster, with the values `base.py` and `production.py` composed onto the module
# rather than values a test wrote by hand. Two cases, not a matrix -- the
# condition-by-condition suite must not be duplicated here.
# ---------------------------------------------------------------------------

# Condition 4, and the state that fires first on a real deployed import today.
#
# It used to be state 2a -- `base.py` listed `ModelBackend` in
# `AUTHENTICATION_BACKENDS` -- and then state 2b, `ACCOUNT_LOGIN_METHODS =
# {"username"}`. Story 4.6 moved both into `local.py` and `test.py`, which is what
# made a deployed component importable at all, and the chain then reached exactly
# where the previous revision of this test predicted: an unset trust anchor, which
# is a genuine deployment requirement rather than a leftover of the reference
# application.
#: The backend a deployed component keeps -- the only entry `base.py` declares.
ALLAUTH_BACKEND = "allauth.account.auth_backends.AuthenticationBackend"

LIVE_REFUSAL_SETTING = "OIDC_ISSUER"
LIVE_REFUSAL_VARIABLE = "COMPONENT_OIDC_ISSUER"


@pytest.mark.usefixtures("production_env")
def test_a_deployed_production_import_is_refused_by_stage_one(monkeypatch: pytest.MonkeyPatch):
    """The refusal fires through a genuine import, not through a hand-built namespace.

    `DATABASE_URL` is set so the sqlite refusal at `production.py:26-28` cannot
    fire first -- that one raises before the module's last statement is reached,
    and a test that stopped there would say nothing about stage 1 at all.
    `COMPONENT_RUNTIME` is deleted rather than set, because locality fails closed
    (AD-13) and absent is how a deployment that lost the variable spells itself.

    **Which condition this currently catches, and why that matters.** It is
    condition 4, the trust anchor. States 2a and 2b used to fire first -- `base.py`
    listed `ModelBackend` in `AUTHENTICATION_BACKENDS` and set
    `ACCOUNT_LOGIN_METHODS = {"username"}` -- so this test caught condition 2 and
    conditions 3, 4 and 5 were never reached. Epic 2 was recorded as owning that
    removal and did not perform it, which left the tree in a state where **no**
    deployed component could import this module: `production.py` adds no override,
    so the refusal fired on every real deployment. Story 4.6 moved both states into
    `local.py` and `test.py`, and the chain advanced to here, exactly as the
    previous revision of this docstring predicted it would.

    **When condition 4 stops being the live one, move this forward rather than
    delete it.** A deployment that supplies `COMPONENT_OIDC_ISSUER` reaches
    condition 5, the claims contract, and then leaves stage 1 entirely -- at which
    point the case this test makes is carried by
    `test_a_deployed_production_import_is_accepted_when_the_contract_is_complete`
    and this one asserts the last remaining unmet deployment requirement.

    The assertion names the condition on purpose. A bare
    `pytest.raises(ImproperlyConfigured)` here would stay green through every one
    of those transitions and would read, to the next person, as proof that the
    whole contract holds -- when it is only ever proof that the first live
    condition holds.
    """
    monkeypatch.setenv("DATABASE_URL", "postgres://user:pw@db:5432/app")
    monkeypatch.delenv(RUNTIME_ENV_VAR, raising=False)

    with pytest.raises(ImproperlyConfigured) as refused:
        importlib.import_module(PRODUCTION)

    message = str(refused.value)
    assert LIVE_REFUSAL_SETTING in message
    assert LIVE_REFUSAL_VARIABLE in message


#: Everything a deployed component has to supply for stage 1 to pass it. Set as a
#: mapping rather than as eleven `setenv` lines because the point of the case is
#: the completeness of the set: this *is* the deployment contract, and a condition
#: added later is meant to fail here until its variable is added.
DEPLOYED_ENVIRONMENT = {
    "DATABASE_URL": "postgres://user:pw@db:5432/app",
    "REDIS_URL": "redis://redis:6379/0",
    "DJANGO_ADMIN_FORCE_ALLAUTH": "True",
    "COMPONENT_IDENTITY_CLAIM": "sub",
    "COMPONENT_GROUP_CLAIM": "groups",
    "COMPONENT_STAFF_GROUP": "platform-staff",
    "COMPONENT_SUPERUSER_GROUP": "platform-superuser",
    "COMPONENT_OIDC_ISSUER": "https://idp.example.invalid/realms/component",
    "COMPONENT_OIDC_CLIENT_ID": "component-api",
    "COMPONENT_OIDC_CLIENT_SECRET": "not-a-real-secret",
}


@pytest.mark.usefixtures("production_env")
def test_a_deployed_production_import_is_accepted_when_every_requirement_is_met(
    monkeypatch: pytest.MonkeyPatch,
):
    """A deployed component can start. Until Story 4.6 it could not, and nothing said so.

    This is the case whose absence let the escape through. Every stage-1 condition
    had a test that configured its forbidden state and asserted the refusal, and
    `test_a_deployed_production_import_is_refused_by_stage_one` above proved a real
    deployed import refuses -- but *nothing* asserted that some environment exists
    in which it does not. So `base.py` keeping `ModelBackend` and
    `ACCOUNT_LOGIN_METHODS` read, to every green run, as the contract working: the
    refusal fired, which is what the suite was watching for.

    A refusal suite without this case cannot distinguish "refuses the forbidden
    state" from "refuses everything", which is the FR-16 blind spot Story 4.5's
    positive controls close per condition. This closes it for the composition as a
    whole: the nine conditions have to be jointly satisfiable by a real settings
    module, not only individually satisfiable by nine hand-built namespaces.

    Locality is declared by deleting `COMPONENT_RUNTIME` rather than by setting it,
    because locality fails closed (AD-13) and absent is how a deployment spells
    itself -- so this composition is judged by every condition, exactly as a real
    one is.
    """
    for name, value in DEPLOYED_ENVIRONMENT.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv(RUNTIME_ENV_VAR, raising=False)

    production = importlib.import_module(PRODUCTION)

    assert production.DEBUG is False
    assert production.AUTHENTICATION_BACKENDS == [ALLAUTH_BACKEND]
    assert not production.ACCOUNT_LOGIN_METHODS


@pytest.mark.usefixtures("production_env")
def test_a_local_production_import_reaches_stage_one_and_is_accepted(monkeypatch: pytest.MonkeyPatch):
    """The paired positive, and the reason the case above means anything.

    Every stage-1 condition is deployed-only, so the whole stage returns before
    any of them runs when locality is local. Without this, the refusal above
    would pass just as well against a `run_stage_one` that raised
    unconditionally -- which is the version that makes `pixi run manage`
    impossible on a fresh clone.

    Locality is declared explicitly rather than inherited from the pixi `dev`
    feature's `COMPONENT_RUNTIME=local`, so the case states the condition it
    depends on instead of relying on the environment the suite happens to run
    in.
    """
    monkeypatch.setenv("DATABASE_URL", "postgres://user:pw@db:5432/app")
    monkeypatch.setenv(RUNTIME_ENV_VAR, LOCAL_RUNTIME)

    production = importlib.import_module(PRODUCTION)

    assert production.DATABASES["default"]["ENGINE"].endswith("postgresql")


# ---------------------------------------------------------------------------
# Story 2.7 -- the Bearer credential's wiring and its configuration.
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("no_database_env", "no_oidc_env")
def test_the_bearer_class_is_asked_before_the_session():
    """AC #1 / Task 4: a request carrying both credentials is decided by the Bearer one.

    Order is the assertion, not membership. The class returns None rather than
    raising when no Bearer header is present, so placing it first costs a
    session-authenticated request nothing -- while placing it *after*
    `SessionAuthentication` would mean a stale session cookie decided a request
    that presented a fresh token.
    """
    base = importlib.import_module(BASE)

    classes = list(base.REST_FRAMEWORK["DEFAULT_AUTHENTICATION_CLASSES"])

    assert classes[0] == "config.authorization.authentication.OIDCBearerAuthentication"
    assert classes.index("rest_framework.authentication.SessionAuthentication") > 0


def _assert_no_static_token_surface(module) -> None:
    """Assert a settings module declares neither half of the retired token surface.

    Args:
        module: An imported settings module.

    """
    assert "rest_framework.authtoken" not in module.INSTALLED_APPS
    assert not [
        entry for entry in module.REST_FRAMEWORK["DEFAULT_AUTHENTICATION_CLASSES"] if "TokenAuthentication" in entry
    ]


@pytest.mark.usefixtures("no_database_env", "no_oidc_env")
def test_the_static_token_credential_is_gone():
    """FR-6 / Story 2.8: the locally minted credential is no longer one of the defaults.

    Read off `base.py` rather than the loaded settings so the removal is asserted
    where it is declared -- `tests/unit/test_credential_surface.py` makes the
    same assertion against the settings in force, and against the URLconf the
    minting route was mounted in.
    """
    _assert_no_static_token_surface(importlib.import_module(BASE))


@pytest.mark.usefixtures("no_database_env", "no_oidc_env")
def test_the_static_token_credential_is_gone_from_local_too():
    """`local.py` mutates `INSTALLED_APPS` three times, so inheriting the removal is not assumed.

    A removal asserted only against `base` is a removal that a
    `INSTALLED_APPS += [...]` one module down can undo with nothing to catch it.
    """
    _assert_no_static_token_surface(importlib.import_module(LOCAL))


@pytest.mark.usefixtures("no_oidc_env", "production_env")
def test_the_static_token_credential_is_gone_from_production(monkeypatch: pytest.MonkeyPatch):
    """The settings module that actually ships is the one the story's goal is stated about.

    `production.py` appends to `INSTALLED_APPS` as well, and AD-24 forbids making
    the removal conditional on locality. This is what would notice if it were.
    """
    monkeypatch.setenv("DATABASE_URL", "postgres://user:pw@db:5432/app")

    _assert_no_static_token_surface(importlib.import_module(PRODUCTION))


@pytest.mark.usefixtures("no_database_env", "no_oidc_env")
def test_the_rest_framework_block_keeps_everything_else_it_declared():
    """The permission default and the schema class are not this story's to move."""
    base = importlib.import_module(BASE)

    assert base.REST_FRAMEWORK["DEFAULT_PERMISSION_CLASSES"] == ("rest_framework.permissions.IsAuthenticated",)
    assert base.REST_FRAMEWORK["DEFAULT_SCHEMA_CLASS"] == "drf_spectacular.openapi.AutoSchema"
    # Both API roots since `CPM-APP-S14`: the platform's at `/api/` and this
    # application's at `/conda-sentinel/api/<version>/`. A rule naming only the first
    # would leave every browser-based caller of this product's API failing preflight,
    # and silently -- a CORS rule that matches nothing raises nothing.
    assert base.CORS_URLS_REGEX == r"^(/conda-sentinel)?/api/.*$"


@pytest.mark.usefixtures("no_database_env", "no_oidc_env")
def test_the_bearer_configuration_defaults_are_the_declared_ones():
    """AD-23's windows, and an algorithm allowlist that never comes from the token."""
    base = importlib.import_module(BASE)

    assert base.OIDC_ALGORITHMS == ["RS256"]
    assert base.JWKS_TTL_SECONDS == DEFAULT_JWKS_TTL_SECONDS
    assert base.JWKS_MIN_REFETCH_SECONDS == DEFAULT_JWKS_MIN_REFETCH_SECONDS
    # The clock-skew lever ships pulled all the way back. A default above zero
    # would be a change to the verification posture wearing a setting's clothes.
    assert base.OIDC_LEEWAY_SECONDS == DEFAULT_OIDC_LEEWAY_SECONDS
    # Unset means unconfigured, which refuses every token. It is never defaulted
    # to a conventional issuer or to "any audience".
    assert base.OIDC_ISSUER == ""
    assert base.OIDC_AUDIENCE == ""
    assert base.OIDC_JWKS_URL == ""


@pytest.mark.usefixtures("no_database_env")
def test_there_is_one_issuer_variable_and_both_consumers_read_it(monkeypatch: pytest.MonkeyPatch):
    """AD-23: the trust anchor is single, so the provider and the Bearer path read one name."""
    monkeypatch.setenv("COMPONENT_OIDC_ISSUER", "https://idp.example.test/realms/component")

    base = importlib.import_module(BASE)

    assert base.OIDC_ISSUER == "https://idp.example.test/realms/component"
    assert _oidc_app(base)["settings"]["server_url"] == base.OIDC_ISSUER  # type: ignore[index]


@pytest.mark.usefixtures("no_database_env")
def test_the_audience_falls_back_to_the_client_id(monkeypatch: pytest.MonkeyPatch):
    """An IdP issuing tokens for this component's own client puts the client id in `aud`."""
    monkeypatch.delenv("COMPONENT_OIDC_AUDIENCE", raising=False)
    monkeypatch.setenv("COMPONENT_OIDC_CLIENT_ID", "component-web")

    base = importlib.import_module(BASE)

    assert base.OIDC_AUDIENCE == "component-web"


@pytest.mark.usefixtures("no_database_env")
def test_an_explicit_audience_wins_over_the_client_id(monkeypatch: pytest.MonkeyPatch):
    """A deployment whose IdP names a separate resource server sets the variable."""
    monkeypatch.setenv("COMPONENT_OIDC_CLIENT_ID", "component-web")
    monkeypatch.setenv("COMPONENT_OIDC_AUDIENCE", "component-api")

    base = importlib.import_module(BASE)

    assert base.OIDC_AUDIENCE == "component-api"


@pytest.mark.usefixtures("no_database_env")
def test_the_jwks_windows_are_environment_driven(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("COMPONENT_JWKS_TTL_SECONDS", "900")
    monkeypatch.setenv("COMPONENT_JWKS_MIN_REFETCH_SECONDS", "30")
    monkeypatch.setenv("COMPONENT_OIDC_ALGORITHMS", "RS256,ES256")

    base = importlib.import_module(BASE)

    assert base.JWKS_TTL_SECONDS == OVERRIDDEN_JWKS_TTL_SECONDS
    assert base.JWKS_MIN_REFETCH_SECONDS == OVERRIDDEN_JWKS_MIN_REFETCH_SECONDS
    assert base.OIDC_ALGORITHMS == ["RS256", "ES256"]


@pytest.mark.usefixtures("no_database_env", "no_oidc_env")
@pytest.mark.parametrize("configured", ["0", "-1", "-3600"])
def test_a_zero_or_negative_refetch_window_is_clamped_rather_than_honoured(
    monkeypatch: pytest.MonkeyPatch,
    configured: str,
):
    """A window at or below zero disables the rate limit outright.

    `now - last_attempt < window` is false for every caller once the window is
    zero or negative, so each unmatched `kid` produces an outbound fetch -- which
    is precisely the amplification against the IdP's JWKS endpoint that AD-23
    built this module to prevent, re-armed by one environment variable. It is
    clamped rather than refused at startup because a running component with a
    slightly-too-short window is strictly better than one that will not boot,
    and clamped rather than trusted because the value arrives from a deployment
    environment nobody reviews line by line.
    """
    monkeypatch.setenv("COMPONENT_JWKS_MIN_REFETCH_SECONDS", configured)

    base = importlib.import_module(BASE)

    assert base.JWKS_MIN_REFETCH_SECONDS == FLOOR_JWKS_MIN_REFETCH_SECONDS


@pytest.mark.usefixtures("no_database_env", "no_oidc_env")
@pytest.mark.parametrize("configured", ["0", "-1"])
def test_a_zero_or_negative_cache_lifetime_is_clamped_rather_than_honoured(
    monkeypatch: pytest.MonkeyPatch,
    configured: str,
):
    """At or below zero every lookup sees the cache as expired, so nothing is ever cached."""
    monkeypatch.setenv("COMPONENT_JWKS_TTL_SECONDS", configured)

    base = importlib.import_module(BASE)

    assert base.JWKS_TTL_SECONDS == FLOOR_JWKS_TTL_SECONDS


@pytest.mark.usefixtures("no_database_env", "no_oidc_env")
def test_the_clock_skew_tolerance_is_environment_driven(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("COMPONENT_OIDC_LEEWAY_SECONDS", "5")

    base = importlib.import_module(BASE)

    assert base.OIDC_LEEWAY_SECONDS == OVERRIDDEN_OIDC_LEEWAY_SECONDS


@pytest.mark.usefixtures("no_database_env", "no_oidc_env")
def test_a_negative_clock_skew_tolerance_is_clamped_to_zero(monkeypatch: pytest.MonkeyPatch):
    """PyJWT takes `leeway` as a magnitude, so a negative value is a nonsense the reader owns."""
    monkeypatch.setenv("COMPONENT_OIDC_LEEWAY_SECONDS", "-30")

    base = importlib.import_module(BASE)

    assert base.OIDC_LEEWAY_SECONDS == DEFAULT_OIDC_LEEWAY_SECONDS


# ---------------------------------------------------------------------------
# Story 3.2 -- the cache and task substitutions (FR-18, FR-22).
#
# The database substitution is asserted in `tests/unit/test_database_selection.py`,
# which owns that contract; these two are the other halves of the same "nothing
# has to be running" claim. `base.py` declares no `CACHES` and no `CELERY_TASK_*`
# at all, so every attribute read below fails outright if the declaration is
# removed from the module under test rather than falling through to an inherited
# value -- which is exactly why `test.py` declares them instead of relying on
# Django's implicit LocMemCache default.
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("no_database_env")
def test_local_configures_an_in_process_cache():
    """FR-18: with no Redis running, the cache is in-process rather than absent.

    The substitution is a backend swap and nothing else -- the cache API is
    unchanged, so no call site branches on which of the two is active. What this
    pins is that a backend *is* named locally: falling back to Django's implicit
    default would work by accident and would be undone silently the day `base.py`
    grows a `CACHES` key of its own.
    """
    local = importlib.import_module(LOCAL)

    assert local.CACHES["default"]["BACKEND"] == LOCMEM_CACHE_BACKEND


@pytest.mark.usefixtures("no_database_env")
def test_local_executes_tasks_eagerly_and_propagates():
    """FR-18 / FR-22: no broker locally, in every valid combination.

    Both settings are asserted because eager alone is not the substitution.
    Eager execution captures a raised exception into the `EagerResult` instead
    of re-raising it, so a caller that does not inspect the result sees a failing
    task as a passing one; propagation is what makes the synchronous call behave
    like the call it stands in for.
    """
    local = importlib.import_module(LOCAL)

    assert local.CELERY_TASK_ALWAYS_EAGER is True
    assert local.CELERY_TASK_EAGER_PROPAGATES is True


@pytest.mark.usefixtures("no_database_env", "no_cache_url_env")
def test_the_test_settings_declare_the_same_substitutions_rather_than_inheriting_them():
    """The suite runs under `config.settings.test`, so it owes the same three.

    Written against the settings module the suite itself loads: if these three
    were left implicit, the substitutions the rest of this story documents would
    be true of `local.py` and merely *probably* true of the environment every
    test in this repository actually executes in.
    """
    test_settings = importlib.import_module(TEST)

    assert test_settings.DATABASES["default"]["ENGINE"].endswith("sqlite3")
    assert test_settings.CACHES["default"]["BACKEND"] == LOCMEM_CACHE_BACKEND
    assert test_settings.CELERY_TASK_ALWAYS_EAGER is True
    assert test_settings.CELERY_TASK_EAGER_PROPAGATES is True


@pytest.mark.usefixtures("no_database_env")
def test_a_declared_redis_url_selects_the_deployed_cache_backend(monkeypatch: pytest.MonkeyPatch):
    """CPM-EVIDENCE-S08: one variable moves the suite onto a real cache, and nothing else does.

    The shape `DATABASE_URL` already has for the database: no settings change,
    no feature flag, one environment variable read in one place. It exists
    because `core/rate_limit.py`'s add-then-incr sequence is written for two
    *processes* sharing one counter, and under LocMem each process holds its own
    -- so the property cannot fail and the reasoning is unproven until the suite
    can be pointed at a real Redis.

    The client class is asserted as well as the backend. `config/settings/
    production.py` configures `DefaultClient`, and a proof run against a
    different client than the deployed one would be a statement about a
    configuration nothing ships.
    """
    monkeypatch.setenv(CACHE_URL_VARIABLE, A_REDIS_URL)

    test_settings = importlib.import_module(TEST)

    assert test_settings.CACHES["default"]["BACKEND"] == REDIS_CACHE_BACKEND
    assert test_settings.CACHES["default"]["LOCATION"] == A_REDIS_URL
    assert test_settings.CACHES["default"]["OPTIONS"]["CLIENT_CLASS"] == "django_redis.client.DefaultClient"


@pytest.mark.usefixtures("no_database_env")
def test_an_empty_redis_url_falls_back_rather_than_selecting_nothing(monkeypatch: pytest.MonkeyPatch):
    """`CPM_TEST_REDIS_URL=""` is not a selection -- the branch is truthiness.

    The sibling of `test_an_empty_database_url_falls_back_rather_than_selecting_nothing`,
    and the same single edit: exported-but-empty is the one value that would
    leave the variable present, every gate assertion green, and the suite back on
    the substitution with AC 4's proof silently skipped.
    """
    monkeypatch.setenv(CACHE_URL_VARIABLE, "")

    test_settings = importlib.import_module(TEST)

    assert test_settings.CACHES["default"]["BACKEND"] == LOCMEM_CACHE_BACKEND


@pytest.mark.usefixtures("no_database_env")
def test_the_redis_branch_does_not_swallow_cache_errors(monkeypatch: pytest.MonkeyPatch):
    """The one thing the suite's Redis configuration must *not* copy from production.

    `config/settings/production.py` sets `IGNORE_EXCEPTIONS` so that a cache
    outage degrades rather than fails, which is right for a deployment and wrong
    for a gate: a suite that silently swallowed a Redis error would report a
    passing run for a service that never came up, and AC 4's proof would be a
    case that connected to nothing and agreed with itself.
    """
    monkeypatch.setenv(CACHE_URL_VARIABLE, A_REDIS_URL)

    test_settings = importlib.import_module(TEST)

    assert test_settings.CACHES["default"]["OPTIONS"].get("IGNORE_EXCEPTIONS") is not True


# ---------------------------------------------------------------------------
# Story 6.1 -- the logging invariant and the Celery correlation switch.
# ---------------------------------------------------------------------------

#: The one settings module whose `LOGGING` this suite reads as text. The Celery
#: block's boundaries are a property of *position*, which the imported module
#: cannot show: every name in it is an attribute of one flat namespace whether
#: it was written above the banner or below it.
BASE_SETTINGS_SOURCE = REPO_ROOT / "src" / "config" / "settings" / "base.py"

CELERY_BANNER = "# Celery"
NEXT_BANNER = "# django-allauth"
CELERY_SWITCH = "DJANGO_STRUCTLOG_CELERY_ENABLED"

# The inherited Celery limits, in seconds, named rather than written at the
# assertion so the numbers read as the policy CPM-AD-9 states: five minutes hard,
# sixty seconds soft, and work that would exceed them chunked per package.
INHERITED_TASK_TIME_LIMIT_SECONDS = 300
INHERITED_TASK_SOFT_TIME_LIMIT_SECONDS = 60

# Where cadence lives (CPM-AD-20). The scheduler that reads its schedules from
# database rows an operator can edit, rather than from a decorator that needs a
# deploy to change.
DATABASE_SCHEDULER = "django_celery_beat.schedulers:DatabaseScheduler"

# The one Celery key this product contributes to is `queues.CONTRIBUTED_SETTING_KEY`
# and is not respelled here: it is the module's own contract, two test modules
# assert its membership of `CONTRIBUTABLE_KEYS`, and one string spelled in two
# places is the failure `core/queues.py` exists to prevent, applied to itself.


def _assignment_lines(lines: list[str]) -> list[tuple[int, str]]:
    """Return the top-level assignment lines of a settings module's source.

    Indented lines are excluded because they belong to a block rather than to
    the module's own sequence of settings, and comments and blanks because the
    property under test is which *settings* separate two markers.

    Args:
        lines: The module source, already split into lines.

    Returns:
        `(index, line)` pairs for every top-level assignment.

    """
    return [
        (index, line)
        for index, line in enumerate(lines)
        if line and not line[0].isspace() and not line.startswith("#") and "=" in line
    ]


@pytest.mark.usefixtures("production_env")
def test_the_production_logging_config_writes_no_files(monkeypatch: pytest.MonkeyPatch):
    """AC #1, over the dictionary production actually ships.

    `tests/unit/test_observability_logging.py` asserts the same property over a
    literal argument list shaped like production's. That cannot catch production
    adding a file handler of its own later; this can, because it reads the
    module's own `LOGGING` after the module has finished building it -- including
    the `filters` key production bolts on afterwards.
    """
    monkeypatch.setenv("DATABASE_URL", "postgres://user:pw@db:5432/app")

    production = importlib.import_module(PRODUCTION)

    assert_writes_no_files(production.LOGGING)
    assert production.LOGGING["root"]["handlers"] == ["console"]


def test_the_celery_correlation_switch_sits_inside_the_celery_block():
    """AD-24: the switch has to be enclosable in one `feature:celery` region.

    Asserted textually, and that is the point rather than an expedient: the
    property is *position within a block*, and an imported settings module has
    no positions -- `DJANGO_STRUCTLOG_CELERY_ENABLED` reads identically whether
    it was written three lines above the `# Celery` banner or three below it.
    Only the source says which, and only the source is what Epic 7 encloses in a
    marker pair.

    Three claims, because the first alone would be satisfied by the switch
    landing anywhere later in the file: it follows the banner, nothing but
    `CELERY_`-prefixed settings separates the two, and it precedes the next
    section. An edit that moved it back above `REDIS_URL` fails the first; one
    that moved it past the allauth banner fails the third.
    """
    lines = BASE_SETTINGS_SOURCE.read_text(encoding="utf-8").splitlines()

    banner = lines.index(CELERY_BANNER)
    switch = next(index for index, line in enumerate(lines) if line.startswith(CELERY_SWITCH))
    next_banner = lines.index(NEXT_BANNER)

    assert banner < switch, f"{CELERY_SWITCH} is written above the {CELERY_BANNER} banner"
    assert switch < next_banner, f"{CELERY_SWITCH} is written past the {NEXT_BANNER} banner"

    intervening = [line for index, line in _assignment_lines(lines) if banner < index < switch]
    assert all(line.startswith("CELERY_") for line in intervening), (
        f"non-Celery settings separate the banner from {CELERY_SWITCH}: {intervening}"
    )


# ---------------------------------------------------------------------------
# CPM-EVIDENCE-S04 -- the three queues, and cadence as data.
# ---------------------------------------------------------------------------


@pytest.fixture
def any_settings_module(monkeypatch: pytest.MonkeyPatch) -> None:
    """An environment under which all four settings modules import.

    The four are not interchangeable to import: `production.py` refuses without
    `DJANGO_SECRET_KEY` and `DJANGO_ADMIN_URL`, and every module needs a database
    URL it will accept. One fixture that satisfies all of them is what lets the
    Celery assertions below be *parametrized over the four* rather than written
    against `base` alone -- and that is not tidiness. A `CELERY_BEAT_SCHEDULE` or
    a raised `CELERY_TASK_TIME_LIMIT` in `production.py` is the one that would
    actually reach a deployed component, and it is exactly the one a case reading
    only `base` cannot see.
    """
    monkeypatch.setenv("DJANGO_SECRET_KEY", "x" * 50)
    monkeypatch.setenv("DJANGO_ADMIN_URL", "admin/")
    monkeypatch.setenv("DATABASE_URL", "postgres://user:pw@db:5432/app")


#: The four settings modules, as one list the Celery cases parametrize over.
EVERY_SETTINGS_MODULE = (BASE, LOCAL, PRODUCTION, TEST)

#: The two settings the published-conda-package collector reads its monitored
#: surfaces from, read from the collector that reads them rather than spelled
#: again here: a literal in this file would be a second declaration of a name,
#: and it would keep passing while the two drifted apart.
MONITORED_SETTINGS = (CHANNELS_SETTING, PLATFORMS_SETTING)

#: The keys `CELERY_BEAT_SCHEDULE` carries, one per per-package collector
#: (`CPM-CURRENCY-S05`). Named here because a *key* is the one part of an entry
#: that names nothing else in the repository -- `django_celery_beat` uses it as
#: the periodic task's own name -- so unlike the task name, the keyword and the
#: cadences below there is nothing to reconcile it against, and an entry
#: disappearing would otherwise be invisible to the equality on the collectors.
EXPECTED_SWEEP_ENTRIES = (
    "cpm-sweep-source-release",
    "cpm-sweep-pypi-release",
    "cpm-sweep-feedstock",
    "cpm-sweep-conda-package",
    "cpm-sweep-vulnerability",
    "cpm-sweep-kev",
    "cpm-sweep-license",
    "cpm-sweep-python-readiness",
    "cpm-sweep-resolve-identity",
)


@pytest.mark.usefixtures("no_database_env")
def test_the_celery_block_contributes_the_route_table_core_owns():
    """`CPM-AD-20`: the route table is `core`'s, and settings is where it is installed.

    Asserted as identity against the module `core` declares rather than against a
    literal table written out here. A copy in this file would be a second
    declaration of what routes where -- the exact failure the module exists to
    prevent -- and it would pass while the two drifted.

    Inherited `AD-8` is honoured by the *key*: `CELERY_TASK_ROUTES` is on the
    `CONTRIBUTABLE_KEYS` roster a domain application may contribute to. The
    membership is read from the allowlist rather than restated, and the roster's
    length is deliberately not quoted here -- a count written into a docstring is
    true until the day the roster changes and silently wrong afterwards. The
    composition step that would apply such a contribution is the platform's
    Epic 9 and is not built, so the entry is written by hand here on the same
    terms the `LOCAL_APPS` adoption already is.
    """
    base = importlib.import_module(BASE)

    assert base.CELERY_TASK_ROUTES is queues.CELERY_TASK_ROUTES
    assert queues.CONTRIBUTED_SETTING_KEY in CONTRIBUTABLE_KEYS


@pytest.mark.usefixtures("any_settings_module")
@pytest.mark.parametrize("module", EVERY_SETTINGS_MODULE, ids=lambda name: name.rpartition(".")[2])
def test_the_inherited_celery_time_limits_are_unchanged(module: str):
    """`CPM-AD-9`: work that would exceed them is chunked, never given a longer limit.

    Pinned rather than left to the platform, because this story's whole argument
    for the queue split is that a five-minute `verify` build is *allowed* to take
    five minutes and must not do it beside the daily sweep. Raise the limit and
    the argument changes: the starvation window grows and the per-package
    transaction boundary `CPM-AD-23` fixes stops being reachable.

    Over all four modules, not `base` alone. `base` is the only one that declares
    either value today, and `production.py` is the one whose value a deployed
    component actually runs under -- so a case that read `base` would be checking
    the module where an override is least likely and skipping the one where it
    matters.

    `tests/unit/django_apps/test_task_declaration_audit.py` holds the other half
    -- that no individual task, and no module outside settings, overrides either.
    """
    settings_module = importlib.import_module(module)

    assert settings_module.CELERY_TASK_TIME_LIMIT == INHERITED_TASK_TIME_LIMIT_SECONDS
    assert settings_module.CELERY_TASK_SOFT_TIME_LIMIT == INHERITED_TASK_SOFT_TIME_LIMIT_SECONDS


@pytest.mark.usefixtures("any_settings_module")
@pytest.mark.parametrize("module", EVERY_SETTINGS_MODULE, ids=lambda name: name.rpartition(".")[2])
def test_cadence_lives_in_the_database_scheduler(module: str):
    """`CPM-AD-20` / `CPM-NFR-2`: a schedule is data an operator can change.

    The database scheduler is what makes that true: a cadence lives in
    `django_celery_beat`'s tables, seeded from the declaration below and editable
    afterwards without a deploy. A decorator-borne schedule is the alternative
    this forecloses, and the source sweep in
    `tests/unit/django_apps/test_task_declaration_audit.py` is the gate on it --
    `config/settings/` is the one directory that sweep permits a schedule in,
    which is why this reads all four modules rather than `base` alone: a cadence
    added to `production.py` is inside the carve-out, parsed by nothing, and is
    the one that would actually reach a deployed component.

    `CPM-CURRENCY-S05` is what put entries here. Every leaf module inherits them
    through `from .base import *`, and none may drop or override them silently --
    a component whose schedule and whose collectors disagree is refused at
    start-up by `CollectorsConfig.ready()`, which is the hook that registers the
    collectors and therefore the only one a deployed process reaches with a
    populated registry (`config/startup/stage_two.py` evaluates the same rule as
    condition 11). A component with *no* entries is refused there too, which is
    the half this case pins from the settings side.
    """
    settings_module = importlib.import_module(module)

    assert settings_module.CELERY_BEAT_SCHEDULER == DATABASE_SCHEDULER
    assert set(settings_module.CELERY_BEAT_SCHEDULE) == set(EXPECTED_SWEEP_ENTRIES)


@pytest.mark.usefixtures("any_settings_module")
@pytest.mark.parametrize("module", EVERY_SETTINGS_MODULE, ids=lambda name: name.rpartition(".")[2])
def test_every_schedule_entry_fires_the_dispatch_task_by_the_name_it_declares(module: str):
    """The anti-drift half: settings' literals are reconciled against the module that owns them.

    A settings module cannot import a collector -- these modules execute before
    the app registry exists and every collector module reaches a Django model --
    so the task name and the collector keyword are written out in
    `config/settings/base.py` as literals. That is a deliberate second spelling,
    and the start-up reconciliation in `CollectorsConfig.ready()` is what makes it
    safe in a deployed process. This is what makes it safe at gate time: the
    literals are compared against `collectors/sweep.py`'s own declarations, so a
    rename on either side fails here rather than shipping a beat entry that fires
    into nothing on every tick.
    """
    settings_module = importlib.import_module(module)

    for entry in settings_module.CELERY_BEAT_SCHEDULE.values():
        assert entry["task"] == SWEEP_TASK_NAME
        assert set(entry["kwargs"]) == {COLLECTOR_KWARG}


@pytest.mark.usefixtures("any_settings_module")
def test_the_schedule_dispatches_each_per_package_collector_exactly_once():
    """One entry per collector, and the cadences are the collectors' own.

    Asserted against the nine collector modules' declared cadences rather than
    against intervals written out here: a literal in this file would be a third
    spelling of a number that already lives in two places, and it would keep
    passing while the schedule and the collectors drifted.
    `CollectorsConfig.ready()` is the deployed enforcement, and
    `tests/integration/startup/test_collector_boot_refusal.py` proves it in a
    child process; this is the one that fails a pull request.

    Inventory ingestion is deliberately not in the mapping. It is run-scoped --
    one document naming many packages (`CPM-AD-25`) -- so it declares no cadence
    and a per-package dispatch refuses it by name.
    """
    base = importlib.import_module(BASE)

    dispatched = {entry["kwargs"][COLLECTOR_KWARG]: entry["schedule"] for entry in base.CELERY_BEAT_SCHEDULE.values()}

    assert dispatched == {
        SOURCE_RELEASE_NAME: SourceReleaseCollector.cadence,
        PYPI_RELEASE_NAME: PyPIReleaseCollector.cadence,
        FEEDSTOCK_NAME: FeedstockCollector.cadence,
        CONDA_PACKAGE_NAME: CondaPackageCollector.cadence,
        VULNERABILITY_NAME: VulnerabilityCollector.cadence,
        KEV_NAME: KevCollector.cadence,
        LICENSE_NAME: LicenseCollector.cadence,
        PYTHON_READINESS_NAME: PythonReadinessCollector.cadence,
        RESOLVE_IDENTITY_NAME: IdentityResolutionCollector.cadence,
    }
    assert len(base.CELERY_BEAT_SCHEDULE) == len(dispatched)


@pytest.mark.usefixtures("any_settings_module")
def test_the_phased_dispatches_are_exactly_the_three_that_declare_an_offset():
    """`CPM-SECURITY-S02`, `CPM-SECURITY-S03` and `CPM-PY314-S01`: the sweeps that must not fire on the tick.

    The KEV sweep cross-references what the vulnerability sweep wrote, so firing
    them from one instant means a KEV run reads the previous day's advisories -- an
    answer one cadence behind, with nothing saying so. The licence sweep reads the
    same host `CPM-CURRENCY-S04`'s published-package sweep reads and spends a
    separate allowance against it, so firing them together spends both at once. The
    static-readiness sweep reads the same host `CPM-CURRENCY-S02`'s PyPI sweep reads
    and spends its own allowance against it, so one day in seven the two would begin
    at one instant. The reconciliation above compares an entry's `schedule` with its collector's
    declared cadence, so the interval cannot carry a phase and a crontab cannot be
    read as an interval; beat passes an entry's `options` to `apply_async`, so a
    countdown on the dispatch is the only phase this schedule can express.

    Asserted against each collector module's own constant rather than against the
    number, for the reason every cadence here is: a literal in this file would be a
    second spelling that keeps passing while the two drift. And asserted as
    *exactly* those three entries, because a fourth one appearing without a reason is
    a schedule nobody decided.

    The offsets are asserted **pairwise distinct** as well as declared, which is the
    half a per-entry case would miss: two entries sharing a phase fire together
    again, and the offset then buys nothing while every other assertion here still
    passes.
    """
    base = importlib.import_module(BASE)

    phased = {
        entry["kwargs"][COLLECTOR_KWARG]: entry["options"]["countdown"]
        for entry in base.CELERY_BEAT_SCHEDULE.values()
        if "options" in entry
    }

    assert phased == {
        KEV_NAME: int(KEV_DISPATCH_OFFSET.total_seconds()),
        LICENSE_NAME: int(LICENSE_DISPATCH_OFFSET.total_seconds()),
        PYTHON_READINESS_NAME: int(READINESS_DISPATCH_OFFSET.total_seconds()),
    }
    assert KevCollector.cadence > KEV_DISPATCH_OFFSET
    assert LicenseCollector.cadence > LICENSE_DISPATCH_OFFSET
    assert PythonReadinessCollector.cadence > READINESS_DISPATCH_OFFSET
    declared = (KEV_DISPATCH_OFFSET, LICENSE_DISPATCH_OFFSET, READINESS_DISPATCH_OFFSET)
    assert len(set(declared)) == len(declared)


# ---------------------------------------------------------------------------
# CPM-CURRENCY-S04 -- the monitored channels and platforms, declared and empty.
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("any_settings_module")
@pytest.mark.parametrize("module", EVERY_SETTINGS_MODULE, ids=lambda name: name.rpartition(".")[2])
@pytest.mark.parametrize("setting", MONITORED_SETTINGS)
def test_the_monitored_surfaces_are_declared_and_ship_empty(module: str, setting: str):
    """PRD Open Question 4: the mechanism ships, the choice does not (`CPM-FR-10`).

    Two halves, and both are load bearing.

    *Declared.* `CollectorsConfig.ready()` refuses to boot a settings module that
    carries neither name, so a module that dropped one would be a component that
    will not start. Asserted over all four rather than over `base` alone: the leaf
    modules inherit through `from .base import *`, and the one whose value a
    deployed component actually runs under is `production.py` -- exactly the one a
    case reading only `base` cannot see.

    *Empty.* Which conda channels and platforms this product watches is an
    operator's decision and is unresolved. A default here would answer it, and the
    component would record evidence about a surface nobody chose -- permanently,
    in an append-only log nothing may correct. What the emptiness buys is a run
    that fails loudly naming the setting, which
    `tests/integration/django_apps/test_conda_package.py` asserts end to end.
    """
    settings_module = importlib.import_module(module)

    declared = getattr(settings_module, setting)

    assert declared == ()
    # A tuple rather than a list, and asserted rather than assumed: a mutable
    # default in a settings module is one an importer can append to, and "which
    # surfaces does this component observe" is not a question an import order may
    # answer.
    assert isinstance(declared, tuple)


@pytest.mark.usefixtures("any_settings_module")
@pytest.mark.parametrize("setting", MONITORED_SETTINGS)
def test_the_monitored_surfaces_are_the_names_the_collector_reads(setting: str):
    """The anti-vacuity half: settings and the collector must name the same two settings.

    The case above would pass just as happily against two names nothing reads.
    What ties them together is that the collector declares the names and this
    asserts the settings module assigns *those* -- so a rename on either side is a
    failure here rather than a component that boots, starts, and silently observes
    nothing for ever.
    """
    settings_module = importlib.import_module(BASE)

    assert hasattr(settings_module, setting)
