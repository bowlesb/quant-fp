"""Shape 001 — CONDITIONAL PARTICIPATION on the existing 30m signal (explorer-shapes).

THE SHAPE (not a new feature/model — a PARTICIPATION RULE wrapped around the existing signal):
the 30m cross-sectional signal has real raw IC but dies net-of-cost because we trade every name
every timestamp. Hypothesis: gate participation to (a) HIGH-CONVICTION name-timestamps (|prediction|
extremity) and (b) the cheap LIQUID tier, so turnover collapses and the average traded name is
cheap -> the same signal can cross net-of-cost positive even though the full-breadth version is -ve.
(A) low-turnover + (B) sparse + (C) optional long-only. The deliverable is the
PARTICIPATION-vs-NET-SHARPE FRONTIER, not a single cell.

PRE-REGISTERED TENSION (handed by the Lead — this is the make-or-break):
  - task #5: the signal is ~0 on the liquid-50 tier (IC -0.0035 vs +0.023 full panel).
  - explorer-data: ret_5m is a REVERSAL concentrated in ILLIQUID names.
  -> The cheap-tier gate may remove the SIGNAL, not just the cost. So this script SEPARATES the two
     gates: it sweeps the CONVICTION gate on the FULL panel (where the signal lives) AND on the
     liquid tier (where it's cheap), so we can SEE whether conviction-gating rescues net-Sharpe
     anywhere, and whether the liquid-tier signal-loss kills it. The conviction-gate axis itself is
     UNTESTED — that is the new knowledge, regardless of the tier outcome.

PRE-REGISTERED FALSIFIER: if net-of-cost Sharpe is <= 0 (or no better than the full-breadth -ve
baseline) across EVERY (conviction x tier) cell — sparsity + cheap-tier gating does NOT lift it
above breakeven — the turnover-not-signal rescue is dead for this signal. Pre-registered prior ~40%.
12-cell grid -> the Lead judges the FRONTIER (multiple-testing aware), not a lucky cell.

Run as a module from /app (mirrors cost_liquidity_tier.py):
  docker compose exec -T -w /app -e SET_VERSION=v1.1.1 experimenter python -m experiments.shape_conditional_participation
  docker compose exec -T -w /app -e SET_VERSION=v1.1.1 -e SMOKE_DAYS=200 experimenter python -m experiments.shape_conditional_participation
"""

import json
import math
import os
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone

import numpy as np
import psycopg

from quantlib.backtest import mean_ic, per_timestamp_ic, shuffle_within_groups
from quantlib.research import load_panel

from experiments.battery import collect_oos, filter_smoke, per_symbol_demean

SET_VERSION = os.environ.get("SET_VERSION", "v1.1.1")
SMOKE_DAYS = int(os.environ.get("SMOKE_DAYS", "0"))
RESULTS = os.environ.get(
    "PARTICIPATION_RESULTS", "/app/experiments/shape_participation_results.jsonl"
)
FORBIDDEN_VERSIONS = {"v1.0.0", "v1.1.0"}
MIN_ROWS = 1000
SEED = 13

# The 50 OFI-capture names = the liquid tier (same list as cost_liquidity_tier.py).
LIQUID_TIER = [
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
]
SIGNAL_FEATURES = [
    "ret_5m",
    "vwap_dev",
    "range_pct",
    "gap_from_open",
]  # the carrier set (task #5)
# Conviction gate: keep only name-timestamps whose |rank-centered prediction| is in the top
# CONVICTION_FRAC of its timestamp. 1.0 = trade everyone (baseline); 0.05 = only top-5% conviction.
CONVICTION_FRACS = [1.0, 0.5, 0.3, 0.2, 0.1, 0.05]
# Net-of-cost: per-name measured half-spread sweep bracketing the liquid tier (task #5 distribution).
COST_SWEEP_BPS = [1.4, 2.0, 2.7]
LEG_FRAC = 0.1  # L/S decile width within the gated subset

DB_KWARGS = {
    "host": os.environ["DB_HOST"],
    "port": int(os.environ.get("DB_PORT", "5432")),
    "dbname": os.environ["DB_NAME"],
    "user": os.environ["DB_USER"],
    "password": os.environ["DB_PASSWORD"],
}


def rank_center_by_ts(preds: list[float], group: list[datetime]) -> list[float]:
    """Per-timestamp rank-centered conviction in [-1, 1]: rank within the timestamp, center on the
    median, scale to [-1, 1]. |value| near 1 = an extreme (high-conviction) name that timestamp.
    """
    by_ts: dict[datetime, list[int]] = defaultdict(list)
    for i, ts in enumerate(group):
        by_ts[ts].append(i)
    centered = [0.0] * len(preds)
    for ts, idxs in by_ts.items():
        order = sorted(idxs, key=lambda i: preds[i])
        n = len(order)
        if n < 2:
            centered[order[0]] = 0.0
            continue
        for rank_pos, i in enumerate(order):
            # rank_pos in [0, n-1] -> [-1, 1]
            centered[i] = 2.0 * rank_pos / (n - 1) - 1.0
    return centered


def gated_long_short(
    preds: list[float],
    realized: list[float],
    group: list[datetime],
    symbol: list[str],
    conviction: list[float],
    conviction_frac: float,
    *,
    leg_frac: float,
    cost_bps_oneway: float,
    periods_per_year: float,
    long_only: bool,
) -> dict[str, float]:
    """L/S (or long-only) backtest on the CONVICTION-GATED subset. Within each timestamp, keep only
    names whose |conviction| is in the top `conviction_frac`; among those, take the top/bottom
    `leg_frac` as the book. Charges one-way cost on realized turnover. Reports net Sharpe, turnover,
    breakeven, AND the realized PARTICIPATION RATE (the selling point of a sparse rule).
    """
    buckets: dict[datetime, list[tuple[float, float, float, str]]] = defaultdict(list)
    for pred_value, realized_ret, ts, sym, conv in zip(
        preds, realized, group, symbol, conviction
    ):
        if not (math.isnan(pred_value) or math.isnan(realized_ret)):
            buckets[ts].append((pred_value, realized_ret, conv, sym))
    cost = cost_bps_oneway / 1e4
    gross_list: list[float] = []
    net_list: list[float] = []
    turn_list: list[float] = []
    n_traded = 0
    n_eligible = 0
    prev_w: dict[str, float] = {}
    for ts in sorted(buckets):
        rows = buckets[ts]
        n_eligible += len(rows)
        # conviction gate: keep the top conviction_frac by |conviction|
        keep_n = max(2, int(conviction_frac * len(rows)))
        gated = sorted(rows, key=lambda row: abs(row[2]), reverse=True)[:keep_n]
        gated.sort(key=lambda row: row[0])  # ascending prediction for leg selection
        k = max(1, int(leg_frac * len(gated)))
        if len(gated) < 2 * k:
            continue
        longs = gated[-k:]
        shorts = [] if long_only else gated[:k]
        weights: dict[str, float] = {}
        for _, _, _, sym in longs:
            weights[sym] = weights.get(sym, 0.0) + 1.0 / len(longs)
        if shorts:
            for _, _, _, sym in shorts:
                weights[sym] = weights.get(sym, 0.0) - 1.0 / len(shorts)
        book = longs + shorts
        n_traded += len(book)
        gross = sum(weights[sym] * realized_ret for _, realized_ret, _, sym in book)
        turnover = sum(
            abs(weights.get(sym, 0.0) - prev_w.get(sym, 0.0))
            for sym in set(weights) | set(prev_w)
        )
        net = gross - cost * turnover
        gross_list.append(gross)
        net_list.append(net)
        turn_list.append(turnover)
        prev_w = weights
    if len(net_list) < 2:
        return {"n_periods": len(net_list)}
    mean_gross = statistics.mean(gross_list)
    mean_net = statistics.mean(net_list)
    std_net = statistics.stdev(net_list)
    mean_turn = statistics.mean(turn_list)
    return {
        "n_periods": len(net_list),
        "participation_rate": (
            round(n_traded / n_eligible, 4) if n_eligible else math.nan
        ),
        "gross_per_period": round(mean_gross, 6),
        "net_per_period": round(mean_net, 6),
        "sharpe_net": (
            round(mean_net / std_net * math.sqrt(periods_per_year), 3)
            if std_net > 0
            else math.nan
        ),
        "mean_turnover": round(mean_turn, 3),
        "breakeven_cost_bps": (
            round(mean_gross / mean_turn * 1e4, 2) if mean_turn > 0 else math.nan
        ),
    }


def main() -> None:
    if SET_VERSION in FORBIDDEN_VERSIONS:
        sys.exit(f"REFUSING SET_VERSION={SET_VERSION}: dirty labels. Use v1.1.1.")
    horizon, horizon_minutes, cadence_min = "fwd_30m", 30, 30
    periods_per_year = 252.0 * (390.0 / cadence_min)
    records: list[dict[str, object]] = []

    with psycopg.connect(**DB_KWARGS) as conn:
        names, ts, symbols, X, y = load_panel(conn, horizon, SET_VERSION)
    if SMOKE_DAYS:
        ts, symbols, X, y = filter_smoke(ts, symbols, X, y, SMOKE_DAYS)
    feat_idx = [names.index(name) for name in SIGNAL_FEATURES]
    vol_scaler = X[:, names.index("vol_30m")]

    for tier_name, tier in [("full_panel", None), ("liquid50", LIQUID_TIER)]:
        if tier is None:
            t_ts, t_sym, t_X, t_y, t_vol = ts, symbols, X, y, vol_scaler
        else:
            keep = set(tier)
            idx = [i for i, sym in enumerate(symbols) if sym in keep]
            t_ts = [ts[i] for i in idx]
            t_sym = [symbols[i] for i in idx]
            t_X, t_y, t_vol = X[idx], y[idx], vol_scaler[idx]
        if len(t_y) < MIN_ROWS:
            print(f"SKIP {tier_name}: {len(t_y)} rows < {MIN_ROWS}", flush=True)
            continue
        Xs = t_X[:, feat_idx]

        preds, realized, pred_ts, pred_sym = collect_oos(
            Xs, t_y, t_y, t_ts, t_sym, "rank", t_vol, horizon_minutes
        )
        gross_ic = per_timestamp_ic(preds, realized, pred_ts)
        conviction = rank_center_by_ts(preds, pred_ts)

        # canary: shuffle labels, re-derive predictions+conviction, confirm gated edge collapses.
        shuffled = np.asarray(shuffle_within_groups(list(t_y), t_ts, SEED), dtype=float)
        c_preds, c_real, c_ts, c_sym = collect_oos(
            Xs, shuffled, t_y, t_ts, t_sym, "rank", t_vol, horizon_minutes
        )
        c_conv = rank_center_by_ts(c_preds, c_ts)
        neutral_preds = per_symbol_demean(preds, pred_sym)

        print(
            f"\n=== {tier_name} | {len(set(t_sym))} names | {len(t_y)} rows | "
            f"gross IC={mean_ic(gross_ic):+.5f} ===",
            flush=True,
        )

        for conviction_frac in CONVICTION_FRACS:
            for long_only in (False, True):
                base_cost = 2.0
                gated = gated_long_short(
                    preds,
                    realized,
                    pred_ts,
                    pred_sym,
                    conviction,
                    conviction_frac,
                    leg_frac=LEG_FRAC,
                    cost_bps_oneway=base_cost,
                    periods_per_year=periods_per_year,
                    long_only=long_only,
                )
                if gated.get("n_periods", 0) < 2:
                    continue
                # survivorship-neutral + canary on the same gate
                neutral = gated_long_short(
                    neutral_preds,
                    realized,
                    pred_ts,
                    pred_sym,
                    conviction,
                    conviction_frac,
                    leg_frac=LEG_FRAC,
                    cost_bps_oneway=base_cost,
                    periods_per_year=periods_per_year,
                    long_only=long_only,
                )
                canary = gated_long_short(
                    c_preds,
                    c_real,
                    c_ts,
                    c_sym,
                    c_conv,
                    conviction_frac,
                    leg_frac=LEG_FRAC,
                    cost_bps_oneway=base_cost,
                    periods_per_year=periods_per_year,
                    long_only=long_only,
                )
                cost_sweep = {}
                for cost in COST_SWEEP_BPS:
                    bt = gated_long_short(
                        preds,
                        realized,
                        pred_ts,
                        pred_sym,
                        conviction,
                        conviction_frac,
                        leg_frac=LEG_FRAC,
                        cost_bps_oneway=cost,
                        periods_per_year=periods_per_year,
                        long_only=long_only,
                    )
                    cost_sweep[f"cost_{cost}"] = bt.get("sharpe_net")
                mode = "long_only" if long_only else "long_short"
                print(
                    f"  conv={conviction_frac:<4} {mode:<10} "
                    f"part={gated.get('participation_rate')} turn={gated.get('mean_turnover')} "
                    f"breakeven={gated.get('breakeven_cost_bps')}bps "
                    f"sharpe@2bps={gated.get('sharpe_net')} "
                    f"surv-neutral={neutral.get('sharpe_net')} canary={canary.get('sharpe_net')}",
                    flush=True,
                )
                records.append(
                    {
                        "run_at": datetime.now(timezone.utc).isoformat(
                            timespec="seconds"
                        ),
                        "tier": tier_name,
                        "conviction_frac": conviction_frac,
                        "mode": mode,
                        "participation_rate": gated.get("participation_rate"),
                        "mean_turnover": gated.get("mean_turnover"),
                        "breakeven_cost_bps": gated.get("breakeven_cost_bps"),
                        "sharpe_net_2bps": gated.get("sharpe_net"),
                        "surv_neutral_sharpe": neutral.get("sharpe_net"),
                        "canary_sharpe": canary.get("sharpe_net"),
                        "cost_sweep": cost_sweep,
                        "set_version": SET_VERSION,
                    }
                )

    with open(RESULTS, "a") as f:
        for result in records:
            f.write(json.dumps(result) + "\n")
    print(f"\nwrote {len(records)} records to {RESULTS}", flush=True)


if __name__ == "__main__":
    main()
