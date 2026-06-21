"""Unit tests for the Stage-1 realized half-spread measured from the quote tape.

Hermetic: each test writes a tiny synthetic quote partition into a temp store laid out like the real one
(``<store>/raw/quotes/symbol=<S>/date=<D>/data.parquet``), then asserts the measured half-spread, the
time-weighting, the strict ``ts < T`` no-look-ahead cut, and the valid-NBBO / too-few-quotes handling.
"""

from __future__ import annotations

import datetime as dt
import os

import polars as pl
import pytest

from quantlib.data.realized_cost import MIN_QUOTES, realized_half_spread_bps, realized_half_spread_bps_multi

DAY = "2026-06-10"
ENTRY = dt.datetime(2026, 6, 10, 13, 40, tzinfo=dt.timezone.utc)


def _write_quotes(store: str, symbol: str, rows: list[dict[str, object]]) -> None:
    path = os.path.join(store, "raw", "quotes", f"symbol={symbol}", f"date={DAY}")
    os.makedirs(path, exist_ok=True)
    pl.DataFrame(rows).write_parquet(os.path.join(path, "data.parquet"))


def _quote(seconds_before_entry: float, bid: float, ask: float, size: float = 100.0) -> dict[str, object]:
    return {
        "ts": ENTRY - dt.timedelta(seconds=seconds_before_entry),
        "bid_price": bid,
        "bid_size": size,
        "ask_price": ask,
        "ask_size": size,
    }


def test_measures_constant_half_spread(tmp_path: str) -> None:
    store = str(tmp_path)
    # constant 10 bps full spread on a $100 mid -> 5 bps half-spread, many quotes in the window
    rows = [_quote(s, 99.95, 100.05) for s in range(1, 120)]
    _write_quotes(store, "AAA", rows)
    out = realized_half_spread_bps(store, DAY, ["AAA"], ENTRY)
    assert out.height == 1
    assert out["realized_half_spread_bps"][0] == pytest.approx(5.0, abs=0.1)


def test_time_weighting_favors_long_dwell_quote(tmp_path: str) -> None:
    store = str(tmp_path)
    # a wide quote that STOOD for ~50s then a flicker of tight quotes in the last second: the
    # time-weighted half-spread must be near the WIDE value, not the count-mean.
    rows = [_quote(55.0, 99.0, 101.0)]  # 200 bps full / 100 bps half, dwells ~54s
    rows += [_quote(1.0 - i * 0.05, 99.99, 100.01) for i in range(10)]  # tight flickers in the last 1s
    _write_quotes(store, "BBB", rows)
    out = realized_half_spread_bps(store, DAY, ["BBB"], ENTRY)
    half = out["realized_half_spread_bps"][0]
    assert half > 80.0  # dominated by the long-dwell wide quote, not the tight flickers


def test_strict_no_lookahead_excludes_quotes_at_or_after_entry(tmp_path: str) -> None:
    store = str(tmp_path)
    # pre-entry quotes are WIDE; a post-entry quote is TIGHT. The measure must read ONLY pre-entry.
    rows = [_quote(s, 99.0, 101.0) for s in range(1, 60)]
    rows += [{"ts": ENTRY, "bid_price": 99.999, "bid_size": 100.0, "ask_price": 100.001, "ask_size": 100.0}]
    rows += [
        {
            "ts": ENTRY + dt.timedelta(seconds=5),
            "bid_price": 99.999,
            "bid_size": 100.0,
            "ask_price": 100.001,
            "ask_size": 100.0,
        }
    ]
    _write_quotes(store, "CCC", rows)
    out = realized_half_spread_bps(store, DAY, ["CCC"], ENTRY)
    assert out["realized_half_spread_bps"][0] > 80.0  # the tight post-entry quotes did not leak in


def test_invalid_nbbo_and_too_few_quotes_are_dropped(tmp_path: str) -> None:
    store = str(tmp_path)
    # crossed/zero quotes are invalid; with fewer than MIN_QUOTES valid ones the name is omitted.
    rows = [
        {
            "ts": ENTRY - dt.timedelta(seconds=10),
            "bid_price": 101.0,
            "bid_size": 100.0,
            "ask_price": 100.0,
            "ask_size": 100.0,
        },  # crossed (bid>ask) -> invalid
        {
            "ts": ENTRY - dt.timedelta(seconds=8),
            "bid_price": 0.0,
            "bid_size": 100.0,
            "ask_price": 100.0,
            "ask_size": 100.0,
        },  # zero bid -> invalid
        _quote(6.0, 99.95, 100.05),  # one valid
    ]
    _write_quotes(store, "DDD", rows)
    out = realized_half_spread_bps(store, DAY, ["DDD"], ENTRY)
    assert out.height == 0  # < MIN_QUOTES valid -> omitted


def test_missing_symbol_is_omitted(tmp_path: str) -> None:
    store = str(tmp_path)
    _write_quotes(store, "EEE", [_quote(s, 99.95, 100.05) for s in range(1, 30)])
    out = realized_half_spread_bps(store, DAY, ["EEE", "ZZZ_NO_PARTITION"], ENTRY)
    assert set(out["symbol"].to_list()) == {"EEE"}


def test_min_quotes_constant_is_enforced() -> None:
    assert MIN_QUOTES >= 2  # time-weighting needs at least a couple of quotes to be meaningful


_MULTI_INSTANTS = [
    dt.datetime(2026, 6, 10, 13, 40, tzinfo=dt.timezone.utc),
    dt.datetime(2026, 6, 10, 14, 10, tzinfo=dt.timezone.utc),
    dt.datetime(2026, 6, 10, 14, 40, tzinfo=dt.timezone.utc),
]


def test_multi_equals_per_instant_loop(tmp_path: str) -> None:
    """``realized_half_spread_bps_multi`` is row-for-row identical to calling the single-instant
    function once per instant — same value, same (symbol, instant) inclusion — but reads each
    symbol's partition ONCE. The whole point of the batched fast path: equivalence, not approximation."""
    store = str(tmp_path)
    # AAA: dense quotes around EACH instant (measurable at every instant).
    # BBB: quotes only around the FIRST instant (omitted at the later two -> tests selective inclusion).
    aaa_rows = []
    for inst in _MULTI_INSTANTS:
        aaa_rows += [
            {
                "ts": inst - dt.timedelta(seconds=s),
                "bid_price": 99.95,
                "bid_size": 100.0,
                "ask_price": 100.05,
                "ask_size": 100.0,
            }
            for s in range(1, 90)
        ]
    _write_quotes(store, "AAA", aaa_rows)
    first = _MULTI_INSTANTS[0]
    bbb_rows = [
        {
            "ts": first - dt.timedelta(seconds=s),
            "bid_price": 99.0,
            "bid_size": 100.0,
            "ask_price": 101.0,
            "ask_size": 100.0,
        }
        for s in range(1, 60)
    ]
    _write_quotes(store, "BBB", bbb_rows)

    symbols = ["AAA", "BBB", "ZZZ_MISSING"]
    per_instant = []
    for at_ts in _MULTI_INSTANTS:
        out = realized_half_spread_bps(store, DAY, symbols, at_ts)
        if out.height:
            per_instant.append(out.with_columns(pl.lit(at_ts).alias("minute")))
    expected = pl.concat(per_instant).sort(["symbol", "minute"])
    got = realized_half_spread_bps_multi(store, DAY, symbols, _MULTI_INSTANTS).sort(["symbol", "minute"])

    assert got.height == expected.height
    merged = got.join(expected, on=["symbol", "minute"], suffix="_exp")
    assert merged.height == got.height  # identical key set
    max_diff = (merged["realized_half_spread_bps"] - merged["realized_half_spread_bps_exp"]).abs().max()
    assert max_diff is not None and max_diff < 1e-9
    # BBB present only at the first instant; AAA at all three.
    assert set(got.filter(pl.col("minute") == first)["symbol"].to_list()) == {"AAA", "BBB"}
    assert set(got.filter(pl.col("minute") == _MULTI_INSTANTS[-1])["symbol"].to_list()) == {"AAA"}


def test_multi_reads_each_partition_once(tmp_path: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Lock in the fast path: the batched call opens each symbol's quote file exactly ONCE regardless of
    how many instants are requested (the old loop re-read the same whole-day file per instant)."""
    import quantlib.data.realized_cost as rc

    store = str(tmp_path)
    rows = []
    for inst in _MULTI_INSTANTS:
        rows += [
            {
                "ts": inst - dt.timedelta(seconds=s),
                "bid_price": 99.95,
                "bid_size": 100.0,
                "ask_price": 100.05,
                "ask_size": 100.0,
            }
            for s in range(1, 90)
        ]
    _write_quotes(store, "AAA", rows)

    reads: list[str] = []
    real_read = rc._read_valid_quotes_day

    def _counting_read(store_arg: str, symbol: str, day: dt.date) -> pl.DataFrame | None:
        reads.append(symbol)
        return real_read(store_arg, symbol, day)

    monkeypatch.setattr(rc, "_read_valid_quotes_day", _counting_read)
    rc.realized_half_spread_bps_multi(store, DAY, ["AAA"], _MULTI_INSTANTS)
    assert reads == ["AAA"]  # ONE read for 3 instants, not 3
