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
import random
import sys
from typing import Callable

import polars as pl

from quantlib.features import store, trust_binary, trust_lifecycle, validate as validate_mod, validation_store
from quantlib.features.base import FeatureType
from quantlib.features.cleanliness import (
    clean_symbols,
    gather_coherence,
    symbol_day_cleanliness,
)
from quantlib.features.groups.market_context import INDICES as MARKET_INDICES
from quantlib.features.materialize import (
    DEFAULT_RAW_ROOT,
    materialize_from_raw,
    materialize_from_raw_bar_groups,
    materialize_from_raw_full,
)
from quantlib.features.raw_loaders import load_raw_minute_agg, load_raw_trades
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
DEFAULT_CHUNK = 500
# A market-wide breadth scalar broadcast IDENTICALLY to every symbol's row each minute — the gather-coherence
# probe. One distinct value per minute in a clean single-gather capture; >1 means the universe-wide gather
# fragmented (a restart / SIP-contention day's concurrent partial-universe gathers). Read from the live
# stream over the whole universe and fed to ``cleanliness.gather_coherence``.
GATHER_COHERENCE_FEATURE = "breadth_up_5m"
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

# UNIVERSE-REDUCE (cross-sectional) groups: a symbol's value is a reduction over the WHOLE universe present
# that minute (a breadth fraction, a percentile rank, a dispersion stat, a peer demean), so it can ONLY be
# reproduced when the SAME symbol set is present both sides. The gradable-set PASS-2 backfill is a ~92-symbol
# subset of the full-universe live stream, so these mis-grade as ~0.000 DIVERGENT there — a SCOPE artifact,
# not a real divergence. They are validated against the FULL-UNIVERSE PASS-1 (bar-only) backfill instead,
# where the present-set matches the stream by construction. They need only bars (close/volume), never ticks,
# so PASS 1 already produces everything they require.
#
# The structural signal is FeatureType.CROSS_SECTIONAL — but that family ALSO contains the REFERENCE-relative
# groups (market_context, market_beta), whose values regress each symbol against a FIXED reference (SPY/QQQ)
# and are INVARIANT to which OTHER universe symbols are present. Those already validate on the gradable set
# (the MARKET_TICKERS pin supplies their reference) and are deep-window — NOT universe-reduce — so they are
# excluded here. This exclusion list is the one piece that isn't purely structural (the enum cannot tell a
# universe reduce from a reference regression); kept explicit and documented per the parity-trust playbook.
REFERENCE_RELATIVE_GROUPS: frozenset[str] = frozenset({"market_context", "market_beta"})


# A settled market-ticker bar tape is a full RTH session (390 minutes) plus pre/post — hundreds of rows.
# A PER-TICKER floor rejects a stub partition (a pre-session placeholder that wrote a handful of rows then
# never re-fetched) WITHOUT tripping on a legitimately thin name; well below a real session, well above a stub.
MIN_MARKET_TICKER_BARS = 100
# A settled market-ticker tick tape is thousands of prints; a single-digit count is a placeholder stub, not a
# settled tape (observed: SPY landing 2 trades while QQQ had 757k on a half-acquired day).
MIN_MARKET_TICKER_TRADES = 100


def _per_ticker_counts(frame: pl.DataFrame, probe: list[str]) -> dict[str, int]:
    """Row count per pinned ticker in ``frame`` (0 for a ticker absent from the union). Each ``probe`` ticker
    must be present INDIVIDUALLY — a height>0 union can hide one empty ticker behind another's full tape."""
    if frame.height == 0 or "symbol" not in frame.columns:
        return {ticker: 0 for ticker in probe}
    counts = dict(frame.group_by("symbol").len().iter_rows())
    return {ticker: int(counts.get(ticker, 0)) for ticker in probe}


def assert_raw_present(day: str, raw_root: str, with_ticks: bool) -> None:
    """Refuse to sweep a day whose ``/store/raw`` side has not SETTLED yet (empty/stub bars/trades partitions).

    ``assert_settled`` only checks the calendar date (``day >= today``); it cannot see that Alpaca historical
    raw lands hours after the close (often ~T+1). On a closed-but-unsettled day the raw partitions exist as
    EMPTY/stub files, so ``load_raw_minute_agg`` returns an empty/thin frame ("the caller never needs to
    special-case missing raw days") — PASS 1 materializes nothing, every stream symbol reads as ``no_raw``,
    and the sweep silently writes a wall of false ``missing_backfill`` cells that mis-grade the whole day.

    The check probes EACH pinned MARKET_TICKER (SPY/QQQ — always acquired into raw on a settled day) via the
    SAME loaders the sweep uses, requiring each INDIVIDUALLY to carry a real-session tape (>= the floors).
    Checking the union height was insufficient: on a half-acquired day one ticker can land a full tape while
    the other is empty or a few-row stub (observed: SPY trades=2 while QQQ had 757k), so ``height > 0`` passed
    while a market reference was actually missing — exactly the silent mis-grade this guard exists to prevent.
    ``with_ticks`` sweeps additionally require the TRADES tier (the order-flow groups' backfill side); a
    bar-only sweep needs only bars.
    """
    probe = list(MARKET_TICKERS)
    bar_counts = _per_ticker_counts(load_raw_minute_agg(raw_root, day, probe), probe)
    thin_bars = {ticker: count for ticker, count in bar_counts.items() if count < MIN_MARKET_TICKER_BARS}
    if thin_bars:
        raise ValueError(
            f"refusing to sweep {day}: raw BARS are empty/stub for pinned market tickers {thin_bars} "
            f"(need >= {MIN_MARKET_TICKER_BARS} bars each) under {raw_root}/raw/bars — the day's raw has not "
            f"settled (Alpaca historical lands hours after close, often ~T+1; a stub partition is a "
            f"pre-session placeholder). Acquire it: `ops/raw_backfill.sh daily` (or DAY={day}), then re-sweep."
        )
    if with_ticks:
        trade_counts = _per_ticker_counts(load_raw_trades(raw_root, day, probe), probe)
        thin_trades = {ticker: count for ticker, count in trade_counts.items() if count < MIN_MARKET_TICKER_TRADES}
        if thin_trades:
            raise ValueError(
                f"refusing to sweep {day} with ticks: raw TRADES are empty/stub for {thin_trades} "
                f"(need >= {MIN_MARKET_TICKER_TRADES} trades each) under {raw_root}/raw/trades — the tick side "
                f"has not settled (bars settled but trades have not, or only partially). Acquire trades "
                f"(`ops/raw_backfill.sh daily`) and re-sweep, or run a bar-only sweep (--no-ticks) to grade "
                f"just the bar/cross-sectional groups now."
            )


# The pinned MARKET_TICKERS (SPY/QQQ) are the FIRST symbols Alpaca's symbol-by-symbol historical fetch
# settles — they pass ``assert_raw_present`` while the ILLIQUID TAIL is still landing hours later. That tail
# gap is the 2026-06-18 footgun: SPY/QQQ raw was full (gate passed) but ~450 thin names had streamed bars and
# NO backfill bars yet, so the sweep graded them stream>0/backfill=0 and filed ~450 false DIVERGENT defects.
# We additionally probe a RANDOM sample of the discovered stream universe and require nearly all of them to
# have landed real backfill bars; on a settled day Alpaca has fetched essentially every streamed symbol, so a
# sample present-rate below the floor means the universe is only PARTIALLY settled and must not be graded yet.
TAIL_SETTLE_SAMPLE = 200  # symbols sampled from the discovered universe to probe backfill landing (bounded reads)
# On a fully settled day Alpaca historical has fetched essentially every streamed symbol; the few percent that
# legitimately have no raw bars are delisted/halted names that never produce backfill on ANY day, so the floor
# sits below 100% to tolerate them while still catching the gross tail-gap of a half-acquired day (where a large
# fraction of the sample has no backfill yet). Empirically a settled day samples ~98%+; the 06-18 partial day
# would have sampled far below this as the illiquid tail had not landed.
MIN_TAIL_SETTLE_RATE = 0.90
# A symbol counts as backfill-settled only if its raw bars clear the same stub floor the market tickers use —
# a handful of pre-session placeholder rows is not a landed tape (mirrors MIN_MARKET_TICKER_BARS).
MIN_TAIL_SYMBOL_BARS = MIN_MARKET_TICKER_BARS


def _sample_universe(symbols: list[str], day: str, sample_size: int) -> list[str]:
    """A deterministic (day-seeded) random sample of ``symbols`` — same day always probes the same set, so the
    settle gate is idempotent across re-runs. The market tickers are excluded (they are probed separately and
    settle first, so including them would bias the tail-settle rate upward)."""
    candidates = [symbol for symbol in symbols if symbol not in set(MARKET_TICKERS)]
    if len(candidates) <= sample_size:
        return candidates
    rng = random.Random(day)
    return rng.sample(candidates, sample_size)


def assert_tail_settled(day: str, raw_root: str, discovered: list[str]) -> None:
    """Refuse to grade a day whose ILLIQUID TAIL has not finished landing in ``/store/raw`` yet.

    ``assert_raw_present`` only probes the pinned market tickers (SPY/QQQ), which settle FIRST in Alpaca's
    symbol-by-symbol historical fetch — they pass while the thin tail of the universe is still arriving hours
    later. On such a partially-settled day the streamed thin names have rows live but NO backfill side yet, so
    the sweep grades them stream>0/backfill=0 and manufactures a wall of false DIVERGENT defects (the 2026-06-18
    ~450-defect mis-grade). This guard samples the discovered stream universe and requires nearly all sampled
    symbols to have landed real backfill bars (>= the stub floor); a present-rate below ``MIN_TAIL_SETTLE_RATE``
    means the universe is only PARTIALLY settled and the day must not be graded until its raw backfill completes.
    """
    sample = _sample_universe(discovered, day, TAIL_SETTLE_SAMPLE)
    if not sample:
        return  # nothing beyond the market tickers to probe (tiny sandbox universe) — assert_raw_present suffices
    bar_counts = _per_ticker_counts(load_raw_minute_agg(raw_root, day, sample), sample)
    settled = [symbol for symbol, count in bar_counts.items() if count >= MIN_TAIL_SYMBOL_BARS]
    settle_rate = len(settled) / len(sample)
    if settle_rate < MIN_TAIL_SETTLE_RATE:
        missing = sorted(symbol for symbol, count in bar_counts.items() if count < MIN_TAIL_SYMBOL_BARS)
        raise ValueError(
            f"refusing to sweep {day}: only {len(settled)}/{len(sample)} sampled stream symbols "
            f"({settle_rate:.1%}) have landed raw BARS (need >= {MIN_TAIL_SETTLE_RATE:.0%}) under "
            f"{raw_root}/raw/bars — the ILLIQUID TAIL has not settled (Alpaca historical lands symbol-by-symbol, "
            f"the thin names hours after the liquid ones; grading now would file false DIVERGENT defects for "
            f"streamed names whose backfill side has not arrived). Acquire the full universe "
            f"(`ops/raw_backfill.sh daily` or DAY={day}) and re-sweep once settled. "
            f"Unsettled examples: {missing[:10]}"
        )


def cross_sectional_groups() -> list[str]:
    """The universe-reduce group names: every FeatureType.CROSS_SECTIONAL group MINUS the reference-relative
    ones (see REFERENCE_RELATIVE_GROUPS). Derived from the registry so a NEW universe-reduce group is picked
    up automatically by declaring ``type = FeatureType.CROSS_SECTIONAL`` (and, if it is reference-relative
    rather than a universe reduce, by adding its name to REFERENCE_RELATIVE_GROUPS)."""
    return [
        group.name
        for group in REGISTRY.groups()
        if group.type == FeatureType.CROSS_SECTIONAL and group.name not in REFERENCE_RELATIVE_GROUPS
    ]


# A from-raw materialize: (feature_root, raw_root, day, symbols, shard) -> symbols materialized. Both
# materialize_from_raw (bar-only, cheap) and materialize_from_raw_full (full tick tape) match this shape.
MaterializeFn = Callable[..., int]


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


def day_gather_coherence(feature_root: str, day: str, symbols: list[str]) -> dict[str, float | int | bool]:
    """Day-level GATHER-COHERENCE verdict from the live broadcast breadth scalar (see
    ``cleanliness.gather_coherence``). Reads ONLY the one ``GATHER_COHERENCE_FEATURE`` column over the
    discovered universe (the broadcast scalar is one number per minute, so this is small), RTH-masked by the
    coherence check itself. A fragmented day (>1 distinct broadcast value across many minutes) cannot fairly
    grade the universe-reduce features — the gate uses ``is_coherent``. Empty (no breadth captured) is
    vacuously coherent — the per-symbol cleanliness still gates grading."""
    start, end = _day_bounds(day)
    stream = store.get_features([GATHER_COHERENCE_FEATURE], symbols, start, end, feature_root, source="stream")
    return gather_coherence(stream, GATHER_COHERENCE_FEATURE)


def _registry_maps() -> tuple[dict[str, str], dict[str, str]]:
    group_of = {spec.name: group.name for group, spec in REGISTRY.feature_specs()}
    version_of = {spec.name: group.version for group, spec in REGISTRY.feature_specs()}
    return group_of, version_of


def _build_clean_history(cell: pl.DataFrame, cleanliness: pl.DataFrame, day: str) -> pl.DataFrame:
    """The CLEAN per-(feature, day) comparison rows for one day from its cell rollup + cleanliness."""
    return clean_feature_day(cell, clean_symbols(cleanliness), day)


def _materialize_chunks(
    materialize: MaterializeFn,
    feature_root: str,
    raw_root: str,
    day: str,
    symbols: list[str],
    chunk: int,
) -> tuple[list[str], list[str]]:
    """Materialize the backfill side for ``symbols`` in CHUNKS (sharded so disjoint chunks union on read),
    pinning the market reference tickers into every chunk. The caller clears the day's backfill side BEFORE
    calling (clean replace), then validates the union once (see ``sweep_day``).

    Returns ``(materialized, no_raw)`` — which requested symbols produced a backfill side vs had no
    ``/store/raw`` partition. Chunking bounds peak memory (a ~10k-symbol day never loads at once); the
    per-file shard (``data-<chunk>.parquet``) makes the chunks union on read instead of clobbering.
    """
    materialized: list[str] = []
    no_raw: list[str] = []
    for chunk_index, batch in enumerate(_chunks(symbols, chunk)):
        # PIN the market tickers into the materialize scope so the cross-sectional features have their
        # backfill market reference (see MARKET_TICKERS). They are deduped against the batch and only the
        # requested symbols are accounted in materialized/no_raw — the market tickers are reference symbols,
        # not part of the universe being certified.
        scope = batch + [ticker for ticker in MARKET_TICKERS if ticker not in batch]
        materialize(feature_root, raw_root, day, scope, shard=chunk_index)
        present_set = set(store.stream_symbols_on(feature_root, day, source="backfill"))
        materialized.extend([symbol for symbol in batch if symbol in present_set])
        no_raw.extend([symbol for symbol in batch if symbol not in present_set])
    return materialized, no_raw


def sweep_day(
    feature_root: str,
    val_root: str,
    day: str,
    raw_root: str = DEFAULT_RAW_ROOT,
    chunk: int = DEFAULT_CHUNK,
    allow_today: bool = False,
    max_symbols: int | None = None,
    with_ticks: bool = True,
) -> dict[str, object]:
    """Run the full nightly sweep for one day and return a summary dict.

    Returns counts the operator/cron logs: symbols discovered/materialized/skipped, per lifecycle-state
    feature counts, the new/total defects, and contamination stats.

    ``with_ticks`` (default True) materializes the backfill side from the FULL raw tape (bars + trades +
    quotes), so the order-flow groups (trade_flow / quote_spread / liquidity / signed_trade_ratio /
    tick_runlength / microstructure_burst) get a backfill side and their features are validated. Set False
    for a bar-only sweep (faster, but the tick/quote features stay PENDING — no backfill to compare).

    GRADABLE-SET TWO-PASS (the speedup). Grading only ever consults symbols that are BOTH clean-streamed
    AND have raw — the "gradable set". Materializing the full tick tape for all ~10k discovered symbols (of
    which ~6k are capture-contaminated and ~3.6k have no raw) wastes the bulk of the sweep on symbols whose
    backfill never enters a grade. So we split the work:
      PASS 1 (cheap, bar-only, ALL symbols): materialize only the bar features (``materialize_from_raw``)
        — enough to compute ``ret_1m`` minute coverage and therefore cleanliness. This decides the gradable
        set. A symbol with no raw bars is simply absent (no_raw).
      CROSS-SECTIONAL grade (full-universe, off pass 1): the universe-reduce groups (breadth_* / *_rank /
        dispersion / peer — see ``cross_sectional_groups``) value a symbol by a reduction over the WHOLE
        present universe, so they only reproduce the live stream when the SAME symbols are present both
        sides. They are graded against the FULL-UNIVERSE pass-1 bar-only backfill (where the present-set
        matches the stream); the gradable subset would mis-grade them ~0.000 (a SCOPE artifact). They need
        only bars, so pass 1 already produced everything they require.
      PASS 2 (full tape, GRADABLE SET ONLY): clear the day, re-materialize the clean symbols from the FULL
        tick tape (``materialize_from_raw_full`` when with_ticks) and grade the PER-SYMBOL + tick groups
        (everything except the cross-sectional groups). The order-flow groups get their backfill side for
        exactly the symbols that will be graded.
    The two group sets are disjoint; their results are merged and persisted ONCE. Parity is unchanged: the
    same clean symbols are graded against the same backfill scope that makes each comparison fair; only the
    never-graded contaminated/no-raw symbols are spared the expensive tick read. On a too-contaminated day
    (clean breadth < MIN_CLEAN_SYMBOLS) pass 2 is skipped entirely (and so is the cross-sectional grade).
    """
    validate_mod.assert_settled(day, allow_today)
    assert_raw_present(day, raw_root, with_ticks)
    full_materialize = materialize_from_raw_full if with_ticks else materialize_from_raw
    discovered = store.stream_symbols_on(feature_root, day)
    # The pinned-ticker probe (assert_raw_present) only certifies SPY/QQQ, which settle first; the illiquid
    # tail of the FULL discovered universe may still be landing. Probe it on the full set (before the sandbox
    # cap) so a partially-settled day can't file false stream>0/backfill=0 defects. A max_symbols evidence run
    # deliberately scopes down to a subset, so its tail-settle rate is not the universe's — skip the probe there.
    if max_symbols is None:
        assert_tail_settled(day, raw_root, discovered)
    if max_symbols is not None:
        discovered = discovered[:max_symbols]
    if not discovered:
        return {"day": day, "discovered": 0, "note": "no source=stream symbols collected — nothing to sweep"}

    # PASS 1 — cheap bar-only materialize over ALL discovered symbols to determine the gradable set. Clear
    # the day's backfill side first so each chunk's sharded file (data-<chunk>.parquet) unions cleanly on
    # read instead of colliding with a prior run's files.
    store.clear_backfill_day(feature_root, day)
    _, no_raw = _materialize_chunks(materialize_from_raw, feature_root, raw_root, day, discovered, chunk)
    cleanliness = day_cleanliness(feature_root, day, discovered)
    clean_count = int(cleanliness["is_clean"].sum()) if cleanliness.height else 0
    contaminated = (cleanliness.height - clean_count) if cleanliness.height else 0
    gradable = clean_symbols(cleanliness)

    # Insufficient clean breadth -> the day is too contaminated to be a fair parity test. Record the
    # per-symbol cleanliness (the audit trail) but contribute NO clean-day grade, so no feature is condemned
    # off a handful of marginal survivors. Features simply stay PENDING for this day. Both the cross-sectional
    # grade and the expensive full-tape PASS 2 are skipped — neither runs on a day that can't grade.
    group_of, version_of = _registry_maps()
    # Deterministic features (CALENDAR) are TRUSTED by construction — grant them on EVERY run, independent
    # of the day's cleanliness (they need no parity day). Idempotent: already-trusted features are skipped.
    trust_binary.write_trust_grants([], trust_binary.deterministic_features(), pl.DataFrame(), day)
    tolerance_of = trust_binary.cell_tolerance_map()
    if clean_count < MIN_CLEAN_SYMBOLS:
        trust_lifecycle.write_lifecycle(pl.DataFrame(), [], cleanliness, version_of, day)
        return {
            "day": day,
            "discovered": len(discovered),
            "materialized": 0,
            "no_raw_skipped": len(no_raw),
            "no_raw_examples": no_raw[:10],
            "clean_symbols": clean_count,
            "contaminated_symbols": contaminated,
            "features_graded": 0,
            "note": f"clean breadth {clean_count} < MIN_CLEAN_SYMBOLS {MIN_CLEAN_SYMBOLS} — day too "
            "contaminated to grade; features stay PENDING (no defects filed)",
        }

    # GATHER-COHERENCE gate — is the live universe-wide gather coherent (one breadth scalar per minute), or
    # did it FRAGMENT into concurrent partial-universe gathers (a restart / SIP-contention day)? On a
    # fragmented day the live cross-sectional values are partial-universe reductions the single full-universe
    # backfill can never match, so grading them manufactures false DIVERGENT verdicts — the exact 2026-06-15
    # failure the per-symbol coverage check cannot see (every symbol still has a row, just a partial value).
    # When fragmented we SKIP the cross-sectional grade entirely: those features stay PENDING for the day
    # (no clean comparison) rather than being condemned. The per-symbol / tick PASS 2 still runs — the
    # well-behaved per-symbol features (daily_return, sector flags) earn their clean-day grade regardless.
    coherence = day_gather_coherence(feature_root, day, discovered)
    xsec_groups = cross_sectional_groups()
    if coherence["is_coherent"]:
        # CROSS-SECTIONAL grade — graded against a FULL-UNIVERSE, SINGLE-COMPUTE backfill, BEFORE pass 2 clears
        # the day. Universe-reduce features (breadth_* / *_rank / dispersion / peer) value a symbol by a
        # reduction over the whole present universe, so the backfill compute MUST see EVERY symbol at once —
        # pass 1's CHUNKED materialize computes a separate partial-universe reduction per 500-symbol chunk (a
        # 500-name breadth, not a full-universe one), which the full-universe live gather can never match. So
        # re-materialize JUST these groups, bar-only and UN-CHUNKED, over the full discovered universe, clearing
        # their chunked pass-1 partitions first so the single-file write is a clean replace (not a union with
        # the chunk shards). They read only bars, so this skips the tick tape. The clean-symbol grade is taken
        # later from the clean cells.
        store.clear_backfill_groups_day(feature_root, day, xsec_groups)
        xsec_scope, xsec_tiers = validate_mod.scoped_tiers(day, discovered)
        materialize_from_raw_bar_groups(feature_root, raw_root, day, xsec_scope, only_groups=xsec_groups)
        xsec_result = validate_mod.compare_groups(
            feature_root, day, xsec_scope, xsec_tiers, groups=xsec_groups, tolerance_of=tolerance_of
        )
    else:
        xsec_result = validate_mod.empty_result()

    # PASS 2 — full-tape materialize over the GRADABLE SET ONLY, then validate the PER-SYMBOL + tick groups
    # (everything EXCEPT the cross-sectional groups already graded full-universe above). Re-clear the day so
    # the cheap bar-only pass-1 files are replaced by the full-tick backfill for the clean symbols (the
    # order-flow groups need the tick tape). The clean symbols all have raw bars by construction (cleanliness
    # requires backfill-present minutes), so this pass produces no further no_raw.
    store.clear_backfill_day(feature_root, day)
    materialized, _ = _materialize_chunks(full_materialize, feature_root, raw_root, day, gradable, chunk)
    # ONE compare over the full gradable scope: a per-chunk validate would only retain the last chunk in the
    # whole-day-replace cell store and cap the grade. Comparing the union once builds the complete cell rows;
    # the per-group read inside ``compare_groups`` (one group's features at a time, symbol-filter pushed
    # down) keeps it memory-bounded. The market tickers are pinned so the reference-relative features resolve
    # their SPY/QQQ reference. The cross-sectional groups are EXCLUDED here (graded full-universe in pass 1).
    grade_scope = gradable + [ticker for ticker in MARKET_TICKERS if ticker not in gradable]
    per_symbol_groups = [group.name for group in REGISTRY.groups() if group.name not in set(xsec_groups)]
    per_symbol_scope, per_symbol_tiers = validate_mod.scoped_tiers(day, grade_scope)
    per_symbol_result = validate_mod.compare_groups(
        feature_root, day, per_symbol_scope, per_symbol_tiers, groups=per_symbol_groups, tolerance_of=tolerance_of
    )

    # Merge the full-universe cross-sectional grade with the gradable-set per-symbol/tick grade and persist
    # ONCE — the two group sets are disjoint, so each feature is graded against the backfill scope that makes
    # its comparison fair, with no feature double-counted.
    validate_mod.persist_validation(
        val_root, day, validate_mod.merge_results([xsec_result, per_symbol_result])
    )

    cell = validation_store.read_cell(val_root, day)
    exceptions = validation_store.read_exceptions(val_root, day)
    clean_history_today = _build_clean_history(cell, cleanliness, day)
    history = validation_store.read_feature_day(val_root)  # for cross-day context (legacy trust source)
    states = lifecycle_state(clean_history_today, retired_features())
    defects = defect_rows(states, clean_history_today, exceptions, group_of, version_of)
    trust_lifecycle.write_lifecycle(states, defects, cleanliness, version_of, day)

    # Binary trust (docs/TRUST_REDESIGN.md): features that matched backfill within their per-type tolerance
    # on this CLEAN day earn TRUSTED — permanently, with provenance + a check-history row. Only NON_TRUSTED
    # features move; nothing is auto-demoted (the random check is the only un-trust path).
    earned = trust_binary.earned_features(clean_history_today, trust_binary.feature_policy_map())
    grant_counts = trust_binary.write_trust_grants(earned, [], clean_history_today, day)

    state_counts = states.group_by("lifecycle_state").len().sort("lifecycle_state").to_dicts() if states.height else []
    return {
        "day": day,
        "discovered": len(discovered),
        "materialized": len(materialized),
        "no_raw_skipped": len(no_raw),
        "no_raw_examples": no_raw[:10],
        "clean_symbols": clean_count,
        "contaminated_symbols": contaminated,
        "gather_coherent": bool(coherence["is_coherent"]),
        "gather_incoherent_frac": round(float(coherence["incoherent_frac"]), 3),
        "cross_sectional_graded": bool(coherence["is_coherent"]),
        "features_graded": states.height,
        "newly_trusted": grant_counts["earned_trusted"],
        "state_counts": {row["lifecycle_state"]: row["len"] for row in state_counts},
        "new_or_updated_defects": len(defects),
        "defect_features": [row[0] for row in defects][:20],
        "history_days_available": int(history["day"].n_unique()) if history.height else 0,
    }


def _parse_args(args: list[str]) -> dict[str, object]:
    allow_today = "--allow-today" in args
    with_ticks = "--no-ticks" not in args
    rest = [arg for arg in args if arg not in ("--allow-today", "--no-ticks")]
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
            "[raw_root] [--chunk N] [--allow-today] [--max-symbols N] [--no-ticks]"
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
        "with_ticks": with_ticks,
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
        with_ticks=parsed["with_ticks"],  # type: ignore[arg-type]
    )
    print(f"=== Parity-validation sweep summary for {day} ===")
    for key, value in summary.items():
        print(f"  {key}: {value}")


if __name__ == "__main__":
    main()
