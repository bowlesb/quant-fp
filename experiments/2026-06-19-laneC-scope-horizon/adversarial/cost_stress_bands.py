"""Attack 2: harvestability. For ALL bands, run the unmodified harness at escalating one-way cost
(1x/1.5x/2x/2.5x the band's OWN CS-derived cost) and report net Sharpe — at what multiple does each
band flip to a loss? Specifically: is there ANY net-positive edge in the LIQUID bands (B1/B2) after
realistic cost, since the headline edge lives in the UNTRADEABLE illiquid tail (B4/B5)?
"""
from __future__ import annotations

import datetime as dt
import json
import os

import numpy as np

from quantlib.research import run_experiment

DATADIR = "/bands"
HORIZON_MINUTES = 1440
CADENCE_MIN = 390
BANDS = ["B1_0001_0500", "B2_0500_1000", "B3_1000_2000", "B4_2000_4000", "B5_4000_6000"]


def load(band: str) -> tuple[list, list[str], np.ndarray, np.ndarray]:
    d = np.load(os.path.join(DATADIR, f"band_{band}_fwd_1d.npz"), allow_pickle=True)
    ts = [dt.datetime.fromtimestamp(int(t) / 1e9, tz=dt.timezone.utc) for t in d["ts_ns"]]
    tbl = [str(s) for s in d["symbols"]]
    symbols = [tbl[i] for i in d["sym_idx"]]
    return ts, symbols, d["X"].astype(float), d["y"].astype(float)


def main() -> None:
    costs = json.load(open(os.path.join(DATADIR, "band_costs_deep.json")))
    print(f"{'band':<14}{'own_bps':>8}{'mult':>6}{'cost_bps':>10}{'net/per':>12}{'sharpe':>9}")
    for band in BANDS:
        ts, symbols, X, y = load(band)
        own = float(costs[band]["oneway_cost_bps"])
        for mult in [1.0, 1.5, 2.0, 2.5]:
            cost = mult * own
            res = run_experiment(X, y, ts, symbols=symbols, label="raw", n_folds=5,
                                 horizon_minutes=HORIZON_MINUTES, cadence_min=CADENCE_MIN,
                                 cost_bps_oneway=cost)
            net = res["net_per_period"]
            sh = res["sharpe_net"]
            net_s = f"{net:.6f}" if isinstance(net, (int, float)) and net == net else "nan"
            sh_s = f"{sh:.3f}" if isinstance(sh, (int, float)) and sh == sh else "nan"
            flag = " <-LOSS" if (isinstance(sh, (int, float)) and sh == sh and sh < 0) else ""
            print(f"{band:<14}{own:>8.2f}{mult:>6.1f}{cost:>10.2f}{net_s:>12}{sh_s:>9}{flag}")
        print()


if __name__ == "__main__":
    main()
