"""FP_INCREMENTAL real-data sandbox soak. Replays real captured bars (one trading day) minute-by-minute
through the production incremental path (IncrementalEngine.step, the centered default = assemble_from_long)
vs the batch truth (compute_reduction_batch), and grades EACH incremental_safe reduction group with the EXACT
production self-check (capture._incremental_parity, ratio>10 or null/non-null flip = breach).

Reproduces the production process_bars split: a SEPARATE incremental engine over the incremental_safe groups
(no mixed-engine confound), the volume anchor attached from a daily snapshot (so volume's centered std finds
its anchor), graded over the post-warmup minutes (every window incl 180m fully filled). The synthetic stream
in tests/test_fp_incremental_features.py only triggers `distribution`; this real-data A/B is the authority on
the per-group arming list the readiness doc defers to.

Per-group verdict: a group is GO iff it NEVER breaches across the graded minutes.

Run (in fp-dev with the live store mounted read-only):
  docker run --rm --cpus 4 -v "$PWD":/app -w /app -v fp_store_real:/store:ro \\
    fp-dev python scripts/incremental_realdata_soak.py 2026-06-17 [SYM1,SYM2,...]"""

from __future__ import annotations

import glob
import os
import sys

import polars as pl

from quantlib.features.base import BatchContext
from quantlib.features.capture import _PARITY_BREACH_RATIO, _incremental_parity
from quantlib.features.compare import runnable
from quantlib.features.declarative import ReductionGroup, compute_reduction_batch
from quantlib.features.incremental import IncrementalEngine
from quantlib.features.reduction_anchor import attach_reduction_anchors

STORE = "/store/raw/bars"
DEEPEST_WINDOW_M = 180  # grade only once every declared window (incl 180m) is fully filled


def load_minute_agg(date: str, symbols: list[str]) -> pl.DataFrame:
    """Build the minute_agg frame from real raw bars for one date. Renames ts->minute, supplies the tick-
    aggregate columns the reduction groups read (signed_volume/n_trades/spread/imbalance/sizes) — real where
    the bar carries them (trade_count), neutral (0/null) where the raw bar has no tick enrichment. This is the
    bar-only substrate; it exercises the volume/return/OLS reductions, which is what the 20 ready groups are.
    """
    frames = []
    for sym in symbols:
        files = glob.glob(f"{STORE}/symbol={sym}/date={date}/*.parquet")
        if not files:
            continue
        df = pl.read_parquet(files[0])
        frames.append(df.with_columns(pl.lit(sym).alias("symbol")))
    if not frames:
        raise SystemExit(f"no bars for {date} across {len(symbols)} symbols")
    bars = pl.concat(frames, how="vertical_relaxed")
    return (
        bars.rename({"ts": "minute"})
        .with_columns(
            pl.col("volume").cast(pl.Float64),
            pl.col("trade_count").cast(pl.Float64).alias("n_trades"),
            # tick-derived columns the trade/quote groups read; neutral fills (real bar-only soak — the
            # reduction MATH on volume/return/OLS is what we are grading, present on every bar).
            (pl.col("volume").cast(pl.Float64) * 0.0).alias("signed_volume"),
            pl.lit(0.0).alias("mean_spread_bps"),
            pl.lit(0.0).alias("quote_imbalance"),
            pl.lit(0.0).alias("mean_bid_size"),
            pl.lit(0.0).alias("mean_ask_size"),
        )
        .select(
            "symbol",
            "minute",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "n_trades",
            "signed_volume",
            "mean_spread_bps",
            "quote_imbalance",
            "mean_bid_size",
            "mean_ask_size",
        )
        .sort(["symbol", "minute"])
    )


def daily_snapshot(minute_agg: pl.DataFrame, scale: str = "daily_total") -> pl.DataFrame:
    """A per-symbol daily volume+close snapshot (the anchor source) — production-faithful: ``daily_total`` =
    the per-symbol whole-day total volume, exactly what production's daily-bar ``daily.volume`` provides, plus
    the per-symbol daily ``close`` (the y-side OLS conditioning anchor, ``attach_close_anchor``). The
    per-minute scaling lives INSIDE ``attach_volume_anchor`` (``/ _RTH_MINUTES_PER_DAY``), so feeding the raw
    daily total here exercises the real production anchor path. (``minute_mean`` remains for A/B-ing the
    pre-fix mismatch — it pre-scales to the per-minute mean.)"""
    agg = pl.col("volume").mean() if scale == "minute_mean" else pl.col("volume").sum()
    return (
        minute_agg.group_by("symbol")
        .agg(agg.alias("volume"), pl.col("close").last().alias("close"))
        .with_columns(pl.lit(1).alias("date"))
    )


def main() -> None:
    date = sys.argv[1] if len(sys.argv) > 1 else "2026-06-17"
    syms_arg = sys.argv[2] if len(sys.argv) > 2 else ""
    if syms_arg:
        symbols = syms_arg.split(",")
    else:
        # liquid + mid + sparse mix (sparse names exercise the gappy-window degenerate cells)
        symbols = [
            "AAPL",
            "MSFT",
            "NVDA",
            "TSLA",
            "AMZN",
            "META",
            "GOOGL",
            "AMD",
            "SPY",
            "QQQ",
            "JPM",
            "BAC",
            "XOM",
            "CVX",
            "PFE",
            "KO",
            "WMT",
            "DIS",
            "INTC",
            "F",
            "IIF",
            "GE",
            "T",
            "C",
            "WFC",
            "NKE",
            "BA",
            "CAT",
            "MMM",
            "ORCL",
        ]

    minute_agg = load_minute_agg(date, symbols)
    daily = daily_snapshot(minute_agg)
    # the production anchor-attach (the SAME entrypoint capture.py + materialize.py call before seeding)
    minute_agg = attach_reduction_anchors({"minute_agg": minute_agg, "daily": daily})["minute_agg"]
    present = sorted(minute_agg["symbol"].unique().to_list())
    minutes = sorted(minute_agg["minute"].unique())
    print(f"=== SOAK {date}: {len(present)} symbols, {len(minutes)} minutes, {minute_agg.height} rows ===")

    groups = [g for g in runnable({"minute_agg": minute_agg}) if isinstance(g, ReductionGroup)]
    # FP_SOAK_PROBE_PARKED=g1,g2,... force-promotes the named parked groups to incremental_safe for THIS probe
    # only (never touches prod) — the reproduction path for the FP_RUST_REDUCE breach→clean gate
    # (docs/INCREMENTAL_READINESS.md): run with FP_RUST_REDUCE=0 then =1 over
    # FP_SOAK_PROBE_PARKED=trend_quality,clean_momentum,residual_analysis and compare the per-group worst ratio.
    probe_parked = set(filter(None, os.environ.get("FP_SOAK_PROBE_PARKED", "").split(",")))
    if probe_parked:
        for group in groups:
            if group.name in probe_parked:
                group.incremental_safe = True
        print(f"PROBE: force-incremental_safe (probe-only, NOT prod): {sorted(probe_parked)}")
    safe = [g for g in groups if g.incremental_safe]
    unsafe = [g for g in groups if not g.incremental_safe]
    print(
        f"reduction groups: {len(groups)} total, {len(safe)} incremental_safe (graded), "
        f"{len(unsafe)} parked: {[g.name for g in unsafe]}"
    )

    # Production split: a SEPARATE engine over the safe groups only (the running sums it seeds are the safe
    # set, exactly as process_bars does).
    eng_safe = IncrementalEngine(safe)

    ever_breached: dict[str, float] = {}  # group -> worst ratio seen
    clean_groups: set[str] = {g.name for g in safe}
    graded = 0
    worst_overall = 0.0
    anchor_nonzero = None

    for ti, minute in enumerate(minutes):
        buffer = minute_agg.filter(pl.col("minute") <= minute)
        ctx = BatchContext(frames={"minute_agg": buffer})
        # EXACT production self-check: batch (compute_reduction_batch = the written truth) vs incremental step.
        batched = compute_reduction_batch(safe, ctx)
        inc_out = eng_safe.step(buffer, slice_derive=True)
        if ti <= DEEPEST_WINDOW_M:
            continue
        graded += 1
        for group in safe:
            ratio = _incremental_parity(
                {group.name: batched[group.name]}, {group.name: inc_out.get(group.name)}
            )
            worst_overall = max(worst_overall, ratio if ratio != float("inf") else 1e18)
            if ratio > _PARITY_BREACH_RATIO:
                ever_breached[group.name] = max(ever_breached.get(group.name, 0.0), ratio)
                clean_groups.discard(group.name)

    # verify the anchor is non-zero (volume centering actually engaged)
    if "_b" in str(minute_agg.columns):
        pass
    anchor_cols = [c for c in minute_agg.columns if "anchor" in c.lower()]
    if anchor_cols:
        anchor_nonzero = minute_agg.select(pl.col(anchor_cols[0]).gt(0).sum()).item()

    print(
        f"\ngraded {graded} post-warmup minutes; worst tol-ratio overall={worst_overall:.2f} "
        f"(breach threshold {_PARITY_BREACH_RATIO})"
    )
    print(f"anchor column(s): {anchor_cols}  non-zero-anchor rows: {anchor_nonzero}")
    print("\n=== PER-GROUP VERDICT (20 ready reduction groups) ===")
    for group in sorted(safe, key=lambda g: g.name):
        if group.name in ever_breached:
            print(f"  [NO-GO]  {group.name:24s} worst-ratio={ever_breached[group.name]:.1f}")
        else:
            print(f"  [GO]     {group.name:24s} clean")
    n_go = len(clean_groups)
    print(
        f"\nSUMMARY: {n_go}/{len(safe)} ready groups CLEAN. "
        f"{'ALL-GO' if n_go == len(safe) else 'BREACHERS: ' + ','.join(sorted(ever_breached))}"
    )


if __name__ == "__main__":
    main()
