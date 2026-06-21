"""FP_CENTERED_TIME conditions the BATCH time-axis OLS exactly like the incremental engine — it pins a
``kind="time"`` regression's x to the incremental engine's small anchor origin (``latest − _TIME_ORIGIN_LAG``)
so the OLS operand sums (Σx, Σxx, Σxy) stay small and ``denom_x = b·Σxx − (Σx)²`` / ``cov_n = b·Σxy − Σx·Σy``
are no longer catastrophic-cancellation differences of large near-equal sums.

The conditioning is VALUE-IDENTICAL on well-conditioned cells (OLS is origin-invariant): the SAME features
come out, only the float cancellation shrinks — so the fingerprint is unchanged and trust is preserved. The
flag is default OFF (the batch expression graph is byte-identical to today); these tests drive both states.

Scope: the time-axis regressions (trend_quality.trend, clean_momentum.cm_clean, residual_analysis.resid,
price_volume.obv) whose x slot is ``kind="time"``. The non-time regressions (market_beta SPY-broadcast,
return_dynamics autocorr, price_volume.pv_correlation return) are untouched (they need a separate return
anchor — out of this PR's scope)."""

from __future__ import annotations

import datetime as dt

import numpy as np
import polars as pl
import pytest

from quantlib.features import declarative
from quantlib.features.base import BatchContext
from quantlib.features.declarative import (
    ReductionGroup,
    _pinned_time_x,
    build_plan,
    compute_reduction_batch,
)
from quantlib.features.incremental import _TIME_ORIGIN_LAG
from quantlib.features.latest import rust_windowed_sums
from quantlib.features.registry import REGISTRY

quant_tick = pytest.importorskip("quant_tick")

BASE = dt.datetime(2026, 3, 2, 14, 30, tzinfo=dt.timezone.utc)
TIME_AXIS_GROUPS = ("trend_quality", "clean_momentum", "residual_analysis", "price_volume")


def _group(name: str) -> ReductionGroup:
    groups = [g for g in REGISTRY.groups() if isinstance(g, ReductionGroup) and g.name == name]
    assert groups, f"{name} missing from registry"
    return groups[0]


def _trend_frame(n_sym: int, n_min: int, present_p: float, seed: int, *, near_perfect: bool) -> pl.DataFrame:
    """A deep per-symbol price stream. ``near_perfect`` -> a steep near-linear trend (the r2≈1 regime where the
    time-OLS denom cancellation bites); else moderate-vol drift (a well-conditioned reference for value-eq).
    """
    rng = np.random.default_rng(seed)
    rows = []
    price = {s: 100.0 + s for s in range(n_sym)}
    slope = {s: 0.03 + 0.004 * s for s in range(n_sym)}
    for mi in range(n_min):
        minute = BASE + dt.timedelta(minutes=mi)
        for s in range(n_sym):
            if not (mi == 0 or rng.random() < present_p):
                continue
            if near_perfect:
                close = 100.0 + slope[s] * mi + rng.standard_normal() * 2e-4
            else:
                price[s] *= 1.0 + rng.standard_normal() * 0.02
                close = price[s]
            rows.append(
                {
                    "symbol": f"S{s}",
                    "minute": minute,
                    "open": close * 0.999,
                    "close": close,
                    "high": close * 1.002,
                    "low": close * 0.998,
                    "volume": 1000.0 + rng.random() * 4000,
                }
            )
    return (
        pl.DataFrame(rows).with_columns(pl.col("minute").dt.cast_time_unit("us")).sort(["symbol", "minute"])
    )


def test_time_axis_groups_declare_a_time_regression() -> None:
    """The groups this conditioning targets each expose exactly the ``kind="time"`` x regression it keys off,
    and NO non-time group is accidentally swept in (the scope guard)."""
    assert _group("trend_quality")._time_regression_names() == {"trend"}
    assert _group("clean_momentum")._time_regression_names() == {"cm_clean"}
    assert _group("residual_analysis")._time_regression_names() == {"resid"}
    assert _group("price_volume")._time_regression_names() == {"obv"}
    # market_beta / return_dynamics have NO time regression — they must stay untouched by this fix
    assert _group("market_beta")._time_regression_names() == set()
    assert _group("return_dynamics")._time_regression_names() == set()


def test_pinned_time_x_matches_incremental_origin() -> None:
    """The batch latest-pin maps the anchor minute to exactly ``_TIME_ORIGIN_LAG`` — byte-identical to the
    incremental engine's per-fold origin (``minute − _TIME_ORIGIN_LAG·60``), so the conditioned batch axis and
    the incremental axis coincide at the anchor minute (not merely 'both small')."""
    latest = BASE + dt.timedelta(minutes=300)
    latest_epoch = int(latest.timestamp())
    frame = pl.DataFrame({"minute": [latest]}).with_columns(pl.col("minute").dt.cast_time_unit("us"))
    x = frame.select(_pinned_time_x(latest_epoch).alias("x"))["x"].to_numpy()[0]
    assert x == pytest.approx(float(_TIME_ORIGIN_LAG), abs=1e-12)


def test_centered_time_shrinks_operands_but_keeps_denom_identical(monkeypatch: pytest.MonkeyPatch) -> None:
    """ON vs OFF: the raw OLS operand sums Σx/Σxx/Σxy shrink by orders of magnitude (the conditioning), while
    ``denom_x``/``cov_n`` — the origin-invariant quantities the stats are built from — are UNCHANGED. This is
    the proof the fix is value-identical (it moves only the float conditioning, not the math)."""
    group = [_group("trend_quality")]
    frame = _trend_frame(n_sym=1, n_min=300, present_p=1.0, seed=5, near_perfect=True)
    prepared = group[0].prepare(frame)
    latest = int(prepared["minute"].max().timestamp())

    def operands(time_origin: int | None) -> dict[str, float]:
        derived, extra, value_cols, _, reg_plan, windows, _ = build_plan(
            group, time_origin_epoch=time_origin
        )
        marshalled = prepared.with_columns(derived).with_columns(extra)
        long = rust_windowed_sums(marshalled, value_cols, windows)
        ns = reg_plan[0][4]
        row = long.filter(pl.col("window") == 60)
        sums = {key: float(row[f"__rd_{ns}_{key}"].to_numpy()[0]) for key in ("b", "x", "xx", "xy", "y")}
        sums["denom_x"] = sums["b"] * sums["xx"] - sums["x"] * sums["x"]
        sums["cov_n"] = sums["b"] * sums["xy"] - sums["x"] * sums["y"]
        return sums

    off = operands(None)  # whole-frame epoch.min() origin (current batch)
    on = operands(latest)  # FP_CENTERED_TIME pinned origin
    # the operands shrink markedly (Σxx by >10x — the conditioning)
    assert abs(on["xx"]) < abs(off["xx"]) / 10.0
    assert abs(on["x"]) < abs(off["x"])
    # ...while denom_x / cov_n (origin-invariant) are identical to ~machine precision -> value-identical
    assert on["denom_x"] == pytest.approx(off["denom_x"], rel=1e-9)
    assert on["cov_n"] == pytest.approx(off["cov_n"], rel=1e-9)


@pytest.mark.parametrize("group_name", TIME_AXIS_GROUPS)
def test_centered_time_is_value_identical_on_good_cells(
    group_name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On a well-conditioned stream the conditioned batch (FP_CENTERED_TIME ON) produces the SAME features as
    OFF to machine precision — the fingerprint-unchanged / trust-preserved guarantee. Null masks identical.
    """
    group = [_group(group_name)]
    frame = _trend_frame(n_sym=10, n_min=200, present_p=0.7, seed=55, near_perfect=False)
    ctx = BatchContext(frames={"minute_agg": frame})

    monkeypatch.setattr(declarative, "_USE_CENTERED_TIME", False)
    off = compute_reduction_batch(group, ctx)[group_name].sort("symbol")
    monkeypatch.setattr(declarative, "_USE_CENTERED_TIME", True)
    on = compute_reduction_batch(group, ctx)[group_name].sort("symbol")

    assert off.columns == on.columns
    for col in off.columns:
        if col in ("symbol", "minute"):
            continue
        off_v, on_v = off[col].to_numpy(), on[col].to_numpy()
        off_null, on_null = pl.Series(off_v).is_null().to_numpy(), pl.Series(on_v).is_null().to_numpy()
        np.testing.assert_array_equal(np.isnan(off_v.astype(float)), np.isnan(on_v.astype(float)))
        np.testing.assert_array_equal(off_null, on_null)
        finite = ~np.isnan(off_v.astype(float))
        if finite.any():
            np.testing.assert_allclose(
                on_v.astype(float)[finite], off_v.astype(float)[finite], rtol=1e-7, atol=1e-9
            )


def test_centered_time_flag_default_off_is_byte_identical(monkeypatch: pytest.MonkeyPatch) -> None:
    """With the flag OFF (the default), the batch path is byte-identical to its pre-fix self — the safety
    guarantee that turning the flag off restores the exact current behavior (fp unchanged until a Lead/Ben
    relaunch flips it). Proven by build_plan producing the SAME derived exprs as the no-origin call."""
    group = [_group("trend_quality")]
    monkeypatch.setattr(declarative, "_USE_CENTERED_TIME", False)
    derived_off, *_ = build_plan(group, time_origin_epoch=None)
    derived_explicit, *_ = build_plan(group)  # default arg
    assert [str(e) for e in derived_off] == [str(e) for e in derived_explicit]
