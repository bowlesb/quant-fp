"""W4 — Overnight vs intraday return decomposition, LIQUID portfolio.

Tests (pre-registered, hypothesis.md):
  1. DESCRIPTIVE   : mean overnight vs intraday per name, pooled + cross-sectional.
  2. DEMEAN (load-bearing): per-symbol demean each component; does any structure survive removing the level?
  3. CROSS-SECTIONAL L/S: does a name's recent overnight (intraday) return predict its NEXT overnight
     (intraday) return? Decile L/S momentum AND reversal, equal-weight, rebalanced on the component.
  4. TRADEABLE ENTRY: overnight bet = buy@today_close -> sell@tomorrow_open; intraday = buy@open->sell@close.
     Charge measured spread per leg + 2x stress. MOC/MOO auction-fill caveat noted in method.md.
  5. GATES: shuffle-canary, per-symbol demean (PRIMARY), walk-forward OOS, per-trade bootstrap (10k, 95% CI),
     cost gate measured + 2x. DECISIVE = demean-surviving, OOS, net-of-cost, bootstrap CI>0.

All metrics hand-rolled / from hf_metrics_fixed. polars+numpy only. VECTORIZED.
"""

from __future__ import annotations

import os
import sys

import numpy as np
import polars as pl

sys.path.insert(0, "/app/experiments/2026-06-16-hf01-quote-imbalance")
from hf_metrics_fixed import day_clustered_tstat, spearman_ic  # noqa: E402

PANEL = "/app/experiments/2026-06-16-w4-overnight-intraday/panel.parquet"
OUTDIR = "/app/experiments/2026-06-16-w4-overnight-intraday"

TOP_N = 500
MEGACAP_N = 100
MIN_DAYS = 100
N_DECILES = 10
RNG = np.random.default_rng(42)
N_BOOT = 10_000


def load_liquid(panel: pl.DataFrame, top_n: int) -> pl.DataFrame:
    liq = (
        panel.group_by("symbol")
        .agg(pl.col("dollar_vol").median().alias("mdv"), pl.len().alias("ndays"))
        .filter(pl.col("ndays") >= MIN_DAYS)
        .sort("mdv", descending=True)
        .head(top_n)
    )
    return panel.filter(pl.col("symbol").is_in(liq["symbol"].to_list()))


def add_returns(df: pl.DataFrame) -> pl.DataFrame:
    """Per symbol, sorted by date: overnight = open/prev_close-1; intraday = close/open-1."""
    df = df.sort(["symbol", "date"])
    df = df.with_columns(pl.col("rth_close").shift(1).over("symbol").alias("prev_close"))
    df = df.with_columns(
        (pl.col("rth_open") / pl.col("prev_close") - 1.0).alias("overnight"),
        (pl.col("rth_close") / pl.col("rth_open") - 1.0).alias("intraday"),
    )
    # sane bounds: drop absurd values (splits/data errors) -> |ret|>50% overnight is almost surely a split
    df = df.filter(
        pl.col("overnight").is_finite()
        & pl.col("intraday").is_finite()
        & (pl.col("overnight").abs() < 0.5)
        & (pl.col("intraday").abs() < 0.5)
    )
    return df


# ---------------- Test 1: descriptive ----------------
def descriptive(df: pl.DataFrame) -> dict:
    pooled_on = float(df["overnight"].mean())
    pooled_id = float(df["intraday"].mean())
    per_sym = df.group_by("symbol").agg(
        pl.col("overnight").mean().alias("on"), pl.col("intraday").mean().alias("id")
    )
    return {
        "pooled_overnight_mean_bps": pooled_on * 1e4,
        "pooled_intraday_mean_bps": pooled_id * 1e4,
        "xsec_overnight_mean_bps": float(per_sym["on"].mean()) * 1e4,
        "xsec_overnight_median_bps": float(per_sym["on"].median()) * 1e4,
        "xsec_overnight_pct_positive": float((per_sym["on"] > 0).mean()),
        "xsec_intraday_mean_bps": float(per_sym["id"].mean()) * 1e4,
        "xsec_intraday_median_bps": float(per_sym["id"].median()) * 1e4,
        "xsec_intraday_pct_positive": float((per_sym["id"] > 0).mean()),
        "n_symbols": per_sym.height,
        "n_obs": df.height,
    }


# ---------------- Test 2: per-symbol demean (LOAD-BEARING) ----------------
def demean_level_test(df: pl.DataFrame) -> dict:
    """After removing each name's OWN mean, is the residual still signed? (it must be ~0 by construction
    for the LEVEL; the real question is whether the level itself is significant across names = a
    day-clustered t-stat on the per-day cross-sectional mean). We test the RAW level significance with a
    day-clustered t-test (each day's equal-weight cross-sectional mean overnight/intraday return is one
    obs), then confirm the demeaned series has ~zero mean (level removed)."""
    out = {}
    for comp in ("overnight", "intraday"):
        # day-clustered: equal-weight cross-sectional mean per day -> series over days
        daily = df.group_by("date").agg(pl.col(comp).mean().alias("m")).sort("date")
        series = daily["m"].to_numpy()
        mean_bps = float(series.mean()) * 1e4
        sd = float(series.std(ddof=1))
        t = float(series.mean() / (sd / np.sqrt(len(series)))) if sd > 0 else np.nan
        # per-symbol demean -> residual mean must be ~0 (proves the signal WAS just the level)
        dm = df.with_columns((pl.col(comp) - pl.col(comp).mean().over("symbol")).alias("dm"))
        dm_daily = dm.group_by("date").agg(pl.col("dm").mean().alias("m")).sort("date")
        dm_series = dm_daily["m"].to_numpy()
        dm_mean_bps = float(dm_series.mean()) * 1e4
        dm_sd = float(dm_series.std(ddof=1))
        dm_t = float(dm_series.mean() / (dm_sd / np.sqrt(len(dm_series)))) if dm_sd > 0 else np.nan
        out[comp] = {
            "raw_daily_mean_bps": mean_bps,
            "raw_day_clustered_t": t,
            "demeaned_daily_mean_bps": dm_mean_bps,
            "demeaned_day_clustered_t": dm_t,
            "n_days": len(series),
        }
    return out


# ---------------- Test 3: cross-sectional L/S (momentum & reversal) ----------------
def xsec_ls(df: pl.DataFrame, comp: str) -> tuple[pl.DataFrame, list[float]]:
    """For component comp, on each rebalance date d, rank names by their comp return on the PRIOR
    realization (signal = comp[d-1]); form decile L/S on the NEXT realization (target = comp[d]).
    MOMENTUM L/S = long top decile (high prior), short bottom decile. REVERSAL = opposite sign.
    Returns a per-date series of the LONG-minus-SHORT spread on comp[d] (equal-weight).

    The signal at date d is the name's comp realized at d-1 (information available before d's bet:
    for overnight, comp[d-1] = yesterday's open/prev_close, known at yesterday's open, well before we
    place tonight's close->open bet; for intraday, comp[d-1] = yesterday close/open, known at
    yesterday's close, before today's open->close bet). No look-ahead.
    """
    work = df.select(["symbol", "date", comp]).sort(["symbol", "date"])
    work = work.with_columns(pl.col(comp).shift(1).over("symbol").alias("sig")).drop_nulls(["sig", comp])
    spreads: list[dict] = []
    for date, cell in work.group_by("date"):
        date_val = date[0] if isinstance(date, tuple) else date
        if cell.height < 2 * N_DECILES:
            continue
        ranks = cell["sig"].rank(method="ordinal")
        n = cell.height
        dec = ((ranks - 1) * N_DECILES // n).cast(pl.Int32)
        cell = cell.with_columns(dec.alias("decile"))
        top = cell.filter(pl.col("decile") == N_DECILES - 1)[comp].mean()
        bot = cell.filter(pl.col("decile") == 0)[comp].mean()
        if top is None or bot is None:
            continue
        spreads.append({"date": date_val, "mom_spread": float(top) - float(bot)})
    sp = pl.DataFrame(spreads).sort("date") if spreads else pl.DataFrame({"date": [], "mom_spread": []})
    return sp, sp["mom_spread"].to_list() if sp.height else []


# ---------------- Test 5 helpers: cost, bootstrap, canary, OOS ----------------
def per_name_halfspread_bps(df: pl.DataFrame) -> dict[str, float]:
    """Per-name median range-based round-trip-cost proxy (bps). spread_bps is (high-low)/close*1e4 per
    bar; its per-name median is a generous full round-trip cost. Half on each leg."""
    sp = df.group_by("symbol").agg(pl.col("spread_bps").median().alias("s"))
    return {r["symbol"]: float(r["s"]) for r in sp.iter_rows(named=True)}


def bootstrap_ci(returns: np.ndarray, n_boot: int = N_BOOT) -> tuple[float, float, float]:
    """Per-observation bootstrap of the mean. Returns (mean, lo95, hi95)."""
    returns = returns[np.isfinite(returns)]
    n = len(returns)
    if n < 5:
        return np.nan, np.nan, np.nan
    idx = RNG.integers(0, n, size=(n_boot, n))
    boot_means = returns[idx].mean(axis=1)
    return float(returns.mean()), float(np.percentile(boot_means, 2.5)), float(np.percentile(boot_means, 97.5))


def ls_portfolio_with_cost(
    df: pl.DataFrame, comp: str, direction: int, cost_mult: float
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Build the per-rebalance NET L/S portfolio return series for component comp.

    direction = +1 momentum (long high-prior), -1 reversal (long low-prior).
    Cost: each name in the L/S pays a round-trip = its per-name spread proxy (* cost_mult). The L/S
    portfolio is long a decile and short a decile -> charge the avg name's full round-trip on EACH side.
    Returns (gross_series, net_series, dates_idx)."""
    halfspread = per_name_halfspread_bps(df)
    work = df.select(["symbol", "date", comp]).sort(["symbol", "date"])
    work = work.with_columns(pl.col(comp).shift(1).over("symbol").alias("sig")).drop_nulls(["sig", comp])
    gross, net, dates = [], [], []
    for date, cell in work.group_by("date"):
        if cell.height < 2 * N_DECILES:
            continue
        ranks = cell["sig"].rank(method="ordinal")
        n = cell.height
        dec = ((ranks - 1) * N_DECILES // n).cast(pl.Int32)
        cell = cell.with_columns(dec.alias("decile"))
        long_dec = N_DECILES - 1 if direction == 1 else 0
        short_dec = 0 if direction == 1 else N_DECILES - 1
        longs = cell.filter(pl.col("decile") == long_dec)
        shorts = cell.filter(pl.col("decile") == short_dec)
        if longs.height == 0 or shorts.height == 0:
            continue
        g = float(longs[comp].mean()) - float(shorts[comp].mean())
        # cost: each leg pays round-trip spread (in return units). Avg per-name spread over both legs.
        leg_syms = longs["symbol"].to_list() + shorts["symbol"].to_list()
        avg_rt_bps = float(np.mean([halfspread[s] for s in leg_syms]))
        cost = (avg_rt_bps * 1e-4) * cost_mult  # round-trip already; apply to the L/S notional once per side
        net.append(g - cost)
        gross.append(g)
        dates.append(date[0] if isinstance(date, tuple) else date)
    return np.array(gross), np.array(net), np.array(dates, dtype=object)


def shuffle_canary(df: pl.DataFrame, comp: str, direction: int) -> float:
    """Shuffle the signal WITHIN each date (break the signal->target link) -> the L/S edge should vanish.
    Returns the mean gross L/S spread under shuffle (should be ~0)."""
    work = df.select(["symbol", "date", comp]).sort(["symbol", "date"])
    work = work.with_columns(pl.col(comp).shift(1).over("symbol").alias("sig")).drop_nulls(["sig", comp])
    spreads = []
    for date, cell in work.group_by("date"):
        if cell.height < 2 * N_DECILES:
            continue
        shuffled = cell["sig"].to_numpy().copy()
        RNG.shuffle(shuffled)
        cell = cell.with_columns(pl.Series("sig_sh", shuffled))
        ranks = cell["sig_sh"].rank(method="ordinal")
        n = cell.height
        dec = ((ranks - 1) * N_DECILES // n).cast(pl.Int32)
        cell = cell.with_columns(dec.alias("decile"))
        long_dec = N_DECILES - 1 if direction == 1 else 0
        short_dec = 0 if direction == 1 else N_DECILES - 1
        longs = cell.filter(pl.col("decile") == long_dec)[comp].mean()
        shorts = cell.filter(pl.col("decile") == short_dec)[comp].mean()
        if longs is None or shorts is None:
            continue
        spreads.append(float(longs) - float(shorts))
    return float(np.mean(spreads)) if spreads else np.nan


def main() -> None:
    panel = pl.read_parquet(PANEL)
    results: dict = {}

    for label, top_n in (("liquid500", TOP_N), ("megacap100", MEGACAP_N)):
        sub = load_liquid(panel, top_n)
        sub = add_returns(sub)
        block: dict = {"universe_n_symbols": sub["symbol"].n_unique(), "n_obs": sub.height}

        block["descriptive"] = descriptive(sub)
        block["demean"] = demean_level_test(sub)

        # cross-sectional L/S, both components, momentum (+1) and reversal (-1)
        dates_sorted = sorted(sub["date"].unique().to_list())
        split = dates_sorted[len(dates_sorted) // 2]  # walk-forward: 2nd half = OOS
        oos_set = set(d for d in dates_sorted if d > split)

        ls_block: dict = {}
        for comp in ("overnight", "intraday"):
            for direction, dname in ((1, "momentum"), (-1, "reversal")):
                gross, net, dts = ls_portfolio_with_cost(sub, comp, direction, cost_mult=1.0)
                _, net2x, _ = ls_portfolio_with_cost(sub, comp, direction, cost_mult=2.0)
                # canary
                canary = shuffle_canary(sub, comp, direction)
                # OOS mask
                oos_mask = np.array([d in oos_set for d in dts])
                is_mask = ~oos_mask
                # bootstrap on per-rebalance NET (measured cost) full + OOS
                mean_net, lo, hi = bootstrap_ci(net)
                mean_net_oos, lo_oos, hi_oos = bootstrap_ci(net[oos_mask]) if oos_mask.any() else (np.nan, np.nan, np.nan)
                key = f"{comp}_{dname}"
                ls_block[key] = {
                    "n_rebal": int(len(net)),
                    "gross_mean_bps": float(np.mean(gross)) * 1e4 if len(gross) else np.nan,
                    "net_meas_mean_bps": mean_net * 1e4,
                    "net_meas_boot_lo_bps": lo * 1e4,
                    "net_meas_boot_hi_bps": hi * 1e4,
                    "net_meas_ci_excl_zero": bool(np.isfinite(lo) and np.isfinite(hi) and (lo > 0 or hi < 0)),
                    "net_2x_mean_bps": float(np.mean(net2x)) * 1e4 if len(net2x) else np.nan,
                    "is_gross_mean_bps": float(np.mean(gross[is_mask])) * 1e4 if is_mask.any() else np.nan,
                    "oos_net_meas_mean_bps": mean_net_oos * 1e4 if np.isfinite(mean_net_oos) else np.nan,
                    "oos_net_boot_lo_bps": lo_oos * 1e4 if np.isfinite(lo_oos) else np.nan,
                    "oos_net_boot_hi_bps": hi_oos * 1e4 if np.isfinite(hi_oos) else np.nan,
                    "oos_ci_excl_zero": bool(np.isfinite(lo_oos) and np.isfinite(hi_oos) and (lo_oos > 0 or hi_oos < 0)),
                    "canary_gross_bps": canary * 1e4 if np.isfinite(canary) else np.nan,
                    "avg_rt_cost_bps": float((np.mean(gross) - mean_net)) * 1e4,
                }
        block["xsec_ls"] = ls_block
        results[label] = block

    import json

    with open(f"{OUTDIR}/results.json", "w") as fh:
        json.dump(results, fh, indent=2, default=str)
    print(json.dumps(results, indent=2, default=str))


if __name__ == "__main__":
    main()
