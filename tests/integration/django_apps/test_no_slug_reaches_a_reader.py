"""Every view, swept for the thing a template prints when it has a value and no label.

`CPM-APP-S20`. `CPM-APP-S15` fixed it in the navigation and `CPM-APP-S19` fixed it on
one column of the coverage table, both after somebody noticed. Forty-seven distinct
slugs were reaching a reader when this module was written, across eleven views --
collector names, evidence table names, mapping kinds, work types, priority buckets and
every derived status.

Fixing them one report at a time is not a strategy, so this is the sweep: **render every
view and read what is actually on it.** A template that prints a value where a label
belongs fails here rather than in a screenshot.

The rule is `surface/labels.py`'s: an underscore is how this product spells a *value*,
and nothing a person reads is spelled that way. The exceptions are declared below and
each is argued, because an exception list nobody can justify is how a sweep stops
meaning anything.

**Not the API and not the CSV.** `CPM-AD-24` binds those to the value, and
`test_the_machine_surfaces_still_speak_in_values` holds that from the other side -- so
this module cannot be satisfied by relabelling everything everywhere.
"""

from __future__ import annotations

import re
from http import HTTPStatus
from typing import Final

import pytest
from django.conf import settings
from django.contrib.auth.models import Group
from django.test import override_settings
from django.urls import reverse
from rest_framework.test import APIClient

from conda_sentinel.collectors.digest import EMAIL_SETTING
from conda_sentinel.collectors.digest import WEBHOOK_URL_SETTING
from conda_sentinel.collectors.digest import compose_digest
from conda_sentinel.collectors.source_release import COLLECTOR_NAME as SOURCE_RELEASE
from conda_sentinel.core.clock import FixedClock
from conda_sentinel.core.collection import ALLOWANCE_REFUSAL_MARKER
from conda_sentinel.core.delivery import DeliveryOutcome
from conda_sentinel.core.permissions import PRODUCT_ROLES
from conda_sentinel.core.runs import RunState
from conda_sentinel.identity.confidence import IdentityConfidence
from conda_sentinel.surface.reports import REPORTS
from conda_sentinel.workflow.models import WorkflowItem
from conda_sentinel.workflow.states import ItemState
from conda_sentinel.workflow.states import Queue
from tests.collectors import A_DIGEST_EMAIL
from tests.collectors import A_DIGEST_WEBHOOK_URL
from tests.collectors import RecordedWebhookDeliverer
from tests.factories import UserFactory

from .test_coverage_view import NOW
from .test_coverage_view import a_package
from .test_coverage_view import a_rollup_row
from .test_coverage_view import a_run
from .test_digest import a_ledger_row

pytestmark = pytest.mark.integration

#: A slug as a reader would meet one: lowercase words joined by this product's
#: separator. Deliberately not "any underscore" -- a URL, a CSS class and a template
#: name all carry them, and none of those is text somebody reads.
A_SLUG: Final[re.Pattern[str]] = re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b")

#: Everything between tags, which is what a person actually sees.
A_TAG: Final[re.Pattern[str]] = re.compile(r"<[^>]+>")
A_SCRIPT: Final[re.Pattern[str]] = re.compile(r"<(script|style)\b.*?</\1>", re.DOTALL | re.IGNORECASE)

#: A stored record reproduced verbatim, which is not a template printing a value.
#: The Digests page shows the digest's text as the webhook and the mail carried
#: it -- `CPM-OPERATE-S09` fixes that the page and the delivery are the same
#: bytes, so an operator can hold one against the other -- and that text names
#: collectors as the ledger spells them. It is the one block a `<pre
#: class="record">` marks, and it is the only markup this sweep reads past;
#: everything else on that page is labelled and swept like any other.
A_RECORD: Final[re.Pattern[str]] = re.compile(r"<pre class=\"record\">.*?</pre>", re.DOTALL)

#: Slugs that are allowed to reach a reader, and why each one is.
#:
#: **`finding_facts`' keys.** `advisory_id=CVE-2026-48588 affected_range=>=6.0.0,<6.0.7`
#: is one stored string, composed by `core/finding_keys.py`, and its *values* may
#: contain spaces -- a licence expression is `Apache-2.0 OR BSD-3-Clause`. So it cannot
#: be split reliably at render time, and making it readable means changing what is
#: stored rather than how it is printed. That is a story, not a labelling pass.
#:
#: **A parameter file's own key.** `license_rules` appears inside a sentence explaining
#: that a policy version records none. It is the name of a key in a TOML file, quoted
#: the way a setting name is quoted, and labelling it would break the reader's ability
#: to go and find it.
PERMITTED: Final[frozenset[str]] = frozenset(
    {
        "advisory_id",
        "affected_range",
        "normalized_license",
        "license_rules",
    }
)


def every_view() -> list[str]:
    """Return every URL a person reads, enumerated from the code.

    Returns:
        The paths, so a report or a queue added later is swept the day it exists.

    """
    return [
        reverse("conda_sentinel:home"),
        reverse("conda_sentinel:package-health"),
        reverse("conda_sentinel:coverage"),
        reverse("conda_sentinel:digest"),
        *(reverse("conda_sentinel:queue", kwargs={"queue": queue.value}) for queue in Queue),
        *(reverse("conda_sentinel:report", kwargs={"slug": report.slug}) for report in REPORTS),
    ]


def a_reader_of_everything() -> APIClient:
    """Return a client holding every product role.

    Returns:
        An authenticated client.

    """
    user = UserFactory.create()
    for role in PRODUCT_ROLES:
        user.groups.add(Group.objects.get(name=getattr(settings.ROLE_CONTRACT, role)))
    client = APIClient()
    client.force_login(user)
    return client


def visible_text(body: str) -> str:
    """Return what a reader sees, with the markup taken out.

    Args:
        body: The rendered page.

    Returns:
        The text between the tags.

    """
    return A_TAG.sub(" ", A_SCRIPT.sub(" ", A_RECORD.sub(" ", body)))


@pytest.fixture
def _something_to_read() -> None:
    """Put a row on every table these views render.

    **Without this the sweep passes by rendering nothing.** An empty database gives
    every page its "nothing matches" row, no status cell is produced, and a template
    that prints values instead of labels is indistinguishable from one that prints
    nothing at all -- which is how the first version of this module passed with the
    defect deliberately reintroduced.

    The statuses are contributed explicitly rather than left at their defaults so each
    is a *determinate* value: a table of `unknown` would exercise one word, and
    `unknown` is the one word that happens to need no label.
    """
    run = a_run()
    behind = a_package("django")
    a_rollup_row(
        behind,
        run,
        currency_status="behind",
        feedstock_presence_status="present_and_maintained",
        priority_status="p1",
        work_type_status="fix_vulnerability",
    )
    unmapped = a_package("internal-telemetry-sdk", confidence=IdentityConfidence.UNMAPPED)
    a_rollup_row(unmapped, run)
    # `in_progress` rather than `open`: `open` is one word and needs no label, so a
    # queue seeded only with it would leave the state column exercised by a value that
    # cannot fail. Verified -- reverting that column's label while every item was
    # `open` failed nothing.
    # `workflow_claimed_only_while_in_progress` requires one, and rightly: an item
    # nobody holds cannot be in progress.
    # A digest whose text names collectors as values, with a failed delivery
    # so the delivery chips and detail are exercised, and a stale figure so the
    # per-collector table has every column filled.
    a_ledger_row(SOURCE_RELEASE)
    a_ledger_row(SOURCE_RELEASE, package=behind, status=RunState.FAILED, detail=ALLOWANCE_REFUSAL_MARKER)
    with override_settings(**{WEBHOOK_URL_SETTING: A_DIGEST_WEBHOOK_URL, EMAIL_SETTING: A_DIGEST_EMAIL}):
        compose_digest(
            clock=FixedClock(instant=NOW),
            deliverer=RecordedWebhookDeliverer(outcome=DeliveryOutcome(delivered=False, detail="HTTP 500")),
        )
    holder = UserFactory.create()
    for queue in Queue:
        WorkflowItem.objects.create(
            claimed_by=holder,
            finding_key=f"fixture:{queue.value}",
            finding_facts="normalized_license=MIT channel=conda-forge",
            package=behind,
            queue=queue.value,
            state=ItemState.IN_PROGRESS,
            opened_at=NOW,
            changed_at=NOW,
        )


def test_the_sweep_covers_every_view() -> None:
    """Because an enumeration that returned nothing would pass every case below."""
    expected = 4 + len(Queue) + len(REPORTS)

    assert len(every_view()) == expected
    assert len(set(every_view())) == expected


@pytest.mark.django_db
@pytest.mark.usefixtures("_something_to_read")
@pytest.mark.parametrize("path", every_view(), ids=lambda path: path.strip("/").replace("/", "-"))
def test_no_slug_reaches_a_reader(path: str) -> None:
    """The sweep this module exists for.

    Args:
        path: The view under test.

    """
    response = a_reader_of_everything().get(path)

    assert response.status_code == HTTPStatus.OK, path
    found = {slug for slug in A_SLUG.findall(visible_text(response.content.decode())) if slug not in PERMITTED}

    assert found == set(), f"{path} shows these as slugs: {sorted(found)}"


@pytest.mark.django_db
def test_the_sweep_would_notice_a_slug() -> None:
    """A regex over a page that happens to be clean reports nothing either way.

    So the detector is shown a slug and asked to find it -- otherwise a pattern that
    had stopped matching anything would look exactly like a product with no slugs
    left in it.
    """
    assert A_SLUG.findall(visible_text("<td>advisories_matched</td>")) == ["advisories_matched"]
    assert A_SLUG.findall(visible_text("<td>Advisories matched</td>")) == []


@pytest.mark.django_db
def test_the_machine_surfaces_still_speak_in_values() -> None:
    """The other half of the decision, and the half that keeps `CPM-AD-24` true.

    That decision's rule names three surfaces -- "API, export, and governed view" --
    and all three are read by machines. The HTML was never among them; it rendered
    values because a template prints what it is handed.

    So the screen labels and the machine surfaces do not, and **both are asserted**. A
    change that relabelled everything everywhere would satisfy the sweep above and
    break every integrator parsing a status.
    """
    # `confidence` is a status and lives on the rollup, so one row is enough. The
    # vulnerability status is on its own derived table (`CPM-AD-21`), and seeding that
    # here would be testing the fixture rather than the projection.
    a_rollup_row(a_package("django"), a_run())
    client = a_reader_of_everything()

    api = client.get(reverse("conda_sentinel_api:package-health")).json()["results"][0]
    csv_body = client.get(reverse("conda_sentinel:report-export", kwargs={"slug": "unmapped-identities"}))

    assert api["confidence"] == "verified", api
    assert "Verified" not in csv_body.content.decode()
