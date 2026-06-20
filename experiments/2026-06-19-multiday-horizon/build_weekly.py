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
CANDIDATE_ADV_FLOOR = 5_000_000.0  # $5M/day dollar-volume floor to count a liquid day
CANDIDATE_MIN_LIQUID_DAYS = 60  # a symbol needs this many in-span liquid days to enter the candidate set
# Bound how many pending days ONE process caches before exiting (0 = all). Chunking the cache across fresh
# subprocess invocations (run_all.sh loops until complete) caps RSS — polars/Arrow allocations from the
# per-day scans accumulate ~17MB/day in a long-lived process; a fresh process per chunk reclaims them at exit.
MAX_DAYS_PER_RUN = int(os.environ.get("MAX_DAYS_PER_RUN", "0"))
CACHE_ONLY = (
    os.environ.get("CACHE_ONLY", "0") == "1"
)  # only build the cache chunk, then exit (no weekly panel)


def list_days() -> list[str]:
    days = sorted(
        p.split("date=")[1].split("/")[0] for p in glob.glob(f"{STORE}/raw/bars/symbol=SPY/date=*")
    )
    return [d for d in days if SPAN_START <= d <= SPAN_END]


def day_daily(day: str) -> pl.DataFrame:
    """ONE scan over ALL symbols for ONE day → per-symbol (symbol, close=RTH last, dvol=RTH dollar volume,
    entry=first RTH bar close >=09:35 ET). Per-DAY batching (one hive scan reads every symbol that day) keeps
    the multi-year build to ~one scan per trading day instead of millions of per-(symbol,day) opens.
    ET-anchored Int32-cast (the #197 DST/Int8 fix)."""
    pattern = f"{STORE}/raw/bars/symbol=*/date={day}/*.parquet"
    if not glob.glob(pattern):
        return pl.DataFrame()
    lazy = pl.scan_parquet(pattern, hive_partitioning=True).select(["symbol", "ts", "close", "volume"])
    et = pl.col("ts").dt.convert_time_zone("America/New_York")
    etm = et.dt.hour().cast(pl.Int32) * 60 + et.dt.minute().cast(pl.Int32)
    rth = lazy.with_columns(etm.alias("_etm")).filter(
        (pl.col("_etm") >= 9 * 60 + 30) & (pl.col("_etm") < 16 * 60)
    )
    out = (
        rth.sort("ts")
        .group_by("symbol")
        .agg(
            pl.col("close").last().alias("close"),
            (pl.col("close") * pl.col("volume")).sum().alias("dvol"),
            pl.col("close").filter(pl.col("_etm") >= ENTRY_ET_MIN).first().alias("entry"),
        )
        .collect()
    )
    return out.with_columns(pl.lit(day).alias("date"))


def weekly_rebalance_days(days: list[str]) -> list[tuple[int, str, str]]:
    """Index the trading-day list into (friday_idx, friday_day, next_monday_entry_day) tuples. We rebalance
    every 5 trading days: index i = signal as-of close of days[i], enter at days[i+1]'s tradeable open, hold
    to days[i+REV_DAYS]. Reconstructed purely from the trading-day calendar present in the bars."""
    out = []
    for i in range(VOL_DAYS, len(days) - REV_DAYS - 1, REV_DAYS):
        out.append((i, days[i], days[i + 1]))
    return out


def build_daily_cache(days: list[str]) -> pl.DataFrame:
    """The slow step, made crash-survivable AND memory-bounded: scan each trading day once → its per-symbol
    daily aggregate, written to its OWN partition file ``daily_cache/<date>.parquet``. Flat memory (each day
    is written then dropped — never an in-RAM accumulation of the whole panel), and resumable (a re-run skips
    days whose partition already exists, so a killed build resumes from where it stopped). The final lazy
    scan over the partition dir assembles the daily frame once, at the end."""
    cache_dir = f"{OUT_DIR}/daily_cache"
    os.makedirs(cache_dir, exist_ok=True)
    done = {p[:-8] for p in os.listdir(cache_dir) if p.endswith(".parquet")}
    pending = [d for d in days if d not in done]
    if MAX_DAYS_PER_RUN > 0:
        pending = pending[:MAX_DAYS_PER_RUN]
    if done:
        print(f"resuming: {len(done)} cached, {len(pending)} this chunk", flush=True)
    for i, day in enumerate(pending):
        dd = day_daily(day)
        if dd.height:
            dd.write_parquet(f"{cache_dir}/{day}.parquet")  # one small file/day; memory does NOT grow
        if (i + 1) % 60 == 0 or i == len(pending) - 1:
            print(f"  cached {len(done) + i + 1} (chunk {i + 1}/{len(pending)})", flush=True)
    n_cached = len([p for p in os.listdir(cache_dir) if p.endswith(".parquet")])
    print(f"CACHE_STATUS cached={n_cached} total={len(days)}", flush=True)
    if CACHE_ONLY:
        return pl.DataFrame()
    return pl.scan_parquet(f"{cache_dir}/*.parquet").filter(pl.col("date").is_in(set(days))).collect()


def build() -> None:
    days = list_days()
    print(f"trading days {len(days)} {days[0]}..{days[-1]}", flush=True)
    daily = build_daily_cache(days)
    if CACHE_ONLY:
        return  # cache chunk done; the orchestrator loops until the cache is complete, then assembles
    # Candidate set: a symbol needs CANDIDATE_MIN_LIQUID_DAYS in-span days over the ADV floor.
    liq = daily.filter(pl.col("dvol") >= CANDIDATE_ADV_FLOOR).group_by("symbol").len()
    candidates = set(liq.filter(pl.col("len") >= CANDIDATE_MIN_LIQUID_DAYS)["symbol"].to_list())
    daily = daily.filter(pl.col("symbol").is_in(candidates))
    closes: dict[str, dict[str, float]] = {}
    dvols: dict[str, dict[str, float]] = {}
    entries: dict[str, dict[str, float]] = {}
    for sym, grp in daily.group_by("symbol"):
        s = sym[0] if isinstance(sym, tuple) else sym
        closes[s] = dict(zip(grp["date"].to_list(), grp["close"].to_list()))
        dvols[s] = dict(zip(grp["date"].to_list(), grp["dvol"].to_list()))
        entries[s] = dict(zip(grp["date"].to_list(), grp["entry"].to_list()))
    candidates = list(closes.keys())
    print(
        f"candidates={len(candidates)} (>= {CANDIDATE_MIN_LIQUID_DAYS} days over ${CANDIDATE_ADV_FLOOR/1e6:.0f}M ADV)",
        flush=True,
    )

    rebal = weekly_rebalance_days(days)
    rows = []
    for friday_idx, friday, monday in rebal:
        window_prior = days[friday_idx - VOL_DAYS : friday_idx + 1]
        fwd_end = days[friday_idx + 1 + REV_DAYS] if friday_idx + 1 + REV_DAYS < len(days) else None
        # POINT-IN-TIME universe: trailing-20d ADV as of THIS Friday (no future liquidity peeking), top-N.
        adv_now = []
        for sym in candidates:
            dser = dvols[sym]
            trailing = [dser[d] for d in window_prior if d in dser]
            if len(trailing) >= VOL_DAYS // 2:
                adv_now.append((sym, float(np.mean(trailing))))
        adv_now.sort(key=lambda kv: kv[1], reverse=True)
        universe = {sym: adv for sym, adv in adv_now[:N_SYMBOLS]}
        for sym, adv20 in universe.items():
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
            entry = entries[sym].get(monday)
            if entry is not None and entry < MIN_PRICE:
                entry = None
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
                    "size": float(np.log(adv20)),  # log trailing-ADV = the size/liquidity control
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
