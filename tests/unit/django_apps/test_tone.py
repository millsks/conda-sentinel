"""Every status a surface can render has a tone, and none of them is reassuring by default.

`CPM-APP-S02`'s AC 4 has two halves. "Renders as itself" is satisfied by printing the
value and is asserted where the value is printed, in
`tests/integration/django_apps/test_package_health_view.py`. "Never as blank or as
clean" is this module, and it is the half that needs an audit: the obvious default
for a value a stylesheet does not recognise is the neutral one, and neutral, on a
screen where green means safe, reads as safe.

So the sweep is over the vocabularies rather than over the table. A new outcome added
to any of them -- a fifth licence verdict, an eleventh priority bucket -- fails here
on the day it is declared, rather than appearing on the health view as an
undecorated chip nobody notices.

**The four sentinels are checked apart from everything else**, because `CPM-FR-5`
turns on a reader being able to tell them apart: nobody looked, the lookup found
nothing, the question does not apply, the lookup broke. Four different unhappy
answers, four different tones, and none of them the tone that means fine.

**And the tones are checked for not being statuses.** The mockups' CSS names its chip
variants `ok`, `warn`, `unknown`, `error` -- four of which are also `OutcomeState`
values -- so an unprefixed tone table would be indistinguishable, by reading or by
audit, from a mapping of confidence values to statuses. `tone.py` prefixes every tone
`tone-`; this is what holds that.

No database, no network, no requests.
"""

from __future__ import annotations

from typing import Final

import pytest
from django.db import models

from conda_sentinel.core.outcomes import OutcomeState
from conda_sentinel.core.runs import RunState
from conda_sentinel.identity.confidence import IdentityConfidence
from conda_sentinel.surface.tone import PLAIN
from conda_sentinel.surface.tone import TONED_VOCABULARIES
from conda_sentinel.surface.tone import TONES
from conda_sentinel.surface.tone import tone_of

#: The prefix every tone carries, and the reason it exists. See the module docstring.
TONE_PREFIX: Final[str] = "tone-"

#: The tone that means "nothing is wrong here". Named so the cases that assert a
#: value must *not* have it read as the claim they are making.
REASSURING: Final[str] = "tone-ok"

#: The tone `CPM-FR-5`'s four unhappy answers must each have, and each differently.
SENTINEL_TONES: Final[dict[str, str]] = {
    OutcomeState.UNKNOWN.value: "tone-unknown",
    OutcomeState.NOT_FOUND.value: "tone-notfound",
    OutcomeState.NOT_APPLICABLE.value: "tone-na",
    OutcomeState.ERROR.value: "tone-error",
}

#: Every value any rendered vocabulary can hold, with the vocabulary that declares
#: it, so a failure names both. Built rather than written out -- restating a closed
#: set in a test is the same defect the source refuses.
EVERY_VALUE: Final[tuple[tuple[str, str], ...]] = tuple(
    (vocabulary.__name__, value)
    for vocabulary in TONED_VOCABULARIES
    for value in vocabulary.values  # type: ignore[attr-defined]
)


@pytest.mark.parametrize(("vocabulary", "value"), EVERY_VALUE, ids=str)
def test_every_value_of_every_rendered_vocabulary_has_a_tone(vocabulary: str, value: str) -> None:
    """The sweep: a status with no tone is a chip that looks like nothing is wrong.

    Args:
        vocabulary: The declaring type's name, for the failure message.
        value: The status value.

    """
    assert value in TONES, (
        f"{vocabulary}.{value} has no tone. A value the table does not carry renders undecorated, which on a "
        f"screen where green means safe is the one appearance a status must never have by accident."
    )


def test_the_vocabulary_roster_is_not_empty() -> None:
    """The anti-vacuity guard: an empty roster would make the sweep above prove nothing.

    A parametrize over no cases is a skipped test, and a skipped test reads in a
    report as a gate that ran.
    """
    assert TONED_VOCABULARIES != ()
    assert len(EVERY_VALUE) > len(OutcomeState.values)


@pytest.mark.parametrize(("value", "expected"), sorted(SENTINEL_TONES.items()))
def test_each_sentinel_has_its_own_tone(value: str, expected: str) -> None:
    """`CPM-FR-5`: four unhappy answers, told apart.

    Args:
        value: The sentinel.
        expected: The tone it must carry.

    """
    assert tone_of(value) == expected


def test_no_sentinel_is_toned_as_reassuring() -> None:
    """The claim AC 4 actually makes, stated as one assertion.

    "Never as clean" -- so whatever else changes about the palette, none of the four
    may end up wearing the tone that means fine.
    """
    assert REASSURING not in {tone_of(value) for value in SENTINEL_TONES}


def test_the_four_sentinels_do_not_share_a_tone_with_each_other() -> None:
    """Distinguishable, not merely non-green.

    Toning all four `tone-unknown` would satisfy the case above and lose exactly the
    distinction `CPM-FR-5` is about.
    """
    assert len({tone_of(value) for value in SENTINEL_TONES}) == len(SENTINEL_TONES)


def test_a_value_no_vocabulary_declares_is_undecorated_rather_than_fine() -> None:
    """The fallback, which the audit exists to make unreachable and which still matters.

    If a value ever does slip past the sweep -- a status read out of a database
    written by an older version, say -- it gets the tone that draws no dot and no
    colour, not the one that says nothing is wrong.
    """
    assert tone_of("a-value-no-vocabulary-declares") == PLAIN
    assert PLAIN != REASSURING


def test_every_tone_is_prefixed_so_it_cannot_be_read_as_a_status() -> None:
    """The prefix, and why `tests/unit/django_apps/test_confidence_gate_audit.py` wanted it.

    Unprefixed, `TONES` would map identity-confidence values to strings spelled
    exactly like `OutcomeState` values -- which is indistinguishable from a second
    confidence gate. The prefix is what makes a tone unmistakably not a status, in
    the source, in the stylesheet and in the rendered class attribute.
    """
    assert all(tone.startswith(TONE_PREFIX) for tone in TONES.values()), sorted(set(TONES.values()))
    assert PLAIN.startswith(TONE_PREFIX)
    assert set(TONES.values()) & set(OutcomeState.values) == set()


def test_no_two_vocabularies_disagree_about_a_shared_value() -> None:
    """What lets `TONES` be one flat table rather than one per vocabulary.

    `outcome_type` composes each domain vocabulary from `core`'s four sentinels plus
    members nobody else declares, so a value means one thing wherever it appears. If
    that stopped being true -- two domains both declaring `pending`, meaning
    different things -- a single table would tone one of them wrongly and this is
    where that shows up.
    """
    meanings: dict[str, set[str]] = {}
    for vocabulary in TONED_VOCABULARIES:
        for value, label in vocabulary.choices:  # type: ignore[attr-defined]
            meanings.setdefault(value, set()).add(str(label))

    ambiguous = {value: sorted(labels) for value, labels in meanings.items() if len(labels) > 1}

    assert ambiguous == {}, (
        f"these values are declared with different labels by different vocabularies: {ambiguous}. TONES is one "
        f"flat table and would tone one of them by the other's meaning."
    )


def test_the_toned_vocabularies_are_the_ones_a_surface_renders() -> None:
    """The roster is checked for being a roster of vocabularies, not of anything else.

    A `TONED_VOCABULARIES` entry that was not a `TextChoices` would have no `values`
    and the sweep would silently cover nothing for it.
    """
    for vocabulary in TONED_VOCABULARIES:
        assert issubclass(vocabulary, models.TextChoices), vocabulary
        assert vocabulary.values, vocabulary  # type: ignore[attr-defined]


def test_identity_confidence_is_toned_because_the_health_view_shows_it() -> None:
    """The one vocabulary that is not an outcome, and is on every row of the table.

    `unmapped` is toned `tone-unknown` rather than `tone-warn` deliberately: it is
    the state `CPM-AD-4`'s gate reads, and every derived column on that row is
    `unknown` too, so one tone across the row is the honest picture rather than an
    amber cell beside six grey ones.
    """
    assert tone_of(IdentityConfidence.UNMAPPED.value) == SENTINEL_TONES[OutcomeState.UNKNOWN.value]
    assert tone_of(IdentityConfidence.VERIFIED.value) == REASSURING


#: The run ledger's five states and the tone each wears (`CPM-OPERATE-S08`).
RUN_STATE_TONES: Final[dict[str, str]] = {
    RunState.RUNNING.value: "tone-info",
    RunState.SUCCEEDED.value: "tone-ok",
    RunState.PARTIAL.value: "tone-warn",
    RunState.FAILED.value: "tone-crit",
    RunState.SKIPPED.value: "tone-plain",
}


@pytest.mark.parametrize(("value", "expected"), sorted(RUN_STATE_TONES.items()))
def test_each_run_state_has_the_tone_the_page_draws_it_with(value: str, expected: str) -> None:
    """The five states, toned rather than plain, so the in-flight panel and the ledger stop rendering grey.

    `running` is the one `info` in the table: something is happening and nothing
    is wrong yet. `skipped` is deliberately undecorated -- the window declining to
    observe again is not a failure and must not look like one.

    Args:
        value: The state.
        expected: Its tone.

    """
    assert RunState in TONED_VOCABULARIES
    assert tone_of(value) == expected


def test_a_running_run_is_neither_reassuring_nor_a_sentinel() -> None:
    """`info` is its own tone: not `ok`, and not one of the four `CPM-FR-5` reserves for the sentinels."""
    assert tone_of(RunState.RUNNING.value) != REASSURING
    assert tone_of(RunState.RUNNING.value) not in set(SENTINEL_TONES.values())
