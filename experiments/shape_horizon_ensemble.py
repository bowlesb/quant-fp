"""STRATEGY SHAPE 7 — horizon ensemble: does the 30m signal carry OVERNIGHT information?

Panel-only (feature_vectors + labels) — NO bars_1m scan, so it does not contend with the
post-close backfill batch. Hypothesis: the intraday 30m cross-sectional signal is uneconomic to
TRADE at 30m (battery: breakeven < cost) but may GATE the overnight book — i.e. the 30m model's
prediction at the last intraday cadence (15:30 ET) may predict the OVERNIGHT return.

Test: train the price-only model on the 30m target, walk-forward; take the OOS predictions on the
15:30-ET rows (the overnight anchor); measure within-ts rank-IC of those predictions against the
OVERNIGHT label (not the 30m label) + the net-of-cost overnight L/S + survivorship demean. If the
30m-trained signal has overnight IC above its canary AND survives the gates, the ensemble (30m
signal gating overnight holds) has value — a composition of what we already have, no new data.

Run as a module from /app (panel-light, safe during the batch):
  docker compose exec -T -w /app -e SET_VERSION=v1.1.1 experimenter python -u -m experiments.shape_horizon_ensemble
"""
import json
import os
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import numpy as np
import psycopg

from quantlib.backtest import (
    long_short_backtest,
    mean_ic,
    newey_west_tstat,
    per_timestamp_ic,
    shuffle_within_groups,
)
from quantlib.research import load_panel, run_experiment

from experiments.battery import collect_oos, per_symbol_demean

SET_VERSION = os.environ.get("SET_VERSION", "v1.1.1")
RESULTS = os.environ.get("ENSEMBLE_RESULTS", "/app/experiments/shape_ensemble_results.jsonl")
FORBIDDEN = {"v1.0.0", "v1.1.0"}
PRICE_ONLY_DROP = {"minute_of_day", "day_of_week"}
ET = ZoneInfo("America/New_York")
ANCHOR_MIN = 15 * 60                              # 15:00 ET = the LAST intraday 30m cadence (the
#                                                   overnight label anchors at 15:30, which has no
#                                                   30m feature row — gate the hold on the 15:00 signal)

DB_KWARGS = {
    "host": os.environ["DB_HOST"], "port": int(os.environ.get("DB_PORT", "5432")),
    "dbname": os.environ["DB_NAME"], "user": os.environ["DB_USER"],
    "password": os.environ["DB_PASSWORD"],
}


def main() -> None:
    if SET_VERSION in FORBIDDEN:
        sys.exit(f"REFUSING SET_VERSION={SET_VERSION}: dirty labels. Use v1.1.1.")
    with psycopg.connect(**DB_KWARGS) as conn:
        # 30m panel: features + 30m label (the model's training target).
        names, ts_30, sym_30, X_30, y_30 = load_panel(conn, "fwd_30m", SET_VERSION)
        # overnight panel: the SAME features at 15:30 + the overnight label (the eval target).
        names_o, ts_o, sym_o, X_o, y_on = load_panel(conn, "overnight", SET_VERSION)

    feature_idx = [i for i, name in enumerate(names) if name not in PRICE_ONLY_DROP]
    vol_col = names.index("vol_30m")

    # --- Baseline reference: the 30m signal's OWN 30m IC (sanity vs the battery ~0.027). ---
    base = run_experiment(
        X_30, y_30, ts_30, symbols=sym_30,
        vol_scaler=X_30[:, vol_col], label="raw",
        feature_idx=feature_idx, horizon_minutes=30, cadence_min=30,
    )
    print(f"[ref] 30m-trained, 30m-eval IC={base['mean_ic']} canary={base['canary_ic']}", flush=True)

    # --- Ensemble: 30m-TRAINED model, evaluated against the OVERNIGHT label on the 15:30 rows. ---
    # Map overnight (symbol, ts) -> overnight label, then attach to the 30m model's OOS prediction
    # The 30m intraday cadence ends at 15:00 ET (no 15:30 row); the overnight label is anchored at
    # 15:30 ET. So join the LAST intraday prediction (15:00) to the overnight label by (symbol, DATE),
    # and group IC by the overnight ANCHOR ts (one cross-section/day). This IS the ensemble: the last
    # intraday signal gates the overnight hold. Key overnight label + its anchor ts by (symbol, date).
    overnight_label = {(sym_o[i], ts_o[i].astimezone(ET).date()): y_on[i] for i in range(len(y_on))}
    overnight_ts = {(sym_o[i], ts_o[i].astimezone(ET).date()): ts_o[i] for i in range(len(y_on))}
    Xs = X_30[:, feature_idx]
    preds, _, pred_ts, pred_sym = collect_oos(
        Xs, y_30, y_30, ts_30, sym_30, "raw", X_30[:, vol_col], horizon_minutes=30
    )
    # keep OOS predictions on the LAST intraday cadence (15:00 ET) that have an overnight label
    ens_pred, ens_real, ens_ts, ens_sym = [], [], [], []
    for prediction, timestamp, symbol in zip(preds, pred_ts, pred_sym):
        local = timestamp.astimezone(ET)
        if local.hour * 60 + local.minute != ANCHOR_MIN:
            continue
        key = (symbol, local.date())
        if key in overnight_label:
            ens_pred.append(prediction)
            ens_real.append(overnight_label[key])
            ens_ts.append(overnight_ts[key])             # group IC by the overnight anchor ts
            ens_sym.append(symbol)

    if len(ens_pred) < 1000:
        sys.exit(f"too few ensemble rows joined: {len(ens_pred)} (need the {ANCHOR_MIN//60}:{ANCHOR_MIN%60:02d} ET cadence + overnight label)")

    ic = per_timestamp_ic(ens_pred, ens_real, ens_ts)
    shuffled_real = shuffle_within_groups(ens_real, ens_ts, 13)
    canary = per_timestamp_ic(ens_pred, shuffled_real, ens_ts)
    periods_per_year = 252.0
    bt = long_short_backtest(ens_pred, ens_real, ens_ts, ens_sym, periods_per_year=periods_per_year)
    neutral = long_short_backtest(per_symbol_demean(ens_pred, ens_sym), ens_real, ens_ts, ens_sym,
                                  periods_per_year=periods_per_year)

    result = {
        "shape": "horizon_ensemble_30m_gates_overnight",
        "n_rows": len(ens_pred),
        "n_ts": len(ic),
        "mean_ic": round(mean_ic(ic), 5),
        "nw_t": round(newey_west_tstat(ic, 1), 3),
        "canary_ic": round(mean_ic(canary), 5),
        "breakeven_cost_bps": bt.get("breakeven_cost_bps"),
        "sharpe_net": bt.get("sharpe_net"),
        "survivorship_neutral_sharpe": neutral.get("sharpe_net"),
        "set_version": SET_VERSION,
        "run_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    print(f"[ENSEMBLE] 30m-trained -> OVERNIGHT eval: IC={result['mean_ic']} "
          f"canary={result['canary_ic']} breakeven={result['breakeven_cost_bps']}bps "
          f"SURV-OUT sharpe={result['survivorship_neutral_sharpe']}", flush=True)

    with open(RESULTS, "a") as f:
        f.write(json.dumps(result) + "\n")
    print(f"wrote 1 record to {RESULTS}", flush=True)


if __name__ == "__main__":
    main()
