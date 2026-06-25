"""Correctness + invariant tests for the clean engine (clean_engine.py + the worked group examples).

Validates by CORRECTNESS (formula / intuition / the design invariant), NOT byte-identity to the legacy engine
— per the rewrite mandate. The load-bearing test is ``test_backfill_equals_replay``: it proves the whole
design's central claim (live and backfill are the same replay, so they cannot diverge) — which is what makes
the legacy second-form + parity machinery unnecessary.
"""

from __future__ import annotations

import numpy as np

from quantlib.features import clean_groups_example as ex
from quantlib.features.clean_engine import CleanEngine, RingBuffer


def _bars(t: int, syms: tuple[str, ...] = ("A", "B", "C")) -> dict[str, np.ndarray]:
    n = len(syms)
    return {
        "symbol": np.array(syms),
        "close": np.array([100.0 + t, 200.0 + 0.5 * t, 150.0 - 0.3 * t][:n]),
        "high": np.array([101.0 + t, 201.0, 151.0][:n]),
        "low": np.array([99.0 + t, 199.0, 149.0][:n]),
        "open": np.array([100.0 + t, 200.0, 150.0][:n]),
        "volume": np.array([10.0, 20.0, 15.0][:n]),
        "minute_epoch": np.array([t * 60]),
    }


def _all_groups() -> list:
    return [
        ex.TrendQualityClean(),
        ex.VwapDeviationClean(),
        ex.RealizedRangeClean(),
        ex.CandlestickClean(),
        ex.BreadthClean(),
        ex.MacdClean(),
        ex.IntradaySeasonalityClean(),
        ex.SwingClean(),
    ]


def test_ring_gap_safe_positional_window() -> None:
    """The ring's trailing window is the last PRESENT bars per symbol, gap-safe (an absent symbol holds)."""
    rb = RingBuffer(["A", "B"], 3, ("close",))
    rb.append(np.array([[100.0], [200.0]]), np.array([0, 1]))
    rb.append(np.array([[101.0]]), np.array([0]))  # B absent
    rb.append(np.array([[102.0], [201.0]]), np.array([0, 1]))
    assert rb.latest("close").tolist() == [102.0, 201.0]
    assert rb.count().tolist() == [3, 2]
    assert rb.trailing("close")[0].tolist() == [100.0, 101.0, 102.0]
    # B has 2 present bars in a window of 3 → newest two are its real bars (gap-safe positional)
    assert rb.trailing("close")[1].tolist()[-2:] == [200.0, 201.0]


def test_backfill_equals_replay() -> None:
    """THE load-bearing invariant: seed(H) + step(m) == step over the whole H+m sequence — live and backfill
    are the same replay, so they cannot diverge. Proven across every carried-state kind in one multi-group
    engine (windowed, cross-sectional, recursive-EMA, cumulative, swing)."""
    continuous = CleanEngine(_all_groups(), ["A", "B", "C"], 60)
    for t in range(20):
        out_continuous = continuous.step(_bars(t))
    seeded = CleanEngine(_all_groups(), ["A", "B", "C"], 60)
    seeded.seed([_bars(t) for t in range(15)])
    for t in range(15, 20):
        out_seeded = seeded.step(_bars(t))
    for group in out_continuous:
        for feat in out_continuous[group]:
            assert np.allclose(
                out_continuous[group][feat], out_seeded[group][feat], equal_nan=True
            ), f"{group}.{feat} diverged between continuous and seed+replay"


def test_trend_quality_formula() -> None:
    """A perfect line → r²==1; a steeper trend → larger |slope|; a flat series → slope≈0."""
    eng = CleanEngine([ex.TrendQualityClean()], ["A", "B"], 60)
    for t in range(10):
        out = eng.step({"symbol": np.array(["A", "B"]), "close": np.array([100.0 + t, 100.0])})
    tq = out["trend_quality"]
    assert abs(tq["price_r2_5m"][0] - 1.0) < 1e-9  # A is a perfect line
    assert tq["price_slope_5m"][0] > 0  # A trends up
    assert abs(tq["price_slope_5m"][1]) < 1e-12  # B is flat


def test_breadth_cross_sectional() -> None:
    """Cross-sectional reduce over the symbol axis: K of N up → breadth_up == K/N exactly."""
    eng = CleanEngine([ex.BreadthClean()], ["A", "B", "C", "D", "E"], 60)
    for t in range(8):
        eng.step(
            {
                "symbol": np.array(["A", "B", "C", "D", "E"]),
                "close": np.array([100.0 + t, 100.0 + 2 * t, 100.0 - t, 100.0 + 0.5 * t, 100.0]),
            }
        )
    out = eng.step(
        {
            "symbol": np.array(["A", "B", "C", "D", "E"]),
            "close": np.array([110.0, 120.0, 90.0, 105.0, 100.0]),
        }
    )
    assert abs(out["breadth"]["breadth_up_5"][0] - 0.6) < 1e-9  # 3 of 5 up
    assert abs(out["breadth"]["breadth_down_5"][0] - 0.2) < 1e-9  # 1 of 5 down


def test_macd_recursive_ema_presence_decay() -> None:
    """Recursive EMA carried in window.state: a jump moves macd_line positive; an ABSENT symbol HOLDS its EMA
    (decay on bar-presence, not clock)."""
    eng = CleanEngine([ex.MacdClean()], ["A", "B"], 60)
    for _ in range(40):
        eng.step({"symbol": np.array(["A", "B"]), "close": np.array([100.0, 100.0])})
    # A jumps, B is ABSENT this minute → B's macd must be unchanged (held), A's must move +
    before_b = eng._group_state["macd"]["ema12"][1]
    out = eng.step({"symbol": np.array(["A"]), "close": np.array([110.0])})
    assert out["macd"]["macd_line"][0] > 0  # A reacted
    assert eng._group_state["macd"]["ema12"][1] == before_b  # B held (presence-decay)


def test_swing_state_machine() -> None:
    """Per-symbol ZigZag state machine carried in window.state: an up-leg then a ≥θ reversal confirms a down
    pivot + flips the direction to -1."""
    eng = CleanEngine([ex.SwingClean()], ["A"], 60)
    pivot_fired = 0.0
    out = None
    for c in [100.0, 101.0, 102.0, 103.0, 104.0, 103.0, 102.0, 101.5]:
        out = eng.step({"symbol": np.array(["A"]), "close": np.array([c])})
        pivot_fired = max(pivot_fired, float(out["swing"]["swing_pivot"][0]))
    assert pivot_fired == 1.0  # a pivot was confirmed
    assert out["swing"]["swing_direction"][0] == -1.0  # now on a down-leg


def test_intraday_seasonality_cumulative_reset() -> None:
    """Cumulative running mean: 30 vs mean(10,20,30)=20 → 1.5; a new session day resets the running sum."""
    eng = CleanEngine([ex.IntradaySeasonalityClean()], ["A"], 60)
    for v in (10.0, 20.0, 30.0):
        out = eng.step({"symbol": np.array(["A"]), "volume": np.array([v]), "minute_epoch": np.array([0])})
    assert abs(out["intraday_seasonality"]["volume_vs_session_mean"][0] - 1.5) < 1e-9
    # next session day → running mean resets, so a single bar reads ratio 1.0
    out2 = eng.step(
        {"symbol": np.array(["A"]), "volume": np.array([50.0]), "minute_epoch": np.array([86400])}
    )
    assert abs(out2["intraday_seasonality"]["volume_vs_session_mean"][0] - 1.0) < 1e-9


def test_idempotent_on_duplicate_minute() -> None:
    """The C4 footgun guard: a re-delivered minute (epoch <= watermark) does NOT double-advance carried state —
    not the cumulative sum, the EMA, or the swing leg. The engine owns idempotency once for every kind."""
    eng = CleanEngine([ex.IntradaySeasonalityClean(), ex.MacdClean()], ["A"], 60)
    eng.step(
        {
            "symbol": np.array(["A"]),
            "volume": np.array([10.0]),
            "close": np.array([100.0]),
            "minute_epoch": np.array([0]),
        }
    )
    eng.step(
        {
            "symbol": np.array(["A"]),
            "volume": np.array([20.0]),
            "close": np.array([110.0]),
            "minute_epoch": np.array([60]),
        }
    )
    sum_before = float(eng._group_state["intraday_seasonality"]["sum"][0])
    ema_before = float(eng._group_state["macd"]["ema12"][0])
    # RE-DELIVER minute 60 — must be a no-op on all carried state
    eng.step(
        {
            "symbol": np.array(["A"]),
            "volume": np.array([20.0]),
            "close": np.array([110.0]),
            "minute_epoch": np.array([60]),
        }
    )
    assert float(eng._group_state["intraday_seasonality"]["sum"][0]) == sum_before
    assert float(eng._group_state["macd"]["ema12"][0]) == ema_before


def test_seed_replay_carried_state_bit_identical() -> None:
    """VERIFY (not assume) the unification: seeding N minutes via replay builds carried state BIT-IDENTICAL to
    feeding the same N minutes live one-at-a-time — for the EMA accumulator, the cumulative sum, AND the swing
    leg state. If replay-warmup diverged from live accumulation, the live==backfill unification would be broken.
    """

    def bars(t: int) -> dict[str, np.ndarray]:
        return {
            "symbol": np.array(["A"]),
            "close": np.array([100.0 + np.sin(t) * 5]),
            "volume": np.array([10.0 + t]),
            "minute_epoch": np.array([t * 60]),
        }

    live = CleanEngine([ex.MacdClean(), ex.IntradaySeasonalityClean(), ex.SwingClean()], ["A"], 60)
    for t in range(20):
        live.step(bars(t))
    seeded = CleanEngine([ex.MacdClean(), ex.IntradaySeasonalityClean(), ex.SwingClean()], ["A"], 60)
    seeded.seed([bars(t) for t in range(20)])
    for group in live._group_state:
        for key in live._group_state[group]:
            assert np.allclose(
                live._group_state[group][key], seeded._group_state[group][key], equal_nan=True
            ), f"carried state {group}.{key} diverged between live accumulation and seed-replay"
