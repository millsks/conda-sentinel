"""What a person reads where the product stores a slug, and where the line is drawn.

Three of this product's vocabularies reach a reader: derived statuses, queue names,
and role names. **They are not treated the same, and the difference is the whole of
this module.**

**A derived status is emitted verbatim on every surface `CPM-AD-24` names, and the
HTML is not one of them.** That decision's rule lists three — "API, export, and
governed view" — and all three are read by machines. `unknown` means the same thing in
a CSV and in a JSON response precisely because nobody translated it, and an integrator
who has learned the five states can carry them between those surfaces.

The screen was never in that list. It rendered values because a template prints what it
is handed, not because a rule said to, and `CPM-APP-S20` made that a decision instead
of a default: **the HTML renders a value's label and carries the value beside it**, so
the export a reader downloads still says `advisories_matched` while the table they
downloaded it from says `Advisories matched`. The value is what is projected; the label
is how it is read.

**A queue name and a role name are the opposite case.** `identity_review` and
`security_reviewer` are storage: nobody says them aloud, they are not a vocabulary a
reader learns, and no rule requires them to survive a trip to a screen unchanged. They
were reaching the navigation, a page title, a heading and an "owned by" line -- lower
case, underscored, four places -- because the templates rendered the value where a
label existed or should have.

**So the rule is not "labels everywhere". It is: a value a person reads is either a
status this product asserts, or it has a label.** `tests/unit/django_apps/
test_display_vocabulary.py` holds it.

**The coverage screen has a fourth vocabulary, and it needed the same treatment.**
`surface/coverage.py` reports a collector as `ok`, `failing` or `never_run`, and those
are *not* `OutcomeState` values -- its own docstring argues that `never_run` is
deliberately not `unknown`, because `unknown` is an answer about a package and this is
a statement about a collector. So the rule above applies to them, and one of the three
falls foul of it.

It showed as two spellings of one idea on a single row: the *Last finished* column said
`never run`, because the template had nothing to render and wrote a sentence, while
*Status* beside it said `never_run`, because it rendered what it had. `ok` and
`failing` need no label and are not given one -- a map that spelled them out would be
the "labels everywhere" rule this module opens by rejecting.

**Two sources underneath, one module on top.** A queue's label is `Queue`'s own --
that is what a `TextChoices` label is for, and a second mapping beside it would be a
second thing to keep true. Roles have no label anywhere: `core/roles.py` is imported
at settings time and deliberately imports almost nothing, so the labels live here
rather than there. What the templates see is one module either way.

**On the `AD-` prefix.** A bare `AD-n` in this repository is an *inherited* platform
decision; a decision from this product's own architecture spine always carries the
`CPM-` prefix.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from typing import Final

from django.utils.translation import gettext_lazy as _

from conda_sentinel.core.roles import LEADERSHIP
from conda_sentinel.core.roles import PACKAGING_ENGINEER
from conda_sentinel.core.roles import SECURITY_REVIEWER
from conda_sentinel.workflow.states import Queue

if TYPE_CHECKING:
    from datetime import datetime

    from django.utils.functional import _StrPromise

__all__ = [
    "COLLECTOR_STATUS_LABELS",
    "ROLE_LABELS",
    "SPELLED_OUT",
    "absence_tag",
    "collector_status_label",
    "display_label",
    "labelled_queues",
    "queue_label",
    "role_label",
]

#: What a collector's health is called where somebody reads it.
#:
#: One entry, on purpose. `ok` and `failing` are what a person would say already; the
#: slug is the only one spelled the way this product spells *values* rather than the
#: way anybody reads them, and it was appearing beside a column that said the same
#: thing in English.
COLLECTOR_STATUS_LABELS: Final[dict[str, _StrPromise]] = {
    "never_run": _("never run"),
}

#: What each role is called on a screen.
#:
#: `core/roles.py` holds the slots -- `security_reviewer` and the rest -- and holds
#: nothing else on purpose: it is imported from `config/settings/base.py` at
#: settings-import time, before the app registry exists, and every import added to it
#: is a way for that to stop being true. A label is presentation, presentation is this
#: application's, and this is where it goes.
#:
#: Spelled out rather than derived from the slot by replacing underscores. "Platform
#: and engineering leadership" is not `leadership.title()`, and a rule that produced
#: `Security Reviewer` would be a rule nobody could correct without leaving it.
ROLE_LABELS: Final[dict[str, _StrPromise]] = {
    SECURITY_REVIEWER: _("Security review"),
    PACKAGING_ENGINEER: _("Packaging engineering"),
    LEADERSHIP: _("Platform and engineering leadership"),
}


def queue_label(queue: str) -> str:
    """Return what a queue is called on a screen.

    Args:
        queue: The queue's stored value.

    Returns:
        Its label. The stored value itself for a name that is not a declared queue --
        which no surface should be able to produce, because every one of them refuses
        an unknown queue with a 404 before rendering anything. Falling back rather
        than raising is the choice that keeps a page from 500-ing over a label.

    """
    try:
        return str(Queue(queue).label)
    except ValueError:
        return queue


def role_label(role: str) -> str:
    """Return what a role is called on a screen.

    Args:
        role: The role's slot name.

    Returns:
        Its label, or the slot itself for a name `core/roles.py` does not declare --
        which `tests/unit/django_apps/test_display_vocabulary.py` asserts cannot
        happen for any role this product uses.

    """
    return str(ROLE_LABELS.get(role, role))


def collector_status_label(status: str) -> str:
    """Return what a collector's health is called on the coverage screen.

    Only `never_run` has an entry. `ok` and `failing` are already what a person would
    say, and spelling them into a map would make the map look like a translation table
    for a vocabulary that mostly does not need one -- which is how a later reader
    concludes that every value must have a label, and gives one to a status.

    Args:
        status: What `surface/coverage.py` concluded about the collector.

    Returns:
        Its label, or the value itself for anything not declared here -- the fallback
        `queue_label` and `role_label` make for the same reason: a label is not worth
        a 500.

    """
    return str(COLLECTOR_STATUS_LABELS.get(status, status))


def labelled_queues() -> list[dict[str, str]]:
    """Return every queue as the navigation renders it.

    Returns:
        One entry per queue in declaration order, carrying the value the URL needs
        and the label a reader sees. A pair rather than a label alone, because the
        navigation needs both and a template that derived one from the other would be
        the thing this module exists to stop.

    """
    return [{"value": queue.value, "label": str(queue.label)} for queue in Queue]


#: The values whose label is not their words with the separators taken out.
#:
#: Everything else derives, and deriving is what keeps this map short: a vocabulary of
#: forty values with forty hand-written labels is forty chances to disagree with the
#: value, and the next person to add a status would have to find this file. What cannot
#: derive is a value carrying a name, an acronym or a version number -- `kev` is an
#: initialism, `pypi` is spelled two ways and neither is `Pypi`, and `py314` is a
#: Python version with the dot taken out.
SPELLED_OUT: Final[dict[str, _StrPromise]] = {
    # Two letters, and deriving makes them `Ok`, which nobody writes.
    "ok": _("OK"),
    "kev": _("KEV"),
    "kev_findings": _("KEV findings"),
    "pypi_release": _("PyPI release"),
    "pypi_release_snapshots": _("PyPI release snapshots"),
    "py314_verification": _("Python 3.14 verification"),
    "python_verification_results": _("Python 3.14 verification results"),
    "validate_python_314": _("Validate Python 3.14"),
    "conda_package": _("Conda package"),
    "conda_artifact": _("Conda artifact"),
    "conda_package_snapshots": _("Conda package snapshots"),
}


def display_label(value: object) -> str:
    """Return what one stored value is called where a person reads it.

    **The one entry point the templates use**, so a template author never has to know
    which vocabulary a value came from. Four sources, in order, and the order is the
    specific before the general:

    1. A queue, whose label is `Queue`'s own -- that is what a `TextChoices` label is
       for, and a second mapping beside it would be a second thing to keep true.
    2. A role, from `ROLE_LABELS`, because `core/roles.py` is imported at settings time
       and deliberately imports almost nothing.
    3. `SPELLED_OUT`, for the values whose label cannot be derived.
    4. Derivation: separators out, first letter up, the rest left alone.

    **Sentence case, not title case.** Django's automatic `TextChoices` label
    title-cases every word and produces `Not Listed`, which is a heading rather than a
    thing somebody says. Deriving here rather than reading those labels is what makes
    the whole product read one way.

    Args:
        value: The stored value. Anything not a string is returned as its own text,
            so a template handing this a number or `None` renders rather than raising.

    Returns:
        The label. The value's own text where nothing is declared and nothing derives,
        which is the fallback `queue_label` and `role_label` already make: a label is
        not worth a 500.

    """
    if not isinstance(value, str) or not value.strip():
        return str(value)
    if (labelled := queue_label(value)) != value:
        return labelled
    if (labelled := role_label(value)) != value:
        return labelled
    if value in SPELLED_OUT:
        return str(SPELLED_OUT[value])
    # Underscores only. A hyphen inside a value is usually a hyphen in the words --
    # `inventory-derived` is *inventory-derived*, not "inventory derived" -- while an
    # underscore is always this product's separator standing in for a space.
    words = value.replace("_", " ").strip()
    return words[:1].upper() + words[1:]


def absence_tag(since: datetime | None, last_listed: datetime | None) -> str:
    """Return the text tag an absent package carries on every surface, or nothing.

    `CPM-FR-38` and `CPM-OPERATE-S11`: a package the inventory no longer lists is
    *labelled* wherever it appears -- the package page header, the health list's
    name cell, a queue row, a report row -- and never silently dropped. The tag is
    text rather than a status chip: absence is an observation the inventory
    collector made, not a verdict a pass reached, and a chip would put it in the
    vocabulary `surface/tone.py` audits. Read from the rollup's two columns only,
    which are cut-off bound and one query away on every list.

    Args:
        since: `PackageHealth.inventory_absent_since`, or `None` for a listed
            package.
        last_listed: `PackageHealth.inventory_last_listed`, which the after-run
            step sets beside `since`; `None` is tolerated and simply not said.

    Returns:
        `"absent from the inventory since YYYY-MM-DD (last listed YYYY-MM-DD)"`, the
        parenthesis omitted when no listing is recorded, or `""` for a package the
        inventory lists -- the one place a blank is right, because there is no tag
        to print rather than a value to hide.

    """
    if since is None:
        return ""
    tag = _("absent from the inventory since %(since)s") % {"since": since.date().isoformat()}
    if last_listed is None:
        return str(tag)
    return str(_("%(tag)s (last listed %(last_listed)s)") % {"tag": tag, "last_listed": last_listed.date().isoformat()})
