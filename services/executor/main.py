"""Executor — the trade step + full BET LIFECYCLE (Ben: prove we can place, manage, and
TERMINATE bets on market days — that's infra to build/test now, independent of edge).

Lifecycle (one tiny paper basket per day, held, then terminated):
  open flat -> SUBMIT once (marketable-limit, idempotent, caps from a FRESH broker snapshot)
  -> MANAGE (capture fills, reconcile our book vs broker each cycle)
  -> TERMINATE (EOD flatten ~12 min before close, or a daily max-loss KILL SWITCH) -> flat.
Holding one basket all day (no intraday rebalancing) sidesteps the flip/wash foot-guns.

DRY_RUN=true logs the intended basket without submitting. DRY_RUN=false runs the live
(paper) lifecycle. Either way it's a PAPER account and tiny size — we're proving execution,
not chasing edge (the signal isn't proven; see JOURNAL).
"""
import json
import logging
import math
import os
import time
from datetime import date, datetime, time as dtime, timezone
from zoneinfo import ZoneInfo

import psycopg
from alpaca.common.exceptions import APIError
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestQuoteRequest
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import GetOrdersRequest, LimitOrderRequest

from quantlib.corporate_actions import names_with_recent_ex_date
from quantlib.universe import is_etf_like

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("executor")

MODE = os.environ.get("MODE", "paper")
DRY_RUN = os.environ.get("DRY_RUN", "true").lower() != "false"
MODEL_VERSION = os.environ.get("MODEL_VERSION", "lgbm_fwd_30m_v1.0.0")
K_LONG = int(os.environ.get("K_LONG", "3"))
K_SHORT = int(os.environ.get("K_SHORT", "3"))
NOTIONAL_PER_NAME = float(os.environ.get("NOTIONAL_PER_NAME", "200"))
GROSS_CAP = float(os.environ.get("GROSS_CAP", "2000"))
MAX_DAILY_LOSS = float(os.environ.get("MAX_DAILY_LOSS", "150"))
EOD_FLATTEN_MIN = int(os.environ.get("EOD_FLATTEN_MIN", "12"))   # flatten this many min before close
STALENESS_MAX_MIN = int(os.environ.get("STALENESS_MAX_MIN", "35"))
MIN_SCORE_SEP = float(os.environ.get("MIN_SCORE_SEP", "0.0005"))
LOOP_SECONDS = int(os.environ.get("LOOP_SECONDS", "30"))
# Symbols barred from the live basket because their LIVE feed is known-bad — the model
# scores them off corrupt stream bars so their rank is artifact, not signal. KLAC (2026-06-12):
# stream bars persistently 10x the true price (feed scaling bug); the research panel is
# backfill-sourced and verified unaffected, but model-server computes live features from stream.
# REMOVAL CONDITION: out only when prod-architect confirms the KLAC ingestion fix is live AND
# QA's parity check shows KLAC stream==backfill. Env-extensible for the Nx-sibling sweep.
SYMBOL_DENYLIST = {
    sym.strip().upper() for sym in os.environ.get("SYMBOL_DENYLIST", "KLAC").split(",") if sym.strip()
}
# Data-driven successor to the manual denylist (task #18): exclude any name with a split ex-date
# inside the feature-lookback window, since its multi-bar features straddle the adjustment boundary
# (the KLAC failure class). Must cover the longest backward feature window — currently mom_10d
# (10 trading days ≈ 14 calendar) → default 15. Splits only by default (dividends don't corrupt
# intraday momentum). Pairs with QA's price-match invariant (the backstop for already-bad history).
EX_DATE_LOOKBACK_DAYS = int(os.environ.get("EX_DATE_LOOKBACK_DAYS", "15"))
# Marketable-limit cross beyond the touch, scaled to the spread so wide-spread/low-priced names
# stay marketable even if the quote ticks between snapshot and submit (a fixed 1c cross left
# FLY resting inside the bid on 6/12). buy = ask + buffer, sell = bid - buffer, where
# buffer = max(1 tick, CROSS_SPREAD_FRAC x spread). Tunable; slippage view measures the cost.
CROSS_SPREAD_FRAC = float(os.environ.get("CROSS_SPREAD_FRAC", "0.5"))
# Alpaca terminal order states — an order here is done (won't fill further); anything else is open.
TERMINAL_ORDER_STATES = {"filled", "canceled", "expired", "rejected", "done_for_day", "replaced"}
_NY = ZoneInfo("America/New_York")

DB_KWARGS = {
    "host": os.environ["DB_HOST"], "port": int(os.environ.get("DB_PORT", "5432")),
    "dbname": os.environ["DB_NAME"], "user": os.environ["DB_USER"],
    "password": os.environ["DB_PASSWORD"],
}

trading = TradingClient(
    os.environ["ALPACA_KEY_ID"], os.environ["ALPACA_SECRET_KEY"], paper=(MODE == "paper")
)
data_client = StockHistoricalDataClient(os.environ["ALPACA_KEY_ID"], os.environ["ALPACA_SECRET_KEY"])


def marketable_limit(symbols: list[str], fallback: dict[str, float]) -> dict[str, dict[str, float | None]]:
    """Per-symbol marketable-limit prices from the LIVE NBBO: buy = ask + buffer, sell = bid - buffer,
    where buffer = max(1 tick, CROSS_SPREAD_FRAC x spread). The spread-scaled cross keeps wide-spread/
    low-priced names marketable even if the quote ticks between snapshot and submit — a fixed 1c cross
    left FLY resting inside the bid on 6/12. Also returns the arrival NBBO bid/ask/mid captured at
    submit; we persist mid on orders_log so per-leg slippage (fill vs arrival mid = our measured
    one-way cost) is computable. Falls back to last_close ±0.5% (mid=close) if a quote is missing."""
    out: dict[str, dict[str, float | None]] = {}
    try:
        quotes = data_client.get_stock_latest_quote(StockLatestQuoteRequest(symbol_or_symbols=symbols))
    except APIError:
        quotes = {}
    for symbol in symbols:
        q = quotes.get(symbol)
        ask = float(q.ask_price) if q and q.ask_price else 0.0
        bid = float(q.bid_price) if q and q.bid_price else 0.0
        if ask > 0 and bid > 0:
            buffer = max(0.01, round((ask - bid) * CROSS_SPREAD_FRAC, 2))
            out[symbol] = {"buy": round(ask + buffer, 2), "sell": round(max(0.01, bid - buffer), 2),
                           "bid": bid, "ask": ask, "mid": round((bid + ask) / 2, 4)}
        else:
            close = fallback[symbol]
            out[symbol] = {"buy": round(close * 1.005, 2), "sell": round(close * 0.995, 2),
                           "bid": None, "ask": None, "mid": round(close, 4)}
    return out

STATE_DDL = """
CREATE TABLE IF NOT EXISTS executor_state (
    day date PRIMARY KEY, start_equity numeric, halted boolean NOT NULL DEFAULT false)
"""
PNL_DDL = """
CREATE TABLE IF NOT EXISTS pnl_daily (
    day date PRIMARY KEY, start_equity numeric, equity numeric, unrealized numeric,
    n_positions int, updated_at timestamptz)
"""
# Self-healing: 01_schema.sql only runs on a fresh DB, so add the attribution columns + views
# here (idempotent) for already-initialized databases.
EXEC_DDL = [
    "ALTER TABLE fills_log ADD COLUMN IF NOT EXISTS symbol text",
    "ALTER TABLE fills_log ADD COLUMN IF NOT EXISTS side text",
    "ALTER TABLE orders_log ADD COLUMN IF NOT EXISTS nbbo_bid numeric",
    "ALTER TABLE orders_log ADD COLUMN IF NOT EXISTS nbbo_ask numeric",
    "ALTER TABLE orders_log ADD COLUMN IF NOT EXISTS nbbo_mid numeric",
    "ALTER TABLE orders_log ADD COLUMN IF NOT EXISTS filled_qty numeric",
    """CREATE OR REPLACE VIEW realized_pnl_by_name AS
       SELECT fill_ts::date AS day, symbol,
              round(sum(CASE WHEN side='sell' THEN qty*price ELSE -qty*price END), 2) AS realized_pnl,
              sum(CASE WHEN side='buy'  THEN qty ELSE 0 END) AS bought_qty,
              sum(CASE WHEN side='sell' THEN qty ELSE 0 END) AS sold_qty,
              count(*) AS n_fills
       FROM fills_log WHERE symbol IS NOT NULL GROUP BY 1, 2""",
    # Per-leg execution slippage = our MEASURED one-way cost (directly comparable to the
    # battery's assumed cost_bps_oneway=2.0). slippage_bps = signed (fill - arrival_mid)/mid
    # in bps; positive = cost paid (bought above / sold below mid). arrival_mid is the live
    # NBBO mid stored at submit (arrival_src='nbbo'); for legs predating that capture it falls
    # back to the bars_1m close at the submit minute (arrival_src='bar_proxy').
    """CREATE OR REPLACE VIEW execution_slippage AS
       SELECT o.intended_at::date AS day, o.symbol, o.side, f.qty, f.price AS fill_price,
              o.limit_price, COALESCE(o.nbbo_mid, bar.close::numeric) AS arrival_mid,
              CASE WHEN o.nbbo_mid IS NOT NULL THEN 'nbbo' ELSE 'bar_proxy' END AS arrival_src,
              round((CASE WHEN o.side='buy' THEN f.price - COALESCE(o.nbbo_mid, bar.close::numeric)
                          ELSE COALESCE(o.nbbo_mid, bar.close::numeric) - f.price END)
                    / NULLIF(COALESCE(o.nbbo_mid, bar.close::numeric), 0) * 10000, 2) AS slippage_bps,
              round((CASE WHEN o.side='buy' THEN f.price - COALESCE(o.nbbo_mid, bar.close::numeric)
                          ELSE COALESCE(o.nbbo_mid, bar.close::numeric) - f.price END) * f.qty, 2) AS slippage_usd,
              o.submitted_at AS submit_ts          -- PIT key so the cost model can join ADV$/price per name
       FROM orders_log o
       JOIN fills_log f ON f.alpaca_order_id = o.alpaca_order_id
       LEFT JOIN LATERAL (SELECT close FROM bars_1m b
                          WHERE b.symbol = o.symbol AND b.ts < date_trunc('minute', o.submitted_at)
                          ORDER BY b.ts DESC LIMIT 1) bar ON true
       WHERE o.status = 'submitted'""",
    # Daily roll-up: oneway_cost_bps_* is the real number to feed long_short_backtest(cost_bps_oneway=).
    """CREATE OR REPLACE VIEW execution_slippage_daily AS
       SELECT day, count(*) AS n_legs,
              round(avg(slippage_bps), 2) AS oneway_cost_bps_mean,
              round((percentile_cont(0.5) WITHIN GROUP (ORDER BY slippage_bps))::numeric, 2) AS oneway_cost_bps_median,
              round(sum(slippage_usd), 2) AS slippage_usd_total,
              round(sum(qty * fill_price), 2) AS gross_traded_usd
       FROM execution_slippage GROUP BY day ORDER BY day""",
]
INTENT_SQL = """
INSERT INTO orders_log
    (client_order_id, symbol, side, qty, order_type, limit_price, mode, intended_at, status,
     prediction, model_version, nbbo_bid, nbbo_ask, nbbo_mid)
VALUES (%s,%s,%s,%s,'limit',%s,%s,%s,'intended',%s,%s,%s,%s,%s)
ON CONFLICT (client_order_id) DO NOTHING
"""


def ex_date_excluded(conn: psycopg.Connection, as_of: date) -> set[str]:
    """Names with a split ex-date inside the feature-lookback window — excluded until their series
    is verified consistent (the data-driven successor to the manual denylist). Returns empty (with a
    warning) while prod's corporate_actions table is not yet created — the manual SYMBOL_DENYLIST
    still covers known cases until then; QA's price-match invariant is the other backstop."""
    try:
        return names_with_recent_ex_date(conn, as_of, EX_DATE_LOOKBACK_DAYS)
    except psycopg.errors.UndefinedTable:
        logger.warning("corporate_actions table absent; ex-date guard INACTIVE (manual denylist applies)")
        return set()


def candidate_pool(conn: psycopg.Connection, ts: datetime, today: date) -> list[dict]:
    excluded = SYMBOL_DENYLIST | ex_date_excluded(conn, today)
    with conn.cursor() as cur:
        cur.execute(
            """SELECT p.symbol, p.score, am.easy_to_borrow, am.name,
                      (SELECT close FROM bars_1m b WHERE b.symbol=p.symbol AND b.ts<=%s
                       ORDER BY b.ts DESC LIMIT 1) AS last_close
               FROM predictions p JOIN asset_metadata am ON am.symbol=p.symbol
               WHERE p.model_version=%s AND p.ts=%s""",
            (ts, MODEL_VERSION, ts),
        )
        rows = cur.fetchall()
    pool = []
    for symbol, score, easy_to_borrow, name, last_close in rows:
        if symbol in excluded or is_etf_like(name) or last_close is None or last_close < 5:
            continue
        pool.append({"symbol": symbol, "score": float(score), "etb": bool(easy_to_borrow),
                     "price": float(last_close)})
    return pool


def build_basket(pool: list[dict]) -> tuple[list[dict], list[dict]]:
    ranked = sorted(pool, key=lambda r: (-r["score"], r["symbol"]))
    longs = ranked[:K_LONG]
    shorts = [r for r in reversed(ranked) if r["etb"]][:K_SHORT]
    short_syms = {r["symbol"] for r in shorts}
    longs = [r for r in longs if r["symbol"] not in short_syms]
    for leg in longs + shorts:
        leg["qty"] = max(1, int(NOTIONAL_PER_NAME // leg["price"]))
    return longs, shorts


def ensure_session_state(conn: psycopg.Connection, today: object) -> dict:
    """One row per trading day: start_equity (for the kill-switch) + halted flag (persists
    across restarts so a tripped kill-switch stays tripped)."""
    equity = float(trading.get_account().equity)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO executor_state (day, start_equity) VALUES (%s,%s) "
                    "ON CONFLICT (day) DO NOTHING", (today, equity))
        cur.execute("SELECT start_equity, halted FROM executor_state WHERE day=%s", (today,))
        start_equity, halted = cur.fetchone()
    return {"start_equity": float(start_equity), "halted": halted, "equity": equity}


def traded_today(conn: psycopg.Connection, today: object) -> bool:
    # mode-agnostic: ANY submitted order today means we've traded (don't re-submit and trip
    # Alpaca's unique-client_order_id). NY date matches intended_at::date during RTH.
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM orders_log WHERE intended_at::date=%s AND status!='intended' "
                    "LIMIT 1", (today,))
        return cur.fetchone() is not None


def submit_basket(conn: psycopg.Connection, ts: datetime, today: object) -> None:
    pool = candidate_pool(conn, ts, today)
    if len(pool) < K_LONG + K_SHORT:
        logger.warning("pool too small (%d); skip", len(pool)); return
    longs, shorts = build_basket(pool)
    sep = (min(l["score"] for l in longs) - max(s["score"] for s in shorts)) if (longs and shorts) else 0.0
    if sep < MIN_SCORE_SEP:
        logger.warning("degenerate scores (sep %.6f); skip — won't trade tie-break noise", sep); return
    gross = sum(leg["qty"] * leg["price"] for leg in longs + shorts)
    account = trading.get_account()
    cap = min(GROSS_CAP, float(account.equity) * 0.05)        # cap from FRESH broker snapshot
    if gross > cap:
        logger.error("gross %.0f > cap %.0f (equity %.0f); skip", gross, cap, float(account.equity)); return
    rb = ts.strftime("%Y%m%dT%H%M")
    legs = longs + shorts
    prices = marketable_limit([leg["symbol"] for leg in legs], {leg["symbol"]: leg["price"] for leg in legs})
    for leg, side in [(x, "buy") for x in longs] + [(x, "sell") for x in shorts]:
        coid = f"{MODEL_VERSION}-{rb}-{leg['symbol']}-{side}"
        px = prices[leg["symbol"]]
        limit = px[side]                                      # marketable limit from live NBBO
        with conn.cursor() as cur:                            # persist INTENT + arrival NBBO before submit
            cur.execute(INTENT_SQL, (coid, leg["symbol"], side, leg["qty"], limit, MODE,
                                     datetime.now(timezone.utc), leg["score"], MODEL_VERSION,
                                     px["bid"], px["ask"], px["mid"]))
        if DRY_RUN:
            continue
        try:
            order = trading.submit_order(LimitOrderRequest(
                symbol=leg["symbol"], qty=leg["qty"], limit_price=limit, time_in_force=TimeInForce.DAY,
                side=OrderSide.BUY if side == "buy" else OrderSide.SELL, client_order_id=coid))
        except APIError as exc:
            if "unique" in str(exc):                          # coid already submitted: idempotent skip
                logger.warning("coid %s already submitted; skipping", coid); continue
            raise
        with conn.cursor() as cur:
            cur.execute("UPDATE orders_log SET status='submitted', submitted_at=now(), mode=%s, "
                        "alpaca_order_id=%s WHERE client_order_id=%s", (MODE, str(order.id), coid))
    logger.info("BASKET %s | longs=%s shorts=%s gross=%.0f | %s", ts.isoformat(),
                [l["symbol"] for l in longs], [s["symbol"] for s in shorts], gross,
                "DRY-RUN (logged)" if DRY_RUN else "SUBMITTED (paper)")


def flatten_all(reason: str, positions: list) -> None:
    """TERMINATE bets: cancel open orders + close all positions. Called whenever positions
    exist in any termination state, so it RE-RUNS each cycle until flat (verify-by-retry —
    a partial/failed close gets retried next cycle instead of being assumed done)."""
    if not positions:
        return
    logger.warning("FLATTEN (%s): closing %d positions + cancelling open orders", reason, len(positions))
    if not DRY_RUN:
        trading.close_all_positions(cancel_orders=True)


def sync_orders_and_fills(conn: psycopg.Connection, orders: list) -> None:
    """Single broker-truth pass over today's orders: (1) write each order's CURRENT status +
    cumulative filled_qty back to orders_log (it used to be stuck at 'submitted' — terminal-blind,
    so the ledger showed all-time filled=0 despite real fills); (2) record realized fills to
    fills_log for P&L/attribution, including PARTIALS (a partially-filled order ends terminal as
    'canceled' with filled_qty>0 and would otherwise be lost). fill_ts uses the order's stable
    terminal timestamp so the (alpaca_order_id, fill_ts) upsert hits ONE row — no double counting.
    Drives the symmetric reconcile + truthful realized P&L."""
    if DRY_RUN:
        return
    with conn.cursor() as cur:
        for order in orders:
            filled_qty = float(order.filled_qty or 0)
            cur.execute("UPDATE orders_log SET status=%s, filled_qty=%s WHERE alpaca_order_id=%s",
                        (order.status.value, filled_qty, str(order.id)))
            if order.status.value in TERMINAL_ORDER_STATES and filled_qty > 0 and order.filled_avg_price:
                fill_ts = order.filled_at or order.canceled_at or order.updated_at
                cur.execute(
                    "INSERT INTO fills_log (alpaca_order_id, fill_ts, qty, price, symbol, side) "
                    "VALUES (%s,%s,%s,%s,%s,%s) "
                    "ON CONFLICT (alpaca_order_id, fill_ts) DO UPDATE "
                    "SET qty=EXCLUDED.qty, price=EXCLUDED.price, symbol=EXCLUDED.symbol, side=EXCLUDED.side",
                    (str(order.id), fill_ts, filled_qty,
                     float(order.filled_avg_price), order.symbol, order.side.value))


def write_pnl(conn: psycopg.Connection, today: object, state: dict, positions: list) -> None:
    """Daily P&L record so we can say what the day's bets did: equity vs session-start (total
    realized+unrealized+fees) and the book's mark-to-market unrealized. After the EOD flatten
    the final row is the realized day P&L (equity - start_equity, book flat)."""
    unrealized = sum(float(p.unrealized_pl) for p in positions)
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO pnl_daily (day, start_equity, equity, unrealized, n_positions, updated_at)
               VALUES (%s,%s,%s,%s,%s, now())
               ON CONFLICT (day) DO UPDATE SET equity=EXCLUDED.equity, unrealized=EXCLUDED.unrealized,
                   n_positions=EXCLUDED.n_positions, updated_at=now()""",
            (today, state["start_equity"], state["equity"], round(unrealized, 2), len(positions)))


def reconcile(conn: psycopg.Connection, positions: list, today: object) -> None:
    """Broker-truth probe with a MEANINGFUL ok: flag UNEXPECTED broker positions (a symbol
    we didn't submit today) — the dangerous desync. After an EOD flatten the broker is empty
    so ok stays true; a stray/unintended position trips ok=false."""
    broker = {p.symbol: round(float(p.qty), 4) for p in positions}
    with conn.cursor() as cur:
        cur.execute("SELECT DISTINCT symbol FROM orders_log WHERE intended_at::date=%s "
                    "AND status='submitted' AND mode=%s", (today, MODE))
        expected_syms = {row[0] for row in cur.fetchall()}
        unexpected = sorted(s for s in broker if s not in expected_syms)
        ok = len(unexpected) == 0
        cur.execute("INSERT INTO reconciliation_log (ts, ok, detail) VALUES (now(), %s, %s)",
                    (ok, json.dumps({"mode": "dry_run" if DRY_RUN else MODE,
                                     "broker": broker, "unexpected": unexpected})))
    if not ok:
        logger.warning("reconcile DRIFT: unexpected broker positions %s", unexpected)


def main() -> None:
    logger.info("executor starting: mode=%s dry_run=%s model=%s K=%d/%d notional=%.0f cap=%.0f denylist=%s",
                MODE, DRY_RUN, MODEL_VERSION, K_LONG, K_SHORT, NOTIONAL_PER_NAME, GROSS_CAP,
                sorted(SYMBOL_DENYLIST))
    with psycopg.connect(**DB_KWARGS, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(STATE_DDL)
        cur.execute(PNL_DDL)
        for stmt in EXEC_DDL:
            cur.execute(stmt)
    while True:
        try:
            with psycopg.connect(**DB_KWARGS, autocommit=True) as conn:
                clock = trading.get_clock()
                now = datetime.now(timezone.utc)
                today = now.astimezone(_NY).date()
                state = ensure_session_state(conn, today)
                positions = trading.get_all_positions()       # broker truth, fetched once
                kill_breach = state["equity"] < state["start_equity"] - MAX_DAILY_LOSS
                in_eod = clock.is_open and (clock.next_close - now).total_seconds() / 60 <= EOD_FLATTEN_MIN
                stranded = (not clock.is_open) and bool(positions)   # missed-window / overnight catch-up
                # TERMINATION takes priority over everything and RE-RUNS until flat (robust to
                # restarts/partial failures) — halted does NOT skip flatten.
                if kill_breach and not state["halted"]:
                    with conn.cursor() as cur:
                        cur.execute("UPDATE executor_state SET halted=true WHERE day=%s", (today,))
                    state["halted"] = True
                    logger.error("KILL SWITCH: equity %.0f < start %.0f - %.0f",
                                 state["equity"], state["start_equity"], MAX_DAILY_LOSS)
                if state["halted"] or in_eod or stranded:
                    flatten_all("KILL_SWITCH" if state["halted"] else ("EOD" if in_eod else "CATCH_UP"),
                                positions)
                elif clock.is_open and not traded_today(conn, today):
                    with conn.cursor() as cur:
                        cur.execute("SELECT max(ts) FROM predictions WHERE model_version=%s", (MODEL_VERSION,))
                        ts = cur.fetchone()[0]
                    if ts and (now - ts).total_seconds() / 60 <= STALENESS_MAX_MIN:
                        submit_basket(conn, ts, today)        # one basket/day
                reconcile(conn, positions, today)
                capture_fills(conn, today)
                write_pnl(conn, today, state, positions)
        except (psycopg.Error, ValueError, KeyError, APIError) as exc:
            logger.error("cycle error: %s", exc)
        time.sleep(LOOP_SECONDS)


if __name__ == "__main__":
    main()
