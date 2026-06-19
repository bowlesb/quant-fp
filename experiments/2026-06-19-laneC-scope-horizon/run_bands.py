"""Net-of-cost boundary adjudication runner (boundary_hypothesis.md).

For each pre-declared ADV-rank band: run the UNMODIFIED quantlib.research.run_experiment twice:
  (a) cost_bps_oneway=2.0  -> read gross OOS IC, NW t, gross-derived breakeven_cost_bps;
  (b) cost_bps_oneway = the band's OWN one-way cost (median Corwin-Schultz half-spread + 1bp pad)
      -> read net_per_period / sharpe_net at the band's realistic own cost.

Gate per band (ALL of): IC>=0.01 AND |t|>=2.0 AND breakeven_cost_bps > own_oneway_cost
AND sharpe_net(@own cost) > 0. Verdict: TRADEABLE NICHE if any band clears all four; else SETTLED NULL.
"""
from __future__ import annotations

import datetime as dt
import json
import os

import numpy as np

from quantlib.research import run_experiment

DATADIR = os.environ.get("DATADIR", "/app/experiments/data")
HORIZON_MINUTES = 1440
CADENCE_MIN = 390

BANDS = ["B1_0001_0500", "B2_0500_1000", "B3_1000_2000", "B4_2000_4000", "B5_4000_6000"]


def load_xy(band: str) -> tuple[list[str], list, list[str], np.ndarray, np.ndarray]:
    data = np.load(os.path.join(DATADIR, f"band_{band}_fwd_1d.npz"), allow_pickle=True)
    names = [str(n) for n in data["names"]]
    ts = [dt.datetime.fromtimestamp(int(t) / 1e9, tz=dt.timezone.utc) for t in data["ts_ns"]]
    symbol_table = [str(s) for s in data["symbols"]]
    symbols = [symbol_table[i] for i in data["sym_idx"]]
    return names, ts, symbols, data["X"].astype(float), data["y"].astype(float)


def main() -> None:
    with open(os.path.join(DATADIR, "band_costs.json")) as fh:
        costs = json.load(fh)

    rows_out = []
    for band in BANDS:
        path = os.path.join(DATADIR, f"band_{band}_fwd_1d.npz")
        if not os.path.exists(path):
            print(f"== {band}: no NPZ (empty band), skipping ==\n")
            continue
        names, ts, symbols, X, y = load_xy(band)
        own_cost = float(costs[band]["oneway_cost_bps"])
        n_days = len({t for t in ts})
        print(f"================ {band} ================")
        print(f"rows={len(y)} days={n_days} symbols={len(set(symbols))} "
              f"own_oneway_cost={own_cost:.2f}bps (CS_half={costs[band]['median_cs_half_bps']:.2f}+1)")

        gross = run_experiment(X, y, ts, symbols=symbols, label="raw", n_folds=5,
                               horizon_minutes=HORIZON_MINUTES, cadence_min=CADENCE_MIN,
                               cost_bps_oneway=2.0)
        netrun = run_experiment(X, y, ts, symbols=symbols, label="raw", n_folds=5,
                                horizon_minutes=HORIZON_MINUTES, cadence_min=CADENCE_MIN,
                                cost_bps_oneway=own_cost)

        ic = gross["mean_ic"]
        nw_t = gross["nw_t"]
        be = gross["breakeven_cost_bps"]
        be_val = be if isinstance(be, (int, float)) and be == be else float("nan")
        sharpe_own = netrun["sharpe_net"]
        net_own = netrun["net_per_period"]
        sharpe_val = sharpe_own if isinstance(sharpe_own, (int, float)) and sharpe_own == sharpe_own else float("nan")

        leg_ic = ic >= 0.01
        leg_t = abs(nw_t) >= 2.0
        leg_be = be_val > own_cost
        leg_sh = sharpe_val > 0.0
        hit = leg_ic and leg_t and leg_be and leg_sh

        print(f"  REAL OOS IC={ic:.5f}  SHUF={gross['canary_ic']:.5f}  "
              f"edge={ic - gross['canary_ic']:+.5f}  NW t={nw_t}")
        print(f"  gross breakeven={be_val:.2f}bps  own_cost={own_cost:.2f}bps  "
              f"=> breakeven>cost: {leg_be}")
        print(f"  net@own_cost/period={net_own}  sharpe_net@own_cost={sharpe_val}  turnover={gross['mean_turnover']}")
        print(f"  GATE: IC>=.01={leg_ic} |t|>=2={leg_t} be>cost={leg_be} sharpe>0={leg_sh} "
              f"==> {'TRADEABLE' if hit else 'MISS'}")
        print()
        rows_out.append((band, len(y), n_days, ic, gross["canary_ic"], nw_t, be_val,
                         own_cost, net_own, sharpe_val, gross["mean_turnover"], hit))

    print("\n================ BAND TABLE ================")
    print(f"{'band':<14}{'rows':>8}{'days':>5}{'IC':>9}{'SHUF':>9}{'NWt':>7}"
          f"{'gross_be':>10}{'own_cost':>10}{'net/per':>11}{'sharpe@own':>12}{'turn':>7}  verdict")
    any_hit = False
    for (band, n, days, ic, shuf, nwt, be_val, own, net_own, sh, turn, hit) in rows_out:
        any_hit = any_hit or hit
        net_str = f"{net_own:.6f}" if isinstance(net_own, (int, float)) and net_own == net_own else "nan"
        print(f"{band:<14}{n:>8}{days:>5}{ic:>9.4f}{shuf:>9.4f}{nwt:>7.2f}"
              f"{be_val:>10.2f}{own:>10.2f}{net_str:>11}{sh:>12.3f}{turn:>7.2f}  "
              f"{'TRADEABLE' if hit else 'MISS'}")
    print(f"\nVERDICT: {'TRADEABLE NICHE (>=1 band clears all 4 legs)' if any_hit else 'SETTLED NULL (no band tradeable at its own cost)'}")


if __name__ == "__main__":
    main()
