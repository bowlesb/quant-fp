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
from quantlib.strategy_core.cross_sectional_ls import CrossSectionalLS


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


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
