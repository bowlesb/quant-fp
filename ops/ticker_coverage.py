"""Read-only PER-TICKER feature-coverage report over the feature store.

The dashboard's coverage surfaces are GROUP-centric ("for THIS group, which symbols are covered?") and the
companion ``ops/analyze_ticker_representation.py`` is AGGREGATE symbol-centric ("across ALL symbols, which
are under-represented?"). Neither answers the single most operationally useful question for one name:

    "For THIS ticker, exactly which features does it have, how far back, from the live stream vs settled
     backfill — and what is the trust/lifecycle state of those features?"

This fills that gap. Given one symbol it walks every feature group and reports, per group:

  * PRESENCE   — is the symbol in this group's stream partitions, its backfill partitions, or both?
  * HISTORY    — the earliest and latest BACKFILL date the symbol is observed on, and the span in days
                 (a tight UPPER bound on its true first appearance: it can only be earlier than the
                 earliest SAMPLED date it was present on).
  * FEATURES   — the feature columns that group's partitions carry (features in a group are co-captured in
                 the same partition, so a symbol's feature coverage equals its group coverage). Read from
                 the parquet SCHEMA, so the feature list needs no feature-engine import.
  * TRUST      — each covered feature's binary ``trust_state`` (TRUSTED) and clean-day ``lifecycle_state``
                 (incl. DIVERGENT) from the ``feature_trust`` table, when a trust reader is supplied.

plus per-ticker roll-ups: BREADTH (groups covered / total), the LIVE GAP (groups settled in backfill but
absent from the live stream — the FP_TICK_SYMBOLS gap for this one name), the SHALLOWEST-history groups,
and a trust tally over the symbol's covered features.

PURE READ — it reads partition directory names + a bounded SAMPLE of ``symbol`` columns + the parquet
schema + (optionally) the trust table. It NEVER writes the store, changes schema/format, or touches a
fingerprint. The store walk is factored behind the ``StoreReader`` protocol so the roll-up + trust-join
logic is unit-tested with an in-memory fake (no /store mount, no DB) — only the concrete
``PartitionStoreReader`` touches disk and must run in a container with polars + the store mounted read-only
(the host has neither). The store-only path needs ONLY polars + /store (no feature engine); the optional
``--with-trust`` join additionally needs the trust DB, so run it where ``quantlib`` is importable::

    # store-only coverage (slim env, no engine):
    docker exec -w /app feature-computer python ops/ticker_coverage.py AAPL --store /store

    # + per-feature TRUSTED/DIVERGENT join (needs quantlib on the path + DB env):
    docker exec -w /app -e PYTHONPATH=/app feature-computer \
        python ops/ticker_coverage.py AAPL --store /store --with-trust

Output is human-readable text by default, or ``--json`` for a machine-readable blob.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import polars as pl

# Per-minute stream partitions hold thousands of ``data-<shard>-<epoch>.parquet`` files; the symbol universe
# is ~constant across a day's files, so an evenly-spaced bounded sample of the ``symbol`` column recovers the
# partition's symbol SET at a fixed cost. Backfill partitions are a single settled file.
MAX_FILES_PER_PARTITION = 12

# The stream only spans the last few captured days, so stream presence is read over a recent window only
# (a wider read finds nothing extra and costs more).
STREAM_WINDOW_DAYS = 7

# Reading the symbol set on EVERY date of a multi-year backfill group is the expensive part. History uses a
# bounded date sample: the earliest+latest edges (so the reach bound is as tight as the data allows) plus an
# evenly-spaced interior spread (so a symbol that appears only mid-history is still found).
HISTORY_EDGE_DATES = 6
HISTORY_INTERIOR_DATES = 8

# Partition columns that are NOT features: the row keys every group partition carries.
NON_FEATURE_COLUMNS = frozenset({"symbol", "minute", "ts", "timestamp", "date"})

DEFAULT_STORE_ROOT = os.environ.get("STORE_ROOT", "/store")


@dataclass
class FeatureTrust:
    """One feature's trust evidence from ``feature_trust`` (the join target for the per-feature view)."""

    feature: str
    trust_state: str  # TRUSTED | NON_TRUSTED
    lifecycle_state: str | None  # DIVERGENT | PENDING | VALIDATED | RETIRED | None (never swept)


@dataclass
class GroupCoverage:
    """One feature-group's coverage for the target symbol: presence, history reach, and its feature columns.

    GROUP-GRANULAR, not per-feature-verified. Coverage is decided at the GROUP level (is the symbol's row
    present in the group's partitions?) and the listed ``features`` are the group's columns — claimed covered
    on the assumption that a group's features are CO-CAPTURED in the same (group, version, source, date)
    partition, so any feature in the group shares the symbol's group-level presence. That holds for the
    current store layout (one partition per group carries all its feature columns). It is NOT a per-feature
    value check: this report does not assert each individual feature column is non-null for the symbol, only
    that the symbol is present in the group that produces them."""

    group: str
    version: str
    in_stream: bool
    in_backfill: bool
    earliest_backfill_date: str | None
    latest_backfill_date: str | None
    features: list[str] = field(default_factory=list)

    @property
    def covered(self) -> bool:
        return self.in_stream or self.in_backfill

    @property
    def backfill_only(self) -> bool:
        """Settled in backfill but absent from the live stream — the per-ticker FP_TICK_SYMBOLS live gap."""
        return self.in_backfill and not self.in_stream

    @property
    def backfill_span_days(self) -> int:
        if self.earliest_backfill_date is None or self.latest_backfill_date is None:
            return 0
        first = dt.date.fromisoformat(self.earliest_backfill_date)
        last = dt.date.fromisoformat(self.latest_backfill_date)
        return (last - first).days


class StoreReader(Protocol):
    """The bounded store-access surface the report needs. A real implementation reads partitions off disk;
    a fake implements the same four methods in memory so the roll-up logic is tested without a store."""

    def list_groups(self) -> list[str]: ...

    def group_version(self, group: str) -> str | None: ...

    def partition_dates(self, group: str, version: str, source: str) -> list[str]: ...

    def symbols_on_date(self, group: str, version: str, source: str, date_iso: str) -> set[str]: ...

    def group_features(self, group: str, version: str) -> list[str]: ...


def _sample_files(files: list[Path], cap: int) -> list[Path]:
    """Up to ``cap`` evenly-spaced files (always first + last) — bounds the per-minute-file symbol read."""
    if len(files) <= cap:
        return files
    step = (len(files) - 1) / (cap - 1)
    indices = sorted({round(i * step) for i in range(cap)})
    return [files[i] for i in indices]


def sample_history_dates(dates: list[str]) -> list[str]:
    """A bounded set of dates for the history pass: the earliest+latest edges plus an interior spread."""
    if len(dates) <= 2 * HISTORY_EDGE_DATES + HISTORY_INTERIOR_DATES:
        return dates
    edges = dates[:HISTORY_EDGE_DATES] + dates[-HISTORY_EDGE_DATES:]
    interior = dates[HISTORY_EDGE_DATES:-HISTORY_EDGE_DATES]
    step = (len(interior) - 1) / (HISTORY_INTERIOR_DATES - 1)
    sampled_interior = [interior[round(i * step)] for i in range(HISTORY_INTERIOR_DATES)]
    return sorted(set(edges + sampled_interior))


class PartitionStoreReader:
    """Concrete ``StoreReader`` over the on-disk ``group=/v=/source=/date=`` partition tree (polars + /store)."""

    def __init__(self, root: str) -> None:
        self.root = Path(root)

    def list_groups(self) -> list[str]:
        return sorted(
            path.name.removeprefix("group=") for path in self.root.glob("group=*") if path.is_dir()
        )

    def group_version(self, group: str) -> str | None:
        versions = sorted((self.root / f"group={group}").glob("v=*"))
        if not versions:
            return None
        # A group has a single active version on disk; if several, the lexically-latest is the current one.
        return versions[-1].name.removeprefix("v=")

    def partition_dates(self, group: str, version: str, source: str) -> list[str]:
        base = self.root / f"group={group}" / f"v={version}" / f"source={source}"
        if not base.exists():
            return []
        return sorted(path.name.removeprefix("date=") for path in base.glob("date=*") if path.is_dir())

    def symbols_on_date(self, group: str, version: str, source: str, date_iso: str) -> set[str]:
        partition = self.root / f"group={group}" / f"v={version}" / f"source={source}" / f"date={date_iso}"
        files = sorted(partition.glob("data*.parquet"))
        if not files:
            return set()
        symbols: set[str] = set()
        for path in _sample_files(files, MAX_FILES_PER_PARTITION):
            symbols.update(pl.read_parquet(path, columns=["symbol"])["symbol"].to_list())
        return symbols

    def group_features(self, group: str, version: str) -> list[str]:
        """The group's feature columns, read from a partition's parquet SCHEMA (no feature-engine import).

        Prefers a settled backfill file (one settled file, cheap); falls back to a stream file. Excludes the
        row-key columns. Returns [] if the group has no readable partition file."""
        for source in ("backfill", "stream"):
            for date_iso in reversed(self.partition_dates(group, version, source)):
                partition = (
                    self.root / f"group={group}" / f"v={version}" / f"source={source}" / f"date={date_iso}"
                )
                files = sorted(partition.glob("data*.parquet"))
                if not files:
                    continue
                schema = pl.read_parquet_schema(str(files[0]))
                return [name for name in schema if name not in NON_FEATURE_COLUMNS]
        return []


def _stream_window_dates(dates: list[str], window_days: int) -> list[str]:
    """The subset of ``dates`` within ``window_days`` of the latest date (where the live stream lives)."""
    if not dates:
        return []
    anchor = dt.date.fromisoformat(dates[-1])
    cutoff = (anchor - dt.timedelta(days=window_days)).isoformat()
    return [date_iso for date_iso in dates if date_iso >= cutoff]


def build_group_coverage(reader: StoreReader, symbol: str, group: str) -> GroupCoverage | None:
    """Walk one group for the target symbol: stream presence (recent window), backfill presence + history
    reach (bounded date sample), and the group's feature columns. Returns None if the group has no active
    version on disk."""
    version = reader.group_version(group)
    if version is None:
        return None

    stream_dates = _stream_window_dates(reader.partition_dates(group, version, "stream"), STREAM_WINDOW_DAYS)
    in_stream = any(
        symbol in reader.symbols_on_date(group, version, "stream", date_iso) for date_iso in stream_dates
    )

    backfill_dates = reader.partition_dates(group, version, "backfill")
    present_dates = [
        date_iso
        for date_iso in sample_history_dates(backfill_dates)
        if symbol in reader.symbols_on_date(group, version, "backfill", date_iso)
    ]
    in_backfill = bool(present_dates)
    earliest = min(present_dates) if present_dates else None
    latest = max(present_dates) if present_dates else None

    features = reader.group_features(group, version) if (in_stream or in_backfill) else []
    return GroupCoverage(
        group=group,
        version=version,
        in_stream=in_stream,
        in_backfill=in_backfill,
        earliest_backfill_date=earliest,
        latest_backfill_date=latest,
        features=sorted(features),
    )


def build_ticker_coverage(reader: StoreReader, symbol: str) -> list[GroupCoverage]:
    """Every group's coverage for ``symbol``, covered groups first (then alphabetical). Pure over the reader,
    so it is unit-tested with an in-memory fake store."""
    coverages: list[GroupCoverage] = []
    for group in reader.list_groups():
        coverage = build_group_coverage(reader, symbol, group)
        if coverage is not None:
            coverages.append(coverage)
    coverages.sort(key=lambda item: (not item.covered, item.group))
    return coverages


def covered_features(coverages: list[GroupCoverage]) -> list[str]:
    """The flat set of feature names the symbol is covered for, across all its covered groups (sorted)."""
    names: set[str] = set()
    for coverage in coverages:
        if coverage.covered:
            names.update(coverage.features)
    return sorted(names)


def summarize_trust(feature_names: list[str], trust_by_feature: dict[str, FeatureTrust]) -> dict[str, int]:
    """Tally the trust/lifecycle state over the symbol's covered features: how many are TRUSTED, DIVERGENT,
    or have no trust row yet. A feature absent from ``feature_trust`` counts as untracked (never swept)."""
    tally = {"total": len(feature_names), "trusted": 0, "divergent": 0, "untracked": 0}
    for name in feature_names:
        record = trust_by_feature.get(name)
        if record is None:
            tally["untracked"] += 1
            continue
        if record.trust_state == "TRUSTED":
            tally["trusted"] += 1
        if record.lifecycle_state == "DIVERGENT":
            tally["divergent"] += 1
    return tally


def build_report(
    reader: StoreReader, symbol: str, trust_by_feature: dict[str, FeatureTrust] | None = None
) -> dict[str, object]:
    """The full per-ticker coverage report as a JSON-able dict. Pure over its inputs (reader + trust map),
    so the whole assembly is unit-tested without a store or a DB."""
    coverages = build_ticker_coverage(reader, symbol)
    covered = [item for item in coverages if item.covered]
    backfill_only = [item for item in covered if item.backfill_only]
    feature_names = covered_features(coverages)

    shallowest = sorted(
        (item for item in covered if item.in_backfill),
        key=lambda item: item.backfill_span_days,
    )[:10]

    # Denominators are DERIVED at runtime, never hardcoded: ``n_groups_total`` is the count of groups the
    # reader actually finds on disk (the live registry/active-set as materialized in the store), and
    # ``n_features_covered`` is the size of the symbol's own covered-feature set. So if the clean-engine
    # integration shifts the group/feature count, these track it automatically instead of silently rotting.
    report: dict[str, object] = {
        "symbol": symbol,
        "n_groups_total": len(coverages),
        "n_groups_covered": len(covered),
        "n_groups_backfill_only": len(backfill_only),
        "n_features_covered": len(feature_names),
        "backfill_only_groups": [item.group for item in backfill_only],
        "shallowest_history_groups": [
            {
                "group": item.group,
                "span_days": item.backfill_span_days,
                "earliest": item.earliest_backfill_date,
            }
            for item in shallowest
        ],
        "groups": [
            {
                "group": item.group,
                "version": item.version,
                "in_stream": item.in_stream,
                "in_backfill": item.in_backfill,
                "earliest_backfill_date": item.earliest_backfill_date,
                "latest_backfill_date": item.latest_backfill_date,
                "backfill_span_days": item.backfill_span_days,
                "n_features": len(item.features),
                "features": item.features,
            }
            for item in coverages
        ],
    }

    if trust_by_feature is not None:
        report["trust"] = summarize_trust(feature_names, trust_by_feature)
        report["feature_trust"] = [
            {
                "feature": name,
                "trust_state": trust_by_feature[name].trust_state if name in trust_by_feature else None,
                "lifecycle_state": (
                    trust_by_feature[name].lifecycle_state if name in trust_by_feature else None
                ),
            }
            for name in feature_names
        ]

    return report


def read_trust_by_feature() -> dict[str, FeatureTrust]:
    """{feature: FeatureTrust} from the trust DB — the binary grant + clean-day lifecycle state per feature.

    Uses the same env-driven connection contract every other read uses (DB_PASSWORD required). Imported
    locally is NOT needed: psycopg/DB_KWARGS are module-level so import failures fail fast."""
    with _connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT feature, trust_state, lifecycle_state FROM feature_trust")
        return {
            str(feature): FeatureTrust(
                feature=str(feature),
                trust_state=str(trust_state),
                lifecycle_state=str(lifecycle_state) if lifecycle_state is not None else None,
            )
            for feature, trust_state, lifecycle_state in cur.fetchall()
        }


def render_text(report: dict[str, object]) -> str:
    lines: list[str] = []
    symbol = report["symbol"]
    lines.append(f"FEATURE-STORE COVERAGE — {symbol}")
    lines.append("=" * 60)
    lines.append(
        f"groups covered: {report['n_groups_covered']}/{report['n_groups_total']}  ·  "
        f"features covered: {report['n_features_covered']}  ·  "
        f"backfill-only (live gap): {report['n_groups_backfill_only']}"
    )
    trust = report.get("trust")
    if isinstance(trust, dict):
        lines.append(
            f"trust over covered features: {trust['trusted']}/{trust['total']} TRUSTED  ·  "
            f"{trust['divergent']} DIVERGENT  ·  {trust['untracked']} untracked"
        )
    lines.append("")

    backfill_only = report["backfill_only_groups"]
    if isinstance(backfill_only, list) and backfill_only:
        lines.append(
            f"LIVE GAP — settled in backfill but absent from the live stream ({len(backfill_only)}):"
        )
        lines.append("  " + ", ".join(str(group) for group in backfill_only))
        lines.append("")

    lines.append("PER-GROUP COVERAGE (covered first):")
    lines.append(f"  {'group':28} {'src':12} {'history':24} feats")
    groups = report["groups"]
    if isinstance(groups, list):
        for entry in groups:
            assert isinstance(entry, dict)
            if entry["in_stream"] and entry["in_backfill"]:
                source = "stream+bf"
            elif entry["in_stream"]:
                source = "stream"
            elif entry["in_backfill"]:
                source = "backfill"
            else:
                source = "—"
            if entry["in_backfill"]:
                history = f"{entry['earliest_backfill_date']}→{entry['latest_backfill_date']}"
            else:
                history = "—"
            lines.append(f"  {str(entry['group']):28} {source:12} {history:24} {entry['n_features']}")
    return "\n".join(lines)


def _connect() -> Any:
    """The trust-DB connection, using the shared env-driven contract.

    ``psycopg`` and ``quantlib.features.validation_db`` are imported lazily ON PURPOSE: importing
    validation_db runs ``quantlib/features/__init__.py``, which self-registers the whole feature engine (and
    pulls prometheus_client etc.). Keeping it out of module scope means ``ticker_coverage`` imports — and the
    store-only path (no ``--with-trust``) runs — with NO feature-engine dependency, so this tool works in a
    slim store-only environment. (Mirrors the same lazy pattern in the dashboard's lifecycle_state /
    feature_grid and in validation_db's own ``psycopg.connect(**DB_KWARGS)`` call.)"""
    import psycopg

    from quantlib.features.validation_db import DB_KWARGS

    # psycopg.connect's stubbed overloads can't see that DB_KWARGS holds connection kwargs; spread via an
    # untyped mapping so the call type-checks (validation_db itself spreads the same dict the same way).
    connect_kwargs: dict[str, Any] = {**DB_KWARGS, "connect_timeout": 5}
    return psycopg.connect(**connect_kwargs)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Per-ticker feature-store coverage report.")
    parser.add_argument("symbol", help="The ticker to report on, e.g. AAPL.")
    parser.add_argument(
        "--store", default=DEFAULT_STORE_ROOT, help="Feature-store root (default $STORE_ROOT)."
    )
    parser.add_argument(
        "--with-trust",
        action="store_true",
        help="Join the feature_trust table for per-feature TRUSTED/DIVERGENT state (needs DB access).",
    )
    parser.add_argument("--json", action="store_true", help="Emit the machine-readable JSON report.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if not Path(args.store).exists():
        print(f"store root not found: {args.store}", file=sys.stderr)
        return 2
    reader = PartitionStoreReader(args.store)
    trust_by_feature = read_trust_by_feature() if args.with_trust else None
    report = build_report(reader, args.symbol.upper(), trust_by_feature)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(render_text(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
