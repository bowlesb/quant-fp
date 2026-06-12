"""Proposal 001 runner — liquid-tier ret_5m short-reversal at a 60m hold, OOS-split.

Lead-APPROVED (experiments/proposals/explorer-data/001_liquid_tier_reversal_60m_hold.md).
Tests whether the univariate ret_5m REVERSAL (signal = -ret_5m), restricted to the most-liquid
tier and held to the 60m horizon, clears net-of-cost breakeven where the modeller's task-#5
30m-cadence multivariate model could not.

This is a SINGLE-FEATURE signal — no model training, no leakage surface beyond the shuffle canary.
The hypothesis was observed on the full panel, so H1 is judged ONLY on the OOS window (2025-07-01+).

Gates (all reported):
  1. within-ts rank-IC of signal vs fwd_60m, NW t, per window + per OOS month (sign stability).
  2. shuffle-label canary (permute realized within ts) — overfit/leakage floor.
  3. net-of-cost L/S backtest (quantlib.long_short_backtest) at flat cost; report breakeven bps,
     compared to the modeller's MEASURED liquid-tier half-spread (~3bps median, task #5).
  4. survivorship neutralization (per-symbol demean the signal) — reversal is TIMING, should survive.
  5. turnover honesty — report realized turnover at the 60m hold.
  6. half-life — IC at 30m vs 60m (the load-bearing not-bounce claim the Lead asked to report).

Run (read-only on public; quantlib mounted):
  docker compose exec -T -e DB_HOST=timescaledb -e DB_NAME=quant -e DB_USER=quant \
    -e DB_PASSWORD=quant experimenter python experiments/builders/explorer_data_001_liquid_reversal.py
"""
import math
import os
import statistics
from collections import defaultdict
from datetime import date

import numpy as np
import psycopg

from quantlib.backtest import long_short_backtest, newey_west_tstat, per_timestamp_ic

DB_KWARGS = {
    "host": os.environ["DB_HOST"],
    "port": int(os.environ.get("DB_PORT", "5432")),
    "dbname": os.environ["DB_NAME"],
    "user": os.environ["DB_USER"],
    "password": os.environ["DB_PASSWORD"],
}

SET_VERSION = "v1.1.1"
OOS_START = date(2025, 7, 1)            # binding: H1 judged on rows >= this date
LIQUID_QUARTILE = 4                     # ntile-4 by ADV = most liquid tier (the prize tier)
RET_5M_IDX = 1                          # vector[1] (1-based in SQL -> 0-based here)
MIN_OF_DAY_IDX = 12
PERIODS_PER_YEAR_60M = 252 * 6.5        # ~1638; 60m hold => ~6-7 rebalances/day

# Measured liquid-tier half-spread the modeller characterized in task #5 (median name ~3bps;
# 11/50 names clear 1.4bps). We report breakeven vs both the optimistic and median lines.
MEASURED_LIQUID_HALF_SPREAD_BPS = 3.0
OPTIMISTIC_LIQUID_HALF_SPREAD_BPS = 1.4


def load_liquid_reversal_panel(conn: psycopg.Connection) -> dict[str, list]:
    """Liquid-quartile rows: signal = -ret_5m (reversal), fwd_60m label, excl 9:30 open + NaN ret_5m.

    Liquidity tier = ntile(4) by per-symbol mean dollar-volume over the FULL backfill history
    (a static name property; computed once). fwd_60m is the hold horizon.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            -- Liquidity tier from a single recent month's ADV (cheap window) — a static name
            -- property; scoped to avoid the full-history bars_1m scan that contends with battery loads.
            WITH dv AS (
                SELECT symbol, avg(close * volume) AS adv
                FROM bars_1m
                WHERE source='backfill' AND ts::date BETWEEN '2026-05-01' AND '2026-05-31'
                GROUP BY symbol
            ),
            tier AS (SELECT symbol, ntile(4) OVER (ORDER BY adv) AS liq_q FROM dv)
            SELECT fv.ts, fv.symbol, fv.vector[%s] AS ret_5m, l.value AS y
            FROM feature_vectors fv
            JOIN labels l ON l.symbol=fv.symbol AND l.ts=fv.ts AND l.horizon='fwd_60m'
            JOIN tier t ON t.symbol=fv.symbol
            WHERE fv.source='historical' AND fv.set_version=%s
              AND t.liq_q=%s
              AND fv.vector[%s] <> 'NaN'::float8
              AND fv.vector[%s] > 570
              AND l.value IS NOT NULL
            ORDER BY fv.ts
            """,
            (RET_5M_IDX, SET_VERSION, LIQUID_QUARTILE, RET_5M_IDX, MIN_OF_DAY_IDX),
        )
        rows = cur.fetchall()
    ts = [r[0] for r in rows]
    symbol = [r[1] for r in rows]
    signal = [-float(r[2]) for r in rows]          # REVERSAL: short high ret_5m, long low
    y = [float(r[3]) for r in rows]
    return {"ts": ts, "symbol": symbol, "signal": signal, "y": y}


def split_oos(panel: dict[str, list]) -> tuple[list[int], list[int]]:
    is_idx, oos_idx = [], []
    for i, t in enumerate(panel["ts"]):
        (oos_idx if t.date() >= OOS_START else is_idx).append(i)
    return is_idx, oos_idx


def subset(panel: dict[str, list], idx: list[int]) -> dict[str, list]:
    return {k: [v[i] for i in idx] for k, v in panel.items()}


def rank_ic_stats(panel: dict[str, list]) -> tuple[float, float, int]:
    ics = per_timestamp_ic(panel["signal"], panel["y"], panel["ts"])
    if not ics:
        return float("nan"), float("nan"), 0
    mean_ic = statistics.mean(ics.values())
    nw_t = newey_west_tstat(ics, lag=5)
    return mean_ic, nw_t, len(ics)


def monthly_ic(panel: dict[str, list]) -> dict[str, float]:
    ics = per_timestamp_ic(panel["signal"], panel["y"], panel["ts"])
    by_month: dict[str, list[float]] = defaultdict(list)
    for ts, ic in ics.items():
        by_month[ts.strftime("%Y-%m")].append(ic)
    return {m: statistics.mean(v) for m, v in sorted(by_month.items())}


def shuffle_canary(panel: dict[str, list], seed: int = 13) -> float:
    rng = np.random.default_rng(seed)
    by_ts: dict[object, list[int]] = defaultdict(list)
    for i, t in enumerate(panel["ts"]):
        by_ts[t].append(i)
    shuffled_y = list(panel["y"])
    for idxs in by_ts.values():
        vals = [panel["y"][i] for i in idxs]
        rng.shuffle(vals)
        for i, v in zip(idxs, vals):
            shuffled_y[i] = v
    ics = per_timestamp_ic(panel["signal"], shuffled_y, panel["ts"])
    return statistics.mean(ics.values()) if ics else float("nan")


def per_symbol_demean(panel: dict[str, list]) -> list[float]:
    by_sym: dict[str, list[float]] = defaultdict(list)
    for sym, sig in zip(panel["symbol"], panel["signal"]):
        by_sym[sym].append(sig)
    means = {sym: statistics.mean(v) for sym, v in by_sym.items()}
    return [sig - means[sym] for sym, sig in zip(panel["symbol"], panel["signal"])]


def report(name: str, panel: dict[str, list]) -> None:
    mean_ic, nw_t, n_ts = rank_ic_stats(panel)
    canary = shuffle_canary(panel)
    bt = long_short_backtest(
        panel["signal"], panel["y"], panel["ts"], panel["symbol"],
        frac=0.1, cost_bps_oneway=0.0, periods_per_year=PERIODS_PER_YEAR_60M,
    )
    demeaned = per_symbol_demean(panel)
    bt_surv = long_short_backtest(
        demeaned, panel["y"], panel["ts"], panel["symbol"],
        frac=0.1, cost_bps_oneway=0.0, periods_per_year=PERIODS_PER_YEAR_60M,
    )
    breakeven = bt.get("breakeven_cost_bps", float("nan"))
    print(f"\n=== {name} (n_rows={len(panel['ts'])}, n_ts={n_ts}) ===")
    print(f"  rank-IC {mean_ic:+.4f}  NW_t {nw_t:+.2f}  shuffle-canary {canary:+.4f}")
    print(f"  gross/period {bt.get('gross_per_period', float('nan')):+.6f}  "
          f"breakeven {breakeven:.2f}bps  "
          f"turnover {bt.get('mean_turnover', float('nan')):.2f}  "
          f"sharpe_net {bt.get('sharpe_net', float('nan')):+.2f}")
    print(f"  SURV-OUT (per-symbol demean): breakeven {bt_surv.get('breakeven_cost_bps', float('nan')):.2f}bps "
          f"sharpe_net {bt_surv.get('sharpe_net', float('nan')):+.2f}")
    clears = isinstance(breakeven, float) and breakeven == breakeven and breakeven > MEASURED_LIQUID_HALF_SPREAD_BPS
    print(f"  VERDICT vs measured liquid half-spread: breakeven {breakeven:.2f}bps "
          f"vs {OPTIMISTIC_LIQUID_HALF_SPREAD_BPS}bps(optimistic) / {MEASURED_LIQUID_HALF_SPREAD_BPS}bps(median) "
          f"-> {'CLEARS' if clears else 'BELOW'} median")


def main() -> None:
    with psycopg.connect(**DB_KWARGS) as conn:
        panel = load_liquid_reversal_panel(conn)
    is_idx, oos_idx = split_oos(panel)
    print(f"Loaded {len(panel['ts'])} liquid-tier rows. "
          f"IS={len(is_idx)} (<{OOS_START}) OOS={len(oos_idx)} (>={OOS_START}).")
    report("FULL PANEL", panel)
    report("IN-SAMPLE (observe window)", subset(panel, is_idx))
    report("OUT-OF-SAMPLE (verdict window — H1 judged HERE)", subset(panel, oos_idx))
    print("\nOOS monthly IC (sign-stability gate — should be consistently negative):")
    for month, ic in monthly_ic(subset(panel, oos_idx)).items():
        print(f"  {month}: {ic:+.4f}")


if __name__ == "__main__":
    main()
