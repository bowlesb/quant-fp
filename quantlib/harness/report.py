"""The organized harness output: `StrategyReport` (the money + the threshold curve + baselines) and
its markdown renderer.

Two parts, per the deliverable:
  a. MONEY  — equity curve ($ on the stated capital), total $ P&L, net return, after-cost Sharpe, max
     drawdown, per-period turnover.
  b. PERCENTILE-THRESHOLD DIAGNOSTICS — the curve over the cuts (precision, $/trade, total $, Sharpe),
     the headline, with the shuffle / predict-zero baselines alongside.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from quantlib.harness.config import HarnessConfig
from quantlib.harness.diagnostics import ThresholdCurve


@dataclass(frozen=True)
class EquityPoint:
    period: int
    timestamp: str
    net_return: float  # the basket's net per-period return
    equity: float  # cumulative $ on the book capital


@dataclass
class MoneyResult:
    """The $ economics of the configured (long_short_frac) basket — the headline P&L."""

    capital: float
    total_pnl: float
    net_return: float  # total return on capital over the test span
    sharpe_net: float
    max_drawdown: float  # fractional, on the equity curve
    mean_turnover: float
    n_periods: int
    breakeven_cost_bps: float
    cost_used_bps: float
    equity_curve: list[EquityPoint] = field(default_factory=list)


@dataclass
class StrategyReport:
    config: HarnessConfig
    n_rows: int
    n_features: int
    n_symbols: int
    n_test_timestamps: int
    money: MoneyResult
    threshold_curve: ThresholdCurve
    shuffle_curve: ThresholdCurve | None
    predict_zero_total_pnl: float
    panel_load_seconds: float
    fit_apply_seconds: float
    total_seconds: float
    notes: list[str] = field(default_factory=list)
    summary_md: str = ""


def _fmt_money(value: float) -> str:
    if value != value:
        return "nan"
    sign = "-" if value < 0 else ""
    return f"{sign}${abs(value):,.0f}"


def _fmt(value: float, places: int = 4) -> str:
    return f"{value:.{places}f}" if value == value else "nan"


def render_summary_md(report: StrategyReport) -> str:
    config = report.config
    money = report.money
    lines: list[str] = []
    lines.append("# Strategy Harness report")
    lines.append("")
    lines.append(
        f"model=**{config.model.value}**  cadence={config.cadence.value}  "
        f"label_horizon={config.label_horizon_days}d/{config.label_horizon_min}m  "
        f"L/S frac={config.long_short_frac:.0%}  capital={_fmt_money(config.capital)}  "
        f"universe_top={config.universe_top}  folds={config.n_folds}"
    )
    lines.append(
        f"panel={report.n_rows:,} rows x {report.n_features} features x {report.n_symbols} symbols  "
        f"test_timestamps={report.n_test_timestamps}"
    )
    lines.append(
        f"panel_load={report.panel_load_seconds}s  fit+apply={report.fit_apply_seconds}s  "
        f"**total={report.total_seconds}s**"
    )
    for note in report.notes:
        lines.append(f"> NOTE: {note}")
    lines.append("")

    lines.append("## a. MONEY (the configured basket, net of per-name cost)")
    lines.append("")
    lines.append(f"- **total P&L**: {_fmt_money(money.total_pnl)} on {_fmt_money(money.capital)} book")
    lines.append(f"- **net return**: {_fmt(money.net_return * 100, 2)}% over the test span")
    lines.append(f"- **after-cost Sharpe**: {_fmt(money.sharpe_net, 2)} (annualized)")
    lines.append(f"- **max drawdown**: {_fmt(money.max_drawdown * 100, 2)}%")
    lines.append(f"- **mean turnover/period**: {_fmt(money.mean_turnover, 3)}")
    lines.append(
        f"- **breakeven cost**: {_fmt(money.breakeven_cost_bps, 2)} bps one-way "
        f"(charged {_fmt(money.cost_used_bps, 2)} bps median)"
    )
    lines.append(f"- **periods**: {money.n_periods}")
    lines.append("")

    lines.append("## b. PERCENTILE-THRESHOLD CURVE (the headline — conservative-application analysis)")
    lines.append("")
    lines.append("As the cut shrinks (more selective), does directional precision and $/trade improve?")
    lines.append("")
    lines.append(
        "| cut (top/bot) | n_trades | precision | mean_fwd_ret | $/trade | total $ P&L | "
        "net/period | Sharpe_net |"
    )
    lines.append("|---|---|---|---|---|---|---|---|")
    for cut in report.threshold_curve.cuts:
        lines.append(
            f"| {cut.frac:.0%} | {cut.n_trades:,} | {_fmt(cut.directional_precision, 4)} | "
            f"{_fmt(cut.mean_fwd_return * 1e4, 2)}bps | {_fmt_money(cut.dollar_per_trade)} | "
            f"{_fmt_money(cut.total_dollar_pnl)} | {_fmt(cut.net_per_period * 1e4, 3)}bps | "
            f"{_fmt(cut.sharpe_net, 2)} |"
        )
    lines.append("")
    lines.append(
        f"context model diagnostics: sign-AUC={_fmt(report.threshold_curve.auc, 4)}  "
        f"rank-IC={_fmt(report.threshold_curve.rank_ic, 4)}"
    )
    lines.append("")

    lines.append("## c. BASELINES (the trust gate — the curve must beat these)")
    lines.append("")
    lines.append(f"- **predict-zero** (no signal): total P&L = {_fmt_money(report.predict_zero_total_pnl)}")
    if report.shuffle_curve is not None:
        lines.append("- **shuffle** (within-timestamp label permutation — the leakage/overfit null):")
        lines.append("")
        lines.append("| cut | precision | mean_fwd_ret | total $ P&L | Sharpe_net |")
        lines.append("|---|---|---|---|---|")
        for cut in report.shuffle_curve.cuts:
            lines.append(
                f"| {cut.frac:.0%} | {_fmt(cut.directional_precision, 4)} | "
                f"{_fmt(cut.mean_fwd_return * 1e4, 2)}bps | {_fmt_money(cut.total_dollar_pnl)} | "
                f"{_fmt(cut.sharpe_net, 2)} |"
            )
        lines.append("")
        lines.append(
            f"shuffle sign-AUC={_fmt(report.shuffle_curve.auc, 4)} (~0.5 expected); "
            f"shuffle rank-IC={_fmt(report.shuffle_curve.rank_ic, 4)} (~0 expected)."
        )
    lines.append("")
    lines.append(
        "> A PASS-grade result needs precision and $/trade to RISE as the cut shrinks AND the real "
        "curve to dominate the shuffle/predict-zero baselines at every cut. Read this as an idealized "
        "upper bound (frictionless basket, survivorship caveat), not a live realized P&L."
    )
    lines.append("")
    return "\n".join(lines)
