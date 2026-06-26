"""Real-snapshot-shaped fixture for CriticalProfiler's #76 canary (the session/index groups can't be diffed
without snapshots — market_beta/market_context go all-NaN with no spy_row, and the daily/news/edgar groups
emit their NaN defaults).

Produces the FIVE held snapshots exactly as the loaders shape them (so build_session + the OLD-path groups both
self-select), plus a streamed-bars sequence over the same universe:
  reference : (symbol, sector, shortable, easy_to_borrow, marginable, fractionable, cluster_id) — load_reference
  universe  : (symbol,)                                                                          — load_universe
  daily     : (symbol, date, open, high, low, close, volume, vwap)                               — backfill_daily
  news      : (symbol, available_at, sentiment)                                                  — load_news_features
  filings   : (symbol, form_type, available_at)                                                  — load_filings

SPY/QQQ/IWM are in the universe + reference + daily so the market groups get a real spy_row. A late-listing
symbol (LATE) streams from minute 5 onward but IS in the universe (the production-representative coverage case).

Use:
  from canary_snapshot_fixture import make_snapshots, make_minute_bars, SYMBOLS, DAY
  snapshots = make_snapshots()
  for minute_index in range(N): process_bars(state, make_minute_bars(minute_index), ..., snapshots=snapshots)
"""
from __future__ import annotations

import datetime
from datetime import date, timezone

import numpy as np
import polars as pl

UTC = timezone.utc
DAY = date(2026, 6, 18)
_SECTORS = ["Technology", "Energy", "Financial Services", "Healthcare"]
# index ETFs (market-context inputs) + ordinary names + one late-listing name.
INDEX_SYMBOLS = ["SPY", "QQQ", "IWM"]
ORDINARY = ["AAA", "BBB", "CCC", "DDD", "EEE"]
LATE = ["LATE"]  # in-universe but first streams at minute 5 (production-representative late-bar case)
SYMBOLS = INDEX_SYMBOLS + ORDINARY + LATE
_N_DAILY_DAYS = 130  # > the deepest daily window (120d vwap / 60d beta / 250d high) so they're warm
_rng = np.random.default_rng(42)


def _daily_frame() -> pl.DataFrame:
    dates = [DAY - datetime.timedelta(days=offset) for offset in range(_N_DAILY_DAYS - 1, -1, -1)]
    rows = []
    for symbol in SYMBOLS:
        price = 400.0 if symbol in INDEX_SYMBOLS else 100.0
        for trade_day in dates:
            op = price * (1 + _rng.normal(0, 0.01))
            hi = max(op, price) * (1 + abs(_rng.normal(0, 0.008)))
            lo = min(op, price) * (1 - abs(_rng.normal(0, 0.008)))
            cl = price * (1 + _rng.normal(0, 0.012))
            rows.append(
                {"symbol": symbol, "date": trade_day, "open": op, "high": hi, "low": lo, "close": cl,
                 "volume": float(abs(_rng.normal(2e6, 4e5))), "vwap": (hi + lo + cl) / 3.0}
            )
            price = cl
    return pl.DataFrame(rows).with_columns(pl.col("date").cast(pl.Date))


def _reference_frame() -> pl.DataFrame:
    rows = []
    for position, symbol in enumerate(SYMBOLS):
        rows.append({
            "symbol": symbol,
            "sector": "Financial Services" if symbol in INDEX_SYMBOLS else _SECTORS[position % len(_SECTORS)],
            "shortable": True, "easy_to_borrow": position % 2 == 0,
            "marginable": True, "fractionable": position % 3 != 0, "cluster_id": position % 4,
        })
    return pl.DataFrame(rows).with_columns(pl.col("cluster_id").cast(pl.Int32))


def _news_frame(base: datetime.datetime) -> pl.DataFrame:
    rows = []
    for symbol in ORDINARY:
        for _ in range(int(_rng.integers(2, 12))):
            back_days = float(_rng.uniform(0, 8))
            available = base - datetime.timedelta(days=back_days) + datetime.timedelta(seconds=int(_rng.integers(0, 86400)))
            rows.append({"symbol": symbol, "available_at": available, "sentiment": float(_rng.uniform(-1, 1))})
    return pl.DataFrame(rows).with_columns(pl.col("available_at").cast(pl.Datetime("us", "UTC")))


def _filings_frame(base: datetime.datetime) -> pl.DataFrame:
    forms = ["8-K", "10-Q", "10-K", "4"]
    rows = []
    for symbol in ORDINARY:
        for _ in range(int(_rng.integers(2, 20))):
            back_days = float(_rng.uniform(0, 360))
            available = base - datetime.timedelta(days=back_days) + datetime.timedelta(seconds=int(_rng.integers(0, 86400)))
            rows.append({"symbol": symbol, "form_type": forms[int(_rng.integers(0, 4))], "available_at": available})
    return pl.DataFrame(rows).with_columns(pl.col("available_at").cast(pl.Datetime("us", "UTC")))


def _session_open() -> datetime.datetime:
    return datetime.datetime(DAY.year, DAY.month, DAY.day, 14, 30, tzinfo=UTC)


def make_snapshots() -> dict[str, pl.DataFrame]:
    """The five held snapshots, loader-shaped — pass as ``snapshots`` to process_bars / build_session."""
    base = _session_open()
    return {
        "reference": _reference_frame(),
        "universe": pl.DataFrame({"symbol": SYMBOLS}),
        "daily": _daily_frame(),
        "news": _news_frame(base),
        "filings": _filings_frame(base),
    }


def make_minute_bars(minute_index: int) -> list[dict]:
    """One minute's streamed bars (the normalized S/o/c/h/l/v/t dicts process_bars takes). LATE first appears at
    minute 5 — the production-representative late-bar case (in-universe, late first print)."""
    base = _session_open()
    when = base + datetime.timedelta(minutes=minute_index)
    present = INDEX_SYMBOLS + ORDINARY + (LATE if minute_index >= 5 else [])
    bars = []
    for symbol in present:
        price = 400.0 if symbol in INDEX_SYMBOLS else 100.0
        drift = 1 + 0.0005 * (minute_index - 5) + _rng.normal(0, 0.001)
        close = price * drift
        bars.append({"S": symbol, "o": price, "c": close, "h": max(price, close) * 1.001,
                     "l": min(price, close) * 0.999, "v": float(abs(_rng.normal(5e4, 1e4))),
                     "t": when.isoformat()})
    return bars


if __name__ == "__main__":
    # smoke: build the session/static from the fixture, confirm the market groups get a real spy_row + non-NaN.
    from quantlib.features.clean_session import build_session  # noqa: PLC0415

    snaps = make_snapshots()
    session, static = build_session(snaps, SYMBOLS)
    print("session keys:", sorted(session.keys()))
    print("static keys:", sorted(static.keys()))
    print("spy_row:", static["spy_row"], "(SPY at index", SYMBOLS.index("SPY"), ")")
    print("daily_close shape:", session["daily_close"].shape, "(n_sym x n_days)")
    print("news events:", len(session["news_at"]), "| edgar events:", len(session["edgar_at"]))
    assert static["spy_row"][0] == SYMBOLS.index("SPY")
    print("FIXTURE OK — spy_row resolved, session populated")
