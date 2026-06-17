"""W3 — 13D activist-stake drift on LIQUID targets.

Pre-registered in hypothesis.md (Lens L5, EDGAR content). The bet: an activist files a Schedule 13D
(>5% stake with intent to influence) on a LIQUID target -> documented positive forward drift over
days/weeks (Brav-Jiang-Partnoy-Thomas activist-announcement premium). The most differentiated wave-1
bet: a genuine slow INFO SHOCK the price tape does not contain, far less crowded than price factors.

Pipeline (see method.md):
  1. Daily (symbol, date) panel from /store/raw/bars (close, open_price, dollar_vol), cached.
  2. ADV_dollar (median 20d) -> liquidity tertiles; LIQUID = top tertile (PRIMARY) + top-300 sub-cut.
  3. 13D events = filings form_type in {SC 13D, SC 13D/A, SCHEDULE 13D, SCHEDULE 13D/A}, symbol not
     null, available_at >= 2024-12-11. Split INITIAL 13D (SCHEDULE 13D / SC 13D) vs AMENDMENTS (/A).
  4. Entry = D+1 OPEN after available_at; forward returns open-entry {1,3,5,10,20,40,60}d.
  5. Headline = directional LONG cohort drift vs same-date non-event control (per-symbol demeaned,
     day-clustered, net of cost). 13D is a documented POSITIVE-direction event -> long the cohort.
  6. Gates: shuffle-canary, per-symbol demean, walk-forward OOS by date, per-trade bootstrap on the
     realized D+1->D+H long round-trips (10k), cost gate (measured liquid spread + 2x stress).

UTC: bars ts genuine UTC; 13:30 UTC = 09:30 ET (summer). RTH hours [13,21]. Minute math cast Int32.
Forward returns finite-filtered (drop_nulls does NOT drop NaN). available_at = PIT event time.
"""

import json
import math
import os
import random
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl
import psycopg

BARS_ROOT = Path("/store/raw/bars")
QUOTES_ROOT = Path("/store/raw/quotes")
OUTPUT_DIR = Path("/app/experiments/2026-06-16-w3-activist-13d")
PANEL_CACHE = OUTPUT_DIR / "daily_panel.parquet"
SPREAD_CACHE = OUTPUT_DIR / "liquid_half_spread.json"

HORIZONS = [1, 3, 5, 10, 20, 40, 60]
CANARY_SEEDS = 20
N_BOOT = 10000
ADV_DAYS = 20
TOP_N = 300

RTH_OPEN_UTC_HOUR = 13
RTH_CLOSE_UTC_HOUR = 21

INITIAL_FORMS = ("SC 13D", "SCHEDULE 13D")
AMEND_FORMS = ("SC 13D/A", "SCHEDULE 13D/A")
ALL_13D_FORMS = INITIAL_FORMS + AMEND_FORMS
SPREAD_SAMPLE_DAYS = 5


def get_db_connection() -> psycopg.Connection[Any]:
    return psycopg.connect(
        host=os.environ["DB_HOST"],
        port=os.environ["DB_PORT"],
        dbname=os.environ["DB_NAME"],
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
    )


def build_daily_panel_from_bars() -> pl.DataFrame:
    print("Building daily bar panel from /store/raw/bars ...", flush=True)
    all_frames: list[pl.DataFrame] = []
    symbol_dirs = [d for d in BARS_ROOT.iterdir() if d.is_dir()]
    total = len(symbol_dirs)
    for idx, sym_dir in enumerate(symbol_dirs):
        symbol = sym_dir.name.split("=", 1)[1]
        sym_frames: list[pl.DataFrame] = []
        for date_dir in sym_dir.iterdir():
            if not date_dir.is_dir():
                continue
            parquet_path = date_dir / "data.parquet"
            if not parquet_path.exists():
                continue
            sym_frames.append(pl.read_parquet(parquet_path, columns=["ts", "open", "close", "volume"]))
        if not sym_frames:
            continue
        sym_all = (
            pl.concat(sym_frames, rechunk=True)
            .with_columns(
                pl.col("ts").dt.hour().cast(pl.Int32).alias("utc_hour"),
                pl.col("ts").dt.minute().cast(pl.Int32).alias("utc_minute"),
                pl.col("ts").dt.date().alias("bar_date"),
            )
            .filter(
                (pl.col("utc_hour") >= RTH_OPEN_UTC_HOUR) & (pl.col("utc_hour") <= RTH_CLOSE_UTC_HOUR)
            )
        )
        if sym_all.is_empty():
            continue
        sym_all = sym_all.with_columns(
            (
                ((pl.col("utc_hour") == 13) & (pl.col("utc_minute") >= 30)) | (pl.col("utc_hour") > 13)
            ).alias("is_open_eligible")
        )
        daily_close = sym_all.sort("ts").group_by("bar_date").agg(pl.col("close").last().alias("close"))
        daily_open = (
            sym_all.filter(pl.col("is_open_eligible"))
            .sort("ts")
            .group_by("bar_date")
            .agg(pl.col("open").first().alias("open_price"))
        )
        daily_dvol = (
            sym_all.with_columns((pl.col("close") * pl.col("volume")).alias("dollar_bar"))
            .group_by("bar_date")
            .agg(pl.col("dollar_bar").sum().alias("dollar_vol"))
        )
        daily = (
            daily_close.join(daily_open, on="bar_date", how="left")
            .join(daily_dvol, on="bar_date", how="left")
            .with_columns(pl.lit(symbol).alias("symbol"))
            .select(["symbol", "bar_date", "close", "open_price", "dollar_vol"])
        )
        all_frames.append(daily)
        if (idx + 1) % 1000 == 0:
            print(f"  {idx + 1}/{total} symbols", flush=True)
    panel = pl.concat(all_frames, rechunk=True).sort(["symbol", "bar_date"])
    return panel


def load_daily_panel() -> pl.DataFrame:
    if PANEL_CACHE.exists():
        print(f"Loading cached W3 panel {PANEL_CACHE}", flush=True)
        return pl.read_parquet(PANEL_CACHE)
    panel = build_daily_panel_from_bars()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    panel.write_parquet(PANEL_CACHE)
    print(f"  Panel shape: {panel.shape}; cached -> {PANEL_CACHE}", flush=True)
    return panel


def compute_adv_dollar(panel: pl.DataFrame, adv_days: int = ADV_DAYS) -> pl.DataFrame:
    all_dates = sorted(panel["bar_date"].unique().to_list())
    last_dates = set(all_dates[-adv_days:]) if len(all_dates) >= adv_days else set(all_dates)
    adv_panel = panel.filter(pl.col("bar_date").is_in(list(last_dates)))
    adv = (
        adv_panel.group_by("symbol")
        .agg(pl.col("dollar_vol").median().alias("adv_dollar"))
        .filter(pl.col("adv_dollar").is_not_null() & (pl.col("adv_dollar") > 0))
    )
    return adv


def assign_liquidity_tertile(adv: pl.DataFrame) -> pl.DataFrame:
    sorted_adv = adv.sort("adv_dollar", descending=True)
    n = len(sorted_adv)
    t1, t2 = n // 3, 2 * n // 3
    labels = ["liquid"] * t1 + ["mid"] * (t2 - t1) + ["illiquid"] * (n - t2)
    return sorted_adv.with_columns(pl.Series("adv_tertile", labels))


def file_half_spread_median(parquet_path: Path) -> float | None:
    lazy = (
        pl.scan_parquet(parquet_path)
        .select(["ts", "bid_price", "ask_price"])
        .with_columns(
            pl.col("ts").dt.hour().cast(pl.Int32).alias("utc_hour"),
            pl.col("ts").dt.minute().cast(pl.Int32).alias("utc_minute"),
        )
        .filter(
            (((pl.col("utc_hour") == 13) & (pl.col("utc_minute") >= 30)) | (pl.col("utc_hour") > 13))
            & (pl.col("utc_hour") <= RTH_CLOSE_UTC_HOUR)
            & (pl.col("bid_price") > 0)
            & (pl.col("ask_price") > pl.col("bid_price"))
        )
        .select(
            (
                0.5
                * (pl.col("ask_price") - pl.col("bid_price"))
                / ((pl.col("ask_price") + pl.col("bid_price")) / 2.0)
                * 10000.0
            )
            .median()
            .alias("hs_med"),
            pl.len().alias("n"),
        )
    )
    out = lazy.collect()
    if out.height == 0 or out["n"][0] < 10:
        return None
    med = out["hs_med"][0]
    return float(med) if med is not None and math.isfinite(med) and med > 0 else None


def measure_liquid_half_spread(liquid_symbols: set[str]) -> float:
    print("Measuring liquid half-spread from quotes...", flush=True)
    quote_syms = [d.name.split("=", 1)[1] for d in QUOTES_ROOT.iterdir() if d.is_dir()]
    common = sorted(s for s in quote_syms if s in liquid_symbols)
    print(f"  {len(common)} liquid symbols have quotes", flush=True)
    per_sym_hs: list[float] = []
    for idx, symbol in enumerate(common):
        sym_dir = QUOTES_ROOT / f"symbol={symbol}"
        date_dirs = sorted((d for d in sym_dir.iterdir() if d.is_dir()), reverse=True)[:SPREAD_SAMPLE_DAYS]
        file_meds: list[float] = []
        for date_dir in date_dirs:
            parquet_path = date_dir / "data.parquet"
            if not parquet_path.exists():
                continue
            med = file_half_spread_median(parquet_path)
            if med is not None:
                file_meds.append(med)
        if file_meds:
            per_sym_hs.append(float(np.median(file_meds)))
        if (idx + 1) % 500 == 0:
            print(f"  spread: {idx + 1}/{len(common)} liquid symbols", flush=True)
    median_hs = float(np.median(per_sym_hs)) if per_sym_hs else float("nan")
    print(f"  Liquid median half-spread: {median_hs:.2f} bps (n={len(per_sym_hs)} names)", flush=True)
    return median_hs


def load_liquid_half_spread(liquid_symbols: set[str]) -> float:
    if SPREAD_CACHE.exists():
        liquid_hs = float(json.loads(SPREAD_CACHE.read_text())["liquid_half_spread_bps"])
        print(f"Loaded cached liquid half-spread: {liquid_hs:.2f} bps", flush=True)
        return liquid_hs
    liquid_hs = measure_liquid_half_spread(liquid_symbols)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    SPREAD_CACHE.write_text(json.dumps({"liquid_half_spread_bps": liquid_hs}))
    return liquid_hs


def load_13d_events() -> pl.DataFrame:
    print("Loading 13D events from filings...", flush=True)
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT symbol, available_at::date AS avail_date, available_at, form_type, accession_number
        FROM filings
        WHERE form_type = ANY(%s) AND symbol IS NOT NULL AND available_at >= '2024-12-11'
        ORDER BY symbol, available_at
        """,
        (list(ALL_13D_FORMS),),
    )
    rows = cur.fetchall()
    conn.close()
    df = pl.DataFrame(
        {
            "symbol": [r[0] for r in rows],
            "available_at_date": [r[1] for r in rows],
            "available_at": [r[2] for r in rows],
            "form_type": [r[3] for r in rows],
            "accession_number": [r[4] for r in rows],
        }
    ).unique(subset=["symbol", "available_at_date", "accession_number"])
    df = df.with_columns(
        pl.when(pl.col("form_type").is_in(list(INITIAL_FORMS)))
        .then(pl.lit("initial"))
        .otherwise(pl.lit("amend"))
        .alias("event_kind")
    )
    print(
        f"  {len(df)} 13D events; initial={df.filter(pl.col('event_kind') == 'initial').height} "
        f"amend={df.filter(pl.col('event_kind') == 'amend').height} ; "
        f"{df['symbol'].n_unique()} symbols",
        flush=True,
    )
    return df


def assign_entry_dates(events: pl.DataFrame, trading_dates: list[date]) -> pl.DataFrame:
    trading_set = set(trading_dates)
    trading_sorted = sorted(trading_dates)
    last_td = trading_sorted[-1]
    entry_dates: list[date | None] = []
    for row in events.to_dicts():
        avail = row["available_at_date"]
        if isinstance(avail, datetime):
            avail = avail.date()
        cand = avail + timedelta(days=1)
        picked: date | None = None
        while cand <= last_td:
            if cand in trading_set:
                picked = cand
                break
            cand += timedelta(days=1)
        entry_dates.append(picked)
    result = events.with_columns(pl.Series("entry_date", entry_dates, dtype=pl.Date))
    result = result.filter(pl.col("entry_date").is_not_null())
    print(f"  Events with valid entry date: {len(result)}", flush=True)
    return result


def build_forward_returns(panel: pl.DataFrame, horizons: list[int]) -> pl.DataFrame:
    result = panel.sort(["symbol", "bar_date"])
    for horizon in horizons:
        result = result.with_columns(
            (pl.col("close").shift(-horizon).over("symbol") / pl.col("open_price") - 1).alias(
                f"open_fwd_{horizon}d"
            )
        )
    return result


def day_clustered(values_per_date: list[float]) -> tuple[float, float, int]:
    arr = np.array([v for v in values_per_date if np.isfinite(v)])
    n = len(arr)
    if n < 5:
        return float("nan"), float("nan"), n
    mean = float(arr.mean())
    sd = float(arr.std(ddof=1))
    t = mean / (sd / np.sqrt(n)) if sd > 0 else float("nan")
    return mean, t, n


def headline_drift(panel_events: pl.DataFrame, fwd_col: str, cost: float) -> dict[str, Any]:
    """Directional LONG cohort(event) - control(non-event), per-symbol-demeaned, day-clustered,
    net of `cost` (round-trip). Vectorized per-date aggregation (no Python row loops)."""
    panel_events = panel_events.filter(pl.col(fwd_col).is_finite())
    sym_means = panel_events.group_by("symbol").agg(pl.col(fwd_col).mean().alias("sym_mean"))
    panel_dm = panel_events.join(sym_means, on="symbol", how="left").with_columns(
        (pl.col(fwd_col) - pl.col("sym_mean")).alias("fwd_dm")
    )

    # raw per-date: event mean (minus cost) - control mean
    raw_by_date = (
        panel_events.group_by("bar_date")
        .agg(
            pl.col(fwd_col).filter(pl.col("is_event")).mean().alias("ev_mean"),
            pl.col(fwd_col).filter(~pl.col("is_event")).mean().alias("ct_mean"),
            pl.col("is_event").sum().alias("n_ev"),
        )
        .filter((pl.col("n_ev") > 0) & pl.col("ct_mean").is_not_null())
        .with_columns((pl.col("ev_mean") - cost - pl.col("ct_mean")).alias("raw_diff"))
    )
    raw_per_date = raw_by_date["raw_diff"].to_list()
    n_events_total = int(raw_by_date["n_ev"].sum())

    dm_by_date = (
        panel_dm.group_by("bar_date")
        .agg(
            pl.col("fwd_dm").filter(pl.col("is_event")).mean().alias("ev_dm"),
            pl.col("fwd_dm").filter(~pl.col("is_event")).mean().alias("ct_dm"),
            pl.col("is_event").sum().alias("n_ev"),
        )
        .filter((pl.col("n_ev") > 0) & pl.col("ct_dm").is_not_null() & pl.col("ev_dm").is_not_null())
        .with_columns((pl.col("ev_dm") - cost - pl.col("ct_dm")).alias("dm_diff"))
    )
    dm_per_date = dm_by_date["dm_diff"].to_list()

    raw_mean, raw_t, n_dates = day_clustered(raw_per_date)
    dm_mean, dm_t, _ = day_clustered(dm_per_date)

    # canary: permute event/control labels within each date (vectorized resample of the diff null)
    event_dates = raw_by_date["bar_date"].to_list()
    by_date_arrays: dict[Any, tuple[np.ndarray, int]] = {}
    grouped = panel_events.select(["bar_date", fwd_col, "is_event"]).partition_by("bar_date", as_dict=True)
    for key, frame in grouped.items():
        bdate = key[0] if isinstance(key, tuple) else key
        allret = frame[fwd_col].to_numpy()
        n_ev = int(frame["is_event"].sum())
        by_date_arrays[bdate] = (allret, n_ev)

    canary_means: list[float] = []
    rng = random.Random(42)
    for seed in range(CANARY_SEEDS):
        rng.seed(seed)
        perm_per_date: list[float] = []
        for bdate in event_dates:
            allret, n_ev = by_date_arrays[bdate]
            if n_ev == 0 or len(allret) < 2:
                continue
            perm = allret.copy()
            np.random.default_rng(seed * 100003 + hash(bdate) % 100003).shuffle(perm)
            perm_ev, perm_ct = perm[:n_ev], perm[n_ev:]
            if len(perm_ct):
                perm_per_date.append(float(perm_ev.mean()) - float(perm_ct.mean()))
        if perm_per_date:
            canary_means.append(float(np.mean(perm_per_date)))
    canary_mean = float(np.mean(canary_means)) if canary_means else float("nan")
    canary_p95 = float(np.percentile(canary_means, 95)) if canary_means else float("nan")

    return {
        "n_event_obs": n_events_total,
        "n_dates": n_dates,
        "alpha_raw_pct": raw_mean * 100 if np.isfinite(raw_mean) else None,
        "t_raw": round(raw_t, 2) if np.isfinite(raw_t) else None,
        "alpha_demean_pct": dm_mean * 100 if np.isfinite(dm_mean) else None,
        "t_demean": round(dm_t, 2) if np.isfinite(dm_t) else None,
        "canary_mean_pct": canary_mean * 100 if np.isfinite(canary_mean) else None,
        "canary_p95_pct": canary_p95 * 100 if np.isfinite(canary_p95) else None,
    }


def long_trades(panel_events: pl.DataFrame, fwd_col: str, cost: float) -> tuple[np.ndarray, dict[str, Any]]:
    """Per-trade realized LONG round-trips: each 13D event = enter D+1 open, hold H days, exit.
    payoff = (event open-fwd return) - (same-date control mean) - cost. Non-overlapping per event."""
    panel_events = panel_events.filter(pl.col(fwd_col).is_finite())
    ctrl_mean = (
        panel_events.filter(~pl.col("is_event"))
        .group_by("bar_date")
        .agg(pl.col(fwd_col).mean().alias("ctrl_mean"))
    )
    ev = panel_events.filter(pl.col("is_event")).join(ctrl_mean, on="bar_date", how="left")
    ev = ev.filter(pl.col("ctrl_mean").is_not_null())
    ev = ev.with_columns((pl.col(fwd_col) - pl.col("ctrl_mean") - cost).alias("net_ret"))
    ev = ev.filter(pl.col("net_ret").is_finite())
    trades = ev["net_ret"].to_numpy()
    per_date_frame = ev.group_by("bar_date").agg(pl.col("net_ret").mean().alias("d_mean"))
    per_date = per_date_frame["d_mean"].to_list()
    mean, t, n_dates = day_clustered(per_date)
    summary = {
        "n_trades": int(len(trades)),
        "n_dates": n_dates,
        "mean_net_pct": float(np.mean(trades)) * 100 if len(trades) else None,
        "day_clustered_mean_pct": mean * 100 if np.isfinite(mean) else None,
        "day_clustered_t": round(t, 2) if np.isfinite(t) else None,
    }
    return trades, summary


def bootstrap_ci(trades: np.ndarray, n_boot: int = N_BOOT) -> dict[str, Any]:
    trades = trades[np.isfinite(trades)]
    if len(trades) < 20:
        return {"lo_pct": None, "hi_pct": None, "mean_pct": None, "n": int(len(trades)),
                "ci_excludes_zero_above": None}
    rng = np.random.default_rng(42)
    idx = rng.integers(0, len(trades), size=(n_boot, len(trades)))
    boot_means = trades[idx].mean(axis=1)
    lo, hi = np.percentile(boot_means, [2.5, 97.5])
    return {
        "lo_pct": float(lo) * 100,
        "hi_pct": float(hi) * 100,
        "mean_pct": float(trades.mean()) * 100,
        "n": int(len(trades)),
        "ci_excludes_zero_above": bool(lo > 0),
    }


def build_event_panel(
    fwd_panel: pl.DataFrame, events: pl.DataFrame, cohort_symbols: set[str]
) -> pl.DataFrame:
    entries = (
        events.filter(pl.col("symbol").is_in(list(cohort_symbols)))
        .select(["symbol", "entry_date"])
        .unique()
        .rename({"entry_date": "bar_date"})
        .with_columns(pl.lit(True).alias("is_event"))
    )
    cohort = fwd_panel.filter(pl.col("symbol").is_in(list(cohort_symbols)))
    return cohort.join(entries, on=["symbol", "bar_date"], how="left").with_columns(
        pl.col("is_event").fill_null(False)
    )


def run_cohort(
    fwd_panel: pl.DataFrame,
    events: pl.DataFrame,
    cohort_symbols: set[str],
    label: str,
    cost_rt: float,
    cost_rt_2x: float,
    oos_dates: set[date],
    train_dates: set[date],
) -> dict[str, Any]:
    print(f"\n=== {label} ({len(cohort_symbols)} symbols, {events['symbol'].n_unique()} event-syms) ===",
          flush=True)
    panel = build_event_panel(fwd_panel, events, cohort_symbols)
    out: dict[str, Any] = {"label": label, "n_symbols": len(cohort_symbols), "horizons": {}}
    for horizon in HORIZONS:
        open_col = f"open_fwd_{horizon}d"
        h_out: dict[str, Any] = {}
        full = panel.filter(pl.col(open_col).is_finite())
        oos = panel.filter(pl.col("bar_date").is_in(list(oos_dates)) & pl.col(open_col).is_finite())
        train = panel.filter(pl.col("bar_date").is_in(list(train_dates)) & pl.col(open_col).is_finite())

        h_out["headline_full_net"] = headline_drift(full, open_col, cost_rt)
        h_out["headline_train_net"] = headline_drift(train, open_col, cost_rt)
        h_out["headline_oos_net"] = headline_drift(oos, open_col, cost_rt)
        h_out["headline_full_gross"] = headline_drift(full, open_col, 0.0)

        trades_full, sum_full = long_trades(full, open_col, cost_rt)
        trades_full_gross, sum_full_gross = long_trades(full, open_col, 0.0)
        trades_oos, sum_oos = long_trades(oos, open_col, cost_rt)
        trades_oos_2x, sum_oos_2x = long_trades(oos, open_col, cost_rt_2x)
        trades_oos_gross, sum_oos_gross = long_trades(oos, open_col, 0.0)
        h_out["long_full_net"] = {**sum_full, "bootstrap": bootstrap_ci(trades_full)}
        h_out["long_full_gross"] = {**sum_full_gross, "bootstrap": bootstrap_ci(trades_full_gross)}
        h_out["long_oos_net"] = {**sum_oos, "bootstrap": bootstrap_ci(trades_oos)}
        h_out["long_oos_net_2x"] = {**sum_oos_2x, "bootstrap": bootstrap_ci(trades_oos_2x)}
        h_out["long_oos_gross"] = {**sum_oos_gross, "bootstrap": bootstrap_ci(trades_oos_gross)}

        out["horizons"][f"{horizon}d"] = h_out
        sig = h_out["long_oos_net"]
        boot = sig["bootstrap"]
        hd = h_out["headline_full_net"]
        print(
            f"  H={horizon:2d}d | full dm%={hd['alpha_demean_pct']} t={hd['t_demean']} "
            f"canary95%={hd['canary_p95_pct']} | OOS long net%={sig['mean_net_pct']} "
            f"CI=[{boot['lo_pct']},{boot['hi_pct']}] n={boot['n']} excl0>{boot.get('ci_excludes_zero_above')}",
            flush=True,
        )
    return out


def main() -> None:
    print("=== W3: 13D activist drift on LIQUID targets ===\n", flush=True)
    panel = load_daily_panel()
    trading_dates_all: list[date] = sorted(panel["bar_date"].unique().to_list())
    n_dates = len(trading_dates_all)
    print(f"Panel: {panel['symbol'].n_unique()} symbols, {n_dates} dates "
          f"({trading_dates_all[0]} to {trading_dates_all[-1]})", flush=True)

    adv = assign_liquidity_tertile(compute_adv_dollar(panel))
    liquid_symbols = set(adv.filter(pl.col("adv_tertile") == "liquid")["symbol"].to_list())
    mid_symbols = set(adv.filter(pl.col("adv_tertile") == "mid")["symbol"].to_list())
    illiquid_symbols = set(adv.filter(pl.col("adv_tertile") == "illiquid")["symbol"].to_list())
    all_symbols = set(adv["symbol"].to_list())
    top300_symbols = set(adv.sort("adv_dollar", descending=True).head(TOP_N)["symbol"].to_list())
    print(f"Tertiles: liquid={len(liquid_symbols)} mid={len(mid_symbols)} illiquid={len(illiquid_symbols)}",
          flush=True)

    liquid_hs = load_liquid_half_spread(liquid_symbols)
    cost_rt = 2.0 * (liquid_hs / 10000.0) if math.isfinite(liquid_hs) else 0.0
    cost_rt_2x = 2.0 * (2.0 * liquid_hs / 10000.0) if math.isfinite(liquid_hs) else 0.0
    print(f"Liquid round-trip cost: {cost_rt*10000:.1f} bps (1x), {cost_rt_2x*10000:.1f} bps (2x)",
          flush=True)

    events_all = load_13d_events()
    events_all = assign_entry_dates(events_all, trading_dates_all)
    events_all = events_all.filter(pl.col("symbol").is_in(list(all_symbols)))
    events_initial = events_all.filter(pl.col("event_kind") == "initial")
    events_amend = events_all.filter(pl.col("event_kind") == "amend")

    def liq_count(ev: pl.DataFrame) -> int:
        return ev.filter(pl.col("symbol").is_in(list(liquid_symbols))).height

    print(f"Events with bars: all={len(events_all)} (liq {liq_count(events_all)}) "
          f"initial={len(events_initial)} (liq {liq_count(events_initial)}) "
          f"amend={len(events_amend)} (liq {liq_count(events_amend)})", flush=True)

    fwd_panel = build_forward_returns(panel, HORIZONS)

    split_idx = n_dates // 2
    train_dates_set = set(trading_dates_all[:split_idx])
    oos_dates_set = set(trading_dates_all[split_idx:])
    print(f"Split: TRAIN {sorted(train_dates_set)[0]}..{sorted(train_dates_set)[-1]} "
          f"({len(train_dates_set)}d) | OOS {sorted(oos_dates_set)[0]}..{sorted(oos_dates_set)[-1]} "
          f"({len(oos_dates_set)}d)", flush=True)

    event_sets = [
        ("13D_initial", events_initial),
        ("13D_amend", events_amend),
        ("13D_all", events_all),
    ]
    cohort_defs = [
        ("LIQUID tertile (PRIMARY)", liquid_symbols),
        ("Top-300 (sub-cut)", top300_symbols),
        ("Full universe (context)", all_symbols),
        ("Mid tertile (context)", mid_symbols),
        ("Illiquid tertile (context)", illiquid_symbols),
    ]

    results: dict[str, Any] = {
        "meta": {
            "n_symbols_panel": panel["symbol"].n_unique(),
            "n_dates": n_dates,
            "date_start": str(trading_dates_all[0]),
            "date_end": str(trading_dates_all[-1]),
            "n_events_all": len(events_all),
            "n_events_initial": len(events_initial),
            "n_events_amend": len(events_amend),
            "n_events_all_liquid": liq_count(events_all),
            "n_events_initial_liquid": liq_count(events_initial),
            "n_events_amend_liquid": liq_count(events_amend),
            "liquid_half_spread_bps": liquid_hs,
            "cost_rt_bps": cost_rt * 10000,
            "cost_rt_2x_bps": cost_rt_2x * 10000,
            "n_liquid_symbols": len(liquid_symbols),
            "n_top300_symbols": len(top300_symbols),
            "horizons": HORIZONS,
            "oos_start": str(sorted(oos_dates_set)[0]),
        },
        "event_sets": {},
    }
    for set_label, events in event_sets:
        results["event_sets"][set_label] = {}
        for cohort_label, syms in cohort_defs:
            results["event_sets"][set_label][cohort_label] = run_cohort(
                fwd_panel, events, syms, f"{set_label} | {cohort_label}",
                cost_rt, cost_rt_2x, oos_dates_set, train_dates_set
            )

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    tmp = OUTPUT_DIR / "raw_results_w3.json.tmp"
    with open(tmp, "w") as json_file:
        json.dump(results, json_file, indent=2, default=str)
    tmp.replace(OUTPUT_DIR / "raw_results_w3.json")
    print("\nSaved raw_results_w3.json", flush=True)


if __name__ == "__main__":
    main()
