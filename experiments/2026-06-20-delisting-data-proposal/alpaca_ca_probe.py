"""Can Alpaca corporate-actions give the terminal MERGER value (cash-per-share) for delisted-by-acquisition
names? Probe CELG (cash+stock merger w/ BMS Nov 2019) + a few. NEVER prints creds."""
from __future__ import annotations

import datetime as dt
import os

from quantlib.corporate_actions import fetch_corporate_actions
from quantlib.data.corporate_actions_backfill import corporate_actions_client

client = corporate_actions_client()
for sym, win in {
    "CELG": ("2019-09-01", "2019-12-31"),
    "WCG": ("2019-12-01", "2020-02-28"),
    "MXIM": ("2021-07-01", "2021-09-30"),
    "WORK": ("2021-06-01", "2021-08-31"),
}.items():
    acts = fetch_corporate_actions(client, [sym], dt.date.fromisoformat(win[0]), dt.date.fromisoformat(win[1]))
    merg = [a for a in acts if "merger" in a.action_type]
    print(f"  {sym}: {len(acts)} actions in window; merger actions:")
    for a in merg:
        print(f"      type={a.action_type} ex_date={a.ex_date} cash_rate={a.cash_rate}")
    if not merg:
        print(f"      (no merger action; types seen: {sorted(set(a.action_type for a in acts))})")
