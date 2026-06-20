"""The shared, load-ONCE cross-sectional `Panel` — the data substrate every battery
archetype evaluates over.

Layout (the §3.4 design contract): a column-major table sorted by `(symbol_code, minute)`
where `symbol_code` is a contiguous integer block per symbol — the SAME ordering every
`rust/src/lib.rs` kernel (`windowed_reduce`, `rolling_extrema`) already assumes, so the
Phase-1 Rust `first_touch` kernel drops in over the resident arrays with zero conversion.

Two build modes, both reusing the PROVEN reduce->panel patterns from the hand-rolled
harnesses:

  - ``build_daily_panel``  — one row per (symbol, trading-day): RTH daily aggregates + the
    three tradeable execution prices (open / 15:59 close / next-day 09:35) + per-name
    trailing features. This is the laneC ``build_overnight_dataset`` two-stage reduce, the
    substrate for the EOD / overnight / multi-day horizons. Memory-bounded: one date at a
    time. The daily-reduced grid is small (~7700 sym x ~250 day), which is what makes the
    whole battery fit the <30-60s budget for those horizons.

  - ``build_intraday_panel`` — (symbol, sampled-minute) at a fixed RTH cadence with the
    trusted point-in-time features joined as-of the minute, the per-name `spread_bps`
    half-spread, and forward-return labels at the requested minute horizons. This is the
    ``build_dataset`` intraday pattern, restricted to a liquid universe slice (the only
    tradeable universe anyway, per trap #1).

Discipline baked in here (so no archetype re-rolls it): tradeable entry >= 09:35 ET, the
$1 price-integrity floor on BOTH legs, the liquidity floor at entry, and the per-name
half-spread carried as a column for the realistic cost model.

The `Panel` itself is label-agnostic: it carries features + the execution prices + the
cost column; each `Strategy` derives its own forward/path label from the SAME resident
arrays (a shifted self-join for vectorizable labels; the Rust forward scan in Phase 1).

Known caveats (documented, lower-severity — audit 2026-06-19):
  - SURVIVORSHIP / universe look-ahead: the liquid-universe cut (`universe_top` by mean ADV) is
    computed over the WHOLE date range, so a name must survive the range to be selected — the same
    survivorship caveat the B4/laneC findings carry. A point-in-time-as-of-each-day universe is the
    correct fix; until then, liquid-cut results are mildly survivorship-optimistic (acceptable for the
    null/HIT-direction verdicts the battery reproduces, NOT for a deployable liquid-only edge claim).
"""
from __future__ import annotations

import datetime as dt
import glob
import os
from dataclasses import dataclass, field

import numpy as np
import polars as pl

STORE = os.environ.get("STORE_ROOT", "/store")

# RTH boundary minutes in UTC (bars are tz-aware UTC; ET RTH 09:30-16:00 == 13:30-20:00 UTC).
OPEN_HM = 1330  # 09:30 ET open print
EXEC_HM = 1335  # 09:35 ET first fillable minute (tradeable open / overnight EXIT)
CLOSE_HM = 1959  # 15:59 ET last RTH minute (daily close / overnight ENTRY)
RTH_START_HM = 1330
RTH_END_HM = 1959

# Intraday tradeable-entry sampling: 30-min cadence, 13:35..19:35 UTC (09:35..15:35 ET), all
# >= 09:35 ET so the entry is fillable (never the 09:30 print — the gap-fade look-ahead trap).
INTRADAY_SAMPLE_MINUTES_UTC = [
    (13, 35),
    (14, 5),
    (14, 35),
    (15, 5),
    (15, 35),
    (16, 5),
    (16, 35),
    (17, 5),
    (17, 35),
    (18, 5),
    (18, 35),
    (19, 5),
    (19, 35),
]

MIN_DOLLAR_VOL = 50_000.0  # entry-minute liquidity floor (enterable)
MIN_PRICE = 1.0  # $1 price-integrity floor on BOTH legs (penny-print exclusion)
MIN_TRAILING_DAYS = 21  # warmup for the 20d trailing daily features
# A spread fallback (bps, one-way half-spread) when the order-flow spread column is absent for a
# name/day. Conservative-ish liquid-name default; the per-name spread column overrides it where present.
DEFAULT_HALF_SPREAD_BPS = 3.0


@dataclass
class Panel:
    """Column-major resident arrays, sorted by (symbol_code, minute_epoch).

    `feature_matrix` is (n_rows, n_features) float64. `symbol_code` is a contiguous-per-symbol
    integer block (the Rust-kernel ordering). `minute_epoch` is int64 ns since epoch. The
    execution-price columns (`entry_close`, plus daily `exec_0935`/`rth_open` or the per-minute
    forward `close`) let each Strategy build its own label without re-reading the store.
    """

    symbol_code: np.ndarray  # int64, contiguous block per symbol
    symbol_names: list[str]  # symbol_code -> ticker
    minute_epoch: np.ndarray  # int64 ns since epoch (sorted within each symbol block)
    feature_names: list[str]
    feature_matrix: np.ndarray  # (n_rows, n_features) float64
    entry_close: np.ndarray  # float64 — the tradeable entry price at this row
    half_spread_bps: np.ndarray  # float64 — per-name one-way half-spread (the realistic cost)
    high: np.ndarray  # float64 — forward-path high (daily RTH high / minute bar high)
    low: np.ndarray  # float64 — forward-path low
    volume: np.ndarray  # float64
    # mode-specific execution columns (one is populated per build mode)
    extra: dict[str, np.ndarray] = field(default_factory=dict)
    cadence: str = "daily"  # "daily" or "intraday"
    sector: np.ndarray | None = None  # optional per-row sector label (str codes) for stratification

    @property
    def n_rows(self) -> int:
        return int(self.feature_matrix.shape[0])

    @property
    def minute_dt(self) -> list[dt.datetime]:
        """minute_epoch as tz-aware UTC datetimes (what walk_forward_folds consumes)."""
        return [dt.datetime.fromtimestamp(int(ns) / 1e9, tz=dt.timezone.utc) for ns in self.minute_epoch]

    def feature_index(self, name: str) -> int:
        return self.feature_names.index(name)


def _all_raw_dates() -> list[str]:
    dates = {
        os.path.basename(path).replace("date=", "")
        for path in glob.glob(f"{STORE}/raw/bars/symbol=*/date=*")
    }
    return sorted(dates)


def _reduce_one_date(date_iso: str) -> pl.DataFrame | None:
    """One row per symbol for date_iso: RTH daily aggregates + the three execution-minute prices.
    The laneC stage-1 reduce, verbatim in shape."""
    pattern = f"{STORE}/raw/bars/symbol=*/date={date_iso}/*.parquet"
    if not glob.glob(pattern):
        return None
    bars = (
        pl.scan_parquet(pattern, hive_partitioning=True)
        .select(["symbol", "ts", "open", "high", "low", "close", "volume"])
        .with_columns(
            (pl.col("ts").dt.hour().cast(pl.Int32) * 100 + pl.col("ts").dt.minute().cast(pl.Int32)).alias(
                "hm"
            )
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


def build_daily_table(dates: list[str], daily_cache: str | None) -> pl.DataFrame:
    """Stage 1: reduce every date to the compact daily table (one row per symbol/day).
    Cached to `daily_cache` and reused if present (the expensive raw-glob pass runs once)."""
    if daily_cache and os.path.exists(daily_cache):
        cached = pl.read_parquet(daily_cache)
        return cached.filter(pl.col("date").is_in(set(dates)))
    rows: list[pl.DataFrame] = []
    for date_iso in dates:
        reduced = _reduce_one_date(date_iso)
        if reduced is not None:
            rows.append(reduced)
    daily = pl.concat(rows, how="vertical_relaxed")
    if daily_cache:
        os.makedirs(os.path.dirname(daily_cache), exist_ok=True)
        daily.write_parquet(daily_cache)
    return daily


DAILY_FEATURE_COLS = [
    "ret_1d",
    "ret_co_1d",
    "overnight_prev",
    "intraday_prev",
    "ret_2d",
    "ret_5d",
    "ret_10d",
    "ret_20d",
    "rvol_5d",
    "rvol_20d",
    "dollar_vol_20d",
    "gap_z",
    "range_20d_pos",
]


def _compute_daily_features(daily: pl.DataFrame) -> pl.DataFrame:
    """Per-symbol trailing EOD features (point-in-time) — the laneC ``compute_features`` block.
    Also derives the per-name half-spread proxy + an up/down-market-day regime flag (cross-sectional
    median ret_1d sign), both as-of the row's day (no look-ahead)."""
    daily = daily.sort(["symbol", "date"])
    over = pl.col("symbol")
    feat = daily.with_columns(
        (pl.col("rth_close") / pl.col("rth_open") - 1.0).alias("ret_1d"),
        (pl.col("rth_close") / pl.col("rth_close").shift(1).over(over) - 1.0).alias("ret_co_1d"),
        (pl.col("rth_open") / pl.col("rth_close").shift(1).over(over) - 1.0).alias("overnight_prev"),
        (pl.col("rth_close").shift(1).over(over) / pl.col("rth_open").shift(1).over(over) - 1.0).alias(
            "intraday_prev"
        ),
        *[
            (pl.col("rth_close") / pl.col("rth_close").shift(k).over(over) - 1.0).alias(f"ret_{k}d")
            for k in [2, 5, 10, 20]
        ],
        (pl.col("rth_dollar_vol").rolling_mean(window_size=20).over(over) + 1.0)
        .log()
        .alias("dollar_vol_20d"),
        (
            (pl.col("rth_close") - pl.col("rth_low").rolling_min(20).over(over))
            / (pl.col("rth_high").rolling_max(20).over(over) - pl.col("rth_low").rolling_min(20).over(over))
        ).alias("range_20d_pos"),
    )
    feat = feat.with_columns(
        pl.col("ret_co_1d").rolling_std(window_size=5).over(over).alias("rvol_5d"),
        pl.col("ret_co_1d").rolling_std(window_size=20).over(over).alias("rvol_20d"),
        (
            (pl.col("rth_close") - pl.col("rth_close").rolling_mean(20).over(over))
            / pl.col("rth_close").rolling_std(20).over(over)
        ).alias("gap_z"),
        pl.col("date").cum_count().over(over).alias("bar_idx"),
    )
    # half-spread proxy: bigger names trade tighter. Map log10(ADV) in [4..9] -> [12..1.5] bps.
    # A coarse monotone proxy; the intraday panel uses the REAL spread column where present.
    feat = feat.with_columns(
        (12.0 - 2.1 * (pl.col("dollar_vol_20d") / 2.302585 - 4.0).clip(0.0, 5.0))
        .clip(1.5, 25.0)
        .alias("half_spread_bps")
    )
    # up/down-market-day regime: sign of the per-day cross-sectional MEDIAN ret_1d (as-of the day).
    med = feat.group_by("date").agg(pl.col("ret_1d").median().alias("_mkt_med"))
    feat = (
        feat.join(med, on="date")
        .with_columns((pl.col("_mkt_med") >= 0).alias("up_market_day"))
        .drop("_mkt_med")
    )
    # FORWARD exit prices for the overnight / 2d / 3d labels — computed on the FULL per-symbol daily
    # grid HERE (before the liquidity / warmup filter), so a day dropped downstream cannot corrupt a
    # naive post-filter row-shift. Each is the symbol's price k trading days AHEAD (NaN at the tail).
    feat = feat.with_columns(
        pl.col("exec_0935").shift(-1).over(over).alias("exit_overnight"),  # next-day 09:35
        pl.col("rth_close").shift(-2).over(over).alias("exit_2d"),  # close 2 days ahead
        pl.col("rth_close").shift(-3).over(over).alias("exit_3d"),  # close 3 days ahead
    )
    return feat


def build_daily_panel(
    date_range: tuple[str, str],
    *,
    universe_top: int | None = None,
    daily_cache: str | None = None,
) -> pl.DataFrame:
    """The daily-reduced (symbol, day) panel with trailing features + execution prices + the
    half-spread column + the up/down-market regime flag, ready for the EOD/overnight/multi-day
    archetypes. Returns a polars frame; ``panel_from_daily_frame`` turns it into a `Panel` for a
    specific horizon. Liquidity + $1 floors applied at the entry (close) row."""
    start, end = date_range
    dates = [d for d in _all_raw_dates() if start <= d <= end]
    daily = build_daily_table(dates, daily_cache)
    if universe_top:
        adv = (
            daily.group_by("symbol")
            .agg(pl.col("rth_dollar_vol").mean().alias("adv"))
            .sort("adv", descending=True)
            .head(universe_top)["symbol"]
        )
        daily = daily.filter(pl.col("symbol").is_in(adv))
    feat = _compute_daily_features(daily)
    feat = feat.filter(
        (pl.col("bar_idx") >= MIN_TRAILING_DAYS)
        & ((pl.col("rth_close") * pl.col("rth_volume")) >= MIN_DOLLAR_VOL)
        & (pl.col("rth_close") >= MIN_PRICE)
    )
    feat = feat.with_columns(
        (
            pl.col("date").str.to_datetime("%Y-%m-%d", time_zone="UTC") + pl.duration(hours=19, minutes=59)
        ).alias("minute")
    )
    return feat


def panel_from_daily_frame(feat: pl.DataFrame) -> Panel:
    """Materialize a daily polars frame into the column-major `Panel` (sorted by symbol, minute).
    Carries `exec_0935` and `rth_open` in `extra` so the overnight/EOD labels read them directly."""
    feat = feat.sort(["symbol", "minute"])
    symbols = feat["symbol"].to_list()
    uniq = sorted(set(symbols))
    sym_to_idx = {sym: i for i, sym in enumerate(uniq)}
    symbol_code = np.array([sym_to_idx[s] for s in symbols], dtype=np.int64)
    minute_epoch = feat["minute"].dt.timestamp("ns").to_numpy().astype(np.int64)
    feature_matrix = feat.select(DAILY_FEATURE_COLS).to_numpy().astype(float)
    # exec_0935 shifted forward per symbol gives the overnight exit; keep raw cols for k-day shifts.
    extra = {
        "rth_open": feat["rth_open"].to_numpy().astype(float),
        "exec_0935": feat["exec_0935"].to_numpy().astype(float),
        "rth_close": feat["rth_close"].to_numpy().astype(float),
        "rth_dollar_vol": feat["rth_dollar_vol"].to_numpy().astype(float),
        # forward exit prices computed on the FULL daily grid (gap-safe), carried for the labels
        "exit_overnight": feat["exit_overnight"].to_numpy().astype(float),
        "exit_2d": feat["exit_2d"].to_numpy().astype(float),
        "exit_3d": feat["exit_3d"].to_numpy().astype(float),
    }
    return Panel(
        symbol_code=symbol_code,
        symbol_names=uniq,
        minute_epoch=minute_epoch,
        feature_names=list(DAILY_FEATURE_COLS),
        feature_matrix=feature_matrix,
        entry_close=feat["rth_close"].to_numpy().astype(float),
        half_spread_bps=feat["half_spread_bps"].to_numpy().astype(float),
        high=feat["rth_high"].to_numpy().astype(float),
        low=feat["rth_low"].to_numpy().astype(float),
        volume=feat["rth_volume"].to_numpy().astype(float),
        extra=extra,
        cadence="daily",
        sector=None,
    )


def _group_version_dir(group: str) -> str | None:
    dirs = sorted(glob.glob(f"{STORE}/group={group}/v=*"))
    return dirs[-1] if dirs else None


def _load_features_for_date(date_iso: str, groups: dict[str, list[str]]) -> pl.DataFrame | None:
    """Wide (symbol, minute, <features>) for one date, inner-joined across the requested groups.
    The point-in-time store read from ``build_dataset.load_features_for_date``."""
    panel: pl.DataFrame | None = None
    for group, feats in groups.items():
        vdir = _group_version_dir(group)
        if vdir is None:
            return None
        files = sorted(glob.glob(f"{vdir}/source=backfill/date={date_iso}/data*.parquet"))
        if not files:
            return None
        cols = ["symbol", "minute"] + feats
        frame = pl.concat([pl.read_parquet(path, columns=cols) for path in files])
        panel = frame if panel is None else panel.join(frame, on=["symbol", "minute"], how="inner")
    return panel


def _load_bars_for_date(date_iso: str) -> pl.DataFrame:
    pattern = f"{STORE}/raw/bars/symbol=*/date={date_iso}/*.parquet"
    if not glob.glob(pattern):
        return pl.DataFrame(
            schema={
                "symbol": pl.String,
                "ts": pl.Datetime("us", "UTC"),
                "high": pl.Float64,
                "low": pl.Float64,
                "close": pl.Float64,
                "volume": pl.Int64,
            }
        )
    return (
        pl.scan_parquet(pattern, hive_partitioning=True)
        .select(["symbol", "ts", "high", "low", "close", "volume"])
        .collect()
    )


def _load_spread_for_date(date_iso: str, spread_col: str) -> pl.DataFrame | None:
    """The per-(symbol, minute) order-flow half-spread (bps) for the cost model, if the
    quote_spread group covers this date. Returns None when absent (caller falls back)."""
    vdir = _group_version_dir("quote_spread")
    if vdir is None:
        return None
    files = sorted(glob.glob(f"{vdir}/source=backfill/date={date_iso}/data*.parquet"))
    if not files:
        return None
    frames = [pl.read_parquet(path, columns=["symbol", "minute", spread_col]) for path in files]
    return pl.concat(frames)


def build_intraday_panel(
    date_range: tuple[str, str],
    *,
    feature_groups: dict[str, list[str]],
    horizons_min: list[int],
    universe_top: int | None = None,
    spread_col: str = "spread_bps_30m",
) -> pl.DataFrame:
    """The (symbol, sampled-minute) intraday panel: trusted features joined as-of the minute,
    forward cross-sectional EXCESS-return labels at each horizon, the per-name half-spread, all
    at a tradeable entry >= 09:35 ET. The ``build_dataset`` intraday pattern, one date at a time."""
    start, end = date_range
    rep_group = next(iter(feature_groups))
    rep_vdir = _group_version_dir(rep_group)
    if rep_vdir is None:
        raise FileNotFoundError(f"no store version dir for group {rep_group}")
    dates = sorted(
        os.path.basename(p).replace("date=", "")
        for p in glob.glob(f"{rep_vdir}/source=backfill/date=*")
        if start <= os.path.basename(p).replace("date=", "") <= end
    )
    panels: list[pl.DataFrame] = []
    for date_iso in dates:
        built = _build_intraday_date(date_iso, feature_groups, horizons_min, spread_col)
        if built is not None:
            panels.append(built)
    if not panels:
        raise ValueError(f"no intraday panel rows in {date_range} for groups {list(feature_groups)}")
    full = pl.concat(panels, how="vertical_relaxed")
    if universe_top:
        adv = (
            full.group_by("symbol")
            .agg(pl.col("entry_dollar_vol").mean().alias("adv"))
            .sort("adv", descending=True)
            .head(universe_top)["symbol"]
        )
        full = full.filter(pl.col("symbol").is_in(adv))
    return full


def _build_intraday_date(
    date_iso: str, feature_groups: dict[str, list[str]], horizons_min: list[int], spread_col: str
) -> pl.DataFrame | None:
    feats = _load_features_for_date(date_iso, feature_groups)
    if feats is None or feats.height == 0:
        return None
    day = dt.date.fromisoformat(date_iso)
    sample_dts = [
        dt.datetime(day.year, day.month, day.day, hour, minute, tzinfo=dt.timezone.utc)
        for (hour, minute) in INTRADAY_SAMPLE_MINUTES_UTC
    ]
    sample_ts = pl.Series(sample_dts)
    feats = feats.filter(pl.col("minute").is_in(sample_ts))
    if feats.height == 0:
        return None
    bars = _load_bars_for_date(date_iso)
    if bars.height == 0:
        return None
    entry = (
        bars.rename({"ts": "minute"})
        .with_columns((pl.col("close") * pl.col("volume")).alias("entry_dollar_vol"))
        .select(["symbol", "minute", "close", "high", "low", "volume", "entry_dollar_vol"])
        .rename({"close": "entry_close", "high": "rth_high", "low": "rth_low", "volume": "entry_volume"})
    )
    feats = feats.join(entry, on=["symbol", "minute"], how="inner").filter(
        (pl.col("entry_dollar_vol") >= MIN_DOLLAR_VOL) & (pl.col("entry_close") >= MIN_PRICE)
    )
    if feats.height == 0:
        return None
    # half-spread (bps): real order-flow column where present, else the ADV proxy.
    spread = _load_spread_for_date(date_iso, spread_col)
    if spread is not None:
        feats = (
            feats.join(spread, on=["symbol", "minute"], how="left")
            .with_columns((pl.col(spread_col) / 2.0).alias("half_spread_bps"))
            .drop(spread_col)
        )
    else:
        feats = feats.with_columns(pl.lit(DEFAULT_HALF_SPREAD_BPS).alias("half_spread_bps"))
    feats = feats.with_columns(pl.col("half_spread_bps").fill_null(DEFAULT_HALF_SPREAD_BPS))
    for horizon in horizons_min:
        fwd = _forward_excess(bars, sample_ts, horizon)
        feats = feats.join(fwd, on=["symbol", "minute"], how="left")
    return feats


def panel_from_intraday_frame(feat: pl.DataFrame, feature_names: list[str]) -> Panel:
    """Materialize the intraday polars frame into the column-major `Panel`. The forward-excess
    label columns (`fwd_<h>m`) are carried in `extra` so each horizon's Strategy reads its label
    directly off the resident arrays (no per-archetype store re-read)."""
    feat = feat.sort(["symbol", "minute"])
    symbols = feat["symbol"].to_list()
    uniq = sorted(set(symbols))
    sym_to_idx = {sym: i for i, sym in enumerate(uniq)}
    symbol_code = np.array([sym_to_idx[s] for s in symbols], dtype=np.int64)
    minute_epoch = feat["minute"].dt.timestamp("ns").to_numpy().astype(np.int64)
    feature_matrix = feat.select(feature_names).to_numpy().astype(float)
    label_cols = [c for c in feat.columns if c.startswith("fwd_") and c.endswith("m")]
    extra = {c: feat[c].to_numpy().astype(float) for c in label_cols}
    return Panel(
        symbol_code=symbol_code,
        symbol_names=uniq,
        minute_epoch=minute_epoch,
        feature_names=list(feature_names),
        feature_matrix=feature_matrix,
        entry_close=feat["entry_close"].to_numpy().astype(float),
        half_spread_bps=feat["half_spread_bps"].to_numpy().astype(float),
        high=feat["rth_high"].to_numpy().astype(float),
        low=feat["rth_low"].to_numpy().astype(float),
        volume=feat["entry_volume"].to_numpy().astype(float),
        extra=extra,
        cadence="intraday",
        sector=None,
    )


def _forward_excess(bars: pl.DataFrame, sample_ts: pl.Series, horizon: int) -> pl.DataFrame:
    """close[t+h]/close[t]-1 by exact-timestamp lookup (with the $1 floor on BOTH legs), then
    cross-sectional EXCESS vs the per-minute median (breadth-floored). The ``build_dataset``
    forward_returns + excess, fused."""
    base = bars.rename({"ts": "minute", "close": "entry_c"}).select(["symbol", "minute", "entry_c"])
    fwd = (
        bars.with_columns((pl.col("ts") - pl.duration(minutes=horizon)).alias("minute"))
        .rename({"close": "fwd_c"})
        .select(["symbol", "minute", "fwd_c"])
    )
    joined = base.join(fwd, on=["symbol", "minute"], how="inner").filter(pl.col("minute").is_in(sample_ts))
    # $1 price-integrity floor on BOTH legs (mirrors the daily _ratio_with_floor): a sub-$1 entry OR
    # forward print is a bad/odd-lot print that manufactures fake returns (the Lane C / B4 trap), so
    # null the raw return rather than let it leak into the cross-section. The breadth count below uses
    # raw.count(), which ignores these nulls — so a nulled penny print also cannot satisfy the floor.
    joined = joined.with_columns(
        pl.when((pl.col("entry_c") >= MIN_PRICE) & (pl.col("fwd_c") >= MIN_PRICE))
        .then((pl.col("fwd_c") / pl.col("entry_c")) - 1.0)
        .otherwise(None)
        .alias("raw")
    )
    counts = joined.group_by("minute").agg(pl.col("raw").count().alias("n"))
    med = joined.group_by("minute").agg(pl.col("raw").median().alias("med"))
    out = joined.join(counts, on="minute").join(med, on="minute")
    return out.with_columns(
        pl.when(pl.col("n") >= 20)
        .then(pl.col("raw") - pl.col("med"))
        .otherwise(None)
        .alias(f"fwd_{horizon}m")
    ).select(["symbol", "minute", f"fwd_{horizon}m"])
