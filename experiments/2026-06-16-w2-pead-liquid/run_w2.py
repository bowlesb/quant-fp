"""W2 — item-2.02 PEAD on LIQUID names.

Pipeline (see method.md):
  1. Daily (symbol, date) panel from /store/raw/bars (close, open_price, dollar_vol).
  2. ADV_dollar (median 20d) -> liquidity tertiles; LIQUID = top tertile (PRIMARY); +top-100 megacap cut.
  3. Earnings events = 8-Ks with item 2.02 (from item_map.json), available_at>=2025-12-15, entry=D+1 open.
  4. Forward returns open-entry {1,3,5,10,20,40}d; sign each event by its D+1 open->close reaction.
  5. Headline drift (cohort - control, demeaned, day-clustered) + signed L/S (drift from D+1 CLOSE).
  6. Gates: canary, demean, walk-forward OOS, per-trade bootstrap (10k), cost gate (liquid spread, 2x).

UTC: bars ts genuine UTC; 13:30 UTC = 09:30 ET (summer). RTH hours [13,21].
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
OUTPUT_DIR = Path("/app/experiments/2026-06-16-w2-pead-liquid")

HORIZONS = [1, 3, 5, 10, 20, 40]
CANARY_SEEDS = 10
N_BOOT = 10000
ADV_DAYS = 20
MEGACAP_N = 100

RTH_OPEN_UTC_HOUR = 13
RTH_OPEN_UTC_MINUTE = 30
RTH_CLOSE_UTC_HOUR = 21


def get_db_connection() -> psycopg.Connection[Any]:
    return psycopg.connect(
        host=os.environ["DB_HOST"],
        port=os.environ["DB_PORT"],
        dbname=os.environ["DB_NAME"],
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
    )


PANEL_CACHE = OUTPUT_DIR / "daily_panel.parquet"


def load_daily_panel() -> pl.DataFrame:
    if PANEL_CACHE.exists():
        print(f"Loading cached daily panel from {PANEL_CACHE}")
        return pl.read_parquet(PANEL_CACHE)
    print("Loading daily bar panel...")
    all_frames: list[pl.DataFrame] = []
    symbol_dirs = [d for d in BARS_ROOT.iterdir() if d.is_dir()]
    total = len(symbol_dirs)
    print(f"  {total} symbol directories")

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
                (pl.col("utc_hour") >= RTH_OPEN_UTC_HOUR)
                & (pl.col("utc_hour") <= RTH_CLOSE_UTC_HOUR)
            )
        )
        if sym_all.is_empty():
            continue

        sym_all = sym_all.with_columns(
            (
                ((pl.col("utc_hour") == 13) & (pl.col("utc_minute") >= 30))
                | (pl.col("utc_hour") > 13)
            ).alias("is_open_eligible")
        )

        daily_close = (
            sym_all.sort("ts").group_by("bar_date").agg(pl.col("close").last().alias("close"))
        )
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
            print(f"  {idx + 1}/{total} symbols done")

    panel = pl.concat(all_frames, rechunk=True).sort(["symbol", "bar_date"])
    print(f"  Panel shape: {panel.shape}")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    panel.write_parquet(PANEL_CACHE)
    print(f"  Cached panel to {PANEL_CACHE}")
    return panel


def compute_adv_dollar(panel: pl.DataFrame, adv_days: int = ADV_DAYS) -> pl.DataFrame:
    all_dates = sorted(panel["bar_date"].unique().to_list())
    last_dates = set(all_dates[-adv_days:]) if len(all_dates) >= adv_days else set(all_dates)
    adv_panel = panel.filter(pl.col("bar_date").is_in(list(last_dates)))
    adv = (
        adv_panel.group_by("symbol")
        .agg(
            pl.col("dollar_vol").median().alias("adv_dollar"),
            pl.col("close").median().alias("median_close"),
        )
        .filter(pl.col("adv_dollar").is_not_null() & (pl.col("adv_dollar") > 0))
    )
    print(f"  ADV computed for {len(adv)} symbols")
    return adv


def assign_liquidity_tertile(adv: pl.DataFrame) -> pl.DataFrame:
    sorted_adv = adv.sort("adv_dollar", descending=True)
    n = len(sorted_adv)
    t1, t2 = n // 3, 2 * n // 3
    labels = ["liquid"] * t1 + ["mid"] * (t2 - t1) + ["illiquid"] * (n - t2)
    return sorted_adv.with_columns(pl.Series("adv_tertile", labels))


SPREAD_SAMPLE_DAYS = 5


def file_half_spread_median(parquet_path: Path) -> float | None:
    """Median RTH half-spread (bps) for ONE quote file, computed lazily (never holds the full frame)."""
    lazy = (
        pl.scan_parquet(parquet_path)
        .select(["ts", "bid_price", "ask_price"])
        .with_columns(
            pl.col("ts").dt.hour().cast(pl.Int32).alias("utc_hour"),
            pl.col("ts").dt.minute().cast(pl.Int32).alias("utc_minute"),
        )
        .filter(
            (
                ((pl.col("utc_hour") == 13) & (pl.col("utc_minute") >= 30))
                | (pl.col("utc_hour") > 13)
            )
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
    """Median half-spread (bps) across LIQUID names with quotes.

    Memory-safe: scans each quote file lazily and only over the last SPREAD_SAMPLE_DAYS days per
    symbol (a per-name spread median is structural and needs no full history) — this avoids the OOM
    from concatenating a liquid name's entire quote tick history.
    """
    print("Measuring liquid half-spread from quotes...")
    quote_syms = [d.name.split("=", 1)[1] for d in QUOTES_ROOT.iterdir() if d.is_dir()]
    common = sorted(s for s in quote_syms if s in liquid_symbols)
    print(f"  {len(common)} liquid symbols have quotes")
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
            print(f"  spread: {idx + 1}/{len(common)} liquid symbols")
    median_hs = float(np.median(per_sym_hs)) if per_sym_hs else float("nan")
    print(f"  Liquid median half-spread: {median_hs:.2f} bps (n={len(per_sym_hs)} names)")
    return median_hs


def load_earnings_events(item_map_path: Path) -> pl.DataFrame:
    """Load item-2.02 8-Ks: join DB rows to the SEC item_map by accession_number."""
    print("Loading earnings (item 2.02) events...")
    with open(item_map_path) as json_file:
        cached = json.load(json_file)
    item_map = cached["item_map"]
    earnings_acc = {
        acc for acc, items in item_map.items()
        if "2.02" in [x.strip() for x in items.split(",")]
    }
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT symbol, available_at::date AS avail_date, available_at, accession_number, cik
        FROM filings
        WHERE form_type = '8-K' AND available_at >= '2025-12-15'
        ORDER BY symbol, available_at
        """
    )
    rows = cur.fetchall()
    conn.close()
    df = pl.DataFrame(
        {
            "symbol": [r[0] for r in rows],
            "available_at_date": [r[1] for r in rows],
            "available_at": [r[2] for r in rows],
            "accession_number": [r[3] for r in rows],
            "cik": [r[4] for r in rows],
        }
    )
    df = df.filter(pl.col("accession_number").is_in(list(earnings_acc)))
    print(f"  {len(df)} item-2.02 events ({df['symbol'].n_unique()} symbols)")
    return df


def assign_entry_dates(events: pl.DataFrame, trading_dates: list[date]) -> pl.DataFrame:
    trading_set = set(trading_dates)
    trading_sorted = sorted(trading_dates)
    next_td: dict[date, date | None] = {}
    for dt in trading_sorted:
        candidate = dt + timedelta(days=1)
        while True:
            if candidate in trading_set:
                next_td[dt] = candidate
                break
            candidate += timedelta(days=1)
            if candidate > trading_sorted[-1]:
                next_td[dt] = None
                break
    entry_dates = []
    for row in events.to_dicts():
        avail = row["available_at_date"]
        if isinstance(avail, datetime):
            avail = avail.date()
        entry_dates.append(next_td.get(avail))
    result = events.with_columns(pl.Series("entry_date", entry_dates, dtype=pl.Date))
    result = result.filter(pl.col("entry_date").is_not_null())
    print(f"  Events with valid entry date: {len(result)}")
    return result


def build_forward_returns(panel: pl.DataFrame, horizons: list[int]) -> pl.DataFrame:
    """open-entry forward returns (entry day's open -> close[t+H]) and
    drift-from-D+1-close forward returns (close[entry] -> close[entry+H]) for the signed leg."""
    result = panel.sort(["symbol", "bar_date"])
    for horizon in horizons:
        result = result.with_columns(
            (pl.col("close").shift(-horizon).over("symbol") / pl.col("open_price") - 1).alias(
                f"open_fwd_{horizon}d"
            ),
            (pl.col("close").shift(-horizon).over("symbol") / pl.col("close") - 1).alias(
                f"close_fwd_{horizon}d"
            ),
        )
    # D+1 reaction = entry day's open -> close (the immediate market reaction we sign by)
    result = result.with_columns(
        (pl.col("close") / pl.col("open_price") - 1).alias("reaction_d1")
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
    """Cohort(event) - control(non-event) per-symbol-demeaned day-clustered alpha, net of `cost`."""
    sym_means = panel_events.group_by("symbol").agg(pl.col(fwd_col).mean().alias("sym_mean"))
    panel_dm = panel_events.join(sym_means, on="symbol", how="left").with_columns(
        (pl.col(fwd_col) - pl.col("sym_mean")).alias("fwd_dm")
    )
    event_dates = panel_events.filter(pl.col("is_event"))["bar_date"].unique().to_list()
    raw_per_date: list[float] = []
    dm_per_date: list[float] = []
    n_events_total = 0
    for dt in sorted(event_dates):
        day = panel_events.filter((pl.col("bar_date") == dt) & pl.col(fwd_col).is_not_null())
        day_dm = panel_dm.filter((pl.col("bar_date") == dt) & pl.col(fwd_col).is_not_null())
        ev = day.filter(pl.col("is_event"))[fwd_col].to_numpy()
        ct = day.filter(~pl.col("is_event"))[fwd_col].to_numpy()
        if len(ev) == 0 or len(ct) == 0:
            continue
        n_events_total += len(ev)
        raw_per_date.append(float((ev - cost).mean()) - float(ct.mean()))
        ev_dm = day_dm.filter(pl.col("is_event"))["fwd_dm"].to_numpy()
        ct_dm = day_dm.filter(~pl.col("is_event"))["fwd_dm"].to_numpy()
        if len(ev_dm) and len(ct_dm):
            dm_per_date.append(float((ev_dm - cost).mean()) - float(ct_dm.mean()))
    raw_mean, raw_t, n_dates = day_clustered(raw_per_date)
    dm_mean, dm_t, _ = day_clustered(dm_per_date)

    # canary: permute event/control labels within each date
    canary_means: list[float] = []
    rng = random.Random(42)
    for seed in range(CANARY_SEEDS):
        rng.seed(seed)
        perm_per_date: list[float] = []
        for dt in sorted(event_dates):
            day = panel_events.filter((pl.col("bar_date") == dt) & pl.col(fwd_col).is_not_null())
            n_ev = int(day.filter(pl.col("is_event")).height)
            allret = day[fwd_col].to_list()
            if n_ev == 0 or len(allret) < 2:
                continue
            shuffled = allret.copy()
            rng.shuffle(shuffled)
            perm_ev, perm_ct = shuffled[:n_ev], shuffled[n_ev:]
            if perm_ct:
                perm_per_date.append(float(np.mean(perm_ev)) - float(np.mean(perm_ct)))
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


def signed_ls_trades(
    panel_events: pl.DataFrame, drift_col: str, cost: float
) -> tuple[np.ndarray, dict[str, Any]]:
    """Per-trade signed net returns: sign by D+1 reaction (relative to same-date control mean),
    payoff = sign * drift_col (close[entry]->close[entry+H]), minus round-trip cost.
    Returns (per_trade_array, day_clustered_summary)."""
    # date-control reaction mean for abnormal reaction
    ctrl_react = (
        panel_events.filter(~pl.col("is_event"))
        .group_by("bar_date")
        .agg(pl.col("reaction_d1").mean().alias("ctrl_react_mean"))
    )
    ev = panel_events.filter(pl.col("is_event")).join(ctrl_react, on="bar_date", how="left")
    ev = ev.with_columns(
        (pl.col("reaction_d1") - pl.col("ctrl_react_mean").fill_null(0.0)).alias("abn_reaction")
    ).filter(pl.col(drift_col).is_not_null() & pl.col("abn_reaction").is_not_null())
    # sign by abnormal reaction; drop ~zero reactions
    ev = ev.filter(pl.col("abn_reaction").abs() > 1e-6)
    ev = ev.with_columns(
        (pl.col("abn_reaction").sign() * pl.col(drift_col) - cost).alias("signed_net")
    )
    trades = ev["signed_net"].to_numpy()

    # day-clustered mean of the signed L/S leg (control-neutralized already via per-date sign baseline)
    per_date: list[float] = []
    for dt in ev["bar_date"].unique().to_list():
        day = ev.filter(pl.col("bar_date") == dt)["signed_net"].to_numpy()
        if len(day):
            per_date.append(float(day.mean()))
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
    if len(trades) < 20:
        return {"lo_pct": None, "hi_pct": None, "mean_pct": None, "n": int(len(trades))}
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
    """Tag the forward panel (restricted to cohort symbols) with is_event at each (symbol, entry_date)."""
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
    print(f"\n=== {label} ({len(cohort_symbols)} symbols) ===")
    panel = build_event_panel(fwd_panel, events, cohort_symbols)
    out: dict[str, Any] = {"label": label, "n_symbols": len(cohort_symbols), "horizons": {}}
    for horizon in HORIZONS:
        open_col = f"open_fwd_{horizon}d"
        drift_col = f"close_fwd_{horizon}d"
        h_out: dict[str, Any] = {}

        # Headline (un-signed) drift, full / OOS, net at measured cost
        full = panel.filter(pl.col(open_col).is_not_null())
        oos = panel.filter(pl.col("bar_date").is_in(list(oos_dates)) & pl.col(open_col).is_not_null())
        train = panel.filter(pl.col("bar_date").is_in(list(train_dates)) & pl.col(open_col).is_not_null())
        h_out["headline_full_net"] = headline_drift(full, open_col, cost_rt)
        h_out["headline_train_net"] = headline_drift(train, open_col, cost_rt)
        h_out["headline_oos_net"] = headline_drift(oos, open_col, cost_rt)
        h_out["headline_full_gross"] = headline_drift(full, open_col, 0.0)

        # Signed L/S per-trade (the tradeable bet) — full & OOS, measured cost + 2x
        trades_full, sum_full = signed_ls_trades(full, drift_col, cost_rt)
        trades_oos, sum_oos = signed_ls_trades(oos, drift_col, cost_rt)
        trades_oos_2x, sum_oos_2x = signed_ls_trades(oos, drift_col, cost_rt_2x)
        trades_oos_gross, sum_oos_gross = signed_ls_trades(oos, drift_col, 0.0)
        h_out["signed_full_net"] = {**sum_full, "bootstrap": bootstrap_ci(trades_full)}
        h_out["signed_oos_net"] = {**sum_oos, "bootstrap": bootstrap_ci(trades_oos)}
        h_out["signed_oos_net_2x"] = {**sum_oos_2x, "bootstrap": bootstrap_ci(trades_oos_2x)}
        h_out["signed_oos_gross"] = {**sum_oos_gross, "bootstrap": bootstrap_ci(trades_oos_gross)}

        out["horizons"][f"{horizon}d"] = h_out
        sig = h_out["signed_oos_net"]
        boot = sig["bootstrap"]
        print(
            f"  H={horizon:2d}d | headline OOS dm%={h_out['headline_oos_net']['alpha_demean_pct']} "
            f"| signed OOS net%={sig['mean_net_pct']} CI=[{boot['lo_pct']},{boot['hi_pct']}] "
            f"n={boot['n']} excl0>{boot.get('ci_excludes_zero_above')}"
        )
    return out


def main() -> None:
    print("=== W2: item-2.02 PEAD on LIQUID names ===\n")
    panel = load_daily_panel()
    trading_dates_all: list[date] = sorted(panel["bar_date"].unique().to_list())
    n_dates = len(trading_dates_all)
    print(f"Panel: {panel['symbol'].n_unique()} symbols, {n_dates} dates "
          f"({trading_dates_all[0]} to {trading_dates_all[-1]})")

    adv = compute_adv_dollar(panel)
    adv = assign_liquidity_tertile(adv)
    liquid_symbols = set(adv.filter(pl.col("adv_tertile") == "liquid")["symbol"].to_list())
    mid_symbols = set(adv.filter(pl.col("adv_tertile") == "mid")["symbol"].to_list())
    illiquid_symbols = set(adv.filter(pl.col("adv_tertile") == "illiquid")["symbol"].to_list())
    all_symbols = set(adv["symbol"].to_list())
    megacap_symbols = set(adv.sort("adv_dollar", descending=True).head(MEGACAP_N)["symbol"].to_list())
    print(f"Tertiles: liquid={len(liquid_symbols)} mid={len(mid_symbols)} illiquid={len(illiquid_symbols)}")

    liquid_hs = measure_liquid_half_spread(liquid_symbols)
    cost_rt = 2.0 * (liquid_hs / 10000.0) if math.isfinite(liquid_hs) else 0.0
    cost_rt_2x = 2.0 * (2.0 * liquid_hs / 10000.0) if math.isfinite(liquid_hs) else 0.0
    print(f"Liquid round-trip cost: {cost_rt*10000:.1f} bps (1x), {cost_rt_2x*10000:.1f} bps (2x)")

    events = load_earnings_events(OUTPUT_DIR / "item_map.json")
    events = assign_entry_dates(events, trading_dates_all)
    events = events.filter(pl.col("symbol").is_in(list(all_symbols)))
    n_liquid_events = events.filter(pl.col("symbol").is_in(list(liquid_symbols))).height
    print(f"Earnings events with bars: {len(events)} ; in LIQUID tier: {n_liquid_events}")

    fwd_panel = build_forward_returns(panel, HORIZONS)

    split_idx = n_dates // 2
    train_dates_set = set(trading_dates_all[:split_idx])
    oos_dates_set = set(trading_dates_all[split_idx:])
    print(f"Split: TRAIN {sorted(train_dates_set)[0]}..{sorted(train_dates_set)[-1]} "
          f"({len(train_dates_set)}d) | OOS {sorted(oos_dates_set)[0]}..{sorted(oos_dates_set)[-1]} "
          f"({len(oos_dates_set)}d)")

    cohorts = [
        ("LIQUID tertile (PRIMARY)", liquid_symbols),
        ("Top-100 megacap", megacap_symbols),
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
            "n_earnings_events_total": len(events),
            "n_earnings_events_liquid": n_liquid_events,
            "liquid_half_spread_bps": liquid_hs,
            "cost_rt_bps": cost_rt * 10000,
            "cost_rt_2x_bps": cost_rt_2x * 10000,
            "n_liquid_symbols": len(liquid_symbols),
            "n_megacap_symbols": len(megacap_symbols),
            "horizons": HORIZONS,
            "oos_start": str(sorted(oos_dates_set)[0]),
        },
        "cohorts": {},
    }
    for label, syms in cohorts:
        results["cohorts"][label] = run_cohort(
            fwd_panel, events, syms, label, cost_rt, cost_rt_2x, oos_dates_set, train_dates_set
        )

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_DIR / "raw_results_w2.json", "w") as json_file:
        json.dump(results, json_file, indent=2, default=str)
    print(f"\nSaved raw_results_w2.json")


if __name__ == "__main__":
    main()
