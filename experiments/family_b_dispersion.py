"""Family B prototype — dispersion / beta-to-universe / idiosyncratic-residual features.

ZERO new data collection: these are derived PURELY from the existing v1.1.1 panel's return
columns, so this is a sandbox prototype (Tier-2) that needs no panel rebuild. The question:
does the idiosyncratic (universe-beta-removed) component of a name's return carry within-ts
signal that raw momentum conflates with market beta — and is cross-sectional dispersion a
useful regime feature?

Construction (all point-in-time from a single cross-section at each ts):
- The panel carries each name's return term-structure [ret_5m, ret_15m, ret_30m, ret_60m].
- universe_vec(ts) = cross-sectional MEAN of that 4-vector across names in the ts.
- beta_i = cov(ret_vec_i, universe_vec) / var(universe_vec) over the 4 horizons — each name's
  sensitivity to the common (market) move, estimated within the snapshot.
- resid_i = ret_30m_i - beta_i * universe_ret_30m — the idiosyncratic 30m return.
- dispersion(ts) = cross-sectional std of ret_30m across names — a regime feature (constant
  within a ts, so it can only act through interactions, like calendar).
- resid_mom = resid built from the longer end (ret_60m) as a slower idiosyncratic signal.

We then run the SAME gates as the battery (IC vs raw return + shuffle canary + net-of-cost L/S
+ survivorship demean) on: (a) BASELINE = the 19 price-only feats; (b) +FAMILY_B. If the
Family-B augmentation lifts IC ABOVE the canary AND improves breakeven, it's worth a real
feature group (then proposed via a Tier-1 PR). If it moves nothing, that sharpens the
"data-starved, not model-starved" read. Honest either way — this is a prototype, not edge.

Run inside the experimenter container as a MODULE from /app (so quantlib + experiments.battery
both resolve; running the file path directly breaks the quantlib import):
  docker compose exec -T -w /app -e SET_VERSION=v1.1.1 experimenter python -m experiments.family_b_dispersion
  # fast smoke (last N days):
  docker compose exec -T -w /app -e SET_VERSION=v1.1.1 -e SMOKE_DAYS=120 experimenter python -m experiments.family_b_dispersion
"""
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone

import numpy as np
import psycopg

from quantlib.research import load_panel

from experiments.battery import filter_smoke, run_config  # reuse the exact gate harness

SET_VERSION = os.environ.get("SET_VERSION", "v1.1.1")
SMOKE_DAYS = int(os.environ.get("SMOKE_DAYS", "0"))
RESULTS = os.environ.get("FAMILY_B_RESULTS", "/app/experiments/family_b_results.jsonl")
FORBIDDEN_VERSIONS = {"v1.0.0", "v1.1.0"}
MIN_ROWS = 1000

RET_NAMES = ["ret_5m", "ret_15m", "ret_30m", "ret_60m"]
FAMILY_B_NAMES = ["univ_beta", "idio_resid_30m", "idio_resid_60m", "dispersion_30m"]

DB_KWARGS = {
    "host": os.environ["DB_HOST"], "port": int(os.environ.get("DB_PORT", "5432")),
    "dbname": os.environ["DB_NAME"], "user": os.environ["DB_USER"],
    "password": os.environ["DB_PASSWORD"],
}


def group_indices_by_ts(ts: list[datetime]) -> dict[datetime, list[int]]:
    groups: dict[datetime, list[int]] = defaultdict(list)
    for i, t in enumerate(ts):
        groups[t].append(i)
    return groups


def compute_family_b(names: list[str], ts: list[datetime], X: np.ndarray) -> np.ndarray:
    """Return an (n_rows x 4) matrix [univ_beta, idio_resid_30m, idio_resid_60m, dispersion_30m].

    Beta is each name's return-term-structure regressed on the cross-sectional mean term
    structure; the residual is the idiosyncratic return. Dispersion is the per-ts cross-section
    std of ret_30m (a regime feature). All strictly within-timestamp -> point-in-time honest.
    """
    ret_idx = [names.index(name) for name in RET_NAMES]
    i30 = names.index("ret_30m")
    i60 = names.index("ret_60m")
    out = np.full((X.shape[0], len(FAMILY_B_NAMES)), np.nan, dtype=float)

    for idxs in group_indices_by_ts(ts).values():
        rows = np.asarray(idxs)
        ret_vecs = X[np.ix_(rows, ret_idx)]                 # (n_names_ts x 4)
        universe_vec = np.nanmean(ret_vecs, axis=0)         # (4,) the common move
        uvar = float(np.nanvar(universe_vec))
        disp = float(np.nanstd(X[rows, i30]))               # regime: cross-sectional spread
        univ_ret_30 = float(universe_vec[RET_NAMES.index("ret_30m")])
        univ_ret_60 = float(universe_vec[RET_NAMES.index("ret_60m")])

        for local, row in enumerate(rows):
            ret_vec = ret_vecs[local]
            if uvar > 0 and np.all(np.isfinite(ret_vec)):
                beta = float(np.cov(ret_vec, universe_vec, bias=True)[0, 1] / uvar)
            else:
                beta = np.nan
            resid30 = float(X[row, i30] - beta * univ_ret_30) if np.isfinite(beta) else np.nan
            resid60 = float(X[row, i60] - beta * univ_ret_60) if np.isfinite(beta) else np.nan
            out[row, 0] = beta
            out[row, 1] = resid30
            out[row, 2] = resid60
            out[row, 3] = disp
    return out


def price_only_idx(names: list[str]) -> list[int]:
    """The 19 price feats (drop the 2 calendar cols), matching the battery's price-only set."""
    drop = {"minute_of_day", "day_of_week"}
    return [i for i, name in enumerate(names) if name not in drop]


def run_variant(tag, Xv, y, ts, symbols, vol_scaler, horizon, cadence_min):
    horizon_minutes = {"fwd_30m": 30, "fwd_60m": 60, "overnight": 1440}[horizon]
    result = run_config(Xv, y, ts, symbols, vol_scaler, "raw", horizon_minutes, cadence_min)
    result["variant"] = tag
    result["horizon"] = horizon
    result["n_features"] = int(Xv.shape[1])
    return result


def main() -> None:
    if SET_VERSION in FORBIDDEN_VERSIONS:
        sys.exit(f"REFUSING SET_VERSION={SET_VERSION}: dirty labels (overwritten by v1.1.1). Use v1.1.1.")
    records: list[dict[str, object]] = []
    with psycopg.connect(**DB_KWARGS) as conn:
        for horizon in ["fwd_30m", "overnight"]:
            names, ts, symbols, X, y = load_panel(conn, horizon, SET_VERSION)
            if SMOKE_DAYS:
                ts, symbols, X, y = filter_smoke(ts, symbols, X, y, SMOKE_DAYS)
            if len(y) < MIN_ROWS:
                sys.exit(f"PANEL TOO SMALL: {horizon} set={SET_VERSION} has {len(y)} rows (< {MIN_ROWS}).")
            cadence_min = 390 if horizon == "overnight" else 30
            vol_scaler = X[:, names.index("vol_30m")]
            base_idx = price_only_idx(names)
            family_b = compute_family_b(names, ts, X)
            print(f"\n=== {horizon} | set={SET_VERSION} | {len(y)} rows | "
                  f"Family-B NaN-rate {np.isnan(family_b).mean():.4f} ===", flush=True)

            base = run_variant("baseline_price_only", X[:, base_idx], y, ts, symbols,
                               vol_scaler, horizon, cadence_min)
            aug_X = np.hstack([X[:, base_idx], family_b])
            aug = run_variant("plus_family_b", aug_X, y, ts, symbols, vol_scaler, horizon, cadence_min)
            fb_only = run_variant("family_b_only", family_b, y, ts, symbols, vol_scaler,
                                  horizon, cadence_min)

            for result in (base, aug, fb_only):
                result["set_version"] = SET_VERSION
                result["run_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
                records.append(result)
                print(f"  {result['variant']:24s} IC={result['mean_ic']:+.5f} "
                      f"canary={result['canary_ic']:+.5f} breakeven={result['breakeven_cost_bps']}bps "
                      f"surv_sharpe={result['survivorship_neutral_sharpe']}", flush=True)

    with open(RESULTS, "a") as f:
        for result in records:
            f.write(json.dumps(result) + "\n")
    print(f"\nwrote {len(records)} records to {RESULTS}", flush=True)


if __name__ == "__main__":
    main()
