"""LIVE feature-store glimpse GRID — DATE rows x FEATURE-GROUP columns, two encodings per cell.

This is the "immediate glimpse into our current features" surface: at one glance, what features exist,
how covered, how fresh, and their trust — the live state of the feature store.

DEFAULT VIEW is a grid:
  * ROWS = DATES, most-recent at TOP (Today, Yesterday, ... back ``days``), each row a captured session.
  * COLUMNS = FEATURE GROUPS (the ~63 registry groups), each EXPANDABLE on click to its individual
    features, plus a "Total" summary column (the whole-store coverage for that date).
  * EACH (date x group-or-feature) CELL carries TWO independent visual encodings (Ben's two annotations):
      1. DARKNESS / opacity = PROPORTION OF COVERAGE = the fraction of the captured universe that has this
         feature-group on this date (``coverage = n_symbols_that_day / universe_size``). Darker = more
         tickers covered; absent = blank. This is the #221 coverage-VOLUME heat, normalized to the whole
         universe (not the group's own peak) so a thin order-flow group reads honestly thin against a
         full-universe bar group.
      2. COLOR / hue = TRUST STATUS of the feature(s) in the cell (green=trusted / amber=pending /
         red=divergent / grey=ungraded), pulled from the ``feature_trust`` table (the same source the
         #221/#223 grid reads). A cell thus shows coverage (darkness) AND trust (hue) together.

DRILL-DOWN (Ben's "one box per ticker and date"): clicking a (date x group) cell opens a TICKER x DATE
grid for THAT group — one tiny box per ticker, shaded by that ticker's per-date presence/freshness. Lazy
(only fetched on drill; ranked most-covered first; paginated) — see ``build_ticker_drill``.

Read-side ONLY, and WINDOWED: unlike the #221 grid (which reads every group's whole multi-year backfill
history), the glimpse finds the store anchor from DIRECTORY NAMES (no parquet) and then reads symbol COUNTS
only for the dates IN the grid window — so a 30-row grid pays ≤30 dates/source/group, not the full history,
keeping a cold build cheap and a cached refresh instant. It REUSES ``feature_grid``'s bounded per-partition
symbol reads (``_read_n_symbols`` / ``_read_symbols`` — evenly-spaced file sampling) and ``trust_by_feature``
(the ``feature_trust`` read) — NO new heavy store I/O and NO new third-party import (the import closure stays
exactly what the #234 dep-guard already validates). NO schema/format/fingerprint change.
"""

from __future__ import annotations

import datetime as dt
import os
import time
from pathlib import Path

from feature_grid import (
    STORE_ROOT,
    _catalog_by_group,
    _group_version,
    _read_n_symbols,
    _read_symbols,
    trust_by_feature,
)

# How many most-recent captured DATES (grid rows) to surface by default, and the hard cap. Weekdays land,
# so ~30 sessions ≈ 6 calendar weeks. The cap bounds the render + keeps a full refresh cheap.
GLIMPSE_DEFAULT_DAYS = 30
GLIMPSE_MAX_DAYS = 90

# The captured-universe denominator for the coverage DARKNESS (fraction-of-universe). The live capture
# universe is ~7.3k symbols (the #227 universe-coverage available filtered set); a cell's darkness is
# n_symbols_that_day / UNIVERSE_SIZE. Env-overridable so a future universe resize is a config change, not
# a code edit; floored against the observed max so darkness never exceeds 1.0 if the store ever exceeds it.
UNIVERSE_SIZE = int(os.environ.get("GLIMPSE_UNIVERSE_SIZE", "7318"))

# Trust lifecycle_state -> the cell HUE class the page paints. The grid's per-feature lifecycle_state is one
# of UNGRADED / PENDING / VALIDATED / DIVERGENT / RETIRED (feature_grid documents the five); we collapse to
# four legible hues. VALIDATED == binary-trusted (green); PENDING == accruing clean days (amber); DIVERGENT
# == open parity defect (red); everything else == ungraded (grey).
TRUST_HUE = {
    "VALIDATED": "trusted",
    "PENDING": "pending",
    "DIVERGENT": "divergent",
    "UNGRADED": "ungraded",
    "RETIRED": "ungraded",
}

# Drill defaults: how many ticker boxes to return per page (the universe is ~7.3k, far too many to paint at
# once), ranked most-covered first.
DRILL_DEFAULT_LIMIT = 500
DRILL_MAX_LIMIT = 2000


def _trust_hue(lifecycle_state: str) -> str:
    """The cell hue class for a single feature's lifecycle_state (defaults to ungraded/grey)."""
    return TRUST_HUE.get(lifecycle_state, "ungraded")


def _aggregate_hue(states: list[str]) -> str:
    """Reduce a group's per-feature trust states to ONE cell hue — worst-actionable-first so risk shows
    first (mirrors feature_grid._aggregate_trust's badge logic): any DIVERGENT -> divergent; else any
    VALIDATED -> trusted (the group has earned-trust features); else any PENDING -> pending; else ungraded.
    """
    if not states:
        return "ungraded"
    if any(state == "DIVERGENT" for state in states):
        return "divergent"
    if any(state == "VALIDATED" for state in states):
        return "trusted"
    if any(state == "PENDING" for state in states):
        return "pending"
    return "ungraded"


def _coverage_fraction(n_symbols: int, universe_size: int) -> float:
    """Fraction of the captured universe present on a date (the cell DARKNESS), clamped to [0, 1]."""
    if universe_size <= 0 or n_symbols <= 0:
        return 0.0
    return min(1.0, round(n_symbols / universe_size, 4))


def _day_count(per_date: dict[str, dict[str, int]], date_iso: str) -> int:
    """Best per-date symbol count for a group: max over sources (stream vs backfill) on ``date_iso``.

    Either source covering a symbol on a date means the feature EXISTS for it that date, so the cell
    darkness should reflect the wider of the two — a fully-backfilled day reads full even if the live
    stream that day was thin, and vice versa."""
    stream_n = per_date.get("stream", {}).get(date_iso, 0)
    backfill_n = per_date.get("backfill", {}).get(date_iso, 0)
    return max(stream_n, backfill_n)


def _partition_dates(root: str, group: str, version: str, source: str) -> list[str]:
    """The date-partition names under a (group, version, source), from DIRECTORY NAMES only — no parquet
    read. Used to find the store anchor and to know WHICH dates exist before paying any symbol-count read."""
    base = Path(root) / f"group={group}" / f"v={version}" / f"source={source}"
    if not base.exists():
        return []
    return [date_dir.name.removeprefix("date=") for date_dir in base.glob("date=*")]


def _store_anchor_date(root: str, groups: list[str], versions: dict[str, str | None]) -> dt.date | None:
    """The most recent date present anywhere across all groups/sources — from directory names only (no
    parquet). The grid row axis ends here; everything else reads only the window back from it."""
    latest: dt.date | None = None
    for group in groups:
        version = versions.get(group)
        if version is None:
            continue
        for source in ("stream", "backfill"):
            for date_iso in _partition_dates(root, group, version, source):
                day = dt.date.fromisoformat(date_iso)
                if latest is None or day > latest:
                    latest = day
    return latest


def _windowed_counts(root: str, group: str, version: str, window: set[str]) -> dict[str, dict[str, int]]:
    """Per-source {date_iso: n_symbols} for a group, restricted to the ``window`` dates — so a 30-row grid
    reads at most 30 dates/source, NOT the group's whole multi-year backfill history. Each present
    in-window date pays one bounded per-partition symbol read (the same evenly-spaced file sampling the grid
    uses)."""
    out: dict[str, dict[str, int]] = {}
    for source in ("stream", "backfill"):
        per_date: dict[str, int] = {}
        for date_iso in _partition_dates(root, group, version, source):
            if date_iso in window:
                per_date[date_iso] = _read_n_symbols(root, group, version, source, date_iso)
        out[source] = per_date
    return out


def build_store_glimpse(
    root: str = STORE_ROOT,
    days: int = GLIMPSE_DEFAULT_DAYS,
    universe_size: int = UNIVERSE_SIZE,
) -> dict[str, object]:
    """The live feature-store glimpse: DATE rows x FEATURE-GROUP columns, each cell carrying coverage
    fraction (darkness) + trust hue (color), plus a per-date Total column and an expandable per-feature
    breakdown (precomputed cheaply, the page reveals it on click).

    Shape (see docs/STORE_GLIMPSE.md):
      {generated_at, store_root, anchor_date, days, universe_size,
       summary: {n_groups, n_features, n_dates, n_trusted, trusted_pct,
                 trust_counts: {trusted, pending, divergent, ungraded}},
       groups: [{group, version, n_features, trust_hue, trust_counts,
                 features: [{feature, trust_hue, lifecycle_state}]}],   # column header + per-feature trust
       dates: ["2026-06-20", ...],                                       # rows, newest first
       cells: {date: {group: {coverage, n_symbols, hue}, ... "__total__": {coverage, n_symbols, hue}}}}

    Per-feature coverage equals its group's coverage (features in a group are co-captured per (group,date)
    partition), so the per-feature darkness is the group's; only the HUE differs per feature. This keeps the
    expansion free of extra store I/O.
    """
    catalog_by_group = _catalog_by_group()
    trust = trust_by_feature()
    groups = sorted(catalog_by_group)
    span = max(1, min(days, GLIMPSE_MAX_DAYS))

    versions = {group: _group_version(group) for group in groups}

    # Find the store anchor from DIRECTORY NAMES only (no parquet), then read symbol COUNTS for the window
    # dates ONLY — so the grid never pays the full multi-year backfill-history read the #221 grid does.
    anchor = _store_anchor_date(root, groups, versions)
    if anchor is None:
        return {
            "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "store_root": root,
            "anchor_date": None,
            "days": span,
            "universe_size": universe_size,
            "summary": {
                "n_groups": 0,
                "n_features": 0,
                "n_dates": 0,
                "n_trusted": 0,
                "trusted_pct": 0.0,
                "trust_counts": {"trusted": 0, "pending": 0, "divergent": 0, "ungraded": 0},
            },
            "groups": [],
            "dates": [],
            "cells": {},
        }

    dates = [(anchor - dt.timedelta(days=offset)).isoformat() for offset in range(span)]
    window = set(dates)

    # Per-group per-source {date_iso: n_symbols}, restricted to the window (≤ span dates/source per group).
    per_group_counts: dict[str, dict[str, dict[str, int]]] = {}
    for group in groups:
        version = versions[group]
        if version is None:
            continue
        per_group_counts[group] = _windowed_counts(root, group, version, window)

    group_columns: list[dict[str, object]] = []
    total_features = 0
    total_trusted = 0
    overall_counts = {"trusted": 0, "pending": 0, "divergent": 0, "ungraded": 0}

    for group in groups:
        features = catalog_by_group[group]
        feature_rows: list[dict[str, object]] = []
        states: list[str] = []
        counts = {"trusted": 0, "pending": 0, "divergent": 0, "ungraded": 0}
        for record in features:
            name = str(record["feature"])
            lifecycle = trust[name].lifecycle_state if name in trust else "UNGRADED"
            hue = _trust_hue(lifecycle)
            states.append(lifecycle)
            counts[hue] += 1
            overall_counts[hue] += 1
            if lifecycle == "VALIDATED":
                total_trusted += 1
            feature_rows.append({"feature": name, "trust_hue": hue, "lifecycle_state": lifecycle})
        total_features += len(features)
        version = versions[group] or "?"
        group_columns.append(
            {
                "group": group,
                "version": version,
                "n_features": len(features),
                "trust_hue": _aggregate_hue(states),
                "trust_counts": counts,
                "features": feature_rows,
            }
        )

    # The Total column's HUE summarizes the WHOLE store's trust (worst-first reduction over every group's
    # aggregate hue) so the rightmost column reads the store's overall trust at a glance. It is the SAME for
    # every date row (trust is per-feature, not per-date), computed once.
    _hue_to_state = {
        "trusted": "VALIDATED",
        "pending": "PENDING",
        "divergent": "DIVERGENT",
        "ungraded": "UNGRADED",
    }
    group_hue_by_name = {str(col["group"]): str(col["trust_hue"]) for col in group_columns}
    total_hue = _aggregate_hue([_hue_to_state[hue] for hue in group_hue_by_name.values()])

    cells: dict[str, dict[str, dict[str, object]]] = {}
    for date_iso in dates:
        row: dict[str, dict[str, object]] = {}
        total_symbols = 0
        for group in groups:
            counts_for_group = per_group_counts.get(group)
            n_symbols = _day_count(counts_for_group, date_iso) if counts_for_group is not None else 0
            if n_symbols > total_symbols:
                total_symbols = n_symbols
            row[group] = {
                "coverage": _coverage_fraction(n_symbols, universe_size),
                "n_symbols": n_symbols,
                "hue": group_hue_by_name[group],
            }
        row["__total__"] = {
            "coverage": _coverage_fraction(total_symbols, universe_size),
            "n_symbols": total_symbols,
            "hue": total_hue,
        }
        cells[date_iso] = row

    return {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "store_root": root,
        "anchor_date": anchor.isoformat(),
        "days": span,
        "universe_size": universe_size,
        "summary": {
            "n_groups": len(group_columns),
            "n_features": total_features,
            "n_dates": len(dates),
            "n_trusted": total_trusted,
            "trusted_pct": round(100.0 * total_trusted / total_features, 1) if total_features else 0.0,
            "trust_counts": overall_counts,
        },
        "groups": group_columns,
        "dates": dates,
        "cells": cells,
    }


def _windowed_symbol_sets(
    root: str, group: str, version: str, source: str, window: set[str]
) -> dict[str, set[str]]:
    """Per-date distinct-symbol SETS for a (group, source), restricted to the ``window`` dates only — the
    drill's per-ticker presence vector. Reads only the requested recent dates (one bounded per-partition
    sampled read each), not the group's whole history."""
    out: dict[str, set[str]] = {}
    for date_iso in _partition_dates(root, group, version, source):
        if date_iso in window:
            symbols = _read_symbols(root, group, version, source, date_iso)
            if symbols:
                out[date_iso] = symbols
    return out


def build_ticker_drill(
    group: str,
    root: str = STORE_ROOT,
    days: int = GLIMPSE_DEFAULT_DAYS,
    universe_size: int = UNIVERSE_SIZE,
    limit: int = DRILL_DEFAULT_LIMIT,
) -> dict[str, object]:
    """The drill-down for one (date x group) cell: a TICKER x DATE presence grid for THAT group.

    One row per ticker (Ben's "one box per ticker and date"), one box per date, shaded by whether that
    ticker has the group's features on that date and from which source (both / stream / backfill / absent).
    Lazy by construction — only called on a cell click — and PAGINATED: tickers are ranked by how many of
    the window's dates they are present on (most-covered first), and at most ``limit`` are returned (the
    universe is ~7.3k). The full window's date columns + the per-ticker presence vector come from one
    bounded per-partition symbol read per (source, date) — ``_per_date_symbol_sets`` (the same evenly-spaced
    file sampling the grid/timeline pay), so a 7k-file stream partition is ~12 reads, not 7k.
    """
    if group not in _catalog_by_group():
        raise KeyError(group)
    version = _group_version(group)
    if version is None:
        raise KeyError(group)
    span = max(1, min(days, GLIMPSE_MAX_DAYS))
    capped_limit = max(1, min(limit, DRILL_MAX_LIMIT))

    # Anchor from directory names only (no parquet); the drill only reads the window dates' symbol SETS.
    all_dates = _partition_dates(root, group, version, "stream") + _partition_dates(
        root, group, version, "backfill"
    )
    anchor = max((dt.date.fromisoformat(d) for d in all_dates), default=None)
    if anchor is None:
        return {
            "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "group": group,
            "version": version,
            "anchor_date": None,
            "days": span,
            "dates": [],
            "n_tickers": 0,
            "limit": capped_limit,
            "tickers": [],
        }

    dates = [(anchor - dt.timedelta(days=offset)).isoformat() for offset in range(span)]
    window = set(dates)

    stream_by_date = _windowed_symbol_sets(root, group, version, "stream", window)
    backfill_by_date = _windowed_symbol_sets(root, group, version, "backfill", window)

    all_symbols: set[str] = set()
    for symbols in stream_by_date.values():
        all_symbols |= symbols
    for symbols in backfill_by_date.values():
        all_symbols |= symbols

    ticker_rows: list[dict[str, object]] = []
    for symbol in all_symbols:
        boxes: list[dict[str, object]] = []
        n_present = 0
        for date_iso in dates:
            in_stream = symbol in stream_by_date.get(date_iso, set())
            in_backfill = symbol in backfill_by_date.get(date_iso, set())
            if in_stream and in_backfill:
                provenance = "both"
            elif in_stream:
                provenance = "stream"
            elif in_backfill:
                provenance = "backfill"
            else:
                provenance = "absent"
            if provenance != "absent":
                n_present += 1
            boxes.append({"date": date_iso, "provenance": provenance})
        ticker_rows.append({"symbol": symbol, "n_present": n_present, "boxes": boxes})

    # Rank most-covered first (Ben wants the live, well-covered names at a glance), then by symbol; page it.
    ticker_rows.sort(key=lambda row: (-int(row["n_present"]), str(row["symbol"])))
    n_tickers = len(ticker_rows)

    return {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "group": group,
        "version": version,
        "anchor_date": anchor.isoformat(),
        "days": span,
        "dates": dates,
        "n_tickers": n_tickers,
        "limit": capped_limit,
        "universe_size": universe_size,
        "tickers": ticker_rows[:capped_limit],
    }


class StoreGlimpseCache:
    """TTL cache mirroring ``feature_grid.GridCache`` / ``UniverseCoverageCache``. The glimpse is one cheap
    read-side pass (reuses the grid's gathered counts), so a 60s TTL keeps the LIVE auto-refresh instant
    while staying fresh. Glimpse keyed by ``days``; drills keyed by (group, days, limit)."""

    def __init__(self, ttl: float = 60.0) -> None:
        self.ttl = ttl
        self._glimpse: dict[int, tuple[float, dict[str, object]]] = {}
        self._drills: dict[tuple[str, int, int], tuple[float, dict[str, object]]] = {}

    def glimpse(
        self, root: str = STORE_ROOT, days: int = GLIMPSE_DEFAULT_DAYS, force: bool = False
    ) -> dict[str, object]:
        now = time.monotonic()
        cached = self._glimpse.get(days)
        if not force and cached is not None and (now - cached[0]) <= self.ttl:
            return cached[1]
        view = build_store_glimpse(root, days=days)
        self._glimpse[days] = (now, view)
        return view

    def drill(
        self,
        group: str,
        root: str = STORE_ROOT,
        days: int = GLIMPSE_DEFAULT_DAYS,
        limit: int = DRILL_DEFAULT_LIMIT,
        force: bool = False,
    ) -> dict[str, object]:
        now = time.monotonic()
        key = (group, days, limit)
        cached = self._drills.get(key)
        if not force and cached is not None and (now - cached[0]) <= self.ttl:
            return cached[1]
        view = build_ticker_drill(group, root, days=days, limit=limit)
        self._drills[key] = (now, view)
        return view


CACHE = StoreGlimpseCache()
