"""`EVIDENCE.02-UNIT-001`: an observation that is already written cannot be rewritten.

`CPM-AD-2` and `CPM-FR-36`: evidence inserts and never updates, because `R-06` is
the loss of the audit trail itself -- an overwritten observation cannot be
reconstructed from anything, so the guard has to be a refusal at the write rather
than a rule in review.

**Every refusal is exercised at runtime, not inferred from the source.** That
distinction is the reason several cases below look redundant against
`tests/unit/django_apps/test_mutation_path_audit.py`: the scan proves no *source
file* carries a bypass, and these prove that a bypass would be refused if one
were written. Two of them additionally pin facts about *Django* rather than about
this repository -- that `QuerySet.delete` is never copied onto a manager, and
that the async mutators are `sync_to_async` wrappers around the sync ones -- so
that a future release which changed either is a failing test rather than a
silently reopened path.

**Almost every case here runs without a database, and that is a property of the
design rather than a compromise.** `AppendOnlyModel.save()` refuses *before* it
reaches Django's insert, and `AppendOnlyQuerySet.update()` refuses before it
compiles a statement, so the refusals are observable with no table in existence.
Two rows of the story's matrix genuinely need one -- the second observation
actually landing as a second row, and the clock supplying its instant -- and they
are `tests/integration/django_apps/test_append_only_evidence.py`.

**The subject is built and discarded.** `CPM-EVIDENCE-S02` is forbidden to create
a concrete evidence model: `CPM-AD-7` gives each collector its own table and the
first arrives with `CPM-EP-CURRENCY`. So the base is proved against fixture
models registered in an isolated registry by `django.test.utils.isolate_apps`,
which patches `Options.default_apps` and discards everything defined inside the
block. They are genuine model classes with genuine fields -- a stand-in object
would prove the guard works on stand-in objects -- and `makemigrations --check`
cannot see them.

**Two cases patch `Model.save` rather than writing a row.** "A first save is
permitted" and "`create()` still inserts" are assertions about the guard *letting
something through*, and the only thing on the far side of the guard is Django's
own insert. Patching `django.db.models.Model.save` puts the boundary exactly
where this module's scope ends: what is asserted is that control reached
Django with the arguments it was given, not that PostgreSQL accepted a row.

No database, no network, no filesystem: defining a model and calling a method
that raises touches none of them.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from datetime import timezone
from typing import TYPE_CHECKING

import pytest
from django.db import models
from django.test.utils import isolate_apps

from conda_sentinel.core.models import AppendOnlyError
from conda_sentinel.core.models import AppendOnlyManager
from conda_sentinel.core.models import AppendOnlyModel
from conda_sentinel.core.models import AppendOnlyQuerySet
from tests.clocks import FIXED_INSTANT
from tests.model_registry import A_FACT
from tests.model_registry import FIXTURE_APP
from tests.model_registry import FIXTURE_LABEL

if TYPE_CHECKING:
    from collections.abc import Callable
    from collections.abc import Coroutine
    from collections.abc import Iterator
    from typing import Any

#: The primary key a loaded row is given. Any value would do; it is named so the
#: assertion that the refusal *reports* the pk reads against something specific.
A_WRITTEN_ROW_PK = 7

#: An offset that is neither UTC nor the machine's. The awareness check must
#: accept it: an aware instant carrying any offset is the same instant, and
#: Django converts on the way to the column.
ANOTHER_ZONE = timezone(timedelta(hours=-5))

#: The same instant as `FIXED_INSTANT`, with the offset thrown away. What a
#: writer produces by reaching for `datetime.now()` or by stripping a tzinfo on
#: the way into the model, and the value `FixedClock` refuses at construction.
A_NAIVE_INSTANT = FIXED_INSTANT.replace(tzinfo=None)

#: What Django hands `Model.save()` when nothing was overridden. Written out so
#: the two "the guard let it through" cases assert the arguments that reached
#: Django rather than merely that something did.
DEFAULT_SAVE_ARGUMENTS = {"force_insert": False, "force_update": False, "using": None, "update_fields": None}


@pytest.fixture
def observation() -> Iterator[type[AppendOnlyModel]]:
    """A concrete evidence model, built and discarded.

    Yields:
        A model inheriting `AppendOnlyModel` with one ordinary field of its own,
        registered only for the duration of the test.

    """
    with isolate_apps(FIXTURE_APP):

        class Observation(AppendOnlyModel):
            fact = models.CharField(max_length=64)

            class Meta:
                app_label = FIXTURE_LABEL

        yield Observation


def _loaded(model: type[AppendOnlyModel]) -> AppendOnlyModel:
    """Build an instance exactly as the ORM builds one it read from a row.

    `from_db` is what a queryset calls for every row it returns, so an instance
    built here carries `_state.adding = False` and a primary key without any
    query having run. Constructing one by hand and assigning `pk` would prove the
    guard against a shape the ORM never produces.

    Args:
        model: The evidence model to build an instance of.

    Returns:
        An instance indistinguishable from one a `filter()` returned.

    """
    row: dict[str, object] = {"id": A_WRITTEN_ROW_PK, "observed_at": FIXED_INSTANT, "fact": A_FACT}
    names = [field.attname for field in model._meta.concrete_fields]  # noqa: SLF001 - `_meta` is Django's own public-by-convention API
    return model.from_db(None, names, [row[name] for name in names])


# ---------------------------------------------------------------------------
# The declaration itself.
# ---------------------------------------------------------------------------


def test_the_base_is_abstract_and_so_declares_no_table() -> None:
    """A concrete base would be a table `CPM-EVIDENCE-S02` is forbidden to create.

    It would also show up in `makemigrations --check`, which the story's
    verification runs: a migration appearing is the signal that a concrete model
    was added by mistake, and it can only be that signal while the base itself
    produces none.
    """
    assert AppendOnlyModel._meta.abstract is True  # noqa: SLF001 - `_meta` is Django's own public-by-convention API


def test_observed_at_has_no_default_and_no_auto_now() -> None:
    """`CPM-AD-26`, asserted on the field rather than only on the audit that sweeps for it.

    `default=timezone.now` and `auto_now_add=True` are the two spellings a writer
    reaches for, and both are failures of `EVIDENCE.01-AUDIT-002` -- they read the
    process wall clock where the row is written, which is what makes an
    observation-window test unwritable. The audit fails on either the day it is
    added; this case says what the field *is*, so a reader of this model does not
    have to infer the rule from a scan in another directory.
    """
    field = AppendOnlyModel._meta.get_field("observed_at")  # noqa: SLF001 - `_meta` is Django's own public-by-convention API

    assert isinstance(field, models.DateTimeField)
    assert field.has_default() is False
    assert field.auto_now_add is False
    assert field.auto_now is False
    assert field.null is False


def test_both_of_djangos_managers_are_the_append_only_one(observation: type[AppendOnlyModel]) -> None:
    """Every model has two managers, and only one of them is the one people name.

    `_default_manager` is what `objects` resolves to. `_base_manager` is the one
    Django builds *itself* when `Meta.base_manager_name` is unset -- a plain
    `Manager` returning a plain `QuerySet` -- and it is a real, reachable
    attribute on every model: `Evidence._base_manager.filter(...).update(...)`
    would compile and run an `UPDATE` with every refusal in this module bypassed.
    That spelling is what somebody writes *after* `objects.update()` has refused,
    so it is asserted here and swept for by `EVIDENCE.02-AUDIT-001`.
    """
    assert isinstance(observation.objects, AppendOnlyManager)
    assert isinstance(observation.objects.get_queryset(), AppendOnlyQuerySet)
    assert isinstance(observation._default_manager, AppendOnlyManager)  # noqa: SLF001 - Django's own accessor
    assert isinstance(observation._base_manager, AppendOnlyManager)  # noqa: SLF001 - Django's own accessor
    assert isinstance(observation._base_manager.get_queryset(), AppendOnlyQuerySet)  # noqa: SLF001 - Django's own accessor


def test_the_manager_carries_its_routing_hints_into_the_queryset(
    observation: type[AppendOnlyModel],
) -> None:
    """A queryset built without the manager's hints asks the router a different question.

    `CPM-AD-16` adds a second database alias for analytics, and a router decides
    per query which one to use from the alias and the hints it is given. Dropping
    them here would be invisible today and would send writes to a different alias
    than the manager was asked for once a router exists.
    """
    hinted = observation.objects.db_manager(hints={"instance": _loaded(observation)})

    assert hinted.get_queryset()._hints == hinted._hints  # noqa: SLF001 - the hints are the thing under test


# ---------------------------------------------------------------------------
# Matrix rows 1-5 and 10: the instance write paths.
# ---------------------------------------------------------------------------


def test_a_first_save_reaches_djangos_insert(
    observation: type[AppendOnlyModel],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Matrix row 1. The guard permits an insert and passes its arguments through.

    Without this case every other one here could be satisfied by a `save()` that
    refuses unconditionally, which would be an evidence log that cannot be
    written to at all.
    """
    reached: list[dict[str, object]] = []
    monkeypatch.setattr(models.Model, "save", lambda self, **kwargs: reached.append(kwargs))
    instance = observation(observed_at=FIXED_INSTANT, fact=A_FACT)

    instance.save()

    assert reached == [DEFAULT_SAVE_ARGUMENTS]


def test_a_second_save_is_refused(
    observation: type[AppendOnlyModel],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`EVIDENCE.02-UNIT-001`, and the criterion the whole story exists for.

    Driven through a real first save -- with Django's insert patched out -- so
    that the pk is set the way an insert sets it, rather than assigned by the
    test. The refusal names the model and the row, because "cannot save" with no
    subject sends its reader through every collector looking for which write it
    was.
    """
    monkeypatch.setattr(models.Model, "save", lambda self, **kwargs: setattr(self, "pk", A_WRITTEN_ROW_PK))
    instance = observation(observed_at=FIXED_INSTANT, fact=A_FACT)
    instance.save()

    with pytest.raises(AppendOnlyError) as refusal:
        instance.save()

    assert refusal.value.model_label == f"{FIXTURE_LABEL}.Observation"
    assert refusal.value.pk == A_WRITTEN_ROW_PK
    assert "Observation" in str(refusal.value)
    assert str(A_WRITTEN_ROW_PK) in str(refusal.value)


def test_a_forced_update_is_refused(observation: type[AppendOnlyModel]) -> None:
    """Matrix row 3. `force_update=True` asks for exactly the operation this model has none of.

    Refused on an *unsaved* instance too, which is the case the pk check alone
    would miss: Django would answer it with "Cannot force an update in save()
    with no primary key", a message about an argument rather than about evidence
    being append-only, and a reader would go looking for the missing pk.
    """
    unsaved = observation(observed_at=FIXED_INSTANT, fact=A_FACT)

    with pytest.raises(AppendOnlyError, match="force_update"):
        unsaved.save(force_update=True)

    with pytest.raises(AppendOnlyError, match="force_update"):
        _loaded(observation).save(force_update=True)


def test_a_positional_save_is_refused_with_this_models_own_error(
    observation: type[AppendOnlyModel],
) -> None:
    """The rule for both write methods: accept what Django accepts, refuse by name.

    Django 5.2 still takes `save()`'s four arguments positionally under a
    deprecation, so a signature that simply excluded them would answer
    `obj.save(False, True)` with a bare `TypeError` naming an argument count --
    the unhelpful message the `force_update` refusal exists to replace. The
    positional call can only be asking for one of the four, and three of those
    are refused, so it is refused as a whole and told why.

    `delete()` keeps Django's positional signature for the same reason, and every
    call to it is refused anyway.
    """
    instance = observation(observed_at=FIXED_INSTANT, fact=A_FACT)

    with pytest.raises(AppendOnlyError, match="positional"):
        instance.save(False, True)  # noqa: FBT003 - the deprecated positional spelling is the subject


def test_a_row_loaded_from_the_database_cannot_be_saved(observation: type[AppendOnlyModel]) -> None:
    """Matrix row 4, and the accidental path rather than a constructed one.

    Nobody writes "fetch the row and save it" meaning to destroy history. It is
    what `refresh, mutate, save` looks like, what a serializer's `update()` does,
    and what an admin form does -- and it is indistinguishable from a first save
    except by the primary key the load left behind.
    """
    loaded = _loaded(observation)

    with pytest.raises(AppendOnlyError) as refusal:
        loaded.save()

    assert refusal.value.pk == A_WRITTEN_ROW_PK


def test_an_instance_cannot_be_deleted(observation: type[AppendOnlyModel]) -> None:
    """Matrix row 5. Removing an observation loses the same history overwriting it does.

    Retention is a decision taken against a declared window by an administrative
    process (the shape `AD-31` already establishes for expired state), never by a
    line of product code holding one row.
    """
    with pytest.raises(AppendOnlyError, match="delete"):
        _loaded(observation).delete()


def test_a_save_with_no_observed_at_is_refused(observation: type[AppendOnlyModel]) -> None:
    """Matrix row 10. The omission is loud rather than silently defaulted.

    This is the other side of `observed_at` having no default. A field default
    would make this write succeed and stamp it with the wall clock of whichever
    process happened to run it, which is both the `CPM-AD-26` violation and an
    observation whose recorded instant is not the instant it describes.
    """
    with pytest.raises(AppendOnlyError, match="observed_at"):
        observation(fact=A_FACT).save()


def test_a_save_with_a_naive_observed_at_is_refused(observation: type[AppendOnlyModel]) -> None:
    """A naive instant is not a smaller version of the same mistake.

    `FixedClock` goes to real trouble to refuse one and `SystemClock` cannot
    produce one, so a naive `observed_at` means the writer went around the clock.
    Django would accept it: `USE_TZ` is on, so it warns and stores the value as if
    it were UTC, and every freshness comparison, observation window and "what did
    we know at" query is then wrong by the writer's offset -- silently, and in a
    direction that reads as fresher or staler depending on the timezone the
    process happened to run in. This is the one place every evidence write passes
    through, so it is where the check belongs.
    """
    with pytest.raises(AppendOnlyError, match="naive"):
        observation(observed_at=A_NAIVE_INSTANT, fact=A_FACT).save()


def test_an_aware_observed_at_in_another_zone_is_accepted(
    observation: type[AppendOnlyModel],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other side of the same rule, so it cannot be satisfied by refusing everything.

    The check is awareness, not UTC. `FixedClock` normalises to UTC and Django
    converts on the way to the column, so an aware instant carrying any offset is
    the same instant and is written correctly; refusing it would be a rule about
    rendering rather than about time.
    """
    reached: list[dict[str, object]] = []
    monkeypatch.setattr(models.Model, "save", lambda self, **kwargs: reached.append(kwargs))
    elsewhere = FIXED_INSTANT.astimezone(ANOTHER_ZONE)

    observation(observed_at=elsewhere, fact=A_FACT).save()

    assert elsewhere.utcoffset() != timedelta(0)
    assert elsewhere == FIXED_INSTANT
    assert reached == [DEFAULT_SAVE_ARGUMENTS]


# ---------------------------------------------------------------------------
# Matrix rows 6 and 7: the manager and queryset paths.
# ---------------------------------------------------------------------------


def test_the_manager_offers_no_delete(observation: type[AppendOnlyModel]) -> None:
    """Matrix row 6, first spelling: `Model.objects.delete()` does not exist.

    It does not exist because Django marks `QuerySet.delete` as `queryset_only`
    and never copies it onto a manager -- which is a property of Django, not of
    this base. Pinned here so that a future release which started copying it
    would be a failing test rather than a silently reopened path.
    """
    with pytest.raises(AttributeError):
        _ = observation.objects.delete  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda model: model.objects.update(fact="rewritten"),
        lambda model: model.objects.bulk_update([], ["fact"]),
        lambda model: model.objects.all().update(fact="rewritten"),
        lambda model: model.objects.all().delete(),
        lambda model: model.objects.filter(fact=A_FACT).update(fact="rewritten"),
        lambda model: model.objects.filter(fact=A_FACT).delete(),
        lambda model: model.objects.all()._raw_delete("default"),  # noqa: SLF001 - the private spelling is the subject
        lambda model: model._base_manager.update(fact="rewritten"),  # noqa: SLF001 - Django's own accessor
        lambda model: model._base_manager.bulk_update([], ["fact"]),  # noqa: SLF001 - Django's own accessor
        lambda model: model._base_manager.all().update(fact="rewritten"),  # noqa: SLF001 - Django's own accessor
        lambda model: model._base_manager.filter(fact=A_FACT).delete(),  # noqa: SLF001 - Django's own accessor
        lambda model: model.objects.bulk_create([], ignore_conflicts=True),
        lambda model: model.objects.bulk_create([], update_conflicts=True),
    ],
    ids=[
        "manager-update",
        "manager-bulk-update",
        "queryset-update",
        "queryset-delete",
        "filtered-update",
        "filtered-delete",
        "raw-delete",
        "base-manager-update",
        "base-manager-bulk-update",
        "base-manager-queryset-update",
        "base-manager-filtered-delete",
        "bulk-create-ignore-conflicts",
        "bulk-create-update-conflicts",
    ],
)
def test_every_manager_and_queryset_mutation_is_refused(
    observation: type[AppendOnlyModel],
    mutate: Callable[[type[AppendOnlyModel]], object],
) -> None:
    """Matrix row 6. The bypasses `save()` cannot see, refused where they are issued.

    None of these constructs an instance, so none of them passes through
    `AppendOnlyModel.save()` at all: `update` and `bulk_update` compile an
    `UPDATE`, `QuerySet.delete()` runs Django's collector rather than the
    instance's `delete()`, and `_raw_delete` is the statement that collector
    issues. Both manager spellings are covered -- `objects` and the
    `_base_manager` Django reaches for internally -- because `Manager` is built
    from `QuerySet` and only some of these methods are copied across.

    The last two are the ones that hide inside an insert:
    `bulk_create(update_conflicts=True)` compiles to
    `INSERT ... ON CONFLICT DO UPDATE`, an overwrite wearing an insert's name, and
    `ignore_conflicts=True` compiles to `DO NOTHING`, which drops the observation
    rather than recording it. `CPM-AD-7` says re-observation is never a no-op any
    more than it is an update.

    Every refusal is raised before any statement is compiled, which is why this
    is a unit test with no table behind it.
    """
    with pytest.raises(AppendOnlyError):
        mutate(observation)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda model: model.objects.all().aupdate(fact="rewritten"),
        lambda model: model.objects.all().adelete(),
        lambda model: model.objects.all().abulk_update([], ["fact"]),
    ],
    ids=["aupdate", "adelete", "abulk-update"],
)
def test_every_async_mutation_is_refused(
    observation: type[AppendOnlyModel],
    mutate: Callable[[type[AppendOnlyModel]], Coroutine[Any, Any, object]],
) -> None:
    """The async spellings, driven rather than reasoned about.

    `AppendOnlyQuerySet` overrides only the synchronous three, and the async ones
    refuse because Django implements them as `sync_to_async` wrappers around
    exactly those. That is a fact about Django, not about this repository -- the
    same kind of dependency `test_the_manager_offers_no_delete` pins for
    `queryset_only` -- so it is executed here. A future native async
    implementation would reopen all three at once, and this is what would go red.

    `asyncio.run` rather than an async test: the suite carries no async plugin,
    and the refusal is raised in the worker thread and propagates out of the
    awaited coroutine unchanged.
    """
    with pytest.raises(AppendOnlyError):
        asyncio.run(mutate(observation))


def test_a_queryset_refusal_names_the_model_and_the_columns(
    observation: type[AppendOnlyModel],
) -> None:
    """A set-wide refusal has no one row to name, and says what it does have.

    `pk` is documented as `None` for these, which is a contract a caller can read
    rather than an accident of construction, and the message carries the columns
    the caller was trying to write -- sorted, so two runs of the same defect
    produce the same message. Without this the queryset refusals would be tested
    only for their exception type, and a message naming the wrong model or no
    columns at all would pass.
    """
    with pytest.raises(AppendOnlyError) as updating:
        observation.objects.update(fact="rewritten", observed_at=FIXED_INSTANT)
    with pytest.raises(AppendOnlyError) as bulk:
        observation.objects.bulk_update([], ["observed_at", "fact"])

    assert updating.value.pk is None
    assert updating.value.model_label == f"{FIXTURE_LABEL}.Observation"
    assert "update(fact, observed_at)" in str(updating.value)
    assert bulk.value.pk is None
    assert "bulk_update(fact, observed_at)" in str(bulk.value)


def test_create_still_inserts(
    observation: type[AppendOnlyModel],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Matrix row 7. An insert is not a mutation, and the manager keeps every insert path.

    `create()` is Django's own, and it reaches `save(force_insert=True)`, so it
    passes the guard rather than going around it. A base that made insertion
    awkward would push collectors toward raw SQL, which is the one write path no
    guard in this module can see.
    """
    saved: list[dict[str, object]] = []
    monkeypatch.setattr(models.Model, "save", lambda self, **kwargs: saved.append(kwargs))

    created = observation.objects.create(observed_at=FIXED_INSTANT, fact=A_FACT)

    assert created.observed_at == FIXED_INSTANT
    assert saved == [{**DEFAULT_SAVE_ARGUMENTS, "force_insert": True, "using": "default"}]


def test_a_plain_bulk_create_is_not_refused(observation: type[AppendOnlyModel]) -> None:
    """The other half of matrix row 7: only the conflict-handling forms are closed.

    Driven with an empty list, which Django returns from without touching the
    database, so the assertion is that the override *delegates* rather than that
    it refuses everything with `bulk_create` in the name. The multi-row insert
    itself is asserted against a real table in
    `tests/integration/django_apps/test_append_only_evidence.py`.
    """
    assert observation.objects.bulk_create([]) == []


# ---------------------------------------------------------------------------
# The one audited door (`CPM-OPERATE-S07`).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("door", [object(), None, "door", type("RetentionDoor", (), {})()])
def test_the_door_refuses_anything_but_the_retention_token(
    observation: type[AppendOnlyModel],
    door: object,
) -> None:
    """Matrix row `Door`: `retire(door=object())` is refused, and every case above is unchanged.

    The door is a method beside the five refusals rather than a softening of any
    of them. It opens only for the token `core/retention.py` constructs -- by
    identity, so a look-alike class, `None` and a string are all refused before
    any statement is compiled -- which is why this is a unit test with no table
    behind it, like every refusal above.

    Args:
        observation: The evidence model.
        door: What a caller might hand in.

    """
    with pytest.raises(AppendOnlyError, match="retire") as refused:
        observation.objects.all().retire(door=door)

    assert refused.value.model_label == observation._meta.label  # noqa: SLF001 - `_meta` is Django's own public-by-convention API
    assert refused.value.pk is None
