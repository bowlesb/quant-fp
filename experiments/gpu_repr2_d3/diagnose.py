"""Honesty diagnostics for the D3 world-model — is the 50% MSE win real skill or a baseline artifact?

The headline run shows the LSTM beats a PERSISTENCE baseline by ~50% on next-minute MSE (held-out time AND
symbols). Before claiming a shippable surprise feature, we stress the result:

  1. PREDICT-ZERO baseline. Targets are z-scored WITHIN session (mean 0). For a near-white return series,
     predicting 0 is strong and persistence (predict last) is WEAK. If "predict 0" already beats persistence
     by ~50%, the LSTM's win is mostly "don't chase noise", not learned dynamics. We report the LSTM vs BOTH
     baselines.
  2. PER-CHANNEL decomposition. logret (channel 0) is near-unpredictable; range (channel 1) is strongly
     autocorrelated (vol clustering). If the LSTM's edge lives almost entirely in the RANGE channel, the
     "surprise" feature is a volatility-clustering tracker — useful but largely captured by existing realized
     range / vol features. We report per-channel LSTM-vs-zero skill so the win is attributable.
  3. SURPRISE INFORMATION. corr(surprise, next_range) was 0.66 — but is surprise predictable from the
     CURRENT range alone (i.e. is it just current-vol)? We also report corr(surprise, current_range).

This decides the HONEST feature verdict: ship only if the LSTM beats predict-zero (not just persistence) AND
the surprise carries information beyond current realized range.

Run (inside fp-torch-gpu):
  python experiments/gpu_repr2_d3/diagnose.py --sequences experiments/gpu_repr2_d3/out/sequences.npz \
      --model experiments/gpu_repr2_d3/out/worldmodel_lstm.pt --out experiments/gpu_repr2_d3/out
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import torch

from train_worldmodel import (
    EVAL_BATCH,
    HIDDEN_DIM,
    SEED,
    IntradayLSTM,
    _pearson_from_sums,
    build_valid_mask,
    next_step_targets,
    split_indices,
)


def channel_mse(
    model: IntradayLSTM, sequences_cpu: torch.Tensor, device: torch.device, eval_batch: int
) -> dict:
    """Per-channel next-step MSE for LSTM, persistence, and predict-zero, accumulated over minibatches."""
    model.eval()
    sums = {name: np.zeros(2) for name in ["lstm", "persistence", "zero"]}
    total_valid = 0.0
    surprise_vs_curr_range = {key: 0.0 for key in ["sum_x", "sum_y", "sum_xx", "sum_yy", "sum_xy", "n"]}
    with torch.no_grad():
        for start in range(0, sequences_cpu.shape[0], eval_batch):
            batch = sequences_cpu[start : start + eval_batch].to(device)
            target = next_step_targets(batch)  # B x (T-1) x 2
            valid = build_valid_mask(batch).bool()  # B x (T-1)
            prediction, _ = model(batch)
            prediction = prediction[:, :-1, :]
            persistence = batch[:, :-1, (0, 1)]
            zero = torch.zeros_like(target)
            valid_f = valid.float().unsqueeze(-1)
            for name, pred in [("lstm", prediction), ("persistence", persistence), ("zero", zero)]:
                err = ((pred - target) ** 2 * valid_f).sum(dim=(0, 1))  # per-channel
                sums[name] += err.cpu().numpy()
            total_valid += float(valid.sum().item())

            surprise = ((prediction - target) ** 2).mean(dim=-1)[valid]
            curr_range = batch[:, :-1, 1][valid]  # range at t (not t+1)
            stats = surprise_vs_curr_range
            stats["sum_x"] += float(surprise.sum().item())
            stats["sum_y"] += float(curr_range.sum().item())
            stats["sum_xx"] += float((surprise**2).sum().item())
            stats["sum_yy"] += float((curr_range**2).sum().item())
            stats["sum_xy"] += float((surprise * curr_range).sum().item())
            stats["n"] += float(surprise.numel())

    result = {}
    for name in ["lstm", "persistence", "zero"]:
        per_channel = sums[name] / max(total_valid, 1.0)
        result[name] = {
            "logret_mse": round(float(per_channel[0]), 5),
            "range_mse": round(float(per_channel[1]), 5),
        }
    result["lstm_vs_zero_skill_pct"] = {
        "logret": round(
            100
            * (result["zero"]["logret_mse"] - result["lstm"]["logret_mse"])
            / result["zero"]["logret_mse"],
            2,
        ),
        "range": round(
            100 * (result["zero"]["range_mse"] - result["lstm"]["range_mse"]) / result["zero"]["range_mse"], 2
        ),
    }
    result["corr_surprise_vs_current_range"] = round(_pearson_from_sums(surprise_vs_curr_range), 3)
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sequences", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    rng = np.random.default_rng(SEED)

    data = np.load(args.sequences, allow_pickle=True)
    sequences = data["sequences"].astype(np.float32)
    symbols = data["symbols"]
    dates = data["dates"]
    splits = split_indices(symbols, dates, rng)

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model = IntradayLSTM(sequences.shape[2], HIDDEN_DIM).to(device)
    model.load_state_dict(torch.load(args.model, map_location=device))

    heldout_time_x = torch.from_numpy(sequences[splits["heldout_time"]])
    diag = channel_mse(model, heldout_time_x, device, EVAL_BATCH)

    zero_beats_persistence_combined = (
        diag["zero"]["logret_mse"] + diag["zero"]["range_mse"]
        < diag["persistence"]["logret_mse"] + diag["persistence"]["range_mse"]
    )
    lstm_beats_zero_combined = (
        diag["lstm"]["logret_mse"] + diag["lstm"]["range_mse"]
        < diag["zero"]["logret_mse"] + diag["zero"]["range_mse"]
    )
    summary = {
        "heldout_time_per_channel_mse": {
            "lstm": diag["lstm"],
            "persistence": diag["persistence"],
            "predict_zero": diag["zero"],
        },
        "lstm_vs_zero_skill_pct": diag["lstm_vs_zero_skill_pct"],
        "corr_surprise_vs_current_range": diag["corr_surprise_vs_current_range"],
        "honest_verdict": {
            "predict_zero_beats_persistence": bool(zero_beats_persistence_combined),
            "lstm_beats_predict_zero": bool(lstm_beats_zero_combined),
            "lstm_edge_is_in_range_channel": bool(
                diag["lstm_vs_zero_skill_pct"]["range"]
                > 3 * max(diag["lstm_vs_zero_skill_pct"]["logret"], 0.01)
            ),
        },
    }
    out_path = Path(args.out) / "diagnose_result.json"
    out_path.write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))
    print(f"\nwrote {out_path}")


if __name__ == "__main__":
    main()
