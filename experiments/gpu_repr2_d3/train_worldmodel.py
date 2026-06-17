"""D3 — intraday SEQUENCE world-model (LSTM next-state predictor) on the RTX 3090.

Given a symbol's RTH minute path so far, predict the NEXT minute's behavior. The LSTM hidden state is a
learned dynamics embedding; the per-minute prediction SURPRISE (squared error of the next-step prediction)
is the candidate feature — "where is the intraday path predictable vs surprising". This is the temporal
DYNAMICS frontier, orthogonal to the static cross-sectional embeddings (#76 / repr-2).

Target: next-minute (logret, range) — the two scale-free behavior channels. The model is trained with
teacher forcing over the whole RTH sequence (predict t+1 from the hidden state at t).

PRE-REGISTERED rigor bar (decided before training; PLAN.md §D3):
  1. Held-out SYMBOLS: 20% of symbols never seen in training.
  2. Held-out TIME: the last 20% of dates never seen in training.
  3. The LSTM must BEAT a PERSISTENCE baseline (predict next = current value) on next-minute MSE, on BOTH
     held-out symbols and held-out time. If it does not beat persistence, intraday minute dynamics are not
     learnably-forecastable at this resolution and the surprise feature is just rescaled volatility -> we do
     NOT ship a D3 feature (an honest null, like the Stage-2a give-back result).

The surprise feature's HONESTY check: if the LSTM only matches persistence, then surprise == |actual - last|
== a trivial volatility proxy, which existing range/volatility features already capture. The feature earns
its place ONLY if the model's prediction is non-trivial (beats persistence) AND the resulting surprise is
non-redundant with plain realized range.

Parity caveat (flagged, not hidden): the surprise feature is RT-feasible ONLY with a rolling per-symbol
LSTM hidden state, which MUST live in the platform's FeatureState manager (state_spec/seed/fold/emit) so
backfill == stream by construction. It is NOT a stateless feedforward like the repr-2 cluster lookup.

Run (inside fp-torch-gpu):
  python experiments/gpu_repr2_d3/train_worldmodel.py --sequences experiments/gpu_repr2_d3/out/sequences.npz \
      --out experiments/gpu_repr2_d3/out
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import torch
import torch.nn as nn

HIDDEN_DIM = 64
EPOCHS = 30
LR = 1e-3
BATCH = 512
EVAL_BATCH = 1024
SEED = 0
TARGET_IDX = (0, 1)  # logret, range — the scale-free next-state targets


class IntradayLSTM(nn.Module):
    """One-layer LSTM over minute features -> next-minute (logret, range) prediction at every step."""

    def __init__(self, n_features: int, hidden_dim: int) -> None:
        super().__init__()
        self.lstm = nn.LSTM(n_features, hidden_dim, batch_first=True)
        self.head = nn.Linear(hidden_dim, len(TARGET_IDX))

    def forward(self, sequences: torch.Tensor) -> tuple[torch.Tensor, torch.Tensor]:
        hidden_seq, _ = self.lstm(sequences)
        prediction = self.head(hidden_seq)
        return prediction, hidden_seq


def split_indices(symbols: np.ndarray, dates: np.ndarray, rng: np.random.Generator) -> dict[str, np.ndarray]:
    """Train / held-out-symbol / held-out-time index sets (time split by date, symbol split by name)."""
    unique_symbols = np.array(sorted(set(symbols.tolist())))
    rng.shuffle(unique_symbols)
    n_heldout_sym = max(1, int(0.2 * len(unique_symbols)))
    heldout_symbols = set(unique_symbols[:n_heldout_sym].tolist())

    unique_dates = np.array(sorted(set(dates.tolist())))
    time_cut = unique_dates[int(0.8 * len(unique_dates))]
    is_late = dates >= time_cut
    is_heldout_sym = np.array([sym in heldout_symbols for sym in symbols])

    train = ~is_late & ~is_heldout_sym
    heldout_time = is_late & ~is_heldout_sym  # unseen time, seen symbols
    heldout_symbol = ~is_late & is_heldout_sym  # unseen symbols, seen time
    return {
        "train": np.flatnonzero(train),
        "heldout_time": np.flatnonzero(heldout_time),
        "heldout_symbol": np.flatnonzero(heldout_symbol),
        "time_cut": time_cut,
    }


def next_step_targets(sequences: torch.Tensor) -> torch.Tensor:
    """Target at step t = the (logret, range) at step t+1; last step has no target (masked)."""
    return sequences[:, 1:, TARGET_IDX]


def masked_mse(prediction: torch.Tensor, target: torch.Tensor, valid: torch.Tensor) -> torch.Tensor:
    """MSE over valid (non-pad) steps. prediction/target: B x T x 2; valid: B x T bool."""
    error = (prediction - target) ** 2
    error = error.mean(dim=-1)
    return (error * valid).sum() / valid.sum().clamp(min=1.0)


def persistence_mse(sequences: torch.Tensor, target: torch.Tensor, valid: torch.Tensor) -> float:
    """Baseline: predict next (logret,range) = current (logret,range). The bar the LSTM must beat."""
    current = sequences[:, :-1, TARGET_IDX]
    return float(masked_mse(current, target, valid).item())


def build_valid_mask(sequences: torch.Tensor) -> torch.Tensor:
    """Steps are valid (non-pad) where any feature in the NEXT minute is non-zero (pad rows are all-zero)."""
    nxt = sequences[:, 1:, :]
    return (nxt.abs().sum(dim=-1) > 1e-9).float()


def _pearson_from_sums(stats: dict[str, float]) -> float:
    """Pearson correlation from streaming sufficient statistics (sum_x, sum_y, sum_xx, sum_yy, sum_xy, n)."""
    n = stats["n"]
    cov = stats["sum_xy"] - stats["sum_x"] * stats["sum_y"] / n
    var_x = stats["sum_xx"] - stats["sum_x"] ** 2 / n
    var_y = stats["sum_yy"] - stats["sum_y"] ** 2 / n
    denom = (var_x * var_y) ** 0.5
    return float(cov / denom) if denom > 1e-12 else 0.0


def evaluate(model: IntradayLSTM, sequences_cpu: torch.Tensor, device: torch.device, eval_batch: int) -> dict:
    """Batched next-step MSE vs the persistence baseline. Batches move to GPU per-step (fits 24 GB)."""
    model.eval()
    lstm_sq = 0.0
    base_sq = 0.0
    total_valid = 0.0
    with torch.no_grad():
        for start in range(0, sequences_cpu.shape[0], eval_batch):
            batch = sequences_cpu[start : start + eval_batch].to(device)
            target = next_step_targets(batch)
            valid = build_valid_mask(batch)
            prediction, _ = model(batch)
            prediction = prediction[:, :-1, :]
            n_valid = float(valid.sum().item())
            lstm_sq += float(masked_mse(prediction, target, valid).item()) * n_valid
            base_sq += persistence_mse(batch, target, valid) * n_valid
            total_valid += n_valid
    lstm_mse = lstm_sq / max(total_valid, 1.0)
    base_mse = base_sq / max(total_valid, 1.0)
    return {
        "lstm_next_step_mse": round(lstm_mse, 5),
        "persistence_mse": round(base_mse, 5),
        "lstm_beats_persistence": bool(lstm_mse < base_mse),
        "skill_vs_persistence_pct": round(100.0 * (base_mse - lstm_mse) / base_mse, 2),
    }


def surprise_redundancy(
    model: IntradayLSTM, sequences_cpu: torch.Tensor, device: torch.device, eval_batch: int
) -> dict:
    """Is per-minute surprise just realized range? Streaming Pearson corr over minibatches."""
    model.eval()
    range_stats = {key: 0.0 for key in ["sum_x", "sum_y", "sum_xx", "sum_yy", "sum_xy", "n"]}
    absret_stats = {key: 0.0 for key in ["sum_x", "sum_y", "sum_xx", "sum_yy", "sum_xy", "n"]}
    with torch.no_grad():
        for start in range(0, sequences_cpu.shape[0], eval_batch):
            batch = sequences_cpu[start : start + eval_batch].to(device)
            target = next_step_targets(batch)
            valid = build_valid_mask(batch).bool()
            prediction, _ = model(batch)
            prediction = prediction[:, :-1, :]
            surprise = ((prediction - target) ** 2).mean(dim=-1)[valid]
            next_range = batch[:, 1:, 1][valid]
            next_absret = batch[:, 1:, 0].abs()[valid]
            for stats, other in [(range_stats, next_range), (absret_stats, next_absret)]:
                stats["sum_x"] += float(surprise.sum().item())
                stats["sum_y"] += float(other.sum().item())
                stats["sum_xx"] += float((surprise**2).sum().item())
                stats["sum_yy"] += float((other**2).sum().item())
                stats["sum_xy"] += float((surprise * other).sum().item())
                stats["n"] += float(surprise.numel())
    corr_range = _pearson_from_sums(range_stats)
    corr_absret = _pearson_from_sums(absret_stats)
    return {
        "corr_surprise_vs_range": round(corr_range, 3),
        "corr_surprise_vs_absret": round(corr_absret, 3),
        "redundant_with_range": bool(abs(corr_range) > 0.9),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sequences", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    torch.manual_seed(SEED)
    rng = np.random.default_rng(SEED)

    data = np.load(args.sequences, allow_pickle=True)
    sequences = data["sequences"].astype(np.float32)  # n x T x F
    symbols = data["symbols"]
    dates = data["dates"]
    feature_names = [str(name) for name in data["feature_names"]]

    splits = split_indices(symbols, dates, rng)
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print(
        f"device: {device} | sessions: {sequences.shape[0]} | seq_len: {sequences.shape[1]} | "
        f"features: {sequences.shape[2]}"
    )
    print(
        f"train: {len(splits['train'])} | heldout_time: {len(splits['heldout_time'])} | "
        f"heldout_symbol: {len(splits['heldout_symbol'])} | time_cut: {splits['time_cut']}"
    )

    # Keep the full panel on CPU (104k x 390 x 6 is ~1 GB); move only minibatches to the 24 GB GPU.
    train_x = torch.from_numpy(sequences[splits["train"]])
    model = IntradayLSTM(sequences.shape[2], HIDDEN_DIM).to(device)
    optimizer = torch.optim.Adam(model.parameters(), lr=LR)

    n_train = train_x.shape[0]
    loss_curve: list[float] = []
    for epoch in range(EPOCHS):
        model.train()
        perm = torch.randperm(n_train)
        epoch_loss = 0.0
        n_batches = 0
        for start in range(0, n_train, BATCH):
            batch = train_x[perm[start : start + BATCH]].to(device)
            target = next_step_targets(batch)
            valid = build_valid_mask(batch)
            prediction, _ = model(batch)
            prediction = prediction[:, :-1, :]
            loss = masked_mse(prediction, target, valid)
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()
            epoch_loss += float(loss.item())
            n_batches += 1
        if epoch % 5 == 0 or epoch == EPOCHS - 1:
            loss_curve.append(round(epoch_loss / n_batches, 5))

    heldout_time_x = torch.from_numpy(sequences[splits["heldout_time"]])
    heldout_symbol_x = torch.from_numpy(sequences[splits["heldout_symbol"]])
    train_eval = evaluate(model, train_x, device, EVAL_BATCH)
    heldout_time_eval = evaluate(model, heldout_time_x, device, EVAL_BATCH)
    heldout_symbol_eval = evaluate(model, heldout_symbol_x, device, EVAL_BATCH)
    redundancy = surprise_redundancy(model, heldout_time_x, device, EVAL_BATCH)

    summary = {
        "data": {
            "sessions": int(sequences.shape[0]),
            "seq_len": int(sequences.shape[1]),
            "features": feature_names,
            "n_symbols": int(len(set(symbols.tolist()))),
            "date_range": [str(min(dates.tolist())), str(max(dates.tolist()))],
        },
        "model": {"hidden_dim": HIDDEN_DIM, "epochs": EPOCHS, "loss_curve": loss_curve},
        "next_step_skill": {
            "train": train_eval,
            "heldout_time": heldout_time_eval,
            "heldout_symbol": heldout_symbol_eval,
        },
        "surprise_redundancy_heldout_time": redundancy,
        "verdict": {
            "beats_persistence_heldout_time": heldout_time_eval["lstm_beats_persistence"],
            "beats_persistence_heldout_symbol": heldout_symbol_eval["lstm_beats_persistence"],
            "surprise_redundant_with_range": redundancy["redundant_with_range"],
            "ship_d3_feature": bool(
                heldout_time_eval["lstm_beats_persistence"]
                and heldout_symbol_eval["lstm_beats_persistence"]
                and not redundancy["redundant_with_range"]
            ),
        },
    }
    (out_dir / "worldmodel_result.json").write_text(json.dumps(summary, indent=2))
    torch.save(model.state_dict(), out_dir / "worldmodel_lstm.pt")
    print(json.dumps(summary, indent=2))
    print(f"\nwrote {out_dir / 'worldmodel_result.json'}")


if __name__ == "__main__":
    main()
