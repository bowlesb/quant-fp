"""explorer-ml 002 — turnover-aware target: smooth the label to predict the PERSISTENT
component of the next-30m return, not the freshest tick.

WHY (proposal 002): the grind's airtight finding is that the 30m signal IS ret_5m -> you're
chasing the freshest tick -> maximal turnover -> breakeven ~1.4bps < ~2bps cost. The signal is
REAL (clean canary, NW t~20) but UNECONOMIC for a STRUCTURAL reason: the raw target rewards
predicting fast-decaying micro-noise that flips sign every period, forcing the L/S book to
re-trade everything. Every standard label (raw/rank/vol_scaled/lambdarank) trains on the SAME raw
fwd_30m return, so all inherit its turnover.

THE LEVER nobody has pulled: change WHAT the model predicts so its predictions are PERSISTENT
across adjacent timestamps. Train on a SMOOTHED target — the EWMA of the forward return over the
next K in-day cadence steps — so the model learns the part of the signal that survives more than
one rebalance. Lower turnover at the cost of some raw IC. The economic question is whether the
breakeven RISES even if IC falls.

HEADLINE = smoothed-target breakeven_cost_bps vs the raw-target ~1.4bps. Pre-committed grid
K in {2,3,5} x half_life in {1,2} (4 configs reported — k=2 only uses hl=1; see GRID below), no
cherry-pick.

KEY LEAKAGE DISCIPLINE: the smoothed TARGET pulls in returns from ts+30m/ts+60m for the SAME
symbol. That is LEGITIMATE for a target (a label may be any function of FUTURE returns), but the
shuffle canary must still score ~0, proving the FEATURES (all strictly <= ts) carry no leakage.
The canary shuffles the RAW y within each timestamp (NOT the smoothed target), so it stays a
clean features-only arbiter. If the canary lifts off zero, the smoothing leaked features -> void.
The smoothing is computed per symbol along its OWN in-day cadence sequence, and only averages
WITHIN-DAY forward steps (a day boundary truncates the window) so an intraday target is never
contaminated by an overnight gap.

Run as a MODULE from /app:
  docker compose exec -T -w /app -e SET_VERSION=v1.1.1 experimenter python -m experiments.ml_turnover_smoothed_target
  # fast smoke:
  docker compose exec -T -w /app -e SET_VERSION=v1.1.1 -e SMOKE_DAYS=120 experimenter python -m experiments.ml_turnover_smoothed_target
"""

import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone

import numpy as np
import psycopg

from quantlib.backtest import (
    long_short_backtest,
    mean_ic,
    newey_west_tstat,
    per_timestamp_ic,
    shuffle_within_groups,
)
from quantlib.research import load_panel

from experiments.battery import (
    PRICE_ONLY_DROP,
    collect_oos,
    filter_smoke,
    per_symbol_demean,
)

SET_VERSION = os.environ.get("SET_VERSION", "v1.1.1")
SMOKE_DAYS = int(os.environ["SMOKE_DAYS"]) if os.environ.get("SMOKE_DAYS") else None
RESULTS = os.environ.get(
    "SMOOTHED_RESULTS", "/app/experiments/ml_smoothed_results.jsonl"
)
FORBIDDEN_VERSIONS = {"v1.0.0", "v1.1.0"}
MIN_ROWS = 1000

SEED = 13
N_FOLDS = 5
HORIZON = "fwd_30m"
HORIZON_MINUTES = 30
CADENCE_MIN = 30
# Pre-committed (K forward steps, half_life) grid — the IC<->turnover frontier. Reported in full.
GRID = [(2, 1), (3, 1), (3, 2), (5, 2)]

DB_KWARGS = {
    "host": os.environ["DB_HOST"],
    "port": int(os.environ.get("DB_PORT", "5432")),
    "dbname": os.environ["DB_NAME"],
    "user": os.environ["DB_USER"],
    "password": os.environ["DB_PASSWORD"],
}


def ewma_forward_target(
    y: np.ndarray,
    ts: list[datetime],
    symbols: list[str],
    k_steps: int,
    half_life: float,
) -> np.ndarray:
    """Per-symbol forward EWMA of the raw fwd_30m label over the next `k_steps` IN-DAY cadence
    rows (including the current row). Weights decay with the given half_life; the window is
    truncated at the day boundary so an intraday target never averages across an overnight gap.

    Each row's target = sum_j w_j * y[row_j] / sum_j w_j, for j over this row and up to the next
    (k_steps-1) same-symbol, same-DATE rows in ascending ts order. Pure TARGET transform — the
    realized return graded in the gates is always the RAW y."""
    decay = 0.5 ** (1.0 / half_life)
    weights = np.array([decay**j for j in range(k_steps)], dtype=float)

    by_symbol_date: dict[tuple[str, object], list[int]] = defaultdict(list)
    for i, (sym, timestamp) in enumerate(zip(symbols, ts)):
        by_symbol_date[(sym, timestamp.date())].append(i)

    smoothed = np.array(y, dtype=float)
    for indices in by_symbol_date.values():
        ordered = sorted(indices, key=lambda i: ts[i])
        vals = [y[i] for i in ordered]
        for pos, i in enumerate(ordered):
            window = vals[pos : pos + k_steps]
            window_w = weights[: len(window)]
            finite = [
                (value, weight)
                for value, weight in zip(window, window_w)
                if not np.isnan(value)
            ]
            if finite:
                total_w = sum(weight for _, weight in finite)
                smoothed[i] = sum(value * weight for value, weight in finite) / total_w
    return smoothed


def run_smoothed_config(
    Xs: np.ndarray,
    y: np.ndarray,
    ts: list[datetime],
    symbols: list[str],
    vol_scaler: np.ndarray,
    k_steps: int,
    half_life: float,
) -> dict[str, object]:
    """All four gates for one smoothed-target config. The model TRAINS on the smoothed target;
    IC, L/S, and survivorship are all graded vs the RAW forward return y. The canary shuffles RAW
    y (features-only leakage arbiter), NOT the smoothed target."""
    periods_per_year = 252.0 * (390.0 / CADENCE_MIN)
    lag = max(1, HORIZON_MINUTES // CADENCE_MIN)

    target = ewma_forward_target(y, ts, symbols, k_steps, half_life)

    # label="raw" => transform_label is identity, so the model fits `target` directly while the
    # realized series stays y. This reuses the battery's GBM fold loop byte-for-byte.
    preds, realized, pred_ts, pred_sym = collect_oos(
        Xs, target, y, ts, symbols, "raw", vol_scaler, HORIZON_MINUTES
    )
    real_ic = per_timestamp_ic(preds, realized, pred_ts)

    shuffled = np.asarray(shuffle_within_groups(list(y), ts, SEED), dtype=float)
    canary_preds, canary_real, canary_ts, _ = collect_oos(
        Xs, shuffled, y, ts, symbols, "raw", vol_scaler, HORIZON_MINUTES
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
        "model": "gbm_smoothed_target",
        "k_steps": k_steps,
        "half_life": half_life,
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
    }


def main() -> None:
    if SET_VERSION in FORBIDDEN_VERSIONS:
        sys.exit(
            f"REFUSING SET_VERSION={SET_VERSION}: dirty labels (overwritten by v1.1.1). Use v1.1.1."
        )
    mode = f"SMOKE (last {SMOKE_DAYS}d)" if SMOKE_DAYS is not None else "FULL"
    print(
        f"TURNOVER-SMOOTHED TARGET | set={SET_VERSION} | mode={mode} | horizon={HORIZON}",
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
    vol_scaler = X[:, names.index("vol_30m")]
    used = [names[i] for i in feature_idx]
    n_days = len({t.date() for t in ts})
    print(
        f"=== {HORIZON} | {len(y)} rows | {n_days} days | price-only ({len(used)}) ===",
        flush=True,
    )

    # Raw-target baseline for the IC/turnover/breakeven comparison (k=1 == no smoothing).
    records: list[dict[str, object]] = []
    run_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    baseline = run_smoothed_config(Xs, y, ts, symbols, vol_scaler, 1, 1.0)
    baseline["tag"] = "raw_baseline_k1"
    records.append(baseline)
    print(
        f"  raw(k=1)      IC {baseline['mean_ic']:>9} t {baseline['nw_t']:>7} "
        f"canary {baseline['canary_ic']:>9} | breakeven {str(baseline['breakeven_cost_bps']):>7}bps "
        f"turn {baseline['mean_turnover']} || SURV-OUT sharpe "
        f"{str(baseline['survivorship_neutral_sharpe']):>7}",
        flush=True,
    )

    for k_steps, half_life in GRID:
        result = run_smoothed_config(Xs, y, ts, symbols, vol_scaler, k_steps, half_life)
        result["tag"] = f"smoothed_k{k_steps}_hl{half_life}"
        records.append(result)
        print(
            f"  k={k_steps} hl={half_life}      IC {result['mean_ic']:>9} t {result['nw_t']:>7} "
            f"canary {result['canary_ic']:>9} | breakeven {str(result['breakeven_cost_bps']):>7}bps "
            f"turn {result['mean_turnover']} || SURV-OUT sharpe "
            f"{str(result['survivorship_neutral_sharpe']):>7}",
            flush=True,
        )

    for record in records:
        record["horizon"] = HORIZON
        record["set_version"] = SET_VERSION
        record["run_at"] = run_at
        record["mode"] = mode
    with open(RESULTS, "a") as out:
        for record in records:
            out.write(json.dumps(record) + "\n")
    print(f"\nwrote {len(records)} records -> {RESULTS}", flush=True)
    print(
        "HEADLINE: does ANY smoothed config lift breakeven_cost_bps above the raw-target line "
        "(~1.4bps full-depth) by LOWERING turnover faster than gross return falls? Report the "
        "full IC<->turnover frontier; the canary must stay ~0 (features-only leakage arbiter).",
        flush=True,
    )


if __name__ == "__main__":
    main()
