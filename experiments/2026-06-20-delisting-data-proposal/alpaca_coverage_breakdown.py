"""Break down INACTIVE coverage by exchange (exchange-listed vs OTC) — the tradeable universe is
exchange-listed, so OTC gaps don't matter for our liquid panel. NEVER prints creds."""
from __future__ import annotations

import os
from collections import Counter

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import AssetClass, AssetStatus
from alpaca.trading.requests import GetAssetsRequest

client = TradingClient(os.environ["ALPACA_KEY_ID"], os.environ["ALPACA_SECRET_KEY"], paper=True)
inactive = client.get_all_assets(GetAssetsRequest(status=AssetStatus.INACTIVE, asset_class=AssetClass.US_EQUITY))
active = client.get_all_assets(GetAssetsRequest(status=AssetStatus.ACTIVE, asset_class=AssetClass.US_EQUITY))


def exch(a):
    e = getattr(a, "exchange", None)
    return getattr(e, "value", str(e))


inact_by_exch = Counter(exch(a) for a in inactive)
act_by_exch = Counter(exch(a) for a in active)
print("INACTIVE by exchange:", dict(inact_by_exch.most_common()))
print("ACTIVE   by exchange:", dict(act_by_exch.most_common()))
exch_listed = {"NYSE", "NASDAQ", "ARCA", "AMEX", "BATS"}
n_inact_listed = sum(1 for a in inactive if exch(a) in exch_listed)
n_act_listed = sum(1 for a in active if exch(a) in exch_listed)
print(f"\nINACTIVE exchange-listed (NYSE/NASDAQ/ARCA/AMEX/BATS): {n_inact_listed}")
print(f"ACTIVE   exchange-listed: {n_act_listed}")
print(f"=> a survivorship-clean exchange-listed universe would add ~{n_inact_listed} delisted names to the panel")
