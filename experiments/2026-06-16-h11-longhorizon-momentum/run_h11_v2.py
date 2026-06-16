"""
H11 v2: Longer-horizon vwap_dev MOMENTUM (sign-flip of H9 reversion).

CORRECTED timezone: bars ts is TRUE UTC. NYSE RTH:
  09:30 ET = 13:30 UTC = minute 810
  09:35 ET = 13:35 UTC = minute 815
  15:50 ET = 19:50 UTC = minute 1190
  16:00 ET = 20:00 UTC = minute 1200

Previous H9/H11 used ET values (570 etc.) as if they were UTC — this was WRONG.
The prior run scored bars at pre-dawn UTC times, anchored slot-0 at the 13:30 UTC
(09:30 ET) open print, and the tradeable-entry gate (575 UTC) never fired.

Gates applied:
- Gate A: tradeable entry >= 815 (13:35 UTC = 09:35 ET); slot-0 = 810 (open print) excluded
- Gate B: per-symbol demean (survivorship/idiosyncratic check)
- Canary: 10-seed within-CS shuffle
- Robustness: exclude first+last 30 min (10:00-15:30 ET = 14:00-19:30 UTC = 840-1170)

Run from sandbox:
  cd /home/ben/quant-fp && MEM=12g CPUS=8 ops/sandbox.sh "python experiments/2026-06-16-h11-longhorizon-momentum/run_h11_v2.py"
"""

import json
import polars as pl
import numpy as np
from pathlib import Path

STORE = Path("/store/raw/bars")
OUT = Path("/app/experiments/2026-06-16-h11-longhorizon-momentum")
OUT.mkdir(parents=True, exist_ok=True)

N_DAYS = 50

# TRUE UTC constants (EDT = UTC-4, so ET+4h = UTC)
RTH_START_UTC    = 13 * 60 + 30   # 810 — 09:30 ET = 13:30 UTC (market open, open print)
RTH_TRADEABLE    = 13 * 60 + 35   # 815 — 09:35 ET = 13:35 UTC (first tradeable bar)
RTH_END_UTC      = 19 * 60 + 50   # 1190 — 15:50 ET = 19:50 UTC (last scoreable signal bar)
RTH_LOAD_END     = 22 * 60        # 1320 — 22:00 UTC; captures T+120 for T<=1190
# Robustness: exclude open+close microstructure
RTH_EXCL_OPEN_END    = 14 * 60    # 840 — 10:00 ET = 14:00 UTC
RTH_EXCL_CLOSE_START = 19 * 60 + 30  # 1170 — 15:30 ET = 19:30 UTC

UNIVERSE_SIZE = 300
MIN_DATE_COVERAGE = 0.90

SIGNAL_WINDOWS = [30, 60]
FWD_HORIZONS   = [60, 120]
COST_RT_BPS    = [4.0, 6.0, 10.0]
CANARY_SEEDS   = 10
DECILE = 10

print("=== H11 v2: Long-Horizon VWAP-Dev MOMENTUM (timezone-corrected) ===")
print(f"  RTH_START_UTC={RTH_START_UTC} (09:30 ET), RTH_TRADEABLE={RTH_TRADEABLE} (09:35 ET)")
print(f"  RTH_END_UTC={RTH_END_UTC} (15:50 ET), RTH_LOAD_END={RTH_LOAD_END} (22:00 UTC)")

# ── Step 1: Enumerate available dates ─────────────────────────────────────────
print("\n[1] Enumerating dates...")
all_symbols = sorted([d.name.split("=")[1] for d in STORE.iterdir() if d.name.startswith("symbol=")])
aapl_dates = sorted([d.name.split("=")[1] for d in (STORE / "symbol=AAPL").iterdir()])
selected_dates = aapl_dates[-N_DAYS:]
print(f"  Selected {len(selected_dates)} dates: {selected_dates[0]} → {selected_dates[-1]}")

# ── Step 2: Build liquid universe ─────────────────────────────────────────────
print("\n[2] Building liquid universe (sampling ~10 days for dv estimate)...")
sample_dates = selected_dates[::max(1, len(selected_dates) // 10)][:10]
dv_records = []

for sym in all_symbols:
    sym_path = STORE / f"symbol={sym}"
    daily_dvs = []
    for date_str in sample_dates:
        fpath = sym_path / f"date={date_str}" / "data.parquet"
        if not fpath.exists():
            continue
        df = pl.read_parquet(fpath, columns=["close", "volume"])
        if df.is_empty():
            continue
        dv = float((df["close"] * df["volume"]).sum())
        daily_dvs.append(dv)
    if len(daily_dvs) >= len(sample_dates) * 0.8:
        dv_records.append({"symbol": sym, "median_dv": float(np.median(daily_dvs))})

dv_df = pl.DataFrame(dv_records).sort("median_dv", descending=True)
top_symbols = dv_df.head(UNIVERSE_SIZE)["symbol"].to_list()
print(f"  Candidate universe: {len(top_symbols)} symbols. Top 5: {top_symbols[:5]}")

# ── Step 3: Load bars panel (RTH + enough post-market for H=120 lookforward) ──
print("\n[3] Loading bars panel...")
frames = []
for sym in top_symbols:
    sym_path = STORE / f"symbol={sym}"
    for date_str in selected_dates:
        fpath = sym_path / f"date={date_str}" / "data.parquet"
        if not fpath.exists():
            continue
        df = pl.read_parquet(fpath)
        if df.is_empty():
            continue
        df = df.with_columns([
            pl.lit(date_str).alias("date"),
            (pl.col("ts").dt.hour().cast(pl.Int32) * 60 + pl.col("ts").dt.minute().cast(pl.Int32)).alias("utc_minute"),
        ])
        frames.append(df)

print(f"  Loaded {len(frames)} (symbol, date) partitions")
bars = pl.concat(frames, how="diagonal_relaxed")
print(f"  Raw rows: {bars.shape[0]:,}")

# Load filter: RTH_START_UTC to RTH_LOAD_END (captures T+120 for T<=1190)
bars = bars.filter(
    (pl.col("utc_minute") >= RTH_START_UTC) & (pl.col("utc_minute") < RTH_LOAD_END)
)
bars = bars.filter(pl.col("volume") > 0)
print(f"  After RTH+volume filter: {bars.shape[0]:,}")

# ── Step 4: Filter universe to symbols with sufficient date coverage ───────────
date_counts = (
    bars.group_by("symbol")
    .agg(pl.col("date").n_unique().alias("n_dates"))
    .filter(pl.col("n_dates") >= int(len(selected_dates) * MIN_DATE_COVERAGE))
)
final_symbols = date_counts["symbol"].to_list()
bars = bars.filter(pl.col("symbol").is_in(final_symbols))
print(f"  Final universe: {len(final_symbols)} symbols, {bars.shape[0]:,} rows")

# ── Step 5: Compute trailing vwap_dev for each signal window W ─────────────────
print("\n[4] Computing trailing VWAP deviations (within RTH only, no pre-market lookback)...")
bars = bars.sort(["symbol", "date", "utc_minute"])

result_frames = {}
for W in SIGNAL_WINDOWS:
    print(f"  W={W}: computing trailing {W}-min VWAP...")
    df_w = (
        bars
        .with_columns([(pl.col("close") * pl.col("volume")).alias("cv")])
        .with_columns([
            pl.col("cv").rolling_sum(window_size=W, min_samples=W).over(["symbol", "date"]).alias("sum_cv_W"),
            pl.col("volume").rolling_sum(window_size=W, min_samples=W).over(["symbol", "date"]).alias("sum_vol_W"),
        ])
        .with_columns([(pl.col("sum_cv_W") / pl.col("sum_vol_W")).alias(f"tvwap_{W}")])
        .with_columns([(pl.col("close") / pl.col(f"tvwap_{W}") - 1.0).alias(f"vwap_dev_{W}")])
        .drop(["cv", "sum_cv_W", "sum_vol_W", f"tvwap_{W}"])
    )
    result_frames[W] = df_w

bars_sig = result_frames[SIGNAL_WINDOWS[0]]
for W in SIGNAL_WINDOWS[1:]:
    bars_sig = bars_sig.join(
        result_frames[W].select(["symbol", "date", "utc_minute", f"vwap_dev_{W}"]),
        on=["symbol", "date", "utc_minute"],
        how="left",
    )

# ── Step 6: Compute forward returns at each horizon H ─────────────────────────
print("\n[5] Computing forward returns...")
for H in FWD_HORIZONS:
    bars_sig = bars_sig.with_columns(
        [(pl.col("utc_minute") + H).alias("target_minute")]
    ).join(
        bars_sig.select(["symbol", "date", "utc_minute", "close"])
            .rename({"close": f"close_fwd_{H}", "utc_minute": "target_minute"}),
        on=["symbol", "date", "target_minute"],
        how="left",
    ).with_columns(
        [(pl.col(f"close_fwd_{H}") / pl.col("close") - 1.0).alias(f"fwd_ret_{H}")]
    ).drop("target_minute")

print(f"  bars_sig shape: {bars_sig.shape}")

# Verify: print a few rows around the open to confirm slot timing
open_check = bars_sig.filter(
    (pl.col("symbol") == "AAPL")
    & (pl.col("date") == selected_dates[-5])
    & (pl.col("utc_minute") >= RTH_START_UTC - 2)
    & (pl.col("utc_minute") <= RTH_START_UTC + 10)
).select(["symbol", "date", "utc_minute", "vwap_dev_30", "fwd_ret_60"])
print(f"\n  Sanity check — AAPL bars around open (minute ~810):")
print(open_check.head(15))


def run_momentum_ls(
    panel_in: pl.DataFrame,
    sig_col: str,
    ret_col: str,
    label: str,
) -> dict:
    """
    Compute momentum L/S (long top decile, short bottom decile) for a given panel slice.
    Returns dict of results.
    """
    n_obs = panel_in.shape[0]
    n_dates_panel = panel_in["date"].n_unique()
    print(f"\n  [{label}] {n_obs:,} obs across {n_dates_panel} dates")

    if n_obs < 50:
        print(f"    Too few obs, skipping.")
        return {}

    # Decile rank within each (date, slot) cross-section
    # MOMENTUM: long top decile (decile 9 = highest vwap_dev), short bottom (decile 0)
    panel_ranked = panel_in.with_columns([
        pl.col(sig_col).rank("ordinal").over(["date", "slot"]).alias("rank_raw"),
        pl.col(sig_col).count().over(["date", "slot"]).alias("cs_count"),
    ]).with_columns([
        ((pl.col("rank_raw") - 1) / pl.col("cs_count") * DECILE).floor().cast(pl.Int32).clip(0, DECILE - 1).alias("decile"),
    ])

    # MOMENTUM L/S: long decile 9 (most-above-VWAP), short decile 0 (most-below-VWAP)
    long_ret = (
        panel_ranked.filter(pl.col("decile") == DECILE - 1)
        .group_by(["date", "slot"])
        .agg(pl.col(ret_col).mean().alias("long_ret"))
    )
    short_ret = (
        panel_ranked.filter(pl.col("decile") == 0)
        .group_by(["date", "slot"])
        .agg(pl.col(ret_col).mean().alias("short_ret"))
    )
    ls = long_ret.join(short_ret, on=["date", "slot"], how="inner").with_columns([
        (pl.col("long_ret") - pl.col("short_ret")).alias("ls_ret"),
    ])

    gross_bps = float(ls["ls_ret"].mean()) * 10000
    n_periods = ls.shape[0]
    print(f"    Momentum gross L/S: {gross_bps:.2f} bps ({n_periods} rebalance periods)")

    # Turnover
    panel_leg = panel_ranked.with_columns([
        pl.when(pl.col("decile") == DECILE - 1).then(pl.lit(1))
        .when(pl.col("decile") == 0).then(pl.lit(-1))
        .otherwise(pl.lit(0))
        .alias("leg"),
    ]).select(["symbol", "date", "slot", "leg"])

    prev_panel = panel_leg.rename({"slot": "prev_slot", "leg": "prev_leg"}).with_columns([
        (pl.col("prev_slot") + 1).alias("slot"),
    ])
    turnover_df = panel_leg.join(prev_panel, on=["symbol", "date", "slot"], how="inner")
    turnover_df = turnover_df.with_columns([
        (
            (pl.col("leg") != pl.col("prev_leg")) &
            ((pl.col("leg") != 0) | (pl.col("prev_leg") != 0))
        ).cast(pl.Float64).alias("changed"),
        ((pl.col("leg") != 0) | (pl.col("prev_leg") != 0)).cast(pl.Float64).alias("in_leg"),
    ]).filter(pl.col("in_leg") > 0)
    turnover = float(turnover_df["changed"].mean()) if turnover_df.shape[0] > 0 else float("nan")
    print(f"    Turnover: {turnover:.3f}")

    nets = {}
    for cost in COST_RT_BPS:
        net_val = gross_bps - turnover * cost
        nets[cost] = net_val
        print(f"    Net @{cost}bps: {net_val:.2f} bps")

    # Day-clustered t-stat
    daily_ls = ls.group_by("date").agg(pl.col("ls_ret").mean().alias("daily_ls"))
    daily_arr = daily_ls["daily_ls"].to_numpy()
    n_days_clust = len(daily_arr)
    if n_days_clust > 1:
        tstat = float(np.mean(daily_arr) / (np.std(daily_arr, ddof=1) / np.sqrt(n_days_clust)))
    else:
        tstat = float("nan")
    print(f"    Day-clustered t: {tstat:.2f} (n_days={n_days_clust})")

    # Canary (within-CS shuffle on momentum leg)
    canary_gross_list = []
    for seed in range(CANARY_SEEDS):
        shuffled = panel_ranked.with_columns([
            pl.Series(
                ret_col + "_shuf",
                panel_ranked.with_columns([
                    pl.col(ret_col).alias("_ret"),
                    (pl.col("date") + "_" + pl.col("slot").cast(pl.Utf8)).alias("_cs_key"),
                ]).select(
                    pl.col("_ret").shuffle(seed=seed).over("_cs_key")
                ).to_series().to_numpy(),
            )
        ])
        long_s = (
            shuffled.filter(pl.col("decile") == DECILE - 1)
            .group_by(["date", "slot"])
            .agg(pl.col(ret_col + "_shuf").mean().alias("lr"))
        )
        short_s = (
            shuffled.filter(pl.col("decile") == 0)
            .group_by(["date", "slot"])
            .agg(pl.col(ret_col + "_shuf").mean().alias("sr"))
        )
        ls_s = long_s.join(short_s, on=["date", "slot"], how="inner")
        if ls_s.shape[0] > 0:
            canary_gross_list.append(float((ls_s["lr"] - ls_s["sr"]).mean()) * 10000)

    canary_mean = float(np.mean(canary_gross_list)) if canary_gross_list else float("nan")
    canary_std  = float(np.std(canary_gross_list)) if canary_gross_list else float("nan")
    canary_95   = canary_mean + 2 * canary_std
    clears_canary = gross_bps > canary_95
    print(f"    Canary band: mean={canary_mean:.2f}, std={canary_std:.2f}, 95th={canary_95:.2f}")
    print(f"    Clears canary: {clears_canary}")

    return {
        "n_obs": n_obs,
        "n_periods": n_periods,
        "n_days": n_days_clust,
        "gross_bps": gross_bps,
        "turnover": turnover,
        "net_4bps": nets[4.0],
        "net_6bps": nets[6.0],
        "net_10bps": nets[10.0],
        "tstat_day_clustered": tstat,
        "canary_mean": canary_mean,
        "canary_std": canary_std,
        "canary_95": canary_95,
        "clears_canary": clears_canary,
    }


# ── Step 8: Main analysis loop ─────────────────────────────────────────────────
print("\n[6] Running H11 momentum analysis (timezone-corrected)...")
all_results: dict = {}

for W in SIGNAL_WINDOWS:
    for H in FWD_HORIZONS:
        sig_col = f"vwap_dev_{W}"
        ret_col = f"fwd_ret_{H}"
        key = f"W{W}_H{H}"
        print(f"\n{'='*60}")
        print(f"  W={W}, H={H}")
        print(f"{'='*60}")

        # Build base panel: all RTH bars with valid signal and fwd return
        # Rebalance on grid: 810, 810+H, 810+2H, ...
        base_panel = (
            bars_sig
            .filter(
                pl.col(sig_col).is_not_null()
                & pl.col(ret_col).is_not_null()
                & (pl.col("utc_minute") < RTH_END_UTC)
                & (pl.col("utc_minute") >= RTH_START_UTC)
            )
            .with_columns([
                (((pl.col("utc_minute") - RTH_START_UTC) / H).floor().cast(pl.Int32)).alias("slot"),
            ])
            .filter(
                pl.col("utc_minute") == (RTH_START_UTC + pl.col("slot") * H)
            )
        )

        n_base = base_panel.shape[0]
        print(f"  Base panel: {n_base:,} obs")
        if n_base > 0:
            slot_dist = base_panel.group_by("utc_minute").agg(pl.col("date").count()).sort("utc_minute")
            print(f"  Slot minute distribution (utc_minute: count):")
            print(slot_dist.head(10))

        # ── RAW MOMENTUM (standard entry, includes 09:30 ET open print = slot 0 at 810) ─
        print(f"\n  --- RAW MOMENTUM (standard entry, includes 09:30 open print at UTC 810) ---")
        raw_res = run_momentum_ls(base_panel, sig_col, ret_col, f"raw W={W} H={H}")

        # ── GATE A: TRADEABLE ENTRY (>=09:35 ET = UTC 815) ───────────────────────────────
        print(f"\n  --- GATE A: TRADEABLE ENTRY (>=13:35 UTC / 09:35 ET) ---")
        tradeable_panel = base_panel.filter(pl.col("utc_minute") >= RTH_TRADEABLE)
        n_trad = tradeable_panel.shape[0]
        print(f"  Tradeable panel: {n_trad:,} obs (removed {n_base - n_trad:,} from open print)")
        tradeable_res = run_momentum_ls(tradeable_panel, sig_col, ret_col, f"tradeable W={W} H={H}")

        # ── GATE B: PER-SYMBOL DEMEAN ────────────────────────────────────────────────────
        print(f"\n  --- GATE B: PER-SYMBOL DEMEAN (survivorship check) ---")
        sym_mean = (
            base_panel
            .group_by("symbol")
            .agg(pl.col(ret_col).mean().alias("sym_mean_ret"))
        )
        demeaned_panel = base_panel.join(sym_mean, on="symbol", how="left").with_columns([
            (pl.col(ret_col) - pl.col("sym_mean_ret")).alias(ret_col + "_dm"),
        ])
        demean_res = run_momentum_ls(demeaned_panel, sig_col, ret_col + "_dm", f"demeaned W={W} H={H}")

        # ── COMBINED: TRADEABLE ENTRY + DEMEAN ──────────────────────────────────────────
        print(f"\n  --- COMBINED: TRADEABLE + DEMEAN ---")
        sym_mean_trad = (
            tradeable_panel
            .group_by("symbol")
            .agg(pl.col(ret_col).mean().alias("sym_mean_ret"))
        )
        trad_dm_panel = tradeable_panel.join(sym_mean_trad, on="symbol", how="left").with_columns([
            (pl.col(ret_col) - pl.col("sym_mean_ret")).alias(ret_col + "_dm"),
        ])
        trad_dm_res = run_momentum_ls(trad_dm_panel, sig_col, ret_col + "_dm", f"trad+demean W={W} H={H}")

        # ── ROBUSTNESS: EXCLUDE OPEN+CLOSE ───────────────────────────────────────────────
        print(f"\n  --- ROBUSTNESS: EXCLUDE FIRST + LAST 30 MIN (10:00-15:30 ET only) ---")
        robust_panel = tradeable_panel.filter(
            (pl.col("utc_minute") >= RTH_EXCL_OPEN_END)
            & (pl.col("utc_minute") < RTH_EXCL_CLOSE_START)
        )
        robust_res = run_momentum_ls(robust_panel, sig_col, ret_col, f"robust W={W} H={H}")

        all_results[key] = {
            "W": W,
            "H": H,
            "raw": raw_res,
            "tradeable_entry": tradeable_res,
            "per_symbol_demean": demean_res,
            "tradeable_plus_demean": trad_dm_res,
            "robust_excl_open_close": robust_res,
        }

# ── Save full results ──────────────────────────────────────────────────────────
with open(OUT / "raw_results_v2.json", "w") as results_file:
    json.dump(all_results, results_file, indent=2)
print(f"\nFull results saved to {OUT}/raw_results_v2.json")

# ── Summary tables ─────────────────────────────────────────────────────────────
print("\n\n=== H11 v2 SUMMARY: RAW MOMENTUM (includes 09:30 open print) ===")
print(f"{'W':>4} {'H':>4} {'Gross':>8} {'Turn':>7} {'Net@4':>8} {'Net@6':>8} {'Net@10':>8} {'t-stat':>8} {'Can95':>8} {'ClearsCan':>10}")
print("-" * 90)
for key, res_dict in sorted(all_results.items()):
    raw = res_dict.get("raw", {})
    if not raw:
        continue
    W = res_dict["W"]
    H = res_dict["H"]
    print(f"{W:>4} {H:>4} {raw['gross_bps']:>8.2f} {raw['turnover']:>7.3f} "
          f"{raw['net_4bps']:>8.2f} {raw['net_6bps']:>8.2f} {raw['net_10bps']:>8.2f} "
          f"{raw['tstat_day_clustered']:>8.2f} {raw['canary_95']:>8.2f} {str(raw['clears_canary']):>10}")

print("\n\n=== H11 v2 GATE A: TRADEABLE ENTRY (>=09:35 ET, open print EXCLUDED) ===")
print(f"{'W':>4} {'H':>4} {'Gross':>8} {'Net@6':>8} {'t-stat':>8} {'ClearsCan':>10}")
print("-" * 55)
for key, res_dict in sorted(all_results.items()):
    te = res_dict.get("tradeable_entry", {})
    if not te:
        continue
    W = res_dict["W"]
    H = res_dict["H"]
    print(f"{W:>4} {H:>4} {te['gross_bps']:>8.2f} {te['net_6bps']:>8.2f} {te['tstat_day_clustered']:>8.2f} {str(te['clears_canary']):>10}")

print("\n\n=== H11 v2 GATE B: PER-SYMBOL DEMEAN ===")
print(f"{'W':>4} {'H':>4} {'DemGross':>10} {'DemNet@6':>10} {'t-stat':>8} {'ClearsCan':>10}")
print("-" * 55)
for key, res_dict in sorted(all_results.items()):
    dm = res_dict.get("per_symbol_demean", {})
    if not dm:
        continue
    W = res_dict["W"]
    H = res_dict["H"]
    print(f"{W:>4} {H:>4} {dm['gross_bps']:>10.2f} {dm['net_6bps']:>10.2f} {dm['tstat_day_clustered']:>8.2f} {str(dm['clears_canary']):>10}")

print("\n\n=== H11 v2 COMBINED: TRADEABLE + DEMEAN (the real test) ===")
print(f"{'W':>4} {'H':>4} {'Gross':>8} {'Net@6':>8} {'t-stat':>8} {'ClearsCan':>10}")
print("-" * 55)
for key, res_dict in sorted(all_results.items()):
    td = res_dict.get("tradeable_plus_demean", {})
    if not td:
        continue
    W = res_dict["W"]
    H = res_dict["H"]
    print(f"{W:>4} {H:>4} {td['gross_bps']:>8.2f} {td['net_6bps']:>8.2f} {td['tstat_day_clustered']:>8.2f} {str(td['clears_canary']):>10}")

print("\n\n=== H11 v2 ROBUSTNESS: EXCL FIRST+LAST 30 MIN (tradeable entry only) ===")
print(f"{'W':>4} {'H':>4} {'Gross':>8} {'Net@6':>8} {'t-stat':>8}")
print("-" * 45)
for key, res_dict in sorted(all_results.items()):
    rob = res_dict.get("robust_excl_open_close", {})
    if not rob:
        continue
    W = res_dict["W"]
    H = res_dict["H"]
    print(f"{W:>4} {H:>4} {rob['gross_bps']:>8.2f} {rob['net_6bps']:>8.2f} {rob['tstat_day_clustered']:>8.2f}")

print("\nDone.")
