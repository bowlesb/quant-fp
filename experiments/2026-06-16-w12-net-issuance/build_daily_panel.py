"""Build a daily (symbol, date, close) panel for the universe from /store/raw/bars.

Daily close = the close of the last regular-session bar (13:30-20:00 UTC) on each date.
Restrict to the universe symbols (symbol_cik.parquet). Writes data/daily_panel.parquet.
"""
from __future__ import annotations

import polars as pl

OUT = "experiments/2026-06-16-w12-net-issuance/data/daily_panel.parquet"
SYMBOL_CIK = "experiments/2026-06-16-w12-net-issuance/data/symbol_cik.parquet"
RTH_START_MIN = 13 * 60 + 30
RTH_END_MIN = 20 * 60


def main() -> None:
    syms = pl.read_parquet(SYMBOL_CIK)["symbol"].to_list()
    globs = [f"/store/raw/bars/symbol={s}/date=*/data.parquet" for s in syms]
    lf = pl.scan_parquet(globs)
    minute_of_day = pl.col("ts").dt.hour().cast(pl.Int32) * 60 + pl.col("ts").dt.minute().cast(pl.Int32)
    lf = lf.with_columns(minute_of_day.alias("mod"), pl.col("ts").dt.date().alias("date"))
    lf = lf.filter((pl.col("mod") >= RTH_START_MIN) & (pl.col("mod") < RTH_END_MIN))
    daily = (
        lf.sort("ts")
        .group_by(["symbol", "date"])
        .agg(pl.col("close").last().alias("close"))
        .filter(pl.col("close").is_finite() & (pl.col("close") > 0))
    )
    result = daily.sort(["symbol", "date"]).collect(engine="streaming")
    result.write_parquet(OUT)
    print(f"Wrote {OUT}: {result.shape}; symbols={result['symbol'].n_unique()} {result['date'].min()}..{result['date'].max()}")


if __name__ == "__main__":
    main()
