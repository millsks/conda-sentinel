"""The collector base: every external-call rule in one place, written once.

`CPM-AD-20` puts "rate limiting, retry with backoff, timeouts and caching ... in
a shared collector base in `core`, not per collector", and `CPM-AD-27` puts the
transport boundary here so that a collector is a pure translation from a recorded
payload to evidence rows. Eight collectors are coming. Written eight times these
rules would differ eight ways, and the difference that matters is not cosmetic:
`CPM-NFR-3` says the system "degrades to stale evidence, never to a clean
result", and a rate-limited source quietly producing an empty parse is exactly
that failure.

**What a subclass supplies, and what it never touches.** It declares nine values
-- `name`, `evidence_model`, `observation_window`, `freshness_target`,
`timeout`, `retries`, `rate_limit`, `headers`, `response_cache_ttl` -- and
implements three methods:
which source to read, how to turn a payload into evidence rows, and how to shape
one sentinel row for a table only it knows. It never sees a socket, a session, a
retry policy, a cache or a clock read. Every one of those is applied here.

**Caching is the fourth external-call rule, and it is this base's too.**
`CPM-NFR-3` names four -- rate limiting, retry with backoff, request timeouts and
caching -- and `CPM-AD-20` puts all four in one base. The first three arrived
with `CPM-EVIDENCE-S05`; this is the fourth. The base reads what it remembered
for a locator, composes the conditional request from the entry's validator,
merges the collector's declared headers underneath it, and replays a `304`'s
cached body through the collector's ordinary `translate`. A `304` is an
**observation**: it writes evidence and finalizes a `succeeded` ledger row like
any other answered call, because a confirmed-unchanged fact is a fact confirmed
now (`CPM-AD-5`, `R-01`), and re-observation inserts (`CPM-AD-2`), which is what
makes freshness advance without a body crossing the network.

**The cache write is last, and the ordering is the guarantee:**

```python
payload = self._transport.fetch(source, headers=headers)   # conditional
evidence = self.translate(payload, ...)                    # may raise, may be empty
written = self._write_evidence(evidence, observed_at=observed_at)
self._remember(...)                                        # only now
```

Caching before the parse would make one malformed body permanent: every later run
sends the validator, is answered `304`, replays the same body and fails
identically without ever re-reading the source. So nothing is remembered until
the evidence for that payload is in the table.

**A `304` with nothing cached fails.** It is the source contradicting the
request: no validator was sent, so there is no body and no observation exists to
record. Inventing an empty one is exactly the clean-looking result `CPM-NFR-3`
forbids, so the run is `failed` with an `error` row naming the source and the
misbehaviour.

**Headers reach the socket only from here** (`CPM-AD-20`, `CPM-AD-27`). A
collector declares what its source expects -- a `User-Agent`, an
`Authorization` -- and declares *nothing conditional*: `If-None-Match` and
`If-Modified-Since` are the base's, composed from what the response cache holds,
and a collector declaring one is refused at construction. A collector forging a
validator would be asking a question about a body this process does not have.

**The order is the whole guarantee, and it is written out rather than implied:**

```python
with collection_run(collector=self.name, clock=clock, package_id=package_id) as run:
    payload = self._transport.fetch(...)          # outside any transaction
    with transaction.atomic():                    # nested, one package
        self.evidence_model.objects.bulk_create(self.translate(payload))
```

**The run ledger is written outside any transaction this base opens.** That is
`CPM-EVIDENCE-S03`'s recorded, deferred constraint arriving at the story that
makes it enforceable. `core/ledger.py` states it: the `running` row is only worth
having because it is *committed* before the outbound call, so a caller wrapping
the recorder in `transaction.atomic()` and then being killed loses the row and
the ledger is back to recording nothing. No runtime guard was available -- pytest's
`django_db` runs every test inside exactly such a block, so a check on
`connection.in_atomic_block` would refuse the whole suite -- and the guard that
*is* available is a source sweep. This module is the first thing there was to
sweep, and `tests/unit/django_apps/test_collector_base_audit.py` is the sweep. It
runs in both directions: no transaction may enclose the recorder, and every
evidence write here must be inside one.

**The atomic unit is one package's evidence write** (`CPM-AD-23`), nested inside
the recorder and never around it. A later package's failure never rolls back an
earlier package's evidence, which is what makes `CPM-FR-15`'s partial success
reachable at all.

**Never a clean result, and never no row.** Every path that reaches the transport
writes evidence. A call that ultimately fails writes a row carrying `error`; a
source that answered "this does not exist" writes one carrying `not_found`; a
translation that raises writes `error` and *then* lets the exception out; and a
translation that returns **nothing** writes `error` too, because a parser that
found no rows in a body the source served is a parser that no longer matches its
source, and recording that as a clean success is the `R-01` ambiguity the outcome
vocabulary exists to remove. Both states come from `core/outcomes.py`'s
vocabulary and neither is `ok` (`CPM-AD-5`), and the base checks that the row a
collector hands back actually carries the state it was asked for.

**A question that does not apply is an observation too, and it is the one path
that writes a row without reaching the transport.** `CPM-FR-6` keeps
`not_applicable` apart from `not_found` and from `unknown`, and `CPM-FR-8` says
a non-Python package "is never marked stale against PyPI for not being
published there" -- which a package with *no row* would be, because a package
with no observation reads as `unknown` and ages from there. So a collector may
say, before any locator is asked for, that the question does not apply to this
package (`inapplicability`), and the base then writes the `not_applicable`
sentinel row itself, exactly as it writes `error` and `not_found`: no call is
made, no allowance is spent, no cache is read, and the run is `succeeded`
carrying the reason. The window still applies (`CPM-AD-7`), because a decision
not to observe is the same decision whatever the observation would have said.
The base decides the sentinel; what it does *not* decide is applicability, which
is read from `identity` and never guessed (`CPM-FR-1`).

The one path that writes nothing is the observation window, and it writes nothing
on purpose: `CPM-AD-7` says a second run inside the window records a ledger row
with status `skipped` and no evidence, which is a *decision not to observe*
rather than an observation.

**Re-observation inserts, and the instant is checked on the way in.** Evidence
goes through `AppendOnlyModel`, so this base uses `bulk_create` and never
`update_or_create` (`CPM-AD-2`). `bulk_create` does not call `save()`, which
means it bypasses the naive-`observed_at` refusal `core/models.py` calls "the one
place every evidence write passes through" -- so that refusal is restored here,
strengthened: every row must carry *exactly* the instant this run was handed, not
merely an aware one. One observation has one moment (`CPM-AD-7`), and a collector
that stamped rows from somewhere else would make every freshness comparison and
every observation window silently wrong.

**Rate limiting is reconciled with retry.** The limiter is charged `1 + retries`
per collection, because that is how many real requests the mounted retry policy
may issue. See `core/rate_limit.py`.

**Freshness targets and observation-window values are not chosen here.** They are
PRD Open Question 7 and are declared per collector; this module builds the
mechanism and reads them. What a declared target *means* -- whether evidence has
aged past it -- is `core/freshness.py`'s and not this module's, for the reason
`core/outcomes.py` owns the status vocabulary: eight collectors and every read
surface asking the same question have to get the same answer.

**The freshness target is refused here and swept at boot, and those are one rule
with two moments rather than two rules.** `_require_freshness_target` below is
the enforcement point: absence, a mistyped value and a zero or negative interval
are all refused where the collector is constructed, exactly as the other seven
declarations are. `config/startup/stage_two.py`'s
`_refuse_collector_without_freshness_target` calls *this* function over the
registered classes so the same defect is also fatal at boot -- which is what
`CPM-AD-28` asks for, because a refusal that waits for the first construction in
a queue is a refusal a worker meets after the ledger row is already `running`.
There is still one place the rule is written.

**A zero target is refused, unlike a zero window, and the asymmetry is
deliberate.** `NO_WINDOW` means "observe on every run", which is a thing an
operator means and says. A zero freshness target means "evidence is stale the
instant it is written", which nobody means and which would make every surface
permanently amber. The two sentinels look alike and behave oppositely, so the
refusal is written out rather than inherited by symmetry.

**There is a second entry point, and it is run-scoped rather than package-scoped.**
`collect(package_id=...)` reads one locator per package and writes one package's
rows. Inventory ingestion (`CPM-AD-25`, `CPM-FR-42`) reads **one** document that
names **many** packages, and some of them have no row yet -- so there is no
`package_id` to pass and no per-package locator to ask for. `sweep()` is that
path: it opens the same recorder with `package_id=None`, which
`core/ledger.py` already accepts for a run that is not scoped to one package, and
it reuses this base's clock, window, limiter and transport unchanged. What it
does *not* do is decide how a document becomes rows -- that is
`persist_sweep`, the subclass's, because only the subclass knows what a record
is, which package it names and which table it belongs in. The per-package path
below is untouched by all of it: eight later collectors inherit `collect`, and
reshaping it for the one collector that reads a document would be reshaping it
for the seven that do not.

**The sweep gets every guarantee the per-package path has, and it gets them the
same way: by writing through this base.** `persist_sweep` opens the per-package
transaction (`CPM-AD-23`) but hands its rows to `_write_evidence`, which checks
they belong to the declared evidence model, checks they carry this run's instant,
inserts them through the append-only manager and counts them. The count is then
reconciled against what the subclass says it wrote. A report is not a guarantee:
a `SweepOutcome` naming rows nobody inserted would otherwise finalize a
successful run, and a subclass calling `bulk_create` itself would walk around the
stamping check exactly as `bulk_create` walks around `save()`.

**The sweep reads unconditionally, and that is a bound rather than an
oversight.** The response cache's whole value is the `304` replay, and replaying
one writes evidence through `translate` -- which is per package. A run-scoped
read has no package to attribute a revalidation to, so the sweep asks for the
document outright and remembers nothing; the collector that uses this path
declares `NO_CACHE`, which is the same statement made where a reader can see it.

**Nothing is invented on a sweep that fails, and no sentinel row is written
either.** `sentinel_evidence` shapes a row *about one package*, and a source that
could not be read named none -- so a failing sweep records the reason on its
ledger row and writes no evidence at all. That is not the clean result
`CPM-NFR-3` forbids: the run is `failed`, loudly, and no observation claims
anything about any package.

**On the `AD-` prefix.** A bare `AD-n` in this repository is an *inherited*
platform decision; a decision from this product's own architecture spine always
carries the `CPM-` prefix.
"""

from __future__ import annotations

from abc import ABC
from abc import abstractmethod
from collections.abc import Mapping
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from http import HTTPStatus
from math import isfinite
from types import MappingProxyType
from typing import TYPE_CHECKING
from typing import ClassVar
from typing import Final
from typing import Self

import structlog
from django.db import DatabaseError
from django.db import models
from django.db import transaction

from conda_sentinel.core.clock import is_aware
from conda_sentinel.core.credentials import AUTHORIZATION_HEADER
from conda_sentinel.core.credentials import carries_credential
from conda_sentinel.core.credentials import credentialed_headers
from conda_sentinel.core.credentials import host_of
from conda_sentinel.core.freshness import UNOBSERVED_STATUS
from conda_sentinel.core.freshness import FreshnessReport
from conda_sentinel.core.freshness import freshness_of
from conda_sentinel.core.freshness import latest_observation
from conda_sentinel.core.ledger import collection_run
from conda_sentinel.core.models import AppendOnlyModel
from conda_sentinel.core.models import CollectionRun
from conda_sentinel.core.outcomes import OutcomeState
from conda_sentinel.core.rate_limit import CacheRateLimiter
from conda_sentinel.core.rate_limit import RateLimit
from conda_sentinel.core.response_cache import CachedResponse
from conda_sentinel.core.response_cache import CacheResponseCache
from conda_sentinel.core.response_cache import ResponseCacheError
from conda_sentinel.core.response_cache import conditional_headers
from conda_sentinel.core.runs import RunState
from conda_sentinel.core.transport import DEFAULT_RETRIES
from conda_sentinel.core.transport import IF_MODIFIED_SINCE_HEADER
from conda_sentinel.core.transport import IF_NONE_MATCH_HEADER
from conda_sentinel.core.transport import Payload
from conda_sentinel.core.transport import RequestsTransport
from conda_sentinel.core.transport import TransportError

if TYPE_CHECKING:
    from collections.abc import Iterable
    from datetime import datetime
    from types import TracebackType

    from conda_sentinel.core.clock import Clock
    from conda_sentinel.core.ledger import RunHandle
    from conda_sentinel.core.rate_limit import RateLimiter
    from conda_sentinel.core.response_cache import ResponseCache
    from conda_sentinel.core.transport import Transport

__all__ = [
    "COLLECTION_CREDENTIAL_REFUSED_EVENT",
    "COLLECTION_FAILED_EVENT",
    "COLLECTION_NOT_APPLICABLE_EVENT",
    "COLLECTION_NOT_MODIFIED_EVENT",
    "COLLECTION_NOT_REMEMBERED_EVENT",
    "COLLECTION_PARTIAL_EVENT",
    "COLLECTION_REFUSED_EVENT",
    "COLLECTION_SKIPPED_EVENT",
    "CONDITIONAL_HEADERS",
    "EVENT_KEYS",
    "NO_CACHE",
    "NO_CADENCE",
    "NO_FRESHNESS",
    "NO_WINDOW",
    "REFUSED_CREDENTIAL_STATUS",
    "STATE_FIELD",
    "SUPPRESSING_STATES",
    "CollectionResult",
    "CollectionWriteError",
    "Collector",
    "CollectorConfigurationError",
    "SweepOutcome",
    "failure_detail",
    "freshness_target_fault",
    "has_recent_success",
    "request_headers",
    "require_cadence",
    "require_freshness_target",
    "window_query",
]

logger = structlog.get_logger(__name__)

#: The event a windowed skip is logged under. Dotted, as
#: `config/health/drain.py`'s `drain.begin` is; named so the case that asserts
#: the log and the code that emits it cannot drift, exactly as
#: `core/ledger.py`'s `FINALIZATION_FAILED_EVENT` is and
#: `tests/integration/django_apps/test_run_ledger.py` proves.
COLLECTION_SKIPPED_EVENT: Final[str] = "collection.skipped_inside_window"

#: The event a rate-limit refusal is logged under. Distinct from a failure: the
#: call was never issued, which is a different operational fact from a source
#: that answered badly, and an operator reading "why did this collect nothing"
#: needs to tell them apart.
COLLECTION_REFUSED_EVENT: Final[str] = "collection.refused_by_rate_limit"

#: The event a failed call is logged under. Emitted from three places -- a
#: transport failure, a translation that raised, and a translation that found
#: nothing -- and every one of them carries the same keys, so a log query does
#: not have to know which.
COLLECTION_FAILED_EVENT: Final[str] = "collection.failed"

#: The event a collection refused *before any call* because the source
#: already refused the declared credential earlier in this window
#: (`CPM-OPERATE-S05`). Distinct from `COLLECTION_FAILED_EVENT`, which is the
#: run that met the `401`, and from `COLLECTION_REFUSED_EVENT`, which is an
#: allowance spent: this one is an operator's setting known to be wrong, and
#: it is logged so that a sweep failing fast ten thousand times in an hour
#: reads as one cause rather than ten thousand.
COLLECTION_CREDENTIAL_REFUSED_EVENT: Final[str] = "collection.credential_refused_this_window"

#: The event a confirmed-unchanged answer is logged under. Distinct from a
#: success and from a skip, because it is neither: the source was asked and
#: answered, and the answer was that nothing changed. An operator reading "why is
#: this collector transferring nothing" needs to tell a working cache from a
#: window that is suppressing runs.
COLLECTION_NOT_MODIFIED_EVENT: Final[str] = "collection.not_modified"

#: The event an answer that cannot be remembered is logged under. A source that
#: offers neither an `ETag` nor a `Last-Modified` is not a defect and does not
#: fail the run -- it is a source this collector will keep re-reading in full --
#: but it is not nothing either: it is the reason a collector that declared a
#: cache lifetime never sees a `304`, and an operator asking why is otherwise
#: looking at a cache that is configured, working, and empty for no visible
#: reason.
COLLECTION_NOT_REMEMBERED_EVENT: Final[str] = "collection.not_remembered"

#: The event a run-scoped sweep that did some of its work is logged under.
#:
#: Distinct from `COLLECTION_FAILED_EVENT`, and the separation is the same one
#: `_reported` is written for: `partial` and `failed` are different operational
#: facts -- one of them means packages were written and one means none were -- and
#: a log query that had to tell them apart by parsing `detail` would be reading
#: two schemas out of one key. Only the run-scoped path emits it: a per-package
#: collection observes one package and cannot half-succeed.
COLLECTION_PARTIAL_EVENT: Final[str] = "collection.partial"

#: The event a question that does not apply is logged under. Distinct from a
#: skip and from a success with a body: the source was never asked, and that is
#: the right outcome rather than a suppressed one. An operator reading "why does
#: this collector never call its source for these packages" needs to tell a
#: window that is suppressing runs from a collector that has, correctly, nothing
#: to ask. It carries `EVENT_KEYS` like every other event, and its `source` is
#: the empty string -- no locator exists on this path, because the collector was
#: never asked for one, and inventing one for the log line would be worse than
#: saying so.
COLLECTION_NOT_APPLICABLE_EVENT: Final[str] = "collection.not_applicable"

#: The keys every one of the seven events above carries, and the reason they are
#: named here. A `detail` that is an exception's `"Type: message"` on two paths
#: and a sentence on the third is two schemas wearing one key, and a log query
#: written against either would be wrong half the time. So the value is fixed as
#: *the same string the ledger row's `detail` column carries* on every path --
#: which is also what `CollectionResult.detail` promises a caller.
EVENT_KEYS: Final[tuple[str, ...]] = ("collector", "package_id", "source")

#: The shortest window that means anything. Zero is permitted and means "never
#: skip": a collector that should observe on every run says so by declaring it,
#: which is a decision a reader can see, rather than by omitting the value. It is
#: also short-circuited rather than queried -- see `Collector.collect`.
NO_WINDOW: Final[timedelta] = timedelta(0)

#: The shortest freshness target that means anything, and it is the one value
#: `require_freshness_target` refuses rather than accepts.
#:
#: Named beside `NO_WINDOW` deliberately, because the two are the same interval
#: and the opposite decision: `NO_WINDOW` says "observe on every run", which an
#: operator means, while a zero target would say "evidence is stale the instant
#: it is written", which nobody means. A reader meeting one of them here meets
#: the other, which is what stops the symmetry being assumed.
NO_FRESHNESS: Final[timedelta] = timedelta(0)

#: The shortest cadence that means anything, and the third of these zero
#: intervals. Named beside the two above for the reason `NO_FRESHNESS` is named
#: beside `NO_WINDOW`: all three are `timedelta(0)` and only one of them is a
#: value an operator ever means. A zero *window* says "observe on every run"; a
#: zero *target* would say "stale the instant it is written"; a zero *cadence*
#: would say "fire continuously", which is a worker that never stops enqueueing.
#: `require_cadence` refuses it.
NO_CADENCE: Final[timedelta] = timedelta(0)

#: The run states that suppress a later observation inside the window.
#:
#: Exactly one. `failed` is excluded because a source that is broken must not
#: suppress the observation that would record it as broken; `partial` is excluded
#: because a run that did *some* of its work did not observe this package
#: completely, and treating it as an observation is the same false-clean move by
#: a quieter route; `running` is excluded because it has not finished. Derived as
#: a set rather than written into the query so the decision is one a reader meets
#: rather than one they infer from a keyword.
SUPPRESSING_STATES: Final[frozenset[RunState]] = frozenset({RunState.SUCCEEDED})

#: The response-cache lifetime that means "do not cache". Zero, and it is a
#: *declaration* on the same terms `NO_WINDOW` is: a collector whose source
#: offers no validator, or whose body must be re-read every time, says so where a
#: reader can see it rather than by omitting the value.
#:
#: Short-circuited rather than handed to the cache. Django reads a `timeout` of
#: `0` as *do not cache* and `None` as *never expire*, so passing this value
#: through would happen to produce the right behaviour by way of a rule nobody
#: reading this line would have to know -- and one `int()` away from the opposite
#: one. `_require_cache_ttl` refuses everything between zero and a whole second
#: for the same reason: it truncates to `0`, which is a collector that declared
#: caching and silently caches nothing.
NO_CACHE: Final[timedelta] = timedelta(0)

#: The request headers this base owns and no collector may declare.
#:
#: They are composed from what the response cache holds, so a collector
#: declaring one would be asking a source about a body this process does not
#: have -- and would be answered `304` for it, which the base would then have no
#: entry to replay. Refused at construction, where the answer already exists.
CONDITIONAL_HEADERS: Final[frozenset[str]] = frozenset({IF_NONE_MATCH_HEADER, IF_MODIFIED_SINCE_HEADER})

#: The same set, lowered once at import. HTTP header names are case-insensitive
#: on the wire, so the refusal has to be, and computing the comparison set per
#: declared header would be rebuilding a constant inside a loop.
_LOWERED_CONDITIONAL_HEADERS: Final[frozenset[str]] = frozenset(header.lower() for header in CONDITIONAL_HEADERS)

#: The status a source answers when it refuses the credential a request carried
#: (`CPM-OPERATE-S05`). Named here rather than in `core/transport.py` because
#: the transport records every non-success status the same way and this base
#: is where one of them is worded differently: a `401` is not the source being
#: broken, it is the source saying the declared credential is wrong, and an
#: operator reading the ledger needs that told apart from a `500` without the
#: header that earned it being repeated anywhere. It is deliberately **not** in
#: `DEFAULT_RETRY_STATUSES`: a refused credential answers identically however
#: many times it is asked.
REFUSED_CREDENTIAL_STATUS: Final[int] = int(HTTPStatus.UNAUTHORIZED)


class CollectorConfigurationError(ValueError):
    """A collector's declared configuration cannot be used to make a call.

    One type rather than a hierarchy, on the same terms as `core/models.py`'s
    `AppendOnlyError`: every occurrence is a defect in a class definition or in
    an override of one, to be fixed where that code is written, and no caller
    branches on which declaration was wrong. The detail is in the message.

    A `ValueError` subclass, matching `core/rate_limit.py`'s `RateLimitError` and
    `core/outcomes.py`'s `OutcomeVocabularyError`: every "this declaration is
    unusable" in this product is a `ValueError`, so a caller catching one catches
    them all. `core/transport.py`'s `TransportError` is deliberately not one -- a
    source being down is not a declaration defect.

    It is raised at **construction** wherever it can be, which is the matrix row
    `CPM-EVIDENCE-S05` states in so many words: a collector configured with no
    timeout is refused when it is built. The alternative is a worker discovering
    it halfway through a sweep, with a run ledger row already `running` and a
    source already contacted. The two checks that cannot happen at construction --
    that `translate` stamps the instant it was handed, and that
    `sentinel_evidence` carries the state it was asked for -- are made at the
    write, which is the first moment the answer exists.
    """


class CollectionWriteError(Exception):
    """Evidence could not be written, on a path that was already recording a failure.

    Not a declaration defect and so not a `ValueError`. It exists for one
    ordering hazard: on a failing path the base has a *reason* -- the source was
    unreachable, the allowance was spent, the parser broke -- and it then writes
    a sentinel row. If that write raises, the database error would reach the run
    recorder and become the ledger row's `detail`, replacing the reason with a
    message about the write. So the write's failure is wrapped in this, carrying
    both, and `core/ledger.py`'s recorder records the pair.

    Attributes:
        detail: The reason the run was already failing, preserved verbatim so a
            reader is never left with only the epitaph.

    """

    def __init__(self, message: str, *, detail: str) -> None:
        """Record the combined message and the original reason.

        Args:
            message: The reason and the write failure, in that order.
            detail: The reason on its own.

        """
        super().__init__(message)
        self.detail = detail


@dataclass(frozen=True, slots=True)
class CollectionResult:
    """What one collection run did, for the caller that asked for it.

    The ledger row is the durable record and this is the immediate answer: a
    Celery task (`CPM-EVIDENCE-S04`'s, not this story's) returns it, and a test
    asserts against it without reading the row back. Frozen, because it is a
    report rather than a workspace.

    Attributes:
        state: How the run ended, over `RunState` -- the same value the ledger
            row carries, never `RunState.RUNNING`.
        evidence_rows: How many evidence rows were inserted. On the per-package
            path, zero only for a windowed skip; every other path writes at least
            one row, which is `CPM-NFR-3`'s "never no row". On the run-scoped
            sweep it is also zero for a run that failed before any record was
            read -- a sweep that could not reach its source named no package, and
            there is no package for a sentinel row to be about.
        detail: Why the run ended this way, in the same words the ledger row's
            `detail` carries -- every terminal path declares the same string to
            both, which
            `tests/integration/django_apps/test_collection.py` asserts. Empty for
            a plain success, which needs no explanation.

    """

    state: RunState
    evidence_rows: int
    detail: str


@dataclass(frozen=True, slots=True)
class SweepOutcome:
    """What one subclass's run-scoped persistence did, for the base that asked.

    The sweep's counterpart to `CollectionResult`: the base owns the ledger row
    and the terminal state, the subclass owns the rows, and this is the whole of
    what passes between them. Frozen, because it is a report rather than a
    workspace.

    **The two row counts are separate, and collapsing them is a real defect
    rather than a tidiness question.** A sweep writes rows of two kinds: rows
    derived from records the source actually supplied, and rows the *absence* of
    a record implies -- an inventory sweep records a package the source stopped
    naming as `not_found` (`CPM-AD-25`). Only the first kind is evidence that the
    source was read and understood. A single total lets the second kind stand in
    for the first, and the consequence is not subtle: a source that served an
    empty document, or one whose every record failed, would write an absence row
    for every package the system has ever seen, count them, and report a run that
    succeeded. Those rows are permanent -- the log is append-only and nothing may
    correct it -- and every later replay would read them. So the base decides
    "did this produce anything" on `observed_rows` alone.

    **Failures are a list rather than a count, and both halves matter.**
    `CPM-AD-23`'s atomic unit is one package, so a sweep that could not persist
    the middle of three records has committed the other two and must say so --
    which is `partial` and not `failed` (`CPM-FR-15`). A bare count would leave
    the ledger row saying "two of three" and nothing about which one or why, and
    the run's `detail` is the only durable record of that.

    Attributes:
        observed_rows: How many rows came from records the source supplied. Zero
            means the source produced nothing this run could act on, which the
            base reads as a failure for the reason an empty `translate` is one: a
            parser that finds nothing in a document its source served no longer
            matches its source.
        derived_rows: How many rows this run wrote about packages the source did
            *not* name. Real evidence and counted in the result, but never
            evidence that the source was read -- see above.
        failures: One message per package that could not be persisted, in the
            order they were met. Empty for a run in which every record was
            written.

    """

    observed_rows: int
    derived_rows: int = 0
    failures: tuple[str, ...] = ()

    @property
    def evidence_rows(self) -> int:
        """Return how many rows the run wrote in total.

        Returns:
            Both kinds added together, which is what a caller asking "how much
            did this run record" means and what `CollectionResult` carries. It is
            deliberately *not* what the base branches on.

        """
        return self.observed_rows + self.derived_rows


def window_query(*, collector: str, package_id: int | None, since: datetime) -> models.Q:
    """Return the filter that finds a successful observation inside the window.

    Four conditions, and three of them are the matrix rows that would otherwise
    be silent. `collector` and `package_id` are what make the window
    *per collector and per package*: a recent run of another collector, or of
    this one against another package, must not suppress this observation.
    `status` is what makes only a **succeeded** run suppress -- see
    `SUPPRESSING_STATES` for why `failed` and `partial` are both excluded and
    what each exclusion prevents.

    `finished_at` rather than `started_at` is the authority for the same reason
    `RunLedgerQuerySet.unfinished()` reads it: it is the column the recorder's
    `finally` writes, so it exists exactly when the run has an ending.

    Args:
        collector: The declared collector name, as its ledger rows carry it.
        package_id: The package the window is being asked about, by the integer
            primary key `CPM-AD-3` fixes, or `None` for a run-scoped sweep. Django
            reads `package_id=None` as `IS NULL`, which is exactly the question a
            sweep asks: did a previous run *of this collector that was not scoped
            to one package* succeed inside the window? A sweep must not be
            suppressed by a package-scoped run and vice versa, which is the same
            per-package separation the integer form makes.
        since: The start of the window -- `now` minus the declared observation
            window, from the injected clock (`CPM-AD-26`).

    Returns:
        A `Q` naming all four conditions. Returned as a `Q` rather than applied
        here so that `tests/unit/django_apps/test_collection.py` can assert its
        shape without a database: the three conditions above are the difference
        between a correct window and one that suppresses the wrong runs, and
        each of them is one keyword away from being absent.

    """
    return models.Q(
        collector=collector,
        package_id=package_id,
        status__in=sorted(state.value for state in SUPPRESSING_STATES),
        finished_at__gte=since,
    )


def has_recent_success(*, collector: str, package_id: int | None, since: datetime) -> bool:
    """Report whether this collector already observed this package inside the window.

    Args:
        collector: The declared collector name.
        package_id: The package being collected, or `None` for a run-scoped
            sweep.
        since: The start of the window.

    Returns:
        True when a `succeeded` run for this collector and package finished at
        or after `since`. `exists()` rather than a fetch: the answer is a
        boolean and the rows are not wanted.

    """
    return CollectionRun.objects.filter(window_query(collector=collector, package_id=package_id, since=since)).exists()


def request_headers(*, declared: Mapping[str, str], entry: CachedResponse | None) -> dict[str, str]:
    """Return the headers one outbound call carries, from both of their sources.

    Two sources and one order. What the collector declared -- the `User-Agent`
    its source expects, the `Authorization` its API requires -- goes down first;
    the conditional request the response cache asks for goes on top. The base's
    headers therefore win, which is belt and braces rather than a live conflict:
    a collector declaring a conditional header is already refused at
    construction, and this order means a refusal that was ever bypassed would
    still not let a forged validator reach a source.

    Returned as a plain mapping rather than applied here, for the reason
    `window_query` is returned as a `Q`: the composition is one dictionary merge
    away from being wrong in a way no behavioural case would notice -- a lost
    declaration, or a conditional header overwritten by the collector's own --
    and `tests/unit/django_apps/test_collection.py` asserts the shape without a
    socket.

    Args:
        declared: What the collector declared, already checked at construction.
        entry: The remembered response for this locator, or `None` for a miss,
            for a collector that caches nothing, and for a run that is not
            allowed to ask conditionally.

    Returns:
        The merged headers. Empty when a collector declares none and nothing is
        remembered, which is the same request `CPM-EVIDENCE-S05` issued.

    """
    return {**declared} if entry is None else {**declared, **conditional_headers(entry)}


def failure_detail(failure: TransportError, *, credentialed: bool) -> str:
    """Return what the ledger, the evidence row and the log say about one failed fetch.

    One sentence shape for every failure but one. A transport failure is
    recorded as the exception's type and message -- the message already names
    the locator and the status, and never a header. The exception is a `401`
    to a request that *carried the declared credential*: the source did
    answer, and what it said is that the credential is not one it accepts.
    That is an operator's problem with a setting rather than a source's
    problem with itself, so the sentence says so and names the host that
    refused -- and only the host. A `401` to a request that carried no
    credential is not that: it is a source demanding one this collector never
    declared, and is worded as any other failure. The credential, its prefix
    and the header it travelled in appear in no message on any path
    (`CPM-OPERATE-S05`); this base never had them in a string to begin with,
    and this function does not start.

    Args:
        failure: What the transport raised.
        credentialed: Whether the request that failed carried `Authorization`
            -- read off the headers actually sent, so a request the base
            stripped the credential from is not reported as a refused one.

    Returns:
        `"<Type>: <message>"` for every failure, except that a
        `REFUSED_CREDENTIAL_STATUS` answer to a credentialed request reads
        `"the declared credential was refused by <host>: <message>"`. The host
        is read from the failure's own `source`; a locator with no readable
        host falls back to the locator itself, which the transport built and
        which carries no credential.

    """
    if credentialed and failure.status_code == REFUSED_CREDENTIAL_STATUS:
        return f"the declared credential was refused by {host_of(failure.source) or failure.source}: {failure}"
    return f"{type(failure).__name__}: {failure}"


#: The column every evidence model carries its outcome in, verbatim
#: (`CPM-AD-5`, `CPM-AD-24`). Named here because this base *reads* it: a sentinel
#: row is checked against the state it was asked for, and the check has to know
#: which column says so rather than scanning every field for a matching string.
#: `tests/unit/django_apps/test_outcome_field_audit.py` is what keeps every model
#: spelling it this way.
STATE_FIELD: Final[str] = "state"


class Collector(ABC):
    """The base every collector inherits, and the only code that makes a call.

    **Declared configuration, not implemented behaviour.** The nine class
    attributes the constructor reads are what a subclass states about itself
    (`cadence` is a tenth and the one exception -- see its own comment); every
    one of the nine
    is checked -- for presence *and* for type -- when the collector is
    constructed, so a class that forgets one, or declares a string where an
    interval belongs, fails where it is built with a message naming the
    declaration rather than with a `TypeError` from somewhere downstream.
    `CPM-AD-20`'s rule -- that these live in one shared base -- is only true if a
    subclass has no way of its own to make a call, and the abstract methods are
    shaped so that it does not: it says *what* to fetch and *what a payload
    means*, and never *how* to fetch.

    **Three abstract methods, and the third is not an accident.** `translate` is
    the one `CPM-AD-27` is about. `source_for` is what makes the URL a property
    of the collector rather than of the base. `sentinel_evidence` exists because
    the base owns the *rule* -- an `error` row for a failed call, a `not_found`
    row for an absent resource, always, never `ok` -- while the collector owns
    the *shape* of a row in a table `CPM-AD-7` gives it and this base has never
    seen. A base that built the row itself would need a concrete evidence model,
    which `CPM-AD-7` puts in `CPM-EP-CURRENCY` and which this story is forbidden
    to invent. What the base does *not* do is trust the result: it checks that
    the row carries the state it asked for, because a subclass that ignored the
    argument and wrote `ok` would defeat "never a clean result" entirely and
    would type-check perfectly.

    **Three defaulted hooks sit beside the three abstract ones, and each was
    added when an acceptance criterion could not be met without it.**
    `inapplicability` (`CPM-CURRENCY-S02`) answers "applies" by default, so a
    collector whose question applies to every package declares nothing.
    `sentinel_evidence_rows` (`CPM-CURRENCY-S04`) answers with the one row
    `sentinel_evidence` shapes, so a collector that observes one surface per
    package declares nothing either -- and a collector that observes *several*
    surfaces in one collection can say what the whole run owes on a path where
    the base, rather than the collector, decides what a row says.
    `selectable_packages` (`CPM-CURRENCY-S05`) answers `None`, meaning "this
    collector is not swept one package at a time", so a collector nothing
    dispatches per package declares nothing either. All three are non-abstract
    and all three have defaults that leave every collector written before them
    byte-identical, which is the only shape in which this base grows.

    **The clock is a constructor parameter and there is no default**
    (`CPM-AD-26`): `observed_at`, the window comparison, the rate-limit window
    and the ledger's two instants all read it, and a base that reached for
    `SystemClock()` on its own would make every window test a statement about how
    long the test took.

    **It is closable, and it is a context manager.** A collector that was handed
    no transport builds one, and that transport holds a connection pool; `close()`
    releases it and closes nothing the caller owns.
    """

    #: What this collector is called, on its ledger rows and in its cache keys.
    #: A blank name is refused: `CPM-FR-39` needs a run traceable to the code
    #: that performed it, and `core/ledger.py` refuses one for the same reason.
    name: ClassVar[str] = ""

    #: The append-only table this collector writes (`CPM-AD-7`: its own, never
    #: another collector's). Declared rather than owned by this base, which
    #: deliberately holds no concrete evidence model. It must inherit
    #: `AppendOnlyModel`: a plain `Model` would satisfy every type annotation
    #: here and would go around every refusal `CPM-AD-2` exists to make.
    evidence_model: ClassVar[type[AppendOnlyModel] | None] = None

    #: How long a successful observation suppresses the next one (`CPM-AD-7`).
    #: The *value* is PRD Open Question 7 and is not chosen here.
    #: `NO_WINDOW` is permitted and means "observe on every run".
    observation_window: ClassVar[timedelta | None] = None

    #: Seconds any single connect or read phase may take. Applied by the
    #: transport this base builds from it; refused at construction when it is
    #: absent, which is the matrix row that says a timeout has no default-less
    #: path. `core/transport.py` says what the value does and does not bound.
    timeout: ClassVar[float | None] = None

    #: How many times a failed request is retried. Declared here as well as
    #: applied by the transport, because it is what the rate limiter is charged:
    #: one collection may issue `1 + retries` requests, and an allowance that
    #: counted collections would not bound requests at all.
    retries: ClassVar[int] = DEFAULT_RETRIES

    #: How hard this collector may push its source (`CPM-AD-20`). Required
    #: rather than optional: "never issued unlimited" is not a property a
    #: collector gets to opt out of by omission.
    rate_limit: ClassVar[RateLimit | None] = None

    #: The request headers this collector's source expects -- a `User-Agent`, an
    #: `Authorization`. Declared rather than sent: the base merges them under the
    #: conditional request and hands the result to the transport, which is the
    #: only place a header reaches a socket (`CPM-AD-20`, `CPM-AD-27`). Empty is
    #: a complete statement, so unlike the six declarations above this one has a
    #: usable default; what is *refused* is a conditional header, which belongs
    #: to the base and to the entry it is composed from.
    headers: ClassVar[Mapping[str, str]] = MappingProxyType({})

    #: The one host the `Authorization` header in `headers` belongs to, or
    #: `None` for a collector that declares no credential (`CPM-OPERATE-S05`).
    #: This base composes the headers for every primary fetch, and it is here
    #: that the credential is kept to its host: a locator on any other host,
    #: or on this host over plain `http`, is sent the declaration *without*
    #: `Authorization` -- so a rewritten `source_for` cannot carry a bearer to
    #: a stranger. A collector that declares `Authorization` and no host is
    #: refused at construction, because a credential with nowhere it belongs
    #: is a credential that goes everywhere.
    credential_host: ClassVar[str | None] = None

    #: How long evidence this collector wrote may be read as current
    #: (`CPM-AD-28`). Required, and with no sentinel for "never goes stale": an
    #: unset target behaving as fresh-forever is the named failure -- six-month-
    #: old evidence reading as current -- rather than a configuration anybody
    #: chooses. The *value* is PRD Open Question 7 and is not chosen here; its
    #: presence is not optional. `core/freshness.py` is what compares against it.
    freshness_target: ClassVar[timedelta | None] = None

    #: How long a remembered response may be replayed before it is re-read.
    #: Required, on the same terms `observation_window` is: `NO_CACHE` says
    #: "fetch a body every time" and a reader can see it, while omitting the
    #: value says nothing at all. The *value* is a per-collector decision like
    #: the window's and is not chosen here.
    response_cache_ttl: ClassVar[timedelta | None] = None

    #: How often this collector is meant to run (`CPM-CURRENCY-S05`), and the
    #: tenth declaration -- the first one this base does **not** check at
    #: construction.
    #:
    #: `None` means "nothing schedules this collector", which is the honest
    #: default: a collector reached only by `CPM-UJ-1`'s manual recollection has
    #: no cadence, and demanding one would make the absence of a schedule a
    #: construction failure rather than a fact. What it is *for* is
    #: `CPM-AD-20`'s reconciliation: the schedule itself lives in
    #: `CELERY_BEAT_SCHEDULE` as data, and a component whose declared cadence and
    #: schedule entry disagree refuses to start -- which is the failure
    #: `CPM-CURRENCY-S01` recorded, a weekly schedule against a daily-derived
    #: two-day target making the whole inventory read stale five days out of
    #: seven with every gate green.
    #:
    #: **Where that refusal fires is `collectors/apps.py`'s `ready()`**, because
    #: that is the hook which *populates* the registry: `config/startup/
    #: stage_two.py` evaluates the same rule as condition 11, but it runs from
    #: the platform owner's `ready()`, which every adopted application is
    #: installed after -- so in a deployed process it sweeps a registry that is
    #: still empty. Both call one function
    #: (`collectors/sweep.py`'s `cadence_reconciliation_fault`); only the
    #: application's own hook is positioned to see anything.
    #:
    #: It is not checked here because a `None` is legitimate and a declared one
    #: is only meaningful against a schedule this object cannot see. The
    #: enforcement point is `require_cadence`, called by the boot sweep -- the
    #: same shape `require_freshness_target` has, minus the constructor half.
    cadence: ClassVar[timedelta | None] = None

    @classmethod
    def selectable_packages(cls) -> Iterable[int] | None:
        """Return the packages this collector can be asked about, or say it is not swept per package.

        The hook `CPM-CURRENCY-S05` added, and the third defaulted one. Every
        per-package collector already refuses, from `source_for`, the packages it
        cannot answer about -- an unresolved release-ecosystem mapping, a package
        with no source repository -- and each of those refusals was recorded as a
        deferred item saying that a sweep offering *every* package would fail
        every run. The set a collector can answer about is the complement of its
        own refusals, so it is declared here, beside them: a dispatch module
        holding a table of four preconditions would be a second place to edit
        whenever a collector's refusals change, and the two would diverge
        silently.

        **`None` is not an empty selection.** It means "this collector is not
        swept one package at a time" -- inventory ingestion reads one document
        naming many packages (`CPM-AD-25`) and its run-scoped `sweep()` is the
        whole of its schedule -- and `collectors/sweep.py` refuses to dispatch
        such a collector *by name* rather than skipping it silently. An empty
        iterable is a different answer and a successful one: the selection ran
        and matched nothing.

        **A classmethod, where `inapplicability` and `sentinel_evidence_rows`
        are instance methods.** Both of the callers read it off a class:
        `collectors/sweep.py` selects and enqueues without ever collecting, and
        constructing a collector to ask would build a `RequestsTransport` and its
        connection pool for a dispatch that makes no call; `config/startup/
        stage_two.py` sweeps registered *classes* for exactly the same reason,
        which its own docstring records. Nothing here reads instance state,
        because there is none to read before a run begins.

        Returns:
            An iterable of package primary keys, or `None`. The iterable is
            **streamed, never materialised**: `CPM-NFR-1`'s ten thousand packages
            must reach the queue without a list of ten thousand ids existing
            anywhere, so an implementation returns a lazy queryset or an iterator
            and `collectors/sweep.py` chunks it. The default answers `None`, so
            every collector written before this hook declares nothing new.

        """
        return None

    def __init__(
        self,
        *,
        clock: Clock,
        transport: Transport | None = None,
        limiter: RateLimiter | None = None,
        response_cache: ResponseCache | None = None,
    ) -> None:
        """Check the declared configuration and build what it describes.

        Args:
            clock: The clock every instant in this run is read from
                (`CPM-AD-26`). No default: a component's dependence on time
                belongs in its signature.
            transport: The seam `CPM-AD-27` opens. Defaults to a
                `RequestsTransport` carrying this collector's declared timeout
                and retry count, which is the only place either becomes a call
                setting. A test substitutes a fake returning recorded payloads,
                and `CPM-AD-29`'s inventory adapter substitutes a file reader.
            limiter: The rate limiter, defaulting to the shared cache-backed one.
                Substituted in a test that wants a refusal without arranging a
                cache state.
            response_cache: Where answers are remembered between runs, defaulting
                to the shared cache-backed one. A seam for the same reason the
                limiter is one: a case about the replay should not have to
                arrange a cache entry through a key it then has to compute.

        Raises:
            CollectorConfigurationError: When any declared value is absent, of
                the wrong type, or unusable. Raised here rather than at call
                time, so a misdeclared collector never reaches a source.

        """
        label = type(self).__name__
        self._name = _require_name(self.name, label=label)
        self._evidence_model = _require_evidence_model(self.evidence_model, label=label)
        self._window = _require_window(self.observation_window, label=label)
        self._freshness_target = require_freshness_target(self.freshness_target, label=label)
        self._timeout = _require_timeout(self.timeout, label=label)
        self._retries = _require_retries(self.retries, label=label)
        self._rate_limit = _require_rate_limit(self.rate_limit, label=label)
        self._headers = _require_headers(self.headers, label=label)
        self._credential_host = _require_credential_host(self.credential_host, headers=self._headers, label=label)
        self._cache_ttl = _require_cache_ttl(self.response_cache_ttl, label=label)
        self._clock: Clock = clock
        # Ownership is tracked so `close()` releases only what this object
        # built. Closing a transport the caller supplied would take a pool away
        # from whoever else is holding it, which is the kind of tidy-up that is
        # only ever discovered in production.
        self._owns_transport = transport is None
        self._transport: Transport = (
            RequestsTransport(timeout=self._timeout, retries=self._retries) if transport is None else transport
        )
        self._limiter: RateLimiter = CacheRateLimiter() if limiter is None else limiter
        self._response_cache: ResponseCache = CacheResponseCache() if response_cache is None else response_cache
        # How many rows this base has inserted for the run in progress. Reset by
        # `sweep()` and read by `_require_counted`, which is what stops a
        # subclass reporting rows it never wrote. `collect()` writes through the
        # same door and increments it harmlessly; nothing reads it there,
        # because that path hands the base its rows rather than reporting them.
        self._swept_rows: int = 0

    def __enter__(self) -> Self:
        """Return this collector, so a caller can scope its connection pool.

        Returns:
            This collector, unchanged. Typed `Self` so a subclass's own methods
            stay visible to a caller that entered the block.

        """
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Release anything this collector built.

        Args:
            exc_type: The exception's type, if one is leaving the block.
            exc: The exception, if one is leaving the block.
            traceback: Its traceback, if one is leaving the block.

        """
        self.close()

    def close(self) -> None:
        """Release the connection pool, if this collector built one.

        A transport the caller supplied is left alone: it belongs to whoever
        made it, and may well outlive this collector.
        """
        if self._owns_transport and isinstance(self._transport, RequestsTransport):
            self._transport.close()

    @property
    def request_cost(self) -> int:
        """Return how many requests one collection may issue.

        Returns:
            `1 + retries`. This is what the rate limiter is charged per
            collection, so a declared allowance bounds *requests* rather than
            bounding collections while the retry policy multiplies underneath
            it (`core/rate_limit.py`).

        """
        return 1 + self._retries

    def freshness(self, *, package_id: int, now: datetime, status: str = UNOBSERVED_STATUS) -> FreshnessReport:
        """Report how old this collector's evidence for one package is.

        The read side of `CPM-AD-28`, and the reason the declaration above is
        required: a collector that declares a target is a collector whose
        evidence can be asked whether it has aged past one. The comparison itself
        is `core/freshness.py`'s -- eight collectors deciding "is this old" for
        themselves is the divergence that module exists to prevent -- and what
        this method adds is the two things only the collector knows: which table
        holds its observations (`CPM-AD-7`) and what target it declared.

        Args:
            package_id: The package being asked about, by the integer primary key
                `CPM-AD-3` fixes.
            now: The instant staleness is measured from, from the injected clock
                (`CPM-AD-26`). A parameter rather than `self._clock.now()`: a
                read surface composing one row asks about many collectors and
                every one of them must answer as of the same instant, or a
                package can read fresh under one collector and stale under
                another for no reason but the order they were called in.
            status: The `OutcomeState` value the evidence carries, passed through
                to the report untouched. Defaults to `unknown`, which is what a
                caller holding no observation has to hand over.

        Returns:
            The report, carrying the status unchanged, the staleness verdict
            beside it, and the observation instant so a surface can say how old.

        Raises:
            FreshnessError: When `now` is naive, or when this collector's
                evidence model declares no package reference.

        """
        return freshness_of(
            observed_at=latest_observation(self._evidence_model, package_id=package_id),
            target=self._freshness_target,
            now=now,
            status=status,
        )

    def inapplicability(self, *, package_id: int) -> str:
        """Say why the question this collector asks does not apply to one package, or say nothing.

        The hook `CPM-FR-6`'s `not_applicable` arrives through. It is asked
        **before** `source_for`, because a collector that cannot name a locator
        for a package its question does not apply to must not be asked for one
        -- and it is not abstract, because most collectors' questions apply to
        every package: a source repository is a thing any package may have. The
        default is therefore "applies", and the two collectors that predate this
        hook declare nothing new.

        What a subclass answers here is read from `identity` and never guessed
        (`CPM-FR-1`): "this package's type makes the question inapplicable" is a
        fact resolution recorded, and a collector inferring it from a name would
        be the guess `CPM-FR-1` forbids. What the base does with a reason is
        write the `not_applicable` sentinel row itself, with no call made and no
        allowance spent -- see the module docstring.

        Args:
            package_id: The package being collected, by the integer primary key
                `CPM-AD-3` fixes. Keyword only, as every other argument in this
                module is.

        Returns:
            The empty string when the question applies -- or when the collector
            cannot yet tell, in which case `source_for` is where the refusal
            belongs -- and otherwise the reason it does not, in words worth
            storing on the row and on the ledger.

        """
        return ""

    @abstractmethod
    def source_for(self, *, package_id: int) -> str:
        """Return the locator this collector reads for one package.

        Args:
            package_id: The package being collected, by the integer primary key
                `CPM-AD-3` fixes.

        Returns:
            The URL, path or other locator to hand the transport.

        """

    @abstractmethod
    def translate(self, payload: Payload, *, package_id: int, observed_at: datetime) -> Sequence[AppendOnlyModel]:
        """Turn a recorded payload into unsaved evidence rows.

        The method `CPM-AD-27` exists for: it takes data, returns data, and
        needs no network to test. It is called only for a payload the source
        answered successfully -- absence and failure are the base's to record --
        so it never has to decide whether it was handed a real answer.

        Args:
            payload: What the source said, recorded.
            package_id: The package the observation is about.
            observed_at: The instant to stamp every row with, from the injected
                clock. Passed in rather than read, because `observed_at` means
                "the moment of *this* observation" (`CPM-AD-7`); the base
                refuses a row stamped with anything else.

        Returns:
            At least one unsaved row for this collector's evidence model. The
            base inserts them; an implementation that saved them itself would
            write outside the per-package transaction. Returning nothing is a
            failure, not a success -- see the module docstring.

        """

    @abstractmethod
    def sentinel_evidence(
        self,
        *,
        state: OutcomeState,
        package_id: int,
        observed_at: datetime,
        detail: str,
    ) -> AppendOnlyModel:
        """Return one unsaved evidence row carrying a sentinel state.

        The base decides *which* sentinel and that there is always one; the
        collector decides what a row in its own table looks like. See the class
        docstring for why the split is where it is, and why the base checks the
        result rather than trusting it.

        Args:
            state: `OutcomeState.ERROR`, `OutcomeState.NOT_FOUND` or
                `OutcomeState.NOT_APPLICABLE` -- the first two for a call that
                failed or was answered "absent", the third for a question this
                collector said does not apply (`inapplicability`). Never
                `OutcomeState.OK` -- the base does not call this for a
                successful observation -- and never `OutcomeState.UNKNOWN`,
                which is what a package with *no* row reads as. Keyword only, as
                every other argument in this module is: eight subclasses
                implement this by hand, and a positional first argument is the
                one place their signatures could quietly disagree.
            package_id: The package the observation is about.
            observed_at: The instant to stamp the row with. The base refuses a
                row stamped with anything else.
            detail: What happened, in words worth storing beside the state.

        Returns:
            One unsaved row carrying `state`'s value verbatim (`CPM-AD-24`) in
            one of its fields.

        """

    def sentinel_evidence_rows(
        self,
        *,
        state: OutcomeState,
        package_id: int,
        observed_at: datetime,
        detail: str,
    ) -> Sequence[AppendOnlyModel]:
        """Return every sentinel row this run owes, for a path that produced no observation.

        The hook `CPM-CURRENCY-S04` added, and it is **not abstract** for the
        reason `inapplicability` is not: one sentinel row is the right answer for
        every collector that observes one surface per package, which is all four
        that predate it. The default below is exactly what those four did before
        the hook existed, so none of them changed and none of them declares
        anything new.

        **What it is for is a collector that owes several rows.** `CPM-FR-10`'s
        published-package collector observes one row per monitored
        `(channel, platform)` pair, and a run whose *first* channel answers "no
        such package" still owes a row for every other pair -- an answer the base
        could not let it give while a sentinel path wrote exactly one row and
        never reached `translate`. That is AC 1 ("each monitored channel produces
        its own observation") holding on the paths where the base, rather than the
        collector, decides what a row says.

        **A collector that overrides this may reach its transport, and that is
        the difference from `sentinel_evidence`.** The single-row hook shapes a
        row and does nothing else, deliberately: it is called from paths that are
        already recording a failure, where a raised exception would replace the
        reason being recorded. An override here inherits that constraint --
        whatever it does must not raise -- and in exchange it may answer for
        surfaces the base's one call never reached.

        Args:
            state: `OutcomeState.ERROR`, `OutcomeState.NOT_FOUND` or
                `OutcomeState.NOT_APPLICABLE`, decided by the base exactly as it
                decides them for `sentinel_evidence`. Keyword only, as every
                other argument in this module is.
            package_id: The package the observation is about.
            observed_at: The instant to stamp every row with. The base refuses a
                row stamped with anything else.
            detail: What happened, in words worth storing beside the state.

        Returns:
            At least one unsaved row, of which at least one carries `state`'s
            value verbatim (`CPM-AD-24`) -- the answer to the call the base
            actually made. Rows about surfaces that call never touched carry
            whatever *those* surfaces said, which is the point of answering for
            them at all. The base refuses an empty answer: every path that
            reaches this one has already promised an observation, and a hook that
            answered with nothing would turn `CPM-NFR-3`'s guarantee off on the
            paths where nobody is looking.

        """
        return [
            self.sentinel_evidence(
                state=state,
                package_id=package_id,
                observed_at=observed_at,
                detail=detail,
            ),
        ]

    def collect(  # noqa: PLR0911 - one return per terminal path; see below
        self,
        *,
        package_id: int,
        force: bool = False,
    ) -> CollectionResult:
        """Collect one package, applying every rule this base owns.

        The recorder is opened first and nothing wraps it -- see the module
        docstring for why that ordering is the whole of `CPM-EVIDENCE-S03`'s
        deferred constraint. Inside it: applicability, the window, the rate
        limit, the cache read, the call, and one `transaction.atomic()` around
        the evidence write.

        **Eight returns, one per terminal path, and that is what the `noqa`
        licenses.** They are the I/O matrix's rows: the window suppressed the
        run; the question does not apply to this package; the allowance was
        spent; the source did not answer; it answered `304` with nothing behind
        the request; it answered that the resource is absent; the parser found
        nothing; the observation was written. Each declares its own `detail`,
        which is the string the ledger row and the returned result both carry,
        so collapsing two of them into a shared return would mean a shared
        explanation -- and a reader asking "why did this collect nothing" would
        get a sentence written for a different reason. The alternative shape, a
        helper returning `Payload | CollectionResult` for the caller to
        type-test, moves the ordering this module's docstring is written about
        out of the method it is written about.

        Args:
            package_id: The package to collect, by the integer primary key
                `CPM-AD-3` fixes.
            force: Bypass the observation window. `CPM-UJ-1`'s manually
                triggered recollection "always bypasses the window and always
                writes", so this is a parameter rather than a separate entry
                point -- one path, one set of rules, one difference.

        Returns:
            What the run did, mirroring the ledger row it wrote. Every path that
            returns has written one; the refusals below are the paths that do
            not return at all.

        Raises:
            RunLedgerError: When `package_id` names no package. The recorder
                checks the key before it writes the opening row
                (`CPM-EVIDENCE-S09`), so this one leaves *nothing* behind -- no
                ledger row, no log line, no evidence -- and is the one exit from
                this method that does not mirror a ledger row, because there is
                no run to mirror. A collector reaches it by being handed a key
                for a package that was never resolved, which is a caller defect
                rather than a collection outcome.
            CollectorConfigurationError: When a row this collector produced does
                not carry the instant it was handed, when a sentinel row does
                not carry the state it was asked for, or when `inapplicability`
                answered something that is neither a reason nor the empty
                string. All are defects in the subclass; all are caught before
                a row reaches the table.
            CollectionWriteError: When the evidence write itself failed on a
                path that was already recording a failure. It carries the
                original reason, so the ledger never records only the epitaph.
            Exception: Whatever `translate` raised, re-raised unchanged *after*
                an evidence row carrying `error` has been written and with the
                ledger row finalized to `failed` by the recorder. Caught this
                widely and deliberately: a malformed payload can break a parser
                in any way at all, and the guarantee being defended -- never no
                row -- does not depend on which way.

        """
        with collection_run(collector=self._name, clock=self._clock, package_id=package_id) as run:
            observed_at = self._clock.now()
            # Applicability first, and the locator only when the question
            # applies: a collector whose question does not apply to this package
            # has no locator to name and must not be asked for one. Otherwise the
            # locator is asked for once, before the window is consulted, so that
            # every log line and every message below names the same one -- and
            # so that a `source_for` which cannot answer fails the run rather
            # than failing only on the paths that happen to reach the transport.
            reason = _require_reason(self.inapplicability(package_id=package_id), label=type(self).__name__)
            source = "" if reason else self.source_for(package_id=package_id)

            if not force and self._inside_window(package_id=package_id, now=observed_at):
                detail = (
                    f"{self._name} observed package {package_id} within the last {self._window}; "
                    f"the observation window suppressed this run (CPM-AD-7)."
                )
                logger.info(
                    COLLECTION_SKIPPED_EVENT,
                    collector=self._name,
                    package_id=package_id,
                    source=source,
                    detail=detail,
                )
                run.skipped(detail=detail)
                return CollectionResult(state=RunState.SKIPPED, evidence_rows=0, detail=detail)

            if reason:
                return self._not_applicable(reason=reason, package_id=package_id, observed_at=observed_at, run=run)

            refusal = self._call_refusal(source=source, package_id=package_id, now=observed_at)
            if refusal:
                return self._failed(run_detail=refusal, package_id=package_id, observed_at=observed_at, run=run)

            # Read after the allowance is granted rather than before it: a
            # collection that is not going to happen has no use for an entry,
            # and the cache is a shared resource like the counter beside it.
            remembered = self._remembered(source)
            sent = request_headers(declared=self.headers_for(source), entry=remembered)
            try:
                payload = self._transport.fetch(source, headers=sent)
            except TransportError as failure:
                detail = self._failure_detail(failure, sent=sent, now=observed_at)
                logger.warning(
                    COLLECTION_FAILED_EVENT,
                    collector=self._name,
                    package_id=package_id,
                    source=source,
                    detail=detail,
                )
                return self._failed(run_detail=detail, package_id=package_id, observed_at=observed_at, run=run)

            if payload.not_modified:
                if remembered is None:
                    detail = (
                        f"{source} answered that nothing has changed, but nothing asked it what changed: this "
                        f"run sent no validator and holds no cached body, so there is no observation to "
                        f"record. Inventing an empty one is the clean-looking result CPM-NFR-3 forbids."
                    )
                    logger.warning(
                        COLLECTION_FAILED_EVENT,
                        collector=self._name,
                        package_id=package_id,
                        source=source,
                        detail=detail,
                    )
                    return self._failed(run_detail=detail, package_id=package_id, observed_at=observed_at, run=run)
                logger.info(
                    COLLECTION_NOT_MODIFIED_EVENT,
                    collector=self._name,
                    package_id=package_id,
                    source=source,
                    detail=f"{source} confirmed the cached response still holds; the body was not transferred.",
                )
                payload = _replayed(payload, remembered=remembered, source=source)

            if not payload.found:
                detail = f"{source} reports that the resource does not exist"
                rows = self._write_evidence(
                    self._sentinel_rows(
                        OutcomeState.NOT_FOUND,
                        package_id=package_id,
                        observed_at=observed_at,
                        detail=detail,
                    ),
                    observed_at=observed_at,
                )
                # After the write, as every cache mutation on every path is. The
                # locator answered "gone", so what is remembered for it is gone
                # too -- a body kept past that answer would be replayed the day
                # the locator came back, and what comes back need not be what
                # left. Doing it *before* the write would make the ordering rule
                # this module states hold on three paths out of four, which is
                # not a rule anybody could then rely on.
                self._forget(source)
                # `succeeded`, not `failed`: the source answered, and the answer
                # was "no such thing". `CPM-AD-5` keeps the two distinguishable
                # precisely so a reader is never asked to infer which happened.
                # The detail is declared so the ledger row says what the returned
                # result says, on this path as on every other.
                run.succeeded(detail=detail)
                return CollectionResult(state=RunState.SUCCEEDED, evidence_rows=rows, detail=detail)

            # Caught this widely and re-raised unchanged. Nothing is swallowed --
            # the `raise` below is unconditional and re-raises the same object,
            # which inherited `CG-3` requires -- and the row is written first so
            # that the "never no row" guarantee survives a parser breaking in a
            # way nobody anticipated. The recorder finalizes the ledger row to
            # `failed` with the exception's type and message on the way out, so
            # there is no `run.failed()` here to contradict it.
            try:
                evidence = self.translate(payload, package_id=package_id, observed_at=observed_at)
            except Exception as broken:
                detail = f"{type(broken).__name__}: {broken}"
                logger.warning(
                    COLLECTION_FAILED_EVENT,
                    collector=self._name,
                    package_id=package_id,
                    source=source,
                    detail=detail,
                )
                self._write_evidence(
                    self._sentinel_rows(
                        OutcomeState.ERROR,
                        package_id=package_id,
                        observed_at=observed_at,
                        detail=detail,
                    ),
                    observed_at=observed_at,
                )
                raise

            if not evidence:
                detail = (
                    f"{self._name} translated {source} into no evidence rows. A parser that finds nothing in a "
                    f"body the source served no longer matches its source, and recording that as a clean "
                    f"success is the ambiguity the outcome vocabulary exists to remove (CPM-NFR-3)."
                )
                logger.warning(
                    COLLECTION_FAILED_EVENT,
                    collector=self._name,
                    package_id=package_id,
                    source=source,
                    detail=detail,
                )
                return self._failed(run_detail=detail, package_id=package_id, observed_at=observed_at, run=run)

            written = self._write_evidence(evidence, observed_at=observed_at)
            # Last, and only now. Everything above can still fail; a body
            # remembered before its evidence was written would be replayed
            # forever by a validator this run had already sent.
            self._remember(source, payload, package_id=package_id, remembered=remembered)
            return CollectionResult(state=RunState.SUCCEEDED, evidence_rows=written, detail="")

    def sweep_source(self) -> str:
        """Return the one locator a run-scoped read asks for.

        `source_for` is the per-package question and this is the run-scoped one.
        Not abstract, and deliberately: eight collectors read one locator per
        package and have no run-scoped document at all, so an abstract method
        here would make every one of them implement a path it does not have.
        The default is therefore a refusal that names what is missing.

        Returns:
            The URL, path or other locator of the document this run reads.

        Raises:
            CollectorConfigurationError: When the subclass declares no
                run-scoped source. Raised where `sweep()` first asks, which is
                before the window, the allowance and the transport -- so a
                collector asked to sweep when it cannot never reaches a source.

        """
        message = (
            f"{type(self).__name__} was asked to sweep and declares no sweep_source. A run-scoped read is one "
            f"document naming many packages (CPM-AD-25); a collector that reads one locator per package "
            f"collects with collect(package_id=...) instead."
        )
        raise CollectorConfigurationError(message)

    def persist_sweep(self, payload: Payload, *, observed_at: datetime) -> SweepOutcome:
        """Turn one run-scoped document into rows, one transaction per package.

        The sweep's counterpart to `translate`, and it is deliberately not the
        same shape. `translate` returns unsaved rows because the base knows which
        package they are about and can write them all in one transaction.
        A sweep's document names many packages, some of which have no row yet, so
        the atomic unit is *inside* this method: the subclass opens one
        `transaction.atomic()` per package, creates whatever that package needs
        and inserts its evidence, and a later package's failure never rolls back
        an earlier one's (`CPM-AD-23`, `CPM-FR-15`). A base that collected rows
        and wrote them itself would hold one transaction across the whole
        document, which is the thing `CPM-AD-23` forbids.

        **Every row still goes through `_write_evidence`, and that is not a
        convenience.** It is the one door, and passing through it is what applies
        the declared-model check, the `observed_at` check and the tally
        `_require_counted` reads on the way out. An implementation that called
        `bulk_create` itself would write rows the base never saw, and the run
        would be refused for a count that does not add up rather than quietly
        succeed -- which is the point.

        Not abstract, for the reason `sweep_source` is not.

        Args:
            payload: What the source said, recorded. The base has already
                established that the source answered, that it answered with a
                body, and that the document exists; what it means is this
                method's to decide.
            observed_at: The instant every row this run writes must carry, from
                the injected clock (`CPM-AD-26`). One observation has one moment
                (`CPM-AD-7`), and that includes the rows recording that a package
                the document no longer names was not seen.

        Returns:
            How many rows were written, split into the ones the source's own
            records produced and the ones their *absence* implied, and which
            packages could not be written at all. See `SweepOutcome` for why the
            two counts must not be added together before the base sees them.

        Raises:
            CollectorConfigurationError: When the subclass declares no
                run-scoped persistence.

        """
        message = (
            f"{type(self).__name__} was asked to sweep and declares no persist_sweep. The base owns the run "
            f"ledger, the window, the allowance and the call; what a record means and which package it names "
            f"is the collector's (CPM-AD-27)."
        )
        raise CollectorConfigurationError(message)

    def sweep(self, *, force: bool = False) -> CollectionResult:  # noqa: PLR0911 - one return per terminal path; see below
        """Collect every package one run-scoped document names.

        The run-scoped entry point. The recorder is opened first and nothing
        wraps it, exactly as in `collect`, and it is opened with **no package
        reference** -- `core/ledger.py` writes NULL for a run that was not scoped
        to one package, which is what a sweep is. Inside it: the window, the rate
        limit, the call, and then the subclass's per-package persistence.

        **Seven returns, one per terminal path, and that is what the `noqa`
        licenses**, on the same terms `collect`'s seven are: the window
        suppressed the run; the allowance was spent; the source did not answer;
        it answered `304` to a question nothing asked; it answered that the
        document is absent; the document produced no rows; and the document was
        ingested, wholly or in part. Each declares its own `detail`, which is the
        string the ledger row and the returned result both carry.

        Args:
            force: Bypass the observation window, on the same terms `collect`
                offers it -- `CPM-UJ-1`'s manually triggered recollection always
                bypasses the window.

        Returns:
            What the run did, mirroring the ledger row it wrote. `partial` when
            some packages were written and some were not, which is
            `CPM-FR-15`'s partial success and is only reachable because the
            transaction is per package.

        Raises:
            CollectorConfigurationError: When the subclass declares no
                `sweep_source` or no `persist_sweep`, or when what it reports
                having written is not what went through this base -- see
                `_require_counted`.
            Exception: Whatever `persist_sweep` raised, re-raised unchanged with
                the ledger row finalized to `failed` by the recorder. Not caught
                here and not turned into a sentinel row: a sweep that broke
                before it decided anything about a package has no package to
                write a row about, and an invented one would be the clean-looking
                result `CPM-NFR-3` forbids wearing a different hat.

        """
        with collection_run(collector=self._name, clock=self._clock) as run:
            observed_at = self._clock.now()
            # Reset before the subclass can write anything, so the tally
            # `_require_counted` reads below describes *this* run and no other.
            self._swept_rows = 0
            # Asked for once, before anything else, for the reason `collect`
            # asks for `source_for` once: every message below names the same
            # locator, and a collector that cannot say what it reads fails the
            # run rather than only the paths that reach the transport.
            source = self.sweep_source()

            if not force and self._inside_window(package_id=None, now=observed_at):
                detail = (
                    f"{self._name} swept {source} within the last {self._window}; "
                    f"the observation window suppressed this run (CPM-AD-7)."
                )
                logger.info(
                    COLLECTION_SKIPPED_EVENT,
                    collector=self._name,
                    package_id=None,
                    source=source,
                    detail=detail,
                )
                run.skipped(detail=detail)
                return CollectionResult(state=RunState.SKIPPED, evidence_rows=0, detail=detail)

            refusal = self._call_refusal(source=source, package_id=None, now=observed_at)
            if refusal:
                return self._failed_sweep(detail=refusal, run=run)

            # `entry=None`: the sweep sends no conditional request. See the
            # module docstring for why a run-scoped read remembers nothing.
            sent = request_headers(declared=self.headers_for(source), entry=None)
            try:
                payload = self._transport.fetch(source, headers=sent)
            except TransportError as failure:
                detail = self._failure_detail(failure, sent=sent, now=observed_at)
                logger.warning(
                    COLLECTION_FAILED_EVENT,
                    collector=self._name,
                    package_id=None,
                    source=source,
                    detail=detail,
                )
                return self._failed_sweep(detail=detail, run=run)

            if payload.not_modified:
                # A run-scoped read is unconditional by construction -- this
                # method passes `entry=None` a few lines up and remembers
                # nothing -- so a `304` here is not a cache hit this base failed
                # to arrange. It is the source answering a question nobody asked,
                # and there is no body behind it. Left to fall through it would
                # surface as whatever the subclass makes of an empty document,
                # which is a parse error describing the wrong problem.
                detail = (
                    f"{source} answered that nothing has changed, but a run-scoped read sends no validator: "
                    f"this sweep asked unconditionally and holds no cached body, so there is no document to "
                    f"read and no observation to record."
                )
                logger.warning(
                    COLLECTION_FAILED_EVENT,
                    collector=self._name,
                    package_id=None,
                    source=source,
                    detail=detail,
                )
                return self._failed_sweep(detail=detail, run=run)

            if not payload.found:
                # `failed`, where the per-package path records `succeeded` with a
                # `not_found` row. The asymmetry is the point: "this package does
                # not exist" is an observation about a package, while "the
                # inventory document does not exist" is the run being unable to
                # observe anything at all, and recording it as a success would
                # make every package in the inventory silently unobserved.
                detail = f"{source} reports that the run-scoped source does not exist"
                logger.warning(
                    COLLECTION_FAILED_EVENT,
                    collector=self._name,
                    package_id=None,
                    source=source,
                    detail=detail,
                )
                return self._failed_sweep(detail=detail, run=run)

            outcome = self.persist_sweep(payload, observed_at=observed_at)
            self._require_counted(outcome)

            # `observed_rows` and never `evidence_rows`. A sweep's other rows are
            # written *because* the source stopped naming something, so counting
            # them here would let a source that served nothing at all satisfy
            # "this run produced evidence" with a row per package it failed to
            # mention -- permanently, in a log nothing may correct. See
            # `SweepOutcome`.
            if not outcome.observed_rows:
                detail = (
                    f"{self._name} swept {source} and wrote no evidence row from any record its source "
                    f"supplied{_reported(outcome.failures)}. A run that observed nothing is not a run that "
                    f"found nothing wrong, and recording it as a clean success is the ambiguity the outcome "
                    f"vocabulary exists to remove (CPM-NFR-3)."
                )
                logger.warning(
                    COLLECTION_FAILED_EVENT,
                    collector=self._name,
                    package_id=None,
                    source=source,
                    detail=detail,
                )
                return self._failed_sweep(detail=detail, run=run)

            if outcome.failures:
                detail = (
                    f"{self._name} swept {source} and wrote {outcome.evidence_rows} evidence row(s)"
                    f"{_reported(outcome.failures)}. The packages that were written are committed "
                    f"(CPM-AD-23)."
                )
                logger.warning(
                    COLLECTION_PARTIAL_EVENT,
                    collector=self._name,
                    package_id=None,
                    source=source,
                    detail=detail,
                )
                run.partial(detail=detail)
                return CollectionResult(
                    state=RunState.PARTIAL,
                    evidence_rows=outcome.evidence_rows,
                    detail=detail,
                )

            return CollectionResult(state=RunState.SUCCEEDED, evidence_rows=outcome.evidence_rows, detail="")

    def _require_counted(self, outcome: SweepOutcome) -> None:
        """Refuse a sweep whose reported rows are not the rows that were written.

        **The sweep's counterpart to `_require_stamped`, and it exists for the
        same class of reason.** The per-package path hands the base unsaved rows
        and the base inserts them, so every invariant -- the declared model, the
        run's instant, the append-only manager -- is checked on the way in. A
        sweep writes its own rows, one transaction per package (`CPM-AD-23`), and
        then *reports* what it did. A report is not a guarantee: a subclass that
        returned `SweepOutcome(observed_rows=99)` over no writes at all would
        finalize a successful run, and one that wrote through its own
        `bulk_create` would go around the stamping and declared-model checks
        entirely. Neither would fail any behavioural case, and both type-check.

        So the base counts for itself. `_write_evidence` is the one door, it
        tallies every row it inserts, and this compares the tally against what
        the subclass said. A count that matches is a count the base watched being
        written -- which is what makes the two checks in `_write_evidence` cover
        the sweep path as well.

        Args:
            outcome: What the subclass reported.

        Raises:
            CollectorConfigurationError: When the reported total is not the
                number of rows this base inserted during the run. The message
                names both numbers, because "the count is wrong" sends a reader
                looking and naming them says which way.

        """
        if outcome.evidence_rows != self._swept_rows:
            message = (
                f"{type(self).__name__}.persist_sweep reported {outcome.evidence_rows} evidence row(s) "
                f"({outcome.observed_rows} observed, {outcome.derived_rows} derived) and this base wrote "
                f"{self._swept_rows}. Every sweep row goes through the base, which is what applies the "
                f"declared-model and observed_at checks the per-package path gets (CPM-AD-7, CPM-AD-26); a "
                f"reported count is not a written row."
            )
            raise CollectorConfigurationError(message)

    def headers_for(self, locator: str) -> dict[str, str]:
        """Return the declared headers one call to this locator may carry: the credential only for its own host.

        The one place a credential is kept to its host for the primary fetch
        (`CPM-OPERATE-S05`), and the rule a collector's bounded second calls
        apply to themselves: `Authorization` travels only to `credential_host`
        over `https`, and every other locator is sent the declaration without
        it. Public so a collector making a second call from `translate` can
        ask the base rather than restating the rule.

        Args:
            locator: The URL about to be fetched.

        Returns:
            A fresh dictionary of the declared headers, with `Authorization`
            stripped unless the locator is the credential's own host over TLS.

        """
        return credentialed_headers(locator, declared=self._headers, credential_host=self._credential_host)

    def _failure_detail(self, failure: TransportError, *, sent: Mapping[str, str], now: datetime) -> str:
        """Word one failed fetch, and remember a refused credential for the rest of the window.

        Args:
            failure: What the transport raised.
            sent: The headers the failed request carried, which is what decides
                whether a `401` is a refused credential.
            now: The instant of the run, which decides the window the refusal
                is remembered for.

        Returns:
            The detail the ledger, the evidence row and the log all carry.

        """
        credentialed = carries_credential(sent)
        detail = failure_detail(failure, credentialed=credentialed)
        if credentialed and failure.status_code == REFUSED_CREDENTIAL_STATUS:
            # Remembered under the collector's name for the current allowance
            # window, so a sweep meets one refused call per window rather than
            # one per package; the next window tries the credential once more.
            self._limiter.remember_refusal(collector=self._name, limit=self._rate_limit, now=now, detail=detail)
        return detail

    def _call_refusal(self, *, source: str, package_id: int | None, now: datetime) -> str:
        """Return why no call is issued, logging the reason, or `""` when the call may go.

        Two reasons, asked in this order: the declared credential was refused
        earlier in this allowance window (`CPM-OPERATE-S05`), which costs no
        allowance to know; and the allowance itself is spent (`CPM-AD-20`),
        which is asked of the limiter and charged whether or not it fits.

        Args:
            source: The locator the collection would read, for the messages.
            package_id: The package, or `None` for a run-scoped sweep.
            now: The instant the window is decided from.

        Returns:
            The detail for a `failed` run, or the empty string.

        """
        refused_earlier = self._credential_refused_earlier(source=source, now=now)
        if refused_earlier:
            logger.warning(
                COLLECTION_CREDENTIAL_REFUSED_EVENT,
                collector=self._name,
                package_id=package_id,
                source=source,
                detail=refused_earlier,
            )
            return refused_earlier
        if not self._limiter.acquire(collector=self._name, limit=self._rate_limit, now=now, cost=self.request_cost):
            detail = (
                f"{self._name} has spent its allowance of {self._rate_limit.calls} requests per "
                f"{self._rate_limit.per}; the call was refused rather than issued unlimited (CPM-AD-20)."
            )
            logger.warning(
                COLLECTION_REFUSED_EVENT,
                collector=self._name,
                package_id=package_id,
                source=source,
                detail=detail,
                cost=self.request_cost,
            )
            return detail
        return ""

    def _credential_refused_earlier(self, *, source: str, now: datetime) -> str:
        """Return why no call is issued, when the credential was refused earlier this window.

        Asked before the allowance is charged and before any fetch, and only
        for a collector that would send a credential -- a collector sending
        none has nothing to have been refused, and is spared the cache read.

        Args:
            source: The locator the collection would have read, for the message.
            now: The instant the window is decided from.

        Returns:
            The detail for a fail-fast `failed` run, or `""` when the call may
            be issued.

        """
        if not carries_credential(self._headers):
            return ""
        refused = self._limiter.refusal(collector=self._name, limit=self._rate_limit, now=now)
        if refused is None:
            return ""
        return (
            f"the declared credential was refused earlier in this allowance window, so no call to {source} was "
            f"issued; it is tried again when the window turns at {refused.until.isoformat()} (CPM-OPERATE-S05). "
            f"The refusal recorded: {refused.detail}"
        )

    def _failed_sweep(self, *, detail: str, run: RunHandle) -> CollectionResult:
        """Declare a run-scoped run failed, with no evidence row to write.

        The sweep's counterpart to `_failed`, and the difference between them is
        the whole of why it exists: `_failed` writes a sentinel row about the one
        package the run was scoped to, and a sweep has no such package.

        Args:
            detail: Why the run failed, in the words the ledger row and the
                returned result both carry.
            run: The recorder's handle.

        Returns:
            The failed result, carrying no rows and the same detail the ledger
            row now holds.

        """
        run.failed(detail=detail)
        return CollectionResult(state=RunState.FAILED, evidence_rows=0, detail=detail)

    def _inside_window(self, *, package_id: int | None, now: datetime) -> bool:
        """Report whether the observation window suppresses this run.

        A zero window short-circuits rather than querying, and the difference is
        not cosmetic: `finished_at__gte=since` is inclusive, so with
        `NO_WINDOW` a run that finished at exactly this instant would suppress.
        That is unreachable under `SystemClock` and exactly reproducible under
        the `FixedClock` every case injects -- which is the worst combination,
        because it makes the bug a property of the tests rather than of the
        product. `NO_WINDOW` means "observe on every run", so it is answered
        without asking.

        Args:
            package_id: The package being collected, or `None` for a run-scoped
                sweep -- see `window_query` for why the two never suppress each
                other.
            now: The instant the window is measured back from.

        Returns:
            True when a suppressing run finished inside the window.

        """
        if self._window <= NO_WINDOW:
            return False
        return has_recent_success(collector=self._name, package_id=package_id, since=now - self._window)

    def _remembered(self, source: str) -> CachedResponse | None:
        """Return what was cached for this locator, if this collector caches.

        `NO_CACHE` short-circuits rather than reading, which is the same shape
        `_inside_window` uses for a zero window and is what makes "caching
        disabled" mean *no cache read, no cache write and no conditional
        header* rather than "a read that always misses".

        Args:
            source: The locator about to be read.

        Returns:
            The remembered response, or `None` for a miss and for a collector
            that declared `NO_CACHE`.

        """
        if self._cache_ttl <= NO_CACHE:
            return None
        return self._response_cache.read(collector=self._name, source=source)

    def _remember(
        self,
        source: str,
        payload: Payload,
        *,
        package_id: int,
        remembered: CachedResponse | None,
    ) -> None:
        """Record this answer, or refresh the entry a `304` confirmed.

        Called after the evidence write and nowhere else -- see the module
        docstring for why a body cached before its parse becomes permanent.

        A `304` writes its entry again rather than nothing: the source has
        confirmed it, so the lifetime starts from the confirmation. The *body*
        is the remembered one -- there was none to transfer -- but the
        validators are taken from the `304` where it supplied them, because a
        source is entitled to hand back a new `ETag` or `Last-Modified` on a
        revalidation and the next conditional request must carry what the source
        last said rather than what it said before that. Where the `304` supplies
        neither, the remembered validators are kept, which is the ordinary case.

        A `200` carrying no validator writes nothing: an entry with no validator
        can never be revalidated and `core/response_cache.py` refuses to build
        one, so there is nothing to store that a later run could use. That is a
        property of the source rather than a defect, so it does not fail the
        run -- but it is logged, because it is the reason a collector that
        declared a cache lifetime never sees a `304`, and an operator asking why
        would otherwise be looking at a cache that is configured, working and
        permanently empty.

        Args:
            source: The locator that was asked, which is what the entry is
                keyed on. Taken from the run rather than from `payload.source`:
                the payload's locator is data a transport supplied, and a key
                built from it would be a key a source could choose.
            payload: What the source said, or the replayed answer a `304`
                produced.
            package_id: The package the observation is about. Carried only so
                the log line below has the key set every event this module emits
                has (`EVENT_KEYS`) -- a log query written against those keys must
                not be wrong for one of the five.
            remembered: The entry this run was holding, whose body a `304`
                keeps.

        """
        if self._cache_ttl <= NO_CACHE:
            return
        try:
            entry = _entry_to_remember(payload, remembered=remembered)
        except ResponseCacheError as unrevalidatable:
            # Never swallowed: reported with the reason and the locator, and the
            # run continues. A source this collector must keep re-reading in
            # full is what a cache with nothing to revalidate against would have
            # produced anyway.
            logger.info(
                COLLECTION_NOT_REMEMBERED_EVENT,
                collector=self._name,
                package_id=package_id,
                source=source,
                detail=f"{type(unrevalidatable).__name__}: {unrevalidatable}",
            )
            return
        self._response_cache.write(
            collector=self._name,
            source=source,
            response=entry,
            ttl_seconds=int(self._cache_ttl.total_seconds()),
        )

    def _forget(self, source: str) -> None:
        """Drop whatever is remembered for a locator the source says is gone.

        Args:
            source: The locator to forget.

        """
        if self._cache_ttl <= NO_CACHE:
            return
        self._response_cache.forget(collector=self._name, source=source)

    def _not_applicable(
        self,
        *,
        reason: str,
        package_id: int,
        observed_at: datetime,
        run: RunHandle,
    ) -> CollectionResult:
        """Write the `not_applicable` row for a question this collector said does not apply.

        The counterpart to the `not_found` branch of `collect`, reached before
        the allowance, the cache and the transport rather than after them: the
        source is never asked, so nothing is charged, nothing is read and nothing
        is remembered or forgotten. The row goes through `_write_evidence` like
        every other, so it is checked against the declared model and the run's
        instant on the way in, and `_sentinel_rows` checks it carries the state.

        `succeeded`, not `failed`: the collector answered, and the answer was
        "this question is not about this package" (`CPM-FR-6`). The reason is
        declared to the ledger so the ledger row says what the returned result
        says, on this path as on every other -- and it is the same string the
        row's own `detail` carries, because there is only one reason.

        Args:
            reason: Why the question does not apply, as `inapplicability` said
                it.
            package_id: The package the observation is about.
            observed_at: The instant to stamp the row with.
            run: The recorder's handle.

        Returns:
            The succeeded result, carrying the row count and the reason.

        """
        logger.info(
            COLLECTION_NOT_APPLICABLE_EVENT,
            collector=self._name,
            package_id=package_id,
            source="",
            detail=reason,
        )
        rows = self._write_evidence(
            self._sentinel_rows(
                OutcomeState.NOT_APPLICABLE,
                package_id=package_id,
                observed_at=observed_at,
                detail=reason,
            ),
            observed_at=observed_at,
        )
        run.succeeded(detail=reason)
        return CollectionResult(state=RunState.SUCCEEDED, evidence_rows=rows, detail=reason)

    def _failed(
        self,
        *,
        run_detail: str,
        package_id: int,
        observed_at: datetime,
        run: RunHandle,
    ) -> CollectionResult:
        """Declare the run failed, write the `error` row, and report both.

        The declaration comes first so the ledger's reason is set before
        anything else can go wrong, and the write is wrapped so that a database
        failure carries the reason with it rather than replacing it.

        Args:
            run_detail: Why the run failed.
            package_id: The package the observation is about.
            observed_at: The instant to stamp the row with.
            run: The recorder's handle, so the ledger row's reason is declared
                before anything else can go wrong.

        Returns:
            The failed result, carrying the row count and the same detail the
            ledger row now holds.

        Raises:
            CollectionWriteError: When the sentinel row could not be written.

        """
        run.failed(detail=run_detail)
        rows = self._write_sentinel(
            OutcomeState.ERROR,
            package_id=package_id,
            observed_at=observed_at,
            detail=run_detail,
        )
        return CollectionResult(state=RunState.FAILED, evidence_rows=rows, detail=run_detail)

    def _write_sentinel(
        self,
        state: OutcomeState,
        *,
        package_id: int,
        observed_at: datetime,
        detail: str,
    ) -> int:
        """Write this run's sentinel rows, preserving the reason if the write itself fails.

        Args:
            state: The sentinel the base decided on.
            package_id: The package the observation is about.
            observed_at: The instant to stamp the row with.
            detail: The reason the run is already failing.

        Returns:
            How many rows were inserted -- which is however many the collector's
            `sentinel_evidence_rows` owed, not always one.

        Raises:
            CollectionWriteError: When the write raised. The message carries the
                original reason first, so the ledger row -- which the recorder
                fills from the exception on its way out -- never records only
                that the epitaph could not be written.

        """
        try:
            return self._write_evidence(
                self._sentinel_rows(state, package_id=package_id, observed_at=observed_at, detail=detail),
                observed_at=observed_at,
            )
        except DatabaseError as write_failure:
            message = (
                f"{detail} -- and the {state.value} evidence row recording it could not be written: "
                f"{type(write_failure).__name__}: {write_failure}"
            )
            raise CollectionWriteError(message, detail=detail) from write_failure

    def _sentinel_rows(
        self,
        state: OutcomeState,
        *,
        package_id: int,
        observed_at: datetime,
        detail: str,
    ) -> list[AppendOnlyModel]:
        """Ask the collector for this run's sentinel rows, and check every one carries the state.

        The one place a sentinel row set enters this base, so the checks below
        hold on all three sentinel paths -- the `not_found` branch, `_failed` and
        `_not_applicable` -- rather than on whichever of them a later edit
        remembers.

        Args:
            state: The sentinel the base decided on.
            package_id: The package the observation is about.
            observed_at: The instant to stamp the rows with.
            detail: What happened.

        Returns:
            The rows the collector built, at least one.

        Raises:
            CollectorConfigurationError: When the collector returned something
                that is not a sequence of rows, returned none, or returned rows
                of which **not one** carries the state's value.

                The emptiness check is the one worth arguing for: every path that
                reaches here has already promised a row (`CPM-NFR-3`, "never a
                clean result and never no row"), so a hook that answered with
                nothing would turn the guarantee off silently -- and it would do
                it on a path that is *already* recording a failure, where nobody
                is looking.

                The verbatim check is `CPM-AD-24`'s: a subclass that ignored the
                argument and wrote a determinate verdict would type-check
                perfectly and would defeat "never a clean result" outright, so
                the base checks rather than trusts. **It is "at least one row"
                rather than "every row", and the difference is what the plural
                hook is for.** The state the base decided is about the *one call
                the base made*; a collector answering for surfaces that call
                never touched can only report what each of them said, and a row
                saying "this other channel publishes version 2.1.3" is an
                observation rather than a sentinel that forgot which sentinel it
                is. What the check still guarantees is that the base's own answer
                is on the record: a collector that returned nothing but
                determinate rows is refused exactly as it was before.

        """
        produced = self.sentinel_evidence_rows(
            state=state,
            package_id=package_id,
            observed_at=observed_at,
            detail=detail,
        )
        # A `str` is a `Sequence` and a model instance is not one at all, so both
        # of the plausible ways to answer this hook wrongly are refused here
        # rather than at `bulk_create`, where the message would be about a field.
        if isinstance(produced, (str, bytes)) or not isinstance(produced, Sequence):
            message = (
                f"{type(self).__name__}.sentinel_evidence_rows was asked for {state.value!r} and returned "
                f"{type(produced).__name__} rather than a sequence of rows. The hook answers with the rows this "
                f"run owes; one row is a sequence of one."
            )
            raise CollectorConfigurationError(message)
        rows = list(produced)
        if not rows:
            message = (
                f"{type(self).__name__}.sentinel_evidence_rows was asked for {state.value!r} and returned no "
                f"rows. Every path that reaches it is a path this base has already promised an observation for: "
                f"never a clean result, and never no row (CPM-NFR-3)."
            )
            raise CollectorConfigurationError(message)
        carried = [str(self._state_of(row)) for row in rows]
        if state.value not in carried:
            message = (
                f"{type(self).__name__}.sentinel_evidence_rows was asked for {state.value!r} and returned "
                f"{len(rows)} row(s) carrying {sorted(set(carried))}, none of them that state. A sentinel that "
                f"does not say which sentinel it is cannot be told from a clean result (CPM-AD-5, CPM-AD-24)."
            )
            raise CollectorConfigurationError(message)
        # A determinate row may accompany a `not_found` -- a collector answering
        # for several surfaces reports what each of them said, and one of them
        # having the thing is an observation. It may **not** accompany an `error`
        # or a `not_applicable`: those two are written by `_failed` and
        # `_not_applicable`, which have already declared the run's verdict, so a
        # determinate row there is permanent evidence that the source answered
        # cleanly underneath a ledger row saying the run did not -- "never a
        # clean result" (`CPM-NFR-3`) defeated from inside the contract.
        if state is not OutcomeState.NOT_FOUND and OutcomeState.OK.value in carried:
            message = (
                f"{type(self).__name__}.sentinel_evidence_rows was asked for {state.value!r} and returned a row "
                f"carrying {OutcomeState.OK.value!r}. A run recording {state.value!r} has already declared its "
                f"verdict, and a determinate row written under it is a clean result nothing may correct "
                f"(CPM-NFR-3, CPM-AD-5)."
            )
            raise CollectorConfigurationError(message)
        return rows

    def _state_of(self, row: AppendOnlyModel) -> object:
        """Return the value a row's own state column carries.

        Read from the column named `STATE_FIELD` rather than by scanning every
        field, and the difference is a real one: `CPM-AD-24` makes the value a
        short lowercase word, so a `detail`, a `source` or a package key equal to
        `"error"` would satisfy a scan -- and a hook answering with several rows
        makes an incidental match that many times likelier. Every evidence model
        in this product names the column `state` (`CPM-AD-5`), and
        `tests/unit/django_apps/test_outcome_field_audit.py` is what keeps it
        that way.

        Args:
            row: The unsaved row.

        Returns:
            The column's value.

        Raises:
            CollectorConfigurationError: When the row declares no such column. A
                sentinel row that cannot say which sentinel it is has nothing the
                base could check.

        """
        if not any(
            field.attname == STATE_FIELD
            for field in row._meta.concrete_fields  # noqa: SLF001 - `_meta` is Django's own public-by-convention API
        ):
            message = (
                f"{type(self).__name__} produced a {type(row).__name__} row, which declares no {STATE_FIELD!r} "
                f"column. Every evidence model carries the outcome verbatim in one (CPM-AD-5, CPM-AD-24), and a "
                f"row without one cannot be told from a clean result."
            )
            raise CollectorConfigurationError(message)
        return getattr(row, STATE_FIELD)

    def _write_evidence(self, evidence: Sequence[AppendOnlyModel], *, observed_at: datetime) -> int:
        """Insert one package's evidence rows, inside one transaction and no more.

        `transaction.atomic()` is *here* -- around the write of one package's
        rows -- and nowhere else in this class. It is nested inside the recorder
        (`CPM-AD-23`), so a later package's failure never rolls back an earlier
        package's evidence and the run ledger row is never inside a block that
        could take it away. `tests/unit/django_apps/test_collector_base_audit.py`
        asserts both halves: that no transaction encloses the recorder, and that
        this write is inside one.

        `bulk_create` and never `update_or_create`: evidence is append-only, and
        re-observation inserts (`CPM-AD-2`). `bulk_create` does not call
        `save()`, so the instant is checked here -- see `_require_stamped`.

        **The run-scoped path writes through here too, and that is the whole of
        why the sweep gets the same guarantees.** A sweeping subclass calls this
        from inside its own per-package `transaction.atomic()`, so the block
        opened here is a savepoint within it rather than a second boundary --
        and every row it writes is stamped-checked, model-checked and tallied on
        the way past. `_require_counted` reads that tally.

        Args:
            evidence: The unsaved rows to insert.
            observed_at: The instant every one of them must carry.

        Returns:
            How many rows were inserted.

        Raises:
            CollectorConfigurationError: When a row is not an instance of the
                declared evidence model, or carries a different instant.

        """
        rows = list(evidence)
        self._require_declared_model(rows)
        self._require_stamped(rows, observed_at=observed_at)
        with transaction.atomic():
            created = self._evidence_model.objects.bulk_create(rows)
        self._swept_rows += len(created)
        return len(created)

    def _require_declared_model(self, rows: Sequence[AppendOnlyModel]) -> None:
        """Refuse a row that does not belong to the table this collector declared.

        `CPM-AD-7` gives each collector its own evidence table, and
        `evidence_model` is where a collector says which. `bulk_create` is called
        on that model's manager, so a row of some *other* model reaches it as a
        set of attributes to read fields off -- Django does not require the
        objects to be instances of the model, and what lands is a row assembled
        from whichever fields happened to match, in the wrong table's shape.

        The per-package path never noticed because `translate` and
        `sentinel_evidence` are written beside the model they return; the sweep
        path assembles rows across a whole document and is where the mistake
        becomes reachable.

        Args:
            rows: The rows about to be inserted.

        Raises:
            CollectorConfigurationError: When any row is not an instance of the
                declared evidence model.

        """
        wrong = sorted({type(row).__name__ for row in rows if not isinstance(row, self._evidence_model)})
        if wrong:
            message = (
                f"{type(self).__name__} produced evidence row(s) of {wrong} rather than of its declared "
                f"evidence_model {self._evidence_model.__name__}. A collector writes its own append-only "
                f"table and no other (CPM-AD-7)."
            )
            raise CollectorConfigurationError(message)

    def _require_stamped(self, rows: Sequence[AppendOnlyModel], *, observed_at: datetime) -> None:
        """Refuse a row that does not carry the instant this run was handed.

        `AppendOnlyModel.save()` refuses a missing or naive `observed_at` and
        `core/models.py` calls that "the one place every evidence write passes
        through" -- but `bulk_create` does not call `save()`, so this base would
        walk around it on every write it makes. The check is restored here and
        made stricter: not merely aware, but *this* observation's instant. One
        observation has one moment (`CPM-AD-7`), and a collector stamping rows
        from anywhere else would make every freshness comparison and every
        observation window silently wrong -- in an append-only table that nothing
        may correct.

        Args:
            rows: The rows about to be inserted.
            observed_at: The instant they must carry.

        Raises:
            CollectorConfigurationError: When any row's `observed_at` is absent,
                naive, or a different instant.

        """
        wrong = [
            row.observed_at
            for row in rows
            if row.observed_at is None or not is_aware(row.observed_at) or row.observed_at != observed_at
        ]
        if wrong:
            message = (
                f"{type(self).__name__} produced {len(wrong)} evidence row(s) stamped {wrong!r} rather than "
                f"with the observed_at it was handed ({observed_at!r}). bulk_create does not call save(), so "
                f"this is the only guard between a mis-stamped observation and an append-only table nothing "
                f"can correct (CPM-AD-7, CPM-AD-26)."
            )
            raise CollectorConfigurationError(message)


def _reported(failures: Sequence[str]) -> str:
    """Return the clause a sweep's `detail` carries about the packages it lost.

    Written once because both of the sweep's non-clean endings need it and they
    must say the same thing: a `partial` naming its failures one way and a
    `failed` naming them another is two schemas in one column, which is the
    hazard `EVENT_KEYS` records for the log lines beside them.

    Args:
        failures: One message per package that could not be persisted, possibly
            none.

    Returns:
        The empty string when nothing failed, so the sentence around it reads
        normally; otherwise a clause naming how many failed and what each said.

    """
    if not failures:
        return ""
    return f", and {len(failures)} package(s) could not be persisted: {'; '.join(failures)}"


def _replayed(payload: Payload, *, remembered: CachedResponse, source: str) -> Payload:
    """Return the answer a `304` stands for, with the body this process kept.

    **What the collector sees, stated exactly.** The `body`, the `source` and
    the `found` flag are indistinguishable from a `200` carrying the same body,
    which is what matters: `translate` is a pure function from a recorded
    payload to evidence rows (`CPM-AD-27`), and a collector that had to know
    where its body came from would be a second parsing path to keep correct.
    What *is* visible is that this was a revalidation -- `not_modified` stays
    `True` and `status_code` stays `304` -- and both are deliberate. The base
    reads the flag to decide that the entry is being refreshed rather than
    replaced, and rewriting the status to `200` would be this module telling a
    collector something the source did not say, in a field whose whole purpose
    is to record what the source said.

    The validators are the source's latest: the `304`'s where it supplied them,
    the remembered ones otherwise. An origin may hand back a new `ETag` on a
    revalidation, and the next conditional request has to carry what the source
    last said.

    Args:
        payload: The not-modified answer, carrying no body.
        remembered: The entry whose body is replayed.
        source: The locator that was asked.

    Returns:
        A payload carrying the cached body, as though the source had served it
        again, and the validators that now describe it.

    """
    return Payload(
        source=source,
        found=True,
        body=remembered.body,
        status_code=payload.status_code,
        not_modified=True,
        etag=payload.etag or remembered.etag,
        last_modified=payload.last_modified or remembered.last_modified,
    )


def _entry_to_remember(payload: Payload, *, remembered: CachedResponse | None) -> CachedResponse:
    """Return the entry one answered call leaves behind.

    Args:
        payload: What the source said, or the replayed answer a `304` produced.
        remembered: The entry this run was holding.

    Returns:
        For a `304`, the remembered body with whatever validators now describe
        it -- `_replayed` has already resolved those, so this reads them off the
        payload like any other answer, and the one thing it must not take from
        there is the body, which a `304` does not carry. For anything else, the
        answer as served.

    Raises:
        ResponseCacheError: When the result would carry no validator and so
            could never be revalidated. `core/response_cache.py` refuses such an
            entry for everybody; the caller logs it and moves on.

    """
    body = remembered.body if payload.not_modified and remembered is not None else payload.body
    return CachedResponse(body=body, etag=payload.etag, last_modified=payload.last_modified)


def _require_reason(reason: object, *, label: str) -> str:
    """Refuse an `inapplicability` answer that is not a usable reason or a usable silence.

    The hook's answer is written verbatim into the row's `detail`, the ledger
    row's `detail` and the log line, and it decides whether a locator is asked
    for -- so an answer of the wrong kind is a defect in the subclass rather than
    a fact about the package. The empty string is the one silence: "applies".

    Args:
        reason: What the subclass answered.
        label: The class's own name, for the message.

    Returns:
        The reason, unchanged.

    Raises:
        CollectorConfigurationError: When the answer is not a string -- `None`,
            a boolean, an object whose truthiness would have decided the path --
            or when it is a non-empty string of nothing but whitespace, which is
            a collector saying "does not apply" with no reason to record.

    """
    if not isinstance(reason, str):
        message = (
            f"{label}.inapplicability answered {reason!r}, which is not a string. The answer is the reason a "
            f"not_applicable row records and the ledger carries; return the empty string when the question "
            f"applies and a sentence when it does not."
        )
        raise CollectorConfigurationError(message)
    if reason and not reason.strip():
        message = (
            f"{label}.inapplicability answered {reason!r}, which says the question does not apply and gives no "
            f"reason. A not_applicable row with a blank detail is an observation nobody can read back "
            f"(CPM-FR-6); return the empty string for a question that applies."
        )
        raise CollectorConfigurationError(message)
    return reason


def _require_name(name: str, *, label: str) -> str:
    """Refuse a collector that does not say what it is called.

    Args:
        name: The declared name.
        label: The class's own name, for the message.

    Returns:
        The name, unchanged.

    Raises:
        CollectorConfigurationError: When it is not a non-blank string.

    """
    if not isinstance(name, str) or not name.strip():
        message = (
            f"{label} declares name={name!r}. A run is traceable to the code that performed it (CPM-FR-39), "
            f"and a blank or absent name is a ledger row nothing can be traced to."
        )
        raise CollectorConfigurationError(message)
    return name


def _require_evidence_model(model: type[AppendOnlyModel] | None, *, label: str) -> type[AppendOnlyModel]:
    """Refuse a collector with nowhere append-only to write.

    Args:
        model: The declared evidence model.
        label: The class's own name, for the message.

    Returns:
        The model, unchanged.

    Raises:
        CollectorConfigurationError: When it is absent, or is not a subclass of
            `AppendOnlyModel`. A plain `Model` satisfies every annotation in
            this module and goes around every refusal `CPM-AD-2` makes -- the
            manager that offers no `update()`, the `save()` that refuses a
            written row, the base manager that cannot be used to bypass either.

    """
    if model is None:
        message = (
            f"{label} declares no evidence_model. A collector writes its own append-only table (CPM-AD-7); "
            f"a collector with nowhere to write cannot record that it failed either."
        )
        raise CollectorConfigurationError(message)
    if not (isinstance(model, type) and issubclass(model, AppendOnlyModel)):
        message = (
            f"{label} declares evidence_model={model!r}, which does not inherit AppendOnlyModel. Evidence is "
            f"append-only (CPM-AD-2); a plain Model would satisfy every annotation here and would carry none "
            f"of the refusals."
        )
        raise CollectorConfigurationError(message)
    return model


def _require_window(window: timedelta | None, *, label: str) -> timedelta:
    """Refuse an observation window that is absent, mistyped or negative.

    Args:
        window: The declared window.
        label: The class's own name, for the message.

    Returns:
        The window, unchanged.

    Raises:
        CollectorConfigurationError: When it is not a non-negative `timedelta`.

    """
    if not isinstance(window, timedelta) or window < NO_WINDOW:
        message = (
            f"{label} declares observation_window={window!r}, which is not an interval a window can be "
            f"measured over. Declare timedelta(0) to observe on every run; omitting the value, or declaring "
            f"a number of unstated units, is not the same statement."
        )
        raise CollectorConfigurationError(message)
    return window


def require_freshness_target(target: timedelta | None, *, label: str) -> timedelta:
    """Refuse a freshness target that is absent, mistyped, zero or negative.

    **Public where the other eight are private, and for one reason.** This is the
    single enforcement point `CPM-AD-28` names, and it has two moments: the
    collector's construction, above, and the boot sweep in
    `config/startup/stage_two.py` over the registered classes. A second copy of
    the rule in the startup module would be the two-enforcement-points problem
    this module's docstring is about; a private name imported across modules
    would be the same call wearing a `noqa`.

    Args:
        target: The declared target.
        label: The class's own name, for the message.

    Returns:
        The target, unchanged.

    Raises:
        CollectorConfigurationError: When it is not a `timedelta`, or is not
            strictly positive.

            Absence is the failure `CPM-AD-28` is named for: an unset target
            behaves as "fresh forever", so six-month-old evidence reads as
            current -- the `CPM-SM-C1` failure this product is built to avoid --
            and there is deliberately no sentinel meaning "never goes stale".

            Zero and negative are refused where `NO_WINDOW` is accepted, and the
            asymmetry is the point: "observe on every run" is a thing an operator
            means, while "evidence is stale the instant it is written" is not,
            and would make every surface permanently amber. The two sentinels
            look alike and behave oppositely, so this is written out rather than
            inherited by symmetry.

    """
    if not isinstance(target, timedelta) or target <= NO_FRESHNESS:
        message = (
            f"{label} declares freshness_target={target!r}, which is not an age evidence may reach. Every "
            f"collector declares a positive target (CPM-AD-28): an unset one behaves as fresh forever, so "
            f"six-month-old evidence reads as current, and a zero or negative one makes evidence stale the "
            f"instant it is written."
        )
        raise CollectorConfigurationError(message)
    return target


def freshness_target_fault(collectors: Sequence[type[Collector]]) -> str:
    """Return why registered collectors cannot be started, or nothing when they can.

    `CPM-AD-28`'s sweep as a pure function over the registered classes, so that
    the two places which enforce it call one rule rather than restating it:
    `collectors/apps.py` from the `ready()` that registers them -- the only hook
    in a deployed process that runs *after* the registry is populated -- and
    `config/startup/stage_two.py` as condition 10. `require_cadence` has the same
    shape next door and `collectors/sweep.py`'s `cadence_reconciliation_fault` is
    `CPM-AD-20`'s equivalent.

    **Every offender is reported together.** Raising on the first costs an
    operator a restart per collector: they fix the target the message named,
    redeploy, and meet the next refusal -- which with eight collectors coming is
    eight boots to learn what one could have said.

    **Classes, never instances.** Constructing a collector to ask what it
    declares would build a `RequestsTransport` and its connection pool inside
    `django.setup()`, which `gunicorn --preload` then forks into every worker.
    The declaration is a class attribute, so reading it costs nothing.

    Args:
        collectors: The registered collector classes. An empty roster is not a
            failure: a component that has adopted none has forgotten nothing.

    Returns:
        One message naming every collector whose declared target is absent,
        mistyped, zero or negative, or the empty string when every one of them
        declares a usable target. The message names the class and what the
        declaration actually says, because "a collector has no freshness target"
        tells an operator nothing they can act on while naming the class tells
        them which file to open.

    """
    undeclared: list[str] = []
    for collector in collectors:
        try:
            require_freshness_target(collector.freshness_target, label=collector.__name__)
        except CollectorConfigurationError as refusal:
            undeclared.append(f"{collector.__name__} (name={collector.name!r}): {refusal}")

    if not undeclared:
        return ""
    offenders = "; ".join(undeclared)
    return (
        f"{len(undeclared)} registered collector(s) cannot be started -- {offenders} An unset freshness "
        "target behaves as 'fresh forever', so evidence collected months ago reads as current on every "
        "surface -- which is the failure CPM-AD-28 exists to prevent, and it is invisible at run time. "
        "Declare freshness_target on each class named above."
    )


def require_cadence(cadence: timedelta | None, *, label: str) -> timedelta:
    """Refuse a declared cadence that is absent, mistyped, zero or negative.

    **Public where most of the declaration checks are private, and for the
    reason `require_freshness_target` is.** The rule has one enforcement point
    and that point is not this module's constructor: a cadence is only
    meaningful against the schedule that fires it, and the schedule is data an
    operator edits (`CPM-AD-20`). So it is `config/startup/stage_two.py` that
    calls this, over the registered classes, in the same sweep that refuses a
    missing freshness target -- and a copy of the rule restated there would be
    the two-enforcement-points problem this module's docstring is about.

    **It is never called for a collector that declares none.** `cadence = None`
    means "nothing schedules this", which is a legitimate state and the base's
    default; the boot sweep asks this only about a collector that declared a
    value, so `None` reaching here is a caller mistake rather than an absent
    declaration -- and it is refused with the same message, because a cadence
    somebody meant to declare and spelled `None` is exactly as unschedulable.

    Args:
        cadence: The declared cadence.
        label: The class's own name, for the message.

    Returns:
        The cadence, unchanged.

    Raises:
        CollectorConfigurationError: When it is not a `timedelta`, or is not
            strictly positive. Zero is refused for the reason a zero freshness
            target is: "run continuously" is not something an operator means,
            and a beat entry firing on a zero interval is a worker that never
            stops enqueueing.

    """
    if not isinstance(cadence, timedelta) or cadence <= NO_CADENCE:
        message = (
            f"{label} declares cadence={cadence!r}, which is not an interval anything can be scheduled on. A "
            f"collector that is swept package by package declares how often (CPM-AD-20), and the schedule "
            f"entry that fires it is reconciled against that number at boot; a zero or negative one would "
            f"fire continuously."
        )
        raise CollectorConfigurationError(message)
    return cadence


def _require_timeout(timeout: float | None, *, label: str) -> float:
    """Refuse a timeout that is absent, mistyped or unusable.

    Args:
        timeout: The declared timeout in seconds.
        label: The class's own name, for the message.

    Returns:
        The timeout, unchanged.

    Raises:
        CollectorConfigurationError: When it is not a positive finite number.
            `bool` is excluded explicitly: it is a subclass of `int`, so
            `timeout = True` would otherwise be a one-second timeout that
            somebody meant as a flag.

    """
    if isinstance(timeout, bool) or not isinstance(timeout, int | float) or not isfinite(timeout) or timeout <= 0:
        message = (
            f"{label} declares timeout={timeout!r}. Every outbound call carries a positive finite timeout "
            f"(CPM-NFR-3) and there is no default-less path; this is refused at construction rather than at "
            f"the call, where a ledger row would already be running."
        )
        raise CollectorConfigurationError(message)
    return float(timeout)


def _require_retries(retries: int, *, label: str) -> int:
    """Refuse a retry count that is mistyped or negative.

    Args:
        retries: The declared retry count.
        label: The class's own name, for the message.

    Returns:
        The count, unchanged.

    Raises:
        CollectorConfigurationError: When it is not a non-negative integer.

    """
    if isinstance(retries, bool) or not isinstance(retries, int) or retries < 0:
        message = (
            f"{label} declares retries={retries!r}, which is not a number of attempts. Declare 0 for a call "
            f"that is tried once; the value is also what the rate limiter is charged per collection."
        )
        raise CollectorConfigurationError(message)
    return retries


def _require_rate_limit(limit: RateLimit | None, *, label: str) -> RateLimit:
    """Refuse a collector that declares no allowance.

    Args:
        limit: The declared allowance.
        label: The class's own name, for the message.

    Returns:
        The allowance, unchanged.

    Raises:
        CollectorConfigurationError: When it is absent or is not a `RateLimit`.

    """
    if not isinstance(limit, RateLimit):
        message = (
            f"{label} declares rate_limit={limit!r}. Rate limiting lives in this base rather than per "
            f"collector (CPM-AD-20), and a call issued unlimited is the one thing that arrangement exists to "
            f"prevent."
        )
        raise CollectorConfigurationError(message)
    return limit


def _require_headers(headers: Mapping[str, str], *, label: str) -> Mapping[str, str]:
    """Refuse declared headers that are unusable, injectable, or the base's to send.

    Four refusals, and the middle two are about the fact that HTTP header names
    are case-insensitive on the wire while a Python mapping's keys are not.

    Args:
        headers: The declared header mapping.
        label: The class's own name, for the message.

    Returns:
        A read-only copy, so a collector cannot widen its own header set at run
        time by mutating the class attribute after construction -- the same
        reason `RateLimit` is frozen.

    Raises:
        CollectorConfigurationError: When the declaration is not a mapping, or
            names a header whose name or value is not a string -- listed by
            name, never by value; when a name or a value carries a carriage return
            or a line feed; when two names differ only in case; or when it names
            one of `CONDITIONAL_HEADERS`.

            The line-break refusal is the security one. A header value is
            terminated by CRLF, so a newline inside one *is* the start of the
            next header -- and these values are assembled from configuration
            (`Authorization: Bearer $TOKEN` is the obvious shape), which is
            exactly where an attacker-influenced string arrives. `requests`
            refuses some of these at call time and not all of them, and a
            refusal in a worker halfway through a sweep is not the same thing as
            a refusal where the collector is written.

            The duplicate-name refusal is the silent one. `{"user-agent": ...,
            "User-Agent": ...}` is two keys to Python and one header to every
            origin, so one of the two declarations is discarded by whichever
            merge happens to run last -- and which one survives is not
            something the declaring collector can see.

            The conditional refusal is `CPM-AD-20`'s: a collector sending its
            own `If-None-Match` asks a source about a body this process does not
            hold, and the `304` it earns has no entry behind it and fails the
            run.

    """
    if not isinstance(headers, Mapping):
        message = (
            f"{label} declares headers of type {type(headers).__name__}, which is not a mapping of header names "
            f"to values. Headers reach the socket through this base and nowhere else (CPM-AD-27); declare an "
            f"empty mapping to send none."
        )
        raise CollectorConfigurationError(message)
    # The *names* are listed and the values never are: a declared header is
    # assembled from configuration, and the one this refusal is most likely to
    # meet is `Authorization` carrying something that is not a string -- whose
    # repr would put a credential into a boot log (CPM-OPERATE-S05).
    unusable = sorted(
        repr(name) for name, value in headers.items() if not isinstance(name, str) or not isinstance(value, str)
    )
    if unusable:
        message = (
            f"{label} declares the header(s) {', '.join(unusable)} with a name or a value that is not a string. "
            f"Headers reach the socket through this base and nowhere else (CPM-AD-27), and only a string reaches "
            f"a socket; the values are not repeated here."
        )
        raise CollectorConfigurationError(message)
    broken = sorted(name for name, value in headers.items() if _has_line_break(name) or _has_line_break(value))
    if broken:
        message = (
            f"{label} declares the header(s) {broken} carrying a carriage return or a line feed. A header is "
            f"terminated by CRLF, so a line break inside one is the start of another header -- and these "
            f"values are assembled from configuration, which is where an injected string arrives."
        )
        raise CollectorConfigurationError(message)
    lowered = [name.lower() for name in headers]
    duplicated = sorted({name for name in lowered if lowered.count(name) > 1})
    if duplicated:
        message = (
            f"{label} declares the header name(s) {duplicated} more than once, differing only in case. HTTP "
            f"header names are case-insensitive on the wire and a mapping's keys are not, so one of the "
            f"declarations is discarded by whichever merge runs last and the collector cannot see which."
        )
        raise CollectorConfigurationError(message)
    forged = sorted(name for name in headers if name.lower() in _LOWERED_CONDITIONAL_HEADERS)
    if forged:
        message = (
            f"{label} declares the conditional header(s) {forged}. Conditional requests are composed by this "
            f"base from what the response cache holds (CPM-AD-20): a collector-supplied validator asks a "
            f"source about a body this process does not have, and the 304 it earns has no entry to replay."
        )
        raise CollectorConfigurationError(message)
    return MappingProxyType(dict(headers))


def _require_credential_host(host: str | None, *, headers: Mapping[str, str], label: str) -> str | None:
    """Refuse a credential host that is unusable, or a declared credential with no host to belong to.

    Args:
        host: The declared `credential_host`.
        headers: The already-checked declared headers, read for `Authorization`.
        label: The class's own name, for the message.

    Returns:
        The host, lower-cased, or `None` when none is declared and no
        credential is either.

    Raises:
        CollectorConfigurationError: When the host is declared and is not a
            non-empty string naming a bare host -- no scheme, no path, no
            whitespace -- or when the headers carry `Authorization` and no host
            is declared. The second refusal names the header and never its
            value (`CPM-OPERATE-S05`).

    """
    if host is None:
        if carries_credential(headers):
            message = (
                f"{label} declares an {AUTHORIZATION_HEADER} header and no credential_host, so this base cannot "
                f"tell which host the credential belongs to and would have to send it everywhere the collector "
                f"reads. Declare the one host it is for (CPM-OPERATE-S05); the value is not repeated here."
            )
            raise CollectorConfigurationError(message)
        return None
    if (
        not isinstance(host, str)
        or not host
        or any(character.isspace() for character in host)
        or host_of(f"https://{host}/") != host.lower()
    ):
        message = (
            f"{label} declares credential_host={host!r}, which is not a bare host name. It is compared against "
            f"the host of every locator this collector fetches, so it is a host and nothing else: no scheme, no "
            f"path, no port, no whitespace."
        )
        raise CollectorConfigurationError(message)
    return host.lower()


def _has_line_break(text: str) -> bool:
    """Report whether a header name or value carries a CR or an LF.

    Args:
        text: The declared name or value.

    Returns:
        True when it contains either, which makes it two headers rather than
        one.

    """
    return "\r" in text or "\n" in text


def _require_cache_ttl(ttl: timedelta | None, *, label: str) -> timedelta:
    """Refuse a response-cache lifetime that is absent, mistyped, negative or sub-second.

    Args:
        ttl: The declared lifetime.
        label: The class's own name, for the message.

    Returns:
        The lifetime, unchanged.

    Raises:
        CollectorConfigurationError: When it is not a non-negative `timedelta`,
            or when it is positive and shorter than the whole second the cache
            counts in. Absence is refused for the reason `observation_window`'s
            is: `NO_CACHE` says "read a body every run" and a reader can see it,
            while omitting the value says nothing and would leave the base
            guessing which of the two a collector meant. The sub-second refusal
            is `RateLimit.per`'s, arriving at the other value handed to the same
            API and failing the other way round: the lifetime is truncated to
            whole seconds, so anything under a second becomes `0`, which Django
            reads as *do not cache* -- a collector that declared caching, passed
            every check, and quietly caches nothing. `NO_CACHE` is the way to say
            that on purpose, and it is the one value below a second that is
            permitted.

    """
    if not isinstance(ttl, timedelta) or ttl < NO_CACHE:
        message = (
            f"{label} declares response_cache_ttl={ttl!r}, which is not a lifetime a cached response can be "
            f"held for. Declare NO_CACHE to fetch a body every run; omitting the value, or declaring a number "
            f"of unstated units, is not the same statement."
        )
        raise CollectorConfigurationError(message)
    if ttl > NO_CACHE and int(ttl.total_seconds()) < 1:
        message = (
            f"{label} declares response_cache_ttl={ttl!r}, which is shorter than the whole second the cache "
            f"counts in. It truncates to a zero-second lifetime, which Django reads as 'do not cache' -- so "
            f"the collector would look like it caches and would remember nothing. Declare NO_CACHE if that is "
            f"what was meant, and at least a second if it was not."
        )
        raise CollectorConfigurationError(message)
    return ttl
