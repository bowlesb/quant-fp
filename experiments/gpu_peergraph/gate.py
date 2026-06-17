"""Peer-GRAPH forward-IC gate: graph-weighted demean vs uniform-cluster demean vs shuffle (prereg.md).

Computes the OOS rank-IC (vs TRADEABLE next-minute forward returns, walk-forward TEST) of three peer-relative
arms, paired on identical (symbol, minute) observations:
  UNIFORM = ret_w - mean(ret_w over the symbol's v1 hard cluster)        [the production baseline]
  GRAPH   = ret_w - Σ_j W(i,j)·ret_w(j)  over top-K graph neighbours      [the new relational feature]
  SHUFFLE = GRAPH with the symbol->embedding-row map permuted             [canary: kills the structure]

Ship criterion (prereg): GRAPH |IC| > UNIFORM |IC| AND > SHUFFLE |IC| in >= 3/4 cells, directionally
consistent. Else decisive negative.

This reuses the C1-v2 gate harness conventions (RTH ET window, tradeable entry = close.shift(-1), Spearman IC,
top-N liquid sample). The graph-weighted peer mean is computed per (date, minute) as a sparse mat-vec over the
minute's return cross-section.

Run (inside fp-torch-gpu or fp-ml, with -v fp_store_real:/store:ro):
  python experiments/gpu_peergraph/gate.py --graph experiments/gpu_peergraph/out/graph_weights.parquet \
      --v1 quantlib/features/data/behavioral_clusters_v1.parquet --out experiments/gpu_peergraph/out
"""

from __future__ import annotations

import argparse
import glob
import json
import os
from pathlib import Path

import numpy as np
import polars as pl
from scipy.stats import spearmanr

RET_WINDOWS = (5, 30)
FWD_HORIZONS = (5, 30)
RTH_LO, RTH_HI = 570, 960
N_SYMBOLS = 1000
MAX_DAYS = 60


def load_panel(bars: str) -> pl.DataFrame:
    probe_day = "2026-06-16"
    probe = []
    for path in glob.glob(f"{bars}/symbol=*/date={probe_day}/data.parquet"):
        sym = path.split("symbol=")[1].split("/")[0]
        try:
            frame = pl.read_parquet(path, columns=["close", "volume"])
        except (OSError, pl.exceptions.PolarsError):
            continue
        probe.append((sym, float((frame["close"] * frame["volume"]).sum())))
    probe.sort(key=lambda kv: -kv[1])
    syms = [s for s, _ in probe[:N_SYMBOLS]]

    frames = []
    for sym in syms:
        for path in sorted(glob.glob(f"{bars}/symbol={sym}/date=*/data.parquet"))[-MAX_DAYS:]:
            date = os.path.basename(os.path.dirname(path)).split("=")[1]
            try:
                frame = pl.read_parquet(path, columns=["ts", "close"])
            except (OSError, pl.exceptions.PolarsError):
                continue
            if frame.height == 0:
                continue
            et = frame["ts"].dt.convert_time_zone("America/New_York")
            etm = et.dt.hour().cast(pl.Int32) * 60 + et.dt.minute().cast(pl.Int32)
            frame = frame.filter((etm >= RTH_LO) & (etm < RTH_HI)).with_columns(
                pl.lit(sym).alias("symbol"), pl.lit(date).alias("date"), pl.col("ts").alias("minute")
            )
            if frame.height:
                frames.append(frame.select(["symbol", "date", "minute", "close"]))
    return pl.concat(frames).sort(["symbol", "date", "minute"])


def add_returns_and_forward(panel: pl.DataFrame) -> pl.DataFrame:
    exprs = []
    for w in RET_WINDOWS:
        exprs.append(
            (pl.col("close") / pl.col("close").shift(w).over(["symbol", "date"]) - 1.0).alias(f"_ret{w}")
        )
    for h in FWD_HORIZONS:
        entry = pl.col("close").shift(-1).over(["symbol", "date"])
        future = pl.col("close").shift(-1 - h).over(["symbol", "date"])
        exprs.append((future / entry - 1.0).alias(f"fwd{h}"))
    return panel.with_columns(exprs)


def uniform_demean(panel: pl.DataFrame, clusters: pl.DataFrame) -> pl.DataFrame:
    out = panel.join(clusters, on="symbol", how="left")
    exprs = [
        pl.when(pl.col("cluster_id").is_not_null())
        .then(pl.col(f"_ret{w}") - pl.col(f"_ret{w}").mean().over(["cluster_id", "minute"]))
        .otherwise(None)
        .alias(f"uniform{w}")
        for w in RET_WINDOWS
    ]
    return out.with_columns(exprs).select(["symbol", "date", "minute", *[f"uniform{w}" for w in RET_WINDOWS]])


def graph_demean(
    panel: pl.DataFrame, symbols: list[str], topk_idx: np.ndarray, weights: np.ndarray, suffix: str
) -> pl.DataFrame:
    """peerrel_graph_w(i) = ret_w(i) - Σ_j W(i,j)·ret_w(j), per (date, minute) sparse mat-vec."""
    sym_to_row = {sym: idx for idx, sym in enumerate(symbols)}
    panel = panel.with_columns(pl.col("symbol").replace_strict(sym_to_row, default=-1).alias("_row"))
    results: list[pl.DataFrame] = []
    for (date, minute), sub in panel.group_by(["date", "minute"], maintain_order=True):
        rows = sub["_row"].to_numpy()
        valid = rows >= 0
        present = np.zeros(len(symbols), dtype=bool)
        present[rows[valid]] = True
        cols = {}
        for w in RET_WINDOWS:
            ret_full = np.full(len(symbols), np.nan, dtype=np.float64)
            ret_full[rows[valid]] = sub[f"_ret{w}"].to_numpy()[valid]
            neigh_ret = ret_full[topk_idx]  # n_symbols x K
            neigh_present = present[topk_idx]
            wt = weights * neigh_present  # zero out absent neighbours
            wsum = wt.sum(axis=1, keepdims=True)
            safe = np.where(neigh_present, np.nan_to_num(neigh_ret), 0.0)
            peer_mean = (wt * safe).sum(axis=1) / np.where(wsum[:, 0] > 1e-9, wsum[:, 0], np.nan)
            demeaned = ret_full - peer_mean
            cols[f"graph{w}_{suffix}"] = demeaned[rows[valid]]
        out = sub.filter(pl.Series(valid)).select(["symbol", "date", "minute"])
        for w in RET_WINDOWS:
            out = out.with_columns(pl.Series(f"graph{w}_{suffix}", cols[f"graph{w}_{suffix}"]))
        results.append(out)
    return pl.concat(results)


def ic(a: np.ndarray, b: np.ndarray) -> tuple[float, int]:
    mask = np.isfinite(a) & np.isfinite(b)
    if int(mask.sum()) < 500:
        return float("nan"), int(mask.sum())
    return float(spearmanr(a[mask], b[mask]).correlation), int(mask.sum())


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--graph", required=True)
    parser.add_argument("--v1", required=True)
    parser.add_argument("--bars", default="/store/raw/bars")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    np.seterr(all="ignore")

    data = np.load(Path(args.graph).parent / "graph_embedding.npz", allow_pickle=True)
    symbols = [str(s) for s in data["symbols"]]
    topk_idx = data["topk_idx"].astype(np.int64)
    weights = data["weights"].astype(np.float64)
    rng = np.random.default_rng(0)
    perm = rng.permutation(len(symbols))  # shuffle canary: permute the symbol->row map
    shuf_idx = perm[topk_idx]  # neighbours now point to permuted rows (structure destroyed)

    print("loading panel...", flush=True)
    panel = add_returns_and_forward(load_panel(args.bars))
    print(f"panel: {panel['symbol'].n_unique()} syms, {panel.height:,} rows", flush=True)

    v1 = pl.read_parquet(args.v1).select(
        pl.col("symbol").cast(pl.String), pl.col("cluster_id").cast(pl.Int32)
    )
    uni = uniform_demean(panel, v1)
    grp = graph_demean(panel, symbols, topk_idx, weights, "graph")
    shf = graph_demean(panel, symbols, shuf_idx, weights, "shuf")

    fwd_cols = [f"fwd{h}" for h in FWD_HORIZONS]
    merged = (
        panel.select(["symbol", "date", "minute", *fwd_cols])
        .join(uni, on=["symbol", "date", "minute"])
        .join(grp, on=["symbol", "date", "minute"])
        .join(shf, on=["symbol", "date", "minute"])
    )
    dates = sorted(merged["date"].unique().to_list())
    cut = dates[int(len(dates) * 0.70)]
    test = merged.filter(pl.col("date") >= cut)
    print(f"walk-forward TEST >= {cut} ({test.height:,} rows, {test['date'].n_unique()} days)", flush=True)

    cells = []
    graph_beats_uniform = 0
    graph_beats_shuffle = 0
    for w in RET_WINDOWS:
        for h in FWD_HORIZONS:
            fwd = test[f"fwd{h}"].to_numpy()
            ic_uni, n = ic(test[f"uniform{w}"].to_numpy(), fwd)
            ic_grp, _ = ic(test[f"graph{w}_graph"].to_numpy(), fwd)
            ic_shf, _ = ic(test[f"graph{w}_shuf"].to_numpy(), fwd)
            beats_uni = abs(ic_grp) > abs(ic_uni)
            beats_shf = abs(ic_grp) > abs(ic_shf)
            graph_beats_uniform += int(beats_uni)
            graph_beats_shuffle += int(beats_shf)
            cells.append(
                {
                    "ret_window": w,
                    "fwd_horizon": h,
                    "n": n,
                    "uniform_ic": round(ic_uni, 5),
                    "graph_ic": round(ic_grp, 5),
                    "shuffle_ic": round(ic_shf, 5),
                    "graph_beats_uniform": bool(beats_uni),
                    "graph_beats_shuffle": bool(beats_shf),
                }
            )
            print(
                f"  ret{w:>2}m fwd{h:>2}m | uniform {ic_uni:+.5f} | graph {ic_grp:+.5f} | "
                f"shuffle {ic_shf:+.5f} | beats_uni={beats_uni} beats_shuf={beats_shf} (n={n:,})",
                flush=True,
            )

    ship = graph_beats_uniform >= 3 and graph_beats_shuffle >= 3
    summary = {
        "test_cut": str(cut),
        "cells": cells,
        "graph_beats_uniform_cells": f"{graph_beats_uniform}/4",
        "graph_beats_shuffle_cells": f"{graph_beats_shuffle}/4",
        "ship_graph_feature": bool(ship),
        "verdict": (
            "SHIP — graph-weighted demean adds forward-IC beyond the demean mechanics"
            if ship
            else "NO SHIP — graph weighting adds no forward-IC beyond uniform/shuffle (decisive negative)"
        ),
    }
    (Path(args.out) / "gate_result.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary["verdict"], indent=2), flush=True)
    print(f"wrote {Path(args.out) / 'gate_result.json'}", flush=True)


if __name__ == "__main__":
    main()
