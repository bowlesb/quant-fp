"""
H3: Book depth / spread state as a vwap_dev reversion conditioner.

Adds book_depth and size_imbalance per (symbol, minute) from raw quotes,
joins onto H2 panel, then tests whether any book-state tercile lifts
the vwap_dev L/S net-of-cost gross above that tercile's spread cost.
"""

import os
import json
import random
import datetime
import polars as pl
import numpy as np

PANEL_PATH = "/app/experiments/2026-06-16-h2-retest-ofi-orthogonal/data/panel.parquet"
QUOTE_BASE = "/store/raw/quotes"
BOOK_CACHE = "/app/experiments/2026-06-16-h3-depth-conditioner/data/book_features.parquet"
RESULTS_PATH = "/app/experiments/2026-06-16-h3-depth-conditioner/data/results.json"
N_CANARY_SEEDS = 10
RTH_START_UTC = 14  # 09:30 ET = 14:30 UTC  (we'll use hour >= 14)
RTH_END_UTC_H = 20  # 15:00 ET = 20:00 UTC

print("=== H3 depth conditioner ===", flush=True)

# ---------------------------------------------------------------
# 1. Load panel: get symbols + dates
# ---------------------------------------------------------------
print("Loading panel...", flush=True)
panel = pl.read_parquet(PANEL_PATH)
print(f"  Panel: {panel.shape[0]:,} rows, {panel['symbol'].n_unique()} symbols, "
      f"{panel['date'].n_unique()} dates", flush=True)

symbols = sorted(panel["symbol"].unique().to_list())
dates = sorted(panel["date"].unique().to_list())
print(f"  Symbols: {len(symbols)}, Dates: {len(dates)}", flush=True)

# ---------------------------------------------------------------
# 2. Build book features: book_depth + size_imbalance per (symbol, minute)
#    from raw quotes — RTH only, positive-depth only
# ---------------------------------------------------------------
if os.path.exists(BOOK_CACHE):
    print("Loading cached book features...", flush=True)
    book_df = pl.read_parquet(BOOK_CACHE)
    print(f"  Cached: {book_df.shape[0]:,} rows", flush=True)
else:
    print("Building book features from raw quotes...", flush=True)
    chunks: list[pl.DataFrame] = []
    missing = 0
    loaded = 0

    for sym in symbols:
        sym_dir = os.path.join(QUOTE_BASE, f"symbol={sym}")
        if not os.path.isdir(sym_dir):
            missing += 1
            continue
        for date_str in dates:
            fpath = os.path.join(sym_dir, f"date={date_str}", "data.parquet")
            if not os.path.exists(fpath):
                missing += 1
                continue
            try:
                q = pl.read_parquet(fpath, columns=["ts", "bid_price", "bid_size", "ask_price", "ask_size"])
            except Exception as exc:
                print(f"  WARN: {sym}/{date_str}: {exc}", flush=True)
                missing += 1
                continue

            # RTH filter: 14:30–20:00 UTC (09:30–15:00 ET)
            q = q.filter(
                (pl.col("ts").dt.hour() >= 14) & (pl.col("ts").dt.hour() < 20)
            ).filter(
                ~((pl.col("ts").dt.hour() == 14) & (pl.col("ts").dt.minute() < 30))
            )
            if q.is_empty():
                continue

            # Positive-depth filter
            q = q.filter((pl.col("bid_size") + pl.col("ask_size")) > 0)
            if q.is_empty():
                continue

            # Truncate ts to minute
            q = q.with_columns(
                pl.col("ts").dt.truncate("1m").alias("minute"),
                pl.lit(sym).alias("symbol"),
            )

            # Pre-compute depth and imbalance columns before aggregating
            q = q.with_columns([
                (pl.col("bid_size") + pl.col("ask_size")).alias("_total_size"),
                ((pl.col("bid_size") - pl.col("ask_size")) /
                 (pl.col("bid_size") + pl.col("ask_size"))).alias("_imbal"),
            ])

            # Per-minute aggregates
            agg = q.group_by(["symbol", "minute"]).agg([
                pl.col("_total_size").mean().alias("book_depth"),
                pl.col("_imbal").mean().alias("size_imbalance"),
            ])
            chunks.append(agg)
            loaded += 1

    print(f"  Loaded {loaded} (sym,date) pairs, {missing} missing", flush=True)
    book_df = pl.concat(chunks)
    print(f"  Book features: {book_df.shape[0]:,} rows", flush=True)
    book_df.write_parquet(BOOK_CACHE)
    print(f"  Cached to {BOOK_CACHE}", flush=True)

# ---------------------------------------------------------------
# 3. Join book features onto panel
# ---------------------------------------------------------------
print("Joining book features onto panel...", flush=True)
# Panel minute is UTC datetime; book_df minute is UTC datetime — join directly
panel_aug = panel.join(book_df, on=["symbol", "minute"], how="left")
before = panel.shape[0]
after = panel_aug.shape[0]
joined = panel_aug.filter(pl.col("book_depth").is_not_null()).shape[0]
print(f"  Panel rows: {before:,} -> {after:,}, with book features: {joined:,}", flush=True)

# Drop rows lacking book features or signal
panel_aug = panel_aug.filter(
    pl.col("book_depth").is_not_null() &
    pl.col("size_imbalance").is_not_null() &
    pl.col("vwap_dev_15").is_not_null() &
    pl.col("fwd_ret_15").is_not_null() &
    pl.col("rel_spread_mean").is_not_null()
)
print(f"  Clean rows for analysis: {panel_aug.shape[0]:,}", flush=True)

# ---------------------------------------------------------------
# 4. Conditioning test — all vectorized via polars
# ---------------------------------------------------------------

def decile_ls_net_gross(df: pl.DataFrame, signal: str, fwd_ret: str, spread_col: str) -> dict:
    """
    Within each (date, minute) cross-section:
      - rank signal into deciles
      - long bottom decile (most below VWAP = reversion longs), short top decile
      - net-of-cost gross = mean(long_rets) - mean(short_rets) - median_spread (round-trip)
    Returns dict with gross_bps, cost_bps, net_bps, n_xs, spread_bps.
    """
    # Compute within-cross-section decile ranks
    ranked = df.with_columns(
        pl.col(signal)
        .rank("ordinal")
        .over(["date", "minute"])
        .alias("_rank"),
        pl.len().over(["date", "minute"]).alias("_n"),
    ).with_columns(
        (pl.col("_rank") / pl.col("_n") * 10).floor().clip(0, 9).cast(pl.Int32).alias("_decile")
    )

    # L/S: long decile 0 (lowest vwap_dev = most below VWAP), short decile 9
    ls = ranked.filter(pl.col("_decile").is_in([0, 9]))
    ls = ls.with_columns(
        pl.when(pl.col("_decile") == 0).then(1.0).otherwise(-1.0).alias("_side")
    )

    # Per cross-section L/S return
    xs_ret = (
        ls.with_columns((pl.col(fwd_ret) * pl.col("_side")).alias("_ls_ret"))
        .group_by(["date", "minute"])
        .agg([
            pl.mean("_ls_ret").alias("_xs_ls"),
            pl.mean(spread_col).alias("_xs_spread"),
        ])
    )

    gross_bps = xs_ret["_xs_ls"].mean() * 10000
    spread_bps = xs_ret["_xs_spread"].mean() * 10000
    cost_bps = spread_bps  # round-trip cost = spread (we use rel_spread_mean as bps proxy — it's already fractional)
    # rel_spread_mean = (ask-bid)/mid, already fractional → * 10000 = bps
    net_bps = gross_bps - cost_bps
    n_xs = xs_ret.shape[0]

    return {
        "gross_bps": float(gross_bps) if gross_bps is not None else float("nan"),
        "spread_bps": float(spread_bps) if spread_bps is not None else float("nan"),
        "net_bps": float(net_bps) if net_bps is not None else float("nan"),
        "n_xs": int(n_xs),
    }


def canary_net_bps(df: pl.DataFrame, signal: str, fwd_ret: str, spread_col: str, n_seeds: int = 10) -> list:
    """Shuffle signal within each cross-section N times, return distribution of net_bps."""
    results = []
    data = df.with_columns([
        pl.col(signal).alias("_sig_orig"),
        pl.col(fwd_ret).alias("_fwd"),
        pl.col(spread_col).alias("_spr"),
    ])
    for seed in range(n_seeds):
        rng = random.Random(seed + 42)
        # Shuffle signal within each cross-section
        shuffled = (
            data.with_columns(
                pl.col("_sig_orig")
                .shuffle(seed=seed + 42)
                .over(["date", "minute"])
                .alias("_sig_shuf")
            )
        )
        res = decile_ls_net_gross(shuffled, "_sig_shuf", "_fwd", "_spr")
        results.append(res["net_bps"])
    return results


def add_tercile_col(df: pl.DataFrame, col: str, name: str) -> pl.DataFrame:
    """Add within-date tercile column for a book-state variable (cross-date quantiles)."""
    q33 = df[col].quantile(0.333)
    q67 = df[col].quantile(0.667)
    return df.with_columns(
        pl.when(pl.col(col) <= q33).then(pl.lit(0))
        .when(pl.col(col) <= q67).then(pl.lit(1))
        .otherwise(pl.lit(2))
        .cast(pl.Int32)
        .alias(name)
    )


print("\n--- FLAT vwap_dev baseline ---", flush=True)
flat_15 = decile_ls_net_gross(panel_aug, "vwap_dev_15", "fwd_ret_15", "rel_spread_mean")
flat_30 = decile_ls_net_gross(panel_aug, "vwap_dev_15", "fwd_ret_30", "rel_spread_mean")
print(f"  H15: gross={flat_15['gross_bps']:.2f} bps, spread={flat_15['spread_bps']:.2f} bps, "
      f"net={flat_15['net_bps']:.2f} bps, n_xs={flat_15['n_xs']}", flush=True)
print(f"  H30: gross={flat_30['gross_bps']:.2f} bps, spread={flat_30['spread_bps']:.2f} bps, "
      f"net={flat_30['net_bps']:.2f} bps, n_xs={flat_30['n_xs']}", flush=True)

print("\n--- Flat vwap_dev canary (10 seeds) ---", flush=True)
flat_canary_15 = canary_net_bps(panel_aug, "vwap_dev_15", "fwd_ret_15", "rel_spread_mean", N_CANARY_SEEDS)
flat_canary_30 = canary_net_bps(panel_aug, "vwap_dev_15", "fwd_ret_30", "rel_spread_mean", N_CANARY_SEEDS)
print(f"  H15 canary net bps: mean={np.mean(flat_canary_15):.2f}, max={np.max(flat_canary_15):.2f}", flush=True)
print(f"  H30 canary net bps: mean={np.mean(flat_canary_30):.2f}, max={np.max(flat_canary_30):.2f}", flush=True)

# ---------------------------------------------------------------
# 5. Book-state conditioning
# ---------------------------------------------------------------
# Add tercile columns for each conditioner
panel_aug = add_tercile_col(panel_aug, "rel_spread_mean", "spread_tercile")
panel_aug = add_tercile_col(panel_aug, "book_depth", "depth_tercile")
panel_aug = add_tercile_col(panel_aug, "size_imbalance", "imbal_tercile")

TERCILE_NAMES = {0: "tight/thin/bid-heavy", 1: "mid", 2: "wide/deep/ask-heavy"}
SPREAD_NAMES = {0: "tight", 1: "mid", 2: "wide"}
DEPTH_NAMES = {0: "thin", 1: "mid", 2: "deep"}
IMBAL_NAMES = {0: "bid-heavy", 1: "neutral", 2: "ask-heavy"}

conditioners = [
    ("spread", "spread_tercile", SPREAD_NAMES),
    ("depth", "depth_tercile", DEPTH_NAMES),
    ("size_imbalance", "imbal_tercile", IMBAL_NAMES),
]

conditioned_results: dict = {}
best_net_15 = float("-inf")
best_cell_15 = None

for cond_name, tercile_col, tercile_labels in conditioners:
    print(f"\n--- Conditioning on {cond_name} ---", flush=True)
    cond_res = {}
    for tercile_val in [0, 1, 2]:
        subset = panel_aug.filter(pl.col(tercile_col) == tercile_val)
        label = tercile_labels[tercile_val]
        if subset.shape[0] < 1000:
            print(f"  {label}: too few rows ({subset.shape[0]}), skip", flush=True)
            continue

        res_15 = decile_ls_net_gross(subset, "vwap_dev_15", "fwd_ret_15", "rel_spread_mean")
        res_30 = decile_ls_net_gross(subset, "vwap_dev_15", "fwd_ret_30", "rel_spread_mean")

        # Canary within this cell
        can_15 = canary_net_bps(subset, "vwap_dev_15", "fwd_ret_15", "rel_spread_mean", N_CANARY_SEEDS)
        can_30 = canary_net_bps(subset, "vwap_dev_15", "fwd_ret_30", "rel_spread_mean", N_CANARY_SEEDS)

        print(f"  {label}: n={subset.shape[0]:,}", flush=True)
        print(f"    H15: gross={res_15['gross_bps']:.2f}, spread={res_15['spread_bps']:.2f}, "
              f"net={res_15['net_bps']:.2f} | canary_max={np.max(can_15):.2f}", flush=True)
        print(f"    H30: gross={res_30['gross_bps']:.2f}, spread={res_30['spread_bps']:.2f}, "
              f"net={res_30['net_bps']:.2f} | canary_max={np.max(can_30):.2f}", flush=True)

        if res_15["net_bps"] > best_net_15:
            best_net_15 = res_15["net_bps"]
            best_cell_15 = {
                "conditioner": cond_name,
                "tercile": label,
                "net_bps_15": res_15["net_bps"],
                "gross_bps_15": res_15["gross_bps"],
                "spread_bps_15": res_15["spread_bps"],
                "canary_max_15": float(np.max(can_15)),
                "net_bps_30": res_30["net_bps"],
                "n_rows": subset.shape[0],
            }

        cond_res[label] = {
            "n_rows": subset.shape[0],
            "h15": {**res_15, "canary_15": can_15},
            "h30": {**res_30, "canary_30": can_30},
        }

    conditioned_results[cond_name] = cond_res

# ---------------------------------------------------------------
# 6. Persist results
# ---------------------------------------------------------------
results = {
    "flat_15": flat_15,
    "flat_30": flat_30,
    "flat_canary_15": flat_canary_15,
    "flat_canary_30": flat_canary_30,
    "conditioned": conditioned_results,
    "best_cell_h15": best_cell_15,
}
with open(RESULTS_PATH, "w") as f_out:
    json.dump(results, f_out, indent=2)
print(f"\nResults saved to {RESULTS_PATH}", flush=True)

print("\n=== SUMMARY ===", flush=True)
print(f"Flat vwap_dev H15: gross={flat_15['gross_bps']:.2f} bps, net={flat_15['net_bps']:.2f} bps", flush=True)
print(f"Flat canary H15 max: {max(flat_canary_15):.2f} bps", flush=True)
print(f"Best conditioned cell H15: {best_cell_15}", flush=True)
