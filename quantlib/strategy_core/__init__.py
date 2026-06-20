"""The SHARED strategy decision core — the ONE place a strategy's signal→intent logic lives, so the
BATTERY (backtest) and a LIVE CONTAINER execute the SAME decision with no re-implementation.

This is the strategy-layer analogue of the platform's parity-by-construction: features compute
identically live + backfill via a shared `emit`; strategies decide identically live + backtest via a
shared `DecisionCore.decide`. The ONLY difference between the two is the execution harness — panel
iteration vs bus subscription + broker orders — never the decision logic.

See `docs/STRATEGY_BATTERY_PORTABILITY.md` for the verification + design. The existing live containers
(`strategies/lib/`: VwapReversionModel.predict, OvernightBetaModel.select_legs) already follow this
shape; this module formalizes the contract both sides import.

A `DecisionCore` is PURE: no bus, no broker, no DB, no wall-clock. It reads features BY NAME off a
source-agnostic `CrossSection` view and returns target positions. The battery wraps a historical panel
slice as a `CrossSection`; a live container wraps the latest bus vectors as a `CrossSection`. Same
`decide`, both paths.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

import numpy as np


@dataclass(frozen=True)
class TargetPosition:
    """One target holding the decision core wants. `target_weight` is a dollar-neutral book weight in
    [-1, 1] (the L/S basket leg) or a notional sign (single-name). `score` is the model's conviction
    (rank / probability) — for sizing + logging. The battery books these as the L/S P&L; the live
    container diffs them against held positions and places the broker orders."""

    symbol: str
    target_weight: float
    score: float


@runtime_checkable
class CrossSection(Protocol):
    """The as-of-t cross-section a decision reads — source-agnostic so the SAME `decide` runs over a
    historical panel slice (battery) and the latest bus vectors (live). All feature reads are BY NAME
    (the invariant that makes backtest==live) and NaN-safe."""

    symbols: list[str]
    minute: dt.datetime

    def feature(self, name: str) -> np.ndarray:
        """Per-name values for one named feature, aligned to `symbols` (NaN where absent)."""
        ...

    def feature_for(self, symbol: str, name: str) -> float:
        """One name's value for one feature (NaN where absent)."""
        ...


class DecisionCore(Protocol):
    """The shared signal→intent contract. PURE — no I/O, no wall-clock. Called once per timestamp by
    the battery and once per cycle by the live container, identical code path."""

    def decide(self, cross_section: CrossSection) -> list[TargetPosition]:
        """Given the as-of-t cross-section, return the target book."""
        ...
