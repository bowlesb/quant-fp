"""MONTHLY LOW-TURNOVER FACTOR — panel builder (pre-registered, see prereg.md).

The cost-is-not-the-enemy pivot after 6 cost-killed nulls. Builds a MONTHLY-rebalance cross-sectional panel
from the deep `fp_store_real` minute bars → daily (RTH last-close + dollar volume, ET-anchored Int32-cast,
the #197 DST/Int8 fix). REUSES the #205 memory-bounded, host-mounted, resumable daily-cache infra (per-day
partition files + chunked-subprocess build — the lessons that got #205 home). Each monthly observation =
(rebalance_day, symbol) with:
  - lowvol   = − trailing-60-trading-day realized daily-return vol (the low-vol factor; sign so higher=better)
  - sec_rel_mom = trailing 21d return minus the name's GICS-sector EW-mean 21d return (sector-relative drift)
  - vol60    = the raw trailing-60d vol (own-vol control)
  - size     = log trailing-21d mean dollar volume (the size/liquidity control)
  - y_fwd_1m = forward 21-trading-day return ENTERED at the NEXT session's tradeable open (>=09:35 ET, never
    the month-end close — no close-to-close look-ahead)
  - disappeared = 1 if the name stops printing bars during the forward month (the delisting-haircut flag)
The screen builds the TURNOVER-BANDED book + the net-IR / net-median COST GATE (cost as a first-class term).

Survivorship is explicit (same caveat as #205): the monthly universe = top-N trailing-ADV names PRINTING,
reconstructed from bars. $1-floor + per-month winsor + the −30%/−100% delisting haircut live in the screen.
READ-ONLY stores. NOTE: turn-key; the full run is sequenced with the Lead.
"""

from __future__ import annotations

import glob
import os

import numpy as np
import polars as pl

from quantlib.features.loaders import _query

STORE = os.environ.get("STORE_ROOT", "/store")
OUT_DIR = "/app/experiments/2026-06-20-monthly-lowturnover"

SPAN_START = os.environ.get("SPAN_START", "2016-01-01")
SPAN_END = os.environ.get("SPAN_END", "2025-12-31")
N_SYMBOLS = int(os.environ.get("N_SYMBOLS", "500"))  # top-N liquid names per monthly rebalance
REBAL_DAYS = 21  # monthly rebalance cadence (and the forward-return horizon), in trading days
VOL_DAYS = 60  # trailing realized-vol window (the low-vol factor)
MOM_DAYS = 21  # trailing window for the sector-relative momentum signal
ADV_DAYS = 21  # trailing dollar-volume window for the universe rank + size control
ENTRY_ET_MIN = 9 * 60 + 35  # 09:35 ET tradeable next-session entry (never the month-end close)
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


def monthly_rebalance_days(days: list[str]) -> list[tuple[int, str, str]]:
    """(rebal_idx, signal_day, next_session_entry_day) every REBAL_DAYS trading days: signal as-of close of
    days[i], enter at days[i+1]'s tradeable open, hold REBAL_DAYS to days[i+1+REBAL_DAYS]. Pure calendar from
    the bars. Starts at VOL_DAYS so the trailing 60d vol window is fully defined."""
    out = []
    for i in range(VOL_DAYS, len(days) - REBAL_DAYS - 1, REBAL_DAYS):
        out.append((i, days[i], days[i + 1]))
    return out


def load_sector_map() -> dict[str, str]:
    """Per-symbol normalized GICS sector (the #182 join), for the sector-relative momentum signal."""
    ref = _query("SELECT symbol, sector FROM sector_map WHERE sector IS NOT NULL", {})
    return {row["symbol"]: row["sector"].lower().replace(" ", "_") for row in ref.iter_rows(named=True)}


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

    sector_of = load_sector_map()
    rebal = monthly_rebalance_days(days)
    rows = []
    for idx, sig_day, entry_day in rebal:
        adv_window = days[idx - ADV_DAYS : idx + 1]
        vol_window = days[idx - VOL_DAYS : idx + 1]
        mom_start = days[idx - MOM_DAYS]
        fwd_end = days[idx + 1 + REBAL_DAYS] if idx + 1 + REBAL_DAYS < len(days) else None
        # POINT-IN-TIME universe: trailing-21d ADV as of THIS rebalance (no future liquidity peek), top-N.
        adv_now = []
        for sym in candidates:
            dser = dvols[sym]
            trailing = [dser[d] for d in adv_window if d in dser]
            if len(trailing) >= ADV_DAYS // 2:
                adv_now.append((sym, float(np.mean(trailing))))
        adv_now.sort(key=lambda kv: kv[1], reverse=True)
        universe = dict(adv_now[:N_SYMBOLS])

        # First pass: per-name lowvol + 21d momentum; accumulate per-sector 21d returns for the sector mean.
        per_name: dict[str, dict] = {}
        sector_rets: dict[str, list[float]] = {}
        for sym, adv in universe.items():
            series = closes[sym]
            if sig_day not in series or mom_start not in series:
                continue
            c_sig, c_mom0 = series[sig_day], series[mom_start]
            if c_sig < MIN_PRICE or c_mom0 < MIN_PRICE:
                continue
            mom_21 = c_sig / c_mom0 - 1.0
            prior_closes = [series[d] for d in vol_window if d in series]
            if len(prior_closes) < VOL_DAYS // 2:
                continue
            vol60 = float(np.std(np.diff(np.log(prior_closes))))
            sec = sector_of.get(sym)
            per_name[sym] = {"mom_21": mom_21, "vol60": vol60, "adv": adv, "sector": sec}
            if sec is not None:
                sector_rets.setdefault(sec, []).append(mom_21)
        sector_mean = {sec: float(np.mean(rets)) for sec, rets in sector_rets.items()}

        for sym, rec in per_name.items():
            entry = entries[sym].get(entry_day)
            if entry is not None and entry < MIN_PRICE:
                entry = None
            disappeared = 1 if (fwd_end is not None and fwd_end not in closes[sym]) else 0
            y_fwd = None
            if entry is not None and fwd_end is not None and fwd_end in closes[sym]:
                c_end = closes[sym][fwd_end]
                if c_end >= MIN_PRICE:
                    y_fwd = c_end / entry - 1.0
            sec = rec["sector"]
            sec_rel = (
                (rec["mom_21"] - sector_mean[sec]) if (sec is not None and sec in sector_mean) else None
            )
            rows.append(
                {
                    "rebal": sig_day,
                    "year": int(sig_day[:4]),
                    "symbol": sym,
                    "lowvol": -rec["vol60"],  # higher = lower vol = the factor (sign flipped)
                    "vol60": rec["vol60"],  # raw, for the own-vol control
                    "sec_rel_mom": sec_rel,
                    "size": float(np.log(rec["adv"])),
                    "y_fwd_1m": y_fwd,
                    "disappeared": disappeared,
                }
            )
    panel = pl.DataFrame(rows, infer_schema_length=None)
    out = f"{OUT_DIR}/monthly_panel.parquet"
    panel.write_parquet(out)
    n_months = panel["rebal"].n_unique() if panel.height else 0
    print(
        f"WROTE {out}: {panel.height} obs, {n_months} rebalances, "
        f"{panel['symbol'].n_unique() if panel.height else 0} syms, "
        f"disappeared={int(panel['disappeared'].sum()) if panel.height else 0}",
        flush=True,
    )


if __name__ == "__main__":
    build()
