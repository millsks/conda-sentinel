"""The tables in the new reference pages, reconciled against what the code declares.

`CPM-DOCS-S05` documented three subsystems that had no page of their own -- identity
and authorization, the asynchronous work, and the queues -- and each page's spine is a
**table**: seven environment variables, thirteen tasks, four queues, seven
transitions.

A table is the most useful shape for a reader and the most dangerous one to leave
unchecked. Prose that goes stale reads as vague; a table that goes stale reads as
precise and is wrong, and the reader has no way to tell. A documented state machine
missing a transition is worse than no documented state machine, because somebody plans
around it.

So every one of those tables is swept here against its declaration. The rule is the
same one `test_documentation_commands.py` applies to commands: **documentation is
pasted, not read**, and a wrong table fails a person rather than a test.

Reads Markdown and imports declarations. No database, no requests.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

import pytest

from conda_sentinel.core.permissions import PRODUCT_ROLES
from conda_sentinel.core.queues import Queue
from conda_sentinel.core.roles import ROLE_ENVIRONMENT_VARIABLES
from conda_sentinel.workflow.states import QUEUE_OWNERS
from conda_sentinel.workflow.states import TRANSITIONS
from config.authorization.claims import CLAIMS_ENVIRONMENT_VARIABLES

#: The documentation root, from this file rather than from a layout assumption.
DOCS: Final[Path] = Path(__file__).resolve().parents[3] / "docs" / "conda-sentinel"

AUTHORIZATION: Final[Path] = DOCS / "authorization.md"
ASYNCHRONOUS: Final[Path] = DOCS / "asynchronous-work.md"
QUEUES: Final[Path] = DOCS / "the-queues.md"
INVENTORY: Final[Path] = DOCS / "managing-the-inventory.md"

#: Every page this module sweeps. Parametrized so a page that is deleted or renamed
#: fails here rather than making every sweep below pass over an empty string.
PAGES: Final[tuple[Path, ...]] = (AUTHORIZATION, ASYNCHRONOUS, QUEUES, INVENTORY)

#: A task name as either the code or the documentation writes it.
A_TASK_NAME: Final[re.Pattern[str]] = re.compile(r"\bcpm\.[a-z_]+\.[a-z_0-9]+\b")


def text(page: Path) -> str:
    """Return one page's text.

    Args:
        page: Which page.

    Returns:
        Its content.

    """
    return page.read_text(encoding="utf-8")


@pytest.mark.parametrize("page", PAGES, ids=lambda page: page.name)
def test_the_page_this_module_sweeps_exists(page: Path) -> None:
    """Because a sweep over a missing file reads as a clean sweep.

    Args:
        page: The page that must be there.

    """
    assert page.is_file(), page
    assert text(page).strip() != ""


# ---------------------------------------------------------------------------
# authorization.md
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("variable", CLAIMS_ENVIRONMENT_VARIABLES + ROLE_ENVIRONMENT_VARIABLES)
def test_every_configured_variable_is_named_where_somebody_configures_it(variable: str) -> None:
    """Seven variables, none with a default, and an unset one refuses at start-up.

    A deployment engineer reading this page is doing so *because* they have to set
    them. One missing from the page is one they do not set, and what they get is a
    start-up refusal naming a variable they have never seen.

    Args:
        variable: The variable that must be documented.

    """
    assert variable in text(AUTHORIZATION), variable


@pytest.mark.parametrize("role", PRODUCT_ROLES)
def test_every_product_role_is_explained(role: str) -> None:
    """Three slots, and the page has to name all three by their slot names.

    Slot names rather than group names, which is the distinction the page exists to
    teach: what the group is called is the deployment's business.

    Args:
        role: The role slot.

    """
    assert role in text(AUTHORIZATION), role


def test_the_refusal_event_an_operator_alerts_on_is_documented() -> None:
    """`CPM-AD-13` requires a refusal to be logged with the acting identity.

    An operator alerting on authorization failures queries one string, and a page that
    told them to alert on the wrong one would leave the alert silent.
    """
    from conda_sentinel.core.permissions import REFUSAL_EVENT  # noqa: PLC0415 - read beside the claim

    assert REFUSAL_EVENT in text(AUTHORIZATION)


# ---------------------------------------------------------------------------
# asynchronous-work.md
# ---------------------------------------------------------------------------


def declared_task_names() -> set[str]:
    """Return every task name this product registers.

    Imported from the modules that declare them rather than from the Celery registry,
    so this module needs no configured app.

    Returns:
        The names.

    """
    from conda_sentinel.collectors import tasks as collector_tasks  # noqa: PLC0415 - after django.setup()
    from conda_sentinel.collectors.sweep import SWEEP_TASK_NAME  # noqa: PLC0415 - as above
    from conda_sentinel.core.queues import EXPORT_JOB_TASK_NAME  # noqa: PLC0415 - as above
    from conda_sentinel.core.tasks import POLICY_RUN_TASK_NAME  # noqa: PLC0415 - as above

    names = {
        value for name, value in vars(collector_tasks).items() if name.endswith("_TASK_NAME") and isinstance(value, str)
    }
    return names | {SWEEP_TASK_NAME, EXPORT_JOB_TASK_NAME, POLICY_RUN_TASK_NAME}


def test_every_task_is_in_the_catalogue() -> None:
    """The page's whole claim is that it lists them *all*.

    A reader planning a schedule from a catalogue missing an entry plans a system that
    never runs one of its own tasks -- which is exactly the state the page's own
    warning about `cpm.policy.run` describes, arrived at by accident.
    """
    documented = set(A_TASK_NAME.findall(text(ASYNCHRONOUS)))
    missing = declared_task_names() - documented

    assert missing == set(), f"the task catalogue is missing {sorted(missing)}"


def test_the_catalogue_invents_no_task() -> None:
    """The other direction, and the one a reader cannot detect.

    A name that does not exist is a name somebody puts in a `PeriodicTask` row, where
    it fails at fire time with nothing on the page to contradict.
    """
    documented = set(A_TASK_NAME.findall(text(ASYNCHRONOUS)))
    invented = documented - declared_task_names()

    assert invented == set(), f"these documented tasks do not exist: {sorted(invented)}"


@pytest.mark.parametrize("queue", list(Queue), ids=lambda queue: queue.value)
def test_every_celery_queue_is_documented(queue: Queue) -> None:
    """Four workload classes, and the page argues why they are four.

    A queue nothing documents is a queue nobody sizes, and the symptom is work that
    waits behind unrelated work.

    Args:
        queue: The queue that must be documented.

    """
    assert queue.value in text(ASYNCHRONOUS), queue.value


def test_the_two_unscheduled_tasks_are_still_unscheduled() -> None:
    """The page's loudest warning, and it must stop being made the day it stops being true.

    Nothing enqueues `cpm.policy.run` or `cpm.collect.inventory` -- no beat entry, no
    chained call. If a later story schedules either, this fails and the warning comes
    out; a page still warning about it would send an operator to add a second, duplicate
    schedule entry.
    """
    from django.conf import settings  # noqa: PLC0415 - read at call time

    from conda_sentinel.collectors.tasks import INGEST_TASK_NAME  # noqa: PLC0415 - as above
    from conda_sentinel.core.tasks import POLICY_RUN_TASK_NAME  # noqa: PLC0415 - as above

    scheduled = {entry["task"] for entry in settings.CELERY_BEAT_SCHEDULE.values()}

    assert POLICY_RUN_TASK_NAME not in scheduled, "a schedule now fires the policy run; update the warning"
    assert INGEST_TASK_NAME not in scheduled, "a schedule now fires inventory ingestion; update the warning"
    assert "Two tasks nothing fires" in text(ASYNCHRONOUS)


def test_every_scheduled_entry_is_in_the_beat_table() -> None:
    """Nine sweeps, and an operator reads this table to know what should have run.

    An entry missing from it is a nightly job nobody knows to look for when it stops.
    """
    from django.conf import settings  # noqa: PLC0415 - read at call time

    page = text(ASYNCHRONOUS)
    missing = [name for name in settings.CELERY_BEAT_SCHEDULE if name not in page]

    assert missing == [], f"the beat table is missing {sorted(missing)}"


# ---------------------------------------------------------------------------
# the-queues.md
# ---------------------------------------------------------------------------


def test_every_transition_is_in_the_documented_table() -> None:
    """A documented state machine missing a move is one somebody plans around.

    Each row is checked as a *pair* -- from and to on one line of the table -- rather
    than as two words present somewhere on the page, because every state name appears
    many times and a sweep for words alone would pass on a table with no rows at all.
    """
    page = text(QUEUES)
    # Anchored on the *reason* column, so the queue-and-owner table two sections up --
    # which has the same shape in its first two cells -- is not read as transitions.
    rows = {
        (match.group(1), match.group(2))
        for match in re.finditer(
            r"^\| `([a-z_]+)` \| `([a-z_]+)` \| [^|]+ \| [^|]*\b(?:yes|no)\b[^|]* \|", page, re.MULTILINE
        )
    }
    declared = {(transition.from_state, transition.to_state) for transition in TRANSITIONS}

    assert declared - rows == set(), f"undocumented transitions: {sorted(declared - rows)}"
    assert rows - declared == set(), f"documented transitions that do not exist: {sorted(rows - declared)}"


def test_the_transition_that_demands_a_reason_is_marked_as_one() -> None:
    """It is the only one, and it is the only one restricted to a single role.

    A reader who learned that acceptance needs no justification writes an integration
    that is refused at the one moment it matters -- somebody accepting a risk.
    """
    demanding = [transition for transition in TRANSITIONS if transition.requires_justification]
    page = text(QUEUES)

    assert len(demanding) == 1, [transition.to_state for transition in demanding]
    (accepted,) = demanding
    row = re.search(rf"^\| `{accepted.from_state}` \| `{accepted.to_state}` \|.*$", page, re.MULTILINE)
    assert row is not None, accepted.to_state
    assert "yes" in row.group(0), row.group(0)
    assert all(role in row.group(0) for role in accepted.required_roles), row.group(0)


@pytest.mark.parametrize(("queue", "owner"), sorted(QUEUE_OWNERS.items()))
def test_every_queue_is_documented_with_the_role_that_owns_it(queue: str, owner: str) -> None:
    """Getting this pair wrong sends somebody to ask for the wrong group.

    Args:
        queue: The queue name.
        owner: The role slot that owns it.

    """
    page = text(QUEUES)
    row = re.search(rf"^\| `{queue}` \| `{owner}` \|.*$", page, re.MULTILINE)

    assert row is not None, f"{queue} is not documented as owned by {owner}"


def test_the_read_only_reality_is_stated() -> None:
    """Because a reader who expects a button and finds none concludes it is broken.

    The queue screens render work and offer no way to move it; every transition is an
    API call. That is a real limitation and the page says so rather than describing
    the machine as though a person could drive it.
    """
    page = text(QUEUES)

    assert "read-only" in page
    assert "workflow-items" in page


# ---------------------------------------------------------------------------
# managing-the-inventory.md
# ---------------------------------------------------------------------------


def test_the_watchlist_columns_are_the_columns_the_file_declares() -> None:
    """Eight names, exactly, and an undefined column is refused rather than ignored.

    A reader adding a column this page invented gets a refused ingestion; one missing
    a required column gets a refused row. Both are read off the shipped file's own
    header here, so the page cannot drift from the contract.
    """
    from conda_sentinel.collectors.watchlist import WATCHLIST_COLUMNS  # noqa: PLC0415 - after django.setup()

    page = text(INVENTORY)
    undocumented = sorted(column for column in WATCHLIST_COLUMNS if f"`{column}`" not in page)

    assert undocumented == [], undocumented
    # The count too: the header is "exactly these names and nothing else", so a page
    # that documented seven of eight and a ninth that does not exist would satisfy the
    # sweep above on the seven and mislead on both of the others.
    assert str(len(WATCHLIST_COLUMNS)) in page or "eight" in page


# ---------------------------------------------------------------------------
# onboarding.md -- the primer, which is a curriculum rather than a reference
# ---------------------------------------------------------------------------

#: The guided path. Swept here rather than in a module of its own because what it
#: carries is the same kind of claim: tables about the code, which go stale silently.
ONBOARDING: Final[Path] = DOCS / "onboarding.md"

#: Shorter than this and the primer has been emptied rather than edited. A round
#: number, and the point is the order of magnitude: the page is thousands of words, so
#: anything near this is a file somebody truncated.
A_SUBSTANTIAL_PAGE: Final[int] = 1000


def test_the_primer_exists_and_says_something() -> None:
    """Because every sweep below reads it, and a missing file reads as a clean sweep."""
    assert ONBOARDING.is_file()
    assert len(text(ONBOARDING)) > A_SUBSTANTIAL_PAGE


def test_the_primer_names_every_collector_and_the_table_it_writes() -> None:
    """Part 3's table is a reader's index of where the data comes from.

    A collector missing from it is a source somebody does not know is being read --
    which matters here more than usual, because three of the eleven ship with no source
    declared and observe nothing until an operator configures one. Somebody who never
    learned a collector exists never learns theirs is inert.
    """
    from conda_sentinel.core.registry import registered_collectors  # noqa: PLC0415 - after django.setup()

    page = text(ONBOARDING)
    missing = [
        f"{collector.name} ({collector.evidence_model.__name__})"
        for collector in registered_collectors()
        if collector.name not in page or collector.evidence_model.__name__ not in page
    ]

    assert missing == [], f"the collector roster is missing {missing}"


def test_the_primer_is_honest_about_which_collectors_ship_without_a_source() -> None:
    """The three that observe nothing until configured are the ones to say so about.

    A primer that listed all eleven as though they worked out of the box would send a new
    maintainer looking for a bug in the vulnerability column, which is reading
    `unknown` for the honest reason.
    """
    page = text(ONBOARDING)

    assert "you declare" in page
    assert "observe nothing" in page or "observes nothing" in page


@pytest.mark.parametrize("state", ["ok", "not_applicable", "not_found", "unknown", "error"])
def test_the_primer_explains_every_outcome_state(state: str) -> None:
    """Five states, and three of them are about the absence of an answer.

    A reader who learned four of them has learned a vocabulary with a hole in exactly
    the place this product's whole argument lives.

    Args:
        state: The state that must be explained.

    """
    assert state in text(ONBOARDING), state


def test_the_primer_writes_the_precedence_order_the_way_the_code_declares_it() -> None:
    """Worst-first, and getting it backwards inverts every aggregate verdict.

    Read off `OutcomeState`'s own order rather than compared against a literal here,
    so this cannot agree with a page that is wrong by both being wrong the same way.
    """
    from conda_sentinel.core.outcomes import PRECEDENCE  # noqa: PLC0415 - after django.setup()

    # The arrow line itself, not the first occurrence of each word on the page. Every
    # state is named several times before that line -- `ok` appears in the vocabulary
    # table above it -- so a sweep for first positions measures the order of the prose
    # rather than the order the page states, which is how the first version of this
    # case failed.
    arrows = re.search(r"^\s*([a-z_]+(?:\s*→\s*[a-z_]+)+)\s*$", text(ONBOARDING), re.MULTILINE)

    assert arrows is not None, "the primer states no precedence order at all"
    stated = [state.strip() for state in arrows.group(1).split("→")]
    assert stated == [str(state.value) for state in PRECEDENCE], (
        f"the primer states {stated}; the code declares {[str(state.value) for state in PRECEDENCE]}"
    )


def test_the_primer_links_to_every_page_it_is_the_path_through() -> None:
    """A curriculum that does not reach a page leaves that page unfindable in order.

    The primer's whole job is to be the sequence; the other pages are references it
    sends people to. One added later and not linked from here is one a new maintainer
    meets only by scrolling the navigation.
    """
    page = text(ONBOARDING)
    siblings = {path.name for path in DOCS.glob("*.md") if path.name not in {"onboarding.md", "index.md"}}
    unlinked = sorted(name for name in siblings if f"({name}" not in page and f"({name}#" not in page)

    assert unlinked == [], f"the primer links to no {unlinked}"
