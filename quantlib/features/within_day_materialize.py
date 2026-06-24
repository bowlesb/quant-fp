"""Within-Day Parity Certifier — LIVE-INTRADAY backfill MATERIALIZATION (the on-demand settled-window recompute).

Phase-1 :func:`within_day_parity.compare_window` reads ``source=backfill`` cells from the store — which on a
SWEPT day are already there (the nightly sweep materialized them). But the within-day monitor runs INTRADAY,
on the CURRENT day, where the backfill side has NOT been materialized yet: the settled window is minutes old,
no nightly sweep has touched it. So an intraday cycle would always find "no backfill cells" and never make
streak progress — the monitor would only ever work on past, already-swept days.

This module closes that gap: before the compare, MATERIALIZE just the one group's backfill side over the
settled window's day for the symbol sample, reading the already-downloaded ``/store/raw`` tape via the SAME
``materialize.materialize_from_raw_*`` path the nightly sweep + selective backfill use — so the backfill cells
the monitor compares against are produced by the canonical backfill code path, not a within-day shortcut (a
within-day match == a nightly match by construction, the gate-read contract).

It is resource-bounded exactly like the rest of the WDPC (one group, the symbol sample, the settled day):

  * ONE GROUP — only the monitored group's partition is (re)written (``only_groups=[group]``), never the
    whole ~610-feature set.
  * THE SYMBOL SAMPLE — the same bounded sample the monitor compares (default 30), never the full universe.
  * RAW, NOT ALPACA — reads ``/store/raw`` (download-once). Optionally runs ``ensure_inputs`` FIRST (the
    source-data dependency abstraction, #74) to patch any holes in the group's declared raw layers over the
    window before computing — so the live-intraday materialize never silently computes off a partial tape.
  * THE RIGHT VARIANT — bar-only groups use the bar materialize; trade/quote groups use the tick-enriched
    one (so order-flow features are runnable); cross-sectional universe-reduce groups use the un-sharded
    full-universe bar materialize (a chunked partial-universe reduction would never match the live stream).

Why per-DAY not per-window: the store partitions by (group, source, date), so the materialize writes the
day's partition; ``compare_window`` then filters to the settled window ∩ RTH. Materializing the whole sampled
day for one group on a 30-symbol sample is tiny — and it makes the next cycle's window (later the same day)
already present, so repeated cycles only re-materialize when the day's tape grew.
"""

from __future__ import annotations

import datetime as dt
import logging
import os

import polars as pl

from quantlib.data.raw_store import partition_dir
from quantlib.data.source_dependency import default_fetcher, ensure_inputs, required_layers_for_groups
from quantlib.features.base import FeatureType, RawLayer
from quantlib.features.materialize import (
    materialize_alpaca_bar_groups,
    materialize_from_raw_bar_groups,
    materialize_from_raw_groups,
)
from quantlib.features.registry import REGISTRY

logger = logging.getLogger("within_day_materialize")

# The STORE root (NOT ``/store/raw``): ``raw_store.partition_dir`` appends the ``raw/`` segment itself, so the
# raw-reading materialize variants take the store root. This matches ``selective_backfill.DEFAULT_RAW_ROOT``.
DEFAULT_RAW_ROOT = "/store"


def _group_needs_full_universe(group_name: str) -> bool:
    """A cross-sectional universe-reduce group (breadth/rank): its per-minute value reduces over the WHOLE
    symbol set, so a symbol-sample materialize would write a partial-universe reduction that the full-universe
    live stream can never match. Such a group is NOT live-intraday materializable on a sample — the caller
    must fall back to the swept-day backfill (the nightly full-universe materialize)."""
    return REGISTRY.get_group(group_name).type == FeatureType.CROSS_SECTIONAL


def _raw_bars_present(raw_root: str, day: dt.date, symbols: list[str]) -> bool:
    """Whether the ``/store/raw/bars`` tape for ``day`` holds REAL rows for the sample (not just empty
    partition shells). Intraday on the CURRENT day the raw bars are not yet acquired — the partition dirs may
    exist (claimed) but the parquet is empty — so a from-raw materialize would write nothing. This decides
    whether to read raw (past, swept day) or fetch from Alpaca (the live-intraday source). Checks a few
    sample symbols and returns True on the first that has rows."""
    for symbol in symbols[:5]:
        path = os.path.join(partition_dir(raw_root, "bars", symbol, day), "data.parquet")
        if not os.path.exists(path):
            continue
        try:
            if pl.read_parquet(path, columns=["ts"]).height > 0:
                return True
        except (OSError, pl.exceptions.PolarsError) as exc:
            logger.warning("raw-bars presence probe failed for %s %s: %s", symbol, day, exc)
    return False


def ensure_window_inputs(
    raw_root: str,
    group_name: str,
    day: dt.date,
    symbols: list[str],
    agent_id: str,
    *,
    dry_run: bool = True,
) -> bool:
    """Patch any holes in ``group_name``'s declared raw layers over ``symbols × {day}`` before the compute —
    the #74 source-data dependency step. Returns True iff the source is present after the call (no layer left
    skipped because its ingest lock was held). dry_run reports holes without fetching or a DB lock.

    A within-day materialize REQUIRES the source first (else it computes off a partial tape and a parity
    'mismatch' would be a missing-download artifact, not a code bug). The caller asserts the return before
    materializing under ``dry_run=False``."""
    layers = required_layers_for_groups([group_name])
    report = ensure_inputs(
        raw_root,
        layers,
        symbols,
        [day],
        agent_id=agent_id,
        fetcher=default_fetcher(raw_root),
        dry_run=dry_run,
    )
    if not report.all_present:
        logger.warning(
            "ensure_window_inputs: group=%s day=%s left layers skipped (lock held): %s",
            group_name,
            day.isoformat(),
            [layer.value for layer in report.skipped_locked],
        )
    return report.all_present


def materialize_settled_window(
    feature_root: str,
    raw_root: str,
    group_name: str,
    day: dt.date,
    symbols: list[str],
    *,
    ensure_inputs_first: bool = False,
    agent_id: str = "wdpc-materialize",
    dry_run: bool = True,
) -> int:
    """Materialize the ``source=backfill`` side of ``group_name`` for ``day`` over the ``symbols`` sample
    so :func:`within_day_parity.compare_window` has a backfill side to compare INTRADAY.

    Routes to the correct materialize variant for the group's type (bar-only / tick-enriched / cross-
    sectional) AND its backfill source. For a bar-only group, the source is chosen by whether the
    ``/store/raw`` minute tape is already on disk for the day: a PAST, swept day reads the download-once raw
    bars (:func:`materialize_from_raw_bar_groups`); the CURRENT day — where the raw bars are NOT yet acquired
    (the nightly acquisition runs post-session) — fetches the day's settled minute bars straight from Alpaca
    (:func:`materialize_alpaca_bar_groups`), the live-intraday source. Both produce schema-identical bars, so
    a within-day match holds either way. Optionally runs :func:`ensure_window_inputs` first to patch raw holes
    (the #74 step; raw-source path only). Returns the number of symbols materialized; 0 when ``dry_run``
    (nothing written) or when the group needs the full universe (cross-sectional — caller falls back to the
    swept-day backfill).

    ``dry_run`` (default) writes NOTHING — it logs what WOULD be materialized so the monitor's live-intraday
    wiring is exercisable offline; the live activation (real raw read + store write) is the gated step the
    monitor opts into with ``materialize_backfill=True`` under a non-dry run."""
    if not symbols:
        logger.info(
            "materialize_settled_window: no symbols for group=%s day=%s — nothing to do",
            group_name,
            day.isoformat(),
        )
        return 0

    if _group_needs_full_universe(group_name):
        logger.info(
            "materialize_settled_window: group=%s is cross-sectional (full-universe reduce) — NOT sample-"
            "materializable intraday; the monitor falls back to the swept-day backfill",
            group_name,
        )
        return 0

    layers = REGISTRY.get_group(group_name).required_raw_layers()
    needs_ticks = RawLayer.TRADES in layers or RawLayer.QUOTES in layers
    # Bar-only groups on the CURRENT (intraday) day source from Alpaca because the raw bar tape is not yet on
    # disk; tick groups always read raw (their tape is acquired, and they certify on past swept days).
    use_alpaca = not needs_ticks and not _raw_bars_present(raw_root, day, symbols)

    if ensure_inputs_first and not use_alpaca:
        present = ensure_window_inputs(raw_root, group_name, day, symbols, agent_id, dry_run=dry_run)
        if not present and not dry_run:
            raise RuntimeError(
                f"materialize_settled_window: source not present for group={group_name} day={day} "
                "(ensure_inputs left a layer locked) — cannot materialize a complete backfill side"
            )

    day_iso = day.isoformat()
    source = "alpaca" if use_alpaca else "raw"
    if dry_run:
        logger.info(
            "DRY-RUN materialize_settled_window: would materialize backfill group=%s day=%s over %d symbols "
            "from %s (source=%s, no store write)",
            group_name,
            day_iso,
            len(symbols),
            raw_root,
            source,
        )
        return 0

    if needs_ticks:
        count = materialize_from_raw_groups(feature_root, raw_root, day_iso, symbols, [group_name])
    elif use_alpaca:
        count = materialize_alpaca_bar_groups(feature_root, day_iso, symbols, [group_name])
    else:
        count = materialize_from_raw_bar_groups(feature_root, raw_root, day_iso, symbols, [group_name])
    logger.info(
        "materialize_settled_window: materialized backfill group=%s day=%s over %d symbols (ticks=%s "
        "source=%s)",
        group_name,
        day_iso,
        count,
        needs_ticks,
        source,
    )
    return count
