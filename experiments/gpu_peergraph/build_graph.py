"""Peer-GRAPH embedding -> sparse weighted similarity edge table W(i,j) (the nightly static lookup).

The relational feature (prereg.md) is a graph-WEIGHTED demean: peerrel_graph = ret_i - Σ_j W(i,j)·ret_j,
W = softmax over top-K embedding-similar peers. This script learns the embedding on the 3090 and emits W as a
sparse parquet (symbol_i, symbol_j, weight) — a FROZEN NIGHTLY per-symbol-pair static lookup (parity-true).

Embedding: a contrastive model over the daily co-movement structure. Per training step, positives = pairs of
symbols that co-moved on a sampled day (same c2c-return decile that day), negatives = random cross-decile
pairs; InfoNCE pulls co-movers together + an autoencoder recon term keeps the embedding faithful. Same family
as repr-2 (which validated OOS), but here the OUTPUT we care about is the neighbourhood graph, not clusters.

W construction: cosine similarity in embedding space -> top-K neighbours per symbol -> softmax(sim/τ) weights
(self excluded, rows sum to 1). Sparse, deterministic.

Run (inside fp-torch-gpu):
  python experiments/gpu_peergraph/build_graph.py --bars <certify300_daily.parquet> \
      --out experiments/gpu_peergraph/out --topk 20 --tau 0.1
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import polars as pl
import torch
import torch.nn as nn
import torch.nn.functional as F

EMBED_DIM = 16
EPOCHS = 400
LR = 1e-3
PAIRS_PER_STEP = 4096
N_DECILES = 10
SEED = 0
MIN_DAYS = 250


def build_return_matrix(bars_path: str) -> tuple[np.ndarray, list[str]]:
    """(symbol x day) standardized c2c log-return matrix for symbols with >= MIN_DAYS history."""
    frame = pl.read_parquet(bars_path).sort(["symbol", "date"])
    frame = frame.with_columns(
        logret=(pl.col("rth_close").log() - pl.col("rth_close").log().shift(1)).over("symbol")
    ).drop_nulls("logret")
    counts = frame.group_by("symbol").len().filter(pl.col("len") >= MIN_DAYS)
    keep = sorted(counts["symbol"].to_list())
    frame = frame.filter(pl.col("symbol").is_in(keep))
    wide = frame.pivot(index="date", on="symbol", values="logret").sort("date")
    symbols = [c for c in wide.columns if c != "date"]
    matrix = wide.select(symbols).to_numpy().T  # symbol x day
    matrix = np.nan_to_num(matrix, nan=0.0).astype(np.float32)
    mean = matrix.mean(axis=1, keepdims=True)
    std = matrix.std(axis=1, keepdims=True)
    std = np.where(std < 1e-8, 1.0, std)
    return (matrix - mean) / std, symbols


def decile_labels(matrix_std: np.ndarray) -> np.ndarray:
    """Per day, each symbol's c2c-return decile (n_symbols x n_days)."""
    n_symbols, n_days = matrix_std.shape
    deciles = np.zeros((n_symbols, n_days), dtype=np.int64)
    for di in range(n_days):
        ranks = np.argsort(np.argsort(matrix_std[:, di]))
        deciles[:, di] = (ranks * N_DECILES // n_symbols).clip(0, N_DECILES - 1)
    return deciles


class GraphEncoder(nn.Module):
    """Encoder MLP over the per-symbol return series -> embedding; decoder reconstructs (faithfulness)."""

    def __init__(self, n_days: int, embed_dim: int) -> None:
        super().__init__()
        self.encoder = nn.Sequential(
            nn.Linear(n_days, 256), nn.GELU(), nn.LayerNorm(256), nn.Linear(256, embed_dim)
        )
        self.decoder = nn.Sequential(nn.Linear(embed_dim, 256), nn.GELU(), nn.Linear(256, n_days))

    def forward(self, x: torch.Tensor) -> tuple[torch.Tensor, torch.Tensor]:
        z = self.encoder(x)
        return z, self.decoder(z)


def sample_triplets(
    deciles: np.ndarray, rng: np.random.Generator, n_pairs: int
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    n_symbols, n_days = deciles.shape
    anchors = np.empty(n_pairs, dtype=np.int64)
    positives = np.empty(n_pairs, dtype=np.int64)
    negatives = np.empty(n_pairs, dtype=np.int64)
    filled = 0
    while filled < n_pairs:
        day = rng.integers(0, n_days)
        col = deciles[:, day]
        anchor = rng.integers(0, n_symbols)
        same = np.flatnonzero(col == col[anchor])
        diff = np.flatnonzero(col != col[anchor])
        if len(same) < 2 or len(diff) < 1:
            continue
        positive = anchor
        while positive == anchor:
            positive = same[rng.integers(0, len(same))]
        anchors[filled] = anchor
        positives[filled] = positive
        negatives[filled] = diff[rng.integers(0, len(diff))]
        filled += 1
    return anchors, positives, negatives


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bars", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--topk", type=int, default=20)
    parser.add_argument("--tau", type=float, default=0.1)
    args = parser.parse_args()
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    torch.manual_seed(SEED)
    rng = np.random.default_rng(SEED)

    matrix, symbols = build_return_matrix(args.bars)
    deciles = decile_labels(matrix)
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print(f"device: {device} | symbols: {len(symbols)} | days: {matrix.shape[1]}", flush=True)

    features = torch.from_numpy(matrix).to(device)
    model = GraphEncoder(matrix.shape[1], EMBED_DIM).to(device)
    optimizer = torch.optim.Adam(model.parameters(), lr=LR)
    for epoch in range(EPOCHS):
        model.train()
        anchors, positives, negatives = sample_triplets(deciles, rng, PAIRS_PER_STEP)
        z, recon = model(features)
        recon_loss = F.mse_loss(recon, features)
        normed = F.normalize(z, dim=1)
        pos = (normed[anchors] * normed[positives]).sum(1) / 0.2
        neg = (normed[anchors] * normed[negatives]).sum(1) / 0.2
        logits = torch.stack([pos, neg], dim=1)
        targets = torch.zeros(logits.shape[0], dtype=torch.long, device=device)
        loss = F.cross_entropy(logits, targets) + 0.5 * recon_loss
        optimizer.zero_grad()
        loss.backward()
        optimizer.step()

    model.eval()
    with torch.no_grad():
        embedding = F.normalize(model.encoder(features), dim=1)
        sims = embedding @ embedding.T  # cosine
        sims.fill_diagonal_(-1e9)  # exclude self
        topk_vals, topk_idx = torch.topk(sims, args.topk, dim=1)
        weights = torch.softmax(topk_vals / args.tau, dim=1)  # row-normalized over the K neighbours
    topk_idx = topk_idx.cpu().numpy()
    weights = weights.cpu().numpy()
    embedding_np = embedding.cpu().numpy()

    rows_i: list[str] = []
    rows_j: list[str] = []
    rows_w: list[float] = []
    for i in range(len(symbols)):
        for k in range(args.topk):
            rows_i.append(symbols[i])
            rows_j.append(symbols[topk_idx[i, k]])
            rows_w.append(float(weights[i, k]))
    edge_table = pl.DataFrame({"symbol_i": rows_i, "symbol_j": rows_j, "weight": rows_w})
    edge_table.write_parquet(out_dir / "graph_weights.parquet")
    np.savez_compressed(
        out_dir / "graph_embedding.npz",
        embedding=embedding_np.astype(np.float32),
        symbols=np.array(symbols),
        topk_idx=topk_idx.astype(np.int32),
        weights=weights.astype(np.float32),
    )
    summary = {
        "n_symbols": len(symbols),
        "n_days": int(matrix.shape[1]),
        "embed_dim": EMBED_DIM,
        "topk": args.topk,
        "tau": args.tau,
        "n_edges": edge_table.height,
    }
    print(json.dumps(summary, indent=2))
    print(f"wrote {out_dir / 'graph_weights.parquet'} ({edge_table.height} edges)")


if __name__ == "__main__":
    main()
