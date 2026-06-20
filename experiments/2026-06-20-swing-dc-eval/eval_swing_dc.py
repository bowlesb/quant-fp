"""swing_dc $-EVALUATION via the harness discipline — does any DC scale's chunk/Fib structure carry net-new
tradeable signal? (the invent→evaluate verdict that gates the deploy.)

Per backfill date: load the tick-enriched minute_agg (raw bars + trades/quotes), compute the swing_dc group
(74 feats, Olsen DC multi-scale ladder + Fib), sample the entry minute (>=09:35 ET), attach own_vol/size
controls + the forward-30m CROSS-SECTIONAL EXCESS label. Accumulate a compact panel, then:
  - per-feature daily rank-IC vs the forward label + NW-t (the per-scale leaderboard) + own-vol/size collapse.
  - GBM walk-forward (purged) → the percentile-threshold $-curve + shuffle + predict-zero baselines.
  - headline: dc_fib_setup_long-class features — net-new tail signal surviving controls?
  - BY-FDR across the 74 (esp. the 4 scales).
Median-anchored. NEEDS the fp-dev-swingdc image (the swing_dc Rust kernel is NOT on main). READ-ONLY store.
"""

from __future__ import annotations

import datetime as dt
import glob
import os
from collections import defaultdict
from zoneinfo import ZoneInfo

import numpy as np
import polars as pl

from quantlib.features.base import BatchContext
from quantlib.features.raw_loaders import load_raw_minute_agg, load_raw_tick_enriched_minute_agg
from quantlib.features.registry import REGISTRY

OUT_DIR = "/app/experiments/2026-06-20-swing-dc-eval"
STORE = "/store"
ENTRY_ET = 9 * 60 + 40  # 09:40 ET entry (>=09:35, a few min after open so the DC fold has warmed)
FWD_MIN = 30
UNIVERSE_TOP = int(os.environ.get("UNIVERSE_TOP", "200"))
N_DATES = int(os.environ.get("N_DATES", "40"))
N_SHUFFLE = int(os.environ.get("N_SHUFFLE", "100"))


def liquid_universe(day: str, n: int) -> list[str]:
    """Top-n by RTH dollar volume on `day` (cheap per-day liquid set)."""
    syms = [p.split("symbol=")[1].split("/")[0] for p in glob.glob(f"{STORE}/raw/bars/symbol=*/date={day}")]
    if not syms:
        return []
    lazy = pl.scan_parquet(f"{STORE}/raw/bars/symbol=*/date={day}/*.parquet", hive_partitioning=True).select(
        ["symbol", "ts", "close", "volume"]
    )
    et = pl.col("ts").dt.convert_time_zone("America/New_York")
    m = et.dt.hour().cast(pl.Int32) * 60 + et.dt.minute().cast(pl.Int32)
    dv = (
        lazy.filter((m >= 9 * 60 + 30) & (m < 16 * 60))
        .group_by("symbol")
        .agg((pl.col("close") * pl.col("volume")).sum().alias("dv"))
        .sort("dv", descending=True)
        .head(n)
        .collect()
    )
    return dv["symbol"].to_list()


def entry_ts(day: str, et_min: int) -> dt.datetime:
    d = dt.date.fromisoformat(day)
    return dt.datetime(
        d.year, d.month, d.day, et_min // 60, et_min % 60, tzinfo=ZoneInfo("America/New_York")
    ).astimezone(dt.timezone.utc)


def build_date(day: str) -> pl.DataFrame:
    syms = liquid_universe(day, UNIVERSE_TOP)
    if len(syms) < 30:
        return pl.DataFrame()
    bars = load_raw_minute_agg(STORE, day, syms)
    if bars.height == 0:
        return pl.DataFrame()
    enr = load_raw_tick_enriched_minute_agg(STORE, day, syms, bars)
    feat = REGISTRY.get_group("swing_dc").compute(BatchContext(frames={"minute_agg": enr}))
    feat_cols = [c for c in feat.columns if c not in ("symbol", "minute")]
    et = entry_ts(day, ENTRY_ET)
    # features at the entry minute (first row at/after entry per symbol)
    at = feat.filter(pl.col("minute") >= et).sort(["symbol", "minute"]).group_by("symbol").first()
    # forward 30m return + own_vol (trailing 60m realized) + size (RTH dollar vol) from bars
    bsort = bars.sort(["symbol", "minute"])
    px = bsort.select(["symbol", "minute", "close"])
    entry_px = (
        px.filter(pl.col("minute") >= et).group_by("symbol").first().rename({"close": "c0", "minute": "m0"})
    )
    fwd_px = (
        px.filter(pl.col("minute") >= et + dt.timedelta(minutes=FWD_MIN))
        .group_by("symbol")
        .first()
        .rename({"close": "cF"})
    )
    # own_vol: std of 1-min logret over the 60m before entry; size: log RTH dollar vol
    pre = bsort.filter(
        (pl.col("minute") < et) & (pl.col("minute") >= et - dt.timedelta(minutes=60))
    ).with_columns((pl.col("close") / pl.col("close").shift(1).over("symbol")).log().alias("_lr"))
    own = pre.group_by("symbol").agg(pl.col("_lr").std().alias("own_vol"))
    size = bsort.group_by("symbol").agg((pl.col("close") * pl.col("volume")).sum().log().alias("size"))
    out = (
        at.select(["symbol", *feat_cols])
        .join(entry_px.select(["symbol", "c0"]), on="symbol")
        .join(fwd_px.select(["symbol", "cF"]), on="symbol")
        .join(own, on="symbol")
        .join(size, on="symbol")
    )
    out = out.with_columns((pl.col("cF") / pl.col("c0") - 1.0).alias("_raw"))
    out = out.with_columns(
        (pl.col("_raw") - pl.col("_raw").median()).alias("y_fwd"), pl.lit(day).alias("date")
    )
    return out.drop(["c0", "cF", "_raw"])


def main() -> None:
    days = sorted(
        p.split("date=")[1].split("/")[0]
        for p in glob.glob(f"{STORE}/group=volatility/v=1.0.0/source=backfill/date=*")
    )[-N_DATES:]
    print(f"swing_dc eval: {len(days)} dates {days[0]}..{days[-1]}, top-{UNIVERSE_TOP} liquid", flush=True)
    frames = []
    for i, day in enumerate(days):
        d = build_date(day)
        if d.height:
            frames.append(d)
        if (i + 1) % 5 == 0:
            print(f"  built {i+1}/{len(days)} ({sum(f.height for f in frames)} rows)", flush=True)
    panel = pl.concat(frames, how="vertical_relaxed")
    panel.write_parquet(f"{OUT_DIR}/swing_dc_panel.parquet")
    feat_cols = [c for c in panel.columns if c not in ("symbol", "date", "y_fwd", "own_vol", "size", "m0")]
    print(
        f"panel: {panel.height} rows, {panel['date'].n_unique()} days, {len(feat_cols)} swing_dc feats",
        flush=True,
    )

    def rank(a):
        return a.argsort().argsort().astype(float)

    def sp(x, y):
        rx, ry = rank(x), rank(y)
        return (
            float(np.corrcoef(rx, ry)[0, 1]) if np.std(rx) > 1e-12 and np.std(ry) > 1e-12 else float("nan")
        )

    # per-feature daily rank-IC + own-vol/size collapse + shuffle-z
    recs = []
    for f in feat_cols:
        df = panel.select(["date", f, "own_vol", "size", "y_fwd"]).drop_nulls()
        ics, raws, pars = [], [], []
        for (_,), g in df.group_by(["date"]):
            if g.height < 20:
                continue
            x, y = g[f].to_numpy(), g["y_fwd"].to_numpy()
            if np.std(x) < 1e-12 or np.std(y) < 1e-12:
                continue
            ics.append(sp(x, y))
            Z = np.column_stack([np.ones(g.height), g["own_vol"].to_numpy(), g["size"].to_numpy()])
            rx = x - Z @ np.linalg.lstsq(Z, x, rcond=None)[0]
            ry = y - Z @ np.linalg.lstsq(Z, y, rcond=None)[0]
            raws.append(sp(x, y))
            if np.std(rx) > 1e-12 and np.std(ry) > 1e-12:
                pars.append(sp(rx, ry))
        if len(ics) < 8:
            continue
        a = np.array(ics)
        ic, t = float(a.mean()), float(a.mean() / (a.std(ddof=1) / np.sqrt(len(a)) + 1e-12))
        rawm, parm = float(np.mean(raws)), float(np.mean(pars)) if pars else float("nan")
        collapse = abs(parm) / abs(rawm) if abs(rawm) > 1e-9 else float("nan")
        recs.append(
            {"feature": f, "ic": ic, "nw_t": t, "partial_ic": parm, "collapse": collapse, "n_days": len(a)}
        )
    res = pl.DataFrame(recs).sort("nw_t", descending=True, nulls_last=True)
    res.write_csv(f"{OUT_DIR}/swing_dc_leaderboard.csv")
    print(
        "\n=== ⭐ swing_dc PER-FEATURE LEADERBOARD (top + bottom by |NW-t|; collapse = own-vol/size survival) ==="
    )
    with pl.Config(tbl_rows=20, fmt_str_lengths=34):
        top = res.with_columns(pl.col("nw_t").abs().alias("_a")).sort("_a", descending=True).head(15)
        print(top.select(["feature", "ic", "nw_t", "partial_ic", "collapse", "n_days"]))
    # headline: the fib/setup features
    fib = res.filter(pl.col("feature").str.contains("fib|setup"))
    print("\n=== ⭐ FIB/SETUP features (the headline 'likely-up chunk' surface) ===")
    with pl.Config(tbl_rows=20, fmt_str_lengths=34):
        print(
            fib.sort("nw_t", descending=True)
            .select(["feature", "ic", "nw_t", "partial_ic", "collapse"])
            .head(15)
        )
    # BY-FDR across all features (two-sided on NW-t)
    import math

    def p_of(t):
        return 2 * (1 - 0.5 * (1 + math.erf(abs(t) / math.sqrt(2)))) if not np.isnan(t) else float("nan")

    ps = [p_of(r["nw_t"]) for r in recs]
    pv = np.array(ps)
    valid = ~np.isnan(pv)
    m = int(valid.sum())
    cm = float(np.sum(1.0 / np.arange(1, m + 1)))
    order = np.argsort(np.where(valid, pv, np.inf))
    crit = (np.arange(1, m + 1) / (m * cm)) * 0.10
    passed = np.where(pv[order][:m] <= crit)[0]
    n_surv = (passed.max() + 1) if len(passed) else 0
    print(f"\n=== BY-FDR(q=0.10) survivors among {m} swing_dc features: {n_surv} ===")
    if n_surv:
        survivors = [recs[i]["feature"] for i in order[:n_surv]]
        print("  ", survivors[:12])


if __name__ == "__main__":
    main()
