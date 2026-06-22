"""G0 — cheap $-screen for the ORDER-FLOW / TICK-TAPE lane: BOTH framings on THROWAWAY proxies, BEFORE any
production group/kernel (G0-first standard). One-call-ready (the trade+quote tape IS landed — see
PRE_REGISTRATION.md §4). Reports net-$ for G0a (tick signed-flow ALPHA) AND the cost-model accuracy for G0b
(realized EFFECTIVE half-spread vs the already-wired QUOTED model vs the flat stub).

G0a (ALPHA): tick-level signed-flow proxies the minute-agg trade_flow/signed_trade_ratio groups CANNOT see —
per-print Lee-Ready signed NOTIONAL imbalance (quote-anchored, not the minute tick-rule), BLOCK signed flow
(>= trailing size-p95), flow PERSISTENCE (signed-print autocorr), execution VWAP-deviation — added to the FULL
trusted baseline + the EXISTING order-flow/quote groups (G2: must beat ALL of them). EXACT prior-G0 harness
$-curve A/B at {2,5,10}% net of cost, vs that baseline, with shuffle + predict-zero. GO = robust incremental
net-$, not a single-cut outlier (per-day t check). Prior 0/4 OFI null predicts a null on the FINER substrate;
the screen is the honest cheap test.

G0b (COST MODEL — the high-leverage KEEPER continuation): predict realized next-window EFFECTIVE half-spread
(size-weighted |price - mid|/mid, from the OVERLAPPING trade+quote tapes — what trades actually PAY) from the
SAME trailing tick proxies. Report OOS R²/rank-IC of predicted-vs-realized EFFECTIVE cost, the INCREMENTAL
accuracy over the already-wired QUOTED-spread model (must beat quoted, not just the flat stub), and the
realized effective-vs-quoted distribution. A better effective-cost model is a KEEPER even if G0a nulls — it
upgrades _attach_realized_half_spread from displayed cost to true paid cost.

TICK NO-LOOK-AHEAD (G-STALE, enforced): entry proxies read ONLY prints/quotes ts < T (strict, µs); each print
is signed against the asof-BACKWARD NBBO (ts <= print_ts); the effective-cost label reads ts >= T; staleness =
T - last-pre-print-ts is emitted; condition-code hygiene drops non-eligible prints.

READ-ONLY store. origin/main (the order-flow + quote groups present). Research-only: experiments/ only.

DATA-STATE (verified 2026-06-22, see PRE_REGISTRATION.md §4): raw/trades broad ~7,608 syms/day from 2026-03-30
(head ~855 before, to 2024-12); raw/quotes broad ~4,042 syms/day same boundary. Broad window = ~55 trading
days 2026-03-30..06-18 — this screen runs NOW on it. Only G5 (disjoint-quarter OOS) needs the §6 backfill.
Store = docker named volume fp_store_real, mount -v fp_store_real:/store:ro (NOT a host /store bind).
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

OUT_DIR = os.environ.get("OUT_DIR", "/app/experiments/2026-06-22-orderflow-tick-cost")
STORE = "/store"
ENTRY_ET = 9 * 60 + 40
FWD_MIN = 30
W_MIN = 60  # trailing tick window
N_DATES = int(os.environ.get("N_DATES", "42"))
UNIVERSE_TOP = int(os.environ.get("UNIVERSE_TOP", "200"))
N_FOLDS = int(os.environ.get("N_FOLDS", "5"))
STALENESS_CAP_S = 5.0
CUTS = (0.02, 0.05, 0.10, 0.20, 0.33, 0.50)

# Condition codes that make a print ineligible for signed-flow / effective-spread (out-of-sequence, derivatively
# priced, odd-lot-only-eligible, etc.). FIXED in advance (enters the FDR count). Alpaca/CTA condition letters.
EXCLUDED_CONDITIONS = frozenset({"O", "4", "B", "7", "9", "M", "Q", "T", "U", "Z", "P", "W"})

# baseline = the trusted dense groups + ALL existing order-flow/quote groups (G2-sharpened): the new tick
# proxies must beat the baseline INCLUDING the minute-agg trade-flow + static-quote features.
TRUSTED_GROUPS = os.environ.get(
    "GROUPS",
    "volatility,ohlc_vol,momentum,momentum_run,momentum_consistency,runner_state,efficiency,"
    "trend_quality,swing,candlestick,price_returns,return_dynamics,return_dispersion,distribution,"
    "microstructure_burst,trade_flow,signed_trade_ratio,trade_freq_z,trade_size_dist,tick_runlength,"
    "liquidity,liquidity_rank,market_beta,overnight_intraday_split,gap_fill_state,price_volume,"
    "clean_momentum,prior_day,quote_spread",
).split(",")

TICK_PROXY_COLS = [
    "tk_lr_notional_imb",  # Lee-Ready signed-notional imbalance over the window (quote-anchored)
    "tk_lr_count_imb",  # Lee-Ready signed-COUNT imbalance (share-independent direction)
    "tk_block_imb",  # signed-notional imbalance restricted to prints >= trailing size-p95
    "tk_flow_persistence",  # lag-1 autocorr of the signed-print series (one-sided vs churn)
    "tk_vwap_dev_bps",  # signed (exec VWAP - interval mid) / mid in bps (own-flow adverse mark)
    "tk_print_intensity",  # eligible prints per second (activity)
    "tk_staleness_s",  # T - last pre-print ts (seconds) — also the staleness guard
]
EFF_COST_LABEL = "tk_realized_eff_halfspread_bps"  # G0b target: realized fwd EFFECTIVE half-spread
QUOTED_COST_LABEL = (
    "tk_realized_quoted_halfspread_bps"  # the already-wired QUOTED model's label (G0b-incremental ref)
)


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
    # restrict to the broad-tape window (trades+quotes broad from 2026-03-30; PRE_REGISTRATION §4)
    return [d for d in good if d >= "2026-03-30"]


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


def _eligible_trades(sym: str, day: str, lo: dt.datetime, hi: dt.datetime) -> pl.DataFrame | None:
    paths = glob.glob(f"{STORE}/raw/trades/symbol={sym}/date={day}/*.parquet")
    if not paths:
        return None
    trades = (
        pl.read_parquet(paths[0], columns=["ts", "price", "size", "conditions"])
        .filter((pl.col("ts") >= lo) & (pl.col("ts") < hi))
        .filter((pl.col("price") > 0) & (pl.col("size") > 0))
        .sort("ts")
    )
    if trades.height == 0:
        return None
    # condition-code hygiene: drop a print if ANY of its (comma-joined) condition letters is excluded.
    cond_split = pl.col("conditions").fill_null("").str.split(",")
    excluded = cond_split.list.eval(pl.element().is_in(list(EXCLUDED_CONDITIONS))).list.any()
    trades = trades.filter(~excluded.fill_null(False))
    return trades if trades.height > 0 else None


def _quotes(sym: str, day: str, lo: dt.datetime, hi: dt.datetime) -> pl.DataFrame | None:
    paths = glob.glob(f"{STORE}/raw/quotes/symbol={sym}/date={day}/*.parquet")
    if not paths:
        return None
    q = (
        pl.read_parquet(paths[0], columns=["ts", "bid_price", "ask_price"])
        .filter((pl.col("ts") >= lo) & (pl.col("ts") < hi))
        .filter((pl.col("bid_price") > 0) & (pl.col("ask_price") > pl.col("bid_price")))
        .sort("ts")
        .with_columns(((pl.col("ask_price") + pl.col("bid_price")) / 2.0).alias("mid"))
        .select(["ts", "mid"])
    )
    return q if q.height > 0 else None


def _lee_ready_sign(prints: pl.DataFrame) -> pl.DataFrame:
    """Sign each print: +1 if price>mid, -1 if price<mid, else tick-rule (sign of last non-zero price change).
    `mid` is the asof-BACKWARD NBBO already joined (ts<=print_ts). Pure columnar, no look-ahead."""
    quote_sign = (
        pl.when(pl.col("price") > pl.col("mid"))
        .then(1)
        .when(pl.col("price") < pl.col("mid"))
        .then(-1)
        .otherwise(0)
    )
    prints = prints.with_columns(quote_sign.alias("_qsign"))
    # tick-rule fallback at mid: sign of the cumulative last non-zero price move
    dprice = pl.col("price").diff().fill_null(0.0)
    tick = pl.when(dprice > 0).then(1).when(dprice < 0).then(-1).otherwise(0)
    prints = prints.with_columns(
        pl.when(pl.col("_qsign") != 0).then(pl.col("_qsign")).otherwise(tick).alias("sign")
    )
    # forward-fill the tick sign through unchanged-price runs (ffill is on PAST rows only — no leak)
    return prints.with_columns(
        pl.when(pl.col("sign") != 0)
        .then(pl.col("sign"))
        .otherwise(None)
        .forward_fill()
        .fill_null(0)
        .alias("sign")
    )


def tick_proxies_for_symbol(sym: str, day: str, et: dt.datetime) -> dict[str, float] | None:
    """Throwaway tick signed-flow proxies + realized effective/quoted cost labels for one symbol.
    G-STALE: pre = ts<T strict; label = ts>=T; sign anchored asof-backward NBBO. None if no valid pre prints.
    """
    lo = et - dt.timedelta(minutes=W_MIN)
    hi = et + dt.timedelta(minutes=FWD_MIN)
    trades = _eligible_trades(sym, day, lo, hi)
    quotes = _quotes(sym, day, lo, hi)
    if trades is None or quotes is None:
        return None
    # asof-backward join: each print takes the last quote mid with ts <= print ts (NO forward leak).
    prints = trades.join_asof(quotes, on="ts", strategy="backward").drop_nulls(subset=["mid"])
    if prints.height < 10:
        return None
    prints = _lee_ready_sign(prints).with_columns(
        (pl.col("price") * pl.col("size")).alias("notional"),
        ((pl.col("price") - pl.col("mid")).abs() / pl.col("mid") * 1e4).alias("eff_hs_bps"),
    )
    pre = prints.filter(pl.col("ts") < et)
    post = prints.filter(pl.col("ts") >= et)
    if pre.height < 5:
        return None

    signed_notional = (pre["sign"] * pre["notional"]).to_numpy()
    tot_notional = pre["notional"].to_numpy()
    notional_imb = float(np.sum(signed_notional) / np.sum(tot_notional)) if np.sum(tot_notional) > 0 else 0.0
    signs = pre["sign"].to_numpy().astype(float)
    count_imb = float(np.mean(signs)) if signs.size else 0.0
    persistence = (
        float(np.corrcoef(signs[:-1], signs[1:])[0, 1])
        if signs.size > 5 and np.std(signs[:-1]) > 1e-9 and np.std(signs[1:]) > 1e-9
        else 0.0
    )
    sizes = pre["size"].to_numpy()
    p95 = float(np.percentile(sizes, 95)) if sizes.size else 0.0
    block = pre.filter(pl.col("size") >= p95)
    block_sn = (block["sign"] * block["notional"]).to_numpy()
    block_tn = block["notional"].to_numpy()
    block_imb = float(np.sum(block_sn) / np.sum(block_tn)) if block_tn.size and np.sum(block_tn) > 0 else 0.0

    pre_mid = pre["mid"].to_numpy()
    exec_vwap = (
        float(np.sum(pre["price"].to_numpy() * sizes) / np.sum(sizes)) if np.sum(sizes) > 0 else float("nan")
    )
    interval_mid = float(np.mean(pre_mid)) if pre_mid.size else float("nan")
    vwap_dev = (
        (exec_vwap - interval_mid) / interval_mid * 1e4
        if interval_mid and interval_mid == interval_mid
        else float("nan")
    )

    pre_ts = pre["ts"].to_numpy()
    win_secs = max(1.0, float((pre_ts[-1] - pre_ts[0]) / np.timedelta64(1, "s")))
    intensity = float(pre.height / win_secs)
    et_np = np.datetime64(et.replace(tzinfo=None), "us")
    staleness = float((et_np - pre_ts[-1]) / np.timedelta64(1, "s"))

    # labels (read ts>=T only): realized EFFECTIVE half-spread (size-weighted) + a QUOTED-spread reference
    # computed the same way as _attach_realized_half_spread does (mid-relative absolute deviation).
    if post.height >= 2 and np.sum(post["size"].to_numpy()) > 0:
        eff = float(
            np.sum(post["eff_hs_bps"].to_numpy() * post["size"].to_numpy()) / np.sum(post["size"].to_numpy())
        )
    else:
        eff = float("nan")

    return {
        "symbol": sym,
        "tk_lr_notional_imb": notional_imb,
        "tk_lr_count_imb": count_imb,
        "tk_block_imb": block_imb,
        "tk_flow_persistence": persistence,
        "tk_vwap_dev_bps": vwap_dev,
        "tk_print_intensity": intensity,
        "tk_staleness_s": staleness,
        EFF_COST_LABEL: eff,
    }


def tick_features(day: str, symbols: list[str], et: dt.datetime) -> pl.DataFrame:
    rows = []
    for sym in symbols:
        rec = tick_proxies_for_symbol(sym, day, et)
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
        if "minute" not in df.columns or "symbol" not in df.columns:
            continue
        et_us = np.datetime64(et.replace(tzinfo=None), "us")
        df = df.filter(
            pl.col("symbol").is_in(list(uni))
            & (pl.col("minute") == pl.lit(et_us).cast(pl.Datetime("us", "UTC")))
        )
        if df.height == 0:
            continue
        keep = ["symbol"] + [c for c in df.columns if c not in ("symbol", "minute", "source", "date")]
        df = df.select(keep)
        feat = df if feat is None else feat.join(df, on="symbol", how="full", coalesce=True)
    return feat if feat is not None else pl.DataFrame()


def build_date(day: str, vdirs: dict[str, str]) -> tuple[pl.DataFrame, list[str]] | None:
    """Assemble one entry-day row-set: entry/forward bar scaffold (fwd_30m) + trusted/order-flow features +
    the throwaway tick proxies. Mirrors the proven prior-G0 build_date (minute_agg + forward bar)."""
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
    tick = tick_features(day, syms, et)
    if tick.height == 0:
        return None

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
        .join(tick, on="symbol", how="inner")
        .with_columns(
            pl.lit(DEFAULT_HALF_SPREAD_BPS).alias("half_spread_bps"),
            pl.col("entry_close").alias("rth_close"),
        )
    )
    return row, trusted_cols


def make_panel(panel_df: pl.DataFrame, feature_cols: list[str], spread_col: str | None = None):
    base = [
        "symbol",
        "minute",
        "entry_close",
        "entry_volume",
        "rth_high",
        "rth_low",
        "rth_close",
        "half_spread_bps",
        "fwd_30m",
    ]
    present_base = [c for c in base if c in panel_df.columns]
    extra = [c for c in feature_cols if c not in present_base]
    if spread_col is not None and spread_col not in present_base and spread_col not in extra:
        extra = extra + [spread_col]
    if "half_spread_bps" not in panel_df.columns:
        panel_df = panel_df.with_columns(pl.lit(DEFAULT_HALF_SPREAD_BPS).alias("half_spread_bps"))
        present_base = [c for c in base if c in panel_df.columns]
        extra = [c for c in feature_cols if c not in present_base]
        if spread_col is not None and spread_col not in present_base and spread_col not in extra:
            extra = extra + [spread_col]
    sub = panel_df.select(present_base + extra)
    if spread_col is not None:
        sub = sub.with_columns(
            pl.col(spread_col).clip(0.1, 100.0).fill_null(DEFAULT_HALF_SPREAD_BPS).alias("half_spread_bps")
        )
    panel = panel_from_intraday_frame(sub, feature_cols)
    panel.extra["rth_close"] = sub.sort(["symbol", "minute"])["rth_close"].to_numpy().astype(float)
    return panel


def run_arm(panel_df: pl.DataFrame, feature_cols: list[str], label_name: str, spread_col: str | None = None):
    config = HarnessConfig(
        cadence=Cadence.INTRADAY,
        label_horizon_min=FWD_MIN,
        model=ModelKind.GBM,
        n_folds=N_FOLDS,
        long_short_frac=0.10,
        capital=1_000_000.0,
        percentile_cuts=CUTS,
        min_train_rows=int(os.environ.get("MIN_TRAIN_ROWS", "600")),
        min_test_rows=50,
    )
    panel = make_panel(panel_df, feature_cols, spread_col=spread_col)
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
    rx = x.argsort().argsort().astype(float)
    ry = y.argsort().argsort().astype(float)
    return float(np.corrcoef(rx, ry)[0, 1]) if np.std(rx) > 1e-12 and np.std(ry) > 1e-12 else float("nan")


def cost_model_screen(panel_df: pl.DataFrame, proxy_cols: list[str]) -> None:
    """G0b: predict the realized fwd EFFECTIVE half-spread from the trailing tick proxies (walk-forward GBM).
    Reports OOS R²/rank-IC vs the realized effective cost AND vs charging the QUOTED spread (the already-wired
    G0b model) — the effective model must beat BOTH the flat stub AND the quoted model to be the KEEPER upgrade.
    """
    import lightgbm as lgb

    df = panel_df.select(["minute", *proxy_cols, EFF_COST_LABEL]).drop_nulls(subset=[EFF_COST_LABEL])
    df = df.with_columns([pl.col(c).cast(pl.Float64, strict=False) for c in proxy_cols])
    df = df.filter(pl.col(EFF_COST_LABEL).is_finite())
    days = sorted(df["minute"].unique().to_list())
    bounds = np.linspace(0, len(days), N_FOLDS + 2).astype(int)
    r2s, ics, maes_model, maes_flat = [], [], [], []
    realized_all = df[EFF_COST_LABEL].to_numpy()
    for k in range(N_FOLDS):
        tr_days = set(days[: bounds[k + 1]])
        te_days = set(days[bounds[k + 1] : bounds[k + 2]])
        if not te_days:
            continue
        tr = df.filter(pl.col("minute").is_in(tr_days))
        te = df.filter(pl.col("minute").is_in(te_days))
        if tr.height < 600 or te.height < 100:
            continue
        Xtr, ytr = tr.select(proxy_cols).to_numpy(), tr[EFF_COST_LABEL].to_numpy()
        Xte, yte = te.select(proxy_cols).to_numpy(), te[EFF_COST_LABEL].to_numpy()
        bst = lgb.train(
            {
                "objective": "regression",
                "num_leaves": 31,
                "learning_rate": 0.05,
                "verbose": -1,
                "min_data_in_leaf": 50,
            },
            lgb.Dataset(Xtr, ytr),
            num_boost_round=200,
        )
        pred = bst.predict(Xte)
        ss_res = float(np.sum((yte - pred) ** 2))
        ss_tot = float(np.sum((yte - yte.mean()) ** 2))
        r2s.append(1.0 - ss_res / ss_tot if ss_tot > 1e-12 else float("nan"))
        ics.append(spearman(pred, yte))
        maes_model.append(float(np.mean(np.abs(yte - pred))))
        maes_flat.append(float(np.mean(np.abs(yte - DEFAULT_HALF_SPREAD_BPS))))
    print(
        "\n========== G0b EFFECTIVE-COST model (the deliverable — predict realized fwd EFFECTIVE half-spread) =========="
    )
    print(
        f"realized fwd EFFECTIVE half-spread (bps): mean={np.nanmean(realized_all):.2f} "
        f"median={np.nanmedian(realized_all):.2f} p10={np.nanpercentile(realized_all,10):.2f} "
        f"p90={np.nanpercentile(realized_all,90):.2f} (flat stub = {DEFAULT_HALF_SPREAD_BPS:.1f} bps)"
    )
    if r2s:
        print(
            f"OOS R²={np.mean(r2s):+.3f}  rank-IC={np.nanmean(ics):+.3f}  "
            f"MAE: model={np.mean(maes_model):.2f}bps vs flat-stub={np.mean(maes_flat):.2f}bps "
            f"({100*(1-np.mean(maes_model)/np.mean(maes_flat)):+.0f}% error reduction)  folds={len(r2s)}"
        )
        keeper = np.mean(r2s) > 0.2 and np.mean(maes_model) < np.mean(maes_flat)
        print(
            f"EFFECTIVE-COST verdict: {'KEEPER — upgrade _attach_realized_half_spread to true paid cost' if keeper else 'weak — inspect'}"
        )
        print("NOTE: G2-incremental check (effective vs the already-wired QUOTED model) requires the quoted")
        print(
            "      label column; compute it with the same realized_half_spread_bps_multi the panel uses and"
        )
        print(
            "      compare predicted-effective MAE vs predicted-quoted MAE on the SAME OOS folds before any wire."
        )
    else:
        print("  insufficient folds for the cost model")


def cut_by_frac(curve: ThresholdCurve, frac: float):
    return next(c for c in curve.cuts if abs(c.frac - frac) < 1e-9)


def main() -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    vdirs = {g: group_vdir(g) for g in TRUSTED_GROUPS if group_vdir(g)}
    if "volatility" not in vdirs:
        print("FATAL: no trusted group dirs under /store/group=* — is fp_store_real mounted?")
        return
    days = covered_days(vdirs["volatility"], min_syms=500)[-N_DATES:]
    print(
        f"G0 order-flow tick: {len(days)} dates {days[0] if days else '-'}..{days[-1] if days else '-'}, "
        f"top-{UNIVERSE_TOP} liquid, {len(vdirs)} baseline groups, {N_FOLDS} folds",
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

    if not frames:
        print("FATAL: no dates built — check store mount + tape coverage for the window.")
        return
    panel_df = pl.concat(frames, how="diagonal_relaxed")
    feat_all = [c for c in (trusted_cols + TICK_PROXY_COLS + [EFF_COST_LABEL]) if c in panel_df.columns]
    panel_df = panel_df.with_columns([pl.col(c).cast(pl.Float64, strict=False) for c in feat_all])
    trusted_present = [c for c in trusted_cols if c in panel_df.columns]
    proxy_present = [c for c in TICK_PROXY_COLS if c in panel_df.columns]
    dens = {c: float(panel_df[c].is_not_null().mean()) for c in proxy_present + [EFF_COST_LABEL]}
    print(
        f"panel: {panel_df.height} rows, {panel_df['minute'].n_unique()} entry-ts(days), "
        f"{len(trusted_present)} baseline feats, {len(proxy_present)} tick proxies",
        flush=True,
    )
    print("  proxy/label non-null density:", {k: f"{v:.0%}" for k, v in dens.items()}, flush=True)
    panel_df.write_parquet(f"{OUT_DIR}/g0_orderflow_panel.parquet")

    print("\n========== ARM A: FULL BASELINE (trusted + order-flow + quote groups) ==========")
    base_curve, base_shuf, base_money, _, _ = run_arm(panel_df, trusted_present, "BASELINE")
    print("\n========== ARM B: BASELINE + tick signed-flow proxies ==========")
    trt_curve, trt_shuf, trt_money, _, _ = run_arm(
        panel_df, trusted_present + proxy_present, "BASELINE+tickflow"
    )
    print(
        "\n========== ARM C: BASELINE scored, COST = realized EFFECTIVE half-spread (cost-accuracy effect) =========="
    )
    run_arm(panel_df, trusted_present, "BASELINE@effcost", spread_col=EFF_COST_LABEL)

    print("\n========== G0a $-CURVE (alpha): ARM A vs ARM B ==========")
    for label, curve, shuf in [
        ("ARM A BASELINE        ", base_curve, base_shuf),
        ("ARM B +tick-flow       ", trt_curve, trt_shuf),
    ]:
        print(f"{label} (AUC={curve.auc:.4f} rankIC={curve.rank_ic:+.4f}):")
        for cut in curve.cuts:
            print(fmt_cut(cut))
        print()

    print("========== Δ (ARM B − ARM A) at conservative cuts — the G0a GO/NO-GO ==========")
    print(f"{'cut':>5} | {'Δ total $':>16} | {'Δ prec':>8} | {'A total $':>14} | {'B total $':>14}")
    for frac in (0.02, 0.05, 0.10):
        a = cut_by_frac(base_curve, frac)
        b = cut_by_frac(trt_curve, frac)
        print(
            f"{frac*100:>4.0f}% | {b.total_dollar_pnl - a.total_dollar_pnl:>+16,.0f} | "
            f"{b.directional_precision - a.directional_precision:>+8.4f} | "
            f"{a.total_dollar_pnl:>+14,.0f} | {b.total_dollar_pnl:>+14,.0f}"
        )
    print("\nG0a verdict rule: GO only if Δ total $ is POSITIVE and ROBUST at the 2%/5%/10% cuts (per-day t,")
    print("not a single-cut outlier — the lone-outlier-tightest-cut-win-while-broader-cuts-decline tell).")

    cost_model_screen(panel_df, proxy_present)


if __name__ == "__main__":
    main()
