"""Multi-scale Directional-Change (DC) intrinsic-time decomposition (family: TREND_QUALITY, Layer A).

Ben's "decompose recent history into a series of major up/down chunks, ignore blips but respect large moves,
and characterize each chunk in detail" — formalized as the Olsen DIRECTIONAL-CHANGE intrinsic-time operator
swept over a LADDER of volatility-scaled thresholds in ONE pass.

THE DC SPINE (causal / no-repaint = the no-look-ahead property). For a threshold delta, a directional-change
event confirms when the close reverses by >= delta from the running leg extreme; after a DC the price continues
in the new direction (the OVERSHOOT) until the next reversal. A pivot is confirmed ONLY once the delta-reversal
has ALREADY occurred by the current bar — never with a future bar. So at minute T the decomposition reads only
bars <= T and the CURRENT (most recent) leg is always PROVISIONAL (its extreme can still extend; its features
are partial-by-construction, never reading a future end). That makes live == backfill by construction (the same
single ordered fold), exactly like the existing ``swing`` group — a STANDARD zigzag repaints (confirms pivots
with future bars) and would manufacture a fake edge; this does not.

WHY A LADDER, NOT ONE THRESHOLD. "Major vs blip" is the name's OWN volatility, so delta_s = SCALE[s] * sigma,
sigma = trailing realized per-minute log-return vol (point-in-time, floored/capped). The geometric ladder
SCALE = (0.5, 1, 2, 4) samples two octaves of structure (fine texture .. large regime moves) — geometric
because the DC scaling laws are power laws in delta. We assert NO winning scale; the StrategyHarness winnows
the family with FDR across scales. The CROSS-SCALE-CONSISTENCY features (structure that persists across scales
= robust; single-scale-only = noise) are the built-in noise filter that justifies carrying all four scales, and
the THRESHOLD-RESPONSE SIGNATURE (how #chunks / chunk-size scale with delta — the empirical DC scaling-law
exponent + overshoot/delta ratio) is a parameter-free roughness fingerprint.

PER-LEG TRADES + QUOTE ACTIVITY come free from ``minute_agg`` (``n_trades`` summed and ``mean_spread_bps``
averaged over a leg's minutes) — Layer A, no raw-tape join, RT-trivial.

THE FOLD lives in the Rust ``quant_tick.swing_dc_fold`` kernel (each bar's per-scale DC state depends on the
prior bar's state — not vectorizable in Polars), called identically from the live tape and the backfill through
this ONE group, so parity holds by construction; a pure-Python reference pins the Rust output cell-for-cell
(tests/test_fp_swing_dc.py).
"""
from __future__ import annotations

import polars as pl
import quant_tick

from quantlib.features.base import (
    BatchContext,
    FeatureGroup,
    FeatureSpec,
    FeatureType,
    InputSpec,
)
from quantlib.features.registry import register

SCALES: tuple[float, ...] = (0.5, 1.0, 2.0, 4.0)
VOL_WIN: int = 30  # trailing minutes for the realized per-minute log-return sigma
THETA_FLOOR: float = 0.001  # 10 bps floor so a flat name doesn't pivot on every tick
THETA_CAP: float = 0.05  # 5% cap so a garbage print can't set an absurd threshold
RING_K: int = 8  # confirmed legs kept per scale (>= the detailed legs + percentile history)
DAY_SECS: int = 86_400
FIB_DC_MAX_ABS: float = 10.0  # degenerate-basis guard, mirrored from the kernel

# Per-scale feature stems, in the EXACT order the Rust kernel's emit_row writes them (16 per scale).
_PER_SCALE_STEMS: tuple[str, ...] = (
    "cur_dir",
    "minutes_since_dc",
    "last_leg_height",
    "last_leg_slope",
    "last_leg_dur",
    "last_leg_ntrades",
    "last_leg_spread",
    "last_leg_pctile",
    "os_to_dc",
    "persistence",
    "fib_retr",
    "fib_golden",
    "fib_hold618",
    "fib_broke786",
    "fib_ext",
    "fib_dist",
)
# Global (scale-agnostic) feature stems, in emit order (5 cross-scale + 4 response + 1 sigma).
_GLOBAL_STEMS: tuple[str, ...] = (
    "xscale_dir_agreement",
    "xscale_dir_dominant",
    "xscale_setup_long_count",
    "xscale_pivot_coincidence",
    "xscale_finest_only",
    "resp_nlegs_slope",
    "resp_chunk_slope",
    "resp_os_ratio_mean",
    "resp_roughness",
    "sigma30_bps",
)


def _scale_tag(scale: float) -> str:
    """A filename-safe scale tag: 0.5 -> ``s05``, 1.0 -> ``s1``, 2.0 -> ``s2``, 4.0 -> ``s4``."""
    if scale == int(scale):
        return f"s{int(scale)}"
    return "s" + str(scale).replace(".", "")


def _feature_cols() -> list[str]:
    """The full ordered feature column list: per-scale block (scale-major) then the global block."""
    cols: list[str] = []
    for scale in SCALES:
        tag = _scale_tag(scale)
        cols.extend(f"dc_{stem}_{tag}" for stem in _PER_SCALE_STEMS)
    cols.extend(f"dc_{stem}" for stem in _GLOBAL_STEMS)
    return cols


_FEATURE_COLS: tuple[str, ...] = tuple(_feature_cols())

_SCHEMA: dict[str, pl.DataType] = {
    "symbol": pl.String,
    "minute": pl.Datetime("us", "UTC"),
    **{name: pl.Float64 for name in _FEATURE_COLS},
}

# Flag features (0/1) — stored UInt8. Their stems carry a fib_/xscale_ prefix and a binary range.
_FLAG_STEMS: frozenset[str] = frozenset({"fib_golden", "fib_hold618", "fib_broke786"})
# Warmup features — null before sigma / the first leg / the first pivot exists (NaN sentinels -> null).
_WARMUP_STEMS: frozenset[str] = frozenset(
    {
        "minutes_since_dc",
        "last_leg_height",
        "last_leg_slope",
        "last_leg_dur",
        "last_leg_ntrades",
        "last_leg_spread",
        "last_leg_pctile",
        "os_to_dc",
        "fib_retr",
        "fib_ext",
        "fib_dist",
    }
)
_WARMUP_GLOBAL: frozenset[str] = frozenset(
    {
        "xscale_dir_agreement",
        "resp_nlegs_slope",
        "resp_chunk_slope",
        "resp_os_ratio_mean",
        "resp_roughness",
        "sigma30_bps",
    }
)


def swing_dc_fold_frame(frame: pl.DataFrame) -> pl.DataFrame:
    """Run the point-in-time multi-scale DC fold over EVERY (symbol, minute) in ``frame`` via the Rust kernel.

    Codes symbols to ints + sorts (symbol, minute) so the kernel folds each symbol's series in order, emitting
    one row per input bar with that bar's POINT-IN-TIME multi-scale DC vector (only bars <= that bar were read).
    Returns a (symbol, minute, <feature cols>) frame. The whole-history fold IS the parity reference: the live
    path takes the latest minute of the same fold, so live == backfill cell-for-cell."""
    base = frame.select(["symbol", "minute", "close", "n_trades", "mean_spread_bps"])
    if base.height == 0:
        return pl.DataFrame(schema=_SCHEMA)
    uniq = sorted(base["symbol"].unique().to_list())
    codes = pl.DataFrame(
        {"symbol": uniq, "_code": list(range(len(uniq)))},
        schema={"symbol": pl.String, "_code": pl.Int64},
    )
    coded = (
        base.join(codes, on="symbol", how="left")
        .with_columns(pl.col("minute").dt.epoch("s").alias("_mi"))
        .sort(["_code", "_mi"])
    )
    out = quant_tick.swing_dc_fold(
        coded["_code"].to_numpy(),
        coded["_mi"].to_numpy(),
        coded.select(pl.col("close").cast(pl.Float64)).to_numpy().reshape(-1),
        coded.select(pl.col("n_trades").cast(pl.Float64)).to_numpy().reshape(-1),
        coded.select(pl.col("mean_spread_bps").cast(pl.Float64)).to_numpy().reshape(-1),
        list(SCALES),
        VOL_WIN,
        THETA_FLOOR,
        THETA_CAP,
        RING_K,
        DAY_SECS,
    )
    result = coded.select(["symbol", "minute"]).with_columns(
        [pl.Series(name, out[i], dtype=pl.Float64) for i, name in enumerate(_FEATURE_COLS)]
    )
    # NaN sentinels (warmup / no-leg / degenerate-basis) restore to Polars null so the warmup nan_policy holds
    # and parity treats them as MISSING, not a finite reading — applied identically on the one fold path.
    null_cols: list[str] = []
    for scale in SCALES:
        tag = _scale_tag(scale)
        null_cols.extend(f"dc_{stem}_{tag}" for stem in _WARMUP_STEMS)
    null_cols.extend(f"dc_{stem}" for stem in _WARMUP_GLOBAL)
    result = result.with_columns([pl.col(name).fill_nan(None) for name in null_cols])
    return result.select(["symbol", "minute", *_FEATURE_COLS])


@register
class SwingDcGroup(FeatureGroup):
    name = "swing_dc"
    version = "1.0.0"
    owner = "feature-dev"
    type = FeatureType.TREND_QUALITY
    inputs = (
        InputSpec(
            name="minute_agg",
            columns=("symbol", "minute", "close", "n_trades", "mean_spread_bps"),
        ),
    )

    def declare(self) -> list[FeatureSpec]:
        specs: list[FeatureSpec] = []
        for scale in SCALES:
            tag = _scale_tag(scale)
            specs.extend(self._declare_scale(scale, tag))
        specs.extend(self._declare_global())
        return specs

    def _declare_scale(self, scale: float, tag: str) -> list[FeatureSpec]:
        mult = f"{scale:g}x-sigma"
        descriptions: dict[str, tuple[str, tuple[float | None, float | None], str]] = {
            "cur_dir": (
                f"Direction of the current (provisional) DC leg at the {mult} threshold: +1 in an up-leg, -1 "
                f"in a down-leg, 0 before a direction is established.",
                (-1.0, 1.0),
                "none",
            ),
            "minutes_since_dc": (
                f"Minutes since the last confirmed directional change at {mult} (the current overshoot's age); "
                f"null before the first DC.",
                (0.0, 1e6),
                "warmup",
            ),
            "last_leg_height": (
                f"Signed fractional price move of the last COMPLETED leg (chunk) at {mult}, start to pivot; "
                f"null before a leg completes.",
                (-10.0, 10.0),
                "warmup",
            ),
            "last_leg_slope": (
                f"Per-minute slope (signed fractional return / minutes) of the last completed leg at {mult}; "
                f"null before a leg completes.",
                (-1.0, 1.0),
                "warmup",
            ),
            "last_leg_dur": (
                f"Duration in minutes of the last completed leg at {mult}; null before a leg completes.",
                (0.0, 1e6),
                "warmup",
            ),
            "last_leg_ntrades": (
                f"Total trade count (sum of minute n_trades) over the last completed leg at {mult}; null "
                f"before a leg completes.",
                (0.0, None),
                "warmup",
            ),
            "last_leg_spread": (
                f"Mean minute spread in bps over the last completed leg at {mult} (quote activity during the "
                f"chunk); null before a leg completes.",
                (0.0, None),
                "warmup",
            ),
            "last_leg_pctile": (
                f"Percentile rank of the last completed leg's absolute height vs the name's recent leg-history "
                f"at {mult} (0..1); null before a leg completes.",
                (0.0, 1.0),
                "warmup",
            ),
            "os_to_dc": (
                f"Current provisional overshoot magnitude divided by the {mult} threshold (the overshoot "
                f"scaling-law observable); null while undirected.",
                (0.0, None),
                "warmup",
            ),
            "persistence": (
                f"Net signed leg progression over the recent legs plus the provisional leg at {mult} "
                f"(same-signed legs accumulate; chop cancels toward 0).",
                (-100.0, 100.0),
                "none",
            ),
            "fib_retr": (
                f"Current pullback as a fraction of the last completed {mult} leg's range, measured from the "
                f"leg end back toward its start (0 at the pivot, 1 a full retrace); null until a leg completes "
                f"or on a degenerate near-zero-range basis.",
                (-FIB_DC_MAX_ABS, FIB_DC_MAX_ABS),
                "warmup",
            ),
            "fib_golden": (
                f"1.0 when the close sits in the 61.8-78.6% retracement band of the last completed {mult} leg "
                f"(the classic continuation 'golden zone'), else 0.0.",
                (0.0, 1.0),
                "none",
            ),
            "fib_hold618": (
                f"1.0 when price retraced to ~50-78.6% of the last completed {mult} leg AND the provisional "
                f"leg has resumed in the CONTINUATION direction (opposite the completed pullback leg) — a held "
                f"golden-ratio pullback that resumed the prior trend, else 0.0.",
                (0.0, 1.0),
                "none",
            ),
            "fib_broke786": (
                f"1.0 when price retraced PAST 78.6% of the last completed {mult} leg (setup invalidated / "
                f"likely full reversal), else 0.0.",
                (0.0, 1.0),
                "none",
            ),
            "fib_ext": (
                f"Continuation travel toward the 127.2/161.8% extension of the last completed {mult} leg once "
                f"price passes the prior pivot in the leg direction (0 at the pivot, ~1 at 161.8%); null until "
                f"a leg completes.",
                (0.0, 1.0),
                "warmup",
            ),
            "fib_dist": (
                f"Signed distance (in leg-range fractions) from the current retracement to the nearest standard "
                f"Fibonacci level (38.2/50/61.8/78.6) at {mult}; small magnitude = sitting on a level; null "
                f"until a leg completes.",
                (-FIB_DC_MAX_ABS, FIB_DC_MAX_ABS),
                "warmup",
            ),
        }
        specs: list[FeatureSpec] = []
        for stem in _PER_SCALE_STEMS:
            desc, vrange, policy = descriptions[stem]
            specs.append(
                FeatureSpec(
                    name=f"dc_{stem}_{tag}",
                    description=desc,
                    dtype="Float64",
                    valid_range=vrange,
                    nan_policy=policy,
                    layer="A",
                    storage="UInt8" if stem in _FLAG_STEMS else None,
                )
            )
        return specs

    def _declare_global(self) -> list[FeatureSpec]:
        global_specs: dict[str, tuple[str, tuple[float | None, float | None], str, str | None]] = {
            "xscale_dir_agreement": (
                "Fraction of the scale ladder whose current provisional leg agrees in sign (1.0 = a move "
                "directed at every granularity = robust; low = single-scale = likely noise); null before any "
                "scale is directed.",
                (0.0, 1.0),
                "warmup",
                None,
            ),
            "xscale_dir_dominant": (
                "The signed direction held by the majority of scales (+1/-1/0) — the cross-scale-robust "
                "direction.",
                (-1.0, 1.0),
                "none",
                None,
            ),
            "xscale_setup_long_count": (
                "How many scales currently fire the golden-zone long-continuation setup (multi-scale "
                "agreement = the high-conviction 'beginning of a likely-up chunk' read).",
                (0.0, float(len(SCALES))),
                "none",
                None,
            ),
            "xscale_pivot_coincidence": (
                "How many scales confirmed a directional change on THIS minute (a pivot aligned across scales "
                "= a structurally significant turn).",
                (0.0, float(len(SCALES))),
                "none",
                None,
            ),
            "xscale_finest_only": (
                "1.0 when directional structure exists at the finest scale but NOT at any coarser (>= 2x) "
                "scale — a 'this is just texture/noise' flag by construction, else 0.0.",
                (0.0, 1.0),
                "none",
                "UInt8",
            ),
            "resp_nlegs_slope": (
                "Slope of log(1+n_legs) vs log(threshold) across the ladder — the empirical DC scaling-law "
                "exponent (steeper negative = a choppier/noisier path); null before sigma / any legs exist.",
                (None, None),
                "warmup",
                None,
            ),
            "resp_chunk_slope": (
                "Slope of log(median |leg height|) vs log(threshold) across the ladder; null before sigma / "
                "any legs exist.",
                (None, None),
                "warmup",
                None,
            ),
            "resp_os_ratio_mean": (
                "Mean across scales of the provisional overshoot/threshold ratio (the overshoot scaling-law "
                "observable; ~1 normal, >1 trending, <1 mean-reverting recently); null when no scale is "
                "directed.",
                (0.0, None),
                "warmup",
                None,
            ),
            "resp_roughness": (
                "Ratio of n_legs at the finest scale to n_legs at the coarsest (high = rough/choppy path, low "
                "= clean trend); null before any legs exist.",
                (0.0, None),
                "warmup",
                None,
            ),
            "sigma30_bps": (
                "The trailing 30-minute realized per-minute log-return volatility in bps that scales the "
                "threshold ladder (observability); null during the sigma warmup.",
                (0.0, None),
                "warmup",
                None,
            ),
        }
        specs: list[FeatureSpec] = []
        for stem in _GLOBAL_STEMS:
            desc, vrange, policy, storage = global_specs[stem]
            specs.append(
                FeatureSpec(
                    name=f"dc_{stem}",
                    description=desc,
                    dtype="Float64",
                    valid_range=vrange,
                    nan_policy=policy,
                    layer="A",
                    storage=storage,
                )
            )
        return specs

    def compute(self, ctx: BatchContext) -> pl.DataFrame:
        """BACKFILL + LIVE (source of truth): the point-in-time multi-scale DC fold over the whole buffer, one
        row per minute. The live path uses the default ``compute_latest`` (this fold, filtered to T) — NOT a
        window slice: several features read the cross-leg RING (the percentile rank, the persistence sum, the
        response-signature legs), whose contents depend on legs that can sit further back than a fixed-minute
        window. Window-slicing would truncate that ring and diverge from backfill; the whole-buffer fold is
        O(1) per bar per scale so it stays in the latency budget, exactly like the existing ``swing`` group.
        """
        frame = ctx.frame("minute_agg").select(["symbol", "minute", "close", "n_trades", "mean_spread_bps"])
        return swing_dc_fold_frame(frame)
