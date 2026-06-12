"""Ex-div corrected overnight battery (Family A) — load-time label hygiene, NO persistence.

Tests whether neutralizing the confirmed ex-dividend overnight-label artifact (a mechanical
~-52bps drop on nights whose forward open is the ex-morning, ~85% explained by the dividend
yield) changes the overnight verdict. The correction is applied IN-MEMORY at load time — the
v1.1.1 labels table is FROZEN and is NEVER written (Manager directive: labels have no version
column; overwriting them would destroy the M1 verdict's labels, the same trap that hit v1.1.0).

For each overnight label whose label_date+1 == ex_date (forward open = ex-morning), we ADD BACK
the dividend yield (cash_amount / prior_RTH_close) so the mechanical drop is removed and only the
genuine overnight move remains. Then we run the SAME 4 gates as the verdict battery on:
  RAW (canonical v1.1.1 labels)  vs  CORRECTED (ex-div-neutralized labels),
so the delta is exactly the effect of removing the dividend artifact. If CORRECTED cleans or
kills the residual overnight signal, that's the finding. INTERPRETATION is HELD until qa-2's
independent verification of the underlying diagnostic.

Run inside the experimenter container as a module from /app:
  docker compose exec -T -w /app -e SET_VERSION=v1.1.1 experimenter python -m experiments.exdiv_corrected_battery
  # smoke:
  docker compose exec -T -w /app -e SET_VERSION=v1.1.1 -e SMOKE_DAYS=120 experimenter python -m experiments.exdiv_corrected_battery
"""
import json
import os
import sys
from datetime import date, datetime, timezone

import numpy as np
import psycopg

from quantlib.research import load_panel

from experiments.battery import filter_smoke, run_config

SET_VERSION = os.environ.get("SET_VERSION", "v1.1.1")
SMOKE_DAYS = int(os.environ["SMOKE_DAYS"]) if os.environ.get("SMOKE_DAYS") else None
RESULTS = os.environ.get("EXDIV_RESULTS", "/app/experiments/exdiv_corrected_results.jsonl")
FORBIDDEN_VERSIONS = {"v1.0.0", "v1.1.0"}
PRICE_ONLY_DROP = {"minute_of_day", "day_of_week"}
LABELS = ["raw", "rank", "vol_scaled", "lambdarank"]
MIN_ROWS = 1000

DB_KWARGS = {
    "host": os.environ["DB_HOST"], "port": int(os.environ.get("DB_PORT", "5432")),
    "dbname": os.environ["DB_NAME"], "user": os.environ["DB_USER"],
    "password": os.environ["DB_PASSWORD"],
}


def _month_starts(start: date, end: date) -> list[tuple[date, date]]:
    """[(month_start, next_month_start), ...] spanning [start, end) — one bounded window per
    statement so the bars_1m scan touches only ~1 month of chunks (lock-budget safe)."""
    windows: list[tuple[date, date]] = []
    cur = date(start.year, start.month, 1)
    while cur < end:
        nxt = date(cur.year + 1, 1, 1) if cur.month == 12 else date(cur.year, cur.month + 1, 1)
        windows.append((cur, nxt))
        cur = nxt
    return windows


def load_exdiv_yields(conn: psycopg.Connection) -> dict[tuple[str, object], float]:
    """Map (symbol, label_date) -> dividend yield for nights whose forward open is the ex-morning.

    Keyed by the LABEL's local date D (the night close(D)->open(D+1)); the artifact hits when
    ex_date == D+1. Yield = cash_amount / prior RTH close (15:59 ET backfill bar). READ-ONLY.
    """
    # bars_1m is a 693-chunk hypertable; a panel-wide scan with a time-of-day predicate locks
    # every chunk in range and, under parallel workers, exhausts max_locks_per_transaction (=64)
    # -> "out of shared memory". Fix: (a) disable parallel workers (they multiply per-chunk lock
    # usage), and (b) query MONTH-BY-MONTH so each statement touches only ~1 month of chunks,
    # well under the lock budget. The yield is cash_amount / prior RTH close (15:59 ET bar).
    yields: dict[tuple[str, object], float] = {}
    months = _month_starts(date(2024, 1, 1), date(2026, 6, 14))
    with conn.cursor() as cur:
        cur.execute("SET max_parallel_workers_per_gather = 0")
        for month_start, month_end in months:
            cur.execute(
                """
                WITH div AS (
                  SELECT symbol, ex_date, cash_amount
                  FROM corporate_actions_pit
                  WHERE action_type = 'cash_dividend'
                    AND (ex_date - 1) >= %(start)s AND (ex_date - 1) < %(end)s
                ),
                px AS (
                  SELECT symbol, (ts AT TIME ZONE 'America/New_York')::date AS d, close
                  FROM bars_1m
                  WHERE source = 'backfill'
                    AND ts >= %(start)s::timestamptz
                    AND ts <  %(end)s::timestamptz
                    AND (ts AT TIME ZONE 'America/New_York')::time = '15:59'
                )
                SELECT d.symbol, (d.ex_date - 1) AS label_date,
                       d.cash_amount / NULLIF(px.close, 0) AS yield
                FROM div d
                JOIN px ON px.symbol = d.symbol AND px.d = d.ex_date - 1
                WHERE px.close > 0 AND d.cash_amount IS NOT NULL
                """,
                {"start": month_start, "end": month_end},
            )
            for sym, label_date, yld in cur.fetchall():
                if yld is not None:
                    yields[(sym, label_date)] = float(yld)
    return yields


def apply_correction(y: np.ndarray, ts: list, symbols: list[str],
                     yields: dict[tuple[str, object], float]) -> tuple[np.ndarray, int]:
    """Return a COPY of y with the dividend yield added back on affected ex-div nights."""
    corrected = y.copy()
    n_applied = 0
    for i, (symbol, timestamp) in enumerate(zip(symbols, ts)):
        label_date = timestamp.astimezone(timestamp.tzinfo).date() if timestamp.tzinfo else timestamp.date()
        key = (symbol, label_date)
        if key in yields:
            corrected[i] += yields[key]                # add back the mechanical drop
            n_applied += 1
    return corrected, n_applied


def run_label_set(tag, Xs, y, ts, symbols, vol_scaler, horizon_minutes, cadence_min):
    rows = []
    for label in LABELS:
        result = run_config(Xs, y, ts, symbols, vol_scaler, label, horizon_minutes, cadence_min)
        result["label_basis"] = tag
        rows.append(result)
        print(f"  [{tag:9}] {label:11} IC {result['mean_ic']:>9} canary {result['canary_ic']:>9} "
              f"breakeven {str(result['breakeven_cost_bps']):>6}bps SURV-OUT sharpe "
              f"{str(result['survivorship_neutral_sharpe']):>7}", flush=True)
    return rows


def main() -> None:
    if SET_VERSION in FORBIDDEN_VERSIONS:
        sys.exit(f"REFUSING SET_VERSION={SET_VERSION}: dirty labels. Use v1.1.1.")
    horizon = "overnight"
    horizon_minutes, cadence_min = 1440, 390
    records: list[dict[str, object]] = []
    with psycopg.connect(**DB_KWARGS) as conn:
        names, ts, symbols, X, y = load_panel(conn, horizon, SET_VERSION)
        if SMOKE_DAYS is not None:
            ts, symbols, X, y = filter_smoke(ts, symbols, X, y, SMOKE_DAYS)
        if len(y) < MIN_ROWS:
            sys.exit(f"PANEL TOO SMALL: overnight set={SET_VERSION} has {len(y)} rows.")
        yields = load_exdiv_yields(conn)

    corrected_y, n_applied = apply_correction(y, ts, symbols, yields)
    print(f"\n=== overnight | set={SET_VERSION} | {len(y)} rows | "
          f"ex-div corrections applied to {n_applied} labels "
          f"({100.0 * n_applied / len(y):.3f}%) ===", flush=True)

    feature_idx = [i for i, name in enumerate(names) if name not in PRICE_ONLY_DROP]
    Xs = X[:, feature_idx]
    vol_scaler = X[:, names.index("vol_30m")]

    for tag, y_variant in [("raw", y), ("exdiv_fix", corrected_y)]:
        for result in run_label_set(tag, Xs, y_variant, ts, symbols, vol_scaler,
                                    horizon_minutes, cadence_min):
            result["set_version"] = SET_VERSION
            result["horizon"] = horizon
            result["n_corrections"] = n_applied
            result["run_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            records.append(result)

    with open(RESULTS, "a") as f:
        for result in records:
            f.write(json.dumps(result) + "\n")
    print(f"\nwrote {len(records)} records to {RESULTS}", flush=True)


if __name__ == "__main__":
    main()
