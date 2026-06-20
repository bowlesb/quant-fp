"""DEEP S-INTRADAY stream builder (Option A, pre-registered). The intraday-family strategy over the full
2018-2025 span = daily-rebalanced decile L/S on the L2+L3 composite, P&L per day (later aggregated to weekly
in the combination screen to align with S-WEEKLY).

Per trading day, for the point-in-time top-N ADV universe:
  - L2 = dc_resp_chunk_slope (swing_dc magnitude) at the day's close, via the Rust kernel on that day's minute
    bars (trades/spread passed as 0 — the magnitude feature is pure price-path, deep-computable; verified).
  - L3 = the path/vol composite = standardized trailing-vol + range + multi-horizon returns block (from the
    daily-aggregated bars); the screen reduces it to PC1 per train fold (parameter-free, fit on train only).
    Here we EMIT the raw L3 block columns; PC1 is computed in the screen (no look-ahead).
  - y_fwd = forward 1-day tradeable return (enter next day >=09:35 ET open, exit the following day's open).
  - half_spread_bps = Stage-1 realized cost at the next-day entry where the quote tape exists, else null
    (screen falls back to a conservative bar proxy on deep dates).

Writes intraday_daily_panel.parquet: (day, symbol, L2, L3-block..., y_fwd_1d, half_spread_bps). READ-ONLY.
Vectorized daily-reduce STREAMED to disk (the #287 memory lesson). ET-anchored Int32-cast.
"""

from __future__ import annotations

import datetime as dt
import glob
import os
from zoneinfo import ZoneInfo

import numpy as np
import polars as pl

from quantlib.data.realized_cost import realized_half_spread_bps
from quantlib.features.groups.swing_dc import swing_dc_fold_frame
from quantlib.features.raw_loaders import load_raw_minute_agg

STORE = os.environ.get("STORE_ROOT", "/store")
OUT_DIR = "/app/experiments/2026-06-20-portfolio-combination"
SPAN_START = os.environ.get("SPAN_START", "2018-01-01")
SPAN_END = os.environ.get("SPAN_END", "2025-12-31")
N_SYMBOLS = int(os.environ.get("N_SYMBOLS", "500"))
VOL_DAYS = 20
ENTRY_ET_MIN = 9 * 60 + 35
MIN_PRICE = 1.0
RET_HORIZONS = (5, 10, 20)  # trailing daily-return horizons for the L3 block (deep, bar-derived)


def list_days() -> list[str]:
    days = sorted(p.split("date=")[1].split("/")[0] for p in glob.glob(f"{STORE}/raw/bars/symbol=SPY/date=*"))
    return [d for d in days if SPAN_START <= d <= SPAN_END]


def daily_reduce(day: str) -> pl.DataFrame:
    """Per-symbol RTH last-close, dollar-vol, tradeable entry, AND the day's swing_dc close score (L2) —
    computed ONLY on the day's top-N-by-dollar-vol universe (swing_dc on ~7000 syms/day is the bottleneck;
    we rank cheaply first, then fold only the universe)."""
    if not glob.glob(f"{STORE}/raw/bars/symbol=*/date={day}"):
        return pl.DataFrame()
    et = pl.col("ts").dt.convert_time_zone("America/New_York")
    etm = et.dt.hour().cast(pl.Int32) * 60 + et.dt.minute().cast(pl.Int32)
    raw = (
        pl.scan_parquet(f"{STORE}/raw/bars/symbol=*/date={day}/*.parquet", hive_partitioning=True)
        .select(["symbol", "ts", "close", "volume"])
        .with_columns(etm.alias("_etm"))
        .filter((pl.col("_etm") >= 9 * 60 + 30) & (pl.col("_etm") < 16 * 60))
        .collect()
    )
    if raw.height == 0:
        return pl.DataFrame()
    agg = raw.group_by("symbol").agg(
        pl.col("close").last().alias("close"),
        (pl.col("close") * pl.col("volume")).sum().alias("dvol"),
        pl.col("close").filter(pl.col("_etm") >= ENTRY_ET_MIN).first().alias("entry"),
    )
    # take a generous top universe (2x N_SYMBOLS so per-day ADV-rank below has headroom) for the swing_dc fold
    universe = set(agg.sort("dvol", descending=True).head(2 * N_SYMBOLS)["symbol"].to_list())
    fold_in = (
        raw.filter(pl.col("symbol").is_in(universe))
        .sort(["symbol", "ts"])
        .rename({"ts": "minute"})
        .select(["symbol", "minute", "close"])
        .with_columns(pl.lit(0.0).alias("n_trades"), pl.lit(0.0).alias("mean_spread_bps"))
    )
    fold = swing_dc_fold_frame(fold_in)
    l2 = (
        fold.sort(["symbol", "minute"]).group_by("symbol").last().select(
            ["symbol", pl.col("dc_resp_chunk_slope").alias("L2_chunk_slope")]
        )
    )
    return agg.join(l2, on="symbol", how="left")


def build() -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    days = list_days()
    print(f"trading days {len(days)} {days[0]}..{days[-1]}", flush=True)
    di = {d: k for k, d in enumerate(days)}
    reduce_dir = f"{OUT_DIR}/_intraday_reduce"
    os.makedirs(reduce_dir, exist_ok=True)
    for stale in glob.glob(f"{reduce_dir}/*.parquet"):
        os.remove(stale)
    for i, day in enumerate(days):
        red = daily_reduce(day)
        if red.height:
            red.with_columns(pl.lit(di[day]).cast(pl.Int32).alias("didx")).write_parquet(
                f"{reduce_dir}/d{di[day]:05d}.parquet"
            )
        if (i + 1) % 200 == 0:
            print(f"  reduced {i+1}/{len(days)} days", flush=True)
    daily = pl.scan_parquet(f"{reduce_dir}/*.parquet").sort(["symbol", "didx"]).collect()
    print(f"daily frame: {daily.height} rows, {daily['symbol'].n_unique()} symbols", flush=True)

    # full calendar grid per symbol for gap-safe trailing/forward shifts
    grid = pl.DataFrame({"didx": list(range(len(days)))}, schema={"didx": pl.Int32})
    syms = daily["symbol"].unique().to_list()
    full = (
        pl.DataFrame({"symbol": syms}, schema={"symbol": pl.Utf8})
        .join(grid, how="cross")
        .join(daily, on=["symbol", "didx"], how="left")
        .sort(["symbol", "didx"])
    )
    full = full.with_columns(
        (pl.col("close") / pl.col("close").shift(1).over("symbol")).log().alias("_lr"),
        pl.col("dvol").rolling_mean(VOL_DAYS, min_periods=VOL_DAYS // 2).over("symbol").alias("adv20"),
        pl.col("entry").shift(-1).over("symbol").alias("entry_next"),  # next-day tradeable entry
        pl.col("entry").shift(-2).over("symbol").alias("entry_exit"),  # exit the day after
    )
    full = full.with_columns(
        pl.col("_lr").rolling_std(VOL_DAYS, min_periods=5).over("symbol").alias("L3_vol20"),
        *[
            (pl.col("close") / pl.col("close").shift(h).over("symbol") - 1.0).alias(f"L3_ret{h}d")
            for h in RET_HORIZONS
        ],
        ((pl.col("close").rolling_max(VOL_DAYS).over("symbol") - pl.col("close").rolling_min(VOL_DAYS).over("symbol")) / pl.col("close")).alias("L3_range20"),
    )
    # rebalance EVERY trading day (daily-rebalanced intraday-family stream)
    obs = full.filter(
        pl.col("close").is_not_null() & (pl.col("close") >= MIN_PRICE) & pl.col("adv20").is_not_null()
        & pl.col("L2_chunk_slope").is_not_null() & pl.col("L3_vol20").is_not_null()
    ).with_columns(
        pl.col("adv20").rank(method="ordinal", descending=True).over("didx").alias("_advrank")
    ).filter(pl.col("_advrank") <= N_SYMBOLS)
    obs = obs.with_columns(
        pl.when((pl.col("entry_next") >= MIN_PRICE) & (pl.col("entry_exit") >= MIN_PRICE))
        .then(pl.col("entry_exit") / pl.col("entry_next") - 1.0)
        .otherwise(None)
        .alias("y_fwd_1d"),
        pl.col("didx").replace_strict({k: d for d, k in di.items()}, return_dtype=pl.Utf8).alias("day"),
    )

    # Stage-1 realized cost at the next-day entry where quotes exist (recent dates only; null deep).
    idx_to_day = {k: d for d, k in di.items()}
    hs_rows = []
    rebal_days = sorted(obs["didx"].unique().to_list())
    for n_done, didx in enumerate(rebal_days):
        nxt = didx + 1
        if nxt not in idx_to_day:
            continue
        entry_day = idx_to_day[nxt]
        u = obs.filter(pl.col("didx") == didx)["symbol"].to_list()
        d0 = dt.date.fromisoformat(entry_day)
        ets = dt.datetime(d0.year, d0.month, d0.day, ENTRY_ET_MIN // 60, ENTRY_ET_MIN % 60,
                          tzinfo=ZoneInfo("America/New_York")).astimezone(dt.timezone.utc)
        rc = realized_half_spread_bps(STORE, entry_day, u, ets)
        if rc.height:
            hs_rows.append(rc.with_columns(pl.lit(didx).cast(pl.Int32).alias("didx")))
        if (n_done + 1) % 200 == 0:
            print(f"  realized-cost {n_done+1}/{len(rebal_days)} days ({sum(r.height for r in hs_rows)} rows)", flush=True)
    hs = pl.concat(hs_rows, how="vertical") if hs_rows else pl.DataFrame(
        schema={"symbol": pl.Utf8, "realized_half_spread_bps": pl.Float64, "didx": pl.Int32}
    )
    l3_cols = ["L3_vol20", "L3_range20"] + [f"L3_ret{h}d" for h in RET_HORIZONS]
    panel = (
        obs.join(hs, on=["symbol", "didx"], how="left")
        .rename({"realized_half_spread_bps": "half_spread_bps"})
        .select(["day", "symbol", "L2_chunk_slope", *l3_cols, "y_fwd_1d", "half_spread_bps"])
    )
    out = f"{OUT_DIR}/intraday_daily_panel.parquet"
    panel.write_parquet(out)
    print(
        f"WROTE {out}: {panel.height} obs, {panel['day'].n_unique()} days, {panel['symbol'].n_unique()} syms, "
        f"realized-cost rows={int(panel['half_spread_bps'].is_not_null().sum())}",
        flush=True,
    )


if __name__ == "__main__":
    build()
