"""Attack 3: regime/year concentration. Extends run_b4_byyear.py with per-year SHARPE and the
cumulative-concentration decomposition: how much of the total net L/S P&L comes from the top 1-2
years? Reconcile the t=13 cross-sectional result with the Lane D near-null EW basket. Run for B4
(headline) and B1 (liquid) for contrast. Reuses the verbatim harness OOS-prediction + decile-L/S.
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

DATADIR = "/bands"
COSTS_PATH = "/bands/band_costs_deep.json"
HORIZON_MINUTES = 1440
CADENCE_MIN = 390
PERIODS_PER_YEAR = 252.0
N_FOLDS = 5
NUM_ROUNDS = 200
FRAC = 0.1
BORROW_BPS_ANNUAL = 50.0


def load_xy(band: str) -> tuple[list[dt.datetime], list[str], np.ndarray, np.ndarray]:
    data = np.load(os.path.join(DATADIR, f"band_{band}_fwd_1d.npz"), allow_pickle=True)
    ts = [dt.datetime.fromtimestamp(int(t) / 1e9, tz=dt.timezone.utc) for t in data["ts_ns"]]
    tbl = [str(s) for s in data["symbols"]]
    symbols = [tbl[i] for i in data["sym_idx"]]
    return ts, symbols, data["X"].astype(float), data["y"].astype(float)


def oos_predictions(X, y, ts, symbols):
    folds = walk_forward_folds(ts, HORIZON_MINUTES, N_FOLDS)
    collected = []
    for fold in folds:
        if len(fold.train_idx) < 500 or len(fold.test_idx) < 50:
            continue
        tr, te = fold.train_idx, fold.test_idx
        model = lgb.train(DEFAULT_LGB,
                          lgb.Dataset(X[tr], label=np.asarray([y[i] for i in tr], dtype=float)),
                          num_boost_round=NUM_ROUNDS)
        pred = model.predict(X[te])
        collected.extend((float(pred[j]), float(y[i]), ts[i], symbols[i]) for j, i in enumerate(te))
    return collected


def daily_net_pnl(preds, cost_bps_oneway):
    buckets = defaultdict(list)
    for pred, realized, group, sym in preds:
        if not (math.isnan(pred) or math.isnan(realized)):
            buckets[group].append((pred, realized, sym))
    cost = cost_bps_oneway / 1e4
    borrow = (BORROW_BPS_ANNUAL / 1e4) / PERIODS_PER_YEAR
    prev_w = {}
    series = []
    for group in sorted(buckets):
        rows = sorted(buckets[group], key=lambda r: r[0])
        k = max(1, int(FRAC * len(rows)))
        if len(rows) < 2 * k:
            continue
        shorts, longs = rows[:k], rows[-k:]
        weights = {}
        for _, _, sym in longs:
            weights[sym] = weights.get(sym, 0.0) + 1.0 / len(longs)
        for _, _, sym in shorts:
            weights[sym] = weights.get(sym, 0.0) - 1.0 / len(shorts)
        gross = sum(weights[sym] * realized for _, realized, sym in longs + shorts)
        turnover = sum(abs(weights.get(s, 0.0) - prev_w.get(s, 0.0)) for s in set(weights) | set(prev_w))
        net = gross - cost * turnover - borrow
        series.append((group, net))
        prev_w = weights
    return series


def run_band(band: str) -> None:
    costs = json.load(open(COSTS_PATH))
    own = float(costs[band]["oneway_cost_bps"])
    ts, symbols, X, y = load_xy(band)
    preds = oos_predictions(X, y, ts, symbols)
    series = daily_net_pnl(preds, own)
    by_year = defaultdict(dict)
    for group, net in series:
        by_year[group.year][group] = net

    print(f"\n=== {band} by-year (own cost {own:.2f}bps) ===")
    print(f"{'year':>6}{'days':>6}{'mean':>11}{'sum':>10}{'sharpe':>9}{'NWt':>7}")
    allvals = []
    year_sums = {}
    for year in sorted(by_year):
        vals = list(by_year[year].values())
        allvals.extend(vals)
        mean = statistics.mean(vals)
        s = sum(vals)
        year_sums[year] = s
        sd = statistics.pstdev(vals) if len(vals) > 1 else float("nan")
        sharpe = mean / sd * math.sqrt(PERIODS_PER_YEAR) if sd > 0 else float("nan")
        nwt = newey_west_tstat(by_year[year], lag=3) if len(by_year[year]) >= 3 else float("nan")
        print(f"{year:>6}{len(vals):>6}{mean:>11.6f}{s:>10.4f}{sharpe:>9.2f}{nwt:>7.2f}")

    full = {yr: s for yr, s in year_sums.items() if yr <= 2025}
    total = sum(full.values())
    # concentration: top-1 and top-2 positive years as fraction of total
    pos_sorted = sorted(full.items(), key=lambda kv: kv[1], reverse=True)
    top1 = pos_sorted[0]
    top2_sum = pos_sorted[0][1] + pos_sorted[1][1]
    print(f"  total cumulative net P&L (full yrs {min(full)}-2025): {total:.4f}")
    print(f"  TOP year {top1[0]}: {top1[1]:.4f} = {100*top1[1]/total:.1f}% of total")
    print(f"  TOP-2 years {pos_sorted[0][0]},{pos_sorted[1][0]}: {top2_sum:.4f} = {100*top2_sum/total:.1f}% of total")
    # overall sharpe
    omean = statistics.mean(allvals)
    osd = statistics.pstdev(allvals)
    print(f"  full-sample net Sharpe (all OOS days): {omean/osd*math.sqrt(PERIODS_PER_YEAR):.2f}")


def main() -> None:
    for band in ["B4_2000_4000", "B1_0001_0500"]:
        run_band(band)


if __name__ == "__main__":
    main()
