"""A local inventory with evidence behind it, so the screens have something to render.

`seed_personas` gives a developer a way in. This gives them something to look at:
without it, a fresh checkout renders every status as `unknown` -- correct, and
useless for judging a screen, because the one thing every cell has in common is the
value it would have if the projection were broken.

**Nothing here fabricates a verdict.** That is the constraint the whole module is
shaped around. It writes *evidence* -- append-only rows, exactly as a collector would
(`CPM-AD-2`) -- and then runs the real policy engine over it. Every status on the
resulting screens was concluded by the pass that owns it, from the parameter file
that ships. A seeder that inserted rows into `package_health` directly would be
faster, would produce prettier screens, and would make the demo a picture of a
product rather than the product; `CPM-AD-10` forbids the application layer a write
path to a derived status, and a fixture that took one would be testing the templates
against data the engine cannot produce.

**Nothing here asserts what it never observed.** The first draft of this module
did: every identified package was `verified` with a repository under a fictional
GitHub owner, a feedstock asserted or denied by a hand-kept flag (conda-forge
disagreed for two of them), a conda build called `py312h0`, a Python-readiness
verdict and a verification log under an invented `demo://` scheme. A reviewer
who checked any of it found nothing, and learned to stop checking -- which is the
one habit the product exists to support. So the roster now holds only what has a
real source behind it: a name and a version pair, an OSV advisory, a CISA KEV
listing, and a licence. What the seeder writes is exactly that, and nothing more:
the advisory's finding (or the collector's own nothing-matched row) and a KEV
finding for every package; a licence finding only where the roster states a
licence; and a `PyPIReleaseSnapshot` only for a package the resolver has just
confirmed PyPI has a project for -- so `git` and `sqlite` carry no PyPI row, and
a package nothing declares a licence for carries no licence row rather than a
`not_found` claiming conda-forge was asked. No source release, no feedstock, no
conda build, no readiness, no verification, and no invented `error`.

**Identity is observed, not written.** `CPM-AD-14` gives governed reference data
exactly one write path and `CPM-AD-25` says a collector "never writes the package
table" -- it calls the resolution service, which creates the shell at `unmapped`.
So does this, and then it stops: every demo package is a shell, exactly as the
inventory collector leaves one, with no mapping rows and no confidence claimed.
What resolves them is the real `resolve_identity` collector, run inline for every
package before the evidence is written, so a fresh seed shows real repositories,
real feedstocks and honest `not_found`s on first paint -- and the two `internal-*`
packages are `unmapped` because conda-forge has no such package, not because a
field said so. The resolver's runs are real runs under its own name; the Coverage
screen showing them is the screen telling the truth. Everything else --
feedstock presence, upstream-release currency, Python readiness -- reads `unknown`
until the stack's own sweeps observe it, which is the product's real shape stated
on the screen rather than painted over.

**The resolver runs unmetered here and nowhere else.** Its allowance charges
`1 + retries` per collection, so a hundred inline collections through the shared
counter would refuse most of them. A seed is a one-off of two hundred requests to
two public hosts; the daily sweep keeps the courtesy allowance. A resolution that
does not conclude leaves that package `unmapped` beside its own snapshot and is
logged by name, and the seed carries on. The summary tells the two reasons apart:
`not_on_conda_forge` is the index answering that there is no such package -- the
normal outcome for the two `internal-*` names -- and `unreachable` is a source
that could not be asked or a document that could not be read. After three
consecutive `unreachable` results the resolver stops and the rest are counted
`unreachable` without being asked, because a laptop with no network should not
wait out a hundred timeouts. A package a person has set `verified` is never
offered to the resolver at all, and is counted `verified_kept`. The seed never
fails whole for any of it.

**It refuses outside a local run**, on the terms `seeding.py` sets: this writes
inventory and evidence, and a deployed component that ran it would have permanent,
replayable observations in a log nothing may update or delete.

**What it cannot show you, it says rather than leaves you to infer.** A parameter
the shipped file records as empty makes the column it drives inert whatever evidence
is behind it -- `license_rules = []` is one today, and `priority_rules` was until
`2026.09.4` recorded ten. So the sentence `SEEDED_EVENT` carries is *read from the
parameters at the version the run applied*, never written here: a fixed sentence
became false the moment a rule set was recorded, and a demo confidently explaining a
state it is no longer in is worse than a demo that explains nothing. See
`_unconfigured_at`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from typing import TYPE_CHECKING
from typing import Final

import structlog
from django.core.exceptions import ImproperlyConfigured
from django.db import transaction

from config.locality import is_local

if TYPE_CHECKING:
    from collections.abc import Sequence

    from conda_sentinel.core.clock import Clock
    from conda_sentinel.core.rate_limit import RateLimit
    from conda_sentinel.core.transport import Transport
    from conda_sentinel.identity.models import Package

__all__ = [
    "DEMO_COLLECTOR",
    "DEMO_PACKAGES",
    "RESOLUTION_ABANDONED_EVENT",
    "SEEDED_EVENT",
    "UNREACHABLE_STREAK_LIMIT",
    "UNRESOLVED_EVENT",
    "DemoPackage",
    "ResolutionCounts",
    "seed_demo_inventory",
]

logger: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

#: What a deployed component is told when it tries to run this.
#:
#: Stronger wording than the persona seeder's, because the consequence is worse and
#: is not undoable: personas are user rows somebody can delete, while this writes
#: append-only evidence. `CPM-AD-2` means an observation, once written, is permanent
#: and will be read by every replayed policy run for ever.
_DEPLOYED_REFUSAL: Final[str] = (
    "seed_demo_inventory writes inventory and evidence and must never run outside a local run. "
    "Evidence is append-only (CPM-AD-2): an observation cannot be deleted, and every replayed "
    "policy run would read it. Set COMPONENT_RUNTIME=local, which the dev pixi environment declares."
)

#: The collector name the seeder's own run is filed under.
#:
#: Deliberately not one of the registered collector names. The coverage screen
#: reads the run ledger by collector, and seeding runs under `vulnerability` would
#: report that collector as healthy on a machine where it has never made a request --
#: which is precisely the lie that screen exists to prevent. The resolver's runs are
#: the exception that proves it: they are filed under `resolve_identity` because
#: that collector really did run.
DEMO_COLLECTOR: Final[str] = "local-dev-demo-seed"

#: The SPDX operators that make a licence string an *expression* rather than an
#: identifier. `WITH` counts: `GPL-2.0-only WITH Classpath-exception-2.0` is one
#: expression, not one identifier, and the column's three values distinguish them.
_COMPOUND: Final[re.Pattern[str]] = re.compile(r"\s(?:OR|AND|WITH)\s")

#: The licence vocabulary's determinate value, spelled as a string.
#:
#: `core/outcomes.py`'s `outcome_type` composes each vocabulary per domain, which
#: means a member reference is invisible to the type checker -- the composed class is
#: a `TextChoices` with no statically known members. The value is asserted against
#: the model's declared choices in `tests/unit/test_local_dev_demo_data.py`, so a
#: renamed value fails there rather than at the first `IntegrityError` on somebody's
#: laptop.
_NORMALIZED: Final[str] = "normalized"

#: The event the seeder logs when it has finished.
SEEDED_EVENT: Final[str] = "local_dev.demo_inventory_seeded"

#: The event logged, once per package, for a resolution that did not conclude.
#:
#: At INFO when conda-forge answered that there is no such package, which is the
#: normal outcome for the two `internal-*` names; at WARNING when a source could
#: not be asked, a document could not be read, or the recorder refused.
UNRESOLVED_EVENT: Final[str] = "local_dev.demo_identity_unresolved"

#: The event logged once when the resolver gives up on the network.
RESOLUTION_ABANDONED_EVENT: Final[str] = "local_dev.demo_identity_resolution_abandoned"

#: How many `unreachable` results in a row make the resolver stop asking.
#:
#: Offline, every collection waits out the transport's timeout and retries before
#: it fails, and a hundred of those in a row is minutes of a seed doing nothing but
#: timing out. Three is enough to tell "the network is down" from one flaky answer.
UNREACHABLE_STREAK_LIMIT: Final[int] = 3


@dataclass(frozen=True, slots=True)
class DemoPackage:
    """One package to seed, and what its real sources state about it.

    Every field is an *observation* with a source a reader can check, never a
    verdict. There is no way to express "this package should come out P1" here, and
    there is no way to name a repository, a feedstock, a build or a readiness either:
    those are the resolver's and the sweeps' to observe, and a roster field for any
    of them was a fact nobody had checked.
    """

    name: str

    #: What the upstream source and the installed artifact are at. A package behind
    #: its upstream is the ordinary interesting case.
    upstream_version: str
    installed_version: str

    #: The advisory to record, if any: `(advisory_id, severity, affected_range)`.
    advisory: tuple[str, str, str] | None = None

    #: Whether the advisory is in the KEV catalogue. Only meaningful with one.
    kev_listed: bool = False

    #: The date CISA's catalogue states it added the advisory, as `YYYY-MM-DD`.
    #:
    #: Read off the catalogue rather than computed from the run, because the whole
    #: reason a KEV listing is worth showing is that it is checkable: a date derived
    #: from "thirty days before you ran the seeder" moves every time somebody reruns
    #: it and matches nothing anybody can look up. Blank on a listing that states no
    #: date -- which `kev_findings` treats as missing, not as absent.
    kev_catalogued: str = ""

    #: The licence the artifact declares, if the licence lookup found one.
    licence: str = ""


#: The inventory the demo seeds.
#:
#: **A hundred packages, and the number is the point.** The roster used to hold ten,
#: which was enough to put every tone the stylesheet draws on one screen and not
#: nearly enough to judge one: ten rows fit above the fold, sort instantly, paginate
#: never, and make a queue that holds two items look like a queue. A reviewer asking
#: "is this screen usable" was being shown a screen that could not be unusable.
#:
#: **A mixture, on the product owner's terms**: web frameworks, data science, and the
#: ordinary utilities every environment carries -- well known and less so, and seven
#: things conda-forge ships that are not Python at all.
#:
#: ### What is real here
#:
#: **The advisories are real.** Every `advisory=` below carries an identifier, a
#: severity and an affected range taken from **OSV.dev**, and the one KEV listing is a
#: real entry in **CISA's Known Exploited Vulnerabilities catalogue** with the date
#: the catalogue states. They were harvested rather than invented, at the product
#: owner's direction and against this module's first draft, which used names like
#: `GHSA-demo-high`. A reader who does not believe a row can look the identifier up,
#: which is the whole difference: a fictional advisory teaches a reviewer to stop
#: checking.
#:
#: Note what the KEV column then looks like: **one row out of twenty-eight**. That is
#: not a thin demo, it is the truth about that catalogue -- it lists software known to
#: be exploited in the wild, and almost nothing on PyPI is in it. The row that is
#: listed is `git`, which conda-forge ships and CISA catalogued on 2025-08-25.
#:
#: **The versions and licences are what the sources state**, read off PyPI and
#: conda-forge when the roster was written; on a package carrying an advisory the
#: upstream version is the advisory's own fixed version and the installed version is
#: a real release inside its affected range. **Everything else is observed at seed
#: time or not at all**: which repository a package lives in, whether conda-forge
#: has a feedstock for it and what its purls are come from the `resolve_identity`
#: collector reading the real index and the real project document, and nothing in
#: this table can say otherwise. `internal-telemetry-sdk` and `internal-feature-flags`
#: are not packages at all, and conda-forge says so.
#:
#: ### Reading the table
#:
#: The first three arguments are positional and they are a table: **name, upstream,
#: installed** -- upstream first because that is the column a currency verdict is
#: measured against. A hundred rows only stay readable as one line each, and one line
#: each is what makes "which of these is behind" a thing a reviewer can see rather
#: than compute.
#:
#: The order is the risk, and it is the risk positional arguments always carry:
#: swapping the pair inverts a package's currency verdict silently.
#: `test_local_dev_demo_data.py` compares every parsable pair and fails on an
#: inversion, which is the guard that makes the shape safe rather than merely
#: shorter.
DEMO_PACKAGES: Final[tuple[DemoPackage, ...]] = (
    # --- Carrying a real advisory ------------------------------------------------
    DemoPackage(
        "django", "6.0.7", "6.0.0", advisory=("CVE-2026-48588", "low", ">=6.0.0,<6.0.7"), licence="BSD-3-Clause"
    ),
    DemoPackage(
        "aiohttp",
        "3.10.11",
        "3.10.6",
        advisory=("CVE-2024-52303", "moderate", ">=3.10.6,<3.10.11"),
        licence="Apache-2.0",
    ),
    DemoPackage(
        "jinja2", "3.1.5", "3.0.0", advisory=("CVE-2024-56201", "moderate", ">=3.0.0,<3.1.5"), licence="BSD-3-Clause"
    ),
    DemoPackage(
        "werkzeug", "3.0.1", "3.0.0", advisory=("CVE-2023-46136", "moderate", ">=3.0.0,<3.0.1"), licence="BSD-3-Clause"
    ),
    DemoPackage("urllib3", "2.7.0", "2.6.0", advisory=("CVE-2026-44432", "high", ">=2.6.0,<2.7.0"), licence="MIT"),
    DemoPackage(
        "cryptography",
        "49.0.0",
        "45.0.0",
        advisory=("CVE-2026-69248", "moderate", ">=45.0.0,<49.0.0"),
        licence="Apache-2.0 OR BSD-3-Clause",
    ),
    DemoPackage(
        "paramiko",
        "3.4.0",
        "2.5.0",
        advisory=("CVE-2023-48795", "moderate", ">=2.5.0,<3.4.0"),
        licence="LGPL-2.1-or-later",
    ),
    DemoPackage("pyjwt", "2.13.0", "2.8.0", advisory=("CVE-2026-48525", "moderate", ">=2.8.0,<2.13.0"), licence="MIT"),
    DemoPackage(
        "authlib", "1.7.1", "1.7.0", advisory=("CVE-2026-41479", "moderate", ">=1.7.0,<1.7.1"), licence="BSD-3-Clause"
    ),
    DemoPackage(
        "waitress", "3.0.1", "2.0.0", advisory=("CVE-2024-49768", "critical", ">=2.0.0,<3.0.1"), licence="ZPL-2.1"
    ),
    DemoPackage(
        "starlette", "1.3.1", "0.41.3", advisory=("CVE-2026-54283", "high", ">=0.4.1,<1.3.1"), licence="BSD-3-Clause"
    ),
    DemoPackage(
        "litestar", "2.20.0", "2.19.0", advisory=("CVE-2026-25480", "moderate", ">=2.19.0,<2.20.0"), licence="MIT"
    ),
    DemoPackage(
        "tornado", "6.5.8", "6.5.5", advisory=("GHSA-wwv5-g3v4-889x", "low", ">=6.5.5,<6.5.8"), licence="Apache-2.0"
    ),
    DemoPackage(
        "scrapy",
        "2.14.2",
        "2.11.2",
        advisory=("GHSA-cwxj-rr6w-m6w7", "high", ">=1.4.0,<2.14.2"),
        licence="BSD-3-Clause",
    ),
    DemoPackage(
        "flask", "3.1.1", "3.1.0", advisory=("CVE-2025-47278", "low", ">=3.1.0,<3.1.1"), licence="BSD-3-Clause"
    ),
    DemoPackage("pyyaml", "5.2", "5.1", advisory=("CVE-2019-20477", "critical", ">=5.1,<5.2"), licence="MIT"),
    DemoPackage("wheel", "0.46.2", "0.40.0", advisory=("CVE-2026-24049", "high", ">=0.40.0,<0.46.2"), licence="MIT"),
    DemoPackage("black", "26.3.1", "24.3.0", advisory=("CVE-2026-32274", "high", ">=24.3.0,<26.3.1"), licence="MIT"),
    DemoPackage(
        "pyarrow", "23.0.1", "15.0.0", advisory=("CVE-2026-25087", "high", ">=15.0.0,<23.0.1"), licence="Apache-2.0"
    ),
    DemoPackage(
        "pillow", "12.3.0", "11.3.0", advisory=("CVE-2026-59204", "high", ">=8.2.0,<12.3.0"), licence="MIT-CMU"
    ),
    DemoPackage(
        "jupyterlab", "4.6.2", "4.6.0", advisory=("CVE-2026-73417", "high", ">=4.6.0,<4.6.2"), licence="BSD-3-Clause"
    ),
    DemoPackage(
        "notebook", "7.5.6", "7.0.0", advisory=("CVE-2026-42557", "high", ">=7.0.0,<7.5.6"), licence="BSD-3-Clause"
    ),
    DemoPackage(
        "transformers",
        "4.51.0",
        "4.49.0",
        advisory=("CVE-2025-3262", "moderate", ">=4.49.0,<4.51.0"),
        licence="Apache-2.0",
    ),
    DemoPackage(
        "keras", "3.15.0", "3.13.0", advisory=("CVE-2026-9335", "moderate", ">=3.13.0,<3.15.0"), licence="Apache-2.0"
    ),
    DemoPackage("lightgbm", "4.6.0", "4.5.0", advisory=("CVE-2024-43598", "high", ">=1.0.0,<4.6.0"), licence="MIT"),
    DemoPackage(
        "grpcio", "1.55.3", "1.55.0", advisory=("CVE-2023-4785", "high", ">=1.55.0,<1.55.3"), licence="Apache-2.0"
    ),
    DemoPackage("pydantic", "2.4.0", "2.0.0", advisory=("CVE-2024-3772", "moderate", ">=2.0.0,<2.4.0"), licence="MIT"),
    DemoPackage(
        "git",
        "2.50.1",
        "2.49.0",
        advisory=("CVE-2025-48384", "high", ">=0,<2.50.1"),
        kev_listed=True,
        kev_catalogued="2025-08-25",
        licence="GPL-2.0-only",
    ),
    # --- Web frameworks and the stack around them --------------------------------
    DemoPackage("fastapi", "0.121.4", "0.121.4", licence="MIT"),
    DemoPackage("uvicorn", "0.38.0", "0.38.0", licence="BSD-3-Clause"),
    DemoPackage("gunicorn", "23.0.0", "23.0.0", licence="MIT"),
    DemoPackage("httpx", "0.29.0", "0.29.0", licence="BSD-3-Clause"),
    DemoPackage("requests", "2.32.5", "2.32.3", licence="Apache-2.0"),
    DemoPackage("bottle", "0.13.4", "0.13.4", licence="MIT"),
    DemoPackage("falcon", "4.1.0", "4.1.0", licence="Apache-2.0"),
    DemoPackage("pyramid", "2.0.2", "2.0.0", licence="ZPL-2.1"),
    DemoPackage("quart", "0.20.0", "0.20.0"),
    DemoPackage("hypercorn", "0.17.3", "0.17.3"),
    DemoPackage("sanic", "25.3.0", "24.12.0", licence="MIT"),
    DemoPackage("twisted", "25.5.0", "24.11.0", licence="MIT"),
    DemoPackage("wtforms", "3.2.1", "3.1.2"),
    DemoPackage("flask-cors", "6.0.1", "6.0.1", licence="MIT"),
    # --- Data science -------------------------------------------------------------
    DemoPackage("numpy", "2.3.4", "2.2.6", licence="BSD-3-Clause"),
    DemoPackage("scipy", "1.16.2", "1.15.2", licence="BSD-3-Clause"),
    DemoPackage("pandas", "2.3.3", "2.2.3", licence="BSD-3-Clause"),
    DemoPackage("scikit-learn", "1.7.2", "1.6.1", licence="BSD-3-Clause"),
    DemoPackage("matplotlib", "3.10.6", "3.10.6", licence="PSF-2.0"),
    DemoPackage("statsmodels", "0.14.5", "0.14.5", licence="BSD-3-Clause"),
    DemoPackage("xarray", "2025.9.0", "2025.9.0", licence="Apache-2.0"),
    DemoPackage("dask", "2025.9.1", "2025.9.1", licence="BSD-3-Clause"),
    DemoPackage("polars", "1.34.0", "1.34.0", licence="MIT"),
    DemoPackage("h5py", "3.14.0", "3.14.0", licence="BSD-3-Clause"),
    DemoPackage("numba", "0.62.1", "0.62.1", licence="BSD-2-Clause"),
    DemoPackage("ipython", "9.6.0", "8.32.0", licence="BSD-3-Clause"),
    DemoPackage("plotly", "6.3.0", "6.3.0", licence="MIT"),
    DemoPackage("bokeh", "3.8.0", "3.8.0", licence="BSD-3-Clause"),
    DemoPackage("pytorch", "2.9.0", "2.6.0", licence="BSD-3-Clause"),
    DemoPackage("nltk", "3.9.2", "3.9.2", licence="Apache-2.0"),
    DemoPackage("spacy", "3.8.7", "3.8.7", licence="MIT"),
    DemoPackage("gensim", "4.3.3", "4.3.2"),
    DemoPackage("scikit-image", "0.25.2", "0.25.2", licence="BSD-3-Clause"),
    DemoPackage("shap", "0.48.0", "0.48.0", licence="MIT"),
    DemoPackage("patsy", "1.0.2", "1.0.1"),
    DemoPackage("datashader", "0.18.1", "0.18.1"),
    DemoPackage("umap-learn", "0.5.9", "0.5.9"),
    DemoPackage("hdbscan", "0.8.40", "0.8.40"),
    DemoPackage("imbalanced-learn", "0.14.0", "0.14.0", licence="MIT"),
    # --- The utilities every environment carries ----------------------------------
    DemoPackage("setuptools", "80.9.0", "75.8.0", licence="MIT"),
    DemoPackage("pip", "25.2", "25.0", licence="MIT"),
    DemoPackage("typer", "0.19.2", "0.19.2", licence="MIT"),
    DemoPackage("tqdm", "4.67.1", "4.67.1", licence="MPL-2.0 AND MIT"),
    DemoPackage("attrs", "25.4.0", "25.4.0", licence="MIT"),
    DemoPackage("cattrs", "25.3.0", "25.3.0", licence="MIT"),
    DemoPackage("packaging", "25.0", "25.0", licence="Apache-2.0 OR BSD-2-Clause"),
    DemoPackage("certifi", "2025.10.5", "2025.1.31", licence="MPL-2.0"),
    DemoPackage("idna", "3.11", "3.11", licence="BSD-3-Clause"),
    DemoPackage("psutil", "7.1.0", "7.1.0", licence="BSD-3-Clause"),
    DemoPackage("boto3", "1.40.40", "1.36.20", licence="Apache-2.0"),
    DemoPackage("sqlalchemy", "2.0.44", "2.0.38", licence="MIT"),
    DemoPackage("celery", "5.6.3", "5.4.0", licence="BSD-3-Clause"),
    DemoPackage("redis-py", "6.4.0", "5.2.1", licence="MIT"),
    DemoPackage("psycopg2", "2.9.10", "2.9.10", licence="LGPL-3.0-or-later"),
    DemoPackage("ruff", "0.14.1", "0.14.1", licence="MIT"),
    DemoPackage("mypy", "1.18.2", "1.18.2", licence="MIT"),
    DemoPackage("pytest", "8.4.2", "8.4.2", licence="MIT"),
    DemoPackage("coverage", "7.10.7", "7.10.7", licence="Apache-2.0"),
    DemoPackage("structlog", "25.4.0", "25.4.0", licence="Apache-2.0 OR MIT"),
    DemoPackage("orjson", "3.11.3", "3.11.3", licence="Apache-2.0 OR MIT"),
    DemoPackage("protobuf", "6.33.0", "5.29.3", licence="BSD-3-Clause"),
    DemoPackage("tenacity", "9.1.2", "9.1.2", licence="Apache-2.0"),
    DemoPackage("filelock", "3.19.1", "3.19.1", licence="Unlicense"),
    DemoPackage("marshmallow", "4.1.0", "4.1.0", licence="MIT"),
    # --- Things conda-forge ships that are not Python at all ----------------------
    #
    # Where `not_applicable` will come from once the readiness sweep has observed
    # them. The Python 3.14 question is not a hard one for these, it is not a
    # question at all, and a reader has to be able to tell that from `unknown` --
    # which is why they are in the roster rather than being simulated by a flag on
    # a package the question does apply to. Until the sweep runs they read
    # `unknown`, which is the truth about a question nobody has asked yet.
    DemoPackage("nodejs", "24.10.0", "22.13.1", licence="MIT"),
    DemoPackage("cmake", "4.1.2", "3.31.5", licence="BSD-3-Clause"),
    DemoPackage("ripgrep", "14.1.1", "14.1.1", licence="MIT OR Unlicense"),
    DemoPackage("ffmpeg", "8.0", "7.1.0", licence="GPL-3.0-or-later"),
    DemoPackage("libarchive", "3.8.1", "3.8.1", licence="BSD-2-Clause"),
    DemoPackage("sqlite", "3.50.4", "3.50.4", licence="blessing"),
    # --- Not packages at all, so the resolver finds nothing and says so -----------
    #
    # The rows a reviewer opens the identity queue for, and deliberately not real
    # packages: conda-forge's index has no entry under either name, so the resolver
    # records `not_found`, the shell stays `unmapped`, and `CPM-AD-4` gates every
    # verdict on them. Two rather than one so the queue looks like a queue. Nothing
    # here says they are unmapped; the source does.
    DemoPackage("internal-telemetry-sdk", "2.4.0", "2.4.0"),
    DemoPackage("internal-feature-flags", "0.9.3", "0.9.1"),
)


class _Unmetered:
    """A rate limiter that permits every call, for the seed's inline resolver and nothing else.

    Satisfies `core/rate_limit.py`'s `RateLimiter` protocol by answering yes. The
    resolver's allowance charges `1 + retries` per collection through a counter
    every worker shares, and a hundred inline collections would spend it inside the
    first minute and refuse the rest -- an honest refusal for a sweep and a useless
    one for a one-off seed of two hundred requests to two public hosts. The daily
    sweep keeps the real allowance; only the seed is handed this.
    """

    def acquire(self, *, collector: str, limit: RateLimit, now: datetime, cost: int = 1) -> bool:
        """Permit the call, whatever was asked.

        Args:
            collector: The collector's declared name. Unread.
            limit: The allowance a metered limiter would count against. Unread.
            now: The instant a metered limiter would decide the window from. Unread.
            cost: How many requests the caller is about to issue. Unread.

        Returns:
            True, always.

        """
        return True


def seed_demo_inventory(*, transport: Transport | None = None) -> dict[str, object]:
    """Seed a demo inventory, its evidence, a live identity resolution, and one real policy run.

    Args:
        transport: The transport the inline resolver reads conda-forge's index and
            PyPI through. `None` -- the local-dev default -- lets the collector build
            its own and reach the two hosts; the integration suite substitutes a
            scripted one so that it never opens a socket (`CPM-AD-27`).

    Returns:
        What was seeded; how many packages the resolver concluded on (`resolved`),
        how many conda-forge has no entry for (`not_on_conda_forge`), how many could
        not be asked about (`unreachable`) and how many were left alone because a
        person had verified them (`verified_kept`); and what the shipped parameter
        file leaves unconfigured, so the caller can say all of it.

    Raises:
        ImproperlyConfigured: The run is not local. Raised before any row is written,
            for the reason `_DEPLOYED_REFUSAL` states: append-only evidence cannot be
            taken back.

    """
    if not is_local():
        raise ImproperlyConfigured(_DEPLOYED_REFUSAL)

    # Imported here rather than at module scope: this module is reachable from
    # `config/`, which is imported at settings time, and the domain applications'
    # models need a populated app registry.
    from conda_sentinel.core.clock import SystemClock  # noqa: PLC0415 - see above

    clock = SystemClock()

    # Shells first, the resolver second, evidence third: the PyPI snapshot is
    # written only for a package the resolver has just confirmed PyPI has a
    # project for, so the resolution has to be on the package before the
    # evidence is chosen.
    packages = [_seeded_package(demo, clock=clock) for demo in DEMO_PACKAGES]
    counts = _resolve_identities(packages, clock=clock, transport=transport)
    observed_at = clock.now()
    for demo, package in zip(DEMO_PACKAGES, packages, strict=True):
        _seed_evidence(demo, package, observed_at=observed_at)

    summary = _run_policy(clock=clock)
    summary["resolved"] = counts.resolved
    summary["not_on_conda_forge"] = counts.not_on_conda_forge
    summary["unreachable"] = counts.unreachable
    summary["verified_kept"] = counts.verified_kept
    logger.info(
        SEEDED_EVENT,
        packages=[demo.name for demo in DEMO_PACKAGES],
        resolved=counts.resolved,
        not_on_conda_forge=counts.not_on_conda_forge,
        unreachable=counts.unreachable,
        verified_kept=counts.verified_kept,
        policy_version=summary["policy_version"],
        rollup_rows=summary["rollup_rows"],
        # Said rather than left to be discovered: whatever the shipped parameter
        # file records as empty makes the column it drives inert whatever evidence
        # is behind it.
        unconfigured=summary["unconfigured"],
    )
    return summary


def _seeded_package(demo: DemoPackage, *, clock: Clock) -> Package:
    """Return one demo package as ingestion would leave it: a shell, and nothing claimed.

    Args:
        demo: The package to seed.
        clock: The injected clock (`CPM-AD-26`).

    Returns:
        The saved package at `unmapped` confidence with no mapping rows. What it
        maps to is the resolver's to observe -- see `_resolve_identities`.

    """
    from conda_sentinel.identity.services import resolve_package_shell  # noqa: PLC0415 - after django.setup()

    # The shell is filed under the pair the resolver will find it by, because
    # `record_resolution` *updates* the row filed under `(identity_source,
    # associator_key)` and never creates one -- `CPM-AD-25` gives creation to
    # `resolve_package_shell` alone.
    with transaction.atomic():
        return resolve_package_shell(
            source_package_key=f"pypi:{demo.name}",
            package_name=demo.name,
            identity_source="pypi",
            clock=clock,
        )


def _seed_evidence(demo: DemoPackage, package: Package, *, observed_at: datetime) -> None:
    """Insert the observations the demo package's real sources state.

    Every row is an insert. `CPM-AD-2` makes evidence append-only, so this is exactly
    the write a collector performs -- and re-running the seeder adds a second
    observation of each fact rather than replacing the first, which is realistic and
    is what makes the detail view's superseded-evidence list worth looking at.

    At most four kinds of row, each with a source a reader can check: the advisory
    (or the collector's own nothing-matched row) and the KEV listing for every
    package; the licence only where the roster states one; and the PyPI release
    only for a package whose `release_ecosystem` mapping the resolver has just
    established -- which is the resolver having confirmed, minutes ago, that PyPI
    has a project under this name. A package PyPI has no project for (`git`,
    `sqlite`, the two `internal-*` names) gets no PyPI row, because PyPI never
    stated a version for it. Nothing about a source release, a feedstock, a conda
    build, readiness or verification -- those are the sweeps' to observe.

    Args:
        demo: What the sources state.
        package: The package they state it about, as the resolver left it.
        observed_at: When they were read.

    """
    with transaction.atomic():
        _seed_pypi_release(demo, package, observed_at=observed_at)
        _seed_advisory(demo, package, observed_at=observed_at)
        _seed_licence(demo, package, observed_at=observed_at)


def _seed_pypi_release(demo: DemoPackage, package: Package, *, observed_at: datetime) -> None:
    """Record the PyPI release, for a package the resolver has just found on PyPI.

    The version recorded is the roster's, which is what PyPI stated when the roster
    was harvested rather than what it states today; the daily `pypi_release` sweep
    observes the current one and supersedes this row. What makes the row honest is
    that PyPI does have the project -- the `release_ecosystem` mapping says so, and
    it was written minutes ago by the resolver reading PyPI's own document.

    Args:
        demo: What PyPI stated at harvest.
        package: The package, as the resolver left it.
        observed_at: When the row is stamped.

    """
    from conda_sentinel.collectors.models import PyPIReleaseSnapshot  # noqa: PLC0415 - after django.setup()
    from conda_sentinel.core.outcomes import OutcomeState  # noqa: PLC0415 - as above
    from conda_sentinel.identity.models import ESTABLISHED  # noqa: PLC0415 - as above
    from conda_sentinel.identity.models import MappingKind  # noqa: PLC0415 - as above
    from conda_sentinel.identity.models import PackageMapping  # noqa: PLC0415 - as above

    on_pypi = PackageMapping.objects.filter(
        package=package,
        kind=MappingKind.RELEASE_ECOSYSTEM.value,
        outcome=ESTABLISHED,
    ).exists()
    if not on_pypi:
        return
    PyPIReleaseSnapshot.objects.create(
        package=package,
        observed_at=observed_at,
        state=OutcomeState.OK.value,
        source=f"https://pypi.org/project/{demo.name}/",
        latest_version=demo.upstream_version,
    )


def _seed_advisory(demo: DemoPackage, package: Package, *, observed_at: datetime) -> None:
    """Record what the advisory sources state, and what the catalogue states beside it.

    Args:
        demo: What the sources state.
        package: The package.
        observed_at: When they were read.

    """
    from conda_sentinel.collectors.match_confidence import MatchConfidence  # noqa: PLC0415 - after django.setup()
    from conda_sentinel.collectors.models import KevFinding  # noqa: PLC0415 - as above
    from conda_sentinel.collectors.models import VulnerabilityFinding  # noqa: PLC0415 - as above
    from conda_sentinel.collectors.outcomes import LISTED  # noqa: PLC0415 - as above
    from conda_sentinel.collectors.outcomes import MATCHED  # noqa: PLC0415 - as above
    from conda_sentinel.collectors.outcomes import NOT_LISTED  # noqa: PLC0415 - as above
    from conda_sentinel.collectors.vulnerability import NOTHING_MATCHED_DETAIL  # noqa: PLC0415 - as above
    from conda_sentinel.core.outcomes import OutcomeState  # noqa: PLC0415 - as above

    if demo.advisory is None:
        # "The source was read and matched nothing" is `unknown` plus the collector's
        # own detail, **not** `not_found` -- and the seeder got that wrong first.
        # `VulnerabilityPass` reads the detail to tell "we looked and this package has
        # no advisory" from "we could not establish anything", because the two look
        # identical in the state column and only one of them is reassuring. The
        # constant is imported from the collector rather than retyped: the pass
        # matches on its exact prefix, so a copy that drifted would silently turn
        # every clean package `unknown`.
        VulnerabilityFinding.objects.create(
            package=package,
            observed_at=observed_at,
            state=OutcomeState.UNKNOWN.value,
            source="https://api.osv.dev/v1/query",
            detail=NOTHING_MATCHED_DETAIL,
        )
        return

    advisory_id, severity, affected_range = demo.advisory
    finding = VulnerabilityFinding.objects.create(
        package=package,
        observed_at=observed_at,
        state=MATCHED,
        source=f"https://osv.dev/vulnerability/{advisory_id}",
        advisory_id=advisory_id,
        severity=severity,
        affected_range=affected_range,
        # A **bare version**, which is what OSV states as the range's `fixed` event and
        # the one form `policies/remediation.py` can compare -- anything carrying an
        # ordering marker states a *set* of versions and is `not_comparable`.
        #
        # Every advisory row's `upstream_version` is the advisory's own fixed version,
        # by construction of the roster, so there is no second place to keep it.
        # Without it the remediation pass reads no surface at all and every
        # remediation row comes out `unknown` -- a demo where a quarter of the
        # inventory is vulnerable and the screen cannot say what to do about any of it.
        fixed_range=demo.upstream_version,
        matched_version=demo.installed_version,
        match_confidence=MatchConfidence.EXACT_VERSION,
    )
    KevFinding.objects.create(
        package=package,
        vulnerability_finding=finding,
        observed_at=observed_at,
        state=LISTED if demo.kev_listed else NOT_LISTED,
        source="https://www.cisa.gov/known-exploited-vulnerabilities-catalog",
        catalog_date_added=_catalogued(demo),
    )


def _catalogued(demo: DemoPackage) -> datetime | None:
    """Return the instant CISA's catalogue states it added this advisory, or none.

    Args:
        demo: The package, which carries the date the catalogue states.

    Returns:
        The stated date as an aware instant, or `None` for an advisory the catalogue
        does not list -- which is what `kev_findings` requires of a `not_listed` row.

    """
    if not demo.kev_listed or not demo.kev_catalogued:
        return None
    return datetime.fromisoformat(demo.kev_catalogued).replace(tzinfo=UTC)


def _seed_licence(demo: DemoPackage, package: Package, *, observed_at: datetime) -> None:
    """Record what the artifact's metadata declares as its licence, where the roster states one.

    A blank licence seeds nothing. The first draft wrote a `not_found` row sourced at
    conda-forge for those, which claimed conda-forge had been asked and declared no
    licence -- it had not been asked. The `license` collector observes that on the
    stack; until it does, the column honestly reads `unknown`.

    Args:
        demo: What the source states.
        package: The package.
        observed_at: When it was read.

    """
    from conda_sentinel.collectors.models import LicenseFinding  # noqa: PLC0415 - after django.setup()

    if not demo.licence:
        return

    LicenseFinding.objects.create(
        package=package,
        observed_at=observed_at,
        # `normalized`, not `ok`: every evidence vocabulary composes `core`'s four
        # sentinels with its *own* determinate members, and this table's is named for
        # what it did -- it read a licence and normalised it.
        state=_NORMALIZED,
        source=f"https://conda.anaconda.org/conda-forge/{demo.name}",
        channel="conda-forge",
        raw_license=demo.licence,
        normalized_license=demo.licence,
        # One of the three the column declares. `spdx-identifier` is what a
        # single well-known identifier is recognised as; a compound expression is
        # what `spdx-expression` is for -- and the roster carries `AND` as well
        # as `OR`, which a test for `" OR "` alone would have filed as an identifier.
        detection_method="spdx-expression" if _COMPOUND.search(demo.licence) else "spdx-identifier",
    )


@dataclass(frozen=True, slots=True)
class ResolutionCounts:
    """What the inline resolver did with the roster, by outcome.

    Attributes:
        resolved: Packages whose confidence is no longer `unmapped` after the run.
        not_on_conda_forge: Packages conda-forge's index has no entry for -- the run
            succeeded and recorded that absence. The normal outcome for the two
            `internal-*` names.
        unreachable: Packages the resolver could not conclude on for any other
            reason: a source that could not be asked, a document that could not be
            read, a recorder that refused, or a package skipped because the network
            had already failed three times in a row.
        verified_kept: Packages a person had set `verified`, which the resolver was
            never offered.

    """

    resolved: int
    not_on_conda_forge: int
    unreachable: int
    verified_kept: int


def _resolve_identities(
    packages: Sequence[Package],
    *,
    clock: Clock,
    transport: Transport | None,
) -> ResolutionCounts:
    """Run the real `resolve_identity` collector over every seeded package, inline.

    The step that makes the seeded identities observed rather than asserted. Each
    package goes through `IdentityResolutionCollector.collect` with `force=True`,
    so a second seed on the same day re-resolves rather than being suppressed by
    the observation window; the collector reads conda-forge's index and PyPI's
    project document and hands what they establish to `record_resolution`
    (`CPM-AD-14`), which is the one door this module never opens itself. Its runs
    are real runs, filed under its own name, and its snapshot rows are real
    evidence.

    **Only what the collector itself would select is offered.** `force=True`
    bypasses the observation window and nothing else, so a package a person has
    set `verified` since the last seed is filtered out here by the collector's own
    `selectable_packages` -- the recorder would hold the confidence back but would
    still rewrite the mappings, and a re-seed must not undo a person's decision.

    **A failure is one package's, never the seed's.** The base turns an unreachable
    source into a `failed` run with an `error` row and an absent index entry into a
    `not_found` row, and neither raises. A document that cannot be read or a
    recorder that refuses what it was handed raises out of `translate`, after the
    base has written that package's `error` row; a locator that cannot be built
    raises out of `source_for`, before any evidence path, and leaves a `failed`
    ledger row with no snapshot beside it. Each is caught here, logged by name, and
    counted -- the package stays the `unmapped` shell it was, which is the honest
    state for a package nothing resolved.

    **The outcome is read off the package, not off the run.** A run the base files
    as `succeeded` covers both "the recorder ran" and "the index has no entry", and
    the recorder can run and conclude nothing. So `resolved` means the package's
    confidence is no longer `unmapped`; anything still `unmapped` is
    `not_on_conda_forge` when its newest snapshot says `not_found`, and
    `unreachable` otherwise.

    **Three `unreachable` in a row and the rest are not asked.** Offline, every
    collection waits out the transport's timeout and its retries before failing,
    so a hundred of them is minutes of timing out. The remaining packages are
    counted `unreachable` without a run, and one warning says how many and why.

    Args:
        packages: The seeded shells, in roster order.
        clock: The injected clock (`CPM-AD-26`).
        transport: The transport to read through, or `None` for the collector's own.

    Returns:
        The counts by outcome.

    """
    from conda_sentinel.collectors.models import IdentityResolutionSnapshot  # noqa: PLC0415 - see below
    from conda_sentinel.collectors.resolve_identity import IdentityResolutionCollector  # noqa: PLC0415 - see below
    from conda_sentinel.collectors.resolve_identity import ResolutionDocumentError  # noqa: PLC0415 - see below
    from conda_sentinel.collectors.resolve_identity import ResolutionLocatorError  # noqa: PLC0415 - see below
    from conda_sentinel.core.outcomes import OutcomeState  # noqa: PLC0415 - after django.setup()
    from conda_sentinel.identity.models import IdentityConfidence  # noqa: PLC0415 - as above
    from conda_sentinel.identity.models import Package  # noqa: PLC0415 - as above
    from conda_sentinel.identity.services import ResolutionError  # noqa: PLC0415 - as above

    resolved = 0
    not_on_conda_forge = 0
    unreachable = 0
    unreachable_streak = 0
    refusals = (ResolutionLocatorError, ResolutionDocumentError, ResolutionError)
    selectable = set(IdentityResolutionCollector.selectable_packages())
    offered = [package for package in packages if package.pk in selectable]
    verified_kept = len(packages) - len(offered)
    with IdentityResolutionCollector(clock=clock, transport=transport, limiter=_Unmetered()) as collector:
        for position, package in enumerate(offered):
            if unreachable_streak >= UNREACHABLE_STREAK_LIMIT:
                skipped = len(offered) - position
                unreachable += skipped
                logger.warning(
                    RESOLUTION_ABANDONED_EVENT,
                    skipped=skipped,
                    detail=(
                        f"{UNREACHABLE_STREAK_LIMIT} consecutive resolutions could not reach their source, so the "
                        f"remaining {skipped} packages were not asked about and are counted unreachable. They stay "
                        f"unmapped; a second seed once the network is back resolves them."
                    ),
                )
                break
            detail = ""
            try:
                result = collector.collect(package_id=package.pk, force=True)
            except refusals as refused:
                detail = f"{type(refused).__name__}: {refused}"
            else:
                detail = result.detail
            # Read off the package rather than off the run: `succeeded` covers both
            # a recorded resolution and an absent index entry, and a recorded
            # resolution can conclude nothing. A keyword filter rather than a
            # comparison, on the terms `selectable_packages` sets.
            still_unmapped = Package.objects.filter(pk=package.pk, confidence=IdentityConfidence.UNMAPPED).exists()
            if not still_unmapped:
                resolved += 1
                unreachable_streak = 0
                continue
            newest = (
                IdentityResolutionSnapshot.objects.filter(package_id=package.pk)
                .order_by("-pk")
                .values_list("state", flat=True)
                .first()
            )
            if newest == OutcomeState.NOT_FOUND.value:
                not_on_conda_forge += 1
                unreachable_streak = 0
                logger.info(
                    UNRESOLVED_EVENT, package=package.canonical_name, outcome="not_on_conda_forge", detail=detail
                )
                continue
            unreachable += 1
            unreachable_streak += 1
            logger.warning(UNRESOLVED_EVENT, package=package.canonical_name, outcome="unreachable", detail=detail)
    return ResolutionCounts(
        resolved=resolved,
        not_on_conda_forge=not_on_conda_forge,
        unreachable=unreachable,
        verified_kept=verified_kept,
    )


def _run_policy(*, clock: Clock) -> dict[str, object]:
    """Record a finished collection run and execute one real policy run over it.

    The step that makes the seeded screens honest: every status they show is
    concluded here, by the passes that own it, from the parameter file that ships.

    The policy run's evidence cut-off is what `core/policy_run.py` derives from the
    ledger -- the seeder's own run, opened and finished here through `collection_run`
    at the clock's instants, is the newest finished run and so every row seeded
    before it is inside the cut-off.

    Args:
        clock: The injected clock (`CPM-AD-26`), which stamps the run.

    Returns:
        What the run concluded and what the shipped parameters left unconfigured.

    """
    from conda_sentinel.core.ledger import collection_run  # noqa: PLC0415 - after django.setup()
    from conda_sentinel.core.policy_run import execute_policy_run  # noqa: PLC0415 - as above
    from conda_sentinel.policies.parameters import parameters_file  # noqa: PLC0415 - as above

    # Through the ledger's own writer rather than an insert. Two reasons: it opens
    # the run before the work and finalises it after, which is the shape a real
    # collector's run has and therefore the shape the coverage screen reads; and it
    # keeps the `status=` write inside `core/ledger.py`, which is the module
    # `tests/unit/django_apps/test_derived_status_writability_audit.py` records as
    # owning one. A seeder that inserted the row itself would need an exemption in
    # that table, which is a heavy thing to spend on a fixture.
    with collection_run(collector=DEMO_COLLECTOR, clock=clock) as handle:
        handle.succeeded()
    version = _shipped_policy_version()
    summary = execute_policy_run(policy_version=version, clock=clock)
    return {
        "policy_version": version,
        "rollup_rows": summary.rollup_rows,
        "parameters_file": str(parameters_file()),
        "unconfigured": _unconfigured_at(version),
    }


def _unconfigured_at(version: str) -> str:
    """Return what the parameter file leaves undecided at the version just run.

    **Read from the parameters rather than written as a sentence**, and the reason is
    a lesson rather than a preference: this used to be a fixed string saying priority
    and licence come out inert. Recording a rule set at a newer version made that
    string false the moment the seeder picked the newer version up -- a demo
    confidently explaining a state it was no longer in, which is worse than a demo
    that says nothing.

    Args:
        version: The policy version the run applied.

    Returns:
        A sentence naming what is still empty, or -- when nothing is -- pointing at
        the file so a reader can see whether the version they just ran at is a
        proposal.

    """
    from conda_sentinel.policies.parameters import parameters_for  # noqa: PLC0415 - after django.setup()

    recorded = parameters_for(version)
    empty = [
        name
        for name, values in (("priority_rules", recorded.priority_rules), ("license_rules", recorded.license_rules))
        if not values
    ]
    if not empty:
        return (
            f"Every rule set is recorded at {version}. If that version is a PROPOSAL -- the comment above it in "
            f"the parameter file says so -- then the priority buckets on these screens are a proposal too, and "
            f"reviewing them by looking at the screens is exactly what it is for."
        )
    return (
        f"{' and '.join(empty)} are empty at {version}, so the columns they drive come out unknown or "
        f"manual_review whatever evidence is behind them. The parameter file says why; the values are PRD "
        f"Open Questions 8 and 4."
    )


def _shipped_policy_version() -> str:
    """Return the newest policy version the shipped parameter file records.

    Read rather than written down: an unrecorded version fails every package
    (`CPM-CURRENCY-S07`), so a constant here would break the seeder on the day
    somebody added a version and not before.

    Returns:
        The newest recorded version.

    Raises:
        ImproperlyConfigured: The shipped file records none, which would make every
            seeded package fail for a reason that has nothing to do with the demo.

    """
    from conda_sentinel.policies.parameters import parameters_file  # noqa: PLC0415 - after django.setup()
    from conda_sentinel.policies.parameters import parameters_from  # noqa: PLC0415 - as above

    source = parameters_file()
    versions = sorted(parameters_from(source.read_text(encoding="utf-8"), source=source))
    if not versions:
        message = "the shipped policy parameter file records no version, so no policy run can complete."
        raise ImproperlyConfigured(message)
    return versions[-1]
