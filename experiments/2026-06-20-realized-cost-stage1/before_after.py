"""Stage 1 before/after: re-book a prior verdict's harness $-curve under the FLAT stub vs the REALIZED
per-name half-spread measured from the quote tape — confirming accurate cost makes the verdicts MORE-null
(strictly lower net-$), per the G0 finding that the 3.0bps stub undercharges ~2.6x.

Reuses the swing_dc panel from PR #259's eval (the trusted baseline + swing_dc A/B) if present; else rebuilds
a trusted-baseline panel directly per-date (same pattern as the prior evals). For each arm we book the SAME
walk-forward GBM scores two ways: (1) flat DEFAULT_HALF_SPREAD_BPS, (2) realized tape half-spread at the
entry instant. We report the headline-10% and the {2,5,10}% threshold-cut $ under each cost.

READ-ONLY store. origin/main + this branch's realized_cost module. Research-only: experiments/ only.
"""

from __future__ import annotations

import datetime as dt
import glob
import os
from zoneinfo import ZoneInfo

import numpy as np
import polars as pl

from quantlib.battery.panel import DEFAULT_HALF_SPREAD_BPS, panel_from_intraday_frame
from quantlib.data.realized_cost import realized_half_spread_bps
from quantlib.features.raw_loaders import load_raw_minute_agg
from quantlib.harness.config import Cadence, HarnessConfig, ModelKind
from quantlib.harness.diagnostics import threshold_curve
from quantlib.harness.labels import forward_excess_label
from quantlib.harness.run import _money_from_basket, _walk_forward_scores

STORE = "/store"
ENTRY_ET = 9 * 60 + 40
FWD_MIN = 30
N_DATES = int(os.environ.get("N_DATES", "42"))
UNIVERSE_TOP = int(os.environ.get("UNIVERSE_TOP", "200"))
N_FOLDS = int(os.environ.get("N_FOLDS", "5"))
CUTS = (0.02, 0.05, 0.10, 0.20, 0.33, 0.50)
TRUSTED_GROUPS = (
    "volatility,ohlc_vol,momentum,momentum_run,momentum_consistency,runner_state,efficiency,"
    "trend_quality,swing,candlestick,price_returns,return_dynamics,return_dispersion,distribution,"
    "microstructure_burst,trade_flow,signed_trade_ratio,trade_freq_z,liquidity,liquidity_rank,market_beta,"
    "overnight_intraday_split,gap_fill_state,price_volume,clean_momentum,prior_day"
).split(",")


def group_vdir(group: str) -> str | None:
    cand = sorted(glob.glob(f"{STORE}/group={group}/v=*"))
    return cand[-1] if cand else None


def entry_ts(day: str) -> dt.datetime:
    d = dt.date.fromisoformat(day)
    return dt.datetime(
        d.year, d.month, d.day, ENTRY_ET // 60, ENTRY_ET % 60, tzinfo=ZoneInfo("America/New_York")
    ).astimezone(dt.timezone.utc)


def covered_days(vdir: str, min_syms: int = 500) -> list[str]:
    days = sorted(
        os.path.basename(p).replace("date=", "") for p in glob.glob(f"{vdir}/source=backfill/date=*")
    )
    good = []
    for day in days:
        paths = glob.glob(f"{vdir}/source=backfill/date={day}/*.parquet")
        if paths and pl.read_parquet(paths[0], columns=["symbol"])["symbol"].n_unique() >= min_syms:
            good.append(day)
    return good


def liquid_universe(day: str, n: int) -> list[str]:
    lazy = pl.scan_parquet(f"{STORE}/raw/bars/symbol=*/date={day}/*.parquet", hive_partitioning=True).select(
        ["symbol", "ts", "close", "volume"]
    )
    et = pl.col("ts").dt.convert_time_zone("America/New_York")
    minute = et.dt.hour().cast(pl.Int32) * 60 + et.dt.minute().cast(pl.Int32)
    return (
        lazy.filter((minute >= 9 * 60 + 30) & (minute < 16 * 60))
        .group_by("symbol")
        .agg((pl.col("close") * pl.col("volume")).sum().alias("dv"))
        .sort("dv", descending=True)
        .head(n)
        .collect()["symbol"]
        .to_list()
    )


def trusted_features(day: str, symbols: list[str], vdirs: dict[str, str], et: dt.datetime) -> pl.DataFrame:
    uni = set(symbols)
    feat: pl.DataFrame | None = None
    for _, vdir in vdirs.items():
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
        df = df.select(["symbol"] + [c for c in df.columns if c not in ("symbol", "minute")])
        feat = df if feat is None else feat.join(df, on="symbol", how="full", coalesce=True)
    return feat if feat is not None else pl.DataFrame()


def build_date(day: str, vdirs: dict[str, str]) -> tuple[pl.DataFrame, list[str]] | None:
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
    realized = realized_half_spread_bps(STORE, day, syms, et)  # the Stage-1 measured cost

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
        .join(realized, on="symbol", how="left")
        .with_columns(pl.col("entry_close").alias("rth_close"))
    )
    return row, trusted_cols


def make_panel(panel_df: pl.DataFrame, feature_cols: list[str], half_spread_expr: pl.Expr):
    base = ["symbol", "minute", "entry_close", "entry_volume", "rth_high", "rth_low", "rth_close", "fwd_30m"]
    sub = panel_df.select(
        base + [c for c in feature_cols if c not in base] + ["realized_half_spread_bps"]
    ).with_columns(half_spread_expr.alias("half_spread_bps"))
    panel = panel_from_intraday_frame(sub, feature_cols)
    panel.extra["rth_close"] = sub.sort(["symbol", "minute"])["rth_close"].to_numpy().astype(float)
    return panel


def book(panel_df: pl.DataFrame, feature_cols: list[str], half_spread_expr: pl.Expr, tag: str):
    config = HarnessConfig(
        cadence=Cadence.INTRADAY,
        label_horizon_min=FWD_MIN,
        model=ModelKind.GBM,
        n_folds=N_FOLDS,
        long_short_frac=0.10,
        capital=1_000_000.0,
        percentile_cuts=CUTS,
        min_train_rows=600,
        min_test_rows=50,
    )
    panel = make_panel(panel_df, feature_cols, half_spread_expr)
    label = forward_excess_label(panel, horizon_days=1, horizon_min=FWD_MIN)
    scores, labels, groups, symbols, spreads = _walk_forward_scores(config, panel, label)
    curve = threshold_curve(
        scores,
        labels,
        groups,
        symbols,
        spreads,
        cuts=CUTS,
        capital=config.capital,
        cost_mult=config.cost_mult,
        slippage_bps=config.slippage_bps,
        borrow_bps_annual=config.borrow_bps_annual,
        periods_per_year=config.periods_per_year,
    )
    money = _money_from_basket(scores, labels, groups, symbols, spreads, config)
    med_cost = float(np.nanmedian([s for s in spreads if s == s]))
    print(
        f"  [{tag}] median half-spread={med_cost:.2f}bps  headline10% $={money.total_pnl:+,.0f}  "
        f"Sharpe={money.sharpe_net:+.2f}",
        flush=True,
    )
    return curve, money


def main() -> None:
    vdirs = {g: group_vdir(g) for g in TRUSTED_GROUPS if group_vdir(g)}
    days = covered_days(vdirs["volatility"], min_syms=500)[-N_DATES:]
    print(
        f"Stage-1 before/after: {len(days)} dates {days[0]}..{days[-1]}, top-{UNIVERSE_TOP}, {N_FOLDS} folds",
        flush=True,
    )
    frames, trusted_cols = [], None
    for i, day in enumerate(days):
        built = build_date(day, vdirs)
        if built is None:
            continue
        row, tcols = built
        if row.height:
            frames.append(row)
            trusted_cols = tcols
        if (i + 1) % 10 == 0:
            print(f"  built {i+1}/{len(days)}", flush=True)
    panel_df = pl.concat(frames, how="diagonal_relaxed")
    feats = [c for c in trusted_cols if c in panel_df.columns]
    panel_df = panel_df.with_columns([pl.col(c).cast(pl.Float64, strict=False) for c in feats])
    n_real = int(panel_df["realized_half_spread_bps"].is_not_null().sum())
    print(
        f"panel: {panel_df.height} rows, {panel_df['minute'].n_unique()} days, {len(feats)} feats; "
        f"realized cost measured for {n_real}/{panel_df.height} rows "
        f"(median {float(panel_df['realized_half_spread_bps'].median()):.2f}bps vs flat {DEFAULT_HALF_SPREAD_BPS}bps)",
        flush=True,
    )

    flat = pl.lit(DEFAULT_HALF_SPREAD_BPS)
    realized = pl.col("realized_half_spread_bps").clip(0.1, 100.0).fill_null(DEFAULT_HALF_SPREAD_BPS)
    print(
        "\n========== ⭐ BEFORE/AFTER: trusted-baseline harness $-curve, FLAT stub vs REALIZED tape cost =========="
    )
    print("FLAT (DEFAULT_HALF_SPREAD_BPS=3.0):")
    flat_curve, flat_money = book(panel_df, feats, flat, "FLAT-stub")
    print("REALIZED (per-name tape half-spread):")
    real_curve, real_money = book(panel_df, feats, realized, "REALIZED")

    def cut(curve, frac):
        return next(c for c in curve.cuts if abs(c.frac - frac) < 1e-9)

    print(
        f"\n{'cut':>5} | {'FLAT $':>14} | {'REALIZED $':>14} | {'Δ (real-flat)':>14} | {'FLAT Sh':>8} | {'REAL Sh':>8}"
    )
    for frac in (0.02, 0.05, 0.10):
        a, b = cut(flat_curve, frac), cut(real_curve, frac)
        print(
            f"{frac*100:>4.0f}% | {a.total_dollar_pnl:>+14,.0f} | {b.total_dollar_pnl:>+14,.0f} | "
            f"{b.total_dollar_pnl-a.total_dollar_pnl:>+14,.0f} | {a.sharpe_net:>+8.2f} | {b.sharpe_net:>+8.2f}"
        )
    haircut = (
        100 * (1 - real_money.total_pnl / flat_money.total_pnl) if flat_money.total_pnl else float("nan")
    )
    print(
        f"\nheadline-10%: FLAT ${flat_money.total_pnl:+,.0f} -> REALIZED ${real_money.total_pnl:+,.0f} "
        f"({haircut:+.0f}% change). Realized cost makes the verdict MORE-null where it undercharged the truth."
    )


if __name__ == "__main__":
    main()
