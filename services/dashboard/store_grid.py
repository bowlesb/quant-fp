"""DATE × COLUMN coverage MATRIX — the always-warm "glimpse into the feature store" grid (v3).

ROWS = dates (most-recent at top, ~18 months back). COLUMNS = the RAW tape layers (bars / trades / quotes)
followed by the ~63 registry FEATURE GROUPS. This fits one screen and is legible.

WHAT A CELL MEANS — coverage against the FULL UNIVERSE (one fixed reference for every column and date):

  coverage[col][date] = (# tickers that have this column's data on this date) / UNIVERSE_SIZE

  The denominator is a SINGLE fixed number — the current ``universe_membership`` size — applied identically to
  every raw layer, every feature group, and every individual feature. (Ben: "the SAME fixed reference for every
  group and every feature.") A universe-wide bar group / the bars raw layer reads ~full; a thin order-flow
  group reads faint; a far-back date where only the calendar groups backfill shows a couple of full columns and
  the rest white. Coverage is quantized to a byte (0 = none, 255 = the whole universe).

COLUMN KINDS (drive the colour ramp on the white-background grid):
  * ``raw``   — a raw Alpaca tape layer (bars / trades / quotes). Not trust-graded; neutral (slate) dark end.
  * ``group`` — a feature group. ``trusted`` true iff ALL its features are trusted: trusted covered cells go
    dark BLUE, untrusted dark RED. Each group carries its ``features`` so the UI can expand it horizontally
    into per-feature sub-columns (features in a group are co-captured in the same (group, date) partition, so a
    feature's coverage equals its group's — the expand needs no extra store I/O).

DRILL — click a (date × group) cell -> the per-TICKER breakdown for that group+date (which tickers have it).

COST — the whole store is ~2.4k (group, source, date) partitions, each a handful of files; one bounded
evenly-sampled symbol-set read per partition gives, per (group, date), the set of tickers present. The raw
layers come from the raw manifests (cheap). The matrix is then a rollup against the fixed denominator. A full
rebuild measures ~2.5min, paid by the background worker on a loop while always serving the last-good document.

Read-side ONLY. Reuses ``feature_grid``'s bounded partition reads + the registry catalog + the trust read, the
``raw_coverage`` manifest read, and a single ``universe_membership`` count. NO feature-store schema/format/
fingerprint change.
"""

from __future__ import annotations

import datetime as dt
import os
from dataclasses import dataclass, field
from pathlib import Path

from feature_grid import (
    STORE_ROOT,
    _catalog_by_group,
    _group_version,
    _read_symbols,
    trusted_feature_names,
)
from group_guide import build_group_info
from raw_coverage import RAW_TIERS, _tier_coverage

# Short "what it is" blurbs for the RAW tape layers (not registry features, so no docstring/catalog). The
# detail panel shows these for a raw column; they carry no per-feature list and no curated guide.
RAW_LAYER_INFO: dict[str, str] = {
    "bars": (
        "Raw Alpaca 1-minute OHLCV BARS — the base price/volume tape every price, volume, calendar, and "
        "candlestick feature is computed from. Coverage = tickers with minute bars that day."
    ),
    "trades": (
        "Raw Alpaca TICK TRADES — individual prints (price, size, conditions). The substrate for trade-flow / "
        "order-flow / microstructure features. Tick coverage is far thinner than bars (gated by capture cost)."
    ),
    "quotes": (
        "Raw Alpaca TICK QUOTES — top-of-book bid/ask updates. The substrate for spread / liquidity-provision "
        "features. The deepest and most expensive layer; coverage is the thinnest."
    ),
}

# Row axis: how many calendar days back from the store anchor the matrix spans. ~18 months ≈ 548 calendar days.
# WEEKDAYS only (a local calendar proxy — no Alpaca calendar / network / secrets in the worker). Env-overridable.
GRID_LOOKBACK_DAYS = int(os.environ.get("STORE_GRID_LOOKBACK_DAYS", "548"))

# Coverage is quantized to a single byte (0 = none, 255 = the whole universe).
COVERAGE_MAX_BYTE = 255

# Fallback full-universe denominator if the DB read is unavailable (the worker normally queries the live
# ``universe_membership`` size for the latest session). ~7.3k is the recent captured in-universe set.
DEFAULT_UNIVERSE_SIZE = int(os.environ.get("STORE_GRID_UNIVERSE_SIZE", "7318"))

# Drill page size: a (date × group) cell click opens the per-ticker breakdown, ranked + paginated.
DRILL_DEFAULT_LIMIT = 500
DRILL_MAX_LIMIT = 2000

# Column kinds.
KIND_RAW = "raw"
KIND_GROUP = "group"

# The two write sources a feature partition can have: ``stream`` (live capture) and ``backfill`` (T+1 / deep
# history). A cell's tickers may come from either or both; keeping them split surfaces the live-coverage gap.
SOURCES = ("stream", "backfill")

# coverage_source sentinel for a cell with no stream/backfill split (raw tape layers, or an absent feature
# cell). The grid carries a per-cell stream-fraction byte 0..255 for feature cells; this marks "not applicable".
SOURCE_NA = -1


_UNIVERSE_SIZE_SQL = """
SELECT count(*) FROM universe_membership
WHERE trade_date = (SELECT max(trade_date) FROM universe_membership) AND in_universe
"""


def universe_size() -> int:
    """The FULL-UNIVERSE denominator — one fixed number applied to every cell, every date, every column. The
    in-universe symbol count for the LATEST session (``universe_membership`` is per trade_date; the table row
    count would wrongly accumulate every date, so we count the most-recent session's in-universe set ~7.3k).
    Falls back to ``DEFAULT_UNIVERSE_SIZE`` if the DB is unreachable so a build never fails on the denominator.
    """
    import psycopg

    from quantlib.features.validation_db import DB_KWARGS

    try:
        with psycopg.connect(**DB_KWARGS, connect_timeout=5) as conn, conn.cursor() as cur:
            cur.execute(_UNIVERSE_SIZE_SQL)
            row = cur.fetchone()
    except psycopg.Error:
        return DEFAULT_UNIVERSE_SIZE
    size = int(row[0]) if row and row[0] else 0
    return size if size > 0 else DEFAULT_UNIVERSE_SIZE


def _partition_dates(root: str, group: str, version: str, source: str) -> list[str]:
    """Date-partition names under a (group, version, source) from DIRECTORY NAMES only — no parquet read."""
    base = Path(root) / f"group={group}" / f"v={version}" / f"source={source}"
    if not base.exists():
        return []
    return [date_dir.name.removeprefix("date=") for date_dir in base.glob("date=*")]


def _store_anchor_date(root: str, group_versions: dict[str, str]) -> dt.date | None:
    """The most recent date present anywhere across all groups/sources — from directory names only."""
    latest: dt.date | None = None
    for group, version in group_versions.items():
        for source in ("stream", "backfill"):
            for date_iso in _partition_dates(root, group, version, source):
                day = dt.date.fromisoformat(date_iso)
                if latest is None or day > latest:
                    latest = day
    return latest


def _window_dates(anchor: dt.date, lookback_days: int) -> list[str]:
    """The grid's date ROWS, most-recent FIRST: ``anchor`` back ``lookback_days`` calendar days, WEEKDAYS only."""
    span = max(1, lookback_days)
    floor = anchor - dt.timedelta(days=span - 1)
    dates: list[str] = []
    day = anchor
    while day >= floor:
        if day.weekday() < 5:
            dates.append(day.isoformat())
        day -= dt.timedelta(days=1)
    return dates


def _group_symbols_by_source_in_window(
    root: str, group: str, version: str, window: set[str]
) -> dict[str, dict[str, set[str]]]:
    """{date_iso: {source: set(tickers)}} for a group over the window dates, kept SPLIT by source so the
    stream-vs-backfill provenance per (group, date) is preserved (the matrix's union is derived from this).
    One read per (source, date) partition — the same reads the union path did, just not collapsed."""
    by_date: dict[str, dict[str, set[str]]] = {}
    for source in SOURCES:
        for date_iso in _partition_dates(root, group, version, source):
            if date_iso not in window:
                continue
            symbols = _read_symbols(root, group, version, source, date_iso)
            if symbols:
                by_date.setdefault(date_iso, {})[source] = symbols
    return by_date


def _union_over_sources(by_source: dict[str, set[str]]) -> set[str]:
    """The distinct tickers across all sources for one (group, date) — the matrix's coverage denominator."""
    union: set[str] = set()
    for symbols in by_source.values():
        union |= symbols
    return union


# The drill's source rollup for an empty / raw cell (no per-ticker provenance).
_ZERO_SOURCE_COUNTS: dict[str, int] = {
    "stream": 0,
    "backfill": 0,
    "both": 0,
    "stream_only": 0,
    "backfill_only": 0,
}


def _ticker_source(ticker: str, stream: set[str], backfill: set[str]) -> str:
    """One ticker's provenance tag for the drill list: ``both`` (live + backfill), ``stream_only`` (live but
    not yet backfilled), or ``backfill_only`` (in history but NOT captured live — the FP_TICK_SYMBOLS gap).
    """
    in_stream = ticker in stream
    in_backfill = ticker in backfill
    if in_stream and in_backfill:
        return "both"
    if in_stream:
        return "stream_only"
    return "backfill_only"


def _fully_trusted_groups(
    catalog_by_group: dict[str, list[dict[str, object]]], trusted: set[str]
) -> set[str]:
    """The groups whose features are ALL trusted — a group's column is trusted only if every one of its
    features has earned binary trust."""
    fully: set[str] = set()
    for group, features in catalog_by_group.items():
        if features and all(str(record["feature"]) in trusted for record in features):
            fully.add(group)
    return fully


def _raw_layer_counts(root: str, window: set[str]) -> dict[str, dict[str, int]]:
    """{tier: {date_iso: n_symbols}} for each raw layer over the window, from the raw manifests (cheap). Only
    in-window dates are kept; a tier with no manifest yields an empty map (rendered as a blank column)."""
    out: dict[str, dict[str, int]] = {}
    for tier, _label in RAW_TIERS:
        coverage = _tier_coverage(root, tier)
        per_date: dict[str, int] = {}
        for cell in coverage["dates"]:  # type: ignore[union-attr]
            date_iso = str(cell["date"])  # type: ignore[index]
            if date_iso in window:
                per_date[date_iso] = int(cell["n_symbols"])  # type: ignore[index]
        out[tier] = per_date
    return out


@dataclass
class WindowData:
    """One full-store read pass over the grid window — gathered ONCE, reused for the matrix AND every drill.

    ``group_symbols`` is {group: {date_iso: set(tickers)}} (the union over sources, the matrix denominator);
    ``group_source_symbols`` is {group: {date_iso: {source: set(tickers)}}} (the SAME tickers kept split by
    stream/backfill so per-cell provenance is recoverable); ``raw_counts`` is {tier: {date_iso: n_symbols}};
    ``group_features`` is {group: [feature names]} for the per-feature expand; ``universe`` the fixed denom.
    """

    anchor: dt.date
    dates: list[str]
    group_versions: dict[str, str]
    group_symbols: dict[str, dict[str, set[str]]]
    group_source_symbols: dict[str, dict[str, dict[str, set[str]]]]
    fully_trusted_groups: set[str]
    raw_counts: dict[str, dict[str, int]]
    group_features: dict[str, list[str]]
    universe: int = field(default=DEFAULT_UNIVERSE_SIZE)

    @property
    def n_groups(self) -> int:
        return len(self.group_versions)


def gather_window(root: str = STORE_ROOT, lookback_days: int = GRID_LOOKBACK_DAYS) -> WindowData | None:
    """The single store-reading pass: catalog + versions + trust + the fixed universe size, the anchor, the
    per-group windowed symbol sets, and the raw-layer per-date counts. ``None`` if the store is empty."""
    catalog_by_group = _catalog_by_group()
    versions: dict[str, str] = {}
    group_features: dict[str, list[str]] = {}
    for group in sorted(catalog_by_group):
        version = _group_version(group)
        if version is not None:
            versions[group] = version
            group_features[group] = [str(record["feature"]) for record in catalog_by_group[group]]

    fully_trusted_groups = _fully_trusted_groups(catalog_by_group, trusted_feature_names())
    anchor = _store_anchor_date(root, versions)
    if anchor is None or not versions:
        return None

    dates = _window_dates(anchor, lookback_days)
    window = set(dates)
    group_symbols: dict[str, dict[str, set[str]]] = {}
    group_source_symbols: dict[str, dict[str, dict[str, set[str]]]] = {}
    for group, version in versions.items():
        by_source = _group_symbols_by_source_in_window(root, group, version, window)
        group_source_symbols[group] = by_source
        group_symbols[group] = {
            date_iso: _union_over_sources(sources) for date_iso, sources in by_source.items()
        }

    return WindowData(
        anchor=anchor,
        dates=dates,
        group_versions=versions,
        group_symbols=group_symbols,
        group_source_symbols=group_source_symbols,
        fully_trusted_groups=fully_trusted_groups,
        raw_counts=_raw_layer_counts(root, window),
        group_features=group_features,
        universe=universe_size(),
    )


def _coverage_byte(n_present: int, universe: int) -> int:
    """Quantize a count against the fixed universe denominator to a 0..255 darkness byte."""
    if universe <= 0 or n_present <= 0:
        return 0
    return min(COVERAGE_MAX_BYTE, round(COVERAGE_MAX_BYTE * min(1.0, n_present / universe)))


def _stream_fraction_byte(by_source: dict[str, set[str]]) -> int:
    """The fraction of a cell's covered tickers that the LIVE stream has, quantized 0..255 (0 = entirely
    backfill-only, 255 = every covered ticker is stream-present). The denominator is the cell's own union
    (NOT the universe), so this reads as "of what we have here, how much is live". ``SOURCE_NA`` if the cell
    has no tickers at all (an absent feature cell carries no provenance)."""
    union = _union_over_sources(by_source)
    if not union:
        return SOURCE_NA
    stream = by_source.get("stream", set())
    return min(COVERAGE_MAX_BYTE, round(COVERAGE_MAX_BYTE * len(stream) / len(union)))


def build_store_grid(
    root: str = STORE_ROOT,
    lookback_days: int = GRID_LOOKBACK_DAYS,
    window_data: WindowData | None = None,
) -> dict[str, object]:
    """Build the DATE × COLUMN coverage matrix (raw layers + feature groups, full-universe denominator).

    Output (compact, JSON-serializable — the worker writes this to Mongo, the reader serves it as-is):
      {generated_at, store_root, anchor_date, lookback_days, universe_size, n_groups, n_trusted_groups,
       dates:   ["2026-06-20", ...],                       # rows, newest first
       columns: [{key, label, kind, trusted, features}],  # raw layers first, then trusted-first feature groups
       coverage: [[byte, ...], ...],                       # rows ⟂ dates, cols ⟂ columns (0..255)
       coverage_source: [[byte, ...], ...],               # parallel: per feature cell the STREAM fraction
       column_coverage_pct: [...],                         # per-column mean coverage over its present dates
       summary: {n_dates, n_columns, n_groups, n_trusted_groups, n_raw, mean_coverage_pct, universe_size}}

    coverage byte = round(255 * n_tickers_present / universe_size). A column absent on a date is byte 0.
    coverage_source byte = round(255 * n_stream_tickers / n_cell_tickers) for a feature cell (0 = entirely
    backfill-only, 255 = every covered ticker is live-stream-present); ``SOURCE_NA`` (-1) for raw / absent cells.
    """
    data = window_data if window_data is not None else gather_window(root, lookback_days)
    if data is None:
        return _empty_grid(root, lookback_days)

    dates = data.dates
    universe = data.universe

    # Columns: RAW layers first (the substrate), then feature groups TRUSTED-FIRST then alphabetical.
    columns: list[dict[str, object]] = []
    for tier, label in RAW_TIERS:
        columns.append({"key": tier, "label": label, "kind": KIND_RAW, "trusted": False, "features": []})
    feature_groups = sorted(
        data.group_versions, key=lambda group: (group not in data.fully_trusted_groups, group)
    )
    for group in feature_groups:
        columns.append(
            {
                "key": group,
                "label": group,
                "kind": KIND_GROUP,
                "trusted": group in data.fully_trusted_groups,
                "features": data.group_features.get(group, []),
            }
        )

    n_columns = len(columns)
    n_raw = len(RAW_TIERS)

    # Per-column per-date present-ticker count.
    def _count(column: dict[str, object], date_iso: str) -> int:
        if column["kind"] == KIND_RAW:
            return data.raw_counts.get(str(column["key"]), {}).get(date_iso, 0)
        symbols = data.group_symbols.get(str(column["key"]), {}).get(date_iso)
        return len(symbols) if symbols else 0

    # Per (group, date) stream fraction, parallel to the coverage byte. Raw-layer cells (no per-source split)
    # and absent cells carry SOURCE_NA. The reader uses this to colour the live-vs-backfill provenance of a cell.
    def _source_byte(column: dict[str, object], date_iso: str) -> int:
        if column["kind"] == KIND_RAW:
            return SOURCE_NA
        by_source = data.group_source_symbols.get(str(column["key"]), {}).get(date_iso)
        return _stream_fraction_byte(by_source) if by_source else SOURCE_NA

    coverage: list[list[int]] = []
    coverage_source: list[list[int]] = []
    col_cov_sum = [0.0] * n_columns
    col_present_dates = [0] * n_columns
    for date_iso in dates:
        cov_row = [0] * n_columns
        src_row = [SOURCE_NA] * n_columns
        for idx, column in enumerate(columns):
            n_present = _count(column, date_iso)
            if n_present <= 0:
                continue
            cov_row[idx] = _coverage_byte(n_present, universe)
            src_row[idx] = _source_byte(column, date_iso)
            col_cov_sum[idx] += min(1.0, n_present / universe) if universe else 0.0
            col_present_dates[idx] += 1
        coverage.append(cov_row)
        coverage_source.append(src_row)

    column_coverage_pct = [
        round(100.0 * col_cov_sum[idx] / col_present_dates[idx], 1) if col_present_dates[idx] else 0.0
        for idx in range(n_columns)
    ]

    populated = [pct for idx, pct in enumerate(column_coverage_pct) if col_present_dates[idx]]
    mean_cov = round(sum(populated) / len(populated), 1) if populated else 0.0
    n_trusted_groups = len(data.fully_trusted_groups)

    # Per-column detail-panel content: registry-derived per-group info (docstring + per-feature descriptions +
    # the curated guide) for feature groups, plus a short blurb for each raw tape layer. Baked into the doc so
    # the dashboard serves it with the grid (zero request-path cost).
    group_info: dict[str, object] = dict(build_group_info())
    for tier, _label in RAW_TIERS:
        group_info[tier] = {
            "docstring": RAW_LAYER_INFO.get(tier, ""),
            "type": "raw_tape",
            "layer": "raw",
            "n_features": 0,
            "features": [],
            "guide": None,
        }

    return {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "store_root": root,
        "anchor_date": data.anchor.isoformat(),
        "lookback_days": max(1, lookback_days),
        "universe_size": universe,
        "n_groups": data.n_groups,
        "n_trusted_groups": n_trusted_groups,
        "dates": dates,
        "columns": columns,
        "group_info": group_info,
        "coverage": coverage,
        "coverage_source": coverage_source,
        "column_coverage_pct": column_coverage_pct,
        "summary": {
            "n_dates": len(dates),
            "n_columns": n_columns,
            "n_groups": data.n_groups,
            "n_trusted_groups": n_trusted_groups,
            "n_raw": n_raw,
            "mean_coverage_pct": mean_cov,
            "universe_size": universe,
        },
    }


def _empty_grid(root: str, lookback_days: int) -> dict[str, object]:
    """Valid empty matrix (no store yet / no registry groups). Same shape as a real build, zero rows/cols."""
    return {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "store_root": root,
        "anchor_date": None,
        "lookback_days": max(1, lookback_days),
        "universe_size": DEFAULT_UNIVERSE_SIZE,
        "n_groups": 0,
        "n_trusted_groups": 0,
        "dates": [],
        "columns": [],
        "group_info": {},
        "coverage": [],
        "coverage_source": [],
        "column_coverage_pct": [],
        "summary": {
            "n_dates": 0,
            "n_columns": 0,
            "n_groups": 0,
            "n_trusted_groups": 0,
            "n_raw": 0,
            "mean_coverage_pct": 0.0,
            "universe_size": DEFAULT_UNIVERSE_SIZE,
        },
    }


def build_cell_drill(
    group: str,
    date: str,
    root: str = STORE_ROOT,
    lookback_days: int = GRID_LOOKBACK_DAYS,
    limit: int = DRILL_DEFAULT_LIMIT,
    window_data: WindowData | None = None,
) -> dict[str, object]:
    """Drill for one (date × group) CELL: the per-TICKER breakdown — which tickers have that group's features
    on that date (sorted, capped at ``limit``), plus the full count, the fixed universe size, and coverage %.
    Each ticker is tagged with its SOURCE provenance — ``stream`` (live + backfill or live-only), ``backfill``
    (backfill-only = present in history but NOT captured live = the FP_TICK_SYMBOLS gap), via the parallel
    ``ticker_sources`` list and the ``source_counts`` rollup. Raw-layer cells have no per-ticker store
    partition, so a raw key returns an empty (count-only) drill with a zeroed source rollup."""
    data = window_data if window_data is not None else gather_window(root, lookback_days)
    capped_limit = max(1, min(limit, DRILL_MAX_LIMIT))
    universe = data.universe if data is not None else DEFAULT_UNIVERSE_SIZE
    if data is None or group not in data.group_symbols:
        n_present = 0
        if data is not None and group in data.raw_counts:
            n_present = data.raw_counts[group].get(date, 0)
        return {
            "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "group": group,
            "date": date,
            "trusted": False,
            "n_tickers": n_present,
            "universe": universe,
            "coverage_pct": round(100.0 * n_present / universe, 1) if universe else 0.0,
            "limit": capped_limit,
            "tickers": [],
            "ticker_sources": [],
            "source_counts": _ZERO_SOURCE_COUNTS.copy(),
        }

    by_source = data.group_source_symbols.get(group, {}).get(date, {})
    stream = by_source.get("stream", set())
    backfill = by_source.get("backfill", set())
    symbols = data.group_symbols[group].get(date, set())
    tickers = sorted(symbols)
    coverage_pct = round(100.0 * len(tickers) / universe, 1) if universe else 0.0
    source_counts = {
        "stream": len(stream),
        "backfill": len(backfill),
        "both": len(stream & backfill),
        "stream_only": len(stream - backfill),
        "backfill_only": len(backfill - stream),
    }
    ticker_sources = [_ticker_source(ticker, stream, backfill) for ticker in tickers[:capped_limit]]

    return {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "group": group,
        "date": date,
        "trusted": group in data.fully_trusted_groups,
        "n_tickers": len(tickers),
        "universe": universe,
        "coverage_pct": coverage_pct,
        "limit": capped_limit,
        "tickers": tickers[:capped_limit],
        "ticker_sources": ticker_sources,
        "source_counts": source_counts,
    }
