"""Measure the intraday SETTLE-LAG per data layer: how far behind "now" the historical/backfill API's
freshest settled minute trails, during market hours.

The within-day parity certifier (docs/WITHIN_DAY_PARITY_CERTIFICATION.md) must NEVER compare the live
tail — during the session the current minute is provisional (the SIP hasn't applied corrections; the
historical API hasn't served it). It compares only minutes that settled at least SETTLE_LAG ago. This
module MEASURES that lag directly, per layer, so the certifier picks a conservative window instead of
guessing.

The measurement is direct and honest: query the historical (backfill) API RIGHT NOW for today's tape of
a few liquid probe symbols, find the LATEST minute it returns, and report ``lag = now - latest_minute``
per layer (bars / trades / quotes). bars settle fastest, trades slower, quotes/sub-minute slowest
(PARITY_PLAYBOOK.md §2 layers A/B/C), so the certifier uses a per-layer lag — or, conservatively, the
WORST (largest) layer lag across the layers a group needs (gate-read answer #1: cover the worst-case
layer, tighten with data).

Run inside fp-dev (needs Alpaca creds from .env)::

    docker run --rm --env-file .env -v "$PWD":/app -w /app fp-dev \\
        python -m quantlib.features.settle_lag --symbols SPY,QQQ,AAPL

Outside RTH (nothing fresh to settle) it reports the off-session state rather than a misleading lag.
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging

import polars as pl

from quantlib.data.raw_backfill import data_client
from quantlib.data.raw_fetchers import (fetch_bars_day, fetch_quotes_day,
                                        fetch_trades_day)
from quantlib.features.session import CLOSE_MINUTE, OPEN_MINUTE

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("settle_lag")

DEFAULT_PROBE_SYMBOLS = ("SPY", "QQQ", "AAPL")

# Conservative per-layer fallback lags (minutes) used when the live probe can't run (off-session) — the
# certifier should still get a safe default. Sized to the historical-API intraday delay observed to date;
# bars settle fastest, quotes/sub-minute slowest. MEASURE to tighten (these are the safe ceiling).
FALLBACK_LAG_MINUTES = {"bars": 20, "trades": 25, "quotes": 30}

_ET_TZ = "America/New_York"


def latest_minute(frame: pl.DataFrame, ts_col: str) -> dt.datetime | None:
    """The most-recent timestamp in a probe frame (UTC), or None if the API returned nothing."""
    if frame.height == 0 or ts_col not in frame.columns:
        return None
    value = frame.select(pl.col(ts_col).max()).item()
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return value


def in_session(now_utc: dt.datetime) -> bool:
    """True if ``now_utc`` is inside RTH (09:30-16:00 ET) — only then is there a fresh tail to settle."""
    et_local = pl.Series([now_utc]).dt.convert_time_zone(_ET_TZ)[0]
    minute_of_day = et_local.hour * 60 + et_local.minute
    return OPEN_MINUTE <= minute_of_day <= CLOSE_MINUTE


def probe_layer_lag(
    client: object,
    symbols: list[str],
    day: dt.date,
    now_utc: dt.datetime,
) -> dict[str, float | None]:
    """For each layer, the lag in minutes between ``now_utc`` and the freshest settled minute the
    historical API returns for ``day`` across the probe symbols. None for a layer that returned nothing."""
    fetchers = {
        "bars": (fetch_bars_day, "ts"),
        "trades": (fetch_trades_day, "ts"),
        "quotes": (fetch_quotes_day, "ts"),
    }
    lags: dict[str, float | None] = {}
    for layer, (fetch, ts_col) in fetchers.items():
        freshest: dt.datetime | None = None
        for symbol in symbols:
            frame = fetch(client, symbol, day)  # type: ignore[arg-type]
            latest = latest_minute(frame, ts_col)
            if latest is not None and (freshest is None or latest > freshest):
                freshest = latest
        if freshest is None:
            lags[layer] = None
        else:
            lags[layer] = (now_utc - freshest).total_seconds() / 60.0
    return lags


def recommended_settle_lag(measured: dict[str, float | None]) -> dict[str, float]:
    """Per-layer SETTLE_LAG (minutes): the measured lag where available, else the conservative fallback.

    A measured lag is rounded UP to a safe whole minute + 1 minute of margin (never compare a minute that
    only just settled). The certifier reads this map and, for a multi-layer group, takes the MAX over the
    layers it needs (gate-read #1: cover the worst-case layer)."""
    out: dict[str, float] = {}
    for layer, fallback in FALLBACK_LAG_MINUTES.items():
        value = measured.get(layer)
        if value is None or value < 0:
            out[layer] = float(fallback)
        else:
            out[layer] = float(int(value) + 1)
    return out


def report(symbols: list[str]) -> dict[str, float]:
    """Measure + log the per-layer settle-lag and the recommended SETTLE_LAG map. Returns the map."""
    now_utc = dt.datetime.now(dt.timezone.utc)
    day = now_utc.astimezone(dt.timezone.utc).date()
    if not in_session(now_utc):
        logger.info(
            "OFF-SESSION (now=%s UTC, outside 09:30-16:00 ET): no fresh tail to settle. "
            "Using conservative fallback SETTLE_LAG=%s min.",
            now_utc.isoformat(timespec="minutes"),
            FALLBACK_LAG_MINUTES,
        )
        return {layer: float(value) for layer, value in FALLBACK_LAG_MINUTES.items()}

    client = data_client()
    measured = probe_layer_lag(client, symbols, day, now_utc)
    recommended = recommended_settle_lag(measured)
    for layer in ("bars", "trades", "quotes"):
        value = measured.get(layer)
        logger.info(
            "layer=%s measured_lag=%s min -> recommended SETTLE_LAG=%.0f min",
            layer,
            "n/a" if value is None else f"{value:.1f}",
            recommended[layer],
        )
    logger.info("recommended SETTLE_LAG map (minutes): %s", recommended)
    return recommended


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--symbols",
        default=",".join(DEFAULT_PROBE_SYMBOLS),
        help="comma-list of liquid probe symbols (default SPY,QQQ,AAPL)",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    report(symbols)


if __name__ == "__main__":
    main()
