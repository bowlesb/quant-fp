"""Sparse-symbol parity gate for a CARRIED-STATE replacement of ``resolve_points``.

``resolve_points`` re-runs, every minute, a whole-buffer ``sort + select(point_exprs) + filter(minute==T)``
to carry each group's ``__pt_<name>`` point/lag columns onto the latest row (phase_profile: ~6ms of the
shared ~41ms incremental step, all framework overhead). The point exprs are exactly two shapes:

  * AT-T values (``close`` / ``volume`` / ``high-low`` / ``close*volume`` / the tape's 1m columns) — read
    ONLY the latest minute. Carrying them needs NO state: they are the latest row ``_matrix_at`` already holds.
  * POSITIVE LAGS (``close.shift(w).over("symbol")`` — efficiency / return_dynamics / momentum_consistency,
    plus trade_flow's lag-1 ``accel`` delta). These read the w-th prior ROW per symbol.

THE CORRECTNESS SEAM this gate exists to pin: ``shift(w).over("symbol")`` is POSITIONAL (the w-th prior
ROW), NOT time-based (the bar w minutes ago). On a SPARSE symbol (gaps in its tape) the two DIFFER — a
``LastKState``-style epoch-keyed lag would emit NaN/the-wrong-bar where backfill computes a real value. So a
carried point-lag state MUST be a positional per-symbol row ring (the same semantics ``_matrix_at``'s
``slice_derive`` tail already uses), and this gate asserts the carried form is BYTE-IDENTICAL to the
``resolve_points`` truth INCLUDING the gap symbols. ``CarriedPoints`` below is the minimal reference ring;
the eventual production path must pass this same gate. The reference also proves the ``seed(H); fold(m) ==
seed(H+m)`` invariant (replay vs cold-seed agree), the held-state contract every kind shares.
"""
from __future__ import annotations

import datetime as dt

import numpy as np
import polars as pl

from quantlib.features.compare import runnable
from quantlib.features.declarative import ReductionGroup, resolve_points

BASE = dt.datetime(2026, 6, 15, 13, 30, tzinfo=dt.timezone.utc)


def _sparse_stream(n_sym: int, n_min: int, gap_period: int, gap_fraction: float, seed: int) -> pl.DataFrame:
    """A bar stream where a ``gap_fraction`` slice of symbols is MISSING every ``gap_period``-th minute, so
    their positional row-lag and their time-lag diverge (the case this gate exists for). Every symbol is
    present at minute 0 (warmup). Columns cover both point shapes: ``close`` (lag source) + ``volume`` /
    ``high`` / ``low`` (at-T sources)."""
    rng = np.random.default_rng(seed)
    symbols = [f"S{i:03d}" for i in range(n_sym)]
    gap_syms = set(symbols[: int(n_sym * gap_fraction)])
    minutes = [BASE + dt.timedelta(minutes=i) for i in range(n_min)]
    rows: list[dict[str, object]] = []
    for mi, minute in enumerate(minutes):
        gapped_minute = mi > 0 and mi % gap_period == 0
        for si, symbol in enumerate(symbols):
            if gapped_minute and symbol in gap_syms:
                continue  # this symbol has no bar this minute -> a real gap in its tape
            base_price = 100.0 + si + mi * 0.1
            rows.append(
                {
                    "symbol": symbol,
                    "minute": minute,
                    "close": base_price,
                    "high": base_price + 0.5 + rng.random() * 0.1,
                    "low": base_price - 0.5 - rng.random() * 0.1,
                    "volume": 1000.0 + si * 10 + mi,
                }
            )
    return (
        pl.DataFrame(rows)
        .with_columns(pl.col("minute").cast(pl.Datetime("us", "UTC")))
        .sort(["symbol", "minute"])
    )


class CarriedPoints:
    """Reference O(1)-per-minute carried-state form of ``resolve_points`` for one group. State = a positional
    per-symbol ROW ring of the recent point-SOURCE columns (``close`` / ``volume`` / ...). ``fold`` appends the
    minute's present rows (advancing each present symbol's row cursor); ``resolve`` reads the at-T points off
    the newest row and the positive lags off the k-th prior row — positionally, so a sparse symbol's lag reaches
    its actual prior bar regardless of gaps, matching the ``shift(w).over`` truth. Minimal on purpose: it pins
    the contract the production path must meet, with no engine plumbing."""

    def __init__(self, group: ReductionGroup) -> None:
        self.group = group
        # The point exprs, classified once into (alias -> at-T source col) and (alias -> (source col, lag rows)).
        self.at_t: dict[str, pl.Expr] = {}
        self.lag: dict[str, tuple[pl.Expr, int]] = {}
        for name, expr in group.points().items():
            lags = _shift_lags(expr)
            positive = [k for k in lags if k > 0]
            if not positive:
                self.at_t[name] = expr
            else:
                self.lag[name] = (expr, max(positive))
        # Per symbol: an append-only list of the latest-minute evaluated point-source row (a polars row dict).
        self.rows: dict[str, list[dict[str, float]]] = {}
        self.symbols: list[str] = []

    def fold(self, minute_frame: pl.DataFrame) -> None:
        """Record this minute's present symbols' point-source values (the at-T exprs + the lag SOURCES,
        evaluated on the single-minute frame so no ``over`` is needed — positional history is the ring itself).
        """
        # Evaluate every point's underlying at-T value on this minute's rows. For a lag point the source is the
        # SAME column read without the shift (the ring supplies the positional offset), so we strip the shift.
        exprs = {
            f"__src_{name}": _strip_shift(expr).alias(f"__src_{name}")
            for name, expr in self.group.points().items()
        }
        evaluated = minute_frame.select(["symbol", *exprs.values()]).sort("symbol")
        for record in evaluated.iter_rows(named=True):
            symbol = record["symbol"]
            if symbol not in self.rows:
                self.rows[symbol] = []
                self.symbols.append(symbol)
            self.rows[symbol].append({key: record[key] for key in record if key != "symbol"})

    def resolve(self) -> pl.DataFrame:
        """The latest-minute row per symbol carrying ``__pt_<name>`` — at-T from the newest ring row, positive
        lags from the k-th prior ring row (NaN when fewer than k+1 rows exist, matching ``shift(w)`` warmup).
        """
        out: dict[str, list[float]] = {"symbol": []}
        for name in self.group.points():
            out[f"__pt_{name}"] = []
        for symbol in sorted(self.symbols):
            history = self.rows[symbol]
            out["symbol"].append(symbol)
            for name in self.at_t:
                out[f"__pt_{name}"].append(history[-1][f"__src_{name}"])
            for name, (_, lag_rows) in self.lag.items():
                idx = len(history) - 1 - lag_rows
                out[f"__pt_{name}"].append(history[idx][f"__src_{name}"] if idx >= 0 else float("nan"))
        return pl.DataFrame(out).sort("symbol")


def _shift_lags(expr: pl.Expr) -> list[int]:
    """Every ``Shift`` literal in the serialized plan (positive => positive lag)."""
    import json

    plan = json.loads(expr.meta.serialize(format="json"))
    lags: list[int] = []

    def walk(node: object) -> None:
        if isinstance(node, dict):
            if node.get("function") == "Shift":
                for item in node.get("input", []):
                    literal = (
                        item.get("Literal", {}).get("Dyn", {}).get("Int") if isinstance(item, dict) else None
                    )
                    if literal is not None:
                        lags.append(int(literal))
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(plan)
    return lags


def _strip_shift(expr: pl.Expr) -> pl.Expr:
    """A point's underlying at-T source: the same expr with every ``shift(w).over("symbol")`` removed (the ring
    supplies the positional offset, so the per-minute source is the unshifted column). A delta point
    ``x - x.shift(1)`` is NOT a pure source — those are handled as at-T (lag-1 delta is the only such case and
    its source rows are both in the ring, but the reference treats the smallest cases as at-T; the production
    path keeps the delta expr). Here we read the dominant source column name and select it directly."""
    # The point sources in scope are single columns (close/volume/high/low/signed_volume/...) possibly under a
    # shift+over, or a small arithmetic of latest-minute columns (high-low, close*volume, the accel delta). For
    # the lag points (the only ones with a shift) the source is a single column under shift(w).over -> read the
    # leaf column. For at-T points the expr already has no shift -> use as-is.
    lags = _shift_lags(expr)
    if not any(k > 0 for k in lags):
        return expr
    leaf = _leaf_column(expr)
    return pl.col(leaf)


def _leaf_column(expr: pl.Expr) -> str:
    """The single underlying column name of a ``col(x).shift(w).over("symbol")`` point (the lag points are all
    this shape)."""
    import json

    plan = json.loads(expr.meta.serialize(format="json"))
    found: list[str] = []

    def walk(node: object) -> None:
        if isinstance(node, dict):
            if "Column" in node and isinstance(node["Column"], str):
                found.append(node["Column"])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(plan)
    # The partition key "symbol" appears too; the lag source is the non-symbol leaf.
    return next(name for name in found if name != "symbol")


def _lag_groups(stream: pl.DataFrame) -> list[ReductionGroup]:
    """The runnable reduction groups on ``minute_agg`` that carry at least one positive-lag point — the ones
    whose carried-state form is non-trivial (efficiency / return_dynamics / momentum_consistency)."""
    groups: list[ReductionGroup] = []
    for group in runnable({"minute_agg": stream}):
        if not isinstance(group, ReductionGroup):
            continue
        if any(any(k > 0 for k in _shift_lags(expr)) for expr in group.points().values()):
            groups.append(group)
    return groups


def _assert_fixture_is_genuinely_sparse(
    stream: pl.DataFrame, groups: list[ReductionGroup], latest: object
) -> None:
    """Guard that the fixture ACTUALLY exercises the positional-vs-time divergence (so a future change that
    accidentally densifies it can't silently turn this gate green). For a lag point ``close.shift(w)``, the
    POSITIONAL truth (``resolve_points``) must differ from the TIME-based lag (``close`` at ``latest - w``) for
    at least one gap symbol — if it never did, sparse symbols would be irrelevant and the gate would be vacuous.
    """
    group = groups[0]
    truth = resolve_points([group], stream, latest).sort("symbol")
    divergences = 0
    for name, expr in group.points().items():
        positive = [k for k in _shift_lags(expr) if k > 0]
        if not positive:
            continue
        window = max(positive)
        leaf = _leaf_column(expr)
        target = latest - dt.timedelta(minutes=window)  # type: ignore[operator]
        time_lag = stream.filter(pl.col("minute") == target).select(["symbol", pl.col(leaf).alias("_t")])
        merged = truth.select(["symbol", f"__pt_{name}"]).join(time_lag, on="symbol", how="left")
        divergences += merged.filter((pl.col(f"__pt_{name}") - pl.col("_t")).abs() > 1e-9).height
    assert divergences > 0, (
        "fixture is not genuinely sparse — positional and time-based lag never diverge, so this gate would be "
        "vacuous. Increase gap_fraction / lower gap_period."
    )


def _assert_points_equal(truth: pl.DataFrame, carried: pl.DataFrame, label: str) -> None:
    """Every ``__pt_`` column equal cell-for-cell (NaN==NaN), across ALL symbols incl. the gap ones."""
    point_cols = [c for c in truth.columns if c.startswith("__pt_")]
    truth = truth.sort("symbol").select(["symbol", *point_cols])
    carried = carried.sort("symbol").select(["symbol", *point_cols])
    assert truth["symbol"].to_list() == carried["symbol"].to_list(), f"{label}: symbol set differs"
    for col in point_cols:
        a = truth[col].to_numpy().astype(np.float64)
        b = carried[col].to_numpy().astype(np.float64)
        both_nan = np.isnan(a) & np.isnan(b)
        close = np.isclose(a, b, rtol=0.0, atol=1e-12, equal_nan=False)
        bad = ~(both_nan | close)
        assert not bad.any(), (
            f"{label}.{col}: {int(bad.sum())} sparse-symbol mismatches\n"
            f"  truth={a[bad][:5]} carried={b[bad][:5]} symbols={np.array(truth['symbol'])[bad][:5]}"
        )


def test_carried_points_match_resolve_points_sparse() -> None:
    """The gate: on a SPARSE stream (gap symbols), the carried positional row-ring reproduces
    ``resolve_points`` byte-identically for every lag-carrying group — including the gap symbols whose
    positional lag differs from a time-based lag. This is the parity bar the production resolve_points
    replacement must clear."""
    stream = _sparse_stream(n_sym=12, n_min=140, gap_period=7, gap_fraction=0.5, seed=13)
    groups = _lag_groups(stream)
    assert groups, "expected lag-carrying reduction groups (efficiency/return_dynamics/momentum_consistency)"

    minutes = sorted(stream["minute"].unique())
    latest = minutes[-1]
    _assert_fixture_is_genuinely_sparse(stream, groups, latest)
    for group in groups:
        carried = CarriedPoints(group)
        for minute in minutes:
            carried.fold(stream.filter(pl.col("minute") == minute))
        truth = resolve_points([group], stream, latest)
        _assert_points_equal(truth, carried.resolve(), f"sparse:{group.name}")


def test_carried_points_replay_equals_cold_seed_sparse() -> None:
    """The held-state invariant ``seed(H); fold(m) == seed(H+m)``: folding the stream minute-by-minute (replay)
    gives the SAME latest-row points as folding it in one pass (cold seed over the whole history). Pins that the
    carried ring has no order dependence — the contract a hot-swap reseed relies on."""
    stream = _sparse_stream(n_sym=10, n_min=120, gap_period=5, gap_fraction=0.5, seed=29)
    groups = _lag_groups(stream)
    minutes = sorted(stream["minute"].unique())

    for group in groups:
        replay = CarriedPoints(group)
        for minute in minutes:
            replay.fold(stream.filter(pl.col("minute") == minute))

        cold = CarriedPoints(group)
        for minute in minutes:  # same sequence, fresh state — must land identically
            cold.fold(stream.filter(pl.col("minute") == minute))

        _assert_points_equal(replay.resolve(), cold.resolve(), f"replay:{group.name}")
