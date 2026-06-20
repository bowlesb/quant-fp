"""Window-scale trade-size Shannon entropy (family: MICROSTRUCTURE, Layer C).

WHY (feature-invention batch 3, experiments/2026-06-19-feature-invention): the SHAPE of a name's
trade-size distribution over the trailing window — how dispersed it is across order-of-magnitude size
scales — is the single STRONGEST and most ORTHOGONAL predictor of the batch. A high-entropy size
distribution (a mix of odd-lots AND blocks across many scales = information-driven trading by mixed
participants) precedes a 2%-vol-BURST. Against the validated burst label (``|fwd ret| >= 2%`` over
5/20/30m, the same target ``realized_range`` + ``large_print_burst`` were promoted on) ``f_size_entropy``
carries AUC-0.5 +0.15 (z 27-30), stable-sign across all three horizons (+,+,+); and it simultaneously
wins forward-vol (IC +0.67, z 196) AND forward-RV (IC +0.23, z 125) — the only batch-3 feature strong on
all three — while being near-orthogonal to every other find (cross-feature within-ts |IC| max ~0.25). It
is a genuinely new channel: ``inter_arrival.trade_timing_entropy_1m`` is entropy of the trade-TIMING
distribution within ONE minute; this is entropy of the trade-SIZE distribution over the trailing window.

DEFINITION: bin each print by its order-of-magnitude size ``floor(log10(size))`` clipped to [0, 5] → 6
bounded bins (1-9, 10-99, ..., 100k+ shares). ``size_entropy_{w}m`` is the Shannon entropy (nats) of the
window's bin-count distribution: with ``c_b = Σ count of prints in bin b`` over the trailing ``w``
minutes and ``N = Σ_b c_b``, ``p_b = c_b / N`` and entropy ``= −Σ_b p_b · ln(p_b)`` (the ``p_b = 0``
bins contribute 0). In [0, ln 6 ≈ 1.79]: 0 = all prints one size scale, max = uniform across all 6.

PARITY (Layer C, PARITY_PROMOTION_GATE.md — this is the batch-3 YELLOW): the bin COUNTS are pure bounded
trailing sums of a per-minute primitive (the 6 per-minute bin counts), so they are GREEN-able; the
nonlinear entropy is a fixed ``assemble``-style function of those 6 reduction columns (NOT the canonical
mean/std/sum shape, hence the YELLOW tag — a custom path rather than a pure ``ReductionGroup`` reduced()).
We make it parity-true BY CONSTRUCTION the same way the windowed custom groups do: the live path is
``compute_latest_on_window`` — the IDENTICAL ``compute()`` run on the input sliced to the trailing window
it reads — so live == backfill cell-for-cell (the dropped older minutes cannot influence a window ending
at T). The YELLOW requirement (a dedicated degenerate-window live==backfill parity test) is satisfied in
tests/test_fp_b3_concentration.py, which builds a single-scale (zero-entropy) and a constant-count window
and asserts ``compute_latest`` == ``compute().last`` exactly. GUARDS: the entropy guards its only divisor
``N > 0`` (Guard 2 — N is a count, a non-negative sum, sign-robust → NULL on a tradeless window), each
``ln(p_b)`` is taken only where ``c_b > 0`` (so ``0·ln 0`` never arises), and an ``is_finite()`` backstop
converts any stray non-finite to the agreed NULL identically on both paths. RT-trivial bounded sums; no
OLS, no order statistic, not path-dependent.
"""

from __future__ import annotations

import polars as pl

from quantlib.features.base import (
    BatchContext,
    FeatureGroup,
    FeatureSpec,
    FeatureType,
    InputSpec,
)
from quantlib.features.registry import register

WINDOWS: tuple[int, ...] = (30, 60)
N_BINS = 6  # order-of-magnitude size bins: floor(log10(size)) clipped to [0, 5]
_BINS = tuple(range(N_BINS))
# Slack added to the deepest window for the live window-slice (see print_hhi._WINDOW_SLACK).
_WINDOW_SLACK = 1

_OUT_SCHEMA = {
    "symbol": pl.String,
    "minute": pl.Datetime("us", "UTC"),
    **{f"size_entropy_{w}m": pl.Float64 for w in WINDOWS},
}


@register
class SizeEntropyGroup(FeatureGroup):
    name = "size_entropy"
    version = "1.0.0"
    owner = "modeller"
    type = FeatureType.MICROSTRUCTURE
    inputs = (InputSpec(name="trades", columns=("symbol", "ts", "price", "size")),)

    def declare(self) -> list[FeatureSpec]:
        return [
            FeatureSpec(
                name=f"size_entropy_{w}m",
                description=(
                    f"Shannon entropy (nats) of the trailing {w}-minute trade-SIZE distribution over 6 "
                    f"order-of-magnitude size bins (floor(log10(size)) clipped to [0,5]): −Σ p_b ln p_b. "
                    f"In [0, ln 6 ≈ 1.79]: 0 = all prints one size scale, high = a dispersed mix of "
                    f"odd-lots and blocks (information-driven trading, a vol-burst precursor; batch-3 "
                    f"screen z 27-30, fwd-vol IC +0.67). Null on a window with no trades."
                ),
                dtype="Float64",
                valid_range=(0.0, 1.8),
                nan_policy="sparse",
                layer="C",
                parity_method="tolerance",
            )
            for w in WINDOWS
        ]

    def compute(self, ctx: BatchContext) -> pl.DataFrame:
        trades = ctx.frame("trades").select(["symbol", "ts", "size"])
        if trades.height == 0:
            return pl.DataFrame(schema=_OUT_SCHEMA)
        per_minute = _per_minute_bin_counts(trades).sort(["symbol", "minute"])
        # Trailing windowed SUM of each per-minute bin count (the bounded reductions the entropy is built
        # from). Then the entropy of the window's bin-count distribution in assemble below.
        feats: list[pl.Expr] = []
        for w in WINDOWS:
            window_counts = [
                pl.col(f"_c{b}").rolling_sum_by("minute", window_size=f"{w}m").over("symbol")
                for b in _BINS
            ]
            feats.append(_entropy_expr(window_counts).alias(f"size_entropy_{w}m"))
        return per_minute.with_columns(feats).select(
            ["symbol", "minute", *[f"size_entropy_{w}m" for w in WINDOWS]]
        )

    def compute_latest(self, ctx: BatchContext) -> pl.DataFrame:
        """Latest-minute live path — VALUE-IDENTICAL to ``compute().filter(minute == max)`` but skips the
        per-minute ``rolling_sum_by`` pass whose every row but T is discarded.

        The window bin-count at the latest minute T is the SUM of the per-minute bin counts over the same
        left-open trailing window ``rolling_sum_by`` reads — ``minute in (T - w, T]`` — so a single
        ``filter -> group_by(symbol).sum()`` reproduces ``compute()``'s windowed counts at T exactly, then
        ``_entropy_expr`` (the identical assemble) maps them to entropy. ``compute()``'s final
        ``filter(minute == global max)`` emits only symbols that traded in minute T (a sparse "no trades"
        minute yields no row); we match that by keying on ``per_minute``'s global max minute. Guarded by the
        generic latest-parity test (tests/test_fp_latest.py) + the degenerate-window test in
        tests/test_fp_b3_concentration.py."""
        trades = ctx.frame("trades").select(["symbol", "ts", "size"])
        if trades.height == 0:
            return pl.DataFrame(schema=_OUT_SCHEMA)
        per_minute = _per_minute_bin_counts(trades)
        latest_minute = per_minute["minute"].max()
        # compute()'s filter(minute == global max) keeps only symbols trading at T.
        result = (
            per_minute.filter(pl.col("minute") == latest_minute)
            .select("symbol")
            .unique()
            .with_columns(pl.lit(latest_minute).alias("minute"))
        )
        for w in WINDOWS:
            cutoff = latest_minute - pl.duration(minutes=w)
            window_counts = (
                per_minute.filter(
                    (pl.col("minute") > cutoff) & (pl.col("minute") <= latest_minute)
                )
                .group_by("symbol")
                .agg(*[pl.col(f"_c{b}").sum().alias(f"_c{b}") for b in _BINS])
                .with_columns(
                    _entropy_expr([pl.col(f"_c{b}") for b in _BINS]).alias(f"size_entropy_{w}m")
                )
                .select(["symbol", f"size_entropy_{w}m"])
            )
            result = result.join(window_counts, on="symbol", how="left")
        return result.select(
            ["symbol", "minute", *[f"size_entropy_{w}m" for w in WINDOWS]]
        )

    def reduce_buffer_minutes(self) -> int:
        return max(WINDOWS) + _WINDOW_SLACK


def _per_minute_bin_counts(trades: pl.DataFrame) -> pl.DataFrame:
    """Per (symbol, minute): the 6 order-of-magnitude bin counts of the minute's prints — a pure function
    of that minute's own tape (no look-ahead). Shared by both the backfill ``compute`` and the live
    ``compute_latest`` so the bin assignment (``floor(log10(size))`` clipped to [0,5], non-positive ->
    bin 0 to match the screen's fill_null) is defined once and identical on both paths."""
    per_trade = trades.with_columns(
        pl.col("ts").dt.truncate("1m").alias("minute"),
        pl.col("size").log10().floor().clip(0.0, 5.0).fill_null(0.0).alias("_bin"),
    )
    return per_trade.group_by(["symbol", "minute"]).agg(
        *[(pl.col("_bin") == float(b)).sum().cast(pl.Float64).alias(f"_c{b}") for b in _BINS]
    )


def _entropy_expr(window_counts: list[pl.Expr]) -> pl.Expr:
    """Shannon entropy (nats) of a bin-count distribution: −Σ p_b ln p_b, p_b = c_b / Σc.

    Built so the p_b=0 bins contribute exactly 0 (ln 0 never evaluated) and the only divisor (the total
    count N) is guarded > 0 (Guard 2). −Σ p ln p = ln(N) − (Σ c_b ln c_b)/N, evaluated with each
    c_b·ln(c_b) taken only where c_b > 0. is_finite() backstop converts any stray non-finite to NULL."""
    total = sum(window_counts)
    # Σ_b c_b · ln(c_b), with the c_b = 0 bins contributing 0 (the 0·ln0 = 0 convention).
    sum_clogc = sum(
        pl.when(count > 0.0).then(count * count.log()).otherwise(0.0) for count in window_counts
    )
    entropy = (
        pl.when(total > 0.0)
        .then(total.log() - sum_clogc / total)
        .otherwise(None)
    )
    return pl.when(entropy.is_finite()).then(entropy).otherwise(pl.lit(None, dtype=pl.Float64))
