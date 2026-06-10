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
import math
from bisect import bisect_right
from datetime import datetime, timedelta

import psycopg

from quantlib.features import (
    FEATURE_NAMES,
    FEATURE_SET_VERSION,
    BarRow,
    FeatureContext,
    feature_vector,
)

WARMUP = timedelta(minutes=70)        # enough history for the 60m features
MARKET_SYMBOL = "SPY"


def register_feature_set(conn: psycopg.Connection) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO feature_sets (version, names, notes) VALUES (%s,%s,%s)
               ON CONFLICT (version) DO NOTHING""",
            (FEATURE_SET_VERSION, FEATURE_NAMES,
             "v1 set: returns, vol, volume-z, vwap dev, range, gap, "
             "market-relative, calendar, microstructure"),
        )


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


def build_feature_store(conn: psycopg.Connection, symbols: list[str],
                        start: datetime, end: datetime, bar_source: str,
                        source_label: str) -> int:
    """Compute and insert feature_vectors for symbols over [start, end]. Returns
    the number of vectors written."""
    register_feature_set(conn)
    market = load_bars(conn, MARKET_SYMBOL, bar_source, start, end)
    market_ts = [bar.ts for bar in market]
    total = 0
    for symbol in symbols:
        bars = load_bars(conn, symbol, bar_source, start, end)
        if not bars:
            continue
        micro = load_micro(conn, symbol, start, end)
        session_open: dict[object, float] = {}
        for bar in bars:
            session_open.setdefault(bar.ts.date(), bar.open)
        rows = []
        for i, bar in enumerate(bars):
            if bar.ts < start or bar.ts > end:
                continue
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
            rows.append((symbol, bar.ts, FEATURE_SET_VERSION, feature_vector(ctx), source_label))
        with conn.cursor() as cur:
            cur.executemany(
                """INSERT INTO feature_vectors (symbol, ts, set_version, vector, source)
                   VALUES (%s,%s,%s,%s,%s)
                   ON CONFLICT (symbol, ts, set_version, source) DO NOTHING""",
                rows,
            )
        total += len(rows)
    return total
