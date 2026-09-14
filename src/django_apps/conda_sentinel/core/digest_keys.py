"""The keys an operator digest's `figures` is composed under, spelled once (`CPM-OPERATE-S09`).

`collectors/digest.py` writes `figures` and `surface/digest.py` reads it back,
and the two may not import each other: the composer imports the delivery seam
a request must never reach. So the spellings live here, in `core`, where both
may import them -- keys only, no behaviour, no imports beyond typing, so this
module is reachable from a request and from a settings-time reader alike.

The shape they describe:

```
{
  "collectors": {<name>: {"dispatches": {<state>: n}, "collections": {<state>: n},
                          "rate_limited": n, "past_freshness_target": n, "never_observed": n}},
  "overall": {"prune_runs": {<state>: n}, "inventory": {"ingested": n, "absent": n},
              "packages": {<confidence>: n, "resolved": n, "unresolved": n},
              "policy_run": {"version": v, "finished_at": iso, "status": state} | null},
}
```

`dispatches` is present only for a collector swept per package; the two
freshness figures only for a collector with a target and a selection.
"""

from __future__ import annotations

from typing import Final

__all__ = [
    "ABSENT_KEY",
    "COLLECTIONS_KEY",
    "COLLECTORS_KEY",
    "DISPATCHES_KEY",
    "FINISHED_AT_KEY",
    "INGESTED_KEY",
    "INVENTORY_KEY",
    "NEVER_OBSERVED_KEY",
    "OVERALL_KEY",
    "PACKAGES_KEY",
    "PAST_TARGET_KEY",
    "POLICY_RUN_KEY",
    "PRUNE_RUNS_KEY",
    "RATE_LIMITED_KEY",
    "RESOLVED_KEY",
    "STATUS_KEY",
    "UNRESOLVED_KEY",
    "VERSION_KEY",
]

#: The two top-level halves.
COLLECTORS_KEY: Final[str] = "collectors"
OVERALL_KEY: Final[str] = "overall"

#: Per collector.
DISPATCHES_KEY: Final[str] = "dispatches"
COLLECTIONS_KEY: Final[str] = "collections"
RATE_LIMITED_KEY: Final[str] = "rate_limited"
PAST_TARGET_KEY: Final[str] = "past_freshness_target"
NEVER_OBSERVED_KEY: Final[str] = "never_observed"

#: Overall.
PRUNE_RUNS_KEY: Final[str] = "prune_runs"
INVENTORY_KEY: Final[str] = "inventory"
INGESTED_KEY: Final[str] = "ingested"
ABSENT_KEY: Final[str] = "absent"
PACKAGES_KEY: Final[str] = "packages"
RESOLVED_KEY: Final[str] = "resolved"
UNRESOLVED_KEY: Final[str] = "unresolved"
POLICY_RUN_KEY: Final[str] = "policy_run"
VERSION_KEY: Final[str] = "version"
FINISHED_AT_KEY: Final[str] = "finished_at"
STATUS_KEY: Final[str] = "status"
