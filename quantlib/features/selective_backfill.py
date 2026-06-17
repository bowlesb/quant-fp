"""Selective feature/group backfill driver — materialize JUST the requested feature(s) over a date range.

The findings->features loop needs: when a NEW feature is added, give it historical coverage WITHOUT
re-materializing all ~610 features. The building block exists — ``materialize.materialize_from_raw_full``
computes every runnable group from ``/store/raw`` for one day, and ``store.write_group`` writes one atomic
``(group, version, source=backfill, date)`` partition. This driver is the missing scope+fan-out layer:

  * Resolve feature NAMES -> their GROUPS (features in a group share compute, so group is the work unit).
  * Fan out (group, date) units across the date range in a memory-capped PROCESS pool (one day per task),
    each day calling the full-tick materialize and writing only the requested groups' partitions.
  * SKIP-EXISTING resume: a (group, version, source=backfill, date) partition already on disk is not
    recomputed (idempotent, atomic per partition) unless ``--force``.

Day x group is an independent unit (embarrassingly parallel, no contention, atomic per-partition write) —
the same machinery the full vector backfill will use, scoped to a feature subset.

Usage:
    python -m quantlib.features.selective_backfill \
        --features microstructure_burst,trade_flow_imbalance_1m --start 2025-01-02 --end 2025-06-30
    python -m quantlib.features.selective_backfill --groups trade_flow,tick_runlength --months 3
    python -m quantlib.features.selective_backfill --features <name> --symbols AAPL,NVDA --start ... --end ...
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import os
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed

from quantlib.data.raw_backfill import trading_client, trading_days, universe_symbols
from quantlib.features import store
from quantlib.features.materialize import materialize_from_raw_groups
from quantlib.features.registry import REGISTRY
from quantlib.features.store import _resolve
from quantlib.features.trusted_list import trusted_names

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
)
logger = logging.getLogger("selective_backfill")

DEFAULT_ROOT = os.environ.get("STORE_ROOT", "/store")
DEFAULT_RAW_ROOT = "/store"


def resolve_groups(features: list[str], groups: list[str]) -> dict[str, str]:
    """Resolve the requested feature NAMES + group NAMES to a {group_name: version} map (the work units).
    Feature names map to their owning group via the registry; group names map to their registered version.
    Raises KeyError on an unknown feature/group (never silently skip a requested target).
    """
    resolved: dict[str, str] = {}
    for feature in features:
        group_name, version = _resolve(feature)
        resolved[group_name] = version
    for group_name in groups:
        group = REGISTRY.get_group(group_name)
        resolved[group.name] = group.version
    return resolved


def trusted_target_groups() -> dict[str, str]:
    """The groups owning the currently-TRUSTED features — the trusted->lightGBM loop's work set. Reads the
    validation agent's trusted list (``trusted_list.trusted_names`` over the ``trusted_features`` view =
    feature_trust.lifecycle_state='VALIDATED') and resolves each trusted feature to its group/version. The
    set GROWS as the nightly sweep promotes PENDING->VALIDATED, so re-running incrementally backfills the
    newly-trusted features (skip-existing resume means already-backfilled groups are no-ops).
    """
    names = trusted_names()
    resolved: dict[str, str] = {}
    for feature in names:
        group_name, version = _resolve(feature)
        resolved[group_name] = version
    logger.info(
        "trusted cohort: %d features -> %d distinct groups", len(names), len(resolved)
    )
    return resolved


def pending_dates(
    root: str, group_versions: dict[str, str], days: list[dt.date], force: bool
) -> dict[str, list[str]]:
    """For each (group), the dates in ``days`` whose backfill partition is NOT already on disk (skip-existing
    resume). With ``force`` every date is pending. Returns {group: [date_iso, ...]}."""
    pending: dict[str, list[str]] = {}
    for group_name, version in group_versions.items():
        if force:
            done: set[str] = set()
        else:
            done = store.settled_dates(root, group_name, version)
        pending[group_name] = [
            day.isoformat() for day in days if day.isoformat() not in done
        ]
    return pending


def materialize_day(
    root: str, raw_root: str, day: str, symbols: list[str], groups: list[str]
) -> tuple[str, int]:
    """Worker: materialize ONLY the requested ``groups`` for one day from ``/store/raw`` (full tick
    enrichment so order-flow groups are runnable, but only the requested groups' partitions are written).
    Returns (day, n_symbols)."""
    count = materialize_from_raw_groups(root, raw_root, day, symbols, groups)
    return day, count


def run(
    root: str,
    raw_root: str,
    group_versions: dict[str, str],
    days: list[dt.date],
    symbols: list[str],
    processes: int,
    force: bool,
) -> None:
    pending = pending_dates(root, group_versions, days, force)
    all_pending_dates = sorted(
        {date_iso for dates in pending.values() for date_iso in dates}
    )
    groups = sorted(group_versions)
    logger.info(
        "selective backfill: groups=%s, %d symbols, %d/%d dates pending (force=%s)",
        groups,
        len(symbols),
        len(all_pending_dates),
        len(days),
        force,
    )
    if not all_pending_dates:
        logger.info(
            "nothing to do — all requested (group, date) partitions already on disk"
        )
        return

    written_days = 0
    with ProcessPoolExecutor(max_workers=max(1, processes)) as executor:
        futures = {
            executor.submit(materialize_day, root, raw_root, day, symbols, groups): day
            for day in all_pending_dates
        }
        for future in as_completed(futures):
            day = futures[future]
            _, count = future.result()
            written_days += 1
            logger.info(
                "materialized day=%s (%d symbols) [%d/%d]",
                day,
                count,
                written_days,
                len(all_pending_dates),
            )
    logger.info("DONE: materialized %d day(s) for groups=%s", written_days, groups)


def date_window(
    months: int | None, start: str | None, end: str | None
) -> list[dt.date]:
    """The trading-day window to backfill: an explicit [start, end] (ISO) OR the last ``months`` of trading
    days. Uses the Alpaca calendar so only real sessions are produced."""
    client = trading_client()
    today = dt.datetime.now(dt.timezone.utc).date()
    if start is not None and end is not None:
        return trading_days(
            client, dt.date.fromisoformat(start), dt.date.fromisoformat(end)
        )
    if months is not None:
        lookback = int(months * 31) + 7
        days = trading_days(client, today - dt.timedelta(days=lookback), today)
        return days[-int(months * 21) :]
    raise SystemExit("specify either --start and --end, or --months")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Selective feature/group backfill over a date range"
    )
    parser.add_argument(
        "--features", default="", help="comma list of FEATURE names to backfill"
    )
    parser.add_argument(
        "--groups", default="", help="comma list of GROUP names to backfill"
    )
    parser.add_argument(
        "--trusted",
        action="store_true",
        help="backfill the groups owning the currently-TRUSTED features (the trusted->lightGBM loop)",
    )
    parser.add_argument("--start", default=None, help="ISO start date (with --end)")
    parser.add_argument("--end", default=None, help="ISO end date (with --start)")
    parser.add_argument(
        "--months", type=int, default=None, help="last N months of trading days"
    )
    parser.add_argument(
        "--symbols", default=None, help="comma list; default = full tradable universe"
    )
    parser.add_argument(
        "--root", default=DEFAULT_ROOT, help="feature store root (env STORE_ROOT)"
    )
    parser.add_argument("--raw-root", default=DEFAULT_RAW_ROOT, help="raw /store root")
    parser.add_argument("--processes", type=int, default=8, help="parallel day workers")
    parser.add_argument(
        "--force",
        action="store_true",
        help="recompute even if the partition already exists",
    )
    return parser.parse_args(argv)


def main() -> None:
    args = parse_args(sys.argv[1:])
    features = [f.strip() for f in args.features.split(",") if f.strip()]
    group_names = [g.strip() for g in args.groups.split(",") if g.strip()]
    if not features and not group_names and not args.trusted:
        raise SystemExit("specify --features and/or --groups, or --trusted")
    group_versions = resolve_groups(features, group_names)
    if args.trusted:
        group_versions.update(trusted_target_groups())
    if not group_versions:
        logger.info("no target groups (trusted cohort empty?) — nothing to backfill")
        return

    days = date_window(args.months, args.start, args.end)
    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    else:
        symbols = universe_symbols(trading_client())

    run(
        args.root,
        args.raw_root,
        group_versions,
        days,
        symbols,
        args.processes,
        args.force,
    )


if __name__ == "__main__":
    main()
