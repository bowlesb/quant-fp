"""EDGAR filing-FREQUENCY features (family: REFERENCE, Layer A) — Ben's #2 alt-data ask.

Frequency/timing/form-type features off the live ``filings`` event store (db/init/08_filings.sql,
services/edgar; 3.18M rows / 5628 symbols / 1994->2026), NO content parsing. Each (symbol, minute)
cell reads ONLY that symbol's filings with ``available_at <= minute`` — the point-in-time, look-ahead-
safe field. ``available_at`` is FIXED at first sight (the ingestor's ON CONFLICT DO NOTHING never
rewrites it), so the gated set at any minute T is IDENTICAL in live and backfill: a DB-join feature
keyed purely on ``available_at <= minute`` is parity-true BY CONSTRUCTION, the same compute-time-join
contract the reference/behavioral-cluster joins use, extended with the per-minute point-in-time gate
the multi_day groups apply to the daily snapshot.

The ``filings`` input is a per-session snapshot loaded ONCE covering ``[day_start - 90d, day_end)`` (the
deepest count window plus the trailing-year baseline for the burst); the ``available_at <= minute`` gate
inside ``compute`` makes it point-in-time per minute, so an 8-K with ``available_at=10:32`` enters the
feature only from the 10:32 minute onward (verified by the look-ahead test). Per-SYMBOL (not a universe
reduce), so it runs per shard on each shard's own symbols' filings — a normal FeatureGroup, NOT a
ReductionGroup (calendar-day counts off an event table, not minute-bar folds), so the incremental
engine never touches it.

SESSION-SNAPSHOT vs the design's per-minute re-query: the snapshot is loaded ONCE at session start
covering ``[day_start - lookback, day_end)`` (the same lifecycle as the ``daily``/``reference``/``universe``
snapshots), NOT re-queried each minute. This keeps the LIVE and BACKFILL sides reading the byte-identical
``load_filings`` frame, so backfill==live is parity-true BY CONSTRUCTION (the parity test's whole point) —
the design's alternative (a) per-minute trailing-window DB re-query would have to thread a DB connection
into the spawned shard workers (a much larger surface) and risk live/backfill skew if the live query saw a
filing the backfill replay didn't. THE ONE LIVE CAVEAT this makes explicit: a same-session 8-K whose DB row
lands AFTER the premarket snapshot load is reflected only from the next session's snapshot, so
``minutes_since_last_8k`` can lag intraday for a filing that arrives mid-session. The frequency/count
features (7/30/90-day) are unaffected to any material degree (a same-day filing barely moves a multi-week
count), and parity is never at risk (both sides read the same snapshot). If intraday same-session event
latency proves to matter, the upgrade is a minute-refreshed snapshot on the reader, NOT a worker-side DB hit.
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

COUNT_WINDOWS_D: tuple[int, ...] = (7, 30, 90)
# How far back the EDGAR source must be PRESENT for a backfill of this group (the deepest window — the
# 365-day burst baseline — plus calendar-edge slack) — declared via ``source_lookback_days`` so ensure_sources
# expands a backfill's horizon by it. MIRRORS ``loaders.FILINGS_LOOKBACK_DAYS`` (the reader's snapshot window);
# kept here rather than imported because ``loaders`` reads DB env at module import, and a pure source
# DECLARATION must import without a DB.
FILINGS_LOOKBACK_DAYS = 370
# SEC form_type label (as stored in the filings table) -> feature-name suffix. Form 4 is stored as "4".
MAJOR_FORMS: dict[str, str] = {"8-K": "8k", "10-Q": "10q", "10-K": "10k", "4": "form4"}
BURST_BASELINE_D = 365  # trailing-year window the 7-day burst rate is compared against
BURST_WINDOW_D = 7


@register
class EdgarFilingFrequencyGroup(FeatureGroup):
    name = "edgar_filing_frequency"
    version = "1.0.0"
    owner = "modeller"
    type = FeatureType.REFERENCE
    inputs = (
        InputSpec(name="minute_agg", columns=("symbol", "minute")),
        InputSpec(name="filings", columns=("symbol", "form_type", "available_at")),
    )
    # Per-session cache keyed by the (filings, minute-grid) snapshot identities: both are fixed for the
    # whole session, so the point-in-time join is recomputed only when either snapshot changes. compute()
    # and compute_latest() share it (compute_latest slices to T's minute off the same cached frame).
    _cache: tuple[int, int, pl.DataFrame] | None = None

    def required_sources(self) -> frozenset[Source]:
        """This group reads ONLY the EDGAR ``filings`` event store — its minute grid comes from the bar tape
        but every feature value derives from the filings source, so a backfill must ENSURE EDGAR is current
        first (docs/SOURCE_DATA_DEPENDENCY.md). Overrides the default (which would lift only ``{bars}``) to
        declare the alt-data EDGAR source."""
        return frozenset({Source.EDGAR})

    def source_lookback_days(self, source: Source) -> int:
        """EDGAR must be present back the deepest window the group reads — the 365-day burst baseline — so the
        trailing counts and minutes-since-last are correct from the session's first minute. Matches
        ``loaders.FILINGS_LOOKBACK_DAYS`` (the same snapshot window the live/backfill reader loads)."""
        if source is Source.EDGAR:
            return FILINGS_LOOKBACK_DAYS
        return 0

    def declare(self) -> list[FeatureSpec]:
        specs: list[FeatureSpec] = []
        for window in COUNT_WINDOWS_D:
            specs.append(
                FeatureSpec(
                    name=f"edgar_filing_count_{window}d",
                    description=f"Count of this symbol's SEC filings that became publicly available in the trailing {window} calendar days as of the minute (available_at in (minute - {window}d, minute]).",
                    dtype="Float64",
                    valid_range=(0.0, 10000.0),
                    nan_policy="none",
                    layer="A",
                )
            )
        specs.append(
            FeatureSpec(
                name="edgar_minutes_since_last_filing",
                description="Minutes since this symbol's most recent filing became available (available_at <= minute); null when the symbol has no prior filing on record.",
                dtype="Float64",
                valid_range=(0.0, None),
                nan_policy="sparse",
                layer="A",
            )
        )
        specs.append(
            FeatureSpec(
                name="edgar_minutes_since_last_8k",
                description="Minutes since this symbol's most recent 8-K (material-event filing) became available; null when the symbol has no prior 8-K on record.",
                dtype="Float64",
                valid_range=(0.0, None),
                nan_policy="sparse",
                layer="A",
            )
        )
        for suffix in MAJOR_FORMS.values():
            specs.append(
                FeatureSpec(
                    name=f"edgar_count_{suffix}_90d",
                    description=f"Count of this symbol's {suffix.upper()} filings available in the trailing 90 calendar days as of the minute.",
                    dtype="Float64",
                    valid_range=(0.0, 10000.0),
                    nan_policy="none",
                    layer="A",
                )
            )
        specs.append(
            FeatureSpec(
                name="edgar_filing_burst",
                description="Filing-frequency spike: trailing-7-day filing count relative to the symbol's expected 7-day count from its trailing-365-day baseline rate (count_7d / (count_365d * 7/365)); NaN when the baseline is zero (no filings in the trailing year, undefined rate).",
                dtype="Float64",
                valid_range=(0.0, None),
                nan_policy="sparse",
                layer="A",
            )
        )
        return specs

    def _point_in_time(self, keys: pl.DataFrame, filings: pl.DataFrame) -> pl.DataFrame:
        """Per (symbol, minute) point-in-time aggregation: join the symbol's filings onto its minute grid
        and aggregate over only those with ``available_at <= minute``. The left join keeps every (symbol,
        minute) cell even for symbols with no filings (all-null filing columns → counts 0, since-last null).
        """
        joined = keys.join(filings, on="symbol", how="left")
        avail = pl.col("available_at") <= pl.col("minute")
        aggs: list[pl.Expr] = []
        for window in COUNT_WINDOWS_D:
            lower = pl.col("minute") - pl.duration(days=window)
            aggs.append(
                (avail & (pl.col("available_at") > lower)).sum().cast(pl.Float64).alias(f"edgar_filing_count_{window}d")
            )
        aggs.append(
            (pl.col("minute").first() - pl.col("available_at").filter(avail).max())
            .dt.total_minutes()
            .cast(pl.Float64)
            .alias("edgar_minutes_since_last_filing")
        )
        aggs.append(
            (
                pl.col("minute").first()
                - pl.col("available_at").filter(avail & (pl.col("form_type") == "8-K")).max()
            )
            .dt.total_minutes()
            .cast(pl.Float64)
            .alias("edgar_minutes_since_last_8k")
        )
        lower_90 = pl.col("minute") - pl.duration(days=90)
        for form_label, suffix in MAJOR_FORMS.items():
            aggs.append(
                (avail & (pl.col("available_at") > lower_90) & (pl.col("form_type") == form_label))
                .sum()
                .cast(pl.Float64)
                .alias(f"edgar_count_{suffix}_90d")
            )
        lower_7 = pl.col("minute") - pl.duration(days=BURST_WINDOW_D)
        lower_baseline = pl.col("minute") - pl.duration(days=BURST_BASELINE_D)
        aggs.append((avail & (pl.col("available_at") > lower_7)).sum().cast(pl.Float64).alias("_burst_recent"))
        aggs.append(
            (avail & (pl.col("available_at") > lower_baseline)).sum().cast(pl.Float64).alias("_burst_baseline")
        )
        out = joined.group_by(["symbol", "minute"]).agg(aggs)
        expected = pl.col("_burst_baseline") * (BURST_WINDOW_D / BURST_BASELINE_D)
        return out.with_columns(
            pl.when(expected > 0).then(pl.col("_burst_recent") / expected).otherwise(None).alias("edgar_filing_burst")
        ).drop(["_burst_recent", "_burst_baseline"])

    def _compute_cached(self, ctx: BatchContext) -> pl.DataFrame:
        keys = ctx.frame("minute_agg").select(["symbol", "minute"])
        filings = ctx.frame("filings").select(["symbol", "form_type", "available_at"])
        cache_key = (id(ctx.frame("filings")), id(ctx.frame("minute_agg")))
        if self._cache is not None and self._cache[:2] == cache_key:
            return self._cache[2]
        result = self._point_in_time(keys, filings)
        self._cache = (cache_key[0], cache_key[1], result)
        return result

    def compute(self, ctx: BatchContext) -> pl.DataFrame:
        names = [spec.name for spec in self.declare()]
        return self._compute_cached(ctx).select(["symbol", "minute", *names])

    def compute_latest(self, ctx: BatchContext) -> pl.DataFrame:
        """Live form: emit ONLY the latest minute's row per symbol. Each cell is a pure function of the
        symbol's ``available_at <= minute`` filings, so the latest minute is computed directly off the
        same point-in-time join — NO rolling over the buffer's older minutes (the default would join the
        whole-buffer grid then discard all but T). Held to byte-equality with ``compute().last`` by the
        generic latest-parity test."""
        minute_agg = ctx.frame("minute_agg")
        if minute_agg.height == 0:
            return self.compute(ctx)
        latest = minute_agg.select(pl.col("minute").max()).item()
        keys = minute_agg.filter(pl.col("minute") == latest).select(["symbol", "minute"]).unique()
        filings = ctx.frame("filings").select(["symbol", "form_type", "available_at"])
        names = [spec.name for spec in self.declare()]
        return self._point_in_time(keys, filings).select(["symbol", "minute", *names])
