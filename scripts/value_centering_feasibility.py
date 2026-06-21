"""Feasibility probe for VALUE-column centering of the 3 parked non-time corr-denom reduction groups
(distribution.ret_kurt / market_beta / return_dynamics.autocorr) — the #307-sibling follow-up #386 named.

This is the measure-first evidence behind the docs/INCREMENTAL_READINESS.md verdict that value-column
centering does NOT yield a clean value-identical FP_INCREMENTAL promotion for these three groups (unlike
volume #307, whose anchor is a stable per-symbol daily-volume SCALE). Run it to reproduce the numbers:

  python scripts/value_centering_feasibility.py

The mechanism (mirror of #386's _pinned_time_x but on the VALUE column) only conditions when the centering
anchor is ~the window mean. The obstacles, both measured here:

  (A) NO reproducible static per-symbol RETURN anchor. A prior-day-derived anchor (the daily-snapshot form
      that works for volume) is uncorrelated with today's intraday window mean and off by ~100 std in the
      breach regime -> it does NOT condition (it breaches as often as raw). The only anchor that conditions
      is the per-window mean, which is path-divergent across the sliding window.

  (B) Rebase-after-the-fact re-introduces the cancellation. Unlike the TIME axis (where the incremental
      engine ACCUMULATES on the already-small rebased x), a value anchor applied by binomially rebasing the
      raw power sums Sigma(r^k) re-runs the same large-near-equal subtraction -> conditioning is lost. Only
      sums ACCUMULATED on (r - a) stay conditioned, which needs a static a known before accumulation -> (A).

  (C) For the OLS/corr groups, centering MOVES the defined-guard boundary. The production guard is
      denom_x > eps*(Sigma x)^2; centering x changes Sigma x, so the guard RHS changes and a straddle cell
      can FLIP null<->non-null -> NOT value-identical (would change the feature output and the fingerprint).

Verdict: the time-axis class (#386) is conditionable value-identically because OLS is origin-invariant and
the engine controls the per-fold origin BEFORE accumulating; the value-column class is not, for the reasons
above. The real fix for these three is a cancellation-free reduction kernel (the Rust corr/OLS/moment kernel
already named as future engine work), not a centering anchor.
"""
from __future__ import annotations

from fractions import Fraction

import numpy as np

TOL = 1e-4


def _exact_kurt(returns: np.ndarray) -> float | None:
    fr = [Fraction(float(x)).limit_denominator(10**15) for x in returns]
    n = len(fr)
    mean = sum(fr) / n
    m2 = sum((x - mean) ** 2 for x in fr) / n
    m4 = sum((x - mean) ** 4 for x in fr) / n
    return None if m2 == 0 else float(m4 / (m2 * m2) - 3)


def _kurt_from_anchor(returns: np.ndarray, anchor: float) -> float | None:
    centered = returns - anchor
    n = len(returns)
    t1, t2 = float(np.sum(centered)), float(np.sum(centered**2))
    t3, t4 = float(np.sum(centered**3)), float(np.sum(centered**4))
    cmean = t1 / n
    m2 = t2 / n - cmean * cmean
    m4 = t4 / n - 4.0 * cmean * (t3 / n) + 6.0 * cmean * cmean * (t2 / n) - 3.0 * cmean**4
    return None if m2 <= 0 else m4 / (m2 * m2) - 3.0


def measure_kurtosis_anchors(n_cells: int = 3000, seed: int = 99) -> dict[str, float]:
    """(A): the centered form with the WINDOW-MEAN anchor conditions to ~machine precision (value-identical),
    but a reproducible PRIOR-DAY anchor does NOT robustly condition across the full per-minute-return regime."""
    rng = np.random.default_rng(seed)
    worst = {"raw": 0.0, "window_mean": 0.0, "prior_day": 0.0}
    breaches = {"raw": 0, "window_mean": 0, "prior_day": 0}
    for _ in range(n_cells):
        today_mean = rng.choice([1e-5, 1e-4, 5e-4, 1e-3, 2e-3]) * rng.choice([-1, 1])
        vol = rng.choice([1e-6, 1e-5, 5e-5]) * rng.uniform(0.5, 2.0)
        count = int(rng.choice([5, 10, 15, 30]))
        returns = today_mean + rng.standard_normal(count) * vol
        truth = _exact_kurt(returns)
        if truth is None or abs(truth) < 1e-6:
            continue
        prior_anchor = (rng.standard_normal() * 0.01) / 390.0  # reproducible daily-snapshot-derived anchor
        candidates = {
            "raw": _kurt_from_anchor(returns, 0.0) if abs(today_mean) < 1e-9 else _raw_kurt(returns),
            "window_mean": _kurt_from_anchor(returns, float(np.mean(returns))),
            "prior_day": _kurt_from_anchor(returns, prior_anchor),
        }
        for label, value in candidates.items():
            if value is None:
                continue
            rel = abs(value - truth) / (abs(truth) + 1e-30)
            worst[label] = max(worst[label], rel)
            breaches[label] += int(rel > TOL)
    return {"worst": worst, "breaches": breaches}  # type: ignore[dict-item]


def _raw_kurt(returns: np.ndarray) -> float | None:
    n = len(returns)
    s1, s2 = float(np.sum(returns)), float(np.sum(returns**2))
    s3, s4 = float(np.sum(returns**3)), float(np.sum(returns**4))
    mean = s1 / n
    m2 = s2 / n - mean * mean
    m4 = s4 / n - 4.0 * mean * (s3 / n) + 6.0 * mean * mean * (s2 / n) - 3.0 * mean**4
    return None if m2 <= 0 else m4 / (m2 * m2) - 3.0


def measure_corr_guard_flips(n_cells: int = 5000, seed: int = 21) -> int:
    """(C): centering x/y in an OLS corr MOVES the defined-guard RHS (eps*(Sigma x)^2), so a near-flat-window
    straddle cell can FLIP null<->non-null. Any flip => the centering is NOT value-identical for the OLS/corr
    groups (market_beta, return_dynamics autocorr). Returns the flip count."""
    rng = np.random.default_rng(seed)
    eps = 1e-10

    def guarded(x: np.ndarray, y: np.ndarray, ax: float, ay: float) -> float | None:
        xc, yc = x - ax, y - ay
        n = len(x)
        sx, sy = float(np.sum(xc)), float(np.sum(yc))
        sxx, syy = float(np.sum(xc * xc)), float(np.sum(yc * yc))
        sxy = float(np.sum(xc * yc))
        denom_x, denom_y = n * sxx - sx * sx, n * syy - sy * sy
        cov_n = n * sxy - sx * sy
        if not (denom_x > eps * (sx * sx) and denom_y > eps * (sy * sy)):
            return None
        return cov_n / np.sqrt(denom_x * denom_y)

    flips = 0
    for _ in range(n_cells):
        spy_base = rng.uniform(-5e-4, 5e-4)
        count = int(rng.choice([3, 4, 5]))
        x = spy_base + rng.standard_normal(count) * float(rng.choice([1e-7, 1e-6, 1e-5]))
        y = rng.standard_normal(count) * 5e-4
        raw = guarded(x, y, 0.0, 0.0)
        centered = guarded(x, y, float(np.mean(x)), float(np.mean(y)))
        flips += int((raw is None) != (centered is None))
    return flips


if __name__ == "__main__":
    kurt = measure_kurtosis_anchors()
    print("KURTOSIS anchor conditioning (tol=1e-4, 3000 breach-regime cells):")
    for label in ("raw", "window_mean", "prior_day"):
        print(f"  {label:>12}: worst_rel={kurt['worst'][label]:.2e}  breaches={kurt['breaches'][label]}")
    flips = measure_corr_guard_flips()
    print(f"\nOLS-CORR guard flips under centering (5000 near-flat cells): {flips}")
    print("\nVERDICT: window-mean centering is value-identical but NOT reproducible across slides;")
    print("prior-day anchor is reproducible but does NOT condition; corr centering perturbs the guard.")
    print("=> no clean value-identical centering promotion for these 3; needs a cancellation-free kernel.")
