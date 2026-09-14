"""`CPM-AD-9`'s boundary, swept over everything a request can reach.

`CPM-APP-S08` AC 1 names four kinds of work that must leave a request: an outbound
call, a collector, a policy pass, and an export beyond the row cap. **Three of the
four have no request path today**, and that is the interesting part -- the story's
deliverable for them is not code, it is this module. Nothing in `surface/` calls a
collector, and the way it stays that way is a gate rather than a habit.

**The subject is the import closure, not the view modules.** A view that imported
`core/transport.py` would be obvious in review. The one that arrives is a view
importing a helper that imports a service that imports the client -- three files
apart, each edit reasonable on its own. So the sweep starts at every registered view
and walks `conda_sentinel.*` imports transitively, which is what "reachable from a
request" actually means.

**The failure it prevents is a page that hangs, not a page that is wrong.**
`CPM-NFR-6` and the story's own words: "the page returns instead of hanging on a
rate-limited third party". An outbound call in a request is fine on the developer's
machine, fine in CI, and a 30-second page the first time an upstream service is slow.
Nothing goes red; the page just stops coming back.

**AC 2 is a different shape and gets a different sweep.** One constant used by every
export path is checkable by counting who reads it -- and the answer has to be small
enough to name, because three paths each reading the setting are three chances to
compare it slightly differently.

**On the `AD-` prefix.** A bare `AD-n` in this repository is an *inherited* platform
decision; a decision from this product's own architecture spine always carries the
`CPM-` prefix.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import TYPE_CHECKING
from typing import Final

import pytest
from django.urls import get_resolver

import conda_sentinel

if TYPE_CHECKING:
    from collections.abc import Iterator

#: The package every module under audit lives in.
IMPORT_ROOT: Final[str] = "conda_sentinel"

#: The root of the product's source tree, from the package rather than rebuilt.
SRC_ROOT: Final[Path] = Path(conda_sentinel.__file__ or "").parent

#: The modules a request may not reach, and what each one is.
#:
#: **Named modules rather than a pattern**, because the rule is about five specific
#: capabilities and a pattern would either miss one or catch a module that merely
#: sounds like it. Each entry carries what a reader who hit this gate needs to know:
#: not that it is forbidden, but which of `CPM-AD-9`'s four kinds of work it is.
FORBIDDEN_FROM_A_REQUEST: Final[dict[str, str]] = {
    f"{IMPORT_ROOT}.core.transport": (
        "the outbound HTTP client. A request that makes one returns when a third party does, and the failure is "
        "a page that stops coming back rather than a page that is wrong"
    ),
    f"{IMPORT_ROOT}.collectors.tasks": (
        "the collectors. Every one of them makes an outbound call, and `CPM-AD-9` puts them behind the boundary "
        "for that reason"
    ),
    f"{IMPORT_ROOT}.core.policy_run": (
        "the policy run, which evaluates every pass over the whole inventory -- `CPM-NFR-1` sizes that at ten "
        "thousand packages"
    ),
    f"{IMPORT_ROOT}.collectors.sweep": ("the full-inventory collector sweep, which enqueues one task per package"),
    f"{IMPORT_ROOT}.core.delivery": (
        "the outbound webhook POST the operator digest is delivered through (`CPM-OPERATE-S09`). A delivery is a "
        "task's, never a request's: the Digests page reads the stored row through `surface/digest.py` and reaches "
        "neither the composer nor this seam"
    ),
}

#: Import edges the walk does not follow, and why each is licensed.
#:
#: **An edge rather than a module**, and the distinction is the whole value of the
#: exemption. Exempting `core/transport.py` outright would retire the gate for it.
#: Exempting *this one edge* leaves every other path to it failing -- a view importing
#: the client, a helper importing `collectors/tasks.py` -- which is the failure this
#: module is actually written about.
#:
#: `core/registry.py` -> `core/collection.py` is licensed because the registry needs
#: the `Collector` class at runtime, for the `issubclass` refusal that is the whole
#: of what it does; `Collector` cannot move behind `TYPE_CHECKING`. The read
#: surfaces that reach the registry read *declarations* off the classes and never
#: construct one: `surface/coverage.py` reads names and cadences, and since
#: `CPM-OPERATE-S08` the request path also calls `selectable_packages()` on each
#: swept class (`core/registry.py`'s `swept_collectors` and `selects`, for the
#: "Collect now" service) -- a classmethod that builds a lazy queryset and touches
#: no transport, which is the property `core/collection.py`'s own docstring
#: declares for it. So the client is reachable by import and unreachable by call,
#: and closing the gap would mean splitting `Collector`'s declaration from its
#: transport, which is a change to `CPM-AD-9`'s own vocabulary rather than to
#: this story.
#:
#: Spelled exactly and spent exactly: `test_every_recorded_edge_is_still_real` fails
#: on an entry that has stopped existing, so this cannot go on licensing nothing.
RECORDED_EDGES: Final[dict[tuple[str, str], str]] = {
    (f"{IMPORT_ROOT}.core.registry", f"{IMPORT_ROOT}.core.collection"): (
        "the registry holds the Collector class for its issubclass refusal; the coverage screen reads names and "
        "cadences off the registry, the recollection service calls selectable_packages() on the swept classes "
        "(a query, no transport), and neither constructs a collector"
    ),
}

#: How deep an import chain has to be for the transitivity case to have proved
#: anything. Three modules is one edge the walk was not given.
A_TRANSITIVE_CHAIN: Final[int] = 3

#: `CPM-NFR-1`'s inventory size, which the export cap must sit below.
THE_INVENTORY_SIZE: Final[int] = 10_000

#: Which modules may read the export row cap.
#:
#: AC 2: "it comes from one settings constant used by every export path". Spelled out
#: and spent, on the terms `test_app_layering_audit.py` sets for its recorded
#: imports -- an entry that stops being real fails below rather than licensing
#: nothing.
#:
#: `surface/exports.py` owns the *comparison* and is the only module that decides
#: whether a report is too large. `surface/views.py` reads the number to put it in a
#: message and to bound the synchronous file, which is using the constant rather than
#: choosing one. A third reader is a third chance to compare it differently, which is
#: the whole failure AC 2 is written about.
CAP_SETTING: Final[str] = "CPM_SYNC_EXPORT_MAX_ROWS"
MAY_READ_THE_CAP: Final[frozenset[str]] = frozenset(
    {
        "surface/exports.py",
        "surface/views.py",
    },
)


def product_modules() -> Iterator[tuple[str, Path]]:
    """Yield every module in this product's package with its path.

    Yields:
        `(dotted name, path)`, migrations excluded -- a migration is not reachable
        from a request and its generated body would dominate any sweep.

    """
    for path in sorted(SRC_ROOT.rglob("*.py")):
        relative = path.relative_to(SRC_ROOT)
        if "migrations" in relative.parts:
            continue
        parts = list(relative.with_suffix("").parts)
        if parts[-1] == "__init__":
            parts.pop()
        yield ".".join([IMPORT_ROOT, *parts]), path


#: Every product module by dotted name, parsed once.
MODULE_PATHS: Final[dict[str, Path]] = dict(product_modules())


def imported_product_modules(path: Path) -> set[str]:
    """Return the product modules one module imports.

    Both spellings, and both matter: `import a.b` and `from a.b import c` reach the
    same module, and a sweep that saw only one would be satisfied by the other.
    Function-local imports count -- a deferred import is still an import, and every
    `noqa: PLC0415` in this codebase is one.

    Args:
        path: The module's source.

    Returns:
        The dotted names it imports from inside this product, resolved to modules
        that exist -- so `from conda_sentinel.core.models import PackageHealth`
        resolves to `conda_sentinel.core.models` rather than to a name under it.

    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names if alias.name.startswith(f"{IMPORT_ROOT}."))
        elif isinstance(node, ast.ImportFrom) and (node.module or "").startswith(IMPORT_ROOT):
            module = node.module or ""
            found.add(module)
            # `from conda_sentinel.core import queues` names the module in `names`.
            found.update(f"{module}.{alias.name}" for alias in node.names)
    return {name for name in found if name in MODULE_PATHS}


def reachable_from(start: set[str]) -> dict[str, list[str]]:
    """Return every product module reachable from these, with how it was reached.

    Args:
        start: The modules to walk from.

    Returns:
        Each reachable module mapped to the import chain that reaches it, so a
        failure names the path rather than only the destination -- which is the whole
        difference between a gate somebody can act on and one they have to
        reconstruct.

    """
    chains: dict[str, list[str]] = {name: [name] for name in start if name in MODULE_PATHS}
    pending = list(chains)
    while pending:
        name = pending.pop()
        for imported in sorted(imported_product_modules(MODULE_PATHS[name])):
            if (name, imported) in RECORDED_EDGES:
                continue
            if imported not in chains:
                chains[imported] = [*chains[name], imported]
                pending.append(imported)
    return chains


def view_modules() -> set[str]:
    """Return the module of every view this product registers.

    Returns:
        The dotted module names, walked off the URL resolver -- so a view added in a
        module nobody listed here is swept from the moment it is routed.

    """
    found: set[str] = set()
    pending = list(get_resolver().url_patterns)
    while pending:
        entry = pending.pop()
        nested = getattr(entry, "url_patterns", None)
        if nested is not None:
            pending.extend(nested)
            continue
        callback = getattr(entry, "callback", None)
        view = getattr(callback, "cls", None) or getattr(callback, "view_class", None) or callback
        module = getattr(view, "__module__", "")
        if module.startswith(f"{IMPORT_ROOT}."):
            found.add(module)
    return found


# ---------------------------------------------------------------------------
# AC 1: work that must leave the request cannot be reached from one.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("forbidden", sorted(FORBIDDEN_FROM_A_REQUEST))
def test_no_request_can_reach_work_that_must_leave_it(forbidden: str) -> None:
    """AC 1, over the import closure rather than over the view modules.

    A view importing the HTTP client would be obvious in review. The one that arrives
    is a view importing a helper importing a service importing the client, three
    files apart and each edit reasonable on its own -- so the walk is transitive.

    One case per forbidden module, so a failure names which capability crossed the
    boundary rather than reporting that something did.

    Args:
        forbidden: The module a request may not reach.

    """
    chains = reachable_from(view_modules())

    assert forbidden not in chains, (
        f"a request can reach {forbidden} -- {FORBIDDEN_FROM_A_REQUEST[forbidden]}. The chain is "
        f"{' -> '.join(chains.get(forbidden, []))}. CPM-AD-9 puts this work behind a task; core/jobs.py is how "
        f"a request hands it over and still has something to point at."
    )


def test_the_sweep_starts_from_real_views() -> None:
    """So the cases above cannot pass by walking from nothing.

    A resolver walk that found no product view would report no violations rather than
    no coverage, and would go on doing so after somebody moved the URLconf.
    """
    modules = view_modules()

    assert modules, "no product view was found on the resolver, so the boundary sweep is asserting nothing."
    assert any(module.endswith(".surface.views") for module in modules), sorted(modules)


def test_the_walk_is_transitive_and_not_only_direct() -> None:
    """The mechanism itself, because a one-level walk would pass every case above.

    Asserted as a property rather than against a named module: any module far enough
    away would do, and naming one would make this fail on a refactor that moved an
    import for good reasons. What is checked is that *something* was reached which no
    starting module imports, which is the only thing "transitive" means.
    """
    start = f"{IMPORT_ROOT}.surface.views"
    chains = reachable_from({start})
    direct = imported_product_modules(MODULE_PATHS[start])

    indirect = {name for name, chain in chains.items() if len(chain) >= A_TRANSITIVE_CHAIN and name not in direct}

    assert indirect, "the walk reached nothing it was not handed, so it followed no edge of its own."


def test_every_forbidden_module_still_exists() -> None:
    """An entry that has stopped being real fails rather than guarding nothing.

    The same rule `test_app_layering_audit.py` applies to its recorded imports: a
    list of names is only a gate while every name resolves.
    """
    missing = sorted(name for name in FORBIDDEN_FROM_A_REQUEST if name not in MODULE_PATHS)

    assert missing == [], f"these are gated and no longer exist: {missing}."


def test_every_recorded_edge_is_still_real() -> None:
    """A licensed edge that no longer exists is an exemption guarding nothing.

    The same rule `test_app_layering_audit.py` applies to its recorded imports: a
    list of names is only a gate while every name resolves, and a stale entry quietly
    widens the one below it.
    """
    stale = [
        f"{importer} -> {imported}"
        for importer, imported in RECORDED_EDGES
        if importer not in MODULE_PATHS or imported not in imported_product_modules(MODULE_PATHS[importer])
    ]

    assert stale == [], f"these edges are licensed and no longer exist: {stale}."


def test_a_recorded_edge_licenses_only_itself() -> None:
    """The exemption is an edge, not a module, and this is what says so.

    Without the licensed edge nothing forbidden is reachable; *with* the walk allowed
    to follow it, `core/transport.py` is -- which is the state the exemption
    describes. If somebody later exempted the module instead, this case is what would
    notice that the gate had stopped covering every other path to it.
    """
    unrestricted: dict[str, list[str]] = {name: [name] for name in view_modules() if name in MODULE_PATHS}
    pending = list(unrestricted)
    while pending:
        name = pending.pop()
        for imported in sorted(imported_product_modules(MODULE_PATHS[name])):
            if imported not in unrestricted:
                unrestricted[imported] = [*unrestricted[name], imported]
                pending.append(imported)

    assert f"{IMPORT_ROOT}.core.transport" in unrestricted
    assert f"{IMPORT_ROOT}.core.transport" not in reachable_from(view_modules())


# ---------------------------------------------------------------------------
# AC 2: one constant, and few enough readers to name.
# ---------------------------------------------------------------------------


def test_only_the_declared_modules_read_the_export_cap() -> None:
    """AC 2, by counting who asks rather than by trusting one place to.

    The failure is not visible: two paths comparing the same setting slightly
    differently produce a download that is simply short, and nothing says so.
    """
    # `as_posix()` rather than `str()`: `Path` renders with the host separator, so
    # this compared `surface\exports.py` against `surface/exports.py` and failed on
    # the Windows runner alone. The recorded set is written with forward slashes
    # because a path in prose is, and the comparison has to meet it there.
    readers = sorted(
        path.relative_to(SRC_ROOT).as_posix()
        for name, path in MODULE_PATHS.items()
        if CAP_SETTING in path.read_text(encoding="utf-8")
    )

    assert set(readers) == MAY_READ_THE_CAP, (
        f"these read {CAP_SETTING}: {readers}. AC 2 asks for one constant used by every export path, and each "
        f"extra reader is another chance to compare it differently -- which produces a short download and no "
        f"complaint."
    )


def test_the_cap_is_one_settings_constant_with_a_real_value() -> None:
    """The constant itself, so the sweep above is about something.

    Read through `django.conf.settings` rather than off the module, because that is
    how every export path reads it and an override in a deployment is the case the
    constant exists for.
    """
    from django.conf import settings  # noqa: PLC0415 - read beside the claim

    cap: int = getattr(settings, CAP_SETTING)

    assert isinstance(cap, int)
    assert cap > 0


def test_the_cap_is_below_the_inventory_it_bounds() -> None:
    """PROVISIONAL, and deliberately smaller than the estate it is measured against.

    `CPM-NFR-1` sizes the inventory at ten thousand packages. A cap above that would
    never bite, and the asynchronous path `CPM-AD-9` requires would ship untested
    until the day it mattered -- which is the day somebody's report first exceeded a
    number nobody had ever crossed.
    """
    from django.conf import settings  # noqa: PLC0415 - read beside the claim

    cap: int = getattr(settings, CAP_SETTING)

    assert cap < THE_INVENTORY_SIZE
