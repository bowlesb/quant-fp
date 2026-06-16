"""Entrypoint for the smoke strategy container: wire env -> handles -> run loop.

``python -m strategies.smoke``. All configuration is environment-driven (see SmokeConfig + the
compose service). Secrets (Alpaca keys, DB password) are read from the environment and NEVER logged.
"""
from __future__ import annotations

import os

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.trading.client import TradingClient

from quantlib.bus.consumer import BusConsumer
from quantlib.bus.publisher import DEFAULT_REDIS_URL
from strategies.lib.model import MockMLModel
from strategies.smoke.bet_store import BetStore
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
    trading = TradingClient(
        os.environ["ALPACA_KEY_ID"], os.environ["ALPACA_SECRET_KEY"], paper=True
    )
    data_client = StockHistoricalDataClient(
        os.environ["ALPACA_KEY_ID"], os.environ["ALPACA_SECRET_KEY"]
    )
    store = BetStore(DB_KWARGS)
    model = MockMLModel(MODEL_FOLD_FEATURES)
    strategy = SmokeStrategy(config, consumer, trading, data_client, store, model)
    strategy.run()


if __name__ == "__main__":
    main()
