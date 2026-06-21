"""Probe Alpaca for survivorship-clean coverage. NEVER prints creds. Counts INACTIVE US-equity assets,
inspects the Asset fields (delisting date?), and spot-checks known-delisted tickers."""
from __future__ import annotations

import os

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import AssetClass, AssetStatus
from alpaca.trading.requests import GetAssetsRequest

client = TradingClient(os.environ["ALPACA_KEY_ID"], os.environ["ALPACA_SECRET_KEY"], paper=True)

active = client.get_all_assets(GetAssetsRequest(status=AssetStatus.ACTIVE, asset_class=AssetClass.US_EQUITY))
inactive = client.get_all_assets(GetAssetsRequest(status=AssetStatus.INACTIVE, asset_class=AssetClass.US_EQUITY))
print(f"ACTIVE   US-equity assets: {len(active)}")
print(f"INACTIVE US-equity assets: {len(inactive)}")

# what fields does an Asset carry? (look for a delisting date / status info)
sample = inactive[0] if inactive else (active[0] if active else None)
if sample is not None:
    fields = [f for f in dir(sample) if not f.startswith("_")]
    print("Asset fields:", [f for f in fields if f not in ("dict", "json", "construct", "copy", "from_orm", "parse_file", "parse_obj", "parse_raw", "schema", "schema_json", "update_forward_refs", "validate", "model_dump", "model_dump_json", "model_validate", "model_construct", "model_copy", "model_fields", "model_config", "model_extra", "model_fields_set", "model_computed_fields", "model_post_init", "model_validate_json", "model_validate_strings", "model_rebuild", "model_json_schema", "model_parametrized_name")])
    print("sample INACTIVE asset:", {k: getattr(sample, k, None) for k in ("symbol", "name", "status", "tradable", "exchange", "id")})

# spot-check known-delisted / acquired / bankrupt tickers
known = ["LEHMQ", "FNMA", "SVB", "SIVBQ", "BBBYQ", "FTX", "WORK", "TWTR", "ATVI", "FRC", "FRCB", "SBNY", "CREE", "YHOO", "WCG", "CELG", "XLNX", "MXIM"]
by_sym = {a.symbol: a for a in (active + inactive)}
print("\n=== spot-check known-delisted/acquired/bankrupt tickers ===")
for s in known:
    a = by_sym.get(s)
    if a is None:
        print(f"  {s}: NOT in Alpaca asset list")
    else:
        st = getattr(a, "status", None)
        print(f"  {s}: status={getattr(st,'value',st)} tradable={a.tradable} exch={getattr(a.exchange,'value',a.exchange)} name={a.name[:40]}")
