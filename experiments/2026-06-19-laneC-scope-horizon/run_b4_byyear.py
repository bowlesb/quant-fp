"""B4 deep-revalidation: leg P (by-year persistence) for the pre-registered TOM-killer test.

Reproduces the UNMODIFIED quantlib.research walk-forward OOS predictions for the B4 band
(raw label, DEFAULT_LGB, n_folds=5, horizon_minutes=1440, cadence_min=390) -- the SAME model
run_experiment fits -- then computes the band's net-of-OWN-cost daily long/short P&L series and
splits it by CALENDAR YEAR to evaluate the pre-registered persistence requirement P:

  P1 (majority-positive): net edge POSITIVE in >= ceil(0.70 * N_full_years) full years.
  P2 (no decisive sign-flip): NO full year is both NEGATIVE and NW|t| >= 2.0.

The by-year net-edge table is the kill/confirm shot (exactly as it was for TOM). 2026 is a
partial stub: reported, excluded from the full-year count.

Cost = the band's OWN one-way cost = median Corwin-Schultz half-spread + 1bp, recomputed
point-in-time on the deep panel (read from band_costs_deep.json written by build_bands.py).
"""
from __future__ import annotations

import datetime as dt
import json
import math
import os
import statistics
from collections import defaultdict

import numpy as np

from quantlib.backtest import newey_west_tstat, walk_forward_folds
from quantlib.research import DEFAULT_LGB

import lightgbm as lgb

DATADIR = os.environ.get("DATADIR", "/app/experiments/data")
COSTS_PATH = os.environ.get("COSTS_PATH", os.path.join(DATADIR, "band_costs_deep.json"))
BAND = os.environ.get("BAND", "B4_2000_4000")
HORIZON_MINUTES = 1440
CADENCE_MIN = 390
PERIODS_PER_YEAR = 252.0 * (390.0 / CADENCE_MIN)  # == 252
N_FOLDS = 5
SEED = 13
NUM_ROUNDS = 200
FRAC = 0.1
BORROW_BPS_ANNUAL = 50.0


def load_xy(band: str) -> tuple[list[dt.datetime], list[str], np.ndarray, np.ndarray]:
    data = np.load(os.path.join(DATADIR, f"band_{band}_fwd_1d.npz"), allow_pickle=True)
    ts = [dt.datetime.fromtimestamp(int(t) / 1e9, tz=dt.timezone.utc) for t in data["ts_ns"]]
    symbol_table = [str(s) for s in data["symbols"]]
    symbols = [symbol_table[i] for i in data["sym_idx"]]
    return ts, symbols, data["X"].astype(float), data["y"].astype(float)


def oos_predictions(
    X: np.ndarray, y: np.ndarray, ts: list[dt.datetime], symbols: list[str]
) -> list[tuple[float, float, dt.datetime, str]]:
    """Reproduce run_experiment's raw-label walk-forward OOS predictions, verbatim."""
    folds = walk_forward_folds(ts, HORIZON_MINUTES, N_FOLDS)
    collected: list[tuple[float, float, dt.datetime, str]] = []
    for fold in folds:
        if len(fold.train_idx) < 500 or len(fold.test_idx) < 50:
            continue
        tr, te = fold.train_idx, fold.test_idx
        model = lgb.train(
            DEFAULT_LGB,
            lgb.Dataset(X[tr], label=np.asarray([y[i] for i in tr], dtype=float)),
            num_boost_round=NUM_ROUNDS,
        )
        pred = model.predict(X[te])
        collected.extend(
            (float(pred[j]), float(y[i]), ts[i], symbols[i]) for j, i in enumerate(te)
        )
    return collected


def daily_net_pnl(
    preds: list[tuple[float, float, dt.datetime, str]], cost_bps_oneway: float
) -> list[tuple[dt.datetime, float]]:
    """Per-timestamp dollar-neutral top/bottom-decile L/S NET-of-cost P&L (long_short_backtest
    logic, verbatim), returned as a (timestamp, net) series for by-year splitting."""
    buckets: dict[dt.datetime, list[tuple[float, float, str]]] = defaultdict(list)
    for pred, realized, group, sym in preds:
        if not (math.isnan(pred) or math.isnan(realized)):
            buckets[group].append((pred, realized, sym))
    cost = cost_bps_oneway / 1e4
    borrow_per_period = (BORROW_BPS_ANNUAL / 1e4) / PERIODS_PER_YEAR
    prev_w: dict[str, float] = {}
    series: list[tuple[dt.datetime, float]] = []
    for group in sorted(buckets):
        rows = sorted(buckets[group], key=lambda row: row[0])
        k = max(1, int(FRAC * len(rows)))
        if len(rows) < 2 * k:
            continue
        shorts, longs = rows[:k], rows[-k:]
        weights: dict[str, float] = {}
        for _, _, sym in longs:
            weights[sym] = weights.get(sym, 0.0) + 1.0 / len(longs)
        for _, _, sym in shorts:
            weights[sym] = weights.get(sym, 0.0) - 1.0 / len(shorts)
        gross = sum(weights[sym] * realized for _, realized, sym in longs + shorts)
        turnover = sum(
            abs(weights.get(sym, 0.0) - prev_w.get(sym, 0.0))
            for sym in set(weights) | set(prev_w)
        )
        net = gross - cost * turnover - borrow_per_period
        series.append((group, net))
        prev_w = weights
    return series


def year_nw_t(net_by_day: dict[dt.datetime, float]) -> float:
    """NW |t| of a single year's daily-mean-zero null using the harness's own estimator."""
    if len(net_by_day) < 3:
        return float("nan")
    return newey_west_tstat(net_by_day, lag=3)


def main() -> None:
    with open(COSTS_PATH) as fh:
        costs = json.load(fh)
    own_cost = float(costs[BAND]["oneway_cost_bps"])

    ts, symbols, X, y = load_xy(BAND)
    n_days = len({t.date() for t in ts})
    print(f"=== {BAND} by-year persistence (leg P) ===")
    print(f"rows={len(y)} days={n_days} symbols={len(set(symbols))} own_oneway_cost={own_cost:.2f}bps")

    preds = oos_predictions(X, y, ts, symbols)
    print(f"OOS prediction rows: {len(preds)}  OOS timestamps: {len({p[2] for p in preds})}")

    series = daily_net_pnl(preds, own_cost)
    print(f"daily net L/S P&L periods: {len(series)}")

    by_year: dict[int, dict[dt.datetime, float]] = defaultdict(dict)
    for group, net in series:
        by_year[group.year][group] = net

    print("\n================ BY-YEAR NET-OF-OWN-COST L/S TABLE ================")
    print(f"{'year':>6}{'days':>6}{'mean_net':>12}{'sum_net':>12}{'NWt':>8}{'sign':>6}")
    rows_out = []
    for year in sorted(by_year):
        net_map = by_year[year]
        days = len(net_map)
        vals = list(net_map.values())
        mean_net = statistics.mean(vals)
        sum_net = sum(vals)
        nwt = year_nw_t(net_map)
        sign = "+" if mean_net > 0 else "-"
        rows_out.append((year, days, mean_net, sum_net, nwt, sign))
        print(f"{year:>6}{days:>6}{mean_net:>12.6f}{sum_net:>12.4f}{nwt:>8.2f}{sign:>6}")

    full_years = [r for r in rows_out if r[0] <= 2025]
    n_full = len(full_years)
    n_pos = sum(1 for r in full_years if r[2] > 0)
    threshold = math.ceil(0.70 * n_full)
    p1 = n_pos >= threshold
    # P2: any full year both NEGATIVE and NW|t| >= 2.0
    sig_neg = [r for r in full_years if r[2] < 0 and (r[4] == r[4]) and abs(r[4]) >= 2.0]
    p2 = len(sig_neg) == 0

    print(f"\nFull years (<=2025): {n_full}  positive: {n_pos}  threshold(>=ceil(.7*N)): {threshold}")
    print(f"P1 (majority-positive): {'PASS' if p1 else 'FAIL'}")
    if sig_neg:
        print(f"P2 sign-flip years (NEG & |t|>=2): {[r[0] for r in sig_neg]}")
    print(f"P2 (no significant negative year): {'PASS' if p2 else 'FAIL'}")
    print(f"\nLEG P (P1 AND P2): {'PASS' if (p1 and p2) else 'FAIL'}")


if __name__ == "__main__":
    main()
