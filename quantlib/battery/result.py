"""The rigorous evidence bundle (§2.3). A result is NOT a Sharpe number — it is the bundle that
makes the three historical traps un-foolable:

  trap #1 (illiquid tail) -> `by_stratum` + `cost_curve` + per-name `cost_used_bps`
  trap #2 (symmetric burst) -> `directional` (magnitude labels never graduate to a P&L verdict)
  trap #3 (data traps)    -> the auto-run `SanityReport` ($1 floor / per-day winsor / label-std /
                             tradeable-entry), which can FLAG the verdict.

Every field maps to a §0 lesson; the `verdict` is the one-line arbiter.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from quantlib.battery.spec import ArchetypeSpec


class Verdict(str, Enum):
    PASS = "PASS"  # clears the gate AND beats its shuffle AND tradeable net>0
    FAIL = "FAIL"  # an honest null (the expected, healthy outcome)
    DESCRIPTIVE_ONLY = "DESCRIPTIVE-ONLY"  # a non-directional (magnitude) label -> no P&L claim
    TRAP_FLAGGED = "TRAP-FLAGGED"  # a SanityReport guard tripped -> the number is not trustworthy


@dataclass
class NullStat:
    ic: float
    n: int


@dataclass
class StratumStat:
    name: str
    real_ic: float
    shuffle_ic: float
    nw_t: float
    net_per_period: float
    breakeven_cost_bps: float
    n_names: int


@dataclass
class SanityReport:
    """The auto-guard from §0.3 — the discipline each modeller used to re-remember, run once here."""

    price_floor_applied: bool  # $1 floor enforced on both legs (in the Panel build)
    winsorized: bool  # per-day symmetric winsorization applied to the raw return
    label_std: float  # catches the 50-226x fake-return blow-up
    label_std_ok: bool  # label_std within a sane band for the horizon
    entry_minute_ok: bool  # earliest entry >= 09:35 ET (never the 09:30 print)
    tradeable_fraction: float  # rows passing the liquidity floor

    @property
    def ok(self) -> bool:
        return self.price_floor_applied and self.label_std_ok and self.entry_minute_ok

    @property
    def reason(self) -> str:
        problems = []
        if not self.price_floor_applied:
            problems.append("no $1 price floor")
        if not self.label_std_ok:
            problems.append(f"label_std={self.label_std:.4f} out of band")
        if not self.entry_minute_ok:
            problems.append("entry < 09:35 ET")
        return "; ".join(problems) if problems else "clean"


@dataclass
class BacktestResult:
    spec: ArchetypeSpec

    # headline economics (net of REALISTIC per-name half-spread cost)
    net_per_period: float
    gross_per_period: float
    sharpe_net: float
    hit_rate: float
    mean_turnover: float
    breakeven_cost_bps: float

    # the two null baselines (the only legitimate no-skill lines)
    shuffle_canary: NullStat
    predict_zero: NullStat
    edge_vs_shuffle: float  # real_ic - shuffle_ic ; the trust arbiter

    # significance (overlap-aware)
    mean_ic: float
    nw_t: float
    n_test_ts: int
    n_rows: int

    # directionality (trap #2)
    directional: bool
    up_vs_down_asymmetry: float | None  # first-touch only (Phase 1); None for cross-sectional

    # data-trap sanity (trap #3)
    sanity: SanityReport

    # breakdowns + cost curve (trap #1)
    by_stratum: dict[str, StratumStat] = field(default_factory=dict)
    by_regime: dict[str, StratumStat] = field(default_factory=dict)
    cost_curve: list[tuple[float, float]] = field(default_factory=list)
    cost_used_bps: float = 0.0

    verdict: Verdict = Verdict.FAIL
    verdict_reason: str = ""
    # path-dependent fields (Phase 1) — stubbed for the cross-sectional phase
    per_trade_pnl: None = None


# Pre-registered PASS gate (the laneC 1d gate, generalized). A cell PASSES iff it clears ALL legs.
GATE_IC = 0.01
GATE_EDGE = 0.01
GATE_NW_T = 2.0
GATE_BREAKEVEN_BPS = 10.0


def decide_verdict(result: BacktestResult) -> tuple[Verdict, str]:
    """Map the bundle to the one-line verdict. Order matters: a tripped trap-guard or a
    non-directional label SHORT-CIRCUITS before any P&L claim is made."""
    if not result.directional:
        return Verdict.DESCRIPTIVE_ONLY, "non-directional (magnitude) label — cannot graduate to P&L"
    if not result.sanity.ok:
        return Verdict.TRAP_FLAGGED, result.sanity.reason
    breakeven = result.breakeven_cost_bps
    breakeven_ok = breakeven == breakeven and breakeven > GATE_BREAKEVEN_BPS  # NaN-safe
    legs = {
        "IC>=0.01": result.mean_ic >= GATE_IC,
        "edge>=0.01": result.edge_vs_shuffle >= GATE_EDGE,
        "|t|>=2.0": abs(result.nw_t) >= GATE_NW_T if result.nw_t == result.nw_t else False,
        f"breakeven>{GATE_BREAKEVEN_BPS:.0f}": breakeven_ok,
    }
    if all(legs.values()):
        return Verdict.PASS, "all gate legs pass + tradeable net of per-name cost"
    failed = [name for name, ok in legs.items() if not ok]
    return Verdict.FAIL, "honest null — failed: " + ", ".join(failed)
