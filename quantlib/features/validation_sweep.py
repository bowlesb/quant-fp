"""The NIGHTLY parity-validation sweep — prove live (source=stream) == backfill (source=backfill) for
every symbol collected on the last market day, contamination-aware, chunked, idempotent.

This is the scheduled job behind the trust LIFECYCLE (docs/PARITY_LIFECYCLE.md). For a settled day:

  1. DISCOVER the symbols collected live that day (distinct ``source=stream`` symbols in the store).
  2. MATERIALIZE the backfill side for those symbols from ``/store/raw`` (``materialize_from_raw`` —
     download-once tape, no Alpaca re-fetch), in CHUNKS (default 200 symbols) so a ~11k-symbol day never
     loads at once (the OOM the symbol-scope avoids). Symbols with no ``/store/raw`` bars are reported and
     skipped (can't validate what backfill can't produce).
  3. VALIDATE each chunk (``validate(symbols=chunk)``) — writes the per-cell verdicts, the exceptions, the
     feature_day rollup, the legacy trust, and the canonical DB record, scoped + memory-safe.
  4. GRADE contamination-aware: per (symbol, day) decide CLEAN vs contaminated (``cleanliness``); grade
     each feature over CLEAN comparisons ONLY; derive the lifecycle state (PENDING/VALIDATED/DIVERGENT/
     RETIRED); upsert the parity-defect backlog for DIVERGENT features (``trust_lifecycle``).
  5. SUMMARIZE: counts per lifecycle state + new defects + contamination stats.

Idempotent/resumable: every write is an upsert keyed on (feature[,symbol],day); re-running a day refreshes
in place and never double-counts. The clean-day history is re-read from the persisted cleanliness +
feature_validation_day each run, so the lifecycle is a pure recompute (self-healing).

Usage:
  python -m quantlib.features.validation_sweep [YYYY-MM-DD] <feature_root> <val_root> [raw_root]
                                               [--chunk N] [--allow-today] [--max-symbols N]
  (day defaults to the LAST MARKET DAY via the Alpaca calendar.)
"""

from __future__ import annotations

import datetime as dt
import sys

import polars as pl

from quantlib.features import store, trust_lifecycle, validate as validate_mod, validation_store
from quantlib.features.cleanliness import clean_symbols, symbol_day_cleanliness
from quantlib.features.groups.market_context import INDICES as MARKET_INDICES
from quantlib.features.materialize import DEFAULT_RAW_ROOT, materialize_from_raw
from quantlib.features.registry import REGISTRY
from quantlib.features.session import rth_mask
from quantlib.features.trust_lifecycle import (
    clean_feature_day,
    defect_rows,
    lifecycle_state,
    retired_features,
)
from quantlib.data.raw_backfill import trading_client, trading_days

# Bar features that are non-null at every minute a bar printed (present in BOTH stream and backfill) —
# their per-minute presence is the minute-coverage signal the cleanliness heuristic reads.
COVERAGE_FEATURES = ["ret_1m"]
DEFAULT_CHUNK = 200
# The market-context tickers (SPY/QQQ) the cross-sectional features regress against. They are screened out
# of the raw-backfill UNIVERSE (is_etf_like), so a materialize chunk that lacks them produces a NULL market
# return for the whole chunk and every market-relative feature (market_beta/market_corr/idio_vol/
# market_return/nasdaq_return/relative_return/outperforming) reads as all-extra_live -> never compared ->
# can NEVER validate. We therefore PIN them into every materialize+validate chunk so the regression resolves
# its market reference. They must be acquired into /store/raw regardless of the ETF screen (ops/daily_lifecycle.sh).
MARKET_TICKERS: tuple[str, ...] = tuple(sorted(set(MARKET_INDICES.values())))
# A day must have at least this many CLEAN symbols to contribute a clean-day grade. Grading off one or two
# marginal survivors of a contaminated day is noise (a single thin name's near-zero-denominator rel-errors
# masquerade as failures); below the floor the day yields NO clean comparison and every feature stays
# PENDING for it — exactly "not enough clean comparisons" in the lifecycle. A normal day has thousands of
# clean liquid names, so this only ever suppresses pathologically contaminated days.
MIN_CLEAN_SYMBOLS = 20


def last_market_day(today: dt.date | None = None) -> str:
    """The most recent SETTLED (closed) NYSE trading day strictly before ``today`` (default: real today),
    via the Alpaca calendar. The sweep targets this day: its session is over and its raw backfill is
    available, so validating it is a fair settled comparison."""
    anchor = today or dt.datetime.now(dt.timezone.utc).astimezone().date()
    client = trading_client()
    days = trading_days(client, anchor - dt.timedelta(days=10), anchor - dt.timedelta(days=1))
    if not days:
        raise ValueError(f"no NYSE trading day found in the 10 days before {anchor}")
    return days[-1].isoformat()


def _chunks(items: list[str], size: int) -> list[list[str]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def _day_bounds(day: str) -> tuple[dt.datetime, dt.datetime]:
    year, month, dom = int(day[:4]), int(day[5:7]), int(day[8:10])
    start = dt.datetime(year, month, dom, tzinfo=dt.timezone.utc)
    end = dt.datetime(year, month, dom, 23, 59, 59, tzinfo=dt.timezone.utc)
    return start, end


def day_cleanliness(feature_root: str, day: str, symbols: list[str]) -> pl.DataFrame:
    """Per-(symbol) CLEAN/contaminated verdict for the day, from the bar-feature minute coverage of the
    scoped symbols. Reads only the COVERAGE_FEATURES columns (stream + backfill), RTH-masked, then defers
    to ``symbol_day_cleanliness``. Pushed-down symbol filter keeps this memory-safe."""
    start, end = _day_bounds(day)
    stream = store.get_features(COVERAGE_FEATURES, symbols, start, end, feature_root, source="stream")
    backfill = store.get_features(COVERAGE_FEATURES, symbols, start, end, feature_root, source="backfill")
    if backfill.height == 0:
        return pl.DataFrame()
    if stream.height == 0:
        stream = backfill.clear()
    joined = stream.join(backfill, on=["symbol", "minute"], how="full", suffix="_bk", coalesce=True).filter(
        rth_mask(pl.col("minute"))
    )
    return symbol_day_cleanliness(joined)


def _registry_maps() -> tuple[dict[str, str], dict[str, str]]:
    group_of = {spec.name: group.name for group, spec in REGISTRY.feature_specs()}
    version_of = {spec.name: group.version for group, spec in REGISTRY.feature_specs()}
    return group_of, version_of


def _build_clean_history(cell: pl.DataFrame, cleanliness: pl.DataFrame, day: str) -> pl.DataFrame:
    """The CLEAN per-(feature, day) comparison rows for one day from its cell rollup + cleanliness."""
    return clean_feature_day(cell, clean_symbols(cleanliness), day)


def sweep_day(
    feature_root: str,
    val_root: str,
    day: str,
    raw_root: str = DEFAULT_RAW_ROOT,
    chunk: int = DEFAULT_CHUNK,
    allow_today: bool = False,
    max_symbols: int | None = None,
) -> dict[str, object]:
    """Run the full nightly sweep for one day and return a summary dict.

    Returns counts the operator/cron logs: symbols discovered/materialized/skipped, per lifecycle-state
    feature counts, the new/total defects, and contamination stats.
    """
    validate_mod.assert_settled(day, allow_today)
    discovered = store.stream_symbols_on(feature_root, day)
    if max_symbols is not None:
        discovered = discovered[:max_symbols]
    if not discovered:
        return {"day": day, "discovered": 0, "note": "no source=stream symbols collected — nothing to sweep"}

    materialized: list[str] = []
    no_raw: list[str] = []
    for batch in _chunks(discovered, chunk):
        # PIN the market tickers into the materialize+validate scope so the cross-sectional features have
        # their backfill market reference (see MARKET_TICKERS). They are deduped against the batch and only
        # the DISCOVERED symbols are accounted in materialized/no_raw — the market tickers are reference
        # symbols, not part of the day's collected universe being certified.
        scope = batch + [ticker for ticker in MARKET_TICKERS if ticker not in batch]
        materialize_from_raw(feature_root, raw_root, day, scope)
        present = store.stream_symbols_on(feature_root, day, source="backfill")
        present_set = set(present)
        materialized.extend([symbol for symbol in batch if symbol in present_set])
        no_raw.extend([symbol for symbol in batch if symbol not in present_set])
        validate_mod.validate(feature_root, day, val_root, allow_today=allow_today, symbols=scope)

    cell = validation_store.read_cell(val_root, day)
    exceptions = validation_store.read_exceptions(val_root, day)
    cleanliness = day_cleanliness(feature_root, day, discovered)
    clean_count = int(cleanliness["is_clean"].sum()) if cleanliness.height else 0
    contaminated = (cleanliness.height - clean_count) if cleanliness.height else 0

    # Insufficient clean breadth -> the day is too contaminated to be a fair parity test. Record the
    # per-symbol cleanliness (the audit trail) but contribute NO clean-day grade, so no feature is condemned
    # off a handful of marginal survivors. Features simply stay PENDING for this day.
    group_of, version_of = _registry_maps()
    if clean_count < MIN_CLEAN_SYMBOLS:
        trust_lifecycle.write_lifecycle(pl.DataFrame(), [], cleanliness, version_of, day)
        return {
            "day": day,
            "discovered": len(discovered),
            "materialized": len(materialized),
            "no_raw_skipped": len(no_raw),
            "no_raw_examples": no_raw[:10],
            "clean_symbols": clean_count,
            "contaminated_symbols": contaminated,
            "features_graded": 0,
            "note": f"clean breadth {clean_count} < MIN_CLEAN_SYMBOLS {MIN_CLEAN_SYMBOLS} — day too "
            "contaminated to grade; features stay PENDING (no defects filed)",
        }

    clean_history_today = _build_clean_history(cell, cleanliness, day)
    history = validation_store.read_feature_day(val_root)  # for cross-day context (legacy trust source)
    states = lifecycle_state(clean_history_today, retired_features())
    defects = defect_rows(states, clean_history_today, exceptions, group_of, version_of)
    trust_lifecycle.write_lifecycle(states, defects, cleanliness, version_of, day)

    state_counts = states.group_by("lifecycle_state").len().sort("lifecycle_state").to_dicts() if states.height else []
    return {
        "day": day,
        "discovered": len(discovered),
        "materialized": len(materialized),
        "no_raw_skipped": len(no_raw),
        "no_raw_examples": no_raw[:10],
        "clean_symbols": clean_count,
        "contaminated_symbols": contaminated,
        "features_graded": states.height,
        "state_counts": {row["lifecycle_state"]: row["len"] for row in state_counts},
        "new_or_updated_defects": len(defects),
        "defect_features": [row[0] for row in defects][:20],
        "history_days_available": int(history["day"].n_unique()) if history.height else 0,
    }


def _parse_args(args: list[str]) -> dict[str, object]:
    allow_today = "--allow-today" in args
    rest = [arg for arg in args if arg != "--allow-today"]
    chunk = DEFAULT_CHUNK
    max_symbols: int | None = None
    positional: list[str] = []
    iterator = iter(rest)
    for arg in iterator:
        if arg == "--chunk":
            chunk = int(next(iterator))
        elif arg == "--max-symbols":
            max_symbols = int(next(iterator))
        else:
            positional.append(arg)
    # positional: [day] feature_root val_root [raw_root]; day optional (defaults to last market day)
    day: str | None = None
    if positional and len(positional[0]) == 10 and positional[0][4] == "-":
        day = positional.pop(0)
    if len(positional) < 2:
        raise SystemExit(
            "usage: python -m quantlib.features.validation_sweep [YYYY-MM-DD] <feature_root> <val_root> "
            "[raw_root] [--chunk N] [--allow-today] [--max-symbols N]"
        )
    feature_root = positional[0]
    val_root = positional[1]
    raw_root = positional[2] if len(positional) > 2 else DEFAULT_RAW_ROOT
    return {
        "day": day,
        "feature_root": feature_root,
        "val_root": val_root,
        "raw_root": raw_root,
        "chunk": chunk,
        "allow_today": allow_today,
        "max_symbols": max_symbols,
    }


def main() -> None:
    parsed = _parse_args(sys.argv[1:])
    day = parsed["day"] or last_market_day()
    summary = sweep_day(
        feature_root=parsed["feature_root"],  # type: ignore[arg-type]
        val_root=parsed["val_root"],  # type: ignore[arg-type]
        day=day,  # type: ignore[arg-type]
        raw_root=parsed["raw_root"],  # type: ignore[arg-type]
        chunk=parsed["chunk"],  # type: ignore[arg-type]
        allow_today=parsed["allow_today"],  # type: ignore[arg-type]
        max_symbols=parsed["max_symbols"],  # type: ignore[arg-type]
    )
    print(f"=== Parity-validation sweep summary for {day} ===")
    for key, value in summary.items():
        print(f"  {key}: {value}")


if __name__ == "__main__":
    main()
