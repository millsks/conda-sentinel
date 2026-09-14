"""Where a declared credential may go: one host, over TLS, and nowhere else.

`CPM-OPERATE-S05` gave the two GitHub-reading collectors a bearer credential,
and with it a rule the collector base has to hold rather than trust each
collector to remember: the `Authorization` header a collector declares reaches
exactly the host the collector declared it for, over `https`, and is stripped
from every other request the collector makes. This module is that rule as
three small pure functions, and it is model-free so that `collectors/github.py`
-- importable at settings time and from `CollectorsConfig.ready()` -- and
`core/collection.py` can both call the same code rather than each carrying a
`urlsplit` of their own.

**One parse.** `host_of` is the only place a locator's host is read for a
credential decision, and `core/collection.py`'s refusal wording reads the same
function, so "which host" means one thing everywhere.

**Scheme as well as host.** A bearer over plain `http` is a bearer on the wire
in the clear, and a locator that names the right host under the wrong scheme
is exactly the mistake a rewritten `source_for` would make; so the credential
goes only where both match.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from typing import Final
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = [
    "AUTHORIZATION_HEADER",
    "CREDENTIAL_SCHEME",
    "carries_credential",
    "credentialed_headers",
    "host_of",
]

#: The header a credential travels in, and the one this module strips.
AUTHORIZATION_HEADER: Final[str] = "Authorization"

#: The only scheme a credential is sent under.
CREDENTIAL_SCHEME: Final[str] = "https"


def host_of(locator: str) -> str | None:
    """Return the host a locator names, lower-cased, or `None` when it names none.

    Args:
        locator: A URL, or something a substituted transport was handed that is
            not one.

    Returns:
        The host, or `None` for a locator with no host or one `urlsplit`
        refuses -- a malformed IPv6 literal, say. `None` rather than the
        locator itself, so a caller deciding whether to send a credential
        cannot match the wrong thing.

    """
    try:
        host = urlsplit(locator).hostname
    except ValueError:
        return None
    return host.lower() if host else None


def _scheme_of(locator: str) -> str:
    """Return a locator's scheme, lower-cased, or `""` when it has none or cannot be read.

    Args:
        locator: A URL, or not.

    Returns:
        The scheme, or the empty string.

    """
    try:
        return urlsplit(locator).scheme.lower()
    except ValueError:
        return ""


def carries_credential(headers: Mapping[str, str] | None) -> bool:
    """Report whether a header mapping names `Authorization`, in any case.

    Args:
        headers: The headers a request carried or is about to carry, or `None`
            for a request sending none.

    Returns:
        True when one of the names is `Authorization` however it is cased --
        header names are case-insensitive on the wire, so the check is too.

    """
    if not headers:
        return False
    return any(name.lower() == AUTHORIZATION_HEADER.lower() for name in headers)


def credentialed_headers(locator: str, *, declared: Mapping[str, str], credential_host: str | None) -> dict[str, str]:
    """Return the headers one call may carry to this locator: the credential only for its own host, over TLS.

    Args:
        locator: The URL the call is about to be made at.
        declared: The collector's assembled headers, credential included when
            one is declared.
        credential_host: The one host the credential belongs to, lower-cased,
            or `None` for a collector that declared no such host -- in which
            case the credential goes nowhere.

    Returns:
        A fresh dictionary: `declared` as it stands when the locator's scheme
        is `https` and its host is `credential_host`, and `declared` without
        any `Authorization` header otherwise. A locator whose host or scheme
        cannot be read is treated as a stranger, which is the direction that
        fails closed.

    """
    if credential_host is not None and _scheme_of(locator) == CREDENTIAL_SCHEME and host_of(locator) == credential_host:
        return dict(declared)
    return {name: value for name, value in declared.items() if name.lower() != AUTHORIZATION_HEADER.lower()}
