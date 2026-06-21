"""G0 net-of-cost $-screen for the vol-predictability edge — the straddle expression.

The standing G0 discipline: a CHEAP throwaway-proxy screen of whether the predicted-vol edge clears
NET-OF-COST in the chosen tradeable expression, BEFORE proposing any build. The binding constraint is
net-of-cost $, NOT IC.

EXPRESSION = a long ATM straddle on the entry minute, held H bars, on names the predictor flags as
forward-HIGH-vol. A straddle's payoff at horizon H is ~ |forward move| (the realized absolute return),
and it COSTS the option premium (which a rational vol-seller prices off expected vol) plus the options
round-trip (wide bid/ask). So the per-entry net, in return units, is:

    net = realized_|move|  -  premium  -  round_trip_cost

PREMIUM PROXY (throwaway, no historical option-price backfill needed for G0): a vol-seller prices the
straddle off the EXPECTED move over the horizon. We use the name's TRAILING realized vol scaled to the
horizon as that expected-move proxy: premium ≈ k * trailing_rv * sqrt(H). If our predictor only restates
"vol clusters", the realized move == the premium in expectation and net <= 0 after cost. An edge exists
ONLY if, on the predictor-SELECTED names, realized move beats the premium by more than the round-trip —
i.e. the predictor finds entries where forward vol will EXCEED what trailing vol implies (the incremental
content), net of the options round-trip.

ROUND-TRIP: ATM options for liquid underlyings trade ~3-10% of premium per side; we sweep a round-trip
haircut over {2,5,10,15}% of premium. (A real build would measure it from the option quote tape.)

We report the $-CURVE: for each predictor and each top-quantile cut, the MEDIAN and MEAN per-entry net
(in bps of underlying notional) + win-rate, vs the round-trip sweep, AND vs a random-selection baseline.
MEDIAN (not mean) is the tradeability gate — a positive mean carried by a fat right tail is not tradeable
(the #197 lesson). Honest verdict: does ANY predictor+cut clear median-net-positive after a realistic
round-trip? If not -> the vol edge does not monetize in the straddle expression at our cost (a DIFFERENT
null than direction: we'd have actually tested the vol lane).
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import math
import os

import numpy as np
import polars as pl

STORE = os.environ.get("STORE_ROOT", "/store")
MIN_DOLLAR_VOL = 50_000.0
MIN_PRICE = 1.0
SAMPLE_HM_UTC = [(13, 35), (14, 5), (14, 35), (15, 5), (15, 35), (16, 5), (16, 35),
                 (17, 5), (17, 35), (18, 5), (18, 35), (19, 5), (19, 35)]

# the predictors to monetize: the trailing-vol persistence baseline + the incremental anticipators the
# signal screen surfaced (vol term-structure + spread). Each ranks names -> select top-quantile.
PREDICTOR_GROUPS = {
    "volatility": ["realized_vol_5m", "realized_vol_30m", "realized_vol_60m"],
    "quote_spread": ["spread_bps_15m"],
}
PREDICTORS = ["trailing_rv", "realized_vol_5m", "realized_vol_30m", "realized_vol_60m", "spread_bps_15m"]
CUTS = [0.05, 0.10, 0.20]                 # top-quantile selected as 'predicted high vol'
ROUND_TRIP_FRAC = [0.02, 0.05, 0.10, 0.15]  # options round-trip as a fraction of premium
PREMIUM_K = 0.8  # straddle break-even multiple: ATM straddle ~ 0.8*sigma*sqrt(H) expected |move| to BE


def _raw_dates(start: str, end: str) -> list[str]:
    dates = {os.path.basename(p).replace("date=", "")
             for p in glob.glob(f"{STORE}/raw/bars/symbol=*/date=*")}
    return sorted(d for d in dates if start <= d <= end)


def _group_vdir(group: str) -> str:
    return sorted(glob.glob(f"{STORE}/group={group}/v=*"))[-1]


def _load_group_at(date_iso: str, group: str, feats: list[str], sample_ts: pl.Series):
    files = sorted(glob.glob(f"{_group_vdir(group)}/source=backfill/date={date_iso}/data*.parquet"))
    if not files:
        return None
    frame = pl.concat([pl.read_parquet(p, columns=["symbol", "minute"] + feats) for p in files])
    return frame.filter(pl.col("minute").is_in(sample_ts)).unique(subset=["symbol", "minute"])


def _build_date(date_iso: str, horizon_bars: int, trailing_bars: int):
    pattern = f"{STORE}/raw/bars/symbol=*/date={date_iso}/*.parquet"
    if not glob.glob(pattern):
        return None
    bars = (pl.scan_parquet(pattern, hive_partitioning=True)
            .select(["symbol", "ts", "close", "volume"]).collect().sort(["symbol", "ts"]))
    if bars.height == 0:
        return None
    bars = bars.with_columns(
        (pl.col("close").log() - pl.col("close").shift(1).over("symbol").log()).alias("logret"))
    day = dt.date.fromisoformat(date_iso)
    sample_ts = pl.Series([dt.datetime(day.year, day.month, day.day, h, m, tzinfo=dt.timezone.utc)
                           for (h, m) in SAMPLE_HM_UTC])
    bars = bars.with_columns(
        pl.col("logret").rolling_std(window_size=trailing_bars).over("symbol").alias("trailing_rv"),
        (pl.col("close").shift(-horizon_bars).over("symbol") / pl.col("close") - 1.0).abs().alias("fwd_absmove"),
        (pl.col("close") * pl.col("volume")).alias("dollar_vol"))
    entry = bars.filter(pl.col("ts").is_in(sample_ts)).rename({"ts": "minute"})
    entry = entry.filter((pl.col("close") >= MIN_PRICE) & (pl.col("dollar_vol") >= MIN_DOLLAR_VOL)
                         & pl.col("trailing_rv").is_finite() & pl.col("fwd_absmove").is_finite()
                         ).select(["symbol", "minute", "dollar_vol", "trailing_rv", "fwd_absmove"])
    if entry.height == 0:
        return None
    for group, feats in PREDICTOR_GROUPS.items():
        gframe = _load_group_at(date_iso, group, feats, sample_ts)
        if gframe is None:
            return None
        entry = entry.join(gframe, on=["symbol", "minute"], how="left")
    return entry


def _net_bps_for_selection(realized: np.ndarray, premium: np.ndarray, rt_frac: float,
                           side: str = "buy") -> dict[str, float]:
    """Per-entry straddle net in bps of underlying. BUY: realized |move| - premium - round_trip. SELL:
    premium - realized |move| - round_trip (the vol-seller harvests the premium, pays realized + cost)."""
    rt = premium * rt_frac
    if side == "sell":
        net = (premium - realized - rt) * 1e4
    else:
        net = (realized - premium - rt) * 1e4
    return {"median_net_bps": float(np.median(net)), "mean_net_bps": float(np.mean(net)),
            "win_rate": float(np.mean(net > 0)), "n": int(net.size)}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2026-05-29")
    parser.add_argument("--end", default="2026-06-18")
    parser.add_argument("--universe-top", type=int, default=200)
    parser.add_argument("--horizon-bars", type=int, default=30)
    parser.add_argument("--trailing-bars", type=int, default=30)
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

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

    # premium proxy: a straddle priced off TRAILING vol scaled to the horizon (what a vol-seller charges).
    horizon_scale = math.sqrt(args.horizon_bars)
    full = full.with_columns((PREMIUM_K * pl.col("trailing_rv") * horizon_scale).alias("premium"))

    minutes = full["minute"].dt.timestamp("ns").to_numpy()
    realized = full["fwd_absmove"].to_numpy().astype(float)
    premium = full["premium"].to_numpy().astype(float)
    rng = np.random.default_rng(13)

    out = {"window": [args.start, args.end], "horizon_bars": args.horizon_bars,
           "trailing_bars": args.trailing_bars, "n_rows": full.height,
           "premium_k": PREMIUM_K, "curves": {}}

    # ALL-ENTRIES baselines: buying / selling every straddle (no selection) — the unconditional P&L.
    for rt in ROUND_TRIP_FRAC:
        out["curves"].setdefault("BUY_ALL", {})[f"rt{int(rt*100)}"] = _net_bps_for_selection(realized, premium, rt, "buy")
        out["curves"].setdefault("SELL_ALL", {})[f"rt{int(rt*100)}"] = _net_bps_for_selection(realized, premium, rt, "sell")

    # VOL-SELLER side: sell straddles on the predicted-LOWEST-vol names (bottom cut) — does forecasting
    # low forward vol let a seller harvest premium net of realized + round-trip? (the structural-VRP side)
    for predictor in PREDICTORS:
        col = full[predictor].to_numpy().astype(float)
        for cut in CUTS:
            sel = np.zeros(col.size, dtype=bool)
            for m in np.unique(minutes):
                mask = (minutes == m) & np.isfinite(col)
                idx = np.where(mask)[0]
                if idx.size < 20:
                    continue
                k = max(1, int(cut * idx.size))
                bottom = idx[np.argsort(col[idx])[:k]]  # predicted-LOW vol
                sel[bottom] = True
            valid = sel & np.isfinite(realized) & np.isfinite(premium)
            if valid.sum() < 30:
                continue
            for rt in ROUND_TRIP_FRAC:
                out["curves"].setdefault(f"SELL_{predictor}_bot{int(cut*100)}", {})[f"rt{int(rt*100)}"] = \
                    _net_bps_for_selection(realized[valid], premium[valid], rt, "sell")

    for predictor in PREDICTORS:
        col = full[predictor].to_numpy().astype(float)
        # rank within each timestamp; select the top-cut as 'predicted high vol'
        for cut in CUTS:
            sel = np.zeros(col.size, dtype=bool)
            for m in np.unique(minutes):
                mask = (minutes == m) & np.isfinite(col)
                idx = np.where(mask)[0]
                if idx.size < 20:
                    continue
                k = max(1, int(cut * idx.size))
                top = idx[np.argsort(col[idx])[-k:]]
                sel[top] = True
            valid = sel & np.isfinite(realized) & np.isfinite(premium)
            if valid.sum() < 30:
                continue
            for rt in ROUND_TRIP_FRAC:
                res = _net_bps_for_selection(realized[valid], premium[valid], rt)
                out["curves"].setdefault(f"{predictor}_top{int(cut*100)}", {})[f"rt{int(rt*100)}"] = res
        # random-selection control at the 10% cut (does the predictor beat random?)
        sel = np.zeros(col.size, dtype=bool)
        for m in np.unique(minutes):
            idx = np.where(minutes == m)[0]
            if idx.size < 20:
                continue
            k = max(1, int(0.10 * idx.size))
            sel[rng.choice(idx, size=k, replace=False)] = True
        valid = sel & np.isfinite(realized) & np.isfinite(premium)
        if valid.sum() >= 30:
            out["curves"].setdefault(f"RANDOM_top10_seed13", {})["rt5"] = _net_bps_for_selection(
                realized[valid], premium[valid], 0.05)

    if args.out:
        with open(args.out, "w") as handle:
            json.dump(out, handle, indent=2, default=str)
    print(f"G0 straddle screen n_rows={full.height} horizon={args.horizon_bars}bars premium_k={PREMIUM_K}", flush=True)
    print("# selection            rt%   median_net_bps  mean_net_bps  win_rate   n", flush=True)
    for name, by_rt in out["curves"].items():
        for rtk, res in by_rt.items():
            print(f"{name:<22} {rtk:<5} {res['median_net_bps']:+13.2f} {res['mean_net_bps']:+13.2f} "
                  f"{res['win_rate']:8.3f} {res['n']}", flush=True)


if __name__ == "__main__":
    main()
