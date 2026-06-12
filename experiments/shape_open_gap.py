"""STRATEGY SHAPE 1 + 2 prototype — open-gap dynamics & opening-range, NEW labels (Modeller).

A DIFFERENT strategy shape from the cross-sectional 30m/overnight L/S we've only ever tested.
Builds two NEW labels IN-EXPERIMENT (sandbox, no persistence) from the existing backfill bars:
  - open_to_close: 09:30 open -> 16:00 close return, cross-sectionally demeaned (Shape 1).
  - ten_to_close:  10:00 -> 16:00 close return, cross-sectionally demeaned (Shape 2).
and tests whether the morning's STATE predicts the rest-of-day move:
  - gap = 09:30 open / prior-close - 1 (the overnight gap)
  - or  = first-30-min range position (where 10:00 sits in the 09:30-10:00 range) — Shape 2.

We reuse the existing v1.1.1 morning feature vectors (the cadence row nearest 10:00 ET carries
ret_*/vol_*/gap_from_open/momentum) as the feature set, and run the SAME 4 gates as the battery
(IC vs the new label + shuffle canary + net-of-cost L/S + survivorship demean). If a shape's IC
clears its canary AND breakeven, it's a candidate worth a real label (Tier-1 PR). Honest either
way — prototype, not edge. Lock limit raised to 2048 (prod), so the bar scan runs in one pass.

Run as a module from /app:
  docker compose exec -T -w /app -e SET_VERSION=v1.1.1 experimenter python -m experiments.shape_open_gap
"""
import json
import os
import sys
from collections import defaultdict
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import numpy as np
import psycopg

from quantlib.research import load_panel

from experiments.battery import run_config

SET_VERSION = os.environ.get("SET_VERSION", "v1.1.1")
RESULTS = os.environ.get("SHAPE_RESULTS", "/app/experiments/shape_open_gap_results.jsonl")
FORBIDDEN = {"v1.0.0", "v1.1.0"}
PRICE_ONLY_DROP = {"minute_of_day", "day_of_week"}

ET = ZoneInfo("America/New_York")

DB_KWARGS = {
    "host": os.environ["DB_HOST"], "port": int(os.environ.get("DB_PORT", "5432")),
    "dbname": os.environ["DB_NAME"], "user": os.environ["DB_USER"],
    "password": os.environ["DB_PASSWORD"],
}


def load_session_prices(conn: psycopg.Connection) -> dict[tuple[str, date], dict[str, float]]:
    """{(symbol, session_date): {open, open_10, high_30, low_30, close}} from backfill bars.

    open = 09:30, open_10 = 10:00, close = 15:59; high_30/low_30 over [09:30,10:00] (Shape 2's
    opening range). Lock limit is 2048 so the full-window scan runs in one statement."""
    out: dict[tuple[str, date], dict[str, float]] = defaultdict(dict)
    with conn.cursor() as cur:
        cur.execute("SET max_parallel_workers_per_gather = 0")
        cur.execute(
            """
            WITH b AS (
              SELECT symbol,
                     (ts AT TIME ZONE 'America/New_York')::date AS d,
                     (ts AT TIME ZONE 'America/New_York')::time AS t,
                     open, high, low, close
              FROM bars_1m
              WHERE source = 'backfill'
                AND ts >= '2024-01-01'::timestamptz AND ts < '2026-06-15'::timestamptz
                AND (ts AT TIME ZONE 'America/New_York')::time BETWEEN '09:30' AND '16:00'
            )
            SELECT symbol, d,
              max(open)  FILTER (WHERE t = '09:30') AS open_930,
              max(close) FILTER (WHERE t = '10:00') AS close_1000,
              max(close) FILTER (WHERE t = '15:59') AS close_1559,
              max(high)  FILTER (WHERE t BETWEEN '09:30' AND '10:00') AS high_30,
              min(low)   FILTER (WHERE t BETWEEN '09:30' AND '10:00') AS low_30
            FROM b GROUP BY symbol, d
            """
        )
        for symbol, d, open_930, close_1000, close_1559, high_30, low_30 in cur.fetchall():
            if open_930 and close_1559:
                out[(symbol, d)] = {
                    "open": float(open_930),
                    "open_10": float(close_1000) if close_1000 else float("nan"),
                    "close": float(close_1559),
                    "high_30": float(high_30) if high_30 else float("nan"),
                    "low_30": float(low_30) if low_30 else float("nan"),
                }
    return out


def cross_sectional_demean(values: dict[tuple[str, date], float]) -> dict[tuple[str, date], float]:
    """Subtract the per-date cross-sectional median (matches the panel's excess-return labels)."""
    by_date: dict[date, list[float]] = defaultdict(list)
    for (sym, d), val in values.items():
        if np.isfinite(val):
            by_date[d].append(val)
    medians = {d: float(np.median(vals)) for d, vals in by_date.items() if vals}
    return {key: val - medians[key[1]] for key, val in values.items()
            if np.isfinite(val) and key[1] in medians}


def build_labels(prices: dict[tuple[str, date], dict[str, float]]) -> dict[str, dict]:
    """open_to_close + ten_to_close, each cross-sectionally demeaned."""
    otc_raw = {key: (px["close"] / px["open"] - 1.0)
               for key, px in prices.items() if px["open"]}
    ttc_raw = {key: (px["close"] / px["open_10"] - 1.0)
               for key, px in prices.items() if np.isfinite(px["open_10"]) and px["open_10"]}
    return {"open_to_close": cross_sectional_demean(otc_raw),
            "ten_to_close": cross_sectional_demean(ttc_raw)}


def main() -> None:
    if SET_VERSION in FORBIDDEN:
        sys.exit(f"REFUSING SET_VERSION={SET_VERSION}: dirty labels. Use v1.1.1.")
    with psycopg.connect(**DB_KWARGS) as conn:
        # Reuse the existing morning feature cadence as the feature set: load the 30m panel
        # (carries ret_*/vol_*/gap_from_open/momentum per ts), keep the ~10:00 ET cadence row.
        names, ts, symbols, X, _ = load_panel(conn, "fwd_30m", SET_VERSION)
        prices = load_session_prices(conn)
    labels = build_labels(prices)

    feature_idx = [i for i, name in enumerate(names) if name not in PRICE_ONLY_DROP]
    vol_col = names.index("vol_30m")

    # Each label is anchored at one morning time; the FEATURE row must be the cadence row at
    # that anchor (one row per symbol/date) so features are known at the label's start — no
    # lookahead and no leakage from attaching a same-day afternoon row to a morning-anchored move.
    anchor_et = {"open_to_close": 9 * 60 + 30, "ten_to_close": 10 * 60}

    records: list[dict[str, object]] = []
    for label_name, label_map in labels.items():
        anchor_minutes = anchor_et[label_name]
        keep = []
        for i, (sym, timestamp) in enumerate(zip(symbols, ts)):
            local = timestamp.astimezone(ET)
            if local.hour * 60 + local.minute != anchor_minutes:
                continue
            if (sym, local.date()) in label_map:
                keep.append(i)
        if len(keep) < 1000:
            print(f"{label_name}: only {len(keep)} joined rows — skipping", flush=True)
            continue
        Xs = X[np.ix_(keep, feature_idx)]
        ks_ts = [ts[i] for i in keep]
        ks_sym = [symbols[i] for i in keep]
        y = np.array([label_map[(symbols[i], ts[i].astimezone(ET).date())] for i in keep], dtype=float)
        vol_scaler = X[keep][:, vol_col]
        print(f"\n=== {label_name} | {len(y)} rows | {len(set(ks_ts))} ts ===", flush=True)
        for lab in ["raw", "rank"]:
            result = run_config(Xs, y, ks_ts, ks_sym, vol_scaler, lab, 30, 30)
            result.update({"shape_label": label_name, "label": lab, "set_version": SET_VERSION,
                           "run_at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
            records.append(result)
            print(f"  [{lab:4}] IC {result['mean_ic']:>9} canary {result['canary_ic']:>9} "
                  f"breakeven {result['breakeven_cost_bps']}bps SURV-OUT "
                  f"{result['survivorship_neutral_sharpe']}", flush=True)

    with open(RESULTS, "a") as f:
        for result in records:
            f.write(json.dumps(result) + "\n")
    print(f"\nwrote {len(records)} records to {RESULTS}", flush=True)


if __name__ == "__main__":
    main()
