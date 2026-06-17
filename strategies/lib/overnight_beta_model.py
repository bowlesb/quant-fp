"""Overnight-beta portfolio model — the CERTIFIED W11 signal as a pure, testable function.

The W11 overnight-beta premium (certified on 18mo, `experiments/2026-06-16-w11-overnight-beta/`): high-beta
names earn the market risk premium OVERNIGHT (close→open), low-beta intraday. The tradeable form is a
LONG-high-beta / SHORT-low-beta quintile L/S held OVERNIGHT only (enter at the close auction, exit at the
next open auction), monthly rebalance, on the liquid universe EXCLUDING the crypto/AI speculation cohort (the
certification's confound control).

This module is the PURE signal: given a panel of recent daily returns per name + the market (SPY) returns, it
computes each name's market beta (rolling OLS) and returns the high-beta (long) and low-beta (short) leg name
sets. No I/O, no wall-clock, deterministic — unit-testable, and the container wraps it with the auction
execution + the slippage measurement that actually gates the edge.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class BetaLegs:
    """The selected L/S legs for one rebalance: the long (high-beta) and short (low-beta) name sets, plus
    each name's estimated beta (for logging / inspection)."""

    long: tuple[str, ...]
    short: tuple[str, ...]
    betas: dict[str, float]


def compute_beta(name_returns: np.ndarray, market_returns: np.ndarray) -> float:
    """Market beta = OLS slope of the name's daily returns on the market's, over the aligned window.

    Both arrays are aligned same-length daily returns (most recent ``window`` days). Returns NaN if there is
    insufficient finite data or the market has zero variance (beta undefined). Pure, no wall-clock."""
    mask = np.isfinite(name_returns) & np.isfinite(market_returns)
    if int(mask.sum()) < 20:  # need a reasonable window for a stable beta
        return float("nan")
    x = market_returns[mask]
    y = name_returns[mask]
    xm = x - x.mean()
    denom = float((xm * xm).sum())
    if denom <= 0.0:
        return float("nan")
    return float((xm * (y - y.mean())).sum() / denom)


class OvernightBetaModel:
    """Selects the high-minus-low-beta L/S legs from a recent daily-return panel. The certified W11 signal."""

    def __init__(self, beta_window: int = 60, quantile: float = 0.2) -> None:
        """``beta_window`` = trailing days for the beta OLS (60 = the certified value). ``quantile`` = the
        leg fraction (0.2 = top/bottom quintile, the certified split)."""
        self._beta_window = beta_window
        self._quantile = quantile

    @property
    def beta_window(self) -> int:
        return self._beta_window

    def select_legs(self, returns_by_name: dict[str, np.ndarray], market_returns: np.ndarray) -> BetaLegs:
        """Compute each name's beta over the trailing ``beta_window`` and return the high-beta long leg +
        low-beta short leg (each the ``quantile`` fraction of names with a finite beta).

        ``returns_by_name``: {symbol: daily-return array}, ``market_returns``: the market (SPY) daily-return
        array — all aligned to the same dates, most-recent last. Names with a non-finite beta (too little
        data) are dropped. Deterministic; ties broken by sorted symbol for reproducibility."""
        w = self._beta_window
        mkt = market_returns[-w:]
        betas: dict[str, float] = {}
        for symbol in sorted(returns_by_name):
            beta = compute_beta(returns_by_name[symbol][-w:], mkt)
            if np.isfinite(beta):
                betas[symbol] = beta
        if len(betas) < 5:
            return BetaLegs(long=(), short=(), betas=betas)
        # Sort by beta; the top quantile is the long (high-beta) leg, the bottom quantile the short.
        ranked = sorted(betas, key=lambda s: (betas[s], s))  # ascending beta; symbol tiebreak
        n_leg = max(1, int(len(ranked) * self._quantile))
        short = tuple(ranked[:n_leg])              # lowest beta
        long = tuple(ranked[-n_leg:])              # highest beta
        return BetaLegs(long=long, short=short, betas=betas)
