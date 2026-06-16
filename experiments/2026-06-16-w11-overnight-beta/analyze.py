"""W11 — Overnight-BETA premium test.

Reads daily.parquet (per-symbol,date RTH open/close/dollar_vol). Builds:
  - overnight return  = rth_open_t / rth_close_{t-1} - 1
  - intraday  return  = rth_close_t / rth_open_t   - 1
  - 24h       return  = rth_close_t / rth_close_{t-1} - 1  (== compounding of overnight+intraday)
  - market    return  = equal-weight liquid-universe 24h return (SPY too gappy here; stated)
  - BETA per name      = trailing 60-day OLS of name 24h return on market 24h return, re-est every 21d.
At each rebalance: beta QUINTILES over liquid names; high-minus-low L/S realized 3 ways (overnight,
intraday, 24h) over the next 21-day holding period (the period BEFORE the next rebalance => OOS-by-construction
for the held returns relative to the beta estimation window). Per-rebalance L/S return = mean daily L/S over
the hold (compounded? -> we report mean daily L/S * is simplest; we use the AVERAGE daily L/S return over the
hold as the per-rebalance observation, which is the non-overlapping unit for the bootstrap).

Cost: turnover-aware. On each rebalance, charge the measured spread (proxy) on the fraction of legs that
changed, one round trip, +2x stress. We approximate the spread from the liquid universe (top names ~1-3bps);
we use a conservative flat per-side spread and report sensitivity.

Gates: shuffle-canary (permute beta->name mapping), walk-forward OOS (held returns are already forward of the
beta window; we additionally split rebalances into first-half IS / second-half OOS), per-rebalance bootstrap
(10k) on the non-overlapping per-rebalance overnight L/S returns.
"""
from __future__ import annotations

import json

import numpy as np
import polars as pl

DAILY = "/app/experiments/2026-06-16-w11-overnight-beta/daily.parquet"
OUT_JSON = "/app/experiments/2026-06-16-w11-overnight-beta/results.json"

N_LIQUID = 500
MIN_DAYS = 100        # symbol must have >=100 days to be eligible (beta needs history)
BETA_WINDOW = 60      # trailing trading days for beta
REBAL_EVERY = 21      # re-estimate beta / rebalance monthly
N_QUINTILES = 5
SPREAD_BPS = 3.0      # per-side spread proxy (bps) for a liquid name; stress = 2x
RNG_SEED = 11


def load_returns() -> tuple[pl.DataFrame, list[str], list]:
    daily = pl.read_parquet(DAILY)
    # liquidity: median daily dollar volume, require >= MIN_DAYS observations
    liq = (
        daily.group_by("symbol")
        .agg(pl.col("dollar_vol").median().alias("mdv"), pl.len().alias("n"))
        .filter(pl.col("n") >= MIN_DAYS)
        .sort("mdv", descending=True)
    )
    liquid_syms = liq.head(N_LIQUID)["symbol"].to_list()
    daily = daily.filter(pl.col("symbol").is_in(liquid_syms)).sort("symbol", "date")
    # per-symbol prev close
    daily = daily.with_columns(
        pl.col("rth_close").shift(1).over("symbol").alias("prev_close"),
        pl.col("date").shift(1).over("symbol").alias("prev_date"),
    )
    daily = daily.with_columns(
        (pl.col("rth_open") / pl.col("prev_close") - 1.0).alias("overnight"),
        (pl.col("rth_close") / pl.col("rth_open") - 1.0).alias("intraday"),
        (pl.col("rth_close") / pl.col("prev_close") - 1.0).alias("ret24"),
    )
    # drop the first day per symbol (no prev_close) and any non-finite
    daily = daily.filter(
        pl.col("overnight").is_finite() & pl.col("intraday").is_finite() & pl.col("ret24").is_finite()
    )
    dates = sorted(daily["date"].unique().to_list())
    return daily, liquid_syms, dates


def build_market(daily: pl.DataFrame) -> pl.DataFrame:
    """Equal-weight liquid-universe 24h return per date = market proxy."""
    mkt = daily.group_by("date").agg(pl.col("ret24").mean().alias("mkt_ret24")).sort("date")
    return mkt


def estimate_betas(daily: pl.DataFrame, mkt: pl.DataFrame, est_date_idx: int, dates: list) -> dict[str, float]:
    """OLS beta of each symbol's ret24 on mkt_ret24 over the trailing BETA_WINDOW days ending at est_date."""
    lo_idx = est_date_idx - BETA_WINDOW
    if lo_idx < 0:
        lo_idx = 0
    window_dates = dates[lo_idx:est_date_idx]  # trailing window, EXCLUDES est_date (point-in-time)
    if len(window_dates) < BETA_WINDOW // 2:
        return {}
    sub = daily.filter(pl.col("date").is_in(window_dates)).join(mkt, on="date", how="left")
    betas: dict[str, float] = {}
    mkt_by_sym = sub.select(["symbol", "ret24", "mkt_ret24"]).drop_nulls()
    for sym, cell in mkt_by_sym.group_by("symbol"):
        symbol = sym[0]
        x = cell["mkt_ret24"].to_numpy()
        y = cell["ret24"].to_numpy()
        if len(x) < 20:
            continue
        var_x = float(np.var(x))
        if var_x == 0.0:
            continue
        beta = float(np.cov(x, y, ddof=0)[0, 1] / var_x)
        betas[symbol] = beta
    return betas


def ls_returns_over_hold(daily: pl.DataFrame, hold_dates: list, high_syms: set, low_syms: set) -> dict[str, float]:
    """Average daily L/S return (high - low, equal weight within leg) over the holding period, 3 ways."""
    hold = daily.filter(pl.col("date").is_in(hold_dates))
    out: dict[str, float] = {}
    for col in ("overnight", "intraday", "ret24"):
        high = hold.filter(pl.col("symbol").is_in(list(high_syms))).group_by("date").agg(pl.col(col).mean().alias("h"))
        low = hold.filter(pl.col("symbol").is_in(list(low_syms))).group_by("date").agg(pl.col(col).mean().alias("l"))
        merged = high.join(low, on="date", how="inner")
        ls = (merged["h"] - merged["l"]).to_numpy()
        out[col] = float(np.mean(ls)) if len(ls) else np.nan
        out[col + "_n"] = len(ls)
    return out


def run(permute_beta: bool = False) -> list[dict]:
    daily, liquid_syms, dates = load_returns()
    mkt = build_market(daily)
    rng = np.random.default_rng(RNG_SEED)
    results: list[dict] = []
    # first rebalance once we have BETA_WINDOW days of history
    start_idx = BETA_WINDOW
    rebal_idxs = list(range(start_idx, len(dates) - 1, REBAL_EVERY))
    for r, est_idx in enumerate(rebal_idxs):
        betas = estimate_betas(daily, mkt, est_idx, dates)
        if len(betas) < N_QUINTILES * 5:
            continue
        syms = list(betas.keys())
        beta_vals = np.array([betas[symbol] for symbol in syms])
        if permute_beta:
            beta_vals = rng.permutation(beta_vals)
        # quintiles
        order = np.argsort(beta_vals)
        q_size = len(syms) // N_QUINTILES
        low_syms = {syms[i] for i in order[:q_size]}              # lowest beta
        high_syms = {syms[i] for i in order[-q_size:]}            # highest beta
        # holding period = the REBAL_EVERY days AFTER est_idx (strictly forward of the beta window)
        hold_dates = dates[est_idx : est_idx + REBAL_EVERY]
        if len(hold_dates) < 5:
            continue
        ls = ls_returns_over_hold(daily, hold_dates, high_syms, low_syms)
        ls.update({
            "rebal": r,
            "est_date": str(dates[est_idx]),
            "n_syms": len(syms),
            "q_size": q_size,
            "high_syms": sorted(high_syms),
            "low_syms": sorted(low_syms),
            "mean_high_beta": float(np.mean([betas[s] for s in high_syms])) if not permute_beta else None,
            "mean_low_beta": float(np.mean([betas[s] for s in low_syms])) if not permute_beta else None,
        })
        results.append(ls)
    return results


def turnover(results: list[dict]) -> list[float]:
    """Fraction of high-leg + low-leg membership that changed between consecutive rebalances."""
    tos: list[float] = []
    for i in range(1, len(results)):
        prev_h, cur_h = set(results[i - 1]["high_syms"]), set(results[i]["high_syms"])
        prev_l, cur_l = set(results[i - 1]["low_syms"]), set(results[i]["low_syms"])
        ch_h = len(cur_h - prev_h) / max(len(cur_h), 1)
        ch_l = len(cur_l - prev_l) / max(len(cur_l), 1)
        tos.append(0.5 * (ch_h + ch_l))
    return tos


def bootstrap_ci(values: np.ndarray, n_boot: int = 10000, seed: int = 11) -> tuple[float, float, float]:
    rng = np.random.default_rng(seed)
    if len(values) < 2:
        return float(np.mean(values)) if len(values) else np.nan, np.nan, np.nan
    means = np.array([rng.choice(values, size=len(values), replace=True).mean() for _ in range(n_boot)])
    return float(np.mean(values)), float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))


def main() -> None:
    real = run(permute_beta=False)
    canary = run(permute_beta=True)
    tos = turnover(real)
    avg_turnover = float(np.mean(tos)) if tos else np.nan

    # per-rebalance overnight L/S (gross) — the non-overlapping bootstrap unit
    on_gross = np.array([r["overnight"] for r in real])
    intr_gross = np.array([r["intraday"] for r in real])
    d24_gross = np.array([r["ret24"] for r in real])

    # cost: per holding period charge spread on the changed fraction, one round trip (both legs), +2x stress.
    # avg fraction changed ~ avg_turnover; round-trip cost in return units = 2 sides * SPREAD_BPS/1e4 * turnover_frac
    # applied to each rebalance's overnight return. The first rebalance has no prior => full turnover.
    cost_per_rebal = []
    for i, r in enumerate(real):
        frac = 1.0 if i == 0 else tos[i - 1]
        rt_cost = 2.0 * (SPREAD_BPS / 1e4) * frac  # one round-trip both legs at per-side spread
        cost_per_rebal.append(rt_cost)
    cost_arr = np.array(cost_per_rebal)
    on_net = on_gross - cost_arr
    on_net_stress = on_gross - 2.0 * cost_arr

    n = len(real)
    half = n // 2
    oos_idx = list(range(half, n))  # second half = OOS rebalances
    on_oos_net = on_net[oos_idx] if len(oos_idx) else np.array([])

    boot = {
        "overnight_gross": bootstrap_ci(on_gross),
        "overnight_net": bootstrap_ci(on_net),
        "overnight_net_stress": bootstrap_ci(on_net_stress),
        "overnight_oos_net": bootstrap_ci(on_oos_net) if len(on_oos_net) >= 2 else None,
        "intraday_gross": bootstrap_ci(intr_gross),
        "ret24_gross": bootstrap_ci(d24_gross),
    }
    canary_on = np.array([r["overnight"] for r in canary]) if canary else np.array([])
    boot["canary_overnight_gross"] = bootstrap_ci(canary_on) if len(canary_on) >= 2 else None

    summary = {
        "n_rebalances": n,
        "n_liquid": N_LIQUID,
        "beta_window": BETA_WINDOW,
        "rebal_every": REBAL_EVERY,
        "q_size": real[0]["q_size"] if real else None,
        "avg_turnover": avg_turnover,
        "spread_bps_per_side": SPREAD_BPS,
        "mean_overnight_gross": float(np.mean(on_gross)),
        "mean_intraday_gross": float(np.mean(intr_gross)),
        "mean_24h_gross": float(np.mean(d24_gross)),
        "mean_overnight_net": float(np.mean(on_net)),
        "mean_overnight_net_stress": float(np.mean(on_net_stress)),
        "split_predicted_direction": bool(np.mean(on_gross) > np.mean(intr_gross)),
        "bootstrap": boot,
        "per_rebal": [
            {
                "est_date": r["est_date"],
                "overnight": r["overnight"],
                "intraday": r["intraday"],
                "ret24": r["ret24"],
                "mean_high_beta": r["mean_high_beta"],
                "mean_low_beta": r["mean_low_beta"],
                "overnight_n_days": r["overnight_n"],
            }
            for r in real
        ],
        "turnover_per_rebal": tos,
    }
    with open(OUT_JSON, "w") as f:
        json.dump(summary, f, indent=2, default=str)

    print("=== W11 OVERNIGHT-BETA PREMIUM ===")
    print(f"n_rebalances={n}  q_size={summary['q_size']}  avg_turnover={avg_turnover:.3f}")
    print(f"high-low-beta L/S mean per-rebalance return (per-day avg over hold):")
    print(f"  OVERNIGHT gross = {summary['mean_overnight_gross']*1e4:+.2f} bps/day")
    print(f"  INTRADAY  gross = {summary['mean_intraday_gross']*1e4:+.2f} bps/day")
    print(f"  24H       gross = {summary['mean_24h_gross']*1e4:+.2f} bps/day")
    print(f"  SPLIT predicted direction (overnight>intraday): {summary['split_predicted_direction']}")
    print(f"  OVERNIGHT net   = {summary['mean_overnight_net']*1e4:+.2f} bps/day  (stress {summary['mean_overnight_net_stress']*1e4:+.2f})")
    print("bootstrap CIs (mean, lo2.5, hi97.5) in bps/day:")
    for k, v in boot.items():
        if v is None:
            print(f"  {k}: n/a (too few)")
        else:
            print(f"  {k}: {v[0]*1e4:+.2f}  [{v[1]*1e4:+.2f}, {v[2]*1e4:+.2f}]")
    print("per-rebalance overnight (bps/day):", [round(r['overnight']*1e4, 1) for r in real])
    print("per-rebalance intraday  (bps/day):", [round(r['intraday']*1e4, 1) for r in real])
    print("high/low mean beta per rebal:", [(round(r['mean_high_beta'],2), round(r['mean_low_beta'],2)) for r in real])


if __name__ == "__main__":
    main()
