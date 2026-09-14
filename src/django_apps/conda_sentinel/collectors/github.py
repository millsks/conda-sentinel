"""The one GitHub credential this product may send, and the two allowances it buys.

`CPM-OPERATE-S05`. Two collectors read GitHub -- `collectors/source_release.py`
reads a repository's releases and tags, `collectors/feedstock.py` reads
conda-forge's feedstocks and the staged-recipes search -- and both declared
GitHub's *unauthenticated* allowances because no credential existed: sixty
requests an hour, charged four per collection, is fifteen packages an hour,
and `CPM-NFR-1`'s ten-thousand-package inventory cannot be swept inside its
two-day freshness target at that rate. This module is the credential, read from
one setting, and everything a collector needs to declare the allowance that
comes with it.

**It sits beside `collectors/agent.py` for the reason that module gives.**
`CPM-AD-7` says a collector never imports another, so the pieces two of them
need live in neither; the `User-Agent` was the first such piece and the token
is the second. It imports no model and reads no database, so it is importable
at settings time and from `CollectorsConfig.ready()`, which is where a
malformed token is refused before any process that would send it starts.

**The token appears in exactly one place on the wire and nowhere else.**
`authenticated_headers` puts it in an `Authorization: Bearer` header; the
collector base merges that under the conditional request and hands it to the
transport, which is the only path to a socket (`CPM-AD-20`, `CPM-AD-27`). It is
never interpolated into a URL, a log event, a ledger row's `detail`, an
evidence row or an exception message -- `token_fault` is written so that the
sentence refusing a malformed value never repeats the value, and the tests in
both tiers grep every captured line and row for the token and its prefix.

**It reaches `https://api.github.com` and no other host.** GitHub does not count
`raw.githubusercontent.com` against the API allowance and does not need the
credential there, and a credential should reach exactly the host it is for,
over TLS -- which is what `core/credentials.py` decides, per locator. Each
collector declares that host as its `credential_host`, so the collector base
strips the header from any fetch aimed elsewhere, and the bounded second calls
the two collectors make from `translate` ask the base for their headers on
the same terms.

**Where the value comes from, and when.** `config/settings/base.py` reads
`CPM_GITHUB_TOKEN` from the environment -- the shell that started the process,
a deployment's secret, or the gitignored `.env` file `base.py` reads when
`DJANGO_READ_DOT_ENV_FILE` is on -- *once, at settings import*. `github_token`
reads the settings object at call time, so a test can substitute a value
through `override_settings`; but a token rotated in the environment reaches a
running worker only when the worker restarts, because the settings module is
evaluated once per process.

**The allowances are derived from GitHub's documented authenticated numbers,
not measured.** Five thousand core requests an hour and thirty searches a
minute are what GitHub states for a personal access token or an App
installation token; the core number declared here is that pool *less* what the
feedstock collector can spend from it in the same hour, because both
collectors draw on one pool per token (the constant's own comment has the
arithmetic). The arithmetic that turns them into "how long does a sweep take"
is in `docs/conda-sentinel/operations.md`, beside the caveat that the local
counter does not see the uncharged second call.
"""

from __future__ import annotations

from datetime import timedelta
from types import MappingProxyType
from typing import TYPE_CHECKING
from typing import Final

from django.core.exceptions import ImproperlyConfigured

from conda_sentinel.core.credentials import AUTHORIZATION_HEADER
from conda_sentinel.core.credentials import carries_credential
from conda_sentinel.core.rate_limit import RateLimit

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = [
    "AUTHENTICATED_CORE_ALLOWANCE",
    "AUTHENTICATED_SEARCH_ALLOWANCE",
    "BEARER_SCHEME",
    "GITHUB_API_HOST",
    "GITHUB_CORE_POOL_PER_HOUR",
    "GITHUB_TOKEN_PREFIXES",
    "GITHUB_TOKEN_SETTING",
    "MAX_TOKEN_CHARACTERS",
    "authenticated_headers",
    "github_token",
    "token_fault",
]

#: The setting `config/settings/base.py` assigns from the environment variable
#: of the same name, and `CollectorsConfig.ready()` refuses at boot when it is
#: malformed. Spelled once, here, so the boot refusal, the run-time read and the
#: settings test name the same thing. It is **never** declared in `pixi.toml`
#: or `compose.yaml`: a developer exports it in their shell before
#: `pixi run local-stack` or keeps it in the gitignored `.env`, and a deployment
#: carries it as a secret.
GITHUB_TOKEN_SETTING: Final[str] = "CPM_GITHUB_TOKEN"  # noqa: S105 - the setting's name, not a credential

#: The one host the credential is sent to. Both GitHub-reading collectors
#: import it from here so there is one spelling to compare a locator's host
#: against.
GITHUB_API_HOST: Final[str] = "api.github.com"

#: The scheme the credential is sent under in the header `core/credentials.py`
#: names. `Bearer` rather than the older `token` prefix: GitHub accepts both for
#: a personal access token, and only `Bearer` for an App installation token.
BEARER_SCHEME: Final[str] = "Bearer"

#: The prefixes GitHub documents for the tokens it issues, and therefore the
#: only shapes a declared value may take: `ghp_` a classic personal access
#: token, `github_pat_` a fine-grained one, `ghs_` an App installation token,
#: `gho_` an OAuth token, `ghu_` an App user-to-server token. A value with none
#: of them is not a token -- `CPM_GITHUB_TOKEN=1`, or a value that kept its
#: ConfigMap quotes -- and is refused at boot rather than sent on every request
#: and refused by GitHub on every one.
GITHUB_TOKEN_PREFIXES: Final[tuple[str, ...]] = ("ghp_", "github_pat_", "ghs_", "gho_", "ghu_")

#: The widest value accepted as a token. A fine-grained personal access token is
#: under a hundred characters and an installation token is shorter; the bound
#: exists so that a whole file pasted into the variable by mistake is refused
#: at boot rather than sent as a header on every request.
MAX_TOKEN_CHARACTERS: Final[int] = 512

#: GitHub's documented allowances for an authenticated caller (`CPM-AD-20`), and
#: the share of the core one this product may declare.
#:
#: The *search* pool covers `/search/issues`, which the feedstock collector's
#: absent branch reads: thirty requests a minute, and that collector declares
#: it whole, because the base charges one allowance per collection before it
#: knows which branch a package takes and the tighter of the two is the only
#: one that cannot be exceeded by accident.
#:
#: The *core* pool covers the repository, releases and tags endpoints: five
#: thousand requests an hour per token -- and **both** collectors draw on it.
#: Every feedstock collection makes at most one core call (the mapped branch's
#: repository read, or the absent branch's conventional-repository read; the
#: search is the other pool and the recipe is the raw host), and the feedstock
#: counter permits at most `AUTHENTICATED_SEARCH_ALLOWANCE.calls` requests a
#: minute, so in one hour it can spend at most `30 x 60 = 1,800` core requests.
#: Two local counters that each declared the whole pool could sum past it, so
#: the upstream-release collector declares the pool *less* that worst case:
#: `5,000 - 1,800 = 3,200` an hour, which is 800 collections an hour at four
#: requests each and a ten-thousand-package inventory in about 12.5 hours. The
#: docs say the rest: a user's personal access tokens all share one pool per
#: account, while an App installation token has a pool of its own.
GITHUB_CORE_POOL_PER_HOUR: Final[int] = 5000
AUTHENTICATED_SEARCH_ALLOWANCE: Final[RateLimit] = RateLimit(calls=30, per=timedelta(minutes=1))
_FEEDSTOCK_CORE_SPEND_PER_HOUR: Final[int] = AUTHENTICATED_SEARCH_ALLOWANCE.calls * 60
AUTHENTICATED_CORE_ALLOWANCE: Final[RateLimit] = RateLimit(
    calls=GITHUB_CORE_POOL_PER_HOUR - _FEEDSTOCK_CORE_SPEND_PER_HOUR,
    per=timedelta(hours=1),
)

#: The two characters that end a header on the wire. A value carrying either is
#: the start of another header, which is the injection `core/collection.py`'s
#: `_require_headers` also refuses -- this module refuses it first, at boot,
#: naming the setting rather than the header.
_LINE_BREAKS: Final[frozenset[str]] = frozenset({"\r", "\n"})


def token_fault(value: object) -> str:  # noqa: PLR0911 - one return per reason a token is refused
    """Return why a declared token cannot be sent, or the empty string when it can.

    Pure, so the boot hook and a case can ask the same question of the same
    value, on the terms `collectors/inventory_source.py`'s
    `inventory_source_fault` sets: the caller decides what to raise. Every
    sentence names the setting and the *kind* of fault and never the value --
    a refusal is written to stderr at boot and from there into whatever
    collects a crashed container's logs, and a credential in a log outlives its
    expiry.

    Args:
        value: Whatever the settings module assigned.

    Returns:
        The reason, or `""`. An empty or whitespace-only string is *usable*: it
        is the shipped state, meaning no credential is declared, and the two
        collectors keep their unauthenticated declarations. Surrounding
        whitespace is stripped before the value is judged, as `github_token`
        strips it before the value is sent, so a trailing newline from a
        ConfigMap is not a fault; whitespace *inside* the value is, because a
        header value cannot carry it and a token never does.

    """
    if not isinstance(value, str):
        return (
            f"{GITHUB_TOKEN_SETTING} holds {type(value).__name__} rather than a string, so this component "
            f"cannot send it as a credential. Declare it as the token itself, or leave it empty to read GitHub "
            f"unauthenticated (CPM-OPERATE-S05)."
        )
    token = value.strip()
    if not token:
        return ""
    if any(character in _LINE_BREAKS for character in token):
        return (
            f"{GITHUB_TOKEN_SETTING} carries a carriage return or a line feed inside the value. A header is "
            f"terminated by CRLF, so a line break inside a credential is the start of another header; the value "
            f"is refused rather than sent, and is not repeated here."
        )
    if any(character.isspace() for character in token):
        return (
            f"{GITHUB_TOKEN_SETTING} carries whitespace inside the value. A GitHub token never does, and a header "
            f"value cannot; the value is refused rather than sent, and is not repeated here."
        )
    if not token.isascii():
        return (
            f"{GITHUB_TOKEN_SETTING} carries a non-ASCII character. A GitHub token is ASCII and a header value "
            f"must be; the value is refused rather than sent, and is not repeated here."
        )
    if not token.isprintable():
        return (
            f"{GITHUB_TOKEN_SETTING} carries a control character. A GitHub token is printable ASCII; the value is "
            f"refused rather than sent, and is not repeated here."
        )
    if len(token) > MAX_TOKEN_CHARACTERS:
        return (
            f"{GITHUB_TOKEN_SETTING} is {len(token)} characters wide, and no GitHub token is wider than "
            f"{MAX_TOKEN_CHARACTERS}. The value looks like something other than a token -- a file, most "
            f"plausibly -- and is refused rather than sent on every request."
        )
    if not token.startswith(GITHUB_TOKEN_PREFIXES):
        return (
            f"{GITHUB_TOKEN_SETTING} does not begin with a prefix GitHub issues tokens under "
            f"({', '.join(GITHUB_TOKEN_PREFIXES)}), so it is not a token: a stray value, or one that kept the "
            f"quotes it was written with. It is refused at boot rather than sent on every request and refused "
            f"by GitHub on every one; the value is not repeated here."
        )
    return ""


def github_token() -> str:
    """Return the declared token, stripped, or `""` when none is declared.

    Read from `django.conf.settings` at *call* time rather than copied at
    import, so a test can declare one through `override_settings` and each
    collector reads the setting as it stands when constructed. That is not
    live rotation: `config/settings/base.py` evaluates `CPM_GITHUB_TOKEN` once
    per process, so a token rotated in the environment reaches a worker when
    the worker restarts, and the first collection after that restart is the
    first to send it.

    Returns:
        The token with surrounding whitespace removed, or the empty string when
        the setting is absent, empty or whitespace-only.

    Raises:
        ImproperlyConfigured: When the declared value is one `token_fault`
            refuses. `CollectorsConfig.ready()` has already refused it at boot;
            this is the second line, so that a value which bypassed the hook --
            an override in a test, a settings module edited by hand -- can never
            become a header. The message never carries the value.

    """
    from django.conf import settings  # noqa: PLC0415 - read at call time, so the setting is live rather than frozen

    declared = getattr(settings, GITHUB_TOKEN_SETTING, "")
    fault = token_fault(declared)
    if fault or not isinstance(declared, str):
        # The second clause narrows the type; a non-string has already been
        # refused by the first, so the two never disagree.
        raise ImproperlyConfigured(fault)
    return declared.strip()


def authenticated_headers(declared: Mapping[str, str], token: str) -> Mapping[str, str]:
    """Return a collector's declared headers with the bearer credential added, when there is one.

    Args:
        declared: What the collector declares for every request -- its
            `User-Agent`, the `Accept` and API-version pins.
        token: The credential `github_token` answered, or `""` for none.

    Returns:
        A read-only mapping. With no token -- empty, or whitespace only, which
        is stripped first so that `Bearer ` with nothing after it can never be
        sent -- it is a copy of `declared`, so a collector constructed without
        a credential sends exactly what it sent before this module existed.
        With one it is `declared` plus `Authorization: Bearer <token>`.

    Raises:
        ImproperlyConfigured: When the token is one `token_fault` refuses --
            the value must never become a header, whichever path handed it in.
        ValueError: When `declared` already names an `Authorization` header in
            any case. Two credentials for one request is a mistake, and
            silently overwriting one with the other would hide which won. The
            message names the header, never either value.

    """
    token = token.strip()
    if not token:
        return MappingProxyType(dict(declared))
    fault = token_fault(token)
    if fault:
        raise ImproperlyConfigured(fault)
    if carries_credential(declared):
        message = (
            f"the declared headers already carry {AUTHORIZATION_HEADER}, so adding the {GITHUB_TOKEN_SETTING} "
            f"credential would send two for one request. A collector that reads GitHub declares no credential "
            f"of its own; the setting is the only one (CPM-OPERATE-S05)."
        )
        raise ValueError(message)
    return MappingProxyType({**declared, AUTHORIZATION_HEADER: f"{BEARER_SCHEME} {token}"})
