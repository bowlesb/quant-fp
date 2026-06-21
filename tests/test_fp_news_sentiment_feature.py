"""Tests for the per-symbol windowed news-sentiment feature group (Ben's first news featurization).

The group joins the ``/store/news`` article tape (each article carrying a deterministic baseline
``sentiment``) onto the (symbol, minute) grid, gated by ``available_at <= minute``. Because ``available_at``
is fixed at first sight AND ``sentiment`` is a pure function of the article's own text (identical live vs
backfill), the same ``load_news_features`` frame fed to both sides yields identical values — the
BACKFILL==LIVE parity test below is the gate proving that for this NEW input source (a new join source is
exactly where parity bugs hide), mirroring the edgar_filing_frequency parity gate.
"""
from __future__ import annotations

import datetime as dt

import polars as pl

from quantlib.features.base import BatchContext
from quantlib.features.compare import runnable
from quantlib.features.groups.news_sentiment import NewsSentimentGroup

UTC = dt.timezone.utc
SESSION_DAY = dt.datetime(2026, 6, 19, 14, 0, tzinfo=UTC)  # 14:00 UTC ~ market open window


def _minute_grid(symbols: tuple[str, ...], n_min: int, start: dt.datetime = SESSION_DAY) -> pl.DataFrame:
    minutes = pl.DataFrame({"minute": [start + dt.timedelta(minutes=i) for i in range(n_min)]})
    return pl.DataFrame({"symbol": list(symbols)}).join(minutes, how="cross")


def _news(rows: list[tuple[str, dt.datetime, float]]) -> pl.DataFrame:
    return pl.DataFrame(
        rows,
        schema={"symbol": pl.String, "available_at": pl.Datetime("us", "UTC"), "sentiment": pl.Float64},
        orient="row",
    )


def _compute(keys: pl.DataFrame, news: pl.DataFrame) -> pl.DataFrame:
    group = NewsSentimentGroup()
    ctx = BatchContext(frames={"minute_agg": keys, "news": news})
    return group.compute(ctx).sort(["symbol", "minute"])


def test_declared_feature_count_and_names() -> None:
    """3 windowed means + (2 windows x [sum, count]) + last + minutes-since-last = 9 features."""
    group = NewsSentimentGroup()
    names = group.feature_names
    assert len(names) == 9
    assert set(names) == {
        "news_sentiment_mean_60m",
        "news_sentiment_mean_1d",
        "news_sentiment_mean_7d",
        "news_sentiment_sum_60m",
        "news_count_60m",
        "news_sentiment_sum_1d",
        "news_count_1d",
        "news_sentiment_last",
        "news_minutes_since_last",
    }


def test_windowed_mean_is_point_in_time() -> None:
    """mean_{60m,1d,7d} averages only articles with available_at in (minute - window, minute]."""
    keys = _minute_grid(("AAA",), n_min=1)
    minute = keys["minute"][0]
    news = _news(
        [
            ("AAA", minute - dt.timedelta(minutes=30), 1.0),  # in 60m / 1d / 7d
            ("AAA", minute - dt.timedelta(hours=5), -1.0),  # in 1d / 7d (not 60m)
            ("AAA", minute - dt.timedelta(days=3), 0.5),  # in 7d only
        ]
    )
    row = _compute(keys, news).row(0, named=True)
    assert row["news_sentiment_mean_60m"] == 1.0  # only the +1.0 article
    assert row["news_sentiment_mean_1d"] == 0.0  # mean(1.0, -1.0)
    assert abs(row["news_sentiment_mean_7d"] - (1.0 - 1.0 + 0.5) / 3) < 1e-12
    assert row["news_count_60m"] == 1.0
    assert row["news_count_1d"] == 2.0


def test_sum_is_net_intensity_and_zero_when_empty() -> None:
    """sum = net sentiment intensity (count x polarity); 0.0 when no article in the window (neutral net)."""
    keys = _minute_grid(("AAA",), n_min=1)
    minute = keys["minute"][0]
    news = _news(
        [
            ("AAA", minute - dt.timedelta(minutes=10), 0.5),
            ("AAA", minute - dt.timedelta(minutes=20), 0.5),
        ]
    )
    row = _compute(keys, news).row(0, named=True)
    assert row["news_sentiment_sum_60m"] == 1.0
    assert row["news_count_60m"] == 2.0


def test_last_sentiment_and_minutes_since_last() -> None:
    """last = the sentiment of the most recent available article; minutes-since-last = the gap to it."""
    keys = _minute_grid(("AAA",), n_min=1)
    minute = keys["minute"][0]
    news = _news(
        [
            ("AAA", minute - dt.timedelta(minutes=500), 0.9),
            ("AAA", minute - dt.timedelta(minutes=10), -0.3),  # most recent
        ]
    )
    row = _compute(keys, news).row(0, named=True)
    assert row["news_sentiment_last"] == -0.3
    assert row["news_minutes_since_last"] == 10.0


def test_no_news_gives_zero_counts_and_null_means() -> None:
    """A symbol with NO news: counts/sums are 0 (not garbage), means/last/since-last are null (the sentinel
    for 'no article on record')."""
    keys = _minute_grid(("EMPTY",), n_min=1)
    news = _news([("OTHER", SESSION_DAY - dt.timedelta(minutes=5), 1.0)])
    row = _compute(keys, news).row(0, named=True)
    assert row["news_count_60m"] == 0.0
    assert row["news_count_1d"] == 0.0
    assert row["news_sentiment_sum_60m"] == 0.0
    assert row["news_sentiment_mean_60m"] is None
    assert row["news_sentiment_mean_1d"] is None
    assert row["news_sentiment_last"] is None
    assert row["news_minutes_since_last"] is None


def test_lookahead_article_enters_only_from_its_minute() -> None:
    """THE point-in-time guarantee: an article with available_at=T must NOT appear in the T-1 minute's value
    and MUST appear from the T minute onward (no look-ahead)."""
    keys = _minute_grid(("AAA",), n_min=5)
    minutes = keys["minute"].to_list()
    article_minute = minutes[2]  # the 3rd minute
    news = _news([("AAA", article_minute, 0.8)])
    out = _compute(keys, news)
    counts = out["news_count_60m"].to_list()
    assert counts == [0.0, 0.0, 1.0, 1.0, 1.0]  # appears exactly at article_minute, never before
    last = out["news_sentiment_last"].to_list()
    assert last[:2] == [None, None]
    assert last[2:] == [0.8, 0.8, 0.8]
    since = out["news_minutes_since_last"].to_list()
    assert since[:2] == [None, None]
    assert since[2:] == [0.0, 1.0, 2.0]


def _wide_news(symbols: tuple[str, ...], seed_minute: dt.datetime) -> pl.DataFrame:
    """A multi-symbol article history spanning >1 week, with one article landing INSIDE the session window so
    the intraday look-ahead gate is exercised in the parity test."""
    rows: list[tuple[str, dt.datetime, float]] = []
    for s_idx, symbol in enumerate(symbols):
        for k in range(6):
            sentiment = ((k % 5) - 2) / 2.0  # spread of -1.0 .. +1.0
            rows.append((symbol, seed_minute - dt.timedelta(hours=6 * k + s_idx), sentiment))
        # an intraday same-session article a few minutes into the session
        rows.append((symbol, seed_minute + dt.timedelta(minutes=3 + s_idx), 0.25))
    return _news(rows)


def test_backfill_equals_live_parity() -> None:
    """THE GATE for a new join source: the group's BACKFILL form (whole-buffer compute) must equal its LIVE
    form (per-minute compute_latest over a trailing buffer) cell-for-cell at every minute. Both read the SAME
    news snapshot (available_at + sentiment fixed at first sight), so they must agree exactly."""
    symbols = ("AAA", "BBB", "CCC", "DDD")
    n_min = 30
    keys = _minute_grid(symbols, n_min=n_min)
    news = _wide_news(symbols, SESSION_DAY)
    group = NewsSentimentGroup()

    backfill = group.compute(BatchContext(frames={"minute_agg": keys, "news": news})).sort(
        ["symbol", "minute"]
    )

    minutes = sorted(keys["minute"].unique().to_list())
    live_rows = []
    for end_idx in range(len(minutes)):
        buf = keys.filter(pl.col("minute") <= minutes[end_idx])
        live_group = NewsSentimentGroup()  # fresh per minute: the live worker recomputes each minute
        latest = live_group.compute_latest(BatchContext(frames={"minute_agg": buf, "news": news}))
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


def test_runnable_only_with_news_frame() -> None:
    """The group is runnable iff both minute_agg AND news (with its sentiment column) are present — a
    crypto/bars-only frame (no news) correctly SKIPS it rather than erroring."""
    keys = _minute_grid(("AAA",), n_min=1)
    names_without = {g.name for g in runnable({"minute_agg": keys})}
    names_with = {g.name for g in runnable({"minute_agg": keys, "news": _news([])})}
    assert "news_sentiment" not in names_without
    assert "news_sentiment" in names_with


def test_stateless_up_to_date_default() -> None:
    """The group carries NO cross-minute mutable state (snapshot + at-T gate), so it stays the default
    up_to_date=True (nothing to reseed across minutes / a hot-swap / a session boundary) — the RunningState
    contract for a snapshot-based group."""
    group = NewsSentimentGroup()
    assert group.up_to_date(None) is True
    assert group.up_to_date(_minute_grid(("AAA",), n_min=1)) is True
