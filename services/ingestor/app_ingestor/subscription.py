"""Build the ingestor's subscription sets from the live universe.

Bars stream for the WHOLE current equities universe (the research panel breadth).
Trades and quotes stream only for the top-by-ADV OFI names (the order-flow signal
tier) — derived DYNAMICALLY from universe_membership at subscription-build time so
it self-maintains as the universe churns. A tiny separate market-context set
(QQQ/SPY/IWM) is streamed for bars only as a market-beta reference; those index
ETFs are correctly absent from the equities universe and must not silently vanish
when the OFI set is rebuilt off it.
"""
import os

import psycopg

from app_ingestor.shard import shard_for

# Market-beta reference (index ETFs, never in the equities universe, never traded).
# Streamed for bars only so feature code has a market context; kept OUT of the
# OFI equities set. Without this they'd silently disappear at the universe rebuild.
MARKET_CONTEXT_SYMBOLS = [
    s.strip().upper()
    for s in os.environ.get("MARKET_CONTEXT_SYMBOLS", "QQQ,SPY,IWM").split(",")
    if s.strip()
]

# How many top-ADV equities get trades/quotes (the OFI tier). Default 512 = an even
# 4-shard cut at the soft ADV boundary (rank 500 ≈ rank 512 in liquidity). M2 floor
# is 500; this >=500 satisfies the criterion while sharding cleanly.
OFI_SYMBOL_COUNT = int(os.environ.get("OFI_SYMBOL_COUNT", "512"))

DEFAULT_SYMBOLS = ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "JPM"]

LATEST_DATE_SQL = "SELECT max(trade_date) FROM universe_membership"

BAR_SYMBOLS_SQL = """
SELECT symbol FROM universe_membership
WHERE trade_date = %s AND in_universe
ORDER BY symbol
"""

OFI_SYMBOLS_SQL = """
SELECT symbol FROM universe_membership
WHERE trade_date = %s AND in_universe AND adv_dollar IS NOT NULL
ORDER BY adv_dollar DESC, symbol
LIMIT %s
"""


def load_subscription(
    db_kwargs: dict[str, str | int], n_shards: int, ofi_count: int = OFI_SYMBOL_COUNT
) -> tuple[list[str], list[str], list[list[str]]]:
    """Return (bar_symbols, ofi_symbols, shard_symbol_lists).

    bar_symbols = whole equities universe + market-context ETFs (bars only).
    ofi_symbols = top-`ofi_count` equities by ADV (trades + quotes).
    shard_symbol_lists[i] = the OFI symbols routed to worker i, by symbol-hash —
    the exact partition the reader uses, so each worker knows its expected coverage.
    """
    with psycopg.connect(**db_kwargs, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(LATEST_DATE_SQL)
        row = cur.fetchone()
        latest_date = row[0] if row else None

        if latest_date is None:
            equities = list(DEFAULT_SYMBOLS)
            ofi_symbols = list(DEFAULT_SYMBOLS)
        else:
            cur.execute(BAR_SYMBOLS_SQL, (latest_date,))
            equities = [r[0] for r in cur.fetchall()]
            cur.execute(OFI_SYMBOLS_SQL, (latest_date, ofi_count))
            ofi_symbols = [r[0] for r in cur.fetchall()]

    bar_symbols = sorted(set(equities) | set(MARKET_CONTEXT_SYMBOLS))

    shard_symbol_lists: list[list[str]] = [[] for _ in range(n_shards)]
    for symbol in ofi_symbols:
        shard_symbol_lists[shard_for(symbol, n_shards)].append(symbol)

    return bar_symbols, ofi_symbols, shard_symbol_lists
