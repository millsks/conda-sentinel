"""The versioned policy parameters: one reviewed file, read by the run's policy version.

`CPM-FR-40` requires the feedstock inactivity threshold to be "a versioned policy
parameter" rather than a constant, and `CPM-AD-8` says a rule set is *versioned
data*. This module is the mechanism: a delimited, reviewed file shipped inside
the wheel, mapping a policy version to the parameters a run at that version
applies, read through one contract that refuses rather than repairs.

`CPM-SECURITY-S04` added the second parameter on the same terms: `CPM-FR-17`
names a per-package risk level and the PRD seeds no scale for it, so the
*severity order* it is drawn from is recorded here rather than written into a
pass. See `RISK_ORDER_KEY` and `PARAMETER_KEYS` for why that key is optional in
the file, and what a run at a version that omits it derives instead.

`CPM-SECURITY-S05` added the third, and it is the one that ships **empty**.
`CPM-FR-18` gives licence compliance to a versioned policy, and *which licences
are allowed* is PRD Open Question 2 -- a decision nobody has taken and one this
component was told not to take. So `RULES_KEY` records a rule set that names no
licence, every package with a licence reaches `manual_review`, and `allowed` is
unreachable until review writes a rule that says so. See `RULES_KEY` for the
schema and `_license_rules` for every fault it refuses.

**Why a file rather than a setting or a database table.** `CPM-AD-14` makes
reviewed reference data in the repository this product's one governed shape for
exactly this, and `collectors/data/` is the precedent, down to shipping inside
the built artifact and being changed by pull request. A *setting* would be
per-deployment rather than per-version, so two components at one policy version
could disagree about what that version means -- which is the whole property
`CPM-AD-8`'s versioning exists to give. A *table* would be a write path nothing
audits: a verdict would change because somebody ran an `UPDATE`, with no diff and
no reviewer.

**The file is a history, not a current value, and that is load-bearing.**
`CPM-FR-22` promises that re-running a recorded policy version at its recorded
cut-off reproduces the original output. A version's parameters must therefore
still be readable long after a newer version has superseded them, so an entry is
*added* when a threshold changes and the old entry stays. Removing one makes
every run recorded at that version unreplayable.

**An unknown version is refused, never defaulted.** There is no fallback entry
and there must not be: a default would make a run at a version nobody reviewed
produce verdicts that look exactly like reviewed ones, which is the "degrades to
a clean result" `CPM-NFR-3` forbids. `CPM-AD-23` contains the refusal to one
package -- that package's derived rows roll back and every other package's
commit -- so the cost of the refusal is bounded and visible in the run's ending.
The operational consequence is stated rather than left to be met: **a policy run
must name a version this file records, or every package fails.**

**Parsing and reading are two things, and they are separated here** on exactly
the terms `collectors/watchlist.py` separates them. `parameters_from` turns
*text* into parameter sets and owns every refusal about content; `parameters_at`
opens a file and owns only the refusals that are about a file. That split is what
lets the whole contract be measured in memory.

**The read is memoized, and that is the one place this module differs from the
watchlist deliberately.** A watchlist is data an operator corrects between
sweeps, so it is re-read on every fetch. These parameters are keyed by the policy
version a run declares, and `CPM-AD-8` makes one version mean one rule set: a
file re-read per package would let an edit mid-run split a single run across two
rule sets, with half the inventory judged under each. So the file is read once
per process and a change to it takes effect at the next start -- which shipping a
new artifact already is, because the file ships inside the wheel.

**Every refusal is `ImproperlyConfigured`, names the file, and names the fault.**
The parameters are governed reference data under `CPM-AD-14`, so a malformed set
is a misconfigured deployment rather than a misbehaving source, and an operator
sent to the file is being sent to the right place. `collectors/watchlist.py`'s
`WatchlistError` is the same boundary drawn for the same reason.

**On the `AD-` prefix.** A bare `AD-n` in this repository is an *inherited*
platform decision; a decision from this product's own architecture spine always
carries the `CPM-` prefix.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from datetime import timedelta
from functools import cache as memoized
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING
from typing import Final

from django.core.exceptions import ImproperlyConfigured

from conda_sentinel.collectors.spdx import OPERATORS
from conda_sentinel.collectors.spdx import SPELLINGS
from conda_sentinel.policies.outcomes import PRIORITY_BUCKETS
from conda_sentinel.policies.outcomes import RULE_DISPOSITIONS

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = [
    "INACTIVITY_DAYS_KEY",
    "MAX_INACTIVITY_DAYS",
    "MAX_LICENSE_EXPRESSION_CHARACTERS",
    "MAX_PRIORITY_TEXT_CHARACTERS",
    "MAX_RISK_LEVEL_CHARACTERS",
    "MAX_SIGNAL_WEIGHT",
    "NORMALIZABLE_IDENTIFIERS",
    "NORMALIZABLE_OPERATORS",
    "PARAMETERS_FILENAME",
    "PRIORITY_BUCKET_FIELD",
    "PRIORITY_DESCRIPTION_FIELD",
    "PRIORITY_DOMAINS",
    "PRIORITY_REASON_FIELD",
    "PRIORITY_RULES_KEY",
    "PRIORITY_RULE_KEYS",
    "PRIORITY_SIGNALS",
    "PRIORITY_WEIGHTS_KEY",
    "PRIORITY_WHEN_FIELD",
    "RISK_ORDER_KEY",
    "RULES_KEY",
    "RULE_DISPOSITION_KEY",
    "RULE_EXPRESSION_KEY",
    "RULE_KEYS",
    "VERSIONS_TABLE",
    "VERSION_SEPARATOR",
    "LicenseRule",
    "PolicyParameterError",
    "PolicyParameters",
    "PriorityRule",
    "forget_recorded_parameters",
    "newest_recorded_version",
    "parameters_at",
    "parameters_directory",
    "parameters_file",
    "parameters_for",
    "parameters_from",
    "parameters_in",
    "recorded_parameters",
    "recorded_versions",
    "version_key",
]

#: The one top-level table the file declares: policy version to parameter set.
#:
#: Nested rather than flat -- `[versions."2026.09"]` rather than
#: `["2026.09"]` -- so the file has room for a header comment and for a later
#: top-level key without every version becoming ambiguous with it. A key outside
#: this table is refused rather than ignored, on the terms
#: `collectors/watchlist.py` refuses an undefined column: a reviewer who adds one
#: believes they have supplied something.
VERSIONS_TABLE: Final[str] = "versions"

#: The one parameter a version records today, in the unit review reads it in.
#:
#: Days rather than seconds because the file is changed by a person reading a
#: pull-request diff, and "180" is a number a reviewer can hold an opinion about
#: where "15552000" is not. It becomes a `timedelta` at the boundary, so nothing
#: downstream carries a unit in a name.
INACTIVITY_DAYS_KEY: Final[str] = "feedstock_inactivity_days"

#: `CPM-FR-17`'s risk-level parameter: the severities a version ranks, worst
#: first, exactly as a source states them.
#:
#: **The key names an order rather than a scale, and that is the whole of what
#: this component decides.** `CPM-FR-17` asks for a per-package risk level and
#: the PRD seeds no thresholds for it, so inventing a severity taxonomy in code
#: is the one thing `CPM-SECURITY-S04`'s `Block If` forbids outright. What ships
#: instead is the mechanism: the *file* states which severity labels this
#: product recognises and in which order, review changes them without a
#: deployment, and `policies/vulnerability.py` applies whatever the run's version
#: records. A version that ranks nothing has no rule for that pass to apply, so
#: its rows carry no risk level and say so.
#:
#: A list of strings rather than a mapping of label to number, because a number
#: is an invitation to average two of them and `CPM-FR-17`'s single hazard is a
#: severity score that averages a known-exploited advisory away. An order can be
#: read but not summed.
RISK_ORDER_KEY: Final[str] = "vulnerability_risk_order"

#: `CPM-FR-18`'s licence parameter: the rules a version applies, each naming one
#: normalized SPDX expression and what this product does about it.
#:
#: **The key ships recording an empty list, and that is the decision rather than
#: a placeholder.** `CPM-FR-18` gives licence compliance to a versioned policy,
#: and PRD Open Question 2 -- *which licences are allowed* -- is unanswered and
#: named by the PRD as blocking `CPM-EP-SECURITY`. So what ships is the
#: mechanism: a schema a reviewer can fill in without a deployment, filled in
#: with nothing. A run at such a version routes every package whose licence was
#: established to `manual_review`, which is the correct answer to "no policy has
#: been decided" and is exactly what `CPM-SECURITY-S03`'s AC 2 already promised.
#:
#: This differs from `RISK_ORDER_KEY`, which shipped a *provisional* order. There
#: the PRD named a risk level and simply seeded no thresholds, so a value marked
#: as provisional in the file was a reviewable list rather than a claim about any
#: package. Here the PRD names the decision itself as open, and a provisional
#: allow list would be this component deciding a compliance question it was told
#: not to decide -- and deciding it in the one direction (`allowed`) that looks
#: like good news and is least likely to be questioned.
#:
#: The shape, one table per rule:
#:
#: ```toml
#: license_rules = [
#:   { expression = "MIT", disposition = "allowed" },
#: ]
#: ```
#:
#: A list of tables rather than a mapping of expression to disposition, because a
#: TOML mapping key cannot carry an SPDX expression containing spaces without
#: quoting rules a reviewer has to know, and because a rule is a thing this
#: schema will grow fields on (a note, a review date) where a bare value is not.
RULES_KEY: Final[str] = "license_rules"

#: The rule field naming the normalized SPDX expression it is about.
#:
#: `expression` rather than `license`, and the name is the honest one: what a
#: rule matches is `license_findings.normalized_license`, which is an SPDX
#: *expression* and may be compound (`MIT OR Apache-2.0`). A rule about a
#: compound expression names it whole -- `policies/licence.py` matches whole, and
#: takes a conjunction apart only to *withhold* a permission, never to grant one.
#:
#: The value is refused unless `collectors/spdx.py` could actually normalize some
#: stated licence to it: see `_unreachable_expression_fault`. A rule naming
#: `GPL-3.0` matches nothing this product can ever store.
RULE_EXPRESSION_KEY: Final[str] = "expression"

#: The rule field naming what this product does about that expression, over
#: `policies/outcomes.py`'s `RULE_DISPOSITIONS`.
RULE_DISPOSITION_KEY: Final[str] = "disposition"

#: Every key one rule's table may declare, and exactly the keys it must. Both
#: halves are required: a rule missing either names nothing or decides nothing,
#: and an extra key is a reviewer who believes they supplied a rule field this
#: contract reads.
#:
#: **Exact, so a key this contract has not grown yet refuses the file.** That is
#: not in tension with `RULES_KEY`'s list-of-tables shape, which is chosen for a
#: schema that *will* grow fields: growing one means adding it here in the same
#: commit, and until then `note = "..."` is a reviewer who believes they recorded
#: a note something reads. `PARAMETER_KEYS` refuses an undefined version key on
#: exactly the same terms one level up, and the alternative -- accepting and
#: ignoring keys this contract does not define -- is the silently dropped edit
#: both refusals exist to prevent. The cost is stated plainly rather than
#: discovered: adding a field to the schema is a code change and not only a file
#: change, and a file written against a newer schema than the deployed wheel
#: fails the whole file rather than one rule.
RULE_KEYS: Final[frozenset[str]] = frozenset({RULE_EXPRESSION_KEY, RULE_DISPOSITION_KEY})

#: Every SPDX identifier `collectors/spdx.py` can put in `normalized_license`,
#: case-folded for comparison.
#:
#: **Read off that module's own table rather than listed**, which is the whole
#: point: what a rule has to be able to match is exactly what the normalizer
#: produces, and a second list here would drift from it by one identifier and
#: start refusing rules that would have matched. `SPELLINGS` maps every
#: recognised *spelling* to an identifier, and `normalize` returns the
#: identifier, so the reachable operands are its **values** -- `mit license` is a
#: key of it and is not a value, and a rule naming it could never match a stored
#: expression.
#:
#: Importing from a collector closes no cycle: `collectors/spdx.py` is a leaf
#: that imports `django.db.models` and the standard library and nothing else, and
#: this module already reaches across to a sibling for `RULE_DISPOSITIONS` for
#: exactly this kind of gate.
#:
#: This is not a licence policy and states no disposition. It is the alphabet the
#: normalizer writes in, and every identifier in it is as forbiddable as it is
#: allowable.
NORMALIZABLE_IDENTIFIERS: Final[frozenset[str]] = frozenset(identifier.casefold() for identifier in SPELLINGS.values())

#: The operators a normalized expression may join its operands with, case-folded.
#:
#: Folded even though `collectors/spdx.py` recognises them in upper case only,
#: because the *rule* comparison in `policies/licence.py` folds both sides: a
#: rule spelled `mit or apache-2.0` does match the stored `MIT OR Apache-2.0`, so
#: refusing it here would refuse a rule that works.
NORMALIZABLE_OPERATORS: Final[frozenset[str]] = frozenset(operator.casefold() for operator in OPERATORS)

#: How many distinct operators a normalized expression joins its operands with:
#: one. `collectors/spdx.py` refuses a mixed expression outright, so a rule naming
#: one could never match. Named rather than spelled at the comparison, where a
#: bare `1` reads as an arbitrary bound.
_ONE_OPERATOR: Final[int] = 1

#: The longest expression a rule may name, which is also how wide the column
#: recording a matched rule is (`policies/models.py` reads this name for it).
#:
#: The number is `license_findings.normalized_license`'s own width, and it is
#: that number because a rule that could not be *matched* against a stored
#: expression is a rule nothing will ever apply -- and a matched rule the derived
#: column could not hold would be truncated into an audit trail nobody wrote.
#: Reconciled against the evidence column by a case rather than by this comment.
MAX_LICENSE_EXPRESSION_CHARACTERS: Final[int] = 2048

#: Every key a version's table may declare. The set is what makes an
#: unrecognised key a refusal rather than a silently dropped edit.
#:
#: **Neither key is required of every version, and neither is required of every
#: *run*.** `feedstock_inactivity_days` is refused when absent because
#: `CPM-CURRENCY-S07` shipped with it and every recorded version carries it.
#: `RISK_ORDER_KEY` arrived later, so a version recorded before it exists cannot
#: carry it and must stay readable -- `CPM-FR-22`'s replay is only possible while
#: an old entry still says what it said. So an entry without it parses and
#: records `None`, and the vulnerability pass derives its status and its KEV
#: membership normally, leaves the risk level blank, and says on the row that the
#: version records no order. It does **not** refuse: `core/policy_run.py` puts
#: one *package* in a transaction rather than one pass, so a refusal there would
#: roll that package's currency and feedstock rows back too -- and, the condition
#: being version-wide, would fail every package and finalize the run `failed`.
#: That would break the currency and feedstock replay of every run recorded at
#: such a version while protecting no vulnerability replay, because no run at one
#: ever carried a vulnerability verdict. A *malformed* order is a different thing
#: and is still refused, here, at the read: it is an operator error in a file
#: somebody can edit rather than a historical artifact.
#:
#: `RULES_KEY` is optional on the same terms and for the same two reasons, plus
#: one of its own. A version recorded before `CPM-SECURITY-S05` cannot carry it
#: and must stay replayable; refusing would take that version's other three
#: domains' rows down with it, one package at a time, and finalize the run
#: `failed`. And a version that records the key as an **empty list** means the
#: same thing as one that omits it -- no rule names any licence -- so both parse
#: to an empty rule set and the pass derives `manual_review` for every package
#: whose licence was established, saying on the row that no rule set was
#: recorded. That is deliberately unlike `RISK_ORDER_KEY`, which refuses an empty
#: list: an empty severity order would produce a blank risk level for the whole
#: inventory, indistinguishable from sources that state no severities, whereas an
#: empty rule set produces a distinct, self-describing verdict and is the state
#: this component ships in. A *malformed* rule set is still refused here.
#: `CPM-FR-20`'s rule set: the top-down, first-match rules a run assigns priority
#: buckets by.
#:
#: **The key ships recording an empty list, and that is the decision rather than a
#: placeholder** -- the same posture `RULES_KEY` takes and for a stronger reason.
#: PRD Open Question 8 asks what seeds the priority rule set and the score function
#: and answers "both are undefined -- they encode an organizational risk posture
#: that does not exist yet", naming the epic as blocked; `CPM-PRIORITY-S01`'s epic
#: entry then constrains that story to "the engine, the schema and the
#: explainability fields -- not a seeded rule set". So what ships is the mechanism:
#: a schema a reviewer can fill in without a deployment, filled in with nothing.
#:
#: A version recording no rules puts every package in `unknown`, which is the
#: correct answer to "nobody has decided what P1 means" and is emphatically not
#: `P10`: a default bucket is a claim about importance nobody made, in the
#: direction least likely to be questioned.
#:
#: The shape, one table per rule, in the order they are matched:
#:
#: ```toml
#: priority_rules = [
#:   { bucket = "p1", description = "...", reason = "...", when = { vulnerability_status = "advisories_matched" } },
#: ]
#: ```
PRIORITY_RULES_KEY: Final[str] = "priority_rules"

#: The rule fields, and every one of them is required.
#:
#: `description` and `reason` are required because `CPM-PRIORITY-S01`'s AC 2 is
#: that an assignment explains itself: a rule that assigned a bucket and said
#: nothing would produce exactly the row the story exists to prevent, and it would
#: be the reviewer who wrote the rule -- not the code -- who left the explanation
#: out. Refusing at the read is what puts that back in front of them.
#:
#: `when` is required and must not be empty. A rule matching everything is a
#: default bucket wearing a condition, and the file is where that is caught.
PRIORITY_BUCKET_FIELD: Final[str] = "bucket"
PRIORITY_DESCRIPTION_FIELD: Final[str] = "description"
PRIORITY_REASON_FIELD: Final[str] = "reason"
PRIORITY_WHEN_FIELD: Final[str] = "when"
PRIORITY_RULE_KEYS: Final[frozenset[str]] = frozenset(
    {PRIORITY_BUCKET_FIELD, PRIORITY_DESCRIPTION_FIELD, PRIORITY_REASON_FIELD, PRIORITY_WHEN_FIELD},
)

#: The derived verdicts a rule's `when` may match on, by the name the file uses.
#:
#: Declared here rather than in `policies/priority.py` so the *file* is validated
#: against the same set the pass reads, and a rule naming a domain nothing answers
#: is refused where a reviewer can see it rather than silently matching nothing for
#: ever. `tests/unit/django_apps/test_priority_policy.py` reconciles these names
#: against the readers the pass declares, which is what stops the two drifting.
#:
#: Six domains, one per policy pass that runs before the priority pass. There is no
#: entry for priority itself: a rule that matched on the bucket it assigns would be
#: a cycle, and the file is where that is refused.
PRIORITY_DOMAINS: Final[frozenset[str]] = frozenset(
    {
        "currency_status",
        "feedstock_presence_status",
        "vulnerability_status",
        "license_outcome",
        "remediation_readiness",
        "python_readiness",
    },
)

#: `CPM-FR-20`'s score function: the weight each internal usage signal carries.
#:
#: **Empty on purpose, on exactly the terms `PRIORITY_RULES_KEY` is.** The PRD
#: names the score function as undefined in the same breath as the rule set.
#:
#: A mapping of signal name to a non-negative integer weight. A version recording
#: none computes no score at all -- **not** a score of zero, and not a score from
#: unweighted signals. The shape:
#:
#: ```toml
#: priority_score_weights = { internal_component_count = 3, internal_lob_count = 2 }
#: ```
PRIORITY_WEIGHTS_KEY: Final[str] = "priority_score_weights"

#: The internal usage signals a weight may name, by the column `inventory_snapshots`
#: stores them in (`CPM-AD-25`, PRD Open Question 3b).
#:
#: `internal_component_count` and `internal_lob_count` are required on every `ok`
#: inventory row and together are the usage breadth `CPM-FR-4` ranks by; `apps`,
#: `platforms`, `downloads` and `versions` are nullable, are score inputs for this
#: requirement, and are never invented when blank. A weight naming anything else is
#: refused: a signal nothing observes would contribute nothing to every score, for
#: ever, silently.
PRIORITY_SIGNALS: Final[frozenset[str]] = frozenset(
    {
        "internal_component_count",
        "internal_lob_count",
        "apps",
        "platforms",
        "downloads",
        "versions",
    },
)

#: The largest weight a signal may carry. A bound rather than a judgement: the
#: score is normalized against the recorded weights, so the absolute numbers only
#: have to be comparable -- and an unbounded one is an overflow waiting for a
#: reviewer's typo.
MAX_SIGNAL_WEIGHT: Final[int] = 1_000_000

#: The longest a recorded bucket description or reason may be, which is also how
#: wide the columns that store them are (`policies/models.py` reads these names).
#:
#: One number rather than two, on exactly the terms `MAX_RISK_LEVEL_CHARACTERS`
#: states: an explanation the file records but the column cannot hold would be
#: truncated into a reason nobody wrote, which on this table is the whole of AC 2.
MAX_PRIORITY_TEXT_CHARACTERS: Final[int] = 512

PARAMETER_KEYS: Final[frozenset[str]] = frozenset(
    {INACTIVITY_DAYS_KEY, RISK_ORDER_KEY, RULES_KEY, PRIORITY_RULES_KEY, PRIORITY_WEIGHTS_KEY},
)

#: The longest a recorded severity label may be, which is also how wide the
#: column that stores one is (`policies/models.py` reads this name for it).
#:
#: One number rather than two, because the bound and the column are the same
#: fact: a label the file records but the column cannot hold would be truncated
#: into a risk level nobody wrote. Refused at the read so the message names the
#: file and the version, rather than surfacing as a database error about a
#: `varchar` several frames from the entry a reviewer has to correct.
MAX_RISK_LEVEL_CHARACTERS: Final[int] = 32

#: The reviewed file's name.
PARAMETERS_FILENAME: Final[str] = "policy-parameters.toml"

#: The encoding the file is read as. `tomllib` requires UTF-8 by specification,
#: and the decode happens here so a file that is not text produces a refusal
#: naming the file rather than a `UnicodeDecodeError` from inside the parser.
PARAMETERS_ENCODING: Final[str] = "utf-8"

#: The largest interval a recorded threshold may express, in days.
#:
#: `timedelta`'s own ceiling, and the bound exists to turn an `OverflowError`
#: into a refusal that names the file and the version rather than to express an
#: opinion about how long is too long. A reviewer who writes a nine-digit number
#: has made a typing mistake, and the message they get should say which file to
#: go and edit -- not `days=1000000000; must have magnitude <= 999999999`, raised
#: from inside a constructor, with no mention of a parameter set anywhere in it.
MAX_INACTIVITY_DAYS: Final[int] = timedelta.max.days


class PolicyParameterError(ImproperlyConfigured):
    """The reviewed policy parameters cannot be read, or do not cover a version.

    An `ImproperlyConfigured` subclass, on exactly the terms
    `collectors/watchlist.py`'s `WatchlistError` is one: these parameters are
    governed reference data (`CPM-AD-14`), so a malformed set is a misconfigured
    deployment and the operator is being sent to a file they can edit.

    A named subclass rather than a bare `ImproperlyConfigured`, so a case can
    assert *this* refusal rather than any misconfiguration. It adds no startup
    condition: `src/config/startup/` is where those live, and this is a run-time
    refusal raised from a domain application.
    """


@dataclass(frozen=True, slots=True)
class LicenseRule:
    """One recorded statement about one normalized licence expression.

    Frozen, on exactly the terms `PolicyParameters` is: a rule that could be
    edited after it was read would make "this run applied this version's rules" a
    claim nothing supports -- and the one field a mutation would reach first is
    the disposition, which is the difference between `manual_review` and
    `allowed`.

    Attributes:
        expression: The normalized SPDX expression this rule is about, recorded
            **exactly as the reviewer wrote it**. Not case-folded here: the
            comparison in `policies/licence.py` folds both sides, and what a
            derived row stores as its matched rule is this spelling, so a
            reviewer reading a report sees the string they put in the file.
        disposition: What this product does about that expression, one of
            `policies/outcomes.py`'s `RULE_DISPOSITIONS`. The read below refuses
            anything else, which is what makes `allowed` reachable only from a
            file that says `allowed`.

    """

    expression: str
    disposition: str


@dataclass(frozen=True, slots=True)
class PriorityRule:
    """One `CPM-FR-20` rule: what it matches, which bucket it assigns, and why.

    Frozen and slotted, on the terms `LicenseRule` is: a rule that could be edited
    after it was read would make "this run applied this version's rules" a claim
    nothing supports.

    Attributes:
        bucket: The `PriorityBucket` value this rule assigns, `p1` through `p10`.
        description: What the bucket means, in the reviewer's own words. Stored on
            every row this rule produces, which is `CPM-PRIORITY-S01`'s AC 2: the
            description travels with the assignment so nobody has to open the rule
            set to read it.
        reason: Why this rule fires, in the reviewer's own words. Stored beside the
            description, and deliberately a second field rather than half of it --
            "what P1 means" and "why *this* package is P1" are different sentences,
            and folding them would lose whichever the reader needed.
        conditions: The `(domain, verdict)` pairs that must **all** hold, in the
            order the file states them.

            A tuple of pairs rather than a mapping, and both halves matter: a
            `dict` is unhashable and would break the frozen dataclass, and the
            *order* is what makes a refusal message name the conditions in the
            order a reviewer wrote them rather than in whatever order a hash
            produced.

    """

    bucket: str
    description: str
    reason: str
    conditions: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class PolicyParameters:
    """The parameter set one policy version applies.

    Frozen, because a parameter set that could be edited after it was read would
    make "this run applied this version's rules" a claim nothing supports.

    Attributes:
        version: The policy version this set was recorded under. Carried on the
            object rather than only in the mapping's key, so a refusal or a log
            line downstream can name the version without the caller passing it
            twice.
        feedstock_inactivity: How long a feedstock may go without a push before
            `CPM-FR-40`'s policy calls it inactive. A `timedelta`, positive by
            construction -- the read below refuses anything else.
        vulnerability_risk_order: The severity labels `CPM-FR-17`'s risk level is
            drawn from, worst first, or `None` where this version records none.
            `None` is a real, ordinary state and not a failure of the file: a
            version recorded before this parameter existed has to stay readable
            for `CPM-FR-22`'s replay, and it is the *vulnerability pass* that
            refuses, per package, naming the parameter. Defaulted so a version
            that predates the parameter constructs exactly as it always did.
        license_rules: `CPM-FR-18`'s rules, in the order the file states them,
            or empty where this version records none. **Empty rather than
            `None`, and there is exactly one un-ruled state rather than two.** A
            version that predates the key and a version that records `[]` mean
            the same thing -- no rule names any licence -- and the licence pass
            derives `manual_review` for every package with an established licence
            either way, saying so on the row. `vulnerability_risk_order` keeps a
            `None` because its empty list is *refused*, which makes `None`
            unambiguous there; here an empty list is the shipped state, so a
            second spelling of it would be a distinction no verdict reads.

    """

    version: str
    feedstock_inactivity: timedelta
    vulnerability_risk_order: tuple[str, ...] | None = None
    license_rules: tuple[LicenseRule, ...] = ()
    priority_rules: tuple[PriorityRule, ...] = ()
    priority_score_weights: tuple[tuple[str, int], ...] = ()


def _parameters_directory(module: str) -> Path:
    """Return the directory the reviewed file lives in, or refuse the installation.

    The reviewed file ships beside this module: `pyproject.toml`'s
    `only-include = ["src"]` with the `sources` mapping that strips
    `src/django_apps` puts `data/` in the wheel here. A path computed from
    `BASE_DIR` would work in a checkout and fail in a container, because the
    `src/` segment does not exist in the wheel layout -- and the failure would
    arrive at the first deployed policy run rather than at the build.
    `collectors/watchlist.py` computes its own the same way and for the same
    reason, and `tests/integration/test_import_resolution.py` asserts both trees
    are actually inside the built wheel.

    `strict=True` resolves every path component and follows symlinks, so an
    editable install whose `policies/` is a link into a checkout resolves to the
    checkout, which is where the file actually is.

    Takes the module's location rather than reading `__file__` itself, so the
    refusal is reachable from a case: `__file__` for a module being imported
    names a file that exists, and a branch only provokable by deleting the module
    out from under the interpreter would be an unreachable line and a
    `pragma: no cover` that `tests/unit/test_coverage_policy.py` bans.

    Args:
        module: The location of the module the `data/` tree sits beside --
            `__file__` in production.

    Returns:
        The `data/` directory beside it.

    Raises:
        PolicyParameterError: When that location cannot be resolved.

    """
    try:
        here = Path(module).resolve(strict=True)
    except OSError as unresolvable:
        message = (
            f"the policy parameter directory cannot be resolved from {module!r}: "
            f"{type(unresolvable).__name__}: {unresolvable}. The reviewed file ships beside this module "
            f"(CPM-AD-14); an installation without it cannot run a policy pass that reads a versioned "
            f"parameter."
        )
        raise PolicyParameterError(message) from unresolvable
    return here.parent / "data"


def parameters_directory() -> Path:
    """Return where the reviewed file lives: beside this module, in the wheel and in a checkout.

    **Resolved on demand rather than at import, and that changed for a reason.**
    A module-scope constant resolves during `django.setup()`, so an installation
    that shipped the modules and dropped the `data/` tree would refuse to *boot*
    -- and `CPM-AD-23` puts the atomic unit at one package, with a pass's refusal
    costing one package's rows and leaving every other package's committed. A
    misconfiguration that fails the whole component is the opposite of that
    containment, and it fails a web process that would never have read this file.
    Resolved here, the same misconfiguration fails the policy run that needed it
    and nothing else.

    Cheap enough to do per call: one `Path.resolve`. The *file* is what is
    expensive to read, and `recorded_parameters` memoizes that.

    Returns:
        The `data/` directory beside this module.

    Raises:
        PolicyParameterError: When this module's own location cannot be resolved.

    """
    return _parameters_directory(__file__)


def parameters_file() -> Path:
    """Return the reviewed parameter file this component ships.

    The one substitution point the suite has: a case that needs a different file
    patches this name, which is what `tests/policy_parameters.py` does. A module
    constant would have been the same seam with none of `parameters_directory`'s
    laziness.

    Returns:
        The path to `policy-parameters.toml` beside this module.

    Raises:
        PolicyParameterError: When the directory cannot be resolved.

    """
    return parameters_directory() / PARAMETERS_FILENAME


def parameters_from(text: str, *, source: Path | str) -> dict[str, PolicyParameters]:
    """Turn a whole parameter file into parameter sets, or refuse it.

    Pure: it opens nothing and knows nothing about where the text came from
    beyond the name it puts in its messages. Every refusal about *content* is
    here, which is what lets the contract be measured against strings rather than
    against files.

    The whole document is turned into parameter sets before anything is returned.
    A generator that yielded entries until it met a bad one would hand a caller a
    prefix of a reviewed file, and the version it happened to want might be in it
    -- so a broken file would fail or not depending on which version was asked
    for.

    Args:
        text: The file's contents.
        source: What to call the file in a refusal. A `Path` in production; a
            case parsing a literal may pass any name.

    Returns:
        One `PolicyParameters` per recorded version, by version.

    Raises:
        PolicyParameterError: When the text is not TOML; when it declares a key
            outside `VERSIONS_TABLE`; when `VERSIONS_TABLE` is absent, is not a
            table, or is empty; when a version's entry is not a table or declares
            an unrecognised key; when its threshold is missing, is not a whole
            number of days, or is not positive; when its severity order is
            malformed; or when its licence rule set is.

    """
    try:
        document = tomllib.loads(text)
    except tomllib.TOMLDecodeError as unparsable:
        message = (
            f"the policy parameters at {source} are not readable as TOML: {type(unparsable).__name__}: "
            f"{unparsable}. A parameter set is reviewed in a pull-request diff and is text (CPM-AD-14)."
        )
        raise PolicyParameterError(message) from unparsable

    undefined = sorted(key for key in document if key != VERSIONS_TABLE)
    if undefined:
        message = (
            f"the policy parameters at {source} declare the top-level key(s) {undefined}, which this "
            f"contract does not define. The only table is [{VERSIONS_TABLE}]; a key outside it is refused "
            f"rather than ignored, because a silently dropped key is a reviewer who believes they supplied "
            f"a parameter."
        )
        raise PolicyParameterError(message)

    recorded = document.get(VERSIONS_TABLE)
    if not isinstance(recorded, dict):
        found = "nothing" if recorded is None else type(recorded).__name__
        message = (
            f"the policy parameters at {source} do not declare a [{VERSIONS_TABLE}] table -- found {found}. "
            f"Every parameter set is recorded under the policy version that applies it (CPM-AD-8), so a file "
            f"without that table records nothing a run could ask for."
        )
        raise PolicyParameterError(message)
    if not recorded:
        message = (
            f"the policy parameters at {source} record no versions at all. A file awaiting review is not a "
            f"parameter set of nothing: every policy run naming any version would fail against it, and an "
            f"empty table says so far less clearly than this refusal does."
        )
        raise PolicyParameterError(message)

    return {
        _named_version(version, source=source): _parameters(entry, version=version, source=source)
        for version, entry in recorded.items()
    }


def _named_version(version: str, *, source: Path | str) -> str:
    """Refuse a version key that names nothing, and return it unchanged.

    Args:
        version: The key the file recorded a parameter set under.
        source: What to call the file in a refusal.

    Returns:
        The key, **unchanged** -- not stripped. A run declares its version as a
        literal string and `core/ledger.py` records that string verbatim, so a
        key silently trimmed here would match a version the ledger row does not
        carry, and the replay `CPM-FR-22` promises would then read a parameter
        set the original run's own row cannot be traced to.

    Raises:
        PolicyParameterError: When the key is empty or is nothing but
            whitespace. `core/ledger.py` refuses a policy run whose version names
            nothing, so a `[versions.""]` entry can never be reached by a run --
            and a `[versions." 2026.09 "]` entry can never be reached either,
            because the lookup is exact. Both are edits nobody finished, and
            leaving them in the file would mean a reviewer believing they had
            recorded a threshold that nothing will ever apply.

    """
    if not version.strip():
        message = (
            f"the policy parameters at {source} record a parameter set under a version key that names "
            f"nothing ({version!r}). A policy run declares its version as a string and the run ledger "
            f"refuses one that names nothing (CPM-AD-8), so no run can ever reach this entry."
        )
        raise PolicyParameterError(message)
    if version != version.strip():
        message = (
            f"the policy parameters at {source} record a parameter set under {version!r}, which carries "
            f"surrounding whitespace. The lookup is exact, so only a run declaring that exact string -- "
            f"padding included -- would reach it; trimming it here instead would match a version the run's "
            f"own ledger row does not carry."
        )
        raise PolicyParameterError(message)
    return version


def _parameters(entry: object, *, version: str, source: Path | str) -> PolicyParameters:
    """Turn one version's entry into its parameter set, or refuse the entry.

    Args:
        entry: Whatever the file recorded under that version.
        version: The version being read, for the message and for the result.
        source: What to call the file in a refusal.

    Returns:
        The parameter set.

    Raises:
        PolicyParameterError: When the entry is not a table, declares a key this
            contract does not define, or records a threshold that is missing, of
            the wrong type, or not positive.

    """
    if not isinstance(entry, dict):
        message = (
            f"the policy parameters at {source} record {version!r} as {type(entry).__name__} rather than as "
            f"a table. Each version declares its parameters under "
            f"[{VERSIONS_TABLE}.{version!r}], and a scalar there is an edit nobody finished."
        )
        raise PolicyParameterError(message)

    unrecognised = sorted(set(entry) - PARAMETER_KEYS)
    if unrecognised:
        message = (
            f"the policy parameters at {source} declare the key(s) {unrecognised} for version {version!r}, "
            f"which this contract does not define. The parameters are exactly "
            f"{sorted(PARAMETER_KEYS)}; an unrecognised key is refused rather than ignored, because a "
            f"threshold spelled a way nothing reads is a reviewer who believes they changed a verdict."
        )
        raise PolicyParameterError(message)

    return PolicyParameters(
        version=version,
        feedstock_inactivity=_interval(entry.get(INACTIVITY_DAYS_KEY), version=version, source=source),
        vulnerability_risk_order=_risk_order(entry.get(RISK_ORDER_KEY), version=version, source=source),
        license_rules=_license_rules(entry.get(RULES_KEY), version=version, source=source),
        priority_rules=_priority_rules(entry.get(PRIORITY_RULES_KEY), version=version, source=source),
        priority_score_weights=_score_weights(entry.get(PRIORITY_WEIGHTS_KEY), version=version, source=source),
    )


def _license_rules(rules: object, *, version: str, source: Path | str) -> tuple[LicenseRule, ...]:
    """Refuse a rule set nobody could apply, and return the rules it records.

    Args:
        rules: Whatever the file recorded, or `None` where it recorded nothing.
        version: The version being read, for the message.
        source: What to call the file in a refusal.

    Returns:
        The rules in the order the file states them, or `()` where this version
        records none. `()` is not a refusal and is the shipped state: PRD Open
        Question 2 is unanswered, so no version records a rule, and every package
        whose licence was established reaches `manual_review`. An **empty list**
        returns the same `()` as an absent key -- see `PolicyParameters` for why
        there is one un-ruled state here and not two.

    Raises:
        PolicyParameterError: When the value is not a list; when an entry is not
            a table or does not declare exactly `RULE_KEYS`; when an expression
            is not a string, is blank, carries surrounding whitespace, is longer
            than `MAX_LICENSE_EXPRESSION_CHARACTERS`, or names something
            `collectors/spdx.py` can never produce; when a disposition is not one
            of `RULE_DISPOSITIONS`; or when two rules name the same expression.
            Each of those is a reviewer who believes they recorded a compliance
            decision. The unreachable expression is the one whose absence an
            operator could not detect -- see `_unreachable_expression_fault`. The
            duplicate is refused whether or not the two agree, because an
            expression named twice is ranked in two places at once and which rule
            a package reaches would depend on where a reader stopped counting --
            and the shape the story's matrix names, one rule allowing what
            another forbids, is the one where that matters most.

    """
    if rules is None:
        return ()
    if not isinstance(rules, list):
        message = (
            f"the policy parameters at {source} record {RULES_KEY}={rules!r} for version {version!r}, which is "
            f"{type(rules).__name__} rather than a list of rules. CPM-FR-18's licence outcome is drawn from "
            f"rules a reviewer reads one per line; a value of another shape is refused rather than coerced, "
            f"because a rule set nobody meant is a compliance verdict about every package."
        )
        raise PolicyParameterError(message)

    # Sorted by the rule's *position* rather than by the rendered clause, which is
    # the same order a reviewer reads the file in. Sorting the strings put `rule
    # 10` before `rule 2`: deterministic, so nothing replayed differently, and
    # still an order nobody could follow down a twelve-rule list.
    faults = [
        f"rule {position} ({fault})"
        for position, rule in enumerate(rules)
        if (fault := _license_rule_fault(rule)) is not None
    ]
    if faults:
        message = (
            f"the policy parameters at {source} record {RULES_KEY} entries for version {version!r} that cannot "
            f"be applied: {', '.join(faults)}. Every rule is a table declaring exactly {sorted(RULE_KEYS)}, "
            f"whose {RULE_EXPRESSION_KEY} is a non-blank expression of at most "
            f"{MAX_LICENSE_EXPRESSION_CHARACTERS} characters that collectors/spdx.py can actually normalize a "
            f"stated licence to, and whose {RULE_DISPOSITION_KEY} is one of {sorted(RULE_DISPOSITIONS)} "
            f"(CPM-AD-5, CPM-AD-24). manual_review is deliberately not a disposition: it is what a licence no "
            f"rule names already reaches."
        )
        raise PolicyParameterError(message)

    recorded = [
        LicenseRule(expression=rule[RULE_EXPRESSION_KEY], disposition=rule[RULE_DISPOSITION_KEY]) for rule in rules
    ]
    # One pass over the rules keyed on the folded expression, rather than a
    # `count()` per element and a `set()` rebuilt inside the comprehension that
    # reads it. The behaviour is identical -- a duplicate is refused either way --
    # and the shape is the one a rule set of any size can be read with.
    by_expression: dict[str, list[LicenseRule]] = {}
    for rule in recorded:
        by_expression.setdefault(rule.expression.casefold(), []).append(rule)
    repeated = sorted(expression for expression, named_by in by_expression.items() if len(named_by) > 1)
    if repeated:
        named = sorted(
            f"{rule.expression!r} -> {rule.disposition}"
            for expression in repeated
            for rule in by_expression[expression]
        )
        message = (
            f"the policy parameters at {source} record {RULES_KEY} for version {version!r} naming the "
            f"expression(s) {repeated} more than once: {', '.join(named)}. The match is case-insensitive, so "
            f"two spellings of one expression are one licence -- and a licence one rule allows while another "
            f"forbids it has no verdict at all, only whichever rule a reader stopped at. Refused rather than "
            f"resolved: which of two compliance decisions stands is a reviewer's to state, not this "
            f"component's to guess."
        )
        raise PolicyParameterError(message)
    return tuple(recorded)


def _license_rule_fault(rule: object) -> str | None:
    """Return why one recorded rule cannot be applied, or `None`.

    Separated from the refusal above so every fault a *rule set* can carry is
    reported at once, on exactly the terms `_risk_label_fault` is separated: a
    reviewer correcting one entry at a time, told about one entry at a time,
    edits the file four times to learn it had four mistakes.

    Args:
        rule: One entry of the recorded rule set.

    Returns:
        A short clause naming the fault, or `None` where the rule is usable.

    """
    if not isinstance(rule, dict):
        return f"{type(rule).__name__} rather than a table"
    declared = set(rule)
    if declared != RULE_KEYS:
        missing = sorted(RULE_KEYS - declared)
        extra = sorted(declared - RULE_KEYS)
        return f"declares {sorted(declared)} rather than {sorted(RULE_KEYS)}; missing {missing}, unrecognised {extra}"
    return _rule_expression_fault(rule[RULE_EXPRESSION_KEY]) or _rule_disposition_fault(rule[RULE_DISPOSITION_KEY])


def _rule_expression_fault(expression: object) -> str | None:
    """Return why one rule's expression cannot be matched, or `None`.

    A separate function from the disposition's rather than one long chain, for
    the reason `_risk_label_fault` is separate from `_risk_order`: the two are
    about different fields with different rules, and a single function holding
    both is one a reader has to scan to find out which half they are in.

    Args:
        expression: Whatever the rule recorded under `RULE_EXPRESSION_KEY`.

    Returns:
        A short clause naming the fault, or `None` where the expression is
        usable. Deliberately **not** case-checked, unlike a severity label: SPDX
        identifiers are mixed case and a reviewer writes them as SPDX spells
        them, so `policies/licence.py` folds both sides of the comparison instead
        of this contract demanding a lowercase file no SPDX reader would
        recognise.

        The content **is** checked, in `_unreachable_expression_fault`, and that
        is a correction to an earlier draft of this function which explicitly
        declined to. A rule naming something the normalizer cannot produce is
        inert for ever, with no refusal and no log line, and that is the one
        failure an operator cannot detect from the outside: the deny half of a
        licence policy would simply do nothing.

    """
    if not isinstance(expression, str):
        return f"{RULE_EXPRESSION_KEY} is {type(expression).__name__} rather than a string"
    if not expression.strip():
        return f"{RULE_EXPRESSION_KEY} names nothing"
    if expression != expression.strip():
        return f"{RULE_EXPRESSION_KEY} carries surrounding whitespace"
    if len(expression) > MAX_LICENSE_EXPRESSION_CHARACTERS:
        return (
            f"{RULE_EXPRESSION_KEY} is longer than the {MAX_LICENSE_EXPRESSION_CHARACTERS} characters a "
            f"normalized expression can be"
        )
    return _unreachable_expression_fault(expression)


def _unreachable_expression_fault(expression: str) -> str | None:
    """Return why no evidence row could ever carry this expression, or `None`.

    **The refusal an operator is least able to detect the absence of.**
    `normalized_license` is never free text: `collectors/spdx.py` writes an
    identifier from its own table, or two or more of them joined by single spaces
    and one repeated operator, or nothing at all. A rule naming anything else
    matches no row this product can write, ever -- so it forbids nothing, permits
    nothing and restricts nothing, silently and permanently, and every affected
    package reads `manual_review`, which is indistinguishable from "no rule
    covers this". The deny half of a licence policy would fail with no refusal
    and no log line.

    The spelling most likely to be written is the one this refuses first.
    `GPL-3.0`, `GPLv3`, `AGPL-3.0` and `LGPL-2.1` are **not** recognised
    spellings: `CPM-SECURITY-S03` removed them deliberately, because each names a
    version without an `-only`/`-or-later` disposition and is therefore two
    licences. A reviewer who writes `{ expression = "GPL-3.0", disposition =
    "forbidden" }` has to be told, not accommodated -- accommodating it would be
    this component guessing which of two licences was meant, in the file whose
    whole purpose is that it does not.

    Checked case-insensitively on both halves, matching `policies/licence.py`'s
    comparison exactly: a rule the pass *would* match must not be refused here,
    and one it could never match must not be recorded.

    Args:
        expression: The rule's expression, already known to be a non-blank,
            trimmed string of a length a stored expression can hold.

    Returns:
        A short clause naming what is unreachable, or `None` where
        `collectors/spdx.py` can produce this expression.

    """
    tokens = expression.split(" ")
    if len(tokens) % 2 == 0:
        # An expression alternates operand, operator, operand, so it has an odd
        # number of tokens: one on its own, or three or more.
        # An even count is a dangling operator or a multi-token operand, and
        # reading it as either would pair an operand with whatever followed it.
        return (
            f"{RULE_EXPRESSION_KEY} is {expression!r}, which is not the shape of a normalized expression: "
            f"collectors/spdx.py writes one identifier, or two or more joined by single spaces and one "
            f"repeated operator"
        )
    unrecognised = sorted({token for token in tokens[0::2] if token.casefold() not in NORMALIZABLE_IDENTIFIERS})
    if unrecognised:
        return (
            f"{RULE_EXPRESSION_KEY} is {expression!r}, whose operand(s) {unrecognised} name nothing "
            f"collectors/spdx.py normalizes to, so no license_findings row can ever carry them and the rule "
            f"would be inert. Abbreviations of the GNU family are deliberately absent: each names a version "
            f"without an -only/-or-later disposition and is therefore two licences"
        )
    operators = {token.casefold() for token in tokens[1::2]}
    if not operators <= NORMALIZABLE_OPERATORS or len(operators) > _ONE_OPERATOR:
        return (
            f"{RULE_EXPRESSION_KEY} is {expression!r}, which joins its operands with {sorted(operators)} rather "
            f"than with one repeated operator from {sorted(NORMALIZABLE_OPERATORS)}. collectors/spdx.py refuses "
            f"a mixed or unrecognised expression outright, so no stored expression is spelled this way"
        )
    return None


def _rule_disposition_fault(disposition: object) -> str | None:
    """Return why one rule's disposition cannot be applied, or `None`.

    Args:
        disposition: Whatever the rule recorded under `RULE_DISPOSITION_KEY`.

    Returns:
        A short clause naming the fault, or `None` where it is one of
        `RULE_DISPOSITIONS`. `manual_review` is refused here like any other
        non-member and it is the plausible mistake, because it *is* a real
        outcome -- and a rule stating it would be a rule saying what a licence no
        rule names already says.

    """
    if not isinstance(disposition, str):
        return f"{RULE_DISPOSITION_KEY} is {type(disposition).__name__} rather than a string"
    if disposition not in RULE_DISPOSITIONS:
        return f"{RULE_DISPOSITION_KEY} is {disposition!r}, which is not one of {sorted(RULE_DISPOSITIONS)}"
    return None


def _risk_order(labels: object, *, version: str, source: Path | str) -> tuple[str, ...] | None:
    """Refuse a risk order that is not a list of distinct fixed lowercase labels, and return it.

    Args:
        labels: Whatever the file recorded, or `None` where it recorded nothing.
        version: The version being read, for the message.
        source: What to call the file in a refusal.

    Returns:
        The labels in the order the file states them, worst first, or `None`
        where this version records no such key. `None` is not a refusal: see
        `PARAMETER_KEYS` for why an older entry must stay readable, and
        `policies/vulnerability.py` for what a run at such a version derives
        instead -- a row with a blank risk level saying so, rather than a
        failure.

    Raises:
        PolicyParameterError: When the value is not a list; when it is empty;
            when an entry is not a string; when an entry is blank, carries
            surrounding whitespace or is not already lowercase; when an entry is
            longer than `MAX_RISK_LEVEL_CHARACTERS`; or when two entries are the
            same label. Each of those is a reviewer who believes they ranked
            something. An empty list is refused rather than read as "rank
            nothing", because a version that means to rank nothing omits the key
            -- and an empty list would silently produce a blank risk level for
            every package in the inventory, which reads exactly like a source
            that states no severities.

    """
    if labels is None:
        return None
    if not isinstance(labels, list):
        message = (
            f"the policy parameters at {source} record {RISK_ORDER_KEY}={labels!r} for version {version!r}, "
            f"which is {type(labels).__name__} rather than a list of severity labels. CPM-FR-17's risk level "
            f"is drawn from an order a reviewer reads top to bottom; a value of another shape is refused "
            f"rather than coerced."
        )
        raise PolicyParameterError(message)
    if not labels:
        message = (
            f"the policy parameters at {source} record an empty {RISK_ORDER_KEY} for version {version!r}. A "
            f"version that ranks no severity has no rule for the vulnerability pass to apply, and an empty "
            f"list would produce a blank risk level for every package -- indistinguishable from a source that "
            f"states no severities. Omit the key instead, which is refused where it is needed."
        )
        raise PolicyParameterError(message)

    faults = sorted(f"{label!r} ({fault})" for label in labels if (fault := _risk_label_fault(label)) is not None)
    if faults:
        message = (
            f"the policy parameters at {source} record {RISK_ORDER_KEY} entries for version {version!r} that "
            f"cannot be applied: {', '.join(faults)}. Every entry is a fixed lowercase severity label of at "
            f"most {MAX_RISK_LEVEL_CHARACTERS} characters (CPM-AD-5, CPM-AD-24). The comparison case-folds "
            f"the severity a *source* stated; what a derived row stores is the reviewed label from this "
            f"order, spelled exactly as it is written here."
        )
        raise PolicyParameterError(message)

    ordered = [str(label) for label in labels]
    repeated = sorted({label for label in ordered if ordered.count(label) > 1})
    if repeated:
        message = (
            f"the policy parameters at {source} record {RISK_ORDER_KEY} for version {version!r} with the "
            f"repeated label(s) {repeated}. An order is a ranking, and a label appearing twice ranks it in two "
            f"places at once -- so which risk level a finding of that severity reaches would depend on where a "
            f"reader stopped counting."
        )
        raise PolicyParameterError(message)
    return tuple(ordered)


def _risk_label_fault(label: object) -> str | None:
    """Return why one recorded severity label cannot be applied, or `None`.

    Separated from the refusal above so every fault a *list* can carry is
    reported at once. A reviewer correcting one entry at a time, told about one
    entry at a time, edits the file four times to learn it had four mistakes.

    Args:
        label: One entry of the recorded order.

    Returns:
        A short clause naming the fault, or `None` where the entry is usable.

    """
    if not isinstance(label, str):
        return f"{type(label).__name__} rather than a string"
    if not label.strip():
        return "names nothing"
    if label != label.strip():
        return "carries surrounding whitespace"
    if label != label.casefold():
        return "is not lowercase, and the comparison case-folds the source's severity rather than this label"
    if len(label) > MAX_RISK_LEVEL_CHARACTERS:
        return f"is longer than the {MAX_RISK_LEVEL_CHARACTERS} characters a stored risk level can hold"
    return None


def _interval(days: object, *, version: str, source: Path | str) -> timedelta:
    """Refuse a threshold that is not a positive whole number of days, and return the interval.

    Args:
        days: Whatever the file recorded, or `None` where it recorded nothing.
        version: The version being read, for the message.
        source: What to call the file in a refusal.

    Returns:
        The threshold as a `timedelta`.

    Raises:
        PolicyParameterError: When the value is absent, is not an integer, is a
            boolean, or is not positive. A boolean is refused explicitly because
            `isinstance(True, int)` is true in Python and TOML spells `true` in a
            way a reviewer could plausibly reach for -- and a threshold of one day
            arrived at by a typo is worse than a refusal. Zero and negatives are
            refused because an interval that is not positive makes every observed
            feedstock inactive the instant it is observed, which is a verdict
            about the whole inventory reached by arithmetic nobody intended.

    """
    if days is None:
        message = (
            f"the policy parameters at {source} record no {INACTIVITY_DAYS_KEY} for version {version!r}. "
            f"CPM-FR-40 makes the inactivity threshold a versioned policy parameter, so a version that "
            f"records none has no rule for this pass to apply -- and defaulting one would make an "
            f"unreviewed value indistinguishable from a reviewed one."
        )
        raise PolicyParameterError(message)
    if isinstance(days, bool) or not isinstance(days, int):
        message = (
            f"the policy parameters at {source} record {INACTIVITY_DAYS_KEY}={days!r} for version "
            f"{version!r}, which is {type(days).__name__} rather than a whole number of days. The threshold "
            f"is an interval a reviewer reads in days; a value of another type is refused rather than "
            f"coerced, because coercion is how a threshold nobody meant becomes a verdict about every "
            f"package."
        )
        raise PolicyParameterError(message)
    if days <= 0:
        message = (
            f"the policy parameters at {source} record {INACTIVITY_DAYS_KEY}={days} for version "
            f"{version!r}, which is not a positive interval. A threshold of zero or less calls every "
            f"feedstock inactive at the instant it was last pushed to, which is a verdict about the whole "
            f"inventory rather than a threshold."
        )
        raise PolicyParameterError(message)
    if days > MAX_INACTIVITY_DAYS:
        message = (
            f"the policy parameters at {source} record {INACTIVITY_DAYS_KEY}={days} for version "
            f"{version!r}, which is longer than the {MAX_INACTIVITY_DAYS} days an interval can express. "
            f"Refused here so the message names the file and the version: built without the check, the "
            f"constructor raises an OverflowError about magnitudes with nothing in it to say which "
            f"parameter set a reviewer has to go and correct."
        )
        raise PolicyParameterError(message)
    return timedelta(days=days)


def parameters_in(
    recorded: Mapping[str, PolicyParameters],
    *,
    version: str,
    source: Path | str,
) -> PolicyParameters:
    """Return one version's parameter set, or refuse a version nothing records.

    Pure, and separated from the read above so the unknown-version refusal -- the
    one AC 3 is actually about -- can be exercised against a mapping rather than
    against a file.

    Args:
        recorded: Every parameter set the file holds, by version.
        version: The policy version the run declared.
        source: What to call the file in the refusal, so an operator is sent to
            the file they have to edit.

    Returns:
        That version's parameter set.

    Raises:
        PolicyParameterError: When the version is not recorded. Refused rather
            than defaulted: see the module docstring. The message lists the
            versions the file does record, because "no parameters for this
            version" without them sends an operator to open the file by hand.

    """
    parameters = recorded.get(version)
    if parameters is None:
        message = (
            f"the policy parameters at {source} record nothing for policy version {version!r}. The recorded "
            f"versions are {sorted(recorded)}. CPM-FR-40 makes the threshold a versioned policy parameter "
            f"and CPM-AD-8 makes a rule set versioned data, so a run at an unrecorded version is refused "
            f"rather than given a default -- a defaulted verdict is indistinguishable from a reviewed one "
            f"in every report that reads it."
        )
        raise PolicyParameterError(message)
    return parameters


def parameters_at(path: Path) -> dict[str, PolicyParameters]:
    """Read one parameter file, or refuse the file.

    Owns only the refusals that are about a *file*: one that is missing,
    unreadable, or not text this component can decode. What the text says is
    `parameters_from`'s.

    Args:
        path: The file to read.

    Returns:
        Every parameter set it records, by version.

    Raises:
        PolicyParameterError: When the file cannot be opened or read, is not
            `PARAMETERS_ENCODING`, or says something `parameters_from` refuses.

    """
    try:
        text = path.read_text(encoding=PARAMETERS_ENCODING)
    except OSError as unreadable:
        message = (
            f"the policy parameters at {path} could not be read: {type(unreadable).__name__}: {unreadable}. "
            f"The parameter sets are a reviewed file this component ships (CPM-AD-14); a policy run is "
            f"refused rather than given a default threshold."
        )
        raise PolicyParameterError(message) from unreadable
    except UnicodeDecodeError as undecodable:
        message = (
            f"the policy parameters at {path} are not {PARAMETERS_ENCODING}: {undecodable}. A parameter set "
            f"is reviewed in a pull-request diff and is text (CPM-AD-14), and TOML is UTF-8 by "
            f"specification."
        )
        raise PolicyParameterError(message) from undecodable
    return parameters_from(text, source=path)


@memoized
def _remembered(path: Path) -> Mapping[str, PolicyParameters] | PolicyParameterError:
    """Read one parameter file once per process, remembering a refusal as readily as a parse.

    Split out of `recorded_parameters` below because `functools.cache` stores
    *returns* and re-runs on every exception, and a refusal that is not
    remembered is the hazard the memoization exists to prevent arrived at from
    the other side: a reviewed file repaired while a failing run was still going
    would begin succeeding mid-inventory, so half the packages would be judged
    under a rule set the other half never saw. So the refusal is returned as a
    value and the caller raises it.

    Args:
        path: The file to read.

    Returns:
        Every recorded parameter set as a **read-only** mapping, or the refusal
        the read raised. The parse is shared by every caller for the life of the
        process, so handing out the mutable dictionary would let one caller's
        assignment change the rule set for every later one -- which is the
        mutability `PolicyParameters` is frozen to prevent, one level up.

    """
    try:
        return MappingProxyType(parameters_at(path))
    except PolicyParameterError as refused:
        return refused


#: Forget every parse this process has made, so the next read opens the files
#: again.
#:
#: The one supported way to make a corrected parameter file take effect without
#: restarting: `recorded_parameters` reads once per process on purpose
#: (`CPM-AD-8` -- one policy version means one rule set, and a file re-read per
#: package would split a run across two of them), and this is the deliberate act
#: that says a reader knows they are changing the rules mid-process. The suite
#: uses it between substituted files; an operator's alternative is a restart,
#: which shipping a new artifact already is.
forget_recorded_parameters: Final = _remembered.cache_clear


def recorded_parameters(path: Path) -> Mapping[str, PolicyParameters]:
    """Return every parameter set one file records, reading it once per process.

    **Memoized on the path**, not on nothing. An argument-free memo over a module
    global answers about whatever file the *first* caller happened to name, so a
    later caller reading a different one would silently get the earlier parse --
    which is a suite whose substituted file stops taking effect, and a component
    whose parameter tree moved and nobody noticed.

    **Memoized deliberately at all** -- see the module docstring and `_remembered`
    above, which is also where a refusal is remembered rather than retried. The
    consequence, stated rather than discovered: a change to the shipped file takes
    effect at the next process start, or at the next `forget_recorded_parameters()`.

    Args:
        path: The file to read. `parameters_file()` in production.

    Returns:
        Every recorded parameter set, by version, as a read-only mapping.

    Raises:
        PolicyParameterError: For everything `parameters_at` refuses, on the
            first call and on every later one -- the same object each time, which
            is what makes the refusal a remembered fact about the file rather
            than a fresh opinion about it.

    """
    remembered = _remembered(path)
    if isinstance(remembered, PolicyParameterError):
        raise remembered
    return remembered


def parameters_for(version: str) -> PolicyParameters:
    """Return the parameter set a run at one policy version applies.

    The one entry point a pass calls. It composes the memoized read with the
    unknown-version refusal, and holds no rule of its own -- both halves are
    argued and exercised where they live.

    Args:
        version: The policy version the run declared, off the run's own row.

    Returns:
        That version's parameter set.

    Raises:
        PolicyParameterError: When the shipped file cannot be read, or records
            nothing for this version.

    """
    path = parameters_file()
    return parameters_in(recorded_parameters(path), version=version, source=path)


#: What separates a version's segments, for `version_key`.
VERSION_SEPARATOR: Final[str] = "."


def version_key(version: str) -> tuple[tuple[int, int | str], ...]:
    """Return a sort key that orders dotted versions numerically, segment by segment.

    Lifted here from `run_policy`'s command by `CPM-OPERATE-S03`, so that the
    command and the demo seeder derive "newest" through one rule rather than
    two: the seeder's own `sorted(...)[-1]` was a lexicographic sort that would
    have put `2026.09.10` before `2026.09.4` the day a tenth revision was
    recorded.

    Args:
        version: A recorded version, such as `2026.09.4`.

    Returns:
        One pair per segment: an all-digit segment as `(0, int)`, any other as
        `(1, str)`, so `2026.09.10` sorts after `2026.09.4`, a shorter version
        sorts before its own extensions, and a segment that is not a number
        compares against one that is without raising.

    """
    segments = version.split(VERSION_SEPARATOR)
    return tuple((0, int(segment)) if segment.isdigit() else (1, segment) for segment in segments)


def recorded_versions() -> list[str]:
    """Return every policy version the shipped parameter file records, oldest first.

    Read through `parameters_from` rather than the memoized `recorded_parameters`,
    on the terms the command set: a corrected file is believed by the next
    invocation without a restart.

    Returns:
        The versions under `version_key`'s ordering, so the last is the newest.

    Raises:
        PolicyParameterError: When the file cannot be read as a parameter set.
            Not caught: a malformed reviewed file is a misconfigured deployment
            (`CPM-AD-14`) and the refusal names the file to edit.

    """
    source = parameters_file()
    return sorted(parameters_from(source.read_text(encoding="utf-8"), source=source), key=version_key)


def newest_recorded_version() -> str:
    """Return the newest policy version the shipped parameter file records.

    What `run_policy` applies when no `--version` is given and what the demo
    seeder runs at. Read rather than written down: an unrecorded version fails
    every package (`CPM-CURRENCY-S07`), so a constant would break both on the day
    somebody added a version and not before.

    Returns:
        The newest recorded version under `version_key`'s ordering.

    Raises:
        PolicyParameterError: When the file cannot be read as a parameter set,
            or -- `parameters_from` refuses an empty version table -- records no
            version at all.

    """
    return recorded_versions()[-1]


def _priority_rules(rules: object, *, version: str, source: Path | str) -> tuple[PriorityRule, ...]:
    """Refuse a priority rule set nobody could apply, and return the rules it records.

    Args:
        rules: Whatever the file recorded, or `None` where it recorded nothing.
        version: The version being read, for the message.
        source: What to call the file in a refusal.

    Returns:
        The rules in the order the file states them, which is the order they are
        matched, or `()` where this version records none. `()` is not a refusal and
        is the shipped state -- see `PRIORITY_RULES_KEY`.

    Raises:
        PolicyParameterError: When the value is not a list, or when any entry
            cannot be applied. Every fault is reported at once and each names the
            rule by the position a reviewer counts it at, on the terms
            `_license_rules` states.

    """
    if rules is None:
        return ()
    if not isinstance(rules, list):
        message = (
            f"the policy parameters at {source} record {PRIORITY_RULES_KEY}={rules!r} for version {version!r}, "
            f"which is {type(rules).__name__} rather than a list of rules. CPM-FR-20's bucket is assigned by "
            f"top-down first-match rules, so the rule set is an ordered list; a value of another shape is "
            f"refused rather than coerced, because a rule set nobody meant ranks every package in the queue."
        )
        raise PolicyParameterError(message)

    faults = [
        f"rule {position} ({fault})"
        for position, rule in enumerate(rules)
        if (fault := _priority_rule_fault(rule)) is not None
    ]
    if faults:
        message = (
            f"the policy parameters at {source} record {PRIORITY_RULES_KEY} entries for version {version!r} "
            f"that cannot be applied: {', '.join(faults)}. Every rule is a table declaring exactly "
            f"{sorted(PRIORITY_RULE_KEYS)}, whose {PRIORITY_BUCKET_FIELD} is one of {list(PRIORITY_BUCKETS)}, "
            f"whose {PRIORITY_DESCRIPTION_FIELD} and {PRIORITY_REASON_FIELD} are non-blank and at most "
            f"{MAX_PRIORITY_TEXT_CHARACTERS} characters, and whose {PRIORITY_WHEN_FIELD} is a non-empty table "
            f"of {sorted(PRIORITY_DOMAINS)} to the verdict each must hold."
        )
        raise PolicyParameterError(message)

    return tuple(
        PriorityRule(
            bucket=rule[PRIORITY_BUCKET_FIELD],
            description=rule[PRIORITY_DESCRIPTION_FIELD].strip(),
            reason=rule[PRIORITY_REASON_FIELD].strip(),
            conditions=tuple((domain, verdict) for domain, verdict in rule[PRIORITY_WHEN_FIELD].items()),
        )
        for rule in rules
    )


def _priority_rule_fault(rule: object) -> str | None:
    """Return why one priority rule cannot be applied, or nothing.

    One return per fault rather than a collected list, on the terms
    `_license_rule_fault` states: the first thing wrong with a rule is what a
    reviewer fixes, and reporting four faults about one malformed table reads as
    four problems.

    Args:
        rule: The entry the file recorded at one position.

    Returns:
        The reason it cannot be applied, or `None` when it can.

    """
    if not isinstance(rule, dict):
        return f"{type(rule).__name__} rather than a table"
    if set(rule) != PRIORITY_RULE_KEYS:
        return f"declares {sorted(rule)} rather than exactly {sorted(PRIORITY_RULE_KEYS)}"
    if rule[PRIORITY_BUCKET_FIELD] not in PRIORITY_BUCKETS:
        return f"names bucket {rule[PRIORITY_BUCKET_FIELD]!r}, which is not one of {list(PRIORITY_BUCKETS)}"
    for field in (PRIORITY_DESCRIPTION_FIELD, PRIORITY_REASON_FIELD):
        stated = rule[field]
        if not isinstance(stated, str) or not stated.strip():
            return f"records {field}={stated!r}, and an assignment that cannot explain itself is what AC 2 forbids"
        if len(stated.strip()) > MAX_PRIORITY_TEXT_CHARACTERS:
            return (
                f"records a {field} of {len(stated.strip())} characters, and the column that "
                f"stores it takes {MAX_PRIORITY_TEXT_CHARACTERS}"
            )
    return _condition_fault(rule[PRIORITY_WHEN_FIELD])


def _condition_fault(when: object) -> str | None:
    """Return why one rule's conditions cannot be matched, or nothing.

    Args:
        when: Whatever the rule recorded under its `when` key.

    Returns:
        The reason, or `None` when the conditions are usable.

    """
    if not isinstance(when, dict):
        return f"records {PRIORITY_WHEN_FIELD} as {type(when).__name__} rather than a table"
    if not when:
        return (
            f"records an empty {PRIORITY_WHEN_FIELD}, which would match every package -- a default bucket "
            f"wearing a condition"
        )
    unknown = sorted(set(when) - PRIORITY_DOMAINS)
    if unknown:
        return f"matches on {unknown}, which no policy pass answers; the domains are {sorted(PRIORITY_DOMAINS)}"
    mistyped = sorted(domain for domain, verdict in when.items() if not isinstance(verdict, str) or not verdict)
    if mistyped:
        return f"records a non-string or blank verdict for {mistyped}"
    return None


def _score_weights(weights: object, *, version: str, source: Path | str) -> tuple[tuple[str, int], ...]:
    """Refuse a score function nobody could compute, and return the weights it records.

    Args:
        weights: Whatever the file recorded, or `None` where it recorded nothing.
        version: The version being read, for the message.
        source: What to call the file in a refusal.

    Returns:
        The `(signal, weight)` pairs in the order the file states them, or `()`
        where this version records none. `()` means no score is computed at all --
        see `PRIORITY_WEIGHTS_KEY`.

    Raises:
        PolicyParameterError: When the value is not a table, names a signal nothing
            observes, or records a weight that is not a non-negative integer at
            most `MAX_SIGNAL_WEIGHT`.

    """
    if weights is None:
        return ()
    if not isinstance(weights, dict):
        message = (
            f"the policy parameters at {source} record {PRIORITY_WEIGHTS_KEY}={weights!r} for version "
            f"{version!r}, which is {type(weights).__name__} rather than a table of signal to weight."
        )
        raise PolicyParameterError(message)

    unknown = sorted(set(weights) - PRIORITY_SIGNALS)
    if unknown:
        message = (
            f"the policy parameters at {source} weight {unknown} for version {version!r}, which the inventory "
            f"does not observe. The signals are {sorted(PRIORITY_SIGNALS)} (CPM-AD-25, PRD Open Question 3b); a "
            f"weight on anything else contributes nothing to every score, for ever, and silently."
        )
        raise PolicyParameterError(message)

    faults = sorted(
        signal
        for signal, weight in weights.items()
        if isinstance(weight, bool) or not isinstance(weight, int) or weight < 0 or weight > MAX_SIGNAL_WEIGHT
    )
    if faults:
        message = (
            f"the policy parameters at {source} record unusable weights for {faults} at version {version!r}. "
            f"A weight is a whole number from 0 to {MAX_SIGNAL_WEIGHT}: the score is normalized against the "
            f"weights recorded, so what matters is that they are comparable, and a negative one would make a "
            f"signal count against a package for being observed."
        )
        raise PolicyParameterError(message)

    return tuple((signal, weights[signal]) for signal in weights)
