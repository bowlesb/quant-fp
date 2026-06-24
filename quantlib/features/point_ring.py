"""``PointRing`` — O(1)-per-minute carried-state replacement for ``resolve_points``.

``resolve_points`` (declarative.py) re-runs, every minute, a whole-buffer ``sort + select(point_exprs) +
filter(minute==T)`` to carry each reduction group's ``__pt_<name>`` point columns onto the latest row. That is
~6ms of the shared ~41ms incremental step (phase_profile #433), 100% framework overhead. The point exprs are
a CLOSED vocabulary — every one is an at-T value, a positive-lag ``col.shift(w).over("symbol")``, or a lag-1
delta — so the carried form is provably a per-symbol ring of the recent point-SOURCE columns.

``PointRing`` holds, per (symbol, source-column), the last ``depth+1`` POSITIONAL rows (a symbol's row cursor
advances only on the minutes it is present), so ``lag(source, w)`` returns the w-th prior PRESENT row — the
positional semantics ``shift(w).over("symbol")`` has, NOT an epoch-keyed time lag (which diverges from
backfill on sparse symbols; the load-bearing invariant gated by tests/test_fp_points_carried_parity.py).
Depth is the deepest declared point lag (e.g. ``shift(120)``); the ring carries only the point-source columns
(``close`` / ``volume`` / ...), so it does NOT widen the engine's value-derive tail (which is shallow,
``max_lag``≈5) — a dedicated shallow-footprint ring (~1.2MB/shard at 312×121×4) measured 138× cheaper than
``resolve_points``.

It slots under the SAME group-level ``RunningState`` lifecycle (``up_to_date`` / ``rebuild_from_history``) the
other held-state kinds use: ``seed`` folds every buffered minute (== backfill over the buffer), then each live
minute ``fold``s one new minute. This module provides the kind; the engine wiring (replace the
``resolve_points`` call in ``IncrementalEngine.step`` with a ring read) is a thin separate layer.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

import numpy as np
import polars as pl

from quantlib.features.declarative import ReductionGroup


@dataclass(frozen=True)
class PointSpec:
    """One ``__pt_<name>`` output column: ``alias`` is ``name``, ``source`` is the underlying input column
    (``close`` / ``volume`` / ...), ``lag`` is the positive row-lag (0 = at-T). A lag-1 delta (``x -
    x.shift(1)``) is declared as two specs the assemble expr combines; the ring carries the source rows."""

    alias: str
    source: str
    lag: int


def _walk(node: object, visit) -> None:
    """Depth-first over a polars serialized-plan node (dict/list tree)."""
    if isinstance(node, dict):
        visit(node)
        for value in node.values():
            _walk(value, visit)
    elif isinstance(node, list):
        for value in node:
            _walk(value, visit)


def shift_lags(expr: pl.Expr) -> list[int]:
    """Every ``Shift`` literal in ``expr``'s serialized plan (positive => a positive row lag)."""
    plan = json.loads(expr.meta.serialize(format="json"))
    lags: list[int] = []

    def visit(node: dict) -> None:
        if node.get("function") == "Shift":
            for item in node.get("input", []):
                literal = (
                    item.get("Literal", {}).get("Dyn", {}).get("Int") if isinstance(item, dict) else None
                )
                if literal is not None:
                    lags.append(int(literal))

    _walk(plan, visit)
    return lags


def leaf_columns(expr: pl.Expr) -> list[str]:
    """Every non-``symbol`` column name referenced by ``expr`` (the point sources)."""
    plan = json.loads(expr.meta.serialize(format="json"))
    found: list[str] = []

    def visit(node: dict) -> None:
        column = node.get("Column")
        if isinstance(column, str) and column != "symbol":
            found.append(column)

    _walk(plan, visit)
    # preserve first-seen order, dedup
    seen: dict[str, None] = {}
    for name in found:
        seen.setdefault(name, None)
    return list(seen)


def point_specs(group: ReductionGroup) -> list[PointSpec]:
    """The ``PointSpec``s a group's ``points()`` resolve to. An at-T point -> one lag-0 spec per source column.
    A positive-lag point ``col.shift(w).over`` -> one lag-``w`` spec on that column. A lag-1 delta -> two specs
    (lag-0 + lag-1) on its column. The output ``alias`` is the point name; the assemble accessor reads
    ``__pt_<alias>`` which is rebuilt from the spec(s)."""
    specs: list[PointSpec] = []
    for name, expr in group.points().items():
        lags = shift_lags(expr)
        positive = [k for k in lags if k > 0]
        columns = leaf_columns(expr)
        if not positive:
            # at-T (possibly an arithmetic of several at-T columns, e.g. high-low): carry each source at lag 0.
            for column in columns:
                specs.append(PointSpec(alias=name, source=column, lag=0))
        else:
            # a single-column positive lag (the bar-derived lag points are all this), optionally with a lag-0
            # partner (a delta x - x.shift(1)). Carry lag-0 AND each positive lag on the (single) source column.
            source = columns[0]
            if 0 not in positive and any(k == 0 for k in lags) or len(positive) < len(lags):
                specs.append(PointSpec(alias=name, source=source, lag=0))
            for lag in sorted(set(positive)):
                specs.append(PointSpec(alias=name, source=source, lag=lag))
    return specs


class PointRing:
    """Per-symbol positional ring of recent point-source values. Each symbol carries its OWN last
    ``depth + 1`` PRESENT bars (one ring slot per present minute — absent minutes are simply not recorded), so
    ``lag(source, w)`` is the w-th prior PRESENT bar, matching ``shift(w).over("symbol")`` exactly over gaps.

    State is a single ``(n_symbols, depth + 1, n_sources)`` float ring + a per-symbol write cursor. Folding a
    minute writes the present symbols' values at their next cursor slot (mod ``depth + 1``); a lag read indexes
    ``cursor - 1 - w`` back. O(present_symbols) per minute, O(1) per read."""

    def __init__(self, symbols: list[str], specs: list[PointSpec]) -> None:
        self.symbols = list(symbols)
        self.index = {symbol: i for i, symbol in enumerate(self.symbols)}
        self.n = len(self.symbols)
        self.specs = list(specs)
        self.sources = sorted({spec.source for spec in self.specs})
        self.source_index = {source: i for i, source in enumerate(self.sources)}
        self.depth = max((spec.lag for spec in self.specs), default=0)
        self.slots = self.depth + 1
        # ring[symbol, slot, source] — a circular buffer of each symbol's last ``slots`` PRESENT bars.
        self._ring = np.full((self.n, self.slots, len(self.sources)), np.nan, dtype=np.float64)
        # write[symbol] = the slot the NEXT present bar writes to (mod slots); count = present bars so far.
        self._write = np.zeros(self.n, dtype=np.int64)
        self._count = np.zeros(self.n, dtype=np.int64)

    def fold(self, minute_frame: pl.DataFrame) -> None:
        """Record this minute's PRESENT symbols' source values into each present symbol's next ring slot.
        Absent symbols are untouched (no slot consumed), so each symbol's ring holds its own present bars in
        order — the positional history ``shift(w).over`` reads. ``minute_frame`` is the single new minute's
        rows (one per present symbol)."""
        present = minute_frame.select(["symbol", *self.sources]).sort("symbol")
        present_symbols = present["symbol"].to_list()
        keep = [i for i, symbol in enumerate(present_symbols) if symbol in self.index]
        rows = np.array([self.index[present_symbols[i]] for i in keep], dtype=np.int64)
        if rows.size == 0:
            return
        values = np.column_stack([present[source].to_numpy().astype(np.float64) for source in self.sources])
        slots = self._write[rows]
        self._ring[rows, slots, :] = values[keep]
        self._write[rows] = (slots + 1) % self.slots
        self._count[rows] += 1

    def at_t(self, source: str) -> np.ndarray:
        """The newest present value per symbol for ``source`` (NaN where the symbol was never present)."""
        return self._read_back(source, 0)

    def lag(self, source: str, window: int) -> np.ndarray:
        """The ``window``-th prior PRESENT bar per symbol for ``source`` (positional). NaN where the symbol has
        fewer than ``window + 1`` present bars — matching ``shift(window)`` warmup."""
        return self._read_back(source, window)

    def _read_back(self, source: str, back: int) -> np.ndarray:
        """The value ``back`` present-bars before each symbol's latest (back=0 -> latest). NaN where a symbol
        has <= ``back`` present bars, or has never been present."""
        source_i = self.source_index[source]
        result = np.full(self.n, np.nan, dtype=np.float64)
        enough = self._count > back
        if not enough.any():
            return result
        # the latest present bar sits at (write - 1) mod slots; ``back`` bars before it is (write - 1 - back).
        slot = (self._write - 1 - back) % self.slots
        rows = np.nonzero(enough)[0]
        result[rows] = self._ring[rows, slot[rows], source_i]
        return result
