"""G5: the overnight-beta loop must be IDLE when there's nothing to do — no 300-name beta-panel recompute
every 60s cycle through the close window / when disabled. Two guarantees:

  - the cheap pre-gate (kill switch, market hours, close-auction window, already-entered) short-circuits
    the expensive trailing-panel load when we could not possibly trade this cycle (the G5 hot-spin); and
  - the panel/legs computation is CACHED per UTC date, so even inside the close window the OLS runs once
    per day, not on every cycle.
"""

from __future__ import annotations

import datetime as dt

import numpy as np

from strategies.lib.overnight_beta_model import OvernightBetaModel
from strategies.overnight_beta.strategy import (
    OvernightBetaConfig,
    OvernightBetaStrategy,
    PanelLoader,
)


class _CountingPanel(PanelLoader):
    """A panel loader that counts load() calls — to prove the expensive load is short-circuited / cached.

    Returns a deterministic 5-name beta panel so select_legs yields a real long+short book."""

    def __init__(self) -> None:
        self.load_calls = 0

    def load(self) -> tuple[dict[str, np.ndarray], np.ndarray]:
        self.load_calls += 1
        rng = np.random.default_rng(0)
        market = rng.standard_normal(40)
        betas = {"LO": 0.2, "L2": 0.6, "MID": 1.0, "H2": 1.4, "HI": 1.8}
        returns = {name: market * beta + rng.standard_normal(40) * 0.01 for name, beta in betas.items()}
        return returns, market

    def last_close(self, symbol: str) -> float:
        return 50.0

    def last_open(self, symbol: str) -> float:
        return 50.0


class _Clock:
    def __init__(self, is_open: bool, minutes_to_close: float | None) -> None:
        self.is_open = is_open
        if minutes_to_close is None:
            self.next_close = None
        else:
            self.next_close = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=minutes_to_close)


class _FakeTrading:
    """A trading client that only serves a configurable clock (the loop's only broker call on the no-trade
    path) and never accepts an order in these tests."""

    def __init__(self, clock: _Clock) -> None:
        self._clock = clock

    def get_clock(self) -> _Clock:
        return self._clock


class _NoopStore:
    def count_entered(self) -> int:
        return 0

    def list_entered(self) -> list[dict[str, object]]:
        return []


def _config(**overrides: object) -> OvernightBetaConfig:
    base: dict[str, object] = {
        "notional_usd": 100.0,
        "max_names_per_leg": 20,
        "max_gross_notional_usd": 5000.0,
        "rebalance_days": 21,
        "beta_window": 20,
        "quantile": 0.2,
        "enabled": True,
        "exclude": (),
        "loop_sleep_sec": 60,
    }
    base.update(overrides)
    return OvernightBetaConfig(**base)  # type: ignore[arg-type]


def _strategy(config: OvernightBetaConfig, clock: _Clock, panel: _CountingPanel) -> OvernightBetaStrategy:
    return OvernightBetaStrategy(
        config,
        _FakeTrading(clock),  # type: ignore[arg-type]
        _NoopStore(),  # type: ignore[arg-type]
        OvernightBetaModel(beta_window=config.beta_window, quantile=config.quantile),
        panel,
    )


def test_disabled_does_not_load_panel() -> None:
    """Kill switch off -> the pre-gate blocks BEFORE the panel load: zero parquet fan-out (the steady
    state for OBETA_ENABLED=0, and the bug's worst case — load every cycle with nothing to do)."""
    panel = _CountingPanel()
    strategy = _strategy(_config(enabled=False), _Clock(is_open=True, minutes_to_close=5.0), panel)
    for _ in range(10):
        strategy.maybe_enter_overnight()
    assert panel.load_calls == 0


def test_outside_close_window_does_not_load_panel() -> None:
    """Mid-session (60 min to close, not the auction window) -> the pre-gate short-circuits the panel load
    even with the kill switch ON. The loop is idle until the close-auction window."""
    panel = _CountingPanel()
    strategy = _strategy(_config(enabled=True), _Clock(is_open=True, minutes_to_close=60.0), panel)
    for _ in range(10):
        strategy.maybe_enter_overnight()
    assert panel.load_calls == 0


def test_market_closed_does_not_load_panel() -> None:
    """Market closed (weekend / off-hours) -> no panel load. This is the exact live condition under which
    the strategy was pegging ~2 cores: it must be idle."""
    panel = _CountingPanel()
    strategy = _strategy(_config(enabled=True), _Clock(is_open=False, minutes_to_close=None), panel)
    for _ in range(10):
        strategy.maybe_enter_overnight()
    assert panel.load_calls == 0


def test_panel_loaded_once_per_day_in_close_window() -> None:
    """Inside the close-auction window the legs are computed ONCE and cached per day: across many 60s
    cycles the expensive panel load runs at most once, not every cycle (the G5 within-window recompute)."""
    panel = _CountingPanel()
    strategy = _strategy(_config(enabled=True), _Clock(is_open=True, minutes_to_close=5.0), panel)
    # _select_legs is the cached entry point; call it directly across many cycles within the same day.
    first = strategy._select_legs()  # type: ignore[attr-defined]
    assert first is not None
    for _ in range(20):
        strategy._select_legs()  # type: ignore[attr-defined]
    assert panel.load_calls == 1  # loaded once, then served from the per-day cache


def test_legs_cache_recomputes_on_new_day() -> None:
    """The per-day cache is keyed on the UTC date, so a new session recomputes (the panel rolls daily)."""
    panel = _CountingPanel()
    strategy = _strategy(_config(enabled=True), _Clock(is_open=True, minutes_to_close=5.0), panel)
    strategy._select_legs()  # type: ignore[attr-defined]
    assert panel.load_calls == 1
    # simulate a new trading day: stale the cache date so the next call recomputes.
    strategy._legs_cache_date = dt.date(2020, 1, 1)  # type: ignore[attr-defined]
    strategy._select_legs()  # type: ignore[attr-defined]
    assert panel.load_calls == 2
