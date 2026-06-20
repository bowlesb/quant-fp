"""Gap-fill state — the running point-in-time fraction of the overnight gap that has filled (family:
PRICE).

Characterized in experiments/2026-06-16-r3-gap-fill (313,473 gapped-days, |gap|>=2%, 5,908 syms):
a name opens with an overnight gap (open vs prev_close), and over the session the gap PARTIALLY fills
back toward the prior close. The fill is strongly LIQUIDITY-MONOTONIC (illiquid names fill ~3.7x more
than liquid — stale-price diffusion) and gap-size-decreasing (big gaps are real news that doesn't
revert). The standalone gap-fill STRATEGY is a KILL (illiquid-concentrated, sub-cost in the liquid
tier) but the running fill state is a real, parity-true, NON-redundant FEATURE the model uses to
condition on the gap regime: ``gap_open`` is the gap LEVEL and ``overnight_share`` is the realized
split, but NOTHING encodes the *fraction of the gap filled by minute t* as it develops.

POINT-IN-TIME by construction. At each RTH minute t (using only bars from the ET session open through
t):
  - ``gap_open``-equivalent gap = session_open / prev_close - 1 (the gap to fill).
  - ``gap_fill_fraction`` = (close_t - session_open) / (prev_close - session_open) — 0 at the open,
    1.0 when price is back to prev_close (filled), <0 when price has EXTENDED past the open (momentum).
  - ``gap_extended`` (Int8) = 1 if the gap has extended (fraction < 0) at t.
Defined the moment prev_close and the session open exist; the gap denominator is null on a zero-gap
day (no gap to fill). ``compute_latest`` reruns the same code on the latest minute (no running state
beyond the session open + prev_close, both fixed within the day) -> parity-true by construction.
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


@register
class GapFillStateGroup(FeatureGroup):
    name = "gap_fill_state"
    version = "1.0.0"
    owner = "modeller"
    type = FeatureType.PRICE
    inputs = (
        InputSpec(name="minute_agg", columns=("symbol", "minute", "open", "close")),
        InputSpec(name="daily", columns=("symbol", "date", "close")),
    )

    def declare(self) -> list[FeatureSpec]:
        return [
            FeatureSpec(
                name="gap_fill_fraction",
                description="Fraction of the overnight gap filled by this minute: (close - session_open)/(prev_close - session_open). 0 at the open, 1.0 = back to prev_close (filled), <0 = extended past the open. NULL on a zero-gap day.",
                dtype="Float64",
                valid_range=(-5.0, 5.0),
                nan_policy="warmup",
                layer="A",
            ),
            FeatureSpec(
                name="gap_extended",
                description="1 if the gap has EXTENDED past the open (gap_fill_fraction < 0, i.e. momentum) at this minute, else 0. NULL on a zero-gap day.",
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

    def _session_open(self, ctx: BatchContext) -> pl.DataFrame:
        """The RTH session open (first RTH minute's open) per (symbol, ET-session-date)."""
        frame = ctx.frame("minute_agg").select(["symbol", "minute", "open", "close"])
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
        return rth.with_columns(
            pl.col("open").first().over(["symbol", "sdate"]).alias("_sess_open")
        )

    def _assemble(self, ctx: BatchContext, out_keys: pl.DataFrame) -> pl.DataFrame:
        names = [spec.name for spec in self.declare()]
        joined = self._session_open(ctx).join(
            self._prev_close(ctx),
            left_on=["symbol", "sdate"],
            right_on=["symbol", "date"],
            how="left",
        )
        denom = pl.col("prev_close") - pl.col("_sess_open")
        fill = (
            pl.when(denom.abs() > 1e-9)
            .then((pl.col("close") - pl.col("_sess_open")) / denom)
            .otherwise(None)
        )
        feats = joined.with_columns(
            fill.alias("gap_fill_fraction"),
            pl.when(denom.abs() > 1e-9)
            .then((fill < 0).cast(pl.Int8))
            .otherwise(None)
            .alias("gap_extended"),
        ).select(["symbol", "minute", *names])
        return out_keys.join(feats, on=["symbol", "minute"], how="left").select(
            ["symbol", "minute", *names]
        )

    def _assemble_latest(self, ctx: BatchContext, latest: object) -> pl.DataFrame:
        """Latest-minute live form: resolve each symbol's session-open for T's OWN session as a SINGLE row
        (``open.first()`` aggregated over that session's RTH bars, NOT broadcast over every minute), then
        evaluate the gap-fill ONLY on the rows at T. The gap-fill at T reads nothing but T's session-open,
        prev_close and T's close (session_open is fixed within the day, no cross-session state), so this is
        value-identical to ``_assemble(...).filter(minute == T)`` by construction — same RTH filter, same
        per-(symbol, sdate) first-open, same fill algebra — while touching only T's session, not the buffer."""
        names = [spec.name for spec in self.declare()]
        # The per-(symbol, current-session) aggregate is SHARED across the CumulativeState groups (runner/
        # dumper/gap_fill) — derived once per shard-minute, not three times. ``_sess_open`` (session open) and
        # ``close`` (T's close = the session's last bar) are this group's accumulators; the running max/min/sum
        # the other two need are ignored here.
        joined = session_cumulative_agg(ctx.frame("minute_agg"), latest).join(
            self._prev_close(ctx),
            left_on=["symbol", "sdate"],
            right_on=["symbol", "date"],
            how="left",
        )
        denom = pl.col("prev_close") - pl.col("_sess_open")
        fill = (
            pl.when(denom.abs() > 1e-9)
            .then((pl.col("close") - pl.col("_sess_open")) / denom)
            .otherwise(None)
        )
        feats = joined.with_columns(
            fill.alias("gap_fill_fraction"),
            pl.when(denom.abs() > 1e-9).then((fill < 0).cast(pl.Int8)).otherwise(None).alias("gap_extended"),
        ).select(["symbol", "minute", *names])
        keys = ctx.frame("minute_agg").select(["symbol", "minute"]).filter(pl.col("minute") == latest)
        return keys.join(feats, on=["symbol", "minute"], how="left").select(["symbol", "minute", *names])

    def compute(self, ctx: BatchContext) -> pl.DataFrame:
        return self._assemble(ctx, ctx.frame("minute_agg").select(["symbol", "minute"]))

    def compute_latest(self, ctx: BatchContext) -> pl.DataFrame:
        latest = ctx.frame("minute_agg")["minute"].max()
        return self._assemble_latest(ctx, latest)
