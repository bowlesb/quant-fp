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

import numpy as np

# Default OFF. Mergeable without changing live behaviour: when unset/"0" the engine takes today's exact polars
# ``_derived_row`` path (byte-identical by construction), so this can merge and ride the warm-start deploy seam
# exactly like FP_POINT_RING — armed + relaunched separately under the live parity gate. Same idiom as
# declarative._USE_RUST_REDUCE so a test toggling the env var drives both states in lockstep.
USE_STATE_SPINE = bool(os.environ.get("FP_STATE_SPINE")) and os.environ.get("FP_STATE_SPINE") != "0"

# The groups wired onto the numpy derive, each as its OWN single-group set. The engine takes the numpy path only
# when its groups are EXACTLY one of these sets — so a group rides the spine standalone (its own gated engine),
# and any other composition falls through to today's polars derive. Widened deliberately, ONE group at a time,
# each value-gated (#451 byte-identical) before it is added here.
#   step 1: price_volume (#458/#459)
#   step 2: clean_momentum
SPINE_GROUP_SETS: tuple[frozenset[str], ...] = (
    frozenset({"price_volume"}),
    frozenset({"clean_momentum"}),
)

# Back-compat alias (step 1 referenced SPINE_GROUPS); kept so external readers of the step-1 name still resolve.
SPINE_GROUPS: frozenset[str] = frozenset({"price_volume"})


def spine_active(group_names: frozenset[str] | set[str]) -> bool:
    """True when the flag is on AND the engine's groups are EXACTLY one migrated single-group set.

    A conservative gate: the numpy derive is entered only for an engine holding exactly one already-migrated
    group (price_volume, or clean_momentum). Any other composition (a mixed engine, an un-migrated group) falls
    through to today's polars ``_derived_row`` — so arming the flag is never a silent wrong path. Widened one
    group at a time, each #451-value-gated before being added to ``SPINE_GROUP_SETS``."""
    return USE_STATE_SPINE and frozenset(group_names) in SPINE_GROUP_SETS


def price_volume_safe_cols(
    close: np.ndarray,
    high: np.ndarray,
    low: np.ndarray,
    volume: np.ndarray,
    anchor_volume: np.ndarray,
    prior_close: np.ndarray,
    present: np.ndarray,
    *,
    y_anchored: bool,
) -> dict[str, np.ndarray]:
    """Numpy derive of ``price_volume``'s SAFE value columns at the latest minute — the polars-free equivalent of
    the safe-col half of ``IncrementalEngine._derived_row`` for this one group.

    All inputs are ``(n_symbols,)`` arrays aligned to the fixed session index: the latest minute's ``close /
    high / low / volume`` and per-symbol-constant ``anchor_volume``, each symbol's ``prior_close`` (the bar
    immediately before the latest — ``NaN`` if the symbol has no prior bar, exactly as ``close.shift(1).over``
    leaves it), and ``present`` (did this symbol deliver a bar this minute). Returns ``{col_name: (n_symbols,)
    array}`` for the columns ``_matrix_at`` reads off ``self.safe_value_cols``. The OBV slope's paired columns are
    NOT produced here — they are rebuilt from the engine's carried ``obv_running`` by ``_stateful_matrix`` (the
    already-numpy stateful path), unchanged.

    Reproduces the declarative exprs cell-for-cell (``price_volume.reduced()`` + ``regressions()['pv']`` via
    ``_ols_derived``):
      * ``ret = close/prior_close − 1`` (NaN where no prior bar — null, as the batch ``shift`` leaves it)
      * ``rng = high − low``; ``mfm = (2·close − high − low)/rng`` where ``rng > 0`` else ``0``
      * reduced bases: ``vol``, presence ``vol__p``, ``cv = close·vol``, ``mfv = mfm·vol``,
        ``up = vol where ret>0 else 0``, ``dn = vol where ret<0 else 0``
      * pv-corr OLS pairs (x = ``ret``, y = ``volume − anchor_volume`` when ``y_anchored`` i.e. FP_RUST_REDUCE,
        else raw ``volume``): ``both = x present & y present``, then ``x/y`` zeroed off-pair and the products.

    An ABSENT symbol (``present`` False) has no bar in the batch, so it contributes nothing: its reduced bases are
    null/zero by the absent-as-zero rule the caller already applies (the matrix is pre-zeroed), and its pv pair is
    masked to ``b=0`` here via ``present`` so the running OLS count matches the batch (which has no row for it).
    """
    finite_prior = np.isfinite(prior_close) & (prior_close != 0.0) & present
    ret = np.where(finite_prior, close / np.where(finite_prior, prior_close, 1.0) - 1.0, np.nan)
    rng = high - low
    mfm = np.where(rng > 0.0, (2.0 * close - high - low) / np.where(rng > 0.0, rng, 1.0), 0.0)

    vol = np.where(present, volume, np.nan)  # absent symbol has a null base (no bar), matching the batch
    cv = close * vol
    mfv = mfm * vol
    up = np.where(ret > 0.0, vol, np.where(present, 0.0, np.nan))
    dn = np.where(ret < 0.0, vol, np.where(present, 0.0, np.nan))

    # pv-corr OLS paired columns (x = ret, y = volume centered on the per-symbol anchor under FP_RUST_REDUCE).
    # _ols_derived: both = x.is_not_null() & y.is_not_null(); paired = where(both, val, 0); products.
    pv_y = (volume - anchor_volume) if y_anchored else volume
    pv_y = np.where(present, pv_y, np.nan)  # absent symbol: no y row
    both = np.isfinite(ret) & np.isfinite(pv_y)
    x_paired = np.where(both, ret, 0.0)
    y_paired = np.where(both, pv_y, 0.0)

    return {
        "__b0_vol": vol,
        "__b0_vol__p": np.where(np.isfinite(vol), 1.0, 0.0),
        "__b0_cv": cv,
        "__b0_mfv": mfv,
        "__b0_up": up,
        "__b0_dn": dn,
        "__rd_0_pv_b": both.astype(np.float64),
        "__rd_0_pv_x": x_paired,
        "__rd_0_pv_y": y_paired,
        "__rd_0_pv_xy": x_paired * y_paired,
        "__rd_0_pv_xx": x_paired * x_paired,
        "__rd_0_pv_yy": y_paired * y_paired,
    }


def clean_momentum_safe_cols(
    close: np.ndarray,
    anchor_close: np.ndarray,
    present: np.ndarray,
    *,
    centered: bool,
) -> dict[str, np.ndarray]:
    """Numpy derive of ``clean_momentum``'s SAFE (non-regression) value columns at the latest minute — the
    polars-free equivalent of the reduced-col half of ``_derived_row`` for this group.

    ``clean_momentum`` reduces ``cm_close`` (mean, std) + ``cm_one`` (sum) and regresses ``cm_clean`` (time-OLS:
    x=time STATEFUL → rides the engine's carried ``ref_epoch``; y=close NON-stateful). The OLS pairs are built by
    the caller from the carried time x + the current-bar close y (analogous to the obv block but with a
    non-stateful y); this function produces ONLY the reduced cols:
      * ``cm_close`` base + presence ``__p`` + raw square ``__sq`` (for mean/std)
      * under ``centered`` (FP_RUST_REDUCE): the additive centered power sums ``__c = close − anchor_close`` and
        ``__csq = (close − anchor_close)²`` (the shift-invariant, conditioned variance path the std/r2/resid-std
        read — the close y-anchor that closes clean_momentum's 620× near-perfect-fit breach)
      * ``cm_one = 1.0`` (the count base; sum-only, so no presence/square)

    An ABSENT symbol (``present`` False) has no bar → null base (the matrix is pre-zeroed; presence=0), matching
    the batch (no row, no contribution)."""
    cm_close = np.where(present, close, np.nan)
    cols: dict[str, np.ndarray] = {
        "__b0_cm_close": cm_close,
        "__b0_cm_close__p": np.where(np.isfinite(cm_close), 1.0, 0.0),
        "__b0_cm_close__sq": cm_close * cm_close,
        "__b0_cm_one": np.where(present, 1.0, np.nan),
    }
    if centered:
        vc = (
            cm_close - anchor_close
        )  # null stays null (anchor is a finite per-symbol const; absent → cm_close NaN)
        cols["__b0_cm_close__c"] = vc
        cols["__b0_cm_close__csq"] = vc * vc
    return cols


def obv_increment(
    close: np.ndarray, prior_close: np.ndarray, volume: np.ndarray, present: np.ndarray
) -> np.ndarray:
    """The OBV cumulative's per-minute increment (``__inc_0_obv``): ``+vol`` on an up-bar, ``−vol`` on a down-bar,
    ``0`` flat — the ``signed`` expr from ``price_volume.stateful_regressors()``. Zero for an absent symbol (no
    bar this minute), matching the batch ``cum_sum`` over present rows. This is the ONLY thing the carried OBV
    state needs each minute; ``_stateful_matrix`` adds it to ``obv_running`` and forms the time-OLS pairs, exactly
    as it does today off the polars-derived ``__inc`` column — so the carried OBV path is byte-unchanged, only its
    increment is sourced from numpy instead of a polars expr."""
    finite_prior = np.isfinite(prior_close) & (prior_close != 0.0) & present
    ret = np.where(finite_prior, close / np.where(finite_prior, prior_close, 1.0) - 1.0, np.nan)
    signed = np.where(ret > 0.0, volume, np.where(ret < 0.0, -volume, 0.0))
    return np.where(present, signed, 0.0)
