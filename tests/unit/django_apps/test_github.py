"""`CPM-OPERATE-S05`: the GitHub credential, the two allowances it buys, and where it may go.

Four claims, all about the model-free module `collectors/github.py`, and one
about the boot hook that reads it.

**A malformed token is refused by a sentence that never repeats it.** Every
refusal names `CPM_GITHUB_TOKEN` and the *kind* of fault; none carries the
value or its first eight characters, because the sentence is written to stderr
at boot and from there into whatever collects a crashed container's logs.

**The header is assembled once.** `authenticated_headers` adds
`Authorization: Bearer <token>` on top of what a collector declares and nothing
else -- and nothing at all for a token that is empty once stripped. Where the
header may then *go* is `core/credentials.py`'s rule, measured in
`test_credentials.py`, and the collectors declare `api.github.com` as the one
host through the base's `credential_host`.

**The setting is read at call time, stripped.** `github_token` answers the
setting as it stands when a collector is constructed, so a test can declare one
through `override_settings` and a rotated token reaches the next task.

**The module is model-free.** It is imported from `CollectorsConfig.ready()`
and from both GitHub-reading collectors, and `CPM-AD-7` says neither of those
imports the other -- so it may import no collector and no model.

The token in every case here is `tests.collectors.A_GITHUB_TOKEN`: a permitted
prefix in front of a phrase no real token could be. Nothing in this file or in
the fixtures it builds may look like a credential a scanner would flag.
"""

from __future__ import annotations

import ast
from datetime import timedelta
from types import MappingProxyType
from typing import TYPE_CHECKING
from typing import Final

import pytest
from django.apps import apps
from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings

from conda_sentinel.collectors import feedstock
from conda_sentinel.collectors import source_release
from conda_sentinel.collectors.github import AUTHENTICATED_CORE_ALLOWANCE
from conda_sentinel.collectors.github import AUTHENTICATED_SEARCH_ALLOWANCE
from conda_sentinel.collectors.github import BEARER_SCHEME
from conda_sentinel.collectors.github import GITHUB_API_HOST
from conda_sentinel.collectors.github import GITHUB_CORE_POOL_PER_HOUR
from conda_sentinel.collectors.github import GITHUB_TOKEN_PREFIXES
from conda_sentinel.collectors.github import GITHUB_TOKEN_SETTING
from conda_sentinel.collectors.github import MAX_TOKEN_CHARACTERS
from conda_sentinel.collectors.github import authenticated_headers
from conda_sentinel.collectors.github import github_token
from conda_sentinel.collectors.github import token_fault
from conda_sentinel.collectors.tasks import declared_inventory_adapter
from conda_sentinel.collectors.tasks import withdraw_inventory_adapter
from conda_sentinel.core.credentials import AUTHORIZATION_HEADER
from conda_sentinel.core.transport import DEFAULT_RETRIES
from tests.collectors import A_GITHUB_TOKEN
from tests.collectors import A_GITHUB_TOKEN_PREFIX
from tests.source_scan import SRC_ROOT
from tests.source_scan import parse

if TYPE_CHECKING:
    from collections.abc import Iterator
    from collections.abc import Mapping

#: The credential every case declares, shared with the integration tier so the
#: hygiene grep there and the refusals here look for one spelling.
A_TOKEN: Final[str] = A_GITHUB_TOKEN
A_TOKEN_PREFIX: Final[str] = A_GITHUB_TOKEN_PREFIX

#: What a collector declares before the credential is added.
DECLARED: Final[Mapping[str, str]] = MappingProxyType({"User-Agent": "conda-sentinel/0.0.0", "Accept": "text/plain"})

#: The module under test, for the source sweep.
GITHUB_MODULE: Final[str] = "django_apps/conda_sentinel/collectors/github.py"

#: The label `CollectorsConfig` derives, for the boot cases.
COLLECTORS_APP_LABEL: Final[str] = "collectors"


@pytest.fixture
def _slot_restored() -> Iterator[None]:
    """Start the boot cases from an empty inventory adapter slot, and leave it empty.

    Emptied *before* as well as after: the refused cases assert the hook never
    reached the adapter declaration, and that assertion is only about this case
    if a declaration some earlier case left behind is not sitting in the slot.

    Yields:
        None. The two withdrawals are the effect.

    """
    if declared_inventory_adapter() is not None:
        withdraw_inventory_adapter()
    yield
    if declared_inventory_adapter() is not None:
        withdraw_inventory_adapter()


# ---------------------------------------------------------------------------
# The declarations.
# ---------------------------------------------------------------------------


def test_the_setting_and_the_host_are_the_literals_the_settings_module_and_the_collectors_use() -> None:
    """One spelling each: `base.py` assigns the setting by this name; both collectors build locators on this host."""
    assert GITHUB_TOKEN_SETTING == "CPM_GITHUB_TOKEN"  # noqa: S105 - the setting's name, not a credential
    assert GITHUB_API_HOST == "api.github.com"
    assert source_release.GITHUB_API_HOST is GITHUB_API_HOST
    assert feedstock.GITHUB_API_HOST is GITHUB_API_HOST
    assert AUTHORIZATION_HEADER == "Authorization"
    assert BEARER_SCHEME == "Bearer"


def test_the_authenticated_allowances_are_derived_from_githubs_numbers_and_each_fits_a_collection() -> None:
    """Thirty searches a minute whole; the core pool less feedstock's share; both above one collection's charge.

    Both are also above the unauthenticated declarations they replace, which is
    the whole reason the setting exists; asserted so a transposed number cannot
    quietly declare *less* with a credential than without one.
    """
    assert GITHUB_CORE_POOL_PER_HOUR == 5000  # noqa: PLR2004 - GitHub's documented number
    assert AUTHENTICATED_SEARCH_ALLOWANCE.calls == 30  # noqa: PLR2004 - GitHub's documented number
    assert AUTHENTICATED_SEARCH_ALLOWANCE.per == timedelta(minutes=1)
    # The core declaration is the pool less what the feedstock collector can
    # spend from it in the same hour -- one core call per search-limited
    # collection, thirty a minute, sixty minutes -- so the two collectors'
    # counters cannot sum past the one pool a token has.
    feedstock_core_spend = AUTHENTICATED_SEARCH_ALLOWANCE.calls * 60
    assert AUTHENTICATED_CORE_ALLOWANCE.calls == GITHUB_CORE_POOL_PER_HOUR - feedstock_core_spend
    assert AUTHENTICATED_CORE_ALLOWANCE.calls + feedstock_core_spend <= GITHUB_CORE_POOL_PER_HOUR
    assert AUTHENTICATED_CORE_ALLOWANCE.per == timedelta(hours=1)
    assert AUTHENTICATED_CORE_ALLOWANCE.calls >= 1 + DEFAULT_RETRIES
    assert AUTHENTICATED_SEARCH_ALLOWANCE.calls >= 1 + DEFAULT_RETRIES
    assert AUTHENTICATED_CORE_ALLOWANCE.calls > source_release.SOURCE_RELEASE_RATE_LIMIT.calls
    assert AUTHENTICATED_CORE_ALLOWANCE.per == source_release.SOURCE_RELEASE_RATE_LIMIT.per
    assert AUTHENTICATED_SEARCH_ALLOWANCE.calls > feedstock.FEEDSTOCK_RATE_LIMIT.calls
    assert AUTHENTICATED_SEARCH_ALLOWANCE.per == feedstock.FEEDSTOCK_RATE_LIMIT.per


# ---------------------------------------------------------------------------
# The fault rule.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["", "   ", "\n", "\r\n\t"], ids=repr)
def test_an_empty_or_blank_declaration_is_not_a_fault(value: str) -> None:
    """Empty is the shipped state: no credential, and both collectors read GitHub unauthenticated.

    Args:
        value: The declaration.

    """
    assert token_fault(value) == ""


@pytest.mark.parametrize(
    "value",
    [
        A_TOKEN,
        f"  {A_TOKEN}\n",
        *(f"{prefix}a" for prefix in GITHUB_TOKEN_PREFIXES),
        "ghp_" + "a" * (MAX_TOKEN_CHARACTERS - 4),
    ],
    ids=repr,
)
def test_a_well_formed_token_is_not_a_fault(value: str) -> None:
    """A documented prefix, then printable ASCII with no whitespace inside, stripped, no wider than the bound.

    Args:
        value: The declaration.

    """
    assert token_fault(value) == ""


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(None, id="none"),
        pytest.param(1, id="int"),
        pytest.param([A_TOKEN], id="list"),
        pytest.param(f"{A_TOKEN}\nX-Injected: yes", id="line-feed"),
        pytest.param(f"{A_TOKEN}\rX-Injected: yes", id="carriage-return"),
        pytest.param(f"{A_TOKEN} {A_TOKEN}", id="embedded-space"),
        pytest.param(f"{A_TOKEN}\t{A_TOKEN}", id="embedded-tab"),
        pytest.param(f"{A_TOKEN}é", id="non-ascii"),
        pytest.param(f"{A_TOKEN}\x00", id="control-character"),
        pytest.param(A_TOKEN + "a" * MAX_TOKEN_CHARACTERS, id="too-wide"),
        pytest.param("1", id="not-a-token"),
        pytest.param(f'"{A_TOKEN}"', id="kept-its-quotes"),
        pytest.param(A_TOKEN.removeprefix("ghp_"), id="no-prefix"),
        pytest.param(f"GHP_{A_TOKEN}", id="prefix-wrong-case"),
    ],
)
def test_a_malformed_token_is_refused_naming_the_setting_and_never_the_value(value: object) -> None:
    """Every fault the matrix's `Malformed` row names, and the one property they share.

    The sentence names `CPM_GITHUB_TOKEN`, so an operator knows which variable
    to fix, and carries neither the value nor its first eight characters -- the
    prefix check is what catches a redaction that kept a "helpful" leading
    fragment.

    Args:
        value: The declaration.

    """
    fault = token_fault(value)

    assert fault
    assert GITHUB_TOKEN_SETTING in fault
    assert A_TOKEN not in fault
    assert A_TOKEN_PREFIX not in fault
    assert A_TOKEN.removeprefix("ghp_") not in fault


def test_the_width_bound_is_wide_enough_for_every_token_github_issues() -> None:
    """A fine-grained PAT is under a hundred characters and an App installation token is shorter."""
    assert MAX_TOKEN_CHARACTERS >= 100  # noqa: PLR2004 - the longest token shape GitHub documents, with room


def test_the_prefixes_are_the_five_github_documents_and_the_refusal_names_them() -> None:
    """Classic and fine-grained PATs, installation, OAuth and user-to-server tokens; the sentence lists them."""
    assert set(GITHUB_TOKEN_PREFIXES) == {"ghp_", "github_pat_", "ghs_", "gho_", "ghu_"}

    fault = token_fault("not-a-token")

    assert all(prefix in fault for prefix in GITHUB_TOKEN_PREFIXES)
    assert "not-a-token" not in fault


# ---------------------------------------------------------------------------
# The read.
# ---------------------------------------------------------------------------


def test_the_token_is_read_from_the_setting_at_call_time_and_stripped() -> None:
    """Live, not frozen: what the setting holds when a collector is constructed is what it sends."""
    with override_settings(**{GITHUB_TOKEN_SETTING: f"  {A_TOKEN}\n"}):
        assert github_token() == A_TOKEN
    with override_settings(**{GITHUB_TOKEN_SETTING: ""}):
        assert github_token() == ""


def test_an_absent_setting_reads_as_no_token() -> None:
    """`""` when the attribute is missing altogether -- the boot hook is what refuses that, not the read."""
    from django.conf import settings  # noqa: PLC0415 - deleted through Django's own override machinery

    with override_settings():
        delattr(settings, GITHUB_TOKEN_SETTING)

        assert github_token() == ""


@pytest.mark.parametrize(
    "value", [f"{A_TOKEN} {A_TOKEN}", f"{A_TOKEN}\n{A_TOKEN}", 1], ids=["space", "line-feed", "int"]
)
def test_a_malformed_setting_is_refused_at_the_read_without_echoing_it(value: object) -> None:
    """The second line behind the boot hook: a value that bypassed `ready()` still never becomes a header.

    Args:
        value: The declaration.

    """
    with override_settings(**{GITHUB_TOKEN_SETTING: value}), pytest.raises(ImproperlyConfigured) as refused:
        github_token()

    assert GITHUB_TOKEN_SETTING in str(refused.value)
    assert A_TOKEN not in str(refused.value)
    assert A_TOKEN_PREFIX not in str(refused.value)


# ---------------------------------------------------------------------------
# The header.
# ---------------------------------------------------------------------------


def test_the_bearer_header_is_added_on_top_of_the_declaration_and_nothing_else_changes() -> None:
    """`Authorization: Bearer <token>`, the declared headers intact, and the result read-only."""
    assembled = authenticated_headers(DECLARED, A_TOKEN)

    assert dict(assembled) == {**DECLARED, AUTHORIZATION_HEADER: f"{BEARER_SCHEME} {A_TOKEN}"}
    with pytest.raises(TypeError):
        assembled[AUTHORIZATION_HEADER] = "widened"  # type: ignore[index]


@pytest.mark.parametrize("token", ["", "   ", "\n\t"], ids=repr)
def test_without_a_token_the_declaration_is_sent_exactly_as_declared(token: str) -> None:
    """The matrix's `No token` row: no `Authorization`, and every declared header as it was.

    Whitespace-only is the same as empty: the value is stripped before it is
    judged, so `Bearer ` with nothing after it can never be assembled.

    Args:
        token: The declaration.

    """
    assembled = authenticated_headers(DECLARED, token)

    assert dict(assembled) == dict(DECLARED)
    assert not any(name.lower() == "authorization" for name in assembled)


def test_a_token_with_surrounding_whitespace_is_sent_stripped() -> None:
    """The header value is the token and nothing around it."""
    assembled = authenticated_headers(DECLARED, f"  {A_TOKEN}\n")

    assert assembled[AUTHORIZATION_HEADER] == f"{BEARER_SCHEME} {A_TOKEN}"


def test_a_declaration_that_already_carries_a_credential_is_refused_naming_the_header_only() -> None:
    """Two credentials for one request is a mistake, and the refusal repeats neither."""
    declared = {**DECLARED, "authorization": "Basic other-credential-for-tests"}

    with pytest.raises(ValueError, match=AUTHORIZATION_HEADER) as refused:
        authenticated_headers(declared, A_TOKEN)

    assert A_TOKEN not in str(refused.value)
    assert "other-credential-for-tests" not in str(refused.value)


def test_a_malformed_token_never_becomes_a_header_whichever_path_hands_it_in() -> None:
    """`authenticated_headers` asks the fault rule itself rather than trusting its caller."""
    with pytest.raises(ImproperlyConfigured) as refused:
        authenticated_headers(DECLARED, f"{A_TOKEN}\nX-Injected: yes")

    assert A_TOKEN_PREFIX not in str(refused.value)


# ---------------------------------------------------------------------------
# The boot hook.
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("_slot_restored")
@pytest.mark.parametrize(
    "value",
    [f"{A_TOKEN} {A_TOKEN}", f"{A_TOKEN}\n{A_TOKEN}", A_TOKEN + "a" * MAX_TOKEN_CHARACTERS, 1],
    ids=["space", "line-feed-inside", "too-wide", "int"],
)
def test_a_malformed_token_refuses_boot_naming_the_setting_and_never_the_value(value: object) -> None:
    """The matrix's `Malformed` row at the hook: `ImproperlyConfigured`, before the inventory adapter is declared.

    Args:
        value: The declaration.

    """
    with override_settings(**{GITHUB_TOKEN_SETTING: value}), pytest.raises(ImproperlyConfigured) as refused:
        apps.get_app_config(COLLECTORS_APP_LABEL).ready()

    assert GITHUB_TOKEN_SETTING in str(refused.value)
    assert A_TOKEN not in str(refused.value)
    assert A_TOKEN_PREFIX not in str(refused.value)
    assert declared_inventory_adapter() is None


@pytest.mark.usefixtures("_slot_restored")
def test_a_settings_module_declaring_no_token_refuses_boot_by_name() -> None:
    """Absent from the *settings module* is a dropped assignment, not the empty default."""
    from django.conf import settings  # noqa: PLC0415 - deleted through Django's own override machinery

    with override_settings():
        delattr(settings, GITHUB_TOKEN_SETTING)

        with pytest.raises(ImproperlyConfigured) as refused:
            apps.get_app_config(COLLECTORS_APP_LABEL).ready()

    assert GITHUB_TOKEN_SETTING in str(refused.value)


@pytest.mark.usefixtures("_slot_restored")
@pytest.mark.parametrize("value", ["", A_TOKEN], ids=["empty", "well-formed"])
def test_an_empty_or_well_formed_token_boots(value: str) -> None:
    """Both usable states let the component start; the hook completes and declares the adapter as before.

    Args:
        value: The declaration.

    """
    with override_settings(**{GITHUB_TOKEN_SETTING: value}):
        apps.get_app_config(COLLECTORS_APP_LABEL).ready()

    assert declared_inventory_adapter() is not None


# ---------------------------------------------------------------------------
# The module's shape.
# ---------------------------------------------------------------------------


def test_the_module_imports_no_collector_and_no_model() -> None:
    """Model-free, on the terms `collectors/agent.py` is: importable at settings time and from either collector.

    `CPM-AD-7` says neither GitHub-reading collector imports the other, and
    both import this; so this may import neither, nor anything that reaches a
    model. The two first-party imports are the `RateLimit` value type and the
    model-free credential rule.
    """
    imported = {
        node.module
        for node in ast.walk(parse(SRC_ROOT / GITHUB_MODULE))
        if isinstance(node, ast.ImportFrom) and node.module
    }

    first_party = {name for name in imported if name.startswith("conda_sentinel")}
    assert first_party == {"conda_sentinel.core.credentials", "conda_sentinel.core.rate_limit"}
    assert not any(".models" in name or ".collectors." in name for name in imported)


def test_the_module_declares_the_names_it_exports() -> None:
    """`__all__` is the module's contract, and both collectors import through it."""
    from conda_sentinel.collectors import github  # noqa: PLC0415 - the module object, for its `__all__`

    assert set(github.__all__) <= set(vars(github))
