"""The authoritative list of every ported clean-engine feature group + its legacy correspondence.

The clean ``EngineGroup``s live across the ``clean_groups_*.py`` modules and were, until now, only ever
assembled ad-hoc inside tests. The live capture path needs ONE canonical source to construct the engine from
(the clean analogue of the legacy ``runnable(frames)`` registry selection), and the completeness gate needs the
clean→legacy name map in one place. This module is that source.

``ALL_CLEAN_GROUPS`` — one instance of every ported group, in a stable order (the live engine is built from
exactly this list). ``LEGACY_GROUP_OF`` — each clean group's name → its LEGACY parent group name (usually 1:1;
the two split-out groups ``macd`` and ``vwap_deviation`` map to their legacy parents ``technical`` /
``price_volume`` because legacy ships those features inside the parent group, not as standalone groups). The 8
tick-tape legacy groups (print_hhi / size_entropy / subminute_gap_fano / inter_arrival / large_print_burst /
microstructure_burst / tick_runlength / trade_size_dist) are NOT yet ported (the derived-bar-column work) and
so appear in neither list — they are the only unported legacy groups.
"""

from __future__ import annotations

from typing import cast

from quantlib.features.clean_engine import EngineGroup
from quantlib.features.clean_groups_daily import (DailyBetaClean,
                                                  LiquidityRankClean,
                                                  MultiDayClean,
                                                  MultiDayVwapClean,
                                                  OvernightBetaClean,
                                                  OvernightIntradaySplitClean)
from quantlib.features.clean_groups_example import (BreadthClean,
                                                    CandlestickClean,
                                                    IntradaySeasonalityClean,
                                                    MacdClean, PriorDayClean,
                                                    RealizedRangeClean,
                                                    SwingClean,
                                                    TrendQualityClean,
                                                    VwapDeviationClean)
from quantlib.features.clean_groups_pointwise import (AssetFlagsClean,
                                                      CalendarClean,
                                                      CalendarEventsClean,
                                                      RoundLevelsClean,
                                                      SectorOneHotClean)
from quantlib.features.clean_groups_reference import (
    EdgarFilingFrequencyClean, NewsSentimentClean)
from quantlib.features.clean_groups_stateful import (DumperStateClean,
                                                     GapFillStateClean,
                                                     RunnerStateClean,
                                                     TechnicalClean)
from quantlib.features.clean_groups_windowed import (CleanMomentumClean,
                                                     CountFanoClean,
                                                     DistributionClean,
                                                     DrawRangeClean,
                                                     EfficiencyClean,
                                                     LiquidityClean,
                                                     MomentumClean,
                                                     MomentumConsistencyClean,
                                                     MomentumRunClean,
                                                     OhlcVolClean,
                                                     PriceLevelsClean,
                                                     PriceReturnsClean,
                                                     PriceVolumeClean,
                                                     QuoteSpreadClean,
                                                     RangeExpansionClean,
                                                     ResidualAnalysisClean,
                                                     ReturnDynamicsClean,
                                                     SignedTradeRatioClean,
                                                     TradeFlowClean,
                                                     TradeFreqZClean,
                                                     VolatilityClean,
                                                     VolumeClean,
                                                     VolumeExhaustionClean,
                                                     VolumeLeadsPriceClean)
from quantlib.features.clean_groups_xsectional import (CrossSectionalRankClean,
                                                       MarketBetaClean,
                                                       MarketContextClean,
                                                       MarketTurbulenceClean,
                                                       PeerRelativeClean,
                                                       ReturnDispersionClean,
                                                       SectorBetaClean,
                                                       SectorReturnClean)

# Every ported clean group, grouped by module for readability — the live engine is constructed from this list.
# Un-annotated: the concrete class union is inferred; ``ALL_CLEAN_GROUPS`` (the instances) carries the
# EngineGroup Protocol type, which the instances structurally satisfy (a ``type[Protocol]`` does not).
_CLEAN_GROUP_CLASSES = (
    # daily-snapshot (window.session matrices)
    MultiDayClean,
    MultiDayVwapClean,
    DailyBetaClean,
    OvernightBetaClean,
    OvernightIntradaySplitClean,
    LiquidityRankClean,
    # example-module groups (windowed / candlestick / swing / daily / seasonality)
    TrendQualityClean,
    VwapDeviationClean,
    RealizedRangeClean,
    CandlestickClean,
    BreadthClean,
    MacdClean,
    IntradaySeasonalityClean,
    SwingClean,
    PriorDayClean,
    # point-in-time / calendar / static-label
    CalendarClean,
    RoundLevelsClean,
    CalendarEventsClean,
    SectorOneHotClean,
    AssetFlagsClean,
    # external-frame event tapes (window.session CSR)
    NewsSentimentClean,
    EdgarFilingFrequencyClean,
    # carried-state (stateful)
    RunnerStateClean,
    DumperStateClean,
    GapFillStateClean,
    TechnicalClean,
    # windowed (ReductionGroup time-windows)
    PriceLevelsClean,
    PriceReturnsClean,
    DistributionClean,
    PriceVolumeClean,
    VolatilityClean,
    LiquidityClean,
    QuoteSpreadClean,
    OhlcVolClean,
    RangeExpansionClean,
    MomentumClean,
    EfficiencyClean,
    ReturnDynamicsClean,
    MomentumConsistencyClean,
    DrawRangeClean,
    VolumeClean,
    ResidualAnalysisClean,
    CleanMomentumClean,
    MomentumRunClean,
    TradeFlowClean,
    CountFanoClean,
    TradeFreqZClean,
    SignedTradeRatioClean,
    VolumeExhaustionClean,
    VolumeLeadsPriceClean,
    # cross-sectional (universe reduce / paired-OLS)
    ReturnDispersionClean,
    MarketTurbulenceClean,
    SectorReturnClean,
    PeerRelativeClean,
    SectorBetaClean,
    MarketBetaClean,
    CrossSectionalRankClean,
    MarketContextClean,
)

# cast to the Protocol: every class structurally satisfies EngineGroup at runtime, but the no-bar-column groups
# annotate ``input_cols = ()`` whose inferred ``tuple[()]`` mypy will not widen to ``tuple[str, ...]`` — a
# benign annotation gap, not a contract break (the engine reads ``input_cols`` as the bar columns to fold).
ALL_CLEAN_GROUPS: tuple[EngineGroup, ...] = tuple(cast(EngineGroup, cls()) for cls in _CLEAN_GROUP_CLASSES)

# Clean group name -> its LEGACY parent group name. 1:1 except the two split-out groups whose legacy features
# live inside a parent group (macd inside technical; vwap_deviation inside price_volume).
_SPLIT_OUT_PARENT: dict[str, str] = {
    "macd": "technical",
    "vwap_deviation": "price_volume",
}

LEGACY_GROUP_OF: dict[str, str] = {
    group.name: _SPLIT_OUT_PARENT.get(group.name, group.name) for group in ALL_CLEAN_GROUPS
}

# Sanity: every name is distinct (a duplicate name would silently drop a group from the live engine).
assert len({group.name for group in ALL_CLEAN_GROUPS}) == len(ALL_CLEAN_GROUPS), "duplicate clean group name"
