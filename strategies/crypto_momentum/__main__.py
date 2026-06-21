"""Entrypoint for the crypto-momentum strategy container: wire env -> handles -> run loop.

``python -m strategies.crypto_momentum``. All configuration is environment-driven (see
CryptoMomentumConfig + the compose service). Secrets (Alpaca keys, DB password) are read from the
environment and NEVER logged. The bus consumer is bound to the SEPARATE ``fv:crypto`` namespace so it can
only ever read crypto vectors, never the equity ``fv:<symbol>`` streams.
"""

from __future__ import annotations

import os

from alpaca.trading.client import TradingClient

from quantlib.bus.consumer import BusConsumer
from quantlib.bus.publisher import DEFAULT_REDIS_URL
from quantlib.strategy_core.production_state import PgStateStore
from strategies.crypto_momentum.bet_store import BetStore
from strategies.crypto_momentum.contract import STRATEGY_NAME
from strategies.crypto_momentum.strategy import (
    CRYPTO_BUS_PREFIX,
    CryptoMomentumConfig,
    CryptoMomentumStrategy,
)
from strategies.lib.crypto_momentum_model import CryptoMomentumModel
from strategies.lib.pg_ledger import FILLS_TABLE_DDL, PgFillLedger
from strategies.lib.store import StrategyStore

DB_KWARGS: dict[str, str | int] = {
    "host": os.environ.get("DB_HOST", "timescaledb"),
    "port": int(os.environ.get("DB_PORT", "5432")),
    "dbname": os.environ.get("DB_NAME", "quant"),
    "user": os.environ.get("DB_USER", "quant"),
    "password": os.environ["DB_PASSWORD"],
}


def main() -> None:
    config = CryptoMomentumConfig.from_env()
    bus_url = os.environ.get("BUS_REDIS_URL", DEFAULT_REDIS_URL)
    # bind to the SEPARATE crypto bus namespace (fv:crypto:<SYMBOL>) with the slashless symbols.
    consumer = BusConsumer(config.symbols, url=bus_url, prefix=CRYPTO_BUS_PREFIX)
    trading = TradingClient(os.environ["ALPACA_KEY_ID"], os.environ["ALPACA_SECRET_KEY"], paper=True)
    store = BetStore(DB_KWARGS)
    # the durable StrategyState fill ledger (the SoT), additive in the SAME strat_cryptomomentum schema
    # alongside the retained `bets` table (backward-readable: an OLD-path rollback ignores it).
    state_store = PgStateStore(PgFillLedger(StrategyStore(STRATEGY_NAME, [FILLS_TABLE_DDL], DB_KWARGS)))
    model = CryptoMomentumModel(window_m=config.ret_window_m, sensitivity=config.sensitivity)
    strategy = CryptoMomentumStrategy(config, consumer, trading, store, model, state_store=state_store)
    strategy.run()


if __name__ == "__main__":
    main()
