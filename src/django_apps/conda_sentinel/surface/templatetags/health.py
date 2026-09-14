"""The template's half of `CPM-AD-24`: values in, no decisions taken.

Three filters and one more tag. `tone` looks a status up in `surface/tone.py`, `label`
looks it up in `surface/labels.py`, `querystring` rebuilds a URL with one parameter
changed so the pager and the sort links keep the filters a reader has applied, and
`absence_tag` prints the one text tag an absent package carries (`CPM-OPERATE-S11`)
-- a tag rather than a filter because it reads two columns, and blank for a listed
package because there is no tag to print rather than a value to hide.

**`tone` and `label` take the same value and answer different questions**, which is why
a chip passes it to both: the value picks the colour, the label is what is printed, and
neither is derived from the other. `CPM-APP-S19` is what happens when a template
derives one from the other -- it printed the value because that is what it had.

**Neither filter can produce a blank status**, which is the point of both being here
rather than inline in the template: `{{ cell.status }}` is already verbatim, and the
only way a status becomes blank on the screen is a `{% if %}` somebody adds around
it. Keeping the presentation logic in named, tested functions is what leaves the
template with nothing to be clever with.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django import template

from conda_sentinel.surface.labels import absence_tag as absence_tag_of
from conda_sentinel.surface.labels import display_label
from conda_sentinel.surface.tone import tone_of

if TYPE_CHECKING:
    from datetime import datetime

    from django.http import QueryDict

register = template.Library()


@register.filter(name="tone")
def tone(status: str) -> str:
    """Return the stylesheet tone for a status.

    Args:
        status: The status value.

    Returns:
        The tone, never blank.

    """
    return tone_of(status)


@register.filter(name="label")
def label(value: object) -> str:
    """Return what a stored value is called where a person reads it.

    The one filter every template uses for this, so no template author has to know
    which vocabulary a value came from -- `surface/labels.py` decides, and the
    templates ask.

    **It never blanks.** `tone` is here for the same reason: a status becomes invisible
    on a screen only through an `{% if %}` somebody adds around it, and a filter that
    could return `""` would be a second way. An unrecognised value comes back as
    itself.

    Args:
        value: The stored value.

    Returns:
        The label, never blank for a value that was not blank.

    """
    return display_label(value)


@register.simple_tag
def querystring(query: QueryDict, **changes: object) -> str:
    """Return this request's query string with some parameters replaced.

    What the pager and the sort links are built from. Rebuilding rather than
    appending is the whole of it: `?vuln=critical&page=2` plus `page=3` appended
    twice is a URL Django reads as page 2, so a reader paging forward would stick.

    Args:
        query: The request's `GET`.
        **changes: Parameters to set. A value of `None` removes the parameter, which
            is how "back to page one after changing a filter" is expressed.

    Returns:
        The encoded query string, with no leading `?` so a template can decide
        whether one is needed.

    """
    updated = query.copy()
    for key, value in changes.items():
        if value is None:
            updated.pop(key, None)
        else:
            updated[key] = str(value)
    return updated.urlencode()


@register.simple_tag(name="absence_tag")
def absence_tag(since: datetime | None, last_listed: datetime | None) -> str:
    """Return the text tag an absent package carries, or nothing for a listed one.

    Args:
        since: The rollup row's `inventory_absent_since`.
        last_listed: The rollup row's `inventory_last_listed`.

    Returns:
        `surface/labels.py`'s `absence_tag`, unchanged.

    """
    return absence_tag_of(since, last_listed)
