"""Momentum run/shape features (family: TREND_QUALITY, Layer A).

Two momentum-quality shapes beyond the second-moment residual spread:

  * ``residual_skew_{W}m`` — skewness of the OLS residuals around the close-vs-time trend. The third moment is
    computed WINDOW-LOCALLY: each window's trailing closes are gathered (``rolling`` agg), exploded, and the OLS
    residuals' standardized third moment ``m3/m2**1.5`` is taken over a window-RELATIVE time axis (minutes since
    that window's own earliest bar). This is POINT-IN-TIME by construction — the value at minute T depends only
    on the bars in (T−W, T], so appending future bars cannot change it (guarded by tests/test_fp_lookahead.py).
    The earlier formulation rode rolling THIRD-order power sums over a buffer-relative (per-symbol latest-minute)
    time axis; centered third moments are origin-invariant in exact arithmetic, but the origin SHIFTED as the
    buffer grew, so the catastrophic-cancellation power-sum differences (sxxx_c = sxxx − 3·mx·sxx + …) re-rounded
    past values when future minutes were appended — a look-ahead leak (~1e-8 drift). The window-local fit has no
    cross-window origin and no cancellation (the axis is 0..span minutes within each window), so it is both
    look-ahead-free and better-conditioned.
  * ``longest_streak_{W}m`` — the longest run of consecutive same-direction one-minute returns, normalized by
    the window. A run length is a sequential state machine over the ordered returns (not a sum), so it is the
    one piece evaluated by a light per-window pass over the return signs.

``compute_latest`` runs the SAME ``compute()`` on the buffer SLICED to this group's trailing window
(``LOOKBACK_MINUTES`` = deepest declared window + slack) before filtering to T — not a second formulation, so
backfill and live stay identical by construction (guarded by tests/test_fp_latest.py); it just avoids re-rolling
the ~300m buffer the group never reads. This is not a ``ReductionGroup``, so the incremental reduction engine
does not run it.
"""
from __future__ import annotations

import polars as pl

from quantlib.features.base import (
    BatchContext,
    FeatureGroup,
    FeatureSpec,
    FeatureType,
    InputSpec,
)
from quantlib.features.registry import register

WINDOWS: tuple[int, ...] = (5, 10, 15, 20, 30, 60)
RUN_TOL = 1e-4
# residual_skew is a THIRD moment (m3/m2**1.5). The whole-buffer rolling ``compute()`` and the window-sliced
# live ``compute_latest`` evaluate the IDENTICAL window-local fit (the trailing window's own bars on a
# window-relative time axis), so they agree to the rolling-vs-group_by float-summation noise the rest of the
# moment families declare. longest_streak is an EXACT run length and keeps RUN_TOL.
SKEW_TOL = 1e-4

# Live ``compute_latest`` slices the buffer to this trailing depth before running the SAME ``compute()``.
# The deepest declared window is ``max(WINDOWS)``; +15m of slack covers the 1-bar lag the per-minute return
# (``close.shift(1)``) needs at the window's start and leaves wide margin (correctness > speed). Too-tight a
# slice fails the generic parity test (tests/test_fp_latest.py) loudly — the guard.
LOOKBACK_MINUTES = max(WINDOWS) + 15
MIN_POINTS = 4  # the old codebase required >=4 closes for a meaningful residual distribution
# Skewness = m3 / m2**1.5; m2 is the residual VARIANCE (price^2 units). An absolute ``m2 > 0`` guard is too
# loose: when the window's price path is near-perfectly linear the residual variance collapses to f32 noise
# (~1e-18 of the price level) yet stays positive, so the ratio explodes by ~8 orders of magnitude (observed
# live: residual_skew up to ±1.6e9 vs the declared ±20 range). Gate instead on a RELATIVE residual spread:
# the residual std must exceed REL_RESID_FLOOR of the window's mean price, i.e. m2 > (REL_RESID_FLOOR·my)^2.
# 1e-6 is far below any real intraday tick noise, so it only nulls the genuinely-degenerate near-linear fits.
REL_RESID_FLOOR = 1e-6


def _residual_skew_window(frame: pl.DataFrame, w: int) -> pl.DataFrame:
    """Window-LOCAL skewness of the OLS residuals over the trailing ``w`` minutes, computed point-in-time.

    For each (symbol, minute=T) the trailing window (T−w, T] of (close, epoch) is gathered as a list, and the
    OLS fit of close on a WINDOW-RELATIVE time axis (``__x`` = minutes since that window's earliest bar — gap
    aware, from the actual epochs) is taken VECTORIZED via polars list expressions; the feature is the
    standardized third moment ``m3/m2**1.5`` of the residuals. Because the axis origin is the window's OWN first
    bar and never any later/buffer-wide minute, the value at T uses only bars ≤ T and is identical regardless of
    how far the buffer extends past T (no cross-window origin, no power-sum cancellation). Null where undefined
    (n < MIN_POINTS, zero x-variance, or a residual spread below ``REL_RESID_FLOOR`` of the window mean price —
    the m3/m2**1.5 ratio blows up when m2 is float noise on a near-linear path)."""
    col = f"residual_skew_{w}m"
    gathered = frame.rolling(index_column="minute", period=f"{w}m", group_by="symbol").agg(
        pl.col("close").alias("__c"), pl.col("__epoch").alias("__ep")
    )
    return _residual_skew_from_lists(gathered, ["symbol", "minute"], col)


def _residual_skew_from_lists(gathered: pl.DataFrame, keys: list[str], col: str) -> pl.DataFrame:
    """The window-local standardized third residual moment from a frame whose ``__c`` (closes) / ``__ep``
    (epochs) are per-row LIST columns of one window's gathered bars. Shared by the rolling backfill path
    (``_residual_skew_window``, one list per (symbol, minute)) and the live latest-only path
    (``_compute_skew_latest``, one list per symbol at T) so the OLS + m3/m2**1.5 algebra is a SINGLE source of
    truth — identical values on both paths by construction. Returns ``keys`` + the ``col`` value (null where
    undefined: n < MIN_POINTS, zero x-variance, or a residual spread below REL_RESID_FLOOR of the window mean
    price)."""
    close_list = pl.col("__c")
    x_axis = (pl.col("__ep") - pl.col("__ep").list.min()) / 60.0  # window-relative minutes (gap-aware)
    out = gathered.with_columns(x_axis.alias("__x"))
    xc = pl.col("__x") - pl.col("__x").list.mean()  # center on the window's own mean (origin-invariant residuals)
    yc = close_list - close_list.list.mean()
    out = out.with_columns(xc.alias("__xc"), yc.alias("__yc"), close_list.list.mean().alias("__my"))
    sxx_c = (pl.col("__xc") * pl.col("__xc")).list.sum()
    sxy_c = (pl.col("__xc") * pl.col("__yc")).list.sum()
    out = out.with_columns(sxx_c.alias("__sxxc"), (sxy_c / sxx_c).alias("__slope"))
    resid = pl.col("__yc") - pl.col("__slope") * pl.col("__xc")
    out = out.with_columns(resid.alias("__r"))
    m2 = (pl.col("__r") * pl.col("__r")).list.mean()
    m3 = (pl.col("__r") * pl.col("__r") * pl.col("__r")).list.mean()
    out = out.with_columns(m2.alias("__m2"), m3.alias("__m3"), close_list.list.len().alias("__n"))
    resid_var_floor = (REL_RESID_FLOOR * pl.col("__my")).pow(2)  # (rel_eps · price)^2 — near-linear cutoff
    defined = (pl.col("__n") >= MIN_POINTS) & (pl.col("__sxxc") > 0.0) & (pl.col("__m2") > resid_var_floor)
    value = pl.when(defined).then(pl.col("__m3") / pl.col("__m2").clip(lower_bound=0.0).pow(1.5)).otherwise(None)
    return out.select(*keys, value.cast(pl.Float64).alias(col))


def _global_run_length(present: pl.DataFrame) -> pl.DataFrame:
    """Per-symbol GLOBAL run length of consecutive same-direction nonzero returns, on the present-return rows
    (``present`` is the frame filtered to rows with a non-null ``__ret``, symbol/minute-sorted). ``__rl`` at a
    bar = how many bars the same-sign run has reached at that bar (0 on a zero return — a flat bar breaks the
    run, matching the old codebase). A windowed longest run is then ``max_i min(__rl_i, position_in_window_i)``
    (capping a run so it cannot count bars before the window's start)."""
    sgn = pl.when(pl.col("__ret") > 0.0).then(1).when(pl.col("__ret") < 0.0).then(-1).otherwise(0)
    present = present.with_columns(sgn.alias("__sgn"))
    new_run = ((pl.col("__sgn") != pl.col("__sgn").shift(1).over("symbol")) | (pl.col("__sgn") == 0)).fill_null(True)
    present = present.with_columns(new_run.cum_sum().over("symbol").alias("__runid"))
    rank = pl.col("__sgn").cum_count().over(["symbol", "__runid"])
    run_length = pl.when(pl.col("__sgn") == 0).then(0).otherwise(rank - rank.min().over(["symbol", "__runid"]) + 1)
    return present.with_columns(run_length.cast(pl.Int64).alias("__rl"))


@register
class MomentumRunGroup(FeatureGroup):
    name = "momentum_run"
    # 2.0.0: residual_skew reformulated as a window-local point-in-time fit (was buffer-relative rolling
    # power sums that leaked look-ahead by re-rounding past values when future bars appended). VALUES change
    # for residual_skew_*, so the feature contract / bus fingerprint changes — deploys in the post-close bundle.
    version = "2.0.0"
    owner = "modeller"
    type = FeatureType.TREND_QUALITY
    inputs = (InputSpec(name="minute_agg", columns=("symbol", "minute", "close")),)

    def declare(self) -> list[FeatureSpec]:
        specs: list[FeatureSpec] = []
        for w in WINDOWS:
            specs.append(
                FeatureSpec(name=f"residual_skew_{w}m", description=f"Skewness of the OLS trend residuals over {w} minutes: positive = price spends more time below the line then snaps up; asymmetry of the deviations.",
                            dtype="Float64", valid_range=(-20.0, 20.0), nan_policy="warmup", layer="A", tolerance=SKEW_TOL)
            )
            specs.append(
                FeatureSpec(name=f"longest_streak_{w}m", description=f"Longest run of consecutive same-direction one-minute returns over {w} minutes, normalized by the window; high = a sustained one-way push.",
                            dtype="Float64", valid_range=(0.0, 1.5), nan_policy="warmup", layer="A", tolerance=RUN_TOL)
            )
        return specs

    def _prepared(self, ctx: BatchContext) -> pl.DataFrame:
        """The sorted (symbol, minute, close) input with the per-bar epoch + one-minute return both halves read."""
        frame = ctx.frame("minute_agg").select(["symbol", "minute", "close"]).sort(["symbol", "minute"])
        if frame.height == 0:
            return frame
        return frame.with_columns(
            pl.col("minute").dt.epoch("s").cast(pl.Float64).alias("__epoch"),
            (pl.col("close") / pl.col("close").shift(1).over("symbol") - 1.0).alias("__ret"),
        )

    def _empty(self) -> pl.DataFrame:
        schema = {"symbol": pl.String, "minute": pl.Datetime("us", "UTC"), **{name: pl.Float64 for name in self.feature_names}}
        return pl.DataFrame(schema=schema)

    def _compute_skew(self, frame: pl.DataFrame) -> pl.DataFrame:
        """The residual_skew columns: each window's standardized third residual moment over a WINDOW-LOCAL fit
        (point-in-time). ``frame`` is the ``_prepared`` frame."""
        out = frame.select(["symbol", "minute"]).sort(["symbol", "minute"])
        for w in WINDOWS:
            out = out.join(_residual_skew_window(frame, w), on=["symbol", "minute"], how="left")
        return out

    def _compute_skew_latest(self, frame: pl.DataFrame, latest: object) -> pl.DataFrame:
        """The residual_skew columns at the LATEST minute T ONLY — the live-path fast form. The full
        ``_compute_skew`` runs a rolling gather over EVERY minute of the lookback slice then keeps only T;
        each window's value at T depends only on the trailing ``(T-w, T]`` bars, so gather that single slice
        per window directly (a minute-cutoff filter + per-symbol group, NOT a rolling) and run the IDENTICAL
        window-local OLS + standardized-third-moment. Value-identical to ``_compute_skew(...).filter(==T)`` by
        construction (same window bars, same origin, same algebra), at one gather/window instead of one
        per minute. ``frame`` is the ``_prepared`` frame."""
        base = frame.filter(pl.col("minute") == latest).select(["symbol", "minute"]).sort("symbol")
        for w in WINDOWS:
            col = f"residual_skew_{w}m"
            window = (
                frame.filter(
                    (pl.col("minute") > latest - pl.duration(minutes=w)) & (pl.col("minute") <= latest)
                )
                .group_by("symbol", maintain_order=True)
                .agg(pl.col("close").alias("__c"), pl.col("__epoch").alias("__ep"))
            )
            piece = _residual_skew_from_lists(window, ["symbol"], col)
            base = base.join(piece, on="symbol", how="left")
        return base.select(["symbol", "minute", *[f"residual_skew_{w}m" for w in WINDOWS]])

    def _compute_streak(self, frame: pl.DataFrame) -> pl.DataFrame:
        """The longest_streak columns: vectorized over the present-return rows' global run length, capped per
        window so a run cannot count bars before the window's start (max_i min(__rl_i, position_in_window_i)).
        ``frame`` is the ``_prepared`` frame."""
        out = frame.select(["symbol", "minute"]).sort(["symbol", "minute"])
        present = self._present_run_lengths(frame)
        for w in WINDOWS:
            collected = present.rolling(index_column="minute", period=f"{w}m", group_by="symbol").agg(
                pl.col("__rl").alias("_rl")
            )
            capped = pl.col("_rl").list.eval(pl.min_horizontal(pl.element(), pl.element().cum_count())).list.max()
            n_ret = pl.col("_rl").list.len()
            piece = collected.select(
                "symbol",
                "minute",
                pl.when(n_ret >= 2).then(capped / float(w)).otherwise(None).cast(pl.Float64).alias(f"longest_streak_{w}m"),
            )
            out = out.join(piece, on=["symbol", "minute"], how="left")
        return out

    def compute(self, ctx: BatchContext) -> pl.DataFrame:
        frame = self._prepared(ctx)
        if frame.height == 0:
            return self._empty()
        skew = self._compute_skew(frame)
        streak = self._compute_streak(frame)
        out = skew.join(streak, on=["symbol", "minute"], how="left")
        return out.select(["symbol", "minute", *self.feature_names])

    def compute_latest(self, ctx: BatchContext) -> pl.DataFrame:
        """Window-sliced live path: run the SAME ``compute()`` on the trailing input it reads, filtered to T.

        Two features need DIFFERENT slice depths, so the slice is sized to satisfy BOTH and is parity-true by
        construction for each:

          * ``residual_skew`` is a WINDOW-LOCAL point-in-time fit over <= ``max(WINDOWS)`` minutes (each
            window's own bars on a window-relative time axis) — it needs ONLY the trailing window, and bars
            dropped before the slice cannot change its value at T. ``LOOKBACK_MINUTES`` = deepest window + 15m
            slack covers it exactly.
          * ``longest_streak`` reads a per-bar ``close.shift(1)`` return at EVERY in-window bar; the EARLIEST
            in-window bar's return needs its POSITIONAL predecessor, which for a SPARSE symbol sits an arbitrary
            gap before the window (real-data audit: DIS had 2 bars 47m apart -> the boundary return nulled live
            while backfill resolved it). The streak therefore needs one extra prior bar per symbol.

        So: compute ``residual_skew`` on the tight ``LOOKBACK_MINUTES`` slice, compute ``longest_streak`` on the
        SAME slice PLUS each symbol's last bar strictly before it, and stitch the two at T. Both halves run the
        identical ``compute()`` math their backfill form uses, on the minimal input each needs — live ==
        backfill cell-for-cell for dense AND sparse symbols (guarded by tests/test_fp_momentum_run.py)."""
        frame = ctx.frame("minute_agg")
        if frame.height == 0:
            return self.compute(ctx)
        # residual_skew at T ONLY: gather each window's trailing (T-w, T] slice once (not a rolling over every
        # lookback minute then discard all but T). Value-identical to the rolling form filtered to T — the
        # window bars, the window-local origin, and the OLS+m3/m2**1.5 algebra are the SAME (shared
        # _residual_skew_from_lists). The deepest window only reads the last max(WINDOWS) minutes, so the tight
        # LOOKBACK_MINUTES slice is a safe (and sufficient) input bound for the gathers.
        skew_slice = frame.filter(pl.col("minute") >= frame["minute"].max() - pl.duration(minutes=LOOKBACK_MINUTES))
        skew_prepared = self._prepared(BatchContext(frames={"minute_agg": skew_slice}))
        skew = self._compute_skew_latest(skew_prepared, skew_prepared["minute"].max())
        # The streak half needs ONLY longest_streak — compute it directly (not via the full compute(), which
        # would redundantly recompute the window-local residual_skew gather on this slice and throw it away).
        # Same slice (+ each symbol's prior bar for the window-edge return), same _compute_streak math.
        streak_slice = self._slice_with_prior_bar(frame, LOOKBACK_MINUTES)
        streak_prepared = self._prepared(BatchContext(frames={"minute_agg": streak_slice}))
        streak_full = self._compute_streak(streak_prepared)
        latest = streak_full["minute"].max()
        streak = streak_full.filter(pl.col("minute") == latest)
        return skew.join(streak, on=["symbol", "minute"], how="full", coalesce=True).select(
            ["symbol", "minute", *self.feature_names]
        )

    @staticmethod
    def _slice_with_prior_bar(frame: pl.DataFrame, lookback_minutes: int) -> pl.DataFrame:
        """The trailing ``lookback_minutes`` of ``frame`` PLUS each symbol's single most-recent bar strictly
        before the cutoff — so a window-edge ``close.shift(1)`` return resolves to the SAME predecessor backfill
        sees, even when a sparse symbol's predecessor is an arbitrary gap back. Bars older than the prior bar
        cannot enter any window ending at T within ``lookback_minutes``, so this is parity-true by construction."""
        cutoff = frame["minute"].max() - pl.duration(minutes=lookback_minutes)
        in_window = frame.filter(pl.col("minute") >= cutoff)
        prior = (
            frame.filter(pl.col("minute") < cutoff).sort("minute").group_by("symbol", maintain_order=True).tail(1)
        )
        if prior.height == 0:
            return in_window
        return pl.concat([prior, in_window], how="vertical_relaxed").sort(["symbol", "minute"])

    def _present_run_lengths(self, frame: pl.DataFrame) -> pl.DataFrame:
        """The present-return rows (non-null ``__ret``) with the per-symbol global run-length column ``__rl``."""
        present = frame.filter(pl.col("__ret").is_not_null()).select(["symbol", "minute", "__ret"])
        if present.height == 0:
            return present.with_columns(pl.lit(0, dtype=pl.Int64).alias("__rl"))
        return _global_run_length(present)
