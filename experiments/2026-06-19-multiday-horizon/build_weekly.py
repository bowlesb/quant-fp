"""MULTI-DAY (WEEKLY) HORIZON — panel builder (pre-registered, see prereg.md).

Builds a weekly-rebalance cross-sectional panel from the deep `fp_store_real` minute bars, aggregated to
daily (RTH last-close + dollar volume, ET-anchored Int32-cast to avoid the #197 Int8-overflow bug). Each
weekly observation = (rebalance_friday, symbol) with:
  - rev_1w  = trailing 5-trading-day return as of the Friday close (the reversal feature),
  - vol_20d = trailing 20-trading-day realized daily-return vol (the low-vol feature),
  - adv20   = trailing 20d mean dollar volume (the liquidity / size proxy),
  - y_fwd_1w = forward 5-trading-day return ENTERED at the FOLLOWING Monday's tradeable open (>=09:35 ET,
    never the Friday close — no close-to-close look-ahead),
  - disappeared = 1 if the name stops printing bars during the forward week (the delisting flag for the
    survivorship haircut).

Survivorship is explicit: the universe each week = the top-N liquid names PRINTING that week (reconstructed
from bars — the only historical universe available; 2026-06-15+ universe_membership is too shallow). The
``disappeared`` flag + the screen's delisting haircut quantify the bias. $1-floor + per-week winsor live in
the screen. READ-ONLY stores. Writes weekly_panel.parquet.

NOTE: this is the turn-key run script for the pre-registered design; the full multi-year run is GATED on the
Lead/Ben greenlight of the surface (a smoke run over a short span validates the pipeline).
"""

from __future__ import annotations

import glob
import os

import numpy as np
import polars as pl

STORE = os.environ.get("STORE_ROOT", "/store")
OUT_DIR = "/app/experiments/2026-06-19-multiday-horizon"

SPAN_START = os.environ.get("SPAN_START", "2018-01-01")
SPAN_END = os.environ.get("SPAN_END", "2025-12-31")
N_SYMBOLS = int(os.environ.get("N_SYMBOLS", "500"))  # top-N liquid names per week
REV_DAYS = 5  # trailing/forward week = 5 trading days
VOL_DAYS = 20  # trailing realized-vol / ADV window
ENTRY_ET_MIN = 9 * 60 + 35  # 09:35 ET tradeable Monday entry (never the Friday close)
MIN_PRICE = 1.0  # $1 floor


def list_days() -> list[str]:
    days = sorted(
        p.split("date=")[1].split("/")[0] for p in glob.glob(f"{STORE}/raw/bars/symbol=SPY/date=*")
    )
    return [d for d in days if SPAN_START <= d <= SPAN_END]


def daily_close_dollarvol(symbol: str, day: str) -> tuple[float, float] | None:
    """RTH last close + RTH dollar volume for one (symbol, day), or None if no RTH bars. ET-anchored."""
    pattern = f"{STORE}/raw/bars/symbol={symbol}/date={day}/*.parquet"
    if not glob.glob(pattern):
        return None
    df = pl.scan_parquet(pattern, hive_partitioning=True).select(["ts", "close", "volume"]).collect()
    if df.height == 0:
        return None
    et = pl.col("ts").dt.convert_time_zone("America/New_York")
    etm = et.dt.hour().cast(pl.Int32) * 60 + et.dt.minute().cast(pl.Int32)
    rth = df.filter((etm >= 9 * 60 + 30) & (etm < 16 * 60)).sort("ts")
    if rth.height == 0:
        return None
    close = float(rth["close"][-1])
    dvol = float((rth["close"] * rth["volume"]).sum())
    return close, dvol


def tradeable_open(symbol: str, day: str) -> float | None:
    """The tradeable entry price = first RTH bar close at/after 09:35 ET on ``day`` (the Monday entry)."""
    pattern = f"{STORE}/raw/bars/symbol={symbol}/date={day}/*.parquet"
    if not glob.glob(pattern):
        return None
    df = pl.scan_parquet(pattern, hive_partitioning=True).select(["ts", "close"]).collect()
    if df.height == 0:
        return None
    et = pl.col("ts").dt.convert_time_zone("America/New_York")
    etm = et.dt.hour().cast(pl.Int32) * 60 + et.dt.minute().cast(pl.Int32)
    at = df.filter(etm >= ENTRY_ET_MIN).sort("ts")
    if at.height == 0:
        return None
    price = float(at["close"][0])
    return price if price >= MIN_PRICE else None


def weekly_rebalance_days(days: list[str]) -> list[tuple[int, str, str]]:
    """Index the trading-day list into (friday_idx, friday_day, next_monday_entry_day) tuples. We rebalance
    every 5 trading days: index i = signal as-of close of days[i], enter at days[i+1]'s tradeable open, hold
    to days[i+REV_DAYS]. Reconstructed purely from the trading-day calendar present in the bars."""
    out = []
    for i in range(VOL_DAYS, len(days) - REV_DAYS - 1, REV_DAYS):
        out.append((i, days[i], days[i + 1]))
    return out


def build() -> None:
    days = list_days()
    print(f"trading days {len(days)} {days[0]}..{days[-1]}", flush=True)
    # Liquid universe = top-N by ADV on a recent reference day in-span (the bars' only historical universe).
    ref = days[len(days) // 2]
    bar_syms = [p.split("symbol=")[1].split("/")[0] for p in glob.glob(f"{STORE}/raw/bars/symbol=*")]
    # NOTE: full run ranks ADV per-week; this stub uses a single mid-span ADV ranking for the smoke pipeline.
    advs = []
    for sym in bar_syms:
        cd = daily_close_dollarvol(sym, ref)
        if cd is not None:
            advs.append((sym, cd[1]))
    advs.sort(key=lambda kv: kv[1], reverse=True)
    universe = [sym for sym, _ in advs[:N_SYMBOLS]]
    print(f"universe={len(universe)} (top-{N_SYMBOLS} ADV @ {ref})", flush=True)

    # Per-symbol daily close series across the span (one scan per symbol).
    closes: dict[str, dict[str, float]] = {}
    for sym in universe:
        series = {}
        for day in days:
            cd = daily_close_dollarvol(sym, day)
            if cd is not None:
                series[day] = cd[0]
        closes[sym] = series

    rebal = weekly_rebalance_days(days)
    rows = []
    for friday_idx, friday, monday in rebal:
        window_prior = days[friday_idx - VOL_DAYS : friday_idx + 1]
        fwd_end = days[friday_idx + 1 + REV_DAYS] if friday_idx + 1 + REV_DAYS < len(days) else None
        for sym in universe:
            series = closes[sym]
            if friday not in series or days[friday_idx - REV_DAYS] not in series:
                continue
            c_fri = series[friday]
            c_revstart = series[days[friday_idx - REV_DAYS]]
            if c_fri < MIN_PRICE or c_revstart < MIN_PRICE:
                continue
            rev_1w = c_fri / c_revstart - 1.0
            prior_closes = [series[d] for d in window_prior if d in series]
            if len(prior_closes) < VOL_DAYS // 2:
                continue
            rets = np.diff(np.log(prior_closes))
            vol_20d = float(np.std(rets)) if len(rets) >= 5 else float("nan")
            entry = tradeable_open(sym, monday)
            disappeared = 1 if (fwd_end is not None and fwd_end not in series) else 0
            y_fwd = None
            if entry is not None and fwd_end is not None and fwd_end in series:
                c_end = series[fwd_end]
                if c_end >= MIN_PRICE:
                    y_fwd = c_end / entry - 1.0
            rows.append(
                {
                    "friday": friday,
                    "year": int(friday[:4]),
                    "symbol": sym,
                    "rev_1w": rev_1w,
                    "vol_20d": vol_20d,
                    "y_fwd_1w": y_fwd,
                    "disappeared": disappeared,
                }
            )
    panel = pl.DataFrame(rows, infer_schema_length=None)
    out = f"{OUT_DIR}/weekly_panel.parquet"
    panel.write_parquet(out)
    n_weeks = panel["friday"].n_unique() if panel.height else 0
    print(
        f"WROTE {out}: {panel.height} obs, {n_weeks} weeks, {panel['symbol'].n_unique() if panel.height else 0} syms, disappeared={panel['disappeared'].sum() if panel.height else 0}",
        flush=True,
    )


if __name__ == "__main__":
    build()
