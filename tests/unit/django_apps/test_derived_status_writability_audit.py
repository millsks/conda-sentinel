"""`CPM-FR-37`: no current-status field is directly writable from outside a policy run.

Two rules, written before the table they are about existed, and that was
deliberate.

`CPM-AD-11` puts current package health in a Django-managed rollup written by the
orchestrating policy run, and says in as many words that **only the rollup writer
writes it**. `CPM-EVIDENCE-S07` built that table and `CPM-CURRENCY-S06` gave it
its first domain status column. Writing a guard in the same story that creates
the thing it guards is how a constraint comes to be shaped around that thing
rather than around the rule -- it gets discovered as "whatever the writer happens
to do" and stops being able to reject anything. So the rules were written first,
and what still keeps them honest is that every detector here is *also* measured
against synthetic declarations parsed in this module: an audit that has gone
blind must fail *here*, not report a clean repository forever.

**Rule one: a current-status column is not editable.** `editable=False` is
Django's own spelling of "not directly writable" -- the field leaves every
`ModelForm`, leaves the admin, and leaves `full_clean()`'s validation of
user-supplied data. It is the half of the rule that holds against a human with a
browser and a permission, which no source scan can reach.

**Rule two: nothing under `src/` writes one except a recorded writer.**
`editable=False` does not stop `obj.status = x` or `objects.update(status=x)` --
it was never meant to. The source scan is the half that holds against code, and
it is the half `CPM-AD-11`'s "only the rollup writer writes it" is actually
about.

**How a derived-state model is recognised, and why not by a marker.**
`CPM-AD-11` requires the rollup to carry `computed_at`, so that is the mark: a
first-party model declaring `computed_at` holds state that was *derived at a
moment* rather than observed at one. A marker attribute would have to be
remembered by the author of the very model this rule exists to catch, which is
the argument `tests/unit/django_apps/test_outcome_field_audit.py` already makes
for recognising derived statuses by name rather than by declaration. Evidence
carries `observed_at` and the run ledger carries neither, so both fall outside
without needing an exemption.

**On exemptions.** They are counted decisions, never holes. Each entry licenses a
fixed number of a fixed form in a fixed file, and both directions are checked:
an unrecorded write fails, and a recorded one that has been removed fails too, so
the table cannot rot into a permission nobody re-examined.

This is a unit test: it reads repository files, parses them, and inspects the
model registry. It opens no network or database connection.
"""

from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path
from typing import Final

import pytest
from django.db import models
from django.test.utils import isolate_apps

from tests.model_registry import FIXTURE_APP
from tests.model_registry import FIXTURE_LABEL
from tests.model_registry import first_party_models
from tests.source_scan import SRC_ROOT
from tests.source_scan import parse
from tests.source_scan import project_files

#: The derived-status naming convention, kept identical to
#: `tests/unit/django_apps/test_outcome_field_audit.py`'s. Two audits reading the
#: same convention through two spellings is how one of them comes to police a
#: field the other does not see; the values are repeated rather than imported
#: because importing one collected test module from another ties their
#: collection together, which `tests/conftest.py` argues against at length.
DERIVED_STATUS_NAMES: Final[frozenset[str]] = frozenset({"outcome", "status"})
DERIVED_STATUS_SUFFIXES: Final[tuple[str, ...]] = ("_outcome", "_status")

#: The sibling audit the convention above is copied from, reconciled by
#: `test_the_two_audits_read_the_same_convention` below.
#:
#: Duplication with a reconciliation is not the same thing as duplication.
#: Importing it would tie two collected modules' collection together, which
#: `tests/conftest.py` argues against; copying it silently would produce exactly
#: the failure this module's docstring names -- one audit policing a field the
#: other cannot see. So the values are copied and then *checked*, by reading the
#: other module's source rather than importing it.
SIBLING_AUDIT: Final[str] = "test_outcome_field_audit.py"

#: `CPM-AD-11`'s own required column, used here as the mark of a model holding
#: derived state. See the module docstring for why this is a convention rather
#: than a declared attribute.
DERIVED_STATE_MARK: Final[str] = "computed_at"

#: The field evidence carries instead. Named so the exclusion is a stated
#: decision rather than a side effect of `computed_at` happening not to appear on
#: an append-only table.
OBSERVED_AT_FIELD: Final[str] = "observed_at"

#: The write forms the source scan recognises, as they are reported.
ASSIGNMENT_FORM: Final[str] = "assignment to .{name}"
KEYWORD_FORM: Final[str] = "{name}= keyword to {method}()"

#: The ORM methods that write a row, and the only calls whose keywords count.
#:
#: Reading *every* call's keywords was the first shape of this scan and it was
#: wrong in a way worth recording: `status=` is also how an HTTP response
#: declares its code (`Response(status=...)`, `JsonResponse(status=...)`) and how
#: a queryset *reads* one (`filter(status=RunState.FAILED)`). That version
#: flagged seven modules, none of which writes derived state, and the only way to
#: land it would have been seven exemptions -- an audit exempted into meaning
#: nothing, which is worse than no audit because it looks like one.
#:
#: A model constructor -- `PackageHealth(licence_status=...)` -- is deliberately
#: not reachable from here, because a constructor call is syntactically just a
#: call and no list of names distinguishes one from `Response`. That gap is
#: closed by the other half of the rule instead: an unsaved instance changes
#: nothing until it is written, and every path that writes it passes through a
#: method below.
#:
#: **Four shapes this scan does not see, recorded rather than left to be
#: discovered.** `save(update_fields=["status"])` names the column in a list
#: value, not in a keyword name. `setattr(row, "status", value)` is a call, not
#: an `ast.Attribute` target. `update(**{"status": value})` arrives with
#: `keyword.arg` set to `None`. And raw SQL reaches the column with no Python
#: syntax to match at all -- `tests/unit/django_apps/test_mutation_path_audit.py`
#: is the sweep that reads SQL, and it is a separate rule for that reason.
#:
#: None of the four is defended here, and pretending otherwise would be worse
#: than the gap: a reader who believes this scan is total is a reader who stops
#: looking. What closes them is `editable=False` for anything reachable from a
#: form, and review for the rest.
ORM_WRITE_METHODS: Final[frozenset[str]] = frozenset(
    {
        "abulk_create",
        "abulk_update",
        "acreate",
        "aget_or_create",
        "asave",
        "aupdate",
        "aupdate_or_create",
        "bulk_create",
        "bulk_update",
        "create",
        "get_or_create",
        "save",
        "update",
        "update_or_create",
    },
)

#: Recorded write exemptions, per file, per form, per count.
#:
#: `core/ledger.py` -- the run ledger's own recorder. `CollectionRun.status` and
#: `PolicyRun.status` are *not* derived state: they are the ledger's record of
#: how a run ended, held to `RunState` rather than to the outcome vocabulary,
#: which `tests/unit/django_apps/test_outcome_field_audit.py` records separately
#: in `RECORDED_RUN_LEDGER_STATUS` for exactly the same collision. The name is
#: what collides; the rule does not reach them. Renaming the column to dodge the
#: convention is the option `tests/model_registry.py` names as the worse one.
#:
#: One entry, and it is the recorder's `finally`: `run.status = ...` at the end
#: of a run, which is the whole reason `CPM-EVIDENCE-S03` put the ledger in a
#: database. Recorded rather than special-cased by model type, because this scan
#: reads syntax and cannot know which model an attribute belongs to -- and a
#: heuristic that guessed would be the door a real write later walks through.
#: **`core/rollup.py` is the rollup writer and still has no entry, which is a
#: recorded state rather than an oversight.** It now writes a real domain status
#: -- `currency_status`, added by `CPM-CURRENCY-S06` -- but it reaches every
#: column as a mapping, `update_or_create(defaults=...)`, built by name from
#: `contributable_columns()`. This scan does not see that, and that is stated
#: rather than left to be discovered: it is one of the four shapes
#: `ORM_WRITE_METHODS` already records as outside its reach.
#:
#: The mapping is not a dodge here, and the difference is worth naming. The
#: writer writes *every* contributable column by iterating the model's own
#: fields, precisely so a column added by a later epic is written without an edit
#: -- there is no list of column names to spell as keywords, and spelling them
#: would reintroduce the roster the writer exists not to have. Routing a status
#: column through a mapping *to stay out of this table* is the `**kwargs` dodge
#: that constant names, and `policies/currency.py` is where the honest form is
#: taken instead: explicit keywords, recorded below.
#:
#: An entry recording zero of a form would license nothing and assert nothing,
#: and one recording a form that is not there fails
#: `test_every_recorded_exemption_still_describes_the_file` -- so the honest
#: record is the absence, plus `THE_ROLLUP_WRITER` below, which keeps the file in
#: the scan's view and makes the day it acquires a keyword write a decision
#: somebody has to record here.
#
#: `identity/services.py` -- `CPM-IDENTITY-S02`'s resolution recorder, and the
#: second collision of the same kind. `PackageMapping.outcome` is *not* current
#: package health: it records which of `CPM-FR-6`'s five states a single
#: cross-ecosystem mapping is in, on a table `CPM-AD-11` says nothing about and
#: that carries no `computed_at`, so `derived_state_models()` correctly does not
#: find it. `CPM-AD-14` names its one writer -- "package identity is mutated by
#: resolution, or by the override path" -- and this module is that resolution.
#: The name is what collides; the rollup rule does not reach it.
#:
#: One entry, and it is the *re*-resolution branch: `get_or_create` writes the
#: first outcome through a `defaults` mapping this scan does not read, and the
#: assignment is what a later resolution reaching a different conclusion
#: rewrites. Recorded rather than spelled through the mapping as well, because
#: routing a real status column through `defaults` to stay out of this table is
#: the `**kwargs` dodge `ORM_WRITE_METHODS` already names -- the honest form is
#: the visible one plus an entry somebody had to write.
#
#: `policies/currency.py` -- `CPM-CURRENCY-S06`'s currency pass, and the third
#: collision of the same kind. `PackageCurrency` is a *per-domain derived table*
#: (`CPM-AD-21`), not current package health: it carries no `computed_at`, so
#: `derived_state_models()` correctly does not find it, and `CPM-AD-11`'s single
#: writer is about `core/rollup.py` and the `package_health` table alone. What
#: collides is the naming convention -- five columns on that table are named for
#: the statuses they hold, because renaming them to dodge the convention is the
#: option `tests/model_registry.py` names as the worse one.
#:
#: Five entries, all `create()` keywords, and they are deliberately *visible*:
#: the pass could have written the row through a `defaults` mapping and stayed
#: out of this table entirely, which is the `**kwargs` dodge `ORM_WRITE_METHODS`
#: already names. The honest form is the keyword plus an entry somebody had to
#: write. The count is five because the row carries four per-surface verdicts and
#: one overall; a sixth would fail here until it is recorded.
#:
#: What is *not* exempted, and must never be: this module contributes
#: `currency_status` to the rollup by **returning** it, never by writing it. The
#: return is not a write form this scan matches and is not meant to be --
#: `core/rollup.py` is what writes the column, after `CPM-AD-4`'s gate.
#
#: `policies/feedstock.py` -- `CPM-CURRENCY-S07`'s feedstock presence pass, and
#: the fourth collision of the same kind, on exactly the terms the currency pass's
#: entry states. `PackageFeedstockPresence` is a per-domain derived table
#: (`CPM-AD-21`) carrying no `computed_at`, so the registry sweep correctly does
#: not find it; what collides is the naming convention, because the column that
#: holds this pass's verdict is named for the verdict it holds.
#:
#: One entry, one `create()` keyword, and it too is deliberately *visible*: the
#: row could have been written through a `defaults` mapping and stayed out of this
#: table entirely, which is the `**kwargs` dodge `ORM_WRITE_METHODS` names. One
#: rather than the currency pass's five because this table holds one verdict per
#: package where that one holds five; a second would fail here until it is
#: recorded.
#
#: `policies/vulnerability.py` -- `CPM-SECURITY-S04`'s vulnerability pass, and the
#: fifth collision of the same kind, on exactly the terms the two passes above
#: state. `PackageVulnerability` is a per-domain derived table (`CPM-AD-21`)
#: carrying no `computed_at`, so the registry sweep correctly does not find it;
#: what collides is the naming convention.
#:
#: One entry, one `create()` keyword, and the count is one rather than two for a
#: reason worth recording: that row carries *two* verdict columns, and only
#: `vulnerability_status` is named for a status. `kev_membership` is deliberately
#: not -- it records a membership rather than a derived status, and a name ending
#: `_status` would put it under
#: `tests/unit/django_apps/test_outcome_field_audit.py`'s rule, which would then
#: demand the four `OutcomeState` sentinels of a column whose whole point is that
#: it offers exactly three values. That is a naming decision argued in
#: `policies/outcomes.py`, not a dodge of this table: the column it produces is
#: still `editable=False`, and the write that fills it is in the same visible
#: `create()` call as the one recorded here.
#
#: `policies/licence.py` -- `CPM-SECURITY-S05`'s licence pass, and the sixth
#: collision of the same kind, on exactly the terms the three passes above state.
#: `PackageLicense` is a per-domain derived table (`CPM-AD-21`) carrying no
#: `computed_at`, so the registry sweep correctly does not find it; what collides
#: is the naming convention, because the column holding this pass's verdict is
#: named for the verdict it holds.
#:
#: One entry, one `create()` keyword, and it is `license_outcome` rather than a
#: `_status` name because `CPM-FR-18` and `collectors/outcomes.py` both call this
#: an outcome -- the convention above recognises both suffixes, so the choice
#: bought no exemption and was made for readability alone. The row's other
#: written column, `matched_rule`, is deliberately outside the convention: it
#: holds the *identifier of the rule* that produced the verdict rather than a
#: verdict, the way `authority_order_source` holds a provenance. It is
#: `editable=False` regardless, and it is written in the same visible `create()`
#: call as the one recorded here.
RECORDED_EXEMPTIONS: Final[dict[str, dict[str, int]]] = {
    "django_apps/conda_sentinel/core/ledger.py": {
        ASSIGNMENT_FORM.format(name="status"): 1,
    },
    "django_apps/conda_sentinel/identity/services.py": {
        ASSIGNMENT_FORM.format(name="outcome"): 1,
    },
    "django_apps/conda_sentinel/policies/currency.py": {
        KEYWORD_FORM.format(name="source_status", method="create"): 1,
        KEYWORD_FORM.format(name="pypi_status", method="create"): 1,
        KEYWORD_FORM.format(name="feedstock_status", method="create"): 1,
        KEYWORD_FORM.format(name="conda_package_status", method="create"): 1,
        KEYWORD_FORM.format(name="overall_status", method="create"): 1,
    },
    "django_apps/conda_sentinel/policies/feedstock.py": {
        KEYWORD_FORM.format(name="presence_status", method="create"): 1,
    },
    "django_apps/conda_sentinel/policies/vulnerability.py": {
        KEYWORD_FORM.format(name="vulnerability_status", method="create"): 1,
    },
    "django_apps/conda_sentinel/policies/licence.py": {
        KEYWORD_FORM.format(name="license_outcome", method="create"): 1,
    },
    "django_apps/conda_sentinel/policies/remediation.py": {
        KEYWORD_FORM.format(name="readiness_status", method="create"): 1,
    },
}

#: The one module `CPM-AD-11` permits to write current package health, named so
#: the scan can be asserted to still reach it. See `RECORDED_EXEMPTIONS` above
#: for why it carries no entry today.
THE_ROLLUP_WRITER: Final[str] = "django_apps/conda_sentinel/core/rollup.py"

#: The rollup itself, by `app_label.ModelName`. `derived_state_models()` was
#: empty when this module was written and `CPM-EVIDENCE-S07` is the story named
#: in its docstring as making it non-vacuous, so the model is named here and the
#: sweep is asserted to have found it.
THE_ROLLUP_MODEL_LABEL: Final[str] = "core.PackageHealth"

#: The module the exemption table above is about, asserted to be reachable by the
#: scan so that an exclusion added later cannot quietly take it out of view.
A_MODULE_THAT_WRITES_A_RUN_STATUS: Final[str] = "django_apps/conda_sentinel/core/ledger.py"

# Synthetic declarations the detectors are measured against. Parsed here rather
# than placed under `src/`: a fixture module in the source tree would be found by
# this scan and by every other sweep in this repository, and would need an
# exemption of its own.
AN_EDITABLE_STATUS_WRITE = """
def publish(row, verdict):
    row.licence_status = verdict
"""

AN_ORM_STATUS_WRITE = """
def publish(queryset, verdict):
    queryset.update(status=verdict)
"""

A_CREATED_STATUS = """
def publish(model, package):
    return model.objects.create(package_id=package, licence_outcome="clean")
"""

A_WRITE_TO_SOMETHING_ELSE = """
def publish(row, when):
    row.computed_at = when
"""

# The two shapes that made the first version of this scan useless. Neither
# reaches a column, and both are spelled `status=`.
AN_HTTP_RESPONSE = """
from rest_framework.response import Response

def deny():
    return Response({"detail": "no"}, status=403)
"""

A_STATUS_READ = """
def failures(queryset):
    return queryset.filter(status="failed")
"""


def is_derived_status_name(name: str) -> bool:
    """Report whether a field or keyword name is a derived status by convention.

    Args:
        name: The field or keyword name.

    Returns:
        True when the name is exactly `status`/`outcome` or ends in
        `_status`/`_outcome`.

    """
    return name in DERIVED_STATUS_NAMES or name.endswith(DERIVED_STATUS_SUFFIXES)


def derived_state_models() -> list[type[models.Model]]:
    """Return every first-party model that holds derived state.

    Returns:
        The models declaring `computed_at`. Empty until `CPM-EVIDENCE-S07`
        defines the rollup, which is why the shape cases below are measured
        against synthetic declarations rather than against this list.

    """
    return [model for model in first_party_models() if holds_derived_state(model)]


def holds_derived_state(model: type[models.Model]) -> bool:
    """Report whether a model holds state that was derived rather than observed.

    Args:
        model: The model to inspect.

    Returns:
        True when it declares `computed_at`. See the module docstring for why
        `CPM-AD-11`'s own required column is the mark rather than an attribute
        somebody has to remember to set.

    """
    return any(field.name == DERIVED_STATE_MARK for field in model._meta.concrete_fields)  # noqa: SLF001 - `_meta` is Django's own public-by-convention API


def editable_status_fields(model: type[models.Model]) -> list[str]:
    """Return the derived-status fields on a model that are still editable.

    Args:
        model: The model to inspect.

    Returns:
        The offending field names, in declaration order.

    """
    return [
        field.name
        for field in model._meta.concrete_fields  # noqa: SLF001 - `_meta` is Django's own public-by-convention API
        if is_derived_status_name(field.name) and field.editable
    ]


def status_writes(tree: ast.Module) -> list[str]:
    """Return every write to a derived-status name in one parsed module.

    Two forms. An attribute assignment is the one `editable=False` cannot stop.
    A keyword to one of `ORM_WRITE_METHODS` is the one that reaches a column --
    and only those calls are read, for the reason that constant records.

    Args:
        tree: The parsed module.

    Returns:
        `line: form` strings, one per write.

    """
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign | ast.AugAssign | ast.AnnAssign):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            found.extend(
                f"{node.lineno}: {ASSIGNMENT_FORM.format(name=target.attr)}"
                for target in targets
                if isinstance(target, ast.Attribute) and is_derived_status_name(target.attr)
            )
        elif (
            isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in ORM_WRITE_METHODS
        ):
            found.extend(
                f"{node.lineno}: {KEYWORD_FORM.format(name=keyword.arg, method=node.func.attr)}"
                for keyword in node.keywords
                if keyword.arg is not None and is_derived_status_name(keyword.arg)
            )
    return found


SUBJECT_MODULES: Final[tuple[Path, ...]] = project_files(SRC_ROOT, skip_migrations=True)


def _literal(node: ast.expr) -> object:
    """Evaluate a declared constant, unwrapping the one call `literal_eval` refuses.

    `frozenset({...})` is a `Call`, so `ast.literal_eval` rejects it outright --
    and it is exactly how both audits declare their name set. Unwrapped here
    rather than worked around by comparing sorted lists, because the *type* is
    part of what the two modules must agree on.

    Args:
        node: The declared value's expression.

    Returns:
        The evaluated constant.

    """
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "frozenset":
        return frozenset(ast.literal_eval(node.args[0]))
    return ast.literal_eval(node)


def test_the_two_audits_read_the_same_convention() -> None:
    """The copy above is checked against its source, so the two cannot drift apart.

    `tests/unit/django_apps/test_outcome_field_audit.py` recognises a derived
    status by the same two names and the same two suffixes, and the two audits
    are only complementary while that stays true: one polices the *vocabulary* a
    status column offers, the other polices who may *write* it, and a field that
    fell out of one list would silently leave one of those unenforced.

    Read out of the sibling's source rather than imported, for the reason the
    constant's own comment gives -- and read as literals rather than by executing
    the module, so this stays a unit test that parses a file.
    """
    sibling = ast.parse((Path(__file__).parent / SIBLING_AUDIT).read_text(encoding="utf-8"))
    declared = {
        target.id: _literal(node.value)
        for node in ast.walk(sibling)
        if isinstance(node, ast.AnnAssign | ast.Assign) and node.value is not None
        for target in ([node.target] if isinstance(node, ast.AnnAssign) else node.targets)
        if isinstance(target, ast.Name) and target.id in {"DERIVED_STATUS_NAMES", "DERIVED_STATUS_SUFFIXES"}
    }

    assert declared.keys() == {"DERIVED_STATUS_NAMES", "DERIVED_STATUS_SUFFIXES"}, (
        f"the sibling audit no longer declares the convention under those names: {sorted(declared)}"
    )

    assert declared["DERIVED_STATUS_NAMES"] == DERIVED_STATUS_NAMES
    assert declared["DERIVED_STATUS_SUFFIXES"] == DERIVED_STATUS_SUFFIXES


def test_the_scan_reaches_the_modules_the_rule_is_about() -> None:
    """The sweep below means nothing if its glob resolves to nothing.

    The anti-vacuity guard for the scan itself: a narrowed `project_files`, a
    renamed source root or a stray exclusion would leave every case here green
    while nothing was being read at all.
    """
    relative = {path.relative_to(SRC_ROOT).as_posix() for path in SUBJECT_MODULES}

    assert len(SUBJECT_MODULES) > len(RECORDED_EXEMPTIONS), f"expected modules under {SRC_ROOT}"
    assert A_MODULE_THAT_WRITES_A_RUN_STATUS in relative, (
        f"the scan no longer reaches {A_MODULE_THAT_WRITES_A_RUN_STATUS}, which the exemption table is about"
    )
    assert THE_ROLLUP_WRITER in relative, (
        f"the scan no longer reaches {THE_ROLLUP_WRITER}, which is the one module CPM-AD-11 permits to write "
        f"current package health -- the module this whole rule is about"
    )


def test_the_rollup_is_recognised_as_derived_state() -> None:
    """The sweep over `derived_state_models()` is no longer vacuous, and this says so.

    This module was written before the table existed, deliberately, so the rule
    would be shaped by `CPM-AD-11` rather than by whatever the writer happened to
    do -- and its docstring names `CPM-EVIDENCE-S07` as the story that makes the
    registry sweep meaningful. That story has run. `PackageHealth` declares
    `computed_at`, so `holds_derived_state` finds it, and the editability rule
    below inspects a real model rather than an empty list.

    `CPM-CURRENCY-S06` closed the other half: the rollup now declares
    `currency_status`, so the rule below has a real field to be about rather than
    a model with nothing on it that the convention matches. This case asserts the
    model is in view; the case below asserts the field it carries is not editable.
    """
    found = {model._meta.label for model in derived_state_models()}  # noqa: SLF001 - `_meta` is Django's own public-by-convention API

    assert THE_ROLLUP_MODEL_LABEL in found, (
        f"{THE_ROLLUP_MODEL_LABEL} is not recognised as holding derived state, so the editability rule below "
        f"inspects nothing. CPM-AD-11 requires the rollup to carry {DERIVED_STATE_MARK}."
    )


@pytest.mark.parametrize("path", SUBJECT_MODULES, ids=lambda path: str(path.relative_to(SRC_ROOT)))
def test_no_unrecorded_module_writes_a_derived_status(path: Path) -> None:
    """`CPM-AD-11`: only the rollup writer writes current health.

    Parameterized per module so a violation names the file that introduced it
    rather than reporting the whole source tree as broken. A new writer is a
    decision to record in `RECORDED_EXEMPTIONS` with the reason it is not the
    rollup -- in the open, at the moment it is written, which is the point of the
    check being in the way.
    """
    relative = path.relative_to(SRC_ROOT).as_posix()
    exempted = RECORDED_EXEMPTIONS.get(relative, {})
    found = status_writes(parse(path))
    counted = Counter(write.split(": ", 1)[1] for write in found)
    over_quota = {form for form, count in counted.items() if count > exempted.get(form, 0)}
    writes = [write for write in found if write.split(": ", 1)[1] in over_quota]

    assert writes == [], f"{relative} writes a derived status outside a policy run: {writes}"


def test_every_recorded_exemption_still_describes_the_file() -> None:
    """An exemption that no longer applies is a licence nobody meant to leave open.

    Checked in the direction the exemption is granted: the file must still
    contain the recorded write exactly as many times as the table records.
    Delete the write and this fails until the entry goes with it; add a second
    and the case above fails from the other side.
    """
    stale: list[str] = []
    for relative, forms in RECORDED_EXEMPTIONS.items():
        counted = Counter(write.split(": ", 1)[1] for write in status_writes(parse(SRC_ROOT / relative)))
        stale.extend(
            f"{relative}: {form} recorded {count}, found {counted.get(form, 0)}"
            for form, count in forms.items()
            if counted.get(form, 0) != count
        )

    assert stale == [], f"the exemption table no longer describes the source: {stale}"


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (AN_EDITABLE_STATUS_WRITE, [ASSIGNMENT_FORM.format(name="licence_status")]),
        (AN_ORM_STATUS_WRITE, [KEYWORD_FORM.format(name="status", method="update")]),
        (A_CREATED_STATUS, [KEYWORD_FORM.format(name="licence_outcome", method="create")]),
        (A_WRITE_TO_SOMETHING_ELSE, []),
        (AN_HTTP_RESPONSE, []),
        (A_STATUS_READ, []),
    ],
    ids=["an-assignment", "an-orm-update", "a-create", "not-a-status", "an-http-status", "a-status-read"],
)
def test_the_write_detector_recognises_each_form(source: str, expected: list[str]) -> None:
    """The anti-vacuity guard for the sweep, and the reason it can be trusted while empty.

    Nothing under `src/` holds derived state yet, so the sweep above passes
    today whether or not the detector works. These six measure it directly, and
    the negatives carry as much weight as the positives: a detector that flagged
    everything would satisfy the first three, and the last three are the shapes
    that actually made an earlier version of this scan useless -- an HTTP
    response code and a queryset *read*, both spelled `status=`. A scan that
    cannot tell those from a column write is one that gets exempted until it
    means nothing.
    """
    assert [write.split(": ", 1)[1] for write in status_writes(ast.parse(source))] == expected


def test_no_derived_state_model_leaves_a_status_editable() -> None:
    """Rule one, against whatever the registry actually holds.

    No longer vacuous in either direction: `PackageHealth` is found by
    `derived_state_models()` and it carries `currency_status`, so this sweep both
    reaches a model and inspects a field the convention matches.
    `test_the_rollup_carries_a_status_field_for_this_rule_to_be_about` below is
    what keeps that second half honest, because an offender list that is empty
    because there was nothing to look at reads exactly like a clean repository.
    """
    offenders = {
        f"{model._meta.label}.{field}"  # noqa: SLF001 - `_meta` is Django's own public-by-convention API
        for model in derived_state_models()
        for field in editable_status_fields(model)
    }

    assert offenders == set(), (
        f"a current-status field is editable, so a form or the admin can write it directly: {sorted(offenders)}. "
        f"CPM-AD-11 says only the rollup writer writes current health; declare editable=False."
    )


def test_the_rollup_carries_a_status_field_for_this_rule_to_be_about() -> None:
    """The anti-vacuity guard for the case above, on the half the model sweep misses.

    `derived_state_models()` finding `PackageHealth` says the sweep reaches a
    model; it says nothing about whether that model has a field the convention
    matches. Until `CPM-CURRENCY-S06` it did not, so the editability rule was
    running over a real model and inspecting no fields -- which passes exactly as
    a conforming model does.

    Asserted through `is_derived_status_name` rather than by comparing the string
    to a literal: what makes `currency_status` this rule's business is the
    convention, and a convention that had stopped recognising it is the failure
    worth catching.
    """
    rollup = next(model for model in derived_state_models() if model._meta.label == THE_ROLLUP_MODEL_LABEL)  # noqa: SLF001 - `_meta` is Django's own public-by-convention API
    matched = [field.name for field in rollup._meta.concrete_fields if is_derived_status_name(field.name)]  # noqa: SLF001 - `_meta` is Django's own public-by-convention API

    assert matched != [], (
        f"{THE_ROLLUP_MODEL_LABEL} carries no field this rule's naming convention matches, so "
        f"test_no_derived_state_model_leaves_a_status_editable inspects nothing"
    )
    assert editable_status_fields(rollup) == [], matched


def test_the_rollups_absence_columns_are_not_statuses_and_are_not_editable() -> None:
    """`CPM-OPERATE-S11`'s two columns sit outside this rule's convention, and are locked down anyway.

    `inventory_absent_since` and `inventory_last_listed` are instants, not
    verdicts: absence is an observation the inventory collector made, and the
    columns carry neither `choices` nor a status-shaped name, so this audit does
    not reach them and `CPM-AD-4`'s gate does not apply. They are `editable=False`
    all the same, because only the after-run step may write them -- and this
    case is what says that was decided rather than forgotten.
    """
    rollup = next(model for model in derived_state_models() if model._meta.label == THE_ROLLUP_MODEL_LABEL)  # noqa: SLF001 - `_meta` is Django's own public-by-convention API
    columns = [
        rollup._meta.get_field(name)  # noqa: SLF001 - `_meta` is Django's own public-by-convention API
        for name in ("inventory_absent_since", "inventory_last_listed")
    ]

    assert all(not is_derived_status_name(column.name) for column in columns)
    assert all(not column.editable for column in columns)
    assert all(column.null and not column.choices for column in columns)


def test_the_editability_detector_would_notice_an_editable_status() -> None:
    """The anti-vacuity guard for the case above, which today inspects nothing.

    Two synthetic models, differing only in the declaration under test, so the
    detector is shown to separate them rather than merely to return an empty
    list. Built with `models.Model` subclasses declared here rather than a
    parsed fixture, because `editable` is a field *attribute* and only a real
    field carries one.

    `isolate_apps` for the reason `tests/collectors.py` gives: a model declared
    in a case is otherwise registered globally for the rest of the session, where
    `tests/model_registry.py`'s sweeps and this module's own would meet it. The
    block is left before the classes are used -- each keeps a reference to the
    registry it was defined in, and neither declares a relation, so nothing they
    do needs to look another model up.
    """
    with isolate_apps(FIXTURE_APP):

        class _Editable(models.Model):  # noqa: DJ008 - a detector fixture, never rendered anywhere
            """A rollup that forgot the declaration."""

            computed_at = models.DateTimeField()
            licence_status = models.CharField(max_length=32)

            class Meta:
                app_label = FIXTURE_LABEL

        class _Guarded(models.Model):  # noqa: DJ008 - a detector fixture, never rendered anywhere
            """The same rollup, declared correctly."""

            computed_at = models.DateTimeField()
            licence_status = models.CharField(max_length=32, editable=False)

            class Meta:
                app_label = FIXTURE_LABEL

    assert editable_status_fields(_Editable) == ["licence_status"]
    assert editable_status_fields(_Guarded) == []


def test_the_derived_state_mark_does_not_catch_evidence() -> None:
    """Evidence is observed, not derived, and must not be swept by this rule.

    `AppendOnlyModel` carries `observed_at`, and an audit that read *any*
    timestamp as the derived-state mark would demand `editable=False` on every
    evidence status in the product -- a rule nobody wrote, enforced by a
    coincidence of column naming.
    """
    assert OBSERVED_AT_FIELD != DERIVED_STATE_MARK

    with isolate_apps(FIXTURE_APP):

        class _Evidence(models.Model):  # noqa: DJ008 - a detector fixture, never rendered anywhere
            """An evidence-shaped table, which this rule does not reach."""

            observed_at = models.DateTimeField()
            licence_status = models.CharField(max_length=32)

            class Meta:
                app_label = FIXTURE_LABEL

        class _Derived(models.Model):  # noqa: DJ008 - a detector fixture, never rendered anywhere
            """A rollup-shaped table, which it does."""

            computed_at = models.DateTimeField()
            licence_status = models.CharField(max_length=32)

            class Meta:
                app_label = FIXTURE_LABEL

    # The predicate itself, on both models, rather than a comparison of two
    # module constants. Asserting `OBSERVED_AT_FIELD != DERIVED_STATE_MARK` alone
    # says the two strings differ and nothing about what the audit does with
    # them -- it would hold just as well if `holds_derived_state` matched every
    # timestamp, which is the mistake worth catching. The pair is what separates
    # "the mark is specific" from "the mark is a string".
    assert holds_derived_state(_Derived) is True
    assert holds_derived_state(_Evidence) is False
    assert editable_status_fields(_Evidence) == ["licence_status"], (
        "the evidence model does carry an editable status; the rule simply does not reach it, "
        "and a detector that found nothing here would be passing for the wrong reason"
    )
