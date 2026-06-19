"""Feature-data COVERAGE + TRUST aggregation for the dashboard grid.

This is the read-side aggregation that powers BOTH dashboard entry points (the visual ``/feature-grid``
HTML page and the agent-facing ``/api/feature-grid`` JSON). It does NOT re-encode any source of truth — it
JOINS the three that already exist:

  * STORE partitions  — ``group=<g>/v=<ver>/source=<stream|backfill>/date=<d>/`` on disk, via
    ``quantlib.features.feature_data`` / ``quantlib.features.store`` (which dates a group has data for,
    stream vs backfill, and the per-date symbol counts).
  * TRUST state       — the validation agent's ``feature_trust.lifecycle_state`` via
    ``quantlib.features.trusted_list`` (UNGRADED / PENDING / VALIDATED / DIVERGENT / RETIRED), never
    re-derived here.
  * CATALOG           — ``REGISTRY.catalog()``: feature -> group, version, description, layer.

Two orthogonal dimensions are kept SEPARATE per the design:

  * DATA COVERAGE  — proportion of expected symbol-days present in the period. Drives the blue fill opacity
    and the % number. Computed per source (stream / backfill) and combined.
  * TRUST STATE    — the lifecycle grade, a distinct visual channel (border/badge). A feature can be
    100%-covered yet UNGRADED; the grid must show both.

COVERAGE DENOMINATOR (honest, no hardcoded universe): for a (group, period) we count the symbol-days
actually present and divide by ``n_trading_days_in_period * expected_universe``, where the expected
universe is the group's OWN peak distinct-symbol count on any single date in the window. So a group that
peaks at 135 symbols but only captured 135 on one of two days reads ~50%, which is the honest answer for
sparse data. ``n_trading_days_in_period`` is the weekday count of the period window (a local Mon-Fri
calendar — we never call the network / Alpaca here so the page stays fast and secret-free). A FIXED-lookback
window ("last week / month / 6 months") uses its TRUE span, so a 6-month cell over a 2-day store correctly
reads near-empty — the row axis conveys temporal DEPTH rather than collapsing every multi-day row onto the
same captured days. Only the "all-history" row clamps its start to the earliest captured date, where
pre-capture calendar days should not count as missing.

The full grid is computed in one pass over the store directory tree (no parquet bodies read for the
group-level grid — only ``symbol`` columns are read, and only when per-date symbol counts are needed),
then cached with a short TTL so a refresh is 1-2s, not minutes.
"""

from __future__ import annotations

import datetime as dt
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

import polars as pl

import quantlib.features  # noqa: F401  (import self-registers every feature group into REGISTRY)
from quantlib.features.registry import REGISTRY
from quantlib.features.store import _partition_dir

STORE_ROOT = os.environ.get("STORE_ROOT", "/store")

# The STREAM source writes one tiny parquet PER MINUTE (``data-<shard>-<epoch>.parquet``), so a single
# (group, date) stream partition can hold thousands of files (a full RTH day x shards). Reading every file
# to count symbols would be minutes of I/O. The captured symbol UNIVERSE is ~constant across a day's
# minute-files (the same names stream all day), so we read a bounded EVENLY-SPACED SAMPLE per partition and
# union the symbols — accurate for the coverage universe at a tiny, fixed cost. Backfill partitions are a
# single settled file and are read in full.
MAX_FILES_PER_PARTITION = 12

# Period rows, top -> down: short to long. Each maps to a lookback in CALENDAR days from the latest store
# date (inclusive). "all" is the full captured history. Order is the row order in the grid.
PERIODS: list[tuple[str, str, int | None]] = [
    ("1d", "Last day", 1),
    ("1w", "Last week", 7),
    ("1m", "Last month", 30),
    ("2m", "Last 2 months", 60),
    ("6m", "Last 6 months", 182),
    ("12m", "Last 12 months", 365),
    ("all", "All history", None),
]

# A feature is VALIDATED at MIN_CLEAN_DAYS clean days of held parity. Mirrors trust_lifecycle.MIN_CLEAN_DAYS
# — imported rather than hardcoded would couple the dashboard image to that module's constant; we surface it
# as the "days needed" denominator for the "X% to trusted" progress indicator and keep it here, documented.
DAYS_NEEDED_FOR_TRUST = 2


@dataclass
class FeatureTrust:
    """One feature's trust evidence, joined from ``feature_trust`` (via the full-table read below)."""

    feature: str
    lifecycle_state: str
    clean_days: int
    clean_days_passed: int
    clean_value_rate: float | None
    last_validated_day: str | None


@dataclass
class CellCoverage:
    """Coverage for one (group, period, source): symbol-days present vs expected."""

    n_dates: int = 0
    symbol_days: int = 0
    expected_symbol_days: int = 0
    first_date: str | None = None
    last_date: str | None = None

    @property
    def pct(self) -> float:
        if self.expected_symbol_days <= 0:
            return 0.0
        return round(100.0 * self.symbol_days / self.expected_symbol_days, 1)


@dataclass
class _GroupStoreInfo:
    """Per-group store facts gathered in one directory pass, reused across all periods."""

    version: str
    # source -> {date_iso: n_symbols}
    per_date_symbols: dict[str, dict[str, int]] = field(default_factory=dict)


def trading_weekdays(start: dt.date, end: dt.date) -> int:
    """Mon-Fri count in [start, end] inclusive. A local calendar proxy for trading days — deliberately NOT
    the Alpaca calendar (no network / secrets in the dashboard). Holidays are not removed; over the short
    windows that matter this is a tight upper bound and keeps "missing" well-defined and honest."""
    if end < start:
        return 0
    total = 0
    day = start
    while day <= end:
        if day.weekday() < 5:
            total += 1
        day += dt.timedelta(days=1)
    return total


def _sample_files(files: list[Path], cap: int) -> list[Path]:
    """Up to ``cap`` evenly-spaced files from a sorted list (always includes first + last). Bounds the
    symbol-union read on a stream partition's thousands of per-minute files to a fixed cost."""
    if len(files) <= cap:
        return files
    step = (len(files) - 1) / (cap - 1)
    indices = sorted({round(i * step) for i in range(cap)})
    return [files[i] for i in indices]


def _read_symbols(root: str, group: str, version: str, source: str, date_iso: str) -> set[str]:
    """The distinct symbol SET in a (group, version, source, date) partition. Reads only the ``symbol``
    column, from a bounded evenly-spaced SAMPLE of the partition's files (the symbol universe is ~constant
    across a day's per-minute stream files), so a 7k-file stream partition costs ~12 reads, not 7k."""
    partition = _partition_dir(root, group, version, source, date_iso)
    files = sorted(partition.glob("data*.parquet"))
    if not files:
        return set()
    symbols: set[str] = set()
    for path in _sample_files(files, MAX_FILES_PER_PARTITION):
        symbols.update(pl.read_parquet(path, columns=["symbol"])["symbol"].to_list())
    return symbols


def _read_n_symbols(root: str, group: str, version: str, source: str, date_iso: str) -> int:
    """Distinct-symbol COUNT in a (group, version, source, date) partition (the set's size)."""
    return len(_read_symbols(root, group, version, source, date_iso))


def _group_version(group: str) -> str | None:
    for group_obj in REGISTRY.groups():
        if group_obj.name == group:
            return group_obj.version
    return None


def gather_group_store_info(root: str, group: str, version: str) -> _GroupStoreInfo:
    """One pass over a group's store tree: per-source, per-date distinct-symbol counts. The per-date symbol
    read is the only parquet touch and is what makes coverage symbol-day accurate; it is cached upstream."""
    info = _GroupStoreInfo(version=version)
    for source in ("stream", "backfill"):
        base = Path(root) / f"group={group}" / f"v={version}" / f"source={source}"
        if not base.exists():
            info.per_date_symbols[source] = {}
            continue
        per_date: dict[str, int] = {}
        for date_dir in sorted(base.glob("date=*")):
            date_iso = date_dir.name.removeprefix("date=")
            per_date[date_iso] = _read_n_symbols(root, group, version, source, date_iso)
        info.per_date_symbols[source] = per_date
    return info


def latest_store_date(infos: dict[str, _GroupStoreInfo]) -> dt.date | None:
    """The most recent date with any partition across all groups/sources — the anchor every period ends at."""
    latest: dt.date | None = None
    for info in infos.values():
        for per_date in info.per_date_symbols.values():
            for date_iso in per_date:
                day = dt.date.fromisoformat(date_iso)
                if latest is None or day > latest:
                    latest = day
    return latest


def earliest_store_date(infos: dict[str, _GroupStoreInfo]) -> dt.date | None:
    earliest: dt.date | None = None
    for info in infos.values():
        for per_date in info.per_date_symbols.values():
            for date_iso in per_date:
                day = dt.date.fromisoformat(date_iso)
                if earliest is None or day < earliest:
                    earliest = day
    return earliest


def period_window(
    period_key: str,
    lookback_days: int | None,
    anchor: dt.date,
    floor: dt.date,
) -> tuple[dt.date, dt.date]:
    """[start, end] dates for a period: ends at ``anchor`` (latest store date). For a FIXED lookback the
    start is the TRUE window edge (``anchor - lookback_days + 1``) and is NOT clamped to ``floor`` — so a
    "last 6 months" cell divides by the window's full ~130 trading days and honestly reads near-empty until
    that history is backfilled. The row axis must convey temporal DEPTH; clamping every window to ``floor``
    would collapse every multi-day row onto the same handful of captured days (identical numbers). Only the
    "all-history" row (``lookback_days is None``) starts at ``floor`` — there the intent IS "all the history
    we have", so pre-capture calendar days should not count as missing."""
    end = anchor
    if lookback_days is None:
        start = floor
    else:
        start = anchor - dt.timedelta(days=lookback_days - 1)
    return start, end


def coverage_for_source(
    per_date: dict[str, int], start: dt.date, end: dt.date, n_trading_days: int
) -> CellCoverage:
    """Coverage for one source over [start, end]: symbol-days present vs expected (peak-universe * days)."""
    in_window = {
        date_iso: n
        for date_iso, n in per_date.items()
        if start <= dt.date.fromisoformat(date_iso) <= end and n > 0
    }
    if not in_window:
        return CellCoverage()
    symbol_days = sum(in_window.values())
    peak_universe = max(in_window.values())
    expected = peak_universe * max(n_trading_days, len(in_window))
    dates_sorted = sorted(in_window)
    return CellCoverage(
        n_dates=len(in_window),
        symbol_days=symbol_days,
        expected_symbol_days=expected,
        first_date=dates_sorted[0],
        last_date=dates_sorted[-1],
    )


def _aggregate_trust(states: list[str]) -> tuple[str, float, int, int, int]:
    """Reduce a group's per-feature lifecycle states to a single cell badge.

    Returns (cell_state, trusted_pct, n_trusted, n_validating, n_ungraded). The cell badge is the WORST
    actionable state that is present so the operator sees risk first: any DIVERGENT -> DIVERGENT; else if
    none validated and some pending -> PENDING; else if any validated and all graded -> VALIDATED; else
    UNGRADED. ``trusted_pct`` is n_trusted / n_features (what fraction of the group has EARNED trust)."""
    if not states:
        return "UNGRADED", 0.0, 0, 0, 0
    n_total = len(states)
    n_trusted = sum(1 for state in states if state == "VALIDATED")
    n_validating = sum(1 for state in states if state == "PENDING")
    n_divergent = sum(1 for state in states if state == "DIVERGENT")
    n_ungraded = sum(1 for state in states if state in ("UNGRADED", "RETIRED"))
    trusted_pct = round(100.0 * n_trusted / n_total, 1)
    if n_divergent:
        cell_state = "DIVERGENT"
    elif n_trusted == n_total:
        cell_state = "VALIDATED"
    elif n_trusted:
        cell_state = "VALIDATED"  # partially trusted; pct carries the nuance, badge shows green-with-pct
    elif n_validating:
        cell_state = "PENDING"
    else:
        cell_state = "UNGRADED"
    return cell_state, trusted_pct, n_trusted, n_validating, n_ungraded


def trust_by_feature() -> dict[str, FeatureTrust]:
    """{feature: FeatureTrust} from ``feature_trust`` — the FULL table (every grade, not just VALIDATED), so
    UNGRADED/PENDING/DIVERGENT are distinguishable in the grid. Reuses the same DB the trust state machine
    writes; the VALIDATED subset matches ``trusted_features()`` by construction (asserted in tests)."""
    rows = _read_full_trust()
    out: dict[str, FeatureTrust] = {}
    for row in rows:
        feature = str(row["feature"])
        state = row["lifecycle_state"] or "UNGRADED"
        out[feature] = FeatureTrust(
            feature=feature,
            lifecycle_state=str(state),
            clean_days=int(row["clean_days"] or 0),
            clean_days_passed=int(row["clean_days_passed"] or 0),
            clean_value_rate=(
                float(row["clean_value_rate"]) if row["clean_value_rate"] is not None else None
            ),
            last_validated_day=(
                row["last_validated_day"].isoformat() if row["last_validated_day"] else None
            ),
        )
    return out


_FULL_TRUST_QUERY = """
SELECT feature, lifecycle_state, clean_days, clean_days_passed, clean_value_rate, last_validated_day
FROM feature_trust
"""


def _read_full_trust() -> list[dict[str, object]]:
    """Full ``feature_trust`` table as dict rows. Isolated so tests can monkeypatch it without a DB."""
    import psycopg

    from quantlib.features.validation_db import DB_KWARGS

    with psycopg.connect(**DB_KWARGS, connect_timeout=5) as conn, conn.cursor() as cur:
        cur.execute(_FULL_TRUST_QUERY)
        columns = [desc[0] for desc in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]


def _catalog_by_group() -> dict[str, list[dict[str, object]]]:
    """{group: [feature catalog rows]} from the registry catalog (name, description, layer, version)."""
    catalog = REGISTRY.catalog()
    out: dict[str, list[dict[str, object]]] = {}
    for record in catalog.iter_rows(named=True):
        out.setdefault(record["group"], []).append(record)
    return out


def build_grid(root: str = STORE_ROOT) -> dict[str, object]:
    """The full coverage + trust grid: groups x periods, plus a summary header. The single computation both
    entry points (HTML + JSON) render. Reads the store tree once (per-date symbol counts) and the trust
    table once, then derives every cell — so the whole grid is one cheap pass, cached upstream."""
    catalog_by_group = _catalog_by_group()
    trust = trust_by_feature()
    groups = sorted(catalog_by_group)

    infos: dict[str, _GroupStoreInfo] = {}
    for group in groups:
        version = _group_version(group)
        if version is None:
            continue
        infos[group] = gather_group_store_info(root, group, version)

    anchor = latest_store_date(infos)
    floor = earliest_store_date(infos)

    columns: list[dict[str, object]] = []
    cells: list[dict[str, object]] = []

    total_features = 0
    total_trusted = 0
    coverage_accum: list[float] = []
    fully_validated_groups = 0

    for group in groups:
        features = catalog_by_group[group]
        feature_names = [str(record["feature"]) for record in features]
        states = [
            trust[name].lifecycle_state if name in trust else "UNGRADED" for name in feature_names
        ]
        n_features = len(feature_names)
        total_features += n_features
        n_group_trusted = sum(1 for state in states if state == "VALIDATED")
        total_trusted += n_group_trusted
        if n_features and n_group_trusted == n_features:
            fully_validated_groups += 1

        info = infos.get(group)
        version = info.version if info else (_group_version(group) or "?")
        columns.append(
            {
                "group": group,
                "version": version,
                "layer": (features[0]["layer"] if features else None),
                "n_features": n_features,
            }
        )

        if anchor is None or floor is None or info is None:
            for period_key, _label, _lookback in PERIODS:
                cells.append(
                    _empty_cell(group, period_key, n_features, states)
                )
            continue

        for period_key, _label, lookback in PERIODS:
            start, end = period_window(period_key, lookback, anchor, floor)
            n_trading = trading_weekdays(start, end)
            stream_cov = coverage_for_source(
                info.per_date_symbols.get("stream", {}), start, end, n_trading
            )
            backfill_cov = coverage_for_source(
                info.per_date_symbols.get("backfill", {}), start, end, n_trading
            )
            combined_pct = max(stream_cov.pct, backfill_cov.pct)
            n_symbols = max(
                (max(info.per_date_symbols.get("stream", {}).values(), default=0)),
                (max(info.per_date_symbols.get("backfill", {}).values(), default=0)),
            )
            n_dates = max(stream_cov.n_dates, backfill_cov.n_dates)
            cell_state, trust_pct, n_trusted, n_validating, n_ungraded = _aggregate_trust(states)
            cells.append(
                {
                    "group": group,
                    "period": period_key,
                    "coverage_pct": combined_pct,
                    "stream_pct": stream_cov.pct,
                    "backfill_pct": backfill_cov.pct,
                    "n_features": n_features,
                    "n_symbols": n_symbols if n_dates else 0,
                    "n_dates": n_dates,
                    "trust_state": cell_state,
                    "trust_pct": trust_pct,
                    "n_trusted": n_trusted,
                    "n_validating": n_validating,
                    "n_ungraded": n_ungraded,
                }
            )
            if period_key == "all":
                coverage_accum.append(combined_pct)

    mean_coverage = round(sum(coverage_accum) / len(coverage_accum), 1) if coverage_accum else 0.0

    return {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "store_root": root,
        "anchor_date": anchor.isoformat() if anchor else None,
        "earliest_date": floor.isoformat() if floor else None,
        "periods": [
            {"key": key, "label": label, "lookback_days": lookback}
            for key, label, lookback in PERIODS
        ],
        "groups": columns,
        "cells": cells,
        "summary": {
            "n_groups": len(groups),
            "n_features": total_features,
            "n_trusted": total_trusted,
            "trusted_pct": round(100.0 * total_trusted / total_features, 1) if total_features else 0.0,
            "mean_coverage_pct": mean_coverage,
            "fully_validated_groups": fully_validated_groups,
            "days_needed_for_trust": DAYS_NEEDED_FOR_TRUST,
        },
    }


def _empty_cell(
    group: str, period_key: str, n_features: int, states: list[str]
) -> dict[str, object]:
    cell_state, trust_pct, n_trusted, n_validating, n_ungraded = _aggregate_trust(states)
    return {
        "group": group,
        "period": period_key,
        "coverage_pct": 0.0,
        "stream_pct": 0.0,
        "backfill_pct": 0.0,
        "n_features": n_features,
        "n_symbols": 0,
        "n_dates": 0,
        "trust_state": cell_state,
        "trust_pct": trust_pct,
        "n_trusted": n_trusted,
        "n_validating": n_validating,
        "n_ungraded": n_ungraded,
    }


def build_group_detail(group: str, root: str = STORE_ROOT) -> dict[str, object]:
    """Per-feature detail for one group (the expanded view): each feature's coverage span, trust state,
    trust trajectory (clean_days / needed, match rate, last validated), and description (for hover)."""
    catalog_by_group = _catalog_by_group()
    if group not in catalog_by_group:
        raise KeyError(group)
    features = catalog_by_group[group]
    trust = trust_by_feature()
    version = _group_version(group) or (str(features[0]["version"]) if features else "?")
    info = gather_group_store_info(root, group, version)
    stream_dates = sorted(d for d, n in info.per_date_symbols.get("stream", {}).items() if n > 0)
    backfill_dates = sorted(d for d, n in info.per_date_symbols.get("backfill", {}).items() if n > 0)

    feature_rows: list[dict[str, object]] = []
    for record in features:
        name = str(record["feature"])
        grade = trust.get(name)
        state = grade.lifecycle_state if grade else "UNGRADED"
        clean_days = grade.clean_days if grade else 0
        progress_pct = (
            round(100.0 * min(clean_days, DAYS_NEEDED_FOR_TRUST) / DAYS_NEEDED_FOR_TRUST, 1)
            if state != "VALIDATED"
            else 100.0
        )
        feature_rows.append(
            {
                "feature": name,
                "description": record["description"],
                "layer": record["layer"],
                "parity_method": record["parity_method"],
                "trust_state": state,
                "clean_days": clean_days,
                "days_needed": DAYS_NEEDED_FOR_TRUST,
                "progress_to_trusted_pct": progress_pct,
                "clean_value_rate": grade.clean_value_rate if grade else None,
                "last_validated_day": grade.last_validated_day if grade else None,
            }
        )

    return {
        "group": group,
        "version": version,
        "n_features": len(features),
        "stream_dates": stream_dates,
        "backfill_dates": backfill_dates,
        "stream_first": stream_dates[0] if stream_dates else None,
        "stream_last": stream_dates[-1] if stream_dates else None,
        "backfill_first": backfill_dates[0] if backfill_dates else None,
        "backfill_last": backfill_dates[-1] if backfill_dates else None,
        # parity needs BOTH sides: the dates present on stream but missing on backfill (and vice versa) are
        # exactly why a feature cannot be trusted yet, so surface them.
        "stream_only_dates": sorted(set(stream_dates) - set(backfill_dates)),
        "backfill_only_dates": sorted(set(backfill_dates) - set(stream_dates)),
        "features": feature_rows,
    }


@dataclass
class _GroupSymbolSets:
    """The stream/backfill symbol sets for one group on each source's own latest date — the raw material both
    the per-group surface and the cross-group roll-up classify, computed by ONE pass over the store."""

    stream_date: str | None
    backfill_date: str | None
    stream_symbols: set[str]
    backfill_symbols: set[str]


def _group_symbol_sets(group: str, version: str, root: str) -> _GroupSymbolSets:
    """A group's stream + backfill symbol sets on each source's OWN latest partition. Stream and backfill
    settle at different cadences, so each side uses its freshest captured universe (a lagging stream still
    diffs against its newest day, not an empty newer date). Same bounded per-partition sampling as the grid."""
    stream_date = _latest_partition_date(root, group, version, "stream")
    backfill_date = _latest_partition_date(root, group, version, "backfill")
    stream_symbols = _read_symbols(root, group, version, "stream", stream_date) if stream_date else set()
    backfill_symbols = (
        _read_symbols(root, group, version, "backfill", backfill_date) if backfill_date else set()
    )
    return _GroupSymbolSets(stream_date, backfill_date, stream_symbols, backfill_symbols)


def build_symbol_coverage(group: str, root: str = STORE_ROOT) -> dict[str, object]:
    """Per-SYMBOL coverage for one group on its latest store date: which tickers the live STREAM actually
    captured vs which exist only in BACKFILL. The group grid surfaces a single peak symbol COUNT, which hides
    *which* names are thin live — but the live stream subscribes a far smaller universe than backfill agg
    covers (e.g. an order-flow group can read ~1300 backfill symbols yet only ~50 on the live tick stream).
    This is the ticker-representation surface: ``backfill_only`` is exactly the set under-represented LIVE.

    The date is each source's OWN latest partition (stream and backfill backfill at different cadences), so a
    stream lagging a day still compares its freshest captured universe, not an empty newer date. Symbols are
    read with the same bounded per-partition sampling the grid uses, so this is one cheap pass per source.
    """
    if group not in _catalog_by_group():
        raise KeyError(group)
    version = _group_version(group)
    if version is None:
        raise KeyError(group)

    sets = _group_symbol_sets(group, version, root)
    stream_date, backfill_date = sets.stream_date, sets.backfill_date
    stream_symbols, backfill_symbols = sets.stream_symbols, sets.backfill_symbols

    both = stream_symbols & backfill_symbols
    backfill_only = backfill_symbols - stream_symbols
    stream_only = stream_symbols - backfill_symbols
    union = stream_symbols | backfill_symbols
    stream_pct = round(100.0 * len(stream_symbols) / len(union), 1) if union else 0.0

    return {
        "group": group,
        "version": version,
        "stream_date": stream_date,
        "backfill_date": backfill_date,
        "n_stream": len(stream_symbols),
        "n_backfill": len(backfill_symbols),
        "n_both": len(both),
        # backfill_only = present in the (full-universe) backfill agg but NOT captured live — the
        # under-represented LIVE tickers, the headline of this surface.
        "n_backfill_only": len(backfill_only),
        "n_stream_only": len(stream_only),
        # of every symbol this group has on either side today, what fraction the live stream captured.
        "stream_coverage_pct": stream_pct,
        "both": sorted(both),
        "backfill_only": sorted(backfill_only),
        "stream_only": sorted(stream_only),
    }


def _latest_partition_date(root: str, group: str, version: str, source: str) -> str | None:
    """The most recent ``date=`` partition (ISO string) for one (group, source), or None if the source has
    no partitions. A bare directory-name scan — no parquet bodies read."""
    base = Path(root) / f"group={group}" / f"v={version}" / f"source={source}"
    if not base.exists():
        return None
    dates = sorted(d.name.removeprefix("date=") for d in base.glob("date=*"))
    return dates[-1] if dates else None


# A symbol is only worth flagging as thin-LIVE in a group that the live stream actually covers at all on that
# group — a group with ZERO stream symbols (never live-subscribed) would otherwise mark its ENTIRE backfill
# universe under-represented, drowning the signal. We count a symbol's under-representation only across groups
# that ARE live (stream non-empty), so the rank reflects names the stream COULD have but DIDN'T carry.
def build_thin_live_symbols(root: str = STORE_ROOT, limit: int = 50) -> dict[str, object]:
    """Cross-group roll-up ranking the THINNEST-live tickers: symbols present in the full-universe BACKFILL
    agg but absent from the live STREAM, counted across the MOST groups. The per-group ``/symbols`` surface
    answers "which names is THIS group thin on"; this answers the inverse and system-wide — "which NAMES are
    under-represented live across the most groups", the natural ticker-representation flag for the
    FP_TICK_SYMBOLS coverage gap. Read-side only: reuses ``_group_symbol_sets`` (one bounded pass per group).

    Under-representation is scored only over LIVE groups (groups with a non-empty stream universe today); a
    group the stream never subscribes would otherwise mark its entire backfill universe thin and swamp the
    ranking. So a symbol's ``n_under_groups`` is "of the groups the stream IS carrying, how many omit it".
    """
    catalog = _catalog_by_group()
    groups = sorted(catalog)

    under_count: dict[str, int] = {}
    under_groups: dict[str, list[str]] = {}
    live_count: dict[str, int] = {}
    n_live_groups = 0
    group_rows: list[dict[str, object]] = []

    for group in groups:
        version = _group_version(group)
        if version is None:
            continue
        sets = _group_symbol_sets(group, version, root)
        if not sets.stream_symbols:
            # Group not live-covered today — it has no live universe to be "under" vs, so it cannot witness
            # under-representation. Recorded in the group breakdown but excluded from the per-symbol score.
            group_rows.append(
                {
                    "group": group,
                    "live": False,
                    "n_stream": 0,
                    "n_backfill": len(sets.backfill_symbols),
                    "n_under": 0,
                }
            )
            continue
        n_live_groups += 1
        backfill_only = sets.backfill_symbols - sets.stream_symbols
        for symbol in sets.stream_symbols:
            live_count[symbol] = live_count.get(symbol, 0) + 1
        for symbol in backfill_only:
            under_count[symbol] = under_count.get(symbol, 0) + 1
            under_groups.setdefault(symbol, []).append(group)
        group_rows.append(
            {
                "group": group,
                "live": True,
                "n_stream": len(sets.stream_symbols),
                "n_backfill": len(sets.backfill_symbols),
                "n_under": len(backfill_only),
            }
        )

    # Rank thinnest-first: most under-represented groups, then fewest groups actually carrying it live (a
    # symbol missed by 8 groups and live on 1 is thinner than one missed by 8 yet live on 20), then name.
    ranked = sorted(
        under_count,
        key=lambda symbol: (-under_count[symbol], live_count.get(symbol, 0), symbol),
    )
    symbols = [
        {
            "symbol": symbol,
            "n_under_groups": under_count[symbol],
            "n_live_groups": live_count.get(symbol, 0),
            "under_groups": sorted(under_groups[symbol]),
        }
        for symbol in ranked[:limit]
    ]

    return {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "store_root": root,
        "n_live_groups": n_live_groups,
        "n_groups": len(groups),
        "n_thin_symbols": len(under_count),
        "limit": limit,
        "symbols": symbols,
        "groups": sorted(group_rows, key=lambda gr: (not gr["live"], -int(gr["n_under"]), gr["group"])),
    }


class GridCache:
    """Tiny TTL cache so a page load / refresh re-aggregates at most every ``ttl`` seconds. The grid read is
    1-2s on the live store; a 60s TTL makes a busy refresh instant while staying fresh enough for a coverage
    surface that only changes on the daily lifecycle write."""

    def __init__(self, ttl: float = 60.0) -> None:
        self.ttl = ttl
        self._grid: dict[str, object] | None = None
        self._grid_at: float = 0.0
        self._details: dict[str, tuple[float, dict[str, object]]] = {}
        self._symbols: dict[str, tuple[float, dict[str, object]]] = {}
        self._thin: dict[int, tuple[float, dict[str, object]]] = {}

    def grid(self, root: str, force: bool = False) -> dict[str, object]:
        now = time.monotonic()
        if force or self._grid is None or (now - self._grid_at) > self.ttl:
            self._grid = build_grid(root)
            self._grid_at = now
        return self._grid

    def detail(self, group: str, root: str, force: bool = False) -> dict[str, object]:
        now = time.monotonic()
        cached = self._details.get(group)
        if not force and cached is not None and (now - cached[0]) <= self.ttl:
            return cached[1]
        detail = build_group_detail(group, root)
        self._details[group] = (now, detail)
        return detail

    def symbols(self, group: str, root: str, force: bool = False) -> dict[str, object]:
        now = time.monotonic()
        cached = self._symbols.get(group)
        if not force and cached is not None and (now - cached[0]) <= self.ttl:
            return cached[1]
        coverage = build_symbol_coverage(group, root)
        self._symbols[group] = (now, coverage)
        return coverage

    def thin_live(self, root: str, limit: int = 50, force: bool = False) -> dict[str, object]:
        now = time.monotonic()
        cached = self._thin.get(limit)
        if not force and cached is not None and (now - cached[0]) <= self.ttl:
            return cached[1]
        rollup = build_thin_live_symbols(root, limit)
        self._thin[limit] = (now, rollup)
        return rollup


CACHE = GridCache()
