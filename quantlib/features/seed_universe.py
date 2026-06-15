"""Seed the trading universe + asset reference for a session from Alpaca's tradable US-equity assets.

Pulls active, tradable US-equity common stocks (alpaca-py ``get_all_assets``), keeps the primary-listed
exchanges (NASDAQ / NYSE / AMEX / ARCA — drops OTC and the BATS/IEX test venue), caps at ``MAX_SYMBOLS``
for night-1 single-websocket sanity, then UPSERTs:

  * ``asset_metadata`` — name / exchange / tradability flags for EVERY pulled asset (the sector & asset-flag
    features read this via ``load_reference``); refreshed each run.
  * ``universe_membership`` — the capped, in-universe symbol set for the target ``trade_date`` with a
    placeholder ``adv_dollar`` (real ADV ranking follows once backfill history accrues).

Run inside the fp-dev image with --env-file .env and the quant_default network:
    python -m quantlib.features.seed_universe <trade_date YYYY-MM-DD>
"""
from __future__ import annotations

import os
import sys

import psycopg
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import AssetClass, AssetStatus
from alpaca.trading.requests import GetAssetsRequest

# Primary US listing venues we keep — full-tape, liquid names. OTC = pink/grey markets (no SIP depth),
# BATS = the test/secondary venue Alpaca tags some names with; both dropped for night 1.
KEEP_EXCHANGES = {"NASDAQ", "NYSE", "AMEX", "ARCA"}
MAX_SYMBOLS = int(os.environ.get("UNIVERSE_MAX_SYMBOLS", "3000"))
PLACEHOLDER_ADV = 1_000_000.0  # stand-in until real ADV$ ranking lands from backfill history

DB_KWARGS: dict[str, str | int] = {
    "host": os.environ.get("DB_HOST", "timescaledb"),
    "port": int(os.environ.get("DB_PORT", "5432")),
    "dbname": os.environ.get("DB_NAME", "quant"),
    "user": os.environ.get("DB_USER", "quant"),
    "password": os.environ["DB_PASSWORD"],
}


def fetch_assets() -> list:
    trading = TradingClient(os.environ["ALPACA_KEY_ID"], os.environ["ALPACA_SECRET_KEY"], paper=True)
    return trading.get_all_assets(GetAssetsRequest(status=AssetStatus.ACTIVE, asset_class=AssetClass.US_EQUITY))


def upsert_asset_metadata(conn: psycopg.Connection, assets: list) -> int:
    rows = [
        (
            asset.symbol,
            asset.name,
            str(asset.exchange.value if hasattr(asset.exchange, "value") else asset.exchange),
            bool(asset.tradable),
            bool(asset.marginable),
            bool(asset.shortable),
            bool(asset.easy_to_borrow),
            bool(asset.fractionable),
        )
        for asset in assets
        if asset.tradable and "/" not in asset.symbol
    ]
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO asset_metadata
                (symbol, name, exchange, tradable, marginable, shortable, easy_to_borrow, fractionable, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now())
            ON CONFLICT (symbol) DO UPDATE SET
                name = EXCLUDED.name, exchange = EXCLUDED.exchange, tradable = EXCLUDED.tradable,
                marginable = EXCLUDED.marginable, shortable = EXCLUDED.shortable,
                easy_to_borrow = EXCLUDED.easy_to_borrow, fractionable = EXCLUDED.fractionable,
                updated_at = now()
            """,
            rows,
        )
    return len(rows)


def select_universe(assets: list) -> list[str]:
    kept = [
        asset.symbol
        for asset in assets
        if asset.tradable
        and "/" not in asset.symbol
        and str(asset.exchange.value if hasattr(asset.exchange, "value") else asset.exchange) in KEEP_EXCHANGES
    ]
    return sorted(set(kept))[:MAX_SYMBOLS]


def upsert_universe(conn: psycopg.Connection, trade_date: str, symbols: list[str]) -> int:
    rows = [(trade_date, symbol, True, PLACEHOLDER_ADV) for symbol in symbols]
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO universe_membership (trade_date, symbol, in_universe, adv_dollar)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (trade_date, symbol) DO UPDATE SET
                in_universe = EXCLUDED.in_universe, adv_dollar = EXCLUDED.adv_dollar
            """,
            rows,
        )
    return len(rows)


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit("usage: python -m quantlib.features.seed_universe <trade_date YYYY-MM-DD>")
    trade_date = sys.argv[1]
    assets = fetch_assets()
    print(f"pulled {len(assets)} active US-equity assets from Alpaca", flush=True)
    with psycopg.connect(**DB_KWARGS) as conn:
        n_meta = upsert_asset_metadata(conn, assets)
        symbols = select_universe(assets)
        n_universe = upsert_universe(conn, trade_date, symbols)
        conn.commit()
    print(f"asset_metadata upserted: {n_meta}", flush=True)
    print(f"universe_membership upserted for {trade_date}: {n_universe} (cap {MAX_SYMBOLS})", flush=True)


if __name__ == "__main__":
    main()
