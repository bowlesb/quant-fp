"""Shape 005 — VOLUME-SHOCK OVERNIGHT REVERSAL overlay (explorer-shapes, conditional sparse overlay).

THE SHAPE: a volume shock today (vol_z_30 high) signals liquidity-driven over-extension that partly
reverses overnight. Trade the overnight book ONLY on shock name-days -> SPARSE participation, low
turnover. This reuses the EXISTING overnight label + the EXISTING vol_z_30 feature — the cheapest
shape to test (no new label, no new data).

HONEST PRE-REGISTERED PRIOR ~20% (LOW): the overnight label is SURVIVORSHIP-DEAD full-book across
everything tested (raw/rank/vol/lambdarank, pre/post ex-div). The ONE untested lever is whether
SPARSITY (trading only the shock subset) rescues it. Survivorship-demean is the make-or-break gate.

PRE-REGISTERED FALSIFIER: if the shock-cohort overnight net Sharpe is no better than the full-book
overnight Sharpe AND the survivorship-neutralized net Sharpe stays <= 0 (as it does full-book) — the
volume-shock overlay is dead and this CLOSES the overnight label as a shape (a valuable clean death).

Run as a module from /app:
  docker compose exec -T -w /app -e SET_VERSION=v1.1.1 experimenter python -m experiments.shape_volume_shock_overnight
  docker compose exec -T -w /app -e SET_VERSION=v1.1.1 -e SMOKE_DAYS=300 experimenter python -m experiments.shape_volume_shock_overnight
"""

import json
import math
import os
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone

import numpy as np
import psycopg

from quantlib.backtest import mean_ic, per_timestamp_ic, shuffle_within_groups
from quantlib.research import load_panel

from experiments.battery import collect_oos, filter_smoke, per_symbol_demean

SET_VERSION = os.environ.get("SET_VERSION", "v1.1.1")
SMOKE_DAYS = int(os.environ.get("SMOKE_DAYS", "0"))
RESULTS = os.environ.get(
    "VOLSHOCK_RESULTS", "/app/experiments/shape_volshock_results.jsonl"
)
FORBIDDEN_VERSIONS = {"v1.0.0", "v1.1.0"}
MIN_ROWS = 1000
SEED = 13
HORIZON = "overnight"
HORIZON_MINUTES = 390  # overnight purge = one RTH session
CADENCE_MIN = 390  # ~1 rebalance/day
SIGNAL_FEATURES = ["ret_5m", "vwap_dev", "range_pct", "gap_from_open"]
# Shock thresholds on |vol_z_30| (the existing volume-z feature). inf = full book (baseline).
SHOCK_THRESHOLDS = [0.0, 2.0, 3.0]
LEG_FRAC = 0.1
COST_SWEEP_BPS = [1.4, 2.0, 2.7]

DB_KWARGS = {
    "host": os.environ["DB_HOST"],
    "port": int(os.environ.get("DB_PORT", "5432")),
    "dbname": os.environ["DB_NAME"],
    "user": os.environ["DB_USER"],
    "password": os.environ["DB_PASSWORD"],
}


def shock_gated_backtest(
    preds: list[float],
    realized: list[float],
    group: list[datetime],
    symbol: list[str],
    vol_z: list[float],
    threshold: float,
    *,
    leg_frac: float,
    cost_bps_oneway: float,
    periods_per_year: float,
) -> dict[str, float]:
    """Overnight L/S on the SHOCK-gated subset: within each timestamp keep only names with
    |vol_z| >= threshold (threshold 0 = full book), then take top/bottom leg_frac. Reports net
    Sharpe, turnover, breakeven, and the realized participation rate."""
    buckets: dict[datetime, list[tuple[float, float, float, str]]] = defaultdict(list)
    for pred_value, realized_ret, ts, sym, vz in zip(
        preds, realized, group, symbol, vol_z
    ):
        if not (math.isnan(pred_value) or math.isnan(realized_ret) or math.isnan(vz)):
            buckets[ts].append((pred_value, realized_ret, vz, sym))
    cost = cost_bps_oneway / 1e4
    gross_list: list[float] = []
    net_list: list[float] = []
    turn_list: list[float] = []
    n_traded = 0
    n_eligible = 0
    prev_w: dict[str, float] = {}
    for ts in sorted(buckets):
        rows = buckets[ts]
        n_eligible += len(rows)
        gated = [row for row in rows if abs(row[2]) >= threshold]
        gated.sort(key=lambda row: row[0])
        k = max(1, int(leg_frac * len(gated)))
        if len(gated) < 2 * k:
            continue
        shorts, longs = gated[:k], gated[-k:]
        weights: dict[str, float] = {}
        for _, _, _, sym in longs:
            weights[sym] = weights.get(sym, 0.0) + 1.0 / len(longs)
        for _, _, _, sym in shorts:
            weights[sym] = weights.get(sym, 0.0) - 1.0 / len(shorts)
        book = longs + shorts
        n_traded += len(book)
        gross = sum(weights[sym] * realized_ret for _, realized_ret, _, sym in book)
        turnover = sum(
            abs(weights.get(sym, 0.0) - prev_w.get(sym, 0.0))
            for sym in set(weights) | set(prev_w)
        )
        gross_list.append(gross)
        net_list.append(gross - cost * turnover)
        turn_list.append(turnover)
        prev_w = weights
    if len(net_list) < 2:
        return {"n_periods": len(net_list)}
    mean_gross = statistics.mean(gross_list)
    mean_net = statistics.mean(net_list)
    std_net = statistics.stdev(net_list)
    mean_turn = statistics.mean(turn_list)
    return {
        "n_periods": len(net_list),
        "participation_rate": (
            round(n_traded / n_eligible, 4) if n_eligible else math.nan
        ),
        "net_per_period": round(mean_net, 6),
        "sharpe_net": (
            round(mean_net / std_net * math.sqrt(periods_per_year), 3)
            if std_net > 0
            else math.nan
        ),
        "mean_turnover": round(mean_turn, 3),
        "breakeven_cost_bps": (
            round(mean_gross / mean_turn * 1e4, 2) if mean_turn > 0 else math.nan
        ),
    }


def main() -> None:
    if SET_VERSION in FORBIDDEN_VERSIONS:
        sys.exit(f"REFUSING SET_VERSION={SET_VERSION}: dirty labels. Use v1.1.1.")
    periods_per_year = 252.0  # ~1 overnight rebalance/day
    records: list[dict[str, object]] = []

    with psycopg.connect(**DB_KWARGS) as conn:
        names, ts, symbols, X, y = load_panel(conn, HORIZON, SET_VERSION)
    if SMOKE_DAYS:
        ts, symbols, X, y = filter_smoke(ts, symbols, X, y, SMOKE_DAYS)
    if len(y) < MIN_ROWS:
        sys.exit(f"PANEL TOO SMALL: overnight has {len(y)} rows (< {MIN_ROWS}).")
    feat_idx = [names.index(name) for name in SIGNAL_FEATURES]
    vol_scaler = X[:, names.index("vol_30m")]
    vol_z_full = X[:, names.index("vol_z_30")]
    Xs = X[:, feat_idx]

    preds, realized, pred_ts, pred_sym = collect_oos(
        Xs, y, y, ts, symbols, "rank", vol_scaler, HORIZON_MINUTES
    )
    gross_ic = per_timestamp_ic(preds, realized, pred_ts)
    # align vol_z to the OOS prediction rows (collect_oos preserves test-fold order; re-pull by index)
    # collect_oos returns rows in fold/test order; rebuild vol_z for those rows via a (ts,sym) map.
    vz_by_key = {(ts[i], symbols[i]): float(vol_z_full[i]) for i in range(len(symbols))}
    pred_vol_z = [vz_by_key.get((t, s), math.nan) for t, s in zip(pred_ts, pred_sym)]

    neutral_preds = per_symbol_demean(preds, pred_sym)
    shuffled = np.asarray(shuffle_within_groups(list(y), ts, SEED), dtype=float)
    c_preds, c_real, c_ts, c_sym = collect_oos(
        Xs, shuffled, y, ts, symbols, "rank", vol_scaler, HORIZON_MINUTES
    )
    c_vol_z = [vz_by_key.get((t, s), math.nan) for t, s in zip(c_ts, c_sym)]

    print(
        f"\n=== overnight | {len(set(symbols))} names | {len(y)} rows | "
        f"gross IC={mean_ic(gross_ic):+.5f} ===",
        flush=True,
    )

    for threshold in SHOCK_THRESHOLDS:
        base = shock_gated_backtest(
            preds,
            realized,
            pred_ts,
            pred_sym,
            pred_vol_z,
            threshold,
            leg_frac=LEG_FRAC,
            cost_bps_oneway=2.0,
            periods_per_year=periods_per_year,
        )
        if base.get("n_periods", 0) < 2:
            print(f"  shock>={threshold}: too few periods — SKIP", flush=True)
            continue
        neutral = shock_gated_backtest(
            neutral_preds,
            realized,
            pred_ts,
            pred_sym,
            pred_vol_z,
            threshold,
            leg_frac=LEG_FRAC,
            cost_bps_oneway=2.0,
            periods_per_year=periods_per_year,
        )
        canary = shock_gated_backtest(
            c_preds,
            c_real,
            c_ts,
            c_sym,
            c_vol_z,
            threshold,
            leg_frac=LEG_FRAC,
            cost_bps_oneway=2.0,
            periods_per_year=periods_per_year,
        )
        cost_sweep = {
            f"cost_{cost}": shock_gated_backtest(
                preds,
                realized,
                pred_ts,
                pred_sym,
                pred_vol_z,
                threshold,
                leg_frac=LEG_FRAC,
                cost_bps_oneway=cost,
                periods_per_year=periods_per_year,
            ).get("sharpe_net")
            for cost in COST_SWEEP_BPS
        }
        label = "full_book" if threshold == 0.0 else f"shock>={threshold}sigma"
        print(
            f"  {label:<16} part={base.get('participation_rate')} "
            f"turn={base.get('mean_turnover')} breakeven={base.get('breakeven_cost_bps')}bps "
            f"sharpe@2bps={base.get('sharpe_net')} surv-neutral={neutral.get('sharpe_net')} "
            f"canary={canary.get('sharpe_net')}",
            flush=True,
        )
        records.append(
            {
                "run_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "shock_threshold": threshold,
                "label": label,
                "participation_rate": base.get("participation_rate"),
                "mean_turnover": base.get("mean_turnover"),
                "breakeven_cost_bps": base.get("breakeven_cost_bps"),
                "sharpe_net_2bps": base.get("sharpe_net"),
                "surv_neutral_sharpe": neutral.get("sharpe_net"),
                "canary_sharpe": canary.get("sharpe_net"),
                "cost_sweep": cost_sweep,
                "set_version": SET_VERSION,
            }
        )

    with open(RESULTS, "a") as f:
        for result in records:
            f.write(json.dumps(result) + "\n")
    print(f"\nwrote {len(records)} records to {RESULTS}", flush=True)


if __name__ == "__main__":
    main()
