"""Per-minute aggregation of raw trade and quote ticks.

These pure functions are the parity cornerstone: the live ingestor calls them on
each minute's buffered ticks, and the historical backfiller calls them on REST
tick data. Same inputs -> same outputs, by construction. No wall-clock reads.

Trade-sign classification uses the tick rule with state threaded across minutes
(an uptick is buyer-initiated, a downtick seller-initiated, a zero-tick carries
the previous sign). Threading `TickState` across calls is what makes minute-by-
minute live aggregation identical to a batch pass over the same ordered ticks.
"""
from dataclasses import dataclass, field


@dataclass
class TradeTick:
    ts_epoch: float          # seconds since epoch, UTC; no tz logic needed here
    price: float
    size: float


@dataclass
class QuoteTick:
    ts_epoch: float
    bid: float
    ask: float
    bid_size: float
    ask_size: float


@dataclass
class TickState:
    """Carries cross-minute trade context so live == batch."""

    last_price: float | None = None
    last_sign: int = 1       # bias used only before any price history exists


@dataclass
class TradeAgg:
    signed_volume: float
    buy_volume: float
    sell_volume: float
    large_print_cnt: int
    trade_intensity: float   # trades per second over the bucket
    median_size: float
    p95_size: float
    n_trades: int


@dataclass
class QuoteAgg:
    mean_spread_bps: float
    median_spread_bps: float
    mean_bid_size: float
    mean_ask_size: float
    quote_imbalance: float
    n_quotes: int


def _percentile(sorted_values: list[float], pct: float) -> float:
    """Linear-interpolated percentile of an already-sorted, non-empty list."""
    if len(sorted_values) == 1:
        return sorted_values[0]
    rank = pct / 100.0 * (len(sorted_values) - 1)
    low = int(rank)
    high = min(low + 1, len(sorted_values) - 1)
    frac = rank - low
    return sorted_values[low] * (1 - frac) + sorted_values[high] * frac


def classify_sign(price: float, state: TickState) -> int:
    if state.last_price is None or price == state.last_price:
        sign = state.last_sign
    else:
        sign = 1 if price > state.last_price else -1
    state.last_price = price
    state.last_sign = sign
    return sign


def aggregate_trades(
    ticks: list[TradeTick],
    state: TickState,
    bucket_seconds: int = 60,
    large_print_threshold: float = 10000.0,
) -> TradeAgg:
    """Aggregate one bucket of trades. Mutates `state` so the caller can thread
    it into the next bucket — required for live/batch parity. Ticks must be in
    time order. Empty buckets are a real condition (a tradeless minute), not an
    error, and yield an all-zero aggregate."""
    buy_volume = 0.0
    sell_volume = 0.0
    large_print_cnt = 0
    sizes: list[float] = []
    for tick in ticks:
        sign = classify_sign(tick.price, state)
        if sign > 0:
            buy_volume += tick.size
        else:
            sell_volume += tick.size
        if tick.size >= large_print_threshold:
            large_print_cnt += 1
        sizes.append(tick.size)

    n_trades = len(sizes)
    if n_trades == 0:
        return TradeAgg(0.0, 0.0, 0.0, 0, 0.0, 0.0, 0.0, 0)

    sizes.sort()
    return TradeAgg(
        signed_volume=buy_volume - sell_volume,
        buy_volume=buy_volume,
        sell_volume=sell_volume,
        large_print_cnt=large_print_cnt,
        trade_intensity=n_trades / bucket_seconds,
        median_size=_percentile(sizes, 50.0),
        p95_size=_percentile(sizes, 95.0),
        n_trades=n_trades,
    )


def aggregate_quotes(ticks: list[QuoteTick]) -> QuoteAgg:
    """Aggregate one bucket of quotes. Quotes with a non-positive mid or empty
    book sides are skipped for the affected metric (mathematically undefined),
    not treated as missing data."""
    spreads: list[float] = []
    imbalances: list[float] = []
    bid_sizes: list[float] = []
    ask_sizes: list[float] = []
    for tick in ticks:
        mid = (tick.bid + tick.ask) / 2.0
        if mid > 0 and tick.ask >= tick.bid:
            spreads.append((tick.ask - tick.bid) / mid * 10000.0)
        depth = tick.bid_size + tick.ask_size
        if depth > 0:
            imbalances.append((tick.bid_size - tick.ask_size) / depth)
        bid_sizes.append(tick.bid_size)
        ask_sizes.append(tick.ask_size)

    n_quotes = len(ticks)
    if n_quotes == 0:
        return QuoteAgg(0.0, 0.0, 0.0, 0.0, 0.0, 0)

    spreads.sort()
    return QuoteAgg(
        mean_spread_bps=sum(spreads) / len(spreads) if spreads else 0.0,
        median_spread_bps=_percentile(spreads, 50.0) if spreads else 0.0,
        mean_bid_size=sum(bid_sizes) / n_quotes,
        mean_ask_size=sum(ask_sizes) / n_quotes,
        quote_imbalance=sum(imbalances) / len(imbalances) if imbalances else 0.0,
        n_quotes=n_quotes,
    )


def bucket_minute(ts_epoch: float) -> int:
    """Floor an epoch-seconds timestamp to its minute (the bar's open second)."""
    return int(ts_epoch // 60 * 60)
