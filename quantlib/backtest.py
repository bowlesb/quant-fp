"""Walk-forward backtest harness mechanics — the leakage-catching core, kept pure
(stdlib only) and model-pluggable so it's tested without LightGBM. The real runner
passes a LightGBM fit/predict as `model_fn`; tests pass a trivial stub.

Design (per review): purge/embargo by the LABEL HORIZON in market time (not bar
count); cross-sectional rank-IC computed WITHIN each timestamp then averaged (never
pooled); shuffle-label canary permutes WITHIN each timestamp group; significance via
an autocorrelation-aware (Newey-West) t-stat on the per-timestamp IC series, because
overlapping horizons make naive t-stats 2-3x too confident.
"""
import math
import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass
class Fold:
    train_idx: list[int]
    test_idx: list[int]


def walk_forward_folds(
    row_ts: list[datetime], horizon_minutes: int, n_folds: int
) -> list[Fold]:
    """Expanding-window folds over unique timestamps. A training row is PURGED if
    its label window (ts + horizon) reaches into or past the test block start, so
    no training label peeks at test-period data."""
    unique = sorted(set(row_ts))
    if len(unique) < n_folds + 1:
        raise ValueError("not enough distinct timestamps for the requested folds")
    # split unique timestamps into n_folds+1 contiguous segments by position
    size = len(unique) / (n_folds + 1)
    segments = [unique[round(i * size):round((i + 1) * size)] for i in range(n_folds + 1)]

    folds: list[Fold] = []
    for k in range(1, n_folds + 1):
        test_ts = set(segments[k])
        test_start = min(segments[k])
        cutoff = test_start - timedelta(minutes=horizon_minutes)
        train_ts = {t for seg in segments[:k] for t in seg if t <= cutoff}
        train_idx = [i for i, t in enumerate(row_ts) if t in train_ts]
        test_idx = [i for i, t in enumerate(row_ts) if t in test_ts]
        folds.append(Fold(train_idx=train_idx, test_idx=test_idx))
    return folds


def _rank(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg_rank = (i + j) / 2.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg_rank
        i = j + 1
    return ranks


def _pearson(xs: list[float], ys: list[float]) -> float:
    n = len(xs)
    if n < 2:
        return math.nan
    mx, my = statistics.mean(xs), statistics.mean(ys)
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    sy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if sx == 0 or sy == 0:
        return math.nan
    return cov / (sx * sy)


def _spearman(xs: list[float], ys: list[float]) -> float:
    return _pearson(_rank(xs), _rank(ys))


def per_timestamp_ic(
    pred: list[float], label: list[float], group: list[datetime], min_names: int = 5
) -> dict[datetime, float]:
    """Spearman rank-IC within each timestamp's cross-section. Groups with too few
    names or with non-finite values are skipped."""
    buckets: dict[datetime, list[tuple[float, float]]] = defaultdict(list)
    for p, l, g in zip(pred, label, group):
        if not (math.isnan(p) or math.isnan(l)):
            buckets[g].append((p, l))
    ics: dict[datetime, float] = {}
    for ts, pairs in buckets.items():
        if len(pairs) < min_names:
            continue
        ic = _spearman([p for p, _ in pairs], [l for _, l in pairs])
        if not math.isnan(ic):
            ics[ts] = ic
    return ics


def mean_ic(ics: dict[datetime, float]) -> float:
    return statistics.mean(ics.values()) if ics else math.nan


def newey_west_tstat(ics: dict[datetime, float], lag: int) -> float:
    """t-stat of the per-timestamp IC series with a Newey-West correction up to
    `lag` (set lag to the label-overlap length so overlapping labels don't inflate
    significance)."""
    series = [ics[ts] for ts in sorted(ics)]
    n = len(series)
    if n < 3:
        return math.nan
    mean = statistics.mean(series)
    demeaned = [x - mean for x in series]
    gamma0 = sum(v * v for v in demeaned) / n
    var = gamma0
    for k in range(1, min(lag, n - 1) + 1):
        weight = 1.0 - k / (lag + 1)
        gamma_k = sum(demeaned[t] * demeaned[t - k] for t in range(k, n)) / n
        var += 2.0 * weight * gamma_k
    if var <= 0:
        return math.nan
    long_run_se = math.sqrt(var / n)
    return mean / long_run_se if long_run_se > 0 else math.nan


def shuffle_within_groups(
    label: list[float], group: list[datetime], seed: int
) -> list[float]:
    """Permute labels WITHIN each timestamp group (preserves the cross-section
    structure) — the leakage canary: a correct harness finds ~0 IC on this."""
    rng = _Lcg(seed)
    by_group: dict[datetime, list[int]] = defaultdict(list)
    for i, g in enumerate(group):
        by_group[g].append(i)
    shuffled = list(label)
    for indices in by_group.values():
        vals = [label[i] for i in indices]
        for i in range(len(vals) - 1, 0, -1):
            j = rng.randint(i + 1)
            vals[i], vals[j] = vals[j], vals[i]
        for pos, i in enumerate(indices):
            shuffled[i] = vals[pos]
    return shuffled


class _Lcg:
    """Tiny deterministic RNG (stdlib `random` avoided so results are portable)."""

    def __init__(self, seed: int) -> None:
        self.state = (seed * 2862933555777941757 + 3037000493) & ((1 << 64) - 1)

    def randint(self, bound: int) -> int:
        self.state = (self.state * 2862933555777941757 + 3037000493) & ((1 << 64) - 1)
        return self.state % bound
