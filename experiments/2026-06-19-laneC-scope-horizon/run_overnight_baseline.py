"""Disciplined cross-sectional baseline on the overnight (close->next-open) panel.

Reuses quantlib.research.run_experiment (walk-forward purge, within-day SHUFFLE canary, NW-t, and
the net-of-cost L/S backtest). Sibling of the trusted-baseline run_baseline.py, parameterized for the
DAILY overnight regime per hypothesis.md:
  - horizon_minutes=1440  -> walk-forward purge of >=1 calendar day (no train label peeks into test),
  - cadence_min=390       -> periods_per_year = 252*(390/390) = 252 (one rebalance/day, correct
                             overnight annualization); NW lag = max(1, 1440//390) = 3 (conservative;
                             non-overlapping daily labels have true lag 1, so lag=3 only DEFLATES t).

Per the pre-registered gate (1d headline is the only claim; 2d/3d are descriptive by-horizon):
HIT iff REAL OOS IC >= 0.01 AND (REAL-SHUFFLE) >= 0.01 AND NW|t| >= 2.0 AND breakeven_cost_bps > 10.0.
"""
from __future__ import annotations

import datetime as dt
import glob
import os

import numpy as np

from quantlib.research import run_experiment

DATADIR = os.environ.get("DATADIR", "/app/experiments/data")
NPZ_PREFIX = os.environ.get("NPZ_PREFIX", "overnight_panel")
HORIZON_MINUTES = 1440      # overnight purge (>=1 day)
CADENCE_MIN = 390           # one rebalance per trading day -> periods_per_year = 252


def discover_horizons() -> list[str]:
    """fwd_<k>d npz files present for this prefix, ascending by k (days)."""
    cols = []
    for path in glob.glob(os.path.join(DATADIR, f"{NPZ_PREFIX}_fwd_*d.npz")):
        cols.append(os.path.basename(path).replace(f"{NPZ_PREFIX}_", "").replace(".npz", ""))
    return sorted(cols, key=lambda col: int(col[len("fwd_"):-1]))


def load_xy(horizon_col: str) -> tuple[list[str], list, list[str], np.ndarray, np.ndarray]:
    data = np.load(os.path.join(DATADIR, f"{NPZ_PREFIX}_{horizon_col}.npz"), allow_pickle=True)
    names = [str(n) for n in data["names"]]
    ts = [dt.datetime.fromtimestamp(int(t) / 1e9, tz=dt.timezone.utc) for t in data["ts_ns"]]
    symbol_table = [str(s) for s in data["symbols"]]
    symbols = [symbol_table[i] for i in data["sym_idx"]]
    X = data["X"].astype(float)
    y = data["y"].astype(float)
    return names, ts, symbols, X, y


def main() -> None:
    print(f"DATADIR={DATADIR}  prefix={NPZ_PREFIX}\n")
    for horizon_col in discover_horizons():
        horizon_days = int(horizon_col[len("fwd_"):-1])
        names, ts, symbols, X, y = load_xy(horizon_col)
        n_days = len({t for t in ts})
        print(f"================ {horizon_col} ({horizon_days}d overnight) ================")
        print(f"rows={len(y)} features={X.shape[1]} days={n_days} "
              f"symbols={len(set(symbols))} label_std={float(np.nanstd(y)):.6f}")
        result = run_experiment(
            X, y, ts, symbols=symbols, label="raw",
            n_folds=5, horizon_minutes=HORIZON_MINUTES, cadence_min=CADENCE_MIN,
            cost_bps_oneway=2.0,
        )
        print("  predict-zero baseline IC : 0.00000")
        print(f"  SHUFFLE canary IC        : {result['canary_ic']:.5f}")
        print(f"  REAL out-of-sample IC    : {result['mean_ic']:.5f}  (NW t={result['nw_t']})")
        edge = result["mean_ic"] - result["canary_ic"]
        print(f"  REAL - SHUFFLE           : {edge:+.5f}   "
              f"{'<-- beats canary' if edge > 0 else '<-- NO signal beyond shuffle'}")
        print(f"  net L/S per period       : {result['net_per_period']}  "
              f"sharpe_net={result['sharpe_net']}  breakeven_cost_bps={result['breakeven_cost_bps']}  "
              f"turnover={result['mean_turnover']}")
        gains = result["gain_importance"]
        ranked = sorted(zip(names, gains), key=lambda nv: nv[1], reverse=True)[:12]
        print("  top features by gain:")
        for name, gain in ranked:
            print(f"     {name:<24} {gain}")
        # explicit gate readout for the 1d headline
        if horizon_days == 1:
            be = result["breakeven_cost_bps"]
            be_val = be if isinstance(be, (int, float)) and be == be else float("nan")
            hit = (result["mean_ic"] >= 0.01 and edge >= 0.01
                   and abs(result["nw_t"]) >= 2.0 and be_val > 10.0)
            print(f"  >>> 1d GATE: IC>=0.01={result['mean_ic'] >= 0.01} "
                  f"edge>=0.01={edge >= 0.01} |t|>=2.0={abs(result['nw_t']) >= 2.0} "
                  f"breakeven>10={be_val > 10.0}  ==> {'HIT' if hit else 'MISS / null'}")
        print()


if __name__ == "__main__":
    main()
