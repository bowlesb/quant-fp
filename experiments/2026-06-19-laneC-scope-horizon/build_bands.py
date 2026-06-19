"""Per-ADV-band overnight panel + per-band Corwin-Schultz cost, for the net-of-cost boundary
adjudication (boundary_hypothesis.md). Reuses Lane C's feature/label logic UNCHANGED, then splits
the labelled rows into pre-declared contiguous ADV-rank bands and writes one NPZ per (band, 1d).

Reads the stage-1 daily cache experiments/data/overnight_daily_full.parquet (NO minute re-scan).
Writes:
  experiments/data/band_<B>_fwd_1d.npz       (X, y, ts_ns, sym_idx, names, symbols) per band
  experiments/data/band_costs.json           per-band median Corwin-Schultz one-way cost (bps)
"""
from __future__ import annotations

import json
import os

import numpy as np
import polars as pl

DAILY = os.environ.get("DAILY", "/app/experiments/data/overnight_daily_full.parquet")
OUTDIR = os.environ.get("OUTDIR", "/app/experiments/data")

# --- Lane C constants (UNCHANGED) ---
MIN_DOLLAR_VOL = 50_000.0
MIN_PRICE = 1.0
WINSOR_Q = 0.005
MIN_CROSS_SECTION = 50
MIN_TRAILING_DAYS = 21
HORIZON = 1  # 1d headline only for the adjudication

# Pre-declared ADV-rank bands (boundary_hypothesis.md). (lo_inclusive, hi_exclusive) by ADV rank.
BANDS = {
    "B1_0001_0500": (1, 501),
    "B2_0500_1000": (501, 1001),
    "B3_1000_2000": (1001, 2001),
    "B4_2000_4000": (2001, 4001),
    "B5_4000_6000": (4001, 6001),
}
MIN_DAYS_FOR_RANK = 60  # symbol needs >=60 valid days to receive a stable ADV rank
IMPACT_PAD_BPS = 1.0     # generic slippage/impact/fee pad on top of the half-spread (one-way)

FEATURE_COLS = [
    "ret_1d", "ret_co_1d", "overnight_prev", "intraday_prev",
    "ret_2d", "ret_5d", "ret_10d", "ret_20d",
    "rvol_5d", "rvol_20d", "dollar_vol_20d", "gap_z", "range_20d_pos",
]


def compute_features_and_label(daily: pl.DataFrame) -> pl.DataFrame:
    """Lane C's stage-2 feature/label logic, verbatim (1d horizon only)."""
    daily = daily.sort(["symbol", "date"])
    over = pl.col("symbol")
    feat = daily.with_columns(
        (pl.col("rth_close") / pl.col("rth_open") - 1.0).alias("ret_1d"),
        (pl.col("rth_close") / pl.col("rth_close").shift(1).over(over) - 1.0).alias("ret_co_1d"),
        (pl.col("rth_open") / pl.col("rth_close").shift(1).over(over) - 1.0).alias("overnight_prev"),
        (pl.col("rth_close").shift(1).over(over)
         / pl.col("rth_open").shift(1).over(over) - 1.0).alias("intraday_prev"),
        *[
            (pl.col("rth_close") / pl.col("rth_close").shift(k).over(over) - 1.0).alias(f"ret_{k}d")
            for k in [2, 5, 10, 20]
        ],
        (pl.col("rth_dollar_vol").rolling_mean(window_size=20).over(over) + 1.0)
        .log().alias("dollar_vol_20d"),
        ((pl.col("rth_close") - pl.col("rth_low").rolling_min(20).over(over))
         / (pl.col("rth_high").rolling_max(20).over(over)
            - pl.col("rth_low").rolling_min(20).over(over))).alias("range_20d_pos"),
    )
    feat = feat.with_columns(
        pl.col("ret_co_1d").rolling_std(window_size=5).over(over).alias("rvol_5d"),
        pl.col("ret_co_1d").rolling_std(window_size=20).over(over).alias("rvol_20d"),
        ((pl.col("rth_close") - pl.col("rth_close").rolling_mean(20).over(over))
         / pl.col("rth_close").rolling_std(20).over(over)).alias("gap_z"),
        pl.col("date").cum_count().over(over).alias("bar_idx"),
        # trailing 20d mean dollar-vol in raw $ (for the point-in-time ADV rank, NOT logged)
        pl.col("rth_dollar_vol").rolling_mean(window_size=20).over(over).alias("adv_20d"),
    )
    exit_price = pl.col("exec_0935").shift(-HORIZON).over(over)
    feat = feat.with_columns(
        pl.when((pl.col("rth_close") >= MIN_PRICE) & (exit_price >= MIN_PRICE))
        .then(exit_price / pl.col("rth_close") - 1.0)
        .otherwise(None)
        .alias("fwd_1d_raw")
    )
    return feat


def cross_sectional_excess(frame: pl.DataFrame) -> pl.DataFrame:
    """Lane C's winsorize + per-day cross-sectional median excess, verbatim."""
    bounds = frame.group_by("date").agg(
        pl.col("fwd_1d_raw").quantile(WINSOR_Q).alias("lo"),
        pl.col("fwd_1d_raw").quantile(1.0 - WINSOR_Q).alias("hi"),
        pl.col("fwd_1d_raw").count().alias("n"),
    )
    out = frame.join(bounds, on="date")
    out = out.with_columns(
        pl.col("fwd_1d_raw").clip(lower_bound=pl.col("lo"), upper_bound=pl.col("hi")).alias("clipped")
    )
    med = out.group_by("date").agg(pl.col("clipped").median().alias("med"))
    out = out.join(med, on="date")
    return out.with_columns(
        pl.when(pl.col("n") >= MIN_CROSS_SECTION)
        .then(pl.col("clipped") - pl.col("med"))
        .otherwise(None)
        .alias("fwd_1d")
    ).drop(["lo", "hi", "n", "clipped", "med"])


def corwin_schultz_half_spread_bps(daily: pl.DataFrame) -> pl.DataFrame:
    """Corwin-Schultz (2012) high-low daily bid-ask spread estimator, per (symbol, day).
    beta = E[(ln(H_t/L_t))^2 + (ln(H_{t+1}/L_{t+1}))^2]  (two adjacent single-day high-low vars)
    gamma = (ln(H_{t,t+1}/L_{t,t+1}))^2                    (two-day high-low var)
    alpha = (sqrt(2 beta) - sqrt(beta)) / (3 - 2 sqrt2) - sqrt(gamma / (3 - 2 sqrt2))
    spread S = 2 (e^alpha - 1) / (1 + e^alpha)             (proportional, round-trip)
    Returns one-way HALF spread in bps = S/2 * 1e4, negatives clamped to 0 (CS convention).
    Computed point-in-time from the daily RTH high/low only — quote-free, literature-standard."""
    c1 = 3.0 - 2.0 * np.sqrt(2.0)
    over = pl.col("symbol")
    d = daily.sort(["symbol", "date"]).with_columns(
        (pl.col("rth_high") / pl.col("rth_low")).log().pow(2).alias("hl2"),
        pl.col("rth_high").shift(-1).over(over).alias("h_next"),
        pl.col("rth_low").shift(-1).over(over).alias("l_next"),
    )
    d = d.with_columns(
        (pl.col("h_next") / pl.col("l_next")).log().pow(2).alias("hl2_next"),
        pl.max_horizontal("rth_high", "h_next").alias("h2"),
        pl.min_horizontal("rth_low", "l_next").alias("l2"),
    )
    d = d.with_columns(
        (pl.col("hl2") + pl.col("hl2_next")).alias("beta"),
        (pl.col("h2") / pl.col("l2")).log().pow(2).alias("gamma"),
    )
    d = d.with_columns(
        (((2.0 * pl.col("beta")).sqrt() - pl.col("beta").sqrt()) / c1
         - (pl.col("gamma") / c1).sqrt()).alias("alpha")
    )
    d = d.with_columns(
        (2.0 * (pl.col("alpha").exp() - 1.0) / (1.0 + pl.col("alpha").exp())).alias("cs_spread")
    )
    # one-way half spread in bps, clamp negatives to 0 (CS convention)
    return d.select(
        "symbol", "date",
        (pl.col("cs_spread").clip(lower_bound=0.0) / 2.0 * 1e4).alias("cs_half_bps"),
    )


def main() -> None:
    daily = pl.read_parquet(DAILY)
    print(f"daily table: {daily.shape}")

    feat = compute_features_and_label(daily)
    feat = feat.filter(
        (pl.col("bar_idx") >= MIN_TRAILING_DAYS)
        & ((pl.col("rth_close") * pl.col("rth_volume")) >= MIN_DOLLAR_VOL)
        & (pl.col("rth_close") >= MIN_PRICE)
    )
    feat = cross_sectional_excess(feat)

    cs = corwin_schultz_half_spread_bps(daily)
    feat = feat.join(cs, on=["symbol", "date"], how="left")

    # one cross-section per day, timestamp = 19:59 UTC of day d
    feat = feat.with_columns(
        (pl.col("date").str.to_datetime("%Y-%m-%d", time_zone="UTC")
         + pl.duration(hours=19, minutes=59)).alias("minute")
    )
    labelled = feat.filter(pl.col("fwd_1d").is_not_null() & pl.col("adv_20d").is_not_null())
    print(f"labelled rows (1d): {labelled.height} days={labelled['date'].n_unique()} "
          f"symbols={labelled['symbol'].n_unique()}")

    # Per-symbol ADV (mean of the trailing-20d adv over the symbol's labelled rows) -> stable rank.
    sym_adv = (labelled.group_by("symbol")
               .agg(pl.col("adv_20d").mean().alias("adv"), pl.len().alias("ndays"))
               .filter(pl.col("ndays") >= MIN_DAYS_FOR_RANK)
               .sort("adv", descending=True)
               .with_row_index("adv_rank", offset=1))
    print(f"ranked symbols (>={MIN_DAYS_FOR_RANK} labelled days): {sym_adv.height}")

    labelled = labelled.join(sym_adv.select("symbol", "adv_rank", "adv"), on="symbol", how="inner")

    band_costs: dict[str, dict[str, float]] = {}
    os.makedirs(OUTDIR, exist_ok=True)
    for band, (lo, hi) in BANDS.items():
        sub = labelled.filter((pl.col("adv_rank") >= lo) & (pl.col("adv_rank") < hi)).sort("minute")
        if sub.height == 0:
            print(f"  {band}: EMPTY"); continue
        median_half = float(sub["cs_half_bps"].median())
        oneway_cost = median_half + IMPACT_PAD_BPS
        band_adv_min = float(sub["adv"].min())
        band_adv_median = float(sub["adv"].median())
        # per-name notional at $100K with a ~decile L/S over this band's mean daily breadth
        mean_breadth = sub.group_by("date").len()["len"].mean()
        n_per_side = max(1, int(0.1 * float(mean_breadth)))
        per_name_notional = 100_000.0 / (2 * n_per_side)
        adv_fraction = per_name_notional / band_adv_min if band_adv_min > 0 else float("nan")
        band_costs[band] = {
            "median_cs_half_bps": round(median_half, 3),
            "oneway_cost_bps": round(oneway_cost, 3),
            "band_adv_min": band_adv_min,
            "band_adv_median": band_adv_median,
            "mean_daily_breadth": round(float(mean_breadth), 1),
            "per_name_notional_usd": round(per_name_notional, 2),
            "per_name_notional_frac_of_min_adv": adv_fraction,
            "rows": sub.height,
            "days": sub["date"].n_unique(),
            "symbols": sub["symbol"].n_unique(),
        }
        symbols = sub["symbol"].to_list()
        uniq = sorted(set(symbols))
        sym_to_idx = {s: i for i, s in enumerate(uniq)}
        sym_idx = np.array([sym_to_idx[s] for s in symbols], dtype=np.int64)
        np.savez(
            os.path.join(OUTDIR, f"band_{band}_fwd_1d.npz"),
            X=sub.select(FEATURE_COLS).to_numpy().astype(float),
            y=sub["fwd_1d"].to_numpy().astype(float),
            ts_ns=sub["minute"].dt.timestamp("ns").to_numpy().astype(np.int64),
            sym_idx=sym_idx,
            names=np.array(FEATURE_COLS),
            symbols=np.array(uniq),
        )
        print(f"  {band}: rows={sub.height} days={sub['date'].n_unique()} "
              f"symbols={sub['symbol'].n_unique()} median_CS_half={median_half:.2f}bps "
              f"oneway_cost={oneway_cost:.2f}bps adv_min=${band_adv_min:,.0f} "
              f"per_name=${per_name_notional:.0f} (={adv_fraction:.2e} of min ADV)")

    with open(os.path.join(OUTDIR, "band_costs.json"), "w") as fh:
        json.dump(band_costs, fh, indent=2)
    print(f"\nWROTE band_costs.json")


if __name__ == "__main__":
    main()
