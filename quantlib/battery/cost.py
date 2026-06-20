"""Per-name half-spread L/S cost model (trap #1).

`quantlib.backtest.long_short_backtest` charges a single flat one-way cost. The lesson from B4
is that a flat 2bp HIDES the illiquid-tail trap: the edge that survives a flat 2bp dies under the
realistic per-name spread. So the battery charges each name its OWN half-spread (from the Panel's
`half_spread_bps` column) on its realized turnover, and also produces the `cost_curve` — net P&L
vs a sweep of one-way cost multipliers — so "where does the edge die?" is visible at a glance.

The basket construction (dollar-neutral EW top/bottom-frac per timestamp) mirrors
`long_short_backtest` exactly so this is the SAME mechanism, only with a per-name cost.
"""
from __future__ import annotations

import math
import statistics
from collections import defaultdict


def long_short_per_name_cost(
    pred: list[float],
    realized: list[float],
    group: list,
    symbol: list[str],
    half_spread_bps: list[float],
    *,
    frac: float = 0.1,
    cost_mult: float = 1.0,
    borrow_bps_annual: float = 50.0,
    periods_per_year: float = 3276.0,
) -> dict[str, float]:
    """Dollar-neutral EW top/bottom-`frac` L/S basket, charging each name its OWN one-way
    half-spread (x `cost_mult`) on its realized turnover. Returns gross/net per period, after-cost
    Sharpe, hit-rate, mean turnover, and the breakeven one-way cost in bps."""
    buckets: dict[object, list[tuple[float, float, str, float]]] = defaultdict(list)
    for prediction, ret, grp, sym, spread in zip(pred, realized, group, symbol, half_spread_bps):
        if not (math.isnan(prediction) or math.isnan(ret)):
            spread_ok = spread if (spread == spread) else 0.0
            buckets[grp].append((prediction, ret, sym, spread_ok))
    borrow_per_period = (borrow_bps_annual / 1e4) / periods_per_year
    gross_list: list[float] = []
    net_list: list[float] = []
    turn_list: list[float] = []
    prev_w: dict[str, float] = {}
    spread_by_sym: dict[str, float] = {}
    for ts in sorted(buckets):
        rows = sorted(buckets[ts], key=lambda row: row[0])
        k = max(1, int(frac * len(rows)))
        if len(rows) < 2 * k:
            continue
        shorts, longs = rows[:k], rows[-k:]
        weights: dict[str, float] = {}
        for _, _, sym, spread in longs:
            weights[sym] = weights.get(sym, 0.0) + 1.0 / len(longs)
            spread_by_sym[sym] = spread
        for _, _, sym, spread in shorts:
            weights[sym] = weights.get(sym, 0.0) - 1.0 / len(shorts)
            spread_by_sym[sym] = spread
        gross = sum(weights[sym] * ret for _, ret, sym, _ in longs + shorts)
        # per-name cost on the change in each name's weight (entry + exit both pay the half-spread)
        traded = set(weights) | set(prev_w)
        cost = 0.0
        turnover = 0.0
        for sym in traded:
            dw = abs(weights.get(sym, 0.0) - prev_w.get(sym, 0.0))
            turnover += dw
            spread = spread_by_sym.get(sym, 0.0) * cost_mult / 1e4
            cost += spread * dw
        net = gross - cost - borrow_per_period
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
    hits = sum(1 for net in net_list if net > 0)
    # breakeven one-way cost (bps): gross / turnover, the spread the signal can absorb.
    breakeven = (mean_gross / mean_turn * 1e4) if mean_turn > 0 else math.nan
    return {
        "n_periods": len(net_list),
        "gross_per_period": mean_gross,
        "net_per_period": mean_net,
        "sharpe_net": (mean_net / std_net * math.sqrt(periods_per_year)) if std_net > 0 else math.nan,
        "mean_turnover": mean_turn,
        "hit_rate": hits / len(net_list),
        "breakeven_cost_bps": breakeven,
    }


def cost_curve(
    pred: list[float],
    realized: list[float],
    group: list,
    symbol: list[str],
    half_spread_bps: list[float],
    *,
    frac: float = 0.1,
    periods_per_year: float = 3276.0,
    multipliers: tuple[float, ...] = (0.0, 0.5, 1.0, 1.5, 2.0, 3.0),
) -> list[tuple[float, float]]:
    """(cost_mult, net_per_period) over a sweep of per-name-cost multipliers — where the edge dies."""
    curve: list[tuple[float, float]] = []
    for mult in multipliers:
        result = long_short_per_name_cost(
            pred,
            realized,
            group,
            symbol,
            half_spread_bps,
            frac=frac,
            cost_mult=mult,
            periods_per_year=periods_per_year,
        )
        curve.append((mult, result.get("net_per_period", math.nan)))
    return curve
