"""Model trainer — the 'train' step of the E2E slice.

Loads the training panel (feature_vectors historical ⨝ labels for one horizon,
pinned to a feature-set version), runs purged/embargoed walk-forward LightGBM
through quantlib.backtest, and reports per-timestamp rank-IC + Newey-West t + the
shuffle-label canary. FIRST RESULT IS A PIPELINE CHECK, NOT EDGE — the canary is the
arbiter; a thin panel makes the IC/t untrustworthy. Saves the final booster + feature
names to /models for the model-server to load.

Usage: python main.py [horizon_name]   (default fwd_30m)
"""
import json
import math
import os
import sys

import lightgbm as lgb
import numpy as np
import psycopg

from quantlib.backtest import (
    mean_ic,
    newey_west_tstat,
    per_timestamp_ic,
    shuffle_within_groups,
    walk_forward_folds,
)

SET_VERSION = os.environ.get("FEATURE_SET_VERSION", "v1.0.0")
CADENCE_MIN = int(os.environ.get("FEATURE_CADENCE_MIN", "30"))
N_FOLDS = int(os.environ.get("TRAIN_FOLDS", "5"))
MODELS_DIR = os.environ.get("MODELS_DIR", "/models")

DB_KWARGS = {
    "host": os.environ["DB_HOST"], "port": int(os.environ.get("DB_PORT", "5432")),
    "dbname": os.environ["DB_NAME"], "user": os.environ["DB_USER"],
    "password": os.environ["DB_PASSWORD"],
}

HORIZON_MINUTES = {"fwd_30m": 30, "fwd_60m": 60}

LGB_PARAMS = dict(
    objective="regression", learning_rate=0.05, num_leaves=31,
    min_data_in_leaf=50, bagging_fraction=0.8, bagging_freq=1,
    feature_fraction=0.8, verbose=-1,
)
NUM_ROUNDS = 200


def _fit(X, y):
    return lgb.train(LGB_PARAMS, lgb.Dataset(X, label=np.asarray(y, dtype=float)),
                     num_boost_round=NUM_ROUNDS)


def load_panel(horizon: str):
    with psycopg.connect(**DB_KWARGS) as conn, conn.cursor() as cur:
        cur.execute("SELECT names FROM feature_sets WHERE version=%s", (SET_VERSION,))
        names = cur.fetchone()[0]
        cur.execute(
            """SELECT fv.ts, fv.symbol, fv.vector, l.value
               FROM feature_vectors fv
               JOIN labels l ON l.symbol=fv.symbol AND l.ts=fv.ts AND l.horizon=%s
               WHERE fv.source='historical' AND fv.set_version=%s
               ORDER BY fv.ts""",
            (horizon, SET_VERSION),
        )
        rows = cur.fetchall()
    ts = [r[0] for r in rows]
    symbols = [r[1] for r in rows]
    X = np.array([[float(v) if v is not None else math.nan for v in r[2]] for r in rows], dtype=float)
    y = np.array([float(r[3]) for r in rows], dtype=float)
    return names, ts, symbols, X, y


def evaluate(ts, X, y, label_for_fit):
    """Walk-forward train/predict; return the per-timestamp IC dict over all folds."""
    horizon = HORIZON_MINUTES[HORIZON]
    folds = walk_forward_folds(ts, horizon_minutes=horizon, n_folds=N_FOLDS)
    all_ic = {}
    for fold in folds:
        if len(fold.train_idx) < 500 or len(fold.test_idx) < 50:
            continue
        tr, te = fold.train_idx, fold.test_idx
        model = _fit(X[tr], [label_for_fit[i] for i in tr])
        pred = model.predict(X[te])
        ic = per_timestamp_ic(list(pred), [y[i] for i in te], [ts[i] for i in te])
        all_ic.update(ic)
    return all_ic


def main() -> None:
    global HORIZON
    HORIZON = sys.argv[1] if len(sys.argv) > 1 else "fwd_30m"
    names, ts, symbols, X, y = load_panel(HORIZON)
    print(f"panel: {len(y)} rows, {X.shape[1]} features, {len(set(ts))} timestamps, "
          f"set={SET_VERSION}, horizon={HORIZON}")
    if len(y) < 1000:
        print("panel too small to train meaningfully; aborting")
        return

    real_ic = evaluate(ts, X, list(y), list(y))
    print(f"REAL    : mean rank-IC={mean_ic(real_ic):.4f}  "
          f"NW t={newey_west_tstat(real_ic, lag=max(1, HORIZON_MINUTES[HORIZON]//CADENCE_MIN)):.2f}  "
          f"({len(real_ic)} test timestamps)")

    shuffled = shuffle_within_groups(list(y), ts, seed=13)
    canary_ic = evaluate(ts, X, shuffled, list(y))
    print(f"CANARY  : mean rank-IC={mean_ic(canary_ic):.4f}  (should be ~0; arbiter of leakage)")
    print("NOTE: first run is a PIPELINE CHECK, not an edge claim. Trust the canary, "
          "not the IC, on this thin panel.")

    # Save a final model trained on the whole panel for the model-server.
    final = _fit(X, y)
    os.makedirs(MODELS_DIR, exist_ok=True)
    final.save_model(os.path.join(MODELS_DIR, f"model_{HORIZON}.txt"))
    with open(os.path.join(MODELS_DIR, f"model_{HORIZON}.meta.json"), "w") as f:
        json.dump({"set_version": SET_VERSION, "horizon": HORIZON, "features": names}, f)
    print(f"saved model_{HORIZON}.txt + meta to {MODELS_DIR}")


if __name__ == "__main__":
    main()
