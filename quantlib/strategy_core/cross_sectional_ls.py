"""The cross-sectional long/short top/bottom-k decision core — Ben's EOD / multi-day / sector /
up-down-day archetypes as ONE shared mechanism (the same shape as the live
`OvernightBetaModel.select_legs`).

Written ONCE here; called by BOTH the battery backtest (over each historical panel timestamp) and a
live container (over the latest bus vectors). The signal is either a NAMED feature ranked directly
(the raw fast path — exact backtest==live parity) or an injected `RankModel` (the GBM deeper mode —
the frozen trained model is the shared object). Pure: no I/O, no wall-clock, NaN-safe.
"""
from __future__ import annotations

from typing import Protocol

import numpy as np

from quantlib.strategy_core import CrossSection, TargetPosition


class RankModel(Protocol):
    """A trained ranker that scores a whole cross-section (higher = more bullish). The battery fits it
    walk-forward and FREEZES it; the live container loads the frozen model and calls `rank` per cycle —
    the frozen model is the shared object (the backtest-only walk-forward produces it, exactly as the
    feature platform's fold orchestration is backtest-only but `emit` is shared)."""

    def rank(self, cross_section: CrossSection) -> np.ndarray:
        ...


class CrossSectionalLS:
    """Dollar-neutral EW top/bottom-`frac` L/S basket from a cross-sectional signal.

    `signal_feature` (raw fast path) ranks one named feature directly; `model` (deeper mode) ranks the
    full feature set. Exactly one is used. Sign convention: HIGHER score -> LONG. For a reversion
    signal (e.g. below-VWAP -> bullish), pass the already-signed feature or a model trained to the
    forward return, so higher score always means "expected to outperform".
    """

    def __init__(
        self,
        *,
        frac: float = 0.1,
        signal_feature: str | None = None,
        model: RankModel | None = None,
        signal_sign: float = 1.0,
    ) -> None:
        if (signal_feature is None) == (model is None):
            raise ValueError("provide exactly one of signal_feature or model")
        self._frac = frac
        self._signal_feature = signal_feature
        self._model = model
        self._signal_sign = signal_sign

    def score(self, cross_section: CrossSection) -> np.ndarray:
        """The per-name conviction the core ranks on (model rank, or the signed named feature). Public
        so the battery can rank on EXACTLY this — the same scoring a live decide() uses."""
        if self._model is not None:
            return np.asarray(self._model.rank(cross_section), dtype=float)
        assert self._signal_feature is not None
        return self._signal_sign * np.asarray(cross_section.feature(self._signal_feature), dtype=float)

    def decide(self, cross_section: CrossSection) -> list[TargetPosition]:
        score = self.score(cross_section)
        symbols = cross_section.symbols
        finite = np.where(np.isfinite(score))[0]
        if finite.size < 2:
            return []
        ordered = finite[np.argsort(score[finite])]  # ascending; most-bearish first
        k = max(1, int(self._frac * ordered.size))
        if ordered.size < 2 * k:
            return []
        shorts = ordered[:k]
        longs = ordered[-k:]
        targets: list[TargetPosition] = []
        for idx in longs:
            targets.append(TargetPosition(symbols[idx], 1.0 / k, float(score[idx])))
        for idx in shorts:
            targets.append(TargetPosition(symbols[idx], -1.0 / k, float(score[idx])))
        return targets
