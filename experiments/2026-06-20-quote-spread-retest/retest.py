"""#205 WEEKLY-REVERSAL QUOTE-SPREAD RE-TEST (prereg §E). ONE-SHOT, MEDIAN-ANCHORED.

The weekly reversal (#205) was a REAL, clean, survivorship-robust, OOS-consistent signal that died on cost
with a NEGATIVE MEDIAN net-of-cost under a flat 5bps proxy. The deepened quote tape is now queryable (379d,
2024-12-12→2026-06-18, liquid core complete). This re-runs the EXACT #205 weekly L/S net-of-cost over the
quote-covered weeks using each name's REAL effective spread instead of the 5bps proxy.

⭐ THE GATE IS THE NET MEDIAN (the coordinator's tempering note, pre-committed): the structural blocker was
the negative MEDIAN, NOT the mean. The ONLY outcome that reopens the surface is **net MEDIAN crosses
positive** under real effective spread. A better MEAN with median still < 0 = surface SETTLED — write it and
move to LP. No second look on a better mean.

COST MODEL: per-name effective half-spread = median over RTH of (ask-bid)/mid (in return units). A
liquidity-TAKING round trip on an L/S pair pays the spread on each of 4 crossings (long in, long out, short
in, short out) ≈ 2 x (long half-spread + short half-spread)... modeled per-leg: each name's entry+exit each
cross the half-spread, so per-name round-trip cost = 2 x half_spread; the L/S spread pays the long name's +
the short name's round-trip. Reported alongside the flat 5/10bps for comparison.

READ-ONLY: reads the #205 weekly_panel.parquet (host-mounted) + /store/raw/quotes. Bounded. Writes
retest_results.csv + console verdict.
"""

from __future__ import annotations

import glob
import os

import numpy as np
import polars as pl

PANEL = "/panel/experiments/2026-06-19-multiday-horizon/weekly_panel.parquet"
STORE = os.environ.get("STORE_ROOT", "/store")
OUT_DIR = "/app/experiments/2026-06-20-quote-spread-retest"
QUOTE_START = "2024-12-12"  # first quote day; the re-test window = panel weeks >= this
WINSOR_P = 0.01


def effective_half_spread(symbol: str, sample_days: list[str]) -> float | None:
    """Median RTH (ask-bid)/mid over the sampled days for one name, in RETURN units (not bps). The
    per-fill cost of crossing to the touch. None if no usable quotes."""
    spreads = []
    for day in sample_days:
        pattern = f"{STORE}/raw/quotes/symbol={symbol}/date={day}/*.parquet"
        files = glob.glob(pattern)
        if not files:
            continue
        df = pl.scan_parquet(files).select(["ts", "bid_price", "ask_price"]).collect()
        if df.height == 0:
            continue
        et = pl.col("ts").dt.convert_time_zone("America/New_York")
        etm = et.dt.hour().cast(pl.Int32) * 60 + et.dt.minute().cast(pl.Int32)
        rth = df.with_columns(etm.alias("_m")).filter(
            (pl.col("_m") >= 9 * 60 + 35) & (pl.col("_m") < 16 * 60)
        )
        rth = rth.filter((pl.col("bid_price") > 0) & (pl.col("ask_price") >= pl.col("bid_price")))
        if rth.height < 100:
            continue
        mid = (rth["ask_price"] + rth["bid_price"]) / 2.0
        hs = ((rth["ask_price"] - rth["bid_price"]) / 2.0 / mid).to_numpy()
        hs = hs[np.isfinite(hs) & (hs >= 0) & (hs < 0.05)]  # drop crazy prints (>5% half-spread)
        if len(hs):
            spreads.append(float(np.median(hs)))
    if not spreads:
        return None
    return float(np.median(spreads))  # half-spread in return units (e.g. 0.0001 = 1bp)


def ls_spread(
    panel: pl.DataFrame, half_spread: dict[str, float] | None, flat_bps: float | None
) -> dict[str, float]:
    """Decile long/short weekly spread net of cost. If half_spread given → per-name REAL effective spread
    (round-trip = 2x half per name); if flat_bps given → the #205 flat proxy (4 legs x bps). Reverse signal.
    """
    df = panel.select(["friday", "symbol", "rev_1w", "y_fwd_1w"]).drop_nulls()
    spreads = []
    for (_,), grp in df.group_by(["friday"]):
        if grp.height < 20:
            continue
        s = (-grp["rev_1w"]).to_numpy()  # reversal = short winners / long losers
        y = grp["y_fwd_1w"].to_numpy()
        syms = grp["symbol"].to_list()
        order = s.argsort()
        n = max(1, len(order) // 10)
        long_idx, short_idx = order[-n:], order[:n]
        gross = y[long_idx].mean() - y[short_idx].mean()
        if half_spread is not None:
            long_cost = np.mean([2.0 * half_spread.get(syms[i], np.nan) for i in long_idx])
            short_cost = np.mean([2.0 * half_spread.get(syms[i], np.nan) for i in short_idx])
            cost = np.nansum([long_cost, short_cost])
        else:
            cost = 4.0 * flat_bps / 1e4
        spreads.append(gross - cost)
    if len(spreads) < 8:
        return {}
    arr = np.array(spreads)
    return {
        "net_mean_bps": float(arr.mean() * 1e4),
        "net_median_bps": float(np.median(arr) * 1e4),
        "win": float(np.mean(arr > 0)),
        "weeks": len(arr),
    }


def main() -> None:
    panel = pl.read_parquet(PANEL).filter(pl.col("friday") >= QUOTE_START)
    lo = pl.col("y_fwd_1w").quantile(WINSOR_P).over("friday")
    hi = pl.col("y_fwd_1w").quantile(1 - WINSOR_P).over("friday")
    panel = panel.with_columns(pl.col("y_fwd_1w").clip(lo, hi))
    syms = panel["symbol"].unique().to_list()
    quote_days = sorted(
        p.split("date=")[1].split("/")[0] for p in glob.glob(f"{STORE}/raw/quotes/symbol=SPY/date=*")
    )
    sample_days = quote_days[:: max(1, len(quote_days) // 20)]  # ~20 sampled days for the spread estimate
    print(
        f"panel: {panel.height} obs, {panel['friday'].n_unique()} weeks (>= {QUOTE_START}), {len(syms)} syms"
    )
    print(f"estimating effective spread over {len(sample_days)} sampled quote days...", flush=True)

    half_spread: dict[str, float] = {}
    for i, sym in enumerate(syms):
        hs = effective_half_spread(sym, sample_days)
        if hs is not None:
            half_spread[sym] = hs
        if (i + 1) % 100 == 0:
            print(f"  spread {i+1}/{len(syms)}, {len(half_spread)} with quotes", flush=True)
    covered = [s for s in half_spread.values()]
    print(
        f"\nnames with real spread: {len(half_spread)}/{len(syms)}; "
        f"effective half-spread (bps): median={np.median(covered)*1e4:.2f} "
        f"p25={np.quantile(covered, .25)*1e4:.2f} p75={np.quantile(covered, .75)*1e4:.2f}"
    )

    # restrict the panel to names with a real spread (the tradeable set under the real cost model)
    panel_q = panel.filter(pl.col("symbol").is_in(set(half_spread)))

    records = []
    for label, hs_map, flat in (
        ("flat_5bps", None, 5.0),
        ("flat_10bps", None, 10.0),
        ("REAL_spread", half_spread, None),
    ):
        r = ls_spread(panel_q, hs_map, flat)
        r["cost_model"] = label
        records.append(r)
        print(f"\n=== {label} (quote-covered 51wk window only) ===")
        print(
            f"  net mean={r.get('net_mean_bps', float('nan')):.1f}bps  "
            f"net MEDIAN={r.get('net_median_bps', float('nan')):.1f}bps  "
            f"win={r.get('win', float('nan')):.2f}  weeks={r.get('weeks', 0)}"
        )

    # ⭐ THE DECISIVE ROBUSTNESS CHECK: is any +median a real-spread effect or just this favorable window?
    # Apply a FLAT cost equal to the REAL median full spread (2*half_median) across the FULL #205 panel + the
    # pre-quote period. If the full-panel / pre-2024 median stays NEGATIVE, the recent +median is a
    # SAMPLE-PERIOD artifact, NOT a real-spread reopening → surface settled.
    full = pl.read_parquet(PANEL)
    full = full.with_columns(
        pl.col("y_fwd_1w").clip(
            pl.col("y_fwd_1w").quantile(WINSOR_P).over("friday"),
            pl.col("y_fwd_1w").quantile(1 - WINSOR_P).over("friday"),
        )
    )
    median_half_bps = float(np.median(list(half_spread.values())) * 1e4)
    print(
        f"\n=== PERIOD DECOMPOSITION @ flat {median_half_bps:.1f}bps half-spread (the real liquid median) ==="
    )
    period_recs = []
    for lbl, df in (
        ("FULL_2016_2025", full),
        ("recent_quote_51wk", full.filter(pl.col("friday") >= QUOTE_START)),
        ("pre_2024_12", full.filter(pl.col("friday") < QUOTE_START)),
    ):
        r = ls_spread(df, None, median_half_bps)  # flat at the real median half-spread
        r["cost_model"] = f"period_{lbl}"
        period_recs.append(r)
        print(
            f"  {lbl:20s}: net mean={r.get('net_mean_bps', float('nan')):+.1f}  "
            f"net MEDIAN={r.get('net_median_bps', float('nan')):+.1f}  weeks={r.get('weeks', 0)}"
        )

    pl.DataFrame(records + period_recs).write_csv(f"{OUT_DIR}/retest_results.csv")
    real = next(r for r in records if r["cost_model"] == "REAL_spread")
    full_med = next(r for r in period_recs if r["cost_model"] == "period_FULL_2016_2025")["net_median_bps"]
    pre_med = next(r for r in period_recs if r["cost_model"] == "period_pre_2024_12")["net_median_bps"]
    robust = real.get("net_median_bps", -1) > 0 and full_med > 0 and pre_med > 0
    print(f"\n⭐ GATE (net MEDIAN>0 under REAL spread AND robust across periods):")
    print(f"   recent-window real-spread median = {real.get('net_median_bps', float('nan')):+.1f}bps")
    print(f"   FULL-panel median @real-spread    = {full_med:+.1f}bps")
    print(f"   pre-2024 median @real-spread      = {pre_med:+.1f}bps")
    print(
        f"   → {'REOPENS — robust +median, flag Lead' if robust else 'NOT ROBUST: +median is a recent-window artifact (full/pre-period medians NEGATIVE) → surface SETTLED'}"
    )


if __name__ == "__main__":
    main()
