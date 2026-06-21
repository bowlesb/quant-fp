"""Speculative pre-compute == the at-bar full aggregation, cell-for-cell (the parity gate).

The hard constraint (``docs/SPECULATIVE_PRECOMPUTE.md`` §4): the two-phase pre-pass + tail-fold MUST be
value-identical to the one-installment ``aggregate_shard_ticks``, else live != backfill and the tick groups
lose trust. This pins it the way ``test_fp_incremental`` pins the incremental fold == the batch:

  * ``speculative_aggregate_shard_ticks`` == ``aggregate_shard_ticks`` cell-for-cell over a multi-minute
    stream (no drift accumulation), at multiple tail cutoffs (T-1s / T-2s / T-5s);
  * the boundary tick (next-minute exchange ts arriving in the partial window) buckets correctly;
  * the threaded ``TickState`` sign-classification is identical whether the minute is aggregated whole or
    split partial + tail;
  * the REJECTED hazard is documented here: speculation touches NO trailing window sum, so the ~1e-10
    subtract-expiring cancellation class cannot arise — there is no running arithmetic in the speculative
    state to drift.
"""
from __future__ import annotations

import polars as pl

from quantlib.aggregates import TickState, bucket_minute
from quantlib.features.sharded_capture import (
    SPECULATIVE_TAIL_SECONDS,
    _split_partial_tail,
    aggregate_shard_ticks,
    speculative_aggregate_shard_ticks,
)
from quantlib.features.speculative import SPECULATIVE_TARGET_GROUPS, prepass_aggregate

MINUTE = 60
SYMBOLS = ["AAA", "BBB", "CCC"]


def _trade(symbol: str, minute_epoch: int, second: float, price: float, size: float) -> dict:
    return {"S": symbol, "p": price, "s": size, "ts_epoch": float(minute_epoch + second)}


def _quote(symbol: str, minute_epoch: int, second: float, bid: float, ask: float) -> dict:
    return {
        "S": symbol,
        "bp": bid,
        "ap": ask,
        "bs": 5.0,
        "as": 3.0,
        "ts_epoch": float(minute_epoch + second),
    }


def _synthetic_minute(minute_epoch: int, n_per_symbol: int) -> tuple[list[dict], list[dict], list[dict]]:
    """One minute of bars + dense trades/quotes spread across the whole [0,60) second range (so any tail
    cutoff splits a non-trivial partial/tail), with a deterministic up/down/flat price walk per symbol so
    sign-classification, percentiles, and means are all exercised."""
    bars = [{"S": symbol, "t": minute_epoch, "c": 100.0 + s_idx} for s_idx, symbol in enumerate(SYMBOLS)]
    trades: list[dict] = []
    quotes: list[dict] = []
    for s_idx, symbol in enumerate(SYMBOLS):
        base = 100.0 + s_idx
        for i in range(n_per_symbol):
            second = (i + 0.5) * (60.0 / n_per_symbol)  # strictly inside [0,60)
            price = base + ((i % 3) - 1) * 0.01  # up, flat, down walk
            size = 100.0 + (i % 5) * 50.0
            trades.append(_trade(symbol, minute_epoch, second, price, size))
            quotes.append(_quote(symbol, minute_epoch, second, bid=price - 0.02, ask=price + 0.02))
    return bars, trades, quotes


def _enriched_to_frame(enriched: list[dict]) -> pl.DataFrame:
    """Project the enriched bars to the tick columns for a cell-for-cell compare (stable symbol order)."""
    tick_cols = [
        "n_trades",
        "signed_volume",
        "mean_spread_bps",
        "quote_imbalance",
        "mean_bid_size",
        "mean_ask_size",
    ]
    rows = [{"S": bar["S"], **{c: bar[c] for c in tick_cols}} for bar in enriched]
    return pl.DataFrame(rows).sort("S")


def _max_abs_diff(left: pl.DataFrame, right: pl.DataFrame) -> float:
    """Worst absolute cell divergence over the numeric tick columns of two symbol-aligned frames."""
    worst = 0.0
    numeric = [c for c in left.columns if c != "S"]
    joined = left.join(right, on="S", suffix="_r")
    for col in numeric:
        diffs = (joined[col] - joined[f"{col}_r"]).abs()
        worst = max(worst, float(diffs.max()))
    return worst


def _run_full(
    bars: list[dict], trades: list[dict], quotes: list[dict], minute_epoch: int, states: dict[str, TickState]
) -> tuple[pl.DataFrame, pl.DataFrame]:
    enriched, trades_df = aggregate_shard_ticks(bars, trades, quotes, minute_epoch, states)
    return _enriched_to_frame(enriched), trades_df.sort(["symbol", "ts", "price", "size"])


def _run_speculative(
    bars: list[dict],
    trades: list[dict],
    quotes: list[dict],
    minute_epoch: int,
    states: dict[str, TickState],
    tail_seconds: float,
) -> tuple[pl.DataFrame, pl.DataFrame]:
    enriched, trades_df = speculative_aggregate_shard_ticks(
        bars, trades, quotes, minute_epoch, states, tail_seconds=tail_seconds
    )
    return _enriched_to_frame(enriched), trades_df.sort(["symbol", "ts", "price", "size"])


def test_value_identical_single_minute_multiple_tail_cutoffs() -> None:
    """speculative == full, max|diff| EXACTLY 0.0, at every tail cutoff — the value-identity claim."""
    minute_epoch = bucket_minute(1_700_000_400)
    bars, trades, quotes = _synthetic_minute(minute_epoch, n_per_symbol=80)
    full_enriched, full_trades = _run_full(bars, trades, quotes, minute_epoch, {})
    for tail_seconds in (1.0, 2.0, 5.0):
        spec_enriched, spec_trades = _run_speculative(
            bars, trades, quotes, minute_epoch, {}, tail_seconds=tail_seconds
        )
        assert _max_abs_diff(full_enriched, spec_enriched) == 0.0, f"tail={tail_seconds}s diverged"
        assert full_trades.equals(spec_trades), f"trades frame diverged at tail={tail_seconds}s"


def test_value_identical_multi_minute_soak_no_drift() -> None:
    """60 consecutive minutes, threaded TickState, compared cell-for-cell each minute — no drift
    accumulation (the FP_INCREMENTAL soak discipline applied to the sub-minute split)."""
    full_states: dict[str, TickState] = {}
    spec_states: dict[str, TickState] = {}
    breach_minutes = 0
    for m in range(60):
        minute_epoch = bucket_minute(1_700_000_400 + m * MINUTE)
        bars, trades, quotes = _synthetic_minute(minute_epoch, n_per_symbol=80)
        full_enriched, full_trades = _run_full(bars, trades, quotes, minute_epoch, full_states)
        spec_enriched, spec_trades = _run_speculative(
            bars, trades, quotes, minute_epoch, spec_states, tail_seconds=1.0
        )
        if _max_abs_diff(full_enriched, spec_enriched) != 0.0 or not full_trades.equals(spec_trades):
            breach_minutes += 1
    assert breach_minutes == 0, f"{breach_minutes}/60 minutes diverged (drift)"


def test_threaded_tickstate_identical_across_split() -> None:
    """The threaded TickState (carried across minutes) ends in the IDENTICAL (last_price, last_sign) whether
    a minute is aggregated whole or split partial+tail — the sign-classification parity the whole mechanism
    rests on. A zero-tick inheriting the prior sign is the sharp case."""
    minute_epoch = bucket_minute(1_700_000_400)
    # up, down, flat (zero-tick inherits 'down'): the threaded-sign sharp case for AAA.
    trades = [
        _trade("AAA", minute_epoch, 5.0, 101.0, 10.0),
        _trade("AAA", minute_epoch, 40.0, 100.0, 10.0),
        _trade("AAA", minute_epoch, 59.5, 100.0, 10.0),  # in the T-1s tail
    ]
    bars = [{"S": "AAA", "c": 100.0}]
    full_states = {"AAA": TickState()}
    spec_states = {"AAA": TickState()}
    aggregate_shard_ticks(bars, trades, [], minute_epoch, full_states)
    speculative_aggregate_shard_ticks(bars, trades, [], minute_epoch, spec_states, tail_seconds=1.0)
    assert full_states["AAA"].last_price == spec_states["AAA"].last_price
    assert full_states["AAA"].last_sign == spec_states["AAA"].last_sign


def test_boundary_tick_bucketed_by_exchange_ts_not_wallclock() -> None:
    """A tick carrying a NEXT-minute exchange ts but sitting in this minute's received batch is dropped from
    minute T by both paths (bucketed by exchange ts), and a genuine in-minute tick at T+59.9s is kept by
    both — so the split never moves a tick between minutes."""
    minute_epoch = bucket_minute(1_700_000_400)
    next_minute_tick = _trade("AAA", minute_epoch + MINUTE, 0.5, 100.0, 10.0)  # exchange ts in T+1
    in_minute_late = _trade("AAA", minute_epoch, 59.9, 101.0, 10.0)  # genuine in-minute, in the tail
    trades = [in_minute_late, next_minute_tick]
    bars = [{"S": "AAA", "c": 100.0}]
    full_enriched, _ = _run_full(bars, trades, [], minute_epoch, {})
    spec_enriched, _ = _run_speculative(bars, trades, [], minute_epoch, {}, tail_seconds=1.0)
    assert _max_abs_diff(full_enriched, spec_enriched) == 0.0
    # only the in-minute tick counts toward minute T (the next-minute tick is bucketed out by both)
    assert full_enriched.filter(pl.col("S") == "AAA")["n_trades"][0] == 1.0


def test_split_partial_tail_partitions_whole_set() -> None:
    """_split_partial_tail keeps partial+tail == the whole received set (no tick lost or duplicated) and
    routes only in-minute last-ε ticks to the tail."""
    minute_epoch = bucket_minute(1_700_000_400)
    _, trades, _ = _synthetic_minute(minute_epoch, n_per_symbol=80)
    partial, tail = _split_partial_tail(trades, minute_epoch, 1.0)
    assert len(partial) + len(tail) == len(trades)
    assert all(t["ts_epoch"] >= minute_epoch + 59.0 for t in tail)
    assert all(bucket_minute(t["ts_epoch"]) == minute_epoch for t in tail)


def test_prepass_holds_partial_without_aggregating() -> None:
    """The pre-pass partitions ticks per symbol but finalizes NO aggregate and touches NO window state —
    it is drift-proof by construction (nothing to cancel)."""
    minute_epoch = bucket_minute(1_700_000_400)
    _, trades, quotes = _synthetic_minute(minute_epoch, n_per_symbol=10)
    spec_state = prepass_aggregate(trades, quotes, minute_epoch)
    assert spec_state.n_partial_trades == len(trades)
    assert spec_state.n_partial_quotes == len(quotes)
    assert set(spec_state.trades_by_symbol) == set(SYMBOLS)


def test_target_groups_are_the_class_a_pre_set() -> None:
    """Pin the documented Class-A-PRE target set (the tick reductions + the A-PRE-partial pair)."""
    assert set(SPECULATIVE_TARGET_GROUPS) == {
        "trade_flow",
        "quote_spread",
        "count_fano",
        "trade_freq_z",
        "liquidity",
        "signed_trade_ratio",
    }


def test_default_tail_seconds_is_one() -> None:
    assert SPECULATIVE_TAIL_SECONDS == 1.0
