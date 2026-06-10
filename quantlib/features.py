"""Feature computation — the v1 feature set, computed identically live and
historically. This is the second half of "one feature codebase, two callers":
the live feature-computer and the historical feature-store builder both call
`compute_features` on a FeatureContext.

Hard rules (enforced by tests + review):
- NEVER read wall-clock time. All time logic uses ctx.ts (point-in-time).
- A feature uses only data at or before ctx.ts (no lookahead).
- Return NaN only for the mathematically/temporally undefined (not enough history
  yet, division by zero), never to paper over missing required inputs.

Order of FEATURE_NAMES is the contract: feature_vectors.vector[i] <-> names[i].
Bump FEATURE_SET_VERSION whenever the set changes.
"""
import math
import statistics
from dataclasses import dataclass, field
from datetime import datetime

FEATURE_SET_VERSION = "v1.0.0"

RETURN_WINDOWS = [5, 15, 30, 60]
VOL_WINDOWS = [30, 60]


@dataclass
class BarRow:
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    vwap: float


@dataclass
class FeatureContext:
    """Point-in-time view for one symbol at one minute. `bars` is oldest..newest
    with bars[-1].ts == ts. Optional fields are NaN-producing when absent."""

    symbol: str
    ts: datetime
    bars: list[BarRow]                       # this symbol, up to and incl ts
    session_open: float                      # first bar open of the session
    market_bars: list[BarRow] = field(default_factory=list)   # SPY, aligned
    trade_imbalance: float = math.nan        # signed_volume/(buy+sell) for ts
    large_print_cnt: float = math.nan
    trade_intensity: float = math.nan
    spread_bps: float = math.nan             # mean_spread_bps for ts
    quote_imbalance: float = math.nan


def _closes(bars: list[BarRow]) -> list[float]:
    return [bar.close for bar in bars]


def _ret(closes: list[float], k: int) -> float:
    if len(closes) <= k or closes[-1 - k] == 0:
        return math.nan
    return closes[-1] / closes[-1 - k] - 1.0


def _realized_vol(closes: list[float], window: int) -> float:
    if len(closes) <= window:
        return math.nan
    recent = closes[-(window + 1):]
    log_returns = [
        math.log(recent[i] / recent[i - 1])
        for i in range(1, len(recent))
        if recent[i] > 0 and recent[i - 1] > 0
    ]
    if len(log_returns) < 2:
        return math.nan
    return statistics.stdev(log_returns)


def _volume_zscore(bars: list[BarRow], window: int) -> float:
    if len(bars) <= window:
        return math.nan
    prior = [bar.volume for bar in bars[-(window + 1):-1]]
    if len(prior) < 2:
        return math.nan
    mean = statistics.mean(prior)
    spread = statistics.stdev(prior)
    if spread == 0:
        return math.nan
    return (bars[-1].volume - mean) / spread


def compute_features(ctx: FeatureContext) -> dict[str, float]:
    """Compute the v1 feature dict for ctx. Keys match FEATURE_NAMES."""
    closes = _closes(ctx.bars)
    latest = ctx.bars[-1]
    features: dict[str, float] = {}

    for window in RETURN_WINDOWS:
        features[f"ret_{window}m"] = _ret(closes, window)
    for window in VOL_WINDOWS:
        features[f"vol_{window}m"] = _realized_vol(closes, window)

    features["vol_z_30"] = _volume_zscore(ctx.bars, 30)
    features["vwap_dev"] = (latest.close / latest.vwap - 1.0) if latest.vwap else math.nan
    features["range_pct"] = (latest.high - latest.low) / latest.close if latest.close else math.nan
    features["gap_from_open"] = (latest.close / ctx.session_open - 1.0) if ctx.session_open else math.nan

    # Market-relative (vs SPY): excess of this symbol's 30m return over market's.
    market_closes = _closes(ctx.market_bars)
    market_ret_30m = _ret(market_closes, 30)
    own_ret_30m = features["ret_30m"]
    features["rel_ret_30m"] = (
        own_ret_30m - market_ret_30m
        if not (math.isnan(own_ret_30m) or math.isnan(market_ret_30m))
        else math.nan
    )

    # Calendar (point-in-time from ctx.ts; UTC). Market open features use session.
    features["minute_of_day"] = float(ctx.ts.hour * 60 + ctx.ts.minute)
    features["day_of_week"] = float(ctx.ts.weekday())

    # Microstructure (pass-through of the minute's aggregates, already parity-tested)
    features["trade_imbalance"] = ctx.trade_imbalance
    features["large_print_cnt"] = ctx.large_print_cnt
    features["trade_intensity"] = ctx.trade_intensity
    features["spread_bps"] = ctx.spread_bps
    features["quote_imbalance"] = ctx.quote_imbalance

    return features


# The ordered contract. Built once from a representative call so it can't drift
# from compute_features.
FEATURE_NAMES: list[str] = (
    [f"ret_{w}m" for w in RETURN_WINDOWS]
    + [f"vol_{w}m" for w in VOL_WINDOWS]
    + [
        "vol_z_30", "vwap_dev", "range_pct", "gap_from_open", "rel_ret_30m",
        "minute_of_day", "day_of_week",
        "trade_imbalance", "large_print_cnt", "trade_intensity",
        "spread_bps", "quote_imbalance",
    ]
)


def feature_vector(ctx: FeatureContext) -> list[float]:
    """compute_features as an ordered vector matching FEATURE_NAMES."""
    features = compute_features(ctx)
    if set(features) != set(FEATURE_NAMES):
        raise ValueError(
            f"feature mismatch: {set(features) ^ set(FEATURE_NAMES)}"
        )
    return [features[name] for name in FEATURE_NAMES]
