"""Attack 1b/1c: survivorship decomposition of B4.

(b) Re-run B4 IC/L-S restricted to (i) names that SURVIVE to panel-end (last raw bar within 5d of
    2026-06-18) vs (ii) names PRESENT-AT-TIME but gone before end. Does the edge shrink/concentrate?
(c) Is the long/short DIRECTION correlated with eventual delisting? If delisted-down names cluster
    in the SHORT leg, the backtest is harvesting an untradeable look-ahead bias.

Uses the EXISTING unmodified harness (quantlib.research.run_experiment / walk_forward_folds + the
verbatim long_short_backtest decile logic reproduced in run_b4_byyear.py). Read-only.
"""
from __future__ import annotations

import datetime as dt
import os

import numpy as np
import polars as pl

from quantlib.research import run_experiment

DATADIR = "/bands"
LASTSEEN = "/out/last_seen.parquet"
PANEL_END = dt.date(2026, 6, 18)
HORIZON_MINUTES = 1440
CADENCE_MIN = 390


def load() -> tuple[list, list[str], np.ndarray, np.ndarray]:
    d = np.load(os.path.join(DATADIR, "band_B4_2000_4000_fwd_1d.npz"), allow_pickle=True)
    ts = [dt.datetime.fromtimestamp(int(t) / 1e9, tz=dt.timezone.utc) for t in d["ts_ns"]]
    tbl = [str(s) for s in d["symbols"]]
    symbols = [tbl[i] for i in d["sym_idx"]]
    return ts, symbols, d["X"].astype(float), d["y"].astype(float)


def main() -> None:
    ls = pl.read_parquet(LASTSEEN)
    last = {r["symbol"]: r["ld"] for r in ls.iter_rows(named=True)}
    ts, symbols, X, y = load()

    # delisted proxy: last raw bar > 5 trading-ish days before panel end
    cut = PANEL_END - dt.timedelta(days=5)
    is_survivor = np.array([last[s] >= cut for s in symbols])
    print(f"B4 rows={len(y)}  survivor_rows={is_survivor.sum()}  "
          f"disappeared_rows={(~is_survivor).sum()} ({100*(~is_survivor).mean():.2f}%)")
    disappeared_syms = sorted({s for s in symbols if last[s] < cut})
    print(f"distinct disappeared symbols in B4: {len(disappeared_syms)} of {len(set(symbols))}")

    # (b) IC on survivors-only vs full
    for label, mask in [("FULL", np.ones(len(y), bool)),
                        ("SURVIVORS_ONLY", is_survivor),
                        ("DISAPPEARED_ONLY", ~is_survivor)]:
        idx = np.where(mask)[0]
        if len(idx) < 1000:
            print(f"  {label}: too few rows ({len(idx)}), skip"); continue
        res = run_experiment(X[idx], y[idx], [ts[i] for i in idx], symbols=[symbols[i] for i in idx],
                             label="raw", n_folds=5, horizon_minutes=HORIZON_MINUTES,
                             cadence_min=CADENCE_MIN, cost_bps_oneway=18.574)
        print(f"  (b) {label:<16} rows={len(idx):>8} IC={res['mean_ic']:.5f} "
              f"NWt={res['nw_t']} sharpe_net={res['sharpe_net']}")

    # (c) direction-vs-delisting: for disappeared names, what is the SIGN of their realized fwd
    #     return in the rows that fall in the LAST 60 calendar days before they vanish?
    #     And: are disappeared names over-represented in the SHORT (bottom-decile predicted) leg?
    #     Use the full-sample mean realized fwd return of disappeared vs survivor rows as the
    #     delisting-drift tell.
    surv_mean = float(np.nanmean(y[is_survivor]))
    dis_mean = float(np.nanmean(y[~is_survivor]))
    print(f"\n  (c) mean realized fwd_1d:  survivors={surv_mean:+.5f}  disappeared={dis_mean:+.5f}")
    # within the last 60d before disappearance, the terminal drift:
    last_rows = []
    for i, s in enumerate(symbols):
        if last[s] < cut and (last[s] - ts[i].date()).days <= 60 and ts[i].date() <= last[s]:
            last_rows.append(y[i])
    if last_rows:
        arr = np.array(last_rows)
        print(f"  (c) terminal-60d realized fwd_1d of disappeared names: n={len(arr)} "
              f"mean={np.nanmean(arr):+.5f} median={np.nanmedian(arr):+.5f} "
              f"frac_negative={np.mean(arr<0):.3f}")


if __name__ == "__main__":
    main()
