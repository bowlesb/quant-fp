"""Price-only cost-gated 4-gate battery — the Modeller's verdict harness.

ONE deterministic command that reproduces the full price-only edge battery behind the M1
verdict, so the CLEAN-panel re-run is a single invocation instead of ad-hoc /tmp scripts
that vanish. The four gates, per (horizon x label) config, all PRICE-ONLY (calendar and
microstructure features dropped):

  1. Net-of-cost L/S backtest   — the economic gate (a positive rank-IC still loses money
                                  after costs); reports gross/net/sharpe_net/breakeven.
  2. Shuffle-label canary       — train on labels permuted WITHIN each timestamp; a clean
                                  harness scores ~0. This is the leakage / overfit-floor
                                  arbiter (|IC| below the canary is indistinguishable from 0).
  3. Label de-fragmentation     — overnight labels are stored as ONE 15:30-ET cross-section
                                  per day (baked into the labels table); 30m uses its native
                                  cadence. This keeps overnight turnover ~1 rebalance/day.
  4. Survivorship neutralization — per-symbol-DEMEAN the out-of-sample predictions, then re-run
                                  the L/S backtest. This strips the persistent per-symbol drift
                                  (ex-post survivors) and leaves within-symbol TIMING alpha only.
                                  If sharpe_net collapses, the "edge" was survivorship.

Deterministic: fixed LightGBM seeds (DEFAULT_LGB) + fixed shuffle seed. FAILS LOUD on an
empty/too-small panel — this guards against the mid-rebuild race that previously recorded
n_rows=0 as a permanent "panel too small" result.

Run inside the experimenter container (quantlib baked into the image; experiments/ + docs/
are bind-mounted):

  docker compose exec -T -e SET_VERSION=v1.1.0 experimenter python experiments/battery.py
  # fast smoke (last ~60 days, proves the harness end-to-end):
  docker compose exec -T -e SET_VERSION=v1.1.0 -e SMOKE_DAYS=60 experimenter python experiments/battery.py
  # CLEAN re-run (full depth, equities-only panel):
  docker compose exec -T -e SET_VERSION=<clean-version> experimenter python experiments/battery.py
"""
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone

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
from quantlib.features import CALENDAR_NAMES, MICRO_NAMES
from quantlib.research import (
    DEFAULT_LGB,
    VOL_FLOOR,
    _group_counts,
    _int_relevance,
    load_panel,
    within_ts_rank,
)

DB_KWARGS = {
    "host": os.environ["DB_HOST"],
    "port": int(os.environ.get("DB_PORT", "5432")),
    "dbname": os.environ["DB_NAME"],
    "user": os.environ["DB_USER"],
    "password": os.environ["DB_PASSWORD"],
}

SET_VERSION = os.environ.get("SET_VERSION", "v1.1.0")
SMOKE_DAYS = int(os.environ["SMOKE_DAYS"]) if os.environ.get("SMOKE_DAYS") else None
MIN_ROWS = int(os.environ.get("MIN_ROWS", "50000"))
OUT_JSONL = os.environ.get("BATTERY_OUT", "/app/experiments/battery_results.jsonl")

SEED = 13
N_FOLDS = 5
# Labels have no version column and were OVERWRITTEN in place by the clean v1.1.1 recompute
# (demeaned over the clean ~715-equity universe). So ANY pre-clean feature version (v1.0.0, v1.1.0)
# joined to the current labels = dirty-features ⨝ clean-labels = a meaningless chimera. The canonical
# pre-clean results already live in experiments/results.jsonl. Refuse them in CODE, not memory.
FORBIDDEN_VERSIONS = {"v1.0.0", "v1.1.0"}
NUM_ROUNDS = 200
LABELS = ["raw", "rank", "vol_scaled", "lambdarank"]
# horizon -> (purge horizon_minutes, cadence_min). overnight = ~1 rebalance/day (390 min RTH).
HORIZON_CFG = {"fwd_30m": (30, 30), "overnight": (390, 390)}
PRICE_ONLY_DROP = set(MICRO_NAMES) | set(CALENDAR_NAMES)


def transform_label(
    y: np.ndarray, ts: list[datetime], label: str, vol_scaler: np.ndarray
) -> list[float]:
    """Replicate quantlib.research.run_experiment's label transform so the battery and the
    experimenter train on identical targets. IC/P&L are always measured vs the RAW return."""
    if label == "rank":
        return within_ts_rank(y, ts)
    if label == "vol_scaled":
        return [
            value / (abs(scale) if (scale == scale and abs(scale) > VOL_FLOOR) else VOL_FLOOR)
            for value, scale in zip(y, vol_scaler)
        ]
    return [float(v) for v in y]


def fit_predict(
    Xs: np.ndarray,
    label_source: np.ndarray,
    ts: list[datetime],
    train_idx: list[int],
    test_idx: list[int],
    label: str,
    vol_scaler: np.ndarray,
) -> np.ndarray:
    """Train one fold's model and predict the test block. `label_source` is the target the
    model fits (the real label for the IC pass, the shuffled label for the canary pass)."""
    if label == "lambdarank":
        order = sorted(train_idx, key=lambda i: ts[i])
        relevance = _int_relevance([label_source[i] for i in order], [ts[i] for i in order])
        dataset = lgb.Dataset(
            Xs[order],
            label=np.asarray(relevance, dtype=float),
            group=_group_counts([ts[i] for i in order]),
        )
        model = lgb.train(
            {**DEFAULT_LGB, "objective": "lambdarank"}, dataset, num_boost_round=NUM_ROUNDS
        )
        return model.predict(Xs[test_idx])
    fit_vals = transform_label(label_source, ts, label, vol_scaler)
    dataset = lgb.Dataset(
        Xs[train_idx], label=np.asarray([fit_vals[i] for i in train_idx], dtype=float)
    )
    model = lgb.train(DEFAULT_LGB, dataset, num_boost_round=NUM_ROUNDS)
    return model.predict(Xs[test_idx])


def collect_oos(
    Xs: np.ndarray,
    label_source: np.ndarray,
    y: np.ndarray,
    ts: list[datetime],
    symbols: list[str],
    label: str,
    vol_scaler: np.ndarray,
    horizon_minutes: int,
) -> tuple[list[float], list[float], list[datetime], list[str]]:
    """Walk-forward out-of-sample predictions. Returns (preds, realized_raw_return, ts, symbol).
    Realized return is always the RAW forward return `y` (never the transformed target)."""
    folds = walk_forward_folds(ts, horizon_minutes, N_FOLDS)
    preds: list[float] = []
    realized: list[float] = []
    pred_ts: list[datetime] = []
    pred_sym: list[str] = []
    for fold in folds:
        if len(fold.train_idx) < 500 or len(fold.test_idx) < 50:
            continue
        out = fit_predict(Xs, label_source, ts, fold.train_idx, fold.test_idx, label, vol_scaler)
        for j, i in enumerate(fold.test_idx):
            preds.append(float(out[j]))
            realized.append(float(y[i]))
            pred_ts.append(ts[i])
            pred_sym.append(symbols[i])
    return preds, realized, pred_ts, pred_sym


def per_symbol_demean(preds: list[float], symbols: list[str]) -> list[float]:
    """Subtract each symbol's mean prediction — removes the persistent per-symbol component
    (the survivorship drift) so the L/S backtest measures within-symbol TIMING only."""
    by_symbol: dict[str, list[float]] = defaultdict(list)
    for pred, sym in zip(preds, symbols):
        by_symbol[sym].append(pred)
    sym_mean = {sym: float(np.mean(vals)) for sym, vals in by_symbol.items()}
    return [pred - sym_mean[sym] for pred, sym in zip(preds, symbols)]


def run_config(
    Xs: np.ndarray,
    y: np.ndarray,
    ts: list[datetime],
    symbols: list[str],
    vol_scaler: np.ndarray,
    label: str,
    horizon_minutes: int,
    cadence_min: int,
) -> dict[str, object]:
    """All four gates for one (horizon, label) config."""
    periods_per_year = 252.0 * (390.0 / cadence_min)
    lag = max(1, horizon_minutes // cadence_min)

    preds, realized, pred_ts, pred_sym = collect_oos(
        Xs, y, y, ts, symbols, label, vol_scaler, horizon_minutes
    )
    real_ic = per_timestamp_ic(preds, realized, pred_ts)

    shuffled = np.asarray(shuffle_within_groups(list(y), ts, SEED), dtype=float)
    canary_preds, canary_real, canary_ts, _ = collect_oos(
        Xs, shuffled, y, ts, symbols, label, vol_scaler, horizon_minutes
    )
    canary_ic = per_timestamp_ic(canary_preds, canary_real, canary_ts)

    backtest_raw = long_short_backtest(
        preds, realized, pred_ts, pred_sym, periods_per_year=periods_per_year
    )
    neutral_preds = per_symbol_demean(preds, pred_sym)
    backtest_neutral = long_short_backtest(
        neutral_preds, realized, pred_ts, pred_sym, periods_per_year=periods_per_year
    )

    return {
        "label": label,
        "n_rows": int(len(y)),
        "n_test_ts": len(real_ic),
        "mean_ic": round(mean_ic(real_ic), 5),
        "nw_t": round(newey_west_tstat(real_ic, lag), 3),
        "canary_ic": round(mean_ic(canary_ic), 5),
        "net_per_period": backtest_raw.get("net_per_period"),
        "sharpe_net": backtest_raw.get("sharpe_net"),
        "breakeven_cost_bps": backtest_raw.get("breakeven_cost_bps"),
        "mean_turnover": backtest_raw.get("mean_turnover"),
        "survivorship_neutral_net": backtest_neutral.get("net_per_period"),
        "survivorship_neutral_sharpe": backtest_neutral.get("sharpe_net"),
        "survivorship_neutral_breakeven_bps": backtest_neutral.get("breakeven_cost_bps"),
    }


def filter_smoke(
    ts: list[datetime],
    symbols: list[str],
    X: np.ndarray,
    y: np.ndarray,
    smoke_days: int,
) -> tuple[list[datetime], list[str], np.ndarray, np.ndarray]:
    """Keep only the most recent `smoke_days` distinct dates — a fast end-to-end smoke run."""
    keep_dates = sorted({t.date() for t in ts})[-smoke_days:]
    keep_set = set(keep_dates)
    idx = [i for i, t in enumerate(ts) if t.date() in keep_set]
    return (
        [ts[i] for i in idx],
        [symbols[i] for i in idx],
        X[idx],
        y[idx],
    )


def run_horizon(conn: psycopg.Connection, horizon: str) -> list[dict[str, object]]:
    horizon_minutes, cadence_min = HORIZON_CFG[horizon]
    names, ts, symbols, X, y = load_panel(conn, horizon, SET_VERSION)
    if SMOKE_DAYS is not None:
        ts, symbols, X, y = filter_smoke(ts, symbols, X, y, SMOKE_DAYS)

    n_days = len({t.date() for t in ts})
    print(f"\n=== {horizon} | set={SET_VERSION} | {len(y)} rows | {n_days} days ===", flush=True)
    if SMOKE_DAYS is None and len(y) < MIN_ROWS:
        sys.exit(
            f"PANEL TOO SMALL: {horizon} set={SET_VERSION} has {len(y)} rows (< MIN_ROWS={MIN_ROWS}). "
            "Refusing to record a verdict on an empty/mid-rebuild panel. "
            "Confirm the clean panel is built before running the battery."
        )
    if len(y) < 1000:
        sys.exit(f"PANEL TOO SMALL even for smoke: {horizon} has {len(y)} rows.")

    feature_idx = [i for i, name in enumerate(names) if name not in PRICE_ONLY_DROP]
    Xs = X[:, feature_idx]
    vol_scaler = X[:, names.index("vol_30m")]
    used = [names[i] for i in feature_idx]
    print(f"price-only features ({len(used)}): {used}", flush=True)

    rows: list[dict[str, object]] = []
    for label in LABELS:
        result = run_config(Xs, y, ts, symbols, vol_scaler, label, horizon_minutes, cadence_min)
        result["horizon"] = horizon
        result["set_version"] = SET_VERSION
        rows.append(result)
        print(
            f"  {label:11} IC {result['mean_ic']:>9} t {result['nw_t']:>7} "
            f"canary {result['canary_ic']:>9} | net {str(result['net_per_period']):>11} "
            f"sharpe {str(result['sharpe_net']):>7} breakeven {str(result['breakeven_cost_bps']):>7}bps "
            f"turn {result['mean_turnover']} || SURVIVORSHIP-OUT sharpe "
            f"{str(result['survivorship_neutral_sharpe']):>7} "
            f"net {str(result['survivorship_neutral_net']):>11}",
            flush=True,
        )
    return rows


def main() -> None:
    if SET_VERSION in FORBIDDEN_VERSIONS:
        sys.exit(
            f"REFUSING SET_VERSION={SET_VERSION}: the labels table was overwritten by the clean v1.1.1 "
            "recompute, so battery-ing any pre-clean feature version against the fresh labels produces a "
            "meaningless chimera (dirty features ⨝ clean labels). The canonical pre-clean results live in "
            "experiments/results.jsonl. Use SET_VERSION=v1.1.1 for the clean run."
        )
    mode = f"SMOKE (last {SMOKE_DAYS}d)" if SMOKE_DAYS is not None else "FULL"
    print(f"PRICE-ONLY 4-GATE BATTERY | set={SET_VERSION} | mode={mode}", flush=True)
    all_rows: list[dict[str, object]] = []
    with psycopg.connect(**DB_KWARGS) as conn:
        for horizon in HORIZON_CFG:
            all_rows.extend(run_horizon(conn, horizon))

    run_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with open(OUT_JSONL, "a") as out:
        for row in all_rows:
            out.write(json.dumps({"run_at": run_at, "mode": mode, **row}) + "\n")

    print("\n=== VERDICT (price-only, cost-gated, survivorship-neutralized) ===", flush=True)
    print(
        "A config is EDGE only if: net>0 AND sharpe_net>0 AND |IC|>canary AND it SURVIVES "
        "per-symbol demean (survivorship_neutral_sharpe stays >0). Else: no tradeable edge.",
        flush=True,
    )
    for row in all_rows:
        net = row["net_per_period"]
        neutral = row["survivorship_neutral_sharpe"]
        edge = (
            isinstance(net, (int, float))
            and net > 0
            and isinstance(row["sharpe_net"], (int, float))
            and row["sharpe_net"] > 0
            and isinstance(neutral, (int, float))
            and neutral > 0
        )
        verdict = "EDGE?" if edge else "no edge"
        print(f"  {row['horizon']:9} {row['label']:11} -> {verdict}", flush=True)


if __name__ == "__main__":
    main()
