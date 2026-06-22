"""The two LIVE paths agree: IncrementalEngine.step() == per-group compute_latest() (the batch), feature-
for-feature, across a minute stream. Together with test_fp_latest (batch == backfill) this closes the chain
backfill == batch == incremental — the same feature from one declaration, three execution paths."""

from __future__ import annotations

import datetime as dt
import math

import numpy as np
import polars as pl

from quantlib.features.base import BatchContext
from quantlib.features.compare import runnable
from quantlib.features.declarative import ReductionGroup
from quantlib.features.incremental import _TIME_ORIGIN_LAG, IncrementalEngine
from quantlib.features.reduction_anchor import attach_reduction_anchors
from quantlib.features.registry import REGISTRY

BASE = dt.datetime(2026, 6, 15, 13, 30, tzinfo=dt.timezone.utc)


def _anchored(frame: pl.DataFrame) -> pl.DataFrame:
    """Attach the per-symbol volume + close centering anchors (production attaches them via
    attach_reduction_anchors before either path runs), so the anchor-declaring groups (volume, trend_quality,
    clean_momentum) are runnable on these streams. Value-additive — the anchors are only consumed under the
    centered-std / FP_RUST_REDUCE flags; they make the groups selectable, never change a feature value."""
    daily = (
        frame.group_by("symbol")
        .agg(pl.col("volume").sum().alias("volume"), pl.col("close").last().alias("close"))
        .with_columns(pl.lit(1).alias("date"))
    )
    return attach_reduction_anchors({"minute_agg": frame, "daily": daily})["minute_agg"]


# The deepest declared ReductionGroup window is 180m (momentum/price_returns/trend_quality/volume/...). The
# incremental==batch parity test MUST stream past it, or the 90/120/180m windows are never fully populated and
# a deep-window breach (degenerate-cell sign/null flips from running-sum vs fresh-sum rounding) slips through
# silently. n_min=70 (the legacy default) only ever filled <=70m windows — exactly how the FP_INCREMENTAL
# deep-window breaches went uncaught. Stream >= 200m so every window, incl 180m, is fully filled and graded.
DEEPEST_WINDOW_M = 180
MIN_DEEP_STREAM_M = 200


def _stream(n_sym: int = 8, n_min: int = MIN_DEEP_STREAM_M) -> pl.DataFrame:
    rng = np.random.default_rng(7)
    rows = []
    price = {s: 100.0 + s for s in range(n_sym)}
    for mi in range(n_min):
        minute = BASE + dt.timedelta(minutes=mi)
        for s in range(n_sym):
            price[s] *= 1.0 + (rng.standard_normal() * 0.002)
            c = price[s]
            rows.append(
                {
                    "symbol": f"S{s}",
                    "minute": minute,
                    "open": c * 0.999,
                    "high": c * 1.002,
                    "low": c * 0.998,
                    "close": c,
                    "volume": 1000.0 + rng.random() * 4000,
                    "n_trades": float(rng.integers(1, 200)),
                    "signed_volume": rng.standard_normal() * 1000,
                    "mean_spread_bps": rng.random() * 5,
                    "quote_imbalance": rng.standard_normal() * 0.3,
                    "mean_bid_size": rng.random() * 100,
                    "mean_ask_size": rng.random() * 100,
                }
            )
    return _anchored(pl.DataFrame(rows).with_columns(pl.col("minute").cast(pl.Datetime("us", "UTC"))))


def _degenerate_stream(n_sym: int = 12, n_min: int = MIN_DEEP_STREAM_M, seed: int = 7) -> pl.DataFrame:
    """A deep stream engineered to hit the DEGENERATE-cell parity breach class: a third of the symbols are
    PERFECTLY LINEAR ramps (close-on-time R^2 -> 1, so the OLS SSR is a tiny difference of large near-equal
    sums) and a third are CONSTANT-FLAT (zero windowed variance, so the moment/variance defined-guards
    straddle their epsilon at float-noise level); the rest random-walk. At deep windows the incremental
    running-sum rounds differently from the batch fresh-sum, flipping a defined-guard NULL on/off — the exact
    null/non-null parity breach the production self-check (and LatencyDrive's real-06-18 equity A/B) flagged.
    A smooth random walk never produces these degenerate cells, which is why such breaches hid behind the old
    70m test."""
    rng = np.random.default_rng(seed)
    rows = []
    base_price = {s: 100.0 + s for s in range(n_sym)}
    walk = dict(base_price)
    linear_slope = {s: 1e-4 * (s + 1) for s in range(n_sym) if s % 3 == 0}
    flat = {s for s in range(n_sym) if s % 3 == 1}
    for mi in range(n_min):
        minute = BASE + dt.timedelta(minutes=mi)
        for s in range(n_sym):
            if s in linear_slope:
                close = base_price[s] * (1.0 + linear_slope[s] * mi)
            elif s in flat:
                close = base_price[s]
            else:
                walk[s] *= 1.0 + rng.standard_normal() * 0.002
                close = walk[s]
            rows.append(
                {
                    "symbol": f"S{s}",
                    "minute": minute,
                    "open": close * 0.999,
                    "high": close * 1.002,
                    "low": close * 0.998,
                    "close": close,
                    "volume": 1000.0 + rng.random() * 4000,
                    "n_trades": float(rng.integers(1, 200)),
                    "signed_volume": rng.standard_normal() * 1000,
                    "mean_spread_bps": rng.random() * 5,
                    "quote_imbalance": rng.standard_normal() * 0.3,
                    "mean_bid_size": rng.random() * 100,
                    "mean_ask_size": rng.random() * 100,
                }
            )
    return _anchored(pl.DataFrame(rows).with_columns(pl.col("minute").cast(pl.Datetime("us", "UTC"))))


def _assert_close(batch: pl.DataFrame, inc: pl.DataFrame, label: str) -> None:
    assert set(inc.columns) == set(batch.columns), f"{label}: columns differ"
    batch, inc = batch.sort("symbol"), inc.sort("symbol").select(batch.columns)
    for col in [c for c in batch.columns if c not in ("symbol", "minute")]:
        joined = batch.select("symbol", col).join(inc.select("symbol", pl.col(col).alias("_i")), on="symbol")
        bad = joined.filter(
            ~(
                (pl.col(col).is_null() & pl.col("_i").is_null())
                | ((pl.col(col) - pl.col("_i")).abs() <= 1e-6 + 1e-6 * pl.col(col).abs())
            )
        )
        assert bad.height == 0, f"{label}.{col}: {bad.height} mismatches\n{bad.head()}"


def test_incremental_step_matches_batch() -> None:
    stream = _stream()
    minutes = sorted(stream["minute"].unique())
    assert len(minutes) > DEEPEST_WINDOW_M, "stream must exceed the deepest 180m window to exercise it"
    groups = [g for g in runnable({"minute_agg": stream}) if isinstance(g, ReductionGroup)]
    engine = IncrementalEngine(groups)

    # warmup-ish, mid, a DEEP post-180m minute (the 90/120/180m windows are only filled here), and full-buffer.
    checkpoints = {10, 30, DEEPEST_WINDOW_M + 10, len(minutes) - 1}
    for ti, minute in enumerate(minutes):
        buffer = stream.filter(pl.col("minute") <= minute)
        inc = engine.step(buffer)
        if ti in checkpoints:
            ctx = BatchContext(frames={"minute_agg": buffer})
            for group in groups:
                _assert_close(group.compute_latest(ctx), inc[group.name], f"min{ti}:{group.name}")


# Groups whose incremental==batch parity BREACHES on degenerate cells at DEEP windows (60/120/180m) under the
# production self-check semantics (a null in one path that is non-null in the other = a HARD breach). Verified
# in this test by the degenerate stream below; reproduces LatencyDrive's real-06-18 equity A/B mechanism. This
# is NOT a pass-list to keep green forever — it is the explicit, tracked KNOWN-BREACHING set the n_min>=200
# widening now CATCHES (the 70m test never filled these windows, so the breach hid). STEP-3 (Lever-1: the
# centered-denom / defined-guard fixes, Lead-sequenced) flips each green one at a time; when a group is fixed,
# DELETE it from this set and the test's anti-vacuity check forces it to stay clean thereafter.
#
# NOTE on scope: a SYNTHETIC stream reproduces the degenerate-cell breach CLASS deterministically for the
# variance/moment defined-guard groups (here: ``distribution``) but does NOT reproduce the full real-data
# gap structure, so the broader 9-group set LatencyDrive measured on real 06-18 bars (trend_quality /
# range_expansion / momentum / trade_freq_z / clean_momentum / liquidity / distribution / volatility /
# momentum_consistency) is only fully confirmable on a real-data A/B. This test guarantees the deep windows
# are EXERCISED + the breach class is CAUGHT (non-vacuous); the real-data A/B remains the authority on the
# exact per-group arming list.
DEEP_WINDOW_KNOWN_BREACHERS: frozenset[str] = frozenset({"distribution"})

_PARITY_ATOL = 1e-6
_PARITY_RTOL = 1e-6
_PARITY_BREACH_RATIO = 10.0


def _group_parity_breaches(batch_frame: pl.DataFrame, inc_frame: pl.DataFrame | None) -> bool:
    """Production ``_incremental_parity`` scoped to one group's frame: True iff the worst divergence exceeds
    the breach ratio (a null/non-null flip is an infinite, hard breach)."""
    if inc_frame is None:
        return True
    cols = [c for c in batch_frame.columns if c not in ("symbol", "minute") and c in inc_frame.columns]
    joined = batch_frame.select(["symbol", *cols]).join(
        inc_frame.select(["symbol", *cols]), on="symbol", how="inner", suffix="__inc"
    )
    if joined.is_empty():
        return False
    for col in cols:
        a, b = pl.col(col), pl.col(f"{col}__inc")
        if joined.filter(a.is_null() != b.is_null()).height:
            return True
        ratio = joined.select(
            ((a - b).abs() / (_PARITY_ATOL + _PARITY_RTOL * a.abs())).fill_null(0.0).max()
        ).item()
        if ratio is not None and float(ratio) > _PARITY_BREACH_RATIO:
            return True
    return False


def test_incremental_matches_batch_deep_window_degenerate_cells() -> None:
    """⭐ The deep-window parity gate (the coverage gap that hid the FP_INCREMENTAL breaches). Streams past the
    180m deepest window with DEGENERATE cells (perfectly-linear + constant-flat symbols) and replicates the
    PRODUCTION process_bars split: a SEPARATE incremental engine over the ``incremental_safe=True`` groups and
    over the ``=False`` groups (no mixed-engine confound), graded with the production self-check semantics over
    the post-180m-warmup minutes.

    Two guarantees, both required for the test to be non-vacuous:
      1. Every group NOT in ``DEEP_WINDOW_KNOWN_BREACHERS`` stays parity-clean at deep windows (the real
         regression guard — a future change that breaks a clean group's deep-window parity FAILS here).
      2. Every group IN ``DEEP_WINDOW_KNOWN_BREACHERS`` actually DOES breach (proves the widened windows EXERCISE
         the breach class — if a documented breacher stops breaching it has been FIXED and must be removed from
         the set, which this assertion forces).
    """
    stream = _degenerate_stream()
    minutes = sorted(stream["minute"].unique())
    assert len(minutes) > DEEPEST_WINDOW_M, "degenerate stream must exceed the 180m window"
    groups = [g for g in runnable({"minute_agg": stream}) if isinstance(g, ReductionGroup)]
    safe = [g for g in groups if g.incremental_safe]
    unsafe = [g for g in groups if not g.incremental_safe]
    eng_safe, eng_unsafe = IncrementalEngine(safe), IncrementalEngine(unsafe)

    ever_breached: set[str] = set()
    graded = 0
    for ti, minute in enumerate(minutes):
        buffer = stream.filter(pl.col("minute") <= minute)
        inc_safe = eng_safe.step(buffer, slice_derive=True)
        inc_unsafe = eng_unsafe.step(buffer, slice_derive=True)
        if ti <= DEEPEST_WINDOW_M:  # only grade once every window incl 180m is fully filled
            continue
        graded += 1
        ctx = BatchContext(frames={"minute_agg": buffer})
        for group in safe:
            batch = group.compute_latest(ctx)
            if _group_parity_breaches(batch, inc_safe[group.name]):
                ever_breached.add(group.name)
        for group in unsafe:
            batch = group.compute_latest(ctx)
            if _group_parity_breaches(batch, inc_unsafe[group.name]):
                ever_breached.add(group.name)

    assert (
        graded > 0
    ), "no post-180m-warmup minutes were graded — the stream did not exceed the deepest window"

    safe_names = {g.name for g in safe}
    # GUARANTEE 1: no SAFE group outside the documented set may breach at deep windows.
    unexpected = (ever_breached & safe_names) - DEEP_WINDOW_KNOWN_BREACHERS
    assert not unexpected, (
        f"SAFE groups breached incremental==batch at deep windows but are NOT documented breachers: "
        f"{sorted(unexpected)} — either a regression, or arm-blocking breachers to add to "
        f"DEEP_WINDOW_KNOWN_BREACHERS (and gate incremental_safe=False) before arming FP_INCREMENTAL."
    )
    # GUARANTEE 2: each documented breacher must STILL breach (else it is fixed -> remove it -> non-vacuous).
    no_longer_breaching = DEEP_WINDOW_KNOWN_BREACHERS - ever_breached
    assert not no_longer_breaching, (
        f"{sorted(no_longer_breaching)} no longer breach at deep windows — if a Lever-1 fix landed, REMOVE them "
        f"from DEEP_WINDOW_KNOWN_BREACHERS so the test holds them clean going forward."
    )


def test_slice_derive_matches_whole_buffer() -> None:
    """V2 slice-derive guard: the (n_symbols, n_value_cols) matrix the engine builds for the latest minute —
    short-lag columns over a small slice + stateful regressors (OBV cumulative, time axis) from running state —
    equals the whole-buffer derive (its V1 source of truth), cell-for-cell, at every minute past warmup. This
    pins the slice-derive optimization to the value level (independent of the assemble that follows)."""
    stream = _stream(n_sym=6, n_min=64)
    minutes = sorted(stream["minute"].unique())
    groups = [g for g in runnable({"minute_agg": stream}) if isinstance(g, ReductionGroup)]
    engine = IncrementalEngine(groups)
    engine.symbols = sorted(stream["symbol"].unique().to_list())
    engine._seed_stateful(stream)

    for minute in minutes:
        buffer = stream.filter(pl.col("minute") <= minute)
        # whole-buffer derive of the slice-safe columns (V1 path) — the reference for the short-lag columns
        whole = engine._derived_row(buffer, minute)
        whole_safe = whole.select(engine.safe_value_cols).fill_null(0.0).to_numpy()
        sliced = engine._matrix_at(buffer, minute, slice_derive=True)
        for safe_i, col in enumerate(engine.safe_value_cols):
            ref = whole_safe[:, safe_i]
            got = sliced[:, engine.col_index[col]]
            assert np.allclose(
                ref, got, rtol=1e-9, atol=1e-9
            ), f"{minute} {col}: slice != whole-buffer derive"


def _sparse_stream(n_dense: int = 6, n_min: int = 64, gap: int = 10) -> pl.DataFrame:
    """A dense stream (every symbol every minute) PLUS one sparse symbol ``SP`` that prints only every ``gap``
    minutes (gaps far larger than the legacy DERIVE_SLICE window). At a minute where SP prints, its positional
    prior bar (``close.shift(1).over("symbol")``) is ``gap`` minutes back — a minute-window slice would miss it
    and slice-derive a wrong null lag; the per-symbol row tail reaches it."""
    base = _stream(n_sym=n_dense, n_min=n_min)
    rng = np.random.default_rng(11)
    price = 250.0
    rows = []
    template = base.row(0, named=True)
    for mi in range(0, n_min, gap):
        minute = BASE + dt.timedelta(minutes=mi)
        price *= 1.0 + rng.standard_normal() * 0.003
        row = dict(template)
        row.update(
            {
                "symbol": "SP",
                "minute": minute,
                "open": price * 0.999,
                "high": price * 1.002,
                "low": price * 0.998,
                "close": price,
                "volume": 2000.0,
            }
        )
        rows.append(row)
    sparse = (
        pl.DataFrame(rows).with_columns(pl.col("minute").cast(pl.Datetime("us", "UTC"))).select(base.columns)
    )
    return pl.concat([base, sparse]).sort(["symbol", "minute"])


def test_slice_derive_sparse_symbol_matches_whole_buffer() -> None:
    """REGRESSION (OPEN PARITY CONSTRAINT, resolved): a symbol that skips minutes still assembles cell-for-cell
    equal to the gap-safe whole-buffer derive. Positional lags need the k-th prior ROW (however far back in
    time), so the slice must tail by ROW per symbol, not by a fixed minute window. Two engines step the same
    sparse stream — one slicing (fast), one whole-buffer (truth); their features must agree at every minute.
    Under the old minute-window slice the sparse symbol's lag columns were a wrong null at its print minutes,
    diverging the running sums — this test would have caught that."""
    stream = _sparse_stream()
    minutes = sorted(stream["minute"].unique())
    groups = [g for g in runnable({"minute_agg": stream}) if isinstance(g, ReductionGroup)]
    eng_slice = IncrementalEngine(groups)
    eng_whole = IncrementalEngine(groups)
    assert (
        eng_slice.max_lag >= 1
    )  # the sparse gap (10) must exceed max_lag (and the legacy DERIVE_SLICE) to bite

    sp_minutes = set(stream.filter(pl.col("symbol") == "SP")["minute"].to_list())
    checked_sparse = 0
    for minute in minutes:
        buffer = stream.filter(pl.col("minute") <= minute)
        out_slice = eng_slice.step(buffer, slice_derive=True)
        out_whole = eng_whole.step(buffer, slice_derive=False)
        for group in groups:
            _assert_close(out_whole[group.name], out_slice[group.name], f"{minute}:{group.name}")
        if minute in sp_minutes and minute > min(sp_minutes):
            checked_sparse += 1
    assert checked_sparse >= 3, "test did not exercise enough sparse-symbol print minutes past its first bar"


def test_time_origin_rolls_to_keep_ols_x_bounded() -> None:
    """The time-OLS origin ROLLS forward each minute so the regressor's x stays O(1) over a long session,
    instead of growing unbounded from a fixed seed origin (which makes ``b·Σxx − (Σx)²`` a difference of large
    near-equal sums — the near-perfect-fit conditioning hazard). After many minutes the latest minute's x must
    be pinned at ``_TIME_ORIGIN_LAG`` and the in-window x must stay small, NOT grow with session age."""
    stream = _stream(n_sym=6, n_min=120)
    minutes = sorted(stream["minute"].unique())
    groups = [
        g
        for g in runnable({"minute_agg": stream})
        if isinstance(g, ReductionGroup) and g.name == "trend_quality"
    ]
    engine = IncrementalEngine(groups)
    assert engine.time_ols_cols, "trend_quality declares a time regressor; its OLS cols must be tracked"

    for minute in minutes:
        engine.step(stream.filter(pl.col("minute") <= minute), slice_derive=False)

    latest_epoch = int(minutes[-1].timestamp())
    assert engine.ref_epoch is not None
    latest_x = (latest_epoch - engine.ref_epoch) / 60.0
    assert (
        latest_x == _TIME_ORIGIN_LAG
    ), f"latest minute's x should be pinned at {_TIME_ORIGIN_LAG}, got {latest_x}"

    # The largest in-window x magnitude must stay bounded by the session's longest window, NOT the session age
    # (120 min). Read the running x/b sums for the longest window and bound the per-bar mean x.
    ns = next(iter(engine.stateful_specs))
    longest = max(engine.windows)
    sums = engine.state.sums(longest)  # type: ignore[union-attr]
    x_col = sums[:, engine.col_index[f"__rd_{ns}_x"]]
    b_col = sums[:, engine.col_index[f"__rd_{ns}_b"]]
    mean_x = x_col[b_col > 0] / b_col[b_col > 0]
    assert np.all(
        np.abs(mean_x) <= longest + 1
    ), f"in-window x grew past the longest window ({longest}): {mean_x}"


def test_sqrt_features_clip_negative_residue_to_zero_not_nan() -> None:
    """REGRESSION (real-data parity audit on DIS/C/VZ): parkinson_vol / upside_vol / downside_vol are the sqrt
    of a mathematically NON-NEGATIVE windowed quantity. The LIVE IncrementalEngine sums those columns with a
    running add/expire cycle that, for an all-flat / one-signed sparse symbol, drifts the sum to a TINY NEGATIVE
    residue (~−1e−22) — and an UNclipped ``sqrt`` of that is NaN, while the backfill rolling sum is exactly 0.0
    (a null/NaN-vs-value parity break). The fix clips the non-negative quantity to >=0 before the sqrt. This
    pins the FIX deterministically: a tiny-negative canonical-aggregate value must assemble to 0.0, NEVER NaN.
    (A live stream can't reliably be made to drift the residue negative on demand, so we feed the negative
    directly into each group's assemble expressions — the exact code path the clip protects.)"""
    for group_name, agg_cols, feature_cols in (
        ("volatility", {"__mean_hl2_15": -1e-22}, ["parkinson_vol_15m"]),
        (
            "distribution",
            {"__sum_up2_15": -1e-22, "__sum_dn2_15": -1e-22, "__sum_p_15": 5.0},
            ["upside_vol_15m", "downside_vol_15m"],
        ),
    ):
        group = REGISTRY.get_group(group_name)
        wide = pl.DataFrame({"symbol": ["X"], **{col: [val] for col, val in agg_cols.items()}})
        feats = group.assemble()
        out = wide.with_columns([feats[name].cast(pl.Float64).alias(name) for name in feature_cols])
        for name in feature_cols:
            value = out[name][0]
            assert value is not None and not math.isnan(value), (
                f"{group_name}.{name}: tiny-negative running-sum residue produced {value} (sqrt-of-negative "
                f"NaN) — the clip-to-zero fix is missing"
            )
            assert value == 0.0, f"{group_name}.{name}: clipped residue should be exactly 0.0, got {value}"


def _flat_name_stream(n_min: int = MIN_DEEP_STREAM_M) -> pl.DataFrame:
    """A stream with a FLAT-RETURN name (FLAT grinds one direction by the same tiny amount each minute, so its
    one-minute return — and its lagged return — is a constant, making return_dynamics' autocorrelation regressor
    x EXACTLY constant → Σxx == 0 with no float residue) alongside an ordinary noisy name (so the engine has the
    normal columns too). This is the degenerate cell the shared-engine rebase interaction corrupted."""
    rows = []
    rng = np.random.default_rng(11)
    flat_price, noisy_price = 100.0, 250.0
    for mi in range(n_min):
        minute = BASE + dt.timedelta(minutes=mi)
        flat_price *= 1.001  # EXACT constant return -> lagged-return regressor is constant -> Σxx == 0
        noisy_price *= 1.0 + rng.standard_normal() * 0.002
        for sym, close in (("FLAT", flat_price), ("NOISY", noisy_price)):
            rows.append(
                {
                    "symbol": sym, "minute": minute, "open": close * 0.999, "high": close * 1.002,
                    "low": close * 0.998, "close": close, "volume": 1000.0 + rng.random() * 4000,
                    "n_trades": float(rng.integers(1, 200)), "signed_volume": rng.standard_normal() * 1000,
                    "mean_spread_bps": rng.random() * 5, "quote_imbalance": rng.standard_normal() * 0.3,
                    "mean_bid_size": rng.random() * 100, "mean_ask_size": rng.random() * 100,
                }
            )
    return _anchored(pl.DataFrame(rows).with_columns(pl.col("minute").cast(pl.Datetime("us", "UTC"))))


def test_co_resident_time_ols_group_does_not_perturb_unanchored_group() -> None:
    """⭐ SHARED-ENGINE INVARIANCE (the price_volume × return_dynamics interaction): an unanchored corr group's
    incremental output MUST be bit-identical whether or not a co-resident TIME-OLS group (one declaring a
    ``kind="time"`` regression — price_volume.obv / trend_quality.trend) shares the IncrementalEngine. The
    time-OLS group triggers the per-minute ``WindowedSumState.rebase_time_axis``; if that rebase realizes the
    Neumaier compensation across the WHOLE shared array (not just the time-OLS columns it shifts), it collapses
    a flat-name ``Σxx``-exactly-zero cell into a ~1e-22 residue → the corr defined-guard straddles and the
    engine emits a spurious value where the batch (and the standalone engine) NULL — a shared-engine-only
    parity breach. This pins the fix: rebase must touch ONLY its own columns, so a co-resident group folds
    exactly as standalone."""
    stream = _flat_name_stream()
    minutes = sorted(stream["minute"].unique())
    runnable_groups = {g.name: g for g in runnable({"minute_agg": stream}) if isinstance(g, ReductionGroup)}
    rd = runnable_groups["return_dynamics"]
    time_ols = runnable_groups["trend_quality"]  # declares a kind="time" regression -> triggers the rebase

    eng_solo = IncrementalEngine([rd])
    eng_shared = IncrementalEngine([rd, time_ols])
    assert eng_shared.time_ols_cols, "the co-resident group must declare a time regression (else vacuous)"

    autocorr_cols = [c for c in rd.feature_names if c.startswith("autocorr_")]
    saw_flat_degenerate = False
    for minute in minutes:
        buffer = stream.filter(pl.col("minute") <= minute)
        out_solo = eng_solo.step(buffer, slice_derive=True)["return_dynamics"].sort("symbol")
        out_shared = eng_shared.step(buffer, slice_derive=True)["return_dynamics"].sort("symbol")
        for name in rd.feature_names:
            solo = out_solo[name].to_numpy()
            shared = out_shared[name].to_numpy()
            # null mask must match exactly (the breach was a null/non-null flip), and finite values bit-identical
            assert np.array_equal(np.isnan(solo), np.isnan(shared)), (
                f"{name} @ {minute}: co-resident time-OLS group flipped a null vs standalone — the shared-engine "
                f"rebase perturbed an unanchored group's sums (solo={solo} shared={shared})"
            )
            both = ~np.isnan(solo)
            if both.any():
                assert np.array_equal(solo[both], shared[both]), (
                    f"{name} @ {minute}: co-resident time-OLS group changed a finite value vs standalone"
                )
        # anti-vacuity: the FLAT name (exactly-constant return) must reach the degenerate Σxx≈0 regime past
        # warmup — its autocorrelation is undefined (NULL on the correct path), which is EXACTLY the cell the
        # rebase residue would flip to non-null in the shared engine. Confirming the FLAT row is present with
        # NULL autocorr proves the test exercises that path (not just all-warmup rows).
        flat_solo = out_solo.filter(pl.col("symbol") == "FLAT")
        if flat_solo.height and all(flat_solo[c][0] is None for c in autocorr_cols):
            saw_flat_degenerate = True
    assert saw_flat_degenerate, (
        "the FLAT name never reached a degenerate (null-autocorr) cell past warmup — test is vacuous"
    )
