"""WEEKLY REVERSAL — panel builder (pre-registered, see PRE_REGISTRATION.md; Lead-approved run).

Upgrades the #205 stub to the approved design:
  - POINT-IN-TIME per-week top-N ADV universe (fixes the old single-mid-span-day ADV look-ahead+survivorship
    bug): each rebalance Friday, rank names by trailing-20d mean dollar-volume AS-OF that Friday, take top-N.
  - rev_1w = trailing 5-trading-day return as-of the Friday close (the reversal feature).
  - vol_20d (trailing realized vol) + log-adv (size) as the own-vol/size CONTROLS.
  - y_fwd_1w = forward 5-trading-day return ENTERED at the FOLLOWING Monday tradeable open >=09:35 ET
    (never the Friday close — no close-to-close look-ahead), exited at the next Friday's tradeable open.
  - realized_half_spread_bps at the Monday entry instant (Stage-1 measured cost) where the quote tape exists;
    null elsewhere (the screen uses a conservative bar proxy when absent).
  - disappeared = 1 if the name stops printing during the forward week (in this survivors-only panel ~0, by
    construction — the screen's CALIBRATED haircut, not this flag, carries the survivorship gate).

READ-ONLY stores. Writes weekly_panel.parquet. ET-anchored Int32-cast (the #197 Int8-overflow guard).
"""

from __future__ import annotations

import datetime as dt
import glob
import os
from zoneinfo import ZoneInfo

import numpy as np
import polars as pl

from quantlib.data.realized_cost import realized_half_spread_bps

STORE = os.environ.get("STORE_ROOT", "/store")
OUT_DIR = "/app/experiments/2026-06-20-weekly-reversal"
SPAN_START = os.environ.get("SPAN_START", "2018-01-01")
SPAN_END = os.environ.get("SPAN_END", "2025-12-31")
N_SYMBOLS = int(os.environ.get("N_SYMBOLS", "1000"))
REV_DAYS = 5
VOL_DAYS = 20
ENTRY_ET_MIN = 9 * 60 + 35
MIN_PRICE = 1.0


def list_days() -> list[str]:
    days = sorted(p.split("date=")[1].split("/")[0] for p in glob.glob(f"{STORE}/raw/bars/symbol=SPY/date=*"))
    return [d for d in days if SPAN_START <= d <= SPAN_END]


def daily_reduce(day: str) -> pl.DataFrame:
    """VECTORIZED across ALL symbols for one day: per-symbol RTH last-close, RTH dollar-vol, and the tradeable
    entry price (first RTH close >= 09:35 ET). One lazy glob (hive pushdown) — far faster than per-symbol scans.
    Returns (symbol, close, dvol, entry) for that day; empty frame if no bars."""
    pattern = f"{STORE}/raw/bars/symbol=*/date={day}/*.parquet"
    if not glob.glob(f"{STORE}/raw/bars/symbol=*/date={day}"):
        return pl.DataFrame()
    et = pl.col("ts").dt.convert_time_zone("America/New_York")
    etm = et.dt.hour().cast(pl.Int32) * 60 + et.dt.minute().cast(pl.Int32)
    lazy = (
        pl.scan_parquet(pattern, hive_partitioning=True)
        .select(["symbol", "ts", "close", "volume"])
        .with_columns(etm.alias("_etm"))
        .filter((pl.col("_etm") >= 9 * 60 + 30) & (pl.col("_etm") < 16 * 60))
        .sort(["symbol", "ts"])
    )
    out = lazy.group_by("symbol").agg(
        pl.col("close").last().alias("close"),
        (pl.col("close") * pl.col("volume")).sum().alias("dvol"),
        pl.col("close").filter(pl.col("_etm") >= ENTRY_ET_MIN).first().alias("entry"),
    )
    return out.collect()


def entry_ts(day: str) -> dt.datetime:
    d = dt.date.fromisoformat(day)
    return dt.datetime(
        d.year, d.month, d.day, ENTRY_ET_MIN // 60, ENTRY_ET_MIN % 60, tzinfo=ZoneInfo("America/New_York")
    ).astimezone(dt.timezone.utc)


def build() -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    days = list_days()
    print(f"trading days {len(days)} {days[0]}..{days[-1]}", flush=True)

    di = {d: k for k, d in enumerate(days)}  # day -> trading-day index
    # VECTORIZED daily reduce, STREAMED to disk one day at a time (memory-bounded regardless of span — the
    # accumulate-all-frames-in-RAM version OOMs at multi-year scale). Each day -> one parquet, scanned lazily.
    reduce_dir = f"{OUT_DIR}/_daily_reduce"
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
    print(f"daily frame: {daily.height} (symbol,day) rows, {daily['symbol'].n_unique()} symbols", flush=True)

    # Per-symbol gap-safe trailing/forward features via window functions (a row exists per traded day; the
    # didx alignment below requires a CONTIGUOUS calendar, so reindex each symbol onto the full day grid so a
    # 5-back / 5-forward shift is exactly one trading week regardless of missing days).
    grid = pl.DataFrame({"didx": list(range(len(days)))}, schema={"didx": pl.Int32})
    syms = daily["symbol"].unique().to_list()
    full = (
        pl.DataFrame({"symbol": syms}, schema={"symbol": pl.Utf8})
        .join(grid, how="cross")
        .join(daily, on=["symbol", "didx"], how="left")
        .sort(["symbol", "didx"])
    )
    full = full.with_columns(
        pl.col("close").shift(REV_DAYS).over("symbol").alias("c_rev5"),
        pl.col("dvol").rolling_mean(window_size=VOL_DAYS, min_periods=VOL_DAYS // 2).over("symbol").alias("adv20"),
        (pl.col("close") / pl.col("close").shift(1).over("symbol")).log().alias("_lr"),
    )
    full = full.with_columns(
        pl.col("_lr").rolling_std(window_size=VOL_DAYS, min_periods=5).over("symbol").alias("vol_20d"),
        pl.col("entry").shift(-1).over("symbol").alias("entry_mon"),  # next-day tradeable Monday open
        pl.col("entry").shift(-(1 + REV_DAYS)).over("symbol").alias("entry_exit"),  # exit Friday tradeable open
        pl.col("close").shift(-(1 + REV_DAYS)).over("symbol").alias("c_fwd_end"),  # for disappeared flag
    )
    # Rebalance Fridays = every REV_DAYS-th index from VOL_DAYS up to the last full forward week.
    rebal = set(range(VOL_DAYS, len(days) - REV_DAYS - 1, REV_DAYS))
    obs = full.filter(
        pl.col("didx").is_in(rebal)
        & pl.col("close").is_not_null()
        & pl.col("c_rev5").is_not_null()
        & (pl.col("close") >= MIN_PRICE)
        & (pl.col("c_rev5") >= MIN_PRICE)
        & pl.col("adv20").is_not_null()
    ).with_columns(
        (pl.col("close") / pl.col("c_rev5") - 1.0).alias("rev_1w"),
        (pl.col("adv20") + 1.0).log().alias("log_adv"),
        # POINT-IN-TIME universe rank: dense rank by adv20 WITHIN each rebalance Friday, keep top-N.
        pl.col("adv20").rank(method="ordinal", descending=True).over("didx").alias("_adv_rank"),
    ).filter(pl.col("_adv_rank") <= N_SYMBOLS)
    obs = obs.with_columns(
        pl.when((pl.col("entry_mon") >= MIN_PRICE) & (pl.col("entry_exit") >= MIN_PRICE))
        .then(pl.col("entry_exit") / pl.col("entry_mon") - 1.0)
        .otherwise(None)
        .alias("y_fwd_1w"),
        pl.col("c_fwd_end").is_null().cast(pl.Int8).alias("disappeared"),
        pl.col("didx").replace_strict({k: d for d, k in di.items()}, return_dtype=pl.Utf8).alias("friday"),
    ).with_columns(pl.col("friday").str.slice(0, 4).cast(pl.Int32).alias("year"))

    # Stage-1 realized half-spread at each rebalance Monday for the universe (fetched per rebalance week).
    idx_to_day = {k: d for d, k in di.items()}
    hs_rows = []
    rebal_fridays = sorted(obs["didx"].unique().to_list())
    for n_done, fri in enumerate(rebal_fridays):
        monday = idx_to_day[fri + 1]
        u = obs.filter(pl.col("didx") == fri)["symbol"].to_list()
        rc = realized_half_spread_bps(STORE, monday, u, entry_ts(monday))
        if rc.height:
            hs_rows.append(rc.with_columns(pl.lit(fri).cast(pl.Int32).alias("didx")))
        if (n_done + 1) % 50 == 0:
            print(f"  realized-cost {n_done+1}/{len(rebal_fridays)} weeks", flush=True)
    hs = pl.concat(hs_rows, how="vertical") if hs_rows else pl.DataFrame(
        schema={"symbol": pl.Utf8, "realized_half_spread_bps": pl.Float64, "didx": pl.Int32}
    )
    panel = (
        obs.join(hs, on=["symbol", "didx"], how="left")
        .rename({"realized_half_spread_bps": "half_spread_bps"})
        .select(["friday", "year", "symbol", "rev_1w", "vol_20d", "log_adv", "y_fwd_1w", "half_spread_bps", "disappeared"])
    )
    out = f"{OUT_DIR}/weekly_panel.parquet"
    panel.write_parquet(out)
    n_rc = int(panel["half_spread_bps"].is_not_null().sum()) if panel.height else 0
    print(
        f"WROTE {out}: {panel.height} obs, {panel['friday'].n_unique() if panel.height else 0} weeks, "
        f"{panel['symbol'].n_unique() if panel.height else 0} syms, disappeared={int(panel['disappeared'].sum()) if panel.height else 0}, "
        f"realized-cost rows={n_rc}",
        flush=True,
    )


if __name__ == "__main__":
    build()
