"""Train the D1 beta-VAE on the feature panel and run the PRE-REGISTERED rigor checks.

Pre-registered "good" (decided before training, see PLAN.md D1):
  1. Held-out SYMBOLS: recon R2 on unseen symbols within 20% relative of train R2.
  2. Held-out TIME:    recon R2 on the last-20% minutes (post-embargo) within 20% relative of train R2.
  3. Beats PCA:        VAE recon R2 >= PCA recon R2 at equal latent dim (else linear suffices).
  4. Structured latent: >=75% of latent dims have KL > 0.01 (no collapse); sector silhouette in z >
                        sector silhouette in a random projection of equal dim.

Substrate: the synthetic panel (planted sector/regime structure) by default; swap --panel for the real
FeatureStoreClient export when the vector backfill lands. Synthetic results prove the harness RECOVERS
structure, not that the structure exists in real markets.

Run (host venv with torch+CUDA):
  /home/ben/quant-fp/experiments/dl_research/.venv/bin/python experiments/gpu_repr/train_vae.py \
      --panel /home/ben/quant-fp/experiments/dl_research/synth_panel.parquet --latent 16 --epochs 30
"""
from __future__ import annotations

import argparse
import json
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import polars as pl
import torch
from sklearn.decomposition import PCA
from sklearn.metrics import silhouette_score
from torch.utils.data import DataLoader, TensorDataset

from models.vae import FeatureVAE, per_dim_kl, vae_loss

ID_COLS = ["minute", "symbol"]
LABEL_COLS = ["fwd_ret_5m", "fwd_ret_15m", "fwd_ret_30m", "fwd_ret_60m"]
# Synthetic-panel structure columns used ONLY for post-hoc analysis (never as model input).
STRUCTURE_COLS = ["sector", "regime"]
HERE = Path(__file__).resolve().parent
OUT_DIR = HERE / "out"


@dataclass
class Splits:
    """Index masks over panel rows for the two generalization tests."""

    train_rows: np.ndarray
    heldout_symbol_rows: np.ndarray
    heldout_time_rows: np.ndarray


def load_panel(path: str) -> tuple[pl.DataFrame, list[str]]:
    frame = pl.read_parquet(path)
    feature_names = [
        col
        for col in frame.columns
        if col not in ID_COLS and col not in LABEL_COLS and col not in STRUCTURE_COLS
    ]
    return frame, feature_names


def build_splits(
    frame: pl.DataFrame,
    embargo_minutes: int,
    heldout_symbol_frac: float,
    heldout_time_frac: float,
    seed: int,
) -> Splits:
    """Disjoint held-out-symbol and held-out-time test rows; the rest is train (purged + embargoed).

    Held-out symbols are removed from train entirely (symbol-identity generalization). Held-out time is
    the last `heldout_time_frac` of minutes; an embargo gap of `embargo_minutes` is dropped between train
    and the time-test window so multi-minute feature memory cannot leak.
    """
    rng = np.random.default_rng(seed)
    symbols = frame["symbol"].unique(maintain_order=True).to_list()
    n_heldout_sym = max(1, int(len(symbols) * heldout_symbol_frac))
    heldout_symbols = set(rng.choice(symbols, size=n_heldout_sym, replace=False).tolist())

    # Map each row's minute to a contiguous integer id (avoids datetime64-vs-datetime membership pitfalls).
    unique_minutes = frame["minute"].unique(maintain_order=True).to_list()
    n_minutes = len(unique_minutes)
    minute_to_id = {minute: idx for idx, minute in enumerate(unique_minutes)}
    minute_id_arr = np.array([minute_to_id[minute] for minute in frame["minute"].to_list()], dtype=np.int64)
    symbol_arr = frame["symbol"].to_numpy()

    time_cut = int(n_minutes * (1.0 - heldout_time_frac))
    is_heldout_sym = np.array([sym in heldout_symbols for sym in symbol_arr])
    is_time_test = minute_id_arr >= time_cut
    is_embargo = (minute_id_arr >= time_cut - embargo_minutes) & (minute_id_arr < time_cut)

    heldout_time_rows = np.where(is_time_test & ~is_heldout_sym)[0]
    heldout_symbol_rows = np.where(is_heldout_sym & ~is_time_test)[0]
    train_rows = np.where(~is_heldout_sym & ~is_time_test & ~is_embargo)[0]
    return Splits(train_rows=train_rows, heldout_symbol_rows=heldout_symbol_rows, heldout_time_rows=heldout_time_rows)


def standardize(
    matrix: np.ndarray, mean: np.ndarray | None = None, std: np.ndarray | None = None
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """z-score columns using TRAIN stats only (passed in for test sets). NaNs -> 0 after standardizing."""
    if mean is None or std is None:
        mean = np.nanmean(matrix, axis=0)
        std = np.nanstd(matrix, axis=0)
    std_safe = np.where(std < 1e-8, 1.0, std)
    out = (matrix - mean) / std_safe
    out = np.nan_to_num(out, nan=0.0, posinf=0.0, neginf=0.0).astype(np.float32)
    return out, mean, std_safe


def recon_r2(model: FeatureVAE, data: np.ndarray, device: torch.device, batch: int = 8192) -> float:
    """OOS reconstruction R2 vs the zero baseline (data is z-scored so total var per col ~= 1).

    R2 = 1 - SS_res / SS_tot, with SS_tot computed against the column means of THIS set.
    """
    if len(data) == 0:
        return float("nan")
    model.eval()
    tensor = torch.from_numpy(data)
    preds = np.empty_like(data)
    with torch.no_grad():
        for start in range(0, len(tensor), batch):
            chunk = tensor[start : start + batch].to(device)
            mu, _ = model.encode(chunk)
            preds[start : start + batch] = model.decode(mu).cpu().numpy()
    ss_res = float(((data - preds) ** 2).sum())
    ss_tot = float(((data - data.mean(axis=0)) ** 2).sum())
    return 1.0 - ss_res / ss_tot if ss_tot > 0 else float("nan")


def pca_r2(train: np.ndarray, test: np.ndarray, latent_dim: int) -> float:
    if len(test) == 0 or len(train) == 0:
        return float("nan")
    pca = PCA(n_components=latent_dim, svd_solver="randomized", random_state=0)
    pca.fit(train)
    recon = pca.inverse_transform(pca.transform(test))
    ss_res = float(((test - recon) ** 2).sum())
    ss_tot = float(((test - test.mean(axis=0)) ** 2).sum())
    return 1.0 - ss_res / ss_tot if ss_tot > 0 else float("nan")


def latent_silhouette(
    model: FeatureVAE, data: np.ndarray, labels: np.ndarray, device: torch.device, sample: int = 5000
) -> tuple[float, float]:
    """Silhouette of structure labels in the VAE latent vs in a random projection of equal dim.

    A latent that organizes states by sector should beat a random linear projection. Subsampled for speed.
    """
    rng = np.random.default_rng(0)
    if len(data) > sample:
        idx = rng.choice(len(data), size=sample, replace=False)
        data, labels = data[idx], labels[idx]
    model.eval()
    with torch.no_grad():
        mu, _ = model.encode(torch.from_numpy(data).to(device))
        latent = mu.cpu().numpy()
    rand_proj = data @ rng.standard_normal((data.shape[1], model.latent_dim)).astype(np.float32)
    vae_sil = float(silhouette_score(latent, labels))
    rand_sil = float(silhouette_score(rand_proj, labels))
    return vae_sil, rand_sil


def train_vae(
    train_std: np.ndarray,
    in_dim: int,
    latent_dim: int,
    epochs: int,
    beta: float,
    lr: float,
    device: torch.device,
) -> tuple[FeatureVAE, list[dict[str, float]]]:
    model = FeatureVAE(in_dim=in_dim, latent_dim=latent_dim).to(device)
    optimizer = torch.optim.AdamW(model.parameters(), lr=lr, weight_decay=1e-5)
    loader = DataLoader(
        TensorDataset(torch.from_numpy(train_std)),
        batch_size=2048,
        shuffle=True,
        drop_last=True,
    )
    history: list[dict[str, float]] = []
    for epoch in range(epochs):
        model.train()
        beta_t = beta * min(1.0, (epoch + 1) / max(1, epochs // 3))  # KL warmup
        running = {"total": 0.0, "recon": 0.0, "kl": 0.0}
        n_batches = 0
        for (batch_x,) in loader:
            batch_x = batch_x.to(device)
            optimizer.zero_grad()
            recon, mu, logvar = model(batch_x)
            total, recon_mse, kl = vae_loss(recon, batch_x, mu, logvar, beta_t)
            total.backward()
            optimizer.step()
            running["total"] += float(total)
            running["recon"] += float(recon_mse)
            running["kl"] += float(kl)
            n_batches += 1
        history.append({key: value / n_batches for key, value in running.items()} | {"beta_t": beta_t})
    return model, history


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--panel", required=True)
    parser.add_argument("--latent", type=int, default=16)
    parser.add_argument("--epochs", type=int, default=30)
    parser.add_argument("--beta", type=float, default=0.5)
    parser.add_argument("--lr", type=float, default=1e-3)
    parser.add_argument("--embargo", type=int, default=60)
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args()

    torch.manual_seed(args.seed)
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    frame, feature_names = load_panel(args.panel)
    splits = build_splits(frame, args.embargo, 0.2, 0.2, args.seed)
    matrix = frame.select(feature_names).to_numpy().astype(np.float32)

    train_raw = matrix[splits.train_rows]
    train_std, mean, std = standardize(train_raw)
    sym_std, _, _ = standardize(matrix[splits.heldout_symbol_rows], mean, std)
    time_std, _, _ = standardize(matrix[splits.heldout_time_rows], mean, std)

    start = time.time()
    model, history = train_vae(
        train_std, len(feature_names), args.latent, args.epochs, args.beta, args.lr, device
    )
    train_secs = time.time() - start

    r2_train = recon_r2(model, train_std, device)
    r2_sym = recon_r2(model, sym_std, device)
    r2_time = recon_r2(model, time_std, device)
    r2_pca_sym = pca_r2(train_std, sym_std, args.latent)
    r2_pca_time = pca_r2(train_std, time_std, args.latent)

    mu_t, logvar_t = model.encode(torch.from_numpy(train_std[:20000]).to(device))
    kl_dims = per_dim_kl(mu_t, logvar_t).detach().cpu().numpy()
    active_dims = int((kl_dims > 0.01).sum())

    sector_result: dict[str, float] = {}
    if "sector" in frame.columns:
        sector_labels = frame["sector"].to_numpy()[splits.heldout_symbol_rows]
        vae_sil, rand_sil = latent_silhouette(model, sym_std, sector_labels, device)
        sector_result = {"vae_silhouette": vae_sil, "rand_silhouette": rand_sil}

    checks = {
        "1_heldout_symbol_pass": bool(r2_sym >= 0.8 * r2_train),
        "2_heldout_time_pass": bool(r2_time >= 0.8 * r2_train),
        "3_beats_pca_sym": bool(r2_sym >= r2_pca_sym),
        "3_beats_pca_time": bool(r2_time >= r2_pca_time),
        "4_no_collapse": bool(active_dims >= 0.75 * args.latent),
        "4_sector_structured": bool(sector_result.get("vae_silhouette", -1) > sector_result.get("rand_silhouette", 1)),
    }

    result = {
        "config": {k: v for k, v in vars(args).items()},
        "device": str(device),
        "gpu": torch.cuda.get_device_name(0) if torch.cuda.is_available() else None,
        "train_seconds": round(train_secs, 1),
        "n_train_rows": int(len(splits.train_rows)),
        "n_features": len(feature_names),
        "recon_r2": {
            "train": round(r2_train, 4),
            "heldout_symbol": round(r2_sym, 4),
            "heldout_time": round(r2_time, 4),
            "pca_heldout_symbol": round(r2_pca_sym, 4),
            "pca_heldout_time": round(r2_pca_time, 4),
        },
        "latent": {
            "dim": args.latent,
            "active_dims_kl_gt_0.01": active_dims,
            "kl_per_dim_top5": [round(float(x), 3) for x in sorted(kl_dims, reverse=True)[:5]],
        },
        "sector_structure": {k: round(v, 4) for k, v in sector_result.items()},
        "preregistered_checks": checks,
        "all_checks_pass": all(checks.values()),
        "final_train_loss": {k: round(v, 4) for k, v in history[-1].items()},
    }
    out_path = OUT_DIR / f"vae_z{args.latent}_result.json"
    out_path.write_text(json.dumps(result, indent=2))
    weights_path = OUT_DIR / f"vae_encoder_z{args.latent}.npz"
    np.savez(weights_path, mean=mean, std=std, **model.export_encoder_numpy())
    print(json.dumps(result, indent=2))
    print(f"\nwrote {out_path}\nwrote {weights_path}")


if __name__ == "__main__":
    main()
