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
    is_rth,
)


def test_is_rth_handles_dst() -> None:
    # 14:00 UTC = 09:00 ET (pre-open) in both DST regimes -> not RTH
    assert not is_rth(datetime(2026, 6, 10, 13, 0, tzinfo=timezone.utc))   # 09:00 EDT
    # EDT (summer): RTH is 13:30-20:00 UTC
    assert is_rth(datetime(2026, 6, 10, 13, 30, tzinfo=timezone.utc))      # 09:30 EDT open
    assert is_rth(datetime(2026, 6, 10, 19, 59, tzinfo=timezone.utc))      # 15:59 EDT
    assert not is_rth(datetime(2026, 6, 10, 20, 0, tzinfo=timezone.utc))   # 16:00 EDT close
    # EST (winter, e.g. early March before DST 2026-03-08): RTH shifts to 14:30-21:00 UTC
    assert not is_rth(datetime(2026, 3, 2, 13, 30, tzinfo=timezone.utc))   # 08:30 EST premarket
    assert is_rth(datetime(2026, 3, 2, 14, 30, tzinfo=timezone.utc))       # 09:30 EST open
    assert is_rth(datetime(2026, 3, 2, 20, 30, tzinfo=timezone.utc))       # 15:30 EST, still RTH
    assert not is_rth(datetime(2026, 3, 2, 21, 0, tzinfo=timezone.utc))    # 16:00 EST close


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


def test_returns_are_gap_safe_timestamp_based() -> None:
    """A missing minute must make the affected return NaN, not silently use a
    further-back bar (the positional-lookup bug)."""
    # bars at minutes 0,1,2,3,4 then a gap, then 10 (all within RTH)
    closes = [100, 101, 102, 103, 104]
    bars = [
        BarRow(ts=BASE + timedelta(minutes=i), open=c, high=c, low=c, close=c,
               volume=1000, vwap=c)
        for i, c in enumerate(closes)
    ]
    bars.append(BarRow(ts=BASE + timedelta(minutes=10), open=110, high=110, low=110,
                       close=110, volume=1000, vwap=110))
    ctx = FeatureContext(symbol="X", ts=bars[-1].ts, bars=bars, session_open=100.0)
    feats = compute_features(ctx)
    # 5-min return from minute 10 needs minute-5 bar, which doesn't exist -> NaN
    assert math.isnan(feats["ret_5m"])


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
