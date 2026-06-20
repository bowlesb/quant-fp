"""Feature-data COVERAGE + TRUST aggregation for the dashboard grid.

This is the read-side aggregation that powers BOTH dashboard entry points (the visual ``/feature-grid``
HTML page and the agent-facing ``/api/feature-grid`` JSON). It does NOT re-encode any source of truth — it
JOINS the three that already exist:

  * STORE partitions  — ``group=<g>/v=<ver>/source=<stream|backfill>/date=<d>/`` on disk, via
    ``quantlib.features.feature_data`` / ``quantlib.features.store`` (which dates a group has data for,
    stream vs backfill, and the per-date symbol counts).
  * TRUST state       — TWO distinct surfaces, kept separate on purpose, neither re-derived here:
      - the BINARY-trust GATE ``feature_trust.trust_state='TRUSTED'`` (docs/TRUST_REDESIGN.md, via
        ``quantlib.features.trusted_list``) — the consumable predicate downstream agents gate on, and the
        TRUSTED side of the trust-frontier panel (``_read_trusted_names``).
      - the legacy ``feature_trust.lifecycle_state`` DIAGNOSTIC (UNGRADED / PENDING / VALIDATED / DIVERGENT
        / RETIRED) — the richer per-feature grade the grid BADGE renders for legibility (the binary gate
        collapses these five into trusted/not). It is NOT the gate; it is still written each sweep but lags
        the binary grant, so a feature can be binary-TRUSTED while its lifecycle badge still reads PENDING.
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

# Trust-FRONTIER states (a derived view over the binary-trust gate x open-defect, NOT a new source of truth):
#   TRUSTED  — feature_trust.trust_state == 'TRUSTED' (deterministic-by-construction or stream==backfill on a
#              clean day; already earned, docs/TRUST_REDESIGN.md).
#   BLOCKED  — has an OPEN feature_parity_defect row: a parity failure the agent has NOT yet cleared. These
#              do NOT advance on the next sweep without a fix (today: the FP_TICK_SYMBOLS tick-coverage tail).
#   ELIGIBLE — not yet trusted AND no open defect: features accruing clean days, PLUS those whose defect was
#              cleared (the lifecycle_state lags until the next clean sweep re-grades). This is the frontier
#              that becomes TRUSTED on the next clean settled sweep — the legibility the flat badge hides.
FRONTIER_TRUSTED = "TRUSTED"
FRONTIER_ELIGIBLE = "ELIGIBLE"
FRONTIER_BLOCKED = "BLOCKED"

# A feature is VALIDATED at MIN_CLEAN_DAYS clean days of held parity. Mirrors trust_lifecycle.MIN_CLEAN_DAYS
# — imported rather than hardcoded would couple the dashboard image to that module's constant; we surface it
# as the "days needed" denominator for the "X% to trusted" progress indicator and keep it here, documented.
DAYS_NEEDED_FOR_TRUST = 2

# The tick/order-flow groups: features derived from the per-trade tick stream (signed flow, inter-arrival,
# run-length, size distribution, trade-frequency z, liquidity/spread, exhaustion). These are the groups whose
# LIVE breadth is gated by ``FP_TICK_SYMBOLS`` (unset -> ~24-canary floor) while their full-universe BACKFILL
# agg is parity-true. The coverage-trend surface reads live-stream breadth ONLY over these groups, so the
# day-over-day number tracks exactly the FP_TICK_SYMBOLS widening (vs stalling at the floor) decision — bar/
# price groups, which the stream covers ~universe-wide, would otherwise drown the order-flow signal.
# Kept in sync with the Modeller's FULL order-flow tier (build_orderflow_dataset.py); a group not on disk is
# simply skipped, so listing a not-yet-shipped group here is harmless.
ORDERFLOW_GROUPS = [
    "trade_flow",
    "signed_trade_ratio",
    "liquidity",
    "quote_spread",
    "trade_freq_z",
    "trade_size_dist",
    "inter_arrival",
    "tick_runlength",
    "microstructure_burst",
    "volume_leads_price",
    "volume_exhaustion",
]


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


_OPEN_DEFECT_QUERY = """
SELECT feature, version, feature_group, first_seen_day, last_seen_day, worst_rel_err
FROM feature_parity_defect
WHERE status = 'open'
"""


def _read_open_defects() -> list[dict[str, object]]:
    """OPEN rows of ``feature_parity_defect`` as dict rows — the still-blocking parity failures (a defect the
    parity agent marks ``fixed``/``wontfix`` drops out, even though the feature's ``lifecycle_state`` stays
    DIVERGENT until the next clean sweep re-grades it). Isolated so tests can monkeypatch it without a DB."""
    import psycopg

    from quantlib.features.validation_db import DB_KWARGS

    with psycopg.connect(**DB_KWARGS, connect_timeout=5) as conn, conn.cursor() as cur:
        cur.execute(_OPEN_DEFECT_QUERY)
        columns = [desc[0] for desc in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]


def open_defect_features() -> set[str]:
    """{feature} with an OPEN parity defect — the genuinely-blocked set the trust frontier is gated on."""
    return {str(row["feature"]) for row in _read_open_defects()}


_TRUSTED_NAMES_QUERY = "SELECT feature FROM feature_trust WHERE trust_state = 'TRUSTED'"


def _read_trusted_names() -> list[str]:
    """Feature names with ``feature_trust.trust_state = 'TRUSTED'`` — the BINARY-trust system of record (the
    consumable predicate downstream agents gate on, ``quantlib.features.trusted_list``), NOT the older
    ``lifecycle_state`` column the grid badge uses. Isolated so tests can monkeypatch it without a DB."""
    import psycopg

    from quantlib.features.validation_db import DB_KWARGS

    with psycopg.connect(**DB_KWARGS, connect_timeout=5) as conn, conn.cursor() as cur:
        cur.execute(_TRUSTED_NAMES_QUERY)
        return [str(row[0]) for row in cur.fetchall()]


def trusted_feature_names() -> set[str]:
    """{feature} that have EARNED binary trust (``trust_state = 'TRUSTED'``) — the trusted side of the frontier."""
    return set(_read_trusted_names())


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


# How many symbols to return per source in the symbol-depth surface. The per-symbol depth scan is the full
# per-date set read (heavier than the latest-date-only /symbols pass), so the response caps the listed
# symbols; the summary counts are over ALL symbols, only the per-symbol rows are capped.
SYMBOL_DEPTH_DEFAULT_LIMIT = 200


def _per_date_symbol_sets(root: str, group: str, version: str, source: str) -> dict[str, set[str]]:
    """Per-date distinct-symbol SETS for one (group, source) over its WHOLE history — like
    ``gather_group_store_info`` but RETAINING the sets (it keeps only counts). Same bounded per-partition
    file sampling, so a 7k-file stream partition is still ~12 reads. {} when the source has no partitions."""
    base = Path(root) / f"group={group}" / f"v={version}" / f"source={source}"
    if not base.exists():
        return {}
    per_date: dict[str, set[str]] = {}
    for date_dir in sorted(base.glob("date=*")):
        date_iso = date_dir.name.removeprefix("date=")
        per_date[date_iso] = _read_symbols(root, group, version, source, date_iso)
    return per_date


def _invert_to_symbol_depth(per_date: dict[str, set[str]]) -> dict[str, dict[str, object]]:
    """Invert per-date symbol sets → per-symbol depth: each symbol's earliest date, latest date, and the
    number of dates it is present on. A symbol present on the earliest captured date has FULL history depth;
    one that first appears recently is shallow (a late-listed / late-subscribed ticker)."""
    by_symbol: dict[str, dict[str, object]] = {}
    for date_iso in sorted(per_date):
        for symbol in per_date[date_iso]:
            row = by_symbol.get(symbol)
            if row is None:
                by_symbol[symbol] = {"earliest": date_iso, "latest": date_iso, "n_dates": 1}
            else:
                row["latest"] = date_iso
                row["n_dates"] = int(row["n_dates"]) + 1  # type: ignore[arg-type]
    return by_symbol


def build_symbol_depth(
    group: str, root: str = STORE_ROOT, limit: int = SYMBOL_DEPTH_DEFAULT_LIMIT
) -> dict[str, object]:
    """Per-SYMBOL coverage DEPTH for one group: for each ticker, HOW FAR BACK its data goes (earliest →
    latest date + span + dates-present) PER SOURCE (stream vs backfill).

    This is the time-DEPTH cut the other surfaces don't give. The group grid (``/api/feature-grid``) is per
    (group × period) counts; ``/symbols`` is per-symbol but only on the LATEST date (no depth); the
    ``/timeline`` is depth but only at the GROUP level. This is their intersection — *which TICKER has this
    FEATURE, how far back, and from which source* — the visible answer to "how deep/broad is our tape per
    feature" as DataIntegrity deepens the quote/trade tape.

    Each symbol is classified by where it has history: ``both`` (stream + backfill), ``backfill_only``
    (settled history but not captured live — under-represented LIVE), ``stream_only`` (live but no settled
    backfill yet — not parity-checkable). Per source a symbol carries its OWN earliest/latest/span/n_dates,
    so a ticker that backfills to 2025-05 but only streams the last 4 days reads exactly that.

    Read-side only: one bounded per-partition symbol read across the group's dates per source (same sampling
    the grid uses). ``limit`` caps the per-symbol ROWS returned (ranked shallowest-backfill first — the names
    whose history is thinnest); summary counts + spans are over ALL symbols.
    """
    if group not in _catalog_by_group():
        raise KeyError(group)
    version = _group_version(group)
    if version is None:
        raise KeyError(group)

    stream_by_date = _per_date_symbol_sets(root, group, version, "stream")
    backfill_by_date = _per_date_symbol_sets(root, group, version, "backfill")
    stream_depth = _invert_to_symbol_depth(stream_by_date)
    backfill_depth = _invert_to_symbol_depth(backfill_by_date)

    stream_symbols = set(stream_depth)
    backfill_symbols = set(backfill_depth)
    union = stream_symbols | backfill_symbols
    both = stream_symbols & backfill_symbols
    backfill_only = backfill_symbols - stream_symbols
    stream_only = stream_symbols - backfill_symbols

    stream_dates = sorted(stream_by_date)
    backfill_dates = sorted(backfill_by_date)

    rows = [
        _symbol_depth_row(symbol, stream_depth.get(symbol), backfill_depth.get(symbol)) for symbol in union
    ]
    # Rank shallowest-backfill first (fewest backfill dates = least settled history), then by symbol — the
    # names whose tape is thinnest are what this surface exists to flag. Symbols with NO backfill sort first.
    rows.sort(key=lambda row: (int(row["backfill_n_dates"]), str(row["symbol"])))  # type: ignore[arg-type]

    return {
        "group": group,
        "version": version,
        "n_symbols": len(union),
        "n_both": len(both),
        "n_backfill_only": len(backfill_only),
        "n_stream_only": len(stream_only),
        "stream_earliest": stream_dates[0] if stream_dates else None,
        "stream_latest": stream_dates[-1] if stream_dates else None,
        "stream_n_dates": len(stream_dates),
        "backfill_earliest": backfill_dates[0] if backfill_dates else None,
        "backfill_latest": backfill_dates[-1] if backfill_dates else None,
        "backfill_n_dates": len(backfill_dates),
        "limit": limit,
        "n_shown": min(limit, len(rows)),
        "symbols": rows[:limit],
    }


def _span_days(earliest: str | None, latest: str | None) -> int:
    """Calendar-day span (inclusive) between two ISO dates, or 0 when either is missing."""
    if earliest is None or latest is None:
        return 0
    return (dt.date.fromisoformat(latest) - dt.date.fromisoformat(earliest)).days + 1


def _symbol_depth_row(
    symbol: str, stream: dict[str, object] | None, backfill: dict[str, object] | None
) -> dict[str, object]:
    """One per-symbol depth row: provenance class + each source's earliest/latest/span/n_dates (nulls when a
    source has no history for the symbol)."""
    if stream is not None and backfill is not None:
        provenance = "both"
    elif backfill is not None:
        provenance = "backfill_only"  # settled history, not captured live → under-represented LIVE
    else:
        provenance = "stream_only"  # live but no settled backfill → not yet parity-checkable
    stream_earliest = stream["earliest"] if stream else None
    stream_latest = stream["latest"] if stream else None
    backfill_earliest = backfill["earliest"] if backfill else None
    backfill_latest = backfill["latest"] if backfill else None
    return {
        "symbol": symbol,
        "provenance": provenance,
        "stream_earliest": stream_earliest,
        "stream_latest": stream_latest,
        "stream_span_days": _span_days(stream_earliest, stream_latest),  # type: ignore[arg-type]
        "stream_n_dates": int(stream["n_dates"]) if stream else 0,  # type: ignore[arg-type]
        "backfill_earliest": backfill_earliest,
        "backfill_latest": backfill_latest,
        "backfill_span_days": _span_days(backfill_earliest, backfill_latest),  # type: ignore[arg-type]
        "backfill_n_dates": int(backfill["n_dates"]) if backfill else 0,  # type: ignore[arg-type]
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


# Default span of the recent-day presence grid: enough trading days to read the live-vs-backfill landing
# pattern at a glance without a wall of columns. Calendar days (weekends render as absent on both sources,
# which is the honest "no session" read), capped so the page stays one cheap pass.
TIMELINE_DEFAULT_DAYS = 21
TIMELINE_MAX_DAYS = 120


def _day_provenance(stream_n: int, backfill_n: int) -> str:
    """Classify one (group, day) by which sources landed: both / stream-only (not yet parity-checkable) /
    backfill-only (settled, no live capture that day) / absent (neither source has the day)."""
    if stream_n > 0 and backfill_n > 0:
        return "both"
    if stream_n > 0:
        return "stream_only"
    if backfill_n > 0:
        return "backfill_only"
    return "absent"


def _stream_horizon_days(stream_dates: list[str], anchor: dt.date) -> int:
    """The live-coverage HORIZON: how many of the most recent WEEKDAYS (ending at ``anchor``) the stream
    actually captured, walking back until the first weekday gap. Weekends are skipped (no session expected),
    so a Mon-Fri capture streak reads as its true depth across a weekend. Answers "how far back does live
    coverage reach unbroken" — the live counterpart to backfill's history depth."""
    captured = set(stream_dates)
    horizon = 0
    day = anchor
    while True:
        if day.weekday() < 5:
            if day.isoformat() not in captured:
                break
            horizon += 1
        day -= dt.timedelta(days=1)
        if day < anchor - dt.timedelta(days=TIMELINE_MAX_DAYS):
            break
    return horizon


def build_coverage_timeline(root: str = STORE_ROOT, days: int = TIMELINE_DEFAULT_DAYS) -> dict[str, object]:
    """A (group x recent-day x source) PRESENCE grid + per-group DEPTH stats — the time/depth legibility view.

    The group grid collapses every multi-day row onto a single coverage %; the per-group detail lists raw date
    arrays. Neither answers, at a glance, "on each of the last N days, did stream and/or backfill land for this
    group, and how deep does each source's history reach". This surface does:

      * ``days`` columns, most-recent-first, ending at the latest store date. Each (group, day) cell carries
        ``stream``/``backfill`` symbol counts and a provenance class (both / stream_only / backfill_only /
        absent) — so live-vs-backfill provenance per (group, day) reads off the grid directly.
      * Per-group DEPTH: ``backfill_earliest`` + ``backfill_span_days`` (how far back history reaches) and
        ``stream_horizon_days`` (how many recent weekdays the live stream captured unbroken) — history depth
        and live horizon side by side.

    Read-side only: reuses ``gather_group_store_info`` (the SAME one-pass per-date symbol read the grid pays
    for), so this is no extra store I/O beyond the grid's, and shares nothing's source of truth.
    """
    catalog_by_group = _catalog_by_group()
    groups = sorted(catalog_by_group)

    infos: dict[str, _GroupStoreInfo] = {}
    for group in groups:
        version = _group_version(group)
        if version is None:
            continue
        infos[group] = gather_group_store_info(root, group, version)

    anchor = latest_store_date(infos)
    floor = earliest_store_date(infos)
    span = max(1, min(days, TIMELINE_MAX_DAYS))

    if anchor is None:
        return {
            "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "store_root": root,
            "anchor_date": None,
            "earliest_date": None,
            "days": span,
            "dates": [],
            "groups": [],
        }

    timeline_dates = [(anchor - dt.timedelta(days=offset)).isoformat() for offset in range(span)]

    group_rows: list[dict[str, object]] = []
    for group in groups:
        info = infos.get(group)
        if info is None:
            continue
        stream_per_date = {d: n for d, n in info.per_date_symbols.get("stream", {}).items() if n > 0}
        backfill_per_date = {d: n for d, n in info.per_date_symbols.get("backfill", {}).items() if n > 0}
        stream_dates = sorted(stream_per_date)
        backfill_dates = sorted(backfill_per_date)

        day_cells: list[dict[str, object]] = []
        for date_iso in timeline_dates:
            stream_n = stream_per_date.get(date_iso, 0)
            backfill_n = backfill_per_date.get(date_iso, 0)
            day_cells.append(
                {
                    "date": date_iso,
                    "stream": stream_n,
                    "backfill": backfill_n,
                    "provenance": _day_provenance(stream_n, backfill_n),
                }
            )

        backfill_earliest = backfill_dates[0] if backfill_dates else None
        backfill_latest = backfill_dates[-1] if backfill_dates else None
        backfill_span = (
            (dt.date.fromisoformat(backfill_latest) - dt.date.fromisoformat(backfill_earliest)).days + 1
            if backfill_earliest and backfill_latest
            else 0
        )
        group_rows.append(
            {
                "group": group,
                "version": info.version,
                "layer": (catalog_by_group[group][0]["layer"] if catalog_by_group[group] else None),
                "n_features": len(catalog_by_group[group]),
                "backfill_earliest": backfill_earliest,
                "backfill_latest": backfill_latest,
                "backfill_span_days": backfill_span,
                "stream_earliest": stream_dates[0] if stream_dates else None,
                "stream_latest": stream_dates[-1] if stream_dates else None,
                "stream_horizon_days": _stream_horizon_days(stream_dates, anchor),
                "days": day_cells,
            }
        )

    return {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "store_root": root,
        "anchor_date": anchor.isoformat(),
        "earliest_date": floor.isoformat() if floor else None,
        "days": span,
        # Most-recent first so the freshest day is the leftmost column next to the group label.
        "dates": timeline_dates,
        "groups": sorted(group_rows, key=lambda gr: str(gr["group"])),
    }


def _gather_stream_symbols_by_date(
    root: str, group: str, version: str, dates: list[str]
) -> dict[str, set[str]]:
    """Per-date STREAM symbol SETS for one group, restricted to ``dates``. Same bounded per-partition read
    (``_read_symbols`` -> evenly-spaced file sample) the grid/timeline already pay; here the SETS are RETAINED
    (the timeline keeps only the counts) so the union across groups can be taken per day. Only the requested
    recent ``dates`` are read, so this is at most ``len(dates)`` partition touches per group, not the whole
    history — cheaper than the timeline's full-history count pass."""
    out: dict[str, set[str]] = {}
    for date_iso in dates:
        symbols = _read_symbols(root, group, version, "stream", date_iso)
        if symbols:
            out[date_iso] = symbols
    return out


def build_orderflow_coverage_trend(
    root: str = STORE_ROOT, days: int = TIMELINE_DEFAULT_DAYS
) -> dict[str, object]:
    """Per-recent-day LIVE-stream breadth across the order-flow groups — is FP_TICK_SYMBOLS WIDENING or STALLING?

    The timeline grid (build_coverage_timeline) shows per (group x day) presence + counts, and the per-symbol
    surfaces (#121 /symbols, #127 thin-live) show WHICH names are thin on the LATEST day. Neither answers the
    trend question Ben needs for the universe-wide live order-flow certification: across the tick-derived
    groups, how many DISTINCT symbols did the live stream actually carry on each of the last N days, and is
    that union climbing off the ~24-canary floor or flat?

    For each recent day this surface reports, over ``ORDERFLOW_GROUPS`` present on disk:
      * ``union`` — distinct symbols live on the stream in AT LEAST ONE order-flow group (the widest live
        order-flow universe that day; the headline trend number).
      * ``intersection`` — symbols live in EVERY order-flow group that captured anything that day (the names
        with FULL order-flow coverage — the tradeable-live core).
      * per-group stream counts, so a single thin group is visible against the union.

    Read-side only: reuses ``_read_symbols`` over just the recent ``days`` window per order-flow group (same
    bounded sampling, no extra heavy I/O beyond what the grid already does — and only the recent slice, not
    the full history the timeline scans). NO schema/format change.
    """
    catalog_by_group = _catalog_by_group()
    present_groups = [group for group in ORDERFLOW_GROUPS if group in catalog_by_group]

    versions: dict[str, str] = {}
    for group in present_groups:
        version = _group_version(group)
        if version is not None:
            versions[group] = version
    present_groups = [group for group in present_groups if group in versions]

    span = max(1, min(days, TIMELINE_MAX_DAYS))

    anchor: dt.date | None = None
    for group in present_groups:
        latest = _latest_partition_date(root, group, versions[group], "stream")
        if latest is not None:
            day = dt.date.fromisoformat(latest)
            if anchor is None or day > anchor:
                anchor = day

    if anchor is None:
        return {
            "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "store_root": root,
            "anchor_date": None,
            "days": span,
            "groups": sorted(present_groups),
            "dates": [],
            "trend": [],
        }

    timeline_dates = [(anchor - dt.timedelta(days=offset)).isoformat() for offset in range(span)]

    # {group: {date: stream symbol set}} over only the recent window — the single store read of this surface.
    per_group_sets: dict[str, dict[str, set[str]]] = {}
    for group in present_groups:
        per_group_sets[group] = _gather_stream_symbols_by_date(root, group, versions[group], timeline_dates)

    trend: list[dict[str, object]] = []
    for date_iso in timeline_dates:
        day_sets = [
            per_group_sets[group][date_iso]
            for group in present_groups
            if date_iso in per_group_sets[group]
        ]
        live_groups = [
            group for group in present_groups if date_iso in per_group_sets[group]
        ]
        if day_sets:
            union = set.union(*day_sets)
            # Intersection over only the groups that captured anything that day (an absent group must not
            # zero out the full-coverage core just because it had no session row).
            intersection = set.intersection(*day_sets)
        else:
            union = set()
            intersection = set()
        trend.append(
            {
                "date": date_iso,
                "n_union": len(union),
                "n_intersection": len(intersection),
                "n_live_groups": len(live_groups),
                "per_group": {group: len(per_group_sets[group][date_iso]) for group in live_groups},
            }
        )

    # First vs last captured day in the window -> a single widening/stalling verdict for the header.
    captured = [row for row in trend if int(row["n_union"]) > 0]
    if len(captured) >= 2:
        # trend is most-recent-first, so captured[-1] is the OLDEST captured day in the window.
        oldest_union = int(captured[-1]["n_union"])
        newest_union = int(captured[0]["n_union"])
        union_delta = newest_union - oldest_union
    else:
        oldest_union = newest_union = union_delta = 0

    return {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "store_root": root,
        "anchor_date": anchor.isoformat(),
        "days": span,
        "groups": sorted(present_groups),
        # Most-recent first (matches the timeline surface) so the freshest day reads leftmost.
        "dates": timeline_dates,
        "trend": trend,
        "newest_captured_union": newest_union,
        "oldest_captured_union": oldest_union,
        # > 0 widening, 0 flat, < 0 shrinking — the FP_TICK_SYMBOLS coverage direction at a glance.
        "union_delta": union_delta,
    }


def _frontier_state(is_trusted: bool, has_open_defect: bool) -> str:
    """Classify one feature into a trust-frontier state from its binary-trust grant + open-defect presence.

    Already TRUSTED (``trust_state='TRUSTED'``) -> TRUSTED. Otherwise an OPEN parity defect -> BLOCKED (a
    failure still on the books). A not-yet-trusted feature with NO open defect -> ELIGIBLE: it advances to
    TRUSTED on the next clean settled sweep. This is where a feature whose defect was cleared lands — its flat
    DIVERGENT lifecycle badge would paint it permanently red, but with no open defect it is one clean sweep
    from trusted."""
    if is_trusted:
        return FRONTIER_TRUSTED
    if has_open_defect:
        return FRONTIER_BLOCKED
    return FRONTIER_ELIGIBLE


def build_trust_frontier() -> dict[str, object]:
    """The TRUST FRONTIER: how close the feature set is to fully trusted, split TRUSTED / ELIGIBLE / BLOCKED.

    The binary-trust badge (``trust_state``) plus the flat DIVERGENT lifecycle badge cannot show that a
    not-yet-trusted feature whose parity defect has been CLEARED is one clean sweep from TRUSTED. This view
    joins the binary-trust set (``trust_state='TRUSTED'`` — the consumable predicate downstream agents gate on)
    against the OPEN rows of ``feature_parity_defect`` (both read-only, no new source of truth) to surface that
    frontier: ELIGIBLE = not-yet-trusted with no open defect (advances on the next clean sweep), BLOCKED =
    still has an open parity defect (needs a fix).

    Scoped to the CURRENT registry catalog, so superseded older feature versions in the DB do not inflate the
    counts (the frontier reflects the live feature set, matching the fingerprint).

    Shape:
      {generated_at, n_features, n_trusted, n_eligible, n_blocked, n_open_defects,
       trusted_pct, eligible_pct, blocked_pct, projected_trusted_pct,
       groups: [{group, layer, n_features, n_trusted, n_eligible, n_blocked,
                 trusted_pct, projected_trusted_pct, blocked_features: [...]}]}
    ``projected_trusted_pct`` = (trusted + eligible) / total: where trust lands if every eligible feature
    earns trust on the next clean sweep (the headline of the coming jump)."""
    catalog_by_group = _catalog_by_group()
    trusted = trusted_feature_names()
    blocked = open_defect_features()

    group_rows: list[dict[str, object]] = []
    total = 0
    total_trusted = 0
    total_eligible = 0
    total_blocked = 0

    for group in sorted(catalog_by_group):
        features = catalog_by_group[group]
        n_trusted = 0
        n_eligible = 0
        n_blocked = 0
        blocked_features: list[str] = []
        for record in features:
            name = str(record["feature"])
            state = _frontier_state(name in trusted, name in blocked)
            if state == FRONTIER_TRUSTED:
                n_trusted += 1
            elif state == FRONTIER_BLOCKED:
                n_blocked += 1
                blocked_features.append(name)
            else:
                n_eligible += 1

        n_features = len(features)
        total += n_features
        total_trusted += n_trusted
        total_eligible += n_eligible
        total_blocked += n_blocked
        group_rows.append(
            {
                "group": group,
                "layer": (features[0]["layer"] if features else None),
                "n_features": n_features,
                "n_trusted": n_trusted,
                "n_eligible": n_eligible,
                "n_blocked": n_blocked,
                "trusted_pct": round(100.0 * n_trusted / n_features, 1) if n_features else 0.0,
                "projected_trusted_pct": (
                    round(100.0 * (n_trusted + n_eligible) / n_features, 1) if n_features else 0.0
                ),
                "blocked_features": sorted(blocked_features),
            }
        )

    return {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "n_features": total,
        "n_trusted": total_trusted,
        "n_eligible": total_eligible,
        "n_blocked": total_blocked,
        "n_open_defects": len(blocked),
        "trusted_pct": round(100.0 * total_trusted / total, 1) if total else 0.0,
        "eligible_pct": round(100.0 * total_eligible / total, 1) if total else 0.0,
        "blocked_pct": round(100.0 * total_blocked / total, 1) if total else 0.0,
        "projected_trusted_pct": (
            round(100.0 * (total_trusted + total_eligible) / total, 1) if total else 0.0
        ),
        # Groups ranked most-blocked-first so the genuinely-stuck families (the tick tail) surface on top.
        "groups": sorted(group_rows, key=lambda row: (-int(row["n_blocked"]), str(row["group"]))),
    }


class GridCache:
    """Tiny TTL cache so a page load / refresh re-aggregates at most every ``ttl`` seconds. The grid read is
    1-2s on the live store; a 60s TTL makes a busy refresh instant while staying fresh enough for a coverage
    surface that only changes on the daily lifecycle write."""

    def __init__(self, ttl: float = 60.0) -> None:
        self.ttl = ttl
        self._grid: dict[str, object] | None = None
        self._grid_at: float = 0.0
        self._frontier: dict[str, object] | None = None
        self._frontier_at: float = 0.0
        self._details: dict[str, tuple[float, dict[str, object]]] = {}
        self._symbols: dict[str, tuple[float, dict[str, object]]] = {}
        self._thin: dict[int, tuple[float, dict[str, object]]] = {}
        self._timeline: dict[int, tuple[float, dict[str, object]]] = {}
        self._oflow_trend: dict[int, tuple[float, dict[str, object]]] = {}
        self._symbol_depth: dict[tuple[str, int], tuple[float, dict[str, object]]] = {}

    def grid(self, root: str, force: bool = False) -> dict[str, object]:
        now = time.monotonic()
        if force or self._grid is None or (now - self._grid_at) > self.ttl:
            self._grid = build_grid(root)
            self._grid_at = now
        return self._grid

    def frontier(self, force: bool = False) -> dict[str, object]:
        now = time.monotonic()
        if force or self._frontier is None or (now - self._frontier_at) > self.ttl:
            self._frontier = build_trust_frontier()
            self._frontier_at = now
        return self._frontier

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

    def timeline(
        self, root: str, days: int = TIMELINE_DEFAULT_DAYS, force: bool = False
    ) -> dict[str, object]:
        now = time.monotonic()
        cached = self._timeline.get(days)
        if not force and cached is not None and (now - cached[0]) <= self.ttl:
            return cached[1]
        view = build_coverage_timeline(root, days)
        self._timeline[days] = (now, view)
        return view

    def orderflow_trend(
        self, root: str, days: int = TIMELINE_DEFAULT_DAYS, force: bool = False
    ) -> dict[str, object]:
        now = time.monotonic()
        cached = self._oflow_trend.get(days)
        if not force and cached is not None and (now - cached[0]) <= self.ttl:
            return cached[1]
        view = build_orderflow_coverage_trend(root, days)
        self._oflow_trend[days] = (now, view)
        return view

    def symbol_depth(
        self, group: str, root: str, limit: int = SYMBOL_DEPTH_DEFAULT_LIMIT, force: bool = False
    ) -> dict[str, object]:
        now = time.monotonic()
        key = (group, limit)
        cached = self._symbol_depth.get(key)
        if not force and cached is not None and (now - cached[0]) <= self.ttl:
            return cached[1]
        view = build_symbol_depth(group, root, limit)
        self._symbol_depth[key] = (now, view)
        return view


CACHE = GridCache()
