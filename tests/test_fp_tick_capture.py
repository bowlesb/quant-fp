"""The live tick-capture layer == a batch pass over the same ticks (the parity guarantee at the tick layer).

Threading TickState per symbol across minutes is what makes minute-by-minute LIVE aggregation identical
to what the backfiller computes from the full ordered tick history — so the trade/quote columns the
features see live equal those backfill produces. This pins that.
"""
from __future__ import annotations

from quantlib.aggregates import QuoteTick, TickState, TradeTick, aggregate_trades
from quantlib.features.tick_capture import TICK_COLUMNS, enrich_bars_with_ticks


def _trades(prices: list[float]) -> list[TradeTick]:
    return [TradeTick(ts_epoch=float(i), price=p, size=10.0) for i, p in enumerate(prices)]


def test_enrich_adds_all_tick_columns_and_preserves_bar() -> None:
    bars = [{"S": "AAA", "c": 100.0}]
    trades = {"AAA": _trades([100.0, 101.0])}
    quotes = {"AAA": [QuoteTick(0.0, bid=99.0, ask=101.0, bid_size=5.0, ask_size=3.0)]}
    enriched = enrich_bars_with_ticks(bars, trades, quotes, {})[0]
    assert enriched["c"] == 100.0  # bar fields preserved
    assert set(TICK_COLUMNS) <= set(enriched)
    assert enriched["n_trades"] == 2.0
    assert enriched["quote_imbalance"] == (5.0 - 3.0) / (5.0 + 3.0)


def test_threaded_state_matches_batch_across_minutes() -> None:
    """The sign carried across a minute boundary (a zero-tick inherits the prior sign) must match a
    single batch pass — the whole reason TickState is threaded, not reset per minute."""
    minute1 = [101.0, 100.0]  # up then down -> ends last_price=100, last_sign=-1
    minute2 = [100.0, 100.0]  # zero-ticks: inherit -1 (sell) when threaded; would be +1 (buy) if reset

    states: dict[str, TickState] = {}
    enrich_bars_with_ticks([{"S": "AAA"}], {"AAA": _trades(minute1)}, {}, states)
    live_minute2 = enrich_bars_with_ticks([{"S": "AAA"}], {"AAA": _trades(minute2)}, {}, states)[0]

    # batch truth: classify the FULL ordered sequence, then take minute2's bucket
    batch_state = TickState()
    aggregate_trades(_trades(minute1), batch_state)
    batch_minute2 = aggregate_trades(_trades(minute2), batch_state)

    assert live_minute2["signed_volume"] == batch_minute2.signed_volume == -20.0  # both sell, threaded


def test_tradeless_minute_is_zero_not_missing() -> None:
    enriched = enrich_bars_with_ticks([{"S": "AAA", "c": 100.0}], {}, {}, {})[0]
    assert enriched["n_trades"] == 0.0
    assert enriched["signed_volume"] == 0.0
