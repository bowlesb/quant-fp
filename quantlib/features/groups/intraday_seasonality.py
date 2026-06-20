"""Intraday-seasonality-adjusted activity — is this name's volume / move ANOMALOUS for THIS time of
day (family: PRICE).

Intraday activity is strongly and DETERMINISTICALLY U-shaped (R4 study, 1,500 liquid names, 379d:
close volume 5.2x midday, open |return| 3.28x midday; first-half/second-half profile rank-corr 0.995).
A raw volume / |return| level therefore CONFLATES the time-of-day seasonal with the genuine name-
specific shock — a 0.13% move is routine at 09:30 but a tail event at 12:30. This group removes the
deterministic time-of-day seasonal, isolating the real shock — the same demean logic that made
``return_dispersion`` (universe axis) and ``peer_relative`` (peer axis) non-redundant, now on the TIME
axis. Non-redundant with raw volume/volatility (they carry the seasonal) and with the time-of-day
LABELS (``minute_of_day_et``; they do not normalize anything).

Two features per (symbol, minute):
  - ``absret_vs_tod``  = |close/open - 1| / baseline_absret[tod-bucket]. Scale-free (|ret| is unitless),
    so the pooled market baseline applies to every name: >1 = a bigger move than typical for this clock.
  - ``volume_vs_tod``  = volume / (running since-open MEAN volume * vol_shape[tod-bucket]). Removes the
    symbol's own LEVEL (its running mean) AND the time-of-day seasonal (the unitless shape factor):
    >1 = more volume than this name's typical minute scaled to this time of day.

PARITY: the tod baseline (baseline_absret + vol_shape per 30-min ET bucket) is a FROZEN committed
lookup (``data/intraday_seasonality_v1.parquet``), refreshed nightly from settled history — identical
in stream and backfill (no intraday state in the baseline). The running since-open mean volume is a
cum-mean partitioned by (symbol, ET-session-date) over the FULL session buffer; ``compute_latest``
reruns the identical running computation on the latest minute (auto-guarded by tests/test_fp_latest.py).
NO wall-clock time (the tod bucket is derived from the bar timestamp).
"""

from __future__ import annotations

from pathlib import Path

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

_BASELINE_PATH = (
    Path(__file__).parent.parent / "data" / "intraday_seasonality_v1.parquet"
)
BUCKET = 30
CLOSE_MINUTE_EXCL = 960  # 16:00 ET


def _load_baseline() -> pl.DataFrame:
    if not _BASELINE_PATH.exists():
        return pl.DataFrame(
            schema={
                "bucket": pl.Int32,
                "baseline_absret": pl.Float64,
                "vol_shape": pl.Float64,
            }
        )
    return pl.read_parquet(_BASELINE_PATH).select(
        pl.col("bucket").cast(pl.Int32),
        pl.col("baseline_absret").cast(pl.Float64),
        pl.col("vol_shape").cast(pl.Float64),
    )


@register
class IntradaySeasonalityGroup(FeatureGroup):
    name = "intraday_seasonality"
    version = "1.0.0"
    owner = "modeller"
    type = FeatureType.PRICE
    inputs = (
        InputSpec(
            name="minute_agg", columns=("symbol", "minute", "open", "close", "volume")
        ),
    )

    def declare(self) -> list[FeatureSpec]:
        return [
            FeatureSpec(
                name="absret_vs_tod",
                description="This minute's |close/open-1| divided by the typical |return| for this time-of-day bucket (the frozen intraday-seasonality baseline). >1 = a bigger move than normal for this clock. NULL outside RTH / unmapped bucket.",
                dtype="Float64",
                valid_range=(0.0, None),
                nan_policy="sparse",
                layer="A",
            ),
            FeatureSpec(
                name="volume_vs_tod",
                description="This minute's volume divided by (the name's running since-open mean volume * the time-of-day volume-shape factor). >1 = more volume than this name's typical minute scaled to this time of day. NULL outside RTH / unmapped bucket.",
                dtype="Float64",
                valid_range=(0.0, None),
                nan_policy="sparse",
                layer="A",
            ),
        ]

    def _assemble(self, ctx: BatchContext, out_keys: pl.DataFrame) -> pl.DataFrame:
        names = [spec.name for spec in self.declare()]
        baseline = _load_baseline()
        frame = ctx.frame("minute_agg").select(
            ["symbol", "minute", "open", "close", "volume"]
        )
        etm = et_minute_of_day(pl.col("minute"))
        frame = frame.with_columns(
            pl.col("minute")
            .dt.convert_time_zone("America/New_York")
            .dt.date()
            .alias("sdate"),
            etm.alias("_etm"),
        )
        rth = frame.filter(
            (pl.col("_etm") >= OPEN_MINUTE) & (pl.col("_etm") < CLOSE_MINUTE_EXCL)
        ).sort(["symbol", "sdate", "minute"])
        rth = rth.with_columns(
            (((pl.col("_etm") - OPEN_MINUTE) // BUCKET) * BUCKET + OPEN_MINUTE)
            .cast(pl.Int32)
            .alias("bucket"),
            pl.col("volume").cum_sum().over(["symbol", "sdate"]).alias("_cvol"),
            pl.int_range(1, pl.len() + 1).over(["symbol", "sdate"]).alias("_n"),
        )
        joined = rth.join(baseline, on="bucket", how="left")
        run_mean_vol = pl.col("_cvol") / pl.col("_n")
        absret = (pl.col("close") / pl.col("open") - 1.0).abs()
        feats = joined.with_columns(
            pl.when(pl.col("baseline_absret") > 0)
            .then(absret / pl.col("baseline_absret"))
            .otherwise(None)
            .alias("absret_vs_tod"),
            pl.when((run_mean_vol > 0) & (pl.col("vol_shape") > 0))
            .then(pl.col("volume") / (run_mean_vol * pl.col("vol_shape")))
            .otherwise(None)
            .alias("volume_vs_tod"),
        ).select(["symbol", "minute", *names])
        return out_keys.join(feats, on=["symbol", "minute"], how="left").select(
            ["symbol", "minute", *names]
        )

    def _assemble_latest(self, ctx: BatchContext, latest: object) -> pl.DataFrame:
        """Latest-minute live form: the running since-open MEAN volume at T is ``volume.sum()/count`` over T's
        OWN RTH session up to and including T (T is the latest minute, so the cumulative ``_cvol``/``_n`` at T
        equal a SINGLE per-(symbol, session) ``sum``/``len``), and the tod-bucket / absret read T's own row.
        Value-identical to ``_assemble(...).filter(minute == T)`` by construction — same RTH+session filter,
        same per-(symbol, sdate) cumulative reduce at its last bar, same bucket/baseline algebra — while
        touching only T's session, NOT a per-minute cum_sum over the whole buffer."""
        names = [spec.name for spec in self.declare()]
        baseline = _load_baseline()
        frame = ctx.frame("minute_agg").select(["symbol", "minute", "open", "close", "volume"])
        etm = et_minute_of_day(pl.col("minute"))
        frame = frame.with_columns(
            pl.col("minute").dt.convert_time_zone("America/New_York").dt.date().alias("sdate"),
            etm.alias("_etm"),
        )
        latest_sdate = pl.lit(latest).dt.convert_time_zone("America/New_York").dt.date()
        session = frame.filter(
            (pl.col("_etm") >= OPEN_MINUTE)
            & (pl.col("_etm") < CLOSE_MINUTE_EXCL)
            & (pl.col("sdate") == latest_sdate)
            & (pl.col("minute") <= latest)
        ).sort(["symbol", "minute"])
        agg = session.group_by("symbol", maintain_order=True).agg(
            pl.col("volume").sum().alias("_cvol"),
            pl.len().alias("_n"),
            pl.col("volume").last().alias("volume"),
            pl.col("open").last().alias("open"),
            pl.col("close").last().alias("close"),
            pl.col("_etm").last().alias("_etm"),
            pl.col("minute").last().alias("minute"),
        )
        bucket = ((pl.col("_etm") - OPEN_MINUTE) // BUCKET) * BUCKET + OPEN_MINUTE
        joined = agg.with_columns(bucket.cast(pl.Int32).alias("bucket")).join(
            baseline, on="bucket", how="left"
        )
        run_mean_vol = pl.col("_cvol") / pl.col("_n")
        absret = (pl.col("close") / pl.col("open") - 1.0).abs()
        feats = joined.with_columns(
            pl.when(pl.col("baseline_absret") > 0)
            .then(absret / pl.col("baseline_absret"))
            .otherwise(None)
            .alias("absret_vs_tod"),
            pl.when((run_mean_vol > 0) & (pl.col("vol_shape") > 0))
            .then(pl.col("volume") / (run_mean_vol * pl.col("vol_shape")))
            .otherwise(None)
            .alias("volume_vs_tod"),
        ).select(["symbol", "minute", *names])
        keys = ctx.frame("minute_agg").select(["symbol", "minute"]).filter(pl.col("minute") == latest)
        return keys.join(feats, on=["symbol", "minute"], how="left").select(["symbol", "minute", *names])

    def compute(self, ctx: BatchContext) -> pl.DataFrame:
        return self._assemble(ctx, ctx.frame("minute_agg").select(["symbol", "minute"]))

    def compute_latest(self, ctx: BatchContext) -> pl.DataFrame:
        latest = ctx.frame("minute_agg")["minute"].max()
        return self._assemble_latest(ctx, latest)
