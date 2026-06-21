"""The `BatteryReport` — per-strategy results + the wall-time, rendered as a comparable leaderboard.

One `StrategyResult` per `StrategyConfig`, carrying the net economics + the two anti-fooling nulls
(shuffle IC + predict-zero) so a single battery run gives a trustworthy edge verdict per strategy. The
report's `summary_md` is the dashboard-renderable cell table with the wall-time at the top — so "fast" is
a measured number, not a claim.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class StrategyResult:
    name: str
    signal: str
    label: str
    horizon: int
    n_test_ts: int
    n_rows: int
    mean_ic: float
    shuffle_ic: float
    edge_vs_shuffle: float
    nw_t: float
    net_per_period: float
    gross_per_period: float
    sharpe_net: float
    breakeven_cost_bps: float
    cost_used_bps: float
    predict_zero_pnl: float
    notes: str = ""

    @property
    def verdict(self) -> str:
        """A terse trust verdict: PASS = positive net + beats its shuffle + NW-t >= 2; else FAIL/NULL.

        This is deliberately conservative (the edge hunt's track record is a long string of honest
        nulls); a PASS is "promote to a live paper trial", never a realized-return claim."""
        if self.n_rows == 0 or self.mean_ic != self.mean_ic:
            return "NO-DATA"
        beats_shuffle = self.edge_vs_shuffle == self.edge_vs_shuffle and self.edge_vs_shuffle > 0.0
        net_positive = self.net_per_period == self.net_per_period and self.net_per_period > 0.0
        significant = self.nw_t == self.nw_t and abs(self.nw_t) >= 2.0
        if net_positive and beats_shuffle and significant:
            return "PASS"
        return "FAIL"


@dataclass
class BatteryReport:
    cadence: str
    date_range: tuple[str, str]
    universe_top: int | None
    n_rows: int
    n_features: int
    n_symbols: int
    results: list[StrategyResult]
    panel_load_seconds: float
    eval_seconds: float
    total_seconds: float
    notes: list[str] = field(default_factory=list)

    @property
    def leaderboard(self) -> list[StrategyResult]:
        """PASS strategies ranked by net Sharpe (empty = the honest, expected null)."""
        passing = [r for r in self.results if r.verdict == "PASS"]
        return sorted(
            passing,
            key=lambda r: (r.sharpe_net if r.sharpe_net == r.sharpe_net else -1e9),
            reverse=True,
        )

    @property
    def summary_md(self) -> str:
        return render_battery_md(self)


def _fmt(value: float, places: int = 4) -> str:
    return f"{value:.{places}f}" if value == value else "nan"


def render_battery_md(report: BatteryReport) -> str:
    lines: list[str] = []
    lines.append("# Battery report")
    lines.append("")
    lines.append(
        f"cadence={report.cadence}  range={report.date_range[0]}..{report.date_range[1]}  "
        f"universe_top={report.universe_top}  "
        f"panel={report.n_rows}rows x {report.n_features}feat x {report.n_symbols}sym  "
        f"strategies={len(report.results)}"
    )
    lines.append(
        f"panel_load={report.panel_load_seconds}s  eval={report.eval_seconds}s  "
        f"**total={report.total_seconds}s**  "
        f"(**{_per_strategy(report)}s/strategy** over the shared matrix)"
    )
    for note in report.notes:
        lines.append(f"> NOTE: {note}")
    lines.append("")
    lines.append(
        "| strategy | signal | label | H | n_rows | IC | shuffle_IC | edge | NW t | "
        "net/period | breakeven_bps | cost_bps | sharpe_net | verdict |"
    )
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for result in report.results:
        lines.append(
            f"| {result.name} | {result.signal} | {result.label} | {result.horizon} | "
            f"{result.n_rows} | {_fmt(result.mean_ic)} | {_fmt(result.shuffle_ic)} | "
            f"{_fmt(result.edge_vs_shuffle)} | {_fmt(result.nw_t, 2)} | "
            f"{_fmt(result.net_per_period)} | {_fmt(result.breakeven_cost_bps, 2)} | "
            f"{_fmt(result.cost_used_bps, 2)} | {_fmt(result.sharpe_net, 2)} | {result.verdict} |"
        )
    lines.append("")
    if report.leaderboard:
        lines.append("## Leaderboard (PASS = net-positive + beats shuffle + |NW t|>=2)")
        lines.append("")
        for rank, result in enumerate(report.leaderboard, start=1):
            lines.append(
                f"{rank}. **{result.name}** — sharpe_net={_fmt(result.sharpe_net, 2)} "
                f"IC={_fmt(result.mean_ic)} NW t={_fmt(result.nw_t, 2)} "
                f"breakeven={_fmt(result.breakeven_cost_bps, 2)}bps"
            )
    else:
        lines.append(
            "## Leaderboard — EMPTY (honest null). The expected, healthy outcome given the edge "
            "hunt's track record; a lone surviving cell warrants suspicion, not celebration."
        )
    lines.append("")
    return "\n".join(lines)


def _per_strategy(report: BatteryReport) -> str:
    if not report.results:
        return "nan"
    return f"{report.eval_seconds / len(report.results):.3f}"
