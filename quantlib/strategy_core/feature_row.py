"""`FeatureRow` — the minimal, bus-free protocol a single-name decision core reads.

A per-vector decision core (`Model.predict(row)`) needs only to read a named feature off ONE
(symbol, minute) row, plus the row's identity (symbol/minute). `FeatureRow` is exactly that surface,
so the cores live in `quantlib/strategy_core/` WITHOUT importing the bus (`quantlib.bus.vector`) —
keeping the core bus-free (no redis pulled into the battery). The live `FeatureVector`
(`quantlib.bus.vector`) structurally satisfies this protocol (it has `.symbol`, `.minute`,
`.value(name)`), so a live container passes its real decoded vector unchanged.
"""
from __future__ import annotations

import datetime as dt
from typing import Protocol, runtime_checkable


@runtime_checkable
class FeatureRow(Protocol):
    """One (symbol, minute) feature row — the bus-free surface a single-name `predict` consumes."""

    symbol: str
    minute: dt.datetime

    def value(self, name: str) -> float:
        """The named feature's value on this row (NaN where absent)."""
        ...
