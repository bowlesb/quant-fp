"""Build LIQUID top-500 universe by median daily dollar-volume from /store/raw/bars.

Dollar volume per (symbol, date) = sum over regular-session minute bars of (close * volume).
Regular session = 13:30-20:00 UTC (09:30-16:00 ET). Median across all session days, top 500.
Writes data/liquid_universe.parquet with symbol, median_dollar_vol, n_days.
"""
from __future__ import annotations

import polars as pl

BARS_GLOB = "/store/raw/bars/symbol=*/date=*/data.parquet"
OUT = "experiments/2026-06-16-w12-net-issuance/data/liquid_universe.parquet"
TOP_N = 500
RTH_START_MIN = 13 * 60 + 30
RTH_END_MIN = 20 * 60


def main() -> None:
    lf = pl.scan_parquet(BARS_GLOB)
    minute_of_day = pl.col("ts").dt.hour().cast(pl.Int32) * 60 + pl.col("ts").dt.minute().cast(pl.Int32)
    lf = lf.with_columns(minute_of_day.alias("mod"), pl.col("ts").dt.date().alias("date"))
    lf = lf.filter((pl.col("mod") >= RTH_START_MIN) & (pl.col("mod") < RTH_END_MIN))
    daily = lf.group_by(["symbol", "date"]).agg(
        (pl.col("close") * pl.col("volume")).sum().alias("dollar_vol")
    )
    per_sym = (
        daily.group_by("symbol")
        .agg(
            pl.col("dollar_vol").median().alias("median_dollar_vol"),
            pl.len().alias("n_days"),
        )
        .filter(pl.col("median_dollar_vol").is_finite() & (pl.col("n_days") >= 200))
    )
    result = per_sym.sort("median_dollar_vol", descending=True).head(TOP_N).collect(engine="streaming")
    result.write_parquet(OUT)
    print(f"Wrote {OUT}: {result.shape[0]} symbols; min median $vol={result['median_dollar_vol'].min():.3e}")


if __name__ == "__main__":
    main()
