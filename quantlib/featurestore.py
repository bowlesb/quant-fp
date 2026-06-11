"""Shared feature-store assembly: load bars + microstructure aggregates from the
DB, build point-in-time FeatureContexts, and compute feature_vectors.

Both callers use this identical code:
- the historical builder (backfiller build-features, source_label='historical'),
- the live feature-computer (source_label='stream').

Because both read the same stored data through the same path, the live and
recomputed feature vectors are identical by construction — which the
validate-features replay-equivalence check confirms (guarding against any hidden
live-only state). This module does DB I/O; the pure math stays in features.py.
"""
import logging
import math
from bisect import bisect_right
from datetime import date, datetime, timedelta

import psycopg

from quantlib.features import (
    FEATURE_SET_VERSION,
    FEATURE_SETS,
    MAX_MOMENTUM_LOOKBACK,
    MOMENTUM_NAMES,
    ORDER_FLOW_NAMES,
    BarRow,
    FeatureContext,
    feature_vector,
    is_rth,
    on_cadence,
)

logger = logging.getLogger(__name__)

WARMUP = timedelta(minutes=70)        # enough history for the 60m features
MARKET_SYMBOL = "SPY"


def load_membership(conn: psycopg.Connection) -> dict[object, set[str]]:
    """Point-in-time universe membership as {trade_date: {symbols}}."""
    membership: dict[object, set[str]] = {}
    with conn.cursor() as cur:
        cur.execute("SELECT trade_date, symbol FROM universe_membership WHERE in_universe")
        for trade_date, symbol in cur.fetchall():
            membership.setdefault(trade_date, set()).add(symbol)
    return membership


def register_feature_set(conn: psycopg.Connection, version: str = FEATURE_SET_VERSION) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO feature_sets (version, names, notes) VALUES (%s,%s,%s)
               ON CONFLICT (version) DO NOTHING""",
            (version, FEATURE_SETS[version], f"feature set {version}"),
        )


def load_daily_closes(conn: psycopg.Connection, symbol: str, source: str,
                      end: datetime) -> dict[date, float]:
    """{date: RTH-session close} for the symbol up to `end`. The session close is the
    last RTH bar of each day (DST-correct via America/New_York)."""
    with conn.cursor() as cur:
        cur.execute(
            """SELECT DISTINCT ON (ts::date) ts::date, close FROM bars_1m
               WHERE symbol=%s AND source=%s AND ts<=%s
                 AND (ts AT TIME ZONE 'America/New_York')::time >= '09:30'
                 AND (ts AT TIME ZONE 'America/New_York')::time <  '16:00'
               ORDER BY ts::date, ts DESC""",
            (symbol, source, end),
        )
        return {row[0]: float(row[1]) for row in cur.fetchall()}


def load_bars(conn: psycopg.Connection, symbol: str, source: str,
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


def load_micro(conn: psycopg.Connection, symbol: str,
               start: datetime, end: datetime) -> dict[datetime, dict[str, float]]:
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
            entry = micro.setdefault(ts, {})
            entry["trade_imbalance"] = signed / depth if depth else math.nan
            entry["large_print_cnt"] = float(lpc) if lpc is not None else math.nan
            entry["trade_intensity"] = intensity if intensity is not None else math.nan
        cur.execute(
            """SELECT DISTINCT ON (ts) ts, mean_spread_bps, quote_imbalance
               FROM quote_agg_1m WHERE symbol=%s AND ts>=%s AND ts<=%s
               ORDER BY ts, (source='backfill') DESC""",
            (symbol, start - WARMUP, end),
        )
        for ts, spread, imb in cur.fetchall():
            entry = micro.setdefault(ts, {})
            entry["spread_bps"] = spread if spread is not None else math.nan
            entry["quote_imbalance"] = imb if imb is not None else math.nan
    return micro


def load_flow(conn: psycopg.Connection, symbol: str,
              start: datetime, end: datetime) -> dict[datetime, tuple[float, float]]:
    """Per-minute (signed_volume, total_volume) for the symbol over [start-WARMUP, end] —
    the order-flow series for OFI windows. Prefers backfill over stream (parity-true)."""
    flow: dict[datetime, tuple[float, float]] = {}
    with conn.cursor() as cur:
        cur.execute(
            """SELECT DISTINCT ON (ts) ts, signed_volume, buy_volume, sell_volume
               FROM trade_agg_1m WHERE symbol=%s AND ts>=%s AND ts<=%s
               ORDER BY ts, (source='backfill') DESC""",
            (symbol, start - WARMUP, end),
        )
        for ts, signed, buy, sell in cur.fetchall():
            if signed is not None:
                flow[ts] = (float(signed), float((buy or 0) + (sell or 0)))
    return flow


def build_feature_store(conn: psycopg.Connection, symbols: list[str],
                        start: datetime, end: datetime, bar_source: str,
                        source_label: str,
                        membership: dict[object, set[str]] | None = None,
                        cadence_minutes: int | None = None,
                        feature_set_version: str = FEATURE_SET_VERSION) -> int:
    """Compute and insert feature_vectors for symbols over [start, end]. If
    `membership` is given (point-in-time {date: {symbols}}), a (symbol, ts) row is
    emitted only when the symbol is in that date's universe. Returns vectors written."""
    register_feature_set(conn, feature_set_version)
    needs_daily = bool(set(FEATURE_SETS[feature_set_version]) & set(MOMENTUM_NAMES))
    needs_flow = bool(set(FEATURE_SETS[feature_set_version]) & set(ORDER_FLOW_NAMES))
    # Regular-hours only: drop extended-hours bars so returns/vol windows and
    # session_open reflect the real session (09:30-16:00 ET), not premarket prints.
    market = [bar for bar in load_bars(conn, MARKET_SYMBOL, bar_source, start, end) if is_rth(bar.ts)]
    market_ts = [bar.ts for bar in market]
    market_daily = load_daily_closes(conn, MARKET_SYMBOL, bar_source, end) if needs_daily else {}
    # WARMUP ASSERT (QA-I4): momentum needs MAX_MOMENTUM_LOOKBACK prior trading days. If the
    # build window starts without enough pre-window daily history (e.g. just after a data
    # gap), momentum is silently NaN-degraded at the start — surface it loudly, don't hide it.
    if needs_daily:
        prior_days = sum(1 for d in market_daily if d < start.date())
        if prior_days < MAX_MOMENTUM_LOOKBACK:
            logger.warning(
                "WARMUP SHORTFALL: only %d prior trading days before %s but momentum needs "
                "%d -> momentum features NaN-degraded at the window start (insufficient "
                "pre-window history; widen the backfill or move the start later)",
                prior_days, start.date(), MAX_MOMENTUM_LOOKBACK,
            )
    total = 0
    for symbol in symbols:
        bars = [bar for bar in load_bars(conn, symbol, bar_source, start, end) if is_rth(bar.ts)]
        if not bars:
            continue
        micro = load_micro(conn, symbol, start, end)
        daily_closes = load_daily_closes(conn, symbol, bar_source, end) if needs_daily else {}
        flow_by_ts = load_flow(conn, symbol, start, end) if needs_flow else {}
        session_open: dict[object, float] = {}
        for bar in bars:
            session_open.setdefault(bar.ts.date(), bar.open)   # first RTH bar = the open
        rows = []
        for i, bar in enumerate(bars):
            if bar.ts < start or bar.ts > end:
                continue
            if membership is not None and symbol not in membership.get(bar.ts.date(), set()):
                continue                       # point-in-time: only members of this date
            if cadence_minutes is not None and not on_cadence(bar.ts, cadence_minutes):
                continue                       # only emit at rebalance cadence
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
                daily_closes=daily_closes,
                market_daily_closes=market_daily,
                flow_by_ts=flow_by_ts,
            )
            rows.append((symbol, bar.ts, feature_set_version,
                         feature_vector(ctx, feature_set_version), source_label))
        with conn.cursor() as cur:
            cur.executemany(
                """INSERT INTO feature_vectors (symbol, ts, set_version, vector, source)
                   VALUES (%s,%s,%s,%s,%s)
                   ON CONFLICT (symbol, ts, set_version, source) DO NOTHING""",
                rows,
            )
        total += len(rows)
    return total
