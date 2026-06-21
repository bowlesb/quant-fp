"""Vol/magnitude-predictability EDGE screen — 'predict forward vol/range, not direction' lane.

Ben's reframe: features predicting forward realized-vol/range (IC +0.2..+0.3) is a REAL signal. The CRUX
is INCREMENTAL-OVER-PERSISTENCE: a forecast that only restates 'vol clusters' is not an edge; it must
beat the naive trailing-vol persistence baseline.

This builds a CLEAN panel directly from contiguous 1-min raw bars (the battery's multi-group intraday
join fans out at >=3 groups x several dates — a separate bug flagged in the report; sidestepped here):

  * entry minutes = the battery's tradeable >=09:35 ET 30-min cadence ($1 floor + liquidity floor).
  * forward realized-vol TARGET = std of the next H contiguous 1-min log-returns after the entry
    (the proper RV substrate — contiguous minutes, NOT 30-min samples). Point-in-time, no look-ahead.
  * trailing realized-vol BASELINE (own/persistence) = std of the prior W contiguous 1-min log-returns
    as-of the entry minute. This is the persistence forecast every candidate must beat.
  * candidate features = the trusted store features joined per-group at the entry minute (joined
    SEPARATELY per group, deduped, to avoid the multi-group fan-out).

For each candidate we report, per-timestamp + Newey-West, over the pooled panel:
  1. raw cross-sectional rank-IC vs forward-RV (headline magnitude predictability),
  2. within-timestamp SHUFFLE null (leakage canary),
  3. INCREMENTAL rank-IC after rank-residualizing BOTH signal and forward-RV on the trailing-vol
     baseline (the crux). collapse = |incr|/|raw|; ~0 => pure persistence, >~0.3 => net-new content,
  4. the trailing-vol baseline's OWN IC vs forward-RV (the bar to beat).

No L/S P&L (a magnitude target has no signed return). The net-of-cost straddle $-screen is g0_straddle.py.
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import os
import time

import numpy as np
import polars as pl

from quantlib.backtest import mean_ic, newey_west_tstat, per_timestamp_ic, shuffle_within_groups

STORE = os.environ.get("STORE_ROOT", "/store")
MIN_DOLLAR_VOL = 50_000.0
MIN_PRICE = 1.0
MIN_NAMES = 20

# tradeable >=09:35 ET 30-min cadence (13:35..19:35 UTC), mirrors the battery panel.
SAMPLE_HM_UTC = [(13, 35), (14, 5), (14, 35), (15, 5), (15, 35), (16, 5), (16, 35),
                 (17, 5), (17, 35), (18, 5), (18, 35), (19, 5), (19, 35)]

# trusted candidate anticipators (joined per-group at the entry minute) + the persistence baselines.
GROUPS = {
    "volatility": ["realized_vol_5m", "realized_vol_15m", "realized_vol_30m", "realized_vol_60m"],
    "price_returns": ["ret_5m", "ret_15m", "ret_30m"],
    "microstructure_burst": ["peak_trades_per_second_1m", "inter_arrival_cv_1m", "max_runup_1m"],
    "trade_flow": ["signed_volume_15m", "trade_freq_15m", "trade_rate_accel_1m"],
    "quote_spread": ["spread_bps_15m", "quote_imbalance_15m", "book_depth_1m"],
}
ALL_FEATURES = [f for feats in GROUPS.values() for f in feats]
BASELINE_FEATURE = "realized_vol_30m"  # a store trailing-vol feature, also reported as a candidate


def _raw_dates(start: str, end: str) -> list[str]:
    dates = {os.path.basename(p).replace("date=", "")
             for p in glob.glob(f"{STORE}/raw/bars/symbol=*/date=*")}
    return sorted(d for d in dates if start <= d <= end)


def _group_vdir(group: str) -> str:
    return sorted(glob.glob(f"{STORE}/group={group}/v=*"))[-1]


def _load_group_at(date_iso: str, group: str, feats: list[str], sample_ts: pl.Series) -> pl.DataFrame | None:
    files = sorted(glob.glob(f"{_group_vdir(group)}/source=backfill/date={date_iso}/data*.parquet"))
    if not files:
        return None
    frame = pl.concat([pl.read_parquet(p, columns=["symbol", "minute"] + feats) for p in files])
    frame = frame.filter(pl.col("minute").is_in(sample_ts)).unique(subset=["symbol", "minute"])
    return frame


def _build_date(date_iso: str, horizon_bars: int, trailing_bars: int) -> pl.DataFrame | None:
    pattern = f"{STORE}/raw/bars/symbol=*/date={date_iso}/*.parquet"
    if not glob.glob(pattern):
        return None
    bars = (
        pl.scan_parquet(pattern, hive_partitioning=True)
        .select(["symbol", "ts", "high", "low", "close", "volume"])
        .collect()
        .sort(["symbol", "ts"])
    )
    if bars.height == 0:
        return None
    # contiguous 1-min log-returns per symbol
    bars = bars.with_columns(
        (pl.col("close").log() - pl.col("close").shift(1).over("symbol").log()).alias("logret")
    )
    day = dt.date.fromisoformat(date_iso)
    sample_dts = [dt.datetime(day.year, day.month, day.day, h, m, tzinfo=dt.timezone.utc)
                  for (h, m) in SAMPLE_HM_UTC]
    sample_ts = pl.Series(sample_dts)

    # forward realized vol (std of next H logrets) + trailing realized vol (std of prior W logrets),
    # both as rolling stats over the contiguous per-symbol minute series, then sampled at entry minutes.
    bars = bars.with_columns(
        pl.col("logret").rolling_std(window_size=trailing_bars).over("symbol").alias("trail_rv"),
        # forward window: shift the rolling-std back by horizon so row t carries std(logret[t+1..t+H]).
        pl.col("logret").rolling_std(window_size=horizon_bars).shift(-horizon_bars).over("symbol").alias("fwd_rv"),
        # forward absolute move magnitude (|close[t+H]/close[t]-1|) for the straddle net-of-cost gate
        (pl.col("close").shift(-horizon_bars).over("symbol") / pl.col("close") - 1.0).abs().alias("fwd_absmove"),
        (pl.col("close") * pl.col("volume")).alias("dollar_vol"),
    )
    entry = bars.filter(pl.col("ts").is_in(sample_ts)).rename({"ts": "minute"})
    entry = entry.filter(
        (pl.col("close") >= MIN_PRICE)
        & (pl.col("dollar_vol") >= MIN_DOLLAR_VOL)
        & pl.col("fwd_rv").is_finite()
        & pl.col("trail_rv").is_finite()
    ).select(["symbol", "minute", "close", "dollar_vol", "trail_rv", "fwd_rv", "fwd_absmove"])
    if entry.height == 0:
        return None
    # join the trusted candidate features at the entry minute, per group (deduped) — no multi-group fanout
    for group, feats in GROUPS.items():
        gframe = _load_group_at(date_iso, group, feats, sample_ts)
        if gframe is None:
            return None
        entry = entry.join(gframe, on=["symbol", "minute"], how="left")
    return entry


def _rank(values: np.ndarray) -> np.ndarray:
    order = np.argsort(values, kind="mergesort")
    ranks = np.empty(values.size, dtype=float)
    ranks[order] = np.arange(values.size, dtype=float)
    return ranks / max(values.size - 1, 1)


def _rank_residualize(values: np.ndarray, control: np.ndarray) -> np.ndarray:
    out = np.full_like(values, np.nan, dtype=float)
    finite = np.isfinite(values) & np.isfinite(control)
    if finite.sum() < 30:
        return out
    y = _rank(values[finite])
    x = _rank(control[finite])
    if np.std(x) < 1e-12:
        out[finite] = y - np.mean(y)
        return out
    design = np.column_stack([np.ones(x.size), x])
    beta, *_ = np.linalg.lstsq(design, y, rcond=None)
    out[finite] = y - design @ beta
    return out


def _score(signal: np.ndarray, label: np.ndarray, trail: np.ndarray, groups: list[int],
           horizon_bars: int, seed: int) -> dict[str, float]:
    keep = np.isfinite(signal) & np.isfinite(label)
    idx = np.where(keep)[0]
    sig, lab, tv = signal[idx], label[idx], trail[idx]
    grp = [groups[i] for i in idx]
    real = per_timestamp_ic(list(sig), list(lab), grp, min_names=MIN_NAMES)
    raw_ic = mean_ic(real)
    shuf_ic = mean_ic(per_timestamp_ic(list(sig), shuffle_within_groups(list(lab), grp, seed), grp, min_names=MIN_NAMES))
    nw_t = newey_west_tstat(real, max(1, horizon_bars // 30 or 1))
    resid_sig = _rank_residualize(sig, tv)
    resid_lab = _rank_residualize(lab, tv)
    both = np.isfinite(resid_sig) & np.isfinite(resid_lab)
    rgrp = [grp[i] for i in range(len(grp)) if both[i]]
    incr_real = per_timestamp_ic(list(resid_sig[both]), list(resid_lab[both]), rgrp, min_names=MIN_NAMES)
    incr_ic = mean_ic(incr_real)
    incr_nw_t = newey_west_tstat(incr_real, max(1, horizon_bars // 30 or 1))
    collapse = abs(incr_ic) / abs(raw_ic) if (raw_ic == raw_ic and abs(raw_ic) > 1e-9) else float("nan")
    return {"raw_ic": raw_ic, "shuffle_ic": shuf_ic,
            "edge_vs_shuffle": (raw_ic - shuf_ic) if raw_ic == raw_ic else float("nan"),
            "nw_t": float(nw_t), "incr_ic": incr_ic, "incr_nw_t": float(incr_nw_t),
            "collapse": collapse, "n_rows": int(idx.size), "n_ts": len(real)}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2026-05-29")
    parser.add_argument("--end", default="2026-06-18")
    parser.add_argument("--universe-top", type=int, default=200)
    parser.add_argument("--horizon-bars", type=int, default=30, help="forward RV window in 1-min bars")
    parser.add_argument("--trailing-bars", type=int, default=30, help="trailing RV baseline window")
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    t0 = time.perf_counter()
    frames = []
    for date_iso in _raw_dates(args.start, args.end):
        built = _build_date(date_iso, args.horizon_bars, args.trailing_bars)
        if built is not None:
            frames.append(built)
    full = pl.concat(frames, how="vertical_relaxed")
    if args.universe_top:
        adv = (full.group_by("symbol").agg(pl.col("dollar_vol").mean().alias("adv"))
               .sort("adv", descending=True).head(args.universe_top)["symbol"])
        full = full.filter(pl.col("symbol").is_in(adv))
    t_panel = time.perf_counter()

    label = full["fwd_rv"].to_numpy().astype(float)
    trail = full["trail_rv"].to_numpy().astype(float)
    groups = [int(x) for x in full["minute"].dt.timestamp("ns").to_numpy()]

    base = _score(trail, label, trail, groups, args.horizon_bars, seed=13)
    rows = []
    for feat in ALL_FEATURES:
        sig = full[feat].to_numpy().astype(float)
        res = _score(sig, label, trail, groups, args.horizon_bars, seed=13)
        res["feature"] = feat
        rows.append(res)
    t_eval = time.perf_counter()
    rows.sort(key=lambda r: (r["incr_ic"] if r["incr_ic"] == r["incr_ic"] else -9), reverse=True)

    report = {"window": [args.start, args.end], "universe_top": args.universe_top,
              "horizon_bars": args.horizon_bars, "trailing_bars": args.trailing_bars,
              "n_rows": full.height, "n_symbols": full["symbol"].n_unique(),
              "panel_load_s": round(t_panel - t0, 2), "eval_s": round(t_eval - t_panel, 2),
              "baseline_feature": "trailing_rv_from_bars",
              "baseline_self_ic": base, "results": rows}
    if args.out:
        with open(args.out, "w") as handle:
            json.dump(report, handle, indent=2, default=str)
    print(f"panel n_rows={full.height} n_sym={full['symbol'].n_unique()} "
          f"load={report['panel_load_s']}s eval={report['eval_s']}s", flush=True)
    print(f"# BASELINE trailing_rv self-IC(fwdRV) = {base['raw_ic']:+.4f} (nw_t {base['nw_t']:+.2f}) "
          f"<- the persistence bar every candidate must beat (incrementally)", flush=True)
    print("# feature                     raw_ic  shuffle   nw_t   INCR_ic incr_t collapse  n_rows", flush=True)
    for r in rows:
        coll = r["collapse"] if r["collapse"] == r["collapse"] else float("nan")
        print(f"{r['feature']:<28} {r['raw_ic']:+.4f} {r['shuffle_ic']:+.4f} {r['nw_t']:+6.2f} "
              f"{r['incr_ic']:+.4f} {r['incr_nw_t']:+6.2f} {coll:7.3f} {r['n_rows']}", flush=True)


if __name__ == "__main__":
    main()
