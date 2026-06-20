"""Tests for the EDGAR filing-frequency feature group (Ben's #2 alt-data ask).

The group joins the live ``filings`` event store onto the (symbol, minute) grid, gated by
``available_at <= minute``. Because ``available_at`` is fixed at first sight, the same ``load_filings``
frame fed to live and backfill yields identical values — the BACKFILL==LIVE parity test below is the
gate proving that for this NEW input source (a new join source is exactly where parity bugs hide).
"""
from __future__ import annotations

import datetime as dt

import polars as pl

from quantlib.features.base import BatchContext
from quantlib.features.compare import runnable
from quantlib.features.groups.edgar_filing_frequency import EdgarFilingFrequencyGroup

UTC = dt.timezone.utc
SESSION_DAY = dt.datetime(2026, 6, 19, 14, 0, tzinfo=UTC)  # 14:00 UTC ~ market open window


def _minute_grid(symbols: tuple[str, ...], n_min: int, start: dt.datetime = SESSION_DAY) -> pl.DataFrame:
    minutes = pl.DataFrame({"minute": [start + dt.timedelta(minutes=i) for i in range(n_min)]})
    return pl.DataFrame({"symbol": list(symbols)}).join(minutes, how="cross")


def _filings(rows: list[tuple[str, str, dt.datetime]]) -> pl.DataFrame:
    return pl.DataFrame(
        rows,
        schema={"symbol": pl.String, "form_type": pl.String, "available_at": pl.Datetime("us", "UTC")},
        orient="row",
    )


def _compute(keys: pl.DataFrame, filings: pl.DataFrame) -> pl.DataFrame:
    group = EdgarFilingFrequencyGroup()
    ctx = BatchContext(frames={"minute_agg": keys, "filings": filings})
    return group.compute(ctx).sort(["symbol", "minute"])


def test_declared_feature_count_and_names() -> None:
    """The group declares exactly the designed frequency/timing/form features (3 counts + 2 since-last
    + 4 per-form 90d + burst = 10)."""
    group = EdgarFilingFrequencyGroup()
    names = group.feature_names
    assert len(names) == 10
    assert set(names) == {
        "edgar_filing_count_7d", "edgar_filing_count_30d", "edgar_filing_count_90d",
        "edgar_minutes_since_last_filing", "edgar_minutes_since_last_8k",
        "edgar_count_8k_90d", "edgar_count_10q_90d", "edgar_count_10k_90d", "edgar_count_form4_90d",
        "edgar_filing_burst",
    }


def test_count_windows_are_point_in_time() -> None:
    """count_{7,30,90}d count only filings with available_at in (minute - Nd, minute]."""
    keys = _minute_grid(("AAA",), n_min=1)
    minute = keys["minute"][0]
    filings = _filings([
        ("AAA", "8-K", minute - dt.timedelta(days=3)),    # in 7/30/90
        ("AAA", "10-Q", minute - dt.timedelta(days=20)),  # in 30/90
        ("AAA", "10-K", minute - dt.timedelta(days=60)),  # in 90 only
        ("AAA", "4", minute - dt.timedelta(days=200)),    # in none of the count windows
    ])
    row = _compute(keys, filings).row(0, named=True)
    assert row["edgar_filing_count_7d"] == 1.0
    assert row["edgar_filing_count_30d"] == 2.0
    assert row["edgar_filing_count_90d"] == 3.0


def test_minutes_since_last_filing_and_8k() -> None:
    """minutes-since-last is the gap in minutes to the most recent available_at <= minute; the 8-K
    variant restricts to form_type == '8-K' (a later Form-4 must NOT reset the 8-K clock)."""
    keys = _minute_grid(("AAA",), n_min=1)
    minute = keys["minute"][0]
    filings = _filings([
        ("AAA", "8-K", minute - dt.timedelta(minutes=500)),
        ("AAA", "4", minute - dt.timedelta(minutes=10)),  # most recent overall, but not an 8-K
    ])
    row = _compute(keys, filings).row(0, named=True)
    assert row["edgar_minutes_since_last_filing"] == 10.0
    assert row["edgar_minutes_since_last_8k"] == 500.0


def test_per_form_90d_counts() -> None:
    keys = _minute_grid(("AAA",), n_min=1)
    minute = keys["minute"][0]
    filings = _filings([
        ("AAA", "8-K", minute - dt.timedelta(days=1)),
        ("AAA", "8-K", minute - dt.timedelta(days=40)),
        ("AAA", "10-Q", minute - dt.timedelta(days=10)),
        ("AAA", "10-K", minute - dt.timedelta(days=80)),
        ("AAA", "4", minute - dt.timedelta(days=5)),
        ("AAA", "4", minute - dt.timedelta(days=95)),  # outside 90d -> not counted
        ("AAA", "SC 13G", minute - dt.timedelta(days=2)),  # not a major form -> ignored by form counts
    ])
    row = _compute(keys, filings).row(0, named=True)
    assert row["edgar_count_8k_90d"] == 2.0
    assert row["edgar_count_10q_90d"] == 1.0
    assert row["edgar_count_10k_90d"] == 1.0
    assert row["edgar_count_form4_90d"] == 1.0


def test_no_filings_gives_zero_counts_and_null_since_last() -> None:
    """A symbol with NO filings: counts are 0 (not garbage), minutes-since-last is null (the sentinel for
    'no prior filing on record'), burst is null (undefined baseline)."""
    keys = _minute_grid(("EMPTY",), n_min=1)
    filings = _filings([("OTHER", "8-K", SESSION_DAY - dt.timedelta(days=1))])
    row = _compute(keys, filings).row(0, named=True)
    assert row["edgar_filing_count_7d"] == 0.0
    assert row["edgar_filing_count_30d"] == 0.0
    assert row["edgar_filing_count_90d"] == 0.0
    assert row["edgar_count_8k_90d"] == 0.0
    assert row["edgar_minutes_since_last_filing"] is None
    assert row["edgar_minutes_since_last_8k"] is None
    assert row["edgar_filing_burst"] is None


def test_first_filing_and_since_last_8k_null_when_only_other_forms() -> None:
    """A symbol whose only filing is a non-8-K: since_last_filing is set, but since_last_8k stays null."""
    keys = _minute_grid(("AAA",), n_min=1)
    minute = keys["minute"][0]
    filings = _filings([("AAA", "10-Q", minute - dt.timedelta(minutes=30))])
    row = _compute(keys, filings).row(0, named=True)
    assert row["edgar_minutes_since_last_filing"] == 30.0
    assert row["edgar_minutes_since_last_8k"] is None


def test_multiple_same_day_filings_all_counted() -> None:
    keys = _minute_grid(("AAA",), n_min=1)
    minute = keys["minute"][0]
    filings = _filings([
        ("AAA", "8-K", minute - dt.timedelta(hours=2)),
        ("AAA", "8-K", minute - dt.timedelta(hours=1)),
        ("AAA", "4", minute - dt.timedelta(minutes=30)),
    ])
    row = _compute(keys, filings).row(0, named=True)
    assert row["edgar_filing_count_7d"] == 3.0
    assert row["edgar_count_8k_90d"] == 2.0
    assert row["edgar_minutes_since_last_filing"] == 30.0


def test_lookahead_filing_enters_only_from_its_minute() -> None:
    """THE point-in-time guarantee: a filing with available_at=T must NOT appear in the T-1 minute's
    value and MUST appear from the T minute onward (no look-ahead)."""
    keys = _minute_grid(("AAA",), n_min=5)
    minutes = keys["minute"].to_list()
    file_minute = minutes[2]  # the 3rd minute
    filings = _filings([("AAA", "8-K", file_minute)])
    out = _compute(keys, filings)
    counts = out["edgar_filing_count_7d"].to_list()
    assert counts == [0.0, 0.0, 1.0, 1.0, 1.0]  # appears exactly at file_minute, never before
    since_8k = out["edgar_minutes_since_last_8k"].to_list()
    assert since_8k[:2] == [None, None]
    assert since_8k[2:] == [0.0, 1.0, 2.0]


def test_filing_burst_ratio() -> None:
    """burst = count_7d / (count_365d * 7/365). 1 filing in the trailing 7d and (here) 1 in the year =>
    expected 7/365 ~ 0.019, ratio ~ 52 (a clear spike). Null when the year baseline is empty."""
    keys = _minute_grid(("AAA",), n_min=1)
    minute = keys["minute"][0]
    filings = _filings([("AAA", "8-K", minute - dt.timedelta(days=2))])
    row = _compute(keys, filings).row(0, named=True)
    expected = 1.0 / (1.0 * (7.0 / 365.0))
    assert abs(row["edgar_filing_burst"] - expected) < 1e-9


def _wide_filings(symbols: tuple[str, ...], seed_minute: dt.datetime) -> pl.DataFrame:
    """A multi-symbol filing history spanning >1 year, with one filing landing INSIDE the session window
    so the intraday look-ahead gate is exercised in the parity test."""
    forms = ("8-K", "10-Q", "10-K", "4", "SC 13G")
    rows: list[tuple[str, str, dt.datetime]] = []
    for s_idx, symbol in enumerate(symbols):
        for k in range(6):
            rows.append((symbol, forms[k % len(forms)], seed_minute - dt.timedelta(days=30 * k + s_idx)))
        # an intraday same-session 8-K a few minutes into the session
        rows.append((symbol, "8-K", seed_minute + dt.timedelta(minutes=3 + s_idx)))
    return _filings(rows)


def test_backfill_equals_live_parity() -> None:
    """THE GATE for a new join source: the group's BACKFILL form (whole-buffer compute) must equal its
    LIVE form (per-minute compute_latest over a trailing buffer) cell-for-cell at every minute. Both read
    the SAME ``load_filings`` frame (available_at fixed at first sight), so they must agree exactly.
    """
    symbols = ("AAA", "BBB", "CCC", "DDD")
    n_min = 30
    keys = _minute_grid(symbols, n_min=n_min)
    filings = _wide_filings(symbols, SESSION_DAY)
    group = EdgarFilingFrequencyGroup()

    backfill = group.compute(BatchContext(frames={"minute_agg": keys, "filings": filings})).sort(["symbol", "minute"])

    # LIVE: at each minute T, the worker holds a trailing buffer ending at T; compute_latest emits T's row.
    minutes = sorted(keys["minute"].unique().to_list())
    live_rows = []
    for end_idx in range(len(minutes)):
        buf = keys.filter(pl.col("minute") <= minutes[end_idx])
        live_group = EdgarFilingFrequencyGroup()  # fresh per minute: the live worker recomputes each minute
        latest = live_group.compute_latest(BatchContext(frames={"minute_agg": buf, "filings": filings}))
        live_rows.append(latest)
    live = pl.concat(live_rows).sort(["symbol", "minute"])

    assert live.height == backfill.height
    feature_cols = [c for c in backfill.columns if c not in ("symbol", "minute")]
    joined = backfill.join(live, on=["symbol", "minute"], how="inner", suffix="_live")
    assert joined.height == backfill.height
    for col in feature_cols:
        mismatch = joined.filter(
            (pl.col(col).is_null() != pl.col(f"{col}_live").is_null())
            | (
                pl.col(col).is_not_null()
                & pl.col(f"{col}_live").is_not_null()
                & ((pl.col(col) - pl.col(f"{col}_live")).abs() > 1e-9)
            )
        )
        assert mismatch.height == 0, f"backfill!=live for {col} on {mismatch.height} cells"


def test_runnable_only_with_filings_frame() -> None:
    """The group is runnable iff both minute_agg AND filings are present — a crypto/bars-only frame
    (no filings) correctly SKIPS it rather than erroring."""
    keys = _minute_grid(("AAA",), n_min=1)
    names_without = {g.name for g in runnable({"minute_agg": keys})}
    names_with = {g.name for g in runnable({"minute_agg": keys, "filings": _filings([])})}
    assert "edgar_filing_frequency" not in names_without
    assert "edgar_filing_frequency" in names_with
