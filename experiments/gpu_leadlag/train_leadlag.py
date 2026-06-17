"""Cross-sectional intraday LEAD-LAG model on the RTX 3090.

Question: does the minute-t cross-section predict symbol i's minute-(t+1) MARKET-RESIDUALIZED return, beyond
contemporaneous co-movement? The target is residualized so we are not rediscovering market beta. D3 proved
single-name autoregression is a vol-only null; this asks whether CROSS-symbol structure (A's move leads B's
next move) carries predictive content.

Model: a per-minute LSTM over the cross-sectional state vector x_t = [resid_t (S), flow_t (S), market_t]
(2S+1 dims). The hidden state predicts resid_{t+1} (S dims). The LSTM lets the model use a rolling window of
the cross-section, so genuine lead-lag (multi-minute) is representable. Trained with teacher forcing over the
RTH session, masked to valid (present) symbols.

PRE-REGISTERED bar (decided before training; the D3 baseline lesson applied):
  1. Held-out TIME: the last 20% of dates, never seen in training.
  2. PROPER baselines (NOT just persistence):
       - PREDICT-ZERO: resid_{t+1} = 0  (residual returns are ~mean-0; this is the honest floor).
       - OWN-LAG (AR1): resid_{t+1} = resid_t  (single-name autoregression — D3 showed this is ~null).
  3. The lead-lag model must beat predict-zero on held-out-time next-minute residual MSE AND show a positive
     held-out cross-sectional IC (corr of predicted vs realized resid_{t+1}). If it only matches predict-zero
     / own-lag, cross-sectional minute structure is not predictive at this resolution -> honest NULL, no
     feature (bank the prior, like D3).

Parity note (flagged): a lead-lag feature is CROSS-SECTIONAL + STATEFUL (rolling cross-section hidden state)
-> RT-feasible only inside the FeatureState manager with the cross-sectional reduce path, NOT a per-symbol
lookup. Heavy parity build — only justified if the OOS edge is real and material.

Run (inside fp-torch-gpu):
  python experiments/gpu_leadlag/train_leadlag.py --panel experiments/gpu_leadlag/out/crosssection.npz \
      --out experiments/gpu_leadlag/out
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import torch
import torch.nn as nn

HIDDEN_DIM = 128
EPOCHS = 40
LR = 1e-3
BATCH = 8  # days per batch (each day = 390 x (2S+1) — a few days fit easily)
EVAL_BATCH = 8
SEED = 0
TRAIN_FRAC = 0.8


class LeadLagLSTM(nn.Module):
    """LSTM over cross-sectional state [resid_t, flow_t, market_t] -> predicts resid_{t+1} (S dims)."""

    def __init__(self, n_symbols: int, hidden_dim: int) -> None:
        super().__init__()
        self.lstm = nn.LSTM(2 * n_symbols + 1, hidden_dim, batch_first=True)
        self.head = nn.Linear(hidden_dim, n_symbols)

    def forward(self, state_seq: torch.Tensor) -> torch.Tensor:
        hidden_seq, _ = self.lstm(state_seq)
        return self.head(hidden_seq)


def make_state(resid: torch.Tensor, flow: torch.Tensor, market: torch.Tensor) -> torch.Tensor:
    """Cross-sectional state sequence: B x T x (2S+1) = concat(resid_t, flow_t, market_t)."""
    return torch.cat([resid, flow, market.unsqueeze(-1)], dim=-1)


def masked_mse(pred: torch.Tensor, target: torch.Tensor, mask: torch.Tensor) -> torch.Tensor:
    """MSE over present symbols (mask: B x T x S of 1/0). pred/target: B x T x S."""
    error = (pred - target) ** 2 * mask
    return error.sum() / mask.sum().clamp(min=1.0)


def cross_sectional_ic(pred: np.ndarray, target: np.ndarray, mask: np.ndarray) -> float:
    """Mean per-minute cross-sectional Pearson corr(pred, target) over present symbols (the lead-lag IC)."""
    ics: list[float] = []
    n_days, n_steps, _ = pred.shape
    for di in range(n_days):
        for ti in range(n_steps):
            present = mask[di, ti] > 0
            if present.sum() < 20:
                continue
            predicted = pred[di, ti, present]
            realized = target[di, ti, present]
            if predicted.std() < 1e-9 or realized.std() < 1e-9:
                continue
            ics.append(float(np.corrcoef(predicted, realized)[0, 1]))
    return float(np.mean(ics)) if ics else 0.0


def evaluate(
    model: LeadLagLSTM, resid: torch.Tensor, flow: torch.Tensor, market: torch.Tensor, device: torch.device
) -> dict:
    """Held-out next-minute residual MSE + cross-sectional IC for LSTM, predict-zero, own-lag baselines."""
    model.eval()
    lstm_se = zero_se = lag_se = total = 0.0
    pred_chunks: list[np.ndarray] = []
    target_chunks: list[np.ndarray] = []
    mask_chunks: list[np.ndarray] = []
    with torch.no_grad():
        for start in range(0, resid.shape[0], EVAL_BATCH):
            rb = resid[start : start + EVAL_BATCH].to(device)
            fb = flow[start : start + EVAL_BATCH].to(device)
            mb = market[start : start + EVAL_BATCH].to(device)
            state = make_state(rb, fb, mb)
            target = rb[:, 1:, :]
            mask = (rb[:, 1:, :].abs() > 1e-9).float()
            pred = model(state)[:, :-1, :]
            own_lag = rb[:, :-1, :]
            zero = torch.zeros_like(target)
            valid = mask.sum().clamp(min=1.0)
            lstm_se += float((((pred - target) ** 2) * mask).sum().item())
            zero_se += float((((zero - target) ** 2) * mask).sum().item())
            lag_se += float((((own_lag - target) ** 2) * mask).sum().item())
            total += float(valid.item())
            pred_chunks.append(pred.cpu().numpy())
            target_chunks.append(target.cpu().numpy())
            mask_chunks.append(mask.cpu().numpy())
    pred_all = np.concatenate(pred_chunks, axis=0)
    target_all = np.concatenate(target_chunks, axis=0)
    mask_all = np.concatenate(mask_chunks, axis=0)
    return {
        "lstm_mse": round(lstm_se / total, 7),
        "predict_zero_mse": round(zero_se / total, 7),
        "own_lag_mse": round(lag_se / total, 7),
        "lstm_beats_zero": bool(lstm_se < zero_se),
        "lstm_beats_own_lag": bool(lstm_se < lag_se),
        "skill_vs_zero_pct": round(100.0 * (zero_se - lstm_se) / zero_se, 3),
        "cross_sectional_ic": round(cross_sectional_ic(pred_all, target_all, mask_all), 4),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--panel", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    torch.manual_seed(SEED)

    data = np.load(args.panel, allow_pickle=True)
    resid = torch.from_numpy(data["resid"].astype(np.float32))  # D x T x S
    flow = torch.from_numpy(data["flow"].astype(np.float32))
    market = torch.from_numpy(data["market"].astype(np.float32))  # D x T
    dates = data["dates"]
    n_days, _, n_symbols = resid.shape

    split = int(n_days * TRAIN_FRAC)
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print(
        f"device: {device} | days: {n_days} | symbols: {n_symbols} | train_days: {split} | "
        f"test_days: {n_days - split}"
    )

    model = LeadLagLSTM(n_symbols, HIDDEN_DIM).to(device)
    optimizer = torch.optim.Adam(model.parameters(), lr=LR)

    loss_curve: list[float] = []
    for epoch in range(EPOCHS):
        model.train()
        perm = torch.randperm(split)
        epoch_loss = 0.0
        n_batches = 0
        for start in range(0, split, BATCH):
            idx = perm[start : start + BATCH]
            rb = resid[idx].to(device)
            fb = flow[idx].to(device)
            mb = market[idx].to(device)
            state = make_state(rb, fb, mb)
            target = rb[:, 1:, :]
            mask = (rb[:, 1:, :].abs() > 1e-9).float()
            pred = model(state)[:, :-1, :]
            loss = masked_mse(pred, target, mask)
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()
            epoch_loss += float(loss.item())
            n_batches += 1
        if epoch % 5 == 0 or epoch == EPOCHS - 1:
            loss_curve.append(round(epoch_loss / n_batches, 7))

    train_eval = evaluate(model, resid[:split], flow[:split], market[:split], device)
    test_eval = evaluate(model, resid[split:], flow[split:], market[split:], device)

    summary = {
        "data": {
            "days": int(n_days),
            "minutes": int(resid.shape[1]),
            "symbols": int(n_symbols),
            "train_days": int(split),
            "test_days": int(n_days - split),
            "date_range": [str(dates[0]), str(dates[-1])],
        },
        "model": {"hidden_dim": HIDDEN_DIM, "epochs": EPOCHS, "loss_curve": loss_curve},
        "next_minute_residual": {"train": train_eval, "heldout_time": test_eval},
        "verdict": {
            "lstm_beats_zero_heldout": test_eval["lstm_beats_zero"],
            "heldout_cross_sectional_ic": test_eval["cross_sectional_ic"],
            "ship_leadlag_feature": bool(
                test_eval["lstm_beats_zero"] and test_eval["cross_sectional_ic"] > 0.02
            ),
        },
    }
    (out_dir / "leadlag_result.json").write_text(json.dumps(summary, indent=2))
    torch.save(model.state_dict(), out_dir / "leadlag_lstm.pt")
    print(json.dumps(summary, indent=2))
    print(f"\nwrote {out_dir / 'leadlag_result.json'}")


if __name__ == "__main__":
    main()
