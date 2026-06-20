"""Small-cap morning-runner state — the point-in-time detector of the runner regime (family: PRICE).

Characterized in experiments/2026-06-16-r1-morning-runners (643 CORE runner-days / 468 symbols over
379 trading days): a $2-20 name that runs +50%+ off the prior close in the first 30 minutes on a
volume surge FADES — median EOD close -17.8% off the first-30-min high, fwd 1d/3d/5d -6.3/-9.8/-13.9%.
The tradeable shape is a (gated) SHORT; this group is the parity-true FEATURE the all-features model
uses to condition on that small-cap reversal regime. No existing group encodes it.

POINT-IN-TIME by construction. At each RTH minute t, using ONLY bars from the ET session open through
t, it tracks the RUNNING state since the open (cumulative max-high, cumulative dollar volume). The
running high at/after 10:00 ET equals the first-30-min high the study used, so the study's calibration
applies directly; pre-10:00 the feature is still well-defined (a partial running high). The cumulative
reduce is partitioned by (symbol, ET-session-date) and confined to RTH minutes, so it resets every
session and never reaches across days. Parity-true: ``compute_latest`` reruns the identical running
computation on the latest minute (auto-guarded by tests/test_fp_latest.py). NO wall-clock time
(everything anchors to the bar timestamp); NO dead-band on the continuous features.
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

BAND_LO = 2.0
BAND_HI = 20.0
ACTIVE_EARLY_MOVE = 0.30


@register
class RunnerStateGroup(FeatureGroup):
    name = "runner_state"
    version = "1.0.0"
    owner = "modeller"
    type = FeatureType.PRICE
    inputs = (
        InputSpec(
            name="minute_agg",
            columns=("symbol", "minute", "open", "high", "close", "volume"),
        ),
        InputSpec(name="daily", columns=("symbol", "date", "close")),
    )

    def declare(self) -> list[FeatureSpec]:
        return [
            FeatureSpec(
                name="runner_early_move",
                description="Running max gain off the prior close since the session open: max(high so far)/prev_close - 1. At/after 10:00 ET this equals the first-30-min early_move the runner study used.",
                dtype="Float64",
                valid_range=(-1.0, 30.0),
                nan_policy="warmup",
                layer="A",
            ),
            FeatureSpec(
                name="runner_gap_open",
                description="Overnight gap leg: session open / prior close - 1.",
                dtype="Float64",
                valid_range=(-1.0, 30.0),
                nan_policy="warmup",
                layer="A",
            ),
            FeatureSpec(
                name="runner_pullback_from_high",
                description="Give-back so far: last close / running max-high-since-open - 1 (<=0; the fade signal as it develops — runners close a median -17.8% off the high).",
                dtype="Float64",
                valid_range=(-1.0, 0.5),
                nan_policy="warmup",
                layer="A",
            ),
            FeatureSpec(
                name="runner_log_dollar_vol",
                description="log1p of cumulative dollar volume (sum of close*volume) since the session open — the runner-day liquidity / participation so far.",
                dtype="Float64",
                valid_range=(0.0, None),
                nan_policy="warmup",
                layer="A",
            ),
            FeatureSpec(
                name="runner_in_band",
                description="1 if prior close is in the small-cap runner band [$2,$20], else 0.",
                dtype="Int8",
                valid_range=(0.0, 1.0),
                nan_policy="warmup",
                layer="A",
            ),
            FeatureSpec(
                name="runner_is_active",
                description="1 if runner_in_band AND runner_early_move >= 0.30 (the small-cap-runner regime flag), else 0.",
                dtype="Int8",
                valid_range=(0.0, 1.0),
                nan_policy="warmup",
                layer="A",
            ),
        ]

    def _prev_close(self, ctx: BatchContext) -> pl.DataFrame:
        """Prior-day RTH close per (symbol, ET-session-date) — the band + early_move denominator."""
        daily = (
            ctx.frame("daily")
            .select(["symbol", "date", "close"])
            .sort(["symbol", "date"])
        )
        return daily.with_columns(
            pl.col("close").shift(1).over("symbol").alias("prev_close")
        ).select(["symbol", "date", "prev_close"])

    def _running(self, ctx: BatchContext) -> pl.DataFrame:
        """Per RTH minute, the running since-open state partitioned by (symbol, ET-session-date)."""
        frame = ctx.frame("minute_agg").select(
            ["symbol", "minute", "open", "high", "close", "volume"]
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
            pl.col("high").cum_max().over(keys).alias("_run_high"),
            (pl.col("close") * pl.col("volume"))
            .cum_sum()
            .over(keys)
            .alias("_run_dollar"),
            pl.col("open").first().over(keys).alias("_sess_open"),
        )

    def _assemble(self, ctx: BatchContext, out_keys: pl.DataFrame) -> pl.DataFrame:
        names = [spec.name for spec in self.declare()]
        running = self._running(ctx)
        prev = self._prev_close(ctx)
        joined = running.join(
            prev, left_on=["symbol", "sdate"], right_on=["symbol", "date"], how="left"
        )
        early_move = pl.col("_run_high") / pl.col("prev_close") - 1.0
        in_band = (pl.col("prev_close") >= BAND_LO) & (pl.col("prev_close") <= BAND_HI)
        feats = joined.with_columns(
            early_move.alias("runner_early_move"),
            (pl.col("_sess_open") / pl.col("prev_close") - 1.0).alias(
                "runner_gap_open"
            ),
            (pl.col("close") / pl.col("_run_high") - 1.0).alias(
                "runner_pullback_from_high"
            ),
            pl.col("_run_dollar").log1p().alias("runner_log_dollar_vol"),
            in_band.cast(pl.Int8).alias("runner_in_band"),
            (in_band & (early_move >= ACTIVE_EARLY_MOVE))
            .cast(pl.Int8)
            .alias("runner_is_active"),
        ).select(["symbol", "minute", *names])
        return out_keys.join(feats, on=["symbol", "minute"], how="left").select(
            ["symbol", "minute", *names]
        )

    def _assemble_latest(self, ctx: BatchContext, latest: object) -> pl.DataFrame:
        """Latest-minute live form: the running since-open state AT T is the aggregate over T's OWN session
        up to and including T. Because T is the latest minute, "up to T" is the whole current session in the
        buffer, so the running ``cum_max``/``cum_sum``/``first`` at T equal a SINGLE per-(symbol, session)
        ``max``/``sum``/``first`` — reduce each symbol's current session ONCE (NOT a per-minute cumulative
        scan over ~390 bars) and emit T's row. Value-identical to ``_assemble(...).filter(minute == T)`` by
        construction: a running max/sum/first at the LAST bar of a window equals the window's max/sum/first;
        same RTH filter, same partition, same feature algebra."""
        names = [spec.name for spec in self.declare()]
        frame = ctx.frame("minute_agg").select(["symbol", "minute", "open", "high", "close", "volume"])
        et_minute = et_minute_of_day(pl.col("minute"))
        frame = frame.with_columns(
            pl.col("minute").dt.convert_time_zone("America/New_York").dt.date().alias("sdate"),
            et_minute.alias("_etm"),
        )
        latest_sdate = pl.lit(latest).dt.convert_time_zone("America/New_York").dt.date()
        session = frame.filter(
            (pl.col("_etm") >= OPEN_MINUTE)
            & (pl.col("sdate") == latest_sdate)
            & (pl.col("minute") <= latest)
        ).sort(["symbol", "minute"])
        agg = session.group_by("symbol", maintain_order=True).agg(
            pl.col("high").max().alias("_run_high"),
            (pl.col("close") * pl.col("volume")).sum().alias("_run_dollar"),
            pl.col("open").first().alias("_sess_open"),
            pl.col("close").last().alias("close"),
            pl.col("sdate").first().alias("sdate"),
            pl.col("minute").last().alias("minute"),
        )
        joined = agg.join(
            self._prev_close(ctx),
            left_on=["symbol", "sdate"],
            right_on=["symbol", "date"],
            how="left",
        )
        early_move = pl.col("_run_high") / pl.col("prev_close") - 1.0
        in_band = (pl.col("prev_close") >= BAND_LO) & (pl.col("prev_close") <= BAND_HI)
        feats = joined.with_columns(
            early_move.alias("runner_early_move"),
            (pl.col("_sess_open") / pl.col("prev_close") - 1.0).alias("runner_gap_open"),
            (pl.col("close") / pl.col("_run_high") - 1.0).alias("runner_pullback_from_high"),
            pl.col("_run_dollar").log1p().alias("runner_log_dollar_vol"),
            in_band.cast(pl.Int8).alias("runner_in_band"),
            (in_band & (early_move >= ACTIVE_EARLY_MOVE)).cast(pl.Int8).alias("runner_is_active"),
        ).select(["symbol", "minute", *names])
        keys = ctx.frame("minute_agg").select(["symbol", "minute"]).filter(pl.col("minute") == latest)
        return keys.join(feats, on=["symbol", "minute"], how="left").select(["symbol", "minute", *names])

    def compute(self, ctx: BatchContext) -> pl.DataFrame:
        return self._assemble(ctx, ctx.frame("minute_agg").select(["symbol", "minute"]))

    def compute_latest(self, ctx: BatchContext) -> pl.DataFrame:
        latest = ctx.frame("minute_agg")["minute"].max()
        return self._assemble_latest(ctx, latest)
