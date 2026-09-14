"""The scale spike's seeder, exercised by the gate at three packages by two days.

`CPM-OPERATE-S10`'s spike (`tests/spikes/spike_evidence_scale.py`) seeds ten
thousand packages by ninety-one days with raw SQL, one `INSERT ... SELECT` per
table, spelling every column by name. A renamed column, a new `NOT NULL` one or
a tightened `CheckConstraint` would otherwise be found after a four-minute
Docker run rather than by `pixi run ci`. So the seed functions run here, on
the gate's PostgreSQL, at a scale that takes milliseconds, and the counts are
asserted against the same rule the spike asserts them against. No timings, no
`EXPLAIN`: the spike measures, this reconciles.

**Both backends assert, and neither skips.** The seeder is PostgreSQL SQL --
`generate_series`, `make_interval`, `timestamptz` -- and on sqlite it must
refuse rather than fail half-way through. The expectation is derived from the
declared environment, as `tests/integration/test_postgres_schema.py` derives
its own: under a PostgreSQL `DATABASE_URL` the seed runs and the counts are
asserted; under sqlite the seeder's own refusal is asserted. AC #2 of the
PostgreSQL gate forbids branching on `connection.vendor`, and this branches on
the declared URL instead -- the environment says which leg applies, and the leg
then asserts unconditionally.
"""

from __future__ import annotations

import os
from typing import Final

import pytest
from django.db import connection

from conda_sentinel.core.registry import swept_collectors
from tests.spikes.spike_evidence_scale import Scale
from tests.spikes.spike_evidence_scale import count_rows
from tests.spikes.spike_evidence_scale import expected_counts
from tests.spikes.spike_evidence_scale import require_postgresql_17
from tests.spikes.spike_evidence_scale import seed_everything
from tests.spikes.spike_evidence_scale import seeded_tables
from tests.spikes.spike_evidence_scale import uncited_rows

pytestmark = pytest.mark.integration

#: Three packages by two days: one policy run, one day inside the window and
#: one outside it, every statement executed once. Nothing about the numbers
#: matters beyond being small and non-trivial.
SMOKE_SCALE: Final[Scale] = Scale(packages=3, days=2)

#: The URL schemes that select PostgreSQL in `config/settings/base.py`.
POSTGRES_URL_SCHEMES: Final[tuple[str, ...]] = ("postgres://", "postgresql://", "pgsql://", "postgis://")


def _declared_postgres() -> bool:
    """Report whether the declared environment selects PostgreSQL, as `base.py` reads it."""
    url = os.environ.get("DATABASE_URL", "")
    return url.startswith(POSTGRES_URL_SCHEMES) or (not url and bool(os.environ.get("POSTGRES_DB")))


@pytest.mark.django_db
def test_the_seeder_writes_every_table_at_the_counts_the_scale_rule_says() -> None:
    """On PostgreSQL the seed runs and every table holds `keys x present package-days`; on sqlite it refuses."""
    collectors = tuple(collector.name for collector in swept_collectors())
    if not _declared_postgres():
        with pytest.raises(AssertionError, match="found 'sqlite'"):
            seed_everything(SMOKE_SCALE, collectors)
        return

    require_postgresql_17()
    seed_everything(SMOKE_SCALE, collectors)

    counts = {table: count_rows(table) for table in seeded_tables()}
    assert counts == expected_counts(SMOKE_SCALE, len(collectors))
    assert uncited_rows() == {}
    assert connection.vendor == "postgresql"
