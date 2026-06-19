"""Convert the overnight parquet panel to per-horizon .npz (X, y, ts_ns, sym_idx, names, symbols)
so the lightgbm runner needs only numpy. Sibling of the trusted-baseline panel_to_npz.py, but the
overnight horizons are DAY-based (fwd_1d/2d/3d) and the panel carries an extra `date` column."""
from __future__ import annotations

import os

import numpy as np
import polars as pl

PANEL = os.environ.get("PANEL", "/app/experiments/data/overnight_panel.parquet")
OUTDIR = os.environ.get("OUTDIR", "/app/experiments/data")
NPZ_PREFIX = os.environ.get("NPZ_PREFIX", "overnight_panel")


def main() -> None:
    frame = pl.read_parquet(PANEL)
    horizons = sorted(
        (c for c in frame.columns if c.startswith("fwd_") and c.endswith("d")),
        key=lambda col: int(col[len("fwd_"):-1]),
    )
    non_feature = {"symbol", "minute", "date", *horizons}
    feature_cols = [c for c in frame.columns if c not in non_feature]
    for horizon in horizons:
        sub = frame.filter(pl.col(horizon).is_not_null()).sort("minute")
        ts_ns = sub["minute"].dt.timestamp("ns").to_numpy().astype(np.int64)
        symbols = sub["symbol"].to_list()
        uniq = sorted(set(symbols))
        sym_to_idx = {s: i for i, s in enumerate(uniq)}
        sym_idx = np.array([sym_to_idx[s] for s in symbols], dtype=np.int64)
        X = sub.select(feature_cols).to_numpy().astype(float)
        y = sub[horizon].to_numpy().astype(float)
        out = os.path.join(OUTDIR, f"{NPZ_PREFIX}_{horizon}.npz")
        np.savez(out, X=X, y=y, ts_ns=ts_ns, sym_idx=sym_idx,
                 names=np.array(feature_cols), symbols=np.array(uniq))
        print(f"{horizon}: wrote {out}  X={X.shape} y={y.shape} "
              f"timestamps={len(set(ts_ns.tolist()))} symbols={len(uniq)}")


if __name__ == "__main__":
    main()
