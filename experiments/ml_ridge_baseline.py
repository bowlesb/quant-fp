"""explorer-ml 001 — regularized-linear baseline (closed-form ridge) vs the LightGBM monoculture.

WHY (proposal 001): every verdict in EXPERIMENTS.md is LightGBM. No regularized-linear floor
exists anywhere in the repo. Two load-bearing payoffs:
  (a) If ridge MATCHES the GBM IC (~0.027), the whole 30m signal is LINEAR — the GBM's
      nonlinearity buys nothing on this panel, and the "momentum is dead at 30m" finding (a GBM
      gain-attribution claim) must be re-stated via ridge's signed standardized COEFFICIENTS,
      which cannot bury a weak-but-real linear contribution the way tree gain can.
  (b) Linear predictions are smoother across adjacent timestamps -> possibly LOWER turnover ->
      HIGHER breakeven. That is exactly the economic lever the org is chasing (every price signal
      is real but dies on turnover: breakeven ~1.4bps < ~2bps cost).

HEADLINE = ridge breakeven_cost_bps - GBM breakeven_cost_bps. Plus the standardized-coefficient
ranking (does ret_5m dominate? is any momentum coef >= 50% of |ret_5m|? that would overturn
"momentum is dead" as a model artifact).

scikit-learn is NOT in the experimenter image (checked), so this uses the CLOSED-FORM ridge
(XtX + alpha*I)^-1 Xt y on standardized, median-imputed features — no new dependency, never
blocks on packaging. ElasticNet (L1) has no closed form and is DROPPED from this run; the L2
ridge path is what answers the floor + turnover questions.

GATES (identical to the battery, 001 spec):
  1. Net-of-cost L/S backtest (cost_bps=2.0); reports gross/net/sharpe/breakeven/turnover.
  2. Shuffle-WITHIN-timestamp canary (the SAME shuffle_within_groups(y, ts, SEED=13)) — a clean
     linear harness scores ~0. ALSO catches standardization leakage: the scaler + impute are fit
     on TRAIN folds ONLY (fold-local), so the canary stays clean.
  3. Label de-fragmentation: native 30m cadence (this run is 30m only; the floor question lives
     at 30m where the signal is).
  4. Survivorship neutralization: per-symbol-demean the OOS predictions, re-run the L/S backtest.

Run as a MODULE from /app (so quantlib + experiments.battery resolve):
  docker compose exec -T -w /app -e SET_VERSION=v1.1.1 experimenter python -m experiments.ml_ridge_baseline
  # fast smoke (last N days, proves the harness end-to-end):
  docker compose exec -T -w /app -e SET_VERSION=v1.1.1 -e SMOKE_DAYS=120 experimenter python -m experiments.ml_ridge_baseline
"""

import json
import os
import sys
from datetime import datetime, timezone

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
from quantlib.research import load_panel, within_ts_rank

from experiments.battery import PRICE_ONLY_DROP, filter_smoke, per_symbol_demean

SET_VERSION = os.environ.get("SET_VERSION", "v1.1.1")
SMOKE_DAYS = int(os.environ["SMOKE_DAYS"]) if os.environ.get("SMOKE_DAYS") else None
RESULTS = os.environ.get("RIDGE_RESULTS", "/app/experiments/ml_ridge_results.jsonl")
FORBIDDEN_VERSIONS = {"v1.0.0", "v1.1.0"}
MIN_ROWS = 1000

SEED = 13
N_FOLDS = 5
# Ridge L2 penalty grid. Picked per-run by an inner train/validation split on the FIRST
# train fold only (no test peeking). Spans weak->strong shrinkage on standardized features.
ALPHA_GRID = [1.0, 10.0, 100.0, 1000.0]
HORIZON = "fwd_30m"
HORIZON_MINUTES = 30
CADENCE_MIN = 30

DB_KWARGS = {
    "host": os.environ["DB_HOST"],
    "port": int(os.environ.get("DB_PORT", "5432")),
    "dbname": os.environ["DB_NAME"],
    "user": os.environ["DB_USER"],
    "password": os.environ["DB_PASSWORD"],
}


def fit_ridge(X_train: np.ndarray, y_train: np.ndarray, alpha: float) -> np.ndarray:
    """Closed-form ridge weights for an intercept-augmented design: w = (XtX + alpha*I)^-1 Xt y.
    The intercept column (last) is NOT penalized. X_train is already standardized + imputed.
    """
    n_features = X_train.shape[1]
    design = np.hstack([X_train, np.ones((X_train.shape[0], 1))])
    penalty = alpha * np.eye(n_features + 1)
    penalty[-1, -1] = 0.0  # do not shrink the intercept
    gram = design.T @ design + penalty
    weights = np.linalg.solve(gram, design.T @ y_train)
    return weights


def predict_ridge(X: np.ndarray, weights: np.ndarray) -> np.ndarray:
    design = np.hstack([X, np.ones((X.shape[0], 1))])
    return design @ weights


def standardize_impute(
    X_fit: np.ndarray, X_apply: np.ndarray
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Fit per-column median (for NaN impute) + mean/std (for standardization) on X_fit ROWS
    ONLY (fold-local, leakage-safe), then apply to BOTH. Returns (X_fit_z, X_apply_z, mean, std)
    where the returned mean/std are over the IMPUTED-standardized space (for coefficient scaling).
    GBM handles NaN natively; a linear model cannot, so median-impute is the honest minimal choice.
    """
    medians = np.nanmedian(X_fit, axis=0)
    medians = np.where(np.isnan(medians), 0.0, medians)  # all-NaN column -> 0.0
    fit_imp = np.where(np.isnan(X_fit), medians, X_fit)
    apply_imp = np.where(np.isnan(X_apply), medians, X_apply)
    mean = fit_imp.mean(axis=0)
    std = fit_imp.std(axis=0)
    std = np.where(std < 1e-12, 1.0, std)  # constant column -> no scaling, no div0
    return (fit_imp - mean) / std, (apply_imp - mean) / std, mean, std


def pick_alpha(
    Xs: np.ndarray,
    label_source: np.ndarray,
    ts: list[datetime],
    first_train_idx: list[int],
) -> float:
    """Pick ridge alpha on an INNER time-ordered split of the first training fold ONLY
    (never touches any test block). Inner-val IC vs the inner-val realized return."""
    order = sorted(first_train_idx, key=lambda i: ts[i])
    cut = int(0.8 * len(order))
    inner_train, inner_val = order[:cut], order[cut:]
    if len(inner_train) < 100 or len(inner_val) < 50:
        return ALPHA_GRID[len(ALPHA_GRID) // 2]  # too thin to tune -> grid midpoint
    Xt_z, Xv_z, _, _ = standardize_impute(Xs[inner_train], Xs[inner_val])
    best_alpha, best_ic = ALPHA_GRID[0], -np.inf
    for alpha in ALPHA_GRID:
        weights = fit_ridge(Xt_z, label_source[inner_train], alpha)
        pred = predict_ridge(Xv_z, weights)
        ics = per_timestamp_ic(
            list(pred), list(label_source[inner_val]), [ts[i] for i in inner_val]
        )
        score = mean_ic(ics) if ics else -np.inf
        if score > best_ic:
            best_alpha, best_ic = alpha, score
    return best_alpha


def collect_oos_ridge(
    Xs: np.ndarray,
    label_source: np.ndarray,
    y: np.ndarray,
    ts: list[datetime],
    symbols: list[str],
    alpha: float,
) -> tuple[list[float], list[float], list[datetime], list[str]]:
    """Walk-forward OOS predictions from closed-form ridge, fold-local standardize+impute.
    `label_source` is the TRAINING target (raw return, within-ts rank, or shuffled); the
    returned realized series is always the RAW forward return `y`."""
    folds = walk_forward_folds(ts, HORIZON_MINUTES, N_FOLDS)
    preds: list[float] = []
    realized: list[float] = []
    pred_ts: list[datetime] = []
    pred_sym: list[str] = []
    for fold in folds:
        if len(fold.train_idx) < 500 or len(fold.test_idx) < 50:
            continue
        Xtr_z, Xte_z, _, _ = standardize_impute(Xs[fold.train_idx], Xs[fold.test_idx])
        weights = fit_ridge(Xtr_z, label_source[fold.train_idx], alpha)
        out = predict_ridge(Xte_z, weights)
        for j, i in enumerate(fold.test_idx):
            preds.append(float(out[j]))
            realized.append(float(y[i]))
            pred_ts.append(ts[i])
            pred_sym.append(symbols[i])
    return preds, realized, pred_ts, pred_sym


def standardized_coefficients(
    Xs: np.ndarray,
    label_source: np.ndarray,
    ts: list[datetime],
    names: list[str],
    alpha: float,
) -> list[tuple[str, float]]:
    """Full-panel ridge coefficients on standardized features (comparable across features) —
    the signed driver ranking that re-states or overturns 'momentum is dead' independent of the
    GBM. Standardized X means |coef| is the per-feature contribution per 1-sigma move.
    """
    X_z, _, _, _ = standardize_impute(Xs, Xs)
    weights = fit_ridge(X_z, label_source, alpha)[:-1]  # drop the intercept
    return sorted(zip(names, [float(w) for w in weights]), key=lambda kv: -abs(kv[1]))


def transform_label(label: str, y: np.ndarray, ts: list[datetime]) -> np.ndarray:
    if label == "rank":
        return np.asarray(within_ts_rank(y, ts), dtype=float)
    return y


def run_label(
    Xs: np.ndarray,
    y: np.ndarray,
    ts: list[datetime],
    symbols: list[str],
    names: list[str],
    label: str,
) -> dict[str, object]:
    """All four gates for one ridge label config (raw or within-ts rank target)."""
    periods_per_year = 252.0 * (390.0 / CADENCE_MIN)
    lag = max(1, HORIZON_MINUTES // CADENCE_MIN)

    target = transform_label(label, y, ts)
    folds = walk_forward_folds(ts, HORIZON_MINUTES, N_FOLDS)
    first_train = next(
        (f.train_idx for f in folds if len(f.train_idx) >= 500), folds[0].train_idx
    )
    alpha = pick_alpha(Xs, target, ts, first_train)

    preds, realized, pred_ts, pred_sym = collect_oos_ridge(
        Xs, target, y, ts, symbols, alpha
    )
    real_ic = per_timestamp_ic(preds, realized, pred_ts)

    # Canary target = the SAME label transform applied to the within-ts-shuffled return, so the
    # canary trains on an identically-distributed but leakage-free target (rank-of-shuffled for
    # the rank config, raw-shuffled for raw). Graded against the RAW realized return `y`.
    shuffled = np.asarray(shuffle_within_groups(list(y), ts, SEED), dtype=float)
    canary_target = transform_label(label, shuffled, ts)
    canary_preds, canary_real, canary_ts, _ = collect_oos_ridge(
        Xs, canary_target, y, ts, symbols, alpha
    )
    canary_ic = per_timestamp_ic(canary_preds, canary_real, canary_ts)

    backtest_raw = long_short_backtest(
        preds, realized, pred_ts, pred_sym, periods_per_year=periods_per_year
    )
    neutral_preds = per_symbol_demean(preds, pred_sym)
    backtest_neutral = long_short_backtest(
        neutral_preds, realized, pred_ts, pred_sym, periods_per_year=periods_per_year
    )

    coefs = standardized_coefficients(Xs, target, ts, names, alpha)
    return {
        "model": "ridge_closed_form",
        "label": label,
        "alpha": alpha,
        "n_rows": int(len(y)),
        "n_features": int(Xs.shape[1]),
        "n_test_ts": len(real_ic),
        "mean_ic": round(mean_ic(real_ic), 5),
        "nw_t": round(newey_west_tstat(real_ic, lag), 3),
        "canary_ic": round(mean_ic(canary_ic), 5),
        "net_per_period": backtest_raw.get("net_per_period"),
        "sharpe_net": backtest_raw.get("sharpe_net"),
        "breakeven_cost_bps": backtest_raw.get("breakeven_cost_bps"),
        "mean_turnover": backtest_raw.get("mean_turnover"),
        "survivorship_neutral_sharpe": backtest_neutral.get("sharpe_net"),
        "survivorship_neutral_net": backtest_neutral.get("net_per_period"),
        "top_coefficients": [f"{name}:{coef:+.5f}" for name, coef in coefs[:8]],
    }


def main() -> None:
    if SET_VERSION in FORBIDDEN_VERSIONS:
        sys.exit(
            f"REFUSING SET_VERSION={SET_VERSION}: dirty labels (overwritten by v1.1.1). Use v1.1.1."
        )
    mode = f"SMOKE (last {SMOKE_DAYS}d)" if SMOKE_DAYS is not None else "FULL"
    print(
        f"RIDGE LINEAR BASELINE | set={SET_VERSION} | mode={mode} | horizon={HORIZON}",
        flush=True,
    )

    with psycopg.connect(**DB_KWARGS) as conn:
        names, ts, symbols, X, y = load_panel(conn, HORIZON, SET_VERSION)
    if SMOKE_DAYS is not None:
        ts, symbols, X, y = filter_smoke(ts, symbols, X, y, SMOKE_DAYS)
    if len(y) < MIN_ROWS:
        sys.exit(
            f"PANEL TOO SMALL: {HORIZON} set={SET_VERSION} has {len(y)} rows (< {MIN_ROWS})."
        )

    feature_idx = [i for i, name in enumerate(names) if name not in PRICE_ONLY_DROP]
    Xs = X[:, feature_idx]
    used = [names[i] for i in feature_idx]
    n_days = len({t.date() for t in ts})
    print(
        f"=== {HORIZON} | {len(y)} rows | {n_days} days | price-only features "
        f"({len(used)}): {used} ===",
        flush=True,
    )

    records: list[dict[str, object]] = []
    run_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for label in ["raw", "rank"]:
        result = run_label(Xs, y, ts, symbols, used, label)
        result["horizon"] = HORIZON
        result["set_version"] = SET_VERSION
        result["run_at"] = run_at
        result["mode"] = mode
        records.append(result)
        print(
            f"  ridge/{label:5} a={result['alpha']:>7} IC {result['mean_ic']:>9} "
            f"t {result['nw_t']:>7} canary {result['canary_ic']:>9} | "
            f"net {str(result['net_per_period']):>11} sharpe {str(result['sharpe_net']):>7} "
            f"breakeven {str(result['breakeven_cost_bps']):>7}bps turn {result['mean_turnover']} "
            f"|| SURV-OUT sharpe {str(result['survivorship_neutral_sharpe']):>7}",
            flush=True,
        )
        print(f"    coefs(std): {result['top_coefficients']}", flush=True)

    with open(RESULTS, "a") as out:
        for record in records:
            out.write(json.dumps(record) + "\n")
    print(f"\nwrote {len(records)} records -> {RESULTS}", flush=True)
    print(
        "HEADLINE: compare ridge breakeven_cost_bps vs the GBM price-only ~1.4bps, and read "
        "the standardized coefficients — does ret_5m dominate / is any momentum coef >= 50% "
        "of |ret_5m|? (the latter would overturn 'momentum is dead' as a GBM artifact).",
        flush=True,
    )


if __name__ == "__main__":
    main()
