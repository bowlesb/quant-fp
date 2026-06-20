"""`ArchetypeSpec` — the frozen point in the §1 taxonomy space `(HORIZON) x (LABEL/ENTRY) x
(CONDITIONING/SELECTION) x (SIZING)`. A small frozen dataclass, deliberately NOT a config DSL
(§6.3): four mechanisms + a parameter grid is enough. The spec is the promotable artifact — a
PASS cell carries its full spec as the validation record a live strategy container subscribes on.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Horizon(str, Enum):
    M30 = "30m"  # intraday 30-minute forward
    M60 = "60m"  # intraday 60-minute forward
    EOD = "eod"  # enter intraday, resolve at the session close
    OVERNIGHT = "overnight"  # close_d -> next-session 09:35 open
    D2 = "2d"  # 2-day hold
    D3 = "3d"  # 3-day hold


class Conditioner(str, Enum):
    NONE = "none"
    SECTOR = "sector"
    LIQUIDITY_TERCILE = "liquidity_tercile"  # restrict to the most-liquid tercile (tradeable cut)
    UP_DOWN_MARKET = "up_down_market_day"  # split by the up/down-market-day regime


class Sizing(str, Enum):
    EW = "ew"  # equal-weight top/bottom-k, dollar-neutral


@dataclass(frozen=True)
class ArchetypeSpec:
    """One battery cell. `mechanism` names the distinct archetype (Phase 0 ships only
    `cross_sectional_ls`); horizon/conditioner/sizing are its parameters."""

    mechanism: str  # "cross_sectional_ls" (Phase 0); "triple_barrier"/"streak"/"single_name" later
    horizon: Horizon
    conditioner: Conditioner = Conditioner.NONE
    sizing: Sizing = Sizing.EW
    frac: float = 0.1  # top/bottom fraction for the L/S basket

    @property
    def key(self) -> str:
        return f"{self.mechanism}|{self.horizon.value}|{self.conditioner.value}|{self.sizing.value}"

    @property
    def horizon_minutes(self) -> int:
        """Walk-forward purge horizon in market minutes (overnight/multi-day purge >= the gap)."""
        return {
            Horizon.M30: 30,
            Horizon.M60: 60,
            Horizon.EOD: 390,
            Horizon.OVERNIGHT: 1440,
            Horizon.D2: 2880,
            Horizon.D3: 4320,
        }[self.horizon]

    @property
    def is_intraday(self) -> bool:
        return self.horizon in (Horizon.M30, Horizon.M60)

    @property
    def cadence_min(self) -> int:
        """Rebalance cadence for annualization: 30 for intraday, 390 (one/day) for EOD+daily."""
        return 30 if self.is_intraday else 390
