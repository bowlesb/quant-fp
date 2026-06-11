"""Shared research/experiment runner — used by the always-on experimenter service
(the Modeller's sandbox). Loads the panel, applies a label transform, trains a model
through the leakage-checked walk-forward harness, and returns a structured result
(IC vs the ACTUAL forward return, Newey-West t, and the within-group shuffle canary).

Curious + unattached: run far more experiments than we'd ever ship. The canary is the
arbiter; on this thin panel, treat IC/t as exploration, not edge.
"""
import math
from collections import defaultdict

import lightgbm as lgb
import numpy as np
import psycopg

from quantlib.backtest import (
    long_short_backtest,
    mean_ic,
    newey_west_tstat,
    per_timestamp_ic,
    shuffle_within_groups,
    walk_forward_folds,
)

DEFAULT_LGB = dict(
    objective="regression", learning_rate=0.05, num_leaves=31, min_data_in_leaf=50,
    bagging_fraction=0.8, bagging_freq=1, feature_fraction=0.8, verbose=-1,
)
HORIZON_MIN = {"fwd_30m": 30, "fwd_60m": 60, "overnight": 1440}   # overnight purge ~1 day


def load_panel(conn: psycopg.Connection, horizon: str, set_version: str):
    with conn.cursor() as cur:
        cur.execute("SELECT names FROM feature_sets WHERE version=%s", (set_version,))
        names = cur.fetchone()[0]
        cur.execute(
            """SELECT fv.ts, fv.symbol, fv.vector, l.value
               FROM feature_vectors fv
               JOIN labels l ON l.symbol=fv.symbol AND l.ts=fv.ts AND l.horizon=%s
               WHERE fv.source='historical' AND fv.set_version=%s
               ORDER BY fv.ts""",
            (horizon, set_version),
        )
        rows = cur.fetchall()
    ts = [r[0] for r in rows]
    symbols = [r[1] for r in rows]
    X = np.array([[float(v) if v is not None else math.nan for v in r[2]] for r in rows], dtype=float)
    y = np.array([float(r[3]) for r in rows], dtype=float)
    return names, ts, symbols, X, y


def within_ts_rank(y, ts) -> list[float]:
    """Rank each value within its timestamp's cross-section, normalized to [-1, 1]."""
    groups: dict[object, list[int]] = defaultdict(list)
    for i, t in enumerate(ts):
        groups[t].append(i)
    out = [0.0] * len(y)
    for idxs in groups.values():
        order = sorted(idxs, key=lambda i: y[i])
        m = len(order)
        for r, i in enumerate(order):
            out[i] = (2.0 * r / (m - 1) - 1.0) if m > 1 else 0.0
    return out


VOL_FLOOR = 0.001                                  # floor for the vol-scaled denominator


def _int_relevance(vals, ts_list) -> list[int]:
    """Within-timestamp rank bucketed to 0..30 — LightGBM lambdarank's integer relevance
    (default label_gain has 31 entries -> max valid label is 30)."""
    groups: dict[object, list[int]] = defaultdict(list)
    for i, t in enumerate(ts_list):
        groups[t].append(i)
    out = [0] * len(vals)
    for idxs in groups.values():
        order = sorted(idxs, key=lambda i: vals[i])
        m = len(order)
        for rank, i in enumerate(order):
            out[i] = int(round(30 * rank / (m - 1))) if m > 1 else 0
    return out


def _group_counts(ts_sorted) -> list[int]:
    """Per-timestamp row counts for a ts-SORTED sequence (lambdarank `group`)."""
    counts: list[int] = []
    prev, n = object(), 0
    for t in ts_sorted:
        if t == prev:
            n += 1
        else:
            if n:
                counts.append(n)
            prev, n = t, 1
    if n:
        counts.append(n)
    return counts


def run_experiment(X, y, ts, *, symbols=None, vol_scaler=None, label="raw", feature_idx=None,
                   params=None, n_folds=5, horizon_minutes=30, cadence_min=30, num_rounds=200,
                   seed=13, cost_bps_oneway=2.0, borrow_bps_annual=50.0):
    """Walk-forward train (on the transformed label) + measure IC of predictions vs the
    ACTUAL forward return, PLUS a net-of-cost L/S backtest. label in {raw, rank, vol_scaled}.
    vol_scaled fits y/realized_vol (stops the model ranking volatility instead of alpha);
    pass vol_scaler (per-row realized vol). IC/P&L are always measured vs the RAW return."""
    Xs = X[:, feature_idx] if feature_idx is not None else X
    params = params or DEFAULT_LGB
    folds = walk_forward_folds(ts, horizon_minutes, n_folds)

    def transform(vals):
        if label == "rank":
            return within_ts_rank(vals, ts)
        if label == "vol_scaled" and vol_scaler is not None:
            return [v / (abs(scl) if (scl == scl and abs(scl) > VOL_FLOOR) else VOL_FLOOR)
                    for v, scl in zip(vals, vol_scaler)]   # scl==scl filters NaN vol
        return list(vals)

    is_rank_obj = label == "lambdarank"

    def _fit(train_idx, label_values):
        if is_rank_obj:                            # learning-to-rank: needs group + int relevance
            order = sorted(train_idx, key=lambda i: ts[i])
            rel = _int_relevance([label_values[i] for i in order], [ts[i] for i in order])
            dataset = lgb.Dataset(Xs[order], label=np.asarray(rel, dtype=float),
                                  group=_group_counts([ts[i] for i in order]))
            return lgb.train({**params, "objective": "lambdarank"}, dataset, num_boost_round=num_rounds)
        fit = transform(label_values)
        return lgb.train(params, lgb.Dataset(Xs[train_idx],
                         label=np.asarray([fit[i] for i in train_idx], dtype=float)),
                         num_boost_round=num_rounds)

    def evaluate(label_values, collect=False):
        ics = {}
        coll: list[tuple] = []
        for fold in folds:
            if len(fold.train_idx) < 500 or len(fold.test_idx) < 50:
                continue
            tr, te = fold.train_idx, fold.test_idx
            pred = _fit(tr, label_values).predict(Xs[te])
            ics.update(per_timestamp_ic(list(pred), [y[i] for i in te], [ts[i] for i in te]))
            if collect and symbols is not None:
                coll.extend((float(pred[j]), float(y[i]), ts[i], symbols[i]) for j, i in enumerate(te))
        return ics, coll

    real, test_preds = evaluate(list(y), collect=True)
    shuffled = shuffle_within_groups(list(y), ts, seed)
    canary, _ = evaluate(shuffled)
    lag = max(1, horizon_minutes // cadence_min)
    # Net-of-cost L/S backtest on the out-of-sample predictions (the economic gate).
    periods_per_year = 252.0 * (390.0 / cadence_min)
    backtest = long_short_backtest(
        [c[0] for c in test_preds], [c[1] for c in test_preds],
        [c[2] for c in test_preds], [c[3] for c in test_preds],
        cost_bps_oneway=cost_bps_oneway, borrow_bps_annual=borrow_bps_annual,
        periods_per_year=periods_per_year,
    ) if test_preds else {}
    # Feature importances (gain) from a model on the full panel — lets the Modeller
    # interrogate WHICH features carry signal (and which are dead weight).
    full = _fit(list(range(len(y))), list(y))
    importances = [round(float(v), 1) for v in full.feature_importance(importance_type="gain")]
    return {
        "mean_ic": round(mean_ic(real), 5),
        "nw_t": round(newey_west_tstat(real, lag), 3),
        "canary_ic": round(mean_ic(canary), 5),
        "n_test_ts": len(real),
        "n_rows": int(len(y)),
        "n_features": int(Xs.shape[1]),
        "label": label,
        "gain_importance": importances,
        "net_per_period": backtest.get("net_per_period"),
        "gross_per_period": backtest.get("gross_per_period"),
        "sharpe_net": backtest.get("sharpe_net"),
        "breakeven_cost_bps": backtest.get("breakeven_cost_bps"),
        "mean_turnover": backtest.get("mean_turnover"),
    }
