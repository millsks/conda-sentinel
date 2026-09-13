"""How a status looks, decided once, for every status there is.

`CPM-APP-S02`'s AC 4 is the whole of this module: a status of `unknown`, `not_found`,
`not_applicable` or `error` "renders as itself and never as blank or as clean".
`CPM-AD-24` says the same thing from the other end -- blank is reserved for a field
with no value and is never used for a status.

**Rendering "as itself" is the easy half; "never as clean" is the half that needs a
table.** A template that printed the value would satisfy the first and fail the
second the moment somebody gave the cell a colour, because the obvious default for a
value a stylesheet does not recognise is the neutral one -- and neutral, on a screen
where green means safe, reads as safe. So every value of every vocabulary is assigned
a tone here, and `tests/unit/django_apps/test_tone.py` fails on a value that has
none. A new outcome is a failing test rather than a silently grey chip.

**`unknown` has a tone of its own and is not a shade of `ok`.** The four sentinels
are four different unhappy answers -- nobody looked, the lookup found nothing, the
question does not apply, the lookup broke -- and `CPM-FR-5` turns on a reader being
able to tell them apart. The stylesheet draws each with a different marker for the
same reason: colour alone would collapse them for anybody who cannot distinguish the
hues, which `review-accessibility.md` says in as many words.

**A tone is not a severity.** `behind` is `tone-warn` because it is work, not
because it is dangerous; `p1` is `tone-crit` because it is urgent, not because
something is broken. The stylesheet's tones are a visual vocabulary, and mapping them
onto the policy engine's meanings is a presentation decision -- which is why it lives
here, beside the templates, and not in `policies/outcomes.py` where a pass could read
it.

**Every tone is prefixed `tone-`, and the prefix is load-bearing.** The mockups' CSS
names its chip variants `ok`, `warn`, `unknown`, `error` and so on, which are also --
four of them exactly -- `OutcomeState` values. An unprefixed table would therefore
have been a mapping from identity-confidence values to strings spelled like statuses,
which is indistinguishable, by reading or by audit, from a second confidence gate
(`CPM-AD-4`); `tests/unit/django_apps/test_confidence_gate_audit.py` said so, and it
was right to. The prefix makes a tone unmistakably not a status, here and in the
stylesheet and in the rendered class attribute.
"""

from __future__ import annotations

from typing import Final

from conda_sentinel.core.outcomes import OutcomeState
from conda_sentinel.identity.confidence import IdentityConfidence
from conda_sentinel.policies.outcomes import CurrencyOutcome
from conda_sentinel.policies.outcomes import FeedstockOutcome
from conda_sentinel.policies.outcomes import KevMembership
from conda_sentinel.policies.outcomes import PackageLicenseOutcome
from conda_sentinel.policies.outcomes import PackagePythonReadinessOutcome
from conda_sentinel.policies.outcomes import PackageVulnerabilityOutcome
from conda_sentinel.policies.outcomes import PriorityBucket
from conda_sentinel.policies.outcomes import WorkType

__all__ = [
    "INVENTORY_ACTIVE",
    "INVENTORY_RETIRED",
    "PLAIN",
    "TONED_VOCABULARIES",
    "TONES",
    "tone_of",
]

#: The two states the inventory page draws a chip for. Spelled here, beside
#: their tones, and read by the template through `tone` and `label` -- the same
#: two filters every status goes through.
INVENTORY_ACTIVE: Final[str] = "active"
INVENTORY_RETIRED: Final[str] = "retired"

#: The tone a value with no entry gets. Deliberately not `ok`.
#:
#: It is the tone of a chip with no dot and no colour -- visibly *undecorated* rather
#: than visibly fine -- so a vocabulary that grew a value nobody classified shows up
#: on the screen as something odd rather than as something safe. The audit fails
#: first; this is what happens if it somehow does not.
PLAIN: Final[str] = "tone-plain"

#: What each status looks like, by value.
#:
#: Five tones, and they are the stylesheet's: `ok`, `warn`, `crit`, `unknown`,
#: `notfound`, `na`, `error`, `plain`. The four sentinels take the four that are
#: theirs alone, so no domain value can be mistaken for one and no sentinel can be
#: mistaken for a verdict.
#:
#: **Read as one flat table rather than one per vocabulary**, because the values are
#: globally unique by construction: `outcome_type` composes each domain vocabulary
#: from `core`'s four sentinels plus members nobody else declares, and
#: `tests/unit/django_apps/test_tone.py` asserts no two vocabularies disagree about
#: a shared value.
TONES: Final[dict[str, str]] = {
    # The four sentinels, each its own tone. `CPM-FR-5`: a reader must be able to
    # tell "nobody looked" from "we looked and found nothing".
    OutcomeState.UNKNOWN.value: "tone-unknown",
    OutcomeState.NOT_FOUND.value: "tone-notfound",
    OutcomeState.NOT_APPLICABLE.value: "tone-na",
    OutcomeState.ERROR.value: "tone-error",
    OutcomeState.OK.value: "tone-ok",
    # Currency. `behind` is work rather than danger.
    "current": "tone-ok",
    "behind": "tone-warn",
    # Vulnerability, and KEV membership beside it. `not_established` is a fifth
    # unhappy answer specific to the catalogue -- there was no finding to cross-
    # reference -- and it is toned `unknown` because that is precisely what it is.
    "advisories_matched": "tone-crit",
    "no_advisory_matched": "tone-ok",
    "listed": "tone-crit",
    "not_listed": "tone-ok",
    "not_established": "tone-unknown",
    # Licence. `manual_review` is `warn` and not `unknown`: somebody has to look,
    # which is work, and the policy did reach a conclusion about that.
    "allowed": "tone-ok",
    "restricted": "tone-warn",
    "forbidden": "tone-crit",
    "manual_review": "tone-warn",
    # Python readiness. A verified verdict and an inferred one share a tone --
    # `CPM-PY314-S03` puts the difference in the value itself, and giving proof its
    # own colour would say the inferred verdict is less true rather than less
    # certain.
    "verified_ready": "tone-ok",
    "inferred_ready": "tone-ok",
    "verified_not_ready": "tone-crit",
    "inferred_not_ready": "tone-warn",
    # Feedstock presence. `absent` is `notfound` because that is what it is: the
    # lookup ran and there is no feedstock.
    "present_and_maintained": "tone-ok",
    "present_and_inactive": "tone-warn",
    "staged_recipe_pending": "tone-warn",
    "absent": "tone-notfound",
    # Priority buckets. The top three are the queue; the rest are the backlog.
    "p1": "tone-crit",
    "p2": "tone-crit",
    "p3": "tone-warn",
    "p4": "tone-plain",
    "p5": "tone-plain",
    "p6": "tone-plain",
    "p7": "tone-plain",
    "p8": "tone-plain",
    "p9": "tone-plain",
    "p10": "tone-plain",
    # Work types. Every one of them is work, so none is `ok` and none is `crit`
    # either -- a recommendation is not a severity, and `CPM-PRIORITY-S02` keeps the
    # bucket and the work type deliberately uncoupled. `already_tracked` is the one
    # exception: it is the recommendation to do nothing.
    "fix_vulnerability": "tone-warn",
    "create_recipe": "tone-plain",
    "file_tracking_issue": "tone-plain",
    "already_tracked": "tone-ok",
    "update_feedstock": "tone-plain",
    "validate_python_314": "tone-plain",
    "review_license": "tone-warn",
    "resolve_identity": "tone-warn",
    # Identity confidence. `unmapped` is `unknown` rather than `warn`: it is the
    # state `CPM-AD-4`'s gate reads, and everything downstream of it is `unknown`
    # too -- one tone across the row is the honest picture.
    IdentityConfidence.VERIFIED.value: "tone-ok",
    IdentityConfidence.INVENTORY_DERIVED.value: "tone-warn",
    IdentityConfidence.UNMAPPED.value: "tone-unknown",
    # The inventory table's two states (`CPM-OPERATE-S03`). Not a vocabulary --
    # `retired_at IS NULL` is a column, not a status -- so neither is in
    # `TONED_VOCABULARIES`; they are here so the chip the inventory page draws
    # is decided in the one place every other chip is. `retired` is `notfound`
    # because that is what the next ingestion records about it.
    INVENTORY_ACTIVE: "tone-ok",
    INVENTORY_RETIRED: "tone-notfound",
}

#: Every vocabulary a surface renders, for the audit that checks this table covers
#: them. Named here rather than in the test so a new vocabulary is added in one
#: place -- and so the omission that matters, a vocabulary nobody toned at all, is
#: visible in the source rather than only in a test file.
TONED_VOCABULARIES: Final[tuple[type, ...]] = (
    OutcomeState,
    CurrencyOutcome,
    FeedstockOutcome,
    PackageVulnerabilityOutcome,
    KevMembership,
    PackageLicenseOutcome,
    PackagePythonReadinessOutcome,
    PriorityBucket,
    WorkType,
    IdentityConfidence,
)


def tone_of(status: str) -> str:
    """Return how a status should be drawn.

    Args:
        status: The status value, verbatim as the policy engine wrote it.

    Returns:
        One of the stylesheet's tones. `PLAIN` for a value with no entry, which the
        audit exists to make impossible -- and which is undecorated rather than
        reassuring if it happens anyway.

    """
    return TONES.get(status, PLAIN)
