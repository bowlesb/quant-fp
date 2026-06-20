"""Entrypoint for the smoke strategy container: wire env -> handles -> run loop.

``python -m strategies.smoke``. All configuration is environment-driven (see SmokeConfig + the
compose service). Secrets (Alpaca keys, DB password) are read from the environment and NEVER logged.
"""

from __future__ import annotations

import os

from alpaca.trading.client import TradingClient

from quantlib.bus.consumer import BusConsumer
from quantlib.bus.publisher import DEFAULT_REDIS_URL
from quantlib.strategy_core.production_state import PgStateStore
from strategies.lib.model import MockMLModel
from strategies.lib.pg_ledger import FILLS_TABLE_DDL, PgFillLedger
from strategies.lib.store import StrategyStore
from strategies.smoke.bet_store import BetStore
from strategies.smoke.contract import STRATEGY_NAME
from strategies.smoke.strategy import MODEL_FOLD_FEATURES, SmokeConfig, SmokeStrategy

DB_KWARGS: dict[str, str | int] = {
    "host": os.environ.get("DB_HOST", "timescaledb"),
    "port": int(os.environ.get("DB_PORT", "5432")),
    "dbname": os.environ.get("DB_NAME", "quant"),
    "user": os.environ.get("DB_USER", "quant"),
    "password": os.environ["DB_PASSWORD"],
}


def main() -> None:
    config = SmokeConfig.from_env()
    bus_url = os.environ.get("BUS_REDIS_URL", DEFAULT_REDIS_URL)
    consumer = BusConsumer(config.symbols, url=bus_url)
    trading = TradingClient(os.environ["ALPACA_KEY_ID"], os.environ["ALPACA_SECRET_KEY"], paper=True)
    store = BetStore(DB_KWARGS)
    # the durable StrategyState fill ledger (the migration SoT), additive in the SAME strat_smoke schema
    # alongside the retained `bets` table (backward-readable: a rolled-back OLD-path container ignores it).
    ledger_store = StrategyStore(STRATEGY_NAME, [FILLS_TABLE_DDL], DB_KWARGS)
    state_store = PgStateStore(PgFillLedger(ledger_store))
    model = MockMLModel(MODEL_FOLD_FEATURES)
    strategy = SmokeStrategy(config, consumer, trading, store, model, state_store=state_store)
    strategy.run()


if __name__ == "__main__":
    main()
