"""swing_dc $-CURVE A/B — does ADDING the swing_dc feature group to the trusted-model inputs MOVE the
harness money curve at conservative percentile cuts, vs the SAME trusted-model baseline WITHOUT swing_dc?

This is the deploy-gate test (Thread 1). swing_dc is already a confirmed, replicated MAGNITUDE feature; the
question here is purely $: it should only cost a 728->802 fingerprint change if it improves the money curve.

METHOD (faithful to the harness + prior experiments):
  - Per backfill date: top-`UNIVERSE_TOP` liquid names, single entry minute 09:40 ET (>=09:35 tradeable).
  - BASELINE features = the trusted dense store groups (the tail-importance group list — the trusted-model
    inputs), read DIRECTLY per-date at the entry minute (bypassing the OOM-prone build_intraday_panel).
  - TREATMENT features = BASELINE + swing_dc's 74 (Rust kernel swing_dc_fold on tick-enriched bars).
  - forward-30m RAW return per name (the harness de-means it per timestamp -> cross-sectional excess label).
  - Build ONE Panel per arm over the IDENTICAL row set, then call the EXACT harness path:
    _walk_forward_scores (purged walk-forward GBM, shared CrossSectionalLS decide-core) -> threshold_curve
    -> _money_from_basket + shuffle + predict-zero baselines.
  - Report the {2,5,10}% tail cuts for BOTH arms + the $-delta, with shuffle + predict-zero so the delta is
    honestly bounded. In-sample/OOS + fold count printed.

READ-ONLY store. NEEDS origin/main (swing_dc re-landed, kernel on rust/src/lib.rs). Research-only: NO quantlib edits.
"""

from __future__ import annotations

import datetime as dt
import glob
import os
from zoneinfo import ZoneInfo

import numpy as np
import polars as pl

from quantlib.battery.panel import DEFAULT_HALF_SPREAD_BPS, panel_from_intraday_frame
from quantlib.features.base import BatchContext
from quantlib.features.raw_loaders import load_raw_minute_agg, load_raw_tick_enriched_minute_agg
from quantlib.features.registry import REGISTRY
from quantlib.harness.config import Cadence, HarnessConfig, ModelKind
from quantlib.harness.diagnostics import ThresholdCurve, threshold_curve
from quantlib.harness.labels import forward_excess_label
from quantlib.harness.run import _baseline_curves, _money_from_basket, _walk_forward_scores

OUT_DIR = "/app/experiments/2026-06-20-swing-dc-dollar-test"
STORE = "/store"
ENTRY_ET = 9 * 60 + 40
FWD_MIN = 30
N_DATES = int(os.environ.get("N_DATES", "60"))
UNIVERSE_TOP = int(os.environ.get("UNIVERSE_TOP", "200"))
N_FOLDS = int(os.environ.get("N_FOLDS", "5"))
CUTS = (0.02, 0.05, 0.10, 0.20, 0.33, 0.50)

# The trusted-model dense feature groups (the tail-importance group list = the trusted-model inputs).
TRUSTED_GROUPS = os.environ.get(
    "GROUPS",
    "volatility,realized_range,ohlc_vol,momentum,momentum_run,momentum_consistency,runner_state,efficiency,"
    "trend_quality,swing,candlestick,price_returns,return_dynamics,return_dispersion,distribution,"
    "microstructure_burst,trade_flow,signed_trade_ratio,trade_freq_z,liquidity,liquidity_rank,market_beta,"
    "overnight_intraday_split,gap_fill_state,price_volume,clean_momentum,prior_day,range_expansion,draw_range",
).split(",")


def group_vdir(group: str) -> str | None:
    cand = sorted(glob.glob(f"{STORE}/group={group}/v=*"))
    return cand[-1] if cand else None


def entry_ts(day: str) -> dt.datetime:
    d = dt.date.fromisoformat(day)
    return dt.datetime(
        d.year, d.month, d.day, ENTRY_ET // 60, ENTRY_ET % 60, tzinfo=ZoneInfo("America/New_York")
    ).astimezone(dt.timezone.utc)


def liquid_universe(day: str, n: int) -> list[str]:
    lazy = pl.scan_parquet(f"{STORE}/raw/bars/symbol=*/date={day}/*.parquet", hive_partitioning=True).select(
        ["symbol", "ts", "close", "volume"]
    )
    et = pl.col("ts").dt.convert_time_zone("America/New_York")
    minute = et.dt.hour().cast(pl.Int32) * 60 + et.dt.minute().cast(pl.Int32)
    dv = (
        lazy.filter((minute >= 9 * 60 + 30) & (minute < 16 * 60))
        .group_by("symbol")
        .agg((pl.col("close") * pl.col("volume")).sum().alias("dv"))
        .sort("dv", descending=True)
        .head(n)
        .collect()
    )
    return dv["symbol"].to_list()


def trusted_features(day: str, symbols: list[str], vdirs: dict[str, str], et: dt.datetime) -> pl.DataFrame:
    """Read each trusted group's features at the entry minute, joined per symbol."""
    uni = set(symbols)
    feat: pl.DataFrame | None = None
    for group, vdir in vdirs.items():
        paths = glob.glob(f"{vdir}/source=backfill/date={day}/*.parquet")
        if not paths:
            continue
        df = pl.read_parquet(paths[0])
        if "minute" not in df.columns:
            continue
        df = (
            df.filter(pl.col("symbol").is_in(uni) & (pl.col("minute") >= et))
            .sort(["symbol", "minute"])
            .group_by("symbol")
            .first()
        )
        keep = ["symbol"] + [c for c in df.columns if c not in ("symbol", "minute")]
        df = df.select(keep)
        feat = df if feat is None else feat.join(df, on="symbol", how="outer_coalesce")
    return feat if feat is not None else pl.DataFrame()


def swing_dc_features(enr: pl.DataFrame, et: dt.datetime) -> pl.DataFrame:
    """swing_dc group at the entry minute (first row at/after entry per symbol)."""
    feat = REGISTRY.get_group("swing_dc").compute(BatchContext(frames={"minute_agg": enr}))
    feat_cols = [c for c in feat.columns if c not in ("symbol", "minute")]
    at = feat.filter(pl.col("minute") >= et).sort(["symbol", "minute"]).group_by("symbol").first()
    return at.select(["symbol", *feat_cols])


def build_date(day: str, vdirs: dict[str, str]) -> tuple[pl.DataFrame, list[str], list[str]] | None:
    """Returns (row frame with BOTH trusted+swing_dc feature cols + label/panel cols, trusted_cols, swing_cols)."""
    syms = liquid_universe(day, UNIVERSE_TOP)
    if len(syms) < 40:
        return None
    et = entry_ts(day)
    bars = load_raw_minute_agg(STORE, day, syms)
    if bars.height == 0:
        return None

    trusted = trusted_features(day, syms, vdirs, et)
    if trusted.height == 0:
        return None
    trusted_cols = [c for c in trusted.columns if c != "symbol"]

    enr = load_raw_tick_enriched_minute_agg(STORE, day, syms, bars)
    swing = swing_dc_features(enr, et)
    swing_cols = [c for c in swing.columns if c != "symbol"]

    # panel scaffolding columns from raw bars at the entry minute + forward-30m raw return + OHLC/vol
    bsort = bars.sort(["symbol", "minute"])
    entry = (
        bsort.filter(pl.col("minute") >= et)
        .group_by("symbol")
        .first()
        .select(
            symbol="symbol",
            minute="minute",
            entry_close="close",
            entry_volume="volume",
            rth_high="high",
            rth_low="low",
        )
    )
    fwd = (
        bsort.filter(pl.col("minute") >= et + dt.timedelta(minutes=FWD_MIN))
        .group_by("symbol")
        .first()
        .select(["symbol", pl.col("close").alias("_cf")])
    )
    scaffold = entry.join(fwd, on="symbol", how="left").with_columns(
        pl.when((pl.col("entry_close") >= 1.0) & (pl.col("_cf") >= 1.0))
        .then(pl.col("_cf") / pl.col("entry_close") - 1.0)
        .otherwise(None)
        .alias("fwd_30m")
    )

    row = (
        scaffold.join(trusted, on="symbol", how="inner")
        .join(swing, on="symbol", how="inner")
        .with_columns(
            pl.lit(DEFAULT_HALF_SPREAD_BPS).alias("half_spread_bps"),
            pl.col("entry_close").alias("rth_close"),
        )
    )
    return row, trusted_cols, swing_cols


def make_panel(panel_df: pl.DataFrame, feature_cols: list[str]):
    """Build the harness Panel for a given feature subset over the shared row frame."""
    cols = [
        "symbol",
        "minute",
        "entry_close",
        "entry_volume",
        "rth_high",
        "rth_low",
        "rth_close",
        "half_spread_bps",
        "fwd_30m",
        *feature_cols,
    ]
    sub = panel_df.select(cols)
    panel = panel_from_intraday_frame(sub, feature_cols)
    panel.extra["rth_close"] = sub.sort(["symbol", "minute"])["rth_close"].to_numpy().astype(float)
    return panel


def run_arm(panel_df: pl.DataFrame, feature_cols: list[str], label_name: str):
    config = HarnessConfig(
        cadence=Cadence.INTRADAY,
        label_horizon_min=FWD_MIN,
        model=ModelKind.GBM,
        n_folds=N_FOLDS,
        long_short_frac=0.10,
        capital=1_000_000.0,
        percentile_cuts=CUTS,
        min_train_rows=int(os.environ.get("MIN_TRAIN_ROWS", "400")),
        min_test_rows=int(os.environ.get("MIN_TEST_ROWS", "50")),
    )
    panel = make_panel(panel_df, feature_cols)
    label = forward_excess_label(panel, horizon_days=1, horizon_min=FWD_MIN)
    scores, labels, groups, symbols, spreads = _walk_forward_scores(config, panel, label)
    n_oos_ts = len({str(g) for g in groups})
    curve = threshold_curve(
        scores, labels, groups, symbols, spreads,
        cuts=CUTS, capital=config.capital, cost_mult=config.cost_mult,
        slippage_bps=config.slippage_bps, borrow_bps_annual=config.borrow_bps_annual,
        periods_per_year=config.periods_per_year,
    )
    money = _money_from_basket(scores, labels, groups, symbols, spreads, config)
    shuffle_curve, predict_zero = _baseline_curves(scores, labels, groups, symbols, spreads, config)
    print(
        f"[{label_name}] n_feats={len(feature_cols)} oos_rows={len(scores)} oos_ts(days)={n_oos_ts} "
        f"AUC={curve.auc:.4f} rankIC={curve.rank_ic:+.4f} headline10%_$P&L={money.total_pnl:+,.0f}",
        flush=True,
    )
    return curve, shuffle_curve, money, n_oos_ts, len(scores)


def fmt_cut(cut) -> str:
    return (
        f"  {cut.frac*100:>4.0f}% | prec={cut.directional_precision:.4f} "
        f"$/trade={cut.dollar_per_trade:>+10,.0f} total=${cut.total_dollar_pnl:>+14,.0f} "
        f"Sharpe={cut.sharpe_net:>+6.2f} n={cut.n_trades}"
    )


def covered_days(vdir: str, min_syms: int = 500) -> list[str]:
    """Dates whose trusted store has adequate symbol coverage (the recent capture-outage tail —
    06-15..06-18 — collapses to <100 syms; exclude it so the panel isn't dominated by broken dates)."""
    days = sorted(
        os.path.basename(p).replace("date=", "")
        for p in glob.glob(f"{vdir}/source=backfill/date=*")
    )
    good = []
    for day in days:
        paths = glob.glob(f"{vdir}/source=backfill/date={day}/*.parquet")
        if not paths:
            continue
        n_syms = pl.read_parquet(paths[0], columns=["symbol"])["symbol"].n_unique()
        if n_syms >= min_syms:
            good.append(day)
    return good


def main() -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    vdirs = {g: group_vdir(g) for g in TRUSTED_GROUPS if group_vdir(g)}
    days = covered_days(vdirs["volatility"], min_syms=500)[-N_DATES:]
    print(
        f"swing_dc $-test: {len(days)} well-covered dates {days[0]}..{days[-1]}, top-{UNIVERSE_TOP} liquid, "
        f"{len(vdirs)} trusted groups, {N_FOLDS} folds",
        flush=True,
    )
    frames, trusted_cols, swing_cols = [], None, None
    for i, day in enumerate(days):
        built = build_date(day, vdirs)
        if built is None:
            continue
        row, tcols, scols = built
        if row.height:
            frames.append(row)
            trusted_cols, swing_cols = tcols, scols
        if (i + 1) % 10 == 0:
            print(f"  built {i+1}/{len(days)} ({sum(f.height for f in frames)} rows)", flush=True)

    panel_df = pl.concat(frames, how="diagonal_relaxed")
    # cast all feature cols to float (LightGBM-NaN-tolerant); keep NaNs.
    feat_all = [c for c in (trusted_cols + swing_cols) if c in panel_df.columns]
    panel_df = panel_df.with_columns([pl.col(c).cast(pl.Float64, strict=False) for c in feat_all])
    trusted_present = [c for c in trusted_cols if c in panel_df.columns]
    swing_present = [c for c in swing_cols if c in panel_df.columns]
    print(
        f"panel: {panel_df.height} rows, {panel_df['minute'].n_unique()} entry-ts(days), "
        f"{len(trusted_present)} trusted feats, {len(swing_present)} swing_dc feats",
        flush=True,
    )
    panel_df.write_parquet(f"{OUT_DIR}/panel.parquet")

    print("\n==================== ARM A: TRUSTED BASELINE (no swing_dc) ====================")
    base_curve, base_shuf, base_money, base_ts, base_rows = run_arm(panel_df, trusted_present, "BASELINE")
    print("\n==================== ARM B: TRUSTED + swing_dc ====================")
    trt_curve, trt_shuf, trt_money, trt_ts, trt_rows = run_arm(
        panel_df, trusted_present + swing_present, "TRUSTED+swing_dc"
    )

    def cut_by_frac(curve: ThresholdCurve, frac: float):
        return next(c for c in curve.cuts if abs(c.frac - frac) < 1e-9)

    print("\n==================== ⭐ $-CURVE COMPARISON (the deploy gate) ====================")
    print("Walk-forward GBM, OOS test folds only. Per-name half-spread + slippage + borrow cost net.\n")
    for label, curve, shuf in [
        ("ARM A  BASELINE         ", base_curve, base_shuf),
        ("ARM B  TRUSTED+swing_dc  ", trt_curve, trt_shuf),
    ]:
        print(f"{label} (AUC={curve.auc:.4f}  rankIC={curve.rank_ic:+.4f}):")
        for cut in curve.cuts:
            print(fmt_cut(cut))
        if shuf is not None:
            print("    shuffle baseline (same cuts):")
            for cut in shuf.cuts:
                print("  " + fmt_cut(cut).strip())
        print()

    print("==================== Δ (ARM B − ARM A) at the conservative tail cuts ====================")
    print(f"{'cut':>5} | {'Δ total $':>16} | {'Δ $/trade':>12} | {'Δ prec':>8} | {'Δ Sharpe':>9} | "
          f"{'A total $':>14} | {'B total $':>14}")
    for frac in (0.02, 0.05, 0.10):
        a = cut_by_frac(base_curve, frac)
        b = cut_by_frac(trt_curve, frac)
        print(
            f"{frac*100:>4.0f}% | {b.total_dollar_pnl - a.total_dollar_pnl:>+16,.0f} | "
            f"{b.dollar_per_trade - a.dollar_per_trade:>+12,.0f} | "
            f"{b.directional_precision - a.directional_precision:>+8.4f} | "
            f"{b.sharpe_net - a.sharpe_net:>+9.2f} | {a.total_dollar_pnl:>+14,.0f} | {b.total_dollar_pnl:>+14,.0f}"
        )
    print(
        f"\npredict-zero baseline $P&L = $0 (both arms).  "
        f"BASELINE headline-10% $={base_money.total_pnl:+,.0f}  Sharpe={base_money.sharpe_net:+.2f}; "
        f"+swing_dc headline-10% $={trt_money.total_pnl:+,.0f}  Sharpe={trt_money.sharpe_net:+.2f}"
    )
    print(f"OOS rows: A={base_rows} B={trt_rows}; OOS test-fold timestamps(days): A={base_ts} B={trt_ts}")


if __name__ == "__main__":
    main()
