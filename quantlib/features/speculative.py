"""Speculative / anticipatory pre-compute — the two-phase per-minute tick aggregation (axis D lever).

Ben's latency lever (design + eval + prototype: ``docs/SPECULATIVE_PRECOMPUTE.md`` / PR #378), promoted here
to the PRODUCTION mechanism. The structural opening: trades/quotes stream into the reader's buffers LIVE
during the minute, but the official OHLCV BAR is the late input (dispatched ≈ T+60+δ when the next minute's
first bar lands), and the shard worker is IDLE on ``queue.get()`` in between. That idle window is free compute
budget. Speculative pre-compute spends it on the work whose inputs are already in: the per-symbol tick
AGGREGATION for the Class-A-PRE tick groups (``trade_flow`` / ``quote_spread`` / ``count_fano`` /
``trade_freq_z`` and the A-PRE-partial ``liquidity`` / ``signed_trade_ratio``).

It is NOT a disjoint path (Ben's state-abstraction mandate). It reuses the SAME parity-true
``aggregate_shard_ticks`` seam (``enrich_bars_with_ticks`` / ``trades_frame`` over
``quantlib.aggregates``): the single ``fold`` (the per-symbol tick partition + aggregate) is run in TWO
installments instead of one —

  1. PRE-PASS at ~T−1s, OFF the critical path: partition the ALREADY-ARRIVED ticks of minute T by symbol
     (the O(n) firehose-distribution work that dominates ``aggregate_shard_ticks``) and hold those ordered
     per-symbol lists as speculative state. NO aggregation arithmetic is finalized here, and crucially NO
     window-sum advance (see the parity hazard below).
  2. TAIL-FOLD + EMIT at T, ON the critical path: partition ONLY the last-ε tail ticks, APPEND them in order
     onto the speculative per-symbol lists, then run the EXISTING ``enrich_bars_with_ticks`` /
     ``trades_frame`` over the combined lists — the same aggregation, the same ``TickState`` threading, the
     same buffer the non-speculative path builds at the bar.

THE PARITY GUARANTEE (value-identical, or it is worthless). Partitioning ``partial ∪ tail`` by symbol in
arrival order is byte-identical to partitioning ``partial`` then appending the partition of ``tail`` —
``route``/``bucket`` are order-stable and per-tick-pure. So the combined per-symbol ordered lists this module
hands to ``enrich_bars_with_ticks`` are CELL-FOR-CELL the lists the non-speculative ``aggregate_shard_ticks``
would build at the bar. The aggregation (sign-classification with the threaded ``TickState``, sums,
sort-percentiles, means) then runs once over identical inputs → identical outputs.

THE PARITY HAZARD #378 caught and this module REJECTS by construction: do NOT advance the trailing window
sums speculatively as ``running − expiring + partial + tail``. That difference of large near-equal running
sums is the catastrophic-cancellation class (~1e-10 drift, the NO-GO that parks 8 reduction groups). This
module speculates ONLY the per-minute tick AGGREGATION (the partition + the single at-bar aggregate over the
combined tape); the window sums are still emitted at the bar over the identical ``minute_agg`` buffer by the
group's unchanged ``emit``. Nothing this module does touches the trailing-window arithmetic.

Gated behind ``FP_SPECULATIVE=1`` (default OFF). When OFF the worker takes the unchanged one-installment path
(``aggregate_shard_ticks``); when ON the result is value-identical (a wall-clock scheduling change only) →
fingerprint-neutral, same de-risk ladder as ``FP_INCREMENTAL`` / ``FP_SWING_STATEFUL``.

Honest scope (``docs/SPECULATIVE_PRECOMPUTE.md`` §2): the live value is breadth-gated. At the current
~24-symbol tick canary the moved-off-path work is sub-ms; it becomes meaningful only as ``FP_TICK_SYMBOLS``
widens toward the universe (the #388 ramp). This is built validated-ready so it is live the instant the
breadth makes it worth more than sub-ms — design now, pull when breadth pays.
"""
from __future__ import annotations

import os
from collections import defaultdict
from dataclasses import dataclass, field

import polars as pl

from quantlib.aggregates import QuoteTick, TickState, TradeTick, bucket_minute
from quantlib.features.tick_capture import enrich_bars_with_ticks, trades_frame

# The minute-T tick groups this mechanism speculates (the Class-A-PRE targets, ``docs/SPECULATIVE_PRECOMPUTE``
# §6). All consume only the per-symbol tick partition built here — fully A-PRE; ``liquidity`` /
# ``signed_trade_ratio`` are A-PRE-partial (their bar-volume term finalizes at the bar in the group's own
# emit, never speculated here). Documentation/observability only; the mechanism is group-agnostic (it
# produces the identical ``minute_agg`` buffer the groups read), so widening this set needs no code change.
SPECULATIVE_TARGET_GROUPS: tuple[str, ...] = (
    "trade_flow",
    "quote_spread",
    "count_fano",
    "trade_freq_z",
    "liquidity",
    "signed_trade_ratio",
)


def speculative_enabled() -> bool:
    """``FP_SPECULATIVE=1`` flips the worker onto the two-phase pre-pass + tail-fold schedule. DEFAULT OFF —
    with nothing set the worker takes the unchanged one-installment ``aggregate_shard_ticks`` path, so the
    flag is byte-identical-when-off (no deploy risk) and value-identical-when-on (a scheduling change only),
    hence fingerprint-neutral. Read each minute (cheap) so ops can flip it on a relaunch without a code
    change, the same contract as ``FP_INCREMENTAL``."""
    return os.environ.get("FP_SPECULATIVE") == "1"


def _bucket_trade(trade: dict, minute_epoch: int) -> TradeTick | None:
    """A normalized reader trade dict (S/p/s/ts_epoch) → a ``TradeTick`` IFF it buckets to ``minute_epoch``,
    else ``None``. Bucketing on the exchange ts (``bucket_minute``), NOT wall-clock, is what makes a tick
    that arrives in wall-clock T−ε but carries a next-minute exchange ts land in the right minute regardless
    of WHEN the pre-pass ran — the boundary-tick correctness #378 §4 requires."""
    if bucket_minute(trade["ts_epoch"]) != minute_epoch:
        return None
    return TradeTick(ts_epoch=trade["ts_epoch"], price=trade["p"], size=trade["s"])


def _bucket_quote(quote: dict, minute_epoch: int) -> QuoteTick | None:
    """A normalized reader quote dict (S/bp/ap/bs/as/ts_epoch) → a ``QuoteTick`` IFF it buckets to
    ``minute_epoch``, else ``None``. Same exchange-ts bucketing as ``_bucket_trade``."""
    if bucket_minute(quote["ts_epoch"]) != minute_epoch:
        return None
    return QuoteTick(
        ts_epoch=quote["ts_epoch"],
        bid=quote["bp"],
        ask=quote["ap"],
        bid_size=quote["bs"],
        ask_size=quote["as"],
    )


@dataclass
class SpeculativeMinuteState:
    """The speculative pre-pass result for ONE minute: the partial ticks already partitioned per symbol.

    This is the ``FeatureState`` of the two-installment fold — the pre-pass ``fold(partial)`` populates it
    OFF the critical path; the tail-fold ``fold(tail)`` extends the SAME ordered lists ON the critical path.
    It carries NO running window sums and NO finalized aggregate — only the ordered raw ticks — so it cannot
    drift (there is no running arithmetic to cancel). The aggregation runs once, at the bar, over the
    combined lists."""

    minute_epoch: int
    trades_by_symbol: dict[str, list[TradeTick]] = field(default_factory=lambda: defaultdict(list))
    quotes_by_symbol: dict[str, list[QuoteTick]] = field(default_factory=lambda: defaultdict(list))
    n_partial_trades: int = 0
    n_partial_quotes: int = 0

    def fold_ticks(self, trades: list[dict], quotes: list[dict]) -> None:
        """Partition a slice of reader tick dicts (the partial set in the pre-pass, or the tail at the bar)
        onto the per-symbol lists, IN ARRIVAL ORDER. Idempotent in shape: calling it with ``partial`` then
        ``tail`` yields the identical per-symbol ordered lists as one call over ``partial + tail`` — the
        parity invariant the tail-fold relies on. Off-minute ticks (wrong exchange-ts bucket) are dropped,
        exactly as ``aggregate_shard_ticks`` drops them."""
        for trade in trades:
            tick = _bucket_trade(trade, self.minute_epoch)
            if tick is not None:
                self.trades_by_symbol[trade["S"]].append(tick)
                self.n_partial_trades += 1
        for quote in quotes:
            qtick = _bucket_quote(quote, self.minute_epoch)
            if qtick is not None:
                self.quotes_by_symbol[quote["S"]].append(qtick)
                self.n_partial_quotes += 1


def prepass_aggregate(trades: list[dict], quotes: list[dict], minute_epoch: int) -> SpeculativeMinuteState:
    """PRE-PASS (idle window, OFF the critical path): partition the ALREADY-ARRIVED minute-T ticks by symbol
    into a ``SpeculativeMinuteState``. This is the O(n) firehose-distribution share of ``aggregate_shard_ticks``
    moved off-path. It does NOT aggregate, does NOT touch ``TickState``, and does NOT advance any window sum —
    so it is drift-proof. Run it in the worker's pre-bar idle window over the partial tape; the tail-fold at
    the bar finishes the job."""
    state = SpeculativeMinuteState(minute_epoch=minute_epoch)
    state.fold_ticks(trades, quotes)
    return state


def tail_fold_aggregate(
    state: SpeculativeMinuteState,
    bars: list[dict],
    tail_trades: list[dict],
    tail_quotes: list[dict],
    tick_states: dict[str, TickState],
) -> tuple[list[dict], pl.DataFrame]:
    """TAIL-FOLD + EMIT (at the bar, ON the critical path): append ONLY the last-ε tail ticks onto the
    speculative per-symbol lists (in order), then run the EXISTING ``enrich_bars_with_ticks`` /
    ``trades_frame`` over the combined tape — the SAME aggregation, the SAME threaded ``TickState``, the SAME
    ``minute_agg`` buffer the non-speculative ``aggregate_shard_ticks`` builds at the bar.

    Returns the identical ``(enriched_bars, trades_frame)`` tuple ``aggregate_shard_ticks`` returns —
    cell-for-cell, because the combined ordered per-symbol lists are byte-identical to partitioning the whole
    minute at once. The only work left ON the critical path is partitioning the tail (~1% of the minute's
    ticks) plus the single at-bar aggregation; the bulk partition was done off-path in the pre-pass.

    ``tick_states`` is the worker's per-symbol ``TickState`` threaded across minutes (live == batch); it is
    consumed HERE at the bar, NOT in the pre-pass, so the threaded sign-classification sees the minute's ticks
    in exactly the order and at exactly the instant the non-speculative path would. The window sums are NOT
    touched here — they are emitted by the group's unchanged ``emit`` over this returned buffer."""
    state.fold_ticks(tail_trades, tail_quotes)
    enriched = enrich_bars_with_ticks(
        bars, dict(state.trades_by_symbol), dict(state.quotes_by_symbol), tick_states
    )
    return enriched, trades_frame(dict(state.trades_by_symbol))
