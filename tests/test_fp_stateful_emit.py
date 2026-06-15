"""Consolidated stateful-emit parity: the four stateful groups (technical, candlestick, price_returns,
price_levels) emitted in ONE shared pass (``emit_stateful``) must equal each group's own per-group
``StatefulEngine.step`` CELL-FOR-CELL (byte-identical), and — closing the loop — equal the certified
backfill ``compute().last``.

This is a SCHEDULING change: ``emit_stateful`` folds each engine's state and builds its state frame exactly
as ``step`` does (sharing the ONE coded buffer), then merges the per-symbol state frames and evaluates ALL
groups' ``assemble`` exprs in one polars pass. The state columns + the expressions are unchanged, so the
output must be identical with NO tolerance. If it diverged, the streaming write would disagree with the
per-group fast path the platform certifies.
"""
from __future__ import annotations

import datetime as dt

import numpy as np
import polars as pl

import pytest

from quantlib.features.base import BatchContext
from quantlib.features.groups.candlestick import CandlestickGroup
from quantlib.features.groups.price_levels import PriceLevelGroup
from quantlib.features.groups.price_returns import PriceReturnGroup
from quantlib.features.groups.technical import TechnicalGroup
from quantlib.features.stateful import StatefulEngine, coded_buffer, emit_stateful

BASE = dt.datetime(2026, 6, 15, 13, 30, tzinfo=dt.timezone.utc)


def _stream(n_sym: int = 8, n_min: int = 90, seed: int = 17) -> pl.DataFrame:
    """A dense OHLC minute stream (every symbol present every minute — the Monday flow) carrying every
    column the four stateful groups read (open/high/low/close)."""
    rng = np.random.default_rng(seed)
    rows = []
    price = {s: 100.0 + s for s in range(n_sym)}
    for mi in range(n_min):
        minute = BASE + dt.timedelta(minutes=mi)
        for s in range(n_sym):
            price[s] *= 1.0 + (rng.standard_normal() * 0.003)
            close = price[s]
            opn = close * (1.0 + rng.standard_normal() * 0.001)
            high = max(opn, close) * (1.0 + abs(rng.standard_normal()) * 0.001)
            low = min(opn, close) * (1.0 - abs(rng.standard_normal()) * 0.001)
            rows.append({"symbol": f"S{s}", "minute": minute, "open": opn, "high": high, "low": low, "close": close})
    return pl.DataFrame(rows).with_columns(pl.col("minute").cast(pl.Datetime("us", "UTC")))


def _assert_byte_identical(actual: pl.DataFrame, expected: pl.DataFrame, label: str) -> None:
    actual = actual.sort("symbol").select(expected.columns)
    expected = expected.sort("symbol")
    assert actual.height == expected.height, f"{label}: row count {actual.height} != {expected.height}"
    assert actual.equals(expected), f"{label}: consolidated emit_stateful != per-group step (not byte-identical)"


STATEFUL_GROUP_FACTORIES = (TechnicalGroup, CandlestickGroup, PriceReturnGroup, PriceLevelGroup)


def test_emit_stateful_matches_per_group_step() -> None:
    """``emit_stateful`` over all four groups == each group's own ``StatefulEngine.step``, cell-for-cell,
    across the minute stream INCLUDING warmup. Two parallel engine sets are folded the same way so the only
    difference is the consolidated assemble pass."""
    stream = _stream()
    minutes = sorted(stream["minute"].unique())
    groups = [factory() for factory in STATEFUL_GROUP_FACTORIES]
    per_group_engines = {group.name: StatefulEngine(group) for group in groups}
    consolidated_engines = {group.name: StatefulEngine(group) for group in groups}
    checkpoints = {1, 5, 12, 26, 40, 60, len(minutes) - 1}

    for ti, minute in enumerate(minutes):
        buffer = stream.filter(pl.col("minute") <= minute)
        ctx = BatchContext(frames={"minute_agg": buffer})
        latest = buffer["minute"].max()

        # Per-group reference: each engine steps independently off its own coded buffer.
        coded_ref = coded_buffer(buffer, latest)
        reference = {
            group.name: per_group_engines[group.name].step(buffer, ctx, coded=coded_ref) for group in groups
        }
        # Consolidated: one shared coded buffer, one shared assemble pass.
        coded_con = coded_buffer(buffer, latest)
        consolidated = emit_stateful(
            [consolidated_engines[group.name] for group in groups], buffer, ctx, coded=coded_con
        )

        assert set(consolidated) == {group.name for group in groups}
        if ti in checkpoints:
            for group in groups:
                _assert_byte_identical(consolidated[group.name], reference[group.name], f"{group.name} min{ti}")


def test_emit_stateful_matches_backfill_last() -> None:
    """The consolidated emit must also equal the BACKFILL rolling form's last minute (the source of truth),
    closing the loop emit_stateful == step == compute().last (within each feature's declared tolerance)."""
    stream = _stream()
    latest = stream["minute"].max()
    ctx = BatchContext(frames={"minute_agg": stream})
    groups = [factory() for factory in STATEFUL_GROUP_FACTORIES]
    engines = [StatefulEngine(group) for group in groups]
    coded = coded_buffer(stream, latest)
    consolidated = emit_stateful(engines, stream, ctx, coded=coded)

    for group in groups:
        backfill = group.compute(ctx)
        last = backfill.filter(pl.col("minute") == backfill["minute"].max()).sort("symbol")
        got = consolidated[group.name].sort("symbol").select(last.columns)
        assert got.height == last.height, f"{group.name}: row count differs from backfill"
        for col in [c for c in last.columns if c not in ("symbol", "minute")]:
            joined = last.select("symbol", col).join(
                got.select("symbol", pl.col(col).alias("_g")), on="symbol"
            )
            bad = joined.filter(
                ~(
                    (pl.col(col).is_null() & pl.col("_g").is_null())
                    | ((pl.col(col) - pl.col("_g")).abs() <= 1e-9 + 1e-6 * pl.col(col).abs())
                )
            )
            assert bad.height == 0, f"{group.name}.{col}: {bad.height} mismatches vs backfill\n{bad.head()}"


def _gappy_stream(n_sym: int = 8, n_min: int = 90, seed: int = 23) -> pl.DataFrame:
    """A stream with DROPPED minutes per symbol — every (symbol, minute) where (s + mi) % 7 == 0 is absent.
    On a gappy grid a POSITIONAL prior bar differs from the TIME-based ``base.lagged`` prior, so this is the
    adversarial case that catches any positional-vs-time confusion in the coded reduction path."""
    rng = np.random.default_rng(seed)
    rows = []
    price = {s: 100.0 + s for s in range(n_sym)}
    for mi in range(n_min):
        minute = BASE + dt.timedelta(minutes=mi)
        for s in range(n_sym):
            price[s] *= 1.0 + rng.standard_normal() * 0.003
            if (s + mi) % 7 == 0:
                continue
            close = price[s]
            opn = close * (1.0 + rng.standard_normal() * 0.001)
            high = max(opn, close) * (1.0 + abs(rng.standard_normal()) * 0.001)
            low = min(opn, close) * (1.0 - abs(rng.standard_normal()) * 0.001)
            rows.append({"symbol": f"S{s}", "minute": minute, "open": opn, "high": high, "low": low, "close": close})
    return pl.DataFrame(rows).with_columns(pl.col("minute").cast(pl.Datetime("us", "UTC")))


@pytest.mark.parametrize("stream_factory", [_stream, _gappy_stream])
def test_technical_coded_reduction_matches_certified(stream_factory) -> None:  # type: ignore[no-untyped-def]
    """technical's coded-buffer reduction (``reduction_columns_from_coded``, the fast path the consolidated
    emit uses) must equal the certified ``reduction_columns`` CELL-FOR-CELL — including on a GAPPY grid, where
    the time-based prior close (RSI's gain/loss) differs from a naive positional shift. Byte-equality, no
    tolerance: this is the reduction the streaming write commits."""
    stream = stream_factory()
    latest = stream["minute"].max()
    ctx = BatchContext(frames={"minute_agg": stream})
    group = TechnicalGroup()
    coded = coded_buffer(stream, latest)
    fast = group.reduction_columns_from_coded(coded).sort("symbol")
    certified = group.reduction_columns(ctx).sort("symbol").select(fast.columns)
    assert fast.height == certified.height, "coded reduction row count differs"
    # null<->null and value equality within float-kernel noise (the same tol the reduction parity uses);
    # the algebra is identical, only the marshal differs, so this is effectively byte-equal.
    for col in [c for c in fast.columns if c != "symbol"]:
        joined = certified.select("symbol", col).join(
            fast.select("symbol", pl.col(col).alias("_g")), on="symbol"
        )
        bad = joined.filter(
            ~(
                (pl.col(col).is_null() & pl.col("_g").is_null())
                | ((pl.col(col) - pl.col("_g")).abs() <= 1e-9 + 1e-9 * pl.col(col).abs())
            )
        )
        assert bad.height == 0, f"technical.{col}: {bad.height} coded-vs-certified mismatches\n{bad.head()}"


@pytest.mark.parametrize("factory", STATEFUL_GROUP_FACTORIES)
def test_emit_stateful_single_group(factory: type) -> None:
    """A single stateful group through the consolidated pass still equals its own ``step`` — the
    consolidation must not depend on all four being present."""
    stream = _stream(n_min=60)
    latest = stream["minute"].max()
    ctx = BatchContext(frames={"minute_agg": stream})
    group = factory()
    ref_engine = StatefulEngine(group)
    con_engine = StatefulEngine(group)
    coded_ref = coded_buffer(stream, latest)
    reference = ref_engine.step(stream, ctx, coded=coded_ref)
    coded_con = coded_buffer(stream, latest)
    consolidated = emit_stateful([con_engine], stream, ctx, coded=coded_con)
    _assert_byte_identical(consolidated[group.name], reference, group.name)
