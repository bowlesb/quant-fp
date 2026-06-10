"""Backfiller + streamed-vs-REST validation.

`backfill-bars`  : pull historical 1-minute bars via REST into bars_1m as
                   source='backfill' (append-only; never overwrites stream rows).
`validate-bars`  : compare overlapping (symbol, ts) bars between source='stream'
                   and source='backfill' and report the OHLCV match rate — the
                   Phase 1 gate that proves the live feed equals historical REST.

Trade/quote-aggregate backfill (through the same quantlib functions the ingestor
uses) is the next step; bars parity is validated first.

Usage: python main.py <command>   (config via env, see below)
"""
import logging
import math
import os
import sys
from bisect import bisect_right
from datetime import datetime, timedelta, timezone

import psycopg
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import (
    StockBarsRequest,
    StockQuotesRequest,
    StockTradesRequest,
)
from alpaca.data.timeframe import TimeFrame

from quantlib.aggregates import (
    QuoteTick,
    TickState,
    TradeTick,
    aggregate_quotes,
    aggregate_trades,
    bucket_minute,
)
from quantlib.features import (
    FEATURE_NAMES,
    FEATURE_SET_VERSION,
    BarRow,
    FeatureContext,
    feature_vector,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("backfiller")

DB_KWARGS = {
    "host": os.environ["DB_HOST"],
    "port": int(os.environ.get("DB_PORT", "5432")),
    "dbname": os.environ["DB_NAME"],
    "user": os.environ["DB_USER"],
    "password": os.environ["DB_PASSWORD"],
}

data_client = StockHistoricalDataClient(
    os.environ["ALPACA_KEY_ID"], os.environ["ALPACA_SECRET_KEY"]
)

CHUNK = 200
BAR_SQL = """
INSERT INTO bars_1m (symbol, ts, open, high, low, close, volume, vwap, trade_count, source)
VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'backfill')
ON CONFLICT (symbol, ts, source) DO NOTHING
"""


def universe_symbols(conn: psycopg.Connection) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT symbol FROM universe_membership WHERE trade_date = "
            "(SELECT max(trade_date) FROM universe_membership) ORDER BY symbol"
        )
        return [row[0] for row in cur.fetchall()]


def resolve_window() -> tuple[datetime, datetime]:
    start = datetime.fromisoformat(
        os.environ.get("BACKFILL_START", datetime.now(timezone.utc).date().isoformat())
    ).replace(tzinfo=timezone.utc)
    end_env = os.environ.get("BACKFILL_END")
    end = (
        datetime.fromisoformat(end_env).replace(tzinfo=timezone.utc)
        if end_env
        else datetime.now(timezone.utc) - timedelta(minutes=1)
    )
    return start, end


def resolve_symbols(conn: psycopg.Connection) -> list[str]:
    env = os.environ.get("BACKFILL_SYMBOLS", "").strip()
    if env.lower() == "universe":
        return universe_symbols(conn)
    if env:
        return [s.strip().upper() for s in env.split(",") if s.strip()]
    return ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "SPY", "QQQ", "JPM"]


def backfill_bars() -> None:
    start, end = resolve_window()
    with psycopg.connect(**DB_KWARGS, autocommit=True) as conn:
        symbols = resolve_symbols(conn)
        logger.info("backfilling bars for %d symbols, %s .. %s", len(symbols), start, end)
        total = 0
        for i in range(0, len(symbols), CHUNK):
            chunk = symbols[i : i + CHUNK]
            request = StockBarsRequest(
                symbol_or_symbols=chunk, timeframe=TimeFrame.Minute, start=start, end=end
            )
            barset = data_client.get_stock_bars(request)
            with conn.cursor() as cur:
                for symbol, bars in barset.data.items():
                    for bar in bars:
                        cur.execute(
                            BAR_SQL,
                            (
                                symbol, bar.timestamp, bar.open, bar.high, bar.low,
                                bar.close, int(bar.volume), bar.vwap, bar.trade_count,
                            ),
                        )
                        total += 1
            logger.info("backfilled through %d/%d symbols (%d bars)", min(i + CHUNK, len(symbols)), len(symbols), total)
        logger.info("backfill complete: %d bars inserted (source=backfill)", total)


TRADE_AGG_SQL = """
INSERT INTO trade_agg_1m
    (symbol, ts, signed_volume, buy_volume, sell_volume, large_print_cnt,
     trade_intensity, median_size, p95_size, n_trades, source)
VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'backfill')
ON CONFLICT (symbol, ts, source) DO NOTHING
"""
QUOTE_AGG_SQL = """
INSERT INTO quote_agg_1m
    (symbol, ts, mean_spread_bps, median_spread_bps, mean_bid_size, mean_ask_size,
     quote_imbalance, n_quotes, source)
VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'backfill')
ON CONFLICT (symbol, ts, source) DO NOTHING
"""


def backfill_aggregates() -> None:
    """Backfill per-minute trade & quote aggregates from historical ticks through
    the SAME quantlib functions the live ingestor uses. Parity by construction."""
    start, end = resolve_window()
    with psycopg.connect(**DB_KWARGS, autocommit=True) as conn:
        symbols = resolve_symbols(conn)
        logger.info("backfilling aggregates for %d symbols, %s .. %s", len(symbols), start, end)
        for symbol in symbols:
            trades_resp = data_client.get_stock_trades(
                StockTradesRequest(symbol_or_symbols=symbol, start=start, end=end)
            )
            quotes_resp = data_client.get_stock_quotes(
                StockQuotesRequest(symbol_or_symbols=symbol, start=start, end=end)
            )
            trade_ticks = trades_resp.data.get(symbol, [])
            quote_ticks = quotes_resp.data.get(symbol, [])

            trades_by_min: dict[int, list[TradeTick]] = {}
            for trade in trade_ticks:
                minute = bucket_minute(trade.timestamp.timestamp())
                trades_by_min.setdefault(minute, []).append(
                    TradeTick(trade.timestamp.timestamp(), float(trade.price), float(trade.size))
                )
            quotes_by_min: dict[int, list[QuoteTick]] = {}
            for quote in quote_ticks:
                minute = bucket_minute(quote.timestamp.timestamp())
                quotes_by_min.setdefault(minute, []).append(
                    QuoteTick(
                        quote.timestamp.timestamp(),
                        float(quote.bid_price), float(quote.ask_price),
                        float(quote.bid_size), float(quote.ask_size),
                    )
                )

            state = TickState()
            with conn.cursor() as cur:
                for minute in sorted(trades_by_min):
                    agg = aggregate_trades(trades_by_min[minute], state)
                    ts = datetime.fromtimestamp(minute, tz=timezone.utc)
                    cur.execute(
                        TRADE_AGG_SQL,
                        (symbol, ts, agg.signed_volume, agg.buy_volume, agg.sell_volume,
                         agg.large_print_cnt, agg.trade_intensity, agg.median_size,
                         agg.p95_size, agg.n_trades),
                    )
                for minute in sorted(quotes_by_min):
                    qagg = aggregate_quotes(quotes_by_min[minute])
                    ts = datetime.fromtimestamp(minute, tz=timezone.utc)
                    cur.execute(
                        QUOTE_AGG_SQL,
                        (symbol, ts, qagg.mean_spread_bps, qagg.median_spread_bps,
                         qagg.mean_bid_size, qagg.mean_ask_size, qagg.quote_imbalance,
                         qagg.n_quotes),
                    )
            logger.info(
                "%s: %d trades -> %d trade-min, %d quotes -> %d quote-min",
                symbol, len(trade_ticks), len(trades_by_min),
                len(quote_ticks), len(quotes_by_min),
            )
    logger.info("aggregate backfill complete")


def validate_aggregates() -> None:
    """Compare stream vs backfill trade/quote aggregates on overlapping minutes.
    Some divergence is expected (live buffers can miss late/early ticks, and the
    tick-rule state seeds differently mid-window) — we quantify it rather than
    assume it away."""
    with psycopg.connect(**DB_KWARGS, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT count(*),
                   count(*) FILTER (WHERE abs(s.n_trades - b.n_trades)
                                          <= GREATEST(2, 0.02 * b.n_trades)),
                   avg(CASE WHEN b.n_trades > 0
                            THEN abs(s.n_trades - b.n_trades)::float / b.n_trades END)
            FROM trade_agg_1m s
            JOIN trade_agg_1m b ON b.symbol=s.symbol AND b.ts=s.ts
            WHERE s.source='stream' AND b.source='backfill'
            """
        )
        total, close, mean_rel = cur.fetchone()
        if total:
            logger.info(
                "trade_agg: %d overlapping min | within 2%%/2-trade: %.1f%% | mean rel n_trades diff %.3f",
                total, 100.0 * close / total, mean_rel or 0.0,
            )
        else:
            logger.warning("no overlapping trade_agg to validate")

        cur.execute(
            """
            SELECT count(*),
                   count(*) FILTER (WHERE abs(s.mean_spread_bps - b.mean_spread_bps)
                                          <= GREATEST(0.5, 0.1 * b.mean_spread_bps))
            FROM quote_agg_1m s
            JOIN quote_agg_1m b ON b.symbol=s.symbol AND b.ts=s.ts
            WHERE s.source='stream' AND b.source='backfill'
            """
        )
        qtotal, qclose = cur.fetchone()
        if qtotal:
            logger.info(
                "quote_agg: %d overlapping min | spread within 10%%/0.5bps: %.1f%%",
                qtotal, 100.0 * qclose / qtotal,
            )
        else:
            logger.warning("no overlapping quote_agg to validate")


WARMUP = timedelta(minutes=70)        # enough history for the 60m features


def _load_bars(conn: psycopg.Connection, symbol: str, source: str,
               start: datetime, end: datetime) -> list[BarRow]:
    with conn.cursor() as cur:
        cur.execute(
            """SELECT ts, open, high, low, close, volume, vwap FROM bars_1m
               WHERE symbol=%s AND source=%s AND ts>=%s AND ts<=%s ORDER BY ts""",
            (symbol, source, start - WARMUP, end),
        )
        return [
            BarRow(ts=r[0], open=r[1], high=r[2], low=r[3], close=r[4],
                   volume=float(r[5]), vwap=r[6] if r[6] is not None else r[4])
            for r in cur.fetchall()
        ]


def _load_micro(conn: psycopg.Connection, symbol: str, start: datetime,
                end: datetime) -> dict[datetime, dict[str, float]]:
    """Microstructure pass-throughs per minute, preferring backfill over stream."""
    micro: dict[datetime, dict[str, float]] = {}
    with conn.cursor() as cur:
        cur.execute(
            """SELECT DISTINCT ON (ts) ts, signed_volume, buy_volume, sell_volume,
                      large_print_cnt, trade_intensity
               FROM trade_agg_1m WHERE symbol=%s AND ts>=%s AND ts<=%s
               ORDER BY ts, (source='backfill') DESC""",
            (symbol, start - WARMUP, end),
        )
        for ts, signed, buy, sell, lpc, intensity in cur.fetchall():
            depth = (buy or 0) + (sell or 0)
            micro.setdefault(ts, {})
            micro[ts]["trade_imbalance"] = signed / depth if depth else math.nan
            micro[ts]["large_print_cnt"] = float(lpc) if lpc is not None else math.nan
            micro[ts]["trade_intensity"] = intensity if intensity is not None else math.nan
        cur.execute(
            """SELECT DISTINCT ON (ts) ts, mean_spread_bps, quote_imbalance
               FROM quote_agg_1m WHERE symbol=%s AND ts>=%s AND ts<=%s
               ORDER BY ts, (source='backfill') DESC""",
            (symbol, start - WARMUP, end),
        )
        for ts, spread, imb in cur.fetchall():
            micro.setdefault(ts, {})
            micro[ts]["spread_bps"] = spread if spread is not None else math.nan
            micro[ts]["quote_imbalance"] = imb if imb is not None else math.nan
    return micro


def build_features() -> None:
    """Compute feature_vectors from stored bars/aggregates via quantlib.features
    (source='historical'). Same code the live computer uses -> parity."""
    start, end = resolve_window()
    bar_source = os.environ.get("FEATURE_BAR_SOURCE", "stream")
    with psycopg.connect(**DB_KWARGS, autocommit=True) as conn:
        symbols = resolve_symbols(conn)
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO feature_sets (version, names, notes) VALUES (%s,%s,%s)
                   ON CONFLICT (version) DO NOTHING""",
                (FEATURE_SET_VERSION, FEATURE_NAMES, "v1 set: returns, vol, volume-z, "
                 "vwap dev, range, gap, market-relative, calendar, microstructure"),
            )
        market = _load_bars(conn, "SPY", bar_source, start, end)
        market_ts = [bar.ts for bar in market]
        logger.info("building features for %d symbols, %s..%s (bar source=%s)",
                    len(symbols), start, end, bar_source)
        total = 0
        for symbol in symbols:
            bars = _load_bars(conn, symbol, bar_source, start, end)
            micro = _load_micro(conn, symbol, start, end)
            if not bars:
                continue
            session_open: dict[object, float] = {}
            for bar in bars:
                session_open.setdefault(bar.ts.date(), bar.open)
            rows = []
            for i, bar in enumerate(bars):
                if bar.ts < start:
                    continue            # warmup region: context only
                m_idx = bisect_right(market_ts, bar.ts)
                micro_vals = micro.get(bar.ts, {})
                ctx = FeatureContext(
                    symbol=symbol, ts=bar.ts, bars=bars[: i + 1],
                    session_open=session_open[bar.ts.date()],
                    market_bars=market[:m_idx],
                    trade_imbalance=micro_vals.get("trade_imbalance", math.nan),
                    large_print_cnt=micro_vals.get("large_print_cnt", math.nan),
                    trade_intensity=micro_vals.get("trade_intensity", math.nan),
                    spread_bps=micro_vals.get("spread_bps", math.nan),
                    quote_imbalance=micro_vals.get("quote_imbalance", math.nan),
                )
                rows.append((symbol, bar.ts, FEATURE_SET_VERSION, feature_vector(ctx)))
            with conn.cursor() as cur:
                cur.executemany(
                    """INSERT INTO feature_vectors (symbol, ts, set_version, vector, source)
                       VALUES (%s,%s,%s,%s,'historical')
                       ON CONFLICT (symbol, ts, set_version, source) DO NOTHING""",
                    rows,
                )
            total += len(rows)
            logger.info("%s: %d feature rows", symbol, len(rows))
        logger.info("feature build complete: %d vectors (%s)", total, FEATURE_SET_VERSION)


def validate_bars() -> None:
    """Compare stream vs backfill for overlapping (symbol, ts). OHLCV must match
    within tolerance. This is the Phase 1 streamed-vs-REST gate."""
    with psycopg.connect(**DB_KWARGS, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            """
            WITH joined AS (
                SELECT s.symbol, s.ts,
                       (abs(s.open - b.open)   <= 0.01
                    AND abs(s.high - b.high)   <= 0.01
                    AND abs(s.low  - b.low)    <= 0.01
                    AND abs(s.close - b.close) <= 0.01) AS ohlc_match,
                       (s.volume = b.volume)            AS vol_match
                FROM bars_1m s
                JOIN bars_1m b ON b.symbol = s.symbol AND b.ts = s.ts
                WHERE s.source = 'stream' AND b.source = 'backfill'
            )
            SELECT count(*),
                   count(*) FILTER (WHERE ohlc_match),
                   count(*) FILTER (WHERE ohlc_match AND vol_match)
            FROM joined
            """
        )
        total, ohlc_ok, full_ok = cur.fetchone()
        if total == 0:
            logger.warning("no overlapping stream/backfill bars to validate")
            return
        logger.info(
            "validation: %d overlapping bars | OHLC match %.3f%% | OHLC+volume match %.3f%%",
            total, 100.0 * ohlc_ok / total, 100.0 * full_ok / total,
        )
        cur.execute(
            """
            SELECT s.symbol, s.ts, s.close, b.close, s.volume, b.volume
            FROM bars_1m s JOIN bars_1m b ON b.symbol=s.symbol AND b.ts=s.ts
            WHERE s.source='stream' AND b.source='backfill'
              AND (abs(s.close-b.close) > 0.01 OR s.volume <> b.volume)
            ORDER BY s.ts LIMIT 5
            """
        )
        for row in cur.fetchall():
            logger.info("  mismatch %s %s stream(close=%s vol=%s) backfill(close=%s vol=%s)",
                        row[0], row[1], row[2], row[4], row[3], row[5])


def main() -> None:
    command = sys.argv[1] if len(sys.argv) > 1 else "backfill-bars"
    if command == "backfill-bars":
        backfill_bars()
    elif command == "validate-bars":
        validate_bars()
    elif command == "backfill-aggs":
        backfill_aggregates()
    elif command == "validate-aggs":
        validate_aggregates()
    elif command == "build-features":
        build_features()
    else:
        raise SystemExit(f"unknown command: {command}")


if __name__ == "__main__":
    main()
