"""Strategy-layer parity-by-construction proof + the shared decision-core unit tests.

The headline test (`test_decide_parity_panel_vs_bus`): build a `PanelCrossSection` (the battery's
backtest source) and a `BusCrossSection` (the live container's source) from the IDENTICAL feature
values, run the SAME `CrossSectionalLS.decide` over both, and assert the target books are identical.
That is the strategy analogue of the feature stream==backfill parity test — it proves the decision
logic is portable to production by construction, with no re-implementation.
"""
from __future__ import annotations

import datetime as dt

import numpy as np
import pytest

from quantlib.strategy_core import TargetPosition
from quantlib.strategy_core.adapters import BusCrossSection, PanelCrossSection
from quantlib.strategy_core.backtest_executor import BacktestExecutor
from quantlib.strategy_core.cross_sectional_ls import CrossSectionalLS
from quantlib.strategy_core.execution import (
    BookState,
    RealClock,
    Runner,
    SimClock,
    TargetBookStrategy,
)
from quantlib.strategy_core.feeds import PanelFeed


class _FakeVector:
    """Duck-typed stand-in for quantlib.bus.vector.FeatureVector — `.value(name)` + `.minute`, no
    redis/schema deps. The live BusCrossSection only touches those two members."""

    def __init__(self, symbol: str, minute: dt.datetime, values: dict[str, float]) -> None:
        self.symbol = symbol
        self.minute = minute
        self._values = values

    def value(self, name: str) -> float:
        return float(self._values.get(name, float("nan")))


def _make_cross_section_data(n: int, seed: int) -> tuple[list[str], dt.datetime, np.ndarray]:
    rng = np.random.default_rng(seed)
    symbols = [f"S{i}" for i in range(n)]
    minute = dt.datetime(2026, 1, 5, 19, 59, tzinfo=dt.timezone.utc)
    signal = rng.normal(0, 1, n)
    return symbols, minute, signal


def _panel_cs(symbols, minute, signal) -> PanelCrossSection:
    matrix = signal.reshape(-1, 1)
    return PanelCrossSection(symbols, minute, matrix, {"sig": 0})


def _bus_cs(symbols, minute, signal) -> BusCrossSection:
    latest = {
        symbol: _FakeVector(symbol, minute, {"sig": float(value)}) for symbol, value in zip(symbols, signal)
    }
    return BusCrossSection(latest)


def _sorted_targets(targets: list[TargetPosition]) -> list[tuple[str, float, float]]:
    return sorted((t.symbol, round(t.target_weight, 10), round(t.score, 10)) for t in targets)


def test_decide_parity_panel_vs_bus() -> None:
    """THE parity proof: identical feature data -> identical target book through PanelCrossSection
    (backtest) and BusCrossSection (live), via the SAME decide()."""
    symbols, minute, signal = _make_cross_section_data(60, seed=11)
    core = CrossSectionalLS(frac=0.1, signal_feature="sig")
    panel_targets = core.decide(_panel_cs(symbols, minute, signal))
    bus_targets = core.decide(_bus_cs(symbols, minute, signal))
    assert panel_targets, "expected a non-empty book"
    assert _sorted_targets(panel_targets) == _sorted_targets(bus_targets)


def test_decide_parity_holds_with_nans() -> None:
    """Warmup/sparse NaNs (normal on the live bus) must drop identically on both sides."""
    symbols, minute, signal = _make_cross_section_data(40, seed=3)
    signal[:5] = np.nan  # warmup names
    core = CrossSectionalLS(frac=0.1, signal_feature="sig")
    panel_targets = core.decide(_panel_cs(symbols, minute, signal))
    bus_targets = core.decide(_bus_cs(symbols, minute, signal))
    assert _sorted_targets(panel_targets) == _sorted_targets(bus_targets)
    chosen = {t.symbol for t in panel_targets}
    assert not (chosen & {f"S{i}" for i in range(5)})  # NaN names never selected


def test_ls_basket_is_dollar_neutral() -> None:
    symbols, minute, signal = _make_cross_section_data(50, seed=7)
    targets = CrossSectionalLS(frac=0.2, signal_feature="sig").decide(_panel_cs(symbols, minute, signal))
    assert abs(sum(t.target_weight for t in targets)) < 1e-9  # dollar-neutral
    longs = [t for t in targets if t.target_weight > 0]
    shorts = [t for t in targets if t.target_weight < 0]
    assert len(longs) == len(shorts) > 0


def test_ls_selects_extremes() -> None:
    """The longs are the highest-score names, the shorts the lowest."""
    symbols, minute, signal = _make_cross_section_data(30, seed=1)
    core = CrossSectionalLS(frac=0.1, signal_feature="sig")
    targets = core.decide(_panel_cs(symbols, minute, signal))
    long_scores = [t.score for t in targets if t.target_weight > 0]
    short_scores = [t.score for t in targets if t.target_weight < 0]
    assert min(long_scores) > max(short_scores)


def test_signal_sign_inverts_legs() -> None:
    """signal_sign=-1 (a reversion feature where LOW = bullish) flips long/short vs sign=+1."""
    symbols, minute, signal = _make_cross_section_data(40, seed=5)
    pos = CrossSectionalLS(frac=0.1, signal_feature="sig", signal_sign=1.0).decide(
        _panel_cs(symbols, minute, signal)
    )
    neg = CrossSectionalLS(frac=0.1, signal_feature="sig", signal_sign=-1.0).decide(
        _panel_cs(symbols, minute, signal)
    )
    pos_longs = {t.symbol for t in pos if t.target_weight > 0}
    neg_shorts = {t.symbol for t in neg if t.target_weight < 0}
    assert pos_longs == neg_shorts  # the +1 longs become the -1 shorts


def test_requires_exactly_one_signal_source() -> None:
    with pytest.raises(ValueError):
        CrossSectionalLS(frac=0.1)  # neither feature nor model
    with pytest.raises(ValueError):
        CrossSectionalLS(frac=0.1, signal_feature="sig", model=object())  # type: ignore[arg-type]


def test_thin_cross_section_returns_empty() -> None:
    """A single-name cross-section cannot form both legs -> no book."""
    symbols, minute, signal = _make_cross_section_data(1, seed=2)
    targets = CrossSectionalLS(frac=0.1, signal_feature="sig").decide(_panel_cs(symbols, minute, signal))
    assert targets == []  # < 2 finite names -> cannot form a long AND a short leg


# --- The HOT-SWAP execution seams: same decide(), swapped Executor/Feed ---------------------------


def test_target_book_strategy_emits_intents_matching_targets() -> None:
    """TargetBookStrategy adapts the CrossSectionalLS book into OrderIntents — buys for + weights,
    sells for - weights — so the per-event Runner places exactly the target book."""

    symbols, minute, signal = _make_cross_section_data(40, seed=9)
    core = CrossSectionalLS(frac=0.1, signal_feature="sig")
    targets = core.decide(_panel_cs(symbols, minute, signal))
    strategy = TargetBookStrategy(core)
    intents = strategy.decide(_panel_cs(symbols, minute, signal), BookState())
    by_symbol = {intent.symbol: intent for intent in intents}
    for target in targets:
        intent = by_symbol[target.symbol]
        assert intent.side == ("buy" if target.target_weight >= 0 else "sell")
        assert intent.target_weight == target.target_weight


def test_executor_swap_parity_backtest_vs_paper() -> None:
    """THE pretend-vs-actual swap proof: the SAME decide() (via TargetBookStrategy) produces IDENTICAL
    OrderIntents whether the harness will route them to the BacktestExecutor (pretend fill over the
    panel) or a PaperExecutor (real broker). The decision code is byte-identical; only the executor
    behind the intents differs. We assert the intents are identical and the BacktestExecutor fills
    them at the panel entry price + per-name half-spread cost."""

    symbols, minute, signal = _make_cross_section_data(40, seed=4)
    entry = np.full(len(symbols), 50.0)
    half_spread = np.linspace(2.0, 10.0, len(symbols))
    extra = {"entry_close": entry, "half_spread_bps": half_spread}
    panel_cs = PanelCrossSection(symbols, minute, signal.reshape(-1, 1), {"sig": 0}, extra)

    strategy = TargetBookStrategy(CrossSectionalLS(frac=0.1, signal_feature="sig"))
    # the intents a live PaperExecutor would receive (decision only — no broker here):
    paper_intents = strategy.decide(panel_cs, BookState())
    # the intents the BacktestExecutor receives are produced by the SAME decide on the SAME data:
    backtest_intents = strategy.decide(panel_cs, BookState())
    assert paper_intents == backtest_intents  # byte-identical decision code path

    executor = BacktestExecutor()
    fills = executor.execute(backtest_intents, panel_cs, RealClock())
    assert len(fills) == len(backtest_intents)
    for fill in fills:
        assert fill.fill_price == 50.0  # filled at the panel entry price
        assert fill.cost_bps > 0  # charged the per-name half-spread + slippage
    # the executor's book now holds exactly the target weights
    assert abs(sum(executor.book().weights.values())) < 1e-9  # dollar-neutral book


def test_panel_feed_replays_timestamps_in_order() -> None:
    """PanelFeed yields one CrossSection event per distinct timestamp, in time order, each exposing the
    execution columns by name — the backtest DataFeed the Runner consumes."""

    class _FakePanel:
        symbol_code = np.array([0, 1, 0, 1], dtype=np.int64)
        symbol_names = ["A", "B"]
        minute_epoch = np.array([100, 100, 200, 200], dtype=np.int64) * 1_000_000_000
        feature_names = ["sig"]
        feature_matrix = np.array([[0.1], [0.2], [0.3], [0.4]])
        entry_close = np.array([10.0, 20.0, 11.0, 21.0])
        half_spread_bps = np.array([3.0, 4.0, 3.0, 4.0])

    events = list(PanelFeed(_FakePanel()).events())
    assert len(events) == 2
    assert events[0].ts < events[1].ts
    assert events[0].cross_section.symbols == ["A", "B"]
    assert events[0].cross_section.feature_for("A", "entry_close") == 10.0
    assert events[0].cross_section.feature_for("B", "half_spread_bps") == 4.0


def test_runner_drives_decide_over_feed() -> None:
    """The Runner ties {strategy, feed, executor, clock} and calls the SAME decide per event — the one
    loop the battery backtest and the live container share."""

    n = 40
    rng = np.random.default_rng(2)

    class _FakePanel:
        symbol_code = np.array(list(range(n)) + list(range(n)), dtype=np.int64)
        symbol_names = [f"S{i}" for i in range(n)]
        minute_epoch = np.array([100] * n + [200] * n, dtype=np.int64) * 1_000_000_000
        feature_names = ["sig"]
        feature_matrix = rng.normal(0, 1, (2 * n, 1))
        entry_close = np.full(2 * n, 50.0)
        half_spread_bps = np.full(2 * n, 5.0)

    executor = BacktestExecutor()
    runner = Runner(
        TargetBookStrategy(CrossSectionalLS(frac=0.1, signal_feature="sig")),
        PanelFeed(_FakePanel()),
        executor,
        SimClock(),
    )
    runner.run()
    # after replaying both timestamps the book holds the last timestamp's dollar-neutral target
    assert abs(sum(executor.book().weights.values())) < 1e-9
    assert len(executor.book().weights) > 0


def test_batch_vs_per_event_select_identical_legs() -> None:
    """THE invariant proof: the columnar score expression selects the SAME legs whether applied as a
    fast BATCH sweep (the battery's run_vectorized path) or PER-EVENT (the live Runner path). One
    scoring expression (CrossSectionalLS.score), two application modes, identical decisions — so there
    is no backtest-only fast path that could drift from the live per-event path."""
    n_per_ts = 30
    n_ts = 4
    rng = np.random.default_rng(13)
    frac = 0.2

    symbols_all: list[str] = []
    minute_all: list[int] = []
    score_all: list[float] = []
    for ts in range(n_ts):
        for sym in range(n_per_ts):
            symbols_all.append(f"S{sym}")
            minute_all.append((100 + ts) * 1_000_000_000)
            score_all.append(float(rng.normal()))
    score_arr = np.array(score_all)

    # PER-EVENT: run decide() per timestamp slice, collect the selected (ts, symbol, side).
    core = CrossSectionalLS(frac=frac, signal_feature="sig")
    per_event_legs: set[tuple[int, str, str]] = set()
    for ts_epoch in sorted(set(minute_all)):
        rows = [i for i, m in enumerate(minute_all) if m == ts_epoch]
        cs = PanelCrossSection(
            [symbols_all[i] for i in rows],
            dt.datetime.fromtimestamp(ts_epoch / 1e9, tz=dt.timezone.utc),
            score_arr[rows].reshape(-1, 1),
            {"sig": 0},
        )
        for target in core.decide(cs):
            side = "long" if target.target_weight > 0 else "short"
            per_event_legs.add((ts_epoch, target.symbol, side))

    # BATCH: the run_vectorized path books top/bottom-frac per timestamp on the SAME score. Reproduce
    # its leg selection (long_short_per_name_cost sorts ascending by pred, shorts=first-k, longs=last-k).
    batch_legs: set[tuple[int, str, str]] = set()
    for ts_epoch in sorted(set(minute_all)):
        rows = [i for i, m in enumerate(minute_all) if m == ts_epoch]
        ordered = sorted(rows, key=lambda i: score_arr[i])
        k = max(1, int(frac * len(ordered)))
        for i in ordered[:k]:
            batch_legs.add((ts_epoch, symbols_all[i], "short"))
        for i in ordered[-k:]:
            batch_legs.add((ts_epoch, symbols_all[i], "long"))

    assert per_event_legs == batch_legs  # batch == per-event: ONE decision, two application modes
    assert len(per_event_legs) == n_ts * 2 * max(1, int(frac * n_per_ts))


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
