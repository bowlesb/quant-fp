"""The polars-free numpy hot path for the carried-state reduction engine — step 1: ``price_volume`` only.

Today the live minute derives its value matrix through ``IncrementalEngine._matrix_at`` →
``_derived_row``: a per-minute polars pass (``.lazy().sort().with_columns(...).filter().collect()`` — the
2×/minute re-sort + the 40+ windowed exprs) over a per-symbol row slice. The arithmetic in that pass is a few
ops per cell; the cost is the polars framework around it (matrix_at / resolve_points / assemble ≈ 90% of the
step, measured). This module replaces that derive, **for ``price_volume`` only and behind a default-off flag**,
with a numpy derive of the value matrix straight off the incoming bar — zero per-minute polars.

It is the production form of the proven keystone spike (the throwaway measured 2.0ms / 0.16ms-compute-polars-free
and value byte-identical via #451), with the one thing the throwaway faked — the real carried OBV cumulative —
sourced from the engine's existing ``obv_running`` state instead of a ``0`` placeholder.

Grounded in Ben's ``scode/buffer/tracker.py`` / ``vector_store.pyx`` discipline: carried aggregates maintained on
add, reads O(1) off them, polars off the hot path. The carried state itself (the windowed running sums, the OBV
cumulative, the time origin) is the engine's EXISTING ``WindowedSumState`` + ``obv_running`` + ``ref_epoch`` — this
module reuses them, it does not rebuild them. The only new thing is the polars-free DERIVE of the new minute's
value row.

SCOPE (step 1): ``price_volume`` only. No EMA / extrema / state-machine kinds, no gather L2 — those are later
groups. The single seam is the value-matrix derive; the fold (``WindowedSumState.update``) and the assemble
(``emit_numpy``) are the already-proven shared paths and are NOT touched here.
"""

from __future__ import annotations

import os

# Default OFF. Mergeable without changing live behaviour: when unset/"0" the engine takes today's exact polars
# ``_derived_row`` path (byte-identical by construction), so this can merge and ride the warm-start deploy seam
# exactly like FP_POINT_RING — armed + relaunched separately under the live parity gate. Same idiom as
# declarative._USE_RUST_REDUCE so a test toggling the env var drives both states in lockstep.
USE_STATE_SPINE = bool(os.environ.get("FP_STATE_SPINE")) and os.environ.get("FP_STATE_SPINE") != "0"

# The one group wired onto the numpy derive in step 1. Kept explicit (not "all groups") so the flag is a
# single-feature demonstration, not a broad migration — the engine only takes the numpy path when its groups
# are exactly this set.
SPINE_GROUPS: frozenset[str] = frozenset({"price_volume"})


def spine_active(group_names: frozenset[str] | set[str]) -> bool:
    """True when the flag is on AND the engine's groups are exactly the step-1 scope (``price_volume`` alone).

    A conservative gate: the numpy derive is only entered for the single demonstrated feature. Any other group
    composition falls through to today's polars ``_derived_row`` — so arming the flag in an engine that also
    holds other groups is a no-op, not a silent wrong path. Widened deliberately, group by group, only as each
    is value-gated onto the carried path."""
    return USE_STATE_SPINE and frozenset(group_names) == SPINE_GROUPS
