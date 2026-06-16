"""
H13: Re-cost the illiquid 8-K signal at Ben's $5-10K/position scale.

Strategy:
1. Build ADV_dollar per name from bars (median 20d close*volume RTH)
2. Measure half_spread for quoted names (2504 symbols); fit spread model for illiquid names
3. Build impact_bps = k * sqrt(order_notional / ADV_dollar), k=10, orders $5K and $10K
4. Re-score H10b's ILLIQUID tertile (the alpha concentration point) net of small-size cost
5. Capacity ceiling sweep

UTC time note: bars ts is genuine UTC; 09:30 ET (EDT, summer) = 13:30 UTC.
RTH filter: UTC hour in [13, 21]. Open bar = first bar where UTC hour==13 and minute>=30, or hour>13.
"""

import json
import math
import os
import random
from datetime import date, timedelta, datetime
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl
import psycopg

BARS_ROOT = Path("/store/raw/bars")
QUOTES_ROOT = Path("/store/raw/quotes")
OUTPUT_DIR = Path("/app/experiments/2026-06-16-h13-smallcap-recost")

HORIZONS = [1, 3, 5]
CANARY_SEEDS = 10

# UTC RTH constants (genuine UTC: 13:30 UTC = 09:30 ET in summer)
RTH_OPEN_UTC_HOUR = 13
RTH_OPEN_UTC_MINUTE = 30
RTH_CLOSE_UTC_HOUR = 21

# Impact model params
IMPACT_K = 10.0          # square-root impact coefficient (standard Almgren-Chriss k~10)
ORDER_SIZES = [5_000, 10_000]  # dollars per position

# ADV window
ADV_DAYS = 20

# Spread fit: half_spread_bps ~ a + b*log10(ADV_dollar) + c*(1/price)
# fit on quoted names, extrapolate to illiquid
RTH_FILTER_HOURS = {13, 14, 15, 16, 17, 18, 19, 20, 21}


def get_db_connection() -> psycopg.Connection[Any]:
    return psycopg.connect(
        host=os.environ["DB_HOST"],
        port=os.environ["DB_PORT"],
        dbname=os.environ["DB_NAME"],
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
    )


def load_daily_panel() -> pl.DataFrame:
    """
    Build (symbol, date, close, open_price, dollar_vol, price) panel from minute bars.
    - close: last RTH bar per date
    - open_price: first RTH bar >= 13:30 UTC per date
    - dollar_vol: sum(close * volume) over RTH bars (for ADV)
    """
    print("Loading daily bar panel...")
    all_frames: list[pl.DataFrame] = []
    symbol_dirs = list(BARS_ROOT.iterdir())
    total = len(symbol_dirs)
    print(f"  {total} symbol directories")

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
                print(f"  WARNING: read failed {parquet_path}: {exc}")
                continue

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
            sym_all.sort("ts")
            .group_by("bar_date")
            .agg(pl.col("close").last().alias("close"))
        )
        daily_open = (
            sym_all.filter(pl.col("is_open_eligible"))
            .sort("ts")
            .group_by("bar_date")
            .agg(pl.col("close").first().alias("open_price"))
        )
        daily_dvol = (
            sym_all.with_columns((pl.col("close") * pl.col("volume")).alias("dollar_bar"))
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
            print(f"  {idx + 1}/{total} symbols done")

    print(f"  Concatenating {len(all_frames)} symbol frames...")
    panel = pl.concat(all_frames, rechunk=True).sort(["symbol", "bar_date"])
    print(f"  Panel shape: {panel.shape}")
    return panel


def compute_adv_dollar(panel: pl.DataFrame, adv_days: int = ADV_DAYS) -> pl.DataFrame:
    """
    Compute median daily dollar volume over the last adv_days trading days for each symbol.
    Returns (symbol, adv_dollar, median_close).
    """
    print(f"Computing ADV_dollar (median {adv_days}d daily dollar vol)...")
    # Use the last adv_days dates globally
    all_dates = sorted(panel["bar_date"].unique().to_list())
    last_dates = set(all_dates[-adv_days:]) if len(all_dates) >= adv_days else set(all_dates)

    adv_panel = panel.filter(pl.col("bar_date").is_in(list(last_dates)))

    adv = (
        adv_panel.group_by("symbol")
        .agg(
            pl.col("dollar_vol").median().alias("adv_dollar"),
            pl.col("close").median().alias("median_close"),
            pl.col("dollar_vol").count().alias("n_days"),
        )
        .filter(pl.col("adv_dollar").is_not_null() & (pl.col("adv_dollar") > 0))
    )
    print(f"  ADV computed for {len(adv)} symbols")
    return adv


def measure_quoted_half_spreads(adv_df: pl.DataFrame) -> pl.DataFrame:
    """
    For symbols with quote data, measure median half-spread in bps during RTH.
    half_spread_bps = 0.5 * (ask - bid) / midpoint * 10000
    Returns (symbol, half_spread_bps_measured, n_quotes, depth_top_dollar).
    """
    print("Measuring half-spreads for quoted names...")
    quote_syms = [d.name.split("=", 1)[1] for d in QUOTES_ROOT.iterdir() if d.is_dir()]
    adv_syms = set(adv_df["symbol"].to_list())
    common_syms = [s for s in quote_syms if s in adv_syms]
    print(f"  {len(quote_syms)} symbols have quotes; {len(common_syms)} also have ADV data")

    results: list[dict[str, Any]] = []
    for sym_idx, symbol in enumerate(common_syms):
        sym_dir = QUOTES_ROOT / f"symbol={symbol}"
        if not sym_dir.is_dir():
            continue

        sym_frames: list[pl.DataFrame] = []
        for date_dir in sym_dir.iterdir():
            if not date_dir.is_dir():
                continue
            parquet_path = date_dir / "data.parquet"
            if not parquet_path.exists():
                continue
            try:
                df = pl.read_parquet(parquet_path, columns=["ts", "bid_price", "ask_price", "bid_size", "ask_size"])
                sym_frames.append(df)
            except Exception as exc:  # noqa: BLE001
                continue

        if not sym_frames:
            continue

        sym_q = pl.concat(sym_frames, rechunk=True)
        if sym_q.is_empty():
            continue

        # Filter to RTH quotes only
        sym_q = sym_q.with_columns(
            pl.col("ts").dt.hour().cast(pl.Int32).alias("utc_hour"),
            pl.col("ts").dt.minute().cast(pl.Int32).alias("utc_minute"),
        ).filter(
            (
                ((pl.col("utc_hour") == 13) & (pl.col("utc_minute") >= 30))
                | (pl.col("utc_hour") > 13)
            )
            & (pl.col("utc_hour") <= RTH_CLOSE_UTC_HOUR)
        )

        if sym_q.is_empty():
            continue

        # Filter valid quotes: bid > 0, ask > bid
        sym_q = sym_q.filter(
            (pl.col("bid_price") > 0)
            & (pl.col("ask_price") > pl.col("bid_price"))
        )

        if len(sym_q) < 10:
            continue

        sym_q = sym_q.with_columns(
            (0.5 * (pl.col("ask_price") - pl.col("bid_price"))
             / ((pl.col("ask_price") + pl.col("bid_price")) / 2.0) * 10000.0).alias("half_spread_bps"),
            ((pl.col("bid_price") * pl.col("bid_size") + pl.col("ask_price") * pl.col("ask_size")) / 2.0).alias("depth_top_dollar"),
        )

        median_hs = float(sym_q["half_spread_bps"].median())  # type: ignore[arg-type]
        n_quotes = len(sym_q)
        median_depth = float(sym_q["depth_top_dollar"].median())  # type: ignore[arg-type]

        if not math.isfinite(median_hs) or median_hs <= 0:
            continue

        results.append({
            "symbol": symbol,
            "half_spread_bps_measured": median_hs,
            "n_quotes": n_quotes,
            "depth_top_dollar": median_depth,
        })

        if (sym_idx + 1) % 200 == 0:
            print(f"  {sym_idx + 1}/{len(common_syms)} quoted symbols processed")

    spread_df = pl.DataFrame(results)
    print(f"  Half-spread measured for {len(spread_df)} symbols")
    if len(spread_df) > 0:
        print(f"  Median half_spread_bps: {spread_df['half_spread_bps_measured'].median():.2f} bps")
        print(f"  p10/p50/p90: {spread_df['half_spread_bps_measured'].quantile(0.1):.2f} / "
              f"{spread_df['half_spread_bps_measured'].quantile(0.5):.2f} / "
              f"{spread_df['half_spread_bps_measured'].quantile(0.9):.2f} bps")
    return spread_df


def fit_spread_model(quoted_spreads: pl.DataFrame, adv_df: pl.DataFrame) -> dict[str, Any]:
    """
    Fit linear model: half_spread_bps ~ a + b*log10(adv_dollar) + c*(1/price)
    on the quoted names (where we have measured spreads).
    Returns model coefficients and fit stats.
    """
    print("Fitting spread model for extrapolation to illiquid names...")

    # Join spread data with ADV
    train = quoted_spreads.join(adv_df, on="symbol", how="inner").filter(
        pl.col("adv_dollar").is_not_null()
        & (pl.col("adv_dollar") > 0)
        & pl.col("median_close").is_not_null()
        & (pl.col("median_close") > 0)
        & pl.col("half_spread_bps_measured").is_not_null()
    )

    if len(train) < 10:
        print("  WARNING: Too few data points for spread model fit")
        return {"error": "too few data points"}

    y = train["half_spread_bps_measured"].to_numpy()
    log_adv = np.log10(train["adv_dollar"].to_numpy())
    inv_price = 1.0 / train["median_close"].to_numpy()

    # OLS: [1, log_adv, inv_price]
    X = np.column_stack([np.ones(len(y)), log_adv, inv_price])
    # Use numpy lstsq
    coeffs, residuals, rank, sv = np.linalg.lstsq(X, y, rcond=None)
    y_pred = X @ coeffs
    ss_res = float(np.sum((y - y_pred) ** 2))
    ss_tot = float(np.sum((y - float(np.mean(y))) ** 2))
    r_sq = 1.0 - ss_res / ss_tot if ss_tot > 0 else float("nan")

    # Cap predictions at >=0
    print(f"  Fit: a={coeffs[0]:.4f}, b={coeffs[1]:.4f} (log_adv), c={coeffs[2]:.2f} (1/price)")
    print(f"  R^2={r_sq:.3f} on {len(train)} quoted symbols")
    print(f"  Prediction range: {max(0, float(np.min(y_pred))):.2f} to {float(np.max(y_pred)):.2f} bps")

    return {
        "a": float(coeffs[0]),
        "b": float(coeffs[1]),
        "c": float(coeffs[2]),
        "r_squared": r_sq,
        "n_fit": len(train),
        "y_mean": float(np.mean(y)),
        "y_std": float(np.std(y)),
    }


def compute_per_name_costs(
    adv_df: pl.DataFrame,
    quoted_spreads: pl.DataFrame,
    spread_model: dict[str, Any],
    order_notionals: list[int],
    impact_k: float = IMPACT_K,
) -> pl.DataFrame:
    """
    For each symbol, compute round_trip_cost_bps at each order size.
    half_spread_bps: measured if in quoted universe, estimated (with flag) otherwise.
    impact_bps: k * sqrt(order_notional / adv_dollar)
    round_trip = 2 * (half_spread + impact)
    """
    print("Computing per-name costs...")

    quoted_syms = set(quoted_spreads["symbol"].to_list())

    # Map measured spreads
    spread_map = dict(zip(
        quoted_spreads["symbol"].to_list(),
        quoted_spreads["half_spread_bps_measured"].to_list(),
    ))

    model_a = spread_model.get("a", 5.0)
    model_b = spread_model.get("b", -0.5)
    model_c = spread_model.get("c", 0.0)

    rows: list[dict[str, Any]] = []
    for row in adv_df.to_dicts():
        symbol = row["symbol"]
        adv_dollar = row["adv_dollar"]
        median_close = row["median_close"]

        if adv_dollar is None or adv_dollar <= 0:
            continue
        if median_close is None or median_close <= 0:
            continue

        # Half spread
        if symbol in quoted_syms and symbol in spread_map:
            half_spread_bps = spread_map[symbol]
            spread_is_estimated = False
        else:
            # Estimate from model
            log_adv = math.log10(adv_dollar) if adv_dollar > 0 else 0.0
            inv_price = 1.0 / median_close
            half_spread_bps = max(0.5, model_a + model_b * log_adv + model_c * inv_price)
            spread_is_estimated = True

        cost_row: dict[str, Any] = {
            "symbol": symbol,
            "adv_dollar": adv_dollar,
            "median_close": median_close,
            "half_spread_bps": half_spread_bps,
            "spread_is_estimated": spread_is_estimated,
        }

        for order_notional in order_notionals:
            participation = order_notional / adv_dollar
            impact_bps = impact_k * math.sqrt(participation) * 100.0  # sqrt in fraction, convert to bps
            rt_cost_bps = 2.0 * (half_spread_bps + impact_bps)
            cost_row[f"impact_bps_{order_notional//1000}k"] = impact_bps
            cost_row[f"rt_cost_bps_{order_notional//1000}k"] = rt_cost_bps

        rows.append(cost_row)

    costs = pl.DataFrame(rows)
    print(f"  Cost model built for {len(costs)} symbols")
    return costs


def assign_liquidity_tertile(costs: pl.DataFrame) -> pl.DataFrame:
    """Assign tertile by ADV_dollar: top=liquid, bottom=illiquid."""
    sorted_costs = costs.sort("adv_dollar", descending=True)
    n = len(sorted_costs)
    t1 = n // 3
    t2 = 2 * n // 3

    tertile_labels = (
        ["liquid"] * t1 +
        ["mid"] * (t2 - t1) +
        ["illiquid"] * (n - t2)
    )
    sorted_costs = sorted_costs.with_columns(
        pl.Series("adv_tertile", tertile_labels)
    )
    return sorted_costs


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
        """
    )
    rows = cur.fetchall()
    conn.close()
    df = pl.DataFrame({
        "symbol": [r[0] for r in rows],
        "form_type": [r[1] for r in rows],
        "available_at_date": [r[2] for r in rows],
        "available_at": [r[3] for r in rows],
        "available_at_source": [r[4] for r in rows],
        "accession_number": [r[5] for r in rows],
        "cik": [r[6] for r in rows],
        "link": [r[7] for r in rows],
    })
    print(f"  Loaded {len(df)} 8-K filings ({df['symbol'].n_unique()} symbols)")
    return df


def assign_entry_dates(filings: pl.DataFrame, trading_dates: list[date]) -> pl.DataFrame:
    """Next trading day after available_at_date."""
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
        entry_dates.append(next_td.get(avail_date))
    result = filings.with_columns(pl.Series("entry_date", entry_dates, dtype=pl.Date))
    result = result.filter(pl.col("entry_date").is_not_null())
    print(f"  Filings with valid entry date: {len(result)}")
    return result


def build_forward_returns(panel: pl.DataFrame, horizons: list[int]) -> pl.DataFrame:
    """Build close-to-close and open-entry forward returns."""
    result = panel.sort(["symbol", "bar_date"])
    for horizon in horizons:
        result = result.with_columns(
            (pl.col("close").shift(-horizon).over("symbol") / pl.col("close") - 1).alias(f"fwd_{horizon}d")
        )
        result = result.with_columns(
            (pl.col("close").shift(-horizon).over("symbol") / pl.col("open_price") - 1).alias(f"open_fwd_{horizon}d")
        )
    return result


def compute_horizon_stats(
    panel: pl.DataFrame,
    fwd_col: str,
    cost_rt_bps: float,
    n_canary_seeds: int = CANARY_SEEDS,
) -> dict[str, Any]:
    """
    Day-clustered alpha + t-stat with per-symbol demean (within this slice).
    cost_rt_bps: round-trip cost in bps, deducted from event returns.
    """
    cost = cost_rt_bps / 10000.0

    # Per-symbol demean within this slice
    sym_means = panel.group_by("symbol").agg(pl.col(fwd_col).mean().alias("sym_mean"))
    panel_dm = panel.join(sym_means, on="symbol", how="left").with_columns(
        (pl.col(fwd_col) - pl.col("sym_mean")).alias(f"{fwd_col}_dm")
    )

    dates_with_events = panel.filter(pl.col("is_event"))["bar_date"].unique().to_list()

    alpha_per_date: list[float] = []
    dm_alpha_per_date: list[float] = []
    event_counts: list[int] = []

    for dt in sorted(dates_with_events):
        day_data = panel.filter(pl.col("bar_date") == dt).filter(pl.col(fwd_col).is_not_null())
        day_data_dm = panel_dm.filter(pl.col("bar_date") == dt).filter(pl.col(fwd_col).is_not_null())

        event_ret = day_data.filter(pl.col("is_event"))[fwd_col].to_list()
        ctrl_ret = day_data.filter(~pl.col("is_event"))[fwd_col].to_list()
        event_ret_dm = day_data_dm.filter(pl.col("is_event"))[f"{fwd_col}_dm"].to_list()
        ctrl_ret_dm = day_data_dm.filter(~pl.col("is_event"))[f"{fwd_col}_dm"].to_list()

        if not event_ret or not ctrl_ret:
            continue

        event_ret_net = [r - cost for r in event_ret]
        alpha_per_date.append(float(np.mean(event_ret_net)) - float(np.mean(ctrl_ret)))
        event_counts.append(len(event_ret))

        if event_ret_dm and ctrl_ret_dm:
            event_dm_net = [r - cost for r in event_ret_dm]
            dm_alpha_per_date.append(float(np.mean(event_dm_net)) - float(np.mean(ctrl_ret_dm)))

    n_dates = len(alpha_per_date)
    nan = float("nan")
    if n_dates < 5:
        return {
            "n_event_obs": sum(event_counts),
            "n_dates": n_dates,
            "alpha_mean_pct": nan, "t_stat": nan,
            "alpha_demean_pct": nan, "t_stat_demean": nan,
            "canary_mean_pct": nan, "canary_p95_pct": nan,
        }

    alpha_arr = np.array(alpha_per_date)
    alpha_mean = float(np.mean(alpha_arr))
    alpha_std = float(np.std(alpha_arr, ddof=1))
    t_stat = alpha_mean / (alpha_std / np.sqrt(n_dates)) if alpha_std > 0 else nan

    dm_arr = np.array(dm_alpha_per_date) if dm_alpha_per_date else np.array([nan])
    alpha_dm = float(np.mean(dm_arr))
    dm_std = float(np.std(dm_arr, ddof=1)) if len(dm_arr) > 1 else nan
    t_dm = (alpha_dm / (dm_std / np.sqrt(len(dm_arr))) if dm_std and dm_std > 0 and len(dm_arr) > 1 else nan)

    # Canary permutation
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

    canary_mean = float(np.mean(canary_alphas)) if canary_alphas else nan
    canary_p95 = float(np.percentile(canary_alphas, 95)) if canary_alphas else nan

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


def rescore_cohort(
    fwd_panel: pl.DataFrame,
    event_df: pl.DataFrame,
    cohort_symbols: set[str],
    per_symbol_costs: dict[str, float],
    label: str,
    oos_dates: set[date],
    train_dates: set[date],
    use_open_entry: bool = True,
) -> dict[str, Any]:
    """
    Re-score a cohort (set of symbols) on the OOS period, using per-symbol cost from the cost model.
    Two passes: one at $5K/name average cost, one at $10K/name average cost.
    """
    print(f"\n--- {label} ---")

    # Filter to cohort symbols and join events
    event_entries = event_df.filter(
        pl.col("symbol").is_in(list(cohort_symbols))
    ).select(["symbol", "entry_date"]).unique(subset=["symbol", "entry_date"])

    fwd_cohort = fwd_panel.filter(pl.col("symbol").is_in(list(cohort_symbols)))

    fwd_with_event = fwd_cohort.join(
        event_entries.rename({"entry_date": "bar_date"}).with_columns(pl.lit(True).alias("is_event")),
        on=["symbol", "bar_date"],
        how="left",
    ).with_columns(pl.col("is_event").fill_null(False))

    oos_panel = fwd_with_event.filter(pl.col("bar_date").is_in(list(oos_dates)))
    train_panel = fwd_with_event.filter(pl.col("bar_date").is_in(list(train_dates)))

    # Compute cohort median cost at $5K and $10K
    costs_5k = [per_symbol_costs.get(s, {}).get("5k", 0.0) for s in cohort_symbols if s in per_symbol_costs]
    costs_10k = [per_symbol_costs.get(s, {}).get("10k", 0.0) for s in cohort_symbols if s in per_symbol_costs]

    median_rt_5k = float(np.median(costs_5k)) if costs_5k else 0.0
    median_rt_10k = float(np.median(costs_10k)) if costs_10k else 0.0
    print(f"  Cohort median round-trip cost: {median_rt_5k:.1f} bps ($5K), {median_rt_10k:.1f} bps ($10K)")

    results_by_horizon: dict[str, Any] = {
        "label": label,
        "n_cohort_symbols": len(cohort_symbols),
        "median_rt_cost_5k_bps": median_rt_5k,
        "median_rt_cost_10k_bps": median_rt_10k,
    }

    for horizon in HORIZONS:
        fwd_col = f"open_fwd_{horizon}d" if use_open_entry else f"fwd_{horizon}d"
        print(f"\n  Horizon {horizon}d ({fwd_col}):")

        # OOS: $5K cost
        oos_h = oos_panel.filter(pl.col(fwd_col).is_not_null())
        if oos_h.is_empty():
            print("  (empty OOS panel)")
            continue

        oos_5k = compute_horizon_stats(oos_h, fwd_col, median_rt_5k)
        oos_10k = compute_horizon_stats(oos_h, fwd_col, median_rt_10k)
        train_h = train_panel.filter(pl.col(fwd_col).is_not_null())
        train_stats = compute_horizon_stats(train_h, fwd_col, median_rt_5k)

        # Also: 2x spread sensitivity (double the half_spread component)
        # 2x spread means rt_cost = 2*(2*hs + impact) instead of 2*(hs+impact)
        # Net effect: extra 2*hs per trade
        extra_2x_spread_5k = float(np.median([per_symbol_costs.get(s, {}).get("half_spread", 0.0) * 2 for s in cohort_symbols if s in per_symbol_costs]))
        oos_2x_stress_5k = compute_horizon_stats(oos_h, fwd_col, median_rt_5k + extra_2x_spread_5k)
        oos_2x_stress_10k = compute_horizon_stats(oos_h, fwd_col, median_rt_10k + extra_2x_spread_5k)

        print(
            f"    TRAIN: n={train_stats['n_event_obs']}, dm%={train_stats['alpha_demean_pct']:+.3f}, t_dm={train_stats['t_stat_demean']:+.2f}"
        )
        print(
            f"    OOS $5K: n={oos_5k['n_event_obs']}, dm%={oos_5k['alpha_demean_pct']:+.3f}%, t_dm={oos_5k['t_stat_demean']:+.2f}"
        )
        print(
            f"    OOS $10K: n={oos_10k['n_event_obs']}, dm%={oos_10k['alpha_demean_pct']:+.3f}%, t_dm={oos_10k['t_stat_demean']:+.2f}"
        )
        print(
            f"    OOS 2x-spread stress $5K: dm%={oos_2x_stress_5k['alpha_demean_pct']:+.3f}%, t_dm={oos_2x_stress_5k['t_stat_demean']:+.2f}"
        )

        results_by_horizon[f"horizon_{horizon}d"] = {
            "train": train_stats,
            "oos_5k": oos_5k,
            "oos_10k": oos_10k,
            "oos_2x_spread_stress_5k": oos_2x_stress_5k,
            "oos_2x_spread_stress_10k": oos_2x_stress_10k,
            "rt_cost_5k_bps": median_rt_5k,
            "rt_cost_10k_bps": median_rt_10k,
            "extra_2x_spread_bps": extra_2x_spread_5k,
        }

    return results_by_horizon


def compute_capacity_ceiling(
    illiquid_costs: pl.DataFrame,
    base_gross_alpha_pct: float,
    adv_participation_cap: float = 0.01,
    k_values: list[float] | None = None,
    order_notionals: list[int] | None = None,
) -> dict[str, Any]:
    """
    Sweep total deployed capital and find where net alpha decays to ~zero.
    Methodology:
    - Max position per name = min($10K, adv_participation_cap * adv_dollar)
    - n_concurrent names * position_size = total_capital
    - impact grows with sqrt(position / adv_dollar)
    - Report: at what total_capital does mean round-trip cost exceed gross_alpha?
    """
    print("\nComputing capacity ceiling...")
    if k_values is None:
        k_values = [5.0, 10.0, 20.0]
    if order_notionals is None:
        order_notionals = [1_000, 2_500, 5_000, 10_000, 25_000, 50_000]

    # For each name: max allowed order size = min(50K, 1% of ADV)
    max_position = (illiquid_costs["adv_dollar"] * adv_participation_cap).clip(upper_bound=50_000.0)
    median_adv = float(illiquid_costs["adv_dollar"].median())
    median_half_spread = float(illiquid_costs["half_spread_bps"].median())
    n_names = len(illiquid_costs)

    print(f"  Illiquid cohort: {n_names} names, median ADV ${median_adv:,.0f}, median hs={median_half_spread:.1f} bps")

    results: dict[str, Any] = {
        "n_illiquid_names": n_names,
        "median_adv_dollar": median_adv,
        "median_half_spread_bps": median_half_spread,
        "gross_alpha_pct": base_gross_alpha_pct,
        "participation_cap": adv_participation_cap,
        "capacity_by_k": {},
    }

    for k_val in k_values:
        sweep_rows = []
        for order_notional in order_notionals:
            # How many names can we enter at this order size (bounded by participation cap per name)?
            valid_names = illiquid_costs.filter(
                illiquid_costs["adv_dollar"] * adv_participation_cap >= order_notional
            )
            n_valid = len(valid_names)
            if n_valid == 0:
                continue

            # Median impact at this order size and k
            median_impact = k_val * math.sqrt(order_notional / float(valid_names["adv_dollar"].median())) * 100.0
            median_rt_cost = 2.0 * (float(valid_names["half_spread_bps"].median()) + median_impact)

            # Total deployed capital
            total_capital = order_notional * n_valid

            # Net alpha (gross alpha in bps - round trip cost)
            gross_alpha_bps = base_gross_alpha_pct * 100.0  # pct to bps
            net_alpha_bps = gross_alpha_bps - median_rt_cost

            sweep_rows.append({
                "order_notional": order_notional,
                "n_valid_names": n_valid,
                "total_capital": total_capital,
                "median_impact_bps": median_impact,
                "median_rt_cost_bps": median_rt_cost,
                "net_alpha_bps": net_alpha_bps,
            })
            print(
                f"  k={k_val} | order=${order_notional:>6,} | n={n_valid:4d} | "
                f"total=${total_capital:>10,} | impact={median_impact:.1f}bps | "
                f"rt={median_rt_cost:.1f}bps | net_alpha={net_alpha_bps:+.1f}bps"
            )

        results["capacity_by_k"][f"k_{k_val}"] = sweep_rows

        # Find breakeven total capital (where net_alpha crosses zero)
        positive = [(r["total_capital"], r["net_alpha_bps"]) for r in sweep_rows if r["net_alpha_bps"] > 0]
        negative = [(r["total_capital"], r["net_alpha_bps"]) for r in sweep_rows if r["net_alpha_bps"] <= 0]
        if positive:
            max_pos_cap = max(cap for cap, _ in positive)
            print(f"  k={k_val}: max_positive_capital=${max_pos_cap:,}")
            results["capacity_by_k"][f"k_{k_val}_max_positive_capital"] = max_pos_cap
        else:
            print(f"  k={k_val}: NO positive net alpha at any tested order size")
            results["capacity_by_k"][f"k_{k_val}_max_positive_capital"] = 0

    return results


def main() -> None:
    print("=== H13: Small-capital re-cost of illiquid signals ===\n")

    # 1. Load daily panel
    panel = load_daily_panel()
    trading_dates_all: list[date] = sorted(panel["bar_date"].unique().to_list())
    n_dates = len(trading_dates_all)
    print(f"Panel: {panel['symbol'].n_unique()} symbols, {n_dates} trading dates "
          f"({trading_dates_all[0]} to {trading_dates_all[-1]})")

    # 2. Compute ADV
    adv_df = compute_adv_dollar(panel, ADV_DAYS)

    # 3. Measure half-spreads for quoted names
    quoted_spreads = measure_quoted_half_spreads(adv_df)

    # 4. Fit spread model
    if len(quoted_spreads) > 0:
        spread_model = fit_spread_model(quoted_spreads, adv_df)
    else:
        spread_model = {"error": "no quoted spreads available", "a": 10.0, "b": -0.5, "c": 0.0}

    # 5. Compute per-name costs at $5K and $10K
    costs_df = compute_per_name_costs(adv_df, quoted_spreads, spread_model, ORDER_SIZES)
    costs_df = assign_liquidity_tertile(costs_df)

    # Build per-symbol cost dict
    per_symbol_costs: dict[str, dict[str, float]] = {}
    for row in costs_df.to_dicts():
        per_symbol_costs[row["symbol"]] = {
            "5k": row.get("rt_cost_bps_5k", 0.0),
            "10k": row.get("rt_cost_bps_10k", 0.0),
            "half_spread": row.get("half_spread_bps", 0.0),
            "adv_dollar": row.get("adv_dollar", 0.0),
            "spread_is_estimated": row.get("spread_is_estimated", True),
        }

    # Print cost summary by tertile
    print("\n--- Cost summary by liquidity tertile ---")
    for tertile in ["liquid", "mid", "illiquid"]:
        t_costs = costs_df.filter(pl.col("adv_tertile") == tertile)
        n_sym = len(t_costs)
        med_adv = float(t_costs["adv_dollar"].median())
        med_hs = float(t_costs["half_spread_bps"].median())
        est_frac = float(t_costs["spread_is_estimated"].mean())
        med_rt_5k = float(t_costs["rt_cost_bps_5k"].median())
        med_rt_10k = float(t_costs["rt_cost_bps_10k"].median())
        print(f"  {tertile:9s}: n={n_sym:4d} | ADV ${med_adv:>10,.0f} | hs={med_hs:.1f}bps (est:{est_frac:.0%}) | "
              f"rt_5k={med_rt_5k:.1f}bps | rt_10k={med_rt_10k:.1f}bps")

    # 6. Load 8-K filings + build forward returns
    filings_8k = load_filings_8k()
    filings_8k = assign_entry_dates(filings_8k, trading_dates_all)
    bar_symbols = set(panel["symbol"].unique().to_list())
    filings_8k = filings_8k.filter(pl.col("symbol").is_in(list(bar_symbols)))
    print(f"8-K filings with matching bars: {len(filings_8k)} ({filings_8k['symbol'].n_unique()} symbols)")

    fwd_panel = build_forward_returns(panel, HORIZONS)

    # 7. Train/OOS split (same as H10b: first 63 / last 63 trading days)
    split_idx = n_dates // 2
    train_dates_list = trading_dates_all[:split_idx]
    oos_dates_list = trading_dates_all[split_idx:]
    train_dates_set = set(train_dates_list)
    oos_dates_set = set(oos_dates_list)
    print(f"\nSplit: TRAIN={train_dates_list[0]} to {train_dates_list[-1]} ({len(train_dates_list)} days)")
    print(f"       OOS  ={oos_dates_list[0]} to {oos_dates_list[-1]} ({len(oos_dates_list)} days)")

    # 8. Define illiquid and mid tertile symbol sets
    illiquid_symbols = set(costs_df.filter(pl.col("adv_tertile") == "illiquid")["symbol"].to_list())
    mid_symbols = set(costs_df.filter(pl.col("adv_tertile") == "mid")["symbol"].to_list())
    liquid_symbols = set(costs_df.filter(pl.col("adv_tertile") == "liquid")["symbol"].to_list())

    print(f"\nTertile breakdown: liquid={len(liquid_symbols)}, mid={len(mid_symbols)}, illiquid={len(illiquid_symbols)}")

    # 9. Re-score illiquid tertile
    illiquid_result = rescore_cohort(
        fwd_panel=fwd_panel,
        event_df=filings_8k,
        cohort_symbols=illiquid_symbols,
        per_symbol_costs=per_symbol_costs,
        label="H10 ILLIQUID tertile — small-size re-cost (open entry, D+1)",
        oos_dates=oos_dates_set,
        train_dates=train_dates_set,
        use_open_entry=True,
    )

    # 10. Also rescore with k=5 and k=20 for sensitivity
    # Compute costs at k=5 and k=20
    costs_df_k5 = compute_per_name_costs(adv_df, quoted_spreads, spread_model, ORDER_SIZES, impact_k=5.0)
    costs_df_k5 = assign_liquidity_tertile(costs_df_k5)
    per_sym_k5: dict[str, dict[str, float]] = {}
    for row in costs_df_k5.to_dicts():
        per_sym_k5[row["symbol"]] = {
            "5k": row.get("rt_cost_bps_5k", 0.0),
            "10k": row.get("rt_cost_bps_10k", 0.0),
            "half_spread": row.get("half_spread_bps", 0.0),
        }

    costs_df_k20 = compute_per_name_costs(adv_df, quoted_spreads, spread_model, ORDER_SIZES, impact_k=20.0)
    costs_df_k20 = assign_liquidity_tertile(costs_df_k20)
    per_sym_k20: dict[str, dict[str, float]] = {}
    for row in costs_df_k20.to_dicts():
        per_sym_k20[row["symbol"]] = {
            "5k": row.get("rt_cost_bps_5k", 0.0),
            "10k": row.get("rt_cost_bps_10k", 0.0),
            "half_spread": row.get("half_spread_bps", 0.0),
        }

    illiquid_result_k5 = rescore_cohort(
        fwd_panel=fwd_panel,
        event_df=filings_8k,
        cohort_symbols=illiquid_symbols,
        per_symbol_costs=per_sym_k5,
        label="H10 ILLIQUID tertile — k=5 sensitivity",
        oos_dates=oos_dates_set,
        train_dates=train_dates_set,
        use_open_entry=True,
    )
    illiquid_result_k20 = rescore_cohort(
        fwd_panel=fwd_panel,
        event_df=filings_8k,
        cohort_symbols=illiquid_symbols,
        per_symbol_costs=per_sym_k20,
        label="H10 ILLIQUID tertile — k=20 sensitivity",
        oos_dates=oos_dates_set,
        train_dates=train_dates_set,
        use_open_entry=True,
    )

    # 11. Capacity ceiling
    # Use OOS 1d demean alpha as gross_alpha for capacity calc
    h1_oos_dm_5k = illiquid_result.get("horizon_1d", {}).get("oos_5k", {}).get("alpha_demean_pct", 0.0)
    gross_alpha_for_cap = h1_oos_dm_5k if h1_oos_dm_5k and math.isfinite(h1_oos_dm_5k) else 1.5

    illiquid_costs_df = costs_df.filter(pl.col("adv_tertile") == "illiquid")
    capacity_result = compute_capacity_ceiling(
        illiquid_costs=illiquid_costs_df,
        base_gross_alpha_pct=gross_alpha_for_cap / 100.0,  # convert pct -> decimal
        adv_participation_cap=0.01,
    )

    # 12. Cost stats by tertile for method.md
    cost_summary: dict[str, Any] = {}
    for tertile in ["liquid", "mid", "illiquid"]:
        t_costs = costs_df.filter(pl.col("adv_tertile") == tertile)
        n_est = int(t_costs["spread_is_estimated"].sum())
        n_meas = len(t_costs) - n_est
        cost_summary[tertile] = {
            "n": len(t_costs),
            "n_measured_spread": n_meas,
            "n_estimated_spread": n_est,
            "median_adv_dollar": float(t_costs["adv_dollar"].median()),
            "median_close": float(t_costs["median_close"].median()),
            "median_half_spread_bps": float(t_costs["half_spread_bps"].median()),
            "p10_half_spread_bps": float(t_costs["half_spread_bps"].quantile(0.1)),
            "p90_half_spread_bps": float(t_costs["half_spread_bps"].quantile(0.9)),
            "median_rt_cost_5k_bps": float(t_costs["rt_cost_bps_5k"].median()),
            "median_rt_cost_10k_bps": float(t_costs["rt_cost_bps_10k"].median()),
            "p90_rt_cost_5k_bps": float(t_costs["rt_cost_bps_5k"].quantile(0.9)),
        }

    # 13. Save all results
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    all_output = {
        "spread_model": spread_model,
        "cost_summary_by_tertile": cost_summary,
        "illiquid_rescore_k10": illiquid_result,
        "illiquid_rescore_k5": illiquid_result_k5,
        "illiquid_rescore_k20": illiquid_result_k20,
        "capacity_ceiling": capacity_result,
        "n_quoted_symbols": len(quoted_spreads),
        "n_total_symbols": len(adv_df),
    }

    results_path = OUTPUT_DIR / "raw_results_h13.json"
    with open(results_path, "w") as json_file:
        json.dump(all_output, json_file, indent=2, default=str)
    print(f"\nRaw results saved to {results_path}")

    # Summary print
    print("\n" + "=" * 70)
    print("H13 SUMMARY")
    print("=" * 70)

    for tertile in ["liquid", "mid", "illiquid"]:
        cs = cost_summary[tertile]
        print(f"  {tertile:9s}: n={cs['n']:4d} | ADV=${cs['median_adv_dollar']:>10,.0f} | "
              f"hs={cs['median_half_spread_bps']:.1f}bps | "
              f"rt_5k={cs['median_rt_cost_5k_bps']:.1f}bps | rt_10k={cs['median_rt_cost_10k_bps']:.1f}bps")

    print("\n  ILLIQUID tertile re-score (k=10, open entry, D+1):")
    for horizon in HORIZONS:
        hkey = f"horizon_{horizon}d"
        if hkey not in illiquid_result:
            continue
        h = illiquid_result[hkey]
        oos5 = h.get("oos_5k", {})
        oos10 = h.get("oos_10k", {})
        stress5 = h.get("oos_2x_spread_stress_5k", {})
        print(f"    {horizon}d | OOS $5K: dm%={oos5.get('alpha_demean_pct', float('nan')):+.3f}%, "
              f"t={oos5.get('t_stat_demean', float('nan')):+.2f} | "
              f"OOS $10K: dm%={oos10.get('alpha_demean_pct', float('nan')):+.3f}%, "
              f"t={oos10.get('t_stat_demean', float('nan')):+.2f} | "
              f"2x-stress: dm%={stress5.get('alpha_demean_pct', float('nan')):+.3f}%")

    print("\n  Capacity ceiling (k=10, 1% ADV participation cap):")
    cap_k10 = capacity_result.get("capacity_by_k", {}).get("k_10.0", [])
    for row in cap_k10:
        net = row.get("net_alpha_bps", float("nan"))
        print(f"    order=${row['order_notional']:>6,} | n={row['n_valid_names']:4d} | "
              f"total=${row['total_capital']:>10,} | rt={row['median_rt_cost_bps']:.1f}bps | "
              f"net={net:+.1f}bps")

    max_cap_k10 = capacity_result.get("capacity_by_k", {}).get("k_10.0_max_positive_capital", 0)
    print(f"  Max capital with positive net alpha (k=10): ${max_cap_k10:,}")

    print("\nDone.")


if __name__ == "__main__":
    main()
