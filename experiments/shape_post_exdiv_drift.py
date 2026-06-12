"""Shape 003 — POST-EX-DIVIDEND DRIFT/REVERSAL (explorer-shapes, event-reaction axis).

THE SHAPE: names that just went ex-dividend show a predictable post-event path over the next 1-5
trading days — dividend-capture unwind (a reversal-up as capture buyers sell) OR a post-drop drift.
Direction is empirical, conditional on yield. Event-triggered -> SPARSE by construction -> structurally
low-turnover. A genuinely different shape AXIS than continuous cross-sectional ranking.

DISTINCT FROM THE EX-DIV LABEL-HYGIENE work (which REMOVES the mechanical ex-date drop so the model
can't cheat) and from Family C (dividend FEATURES, verdicted NO-edge). This TRADES the post-event
drift as a signal — it must beat BOTH the canary AND the Family-C precedent to be a finding.

PRE-REGISTERED FALSIFIER: if the forward N-day return is independent of dividend yield AND has no
consistent sign across the event cohort (cohort mean indistinguishable from the same-date universe,
|t| < 2 at all N in {1,3,5}) — the post-ex-dividend drift shape is dead. Pre-registered prior ~30%
(dividend-capture is documented but heavily arbitraged in liquid US names; more plausible in the
high-yield tail, which is ALSO the wide-spread expensive-to-trade tail — report LIQUID-tier event
count honestly).

LABEL (built in-memory here, sparse — keyed by the ~7,133 ex-dates, NOT a panel rebuild):
  for each (symbol, ex_date), forward simple return from the ex_date canonical close (15:59 ET) to
  the close N TRADING DAYS later, demeaned vs the same-date universe cohort (strip market beta).
  PIT: ex_date is announced ahead (corporate_actions), so trading from the ex_date close is legal.

Run as a module from /app:
  docker compose exec -T -w /app experimenter python -m experiments.shape_post_exdiv_drift
"""

import bisect
import json
import math
import os
import statistics
from collections import defaultdict
from datetime import date, datetime, timezone

import numpy as np
import psycopg

RESULTS = os.environ.get("EXDIV_RESULTS", "/app/experiments/shape_exdiv_results.jsonl")
HORIZONS_DAYS = [1, 3, 5]
SEED = 13
MIN_COHORT = 5  # min events on a date to demean the cohort
# Liquid tier (same 50 as task #5) for the tradeable-event count.
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

DB_KWARGS = {
    "host": os.environ["DB_HOST"],
    "port": int(os.environ.get("DB_PORT", "5432")),
    "dbname": os.environ["DB_NAME"],
    "user": os.environ["DB_USER"],
    "password": os.environ["DB_PASSWORD"],
}


def load_ex_dividends(conn: psycopg.Connection) -> list[tuple[str, date, float]]:
    """(symbol, ex_date, cash_rate) for cash dividends with a usable rate."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT symbol, ex_date, cash_rate FROM corporate_actions "
            "WHERE action_type='cash_dividends' AND cash_rate IS NOT NULL AND cash_rate > 0 "
            "ORDER BY ex_date, symbol"
        )
        return [
            (symbol, ex_date, float(cash_rate))
            for symbol, ex_date, cash_rate in cur.fetchall()
        ]


def load_daily_closes(
    conn: psycopg.Connection, symbols: set[str]
) -> dict[str, dict[date, float]]:
    """Canonical daily close (15:59 ET bar) per symbol/trade_date, for the dividend payers only.
    Sparse vs a full panel — restricted to the payer symbol set."""
    closes: dict[str, dict[date, float]] = defaultdict(dict)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT symbol, (ts AT TIME ZONE 'America/New_York')::date AS d, close "
            "FROM bars_1m "
            "WHERE source='backfill' AND symbol = ANY(%s) "
            "AND (ts AT TIME ZONE 'America/New_York')::time = TIME '15:59'",
            (list(symbols),),
        )
        for symbol, trade_date, close in cur.fetchall():
            closes[symbol][trade_date] = float(close)
    return closes


def trading_days_for(closes_by_date: dict[date, float]) -> list[date]:
    return sorted(closes_by_date)


def forward_nday_return(
    closes_by_date: dict[date, float],
    sorted_days: list[date],
    ex_date: date,
    n_days: int,
) -> float:
    """Simple return from the ex_date close to the close N TRADING days later. NaN if either
    endpoint is missing (no synthetic fill). Uses the symbol's actual sessions as the calendar.
    """
    pos = bisect.bisect_left(sorted_days, ex_date)
    if pos >= len(sorted_days) or sorted_days[pos] != ex_date:
        return math.nan  # no bar on the ex_date itself
    target_pos = pos + n_days
    if target_pos >= len(sorted_days):
        return math.nan
    base = closes_by_date[ex_date]
    target = closes_by_date[sorted_days[target_pos]]
    return (target / base - 1.0) if base else math.nan


def main() -> None:
    records: list[dict[str, object]] = []
    with psycopg.connect(**DB_KWARGS) as conn:
        events = load_ex_dividends(conn)
        payer_symbols = {symbol for symbol, _, _ in events}
        closes = load_daily_closes(conn, payer_symbols)

    sorted_days: dict[str, list[date]] = {
        symbol: trading_days_for(by_date) for symbol, by_date in closes.items()
    }
    print(
        f"{len(events)} ex-div events / {len(payer_symbols)} payers / "
        f"{sum(len(d) for d in closes.values())} daily closes loaded",
        flush=True,
    )

    for n_days in HORIZONS_DAYS:
        # raw forward returns + yields per event
        cohort_by_exdate: dict[date, list[tuple[str, float, float]]] = defaultdict(list)
        for symbol, ex_date, cash_rate in events:
            by_date = closes.get(symbol)
            if not by_date or ex_date not in by_date:
                continue
            fwd = forward_nday_return(by_date, sorted_days[symbol], ex_date, n_days)
            if math.isnan(fwd):
                continue
            base_close = by_date[ex_date]
            div_yield = cash_rate / base_close if base_close else math.nan
            cohort_by_exdate[ex_date].append((symbol, fwd, div_yield))

        # cohort-demean the forward return within each ex_date (strip same-date market move)
        excess: list[float] = []
        yields: list[float] = []
        liquid_excess: list[float] = []
        n_liquid_events = 0
        for ex_date, cohort in cohort_by_exdate.items():
            if len(cohort) < MIN_COHORT:
                continue
            cohort_returns = [fwd for _, fwd, _ in cohort]
            median_ret = statistics.median(cohort_returns)
            for symbol, fwd, div_yield in cohort:
                demeaned = fwd - median_ret
                excess.append(demeaned)
                yields.append(div_yield)
                if symbol in LIQUID_TIER:
                    liquid_excess.append(demeaned)
                    n_liquid_events += 1

        if len(excess) < 30:
            print(f"N={n_days}d: only {len(excess)} usable events — SKIP", flush=True)
            continue

        excess_arr = np.asarray(excess)
        yields_arr = np.asarray(yields)
        mean_excess = float(np.mean(excess_arr))
        # t-stat of the cohort-demeaned post-event return (is it != 0?)
        t_excess = mean_excess / (
            float(np.std(excess_arr, ddof=1)) / math.sqrt(len(excess_arr))
        )
        # yield relationship: correlation of demeaned forward return with dividend yield
        valid = ~np.isnan(yields_arr)
        if valid.sum() > 10 and np.std(yields_arr[valid]) > 0:
            yield_corr = float(np.corrcoef(excess_arr[valid], yields_arr[valid])[0, 1])
        else:
            yield_corr = math.nan
        liquid_mean = float(np.mean(liquid_excess)) if liquid_excess else math.nan

        # PLACEBO CANARY: anchor the same forward-N-day window on a RANDOM trading day per symbol
        # (not the true ex_date). If the real post-ex mean isn't distinguishable from this placebo,
        # the effect is not ex-date-specific. Demeaned within the placebo's own random-date cohort.
        rng = np.random.default_rng(SEED + n_days)
        placebo_by_date: dict[date, list[float]] = defaultdict(list)
        for symbol, ex_date, _ in events:
            symbol_days = sorted_days.get(symbol)
            if not symbol_days or len(symbol_days) < n_days + 2:
                continue
            rand_anchor = symbol_days[
                int(rng.integers(0, len(symbol_days) - n_days - 1))
            ]
            fwd = forward_nday_return(closes[symbol], symbol_days, rand_anchor, n_days)
            if not math.isnan(fwd):
                placebo_by_date[rand_anchor].append(fwd)
        placebo_excess: list[float] = []
        for anchor_date, cohort_returns in placebo_by_date.items():
            if len(cohort_returns) < MIN_COHORT:
                continue
            median_ret = statistics.median(cohort_returns)
            placebo_excess.extend(ret - median_ret for ret in cohort_returns)
        placebo_mean = float(np.mean(placebo_excess)) if placebo_excess else math.nan

        print(
            f"N={n_days}d: {len(excess)} events ({n_liquid_events} liquid) | "
            f"mean post-ex excess={mean_excess:+.5f} t={t_excess:+.2f} | "
            f"yield-corr={yield_corr:+.4f} | liquid mean={liquid_mean:+.5f} | "
            f"placebo mean={placebo_mean:+.5f}",
            flush=True,
        )
        records.append(
            {
                "run_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "horizon_days": n_days,
                "n_events": len(excess),
                "n_liquid_events": n_liquid_events,
                "mean_excess": round(mean_excess, 6),
                "t_excess": round(t_excess, 3),
                "yield_corr": (
                    round(yield_corr, 4) if not math.isnan(yield_corr) else None
                ),
                "liquid_mean_excess": (
                    round(liquid_mean, 6) if not math.isnan(liquid_mean) else None
                ),
                "placebo_mean_excess": (
                    round(placebo_mean, 6) if not math.isnan(placebo_mean) else None
                ),
            }
        )

    with open(RESULTS, "a") as f:
        for result in records:
            f.write(json.dumps(result) + "\n")
    print(f"\nwrote {len(records)} records to {RESULTS}", flush=True)


if __name__ == "__main__":
    main()
