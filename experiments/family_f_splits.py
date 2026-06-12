"""Family F prototype — split-event features from the LIVE corporate_actions feed.

Family C mined the 7,133 cash_dividends; the SAME feed carries 19 reverse_splits and 42
forward_splits that nobody touches. Splits are a structurally different corporate-calendar signal
with two opposite, well-documented mechanisms. ZERO new collection. Clones family_c_dividend_
timing.py exactly (PIT discipline + gates byte-identical via battery.run_config).

Mechanism:
- reverse splits = DISTRESS. A reverse split is overwhelmingly a name fighting a sub-$1 delisting
  threshold = a fundamentally weak, often delisting-bound name. These are disproportionately the
  LOSERS, i.e. the short-leg population. CRITICALLY this is a survivorship-RELEVANT signal: the
  delisted-name backfill is months away, but a reverse-split FLAG is a cheap point-in-time proxy
  for "this name is in the distressed tail NOW."
- forward splits = ATTENTION. A forward split lowers price to draw retail flow; documented small
  positive post-announcement drift. Low base rate (42), clean orthogonal mechanism.

Features (strictly point-in-time at each panel ts, from ex_dates only):
- days_since_reverse_split : day distance since most recent past reverse split (cap REV_CAP; CAP
                             when none) — the distress decay window.
- recent_reverse_split     : 1 if a reverse-split ex_date within REV_WINDOW trailing days.
- days_to_forward_split    : day distance to the NEXT upcoming forward split, only if within
                             FORWARD_KNOWN_DAYS (lookahead guard, like Family C); CAP otherwise.
- days_since_forward_split : day distance since most recent past forward split (cap FWD_CAP).

POINT-IN-TIME / LOOKAHEAD GUARD: split ex-dates are announced weeks ahead, so an upcoming split
within FORWARD_KNOWN_DAYS of ts is "known"; past splits unconditionally. EXACTLY Family C's
declaration-proxy logic. The split adjusts the PRICE series; the FEATURE here is event-distance/
flag, NOT price, so it is immune to the KLAC adjustment-basis bug.

INTERPRETATION (pre-registered): reverse-split names are distress/survivorship-correlated, so a
RAW lift that COLLAPSES under per-symbol demean is the EXPECTED honest outcome (the flag partly
proxies survivorship). Judge STRICTLY on the survivorship-neutralized number, NOT raw. If only the
raw lift exists and it demeans away, report it as "a short-leg RISK filter, not timing alpha"
(hand to execution-risk), not an edge. Forward-split features are expected to be too sparse to
matter (logged as such).

Run as a MODULE from /app:
  docker compose exec -T -w /app -e SET_VERSION=v1.1.1 experimenter python -m experiments.family_f_splits
  # fast smoke (last N days):
  docker compose exec -T -w /app -e SET_VERSION=v1.1.1 -e SMOKE_DAYS=120 experimenter python -m experiments.family_f_splits
"""

import bisect
import json
import os
import sys
from collections import defaultdict
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import numpy as np
import psycopg

from quantlib.research import load_panel

from experiments.battery import filter_smoke, run_config

SET_VERSION = os.environ.get("SET_VERSION", "v1.1.1")
SMOKE_DAYS = int(os.environ.get("SMOKE_DAYS", "0"))
SMOKE_SYMBOLS = int(
    os.environ.get("SMOKE_SYMBOLS", "0")
)  # >0: keep only the first N symbols (fast dev smoke)
RESULTS = os.environ.get("FAMILY_F_RESULTS", "/app/experiments/family_f_results.jsonl")
FORBIDDEN_VERSIONS = {"v1.0.0", "v1.1.0"}
MIN_ROWS = 1000

REV_CAP = 60  # cap distress-decay distance (calendar days)
REV_WINDOW = 60  # recent-reverse-split flag window (calendar days)
FWD_CAP = 30  # cap forward-split distances
FORWARD_KNOWN_DAYS = 35  # only treat an upcoming split as known if within this many cal days
LIQUID_HEAD_FRAC = 0.25
FAMILY_F_NAMES = [
    "days_since_reverse_split",
    "recent_reverse_split",
    "days_to_forward_split",
    "days_since_forward_split",
]

_NY = ZoneInfo("America/New_York")

DB_KWARGS = {
    "host": os.environ["DB_HOST"],
    "port": int(os.environ.get("DB_PORT", "5432")),
    "dbname": os.environ["DB_NAME"],
    "user": os.environ["DB_USER"],
    "password": os.environ["DB_PASSWORD"],
}


def load_split_dates(conn: psycopg.Connection, action_type: str) -> dict[str, list[date]]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT symbol, ex_date FROM corporate_actions WHERE action_type=%s ORDER BY symbol, ex_date",
            (action_type,),
        )
        rows = cur.fetchall()
    by_symbol: dict[str, list[date]] = defaultdict(list)
    for symbol, ex_date in rows:
        by_symbol[symbol].append(ex_date)
    return by_symbol


def split_features_for(
    symbol: str,
    as_of: date,
    reverse_by_symbol: dict[str, list[date]],
    forward_by_symbol: dict[str, list[date]],
) -> tuple[float, float, float, float]:
    reverse = reverse_by_symbol.get(symbol, [])
    forward = forward_by_symbol.get(symbol, [])

    rev_pos = bisect.bisect_right(reverse, as_of)
    if rev_pos > 0:
        days_since_rev = float(min((as_of - reverse[rev_pos - 1]).days, REV_CAP))
        recent_rev = 1.0 if (as_of - reverse[rev_pos - 1]).days <= REV_WINDOW else 0.0
    else:
        days_since_rev = float(REV_CAP)
        recent_rev = 0.0

    fwd_pos = bisect.bisect_right(forward, as_of)
    if fwd_pos < len(forward):
        days_to = (forward[fwd_pos] - as_of).days
        days_to_fwd = (
            float(min(days_to, FWD_CAP)) if 0 <= days_to <= FORWARD_KNOWN_DAYS else float(FWD_CAP)
        )
    else:
        days_to_fwd = float(FWD_CAP)
    if fwd_pos > 0:
        days_since_fwd = float(min((as_of - forward[fwd_pos - 1]).days, FWD_CAP))
    else:
        days_since_fwd = float(FWD_CAP)

    return days_since_rev, recent_rev, days_to_fwd, days_since_fwd


def compute_family_f(
    symbols: list[str],
    ts: list[datetime],
    reverse_by_symbol: dict[str, list[date]],
    forward_by_symbol: dict[str, list[date]],
) -> np.ndarray:
    out = np.empty((len(symbols), len(FAMILY_F_NAMES)), dtype=float)
    cache: dict[tuple[str, date], tuple[float, float, float, float]] = {}
    for i, (symbol, timestamp) in enumerate(zip(symbols, ts)):
        key = (symbol, timestamp.astimezone(_NY).date())
        feats = cache.get(key)
        if feats is None:
            feats = split_features_for(symbol, key[1], reverse_by_symbol, forward_by_symbol)
            cache[key] = feats
        out[i] = feats
    return out


def liquid_head_mask(conn: psycopg.Connection, symbols: list[str]) -> np.ndarray:
    uniq = sorted(set(symbols))
    with conn.cursor() as cur:
        cur.execute(
            """SELECT symbol, percentile_cont(0.5) WITHIN GROUP (ORDER BY dollar) AS med
               FROM (SELECT symbol, ts::date d, sum(volume*COALESCE(vwap, close)) dollar
                     FROM bars_1m WHERE source='backfill' AND symbol = ANY(%s)
                     GROUP BY symbol, ts::date) s
               GROUP BY symbol""",
            (uniq,),
        )
        med = {sym: float(val) for sym, val in cur.fetchall()}
    ranked = sorted(med.items(), key=lambda kv: kv[1], reverse=True)
    n_head = max(1, int(len(ranked) * LIQUID_HEAD_FRAC))
    head_syms = {sym for sym, _ in ranked[:n_head]}
    return np.asarray([sym in head_syms for sym in symbols], dtype=bool)


def price_only_idx(names: list[str]) -> list[int]:
    drop = {"minute_of_day", "day_of_week"}
    return [i for i, name in enumerate(names) if name not in drop]


def run_variant(
    tag: str,
    Xv: np.ndarray,
    y: np.ndarray,
    ts: list[datetime],
    symbols: list[str],
    vol_scaler: np.ndarray,
    horizon: str,
    cadence_min: int,
) -> dict[str, object]:
    horizon_minutes = {"fwd_30m": 30, "fwd_60m": 60, "overnight": 1440}[horizon]
    result = run_config(Xv, y, ts, symbols, vol_scaler, "raw", horizon_minutes, cadence_min)
    result["variant"] = tag
    result["horizon"] = horizon
    result["n_features"] = int(Xv.shape[1])
    return result


def subset(mask: np.ndarray, *arrays):
    out = []
    for arr in arrays:
        if isinstance(arr, np.ndarray):
            out.append(arr[mask])
        else:
            out.append([a for a, keep in zip(arr, mask) if keep])
    return out


def main() -> None:
    if SET_VERSION in FORBIDDEN_VERSIONS:
        sys.exit(
            f"REFUSING SET_VERSION={SET_VERSION}: dirty labels (overwritten by v1.1.1). Use v1.1.1."
        )
    records: list[dict[str, object]] = []
    with psycopg.connect(**DB_KWARGS) as conn:
        reverse_by_symbol = load_split_dates(conn, "reverse_splits")
        forward_by_symbol = load_split_dates(conn, "forward_splits")
        for horizon in ["fwd_30m", "overnight"]:
            names, ts, symbols, X, y = load_panel(conn, horizon, SET_VERSION)
            if SMOKE_DAYS:
                ts, symbols, X, y = filter_smoke(ts, symbols, X, y, SMOKE_DAYS)
            if SMOKE_SYMBOLS:
                keep = set(sorted(set(symbols))[:SMOKE_SYMBOLS])
                mask = np.asarray([s in keep for s in symbols], dtype=bool)
                ts = [t for t, k in zip(ts, mask) if k]
                symbols = [s for s, k in zip(symbols, mask) if k]
                X, y = X[mask], y[mask]
            if len(y) < MIN_ROWS:
                sys.exit(
                    f"PANEL TOO SMALL: {horizon} set={SET_VERSION} has {len(y)} rows (< {MIN_ROWS})."
                )
            cadence_min = 390 if horizon == "overnight" else 30
            vol_scaler = X[:, names.index("vol_30m")]
            base_idx = price_only_idx(names)
            family_f = compute_family_f(symbols, ts, reverse_by_symbol, forward_by_symbol)
            head_mask = liquid_head_mask(conn, symbols)
            rev_rows = float(np.mean(family_f[:, 1]))
            print(
                f"\n=== {horizon} | set={SET_VERSION} | {len(y)} rows | "
                f"{len(reverse_by_symbol)} reverse-split syms | {len(forward_by_symbol)} forward | "
                f"recent-reverse rows {rev_rows:.5f} ===",
                flush=True,
            )

            aug_X = np.hstack([X[:, base_idx], family_f])
            scopes = [("full", np.ones(len(y), dtype=bool)), ("liquid_head", head_mask)]
            for scope_tag, mask in scopes:
                Xb, Xa, Xf, ys, tss, syms, vs = subset(
                    mask, X[:, base_idx], aug_X, family_f, y, ts, symbols, vol_scaler
                )
                if len(ys) < MIN_ROWS:
                    continue
                variants = [
                    (f"baseline_price_only[{scope_tag}]", Xb),
                    (f"plus_family_f[{scope_tag}]", Xa),
                    (f"family_f_only[{scope_tag}]", Xf),
                ]
                for tag, Xv in variants:
                    result = run_variant(tag, Xv, ys, tss, syms, vs, horizon, cadence_min)
                    result["scope"] = scope_tag
                    result["set_version"] = SET_VERSION
                    result["run_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
                    records.append(result)
                    print(
                        f"  {result['variant']:34s} IC={result['mean_ic']:+.5f} "
                        f"canary={result['canary_ic']:+.5f} breakeven={result['breakeven_cost_bps']}bps "
                        f"surv_sharpe={result['survivorship_neutral_sharpe']}",
                        flush=True,
                    )

    with open(RESULTS, "a") as f:
        for result in records:
            f.write(json.dumps(result) + "\n")
    print(f"\nwrote {len(records)} records to {RESULTS}", flush=True)


if __name__ == "__main__":
    main()
