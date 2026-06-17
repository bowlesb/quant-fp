"""Non-linear cross-asset behavioral embedding on the RTX 3090 (contrastive autoencoder).

GOAL: beat the #76 LINEAR, SINGLE-CHANNEL (c2c-correlation SVD) embedding on its OWN out-of-sample metric
(held-out-time within-minus-across cluster return correlation; #76 = 0.092 vs 0.0003 random). A deep model
must EARN its complexity (PLAN.md rigor rule) — if it does not beat the linear baseline OOS we ship the
linear one. We report THREE arms honestly so the lift is attributable:

  A. baseline-c2c-SVD     : linear SVD on c2c returns only          -> reproduces #76 (~0.092)
  B. multichannel-PCA     : linear PCA on the 5-channel profile     -> isolates the DATA lift (channels)
  C. multichannel-AE-GPU  : non-linear contrastive autoencoder      -> isolates the NON-LINEARITY lift

Per-symbol input to the encoder = a fixed-length summary of its TRAIN-window multi-channel behavior:
for each of the 5 cross-sectionally-standardized channels we take per-symbol summary stats over train days
(mean, std, downside mean, autocorr-lag1, and the symbol's loading on the top-K daily cross-sectional PCA
components of that channel). This is a deterministic function of settled daily bars -> a nightly static
per-symbol lookup (parity-true) if shipped.

Contrastive supervision: on each TRAIN day, symbols are ranked by that day's c2c move; positives = pairs in
the same return decile that day (they co-moved), negatives = random cross-decile pairs. InfoNCE pulls
co-moving symbols together in embedding space. Plus an autoencoder recon loss on the summary vector so the
embedding stays a faithful compression (not a degenerate collapse). The model NEVER sees held-out-time days.

OOS metric (identical to #76): KMeans(11) on the learned embedding -> cohesion = mean within-cluster minus
mean across-cluster pairwise c2c-return correlation, computed on the HELD-OUT-TIME window only. Random-label
cohesion on the same window is the floor.

Run (inside fp-torch-gpu):
  python experiments/gpu_repr2/train_embedding.py --profiles experiments/gpu_repr2/out/profiles.npz \
      --out experiments/gpu_repr2/out
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from sklearn.cluster import KMeans
from sklearn.decomposition import PCA
from sklearn.metrics import silhouette_score

EMBED_DIM = 16
N_CLUSTERS = 11  # compare to 11 GICS sectors, same as #76
TRAIN_FRAC = 0.7
TOP_PCA_PER_CHANNEL = 4  # daily-cross-section PCA loadings per channel summarize co-movement linearly
N_DECILES = 10
EPOCHS = 400
LR = 1e-3
PAIRS_PER_STEP = 4096
SEED = 0


def per_symbol_summary(panel_train: np.ndarray, channels: list[str]) -> tuple[np.ndarray, list[str]]:
    """Fixed-length per-symbol behavioral summary from the train-window multi-channel panel.

    panel_train: n_symbols x n_train_days x n_channels (cross-sectionally standardized).
    Returns (n_symbols x n_features) and the feature names. All stats are over TRAIN days only.
    """
    n_symbols, _, n_channels = panel_train.shape
    feature_blocks: list[np.ndarray] = []
    names: list[str] = []
    for ci in range(n_channels):
        channel = panel_train[:, :, ci]  # n_symbols x n_train_days
        mean = channel.mean(axis=1)
        std = channel.std(axis=1)
        downside = np.where(channel < 0, channel, 0.0).mean(axis=1)
        shifted = channel[:, 1:]
        lagged = channel[:, :-1]
        centered_a = shifted - shifted.mean(axis=1, keepdims=True)
        centered_b = lagged - lagged.mean(axis=1, keepdims=True)
        denom = (np.linalg.norm(centered_a, axis=1) * np.linalg.norm(centered_b, axis=1)) + 1e-9
        autocorr = (centered_a * centered_b).sum(axis=1) / denom
        for stat_name, stat in [("mean", mean), ("std", std), ("down", downside), ("ac1", autocorr)]:
            feature_blocks.append(stat[:, None])
            names.append(f"{channels[ci]}_{stat_name}")
        pca = PCA(n_components=min(TOP_PCA_PER_CHANNEL, channel.shape[1]), random_state=SEED)
        loadings = pca.fit_transform(channel)
        feature_blocks.append(loadings)
        names.extend([f"{channels[ci]}_pc{component}" for component in range(loadings.shape[1])])
    features = np.concatenate(feature_blocks, axis=1).astype(np.float32)
    mean = features.mean(axis=0, keepdims=True)
    std = features.std(axis=0, keepdims=True)
    std = np.where(std < 1e-8, 1.0, std)
    return (features - mean) / std, names


def cohesion_metric(corr: np.ndarray, labels: np.ndarray) -> float:
    """Mean within-cluster minus mean across-cluster pairwise correlation (off-diagonal). #76's metric."""
    n = len(labels)
    same = labels[:, None] == labels[None, :]
    off_diag = ~np.eye(n, dtype=bool)
    within = corr[same & off_diag]
    across = corr[~same & off_diag]
    return float(within.mean() - across.mean())


def heldout_corr(panel_test_c2c: np.ndarray) -> np.ndarray:
    """Symbol x symbol c2c-return correlation on the held-out-time window (the OOS substrate)."""
    return np.corrcoef(panel_test_c2c)


def evaluate_embedding(embedding: np.ndarray, test_corr: np.ndarray, rng: np.random.Generator) -> dict:
    """KMeans on embedding -> OOS cohesion vs random labels (identical protocol to #76)."""
    kmeans = KMeans(n_clusters=N_CLUSTERS, random_state=SEED, n_init=10)
    labels = kmeans.fit_predict(embedding)
    cohesion = cohesion_metric(test_corr, labels)
    random_labels = rng.integers(0, N_CLUSTERS, size=embedding.shape[0])
    random_cohesion = cohesion_metric(test_corr, random_labels)
    silhouette = float(silhouette_score(embedding, labels))
    return {
        "heldout_cohesion": round(cohesion, 4),
        "random_label_cohesion": round(random_cohesion, 4),
        "silhouette": round(silhouette, 4),
        "ratio_vs_random": round(cohesion / max(random_cohesion, 1e-4), 1),
    }


class ContrastiveAutoencoder(nn.Module):
    """Encoder MLP -> embedding; decoder reconstructs the summary vector. Contrastive head = the embedding."""

    def __init__(self, n_features: int, embed_dim: int) -> None:
        super().__init__()
        self.encoder = nn.Sequential(
            nn.Linear(n_features, 256),
            nn.GELU(),
            nn.LayerNorm(256),
            nn.Linear(256, 128),
            nn.GELU(),
            nn.LayerNorm(128),
            nn.Linear(128, embed_dim),
        )
        self.decoder = nn.Sequential(
            nn.Linear(embed_dim, 128),
            nn.GELU(),
            nn.Linear(128, 256),
            nn.GELU(),
            nn.Linear(256, n_features),
        )

    def forward(self, features: torch.Tensor) -> tuple[torch.Tensor, torch.Tensor]:
        embedding = self.encoder(features)
        reconstruction = self.decoder(embedding)
        return embedding, reconstruction


def build_decile_labels(panel_train_c2c: np.ndarray) -> np.ndarray:
    """Per train day, assign each symbol a c2c-return decile (n_symbols x n_train_days, int in [0, 9])."""
    n_symbols, n_days = panel_train_c2c.shape
    deciles = np.zeros((n_symbols, n_days), dtype=np.int64)
    for di in range(n_days):
        column = panel_train_c2c[:, di]
        ranks = np.argsort(np.argsort(column))
        deciles[:, di] = (ranks * N_DECILES // n_symbols).clip(0, N_DECILES - 1)
    return deciles


def sample_pairs(deciles: np.ndarray, rng: np.random.Generator, n_pairs: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Sample (anchor, positive, negative) symbol triplets from random train days.

    positive = same return decile that day (co-moved); negative = a different-decile symbol that day.
    """
    n_symbols, n_days = deciles.shape
    anchors = np.empty(n_pairs, dtype=np.int64)
    positives = np.empty(n_pairs, dtype=np.int64)
    negatives = np.empty(n_pairs, dtype=np.int64)
    filled = 0
    while filled < n_pairs:
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
        negative = diff[rng.integers(0, len(diff))]
        anchors[filled] = anchor
        positives[filled] = positive
        negatives[filled] = negative
        filled += 1
    return anchors, positives, negatives


def train_contrastive_ae(
    features: np.ndarray,
    deciles: np.ndarray,
    device: torch.device,
    rng: np.random.Generator,
) -> tuple[np.ndarray, dict]:
    """Train the contrastive autoencoder; return (embedding, train_log)."""
    torch.manual_seed(SEED)
    features_tensor = torch.from_numpy(features).to(device)
    model = ContrastiveAutoencoder(features.shape[1], EMBED_DIM).to(device)
    optimizer = torch.optim.Adam(model.parameters(), lr=LR)
    recon_weight = 0.5
    temperature = 0.2

    losses: list[float] = []
    for epoch in range(EPOCHS):
        model.train()
        anchors, positives, negatives = sample_pairs(deciles, rng, PAIRS_PER_STEP)
        anchor_idx = torch.from_numpy(anchors).to(device)
        positive_idx = torch.from_numpy(positives).to(device)
        negative_idx = torch.from_numpy(negatives).to(device)

        embedding, reconstruction = model(features_tensor)
        recon_loss = F.mse_loss(reconstruction, features_tensor)

        normed = F.normalize(embedding, dim=1)
        anchor_vec = normed[anchor_idx]
        positive_vec = normed[positive_idx]
        negative_vec = normed[negative_idx]
        positive_sim = (anchor_vec * positive_vec).sum(dim=1) / temperature
        negative_sim = (anchor_vec * negative_vec).sum(dim=1) / temperature
        logits = torch.stack([positive_sim, negative_sim], dim=1)
        targets = torch.zeros(logits.shape[0], dtype=torch.long, device=device)
        contrastive_loss = F.cross_entropy(logits, targets)

        loss = contrastive_loss + recon_weight * recon_loss
        optimizer.zero_grad()
        loss.backward()
        optimizer.step()
        if epoch % 20 == 0 or epoch == EPOCHS - 1:
            losses.append(round(float(loss.item()), 4))

    model.eval()
    with torch.no_grad():
        embedding = model.encoder(features_tensor).cpu().numpy()
    return embedding.astype(np.float32), {"loss_curve": losses}


def linear_svd_c2c(panel_train_c2c: np.ndarray, dim: int) -> np.ndarray:
    """#76 baseline: per-symbol z-score the c2c train series, top-`dim` SVD -> embedding (U*S)."""
    mean = panel_train_c2c.mean(axis=1, keepdims=True)
    std = panel_train_c2c.std(axis=1, keepdims=True)
    std = np.where(std < 1e-8, 1.0, std)
    standardized = (panel_train_c2c - mean) / std
    left, singular, _ = np.linalg.svd(standardized, full_matrices=False)
    return (left[:, :dim] * singular[:dim]).astype(np.float32)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profiles", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(SEED)

    data = np.load(args.profiles, allow_pickle=True)
    panel = data["panel"]  # n_symbols x n_days x n_channels (cross-sectionally standardized)
    symbols = [str(sym) for sym in data["symbols"]]
    channels = [str(channel) for channel in data["channels"]]
    c2c_idx = channels.index("c2c")

    n_days = panel.shape[1]
    split = int(n_days * TRAIN_FRAC)
    panel_train = panel[:, :split, :]
    panel_test_c2c = panel[:, split:, c2c_idx]
    panel_train_c2c = panel[:, :split, c2c_idx]
    test_corr = heldout_corr(panel_test_c2c)

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print(f"device: {device} | symbols: {len(symbols)} | train days: {split} | test days: {n_days - split}")

    results: dict[str, dict] = {}

    # Arm A — #76 linear baseline (c2c-only SVD)
    embed_a = linear_svd_c2c(panel_train_c2c, EMBED_DIM)
    results["A_baseline_c2c_svd"] = evaluate_embedding(embed_a, test_corr, np.random.default_rng(SEED))

    # Arm B — multi-channel linear PCA (isolates the data lift)
    features, feature_names = per_symbol_summary(panel_train, channels)
    pca = PCA(n_components=EMBED_DIM, random_state=SEED)
    embed_b = pca.fit_transform(features).astype(np.float32)
    results["B_multichannel_pca"] = evaluate_embedding(embed_b, test_corr, np.random.default_rng(SEED))

    # Arm C — non-linear contrastive autoencoder on the GPU (isolates the non-linearity lift)
    deciles = build_decile_labels(panel_train_c2c)
    embed_c, train_log = train_contrastive_ae(features, deciles, device, rng)
    results["C_multichannel_contrastive_ae"] = evaluate_embedding(embed_c, test_corr, np.random.default_rng(SEED))
    results["C_multichannel_contrastive_ae"]["train"] = train_log

    summary = {
        "data": {
            "profiles": args.profiles,
            "n_symbols": len(symbols),
            "n_days_total": int(n_days),
            "train_days": int(split),
            "test_days": int(n_days - split),
            "channels": channels,
            "n_summary_features": features.shape[1],
        },
        "embed_dim": EMBED_DIM,
        "n_clusters": N_CLUSTERS,
        "arms": results,
        "verdict": {
            "baseline_c2c_cohesion": results["A_baseline_c2c_svd"]["heldout_cohesion"],
            "best_arm": max(results, key=lambda arm: results[arm]["heldout_cohesion"]),
            "ae_beats_baseline": bool(
                results["C_multichannel_contrastive_ae"]["heldout_cohesion"]
                > results["A_baseline_c2c_svd"]["heldout_cohesion"]
            ),
            "ae_beats_linear_multichannel": bool(
                results["C_multichannel_contrastive_ae"]["heldout_cohesion"]
                > results["B_multichannel_pca"]["heldout_cohesion"]
            ),
        },
    }
    (out_dir / "embedding_result.json").write_text(json.dumps(summary, indent=2))
    np.savez_compressed(
        out_dir / "behavioral_embedding.npz",
        embedding_ae=embed_c,
        embedding_pca=embed_b,
        embedding_baseline=embed_a,
        symbols=np.array(symbols),
        feature_names=np.array(feature_names),
        channels=np.array(channels),
    )
    print(json.dumps(summary, indent=2))
    print(f"\nwrote {out_dir / 'embedding_result.json'}")


if __name__ == "__main__":
    main()
