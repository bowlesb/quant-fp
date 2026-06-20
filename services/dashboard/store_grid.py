"""DATE × FEATURE-GROUP coverage MATRIX — the always-warm "glimpse into the feature store" grid.

ROWS = dates (most-recent at top, ~18 months back). COLUMNS = the ~63 registry FEATURE GROUPS. This fits one
screen and is legible — unlike the per-ticker axis (11k columns against a shallow store reads as a black void).

WHAT A CELL MEANS — coverage AGGREGATED ACROSS ALL TICKERS for that group on that date:

  coverage[group][date] = (# in-universe tickers that have this GROUP's features on this date)
                          / (# in-universe tickers captured at all that date)

  The denominator is the captured universe THAT DATE (the union of tickers across all groups on that date),
  so a universe-wide bar group reads ~full and a thin order-flow group reads faint — an honest per-group
  breadth. A far-back date where only the calendar groups backfill shows a couple of full columns and the rest
  blank. Coverage is quantized to a byte (0..255) for a compact packed matrix (392×63 ≈ 25k cells — trivial).

BINARY TRUST (Ben: trusted vs untrusted, nothing else)
  Trust is a per-FEATURE property (``feature_trust.trust_state = 'TRUSTED'``); a GROUP is trusted iff ALL its
  features are. Since the columns ARE groups, trust colours whole columns — the 6 trusted vs 57 untrusted are
  immediately visible under the overlay. ``group_trusted`` is the per-column bit.

DRILL — click a (date × group) cell -> the per-TICKER breakdown for that group+date: which tickers have that
group's features that day (ranked, paginated). The secondary "which names" view.

COST — the whole store is ~2.4k (group, source, date) partitions, each a handful of files; one bounded
evenly-sampled symbol-set read per partition (``feature_grid._read_symbols``) gives, per (group, date), the
set of tickers present. The matrix is then a cheap rollup. A full rebuild measures ~3-4min, paid by the
background worker on a loop while always serving the last-good document — never on a request.

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
# them keeps the matrix tight. Env-overridable so a wider/narrower window is a config change, not a code edit.
GRID_LOOKBACK_DAYS = int(os.environ.get("STORE_GRID_LOOKBACK_DAYS", "548"))

# Coverage is quantized to a single byte for the packed matrix (0 = absent, 255 = all in-universe tickers).
COVERAGE_MAX_BYTE = 255

# Drill page size: a (date × group) cell click opens the per-ticker breakdown for that group+date, ranked and
# paginated (the captured universe is ~7-11k, far too many to ship in full per cell).
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
    """The groups whose features are ALL trusted — a group's column is trusted only if every one of its
    features has earned binary trust. A group with any untrusted feature is an untrusted column."""
    fully: set[str] = set()
    for group, features in catalog_by_group.items():
        if features and all(str(record["feature"]) in trusted for record in features):
            fully.add(group)
    return fully


@dataclass
class WindowData:
    """One full-store read pass over the grid window — gathered ONCE, then the matrix and every (group, date)
    drill are derived from it purely in-memory (no re-reads). The store is touched once per worker loop.

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


def _universe_per_date(group_symbols: dict[str, dict[str, set[str]]], dates: list[str]) -> dict[str, int]:
    """Per date, the size of the captured universe = the count of DISTINCT tickers present in ANY group that
    date. This is the per-date denominator for the all-ticker coverage aggregate, so a group covering every
    captured ticker reads full and a thin group reads faint against that day's actual universe."""
    universe: dict[str, int] = {}
    for date_iso in dates:
        seen: set[str] = set()
        for by_date in group_symbols.values():
            symbols = by_date.get(date_iso)
            if symbols:
                seen |= symbols
        universe[date_iso] = len(seen)
    return universe


def build_store_grid(
    root: str = STORE_ROOT,
    lookback_days: int = GRID_LOOKBACK_DAYS,
    window_data: WindowData | None = None,
) -> dict[str, object]:
    """Build the DATE × GROUP coverage + binary-trust matrix (all-ticker aggregate per group per date).

    Pass a pre-gathered ``window_data`` (from ``gather_window``) to assemble without re-reading the store —
    the worker gathers once and reuses it for the matrix AND every drill. Without it, gathers itself.

    Output (compact, JSON-serializable — the worker writes this to Mongo, the reader serves it as-is):
      {generated_at, store_root, anchor_date, lookback_days, n_groups, n_trusted_groups,
       dates:   ["2026-06-20", ...],                       # rows, newest first
       groups:  ["bars_1m", ...],                          # columns (trusted-first, then alpha)
       group_trusted: [1, 0, ...],                         # per-column binary trust bit (aligned to groups)
       coverage: [[byte, ...], ...],                       # rows ⟂ dates, cols ⟂ groups (0..255)
       universe: [n, ...],                                 # per-date captured-universe size (the denominator)
       group_coverage_pct: [...],                          # per-group mean coverage over present dates
       summary: {n_dates, n_groups, n_trusted_groups, mean_coverage_pct}}

    coverage byte = round(255 * |group∩date tickers| / |captured universe that date|). The matrix is dense
    (every date × every group); a group absent on a date is byte 0.
    """
    data = window_data if window_data is not None else gather_window(root, lookback_days)
    if data is None:
        return _empty_grid(root, lookback_days)

    dates = data.dates
    group_symbols = data.group_symbols
    fully_trusted_groups = data.fully_trusted_groups

    # Columns = the groups, ordered TRUSTED-FIRST then alphabetical, so the trusted columns cluster on the left
    # and read as a block under the overlay.
    groups = sorted(data.group_versions, key=lambda group: (group not in fully_trusted_groups, group))
    group_index = {group: idx for idx, group in enumerate(groups)}
    n_groups = len(groups)
    group_trusted = [1 if group in fully_trusted_groups else 0 for group in groups]

    universe = _universe_per_date(group_symbols, dates)

    coverage: list[list[int]] = []
    # Per-group accumulators for the per-column mean coverage (over dates the group is present).
    group_cov_sum = [0.0] * n_groups
    group_present_dates = [0] * n_groups

    for date_iso in dates:
        denom = universe[date_iso]
        cov_row = [0] * n_groups
        if denom > 0:
            for group, by_date in group_symbols.items():
                symbols = by_date.get(date_iso)
                if not symbols:
                    continue
                idx = group_index[group]
                fraction = min(1.0, len(symbols) / denom)
                cov_row[idx] = min(COVERAGE_MAX_BYTE, round(COVERAGE_MAX_BYTE * fraction))
                group_cov_sum[idx] += fraction
                group_present_dates[idx] += 1
        coverage.append(cov_row)

    group_coverage_pct = [
        round(100.0 * group_cov_sum[idx] / group_present_dates[idx], 1) if group_present_dates[idx] else 0.0
        for idx in range(n_groups)
    ]

    # Headline mean coverage = mean over the populated cells (groups present on a date), so an all-blank
    # far-back tail doesn't drag the number toward zero.
    populated = [pct for idx, pct in enumerate(group_coverage_pct) if group_present_dates[idx]]
    mean_cov = round(sum(populated) / len(populated), 1) if populated else 0.0

    return {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "store_root": root,
        "anchor_date": data.anchor.isoformat(),
        "lookback_days": max(1, lookback_days),
        "n_groups": n_groups,
        "n_trusted_groups": len(fully_trusted_groups),
        "dates": dates,
        "groups": groups,
        "group_trusted": group_trusted,
        "coverage": coverage,
        "universe": [universe[date_iso] for date_iso in dates],
        "group_coverage_pct": group_coverage_pct,
        "summary": {
            "n_dates": len(dates),
            "n_groups": n_groups,
            "n_trusted_groups": len(fully_trusted_groups),
            "mean_coverage_pct": mean_cov,
        },
        "legend": _legend(),
    }


def _legend() -> dict[str, str]:
    """The grid's legend text: the coverage scale (all-ticker aggregate per group per date) + a note that
    store depth is UNEVEN, so the faint/blank far-back rows read as honest sparsity, not a bug."""
    return {
        "coverage_scale": (
            "Cell darkness = fraction of that date's captured tickers that have this feature-group "
            "(across ALL tickers — light = few names covered, dark = the whole universe)."
        ),
        "trust_overlay": (
            "Binary trust: a feature-group is GREEN when ALL its features are trusted, else neutral. "
            "Columns are trusted-first, so the trusted groups cluster on the left."
        ),
        "depth_note": (
            "Store depth is UNEVEN — only the calendar groups go back ~18 months; most groups are shallow "
            "(recent weeks/months). Faint/blank far-back rows are honest sparsity, not a bug."
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
        "groups": [],
        "group_trusted": [],
        "coverage": [],
        "universe": [],
        "group_coverage_pct": [],
        "summary": {
            "n_dates": 0,
            "n_groups": 0,
            "n_trusted_groups": 0,
            "mean_coverage_pct": 0.0,
        },
        "legend": _legend(),
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
    on that date. The secondary "which names" view. Tickers are sorted alphabetically and capped at ``limit``
    (the captured universe is large); the full count + the date's universe size are returned for context.

    Pass a pre-gathered ``window_data`` (from ``gather_window``) and the drill is a pure in-memory lookup over
    the SAME read pass the matrix used. Without it (a cell hitting the route live), it gathers once."""
    data = window_data if window_data is not None else gather_window(root, lookback_days)
    capped_limit = max(1, min(limit, DRILL_MAX_LIMIT))
    if data is None or group not in data.group_symbols:
        return {
            "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "group": group,
            "date": date,
            "trusted": False,
            "n_tickers": 0,
            "universe": 0,
            "coverage_pct": 0.0,
            "limit": capped_limit,
            "tickers": [],
        }

    symbols = data.group_symbols[group].get(date, set())
    universe = _universe_per_date(data.group_symbols, [date])[date]
    tickers = sorted(symbols)
    coverage_pct = round(100.0 * len(tickers) / universe, 1) if universe else 0.0

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
    }
