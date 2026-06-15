"""End-to-end harness: synthetic panel -> loader (FeatureStoreClient contract) -> walk-forward
purged+embargoed splits -> {GBDT baseline, Rank-1 DL} -> rank-IC/ICIR per horizon.

Goals (per the agenda):
  (i)   prove the harness works end to end against the FeatureStoreClient interface,
  (ii)  confirm the DL model recovers the PLANTED cross-sectional signal,
  (iii) measure 3090 train time + memory.

Run:  .venv/bin/python train.py            # full run
      .venv/bin/python train.py --quick    # smaller/faster smoke run
"""
from __future__ import annotations

import argparse
import json
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import lightgbm as lgb
import numpy as np
import torch

from evaluation import HorizonMetrics, evaluate_horizon, walk_forward_splits
from loader import HORIZONS, SyntheticFeatureStoreClient, panel_to_arrays
from model import ModelConfig, RankOneModel, cross_sectional_rank_loss
from synth_panel import SIGNAL_FEATURES

HERE = Path(__file__).resolve().parent
PANEL_PATH = HERE / "synth_panel.parquet"
RESULTS_PATH = HERE / "results.json"

WIDE_START = datetime(2026, 1, 1, tzinfo=timezone.utc)
WIDE_END = datetime(2027, 1, 1, tzinfo=timezone.utc)


def load_arrays() -> object:
    client = SyntheticFeatureStoreClient.from_parquet(PANEL_PATH)
    feature_names = client.feature_names()
    # training_export is the contract call a model uses for the safe (certified) training set.
    frame = client.training_export(feature_names, WIDE_START, WIDE_END)
    arrays = panel_to_arrays(frame, feature_names)
    return arrays


def rows_in_minutes(minute_index: np.ndarray, minutes: np.ndarray) -> np.ndarray:
    return np.isin(minute_index, minutes)


def train_gbdt(arrays, splits) -> dict:
    """LightGBM, one regressor per horizon, evaluated walk-forward."""
    per_horizon: dict[str, list[HorizonMetrics]] = {f"{h}m": [] for h in HORIZONS}
    feature_importance = np.zeros(len(arrays.feature_names))
    start = time.time()
    for split in splits:
        train_mask = rows_in_minutes(arrays.minute_index, split.train_minutes)
        test_mask = rows_in_minutes(arrays.minute_index, split.test_minutes)
        x_train, x_test = arrays.features[train_mask], arrays.features[test_mask]
        for hi, horizon in enumerate(HORIZONS):
            y_train = arrays.labels[train_mask, hi]
            params = {
                "objective": "regression", "num_leaves": 31, "learning_rate": 0.05,
                "n_estimators": 200, "min_child_samples": 100, "subsample": 0.8,
                "colsample_bytree": 0.5, "reg_lambda": 1.0, "verbose": -1, "n_jobs": -1,
            }
            booster = lgb.LGBMRegressor(**params)
            booster.fit(x_train, y_train)
            preds = booster.predict(x_test)
            metrics = evaluate_horizon(preds, arrays.labels[test_mask, hi], arrays.minute_index[test_mask], f"{horizon}m")
            per_horizon[f"{horizon}m"].append(metrics)
            feature_importance += booster.feature_importances_
    elapsed = time.time() - start
    top_idx = np.argsort(feature_importance)[::-1][:10]
    top_features = [(arrays.feature_names[i], float(feature_importance[i])) for i in top_idx]
    return {"per_horizon": per_horizon, "elapsed_s": elapsed, "top_features": top_features}


def aggregate(per_horizon: dict[str, list[HorizonMetrics]]) -> dict:
    out = {}
    for horizon, metrics_list in per_horizon.items():
        if not metrics_list:
            continue
        out[horizon] = {
            "mean_rank_ic": float(np.mean([m.mean_rank_ic for m in metrics_list])),
            "icir": float(np.mean([m.icir for m in metrics_list])),
            "hit_rate": float(np.mean([m.hit_rate for m in metrics_list])),
            "n_folds": len(metrics_list),
        }
    return out


def train_dl(arrays, splits, device: str, quick: bool) -> dict:
    """Rank-1 DL model, trained per fold with the cross-sectional ranking loss."""
    config = ModelConfig(n_features=len(arrays.feature_names))
    per_horizon: dict[str, list[HorizonMetrics]] = {f"{h}m": [] for h in HORIZONS}
    n_epochs = 8 if quick else 30
    minutes_per_batch = 16  # one batch = several full cross-sections (so ranking loss has cross-sections)
    peak_mem_mb = 0.0
    start = time.time()
    param_count = 0
    for split in splits:
        torch.manual_seed(0)
        model = RankOneModel(config).to(device)
        param_count = sum(p.numel() for p in model.parameters())
        optimizer = torch.optim.AdamW(model.parameters(), lr=1e-3, weight_decay=1e-4)

        train_minutes = split.train_minutes
        train_rows_by_minute = {int(m): np.where(arrays.minute_index == m)[0] for m in train_minutes}
        # standardize features using TRAIN stats only (no leakage)
        train_mask = rows_in_minutes(arrays.minute_index, train_minutes)
        feat_mean = arrays.features[train_mask].mean(axis=0)
        feat_std = arrays.features[train_mask].std(axis=0) + 1e-6

        model.train()
        rng = np.random.default_rng(split.fold)
        for _epoch in range(n_epochs):
            shuffled = rng.permutation(train_minutes)
            for batch_start in range(0, len(shuffled), minutes_per_batch):
                batch_minutes = shuffled[batch_start : batch_start + minutes_per_batch]
                rows = np.concatenate([train_rows_by_minute[int(m)] for m in batch_minutes])
                x = (arrays.features[rows] - feat_mean) / feat_std
                x_t = torch.from_numpy(x.astype(np.float32)).to(device)
                y_t = torch.from_numpy(arrays.labels[rows]).to(device)
                m_t = torch.from_numpy(arrays.minute_index[rows]).to(device)
                optimizer.zero_grad()
                preds = model(x_t)
                loss = cross_sectional_rank_loss(preds, y_t, m_t)
                loss.backward()
                optimizer.step()
        if device == "cuda":
            peak_mem_mb = max(peak_mem_mb, torch.cuda.max_memory_allocated() / 1e6)

        # ---- evaluate on the test window ----
        model.eval()
        test_mask = rows_in_minutes(arrays.minute_index, split.test_minutes)
        x_test = (arrays.features[test_mask] - feat_mean) / feat_std
        with torch.no_grad():
            preds_test = model(torch.from_numpy(x_test.astype(np.float32)).to(device)).cpu().numpy()
        for hi, horizon in enumerate(HORIZONS):
            metrics = evaluate_horizon(
                preds_test[:, hi], arrays.labels[test_mask, hi], arrays.minute_index[test_mask], f"{horizon}m"
            )
            per_horizon[f"{horizon}m"].append(metrics)
    elapsed = time.time() - start
    return {"per_horizon": per_horizon, "elapsed_s": elapsed, "peak_mem_mb": peak_mem_mb,
            "param_count": param_count, "n_epochs": n_epochs}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--quick", action="store_true")
    args = parser.parse_args()

    device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"device: {device}", f"({torch.cuda.get_device_name(0)})" if device == "cuda" else "(CPU fallback)")

    arrays = load_arrays()
    print(f"panel: {arrays.features.shape[0]} rows, {arrays.features.shape[1]} features, "
          f"{arrays.n_minutes} minutes, {len(np.unique(arrays.symbol_index))} symbols")

    splits = walk_forward_splits(arrays.n_minutes, n_folds=4, max_horizon=60, embargo=30, min_train=600)
    print(f"walk-forward folds: {len(splits)}")

    print("\n=== GBDT baseline (LightGBM) ===")
    gbdt = train_gbdt(arrays, splits)
    gbdt_agg = aggregate(gbdt["per_horizon"])
    for horizon, vals in gbdt_agg.items():
        print(f"  {horizon}: rank-IC={vals['mean_rank_ic']:+.4f}  ICIR={vals['icir']:+.3f}  hit={vals['hit_rate']:.2f}")
    print(f"  train time: {gbdt['elapsed_s']:.1f}s")
    print("  top features by importance:")
    for name, imp in gbdt["top_features"][:8]:
        planted = " <-- PLANTED" if name in SIGNAL_FEATURES else ""
        print(f"    {name}: {imp:.0f}{planted}")

    print("\n=== Rank-1 DL (PLR + MLP trunk + 4 heads + CS ranking loss) ===")
    dl = train_dl(arrays, splits, device, args.quick)
    dl_agg = aggregate(dl["per_horizon"])
    for horizon, vals in dl_agg.items():
        print(f"  {horizon}: rank-IC={vals['mean_rank_ic']:+.4f}  ICIR={vals['icir']:+.3f}  hit={vals['hit_rate']:.2f}")
    print(f"  params: {dl['param_count']:,}  peak GPU mem: {dl['peak_mem_mb']:.0f} MB  "
          f"train time: {dl['elapsed_s']:.1f}s ({dl['n_epochs']} epochs/fold)")

    results = {
        "device": device,
        "gpu": torch.cuda.get_device_name(0) if device == "cuda" else None,
        "panel_shape": list(arrays.features.shape),
        "n_minutes": arrays.n_minutes,
        "n_folds": len(splits),
        "planted_signal_features": SIGNAL_FEATURES,
        "gbdt": {"per_horizon": gbdt_agg, "elapsed_s": gbdt["elapsed_s"], "top_features": gbdt["top_features"]},
        "dl": {"per_horizon": dl_agg, "elapsed_s": dl["elapsed_s"], "peak_mem_mb": dl["peak_mem_mb"],
               "param_count": dl["param_count"], "n_epochs": dl["n_epochs"]},
        "dl_config": asdict(ModelConfig(n_features=len(arrays.feature_names))),
    }
    RESULTS_PATH.write_text(json.dumps(results, indent=2, default=str))
    print(f"\nwrote {RESULTS_PATH}")


if __name__ == "__main__":
    main()
