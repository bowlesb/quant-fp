"""Entrypoint for the reversion strategy container: wire env -> handles -> run loop.

``python -m strategies.reversion``. All configuration is environment-driven (see ReversionConfig + the
compose service). Secrets (Alpaca keys, DB password) are read from the environment and NEVER logged.
"""

from __future__ import annotations

import os

from alpaca.trading.client import TradingClient

from quantlib.bus.consumer import BusConsumer
from quantlib.bus.publisher import DEFAULT_REDIS_URL
from quantlib.strategy_core.production_state import PgStateStore
from strategies.lib.pg_ledger import FILLS_TABLE_DDL, PgFillLedger
from strategies.lib.reversion_model import VwapReversionModel
from strategies.lib.store import StrategyStore
from strategies.reversion.bet_store import BetStore
from strategies.reversion.contract import STRATEGY_NAME
from strategies.reversion.strategy import ReversionConfig, ReversionStrategy

DB_KWARGS: dict[str, str | int] = {
    "host": os.environ.get("DB_HOST", "timescaledb"),
    "port": int(os.environ.get("DB_PORT", "5432")),
    "dbname": os.environ.get("DB_NAME", "quant"),
    "user": os.environ.get("DB_USER", "quant"),
    "password": os.environ["DB_PASSWORD"],
}


def main() -> None:
    config = ReversionConfig.from_env()
    bus_url = os.environ.get("BUS_REDIS_URL", DEFAULT_REDIS_URL)
    consumer = BusConsumer(config.symbols, url=bus_url)
    trading = TradingClient(os.environ["ALPACA_KEY_ID"], os.environ["ALPACA_SECRET_KEY"], paper=True)
    store = BetStore(DB_KWARGS)
    # the durable StrategyState fill ledger (the migration SoT), additive in the SAME strat_reversion
    # schema alongside the retained `bets` table (backward-readable: an OLD-path rollback ignores it).
    state_store = PgStateStore(PgFillLedger(StrategyStore(STRATEGY_NAME, [FILLS_TABLE_DDL], DB_KWARGS)))
    model = VwapReversionModel(window_m=config.vwap_window_m, sensitivity=config.sensitivity)
    strategy = ReversionStrategy(config, consumer, trading, store, model, state_store=state_store)
    strategy.run()


if __name__ == "__main__":
    main()
