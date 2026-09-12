from typing import Final

from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _

#: The setting `config/settings/base.py` assigns the selected watchlist to.
#: Spelled once, here, so the refusal below names the same thing the read asks
#: for -- a literal in each would be two names that can drift apart.
WATCHLIST_PATH_SETTING: Final[str] = "INVENTORY_WATCHLIST_PATH"

# The two settings the published-conda-package collector reads its monitored
# surfaces from (CPM-CURRENCY-S04, PRD Open Question 4) are spelled *once* in the
# whole component, in collectors/conda_package.py beside the read that uses them,
# and are imported inside ready() rather than restated here: the boot refusal
# below and the run-time read must name the same two settings, and two literals
# would be two names that can drift apart.
#
# The distinction the refusal rests on has three parts rather than two. A
# settings module that declares *nothing* is a misconfiguration, refused at boot
# on the terms WATCHLIST_PATH_SETTING is. One that declares the wrong *shape* --
# a bare string, most plausibly, which Python reads as one channel per character
# -- is refused at boot too, because it can never become a usable declaration and
# would otherwise fail every run for ever with nothing said at start-up. One that
# declares an *empty* list boots: that is what ships, it is a component honestly
# saying no operator has chosen yet, and what it costs is a failed run naming the
# setting rather than a component nobody can start.


class CollectorsConfig(AppConfig):
    """The collectors application, third under the second import root.

    `name` is `conda_sentinel.collectors`, never
    `django_apps.conda_sentinel.collectors`:
    `src/django_apps/` is a path root declared by
    `[tool.hatch.build.targets.wheel.sources]` in `pyproject.toml`, not a
    package, and it carries no `__init__.py`. It never appears in an import
    statement.

    The derived label is `collectors`, the last segment of `name`. It is the home
    the architecture spine's capability map gives `CPM-EP-IDENTITY` alongside
    `identity`, and the one `CPM-EP-CURRENCY`, `CPM-EP-SECURITY` and
    `CPM-EP-PY314` will add their own collectors and evidence tables to.

    **The evidence table lives here rather than in `identity`.** `CPM-AD-7` gives
    each collector its own evidence table, and putting `inventory_snapshots` in
    `identity` would put an append-only log inside the application that owns the
    one mutable package row -- which is exactly the confusion `CPM-AD-25` exists
    to prevent.

    No `default_auto_field`. `config/settings/base.py` sets
    `DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"` project-wide, and a
    per-app restatement is a second declaration of the shape `CPM-AD-3` fixes.

    **It does declare `ready()`, and it is the first adopted application that
    does.** `core` and `identity` declare none, and their AppConfigs say why:
    `django_service.users` is the sole stage-two owner (AD-26), and what holds
    that is ordering rather than a ban on the hook --
    `tests/unit/startup/test_installed_apps_ordering.py` asserts that every
    adopted application is installed *after* the owner, so stage two has already
    run by the time anything of this application's does. The hook here adopts this
    application's collectors into `core`'s registry, which is where `CPM-AD-28`'s
    boot sweep and `CPM-AD-20`'s scheduling both look for them -- and
    `core/registry.py` requires exactly this: "a collector arrives here because
    somebody wrote `register(TheCollector)` in an `AppConfig.ready()`, where a
    reader can see it."
    """

    name = "conda_sentinel.collectors"
    verbose_name = _("Collectors")

    def ready(self) -> None:
        """Adopt this application's collectors and its inventory source, one by one (AD-8).

        Registration is a side effect of adoption rather than of import: nothing
        self-registers and nothing is discovered by entry point or module walk,
        so "which collectors does this component run" is answered by the lines
        below and by no other mechanism. The inventory source adapter is declared
        on exactly the same terms (`CPM-AD-29`): one call, in a place a reader
        can see it, and no entry point, module scan or import walk anywhere.

        **The watchlist path is read from settings rather than selected here.**
        `CPM-AD-29` selects the file by `config.locality.is_local()`, and `AD-4`
        forbids anything under `src/django_apps/` importing `config` --
        `config/settings/base.py` performs the read and assigns
        `INVENTORY_WATCHLIST_PATH`, in the shape `ROLE_CONTRACT` already
        establishes. What is here is the settings *access*, which is a read of a
        value the platform composed and not a second selection rule.

        **The roster is a loop over a tuple rather than a line per collector.**
        More are coming (`CPM-EP-CURRENCY`, `CPM-EP-SECURITY`, `CPM-EP-PY314`),
        and the guard below is the part that must not be written eight times: a
        copy of it that compared the wrong name, or that was left off a new
        adoption, would either abort boot on a second `django.setup()` or register
        nothing at all. Adoption stays explicit -- every class is named in the
        tuple, where a reader can see it, and nothing is discovered (inherited
        `AD-8`).

        **Adopting the same class twice is a no-op, and that is not a softening
        of `core/registry.py`'s duplicate-name refusal.** That refusal is about
        two *different* classes under one name -- they would share an allowance,
        share a run history, and be indistinguishable in every report -- and it
        still fires here, because the guard compares identity rather than merely
        checking the name. What it stops is a `ready()` that runs a second time
        aborting process startup over an adoption that had already succeeded:
        `AppConfig.ready` is Django's to call, a second `django.setup()` in one
        process calls it again, and a `CollectorRegistryError` out of a boot hook
        is a component that will not start for no reason anybody chose.

        **The adapter declaration is guarded the same way and for the same
        reason.** `declare_inventory_adapter` refuses a second declaration --
        deliberately, because a second one silently replacing the first is how a
        deployed component comes to ingest a development subset and record every
        package outside it as absent -- so a `ready()` that ran twice would abort
        boot. The guard is as narrow as the registration's beside it: it passes
        only for a `WatchlistAdapter` **already reading the very file settings
        selected**. An adapter of another kind, and a watchlist adapter bound to
        another path, both reach `declare_inventory_adapter` and are both
        refused, because "which file is this component's inventory" is exactly
        the question `CPM-AD-29` will not have answered by import order.

        **Neither security source is declared here, and the absence is the
        declaration.** There are two of them rather than three:
        `CPM-SECURITY-S03`'s licence collector needs no adapter at all, because it
        reads the channels an operator has already declared, so the seams below are
        the whole of what that epic leaves undeclared.
        `collectors/advisories.py` opens the one-slot seam for
        `CPM-FR-11`'s vulnerability collector and `collectors/kev.py` opens a
        second one for `CPM-FR-12`'s KEV collector, and this hook deliberately
        makes neither call: which advisory and KEV sources are licensed for use is
        PRD Open Question 1, it explicitly blocks `CPM-EP-SECURITY`, and a source
        chosen by default would produce security findings about an organisation's
        packages that the organisation never agreed to act on. Nothing is refused
        at boot over either -- unlike the
        watchlist path and the monitored channels below, an undeclared security
        source is the *shipped* state rather than a settings module that dropped
        an assignment, so a component that refused to start over it would refuse
        to start as designed. What it costs instead is a collector whose sweep
        selects nothing and whose task refuses by name, which
        `docs/conda-sentinel/operations.md` tells an operator to expect.

        **Two slots and not one**, because they are two sources: an advisory
        database and a KEV catalog are different products with different licences,
        and an operator may reasonably have one and not the other. Declaring one
        leaves the other's collector observing nothing and saying so, which is a
        state this component can be in honestly.

        **A third seam is left undeclared here, and it is the one with the most
        behind it.** `collectors/verification.py` opens the slot for
        `CPM-PY314-S02`'s Python 3.14 execution backend, and this hook makes no call
        into it either. What the two security seams substitute is *which source is
        read*; what this one substitutes is **what code runs on which machine** --
        verification means executing somebody else's build and somebody else's
        import, and nothing in this product's requirements or architecture decides
        how that is isolated. A backend chosen by default would run arbitrary build
        scripts on whatever host the worker happens to be, in the one product whose
        subject is what arbitrary code from the internet does to an organisation.
        Nothing is refused at boot over it, for the reason nothing is refused over
        the two above: an undeclared backend is the *shipped* state. What it costs
        is a task that refuses by name -- and, unlike the two security collectors,
        not one wasted dispatch either, because nothing sweeps this collector at
        all (`CPM-PY314-S02` AC 3).

        **The two refusals about what a collector *declares* are made here, and
        here is the only place they can be made.** `CPM-AD-28`'s freshness
        refusal and `CPM-AD-20`'s cadence reconciliation both sweep the registry,
        and `config/startup/stage_two.py` evaluates both as conditions 10 and 11
        -- but stage two runs from `django_service.users`' `ready()`, and
        `tests/unit/startup/test_installed_apps_ordering.py` requires every
        adopted application to be installed *after* that owner. So in a deployed
        process stage two sweeps a registry this hook has not populated yet, and
        both conditions pass over nothing. Calling the same two public rules
        immediately after the registrations above is what makes the refusals real
        in a deployed process; stage two keeps them as conditions because that is
        where the contract enumerates them and where the suite drives them against
        fixtures.

        **One rule, three call sites, no restatement.**
        `core/collection.py`'s `freshness_target_fault` and
        `collectors/sweep.py`'s `cadence_reconciliation_fault` are the rules;
        this hook and stage two each ask them and raise. They live in the domain
        application rather than in `config/startup/` because inherited `AD-4`
        forbids anything under `src/django_apps/` importing `config`, so a rule
        owned by the startup package could not be called from here at all.

        **Unconditional, where stage two's conditions are deployed-only.** A
        collector whose declarations contradict its schedule is a misconfiguration
        in any locality, and a developer meeting it at `manage.py` time is the
        point: it is a mistake in a file they are editing, not a property of a
        deployment.

        Raises:
            ImproperlyConfigured: When the settings module declares no
                `INVENTORY_WATCHLIST_PATH`, or neither of the two settings the
                published-conda-package collector reads. Refused rather than left
                to an `AttributeError`: a settings module that dropped an
                assignment is a misconfiguration like every other one here, and a
                bare attribute error out of a boot hook says nothing about which
                setting is missing or what declares it. Also when a registered
                collector declares no usable freshness target (`CPM-AD-28`), or
                when the registered collectors and `CELERY_BEAT_SCHEDULE`
                disagree about a cadence (`CPM-AD-20`).

        """
        # Imported here rather than at module scope: `AppConfig` classes are
        # imported during `django.setup()` *before* the app registry is
        # populated, and this module reaches models through the collector it
        # registers -- a module-scope import would raise `AppRegistryNotReady`.
        from django.conf import settings  # noqa: PLC0415 - see above
        from django.core.exceptions import ImproperlyConfigured  # noqa: PLC0415 - see above

        from conda_sentinel.collectors.conda_package import CHANNELS_SETTING  # noqa: PLC0415 - see above
        from conda_sentinel.collectors.conda_package import PLATFORMS_SETTING  # noqa: PLC0415 - see above
        from conda_sentinel.collectors.conda_package import CondaPackageCollector  # noqa: PLC0415 - see above
        from conda_sentinel.collectors.conda_package import declaration_fault  # noqa: PLC0415 - see above
        from conda_sentinel.collectors.feedstock import FeedstockCollector  # noqa: PLC0415 - see above
        from conda_sentinel.collectors.kev import KevCollector  # noqa: PLC0415 - see above
        from conda_sentinel.collectors.license import LicenseCollector  # noqa: PLC0415 - see above
        from conda_sentinel.collectors.py314_verification import Py314VerificationCollector  # noqa: PLC0415
        from conda_sentinel.collectors.pypi_release import PyPIReleaseCollector  # noqa: PLC0415 - see above
        from conda_sentinel.collectors.python_readiness import PythonReadinessCollector  # noqa: PLC0415 - see above
        from conda_sentinel.collectors.resolve_identity import IdentityResolutionCollector  # noqa: PLC0415 - see above
        from conda_sentinel.collectors.source_release import SourceReleaseCollector  # noqa: PLC0415 - see above
        from conda_sentinel.collectors.sweep import cadence_reconciliation_fault  # noqa: PLC0415 - see above
        from conda_sentinel.collectors.tasks import InventoryIngestionCollector  # noqa: PLC0415 - see above
        from conda_sentinel.collectors.tasks import declare_inventory_adapter  # noqa: PLC0415 - see above
        from conda_sentinel.collectors.tasks import declared_inventory_adapter  # noqa: PLC0415 - see above
        from conda_sentinel.collectors.vulnerability import VulnerabilityCollector  # noqa: PLC0415 - see above
        from conda_sentinel.collectors.watchlist import WatchlistAdapter  # noqa: PLC0415 - see above
        from conda_sentinel.core.collection import freshness_target_fault  # noqa: PLC0415 - see above
        from conda_sentinel.core.registry import register  # noqa: PLC0415 - see above
        from conda_sentinel.core.registry import registered_collectors  # noqa: PLC0415
        from conda_sentinel.core.registry import registrations  # noqa: PLC0415 - see above

        for collector in (
            InventoryIngestionCollector,
            SourceReleaseCollector,
            PyPIReleaseCollector,
            FeedstockCollector,
            CondaPackageCollector,
            VulnerabilityCollector,
            KevCollector,
            LicenseCollector,
            PythonReadinessCollector,
            Py314VerificationCollector,
            IdentityResolutionCollector,
        ):
            if registrations().get(collector.name) is not collector:
                register(collector)

        adopted = registered_collectors()
        for declaration_fault_message in (
            freshness_target_fault(adopted),
            cadence_reconciliation_fault(adopted, getattr(settings, "CELERY_BEAT_SCHEDULE", {})),
        ):
            if declaration_fault_message:
                raise ImproperlyConfigured(declaration_fault_message)

        for monitored_setting in (CHANNELS_SETTING, PLATFORMS_SETTING):
            # `is None` rather than a truth test, and the difference is the whole
            # posture: an *absent* declaration is a settings module that dropped
            # an assignment, and an *empty* one is the shipped state PRD Open
            # Question 4 leaves an operator to change. A truth test here would
            # refuse to boot a component that is behaving exactly as designed.
            declared = getattr(settings, monitored_setting, None)
            if declared is None:
                message = (
                    f"{monitored_setting} is not configured, so this component cannot tell which conda "
                    f"channels and platforms the published-package collector observes (CPM-FR-10). "
                    f"config/settings/base.py assigns it -- empty, because the choice is PRD Open Question "
                    f"4's and an operator's to make -- and an empty declaration is a failed collection "
                    f"naming the setting rather than a component that will not start."
                )
                raise ImproperlyConfigured(message)
            # The shape as well as the presence, asked through the collector's own
            # rule so boot and run time cannot come to disagree. A declaration of
            # the wrong shape can never become a usable one, so refusing it here
            # is the difference between an operator learning at start-up and a
            # component that boots clean and fails every collection for ever.
            unusable = declaration_fault(declared, setting=monitored_setting)
            if unusable:
                raise ImproperlyConfigured(unusable)

        selected = getattr(settings, WATCHLIST_PATH_SETTING, None)
        if selected is None:
            message = (
                f"{WATCHLIST_PATH_SETTING} is not configured, so this component has no inventory source "
                f"file to declare an adapter for. config/settings/base.py assigns it as "
                f"watchlist_path(local=is_local()) -- locality selects the file and fails closed toward "
                f"production (CPM-AD-29)."
            )
            raise ImproperlyConfigured(message)

        declared = declared_inventory_adapter()
        if not (isinstance(declared, WatchlistAdapter) and declared.path == selected):
            declare_inventory_adapter(WatchlistAdapter(path=selected))
