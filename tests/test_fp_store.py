"""FP0 store tests: the Parquet read API (R13) round-trips, tracks source, raises on unknown."""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import polars as pl
import pytest

from quantlib.features import store
from quantlib.features.base import BatchContext
from quantlib.features.engine import run_all
from quantlib.features.registry import REGISTRY

BASE_MINUTE = datetime(2026, 6, 12, 8, 0)


def _minute_agg(n: int = 60) -> pl.DataFrame:
    rows = [
        {"symbol": symbol, "minute": BASE_MINUTE + timedelta(minutes=i), "close": 100.0 + i * 0.1}
        for symbol in ("AAA", "BBB")
        for i in range(n)
    ]
    return pl.DataFrame(rows)


def _ret1m_frame(value: float, n: int) -> pl.DataFrame:
    return pl.DataFrame(
        {"symbol": ["AAA"] * n, "minute": [BASE_MINUTE + timedelta(minutes=i) for i in range(n)], "ret_1m": [value] * n}
    )


def test_store_roundtrip(tmp_path: Path) -> None:
    ctx = BatchContext(frames={"minute_agg": _minute_agg()})
    price = REGISTRY.get_group("price_returns")
    vector = run_all([price], ctx)
    store.write_group(tmp_path, price.name, price.version, "backfill", "2026-06-12", vector)

    got = store.get_features(
        ["ret_5m"], ["AAA"], BASE_MINUTE, BASE_MINUTE + timedelta(minutes=59), tmp_path
    )
    direct = (
        vector.filter(pl.col("symbol") == "AAA")
        .select(["symbol", "minute", "ret_5m"])
        .sort(["symbol", "minute"])
    )
    # Keys are exact; values round-trip through Float32 storage (intentional ~54% space narrowing), so
    # compare within Float32 precision rather than bit-exact Float64.
    assert got.select(["symbol", "minute"]).equals(direct.select(["symbol", "minute"]))
    pair = got.join(direct.rename({"ret_5m": "_d"}), on=["symbol", "minute"])
    assert pair.select(((pl.col("ret_5m") - pl.col("_d")).abs() <= 1e-6 + 1e-6 * pl.col("_d").abs()).all()).item()


def test_store_idempotent_overwrite(tmp_path: Path) -> None:
    ctx = BatchContext(frames={"minute_agg": _minute_agg()})
    price = REGISTRY.get_group("price_returns")
    vector = run_all([price], ctx)
    store.write_group(tmp_path, price.name, price.version, "backfill", "2026-06-12", vector)
    store.write_group(tmp_path, price.name, price.version, "backfill", "2026-06-12", vector)  # rerun
    got = store.get_features(["ret_1m"], "universe", BASE_MINUTE, BASE_MINUTE + timedelta(minutes=59), tmp_path)
    assert got.height == 120  # 2 symbols x 60 minutes, not doubled


def test_store_auto_prefers_backfill_then_stream(tmp_path: Path) -> None:
    store.write_group(tmp_path, "price_returns", "1.0.0", "stream", "2026-06-12", _ret1m_frame(1.0, 10))
    store.write_group(tmp_path, "price_returns", "1.0.0", "backfill", "2026-06-12", _ret1m_frame(2.0, 5))
    got = store.get_features(
        ["ret_1m"], "universe", BASE_MINUTE, BASE_MINUTE + timedelta(minutes=9), tmp_path
    ).sort("minute")
    values = got["ret_1m"].to_list()
    assert values[:5] == [2.0] * 5  # backfill (settled truth) wins where available
    assert values[5:] == [1.0] * 5  # stream fills the unsettled remainder


def test_store_unknown_feature_raises(tmp_path: Path) -> None:
    with pytest.raises(KeyError):
        store.get_features(["does_not_exist"], "universe", BASE_MINUTE, BASE_MINUTE, tmp_path)
