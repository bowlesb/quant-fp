"""OPTION A re-grade (Lead-approved): does a MODEL trade the quote-alpha (G0a quote-dynamics) signal under
ACCURATE per-name cost, on G0a's ORIGINAL full liquid-200 universe? Strict apples-to-apples: the ONLY change
vs the G0a null is the cost term (flat 3.0bps stub -> Stage-1 measured per-name realized half-spread).

Full robustness gate (the SAME bar that nulled it): ARM B (baseline + quote-dynamics) must beat ARM A
(baseline = trusted + quote_spread) on net-$ at ALL {2,5,10}% cuts AND AUC+rankIC up AND per-day NW|t|>=2
(not one outlier) AND dominate shuffle + predict-zero — on BOTH a discovery (2026-04-15..05-14) and a
disjoint replication (2026-05-15..06-12) window. BY-FDR with N=1.

DISPOSITION (Ben): a null = the current model doesn't TRADE this yet under accurate cost; the quote-dynamics
features stay INCLUDED/retained. The $-gate is a what-to-trade question, decoupled from feature inclusion.

READ-ONLY store. Reuses the merged quantlib.data.realized_cost. Research-only.
"""
from __future__ import annotations

import datetime as dt
import glob
import math
import os
from collections import defaultdict
from zoneinfo import ZoneInfo

import numpy as np
import polars as pl

from quantlib.battery.panel import DEFAULT_HALF_SPREAD_BPS, panel_from_intraday_frame
from quantlib.data.realized_cost import realized_half_spread_bps
from quantlib.features.raw_loaders import load_raw_minute_agg
from quantlib.harness.config import Cadence, HarnessConfig, ModelKind
from quantlib.harness.diagnostics import ThresholdCurve, threshold_curve
from quantlib.harness.labels import forward_excess_label
from quantlib.harness.run import _baseline_curves, _money_from_basket, _walk_forward_scores

OUT_DIR = "/app/experiments/2026-06-20-cost-regrade"
STORE = "/store"
ENTRY_ET = 9 * 60 + 40
FWD_MIN = 30
W_MIN = 60
UNIVERSE_TOP = 200  # G0a's ORIGINAL universe (pre-committed; no boundary-fishing)
N_FOLDS = 5
CUTS = (0.02, 0.05, 0.10, 0.20, 0.33, 0.50)
DISCOVERY = ("2026-04-15", "2026-05-14")
REPLICATION = ("2026-05-15", "2026-06-12")

TRUSTED_GROUPS = (
    "volatility,ohlc_vol,momentum,momentum_run,momentum_consistency,runner_state,efficiency,"
    "trend_quality,swing,candlestick,price_returns,return_dynamics,return_dispersion,distribution,"
    "microstructure_burst,trade_flow,signed_trade_ratio,trade_freq_z,liquidity,liquidity_rank,market_beta,"
    "overnight_intraday_split,gap_fill_state,price_volume,clean_momentum,prior_day,quote_spread"
).split(",")
QUOTE_PROXY_COLS = [
    "qd_spread_tw_mean", "qd_spread_std", "qd_spread_trend", "qd_imb_mean", "qd_imb_trend",
    "qd_depth_log", "qd_depth_std", "qd_quote_intensity", "qd_staleness_s",
]


def group_vdir(group: str) -> str | None:
    cand = sorted(glob.glob(f"{STORE}/group={group}/v=*"))
    return cand[-1] if cand else None


def entry_ts(day: str) -> dt.datetime:
    d = dt.date.fromisoformat(day)
    return dt.datetime(
        d.year, d.month, d.day, ENTRY_ET // 60, ENTRY_ET % 60, tzinfo=ZoneInfo("America/New_York")
    ).astimezone(dt.timezone.utc)


def covered_days(vdir: str, min_syms: int = 500) -> list[str]:
    days = sorted(os.path.basename(p).replace("date=", "") for p in glob.glob(f"{vdir}/source=backfill/date=*"))
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


def quote_proxies_for_symbol(sym: str, day: str, et: dt.datetime) -> dict[str, float] | None:
    paths = glob.glob(f"{STORE}/raw/quotes/symbol={sym}/date={day}/*.parquet")
    if not paths:
        return None
    lo = et - dt.timedelta(minutes=W_MIN)
    q = (
        pl.read_parquet(paths[0], columns=["ts", "bid_price", "bid_size", "ask_price", "ask_size"])
        .filter((pl.col("ts") >= lo) & (pl.col("ts") < et))
        .filter(
            (pl.col("bid_price") > 0)
            & (pl.col("ask_price") > pl.col("bid_price"))
            & (pl.col("bid_size") > 0)
            & (pl.col("ask_size") > 0)
        )
        .sort("ts")
    )
    if q.height < 5:
        return None
    mid = (pl.col("ask_price") + pl.col("bid_price")) / 2.0
    q = q.with_columns(
        ((pl.col("ask_price") - pl.col("bid_price")) / mid * 1e4).alias("spread_bps"),
        ((pl.col("bid_size") - pl.col("ask_size")) / (pl.col("bid_size") + pl.col("ask_size"))).alias("imb"),
        (pl.col("bid_size") + pl.col("ask_size")).alias("tot_size"),
    )
    ts = q["ts"].to_numpy()
    et_np = np.datetime64(et.replace(tzinfo=None), "us")
    dwell = (np.append(ts[1:], et_np) - ts) / np.timedelta64(1, "s")
    sp = q["spread_bps"].to_numpy()
    secs = (ts - ts[0]) / np.timedelta64(1, "s")
    spread_trend = float(np.polyfit(secs, sp, 1)[0]) if q.height >= 3 and secs[-1] > 0 else 0.0
    imb = q["imb"].to_numpy()
    imb_trend = float(np.polyfit(secs, imb, 1)[0]) if q.height >= 3 and secs[-1] > 0 else 0.0
    win_secs = max(1.0, float((et_np - ts[0]) / np.timedelta64(1, "s")))
    wsum = float(np.sum(dwell))
    return {
        "symbol": sym,
        "qd_spread_tw_mean": float(np.sum(sp * dwell) / wsum) if wsum > 1e-9 else float(np.mean(sp)),
        "qd_spread_std": float(np.std(sp)),
        "qd_spread_trend": spread_trend,
        "qd_imb_mean": float(np.mean(imb)),
        "qd_imb_trend": imb_trend,
        "qd_depth_log": float(np.mean(np.log1p(q["tot_size"].to_numpy()))),
        "qd_depth_std": float(np.std(np.log1p(q["tot_size"].to_numpy()))),
        "qd_quote_intensity": float(q.height / win_secs),
        "qd_staleness_s": float((et_np - ts[-1]) / np.timedelta64(1, "s")),
    }


def quote_features(day: str, symbols: list[str], et: dt.datetime) -> pl.DataFrame:
    rows = [r for sym in symbols if (r := quote_proxies_for_symbol(sym, day, et)) is not None]
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
    realized = realized_half_spread_bps(STORE, day, syms, et)  # ⭐ accurate per-name cost (the ONLY change)

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


def per_day_t(scores: list[float], labels: list[float], groups: list, frac: float) -> tuple[float, int, float]:
    """Per-day L/S excess at `frac` cut -> NW-style t (mean/se over days) + n_days + max-day share."""
    by: dict[object, list[tuple[float, float]]] = defaultdict(list)
    for s, lab, g in zip(scores, labels, groups):
        if not (math.isnan(s) or math.isnan(lab)):
            by[g].append((s, lab))
    daily = []
    for rows in by.values():
        rows.sort()
        k = max(1, int(frac * len(rows)))
        if len(rows) < 2 * k:
            continue
        longs = rows[-k:]
        shorts = rows[:k]
        daily.append(sum(lab for _, lab in longs) / k - sum(lab for _, lab in shorts) / k)
    arr = np.array(daily)
    if arr.shape[0] < 3 or arr.std(ddof=1) < 1e-12:
        return float("nan"), int(arr.shape[0]), float("nan")
    tstat = float(arr.mean() / (arr.std(ddof=1) / np.sqrt(arr.shape[0])))
    max_share = float(np.max(np.abs(arr)) / np.sum(np.abs(arr))) if np.sum(np.abs(arr)) > 0 else float("nan")
    return tstat, int(arr.shape[0]), max_share


def book(panel_df: pl.DataFrame, feature_cols: list[str], half_spread_expr: pl.Expr, tag: str):
    config = HarnessConfig(
        cadence=Cadence.INTRADAY, label_horizon_min=FWD_MIN, model=ModelKind.GBM, n_folds=N_FOLDS,
        long_short_frac=0.10, capital=1_000_000.0, percentile_cuts=CUTS, min_train_rows=600, min_test_rows=50,
    )
    panel = make_panel(panel_df, feature_cols, half_spread_expr)
    label = forward_excess_label(panel, horizon_days=1, horizon_min=FWD_MIN)
    scores, labels, groups, symbols, spreads = _walk_forward_scores(config, panel, label)
    curve = threshold_curve(
        scores, labels, groups, symbols, spreads, cuts=CUTS, capital=config.capital,
        cost_mult=config.cost_mult, slippage_bps=config.slippage_bps,
        borrow_bps_annual=config.borrow_bps_annual, periods_per_year=config.periods_per_year,
    )
    money = _money_from_basket(scores, labels, groups, symbols, spreads, config)
    shuf, _ = _baseline_curves(scores, labels, groups, symbols, spreads, config)
    med = float(np.nanmedian([s for s in spreads if s == s]))
    print(f"  [{tag}] n_feats={len(feature_cols)} oos_rows={len(scores)} med_cost={med:.2f}bps "
          f"AUC={curve.auc:.4f} rankIC={curve.rank_ic:+.4f} headline10%=${money.total_pnl:+,.0f}", flush=True)
    return curve, shuf, (scores, labels, groups)


def fmt_cut(c) -> str:
    return (f"  {c.frac*100:>4.0f}% | prec={c.directional_precision:.4f} "
            f"$/trade={c.dollar_per_trade:>+9,.0f} total=${c.total_dollar_pnl:>+13,.0f} "
            f"Sharpe={c.sharpe_net:>+6.2f} n={c.n_trades}")


def cut_by(curve: ThresholdCurve, frac: float):
    return next(c for c in curve.cuts if abs(c.frac - frac) < 1e-9)


def run_window(panel_df: pl.DataFrame, trusted: list[str], proxies: list[str], realized_expr: pl.Expr,
               window_name: str) -> dict[str, bool]:
    print(f"\n================= WINDOW: {window_name} ({panel_df['minute'].n_unique()} days, "
          f"{panel_df.height} rows) — booked under ACCURATE per-name cost =================")
    a_curve, a_shuf, _ = book(panel_df, trusted, realized_expr, "ARM A baseline")
    b_curve, b_shuf, b_raw = book(panel_df, trusted + proxies, realized_expr, "ARM B +quote-dyn")
    print("  ARM B threshold curve:")
    for c in b_curve.cuts:
        print(fmt_cut(c))
    # the FULL gate
    gate = {}
    dollar_up = all(cut_by(b_curve, f).total_dollar_pnl > cut_by(a_curve, f).total_dollar_pnl for f in (0.02, 0.05, 0.10))
    rank_up = (b_curve.auc > a_curve.auc) and (b_curve.rank_ic > a_curve.rank_ic)
    beats_shuf = all(cut_by(b_curve, f).total_dollar_pnl > cut_by(b_shuf, f).total_dollar_pnl for f in (0.02, 0.05, 0.10))
    t5, nd, mxs = per_day_t(*b_raw, 0.05)
    t_ok = (not math.isnan(t5)) and abs(t5) >= 2.0
    gate = {"dollar_up_all_cuts": dollar_up, "auc_and_rankic_up": rank_up,
            "per_day_t>=2": t_ok, "beats_shuffle": beats_shuf}
    print(f"\n  GATE [{window_name}]: Δ$ up at all 2/5/10%={dollar_up} | AUC+rankIC up={rank_up} | "
          f"per-day t(5%)={t5:+.2f} (n_days={nd}, max-day-share={mxs:.0%}) >=2={t_ok} | beats-shuffle={beats_shuf}")
    print(f"  Δ at cuts (B−A): " + "  ".join(
        f"{int(f*100)}%={cut_by(b_curve,f).total_dollar_pnl-cut_by(a_curve,f).total_dollar_pnl:+,.0f}" for f in (0.02, 0.05, 0.10)))
    print(f"  => {window_name} PASS-ALL-LEGS: {all(gate.values())}")
    return gate


def main() -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    vdirs = {g: group_vdir(g) for g in TRUSTED_GROUPS if group_vdir(g)}
    days = covered_days(vdirs["volatility"], 500)
    days = [d for d in days if DISCOVERY[0] <= d <= REPLICATION[1]]
    print(f"Option A re-grade: full liquid-{UNIVERSE_TOP}, {len(days)} dates {days[0]}..{days[-1]}, "
          f"ACCURATE per-name cost. Disc {DISCOVERY} / Repl {REPLICATION}.", flush=True)
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
    feats = [c for c in (trusted_cols + QUOTE_PROXY_COLS) if c in panel_df.columns]
    panel_df = panel_df.with_columns([pl.col(c).cast(pl.Float64, strict=False) for c in feats])
    trusted_present = [c for c in trusted_cols if c in panel_df.columns]
    proxy_present = [c for c in QUOTE_PROXY_COLS if c in panel_df.columns]
    n_real = int(panel_df["realized_half_spread_bps"].is_not_null().sum())
    print(f"panel: {panel_df.height} rows, {panel_df['minute'].n_unique()} days, {len(trusted_present)} trusted "
          f"+ {len(proxy_present)} quote-dyn feats; realized cost for {n_real}/{panel_df.height} rows "
          f"(median {float(panel_df['realized_half_spread_bps'].median()):.2f}bps)", flush=True)
    panel_df.write_parquet(f"{OUT_DIR}/optionA_panel.parquet")

    realized_expr = pl.col("realized_half_spread_bps").clip(0.1, 100.0).fill_null(DEFAULT_HALF_SPREAD_BPS)
    disc_df = panel_df.filter((pl.col("minute").dt.date().cast(pl.Utf8) >= DISCOVERY[0]) &
                              (pl.col("minute").dt.date().cast(pl.Utf8) <= DISCOVERY[1]))
    repl_df = panel_df.filter((pl.col("minute").dt.date().cast(pl.Utf8) >= REPLICATION[0]) &
                              (pl.col("minute").dt.date().cast(pl.Utf8) <= REPLICATION[1]))
    disc_gate = run_window(disc_df, trusted_present, proxy_present, realized_expr, "DISCOVERY")
    repl_gate = run_window(repl_df, trusted_present, proxy_present, realized_expr, "REPLICATION")

    disc_pass = all(disc_gate.values())
    repl_pass = all(repl_gate.values())
    print("\n================= ⭐ OPTION A VERDICT =================")
    print(f"DISCOVERY pass-all-legs: {disc_pass}  |  REPLICATION pass-all-legs: {repl_pass}")
    overall = disc_pass and repl_pass
    if overall:
        print("RESULT: PASS — quote-alpha is a TRADEABLE model signal under accurate per-name cost "
              "(replicated disjoint). First real edge candidate; report for confirmatory follow-up.")
    else:
        print("RESULT: NULL — under accurate per-name cost on the full liquid-200, the CURRENT MODEL does NOT "
              "trade the quote-dynamics signal profitably YET (expected: 78% of names get DEARER, not cheaper). "
              "DISPOSITION (Ben): NOT 'exhausted/dead' — the quote-dynamics features stay INCLUDED/retained in "
              "the platform; future data + feature-interactions may make a model use them. The $-test answers "
              "what to TRADE, not what to STORE. ROUTE TO #2 (#205 weekly reversal) as pre-committed.")


if __name__ == "__main__":
    main()
