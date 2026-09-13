"""`CPM-AD-13`: a surface declares the role it needs, and nothing else decides one.

Two acceptance criteria of `CPM-APP-S01` land here, and they are the same rule read
from both ends.

**AC 2 -- every view declares, and the check is implemented once.** The declaration
half is a live sweep of the URLconf: every view this product registers must name a
`core.permissions` class. The "once" half is a source sweep, and it is the half with
teeth: `CPM-AD-13` names the failure as "each view inventing its own role check", and
a view that both declares `AnyProductRole` *and* branches on `request.user.groups` in
its `get_queryset` has satisfied the declaration and reintroduced the defect. So no
module outside `core/permissions.py` may read a membership, a group name, `has_perm`,
`is_superuser` or `is_staff` at all.

**AC 4 -- domain apps touch none of the four global keys.** `config/startup/allowlist.py`
already refuses these four from an adopted app's contributed settings
(`FORBIDDEN_CONTRIBUTABLE_KEYS`, and it is exactly `CPM-APP-S01`'s four), so the
startup gate covers the contribution path. It does not cover a module that assigns
one directly, monkeypatches it, or reaches it through `settings`. That is what is
swept here, and the two are reconciled so a key added to one is not missing from the
other.

**Why the source sweeps are load-bearing and the URLconf sweep is not, yet.**
`CPM-APP-S02` writes this product's first view. Today the URLconf holds the inherited
platform's one viewset and nothing of this product's, so the live sweep passes over
an empty set -- which is why every detector below is measured against fixture source
first, and why the sweeps are over source rather than over what happens to be
registered. An audit whose subject list is empty and whose detector was never run is
two ways of proving nothing at once.

Reads source and the URLconf: no database, no network, no requests. The refusal that
`CPM-AD-13`'s third clause requires is a real request and lives in
`tests/integration/django_apps/test_role_permissions.py`.
"""

from __future__ import annotations

import ast
import importlib
from typing import TYPE_CHECKING
from typing import Final

import pytest
from django.urls import get_resolver

from conda_sentinel.core.permissions import PRODUCT_ROLES
from conda_sentinel.core.permissions import AnyProductRole
from conda_sentinel.core.permissions import RolePermission
from conda_sentinel.core.permissions import RoleRequiredMixin
from conda_sentinel.core.roles import ROLE_ENVIRONMENT_VARIABLES
from config.startup.allowlist import FORBIDDEN_CONTRIBUTABLE_KEYS
from tests.source_scan import SRC_ROOT
from tests.source_scan import dotted_name
from tests.source_scan import parse
from tests.source_scan import project_files

if TYPE_CHECKING:
    from pathlib import Path

#: Where this product's own code lives, relative to `SRC_ROOT`.
#:
#: The source sweeps are scoped to it rather than run over `src/`: `config/` and
#: `django_service/` are the inherited platform, which is *supposed* to read
#: `is_staff` (the admin), assign `MIDDLEWARE` (it composes the settings) and
#: resolve claims to groups (`AD-10`). Sweeping them would produce a table of
#: exemptions longer than the rule.
PRODUCT_TREE: Final[str] = "django_apps/conda_sentinel"

#: The dotted spelling of every authorization question a surface must not ask.
#:
#: Read as a *receiver-independent* suffix match -- `user.is_superuser` and
#: `request.user.is_superuser` are the same read -- because the receiver is exactly
#: what varies between the eight places somebody would write it.
#:
#: `groups` is here because a role is a group membership and reading one is deciding
#: a role. `has_perm`/`has_perms` are here because `CPM-AD-14`'s override permission
#: is Django's permission system and belongs on the model, not in a view branch.
#: `is_superuser`/`is_staff` are here because the module docstring in
#: `core/permissions.py` argues at length that a superuser bypass is the one
#: authorization decision nobody can audit, and a sweep is what keeps that argument
#: from being re-litigated one view at a time.
AUTHORIZATION_READS: Final[frozenset[str]] = frozenset(
    {"groups", "has_perm", "has_perms", "is_staff", "is_superuser"},
)

#: The one module licensed to ask them, because it is the implementation.
#:
#: Singular, and that is the acceptance criterion restated: "the check implemented
#: once". `core/roles.py` is not listed and does not need to be -- it declares group
#: *names* and reads no membership, which the exemption test below asserts rather
#: than assumes.
THE_IMPLEMENTATION: Final[str] = f"{PRODUCT_TREE}/core/permissions.py"

#: The reads licensed outside the implementation, spelled exactly and spent exactly.
#:
#: Two entries, and neither is a role check -- which is the reason they are licensed
#: rather than the reason they are suspicious. `CPM-AD-14` makes the audited identity
#: override a governed human write and gates it on a Django *permission*, and
#: `core/permissions.py`'s own docstring says in as many words that it "stays where
#: Django's permission system already puts it". `_require_permitted` is that gate.
#: `CPM-OPERATE-S03` added the second governed write -- the inventory table -- and
#: its service carries the same gate on the same terms, one read, spent here.
#:
#: Recorded as the exact list the sweep must find rather than as a licensed
#: *attribute*, on the terms `tests/unit/django_apps/test_clock_audit.py` sets: a
#: module that has used its one recorded read gets no second one for free, and a
#: module whose recorded read has been deleted fails here rather than keeping a
#: licence for something that is no longer there.
RECORDED_EXEMPTIONS: Final[dict[str, tuple[str, ...]]] = {
    f"{PRODUCT_TREE}/collectors/inventory.py": ("actor.has_perm",),
    f"{PRODUCT_TREE}/identity/services.py": ("actor.has_perm",),
}

#: The four global keys `CPM-APP-S01`'s AC 4 puts out of reach, reconciled against
#: the startup gate's own list below. Named through the import rather than rewritten
#: so the two lists cannot drift; the alias exists to say what they are *here*.
FORBIDDEN_SETTINGS_KEYS: Final[frozenset[str]] = FORBIDDEN_CONTRIBUTABLE_KEYS

#: Every module the sweeps read: this product's shipped source. Built at import so a
#: violation is a named failing case rather than a line in an assertion message.
PRODUCT_MODULES: Final[tuple[Path, ...]] = tuple(
    path for path in project_files(SRC_ROOT, skip_migrations=True) if PRODUCT_TREE in path.as_posix()
)

#: The same list less the one implementation, which is what the authorization sweep
#: reads. Excluded here rather than skipped inside the case: `tests/unit/test_suite_policy.py`
#: bans `pytest.skip` in a test body, and it is right to -- a skipped case reads in a
#: report as a gate that ran.
SWEPT_FOR_AUTHORIZATION: Final[tuple[Path, ...]] = tuple(
    path for path in PRODUCT_MODULES if path.relative_to(SRC_ROOT).as_posix() != THE_IMPLEMENTATION
)

#: Source each detector is measured against, so a clean sweep means the detector
#: works rather than that it never fired.
A_VIEW_DECIDING_FOR_ITSELF: Final[str] = """
class QueueView(APIView):
    def get_queryset(self):
        if request.user.groups.filter(name="cpm-security-reviewer").exists():
            return Everything.objects.all()
        return Nothing.objects.none()
"""

A_VIEW_LETTING_SUPERUSERS_THROUGH: Final[str] = """
class QueueView(APIView):
    def has_permission(self, request, view):
        return request.user.is_superuser
"""

A_VIEW_CHECKING_A_DJANGO_PERMISSION: Final[str] = """
class OverrideView(APIView):
    def post(self, request):
        if not request.user.has_perm("identity.override_package_identity"):
            raise PermissionDenied
"""

A_VIEW_THAT_ONLY_DECLARES: Final[str] = """
class QueueView(APIView):
    permission_classes = [AnyProductRole]
    queryset = PackageHealth.objects.all()
"""

A_MODULE_ASSIGNING_A_GLOBAL_KEY: Final[str] = """
MIDDLEWARE = [*MIDDLEWARE, "conda_sentinel.core.middleware.Whatever"]
"""

A_MODULE_REACHING_FOR_ONE: Final[str] = """
def widen():
    settings.REST_FRAMEWORK["DEFAULT_PERMISSION_CLASSES"] = []
"""

A_MODULE_NAMING_NONE_OF_THEM: Final[str] = """
REST_FRAMEWORK_IS_NOT_MENTIONED = "DEFAULT_PAGINATION_CLASS"
"""


def authorization_reads(tree: ast.Module) -> list[str]:
    """Return every authorization question asked in a module.

    Matches on the attribute rather than on the receiver: `user.is_superuser`,
    `request.user.is_superuser` and `self.request.user.is_superuser` are one read
    written three ways, and the way it is written is what varies.

    Args:
        tree: The parsed module.

    Returns:
        The dotted spelling of each offending read, in source order.

    """
    return [
        dotted_name(node) or node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr in AUTHORIZATION_READS
    ]


def forbidden_keys(tree: ast.Module) -> list[str]:
    """Return every mention of one of AC 4's four global settings keys.

    Any mention, not just an assignment. A module that names
    `DEFAULT_PERMISSION_CLASSES` is either setting it, reading it to branch on it, or
    subscripting `settings.REST_FRAMEWORK` to replace it -- and the last two are how
    the first one arrives without a diff that looks like it.

    Args:
        tree: The parsed module.

    Returns:
        Each key named, in source order, with repeats kept so a count means
        something.

    """
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and node.value in FORBIDDEN_SETTINGS_KEYS:
            found.append(node.value)
        elif isinstance(node, ast.Name) and node.id in FORBIDDEN_SETTINGS_KEYS:
            found.append(node.id)
        elif isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_SETTINGS_KEYS:
            found.append(node.attr)
    return found


def registered_views() -> list[type]:
    """Return every view class this product registers, by walking the resolver.

    Returns:
        The view classes whose module is inside this product's package. DRF and
        Django both hang the class off the callable as `cls` (`as_view`) or
        `view_class` (`View.as_view`), and a function-based view has neither --
        which is itself a finding, so those are reported rather than skipped, by
        `test_every_registered_view_declares_a_product_permission`.

    """
    found: list[type] = []
    pending = list(get_resolver().url_patterns)
    while pending:
        entry = pending.pop()
        nested = getattr(entry, "url_patterns", None)
        if nested is not None:
            pending.extend(nested)
            continue
        callback = getattr(entry, "callback", None)
        view = getattr(callback, "cls", None) or getattr(callback, "view_class", None) or callback
        if view is not None and getattr(view, "__module__", "").startswith("conda_sentinel."):
            found.append(view)
    return found


# ---------------------------------------------------------------------------
# AC 2, first half: what a surface declares.
# ---------------------------------------------------------------------------


def test_the_common_class_requires_all_three_roles() -> None:
    """`AnyProductRole` is what a read surface declares, and it is not a formality.

    `CPM-AD-13` grants read access to evidence to all three roles, so naming all
    three is correct -- and the class still refuses an authenticated user in none of
    the groups, which is the state the platform's `IsAuthenticated` floor lets
    through.
    """
    assert issubclass(AnyProductRole, RolePermission)
    assert AnyProductRole.required_roles == frozenset(PRODUCT_ROLES)


def test_the_roles_a_surface_may_require_are_the_three_the_contract_declares() -> None:
    """The permission vocabulary is the role contract's, not a second list beside it.

    A fourth role invented here would be a requirement nothing can satisfy, because
    nothing provisions a group for it -- so `PRODUCT_ROLES` is reconciled against the
    contract's own environment variables by count and by construction.
    """
    assert len(PRODUCT_ROLES) == len(ROLE_ENVIRONMENT_VARIABLES)
    assert len(set(PRODUCT_ROLES)) == len(PRODUCT_ROLES)


def declares_a_role(view: type) -> bool:
    """Report whether a registered surface declares the role it requires.

    Two shapes, because `CPM-AD-19` gives every app both an `api/` subpackage and an
    app-level `urls.py` "for any HTML views". A DRF view declares a `RolePermission`
    subclass in `permission_classes`; a Django view mixes in `RoleRequiredMixin` and
    names `required_roles`. `core/permissions.py` decides both, which is what the
    acceptance criterion asks for -- "the check is implemented once" is about where
    the comparison lives, not about how many kinds of view there are.

    **Each shape has a seam for the surface whose roles depend on its URL**, and both
    are recognised here: `roles_required` on a Django view, `get_permissions` on a
    DRF one. `CPM-AD-22`'s queues need it -- which role owns a queue is a property of
    the queue -- and `CPM-APP-S07` needed the DRF half for the same three queues over
    HTTP. Recognising an override is all this static sweep can do; that the override
    returns the *right* role for each queue is asserted by a named case in
    `tests/unit/django_apps/test_api_contract_audit.py`, on the principle that a
    proxy belongs in a sweep and the rule itself belongs in a case.

    Args:
        view: The registered view class.

    Returns:
        True when it declares through any of the three mechanisms, and names somebody
        through it -- a `RoleRequiredMixin` that declares no roles *and* overrides
        nothing keeps the base's empty default, which refuses everybody and is what
        this audit exists to catch.

    """
    if any(
        isinstance(entry, type) and issubclass(entry, RolePermission)
        for entry in getattr(view, "permission_classes", ())
    ):
        return True
    if "get_permissions" in vars(view):
        return True
    if not issubclass(view, RoleRequiredMixin):
        return False
    # Either a declared set, or an override of the seam that computes one. The second
    # exists for `CPM-AD-22`'s queues: which role owns a queue is a property of the
    # queue, so the answer depends on the URL. A view that overrides `roles_required`
    # has said where its answer comes from, which is what this audit is asking; a
    # view that overrides nothing and declares nothing has said nothing, and inherits
    # the mixin's empty default, which refuses everybody.
    overrides_the_seam = "roles_required" in vars(view)
    return bool(view.required_roles) or overrides_the_seam


#: The registered surfaces that deliberately declare no role, and why each does.
#:
#: **Spelled exactly and spent exactly**, on the terms `test_app_layering_audit.py`
#: sets for its recorded imports: `test_every_ungated_surface_is_still_ungated` below
#: fails on an entry that has acquired a role or stopped existing, so this cannot go
#: on licensing nothing.
#:
#: One entry, and it should stay hard to add a second. `CPM-AD-13` scopes access to
#: *evidence*, and a theme is not evidence -- it is a property of the screen somebody
#: is looking at. Requiring a role would also break the thing the control is for:
#: `CPM-APP-S12` brings the sign-in page into this product's shell and the control
#: goes with it, and a reader meets this product there, before they hold any role at
#: all. A control that was visible and inert on that page would be worse than none.
RECORDED_UNGATED_SURFACES: Final[dict[str, str]] = {
    "conda_sentinel.surface.views.ThemeView": (
        "CPM-APP-S11's theme control. It reads and writes no evidence -- it stores one of three words in a "
        "cookie -- and it has to work on the sign-in page, which a reader reaches before they hold any role"
    ),
}


def test_every_registered_view_declares_a_product_permission() -> None:
    """AC 2: every view, viewset and report names the role it requires.

    Written as one loop rather than a parametrize because the subject list was
    empty until `CPM-APP-S02` added the first product view, and a parametrize over
    an empty list is a skipped case -- which reads in a report as a gate that ran.

    The declaration is checked against the classes themselves rather than against a
    name, so a surface cannot satisfy it with a class of its own that happens to be
    called something similar.
    """
    undeclared = [
        name
        for view in registered_views()
        if not declares_a_role(view) and (name := f"{view.__module__}.{view.__name__}") not in RECORDED_UNGATED_SURFACES
    ]

    assert undeclared == [], (
        f"these surfaces declare no role: {undeclared}. CPM-AD-13: the platform's IsAuthenticated floor says "
        f"somebody is signed in and nothing about what they may see."
    )


@pytest.mark.parametrize("recorded", sorted(RECORDED_UNGATED_SURFACES))
def test_every_ungated_surface_is_still_ungated(recorded: str) -> None:
    """An exemption that has stopped being real is one quietly widening the sweep.

    Two ways it stops being real: the view is gone, or it has acquired a role and no
    longer needs licensing. Both fail here rather than going unnoticed -- the second
    is the one worth catching, because a stale entry means the next view added to that
    module inherits an exemption nobody granted it.

    Args:
        recorded: The dotted name of the exempted view.

    """
    module_name, _, class_name = recorded.rpartition(".")
    view = getattr(importlib.import_module(module_name), class_name, None)

    assert view is not None, f"{recorded} is exempted and no longer exists."
    assert not declares_a_role(view), (
        f"{recorded} now declares a role, so its exemption in RECORDED_UNGATED_SURFACES is spent and should be "
        f"removed -- an exemption that licenses nothing is one the next view in that module inherits."
    )


def test_the_ungated_surfaces_are_reachable_and_few() -> None:
    """The exemption list is about *registered* views, and it should stay short.

    A list that named views nobody routes would be describing an intention rather than
    the deployment, and one that grew would mean `CPM-AD-13` had quietly become
    advisory.
    """
    registered = {f"{view.__module__}.{view.__name__}" for view in registered_views()}

    assert set(RECORDED_UNGATED_SURFACES) <= registered, sorted(set(RECORDED_UNGATED_SURFACES) - registered)
    assert len(RECORDED_UNGATED_SURFACES) < len(registered) // 2


def test_the_sweep_has_a_registered_view_to_sweep() -> None:
    """The anti-vacuity guard the case above needs, and did not have until now.

    `CPM-APP-S01` shipped the permission machinery with no product view to apply it
    to, and said so. The moment one exists the audit becomes load-bearing -- and a
    later refactor that unmounted the URLconf would otherwise turn it silently back
    into a test over nothing.
    """
    assert registered_views() != []


# ---------------------------------------------------------------------------
# AC 2, second half: the check is implemented once.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (A_VIEW_DECIDING_FOR_ITSELF, "request.user.groups"),
        (A_VIEW_LETTING_SUPERUSERS_THROUGH, "request.user.is_superuser"),
        (A_VIEW_CHECKING_A_DJANGO_PERMISSION, "request.user.has_perm"),
    ],
    ids=["own-group-check", "superuser-bypass", "django-permission"],
)
def test_the_detector_finds_a_view_deciding_for_itself(source: str, expected: str) -> None:
    """Three ways to reinvent the check, three detections.

    They do not look alike and only the first mentions a role at all, which is why
    the sweep is by attribute rather than by anything resembling a role name.

    Args:
        source: The offending view.
        expected: The read the detector should report.

    """
    assert authorization_reads(ast.parse(source)) == [expected]


def test_the_detector_passes_a_view_that_only_declares() -> None:
    """The anti-vacuity half: declaring a permission class is the *correct* shape."""
    assert authorization_reads(ast.parse(A_VIEW_THAT_ONLY_DECLARES)) == []


@pytest.mark.parametrize("path", SWEPT_FOR_AUTHORIZATION, ids=lambda path: str(path.relative_to(SRC_ROOT)))
def test_no_module_but_the_implementation_decides_authorization(path: Path) -> None:
    """AC 2's "implemented once", swept rather than declared.

    A view that declares `AnyProductRole` and then branches on `request.user.groups`
    in `get_queryset` has satisfied the declaration and reintroduced exactly the
    defect `CPM-AD-13` names. So the question is not only whether a surface declares,
    but whether anything else answers.

    Args:
        path: The module under test.

    """
    relative = path.relative_to(SRC_ROOT).as_posix()

    assert authorization_reads(parse(path)) == list(RECORDED_EXEMPTIONS.get(relative, ())), (
        f"{relative} decides authorization for itself. CPM-AD-13 puts the check in "
        f"{THE_IMPLEMENTATION}; a surface declares the roles it needs and asks nothing else."
    )


def test_the_implementation_is_exempted_and_still_needs_it() -> None:
    """The exemption is spent, so the sweep is not licensing an empty file.

    And `core/roles.py` is checked from the other direction: it declares the group
    *names*, and if it ever read a membership it would be a second implementation
    with no exemption -- which the sweep above would catch, and this asserts is
    currently true rather than currently unexercised.
    """
    implementation = SRC_ROOT / THE_IMPLEMENTATION
    swept = {path.relative_to(SRC_ROOT).as_posix() for path in SWEPT_FOR_AUTHORIZATION}

    assert implementation.is_file()
    assert authorization_reads(parse(implementation)) != []
    assert authorization_reads(parse(SRC_ROOT / PRODUCT_TREE / "core/roles.py")) == []
    assert THE_IMPLEMENTATION not in swept
    assert len(SWEPT_FOR_AUTHORIZATION) + 1 == len(PRODUCT_MODULES)


@pytest.mark.parametrize(("relative", "licensed"), sorted(RECORDED_EXEMPTIONS.items()))
def test_every_recorded_exemption_is_still_a_real_read(relative: str, licensed: tuple[str, ...]) -> None:
    """A licence for a read nobody makes any more is a hole with a paragraph beside it.

    The sweep above spends each entry exactly, so a *second* read fails there. This
    is the other direction: an entry whose read has been deleted or renamed would go
    on licensing something, silently, for whoever writes the next one.

    Args:
        relative: The exempted module.
        licensed: The reads it is licensed for.

    """
    module = SRC_ROOT / relative

    assert module.is_file(), relative
    assert licensed != ()
    assert authorization_reads(parse(module)) == list(licensed)


# ---------------------------------------------------------------------------
# AC 4: the four global keys.
# ---------------------------------------------------------------------------


def test_the_four_keys_are_the_ones_the_startup_gate_already_refuses() -> None:
    """AC 4's list and `AD-8`'s list are the same list, and that is not a coincidence.

    `config/startup/allowlist.py` refuses these four from an adopted app's
    *contributed* settings. `CPM-APP-S01` names the same four for this product's own
    modules. Reconciled here so a fifth key added to either is a failure rather than
    a gap: the startup gate covers the contribution path and this file covers direct
    assignment, and a key in one list only is covered on one path only.
    """
    assert sorted(FORBIDDEN_SETTINGS_KEYS) == [
        "AUTHENTICATION_BACKENDS",
        "DEFAULT_AUTHENTICATION_CLASSES",
        "DEFAULT_PERMISSION_CLASSES",
        "MIDDLEWARE",
    ]


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (A_MODULE_ASSIGNING_A_GLOBAL_KEY, ["MIDDLEWARE", "MIDDLEWARE"]),
        (A_MODULE_REACHING_FOR_ONE, ["DEFAULT_PERMISSION_CLASSES"]),
    ],
    ids=["assigns-it", "subscripts-settings"],
)
def test_the_key_detector_finds_both_ways_in(source: str, expected: list[str]) -> None:
    """Assignment is the obvious one; reaching through `settings` is the quiet one.

    The second names no key on the left of anything and would pass any sweep looking
    for assignments -- which is why the detector matches a *mention*.

    Args:
        source: The offending module.
        expected: The keys the detector should report, repeats included.

    """
    assert forbidden_keys(ast.parse(source)) == expected


def test_the_key_detector_passes_a_module_naming_none_of_them() -> None:
    """The anti-vacuity half: a neighbouring settings key is not one of the four."""
    assert forbidden_keys(ast.parse(A_MODULE_NAMING_NONE_OF_THEM)) == []


@pytest.mark.parametrize("path", PRODUCT_MODULES, ids=lambda path: str(path.relative_to(SRC_ROOT)))
def test_no_domain_module_touches_a_global_authorization_key(path: Path) -> None:
    """AC 4: authentication, authorization and the middleware stack stay the platform's.

    `AD-8` gives the reason in one sentence -- permitting a new key "would otherwise
    hand an adopted app authorization over every API request". This product is such
    an app, and the fact that it is *this* product's own code rather than a
    third party's does not change what the key does.

    Args:
        path: The module under test.

    """
    relative = path.relative_to(SRC_ROOT).as_posix()

    assert forbidden_keys(parse(path)) == [], (
        f"{relative} names a global authorization key. AD-8 keeps all four with the platform; a surface that "
        f"needs a different rule declares a core.permissions class instead."
    )
