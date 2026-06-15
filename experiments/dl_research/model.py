"""Rank-1 DL model: PLR numerical embeddings -> shared MLP trunk -> 4 multi-horizon heads,
trained with a cross-sectional ranking loss.

Design choices come straight from the paper survey (see NOTES.md):
  - PLR embeddings (Gorishniy 2022): per-feature  ReLU(Linear(Periodic(x))), Periodic = [sin,cos](2*pi*c*x)
    with TRAINABLE frequencies c ~ N(0, sigma^2). sigma is the key hyperparameter (start ~0.05).
  - Shared MLP trunk, SHALLOW (Gu/Kelly/Xiu: NN performance peaks ~3 layers in low-SNR finance),
    heavily regularised (dropout + weight decay + early stopping).
  - 4 linear heads, one per horizon, on the shared trunk.
  - Cross-sectional ranking loss: per minute, per horizon, negated Pearson correlation between the
    z-scored prediction and z-scored label (a differentiable soft rank-IC surrogate). Summed over
    horizons. This optimises the cross-sectional ORDERING, not pointwise MSE.
"""
from __future__ import annotations

from dataclasses import dataclass

import torch
import torch.nn as nn


@dataclass
class ModelConfig:
    n_features: int
    n_horizons: int = 4
    plr_dim: int = 12          # embedding dim per feature (kept modest so 519*dim stays bounded)
    plr_frequencies: int = 24  # k periodic frequencies per feature
    plr_sigma: float = 0.05    # init std of frequencies -- THE key PLR hyperparameter
    trunk_hidden: tuple[int, ...] = (512, 256)  # shallow trunk
    dropout: float = 0.2


class PLREmbedding(nn.Module):
    """Periodic-Linear-ReLU embedding (Gorishniy 2022), one independent embedding per feature.

    For input x of shape (B, F):
      periodic = [sin(2*pi*c*x), cos(2*pi*c*x)]  with c per-feature trainable, shape (F, k)
      out = ReLU(Linear(periodic))               per-feature Linear (2k -> d)
    returns (B, F, d), then flattened to (B, F*d) by the caller.
    """

    def __init__(self, n_features: int, n_freq: int, out_dim: int, sigma: float) -> None:
        super().__init__()
        self.n_features = n_features
        self.n_freq = n_freq
        self.out_dim = out_dim
        # trainable frequencies, init ~ N(0, sigma^2)
        self.frequencies = nn.Parameter(torch.randn(n_features, n_freq) * sigma)
        # per-feature linear: (F, 2*n_freq, out_dim) implemented as a batched matmul
        self.linear_weight = nn.Parameter(torch.randn(n_features, 2 * n_freq, out_dim) * (1.0 / (2 * n_freq) ** 0.5))
        self.linear_bias = nn.Parameter(torch.zeros(n_features, out_dim))
        self.act = nn.ReLU()

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        # x: (B, F) -> (B, F, 1)
        x_exp = x.unsqueeze(-1)
        # angles: (B, F, k)
        angles = 2.0 * torch.pi * x_exp * self.frequencies.unsqueeze(0)
        periodic = torch.cat([torch.sin(angles), torch.cos(angles)], dim=-1)  # (B, F, 2k)
        # per-feature linear via einsum: (B,F,2k) x (F,2k,d) -> (B,F,d)
        out = torch.einsum("bfk,fkd->bfd", periodic, self.linear_weight) + self.linear_bias.unsqueeze(0)
        return self.act(out)


class RankOneModel(nn.Module):
    def __init__(self, config: ModelConfig) -> None:
        super().__init__()
        self.config = config
        self.embedding = PLREmbedding(
            config.n_features, config.plr_frequencies, config.plr_dim, config.plr_sigma
        )
        trunk_in = config.n_features * config.plr_dim
        layers: list[nn.Module] = []
        prev = trunk_in
        for hidden in config.trunk_hidden:
            layers += [nn.Linear(prev, hidden), nn.BatchNorm1d(hidden), nn.ReLU(), nn.Dropout(config.dropout)]
            prev = hidden
        self.trunk = nn.Sequential(*layers)
        self.heads = nn.ModuleList([nn.Linear(prev, 1) for _ in range(config.n_horizons)])

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        emb = self.embedding(x)                       # (B, F, d)
        flat = emb.reshape(emb.shape[0], -1)          # (B, F*d)
        trunk_out = self.trunk(flat)                  # (B, H)
        preds = torch.cat([head(trunk_out) for head in self.heads], dim=1)  # (B, n_horizons)
        return preds


def cross_sectional_rank_loss(
    preds: torch.Tensor,
    targets: torch.Tensor,
    minute_ids: torch.Tensor,
) -> torch.Tensor:
    """Negated mean cross-sectional Pearson-IC (a differentiable rank-IC surrogate), summed over
    horizons. preds/targets: (B, n_horizons). minute_ids: (B,) integer minute grouping.

    For each minute group and each horizon: z-score preds and targets across the cross-section,
    take their correlation; loss = -mean over (minutes, horizons).
    """
    total = preds.new_zeros(())
    n_terms = 0
    unique = torch.unique(minute_ids)
    for minute_id in unique:
        sel = minute_ids == minute_id
        if sel.sum() < 5:
            continue
        pred_block = preds[sel]      # (n_sym, H)
        targ_block = targets[sel]    # (n_sym, H)
        pred_z = (pred_block - pred_block.mean(dim=0)) / (pred_block.std(dim=0) + 1e-6)
        targ_z = (targ_block - targ_block.mean(dim=0)) / (targ_block.std(dim=0) + 1e-6)
        ic = (pred_z * targ_z).mean(dim=0)  # (H,) per-horizon correlation
        total = total - ic.sum()
        n_terms += 1
    if n_terms == 0:
        return preds.new_zeros(()).requires_grad_(True)
    return total / n_terms
