"""The fixtures the policy-run and rollup cases are measured against, in one place.

`CPM-EVIDENCE-S07` builds the orchestration and is forbidden to ship a concrete
policy pass: currency, feedstock, vulnerability, licence, readiness and priority
are their own epics, and a pass invented in `core` would be one no epic wants and
one the ownership audit would police forever. What the suite therefore needs is
*fixture* passes writing *fixture* derived tables, in the same shape
`tests/collectors.py` holds the collector fixtures.

Two suites need them. `tests/unit/django_apps/test_policy_registry.py` and
`tests/unit/django_apps/test_pass_ownership_audit.py` build passes to assert the
registration refusals and need no table at all;
`tests/integration/django_apps/test_policy_run.py` and
`tests/integration/django_apps/test_rollup.py` run them against real ones. A
second copy of either would be the failure `tests/source_scan.py` and
`tests/model_registry.py` were both extracted to prevent -- two fixtures that can
disagree look exactly like two passing tests.

**Two derived tables, because one proves nothing about the rule that matters.**
`EVIDENCE.07-INT-001` is "two passes writing two domains in one run, and both
results survive the compose". One fixture table can only show that a pass wrote
something; it cannot show that composing the rollup left another domain alone,
which is the property the single-writer rule is *for*.

**The derived tables key the package and the run by integer, not by
`ForeignKey`.** That is forced rather than chosen. The models are built inside
`django.test.utils.isolate_apps`, which patches `Options.default_apps` with a
registry holding none of this repository's models -- so a `ForeignKey(Package)`
declared there resolves against a registry `identity.Package` is not in, and its
reverse accessor is deferred forever. It is also the shape `tests/collectors.py`
already uses for `CollectedFact.package_id`, and the *real* passes are unaffected:
`PackageHealth` is a migrated model declaring real `ForeignKey`s to both, and it
is the table the rules in this story are about.

**The models are built inside `isolate_apps` and built once.** `isolate_apps`
patches `Options.default_apps` rather than the global registry, so the fixture
models are invisible to `tests/model_registry.py`'s sweeps and to
`tests/unit/django_apps/test_migration_completeness.py` -- which is the whole
reason they are built there rather than declared at module scope, where they
would be two models in `core` that no migration builds. The classes survive their
block (each holds a reference to the registry it was defined in) and are cached,
so the unit tier's types and the integration tier's real tables are the same
classes.

**No fixture pass contributes a *real* rollup column, and that is deliberate
rather than left over.** `core/rollup.py` now offers exactly one,
`currency_status`, which `CPM-CURRENCY-S06`'s `CurrencyPass` owns -- so a fixture
contributing it would be refused for colliding with a real owner, and every case
about the contribution mechanism would then be measuring that collision instead
of the mechanism. The fixtures therefore keep contributing nothing, and
`rollup_with_a_domain_column()` below supplies a column nobody owns where a case
needs one. What the real pass exercises end to end is
`tests/integration/django_apps/test_currency_policy.py`.

**The adopted passes are live in the registry from `django.setup()` onwards.**
`policies/apps.py`'s `ready()` registers `CurrencyPass`, so no module in this
suite meets an empty pass registry any more. `ADOPTED_PASS_NAMES` below is the
one declaration of what that set is, and `registry_without_adopted_passes()` is
how a module that measures the registry's *own* behaviour gets a controlled one
back without pretending the adoption did not happen.

**Five fixture passes, each differing in exactly one way.** One that works, one
that raises for a nominated package, one that reads the first one's rows, one
that returns a column it never declared, and one that declares a column the
rollup does not offer. Each is built by the same factory with one behaviour
changed, which is what makes an assertion about that behaviour rather than about
the fixture.

A helper module, not a collected one. `[tool.pytest.ini_options] python_files`
matches `test_*.py` and `tests.py`, so nothing here is collected, and it sits at
`tests/` rather than under `tests/unit/` for the reason `tests/source_scan.py`,
`tests/model_registry.py`, `tests/celery_tasks.py` and `tests/collectors.py` do:
a collected test module is not a helper library.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextlib import suppress
from functools import cache as memoized
from typing import TYPE_CHECKING
from typing import Final

import pytest
from django.db import models
from django.test.utils import isolate_apps

from conda_sentinel.core import rollup as rollup_module
from conda_sentinel.core.outcomes import OutcomeState
from conda_sentinel.core.policy import PolicyPass
from conda_sentinel.core.policy import PolicyPassError
from conda_sentinel.core.policy import pass_registrations
from conda_sentinel.core.policy import register_pass
from conda_sentinel.core.policy import unregister_pass
from conda_sentinel.policies.currency import POLICY_NAME as CURRENCY_POLICY_NAME
from conda_sentinel.policies.currency import CurrencyPass
from conda_sentinel.policies.feedstock import POLICY_NAME as FEEDSTOCK_POLICY_NAME
from conda_sentinel.policies.feedstock import FeedstockPresencePass
from conda_sentinel.policies.licence import POLICY_NAME as LICENCE_POLICY_NAME
from conda_sentinel.policies.licence import LicensePass
from conda_sentinel.policies.priority import POLICY_NAME as PRIORITY_POLICY_NAME
from conda_sentinel.policies.priority import PriorityPass
from conda_sentinel.policies.py314_readiness import POLICY_NAME as PY314_READINESS_POLICY_NAME
from conda_sentinel.policies.py314_readiness import Py314ReadinessPass
from conda_sentinel.policies.remediation import POLICY_NAME as REMEDIATION_POLICY_NAME
from conda_sentinel.policies.remediation import RemediationPass
from conda_sentinel.policies.vulnerability import POLICY_NAME as VULNERABILITY_POLICY_NAME
from conda_sentinel.policies.vulnerability import VulnerabilityPass
from conda_sentinel.policies.work_type import POLICY_NAME as WORK_TYPE_POLICY_NAME
from conda_sentinel.policies.work_type import WorkTypePass
from tests.model_registry import FIXTURE_APP
from tests.model_registry import FIXTURE_LABEL

if TYPE_CHECKING:
    from collections.abc import Collection
    from collections.abc import Iterator
    from collections.abc import Mapping
    from datetime import datetime

    from conda_sentinel.core.models import PolicyRun
    from conda_sentinel.identity.models import Package

#: The names the fixture passes declare, and therefore what the rollup's
#: per-domain version map is keyed by. Prefixed so they cannot be confused with a
#: real pass's name the day one exists.
FIRST_DOMAIN: Final[str] = "cpm-fixture-first-pass"
SECOND_DOMAIN: Final[str] = "cpm-fixture-second-pass"
OTHER_DOMAIN: Final[str] = "cpm-fixture-other-pass"

#: The tables the fixture derived models are given, rather than the
#: `core_firstfinding` Django would derive. Load-bearing for the same reason
#: `tests/collectors.py`'s `FIXTURE_TABLE` is: the integration fixture drops a
#: stale table of this name at session start -- `--reuse-db` means a killed run
#: leaves one behind -- and a derived name would one day land on a genuine
#: migrated table.
FIRST_TABLE: Final[str] = "cpm_fixture_first_finding"
SECOND_TABLE: Final[str] = "cpm_fixture_second_finding"

#: The verdict a working fixture pass records and contributes. The generic
#: determinate value, so no case asserting "this column is not clean" shares a
#: string with what the fixture writes.
A_VERDICT: Final[str] = OutcomeState.OK.value

#: A rollup column no rollup declares, for the refusal that is about exactly
#: that. Spelled as a column a real epic would plausibly add, so the refusal
#: message reads as the mistake it stands for.
#:
#: It was `currency_status` until `CPM-CURRENCY-S06` made that column real, which
#: is the shape of edit this constant is expected to take: as each policy epic
#: lands its column, the stand-in moves to one that has not landed yet. It must
#: never name a column `core/rollup.py` actually offers, or the refusal it is
#: about would stop being a refusal and the case would pass for the opposite
#: reason. `test_the_undeclared_column_is_one_the_rollup_really_does_not_offer`
#: in `tests/unit/django_apps/test_policy_registry.py` is what says so.
#:
#: **`CPM-SECURITY-S04` landed the vulnerability pass and this constant did not
#: move**, which is worth stating because it looks like an oversight. That pass
#: contributes *no* rollup column at all -- `CPM-AD-21` says no pass writes the
#: health rollup and the story's Never list forbids adding one for this domain --
#: so `vulnerability_status` is still a column `core/rollup.py` does not offer,
#: and still the right stand-in. `package_vulnerability.vulnerability_status` is
#: a column on that pass's own derived table and is a different thing entirely.
#:
#: **`CPM-SECURITY-S05` landed the licence pass and it did not move either**, for
#: the identical reason -- and that is also why `A_DOMAIN_STATUS` below still
#: names `licence_status`: `LicensePass.contributes` is empty too, so the rollup
#: offers neither column and neither has an owner.
AN_UNDECLARED_COLUMN: Final[str] = "vulnerability_status"

#: How wide the fixture verdict column is. Wide enough for any `OutcomeState`
#: value and for a per-status verdict a later epic composes.
_VERDICT_LENGTH: Final[int] = 32

#: What a pass that has no derived table of its own declares. Used by the
#: refusal case, and never registered.
NO_DERIVED_MODEL: Final = None

#: The passes this component adopts at boot, by declared name, in adoption order.
#:
#: `policies/apps.py`'s `ready()` is what performs the adoption and
#: `config/settings/base.py` is what installs the application; this is the
#: suite's one record of what that produces, so a pass that stopped registering
#: fails a case rather than quietly leaving an audit sweeping nothing again. A
#: hand-written roster on purpose, on the terms `tests/model_registry.py`'s
#: `RUN_LEDGER_MODEL_LABELS` is one: adopting a pass is a decision, and a
#: roster derived from the registry would only ever agree with itself.
ADOPTED_PASS_NAMES: Final[tuple[str, ...]] = (
    CURRENCY_POLICY_NAME,
    FEEDSTOCK_POLICY_NAME,
    VULNERABILITY_POLICY_NAME,
    LICENCE_POLICY_NAME,
    REMEDIATION_POLICY_NAME,
    PY314_READINESS_POLICY_NAME,
    WORK_TYPE_POLICY_NAME,
    PRIORITY_POLICY_NAME,
)

#: The adopted pass classes, in the same order, so a case can re-register them.
ADOPTED_PASSES: Final[tuple[type[PolicyPass], ...]] = (
    CurrencyPass,
    FeedstockPresencePass,
    VulnerabilityPass,
    LicensePass,
    RemediationPass,
    Py314ReadinessPass,
    WorkTypePass,
    PriorityPass,
)

#: The policy version the cases that execute a real policy run must declare.
#:
#: **`CPM-CURRENCY-S07` made the policy version load-bearing, and this constant is
#: what records that.** Until it landed, `CPM-AD-8`'s "the version is data an
#: operator supplies" meant any stable string did, and the integration modules
#: said so. `FeedstockPresencePass` now looks its inactivity threshold up *by that
#: version* from `policies/data/policy-parameters.toml` and refuses a version the
#: file does not record -- per package, so a run at an unrecorded version
#: finalizes `failed` having accomplished nothing.
#:
#: A hand-written literal rather than a value read out of the shipped file, on the
#: terms `ADOPTED_PASS_NAMES` is one: a constant derived from the file would only
#: ever agree with it, and what the suite needs to know is that *this* version is
#: recorded. `tests/integration/django_apps/test_feedstock_policy.py` reconciles
#: it against the file in both directions.
#:
#: **`CPM-SECURITY-S04` moved it from `2026.09` to `2026.09.1`.** That story added
#: `vulnerability_risk_order`, a parameter `2026.09` predates and therefore does
#: not record, and what this constant names is the version recording a *complete*
#: set for every adopted pass -- so the cases that assert a risk level have an
#: order to draw one from.
#:
#: **A run at `2026.09` is not a failure, and an earlier version of this comment
#: said it was.** It claimed such a run would finalize `partial` with every
#: package's vulnerability row missing, because `VulnerabilityPass` refused a
#: version recording no order. That refusal was the defect: `core/policy_run.py`
#: wraps **every** pass for one package in one `transaction.atomic()`
#: (`CPM-AD-23`'s unit is one package, not one pass), so it rolled back that
#: package's currency and feedstock rows too and -- the condition holding for
#: every package -- finalized the run `failed` having written nothing. The pass
#: now derives the status and the KEV membership normally at such a version and
#: leaves the risk level blank, so `2026.09` replays with every domain's rows.
#: `2026.09` stays in the shipped file, unedited, because a run recorded at it
#: must still replay (`CPM-FR-22`), and
#: `tests/integration/django_apps/test_vulnerability_policy.py` asserts both
#: halves of that by name.
#:
#: **`CPM-SECURITY-S05` added `2026.09.2` and did not move this constant**, which
#: is the ordinary case and is stated because the paragraph above records the
#: exception. `license_rules` is optional and an absent key means what an empty
#: list means, so `LicensePass` derives identical rows at all three shipped
#: versions -- there is nothing an older entry cannot express, and nothing obliged
#: the suite to move. `policies/data/README.md` records both directions.
A_RECORDED_POLICY_VERSION: Final[str] = "2026.09.1"

#: The newest version the shipped file records, pinned by hand (`CPM-OPERATE-S02`).
#:
#: `run_policy` without `--version` derives this from the file, so a case that
#: derived the expectation the same way would prove the derivation agrees with
#: itself. This constant is the independent oracle:
#: `tests/unit/django_apps/test_operator_commands.py` reads the file with
#: `tomllib` and takes the maximum under a numeric segment ordering, and the
#: integration case asserts the run the command wrote is at *this* version.
#: **Move it when a newer version is recorded** -- the unit case fails until you
#: do, which is the point: a lexicographic "newest" would put `2026.09.10` before
#: `2026.09.4`, and nothing but a pinned value catches that.
THE_NEWEST_RECORDED_POLICY_VERSION: Final[str] = "2026.09.4"


@memoized
def fixture_derived_models() -> tuple[type[models.Model], type[models.Model]]:
    """Return the two fixture derived models, building them once for the session.

    Two ordinary per-domain tables: each keyed by the package and the policy run
    the row was computed in, exactly as `CPM-AD-21` keys a real one, and each
    carrying one verdict column of its own.

    **`isolate_apps` is entered and left before the classes are used.** The
    patched registry is not held open -- each class keeps a reference to the
    registry it was defined in, and neither declares a relation, so nothing they
    do needs to look another model up. Leaving the block is also what keeps them
    out of the global registry that `tests/model_registry.py` sweeps.

    Returns:
        The first and second fixture derived models, in that order. Cached, so
        every caller in the session gets the same classes -- which is what makes
        the integration tier's tables the unit tier's models.

    """
    with isolate_apps(FIXTURE_APP):

        class FirstFinding(models.Model):  # noqa: DJ008 - a fixture table, never rendered anywhere
            #: The package this finding is about, by the integer primary key
            #: `CPM-AD-3` fixes. Not a `ForeignKey`, for the reason the module
            #: docstring gives.
            package_id = models.PositiveBigIntegerField()

            #: The policy run that computed it. Together with `package_id` this
            #: is the `(package, policy_run)` key `CPM-AD-21` requires.
            policy_run_id = models.PositiveBigIntegerField()

            #: What this domain concluded. A `CharField`, never a boolean
            #: (`CPM-AD-5`).
            verdict = models.CharField(max_length=_VERDICT_LENGTH)

            class Meta:
                app_label = FIXTURE_LABEL
                db_table = FIRST_TABLE

        class SecondFinding(models.Model):  # noqa: DJ008 - a fixture table, never rendered anywhere
            package_id = models.PositiveBigIntegerField()
            policy_run_id = models.PositiveBigIntegerField()
            verdict = models.CharField(max_length=_VERDICT_LENGTH)

            #: How many of the *first* pass's rows this pass could see for the
            #: same package and run when it ran. Zero would mean the passes did
            #: not execute in declared order, or that a later pass cannot read an
            #: earlier one's work -- both of which `CPM-AD-21` forbids and
            #: neither of which any other column would show.
            saw_earlier = models.PositiveIntegerField(default=0)

            class Meta:
                app_label = FIXTURE_LABEL
                db_table = SECOND_TABLE

    return FirstFinding, SecondFinding


def working_pass_class(
    *,
    name: str = FIRST_DOMAIN,
    derived_model: type[models.Model] | None = None,
    contributes: tuple[str, ...] = (),
    verdict: str = A_VERDICT,
) -> type[PolicyPass]:
    """Build a fixture pass that writes one derived row per package and contributes nothing.

    Args:
        name: The name the pass declares.
        derived_model: The table it owns. Defaults to the first fixture model.
        contributes: The rollup columns it claims. Empty by default, because the
            rollup offers none -- see the module docstring.
        verdict: The value it writes into its derived table and returns for every
            column it declared.

    Returns:
        A `PolicyPass` subclass.

    """
    model = fixture_derived_models()[0] if derived_model is None else derived_model

    class FixturePass(PolicyPass):
        """A pass that does exactly what a pass is meant to do."""

        def evaluate(
            self,
            package: Package,
            *,
            policy_run: PolicyRun,
            evidence_cutoff: datetime,
        ) -> Mapping[str, str]:
            """Write this pass's own derived row and return its contribution.

            Args:
                package: The package being evaluated.
                policy_run: The run the row is keyed to.
                evidence_cutoff: The instant a real pass would read evidence as
                    of. Unused: the fixture reads no evidence, because what these
                    cases are about is the orchestration around a pass rather
                    than any pass's own logic.

            Returns:
                The declared columns, each carrying `verdict`.

            """
            model.objects.create(package_id=package.pk, policy_run_id=policy_run.pk, verdict=verdict)
            return dict.fromkeys(contributes, verdict)

    FixturePass.name = name
    FixturePass.derived_model = model
    FixturePass.contributes = contributes
    return FixturePass


def failing_pass_class(*, name: str = SECOND_DOMAIN, failing: Collection[int]) -> type[PolicyPass]:
    """Build a fixture pass that raises for nominated packages and works for the rest.

    The matrix's one-package-fails row. `CPM-AD-23` commits the packages that
    worked and rolls back the one that did not, and a pass that failed for
    *every* package could not tell that apart from a run that failed outright.

    Args:
        name: The name the pass declares.
        failing: The package primary keys it refuses to evaluate.

    Returns:
        A `PolicyPass` subclass over the second fixture derived model.

    """
    model = fixture_derived_models()[1]
    refused = frozenset(failing)

    class FailingPass(PolicyPass):
        """A pass that breaks on some packages and not on others."""

        def evaluate(
            self,
            package: Package,
            *,
            policy_run: PolicyRun,
            evidence_cutoff: datetime,
        ) -> Mapping[str, str]:
            """Write a row, then refuse if this package is one of the nominated ones.

            The row is written *before* the refusal on purpose: it is what makes
            the rollback observable. A pass that raised before writing would
            leave nothing behind either way, and the case could not tell a
            transaction from an early return.

            Args:
                package: The package being evaluated.
                policy_run: The run the row is keyed to.
                evidence_cutoff: Unused, as in every fixture pass.

            Returns:
                An empty contribution, for a package this pass accepts.

            Raises:
                RuntimeError: For a nominated package. Deliberately not one of
                    this product's own error types: a pass is somebody else's
                    code and may raise anything at all, and the orchestration
                    must survive whatever it raises.

            """
            model.objects.create(package_id=package.pk, policy_run_id=policy_run.pk, verdict=A_VERDICT)
            if package.pk in refused:
                message = f"fixture pass {name} refuses package {package.pk}"
                raise RuntimeError(message)
            return {}

    FailingPass.name = name
    FailingPass.derived_model = model
    return FailingPass


def reading_pass_class(*, name: str = SECOND_DOMAIN) -> type[PolicyPass]:
    """Build a fixture pass that records how many of the first pass's rows it can see.

    `CPM-AD-21`'s ordered read, made observable. The count goes into the second
    derived table's own `saw_earlier` column rather than into a list this module
    holds, so the assertion is made against the database the run wrote -- a
    module-level list would pass just as well if the pass had run outside the
    run's transaction entirely.

    Args:
        name: The name the pass declares.

    Returns:
        A `PolicyPass` subclass over the second fixture derived model.

    """
    first, second = fixture_derived_models()

    class ReadingPass(PolicyPass):
        """A pass whose only work is looking at what ran before it."""

        def evaluate(
            self,
            package: Package,
            *,
            policy_run: PolicyRun,
            evidence_cutoff: datetime,
        ) -> Mapping[str, str]:
            """Count the earlier pass's rows for this package and this run.

            Args:
                package: The package being evaluated.
                policy_run: The run whose rows are counted. Counted *for this
                    run* rather than for the table as a whole, because a count
                    over every run would be satisfied by a row an earlier run
                    left behind.
                evidence_cutoff: Unused, as in every fixture pass.

            Returns:
                An empty contribution.

            """
            seen = first.objects.filter(package_id=package.pk, policy_run_id=policy_run.pk).count()
            second.objects.create(
                package_id=package.pk,
                policy_run_id=policy_run.pk,
                verdict=A_VERDICT,
                saw_earlier=seen,
            )
            return {}

    ReadingPass.name = name
    ReadingPass.derived_model = second
    return ReadingPass


def undeclared_contribution_pass_class(*, name: str = FIRST_DOMAIN) -> type[PolicyPass]:
    """Build a fixture pass that returns a column it never declared.

    The runtime half of the single-owner rule: registration checks what a pass
    *claims*, and this is what checks what it actually returned. A pass that
    declared nothing and wrote into another pass's column would otherwise walk
    past every static check there is.

    Args:
        name: The name the pass declares.

    Returns:
        A `PolicyPass` subclass over the first fixture derived model.

    """
    model = fixture_derived_models()[0]

    class UndeclaredPass(PolicyPass):
        """A pass whose declaration and whose output disagree."""

        def evaluate(
            self,
            package: Package,
            *,
            policy_run: PolicyRun,
            evidence_cutoff: datetime,
        ) -> Mapping[str, str]:
            """Return a column this pass never claimed.

            Args:
                package: The package being evaluated.
                policy_run: The run the row is keyed to.
                evidence_cutoff: Unused, as in every fixture pass.

            Returns:
                A contribution naming `AN_UNDECLARED_COLUMN`, which
                `contributes` does not.

            """
            model.objects.create(package_id=package.pk, policy_run_id=policy_run.pk, verdict=A_VERDICT)
            return {AN_UNDECLARED_COLUMN: A_VERDICT}

    UndeclaredPass.name = name
    UndeclaredPass.derived_model = model
    return UndeclaredPass


@contextmanager
def registered_pass(policy_pass: type[PolicyPass]) -> Iterator[type[PolicyPass]]:
    """Register one fixture pass for the body of a `with`, and withdraw it afterwards.

    Registration is process-global, so a case that left a pass behind would
    change what every later case in the session sees -- and the ownership audit,
    which sweeps exactly that registry, would then fail in a different module
    with no indication of where the pass came from. A context manager rather than
    a fixture for the reason `tests/collectors.py`'s `registered_collector` is
    one: several cases register two passes in a nominated order, which a fixture
    cannot express.

    Args:
        policy_pass: The class to register.

    Yields:
        The class, so the body can name it without a second reference.

    """
    register_pass(policy_pass)
    try:
        yield policy_pass
    finally:
        # `suppress` because a case may legitimately have withdrawn the pass
        # itself -- the withdrawal cases do -- and an exception raised here would
        # replace whatever the body was reporting, which is the hazard
        # `core/ledger.py`'s finalization and `tests/celery_tasks.py` both record.
        with suppress(PolicyPassError):
            unregister_pass(policy_pass.name)


@contextmanager
def substituted_rollup() -> Iterator[None]:
    """Point `core/rollup.py`'s model at the synthetic rollup for the body of a `with`.

    A context manager rather than the `monkeypatch` fixture, and the reason is
    ordering rather than taste. `registry_without_adopted_passes` restores the
    adopted passes on the way out, and restoring one means `register_pass`
    re-reading `contributable_columns()` -- so a substitution still in place at
    that moment refuses `CurrencyPass` for contributing a column the *synthetic*
    rollup does not declare, and every later case in the module then fails on an
    entry assertion naming nothing that went wrong. A `with` block ends where a
    reader can see it ends, before any fixture teardown runs. The module that
    does not restore anything uses it too, so the two cannot drift into two
    substitutions with different lifetimes.

    The substitution is of `core/rollup.py`'s model rather than of a name inside
    `core/policy.py`: the registry reads `rollup.contributable_columns()` at call
    time precisely so that patching the rollup reaches it, and a case that
    patched a name bound at import would be describing a path it never took.

    Yields:
        Nothing; the substitution is the fixture.

    """
    with pytest.MonkeyPatch.context() as patched:
        patched.setattr(rollup_module, "ROLLUP_MODEL", rollup_with_a_domain_column())
        yield


@contextmanager
def registry_without_adopted_passes() -> Iterator[None]:
    """Withdraw the adopted passes for the body of a `with`, and put them back after.

    `policies/apps.py` registers `CurrencyPass` during `django.setup()`, so the
    pass registry is never empty in this suite. That is the correct state of the
    world and `tests/unit/django_apps/test_pass_ownership_audit.py` sweeps it as
    it is -- but a module measuring the *registry's own* refusals needs a set it
    controls: "registering this pass makes `registered_passes()` return exactly
    it" is a statement about the registry that a live adoption would make
    unwritable.

    Withdraw-and-restore rather than a patched module global, because
    `register_pass` and `unregister_pass` are the only two functions that keep
    `_REGISTERED` and `_COLUMN_OWNERS` in step -- a case that emptied one by hand
    would leave `currency_status` owned by a pass that is no longer registered,
    and the next contribution to it would be refused for a reason nothing in the
    case explains.

    Both ends are asserted. The registry must hold exactly the adopted set on the
    way in, so a module that ran after one which leaked fails here rather than
    somewhere downstream; and the restoration is checked, so a case that
    re-registered a pass itself cannot leave the suite with two.

    Yields:
        Nothing; the withdrawal and the restoration are the fixture.

    """
    assert sorted(pass_registrations()) == sorted(ADOPTED_PASS_NAMES), (
        f"the pass registry does not hold exactly the adopted passes on entry: {sorted(pass_registrations())}"
    )
    for name in ADOPTED_PASS_NAMES:
        unregister_pass(name)
    try:
        yield
    finally:
        for policy_pass in ADOPTED_PASSES:
            register_pass(policy_pass)
        assert sorted(pass_registrations()) == sorted(ADOPTED_PASS_NAMES), (
            f"the adopted passes were not restored: {sorted(pass_registrations())}"
        )


#: The domain status column the synthetic rollup below declares, and what it
#: holds when nobody contributes it.
#:
#: Named as a real epic would name it, so a failure reads as the thing it stands
#: for. The default is `not_applicable` rather than the empty string for two
#: reasons: it is distinguishable from "nothing was written", and it is
#: *determinate enough to be a claim*, which is what makes an ungated default
#: visible to a case rather than indistinguishable from a gated one.
#:
#: **`CPM-SECURITY-S05` landed the licence pass and this name stayed**, which is
#: worth stating because it now looks like a collision. `LicensePass` contributes
#: nothing -- `CPM-AD-21` says no pass writes the health rollup and the story's
#: Never list forbids adding a column for this domain -- so `core/rollup.py`
#: still declares no `licence_status`, nothing owns it, and it is still a column
#: only this synthetic model has. `package_license.license_outcome` is a column
#: on that pass's own derived table and is a different thing entirely, down to
#: the spelling.
A_DOMAIN_STATUS: Final[str] = "licence_status"
THE_COLUMN_DEFAULT: Final[str] = OutcomeState.NOT_APPLICABLE.value


@memoized
def rollup_with_a_domain_column() -> type[models.Model]:
    """Return a synthetic rollup declaring one *unowned* contributable column.

    **The real rollup's one column already has an owner, which is why this is
    still needed.** `PackageHealth.currency_status` arrived with
    `CPM-CURRENCY-S06` and `CurrencyPass` owns it from `django.setup()` onwards,
    so a fixture pass contributing it would be refused for colliding with a real
    owner -- and every case about the *mechanism* would be measuring that
    collision instead. `licence_status` here is a column a later epic will add
    and nothing owns today, which is what lets the confidence gate applied to a
    column, the full-row replace's defaulting, and every refusal about what a
    pass may *return* be exercised against a subject that is nobody's.

    So the cases substitute this where `core/rollup.py` names the model. It is the
    same device `tests/unit/django_apps/test_derived_status_writability_audit.py`
    uses for the rule it was written before there was a table for: measure the
    detector against a declaration built in the suite, so an empty repository
    cannot make it pass vacuously.

    It carries the stamp columns by name as plain fields, because
    `contributable_columns()` subtracts `STAMP_COLUMNS` and the primary key from
    the model's real fields -- a synthetic model missing them would offer six
    columns instead of one, and every case would be measuring something else.
    The status column declares `choices`, because `CPM-AD-5` requires them and
    `permitted_values` reads them.

    **`isolate_apps` is entered and left before the class is used**, for the
    reason `tests/collectors.py` gives: a model declared in a case is otherwise
    registered globally for the rest of the session, where
    `tests/model_registry.py`'s sweeps and
    `tests/unit/django_apps/test_migration_completeness.py` would both meet it.

    Returns:
        The synthetic rollup model. Cached, so every caller in the session gets
        the same class.

    """
    with isolate_apps(FIXTURE_APP):

        class SyntheticRollup(models.Model):  # noqa: DJ008 - a fixture table, never rendered anywhere
            """The rollup as it will look once one policy epic has run."""

            package = models.PositiveBigIntegerField()
            policy_run = models.PositiveBigIntegerField()
            computed_at = models.DateTimeField()
            evidence_cutoff = models.DateTimeField()
            confidence = models.CharField(max_length=_VERDICT_LENGTH)
            policy_versions = models.JSONField(default=dict)
            licence_status = models.CharField(
                max_length=_VERDICT_LENGTH,
                choices=OutcomeState.choices,
                default=THE_COLUMN_DEFAULT,
                editable=False,
            )

            class Meta:
                app_label = FIXTURE_LABEL

    return SyntheticRollup
