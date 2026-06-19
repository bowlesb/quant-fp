"""RAW-TAPE coverage aggregation for the dashboard — what raw Alpaca history exists, per layer.

The feature-grid surfaces (``feature_grid.py``) answer "for each FEATURE group, how much data / is it
trusted". This module answers the layer BELOW that: for each RAW tape we acquire from Alpaca — minute
``bars``, tick ``trades``, ``quotes`` — what is on disk to invent + backfill features on, WITHOUT
re-downloading. It is the read-side legibility surface for the deep-raw-history priority (a deep+broad raw
tape is the substrate the modellers invent on); as the deep backfill fills, this same surface is the live
progress tracker (quotes depth climbing, trades breadth widening).

SOURCE OF TRUTH — the RAW MANIFESTS, not a store scan. ``quantlib.data.raw_store`` records one
(tier, symbol, date, rows, bytes) cell per acquired symbol-day in ``<store>/raw/_manifest_<tier>.d/`` (append-
only parts + a legacy single file). That manifest IS the authoritative coverage record, so this is a cheap
read (~2s cold for the largest tier, dominated by part count) — no heavy partition-tree walk, no parquet
bodies read, no schema change.

A manifest cell with ``rows == 0`` is a "no-data / settled-empty" marker (an illiquid/delisted symbol-day, or
a not-yet-settled recent fetch — see ``raw_store.resumable_done_keys``), NOT a real tape. So everything here is
computed over REAL cells (``rows > 0``): a day "has" a layer only where a real tape landed, and the
symbols-per-day breadth counts only symbols with a real tape that day. The honest answer for "what can I
invent on".

Two dimensions, per layer:
  * DEPTH    — earliest date, latest date, span (calendar days), and a per-date timeline (which dates are
    present, so gaps read off the row at a glance).
  * BREADTH  — distinct symbols with a real tape per date over time (so the "trades thin ~1.9k/day vs bars
    ~6.3k" and "quotes only 3mo" gaps are obvious).
"""

from __future__ import annotations

import datetime as dt
import os
import time

import polars as pl

from quantlib.data.raw_store import load_manifest

STORE_ROOT = os.environ.get("STORE_ROOT", "/store")

# The raw layers we acquire from Alpaca, in acquisition-cost / feature-richness order. A tier with no
# manifest on disk is simply reported absent (empty), so listing one not yet acquired is harmless.
RAW_TIERS: list[tuple[str, str]] = [
    ("bars", "minute bars"),
    ("trades", "tick trades"),
    ("quotes", "tick quotes"),
]

# The timeline can span 18 months of daily cells; the per-date arrays stay light (one int per date), but cap
# the default so a page load is snappy. ``days`` (full history) is opt-in via ``?days=`` / the page control.
TIMELINE_DEFAULT_DAYS = 90


def _real_cells(manifest: pl.DataFrame) -> pl.DataFrame:
    """Per (symbol, date) keep the MAX recorded rows (a later real fetch supersedes an earlier 0-row poison —
    same rule as ``raw_store.resumable_done_keys``), then keep only cells with a REAL tape (rows > 0)."""
    if manifest.height == 0:
        return manifest
    per_key = manifest.group_by(["symbol", "date"]).agg(pl.col("rows").max().alias("rows"))
    return per_key.filter(pl.col("rows") > 0)


def _tier_coverage(root: str, tier: str) -> dict[str, object]:
    """DEPTH + BREADTH for one raw tier, read straight from its manifest. Empty (no manifest / no real tape)
    is reported as a present-but-empty layer, not an error — a tier we have not acquired yet."""
    manifest = load_manifest(root, tier)
    real = _real_cells(manifest)
    if real.height == 0:
        return {
            "tier": tier,
            "earliest": None,
            "latest": None,
            "span_days": 0,
            "n_dates": 0,
            "n_symbols": 0,
            "n_cells": 0,
            "mean_symbols_per_day": 0.0,
            "median_symbols_per_day": 0.0,
            "newest_symbols_per_day": 0,
            "dates": [],
        }
    by_date = (
        real.group_by("date")
        .agg(pl.col("symbol").n_unique().alias("n_symbols"), pl.col("rows").sum().alias("rows"))
        .sort("date")
    )
    date_list = by_date["date"].to_list()
    sym_per_day = by_date["n_symbols"].to_list()
    rows_per_day = by_date["rows"].to_list()
    earliest = date_list[0]
    latest = date_list[-1]
    span_days = (dt.date.fromisoformat(latest) - dt.date.fromisoformat(earliest)).days + 1
    return {
        "tier": tier,
        "earliest": earliest,
        "latest": latest,
        "span_days": span_days,
        "n_dates": len(date_list),
        "n_symbols": real["symbol"].n_unique(),
        "n_cells": real.height,
        "mean_symbols_per_day": round(float(by_date["n_symbols"].mean()), 1),
        "median_symbols_per_day": round(float(by_date["n_symbols"].median()), 1),
        "newest_symbols_per_day": sym_per_day[-1],
        "dates": [
            {"date": date_iso, "n_symbols": n_sym, "rows": rows}
            for date_iso, n_sym, rows in zip(date_list, sym_per_day, rows_per_day)
        ],
    }


def _clip_recent(tier: dict[str, object], days: int | None) -> dict[str, object]:
    """Trim a tier's per-date timeline to the most-recent ``days`` calendar days (ending at its latest date)
    for the default snappy view. ``days is None`` keeps the full history. DEPTH/BREADTH summary stats stay
    computed over the FULL tape (the headline depth must not shrink with the window) — only the ``dates``
    array is clipped, with ``shown_from`` recording the clip so the caller can label it."""
    dates = tier["dates"]
    if days is None or not dates or len(dates) <= 0:
        return {**tier, "shown_from": dates[0]["date"] if dates else None, "n_dates_shown": len(dates)}
    latest = dt.date.fromisoformat(tier["latest"])  # type: ignore[arg-type]
    cutoff = (latest - dt.timedelta(days=days - 1)).isoformat()
    clipped = [cell for cell in dates if cell["date"] >= cutoff]
    return {**tier, "dates": clipped, "shown_from": cutoff, "n_dates_shown": len(clipped)}


def build_raw_coverage(
    root: str = STORE_ROOT, days: int | None = TIMELINE_DEFAULT_DAYS
) -> dict[str, object]:
    """The raw-tape coverage surface: per raw layer (bars / trades / quotes) DEPTH (earliest/latest/span) +
    BREADTH (distinct symbols-per-day over time) + a per-date timeline (which dates present, gaps visible).

    Sourced from the raw manifests only (cheap read, no store scan). ``days`` clips each layer's per-date
    timeline to the most-recent N calendar days for a snappy default; ``days=0`` / None returns the full
    history. Summary depth/breadth stats are always over the full tape.

    Shape (see docs/RAW_TAPE_COVERAGE.md):
      {generated_at, store_root, days, anchor_date, span_earliest, span_latest,
       layers: [{tier, label, earliest, latest, span_days, n_dates, n_symbols, n_cells,
                 mean_symbols_per_day, median_symbols_per_day, newest_symbols_per_day,
                 shown_from, n_dates_shown, dates: [{date, n_symbols, rows}]}]}
    """
    window = None if not days or days <= 0 else days
    layers: list[dict[str, object]] = []
    span_earliest: str | None = None
    span_latest: str | None = None
    for tier, label in RAW_TIERS:
        coverage = _tier_coverage(root, tier)
        if coverage["earliest"] is not None:
            earliest = coverage["earliest"]
            latest = coverage["latest"]
            if span_earliest is None or earliest < span_earliest:  # type: ignore[operator]
                span_earliest = earliest  # type: ignore[assignment]
            if span_latest is None or latest > span_latest:  # type: ignore[operator]
                span_latest = latest  # type: ignore[assignment]
        layers.append({"label": label, **_clip_recent(coverage, window)})
    return {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "store_root": root,
        "days": window,
        "anchor_date": span_latest,
        "span_earliest": span_earliest,
        "span_latest": span_latest,
        "layers": layers,
    }


class RawCoverageCache:
    """Tiny TTL cache mirroring ``feature_grid.GridCache`` — the manifest read is ~2s cold for the largest
    tier, so a 60s TTL makes a busy refresh instant while staying fresh enough for a coverage surface that
    changes only on a backfill/daily-acquire write."""

    def __init__(self, ttl: float = 60.0) -> None:
        self.ttl = ttl
        self._by_days: dict[int, tuple[float, dict[str, object]]] = {}

    def coverage(
        self, root: str, days: int = TIMELINE_DEFAULT_DAYS, force: bool = False
    ) -> dict[str, object]:
        now = time.monotonic()
        cached = self._by_days.get(days)
        if not force and cached is not None and (now - cached[0]) <= self.ttl:
            return cached[1]
        view = build_raw_coverage(root, days)
        self._by_days[days] = (now, view)
        return view


CACHE = RawCoverageCache()
