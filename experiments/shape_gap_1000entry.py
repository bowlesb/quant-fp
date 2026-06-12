"""Shape 002 SECOND ARM — 10:00-entry gap-fade + liquidity-tier targeting (explorer-shapes).

explorer-data archaeology (report 2026-06-12_explorer-data_open-gap-fade-archaeology.md) gave two
upgrades to the gap-fade candidate, both built here:

1. **10:00-ENTRY ARM** — the gap-fade signal persists ~83% to 60m (IC -0.072 at open -> -0.060 at 60m).
   So enter at 10:00 (px_1000) instead of the 09:30 open, using the SAME gap signal, to dodge the
   toxic open spread (6-12bps half) and pay the cheap 10:00-cadence spread (~2.7bps half) instead.
   Trade-off: you forfeit the first 30 min of fade. This is the arm MOST LIKELY to clear cost — the
   whole 002 question was the open spread, and this sidesteps it. Label here = px_1000 -> close_1600
   (ten_to_close), cross-sectionally demeaned. Signal = the overnight gap (known pre-open, PIT-clean).
   Compared head-to-head with the 09:30-entry arm (open -> close) on the SAME dates.

2. **LIQUIDITY-TIER TARGETING** — explorer-data found an INVERTED-U: fade strongest at liq2/liq3
   (mid-liquidity, IC -0.095/-0.089), weakest at mega-cap (liq4, -0.038). So the most-liquid-50 cut may
   be the WEAKEST tier. If research.common_liquidity_tier exists (builder delivered), break results out
   by liq_q and target liq2/liq3; else run the liquid-50 baseline only (graceful).

GATES: walk-forward OOS (learn direction per-fold on TRAIN, apply to TEST) + shuffle canary. Net-of-cost:
  09:30 arm charges the measured open half-spread; 10:00 arm charges the cheap 10:00-cadence half-spread.

PRE-REGISTERED QUESTION: does the 10:00-entry arm clear cost MORE cleanly than the 09:30 arm (cheaper
spread but less fade captured)? And is the fade stronger on liq2/liq3 than liquid-50, per the inverted-U?
Pre-registered prior: ~55% the 10:00 arm has a HIGHER net Sharpe than 09:30 (the spread saving ~9bps RT
likely outweighs the fade given up, since 83% of the fade persists past 10:00).

DEPENDS ON: research.common_daily_session_price (+ optionally research.common_liquidity_tier). Run:
  docker compose exec -T -w /app experimenter python -m experiments.shape_gap_1000entry
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
    "GAP_1000_RESULTS", "/app/experiments/shape_gap_1000_results.jsonl"
)
SEED = 13
MIN_CROSS_SECTION = 20
ADV_WINDOW = 20
LEG_FRAC = 0.1
N_FOLDS = 5
N_CANARY = (
    20  # multi-seed shuffle canary — characterize the null band, not one lucky draw
)
HORIZON_MINUTES = 390
PERIODS_PER_YEAR = 252.0

# Round-trip cost per arm (half-spread entry + half-spread exit, bps). 09:30 entry = toxic open spread
# (Lead measured ~12.6 half @09:30); 10:00 entry = cheap cadence (~2.7 half). Both exit at 15:59 (~2.7).
OPEN_ENTRY_RT_BPS = 12.6 + 2.7  # 09:30 open entry + close exit
TEN_ENTRY_RT_BPS = 2.7 + 2.7  # 10:00 entry + close exit

# liquid-50 fallback when the liquidity-tier table isn't present.
LIQUID_TIER_50 = {
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


def load_liquidity_tiers(conn: psycopg.Connection) -> dict[str, int]:
    """symbol -> liq_q (1..4) from research.common_liquidity_tier if it exists; else empty."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
            "WHERE table_schema='research' AND table_name='common_liquidity_tier')"
        )
        exists = cur.fetchone()[0]
        if not exists:
            return {}
        cur.execute("SELECT symbol, liq_q FROM research.common_liquidity_tier")
        return {symbol: int(liq_q) for symbol, liq_q in cur.fetchall()}


def load_session_prices(
    conn: psycopg.Connection,
) -> dict[str, dict[date, tuple[float, float, float, float]]]:
    """symbol -> {date: (open_0930, px_1000, vol_0930_1000, close_1600)}; skip NULL open/1000/close."""
    by_symbol: dict[str, dict[date, tuple[float, float, float, float]]] = defaultdict(
        dict
    )
    with conn.cursor() as cur:
        cur.execute(
            "SELECT symbol, trade_date, open_0930, px_1000, vol_0930_1000, close_1600 "
            "FROM research.common_daily_session_price "
            "WHERE open_0930 IS NOT NULL AND px_1000 IS NOT NULL AND close_1600 IS NOT NULL "
            "ORDER BY symbol, trade_date"
        )
        for symbol, trade_date, open_0930, px_1000, vol, close_1600 in cur.fetchall():
            by_symbol[symbol][trade_date] = (
                float(open_0930),
                float(px_1000),
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
    by_symbol: dict[str, dict[date, tuple[float, float, float, float]]],
) -> tuple[
    list[datetime], list[str], list[float], list[float], list[float], list[float]
]:
    """(date_as_ts, symbol, gap, or_vol_z, open_to_close_excess, ten_to_close_excess).
    Two labels: open->close (09:30 arm) and 10:00->close (10:00 arm), each cross-sectionally demeaned.
    """
    raw_oc: dict[date, dict[str, float]] = defaultdict(dict)
    raw_tc: dict[date, dict[str, float]] = defaultdict(dict)
    gap_by_key: dict[tuple[str, date], float] = {}
    volz_by_key: dict[tuple[str, date], float] = {}
    for symbol, by_date in by_symbol.items():
        sessions = sorted(by_date)
        vol_history: list[float] = []
        for i, trade_date in enumerate(sessions):
            open_0930, px_1000, vol, close_1600 = by_date[trade_date]
            raw_oc[trade_date][symbol] = (
                close_1600 / open_0930 - 1.0 if open_0930 else math.nan
            )
            raw_tc[trade_date][symbol] = (
                close_1600 / px_1000 - 1.0 if px_1000 else math.nan
            )
            if i > 0:
                prior_close = by_date[sessions[i - 1]][3]
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
    out_oc: list[float] = []
    out_tc: list[float] = []
    for trade_date in raw_oc:
        oc_excess = cross_sectional_excess(raw_oc[trade_date])
        tc_excess = cross_sectional_excess(raw_tc[trade_date])
        as_ts = datetime(
            trade_date.year, trade_date.month, trade_date.day, tzinfo=timezone.utc
        )
        for symbol in oc_excess:
            out_ts.append(as_ts)
            out_sym.append(symbol)
            out_gap.append(gap_by_key[(symbol, trade_date)])
            out_volz.append(volz_by_key[(symbol, trade_date)])
            out_oc.append(oc_excess[symbol])
            out_tc.append(tc_excess[symbol])
    return out_ts, out_sym, out_gap, out_volz, out_oc, out_tc


def learn_direction(gap: list[float], excess: list[float], idx: list[int]) -> float:
    pairs = [
        (gap[i], excess[i])
        for i in idx
        if not (math.isnan(gap[i]) or math.isnan(excess[i]))
    ]
    if len(pairs) < 100:
        return 0.0
    mean_g = statistics.mean([g for g, _ in pairs])
    mean_e = statistics.mean([e for _, e in pairs])
    cov = sum((g - mean_g) * (e - mean_e) for g, e in pairs)
    return 1.0 if cov > 0 else -1.0


def oos_low_vol_returns(
    gap: list[float],
    excess: list[float],
    group: list[datetime],
    volz: list[float],
    folds: list[Fold],
) -> dict[datetime, float]:
    """Walk-forward OOS per-date gross book return for the LOW-VOL fade cohort (the surviving regime).
    Direction + volz-split learned on TRAIN, applied to TEST."""
    out: dict[datetime, float] = {}
    for fold in folds:
        if len(fold.train_idx) < 500 or len(fold.test_idx) < 50:
            continue
        train_volz = [volz[i] for i in fold.train_idx if not math.isnan(volz[i])]
        if len(train_volz) < 100:
            continue
        split = statistics.median(train_volz)
        train_low = [
            i for i in fold.train_idx if not math.isnan(volz[i]) and volz[i] < split
        ]
        direction = learn_direction(gap, excess, train_low)
        if direction == 0.0:
            continue
        test_by_date: dict[datetime, list[tuple[float, float]]] = defaultdict(list)
        for i in fold.test_idx:
            if (
                not math.isnan(volz[i])
                and volz[i] < split
                and not (math.isnan(gap[i]) or math.isnan(excess[i]))
            ):
                test_by_date[group[i]].append((direction * gap[i], excess[i]))
        for ts, rows in test_by_date.items():
            rows.sort(key=lambda row: row[0])
            k = max(1, int(LEG_FRAC * len(rows)))
            if len(rows) < 2 * k:
                continue
            longs = rows[-k:]
            shorts = rows[:k]
            gross = sum(exc for _, exc in longs) / len(longs) - sum(
                exc for _, exc in shorts
            ) / len(shorts)
            out[ts] = gross
    return out


def net_sharpe(
    returns_by_date: dict[datetime, float], rt_cost_bps: float
) -> dict[str, float]:
    dates = sorted(returns_by_date)
    if len(dates) < 2:
        return {"n_days": len(dates)}
    rt = rt_cost_bps / 1e4
    net = [returns_by_date[d] - rt for d in dates]
    gross = [returns_by_date[d] for d in dates]
    std = statistics.stdev(net)
    return {
        "n_days": len(dates),
        "gross_sharpe": (
            round(
                statistics.mean(gross)
                / statistics.stdev(gross)
                * math.sqrt(PERIODS_PER_YEAR),
                3,
            )
            if statistics.stdev(gross) > 0
            else math.nan
        ),
        "net_sharpe": (
            round(statistics.mean(net) / std * math.sqrt(PERIODS_PER_YEAR), 3)
            if std > 0
            else math.nan
        ),
        "net_mean": round(statistics.mean(net), 6),
    }


def shuffle_within_date(
    excess: list[float], group: list[datetime], seed: int
) -> list[float]:
    rng = np.random.default_rng(seed)
    by_date: dict[datetime, list[int]] = defaultdict(list)
    for i, ts in enumerate(group):
        by_date[ts].append(i)
    out = list(excess)
    for idxs in by_date.values():
        values = [excess[i] for i in idxs]
        rng.shuffle(values)
        for i, value in zip(idxs, values):
            out[i] = value
    return out


def run_cohort(
    label: str,
    ts: list[datetime],
    gap: list[float],
    volz: list[float],
    oc_excess: list[float],
    tc_excess: list[float],
    records: list[dict[str, object]],
) -> None:
    folds = walk_forward_folds(ts, HORIZON_MINUTES, N_FOLDS)
    # 09:30 arm (open->close) vs 10:00 arm (10:00->close), low-vol fade, walk-forward OOS.
    oc_oos = oos_low_vol_returns(gap, oc_excess, ts, volz, folds)
    tc_oos = oos_low_vol_returns(gap, tc_excess, ts, volz, folds)

    # MULTI-SEED canary: a single shuffle on ~517 dates is noisy (one seed gave -0.72, another +1.29
    # on the SAME data). Run N_CANARY shuffles, report the null DISTRIBUTION (mean + max) so the real
    # gross Sharpe is judged against the actual null band, not one lucky draw.
    oc_canary_sharpes = [
        net_sharpe(
            oos_low_vol_returns(
                gap, shuffle_within_date(oc_excess, ts, SEED + s), ts, volz, folds
            ),
            0.0,
        ).get("gross_sharpe")
        for s in range(N_CANARY)
    ]
    oc_canary_clean = [
        v for v in oc_canary_sharpes if v is not None and not math.isnan(v)
    ]
    canary_mean = (
        round(statistics.mean(oc_canary_clean), 3) if oc_canary_clean else math.nan
    )
    canary_max = round(max(oc_canary_clean), 3) if oc_canary_clean else math.nan
    canary_std = (
        round(statistics.pstdev(oc_canary_clean), 3)
        if len(oc_canary_clean) > 1
        else math.nan
    )

    oc_net = net_sharpe(oc_oos, OPEN_ENTRY_RT_BPS)
    tc_net = net_sharpe(tc_oos, TEN_ENTRY_RT_BPS)
    print(f"\n=== {label} (walk-forward OOS, low-vol fade) ===", flush=True)
    print(
        f"  09:30 arm: gross {oc_net.get('gross_sharpe')} | net@{OPEN_ENTRY_RT_BPS:.1f}bps "
        f"{oc_net.get('net_sharpe')} | n={oc_net.get('n_days')}",
        flush=True,
    )
    print(
        f"  10:00 arm: gross {tc_net.get('gross_sharpe')} | net@{TEN_ENTRY_RT_BPS:.1f}bps "
        f"{tc_net.get('net_sharpe')} | n={tc_net.get('n_days')}",
        flush=True,
    )
    print(
        f"  CANARY null ({N_CANARY} shuffles): mean {canary_mean} / std {canary_std} / max "
        f"{canary_max}  -> real gross {oc_net.get('gross_sharpe')} is "
        f"{'ABOVE' if (oc_net.get('gross_sharpe') or 0) > canary_max else 'WITHIN'} the null band",
        flush=True,
    )
    records.append(
        {
            "run_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "cohort": label,
            "arm_0930_gross_sharpe": oc_net.get("gross_sharpe"),
            "arm_0930_net_sharpe": oc_net.get("net_sharpe"),
            "arm_0930_rt_bps": OPEN_ENTRY_RT_BPS,
            "arm_1000_gross_sharpe": tc_net.get("gross_sharpe"),
            "arm_1000_net_sharpe": tc_net.get("net_sharpe"),
            "arm_1000_rt_bps": TEN_ENTRY_RT_BPS,
            "canary_null_mean": canary_mean,
            "canary_null_std": canary_std,
            "canary_null_max": canary_max,
            "n_oos_dates": tc_net.get("n_days"),
        }
    )


def main() -> None:
    with psycopg.connect(**DB_KWARGS) as conn:
        tiers = load_liquidity_tiers(conn)
        by_symbol = load_session_prices(conn)
    if not by_symbol:
        raise SystemExit("helper table empty/missing.")

    records: list[dict[str, object]] = []

    # Cohort 1: liquid-50 (baseline, comparable to the original verdict-gate run).
    liquid50 = {sym: rows for sym, rows in by_symbol.items() if sym in LIQUID_TIER_50}
    ts, sym, gap, volz, oc, tc = build_records(liquid50)
    print(f"liquid-50 | {len(sym)} rows / {len(set(sym))} symbols", flush=True)
    run_cohort("liquid50", ts, gap, volz, oc, tc, records)

    # Cohorts 2-3: liq2 and liq3 (explorer-data's inverted-U sweet spot) — only if the tier table landed.
    if tiers:
        for liq_q in (2, 3):
            cohort = {
                sym: rows for sym, rows in by_symbol.items() if tiers.get(sym) == liq_q
            }
            if len(cohort) < 20:
                print(f"liq{liq_q}: {len(cohort)} symbols — SKIP", flush=True)
                continue
            ts, sym, gap, volz, oc, tc = build_records(cohort)
            print(
                f"\nliq{liq_q} | {len(sym)} rows / {len(set(sym))} symbols", flush=True
            )
            run_cohort(f"liq{liq_q}", ts, gap, volz, oc, tc, records)
    else:
        print(
            "\nresearch.common_liquidity_tier NOT present — ran liquid-50 only. "
            "Run builders/common_liquidity_tier.sql to unblock the liq2/liq3 inverted-U cut.",
            flush=True,
        )

    with open(RESULTS, "a") as results_file:
        for result in records:
            results_file.write(json.dumps(result) + "\n")
    print(f"\nwrote {len(records)} records to {RESULTS}", flush=True)


if __name__ == "__main__":
    main()
