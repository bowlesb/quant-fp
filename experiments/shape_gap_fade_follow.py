"""Shape 002 — OPEN-GAP FADE/FOLLOW, conditional (explorer-shapes).

Helper-table rewrite of the gap half of the modeller's shape_open_gap.py prototype: reads
research.common_daily_session_price (helper-000) instead of re-scanning all 693 bars_1m chunks per
experiment — the cost reason the prior session DEFERRED this shape.

THE SHAPE: the overnight gap (today's 09:30 open vs prior 16:00 close) either FADES (reverts toward
prior close intraday) or FOLLOWS (continues), conditional on gap size x first-30-min volume. We have
gap_from_open as a FEATURE but have never used it as the strategy AXIS with a gap-anchored label.

WHY COST-ADVANTAGED: ONE decision per name per day at the open, held to the close -> far lower turnover
than 30m rebalancing (the killer). Restrict to the liquid head -> cheap-tier round-trip. Long-biased
variant dodges short-underfill.

LABEL (in-memory from the helper — NO panel rebuild, NO bars_1m scan):
  open_to_close = close_1600 / open_0930 - 1, cross-sectionally demeaned within each trade_date
  (reuse the median-excess idea from quantlib.labels.cross_sectional_excess). One row per (symbol, date).
CONDITIONING (helper + prior-session close):
  gap = open_0930 / prior_close_1600 - 1   (PIT: prior session's close)
  or_vol_z = first-30-min volume vs trailing ADV (gap conviction)

PRE-REGISTERED FALSIFIER: if the gap term carries NO within-date rank-IC on open_to_close (|IC|<~0.01,
NW t<2) in ANY conditioning regime AND the net-of-cost L/S book is <=0 — the gap axis is dead.
Pre-registered prior ~35% (gap effects documented but often arbitraged out of liquid names).

DEPENDS ON: research.common_daily_session_price (helper-000). Run as a module from /app:
  docker compose exec -T -w /app experimenter python -m experiments.shape_gap_fade_follow
"""

import json
import math
import os
import statistics
from collections import defaultdict
from datetime import date, datetime, timezone

import numpy as np
import psycopg

from quantlib.backtest import mean_ic, newey_west_tstat, per_timestamp_ic

RESULTS = os.environ.get("GAP_RESULTS", "/app/experiments/shape_gap_results.jsonl")
# TIER=liquid50 restricts to the canonical liquid head (task #5 names) — the honest cost cut, since
# low-vol gaps skew illiquid. Default ALL = the full helper universe.
TIER = os.environ.get("TIER", "all")
LIQUID_TIER = {
    "AAOI",
    "AAPL",
    "AMAT",
    "AMD",
    "AMZN",
    "APP",
    "ARM",
    "ASML",
    "AVGO",
    "BE",
    "BRK.B",
    "CAT",
    "COHR",
    "CRM",
    "CRWV",
    "CSCO",
    "DELL",
    "GEV",
    "GLW",
    "GOOG",
    "GOOGL",
    "IBM",
    "INTC",
    "IREN",
    "JPM",
    "LITE",
    "LLY",
    "LRCX",
    "META",
    "MRVL",
    "MSFT",
    "MSTR",
    "MU",
    "NBIS",
    "NOW",
    "NVDA",
    "ORCL",
    "PLTR",
    "QCOM",
    "RKLB",
    "SNDK",
    "STX",
    "TSLA",
    "TSM",
    "TXN",
    "UNH",
    "V",
    "WDC",
    "WMT",
    "XOM",
}
SEED = 13
MIN_CROSS_SECTION = (
    20  # breadth floor per date (matches quantlib.labels.MIN_CROSS_SECTION)
)
ADV_WINDOW = 20  # trailing sessions for the volume-z baseline
LEG_FRAC = 0.1
COST_SWEEP_BPS = [1.4, 2.0, 2.7]
PERIODS_PER_YEAR = 252.0  # one open->close round-trip per day

DB_KWARGS = {
    "host": os.environ["DB_HOST"],
    "port": int(os.environ.get("DB_PORT", "5432")),
    "dbname": os.environ["DB_NAME"],
    "user": os.environ["DB_USER"],
    "password": os.environ["DB_PASSWORD"],
}


def load_session_prices(
    conn: psycopg.Connection,
) -> dict[str, dict[date, tuple[float, float, float]]]:
    """Per symbol -> {trade_date: (open_0930, vol_0930_1000, close_1600)}. Rows with NULL open or
    close (early-close / missing) are skipped — no synthetic fill."""
    by_symbol: dict[str, dict[date, tuple[float, float, float]]] = defaultdict(dict)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT symbol, trade_date, open_0930, vol_0930_1000, close_1600 "
            "FROM research.common_daily_session_price "
            "WHERE open_0930 IS NOT NULL AND close_1600 IS NOT NULL "
            "ORDER BY symbol, trade_date"
        )
        for symbol, trade_date, open_0930, vol, close_1600 in cur.fetchall():
            by_symbol[symbol][trade_date] = (
                float(open_0930),
                float(vol) if vol is not None else math.nan,
                float(close_1600),
            )
    return by_symbol


def cross_sectional_excess(returns: dict[str, float]) -> dict[str, float]:
    """Subtract the cross-sectional median over valid members; NaN if below the breadth floor."""
    valid = [value for value in returns.values() if not math.isnan(value)]
    if len(valid) < MIN_CROSS_SECTION:
        return {symbol: math.nan for symbol in returns}
    median = statistics.median(valid)
    return {
        symbol: (value - median if not math.isnan(value) else math.nan)
        for symbol, value in returns.items()
    }


def build_records(
    by_symbol: dict[str, dict[date, tuple[float, float, float]]],
) -> tuple[list[datetime], list[str], list[float], list[float], list[float]]:
    """(date_as_ts, symbol, gap, or_vol_z, open_to_close_excess). PIT: gap uses the PRIOR session
    close; or_vol_z uses the trailing ADV_WINDOW first-30-min volumes (excluding today).
    """
    raw_oc_by_date: dict[date, dict[str, float]] = defaultdict(dict)
    gap_by_key: dict[tuple[str, date], float] = {}
    volz_by_key: dict[tuple[str, date], float] = {}
    for symbol, by_date in by_symbol.items():
        sessions = sorted(by_date)
        vol_history: list[float] = []
        for i, trade_date in enumerate(sessions):
            open_0930, vol, close_1600 = by_date[trade_date]
            raw_oc_by_date[trade_date][symbol] = (
                close_1600 / open_0930 - 1.0 if open_0930 else math.nan
            )
            if i > 0:
                prior_close = by_date[sessions[i - 1]][2]
                gap_by_key[(symbol, trade_date)] = (
                    open_0930 / prior_close - 1.0 if prior_close else math.nan
                )
            else:
                gap_by_key[(symbol, trade_date)] = math.nan
            if len(vol_history) >= ADV_WINDOW and not math.isnan(vol):
                baseline = vol_history[-ADV_WINDOW:]
                mean_v = statistics.mean(baseline)
                std_v = statistics.pstdev(baseline)
                volz_by_key[(symbol, trade_date)] = (
                    (vol - mean_v) / std_v if std_v > 0 else math.nan
                )
            else:
                volz_by_key[(symbol, trade_date)] = math.nan
            if not math.isnan(vol):
                vol_history.append(vol)

    out_ts: list[datetime] = []
    out_sym: list[str] = []
    out_gap: list[float] = []
    out_volz: list[float] = []
    out_excess: list[float] = []
    for trade_date, raw_oc in raw_oc_by_date.items():
        excess = cross_sectional_excess(raw_oc)
        as_ts = datetime(
            trade_date.year, trade_date.month, trade_date.day, tzinfo=timezone.utc
        )
        for symbol, oc_excess in excess.items():
            out_ts.append(as_ts)
            out_sym.append(symbol)
            out_gap.append(gap_by_key[(symbol, trade_date)])
            out_volz.append(volz_by_key[(symbol, trade_date)])
            out_excess.append(oc_excess)
    return out_ts, out_sym, out_gap, out_volz, out_excess


def gap_long_short(
    gap: list[float],
    excess: list[float],
    group: list[datetime],
    symbol: list[str],
    direction: float,
    *,
    cost_bps_oneway: float,
) -> dict[str, float]:
    """direction=+1 FOLLOW (long high-gap, short low-gap), -1 FADE (reverse). Realized payoff = the
    open_to_close excess. One round-trip/day. Reports net Sharpe."""
    buckets: dict[datetime, list[tuple[float, float, str]]] = defaultdict(list)
    for gap_value, oc_excess, ts, sym in zip(gap, excess, group, symbol):
        if not (math.isnan(gap_value) or math.isnan(oc_excess)):
            buckets[ts].append((direction * gap_value, oc_excess, sym))
    cost = cost_bps_oneway / 1e4
    gross_list: list[float] = []
    net_list: list[float] = []
    prev_w: dict[str, float] = {}
    for ts in sorted(buckets):
        rows = sorted(buckets[ts], key=lambda row: row[0])
        k = max(1, int(LEG_FRAC * len(rows)))
        if len(rows) < 2 * k:
            continue
        shorts, longs = rows[:k], rows[-k:]
        weights: dict[str, float] = {}
        for _, _, sym in longs:
            weights[sym] = weights.get(sym, 0.0) + 1.0 / len(longs)
        for _, _, sym in shorts:
            weights[sym] = weights.get(sym, 0.0) - 1.0 / len(shorts)
        gross = sum(weights[sym] * oc_excess for _, oc_excess, sym in longs + shorts)
        turnover = sum(
            abs(weights.get(sym, 0.0) - prev_w.get(sym, 0.0))
            for sym in set(weights) | set(prev_w)
        )
        gross_list.append(gross)
        net_list.append(gross - cost * turnover)
        prev_w = weights
    if len(net_list) < 2:
        return {"n_periods": len(net_list)}
    mean_net = statistics.mean(net_list)
    std_net = statistics.stdev(net_list)
    return {
        "n_periods": len(net_list),
        "net_per_period": round(mean_net, 6),
        "sharpe_net": (
            round(mean_net / std_net * math.sqrt(PERIODS_PER_YEAR), 3)
            if std_net > 0
            else math.nan
        ),
    }


def shuffle_excess_within_date(
    excess: list[float], group: list[datetime], seed: int
) -> list[float]:
    """Canary: permute the open_to_close excess WITHIN each date, breaking the gap<->return link
    while preserving the per-date return distribution. A real gap effect must collapse here.
    """
    rng = np.random.default_rng(seed)
    by_date: dict[datetime, list[int]] = defaultdict(list)
    for i, ts in enumerate(group):
        by_date[ts].append(i)
    shuffled = list(excess)
    for idxs in by_date.values():
        values = [excess[i] for i in idxs]
        rng.shuffle(values)
        for i, value in zip(idxs, values):
            shuffled[i] = value
    return shuffled


def per_symbol_demean_signal(signal: list[float], symbol: list[str]) -> list[float]:
    """Survivorship neutralization: subtract each symbol's own mean gap so the signal is the
    TIMING of a name's gaps, not which names persistently gap (a survivor-selection artifact).
    """
    sums: dict[str, float] = defaultdict(float)
    counts: dict[str, int] = defaultdict(int)
    for value, sym in zip(signal, symbol):
        if not math.isnan(value):
            sums[sym] += value
            counts[sym] += 1
    means = {sym: sums[sym] / counts[sym] for sym in sums if counts[sym] > 0}
    return [
        (value - means[sym]) if (sym in means and not math.isnan(value)) else math.nan
        for value, sym in zip(signal, symbol)
    ]


def main() -> None:
    records: list[dict[str, object]] = []
    with psycopg.connect(**DB_KWARGS) as conn:
        by_symbol = load_session_prices(conn)
    if not by_symbol:
        raise SystemExit(
            "research.common_daily_session_price is EMPTY/missing — helper-000 must land first."
        )

    if TIER == "liquid50":
        by_symbol = {sym: rows for sym, rows in by_symbol.items() if sym in LIQUID_TIER}

    ts, symbols, gap, volz, excess = build_records(by_symbol)
    n_dates = len({t.date() for t in ts})
    print(
        f"TIER={TIER} | {len(symbols)} (symbol,date) rows / {n_dates} dates / "
        f"{len(set(symbols))} symbols from the session-price helper",
        flush=True,
    )

    gap_ic = per_timestamp_ic(gap, excess, ts)
    print(
        f"gap -> open_to_close: rank-IC={mean_ic(gap_ic):+.5f} NWt={newey_west_tstat(gap_ic, 1):.2f}",
        flush=True,
    )

    valid_volz = [v for v in volz if not math.isnan(v)]
    volz_median = statistics.median(valid_volz) if valid_volz else math.nan
    for regime_name, keep in [
        ("all", lambda v: True),
        ("high_vol", lambda v: not math.isnan(v) and v >= volz_median),
        ("low_vol", lambda v: not math.isnan(v) and v < volz_median),
    ]:
        idx = [i for i in range(len(symbols)) if keep(volz[i])]
        if len(idx) < 1000:
            print(f"  regime={regime_name}: {len(idx)} rows — SKIP", flush=True)
            continue
        r_gap = [gap[i] for i in idx]
        r_excess = [excess[i] for i in idx]
        r_ts = [ts[i] for i in idx]
        r_sym = [symbols[i] for i in idx]
        r_ic = per_timestamp_ic(r_gap, r_excess, r_ts)
        follow = gap_long_short(r_gap, r_excess, r_ts, r_sym, +1.0, cost_bps_oneway=2.0)
        fade = gap_long_short(r_gap, r_excess, r_ts, r_sym, -1.0, cost_bps_oneway=2.0)

        # GATE 1 — shuffle canary: permute the excess within each date; the IC + the strong-direction
        # Sharpe must collapse to ~0 if the effect is real (not a leak).
        c_excess = shuffle_excess_within_date(r_excess, r_ts, SEED)
        c_ic = per_timestamp_ic(r_gap, c_excess, r_ts)
        c_follow = gap_long_short(
            r_gap, c_excess, r_ts, r_sym, +1.0, cost_bps_oneway=2.0
        )
        c_fade = gap_long_short(r_gap, c_excess, r_ts, r_sym, -1.0, cost_bps_oneway=2.0)

        # GATE 2 — survivorship neutralization: demean the gap signal per symbol (timing, not
        # which names persistently gap). The surviving Sharpe is the honest one.
        n_gap = per_symbol_demean_signal(r_gap, r_sym)
        n_follow = gap_long_short(
            n_gap, r_excess, r_ts, r_sym, +1.0, cost_bps_oneway=2.0
        )
        n_fade = gap_long_short(n_gap, r_excess, r_ts, r_sym, -1.0, cost_bps_oneway=2.0)

        print(
            f"  regime={regime_name:<9} n={len(idx)} IC={mean_ic(r_ic):+.5f} "
            f"follow@2bps={follow.get('sharpe_net')} fade@2bps={fade.get('sharpe_net')} "
            f"| CANARY ic={mean_ic(c_ic):+.5f} follow={c_follow.get('sharpe_net')} "
            f"fade={c_fade.get('sharpe_net')} "
            f"| SURV-NEUTRAL follow={n_follow.get('sharpe_net')} fade={n_fade.get('sharpe_net')}",
            flush=True,
        )
        cost_sweep = {
            f"follow_cost_{cost}": gap_long_short(
                r_gap, r_excess, r_ts, r_sym, +1.0, cost_bps_oneway=cost
            ).get("sharpe_net")
            for cost in COST_SWEEP_BPS
        }
        records.append(
            {
                "run_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "tier": TIER,
                "regime": regime_name,
                "n_rows": len(idx),
                "gap_ic": round(mean_ic(r_ic), 5),
                "gap_nw_t": round(newey_west_tstat(r_ic, 1), 3),
                "follow_sharpe_2bps": follow.get("sharpe_net"),
                "fade_sharpe_2bps": fade.get("sharpe_net"),
                "canary_ic": round(mean_ic(c_ic), 5),
                "canary_follow_sharpe": c_follow.get("sharpe_net"),
                "canary_fade_sharpe": c_fade.get("sharpe_net"),
                "surv_neutral_follow_sharpe": n_follow.get("sharpe_net"),
                "surv_neutral_fade_sharpe": n_fade.get("sharpe_net"),
                "cost_sweep": cost_sweep,
            }
        )

    with open(RESULTS, "a") as results_file:
        for result in records:
            results_file.write(json.dumps(result) + "\n")
    print(f"\nwrote {len(records)} records to {RESULTS}", flush=True)


if __name__ == "__main__":
    main()
