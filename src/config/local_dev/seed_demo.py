"""`python -m config.local_dev.seed_demo`: a local inventory with evidence behind it.

The entry point, shaped exactly like `config/local_dev/seed.py`'s and for the same
reasons: Django is set up here rather than at import, the refusal propagates as a
traceback rather than a message and a non-zero exit, and the completion event is
logged under a name that identifies this module rather than under `__main__`.

What it seeds and why nothing in it is fabricated is `config/local_dev/demo_data.py`.
"""

from __future__ import annotations

import os

import django
import structlog

# Named rather than taken from `__name__`: run as `python -m`, this module's
# `__name__` is `__main__`, and a log aggregator would file the one event that says
# seeding finished under a name that identifies nothing.
logger: structlog.stdlib.BoundLogger = structlog.get_logger("config.local_dev.seed_demo")


def main() -> dict[str, object]:
    """Set up Django, seed the demo inventory, and record what was written.

    The completion event carries the seeder's whole summary: the policy version
    the run applied, the rollup row count, the inline `resolve_identity` pass's
    four counts, and what the shipped parameter file leaves unconfigured. The
    counts are what to read first. A healthy seed against the network says
    `not_on_conda_forge=2` -- the two `internal-*` names, which conda-forge has no
    entry for -- and `unreachable=0`; every package `unreachable` is the resolver
    telling you it could not reach conda-forge or PyPI, or was refused by one of
    them, not the seed failing, and a second seed once they answer resolves it.
    `verified_kept` counts packages a person has set `verified`, which a re-seed
    leaves alone.

    Returns:
        What was seeded, the resolution counts, and what the shipped parameter
        file leaves unconfigured.

    Raises:
        ImproperlyConfigured: The run is not local. Propagated rather than rendered
            as a message and a non-zero exit: the traceback names the refusal and the
            variable, and a `SystemExit` here would make the task indistinguishable
            from a task that failed for any other reason.

    """
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.local")
    django.setup()

    from config.local_dev.demo_data import seed_demo_inventory  # noqa: PLC0415 - after django.setup()

    seeded = seed_demo_inventory()
    logger.info("local_dev.demo_seeding_complete", **seeded)
    return seeded


if __name__ == "__main__":
    main()
