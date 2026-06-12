"""Universe selection: pure, testable logic for the daily liquid-symbol screen.

The I/O (fetching daily bars from Alpaca) lives in the scheduler; this module
just turns per-symbol stats into a ranked, filtered membership list. Storing the
result per day in universe_membership gives backtests a point-in-time universe
(no survivorship bias).
"""

import re
from dataclasses import dataclass

# ETF/ETN/leveraged detector by issuer/product name (a curated ETF reference list is the
# eventual fix). Excludes these from a single-stock cross-sectional universe so leveraged/
# inverse/VIX-futures products don't dominate the model's tails or distort the cross-section.
# High-recall by design: catches sponsor names too (QQQ "Invesco ... Trust", GLD "SPDR ...",
# VXX "iPath ... ETN") which lack the literal word "ETF". Sponsor tokens are chosen NOT to
# collide with operating-company names (e.g. "State Street" is omitted so the STT bank stays;
# its SPDR funds are caught by "SPDR"/"ETF" instead).
_ETF_NAME_RE = re.compile(
    r"\b(ETF|ETN|ProShares|Direxion|iShares|SPDR|Invesco|VanEck|Global X|WisdomTree|"
    r"GraniteShares|Roundhill|iPath|Grayscale|Wise Origin|"
    r"Ultra(Pro|Short)?|[1-3]X|Leveraged|Inverse|Bull|Bear|VIX|Index Fund|"
    r"Exchange[- ]Traded)\b",
    re.IGNORECASE,
)
# Commodity/crypto pools and physical trusts (USO/UNG/FBTC/PSLV-style) carry none of the tokens
# above; match them explicitly. Anchored on the product structure, not the sponsor, so they
# don't collide with operating companies (e.g. the STT bank, Northern Trust, Digital Realty Trust).
_COMMODITY_POOL_RE = re.compile(
    r"United States .*Fund|Physical (Gold|Silver|Platinum|Palladium)|"
    r"(Bitcoin|Ethereum) (Trust|Fund)",
    re.IGNORECASE,
)


def is_etf_like(name: str | None) -> bool:
    """True if the asset name looks like an ETF/ETN/leveraged/commodity-pool product."""
    return bool(name) and bool(
        _ETF_NAME_RE.search(name) or _COMMODITY_POOL_RE.search(name)
    )


@dataclass
class SymbolStats:
    symbol: str
    price: float  # latest close
    adv_dollar: float  # average daily dollar volume over the lookback window
    name: str | None = None  # asset name; when set, ETF-like products are screened out


def select_universe(
    stats: list[SymbolStats],
    min_price: float = 5.0,
    min_adv_dollar: float = 10_000_000.0,
    max_symbols: int = 1000,
) -> list[SymbolStats]:
    """Filter to single-name equities by price and average dollar volume, then keep the
    most liquid `max_symbols` by ADV. ETF/ETN/leveraged products are excluded by name (so
    they don't pollute the single-stock cross-section) whenever a name is supplied — callers
    must populate SymbolStats.name or those products leak back in. Deterministic: ties broken
    by symbol so the universe is reproducible for a given input."""
    eligible = [
        s
        for s in stats
        if s.price >= min_price
        and s.adv_dollar >= min_adv_dollar
        and not is_etf_like(s.name)
    ]
    eligible.sort(key=lambda s: (-s.adv_dollar, s.symbol))
    return eligible[:max_symbols]
