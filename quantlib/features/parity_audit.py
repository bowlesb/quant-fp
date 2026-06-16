"""Real-data parity audit — the STANDING live==backfill verifier for EVERY registered group.

The platform's core invariant is that the LIVE path (``compute_latest`` / the incremental engine, the
aggregate-at-T form) emits, for the latest minute, EXACTLY what the BACKFILL path (``compute``, the
whole-history rolling form) computes for that minute. ``tests/test_fp_latest.py`` already guards this on
GOLDEN/synthetic frames; THIS module re-runs the same comparison on PRODUCTION-REALISTIC data loaded from
``/store/raw`` (real bars, real per-minute tick aggregates from raw trades/quotes, real gaps and price
dynamics), and covers ALL ~35 groups including the hand-written ``compute_latest`` overrides and the
cross-sectional/breadth groups the golden frames exercise weakly.

For each group it reports MATCH / DIVERGE / NEEDS-DATA per feature, with the worst divergence magnitude and
an exemplar (symbol, backfill value, realtime value). For ReductionGroups it ALSO compares the incremental
engine's ``step`` output against the batch backfill (the live production path, not just ``compute_latest``).

This is a re-runnable audit, not a pytest: ``python -m quantlib.features.parity_audit [DAY] [N_SYMBOLS]``.
It is the living source of truth behind ``docs/PARITY_COVERAGE.md``.
"""
from __future__ import annotations

import datetime as dt
import os
import sys
from dataclasses import dataclass

import polars as pl

from quantlib.data.raw_store import partition_dir
from quantlib.features.base import BatchContext, FeatureGroup
from quantlib.features.compare import runnable
from quantlib.features.declarative import ReductionGroup
from quantlib.features.incremental import IncrementalEngine
from quantlib.features.registry import REGISTRY

DEFAULT_RAW_ROOT = os.environ.get("FP_RAW_ROOT", "/store")
# A liquid, multi-sector set that has bars + trades + quotes in /store/raw — enough breadth for the
# cross-sectional/breadth ranks to have a real distribution, plus the index ETFs market_context regresses on.
DEFAULT_SYMBOLS: tuple[str, ...] = (
    "SPY", "QQQ", "IWM", "DIA", "AAPL", "MSFT", "NVDA", "TSLA", "AMZN", "META", "GOOGL", "AMD",
    "NFLX", "INTC",
)
ABS_FLOOR = 1e-9  # absolute parity floor near zero (matches tests/test_fp_latest.py)
SECTORS = ("Technology", "Communication Services", "Consumer Discretionary", "Financials", "Energy")


@dataclass
class FeatureVerdict:
    group: str
    feature: str
    status: str  # "MATCH" | "DIVERGE" | "NEEDS_DATA"
    n_bad: int
    worst_abs: float
    worst_rel: float
    exemplar: str  # "symbol: backfill=.. realtime=.." or a reason


def _read_partition(raw_root: str, tier: str, symbol: str, day: dt.date) -> pl.DataFrame | None:
    path = os.path.join(partition_dir(raw_root, tier, symbol, day), "data.parquet")
    if not os.path.exists(path):
        return None
    frame = pl.read_parquet(path)
    return frame if frame.height else None


def load_bars(raw_root: str, day: str, symbols: list[str]) -> pl.DataFrame:
    """The OHLCV ``minute_agg`` core from real ``/store/raw/bars`` partitions (symbol, minute, open, high,
    low, close, volume)."""
    target = dt.date.fromisoformat(day)
    frames = []
    for symbol in symbols:
        raw = _read_partition(raw_root, "bars", symbol, target)
        if raw is None:
            continue
        frames.append(
            raw.select(
                pl.col("symbol"),
                pl.col("ts").alias("minute"),
                pl.col("open").cast(pl.Float64),
                pl.col("high").cast(pl.Float64),
                pl.col("low").cast(pl.Float64),
                pl.col("close").cast(pl.Float64),
                pl.col("volume").cast(pl.Float64),
            )
        )
    if not frames:
        raise SystemExit(f"no raw bars for any of {symbols} on {day} under {raw_root}/raw/bars")
    return pl.concat(frames, how="vertical").sort(["symbol", "minute"])


def _tick_columns_for_symbol(trades: pl.DataFrame, quotes: pl.DataFrame | None) -> pl.DataFrame:
    """Per-(symbol, minute) tick aggregates matching ``loaders._MINUTE_AGG_SQL`` / ``tick_capture``.

    signed_volume uses the tick rule with the sign carried across zero-ticks (state threaded WITHIN a symbol
    over the whole day, exactly as ``aggregate_trades``); the per-minute group_by then sums the signed sizes.
    This reproduces the production tick columns the trade_flow/quote_spread/liquidity groups read — the point
    is to feed those groups REAL, varied tick inputs (not to re-certify the tick aggregator, which has its own
    tests), so the parity invariant is exercised on production-shaped values."""
    trades = trades.sort(["symbol", "ts"])
    # tick-rule sign: +1 uptick, -1 downtick, carry previous sign on a zero-tick (forward-fill within symbol)
    signed = trades.with_columns(
        pl.when(pl.col("price") > pl.col("price").shift(1).over("symbol"))
        .then(1)
        .when(pl.col("price") < pl.col("price").shift(1).over("symbol"))
        .then(-1)
        .otherwise(None)
        .alias("_raw_sign")
    ).with_columns(
        pl.col("_raw_sign").fill_null(strategy="forward").over("symbol").fill_null(1).alias("_sign")
    )
    trade_agg = signed.group_by(
        ["symbol", pl.col("ts").dt.truncate("1m").alias("minute")]
    ).agg(
        pl.len().cast(pl.Float64).alias("n_trades"),
        (pl.col("_sign") * pl.col("size")).sum().alias("signed_volume"),
    )
    if quotes is None or quotes.height == 0:
        return trade_agg
    mid = (pl.col("bid_price") + pl.col("ask_price")) / 2.0
    depth = pl.col("bid_size") + pl.col("ask_size")
    quote_agg = quotes.with_columns(
        pl.when((mid > 0) & (pl.col("ask_price") >= pl.col("bid_price")))
        .then((pl.col("ask_price") - pl.col("bid_price")) / mid * 10000.0)
        .otherwise(None)
        .alias("_spread_bps"),
        pl.when(depth > 0)
        .then((pl.col("bid_size") - pl.col("ask_size")) / depth)
        .otherwise(None)
        .alias("_imbalance"),
    ).group_by(
        ["symbol", pl.col("ts").dt.truncate("1m").alias("minute")]
    ).agg(
        pl.col("_spread_bps").mean().fill_null(0.0).alias("mean_spread_bps"),
        pl.col("_imbalance").mean().fill_null(0.0).alias("quote_imbalance"),
        pl.col("bid_size").mean().alias("mean_bid_size"),
        pl.col("ask_size").mean().alias("mean_ask_size"),
    )
    return trade_agg.join(quote_agg, on=["symbol", "minute"], how="full", coalesce=True)


def load_tick_enriched_minute_agg(raw_root: str, day: str, symbols: list[str], bars: pl.DataFrame) -> pl.DataFrame:
    """Enrich the bar ``minute_agg`` with the real per-minute tick columns (n_trades, signed_volume, spread,
    imbalance, sizes) aggregated from ``/store/raw/trades`` + ``/store/raw/quotes`` — so trade_flow /
    quote_spread / liquidity run on REAL tick inputs, null where a symbol had no ticks that minute (honest)."""
    target = dt.date.fromisoformat(day)
    trade_frames, quote_frames = [], []
    for symbol in symbols:
        trades = _read_partition(raw_root, "trades", symbol, target)
        if trades is not None:
            trade_frames.append(trades.select("symbol", "ts", "price", "size"))
        quotes = _read_partition(raw_root, "quotes", symbol, target)
        if quotes is not None:
            quote_frames.append(quotes.select("symbol", "ts", "bid_price", "ask_price", "bid_size", "ask_size"))
    if not trade_frames:
        return bars
    trades_all = pl.concat(trade_frames, how="vertical")
    quotes_all = pl.concat(quote_frames, how="vertical") if quote_frames else None
    ticks = _tick_columns_for_symbol(trades_all, quotes_all)
    return bars.join(ticks, on=["symbol", "minute"], how="left")


def load_raw_trades_frame(raw_root: str, day: str, symbols: list[str]) -> pl.DataFrame:
    """The raw per-trade ``trades`` frame (symbol, ts, price, size) the tick_runlength / microstructure_burst
    groups declare as input — read straight from ``/store/raw/trades``, RTH-restricted to keep it bounded."""
    target = dt.date.fromisoformat(day)
    frames = []
    for symbol in symbols:
        trades = _read_partition(raw_root, "trades", symbol, target)
        if trades is not None:
            frames.append(trades.select("symbol", "ts", "price", "size"))
    if not frames:
        return pl.DataFrame(schema={"symbol": pl.String, "ts": pl.Datetime("us", "UTC"),
                                    "price": pl.Float64, "size": pl.Float64})
    return pl.concat(frames, how="vertical").sort(["symbol", "ts"])


def build_daily(raw_root: str, day: str, symbols: list[str], lookback_days: int = 200) -> pl.DataFrame:
    """A real ``daily`` history (symbol, date, open, high, low, close, vwap, volume) for the multi_day /
    prior_day groups: resample each symbol's prior-days raw minute bars to daily OHLCV. Uses whatever raw
    bar days exist under ``/store/raw/bars`` up to and INCLUDING ``day`` (the prior_day/multi_day groups
    anchor on the session date)."""
    target = dt.date.fromisoformat(day)
    rows = []
    for symbol in symbols:
        sym_dir = os.path.join(raw_root, "raw", "bars", f"symbol={symbol}")
        if not os.path.isdir(sym_dir):
            continue
        for entry in sorted(os.listdir(sym_dir)):
            if not entry.startswith("date="):
                continue
            date = dt.date.fromisoformat(entry[len("date="):])
            if date > target or (target - date).days > lookback_days:
                continue
            path = os.path.join(sym_dir, entry, "data.parquet")
            if not os.path.exists(path):
                continue
            bars = pl.read_parquet(path)
            if bars.height == 0:
                continue
            agg = bars.select(
                pl.lit(symbol).alias("symbol"),
                pl.lit(date).alias("date"),
                pl.col("open").first().alias("open"),
                pl.col("high").max().alias("high"),
                pl.col("low").min().alias("low"),
                pl.col("close").last().alias("close"),
                ((pl.col("close") * pl.col("volume")).sum() / pl.col("volume").sum()).alias("vwap"),
                pl.col("volume").sum().alias("volume"),
            )
            rows.append(agg)
    if not rows:
        return pl.DataFrame(schema={"symbol": pl.String, "date": pl.Date, "open": pl.Float64,
                                    "high": pl.Float64, "low": pl.Float64, "close": pl.Float64,
                                    "vwap": pl.Float64, "volume": pl.Float64})
    return pl.concat(rows, how="vertical").sort(["symbol", "date"])


def build_reference(symbols: list[str]) -> pl.DataFrame:
    """A typed ``reference`` snapshot (sector + tradability flags) — VARIED sectors so the sector one-hot
    group emits more than one value (the only thing parity needs from it; sector/flag features are static
    and parity-trivial, but they must still run)."""
    return pl.DataFrame(
        {
            "symbol": symbols,
            "sector": [SECTORS[i % len(SECTORS)] for i in range(len(symbols))],
            "shortable": [True] * len(symbols),
            "easy_to_borrow": [i % 2 == 0 for i in range(len(symbols))],
            "marginable": [True] * len(symbols),
            "fractionable": [i % 3 == 0 for i in range(len(symbols))],
        }
    )


def build_frames(raw_root: str, day: str, symbols: list[str]) -> dict[str, pl.DataFrame]:
    """The full production-shaped BatchContext frames from real ``/store/raw`` data."""
    bars = load_bars(raw_root, day, symbols)
    present = bars["symbol"].unique().to_list()
    minute_agg = load_tick_enriched_minute_agg(raw_root, day, present, bars)
    frames = {
        "minute_agg": minute_agg,
        "daily": build_daily(raw_root, day, present),
        "reference": build_reference(present),
        "trades": load_raw_trades_frame(raw_root, day, present),
    }
    frames["universe"] = pl.DataFrame({"symbol": present})
    return frames


def _compare_latest_row(
    group: FeatureGroup, backfill: pl.DataFrame, live: pl.DataFrame
) -> list[FeatureVerdict]:
    """Cell-for-cell compare the latest-minute row of ``live`` against ``backfill`` within each feature's
    declared tolerance (the SAME predicate as tests/test_fp_latest.py). A null-vs-value mismatch is a hard
    DIVERGE (the most important parity break — a feature null live but real in backfill)."""
    latest = backfill["minute"].max()
    expected = backfill.filter(pl.col("minute") == latest).sort("symbol")
    actual = live.filter(pl.col("minute") == latest).sort("symbol").select(expected.columns)
    tolerances = {spec.name: spec.tolerance for spec in group.declare()}
    verdicts = []
    for feature in [c for c in expected.columns if c not in ("symbol", "minute")]:
        tol = tolerances[feature]
        joined = expected.select("symbol", feature).join(
            actual.select("symbol", pl.col(feature).alias("_a")), on="symbol", how="full", coalesce=True
        )
        null_mismatch = pl.col(feature).is_null() != pl.col("_a").is_null()
        both_present = pl.col(feature).is_not_null() & pl.col("_a").is_not_null()
        abs_diff = (pl.col(feature) - pl.col("_a")).abs()
        beyond = both_present & (abs_diff > ABS_FLOOR + tol * pl.col(feature).abs())
        bad = joined.filter(null_mismatch | beyond)
        if bad.height == 0:
            n_back = joined.select(pl.col(feature).is_not_null().sum()).item()
            status = "MATCH" if n_back else "NEEDS_DATA"
            verdicts.append(FeatureVerdict(group.name, feature, status, 0, 0.0, 0.0,
                                           "all-null (no backfill values)" if not n_back else ""))
            continue
        worst = bad.with_columns(
            abs_diff.alias("_absd"),
            (abs_diff / (pl.col(feature).abs() + ABS_FLOOR)).alias("_reld"),
        ).sort("_reld", descending=True, nulls_last=True).row(0, named=True)
        exemplar = (f"{worst['symbol']}: backfill={worst[feature]} realtime={worst['_a']}")
        verdicts.append(FeatureVerdict(
            group.name, feature, "DIVERGE", bad.height,
            float(worst["_absd"] or 0.0), float(worst["_reld"] or 0.0), exemplar,
        ))
    return verdicts


def audit_group_compute_latest(group: FeatureGroup, ctx: BatchContext) -> list[FeatureVerdict]:
    """compute_latest() vs compute().filter(last) for one group on the real frames."""
    backfill = group.compute(ctx)
    if backfill.height == 0:
        return [FeatureVerdict(group.name, spec.name, "NEEDS_DATA", 0, 0.0, 0.0, "compute() empty")
                for spec in group.declare()]
    live = group.compute_latest(ctx)
    return _compare_latest_row(group, backfill, live)


# The incremental engine MUST be driven the way production drives it: seed at session start, then ``step``
# each NEW minute one at a time (the running per-symbol OBV/time state + the slice-derive tail are advanced
# per minute). A single ``step`` on a cold full buffer is NOT a production scenario and would mis-seed the
# stateful regressors. We step the trailing ``WARMUP_MINUTES`` minutes one at a time — enough to warm the
# deepest declared window — which makes the latest minute's running sums identical to the batch.
WARMUP_MINUTES = 260  # > the deepest reduction window (180m) + slack


def audit_incremental(groups: list[ReductionGroup], ctx: BatchContext) -> list[FeatureVerdict]:
    """The LIVE PRODUCTION path for reduction groups: the IncrementalEngine driven minute-by-minute (exactly
    as the live capture does — seed, then ``step`` each new minute) vs the batch backfill. Steps the trailing
    ``WARMUP_MINUTES`` minutes one at a time, then compares the final minute's output cell-for-cell. This is
    stronger than ``compute_latest`` — it exercises the running-sum state, slice-derive tail, and stateful
    regressors (OBV cumulative, time axis), the actual per-minute production code."""
    frame = ctx.frame(groups[0].reduce_input)
    minutes = sorted(frame["minute"].unique())
    tail = minutes[-WARMUP_MINUTES:]
    engine = IncrementalEngine(groups, rust_slice=True)
    out: dict[str, pl.DataFrame] = {}
    for minute in tail:
        out = engine.step(frame.filter(pl.col("minute") <= minute))
    verdicts = []
    for group in groups:
        backfill = group.compute(ctx)
        if backfill.height == 0:
            continue
        verdicts.extend(_compare_latest_row(group, backfill, out[group.name]))
    return verdicts


def run_audit(raw_root: str, day: str, symbols: list[str]) -> tuple[list[FeatureVerdict], list[FeatureVerdict]]:
    """Run the full audit. Returns (compute_latest verdicts, incremental verdicts)."""
    frames = build_frames(raw_root, day, symbols)
    ctx = BatchContext(frames=frames)
    available = {g.name for g in runnable(frames)}
    cl_verdicts: list[FeatureVerdict] = []
    for group in sorted(REGISTRY.groups(), key=lambda g: g.name):
        if group.name not in available:
            cl_verdicts.extend(
                FeatureVerdict(group.name, spec.name, "NEEDS_DATA", 0, 0.0, 0.0, "inputs not present")
                for spec in group.declare()
            )
            continue
        cl_verdicts.extend(audit_group_compute_latest(group, ctx))
    reduction_groups = [
        g for g in REGISTRY.groups() if isinstance(g, ReductionGroup) and g.name in available
    ]
    inc_verdicts = audit_incremental(reduction_groups, ctx) if reduction_groups else []
    return cl_verdicts, inc_verdicts


def _print_report(title: str, verdicts: list[FeatureVerdict]) -> None:
    by_status: dict[str, int] = {}
    for verdict in verdicts:
        by_status[verdict.status] = by_status.get(verdict.status, 0) + 1
    print(f"\n=== {title} ===")
    print(f"features: {len(verdicts)}  " + "  ".join(f"{k}={v}" for k, v in sorted(by_status.items())))
    diverged = [verdict for verdict in verdicts if verdict.status == "DIVERGE"]
    if diverged:
        print(f"\n  DIVERGENCES ({len(diverged)}):")
        for verdict in sorted(diverged, key=lambda v: v.worst_rel, reverse=True):
            print(f"  {verdict.group}.{verdict.feature}: n_bad={verdict.n_bad} "
                  f"worst_abs={verdict.worst_abs:.3e} worst_rel={verdict.worst_rel:.3e} | {verdict.exemplar}")
    needs = sorted({verdict.group for verdict in verdicts if verdict.status == "NEEDS_DATA"})
    if needs:
        print(f"\n  NEEDS_DATA groups: {needs}")


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    day = args[0] if args else "2026-06-15"
    n = int(args[1]) if len(args) > 1 else len(DEFAULT_SYMBOLS)
    symbols = list(DEFAULT_SYMBOLS)[:n] if n <= len(DEFAULT_SYMBOLS) else list(DEFAULT_SYMBOLS)
    print(f"parity audit: day={day} symbols={symbols} raw_root={DEFAULT_RAW_ROOT}")
    cl_verdicts, inc_verdicts = run_audit(DEFAULT_RAW_ROOT, day, symbols)
    _print_report("compute_latest vs compute().last (REAL data)", cl_verdicts)
    _print_report("IncrementalEngine.step vs compute().last (REAL data)", inc_verdicts)
    total_diverge = sum(1 for v in cl_verdicts + inc_verdicts if v.status == "DIVERGE")
    print(f"\nTOTAL DIVERGENCES: {total_diverge}")
    sys.exit(1 if total_diverge else 0)


if __name__ == "__main__":
    main()
