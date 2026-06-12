"""Shape 002 VERDICT GATE — walk-forward OOS + measured-open-cost on the gap fade/follow shape.

The two gates the Lead requires before promoting the conditional gap-fade (the strongest lead the
effort has produced — 3 of 4 M3 gates passed: within-ts IC + clean canary + survivorship + positive
net on liquid-50 at FLAT 2bps). This script replaces the in-sample sort with WALK-FORWARD folds and
charges the MEASURED open-minute half-spread (the gate the Lead measured that decides it).

WHY THIS IS THE DECIDER (lit-consistent — see journal lit entry): Berkman/Baltussen/Della Corte all
find overnight/EOD reversal is an ILLIQUID, cost-FRAGILE, OPEN-SPREAD-killed effect. The Lead measured
the open spread on the liquid-50: 09:30=12.6 / 09:33=7.5 / 09:35=6.7 / 09:40=6.0 bps HALF — 2-4x the
flat 2bps. The fade round-trips AT the open (widest spread of the day). My pre-registered prior the
low-vol-fade SURVIVES measured open cost after walk-forward = ~30% (down from 50% on the literature).

GATE 1 — WALK-FORWARD OOS: per fold, LEARN the volume-split threshold + the fade/follow DIRECTION on
  TRAIN, apply to TEST, accumulate OOS test returns. Removes the in-sample direction-picking leak.
GATE 2 — MEASURED OPEN COST: charge open_half_spread(entry_minute) on entry + close half-spread on
  exit. Sweep entry minute {09:30, 09:33, 09:35, 09:40}. NOTE/LIMITATION: the helper has the 09:30 open
  and 10:00 mark only, so the entry PRICE is fixed at the 09:30 open; the sweep varies the COST by
  minute, not the price-decay (later entry = tighter spread BUT some fade already realized — that
  tradeoff needs intra-window bars, a helper extension; flagged, not faked).

VERDICT RULE (the Lead's): low-vol-fade Sharpe POSITIVE at measured open cost AFTER walk-forward ->
  legitimate M3 candidate, escalate. NEGATIVE -> real-but-uneconomic, documented honestly, not promoted.

DEPENDS ON: research.common_daily_session_price. Liquid-tier only (TIER fixed to liquid50 — the cost
numbers are measured on those 50 names). Run from /app:
  docker compose exec -T -w /app experimenter python -m experiments.shape_gap_walkforward
"""

import json
import math
import os
import statistics
from collections import defaultdict
from datetime import date, datetime, timezone

import numpy as np
import psycopg

from quantlib.backtest import Fold, walk_forward_folds

RESULTS = os.environ.get(
    "GAP_WF_RESULTS", "/app/experiments/shape_gap_wf_results.jsonl"
)
SEED = 13
MIN_CROSS_SECTION = 20
ADV_WINDOW = 20
LEG_FRAC = 0.1
N_FOLDS = 5
HORIZON_MINUTES = (
    390  # one session — purges train labels that reach into the test block
)
PERIODS_PER_YEAR = 252.0

# Lead's MEASURED open-minute half-spread on the liquid-50 (bps). Close exit ~10:00-cadence cheap tier.
OPEN_HALF_SPREAD_BPS = {"09:30": 12.6, "09:33": 7.5, "09:35": 6.7, "09:40": 6.0}
CLOSE_HALF_SPREAD_BPS = (
    2.7  # 15:59 close exit ~ the 10:00 cadence median half (cheap tier)
)

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


def load_session_prices(
    conn: psycopg.Connection,
) -> dict[str, dict[date, tuple[float, float, float]]]:
    """Liquid-tier symbol -> {trade_date: (open_0930, vol_0930_1000, close_1600)}."""
    by_symbol: dict[str, dict[date, tuple[float, float, float]]] = defaultdict(dict)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT symbol, trade_date, open_0930, vol_0930_1000, close_1600 "
            "FROM research.common_daily_session_price "
            "WHERE open_0930 IS NOT NULL AND close_1600 IS NOT NULL AND symbol = ANY(%s) "
            "ORDER BY symbol, trade_date",
            (sorted(LIQUID_TIER),),
        )
        for symbol, trade_date, open_0930, vol, close_1600 in cur.fetchall():
            by_symbol[symbol][trade_date] = (
                float(open_0930),
                float(vol) if vol is not None else math.nan,
                float(close_1600),
            )
    return by_symbol


def cross_sectional_excess(returns: dict[str, float]) -> dict[str, float]:
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
    """(date_as_ts, symbol, gap, or_vol_z, open_to_close_excess). PIT: gap uses the prior session
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
                std_v = statistics.pstdev(baseline)
                volz_by_key[(symbol, trade_date)] = (
                    (vol - statistics.mean(baseline)) / std_v if std_v > 0 else math.nan
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


def learn_direction(gap: list[float], excess: list[float], idx: list[int]) -> float:
    """On TRAIN: sign of the gap->excess relationship. Returns +1 if FOLLOW (gap predicts same-sign
    excess), -1 if FADE (opposite). This is the OOS-honest replacement for the in-sample sort.
    """
    pairs = [
        (gap[i], excess[i])
        for i in idx
        if not (math.isnan(gap[i]) or math.isnan(excess[i]))
    ]
    if len(pairs) < 100:
        return 0.0
    gaps = [gap_value for gap_value, _ in pairs]
    excs = [exc for _, exc in pairs]
    mean_g = statistics.mean(gaps)
    mean_e = statistics.mean(excs)
    cov = sum((gap_value - mean_g) * (exc - mean_e) for gap_value, exc in pairs)
    return 1.0 if cov > 0 else -1.0


def oos_book_returns(
    gap: list[float],
    excess: list[float],
    group: list[datetime],
    symbol: list[str],
    volz: list[float],
    folds: list[Fold],
) -> tuple[dict[datetime, float], dict[datetime, float]]:
    """Walk-forward: per fold, learn the low-vol/high-vol split + direction on TRAIN, apply to TEST.
    Returns per-date GROSS book return for the low_vol and high_vol regimes (OOS test dates only).
    """
    low_returns: dict[datetime, float] = {}
    high_returns: dict[datetime, float] = {}
    for fold in folds:
        if len(fold.train_idx) < 500 or len(fold.test_idx) < 50:
            continue
        train_volz = [volz[i] for i in fold.train_idx if not math.isnan(volz[i])]
        if len(train_volz) < 100:
            continue
        volz_split = statistics.median(train_volz)
        for regime_name, in_regime, store in (
            ("low", lambda v: not math.isnan(v) and v < volz_split, low_returns),
            ("high", lambda v: not math.isnan(v) and v >= volz_split, high_returns),
        ):
            train_regime = [i for i in fold.train_idx if in_regime(volz[i])]
            direction = learn_direction(gap, excess, train_regime)
            if direction == 0.0:
                continue
            test_by_date: dict[datetime, list[tuple[float, float, str]]] = defaultdict(
                list
            )
            for i in fold.test_idx:
                if in_regime(volz[i]) and not (
                    math.isnan(gap[i]) or math.isnan(excess[i])
                ):
                    test_by_date[group[i]].append(
                        (direction * gap[i], excess[i], symbol[i])
                    )
            for ts, rows in test_by_date.items():
                rows.sort(key=lambda row: row[0])
                k = max(1, int(LEG_FRAC * len(rows)))
                if len(rows) < 2 * k:
                    continue
                shorts, longs = rows[:k], rows[-k:]
                gross = sum(exc for _, exc, _ in longs) / len(longs) - sum(
                    exc for _, exc, _ in shorts
                ) / len(shorts)
                store[ts] = gross
    return low_returns, high_returns


def book_sharpe(
    returns_by_date: dict[datetime, float], rt_cost: float
) -> dict[str, float]:
    """Net Sharpe of a per-date book return series, charging rt_cost (round-trip, in return units)
    once per date (one open->close round-trip/day). Turnover ~ 2 (full L/S flip) -> rt_cost already
    the per-rebalance cost; we model the conservative full-turnover round-trip."""
    dates = sorted(returns_by_date)
    if len(dates) < 2:
        return {"n_days": len(dates)}
    gross = [returns_by_date[d] for d in dates]
    net = [g - rt_cost for g in gross]
    mean_net = statistics.mean(net)
    std_net = statistics.stdev(net)
    return {
        "n_days": len(dates),
        "gross_mean": round(statistics.mean(gross), 6),
        "net_mean": round(mean_net, 6),
        "sharpe_net": (
            round(mean_net / std_net * math.sqrt(PERIODS_PER_YEAR), 3)
            if std_net > 0
            else math.nan
        ),
    }


def main() -> None:
    with psycopg.connect(**DB_KWARGS) as conn:
        by_symbol = load_session_prices(conn)
    if not by_symbol:
        raise SystemExit("helper table empty/missing or no liquid-tier rows.")

    ts, symbols, gap, volz, excess = build_records(by_symbol)
    n_dates = len({t.date() for t in ts})
    print(
        f"liquid-50 | {len(symbols)} (symbol,date) rows / {n_dates} dates / "
        f"{len(set(symbols))} symbols",
        flush=True,
    )

    folds = walk_forward_folds(ts, HORIZON_MINUTES, N_FOLDS)
    low_returns, high_returns = oos_book_returns(gap, excess, ts, symbols, volz, folds)
    print(
        f"OOS test dates: low_vol={len(low_returns)} high_vol={len(high_returns)}",
        flush=True,
    )

    # LEAKAGE CANARY: shuffle the open_to_close excess WITHIN each date, re-run the FULL walk-forward
    # pipeline. A +2.6 net Sharpe demands this — if the OOS edge persists on shuffled labels, there is
    # a structural leak; it must collapse to ~0 for the result to be trusted.
    rng = np.random.default_rng(SEED)
    by_date_idx: dict[datetime, list[int]] = defaultdict(list)
    for i, t in enumerate(ts):
        by_date_idx[t].append(i)
    shuffled_excess = list(excess)
    for idxs in by_date_idx.values():
        values = [excess[i] for i in idxs]
        rng.shuffle(values)
        for i, value in zip(idxs, values):
            shuffled_excess[i] = value
    canary_low, _canary_high = oos_book_returns(
        gap, shuffled_excess, ts, symbols, volz, folds
    )
    canary_low_sharpe = book_sharpe(canary_low, 0.0).get("sharpe_net")
    print(
        f"CANARY (shuffled excess) low_vol gross OOS Sharpe: {canary_low_sharpe}",
        flush=True,
    )

    # OOS IC sanity (within-test-date) on the pooled OOS rows.
    records: list[dict[str, object]] = []
    for regime_name, returns_by_date in (
        ("low_vol", low_returns),
        ("high_vol", high_returns),
    ):
        gross_only = book_sharpe(returns_by_date, 0.0)
        print(f"\n=== {regime_name} (walk-forward OOS) ===", flush=True)
        print(
            f"  gross: n_days={gross_only.get('n_days')} "
            f"mean={gross_only.get('gross_mean')} sharpe={gross_only.get('sharpe_net')}",
            flush=True,
        )
        cost_by_minute = {}
        for entry_minute, open_half in OPEN_HALF_SPREAD_BPS.items():
            # round-trip cost = open entry half-spread + close exit half-spread, in return units.
            rt_cost = (open_half + CLOSE_HALF_SPREAD_BPS) / 1e4
            net = book_sharpe(returns_by_date, rt_cost)
            cost_by_minute[entry_minute] = {
                "rt_cost_bps": round((open_half + CLOSE_HALF_SPREAD_BPS), 1),
                "sharpe_net": net.get("sharpe_net"),
                "net_mean": net.get("net_mean"),
            }
            print(
                f"  entry {entry_minute} (RT {open_half + CLOSE_HALF_SPREAD_BPS:.1f}bps) -> "
                f"net Sharpe {net.get('sharpe_net')} (net mean {net.get('net_mean')})",
                flush=True,
            )
        records.append(
            {
                "run_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "regime": regime_name,
                "oos_test_dates": gross_only.get("n_days"),
                "gross_sharpe": gross_only.get("sharpe_net"),
                "gross_mean": gross_only.get("gross_mean"),
                "canary_gross_sharpe": (
                    canary_low_sharpe if regime_name == "low_vol" else None
                ),
                "cost_by_entry_minute": cost_by_minute,
            }
        )

    with open(RESULTS, "a") as results_file:
        for result in records:
            results_file.write(json.dumps(result) + "\n")
    print(f"\nwrote {len(records)} records to {RESULTS}", flush=True)


if __name__ == "__main__":
    main()
