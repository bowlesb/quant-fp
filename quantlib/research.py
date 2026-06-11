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


def run_experiment(X, y, ts, *, symbols=None, label="raw", feature_idx=None, params=None,
                   n_folds=5, horizon_minutes=30, cadence_min=30, num_rounds=200, seed=13,
                   cost_bps_oneway=2.0, borrow_bps_annual=50.0):
    """Walk-forward train (on the transformed label) + measure IC of predictions vs the
    ACTUAL forward return, PLUS a net-of-cost L/S backtest (after-cost Sharpe + breakeven
    cost) — the economic gate IC hides. Returns a result dict. label in {raw, rank}."""
    Xs = X[:, feature_idx] if feature_idx is not None else X
    params = params or DEFAULT_LGB
    folds = walk_forward_folds(ts, horizon_minutes, n_folds)

    def transform(vals):
        return within_ts_rank(vals, ts) if label == "rank" else list(vals)

    def evaluate(fit_label, collect=False):
        ics = {}
        coll: list[tuple] = []
        for fold in folds:
            if len(fold.train_idx) < 500 or len(fold.test_idx) < 50:
                continue
            tr, te = fold.train_idx, fold.test_idx
            booster = lgb.train(
                params, lgb.Dataset(Xs[tr], label=np.asarray([fit_label[i] for i in tr], dtype=float)),
                num_boost_round=num_rounds,
            )
            pred = booster.predict(Xs[te])
            ics.update(per_timestamp_ic(list(pred), [y[i] for i in te], [ts[i] for i in te]))
            if collect and symbols is not None:
                coll.extend((float(pred[j]), float(y[i]), ts[i], symbols[i]) for j, i in enumerate(te))
        return ics, coll

    real, test_preds = evaluate(transform(y), collect=True)
    shuffled = shuffle_within_groups(list(y), ts, seed)
    canary, _ = evaluate(transform(shuffled))
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
    full = lgb.train(params, lgb.Dataset(Xs, label=np.asarray(transform(y), dtype=float)),
                     num_boost_round=num_rounds)
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
