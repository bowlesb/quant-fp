"""Build a cross-sectional CLOSE->NEXT-OPEN overnight panel from the 18-month RAW MINUTE BARS.

Pre-registered in hypothesis.md (Lane C — scope/horizon). The computed feature STORE is only
46 days deep; the deep history lives in the raw bars, so X is computed POINT-IN-TIME from the
raw DAILY bars here (no look-ahead), giving an ~18-month, ~7600-symbol cross-sectional panel.

Two stages:
  1. DAILY REDUCTION (memory-bounded, one date at a time): for each trading day, glob-scan the
     raw minute bars and reduce to one row per symbol with the RTH-session daily aggregates plus
     the two tradeable execution prices we need:
       - rth_open   = close of the 13:30 UTC (09:30 ET) minute   [for ret_1d intraday]
       - rth_close  = close of the 19:59 UTC (15:59 ET) minute   [ENTRY price / daily close]
       - exec_0935  = close of the 13:35 UTC (09:35 ET) minute   [tradeable OPEN, used as the
                      NEXT day's overnight EXIT for the prior day]
       - rth_high/rth_low/rth_volume/rth_dollar_vol over the RTH window 13:30..19:59 UTC
     Written to a compact daily parquet (one row per symbol/day) -> small, reused across features.
  2. PANEL: sort the daily table by (symbol, date), compute the pre-registered trailing EOD
     features with polars window expressions (point-in-time, >=21 trailing days required), attach
     the close->next-open label (entry=rth_close_d, exit=exec_0935_{d+1}), cross-sectional excess
     vs the per-day median over the enterable universe, and the multi-day (2d/3d) forward holds.

Tradeable discipline (hypothesis.md): entry = day-d 15:59 close (MOC-fillable), exit = next-day
09:35 close (NOT the 09:30 print — the gap-fade look-ahead trap). Liquidity floor $50k at entry.

Env:
  STORE_ROOT   /store
  OUT          /app/experiments/data/overnight_panel.parquet
  DAILY_OUT    /app/experiments/data/overnight_daily.parquet  (stage-1 cache; reused if present)
  UNIVERSE_TOP if set (int), keep only the top-N symbols by mean daily dollar-vol (smoke mode)
  MAX_DATES    if set (int), only the most recent N trading days (smoke mode)
  REBUILD_DAILY if "1", force re-reduction even if DAILY_OUT exists
"""
from __future__ import annotations

import glob
import os

import polars as pl

STORE = os.environ.get("STORE_ROOT", "/store")
OUT = os.environ.get("OUT", "/app/experiments/data/overnight_panel.parquet")
DAILY_OUT = os.environ.get("DAILY_OUT", "/app/experiments/data/overnight_daily.parquet")

# RTH boundary minutes in UTC (the bars are tz-aware UTC; ET RTH 09:30-16:00 == 13:30-20:00 UTC).
OPEN_HM = 1330       # 09:30 ET open print minute
EXEC_HM = 1335       # 09:35 ET first fillable minute (tradeable open / overnight EXIT)
CLOSE_HM = 1959      # 15:59 ET last RTH minute (daily close / overnight ENTRY)
RTH_START_HM = 1330
RTH_END_HM = 1959

MIN_DOLLAR_VOL = 50_000.0          # entry-minute liquidity floor (matches the intraday builder)
MIN_PRICE = 1.0                    # PRICE-INTEGRITY floor on BOTH legs: sub-$1 bars are dominated
                                   # by bad/sub-penny 15:59 prints (raw overnight ratios of 50-226x)
                                   # and are not realistically tradeable overnight. Standard
                                   # penny-stock exclusion; symmetric -> cannot manufacture
                                   # directional signal. See results.md "data integrity".
WINSOR_Q = 0.005                   # per-day raw-return winsorization [0.5%, 99.5%] before the
                                   # cross-sectional median (kills residual bad-print tails;
                                   # symmetric, applied to the RAW return pre-excess).
MIN_CROSS_SECTION = 50             # per-day breadth floor for the cross-sectional median
MIN_TRAILING_DAYS = 21             # warmup: need >=21 trailing daily bars for the 20d features
FWD_HORIZONS = [1, 2, 3]           # overnight (1d) headline + 2d/3d descriptive by-horizon holds


def all_dates() -> list[str]:
    """Sorted trading dates present in the raw bar store (union across symbols via a cheap glob)."""
    dates = {
        os.path.basename(path).replace("date=", "")
        for path in glob.glob(f"{STORE}/raw/bars/symbol=*/date=*")
    }
    return sorted(dates)


def reduce_one_date(date_iso: str) -> pl.DataFrame | None:
    """One row per symbol for date_iso: RTH daily aggregates + the three execution-minute prices."""
    pattern = f"{STORE}/raw/bars/symbol=*/date={date_iso}/*.parquet"
    if not glob.glob(pattern):
        return None
    bars = (
        pl.scan_parquet(pattern, hive_partitioning=True)
        .select(["symbol", "ts", "open", "high", "low", "close", "volume"])
        .with_columns(
            (pl.col("ts").dt.hour().cast(pl.Int32) * 100
             + pl.col("ts").dt.minute().cast(pl.Int32)).alias("hm")
        )
        .filter((pl.col("hm") >= RTH_START_HM) & (pl.col("hm") <= RTH_END_HM))
        .collect()
    )
    if bars.height == 0:
        return None
    agg = bars.group_by("symbol").agg(
        pl.col("high").max().alias("rth_high"),
        pl.col("low").min().alias("rth_low"),
        pl.col("volume").sum().alias("rth_volume"),
        (pl.col("close") * pl.col("volume")).sum().alias("rth_dollar_vol"),
        pl.col("close").filter(pl.col("hm") == OPEN_HM).first().alias("rth_open"),
        pl.col("close").filter(pl.col("hm") == CLOSE_HM).first().alias("rth_close"),
        pl.col("close").filter(pl.col("hm") == EXEC_HM).first().alias("exec_0935"),
    )
    return agg.with_columns(pl.lit(date_iso).alias("date"))


def build_daily_table(dates: list[str]) -> pl.DataFrame:
    """Stage 1: reduce every date to the compact daily table (cached to DAILY_OUT)."""
    rows: list[pl.DataFrame] = []
    for date_iso in dates:
        reduced = reduce_one_date(date_iso)
        if reduced is None:
            print(f"  {date_iso}: SKIP (no bars)")
            continue
        rows.append(reduced)
        print(f"  {date_iso}: symbols={reduced.height}")
    daily = pl.concat(rows, how="vertical_relaxed")
    os.makedirs(os.path.dirname(DAILY_OUT), exist_ok=True)
    daily.write_parquet(DAILY_OUT)
    print(f"\nWROTE daily table {DAILY_OUT}: shape={daily.shape}")
    return daily


def apply_universe_cap(daily: pl.DataFrame) -> pl.DataFrame:
    """Smoke mode: keep only the top-N symbols by mean daily dollar volume."""
    top = os.environ.get("UNIVERSE_TOP")
    if not top:
        return daily
    n = int(top)
    keep = (
        daily.group_by("symbol")
        .agg(pl.col("rth_dollar_vol").mean().alias("adv"))
        .sort("adv", descending=True)
        .head(n)["symbol"]
    )
    print(f"UNIVERSE_TOP={n}: keeping {keep.len()} symbols by ADV")
    return daily.filter(pl.col("symbol").is_in(keep))


def compute_features_and_label(daily: pl.DataFrame) -> pl.DataFrame:
    """Stage 2: per-symbol trailing EOD features (point-in-time) + close->next-open label."""
    daily = daily.sort(["symbol", "date"])
    over = pl.col("symbol")  # window partition

    feat = daily.with_columns(
        # --- intraday / overnight building blocks ---
        (pl.col("rth_close") / pl.col("rth_open") - 1.0).alias("ret_1d"),
        (pl.col("rth_close") / pl.col("rth_close").shift(1).over(over) - 1.0).alias("ret_co_1d"),
        (pl.col("rth_open") / pl.col("rth_close").shift(1).over(over) - 1.0).alias("overnight_prev"),
        (pl.col("rth_close").shift(1).over(over)
         / pl.col("rth_open").shift(1).over(over) - 1.0).alias("intraday_prev"),
        # --- multi-day momentum ---
        *[
            (pl.col("rth_close") / pl.col("rth_close").shift(k).over(over) - 1.0).alias(f"ret_{k}d")
            for k in [2, 5, 10, 20]
        ],
        # --- liquidity / size ---
        (pl.col("rth_dollar_vol").rolling_mean(window_size=20).over(over) + 1.0)
        .log().alias("dollar_vol_20d"),
        # --- range position ---
        ((pl.col("rth_close") - pl.col("rth_low").rolling_min(20).over(over))
         / (pl.col("rth_high").rolling_max(20).over(over)
            - pl.col("rth_low").rolling_min(20).over(over))).alias("range_20d_pos"),
    )
    # realized vol from daily close-close returns + a z-scored close level (need ret_co_1d first)
    feat = feat.with_columns(
        pl.col("ret_co_1d").rolling_std(window_size=5).over(over).alias("rvol_5d"),
        pl.col("ret_co_1d").rolling_std(window_size=20).over(over).alias("rvol_20d"),
        ((pl.col("rth_close") - pl.col("rth_close").rolling_mean(20).over(over))
         / pl.col("rth_close").rolling_std(20).over(over)).alias("gap_z"),
        # row index within symbol (for the warmup floor)
        pl.col("date").cum_count().over(over).alias("bar_idx"),
    )

    # --- LABEL: close_d -> exec_0935_{d+1..d+k} overnight/multi-day forward, point-in-time ---
    # PRICE-INTEGRITY: null the raw return unless BOTH legs (entry close_d, exit exec_{d+k}) are
    # >= MIN_PRICE -- sub-$1 bars are dominated by bad/sub-penny 15:59 prints (see results.md).
    for k in FWD_HORIZONS:
        exit_price = pl.col("exec_0935").shift(-k).over(over)
        feat = feat.with_columns(
            pl.when((pl.col("rth_close") >= MIN_PRICE) & (exit_price >= MIN_PRICE))
            .then(exit_price / pl.col("rth_close") - 1.0)
            .otherwise(None)
            .alias(f"fwd_{k}d_raw")
        )
    return feat


def cross_sectional_excess(frame: pl.DataFrame, raw_col: str, excess_col: str) -> pl.DataFrame:
    """Per-day winsorize the RAW return at [WINSOR_Q, 1-WINSOR_Q] (symmetric, kills residual
    bad-print tails), then subtract the per-day cross-sectional MEDIAN; null the day if breadth
    < floor. Winsorization is on the RAW return PRE-excess -> cannot inject directional signal."""
    bounds = frame.group_by("date").agg(
        pl.col(raw_col).quantile(WINSOR_Q).alias("lo"),
        pl.col(raw_col).quantile(1.0 - WINSOR_Q).alias("hi"),
        pl.col(raw_col).count().alias("n"),
    )
    out = frame.join(bounds, on="date")
    out = out.with_columns(
        pl.col(raw_col).clip(lower_bound=pl.col("lo"), upper_bound=pl.col("hi")).alias("clipped")
    )
    med = out.group_by("date").agg(pl.col("clipped").median().alias("med"))
    out = out.join(med, on="date")
    return out.with_columns(
        pl.when(pl.col("n") >= MIN_CROSS_SECTION)
        .then(pl.col("clipped") - pl.col("med"))
        .otherwise(None)
        .alias(excess_col)
    ).drop(["lo", "hi", "n", "clipped", "med"])


FEATURE_COLS = [
    "ret_1d", "ret_co_1d", "overnight_prev", "intraday_prev",
    "ret_2d", "ret_5d", "ret_10d", "ret_20d",
    "rvol_5d", "rvol_20d", "dollar_vol_20d", "gap_z", "range_20d_pos",
]


def main() -> None:
    dates = all_dates()
    max_dates = os.environ.get("MAX_DATES")
    if max_dates:
        dates = dates[-int(max_dates):]
    print(f"trading dates: {len(dates)}  {dates[0]}..{dates[-1]}")

    if os.path.exists(DAILY_OUT) and os.environ.get("REBUILD_DAILY") != "1":
        daily = pl.read_parquet(DAILY_OUT)
        print(f"reusing cached daily table {DAILY_OUT}: shape={daily.shape}")
        if max_dates:
            daily = daily.filter(pl.col("date").is_in(set(dates)))
    else:
        daily = build_daily_table(dates)

    daily = apply_universe_cap(daily)
    feat = compute_features_and_label(daily)

    # warmup floor + liquidity floor + price-integrity floor at the entry minute
    feat = feat.filter(
        (pl.col("bar_idx") >= MIN_TRAILING_DAYS)
        & ((pl.col("rth_close") * pl.col("rth_volume")) >= MIN_DOLLAR_VOL)
        & (pl.col("rth_close") >= MIN_PRICE)
    )

    # cross-sectional excess label per horizon, drop rows with no usable feature/label
    for k in FWD_HORIZONS:
        feat = cross_sectional_excess(feat, f"fwd_{k}d_raw", f"fwd_{k}d")

    # convert the day-d close datetime into the harness "timestamp" (one per day): use 19:59 UTC.
    feat = feat.with_columns(
        (pl.col("date").str.to_datetime("%Y-%m-%d", time_zone="UTC")
         + pl.duration(hours=19, minutes=59)).alias("minute")
    )

    keep_cols = ["symbol", "minute", "date"] + FEATURE_COLS + [f"fwd_{k}d" for k in FWD_HORIZONS]
    panel = feat.select(keep_cols)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    panel.write_parquet(OUT)
    print(f"\nWROTE {OUT}: shape={panel.shape}")
    print(f"feature columns in X: {len(FEATURE_COLS)}  {FEATURE_COLS}")
    for k in FWD_HORIZONS:
        labelled = panel.filter(pl.col(f"fwd_{k}d").is_not_null())
        print(f"  fwd_{k}d: rows_with_label={labelled.height} "
              f"days={labelled['date'].n_unique()} symbols={labelled['symbol'].n_unique()}")


if __name__ == "__main__":
    main()
