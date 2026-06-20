"""Small-cap morning-DUMPER state — the point-in-time detector of the small-cap-crash regime
(family: PRICE), the short-side mirror of ``runner_state`` (F9).

Characterized in experiments/2026-06-16-r2-morning-dumpers (161 CORE dumper-days / 149 syms over
379 trading days): a $2-20 name that DROPS -50%+ off the prior close in the first 30 min on a volume
surge BOUNCES intraday (median close +8.7% off the first-30-min LOW; 48% bounce >10%) but CONTINUES
DOWN multi-day (fwd 1d -6.4%, 5d -12.5%; only ~33% up). This is ASYMMETRIC vs the runner (which
fades down at BOTH horizons): the morning crash is a panic OVERSHOOT intraday but the drop carries
REAL distress/dilution information that keeps bleeding over days. This group is the parity-true
FEATURE the model uses to condition on that crash regime — non-redundant with runner_state (opposite
tail: run-UP off the high vs DROP and bounce off the LOW, plus the distinct multi-day-down asymmetry).

POINT-IN-TIME by construction. At each RTH minute t, using ONLY bars from the ET session open through
t, it tracks the RUNNING state since the open (cumulative MIN-low, cumulative dollar volume),
partitioned by (symbol, ET-session-date) and confined to RTH minutes, so it resets every session and
never reaches across days. NO wall-clock time. ``compute_latest`` reruns the identical running
computation on the latest minute (auto-guarded by tests/test_fp_latest.py).
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
from quantlib.features.session import OPEN_MINUTE, et_minute_of_day
from quantlib.features.session_cumulative import session_cumulative_agg

BAND_LO = 2.0
BAND_HI = 20.0
ACTIVE_EARLY_DROP = 0.30


@register
class DumperStateGroup(FeatureGroup):
    name = "dumper_state"
    version = "1.0.0"
    owner = "modeller"
    type = FeatureType.PRICE
    inputs = (
        InputSpec(
            name="minute_agg",
            columns=("symbol", "minute", "open", "low", "close", "volume"),
        ),
        InputSpec(name="daily", columns=("symbol", "date", "close")),
    )

    def declare(self) -> list[FeatureSpec]:
        return [
            FeatureSpec(
                name="dumper_early_drop",
                description="Running max drop off the prior close since the session open: 1 - min(low so far)/prev_close. At/after 10:00 ET this equals the first-30-min early_drop the dumper study used.",
                dtype="Float64",
                valid_range=(-1.0, 1.0),
                nan_policy="warmup",
                layer="A",
            ),
            FeatureSpec(
                name="dumper_gap_open",
                description="Overnight gap leg: session open / prior close - 1 (typically negative for a dumper).",
                dtype="Float64",
                valid_range=(-1.0, 5.0),
                nan_policy="warmup",
                layer="A",
            ),
            FeatureSpec(
                name="dumper_bounce_from_low",
                description="Bounce so far: last close / running min-low-since-open - 1 (>=0; the intraday recovery as it develops — dumpers close a median +8.7% off the low).",
                dtype="Float64",
                valid_range=(-0.5, 5.0),
                nan_policy="warmup",
                layer="A",
            ),
            FeatureSpec(
                name="dumper_log_dollar_vol",
                description="log1p of cumulative dollar volume (sum of close*volume) since the session open — the crash-day liquidity / participation so far.",
                dtype="Float64",
                valid_range=(0.0, None),
                nan_policy="warmup",
                layer="A",
            ),
            FeatureSpec(
                name="dumper_in_band",
                description="1 if prior close is in the small-cap band [$2,$20], else 0.",
                dtype="Int8",
                valid_range=(0.0, 1.0),
                nan_policy="warmup",
                layer="A",
            ),
            FeatureSpec(
                name="dumper_is_active",
                description="1 if dumper_in_band AND dumper_early_drop >= 0.30 (the small-cap-crash regime flag), else 0.",
                dtype="Int8",
                valid_range=(0.0, 1.0),
                nan_policy="warmup",
                layer="A",
            ),
        ]

    def _prev_close(self, ctx: BatchContext) -> pl.DataFrame:
        daily = (
            ctx.frame("daily")
            .select(["symbol", "date", "close"])
            .sort(["symbol", "date"])
        )
        return daily.with_columns(
            pl.col("close").shift(1).over("symbol").alias("prev_close")
        ).select(["symbol", "date", "prev_close"])

    def _running(self, ctx: BatchContext) -> pl.DataFrame:
        frame = ctx.frame("minute_agg").select(
            ["symbol", "minute", "open", "low", "close", "volume"]
        )
        et_minute = et_minute_of_day(pl.col("minute"))
        frame = frame.with_columns(
            pl.col("minute")
            .dt.convert_time_zone("America/New_York")
            .dt.date()
            .alias("sdate"),
            et_minute.alias("_etm"),
        )
        rth = frame.filter(pl.col("_etm") >= OPEN_MINUTE).sort(
            ["symbol", "sdate", "minute"]
        )
        keys = ["symbol", "sdate"]
        return rth.with_columns(
            pl.col("low").cum_min().over(keys).alias("_run_low"),
            (pl.col("close") * pl.col("volume"))
            .cum_sum()
            .over(keys)
            .alias("_run_dollar"),
            pl.col("open").first().over(keys).alias("_sess_open"),
        )

    def _assemble(self, ctx: BatchContext, out_keys: pl.DataFrame) -> pl.DataFrame:
        names = [spec.name for spec in self.declare()]
        joined = self._running(ctx).join(
            self._prev_close(ctx),
            left_on=["symbol", "sdate"],
            right_on=["symbol", "date"],
            how="left",
        )
        early_drop = 1.0 - pl.col("_run_low") / pl.col("prev_close")
        in_band = (pl.col("prev_close") >= BAND_LO) & (pl.col("prev_close") <= BAND_HI)
        feats = joined.with_columns(
            early_drop.alias("dumper_early_drop"),
            (pl.col("_sess_open") / pl.col("prev_close") - 1.0).alias(
                "dumper_gap_open"
            ),
            (pl.col("close") / pl.col("_run_low") - 1.0).alias(
                "dumper_bounce_from_low"
            ),
            pl.col("_run_dollar").log1p().alias("dumper_log_dollar_vol"),
            in_band.cast(pl.Int8).alias("dumper_in_band"),
            (in_band & (early_drop >= ACTIVE_EARLY_DROP))
            .cast(pl.Int8)
            .alias("dumper_is_active"),
        ).select(["symbol", "minute", *names])
        return out_keys.join(feats, on=["symbol", "minute"], how="left").select(
            ["symbol", "minute", *names]
        )

    def _assemble_latest(self, ctx: BatchContext, latest: object) -> pl.DataFrame:
        """Latest-minute live form: the running since-open state AT T is the aggregate over T's OWN session
        up to and including T. Because T is the latest minute, "up to T" is the whole current session in the
        buffer, so the running ``cum_min``/``cum_sum``/``first`` at T equal a SINGLE per-(symbol, session)
        ``min``/``sum``/``first`` — reduce each symbol's current session ONCE (NOT a per-minute cumulative
        scan over ~390 bars) and emit T's row. Value-identical to ``_assemble(...).filter(minute == T)`` by
        construction: a running min/sum/first at the LAST bar of a window equals the window's min/sum/first;
        same RTH filter, same partition, same feature algebra."""
        names = [spec.name for spec in self.declare()]
        # The per-(symbol, current-session) max/min/sum/first/last is SHARED across the CumulativeState groups
        # (runner/dumper/gap_fill) — derived once per shard-minute, not three times. ``_run_low`` /
        # ``_run_dollar`` / ``_sess_open`` / ``close`` are this group's accumulators (the others are ignored).
        agg = session_cumulative_agg(ctx.frame("minute_agg"), latest)
        joined = agg.join(
            self._prev_close(ctx),
            left_on=["symbol", "sdate"],
            right_on=["symbol", "date"],
            how="left",
        )
        early_drop = 1.0 - pl.col("_run_low") / pl.col("prev_close")
        in_band = (pl.col("prev_close") >= BAND_LO) & (pl.col("prev_close") <= BAND_HI)
        feats = joined.with_columns(
            early_drop.alias("dumper_early_drop"),
            (pl.col("_sess_open") / pl.col("prev_close") - 1.0).alias("dumper_gap_open"),
            (pl.col("close") / pl.col("_run_low") - 1.0).alias("dumper_bounce_from_low"),
            pl.col("_run_dollar").log1p().alias("dumper_log_dollar_vol"),
            in_band.cast(pl.Int8).alias("dumper_in_band"),
            (in_band & (early_drop >= ACTIVE_EARLY_DROP)).cast(pl.Int8).alias("dumper_is_active"),
        ).select(["symbol", "minute", *names])
        keys = ctx.frame("minute_agg").select(["symbol", "minute"]).filter(pl.col("minute") == latest)
        return keys.join(feats, on=["symbol", "minute"], how="left").select(["symbol", "minute", *names])

    def compute(self, ctx: BatchContext) -> pl.DataFrame:
        return self._assemble(ctx, ctx.frame("minute_agg").select(["symbol", "minute"]))

    def compute_latest(self, ctx: BatchContext) -> pl.DataFrame:
        latest = ctx.frame("minute_agg")["minute"].max()
        return self._assemble_latest(ctx, latest)
