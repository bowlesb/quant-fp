"""
H10b: 8-K Drift Escalation — Walk-forward OOS + PEAD split + survivorship + open-entry

Stresses H10's FIRST KEEP (8-K 1-5d demeaned drift +2.95%/+5.69%/+5.53%, t 1.97/3.05/2.96).
Five escalation gates:
  1. Walk-forward OOS split (first ~63 / last ~63 trading days) — LOAD-BEARING
  2. Item-code PEAD split (2.02 earnings vs non-earnings, via SEC EDGAR submissions API)
  3. Survivorship stress (top-tercile dollar-volume)
  4. Tradeable entry realism (D+1 open price, net ~6 bps round-trip)
  5. 10-seed shuffle canary on every cell

CRITICAL: within each split the per-symbol demean is computed ONLY from dates in that split.
No cross-split leakage.

UTC time note: bars ts is genuine UTC; 09:30 ET (EDT, summer) = 13:30 UTC.
RTH filter: UTC hour in [13, 21]. Open bar = FIRST bar where UTC hour >= 13 and minute >= 30
(i.e., ts.hour==13 and ts.minute>=30, or ts.hour>13).
"""

import json
import os
import random
import time
import urllib.request
from datetime import date, timedelta, datetime
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl
import psycopg

BARS_ROOT = Path("/store/raw/bars")
OUTPUT_DIR = Path("/app/experiments/2026-06-16-h10b-8k-drift-escalation")
HORIZONS = [1, 3, 5]
CANARY_SEEDS = 10
RTH_OPEN_UTC_HOUR = 13
RTH_OPEN_UTC_MINUTE = 30
RTH_CLOSE_UTC_HOUR = 21
ROUND_TRIP_BPS = 6  # ~6 bps round-trip cost


def get_db_connection() -> psycopg.Connection[Any]:
    return psycopg.connect(
        host=os.environ["DB_HOST"],
        port=os.environ["DB_PORT"],
        dbname=os.environ["DB_NAME"],
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
    )


def load_daily_panel_with_open() -> pl.DataFrame:
    """
    Build (symbol, date, close, open_price, dollar_vol) panel from minute bars.
    - close: last bar with UTC hour in [13, 21]
    - open_price: first bar with UTC time >= 13:30 (i.e., hour==13, min>=30, or hour>13)
    - dollar_vol: sum(close * volume) over RTH bars for liquidity sorting
    """
    print("Loading daily panel (close + open + dollar_vol) from bars store...")
    all_frames: list[pl.DataFrame] = []

    symbol_dirs = list(BARS_ROOT.iterdir())
    total = len(symbol_dirs)
    print(f"  Found {total} symbol directories")

    for idx, sym_dir in enumerate(symbol_dirs):
        if not sym_dir.is_dir():
            continue
        symbol = sym_dir.name.split("=", 1)[1]

        sym_frames: list[pl.DataFrame] = []
        for date_dir in sym_dir.iterdir():
            if not date_dir.is_dir():
                continue
            parquet_path = date_dir / "data.parquet"
            if not parquet_path.exists():
                continue
            try:
                df = pl.read_parquet(parquet_path, columns=["ts", "close", "volume"])
                sym_frames.append(df)
            except Exception as exc:  # noqa: BLE001
                print(f"  WARNING: failed to read {parquet_path}: {exc}")
                continue

        if not sym_frames:
            continue

        sym_all = pl.concat(sym_frames, rechunk=True)
        sym_all = sym_all.with_columns(
            pl.col("ts").dt.hour().alias("utc_hour"),
            pl.col("ts").dt.minute().alias("utc_minute"),
            pl.col("ts").dt.date().alias("bar_date"),
        ).filter((pl.col("utc_hour") >= RTH_OPEN_UTC_HOUR) & (pl.col("utc_hour") <= RTH_CLOSE_UTC_HOUR))

        if sym_all.is_empty():
            continue

        # Mark open bars: hour==13 and minute>=30, OR hour>13
        sym_all = sym_all.with_columns(
            (
                ((pl.col("utc_hour") == 13) & (pl.col("utc_minute") >= 30))
                | (pl.col("utc_hour") > 13)
            ).alias("is_open_eligible")
        )

        # Close = last bar per date (RTH)
        daily_close = (
            sym_all.sort("ts")
            .group_by("bar_date")
            .agg(pl.col("close").last().alias("close"))
        )

        # Open = first RTH open-eligible bar per date
        daily_open = (
            sym_all.filter(pl.col("is_open_eligible"))
            .sort("ts")
            .group_by("bar_date")
            .agg(pl.col("close").first().alias("open_price"))
        )

        # Dollar vol = sum(close * volume) over RTH
        daily_dvol = (
            sym_all.with_columns(
                (pl.col("close") * pl.col("volume")).alias("dollar_bar")
            )
            .group_by("bar_date")
            .agg(pl.col("dollar_bar").sum().alias("dollar_vol"))
        )

        daily = (
            daily_close
            .join(daily_open, on="bar_date", how="left")
            .join(daily_dvol, on="bar_date", how="left")
            .with_columns(pl.lit(symbol).alias("symbol"))
            .select(["symbol", "bar_date", "close", "open_price", "dollar_vol"])
        )
        all_frames.append(daily)

        if (idx + 1) % 1000 == 0:
            print(f"  Processed {idx + 1}/{total} symbols")

    print(f"  Concatenating {len(all_frames)} symbol frames...")
    panel = pl.concat(all_frames, rechunk=True).sort(["symbol", "bar_date"])
    print(f"  Panel shape: {panel.shape}")
    return panel


def build_forward_returns(panel: pl.DataFrame, horizons: list[int]) -> pl.DataFrame:
    """
    For each (symbol, date), compute close-to-close forward returns at each horizon.
    Also compute open-to-close[t+h] returns (entry at D+1 open, exit at close[t+h]).
    open_fwd_h = close[t+h] / open_price[t] - 1  (where t is the entry date)
    """
    print("Building forward returns...")
    result = panel.sort(["symbol", "bar_date"])
    for horizon in horizons:
        # Close-to-close
        result = result.with_columns(
            (pl.col("close").shift(-horizon).over("symbol") / pl.col("close") - 1).alias(f"fwd_{horizon}d")
        )
        # Open-entry: use open_price as entry, exit at close shifted by horizon
        result = result.with_columns(
            (pl.col("close").shift(-horizon).over("symbol") / pl.col("open_price") - 1).alias(f"open_fwd_{horizon}d")
        )
    print(f"  Forward return panel shape: {result.shape}")
    return result


def load_filings_8k() -> pl.DataFrame:
    """Load 8-K filings from DB."""
    print("Loading 8-K filings from DB...")
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT symbol, form_type,
               available_at::date AS available_at_date,
               available_at,
               available_at_source,
               accession_number,
               cik,
               link
        FROM filings
        WHERE form_type = '8-K'
          AND available_at >= '2025-12-15'
        ORDER BY symbol, available_at
        """,
    )
    rows = cur.fetchall()
    conn.close()

    df = pl.DataFrame(
        {
            "symbol": [r[0] for r in rows],
            "form_type": [r[1] for r in rows],
            "available_at_date": [r[2] for r in rows],
            "available_at": [r[3] for r in rows],
            "available_at_source": [r[4] for r in rows],
            "accession_number": [r[5] for r in rows],
            "cik": [r[6] for r in rows],
            "link": [r[7] for r in rows],
        }
    )
    print(f"  Loaded {len(df)} 8-K filings ({df['symbol'].n_unique()} symbols)")
    return df


def assign_entry_dates(filings: pl.DataFrame, trading_dates: list[date]) -> pl.DataFrame:
    """Assign next-trading-day entry date after available_at_date."""
    trading_dates_set = set(trading_dates)
    trading_dates_sorted = sorted(trading_dates)
    next_td: dict[date, date | None] = {}
    for dt in trading_dates_sorted:
        candidate = dt + timedelta(days=1)
        while True:
            if candidate in trading_dates_set:
                next_td[dt] = candidate
                break
            candidate += timedelta(days=1)
            if candidate > trading_dates_sorted[-1]:
                next_td[dt] = None
                break

    rows = filings.to_dicts()
    entry_dates = []
    for row in rows:
        avail_date = row["available_at_date"]
        if isinstance(avail_date, datetime):
            avail_date = avail_date.date()
        entry = next_td.get(avail_date)
        entry_dates.append(entry)

    result = filings.with_columns(pl.Series("entry_date", entry_dates, dtype=pl.Date))
    result = result.filter(pl.col("entry_date").is_not_null())
    print(f"  Filings with valid entry date: {len(result)}")
    return result


def fetch_item_codes_for_sample(filings: pl.DataFrame, max_filings: int = 1200) -> dict[str, str]:
    """
    Fetch 8-K item codes for a SAMPLE of filings via SEC EDGAR submissions API.
    Returns dict: accession_number (normalized) -> items string (e.g. "2.02,8.01")
    Uses CIK to query the submissions endpoint, which has an 'items' field per filing.
    Rate-limited to ~10 req/sec (SEC guideline).
    """
    print(f"Fetching item codes for up to {max_filings} 8-K filings via SEC API...")

    # Sample randomly across the full range
    rng = random.Random(42)
    sample_idx = list(range(len(filings)))
    rng.shuffle(sample_idx)
    sample_idx = sample_idx[:max_filings]
    sampled = filings[sample_idx]

    # Group by CIK to minimize API calls (one API call per CIK fetches all filings)
    cik_to_accessions: dict[str, list[str]] = {}
    for row in sampled.to_dicts():
        cik = row["cik"]
        acc = row["accession_number"].replace("-", "")  # normalized: no dashes
        if cik not in cik_to_accessions:
            cik_to_accessions[cik] = []
        cik_to_accessions[cik].append(acc)

    print(f"  Sample covers {len(cik_to_accessions)} unique CIKs, {len(sampled)} filings")

    acc_to_items: dict[str, str] = {}
    user_agent = "research-bot ben.bowles@gmail.com"
    n_ciks = len(cik_to_accessions)
    n_fetched = 0
    n_errors = 0

    cik_list = list(cik_to_accessions.items())
    rng.shuffle(cik_list)  # random order to spread across EDGAR

    for cik_idx, (cik, target_accessions) in enumerate(cik_list):
        target_set = set(target_accessions)
        cik_int = int(cik)
        url = f"https://data.sec.gov/submissions/CIK{cik_int:010d}.json"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": user_agent})
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())
        except Exception as exc:  # noqa: BLE001
            n_errors += 1
            if n_errors <= 5:
                print(f"  WARNING: failed to fetch CIK {cik}: {exc}")
            continue

        recent = data.get("filings", {}).get("recent", {})
        acc_list = recent.get("accessionNumber", [])
        items_list = recent.get("items", [])
        forms_list = recent.get("form", [])

        # Parse all older filing pages if needed (SEC paginates at ~40 entries)
        older_files = data.get("filings", {}).get("files", [])
        # For simplicity, only use recent (covers most of our 2025-2026 window)
        # and check if targets are covered
        for acc_raw, items_raw, form_raw in zip(acc_list, items_list, forms_list):
            acc_norm = acc_raw.replace("-", "")
            if acc_norm in target_set and form_raw == "8-K":
                acc_to_items[acc_norm] = str(items_raw) if items_raw else ""

        n_fetched += 1
        # Rate limit: ~8 req/sec to stay well under SEC's 10/sec limit
        if cik_idx % 50 == 49:
            print(f"  Progress: {cik_idx + 1}/{n_ciks} CIKs, {len(acc_to_items)} item codes found")
        time.sleep(0.12)

    print(f"  Done: {len(acc_to_items)} item codes fetched ({n_errors} errors, {n_fetched} CIKs queried)")
    return acc_to_items


def split_by_items(filings: pl.DataFrame, acc_to_items: dict[str, str]) -> tuple[pl.DataFrame, pl.DataFrame]:
    """
    Split filings into earnings (contains item 2.02) and non-earnings.
    Returns (earnings_df, non_earnings_df) — only from the sampled filings that got item codes.
    """
    earnings_accessions = set()
    non_earnings_accessions = set()

    for row in filings.to_dicts():
        acc_norm = row["accession_number"].replace("-", "")
        if acc_norm not in acc_to_items:
            continue
        items_str = acc_to_items[acc_norm]
        if "2.02" in items_str:
            earnings_accessions.add(row["accession_number"])
        else:
            non_earnings_accessions.add(row["accession_number"])

    earnings_df = filings.filter(pl.col("accession_number").is_in(list(earnings_accessions)))
    non_earnings_df = filings.filter(pl.col("accession_number").is_in(list(non_earnings_accessions)))
    print(f"  Item split: {len(earnings_accessions)} earnings (2.02), {len(non_earnings_accessions)} non-earnings")
    return earnings_df, non_earnings_df


def compute_liquid_tertile_mask(panel: pl.DataFrame) -> set[str]:
    """
    Return the set of symbols in the top tercile by median daily dollar-volume.
    """
    sym_dvol = (
        panel.group_by("symbol")
        .agg(pl.col("dollar_vol").median().alias("median_dvol"))
        .sort("median_dvol", descending=True)
    )
    n = len(sym_dvol)
    top_n = n // 3
    top_symbols = set(sym_dvol["symbol"].head(top_n).to_list())
    print(f"  Liquid tertile: {len(top_symbols)} / {n} symbols")
    return top_symbols


def compute_horizon_stats_split(
    panel: pl.DataFrame,
    fwd_col: str,
    horizon: int,
    n_canary_seeds: int,
    is_open_entry: bool = False,
    cost_bps: float = 0.0,
) -> dict[str, Any]:
    """
    Compute day-clustered alpha + t-stat for a single horizon on a single panel slice.
    Per-symbol demean is computed WITHIN this panel slice only (no leakage).
    """
    cost = cost_bps / 10000.0  # convert to decimal

    dates_with_events = (
        panel.filter(pl.col("is_event"))["bar_date"].unique().to_list()
    )

    alpha_per_date: list[float] = []
    dm_alpha_per_date: list[float] = []
    event_counts: list[int] = []

    # Compute per-symbol demean WITHIN this panel slice
    sym_means = (
        panel.group_by("symbol")
        .agg(pl.col(fwd_col).mean().alias("sym_mean"))
    )
    panel_dm = panel.join(sym_means, on="symbol", how="left").with_columns(
        (pl.col(fwd_col) - pl.col("sym_mean")).alias(f"{fwd_col}_dm")
    )

    for dt in sorted(dates_with_events):
        day_data = panel.filter(pl.col("bar_date") == dt).filter(pl.col(fwd_col).is_not_null())
        day_data_dm = panel_dm.filter(pl.col("bar_date") == dt).filter(pl.col(fwd_col).is_not_null())

        event_ret = day_data.filter(pl.col("is_event"))[fwd_col].to_list()
        ctrl_ret = day_data.filter(~pl.col("is_event"))[fwd_col].to_list()
        event_ret_dm = day_data_dm.filter(pl.col("is_event"))[f"{fwd_col}_dm"].to_list()
        ctrl_ret_dm = day_data_dm.filter(~pl.col("is_event"))[f"{fwd_col}_dm"].to_list()

        if not event_ret or not ctrl_ret:
            continue

        # Net of cost (subtract round-trip from event return)
        event_ret_net = [r - cost for r in event_ret]
        alpha_per_date.append(float(np.mean(event_ret_net)) - float(np.mean(ctrl_ret)))
        event_counts.append(len(event_ret))

        if event_ret_dm and ctrl_ret_dm:
            event_dm_net = [r - cost for r in event_ret_dm]
            dm_alpha_per_date.append(float(np.mean(event_dm_net)) - float(np.mean(ctrl_ret_dm)))

    n_dates = len(alpha_per_date)
    if n_dates < 5:
        nan = float("nan")
        return {
            "n_event_obs": sum(event_counts),
            "n_dates": n_dates,
            "alpha_mean_pct": nan,
            "t_stat": nan,
            "alpha_demean_pct": nan,
            "t_stat_demean": nan,
            "canary_mean_pct": nan,
            "canary_p95_pct": nan,
        }

    alpha_arr = np.array(alpha_per_date)
    alpha_mean = float(np.mean(alpha_arr))
    alpha_std = float(np.std(alpha_arr, ddof=1))
    t_stat = alpha_mean / (alpha_std / np.sqrt(n_dates)) if alpha_std > 0 else float("nan")

    dm_arr = np.array(dm_alpha_per_date) if dm_alpha_per_date else np.array([float("nan")])
    alpha_dm = float(np.mean(dm_arr))
    dm_std = float(np.std(dm_arr, ddof=1)) if len(dm_arr) > 1 else float("nan")
    t_dm = (
        alpha_dm / (dm_std / np.sqrt(len(dm_arr)))
        if dm_std and dm_std > 0 and len(dm_arr) > 1
        else float("nan")
    )

    # Canary (permute within each date)
    canary_alphas: list[float] = []
    rng = random.Random(42)
    for seed in range(n_canary_seeds):
        rng.seed(seed)
        perm_per_date: list[float] = []
        for dt in sorted(dates_with_events):
            day_data = panel.filter(pl.col("bar_date") == dt).filter(pl.col(fwd_col).is_not_null())
            n_ev = int(day_data.filter(pl.col("is_event")).height)
            if n_ev == 0:
                continue
            all_ret = day_data[fwd_col].to_list()
            if len(all_ret) < 2:
                continue
            shuffled = all_ret.copy()
            rng.shuffle(shuffled)
            perm_ev = shuffled[:n_ev]
            perm_ctrl = shuffled[n_ev:]
            if not perm_ctrl:
                continue
            perm_per_date.append(float(np.mean(perm_ev)) - float(np.mean(perm_ctrl)))
        if perm_per_date:
            canary_alphas.append(float(np.mean(perm_per_date)))

    canary_mean = float(np.mean(canary_alphas)) if canary_alphas else float("nan")
    canary_p95 = float(np.percentile(canary_alphas, 95)) if canary_alphas else float("nan")

    return {
        "n_event_obs": sum(event_counts),
        "n_dates": n_dates,
        "alpha_mean_pct": alpha_mean * 100,
        "t_stat": t_stat,
        "alpha_demean_pct": alpha_dm * 100,
        "t_stat_demean": t_dm,
        "canary_mean_pct": canary_mean * 100,
        "canary_p95_pct": canary_p95 * 100,
    }


def run_analysis(
    label: str,
    fwd_panel: pl.DataFrame,
    event_df: pl.DataFrame,
    horizons: list[int],
    split_dates: tuple[list[date], list[date]] | None = None,
    liquid_symbols: set[str] | None = None,
    use_open_entry: bool = False,
    cost_bps: float = 0.0,
) -> dict[str, Any]:
    """
    Run the full cohort analysis for a given label/subset.
    split_dates: (train_dates, oos_dates) if walk-forward; None for full period.
    liquid_symbols: restrict to this set if provided.
    use_open_entry: use open_fwd_{h}d instead of fwd_{h}d.
    """
    print(f"\n--- {label} ---")

    # Build event join
    event_entries = event_df.select(["symbol", "entry_date"]).unique(subset=["symbol", "entry_date"])
    fwd_with_event = fwd_panel.join(
        event_entries.rename({"entry_date": "bar_date"}).with_columns(pl.lit(True).alias("is_event")),
        on=["symbol", "bar_date"],
        how="left",
    ).with_columns(pl.col("is_event").fill_null(False))

    if liquid_symbols is not None:
        fwd_with_event = fwd_with_event.filter(pl.col("symbol").is_in(list(liquid_symbols)))

    results: dict[str, Any] = {"label": label}

    for horizon in horizons:
        fwd_col = f"open_fwd_{horizon}d" if use_open_entry else f"fwd_{horizon}d"
        panel_h = fwd_with_event.filter(pl.col(fwd_col).is_not_null())

        if split_dates is not None:
            train_dates, oos_dates = split_dates
            train_set = set(train_dates)
            oos_set = set(oos_dates)

            train_panel = panel_h.filter(pl.col("bar_date").is_in(list(train_set)))
            oos_panel = panel_h.filter(pl.col("bar_date").is_in(list(oos_set)))

            train_stats = compute_horizon_stats_split(
                train_panel, fwd_col, horizon, CANARY_SEEDS, use_open_entry, cost_bps
            )
            oos_stats = compute_horizon_stats_split(
                oos_panel, fwd_col, horizon, CANARY_SEEDS, use_open_entry, cost_bps
            )

            print(
                f"  h={horizon}d TRAIN: n={train_stats['n_event_obs']}, "
                f"alpha_dm={train_stats['alpha_demean_pct']:.3f}%, t_dm={train_stats['t_stat_demean']:.2f}"
            )
            print(
                f"  h={horizon}d OOS:   n={oos_stats['n_event_obs']}, "
                f"alpha_dm={oos_stats['alpha_demean_pct']:.3f}%, t_dm={oos_stats['t_stat_demean']:.2f}"
            )

            results[f"horizon_{horizon}d"] = {
                "train": train_stats,
                "oos": oos_stats,
            }
        else:
            full_stats = compute_horizon_stats_split(
                panel_h, fwd_col, horizon, CANARY_SEEDS, use_open_entry, cost_bps
            )
            print(
                f"  h={horizon}d: n={full_stats['n_event_obs']}, "
                f"alpha_dm={full_stats['alpha_demean_pct']:.3f}%, t_dm={full_stats['t_stat_demean']:.2f}"
            )
            results[f"horizon_{horizon}d"] = full_stats

    return results


def main() -> None:
    print("=== H10b: 8-K Drift Escalation ===\n")

    # 1. Load panel
    panel = load_daily_panel_with_open()

    trading_dates_all: list[date] = sorted(panel["bar_date"].unique().to_list())
    n_dates = len(trading_dates_all)
    print(f"Trading date range: {trading_dates_all[0]} to {trading_dates_all[-1]} ({n_dates} dates)")

    # 2. Build forward returns
    fwd_panel = build_forward_returns(panel, HORIZONS)

    # 3. Load and filter 8-K filings
    filings_8k = load_filings_8k()
    filings_8k = assign_entry_dates(filings_8k, trading_dates_all)
    bar_symbols = set(panel["symbol"].unique().to_list())
    filings_8k = filings_8k.filter(pl.col("symbol").is_in(list(bar_symbols)))
    print(f"8-K filings with matching bars: {len(filings_8k)} ({filings_8k['symbol'].n_unique()} symbols)")

    # 4. Train/OOS split
    split_idx = n_dates // 2
    train_dates = trading_dates_all[:split_idx]
    oos_dates = trading_dates_all[split_idx:]
    print(f"\nSplit: TRAIN={train_dates[0]} to {train_dates[-1]} ({len(train_dates)} days)")
    print(f"       OOS={oos_dates[0]} to {oos_dates[-1]} ({len(oos_dates)} days)")

    # 5. Liquid tertile
    liquid_symbols = compute_liquid_tertile_mask(panel)

    # 6. Fetch item codes (PEAD split)
    acc_to_items = fetch_item_codes_for_sample(filings_8k, max_filings=1200)
    earnings_filings, non_earnings_filings = split_by_items(filings_8k, acc_to_items)
    print(f"  Earnings (2.02): {len(earnings_filings)}, Non-earnings: {len(non_earnings_filings)}")

    # 7. Run all analyses
    all_results: dict[str, Any] = {}

    # 7a. Walk-forward OOS: ALL 8-Ks (close-entry)
    all_results["wf_all_8k_close"] = run_analysis(
        "Walk-forward ALL 8-K (close entry)",
        fwd_panel,
        filings_8k,
        HORIZONS,
        split_dates=(train_dates, oos_dates),
    )

    # 7b. Walk-forward OOS: earnings (2.02) 8-Ks (close-entry)
    if len(earnings_filings) > 10:
        all_results["wf_earnings_8k"] = run_analysis(
            "Walk-forward EARNINGS 8-K (2.02, close entry)",
            fwd_panel,
            earnings_filings,
            HORIZONS,
            split_dates=(train_dates, oos_dates),
        )
    else:
        print("Too few earnings filings parsed — skipping earnings WF")
        all_results["wf_earnings_8k"] = {"label": "SKIPPED (too few)", "note": "too few earnings filings"}

    # 7c. Walk-forward OOS: non-earnings 8-Ks (close-entry)
    if len(non_earnings_filings) > 10:
        all_results["wf_non_earnings_8k"] = run_analysis(
            "Walk-forward NON-EARNINGS 8-K (close entry)",
            fwd_panel,
            non_earnings_filings,
            HORIZONS,
            split_dates=(train_dates, oos_dates),
        )
    else:
        print("Too few non-earnings filings parsed — skipping non-earnings WF")
        all_results["wf_non_earnings_8k"] = {"label": "SKIPPED (too few)", "note": "too few non-earnings filings"}

    # 7d. Survivorship stress: liquid tertile, ALL 8-Ks (OOS only)
    all_results["oos_liquid_tertile"] = run_analysis(
        "OOS liquid-tertile 8-K (close entry)",
        fwd_panel,
        filings_8k,
        HORIZONS,
        split_dates=(train_dates, oos_dates),
        liquid_symbols=liquid_symbols,
    )

    # 7e. Tradeable open-entry, ALL 8-Ks, net of ~6 bps (OOS only)
    all_results["oos_open_entry"] = run_analysis(
        "OOS open-entry 8-K (D+1 open, net 6bps)",
        fwd_panel,
        filings_8k,
        HORIZONS,
        split_dates=(train_dates, oos_dates),
        use_open_entry=True,
        cost_bps=ROUND_TRIP_BPS,
    )

    # 8. Save raw results
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    results_path = OUTPUT_DIR / "raw_results_h10b.json"
    with open(results_path, "w") as json_file:
        json.dump(all_results, json_file, indent=2, default=str)
    print(f"\nRaw results saved to {results_path}")

    # 9. Print summary
    print("\n" + "=" * 70)
    print("SUMMARY: Walk-forward OOS — ALL 8-Ks")
    print("=" * 70)
    wf = all_results["wf_all_8k_close"]
    for horizon in HORIZONS:
        key = f"horizon_{horizon}d"
        if key not in wf:
            continue
        train_s = wf[key]["train"]
        oos_s = wf[key]["oos"]
        print(
            f"  {horizon}d | TRAIN: n={train_s['n_event_obs']:5d}, "
            f"dm%={train_s['alpha_demean_pct']:+.2f}, t_dm={train_s['t_stat_demean']:+.2f} | "
            f"OOS: n={oos_s['n_event_obs']:5d}, "
            f"dm%={oos_s['alpha_demean_pct']:+.2f}, t_dm={oos_s['t_stat_demean']:+.2f}"
        )

    oos_ts = []
    for horizon in HORIZONS:
        key = f"horizon_{horizon}d"
        if key in wf and "oos" in wf[key]:
            t = wf[key]["oos"].get("t_stat_demean", float("nan"))
            oos_ts.append((horizon, t))

    best_oos_t = max((abs(t) for _, t in oos_ts if not np.isnan(t)), default=float("nan"))
    oos_holds = any(t >= 2.0 for _, t in oos_ts if not np.isnan(t))
    print(f"\n  OOS demeaned t >= 2.0: {'YES' if oos_holds else 'NO'} (best t={best_oos_t:.2f})")

    print("\nDone.")


if __name__ == "__main__":
    main()
