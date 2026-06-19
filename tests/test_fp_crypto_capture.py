"""The 24/7 crypto capture harness reuses the EXACT production compute core (capture.process_bars) on
crypto bars/trades, selects only the universal feature groups, excludes the SPY-relative ones, and is
isolated to the fv:crypto:* bus namespace. These pin the pure pieces (no live websocket needed)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from quantlib.aggregates import bucket_minute
from quantlib.features.capture import CaptureState, process_bars
from quantlib.features.crypto_capture import (
    CRYPTO_BUS_PREFIX,
    DEFAULT_CRYPTO_SYMBOLS,
    EXCLUDED_GROUPS,
    aggregate_crypto_ticks,
    crypto_symbols,
    store_symbol,
)

# Equity-specific groups that must NEVER run on crypto (they need reference/daily snapshots or a SPY row).
_EQUITY_ONLY_GROUPS = (
    "sector", "breadth", "peer_relative", "multi_day_returns", "multi_day_vwap", "prior_day",
    "daily_beta", "overnight_beta", "market_context", "market_beta", "liquidity_rank",
)


def test_store_symbol_strips_slash() -> None:
    assert store_symbol("BTC/USD") == "BTCUSD"
    assert store_symbol("ETH/USD") == "ETHUSD"


def test_crypto_bus_prefix_is_separate_namespace() -> None:
    """The crypto bus prefix must NOT be the equity 'fv' prefix, so streams never collide."""
    assert CRYPTO_BUS_PREFIX == "fv:crypto"
    assert CRYPTO_BUS_PREFIX != "fv"


def test_default_symbols_use_slash_pairs() -> None:
    assert all("/" in pair for pair in DEFAULT_CRYPTO_SYMBOLS)
    assert "BTC/USD" in DEFAULT_CRYPTO_SYMBOLS


def test_crypto_symbols_env_override(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("FP_CRYPTO_SYMBOLS", "btc/usd, eth/usd")
    assert crypto_symbols() == ["BTC/USD", "ETH/USD"]
    monkeypatch.delenv("FP_CRYPTO_SYMBOLS")
    assert crypto_symbols() == list(DEFAULT_CRYPTO_SYMBOLS)


def _synthetic_minute(minute: datetime, symbols: list[str]) -> tuple[list[dict], list[dict]]:
    bars = [
        {"S": s, "o": 100.0 + i, "c": 100.4 + i, "h": 100.7 + i, "l": 99.5 + i, "v": 1000.0,
         "t": minute.isoformat()}
        for i, s in enumerate(symbols)
    ]
    trades = [
        {"S": s, "p": 100.0 + k * 0.01, "s": 0.5 + k * 0.05, "ts_epoch": minute.timestamp() + k}
        for s in symbols for k in range(15)
    ]
    return bars, trades


def test_compute_runs_universal_groups_and_excludes_equity(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """The shared core, fed crypto bars+trades with NO reference/daily snapshots and the SPY-relative
    groups excluded, runs the universal price/volatility/order-flow groups and NONE of the equity ones."""
    state = CaptureState()
    tick_states: dict = {}
    symbols = ["BTCUSD", "ETHUSD", "SOLUSD"]
    base = datetime(2026, 6, 19, 12, 0, tzinfo=timezone.utc)
    groups_seen: set[str] = set()
    for offset in range(4):
        minute = base + timedelta(minutes=offset)
        bars, trades = _synthetic_minute(minute, symbols)
        enriched, trades_df = aggregate_crypto_ticks(bars, trades, bucket_minute(minute.timestamp()), tick_states)
        extra = {"trades": trades_df} if trades_df.height else None
        process_bars(
            state, enriched, str(tmp_path), "real", day=None, window=300,
            snapshots=None, exclude_groups=EXCLUDED_GROUPS, extra_frames=extra,
        )
        groups_seen.update(state.group_timings.keys())

    assert state.minutes == 4
    # universal bar + order-flow groups light up on crypto
    for group in ("price_returns", "volatility", "realized_range", "candlestick", "volume",
                  "trade_flow", "signed_trade_ratio", "microstructure_burst", "tick_runlength"):
        assert group in groups_seen, f"expected universal group {group!r} to run on crypto"
    # equity-specific groups must NOT run (no snapshots) and SPY-relative ones are excluded
    leaked = [group for group in _EQUITY_ONLY_GROUPS if group in groups_seen]
    assert not leaked, f"equity-specific groups leaked into crypto compute: {leaked}"
