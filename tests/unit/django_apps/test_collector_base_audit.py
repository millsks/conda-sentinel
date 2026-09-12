"""The sweep that keeps `CPM-EVIDENCE-S05`'s rules in one place, and the ledger safe.

Five rules, and each of them is a rule *about the source tree* rather than about
a run, because each is invisible at run time.

**One module opens connections.** `CPM-AD-27` puts the transport boundary in the
collector base so that a collector is a pure translation from a recorded payload
to evidence rows. A collector that built its own `requests.Session` would work
perfectly, would ship, and would take its parsing, its `not_found` handling and
its `error` handling behind the network with it -- which is the majority of this
product's behaviour and the whole thing `ASR-2` was raised about. Nothing fails;
the fast tier just quietly stops covering anything.

**Every call carries a timeout, and none of them is `None`.** `requests` defaults
to no timeout at all, so the omission is silent and the consequence is a worker
blocked until something upstream gives up. `timeout=None` is the same omission
written out, and it is refused everywhere with no exemption -- a decision to wait
forever is not one this product takes.

**Two modules read the cache, split by purpose and by nothing else.**
`config/settings/local.py` argues that the LocMem substitution is a substitution
because the cache API is preserved and no call site branches on the backend.
Keeping the reads in `core/rate_limit.py` and `core/response_cache.py` is what
makes that two call sites rather than eight, and
`tests/unit/django_apps/test_rate_limit.py` and
`tests/unit/django_apps/test_response_cache.py` hold the other half: that each
module which does read it uses only the public API.

The split is by *what is stored*, which is the only division that would not
drift: one owns how many requests may be issued and the other owns what a request
need not ask for again. They share a backend and share nothing else -- different
key namespaces, different lifetimes, different failure modes (an expired counter
is a fresh window; an unusable entry is a fetch). Folding them into one module
would put a counter and a body under one `clear()`, and folding the rule into
"any module in `core` may read the cache" would give the ten collectors the
door this file exists to shut.

**Every evidence write is inside a `transaction.atomic()`.** `CPM-AD-23` fixes
one package as the atomic unit so that a later package's failure never rolls back
an earlier package's evidence. Deleting the transaction leaves the write working
and every single-package case passing, because the property it buys is only
observable across two packages -- so the enclosure is asserted here and
demonstrated behaviourally in `tests/integration/django_apps/test_collection.py`.

**No `transaction.atomic()` encloses a run recorder.** This is `CPM-EVIDENCE-S03`'s
recorded, deferred constraint arriving at the story that makes it enforceable.
`core/ledger.py` states it: the `running` row is only worth having because it is
*committed* before the outbound call, so a caller that wraps the recorder in a
transaction and is then killed loses the row and the ledger records nothing.
`core/ledger.py` also states why there is no runtime guard -- pytest's
`django_db` runs every test inside exactly such a block, so a check on
`connection.in_atomic_block` would refuse the entire suite -- and says the
constraint "belongs to whichever story first writes a collector that could break
it (`CPM-EVIDENCE-S05`)". This is that story, and this is that guard.

**Matched on the parsed syntax tree, and receivers resolved from the imports.**
The reasons are `tests/unit/django_apps/test_clock_audit.py`'s and they apply
unchanged: prose about the prohibition -- this docstring, `core/transport.py`'s --
must not itself be an offence, and `import requests as http` then `http.Session()`
is a two-character rename that a table of literal names cannot see. So every
receiver is resolved back to what it was imported from before it is compared.

**What this scan cannot see, stated rather than discovered.** It reads one module
at a time and resolves names, so a session handed in from elsewhere and used
through a local, or a recorder called from a function that is itself called
inside an atomic block in another module, are outside its reach. What narrows
that gap is that the constructions themselves are what it matches -- a session
has to be built somewhere -- and that `core/collection.py` is the only caller of
the recorder in this repository.

**On the `AD-` prefix.** A bare `AD-n` in this repository is an *inherited*
platform decision; a decision from this product's own architecture spine always
carries the `CPM-` prefix.

Reads and parses repository files and nothing else: no database, no network, no
subprocess.
"""

from __future__ import annotations

import ast
import sys
from collections import Counter
from pathlib import Path
from typing import Final

import pytest

from conda_sentinel.core.registry import registrations
from tests.source_scan import SRC_ROOT
from tests.source_scan import dotted_name
from tests.source_scan import parse
from tests.source_scan import project_files

#: Every way this product could open, pool or issue an outbound connection,
#: spelled canonically -- module path plus attribute, as an import resolves it.
#:
#: `requests` first, because it is the declared dependency and therefore the one
#: somebody actually reaches for. `urllib3` next, because it is what `requests`
#: carries and what a reader who has just been told "no new dependency" finds.
#: Then the stdlib routes, which need no dependency at all and are exactly what
#: somebody writes to get around a ban on the first two. `httpx` and `aiohttp`
#: are neither present nor declared, and are listed so that adding one is a
#: failing gate rather than a quiet second transport.
#:
#: A table listing a library's *classes* and not its module-level verbs is a
#: table with a door in it: `import httpx; httpx.get(url)` opens a connection,
#: pools nothing, declares no retry, and walks straight past a rule whose stated
#: purpose is that adding a transport is a failing gate. So every library here is
#: listed by both -- the constructor somebody reaches for deliberately, and the
#: one-line verb somebody reaches for in a hurry.
BANNED_TRANSPORT_FORMS: Final[frozenset[str]] = frozenset(
    {
        "aiohttp.ClientSession",
        "aiohttp.request",
        "http.client.HTTPConnection",
        "http.client.HTTPSConnection",
        "httpx.AsyncClient",
        "httpx.Client",
        "httpx.delete",
        "httpx.get",
        "httpx.head",
        "httpx.options",
        "httpx.patch",
        "httpx.post",
        "httpx.put",
        "httpx.request",
        "httpx.stream",
        "requests.Session",
        "requests.adapters.HTTPAdapter",
        "requests.adapters.Retry",
        "requests.delete",
        "requests.get",
        "requests.head",
        "requests.options",
        "requests.patch",
        "requests.post",
        "requests.put",
        "requests.request",
        "requests.session",
        "socket.create_connection",
        "socket.socket",
        "urllib.request.Request",
        "urllib.request.build_opener",
        "urllib.request.urlopen",
        "urllib3.HTTPConnectionPool",
        "urllib3.HTTPSConnectionPool",
        "urllib3.PoolManager",
        "urllib3.Retry",
        "urllib3.request",
        "urllib3.util.Retry",
        "urllib3.util.retry.Retry",
    },
)

#: The transaction opener the ledger must never sit inside, and the one every
#: evidence write must sit inside.
ATOMIC_FORMS: Final[frozenset[str]] = frozenset({"django.db.transaction.atomic"})

#: The insert forms `CPM-AD-23` requires to be inside a per-package transaction.
#:
#: Matched on the method name alone, unlike everything else here: the receiver is
#: `self._evidence_model.objects`, which resolves to nothing an import can name,
#: and there is no other `bulk_create` or `acreate` in this repository for the
#: name to collide with. `update`, `delete` and the rest are absent because
#: `EVIDENCE.02-AUDIT-002` bans them outright rather than requiring a transaction
#: around them.
EVIDENCE_WRITE_METHODS: Final[frozenset[str]] = frozenset({"abulk_create", "acreate", "bulk_create", "create"})

#: The modules the evidence-write rule applies to, relative to `src/`.
#:
#: Scoped rather than repository-wide, and that is the honest bound: `create()`
#: is Django's most ordinary call and a repository-wide rule requiring a
#: transaction around every one of them would be a rule about something else
#: entirely. What `CPM-AD-23` is about is the collector base's write, so that is
#: what is checked.
#:
#: **One entry, still, and the reason a concrete collector did not join it is
#: worth stating.** `CPM-IDENTITY-S06` landed the first one, and it makes no
#: insert of its own: every row it writes goes through `Collector._write_evidence`
#: -- the module below -- which is what applies the declared-model check, the
#: `observed_at` check and the tally the base reconciles against what a sweep
#: reports. So this rule already covers its writes, at the one place they happen,
#: and adding its module here would fail
#: `test_the_evidence_write_rule_has_a_write_to_be_about` for the right reason:
#: there is no `create` in it to be about.
#:
#: What that leaves uncovered here is the collector's *own* obligation -- that its
#: per-package `transaction.atomic()` encloses that call and encloses no loop --
#: and `tests/unit/django_apps/test_inventory_ingestion.py` makes both of those
#: claims over the same file, plus the one this rule cannot: that the module has
#: no direct write at all.
TRANSACTIONAL_WRITE_MODULES: Final[tuple[str, ...]] = ("django_apps/conda_sentinel/core/collection.py",)

#: The two run recorders, by their canonical import paths.
LEDGER_MODULE: Final[str] = "conda_sentinel.core.ledger"
RECORDER_FORMS: Final[frozenset[str]] = frozenset(
    {f"{LEDGER_MODULE}.collection_run", f"{LEDGER_MODULE}.policy_run"},
)

#: The cache objects `django.core.cache` offers, and the only receivers a read is
#: matched on. The *backend classes* are deliberately absent:
#: `config/startup/stage_one.py` imports `LocMemCache` and `DummyCache` to refuse
#: a deployed component configured with one, which is a startup condition about
#: configuration rather than a call site reading the cache.
CACHE_RECEIVERS: Final[frozenset[str]] = frozenset({"django.core.cache.cache", "django.core.cache.caches"})

#: The keyword every outbound call must carry, and the one value it may never be
#: given. Reported as two different forms so an exemption can license a stated
#: timeout without also licensing an unbounded one.
TIMEOUT_KEYWORD: Final[str] = "timeout"
STATED_TIMEOUT_FORM: Final[str] = "timeout=..."
UNBOUNDED_TIMEOUT_FORM: Final[str] = "timeout=None"

# Recorded exemptions, keyed by module, by the exact form *and* by how many times
# that form may appear -- the shape `tests/unit/django_apps/test_clock_audit.py`
# established, and for the same reason: keying by form alone would licence the
# form for the whole file, so the next `requests.get` added to `jwks.py` would be
# permitted silently.
#
# config/authorization/jwks.py -- inherited platform. The JWKS fetch behind
# Bearer authentication: one `requests.get` carrying an explicit connect/read
# timeout pair, whose own comments explain the two values at length. It predates
# this rule, sits outside `CPM-EP-EVIDENCE`'s binding, and routing it through
# `core` would make `config` import a domain application -- inverting the
# dependency direction inherited `AD-4` fixes. It is also this audit's one piece
# of in-tree evidence that the resolution works on somebody else's code.
# core/transport.py -- the boundary itself. The session, the adapter, the retry
# policy and the timeout every request carries: one occurrence each, so a
# *second* session built in this file fails the gate exactly as a first one
# anywhere else would.
# core/collection.py -- the one place a declared timeout becomes a call setting.
# The base builds its default `RequestsTransport` from the collector's declared
# value, which is what makes "every call carries a timeout" true by construction
# rather than by every collector remembering. It constructs no session and issues
# no request; only the keyword is licensed here.
# core/rate_limit.py -- one of the two modules that read the cache, and the one
# that owns the counter. Three calls, which
# `tests/unit/django_apps/test_rate_limit.py` separately reconciles against the
# cache's public API.
# core/response_cache.py -- the other, and the one that owns the remembered
# response (`CPM-EVIDENCE-S08`). Three calls, reconciled the same way by
# `tests/unit/django_apps/test_response_cache.py`. It is a second entry rather
# than a widened rule because the exemption is spent per occurrence per module:
# a third module reaching the cache fails this file exactly as the second one
# would have before this story recorded it.
RECORDED_EXEMPTIONS: Final[dict[str, dict[str, int]]] = {
    "config/authorization/jwks.py": {"requests.get(...)": 1, STATED_TIMEOUT_FORM: 1},
    "django_apps/conda_sentinel/core/collection.py": {STATED_TIMEOUT_FORM: 1},
    "django_apps/conda_sentinel/core/rate_limit.py": {
        "cache.add(...)": 1,
        "cache.incr(...)": 1,
        "cache.set(...)": 1,
    },
    "django_apps/conda_sentinel/core/response_cache.py": {
        "cache.delete(...)": 2,
        "cache.get(...)": 1,
        "cache.set(...)": 1,
    },
    "django_apps/conda_sentinel/core/transport.py": {
        "requests.Session(...)": 1,
        "requests.adapters.HTTPAdapter(...)": 1,
        "requests.adapters.Retry(...)": 1,
        STATED_TIMEOUT_FORM: 1,
    },
}

#: The inherited call site the exemption table names. Asserted to be reachable by
#: the scan, so an exclusion added later cannot quietly take it out of view.
AN_INHERITED_OUTBOUND_CALL: Final[str] = "config/authorization/jwks.py"

#: The modules `CPM-EVIDENCE-S05`, `CPM-EVIDENCE-S08` and `CPM-IDENTITY-S06`
#: wrote, named so that a scan which had stopped reaching them would fail here
#: rather than report a clean repository.
#:
#: The last of them sits under a *different application* from the other three,
#: which is the reason it is worth naming as well as auditing: the walk starts at
#: `src/` and has never had to descend into a second `django_apps` subtree that
#: carries one of these rules, so an exclusion that happened to skip it would look
#: exactly like a repository that had not grown one.
#:
#: The three that carry a detectable form are named individually as well, because
#: `test_the_detectors_find_what_the_named_modules_actually_contain` measures the
#: detectors against them -- and reaching them by tuple index made adding a
#: fourth module a silent change of subject.
THE_LIMITER: Final[str] = "django_apps/conda_sentinel/core/rate_limit.py"
THE_RESPONSE_CACHE: Final[str] = "django_apps/conda_sentinel/core/response_cache.py"
THE_TRANSPORT: Final[str] = "django_apps/conda_sentinel/core/transport.py"
THE_INGESTION_COLLECTOR: Final[str] = "django_apps/conda_sentinel/collectors/tasks.py"

#: `CPM-CURRENCY-S01`'s collector, and the first module in this tree that reads a
#: *remote* source through the base. Named for a reason the ingestion collector's
#: entry does not cover: that one reads a local file through a substituted
#: transport, so it could satisfy every rule below by having no reason to break
#: one. This module has every reason -- a URL, a host, a page size and an API
#: version -- and reaches all of it through the injected transport, which is the
#: claim `CPM-AD-27` actually makes and the one an anchor here keeps in view.
THE_RELEASE_COLLECTOR: Final[str] = "django_apps/conda_sentinel/collectors/source_release.py"

#: `CPM-CURRENCY-S02`'s collector, and the second remote reader. Named for the
#: reason the first is, and for one more: it is the first collector that reads
#: `identity` through a join rather than a single column, and reaches its host
#: through the injected transport with a locator it built from a purl -- every
#: reason to open a connection of its own, and none taken.
THE_PYPI_COLLECTOR: Final[str] = "django_apps/conda_sentinel/collectors/pypi_release.py"

#: `CPM-CURRENCY-S03`'s collector, and the third remote reader. Named for the
#: reasons the first two are, and for one more: it is the first collector that
#: reads *two* hosts and makes a second call on **either** branch, so it has more
#: ways to reach a socket of its own than anything before it -- and takes none.
THE_FEEDSTOCK_COLLECTOR: Final[str] = "django_apps/conda_sentinel/collectors/feedstock.py"

#: `CPM-CURRENCY-S04`'s collector, and the fourth remote reader. Named for the
#: reasons the first three are, and for one more: it is the first collector
#: whose *number* of calls is configuration rather than code -- one per
#: monitored channel -- so it has a call site inside a loop and still reaches
#: every one of them through the injected transport.
THE_CONDA_PACKAGE_COLLECTOR: Final[str] = "django_apps/conda_sentinel/collectors/conda_package.py"

#: `CPM-CURRENCY-S05`'s dispatch, and the first module in this subtree that is
#: *not* a collector. Named for a reason none of the collectors above covers: it walks
#: the registry, resolves a Celery task and enqueues one message per package, so
#: it has every reason to reach for a transaction across those packages
#: (`CPM-AD-23`), for a row of its own (`CPM-AD-7`) or for a call of its own
#: (`CPM-AD-27`) -- and takes none. A scan that stopped reaching it would report a
#: clean repository over the one module here that collects nothing.
THE_SWEEP_DISPATCH: Final[str] = "django_apps/conda_sentinel/collectors/sweep.py"

#: `CPM-SECURITY-S01`'s collector, and the fifth remote reader. Named for the
#: reasons the first four are, and for one more: it is the first collector whose
#: *source* is a declared adapter rather than a host it knows
#: (`CPM-AD-29`), so nothing in it names a URL to be tempted by -- which makes it
#: the module where a reader most needs to see that the seam is the transport's
#: and not a second one this collector opened for itself.
THE_VULNERABILITY_COLLECTOR: Final[str] = "django_apps/conda_sentinel/collectors/vulnerability.py"

#: `CPM-SECURITY-S02`'s collector, and the sixth remote reader. Named for the
#: reasons the first five are, and for one more: it is the only collector that
#: reads another collector's evidence table -- an exception `CPM-AD-7` does not
#: grant and `CPM-SECURITY-S02`'s Spec Change Log records -- so it is the module
#: where a reader most needs to see that the *write* path is still the base's
#: alone and that no transaction, no socket and no second writer came with it.
THE_KEV_COLLECTOR: Final[str] = "django_apps/conda_sentinel/collectors/kev.py"

#: `CPM-SECURITY-S03`'s collector, and the seventh remote reader. Named for the
#: reasons the first six are, and for one more: it reads the *same document* a
#: shipped collector already reads, for a different fact -- so it is the module
#: where a reader most needs to see that it asks the source itself rather than
#: reaching into `conda_package_snapshots` for a licence that is already sitting
#: there. `CPM-AD-7` forbids that read and this collector does not take it, which
#: `MODULES_PERMITTED_TO_READ_ANOTHER_COLLECTORS_EVIDENCE` holds it to by name.
THE_LICENSE_COLLECTOR: Final[str] = "django_apps/conda_sentinel/collectors/license.py"

#: `CPM-PY314-S01`'s collector, and the eighth remote reader. Named for the
#: reasons the first seven are, and for one more: it reads the *same host* a
#: shipped collector already reads and the very field that collector already
#: stores -- `PyPIReleaseSnapshot.requires_python` -- so it is the module where a
#: reader most needs to see that it asks the source itself rather than reaching
#: into another collector's rows for a specifier that is already sitting there.
#: `CPM-AD-7` forbids that read and this collector does not take it, which
#: `MODULES_PERMITTED_TO_READ_ANOTHER_COLLECTORS_EVIDENCE` holds it to by name.
THE_READINESS_COLLECTOR: Final[str] = "django_apps/conda_sentinel/collectors/python_readiness.py"

#: `CPM-IDENTITY-S08`'s collector, and the ninth remote reader. Named for the
#: reasons the first eight are, and for one more: it is the first collector that
#: *writes* outside its own evidence table -- through `identity`'s
#: `record_resolution`, inside a transaction it opens itself -- so it is the
#: module where a reader most needs to see that the transaction sits inside
#: `translate` and never around the run recorder, and that the two hosts it reads
#: are reached through the injected transport and nothing it opened for itself.
THE_RESOLUTION_COLLECTOR: Final[str] = "django_apps/conda_sentinel/collectors/resolve_identity.py"
THE_NEW_MODULES: Final[tuple[str, ...]] = (
    "django_apps/conda_sentinel/core/collection.py",
    THE_CONDA_PACKAGE_COLLECTOR,
    THE_FEEDSTOCK_COLLECTOR,
    THE_INGESTION_COLLECTOR,
    THE_KEV_COLLECTOR,
    THE_LICENSE_COLLECTOR,
    THE_LIMITER,
    THE_PYPI_COLLECTOR,
    THE_READINESS_COLLECTOR,
    THE_RELEASE_COLLECTOR,
    THE_RESOLUTION_COLLECTOR,
    THE_RESPONSE_CACHE,
    THE_SWEEP_DISPATCH,
    THE_TRANSPORT,
    THE_VULNERABILITY_COLLECTOR,
)

#: The modules this repository permits to read **another** collector's evidence
#: table, and it holds exactly one.
#:
#: `CPM-AD-7` forbids the read outright; `CPM-SECURITY-S02` takes one exception and
#: records it. A named, licensable set rather than a bare ban, in the shape the
#: source-declaration sweeps already use: the rule has to be satisfiable by the
#: module the exception was taken for, or it would be deleted rather than amended,
#: and the amendment is the record of who took it.
MODULES_PERMITTED_TO_READ_ANOTHER_COLLECTORS_EVIDENCE: Final[frozenset[str]] = frozenset({THE_KEV_COLLECTOR})


def module_of(collector: type) -> str:
    """Return a collector class's own source file, relative to `src/`.

    Args:
        collector: The registered collector.

    Returns:
        Its module's path under `src/`, in the spelling the constants above use.

    """
    source = sys.modules[collector.__module__].__file__
    assert source is not None
    return Path(source).resolve().relative_to(SRC_ROOT).as_posix()


def _foreign_evidence_named(module: str, *, own: str, every: set[str]) -> set[str]:
    """Return the evidence models a collector module names that are not its own.

    Args:
        module: The module's path under `src/`.
        own: The name of the evidence model that collector writes.
        every: Every registered collector's evidence model name.

    Returns:
        The foreign ones it names, which is empty for every collector but the one
        licensed above.

    """
    tree = parse(SRC_ROOT / module)
    named = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    named |= {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    named |= {node.value for node in ast.walk(tree) if isinstance(node, ast.Constant) and isinstance(node.value, str)}
    return (named & every) - {own}


# Synthetic modules the detectors are measured against. Source text parsed here
# rather than files on disk: a fixture module under `src/` would be found by the
# scan itself and would need an exemption of its own.
A_SESSION = """
import requests

session = requests.Session()
"""

AN_ALIASED_SESSION = """
import requests as http

session = http.Session()
"""

A_MODULE_LEVEL_GET = """
import requests

body = requests.get("https://example.invalid").text
"""

A_BARE_IMPORTED_GET = """
from requests import get

body = get("https://example.invalid").text
"""

A_RETRY_DECLARATION = """
from urllib3.util.retry import Retry

policy = Retry(total=3)
"""

AN_ALIASED_RETRY = """
from urllib3.util.retry import Retry as Backoff

policy = Backoff(total=3)
"""

AN_ADAPTER = """
from requests.adapters import HTTPAdapter

adapter = HTTPAdapter()
"""

A_RAW_SOCKET = """
import socket

connection = socket.create_connection(("example.invalid", 443))
"""

A_URLLIB_OPEN = """
from urllib.request import urlopen

body = urlopen("https://example.invalid").read()
"""

AN_UNBOUNDED_TIMEOUT = """
def fetch(session, url):
    return session.get(url, timeout=None)
"""

A_STATED_TIMEOUT = """
def fetch(session, url):
    return session.get(url, timeout=5.0)
"""

A_CACHE_READ = """
from django.core.cache import cache

used = cache.incr("key")
"""

AN_ALIASED_CACHE_READ = """
from django.core.cache import cache as store

used = store.incr("key")
"""

A_CACHES_READ = """
from django.core.cache import caches

used = caches["default"].incr("key")
"""

A_RECORDER_INSIDE_ATOMIC = """
from django.db import transaction

from conda_sentinel.core.ledger import collection_run


def collect(clock):
    with transaction.atomic():
        with collection_run(collector="x", clock=clock) as run:
            run.succeeded()
"""

AN_ALIASED_RECORDER_INSIDE_ATOMIC = """
from django.db.transaction import atomic

from conda_sentinel.core.ledger import collection_run as recorded


def collect(clock):
    with atomic():
        with recorded(collector="x", clock=clock) as run:
            run.succeeded()
"""

A_DECORATED_RECORDER = """
from django.db import transaction

from conda_sentinel.core.ledger import policy_run


@transaction.atomic
def compose(clock, cutoff):
    with policy_run(policy_version="1", evidence_cutoff=cutoff, clock=clock) as run:
        run.succeeded()
"""

A_COMPACT_RECORDER_INSIDE_ATOMIC = """
from django.db import transaction

from conda_sentinel.core.ledger import collection_run


def collect(clock):
    with transaction.atomic(), collection_run(collector="x", clock=clock) as run:
        run.succeeded()
"""

A_RELATIVELY_IMPORTED_RECORDER_INSIDE_ATOMIC = """
from django.db import transaction

from ..core.ledger import collection_run


def collect(clock):
    with transaction.atomic():
        with collection_run(collector="x", clock=clock) as run:
            run.succeeded()
"""

A_MODULE_LEVEL_HTTPX_GET = """
import httpx

body = httpx.get("https://example.invalid").text
"""

AN_UNENCLOSED_EVIDENCE_WRITE = """
def write(model, rows):
    model.objects.bulk_create(rows)
"""

AN_ENCLOSED_EVIDENCE_WRITE = """
from django.db import transaction


def write(model, rows):
    with transaction.atomic():
        model.objects.bulk_create(rows)
"""

THE_CORRECT_NESTING = """
from django.db import transaction

from conda_sentinel.core.ledger import collection_run


def collect(clock, model, rows):
    with collection_run(collector="x", clock=clock) as run:
        with transaction.atomic():
            model.objects.bulk_create(rows)
        run.succeeded()
"""

THE_CORRECT_COMPACT_NESTING = """
from django.db import transaction

from conda_sentinel.core.ledger import collection_run


def collect(clock, model, rows):
    with collection_run(collector="x", clock=clock) as run, transaction.atomic():
        model.objects.bulk_create(rows)
"""

AN_ATOMIC_BESIDE_A_RECORDER = """
from django.db import transaction

from conda_sentinel.core.ledger import collection_run


def collect(clock, model, rows):
    with transaction.atomic():
        model.objects.bulk_create(rows)
    with collection_run(collector="x", clock=clock) as run:
        run.succeeded()
"""

A_CALL_THROUGH_THE_SEAM = """
def collect(transport, source):
    return transport.fetch(source)
"""

A_LOCAL_CALLED_CACHE = """
def remember(cache, key):
    return cache.incr(key)
"""

A_TIMEOUT_IN_A_MAPPING = """
settings = {"timeout": None}
"""

PROSE_ONLY = '''
"""Nothing here builds a requests.Session or passes timeout=None; it only says so."""
'''


def import_bindings(tree: ast.Module) -> dict[str, str]:
    """Return every local name bound by an import, mapped to what it names.

    A third namesake of `tests/source_scan.py`'s `dotted_name` family, and it is
    a different function again: that one spells an expression out, and this one
    resolves the *root* of such a spelling back to the module it came from. The
    distinction is what makes `import requests as http` visible -- the detectors
    compare canonical names, never source spellings.

    Args:
        tree: The parsed module.

    Returns:
        Local name to the canonical dotted path it refers to. `import a.b.c`
        binds `a` to `a`, because that is how it is used; `import a.b.c as x`
        binds `x` to `a.b.c`; `from a.b import c as d` binds `d` to `a.b.c`.

    """
    bindings: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    bindings[alias.asname] = alias.name
                else:
                    root = alias.name.partition(".")[0]
                    bindings[root] = root
        elif isinstance(node, ast.ImportFrom):
            # A relative import carries no absolute path to resolve against --
            # `from ..core.ledger import collection_run` knows only
            # `core.ledger` -- so the partial path is bound and `matches` below
            # compares by suffix. Skipping relative imports instead, which this
            # scan did, made `from .ledger import collection_run` invisible to
            # the one rule this module exists for.
            module = node.module or ""
            for alias in node.names:
                bindings[alias.asname or alias.name] = f"{module}.{alias.name}" if module else alias.name
    return bindings


def canonical(spelling: str, bindings: dict[str, str]) -> str:
    """Resolve a source spelling to the canonical path it names.

    Args:
        spelling: The dotted spelling at the call site, from `dotted_name`.
        bindings: What the module's imports bound, from `import_bindings`.

    Returns:
        The canonical dotted path, or `""` for anything rooted in a name the
        module did not import -- a parameter, a local, an attribute of `self`.
        The empty answer is what keeps `transport.fetch(...)` and a local
        variable called `cache` out of every table below.

    """
    if not spelling:
        return ""
    head, _, rest = spelling.partition(".")
    base = bindings.get(head)
    if base is None:
        return ""
    return f"{base}.{rest}" if rest else base


def matches(resolved: str, forms: frozenset[str]) -> bool:
    """Report whether a resolved spelling names one of the forms.

    Exact, or a suffix at a dot boundary. The suffix half is what makes a
    relative import comparable: `from ..core.ledger import collection_run`
    resolves to `core.ledger.collection_run`, which is the tail of the absolute
    form. The dot boundary is what keeps it safe -- `utils.get` does not match
    `requests.get`, because the test is against `".utils.get"` rather than
    against `"get"`.

    Args:
        resolved: What `canonical` returned.
        forms: The table to compare against.

    Returns:
        True when the spelling names one of the forms.

    """
    if not resolved:
        return False
    return any(form == resolved or form.endswith(f".{resolved}") for form in forms)


def transport_forms(tree: ast.Module) -> list[str]:
    """Return every outbound-connection form one module constructs or issues.

    Args:
        tree: The parsed module.

    Returns:
        One `line: form` entry per occurrence, the form spelled canonically --
        `requests.Session(...)` -- so an exemption licenses the shape that was
        reviewed rather than any connection in that file.

    """
    bindings = import_bindings(tree)
    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        resolved = canonical(dotted_name(node.func), bindings)
        named = [form for form in sorted(BANNED_TRANSPORT_FORMS) if form == resolved or form.endswith(f".{resolved}")]
        if named:
            found.append(f"{node.lineno}: {named[0]}(...)")
    return sorted(found, key=_by_line)


def timeout_settings(tree: ast.Module) -> list[str]:
    """Return every `timeout=` keyword one module passes, telling `None` from a value.

    Args:
        tree: The parsed module.

    Returns:
        One `line: form` entry per occurrence -- `timeout=...` for a stated
        value, `timeout=None` for the unbounded wait. Two forms, so a module
        licensed to state a timeout is not thereby licensed to omit one.

    """
    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for keyword in node.keywords:
            if keyword.arg != TIMEOUT_KEYWORD:
                continue
            unbounded = isinstance(keyword.value, ast.Constant) and keyword.value.value is None
            found.append(f"{node.lineno}: {UNBOUNDED_TIMEOUT_FORM if unbounded else STATED_TIMEOUT_FORM}")
    return sorted(found, key=_by_line)


def cache_reads(tree: ast.Module) -> list[str]:
    """Return every call one module makes against `django.core.cache`.

    Args:
        tree: The parsed module.

    Returns:
        One `line: form` entry per occurrence, spelled `cache.<method>(...)` --
        the receiver reported by its canonical last segment rather than by
        whatever the importing module called it, so an alias cannot buy a
        different exemption.

    """
    bindings = import_bindings(tree)
    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        resolved = canonical(dotted_name(node.func), bindings)
        receiver, _, method = resolved.rpartition(".")
        if matches(receiver, CACHE_RECEIVERS):
            found.append(f"{node.lineno}: {receiver.rpartition('.')[2]}.{method}(...)")
        elif matches(resolved, CACHE_RECEIVERS):
            found.append(f"{node.lineno}: {resolved.rpartition('.')[2]}(...)")
    found.extend(
        f"{node.lineno}: {canonical(dotted_name(node.value), bindings).rpartition('.')[2]}[...]"
        for node in ast.walk(tree)
        if isinstance(node, ast.Subscript) and matches(canonical(dotted_name(node.value), bindings), CACHE_RECEIVERS)
    )
    return sorted(found, key=_by_line)


def recorders_inside_atomic(tree: ast.Module) -> list[str]:
    """Return every run recorder this module opens inside a transaction.

    Three shapes, and the third is the one this detector was blind to. A
    `with transaction.atomic():` block whose body reaches a recorder; a function
    decorated `@transaction.atomic` that opens one; and
    `with transaction.atomic(), collection_run(...) as run:` -- the *same*
    violation written on one line, where both context managers are items of a
    single `ast.With` and the recorder therefore never appears in the atomic's
    body at all. A formatter nudges toward exactly that spelling, so a detector
    that only looked at bodies would report a clean module for the shorter way of
    writing the thing it exists to ban.

    Position within a single `with` is what decides it, because position is what
    decides the nesting: items are entered left to right, so an atomic *before* a
    recorder wraps it and is an offence, while a recorder before an atomic is the
    correct order and is not.

    Args:
        tree: The parsed module.

    Returns:
        One `line: form` entry per occurrence, naming the recorder that is
        enclosed.

    """
    bindings = import_bindings(tree)
    found: list[str] = []
    for node in ast.walk(tree):
        found.extend(_recorders_beside_atomic(node, bindings))
        enclosed = _atomic_body(node, bindings)
        if enclosed is None:
            continue
        found.extend(
            f"{inner.lineno}: transaction.atomic() encloses {resolved.rpartition('.')[2]}(...)"
            for statement in enclosed
            for inner in ast.walk(statement)
            if isinstance(inner, ast.Call)
            and matches(resolved := canonical(dotted_name(inner.func), bindings), RECORDER_FORMS)
        )
    return sorted(found, key=_by_line)


def _recorders_beside_atomic(node: ast.AST, bindings: dict[str, str]) -> list[str]:
    """Return recorders that share one `with` with an atomic opened before them.

    Args:
        node: Any node from the walk.
        bindings: What the module's imports bound.

    Returns:
        One entry per recorder entered after an atomic in the same statement.

    """
    if not isinstance(node, ast.With | ast.AsyncWith):
        return []
    found: list[str] = []
    atomic_seen = False
    for item in node.items:
        expression = item.context_expr
        if not isinstance(expression, ast.Call):
            continue
        resolved = canonical(dotted_name(expression.func), bindings)
        if matches(resolved, ATOMIC_FORMS):
            atomic_seen = True
        elif atomic_seen and matches(resolved, RECORDER_FORMS):
            found.append(
                f"{expression.lineno}: transaction.atomic() encloses {resolved.rpartition('.')[2]}(...)",
            )
    return found


def evidence_writes_outside_atomic(tree: ast.Module) -> list[str]:
    """Return every evidence insert this module makes outside a transaction.

    The positive half of `CPM-AD-23`, and the half the inverted-nesting rule
    cannot state. Deleting `with transaction.atomic():` from
    `core/collection.py`'s evidence write leaves every behavioural case passing:
    one package still writes its rows, and the property the nesting exists for --
    a later package's failure not rolling back an earlier package's evidence --
    is not something a single-package case can observe. So the enclosure is
    asserted structurally here and demonstrated behaviourally in
    `tests/integration/django_apps/test_collection.py`.

    Args:
        tree: The parsed module.

    Returns:
        One `line: form` entry per insert that no transaction encloses.

    """
    bindings = import_bindings(tree)
    enclosed = {
        id(inner)
        for node in ast.walk(tree)
        if (body := _atomic_body(node, bindings)) is not None
        for statement in body
        for inner in ast.walk(statement)
    }
    return sorted(
        (
            f"{node.lineno}: {method}(...) outside transaction.atomic()"
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and (method := dotted_name(node.func).rpartition(".")[2]) in EVIDENCE_WRITE_METHODS
            and id(node) not in enclosed
        ),
        key=_by_line,
    )


def _atomic_body(node: ast.AST, bindings: dict[str, str]) -> list[ast.stmt] | None:
    """Return the statements a transaction encloses, if this node opens one.

    Args:
        node: Any node from the walk.
        bindings: What the module's imports bound.

    Returns:
        The enclosed statements, or `None` when this node opens no transaction.

    """
    if isinstance(node, ast.With | ast.AsyncWith) and any(
        isinstance(item.context_expr, ast.Call)
        and matches(canonical(dotted_name(item.context_expr.func), bindings), ATOMIC_FORMS)
        for item in node.items
    ):
        return node.body
    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and any(
        matches(canonical(_decorator_spelling(decorator), bindings), ATOMIC_FORMS) for decorator in node.decorator_list
    ):
        return node.body
    return None


def _decorator_spelling(decorator: ast.expr) -> str:
    """Return the dotted spelling of a decorator, called or bare.

    Args:
        decorator: The decorator expression.

    Returns:
        The spelling. `@transaction.atomic` and `@transaction.atomic()` are the
        same decision and must resolve the same way.

    """
    return dotted_name(decorator.func) if isinstance(decorator, ast.Call) else dotted_name(decorator)


def _by_line(entry: str) -> int:
    """Return the line number an entry starts with, for ordering.

    Args:
        entry: A `line: form` string.

    Returns:
        The line number as an integer.

    """
    return int(entry.split(":", 1)[0])


def findings_in(path: Path) -> list[str]:
    """Return every finding of every rule in one file.

    Args:
        path: The module to scan.

    Returns:
        One `line: form` string per finding, from every detector that applies to
        this module, ordered by line so a report reads down the file.

    """
    tree = parse(path)
    findings = [*transport_forms(tree), *timeout_settings(tree), *cache_reads(tree), *recorders_inside_atomic(tree)]
    if path.relative_to(SRC_ROOT).as_posix() in TRANSACTIONAL_WRITE_MODULES:
        findings.extend(evidence_writes_outside_atomic(tree))
    return sorted(findings, key=_by_line)


#: Every module under `src/` the rules apply to, migrations excluded for the
#: reason `tests/unit/django_apps/test_clock_audit.py` gives: generated code is
#: not a decision anybody took.
SUBJECT_MODULES: Final[tuple[Path, ...]] = project_files(SRC_ROOT, skip_migrations=True)


def test_the_scan_reaches_the_modules_the_rules_are_about() -> None:
    """The anti-vacuity guard: the files this story wrote are in view.

    A scan that had stopped reaching them -- an exclusion widened, a walk that
    lost a directory -- would report an empty repository and pass every
    assertion below while proving nothing.
    """
    relative = {path.relative_to(SRC_ROOT).as_posix() for path in SUBJECT_MODULES}

    assert len(SUBJECT_MODULES) > len(RECORDED_EXEMPTIONS), f"expected modules under {SRC_ROOT}"
    for named in (*THE_NEW_MODULES, AN_INHERITED_OUTBOUND_CALL):
        assert named in relative, named


def test_the_detectors_find_what_the_named_modules_actually_contain() -> None:
    """The other half of the guard: the looking finds something, in real code.

    `config/authorization/jwks.py` is in the list on purpose. It is somebody
    else's module, written before this rule existed, and it carries exactly the
    two forms the rule is about -- so a detector that had stopped recognising
    `requests.get` or a `timeout=` keyword goes red here rather than reporting a
    clean repository.
    """
    assert transport_forms(parse(SRC_ROOT / AN_INHERITED_OUTBOUND_CALL)) != []
    assert timeout_settings(parse(SRC_ROOT / AN_INHERITED_OUTBOUND_CALL)) != []
    assert transport_forms(parse(SRC_ROOT / THE_TRANSPORT)) != []
    assert cache_reads(parse(SRC_ROOT / THE_LIMITER)) != []
    assert cache_reads(parse(SRC_ROOT / THE_RESPONSE_CACHE)) != []


@pytest.mark.parametrize("relative", TRANSACTIONAL_WRITE_MODULES, ids=str)
def test_the_evidence_write_rule_has_a_write_to_be_about(relative: str) -> None:
    """The positive rule's own anti-vacuity guard.

    `evidence_writes_outside_atomic` reports nothing for a module that makes no
    evidence write at all, which is indistinguishable from a module whose write
    is correctly enclosed. So the write is asserted to exist: the rule is only
    meaningful while there is something for it to be about, and the day
    `core/collection.py` stops writing evidence is a day somebody should have to
    notice.
    """
    tree = parse(SRC_ROOT / relative)
    writes = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and dotted_name(node.func).rpartition(".")[2] in EVIDENCE_WRITE_METHODS
    ]

    assert writes != [], f"{relative} makes no evidence write, so the enclosure rule proves nothing"
    assert evidence_writes_outside_atomic(tree) == []


@pytest.mark.parametrize(
    "path",
    SUBJECT_MODULES,
    ids=lambda path: str(path.relative_to(SRC_ROOT)),
)
def test_no_module_breaks_a_collector_base_rule(path: Path) -> None:
    """The rules, per module, so a violation names the file that introduced it.

    The exemption above is spent per occurrence rather than per form: a module
    that has used its one recorded session gets no second one for free, and
    `timeout=None` is licensed nowhere at all.
    """
    relative = path.relative_to(SRC_ROOT).as_posix()
    exempted = RECORDED_EXEMPTIONS.get(relative, {})
    findings = findings_in(path)
    counted = Counter(finding.split(": ", 1)[1] for finding in findings)
    over_quota = {form for form, count in counted.items() if count > exempted.get(form, 0)}
    offences = [finding for finding in findings if finding.split(": ", 1)[1] in over_quota]

    assert offences == [], f"{relative} breaks a collector-base rule: {offences}"


def test_the_exemption_table_has_entries_to_check() -> None:
    """The parametrize below means nothing if the table it reads is empty."""
    assert RECORDED_EXEMPTIONS != {}


def test_no_exemption_licenses_an_unbounded_timeout() -> None:
    """The one rule with no way out, asserted against the table rather than trusted.

    Every other form here is a decision somebody could legitimately record.
    `timeout=None` is not: it is the omission written out, and an exemption for
    it would be a recorded decision to block a worker forever.
    """
    licensed = {form for forms in RECORDED_EXEMPTIONS.values() for form in forms}

    assert UNBOUNDED_TIMEOUT_FORM not in licensed


@pytest.mark.parametrize("relative", sorted(RECORDED_EXEMPTIONS), ids=str)
def test_every_recorded_exemption_still_describes_the_file(relative: str) -> None:
    """An exemption that no longer applies is a licence nobody meant to leave open.

    Checked in the direction the exemption is granted, exactly as
    `tests/unit/django_apps/test_clock_audit.py` does: the module has to be one
    the scan reaches -- a rename would otherwise leave the entry green while the
    file it licenses went unscanned -- and it has to still contain the recorded
    form exactly as many times as the table records. Move the JWKS fetch behind
    the transport and this fails until its entry goes with it; add a second
    session to `core/transport.py` and it fails from the other side.
    """
    module = SRC_ROOT / relative

    assert module in SUBJECT_MODULES, f"{relative} is exempted but is not a module the scan reaches"

    counted = Counter(finding.split(": ", 1)[1] for finding in findings_in(module))
    recorded = RECORDED_EXEMPTIONS[relative]
    mismatched = {
        form: (counted.get(form, 0), expected)
        for form, expected in recorded.items()
        if counted.get(form, 0) != expected
    }

    assert mismatched == {}, f"{relative}: recorded exemptions no longer match, found vs recorded {mismatched}"


@pytest.mark.parametrize(
    "source",
    [
        A_SESSION,
        AN_ALIASED_SESSION,
        A_MODULE_LEVEL_GET,
        A_BARE_IMPORTED_GET,
        A_RETRY_DECLARATION,
        AN_ALIASED_RETRY,
        AN_ADAPTER,
        A_RAW_SOCKET,
        A_URLLIB_OPEN,
        A_MODULE_LEVEL_HTTPX_GET,
    ],
    ids=[
        "session",
        "session-aliased",
        "module-level-get",
        "bare-imported-get",
        "retry",
        "retry-aliased",
        "adapter",
        "raw-socket",
        "urllib",
        "httpx-verb",
    ],
)
def test_the_transport_detector_matches_every_banned_form(source: str) -> None:
    """Ten spellings of "open a connection here", because a scan that knew one has a door.

    The two aliased ones are the pair a table of literal names cannot see, and
    they are what somebody writes *after* being told not to use
    `requests.Session`. The socket and `urllib` cases are what somebody writes
    after being told not to add a dependency. `httpx.get` is the one this table
    was blind to: listing a library's client class and not its module-level verb
    leaves the one-line form -- the form somebody reaches for in a hurry --
    walking straight through a rule whose stated purpose is that adding a
    transport is a failing gate.
    """
    assert transport_forms(ast.parse(source)) != []


@pytest.mark.parametrize(
    "library",
    ["aiohttp", "httpx", "requests", "urllib.request", "urllib3"],
    ids=str,
)
def test_every_listed_library_is_listed_by_verb_as_well_as_by_constructor(library: str) -> None:
    """The property that would have caught the hole, stated once rather than per entry.

    A library named only by its client class is a library whose one-line call is
    unguarded. This asserts the table's *shape* -- every library it mentions is
    mentioned more than once -- so the next transport added to it cannot be added
    half way.
    """
    listed = [form for form in BANNED_TRANSPORT_FORMS if form.startswith(f"{library}.")]

    assert len(listed) > 1, f"{library} is listed only as {listed}; a module-level verb would walk past the table"


@pytest.mark.parametrize(
    "source",
    [A_CALL_THROUGH_THE_SEAM, A_LOCAL_CALLED_CACHE, THE_CORRECT_NESTING, AN_ATOMIC_BESIDE_A_RECORDER, PROSE_ONLY],
    ids=["through-the-seam", "local-named-cache", "correct-nesting", "atomic-beside", "prose"],
)
def test_the_detectors_ignore_what_is_not_an_offence(source: str) -> None:
    """The negative control, and the whole reason for parsing rather than grepping.

    `transport.fetch(source)` is the call this design exists to *require*. A
    parameter called `cache` is not the cache. The correct nesting -- recorder
    outside, `transaction.atomic()` within -- is the shape `core/collection.py`
    is built around, and an audit that flagged it would be an audit banning the
    thing it is for. A grep for `atomic`, `cache` or `fetch` flags every one of
    these.
    """
    tree = ast.parse(source)

    assert transport_forms(tree) == []
    assert cache_reads(tree) == []
    assert recorders_inside_atomic(tree) == []


def test_the_timeout_detector_tells_a_stated_value_from_an_unbounded_one() -> None:
    """An exemption licenses the shape that was reviewed, not any timeout in the file.

    `timeout=5.0` and `timeout=None` are opposite decisions with one spelling
    between them, so a module licensed to state a timeout must not be silently
    licensed to omit one.
    """
    stated = timeout_settings(ast.parse(A_STATED_TIMEOUT))
    unbounded = timeout_settings(ast.parse(AN_UNBOUNDED_TIMEOUT))

    assert [entry.split(": ", 1)[1] for entry in stated] == [STATED_TIMEOUT_FORM]
    assert [entry.split(": ", 1)[1] for entry in unbounded] == [UNBOUNDED_TIMEOUT_FORM]


def test_a_timeout_key_in_a_mapping_is_not_a_call_site() -> None:
    """The false positive a text search would produce, and the reason this parses.

    `{"timeout": None}` is configuration data, not an outbound call, and a scan
    that could not tell the two apart would be turned off within a day.
    """
    assert timeout_settings(ast.parse(A_TIMEOUT_IN_A_MAPPING)) == []


@pytest.mark.parametrize(
    "source",
    [A_CACHE_READ, AN_ALIASED_CACHE_READ, A_CACHES_READ],
    ids=["cache", "cache-aliased", "caches"],
)
def test_the_cache_detector_matches_every_way_in(source: str) -> None:
    """Three routes to the same object, and the alias is the one a table would miss.

    `caches["default"]` is the other one worth naming: it reaches the same
    backend by a different import, so a rule written only against `cache` would
    leave the door open beside it.
    """
    assert cache_reads(ast.parse(source)) != []


@pytest.mark.parametrize(
    "source",
    [
        A_RECORDER_INSIDE_ATOMIC,
        AN_ALIASED_RECORDER_INSIDE_ATOMIC,
        A_DECORATED_RECORDER,
        A_COMPACT_RECORDER_INSIDE_ATOMIC,
        A_RELATIVELY_IMPORTED_RECORDER_INSIDE_ATOMIC,
    ],
    ids=["with-block", "aliased", "decorated", "compact", "relative-import"],
)
def test_the_ledger_detector_matches_a_recorder_inside_a_transaction(source: str) -> None:
    """`CPM-EVIDENCE-S03`'s deferred constraint, in the five shapes it can arrive in.

    The decorated one is the one to write by accident: `@transaction.atomic` is a
    single line above a function whose body then looks perfectly correct, and the
    `running` row it silently makes unrecoverable is exactly the row a killed
    worker leaves behind.

    The last two are holes this detector had. **Compact**:
    `with transaction.atomic(), collection_run(...) as run:` is the identical
    violation on one line, and both context managers are items of a single
    `ast.With` -- so a detector looking inside the atomic's *body* never sees the
    recorder at all, and a formatter nudges toward exactly that spelling.
    **Relative import**: the binding table skipped `from ..core.ledger import
    collection_run` outright, so the one rule this module exists for was blind to
    the import style a sibling module inside `core` would most naturally use.
    """
    assert recorders_inside_atomic(ast.parse(source)) != []


@pytest.mark.parametrize(
    "source",
    [THE_CORRECT_NESTING, THE_CORRECT_COMPACT_NESTING],
    ids=["nested", "compact"],
)
def test_the_ledger_detector_permits_the_nesting_the_base_actually_uses(source: str) -> None:
    """The rule is about the order, not about the two constructs appearing together.

    `core/collection.py` opens the recorder and nests one `transaction.atomic()`
    around one package's evidence write (`CPM-AD-23`). An audit that could not
    tell that from the inversion would ban the design it exists to protect -- and
    that has to hold in the compact spelling too, where the order is the position
    within one `with` rather than the indentation: items are entered left to
    right, so a recorder written before an atomic is outside it.
    """
    assert recorders_inside_atomic(ast.parse(source)) == []


def test_the_evidence_write_detector_tells_an_enclosed_write_from_a_bare_one() -> None:
    """`CPM-AD-23`'s positive half, measured against both shapes.

    The inverted-nesting rule cannot state this one: deleting
    `with transaction.atomic():` from an evidence write leaves the write working,
    leaves every single-package case passing, and removes the only thing that
    keeps package *N*'s failure from reaching package *N*-1's rows.
    """
    assert evidence_writes_outside_atomic(ast.parse(AN_UNENCLOSED_EVIDENCE_WRITE)) != []
    assert evidence_writes_outside_atomic(ast.parse(AN_ENCLOSED_EVIDENCE_WRITE)) == []


# ---------------------------------------------------------------------------
# `CPM-AD-7`: no collector reads another collector's evidence table, except the
# one this repository licenses by name.
# ---------------------------------------------------------------------------


def test_only_the_licensed_module_reads_another_collectors_evidence_table() -> None:
    """The clause `collectors/models.py` used to carry in prose, as an object a second reader trips over.

    `CPM-AD-7` says a collector "never reads another collector's evidence table",
    and `CPM-SECURITY-S02` takes one exception: `collectors/kev.py` reads
    `vulnerability_findings`, because `CPM-FR-12` is *defined* as a cross-reference
    of what `CPM-FR-11` recorded and a KEV row that carried no link would fail its
    acceptance criterion outright. That story's Spec Change Log argues it and hands
    the judgement to review.

    **A prose clause cannot be tripped over.** The models module said "none reads
    another's" while one did, so the sentence had to go; what replaces it is this
    sweep, in the shape this repository already uses for the source declarations --
    a named, licensable set that ships holding exactly the module the exception was
    taken for. A second collector reaching for the same read fails here until
    somebody adds it and says why, which is the point: the cost of the exception is
    that it stays one exception.

    The evidence models are read off the registry rather than listed, so a seventh
    collector's table joins the sweep by being registered.
    """
    registered = registrations()
    own = {module_of(collector): collector.evidence_model.__name__ for collector in registered.values()}
    every = set(own.values())

    reading = {
        module: sorted(named)
        for module, mine in own.items()
        if (named := _foreign_evidence_named(module, own=mine, every=every))
    }

    assert set(reading) <= MODULES_PERMITTED_TO_READ_ANOTHER_COLLECTORS_EVIDENCE, (
        f"these collector modules read another collector's evidence table without a licence: {reading}"
    )


def test_every_licensed_module_still_takes_the_read_it_is_licensed_for() -> None:
    """The other direction, and the half that keeps the licence honest.

    A licence nothing uses is a hole somebody can walk through later without
    anybody noticing it was opened. If `collectors/kev.py` stops reading
    `vulnerability_findings` -- because the rollup story took the join, or because
    the exception was ruled against -- the entry has to come out in the same
    change, and the Spec Change Log entry with it.
    """
    registered = registrations()
    own = {module_of(collector): collector.evidence_model.__name__ for collector in registered.values()}
    every = set(own.values())

    for module in MODULES_PERMITTED_TO_READ_ANOTHER_COLLECTORS_EVIDENCE:
        assert module in own, f"{module} is licensed to read another collector's evidence and is not a collector"
        assert _foreign_evidence_named(module, own=own[module], every=every), (
            f"{module} is licensed to read another collector's evidence table and reads none"
        )
