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
from datetime import date, datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

FEATURE_SET_VERSION = "v1.0.0"           # the LIVE serving set; v1.1.0 is offline-only

MOMENTUM_DAYS = [1, 3, 5, 10]            # trailing trading-day windows for daily momentum
MAX_MOMENTUM_LOOKBACK = max(MOMENTUM_DAYS)   # prior trading days a momentum feature needs

_NY = ZoneInfo("America/New_York")
_RTH_OPEN = dtime(9, 30)
_RTH_CLOSE = dtime(16, 0)


def is_rth(ts: datetime) -> bool:
    """True if ts falls in the US regular trading session (09:30-16:00 ET).
    Converts to America/New_York so it's correct across DST (a fixed UTC window
    would be off by an hour for part of the year)."""
    local = ts.astimezone(_NY).timetz()
    return _RTH_OPEN <= local.replace(tzinfo=None) < _RTH_CLOSE


def on_cadence(ts: datetime, cadence_minutes: int) -> bool:
    """True if ts lands on the rebalance cadence (ET clock minutes divisible by
    cadence; e.g. cadence 30 -> :00 and :30 each hour)."""
    return ts.astimezone(_NY).minute % cadence_minutes == 0

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
    daily_closes: dict[date, float] = field(default_factory=dict)         # this symbol, by date
    market_daily_closes: dict[date, float] = field(default_factory=dict)  # SPY, by date
    trade_imbalance: float = math.nan        # signed_volume/(buy+sell) for ts
    large_print_cnt: float = math.nan
    trade_intensity: float = math.nan
    spread_bps: float = math.nan             # mean_spread_bps for ts
    quote_imbalance: float = math.nan


def _contiguous_run(values_by_ts: dict[datetime, float], ts: datetime, window: int) -> list[float]:
    """Values for the contiguous minute run ending at ts: [ts-window .. ts], reset
    on any gap so the result never spans a missing minute or a session boundary
    (with RTH-only bars, the prior session's minutes simply aren't present)."""
    run: list[float] = []
    for offset in range(window, -1, -1):
        value = values_by_ts.get(ts - timedelta(minutes=offset))
        run = run + [value] if value is not None else []
    return run


def _ret_ts(close_by_ts: dict[datetime, float], ts: datetime, k: int) -> float:
    """k-minute return by timestamp lookup (gap/session-safe): NaN unless the bar
    exactly k minutes earlier exists."""
    base = close_by_ts.get(ts - timedelta(minutes=k))
    current = close_by_ts.get(ts)
    if base and current and base > 0:
        return current / base - 1.0
    return math.nan


def _realized_vol_ts(close_by_ts: dict[datetime, float], ts: datetime, window: int) -> float:
    run = _contiguous_run(close_by_ts, ts, window)
    log_returns = [
        math.log(run[i] / run[i - 1])
        for i in range(1, len(run))
        if run[i] > 0 and run[i - 1] > 0
    ]
    if len(log_returns) < 2:
        return math.nan
    return statistics.stdev(log_returns)


def _volume_zscore_ts(vol_by_ts: dict[datetime, float], ts: datetime, window: int) -> float:
    current = vol_by_ts.get(ts)
    prior = _contiguous_run(vol_by_ts, ts - timedelta(minutes=1), window - 1)
    if current is None or len(prior) < 2:
        return math.nan
    spread = statistics.stdev(prior)
    if spread == 0:
        return math.nan
    return (current - statistics.mean(prior)) / spread


def _daily_momentum(closes_by_date: dict[date, float], as_of: date, k: int) -> float:
    """Trailing k-trading-day return using only completed days STRICTLY BEFORE as_of
    (point-in-time: never uses today's still-forming close). NaN if <k+1 prior days."""
    prior = sorted(d for d in closes_by_date if d < as_of)
    if len(prior) <= k:
        return math.nan
    recent = closes_by_date[prior[-1]]
    past = closes_by_date[prior[-1 - k]]
    return (recent / past - 1.0) if past else math.nan


def compute_features(ctx: FeatureContext) -> dict[str, float]:
    """Compute the v1 feature dict for ctx. Keys match FEATURE_NAMES.

    All history lookups are by TIMESTAMP (not list position), so gaps and session
    boundaries never silently shorten or stretch a window — assumes ctx.bars is the
    symbol's RTH bar series ending at ctx.ts."""
    close_by_ts = {bar.ts: bar.close for bar in ctx.bars}
    vol_by_ts = {bar.ts: bar.volume for bar in ctx.bars}
    latest = ctx.bars[-1]
    features: dict[str, float] = {}

    for window in RETURN_WINDOWS:
        features[f"ret_{window}m"] = _ret_ts(close_by_ts, ctx.ts, window)
    for window in VOL_WINDOWS:
        features[f"vol_{window}m"] = _realized_vol_ts(close_by_ts, ctx.ts, window)

    features["vol_z_30"] = _volume_zscore_ts(vol_by_ts, ctx.ts, 30)
    features["vwap_dev"] = (latest.close / latest.vwap - 1.0) if latest.vwap else math.nan
    features["range_pct"] = (latest.high - latest.low) / latest.close if latest.close else math.nan
    features["gap_from_open"] = (latest.close / ctx.session_open - 1.0) if ctx.session_open else math.nan

    # Market-relative (vs SPY): excess of this symbol's 30m return over market's.
    market_close_by_ts = {bar.ts: bar.close for bar in ctx.market_bars}
    market_ret_30m = _ret_ts(market_close_by_ts, ctx.ts, 30)
    own_ret_30m = features["ret_30m"]
    features["rel_ret_30m"] = (
        own_ret_30m - market_ret_30m
        if not (math.isnan(own_ret_30m) or math.isnan(market_ret_30m))
        else math.nan
    )

    # Calendar in market-local (ET) time so the same session moment maps to the same
    # value across the DST boundary (raw UTC would shift these by 60 mid-panel).
    local = ctx.ts.astimezone(_NY)
    features["minute_of_day"] = float(local.hour * 60 + local.minute)
    features["day_of_week"] = float(local.weekday())

    # Microstructure (pass-through of the minute's aggregates, already parity-tested)
    features["trade_imbalance"] = ctx.trade_imbalance
    features["large_print_cnt"] = ctx.large_print_cnt
    features["trade_intensity"] = ctx.trade_intensity
    features["spread_bps"] = ctx.spread_bps
    features["quote_imbalance"] = ctx.quote_imbalance

    # Cross-sectional daily MOMENTUM (v1.1.0): trailing returns + market-relative, from
    # completed prior trading days only. NaN when daily_closes isn't supplied (v1.0.0
    # callers) or there isn't enough history.
    as_of = ctx.ts.astimezone(_NY).date()
    for k in MOMENTUM_DAYS:
        own = _daily_momentum(ctx.daily_closes, as_of, k)
        mkt = _daily_momentum(ctx.market_daily_closes, as_of, k)
        features[f"mom_{k}d"] = own
        features[f"mom_{k}d_rel"] = (
            own - mkt if not (math.isnan(own) or math.isnan(mkt)) else math.nan
        )

    return features


# The ordered contract. Built once from a representative call so it can't drift
# from compute_features.
MICRO_NAMES = ["trade_imbalance", "large_print_cnt", "trade_intensity", "spread_bps", "quote_imbalance"]
CALENDAR_NAMES = ["minute_of_day", "day_of_week"]
MOMENTUM_NAMES = [f"mom_{k}d" for k in MOMENTUM_DAYS] + [f"mom_{k}d_rel" for k in MOMENTUM_DAYS]

FEATURE_NAMES: list[str] = (
    [f"ret_{w}m" for w in RETURN_WINDOWS]
    + [f"vol_{w}m" for w in VOL_WINDOWS]
    + [
        "vol_z_30", "vwap_dev", "range_pct", "gap_from_open", "rel_ret_30m",
        "minute_of_day", "day_of_week",
    ]
    + MICRO_NAMES
)

# v1.1.0 = the 13 non-micro v1 features (stable prefix) + 8 daily-momentum features.
# Offline only — used by the experimenter/backfiller; the live computer stays v1.0.0.
NON_MICRO_NAMES = [name for name in FEATURE_NAMES if name not in MICRO_NAMES]   # 13
V11_NAMES: list[str] = NON_MICRO_NAMES + MOMENTUM_NAMES                          # 21

FEATURE_SETS: dict[str, list[str]] = {"v1.0.0": FEATURE_NAMES, "v1.1.0": V11_NAMES}


def feature_vector(ctx: FeatureContext, version: str = "v1.0.0") -> list[float]:
    """compute_features as an ordered vector matching the named feature set's contract."""
    names = FEATURE_SETS[version]
    features = compute_features(ctx)
    missing = set(names) - set(features)
    if missing:
        raise ValueError(f"feature set {version} missing: {missing}")
    return [features[name] for name in names]
