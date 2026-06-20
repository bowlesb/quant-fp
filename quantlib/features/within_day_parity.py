"""Within-Day Parity Certifier — PHASE 1: the resource-bounded settled-window compare mechanism.

Per docs/WITHIN_DAY_PARITY_CERTIFICATION.md (gate-read approved phased build). This phase proves the
MECHANISM + the RESOURCE BOUNDING, manually triggered for ONE feature group:

  read LIVE (source=stream) and BACKFILL (source=backfill) feature cells for a group, on a SETTLED
  intraday minute window, for a SAMPLE of symbols → compare cell-for-cell with the EXISTING parity
  primitives (compare.cell_verdict / match_predicate, the group's own tolerance) → report per-feature
  match rate.

It does NOT yet: run the continuous loop, root-cause+fix, or grant trust (phases 2-3). It is READ-ONLY
(reads the store; computes the backfill in-memory via the same compute() path the materializer uses) and
HARD-BOUNDED so it never dents live capture (gate-read #4 — the make-or-break):

  * ONE group at a time (caller passes one group name).
  * A bounded SAMPLE of symbols (default 30), never the full universe — the nightly sweep does the full
    pass; this is a fast spot-check.
  * The SETTLED WINDOW ONLY — a bounded recent minute range held back by SETTLE_LAG, not the whole day.
  * No new tolerance: reuse the group spec's rtol + match_predicate, so a within-day match == a nightly
    match by construction (gate-read #2/#6).

The settle-window selection + symbol sampling keep the compute tiny; the caller (ops driver) runs this in
a nice'd, memory-capped, guard-named container so the live_monitor mem/disk guard yields it to fc.
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging

import polars as pl

from quantlib.features import store
from quantlib.features.compare import cell_verdict
from quantlib.features.registry import REGISTRY
from quantlib.features.session import rth_mask
from quantlib.features.settle_lag import FALLBACK_LAG_MINUTES

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("within_day_parity")

DEFAULT_SAMPLE_SIZE = 30
DEFAULT_WINDOW_MINUTES = 30


def settle_lag_for_group(group_name: str) -> float:
    """The SETTLE_LAG (minutes) to hold the window back by for this group: the WORST (largest) fallback
    over the layers the group's tier touches. PHASE 1 uses the conservative fallback map; phase 3 swaps in
    the live-measured settle_lag.report() values. Conservative by design (gate-read #1)."""
    group = REGISTRY.get_group(group_name)
    layer_value = getattr(group.type, "value", "").lower()
    if "trade" in layer_value or "flow" in layer_value:
        return float(FALLBACK_LAG_MINUTES["trades"])
    if "quote" in layer_value or "micro" in layer_value or "spread" in layer_value:
        return float(FALLBACK_LAG_MINUTES["quotes"])
    # Bar-derived groups (price/volume/volatility/momentum/technical/...) settle on the bars layer.
    return float(FALLBACK_LAG_MINUTES["bars"])


def settled_window(
    now_utc: dt.datetime, settle_lag_min: float, window_minutes: int
) -> tuple[dt.datetime, dt.datetime]:
    """The [start, end] UTC minute range that has settled at least ``settle_lag_min`` ago: a
    ``window_minutes``-long band ending ``settle_lag_min`` before now. Never includes the live tail."""
    end = now_utc - dt.timedelta(minutes=settle_lag_min)
    start = end - dt.timedelta(minutes=window_minutes)
    return start.replace(second=0, microsecond=0), end.replace(second=0, microsecond=0)


def window_for_day(
    day: dt.date,
    now_utc: dt.datetime,
    settle_lag_min: float,
    window_minutes: int,
) -> tuple[dt.datetime, dt.datetime]:
    """The settled window to compare on, anchored to ``day``.

    - If ``day`` is TODAY (the live case), the window ends ``settle_lag_min`` before now — the rolling
      recently-settled band that excludes the provisional live tail.
    - If ``day`` is a PAST day (historical spot-check / testing), the whole session is long settled, so use
      a fixed mid-session RTH band (the last ``window_minutes`` before the ~15:30-ET late edge) — anchored
      to the requested day, NOT to wall-clock now."""
    if day == now_utc.date():
        return settled_window(now_utc, settle_lag_min, window_minutes)
    # Past day: a fully-settled mid-session RTH band, e.g. ending 15:30 ET (19:30/20:30 UTC by DST).
    band_end = dt.datetime.combine(day, dt.time(19, 30), tzinfo=dt.timezone.utc)
    band_start = band_end - dt.timedelta(minutes=window_minutes)
    return band_start, band_end


def sample_symbols(feature_root: str, day: dt.date, sample_size: int) -> list[str]:
    """A bounded sample of the day's STREAM symbols (what live capture actually emitted), pinned to the
    most-liquid by appearance so the spot-check lands on names with real tape. Never the full universe."""
    symbols = store.stream_symbols_on(feature_root, day.isoformat())
    if not symbols:
        return []
    # Deterministic, liquidity-agnostic but stable: sorted head. (Phase 3 will rank by liquidity / pin
    # market tickers; phase 1 only needs a small, reproducible, non-empty sample.)
    return sorted(symbols)[:sample_size]


def compare_window(
    feature_root: str,
    group_name: str,
    day: dt.date,
    symbols: list[str],
    window_start: dt.datetime,
    window_end: dt.datetime,
) -> pl.DataFrame:
    """Per-feature match summary for the group over the settled window + symbol sample.

    Reads BOTH sources from the store, joins on (symbol, minute), filters to the window ∩ RTH, and applies
    the group's own cell_verdict per feature. Returns one row per feature: n_compared / n_match /
    n_mismatch / n_extra / n_missing / value_rate. Read-only — no store writes."""
    group = REGISTRY.get_group(group_name)
    specs = {spec.name: spec for spec in group.declare()}
    feature_names = list(specs.keys())

    day_start = dt.datetime.combine(day, dt.time(0, 0), tzinfo=dt.timezone.utc)
    day_end = dt.datetime.combine(day, dt.time(23, 59, 59), tzinfo=dt.timezone.utc)

    live = store.get_features(feature_names, symbols, day_start, day_end, feature_root, source="stream")
    backfill = store.get_features(
        feature_names, symbols, day_start, day_end, feature_root, source="backfill"
    )
    if backfill.height == 0:
        logger.warning(
            "no BACKFILL cells for group=%s day=%s — window not settled / not materialized", group_name, day
        )
        return pl.DataFrame()
    if live.height == 0:
        logger.warning("no LIVE (stream) cells for group=%s day=%s — capture gap?", group_name, day)
        return pl.DataFrame()

    joined = live.join(backfill, on=["symbol", "minute"], how="full", suffix="_bk", coalesce=True).filter(
        (pl.col("minute") >= window_start) & (pl.col("minute") <= window_end) & rth_mask(pl.col("minute"))
    )
    if joined.height == 0:
        logger.warning(
            "no cells in settled window [%s, %s] RTH for group=%s", window_start, window_end, group_name
        )
        return pl.DataFrame()

    rows: list[dict[str, object]] = []
    for feature, spec in specs.items():
        if feature not in joined.columns or f"{feature}_bk" not in joined.columns:
            continue
        verdicts = joined.select(cell_verdict(spec, feature, joined.schema).alias("v"))
        counts = verdicts.group_by("v").len().to_dict(as_series=False)
        tally = dict(zip(counts["v"], counts["len"]))
        n_match = int(tally.get("match", 0))
        n_mismatch = int(tally.get("mismatch", 0))
        n_extra = int(tally.get("extra_live", 0))
        n_missing = int(tally.get("missing_live", 0))
        n_compared = n_match + n_mismatch
        value_rate = (n_match / n_compared) if n_compared > 0 else None
        rows.append(
            {
                "feature": feature,
                "tolerance": spec.tolerance,
                "n_compared": n_compared,
                "n_match": n_match,
                "n_mismatch": n_mismatch,
                "n_extra_live": n_extra,
                "n_missing_live": n_missing,
                "value_rate": value_rate,
            }
        )
    return pl.DataFrame(rows)


def run(
    feature_root: str,
    group_name: str,
    day: dt.date | None = None,
    sample_size: int = DEFAULT_SAMPLE_SIZE,
    window_minutes: int = DEFAULT_WINDOW_MINUTES,
) -> pl.DataFrame:
    """PHASE-1 manual entry: spot-check one group's live==backfill on its settled window + a symbol sample."""
    now_utc = dt.datetime.now(dt.timezone.utc)
    day = day or now_utc.date()
    lag = settle_lag_for_group(group_name)
    window_start, window_end = window_for_day(day, now_utc, lag, window_minutes)
    symbols = sample_symbols(feature_root, day, sample_size)
    logger.info(
        "WDPC phase-1 group=%s day=%s settle_lag=%.0fmin window=[%s,%s] sample=%d symbols",
        group_name,
        day,
        lag,
        window_start.isoformat(timespec="minutes"),
        window_end.isoformat(timespec="minutes"),
        len(symbols),
    )
    if not symbols:
        logger.warning("no stream symbols for day=%s — nothing to compare (off-session / pre-capture)", day)
        return pl.DataFrame()
    summary = compare_window(feature_root, group_name, day, symbols, window_start, window_end)
    if summary.height > 0:
        logger.info("per-feature parity:\n%s", summary)
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--feature-root", default="/store")
    parser.add_argument("--group", required=True, help="the single feature group to spot-check")
    parser.add_argument("--day", default=None, help="YYYY-MM-DD (default: today UTC)")
    parser.add_argument("--sample-size", type=int, default=DEFAULT_SAMPLE_SIZE)
    parser.add_argument("--window-minutes", type=int, default=DEFAULT_WINDOW_MINUTES)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    day = dt.date.fromisoformat(args.day) if args.day else None
    run(
        args.feature_root,
        args.group,
        day=day,
        sample_size=args.sample_size,
        window_minutes=args.window_minutes,
    )


if __name__ == "__main__":
    main()
