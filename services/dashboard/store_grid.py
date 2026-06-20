"""TICKER × DATE feature-store coverage MATRIX — the always-warm "glimpse into the feature store" grid.

This is the HEIC tiny-boxes view: ROWS = dates (most-recent at top, ~18 months back), COLUMNS = tickers
(the ~7k captured universe), each CELL the PROPORTION of the feature store present for that ticker on that
date. Darker = more complete. It replaces the old group×date glimpse with the axis Ben asked for
(ticker×date), and is built by a permanent background worker (``store_grid_worker``) — never on a request.

WHAT A CELL MEANS
  coverage = (# feature-GROUPS present for this ticker on this date) / N_REGISTRY_GROUPS.

  The denominator is the TOTAL registry group count (not "groups that have any data that date"), so the
  18-month depth gradient reads truthfully: far-back dates where only the calendar groups backfill read
  correctly FAINT (a few of N groups), recent fully-captured days read DARK. A ticker present in every
  group on a date reads ~1.0. This is "how complete that ticker's features are that day" against everything
  the store could hold. Coverage is quantized to a byte (0..255) for a compact packed matrix.

BINARY TRUST (Ben: trusted vs untrusted, nothing else)
  Trust is a per-FEATURE property (``feature_trust.trust_state = 'TRUSTED'`` — the binary system of record,
  ``feature_grid.trusted_feature_names``). We project it onto the ticker×date cell as ONE bit: a cell is
  "all-trusted" iff EVERY group present for that ticker×date is fully-trusted (all its features trusted).
  Otherwise "some-untrusted". No PENDING/DIVERGENT/UNGRADED — those collapse to the single untrusted state.

COST / WHY A MATRIX BUILD IS CHEAP ENOUGH FOR A LOOP
  The whole store is ~2.4k (group, source, date) partitions, each a handful of files; one bounded
  evenly-sampled symbol-set read per partition (``feature_grid._read_symbols``) gives, per (group, date),
  the set of tickers present. The matrix is then pure set membership — no extra store I/O. A full rebuild
  measures ~3-4min (dominated by the deep calendar groups' many partitions), which the background worker pays
  on a loop while always serving the last-good blob — never on a request.

Read-side ONLY. Reuses ``feature_grid``'s bounded partition reads + the registry catalog + the trust read;
NO new heavy I/O, NO schema/format/fingerprint change to the feature store.
"""

from __future__ import annotations

import datetime as dt
import os
from dataclasses import dataclass
from pathlib import Path

from feature_grid import (
    STORE_ROOT,
    _catalog_by_group,
    _group_version,
    _read_symbols,
    trusted_feature_names,
)

# Row axis: how many calendar days back from the store anchor the matrix spans. ~18 months ≈ 548 calendar
# days. We keep only WEEKDAYS as rows (a local calendar proxy — no Alpaca calendar / network / secrets in the
# worker), which over 18 months is ~378 trading-date rows; weekend rows would always be blank, so dropping
# them keeps the matrix tight (~30% fewer rows) without losing any captured data. Env-overridable so a
# wider/narrower window is a config change, not a code edit.
GRID_LOOKBACK_DAYS = int(os.environ.get("STORE_GRID_LOOKBACK_DAYS", "548"))

# Coverage is quantized to a single byte for the packed matrix (0 = absent, 255 = present in every group).
COVERAGE_MAX_BYTE = 255

# Drill page size: a (ticker × date) cell click opens that ticker's per-group presence on that date; the
# whole-grid drill lists the most-covered tickers. The universe is ~7k, far too many to ship per cell.
DRILL_DEFAULT_LIMIT = 500
DRILL_MAX_LIMIT = 2000


def _partition_dates(root: str, group: str, version: str, source: str) -> list[str]:
    """Date-partition names under a (group, version, source) from DIRECTORY NAMES only — no parquet read.
    Used to find the store anchor and to know which dates exist before paying any symbol-set read."""
    base = Path(root) / f"group={group}" / f"v={version}" / f"source={source}"
    if not base.exists():
        return []
    return [date_dir.name.removeprefix("date=") for date_dir in base.glob("date=*")]


def _store_anchor_date(root: str, group_versions: dict[str, str]) -> dt.date | None:
    """The most recent date present anywhere across all groups/sources — from directory names only. The grid
    row axis ends here; the window reads back ``GRID_LOOKBACK_DAYS`` from it."""
    latest: dt.date | None = None
    for group, version in group_versions.items():
        for source in ("stream", "backfill"):
            for date_iso in _partition_dates(root, group, version, source):
                day = dt.date.fromisoformat(date_iso)
                if latest is None or day > latest:
                    latest = day
    return latest


def _window_dates(anchor: dt.date, lookback_days: int) -> list[str]:
    """The grid's date ROWS, most-recent FIRST: ``anchor`` back ``lookback_days`` calendar days, WEEKDAYS
    only. Weekend rows are always blank (no capture), so dropping them keeps the matrix tight. A weekday with
    no data still renders (blank) — honest, since it WAS a trading day."""
    span = max(1, lookback_days)
    floor = anchor - dt.timedelta(days=span - 1)
    dates: list[str] = []
    day = anchor
    while day >= floor:
        if day.weekday() < 5:
            dates.append(day.isoformat())
        day -= dt.timedelta(days=1)
    return dates


def _group_symbols_in_window(root: str, group: str, version: str, window: set[str]) -> dict[str, set[str]]:
    """{date_iso: set(tickers)} for a group over the window dates only, unioned across stream + backfill.

    Either source covering a ticker on a date means the feature group EXISTS for it that date, so we union
    the two. Only in-window dates pay a (bounded, evenly-sampled) symbol-set read; the rest of the group's
    history is skipped entirely."""
    by_date: dict[str, set[str]] = {}
    for source in ("stream", "backfill"):
        for date_iso in _partition_dates(root, group, version, source):
            if date_iso not in window:
                continue
            symbols = _read_symbols(root, group, version, source, date_iso)
            if symbols:
                by_date.setdefault(date_iso, set()).update(symbols)
    return by_date


def _fully_trusted_groups(
    catalog_by_group: dict[str, list[dict[str, object]]], trusted: set[str]
) -> set[str]:
    """The groups whose features are ALL trusted — a group counts as trusted in a cell only if every one of
    its features has earned binary trust. A group with any untrusted feature taints any cell it covers."""
    fully: set[str] = set()
    for group, features in catalog_by_group.items():
        if features and all(str(record["feature"]) in trusted for record in features):
            fully.add(group)
    return fully


@dataclass
class WindowData:
    """One full-store read pass over the grid window — gathered ONCE, then both the matrix and every ticker
    drill are derived from it purely in-memory (no re-reads). This is what makes pre-warming N drills cheap:
    the store is touched once per worker loop, not once per drill.

    ``group_symbols`` is {group: {date_iso: set(tickers)}} (stream∪backfill per in-window date);
    ``fully_trusted_groups`` is the set of groups all of whose features have earned binary trust."""

    anchor: dt.date
    dates: list[str]
    group_versions: dict[str, str]
    group_symbols: dict[str, dict[str, set[str]]]
    fully_trusted_groups: set[str]

    @property
    def n_groups(self) -> int:
        return len(self.group_versions)


def gather_window(root: str = STORE_ROOT, lookback_days: int = GRID_LOOKBACK_DAYS) -> WindowData | None:
    """The single store-reading pass: catalog + versions + trust, the anchor, and per-group windowed symbol
    sets. ``None`` if the store is empty / no registry groups (the build returns its empty payload). One
    bounded symbol read per in-window partition — the whole cost of a worker loop lives here."""
    catalog_by_group = _catalog_by_group()
    versions: dict[str, str] = {}
    for group in sorted(catalog_by_group):
        version = _group_version(group)
        if version is not None:
            versions[group] = version

    fully_trusted_groups = _fully_trusted_groups(catalog_by_group, trusted_feature_names())
    anchor = _store_anchor_date(root, versions)
    if anchor is None or not versions:
        return None

    dates = _window_dates(anchor, lookback_days)
    window = set(dates)
    group_symbols: dict[str, dict[str, set[str]]] = {}
    for group, version in versions.items():
        group_symbols[group] = _group_symbols_in_window(root, group, version, window)

    return WindowData(
        anchor=anchor,
        dates=dates,
        group_versions=versions,
        group_symbols=group_symbols,
        fully_trusted_groups=fully_trusted_groups,
    )


def build_store_grid(
    root: str = STORE_ROOT,
    lookback_days: int = GRID_LOOKBACK_DAYS,
    window_data: WindowData | None = None,
) -> dict[str, object]:
    """Build the ticker×date coverage + binary-trust matrix.

    Pass a pre-gathered ``window_data`` (from ``gather_window``) to assemble the matrix without re-reading the
    store — the worker gathers once and reuses it for the matrix AND every drill. Without it, gathers itself.

    Output (compact, JSON-serializable — the worker writes this to Redis, the reader serves it as-is):
      {generated_at, store_root, anchor_date, lookback_days, n_groups, n_trusted_groups,
       dates: ["2026-06-20", ...],                         # rows, newest first
       tickers: ["AAPL", ...],                             # columns, default-sorted by total coverage
       coverage: [[byte, ...], ...],                       # rows aligned to dates, cols to tickers (0..255)
       trusted:  [[bit, ...], ...],                        # 1 = every present group fully-trusted, else 0
       coverage_pct: [...],                                # per-ticker mean coverage over present dates (sort key)
       summary: {n_dates, n_tickers, n_groups, n_trusted_groups, mean_coverage_pct}}

    The matrix is dense (every date × every ticker), so the packed byte/bit rows are the compact wire form;
    a ticker absent on a date is simply byte 0. Coverage byte = round(255 * groups_present / n_groups).
    """
    data = window_data if window_data is not None else gather_window(root, lookback_days)
    if data is None:
        return _empty_grid(root, lookback_days)

    n_groups = data.n_groups
    dates = data.dates
    group_symbols = data.group_symbols
    fully_trusted_groups = data.fully_trusted_groups

    # Per (date, ticker): how many groups present, and how many of those are fully-trusted groups.
    all_tickers: set[str] = set()
    for by_date in group_symbols.values():
        for symbols in by_date.values():
            all_tickers |= symbols
    tickers = sorted(all_tickers)
    ticker_index = {symbol: idx for idx, symbol in enumerate(tickers)}
    n_tickers = len(tickers)

    coverage: list[list[int]] = []
    trusted_rows: list[list[int]] = []
    # Per-ticker accumulators for the default column sort (mean coverage over dates the ticker appears).
    ticker_cov_sum = [0.0] * n_tickers
    ticker_present_dates = [0] * n_tickers

    for date_iso in dates:
        present_count = [0] * n_tickers
        untrusted_hit = [0] * n_tickers  # 1 once an untrusted group covers the ticker this date
        for group, by_date in group_symbols.items():
            symbols = by_date.get(date_iso)
            if not symbols:
                continue
            group_is_trusted = group in fully_trusted_groups
            for symbol in symbols:
                idx = ticker_index[symbol]
                present_count[idx] += 1
                if not group_is_trusted:
                    untrusted_hit[idx] = 1

        cov_row = [0] * n_tickers
        trust_row = [0] * n_tickers
        for idx in range(n_tickers):
            count = present_count[idx]
            if count == 0:
                continue
            fraction = count / n_groups
            cov_row[idx] = min(COVERAGE_MAX_BYTE, round(COVERAGE_MAX_BYTE * fraction))
            trust_row[idx] = 0 if untrusted_hit[idx] else 1
            ticker_cov_sum[idx] += fraction
            ticker_present_dates[idx] += 1
        coverage.append(cov_row)
        trusted_rows.append(trust_row)

    coverage_pct = [
        round(100.0 * ticker_cov_sum[idx] / ticker_present_dates[idx], 1)
        if ticker_present_dates[idx]
        else 0.0
        for idx in range(n_tickers)
    ]

    # Default column order: most-covered tickers first (mean coverage over their present dates), then alpha.
    order = sorted(range(n_tickers), key=lambda idx: (-coverage_pct[idx], tickers[idx]))
    tickers_sorted = [tickers[idx] for idx in order]
    coverage_pct_sorted = [coverage_pct[idx] for idx in order]
    coverage_sorted = [[row[idx] for idx in order] for row in coverage]
    trusted_sorted = [[row[idx] for idx in order] for row in trusted_rows]

    mean_cov = round(sum(coverage_pct_sorted) / len(coverage_pct_sorted), 1) if coverage_pct_sorted else 0.0

    return {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "store_root": root,
        "anchor_date": data.anchor.isoformat(),
        "lookback_days": max(1, lookback_days),
        "n_groups": n_groups,
        "n_trusted_groups": len(fully_trusted_groups),
        "dates": dates,
        "tickers": tickers_sorted,
        "coverage": coverage_sorted,
        "trusted": trusted_sorted,
        "coverage_pct": coverage_pct_sorted,
        "summary": {
            "n_dates": len(dates),
            "n_tickers": n_tickers,
            "n_groups": n_groups,
            "n_trusted_groups": len(fully_trusted_groups),
            "mean_coverage_pct": mean_cov,
        },
        "legend": _legend(n_groups),
    }


def _legend(n_groups: int) -> dict[str, str]:
    """The grid's legend text: the coverage darkness scale + a note that store depth is UNEVEN, so the faint
    far-back rows read as honest sparsity (most groups shallow, only the calendar groups deep) and not a bug.
    """
    return {
        "coverage_scale": (
            f"Cell darkness = fraction of the {n_groups} feature-groups present for that ticker on "
            "that date (light = few groups, dark = all groups)."
        ),
        "trust_overlay": (
            "Binary trust: a cell is GREEN only when every feature-group covering it is fully trusted; "
            "anything else is the neutral untrusted shade."
        ),
        "depth_note": (
            "Store depth is UNEVEN — only the calendar groups go back ~18 months; most groups are shallow "
            "(recent weeks/months). Faint far-back rows are honest sparsity, not a bug."
        ),
    }


def _empty_grid(root: str, lookback_days: int) -> dict[str, object]:
    """Valid empty matrix (no store yet / no registry groups). Same shape as a real build, zero rows/cols."""
    return {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "store_root": root,
        "anchor_date": None,
        "lookback_days": max(1, lookback_days),
        "n_groups": 0,
        "n_trusted_groups": 0,
        "dates": [],
        "tickers": [],
        "coverage": [],
        "trusted": [],
        "coverage_pct": [],
        "summary": {
            "n_dates": 0,
            "n_tickers": 0,
            "n_groups": 0,
            "n_trusted_groups": 0,
            "mean_coverage_pct": 0.0,
        },
        "legend": _legend(0),
    }


def build_ticker_drill(
    symbol: str,
    root: str = STORE_ROOT,
    lookback_days: int = GRID_LOOKBACK_DAYS,
    window_data: WindowData | None = None,
) -> dict[str, object]:
    """Drill for one TICKER: its per-(date × group) presence + per-group binary trust — what a cell click in
    that ticker's column opens. One row per date (newest first), one box per group, marked present/absent and
    trusted/untrusted.

    Pass a pre-gathered ``window_data`` (from ``gather_window``) and the drill is pure in-memory set lookups
    over the SAME read pass the matrix used — so the worker pre-warms N drills for the cost of one store read.
    Without it (an un-warmed ticker hitting the route live), it gathers once for that single request."""
    symbol = symbol.upper()
    data = window_data if window_data is not None else gather_window(root, lookback_days)
    if data is None:
        return {
            "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "symbol": symbol,
            "anchor_date": None,
            "lookback_days": max(1, lookback_days),
            "groups": [],
            "dates": [],
            "cells": {},
        }

    group_rows = [
        {"group": group, "trusted": group in data.fully_trusted_groups}
        for group in sorted(data.group_versions)
    ]

    cells: dict[str, dict[str, bool]] = {date_iso: {} for date_iso in data.dates}
    for group, by_date in data.group_symbols.items():
        for date_iso, symbols in by_date.items():
            if symbol in symbols:
                cells[date_iso][group] = True

    return {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "symbol": symbol,
        "anchor_date": data.anchor.isoformat(),
        "lookback_days": max(1, lookback_days),
        "groups": group_rows,
        "dates": data.dates,
        "cells": cells,
    }
