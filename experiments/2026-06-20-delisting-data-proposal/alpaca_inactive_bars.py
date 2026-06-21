"""Do historical BARS exist for INACTIVE (delisted) symbols? Pull daily bars across their active life for a
few known-delisted-but-present tickers. NEVER prints creds."""
from __future__ import annotations

import datetime as dt
import os

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

client = StockHistoricalDataClient(os.environ["ALPACA_KEY_ID"], os.environ["ALPACA_SECRET_KEY"])

# known delisted/acquired names confirmed present in Alpaca's INACTIVE list
probes = {
    "CELG": ("2010-01-01", "2019-12-31"),   # Celgene, acquired by BMS Nov 2019
    "XLNX": ("2015-01-01", "2022-03-31"),    # Xilinx, acquired by AMD Feb 2022
    "MXIM": ("2015-01-01", "2021-09-30"),    # Maxim, acquired by ADI Aug 2021
    "WORK": ("2019-06-01", "2021-07-31"),    # Slack, acquired by Salesforce Jul 2021
    "WCG":  ("2015-01-01", "2020-02-28"),    # Wellcare, acquired by Centene Jan 2020
    "FNMA": ("2008-01-01", "2010-12-31"),    # Fannie Mae, delisted to OTC 2010
}
for sym, (s, e) in probes.items():
    try:
        req = StockBarsRequest(
            symbol_or_symbols=sym,
            timeframe=TimeFrame.Day,
            start=dt.datetime.fromisoformat(s),
            end=dt.datetime.fromisoformat(e),
            feed="sip",
        )
        bars = client.get_stock_bars(req)
        data = bars.data.get(sym, [])
        if data:
            first, last = data[0], data[-1]
            print(f"  {sym}: {len(data)} daily bars, {first.timestamp.date()} .. {last.timestamp.date()} "
                  f"(last close ${last.close:.2f})")
        else:
            print(f"  {sym}: NO bars returned")
    except Exception as ex:  # noqa: BLE001 - probe: report any vendor error verbatim
        print(f"  {sym}: ERROR {type(ex).__name__}: {str(ex)[:120]}")
