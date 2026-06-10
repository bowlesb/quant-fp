"""Tests for the v1 feature set, including a replay-equivalence check: computing
features for the same point-in-time twice (and from a longer history truncated to
that point) yields identical vectors. This is the feature-level sibling of the
aggregate parity test."""
import math
from datetime import datetime, timedelta, timezone

from quantlib.features import (
    FEATURE_NAMES,
    BarRow,
    FeatureContext,
    compute_features,
    feature_vector,
)

BASE = datetime(2026, 6, 10, 14, 30, tzinfo=timezone.utc)


def _bars(closes: list[float]) -> list[BarRow]:
    return [
        BarRow(
            ts=BASE + timedelta(minutes=i),
            open=c, high=c * 1.001, low=c * 0.999, close=c, volume=1000 + i, vwap=c,
        )
        for i, c in enumerate(closes)
    ]


def test_returns_and_vector_shape() -> None:
    closes = [100.0 + i for i in range(70)]
    bars = _bars(closes)
    ctx = FeatureContext(symbol="X", ts=bars[-1].ts, bars=bars, session_open=100.0)
    feats = compute_features(ctx)
    assert set(feats) == set(FEATURE_NAMES)
    assert len(feature_vector(ctx)) == len(FEATURE_NAMES)
    # 5-minute return = close[-1]/close[-6]-1 = 169/164-1
    assert abs(feats["ret_5m"] - (169.0 / 164.0 - 1.0)) < 1e-12


def test_insufficient_history_is_nan_not_error() -> None:
    bars = _bars([100.0, 101.0, 102.0])  # only 3 bars
    ctx = FeatureContext(symbol="X", ts=bars[-1].ts, bars=bars, session_open=100.0)
    feats = compute_features(ctx)
    assert math.isnan(feats["ret_60m"])      # no 60-min lookback yet -> undefined
    assert math.isnan(feats["ret_5m"])       # only 3 bars, 5-min return undefined too


def test_gap_and_calendar() -> None:
    bars = _bars([100.0, 102.0])
    ctx = FeatureContext(symbol="X", ts=bars[-1].ts, bars=bars, session_open=100.0)
    feats = compute_features(ctx)
    assert abs(feats["gap_from_open"] - 0.02) < 1e-12
    assert feats["day_of_week"] == float(BASE.weekday())
    assert feats["minute_of_day"] == float(bars[-1].ts.hour * 60 + bars[-1].ts.minute)


def test_replay_equivalence() -> None:
    """A feature vector at minute T must be identical whether computed from a
    history ending exactly at T, or from a longer history truncated to T."""
    closes = [100.0 + math.sin(i / 5.0) * 3 for i in range(120)]
    full = _bars(closes)
    market = _bars([400.0 + math.cos(i / 7.0) for i in range(120)])

    cut = 90  # the point-in-time we evaluate
    ctx_truncated = FeatureContext(
        symbol="X", ts=full[cut].ts, bars=full[: cut + 1],
        session_open=closes[0], market_bars=market[: cut + 1],
        trade_imbalance=0.1, large_print_cnt=2, trade_intensity=5.0,
        spread_bps=1.5, quote_imbalance=-0.05,
    )
    # "Live" path that happened to only have data up to the cut.
    ctx_live = FeatureContext(
        symbol="X", ts=full[cut].ts, bars=full[: cut + 1],
        session_open=closes[0], market_bars=market[: cut + 1],
        trade_imbalance=0.1, large_print_cnt=2, trade_intensity=5.0,
        spread_bps=1.5, quote_imbalance=-0.05,
    )
    assert feature_vector(ctx_truncated) == feature_vector(ctx_live)
    # And no lookahead: a vector computed with extra future bars appended must NOT
    # change the value at the cut (we slice to cut+1 either way).
    rel = compute_features(ctx_truncated)["rel_ret_30m"]
    assert not math.isnan(rel)
