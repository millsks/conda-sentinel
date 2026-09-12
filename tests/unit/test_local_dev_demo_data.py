"""The demo seeder's declarations: refused outside a local run, spelled correctly, and asserting nothing invented.

Three things are checked here. The first two are the module's own promises: the
refusal fires before any row is written, and the one state literal the seeder writes
-- `"normalized"` -- is a value its column declares, because `core/outcomes.py`'s
`outcome_type` composes each vocabulary per domain and a member reference is
invisible to the type checker. A literal that drifted would fail at the first
`IntegrityError` on somebody's laptop, halfway through seeding, with a constraint
name and no hint which value was wrong.

**The third is the shape of the roster, and it is this module's reason to exist
since `CPM-PLATFORM-S08`.** The seeder used to assert what it never observed: every
package `verified` with a repository at `github.com/demo/<name>`, a feedstock
asserted or denied by a hand-kept flag, an invented build, an invented readiness. So
`DemoPackage` is pinned to exactly the seven fields that have a real source behind
them, the module is swept for the fictional URL and for the `verified` literal, and
the limiter the inline resolver runs through is checked to permit -- because a
resolver that ran metered would refuse most of the roster and the demo would look
like the network was down.

**The refusal is checked before any row is written**, which is the whole of why it
matters: `CPM-AD-2` makes evidence append-only, so an observation written by a
deployed component cannot be deleted and would be read by every replayed policy run
afterwards. A refusal that fired after the first insert would be no refusal at all.

Reads declarations and model metadata: no database writes, no policy run, no
network. What the seeder actually produces is
`tests/integration/test_local_dev_demo_seeding.py`.
"""

from __future__ import annotations

import ast
import inspect
import re
from datetime import date
from datetime import timedelta
from typing import Final

import pytest
from django.core.exceptions import ImproperlyConfigured

from conda_sentinel.collectors.models import LicenseFinding
from conda_sentinel.collectors.models import PyPIReleaseSnapshot
from conda_sentinel.collectors.models import VulnerabilityFinding
from conda_sentinel.collectors.resolve_identity import COLLECTOR_NAME
from conda_sentinel.collectors.resolve_identity import RESOLUTION_RATE_LIMIT
from conda_sentinel.core.rate_limit import RateLimiter
from conda_sentinel.core.registry import registered_collectors
from conda_sentinel.identity.models import IdentityConfidence
from config.local_dev import demo_data
from tests.clocks import FIXED_INSTANT

#: The literals the seeder writes, and the model whose `state` column has to accept
#: each. Written out rather than read from the module's private names, so the pairing
#: is stated here and a rename on either side is a failing case rather than a test
#: that quietly checks a value against itself.
DECLARED_STATES: Final[tuple[tuple[str, type], ...]] = (("normalized", LicenseFinding),)

#: The seven fields a roster row may carry, and the only seven.
#:
#: Each has a source a reader can check: the name and version pair are what PyPI
#: and conda-forge state, the advisory is OSV's, the two KEV fields are CISA's, and
#: the licence is the artifact's own metadata. A field naming a repository, a
#: feedstock, a build or a readiness was a fact nobody observed, and every one of
#: them has been removed -- so this set is exact rather than a lower bound, and an
#: eighth field is a failing case until it can say where its value comes from.
REAL_SOURCED_FIELDS: Final[frozenset[str]] = frozenset(
    {"name", "upstream_version", "installed_version", "advisory", "kev_listed", "kev_catalogued", "licence"},
)

#: The fictional repository host the first draft of the seeder asserted for every
#: package, and the confidence it asserted it at. Neither may appear in the module.
THE_FICTIONAL_HOST: Final[str] = "github.com/demo"

#: The queryset methods a `confidence=` keyword may be handed: reads, never writes.
QUERYSET_READS: Final[frozenset[str]] = frozenset({"filter", "exclude"})

#: The two names conda-forge has no entry for, which the resolver leaves `unmapped`.
THE_NOT_PACKAGES: Final[frozenset[str]] = frozenset({"internal-telemetry-sdk", "internal-feature-flags"})

#: How many packages the demo seeds.
#:
#: A hundred since `CPM-PLATFORM-S05`, and the number is asserted for the reason it
#: was asserted at ten: the point of the roster is *variety*, and a seeder that had
#: lost most of it would still produce a screen that looked fine. What changed is why
#: a hundred -- ten rows fit above the fold, sort instantly and paginate never, so a
#: reviewer asking whether a screen is usable was being shown one that could not be
#: unusable.
EXPECTED_PACKAGES: Final[int] = 100


@pytest.mark.parametrize(("value", "model"), DECLARED_STATES, ids=str)
def test_every_state_literal_is_one_its_column_declares(value: str, model: type) -> None:
    """The reconciliation the module's docstring promises.

    A literal that drifted fails here, naming the value and the table, rather than at
    the first `IntegrityError` halfway through seeding somebody's laptop.

    Args:
        value: The literal the seeder writes.
        model: The evidence model whose `state` column must accept it.

    """
    declared = {choice for choice, _label in model._meta.get_field("state").choices}  # noqa: SLF001 - model metadata

    assert value in declared, f"{model.__name__}.state does not accept {value!r}; it accepts {sorted(declared)}"


def test_the_demo_collector_is_not_a_registered_collector() -> None:
    """The coverage screen reads the run ledger by collector name.

    Seeding runs under `vulnerability` would report that collector as healthy on a
    machine where it has never made a single request -- which is precisely the lie
    `surface/coverage.py` exists to prevent, told by the fixture meant to demonstrate
    it.
    """
    adopted = {collector.name for collector in registered_collectors()}

    assert demo_data.DEMO_COLLECTOR not in adopted
    assert adopted != set()


def test_the_roster_seeds_enough_packages_to_show_variety() -> None:
    """A seeder that had lost most of its roster would still render a plausible screen.

    Which is the failure worth catching: the demo exists to show that the design
    distinguishes states, and eight identical rows demonstrate nothing.
    """
    assert len(demo_data.DEMO_PACKAGES) == EXPECTED_PACKAGES
    assert len({demo.name for demo in demo_data.DEMO_PACKAGES}) == EXPECTED_PACKAGES


def test_the_roster_covers_every_state_the_screens_must_distinguish() -> None:
    """`CPM-FR-5`'s five states, each present in the seeded inventory.

    Asserted over the *declarations* rather than over what the run concludes, because
    this is the roster's job: a demo that produced only clean packages and adverse
    verdicts would leave the three states a reader most needs to tell apart untested
    by eye.
    """
    assert any(demo.advisory for demo in demo_data.DEMO_PACKAGES), "no adverse verdict"
    assert any(not demo.advisory for demo in demo_data.DEMO_PACKAGES), "nothing found by a lookup"
    assert any(not demo.licence for demo in demo_data.DEMO_PACKAGES), "no package whose licence is left to the sweep"
    names = {demo.name for demo in demo_data.DEMO_PACKAGES}
    assert names >= THE_NOT_PACKAGES, "nothing for the resolver to find absent and the confidence gate to blank"


def test_every_roster_row_carries_only_real_sourced_fields() -> None:
    """The shape `CPM-PLATFORM-S08` fixed: seven fields, each with a source somebody can check.

    Exact rather than a subset in both directions. A field dropped would be a
    source the demo stopped showing; a field added is the failure this exists for --
    the first draft grew `identified`, `feedstock`, `feedstock_idle_days`,
    `fix_reached`, `python_evidence` and `errored`, every one an observation nobody
    made, and a reviewer who checked any of them found nothing.
    """
    fields = frozenset(demo_data.DemoPackage.__dataclass_fields__)

    assert fields == REAL_SOURCED_FIELDS, sorted(fields ^ REAL_SOURCED_FIELDS)


def test_no_roster_row_names_a_repository_feedstock_readiness_or_build() -> None:
    """The matrix's roster row, stated over the names rather than the values.

    A roster field is a claim the seeder will write, so a field whose name says
    "repository" or "feedstock" is a claim about something only the resolver and the
    sweeps observe. The set is the vocabulary the removed fields used and the
    vocabulary a re-introduction would reach for.
    """
    fields = {field.casefold() for field in demo_data.DemoPackage.__dataclass_fields__}
    invented = {"repository", "repo", "feedstock", "readiness", "python", "build", "identified", "errored", "fix"}

    assert [field for field in fields if any(word in field for word in invented)] == []


def test_nothing_in_the_module_names_the_fictional_repository_host() -> None:
    """`github.com/demo/<name>` was a URL every package carried and nothing ever served.

    Swept over the module's source text rather than over the roster's values,
    because the host was never a roster value -- it was an f-string in the code that
    wrote the resolution, and that is where it would come back.
    """
    source = inspect.getsource(demo_data)

    assert THE_FICTIONAL_HOST not in source


def test_the_seeder_never_spells_the_verified_confidence() -> None:
    """The seeder claims no confidence at all, so the literal has no business in it.

    `verified` is the confidence a *person* establishes (`CPM-AD-4`); the first draft
    wrote it for ninety-eight packages nobody had looked at. What the seeder writes
    now is a shell through `resolve_package_shell`, and what the resolver claims is
    its own to decide -- so the string appearing here again would be the first draft
    coming back. Swept as the bare quoted literal: the word appears in prose about
    why it must not, and prose is not a claim.
    """
    source = inspect.getsource(demo_data)
    literal = re.compile(rf"""["']{re.escape(IdentityConfidence.VERIFIED.value)}["']""")

    assert literal.search(source) is None


def test_the_seeder_calls_no_recorder_and_sets_no_confidence() -> None:
    """Identity is written through two doors and the seeder opens only the first.

    `resolve_package_shell` creates the shell; `record_resolution` is the resolver's
    to call, through `IdentityResolutionCollector.collect`. A seeder that called the
    recorder itself would be back to asserting mappings, whatever it asserted -- and
    a `confidence=` handed to anything that writes is the seeder deciding what it
    may claim. A `confidence=` on a queryset *read* (`filter`, `exclude`) is the
    seeder asking what the resolver concluded, which is the one thing it may do
    with a confidence and is how the summary is counted. Read off the syntax tree
    rather than the text: the module's prose names the recorder to say why it is
    not called, and prose is not a call.
    """
    tree = ast.parse(inspect.getsource(demo_data))
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
    called = {_called_name(call) for call in calls}
    keywords = {keyword.arg for call in calls if _called_name(call) not in QUERYSET_READS for keyword in call.keywords}

    assert "record_resolution" not in called
    assert "confidence" not in keywords
    assert "resolve_package_shell" in called
    assert "IdentityResolutionCollector" in called


def _called_name(call: ast.Call) -> str:
    """Return the bare name a call reaches for, however it was spelled.

    Args:
        call: The call node.

    Returns:
        The final name segment -- `record_resolution` for both `record_resolution(...)`
        and `services.record_resolution(...)` -- or the empty string for a call whose
        target is not a name at all.

    """
    target = call.func
    if isinstance(target, ast.Attribute):
        return target.attr
    if isinstance(target, ast.Name):
        return target.id
    return ""


def test_the_unmetered_limiter_permits_and_satisfies_the_protocol() -> None:
    """The resolver runs inline through a limiter that always says yes.

    Its declared allowance charges `1 + retries` per collection through a counter
    every worker shares, so a hundred inline collections through the real limiter
    would refuse most of the roster and the demo would look like the network was
    down. Checked against the protocol so a renamed method fails here rather than
    as a hundred `AttributeError`s halfway through a seed.
    """
    limiter = demo_data._Unmetered()  # noqa: SLF001 - the local-dev class under test

    assert isinstance(limiter, RateLimiter)
    assert limiter.acquire(collector=COLLECTOR_NAME, limit=RESOLUTION_RATE_LIMIT, now=FIXED_INSTANT, cost=1)
    assert limiter.acquire(
        collector=COLLECTOR_NAME,
        limit=RESOLUTION_RATE_LIMIT,
        now=FIXED_INSTANT + timedelta(days=1),
        cost=RESOLUTION_RATE_LIMIT.calls * 2,
    )


def test_the_unreachable_streak_limit_is_small_and_positive() -> None:
    """Three in a row: enough to tell "the network is down" from one flaky answer.

    Offline, each collection waits out the transport's timeout and retries before
    it fails; a limit of a hundred would be no limit, and a limit of one would
    abandon ninety-nine packages for a single refused connection.
    """
    assert 1 < demo_data.UNREACHABLE_STREAK_LIMIT < len(demo_data.DEMO_PACKAGES)


def test_the_roster_carries_a_kev_listing_and_a_non_listing() -> None:
    """The KEV column qualifies the vulnerability column beside it.

    A demo where every advisory was listed -- or none was -- would make the two
    columns look like one.
    """
    with_advisory = [demo for demo in demo_data.DEMO_PACKAGES if demo.advisory]

    assert any(demo.kev_listed for demo in with_advisory)
    assert any(not demo.kev_listed for demo in with_advisory)


def test_seeding_is_refused_outside_a_local_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """`CPM-AD-2` is why this refusal has to fire before the first insert.

    Evidence is append-only: a fictional observation written by a deployed component
    cannot be deleted, and every replayed policy run would read it afterwards. A
    refusal that fired after the first row would be no refusal at all.

    Args:
        monkeypatch: pytest's patcher, which restores the locality reader.

    """
    monkeypatch.setattr(demo_data, "is_local", lambda: False)

    with pytest.raises(ImproperlyConfigured, match=r"append-only"):
        demo_data.seed_demo_inventory()


def test_the_refusal_names_what_to_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """A refusal a reader cannot act on sends them to the source to find out how.

    Args:
        monkeypatch: pytest's patcher.

    """
    monkeypatch.setattr(demo_data, "is_local", lambda: False)

    with pytest.raises(ImproperlyConfigured, match=r"COMPONENT_RUNTIME=local"):
        demo_data.seed_demo_inventory()


def test_no_demo_package_declares_a_verdict() -> None:
    """The constraint the whole module is shaped around, asserted as a shape.

    `DemoPackage` has no field that says "this should come out P1". Every field is an
    observation, and what the screens show has to be something the policy engine
    concluded from them -- otherwise the demo is a picture of a product rather than
    the product.
    """
    fields = {field.name for field in demo_data.DemoPackage.__dataclass_fields__.values()}
    verdict_shaped = {"priority", "bucket", "work_type", "status", "readiness", "currency"}

    assert fields & verdict_shaped == set(), sorted(fields & verdict_shaped)


def test_the_pypi_snapshot_accepts_the_ok_state_the_seeder_writes() -> None:
    """The one table the seeder writes whose determinate value *is* `ok`.

    Which is the trap: `LicenseFinding` calls its determinate state `normalized`,
    while this one calls its `ok`. Writing `ok` to the first is the mistake the
    seeder made, and this is the case that says the other is not the same.
    """
    declared = {choice for choice, _label in PyPIReleaseSnapshot._meta.get_field("state").choices}  # noqa: SLF001

    assert "ok" in declared


def test_the_vulnerability_table_does_not_accept_ok() -> None:
    """And the one that catches the trap from the other side.

    `VulnerabilityFinding`'s determinate state is `matched`. If it ever grew an `ok`,
    the seeder's states would still be right but the argument above would have
    quietly stopped being the reason they are.
    """
    declared = {choice for choice, _label in VulnerabilityFinding._meta.get_field("state").choices}  # noqa: SLF001

    assert "ok" not in declared
    assert "matched" in declared


def _parsed(version: str) -> tuple[int, ...] | None:
    """Return a version as a comparable tuple, or `None` where it is not numeric.

    Deliberately not `packaging.version.Version`. This module needs to compare two
    strings the roster wrote next to each other, not to implement PEP 440, and every
    version in the roster is dotted digits -- so a parser that gives up on anything
    else is honest about its own reach and adds no dependency to say so.

    Args:
        version: The version string as the roster declares it.

    Returns:
        The dotted numbers as a tuple, or `None` when any component is not a number.

    """
    parts = version.split(".")
    if not all(part.isdigit() for part in parts):
        return None
    return tuple(int(part) for part in parts)


def test_no_roster_row_declares_an_installed_version_ahead_of_its_upstream() -> None:
    """The guard that makes the roster's positional columns safe rather than shorter.

    `CPM-PLATFORM-S05` turned a ten-row roster into a hundred, and a hundred rows only
    stay readable as one line each -- which means the first three arguments are
    positional and one of them is `upstream_version` and the next is
    `installed_version`. Swapping that pair is the mistake positional arguments always
    invite, and it is a *silent* one here: the currency pass concludes `behind` for a
    package that is current and `current` for one that is behind, both of which render
    perfectly and neither of which is flagged by anything else.

    Equal versions are correct and common -- most of the roster is up to date.
    """
    inverted = [
        f"{demo.name}: installed {demo.installed_version} ahead of upstream {demo.upstream_version}"
        for demo in demo_data.DEMO_PACKAGES
        if (installed := _parsed(demo.installed_version)) is not None
        and (upstream := _parsed(demo.upstream_version)) is not None
        and installed > upstream
    ]

    assert inverted == []


def test_the_guard_would_catch_an_inverted_pair() -> None:
    """Because a comparison over a roster that happens to be right proves nothing.

    The case above passes on an empty roster, on a roster of equal pairs, and on one
    where every version failed to parse. This is the one that says the comparison
    itself works, written against a declaration rather than against the real roster.
    """
    inverted = demo_data.DemoPackage(name="wrong-way-round", upstream_version="1.0.0", installed_version="2.0.0")

    installed = _parsed(inverted.installed_version)
    upstream = _parsed(inverted.upstream_version)

    assert installed is not None
    assert upstream is not None
    assert installed > upstream


def test_every_advisory_identifier_is_one_somebody_can_look_up() -> None:
    """The product owner asked for real advisories, and this is what "real" has to mean.

    The roster used to carry `GHSA-demo-high` and `GHSA-demo-moderate`. A reviewer who
    looks one of those up finds nothing, and what they learn is to stop looking things
    up -- which is a worse outcome than a demo with no advisories at all, because it
    trains the habit the product exists to support out of them.

    Every identifier below came from OSV.dev. This asserts the *shape* rather than
    re-querying: a test that made a network request would fail on an aeroplane, and
    what it would be checking is that OSV is up rather than that this roster is
    honest. What it does catch is a placeholder, which is the thing that actually went
    wrong.
    """
    identifiers = [demo.advisory[0] for demo in demo_data.DEMO_PACKAGES if demo.advisory]
    real = re.compile(r"^(?:CVE-\d{4}-\d{4,}|GHSA-[0-9a-z]{4}-[0-9a-z]{4}-[0-9a-z]{4})$")

    assert identifiers != []
    assert [name for name in identifiers if not real.match(name)] == []
    assert [name for name in identifiers if "demo" in name.casefold()] == []


def test_every_advisory_states_a_severity_the_shipped_order_ranks() -> None:
    """A severity outside the recorded order contributes nothing to the risk level.

    `vulnerability_risk_order` is matched case-insensitively against the finding's own
    stated severity, and a severity the order does not name is silently ignored -- so
    a roster full of advisories could still produce a screen where every risk level is
    blank, which looks like the pass being broken.
    """
    shipped_order = {"critical", "high", "moderate", "low"}
    stated = {demo.advisory[1] for demo in demo_data.DEMO_PACKAGES if demo.advisory}

    assert stated <= shipped_order
    # More than one, or the risk column shows a single tone and demonstrates no order.
    assert len(stated) > 1


def test_a_kev_listing_carries_the_date_the_catalogue_states() -> None:
    """The reason a KEV listing is worth showing at all is that it is checkable.

    A date computed from the run -- "thirty days ago", which this seeder used to do --
    moves every time somebody reseeds and matches nothing in CISA's catalogue. A
    reader who checks finds a mismatch and concludes the collector is wrong.
    """
    listed = [demo for demo in demo_data.DEMO_PACKAGES if demo.kev_listed]

    assert listed != []
    assert [demo.name for demo in listed if not demo.kev_catalogued] == []
    for demo in listed:
        assert date.fromisoformat(demo.kev_catalogued) <= date.today(), demo.name  # noqa: DTZ011


def test_nothing_unlisted_claims_a_catalogue_date() -> None:
    """`kev_findings` refuses a `not_listed` row that carries one, and rightly.

    A date on a row saying the catalogue does not list the advisory is a contradiction
    the table has a constraint against, so a roster that declared one would fail at
    the first insert rather than at the point the mistake was made.
    """
    stray = [demo.name for demo in demo_data.DEMO_PACKAGES if demo.kev_catalogued and not demo.kev_listed]

    assert stray == []


def test_the_roster_is_the_mixture_it_was_asked_to_be() -> None:
    """Web frameworks, data science, utilities, and things that are not Python at all.

    Asked for by name by the product owner. A roster of a hundred packages all of one
    kind would be a hundred rows that still demonstrate one thing -- and the
    non-Python entries are load-bearing rather than decorative: they are where
    `not_applicable` on the Python 3.14 column comes from, instead of a contrivance.
    """
    names = {demo.name for demo in demo_data.DEMO_PACKAGES}

    for kind, sample in (
        ("web frameworks", {"django", "flask", "fastapi", "tornado", "litestar"}),
        ("data science", {"numpy", "pandas", "scikit-learn", "pytorch", "hdbscan"}),
        ("utilities", {"setuptools", "pytest", "boto3", "sqlalchemy", "cattrs"}),
        ("not Python at all", {"git", "nodejs", "cmake", "ffmpeg", "sqlite"}),
    ):
        assert sample <= names, f"the roster lost its {kind}: {sorted(sample - names)}"


@pytest.mark.parametrize(
    ("licence", "method"),
    [
        ("MIT", "spdx-identifier"),
        ("Apache-2.0 OR BSD-3-Clause", "spdx-expression"),
        ("Apache-2.0 AND BSD-3-Clause", "spdx-expression"),
        ("GPL-2.0-only WITH Classpath-exception-2.0", "spdx-expression"),
        # Not an operator: the word is inside an identifier, not joining two.
        ("BSD-3-Clause-Clear", "spdx-identifier"),
    ],
)
def test_a_compound_licence_is_recorded_as_an_expression(licence: str, method: str) -> None:
    """The column declares three values and the seeder has to pick the right one.

    It used to test for `" OR "` alone, which was true of the ten-package roster and
    false of this one: `tqdm` declares `MPL-2.0 AND MIT` and `python-dateutil`
    declares `Apache-2.0 AND BSD-3-Clause`, and both would have been filed as single
    identifiers -- a licence screen quietly saying an expression is an identifier.

    Args:
        licence: What the metadata declared.
        method: What the seeder should record as having recognised it.

    """
    recognised = "spdx-expression" if demo_data._COMPOUND.search(licence) else "spdx-identifier"  # noqa: SLF001

    assert recognised == method
