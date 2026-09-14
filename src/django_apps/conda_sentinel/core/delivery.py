"""The one outbound POST this product makes, behind its own seam (`CPM-OPERATE-S09`).

`core/transport.py`'s `Transport.fetch` is GET-only by contract, and every
substitute in the suite implements exactly that one method. The operator digest
is the first thing this product *sends* rather than reads, and widening `fetch`
to carry a body would touch every collector's test double for one caller. So
the POST has a seam of its own: `WebhookDeliverer` is the declared adapter, a
test substitutes a recorded fake for it on `tests/collectors.py`'s terms, and
`RequestsWebhookDeliverer` is the one implementation that opens a connection.

**No retry, and a short timeout.** A digest is delivered once a day and the
row that records it says whether the delivery landed; a retry loop inside the
beat-fired task would hold the `policy` queue behind a webhook that is down,
and the next day's digest says the same thing again anyway. Ten seconds is the
whole allowance a receiving hook gets.

**The URL may carry a credential, and nothing here repeats it.** A webhook URL
is the one place this product accepts `https://user:secret@host/path`, and a
`requests` exception message prints the URL it failed against. So no exception
text from the library reaches an outcome: a failure is reported as the
exception's *class* or the response's status code, and the caller names the
host and nothing else (`core/credentials.py`'s `host_of`).

`tests/unit/django_apps/test_collector_base_audit.py` records this module's one
`requests.post` and its one stated timeout; a second call here, or one
anywhere else, fails that gate.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from typing import Any
from typing import Final
from typing import Protocol
from typing import cast
from typing import runtime_checkable

import requests

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = [
    "DEFAULT_WEBHOOK_TIMEOUT",
    "HTTP_STATUS_DETAIL",
    "DeliveryOutcome",
    "RequestsWebhookDeliverer",
    "WebhookDeliverer",
]

#: Seconds a webhook has to accept the digest, per connect and per read.
DEFAULT_WEBHOOK_TIMEOUT: Final[float] = 10.0

#: How a refused status is written into an outcome's detail: `HTTP 500`.
HTTP_STATUS_DETAIL: Final[str] = "HTTP {status}"

#: The statuses that count as delivered: `2xx` and nothing else. A `1xx` is not
#: an acceptance and a `3xx` is a redirect that is not followed -- a credential
#: in the URL must not be replayed to wherever the hook points.
_FIRST_ACCEPTED_STATUS: Final[int] = 200
_LAST_ACCEPTED_STATUS: Final[int] = 299


@dataclass(frozen=True, slots=True)
class DeliveryOutcome:
    """What one delivery attempt came to.

    Attributes:
        delivered: Whether the hook accepted the digest.
        detail: Why it did not -- the exception's class name or `HTTP <status>`
            -- or the empty string when it did. Never the URL: see the module
            docstring.
        status_code: The status the hook answered, or `None` when no answer
            came back.

    """

    delivered: bool
    detail: str = ""
    status_code: int | None = None


@runtime_checkable
class WebhookDeliverer(Protocol):
    """Something that can post a JSON document to a URL and say whether it landed.

    One method, because one method is the entire dependency; `runtime_checkable`
    so a test can assert a substitute satisfies the protocol at all.
    """

    def post(self, url: str, *, body: Mapping[str, object], timeout: float) -> DeliveryOutcome:
        """Post one document and report the outcome.

        The docstring is the whole body, for the reason `core/clock.py`'s
        `Clock.now` gives: a protocol method is never executed, so an `...`
        here would be a permanently uncovered line.

        Args:
            url: Where to post. May carry a credential, which the implementation
                never repeats anywhere.
            body: The JSON-serialisable document.
            timeout: Seconds any single connect or read phase may take.

        Returns:
            The outcome. An implementation reports a failure rather than raising
            it, so the caller records one shape of answer.

        """


class RequestsWebhookDeliverer:
    """The one implementation that opens a connection, with no retry and no session.

    A throwaway `requests.post` rather than a mounted session, deliberately:
    `core/transport.py` mounts an adapter *to get a retry policy*, and this
    seam's contract is that there is none. One call, one answer.
    """

    def post(self, url: str, *, body: Mapping[str, object], timeout: float) -> DeliveryOutcome:
        """Post the document once and report what came back.

        Args:
            url: Where to post.
            body: The JSON document.
            timeout: Seconds per connect and per read.

        Returns:
            Delivered for a `2xx`; otherwise the status the hook answered, or
            the class of the `requests` failure that produced no answer. The
            library's own message is discarded because it prints the URL, and
            the response body is never read.

        """
        try:
            # `requests` types `json=` as its own JSON alias; the seam's contract
            # is "a JSON-serialisable mapping", which is what the caller composes.
            # `stream=True` and the body never read: a hook that trickles a
            # response cannot hold the worker past the timeout it was given, and
            # nothing a hook says back is recorded -- only its status.
            document = cast("Any", dict(body))
            with requests.post(url, json=document, timeout=timeout, allow_redirects=False, stream=True) as response:
                response.raise_for_status()
                status = int(response.status_code)
        except requests.HTTPError as refused:
            # `raise_for_status` raised, so there is a response; its message names
            # the URL and is dropped here. The status is the whole of the detail.
            answered = refused.response.status_code if refused.response is not None else None
            return DeliveryOutcome(
                delivered=False, detail=HTTP_STATUS_DETAIL.format(status=answered), status_code=answered
            )
        except requests.RequestException as failure:
            return DeliveryOutcome(delivered=False, detail=type(failure).__name__)
        if not _FIRST_ACCEPTED_STATUS <= status <= _LAST_ACCEPTED_STATUS:
            # A `1xx` or a redirect: not refused by `raise_for_status`, and not
            # accepted either.
            return DeliveryOutcome(delivered=False, detail=HTTP_STATUS_DETAIL.format(status=status), status_code=status)
        return DeliveryOutcome(delivered=True, status_code=status)
