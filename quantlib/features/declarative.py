"""Declarative windowed-reduction features — declare the reduction ONCE, get BOTH forms.

A ``ReductionGroup`` subclass declares three things instead of hand-writing two parallel implementations:
  - ``reduced()``  -> {name: (expr, stats)}  the value columns to reduce over each window, and which
                     statistics it needs of each ("mean" | "std" | "sum"),
  - ``points()``   -> {name: expr}           at-T scalar columns (the latest minute's values),
  - ``assemble()`` -> {feature: expr}        the features, written with the agg accessors
                     ``mean_(col, w)`` / ``std_(col, w)`` / ``sum_(col, w)`` / ``pt_(name)``.

From that ONE declaration the engine GENERATES:
  - ``compute()``         — the rolling form over every minute (backfill / source of truth), materialised
                            with polars ``rolling_*_by`` (so it stays bit-identical to a hand-written group),
  - ``compute_latest()``  — the aggregate-at-T form, one row per symbol, materialised with the single-pass
                            Rust kernels (``rust_reductions``).
Both forms materialise the SAME canonical aggregate columns (``__mean_<col>_<w>`` etc.) and then evaluate
the SAME ``assemble()`` expressions — so they cannot diverge by more than the kernel-vs-rolling float noise
the parity test already tolerates. The modeller writes it ONCE; production (live) and modeling (backfill)
ride the same declaration. Genuinely-weird features still subclass ``FeatureGroup`` directly and write
arbitrary polars — this is the fast lane for the common windowed-reduction shape, not a cage.
"""
from __future__ import annotations

import os
from abc import abstractmethod
from dataclasses import dataclass

import numpy as np
import polars as pl
import quant_tick

from quantlib.features import _phase
from quantlib.features.base import BatchContext, FeatureGroup
from quantlib.features.latest import pivot_stat, rust_reductions, rust_windowed_sums

# The reduction EMIT (building each group's canonical __<stat>_<col>_<w> columns from the running sums) is
# the fast-path floor. FP_RUST_ASSEMBLE moves that canonical column algebra into the ``assemble_canonical``
# Rust kernel (one pass over the whole running-sum array, NaN==null by construction); the numpy/polars
# ``emit_numpy`` stays the parity reference (FP_RUST_ASSEMBLE unset, or FP_RUST_ASSEMBLE=0).
_USE_RUST_ASSEMBLE = bool(os.environ.get("FP_RUST_ASSEMBLE")) and os.environ.get("FP_RUST_ASSEMBLE") != "0"

# FP_CENTERED_TIME conditions the single-anchor BATCH time-axis OLS the SAME way the incremental engine does —
# it pins the time regressor's x at the anchor minute to the incremental engine's small origin
# (``latest − _TIME_ORIGIN_LAG·60``) so the operand sums (Σxy, Σxx, Σx) stay
# small and ``cov_n = b·Σxy − Σx·Σy`` / ``denom_x = b·Σxx − (Σx)²`` are no longer catastrophic-cancellation
# differences of large near-equal sums. The fix is VALUE-IDENTICAL on well-conditioned cells (OLS is
# origin-invariant — see ``_pinned_time_x``), it only removes the float ill-conditioning that made
# the batch fresh-sum path and the small-origin incremental path round a near-perfect fit's r2/corr/slope
# differently (the time-axis corr-denom breach that gated trend_quality / clean_momentum / residual_analysis /
# price_volume's obv_slope from FP_INCREMENTAL — docs/INCREMENTAL_READINESS.md §Parked). Default OFF keeps the
# batch expression graph byte-identical to today (fp unchanged); the prod flip is a Lead/Ben relaunch click.
_USE_CENTERED_TIME = bool(os.environ.get("FP_CENTERED_TIME")) and os.environ.get("FP_CENTERED_TIME") != "0"

# FP_RUST_REDUCE conditions the OLS R²/corr/resid-std Y-SIDE the way #386 conditioned the time-axis X: it
# CENTERS a regression's ``y`` on a per-symbol-constant anchor (the daily-bar close, attached by
# ``attach_close_anchor``) BEFORE the six paired sums are accumulated, so the y-variance / covariance terms
# ``denom_y = b·Σ(y−a)² − (Σ(y−a))²`` and ``cov_n = b·Σ(x·(y−a)) − Σx·Σ(y−a)`` stay conditioned on small
# centered close (~±$2) instead of raw close (~$45–$500). The near-perfect-fit r²/corr cancellation that
# #386's x-conditioning could NOT reach (the y-side SSR/SST straddle that gated trend_quality / clean_momentum
# / residual_analysis from FP_INCREMENTAL — docs/INCREMENTAL_READINESS.md §"REAL-TAPE PROMOTION GATE") then
# rounds IDENTICALLY in the batch fresh-sum path and the incremental running-sum path. OLS is
# translation-invariant in y, so slope/r²/corr/resid_std are value-identical to the raw form in exact
# arithmetic — only the float conditioning changes (fp unchanged). Because the centering is applied UPSTREAM
# of the windowed sum (on the paired columns), all three emit twins (polars / numpy / Rust assemble_canonical)
# consume the conditioned sums with NO twin-specific change. Default OFF keeps the paired-column expression
# graph byte-identical to today (fp unchanged); the prod flip is a Lead/Ben relaunch click.
_USE_RUST_REDUCE = bool(os.environ.get("FP_RUST_REDUCE")) and os.environ.get("FP_RUST_REDUCE") != "0"

# How far (minutes) behind the anchor minute to pin the latest-row time-OLS origin (compute_latest /
# compute_reduction_batch). IDENTICAL to the incremental engine's per-fold pin so the conditioned batch axis
# matches the incremental axis at the anchor minute exactly (incremental.py re-exports this constant). Small
# and fixed so every in-window x stays O(1).
_TIME_ORIGIN_LAG = 2

# Statistic codes shared with the Rust ``assemble_canonical`` kernel (kind byte). The OLS codes' order
# (slope, corr, r2, mean_y) matches the kernel's 3..=6 arm; ``resid_std`` (7) is the OLS residual-std
# stat the kernel's 7 arm computes. Codes 8/9 are the FP_RUST_REDUCE y-centered (anchored) corr/r2 twins —
# IDENTICAL arithmetic to 4/5 but with the centered-variance denom_y guard (``eps·b·syy`` not ``eps·sy²``),
# so the Rust assemble matches the polars/numpy anchored stat. The Python ``build_assemble_plan`` emits 8/9
# (instead of 4/5) for a regression whose ``ns`` is in the anchored set.
_STAT_CODE = {"sum": 0, "mean": 1, "std": 2, "slope": 3, "corr": 4, "r2": 5, "mean_y": 6, "resid_std": 7}
# Per-stat kind override for an anchored (y-centered) regression: corr/r2 use the centered-variance guard.
_STAT_CODE_ANCHORED = {"corr": 8, "r2": 9}

# OLS residual-std (``resid_std``) degenerate guards — the residual_analysis group's two thresholds, baked
# into the shared stat so backfill/live-batch/incremental compute them identically (the §7 Lever-2 move that
# folds residual_analysis onto the reduction fast path). ``MIN_POINTS`` = the minimum paired count for a
# meaningful residual distribution; ``REL_RESID_FLOOR`` = a relative residual-spread cutoff (a near-perfectly
# linear window's residual variance collapses to f32 noise of the price level — a meaningless ~1e-6%% reading
# whose low bits are accumulation-order-sensitive, so gate on resid_std exceeding this fraction of mean price
# to make the value well-defined and identical on every path). These mirror residual_analysis's old constants.
_RESID_MIN_POINTS = 4.0
_RESID_REL_FLOOR = 1e-6

# Relative floor on each OLS variance numerator (``denom_x = b*Σx² − (Σx)²`` for slope/corr/r2's defined-guard,
# ``denom_y = b*Σy² − (Σy)²`` for corr/r2) — the SIGN-at-threshold trap from #122/#131. On a near-flat
# regressor (or regressand) the numerator is a catastrophic-cancellation difference of two near-equal large
# sums whose low bits are sensitive to accumulation order, so the backfill rolling sums and the live kernel
# sums land it on OPPOSITE sides of zero at the ~1e-16 (machine-eps) relative level. A bare ``denom > 0.0``
# then sends one path to a finite stat and the other to NULL: a stream-vs-backfill parity break on
# degenerate-flat names (e.g. kyle_lambda on a constant-signed-flow window). Require each numerator to be a
# non-trivial fraction of its own scale ((Σx)² / (Σy)²) so a genuinely-flat window is NULL on BOTH paths.
#
# The floor is 1e-10 (raised from 1e-12). For a corr/r2 group whose regressor (and/or regressand) is a small
# one-minute RETURN — return_dynamics (lagged-return vs return), market_beta (SPY-return broadcast), price_volume
# (return vs volume) — the batch fresh-sum and incremental running-sum paths do NOT just round zero differently;
# on a near-CONSTANT-return window the ratio ``denom_x/(Σx)²`` lands the cell in [1e-12, ~3e-12], where the
# corr is ``noise/noise`` (a meaningless reading on a flat window) and the two paths' ~1-ULP sum difference is
# AMPLIFIED by the tiny denom into a >tol value Δ (MEASURED 4.5e-4 on a 60m autocorr cell at the old 1e-12
# floor). 1e-12 sat just BELOW that breach band, so it admitted the degenerate cell on both paths but let them
# disagree. 1e-10 nulls the whole degenerate band on BOTH paths (the CORRECT answer — a flat-return window has
# no defined autocorrelation/beta), and is cleanly separated from real signal: on realistic-volatility returns
# the ratio min is ~4.5e-5 (CV² ≈ O(0.01–1)), 5+ decades ABOVE 1e-10 — so NO well-conditioned window is nulled
# (MEASURED 0.000% of realistic cells affected across return vols 1e-4..1e-3). Same principle as the centered-y
# ``_OLS_DENOM_Y_CENTERED_REL_EPS = 1e-9`` floor (#386): clear the running-sum noise, stay far below real variance.
# This is a VALUE CHANGE on the degenerate band only (per-group version bump + re-trust), un-gating
# return_dynamics + market_beta + price_volume's return-side denom from the incremental fast path.
_OLS_DENOM_X_REL_EPS = 1e-10
_OLS_DENOM_Y_REL_EPS = 1e-10

# Centered-y (FP_RUST_REDUCE anchored regression) denom_y guard eps, relative to the variance scale ``b·syy``.
# When y is centered on the per-symbol anchor the RAW ``eps·sy²`` scale collapses (sy → small), so the guard
# is re-based on the translation-invariant variance scale ``b·syy``. A LARGER eps than the raw 1e-12 is
# required because the centered ``denom_y = b·syy − sy²`` at the b==2 / near-flat corner carries the
# incremental running-sum accumulation noise (~1e-12 RELATIVE to ``b·syy``, well above machine eps — measured
# 4.2e-15 on a KO flat 5m cell where ``b·syy`` ≈ 3.6e-3), so a 1e-12 floor sits AT that noise and a genuinely
# flat window straddles it (batch denom_y == 0 → null, incremental denom_y == 4e-15 → r2 = 1.0). 1e-9 clears
# the running-sum noise by ~1000× while staying far below any real fit's ratio (denom_y/(b·syy) ≈ 1 − the
# mean-fraction, O(1) for a centered window), so a genuinely-flat window is NULL on BOTH paths and a real fit
# is accepted on both — the straddle band is negligibly thin. Only consulted for an anchored regression.
_OLS_DENOM_Y_CENTERED_REL_EPS = 1e-9

# n==2 perfect-fit corner. Two distinct (x, y) points define a line EXACTLY, so the OLS fit through them is
# perfect: r2 == 1.0, corr == sign(slope) == sign(cov). Computed from the sums, ``r2 = cov²/(denom_x·denom_y)``
# at b==2 is ``noise/noise`` — a ratio of two cancellation differences that lands at ``1.0 ± ε`` (often
# slightly ABOVE 1.0, impossible for an R²), and the batch fresh sums and the incremental running sums round
# that ε differently (the residual batch-vs-incremental breach after the origin-rebase — entirely the b==2
# cells: price_r2 / pv_correlation / clean_momentum). Emit the EXACT value at b==2 in all three twins (polars /
# numpy / rust assemble_canonical) so both paths agree cell-for-cell AND the value is mathematically correct
# (1.0 is the true r2 of a line through two points; the prior 0.9998–1.0001 was float noise). Touches ONLY the
# b==2 cells, so it is a degenerate-cell value change -> a per-group version bump + re-trust on the affected groups.
_OLS_PERFECT_FIT_COUNT = 2.0

# Agg accessors — used inside assemble() to reference the canonical aggregate columns the engine builds.
STATS = ("mean", "std", "sum")


def mean_(col: str, w: int) -> pl.Expr:
    return pl.col(f"__mean_{col}_{w}")


def std_(col: str, w: int) -> pl.Expr:
    return pl.col(f"__std_{col}_{w}")


def sum_(col: str, w: int) -> pl.Expr:
    return pl.col(f"__sum_{col}_{w}")


def pt_(name: str) -> pl.Expr:
    return pl.col(f"__pt_{name}")


# OLS (regression) accessors — used inside assemble() to reference a regression's canonical stat columns.
OLS_STATS = ("slope", "corr", "r2", "mean_y", "resid_std")


def slope_(name: str, w: int) -> pl.Expr:
    return pl.col(f"__slope_{name}_{w}")


def corr_(name: str, w: int) -> pl.Expr:
    return pl.col(f"__corr_{name}_{w}")


def r2_(name: str, w: int) -> pl.Expr:
    return pl.col(f"__r2_{name}_{w}")


def mean_y_(name: str, w: int) -> pl.Expr:
    return pl.col(f"__mean_y_{name}_{w}")


def resid_std_(name: str, w: int) -> pl.Expr:
    return pl.col(f"__resid_std_{name}_{w}")


def _ols_stat_exprs(
    sums: dict[str, pl.Expr], stats: tuple[str, ...], *, anchored: bool = False
) -> dict[str, pl.Expr]:
    """OLS slope/corr/r2/mean_y of y-on-x from the six paired windowed sums (b=paired count, x, y, xy, xx,
    yy). Identical algebra to ols.py — pairing handled by the caller (partner-null rows zeroed, excluded
    from b). Undefined cells (n<2 or zero x-variance) are null.

    ``anchored`` (set under ``FP_RUST_REDUCE`` for a y-centered regression): the regressand ``y`` was centered
    on a per-symbol anchor, so ``sy = Σ(y−a)`` is small and the raw ``denom_y > eps·sy²`` guard's scale
    collapses (a near-flat window would spuriously straddle). Use the TRANSLATION-INVARIANT variance scale
    ``eps·b·syy`` instead — both the batch fresh-sum path and the incremental running-sum path compute
    ``denom_y``/``b·syy`` from the SAME bit-identical centered sums, so they NEVER straddle, and a genuinely
    flat window (denom_y ≈ 0) is rejected on both. On a well-conditioned cell denom_y ≈ b·syy ≫ eps·b·syy, so
    the null decision is unchanged from the raw guard — value-identical."""
    b, sx, sy, sxy, sxx, syy = (sums[key] for key in ("b", "x", "y", "xy", "xx", "yy"))
    denom_x = b * sxx - sx * sx
    denom_y = b * syy - sy * sy
    cov_n = b * sxy - sx * sy
    denom_y_scale = (b * syy) if anchored else (sy * sy)
    denom_y_eps = _OLS_DENOM_Y_CENTERED_REL_EPS if anchored else _OLS_DENOM_Y_REL_EPS
    defined = (b >= 2.0) & (denom_x > _OLS_DENOM_X_REL_EPS * (sx * sx))
    defined_corr = defined & (denom_y > denom_y_eps * denom_y_scale)
    perfect = defined_corr & (b == _OLS_PERFECT_FIT_COUNT)  # line through 2 points: r2==1, corr==sign(cov)
    out: dict[str, pl.Expr] = {}
    if "slope" in stats:
        out["slope"] = pl.when(defined).then(cov_n / denom_x).otherwise(None)
    if "corr" in stats:
        corr = pl.when(defined_corr).then(cov_n / (denom_x * denom_y).sqrt()).otherwise(None)
        out["corr"] = pl.when(perfect).then(cov_n.sign()).otherwise(corr)
    if "r2" in stats:
        r2 = pl.when(defined_corr).then((cov_n * cov_n) / (denom_x * denom_y)).otherwise(None)
        out["r2"] = pl.when(perfect).then(pl.lit(1.0)).otherwise(r2)
    if "mean_y" in stats:
        out["mean_y"] = pl.when(b > 0).then(sy / b).otherwise(None)
    if "resid_std" in stats:
        # OLS residual std (percent of mean y) from the SAME six sums — the exact centered-sum algebra the
        # hand-written residual_analysis used (Σ.._c = Σ.. − Σa·Σb/n), so the difference-of-sums rounds
        # identically. Σr² = syy_c − slope·sxy_c (clipped ≥0); resid_var = Σr²/n; std% = √resid_var/ȳ·100.
        sxx_c = sxx - sx * sx / b
        sxy_c = sxy - sx * sy / b
        syy_c = syy - sy * sy / b
        slope_r = sxy_c / sxx_c
        ssr = (syy_c - slope_r * sxy_c).clip(lower_bound=0.0)
        mean_y = sy / b
        resid_var = ssr / b
        resid_floor = (_RESID_REL_FLOOR * mean_y).pow(2)
        resid_defined = (b >= _RESID_MIN_POINTS) & (sxx_c > 0.0) & (resid_var > resid_floor)
        out["resid_std"] = pl.when(resid_defined).then(resid_var.sqrt() / mean_y * 100.0).otherwise(None)
    return out


@dataclass(frozen=True)
class StatefulRegressor:
    """Declares that a regression's ``slot`` (``"x"`` or ``"y"``) is NOT a short-lag PER-SYMBOL column the
    incremental engine can slice-derive, but a value the engine must source specially each minute. Three kinds:

    - ``kind="time"``: a frame-relative time axis ``(epoch_minutes - origin)``. Slice-derive can't reproduce
      a frame-relative origin, so the engine substitutes a FIXED origin (its seed minute). OLS is
      origin-invariant, so slope/r2/corr are identical to the batch's per-frame-centered axis within tol.
    - ``kind="cumulative"``: a running total ``v[T] = v[T-1] + increment[T]`` (e.g. OBV = cum_sum(signed)).
      The group provides ``increment`` (a short-lag expr the engine evaluates per minute) and the engine keeps
      the running per-symbol total. The centered-time *partner* slot may also be ``"time"``.
    - ``kind="broadcast"``: a CROSS-SYMBOL value that is the SAME for every symbol at a given minute — a market
      index's per-minute value broadcast to the whole universe (e.g. SPY's one-minute return as the market-beta
      regressor). The group provides ``broadcast_symbol`` (the index ticker whose row carries the value) and
      ``increment`` (the short-lag expr that yields that ticker's per-minute value, e.g. ``close/close.shift(1)
      - 1``). Each minute the engine reads that expr at the index symbol's row and broadcasts it to all symbols'
      slot — the cross-symbol minute-join the batch path does, without a per-symbol rolling.

    Backfill / live-batch ignore this entirely — they evaluate the group's own ``regressions()`` exprs
    directly (over a frame the group's ``prepare`` has already broadcast onto). It only tells the incremental
    engine HOW to source that regressor instead of re-deriving over the buffer."""

    slot: str  # "x" or "y"
    kind: str  # "time" | "cumulative" | "broadcast"
    increment: pl.Expr | None = None  # required iff kind in {"cumulative", "broadcast"}
    broadcast_symbol: str | None = (
        None  # required iff kind == "broadcast" (the index ticker carrying the value)
    )


_OLS_KEYS = ("b", "x", "y", "xy", "xx", "yy")


def _ols_derived(
    name: str, x_expr: pl.Expr, y_expr: pl.Expr, y_anchor: pl.Expr | None = None
) -> list[pl.Expr]:
    """The six paired columns the engine sums for one regression: only rows where BOTH x and y are present
    contribute (partner-null zeroed and dropped from the count), so a warmup/missing value never biases the
    fit. Column names ``__rd_<name>_{b,x,y,xy,xx,yy}``.

    ``y_anchor`` (set only under ``FP_RUST_REDUCE`` for an anchored regression) is a per-symbol-constant
    column that ``y`` is CENTERED on (``y → y − a``) before the paired y/xy/yy products are formed, so the
    R²/corr y-side denom stays conditioned on small centered close instead of raw close. OLS is
    translation-invariant in y, so this is VALUE-IDENTICAL (only the float conditioning changes); the null
    mask is preserved (centering a present y stays present, a null y stays null), so the paired count ``b``
    and every other path is unchanged. None → the raw ``y`` (byte-identical to today)."""
    y_centered = y_expr if y_anchor is None else (y_expr - y_anchor)
    both = x_expr.is_not_null() & y_centered.is_not_null()
    x_paired = pl.when(both).then(x_expr).otherwise(0.0)
    y_paired = pl.when(both).then(y_centered).otherwise(0.0)
    return [
        both.cast(pl.Float64).alias(f"__rd_{name}_b"),
        x_paired.alias(f"__rd_{name}_x"),
        y_paired.alias(f"__rd_{name}_y"),
        (x_paired * y_paired).alias(f"__rd_{name}_xy"),
        (x_paired * x_paired).alias(f"__rd_{name}_xx"),
        (y_paired * y_paired).alias(f"__rd_{name}_yy"),
    ]


def _pinned_time_x(latest_epoch_seconds: int) -> pl.Expr:
    """A time-axis x pinned to the SAME small origin the incremental engine uses at the anchor minute:
    ``(epoch − (latest − _TIME_ORIGIN_LAG·60))/60``, so the latest minute maps to ``_TIME_ORIGIN_LAG`` and
    every in-window x is O(1). VALUE-IDENTICAL to the group's whole-frame-relative ``regressions()`` time x
    (OLS is origin-invariant — any per-symbol-constant origin shift leaves slope/r2/corr/resid_std unchanged),
    it only keeps ``cov_n``/``denom_x`` from cancelling large near-equal sums. For the single-anchor batch
    paths (compute_latest / compute_reduction_batch), where one origin conditions the only emitted minute."""
    ref_epoch = latest_epoch_seconds - _TIME_ORIGIN_LAG * 60
    return (pl.col("minute").dt.epoch("s").cast(pl.Float64) - float(ref_epoch)) / 60.0


class ReductionGroup(FeatureGroup):
    """Base for a windowed-reduction feature group. Set ``reduce_input`` and implement ``reduced()`` /
    ``points()`` / ``assemble()``; ``compute()`` and ``compute_latest()`` are generated."""

    reduce_input: str = "minute_agg"

    # Whether this group may be served from the INCREMENTAL running sums (FP_INCREMENTAL) or must stay on the
    # batch fresh-sum recompute. Default True — for almost every group the incremental sums and the batch fresh
    # window sums agree to benign float drift. Set False on a group whose canonical algebra is a difference of
    # large near-equal sums on LARGE-MAGNITUDE values (variance/correlation of raw share volume), where the
    # running add/subtract rounds differently from the batch fresh sum and the cancellation amplifies it past
    # the parity-breach ratio at near-degenerate cells (a perfect-fit corr, an n=2 z-score). Such a group keeps
    # the batch fresh-sum path even under FP_INCREMENTAL until a stable-summation rewrite closes the corner;
    # the absolute divergence is ~1e-8 (float floor), so this is a parity-self-check guard, not a value bug.
    incremental_safe: bool = True

    def bind_live_engine(self, engine: object, seed_symbols: list[str] | None = None) -> None:
        """Bind the LIVE incremental engine (``CaptureState.engines[reduce_input]``) that carries this group's
        running sums, marking it PENDING-RESEED. Used by the hot-swap applier: when an engine carries a freshly-
        swapped group's input, the running state must be re-derived for the new compute logic — binding it makes
        ``up_to_date()`` report False so the single contract guard reseeds it (the old SWAP+RESEED kind, now
        expressed through the one ``up_to_date()`` / ``rebuild_from_history()`` surface, no kind classifier).
        """
        self.__dict__["_live_engine"] = engine
        self.__dict__["_live_engine_seed_symbols"] = seed_symbols
        self.__dict__["_live_engine_pending_reseed"] = True

    def up_to_date(self, buffer: pl.DataFrame | None) -> bool:
        """RunningState contract: True unless a live incremental engine was just bound for a swap and not yet
        reseeded. With FP_INCREMENTAL OFF (no bound engine) a reduction recomputes from the shared ring each
        minute → always up to date (the old DIRECT swap). A bound-but-unseeded engine → False → reseed."""
        return not self.__dict__.get("_live_engine_pending_reseed", False)

    def rebuild_from_history(self, buffer: pl.DataFrame | None) -> None:
        """RunningState contract: reseed the bound live engine's running sums from ``buffer`` (the SAME history
        the batch path recomputes over; ``seed(H);fold(m)==seed(H+m)`` by the engine's parity guarantee), then
        clear the pending flag so ``up_to_date()`` is True. No bound engine → no-op (stateless reduction)."""
        engine = self.__dict__.get("_live_engine")
        if engine is None:
            return None
        engine.seed(buffer, self.__dict__.get("_live_engine_seed_symbols"))
        self.__dict__["_live_engine_pending_reseed"] = False
        return None

    def centered_std(self) -> dict[str, str]:
        """{reduced_column_name: anchor_column} — the reduced columns whose std/variance is computed from a
        per-symbol-CENTERED power sum ``Σ(v−a)²−(Σ(v−a))²/n`` (a the anchor) instead of the raw
        ``Σv²−(Σv)²/n``, to keep the variance term conditioned on LARGE-magnitude values (raw share volume).
        Value-identical (variance is shift-invariant) but float-stable — closes the batch-vs-canonical std
        FORMULA gap that gated volume/price_volume (docs/CENTERED_STD_DESIGN.md, PROVEN machine-precision).
        The anchor column must be present on the input frame (attach_volume_anchor, read identically by both
        paths). Default empty — a column not listed keeps the raw power-sum std byte-for-byte. ADDITIVE: the
        raw ``Σv``/``Σv²`` are still summed (mean/ratio need the raw sum), so a group that does NOT center any
        column is byte-identical to before."""
        return {}

    def prepare(self, frame: pl.DataFrame) -> pl.DataFrame:
        """Optional per-minute preprocessing of the (symbol, minute)-sorted input frame BEFORE the reduced /
        regression exprs are evaluated — for a CROSS-SYMBOL column the per-symbol exprs need (e.g. broadcasting
        a market index's per-minute return onto every symbol's row). Applied identically in ``compute`` (rolling
        backfill), ``compute_latest`` (live), and the batched path, so both forms see the same column. The
        incremental engine sources such broadcast regressors from running state (``broadcast`` StatefulRegressor)
        and therefore does NOT call ``prepare``. Default: identity (no extra columns)."""
        return frame

    def reduced(self) -> dict[str, tuple[pl.Expr, tuple[str, ...], tuple[int, ...]]]:
        """{column_name: (expr_over_input, stats, windows)} — the value to reduce, which stats each needs
        ("mean"|"std"|"sum"), and the windows (in minutes) for that column (each column may differ). Default
        none — a regression-ONLY group (e.g. residual_analysis) declares just ``regressions()``."""
        return {}

    def points(self) -> dict[str, pl.Expr]:
        """{name: expr_over_input} — at-T scalar columns referenced via pt_() in assemble(). Default none."""
        return {}

    def regressions(self) -> dict[str, tuple[pl.Expr, pl.Expr, tuple[str, ...], tuple[int, ...]]]:
        """{name: (x_expr, y_expr, stats, windows)} — windowed OLS of y on x; stats ⊆ slope/corr/r2/mean_y,
        referenced via slope_/corr_/r2_/mean_y_ in assemble(). Default none. (For a TIME regressor, pass a
        small frame-relative x like ``(minute.epoch - minute.epoch.min())`` — OLS is origin-invariant.)"""
        return {}

    def stateful_regressors(self) -> dict[str, list[StatefulRegressor]]:
        """{regression_name: [StatefulRegressor, ...]} — declares which regressor slots the INCREMENTAL engine
        must source from running per-symbol state (a frame-relative time axis, or a cumulative like OBV)
        instead of slice-deriving. ONLY the incremental path reads this; backfill/live-batch evaluate the
        ``regressions()`` exprs directly and are unaffected. Default none (all regressors are short-lag)."""
        return {}

    def regression_y_anchor(self) -> dict[str, str]:
        """{regression_name: anchor_column} — under ``FP_RUST_REDUCE``, the regressions whose ``y`` is centered
        on a per-symbol-constant anchor column (``reduction_anchor.anchor_column(...)``, attached to the input
        frame, read identically by both paths) so the R²/corr/resid-std y-side denom stays conditioned on a
        large-magnitude regressand (raw close ~$45–$500). Value-identical (OLS is translation-invariant in y);
        only the float conditioning changes (the parity-critical invariant the close anchor guarantees by
        being a per-symbol constant read identically by batch + incremental). Default empty — a regression not
        listed keeps its raw ``y`` byte-for-byte. The flag default-OFF makes this a no-op regardless of what a
        group declares, so a group can opt in safely ahead of the prod relaunch flip."""
        return {}

    def _y_anchor_exprs(self) -> dict[str, pl.Expr]:
        """The per-regression y-centering anchor exprs ACTIVE for this group — empty unless ``FP_RUST_REDUCE``
        is on AND the regression is declared in ``regression_y_anchor``. Used by every batch/live path that
        builds the OLS paired columns so the centering is applied identically (and is a no-op when the flag is
        off, keeping the expression graph byte-identical to today)."""
        if not _USE_RUST_REDUCE:
            return {}
        return {name: pl.col(anchor) for name, anchor in self.regression_y_anchor().items()}

    def _time_regression_names(self) -> set[str]:
        """The regressions whose ``x`` slot is a ``kind="time"`` axis — the ones the ``FP_CENTERED_TIME`` batch
        conditioning applies to (it pins their x to a small per-window origin so the OLS denom/covariance stays
        well conditioned, matching the incremental engine). Read from ``stateful_regressors()`` so the set is
        data-driven and stays in lockstep with what the incremental engine sources from a rolled time origin.
        """
        return {
            name
            for name, regs in self.stateful_regressors().items()
            for reg in regs
            if reg.slot == "x" and reg.kind == "time"
        }

    @abstractmethod
    def assemble(self) -> dict[str, pl.Expr]:
        """{feature_name: expr} written with mean_/std_/sum_/pt_ — evaluated identically in both forms."""

    def _feature_names(self) -> list[str]:
        return list(self.assemble().keys())

    def _input_columns(self) -> list[str]:
        for spec in self.inputs:
            if spec.name == self.reduce_input:
                return list(spec.columns)
        raise KeyError(f"{self.name}: reduce_input '{self.reduce_input}' not in inputs")

    def reduce_buffer_minutes(self) -> int | None:
        """Derived from this group's DECLARED reduced/regression windows — the longest window is the
        deepest trailing context its latest-minute reduction reads. ``None`` only if it declares no
        windows (then the caller keeps the full buffer)."""
        windows: list[int] = []
        for _, _, group_windows in self.reduced().values():
            windows.extend(group_windows)
        for _, _, _, group_windows in self.regressions().values():
            windows.extend(group_windows)
        return max(windows) if windows else None

    def compute(self, ctx: BatchContext) -> pl.DataFrame:
        """Generated BACKFILL form: rolling_*_by over every minute (source of truth)."""
        frame = self.prepare(
            ctx.frame(self.reduce_input).select(self._input_columns()).sort(["symbol", "minute"])
        )
        reduced, regressions = self.reduced(), self.regressions()
        frame = frame.with_columns([expr.alias(f"__d_{name}") for name, (expr, _, _) in reduced.items()])
        if regressions:
            y_anchors = self._y_anchor_exprs()
            frame = frame.with_columns(
                [
                    col
                    for name, (x, y, _, _) in regressions.items()
                    for col in _ols_derived(name, x, y, y_anchors.get(name))
                ]
            )
        mats: list[pl.Expr] = []
        for name, (_, stats, windows) in reduced.items():
            source = pl.col(f"__d_{name}")
            for w in windows:
                size = f"{w}m"
                if "mean" in stats:
                    mats.append(
                        source.rolling_mean_by("minute", window_size=size)
                        .over("symbol")
                        .alias(f"__mean_{name}_{w}")
                    )
                if "std" in stats:
                    mats.append(
                        source.rolling_std_by("minute", window_size=size)
                        .over("symbol")
                        .alias(f"__std_{name}_{w}")
                    )
                if "sum" in stats:
                    mats.append(
                        source.rolling_sum_by("minute", window_size=size)
                        .over("symbol")
                        .alias(f"__sum_{name}_{w}")
                    )
        y_anchored = self._y_anchor_exprs()
        for name, (_, _, stats, windows) in regressions.items():
            for w in windows:
                size = f"{w}m"
                sums = {
                    key: pl.col(f"__rd_{name}_{key}")
                    .rolling_sum_by("minute", window_size=size)
                    .over("symbol")
                    for key in _OLS_KEYS
                }
                for stat, expr in _ols_stat_exprs(sums, stats, anchored=name in y_anchored).items():
                    mats.append(expr.alias(f"__{stat}_{name}_{w}"))
        frame = frame.with_columns([expr.alias(f"__pt_{name}") for name, expr in self.points().items()])
        frame = frame.with_columns(mats)
        feats = self.assemble()
        return frame.with_columns(
            [expr.cast(pl.Float64).alias(name) for name, expr in feats.items()]
        ).select(["symbol", "minute", *self._feature_names()])

    def compute_latest(self, ctx: BatchContext) -> pl.DataFrame:
        """Generated LIVE form: aggregate-at-T via the Rust reduction kernel, one row per symbol."""
        frame = self.prepare(
            ctx.frame(self.reduce_input).select(self._input_columns()).sort(["symbol", "minute"])
        )
        reduced, regressions = self.reduced(), self.regressions()
        frame = frame.with_columns([expr.alias(f"__d_{name}") for name, (expr, _, _) in reduced.items()])
        latest = frame["minute"].max()
        if regressions:
            time_regs = self._time_regression_names() if _USE_CENTERED_TIME else set()
            latest_epoch = int(latest.timestamp()) if time_regs else 0  # type: ignore[union-attr]
            y_anchors = self._y_anchor_exprs()
            ols_cols: list[pl.Expr] = []
            for name, (x_expr, y_expr, _, _) in regressions.items():
                x = _pinned_time_x(latest_epoch) if name in time_regs else x_expr
                ols_cols += _ols_derived(name, x, y_expr, y_anchors.get(name))
            frame = frame.with_columns(ols_cols)
        wide = resolve_points([self], frame, latest).select(
            ["symbol", *[f"__pt_{name}" for name in self.points()]]
        )
        for name, (_, stats, windows) in reduced.items():
            long = rust_reductions(frame, f"__d_{name}", windows)
            for stat in stats:
                wide = wide.join(
                    pivot_stat(long, stat, f"__{stat}_{name}_{{w}}", windows), on="symbol", how="left"
                )
        for name, (_, _, stats, windows) in regressions.items():
            value_cols = [f"__rd_{name}_{key}" for key in ("b", "x", "y", "xy", "xx", "yy")]
            long = rust_windowed_sums(frame, value_cols, windows)
            sums = {key: pl.col(f"__rd_{name}_{key}") for key in ("b", "x", "y", "xy", "xx", "yy")}
            glong = long.with_columns(
                [
                    expr.alias(f"__c_{stat}_{name}")
                    for stat, expr in _ols_stat_exprs(sums, stats, anchored=name in y_anchors).items()
                ]
            )
            for stat in stats:
                wide = wide.join(
                    pivot_stat(glong, f"__c_{stat}_{name}", f"__{stat}_{name}_{{w}}", windows),
                    on="symbol",
                    how="left",
                )
        feats = self.assemble()
        return (
            wide.with_columns([expr.cast(pl.Float64).alias(name) for name, expr in feats.items()])
            .with_columns(pl.lit(latest).alias("minute"))
            .select(["symbol", "minute", *self._feature_names()])
        )


def _canonical(
    name: str, stats: tuple[str, ...], base: str, *, centered_base: bool = False
) -> list[pl.Expr]:
    """Per-window canonical stat columns for one reduced column, derived from its batched windowed sums in
    the long frame: mean = sum/count, std(ddof=1) = sqrt((sumsq - sum^2/count)/(count-1)). ``base`` is the
    sum-of-value column, ``base__p`` the sum-of-presence (non-null count), ``base__sq`` the sum-of-squares.

    ``centered_base``: compute the std/variance from the PER-SYMBOL-CENTERED power sums ``{base}__c`` =
    Σ(v−a) and ``{base}__csq`` = Σ(v−a)² (a = the anchor) — value-identical (variance is shift-invariant) but
    float-stable on large-magnitude values. mean/sum still use the RAW ``base`` (so mean/ratio are unchanged).
    """
    out = []
    if "sum" in stats:
        out.append(pl.col(base).alias(f"__c_sum_{name}"))
    if "mean" in stats:
        count = pl.col(f"{base}__p")
        # guard count==0 (an all-null window) -> null, matching rolling_mean / rust_reductions (not NaN)
        out.append(pl.when(count > 0).then(pl.col(base) / count).otherwise(None).alias(f"__c_mean_{name}"))
    if "std" in stats:
        count = pl.col(f"{base}__p")
        if centered_base:
            # centered variance = (Σ(v−a)² − (Σ(v−a))²/n)/(n−1) — shift-invariant == the raw variance, but the
            # squared terms stay small so the Σx²−(Σx)²/n cancellation is conditioned (closes the volume gap).
            csum, csumsq = pl.col(f"{base}__c"), pl.col(f"{base}__csq")
            var = (csumsq - csum * csum / count) / (count - 1)
        else:
            total, sumsq = pl.col(base), pl.col(f"{base}__sq")
            var = (sumsq - total * total / count) / (count - 1)
        out.append(pl.when(count > 1).then(var.sqrt()).otherwise(None).alias(f"__c_std_{name}"))
    return out


_PlanEntry = tuple[int, str, tuple[str, ...], tuple[int, ...], str]


def _anchored_namespaces(groups: list[ReductionGroup]) -> set[str]:
    """The ``<gi>_<reg>`` namespaces whose regression ``y`` is centered under ``FP_RUST_REDUCE`` (empty when
    the flag is off — ``_y_anchor_exprs`` already returns {} then). The OLS stat uses the centered-variance
    guard scale (``eps·b·syy`` not ``eps·sy²``) for these, since centering shrinks ``sy``. Derived the SAME
    way every batch/live path namespaces the paired columns, so the guard choice is consistent across paths.
    """
    anchored: set[str] = set()
    for gi, group in enumerate(groups):
        for reg_name in group._y_anchor_exprs():
            anchored.add(f"{gi}_{reg_name}")
    return anchored


def build_plan(
    groups: list[ReductionGroup],
    *,
    time_origin_epoch: int | None = None,
) -> tuple[
    list[pl.Expr],
    list[pl.Expr],
    list[str],
    list[_PlanEntry],
    list[_PlanEntry],
    tuple[int, ...],
    dict[str, str],
]:
    """The union value-column plan for a set of declarative groups — SHARED by the batch and the incremental
    engine so both sum EXACTLY the same columns. Returns:
      derived  — exprs for the base reduced cols + the six OLS paired cols (namespaced per group),
      extra    — exprs for the presence/square cols that mean/std need,
      value_cols — the ordered names to sum (base, base__p, base__sq, OLS b/x/y/xy/xx/yy),
      plan/reg_plan — per-group (gi, name, stats, windows, base|ns) for assemble_from_long,
      windows  — the sorted union of all windows.

    ``time_origin_epoch`` (set only by the single-anchor batch path under FP_CENTERED_TIME) pins a
    ``kind="time"`` regression's x to ``(epoch − (origin − _TIME_ORIGIN_LAG·60))/60`` so the batch OLS operand
    sums stay small and well-conditioned (value-identical, origin-invariant). The incremental engine passes
    None — it sources the time x from its own rolled origin in ``_build_paired`` and overrides this column.
    """
    derived: list[pl.Expr] = []
    plan: list[_PlanEntry] = []
    reg_plan: list[_PlanEntry] = []
    all_windows: set[int] = set()
    for gi, group in enumerate(groups):
        time_regs = group._time_regression_names() if time_origin_epoch is not None else set()
        y_anchors = group._y_anchor_exprs()
        for name, (expr, stats, windows) in group.reduced().items():
            base = f"__b{gi}_{name}"
            derived.append(expr.alias(base))
            plan.append((gi, name, stats, tuple(windows), base))
            all_windows |= set(windows)
        for name, (x_expr, y_expr, stats, windows) in group.regressions().items():
            ns = f"{gi}_{name}"  # namespace the regression's six paired columns per group
            # time_regs is empty unless time_origin_epoch is set, so this only pins under FP_CENTERED_TIME
            x = _pinned_time_x(time_origin_epoch) if name in time_regs else x_expr  # type: ignore[arg-type]
            derived += _ols_derived(ns, x, y_expr, y_anchors.get(name))
            reg_plan.append((gi, name, stats, tuple(windows), ns))
            all_windows |= set(windows)
    # base -> anchor column, for the reduced columns a group opts into centered std (additive; raw stays).
    centered: dict[str, str] = {}
    for gi, group in enumerate(groups):
        for name, anchor in group.centered_std().items():
            centered[f"__b{gi}_{name}"] = anchor

    extra: list[pl.Expr] = []
    value_cols: list[str] = []
    for _, _, stats, _, base in plan:
        value_cols.append(base)
        if "mean" in stats or "std" in stats:
            extra.append(pl.col(base).is_not_null().cast(pl.Float64).alias(f"{base}__p"))
            value_cols.append(f"{base}__p")
        if "std" in stats:
            # RAW Σv² (kept byte-for-byte: mean/ratio and any non-centered std read it unchanged)
            extra.append((pl.col(base) * pl.col(base)).alias(f"{base}__sq"))
            value_cols.append(f"{base}__sq")
            if base in centered:
                # ADDITIVE centered power sums Σ(v−a) and Σ(v−a)² (a = the per-symbol anchor column). The
                # centered variance = (Σ(v−a)² − (Σ(v−a))²/n)/(n−1) is shift-invariant == the raw variance, but
                # the squared terms stay small so the cancellation is conditioned. The raw column null-mask is
                # preserved (so presence/count is identical) by centering the SAME expr: null stays null.
                vc = pl.col(base) - pl.col(centered[base])
                extra.append(vc.alias(f"{base}__c"))
                extra.append((vc * vc).alias(f"{base}__csq"))
                value_cols += [f"{base}__c", f"{base}__csq"]
    for _, _, _, _, ns in reg_plan:
        value_cols += [f"__rd_{ns}_{key}" for key in ("b", "x", "y", "xy", "xx", "yy")]
    return derived, extra, value_cols, plan, reg_plan, tuple(sorted(all_windows)), centered


def compute_reduction_batch(groups: list[ReductionGroup], ctx: BatchContext) -> dict[str, pl.DataFrame]:
    """Compute MANY declarative reduction groups in ONE shared marshal + kernel pass.

    Groups sharing a ``reduce_input`` have their derived columns concatenated into one frame; a SINGLE
    ``rust_windowed_sums`` (over every derived column + its square + a presence flag, across the union of
    all windows) replaces one kernel call per group — so the buffer is symbol-coded, sorted, and copied to
    numpy ONCE instead of N times (that per-group marshaling is the live-path floor). Each group then
    assembles from its own slice of the shared sums. Returns {group_name: feature_frame}; each is identical
    within tolerance to that group's own ``compute_latest`` (the generic parity test still guards each)."""
    groups = [g for g in groups if isinstance(g, ReductionGroup)]
    if not groups:
        return {}
    reduce_input = groups[0].reduce_input
    input_cols: list[str] = []
    for group in groups:
        for col in group._input_columns():
            if col not in input_cols:
                input_cols.append(col)
    with _phase.phase("batch.sort"):
        frame = ctx.frame(reduce_input).select(input_cols).sort(["symbol", "minute"])
    for group in groups:
        frame = group.prepare(frame)
    latest = frame["minute"].max()

    # FP_CENTERED_TIME: pin kind="time" regressions' x to the incremental engine's small anchor origin so the
    # batch OLS operand sums stay conditioned (value-identical). This is a single-anchor path (one emitted
    # minute per symbol), so one origin suffices — matching the incremental axis at the latest minute exactly.
    time_origin = int(latest.timestamp()) if _USE_CENTERED_TIME else None  # type: ignore[union-attr]
    derived, extra, value_cols, plan, reg_plan, windows, centered = build_plan(
        groups, time_origin_epoch=time_origin
    )
    with _phase.phase("batch.derive(value cols)"):
        frame = frame.with_columns(derived)
    with _phase.phase("batch.derive(sq+presence)"):
        frame = frame.with_columns(extra)
    long = rust_windowed_sums(frame, value_cols, windows)

    with _phase.phase("batch.assemble(pivot+join)"):
        return assemble_from_long(
            groups, long, resolve_points(groups, frame, latest), latest, plan, reg_plan, centered
        )


def resolve_points(groups: list[ReductionGroup], frame: pl.DataFrame, latest: object) -> pl.DataFrame:
    """Evaluate every group's ``points()`` exprs over the FULL trailing buffer (so positive-lag exprs such as
    ``close.shift(w).over("symbol")`` resolve against history — exactly as backfill ``compute()`` does), then
    return the single latest-minute row per symbol carrying the materialised ``__pt_<name>`` columns.

    PARITY FIX (was a live-vs-backfill break): every assemble path previously re-evaluated the point exprs on
    a SINGLE-minute frame (``frame.filter(minute == latest)``), where ``shift(w>0).over("symbol")`` is null —
    so the lag-point feature families (efficiency, return_dynamics, momentum_consistency, ...) emitted 100%
    NaN live while backfill computed them fine. Resolving over the whole buffer is gap-safe (a sparse
    symbol's prior bar is found however far back it is) and matches the backfill truth. Point names that
    collide across groups carry the SAME expr on the SAME input column (dedup by output name is byte-correct
    — the invariant ``emit_rust_unified`` already relies on). Assemble paths now SELECT the precomputed
    ``__pt_<name>`` columns by name rather than re-evaluating the exprs on the latest minute."""
    point_exprs: dict[str, pl.Expr] = {}
    for group in groups:
        for name, expr in group.points().items():
            point_exprs.setdefault(f"__pt_{name}", expr.alias(f"__pt_{name}"))
    return (
        frame.sort(["symbol", "minute"])
        .select(["symbol", "minute", *point_exprs.values()])
        .filter(pl.col("minute") == latest)
    )


def assemble_from_long(
    groups: list[ReductionGroup],
    long: pl.DataFrame,
    latest_frame: pl.DataFrame,
    latest: object,
    plan: list[tuple[int, str, tuple[str, ...], tuple[int, ...], str]],
    reg_plan: list[tuple[int, str, tuple[str, ...], tuple[int, ...], str]],
    centered: dict[str, str] | None = None,
) -> dict[str, pl.DataFrame]:
    """Build each group's feature frame from a LONG (symbol, window, <value-col sums>) frame + the latest
    minute's rows carrying the precomputed ``__pt_<name>`` point columns (from ``resolve_points``, which
    resolves positive-lag points over the whole buffer — see that function). SHARED by the live-batch path
    (``long`` from the Rust kernel) and the incremental path (``long`` from the running-sum state) — so the
    canonical algebra and ``assemble()`` are the SAME code in both; only the source of the sums differs.
    ``latest`` is the minute stamped on output."""
    centered = centered or {}
    anchored_ns = _anchored_namespaces(groups)
    results: dict[str, pl.DataFrame] = {}
    for gi, group in enumerate(groups):
        canon: list[pl.Expr] = []
        for pgi, name, stats, _, base in plan:
            if pgi == gi:
                canon += _canonical(name, stats, base, centered_base=base in centered)
        for pgi, name, stats, _, ns in reg_plan:
            if pgi == gi:
                sums = {key: pl.col(f"__rd_{ns}_{key}") for key in ("b", "x", "y", "xy", "xx", "yy")}
                canon += [
                    expr.alias(f"__c_{stat}_{name}")
                    for stat, expr in _ols_stat_exprs(sums, stats, anchored=ns in anchored_ns).items()
                ]
        # ONE pivot for ALL of this group's canonical columns (vs one pivot+join per stat) — the pivot
        # names columns `<value>_<window>`, so `__c_<stat>_<name>` over window w -> `__c_<stat>_<name>_<w>`,
        # which we rename to the `__<stat>_<name>_<w>` the accessors expect. Extra union-windows are dropped
        # by the final feature select.
        # `__c_z` keeps ≥2 value columns so polars always names pivoted columns `<value>_<window>` (with a
        # single value it would drop the value name to just `<window>`).
        glong = long.select(["symbol", "window", *canon, pl.lit(0.0).alias("__c_z")])
        piv = glong.pivot(on="window", index="symbol")
        piv = piv.rename(
            {c: "__" + c[4:] for c in piv.columns if c.startswith("__c_") and not c.startswith("__c_z")}
        )
        wide = latest_frame.select(["symbol", *[f"__pt_{name}" for name in group.points()]]).join(
            piv, on="symbol", how="left"
        )
        feats = group.assemble()
        results[group.name] = (
            wide.with_columns([expr.cast(pl.Float64).alias(name) for name, expr in feats.items()])
            .with_columns(pl.lit(latest).alias("minute"))
            .select(["symbol", "minute", *group._feature_names()])
        )
    return results


@dataclass(frozen=True)
class _AssemblePlan:
    """The flattened, group-INDEPENDENT plan the Rust ``assemble_canonical`` kernel consumes, built ONCE per
    engine (it is pure metadata over plan/reg_plan/col_index/windows). One row per OUTPUT canonical column:
    the window index into ``running``, the statistic kind byte, and up to six value-col indices. ``col_names``
    is the per-column accessor name (``__<stat>_<name>_<w>``) and ``group_slices`` maps each group index to its
    contiguous half-open column range in the kernel output, so a group's wide columns are sliced with no copy.
    """

    win: list[int]
    kind: list[int]
    idx: tuple[list[int], list[int], list[int], list[int], list[int], list[int]]
    col_names: list[str]
    group_slices: dict[int, tuple[int, int]]


def build_assemble_plan(
    groups: list[ReductionGroup],
    windows: tuple[int, ...],
    col_index: dict[str, int],
    plan: list[tuple[int, str, tuple[str, ...], tuple[int, ...], str]],
    reg_plan: list[tuple[int, str, tuple[str, ...], tuple[int, ...], str]],
    centered: dict[str, str] | None = None,
) -> _AssemblePlan:
    """Flatten plan/reg_plan into the per-output-column spec the Rust kernel needs, in the SAME column order
    (per group: reduced columns then regressions, each window then stat) ``emit_numpy`` builds ``wide_cols``.
    Pure metadata; built once and reused every minute. ``centered`` is the ``build_plan`` centered-std map
    (base -> anchor col): a base in it has its std read from the centered power sums ``{base}__c``/``{base}__csq``
    (idx0/idx2) so the Rust ``assemble_canonical`` std matches the centered ``assemble_from_long`` live truth —
    without this the Rust/numpy emit re-introduce the cancellation centering exists to fix (e.g. volume)."""
    centered = centered or {}
    anchored_ns = _anchored_namespaces(groups)
    win_index = {int(w): wi for wi, w in enumerate(windows)}
    win: list[int] = []
    kind: list[int] = []
    idx_lists: tuple[list[int], ...] = ([], [], [], [], [], [])
    col_names: list[str] = []
    group_slices: dict[int, tuple[int, int]] = {}

    def push(
        window: int, stat: str, indices: list[int], col_name: str, *, kind_code: int | None = None
    ) -> None:
        win.append(win_index[int(window)])
        kind.append(_STAT_CODE[stat] if kind_code is None else kind_code)
        padded = (indices + [0, 0, 0, 0, 0, 0])[:6]
        for axis in range(6):
            idx_lists[axis].append(padded[axis])
        col_names.append(col_name)

    for gi, _group in enumerate(groups):
        start = len(col_names)
        for pgi, name, stats, group_windows, base in plan:
            if pgi != gi:
                continue
            for window in group_windows:
                for stat in stats:
                    if stat == "sum":
                        indices = [col_index[base]]
                    elif stat == "mean":
                        indices = [col_index[base], col_index[f"{base}__p"]]
                    elif base in centered:  # centered std: read Σ(v−a)/Σ(v−a)² (idx0/idx2), count unchanged
                        indices = [
                            col_index[f"{base}__c"],
                            col_index[f"{base}__p"],
                            col_index[f"{base}__csq"],
                        ]
                    else:  # raw std
                        indices = [col_index[base], col_index[f"{base}__p"], col_index[f"{base}__sq"]]
                    push(window, stat, indices, f"__{stat}_{name}_{window}")
        for pgi, name, stats, group_windows, ns in reg_plan:
            if pgi != gi:
                continue
            ols_indices = [col_index[f"__rd_{ns}_{key}"] for key in ("b", "x", "y", "xy", "xx", "yy")]
            anchored = ns in anchored_ns
            for window in group_windows:
                for stat in stats:
                    # An anchored (y-centered) regression's corr/r2 use the centered-variance denom_y guard —
                    # the Rust kind 8/9 twins (slope/mean_y/resid_std are unaffected by y-centering).
                    kind_code = (
                        _STAT_CODE_ANCHORED[stat] if anchored and stat in _STAT_CODE_ANCHORED else None
                    )
                    push(window, stat, ols_indices, f"__{stat}_{name}_{window}", kind_code=kind_code)
        group_slices[gi] = (start, len(col_names))
    return _AssemblePlan(win, kind, idx_lists, col_names, group_slices)


def emit_rust(
    groups: list[ReductionGroup],
    running: np.ndarray,
    symbols: list[str],
    asm_plan: _AssemblePlan,
    latest_frame: pl.DataFrame,
    latest: object,
) -> dict[str, pl.DataFrame]:
    """RUST-ASSEMBLE alternative to ``emit_numpy``: compute EVERY group's per-window canonical columns in ONE
    ``quant_tick.assemble_canonical`` pass over the whole ``(n_windows, n_symbols, n_value_cols)`` running-sum
    array (the same canonical/OLS algebra as ``_canonical_numpy``/``_ols_stat_numpy``, NaN==null by
    construction), then slice each group's columns out of the contiguous result. Replaces ONLY how the
    ``__<stat>_<name>_<w>`` columns are PRODUCED — each group's ``assemble()`` (the feature formulas) is
    unchanged. ``asm_plan`` is ``build_assemble_plan(...)`` (pure metadata, built once)."""
    canon = quant_tick.assemble_canonical(
        np.ascontiguousarray(running), asm_plan.win, asm_plan.kind, *asm_plan.idx
    )  # (n_symbols, n_out), NaN where null
    symbol_series = pl.Series("symbol", symbols)
    results: dict[str, pl.DataFrame] = {}
    for gi, group in enumerate(groups):
        start, stop = asm_plan.group_slices[gi]
        # Ingest the group's contiguous canonical block in ONE polars allocation (vs a per-column pl.Series
        # copy): pl.from_numpy over the C-contiguous (n_symbols, n_group_cols) slice. NaN stays NaN (not null),
        # exactly as the numpy emit's per-column Series — so assemble()'s null/NaN handling is unchanged.
        points_select = latest_frame.select(["symbol", *[f"__pt_{name}" for name in group.points()]])
        if stop > start:
            block = np.ascontiguousarray(canon[:, start:stop])
            piv = pl.from_numpy(block, schema=asm_plan.col_names[start:stop]).with_columns(symbol_series)
            wide = points_select.join(piv, on="symbol", how="left")
        else:
            wide = points_select  # points-only group: no canonical columns to ingest
        feats = group.assemble()
        results[group.name] = (
            wide.with_columns([expr.cast(pl.Float64).alias(name) for name, expr in feats.items()])
            .with_columns(pl.lit(latest).alias("minute"))
            .select(["symbol", "minute", *group._feature_names()])
        )
    return results


def emit_rust_unified(
    groups: list[ReductionGroup],
    running: np.ndarray,
    symbols: list[str],
    asm_plan: _AssemblePlan,
    latest_frame: pl.DataFrame,
    latest: object,
) -> dict[str, pl.DataFrame]:
    """UNIFIED single-pass twin of ``emit_rust`` (a SCHEDULING change, not a math change).

    ``emit_rust`` runs the ONE ``assemble_canonical`` Rust kernel, then for EACH of the ~13 reduction
    groups builds its own polars frame (ingest its canonical slice + a per-group points select + a join)
    and evaluates that group's ``assemble()`` exprs in its OWN ``with_columns`` — the per-group polars
    frame-build + expr-eval is the reduction-emit floor (the canonical algebra is ~1-3ms). This builds ONE
    wide frame keyed (symbol) carrying EVERY group's canonical columns (the kernel's contiguous
    ``(n_symbols, n_out)`` block ingested in ONE ``pl.from_numpy``, deduped by canonical name — two groups
    declaring the same reduction key emit the same ``__<stat>_<name>_<w>`` column, an IDENTICAL value by
    construction, so keeping the first is byte-correct) plus the UNION of every group's ``__pt_<name>`` point
    columns (deduped by output name; colliding point names across groups carry the SAME expr on the SAME
    input column, so one shared column is byte-correct), then evaluates ALL groups' ``assemble()`` exprs in
    ONE ``with_columns`` pass, and slices each group's feature columns back out.

    Byte-identical to per-group ``emit_rust`` by construction: the SAME kernel output, the SAME point exprs,
    feeding the SAME ``assemble()`` expressions — only the polars pass/join count changes (1 ingest + 1
    points-select + 1 join + 1 with_columns, vs N of each). Feature names are unique across groups, so the
    per-group slice is exact. Returns the SAME ``{group_name: feature_frame}`` shape ``emit_rust`` returns.
    """
    canon = quant_tick.assemble_canonical(
        np.ascontiguousarray(running), asm_plan.win, asm_plan.kind, *asm_plan.idx
    )  # (n_symbols, n_out), NaN where null
    symbol_series = pl.Series("symbol", symbols)

    # Ingest EVERY group's canonical columns in ONE pl.from_numpy over the kernel block, deduped by canonical
    # name. Two groups can declare the SAME reduction key (e.g. range_expansion + realized_range both reduce a
    # `rng` mean over the same windows) -> ``build_assemble_plan`` emits the same ``__mean_rng_<w>`` name twice.
    # Per-group ``emit_rust`` slices ``col_names[start:stop]`` so duplicates land in DIFFERENT frames; this
    # single shared frame would collide (DuplicateError). A duplicate name carries an IDENTICAL value column by
    # construction — same stat over the same per-bar reduce expr and windows -> the kernel computes the same
    # value from each copy's indices — so keeping the FIRST occurrence's column is byte-correct (the same
    # dedup-by-name invariant the point-column union below already relies on). Every group's ``assemble()`` reads
    # the canonical column BY NAME (``__<stat>_<name>_<w>``), so one shared column feeds both groups correctly.
    seen: set[str] = set()
    keep_indices: list[int] = []
    keep_names: list[str] = []
    for col_idx, col_name in enumerate(asm_plan.col_names):
        if col_name not in seen:
            seen.add(col_name)
            keep_indices.append(col_idx)
            keep_names.append(col_name)
    if keep_indices:
        block = np.ascontiguousarray(canon[:, keep_indices])
        wide = pl.from_numpy(block, schema=keep_names).with_columns(symbol_series)
    else:
        wide = pl.DataFrame({"symbol": symbols})

    # The UNION of every group's at-T point columns, deduped by output name (colliding names are identical
    # exprs), evaluated on the latest minute's frame ONCE and joined onto the wide canonical frame.
    point_cols: list[str] = []
    for group in groups:
        for name in group.points():
            col = f"__pt_{name}"
            if col not in point_cols:
                point_cols.append(col)
    if point_cols:
        points = latest_frame.select(["symbol", *point_cols])
        wide = wide.join(points, on="symbol", how="left")

    # Evaluate ALL groups' assemble() exprs in ONE with_columns pass (feature names unique across groups).
    all_feature_exprs: list[pl.Expr] = []
    for group in groups:
        for name, expr in group.assemble().items():
            all_feature_exprs.append(expr.cast(pl.Float64).alias(name))
    wide = wide.with_columns(all_feature_exprs).with_columns(pl.lit(latest).alias("minute"))

    results: dict[str, pl.DataFrame] = {}
    for group in groups:
        results[group.name] = wide.select(["symbol", "minute", *group._feature_names()])
    return results


def _canonical_numpy(
    sums: np.ndarray,
    stats: tuple[str, ...],
    col_index: dict[str, int],
    base: str,
    *,
    centered_base: bool = False,
) -> dict[str, np.ndarray]:
    """Numpy twin of ``_canonical`` for ONE reduced column over ONE window. ``sums`` is the ``(n_symbols,
    n_value_cols)`` running-sum row for the window. Reproduces the IDENTICAL algebra cell-for-cell, with
    ``np.nan`` standing in for polars ``null`` (the same guard conditions): mean = sum/count guarded count>0,
    std(ddof=1) = sqrt((sumsq - sum^2/count)/(count-1)) guarded count>1. Returns {canonical_col_name: column}
    keyed ``__c_<stat>_<name>`` to mirror the polars path's intermediate names.

    ``centered_base`` (mirrors ``_canonical``): compute std/variance from the PER-SYMBOL-CENTERED power sums
    ``{base}__c`` = Σ(v−a) and ``{base}__csq`` = Σ(v−a)² (a = the anchor) — value-identical (shift-invariant)
    but float-stable on large-magnitude values. mean/sum still read the RAW ``base``. Required so this numpy
    emit (and the Rust ``assemble_canonical`` twin) match the polars ``assemble_from_long`` live truth for a
    centered group (e.g. volume); without it they re-introduce the very cancellation centering exists to fix.
    """
    total = sums[:, col_index[base]]
    out: dict[str, np.ndarray] = {}
    name = base  # key the returned dict by ``base`` (the caller looks up ``__c_<stat>_<base>`` directly)
    if "sum" in stats:
        out[f"__c_sum_{name}"] = total
    if "mean" in stats:
        count = sums[:, col_index[f"{base}__p"]]
        mean = np.where(
            count > 0, np.divide(total, count, out=np.zeros_like(total), where=count > 0), np.nan
        )
        out[f"__c_mean_{name}"] = mean
    if "std" in stats:
        count = sums[:, col_index[f"{base}__p"]]
        if centered_base:
            # centered variance = (Σ(v−a)² − (Σ(v−a))²/n)/(n−1) — shift-invariant == the raw var, conditioned.
            std_total = sums[:, col_index[f"{base}__c"]]
            sumsq = sums[:, col_index[f"{base}__csq"]]
        else:
            std_total = total
            sumsq = sums[:, col_index[f"{base}__sq"]]
        safe = count > 1
        # ((sumsq - total^2/count) / (count - 1)).sqrt() — guarded count>1 (else null), matching _canonical.
        # Sentinel count=2 on unsafe rows (count<=1) so the intermediate never divides by zero; those rows are
        # then masked to NaN. On SAFE rows the algebra is bit-identical to the polars _canonical expression.
        cnt_safe = np.where(safe, count, 2.0)
        var_calc = (sumsq - std_total * std_total / cnt_safe) / (cnt_safe - 1.0)
        out[f"__c_std_{name}"] = np.where(safe, np.sqrt(var_calc), np.nan)
    return out


def _ols_stat_numpy(
    sums: np.ndarray, stats: tuple[str, ...], col_index: dict[str, int], ns: str, *, anchored: bool = False
) -> dict[str, np.ndarray]:
    """Numpy twin of ``_ols_stat_exprs`` for ONE regression over ONE window — IDENTICAL algebra to
    ``_ols_derived``/``ols.py``, with ``np.nan`` for polars ``null`` and the SAME defined guards (b>=2 &
    denom_x>relative-floor for slope; additionally denom_y>relative-floor for corr/r2). The six paired sums
    are columns ``__rd_<ns>_{b,x,y,xy,xx,yy}`` in the running-sum row. ``anchored`` (FP_RUST_REDUCE y-centered
    regression): use the centered-variance guard scale ``eps·b·syy`` not ``eps·sy²`` — see ``_ols_stat_exprs``.
    """
    base_sums = {key: sums[:, col_index[f"__rd_{ns}_{key}"]] for key in ("b", "x", "y", "xy", "xx", "yy")}
    b, sx, sy, sxy, sxx, syy = (base_sums[key] for key in ("b", "x", "y", "xy", "xx", "yy"))
    denom_x = b * sxx - sx * sx
    denom_y = b * syy - sy * sy
    cov_n = b * sxy - sx * sy
    denom_y_scale = (b * syy) if anchored else (sy * sy)
    denom_y_eps = _OLS_DENOM_Y_CENTERED_REL_EPS if anchored else _OLS_DENOM_Y_REL_EPS
    defined = (b >= 2.0) & (denom_x > _OLS_DENOM_X_REL_EPS * (sx * sx))
    defined_corr = defined & (denom_y > denom_y_eps * denom_y_scale)
    perfect = defined_corr & (b == _OLS_PERFECT_FIT_COUNT)  # line through 2 points: r2==1, corr==sign(cov)
    out: dict[str, np.ndarray] = {}
    if "slope" in stats:
        slope = np.where(defined, np.divide(cov_n, denom_x, out=np.zeros_like(cov_n), where=defined), np.nan)
        out["slope"] = slope
    if "corr" in stats:
        denom = np.sqrt(denom_x * denom_y)
        corr = np.where(
            defined_corr, np.divide(cov_n, denom, out=np.zeros_like(cov_n), where=defined_corr), np.nan
        )
        out["corr"] = np.where(perfect, np.sign(cov_n), corr)
    if "r2" in stats:
        prod = denom_x * denom_y
        r2 = np.where(
            defined_corr,
            np.divide(cov_n * cov_n, prod, out=np.zeros_like(cov_n), where=defined_corr),
            np.nan,
        )
        out["r2"] = np.where(perfect, 1.0, r2)
    if "mean_y" in stats:
        out["mean_y"] = np.where(b > 0, np.divide(sy, b, out=np.zeros_like(sy), where=b > 0), np.nan)
    if "resid_std" in stats:
        # numpy twin of _ols_stat_exprs' resid_std — the exact centered-sum residual-std algebra; np.nan==null.
        safe_b = np.where(b > 0, b, 1.0)
        sxx_c = sxx - sx * sx / safe_b
        sxy_c = sxy - sx * sy / safe_b
        syy_c = syy - sy * sy / safe_b
        nonflat = sxx_c > 0.0
        slope_r = np.divide(sxy_c, sxx_c, out=np.zeros_like(sxy_c), where=nonflat)
        ssr = np.clip(syy_c - slope_r * sxy_c, 0.0, None)
        mean_y = np.divide(sy, safe_b, out=np.zeros_like(sy), where=b > 0)
        resid_var = ssr / safe_b
        resid_floor = (_RESID_REL_FLOOR * mean_y) ** 2
        resid_defined = (b >= _RESID_MIN_POINTS) & nonflat & (resid_var > resid_floor)
        std_pct = (
            np.divide(np.sqrt(resid_var), mean_y, out=np.zeros_like(resid_var), where=resid_defined) * 100.0
        )
        out["resid_std"] = np.where(resid_defined, std_pct, np.nan)
    return out


def emit_numpy(
    groups: list[ReductionGroup],
    running: np.ndarray,
    symbols: list[str],
    windows: tuple[int, ...],
    col_index: dict[str, int],
    latest_frame: pl.DataFrame,
    latest: object,
    plan: list[tuple[int, str, tuple[str, ...], tuple[int, ...], str]],
    reg_plan: list[tuple[int, str, tuple[str, ...], tuple[int, ...], str]],
    centered: dict[str, str] | None = None,
) -> dict[str, pl.DataFrame]:
    """NUMPY-NATIVE alternative to ``assemble_from_long`` — builds each group's per-window canonical columns
    (``__<stat>_<name>_<w>``) DIRECTLY from the ``(n_windows, n_symbols, n_value_cols)`` running-sum array,
    BYPASSING the polars pivot. The canonical/OLS algebra is the numpy twin of ``_canonical``/``_ols_stat_exprs``
    (parity-true by construction; null↔NaN), and a column is only emitted for the windows a group actually
    declares (so the wide frame already has the accessor-expected columns, no pivot/rename). ``running`` is
    ``WindowedSumState.running``; ``col_index`` maps value-col name -> column index. ``centered`` is the
    ``build_plan`` centered-std map (base -> anchor col); a base in it computes std from the centered power
    sums, matching ``assemble_from_long``. Returns the SAME {group_name: feature_frame} shape."""
    centered = centered or {}
    anchored_ns = _anchored_namespaces(groups)
    win_index = {int(w): wi for wi, w in enumerate(windows)}
    results: dict[str, pl.DataFrame] = {}
    for gi, group in enumerate(groups):
        wide_cols: dict[str, pl.Series] = {}
        for pgi, name, stats, group_windows, base in plan:
            if pgi != gi:
                continue
            for w in group_windows:
                row_sums = running[win_index[int(w)]]
                canon = _canonical_numpy(row_sums, stats, col_index, base, centered_base=base in centered)
                for stat in stats:
                    column = canon[f"__c_{stat}_{base}"]
                    wide_cols[f"__{stat}_{name}_{w}"] = pl.Series(column, dtype=pl.Float64)
        for pgi, name, stats, group_windows, ns in reg_plan:
            if pgi != gi:
                continue
            for w in group_windows:
                row_sums = running[win_index[int(w)]]
                ols = _ols_stat_numpy(row_sums, stats, col_index, ns, anchored=ns in anchored_ns)
                for stat in stats:
                    wide_cols[f"__{stat}_{name}_{w}"] = pl.Series(ols[stat], dtype=pl.Float64)
        piv = pl.DataFrame({"symbol": symbols, **wide_cols})
        wide = latest_frame.select(["symbol", *[f"__pt_{name}" for name in group.points()]]).join(
            piv, on="symbol", how="left"
        )
        feats = group.assemble()
        results[group.name] = (
            wide.with_columns([expr.cast(pl.Float64).alias(name) for name, expr in feats.items()])
            .with_columns(pl.lit(latest).alias("minute"))
            .select(["symbol", "minute", *group._feature_names()])
        )
    return results
