"""Build a historical option-IV panel by RECONSTRUCTING implied vol from Alpaca's historical option bars.

Why reconstruction (and not a download): Alpaca's option chain / snapshot endpoints (the only ones that
carry implied_volatility + greeks) are CURRENT-SNAPSHOT-ONLY — they take no start/end/as-of, so there is
no historical IV time series to fetch. What Alpaca DOES serve historically (since ~2024-02) is option
BARS / TRADES on a specific OCC contract. So per (underlying, observation-date, contract) we:
  1. discover the contracts that existed (Trading API get_option_contracts, status=active|inactive so
     expired contracts are still enumerable),
  2. fetch the daily option bar (close = the end-of-session mark) over the window,
  3. pull the underlying daily spot for the same session,
  4. invert Black-Scholes to recover implied_vol + first-order greeks,
and write the (underlying, date) partition via option_iv_store (manifest-driven, no double-acquire).

This is the BOUNDED PILOT path: daily granularity, a small liquid-underlying set, a short window. It
proves the end-to-end path (discover -> fetch bars -> spot -> invert -> store, point-in-time correct)
before any full backfill. Memory-light (daily bars, one underlying at a time); network-bound.
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import math
import os
import re
import time
from dataclasses import dataclass

import polars as pl
from alpaca.common.exceptions import APIError
from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.requests import OptionBarsRequest, StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import AssetStatus, ContractType
from alpaca.trading.requests import GetOptionContractsRequest

from quantlib.data import option_iv_store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("option_iv_backfill")

MAX_RETRIES = 5
BACKOFF_BASE_SECONDS = 1.0
BACKOFF_CAP_SECONDS = 30.0

DEFAULT_RATE = 0.045  # flat risk-free assumption for the pilot (refine to a term curve in production)
SESSION_CLOSE_UTC = dt.time(21, 0)  # 16:00 ET end-of-session mark for a daily option bar
SQRT_2PI = math.sqrt(2.0 * math.pi)

_OCC = re.compile(r"^(?P<root>[A-Z]+)(?P<exp>\d{6})(?P<cp>[CP])(?P<strike>\d{8})$")

PILOT_UNDERLYINGS = ["SPY", "QQQ", "AAPL", "NVDA", "TSLA"]


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / SQRT_2PI


def _bs_price(spot: float, strike: float, time_yr: float, rate: float, sigma: float, is_call: bool) -> float:
    if sigma <= 0 or time_yr <= 0:
        intrinsic = max(spot - strike, 0.0) if is_call else max(strike - spot, 0.0)
        return intrinsic
    d1 = (math.log(spot / strike) + (rate + 0.5 * sigma * sigma) * time_yr) / (sigma * math.sqrt(time_yr))
    d2 = d1 - sigma * math.sqrt(time_yr)
    disc = math.exp(-rate * time_yr)
    if is_call:
        return spot * _norm_cdf(d1) - strike * disc * _norm_cdf(d2)
    return strike * disc * _norm_cdf(-d2) - spot * _norm_cdf(-d1)


def _implied_vol(
    price: float, spot: float, strike: float, time_yr: float, rate: float, is_call: bool
) -> float | None:
    """Bisection on BS price = market price. None if the price is below the model floor (no-arb).

    The floor is the BS price at the lower vol bound, NOT undiscounted intrinsic — a European ITM put can
    legitimately price below ``K - S`` (its true floor is the discounted ``K*exp(-rT) - S``)."""
    if time_yr <= 0:
        return None
    lo, hi = 1e-4, 5.0
    f_lo = _bs_price(spot, strike, time_yr, rate, lo, is_call) - price
    f_hi = _bs_price(spot, strike, time_yr, rate, hi, is_call) - price
    if f_lo > 1e-6:  # market price below the model's near-zero-vol floor -> no solution (no-arb)
        return None
    if f_lo * f_hi > 0:  # price above the high-vol cap -> off our [1e-4, 5.0] grid
        return None
    for _ in range(100):
        mid = 0.5 * (lo + hi)
        f_mid = _bs_price(spot, strike, time_yr, rate, mid, is_call) - price
        if abs(f_mid) < 1e-8:
            return mid
        if f_lo * f_mid < 0:
            hi = mid
        else:
            lo, f_lo = mid, f_mid
    return 0.5 * (lo + hi)


def _greeks(
    spot: float, strike: float, time_yr: float, rate: float, sigma: float, is_call: bool
) -> dict[str, float]:
    """First-order BS greeks (per 1.0 vol, per 1 day theta)."""
    if sigma <= 0 or time_yr <= 0:
        return {"delta": float("nan"), "gamma": float("nan"), "vega": float("nan"), "theta": float("nan")}
    sqrt_t = math.sqrt(time_yr)
    d1 = (math.log(spot / strike) + (rate + 0.5 * sigma * sigma) * time_yr) / (sigma * sqrt_t)
    d2 = d1 - sigma * sqrt_t
    pdf_d1 = _norm_pdf(d1)
    disc = math.exp(-rate * time_yr)
    delta = _norm_cdf(d1) if is_call else _norm_cdf(d1) - 1.0
    gamma = pdf_d1 / (spot * sigma * sqrt_t)
    vega = spot * pdf_d1 * sqrt_t / 100.0  # per 1 vol-point
    if is_call:
        theta = (-spot * pdf_d1 * sigma / (2 * sqrt_t) - rate * strike * disc * _norm_cdf(d2)) / 365.0
    else:
        theta = (-spot * pdf_d1 * sigma / (2 * sqrt_t) + rate * strike * disc * _norm_cdf(-d2)) / 365.0
    return {"delta": delta, "gamma": gamma, "vega": vega, "theta": theta}


def _retry(fn, *args, **kwargs):  # type: ignore[no-untyped-def]
    delay = BACKOFF_BASE_SECONDS
    for attempt in range(MAX_RETRIES):
        try:
            return fn(*args, **kwargs)
        except APIError as exc:
            if attempt == MAX_RETRIES - 1:
                raise
            logger.warning(
                "APIError (attempt %d/%d): %s — backing off %.1fs",
                attempt + 1,
                MAX_RETRIES,
                str(exc)[:120],
                delay,
            )
            time.sleep(delay)
            delay = min(delay * 2, BACKOFF_CAP_SECONDS)
    raise RuntimeError("unreachable")


@dataclass(frozen=True)
class Contract:
    occ: str
    expiration: dt.date
    strike: float
    right: str  # "C" / "P"


def discover_contracts(
    trading: TradingClient,
    underlying: str,
    exp_gte: dt.date,
    exp_lte: dt.date,
    strike_gte: float,
    strike_lte: float,
) -> list[Contract]:
    """Enumerate option contracts (active AND inactive, so expired ones are included) for an underlying."""
    seen: dict[str, Contract] = {}
    for status in (AssetStatus.ACTIVE, AssetStatus.INACTIVE):
        page_token = None
        while True:
            req = GetOptionContractsRequest(
                underlying_symbols=[underlying],
                status=status,
                expiration_date_gte=exp_gte,
                expiration_date_lte=exp_lte,
                strike_price_gte=str(strike_gte),
                strike_price_lte=str(strike_lte),
                limit=1000,
                page_token=page_token,
            )
            res = _retry(trading.get_option_contracts, req)
            for contract in res.option_contracts:
                expiration = contract.expiration_date
                if isinstance(expiration, str):
                    expiration = dt.date.fromisoformat(expiration)
                seen[contract.symbol] = Contract(
                    occ=contract.symbol,
                    expiration=expiration,
                    strike=float(contract.strike_price),
                    right="C" if contract.type == ContractType.CALL else "P",
                )
            page_token = getattr(res, "next_page_token", None)
            if not page_token:
                break
    # dedupe across the active/inactive passes (a contract can flip status across the run)
    return list(seen.values())


def fetch_daily_spot(stock: StockHistoricalDataClient, underlying: str, day: dt.date) -> float | None:
    req = StockBarsRequest(
        symbol_or_symbols=underlying,
        timeframe=TimeFrame(1, TimeFrameUnit.Day),
        start=dt.datetime(day.year, day.month, day.day),
        end=dt.datetime(day.year, day.month, day.day) + dt.timedelta(days=1),
    )
    bars = _retry(stock.get_stock_bars, req).df
    if bars is None or bars.empty:
        return None
    return float(bars["close"].iloc[-1])


def fetch_option_daily_closes(
    option: OptionHistoricalDataClient, occ_symbols: list[str], start: dt.date, end: dt.date
) -> pl.DataFrame:
    """Daily option bars for a batch of OCC symbols over [start, end]. Returns (occ, date, close, volume)."""
    if not occ_symbols:
        return pl.DataFrame(
            schema={"occ": pl.String, "date": pl.String, "close": pl.Float64, "volume": pl.Int64}
        )
    req = OptionBarsRequest(
        symbol_or_symbols=occ_symbols,
        timeframe=TimeFrame(1, TimeFrameUnit.Day),
        start=dt.datetime(start.year, start.month, start.day),
        end=dt.datetime(end.year, end.month, end.day) + dt.timedelta(days=1),
    )
    bars = _retry(option.get_option_bars, req).df
    if bars is None or bars.empty:
        return pl.DataFrame(
            schema={"occ": pl.String, "date": pl.String, "close": pl.Float64, "volume": pl.Int64}
        )
    rows = []
    for (occ, ts), row in bars.iterrows():
        rows.append(
            {
                "occ": occ,
                "date": ts.date().isoformat(),
                "close": float(row["close"]),
                "volume": int(row["volume"]),
            }
        )
    return pl.DataFrame(rows)


def build_partition(
    underlying: str,
    day: dt.date,
    contracts: list[Contract],
    option_closes: pl.DataFrame,
    spot: float,
    rate: float,
) -> pl.DataFrame:
    """Invert BS for every contract that traded on `day`, producing one option-IV partition frame."""
    closes_for_day = {
        row["occ"]: (row["close"], row["volume"])
        for row in option_closes.filter(pl.col("date") == day.isoformat()).iter_rows(named=True)
    }
    available_at = dt.datetime.combine(day, option_iv_store_session_close(), tzinfo=dt.timezone.utc)
    out_rows: list[dict[str, object]] = []
    for contract in contracts:
        occ = contract.occ
        if occ not in closes_for_day:
            continue
        close_px, volume = closes_for_day[occ]
        expiration = contract.expiration
        dte = (expiration - day).days
        if dte <= 0:
            continue
        strike = contract.strike
        is_call = contract.right == "C"
        time_yr = dte / 365.0
        iv = _implied_vol(close_px, spot, strike, time_yr, rate, is_call)
        if iv is None:
            status, iv_val, greeks = (
                "no_solution",
                float("nan"),
                {"delta": float("nan"), "gamma": float("nan"), "vega": float("nan"), "theta": float("nan")},
            )
        else:
            status, iv_val, greeks = "ok", iv, _greeks(spot, strike, time_yr, rate, iv, is_call)
        out_rows.append(
            {
                "underlying": underlying,
                "date": day.isoformat(),
                "occ": occ,
                "expiration": expiration.isoformat(),
                "right": "C" if is_call else "P",
                "strike": strike,
                "dte": dte,
                "moneyness": strike / spot,
                "spot": spot,
                "option_close": close_px,
                "option_volume": volume,
                "rate": rate,
                "implied_vol": iv_val,
                "delta": greeks["delta"],
                "gamma": greeks["gamma"],
                "vega": greeks["vega"],
                "theta": greeks["theta"],
                "iv_status": status,
                "available_at": available_at,
            }
        )
    return (
        pl.DataFrame(out_rows, schema=option_iv_store.OPTION_IV_SCHEMA)
        if out_rows
        else pl.DataFrame(schema=option_iv_store.OPTION_IV_SCHEMA)
    )


def option_iv_store_session_close() -> dt.time:
    return SESSION_CLOSE_UTC


def trading_days(start: dt.date, end: dt.date) -> list[dt.date]:
    """Weekday calendar over [start, end] — the option-bar fetch self-filters non-trading days (no bar)."""
    days = []
    cursor = start
    while cursor <= end:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor += dt.timedelta(days=1)
    return days


def run(
    underlyings: list[str],
    start: dt.date,
    end: dt.date,
    store: str,
    rate: float,
    moneyness_band: float,
    max_dte: int,
) -> None:
    key = os.environ["ALPACA_KEY_ID"]
    secret = os.environ["ALPACA_SECRET_KEY"]
    option = OptionHistoricalDataClient(api_key=key, secret_key=secret)
    stock = StockHistoricalDataClient(api_key=key, secret_key=secret)
    trading = TradingClient(api_key=key, secret_key=secret, paper=True)

    done = option_iv_store.done_keys(store)
    days = trading_days(start, end)

    for underlying in underlyings:
        # one spot per day, used to bound the strike band for contract discovery
        spots: dict[dt.date, float] = {}
        for day in days:
            spot = fetch_daily_spot(stock, underlying, day)
            if spot is not None:
                spots[day] = spot
        if not spots:
            logger.warning("%s: no spot in window — skipping", underlying)
            continue
        mid_spot = sorted(spots.values())[len(spots) // 2]
        contracts = discover_contracts(
            trading,
            underlying,
            exp_gte=start,
            exp_lte=end + dt.timedelta(days=max_dte),
            strike_gte=mid_spot * (1 - moneyness_band),
            strike_lte=mid_spot * (1 + moneyness_band),
        )
        logger.info(
            "%s: %d contracts in band (spot~%.1f, +-%.0f%%, dte<=%d)",
            underlying,
            len(contracts),
            mid_spot,
            moneyness_band * 100,
            max_dte,
        )
        if not contracts:
            continue
        occ_symbols = [c.occ for c in contracts]
        # batch the option-bar fetch (alpaca handles many symbols per request, paginates internally)
        option_closes = (
            pl.concat(
                [
                    fetch_option_daily_closes(option, occ_symbols[i : i + 200], start, end)
                    for i in range(0, len(occ_symbols), 200)
                ]
            )
            if occ_symbols
            else pl.DataFrame()
        )

        for day in days:
            if (underlying, day.isoformat()) in done:
                continue
            if day not in spots:
                continue
            frame = build_partition(underlying, day, contracts, option_closes, spots[day], rate)
            # keep only contracts within max_dte of the observation date
            frame = frame.filter(pl.col("dte") <= max_dte)
            nbytes = option_iv_store.write_partition(store, underlying, day, frame)
            n_ok = int(frame.filter(pl.col("iv_status") == "ok").height)
            logger.info(
                "%s %s: %d rows (%d iv-ok), %d bytes",
                underlying,
                day.isoformat(),
                frame.height,
                n_ok,
                nbytes,
            )


def main() -> None:
    parser = argparse.ArgumentParser(description="Bounded option-IV panel pilot backfill.")
    parser.add_argument("--underlyings", nargs="+", default=PILOT_UNDERLYINGS)
    parser.add_argument("--start", required=True, type=lambda s: dt.date.fromisoformat(s))
    parser.add_argument("--end", required=True, type=lambda s: dt.date.fromisoformat(s))
    parser.add_argument("--store", default=os.environ.get("STORE_ROOT", "/store"))
    parser.add_argument("--rate", type=float, default=DEFAULT_RATE)
    parser.add_argument(
        "--moneyness-band", type=float, default=0.15, help="strike band as +-fraction of spot"
    )
    parser.add_argument("--max-dte", type=int, default=60, help="max calendar days to expiry to keep")
    args = parser.parse_args()
    run(args.underlyings, args.start, args.end, args.store, args.rate, args.moneyness_band, args.max_dte)


if __name__ == "__main__":
    main()
