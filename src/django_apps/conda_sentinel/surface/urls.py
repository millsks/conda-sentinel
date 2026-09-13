"""`CPM-AD-19`'s app-level URLconf: this app's HTML surfaces, namespaced.

The decision gives every app under `src/django_apps/` "an app-level `urls.py` with
`app_name` for any HTML views" beside an `api/` subpackage whose routing is central.
This is the first of them.

**Namespaced, and the namespace is load-bearing.** Every reverse in a template goes
through `conda_sentinel:` so a route added by a later story cannot collide with one
of the platform's -- `home`, `about` and the account flows are all unprefixed names
in the root URLconf, and a product route called `home` would silently win or lose
depending on include order.

**Mounted by the root URLconf and not by discovery.** `AD-8` forbids entry-point
discovery, and a URLconf that mounted itself would be exactly that.
"""

from __future__ import annotations

from django.urls import path

from conda_sentinel.surface.views import CoverageView
from conda_sentinel.surface.views import ExportJobDownloadView
from conda_sentinel.surface.views import ExportJobView
from conda_sentinel.surface.views import HomeView
from conda_sentinel.surface.views import InventoryView
from conda_sentinel.surface.views import PackageDetailView
from conda_sentinel.surface.views import PackageHealthView
from conda_sentinel.surface.views import QueueView
from conda_sentinel.surface.views import ReportExportView
from conda_sentinel.surface.views import ReportView
from conda_sentinel.surface.views import ThemeView

app_name = "conda_sentinel"

urlpatterns = [
    # The application's own front page, at the root *of the application* -- which is
    # `/conda-sentinel/` since `CPM-APP-S13` mounted this URLconf under the prefix.
    #
    # `CPM-APP-S12` had briefly put it at the root of the *service*, replacing the
    # accelerator's landing page. The reasoning there is unchanged and is still what
    # this satisfies: nobody visiting this service is looking for the template it was
    # generated from. What changed is that the service's root now leads here rather
    # than being here, so a second application adopted beside this one has a root of
    # its own to be given.
    #
    # Still one canonical URL for the page: the redirect in `config/urls.py` is a
    # redirect and not a second mount, so a link somebody pastes into a ticket and
    # the one in the nav are the same address.
    path("", HomeView.as_view(), name="home"),
    path("coverage/", CoverageView.as_view(), name="coverage"),
    path("packages/", PackageHealthView.as_view(), name="package-health"),
    # Keyed on the canonical name so a link pasted into a ticket says which package
    # it is about. `<str:>` rather than `<slug:>`: a canonical name may carry a dot
    # or an underscore -- `ruamel.yaml`, `backports.zoneinfo` -- and `slug` matches
    # neither, which would make exactly the packages with awkward names unreachable.
    path("packages/<str:canonical_name>/", PackageDetailView.as_view(), name="package-detail"),
    # One route for three queues, because they are three filtered views over one
    # table (`CPM-AD-22`) and three routes would invite three views. `<str:>` rather
    # than an enumeration in the pattern: the closed set is `QUEUE_OWNERS`, and a
    # segment outside it is a 404 the view raises with a message naming the queues
    # that do exist.
    path("queues/<str:queue>/", QueueView.as_view(), name="queue"),
    # One route for six reports, on the same terms the queues take one for three:
    # they are six questions over one rollup, and six routes would invite six views
    # and six chances to forget the provenance every report has to state.
    path("reports/<str:slug>/", ReportView.as_view(), name="report"),
    # One route, two methods, and the split is `CPM-AD-9`. `GET` streams the file
    # when the report fits inside a request and refuses when it does not; `POST`
    # hands the work off. A `GET` that enqueued would make a bookmark, a prefetch or
    # a link checker create jobs.
    path("reports/<str:slug>/export/", ReportExportView.as_view(), name="report-export"),
    # Where a handed-off export is looked at, and where its file comes from. Keyed on
    # the surrogate id rather than on the report, because two people can be preparing
    # the same report and each is asking about their own request.
    path("exports/<int:pk>/", ExportJobView.as_view(), name="export-job"),
    path("exports/<int:pk>/download/", ExportJobDownloadView.as_view(), name="export-job-download"),
    # The governed inventory table and its three forms (`CPM-OPERATE-S03`): one
    # route, `GET` lists and `POST` writes through the service, leadership only.
    # Not in the navigation, which lists what every role can read; the page is
    # reached from the operator documentation.
    path("inventory/", InventoryView.as_view(), name="inventory"),
    # `CPM-APP-S11`. Not under any of the surfaces above, because the control is on
    # every one of them -- including the sign-in page, which belongs to the platform.
    path("theme/", ThemeView.as_view(), name="theme"),
]
