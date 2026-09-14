"""`IDENTITY.03-AUDIT-001`: one confidence gate, in `core`, and nowhere else.

`CPM-AD-4` says the gate is "one function in `core`, called by the orchestrating
policy run (`CPM-AD-21`), never re-implemented per pass". `CPM-EVIDENCE-S07`
built that function -- `core/confidence.py`'s `gated_status` and
`require_known_confidence` -- and wired it into the one rollup writer. This
module is the thing that keeps it the only one.

The gate is three lines, and `R-08` is that the ninth policy pass writes them
again inline. It costs nothing to type, it reads as obviously correct, and it is
invisible in review -- right up until eight passes have eight slightly different
answers to "what may automation claim about an unmapped package" and no row says
which one produced it. So the ban is mechanical.

**What counts as a second gate: a status selected on an identity confidence.**
Not "a comparison against `unmapped`", which was this detector's first and much
too narrow definition. The same rule is written at least five ways, and a person
reaching for the most natural one lands outside a detector built around the
first:

* from the trusted side -- `if confidence in {VERIFIED, INVENTORY_DERIVED}:
  return verdict`, then `return UNKNOWN` below it, which is arguably how anybody
  would write "only claim for identities we trust";
* by inequality -- `if confidence != VERIFIED: return UNKNOWN`, which is
  `CPM-AD-4`'s violation itself, since it degrades `inventory-derived`;
* by `match`, on a 3.12 floor, where the confidence never appears in an
  `ast.Compare` at all;
* as a table, a dict from confidence values to statuses;
* short-circuited into one expression -- `(confidence == UNMAPPED and UNKNOWN) or
  verdict`.

So the offence is: a branch, conditional expression, `match`, `while` or boolean
expression that **tests any identity confidence** and, on either side of that
test, **selects a status**. `CPM-AD-4`'s own words for the second half are
"expressed as writing a value": a branch that raises or logs decides nothing
about what the product claims, which is what leaves `identity/services.py`'s
resolution-time rule alone. The implicit else counts -- a confidence test whose
branch returns, followed by a `return UNKNOWN`, is one statement written as two.

**A status is recognised by name across every vocabulary,** not only on
`OutcomeState`. `core/outcomes.py` composes a per-domain type for each pass
through `outcome_type`, and each inherits the four sentinels by name and value,
so `LicenceOutcome.UNKNOWN` is the spelling the passes this audit is written for
will actually use. A detector that knew only `OutcomeState` would be blind to
every real pass while looking rigorous.

**Names are resolved, not matched.** A confidence bound to a module constant, a
status bound through two hops, a membership test against a named tuple, an
aliased class, a module alias, and package-then-attribute access
(`identity.models.IdentityConfidence.UNMAPPED`) all reach the same vocabulary,
and "rename it and the audit stops firing" is not a ban. Binding resolution runs
to a fixed point.

**Two nets, and the second cannot be used to escape the first.** The ban admits
no exemption. The second net -- `confidence_tests` -- reports every *other* place
a confidence is tested in shipped code, and `RECORDED_EXEMPTIONS` records those
by file, by form **and by the function they sit in**, counted. A test that
selects a status is excluded from that net by construction, so a real second gate
can never be reclassified as "a comparison selecting no status" and legitimised
with an entry. `test_a_recorded_exemption_cannot_licence_a_second_gate` asserts
that in both directions.

The record covers `src/` while the ban covers the whole repository, and that
asymmetry is deliberate: a second gate written into a test is still a second
answer to what may be claimed, but a test *comparing* a confidence is an
assertion about the product rather than a rule the product applies, and
recording every one of those would bury the entries that mean something.

**Matched on the parsed syntax tree, never by text search.** Prose about the
prohibition -- this docstring, `core/confidence.py`'s own, `CPM-AD-4` quoted in a
comment -- must not itself register as an offence, and
`test_the_ban_is_not_a_substring_scan` proves that rather than asserting it in a
sentence: the clean controls all contain the banned word.

**The walk does not stop at `def`,** which is where this audit parts company with
the ordering audit next door. A second confidence gate inside a function body is
the *primary* shape -- it is what "the ninth pass writes the rule inline in its
`run` method" looks like -- so a scan that stopped at the first `def` would miss
every real instance of the thing it exists to catch. It does stop at a `def`
*nested inside a guarded branch*, which is a different question: a closure defined
under a confidence test is not that test selecting a status.

**What escapes it, stated rather than left to be discovered.**

* **A delegating wrapper.** `def currency_gate(v, *, confidence): return
  gated_status(v, confidence=confidence)` is a second answer to "what may be
  claimed" and this detector cannot report it, because AC 4 pins the calling
  shape -- a function whose body is a call to the gate -- as the legitimate one,
  and no AST rule separates the two. `CPM-AD-4`'s "never re-export or wrap" is
  prose here, enforced in review.
* **A table written entirely in string literals.** `{"verified": "ok",
  "unmapped": "unknown"}` is as often fixture data as it is a gate, and the ban
  has no exemption path, so reporting it would leave a reader with no move except
  deleting legitimate data or widening the detector. The table form therefore
  requires at least one member *reference* on one side of a pair. A literal-only
  gate is still caught wherever it is used in a branch.
* **A gate assembled at runtime** -- a mapping appended to, or a status returned
  by a helper that takes no vocabulary reference. Each is a deliberate evasion
  rather than the accident this audit exists to catch.

**The sweep is empty today, and that is the load-bearing problem.** There is no
`policies` app and no `PolicyPass` subclass anywhere in `src/`, so "no policy
module defines its own gate" is true of a tree with no policies in it. So the
detector is measured: against `core/confidence.py` itself, which must be
reported, and against synthetic sources that re-implement the gate in every
spelling above -- with the shapes that merely call the gate, or test a confidence
for another reason, asserted not to be reported. The synthetic sources are parsed
in memory rather than placed under `src/` or `tests/`: a fixture module in the
tree would be found by this sweep and by every other audit in this repository.

Reads and parses repository files and nothing else: no database, no network, no
subprocess. It is the sixth sweep of the tree in this suite and
`tests/source_scan.parse` is uncached, which is a cost worth watching rather than
one worth a refactor here.
"""

from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING
from typing import Final
from typing import NamedTuple

import pytest

from conda_sentinel.core import confidence as confidence_module
from conda_sentinel.core.outcomes import OutcomeState
from conda_sentinel.identity.models import IdentityConfidence
from tests.source_scan import REPO_ROOT
from tests.source_scan import SRC_ROOT
from tests.source_scan import dotted_name
from tests.source_scan import parse
from tests.source_scan import project_files

if TYPE_CHECKING:
    from collections.abc import Callable
    from collections.abc import Iterable
    from collections.abc import Iterator
    from collections.abc import Mapping


def declaring_module(module: ModuleType) -> Path:
    """Return the file a module was loaded from, refusing a module that has none.

    Read off the module object rather than rebuilt from the second import root's
    layout: that layout is `tests/unit/test_import_roots.py`'s to assert, and a
    hand-built path here would be a second copy of it that could stop pointing at
    anything without saying so.

    Args:
        module: The module whose source file is wanted.

    Returns:
        The resolved path to its source.

    Raises:
        RuntimeError: When the module has no `__file__`, or carries `None` there.
            A hard failure rather than a fallback: `Path("")` resolves to the
            working directory, which is a real path that simply is not this
            module, so the audit would exempt nothing, report the declaring
            module as an offender, and take its own anti-vacuity guard down with
            it.

    """
    source: str | None = getattr(module, "__file__", None)
    if source is None:
        message = (
            f"{module.__name__} has no __file__, so the one module CPM-AD-4 exempts cannot be identified and "
            f"the confidence gate audit cannot run."
        )
        raise RuntimeError(message)
    return Path(source).resolve()


#: The one module permitted to implement the gate.
DECLARING_MODULE: Final[Path] = declaring_module(confidence_module)

#: This module, named so the repository-wide sweep can be asserted to still reach
#: the test tree. The ban covers `tests/` deliberately, and an exclusion added to
#: `tests/source_scan.py` for some other reason could quietly take half of that
#: coverage away while every case here stayed green.
THIS_MODULE: Final[Path] = Path(__file__).resolve()

#: The two vocabulary classes, by name. A dotted chain is recognised by the
#: segment before its member -- `IdentityConfidence.UNMAPPED`,
#: `identity.models.IdentityConfidence.UNMAPPED` and
#: `models.IdentityConfidence.UNMAPPED` are one spelling to this audit -- so an
#: import route this file never thought of resolves anyway. Only a *renamed*
#: class needs the alias sets `module_names` builds.
CONFIDENCE_CLASS: Final[str] = "IdentityConfidence"
OUTCOME_CLASS: Final[str] = "OutcomeState"

#: The gate's own module, by name, so `confidence.GATED_VALUE` reached through any
#: alias is recognised as a status: a second gate assembled out of the first one's
#: constants is still a second gate.
GATE_MODULE: Final[str] = "confidence"

#: The confidence the gate has a rule about, by member name and by value, and the
#: whole vocabulary beside it. Read off `IdentityConfidence` rather than written
#: out: `core/confidence.py` argues that restating the three values anywhere
#: creates the second vocabulary `identity/models.py` fixed the spelling once to
#: prevent, and an audit about that rule breaking it would be an odd way to
#: enforce it.
GATED_MEMBER: Final[str] = IdentityConfidence.UNMAPPED.name
GATED_CONFIDENCE: Final[str] = IdentityConfidence.UNMAPPED.value
CONFIDENCE_MEMBERS: Final[frozenset[str]] = frozenset(member.name for member in IdentityConfidence)
CONFIDENCE_VALUES: Final[frozenset[str]] = frozenset(IdentityConfidence.values)

#: The sentinels every outcome vocabulary carries, by member name and by value.
#: `core/outcomes.py` composes a per-domain type for each pass and each inherits
#: these by name and value, so a member named `UNKNOWN` on any class-shaped prefix
#: is a status -- which is what makes this audit reach the passes it was written
#: for rather than only the base vocabulary they are built from.
STATUS_MEMBERS: Final[frozenset[str]] = frozenset(member.name for member in OutcomeState)
STATUS_VALUES: Final[frozenset[str]] = frozenset(OutcomeState.values)

#: The attribute that turns a member reference into its stored string.
#: `IdentityConfidence.UNMAPPED` and `IdentityConfidence.UNMAPPED.value` are the
#: same operand as far as a test is concerned.
VALUE_ATTRIBUTE: Final[str] = "value"

#: Calls that are a spelling of a literal rather than a handoff: a name bound to
#: `frozenset({...})` holds what the braces hold.
SEQUENCE_CONSTRUCTORS: Final[frozenset[str]] = frozenset({"frozenset", "list", "set", "sorted", "tuple"})

#: The mapping constructor, so `dict(unmapped=...)` is read as the table it is.
MAPPING_CONSTRUCTOR: Final[str] = "dict"

#: What a finding at module level is said to sit in.
MODULE_SCOPE: Final[str] = "<module>"

#: The three offences, and the two shapes of test that are not offences. Named
#: rather than spelled at each site because `RECORDED_EXEMPTIONS` is keyed on the
#: last two.
GATE_FORM: Final[str] = "a status selected on an identity confidence"
TABLE_FORM: Final[str] = "a table from confidence values to statuses"
COMPARISON_FORM: Final[str] = "a comparison against an identity confidence, selecting no status"
PATTERN_FORM: Final[str] = "a match case on an identity confidence, selecting no status"


class Finding(NamedTuple):
    """One thing a detector found, with enough of its context to be recorded.

    The line makes a failure actionable, and `where` -- the enclosing function --
    is what makes an exemption entry describe something. An entry counting "one
    comparison in `identity/services.py`" survives that comparison being deleted
    and a different one appearing elsewhere in the file; an entry naming the
    function does not.

    A tuple rather than a formatted string because three call sites used to
    reconstruct the form by splitting on `": "`, which made the message format
    load-bearing and untested.
    """

    line: int
    form: str
    where: str

    @property
    def record(self) -> str:
        """Return the spelling `RECORDED_EXEMPTIONS` is keyed on.

        Returns:
            The form and the scope it sits in, without the line: a decision is
            recorded about a rule in a function, and the rule keeps its meaning
            when the lines above it move.

        """
        return f"{self.form} in {self.where}"

    @property
    def describe(self) -> str:
        """Return the spelling a failure message uses.

        Returns:
            The line, the form and the scope.

        """
        return f"{self.line}: {self.record}"


#: Every place in shipped code where a confidence is tested and no status is
#: selected -- by file, by form, by enclosing function, counted. The record that
#: keeps this detector's precision a decision somebody wrote down rather than a
#: property nobody checked.
#:
#: Two entries. `identity/services.py`'s `_require_confidence_is_earned` refuses a
#: resolution claiming a confidence above `unmapped` while nothing was
#: established; `record_resolution` holds the `CPM-FR-2` downgrade refusal, which
#: compares the *stored* confidence against `verified` twice and selects no
#: status either. Both are rules about what a resolver may record on the identity
#: row, not about what the system may claim outward on a rollup row: `CPM-AD-4`
#: binds the second, `CPM-FR-1` and `CPM-FR-2` the first, and they meet only in
#: naming the same three values.
#:
#: `workflow/opening.py`'s `_open_identity_items` (`CPM-OPERATE-S11`) opens an
#: identity review item for each package the selection offers *at* `unmapped`
#: and for no other: the selection offers every unresolved confidence, and the
#: identity queue is for the one the gate blanks everything on. It was a
#: queryset filter on `Package` before the opening became cut-off bound -- the
#: same test, in SQL, which this scan does not read -- and is a comparison now
#: because the selection hands over packages rather than a queryset. It selects
#: no status, writes no rollup column, and is the rule that always decided which
#: packages get identity work.
#:
#: Counted and scoped, not merely named. An entry keyed by file alone would
#: licence the whole file, so the *next* test added to `identity/services.py`
#: would be silently permitted -- and an entry keyed by file and count alone would
#: survive the exempted rule being deleted and a different one taking its place.
RECORDED_EXEMPTIONS: Final[dict[str, dict[str, int]]] = {
    "django_apps/conda_sentinel/identity/services.py": {
        f"{COMPARISON_FORM} in _require_confidence_is_earned": 1,
        f"{COMPARISON_FORM} in record_resolution": 2,
    },
    "django_apps/conda_sentinel/workflow/opening.py": {
        f"{COMPARISON_FORM} in _open_identity_items": 1,
    },
}

#: The gate's one caller, named so the sweep can be asserted to still reach it.
#: It carries no entry above and that is a recorded state rather than an
#: oversight: `core/rollup.py` compares nothing. It calls `gated_status` and
#: `require_known_confidence`, and routes the field *default* through the gate as
#: well. The day it grows a test of its own is the day this audit should be in the
#: way, and it can only be in the way if the file is still in view.
#:
#: The same path is bound in the sibling audit as `THE_ROLLUP_WRITER`, and a
#: second independently-editable copy of a path is what `tests/source_scan.py` was
#: extracted to prevent. Rather than import one test module into another, the two
#: are reconciled below by reading the sibling's source -- the idiom that module
#: already uses for the derived-status convention it shares with a third audit.
THE_GATES_ONE_CALLER: Final[str] = "django_apps/conda_sentinel/core/rollup.py"

#: The sibling that binds the same path, and the name it binds it to.
SIBLING_AUDIT: Final[str] = "test_derived_status_writability_audit.py"
SIBLING_NAME: Final[str] = "THE_ROLLUP_WRITER"

# Synthetic modules the detectors are measured against. Source text parsed here
# rather than fixture files, for the reason the module docstring gives.
#
# The literal spellings below are interpolated from the vocabulary rather than
# typed out, because a literal `"unmapped"` written into this file would be
# exactly the second spelling of a fixed value that `core/confidence.py` refuses
# to create -- and interpolating means these cases follow the value if it is ever
# respelled, instead of quietly becoming cases about nothing.
A_SECOND_GATE_BY_MEMBER = """
from conda_sentinel.core.outcomes import OutcomeState
from conda_sentinel.identity.models import IdentityConfidence


def currency_status(package, verdict):
    if package.confidence == IdentityConfidence.UNMAPPED:
        return OutcomeState.UNKNOWN.value
    return verdict
"""

A_SECOND_GATE_BY_LITERAL = f'''
def currency_status(package, verdict):
    """The three lines this whole audit exists to make loud."""
    if package.confidence == "{GATED_CONFIDENCE}":
        return "{confidence_module.GATED_VALUE}"
    return verdict
'''

A_SECOND_GATE_THROUGH_A_MODULE_ALIAS = """
from conda_sentinel.core import outcomes as vocabulary
from conda_sentinel.identity import models as identity_models


def licence_status(package, verdict):
    if package.confidence == identity_models.IdentityConfidence.UNMAPPED:
        return vocabulary.OutcomeState.UNKNOWN.value
    return verdict
"""

A_SECOND_GATE_THROUGH_A_PACKAGE_ATTRIBUTE = """
from conda_sentinel import core
from conda_sentinel import identity


def feedstock_status(package, verdict):
    if package.confidence == identity.models.IdentityConfidence.UNMAPPED:
        return core.outcomes.OutcomeState.UNKNOWN.value
    return verdict
"""

A_SECOND_GATE_THROUGH_A_RELATIVE_IMPORT = """
from . import models as vocabulary
from ..core.outcomes import OutcomeState as State


def readiness_status(package, verdict):
    if package.confidence == vocabulary.IdentityConfidence.UNMAPPED:
        return State.UNKNOWN.value
    return verdict
"""

A_SECOND_GATE_FROM_THE_TRUSTED_SIDE = """
from conda_sentinel.core.outcomes import OutcomeState
from conda_sentinel.identity.models import IdentityConfidence

TRUSTED = {IdentityConfidence.VERIFIED, IdentityConfidence.INVENTORY_DERIVED}


def priority_status(package, verdict):
    if package.confidence in TRUSTED:
        return verdict
    return OutcomeState.UNKNOWN.value
"""

A_SECOND_GATE_BY_INEQUALITY = """
from conda_sentinel.core.outcomes import OutcomeState
from conda_sentinel.identity.models import IdentityConfidence


def vulnerability_status(package, verdict):
    if package.confidence != IdentityConfidence.VERIFIED:
        return OutcomeState.UNKNOWN.value
    return verdict
"""

A_SECOND_GATE_BY_MATCH = """
from conda_sentinel.core.outcomes import OutcomeState
from conda_sentinel.identity.models import IdentityConfidence


def currency_status(package, verdict):
    match package.confidence:
        case IdentityConfidence.UNMAPPED:
            return OutcomeState.UNKNOWN.value
        case _:
            return verdict
"""

A_SECOND_GATE_BY_MATCH_ON_LITERALS = f"""
def currency_status(package, verdict):
    match package.confidence:
        case "{GATED_CONFIDENCE}":
            return "{confidence_module.GATED_VALUE}"
        case _:
            return verdict
"""

A_SECOND_GATE_AS_A_TABLE = """
from conda_sentinel.core.outcomes import OutcomeState
from conda_sentinel.identity.models import IdentityConfidence

CLAIMABLE = {
    IdentityConfidence.UNMAPPED: OutcomeState.UNKNOWN,
    IdentityConfidence.VERIFIED: OutcomeState.OK,
}
"""

A_SECOND_GATE_AS_A_TABLE_BUILT_BY_DICT = f"""
from conda_sentinel.core.outcomes import OutcomeState

CLAIMABLE = dict({GATED_CONFIDENCE}=OutcomeState.UNKNOWN.value)
"""

A_SECOND_GATE_AS_A_TABLE_COMPREHENSION = """
from conda_sentinel.core.outcomes import OutcomeState
from conda_sentinel.identity.models import IdentityConfidence

CLAIMABLE = {confidence: OutcomeState.UNKNOWN.value for confidence in IdentityConfidence}
"""

A_SECOND_GATE_AS_A_TABLE_WITH_AN_EXPANSION = """
from conda_sentinel.core.outcomes import OutcomeState
from conda_sentinel.identity.models import IdentityConfidence

BASE = {}
CLAIMABLE = {**BASE, IdentityConfidence.UNMAPPED: OutcomeState.UNKNOWN.value}
"""

A_SECOND_GATE_AS_A_CONDITIONAL_EXPRESSION = """
from conda_sentinel.core.outcomes import OutcomeState
from conda_sentinel.identity.models import IdentityConfidence


def readiness_status(package, verdict):
    return OutcomeState.UNKNOWN.value if package.confidence == IdentityConfidence.UNMAPPED else verdict
"""

A_SECOND_GATE_SHORT_CIRCUITED = """
from conda_sentinel.core.outcomes import OutcomeState
from conda_sentinel.identity.models import IdentityConfidence


def licence_status(package, verdict):
    return (package.confidence == IdentityConfidence.UNMAPPED and OutcomeState.UNKNOWN.value) or verdict
"""

A_SECOND_GATE_IN_A_LOOP = """
from conda_sentinel.core.outcomes import OutcomeState
from conda_sentinel.identity.models import IdentityConfidence


def drain(packages, verdicts):
    while packages[0].confidence == IdentityConfidence.UNMAPPED:
        verdicts[packages.pop(0)] = OutcomeState.UNKNOWN.value
    return verdicts
"""

A_SECOND_GATE_THROUGH_A_BOUND_CONFIDENCE = """
from conda_sentinel.core.outcomes import OutcomeState
from conda_sentinel.identity.models import IdentityConfidence

UNMAPPED = IdentityConfidence.UNMAPPED


def currency_status(package, verdict):
    if package.confidence == UNMAPPED:
        return OutcomeState.UNKNOWN.value
    return verdict
"""

A_SECOND_GATE_BY_MEMBERSHIP_IN_A_BOUND_TUPLE = """
from conda_sentinel.core.outcomes import OutcomeState
from conda_sentinel.identity.models import IdentityConfidence

GATED = (IdentityConfidence.UNMAPPED,)
DEGRADED = OutcomeState.UNKNOWN.value
NOT_CLAIMED = DEGRADED


def licence_status(package, verdict):
    if package.confidence in GATED:
        return NOT_CLAIMED
    return verdict
"""

A_SECOND_GATE_ON_A_COMPOSED_TYPE = """
from conda_sentinel.identity.models import IdentityConfidence

from policies.licence.outcomes import LicenceOutcome


def licence_status(package, verdict):
    if package.confidence == IdentityConfidence.UNMAPPED:
        return LicenceOutcome.UNKNOWN.value
    return verdict
"""

A_SECOND_GATE_INSIDE_A_RETURNED_MAPPING = """
from conda_sentinel.core.outcomes import OutcomeState
from conda_sentinel.identity.models import IdentityConfidence


def evaluate(package, verdict):
    if package.confidence == IdentityConfidence.UNMAPPED:
        return {"currency_status": OutcomeState.UNKNOWN.value}
    return {"currency_status": verdict}
"""

A_SECOND_GATE_THROUGH_A_HELPER_CALL = """
from conda_sentinel.identity.models import IdentityConfidence

from policies.licence.outcomes import LicenceOutcome


def evaluate(package, verdict, degrade):
    if package.confidence == IdentityConfidence.UNMAPPED:
        return degrade(LicenceOutcome.UNKNOWN)
    return verdict
"""

A_SECOND_GATE_ACCUMULATED = """
from conda_sentinel.core.outcomes import OutcomeState
from conda_sentinel.identity.models import IdentityConfidence


def evaluate(package):
    claimed = []
    if package.confidence == IdentityConfidence.UNMAPPED:
        claimed += [OutcomeState.UNKNOWN.value]
    return claimed
"""

A_SECOND_GATE_BUILT_FROM_THE_FIRST = f"""
from conda_sentinel.core.confidence import GATED_VALUE


def feedstock_status(package, verdict):
    if package.confidence == "{GATED_CONFIDENCE}":
        return GATED_VALUE
    return verdict
"""

A_SECOND_GATE_BUILT_FROM_THE_FIRSTS_MODULE = """
from conda_sentinel.core import confidence as gate
from conda_sentinel.identity.models import IdentityConfidence


def feedstock_status(package, verdict):
    if package.confidence == IdentityConfidence.UNMAPPED:
        return gate.GATED_VALUE
    return verdict
"""

CALLS_THE_GATE = """
from conda_sentinel.core.confidence import gated_status


def currency_status(package, verdict):
    return gated_status(verdict, confidence=package.confidence)
"""

A_RESOLUTION_TIME_RULE = """
from conda_sentinel.identity.models import IdentityConfidence


def require_confidence_is_earned(confidence, established):
    if confidence == IdentityConfidence.UNMAPPED or established:
        return
    raise ResolutionError("a resolution claiming more than it established")
"""

AN_ASSERTION_ABOUT_A_CONFIDENCE = """
from conda_sentinel.identity.models import IdentityConfidence


def test_a_shell_is_unmapped(package):
    if package is None:
        raise AssertionError("no package")
    assert package.confidence == IdentityConfidence.UNMAPPED
"""

A_STATUS_CHOSEN_FOR_ANOTHER_REASON = """
from conda_sentinel.core.outcomes import OutcomeState


def currency_status(evidence, verdict):
    if evidence is None:
        return OutcomeState.NOT_FOUND.value
    return verdict
"""

A_MATCH_ON_SOMETHING_ELSE = """
from conda_sentinel.core.outcomes import OutcomeState


def evidence_status(kind, verdict):
    match kind:
        case "missing":
            return OutcomeState.NOT_FOUND.value
        case _:
            return verdict
"""

A_FIXTURE_TABLE_OF_LITERALS = f'''
"""Expected rows for a read-surface case, which is data rather than a rule."""

EXPECTED = {{"verified": "ok", "{GATED_CONFIDENCE}": "{confidence_module.GATED_VALUE}"}}
'''

PROSE_ONLY = f'''
"""What a pass must never write.

A pass never asks whether the confidence is {GATED_CONFIDENCE!r} and writes
{confidence_module.GATED_VALUE!r} itself: it calls core.confidence.gated_status,
which is the one place that comparison is made.
"""

RENAMED = {{"numpy": "numpy-base"}}


def canonical(name):
    # if confidence == unmapped: return unknown -- the shape this module forbids.
    if name in RENAMED:
        return RENAMED[name]
    return name
'''


class ModuleNames(NamedTuple):
    """How one module spells the two vocabularies and the gate's own exports.

    Computed once per module and threaded through the detectors: the same class
    is `IdentityConfidence`, `Confidence` and `identity.models.IdentityConfidence`
    in three files that all import it, and a status is whatever name the module
    bound one to.
    """

    confidences: frozenset[str]
    outcomes: frozenset[str]
    gates: frozenset[str]
    bindings: Mapping[str, ast.expr]


def _class_aliases(tree: ast.Module, class_name: str) -> frozenset[str]:
    """Return the names one module binds a vocabulary class to.

    Only *renamings* need collecting. A dotted chain that still carries the
    class's own name is recognised by that segment however it was reached -- an
    aliased module, a relative import, package-then-attribute access -- so the
    import forms this function would otherwise have to enumerate resolve without
    it. What it cannot resolve is `import IdentityConfidence as Confidence`, and
    that is what this is for, plus the class's own declaring module, which
    declares rather than imports it.

    Args:
        tree: The parsed module.
        class_name: The class to resolve.

    Returns:
        The bare names that resolve to the class there.

    """
    aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            aliases.add(node.name)
        elif isinstance(node, ast.ImportFrom):
            aliases.update(alias.asname or alias.name for alias in node.names if alias.name == class_name)
    return frozenset(aliases)


def _gate_names(tree: ast.Module) -> frozenset[str]:
    """Return the names one module reaches `core/confidence.py` through.

    Both kinds in one set: a bare name imported *from* the gate module -- its
    `GATED_VALUE` above all -- and a prefix naming the module itself, so
    `gate.GATED_VALUE` under any alias is a status too. A second gate assembled
    out of the first one's constants is still a second gate, and it is the form a
    developer arrives at after being told not to hard-code `unknown`.

    Args:
        tree: The parsed module.

    Returns:
        The names and prefixes that reach the gate module.

    """
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported = (node.module or "").rpartition(".")[2]
            for alias in node.names:
                if GATE_MODULE in {imported, alias.name}:
                    names.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            names.update(
                alias.asname or alias.name for alias in node.names if alias.name.rpartition(".")[2] == GATE_MODULE
            )
    return frozenset(names)


def _bindings(tree: ast.Module) -> dict[str, ast.expr]:
    """Return what each name in one module is bound to, by simple assignment.

    The last binding wins, which is what a reader of the file would assume. Only
    plain `name = value` forms are collected: a name bound by a `for` target, a
    walrus or an argument default carries no static value worth resolving.

    Args:
        tree: The parsed module.

    Returns:
        Name to assigned expression.

    """
    bound: dict[str, ast.expr] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign | ast.AnnAssign) or node.value is None:
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for target in targets:
            if isinstance(target, ast.Name):
                bound[target.id] = node.value
    return bound


def module_names(tree: ast.Module) -> ModuleNames:
    """Return every spelling one module uses for the vocabularies and the gate.

    Args:
        tree: The parsed module.

    Returns:
        The name sets and bindings the detectors read.

    """
    return ModuleNames(
        confidences=_class_aliases(tree, CONFIDENCE_CLASS),
        outcomes=_class_aliases(tree, OUTCOME_CLASS),
        gates=_gate_names(tree),
        bindings=_bindings(tree),
    )


def _qualnames(tree: ast.Module) -> dict[int, str]:
    """Return the enclosing scope of every node, by node identity.

    What an exemption entry is *about*: a rule lives in a function, and recording
    the function is what makes an entry fail when that rule is deleted and a
    different one appears elsewhere in the same file.

    Args:
        tree: The parsed module.

    Returns:
        `id(node)` to the dotted name of its enclosing function or class, or
        `MODULE_SCOPE`.

    """
    found: dict[int, str] = {id(tree): MODULE_SCOPE}

    def descend(node: ast.AST, scope: str) -> None:
        for child in ast.iter_child_nodes(node):
            inner = scope
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                inner = child.name if scope == MODULE_SCOPE else f"{scope}.{child.name}"
            found[id(child)] = inner
            descend(child, inner)

    descend(tree, MODULE_SCOPE)
    return found


def _without_value(dotted: str) -> str:
    """Return a dotted name with a trailing `.value` removed.

    `IdentityConfidence.UNMAPPED` and `IdentityConfidence.UNMAPPED.value` are the
    same operand to a test -- one the member, one its stored string -- and a
    detector that recognised only the first would be evaded by typing six
    characters.

    Args:
        dotted: The dotted source spelling.

    Returns:
        The spelling without its trailing `.value`, or unchanged.

    """
    head, _, tail = dotted.rpartition(".")
    return head if head and tail == VALUE_ATTRIBUTE else dotted


def _direct_confidence(node: ast.expr, names: ModuleNames) -> bool:
    """Report whether one expression names an identity confidence, without resolving names.

    Args:
        node: The expression to inspect.
        names: The spellings this module uses.

    Returns:
        True for a member reference through any route -- the class's own name
        anywhere in the chain, or a renamed spelling this module bound -- for its
        `.value`, and for one of the vocabulary's stored strings written as a
        literal.

    """
    if isinstance(node, ast.Constant):
        return node.value in CONFIDENCE_VALUES
    prefix, _, member = _without_value(dotted_name(node)).rpartition(".")
    if member not in CONFIDENCE_MEMBERS or not prefix:
        return False
    return prefix.rpartition(".")[2] == CONFIDENCE_CLASS or prefix in names.confidences


def _direct_status(node: ast.expr, names: ModuleNames) -> bool:
    """Report whether one expression is a status value, without resolving names.

    Args:
        node: The expression to inspect.
        names: The spellings this module uses.

    Returns:
        True for a sentinel-named member on any class-shaped prefix -- which is
        what reaches the per-domain types `outcome_type` composes -- for a name or
        attribute reaching the gate's own exports, and for one of the sentinels'
        stored strings written as a literal.

    """
    if isinstance(node, ast.Constant):
        return node.value in STATUS_VALUES
    if isinstance(node, ast.Name):
        return node.id in names.gates
    prefix, _, member = _without_value(dotted_name(node)).rpartition(".")
    if not prefix:
        return False
    if prefix in names.gates or prefix.rpartition(".")[2] == GATE_MODULE:
        return True
    if prefix in names.outcomes or prefix.rpartition(".")[2] == OUTCOME_CLASS:
        return member.isupper()
    return member in STATUS_MEMBERS and prefix.rpartition(".")[2][:1].isupper()


def _candidates(bound: ast.expr) -> list[ast.expr]:
    """Return the expressions a bound name could be said to hold.

    The value itself, and the elements of a literal collection or of a call
    spelling one: `GATED = (IdentityConfidence.UNMAPPED,)` holds a confidence, and
    `confidence in GATED` tests one.

    Deliberately narrow rather than a walk of the whole expression. A name bound
    to `a == VERIFIED and b != VERIFIED` holds a boolean, not a confidence, and a
    walk would make every branch on that boolean a confidence test.

    Args:
        bound: The assigned expression.

    Returns:
        The value and, where it is a collection, its elements.

    """
    if isinstance(bound, ast.Tuple | ast.List | ast.Set):
        return [bound, *bound.elts]
    if isinstance(bound, ast.Call) and dotted_name(bound.func).rpartition(".")[2] in SEQUENCE_CONSTRUCTORS:
        return [bound, *(element for argument in bound.args for element in _candidates(argument))]
    return [bound]


def _resolves(
    node: ast.expr, names: ModuleNames, direct: Callable[[ast.expr, ModuleNames], bool], seen: frozenset[str]
) -> bool:
    """Report whether an expression is one of the vocabularies, following bindings.

    Runs to a fixed point: `DEGRADED = OutcomeState.UNKNOWN.value` followed by
    `NOT_CLAIMED = DEGRADED` leaves the second name holding a status, and a
    resolver stopping after one hop would be defeated by an alias of an alias.
    `seen` is what keeps a cyclic binding from recurring forever.

    Args:
        node: The expression to inspect.
        names: The spellings this module uses.
        direct: The unresolved test to apply.
        seen: Names already followed on this path.

    Returns:
        True when the expression, or anything a name in it is bound to, satisfies
        `direct`.

    """
    if direct(node, names):
        return True
    if not isinstance(node, ast.Name) or node.id in seen:
        return False
    bound = names.bindings.get(node.id)
    if bound is None:
        return False
    return any(_resolves(candidate, names, direct, seen | {node.id}) for candidate in _candidates(bound))


def is_confidence(node: ast.expr, names: ModuleNames) -> bool:
    """Report whether one expression names an identity confidence.

    Args:
        node: The expression to inspect.
        names: The spellings this module uses.

    Returns:
        True for a member reference, a stored string, or a name bound to either.

    """
    return _resolves(node, names, _direct_confidence, frozenset())


def is_status(node: ast.expr, names: ModuleNames) -> bool:
    """Report whether one expression is a status value.

    Args:
        node: The expression to inspect.
        names: The spellings this module uses.

    Returns:
        True for a sentinel member on any outcome vocabulary, a stored string, a
        gate export, or a name bound to any of those.

    """
    return _resolves(node, names, _direct_status, frozenset())


def _holds_a_status(node: ast.expr, names: ModuleNames) -> bool:
    """Report whether one expression carries a status anywhere inside it.

    Walked rather than tested directly, because `PolicyPass.evaluate` returns a
    `Mapping[str, str]`: `return {"currency_status": UNKNOWN}` is the normal shape
    of a pass's answer, and so is handing the value to a helper on the way out.

    Args:
        node: The expression to inspect.
        names: The spellings this module uses.

    Returns:
        True when any sub-expression is a status.

    """
    return any(is_status(child, names) for child in ast.walk(node) if isinstance(child, ast.expr))


def _names_the_vocabulary(node: ast.expr, names: ModuleNames) -> bool:
    """Report whether one expression names the confidence vocabulary itself.

    For the comprehension form: `{c: UNKNOWN for c in IdentityConfidence}` builds
    the same table as a dict literal, and its keys are a loop variable that names
    nothing on its own.

    Args:
        node: The expression to inspect.
        names: The spellings this module uses.

    Returns:
        True for the class, and for `.values`, `.choices` and any other lowercase
        attribute of it.

    """
    dotted = dotted_name(node)
    while dotted and dotted.rpartition(".")[2][:1].islower():
        dotted = dotted.rpartition(".")[0]
    return bool(dotted) and (dotted.rpartition(".")[2] == CONFIDENCE_CLASS or dotted in names.confidences)


def _operands(compare: ast.Compare) -> Iterator[ast.expr]:
    """Yield the expressions one comparison compares.

    Its own operands, and the elements of a literal collection among them:
    `confidence in (UNMAPPED,)` tests the member exactly as `confidence ==
    UNMAPPED` does.

    Args:
        compare: The comparison node.

    Yields:
        Each operand, and each element of a tuple, list or set operand.

    """
    for operand in [compare.left, *compare.comparators]:
        yield operand
        if isinstance(operand, ast.Tuple | ast.List | ast.Set):
            yield from operand.elts


def _tests_a_confidence(test: ast.expr, names: ModuleNames) -> bool:
    """Report whether one condition tests an identity confidence.

    Walks the condition rather than reading its top node, so a comparison buried
    in a `not (...)` or one side of an `and` is found -- which is where a real
    second gate hides.

    Args:
        test: The condition expression.
        names: The spellings this module uses.

    Returns:
        True when any comparison in it has a confidence operand.

    """
    return any(
        is_confidence(operand, names)
        for node in ast.walk(test)
        if isinstance(node, ast.Compare)
        for operand in _operands(node)
    )


def _branch_statements(statements: Iterable[ast.stmt]) -> Iterator[ast.stmt]:
    """Yield the statements a branch executes, without entering a nested definition.

    A closure or a class defined under a confidence test is not that test
    selecting a status: it is a definition that happens to sit there, and the
    status it returns is selected whenever it is called, by whatever condition the
    caller applies.

    Args:
        statements: The branch's statements.

    Yields:
        Each statement, outermost first, descending through compound statements
        but stopping at a `def` or a `class`.

    """
    for statement in statements:
        if isinstance(statement, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            continue
        yield statement
        for child in ast.iter_child_nodes(statement):
            if isinstance(child, ast.stmt):
                yield from _branch_statements([child])
            elif isinstance(child, ast.ExceptHandler | ast.match_case):
                yield from _branch_statements(child.body)


def _assigned_value(node: ast.stmt) -> ast.expr | None:
    """Return the value a statement returns or assigns, if it is one of those.

    Args:
        node: The statement to inspect.

    Returns:
        The expression, or None for every other kind of statement. A bare
        `return`, a `raise` and a log call all return None, which is the whole of
        the distinction this audit rests on.

    """
    if isinstance(node, ast.Return | ast.Assign | ast.AnnAssign | ast.AugAssign):
        return node.value
    return None


def _selects_a_status(statements: Iterable[ast.stmt], names: ModuleNames) -> bool:
    """Report whether a region of code writes a status value.

    `CPM-AD-4`'s "expressed as writing a value". A statement that raises, returns
    nothing or logs selects nothing -- which is what leaves a resolution-time
    refusal alone. Both sides of a test are read: `if unmapped: raise` with an
    `else: status = OK` still chooses a status on a confidence, and which side it
    was written on is not the question.

    Args:
        statements: The region's statements.
        names: The spellings this module uses.

    Returns:
        True when any of them returns or assigns something carrying a status.

    """
    return any(
        _holds_a_status(value, names)
        for statement in _branch_statements(statements)
        if (value := _assigned_value(statement)) is not None
    )


def _is_table_entry(key: ast.expr | None, value: ast.expr, names: ModuleNames) -> bool:
    """Report whether one key and value are a row of the gate written as a table.

    At least one side must be a *reference* rather than a bare string. A mapping
    written entirely in literals is as often fixture data as it is a rule, and the
    ban has no exemption path: reporting it would leave a reader with no move
    except deleting real test data. The module docstring records that concession.

    Args:
        key: The key, or None where a `**expansion` stands in place of one.
        value: The value.
        names: The spellings this module uses.

    Returns:
        True for a confidence key and a status value, at least one of them named.

    """
    if key is None or not (is_confidence(key, names) and is_status(value, names)):
        return False
    return not (isinstance(key, ast.Constant) and isinstance(value, ast.Constant))


def _maps_a_confidence_to_a_status(node: ast.Dict | ast.DictComp | ast.Call, names: ModuleNames) -> bool:
    """Report whether one mapping is the gate written as a table.

    Three constructions, because a table is a table however it is built: a
    literal, a comprehension over the vocabulary, and `dict(unmapped=...)`.

    One entry is enough. Only one of the three confidences changes what may be
    claimed, so `{UNMAPPED: UNKNOWN}` is the complete rule -- unlike a precedence
    order, where a single pair is a fallback rather than a ranking.

    Args:
        node: The mapping expression.
        names: The spellings this module uses.

    Returns:
        True when it carries a confidence-to-status row.

    """
    if isinstance(node, ast.Dict):
        return any(_is_table_entry(key, value, names) for key, value in zip(node.keys, node.values, strict=True))
    if isinstance(node, ast.DictComp):
        keyed = is_confidence(node.key, names) or any(
            _names_the_vocabulary(generator.iter, names) for generator in node.generators
        )
        return keyed and is_status(node.value, names)
    if dotted_name(node.func).rpartition(".")[2] != MAPPING_CONSTRUCTOR:
        return False
    pairs: list[tuple[ast.expr | None, ast.expr]] = [
        (ast.Constant(value=keyword.arg), keyword.value) for keyword in node.keywords if keyword.arg is not None
    ]
    pairs.extend(
        (element.elts[0], element.elts[1])
        for argument in node.args
        for element in ast.walk(argument)
        if isinstance(element, ast.Tuple) and len(element.elts) == 2  # noqa: PLR2004 - a mapping row is a pair
    )
    return any(is_confidence(key, names) and is_status(value, names) for key, value in pairs if key is not None)


def _comparisons(node: ast.AST) -> list[ast.AST]:
    """Return every comparison inside one expression.

    Args:
        node: The expression to read.

    Returns:
        The `Compare` nodes, which is what `_scan` flags so that an offence's own
        test cannot reappear as a recordable comparison.

    """
    return [child for child in ast.walk(node) if isinstance(child, ast.Compare)]


def _branch_offence(
    node: ast.If | ast.While | ast.IfExp | ast.BoolOp,
    names: ModuleNames,
    following: list[ast.stmt],
) -> list[ast.AST] | None:
    """Return the confidence tests one branch-shaped node offends with.

    Four shapes, one rule: something tests a confidence, and a status is chosen on
    one side of it. `if`, `while` and the conditional expression are the same
    decision written three ways, and the boolean operation is that decision
    short-circuited into one expression -- `(confidence == UNMAPPED and UNKNOWN)
    or verdict`, which no branch detector sees.

    Args:
        node: The branch, loop, conditional expression or boolean operation.
        names: The spellings this module uses.
        following: The statements after it in its suite, for the implicit else.

    Returns:
        The comparisons to flag, or None when this is not an offence.

    """
    if isinstance(node, ast.BoolOp):
        test, selected = node, _holds_a_status(node, names)
    elif isinstance(node, ast.IfExp):
        test, selected = node.test, _holds_a_status(node.body, names) or _holds_a_status(node.orelse, names)
    else:
        test, selected = node.test, _selects_a_status(_region(node, following), names)
    if not selected or not _tests_a_confidence(test, names):
        return None
    return _comparisons(test)


def _match_offence(node: ast.Match, names: ModuleNames) -> list[ast.AST] | None:
    """Return the confidence patterns one `match` statement offends with.

    Read whole rather than case by case, because the wildcard is where the gate
    usually lands: `case UNMAPPED: return verdict` with `case _: return UNKNOWN`
    is the same rule as the other way round, and neither case on its own carries
    both halves of it.

    Args:
        node: The match statement.
        names: The spellings this module uses.

    Returns:
        The pattern values to flag, or None when no case names a confidence or no
        case selects a status.

    """
    claimed: list[ast.AST] = [
        pattern.value
        for case in node.cases
        for pattern in _pattern_values(case.pattern)
        if is_confidence(pattern.value, names)
    ]
    if not claimed or not any(_selects_a_status(case.body, names) for case in node.cases):
        return None
    return claimed


def _scan(tree: ast.Module) -> tuple[list[Finding], list[Finding]]:
    """Return what one module implements and what it merely tests.

    One pass for both, because the two answers are defined against each other: a
    confidence test that selects a status is an offence, and it must not also
    appear in the list an exemption entry can licence. That exclusion is
    structural rather than a matter of message wording -- there is no spelling of
    `RECORDED_EXEMPTIONS` that can wave a second gate through.

    Args:
        tree: The parsed module.

    Returns:
        The offences, and the confidence tests that select no status, both in line
        order. An offence is reported once per place however many nested
        expressions match it -- `(c == UNMAPPED and UNKNOWN) or verdict` is two
        boolean operations and one gate. The tests are not collapsed that way: two
        comparisons written on one line are two decisions, and the record counts
        each of them.

    """
    names = module_names(tree)
    scopes = _qualnames(tree)
    following = _following_statements(tree)
    offences: list[Finding] = []
    flagged: set[int] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.If | ast.While | ast.IfExp | ast.BoolOp):
            form, tested = GATE_FORM, _branch_offence(node, names, following.get(id(node), []))
        elif isinstance(node, ast.Match):
            form, tested = GATE_FORM, _match_offence(node, names)
        elif isinstance(node, ast.Dict | ast.DictComp | ast.Call):
            form, tested = TABLE_FORM, ([] if _maps_a_confidence_to_a_status(node, names) else None)
        else:
            continue
        if tested is None:
            continue
        offences.append(Finding(node.lineno, form, scopes.get(id(node), MODULE_SCOPE)))
        flagged.update(id(child) for child in tested)

    tests = [
        Finding(node.lineno, form, scopes.get(id(node), MODULE_SCOPE))
        for node, form in _confidence_tests(tree, names)
        if id(node) not in flagged
    ]
    return list(dict.fromkeys(sorted(offences))), sorted(tests)


def _confidence_tests(tree: ast.Module, names: ModuleNames) -> Iterator[tuple[ast.AST, str]]:
    """Yield every node that tests an identity confidence, offence or not.

    Args:
        tree: The parsed module.
        names: The spellings this module uses.

    Yields:
        The comparison or match value, and the form it takes. The node yielded is
        the one `_scan` flags, so an offence's own test cannot reappear as a
        recordable comparison.

    """
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare) and any(is_confidence(operand, names) for operand in _operands(node)):
            yield node, COMPARISON_FORM
        elif isinstance(node, ast.MatchValue) and is_confidence(node.value, names):
            yield node.value, PATTERN_FORM


def _pattern_values(pattern: ast.pattern) -> list[ast.MatchValue]:
    """Return every literal value one match pattern tests against.

    Args:
        pattern: The case's pattern.

    Returns:
        The `MatchValue` nodes anywhere inside it, so an `|` alternation and a
        nested sequence pattern are read as readily as a bare value.

    """
    return [node for node in ast.walk(pattern) if isinstance(node, ast.MatchValue)]


def _following_statements(tree: ast.Module) -> dict[int, list[ast.stmt]]:
    """Return the statements that follow each branch in its own suite.

    The implicit else. `if confidence in TRUSTED: return verdict` followed by
    `return UNKNOWN` is one rule written as two statements, and it is the shape a
    person reaches for when stating the gate positively.

    Args:
        tree: The parsed module.

    Returns:
        `id(statement)` to the statements after it, for every `if` and `while`.

    """
    found: dict[int, list[ast.stmt]] = {}
    for node in ast.walk(tree):
        for field in ("body", "orelse", "finalbody"):
            suite = getattr(node, field, None)
            if not isinstance(suite, list):
                continue
            for index, statement in enumerate(suite):
                if isinstance(statement, ast.If | ast.While):
                    found[id(statement)] = [after for after in suite[index + 1 :] if isinstance(after, ast.stmt)]
    return found


def _region(node: ast.If | ast.While, following: list[ast.stmt]) -> list[ast.stmt]:
    """Return the statements a branch decides between.

    Its own two sides, plus what follows it when a side ends in a `return`: the
    implicit else is part of the same decision. A side that ends in a `raise` does
    not extend the region -- a refusal is not a selection, and treating it as one
    would make every function that validates a confidence and later computes a
    status look like a gate.

    Args:
        node: The branch.
        following: The statements after it in its suite.

    Returns:
        The region to read for a status selection.

    """
    guarded = [*node.body, *node.orelse]
    returns = any(branch and isinstance(branch[-1], ast.Return) for branch in (node.body, node.orelse))
    return [*guarded, *following] if returns else guarded


def gate_implementations(tree: ast.Module) -> list[Finding]:
    """Return every second confidence gate one module implements.

    Args:
        tree: The parsed module.

    Returns:
        One finding per offence, in line order.

    """
    return _scan(tree)[0]


def confidence_tests(tree: ast.Module) -> list[Finding]:
    """Return every confidence test one module makes that selects no status.

    Args:
        tree: The parsed module.

    Returns:
        One finding per test, in line order. An offence's own test is not among
        them.

    """
    return _scan(tree)[1]


#: Every module the ban applies to: the whole repository, less the one module that
#: holds the gate. Repository-wide rather than `src/` only, on the ordering
#: audit's reasoning: a second gate written into a test is still a second answer
#: to "what may be claimed", and a test is the one place a wrong answer looks like
#: a passing one.
#:
#: Filtered out of the parametrize list rather than skipped inside the case,
#: because `tests/unit/test_suite_policy.py` bans a skip outright -- and rightly:
#: a skip here would report a green case for the one file where the assertion
#: means the most.
SUBJECT_MODULES: Final[tuple[Path, ...]] = tuple(path for path in project_files(REPO_ROOT) if path != DECLARING_MODULE)

#: The shipped modules the exemption record is about. Migrations are excluded:
#: generated code is not a rule anybody wrote.
SHIPPED_MODULES: Final[tuple[Path, ...]] = tuple(
    path for path in project_files(SRC_ROOT, skip_migrations=True) if path != DECLARING_MODULE
)


def test_the_scan_reaches_the_declaring_module_and_the_test_tree() -> None:
    """A scan that missed either half would pass on a repository it never read.

    Named rather than counted, for the reason `CPM-AD-4` is worth auditing at all:
    the file that must be exempt and the file that must be reported are the same
    file, so a sweep that stopped reaching it would go green in both directions at
    once. And the ban claims the test tree, so an exclusion that quietly dropped
    `tests/` would halve its coverage while every case here stayed green.
    """
    scanned = project_files(REPO_ROOT)

    assert DECLARING_MODULE in scanned, DECLARING_MODULE
    assert THIS_MODULE in scanned, f"the sweep no longer reaches {THIS_MODULE}, so the ban no longer covers tests/"


def test_the_declaring_module_implements_the_gate() -> None:
    """The anti-vacuity guard: the detector finds the one gate that must exist.

    `registered_passes()` is empty, there is no `policies` app, and nothing in the
    tree re-implements the gate today -- so every case below would pass on a
    detector that had stopped recognising anything at all. This is what makes the
    sweep's silence mean something: the detector fires on the real gate, in the
    real file, in the shape the ban is written about.
    """
    found = gate_implementations(parse(DECLARING_MODULE))

    assert found, f"{DECLARING_MODULE} implements no confidence gate, so the detector below is measuring nothing"
    assert [finding.form for finding in found] == [GATE_FORM], [finding.describe for finding in found]
    assert found[0].where == "gated_status", found[0].describe


@pytest.mark.parametrize("path", SUBJECT_MODULES, ids=lambda path: str(path.relative_to(REPO_ROOT)))
def test_no_module_but_core_implements_the_confidence_gate(path: Path) -> None:
    """`CPM-AD-4`: one gate, in `core`, never re-implemented per pass.

    Parametrized per file so that a violation names the module that introduced it,
    and reported with the line and the function rather than as a count, because
    the offending three lines are the whole of what a reader needs.

    There is no exemption from this ban. `RECORDED_EXEMPTIONS` records confidence
    tests that are *not* gates; a module that really does select a status on a
    confidence is the defect `CPM-AD-4` and `R-08` exist to prevent, and the fix is
    to call `core.confidence.gated_status` instead.
    """
    found = [finding.describe for finding in gate_implementations(parse(path))]

    assert found == [], f"{path.relative_to(REPO_ROOT)} implements a second confidence gate: {found}"


def test_the_shipped_sweep_reaches_the_modules_the_record_is_about() -> None:
    """The exemption record means nothing if its files have fallen out of the scan.

    A narrowed `project_files`, a renamed source root or a stray exclusion would
    otherwise leave the record green while nothing was being read -- and the two
    modules named here are the ones the record is a decision about.
    """
    relative = {path.relative_to(SRC_ROOT).as_posix() for path in SHIPPED_MODULES}

    assert relative >= RECORDED_EXEMPTIONS.keys(), sorted(RECORDED_EXEMPTIONS.keys() - relative)
    assert THE_GATES_ONE_CALLER in relative, (
        f"the scan no longer reaches {THE_GATES_ONE_CALLER}, which is the gate's one caller -- the module a "
        f"rule of its own would matter most in"
    )


def test_the_two_audits_name_the_same_rollup_writer() -> None:
    """The path above is checked against the sibling's copy, so the two cannot drift.

    `tests/unit/django_apps/test_derived_status_writability_audit.py` binds the
    same file as `THE_ROLLUP_WRITER`, and two independently-editable copies of one
    path is what `tests/source_scan.py` was extracted to prevent. Read out of the
    sibling's source rather than imported -- a test module is not a helper library
    -- which is the idiom that module already uses for the convention it shares
    with a third audit.
    """
    sibling = ast.parse((Path(__file__).parent / SIBLING_AUDIT).read_text(encoding="utf-8"))
    declared = [
        ast.literal_eval(node.value)
        for node in ast.walk(sibling)
        if isinstance(node, ast.AnnAssign | ast.Assign) and node.value is not None
        for target in ([node.target] if isinstance(node, ast.AnnAssign) else node.targets)
        if isinstance(target, ast.Name) and target.id == SIBLING_NAME
    ]

    assert declared == [THE_GATES_ONE_CALLER], (
        f"{SIBLING_AUDIT} no longer names {THE_GATES_ONE_CALLER} as {SIBLING_NAME}: {declared}"
    )


def test_the_gates_one_caller_tests_nothing() -> None:
    """`core/rollup.py` calls the gate; it does not restate it, and that is asserted.

    The rollup writer is where a second gate would be most natural to write: it
    already knows the package's confidence, it is already choosing what to put in
    every contributable column, and it routes the field *default* through the gate
    as well. It carries no exemption entry because it needs none, and this is the
    case that would notice the day that stopped being true.
    """
    tree = parse(SRC_ROOT / THE_GATES_ONE_CALLER)

    assert gate_implementations(tree) == []
    assert confidence_tests(tree) == []


@pytest.mark.parametrize("path", SHIPPED_MODULES, ids=lambda path: str(path.relative_to(SRC_ROOT)))
def test_no_shipped_module_tests_a_confidence_without_a_recorded_reason(path: Path) -> None:
    """A confidence test that is not a gate is a decision, and decisions are recorded.

    The detector is precise: it reports a test only where a status is selected on
    it. That precision is defensible exactly as long as somebody has looked at
    each test it passes over and said why -- otherwise "the detector does not fire
    on this" and "nobody has read this" are the same green case.

    So every confidence test in shipped code is counted against
    `RECORDED_EXEMPTIONS`, by form *and* by the function it sits in, and an
    unrecorded one fails here with its line. The remedy is to call the gate or to
    write the entry with its reason -- and that remedy is only ever offered for a
    test that selects nothing, because one that selects a status is an offence
    above and never appears in this list at all.
    """
    relative = path.relative_to(SRC_ROOT).as_posix()
    allowance = Counter(RECORDED_EXEMPTIONS.get(relative, {}))
    unrecorded: list[str] = []
    for finding in confidence_tests(parse(path)):
        if allowance[finding.record] > 0:
            allowance[finding.record] -= 1
        else:
            unrecorded.append(finding.describe)

    assert unrecorded == [], f"{relative} tests an identity confidence with no recorded reason: {unrecorded}"


def test_every_recorded_exemption_still_describes_the_file() -> None:
    """An entry that no longer describes real code is a licence nobody meant to leave.

    Reconciled in the direction the record is granted: the file must still exist,
    and must still contain the recorded form, in the recorded function, exactly as
    many times as the entry says. Delete `_require_confidence_is_earned` and this
    fails until the entry goes with it -- including when a different comparison
    appears elsewhere in the same file, which a count alone would have absorbed.
    Add a second and the case above fails from the other side.
    """
    stale: list[str] = []
    for relative, records in RECORDED_EXEMPTIONS.items():
        path = SRC_ROOT / relative
        if not path.is_file():
            stale.append(f"{relative}: recorded, but no such file")
            continue
        counted = Counter(finding.record for finding in confidence_tests(parse(path)))
        stale.extend(
            f"{relative}: {record} recorded {count}, found {counted.get(record, 0)}"
            for record, count in records.items()
            if counted.get(record, 0) != count
        )

    assert stale == [], f"the exemption record no longer describes the source: {stale}"


def test_a_recorded_exemption_cannot_licence_a_second_gate() -> None:
    """The two nets cannot be traded against each other, and this is why.

    The failure this forecloses: a gate the ban misses is caught by the second net
    as "a comparison selecting no status", whose remedy is to write an entry -- so
    the defect gets read as paperwork and legitimised with a sentence. A test that
    selects a status is excluded from the recordable list by construction, so
    there is no spelling of `RECORDED_EXEMPTIONS` that reaches it.

    Asserted twice: on a synthetic gate, whose comparison must be an offence and
    must not be recordable, and on every file the record actually names.
    """
    offences, recordable = _scan(ast.parse(A_SECOND_GATE_BY_MEMBER))

    assert [finding.form for finding in offences] == [GATE_FORM]
    assert recordable == [], f"a second gate is offered as a recordable comparison: {recordable}"

    for relative in RECORDED_EXEMPTIONS:
        found = [finding.describe for finding in gate_implementations(parse(SRC_ROOT / relative))]
        assert found == [], f"{relative} holds an exemption entry and a second gate: {found}"


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (A_SECOND_GATE_BY_MEMBER, GATE_FORM),
        (A_SECOND_GATE_BY_LITERAL, GATE_FORM),
        (A_SECOND_GATE_THROUGH_A_MODULE_ALIAS, GATE_FORM),
        (A_SECOND_GATE_THROUGH_A_PACKAGE_ATTRIBUTE, GATE_FORM),
        (A_SECOND_GATE_THROUGH_A_RELATIVE_IMPORT, GATE_FORM),
        (A_SECOND_GATE_FROM_THE_TRUSTED_SIDE, GATE_FORM),
        (A_SECOND_GATE_BY_INEQUALITY, GATE_FORM),
        (A_SECOND_GATE_BY_MATCH, GATE_FORM),
        (A_SECOND_GATE_BY_MATCH_ON_LITERALS, GATE_FORM),
        (A_SECOND_GATE_AS_A_TABLE, TABLE_FORM),
        (A_SECOND_GATE_AS_A_TABLE_BUILT_BY_DICT, TABLE_FORM),
        (A_SECOND_GATE_AS_A_TABLE_COMPREHENSION, TABLE_FORM),
        (A_SECOND_GATE_AS_A_TABLE_WITH_AN_EXPANSION, TABLE_FORM),
        (A_SECOND_GATE_AS_A_CONDITIONAL_EXPRESSION, GATE_FORM),
        (A_SECOND_GATE_SHORT_CIRCUITED, GATE_FORM),
        (A_SECOND_GATE_IN_A_LOOP, GATE_FORM),
        (A_SECOND_GATE_THROUGH_A_BOUND_CONFIDENCE, GATE_FORM),
        (A_SECOND_GATE_BY_MEMBERSHIP_IN_A_BOUND_TUPLE, GATE_FORM),
        (A_SECOND_GATE_ON_A_COMPOSED_TYPE, GATE_FORM),
        (A_SECOND_GATE_INSIDE_A_RETURNED_MAPPING, GATE_FORM),
        (A_SECOND_GATE_THROUGH_A_HELPER_CALL, GATE_FORM),
        (A_SECOND_GATE_ACCUMULATED, GATE_FORM),
        (A_SECOND_GATE_BUILT_FROM_THE_FIRST, GATE_FORM),
        (A_SECOND_GATE_BUILT_FROM_THE_FIRSTS_MODULE, GATE_FORM),
    ],
    ids=[
        "member",
        "literal",
        "module-alias",
        "package-attribute",
        "relative-import",
        "the-trusted-side",
        "inequality",
        "match",
        "match-on-literals",
        "table",
        "table-by-dict-call",
        "table-comprehension",
        "table-with-an-expansion",
        "conditional-expression",
        "short-circuit",
        "loop",
        "a-bound-confidence",
        "membership-in-a-bound-tuple",
        "a-composed-outcome-type",
        "a-returned-mapping",
        "a-helper-call",
        "an-augmented-assignment",
        "the-gates-own-constant",
        "the-gates-own-module",
    ],
)
def test_the_detector_reports_a_second_gate(source: str, expected: str) -> None:
    """Twenty-four spellings, because nothing in the tree exercises the detector today.

    An audit that recognised only the forms its author happened to imagine is an
    audit with a door in it, and every one of these was written after a reviewer
    ran the detector against it and watched it report nothing. Four are worth
    naming:

    `the-trusted-side` and `inequality` are the same rule stated positively, and
    the second is `CPM-AD-4`'s violation itself -- it degrades `inventory-derived`
    to `unknown`, which is exactly what `R-08` is about. `match` is idiomatic on
    the 3.12 floor and puts the confidence in no comparison at all.
    `a-composed-outcome-type` is the vocabulary every real pass will use, since
    `core/outcomes.py` composes a type per domain and each inherits the sentinels
    by name.
    """
    found = gate_implementations(ast.parse(source))

    assert [finding.form for finding in found] == [expected], [finding.describe for finding in found]


@pytest.mark.parametrize(
    "source",
    [
        CALLS_THE_GATE,
        A_RESOLUTION_TIME_RULE,
        AN_ASSERTION_ABOUT_A_CONFIDENCE,
        A_STATUS_CHOSEN_FOR_ANOTHER_REASON,
        A_MATCH_ON_SOMETHING_ELSE,
        A_FIXTURE_TABLE_OF_LITERALS,
        PROSE_ONLY,
    ],
    ids=[
        "calls-the-gate",
        "a-resolution-time-rule",
        "an-assertion",
        "another-reason",
        "a-match-on-something-else",
        "a-fixture-table-of-literals",
        "prose",
    ],
)
def test_the_detector_ignores_what_is_not_a_second_gate(source: str) -> None:
    """The negative control, and the reason the ban can be absolute.

    `calls-the-gate` is the shape every policy pass is supposed to have, and a
    detector that flagged it would be switched off in a week.
    `a-resolution-time-rule` is `identity/services.py` in miniature: a comparison
    against `unmapped` that selects no status, which is the precision the whole
    design rests on. `another-reason` and `a-match-on-something-else` write a
    status without consulting a confidence, which every pass does on every row.

    `a-fixture-table-of-literals` is the concession the module docstring records:
    a mapping written entirely in strings is data as often as it is a rule, and
    the ban has no exemption path to resolve the difference with.
    """
    assert gate_implementations(ast.parse(source)) == []


@pytest.mark.parametrize(
    "source",
    [AN_ASSERTION_ABOUT_A_CONFIDENCE, A_FIXTURE_TABLE_OF_LITERALS, PROSE_ONLY],
    ids=["an-assertion", "a-fixture-table-of-literals", "prose"],
)
def test_the_ban_is_not_a_substring_scan(source: str) -> None:
    """The AST claim, asserted rather than argued in a docstring.

    Each of these contains the banned vocabulary in plain text -- a docstring
    describing the prohibition, a comment quoting the three lines, an assertion
    naming the member -- and each parses to something that is not a gate. A
    detector rewritten as a text search would fail here; one that had stopped
    firing altogether would pass, which is what the positives next door are for.
    """
    assert GATED_CONFIDENCE.lower() in source.lower(), "the control no longer contains the word it is a control for"
    assert gate_implementations(ast.parse(source)) == []


def test_a_module_without_a_source_file_is_a_hard_failure() -> None:
    """`confidence.__file__` is the one exemption, so its absence cannot be silent.

    `Path("")` resolves to the working directory: a real path, which simply is not
    this module. A fallback would exempt nothing, report `core/confidence.py` as
    an offender, and leave the anti-vacuity guard asserting against a file the
    scan had already excluded -- three failures whose first symptom would be an
    audit that had been passing for months.
    """
    with pytest.raises(RuntimeError, match="no __file__"):
        declaring_module(ModuleType("a_module_that_was_never_loaded_from_a_file"))
