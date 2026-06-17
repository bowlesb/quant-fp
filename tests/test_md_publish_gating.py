"""The md: publish env-gates in real_capture: both flags default OFF (so the live capture path is
unchanged until set), and the firehose allowlist (FP_TICK_SYMBOLS) intersects with the tick-subscribed
set (we never firehose a symbol we don't receive). Pure env logic — no infra, no producer."""
from __future__ import annotations

import pytest

from quantlib.features.real_capture import (
    md_publish_enabled,
    md_tick_symbols,
    tick_publish_enabled,
)


def test_flags_default_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FP_PUBLISH_MD", raising=False)
    monkeypatch.delenv("FP_PUBLISH_TICKS", raising=False)
    assert md_publish_enabled() is False
    assert tick_publish_enabled() is False


def test_flags_on_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FP_PUBLISH_MD", "1")
    monkeypatch.setenv("FP_PUBLISH_TICKS", "1")
    assert md_publish_enabled() is True
    assert tick_publish_enabled() is True


def test_firehose_allowlist_empty_means_all_subscribed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FP_TICK_SYMBOLS", raising=False)
    subscribed = {"AAPL", "MSFT", "NVDA"}
    assert md_tick_symbols(subscribed) == subscribed


def test_firehose_allowlist_all_means_all_subscribed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FP_TICK_SYMBOLS", "all")
    subscribed = {"AAPL", "MSFT"}
    assert md_tick_symbols(subscribed) == subscribed


def test_firehose_allowlist_intersects_with_subscribed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FP_TICK_SYMBOLS", "AAPL, TSLA, ZZZZ")
    subscribed = {"AAPL", "MSFT", "NVDA"}
    # TSLA/ZZZZ are not subscribed -> excluded; only AAPL survives.
    assert md_tick_symbols(subscribed) == {"AAPL"}


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
