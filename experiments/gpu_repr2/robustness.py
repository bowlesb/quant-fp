"""Robustness + non-redundancy checks for the non-linear behavioral embedding.

The single-seed run showed the contrastive AE (0.1293) beats the #76 c2c-SVD baseline (0.1188) and the
linear multi-channel PCA (0.0913) on held-out-time cohesion. A 9%-relative win could be seed noise. Before
claiming an edge we check:

  1. SEED STABILITY: re-train the AE and re-cluster the baseline across N seeds; report mean +/- std of
     held-out cohesion for each arm. The win counts only if the AE's distribution clears the baseline's.
  2. NON-REDUNDANCY vs #76: how much new information does the AE embedding carry beyond the linear baseline
     it must justify itself against? We measure (a) adjusted Rand index between AE clusters and baseline
     clusters (high ARI = same partition = redundant), and (b) the canonical correlations between the AE
     embedding and the baseline SVD embedding (all ~1.0 = the AE is a rotation of the linear space = no new
     structure). A feature candidate only ships if it is BOTH better AND not a re-encoding of #76.

This re-uses the trained artifacts/protocol from train_embedding.py; it re-trains the AE per seed (the GPU
work) and recomputes linear arms deterministically.

Run (inside fp-torch-gpu):
  python experiments/gpu_repr2/robustness.py --profiles experiments/gpu_repr2/out/profiles.npz \
      --out experiments/gpu_repr2/out --seeds 5
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import torch
from sklearn.cluster import KMeans
from sklearn.cross_decomposition import CCA
from sklearn.decomposition import PCA
from sklearn.metrics import adjusted_rand_score

from experiments.gpu_repr2.train_embedding import (
    EMBED_DIM,
    N_CLUSTERS,
    TRAIN_FRAC,
    ContrastiveAutoencoder,
    build_decile_labels,
    cohesion_metric,
    heldout_corr,
    linear_svd_c2c,
    per_symbol_summary,
)


def train_ae_once(features: np.ndarray, deciles: np.ndarray, device: torch.device, seed: int) -> np.ndarray:
    """Train the contrastive AE with a given seed; return the embedding. Mirrors train_embedding."""
    import torch.nn.functional as F

    torch.manual_seed(seed)
    rng = np.random.default_rng(seed)
    features_tensor = torch.from_numpy(features).to(device)
    model = ContrastiveAutoencoder(features.shape[1], EMBED_DIM).to(device)
    optimizer = torch.optim.Adam(model.parameters(), lr=1e-3)
    n_symbols, n_days = deciles.shape
    for _ in range(400):
        model.train()
        anchors = np.empty(4096, dtype=np.int64)
        positives = np.empty(4096, dtype=np.int64)
        negatives = np.empty(4096, dtype=np.int64)
        filled = 0
        while filled < 4096:
            day = rng.integers(0, n_days)
            day_deciles = deciles[:, day]
            anchor = rng.integers(0, n_symbols)
            same = np.flatnonzero(day_deciles == day_deciles[anchor])
            diff = np.flatnonzero(day_deciles != day_deciles[anchor])
            if len(same) < 2 or len(diff) < 1:
                continue
            positive = anchor
            while positive == anchor:
                positive = same[rng.integers(0, len(same))]
            anchors[filled] = anchor
            positives[filled] = positive
            negatives[filled] = diff[rng.integers(0, len(diff))]
            filled += 1
        embedding, reconstruction = model(features_tensor)
        recon_loss = F.mse_loss(reconstruction, features_tensor)
        normed = F.normalize(embedding, dim=1)
        positive_sim = (normed[anchors] * normed[positives]).sum(dim=1) / 0.2
        negative_sim = (normed[anchors] * normed[negatives]).sum(dim=1) / 0.2
        logits = torch.stack([positive_sim, negative_sim], dim=1)
        targets = torch.zeros(logits.shape[0], dtype=torch.long, device=device)
        loss = F.cross_entropy(logits, targets) + 0.5 * recon_loss
        optimizer.zero_grad()
        loss.backward()
        optimizer.step()
    model.eval()
    with torch.no_grad():
        return model.encoder(features_tensor).cpu().numpy().astype(np.float32)


def cohesion_for_embedding(embedding: np.ndarray, test_corr: np.ndarray, seed: int) -> tuple[float, np.ndarray]:
    kmeans = KMeans(n_clusters=N_CLUSTERS, random_state=seed, n_init=10)
    labels = kmeans.fit_predict(embedding)
    return cohesion_metric(test_corr, labels), labels


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profiles", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--seeds", type=int, default=5)
    args = parser.parse_args()
    out_dir = Path(args.out)

    data = np.load(args.profiles, allow_pickle=True)
    panel = data["panel"]
    channels = [str(channel) for channel in data["channels"]]
    c2c_idx = channels.index("c2c")
    n_days = panel.shape[1]
    split = int(n_days * TRAIN_FRAC)
    panel_train = panel[:, :split, :]
    panel_train_c2c = panel[:, :split, c2c_idx]
    test_corr = heldout_corr(panel[:, split:, c2c_idx])

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    features, _ = per_symbol_summary(panel_train, channels)
    deciles = build_decile_labels(panel_train_c2c)

    embed_baseline = linear_svd_c2c(panel_train_c2c, EMBED_DIM)
    embed_pca = PCA(n_components=EMBED_DIM, random_state=0).fit_transform(features).astype(np.float32)

    baseline_cohesions: list[float] = []
    pca_cohesions: list[float] = []
    ae_cohesions: list[float] = []
    ari_ae_vs_baseline: list[float] = []
    last_ae_embed = None
    baseline_labels_ref = None
    for seed in range(args.seeds):
        baseline_coh, baseline_labels = cohesion_for_embedding(embed_baseline, test_corr, seed)
        pca_coh, _ = cohesion_for_embedding(embed_pca, test_corr, seed)
        ae_embed = train_ae_once(features, deciles, device, seed)
        ae_coh, ae_labels = cohesion_for_embedding(ae_embed, test_corr, seed)
        baseline_cohesions.append(baseline_coh)
        pca_cohesions.append(pca_coh)
        ae_cohesions.append(ae_coh)
        ari_ae_vs_baseline.append(float(adjusted_rand_score(baseline_labels, ae_labels)))
        last_ae_embed = ae_embed
        baseline_labels_ref = baseline_labels
        print(f"seed {seed}: baseline={baseline_coh:.4f} pca={pca_coh:.4f} ae={ae_coh:.4f} "
              f"ari(ae,base)={ari_ae_vs_baseline[-1]:.3f}")

    # Canonical correlation: is the AE embedding just a rotation of the linear baseline space?
    cca = CCA(n_components=min(8, EMBED_DIM))
    ae_c, base_c = cca.fit_transform(last_ae_embed, embed_baseline)
    canonical_corrs = [
        round(float(np.corrcoef(ae_c[:, k], base_c[:, k])[0, 1]), 3) for k in range(ae_c.shape[1])
    ]

    summary = {
        "seeds": args.seeds,
        "heldout_cohesion": {
            "baseline_c2c_svd": {
                "mean": round(float(np.mean(baseline_cohesions)), 4),
                "std": round(float(np.std(baseline_cohesions)), 4),
                "values": [round(value, 4) for value in baseline_cohesions],
            },
            "multichannel_pca": {
                "mean": round(float(np.mean(pca_cohesions)), 4),
                "std": round(float(np.std(pca_cohesions)), 4),
                "values": [round(value, 4) for value in pca_cohesions],
            },
            "contrastive_ae": {
                "mean": round(float(np.mean(ae_cohesions)), 4),
                "std": round(float(np.std(ae_cohesions)), 4),
                "values": [round(value, 4) for value in ae_cohesions],
            },
        },
        "ae_minus_baseline": {
            "mean": round(float(np.mean(ae_cohesions) - np.mean(baseline_cohesions)), 4),
            "min_over_seeds": round(float(np.min(np.array(ae_cohesions) - np.array(baseline_cohesions))), 4),
            "ae_wins_all_seeds": bool(np.all(np.array(ae_cohesions) > np.array(baseline_cohesions))),
        },
        "non_redundancy": {
            "adjusted_rand_index_ae_vs_baseline_mean": round(float(np.mean(ari_ae_vs_baseline)), 3),
            "canonical_corrs_ae_vs_baseline": canonical_corrs,
            "interpretation": "ARI<<1 and canonical corrs<1 => AE carries structure beyond the linear baseline",
        },
    }
    (out_dir / "robustness_result.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))
    print(f"\nwrote {out_dir / 'robustness_result.json'}")


if __name__ == "__main__":
    main()
