"""G0 — the CHEAP EARLY $-SCREEN for the path-geometry thesis (Lead amendment): run the binding constraint
(incremental-$-over-the-FULL-baseline) FIRST, on THROWAWAY eval-time proxies, BEFORE any production group or
kernel work. If the proxy moves the money curve -> proceed to G1-G6 + the build; if it $-nulls like swing_dc
-> publish the null, do NOT build, trigger the §6 pivot to the quote/tape microstructure.

The proxies (computed inline per-date from trailing minute bars at the 09:40 ET entry — NO production code,
NO kernel; this is exactly the G7 question asked with disposable construction):

  (A) HOLDER / generalized-Hurst roughness: over a fixed geometric tau ladder {2,4,8,16,32,64} min, the mean
      absolute aggregated log-return at each tau over the trailing W=120m; slope of log(meanabs(tau)) vs
      log(tau) = the roughness exponent H. own-vol-normalized by construction (a slope of a log-log ratio is
      scale-free). Plus the regression R^2 (single-power-law cleanliness).
  (B) DIRECTIONAL PATH-ASYMMETRY: decompose the trailing window into up-legs / down-legs by the sign of a
      short-smoothed move (a lightweight zigzag on a vol-scaled threshold), compare median |slope| and median
      duration up vs down. slope_asym, dur_asym, leg_count_asym. Causal (reads only bars <= T).

A/B via the EXACT Thread-1 harness path (reuse eval_dollar's machinery): FULL trusted baseline vs
baseline + these proxies, walk-forward GBM, threshold curve at {2,5,10}% + shuffle + predict-zero. The
baseline here EXPLICITLY contains the return-shape + volatility groups (G2-sharpened: redundancy is against
the baseline, not against swing_dc alone).

READ-ONLY store. origin/main. Research-only: experiments/ only, NO quantlib changes.
"""

from __future__ import annotations

import datetime as dt
import glob
import os
from zoneinfo import ZoneInfo

import numpy as np
import polars as pl

from quantlib.battery.panel import DEFAULT_HALF_SPREAD_BPS, panel_from_intraday_frame
from quantlib.features.raw_loaders import load_raw_minute_agg
from quantlib.harness.config import Cadence, HarnessConfig, ModelKind
from quantlib.harness.diagnostics import ThresholdCurve, threshold_curve
from quantlib.harness.labels import forward_excess_label
from quantlib.harness.run import _baseline_curves, _money_from_basket, _walk_forward_scores

OUT_DIR = "/app/experiments/2026-06-20-path-geometry"
STORE = "/store"
ENTRY_ET = 9 * 60 + 40
FWD_MIN = 30
W_MIN = 120  # trailing window for the path geometry
TAU_LADDER = (2, 4, 8, 16, 32, 64)  # FIXED — no sweeping (FDR honesty)
ZIGZAG_K = 1.0  # vol-scaled zigzag threshold = K * sigma over the window
N_DATES = int(os.environ.get("N_DATES", "42"))
UNIVERSE_TOP = int(os.environ.get("UNIVERSE_TOP", "200"))
N_FOLDS = int(os.environ.get("N_FOLDS", "5"))
CUTS = (0.02, 0.05, 0.10, 0.20, 0.33, 0.50)

TRUSTED_GROUPS = os.environ.get(
    "GROUPS",
    "volatility,ohlc_vol,momentum,momentum_run,momentum_consistency,runner_state,efficiency,"
    "trend_quality,swing,candlestick,price_returns,return_dynamics,return_dispersion,distribution,"
    "microstructure_burst,trade_flow,signed_trade_ratio,trade_freq_z,liquidity,liquidity_rank,market_beta,"
    "overnight_intraday_split,gap_fill_state,price_volume,clean_momentum,prior_day",
).split(",")

PROXY_COLS = ["pg_holder_h", "pg_holder_r2", "pg_slope_asym", "pg_dur_asym", "pg_legcount_asym"]


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
        os.path.basename(p).replace("date=", "")
        for p in glob.glob(f"{vdir}/source=backfill/date=*")
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
    dv = (
        lazy.filter((minute >= 9 * 60 + 30) & (minute < 16 * 60))
        .group_by("symbol")
        .agg((pl.col("close") * pl.col("volume")).sum().alias("dv"))
        .sort("dv", descending=True)
        .head(n)
        .collect()
    )
    return dv["symbol"].to_list()


def holder_and_asym(closes: np.ndarray) -> tuple[float, float, float, float, float]:
    """The two proxies from one symbol's trailing close path (causal; closes[-1] == the entry bar).
    Returns (holder_h, holder_r2, slope_asym, dur_asym, legcount_asym); NaN where undefined."""
    n = closes.shape[0]
    if n < TAU_LADDER[-1] + 2:
        return (np.nan, np.nan, np.nan, np.nan, np.nan)
    logp = np.log(closes)
    lr = np.diff(logp)  # 1-min log returns
    sigma = float(np.std(lr)) if lr.shape[0] > 1 else np.nan

    # (A) Holder: meanabs of tau-aggregated log returns vs tau, log-log slope.
    xs, ys = [], []
    for tau in TAU_LADDER:
        if lr.shape[0] < tau:
            continue
        # non-overlapping tau-sums of 1-min log returns
        m = (lr.shape[0] // tau) * tau
        agg = lr[:m].reshape(-1, tau).sum(axis=1)
        ma = float(np.mean(np.abs(agg)))
        if ma > 0:
            xs.append(np.log(tau))
            ys.append(np.log(ma))
    holder_h, holder_r2 = np.nan, np.nan
    if len(xs) >= 3:
        xa, ya = np.array(xs), np.array(ys)
        slope, intercept = np.polyfit(xa, ya, 1)
        holder_h = float(slope)
        pred = slope * xa + intercept
        ss_res = float(np.sum((ya - pred) ** 2))
        ss_tot = float(np.sum((ya - ya.mean()) ** 2))
        holder_r2 = 1.0 - ss_res / ss_tot if ss_tot > 1e-18 else np.nan

    # (B) Asymmetry: vol-scaled Directional-Change leg decomposition over the window (causal, no repaint).
    slope_asym = dur_asym = legcount_asym = np.nan
    if not (np.isnan(sigma) or sigma <= 0):
        theta = ZIGZAG_K * sigma * np.sqrt(TAU_LADDER[1])  # ~2 per-min-sigma reversal confirms a leg
        up_slopes, dn_slopes, up_durs, dn_durs = [], [], [], []
        # DC state machine: cur_dir = direction of the CONFIRMED current leg (+1 up, -1 down). Seed the
        # initial direction from the first theta-move. ext = running extreme of the current leg; a reversal
        # of >= theta from ext confirms the leg anchor..ext and flips direction (the new leg starts at ext).
        cur_dir = 0
        anchor_i, anchor_p = 0, logp[0]
        ext_i, ext_p = 0, logp[0]
        for i in range(1, n):
            price = logp[i]
            if cur_dir == 0:
                if price >= anchor_p + theta:
                    cur_dir, ext_i, ext_p = 1, i, price
                elif price <= anchor_p - theta:
                    cur_dir, ext_i, ext_p = -1, i, price
                continue
            if cur_dir == 1:
                if price > ext_p:
                    ext_i, ext_p = i, price
                elif price <= ext_p - theta:  # reversal confirms the completed UP leg anchor..ext
                    dur = ext_i - anchor_i
                    if dur > 0:
                        up_slopes.append((ext_p - anchor_p) / dur)
                        up_durs.append(dur)
                    anchor_i, anchor_p = ext_i, ext_p
                    cur_dir, ext_i, ext_p = -1, i, price
            else:  # cur_dir == -1
                if price < ext_p:
                    ext_i, ext_p = i, price
                elif price >= ext_p + theta:  # reversal confirms the completed DOWN leg anchor..ext
                    dur = ext_i - anchor_i
                    if dur > 0:
                        dn_slopes.append((anchor_p - ext_p) / dur)
                        dn_durs.append(dur)
                    anchor_i, anchor_p = ext_i, ext_p
                    cur_dir, ext_i, ext_p = 1, i, price
        if up_slopes and dn_slopes:
            mu_s, md_s = np.median(up_slopes), np.median(dn_slopes)
            slope_asym = float((mu_s - md_s) / (mu_s + md_s)) if (mu_s + md_s) > 1e-18 else np.nan
            mu_d, md_d = np.median(up_durs), np.median(dn_durs)
            dur_asym = float((mu_d - md_d) / (mu_d + md_d)) if (mu_d + md_d) > 0 else np.nan
            nu, nd = len(up_slopes), len(dn_slopes)
            legcount_asym = float((nu - nd) / (nu + nd)) if (nu + nd) > 0 else np.nan
    return (holder_h, holder_r2, slope_asym, dur_asym, legcount_asym)


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


def proxy_features(day: str, symbols: list[str], bars: pl.DataFrame, et: dt.datetime) -> pl.DataFrame:
    """The two throwaway path-geometry proxies per symbol from the trailing W-min close path <= entry."""
    win = bars.filter((pl.col("minute") <= et) & (pl.col("minute") > et - dt.timedelta(minutes=W_MIN)))
    rows = []
    for sym, grp in win.sort(["symbol", "minute"]).group_by("symbol"):
        closes = grp["close"].to_numpy().astype(float)
        h, r2, sa, da, la = holder_and_asym(closes)
        rows.append(
            {
                "symbol": sym[0] if isinstance(sym, tuple) else sym,
                "pg_holder_h": h,
                "pg_holder_r2": r2,
                "pg_slope_asym": sa,
                "pg_dur_asym": da,
                "pg_legcount_asym": la,
            }
        )
    return pl.DataFrame(rows) if rows else pl.DataFrame()


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
    proxies = proxy_features(day, syms, bars, et)
    if proxies.height == 0:
        return None

    bsort = bars.sort(["symbol", "minute"])
    entry = (
        bsort.filter(pl.col("minute") >= et)
        .group_by("symbol")
        .first()
        .select(
            symbol="symbol", minute="minute", entry_close="close",
            entry_volume="volume", rth_high="high", rth_low="low",
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
        .join(proxies, on="symbol", how="inner")
        .with_columns(
            pl.lit(DEFAULT_HALF_SPREAD_BPS).alias("half_spread_bps"),
            pl.col("entry_close").alias("rth_close"),
        )
    )
    return row, trusted_cols


def make_panel(panel_df: pl.DataFrame, feature_cols: list[str]):
    cols = [
        "symbol", "minute", "entry_close", "entry_volume", "rth_high", "rth_low",
        "rth_close", "half_spread_bps", "fwd_30m", *feature_cols,
    ]
    sub = panel_df.select(cols)
    panel = panel_from_intraday_frame(sub, feature_cols)
    panel.extra["rth_close"] = sub.sort(["symbol", "minute"])["rth_close"].to_numpy().astype(float)
    return panel


def run_arm(panel_df: pl.DataFrame, feature_cols: list[str], label_name: str):
    config = HarnessConfig(
        cadence=Cadence.INTRADAY, label_horizon_min=FWD_MIN, model=ModelKind.GBM, n_folds=N_FOLDS,
        long_short_frac=0.10, capital=1_000_000.0, percentile_cuts=CUTS,
        min_train_rows=int(os.environ.get("MIN_TRAIN_ROWS", "600")), min_test_rows=50,
    )
    panel = make_panel(panel_df, feature_cols)
    label = forward_excess_label(panel, horizon_days=1, horizon_min=FWD_MIN)
    scores, labels, groups, symbols, spreads = _walk_forward_scores(config, panel, label)
    curve = threshold_curve(
        scores, labels, groups, symbols, spreads, cuts=CUTS, capital=config.capital,
        cost_mult=config.cost_mult, slippage_bps=config.slippage_bps,
        borrow_bps_annual=config.borrow_bps_annual, periods_per_year=config.periods_per_year,
    )
    money = _money_from_basket(scores, labels, groups, symbols, spreads, config)
    shuffle_curve, _ = _baseline_curves(scores, labels, groups, symbols, spreads, config)
    print(
        f"[{label_name}] n_feats={len(feature_cols)} oos_rows={len(scores)} "
        f"AUC={curve.auc:.4f} rankIC={curve.rank_ic:+.4f} headline10%_$={money.total_pnl:+,.0f}",
        flush=True,
    )
    return curve, shuffle_curve, money, len(scores), len({str(g) for g in groups})


def fmt_cut(cut) -> str:
    return (
        f"  {cut.frac*100:>4.0f}% | prec={cut.directional_precision:.4f} "
        f"$/trade={cut.dollar_per_trade:>+10,.0f} total=${cut.total_dollar_pnl:>+14,.0f} "
        f"Sharpe={cut.sharpe_net:>+6.2f} n={cut.n_trades}"
    )


def main() -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    vdirs = {g: group_vdir(g) for g in TRUSTED_GROUPS if group_vdir(g)}
    days = covered_days(vdirs["volatility"], min_syms=500)[-N_DATES:]
    print(
        f"G0 $-screen: {len(days)} well-covered dates {days[0]}..{days[-1]}, top-{UNIVERSE_TOP} liquid, "
        f"{len(vdirs)} trusted groups, {N_FOLDS} folds, proxies={PROXY_COLS}",
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
            print(f"  built {i+1}/{len(days)} ({sum(f.height for f in frames)} rows)", flush=True)

    panel_df = pl.concat(frames, how="diagonal_relaxed")
    feat_all = [c for c in (trusted_cols + PROXY_COLS) if c in panel_df.columns]
    panel_df = panel_df.with_columns([pl.col(c).cast(pl.Float64, strict=False) for c in feat_all])
    trusted_present = [c for c in trusted_cols if c in panel_df.columns]
    proxy_present = [c for c in PROXY_COLS if c in panel_df.columns]
    # proxy coverage diagnostic
    dens = {c: float(panel_df[c].is_not_null().mean()) for c in proxy_present}
    print(
        f"panel: {panel_df.height} rows, {panel_df['minute'].n_unique()} entry-ts(days), "
        f"{len(trusted_present)} trusted feats, {len(proxy_present)} proxy feats",
        flush=True,
    )
    print("  proxy non-null density:", {k: f"{v:.1%}" for k, v in dens.items()}, flush=True)
    panel_df.write_parquet(f"{OUT_DIR}/g0_panel.parquet")

    print("\n========== ARM A: FULL TRUSTED BASELINE (incl. return-shape + vol groups) ==========")
    base_curve, base_shuf, base_money, base_rows, base_ts = run_arm(panel_df, trusted_present, "BASELINE")
    print("\n========== ARM B: BASELINE + path-geometry PROXIES ==========")
    trt_curve, trt_shuf, trt_money, trt_rows, trt_ts = run_arm(
        panel_df, trusted_present + proxy_present, "BASELINE+pathgeom"
    )

    def cut_by_frac(curve: ThresholdCurve, frac: float):
        return next(c for c in curve.cuts if abs(c.frac - frac) < 1e-9)

    print("\n========== ⭐ G0 $-CURVE COMPARISON (the binding constraint, screened FIRST) ==========")
    for label, curve, shuf in [
        ("ARM A  BASELINE          ", base_curve, base_shuf),
        ("ARM B  BASELINE+pathgeom  ", trt_curve, trt_shuf),
    ]:
        print(f"{label} (AUC={curve.auc:.4f}  rankIC={curve.rank_ic:+.4f}):")
        for cut in curve.cuts:
            print(fmt_cut(cut))
        if shuf is not None:
            print("    shuffle baseline:")
            for cut in shuf.cuts:
                print("  " + fmt_cut(cut).strip())
        print()

    print("========== Δ (ARM B − ARM A) at conservative cuts — the GO/NO-GO ==========")
    print(f"{'cut':>5} | {'Δ total $':>16} | {'Δ $/trade':>12} | {'Δ prec':>8} | {'Δ Sharpe':>9} | "
          f"{'A total $':>14} | {'B total $':>14}")
    go_signals = []
    for frac in (0.02, 0.05, 0.10):
        a = cut_by_frac(base_curve, frac)
        b = cut_by_frac(trt_curve, frac)
        d_total = b.total_dollar_pnl - a.total_dollar_pnl
        go_signals.append(d_total > 0 and (b.sharpe_net - a.sharpe_net) > 0)
        print(
            f"{frac*100:>4.0f}% | {d_total:>+16,.0f} | {b.dollar_per_trade - a.dollar_per_trade:>+12,.0f} | "
            f"{b.directional_precision - a.directional_precision:>+8.4f} | "
            f"{b.sharpe_net - a.sharpe_net:>+9.2f} | {a.total_dollar_pnl:>+14,.0f} | {b.total_dollar_pnl:>+14,.0f}"
        )
    verdict = "GO (proceed to G1-G6 + build)" if all(go_signals) else (
        "PARTIAL — inspect" if any(go_signals) else "NO-GO ($-null → publish null, do NOT build, §6 pivot)")
    print(f"\nGO/NO-GO (Δ$>0 AND ΔSharpe>0 at ALL of 2/5/10%): {verdict}")
    print(
        f"AUC: A={base_curve.auc:.4f} B={trt_curve.auc:.4f}; rankIC: A={base_curve.rank_ic:+.4f} "
        f"B={trt_curve.rank_ic:+.4f}; headline-10% $: A={base_money.total_pnl:+,.0f} "
        f"(Sh {base_money.sharpe_net:+.2f}) B={trt_money.total_pnl:+,.0f} (Sh {trt_money.sharpe_net:+.2f})"
    )
    print(f"OOS rows: A={base_rows} B={trt_rows}; OOS test-fold days: A={base_ts} B={trt_ts}")


if __name__ == "__main__":
    main()
