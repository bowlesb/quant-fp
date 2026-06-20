"""Measure the LIVE news feed delay: websocket-arrival minus Alpaca created_at.

The news store keeps honest provenance (``available_at_source``): a BACKFILL row sets ``available_at``
to Alpaca's ``created_at`` (the publish instant), while a LIVE row (``news_capture``) sets it to the
websocket ARRIVAL instant we observed. Both keep ``published_at = created_at`` as metadata. The two
sides therefore differ by exactly the real feed delay — the time between an article being published and
our live socket seeing it.

This module computes that delay's distribution over LIVE rows::

    lag = available_at - published_at      (rows WHERE available_at_source == SRC_LIVE)

and reports the p50 / p90 / p99 seconds. The Modeller calibrates the hotness hunt's EMBARGO to the p90
(a backfill-computed feature that gates on ``available_at <= minute`` would otherwise see an article up
to ``lag`` earlier than live could — the embargo offset closes that look-ahead so backfill and live
agree). See ``experiments/2026-06-20-news-hotness/prereg.md`` (EMBARGO = p90 of measured live lag).

The measurement is parity-honest: it reads the SAME store the hunt reads, selects only rows the live
capture actually observed, and uses ``published_at`` (created_at) which is identical on both sides — so
the lag is the genuine websocket arrival delay, not a storage artifact.

Run inside fp-dev::

    docker run --rm -v fp_store_real:/store -v "$PWD":/app -w /app fp-dev \\
        python -m quantlib.data.news_lag --store /store

Until enough LIVE rows have accumulated (``news_capture`` only just came up), the script reports the
current live-row count and the minimum-sample threshold instead of an unreliable quantile.
"""

from __future__ import annotations

import argparse
import glob
import logging
import os

import polars as pl

from quantlib.data.news_store import NEWS_SCHEMA, SRC_LIVE

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("news_lag")

DEFAULT_STORE = "/store"
MIN_LIVE_SAMPLE = 200  # below this many live rows the p90 is too noisy to hand the Modeller as EMBARGO.

_LAG_COLUMNS = ["id", "available_at", "available_at_source", "published_at"]


def load_live_lag_seconds(store: str) -> pl.DataFrame:
    """Every LIVE-arrival article with its feed-delay ``lag_seconds = available_at - published_at``.

    Reads the raw news partitions directly (the shared ``load_news`` loader drops ``available_at_source``,
    which we need to isolate the live rows). Returns a frame with ``id``, ``available_at``,
    ``published_at``, and ``lag_seconds`` (float), one row per live article. A non-live store yields an
    empty frame.
    """
    pattern = os.path.join(store, "news", "published_date=*", "data.parquet")
    paths = sorted(glob.glob(pattern))
    if not paths:
        return pl.DataFrame(schema={"id": NEWS_SCHEMA["id"], "lag_seconds": pl.Float64})
    frame = pl.read_parquet(paths).select(_LAG_COLUMNS)
    live = frame.filter(pl.col("available_at_source") == SRC_LIVE)
    if live.height == 0:
        return live.with_columns(pl.lit(None, dtype=pl.Float64).alias("lag_seconds"))
    return live.with_columns(
        (pl.col("available_at") - pl.col("published_at"))
        .dt.total_microseconds()
        .truediv(1_000_000)
        .alias("lag_seconds")
    )


def lag_summary(lag: pl.DataFrame) -> dict[str, float | int]:
    """The p50 / p90 / p99 (and min/max/count) of ``lag_seconds`` for the live rows."""
    series = lag["lag_seconds"].drop_nulls()
    return {
        "count": series.len(),
        "p50_seconds": float(series.quantile(0.50, "nearest") or 0.0),
        "p90_seconds": float(series.quantile(0.90, "nearest") or 0.0),
        "p99_seconds": float(series.quantile(0.99, "nearest") or 0.0),
        "min_seconds": float(series.min() or 0.0),
        "max_seconds": float(series.max() or 0.0),
    }


def report(store: str) -> None:
    lag = load_live_lag_seconds(store)
    count = lag.height
    if count < MIN_LIVE_SAMPLE:
        logger.info(
            "INSUFFICIENT live overlap: %d live-arrival rows (< %d threshold). news_capture just came "
            "up; re-run once live rows accumulate. The p90 EMBARGO calibration is GATED on this.",
            count,
            MIN_LIVE_SAMPLE,
        )
        return
    summary = lag_summary(lag)
    logger.info(
        "LIVE feed-delay lag over %d articles: p50=%.1fs p90=%.1fs p99=%.1fs (min=%.1fs max=%.1fs)",
        summary["count"],
        summary["p50_seconds"],
        summary["p90_seconds"],
        summary["p99_seconds"],
        summary["min_seconds"],
        summary["max_seconds"],
    )
    logger.info(
        "EMBARGO calibration for the Modeller: p90 = %.1f seconds = %.2f minutes (round UP to the "
        "feature window's minute granularity).",
        summary["p90_seconds"],
        summary["p90_seconds"] / 60.0,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Measure the live news feed-delay lag (p90 = EMBARGO).")
    parser.add_argument("--store", default=DEFAULT_STORE)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    report(args.store)


if __name__ == "__main__":
    main()
