"""Unit tests for raw_backfill.fetched_tiers — the reconcile tier-scoping.

run() reconciles only the tiers it will actually fetch, so a quotes-only deepen/widen job no longer globs
the bars (>20M partitions) + trades manifests it never touches. bars are always fetched (the ranking
substrate); trades/quotes are fetched only when their top_* budget is positive.
"""

from __future__ import annotations

import datetime as dt

from quantlib.data.raw_backfill import BackfillConfig, fetched_tiers


def _config(top_trades: int, top_quotes: int) -> BackfillConfig:
    return BackfillConfig(
        store="/store",
        months=6,
        top_trades=top_trades,
        top_quotes=top_quotes,
        budget_bytes=10**12,
        symbols=None,
        days=None,
        start=dt.date(2024, 12, 12),
        end=dt.date(2026, 6, 18),
        max_workers=4,
        bars_symbols_per_request=50,
        bars_chunk_days=5,
        trades_chunk_days=1,
        quotes_chunk_days=1,
        processes=1,
        threads_per_process=4,
    )


def test_quotes_only_job_skips_trades() -> None:
    # The sector-ETF / B4-widen / G5-deepen pattern: --top-trades 0 --top-quotes N. Reconciles bars+quotes,
    # NOT trades (the 756k-row trades manifest is never globbed).
    assert fetched_tiers(_config(top_trades=0, top_quotes=5000)) == ("bars", "quotes")


def test_full_job_reconciles_all_tiers() -> None:
    # A normal FULL/DAILY run fetches every tier, so no reconcile is skipped (no regression in the dedup
    # guard for a tier that IS being fetched).
    assert fetched_tiers(_config(top_trades=1500, top_quotes=300)) == (
        "bars",
        "trades",
        "quotes",
    )


def test_trades_only_job_skips_quotes() -> None:
    assert fetched_tiers(_config(top_trades=1500, top_quotes=0)) == ("bars", "trades")


def test_bars_always_fetched() -> None:
    # Even with both tick budgets zero, bars are the ranking substrate and are always fetched+reconciled.
    assert fetched_tiers(_config(top_trades=0, top_quotes=0)) == ("bars",)


def test_fetched_tiers_preserves_canonical_order() -> None:
    tiers = fetched_tiers(_config(top_trades=10, top_quotes=10))
    assert tiers == tuple(t for t in ("bars", "trades", "quotes") if t in tiers)
