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
from datetime import date, datetime, timedelta, timezone

import psycopg
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockQuotesRequest, StockTradesRequest

from quantlib.aggregates import (
    QuoteTick,
    TickState,
    TradeTick,
    aggregate_quotes,
    aggregate_trades,
    bucket_minute,
)
from quantlib.barsource import fetch_and_store_bars
from quantlib.features import is_rth, on_cadence
from quantlib.featurestore import build_feature_store, load_membership
from quantlib.labels import (
    LABEL_HORIZONS,
    OVERNIGHT_HORIZON,
    cross_sectional_excess,
    forward_return_series,
    horizon_name,
    overnight_return_series,
)
from quantlib.universe import SymbolStats, select_universe

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

def universe_symbols(conn: psycopg.Connection) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT symbol FROM universe_membership WHERE trade_date = "
            "(SELECT max(trade_date) FROM universe_membership) ORDER BY symbol"
        )
        return [row[0] for row in cur.fetchall()]


UNIVERSE_ADV_LOOKBACK = 20      # trading sessions of trailing ADV
UNIVERSE_MAX_SYMBOLS = 1000


def build_universe_history() -> None:
    """Reconstruct point-in-time universe_membership per historical trade_date by
    screening from TRAILING (strictly-prior) daily ADV — no lookahead. A date is
    only built if it has >= UNIVERSE_ADV_LOOKBACK prior sessions of data.

    Caveat (documented): we can only screen symbols we have bars for (today's
    backfilled universe), so truly-delisted names aren't recovered; but per-date
    screening removes the worst survivorship bias (a name that was a penny/illiquid
    stock on date D is excluded from D's universe even if liquid today)."""
    with psycopg.connect(**DB_KWARGS, autocommit=True) as conn, conn.cursor() as cur:
        # Daily dollar-volume and last close per symbol per session (RTH).
        cur.execute(
            """SELECT symbol, ts::date AS d,
                      sum(close*volume) AS dollar_vol,
                      (array_agg(close ORDER BY ts DESC))[1] AS last_close
               FROM bars_1m
               WHERE source='backfill' AND ts::time>='13:30' AND ts::time<'20:00'
               GROUP BY symbol, ts::date"""
        )
        daily: dict[str, list[tuple]] = {}
        all_days: set = set()
        for symbol, day, dollar_vol, last_close in cur.fetchall():
            daily.setdefault(symbol, []).append((day, float(dollar_vol or 0), float(last_close)))
            all_days.add(day)
        for symbol in daily:
            daily[symbol].sort()
        sessions = sorted(all_days)

        built = 0
        for idx in range(UNIVERSE_ADV_LOOKBACK, len(sessions)):
            trade_date = sessions[idx]
            window = set(sessions[idx - UNIVERSE_ADV_LOOKBACK:idx])  # strictly prior
            stats = []
            for symbol, rows in daily.items():
                prior = [(dv, close) for (day, dv, close) in rows if day in window]
                if len(prior) < UNIVERSE_ADV_LOOKBACK // 2:    # need enough history
                    continue
                adv = sum(dv for dv, _ in prior) / len(prior)
                price = prior[-1][1]
                stats.append(SymbolStats(symbol=symbol, price=price, adv_dollar=adv))
            chosen = select_universe(stats, max_symbols=UNIVERSE_MAX_SYMBOLS)
            cur.execute("DELETE FROM universe_membership WHERE trade_date=%s", (trade_date,))
            for stat in chosen:
                cur.execute(
                    """INSERT INTO universe_membership
                           (trade_date, symbol, in_universe, adv_dollar, price)
                       VALUES (%s,%s,TRUE,%s,%s)""",
                    (trade_date, stat.symbol, stat.adv_dollar, stat.price),
                )
            built += 1
            if built % 10 == 0:
                logger.info("universe-history: built %d dates (latest %s, %d symbols)",
                            built, trade_date, len(chosen))
        logger.info("universe-history complete: %d trade_dates", built)


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
    """One-shot bar backfill over [start, end] using the shared (adjusted, upsert)
    helper — same code the continuous backfill-manager uses."""
    start, end = resolve_window()
    with psycopg.connect(**DB_KWARGS, autocommit=True) as conn:
        symbols = resolve_symbols(conn)
        logger.info("backfilling bars for %d symbols, %s .. %s", len(symbols), start, end)
        total = fetch_and_store_bars(data_client, conn, symbols, start, end)
        logger.info("backfill complete: %d bars upserted (source=backfill)", total)


TRADE_AGG_SQL = """
INSERT INTO trade_agg_1m
    (symbol, ts, signed_volume, buy_volume, sell_volume, large_print_cnt,
     trade_intensity, median_size, p95_size, n_trades, source)
VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'backfill')
ON CONFLICT (symbol, ts, source) DO UPDATE SET
    signed_volume=EXCLUDED.signed_volume, buy_volume=EXCLUDED.buy_volume,
    sell_volume=EXCLUDED.sell_volume, large_print_cnt=EXCLUDED.large_print_cnt,
    trade_intensity=EXCLUDED.trade_intensity, median_size=EXCLUDED.median_size,
    p95_size=EXCLUDED.p95_size, n_trades=EXCLUDED.n_trades, ingested_at=now()
"""
QUOTE_AGG_SQL = """
INSERT INTO quote_agg_1m
    (symbol, ts, mean_spread_bps, median_spread_bps, mean_bid_size, mean_ask_size,
     quote_imbalance, n_quotes, source)
VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'backfill')
ON CONFLICT (symbol, ts, source) DO UPDATE SET
    mean_spread_bps=EXCLUDED.mean_spread_bps, median_spread_bps=EXCLUDED.median_spread_bps,
    mean_bid_size=EXCLUDED.mean_bid_size, mean_ask_size=EXCLUDED.mean_ask_size,
    quote_imbalance=EXCLUDED.quote_imbalance, n_quotes=EXCLUDED.n_quotes, ingested_at=now()
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


def build_features() -> None:
    """Historical feature store: compute feature_vectors (source='historical')
    from stored bars/aggregates via the shared featurestore module."""
    start, end = resolve_window()
    bar_source = os.environ.get("FEATURE_BAR_SOURCE", "stream")
    set_version = os.environ.get("FEATURE_SET_VERSION", "v1.0.0")
    with psycopg.connect(**DB_KWARGS, autocommit=True) as conn:
        membership = load_membership(conn) if os.environ.get("USE_PIT_UNIVERSE") else None
        symbols = (
            sorted({s for members in membership.values() for s in members})
            if membership else resolve_symbols(conn)
        )
        cadence = int(os.environ["FEATURE_CADENCE_MIN"]) if os.environ.get("FEATURE_CADENCE_MIN") else None
        logger.info("building features for %d symbols, %s..%s (src=%s, pit=%s, cadence=%s, set=%s)",
                    len(symbols), start, end, bar_source, membership is not None, cadence, set_version)
        total = build_feature_store(conn, symbols, start, end, bar_source, "historical",
                                    membership, cadence, set_version)
        logger.info("feature build complete: %d vectors", total)


def build_labels() -> None:
    """Compute forward cross-sectional excess-return labels for the universe over
    [start, end] and write them to the labels table. Uses the same bar source as
    features for consistency."""
    start, end = resolve_window()
    bar_source = os.environ.get("FEATURE_BAR_SOURCE", "stream")
    with psycopg.connect(**DB_KWARGS, autocommit=True) as conn:
        membership = load_membership(conn) if os.environ.get("USE_PIT_UNIVERSE") else None
        symbols = (
            sorted({s for members in membership.values() for s in members})
            if membership else resolve_symbols(conn)
        )
        logger.info("building labels for %d symbols, %s..%s, horizons=%s, pit=%s",
                    len(symbols), start, end, LABEL_HORIZONS, membership is not None)
        # close-by-ts per symbol (extend past `end` so forward windows resolve)
        closes: dict[str, dict[datetime, float]] = {}
        with conn.cursor() as cur:
            for symbol in symbols:
                cur.execute(
                    """SELECT ts, close FROM bars_1m
                       WHERE symbol=%s AND source=%s AND ts>=%s
                         AND ts<=%s + make_interval(mins => %s) ORDER BY ts""",
                    (symbol, bar_source, start, end, max(LABEL_HORIZONS)),
                )
                # RTH-only: forward returns are session close-to-close, never
                # anchored in or reaching across the thin extended-hours tape.
                series = {row[0]: row[1] for row in cur.fetchall() if is_rth(row[0])}
                if series:
                    closes[symbol] = series

        total = 0
        for horizon in LABEL_HORIZONS:
            name = horizon_name(horizon)
            fwd_by_symbol = {
                symbol: forward_return_series(series, horizon)
                for symbol, series in closes.items()
            }
            # Pivot to per-ts cross-sections, then demean by the universe median.
            all_ts = {ts for series in fwd_by_symbol.values() for ts in series}
            cadence = int(os.environ["FEATURE_CADENCE_MIN"]) if os.environ.get("FEATURE_CADENCE_MIN") else None
            rows = []
            for ts in all_ts:
                if ts > end:
                    continue
                if cadence is not None and not on_cadence(ts, cadence):
                    continue                   # match the feature panel's cadence
                members = membership.get(ts.date()) if membership else None
                section = {
                    symbol: fwd[ts]
                    for symbol, fwd in fwd_by_symbol.items()
                    if ts in fwd and (members is None or symbol in members)
                }
                # demean within THIS date's universe cross-section (point-in-time)
                excess = cross_sectional_excess(section)
                for symbol, value in excess.items():
                    if not math.isnan(value):
                        rows.append((symbol, ts, name, value))
            with conn.cursor() as cur:
                cur.executemany(
                    """INSERT INTO labels (symbol, ts, horizon, value)
                       VALUES (%s,%s,%s,%s)
                       ON CONFLICT (symbol, ts, horizon) DO UPDATE SET value=EXCLUDED.value""",
                    rows,
                )
            total += len(rows)
            logger.info("%s: %d labels", name, len(rows))
        logger.info("label build complete: %d labels", total)


def build_overnight_labels() -> None:
    """Overnight (close->next-open) cross-sectional excess labels, assigned to each
    day's LAST cadence ts (the prediction point near the close — the low-turnover
    horizon). Demeaned within that date's point-in-time universe."""
    start, end = resolve_window()
    bar_source = os.environ.get("FEATURE_BAR_SOURCE", "backfill")
    cadence = int(os.environ.get("FEATURE_CADENCE_MIN", "30"))
    with psycopg.connect(**DB_KWARGS, autocommit=True) as conn:
        membership = load_membership(conn) if os.environ.get("USE_PIT_UNIVERSE") else None
        symbols = (
            sorted({s for members in membership.values() for s in members})
            if membership else resolve_symbols(conn)
        )
        logger.info("building overnight labels for %d symbols, %s..%s, pit=%s",
                    len(symbols), start, end, membership is not None)
        # per symbol: overnight return by day + the last-cadence ts of each day
        overnight_by_symbol: dict[str, dict[date, float]] = {}
        last_cadence_ts: dict[str, dict[date, datetime]] = {}
        with conn.cursor() as cur:
            for symbol in symbols:
                cur.execute(
                    """SELECT ts, open, close FROM bars_1m
                       WHERE symbol=%s AND source=%s AND ts>=%s
                         AND ts<=%s + interval '1 day' ORDER BY ts""",
                    (symbol, bar_source, start, end),
                )
                daily_open: dict[date, float] = {}
                daily_close: dict[date, float] = {}
                last_cad: dict[date, datetime] = {}
                for ts, bar_open, bar_close in cur.fetchall():
                    if not is_rth(ts):
                        continue
                    day = ts.date()
                    daily_open.setdefault(day, bar_open)     # first RTH bar = session open
                    daily_close[day] = bar_close             # last RTH bar = session close
                    if on_cadence(ts, cadence):
                        last_cad[day] = ts                   # last cadence point of the day
                overnight_by_symbol[symbol] = overnight_return_series(daily_open, daily_close)
                last_cadence_ts[symbol] = last_cad

        all_days = {day for series in overnight_by_symbol.values() for day in series}
        rows = []
        for day in all_days:
            if day > end.date():
                continue
            members = membership.get(day) if membership else None
            section = {
                symbol: series[day]
                for symbol, series in overnight_by_symbol.items()
                if day in series and (members is None or symbol in members)
            }
            excess = cross_sectional_excess(section)
            for symbol, value in excess.items():
                prediction_ts = last_cadence_ts[symbol].get(day)
                if prediction_ts is not None and not math.isnan(value):
                    rows.append((symbol, prediction_ts, OVERNIGHT_HORIZON, value))
        with conn.cursor() as cur:
            cur.executemany(
                """INSERT INTO labels (symbol, ts, horizon, value) VALUES (%s,%s,%s,%s)
                   ON CONFLICT (symbol, ts, horizon) DO UPDATE SET value=EXCLUDED.value""",
                rows,
            )
        logger.info("overnight label build complete: %d labels", len(rows))


def validate_features() -> None:
    """Replay-equivalence at the feature level: live (source='stream') vs
    recomputed (source='historical') feature_vectors must be identical."""
    with psycopg.connect(**DB_KWARGS, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT count(*),
                   count(*) FILTER (WHERE s.vector = h.vector)
            FROM feature_vectors s
            JOIN feature_vectors h
              ON h.symbol=s.symbol AND h.ts=s.ts AND h.set_version=s.set_version
            WHERE s.source='stream' AND h.source='historical'
            """
        )
        total, identical = cur.fetchone()
        if not total:
            logger.warning("no overlapping stream/historical feature vectors")
            return
        logger.info("feature replay-equivalence: %d overlapping vectors | identical %.3f%%",
                    total, 100.0 * identical / total)
        if identical < total:
            cur.execute(
                """SELECT s.symbol, s.ts FROM feature_vectors s
                   JOIN feature_vectors h ON h.symbol=s.symbol AND h.ts=s.ts
                      AND h.set_version=s.set_version
                   WHERE s.source='stream' AND h.source='historical' AND s.vector <> h.vector
                   LIMIT 5"""
            )
            for row in cur.fetchall():
                logger.info("  differs: %s %s", row[0], row[1])


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
    elif command == "validate-features":
        validate_features()
    elif command == "build-labels":
        build_labels()
    elif command == "build-overnight-labels":
        build_overnight_labels()
    elif command == "build-universe-history":
        build_universe_history()
    else:
        raise SystemExit(f"unknown command: {command}")


if __name__ == "__main__":
    main()
