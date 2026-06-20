"""The strategy-battery / edge-evaluation harness — Phase 0 (the cross-sectional battery).

One call turns "I have a feature-set" into "which strategy archetypes show a real, tradeable
edge — with evidence", with the anti-fooling discipline (look-ahead, per-name cost, the two
nulls, the data-trap guards, multiple-comparisons correction) built IN, not re-hand-rolled.

Entry point:

    report = evaluate_features(feature_set, ("2025-01-01", "2026-06-17"))
    print(report.summary_md)      # which cells lit up AFTER BY-FDR correction
    report.leaderboard            # PASS cells (empty = the honest, expected null)

Phase 0 ships archetype 1 (cross-sectional L/S directional excess) over its (horizon x
conditioner x sizing) grid — ~5 of Ben's 7 named strategies as one mechanism. The Panel is
loaded ONCE per cadence (daily-reduced for EOD/overnight/multi-day; intraday-minute for 30m/60m)
and every cell evaluates over the resident arrays — the key to the perf budget.

The Phase-1 Rust first-touch / streak archetypes slot into the SAME Panel layout + the SAME
`Strategy` protocol (see strategy.py) with zero conversion.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from quantlib.battery.family import (
    FamilyCorrection,
    benjamini_yekutieli,
    one_sided_p_from_t,
)
from quantlib.battery.panel import (
    DAILY_FEATURE_COLS,
    Panel,
    build_daily_panel,
    build_intraday_panel,
    panel_from_daily_frame,
    panel_from_intraday_frame,
)
from quantlib.battery.result import BacktestResult, Verdict
from quantlib.battery.spec import ArchetypeSpec, Conditioner, Horizon, Sizing
from quantlib.battery.strategy import CrossSectionalLS

# ---------------------------------------------------------------------------
# Feature-set reference + universe
# ---------------------------------------------------------------------------


@dataclass
class FeatureSetRef:
    """What to evaluate. Phase 0 supports two modes:
    - `daily=True`  -> the daily-reduced trailing-EOD feature panel built from raw bars (the
      laneC substrate); `groups` is ignored (features are the fixed DAILY_FEATURE_COLS).
    - `daily=False` -> the intraday store panel: `groups` = {store group: [feature names]} joined
      point-in-time (the trusted-baseline substrate)."""

    name: str
    daily: bool = True
    groups: dict[str, list[str]] | None = None
    horizons_min: tuple[int, ...] = (30, 60)


@dataclass
class UniverseSpec:
    """Symbol-universe filter for the eval scope. `top_by_adv` caps to the most-liquid N names —
    the only tradeable universe anyway (trap #1), and the lever that bounds the perf budget."""

    name: str = "trusted"
    top_by_adv: int | None = None


TRUSTED = UniverseSpec(name="trusted", top_by_adv=None)
LIQUID_1500 = UniverseSpec(name="liquid_1500", top_by_adv=1500)


# ---------------------------------------------------------------------------
# The PRE-REGISTERED default battery (§6: a fixed, versioned cell set so the default
# run is not post-hoc cell selection)
# ---------------------------------------------------------------------------

DEFAULT_BATTERY_VERSION = "phase0-v1"

_DAILY_HORIZONS = [Horizon.EOD, Horizon.OVERNIGHT, Horizon.D2, Horizon.D3]
_INTRADAY_HORIZONS = [Horizon.M30, Horizon.M60]
_CONDITIONERS = [
    Conditioner.NONE,
    Conditioner.LIQUIDITY_TERCILE,
    Conditioner.UP_DOWN_MARKET,
    Conditioner.SECTOR,
]


def default_battery(daily: bool) -> list[ArchetypeSpec]:
    """The frozen pre-registered grid for the given cadence: archetype 1 x (horizon x conditioner)
    x EW sizing. Daily cadence -> the EOD/overnight/multi-day horizons; intraday -> 30m/60m."""
    horizons = _DAILY_HORIZONS if daily else _INTRADAY_HORIZONS
    return [
        ArchetypeSpec("cross_sectional_ls", horizon, conditioner, Sizing.EW)
        for horizon in horizons
        for conditioner in _CONDITIONERS
    ]


# ---------------------------------------------------------------------------
# BatteryReport
# ---------------------------------------------------------------------------


@dataclass
class BatteryReport:
    feature_set: FeatureSetRef
    date_range: tuple[str, str]
    universe: UniverseSpec
    results: dict[str, BacktestResult]
    family_correction: FamilyCorrection
    leaderboard: list[str]  # PASS cells surviving BY-FDR, ranked by net Sharpe
    panel_load_seconds: float
    eval_seconds: float
    total_seconds: float
    summary_md: str = ""
    notes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# The one-call entry point
# ---------------------------------------------------------------------------


def evaluate_features(
    feature_set: FeatureSetRef,
    date_range: tuple[str, str],
    *,
    archetypes: list[ArchetypeSpec] | None = None,
    universe: UniverseSpec = TRUSTED,
    seed: int = 13,
    use_gbm: bool = False,
    daily_cache: str | None = None,
) -> BatteryReport:
    """Evaluate a feature-set across the (default or supplied) archetype battery and return the
    comparable, BY-FDR-corrected `BatteryReport`. The panel is loaded ONCE per cadence and every
    cell evaluates over the resident arrays.

    `use_gbm=False` (default) is the FAST PATH: the cell's signal is the leading feature's rank
    (no model fit) — the whole battery stays inside the <30-60s budget. `use_gbm=True` is the
    opt-in DEEPER mode: a LightGBM model per fold ranks the full feature set (the trusted-baseline
    / laneC published numbers were produced this way; it roughly doubles eval cost)."""
    pre_registered = archetypes is None
    specs = archetypes or default_battery(feature_set.daily)
    notes: list[str] = []
    if not pre_registered:
        notes.append("EXPLORATORY: custom archetype list (not the pre-registered default battery)")

    t0 = time.perf_counter()
    panel = _load_panel(feature_set, date_range, universe, daily_cache)
    t_panel = time.perf_counter()

    results: dict[str, BacktestResult] = {}
    for spec in specs:
        strategy = CrossSectionalLS(spec, seed=seed, use_gbm=use_gbm)
        results[spec.key] = strategy.backtest(panel)
    t_eval = time.perf_counter()

    family = _family_correction(results, pre_registered)
    leaderboard = _leaderboard(results, family)
    report = BatteryReport(
        feature_set=feature_set,
        date_range=date_range,
        universe=universe,
        results=results,
        family_correction=family,
        leaderboard=leaderboard,
        panel_load_seconds=round(t_panel - t0, 3),
        eval_seconds=round(t_eval - t_panel, 3),
        total_seconds=round(t_eval - t0, 3),
        notes=notes,
    )
    report.summary_md = _summary_md(report)
    return report


def _load_panel(
    feature_set: FeatureSetRef,
    date_range: tuple[str, str],
    universe: UniverseSpec,
    daily_cache: str | None,
) -> Panel:
    if feature_set.daily:
        frame = build_daily_panel(
            date_range,
            universe_top=universe.top_by_adv,
            daily_cache=daily_cache,
        )
        return panel_from_daily_frame(frame)
    if feature_set.groups is None:
        raise ValueError("intraday FeatureSetRef requires `groups`")
    frame = build_intraday_panel(
        date_range,
        feature_groups=feature_set.groups,
        horizons_min=list(feature_set.horizons_min),
        universe_top=universe.top_by_adv,
    )
    feature_names = [f for feats in feature_set.groups.values() for f in feats]
    return panel_from_intraday_frame(frame, feature_names)


def _family_correction(results: dict[str, BacktestResult], pre_registered: bool) -> FamilyCorrection:
    keys = list(results)
    pvals = [one_sided_p_from_t(results[key].nw_t) for key in keys]
    return benjamini_yekutieli(keys, pvals, q=0.10, pre_registered=pre_registered)


def _leaderboard(results: dict[str, BacktestResult], family: FamilyCorrection) -> list[str]:
    """PASS cells that ALSO survive BY-FDR, ranked by net Sharpe. A cell must beat its own
    shuffle (Verdict.PASS already encodes that) AND survive family correction."""
    reject_by_key = dict(zip(family.keys, family.reject))
    survivors = [
        key
        for key, result in results.items()
        if result.verdict == Verdict.PASS and reject_by_key.get(key, False)
    ]
    return sorted(
        survivors,
        key=lambda key: (
            results[key].sharpe_net if results[key].sharpe_net == results[key].sharpe_net else -1e9
        ),
        reverse=True,
    )


def _fmt(value: float) -> str:
    return f"{value:.4f}" if value == value else "nan"


def _summary_md(report: BatteryReport) -> str:
    lines: list[str] = []
    lines.append(f"# Battery report — {report.feature_set.name}")
    lines.append("")
    lines.append(
        f"date_range={report.date_range[0]}..{report.date_range[1]}  "
        f"universe={report.universe.name}  cells={len(report.results)}  "
        f"battery={DEFAULT_BATTERY_VERSION}"
    )
    lines.append(
        f"panel_load={report.panel_load_seconds}s  eval={report.eval_seconds}s  "
        f"**total={report.total_seconds}s**"
    )
    for note in report.notes:
        lines.append(f"> NOTE: {note}")
    lines.append("")
    lines.append("## Cells")
    lines.append("")
    lines.append(
        "| cell | IC | edge_vs_shuffle | NW t | net/period | breakeven_bps | "
        "cost_used_bps | BY survive | verdict |"
    )
    lines.append("|---|---|---|---|---|---|---|---|---|")
    reject_by_key = dict(zip(report.family_correction.keys, report.family_correction.reject))
    for key, result in sorted(report.results.items()):
        survive = "yes" if reject_by_key.get(key, False) else "no"
        lines.append(
            f"| {key} | {_fmt(result.mean_ic)} | {_fmt(result.edge_vs_shuffle)} | "
            f"{_fmt(result.nw_t)} | {_fmt(result.net_per_period)} | "
            f"{_fmt(result.breakeven_cost_bps)} | {_fmt(result.cost_used_bps)} | "
            f"{survive} | {result.verdict.value} |"
        )
    lines.append("")
    if report.leaderboard:
        lines.append("## Leaderboard (PASS + survives BY-FDR, ranked by net Sharpe)")
        lines.append("")
        for rank, key in enumerate(report.leaderboard, start=1):
            result = report.results[key]
            lines.append(
                f"{rank}. **{key}** — sharpe_net={_fmt(result.sharpe_net)} "
                f"IC={_fmt(result.mean_ic)} NW t={_fmt(result.nw_t)} "
                f"breakeven={_fmt(result.breakeven_cost_bps)}bps"
            )
    else:
        lines.append(
            "## Leaderboard — EMPTY (honest null). This is the expected, healthy outcome given "
            "the edge hunt's track record; a lone surviving cell would warrant suspicion, not "
            "celebration."
        )
    lines.append("")
    return "\n".join(lines)


__all__ = [
    "ArchetypeSpec",
    "BatteryReport",
    "Conditioner",
    "DAILY_FEATURE_COLS",
    "DEFAULT_BATTERY_VERSION",
    "FeatureSetRef",
    "Horizon",
    "LIQUID_1500",
    "Panel",
    "Sizing",
    "TRUSTED",
    "UniverseSpec",
    "Verdict",
    "default_battery",
    "evaluate_features",
]
