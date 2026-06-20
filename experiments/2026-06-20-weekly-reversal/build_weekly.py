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

    # VECTORIZED daily reduce across ALL symbols, one day at a time -> a long (symbol, day) frame.
    frames = []
    for i, day in enumerate(days):
        red = daily_reduce(day)
        if red.height:
            frames.append(red.with_columns(pl.lit(day).alias("day")))
        if (i + 1) % 100 == 0:
            print(f"  reduced {i+1}/{len(days)} days", flush=True)
    daily = pl.concat(frames, how="vertical").sort(["symbol", "day"])
    di = {d: k for k, d in enumerate(days)}  # day -> trading-day index
    daily = daily.with_columns(pl.col("day").replace_strict(di, return_dtype=pl.Int32).alias("didx"))
    print(f"daily frame: {daily.height} (symbol,day) rows, {daily['symbol'].n_unique()} symbols", flush=True)

    # Resident per-symbol arrays for the weekly logic (indexed by trading-day index).
    close_by: dict[str, dict[int, float]] = {}
    dvol_by: dict[str, dict[int, float]] = {}
    entry_by: dict[str, dict[int, float]] = {}
    for sym, grp in daily.group_by("symbol"):
        s = sym[0] if isinstance(sym, tuple) else sym
        idx = grp["didx"].to_list()
        close_by[s] = dict(zip(idx, grp["close"].to_list()))
        dvol_by[s] = dict(zip(idx, grp["dvol"].to_list()))
        entry_by[s] = dict(zip(idx, grp["entry"].to_list()))

    rows = []
    rebal_idx = list(range(VOL_DAYS, len(days) - REV_DAYS - 1, REV_DAYS))
    for n_done, friday_idx in enumerate(rebal_idx):
        friday = days[friday_idx]
        monday_idx, fwd_idx, revstart_idx = friday_idx + 1, friday_idx + 1 + REV_DAYS, friday_idx - REV_DAYS
        monday, fwd_end = days[monday_idx], days[fwd_idx]
        window = list(range(friday_idx - VOL_DAYS, friday_idx + 1))
        # POINT-IN-TIME universe: trailing-20d mean dollar-vol AS-OF this Friday, top-N.
        cand = []
        for sym, dv in dvol_by.items():
            if friday_idx not in close_by[sym] or revstart_idx not in close_by[sym]:
                continue
            dvs = [dv[w] for w in window if w in dv]
            if len(dvs) >= VOL_DAYS // 2:
                cand.append((sym, float(np.mean(dvs))))
        cand.sort(key=lambda kv: kv[1], reverse=True)
        universe = [s for s, _ in cand[:N_SYMBOLS]]
        et = entry_ts(monday)
        realized = realized_half_spread_bps(STORE, monday, universe, et)
        rc_map = dict(zip(realized["symbol"].to_list(), realized["realized_half_spread_bps"].to_list())) if realized.height else {}
        for sym in universe:
            cb, db = close_by[sym], dvol_by[sym]
            c_fri, c_revstart = cb[friday_idx], cb[revstart_idx]
            if c_fri < MIN_PRICE or c_revstart < MIN_PRICE:
                continue
            rev_1w = c_fri / c_revstart - 1.0
            prior = [cb[w] for w in window if w in cb]
            if len(prior) < VOL_DAYS // 2:
                continue
            rets = np.diff(np.log(prior))
            vol_20d = float(np.std(rets)) if len(rets) >= 5 else float("nan")
            log_adv = float(np.log(np.mean([db[w] for w in window if w in db]) + 1.0))
            entry = entry_by[sym].get(monday_idx)
            exit_px = entry_by[sym].get(fwd_idx)
            disappeared = 1 if fwd_idx not in cb else 0
            y_fwd = None
            if entry is not None and entry >= MIN_PRICE and exit_px is not None and exit_px >= MIN_PRICE:
                y_fwd = exit_px / entry - 1.0
            rows.append(
                {
                    "friday": friday,
                    "year": int(friday[:4]),
                    "symbol": sym,
                    "rev_1w": rev_1w,
                    "vol_20d": vol_20d,
                    "log_adv": log_adv,
                    "y_fwd_1w": y_fwd,
                    "half_spread_bps": rc_map.get(sym),
                    "disappeared": disappeared,
                }
            )
        if (n_done + 1) % 20 == 0:
            print(f"  {n_done+1}/{len(rebal_idx)} weeks built ({len(rows)} rows)", flush=True)

    panel = pl.DataFrame(rows, infer_schema_length=None)
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
