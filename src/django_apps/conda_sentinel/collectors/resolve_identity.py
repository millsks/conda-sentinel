"""Resolution that finds its mappings: the collector that gives `record_resolution` something to record.

`CPM-FR-1` says each package resolves to a source repository, its release-
ecosystem identity and zero or more conda-forge feedstocks, and until this
module nothing did: `identity/services.py`'s `record_resolution` was a recorder
with no production caller, so on a real watchlist every package stayed
`unmapped` with every mapping `unknown`, and the three collectors that select on
a mapping selected nothing. This is the eleventh collector, and it is the
"batch resolution job" the architecture names: per package it reads two public
documents, decides what they establish, and hands that to the one door
`CPM-AD-14` leaves open.

**Two documents, and the index goes first.** The base makes exactly one fetch
and, when the document is absent, writes the `not_found` sentinel itself --
`translate` is never reached and `sentinel_evidence` may not fetch, because a
call from a row-shaping hook loses its reason. So whichever source is asked first
ends the run with its absence. On a conda watchlist the common case is a package
on conda-forge and not on PyPI rather than the reverse, so conda-forge's
`feedstock-outputs` index is the base's call and PyPI's project document is the
bounded second call inside `translate`. The cost is stated rather than hidden:
**a package absent from conda-forge's index has no PyPI identity resolved by
this collector.** Its snapshot row says `not_found`, nothing is recorded on the
package, and it is offered again next cadence. That is the same trade
`collectors/feedstock.py` made for its second call, and it is deferred on the
same terms.

**The second call never fails the run.** It is one locator, one page, and every
way of not finding out -- a transport failure, a `304` to a request that carried
no validator, a document whose shape has changed -- becomes a sentence in
`detail` beside what the first document established, with the two PyPI-derived
mappings recorded `error` rather than `not_found`: "could not ask" and "asked
and it is not there" are different answers (`CPM-FR-6`), and only the second is
the informative negative. **And a failure to ask never lowers what an earlier
run established**: when PyPI could not be asked, the two PyPI-derived mappings
re-assert the package's current `established` outcome and stored value, so a
transient failure cannot take a package out of the sweeps that select on them.

**Which repository is chosen is a documented precedence, not a search.** PyPI's
`project_urls` is free-form; this module reads `Source`, `Source Code`,
`Repository`, `Code` and `GitHub` in that order, matched case-insensitively on
the stripped key, and takes the first that is present -- whether or not its
value turns out to be readable, because a project that labelled its source is a
project whose label is the answer. `Homepage` is consulted only when none of the
five is present, and only counts when it is a `github.com` owner/repository.
Keys outside that list never win, however GitHub-shaped their value: a
`Bug Tracker` pointing at GitHub is not a source repository.

**The normalised URL is one `collectors/source_release.py` will read.** That
collector's `_repository_segments` is the acceptance rule: a `github.com` or
`www.github.com` host, exactly an owner and a repository, lower-cased, the `.git`
suffix stripped. This module additionally strips a `/tree/...` or `/blob/...`
tail, because a `Source` link into a subdirectory is a link to the repository
that holds it, and emits `https://github.com/<owner>/<repo>` and nothing else.
A URL that does not normalise is recorded as a `not_found` source repository
with the rejected URL and the reason in `detail`, never repaired into one.

**Identity is mutated through `record_resolution` and nothing else**
(`CPM-AD-14`). This module reads `Package` for the pair it was found by and the
name it is called, reads `Feedstock` to know whether rows already exist, and
writes neither -- `identity_source` and `associator_key` are passed to the
recorder verbatim, so the join key `CPM-IDENTITY-S02` protects is never in this
module's hands. What it claims is `inventory-derived` when anything was
established and `unmapped` when nothing was -- the recorder refuses a confidence
nothing earned, and that refusal is the right rule. It corrects no name. A
`verified` package is never selected, so the recorder's downgrade guard is never
even offered a claim to hold back; the snapshot records `downgrade_refused` all
the same, because the guard is the recorder's and a manual collection of a
verified package reaches it.

**Feedstock rows are additive and the index's silence does not remove them.**
`record_resolution` never deletes a feedstock -- removal is a correction, and
corrections are the audited override's -- so a package that holds `Feedstock`
rows from an earlier run keeps its `feedstock` mapping `established` when the
index now lists none, and `detail` says so. `not_found` is recorded only for a
package that holds no rows.

**One transaction per package, opened here around the recorder's write.**
`record_resolution` opens none of its own and says the caller owns the boundary
(`CPM-AD-23`); `translate` is where this collector is that caller, so the
recording and the snapshot row's construction sit in one `transaction.atomic()`
nested inside the base's run recorder and never around it. The row itself is
inserted by the base through `_write_evidence`, after `translate` returns, in
the base's own block -- which is the one bound worth stating: the resolution is
committed before the snapshot is, so a snapshot write refused by the base's own
checks leaves a recorded resolution with no evidence row beside it. Both of
those checks are on this module's own row shape -- the declared model and the
run's instant -- and the row is built from the recorder's result inside the
same block, so the only way to reach that state is a defect in this module.

**The pure functions are the whole of what this module decides.** The two
locators, `normalised_repository`, `repository_from`, `feedstocks_in`,
`project_urls_in` and `resolution_for` take data and return data, reachable with
no database, no socket and no clock (`CPM-AD-27`).

**On the `AD-` prefix.** A bare `AD-n` in this repository is an *inherited*
platform decision; a decision from this product's own architecture spine always
carries the `CPM-` prefix.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import timedelta
from types import MappingProxyType
from typing import TYPE_CHECKING
from typing import ClassVar
from typing import Final
from urllib.parse import urlsplit
from urllib.parse import urlunsplit

from django.db import models
from django.db import transaction

from conda_sentinel.collectors.agent import USER_AGENT
from conda_sentinel.collectors.models import IdentityResolutionSnapshot
from conda_sentinel.collectors.selection import RESOLVED_CONFIDENCES
from conda_sentinel.core.collection import Collector
from conda_sentinel.core.collection import CollectorConfigurationError
from conda_sentinel.core.collection import request_headers
from conda_sentinel.core.ledger import current_trace_id
from conda_sentinel.core.outcomes import OutcomeState
from conda_sentinel.core.rate_limit import RateLimit
from conda_sentinel.core.transport import DEFAULT_RETRIES
from conda_sentinel.core.transport import TransportError
from conda_sentinel.identity.models import ESTABLISHED
from conda_sentinel.identity.models import Feedstock
from conda_sentinel.identity.models import IdentityConfidence
from conda_sentinel.identity.models import MappingKind
from conda_sentinel.identity.models import Package
from conda_sentinel.identity.models import PackageMapping
from conda_sentinel.identity.services import FEEDSTOCK_NAME_LENGTH
from conda_sentinel.identity.services import FeedstockMapping
from conda_sentinel.identity.services import Resolution
from conda_sentinel.identity.services import record_resolution

if TYPE_CHECKING:
    from collections.abc import Iterable
    from collections.abc import Mapping
    from collections.abc import Sequence
    from datetime import datetime

    from conda_sentinel.core.models import AppendOnlyModel
    from conda_sentinel.core.transport import Payload

__all__ = [
    "CARRIED_FORWARD_DETAIL",
    "COLLECTOR_NAME",
    "CONDA_FORGE_ORG",
    "CONDA_PURL_PREFIX",
    "FEEDSTOCKS_FIELD",
    "FEEDSTOCK_OUTPUTS_BRANCH",
    "FEEDSTOCK_OUTPUTS_HOST",
    "FEEDSTOCK_OUTPUTS_REPOSITORY",
    "FEEDSTOCK_SUFFIX",
    "GITHUB_WEB_HOST",
    "GITHUB_WEB_HOSTS",
    "HOMEPAGE_KEY",
    "INDEX_LISTS_NONE_DETAIL",
    "INFO_FIELD",
    "ISSUES_KEYS",
    "MAX_DOCUMENT_CHARACTERS",
    "NO_PYPI_PROJECT_DETAIL",
    "NO_REPOSITORY_DETAIL",
    "PRIOR_ROWS_KEPT_DETAIL",
    "PROJECT_URLS_FIELD",
    "PYPI_HOST",
    "PYPI_PURL_PREFIX",
    "PYPI_UNREADABLE_DETAIL",
    "REPOSITORY_PRECEDENCE",
    "RESOLUTION_CACHE_TTL",
    "RESOLUTION_CADENCE",
    "RESOLUTION_FRESHNESS_TARGET",
    "RESOLUTION_HEADERS",
    "RESOLUTION_OBSERVATION_WINDOW",
    "RESOLUTION_RATE_LIMIT",
    "RESOLUTION_RETRIES",
    "RESOLUTION_TIMEOUT",
    "SHARD_FILL",
    "SHARD_LENGTH",
    "TOLERATED_MISSED_RUNS",
    "ChosenRepository",
    "IdentityResolutionCollector",
    "PackageIdentity",
    "ProjectAnswer",
    "ResolutionDocumentError",
    "ResolutionLocatorError",
    "ResolvedFacts",
    "conda_purl",
    "feedstocks_in",
    "index_locator",
    "normalised_label",
    "normalised_name",
    "normalised_repository",
    "project_locator",
    "project_urls_in",
    "pypi_purl",
    "repository_from",
    "resolution_for",
]

#: What this collector is called, on its ledger rows, in its cache keys and in
#: the registry `config/startup/stage_two.py` sweeps. It is the `collect` half of
#: its task name too, which is what routes it (`core/queues.py`).
COLLECTOR_NAME: Final[str] = "resolve_identity"

#: How often this collector is meant to run, and the number every other interval
#: below is derived from. Daily, the fast end of `CPM-NFR-2`'s range: the three
#: collectors that select on what this one records run daily, and a resolution a
#: week behind would leave a newly ingested package unobserved for that week.
#: The cadence itself is data in `django_celery_beat` (`CPM-AD-20`); this is the
#: number the arithmetic below assumes, reconciled at start-up in both directions
#: from `collectors/apps.py`'s `ready()`.
RESOLUTION_CADENCE: Final[timedelta] = timedelta(days=1)

#: How many consecutive missed collections may pass before this product stops
#: calling a resolution current -- PRD Open Question 7's risk posture.
TOLERATED_MISSED_RUNS: Final[int] = 1

#: How long this collector's evidence may be read as current (`CPM-AD-28`):
#: `cadence x (1 + tolerated_missed_runs)`, strictly greater than the cadence so
#: a package does not read stale at exactly the moment its next run is due.
RESOLUTION_FRESHNESS_TARGET: Final[timedelta] = RESOLUTION_CADENCE * (1 + TOLERATED_MISSED_RUNS)

#: How long a successful observation suppresses the next one (`CPM-AD-7`). Half
#: the cadence, so a scheduled run is never suppressed by the previous one.
RESOLUTION_OBSERVATION_WINDOW: Final[timedelta] = RESOLUTION_CADENCE / 2

#: How many times a failed request is retried, and therefore what the rate
#: limiter is charged against per collection.
RESOLUTION_RETRIES: Final[int] = DEFAULT_RETRIES

#: Seconds any single connect or read phase may take.
#:
#: Two, and the arithmetic is the reason. `core/transport.py`'s
#: `worst_case_call_seconds()` bounds one call through the mounted retry policy,
#: and **both** of this collector's calls go through it: the base builds the
#: transport from this collector's own `retries`, and the second call inside
#: `translate` reaches the same transport, so it is retried exactly as the first
#: is. One collection's real ceiling is therefore twice the computed worst case
#: -- at two seconds, 38 of the inherited 60-second soft limit (`CPM-AD-9`),
#: which leaves the ledger writes around it 22. `tests/unit/django_apps/
#: test_resolve_identity.py` reconciles the doubled figure against the settings
#: module's own declared limit rather than against a number repeated here.
RESOLUTION_TIMEOUT: Final[float] = 2.0

#: How hard this collector may push its sources (`CPM-AD-20`).
#:
#: Neither host publishes a numeric ceiling: PyPI's guidance is an identifying
#: `User-Agent` and reasonable use, and the raw GitHub host serves files with
#: limits of its own that it does not state. Sixty requests a minute is the same
#: declared courtesy bound `collectors/pypi_release.py` states for the same host
#: -- one request a second, fifteen packages a minute at `1 + retries` -- written
#: down so it is a limit the base enforces rather than an allowance that is
#: unlimited by omission. The second call is not charged against it, on the
#: terms the feedstock collector records the same gap.
RESOLUTION_RATE_LIMIT: Final[RateLimit] = RateLimit(calls=60, per=timedelta(minutes=1))

#: What this collector's sources expect on every request (`CPM-AD-20`,
#: `CPM-AD-27`): declared here, merged and sent by the base, never by this module.
#: `Accept` asks PyPI for the JSON representation; the raw host ignores it and
#: serves the file. The `User-Agent` is the one identity every collector shares
#: (`collectors/agent.py`). Nothing conditional is declared -- the validators are
#: the base's.
RESOLUTION_HEADERS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    },
)

#: How long a remembered response may be replayed before it is re-read. Longer
#: than the cadence, so a scheduled collection revalidates rather than
#: re-transfers; the raw host serves an `ETag`, which is what makes a `304`
#: reachable at all.
RESOLUTION_CACHE_TTL: Final[timedelta] = timedelta(days=7)

#: The two hosts this collector reads. Separate constants because they are
#: separate facts: one serves conda-forge's index of what each feedstock outputs,
#: the other serves a project's document.
FEEDSTOCK_OUTPUTS_HOST: Final[str] = "raw.githubusercontent.com"
PYPI_HOST: Final[str] = "pypi.org"

#: The organisation every conda-forge feedstock lives under, the repository that
#: publishes the index of their outputs, and the branch the index is read at.
CONDA_FORGE_ORG: Final[str] = "conda-forge"
FEEDSTOCK_OUTPUTS_REPOSITORY: Final[str] = "feedstock-outputs"
FEEDSTOCK_OUTPUTS_BRANCH: Final[str] = "main"

#: The suffix conda-forge gives every feedstock repository. The index names the
#: feedstock without it; the `Feedstock` row records the repository with it, once,
#: which is the spelling `collectors/feedstock.py`'s locator accepts either way.
FEEDSTOCK_SUFFIX: Final[str] = "-feedstock"

#: The web host a normalised repository URL names, and the hosts a stored one may
#: name to be read as it. The same pair `collectors/source_release.py` accepts,
#: restated rather than imported because no collector imports another
#: (`CPM-AD-7`); the unit tier reconciles the two.
GITHUB_WEB_HOST: Final[str] = "github.com"
GITHUB_WEB_HOSTS: Final[frozenset[str]] = frozenset({GITHUB_WEB_HOST, "www.github.com"})

#: How the index shards its files: by the first three alphanumeric characters of
#: the output's name, each a directory, padded with `z` when the name is shorter.
#: `numpy` lives at `outputs/n/u/m/numpy.json` and `qt` at `outputs/q/t/z/qt.json`.
SHARD_LENGTH: Final[int] = 3
SHARD_FILL: Final[str] = "z"

#: The purl prefixes this collector writes. A PyPI purl has no namespace; the
#: conda purl this collector records names the package and nothing else, because
#: which channel and which build are `CPM-FR-10`'s observations rather than the
#: package's identity.
PYPI_PURL_PREFIX: Final[str] = "pkg:pypi/"
CONDA_PURL_PREFIX: Final[str] = "pkg:conda/"

#: The purl type resolution records as the primary ecosystem, in the spelling
#: `collectors/pypi_release.py` selects on.
PYPI_PURL_TYPE: Final[str] = "pypi"

#: The fields of the two documents this collector reads, named rather than
#: spelled at the call sites so the reader and the cases that build documents
#: cannot drift.
FEEDSTOCKS_FIELD: Final[str] = "feedstocks"
INFO_FIELD: Final[str] = "info"
PROJECT_URLS_FIELD: Final[str] = "project_urls"

#: The `project_urls` keys that name a source repository, in the order one is
#: chosen, and the two consulted only when none of them is present. Keys are
#: matched on their PEP 753 normalised label -- lower-cased, with every
#: character that is not a letter or a digit removed -- so `source-code`,
#: `Source_Code` and `Source Code` are one label, which is how PyPI itself reads
#: them. See the module docstring for why a key outside these lists never wins.
REPOSITORY_PRECEDENCE: Final[tuple[str, ...]] = ("Source", "Source Code", "Repository", "Code", "GitHub")
HOMEPAGE_KEY: Final[str] = "Homepage"
#: The well-known issue-tracker labels, consulted after `Homepage` and only when
#: the link is `github.com/<owner>/<repo>/issues`: GitHub issues live in the
#: repository, so that path names it without inference. Any other tracker is not
#: a repository.
ISSUES_KEYS: Final[tuple[str, ...]] = ("Issues", "Issue Tracker", "Bug Tracker", "Tracker")
_ISSUES_SEGMENT: Final[str] = "issues"

#: What is not a letter or a digit, removed when a `project_urls` label is
#: normalised (PEP 753).
_NOT_LABEL: Final[re.Pattern[str]] = re.compile(r"[^a-z0-9]")

#: The path segments that lead *into* a repository rather than naming one, and
#: are stripped with everything after them.
_TAIL_SEGMENTS: Final[frozenset[str]] = frozenset({"tree", "blob"})

#: How many path segments a repository URL names: the owner and the repository.
_REPOSITORY_SEGMENTS: Final[int] = 2

#: The suffix a clone URL carries and a repository name does not.
_GIT_SUFFIX: Final[str] = ".git"

#: What a conda package name, a feedstock name and a repository segment may be
#: spelled with once lower-cased -- the grammar `collectors/feedstock.py` accepts
#: a feedstock under, restated for the reason the hosts are. The leading
#: underscore is real: `_libgcc_mutex` is a conda-forge package.
_NAME_SEGMENT: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9_][a-z0-9._-]*$")

#: What a project name looks like once PEP 503 has normalised it, and the runs
#: of separators that normalisation collapses -- the grammar
#: `collectors/pypi_release.py` reads a purl with, restated on the same terms.
_NORMALISED_NAME: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")
_SEPARATORS: Final[re.Pattern[str]] = re.compile(r"[-_.]+")

#: The longest document this collector will hand to `json.loads`, in characters.
#: The same bound `collectors/pypi_release.py` declares for the same project
#: document, restated rather than imported: the document lists every file of
#: every release, so for a large project it runs to several mebibytes, and the
#: index file beside it is a few dozen bytes. What the bound protects is the
#: parse and nothing earlier (`CPM-AD-9`).
MAX_DOCUMENT_CHARACTERS: Final[int] = 32 * 1024 * 1024

#: What a row says in its own words, in each of the ways it is reachable. Named
#: so the row a run writes and the case that reads it back cannot drift.
NO_PYPI_PROJECT_DETAIL: Final[str] = "PyPI has no project under this name"
PYPI_UNREADABLE_DETAIL: Final[str] = "PyPI could not be asked"
NO_REPOSITORY_DETAIL: Final[str] = "no source repository was established"
INDEX_LISTS_NONE_DETAIL: Final[str] = "the feedstock-outputs index lists no feedstock for this package"
CARRIED_FORWARD_DETAIL: Final[str] = (
    "the PyPI-derived mappings an earlier run established are re-asserted rather than lowered"
)
PRIOR_ROWS_KEPT_DETAIL: Final[str] = "the feedstock rows already recorded are kept and the mapping stays established"


class ResolutionLocatorError(ValueError):
    """A package's stored name cannot be turned into a locator this collector reads.

    A `ValueError` subclass, matching every "this input cannot describe what it
    claims to" in this product. It escapes `collect()` from `source_for` rather
    than becoming an evidence row: the run ledger row is finalized `failed`
    carrying this message, and nothing is recorded on the package -- a name that
    cannot be looked up is not a resolution of anything.
    """


class ResolutionDocumentError(ValueError):
    """A document could not be read as what it claims to be.

    Raised from `translate` for the index -- the base writes an `error` row and
    re-raises, so `CPM-NFR-3` holds: never a clean result, never no row, and
    nothing recorded on the package. For the project document it is caught
    inside the bounded second call and becomes a sentence in `detail`, because
    the index has already established something the run must not lose.
    """


@dataclass(frozen=True, slots=True)
class PackageIdentity:
    """What this collector reads off the package row and its mapping rows before it asks anything.

    Attributes:
        canonical_name: The name the package is called, which both locators are
            built from.
        identity_source: Half of the pair the recorder finds the package by,
            passed through verbatim (`CPM-FR-2`).
        associator_key: The other half, on the same terms.
        release_outcome: What the `release_ecosystem` mapping currently reads,
            or blank when no mapping row exists. Read so a run on which PyPI
            could not be asked re-asserts an earlier establishment rather than
            overwriting it with `error`.
        source_outcome: The `source_repository` mapping's, on the same terms.
        primary_purl: What the package currently holds, carried forward with
            an established `release_ecosystem` mapping.
        primary_type: Carried forward with it.
        source_repository_url: Carried forward with an established
            `source_repository` mapping.

    """

    canonical_name: str
    identity_source: str
    associator_key: str
    release_outcome: str = ""
    source_outcome: str = ""
    primary_purl: str = ""
    primary_type: str = ""
    source_repository_url: str = ""


@dataclass(frozen=True, slots=True)
class ChosenRepository:
    """Which `project_urls` entry was chosen as the source repository, and what came of it.

    Attributes:
        key: The key that won, as the document spelled it, or blank when no key
            in the precedence was present and `Homepage` did not qualify.
        url: The value that key carried, as the document spelled it, or blank.
        normalised: The repository URL in the form `collectors/source_release.py`
            reads, or blank when the chosen value could not be read as one.
        detail: Why nothing usable was chosen, or empty when `normalised` is set.

    """

    key: str = ""
    url: str = ""
    normalised: str = ""
    detail: str = ""


@dataclass(frozen=True, slots=True)
class ProjectAnswer:
    """What the bounded second call to PyPI came back with.

    Three answers rather than two, because "asked and there is no project" and
    "could not ask" are different facts (`CPM-FR-6`), and only the first is the
    informative negative.

    Attributes:
        asked: Whether a readable answer was obtained -- found or absent. False
            for every way of not finding out.
        found: Whether PyPI holds a project under the name. False when absent
            and False when nobody could ask; `asked` tells the two apart.
        project_urls: The project's `project_urls`, or empty.
        source: The locator that was asked, or blank when none could be built.
        detail: What happened, in words worth storing, or empty for a project
            that answered.

    """

    asked: bool
    found: bool
    project_urls: Mapping[str, str]
    source: str
    detail: str


@dataclass(frozen=True, slots=True)
class ResolvedFacts:
    """What one run concluded, as the resolution to record and the facts the row keeps.

    Attributes:
        resolution: What `record_resolution` is handed.
        repository_url: The normalised repository URL, or blank.
        repository_key: The `project_urls` key that won precedence, whether or
            not its value was readable, or blank when none did.
        pypi_found: Whether PyPI holds a project under the name.
        feedstocks: The feedstock names exactly as the index stated them.
        detail: What the row says beside its facts.

    """

    resolution: Resolution
    repository_url: str
    repository_key: str
    pypi_found: bool
    feedstocks: tuple[str, ...]
    detail: str


def _conda_name(name: str) -> str:
    """Return a package name as conda-forge's index spells it, or refuse it.

    Args:
        name: The package's canonical name.

    Returns:
        The name stripped and lower-cased.

    Raises:
        ResolutionLocatorError: When the name is not a string, is blank, or is
            not a name conda-forge could hold a package under once lower-cased --
            which is also what refuses `.` and `..`, neither of which may begin a
            name and neither of which percent-encoding touches.

    """
    if not isinstance(name, str) or not name.strip():
        message = (
            f"a resolution cannot be attempted for canonical_name={name!r}: this package is called nothing, so "
            f"there is nothing to look up on either source (CPM-FR-1)."
        )
        raise ResolutionLocatorError(message)
    segment = name.strip().lower()
    if not _NAME_SEGMENT.match(segment):
        message = (
            f"{name!r} is {segment!r} once lower-cased, which is not a name conda-forge's index could hold a "
            f"package under. Refused rather than encoded: a locator built from it would ask about nothing, or "
            f"would be a path the source is entitled to resolve somewhere else."
        )
        raise ResolutionLocatorError(message)
    return segment


def index_locator(name: str) -> str:
    """Return the locator naming a package's entry in conda-forge's feedstock-outputs index.

    Pure: no database, no clock, no network. The index is sharded by the first
    three alphanumeric characters of the name, padded with `z` when there are
    fewer, and the file is named after the package itself.

    Args:
        name: The package's canonical name.

    Returns:
        `https://raw.githubusercontent.com/conda-forge/feedstock-outputs/main/outputs/<a>/<b>/<c>/<name>.json`.

    Raises:
        ResolutionLocatorError: On every refusal `_conda_name` makes, and when
            the locator is wider than the `source` column that has to hold it.

    """
    conda = _conda_name(name)
    shard = [character for character in conda if character.isalnum()][:SHARD_LENGTH]
    shard += [SHARD_FILL] * (SHARD_LENGTH - len(shard))
    locator = (
        f"https://{FEEDSTOCK_OUTPUTS_HOST}/{CONDA_FORGE_ORG}/{FEEDSTOCK_OUTPUTS_REPOSITORY}/"
        f"{FEEDSTOCK_OUTPUTS_BRANCH}/outputs/{'/'.join(shard)}/{conda}.json"
    )
    width = _column_width("source")
    if width is not None and len(locator) > width:
        message = (
            f"{name!r} builds a locator of {len(locator)} characters, and the source column that records it "
            f"takes {width}. Refused rather than truncated: a row that cannot say where its observation came "
            f"from is a row an append-only history cannot tell from its neighbours."
        )
        raise ResolutionLocatorError(message)
    return locator


def normalised_name(name: str) -> str:
    """Return the PEP 503-normalised project name PyPI would hold a package under, or refuse it.

    Pure. Runs of `-`, `_` and `.` collapse to one hyphen and the result is
    lower-cased, which is what PyPI itself does -- so `Zope.Interface`,
    `zope_interface` and `zope-interface` reach one locator. The same rule
    `collectors/pypi_release.py` applies to a stored purl, restated because no
    collector imports another (`CPM-AD-7`); the unit tier reconciles the two.

    Args:
        name: The package's canonical name.

    Returns:
        The normalised name.

    Raises:
        ResolutionLocatorError: When the name is not a string, is blank, or is
            not a name PyPI could hold a project under once normalised -- a name
            beginning with an underscore, say, which conda permits and PyPI does
            not.

    """
    if not isinstance(name, str) or not name.strip():
        message = f"a PyPI project cannot be located for canonical_name={name!r}: this package is called nothing."
        raise ResolutionLocatorError(message)
    normalised = _SEPARATORS.sub("-", name.strip()).lower()
    if not _NORMALISED_NAME.match(normalised):
        message = (
            f"{name!r} is {normalised!r} once PEP 503 has normalised it, which is not a name PyPI could hold a "
            f"project under. Refused rather than encoded: a locator built from it would ask about nothing."
        )
        raise ResolutionLocatorError(message)
    return normalised


def project_locator(name: str) -> str:
    """Return the locator naming a PyPI project's JSON document.

    Args:
        name: The package's canonical name.

    Returns:
        `https://pypi.org/pypi/<name>/json`, with the name normalised.

    Raises:
        ResolutionLocatorError: On every refusal `normalised_name` makes.

    """
    return f"https://{PYPI_HOST}/pypi/{normalised_name(name)}/json"


def pypi_purl(name: str) -> str:
    """Return the package URL naming a package's PyPI project.

    Args:
        name: The package's canonical name.

    Returns:
        `pkg:pypi/<name>`, with the name normalised.

    Raises:
        ResolutionLocatorError: On every refusal `normalised_name` makes.

    """
    return f"{PYPI_PURL_PREFIX}{normalised_name(name)}"


def conda_purl(name: str) -> str:
    """Return the package URL naming a package as a conda artifact.

    Args:
        name: The package's canonical name.

    Returns:
        `pkg:conda/<name>`, lower-cased.

    Raises:
        ResolutionLocatorError: On every refusal `_conda_name` makes.

    """
    return f"{CONDA_PURL_PREFIX}{_conda_name(name)}"


def _repository_fault(url: object) -> str:  # noqa: PLR0911 - one return per reason a URL is refused; see below
    """Return why a `project_urls` value is not a readable repository URL, or nothing.

    The rule behind `normalised_repository`, kept as the sentence a row records
    rather than as a boolean, because the rejected URL and the reason are what
    the matrix asks `detail` to carry. Eight returns, one per reason, and that
    is what the `noqa` licenses: each is a different sentence about a different
    defect, and collapsing two would put one reason on a row about the other.

    Args:
        url: The value, as the document spelled it.

    Returns:
        The reason, or the empty string when `normalised_repository` will answer.

    """
    if not isinstance(url, str) or not url.strip():
        return f"{url!r} names no URL"
    stripped = url.strip()
    try:
        parts = urlsplit(stripped)
        host = (parts.hostname or "").lower()
        _ = parts.port
    except ValueError as malformed:
        return f"{stripped!r} cannot be read as a URL: {type(malformed).__name__}: {malformed}"
    if parts.scheme.lower() not in {"http", "https"}:
        return f"{stripped!r} names the scheme {parts.scheme!r}, and a repository URL is read over http or https"
    if host not in GITHUB_WEB_HOSTS:
        return (
            f"{stripped!r} is hosted at {host or '(no host)'!r}, and only {sorted(GITHUB_WEB_HOSTS)} is a host "
            f"the upstream-release collector reads"
        )
    segments = [segment.lower() for segment in parts.path.split("/") if segment]
    for position, segment in enumerate(segments):
        if segment in _TAIL_SEGMENTS and position == _REPOSITORY_SEGMENTS:
            segments = segments[:position]
            break
    if len(segments) != _REPOSITORY_SEGMENTS:
        return f"{stripped!r} has the path {parts.path!r}, which is not an owner and a repository"
    owner, repository = segments
    repository = repository.removesuffix(_GIT_SUFFIX)
    if not _NAME_SEGMENT.match(owner) or not _NAME_SEGMENT.match(repository):
        return f"{stripped!r} names {owner!r}/{repository!r}, which is not an owner and a repository GitHub could hold"
    candidate = f"https://{GITHUB_WEB_HOST}/{owner}/{repository}"
    width = _column_width("repository_url")
    if width is not None and len(candidate) > width:
        return f"{stripped!r} normalises to {len(candidate)} characters, and the column that holds it takes {width}"
    return ""


def normalised_repository(url: object) -> str | None:
    """Return a `project_urls` value as the repository URL this product reads, or `None`.

    Pure. Accepts `http` or `https`, `github.com` or `www.github.com`, exactly an
    owner and a repository -- with a `/tree/...` or `/blob/...` tail, a `.git`
    suffix, a trailing slash, a query or a fragment stripped -- and answers
    `https://github.com/<owner>/<repo>`, lower-cased: the one form
    `collectors/source_release.py`'s `_repository_segments` accepts and the one
    spelling an append-only history should hold for one repository.

    Args:
        url: The value, as the document spelled it.

    Returns:
        The normalised URL, or `None` when the value is not a repository URL this
        product reads. `_repository_fault` says why.

    """
    if _repository_fault(url):
        return None
    parts = urlsplit(str(url).strip())
    segments = [segment.lower() for segment in parts.path.split("/") if segment][:_REPOSITORY_SEGMENTS]
    owner, repository = segments
    return f"https://{GITHUB_WEB_HOST}/{owner}/{repository.removesuffix(_GIT_SUFFIX)}"


def normalised_label(key: str) -> str:
    """Return a `project_urls` key as the label PyPI compares it by (PEP 753).

    Pure. Lower-cased, with every character that is not a letter or a digit
    removed, so `Source Code`, `source-code` and `Source_Code` are one label.

    Args:
        key: The key, as the document spelled it.

    Returns:
        The normalised label; empty for a key with no letter or digit in it.

    """
    return _NOT_LABEL.sub("", key.lower())


def _issues_repository(url: object) -> str | None:
    """Return the repository an issue-tracker link names, or `None`.

    Pure. Only `github.com/<owner>/<repo>/issues` counts: the `issues` segment
    is what says the link is the repository's own tracker rather than some
    other page under the owner.

    Args:
        url: The value, as the document spelled it.

    Returns:
        The normalised repository URL, or `None`.

    """
    if not isinstance(url, str):
        return None
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return None
    segments = [segment for segment in parts.path.split("/") if segment]
    if len(segments) != _REPOSITORY_SEGMENTS + 1 or segments[-1].lower() != _ISSUES_SEGMENT:
        return None
    return normalised_repository(urlunsplit((parts.scheme, parts.netloc, "/".join(segments[:-1]), "", "")))


def _tracker_repository(by_key: Mapping[str, tuple[str, str]]) -> ChosenRepository | None:
    """Return the repository the first well-known issue-tracker label names, or `None`.

    Args:
        by_key: The document's links, keyed by normalised label.

    Returns:
        The choice, or `None` when no tracker label is present or none names a
        repository's own GitHub issues page.

    """
    for wanted in ISSUES_KEYS:
        entry = by_key.get(normalised_label(wanted))
        if entry is None:
            continue
        key, url = entry
        normalised = _issues_repository(url)
        if normalised is not None:
            return ChosenRepository(key=key.strip(), url=url.strip(), normalised=normalised)
    return None


def repository_from(project_urls: Mapping[str, str]) -> ChosenRepository:
    """Choose the source repository a project's `project_urls` names, by the documented precedence.

    Pure. See the module docstring for the rule: the first key of
    `REPOSITORY_PRECEDENCE` that is present wins, readable or not; `Homepage` is
    consulted only when none of them is, and only counts when it normalises;
    one of `ISSUES_KEYS` is consulted last, and only counts when it is the
    repository's own GitHub tracker. Keys are matched on their PEP 753
    normalised label, and when two of the document's keys collapse to one that
    way the first in document order is the one read.

    Args:
        project_urls: The document's `project_urls`, as `project_urls_in` read
            them.

    Returns:
        What was chosen, and why nothing usable was when nothing was.

    """
    by_key: dict[str, tuple[str, str]] = {}
    for key, value in project_urls.items():
        # First occurrence in document order wins when two keys collapse to one
        # after stripping and lower-casing, so the choice is deterministic.
        by_key.setdefault(normalised_label(key), (key, value))
    for wanted in REPOSITORY_PRECEDENCE:
        entry = by_key.get(normalised_label(wanted))
        if entry is None:
            continue
        key, url = entry
        normalised = normalised_repository(url)
        if normalised is None:
            return ChosenRepository(
                key=key.strip(),
                url=url.strip(),
                detail=(
                    f"{NO_REPOSITORY_DETAIL}: the {key.strip()!r} link was rejected because {_repository_fault(url)}"
                ),
            )
        return ChosenRepository(key=key.strip(), url=url.strip(), normalised=normalised)
    homepage = by_key.get(normalised_label(HOMEPAGE_KEY))
    if homepage is not None:
        key, url = homepage
        normalised = normalised_repository(url)
        if normalised is not None:
            return ChosenRepository(key=key.strip(), url=url.strip(), normalised=normalised)
    tracker = _tracker_repository(by_key)
    if tracker is not None:
        return tracker
    if homepage is not None:
        key, url = homepage
        return ChosenRepository(
            detail=(
                f"{NO_REPOSITORY_DETAIL}: the project labels none of {list(REPOSITORY_PRECEDENCE)}, its "
                f"{key.strip()!r} link is not a GitHub repository because {_repository_fault(url)}, and no "
                f"issue tracker names one"
            ),
        )
    return ChosenRepository(
        detail=(
            f"{NO_REPOSITORY_DETAIL}: the project labels none of {list(REPOSITORY_PRECEDENCE)}, no homepage, and "
            f"no issue tracker names one"
        ),
    )


def feedstocks_in(body: str, *, source: str) -> tuple[str, ...]:
    """Read one feedstock-outputs index entry into the feedstock names it lists.

    Pure. The entry is `{"feedstocks": ["<name>", ...]}`; each name is the
    feedstock's name without its `-feedstock` suffix, which is how conda-forge
    writes the index. Names are stripped and lower-cased, and a name listed twice
    is listed once, because the recorder refuses two feedstocks sharing a name.

    Args:
        body: The document the source served.
        source: The locator it was served from, for the messages.

    Returns:
        The names, in the order the index listed them, with the first spelling
        kept when two name one repository -- `x` and `x-feedstock` are one
        feedstock, and two mappings sharing a name is a recorder refusal. Empty
        for an index entry that lists none, which is an answer rather than a
        refusal.

    Raises:
        ResolutionDocumentError: When the body is too long, is not JSON, is not an
            object, has no list under `feedstocks` or a list holding something
            that is not a non-blank string, or names a feedstock whose repository
            name is not one GitHub could hold or is wider than the column
            `identity` records it in.

    """
    document = _document_in(body, source=source)
    listed = document.get(FEEDSTOCKS_FIELD)
    if not isinstance(listed, list):
        message = (
            f"{source} served a document whose {FEEDSTOCKS_FIELD!r} is {type(listed).__name__} rather than a "
            f"list. A source whose shape has changed is refused rather than read for whatever still parses."
        )
        raise ResolutionDocumentError(message)
    names: list[str] = []
    repositories: list[str] = []
    for entry in listed:
        if not isinstance(entry, str) or not entry.strip():
            message = (
                f"{source} lists {entry!r} as a feedstock, which is not a name. A source whose shape has changed "
                f"is refused rather than read past."
            )
            raise ResolutionDocumentError(message)
        name = entry.strip().lower()
        if not _NAME_SEGMENT.match(name):
            message = (
                f"{source} lists {entry!r} as a feedstock, which is not a repository name GitHub could hold once "
                f"lower-cased. Refused rather than recorded: a Feedstock row naming it would be a row the "
                f"feedstock collector refuses to ask about."
            )
            raise ResolutionDocumentError(message)
        repository = _feedstock_repository(name)
        if len(repository) > FEEDSTOCK_NAME_LENGTH:
            message = (
                f"{source} lists {entry!r}, whose repository {repository!r} is {len(repository)} characters, and "
                f"the feedstock name column takes {FEEDSTOCK_NAME_LENGTH}. Refused rather than truncated."
            )
            raise ResolutionDocumentError(message)
        if repository not in repositories:
            repositories.append(repository)
            names.append(name)
    return tuple(names)


def project_urls_in(body: str, *, source: str) -> Mapping[str, str]:
    """Read one PyPI project document into its `project_urls`.

    Pure. Only `info.project_urls` is read: the rest of the document is the
    release collector's business (`CPM-AD-7`). A project that declares no
    `project_urls`, or declares `null`, has declared none.

    Args:
        body: The document the source served.
        source: The locator it was served from, for the messages.

    Returns:
        The label-to-URL mapping, with blank and non-string values dropped and
        keys stripped -- the first of two keys that strip to one spelling wins,
        in document order. Empty when the project declares none.

    Raises:
        ResolutionDocumentError: When the body is too long, is not JSON, is not an
            object, has an `info` that is not an object, or a `project_urls` that
            is present, not null and not an object.

    """
    document = _document_in(body, source=source)
    info = document.get(INFO_FIELD)
    if not isinstance(info, dict):
        message = (
            f"{source} served a document whose {INFO_FIELD!r} is {type(info).__name__} rather than an object. A "
            f"source whose shape has changed is refused rather than read for whatever still parses."
        )
        raise ResolutionDocumentError(message)
    urls = info.get(PROJECT_URLS_FIELD)
    if urls is None:
        return MappingProxyType({})
    if not isinstance(urls, dict):
        message = (
            f"{source} served a document whose {PROJECT_URLS_FIELD!r} is {type(urls).__name__} rather than an "
            f"object. A source whose shape has changed is refused rather than read past."
        )
        raise ResolutionDocumentError(message)
    read: dict[str, str] = {}
    for key, value in urls.items():
        if isinstance(value, str) and value.strip() and str(key).strip():
            read.setdefault(str(key).strip(), value.strip())
    return MappingProxyType(read)


def resolution_for(
    *,
    identity: PackageIdentity,
    feedstocks: Sequence[str],
    project: ProjectAnswer,
    chosen: ChosenRepository,
    prior_feedstock_rows: bool,
) -> ResolvedFacts:
    """Decide what the two documents established, as the resolution to record.

    Pure. Every `MappingKind` is answered:

    - `source_repository` is `established` when a repository was chosen and
      normalised, `not_found` when PyPI answered and none was, and when PyPI
      could not be asked, whatever the package currently holds: `established`
      with the stored value if an earlier run established one, `error`
      otherwise.
    - `release_ecosystem` is `established` when PyPI holds the project,
      `not_found` when it answered that it does not, and on the same
      carry-forward terms when it could not be asked.
    - `feedstock` and `conda_artifact` are `established` when the index names a
      feedstock, or when the package already holds `Feedstock` rows the index has
      gone silent on -- the recorder never removes one -- and `not_found` for a
      package with neither.
    - `cross_ecosystem` is `not_found`: nothing here derives a second purl or a
      CPE.

    The confidence claimed is `inventory-derived` when any mapping owning a value
    was established and `unmapped` otherwise; the recorder refuses a confidence
    nothing earned, and claiming `unmapped` is that rule stated where the
    resolution is built rather than met as a refusal.

    Args:
        identity: What the package row says.
        feedstocks: What the index listed, as `feedstocks_in` read it.
        project: What the bounded second call came back with.
        chosen: Which repository was chosen from the project's URLs.
        prior_feedstock_rows: Whether the package already holds `Feedstock` rows.

    Returns:
        The resolution and the facts the snapshot row keeps beside it.

    """
    sentences: list[str] = []
    if project.detail:
        sentences.append(project.detail)
    elif chosen.detail:
        sentences.append(chosen.detail)

    # A transient failure of the second call must never lower what an earlier
    # run established: the recorder rewrites the outcome row on every call, so
    # `error` here would take a package out of the two PyPI-selecting sweeps
    # while its row still held the finding. When PyPI could not be asked, each
    # of the two kinds re-asserts what the package currently holds.
    if project.asked:
        repository_url = chosen.normalised
        repository_outcome = ESTABLISHED if repository_url else OutcomeState.NOT_FOUND.value
        release_outcome = ESTABLISHED if project.found else OutcomeState.NOT_FOUND.value
        primary_purl = pypi_purl(identity.canonical_name) if project.found else ""
        primary_type = PYPI_PURL_TYPE if project.found else ""
    else:
        source_kept = identity.source_outcome == ESTABLISHED and bool(identity.source_repository_url)
        repository_url = identity.source_repository_url if source_kept else ""
        repository_outcome = ESTABLISHED if source_kept else OutcomeState.ERROR.value
        release_kept = identity.release_outcome == ESTABLISHED and bool(identity.primary_purl)
        release_outcome = ESTABLISHED if release_kept else OutcomeState.ERROR.value
        primary_purl = identity.primary_purl if release_kept else ""
        primary_type = identity.primary_type if release_kept else ""
        if source_kept or release_kept:
            sentences.append(CARRIED_FORWARD_DETAIL)

    builds = bool(feedstocks) or prior_feedstock_rows
    feedstock_outcome = ESTABLISHED if builds else OutcomeState.NOT_FOUND.value
    if not feedstocks:
        sentences.append(f"{INDEX_LISTS_NONE_DETAIL}; {PRIOR_ROWS_KEPT_DETAIL}" if builds else INDEX_LISTS_NONE_DETAIL)
    mappings = tuple(
        FeedstockMapping(
            name=_feedstock_repository(name),
            url=f"https://{GITHUB_WEB_HOST}/{CONDA_FORGE_ORG}/{_feedstock_repository(name)}",
        )
        for name in feedstocks
    )

    established = repository_url or project.found or builds
    confidence = IdentityConfidence.INVENTORY_DERIVED.value if established else IdentityConfidence.UNMAPPED.value
    resolution = Resolution(
        identity_source=identity.identity_source,
        associator_key=identity.associator_key,
        confidence=confidence,
        outcomes={
            MappingKind.SOURCE_REPOSITORY.value: repository_outcome,
            MappingKind.RELEASE_ECOSYSTEM.value: release_outcome,
            MappingKind.CONDA_ARTIFACT.value: feedstock_outcome,
            MappingKind.FEEDSTOCK.value: feedstock_outcome,
            MappingKind.CROSS_ECOSYSTEM.value: OutcomeState.NOT_FOUND.value,
        },
        source_repository_url=repository_url,
        primary_purl=primary_purl,
        primary_type=primary_type,
        conda_purl=conda_purl(identity.canonical_name) if builds else "",
        feedstocks=mappings,
    )
    return ResolvedFacts(
        resolution=resolution,
        repository_url=repository_url,
        repository_key=chosen.key,
        pypi_found=project.found,
        feedstocks=tuple(feedstocks),
        detail="; ".join(sentences),
    )


def _feedstock_repository(name: str) -> str:
    """Return the repository a feedstock name addresses, suffixed exactly once.

    Args:
        name: The name, already stripped and lower-cased.

    Returns:
        The repository segment.

    """
    return name if name.endswith(FEEDSTOCK_SUFFIX) else f"{name}{FEEDSTOCK_SUFFIX}"


def _document_in(body: str, *, source: str) -> dict[str, object]:
    """Decode a document and refuse anything that is not an object.

    Args:
        body: The document the source served.
        source: The locator it was served from, for the messages.

    Returns:
        The decoded object.

    Raises:
        ResolutionDocumentError: When the body is too long to decode, is not
            JSON, or is not an object.

    """
    if len(body) > MAX_DOCUMENT_CHARACTERS:
        message = (
            f"{source} served {len(body)} characters, and this collector decodes at most "
            f"{MAX_DOCUMENT_CHARACTERS}. A document this size is a source doing something else, and parsing it "
            f"would spend a worker's soft time limit finding out (CPM-AD-9)."
        )
        raise ResolutionDocumentError(message)
    try:
        document = json.loads(body)
    except (json.JSONDecodeError, RecursionError) as unreadable:
        message = (
            f"{source} did not serve a readable document: {type(unreadable).__name__}: {unreadable}. The "
            f"observation is refused rather than recorded empty, which would resolve the package to nothing "
            f"(CPM-FR-1)."
        )
        raise ResolutionDocumentError(message) from unreadable
    if not isinstance(document, dict):
        message = (
            f"{source} served {type(document).__name__} rather than an object. A source whose shape has changed "
            f"is refused rather than read for whatever still parses."
        )
        raise ResolutionDocumentError(message)
    return document


def _column_width(field: str) -> int | None:
    """Return how wide one of the evidence table's text columns is.

    Args:
        field: The column's name.

    Returns:
        Its `max_length`, or `None` for a column that declares none.

    """
    column = IdentityResolutionSnapshot._meta.get_field(field)  # noqa: SLF001 - Django's own public-by-convention API
    return column.max_length if isinstance(column, models.CharField) else None


class IdentityResolutionCollector(Collector):
    """The collector that resolves a package's mappings. Writes `identity_resolution_snapshots`.

    Three hooks and nine declarations. See the module docstring for why the
    index is the first call, why the second never fails the run, and why the
    only write to identity is the recorder's.
    """

    name: ClassVar[str] = COLLECTOR_NAME

    evidence_model: ClassVar[type[AppendOnlyModel] | None] = IdentityResolutionSnapshot

    observation_window: ClassVar[timedelta | None] = RESOLUTION_OBSERVATION_WINDOW

    timeout: ClassVar[float | None] = RESOLUTION_TIMEOUT

    retries: ClassVar[int] = RESOLUTION_RETRIES

    rate_limit: ClassVar[RateLimit] = RESOLUTION_RATE_LIMIT

    headers: ClassVar[Mapping[str, str]] = RESOLUTION_HEADERS

    freshness_target: ClassVar[timedelta | None] = RESOLUTION_FRESHNESS_TARGET

    response_cache_ttl: ClassVar[timedelta | None] = RESOLUTION_CACHE_TTL

    #: How often the full-inventory sweep dispatches this collector, reconciled
    #: against its `CELERY_BEAT_SCHEDULE` entry at boot (`CPM-CURRENCY-S05`).
    cadence: ClassVar[timedelta | None] = RESOLUTION_CADENCE

    #: The identity read, remembered on the instance for the run in progress and
    #: keyed by package, on the terms the feedstock collector states: the base
    #: asks `source_for` and then `translate` about one package in one run, and
    #: the recorder must be handed the pair the locator was built for.
    _identity: PackageIdentity | None = None
    _identity_package: int | None = None

    #: The locator this run asked for, remembered when `source_for` answered,
    #: because `sentinel_evidence` records it and is not handed it.
    _locator: str = ""

    @classmethod
    def selectable_packages(cls) -> Iterable[int]:
        """Return the packages this collector can be asked about: every one nobody has verified.

        The complement of `collectors/selection.py`'s `RESOLVED_CONFIDENCES`,
        as a keyword to `exclude` rather than a comparison: a `verified`
        identity is a person's and an automated resolution may not lower it, so
        it is never even offered a claim to hold back. Everything else --
        `unmapped`, `inventory-derived`, and any value the enum does not declare
        -- is a package this collector may re-resolve.

        A package filed under a blank `identity_source` or `associator_key` is
        excluded too: the recorder finds a package by that pair and refuses a
        blank half, so offering such a shell would fail it at `source_for` on
        every sweep for a reason no sweep can change.

        Returns:
            The primary keys, as a lazy queryset ordered by key -- streamed by
            `collectors/sweep.py` rather than materialised.

        """
        return (
            Package.objects.exclude(confidence__in=RESOLVED_CONFIDENCES)
            .exclude(identity_source="")
            .exclude(associator_key="")
            .order_by("pk")
            .values_list("pk", flat=True)
        )

    def inapplicability(self, *, package_id: int) -> str:
        """Say nothing: the question applies to every package.

        Overridden only to forget the previous run's identity and locator, so an
        instance that collects twice reads fresh each time.

        Args:
            package_id: The package being collected.

        Returns:
            The empty string, always.

        """
        self._locator = ""
        self._identity = None
        self._identity_package = None
        return ""

    def source_for(self, *, package_id: int) -> str:
        """Return the index locator for one package, and remember the pair the recorder needs.

        Args:
            package_id: The package being collected.

        Returns:
            The feedstock-outputs index locator. Remembered on the instance as
            well as returned -- see `_locator`.

        Raises:
            ResolutionLocatorError: When the package has no row; when its
                `identity_source` or `associator_key` is blank, so the recorder
                could not find it again; or when its name cannot be turned into
                an index locator.

        """
        identity = self._package_identity(package_id)
        if identity is None:
            message = (
                f"package {package_id} has no identity row, so there is nothing to resolve. A package row is "
                f"created by ingestion (CPM-AD-25); this collector resolves one and never creates one."
            )
            raise ResolutionLocatorError(message)
        if not identity.identity_source.strip() or not identity.associator_key.strip():
            message = (
                f"package {package_id} is filed under identity_source={identity.identity_source!r} and "
                f"associator_key={identity.associator_key!r}, and record_resolution finds a package by that pair "
                f"(CPM-FR-2). A package no source claims cannot be resolved through the one door CPM-AD-14 leaves "
                f"open, so the run is refused rather than the package written around it."
            )
            raise ResolutionLocatorError(message)
        self._locator = index_locator(identity.canonical_name)
        return self._locator

    def translate(self, payload: Payload, *, package_id: int, observed_at: datetime) -> Sequence[AppendOnlyModel]:
        """Turn the index entry, plus the bounded second call, into one recorded resolution and one row.

        Args:
            payload: What the index said, recorded. Reached only for an entry the
                source served -- absence and failure are the base's to record.
            package_id: The package the observation is about.
            observed_at: The instant to stamp the row with, from the injected
                clock.

        Returns:
            One unsaved `IdentityResolutionSnapshot`.

        Raises:
            ResolutionDocumentError: When the index entry cannot be read. The
                base writes an `error` row and re-raises; nothing is recorded on
                the package.
            ResolutionLocatorError: When the package has no row to record
                against -- unreachable through `collect()`, which asked
                `source_for` first.
            ResolutionError: When the recorder refuses what it was handed. The
                transaction is rolled back, the base writes an `error` row and
                re-raises.

        """
        identity = self._package_identity(package_id)
        if identity is None:
            message = f"package {package_id} has no identity row to record a resolution against."
            raise ResolutionLocatorError(message)
        source = self._locator or index_locator(identity.canonical_name)
        feedstocks = feedstocks_in(payload.body, source=source)
        project = self._project_instead(name=identity.canonical_name)
        chosen = repository_from(project.project_urls) if project.found else ChosenRepository()
        facts = resolution_for(
            identity=identity,
            feedstocks=feedstocks,
            project=project,
            chosen=chosen,
            prior_feedstock_rows=Feedstock.objects.filter(package_id=package_id).exists(),
        )
        # One transaction per package, around the recorder's write and the row
        # that describes it (CPM-AD-23). Nested inside the base's run recorder
        # and never around it. `collection_run` opens no transaction, so in
        # production this block and the base's own block around the insert are
        # two independently committed transactions -- the resolution lands
        # first, the snapshot second -- which is the bound the module docstring
        # states.
        with transaction.atomic():
            recorded = record_resolution(resolution=facts.resolution, clock=self._clock)
            row = IdentityResolutionSnapshot(
                observed_at=observed_at,
                package_id=package_id,
                source=source,
                state=OutcomeState.OK.value,
                repository_url=facts.repository_url,
                repository_key=facts.repository_key,
                pypi_asked=project.asked,
                pypi_found=facts.pypi_found,
                pypi_source=project.source,
                feedstocks=list(facts.feedstocks),
                confidence_recorded=recorded.package.confidence,
                downgrade_refused=recorded.downgrade_refused,
                detail=facts.detail,
                trace_id=current_trace_id(),
            )
        return [row]

    def _project_instead(self, *, name: str) -> ProjectAnswer:
        """Read the package's PyPI project document, for a package the index has already answered about.

        **The bounded second call**, bounded three ways: one locator, one page,
        and a failure of it never fails the collection -- the index's answer
        stands, with the reason in `detail`. Nothing here raises, and that is
        the invariant rather than an omission.

        Args:
            name: The package's canonical name.

        Returns:
            The answer: the project's URLs when it answered readably, an absence
            when PyPI said there is no such project, and otherwise the reason
            nobody could ask.

        """
        try:
            locator = project_locator(name)
        except ResolutionLocatorError as unnameable:
            return ProjectAnswer(
                asked=True,
                found=False,
                project_urls=MappingProxyType({}),
                source="",
                detail=f"{NO_PYPI_PROJECT_DETAIL}: {unnameable}",
            )
        try:
            payload = self._transport.fetch(locator, headers=request_headers(declared=self._headers, entry=None))
        except TransportError as failure:
            return ProjectAnswer(
                asked=False,
                found=False,
                project_urls=MappingProxyType({}),
                source=locator,
                detail=f"{PYPI_UNREADABLE_DETAIL}: {locator} could not be read: {failure}",
            )
        if payload.not_modified:
            return ProjectAnswer(
                asked=False,
                found=False,
                project_urls=MappingProxyType({}),
                source=locator,
                detail=(
                    f"{PYPI_UNREADABLE_DETAIL}: {locator} answered that nothing had changed, to an unconditional "
                    f"request"
                ),
            )
        if not payload.found:
            return ProjectAnswer(
                asked=True,
                found=False,
                project_urls=MappingProxyType({}),
                source=locator,
                detail=f"{NO_PYPI_PROJECT_DETAIL}: {locator} reports that the project does not exist",
            )
        try:
            urls = project_urls_in(payload.body, source=locator)
        except ResolutionDocumentError as unreadable:
            return ProjectAnswer(
                asked=False,
                found=False,
                project_urls=MappingProxyType({}),
                source=locator,
                detail=f"{PYPI_UNREADABLE_DETAIL}: {unreadable}",
            )
        return ProjectAnswer(asked=True, found=True, project_urls=urls, source=locator, detail="")

    def sentinel_evidence(
        self,
        *,
        state: OutcomeState,
        package_id: int,
        observed_at: datetime,
        detail: str,
    ) -> AppendOnlyModel:
        """Return one row carrying the sentinel the base decided on.

        Every resolved fact is absent, and nothing was recorded on the package:
        a sentinel row is written for a call that produced no document, and the
        recorder was never reached.

        Args:
            state: `OutcomeState.ERROR`, `OutcomeState.NOT_FOUND` or
                `OutcomeState.NOT_APPLICABLE`, decided by the base.
            package_id: The package the observation is about.
            observed_at: The instant to stamp the row with.
            detail: What happened, in words worth storing beside the state.

        Returns:
            One unsaved `IdentityResolutionSnapshot` carrying the state's value
            verbatim in `state` (`CPM-AD-24`).

        Raises:
            CollectorConfigurationError: When asked for a state this collector
                has no row shape for -- `ok` or `unknown`.

        """
        if (
            state is not OutcomeState.ERROR
            and state is not OutcomeState.NOT_FOUND
            and state is not OutcomeState.NOT_APPLICABLE
        ):
            message = (
                f"{type(self).__name__}.sentinel_evidence was asked for {state.value!r}, and this collector "
                f"shapes a sentinel row for {OutcomeState.ERROR.value!r}, {OutcomeState.NOT_FOUND.value!r} and "
                f"{OutcomeState.NOT_APPLICABLE.value!r} only. A row carrying any other state would record no "
                f"confidence and would be refused by identity_resolution_snapshots' own constraint at insert."
            )
            raise CollectorConfigurationError(message)
        return IdentityResolutionSnapshot(
            observed_at=observed_at,
            package_id=package_id,
            source=self._locator,
            state=state.value,
            repository_url="",
            repository_key="",
            pypi_asked=False,
            pypi_found=False,
            pypi_source="",
            feedstocks=[],
            confidence_recorded="",
            downgrade_refused=False,
            detail=self._sentinel_detail(state, detail),
            trace_id=current_trace_id(),
        )

    def _sentinel_detail(self, state: OutcomeState, detail: str) -> str:
        """Return what a sentinel row records beside the base's own reason.

        Args:
            state: The sentinel the base decided on.
            detail: The base's reason.

        Returns:
            The reason, with the `not_found` caveat appended: the locator was
            the index, so what is absent is the package's conda-forge entry, and
            PyPI was never asked -- the trade the module docstring records.

        """
        if state is not OutcomeState.NOT_FOUND:
            return detail
        return (
            f"{detail} -- conda-forge's feedstock-outputs index has no entry for this package, so PyPI was not "
            f"asked and nothing was recorded on the package; it is offered again next cadence"
        )

    def _package_identity(self, package_id: int) -> PackageIdentity | None:
        """Return what the package row says, read once per run.

        The only database reads of identity in this module besides the
        `Feedstock` existence check, and they read `identity` -- the one
        application a collector may read (`CPM-AD-7`). The two mapping outcomes
        are read beside the row so that a run on which PyPI could not be asked
        can re-assert what an earlier run established.

        Args:
            package_id: The package being collected.

        Returns:
            The identity, or `None` when no row exists.

        """
        if self._identity_package != package_id:
            recorded = (
                Package.objects.filter(pk=package_id)
                .values_list(
                    "canonical_name",
                    "identity_source",
                    "associator_key",
                    "primary_purl",
                    "primary_type",
                    "source_repository_url",
                )
                .first()
            )
            if recorded is None:
                self._identity = None
            else:
                name, source, key, purl, purl_type, repository = recorded
                outcomes = dict(
                    PackageMapping.objects.filter(
                        package_id=package_id,
                        kind__in=(MappingKind.RELEASE_ECOSYSTEM.value, MappingKind.SOURCE_REPOSITORY.value),
                    ).values_list("kind", "outcome"),
                )
                self._identity = PackageIdentity(
                    canonical_name=name,
                    identity_source=source,
                    associator_key=key,
                    release_outcome=outcomes.get(MappingKind.RELEASE_ECOSYSTEM.value, ""),
                    source_outcome=outcomes.get(MappingKind.SOURCE_REPOSITORY.value, ""),
                    primary_purl=purl,
                    primary_type=purl_type,
                    source_repository_url=repository,
                )
            self._identity_package = package_id
        return self._identity
