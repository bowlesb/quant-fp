"""Walk-forward purged+embargoed splits and rank-IC / ICIR evaluation.

Protocol (Gu/Kelly/Xiu 2020 walk-forward + Lopez de Prado purge/embargo):
  - Split the MINUTE axis into contiguous expanding/rolling folds (never shuffle time).
  - PURGE: drop training minutes whose label horizon overlaps the test window (a 60-min forward
    label computed at the train/test boundary peeks into the test period -> leakage). We purge the
    last `max_horizon` minutes of train.
  - EMBARGO: additionally drop a buffer of minutes after the test window before the next train block,
    so serially-correlated features don't leak backward.

Metrics:
  - rank-IC per minute = Spearman corr across the cross-section between predicted score and realised
    forward return, then averaged over test minutes (the standard quant cross-sectional metric).
  - ICIR = mean(rank-IC) / std(rank-IC) over test minutes (the information-ratio of the signal).
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.stats import rankdata


@dataclass
class WalkForwardSplit:
    train_minutes: np.ndarray
    test_minutes: np.ndarray
    fold: int


def walk_forward_splits(
    n_minutes: int,
    n_folds: int = 4,
    max_horizon: int = 60,
    embargo: int = 30,
    min_train: int = 400,
) -> list[WalkForwardSplit]:
    """Expanding-window walk-forward with purge (max_horizon) + embargo between train and test."""
    test_size = (n_minutes - min_train) // n_folds
    splits: list[WalkForwardSplit] = []
    for fold in range(n_folds):
        test_start = min_train + fold * test_size
        test_end = test_start + test_size if fold < n_folds - 1 else n_minutes
        # purge the last max_horizon minutes of training so no train label overlaps the test window
        train_end = test_start - max_horizon - embargo
        if train_end < min_train // 2:
            continue
        train_minutes = np.arange(0, train_end)
        test_minutes = np.arange(test_start, test_end)
        splits.append(WalkForwardSplit(train_minutes, test_minutes, fold))
    return splits


def _rank_ic_one_minute(pred: np.ndarray, target: np.ndarray) -> float:
    """Spearman rank correlation across one cross-section."""
    mask = np.isfinite(pred) & np.isfinite(target)
    if mask.sum() < 5:
        return np.nan
    pred_rank = rankdata(pred[mask])
    target_rank = rankdata(target[mask])
    if pred_rank.std() < 1e-12 or target_rank.std() < 1e-12:
        return np.nan
    return float(np.corrcoef(pred_rank, target_rank)[0, 1])


def rank_ic_series(
    preds: np.ndarray,
    targets: np.ndarray,
    minute_index: np.ndarray,
) -> np.ndarray:
    """Per-minute rank-IC for one horizon. preds/targets are (n_rows,)."""
    out = []
    for minute_id in np.unique(minute_index):
        sel = minute_index == minute_id
        out.append(_rank_ic_one_minute(preds[sel], targets[sel]))
    return np.array(out, dtype=np.float64)


@dataclass
class HorizonMetrics:
    horizon: str
    mean_rank_ic: float
    icir: float
    n_minutes: int
    hit_rate: float  # fraction of minutes with positive rank-IC


def evaluate_horizon(
    preds: np.ndarray,
    targets: np.ndarray,
    minute_index: np.ndarray,
    horizon: str,
) -> HorizonMetrics:
    ic = rank_ic_series(preds, targets, minute_index)
    ic = ic[np.isfinite(ic)]
    mean_ic = float(np.mean(ic)) if ic.size else float("nan")
    std_ic = float(np.std(ic)) if ic.size else float("nan")
    # ICIR conventionally annualised; we report the raw per-minute IR (mean/std) for comparability.
    icir = mean_ic / std_ic if std_ic > 1e-12 else float("nan")
    hit_rate = float(np.mean(ic > 0)) if ic.size else float("nan")
    return HorizonMetrics(horizon, mean_ic, icir, int(ic.size), hit_rate)
