"""Feature-data coverage surface — "what data does feature X have, is it trusted, what's missing?".

The canonical discovery surface that joins, in one place, the THREE sources of truth so an agent (or the
MA) gets a clean answer without re-deriving any of them:

  * STORE partitions  — ``group=<g>/v=<ver>/source=<stream|backfill>/date=<d>/`` on disk: which dates the
    feature has data for, separately on the STREAM (live) and BACKFILL sides, and the date GAPS.
  * TRUST surface     — the validation agent's binary-trust gate (``feature_trust.trust_state='TRUSTED'``,
    docs/TRUST_REDESIGN.md) via ``trusted_list`` (NOT re-encoded here): is the feature TRUSTED, by what
    reason (deterministic | parity_1day), at what trust value rate.
  * CATALOG           — ``REGISTRY.catalog()``: the feature's group, version, layer, parity_method, owner.

This is the read side of the loop the selective-backfill driver writes: an agent asks "is feature X
trusted AND does its data exist over the window I need, or are there gaps to backfill?" and feeds the
answer back to ``selective_backfill``. Distribution/contract checks live in ``introspect`` (a different
concern — realized value sanity); this module is about DATA COVERAGE.

Usage:
    python -m quantlib.features.feature_data feature microstructure_burst_peak_trades_per_second_1m
    python -m quantlib.features.feature_data group trade_flow --start 2024-12-12 --end 2026-06-16 --json
    python -m quantlib.features.feature_data list                 # all features x group x trust state
    python -m quantlib.features.feature_data trusted              # trusted cohort x backfill coverage
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from typing import cast

import polars as pl

from quantlib.data.raw_backfill import trading_client, trading_days
from quantlib.features import store
from quantlib.features.registry import REGISTRY
from quantlib.features.store import _resolve
from quantlib.features.trusted_list import trusted_features

DEFAULT_ROOT = os.environ.get("STORE_ROOT", "/store")


def trust_by_feature() -> dict[str, dict[str, object]]:
    """{feature_name: trusted_features-row} from the validation agent's binary-trust gate (the TRUSTED
    cohort, ``feature_trust.trust_state='TRUSTED'``). Features absent from the map are not (yet) trusted.
    Reuses ``trusted_list`` so the 'what's trusted' definition lives in ONE place (the trust state machine),
    never re-encoded here.
    """
    return {str(row["feature"]): row for row in trusted_features()}


def covered_dates(root: str, group: str, version: str, source: str) -> list[str]:
    """Sorted ISO dates with a partition on disk for one (group, version, source). The typed primitive the
    coverage + gap logic builds on (kept separate from the display dict so callers get a ``list[str]``).
    """
    return sorted(store._date_dirs(root, group, version, source))


def source_coverage(
    root: str, group: str, version: str, source: str
) -> dict[str, object]:
    """Display dict for one (group, version, source): n_dates + first/last + the full date list."""
    dates = covered_dates(root, group, version, source)
    return {
        "source": source,
        "n_dates": len(dates),
        "first_date": dates[0] if dates else None,
        "last_date": dates[-1] if dates else None,
        "dates": dates,
    }


def deep_source_coverage(
    root: str, group: str, version: str, source: str
) -> list[dict[str, object]]:
    """Per-date (n_rows, n_symbols) for one (group, version, source) — the precise, slower probe."""
    out: list[dict[str, object]] = []
    for date_iso in sorted(store._date_dirs(root, group, version, source)):
        files = list(
            store._partition_dir(root, group, version, source, date_iso).glob(
                "data*.parquet"
            )
        )
        if not files:
            continue
        frame = pl.concat([pl.read_parquet(path, columns=["symbol"]) for path in files])
        out.append(
            {
                "date": date_iso,
                "n_rows": frame.height,
                "n_symbols": frame["symbol"].n_unique(),
            }
        )
    return out


def expected_trading_days(start: str | None, end: str | None) -> list[str]:
    """Trading days in [start, end] (the gap-detection window). Empty if no explicit window is given —
    gap detection requires a window so 'missing' is well-defined against the real calendar.
    """
    if start is None or end is None:
        return []
    client = trading_client()
    days = trading_days(
        client, dt.date.fromisoformat(start), dt.date.fromisoformat(end)
    )
    return [day.isoformat() for day in days]


def gaps(covered: list[str], expected: list[str]) -> list[str]:
    """Expected trading days with no partition. Empty when no expected window was supplied."""
    if not expected:
        return []
    covered_set = set(covered)
    return [day for day in expected if day not in covered_set]


def describe_group(
    root: str, group: str, version: str, start: str | None, end: str | None, deep: bool
) -> dict[str, object]:
    """Coverage + trust report for one group: stream/backfill dates, gaps, and per-feature trust state."""
    catalog = REGISTRY.catalog().filter(pl.col("group") == group)
    features = catalog["feature"].to_list()
    trust = trust_by_feature()
    stream = source_coverage(root, group, version, "stream")
    backfill = source_coverage(root, group, version, "backfill")
    backfill_dates = covered_dates(root, group, version, "backfill")
    stream_dates = covered_dates(root, group, version, "stream")
    expected = expected_trading_days(start, end)
    report: dict[str, object] = {
        "group": group,
        "version": version,
        "n_features": len(features),
        "features": features,
        "layer": catalog["layer"][0] if catalog.height else None,
        "owner": catalog["owner"][0] if catalog.height else None,
        "parity_method": catalog["parity_method"][0] if catalog.height else None,
        "stream": stream,
        "backfill": backfill,
        "backfill_gaps": gaps(backfill_dates, expected),
        "stream_gaps": gaps(stream_dates, expected),
        "trusted_features": {f: trust[f] for f in features if f in trust},
        "n_trusted": sum(1 for f in features if f in trust),
    }
    if deep:
        report["backfill_per_date"] = deep_source_coverage(
            root, group, version, "backfill"
        )
        report["stream_per_date"] = deep_source_coverage(root, group, version, "stream")
    return report


def list_features() -> list[dict[str, object]]:
    """All features x group x trust state — the catalog left-joined to the trusted cohort."""
    catalog = REGISTRY.catalog()
    trust = trust_by_feature()
    rows: list[dict[str, object]] = []
    for record in catalog.iter_rows(named=True):
        feature = record["feature"]
        graded = trust.get(feature, {})
        rows.append(
            {
                "feature": feature,
                "group": record["group"],
                "version": record["version"],
                "layer": record["layer"],
                "parity_method": record["parity_method"],
                "trusted": feature in trust,
                "trust_reason": graded.get("trust_reason"),
                "trust_value_rate": graded.get("trust_value_rate"),
            }
        )
    return rows


def trusted_coverage(
    root: str, start: str | None, end: str | None
) -> list[dict[str, object]]:
    """The trusted cohort joined to its backfill coverage — the 'trusted AND (not-)yet-backfilled' surface
    the selective-backfill loop gates on. One row per trusted feature: its group, backfill date span, and
    (if a window is given) the count of missing backfill days the loop still needs to materialize.
    """
    trust = trusted_features()
    expected = expected_trading_days(start, end)
    rows: list[dict[str, object]] = []
    for record in trust:
        feature = str(record["feature"])
        group, version = _resolve(feature)
        dates = covered_dates(root, group, version, "backfill")
        missing = gaps(dates, expected)
        rows.append(
            {
                "feature": feature,
                "group": group,
                "version": version,
                "trust_reason": record.get("trust_reason"),
                "trust_value_rate": record.get("trust_value_rate"),
                "backfill_dates": len(dates),
                "backfill_first": dates[0] if dates else None,
                "backfill_last": dates[-1] if dates else None,
                "missing_backfill_days": len(missing),
            }
        )
    return rows


def _print_group(report: dict[str, object]) -> None:
    stream = cast(dict[str, object], report["stream"])
    backfill = cast(dict[str, object], report["backfill"])
    backfill_gaps = cast(list[str], report["backfill_gaps"])
    trusted = cast(dict[str, object], report["trusted_features"])
    features = cast(list[str], report["features"])
    print(
        f"group={report['group']} v={report['version']} "
        f"({report['n_features']} features, {report['n_trusted']} trusted, "
        f"layer={report['layer']}, owner={report['owner']})"
    )
    print(
        f"  STREAM   : {stream['n_dates']} dates "
        f"[{stream['first_date']} .. {stream['last_date']}]"
    )
    print(
        f"  BACKFILL : {backfill['n_dates']} dates "
        f"[{backfill['first_date']} .. {backfill['last_date']}]"
    )
    if backfill_gaps:
        shown = backfill_gaps[:10]
        suffix = " ..." if len(backfill_gaps) > 10 else ""
        print(f"  BACKFILL GAPS ({len(backfill_gaps)}): {shown}{suffix}")
    for feature in features:
        mark = "TRUSTED" if feature in trusted else "-"
        print(f"  [{mark}] {feature}")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Feature-data coverage + trust discovery surface"
    )
    parser.add_argument("command", choices=["feature", "group", "list", "trusted"])
    parser.add_argument(
        "target",
        nargs="?",
        help="feature name (for 'feature') or group name (for 'group')",
    )
    parser.add_argument(
        "--root", default=DEFAULT_ROOT, help="feature store root (env STORE_ROOT)"
    )
    parser.add_argument("--start", default=None, help="ISO start for gap detection")
    parser.add_argument("--end", default=None, help="ISO end for gap detection")
    parser.add_argument(
        "--deep", action="store_true", help="per-date row/symbol counts (slower)"
    )
    parser.add_argument(
        "--json", action="store_true", help="machine-readable JSON output"
    )
    return parser.parse_args(argv)


def main() -> None:
    args = parse_args(sys.argv[1:])

    if args.command == "list":
        rows = list_features()
        if args.json:
            print(json.dumps(rows, default=str))
        else:
            for row in rows:
                mark = "TRUSTED" if row["trusted"] else "-"
                print(f"{row['feature']:<48} {row['group']:<28} {mark}")
        return

    if args.command == "trusted":
        rows = trusted_coverage(args.root, args.start, args.end)
        if args.json:
            print(json.dumps(rows, default=str))
        else:
            print(f"trusted cohort: {len(rows)} feature(s)")
            for row in rows:
                print(
                    f"  {row['feature']:<48} group={row['group']:<24} "
                    f"backfill_dates={row['backfill_dates']} missing={row['missing_backfill_days']}"
                )
        return

    if not args.target:
        raise SystemExit(f"'{args.command}' requires a target name")

    if args.command == "feature":
        group, version = _resolve(args.target)
    else:
        group_obj = REGISTRY.get_group(args.target)
        group, version = group_obj.name, group_obj.version

    report = describe_group(args.root, group, version, args.start, args.end, args.deep)
    if args.command == "feature":
        report["queried_feature"] = args.target
    if args.json:
        print(json.dumps(report, default=str))
    else:
        _print_group(report)


if __name__ == "__main__":
    main()
