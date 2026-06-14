"""Rust slice-derive parity gate: the Rust ``_derived_row_rust`` path (lag kernel + global re-derive) ==
the Polars ``_derived_row`` (``shift().over("symbol")``) path, cell-for-cell, for EVERY derive column at the
latest row of each symbol — across a full minute stream including warmup (the first bars have no prior bar to
shift from, so the lag is ``null``).

This pins the Rust kernel + expr-rewrite at the value level (independent of the windowed-sum fold and assemble
that follow), and explicitly covers the parity-critical edge cases:
  - the first minute of the stream (every symbol's lag-1/2/3 close is missing -> ``null``),
  - minutes 2 and 3 (lag-2 / lag-3 still missing -> ``null``),
  - a deliberately INTRODUCED missing prior bar (a hole in a symbol's minute sequence within the slice).

Polars ``shift(k).over("symbol")`` is POSITIONAL (the k-th prior ROW), and the engine's slice is minute-
contiguous, so the Rust kernel's positional lag matches it. The hole test confirms BOTH paths treat the slice
positionally (the kernel must agree with Polars even when a minute is absent — neither is time-aware)."""

from __future__ import annotations

import datetime as dt

import numpy as np
import polars as pl

from quantlib.features.compare import runnable
from quantlib.features.declarative import ReductionGroup
from quantlib.features.incremental import IncrementalEngine
from quantlib.features.slice_derive import lag_specs, rewrite_global

BASE = dt.datetime(2026, 6, 15, 13, 30, tzinfo=dt.timezone.utc)


def _stream(n_sym: int = 6, n_min: int = 40) -> pl.DataFrame:
    rng = np.random.default_rng(11)
    rows = []
    price = {s: 100.0 + s for s in range(n_sym)}
    for mi in range(n_min):
        minute = BASE + dt.timedelta(minutes=mi)
        for s in range(n_sym):
            price[s] *= 1.0 + (rng.standard_normal() * 0.003)
            c = price[s]
            rows.append(
                {
                    "symbol": f"S{s}",
                    "minute": minute,
                    "open": c * 0.999,
                    "high": c * 1.002,
                    "low": c * 0.998,
                    "close": c,
                    "volume": 1000.0 + rng.random() * 4000,
                    "n_trades": float(rng.integers(1, 200)),
                    "signed_volume": rng.standard_normal() * 1000,
                    "mean_spread_bps": rng.random() * 5,
                    "quote_imbalance": rng.standard_normal() * 0.3,
                    "mean_bid_size": rng.random() * 100,
                    "mean_ask_size": rng.random() * 100,
                }
            )
    return pl.DataFrame(rows).with_columns(pl.col("minute").cast(pl.Datetime("us", "UTC")))


def _engine(stream: pl.DataFrame) -> IncrementalEngine:
    groups = [g for g in runnable({"minute_agg": stream}) if isinstance(g, ReductionGroup)]
    engine = IncrementalEngine(groups)
    engine.symbols = sorted(stream["symbol"].unique().to_list())
    engine._seed_stateful(stream)
    return engine


def _assert_derive_equal(reference: pl.DataFrame, candidate: pl.DataFrame, label: str) -> None:
    """Every derive column (safe value cols + presence/square + stateful aux) equal cell-for-cell, with
    ``null`` matched to ``null`` (the missing-prior-bar cells) — tol 0 on finite cells (same float ops)."""
    reference = reference.sort("symbol")
    candidate = candidate.sort("symbol")
    cols = [c for c in reference.columns if c.startswith("__") or c in ("close",)]
    assert cols, "no derive columns found to compare"
    for col in cols:
        assert col in candidate.columns, f"{label}: candidate missing {col}"
        ref = reference[col]
        got = candidate[col]
        both_null = ref.is_null() & got.is_null()
        ref_f = ref.fill_null(np.nan).to_numpy().astype(np.float64)
        got_f = got.fill_null(np.nan).to_numpy().astype(np.float64)
        ok = both_null.to_numpy() | (np.abs(ref_f - got_f) <= 1e-12)
        assert ok.all(), f"{label}.{col}: rust != polars derive\n ref={ref.to_list()}\n got={got.to_list()}"


def test_rust_slice_derive_matches_polars_every_minute() -> None:
    """Full minute stream: Rust derive == Polars derive at every minute, incl. warmup (minutes 0/1/2 have
    null lags) and steady state."""
    stream = _stream()
    engine = _engine(stream)
    minutes = sorted(stream["minute"].unique())
    for minute in minutes:
        buffer = stream.filter(pl.col("minute") <= minute)
        cutoff = minute - pl.duration(minutes=engine.DERIVE_SLICE)
        slice_frame = buffer.filter(pl.col("minute") > cutoff)
        polars_row = engine._derived_row(slice_frame, minute)
        rust_row = engine._derived_row_rust(slice_frame, minute)
        _assert_derive_equal(polars_row, rust_row, f"min={minute}")


def test_rust_slice_derive_warmup_lags_are_null() -> None:
    """At the very first minute, every symbol's lag-1/2/3 close is a missing prior bar — the Rust path must
    produce the SAME ``null`` that ``close.shift(k).over('symbol')`` produces (so null-propagation and ``when``
    guards behave identically). Confirms the NaN-sentinel -> null conversion is correct."""
    stream = _stream(n_sym=4, n_min=5)
    engine = _engine(stream)
    minutes = sorted(stream["minute"].unique())
    # minute index 0,1,2 expose lag1 (>=1 missing), lag2 (>=2 missing), lag3 (>=3 missing) respectively
    for idx in (0, 1, 2):
        minute = minutes[idx]
        buffer = stream.filter(pl.col("minute") <= minute)
        cutoff = minute - pl.duration(minutes=engine.DERIVE_SLICE)
        slice_frame = buffer.filter(pl.col("minute") > cutoff)
        polars_row = engine._derived_row(slice_frame, minute)
        rust_row = engine._derived_row_rust(slice_frame, minute)
        _assert_derive_equal(polars_row, rust_row, f"warmup-min{idx}")


def test_rust_slice_derive_with_missing_prior_bar_hole() -> None:
    """A HOLE in a symbol's minute sequence inside the slice: Polars ``shift`` is positional (the prior ROW,
    not the prior minute), and the Rust kernel is too — so both must agree even with a gap. Drop the second-to-
    last minute for symbol S1 and confirm the latest-row derive still matches cell-for-cell."""
    stream = _stream(n_sym=4, n_min=12)
    minutes = sorted(stream["minute"].unique())
    latest = minutes[-1]
    hole_minute = minutes[-2]
    holed = stream.filter(~((pl.col("symbol") == "S1") & (pl.col("minute") == hole_minute)))
    engine = _engine(holed)
    cutoff = latest - pl.duration(minutes=engine.DERIVE_SLICE)
    slice_frame = holed.filter((pl.col("minute") <= latest) & (pl.col("minute") > cutoff))
    polars_row = engine._derived_row(slice_frame, latest)
    rust_row = engine._derived_row_rust(slice_frame, latest)
    _assert_derive_equal(polars_row, rust_row, "hole")


def test_lag_specs_only_positional_close_shifts() -> None:
    """Structural guard: the only ``over('symbol')`` in the safe+aux+extra derive is a positional ``close``
    shift (lags 1/2/3). If a new group adds another grouped op, ``lag_specs`` would surface it here — keeping
    the Rust slice-derive's assumption (everything grouped is a plain-column shift) honest."""
    stream = _stream(n_sym=3, n_min=8)
    engine = _engine(stream)
    lags, max_lag = lag_specs([*engine.safe_derived, *engine.stateful_aux, *engine.extra])
    assert lags == {("close", 1), ("close", 2), ("close", 3)}, f"unexpected lag specs: {lags}"
    assert max_lag == 3
    # rewrite_global must strip the over() (no Over node remains for the shifted column)
    for expr in engine.rust_safe_derived:
        serialized = expr.meta.serialize(format="json")
        assert '"Shift"' not in serialized or '"partition_by"' not in serialized, "over() not stripped"
    # and the rewritten exprs must reference the lag columns
    assert any("__lag1_close" in e.meta.serialize(format="json") for e in engine.rust_safe_derived)


def test_rewrite_global_round_trip() -> None:
    """``rewrite_global`` on a one-minute-return expr evaluates correctly on a lag-carrying frame (no over)."""
    ret = pl.col("close") / pl.col("close").shift(1).over("symbol") - 1.0
    rewritten = rewrite_global(ret)
    frame = pl.DataFrame({"close": [110.0, 121.0], "__lag1_close": [100.0, 110.0]})
    got = frame.select(rewritten.alias("ret"))["ret"].to_list()
    assert abs(got[0] - 0.10) < 1e-12 and abs(got[1] - 0.10) < 1e-12
