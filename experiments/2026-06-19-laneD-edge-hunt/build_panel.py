"""Lane D edge-hunt — PANEL BUILDER (pre-registered, see prereg.md).

Builds a (day, entry-minute, symbol) panel over a multi-year sampled span from the deep `fp_store_real`
minute bars + the Postgres `filings` / `sector_map` tables, with:
  - POINT-IN-TIME EDGAR features (H1): filing burst 7v90, count_7d, mins_since_8k, mins_since_any —
    each using only filings with available_at + EMBARGO <= entry_t (the look-ahead-safe instant + a
    conservative 5-min embargo).
  - POINT-IN-TIME SECTOR features (H2): sector_excess_{15,30,60}, sector_ret_{15,30,60}, abs_sector_beta_30
    — the #182 definitions computed offline from the deep bars + sector_map.
  - MARGINAL CONTROLS: own_rv_30 (own trailing realized vol), mkt_rv_30 (universe turbulence scalar).
  - FORWARD TARGETS: y_ret_{15,30,60}m, y_absret derived, y_fwd_rv, y_fwd_vol (from invent_screen helpers).

Tradeable entries >= 13:35 UTC (09:35 ET). READ-ONLY on stores. Writes panel.parquet for screen.py.
"""

from __future__ import annotations

import datetime as dt
import glob
import os

import numpy as np
import polars as pl

from quantlib.features.loaders import _query

STORE = os.environ.get("STORE_ROOT", "/store")
OUT_DIR = "/app/experiments/2026-06-19-laneD-edge-hunt"

# Tradeable entry minutes (UTC) == 09:35..15:35 ET on the half-hour (mirrors the invention screen).
SAMPLE_MINUTES_UTC = [(13, 35), (14, 35), (15, 35), (16, 35), (17, 35), (18, 35), (19, 35)]
EMBARGO_MIN = 5  # conservative lag on available_at before a filing is "known"
N_SYMBOLS = int(os.environ.get("N_SYMBOLS", "300"))  # liquid cross-section per day
N_DAYS = int(os.environ.get("N_DAYS", "60"))  # sampled trading days across the span
SPAN_START = os.environ.get("SPAN_START", "2018-01-01")
SPAN_END = os.environ.get("SPAN_END", "2025-12-31")
SEED = int(os.environ.get("SEED", "7"))
MIN_DOLLAR_VOL = 50_000.0
MIN_CROSS_SECTION = 20

SECTOR_WINDOWS = (15, 30, 60)
FWD_HORIZONS = (15, 30, 60)
RV_HORIZON = 30
LOOKBACK = 60  # trailing minutes for own_rv + sector returns


def list_bar_days() -> list[str]:
    days = sorted(
        p.split("date=")[1].split("/")[0] for p in glob.glob(f"{STORE}/raw/bars/symbol=SPY/date=*")
    )
    return [d for d in days if SPAN_START <= d <= SPAN_END]


def load_bars_day(date_iso: str, symbols: list[str] | None = None) -> pl.DataFrame:
    pattern = f"{STORE}/raw/bars/symbol=*/date={date_iso}/*.parquet"
    if not glob.glob(pattern):
        return pl.DataFrame()
    lazy = pl.scan_parquet(pattern, hive_partitioning=True).select(["symbol", "ts", "close", "volume"])
    if symbols is not None:
        lazy = lazy.filter(pl.col("symbol").is_in(symbols))
    return lazy.collect()


def pick_liquid_symbols(date_iso: str, n: int) -> list[str]:
    bars = load_bars_day(date_iso)
    if bars.height == 0:
        return []
    mod = pl.col("ts").dt.hour().cast(pl.Int32) * 60 + pl.col("ts").dt.minute().cast(pl.Int32)
    rth = bars.filter((mod >= 13 * 60 + 30) & (mod < 20 * 60))
    dv = (
        rth.with_columns((pl.col("close") * pl.col("volume")).alias("dv"))
        .group_by("symbol")
        .agg(pl.col("dv").sum().alias("dv"))
        .sort("dv", descending=True)
        .head(n)
    )
    return dv["symbol"].to_list()


def entry_minutes(date_iso: str) -> list[dt.datetime]:
    day = dt.date.fromisoformat(date_iso)
    return [
        dt.datetime(day.year, day.month, day.day, hh, mm, tzinfo=dt.timezone.utc)
        for (hh, mm) in SAMPLE_MINUTES_UTC
    ]


def sector_features(
    bars: pl.DataFrame, sector_of: dict[str, str], entries: list[dt.datetime]
) -> pl.DataFrame:
    """sector_excess / sector_ret over each window + abs_sector_beta_30, point-in-time at each entry.

    The sector aggregate is the EW-mean trailing-W return over the day's liquid universe grouped by GICS
    sector (the #182 sector_return definition); sector_excess = own minus that. abs_sector_beta_30 = |OLS
    slope| of own 1m return on its sector's 1m aggregate over the trailing 30m (the #182 sector_beta)."""
    sec_df = pl.DataFrame({"symbol": list(sector_of.keys()), "_sector": list(sector_of.values())})
    bars = bars.join(sec_df, on="symbol", how="inner").sort(["symbol", "ts"])  # drop unmapped names

    # per-(symbol, ts) trailing-W return for each window, time-based lag
    work = bars.select(["symbol", "ts", "close", "_sector"])
    for window in SECTOR_WINDOWS:
        lag = work.select(
            pl.col("symbol"),
            (pl.col("ts") + pl.duration(minutes=window)).alias("ts"),
            pl.col("close").alias(f"_lag{window}"),
        )
        work = work.join(lag, on=["symbol", "ts"], how="left")
    work = work.with_columns(
        [(pl.col("close") / pl.col(f"_lag{w}") - 1.0).alias(f"_ret{w}") for w in SECTOR_WINDOWS]
    )
    # sector EW-mean trailing return per (ts, sector)
    sector_agg = work.group_by(["ts", "_sector"]).agg(
        [pl.col(f"_ret{w}").mean().alias(f"sector_ret_{w}") for w in SECTOR_WINDOWS]
    )
    work = work.join(sector_agg, on=["ts", "_sector"], how="left")

    # 1m returns for the beta (own + sector)
    one = (
        work.select(["symbol", "ts", "close", "_sector"])
        .with_columns(
            pl.col("close").shift(1).over("symbol").alias("_pc"),
            pl.col("ts").shift(1).over("symbol").alias("_pts"),
        )
        .with_columns(
            pl.when((pl.col("ts") - pl.col("_pts")) == pl.duration(minutes=1))
            .then(pl.col("close") / pl.col("_pc") - 1.0)
            .otherwise(None)
            .alias("_oret1")
        )
    )
    sret1 = one.group_by(["ts", "_sector"]).agg(pl.col("_oret1").mean().alias("_sret1"))
    one = one.join(sret1, on=["ts", "_sector"], how="left")

    # rolling 30m OLS beta of own 1m on sector 1m (time-based power sums)
    both = pl.col("_oret1").is_not_null() & pl.col("_sret1").is_not_null()
    one = one.sort(["symbol", "ts"]).with_columns(
        pl.when(both).then(1.0).otherwise(0.0).alias("__one"),
        pl.when(both).then(pl.col("_sret1")).otherwise(0.0).alias("__x"),
        pl.when(both).then(pl.col("_oret1")).otherwise(0.0).alias("__y"),
        pl.when(both).then(pl.col("_sret1") ** 2).otherwise(0.0).alias("__xx"),
        pl.when(both).then(pl.col("_sret1") * pl.col("_oret1")).otherwise(0.0).alias("__xy"),
    )

    def roll(name: str) -> pl.Expr:
        return pl.col(name).rolling_sum_by("ts", window_size="30m").over("symbol")

    n = roll("__one")
    sx, sy, sxx, sxy = roll("__x"), roll("__y"), roll("__xx"), roll("__xy")
    cov = sxy - sx * sy / n
    var_x = sxx - sx * sx / n
    beta = pl.when((n >= 5) & (var_x > 0)).then(cov / var_x).otherwise(None)
    one = one.with_columns(beta.abs().alias("abs_sector_beta_30"))

    work = work.join(one.select(["symbol", "ts", "abs_sector_beta_30"]), on=["symbol", "ts"], how="left")
    work = work.with_columns(
        [
            (pl.col(f"_ret{w}") - pl.col(f"sector_ret_{w}")).alias(f"sector_excess_{w}")
            for w in SECTOR_WINDOWS
        ]
    )

    entry_set = set(entries)
    cols = (
        [f"sector_excess_{w}" for w in SECTOR_WINDOWS]
        + [f"sector_ret_{w}" for w in SECTOR_WINDOWS]
        + [f"_ret{w}" for w in SECTOR_WINDOWS]
        + ["abs_sector_beta_30"]
    )
    return (
        work.filter(pl.col("ts").is_in(entry_set))
        .rename({"ts": "minute"})
        .select(["symbol", "minute", *cols])
    )


def own_rv(bars: pl.DataFrame, entries: list[dt.datetime]) -> pl.DataFrame:
    """own_rv_30 = trailing-30m std of 1m logret, point-in-time at each entry (the marginal control)."""
    b = (
        bars.sort(["symbol", "ts"])
        .with_columns(
            pl.col("close").shift(1).over("symbol").alias("_pc"),
            pl.col("ts").shift(1).over("symbol").alias("_pts"),
        )
        .with_columns(
            pl.when(
                ((pl.col("ts") - pl.col("_pts")) == pl.duration(minutes=1))
                & (pl.col("_pc") > 0)
                & (pl.col("close") > 0)
            )
            .then((pl.col("close") / pl.col("_pc")).log())
            .otherwise(None)
            .alias("_lr")
        )
    )
    rv = b.with_columns(
        pl.col("_lr")
        .rolling_std_by("ts", window_size="30m", min_samples=10)
        .over("symbol")
        .alias("own_rv_30")
    )
    entry_set = set(entries)
    return (
        rv.filter(pl.col("ts").is_in(entry_set))
        .rename({"ts": "minute"})
        .select(["symbol", "minute", "own_rv_30"])
    )


def forward_targets(bars: pl.DataFrame, entries: list[dt.datetime]) -> pl.DataFrame:
    b = (
        bars.sort(["symbol", "ts"])
        .with_columns(
            pl.col("close").shift(1).over("symbol").alias("prev_close"),
            pl.col("ts").shift(1).over("symbol").alias("prev_ts"),
        )
        .with_columns(
            pl.when(
                (pl.col("prev_close") > 0)
                & (pl.col("close") > 0)
                & ((pl.col("ts") - pl.col("prev_ts")) == pl.duration(minutes=1))
            )
            .then((pl.col("close") / pl.col("prev_close")).log())
            .otherwise(None)
            .alias("logret"),
        )
    )
    entry_set = set(entries)
    out = (
        bars.rename({"ts": "minute", "close": "entry_close"})
        .select(["symbol", "minute", "entry_close"])
        .filter(pl.col("minute").is_in(entry_set))
    )
    for horizon in FWD_HORIZONS:
        fwd = (
            bars.with_columns((pl.col("ts") - pl.duration(minutes=horizon)).alias("minute"))
            .rename({"close": "fwd_close"})
            .select(["symbol", "minute", "fwd_close"])
        )
        out = (
            out.join(fwd, on=["symbol", "minute"], how="left")
            .with_columns(((pl.col("fwd_close") / pl.col("entry_close")) - 1.0).alias(f"y_ret_{horizon}m"))
            .drop("fwd_close")
        )
    rframes, vframes = [], []
    for k in range(1, RV_HORIZON + 1):
        rframes.append(
            b.select(["symbol", "ts", "logret"]).with_columns(
                pl.col("ts").dt.offset_by(f"-{k}m").alias("entry")
            )
        )
        vframes.append(
            bars.select(["symbol", "ts", "volume"]).with_columns(
                pl.col("ts").dt.offset_by(f"-{k}m").alias("entry")
            )
        )
    rexp = pl.concat(rframes, how="vertical").filter(pl.col("entry").is_in(entry_set))
    ragg = (
        rexp.group_by(["symbol", "entry"])
        .agg(pl.col("logret").std().alias("y_fwd_rv"), pl.col("logret").count().alias("_n"))
        .with_columns(pl.when(pl.col("_n") >= 10).then(pl.col("y_fwd_rv")).otherwise(None).alias("y_fwd_rv"))
        .drop("_n")
        .rename({"entry": "minute"})
    )
    vexp = pl.concat(vframes, how="vertical").filter(pl.col("entry").is_in(entry_set))
    vagg = (
        vexp.group_by(["symbol", "entry"])
        .agg(pl.col("volume").sum().alias("y_fwd_vol"))
        .rename({"entry": "minute"})
    )
    out = out.join(ragg, on=["symbol", "minute"], how="left").join(vagg, on=["symbol", "minute"], how="left")
    return out.drop("entry_close")


def edgar_features(symbols: list[str], entries: list[dt.datetime]) -> pl.DataFrame:
    """Point-in-time EDGAR features at each entry. One DB pull of all filings for the day's symbols up to
    the last entry, then per (symbol, entry) compute counts/recencies over filings with
    available_at + EMBARGO <= entry."""
    day_end = max(entries)
    window_start = min(entries) - dt.timedelta(days=120)  # cover the 90d baseline + slack
    rows = _query(
        """
        SELECT symbol, form_type, available_at
        FROM filings
        WHERE symbol = ANY(%(syms)s)
          AND available_at >= %(start)s AND available_at <= %(end)s
        """,
        {"syms": symbols, "start": window_start.replace(tzinfo=None), "end": day_end.replace(tzinfo=None)},
    )
    if rows.height == 0:
        # every name has zero filings -> all-zero burst/count, null recencies
        base = pl.DataFrame({"symbol": symbols})
        grid = base.join(pl.DataFrame({"minute": entries}), how="cross")
        return grid.with_columns(
            edgar_cnt_7d=pl.lit(0.0),
            edgar_burst_7v90=pl.lit(0.0),
            mins_since_8k=pl.lit(None, dtype=pl.Float64),
            mins_since_any=pl.lit(None, dtype=pl.Float64),
        )
    rows = rows.with_columns(
        (pl.col("available_at") + pl.duration(minutes=EMBARGO_MIN)).alias("known_at"),
        (pl.col("form_type") == "8-K").alias("is_8k"),
    ).select(["symbol", "known_at", "is_8k"])

    # Vectorized: cross-join the day's (symbol, entry) grid with that symbol's filings, then aggregate the
    # filings known by each entry. Counts of filings are small per symbol over 120d, so this stays cheap.
    grid = pl.DataFrame({"symbol": symbols}).join(pl.DataFrame({"minute": entries}), how="cross")
    joined = grid.join(rows, on="symbol", how="left").filter(
        pl.col("known_at").is_null() | (pl.col("known_at") <= pl.col("minute"))
    )
    cut7 = pl.col("minute") - pl.duration(days=7)
    cut90 = pl.col("minute") - pl.duration(days=90)
    age_min = (pl.col("minute") - pl.col("known_at")).dt.total_seconds() / 60.0
    agg = joined.group_by(["symbol", "minute"]).agg(
        pl.col("known_at").filter(pl.col("known_at") >= cut7).count().alias("edgar_cnt_7d"),
        pl.col("known_at").filter(pl.col("known_at") >= cut90).count().alias("_cnt90"),
        age_min.min().alias("mins_since_any"),
        age_min.filter(pl.col("is_8k")).min().alias("mins_since_8k"),
    )
    return agg.with_columns(
        (
            pl.col("edgar_cnt_7d").cast(pl.Float64) / (pl.col("_cnt90").cast(pl.Float64) / 90.0 * 7.0 + 1.0)
        ).alias("edgar_burst_7v90"),
        pl.col("edgar_cnt_7d").cast(pl.Float64),
    ).select(["symbol", "minute", "edgar_cnt_7d", "edgar_burst_7v90", "mins_since_8k", "mins_since_any"])


def build_day(date_iso: str, sector_of: dict[str, str]) -> pl.DataFrame:
    symbols = pick_liquid_symbols(date_iso, N_SYMBOLS)
    if not symbols:
        return pl.DataFrame()
    bars = load_bars_day(date_iso, symbols)
    if bars.height == 0:
        return pl.DataFrame()
    entries = entry_minutes(date_iso)

    liq = (
        bars.rename({"ts": "minute"})
        .with_columns((pl.col("close") * pl.col("volume")).alias("dv"))
        .filter(pl.col("minute").is_in(entries) & (pl.col("dv") >= MIN_DOLLAR_VOL))
        .select(["symbol", "minute"])
    )
    if liq.height == 0:
        return pl.DataFrame()

    secf = sector_features(bars, sector_of, entries)
    ownrv = own_rv(bars, entries)
    mkt = ownrv.group_by("minute").agg(pl.col("own_rv_30").mean().alias("mkt_rv_30"))
    edg = edgar_features(symbols, entries)
    tgt = forward_targets(bars, entries)

    panel = (
        liq.join(secf, on=["symbol", "minute"], how="left")
        .join(ownrv, on=["symbol", "minute"], how="left")
        .join(mkt, on="minute", how="left")
        .join(edg, on=["symbol", "minute"], how="left")
        .join(tgt, on=["symbol", "minute"], how="left")
    )
    return panel.with_columns(pl.lit(date_iso).alias("date"))


def main() -> None:
    days = list_bar_days()
    rng = np.random.default_rng(SEED)
    if len(days) > N_DAYS:
        idx = np.sort(rng.choice(len(days), size=N_DAYS, replace=False))
        days = [days[i] for i in idx]
    ref = _query("SELECT symbol, sector FROM sector_map WHERE sector IS NOT NULL", {})
    sector_of = {row["symbol"]: row["sector"].lower().replace(" ", "_") for row in ref.iter_rows(named=True)}
    print(f"days={len(days)} span={days[0]}..{days[-1]} sector_map={len(sector_of)} syms", flush=True)
    frames = []
    for i, day in enumerate(days):
        panel = build_day(day, sector_of)
        if panel.height:
            frames.append(panel)
        print(f"[{i+1}/{len(days)}] {day}: {panel.height} rows", flush=True)
    full = pl.concat(frames, how="vertical_relaxed")
    out = f"{OUT_DIR}/panel.parquet"
    full.write_parquet(out)
    print(
        f"WROTE {out}: {full.height} rows, {full['date'].n_unique()} days, {full['symbol'].n_unique()} symbols",
        flush=True,
    )


if __name__ == "__main__":
    main()
