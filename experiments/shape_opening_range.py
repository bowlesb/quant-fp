"""Shape 004 — OPENING-RANGE BREAKOUT, liquid-head (explorer-shapes).

Helper-table rewrite of the opening-range half of the modeller's shape_open_gap.py prototype: reads
research.common_daily_session_price (helper-000) instead of re-scanning bars_1m per experiment.

THE SHAPE: a name that breaks above its 09:30-10:00 HIGH (or below its LOW) tends to continue in the
breakout direction for the rest of the session (classic opening-range breakout). A SINGLE-NAME
TIME-SERIES signal (each name vs its OWN range) — a different axis than cross-sectional ranking.

WHY COST-ADVANTAGED: SPARSE (fires only on names that actually break), one decision/name/day held to
close -> low turnover. Restrict to the liquid head -> cheap-tier round-trip. Long-only (up-breaks
only) variant dodges short-underfill.

LABEL (in-memory from the helper): ten_to_close = close_1600 / px_1000 - 1 (the post-opening-range
window). This is a TIME-SERIES shape, so the RAW direction is the signal (no cross-sectional demean).
SIGNAL: position_in_range = (px_1000 - low) / (high - low); break_up if px_1000 >= high, break_down if
px_1000 <= low. or_vol_z = first-30-min volume vs trailing ADV (breakout conviction).

PRE-REGISTERED FALSIFIER: if ten_to_close is independent of break direction / position_in_range
(breakout-cohort mean indistinguishable from non-breakout, |t|<2) AND the net-of-cost long-only book
is <=0 — ORB is dead in this universe. Pre-registered prior ~30% (ORB is folklore-popular but
well-arbitraged in liquid names and notoriously cost-sensitive).

DEPENDS ON: research.common_daily_session_price (helper-000). Run as a module from /app:
  docker compose exec -T -w /app experimenter python -m experiments.shape_opening_range
"""

import json
import math
import os
import statistics
from collections import defaultdict
from datetime import date, datetime, timezone

import numpy as np
import psycopg

RESULTS = os.environ.get("ORB_RESULTS", "/app/experiments/shape_orb_results.jsonl")
SEED = 13
ADV_WINDOW = 20
COST_SWEEP_BPS = [1.4, 2.0, 2.7]
PERIODS_PER_YEAR = 252.0

DB_KWARGS = {
    "host": os.environ["DB_HOST"],
    "port": int(os.environ.get("DB_PORT", "5432")),
    "dbname": os.environ["DB_NAME"],
    "user": os.environ["DB_USER"],
    "password": os.environ["DB_PASSWORD"],
}


def load_orb_rows(
    conn: psycopg.Connection,
) -> dict[str, dict[date, tuple[float, float, float, float, float]]]:
    """Per symbol -> {trade_date: (px_1000, high, low, vol_0930_1000, close_1600)}. Skips rows with a
    NULL px_1000/high/low/close (early-close / missing) — no synthetic fill."""
    by_symbol: dict[str, dict[date, tuple[float, float, float, float, float]]] = (
        defaultdict(dict)
    )
    with conn.cursor() as cur:
        cur.execute(
            "SELECT symbol, trade_date, px_1000, high_0930_1000, low_0930_1000, "
            "vol_0930_1000, close_1600 "
            "FROM research.common_daily_session_price "
            "WHERE px_1000 IS NOT NULL AND high_0930_1000 IS NOT NULL "
            "AND low_0930_1000 IS NOT NULL AND close_1600 IS NOT NULL "
            "ORDER BY symbol, trade_date"
        )
        for symbol, trade_date, px_1000, high, low, vol, close_1600 in cur.fetchall():
            by_symbol[symbol][trade_date] = (
                float(px_1000),
                float(high),
                float(low),
                float(vol) if vol is not None else math.nan,
                float(close_1600),
            )
    return by_symbol


def build_rows(
    by_symbol: dict[str, dict[date, tuple[float, float, float, float, float]]],
) -> tuple[list[str], list[float], list[int], list[float], list[float]]:
    """(symbol, position_in_range, break_dir, or_vol_z, ten_to_close). break_dir in {-1,0,+1}.
    or_vol_z uses the trailing ADV_WINDOW first-30-min volumes (excluding today)."""
    out_sym: list[str] = []
    out_pos: list[float] = []
    out_break: list[int] = []
    out_volz: list[float] = []
    out_ttc: list[float] = []
    for symbol, by_date in by_symbol.items():
        vol_history: list[float] = []
        for trade_date in sorted(by_date):
            px_1000, high, low, vol, close_1600 = by_date[trade_date]
            ten_to_close = close_1600 / px_1000 - 1.0 if px_1000 else math.nan
            range_width = high - low
            position = (px_1000 - low) / range_width if range_width > 0 else math.nan
            if range_width <= 0:
                break_dir = 0
            elif px_1000 >= high:
                break_dir = 1
            elif px_1000 <= low:
                break_dir = -1
            else:
                break_dir = 0
            if len(vol_history) >= ADV_WINDOW and not math.isnan(vol):
                baseline = vol_history[-ADV_WINDOW:]
                std_v = statistics.pstdev(baseline)
                vol_z = (
                    (vol - statistics.mean(baseline)) / std_v if std_v > 0 else math.nan
                )
            else:
                vol_z = math.nan
            if not math.isnan(vol):
                vol_history.append(vol)
            if math.isnan(ten_to_close):
                continue
            out_sym.append(symbol)
            out_pos.append(position)
            out_break.append(break_dir)
            out_volz.append(vol_z)
            out_ttc.append(ten_to_close)
    return out_sym, out_pos, out_break, out_volz, out_ttc


def cohort_stats(returns: list[float]) -> tuple[float, float, int]:
    """mean, t-stat (vs 0), n of a return cohort."""
    n = len(returns)
    if n < 2:
        return math.nan, math.nan, n
    mean = statistics.mean(returns)
    std = statistics.stdev(returns)
    t = mean / (std / math.sqrt(n)) if std > 0 else math.nan
    return mean, t, n


def main() -> None:
    records: list[dict[str, object]] = []
    with psycopg.connect(**DB_KWARGS) as conn:
        by_symbol = load_orb_rows(conn)
    if not by_symbol:
        raise SystemExit(
            "research.common_daily_session_price is EMPTY/missing — helper-000 must land first."
        )

    symbols, position, break_dir, vol_z, ten_to_close = build_rows(by_symbol)
    print(
        f"{len(symbols)} (symbol,date) rows / {len(set(symbols))} symbols from the helper",
        flush=True,
    )

    ttc_arr = np.asarray(ten_to_close)
    break_arr = np.asarray(break_dir)
    # Core question: does the break direction predict the sign of ten_to_close?
    up_returns = ttc_arr[break_arr == 1].tolist()
    down_returns = ttc_arr[break_arr == -1].tolist()
    no_break = ttc_arr[break_arr == 0].tolist()
    up_mean, up_t, up_n = cohort_stats(up_returns)
    down_mean, down_t, down_n = cohort_stats(down_returns)
    nb_mean, _nb_t, nb_n = cohort_stats(no_break)
    print(
        f"  break_UP: mean ttc={up_mean:+.5f} t={up_t:+.2f} n={up_n} | "
        f"break_DOWN: mean ttc={down_mean:+.5f} t={down_t:+.2f} n={down_n} | "
        f"no_break: mean={nb_mean:+.5f} n={nb_n}",
        flush=True,
    )

    # position-in-range monotonicity: does a higher position predict a higher ten_to_close?
    valid = ~np.isnan(np.asarray(position))
    pos_arr = np.asarray(position)
    if valid.sum() > 100 and np.std(pos_arr[valid]) > 0:
        pos_corr = float(np.corrcoef(pos_arr[valid], ttc_arr[valid])[0, 1])
    else:
        pos_corr = math.nan
    print(f"  corr(position_in_range, ten_to_close)={pos_corr:+.4f}", flush=True)

    # Long-only up-break book: equal-weight all up-breaks each day; net of cost (one round-trip/day).
    # (Time-series shape — the "book" is the cross-section of names that broke up that day.)
    by_date_up: dict[date, list[float]] = defaultdict(list)
    for by_date in by_symbol.values():
        for trade_date in sorted(by_date):
            px_1000, high, low, _vol, close_1600 = by_date[trade_date]
            if px_1000 and high > low and px_1000 >= high:
                by_date_up[trade_date].append(close_1600 / px_1000 - 1.0)
    daily_returns = [statistics.mean(rets) for rets in by_date_up.values() if rets]
    for cost in COST_SWEEP_BPS:
        net = [
            ret - cost / 1e4 for ret in daily_returns
        ]  # one round-trip/day on the up-break book
        if len(net) >= 2 and statistics.stdev(net) > 0:
            sharpe = (
                statistics.mean(net)
                / statistics.stdev(net)
                * math.sqrt(PERIODS_PER_YEAR)
            )
        else:
            sharpe = math.nan
        print(
            f"  long-only up-break book: cost {cost}bps -> sharpe_net {round(sharpe, 3)} "
            f"({len(net)} days)",
            flush=True,
        )

    records.append(
        {
            "run_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "break_up_mean": round(up_mean, 6) if not math.isnan(up_mean) else None,
            "break_up_t": round(up_t, 3) if not math.isnan(up_t) else None,
            "break_up_n": up_n,
            "break_down_mean": (
                round(down_mean, 6) if not math.isnan(down_mean) else None
            ),
            "break_down_t": round(down_t, 3) if not math.isnan(down_t) else None,
            "break_down_n": down_n,
            "no_break_mean": round(nb_mean, 6) if not math.isnan(nb_mean) else None,
            "position_corr": round(pos_corr, 4) if not math.isnan(pos_corr) else None,
        }
    )

    with open(RESULTS, "a") as results_file:
        for result in records:
            results_file.write(json.dumps(result) + "\n")
    print(f"\nwrote {len(records)} records to {RESULTS}", flush=True)


if __name__ == "__main__":
    main()
