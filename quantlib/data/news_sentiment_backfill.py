"""One-time / idempotent backfill RE-SCORE of the baseline sentiment for already-stored ``/store/news``
articles.

New articles get their ``sentiment`` + ``sentiment_model_version`` stamped at ingest (news_fetchers
``article_to_row``, the shared live+backfill write path). The articles ALREADY on disk before the sentiment
field existed carry a null ``sentiment`` (the store's ``_conform_to_schema`` fills the absent column with
null on read). This script scores those rows IN PLACE from their stored ``headline`` + ``summary`` — the SAME
deterministic lexicon (``news_sentiment.score_article``) the ingest path uses, so a backfilled score is
byte-identical to what live would have stamped (the text is the article's own content, look-ahead-safe).

It is:
  * **Idempotent** — only rows whose ``sentiment`` is null OR whose ``sentiment_model_version`` differs from
    the current ``MODEL_VERSION`` are (re)scored; a second run over an already-scored store rewrites nothing.
    A future lexicon bump (new ``MODEL_VERSION``) therefore re-scores cleanly without ambiguity.
  * **Bounded + safe** — per-partition: read one date's parquet, score the stale rows, atomic tmp+rename
    rewrite (the store's ``_write_partition_atomic``). A crash leaves every untouched partition intact and a
    re-run resumes. NO manifest rewrite (article counts/bytes are unchanged; only the sentiment columns are
    filled), NO ``available_at`` / ``id`` / ``ingested_at`` touched, so the parity contract is preserved.
  * **DRY-RUN by default** — reports how many partitions / rows WOULD be scored and writes nothing unless
    ``--apply`` is passed.

Run inside fp-dev::

    docker run --rm -v fp_store_real:/store -v "$PWD":/app -w /app fp-dev \\
        python -m quantlib.data.news_sentiment_backfill --store /store            # dry-run
    docker run --rm -v fp_store_real:/store -v "$PWD":/app -w /app fp-dev \\
        python -m quantlib.data.news_sentiment_backfill --store /store --apply    # rewrite
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
import logging
import os

import polars as pl

from quantlib.data.news_sentiment import MODEL_VERSION, score_article
from quantlib.data.news_store import _conform_to_schema, _write_partition_atomic

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("news_sentiment_backfill")

DEFAULT_STORE = "/store"


def _needs_scoring() -> pl.Expr:
    """Row predicate: a null score OR a score from a stale model version (so a lexicon bump re-scores)."""
    return pl.col("sentiment").is_null() | (pl.col("sentiment_model_version") != MODEL_VERSION)


def score_frame(frame: pl.DataFrame) -> tuple[pl.DataFrame, int]:
    """Return ``(scored_frame, n_rescored)``: every stale row gets its ``sentiment`` recomputed from its OWN
    stored ``headline`` + ``summary`` (deterministic, identical to the ingest-path score) and its
    ``sentiment_model_version`` set to the current model; already-current rows are left exactly as-is."""
    conformed = _conform_to_schema(frame)
    stale_mask = conformed.select(_needs_scoring()).to_series()
    n_stale = int(stale_mask.sum())
    if n_stale == 0:
        return conformed, 0
    headlines = conformed["headline"].to_list()
    summaries = conformed["summary"].to_list()
    needs = stale_mask.to_list()
    old_scores = conformed["sentiment"].to_list()
    old_versions = conformed["sentiment_model_version"].to_list()
    new_scores: list[float | None] = []
    new_versions: list[str | None] = []
    for idx in range(conformed.height):
        if needs[idx]:
            new_scores.append(score_article(headlines[idx], summaries[idx]))
            new_versions.append(MODEL_VERSION)
        else:
            new_scores.append(old_scores[idx])
            new_versions.append(old_versions[idx])
    scored = conformed.with_columns(
        pl.Series("sentiment", new_scores, dtype=pl.Float64),
        pl.Series("sentiment_model_version", new_versions, dtype=pl.String),
    )
    return scored, n_stale


def _partition_date_iso(path: str) -> str:
    segment = [p for p in path.split(os.sep) if p.startswith("published_date=")][0]
    return segment[len("published_date=") :]


def rescore_store(store: str, apply: bool) -> dict[str, int]:
    """Walk every ``/store/news`` partition, score its stale rows, and (when ``apply``) rewrite it atomically.

    Returns a summary dict (partitions scanned / partitions touched / rows rescored). Dry-run (``apply=False``)
    computes the same counts but writes nothing."""
    pattern = os.path.join(store, "news", "published_date=*", "data.parquet")
    paths = sorted(glob.glob(pattern))
    partitions_touched = 0
    rows_rescored = 0
    for path in paths:
        frame = pl.read_parquet(path)
        scored, n_stale = score_frame(frame)
        if n_stale == 0:
            continue
        partitions_touched += 1
        rows_rescored += n_stale
        date_iso = _partition_date_iso(path)
        logger.info("partition %s: %d/%d rows need (re)scoring", date_iso, n_stale, frame.height)
        if apply:
            _write_partition_atomic(store, dt.date.fromisoformat(date_iso), scored)
    summary = {
        "partitions_scanned": len(paths),
        "partitions_touched": partitions_touched,
        "rows_rescored": rows_rescored,
    }
    verb = "REWROTE" if apply else "WOULD rewrite (dry-run)"
    logger.info(
        "%s %d/%d partitions, %d rows (model=%s)",
        verb,
        partitions_touched,
        len(paths),
        rows_rescored,
        MODEL_VERSION,
    )
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Backfill baseline sentiment onto stored /store/news articles."
    )
    parser.add_argument("--store", default=DEFAULT_STORE)
    parser.add_argument("--apply", action="store_true", help="Rewrite partitions; omit for a dry-run.")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    rescore_store(args.store, apply=args.apply)


if __name__ == "__main__":
    main()
