"""One-off diagnostic: is the lambdarank+overnight canary an ARTIFACT or NOISE?

Loads the deep overnight panel ONCE, then runs:
 A. multi-seed shuffle canary (lambdarank): stable -> structural artifact; scattered -> noise
 B. daily-block / Newey-West significance of REAL IC and CANARY IC series
 C. lambdarank feature importances (look for a leaky/selection feature)
 D. PERSISTENT-SELECTION probe: does the within-day-shuffle canary survive because the
    lambdarank prediction has a day-invariant per-symbol component that correlates with the
    per-symbol mean overnight return? (= hypothesis (b), partly-REAL structure)
 E. drop suspicious features and re-measure canary.

Read-only on the DB; trains LightGBM in-process. Heavy (~570k rows) so panel loads once.
"""
import math
import os
import statistics
import sys
from collections import defaultdict

import numpy as np
import psycopg

from quantlib.backtest import (
    mean_ic,
    per_timestamp_ic,
    shuffle_within_groups,
    walk_forward_folds,
)
from quantlib.research import (
    DEFAULT_LGB,
    _group_counts,
    _int_relevance,
)

import lightgbm as lgb

HORIZON = "overnight"
SET_VERSION = "v1.1.0"
HORIZON_MIN = 1440
N_FOLDS = 5
NUM_ROUNDS = 200

DB_KWARGS = dict(
    host=os.environ["DB_HOST"], port=int(os.environ.get("DB_PORT", "5432")),
    dbname=os.environ["DB_NAME"], user=os.environ["DB_USER"], password=os.environ["DB_PASSWORD"],
)

MICRO = {"trade_imbalance", "large_print_cnt", "trade_intensity", "spread_bps", "quote_imbalance"}
CALENDAR = {"minute_of_day", "day_of_week"}


def load_panel(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT names FROM feature_sets WHERE version=%s", (SET_VERSION,))
        names = cur.fetchone()[0]
        cur.execute(
            """SELECT fv.ts, fv.symbol, fv.vector, l.value
               FROM feature_vectors fv
               JOIN labels l ON l.symbol=fv.symbol AND l.ts=fv.ts AND l.horizon=%s
               WHERE fv.source='historical' AND fv.set_version=%s
               ORDER BY fv.ts""",
            (HORIZON, SET_VERSION),
        )
        rows = cur.fetchall()
    ts = [r[0] for r in rows]
    symbols = [r[1] for r in rows]
    X = np.array([[float(v) if v is not None else math.nan for v in r[2]] for r in rows], dtype=float)
    y = np.array([float(r[3]) for r in rows], dtype=float)
    return names, ts, symbols, X, y


def fit_lambdarank(Xs, label_values, ts, train_idx, params):
    order = sorted(train_idx, key=lambda i: ts[i])
    rel = _int_relevance([label_values[i] for i in order], [ts[i] for i in order])
    dataset = lgb.Dataset(Xs[order], label=np.asarray(rel, dtype=float),
                          group=_group_counts([ts[i] for i in order]))
    return lgb.train({**params, "objective": "lambdarank"}, dataset, num_boost_round=NUM_ROUNDS)


def evaluate_lambdarank(Xs, label_values, y, ts, folds, symbols=None, collect=False):
    """Returns (per-ts real-IC dict, collected [(pred,y,ts,symbol)] if collect)."""
    ics = {}
    coll = []
    for fold in folds:
        if len(fold.train_idx) < 500 or len(fold.test_idx) < 50:
            continue
        tr, te = fold.train_idx, fold.test_idx
        model = fit_lambdarank(Xs, label_values, ts, tr, DEFAULT_LGB)
        pred = model.predict(Xs[te])
        ics.update(per_timestamp_ic(list(pred), [y[i] for i in te], [ts[i] for i in te]))
        if collect:
            coll.extend((float(pred[j]), float(y[i]), ts[i], symbols[i]) for j, i in enumerate(te))
    return ics, coll


def daily_block_tstat(ics):
    """IC series is already one value per day (overnight) -> simple t = mean/ (std/sqrt(n))."""
    series = [ics[t] for t in sorted(ics)]
    n = len(series)
    if n < 3:
        return math.nan, math.nan, n
    mean = statistics.mean(series)
    sd = statistics.stdev(series)
    se = sd / math.sqrt(n)
    return mean, (mean / se if se > 0 else math.nan), n


def lag1_autocorr(ics):
    series = [ics[t] for t in sorted(ics)]
    n = len(series)
    if n < 4:
        return math.nan
    mean = statistics.mean(series)
    dm = [x - mean for x in series]
    g0 = sum(v * v for v in dm)
    g1 = sum(dm[i] * dm[i - 1] for i in range(1, n))
    return g1 / g0 if g0 else math.nan


def main():
    print("loading panel ...", flush=True)
    with psycopg.connect(**DB_KWARGS) as conn:
        names, ts, symbols, X, y = load_panel(conn)
    print(f"panel: rows={len(y)} ts={len(set(ts))} syms={len(set(symbols))} feats={len(names)}", flush=True)

    # nocalendar feature selection (matches the reported deep run)
    drop = MICRO | CALENDAR
    feat_idx = [i for i, n in enumerate(names) if n not in drop]
    used_names = [names[i] for i in feat_idx]
    Xs = X[:, feat_idx]
    print(f"nocalendar features ({len(used_names)}): {used_names}", flush=True)

    folds = walk_forward_folds(ts, HORIZON_MIN, N_FOLDS)
    print(f"folds: {[(len(f.train_idx), len(f.test_idx)) for f in folds]}", flush=True)

    # --- REAL IC (collect preds for the persistence probe) ---
    print("\n=== REAL lambdarank IC ===", flush=True)
    real_ics, coll = evaluate_lambdarank(Xs, list(y), y, ts, folds, symbols=symbols, collect=True)
    rmean, rt, rn = daily_block_tstat(real_ics)
    print(f"real mean_ic={mean_ic(real_ics):.5f} daily-block t={rt:.3f} n_days={rn} "
          f"lag1_autocorr={lag1_autocorr(real_ics):.3f}", flush=True)

    # --- MULTI-SEED CANARY ---
    print("\n=== MULTI-SEED shuffle canary (lambdarank) ===", flush=True)
    seeds = [13, 1, 2, 3, 7, 42, 99, 123]
    canary_means = []
    canary_ts_for_block = None
    for seed in seeds:
        shuffled = shuffle_within_groups(list(y), ts, seed)
        c_ics, c_coll = evaluate_lambdarank(Xs, shuffled, y, ts, folds,
                                            symbols=symbols, collect=(seed == 13))
        cmean, ct, cn = daily_block_tstat(c_ics)
        canary_means.append(cmean)
        ac = lag1_autocorr(c_ics)
        print(f"  seed={seed:4d}: canary mean_ic={cmean:.5f} daily-block t={ct:.3f} "
              f"n_days={cn} lag1_autocorr={ac:.3f}", flush=True)
        if seed == 13:
            canary_coll = c_coll
    print(f"  canary mean across seeds = {statistics.mean(canary_means):.5f} "
          f"+/- {statistics.stdev(canary_means):.5f}  (range {min(canary_means):.5f}..{max(canary_means):.5f})",
          flush=True)

    # --- FEATURE IMPORTANCES (full-panel lambdarank) ---
    print("\n=== lambdarank feature importances (gain, full panel) ===", flush=True)
    full = fit_lambdarank(Xs, list(y), ts, list(range(len(y))), DEFAULT_LGB)
    gain = full.feature_importance(importance_type="gain")
    for nm, gv in sorted(zip(used_names, gain), key=lambda kv: -kv[1]):
        print(f"  {nm:14s} {gv:14.1f}", flush=True)

    # --- PERSISTENT-SELECTION PROBE (hypothesis b) ---
    # For each symbol: mean OOS lambdarank prediction (day-invariant component) vs
    # mean overnight return. If a stock's *average* predicted rank correlates with its
    # *average* overnight return, the within-day shuffle CANNOT remove that -> the canary
    # is partly REAL persistent cross-sectional structure, not pure leakage/noise.
    print("\n=== PERSISTENT cross-sectional selection probe ===", flush=True)
    by_sym_pred = defaultdict(list)
    by_sym_y = defaultdict(list)
    for pred, yi, t, sym in coll:
        if not (math.isnan(pred) or math.isnan(yi)):
            by_sym_pred[sym].append(pred)
            by_sym_y[sym].append(yi)
    syms = [s for s in by_sym_pred if len(by_sym_pred[s]) >= 30]
    mean_pred = [statistics.mean(by_sym_pred[s]) for s in syms]
    mean_y = [statistics.mean(by_sym_y[s]) for s in syms]
    # rank correlation between per-symbol mean pred and per-symbol mean overnight return
    def rankcorr(a, b):
        ra = _ranks(a); rb = _ranks(b)
        return _pearson(ra, rb)
    print(f"  n_symbols(>=30 obs)={len(syms)}", flush=True)
    print(f"  corr(mean_pred, mean_overnight_ret) Spearman = {rankcorr(mean_pred, mean_y):.4f}", flush=True)
    print(f"  corr(mean_pred, mean_overnight_ret) Pearson  = {_pearson(mean_pred, mean_y):.4f}", flush=True)

    # Same for the CANARY model (seed 13): its per-symbol mean pred vs the SAME real per-symbol y.
    c_sym_pred = defaultdict(list)
    for pred, yi, t, sym in canary_coll:
        if not math.isnan(pred):
            c_sym_pred[sym].append(pred)
    c_syms = [s for s in syms if s in c_sym_pred and len(c_sym_pred[s]) >= 30]
    c_mean_pred = [statistics.mean(c_sym_pred[s]) for s in c_syms]
    c_mean_y = [statistics.mean(by_sym_y[s]) for s in c_syms]
    print(f"  CANARY corr(mean_pred, mean_overnight_ret) Spearman = {rankcorr(c_mean_pred, c_mean_y):.4f}", flush=True)

    # How much of the per-symbol overnight-return spread is explained by feature-driven
    # persistent ranking? Show the per-symbol mean overnight return spread (is there a real
    # cross-sectional premium to latch onto?).
    spread = sorted(mean_y)
    print(f"  per-symbol mean overnight ret: p10={np.percentile(mean_y,10):.5f} "
          f"median={np.percentile(mean_y,50):.5f} p90={np.percentile(mean_y,90):.5f} "
          f"std={statistics.pstdev(mean_y):.5f}", flush=True)

    # --- DROP-SUSPECT re-measure (gap_from_open, range_pct, price-level proxies) ---
    print("\n=== DROP suspicious persistent features, re-measure canary (seed 13) ===", flush=True)
    for suspect in ["gap_from_open", "range_pct", "vwap_dev", "vol_30m", "vol_60m"]:
        if suspect not in used_names:
            continue
        keep = [i for i, n in enumerate(used_names) if n != suspect]
        Xs2 = Xs[:, keep]
        shuffled = shuffle_within_groups(list(y), ts, 13)
        c_ics, _ = evaluate_lambdarank(Xs2, shuffled, y, ts, folds)
        cmean, ct, cn = daily_block_tstat(c_ics)
        r_ics, _ = evaluate_lambdarank(Xs2, list(y), y, ts, folds)
        rmn = mean_ic(r_ics)
        print(f"  drop {suspect:14s}: real_IC={rmn:.5f}  canary={cmean:.5f} (t={ct:.2f})", flush=True)


def _ranks(values):
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    for r, i in enumerate(order):
        ranks[i] = float(r)
    return ranks


def _pearson(xs, ys):
    n = len(xs)
    if n < 2:
        return math.nan
    mx, my = statistics.mean(xs), statistics.mean(ys)
    cov = sum((x - mx) * (yy - my) for x, yy in zip(xs, ys))
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    sy = math.sqrt(sum((yy - my) ** 2 for yy in ys))
    return cov / (sx * sy) if sx and sy else math.nan


if __name__ == "__main__":
    main()
