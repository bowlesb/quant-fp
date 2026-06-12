"""Family D prototype — high-low realized-vol estimators + intraday range percentile.

NEW transform of data we ALREADY discard: the bar HIGH/LOW path. The panel's only vol features
are vol_30m/vol_60m = close-to-close std of minute returns, which throw away the intra-minute
range. Realized-vol estimators that use the range are far more efficient (Parkinson ~5x,
Garman-Klass ~7-8x), and the range PERCENTILE vs the name's own history is a self-normalized
"is this name unusually active right now" signal. ZERO new collection, full ~715-name breadth.
Mirrors family_c / family_e exactly so the gates are byte-identical via battery.run_config.

Mechanism (why this could rank where ret_5m cannot): information arrival shows up as abnormal
RANGE before it resolves into a clean directional return. ret_5m sees the realized move; the
range-surprise sees that a re-pricing is underway. Vol STRUCTURE as a cross-sectional ranker is
untested (the grind exhausted realized RETURNS, never realized VOLATILITY structure).

Features (all strictly point-in-time at each panel cadence ts, from bars at-or-before ts):
- parkinson_vol_30 : sqrt( (1/(4 ln2)) * mean( ln(high/low)^2 ) ) over the trailing 30 1-min bars.
- gk_vol_30        : sqrt( mean( 0.5*ln(h/l)^2 - (2 ln2 - 1)*ln(c/o)^2 ) ) over the trailing 30.
- range_pctile_20d : today's session-to-date GK range ranked within the name's own trailing-
                     VOL_LOOKBACK_DAYS session-to-date GK range at the SAME minute-of-day (0..1).
                     Same-time-of-day baseline removes the intraday vol U-shape (not calendar).

POINT-IN-TIME / LOOKAHEAD GUARD: every term uses bars with bar.ts <= panel ts. The percentile
baseline uses COMPLETED PRIOR days only. NaN before VOL_LOOKBACK_DAYS of history at that minute.

LIQUIDITY-STRATIFIED READ (Lead's context): ret_5m is an ILLIQUID-name reversal (IC ~0 on the
liquid head). A feature that only lifts illiquid names inherits the cost problem; the prize
carries signal in the LIQUID head where cost is payable. So we ALSO report each variant on the
top-ADV liquidity quartile separately from the full panel.

Baseline = the SAME C11 price-only set in results.jsonl (apples-to-apples breakeven-lift claim).
If +Family-D lifts IC above the canary AND raises breakeven -> Tier-1 PR. A pure vol-LEVEL gain
with no breakeven lift is a FAIL for trading (note it as a label-denominator improvement for
explorer-ml, not a ranker). Honest either way.

Run as a MODULE from /app:
  docker compose exec -T -w /app -e SET_VERSION=v1.1.1 experimenter python -m experiments.family_d_highlow_vol
  # fast smoke (last N days):
  docker compose exec -T -w /app -e SET_VERSION=v1.1.1 -e SMOKE_DAYS=120 experimenter python -m experiments.family_d_highlow_vol
"""

import json
import math
import os
import sys
from collections import defaultdict
from datetime import date, datetime, time as dtime, timezone
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
RESULTS = os.environ.get("FAMILY_D_RESULTS", "/app/experiments/family_d_results.jsonl")
FORBIDDEN_VERSIONS = {"v1.0.0", "v1.1.0"}
MIN_ROWS = 1000

VOL_LOOKBACK_DAYS = 20
TRAIL_MIN = 30
LIQUID_HEAD_FRAC = 0.25
FAMILY_D_NAMES = ["parkinson_vol_30", "gk_vol_30", "range_pctile_20d"]

_NY = ZoneInfo("America/New_York")
_RTH_OPEN = dtime(9, 30)
_RTH_CLOSE = dtime(16, 0)
_LN2 = math.log(2.0)
_GK_C = 2.0 * _LN2 - 1.0

DB_KWARGS = {
    "host": os.environ["DB_HOST"],
    "port": int(os.environ.get("DB_PORT", "5432")),
    "dbname": os.environ["DB_NAME"],
    "user": os.environ["DB_USER"],
    "password": os.environ["DB_PASSWORD"],
}


def session_minute(ts_utc: datetime) -> int:
    local = ts_utc.astimezone(_NY)
    if not (_RTH_OPEN <= local.timetz().replace(tzinfo=None) < _RTH_CLOSE):
        return -1
    return (local.hour - 9) * 60 + local.minute - 30


def load_symbol_bars(conn: psycopg.Connection, symbol: str, start: date, end: date):
    with conn.cursor() as cur:
        cur.execute(
            """SELECT ts, open, high, low, close FROM bars_1m
               WHERE symbol=%s AND source='backfill' AND ts >= %s AND ts < %s
               ORDER BY ts""",
            (
                symbol,
                datetime.combine(start, dtime(0, 0), _NY),
                datetime.combine(end, dtime(0, 0), _NY),
            ),
        )
        return cur.fetchall()


def build_symbol_index(rows) -> dict[date, dict[int, tuple[float, float, float, float]]]:
    """date -> {session_minute -> (open, high, low, close)} for RTH bars."""
    index: dict[date, dict[int, tuple[float, float, float, float]]] = defaultdict(dict)
    for ts_utc, open_, high, low, close in rows:
        minute = session_minute(ts_utc)
        if minute < 0:
            continue
        index[ts_utc.astimezone(_NY).date()][minute] = (
            float(open_),
            float(high),
            float(low),
            float(close),
        )
    return index


def parkinson_gk(
    day_bars: dict[int, tuple[float, float, float, float]], lo: int, hi: int
) -> tuple[float, float]:
    """Parkinson and Garman-Klass realized vol over minutes [lo, hi] (inclusive). NaN if empty."""
    park_terms: list[float] = []
    gk_terms: list[float] = []
    for minute in range(lo, hi + 1):
        bar = day_bars.get(minute)
        if bar is None:
            continue
        open_, high, low, close = bar
        if high <= 0 or low <= 0 or open_ <= 0 or close <= 0 or high < low:
            continue
        hl = math.log(high / low)
        co = math.log(close / open_)
        park_terms.append(hl * hl)
        gk_terms.append(0.5 * hl * hl - _GK_C * co * co)
    if not park_terms:
        return np.nan, np.nan
    park = math.sqrt(max(0.0, (1.0 / (4.0 * _LN2)) * (sum(park_terms) / len(park_terms))))
    gk_mean = sum(gk_terms) / len(gk_terms)
    gk = math.sqrt(gk_mean) if gk_mean > 0 else 0.0
    return park, gk


def compute_symbol_features(
    index: dict[date, dict[int, tuple[float, float, float, float]]],
    panel_rows_for_symbol: list[tuple[int, date, int]],
) -> list[tuple[int, float, float, float]]:
    """Precomputes per-(date, cadence-minute) trailing Parkinson/GK and session-to-date GK ONCE,
    so each panel row is an O(VOL_LOOKBACK_DAYS) lookup instead of re-scanning bar ranges."""
    sorted_dates = sorted(index.keys())
    date_pos = {d: i for i, d in enumerate(sorted_dates)}
    cadence_minutes = sorted({minute for _, _, minute in panel_rows_for_symbol if minute >= 0})
    trail_pg: dict[tuple[date, int], tuple[float, float]] = {}
    sess_gk: dict[tuple[date, int], float] = {}
    for trade_date in sorted_dates:
        day_bars = index[trade_date]
        for minute in cadence_minutes:
            lo = max(0, minute - TRAIL_MIN + 1)
            trail_pg[(trade_date, minute)] = parkinson_gk(day_bars, lo, minute)
            sess_gk[(trade_date, minute)] = parkinson_gk(day_bars, 0, minute)[1]

    out: list[tuple[int, float, float, float]] = []
    for row_idx, trade_date, end_minute in panel_rows_for_symbol:
        pos = date_pos.get(trade_date)
        if pos is None or end_minute < 0:
            out.append((row_idx, np.nan, np.nan, np.nan))
            continue
        park, gk = trail_pg[(trade_date, end_minute)]

        prior_dates = sorted_dates[max(0, pos - VOL_LOOKBACK_DAYS) : pos]
        if len(prior_dates) < VOL_LOOKBACK_DAYS:
            out.append((row_idx, park, gk, np.nan))
            continue
        gk_sess_today = sess_gk[(trade_date, end_minute)]
        base_arr = np.asarray(
            [
                sess_gk[(d, end_minute)]
                for d in prior_dates
                if np.isfinite(sess_gk[(d, end_minute)])
            ],
            dtype=float,
        )
        if base_arr.size == 0 or not np.isfinite(gk_sess_today):
            pctile = np.nan
        else:
            pctile = float(np.mean(base_arr <= gk_sess_today))
        out.append((row_idx, park, gk, pctile))
    return out


def compute_family_d(
    conn: psycopg.Connection, ts: list[datetime], symbols: list[str]
) -> np.ndarray:
    out = np.full((len(symbols), len(FAMILY_D_NAMES)), np.nan, dtype=float)
    rows_by_symbol: dict[str, list[tuple[int, date, int]]] = defaultdict(list)
    for i, (symbol, timestamp) in enumerate(zip(symbols, ts)):
        rows_by_symbol[symbol].append(
            (i, timestamp.astimezone(_NY).date(), session_minute(timestamp))
        )
    all_dates = [t.astimezone(_NY).date() for t in ts]
    start, end = min(all_dates), max(all_dates)
    pad_start = date(start.year - 1, start.month, start.day)
    for symbol, sym_rows in rows_by_symbol.items():
        bars = load_symbol_bars(conn, symbol, pad_start, date(end.year, end.month, end.day))
        if not bars:
            continue
        index = build_symbol_index(bars)
        for row_idx, park, gk, pctile in compute_symbol_features(index, sym_rows):
            out[row_idx] = (park, gk, pctile)
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
        for horizon in ["fwd_30m", "fwd_60m"]:
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
            cadence_min = 30
            vol_scaler = X[:, names.index("vol_30m")]
            base_idx = price_only_idx(names)
            family_d = compute_family_d(conn, ts, symbols)
            head_mask = liquid_head_mask(conn, symbols)
            nan_rate = float(np.isnan(family_d).mean())
            print(
                f"\n=== {horizon} | set={SET_VERSION} | {len(y)} rows | "
                f"Family-D NaN-rate {nan_rate:.4f} | liquid-head rows {int(head_mask.sum())} ===",
                flush=True,
            )

            aug_X = np.hstack([X[:, base_idx], family_d])
            scopes = [("full", np.ones(len(y), dtype=bool)), ("liquid_head", head_mask)]
            for scope_tag, mask in scopes:
                Xb, Xa, Xd, ys, tss, syms, vs = subset(
                    mask, X[:, base_idx], aug_X, family_d, y, ts, symbols, vol_scaler
                )
                if len(ys) < MIN_ROWS:
                    continue
                variants = [
                    (f"baseline_price_only[{scope_tag}]", Xb),
                    (f"plus_family_d[{scope_tag}]", Xa),
                    (f"family_d_only[{scope_tag}]", Xd),
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
