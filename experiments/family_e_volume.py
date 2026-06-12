"""Family E prototype — volume / turnover surprise features (the panel has ZERO volume features).

NEW transform of data we ALREADY have: bars_1m volume + vwap, joined to the v1.1.1 panel. ZERO
new collection, full ~715-name breadth. Mirrors family_c_dividend_timing.py exactly so the gates
are byte-identical via battery.run_config. The question: does HOW MUCH a name traded — relative to
its OWN norm — carry within-ts cross-sectional signal orthogonal to the price-return features
(the grind verdict: the 30m signal IS ret_5m; momentum is dead weight)?

Mechanism: abnormal volume is the textbook information-arrival / attention proxy, and the
volume->volatility->return lead-lag is one of the most replicated facts in microstructure.
ret_5m sees the move that ALREADY happened; a volume surprise says a re-pricing is IN PROGRESS.

Features (all strictly point-in-time at each panel cadence ts, from bars at-or-before ts):
- vol_surprise_z_30  : z-score of the trailing 30-min total share volume vs the name's OWN
                       distribution of 30-min volume at the SAME minute-of-day over the trailing
                       VOL_LOOKBACK_DAYS trading days. Same-time-of-day baseline removes the
                       intraday U-shape, so this is NOT a calendar feature in disguise.
- dollar_turnover_pctile_20d : session-to-date dollar volume (sum vwap*volume from the open to
                       ts) ranked within the name's own trailing-VOL_LOOKBACK_DAYS session-to-date
                       dollar-volume distribution at the same minute-of-day (0..1). The cross-
                       sectionally-comparable "attention" rank.
- signed_vol_ret_interaction : sign(ret_5m) * vol_surprise_z_30 — continuation when a move rides
                       rising volume, exhaustion/reversal flag when volume fades. Lets the model
                       express the split the price panel cannot.

POINT-IN-TIME / LOOKAHEAD GUARD: every term uses bars with bar.ts <= panel ts only. The same-
minute-of-day baseline uses COMPLETED PRIOR trading days plus the current partial session up to
ts; never a future bar. NaN before VOL_LOOKBACK_DAYS of history at that minute-of-day — never a
partial-window placeholder.

LIQUIDITY-STRATIFIED READ (Lead's load-bearing context): ret_5m is an ILLIQUID-name reversal
(IC ~0 on the liquid-50 tier). A feature that only lifts IC on illiquid names inherits the cost
problem; the prize carries signal in the LIQUID head where cost is payable. So we ALSO report each
variant on the top-ADV liquidity quartile (LIQUID_HEAD) separately from the full panel.

Gates (identical to the battery): within-ts rank-IC + NW t, shuffle canary, net-of-cost L/S,
survivorship demean. Baseline = the SAME C11 price-only set in results.jsonl (so the breakeven-
lift claim is apples-to-apples). If +Family-E lifts IC above the canary AND raises breakeven ->
Tier-1 PR. If it moves nothing -> volume adds no cross-sectional signal at 30m; honest either way.

Run as a MODULE from /app (so quantlib + experiments.battery resolve):
  docker compose exec -T -w /app -e SET_VERSION=v1.1.1 experimenter python -m experiments.family_e_volume
  # fast smoke (last N days):
  docker compose exec -T -w /app -e SET_VERSION=v1.1.1 -e SMOKE_DAYS=120 experimenter python -m experiments.family_e_volume
"""

import json
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
RESULTS = os.environ.get("FAMILY_E_RESULTS", "/app/experiments/family_e_results.jsonl")
FORBIDDEN_VERSIONS = {"v1.0.0", "v1.1.0"}
MIN_ROWS = 1000

VOL_LOOKBACK_DAYS = 20  # trailing trading days for the name's own same-time-of-day baseline
TRAIL_MIN = 30  # trailing window (minutes) for the volume-surprise numerator
LIQUID_HEAD_FRAC = 0.25  # top-ADV fraction = the liquid head where cost is payable
FAMILY_E_NAMES = ["vol_surprise_z_30", "dollar_turnover_pctile_20d", "signed_vol_ret_interaction"]

_NY = ZoneInfo("America/New_York")
_RTH_OPEN = dtime(9, 30)
_RTH_CLOSE = dtime(16, 0)

DB_KWARGS = {
    "host": os.environ["DB_HOST"],
    "port": int(os.environ.get("DB_PORT", "5432")),
    "dbname": os.environ["DB_NAME"],
    "user": os.environ["DB_USER"],
    "password": os.environ["DB_PASSWORD"],
}


def panel_symbols(ts: list[datetime], symbols: list[str]) -> list[str]:
    return sorted(set(symbols))


def load_symbol_bars(conn: psycopg.Connection, symbol: str, start: date, end: date):
    """RTH 1-min bars for one symbol, backfill source, ordered. Returns parallel arrays
    (et_minute_of_day, trade_date, volume, dollar) — bounded memory, one symbol at a time."""
    with conn.cursor() as cur:
        cur.execute(
            """SELECT ts, volume, vwap, close
               FROM bars_1m
               WHERE symbol=%s AND source='backfill' AND ts >= %s AND ts < %s
               ORDER BY ts""",
            (
                symbol,
                datetime.combine(start, dtime(0, 0), _NY),
                datetime.combine(end, dtime(0, 0), _NY),
            ),
        )
        rows = cur.fetchall()
    return rows


def session_minute(ts_utc: datetime) -> int:
    """Minutes since 09:30 ET for an RTH timestamp (0..389), or -1 if outside RTH."""
    local = ts_utc.astimezone(_NY)
    if not (_RTH_OPEN <= local.timetz().replace(tzinfo=None) < _RTH_CLOSE):
        return -1
    return (local.hour - 9) * 60 + local.minute - 30


def build_symbol_index(rows) -> dict[date, dict[int, tuple[float, float]]]:
    """date -> {session_minute -> (volume, dollar)} for RTH bars. dollar = price*volume per bar."""
    index: dict[date, dict[int, tuple[float, float]]] = defaultdict(dict)
    for ts_utc, volume, vwap, close in rows:
        minute = session_minute(ts_utc)
        if minute < 0:
            continue
        price = float(vwap) if vwap is not None else float(close)
        index[ts_utc.astimezone(_NY).date()][minute] = (float(volume), price * float(volume))
    return index


def trailing_volume(
    day_bars: dict[int, tuple[float, float]], end_minute: int, window: int
) -> float:
    """Sum of share volume over the `window` minutes ending at end_minute (inclusive)."""
    lo = max(0, end_minute - window + 1)
    return float(sum(day_bars.get(minute, (0.0, 0.0))[0] for minute in range(lo, end_minute + 1)))


def session_dollar(day_bars: dict[int, tuple[float, float]], end_minute: int) -> float:
    """Session-to-date dollar volume from the open through end_minute (inclusive)."""
    return float(sum(day_bars.get(minute, (0.0, 0.0))[1] for minute in range(0, end_minute + 1)))


def compute_symbol_features(
    index: dict[date, dict[int, tuple[float, float]]],
    panel_rows_for_symbol: list[tuple[int, date, int, float]],
) -> list[tuple[int, float, float, float]]:
    """For each (row_idx, trade_date, end_minute, ret_5m) panel row of this symbol, compute the
    3 Family-E features using only same-symbol bars at-or-before that row's date+minute.

    Baselines (trailing same-minute-of-day distributions) use COMPLETED PRIOR days only.
    Precomputes per-(date, cadence-minute) trailing-volume and session-dollar ONCE so each row is
    an O(VOL_LOOKBACK_DAYS) lookup instead of re-summing bar ranges per row."""
    sorted_dates = sorted(index.keys())
    date_pos = {d: i for i, d in enumerate(sorted_dates)}
    cadence_minutes = sorted({minute for _, _, minute, _ in panel_rows_for_symbol if minute >= 0})
    trail_by: dict[tuple[date, int], float] = {}
    dollar_by: dict[tuple[date, int], float] = {}
    for trade_date in sorted_dates:
        day_bars = index[trade_date]
        for minute in cadence_minutes:
            trail_by[(trade_date, minute)] = trailing_volume(day_bars, minute, TRAIL_MIN)
            dollar_by[(trade_date, minute)] = session_dollar(day_bars, minute)

    out: list[tuple[int, float, float, float]] = []
    for row_idx, trade_date, end_minute, ret_5m in panel_rows_for_symbol:
        pos = date_pos.get(trade_date)
        if pos is None or end_minute < 0:
            out.append((row_idx, np.nan, np.nan, np.nan))
            continue
        prior_dates = sorted_dates[max(0, pos - VOL_LOOKBACK_DAYS) : pos]
        if len(prior_dates) < VOL_LOOKBACK_DAYS:
            out.append((row_idx, np.nan, np.nan, np.nan))
            continue

        cur_trail_vol = trail_by[(trade_date, end_minute)]
        cur_sess_dollar = dollar_by[(trade_date, end_minute)]
        base_trail_arr = np.asarray([trail_by[(d, end_minute)] for d in prior_dates], dtype=float)
        base_dollar_arr = np.asarray([dollar_by[(d, end_minute)] for d in prior_dates], dtype=float)
        mu = float(np.mean(base_trail_arr))
        sigma = float(np.std(base_trail_arr))
        vol_z = (cur_trail_vol - mu) / sigma if sigma > 0 else np.nan
        pctile = float(np.mean(base_dollar_arr <= cur_sess_dollar))
        sign = 0.0 if not np.isfinite(ret_5m) or ret_5m == 0 else (1.0 if ret_5m > 0 else -1.0)
        interaction = sign * vol_z if np.isfinite(vol_z) else np.nan
        out.append((row_idx, vol_z, pctile, interaction))
    return out


def compute_family_e(
    conn: psycopg.Connection,
    ts: list[datetime],
    symbols: list[str],
    ret_5m: np.ndarray,
) -> np.ndarray:
    """(n_rows x 3) [vol_surprise_z_30, dollar_turnover_pctile_20d, signed_vol_ret_interaction].

    Streams one symbol at a time: load its RTH bars, compute that symbol's panel-row features,
    discard. Bounded memory across the ~715-name panel."""
    out = np.full((len(symbols), len(FAMILY_E_NAMES)), np.nan, dtype=float)
    rows_by_symbol: dict[str, list[tuple[int, date, int, float]]] = defaultdict(list)
    for i, (symbol, timestamp) in enumerate(zip(symbols, ts)):
        rows_by_symbol[symbol].append(
            (i, timestamp.astimezone(_NY).date(), session_minute(timestamp), float(ret_5m[i]))
        )
    all_dates = [t.astimezone(_NY).date() for t in ts]
    start = min(all_dates)
    end = max(all_dates)
    # extend start back so the first panel day has its VOL_LOOKBACK_DAYS baseline (calendar-pad).
    pad_start = date(start.year - 1, start.month, start.day)
    for symbol, sym_rows in rows_by_symbol.items():
        bars = load_symbol_bars(conn, symbol, pad_start, date(end.year, end.month, end.day))
        if not bars:
            continue
        index = build_symbol_index(bars)
        for row_idx, vol_z, pctile, interaction in compute_symbol_features(index, sym_rows):
            out[row_idx] = (vol_z, pctile, interaction)
    return out


def liquid_head_mask(
    conn: psycopg.Connection, symbols: list[str], ts: list[datetime]
) -> np.ndarray:
    """Boolean mask: rows whose symbol is in the top LIQUID_HEAD_FRAC by median session dollar
    volume over the panel window (the liquid head where trading cost is payable)."""
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
    """The 19 price feats (drop the 2 calendar cols), matching the battery's price-only set."""
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
    """Apply a boolean row-mask to numpy arrays and parallel python lists."""
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
            ret_5m = X[:, names.index("ret_5m")]
            family_e = compute_family_e(conn, ts, symbols, ret_5m)
            head_mask = liquid_head_mask(conn, symbols, ts)
            nan_rate = float(np.isnan(family_e).mean())
            print(
                f"\n=== {horizon} | set={SET_VERSION} | {len(y)} rows | "
                f"Family-E NaN-rate {nan_rate:.4f} | liquid-head rows {int(head_mask.sum())} ===",
                flush=True,
            )

            aug_X = np.hstack([X[:, base_idx], family_e])
            scopes = [("full", np.ones(len(y), dtype=bool)), ("liquid_head", head_mask)]
            for scope_tag, mask in scopes:
                Xb, Xa, Xe, ys, tss, syms, vs = subset(
                    mask, X[:, base_idx], aug_X, family_e, y, ts, symbols, vol_scaler
                )
                if len(ys) < MIN_ROWS:
                    continue
                variants = [
                    (f"baseline_price_only[{scope_tag}]", Xb),
                    (f"plus_family_e[{scope_tag}]", Xa),
                    (f"family_e_only[{scope_tag}]", Xe),
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
