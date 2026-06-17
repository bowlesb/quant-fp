"""Day-ahead behavioral embedding + the HONEST feature-IC validation (the v2-gate lesson applied).

The question is NOT "does an embedding have nice structure" (repr-2 answered that) but "does a parity-true
day-ahead feature have real held-out-time predictive IC on a day-ahead target, NON-REDUNDANT with simple
existing features". So we go straight to the feature bar:

Per next-day target (resid_ret_next, realized_vol_next, overnight_gap_next) we report held-out-time
cross-sectional IC (per-date Spearman/Pearson of feature vs target, averaged) for:
  A. SIMPLE baselines = the raw trailing profile features themselves (e.g. trailing realized vol c2c_std ->
     next-day realized vol; momentum -> next-day return). These ARE the existing-feature analogs; the learned
     embedding must beat the BEST simple feature to be non-redundant.
  B. LEARNED embedding = a small AE over the 12-dim profile (GPU); its z-coords linearly probed to each target
     on TRAIN dates only, then the probe's prediction scored by held-out-time IC. (Probe fit on train, IC on
     held-out time -> no leakage.)

Verdict per target: ship a day-ahead feature ONLY if the learned embedding's held-out IC materially beats the
best simple feature's held-out IC (else the simple feature already captures it -> ship nothing / ship the
simple feature, which needs no GPU). Honest: if even the simple features have ~0 held-out IC on returns
(expected — daily returns are hard), say so; realized-vol is where structure plausibly survives.

Parity note: the embedding is a deterministic function of the trailing profile -> a FROZEN NIGHTLY per-symbol
static lookup (coords), exactly the behavioral_clusters pattern. No FeatureState. The downstream feature is
the per-symbol coord (or the probe score) read from the nightly table.

Run (inside fp-torch-gpu):
  python experiments/gpu_dayahead/train_dayahead.py --data experiments/gpu_dayahead/out/dayahead.npz \
      --out experiments/gpu_dayahead/out
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F

EMBED_DIM = 8
EPOCHS = 200
LR = 1e-3
SEED = 0
TRAIN_FRAC = 0.8


class ProfileAE(nn.Module):
    """Small autoencoder over the 12-dim trailing profile -> EMBED_DIM coords (the day-ahead embedding)."""

    def __init__(self, n_features: int, embed_dim: int) -> None:
        super().__init__()
        self.encoder = nn.Sequential(
            nn.Linear(n_features, 64), nn.GELU(), nn.LayerNorm(64), nn.Linear(64, embed_dim)
        )
        self.decoder = nn.Sequential(nn.Linear(embed_dim, 64), nn.GELU(), nn.Linear(64, n_features))

    def forward(self, x: torch.Tensor) -> tuple[torch.Tensor, torch.Tensor]:
        z = self.encoder(x)
        return z, self.decoder(z)


def per_date_ic(feature: np.ndarray, target: np.ndarray, dates: np.ndarray) -> float:
    """Mean per-date cross-sectional Pearson corr(feature, target). The cross-sectional IC."""
    ics: list[float] = []
    for date in np.unique(dates):
        mask = dates == date
        if mask.sum() < 20:
            continue
        feat = feature[mask]
        targ = target[mask]
        if feat.std() < 1e-9 or targ.std() < 1e-9:
            continue
        ics.append(float(np.corrcoef(feat, targ)[0, 1]))
    return float(np.mean(ics)) if ics else 0.0


def standardize(train: np.ndarray, full: np.ndarray) -> np.ndarray:
    mean = train.mean(axis=0, keepdims=True)
    std = train.std(axis=0, keepdims=True)
    std = np.where(std < 1e-8, 1.0, std)
    return (full - mean) / std


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    torch.manual_seed(SEED)

    data = np.load(args.data, allow_pickle=True)
    profile = data["profile"].astype(np.float32)
    target = data["target"].astype(np.float32)
    dates = data["dates"]
    profile_names = [str(name) for name in data["profile_names"]]
    target_names = [str(name) for name in data["target_names"]]

    unique_dates = np.array(sorted(set(dates.tolist())))
    cut = unique_dates[int(TRAIN_FRAC * len(unique_dates))]
    is_train = dates < cut
    is_test = ~is_train

    profile_std = standardize(profile[is_train], profile)
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print(
        f"device: {device} | rows: {profile.shape[0]} | train: {int(is_train.sum())} | "
        f"test: {int(is_test.sum())} | cut: {cut}"
    )

    # A. simple baselines: each raw profile feature's held-out-time IC on each target
    simple_ic: dict[str, dict[str, float]] = {}
    for tj, tname in enumerate(target_names):
        per_feature = {}
        for fi, fname in enumerate(profile_names):
            per_feature[fname] = round(
                per_date_ic(profile[is_test, fi], target[is_test, tj], dates[is_test]), 4
            )
        simple_ic[tname] = per_feature

    # B. learned embedding (AE) then linear probe per target (probe fit on TRAIN, IC on held-out time)
    train_x = torch.from_numpy(profile_std[is_train]).to(device)
    full_x = torch.from_numpy(profile_std).to(device)
    model = ProfileAE(profile.shape[1], EMBED_DIM).to(device)
    optimizer = torch.optim.Adam(model.parameters(), lr=LR)
    for _ in range(EPOCHS):
        model.train()
        z, recon = model(train_x)
        loss = F.mse_loss(recon, train_x)
        optimizer.zero_grad()
        loss.backward()
        optimizer.step()
    model.eval()
    with torch.no_grad():
        embedding = model.encoder(full_x).cpu().numpy()

    embed_ic: dict[str, float] = {}
    for tj, tname in enumerate(target_names):
        # linear probe: fit z -> target on TRAIN, predict on all, score IC on held-out time
        z_train = embedding[is_train]
        y_train = target[is_train, tj]
        coef, _, _, _ = np.linalg.lstsq(
            np.hstack([z_train, np.ones((z_train.shape[0], 1))]), y_train, rcond=None
        )
        z_full = np.hstack([embedding, np.ones((embedding.shape[0], 1))])
        prediction = z_full @ coef
        embed_ic[tname] = round(per_date_ic(prediction[is_test], target[is_test, tj], dates[is_test]), 4)

    verdict = {}
    for tname in target_names:
        best_simple = max(simple_ic[tname].items(), key=lambda kv: abs(kv[1]))
        learned = embed_ic[tname]
        verdict[tname] = {
            "best_simple_feature": best_simple[0],
            "best_simple_ic": best_simple[1],
            "learned_embedding_ic": learned,
            "embedding_beats_simple": bool(abs(learned) > abs(best_simple[1]) + 0.005),
        }

    summary = {
        "data": {
            "rows": int(profile.shape[0]),
            "n_symbols": int(len(set(data["symbols"].tolist()))),
            "train_rows": int(is_train.sum()),
            "test_rows": int(is_test.sum()),
            "date_cut": str(cut),
        },
        "embed_dim": EMBED_DIM,
        "simple_feature_heldout_ic": simple_ic,
        "learned_embedding_heldout_ic": embed_ic,
        "verdict": verdict,
        "ship_any_dayahead_feature": bool(
            any(verdict[tname]["embedding_beats_simple"] for tname in target_names)
        ),
    }
    (out_dir / "dayahead_result.json").write_text(json.dumps(summary, indent=2))
    np.savez_compressed(
        out_dir / "dayahead_embedding.npz",
        embedding=embedding.astype(np.float32),
        symbols=data["symbols"],
        dates=dates,
    )
    torch.save(model.state_dict(), out_dir / "dayahead_ae.pt")
    print(json.dumps(summary, indent=2))
    print(f"\nwrote {out_dir / 'dayahead_result.json'}")


if __name__ == "__main__":
    main()
