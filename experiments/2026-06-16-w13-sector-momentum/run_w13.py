"""W13 — Sector / industry momentum via the 11 SPDR sector ETFs (pre-registered).

Friction-wall design: the 11 SPDR sector ETFs are the lowest-friction liquid instruments. If momentum
pays anywhere net-of-cost it should be here. Honest risk: small-N (11 instruments, ~15 monthly rebalances
on 18mo) -> wide CIs; report power.

Steps:
  1. Build a daily close panel for the 11 sector ETFs (+ SPY market) over the 378 trading days.
     Daily close = close of the LAST RTH bar (09:30-16:00 ET, DST-safe via tz convert + Int32 minute).
  2. Cross-sectional momentum L/S: each rebalance (every 21 trading days), rank 11 sectors by trailing
     return over F in {21,63,126}; long top-3, short bottom-3, equal-weight; hold 21 days; non-overlapping.
  3. Time-series / absolute momentum: long sectors with positive trailing return, short negative.
  4. Cost: 0.4 bps round-trip ETF spread (no quotes for ETFs in store) x turnover.
  5. Gates: per-rebalance bootstrap (10k), block-bootstrap of the monthly return series, walk-forward OOS
     (first-half / second-half by date), shuffle-canary (permute sector -> forward return). Report n_rebalances.

Reads /store (RO) only. Writes panel + result CSVs into this experiment dir.
"""
from __future__ import annotations

import glob

import numpy as np
import polars as pl

SECTORS = ["XLK", "XLF", "XLE", "XLV", "XLI", "XLY", "XLP", "XLU", "XLB", "XLRE", "XLC"]
MARKET = "SPY"
FORMATIONS = [21, 63, 126]
HOLD = 21
TOP_K = 3
SPREAD_BPS = 0.4  # round-trip; no ETF quotes in /store, conservative liquid-ETF assumption
RTH_OPEN_MIN = 9 * 60 + 30  # 570 (09:30 ET)
RTH_CLOSE_MIN = 16 * 60  # 960 (16:00 ET)
OUT_DIR = "/app/experiments/2026-06-16-w13-sector-momentum"


def daily_close_for_symbol(symbol: str) -> pl.DataFrame:
    """Return a (date, symbol, close) frame: one RTH close per trading day for a symbol."""
    files = sorted(glob.glob(f"/store/raw/bars/symbol={symbol}/date=*/data.parquet"))
    rows: list[dict[str, object]] = []
    for path in files:
        date_str = path.split("date=")[1].split("/")[0]
        df = pl.read_parquet(path).with_columns(
            pl.col("ts").dt.convert_time_zone("America/New_York").alias("et")
        )
        # Int32 cast is REQUIRED: hour() is i8 and hour*60 overflows silently (the RTH-empty bug).
        df = df.with_columns(
            (pl.col("et").dt.hour().cast(pl.Int32) * 60 + pl.col("et").dt.minute().cast(pl.Int32)).alias("em")
        )
        rth = df.filter((pl.col("em") >= RTH_OPEN_MIN) & (pl.col("em") <= RTH_CLOSE_MIN)).sort("et")
        if rth.height == 0:
            continue
        close = float(rth["close"].to_list()[-1])
        rows.append({"date": date_str, "symbol": symbol, "close": close})
    return pl.DataFrame(rows)


def build_panel() -> pl.DataFrame:
    """Wide close panel: index date, one column per symbol. Aligned on common dates."""
    frames = [daily_close_for_symbol(sym) for sym in SECTORS + [MARKET]]
    long = pl.concat(frames)
    wide = long.pivot(values="close", index="date", on="symbol").sort("date")
    # keep only dates where ALL symbols present (drop any partial day)
    wide = wide.drop_nulls()
    return wide


def trailing_return(closes: np.ndarray, idx: int, formation: int) -> float:
    """Trailing simple return from idx-formation to idx (uses close[idx-formation]..close[idx])."""
    if idx - formation < 0:
        return np.nan
    return float(closes[idx] / closes[idx - formation] - 1.0)


def forward_return(closes: np.ndarray, idx: int, hold: int) -> float:
    """Forward simple return from idx to idx+hold."""
    if idx + hold >= len(closes):
        return np.nan
    return float(closes[idx + hold] / closes[idx] - 1.0)


def run_cross_sectional(
    panel: pl.DataFrame, formation: int
) -> tuple[list[float], list[dict[str, object]], list[tuple[int, dict[str, float]]]]:
    """Cross-sectional L/S (long top-3, short bottom-3) per non-overlapping 21d rebalance.

    Returns:
      net_returns: per-rebalance net-of-cost L/S portfolio returns
      detail: per-rebalance bookkeeping rows
      canary_inputs: list of (rebalance_idx, {symbol: fwd_return}) for the shuffle canary
    """
    dates = panel["date"].to_list()
    closes = {sym: panel[sym].to_numpy() for sym in SECTORS}
    n_days = len(dates)
    net_returns: list[float] = []
    detail: list[dict[str, object]] = []
    canary_inputs: list[tuple[int, dict[str, float]]] = []

    # rebalance points: every HOLD days, starting once formation history exists
    start = formation
    rebal_idxs = list(range(start, n_days - HOLD, HOLD))
    prev_long: set[str] = set()
    prev_short: set[str] = set()

    for idx in rebal_idxs:
        trailing = {sym: trailing_return(closes[sym], idx, formation) for sym in SECTORS}
        forward = {sym: forward_return(closes[sym], idx, HOLD) for sym in SECTORS}
        if any(not np.isfinite(v) for v in trailing.values()):
            continue
        if any(not np.isfinite(v) for v in forward.values()):
            continue
        ranked = sorted(SECTORS, key=lambda s: trailing[s])
        short_legs = set(ranked[:TOP_K])
        long_legs = set(ranked[-TOP_K:])
        long_ret = float(np.mean([forward[s] for s in long_legs]))
        short_ret = float(np.mean([forward[s] for s in short_legs]))
        gross = long_ret - short_ret

        # turnover: fraction of legs that changed vs prior rebalance, both sides; each changed leg pays a
        # round-trip. New legs entering + old legs exiting. equal weight 1/TOP_K per leg per side.
        long_turn = len(long_legs.symmetric_difference(prev_long)) / (2 * TOP_K)
        short_turn = len(short_legs.symmetric_difference(prev_short)) / (2 * TOP_K)
        # total turnover as a fraction of the 2-sided book (long + short each weight 1.0 gross)
        turnover = (len(long_legs.symmetric_difference(prev_long)) + len(short_legs.symmetric_difference(prev_short))) / (2 * TOP_K)
        cost = turnover * (SPREAD_BPS / 10000.0)
        net = gross - cost

        net_returns.append(net)
        detail.append({
            "rebal_idx": idx, "date": dates[idx], "long": sorted(long_legs),
            "short": sorted(short_legs), "long_ret": long_ret, "short_ret": short_ret,
            "gross": gross, "turnover": turnover, "cost": cost, "net": net,
        })
        canary_inputs.append((idx, forward))
        prev_long, prev_short = long_legs, short_legs

    return net_returns, detail, canary_inputs


def run_time_series(panel: pl.DataFrame, formation: int) -> tuple[list[float], list[dict[str, object]]]:
    """Absolute / time-series momentum: long sectors with positive trailing return, short negative.

    Equal-weight within each side; portfolio = mean(long fwd) - mean(short fwd). If a side is empty that
    side contributes 0 (i.e. that rebalance is one-sided). Non-overlapping 21d holds."""
    dates = panel["date"].to_list()
    closes = {sym: panel[sym].to_numpy() for sym in SECTORS}
    n_days = len(dates)
    net_returns: list[float] = []
    detail: list[dict[str, object]] = []
    start = formation
    rebal_idxs = list(range(start, n_days - HOLD, HOLD))
    prev_long: set[str] = set()
    prev_short: set[str] = set()

    for idx in rebal_idxs:
        trailing = {sym: trailing_return(closes[sym], idx, formation) for sym in SECTORS}
        forward = {sym: forward_return(closes[sym], idx, HOLD) for sym in SECTORS}
        if any(not np.isfinite(v) for v in trailing.values()):
            continue
        if any(not np.isfinite(v) for v in forward.values()):
            continue
        long_legs = {s for s in SECTORS if trailing[s] > 0}
        short_legs = {s for s in SECTORS if trailing[s] < 0}
        long_ret = float(np.mean([forward[s] for s in long_legs])) if long_legs else 0.0
        short_ret = float(np.mean([forward[s] for s in short_legs])) if short_legs else 0.0
        gross = long_ret - short_ret
        # turnover: count of legs changing on each side, normalized by max book size (11 names, 2 sides)
        changed = len(long_legs.symmetric_difference(prev_long)) + len(short_legs.symmetric_difference(prev_short))
        turnover = changed / len(SECTORS)
        cost = turnover * (SPREAD_BPS / 10000.0)
        net = gross - cost
        net_returns.append(net)
        detail.append({
            "rebal_idx": idx, "date": dates[idx], "n_long": len(long_legs), "n_short": len(short_legs),
            "gross": gross, "turnover": turnover, "cost": cost, "net": net,
        })
        prev_long, prev_short = long_legs, short_legs
    return net_returns, detail


def bootstrap_mean_ci(returns: list[float], n_boot: int = 10000, seed: int = 7) -> dict[str, float]:
    """IID per-rebalance bootstrap 95% CI of the MEAN return (bps)."""
    arr = np.array([r for r in returns if np.isfinite(r)])
    n = len(arr)
    if n < 3:
        return {"n": float(n), "mean_bps": np.nan, "ci_lo_bps": np.nan, "ci_hi_bps": np.nan, "t": np.nan}
    mean = float(arr.mean())
    se = float(arr.std(ddof=1) / np.sqrt(n))
    t_stat = mean / se if se > 0 else np.nan
    rng = np.random.default_rng(seed)
    boot = rng.choice(arr, size=(n_boot, n), replace=True).mean(axis=1)
    return {
        "n": float(n),
        "mean_bps": mean * 10000.0,
        "ci_lo_bps": float(np.percentile(boot, 2.5)) * 10000.0,
        "ci_hi_bps": float(np.percentile(boot, 97.5)) * 10000.0,
        "t": t_stat,
    }


def block_bootstrap_ci(returns: list[float], block: int = 3, n_boot: int = 10000, seed: int = 11) -> dict[str, float]:
    """Moving-block bootstrap 95% CI of the mean — accounts for any serial dependence in the rebalance
    series (the better significance test given the coarse 11-instrument cross-section)."""
    arr = np.array([r for r in returns if np.isfinite(r)])
    n = len(arr)
    if n < 4:
        return {"mean_bps": np.nan, "ci_lo_bps": np.nan, "ci_hi_bps": np.nan, "block": block}
    rng = np.random.default_rng(seed)
    n_blocks = int(np.ceil(n / block))
    max_start = n - block
    means = np.empty(n_boot)
    for b in range(n_boot):
        starts = rng.integers(0, max_start + 1, size=n_blocks)
        sample = np.concatenate([arr[s : s + block] for s in starts])[:n]
        means[b] = sample.mean()
    return {
        "mean_bps": float(arr.mean()) * 10000.0,
        "ci_lo_bps": float(np.percentile(means, 2.5)) * 10000.0,
        "ci_hi_bps": float(np.percentile(means, 97.5)) * 10000.0,
        "block": block,
    }


def shuffle_canary(
    canary_inputs: list[tuple[int, dict[str, float]]], formation: int, panel: pl.DataFrame,
    n_perm: int = 2000, seed: int = 23,
) -> dict[str, float]:
    """Permute the sector->forward-return mapping each rebalance and recompute the L/S mean. If the real
    mean sits inside the permutation null, the ranking carries no information (coarse with 11 sectors)."""
    dates = panel["date"].to_list()
    closes = {sym: panel[sym].to_numpy() for sym in SECTORS}
    real_returns: list[float] = []
    trailings: list[dict[str, float]] = []
    forwards: list[dict[str, float]] = []
    for idx, forward in canary_inputs:
        trailing = {sym: trailing_return(closes[sym], idx, formation) for sym in SECTORS}
        ranked = sorted(SECTORS, key=lambda s: trailing[s])
        short_legs = ranked[:TOP_K]
        long_legs = ranked[-TOP_K:]
        real_returns.append(float(np.mean([forward[s] for s in long_legs])) - float(np.mean([forward[s] for s in short_legs])))
        trailings.append(trailing)
        forwards.append(forward)
    real_mean = float(np.mean(real_returns))
    rng = np.random.default_rng(seed)
    perm_means = np.empty(n_perm)
    for p in range(n_perm):
        vals: list[float] = []
        for trailing, forward in zip(trailings, forwards):
            fwd_vals = np.array([forward[s] for s in SECTORS])
            rng.shuffle(fwd_vals)
            shuffled = dict(zip(SECTORS, fwd_vals))
            ranked = sorted(SECTORS, key=lambda s: trailing[s])
            short_legs = ranked[:TOP_K]
            long_legs = ranked[-TOP_K:]
            vals.append(float(np.mean([shuffled[s] for s in long_legs])) - float(np.mean([shuffled[s] for s in short_legs])))
        perm_means[p] = np.mean(vals)
    p_value = float((np.abs(perm_means) >= abs(real_mean)).mean())
    return {
        "real_mean_bps": real_mean * 10000.0,
        "perm_mean_bps": float(perm_means.mean()) * 10000.0,
        "perm_p95_bps": float(np.percentile(perm_means, 97.5)) * 10000.0,
        "perm_p_value": p_value,
    }


def split_oos(returns: list[float], detail: list[dict[str, object]]) -> tuple[list[float], list[float]]:
    """First-half / second-half by date order (rebalances are already chronological)."""
    n = len(returns)
    half = n // 2
    return returns[:half], returns[half:]


def main() -> None:
    print("Building daily close panel (11 sectors + SPY) ...")
    panel = build_panel()
    panel.write_parquet(f"{OUT_DIR}/panel.parquet")
    print(f"Panel: {panel.height} aligned trading days, cols={panel.columns}")

    xs_summary: list[dict[str, object]] = []
    ts_summary: list[dict[str, object]] = []
    all_detail_rows: list[dict[str, object]] = []

    for formation in FORMATIONS:
        # ---- cross-sectional ----
        net, detail, canary_inputs = run_cross_sectional(panel, formation)
        for row in detail:
            all_detail_rows.append({**row, "formation": formation, "form": "cross_sectional",
                                    "long": ",".join(row["long"]), "short": ",".join(row["short"])})  # type: ignore[arg-type]
        boot = bootstrap_mean_ci(net)
        block = block_bootstrap_ci(net)
        first, second = split_oos(net, detail)
        boot_is = bootstrap_mean_ci(first)
        boot_oos = bootstrap_mean_ci(second)
        block_oos = block_bootstrap_ci(second)
        canary = shuffle_canary(canary_inputs, formation, panel)
        gross_mean = float(np.mean([r["gross"] for r in detail])) * 10000.0
        turn_mean = float(np.mean([r["turnover"] for r in detail]))
        cost_mean = float(np.mean([r["cost"] for r in detail])) * 10000.0
        xs_summary.append({
            "formation": formation, "n_rebalances": len(net),
            "gross_bps": round(gross_mean, 2), "turnover": round(turn_mean, 3), "cost_bps": round(cost_mean, 3),
            "net_mean_bps": round(boot["mean_bps"], 2), "net_t": round(boot["t"], 2),
            "boot_ci_lo": round(boot["ci_lo_bps"], 2), "boot_ci_hi": round(boot["ci_hi_bps"], 2),
            "block_ci_lo": round(block["ci_lo_bps"], 2), "block_ci_hi": round(block["ci_hi_bps"], 2),
            "is_net_bps": round(boot_is["mean_bps"], 2), "is_n": int(boot_is["n"]),
            "oos_net_bps": round(boot_oos["mean_bps"], 2), "oos_n": int(boot_oos["n"]),
            "oos_boot_ci_lo": round(boot_oos["ci_lo_bps"], 2), "oos_boot_ci_hi": round(boot_oos["ci_hi_bps"], 2),
            "oos_block_ci_lo": round(block_oos["ci_lo_bps"], 2), "oos_block_ci_hi": round(block_oos["ci_hi_bps"], 2),
            "canary_real_bps": round(canary["real_mean_bps"], 2), "canary_p": round(canary["perm_p_value"], 3),
            "canary_perm_p95_bps": round(canary["perm_p95_bps"], 2),
        })

        # ---- time-series / absolute ----
        ts_net, ts_detail = run_time_series(panel, formation)
        for row in ts_detail:
            all_detail_rows.append({**row, "formation": formation, "form": "time_series", "long": "", "short": ""})
        ts_boot = bootstrap_mean_ci(ts_net)
        ts_block = block_bootstrap_ci(ts_net)
        ts_first, ts_second = split_oos(ts_net, ts_detail)
        ts_boot_oos = bootstrap_mean_ci(ts_second)
        ts_block_oos = block_bootstrap_ci(ts_second)
        ts_gross = float(np.mean([r["gross"] for r in ts_detail])) * 10000.0
        ts_turn = float(np.mean([r["turnover"] for r in ts_detail]))
        ts_cost = float(np.mean([r["cost"] for r in ts_detail])) * 10000.0
        ts_summary.append({
            "formation": formation, "n_rebalances": len(ts_net),
            "gross_bps": round(ts_gross, 2), "turnover": round(ts_turn, 3), "cost_bps": round(ts_cost, 3),
            "net_mean_bps": round(ts_boot["mean_bps"], 2), "net_t": round(ts_boot["t"], 2),
            "boot_ci_lo": round(ts_boot["ci_lo_bps"], 2), "boot_ci_hi": round(ts_boot["ci_hi_bps"], 2),
            "block_ci_lo": round(ts_block["ci_lo_bps"], 2), "block_ci_hi": round(ts_block["ci_hi_bps"], 2),
            "oos_net_bps": round(ts_boot_oos["mean_bps"], 2), "oos_n": int(ts_boot_oos["n"]),
            "oos_boot_ci_lo": round(ts_boot_oos["ci_lo_bps"], 2), "oos_boot_ci_hi": round(ts_boot_oos["ci_hi_bps"], 2),
            "oos_block_ci_lo": round(ts_block_oos["ci_lo_bps"], 2), "oos_block_ci_hi": round(ts_block_oos["ci_hi_bps"], 2),
        })

    pl.DataFrame(xs_summary).write_csv(f"{OUT_DIR}/cross_sectional_results.csv")
    pl.DataFrame(ts_summary).write_csv(f"{OUT_DIR}/time_series_results.csv")
    pl.DataFrame(all_detail_rows).write_csv(f"{OUT_DIR}/rebalance_detail.csv")

    print("\n=== CROSS-SECTIONAL (long top-3 / short bottom-3) ===")
    print(pl.DataFrame(xs_summary))
    print("\n=== TIME-SERIES / ABSOLUTE (long >0 / short <0 trailing) ===")
    print(pl.DataFrame(ts_summary))


if __name__ == "__main__":
    main()
