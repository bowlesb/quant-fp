"""Daily volatility TERM-STRUCTURE — multi-day vol expanding vs contracting (family: VOLATILITY).

The daily analogue of the intraday ``vol_term_structure``. ``multi_day`` exposes daily realized-vol
LEVELS (daily_vol_{5,10,20,30,60}d = rolling std of daily returns) but NO daily term-structure RATIO —
whether multi-day vol is EXPANDING (short-horizon daily vol > long) or CONTRACTING. This is the
multi-day vol-regime / vol-mean-reversion-speed quantity that conditions risk premia (e.g. the W11
overnight-beta premium is regime-conditional on dispersion/vol). A tree splits on thresholds, not on a
ratio of two daily_vol level columns, so the explicit daily term-structure ratio is genuinely additive.
Distinct from the intraday vol_term_structure (minute horizon) the same way daily_beta is distinct from
the intraday market_beta — the multi-day vol regime is a different, slower quantity.

Per (symbol, date), point-in-time as of the prior close (the multi_day convention):
  - ``daily_vol_term_5_20``  = daily_vol_5d  / daily_vol_20d
  - ``daily_vol_term_20_60`` = daily_vol_20d / daily_vol_60d
where daily_vol_{w}d is the SAME rolling-std-of-daily-returns ``multi_day`` computes (mirrored exactly),
so the ratio is consistent with the platform's daily-vol definition. >1 = vol expanding over that
horizon band, <1 = contracting.

A DAILY-broadcast group (like ``multi_day`` / ``daily_beta``): the daily features are computed per
(symbol, date) from the daily frame (which carries ~200-370d, so the 60d window is warm), then joined
onto every minute of that day. Source-independent (settled daily bars) → parity-true by construction;
``compute_latest`` reruns the same code on the latest minute. NULL on a degenerate flat long-horizon
denominator (absolute floor guard — the DataIntegrity-4 lesson applied from the START, so stream and
backfill agree, no +/-inf). STATIC windowed — NO FeatureState.
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

# (short, long) daily-vol horizon pairs; the rolling-std windows are the union.
TERM_PAIRS: tuple[tuple[int, int], ...] = ((5, 20), (20, 60))
_VOL_DAYS: tuple[int, ...] = tuple(sorted({w for pair in TERM_PAIRS for w in pair}))
# A daily realized vol below this absolute floor is a degenerate flat window where short/long overflows;
# emit NULL there so stream and backfill agree (the DataIntegrity-4 parity discipline).
_VOL_FLOOR = 1e-9


@register
class DailyVolTermStructureGroup(FeatureGroup):
    name = "daily_vol_term_structure"
    version = "1.0.0"
    owner = "modeller"
    type = FeatureType.VOLATILITY
    inputs = (
        InputSpec(name="daily", columns=("symbol", "date", "close")),
        InputSpec(name="minute_agg", columns=("symbol", "minute")),
    )

    def declare(self) -> list[FeatureSpec]:
        return [
            FeatureSpec(
                name=f"daily_vol_term_{short}_{long}",
                description=(
                    f"Daily volatility term-structure: daily_vol_{short}d / daily_vol_{long}d (rolling std "
                    f"of daily returns). >1 = multi-day vol EXPANDING over the {short}d-vs-{long}d band, "
                    f"<1 = contracting. NULL on a degenerate flat long-horizon window."
                ),
                dtype="Float64",
                valid_range=(0.0, None),
                nan_policy="warmup",
                layer="A",
            )
            for short, long in TERM_PAIRS
        ]

    def _daily_features(self, ctx: BatchContext) -> pl.DataFrame:
        """Per (symbol, date) daily-vol ratios, point-in-time as of the PRIOR close (multi_day convention:
        daily_vol_{w}d = rolling std of the prior-close-to-prior-close daily returns over w days).
        """
        daily = (
            ctx.frame("daily")
            .select(["symbol", "date", "close"])
            .sort(["symbol", "date"])
        )
        asof = pl.col("close").shift(1).over("symbol")
        daily = daily.with_columns(asof.alias("_asof"))
        daily = daily.with_columns(
            (pl.col("_asof") / pl.col("_asof").shift(1).over("symbol") - 1.0).alias(
                "_dret"
            )
        )
        vol_exprs = [
            pl.col("_dret")
            .rolling_std(window_size=w)
            .over("symbol")
            .alias(f"_dvol_{w}")
            for w in _VOL_DAYS
        ]
        daily = daily.with_columns(vol_exprs)
        term_exprs = []
        for short, long in TERM_PAIRS:
            long_vol = pl.col(f"_dvol_{long}")
            term_exprs.append(
                pl.when(long_vol > _VOL_FLOOR)
                .then(pl.col(f"_dvol_{short}") / long_vol)
                .otherwise(pl.lit(None, dtype=pl.Float64))
                .alias(f"daily_vol_term_{short}_{long}")
            )
        names = [f"daily_vol_term_{short}_{long}" for short, long in TERM_PAIRS]
        return daily.with_columns(term_exprs).select(["symbol", "date", *names])

    def compute(self, ctx: BatchContext) -> pl.DataFrame:
        names = [spec.name for spec in self.declare()]
        minutes = (
            ctx.frame("minute_agg")
            .select(["symbol", "minute"])
            .with_columns(pl.col("minute").dt.date().alias("date"))
        )
        joined = minutes.join(
            self._daily_features(ctx), on=["symbol", "date"], how="left"
        )
        return joined.select(["symbol", "minute", *names])

    def compute_latest(self, ctx: BatchContext) -> pl.DataFrame:
        minute_agg = ctx.frame("minute_agg")
        latest = minute_agg["minute"].max()
        sub = BatchContext(
            frames={
                **ctx.frames,
                "minute_agg": minute_agg.filter(pl.col("minute") == latest),
            }
        )
        return self.compute(sub)
