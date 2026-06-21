"""Build the $0 SURVIVORSHIP-CLEAN daily-bar panel from Alpaca (INACTIVE u ACTIVE exchange-listed US equities).

The fix for the self-inflicted bias: include INACTIVE (delisted) names, which our normal universe screen
(raw_backfill.py:198 status=ACTIVE) excludes. For each name we pull DAILY split-adjusted bars over the span;
a name is in the cross-section only while it was trading (point-in-time membership). The last bar date IS the
de-facto delisting date. NEVER prints creds (env-name access only).

Writes debiased_daily.parquet: (day, symbol, close, open, dvol, is_delist_last) — one row per (name, day),
plus a per-symbol meta (last_bar_date, last_close, last_20d_ret, is_distress) for the two-bound band.
"""
from __future__ import annotations

import datetime as dt
import os
import re
import time

import polars as pl
from alpaca.data.enums import Adjustment, DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import AssetClass, AssetStatus
from alpaca.trading.requests import GetAssetsRequest

OUT_DIR = "/app/experiments/2026-06-20-delisting-data-proposal"
SPAN_START = os.environ.get("SPAN_START", "2018-01-01")
SPAN_END = os.environ.get("SPAN_END", "2025-12-31")
EXCH_LISTED = {"NYSE", "NASDAQ", "ARCA", "AMEX", "BATS"}
CHUNK = int(os.environ.get("CHUNK", "100"))  # symbols per multi-symbol daily-bar request
ETF_HINT = ("ETF", "FUND", "TRUST", "ISHARES", "SPDR", "INVESCO", "PROSHARES", "INDEX", "ETN")


def exch(asset) -> str:  # noqa: ANN001
    e = getattr(asset, "exchange", None)
    return getattr(e, "value", str(e))


def is_etf_like(name: str) -> bool:
    upper = (name or "").upper()
    return any(h in upper for h in ETF_HINT)


def universe() -> list[str]:
    client = TradingClient(os.environ["ALPACA_KEY_ID"], os.environ["ALPACA_SECRET_KEY"], paper=True)
    syms = set()
    for status in (AssetStatus.ACTIVE, AssetStatus.INACTIVE):
        assets = client.get_all_assets(GetAssetsRequest(status=status, asset_class=AssetClass.US_EQUITY))
        for a in assets:
            # clean A-Z tickers only — exclude CUSIP/CVR/escrow delisting-residue artifacts (e.g. 003CVR016)
            # that are not real tradeable equities (1,087 of ~9k) and that error the bar API.
            if (
                exch(a) in EXCH_LISTED
                and re.fullmatch(r"[A-Z]{1,5}", a.symbol)
                and not is_etf_like(a.name)
            ):
                syms.add(a.symbol)
    return sorted(syms)


def fetch_daily(client: StockHistoricalDataClient, symbols: list[str], start: dt.date, end: dt.date) -> pl.DataFrame:
    req = StockBarsRequest(
        symbol_or_symbols=symbols,
        timeframe=TimeFrame.Day,
        start=dt.datetime.combine(start, dt.time()),
        end=dt.datetime.combine(end, dt.time()),
        adjustment=Adjustment.SPLIT,  # split-adjusted (no dividend distortion for a price-reversal signal)
        feed=DataFeed.SIP,
    )
    barset = client.get_stock_bars(req)
    rows = []
    for sym in symbols:
        for bar in barset.data.get(sym, []):
            rows.append((sym, bar.timestamp.date().isoformat(), float(bar.open), float(bar.close), float(bar.volume)))
    return pl.DataFrame(rows, schema=["symbol", "day", "open", "close", "volume"], orient="row")


def main() -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    syms = universe()
    print(f"exchange-listed universe (ACTIVE u INACTIVE): {len(syms)} symbols", flush=True)
    client = StockHistoricalDataClient(os.environ["ALPACA_KEY_ID"], os.environ["ALPACA_SECRET_KEY"])
    start, end = dt.date.fromisoformat(SPAN_START), dt.date.fromisoformat(SPAN_END)
    frames = []
    for i in range(0, len(syms), CHUNK):
        chunk = syms[i : i + CHUNK]
        try:
            df = fetch_daily(client, chunk, start, end)
            if df.height:
                frames.append(df)
        except Exception as ex:  # noqa: BLE001 - one bad symbol fails the whole multi-symbol chunk;
            # fall back to per-symbol so we keep the good names in this chunk.
            print(f"  chunk {i}: multi-fetch ERROR {type(ex).__name__} {str(ex)[:60]} -> per-symbol fallback", flush=True)
            for sym in chunk:
                try:
                    one = fetch_daily(client, [sym], start, end)
                    if one.height:
                        frames.append(one)
                except Exception:  # noqa: BLE001 - skip the genuinely bad symbol
                    pass
                time.sleep(0.05)
        if (i // CHUNK + 1) % 5 == 0:
            print(f"  fetched {i+len(chunk)}/{len(syms)} symbols ({sum(f.height for f in frames)} rows)", flush=True)
        time.sleep(0.15)
    daily = pl.concat(frames, how="vertical").filter(pl.col("close") >= 1.0)  # $1 floor
    daily = daily.with_columns((pl.col("close") * pl.col("volume")).alias("dvol")).sort(["symbol", "day"])
    daily.write_parquet(f"{OUT_DIR}/debiased_daily.parquet")

    # per-symbol delisting meta (last bar = de-facto delisting; distress classification for the band)
    meta = (
        daily.group_by("symbol")
        .agg(
            pl.col("day").max().alias("last_bar_date"),
            pl.col("day").min().alias("first_bar_date"),
            pl.col("close").last().alias("last_close"),
            pl.len().alias("n_days"),
        )
    )
    # last_20d_ret: close[last]/close[last-20] - 1 (distress = collapsing into delisting)
    tail = daily.group_by("symbol").tail(21).sort(["symbol", "day"])
    ret20 = (
        tail.group_by("symbol")
        .agg((pl.col("close").last() / pl.col("close").first() - 1.0).alias("last_20d_ret"))
    )
    meta = meta.join(ret20, on="symbol", how="left")
    # DISTRESS (bankruptcy-band) classifier: delisted (last bar well before SPAN_END) AND
    # (collapsing last-20d return < -50% OR a penny last close < $5). Acquisitions delist at a stable/high price.
    delist_cut = (dt.date.fromisoformat(SPAN_END) - dt.timedelta(days=10)).isoformat()
    meta = meta.with_columns(
        ((pl.col("last_bar_date") < delist_cut) & ((pl.col("last_20d_ret") < -0.50) | (pl.col("last_close") < 5.0)))
        .alias("is_distress"),
        (pl.col("last_bar_date") < delist_cut).alias("is_delisted"),
    )
    meta.write_parquet(f"{OUT_DIR}/debiased_meta.parquet")
    n_delisted = int(meta["is_delisted"].sum())
    n_distress = int(meta["is_distress"].sum())
    print(
        f"WROTE debiased_daily ({daily.height} rows, {daily['symbol'].n_unique()} syms, "
        f"{daily['day'].min()}..{daily['day'].max()}) + meta: delisted={n_delisted} (distress={n_distress})",
        flush=True,
    )


if __name__ == "__main__":
    main()
