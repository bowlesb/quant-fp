"""G0 — cheap $-screen for the quote-tape lane: BOTH framings on THROWAWAY proxies, BEFORE any production
group/kernel (G0-first standard). Reports net-$ for G0a (alpha) AND the cost-MODEL accuracy for G0b.

G0a (ALPHA): quote-DYNAMICS proxies (spread vol/trend, imbalance mean/trend, quote intensity, staleness,
depth vol) added to the FULL trusted baseline + the existing quote_spread group (G2: must beat BOTH). EXACT
Thread-1 harness $-curve A/B at {2,5,10}% net of cost, vs that baseline, with shuffle + predict-zero. GO =
robust incremental net-$, not a single-cut outlier (per-day t check, like the path-geom G0).

G0b (COST MODEL — a DELIVERABLE, per Lead): from the SAME trailing quote proxies, predict the realized
next-window time-weighted half-spread (the cost a name actually quotes over [T,T+h]). Report OOS R² / rank-IC
of predicted-vs-realized cost. A well-calibrated model is a KEEPER even if cost-TIMING-as-alpha nulls — it
replaces the flat DEFAULT_HALF_SPREAD_BPS stub and makes EVERY harness $-test cost-accurate. Also reports the
realized-cost distribution (how wrong the flat 3.0bps stub is).

QUOTE NO-LOOK-AHEAD (G-STALE, enforced here): entry proxies read ONLY quotes ts < T (strict, µs); the cost
label reads ts >= T; staleness = T - last-pre-quote-ts is emitted; valid-NBBO filter (bid<ask, sizes>0).

READ-ONLY store. origin/main (quote_spread + trusted groups present). Research-only: experiments/ only.
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

OUT_DIR = "/app/experiments/2026-06-21-quote-tape"
STORE = "/store"
ENTRY_ET = 9 * 60 + 40
FWD_MIN = 30
W_MIN = 60  # trailing quote window
N_DATES = int(os.environ.get("N_DATES", "42"))
UNIVERSE_TOP = int(os.environ.get("UNIVERSE_TOP", "200"))
N_FOLDS = int(os.environ.get("N_FOLDS", "5"))
CUTS = (0.02, 0.05, 0.10, 0.20, 0.33, 0.50)

# baseline = the trusted dense groups (path-geom G0 list) + the EXISTING quote_spread (21 static feats) — the
# G2-sharpened comparison: the new dynamics proxies must beat the baseline INCLUDING static quote features.
TRUSTED_GROUPS = os.environ.get(
    "GROUPS",
    "volatility,ohlc_vol,momentum,momentum_run,momentum_consistency,runner_state,efficiency,"
    "trend_quality,swing,candlestick,price_returns,return_dynamics,return_dispersion,distribution,"
    "microstructure_burst,trade_flow,signed_trade_ratio,trade_freq_z,liquidity,liquidity_rank,market_beta,"
    "overnight_intraday_split,gap_fill_state,price_volume,clean_momentum,prior_day,quote_spread",
).split(",")

QUOTE_PROXY_COLS = [
    "qd_spread_tw_mean",   # time-weighted mean relative spread (bps)
    "qd_spread_std",       # spread volatility over the window
    "qd_spread_trend",     # OLS slope of spread vs time (widening/tightening)
    "qd_imb_mean",         # mean top-of-book size imbalance
    "qd_imb_trend",        # imbalance slope (depth shifting bid/ask)
    "qd_depth_log",        # mean log total top-of-book size
    "qd_depth_std",        # depth volatility
    "qd_quote_intensity",  # NBBO updates per second
    "qd_staleness_s",      # T - last pre-quote ts (seconds) — also the staleness guard
]
COST_LABEL = "qd_realized_halfspread_bps"  # the G0b target: realized fwd time-weighted half-spread


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
    dv = (
        lazy.filter((minute >= 9 * 60 + 30) & (minute < 16 * 60))
        .group_by("symbol")
        .agg((pl.col("close") * pl.col("volume")).sum().alias("dv"))
        .sort("dv", descending=True)
        .head(n)
        .collect()
    )
    return dv["symbol"].to_list()


def quote_proxies_for_symbol(sym: str, day: str, et: dt.datetime) -> dict[str, float] | None:
    """Throwaway quote-dynamics proxies + the realized-cost label for one symbol. G-STALE enforced:
    pre = quotes ts<T strict; post = ts>=T. NaN-safe; None if no valid pre-window quotes."""
    paths = glob.glob(f"{STORE}/raw/quotes/symbol={sym}/date={day}/*.parquet")
    if not paths:
        return None
    lo = et - dt.timedelta(minutes=W_MIN)
    hi = et + dt.timedelta(minutes=FWD_MIN)
    q = (
        pl.read_parquet(paths[0], columns=["ts", "bid_price", "bid_size", "ask_price", "ask_size"])
        .filter((pl.col("ts") >= lo) & (pl.col("ts") < hi))
        .filter(
            (pl.col("bid_price") > 0)
            & (pl.col("ask_price") > pl.col("bid_price"))
            & (pl.col("bid_size") > 0)
            & (pl.col("ask_size") > 0)
        )
        .sort("ts")
    )
    if q.height < 10:
        return None
    mid = (pl.col("ask_price") + pl.col("bid_price")) / 2.0
    q = q.with_columns(
        ((pl.col("ask_price") - pl.col("bid_price")) / mid * 1e4).alias("spread_bps"),
        ((pl.col("bid_size") - pl.col("ask_size")) / (pl.col("bid_size") + pl.col("ask_size"))).alias("imb"),
        (pl.col("bid_size") + pl.col("ask_size")).alias("tot_size"),
    )
    pre = q.filter(pl.col("ts") < et)
    post = q.filter(pl.col("ts") >= et)
    if pre.height < 5:
        return None

    # dwell time per quote (seconds it stood) for time-weighting; last quote's dwell -> window end.
    def time_weighted(frame: pl.DataFrame, end_np: np.datetime64, col: str) -> float:
        if frame.height == 0:
            return float("nan")
        ts = frame["ts"].to_numpy()
        nxt = np.append(ts[1:], end_np)
        dwell = (nxt - ts) / np.timedelta64(1, "s")
        vals = frame[col].to_numpy()
        wsum = float(np.sum(dwell))
        return float(np.sum(vals * dwell) / wsum) if wsum > 1e-9 else float(np.mean(vals))

    et_np = np.datetime64(et.replace(tzinfo=None), "us")
    hi_np = np.datetime64(hi.replace(tzinfo=None), "us")
    pre_ts = pre["ts"].to_numpy()
    sp = pre["spread_bps"].to_numpy()
    pre_secs = (pre_ts - pre_ts[0]) / np.timedelta64(1, "s")
    spread_trend = float(np.polyfit(pre_secs, sp, 1)[0]) if pre.height >= 3 and pre_secs[-1] > 0 else 0.0
    imb = pre["imb"].to_numpy()
    imb_trend = float(np.polyfit(pre_secs, imb, 1)[0]) if pre.height >= 3 and pre_secs[-1] > 0 else 0.0
    win_secs = max(1.0, float((et_np - pre_ts[0]) / np.timedelta64(1, "s")))
    staleness = float((et_np - pre_ts[-1]) / np.timedelta64(1, "s"))

    realized = time_weighted(post, hi_np, "spread_bps") / 2.0 if post.height >= 2 else float("nan")

    return {
        "symbol": sym,
        "qd_spread_tw_mean": time_weighted(pre, et_np, "spread_bps"),
        "qd_spread_std": float(np.std(sp)),
        "qd_spread_trend": spread_trend,
        "qd_imb_mean": float(np.mean(imb)),
        "qd_imb_trend": imb_trend,
        "qd_depth_log": float(np.mean(np.log1p(pre["tot_size"].to_numpy()))),
        "qd_depth_std": float(np.std(np.log1p(pre["tot_size"].to_numpy()))),
        "qd_quote_intensity": float(pre.height / win_secs),
        "qd_staleness_s": staleness,
        COST_LABEL: realized,
    }


def quote_features(day: str, symbols: list[str], et: dt.datetime) -> pl.DataFrame:
    rows = []
    for sym in symbols:
        rec = quote_proxies_for_symbol(sym, day, et)
        if rec is not None:
            rows.append(rec)
    return pl.DataFrame(rows) if rows else pl.DataFrame()


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
    quotes = quote_features(day, syms, et)
    if quotes.height == 0:
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
        .join(quotes, on="symbol", how="inner")
        .with_columns(
            pl.lit(DEFAULT_HALF_SPREAD_BPS).alias("half_spread_bps"),
            pl.col("entry_close").alias("rth_close"),
        )
    )
    return row, trusted_cols


def make_panel(panel_df: pl.DataFrame, feature_cols: list[str], spread_col: str | None = None):
    """Build the harness Panel. If spread_col given, use it as the per-name half_spread_bps (cost-accurate
    booking — the G0b cost-model wired into the harness); else the flat stub already on the frame."""
    base = [
        "symbol", "minute", "entry_close", "entry_volume", "rth_high", "rth_low",
        "rth_close", "half_spread_bps", "fwd_30m",
    ]
    extra = [c for c in feature_cols if c not in base]
    if spread_col is not None and spread_col not in base and spread_col not in extra:
        extra = extra + [spread_col]
    sub = panel_df.select(base + extra)
    if spread_col is not None:
        sub = sub.with_columns(
            pl.col(spread_col).clip(0.1, 100.0).fill_null(DEFAULT_HALF_SPREAD_BPS).alias("half_spread_bps")
        )
    panel = panel_from_intraday_frame(sub, feature_cols)
    panel.extra["rth_close"] = sub.sort(["symbol", "minute"])["rth_close"].to_numpy().astype(float)
    return panel


def run_arm(panel_df: pl.DataFrame, feature_cols: list[str], label_name: str, spread_col: str | None = None):
    config = HarnessConfig(
        cadence=Cadence.INTRADAY, label_horizon_min=FWD_MIN, model=ModelKind.GBM, n_folds=N_FOLDS,
        long_short_frac=0.10, capital=1_000_000.0, percentile_cuts=CUTS,
        min_train_rows=int(os.environ.get("MIN_TRAIN_ROWS", "600")), min_test_rows=50,
    )
    panel = make_panel(panel_df, feature_cols, spread_col=spread_col)
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


def spearman(x: np.ndarray, y: np.ndarray) -> float:
    def rank(a: np.ndarray) -> np.ndarray:
        return a.argsort().argsort().astype(float)
    rx, ry = rank(x), rank(y)
    return float(np.corrcoef(rx, ry)[0, 1]) if np.std(rx) > 1e-12 and np.std(ry) > 1e-12 else float("nan")


def cost_model_screen(panel_df: pl.DataFrame, proxy_cols: list[str]) -> None:
    """G0b: predict the realized fwd half-spread from the trailing quote proxies (walk-forward GBM), report
    OOS R² + rank-IC of predicted-vs-realized. The DELIVERABLE artifact (replaces the flat stub)."""
    import lightgbm as lgb

    df = panel_df.select(["minute", *proxy_cols, COST_LABEL]).drop_nulls(subset=[COST_LABEL])
    df = df.with_columns([pl.col(c).cast(pl.Float64, strict=False) for c in proxy_cols])
    # drop_nulls does NOT drop float NaN — filter the label to FINITE so R²/MAE aren't poisoned.
    df = df.filter(pl.col(COST_LABEL).is_finite())
    days = sorted(df["minute"].unique().to_list())
    # expanding walk-forward: split days into N_FOLDS+1 segments; fold k trains on segments[:k+1], tests k+1.
    bounds = np.linspace(0, len(days), N_FOLDS + 2).astype(int)
    r2s, ics, maes_model, maes_flat = [], [], [], []
    realized_all = df[COST_LABEL].to_numpy()
    for k in range(N_FOLDS):
        tr_days = set(days[: bounds[k + 1]])
        te_days = set(days[bounds[k + 1] : bounds[k + 2]])
        if not te_days:
            continue
        tr = df.filter(pl.col("minute").is_in(tr_days))
        te = df.filter(pl.col("minute").is_in(te_days))
        if tr.height < 600 or te.height < 100:
            continue
        Xtr, ytr = tr.select(proxy_cols).to_numpy(), tr[COST_LABEL].to_numpy()
        Xte, yte = te.select(proxy_cols).to_numpy(), te[COST_LABEL].to_numpy()
        bst = lgb.train(
            {"objective": "regression", "num_leaves": 31, "learning_rate": 0.05,
             "verbose": -1, "min_data_in_leaf": 50},
            lgb.Dataset(Xtr, ytr), num_boost_round=200,
        )
        pred = bst.predict(Xte)
        ss_res = float(np.sum((yte - pred) ** 2))
        ss_tot = float(np.sum((yte - yte.mean()) ** 2))
        r2s.append(1.0 - ss_res / ss_tot if ss_tot > 1e-12 else float("nan"))
        ics.append(spearman(pred, yte))
        maes_model.append(float(np.mean(np.abs(yte - pred))))
        maes_flat.append(float(np.mean(np.abs(yte - DEFAULT_HALF_SPREAD_BPS))))
    print("\n========== ⭐ G0b COST-MODEL accuracy (the deliverable — predict realized fwd half-spread) ==========")
    print(f"realized fwd half-spread (bps): mean={np.nanmean(realized_all):.2f} median={np.nanmedian(realized_all):.2f} "
          f"p10={np.nanpercentile(realized_all,10):.2f} p90={np.nanpercentile(realized_all,90):.2f} "
          f"(flat stub = {DEFAULT_HALF_SPREAD_BPS:.1f} bps)")
    if r2s:
        print(f"OOS R²={np.mean(r2s):+.3f}  rank-IC={np.nanmean(ics):+.3f}  "
              f"MAE: model={np.mean(maes_model):.2f}bps vs flat-stub={np.mean(maes_flat):.2f}bps "
              f"({100*(1-np.mean(maes_model)/np.mean(maes_flat)):+.0f}% error reduction)  folds={len(r2s)}")
        keeper = np.mean(r2s) > 0.2 and np.mean(maes_model) < np.mean(maes_flat)
        print(f"COST-MODEL verdict: {'KEEPER — wire into harness (replaces flat stub)' if keeper else 'weak — inspect'}")
    else:
        print("  insufficient folds for the cost model")


def main() -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    vdirs = {g: group_vdir(g) for g in TRUSTED_GROUPS if group_vdir(g)}
    days = covered_days(vdirs["volatility"], min_syms=500)[-N_DATES:]
    print(
        f"G0 quote-tape: {len(days)} dates {days[0]}..{days[-1]}, top-{UNIVERSE_TOP} liquid, "
        f"{len(vdirs)} baseline groups (incl quote_spread={'quote_spread' in vdirs}), {N_FOLDS} folds",
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
        if (i + 1) % 5 == 0:
            print(f"  built {i+1}/{len(days)} ({sum(f.height for f in frames)} rows)", flush=True)

    panel_df = pl.concat(frames, how="diagonal_relaxed")
    feat_all = [c for c in (trusted_cols + QUOTE_PROXY_COLS + [COST_LABEL]) if c in panel_df.columns]
    panel_df = panel_df.with_columns([pl.col(c).cast(pl.Float64, strict=False) for c in feat_all])
    trusted_present = [c for c in trusted_cols if c in panel_df.columns]
    proxy_present = [c for c in QUOTE_PROXY_COLS if c in panel_df.columns]
    dens = {c: float(panel_df[c].is_not_null().mean()) for c in proxy_present + [COST_LABEL]}
    print(
        f"panel: {panel_df.height} rows, {panel_df['minute'].n_unique()} entry-ts(days), "
        f"{len(trusted_present)} baseline feats, {len(proxy_present)} quote proxies",
        flush=True,
    )
    print("  proxy/label non-null density:", {k: f"{v:.0%}" for k, v in dens.items()}, flush=True)
    panel_df.write_parquet(f"{OUT_DIR}/g0_quote_panel.parquet")

    # ---- G0a: ALPHA $-screen ----
    print("\n========== ARM A: FULL BASELINE (trusted + quote_spread) ==========")
    base_curve, base_shuf, base_money, base_rows, base_ts = run_arm(panel_df, trusted_present, "BASELINE")
    print("\n========== ARM B: BASELINE + quote-DYNAMICS proxies ==========")
    trt_curve, trt_shuf, trt_money, trt_rows, trt_ts = run_arm(
        panel_df, trusted_present + proxy_present, "BASELINE+quotedyn"
    )
    # ---- also: cost-accurate booking — baseline scored, but cost = predicted realized half-spread ----
    print("\n========== ARM C: BASELINE scored, COST = realized quote half-spread (cost-accuracy effect) ==========")
    costacc_curve, _, costacc_money, _, _ = run_arm(
        panel_df, trusted_present, "BASELINE@realcost", spread_col=COST_LABEL
    )

    def cut_by_frac(curve: ThresholdCurve, frac: float):
        return next(c for c in curve.cuts if abs(c.frac - frac) < 1e-9)

    print("\n========== ⭐ G0a $-CURVE (alpha): ARM A vs ARM B ==========")
    for label, curve, shuf in [("ARM A BASELINE        ", base_curve, base_shuf),
                               ("ARM B +quote-dynamics  ", trt_curve, trt_shuf)]:
        print(f"{label} (AUC={curve.auc:.4f} rankIC={curve.rank_ic:+.4f}):")
        for cut in curve.cuts:
            print(fmt_cut(cut))
        if shuf is not None:
            print("    shuffle:")
            for cut in shuf.cuts:
                print("  " + fmt_cut(cut).strip())
        print()

    print("========== Δ (ARM B − ARM A) at conservative cuts — the G0a GO/NO-GO ==========")
    print(f"{'cut':>5} | {'Δ total $':>16} | {'Δ prec':>8} | {'Δ Sharpe':>9} | {'A total $':>14} | {'B total $':>14}")
    go = []
    for frac in (0.02, 0.05, 0.10):
        a = cut_by_frac(base_curve, frac); b = cut_by_frac(trt_curve, frac)
        d = b.total_dollar_pnl - a.total_dollar_pnl
        go.append(d > 0 and (b.sharpe_net - a.sharpe_net) > 0)
        print(f"{frac*100:>4.0f}% | {d:>+16,.0f} | {b.directional_precision-a.directional_precision:>+8.4f} | "
              f"{b.sharpe_net-a.sharpe_net:>+9.2f} | {a.total_dollar_pnl:>+14,.0f} | {b.total_dollar_pnl:>+14,.0f}")
    verdict = "GO (proceed)" if all(go) else ("PARTIAL — inspect per-day t" if any(go) else "NO-GO ($-null)")
    print(f"\nG0a GO/NO-GO (Δ$>0 AND ΔSharpe>0 at ALL of 2/5/10%): {verdict}")
    print(f"  AUC A={base_curve.auc:.4f} B={trt_curve.auc:.4f}; rankIC A={base_curve.rank_ic:+.4f} B={trt_curve.rank_ic:+.4f}")
    print(f"  OOS rows A={base_rows} B={trt_rows}; OOS days A={base_ts} B={trt_ts}")
    print(f"\n  [context] same baseline signal, FLAT-stub vs REALIZED-cost booking: "
          f"headline10% flat=${base_money.total_pnl:+,.0f} vs realcost=${costacc_money.total_pnl:+,.0f} "
          f"-> shows how much the cost stub distorts the verdict.")

    # ---- G0b: COST MODEL ----
    cost_model_screen(panel_df, proxy_present)


if __name__ == "__main__":
    main()
