"""`BacktestExecutor` — the PRETEND-trade executor (Ben's explicit pretend-vs-actual swap).

Takes the SAME `OrderIntent`s a live executor takes, but simulates the fill over the historical panel:
the tradeable entry price (>=09:35, already enforced in the Panel build), charging the per-name
half-spread + slippage from the panel's `half_spread_bps` column. It exposes TWO paths with identical
economics:

  - `execute(intents, cs, clock)` — the per-EVENT reference (faithful to the live PaperExecutor's
    one-cross-section-at-a-time shape). Used for the parity proof + small/sequential backtests.
  - `run_vectorized(panel, score_fn)` — the FAST batch path for cross-sectional archetypes: applies the
    strategy's columnar score across ALL timestamps at once and books via `long_short_per_name_cost`
    (the per-timestamp bucketed cost model), hitting Ben's <30-60s budget. The decision LOGIC is the
    same scalar `decide`; only the application is batch-vs-streaming (the feature-platform batch/stream
    split under one shared emit).

Inherently-sequential archetypes (triple-barrier / streak) cannot use `run_vectorized` (the path
early-exit is sequential) — those are the Phase-1 Rust kernel; this executor's per-event path or a Rust
batch path serves them. See docs/STRATEGY_BATTERY_PORTABILITY.md §2.6.
"""
from __future__ import annotations

import datetime as dt

import numpy as np

from quantlib.battery.cost import cost_curve, long_short_per_name_cost
from quantlib.strategy_core import CrossSection
from quantlib.strategy_core.execution import BookState, Clock, Fill, OrderIntent

# A flat slippage added on top of the per-name half-spread (bps, one-way), the laneC/baseline default.
DEFAULT_SLIPPAGE_BPS = 1.0


class BacktestExecutor:
    """Simulated fills over the panel at the tradeable entry, charging per-name half-spread + slippage."""

    def __init__(self, *, slippage_bps: float = DEFAULT_SLIPPAGE_BPS) -> None:
        self._slippage_bps = slippage_bps
        self._book = BookState()

    def book(self) -> BookState:
        return self._book

    def execute(self, intents: list[OrderIntent], cross_section: CrossSection, clock: Clock) -> list[Fill]:
        """Per-event reference fill: book each intent at the cross-section's entry price + its per-name
        half-spread. Updates the book to the intents' target weights (a full rebalance to target)."""
        fills: list[Fill] = []
        new_weights: dict[str, float] = {}
        for intent in intents:
            price = cross_section.feature_for(intent.symbol, "entry_close")
            half_spread = cross_section.feature_for(intent.symbol, "half_spread_bps")
            cost_bps = (half_spread if np.isfinite(half_spread) else 0.0) + self._slippage_bps
            new_weights[intent.symbol] = intent.target_weight
            fills.append(
                Fill(
                    symbol=intent.symbol,
                    side=intent.side,
                    weight=intent.target_weight,
                    fill_price=float(price) if np.isfinite(price) else float("nan"),
                    cost_bps=cost_bps,
                )
            )
        self._book = BookState(weights=new_weights)
        return fills

    @staticmethod
    def run_vectorized(
        preds: list[float],
        realized: list[float],
        groups: list[dt.datetime],
        symbols: list[str],
        half_spread_bps: list[float],
        *,
        frac: float,
        periods_per_year: float,
        slippage_mult_grid: tuple[float, ...] = (0.0, 0.5, 1.0, 1.5, 2.0, 3.0),
    ) -> dict[str, object]:
        """The FAST batch backtest for the cross-sectional L/S archetype: the per-name-half-spread L/S
        P&L over the whole panel at once (no per-event Python loop), plus the cost-sensitivity curve.

        `preds` are the strategy's columnar scores (what `decide` ranks on); `realized` the forward
        excess; both bucketed per `groups` (timestamp). This IS `long_short_per_name_cost` — the
        executor adopts it as its batch path so the backtest books exactly the legs the per-event
        `decide` would, just computed columnar. Returns the economics dict + `cost_curve`."""
        result = long_short_per_name_cost(
            preds,
            realized,
            groups,
            symbols,
            half_spread_bps,
            frac=frac,
            cost_mult=1.0,
            periods_per_year=periods_per_year,
        )
        result["cost_curve"] = cost_curve(
            preds,
            realized,
            groups,
            symbols,
            half_spread_bps,
            frac=frac,
            periods_per_year=periods_per_year,
            multipliers=slippage_mult_grid,
        )
        return result
