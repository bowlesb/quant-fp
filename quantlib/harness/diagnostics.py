"""The PERCENTILE-THRESHOLD diagnostic curve — Ben's headline ("more diagnostic than AUC, conservative
thresholds of a percentile").

For each percentile cut in {1, 2, 5, 10, 20, 33, 50}% the harness books a dollar-neutral L/S basket
that goes LONG the top-cut% model scores and SHORT the bottom-cut% per timestamp, and reports, net of
per-name cost:

  - directional PRECISION  : P(the score predicts the correct sign of the forward excess return), over
                             both legs (long expects +, short expects -). The conservative-application
                             question is "as the cut shrinks (more selective), does precision rise?".
  - mean_fwd_return        : mean forward excess return of the selected names (sign-aligned: + for the
                             long leg, the negative of the short leg's return, so a profitable short
                             counts positively).
  - dollar_per_trade       : the $ P&L attributable to one name-period at this cut on the book capital.
  - total_dollar_pnl       : the cumulative $ P&L of the L/S basket at this cut, on the book capital.
  - n_trades               : the number of name-periods traded (both legs, summed over timestamps).
  - sharpe_net             : the after-cost annualized Sharpe of the per-period basket return.

Plus standard model diagnostics (AUC, rank-IC) for context — but the THRESHOLD CURVE is the headline.

Everything here is a pure function of (scores, labels, timestamps, symbols, half-spreads, capital);
it is computed identically for the real model, the shuffle baseline and predict-zero, so the curves are
directly comparable.
"""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass

import numpy as np

from quantlib.strategy_core.cost import long_short_per_name_cost


@dataclass(frozen=True)
class PercentileCut:
    """One row of the threshold curve: a top/bottom-`frac` L/S basket and its conservative-application
    diagnostics (precision, $/trade, total $, Sharpe)."""

    frac: float
    n_trades: int
    directional_precision: float
    mean_fwd_return: float
    dollar_per_trade: float
    total_dollar_pnl: float
    net_per_period: float
    sharpe_net: float
    breakeven_cost_bps: float


@dataclass(frozen=True)
class ThresholdCurve:
    """The full percentile-threshold curve + the context model diagnostics."""

    cuts: list[PercentileCut]
    auc: float  # sign-AUC: P(score ranks a winner above a loser), over the cross-section
    rank_ic: float  # mean per-timestamp Spearman rank-IC of score vs forward excess


def _bucket(
    scores: list[float], labels: list[float], groups: list, symbols: list[str], spreads: list[float]
) -> dict[object, list[tuple[float, float, str, float]]]:
    buckets: dict[object, list[tuple[float, float, str, float]]] = defaultdict(list)
    for score, label, group, symbol, spread in zip(scores, labels, groups, symbols, spreads):
        if math.isnan(score) or math.isnan(label):
            continue
        spread_ok = spread if (spread == spread) else 0.0
        buckets[group].append((float(score), float(label), symbol, spread_ok))
    return buckets


def _cut_diagnostics(
    buckets: dict[object, list[tuple[float, float, str, float]]],
    *,
    frac: float,
    capital: float,
    cost_mult: float,
    slippage_bps: float,
    borrow_bps_annual: float,
    periods_per_year: float,
) -> PercentileCut:
    """Diagnostics for one percentile cut. Precision + mean-return + $/trade are computed over the
    selected names; the basket $ P&L + Sharpe come from `long_short_per_name_cost` so the money matches
    the equity curve exactly (one cost model, no second implementation)."""
    correct = 0
    n_selected = 0
    sum_signed_return = 0.0
    scores: list[float] = []
    labels: list[float] = []
    flat_groups: list[object] = []
    flat_symbols: list[str] = []
    flat_spreads: list[float] = []
    for group, rows in buckets.items():
        ordered = sorted(rows, key=lambda row: row[0])
        k = max(1, int(frac * len(ordered)))
        if len(ordered) < 2 * k:
            continue
        shorts, longs = ordered[:k], ordered[-k:]
        for _, label, _, _ in longs:
            n_selected += 1
            sum_signed_return += label
            if label > 0:
                correct += 1
        for _, label, _, _ in shorts:
            n_selected += 1
            sum_signed_return += -label
            if label < 0:
                correct += 1
        for score, label, symbol, spread in ordered:
            scores.append(score)
            labels.append(label)
            flat_groups.append(group)
            flat_symbols.append(symbol)
            # fold the flat slippage into the per-name half-spread so the SAME cost model charges
            # (half_spread * cost_mult) + slippage on each name's turnover.
            flat_spreads.append(spread + slippage_bps)
    economics = long_short_per_name_cost(
        scores,
        labels,
        flat_groups,
        flat_symbols,
        flat_spreads,
        frac=frac,
        cost_mult=cost_mult,
        borrow_bps_annual=borrow_bps_annual,
        periods_per_year=periods_per_year,
    )
    net_per_period = float(economics.get("net_per_period", float("nan")))
    n_periods = int(economics.get("n_periods", 0))
    # $ P&L: each period the dollar-neutral book deploys `capital` long + `capital` short; the per-period
    # return is on the long-leg notional (== the book's gross exposure base), so $ P&L per period =
    # net_per_period * capital, and total over the test span = sum of per-period $.
    total_dollar = net_per_period * capital * n_periods if n_periods else float("nan")
    dollar_per_trade = (total_dollar / n_selected) if n_selected else float("nan")
    return PercentileCut(
        frac=frac,
        n_trades=n_selected,
        directional_precision=(correct / n_selected) if n_selected else float("nan"),
        mean_fwd_return=(sum_signed_return / n_selected) if n_selected else float("nan"),
        dollar_per_trade=dollar_per_trade,
        total_dollar_pnl=total_dollar,
        net_per_period=net_per_period,
        sharpe_net=float(economics.get("sharpe_net", float("nan"))),
        breakeven_cost_bps=float(economics.get("breakeven_cost_bps", float("nan"))),
    )


def threshold_curve(
    scores: list[float],
    labels: list[float],
    groups: list,
    symbols: list[str],
    spreads: list[float],
    *,
    cuts: tuple[float, ...],
    capital: float,
    cost_mult: float,
    slippage_bps: float,
    borrow_bps_annual: float,
    periods_per_year: float,
) -> ThresholdCurve:
    buckets = _bucket(scores, labels, groups, symbols, spreads)
    rows = [
        _cut_diagnostics(
            buckets,
            frac=frac,
            capital=capital,
            cost_mult=cost_mult,
            slippage_bps=slippage_bps,
            borrow_bps_annual=borrow_bps_annual,
            periods_per_year=periods_per_year,
        )
        for frac in cuts
    ]
    return ThresholdCurve(cuts=rows, auc=_sign_auc(buckets), rank_ic=_mean_rank_ic(buckets))


def _sign_auc(buckets: dict[object, list[tuple[float, float, str, float]]]) -> float:
    """Sign-AUC pooled across timestamps: P(score ranks a positive-label name above a negative-label
    name). 0.5 == no skill. Computed via the rank-sum identity (O(n log n) per timestamp)."""
    total_pairs = 0.0
    concordant = 0.0
    for rows in buckets.values():
        pos = [score for score, label, _, _ in rows if label > 0]
        neg = [score for score, label, _, _ in rows if label < 0]
        if not pos or not neg:
            continue
        combined = sorted([(score, 1) for score in pos] + [(score, 0) for score in neg])
        rank_sum_pos = 0.0
        i = 0
        while i < len(combined):
            j = i
            while j + 1 < len(combined) and combined[j + 1][0] == combined[i][0]:
                j += 1
            avg_rank = (i + j) / 2.0 + 1.0
            for k in range(i, j + 1):
                if combined[k][1] == 1:
                    rank_sum_pos += avg_rank
            i = j + 1
        n_pos = len(pos)
        n_neg = len(neg)
        auc = (rank_sum_pos - n_pos * (n_pos + 1) / 2.0) / (n_pos * n_neg)
        concordant += auc * n_pos * n_neg
        total_pairs += n_pos * n_neg
    return (concordant / total_pairs) if total_pairs else float("nan")


def _mean_rank_ic(buckets: dict[object, list[tuple[float, float, str, float]]]) -> float:
    ics: list[float] = []
    for rows in buckets.values():
        if len(rows) < 5:
            continue
        score_ranks = _ranks([row[0] for row in rows])
        label_ranks = _ranks([row[1] for row in rows])
        ic = _pearson(score_ranks, label_ranks)
        if not math.isnan(ic):
            ics.append(ic)
    return float(np.mean(ics)) if ics else float("nan")


def _ranks(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = (i + j) / 2.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def _pearson(xs: list[float], ys: list[float]) -> float:
    n = len(xs)
    if n < 2:
        return math.nan
    mx, my = sum(xs) / n, sum(ys) / n
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    sy = math.sqrt(sum((y - my) ** 2 for y in ys))
    return cov / (sx * sy) if sx > 0 and sy > 0 else math.nan
