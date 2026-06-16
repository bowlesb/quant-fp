"""One-shot corporate-actions backfill CLI: populate the `corporate_actions` table for the whole
universe over a configurable lookback window (default ~6 months; deeper is cheap and helps the thin
split sample).

This reuses the authoritative `quantlib.corporate_actions` fetch + upsert (the SAME path the
backfill-manager runs once/day for a narrow ±35-day window) — it does NOT define a second corporate-
action source. The only thing it adds is a runnable entrypoint that sweeps a WIDE date range for the
FULL universe in one pass, so the table is populated for research (H4 splits / H5 dividends) instead
of only the rolling recent window the manager keeps fresh.

Schema written (one row per symbol/action_type/ex_date), with the look-ahead-safe anchor being
`ex_date` — Alpaca's CA payload carries NO declaration/announcement date (verified; see
db/init/05_corporate_actions.sql), so anticipation features are not supportable from this source and a
true declaration feed would be needed for them. The `corporate_actions_pit` view exposes the
research-ready normalized shape (split_ratio, cash_amount, action_type).

Run inside the fp-dev image with Alpaca creds + DB_* in env:
    python -m quantlib.data.corporate_actions_backfill --months 24
A --symbols AAPL,KLAC sample mode backfills just those names for evidence without the universe query.
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import os
import sys

import psycopg
from alpaca.data.historical.corporate_actions import CorporateActionsClient
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import AssetClass, AssetStatus
from alpaca.trading.requests import GetAssetsRequest

from quantlib.corporate_actions import fetch_corporate_actions, upsert_corporate_actions

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
)
logger = logging.getLogger("corporate_actions_backfill")

DEFAULT_MONTHS = 6
DAYS_PER_MONTH = 31
CA_PAUSE_SECONDS = (
    0.3  # between 50-symbol CA chunks; mirrors the backfill-manager poll cadence
)


def db_kwargs() -> dict[str, object]:
    return {
        "host": os.environ["DB_HOST"],
        "port": int(os.environ.get("DB_PORT", "5432")),
        "dbname": os.environ["DB_NAME"],
        "user": os.environ["DB_USER"],
        "password": os.environ["DB_PASSWORD"],
    }


def trading_client() -> TradingClient:
    return TradingClient(
        os.environ["ALPACA_KEY_ID"], os.environ["ALPACA_SECRET_KEY"], paper=True
    )


def corporate_actions_client() -> CorporateActionsClient:
    return CorporateActionsClient(
        os.environ["ALPACA_KEY_ID"], os.environ["ALPACA_SECRET_KEY"]
    )


def universe_symbols(client: TradingClient) -> list[str]:
    """All active, tradable US-equity single names (mirrors raw_backfill's universe screen, minus the
    market-index appendix — corporate actions are a per-name fact, not a cross-sectional reference).
    """
    assets = client.get_all_assets(
        GetAssetsRequest(status=AssetStatus.ACTIVE, asset_class=AssetClass.US_EQUITY)
    )
    symbols = [
        asset.symbol for asset in assets if asset.tradable and "/" not in asset.symbol
    ]
    return sorted(set(symbols))


def run(months: int, symbols: list[str] | None) -> None:
    end = dt.datetime.now(dt.timezone.utc).date()
    start = end - dt.timedelta(days=months * DAYS_PER_MONTH)

    if symbols is None:
        symbols = universe_symbols(trading_client())
        logger.info("universe: %d symbols", len(symbols))
    else:
        logger.info("SAMPLE mode: %d symbols", len(symbols))

    logger.info(
        "corporate-actions backfill: %d symbols, window %s..%s (%d months)",
        len(symbols),
        start.isoformat(),
        end.isoformat(),
        months,
    )

    ca_client = corporate_actions_client()
    actions = fetch_corporate_actions(ca_client, symbols, start, end, CA_PAUSE_SECONDS)
    logger.info("fetched %d corporate actions", len(actions))

    with psycopg.connect(**db_kwargs(), autocommit=True) as conn:
        newly_inserted = upsert_corporate_actions(conn, actions)
    logger.info(
        "DONE: %d actions upserted (%d symbols newly-actioned)",
        len(actions),
        len(newly_inserted),
    )


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="One-shot corporate-actions universe backfill"
    )
    parser.add_argument(
        "--months",
        type=int,
        default=DEFAULT_MONTHS,
        help="lookback window in months (date range is one request param — deeper is ~free)",
    )
    parser.add_argument(
        "--symbols",
        default=None,
        help="comma list => SAMPLE mode (skip the universe query)",
    )
    return parser.parse_args(argv)


def main() -> None:
    args = parse_args(sys.argv[1:])
    symbols = (
        [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
        if args.symbols
        else None
    )
    run(args.months, symbols)


if __name__ == "__main__":
    main()
