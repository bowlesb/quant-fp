"""The authoritative list of every ported clean-engine feature group + its legacy correspondence.

The clean ``EngineGroup``s live across the ``clean_groups_*.py`` modules and were, until now, only ever
assembled ad-hoc inside tests. The live capture path needs ONE canonical source to construct the engine from
(the clean analogue of the legacy ``runnable(frames)`` registry selection), and the completeness gate needs the
clean→legacy name map in one place. This module is that source.

``ALL_CLEAN_GROUPS`` — one instance of every ported group, in a stable order (the live engine is built from
exactly this list). ``LEGACY_GROUP_OF`` — each clean group's name → its LEGACY parent group name (usually 1:1;
the two split-out groups ``macd`` and ``vwap_deviation`` map to their legacy parents ``technical`` /
``price_volume`` because legacy ships those features inside the parent group, not as standalone groups). The 8
tick-tape groups (print_hhi / size_entropy / subminute_gap_fano / inter_arrival / large_print_burst /
microstructure_burst / tick_runlength / trade_size_dist) read enrich-derived per-minute primitives
(``tick_features.compute_tick_primitives``, carried as bar columns) and complete the set at 66/66 legacy groups.
"""

from __future__ import annotations

from typing import cast

from quantlib.features import REGISTRY
from quantlib.features.clean_engine import EngineGroup
from quantlib.features.clean_groups_daily import (
    DailyBetaClean,
    LiquidityRankClean,
    MultiDayClean,
    MultiDayVwapClean,
    OvernightBetaClean,
    OvernightIntradaySplitClean,
)
from quantlib.features.clean_groups_example import (
    BreadthClean,
    CandlestickClean,
    IntradaySeasonalityClean,
    MacdClean,
    PriorDayClean,
    RealizedRangeClean,
    SwingClean,
    TrendQualityClean,
    VwapDeviationClean,
)
from quantlib.features.clean_groups_pointwise import (
    AssetFlagsClean,
    CalendarClean,
    CalendarEventsClean,
    RoundLevelsClean,
    SectorOneHotClean,
)
from quantlib.features.clean_groups_reference import EdgarFilingFrequencyClean, NewsSentimentClean
from quantlib.features.clean_groups_stateful import (
    DumperStateClean,
    GapFillStateClean,
    RunnerStateClean,
    TechnicalClean,
)
from quantlib.features.clean_groups_tick import (
    InterArrivalClean,
    LargePrintBurstClean,
    MicrostructureBurstClean,
    PrintHHIClean,
    SizeEntropyClean,
    SubminuteGapFanoClean,
    TickRunlengthClean,
    TradeSizeDistClean,
)
from quantlib.features.clean_groups_windowed import (
    CleanMomentumClean,
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
    VolumeLeadsPriceClean,
)
from quantlib.features.clean_groups_xsectional import (
    CrossSectionalRankClean,
    MarketBetaClean,
    MarketContextClean,
    MarketTurbulenceClean,
    PeerRelativeClean,
    ReturnDispersionClean,
    SectorBetaClean,
    SectorReturnClean,
)

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
    # tick-tape (MICROSTRUCTURE — read enrich-derived per-minute primitives, like the quote/signed_volume cols)
    PrintHHIClean,
    SubminuteGapFanoClean,
    SizeEntropyClean,
    InterArrivalClean,
    LargePrintBurstClean,
    MicrostructureBurstClean,
    TickRunlengthClean,
    TradeSizeDistClean,
)

# The groups structurally satisfy the EngineGroup Protocol (verified per-class: ``x: EngineGroup = MultiDayClean()``
# type-checks), but mypy infers the heterogeneous concrete-class UNION when instantiating over the tuple and will
# not widen that union to the Protocol — a known Protocol+union ergonomics limitation, not a contract gap. One
# ``cast`` at construction is the minimal, correct expression. (The per-group ``input_cols: tuple[str, ...]``
# annotations make the bar-column contract explicit regardless.)
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

# The engine's bar-column set: the UNION of every group's input_cols (the enriched bar columns the groups fold —
# close/high/low/open/volume + signed_volume / quote spread+imbalance / tick-derived). The live marshal carries
# exactly these into each minute's numpy dict; the RingBuffer is built ``cols=ALL_CLEAN_INPUT_COLS``. Sorted for
# a deterministic column order.
ALL_CLEAN_INPUT_COLS: tuple[str, ...] = tuple(
    sorted({col for group in ALL_CLEAN_GROUPS for col in group.input_cols})
)

# A clean group's store ``version`` = its LEGACY parent group's version, so the clean engine writes the store
# rows at the SAME (group, version, source) path the OLD engine does — the canary store-diff compares like with
# like, and a flag flip doesn't fork the store layout. (The clean ``EngineGroup`` Protocol carries no version;
# the store needs one, resolved here from the legacy registry by the legacy-parent name.)
CLEAN_VERSION_OF: dict[str, str] = {
    group.name: REGISTRY.get_group(LEGACY_GROUP_OF[group.name]).version for group in ALL_CLEAN_GROUPS
}
