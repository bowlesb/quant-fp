"""Entrypoint for the overnight-beta container: wire env -> handles -> run loop.

``python -m strategies.overnight_beta``. Env-driven (OvernightBetaConfig + the compose service). The live
``StorePanelLoader`` reads recent daily bars from the mounted ``/store/raw/bars`` (the SAME data the W11
certification used — parity by construction) to build the trailing daily-return panel for beta estimation.
Secrets (Alpaca keys, DB password) from the environment, never logged. PAPER ONLY; OBETA_ENABLED=0 by default.
"""

from __future__ import annotations

import glob
import os

import numpy as np
import polars as pl
import redis
from alpaca.trading.client import TradingClient

from quantlib.bus.compat import publish_contract
from quantlib.bus.publisher import DEFAULT_REDIS_URL
from quantlib.strategy_core.production_state import PgStateStore
from strategies.lib.overnight_beta_model import OvernightBetaModel
from strategies.lib.pg_ledger import FILLS_TABLE_DDL, PgFillLedger
from strategies.lib.store import StrategyStore
from strategies.overnight_beta.contract import STRATEGY_FEATURES, STRATEGY_NAME
from strategies.overnight_beta.position_store import STRATEGY as LEDGER_STRATEGY
from strategies.overnight_beta.position_store import PositionStore
from strategies.overnight_beta.strategy import (
    OvernightBetaConfig,
    OvernightBetaStrategy,
    PanelLoader,
)

DB_KWARGS: dict[str, str | int] = {
    "host": os.environ.get("DB_HOST", "timescaledb"),
    "port": int(os.environ.get("DB_PORT", "5432")),
    "dbname": os.environ.get("DB_NAME", "quant"),
    "user": os.environ.get("DB_USER", "quant"),
    "password": os.environ["DB_PASSWORD"],
}

STORE_BARS = os.environ.get("OBETA_STORE_BARS", "/store/raw/bars")
MARKET_SYMBOL = os.environ.get("OBETA_MARKET", "SPY")
PANEL_DAYS = int(os.environ.get("OBETA_PANEL_DAYS", "90"))
UNIVERSE_TOP_N = int(os.environ.get("OBETA_UNIVERSE_TOP_N", "300"))


class StorePanelLoader(PanelLoader):
    """Reads the trailing daily-return panel + the latest close/open from the mounted raw bars.

    Daily close/open per (symbol, date) from the last RTH bar / first RTH bar (UTC: 13:30–20:00). Returns the
    trailing ``PANEL_DAYS`` daily returns per liquid name + the market (SPY). Built from the SAME raw bars the
    certification used, so the live betas match the research betas (parity)."""

    def __init__(self, bars_root: str, market: str, panel_days: int, top_n: int) -> None:
        self._root = bars_root
        self._market = market
        self._panel_days = panel_days
        self._top_n = top_n

    def _daily(self, symbol: str) -> pl.DataFrame | None:
        files = sorted(glob.glob(os.path.join(self._root, f"symbol={symbol}", "date=*", "*.parquet")))
        if not files:
            return None
        frames = []
        for f in files[-(self._panel_days + 5) :]:
            df = pl.read_parquet(f)
            if df.height == 0:
                continue
            df = df.with_columns(
                (pl.col("ts").dt.hour().cast(pl.Int32) * 60 + pl.col("ts").dt.minute().cast(pl.Int32)).alias(
                    "m"
                )
            ).filter((pl.col("m") >= 810) & (pl.col("m") < 1200))
            if df.height == 0:
                continue
            date = os.path.basename(os.path.dirname(f)).split("=")[1]
            frames.append(
                pl.DataFrame(
                    {
                        "date": [date],
                        "open": [float(df.sort("m")["open"][0])],
                        "close": [float(df.sort("m")["close"][-1])],
                        "dollar_vol": [float((df["close"] * df["volume"]).sum())],
                    }
                )
            )
        return pl.concat(frames) if frames else None

    def _all_dailies(self) -> dict[str, pl.DataFrame]:
        out: dict[str, pl.DataFrame] = {}
        symbols = [
            os.path.basename(d).split("=")[1] for d in glob.glob(os.path.join(self._root, "symbol=*"))
        ]
        for s in symbols:
            d = self._daily(s)
            if d is not None and d.height >= self._panel_days // 2:
                out[s] = d
        return out

    def load(self) -> tuple[dict[str, np.ndarray], np.ndarray]:
        dailies = self._all_dailies()
        if self._market not in dailies:
            return {}, np.array([])
        # liquid top-N by median dollar-volume
        liquid = sorted(
            (s for s in dailies if s != self._market),
            key=lambda s: -float(dailies[s]["dollar_vol"].median()),
        )[: self._top_n]
        mkt_ret = dailies[self._market]["close"].pct_change().drop_nulls().to_numpy()[-self._panel_days :]
        returns_by_name: dict[str, np.ndarray] = {}
        for s in liquid:
            r = dailies[s]["close"].pct_change().drop_nulls().to_numpy()[-self._panel_days :]
            if len(r) == len(mkt_ret):
                returns_by_name[s] = r
        return returns_by_name, mkt_ret

    def last_close(self, symbol: str) -> float | None:
        d = self._daily(symbol)
        return float(d["close"][-1]) if d is not None and d.height else None

    def last_open(self, symbol: str) -> float | None:
        d = self._daily(symbol)
        return float(d["open"][-1]) if d is not None and d.height else None


def main() -> None:
    config = OvernightBetaConfig.from_env()
    trading = TradingClient(os.environ["ALPACA_KEY_ID"], os.environ["ALPACA_SECRET_KEY"], paper=True)
    store = PositionStore(DB_KWARGS)
    # the durable StrategyState fill ledger (the migration SoT), additive in the SAME strat_overnightbeta
    # schema alongside the untouched positions + slippage_log tables (backward-readable: an OLD-path
    # rollback ignores it). LEDGER_STRATEGY matches the position-store's schema name (no underscore).
    state_store = PgStateStore(PgFillLedger(StrategyStore(LEDGER_STRATEGY, [FILLS_TABLE_DDL], DB_KWARGS)))
    model = OvernightBetaModel(beta_window=config.beta_window, quantile=config.quantile)
    panel = StorePanelLoader(STORE_BARS, MARKET_SYMBOL, PANEL_DAYS, UNIVERSE_TOP_N)
    # publish the (empty) bus-feature contract so the pre-deploy gate sees this strategy as present (B3) —
    # overnight_beta reads no per-minute bus features, only a daily-return panel from the store.
    bus = redis.Redis.from_url(os.environ.get("BUS_REDIS_URL", DEFAULT_REDIS_URL))
    publish_contract(bus, STRATEGY_NAME, STRATEGY_FEATURES)
    strategy = OvernightBetaStrategy(config, trading, store, model, panel, state_store=state_store)
    strategy.run()


if __name__ == "__main__":
    main()
