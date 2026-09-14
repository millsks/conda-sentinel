"""`CPM-OPERATE-S05`: where a declared credential may go -- one host, over TLS, and nowhere else.

The rule `core/credentials.py` holds for the collector base and for
`collectors/github.py`, measured on its own: the host parse that both use, the
case-insensitive `Authorization` check, and the strip that keeps a bearer off
every locator that is not the declared host over `https`.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import TYPE_CHECKING
from typing import Final

import pytest

from conda_sentinel.core.credentials import AUTHORIZATION_HEADER
from conda_sentinel.core.credentials import CREDENTIAL_SCHEME
from conda_sentinel.core.credentials import carries_credential
from conda_sentinel.core.credentials import credentialed_headers
from conda_sentinel.core.credentials import host_of
from tests.collectors import A_GITHUB_TOKEN

if TYPE_CHECKING:
    from collections.abc import Mapping

#: The host the cases declare the credential for.
THE_HOST: Final[str] = "api.example.test"

#: What a collector declares before and after a credential is added.
DECLARED: Final[Mapping[str, str]] = MappingProxyType({"User-Agent": "conda-sentinel/0.0.0", "Accept": "text/plain"})
CREDENTIALED: Final[Mapping[str, str]] = MappingProxyType(
    {**DECLARED, AUTHORIZATION_HEADER: f"Bearer {A_GITHUB_TOKEN}"}
)

#: Locators the credential belongs on: the host, spelled the ways a locator can be.
OWN_LOCATORS: Final[tuple[str, ...]] = (
    f"https://{THE_HOST}/repos/x/y",
    f"HTTPS://{THE_HOST.upper()}/repos/x/y?per_page=30",
    f"https://{THE_HOST}:443/repos/x/y",
    f"https://user@{THE_HOST}/repos/x/y",
)

#: Locators it does not: another host, a host that merely starts or ends with
#: this one, userinfo tricks, the right host over plain `http`, and things
#: that are not URLs at all.
FOREIGN_LOCATORS: Final[tuple[str, ...]] = (
    "https://raw.example.test/x/y/HEAD/recipe/meta.yaml",
    f"https://{THE_HOST}.evil.example/repos/x/y",
    f"https://evil.{THE_HOST}/repos/x/y",
    f"https://evil.example/{THE_HOST}/repos/x/y",
    f"https://user@{THE_HOST}@evil.example/",
    f"http://{THE_HOST}/repos/x/y",
    f"//{THE_HOST}/repos/x/y",
    "https://[::1",
    "not a locator at all",
    "",
)


def test_the_header_and_the_scheme_are_the_literals() -> None:
    """`Authorization`, and only over `https`."""
    assert AUTHORIZATION_HEADER == "Authorization"
    assert CREDENTIAL_SCHEME == "https"


@pytest.mark.parametrize(
    ("locator", "expected"),
    [
        (f"https://{THE_HOST}/x", THE_HOST),
        (f"https://{THE_HOST.upper()}:8443/x", THE_HOST),
        (f"https://user:secret@{THE_HOST}/x", THE_HOST),
        ("https://[::1]/x", "::1"),
        ("https://[::1", None),
        ("not a locator", None),
        ("", None),
        ("/relative/path", None),
    ],
    ids=repr,
)
def test_host_of_reads_the_host_lower_cased_and_answers_none_for_no_host(locator: str, expected: str | None) -> None:
    """One parse for every credential decision, failing closed on anything without a host.

    Args:
        locator: The URL, or not.
        expected: The host it names, or `None`.

    """
    assert host_of(locator) == expected


@pytest.mark.parametrize("headers", [None, {}, DECLARED], ids=["none", "empty", "declared-only"])
def test_carries_credential_is_false_for_nothing_and_for_a_declaration_without_one(
    headers: Mapping[str, str] | None,
) -> None:
    """Nothing, an empty mapping and a plain declaration all carry no credential.

    Args:
        headers: The mapping, or `None`.

    """
    assert carries_credential(headers) is False


@pytest.mark.parametrize(
    "headers",
    [CREDENTIALED, {"authorization": "x"}, {"AUTHORIZATION": "x"}],
    ids=["bearer", "lower-cased", "upper-cased"],
)
def test_carries_credential_is_case_insensitive(headers: Mapping[str, str]) -> None:
    """Header names are case-insensitive on the wire, so the check is too.

    Args:
        headers: A mapping naming `Authorization` some way.

    """
    assert carries_credential(headers) is True


@pytest.mark.parametrize("locator", OWN_LOCATORS, ids=repr)
def test_the_credential_reaches_its_own_host_over_tls_however_it_is_spelled(locator: str) -> None:
    """Case, an explicit port and userinfo are not a different host.

    Args:
        locator: A URL on the declared host over `https`.

    """
    assert credentialed_headers(locator, declared=CREDENTIALED, credential_host=THE_HOST) == dict(CREDENTIALED)


@pytest.mark.parametrize("locator", FOREIGN_LOCATORS, ids=repr)
def test_every_other_locator_has_the_credential_stripped(locator: str) -> None:
    """Another host, a look-alike host, plain `http` on the right host, and anything unreadable.

    Args:
        locator: A URL somewhere else, or not a URL.

    """
    stripped = credentialed_headers(locator, declared=CREDENTIALED, credential_host=THE_HOST)

    assert stripped == dict(DECLARED)
    assert A_GITHUB_TOKEN not in str(stripped)


def test_a_credential_with_no_declared_host_goes_nowhere() -> None:
    """`credential_host=None` strips on every locator, including the one the header was meant for."""
    for locator in (*OWN_LOCATORS, *FOREIGN_LOCATORS):
        assert credentialed_headers(locator, declared=CREDENTIALED, credential_host=None) == dict(DECLARED)


def test_the_strip_is_case_insensitive_and_leaves_the_rest_alone() -> None:
    """`AUTHORIZATION` is the same header, and nothing else in the declaration moves."""
    declared = {**DECLARED, "AUTHORIZATION": f"Bearer {A_GITHUB_TOKEN}"}

    assert credentialed_headers(FOREIGN_LOCATORS[0], declared=declared, credential_host=THE_HOST) == dict(DECLARED)


def test_a_declaration_with_no_credential_passes_through_every_locator_as_a_fresh_copy() -> None:
    """With nothing to strip, both branches answer the declaration, copied."""
    for locator in (*OWN_LOCATORS, *FOREIGN_LOCATORS):
        answered = credentialed_headers(locator, declared=DECLARED, credential_host=THE_HOST)
        assert answered == dict(DECLARED)
        assert answered is not DECLARED
