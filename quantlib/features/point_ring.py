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

It is held alongside the running sums in ``IncrementalEngine``: ``seed`` folds every buffered minute (==
backfill over the buffer), then each live ``step`` ``fold``s the one new minute and ``_latest_frame`` reads
the points from the ring instead of ``resolve_points``. Armed by ``FP_POINT_RING=1`` (default OFF =
``resolve_points`` path); byte-identical either way (tests/test_fp_point_ring.py).
"""
from __future__ import annotations

import json
from dataclasses import dataclass

import numpy as np
import polars as pl

from quantlib.features.declarative import ReductionGroup
from quantlib.features.slice_derive import lag_specs, rewrite_global


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


def point_specs(groups: list[ReductionGroup]) -> list[PointSpec]:
    """Every ``(source, lag)`` the groups' point exprs reference — the columns the ring must carry. Each
    ``col(c).shift(k).over("symbol")`` contributes ``(c, k)``; each at-T leaf column contributes ``(c, 0)``.
    Collected via the SAME ``lag_specs`` machinery the engine uses for value columns, so the ring carries
    exactly what ``rewrite_global`` will read back as ``__lag{k}_{c}``. Deduped across groups."""
    exprs = [expr for group in groups for expr in group.points().values()]
    lags, _ = lag_specs(exprs)  # the (column, k>0) shift pairs
    pairs: set[tuple[str, int]] = set(lags)
    for expr in exprs:
        for column in leaf_columns(expr):
            pairs.add((column, 0))  # at-T read of every leaf column (also the shift's base column)
    return [PointSpec(alias=f"{source}@{lag}", source=source, lag=lag) for source, lag in sorted(pairs)]


def point_frame_from_ring(
    groups: list[ReductionGroup], ring: PointRing, symbols: list[str], latest: object
) -> pl.DataFrame:
    """Reconstruct ``resolve_points``'s latest-row frame (``symbol`` + each ``__pt_<name>``) from the carried
    ring. Builds a one-row-per-symbol frame whose columns are ``__lag{k}_{c}`` (read from the ring), then
    evaluates ``rewrite_global(point_expr)`` over it — the SAME point expr ``resolve_points`` runs, with the
    ``shift(k).over`` swapped for the carried lag column, so the result is byte-identical by construction (the
    arithmetic is unchanged; only the lag SOURCE is the O(1) ring instead of a whole-buffer pass)."""
    columns: dict[str, np.ndarray] = {"symbol": np.array(symbols, dtype=object)}
    seen: set[str] = set()
    point_exprs: dict[str, pl.Expr] = {}
    for group in groups:
        for name, expr in group.points().items():
            # rewrite_global swaps only the shift().over() subtrees -> __lag{k}_{c}; a plain at-T col(c) stays
            # col(c). So carry the at-T value under its PLAIN name ``c`` and each positive lag under ``__lag{k}_{c}``.
            point_exprs.setdefault(f"__pt_{name}", rewrite_global(expr))
            for source in leaf_columns(expr):
                if source not in seen:
                    seen.add(source)
                    columns[source] = ring.at_t(source)  # plain at-T column the un-shifted leaves read
            for column, lag in lag_specs([expr])[0]:
                key = f"__lag{lag}_{column}"
                if key not in seen:
                    seen.add(key)
                    columns[key] = ring.lag(column, lag)
    frame = pl.DataFrame(columns)
    return frame.select(["symbol", *[expr.alias(name) for name, expr in point_exprs.items()]]).with_columns(
        pl.lit(latest).alias("minute")
    )


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
        if not self.sources:
            return  # no point columns to carry (a group set with no points) — the ring is vacuous
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
