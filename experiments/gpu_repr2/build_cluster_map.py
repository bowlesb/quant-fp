"""Build the shippable AE behavioral cluster map (drop-in replacement for behavioral_clusters_v1).

The contrastive AE embedding beats the #76 c2c-SVD baseline on held-out-time co-movement cohesion
(0.131 +/- 0.002 vs 0.114 +/- 0.004; AE wins all 5 seeds; ARI 0.25 + canonical corrs decaying to 0.31 ->
NOT a rotation of the linear baseline). `peer_relative` already reads a `symbol -> cluster_id` map fit from
#76's SVD embedding to peer-demean intraday returns; a higher-cohesion cluster map = a cleaner shared
component = a cleaner idiosyncratic residual, so the AE clusters are a direct, on-objective UPGRADE to that
existing feature, NOT a new redundant feature.

This script fits the AE on the FULL daily history (deterministic, seed=0) and writes
`behavioral_clusters_v2.parquet` (schema identical to v1: `symbol, cluster_id`) plus the AE embedding coords
for provenance. The fit is a deterministic function of settled daily bars -> a nightly static per-symbol
lookup, parity-true by construction (same lookup table in stream and backfill, no intraday state) — exactly
the v1 refresh pattern. Trains on the 3090 (fp-torch-gpu).

Run (inside fp-torch-gpu):
  python experiments/gpu_repr2/build_cluster_map.py --profiles experiments/gpu_repr2/out/profiles.npz \
      --out experiments/gpu_repr2/out
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import polars as pl
import torch
from sklearn.cluster import KMeans

sys.path.insert(0, str(Path(__file__).resolve().parent))

from train_embedding import (  # noqa: E402
    N_CLUSTERS,
    SEED,
    build_decile_labels,
    per_symbol_summary,
)
from robustness import train_ae_once  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profiles", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    out_dir = Path(args.out)

    data = np.load(args.profiles, allow_pickle=True)
    panel = data["panel"]  # n_symbols x n_days x n_channels
    symbols = [str(sym) for sym in data["symbols"]]
    channels = [str(channel) for channel in data["channels"]]
    c2c_idx = channels.index("c2c")

    # Fit on the FULL history for the production map (no held-out split — this is the deployed lookup,
    # the OOS validation that justified it lives in robustness_result.json).
    features, feature_names = per_symbol_summary(panel, channels)
    deciles = build_decile_labels(panel[:, :, c2c_idx])
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print(f"device: {device} | symbols: {len(symbols)} | features: {features.shape[1]}")

    embedding = train_ae_once(features, deciles, device, SEED)
    labels = KMeans(n_clusters=N_CLUSTERS, random_state=SEED, n_init=10).fit_predict(embedding)

    cluster_map = pl.DataFrame({"symbol": symbols, "cluster_id": labels.astype(np.int64)}).sort("symbol")
    map_path = out_dir / "behavioral_clusters_v2.parquet"
    cluster_map.write_parquet(map_path)
    np.savez_compressed(
        out_dir / "ae_cluster_map.npz",
        embedding=embedding.astype(np.float32),
        labels=labels.astype(np.int64),
        symbols=np.array(symbols),
        feature_names=np.array(feature_names),
    )
    sizes = sorted(np.bincount(labels, minlength=N_CLUSTERS).tolist())
    print(f"cluster sizes: {sizes}")
    print(f"wrote {map_path} ({cluster_map.height} symbols, {N_CLUSTERS} clusters)")


if __name__ == "__main__":
    main()
