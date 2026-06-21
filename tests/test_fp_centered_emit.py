"""The numpy + Rust assemble emit paths honor centered std for a centered group (volume) — so they match
the polars ``assemble_from_long`` live truth cell-for-cell. Before the fix, ``emit_numpy`` / ``emit_rust``
(and ``build_assemble_plan``) read the RAW ``base``/``base__sq`` power sums for std even on a centered base,
re-introducing the large-magnitude Σx²−(Σx)²/n cancellation that the centering (Σ(v−a), Σ(v−a)²) exists to
avoid — so FP_RUST_ASSEMBLE on volume would have diverged materially from the batch. This pins the three
emit paths to agreement on a large-volume centered group, the regression guard for that gap."""

from __future__ import annotations

import datetime as dt

import numpy as np
import polars as pl
import pytest

from quantlib.features.declarative import ReductionGroup
from quantlib.features.incremental import IncrementalEngine
from quantlib.features.reduction_anchor import anchor_column, attach_volume_anchor

quant_tick = pytest.importorskip("quant_tick")

BASE = dt.datetime(2026, 6, 15, 13, 30, tzinfo=dt.timezone.utc)


def _large_volume_stream(n_sym: int = 6, n_min: int = 220, seed: int = 5) -> pl.DataFrame:
    """A deep stream with LARGE per-symbol volume magnitudes (1e5..1e7) — the regime where the raw power-sum
    std cancellation bites and centering matters. Streams past the 180m deepest window."""
    rng = np.random.default_rng(seed)
    rows = []
    price = {s: 100.0 + s for s in range(n_sym)}
    scale = {s: 10.0 ** (5 + s % 3) for s in range(n_sym)}  # 1e5, 1e6, 1e7 per-symbol volume scales
    for mi in range(n_min):
        minute = BASE + dt.timedelta(minutes=mi)
        for s in range(n_sym):
            price[s] *= 1.0 + rng.standard_normal() * 0.002
            close = price[s]
            rows.append(
                {
                    "symbol": f"S{s}",
                    "minute": minute,
                    "open": close * 0.999,
                    "high": close * 1.002,
                    "low": close * 0.998,
                    "close": close,
                    # TIGHT relative noise (1e-5) on a large base = the std power-sum cancellation regime
                    # (proven in test_reduction_anchor): raw Σv²−(Σv)²/n loses ~2.5e-6 here, centered is exact.
                    "volume": scale[s] * (1.0 + rng.standard_normal() * 1e-5),
                    "n_trades": float(rng.integers(1, 200)),
                    "signed_volume": rng.standard_normal() * 1000,
                    "mean_spread_bps": rng.random() * 5,
                    "quote_imbalance": rng.standard_normal() * 0.3,
                    "mean_bid_size": rng.random() * 100,
                    "mean_ask_size": rng.random() * 100,
                }
            )
    frame = pl.DataFrame(rows).with_columns(pl.col("minute").cast(pl.Datetime("us", "UTC")))
    # per-symbol daily snapshot -> the volume anchor (the production attach), so the centered std engages
    daily = frame.group_by("symbol").agg(pl.col("volume").mean().alias("volume")).with_columns(
        pl.lit(1).alias("date")
    )
    return attach_volume_anchor(frame, daily)


def _volume_group() -> ReductionGroup:
    from quantlib.features.compare import runnable

    stream = _large_volume_stream(n_min=1)
    vol = [g for g in runnable({"minute_agg": stream}) if isinstance(g, ReductionGroup) and g.name == "volume"]
    assert vol, "volume reduction group not found"
    return vol[0]


def _assert_paths_agree(truth: pl.DataFrame, other: pl.DataFrame, label: str) -> None:
    joined = truth.join(other, on="symbol", suffix="__o")
    for col in [c for c in truth.columns if c not in ("symbol", "minute")]:
        a, b = pl.col(col), pl.col(f"{col}__o")
        bad = joined.filter(
            ~((a.is_null() & b.is_null()) | ((a - b).abs() <= 1e-9 + 1e-9 * a.abs()))
        )
        assert bad.height == 0, f"{label}.{col}: {bad.height} mismatches\n{bad.head()}"


def test_centered_volume_emit_paths_agree() -> None:
    stream = _large_volume_stream()
    vol = _volume_group()
    assert vol.centered_std(), "volume must declare a centered_std column for this test to be meaningful"
    assert anchor_column("volume") in stream.columns, "anchor column must be attached"

    # three independent engines, each stepped over the SAME full buffer (no manual seed+step double-fold).
    truth = IncrementalEngine([vol]).step(stream)["volume"]  # assemble_from_long (centered live truth)
    numpy_out = IncrementalEngine([vol]).step_numpy(stream)["volume"]
    rust_out = IncrementalEngine([vol]).step_rust(stream)["volume"]

    _assert_paths_agree(truth, numpy_out, "emit_numpy")
    _assert_paths_agree(truth, rust_out, "emit_rust")
