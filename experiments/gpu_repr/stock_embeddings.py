"""D4 — Stock embeddings from REAL daily bars (word2vec-for-stocks via co-movement factorization).

Input: the REAL certify300 daily panel (5,223 symbols x ~18mo of RTH open/close + dollar_vol). We build a
per-symbol behavioral embedding from the CORRELATION structure of daily returns, then:
  (a) INSIGHT: discover data-driven peer groups; quantify how well the embedding clusters recover
      co-movement structure; surface tight behavioral neighbours and the leading eigen-portfolios (market /
      style factors that emerge unsupervised).
  (b) FEATURE CANDIDATES: each symbol's static embedding coords (slow-moving, recomputed nightly ->
      trivially parity-true) and peer-relative return (return minus behavioral-peer-centroid return).

This is REAL data, so structure claims here are honest (unlike the synthetic VAE which only proves the
harness recovers PLANTED structure). Rigor: the embedding is fit on a TRAIN time window and the
co-movement-recovery metric is evaluated on a HELD-OUT later window (does behavioral structure persist
out-of-sample, or is it period-specific?).

Method: truncated SVD of the symbol x day standardized-return matrix -> per-symbol left singular vectors
weighted by singular values = embedding. (Equivalent to PCA over symbols; the top components are the
market + dominant style factors. Robust, deterministic, no GPU needed, parity-trivial to refresh nightly.)

Run: /home/ben/quant-fp/experiments/dl_research/.venv/bin/python experiments/gpu_repr/stock_embeddings.py \
        --bars /home/ben/quant-fp/experiments/2026-06-16-w11-overnight-beta/certify300_daily.parquet
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import polars as pl
from sklearn.cluster import KMeans
from sklearn.metrics import silhouette_score

HERE = Path(__file__).resolve().parent
OUT_DIR = HERE / "out"
MIN_DAYS = 250  # require ~1y of history so the correlation estimate is stable


def build_return_matrix(bars_path: str, min_days: int) -> tuple[np.ndarray, list[str], np.ndarray]:
    """Return (symbol x day) daily log-return matrix for symbols with >= min_days of data.

    Uses close-to-close RTH returns. Days are the union of trading dates; symbols missing a day get 0
    (treated as no-info that day, not a real 0 move) AFTER we restrict to liquid, well-covered names.
    """
    frame = pl.read_parquet(bars_path).sort(["symbol", "date"])
    frame = frame.with_columns(
        (pl.col("rth_close").log() - pl.col("rth_close").log().shift(1)).over("symbol").alias("logret")
    ).drop_nulls("logret")

    counts = frame.group_by("symbol").len().filter(pl.col("len") >= min_days)
    keep_symbols = counts["symbol"].to_list()
    frame = frame.filter(pl.col("symbol").is_in(keep_symbols))

    wide = frame.pivot(index="date", on="symbol", values="logret").sort("date")
    dates = wide["date"].to_numpy()
    symbols = [col for col in wide.columns if col != "date"]
    matrix = wide.select(symbols).to_numpy().T  # symbol x day
    matrix = np.nan_to_num(matrix, nan=0.0)
    return matrix.astype(np.float32), symbols, dates


def standardize_rows(matrix: np.ndarray) -> np.ndarray:
    """Per-symbol z-score across days so the embedding captures co-MOVEMENT, not raw volatility level."""
    mean = matrix.mean(axis=1, keepdims=True)
    std = matrix.std(axis=1, keepdims=True)
    std = np.where(std < 1e-8, 1.0, std)
    return (matrix - mean) / std


def svd_embedding(matrix_std: np.ndarray, dim: int) -> tuple[np.ndarray, np.ndarray]:
    """Top-`dim` SVD of the standardized symbol x day matrix -> per-symbol embedding (U*S).

    Returns (embedding [n_symbols x dim], singular_values [dim]).
    """
    left, singular, _ = np.linalg.svd(matrix_std, full_matrices=False)
    embedding = left[:, :dim] * singular[:dim]
    return embedding.astype(np.float32), singular[:dim]


def cohesion_metric(matrix_std: np.ndarray, labels: np.ndarray) -> float:
    """Mean within-cluster pairwise return-correlation minus mean cross-cluster correlation.

    Positive => discovered clusters genuinely co-move more internally than across. The honest test of
    whether peer groups are real. Computed on whatever return window is passed (use the HELD-OUT window).
    """
    corr = np.corrcoef(matrix_std)
    n = len(labels)
    same = labels[:, None] == labels[None, :]
    off_diag = ~np.eye(n, dtype=bool)
    within = corr[same & off_diag]
    across = corr[~same & off_diag]
    return float(within.mean() - across.mean())


def nearest_neighbours(embedding: np.ndarray, symbols: list[str], k: int = 5) -> dict[str, list[str]]:
    """Top-k behavioral neighbours per symbol (cosine in embedding space), for a few illustrative names."""
    norm = embedding / (np.linalg.norm(embedding, axis=1, keepdims=True) + 1e-9)
    sims = norm @ norm.T
    np.fill_diagonal(sims, -np.inf)
    examples = ["AAPL", "JPM", "XOM", "NVDA", "PG", "JNJ", "TSLA", "WMT"]
    result: dict[str, list[str]] = {}
    sym_to_idx = {sym: idx for idx, sym in enumerate(symbols)}
    for name in examples:
        if name in sym_to_idx:
            top = np.argsort(sims[sym_to_idx[name]])[::-1][:k]
            result[name] = [symbols[j] for j in top]
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bars", required=True)
    parser.add_argument("--dim", type=int, default=16)
    parser.add_argument("--clusters", type=int, default=11)  # compare to 11 GICS sectors
    parser.add_argument("--min-days", type=int, default=MIN_DAYS)
    args = parser.parse_args()
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    matrix, symbols, dates = build_return_matrix(args.bars, args.min_days)
    n_days = matrix.shape[1]
    split = int(n_days * 0.7)
    train_std = standardize_rows(matrix[:, :split])
    test_std = standardize_rows(matrix[:, split:])

    embedding, singular = svd_embedding(train_std, args.dim)
    total_var = float((standardize_rows(matrix[:, :split]) ** 2).sum())
    explained = [round(float(s**2 / total_var), 4) for s in singular[:8]]

    kmeans = KMeans(n_clusters=args.clusters, random_state=0, n_init=10)
    labels = kmeans.fit_predict(embedding)

    cohesion_train = cohesion_metric(train_std, labels)
    cohesion_test = cohesion_metric(test_std, labels)  # OOS: do these peer groups persist?
    sil = float(silhouette_score(embedding, labels))

    rng = np.random.default_rng(0)
    random_labels = rng.integers(0, args.clusters, size=len(symbols))
    cohesion_random_test = cohesion_metric(test_std, random_labels)

    neighbours = nearest_neighbours(embedding, symbols, k=6)

    cluster_sizes = np.bincount(labels, minlength=args.clusters).tolist()

    result = {
        "data": {
            "bars": args.bars,
            "n_symbols": len(symbols),
            "n_days_total": int(n_days),
            "train_days": int(split),
            "test_days": int(n_days - split),
            "min_days_filter": args.min_days,
        },
        "embedding": {"dim": args.dim, "top8_explained_var_ratio": explained},
        "clusters": {
            "k": args.clusters,
            "sizes": cluster_sizes,
            "silhouette": round(sil, 4),
        },
        "cohesion_within_minus_across_corr": {
            "train": round(cohesion_train, 4),
            "heldout_time": round(cohesion_test, 4),
            "random_labels_heldout_time": round(cohesion_random_test, 4),
            "structure_is_real_oos": bool(cohesion_test > 5 * max(cohesion_random_test, 1e-4)),
        },
        "example_behavioral_neighbours": neighbours,
    }
    out_path = OUT_DIR / "stock_embeddings_result.json"
    out_path.write_text(json.dumps(result, indent=2))
    np.savez(
        OUT_DIR / "stock_embeddings.npz",
        embedding=embedding,
        symbols=np.array(symbols),
        labels=labels,
        singular=singular,
    )
    print(json.dumps(result, indent=2))
    print(f"\nwrote {out_path}")


if __name__ == "__main__":
    main()
