"""Per-symbol WINDOWED news-sentiment features (family: REFERENCE, Layer A) — Ben's first news featurization.

Baseline-sentiment intensity/abnormality off the live ``/store/news`` article tape (each article carries a
deterministic lexicon ``sentiment`` stamped at first sight — quantlib.data.news_sentiment), NO point-in-time
headline NLP. Each (symbol, minute) cell reads ONLY that symbol's articles with ``available_at <= minute`` —
the point-in-time, look-ahead-safe gate. ``available_at`` is FIXED at first sight (the store de-dups by id,
first-sight-wins, never rewriting it) AND ``sentiment`` is a pure function of the article's own text (identical
live vs backfill), so the gated, scored article set at any minute T is IDENTICAL on both sides: a windowed
sentiment feature keyed purely on ``available_at <= minute`` is parity-true BY CONSTRUCTION — the SAME
session-snapshot + per-minute point-in-time gate contract the ``edgar_filing_frequency`` group nailed.

Per-SYMBOL (not a universe reduce), so it runs per shard on each shard's own symbols' articles — a normal
FeatureGroup, NOT a ReductionGroup, and NOT a held-mutable-state group: it carries NO cross-minute accumulator
(the per-minute value is a pure function of the session-snapshot + the at-T gate), so ``up_to_date()`` stays the
default True (stateless, always up to date — nothing to reseed across minutes / a hot-swap / a session boundary),
exactly the RunningState contract for a snapshot-based group. The session snapshot (``news`` input) is loaded
ONCE at session start over ``[day - NEWS_LOOKBACK_DAYS, day]`` (the deepest 7-day window plus slack); the
``available_at <= minute`` gate inside ``compute`` makes it point-in-time per minute (an article with
``available_at=10:32`` enters the feature only from the 10:32 minute onward — the look-ahead test proves it).

THE ONE LIVE CAVEAT (shared with edgar_filing_frequency): an article whose row lands in the store AFTER the
session snapshot was loaded is reflected only from the next session's snapshot, so the intraday sentiment can lag
a same-session article. The trailing windows (1h / 1d / 7d) are barely perturbed by a single late article, and
parity is never at risk (both sides read the same snapshot). If intraday same-session latency proves to matter,
the upgrade is a minute-refreshed snapshot on the reader, NOT a worker-side store re-read.
"""
from __future__ import annotations

import polars as pl

from quantlib.features.base import (
    BatchContext,
    FeatureGroup,
    FeatureSpec,
    FeatureType,
    InputSpec,
    Source,
)
from quantlib.features.registry import register

# Trailing windows (in MINUTES) the sentiment aggregates are kept over: 1 hour, 1 day, 7 days.
WINDOWS_M: dict[str, int] = {"60m": 60, "1d": 1440, "7d": 10080}
# The windows the article COUNT and net-sum (intensity) features use — the shorter, intraday-relevant ones.
COUNT_WINDOWS_M: dict[str, int] = {"60m": 60, "1d": 1440}
# How far back the news source must be PRESENT for a backfill of this group (the deepest 7-day window plus
# slack) — declared via ``source_lookback_days`` so ensure_sources expands a backfill's horizon by it. MIRRORS
# ``loaders.NEWS_LOOKBACK_DAYS`` (the reader's snapshot window); kept here rather than imported because
# ``loaders`` reads DB env at module import, and a pure source DECLARATION must import without a DB.
NEWS_LOOKBACK_DAYS = 9


@register
class NewsSentimentGroup(FeatureGroup):
    name = "news_sentiment"
    version = "1.0.0"
    owner = "modeller"
    type = FeatureType.REFERENCE
    inputs = (
        InputSpec(name="minute_agg", columns=("symbol", "minute")),
        InputSpec(name="news", columns=("symbol", "available_at", "sentiment")),
    )
    # Per-session cache keyed by the (news-snapshot, minute-grid) identities: both are fixed for the whole
    # session, so the point-in-time join is recomputed only when either snapshot changes. compute() and
    # compute_latest() share it (compute_latest slices to T's minute off the same cached frame).
    _cache: tuple[int, int, pl.DataFrame] | None = None

    def required_sources(self) -> frozenset[Source]:
        """This group reads ONLY the ``/store/news`` article tape — its minute grid comes from the bar tape
        but every feature value derives from the news source, so a backfill must ENSURE news is current first
        (docs/SOURCE_DATA_DEPENDENCY.md). Overrides the default (which would lift only ``{bars}``) to declare
        the alt-data NEWS source."""
        return frozenset({Source.NEWS})

    def source_lookback_days(self, source: Source) -> int:
        """News must be present back the group's deepest trailing window so the 7-day sentiment aggregate is
        correct from the session's first minute. Matches ``loaders.NEWS_LOOKBACK_DAYS`` (the same snapshot
        window the live/backfill reader loads) so ensure_sources and the reader agree by construction."""
        if source is Source.NEWS:
            return NEWS_LOOKBACK_DAYS
        return 0

    def declare(self) -> list[FeatureSpec]:
        specs: list[FeatureSpec] = []
        for suffix in WINDOWS_M:
            specs.append(
                FeatureSpec(
                    name=f"news_sentiment_mean_{suffix}",
                    description=f"Mean baseline lexicon sentiment of this symbol's news articles that became available in the trailing {suffix} (available_at in (minute - {suffix}, minute]); null when no article in the window (undefined mean).",
                    dtype="Float64",
                    valid_range=(-1.0, 1.0),
                    nan_policy="sparse",
                    layer="A",
                )
            )
        for suffix in COUNT_WINDOWS_M:
            specs.append(
                FeatureSpec(
                    name=f"news_sentiment_sum_{suffix}",
                    description=f"Net sentiment intensity = sum of this symbol's article sentiments available in the trailing {suffix} (count x polarity; 0.0 when no article — neutral net signal).",
                    dtype="Float64",
                    valid_range=(None, None),
                    nan_policy="none",
                    layer="A",
                )
            )
            specs.append(
                FeatureSpec(
                    name=f"news_count_{suffix}",
                    description=f"Count of this symbol's news articles available in the trailing {suffix} (available_at in (minute - {suffix}, minute]).",
                    dtype="Float64",
                    valid_range=(0.0, 100000.0),
                    nan_policy="none",
                    layer="A",
                )
            )
        specs.append(
            FeatureSpec(
                name="news_sentiment_last",
                description="Baseline sentiment of this symbol's MOST RECENT article available as of the minute (available_at <= minute); null when the symbol has no article on record.",
                dtype="Float64",
                valid_range=(-1.0, 1.0),
                nan_policy="sparse",
                layer="A",
            )
        )
        specs.append(
            FeatureSpec(
                name="news_minutes_since_last",
                description="Minutes since this symbol's most recent article became available (available_at <= minute); null when the symbol has no article on record.",
                dtype="Float64",
                valid_range=(0.0, None),
                nan_policy="sparse",
                layer="A",
            )
        )
        return specs

    def _point_in_time(self, keys: pl.DataFrame, news: pl.DataFrame) -> pl.DataFrame:
        """Per (symbol, minute) point-in-time aggregation: join the symbol's articles onto its minute grid and
        aggregate over only those with ``available_at <= minute``. The left join keeps every (symbol, minute)
        cell even for symbols with no news (all-null article columns → counts 0, means/recency null)."""
        joined = keys.join(news, on="symbol", how="left")
        avail = pl.col("available_at") <= pl.col("minute")
        aggs: list[pl.Expr] = []
        for suffix, window in WINDOWS_M.items():
            lower = pl.col("minute") - pl.duration(minutes=window)
            in_window = avail & (pl.col("available_at") > lower)
            aggs.append(
                pl.col("sentiment")
                .filter(in_window)
                .mean()
                .cast(pl.Float64)
                .alias(f"news_sentiment_mean_{suffix}")
            )
        for suffix, window in COUNT_WINDOWS_M.items():
            lower = pl.col("minute") - pl.duration(minutes=window)
            in_window = avail & (pl.col("available_at") > lower)
            aggs.append(
                pl.col("sentiment")
                .filter(in_window)
                .sum()
                .cast(pl.Float64)
                .alias(f"news_sentiment_sum_{suffix}")
            )
            aggs.append(in_window.sum().cast(pl.Float64).alias(f"news_count_{suffix}"))
        # Most-recent article: the sentiment carried at the max available_at <= minute, and the gap to it.
        aggs.append(pl.col("available_at").filter(avail).max().alias("_last_at"))
        aggs.append(
            pl.col("sentiment")
            .filter(avail)
            .sort_by(pl.col("available_at").filter(avail))
            .last()
            .cast(pl.Float64)
            .alias("news_sentiment_last")
        )
        out = joined.group_by(["symbol", "minute"]).agg(aggs)
        return out.with_columns(
            (pl.col("minute") - pl.col("_last_at"))
            .dt.total_minutes()
            .cast(pl.Float64)
            .alias("news_minutes_since_last")
        ).drop("_last_at")

    def _compute_cached(self, ctx: BatchContext) -> pl.DataFrame:
        keys = ctx.frame("minute_agg").select(["symbol", "minute"])
        news = ctx.frame("news").select(["symbol", "available_at", "sentiment"])
        cache_key = (id(ctx.frame("news")), id(ctx.frame("minute_agg")))
        if self._cache is not None and self._cache[:2] == cache_key:
            return self._cache[2]
        result = self._point_in_time(keys, news)
        self._cache = (cache_key[0], cache_key[1], result)
        return result

    def compute(self, ctx: BatchContext) -> pl.DataFrame:
        names = [spec.name for spec in self.declare()]
        return self._compute_cached(ctx).select(["symbol", "minute", *names])

    def compute_latest(self, ctx: BatchContext) -> pl.DataFrame:
        """Live form: emit ONLY the latest minute's row per symbol. Each cell is a pure function of the
        symbol's ``available_at <= minute`` articles, so the latest minute is computed directly off the same
        point-in-time join — NO rolling over the buffer's older minutes. Held to byte-equality with
        ``compute().last`` by the generic latest-parity test."""
        minute_agg = ctx.frame("minute_agg")
        if minute_agg.height == 0:
            return self.compute(ctx)
        latest = minute_agg.select(pl.col("minute").max()).item()
        keys = minute_agg.filter(pl.col("minute") == latest).select(["symbol", "minute"]).unique()
        news = ctx.frame("news").select(["symbol", "available_at", "sentiment"])
        names = [spec.name for spec in self.declare()]
        return self._point_in_time(keys, news).select(["symbol", "minute", *names])
