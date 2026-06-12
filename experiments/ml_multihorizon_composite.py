"""explorer-ml 004 — multi-horizon composite target: blend std(fwd_30m)+std(fwd_60m) and trade
at the SLOWER cadence to capture the persistent component of the 30m signal.

WHY (proposal 004): we have 30m AND 60m results separately. They fail for OPPOSITE reasons —
30m has signal (IC 0.027) but too much turnover (breakeven ~1.4bps); 60m has tolerable turnover
(half the rebalance frequency) but thinner signal alone. A fixed 30m horizon may be off-resonance
with our cost structure. A TARGET that blends the two — train on within-ts-zscore(fwd_30m) +
within-ts-zscore(fwd_60m) (scale-fair so neither dominates) — lets the model find predictions
good for BOTH horizons, i.e. the part of the 30m signal that PERSISTS into 60m. That persistent
component is, by construction, the lower-turnover part of the 30m signal.

This differs from 002 (which smooths along the FORWARD PATH of one horizon): here we co-train on
two real, separately-validated horizons and let the model arbitrate.

HEADLINE = composite-target breakeven at 60m cadence vs (a) the 30m-native ~1.4bps and (b) the
pure-60m-target breakeven. The win: a target economic at the slower cadence that still retains
30m-derived signal.

GATES (battery-identical, evaluated at BOTH cadences):
  1. Net-of-cost L/S at 30m AND 60m cadence (the composite's purpose is tradeability at the
     slower one; both reported so the turnover story is explicit).
  2. Shuffle-within-ts canary — the composite mixes two FUTURE returns (both legitimate label
     ingredients), but the canary on the (<=ts) FEATURES must still be ~0. The canary shuffles
     the RAW y30 within each ts (features-only arbiter); a lifted canary => a leaked label join.
  3. Label de-fragmentation: 30m/60m native cadences; the blend is computed only where BOTH
     labels exist (inner join on (symbol, ts)) — rows missing either horizon are DROPPED, never
     imputed, so we never fabricate a horizon.
  4. Survivorship neutralization: per-symbol-demean OOS preds, re-run the L/S at each cadence.

Run as a MODULE from /app:
  docker compose exec -T -w /app -e SET_VERSION=v1.1.1 experimenter python -m experiments.ml_multihorizon_composite
  # fast smoke:
  docker compose exec -T -w /app -e SET_VERSION=v1.1.1 -e SMOKE_DAYS=120 experimenter python -m experiments.ml_multihorizon_composite
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

from experiments.battery import PRICE_ONLY_DROP, collect_oos, per_symbol_demean

SET_VERSION = os.environ.get("SET_VERSION", "v1.1.1")
SMOKE_DAYS = int(os.environ["SMOKE_DAYS"]) if os.environ.get("SMOKE_DAYS") else None
RESULTS = os.environ.get(
    "COMPOSITE_RESULTS", "/app/experiments/ml_composite_results.jsonl"
)
FORBIDDEN_VERSIONS = {"v1.0.0", "v1.1.0"}
MIN_ROWS = 1000

SEED = 13
N_FOLDS = 5
# Purge with the LONGER horizon (60m) so no training label peeks at either test block.
PURGE_MINUTES = 60

DB_KWARGS = {
    "host": os.environ["DB_HOST"],
    "port": int(os.environ.get("DB_PORT", "5432")),
    "dbname": os.environ["DB_NAME"],
    "user": os.environ["DB_USER"],
    "password": os.environ["DB_PASSWORD"],
}


def within_ts_zscore(values: np.ndarray, ts: list[datetime]) -> np.ndarray:
    """Standardize each value within its timestamp's cross-section (mean 0, std 1). Makes the two
    horizons scale-fair before blending so neither dominates the composite by raw magnitude.
    """
    by_ts: dict[datetime, list[int]] = defaultdict(list)
    for i, timestamp in enumerate(ts):
        by_ts[timestamp].append(i)
    out = np.zeros(len(values), dtype=float)
    for indices in by_ts.values():
        block = np.array([values[i] for i in indices], dtype=float)
        mean = float(np.nanmean(block))
        std = float(np.nanstd(block))
        if std < 1e-12:
            continue
        for i in indices:
            out[i] = (values[i] - mean) / std
    return out


def align_horizons(
    rows30: tuple, rows60: tuple
) -> tuple[np.ndarray, list[datetime], list[str], np.ndarray, np.ndarray]:
    """Inner-join the two horizon panels on (symbol, ts). Returns (X, ts, symbols, y30, y60) for
    the rows where BOTH labels exist. X is identical across horizons (same feature rows), so the
    join only aligns labels — never fabricates a horizon."""
    _, ts30, sym30, X30, y30 = rows30
    _, ts60, sym60, _, y60 = rows60
    key60 = {(sym60[i], ts60[i]): y60[i] for i in range(len(ts60))}
    keep, y30_keep, y60_keep = [], [], []
    for i in range(len(ts30)):
        match = key60.get((sym30[i], ts30[i]))
        if match is not None:
            keep.append(i)
            y30_keep.append(y30[i])
            y60_keep.append(match)
    X = X30[keep]
    ts = [ts30[i] for i in keep]
    symbols = [sym30[i] for i in keep]
    return (
        X,
        ts,
        symbols,
        np.array(y30_keep, dtype=float),
        np.array(y60_keep, dtype=float),
    )


def gate_block(
    preds: list[float],
    realized: list[float],
    pred_ts: list[datetime],
    pred_sym: list[str],
    cadence_min: int,
    tag: str,
) -> dict[str, object]:
    """Net-of-cost L/S + survivorship demean at one rebalance cadence, vs one realized horizon."""
    periods_per_year = 252.0 * (390.0 / cadence_min)
    backtest_raw = long_short_backtest(
        preds, realized, pred_ts, pred_sym, periods_per_year=periods_per_year
    )
    neutral = per_symbol_demean(preds, pred_sym)
    backtest_neutral = long_short_backtest(
        neutral, realized, pred_ts, pred_sym, periods_per_year=periods_per_year
    )
    return {
        f"breakeven_cost_bps_{tag}": backtest_raw.get("breakeven_cost_bps"),
        f"net_per_period_{tag}": backtest_raw.get("net_per_period"),
        f"sharpe_net_{tag}": backtest_raw.get("sharpe_net"),
        f"mean_turnover_{tag}": backtest_raw.get("mean_turnover"),
        f"survivorship_neutral_sharpe_{tag}": backtest_neutral.get("sharpe_net"),
    }


def run_target(
    Xs: np.ndarray,
    target: np.ndarray,
    y30: np.ndarray,
    y60: np.ndarray,
    ts: list[datetime],
    symbols: list[str],
    vol_scaler: np.ndarray,
    model_tag: str,
) -> dict[str, object]:
    """Train the GBM on `target`; grade IC vs BOTH raw horizons and run the L/S at BOTH cadences.
    The canary shuffles raw y30 within each ts (features-only leakage arbiter)."""
    lag30 = max(1, 30 // 30)
    lag60 = max(1, 60 // 60)

    preds, _, pred_ts, pred_sym = collect_oos(
        Xs, target, y30, ts, symbols, "raw", vol_scaler, PURGE_MINUTES
    )
    # Realized arrays aligned to the OOS prediction order: collect_oos returns y30 as `realized`,
    # but we also need y60 in the same order — rebuild both by index via a fold replay-free map.
    pred_keyed = list(zip(pred_ts, pred_sym))
    y30_map = {(ts[i], symbols[i]): float(y30[i]) for i in range(len(ts))}
    y60_map = {(ts[i], symbols[i]): float(y60[i]) for i in range(len(ts))}
    realized30 = [y30_map[key] for key in pred_keyed]
    realized60 = [y60_map[key] for key in pred_keyed]

    ic30 = per_timestamp_ic(preds, realized30, pred_ts)
    ic60 = per_timestamp_ic(preds, realized60, pred_ts)

    shuffled = np.asarray(shuffle_within_groups(list(y30), ts, SEED), dtype=float)
    canary_preds, canary_real, canary_ts, _ = collect_oos(
        Xs, shuffled, y30, ts, symbols, "raw", vol_scaler, PURGE_MINUTES
    )
    canary_ic = per_timestamp_ic(canary_preds, canary_real, canary_ts)

    result: dict[str, object] = {
        "model": model_tag,
        "n_rows": int(len(y30)),
        "n_features": int(Xs.shape[1]),
        "n_test_ts": len(ic30),
        "ic_vs_30m": round(mean_ic(ic30), 5),
        "nw_t_vs_30m": round(newey_west_tstat(ic30, lag30), 3),
        "ic_vs_60m": round(mean_ic(ic60), 5),
        "nw_t_vs_60m": round(newey_west_tstat(ic60, lag60), 3),
        "canary_ic": round(mean_ic(canary_ic), 5),
    }
    result.update(gate_block(preds, realized30, pred_ts, pred_sym, 30, "30mcad"))
    result.update(gate_block(preds, realized60, pred_ts, pred_sym, 60, "60mcad"))
    return result


def main() -> None:
    if SET_VERSION in FORBIDDEN_VERSIONS:
        sys.exit(
            f"REFUSING SET_VERSION={SET_VERSION}: dirty labels (overwritten by v1.1.1). Use v1.1.1."
        )
    mode = f"SMOKE (last {SMOKE_DAYS}d)" if SMOKE_DAYS is not None else "FULL"
    print(
        f"MULTI-HORIZON COMPOSITE TARGET | set={SET_VERSION} | mode={mode}", flush=True
    )

    with psycopg.connect(**DB_KWARGS) as conn:
        rows30 = load_panel(conn, "fwd_30m", SET_VERSION)
        rows60 = load_panel(conn, "fwd_60m", SET_VERSION)
    names = rows30[0]
    X, ts, symbols, y30, y60 = align_horizons(rows30, rows60)

    if SMOKE_DAYS is not None:
        keep_dates = sorted({t.date() for t in ts})[-SMOKE_DAYS:]
        keep_set = set(keep_dates)
        idx = [i for i, t in enumerate(ts) if t.date() in keep_set]
        X, ts, symbols = X[idx], [ts[i] for i in idx], [symbols[i] for i in idx]
        y30, y60 = y30[idx], y60[idx]
    if len(y30) < MIN_ROWS:
        sys.exit(
            f"PANEL TOO SMALL after 30m<->60m join: {len(y30)} rows (< {MIN_ROWS})."
        )

    feature_idx = [i for i, name in enumerate(names) if name not in PRICE_ONLY_DROP]
    Xs = X[:, feature_idx]
    vol_scaler = X[:, names.index("vol_30m")]
    n_days = len({t.date() for t in ts})
    print(
        f"=== joined 30m<->60m | {len(y30)} rows | {n_days} days | "
        f"price-only ({len(feature_idx)}) ===",
        flush=True,
    )

    composite = within_ts_zscore(y30, ts) + within_ts_zscore(y60, ts)
    records: list[dict[str, object]] = []
    run_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    # Composite target + two pure-horizon references on the SAME joined panel (fair comparison).
    for target, model_tag in [
        (composite, "composite_z30_plus_z60"),
        (y30.copy(), "pure_30m"),
        (y60.copy(), "pure_60m"),
    ]:
        result = run_target(Xs, target, y30, y60, ts, symbols, vol_scaler, model_tag)
        result["set_version"] = SET_VERSION
        result["run_at"] = run_at
        result["mode"] = mode
        records.append(result)
        print(
            f"  {model_tag:24} IC30 {result['ic_vs_30m']:>9} IC60 {result['ic_vs_60m']:>9} "
            f"canary {result['canary_ic']:>9} | breakeven 30cad "
            f"{str(result['breakeven_cost_bps_30mcad']):>7} 60cad "
            f"{str(result['breakeven_cost_bps_60mcad']):>7} | turn60 "
            f"{result['mean_turnover_60mcad']} SURV60 "
            f"{str(result['survivorship_neutral_sharpe_60mcad']):>7}",
            flush=True,
        )

    with open(RESULTS, "a") as out:
        for record in records:
            out.write(json.dumps(record) + "\n")
    print(f"\nwrote {len(records)} records -> {RESULTS}", flush=True)
    print(
        "HEADLINE: composite breakeven at 60m cadence vs (a) 30m-native ~1.4bps and (b) the "
        "pure_60m breakeven at 60m cadence. Win = composite retains 30m IC (>=0.018) AND its "
        "60m-cadence breakeven clears 1.4bps and beats pure_60m. Canary must stay ~0.",
        flush=True,
    )


if __name__ == "__main__":
    main()
