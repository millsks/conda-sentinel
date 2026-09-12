"""Tests for `tests/model_registry.py`, the primitive three registry audits share.

`tests/unit/django_apps/test_outcome_field_audit.py`,
`tests/unit/django_apps/test_evidence_inheritance_audit.py` and
`tests/unit/django_apps/test_evidence_constraint_audit.py` all take their scope
predicate and their definition of "evidence model" from that module, and each of
them argues at length that an audit which cannot be shown to still detect
anything is an audit that has quietly stopped auditing. The same argument applies
to the module they all depend on, and this file is it -- exactly as
`tests/unit/test_source_scan.py` is for `tests/source_scan.py`, which the helper
modules in this repository otherwise have and this one did not.

**What can go wrong here is not what goes wrong in the audits.** A
`first_party_models()` that had narrowed to nothing makes all three sweeps report
a clean repository, and every anti-vacuity guard they carry is about their *own*
subject rather than about this. An `is_evidence_model` that had narrowed to
nothing does the same thing one layer down: the fixture halves of the two
evidence audits pass their models to the predicates directly, so they would
notice -- but the *sweep* half in each would go silently empty, and since
`CPM-IDENTITY-S06` landed `inventory_snapshots` those sweeps have a real table to
be about.

**The mark and the field it names are checked against each other.**
`OBSERVED_AT_FIELD` is a string, and `AppendOnlyModel.observed_at` is a field; a
rename of one and not the other leaves a mark that matches nothing and an audit
that finds no evidence models forever. That pairing is the one thing here that
cannot be inferred from either module in isolation.

`tests/model_registry.py` imports a Django model at import time, which
`tests/source_scan.py` deliberately does not -- its subject is the model
registry, which does not exist until Django is set up. That trade is recorded in
that module's docstring; what makes it safe is `--ds=config.settings.test` in
`addopts`, and a run without it fails at import rather than reporting a clean
registry.

No database and no network: reading the registry, defining a model and reading
`_meta` touch neither.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

import pytest
from django.apps import apps
from django.db import models
from django.test.utils import isolate_apps

from conda_sentinel.core.models import AppendOnlyModel
from tests.model_registry import A_FACT
from tests.model_registry import A_THIRD_PARTY_APP_NAME
from tests.model_registry import EVIDENCE_APP_LABEL
from tests.model_registry import FIRST_PARTY_APP_NAMES
from tests.model_registry import FIXTURE_APP
from tests.model_registry import FIXTURE_LABEL
from tests.model_registry import NOT_EVIDENCE_ATTRIBUTE
from tests.model_registry import OBSERVED_AT_FIELD
from tests.model_registry import RUN_LEDGER_MODEL_LABELS
from tests.model_registry import declares_not_evidence
from tests.model_registry import evidence_marks
from tests.model_registry import evidence_models
from tests.model_registry import exempt_models
from tests.model_registry import first_party_app_names
from tests.model_registry import first_party_models
from tests.model_registry import installed_app_names
from tests.model_registry import is_evidence_model
from tests.source_scan import SRC_ROOT

#: The three marks, in the order `evidence_marks` argues them.
EVERY_MARK: Final[tuple[str, ...]] = ("base", "app_label", OBSERVED_AT_FIELD)

#: Every evidence model this repository declares, by `app_label.Model`.
#:
#: The same shape `RUN_LEDGER_MODEL_LABELS` has, and here for the same reason: the
#: set is frozen so that a new evidence table is an edit somebody made rather than
#: a widening nothing noticed. It is a hand-written table on purpose -- what a
#: predicate discovers is exactly what a predicate that had narrowed would stop
#: discovering.
#:
#: Three entries. `collectors.InventorySnapshot` is `inventory_snapshots`
#: (`CPM-AD-25`), landed by `CPM-IDENTITY-S06` as the first concrete evidence
#: model in the repository; `identity.IdentityOverride` is `identity_overrides`
#: (`CPM-AD-14`, `CPM-FR-32`), landed by `CPM-IDENTITY-S05` as the audit row of
#: the product's one governed human write; `collectors.SourceReleaseSnapshot` is
#: `source_release_snapshots` (`CPM-FR-7`), landed by `CPM-CURRENCY-S01` as the
#: first evidence table a *surface* collector writes. They live here rather than
#: in `tests/model_registry.py` because nothing else needs them: the two evidence
#: audits assert only that their sweep is non-empty, which is the anti-vacuity
#: claim each of them is about.
#:
#: The second entry is worth more to this table than the first. A single evidence
#: model can be conforming for reasons a detector never had to notice, and that
#: one arrived in a collector's own application -- the shape `CPM-AD-7` makes the
#: norm. The second sits in a *domain* application beside three models that are
#: not evidence at all, so the sweep has to separate them rather than accept a
#: whole application. The third adds the case neither of the others could make:
#: two evidence models in **one** application, which is what `CPM-AD-7`'s "each
#: collector writes its own evidence table" actually looks like once a second
#: collector exists -- so a predicate that had come to answer per application
#: rather than per model fails here.
EVIDENCE_MODEL_LABELS: Final[frozenset[str]] = frozenset(
    {
        "collectors.CondaPackageSnapshot",
        "collectors.FeedstockSnapshot",
        "collectors.IdentityResolutionSnapshot",
        "collectors.InventorySnapshot",
        "collectors.KevFinding",
        "collectors.LicenseFinding",
        "collectors.PyPIReleaseSnapshot",
        "collectors.PythonReadinessAssessment",
        "collectors.PythonVerificationResult",
        "collectors.SourceReleaseSnapshot",
        "collectors.VulnerabilityFinding",
        "identity.IdentityOverride",
    },
)


# ---------------------------------------------------------------------------
# The scope: which applications and models count as this repository's own.
# ---------------------------------------------------------------------------


def test_the_scope_reaches_this_repositorys_applications() -> None:
    """The named applications are in scope, and a third-party one is not.

    Asserted installed *before* asserted out of scope: a third-party package that
    had been removed would satisfy "not in scope" for the wrong reason, and the
    exclusion this case exists to prove would go untested.
    """
    in_scope = first_party_app_names()
    installed = installed_app_names()

    for name in FIRST_PARTY_APP_NAMES:
        assert name in in_scope, name
    assert A_THIRD_PARTY_APP_NAME in installed, A_THIRD_PARTY_APP_NAME
    assert A_THIRD_PARTY_APP_NAME not in in_scope
    assert in_scope < installed


def test_every_first_party_model_comes_from_an_application_in_scope() -> None:
    """The models and the applications are two views of one predicate.

    `first_party_app_names` guards the sweeps' scope assertions and
    `first_party_models` is what they actually iterate; a model arriving from an
    application the scope does not name would mean the two had come apart.
    """
    in_scope = first_party_app_names()
    found = first_party_models()

    assert found, "expected this repository's applications to declare models"
    for model in found:
        assert model._meta.app_config is not None  # noqa: SLF001 - `_meta` is Django's own public-by-convention API
        assert model._meta.app_config.name in in_scope, model._meta.label  # noqa: SLF001 - `_meta` is Django's own public-by-convention API


def test_the_scope_is_taken_from_the_source_tree_rather_than_from_a_list() -> None:
    """The promise the module makes: an application under `src/` is in scope on arrival.

    Stated as a property of the paths rather than as a list of names, because a
    list is what the module docstring argues against -- the first evidence app
    added by somebody who did not know the list existed is the one that escapes.
    """
    for name in first_party_app_names():
        assert Path(apps.get_app_config(name.rpartition(".")[2]).path).resolve().is_relative_to(SRC_ROOT), name


def test_the_fixture_label_names_an_installed_application() -> None:
    """`isolate_apps(FIXTURE_APP)` needs the application to exist, in four test modules.

    A fixture registered under a label Django knows nothing about would fail at
    class-definition time in every one of them, with an error about registries
    rather than about the constant they all share.
    """
    assert FIXTURE_APP in installed_app_names()
    assert apps.get_app_config(FIXTURE_LABEL).name == FIXTURE_APP
    assert A_FACT != ""


# ---------------------------------------------------------------------------
# The marks: what makes a model evidence, and the one declared way out.
# ---------------------------------------------------------------------------


def test_the_observed_at_mark_names_a_field_the_base_really_declares() -> None:
    """The pairing neither module can check on its own.

    `OBSERVED_AT_FIELD` is the mark that catches the shape `CPM-AD-7` makes the
    norm -- a collector's evidence model, in the collector's own app, that forgot
    the base -- and it is a string. Rename the field on `AppendOnlyModel` without
    changing it and the mark matches nothing, forever, with every audit green.
    """
    field = AppendOnlyModel._meta.get_field(OBSERVED_AT_FIELD)  # noqa: SLF001 - `_meta` is Django's own public-by-convention API

    assert isinstance(field, models.DateTimeField)


def test_each_mark_stands_alone() -> None:
    """Three independent marks, and each has to catch a model carrying only it.

    This is the whole argument in `tests/model_registry.py` for defining
    "evidence model" three ways: inheritance alone is circular, the app label
    alone is escapable by moving the table, and `observed_at` alone would miss a
    model that has neither field nor label but does inherit the base. A detector
    that had collapsed to any one of them passes the two evidence audits' own
    fixture cases and silently stops sweeping for the other two shapes.
    """
    with isolate_apps(FIXTURE_APP):

        class BaseOnly(AppendOnlyModel):
            class Meta:
                app_label = FIXTURE_LABEL

        class AppLabelOnly(models.Model):  # noqa: DJ008 - a fixture in an isolated registry; nothing renders it
            recorded_at = models.DateTimeField()

            class Meta:
                app_label = EVIDENCE_APP_LABEL

        class ObservedAtOnly(models.Model):  # noqa: DJ008 - a fixture in an isolated registry; nothing renders it
            observed_at = models.DateTimeField()

            class Meta:
                app_label = FIXTURE_LABEL

        class NoMark(models.Model):  # noqa: DJ008 - a fixture in an isolated registry; nothing renders it
            recorded_at = models.DateTimeField()

            class Meta:
                app_label = FIXTURE_LABEL

        assert evidence_marks(BaseOnly) == ["base", OBSERVED_AT_FIELD]
        assert evidence_marks(AppLabelOnly) == ["app_label"]
        assert evidence_marks(ObservedAtOnly) == [OBSERVED_AT_FIELD]
        assert evidence_marks(NoMark) == []
        assert [is_evidence_model(model) for model in (BaseOnly, AppLabelOnly, ObservedAtOnly, NoMark)] == [
            True,
            True,
            True,
            False,
        ]


def test_a_model_carrying_every_mark_reports_every_mark() -> None:
    """The marks are reported as a list so a failure can say *why* a model was audited.

    A boolean would make the audits' messages read "this is an evidence model",
    which sends their reader to work out which of three definitions caught it.
    """
    with isolate_apps(FIXTURE_APP):

        class Everything(AppendOnlyModel):
            class Meta:
                app_label = EVIDENCE_APP_LABEL

        assert evidence_marks(Everything) == list(EVERY_MARK)


@pytest.mark.parametrize(
    "value",
    [True, False, None, 1, "yes"],
    ids=["true", "false", "none", "one", "string"],
)
def test_only_a_literal_true_declares_a_model_not_evidence(value: object) -> None:
    """The escape is consequential, so it is not reachable by anything truthy.

    `not_evidence = 1` or `not_evidence = "run ledger"` is somebody reaching for
    the attribute without reading what it does, and an audit that honoured either
    would be switched off by a typo. `CPM-EVIDENCE-S03`'s ledger takes this door
    deliberately and is recorded in a table when it does.
    """
    with isolate_apps(FIXTURE_APP):

        class Ledger(models.Model):  # noqa: DJ008 - a fixture in an isolated registry; nothing renders it
            observed_at = models.DateTimeField()

            class Meta:
                app_label = EVIDENCE_APP_LABEL

        setattr(Ledger, NOT_EVIDENCE_ATTRIBUTE, value)

        assert declares_not_evidence(Ledger) is (value is True)
        assert (evidence_marks(Ledger) == []) is (value is True)


def test_a_model_declaring_nothing_is_not_exempt() -> None:
    """The absence of the attribute is not an exemption, which is the common case.

    Every model in this repository is in this state, so a `getattr` defaulting to
    anything truthy would exempt all of them and make all three audits vacuous
    without a single test failing.
    """
    with isolate_apps(FIXTURE_APP):

        class Plain(AppendOnlyModel):
            class Meta:
                app_label = EVIDENCE_APP_LABEL

        assert declares_not_evidence(Plain) is False


# ---------------------------------------------------------------------------
# The two derived sweeps, over the real registry.
# ---------------------------------------------------------------------------


def test_the_registry_holds_exactly_the_evidence_models_and_the_run_ledger_escapes() -> None:
    """The state today, asserted rather than assumed by three audits at once.

    Both halves are pinned so that "the sweep passed over exactly this" and "the
    sweep passed over exactly these two" are statements somebody checked, rather
    than inferences from three audits that each assumed them.

    `evidence_models()` is no longer empty: `CPM-IDENTITY-S06` landed
    `collectors.InventorySnapshot`, the `inventory_snapshots` table `CPM-AD-25`
    names; `CPM-IDENTITY-S05` landed `identity.IdentityOverride`, the
    `identity_overrides` table `CPM-AD-14` requires of the one governed human
    write; and `CPM-CURRENCY-S01` landed `collectors.SourceReleaseSnapshot`, the
    `source_release_snapshots` table `CPM-FR-7` observes into. The second is what
    makes the sweep discriminate rather than merely find: it sits in `identity`
    beside `Package`, `Feedstock` and `PackageMapping`, none of which is evidence,
    so an `is_evidence_model` that had widened to "declared by an application
    holding one" fails here. The third makes the converse claim -- two evidence
    models in one application, which is `CPM-AD-7`'s norm rather than an oddity.
    `exempt_models()` has not been empty since `CPM-EVIDENCE-S03`, and the two it
    holds are the run-ledger tables `CPM-AD-2` exempts by name. That no model is
    in both sets is the property that matters: `evidence_marks` returns nothing
    for a model declaring the escape, so they are disjoint by construction and a
    change that broke that would put the ledger under the append-only sweep.

    The day either set changes again this fails, and the fix is a deliberate edit
    here -- which is the point.
    """
    exempt = {
        model._meta.label  # noqa: SLF001 - `_meta` is Django's own public-by-convention API
        for model in exempt_models()
    }
    evidence = {
        model._meta.label  # noqa: SLF001 - `_meta` is Django's own public-by-convention API
        for model in evidence_models()
    }

    assert evidence == EVIDENCE_MODEL_LABELS
    assert exempt == RUN_LEDGER_MODEL_LABELS
    assert set(exempt_models()) & set(evidence_models()) == set()
    assert set(exempt_models()) <= set(first_party_models())
    # No longer trivially true: both derived sweeps must stay inside the
    # first-party scope, or an audit is reporting on models whose rules are not
    # this product's to dictate.
    assert set(evidence_models()) <= set(first_party_models())
