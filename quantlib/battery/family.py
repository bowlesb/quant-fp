"""Family-wise multiple-comparisons defense (§6) — BY-FDR across the WHOLE battery.

Running ~40 archetype cells IS p-hacking unless defended: ~2 light up at p<0.05 on pure noise.
This lifts the PROVEN Benjamini-Yekutieli step-up (the dependent-test FDR control from the
trusted-baseline `family_correction.py`) to the battery scale — the cells share the panel and
overlap, so BY (not BH) is the correct control. The leaderboard reports only cells surviving
correction, AND each surviving cell must independently beat its own shuffle canary.
"""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass
class FamilyCorrection:
    method: str  # "benjamini_yekutieli"
    q: float  # target FDR
    c_m: float  # the dependent-test penalty c(m) = sum 1/i
    keys: list[str]  # cell keys, input order
    p_values: list[float]  # one-sided NW p per cell
    q_values: list[float]  # BY-adjusted q per cell
    reject: list[bool]  # survives correction (aligned to keys)
    pre_registered: bool  # True iff the DEFAULT battery (not a custom archetype list)


def one_sided_p_from_t(t_stat: float) -> float:
    """One-sided p (upper tail) from a t/z stat via the normal approximation — the same
    `0.5*erfc(t/sqrt2)` the family_correction.py uses (NW t is already a large-sample z)."""
    if math.isnan(t_stat):
        return 1.0
    return 0.5 * math.erfc(t_stat / math.sqrt(2.0))


def by_threshold_table(num_tests: int, q: float) -> list[float]:
    c_m = sum(1.0 / i for i in range(1, num_tests + 1))
    return [(i / (num_tests * c_m)) * q for i in range(1, num_tests + 1)]


def benjamini_yekutieli(
    keys: list[str], p_values: list[float], q: float, *, pre_registered: bool
) -> FamilyCorrection:
    """BY step-up under arbitrary dependence. Returns the reject mask + adjusted q-values aligned
    to the input key order. Verbatim algorithm from the trusted-baseline family_correction.py."""
    num_tests = len(p_values)
    if num_tests == 0:
        return FamilyCorrection("benjamini_yekutieli", q, 0.0, [], [], [], [], pre_registered)
    c_m = sum(1.0 / i for i in range(1, num_tests + 1))
    order = sorted(range(num_tests), key=lambda idx: p_values[idx])
    thresholds = by_threshold_table(num_tests, q)
    largest_k = 0
    for rank, test_idx in enumerate(order, start=1):
        if p_values[test_idx] <= thresholds[rank - 1]:
            largest_k = rank
    reject = [False] * num_tests
    for rank, test_idx in enumerate(order, start=1):
        reject[test_idx] = rank <= largest_k
    qvals = [1.0] * num_tests
    running = 1.0
    for rank in range(num_tests, 0, -1):
        test_idx = order[rank - 1]
        running = min(running, p_values[test_idx] * num_tests * c_m / rank)
        qvals[test_idx] = min(running, 1.0)
    return FamilyCorrection(
        method="benjamini_yekutieli",
        q=q,
        c_m=c_m,
        keys=list(keys),
        p_values=list(p_values),
        q_values=qvals,
        reject=reject,
        pre_registered=pre_registered,
    )
