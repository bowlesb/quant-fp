"""Family C prototype — dividend-timing features (the dividend run-up / ex-date anomaly).

NEW DATA, deep history: joins the LIVE corporate_actions feed (7,133 cash_dividends across
612 symbols, 2023->2026) against the v1.1.1 panel. ZERO panel rebuild — a Tier-2 standalone
prototype following experiments/family_b_dispersion.py. The question: does a name's position in
its dividend cycle carry within-ts cross-sectional signal ORTHOGONAL to price/momentum?

Documented effect under test: the dividend run-up (prices drift up into ex-date as
dividend-capture buyers accumulate) and the post-ex drift/reversal (the mechanical ex-date drop
plus subsequent recovery). This is a CALENDAR-of-the-firm effect, structurally independent of
the intraday price features that the panel already exhausted (30m signal = ret_5m only).

Features (all strictly point-in-time at each panel ts, computed from ex_dates only):
- days_to_ex      : trading-ish day distance to the NEXT upcoming ex-date (capped at CAP=15;
                    CAP when none upcoming within the window). Captures the run-up window.
- days_since_ex   : day distance since the MOST RECENT past ex-date (capped at CAP). Captures
                    post-ex drift/reversal.
- in_runup_window : 1 if 0 < days_to_ex <= 5 (the tight pre-ex capture window), else 0.
- is_dividend_payer: 1 if the symbol has ANY dividend in the feed (a value/income tilt proxy).

POINT-IN-TIME / LOOKAHEAD GUARD: ex-dates are declared weeks in advance (record/payable dates
populated), so the NEXT ex-date is known at ts in the real world — BUT we don't store the
declaration date, so to avoid using a not-yet-announced ex-date we only count an upcoming
ex-date as "known" when it is within FORWARD_KNOWN_DAYS calendar days of ts (dividends are
essentially always declared by then). Past ex-dates are unconditionally known. This is
conservative: it can only WEAKEN a real run-up signal, never manufacture one.

Gates (identical to the battery): within-ts rank-IC + NW t, shuffle canary, net-of-cost L/S,
survivorship demean. If +Family-C lifts IC above the canary AND improves breakeven -> propose a
real feature group via Tier-1 PR. If it moves nothing -> documented honestly; dividend timing is
not a cross-sectional edge at our horizon. Honest either way.

Run as a MODULE from /app (so quantlib + experiments.battery resolve):
  docker compose exec -T -w /app -e SET_VERSION=v1.1.1 experimenter python -m experiments.family_c_dividend_timing
  # fast smoke (last N days):
  docker compose exec -T -w /app -e SET_VERSION=v1.1.1 -e SMOKE_DAYS=120 experimenter python -m experiments.family_c_dividend_timing
"""
import bisect
import json
import os
import sys
from collections import defaultdict
from datetime import date, datetime, timezone

import numpy as np
import psycopg

from quantlib.research import load_panel

from experiments.battery import filter_smoke, run_config

SET_VERSION = os.environ.get("SET_VERSION", "v1.1.1")
SMOKE_DAYS = int(os.environ.get("SMOKE_DAYS", "0"))
RESULTS = os.environ.get("FAMILY_C_RESULTS", "/app/experiments/family_c_results.jsonl")
FORBIDDEN_VERSIONS = {"v1.0.0", "v1.1.0"}
MIN_ROWS = 1000

CAP = 15                      # cap day-distance features (beyond the run-up/drift window)
RUNUP_WINDOW = 5              # tight pre-ex dividend-capture window (trading-ish days)
FORWARD_KNOWN_DAYS = 35       # only treat an upcoming ex-date as known if within this many cal days
FAMILY_C_NAMES = ["days_to_ex", "days_since_ex", "in_runup_window", "is_dividend_payer"]

DB_KWARGS = {
    "host": os.environ["DB_HOST"], "port": int(os.environ.get("DB_PORT", "5432")),
    "dbname": os.environ["DB_NAME"], "user": os.environ["DB_USER"],
    "password": os.environ["DB_PASSWORD"],
}


def load_ex_dates(conn: psycopg.Connection) -> dict[str, list[date]]:
    """symbol -> sorted list of cash-dividend ex-dates."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT symbol, ex_date FROM corporate_actions "
            "WHERE action_type='cash_dividends' ORDER BY symbol, ex_date"
        )
        rows = cur.fetchall()
    by_symbol: dict[str, list[date]] = defaultdict(list)
    for symbol, ex_date in rows:
        by_symbol[symbol].append(ex_date)
    return by_symbol


def dividend_features_for(symbol: str, as_of: date, ex_dates_by_symbol: dict[str, list[date]]) -> tuple[float, float, float, float]:
    """Point-in-time dividend-timing features for one (symbol, date)."""
    ex_dates = ex_dates_by_symbol.get(symbol)
    if not ex_dates:
        return float(CAP), float(CAP), 0.0, 0.0   # non-payer: capped distances, flags 0
    is_payer = 1.0
    pos = bisect.bisect_right(ex_dates, as_of)     # ex_dates[:pos] are <= as_of (past/today)

    # days since most recent PAST ex-date (unconditionally known)
    if pos > 0:
        days_since = (as_of - ex_dates[pos - 1]).days
        days_since_capped = float(min(days_since, CAP))
    else:
        days_since_capped = float(CAP)

    # days to NEXT upcoming ex-date, only if within the forward-known horizon (lookahead guard)
    if pos < len(ex_dates):
        next_ex = ex_dates[pos]
        days_to = (next_ex - as_of).days
        if 0 <= days_to <= FORWARD_KNOWN_DAYS:
            days_to_capped = float(min(days_to, CAP))
        else:
            days_to_capped = float(CAP)
    else:
        days_to_capped = float(CAP)

    in_runup = 1.0 if 0.0 < days_to_capped <= RUNUP_WINDOW else 0.0
    return days_to_capped, days_since_capped, in_runup, is_payer


def compute_family_c(symbols: list[str], ts: list[datetime], ex_dates_by_symbol: dict[str, list[date]]) -> np.ndarray:
    """(n_rows x 4) matrix [days_to_ex, days_since_ex, in_runup_window, is_dividend_payer].

    Cached per (symbol, date) since the panel has many intraday rows per name-day."""
    out = np.empty((len(symbols), len(FAMILY_C_NAMES)), dtype=float)
    cache: dict[tuple[str, date], tuple[float, float, float, float]] = {}
    for i, (symbol, timestamp) in enumerate(zip(symbols, ts)):
        key = (symbol, timestamp.date())
        feats = cache.get(key)
        if feats is None:
            feats = dividend_features_for(symbol, timestamp.date(), ex_dates_by_symbol)
            cache[key] = feats
        out[i] = feats
    return out


def price_only_idx(names: list[str]) -> list[int]:
    """The 19 price feats (drop the 2 calendar cols), matching the battery's price-only set."""
    drop = {"minute_of_day", "day_of_week"}
    return [i for i, name in enumerate(names) if name not in drop]


def run_variant(tag: str, Xv: np.ndarray, y: np.ndarray, ts: list[datetime], symbols: list[str],
                vol_scaler: np.ndarray, horizon: str, cadence_min: int) -> dict[str, object]:
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
        ex_dates_by_symbol = load_ex_dates(conn)
        n_payers = len(ex_dates_by_symbol)
        for horizon in ["fwd_30m", "overnight"]:
            names, ts, symbols, X, y = load_panel(conn, horizon, SET_VERSION)
            if SMOKE_DAYS:
                ts, symbols, X, y = filter_smoke(ts, symbols, X, y, SMOKE_DAYS)
            if len(y) < MIN_ROWS:
                sys.exit(f"PANEL TOO SMALL: {horizon} set={SET_VERSION} has {len(y)} rows (< {MIN_ROWS}).")
            cadence_min = 390 if horizon == "overnight" else 30
            vol_scaler = X[:, names.index("vol_30m")]
            base_idx = price_only_idx(names)
            family_c = compute_family_c(symbols, ts, ex_dates_by_symbol)
            panel_payers = len({symbols[i] for i in range(len(symbols)) if family_c[i, 3] == 1.0})
            runup_rows = float(np.mean(family_c[:, 2]))
            print(f"\n=== {horizon} | set={SET_VERSION} | {len(y)} rows | "
                  f"{n_payers} payers in feed | {panel_payers} payers in panel | "
                  f"runup-window rows {runup_rows:.4f} ===", flush=True)

            base = run_variant("baseline_price_only", X[:, base_idx], y, ts, symbols,
                               vol_scaler, horizon, cadence_min)
            aug_X = np.hstack([X[:, base_idx], family_c])
            aug = run_variant("plus_family_c", aug_X, y, ts, symbols, vol_scaler, horizon, cadence_min)
            fc_only = run_variant("family_c_only", family_c, y, ts, symbols, vol_scaler,
                                  horizon, cadence_min)

            for result in (base, aug, fc_only):
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
