# ruff: noqa: ERA001
"""Base settings to build other settings files upon."""

import os
import ssl
from datetime import timedelta
from pathlib import Path
from typing import Any

import environ
from django.urls import reverse_lazy

from conda_sentinel.collectors.watchlist import watchlist_path
from conda_sentinel.core import queues
from conda_sentinel.core.roles import load_role_contract
from config.authorization.claims import load_claims_contract
from config.locality import is_local
from config.observability.logging import build_logging_config
from config.observability.logging import configure_structlog

# Repository root (holds manage.py, pyproject.toml, .env).
BASE_DIR = Path(__file__).resolve(strict=True).parent.parent.parent.parent
# The django_service package: holds the apps, templates, static and media.
# src/ itself is the import root and is deliberately not a package.
APPS_DIR = BASE_DIR / "src" / "django_service"
env = environ.Env()

READ_DOT_ENV_FILE = env.bool("DJANGO_READ_DOT_ENV_FILE", default=False)
if READ_DOT_ENV_FILE:
    # OS environment variables take precedence over variables from .env
    env.read_env(str(BASE_DIR / ".env"))

# GENERAL
# ------------------------------------------------------------------------------
# https://docs.djangoproject.com/en/dev/ref/settings/#debug
DEBUG = env.bool("DJANGO_DEBUG", False)
# Local time zone. Choices are
# http://en.wikipedia.org/wiki/List_of_tz_zones_by_name
# though not all of them may be available with every OS.
# In Windows, this must be set to your system time zone.
TIME_ZONE = "UTC"
# https://docs.djangoproject.com/en/dev/ref/settings/#language-code
LANGUAGE_CODE = "en-us"
# https://docs.djangoproject.com/en/dev/ref/settings/#languages
# from django.utils.translation import gettext_lazy as _
# LANGUAGES = [
#     ('en', _('English')),
#     ('fr-fr', _('French')),
#     ('pt-br', _('Portuguese')),
# ]
# https://docs.djangoproject.com/en/dev/ref/settings/#site-id
# Kept, and the `sites` app with it. allauth resolves a provider app through
# `SocialApp.objects.on_site(request)` on every lookup, so the table has to
# exist even though AD-31 forbids a row ever being the provider's source.
SITE_ID = 1
# The `Site` domain, environment-driven (AD-31). The data migration that used to
# write it into the database is retired -- see
# django_service/contrib/sites/migrations/0003_set_site_domain_and_name.py --
# because a baked-in domain travels into every deployed component and decides
# what callback URL it advertises. Nothing writes these values to the `Site`
# row: NFR-1 keeps startup free of queries beyond migration state and AD-22
# forbids a boot-time write, so these settings are the source of truth and the
# table exists only for allauth's `on_site` lookup.
SITE_DOMAIN = env.str("COMPONENT_SITE_DOMAIN", default="localhost")
SITE_NAME = env.str("COMPONENT_SITE_NAME", default="localhost")
# https://docs.djangoproject.com/en/dev/ref/settings/#use-i18n
USE_I18N = True
# https://docs.djangoproject.com/en/dev/ref/settings/#use-tz
USE_TZ = True
# https://docs.djangoproject.com/en/dev/ref/settings/#locale-paths
LOCALE_PATHS = [str(BASE_DIR / "locale")]

# DATABASES
# ------------------------------------------------------------------------------
# https://docs.djangoproject.com/en/dev/ref/settings/#databases


def _sqlite_alias(base_dir: Path, alias: str = "default") -> dict[str, Any]:
    """Build the sqlite configuration that stands in for one database alias.

    The `default` alias keeps the historical `db.sqlite3` filename so an existing
    checkout's database is still the one that gets opened; every other alias gets
    its own file, because two aliases sharing one file is not a second database.

    Args:
        base_dir: The repository root, which is where the file is written.
        alias: The database alias the configuration is being built for.

    Returns:
        A Django database configuration dict for the sqlite backend.

    """
    name = base_dir / "db.sqlite3" if alias == "default" else base_dir / f"db.{alias}.sqlite3"
    return {"ENGINE": "django.db.backends.sqlite3", "NAME": str(name)}


def apply_local_database_substitution(databases: dict[str, Any], base_dir: Path) -> None:
    """Fill every unconfigured database alias with the local sqlite substitution.

    This is FR-18's database substitution, and AD-9 requires the *base* to apply
    it automatically rather than each settings module doing so for itself: a
    contributed database (Epic 9) adds an alias to `DATABASES`, and that alias
    must be substituted by this same code path instead of inventing a second
    mechanism. An alias whose configuration is already populated is left exactly
    as it is -- the substitution must never shadow a real database.

    It is called unconditionally from this module, not gated on locality and not
    moved into `local.py`, because FR-12 makes the refusal contract evaluate
    independently of which settings module loaded. What keeps that safe is the
    refusal, not a condition here: `config/settings/production.py` raises rather
    than serving a deployment off sqlite. Do not "fix" this into a local-only
    call -- doing so is what would let a production module with an unconfigured
    alias reach a caller with no database at all.

    Args:
        databases: The `DATABASES` mapping, mutated in place.
        base_dir: The repository root, passed to `_sqlite_alias`.

    """
    for alias in list(databases):
        if not databases[alias]:
            databases[alias] = _sqlite_alias(base_dir, alias)


if os.getenv("DATABASE_URL", default=None):
    DATABASES = {"default": env.db("DATABASE_URL")}
elif os.getenv("POSTGRES_DB", default=None):
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": env.str("POSTGRES_DB"),
            "USER": env.str("POSTGRES_USER"),
            "PASSWORD": env.str("POSTGRES_PASSWORD"),
            "HOST": env.str("POSTGRES_HOST", default="postgres"),
            "PORT": env.str("POSTGRES_PORT", default="5432"),
        },
    }
else:
    # Local-development fallback until Postgres is provisioned. Production
    # refuses to boot on sqlite -- see config/settings/production.py.
    DATABASES = {"default": _sqlite_alias(BASE_DIR)}

# After the selection, never instead of it: the three branches above own which
# backend `default` gets, and this only fills in aliases that ended up with no
# configuration at all. Today that is a no-op; it is the declared hook AD-9's
# contributed database extends, so that FR-18 stays true by construction rather
# than by a second epic remembering to add its own fallback.
apply_local_database_substitution(DATABASES, BASE_DIR)

DATABASES["default"]["ATOMIC_REQUESTS"] = True
# https://docs.djangoproject.com/en/stable/ref/settings/#std:setting-DEFAULT_AUTO_FIELD
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# URLS
# ------------------------------------------------------------------------------
# https://docs.djangoproject.com/en/dev/ref/settings/#root-urlconf
ROOT_URLCONF = "config.urls"
# https://docs.djangoproject.com/en/dev/ref/settings/#wsgi-application
WSGI_APPLICATION = "config.wsgi.application"

# APPS
# ------------------------------------------------------------------------------
DJANGO_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.sites",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # "django.contrib.humanize", # Handy template tags
    "django.contrib.admin",
    "django.forms",
]
THIRD_PARTY_APPS = [
    "crispy_forms",
    "crispy_bootstrap5",
    "allauth",
    "allauth.account",
    "allauth.socialaccount",
    # The OIDC provider that ships with the installed django-allauth
    # distribution (AD-31, FR-4). No second OIDC framework is added, and this
    # entry is never guarded behind an availability check -- AD-24 permits no
    # conditional import, no settings-module inheritance and no
    # `try`/`except ImportError` as a removal mechanism.
    "allauth.socialaccount.providers.openid_connect",
    "django_celery_beat",
    "rest_framework",
    "corsheaders",
    "drf_spectacular",
    "django_structlog",
]

LOCAL_APPS = [
    "django_service.users",
    # Domain applications live under the second import root, src/django_apps/,
    # as subpackages of one distribution package. Appended, never prepended:
    # `django_service.users` is the stage-2 owner (AD-26) and must stay first --
    # tests/unit/startup/test_installed_apps_ordering.py asserts it.
    #
    # `component.toml`'s `adopted_apps` is the AD-8 declaration of the same
    # adoption, but nothing consumes it into INSTALLED_APPS yet (that
    # composition step is Epic 9), so the entry here is what actually installs
    # the application today.
    "conda_sentinel.core",
    "conda_sentinel.identity",
    # The collectors application, and the first adopted application declaring a
    # `ready()` (CPM-IDENTITY-S06). It adopts its collectors into `core`'s
    # registry, which is what CPM-AD-28's stage-2 sweep walks -- so it has to
    # stay after the stage-2 owner exactly as the two above do, which appending
    # is what guarantees.
    "conda_sentinel.collectors",
    # The policy application, and the second adopted application declaring a
    # `ready()` (CPM-CURRENCY-S06). It adopts its passes into `core`'s policy
    # registry, which is what the orchestrating policy run walks -- so it has to
    # stay after the stage-2 owner exactly as the three above do, which appending
    # is what guarantees. Last, because `CPM-AD-21` keeps the pass registry in
    # declaration order: a later pass may read an earlier pass's derived rows, so
    # the order applications are adopted in is part of what is declared.
    "conda_sentinel.policies",
    # The one application that owns every queue item (`CPM-AD-22`, `CPM-APP-S04`).
    # After `policies` because the policy run opens items, and before `surface`
    # because the queues are read there. It declares no `ready()` and registers no
    # pass, so `test_policies_app.py`'s ordering rule is untouched.
    "conda_sentinel.workflow",
    # The read surfaces (`CPM-EP-APP`, `CPM-APP-S02`). Last, and after `policies`
    # rather than merely after the stage-2 owner: it reads every pass's derived
    # table by name, so it depends on those applications rather than the other way
    # round. It declares no models, no `ready()` and no migrations -- `CPM-AD-10`
    # gives the application layer no write path to a derived status, so a read
    # surface with a model of its own would be declaring the one thing the decision
    # forbids it.
    "conda_sentinel.surface",
    # Your stuff: custom apps go here
]
# https://docs.djangoproject.com/en/dev/ref/settings/#installed-apps
INSTALLED_APPS = DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS

# MIGRATIONS
# ------------------------------------------------------------------------------
# https://docs.djangoproject.com/en/dev/ref/settings/#migration-modules
MIGRATION_MODULES = {"sites": "django_service.contrib.sites.migrations"}

# AUTHENTICATION
# ------------------------------------------------------------------------------
# https://docs.djangoproject.com/en/dev/ref/settings/#authentication-backends
# Allauth's backend alone, because the base is the surface a *deployed* component
# inherits: authentication is the identity provider's (FR-4), and stage 1 refuses
# `django.contrib.auth.backends.ModelBackend` here in a deployed component
# (condition 2, state a). It carried `ModelBackend` until this story, which meant
# no deployed component could import this module at all -- `production.py` adds no
# override, so the refusal fired on every real deployment.
#
# `local.py` and `test.py` add it back, which is where the affordance it exists
# for belongs: persona sign-in hands `django.contrib.auth.login` the backend path
# in `config.local_dev.views.SESSION_BACKEND`, and `get_user` answers
# `AnonymousUser` on the next request for a backend this list does not name. A
# locality-scoped credential path is declared where the locality is, exactly as
# the cache and task substitutions are.
#
# Allauth's `AuthenticationBackend` **is** a `ModelBackend` subclass, which is why
# condition 2a compares dotted paths rather than resolving and testing
# `issubclass` -- see `config/startup/stage_one.py`.
AUTHENTICATION_BACKENDS = [
    "allauth.account.auth_backends.AuthenticationBackend",
]
# https://docs.djangoproject.com/en/dev/ref/settings/#auth-user-model
AUTH_USER_MODEL = "users.User"
# https://docs.djangoproject.com/en/dev/ref/settings/#login-redirect-url
LOGIN_REDIRECT_URL = "users:redirect"
# The id the OIDC app is registered under, read here rather than beside
# `SOCIALACCOUNT_PROVIDERS` below because `LOGIN_URL` is built from it. One
# variable, one meaning: the provider block reads this same name.
OIDC_PROVIDER_ID = env.str("COMPONENT_OIDC_PROVIDER_ID", default="oidc")
# https://docs.djangoproject.com/en/dev/ref/settings/#login-url
# FR-4 / AC #1: an unauthenticated request to an authenticated page redirects to
# the IdP, never to allauth's local credential form. `reverse_lazy` rather than
# `reverse` because a module-level reverse in a settings file runs before the
# URLconf is loaded. The local account routes are deliberately still installed
# -- deleting them is Story 2.8's, and refusing them at startup is Epic 4's.
LOGIN_URL = reverse_lazy("openid_connect_login", kwargs={"provider_id": OIDC_PROVIDER_ID})
# The claims contract (FR-10, AD-11, AD-12): the identity-key claim, the group
# claim, and the staff- and superuser-conferring groups, each read from a
# COMPONENT_-prefixed variable with no default. Unset stays unset -- an empty
# field means unconfigured, which is what Epic 4's startup check refuses on.
# See config/authorization/claims.py and docs/accelerator/authentication.md.
CLAIMS_CONTRACT = load_claims_contract(env)
# The product's role contract (CPM-FR-30): the names of the three groups that
# confer the security-and-compliance-reviewer, packaging-engineer and leadership
# roles, each read from a CPM_-prefixed variable with no default. Unset stays
# unset, exactly as above.
#
# Read *here* rather than inside the application it belongs to, for the reason
# FR-38 gives: `.env` is read once, by this module, and `env` is the only handle
# on it. An app reading its own configuration would be a second read of a file
# this module has already consumed and a second place a deployment's
# configuration lives. The application owns the contract's *shape* --
# `conda_sentinel.core.roles` declares the slots, the
# variable names and the loader -- and the settings module owns the read, which
# is the same division `config/authorization/claims.py` is on the other side of.
#
# The import is legal in the direction it runs: `config` may import a domain
# application, and `roles.py` imports nothing from `django.apps` or
# `django.contrib.auth`, so it loads before the app registry exists.
# See src/django_apps/conda_sentinel/core/roles.py and
# docs/accelerator/authentication.md.
ROLE_CONTRACT = load_role_contract(env)

# CPM-NFR-5's latency budget for the current package-health view, in milliseconds
# at the 95th percentile, measured server-side at full inventory size with filters
# applied.
#
# **PROVISIONAL.** The PRD states the budget as a requirement to exist and defers
# its value to the architecture pass, tracked as Open Question 5 and recorded in the
# assumptions index; CPM-APP-S02 is required to configure a budget and enforce it,
# not to choose the number. 800ms is chosen and written down so the enforcement is
# real rather than notional, and the reasoning is here so whoever sets the real one
# knows what they are overriding:
#
#   * The screen is server-rendered and a reader interacts with it in a loop --
#     tick a facet, read, tick another. The classic threshold for an interaction
#     still feeling like part of a train of thought is one second, and 800ms leaves
#     roughly 200ms of that for the network and the browser's own render.
#   * The work behind it is bounded and does not grow with the inventory: one
#     filtered page of fifty rollup rows plus a fixed number of derived-table reads,
#     every one of them over an indexed column. A budget that had to grow with
#     CPM-NFR-1's ten thousand packages would be describing a different design.
#   * It is deliberately not tight. A budget nobody can meet gets raised, and a
#     budget raised once gets raised again; this one is meant to hold, so the test
#     that enforces it fails on a regression rather than on a slow morning.
#
# **What actually guards this, and what does not.** The suite's timing case asserts
# ten times this number, because a stopwatch on a shared CI runner cannot honestly
# measure a p95 -- so it catches an order-of-magnitude regression (a full scan, an
# N+1, a join that multiplied rows) and nothing finer. The real gate is the *exact*
# query-count assertion beside it, which is strict and structural. When there is
# production traffic, measure the p95 from it and set this number from that
# measurement; do not read a green suite as evidence that the budget is met.
#
# Read from the environment so a deployment on slower storage can state its own
# without a code change -- which is also what makes the number replaceable when
# Open Question 5 is answered.
CPM_HEALTH_VIEW_P95_BUDGET_MS = env.int("CPM_HEALTH_VIEW_P95_BUDGET_MS", default=800)

# The largest export this product will build inside a request, in rows.
#
# **PROVISIONAL**, and the second of PRD Open Question 5's two numbers -- the first
# is the latency budget above. `CPM-AD-12` and `CPM-AD-9` name this constant and say
# what it is for: "an export beyond the row cap is a task, never an unpaginated
# response". `CPM-APP-S06` needs a bound now because it ships the export;
# `CPM-APP-S08` is the story that moves the work beyond it out of the request, and
# until then an export at the cap is truncated and says so in a response header
# rather than silently handing somebody a partial file.
#
# 5,000, and the reasoning is that the cap should *bite* rather than be decorative:
#
#   * `CPM-NFR-1` sizes the inventory at ten thousand packages, so a cap of five
#     thousand means the largest reports genuinely take the asynchronous path. A cap
#     set above the inventory would be a number nothing ever reaches, and the export
#     path `CPM-AD-9` requires would ship untested until the day it mattered.
#   * Most reports are filtered subsets well under it -- known-exploited
#     vulnerabilities is tens of rows, unmapped identities hundreds -- so the common
#     case stays synchronous and immediate.
#   * Five thousand rows of eight columns is roughly a megabyte of CSV, which is a
#     second or two of work. That is a request somebody waits through, not one they
#     abandon.
#
# Read from the environment so a deployment can state its own without a code change,
# which is also what makes it replaceable when Open Question 5 is answered.
CPM_SYNC_EXPORT_MAX_ROWS = env.int("CPM_SYNC_EXPORT_MAX_ROWS", default=5_000)
# The inventory source's file (CPM-AD-29, CPM-FR-42): the versioned watchlist the
# declared adapter reads, selected by locality.
#
# The read is *here* for the same reason ROLE_CONTRACT's is, and the split is the
# same one. CPM-AD-29 names config.locality.is_local() as the selector and AD-4
# forbids a domain application importing config, so the application owns the
# contract and a pure function -- watchlist_path(local=...) reads no environment
# variable and touches no filesystem -- and this module performs the read.
#
# Selection fails closed toward production, which is is_local()'s own default:
# only COMPONENT_RUNTIME=local selects the development subset, and absent, empty
# and unrecognized all read the production watchlist. That direction is
# load-bearing. A deployed component reading the development subset would find
# every package outside it missing and record each one as absent -- permanently,
# in an append-only log nothing may correct.
#
# Read at settings-import time, so it freezes for the process. That is what a
# declared adapter is: CollectorsConfig.ready() binds one file at boot, and a
# component that has to read a different one restarts.
# See src/django_apps/conda_sentinel/collectors/watchlist.py.
INVENTORY_WATCHLIST_PATH = watchlist_path(local=is_local())
# The conda channels and platforms the published-package collector observes
# (CPM-FR-10, CPM-CURRENCY-S04). Both ship EMPTY, and that is the decision rather
# than an omission.
#
# Which channels and which platforms this product monitors is PRD Open Question
# 4, and it is unresolved. Choosing one here would answer it by default and would
# be wrong in exactly the way a populated watchlist would be wrong: a component
# that observed a channel nobody chose would record facts about the wrong
# surface, permanently, in an append-only log nothing may correct. So the
# mechanism ships, the declaration ships empty, and a collection refuses --
# loudly, naming the setting -- until an operator declares both. See
# docs/conda-sentinel/operations.md.
#
# Declared here rather than read from the environment, on the same terms the
# watchlist is a reviewed file rather than a variable: which surfaces this
# product records evidence about is a decision worth a pull request and a
# reviewer, not one worth an unreviewed export. Read at *run* time by the
# collector rather than frozen into a locator here, so a declaration changed by
# deploy takes effect on the next collection rather than needing the value to be
# threaded through anything.
#
# The names are single path segments, lower-cased: a channel is the segment
# api.anaconda.org serves a package under ("conda-forge"), and a platform is a
# conda subdir ("linux-64", "osx-arm64", "noarch"). An entry that is blank, not a
# string, duplicated, or carries a path separator is refused rather than encoded.
# See src/django_apps/conda_sentinel/collectors/conda_package.py.
CPM_MONITORED_CHANNELS: tuple[str, ...] = ()
CPM_MONITORED_PLATFORMS: tuple[str, ...] = ()

# PASSWORDS
# ------------------------------------------------------------------------------
# PASSWORD_HASHERS is deliberately unset. Authentication is delegated to an
# OpenID Connect provider, so a component never issues or verifies a local
# password and has no reason to prefer one hasher over another -- Django's
# defaults stand. Dropping the Argon2 entry is what lets argon2-cffi leave the
# dependency set.
# https://docs.djangoproject.com/en/dev/ref/settings/#auth-password-validators
AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",
    },
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# MIDDLEWARE
# ------------------------------------------------------------------------------
# https://docs.djangoproject.com/en/dev/ref/settings/#middleware
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.locale.LocaleMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    # After AuthenticationMiddleware so request.user is resolved and can be
    # bound onto the log context as user_id.
    "django_structlog.middlewares.RequestMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "allauth.account.middleware.AccountMiddleware",
]

# STATIC
# ------------------------------------------------------------------------------
# https://docs.djangoproject.com/en/dev/ref/settings/#static-root
STATIC_ROOT = str(BASE_DIR / "staticfiles")
# https://docs.djangoproject.com/en/dev/ref/settings/#static-url
STATIC_URL = "/static/"
# https://docs.djangoproject.com/en/dev/ref/contrib/staticfiles/#std:setting-STATICFILES_DIRS
STATICFILES_DIRS = [str(APPS_DIR / "static")]
# https://docs.djangoproject.com/en/dev/ref/contrib/staticfiles/#staticfiles-finders
STATICFILES_FINDERS = [
    "django.contrib.staticfiles.finders.FileSystemFinder",
    "django.contrib.staticfiles.finders.AppDirectoriesFinder",
]

# MEDIA
# ------------------------------------------------------------------------------
# https://docs.djangoproject.com/en/dev/ref/settings/#media-root
MEDIA_ROOT = str(APPS_DIR / "media")
# https://docs.djangoproject.com/en/dev/ref/settings/#media-url
MEDIA_URL = "/media/"

# TEMPLATES
# ------------------------------------------------------------------------------
# https://docs.djangoproject.com/en/dev/ref/settings/#templates
TEMPLATES = [
    {
        # https://docs.djangoproject.com/en/dev/ref/settings/#std:setting-TEMPLATES-BACKEND
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        # https://docs.djangoproject.com/en/dev/ref/settings/#dirs
        "DIRS": [str(APPS_DIR / "templates")],
        # https://docs.djangoproject.com/en/dev/ref/settings/#app-dirs
        "APP_DIRS": True,
        "OPTIONS": {
            # https://docs.djangoproject.com/en/dev/ref/settings/#template-context-processors
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.template.context_processors.i18n",
                "django.template.context_processors.media",
                "django.template.context_processors.static",
                "django.template.context_processors.tz",
                "django.contrib.messages.context_processors.messages",
                "django_service.users.context_processors.allauth_settings",
                # The product's own navigation (`CPM-APP-S05`). On the base template
                # rather than in each view: a view that forgot would render a page
                # with a queue missing from its nav, and a reader would conclude the
                # queue did not exist rather than that the page was wrong.
                "conda_sentinel.surface.context_processors.navigation",
                # The reader's theme (`CPM-APP-S11`). Here for the same reason and
                # one more: the control is on the base template, so a view that had
                # to remember this would eventually be one that did not -- and the
                # symptom is a single page rendering in the wrong theme, which is the
                # page nobody thinks to check.
                "conda_sentinel.surface.context_processors.theme",
            ],
        },
    },
]

# https://docs.djangoproject.com/en/dev/ref/settings/#form-renderer
FORM_RENDERER = "django.forms.renderers.TemplatesSetting"

# http://django-crispy-forms.readthedocs.io/en/latest/install.html#template-packs
CRISPY_TEMPLATE_PACK = "bootstrap5"
CRISPY_ALLOWED_TEMPLATE_PACKS = "bootstrap5"

# FIXTURES
# ------------------------------------------------------------------------------
# https://docs.djangoproject.com/en/dev/ref/settings/#fixture-dirs
FIXTURE_DIRS = (str(APPS_DIR / "fixtures"),)

# SECURITY
# ------------------------------------------------------------------------------
# https://docs.djangoproject.com/en/dev/ref/settings/#session-engine
#
# FR-44 / AD-31: sessions are database-backed, and the engine is stated here
# rather than inherited. Django's own global default happens to be this same
# string, so the line changes no behaviour today -- which is precisely why it is
# worth writing. What it removes is the *dependence on a default*: a value the
# component never states is a value a Django release note can move, and one that
# a feature's settings fragment can quietly redefine.
#
# The Redis feature may not change it. Two of the six combinations ship no Redis
# at all, so a cache-backed engine would make session behaviour a property of an
# unrelated toggle -- per-replica sessions in those two LocMem combinations, where
# a user's session then depends on which replica answered. That is the NFR-3
# statelessness failure this setting exists to prevent, and it is why this
# assignment sits **outside every `feature:<name>` region**: an assignment inside
# a region is an assignment the materializer removes from every component that
# did not select that region's feature, whichever feature that turns out to be.
#
# No `cached_db` variant "for performance", and no `SESSION_CACHE_ALIAS`: both
# reintroduce the toggle dependency the explicit engine removes.
# `tests/unit/test_session_settings.py` holds all of it, including the
# outside-every-region half.
SESSION_ENGINE = "django.contrib.sessions.backends.db"
# https://docs.djangoproject.com/en/dev/ref/settings/#session-cookie-httponly
SESSION_COOKIE_HTTPONLY = True
# https://docs.djangoproject.com/en/dev/ref/settings/#csrf-cookie-httponly
CSRF_COOKIE_HTTPONLY = True
# https://docs.djangoproject.com/en/dev/ref/settings/#x-frame-options
X_FRAME_OPTIONS = "DENY"

# EMAIL
# ------------------------------------------------------------------------------
# https://docs.djangoproject.com/en/dev/ref/settings/#email-backend
EMAIL_BACKEND = env(
    "DJANGO_EMAIL_BACKEND",
    default="django.core.mail.backends.smtp.EmailBackend",
)
# https://docs.djangoproject.com/en/dev/ref/settings/#email-timeout
EMAIL_TIMEOUT = 5

# ADMIN
# ------------------------------------------------------------------------------
# Django Admin URL.
ADMIN_URL = "admin/"
# https://docs.djangoproject.com/en/dev/ref/settings/#admins
#
# A list of `(name, address)` pairs, and the shape is load-bearing rather than
# stylistic: `django.core.mail.mail_admins` refuses anything else outright --
# `raise ValueError("The ADMINS setting must be a list of 2-tuples.")` -- and it
# is called from `AdminEmailHandler.emit`, which `production.py` wires onto the
# `django.request` logger. The single-string form this carried until Story 5.3
# therefore turned the *first* 5xx the component ever emitted into an unhandled
# exception raised from inside `logging`, replacing the response with a
# traceback. Nothing caught it before because nothing returned a 5xx
# deliberately; readiness returns 503 by design (AD-22), so it does now, and
# `tests/integration/test_health.py` exercises that path through the real stack.
ADMINS = [("Kevin Samuel Mills", "millsks@gmail.com")]
# https://docs.djangoproject.com/en/dev/ref/settings/#managers
MANAGERS = ADMINS
# https://cookiecutter-django.readthedocs.io/en/latest/settings.html#other-environment-settings
# Force the `admin` sign in process to go through the `django-allauth` workflow.
# FR-7: this defaults *true*. It arrived from cookiecutter-django defaulting
# false, which left `/admin/` serving Django's own credential form through
# `ModelBackend` with the IdP never involved. The mechanism is unchanged --
# `django_service/users/admin.py` already wraps `admin.site.login` in allauth's
# `secure_admin_login`, which redirects at `LOGIN_URL`; only the default was
# wrong.
DJANGO_ADMIN_FORCE_ALLAUTH = env.bool("DJANGO_ADMIN_FORCE_ALLAUTH", default=True)

# LOGGING
# ------------------------------------------------------------------------------
# https://docs.djangoproject.com/en/dev/ref/settings/#logging
# Everything -- ours, Django's, allauth's, Celery's -- renders through structlog.
# See config/observability/logging.py.
DJANGO_LOG_LEVEL = env.str("DJANGO_LOG_LEVEL", default="INFO")
DJANGO_LOG_FORMAT = env.str("DJANGO_LOG_FORMAT", default="")

LOGGING = build_logging_config(
    debug=DEBUG,
    log_level=DJANGO_LOG_LEVEL,
    log_format=DJANGO_LOG_FORMAT,
)

configure_structlog()

REDIS_URL = env("REDIS_URL", default="redis://localhost:6379/0")
REDIS_SSL = REDIS_URL.startswith("rediss://")

# Celery
# ------------------------------------------------------------------------------
# django-structlog binds request_id and user_id for the life of a request and
# carries request_id into the Celery tasks a request enqueues.
DJANGO_STRUCTLOG_CELERY_ENABLED = True
if USE_TZ:
    # https://docs.celeryq.dev/en/stable/userguide/configuration.html#std:setting-timezone
    CELERY_TIMEZONE = TIME_ZONE
# https://docs.celeryq.dev/en/stable/userguide/configuration.html#std:setting-broker_url
CELERY_BROKER_URL = REDIS_URL
# https://docs.celeryq.dev/en/stable/userguide/configuration.html#redis-backend-use-ssl
CELERY_BROKER_USE_SSL = {"ssl_cert_reqs": ssl.CERT_NONE} if REDIS_SSL else None
# https://docs.celeryq.dev/en/stable/userguide/configuration.html#std:setting-result_backend
CELERY_RESULT_BACKEND = REDIS_URL
# https://docs.celeryq.dev/en/stable/userguide/configuration.html#redis-backend-use-ssl
CELERY_REDIS_BACKEND_USE_SSL = CELERY_BROKER_USE_SSL
# https://docs.celeryq.dev/en/stable/userguide/configuration.html#result-extended
CELERY_RESULT_EXTENDED = True
# https://docs.celeryq.dev/en/stable/userguide/configuration.html#result-backend-always-retry
# https://github.com/celery/celery/pull/6122
CELERY_RESULT_BACKEND_ALWAYS_RETRY = True
# https://docs.celeryq.dev/en/stable/userguide/configuration.html#result-backend-max-retries
CELERY_RESULT_BACKEND_MAX_RETRIES = 10
# https://docs.celeryq.dev/en/stable/userguide/configuration.html#std:setting-accept_content
CELERY_ACCEPT_CONTENT = ["json"]
# https://docs.celeryq.dev/en/stable/userguide/configuration.html#std:setting-task_serializer
CELERY_TASK_SERIALIZER = "json"
# https://docs.celeryq.dev/en/stable/userguide/configuration.html#std:setting-result_serializer
CELERY_RESULT_SERIALIZER = "json"
# https://docs.celeryq.dev/en/stable/userguide/configuration.html#task-time-limit
# TODO: set to whatever value is adequate in your circumstances
CELERY_TASK_TIME_LIMIT = 5 * 60
# https://docs.celeryq.dev/en/stable/userguide/configuration.html#task-soft-time-limit
# TODO: set to whatever value is adequate in your circumstances
CELERY_TASK_SOFT_TIME_LIMIT = 60
# https://docs.celeryq.dev/en/stable/userguide/configuration.html#task-routes
# CPM-AD-20's three workload queues, routed by task-name namespace. The table is
# `conda_sentinel.core.queues`' -- the three queue names are
# declared there once and every reader resolves them from it, because a second
# literal spelling of "collect" is exactly what this repository's audits exist to
# catch.
#
# `CELERY_TASK_ROUTES` is one of the keys `config/startup/allowlist.py`'s
# `CONTRIBUTABLE_KEYS` permits a domain application to contribute to. The
# composition step that would apply such a contribution is the platform's Epic 9
# and is not built -- `config/component/loader.py` parses `component.toml` and
# stops there -- so the contribution is written here by hand, on exactly the
# terms the `LOCAL_APPS` entry above records for the same app's adoption. When
# Epic 9 lands, this line is what it replaces.
#
# No catch-all pattern and no `CELERY_TASK_DEFAULT_QUEUE`: a task declaring no
# `cpm.` name keeps landing on the inherited default queue, which is where the
# platform's own `get_users_count` and the correlation probe in
# `tests/integration/test_celery_log_correlation.py` both belong.
CELERY_TASK_ROUTES = queues.CELERY_TASK_ROUTES
# https://docs.celeryq.dev/en/stable/userguide/configuration.html#beat-scheduler
# Cadence is data in this scheduler's tables and never a decorator on a task
# (CPM-AD-20, CPM-NFR-2): a hard-coded schedule cannot be changed without a
# deploy. `tests/unit/django_apps/test_task_declaration_audit.py` is the gate.
CELERY_BEAT_SCHEDULER = "django_celery_beat.schedulers:DatabaseScheduler"
# https://docs.celeryq.dev/en/stable/userguide/configuration.html#beat-schedule
# CPM-CURRENCY-S05's full-inventory sweep: one entry per *per-package* collector,
# each firing the one dispatch task with that collector's registered name, at
# that collector's declared cadence. The dispatch selects the packages its
# collector can be asked about and enqueues one existing per-package collection
# task each, in chunks -- so the atomic unit stays one package (CPM-AD-23), no
# task holds ten thousand packages, and one collector's dispatch failing leaves
# every other collector's untouched (CPM-FR-15).
#
# **The numbers are written here rather than imported, and that is the design
# rather than a shortcut.** CPM-AD-20 makes cadence *data*: this dictionary is
# what django_celery_beat's DatabaseScheduler seeds its tables from. What it does
# **not** buy is an operator changing one of *these nine* intervals without a
# deploy -- the scheduler rewrites every entry it finds here on each beat start,
# so a value edited in the admin is live only until beat restarts. Cadence as data
# is what lets a *later* schedule be added or changed in the tables; these nine
# are the declaration, and changing one is a pull request. docs/conda-sentinel/operations.md says
# the same thing to an operator.
#
# So the schedule and the collector each state a cadence independently, and the
# two are reconciled at start-up in both directions -- a collector whose declared
# cadence does not match its entry, an entry naming a collector nothing
# registered, a collector declaring a cadence without a selection or the reverse,
# and a freshness target that is not strictly greater than its cadence are each an
# ImproperlyConfigured. The refusal fires from the collectors application's own
# AppConfig.ready(), which is the hook that registers the collectors and therefore
# the only one in a deployed process positioned to see them;
# config/startup/stage_two.py evaluates the same rule as condition 11. Without
# that check a weekly schedule against a daily-derived two-day target would make
# the whole inventory read stale five days out of seven with every gate green,
# which is the failure CPM-CURRENCY-S01 recorded and this reconciliation exists
# to prevent.
#
# **The daily entries that carry no phase fire together, and that is accepted
# rather than overlooked.** Beat starts them from one instant, so several
# dispatches land on the `collect` queue at once. A dispatch enqueues and returns
# -- it makes no outbound call and holds no transaction -- so what arrives
# simultaneously is a handful of cheap tasks rather than several inventories of
# I/O, and the collections they enqueue are then bounded by each collector's own
# rate limiter, which is where the real pacing lives (CPM-AD-20). The three
# security entries are the group worth naming: each asks its own source and each
# spends its own allowance -- docs/conda-sentinel/operations.md states what that costs against
# CPM-NFR-1's inventory. Two of them carry a countdown for reasons their own
# comments give; offsetting an entry any other way would need a crontab, which the
# reconciliation below deliberately cannot read as an interval.
#
# A settings module cannot import a collector to read its cadence: these modules
# are executed before the app registry exists and every collector module reaches
# a Django model. The task name and the keyword are literals for the same reason,
# and both are reconciled -- by the boot sweep, and by tests/unit/test_settings.py
# against collectors/tasks.py's own declarations.
#
# Inventory ingestion is deliberately absent. It is run-scoped: it reads one
# document naming many packages (CPM-AD-25) and refuses all three per-package
# hooks, so it is not swept one package at a time and a dispatch refuses it by
# name. Its own schedule is CPM-IDENTITY-S07's and is not written here.
CELERY_BEAT_SCHEDULE = {
    "cpm-sweep-source-release": {
        "task": "cpm.collect.sweep",
        "schedule": timedelta(days=1),
        "kwargs": {"collector": "source_release"},
    },
    "cpm-sweep-pypi-release": {
        "task": "cpm.collect.sweep",
        "schedule": timedelta(days=1),
        "kwargs": {"collector": "pypi_release"},
    },
    "cpm-sweep-feedstock": {
        "task": "cpm.collect.sweep",
        "schedule": timedelta(days=7),
        "kwargs": {"collector": "feedstock"},
    },
    "cpm-sweep-conda-package": {
        "task": "cpm.collect.sweep",
        "schedule": timedelta(days=1),
        "kwargs": {"collector": "conda_package"},
    },
    "cpm-sweep-vulnerability": {
        "task": "cpm.collect.sweep",
        "schedule": timedelta(days=1),
        "kwargs": {"collector": "vulnerability"},
    },
    "cpm-sweep-kev": {
        "task": "cpm.collect.sweep",
        "schedule": timedelta(days=1),
        "kwargs": {"collector": "kev"},
        # The one entry carrying options, and the only phase this schedule can
        # express. The KEV collector cross-references what the vulnerability
        # collector wrote, so the two firing from one instant means a KEV run
        # routinely reads the previous day's advisories -- an answer one cadence
        # behind, with nothing saying so. The reconciliation above compares an
        # entry's `schedule` with its collector's declared cadence, so the interval
        # cannot carry a phase and a crontab cannot be read as an interval; beat
        # passes an entry's `options` to `apply_async`, so a countdown on the
        # dispatch is what is left. It reduces the window and does not close it --
        # at ten thousand packages the vulnerability sweep spends most of a day
        # inside its own allowance -- and `collectors/kev.py`'s KEV_DISPATCH_OFFSET
        # says so, `tests/unit/test_settings.py` reconciles the two, and
        # `docs/conda-sentinel/operations.md` states the residual to an operator.
        "options": {"countdown": 60 * 60},
    },
    "cpm-sweep-license": {
        "task": "cpm.collect.sweep",
        "schedule": timedelta(days=1),
        "kwargs": {"collector": "license"},
        # The second entry carrying options, and its phase is not the KEV entry's.
        # Nothing in the licence collector reads another collector's evidence --
        # the reason KEV carries a countdown at all -- so what this one buys is
        # different: the licence collector and CPM-CURRENCY-S04's published-package
        # collector read the *same host*, api.anaconda.org, on the same daily tick
        # and spend separate allowances against it (CPM-AD-20), and the three
        # security dispatches otherwise arrive in a worker log as one instant.
        # Deliberately a different number from the KEV entry's: two entries sharing
        # a phase would fire together again and the offset would buy nothing.
        # `collectors/license.py`'s LICENSE_DISPATCH_OFFSET is the declaration,
        # `tests/unit/test_settings.py` reconciles the two, and docs/conda-sentinel/operations.md
        # states what the two sweeps cost that host to an operator.
        "options": {"countdown": 2 * 60 * 60},
    },
    "cpm-sweep-python-readiness": {
        "task": "cpm.collect.sweep",
        "schedule": timedelta(days=7),
        "kwargs": {"collector": "python_readiness"},
        # The third entry carrying options, and its phase is neither of the other
        # two. CPM-PY314-S01's static assessment reads pypi.org, which is also what
        # CPM-CURRENCY-S02's daily sweep reads -- so without an offset the two would
        # begin at one instant one day in seven, each spending its own allowance
        # (CPM-AD-20) against one source. Deliberately a different number again:
        # entries sharing a phase fire together and the offset buys nothing.
        # collectors/python_readiness.py's READINESS_DISPATCH_OFFSET is the
        # declaration, tests/unit/test_settings.py reconciles the two, and
        # docs/conda-sentinel/operations.md states what the two sweeps cost that host.
        #
        # **Weekly rather than daily**, which no other collect entry is except the
        # feedstock one. What this collector reads is a project's declared metadata,
        # which changes when the project publishes a release and at no other time;
        # the daily collectors already watch for those releases. The reconciliation
        # below compares this interval with the collector's declared cadence in both
        # directions, so the two cannot drift.
        "options": {"countdown": 3 * 60 * 60},
    },
    "cpm-sweep-resolve-identity": {
        "task": "cpm.collect.sweep",
        "schedule": timedelta(days=1),
        "kwargs": {"collector": "resolve_identity"},
        # No options and no phase, deliberately (CPM-IDENTITY-S08). This is the
        # resolver the four mapping-selecting sweeps depend on -- source_release,
        # pypi_release and feedstock select nothing until it has recorded a
        # mapping, and python_readiness selects on the same release-ecosystem
        # mapping three hours later -- so on the first day it runs the three on
        # its tick select nothing and on the second they select everything it
        # resolved; python_readiness's lag depends on whether the resolver's
        # tasks have drained by its offset. An offset that put this entry ahead
        # of them would be a fourth phased entry, which is a change to the
        # reconciliation test this entry does not make; the lag is stated in
        # docs/conda-sentinel/operations.md instead.
    },
}
# https://docs.celeryq.dev/en/stable/userguide/configuration.html#worker-send-task-events
CELERY_WORKER_SEND_TASK_EVENTS = True
# https://docs.celeryq.dev/en/stable/userguide/configuration.html#std-setting-task_send_sent_event
CELERY_TASK_SEND_SENT_EVENT = True
# https://docs.celeryq.dev/en/stable/userguide/configuration.html#worker-hijack-root-logger
CELERY_WORKER_HIJACK_ROOT_LOGGER = False
# django-allauth
# ------------------------------------------------------------------------------
ACCOUNT_ALLOW_REGISTRATION = env.bool("DJANGO_ACCOUNT_ALLOW_REGISTRATION", True)
# https://docs.allauth.org/en/latest/account/configuration.html
# Empty in the base for the reason `AUTHENTICATION_BACKENDS` above is empty of
# `ModelBackend`: any declared login method keeps allauth's local sign-in form
# reachable, which is stage 1's condition 2, state b. Declared -- rather than left
# out -- because absence is not neutral here: allauth defaults this to a method,
# so a deleted line reinstates the forbidden state, and `stage_one.py` refuses an
# undeclared `ACCOUNT_LOGIN_METHODS` for exactly that reason. `local.py` and
# `test.py` declare the developer-facing form where the locality that justifies it
# is.
ACCOUNT_LOGIN_METHODS: set[str] = set()
# https://docs.allauth.org/en/latest/account/configuration.html
ACCOUNT_SIGNUP_FIELDS = ["email*", "username*", "password1*", "password2*"]
# https://docs.allauth.org/en/latest/account/configuration.html
ACCOUNT_EMAIL_VERIFICATION = "mandatory"
# https://docs.allauth.org/en/latest/account/configuration.html
ACCOUNT_ADAPTER = "django_service.users.adapters.AccountAdapter"
# https://docs.allauth.org/en/latest/account/forms.html
ACCOUNT_FORMS = {"signup": "django_service.users.forms.UserSignupForm"}
# https://docs.allauth.org/en/latest/socialaccount/configuration.html
# The adapter lives in `config.authorization` beside the mapper it calls. AD-4
# permits `config` to import `django_service` and forbids the reverse, so an
# adapter that has to reach the mapper cannot stay in `django_service`.
SOCIALACCOUNT_ADAPTER = "config.authorization.adapters.OIDCSocialAccountAdapter"
# https://docs.allauth.org/en/latest/socialaccount/configuration.html
SOCIALACCOUNT_FORMS = {"signup": "django_service.users.forms.UserSocialSignupForm"}
# https://docs.allauth.org/en/latest/socialaccount/configuration.html
# Already allauth's default, and stated anyway: with it on, a provider asserting
# a verified address that matches a local row signs that row's owner in. AD-11
# makes `idp_subject` the sole identity key and forbids email standing in for
# it, so this being explicit is what stops a later edit turning email into a
# resolution key without anyone noticing the rule it broke.
SOCIALACCOUNT_EMAIL_AUTHENTICATION = False
# The OIDC provider, configured from the environment (AD-31, AC #4). Never from
# database-resident `SocialApp` rows: a component forbidden to migrate itself
# could not create one, and configuring the same provider in both places makes
# allauth's `get_app` raise `MultipleObjectsReturned` at login. So nothing in
# this repository creates such a row, and no fixture or instruction asks anyone
# to.
#
# `server_url` is required -- allauth reads `app.settings["server_url"]` with a
# bare subscript, so a missing one is a `KeyError` rather than a graceful
# degradation. Every value is read with `default=""` all the same: an
# unconfigured IdP is refused at startup by Epic 4's stage 1, not by a settings
# module that cannot be imported to be inspected.
#
# `COMPONENT_OIDC_ISSUER` is the single trust anchor (AD-23). Story 2.7 derives
# the JWKS location from this same variable -- there is no second issuer
# setting, and adding one would give the component two answers to "who signs
# these tokens".
#
# Discovery is a *request*-time fetch inside allauth (`openid_config`), which is
# what keeps NFR-1's "startup makes no network call" true with this block
# present. Nothing here, in an `AppConfig.ready()` or in a system check may
# fetch the discovery document.
#
# `OIDC_ISSUER` and `OIDC_CLIENT_ID` are read into names of their own rather than
# inline in the block below, because the Bearer path (Story 2.7) needs both and
# reading `COMPONENT_OIDC_ISSUER` a second time is how a component acquires two
# answers to "who signs these tokens". One read, one name, two consumers.
OIDC_ISSUER = env.str("COMPONENT_OIDC_ISSUER", default="")
OIDC_CLIENT_ID = env.str("COMPONENT_OIDC_CLIENT_ID", default="")
SOCIALACCOUNT_PROVIDERS = {
    "openid_connect": {
        "APPS": [
            {
                "provider_id": OIDC_PROVIDER_ID,
                "name": env.str("COMPONENT_OIDC_PROVIDER_NAME", default=""),
                "client_id": OIDC_CLIENT_ID,
                "secret": env.str("COMPONENT_OIDC_CLIENT_SECRET", default=""),
                "settings": {
                    "server_url": OIDC_ISSUER,
                    # FR-4 specifies Authorization Code with PKCE. allauth reads
                    # exactly this key (`oauth2.provider.get_pkce_params`) and
                    # falls back to the provider default, which is False --
                    # without the key there is no PKCE.
                    "oauth_pkce_enabled": True,
                },
            },
        ],
    },
}

# The Bearer credential (FR-5, AD-23). Every value below is read here so that
# config/authorization/jwks.py and config/authorization/authentication.py take
# their configuration from Django settings rather than from the environment
# directly -- one `.env` read (FR-38), and a `settings` fixture can move any of
# them inside a test.
#
# `COMPONENT_OIDC_JWKS_URL` is an override, not a second trust anchor: unset, the
# location is derived from `OIDC_ISSUER` above. AD-23's startup refusal for a
# location not derived from the issuer is Epic 4's, and it consumes
# `config.authorization.jwks.jwks_url_derives_from_issuer`. That check is
# syntactic and can be nothing else -- confirming a location against the issuer's
# discovery document would mean fetching it at boot, which FR-23 forbids.
OIDC_JWKS_URL = env.str("COMPONENT_OIDC_JWKS_URL", default="")
# The audience every Bearer token must be minted for. Defaults to the OIDC client
# id, which is what an IdP issuing tokens for this component's own client puts in
# `aud`; a deployment whose IdP names a separate resource server sets the
# variable. Never defaulted to "any": `aud` is a required claim on this path, so
# an unconfigured audience refuses every token rather than accepting all of them.
OIDC_AUDIENCE = env.str("COMPONENT_OIDC_AUDIENCE", default="") or OIDC_CLIENT_ID
# The signature-algorithm allowlist. Never taken from the token's own `alg`
# header -- that is the `alg=none` and algorithm-confusion family of attacks.
OIDC_ALGORITHMS = env.list("COMPONENT_OIDC_ALGORITHMS", default=["RS256"])
# Clock-skew tolerance for `exp`, `iat` and `nbf`, in seconds. **Zero by default,
# which is the verification posture this component shipped with and is not
# changed here** -- this adds the lever, not a new policy. It exists because a
# few seconds of drift between the IdP's clock and this host's produces
# intermittent 401s with no diagnosable cause, and the only alternative is
# telling an operator to fix NTP on a machine they may not own. Raising it is a
# deliberate widening of the credential window: a token is accepted for this many
# seconds *past its own `exp`*, so the value is the amount of extra life every
# token in the deployment gains. Keep it in single-digit seconds.
OIDC_LEEWAY_SECONDS = max(0.0, env.float("COMPONENT_OIDC_LEEWAY_SECONDS", default=0.0))
# The JWKS cache lifetime. Its only job is to notice a key *removed* at the IdP
# (AD-23). Rotation is handled by the uncached-`kid` refetch below, so shortening
# this does not catch a rotation faster, and it has no effect at all on R-2's
# revocation window, which governs authorization rather than key material.
#
# Clamped to a floor rather than taken as given: at zero or below, every lookup
# sees the cache as expired and each one attempts a fetch, so the JWKS document
# is re-fetched as fast as the refetch window permits for the life of the
# process. A minute is the shortest lifetime that still makes the cache a cache.
JWKS_TTL_SECONDS = max(60.0, env.float("COMPONENT_JWKS_TTL_SECONDS", default=3600.0))
# The shortest interval between two outbound JWKS fetches. The Bearer path is
# unauthenticated at the moment a key is needed, so without this a caller sending
# random `kid` values produces one fetch per request against the IdP's JWKS
# endpoint. Lowering it towards zero is what re-arms that amplification.
#
# Clamped for that reason, and this floor is the load-bearing one: at zero or
# below the comparison `now - last_attempt < window` is false for every caller,
# which disables the rate limit outright and re-arms exactly the amplification
# this module was written to prevent. A configuration mistake must not be able to
# turn that off, so the floor is enforced here rather than documented as advice.
JWKS_MIN_REFETCH_SECONDS = max(1.0, env.float("COMPONENT_JWKS_MIN_REFETCH_SECONDS", default=60.0))

# django-rest-framework
# -------------------------------------------------------------------------------
# django-rest-framework - https://www.django-rest-framework.org/api-guide/settings/
REST_FRAMEWORK = {
    # The Bearer class is first deliberately: a request carrying both a session
    # cookie and an `Authorization: Bearer` header is decided by the Bearer
    # credential, because that is the one the caller chose to present. It returns
    # None rather than raising when no Bearer header is there, so a
    # session-authenticated request still falls through to the class below it.
    #
    # These two are the whole credential surface (FR-6, Story 2.8): the
    # locally minted static-token path is deleted, app and class alike, so every
    # credential a component accepts is one the IdP owns, plus the session those
    # flows establish. See docs/accelerator/authentication.md, "Retired surfaces".
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "config.authorization.authentication.OIDCBearerAuthentication",
        "rest_framework.authentication.SessionAuthentication",
    ),
    "DEFAULT_PERMISSION_CLASSES": ("rest_framework.permissions.IsAuthenticated",),
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    # CPM-AD-12: pagination is structural. Neither of these keys existed before
    # CPM-APP-S01 -- the decision says so in as many words -- so until they did,
    # the first collection endpoint anybody wrote returned CPM-NFR-1's ten
    # thousand packages in one response.
    #
    # Set *globally* rather than per view, which is the whole of the rule: a view
    # that declares nothing is paginated, and a view that declares something is
    # what tests/unit/django_apps/test_pagination_audit.py is looking for. No view
    # or serializer may opt out.
    #
    # The class is this product's own rather than DRF's, and
    # conda_sentinel/core/pagination.py argues why at length -- briefly: a class
    # this product owns is one its audits can name, and `page_size_query_param`
    # stays None there beside the paragraph saying why, rather than being DRF's
    # invisible default that a later contributor could open with no diff anybody
    # reads as opening a door.
    "DEFAULT_PAGINATION_CLASS": "conda_sentinel.core.pagination.BoundedPageNumberPagination",
    #
    # The size is a literal here and `DEFAULT_PAGE_SIZE` in that module, reconciled
    # by tests/unit/django_apps/test_pagination_audit.py -- the same shape the beat
    # schedule's `countdown` values take against their collectors' declared offsets.
    # It is deliberately **not** imported: `rest_framework.pagination` evaluates
    # `api_settings.PAGE_SIZE` at class-definition time, so importing that module
    # from here reads DRF's settings *during* this module's own import, before
    # `REST_FRAMEWORK` below is assigned -- and DRF then caches its defaults for the
    # life of the process. The symptom is every REST_FRAMEWORK key silently
    # reverting, which cost a green suite one puzzled hour. core/queues.py records
    # the same constraint from the other direction: a module this file reads at
    # settings-import time imports nothing from Django.
    "PAGE_SIZE": 50,
}

# django-cors-headers - https://github.com/adamchainz/django-cors-headers#setup
#
# **Both API roots since `CPM-APP-S14`.** The platform's is at `/api/` and this
# application's at `/conda-sentinel/api/<version>/`, and a regex naming only the first
# would leave every browser-based caller of this product's API failing preflight --
# silently, because a CORS rule that matches nothing raises nothing. The optional
# group is what keeps one rule covering both rather than two rules drifting apart.
CORS_URLS_REGEX = r"^(/conda-sentinel)?/api/.*$"

# By Default swagger ui is available only to admin user(s). You can change permission classes to change that
# See more configuration options at https://drf-spectacular.readthedocs.io/en/latest/settings.html#settings
# Annotated because production.py adds a "SERVERS" list of dicts, which a
# value-inferred dict type would reject.
SPECTACULAR_SETTINGS: dict[str, Any] = {
    # Named for the product rather than the accelerator it was built from.
    # `CPM-RENAME-S02` put Conda-Sentinel on every operator-facing surface and the
    # published contract is one: an integrator reading a document titled for a
    # different product has no way to know it is the right one.
    "TITLE": "Conda-Sentinel API",
    "DESCRIPTION": (
        "Current package health, per-package evidence, the recurring reports and the work queues. "
        "Derived statuses are emitted verbatim as their outcome values -- `unknown` is a state this product "
        "asserts, never an absence -- and the two writes are the package-identity override and the queue action."
    ),
    "VERSION": "1.0.0",
    "SERVE_PERMISSIONS": ["rest_framework.permissions.IsAdminUser"],
    "SCHEMA_PATH_PREFIX": "/api/",
    # Two vocabularies over one choice set. `expected_state` and `to_state` are both
    # `ItemState`, and without this drf-spectacular mints two enum components with
    # the same members and warns that it had to guess a name. One named component is
    # also what a generated client wants: two would give it two incompatible types
    # for one thing.
    "ENUM_NAME_OVERRIDES": {"WorkflowItemState": "conda_sentinel.workflow.states.ItemState.choices"},
}
# Your stuff...
# ------------------------------------------------------------------------------
