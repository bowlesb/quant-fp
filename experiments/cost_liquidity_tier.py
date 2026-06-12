"""Cost-by-liquidity-tier — is ret_5m+position ALREADY tradeable on the liquid tier? (task #5).

THE THESIS (Modeller, Manager-ratified top priority): every price signal we found is REAL but
dies on the ASSUMED ~2bps one-way cost (ret_5m+position 30m breakeven ~1.4bps). The lever is COST,
not signal. This measures whether the existing signal clears its MEASURED cost on the liquid tier.

PHASE 1 (this script, zero new data beyond the live quote feed):
1. Restrict the v1.1.1 price panel to the 50 OFI-capture names = the top-ADV liquid tier (minus
   the 2 ETFs SPY/QQQ that slipped into the capture and are NOT in the equity universe).
2. Run the ret_5m + position-group signal (the two families that DO carry within-ts signal) on
   that restricted cross-section, under the 4 battery gates.
3. Re-gate net-of-MEASURED-cost: sweep the flat one-way cost across the measured half-spread
   distribution of the tier (p25/median/p75 from quote_agg_1m at RTH cadence marks) instead of the
   2bps strawman, and read the breakeven vs the measured cost.

MEASURED COST CONTEXT (quote_agg_1m, 50 names, 3 days, RTH 10:00-15:30 ET cadence marks):
  per-observation half-spread p25=1.27 / p50=2.70 / p75=5.66 bps
  per-NAME median half-spread: 11/50 equities < 1.4bps; 19 < 2.0; 23 < 3.0; 29 < 4.0; 35 < 5.0
So the cost sweep below brackets the realistic per-name cost on this tier.

HONEST CAVEATS (pre-registered): (a) restricting to 50 names SHRINKS the cross-section -> the L/S
deciles are ~5 names/leg, much noisier than the full panel; IC and turnover both shift. (b) The
spread is measured on 3 days but applied to the 613-day price panel as a STATIC per-tier cost — a
first-order approximation (Phase 2 folds in exec's spread-keyed fill-prob for the dynamic cost).
(c) These 50 are the MOST liquid + most crowded names, where cross-sectional alpha is typically
WEAKEST — so a strong IC here would be surprising. This is DIRECTIONAL, not a verdict.

Run as a module from /app:
  docker compose exec -T -w /app -e SET_VERSION=v1.1.1 experimenter python -m experiments.cost_liquidity_tier
  docker compose exec -T -w /app -e SET_VERSION=v1.1.1 -e SMOKE_DAYS=200 experimenter python -m experiments.cost_liquidity_tier
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
)
from quantlib.research import load_panel

from experiments.battery import collect_oos, filter_smoke, per_symbol_demean

SET_VERSION = os.environ.get("SET_VERSION", "v1.1.1")
SMOKE_DAYS = int(os.environ.get("SMOKE_DAYS", "0"))
RESULTS = os.environ.get("COST_TIER_RESULTS", "/app/experiments/cost_liquidity_results.jsonl")
FORBIDDEN_VERSIONS = {"v1.0.0", "v1.1.0"}
MIN_ROWS = 1000
SEED = 13

# The 50 OFI-capture names = top-ADV liquid tier (SPY/QQQ excluded: ETFs, not equity universe).
LIQUID_TIER = [
    "AAOI", "AAPL", "AMAT", "AMD", "AMZN", "APP", "ARM", "ASML", "AVGO", "BE", "BRK.B", "CAT",
    "COHR", "CRM", "CRWV", "CSCO", "DELL", "GEV", "GLW", "GOOG", "GOOGL", "IBM", "INTC", "IREN",
    "JPM", "LITE", "LLY", "LRCX", "META", "MRVL", "MSFT", "MSTR", "MU", "NBIS", "NOW", "NVDA",
    "ORCL", "PLTR", "QCOM", "RKLB", "SNDK", "STX", "TSLA", "TSM", "TXN", "UNH", "V", "WDC", "WMT",
    "XOM",
]
SIGNAL_FEATURES = ["ret_5m", "vwap_dev", "range_pct", "gap_from_open"]   # the carrier set
# Measured half-spread sweep (bps one-way) bracketing the tier's per-name distribution.
COST_SWEEP_BPS = [1.0, 1.27, 1.4, 2.0, 2.7, 4.0]

DB_KWARGS = {
    "host": os.environ["DB_HOST"], "port": int(os.environ.get("DB_PORT", "5432")),
    "dbname": os.environ["DB_NAME"], "user": os.environ["DB_USER"],
    "password": os.environ["DB_PASSWORD"],
}


def restrict_to_tier(names, ts, symbols, X, y, tier):
    keep = set(tier)
    idx = [i for i, sym in enumerate(symbols) if sym in keep]
    return ([ts[i] for i in idx], [symbols[i] for i in idx], X[idx], y[idx])


def main() -> None:
    if SET_VERSION in FORBIDDEN_VERSIONS:
        sys.exit(f"REFUSING SET_VERSION={SET_VERSION}: dirty labels. Use v1.1.1.")
    horizon, horizon_minutes, cadence_min = "fwd_30m", 30, 30
    periods_per_year = 252.0 * (390.0 / cadence_min)
    records: list[dict[str, object]] = []

    with psycopg.connect(**DB_KWARGS) as conn:
        names, ts, symbols, X, y = load_panel(conn, horizon, SET_VERSION)
    if SMOKE_DAYS:
        ts, symbols, X, y = filter_smoke(ts, symbols, X, y, SMOKE_DAYS)
    feat_idx = [names.index(name) for name in SIGNAL_FEATURES]
    vol_scaler = X[:, names.index("vol_30m")]

    for tier_name, tier in [("liquid50", LIQUID_TIER), ("full_panel", None)]:
        if tier is None:
            t_ts, t_sym, t_X, t_y, t_vol = ts, symbols, X, y, vol_scaler
        else:
            keep = set(tier)
            idx = [i for i, sym in enumerate(symbols) if sym in keep]
            t_ts = [ts[i] for i in idx]
            t_sym = [symbols[i] for i in idx]
            t_X, t_y, t_vol = X[idx], y[idx], vol_scaler[idx]
        if len(t_y) < MIN_ROWS:
            print(f"SKIP {tier_name}: {len(t_y)} rows < {MIN_ROWS}", flush=True)
            continue
        Xs = t_X[:, feat_idx]
        names_in_tier = len(set(t_sym))
        avg_xsec = len(t_y) / len({t for t in t_ts})

        preds, realized, pred_ts, pred_sym = collect_oos(
            Xs, t_y, t_y, t_ts, t_sym, "rank", t_vol, horizon_minutes
        )
        real_ic = per_timestamp_ic(preds, realized, pred_ts)
        shuffled = np.asarray(shuffle_within_groups(list(t_y), t_ts, SEED), dtype=float)
        c_preds, c_real, c_ts, _ = collect_oos(
            Xs, shuffled, t_y, t_ts, t_sym, "rank", t_vol, horizon_minutes
        )
        canary_ic = per_timestamp_ic(c_preds, c_real, c_ts)
        neutral_preds = per_symbol_demean(preds, pred_sym)

        base_bt = long_short_backtest(preds, realized, pred_ts, pred_sym,
                                      cost_bps_oneway=2.0, periods_per_year=periods_per_year)
        lag = max(1, horizon_minutes // cadence_min)
        print(f"\n=== {tier_name} | {names_in_tier} names | {len(t_y)} rows | "
              f"~{avg_xsec:.1f} names/cross-section ===", flush=True)
        print(f"  IC={mean_ic(real_ic):+.5f} NWt={newey_west_tstat(real_ic, lag):.2f} "
              f"canary={mean_ic(canary_ic):+.5f} turnover={base_bt.get('mean_turnover')} "
              f"breakeven={base_bt.get('breakeven_cost_bps')}bps", flush=True)

        cost_results = {}
        for cost in COST_SWEEP_BPS:
            bt = long_short_backtest(preds, realized, pred_ts, pred_sym,
                                     cost_bps_oneway=cost, periods_per_year=periods_per_year)
            nbt = long_short_backtest(neutral_preds, realized, pred_ts, pred_sym,
                                      cost_bps_oneway=cost, periods_per_year=periods_per_year)
            cost_results[f"cost_{cost}"] = {
                "sharpe_net": bt.get("sharpe_net"),
                "net_per_period": bt.get("net_per_period"),
                "surv_neutral_sharpe": nbt.get("sharpe_net"),
            }
            print(f"    cost {cost:>4}bps -> sharpe_net {bt.get('sharpe_net')} "
                  f"net {bt.get('net_per_period')} | surv-neutral sharpe {nbt.get('sharpe_net')}",
                  flush=True)

        records.append({
            "run_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "tier": tier_name, "n_names": names_in_tier, "n_rows": int(len(t_y)),
            "avg_xsec": round(avg_xsec, 1), "features": SIGNAL_FEATURES,
            "mean_ic": round(mean_ic(real_ic), 5), "nw_t": round(newey_west_tstat(real_ic, lag), 3),
            "canary_ic": round(mean_ic(canary_ic), 5),
            "breakeven_cost_bps": base_bt.get("breakeven_cost_bps"),
            "mean_turnover": base_bt.get("mean_turnover"),
            "cost_sweep": cost_results, "set_version": SET_VERSION,
        })

    with open(RESULTS, "a") as f:
        for result in records:
            f.write(json.dumps(result) + "\n")
    print(f"\nwrote {len(records)} records to {RESULTS}", flush=True)


if __name__ == "__main__":
    main()
