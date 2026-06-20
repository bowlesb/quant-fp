"""Verify the survivorship facts that gate the weekly-reversal pre-reg (run before any $-number).
Measures: do any deep-panel symbols stop printing early (in-sample delistings)? what is the start-date spread?
RESULT (2026-06-20, seed 7/11, 600-sym samples): 0/600 stop printing before 2026-06-17 (PERFECTLY
survivors-only -> ZERO in-sample delistings -> the haircut MUST be externally calibrated, not in-panel);
~99% have full history to 2016, ~1.2% are later IPOs. READ-ONLY."""
from __future__ import annotations

import glob
import random

import numpy as np

STORE = "/store"


def main() -> None:
    syms = [p.split("symbol=")[1].split("/")[0] for p in glob.glob(f"{STORE}/raw/bars/symbol=*")]
    print(f"total bar symbols: {len(syms)}", flush=True)
    random.seed(7)
    sample = random.sample(syms, min(600, len(syms)))
    ends, starts = [], []
    for sym in sample:
        days = sorted(p.split("date=")[1].split("/")[0] for p in glob.glob(f"{STORE}/raw/bars/symbol={sym}/date=*"))
        if days:
            starts.append(days[0])
            ends.append(days[-1])
    ends_arr = np.array(sorted(ends))
    starts_arr = np.array(sorted(starts))
    print(f"sampled with bars: {len(ends_arr)}", flush=True)
    for cut in ["2024-01-01", "2025-01-01", "2025-06-01", "2026-01-01", "2026-05-01"]:
        print(f"  last-bar < {cut}: {int(np.sum(ends_arr < cut))}/{len(ends_arr)} "
              f"({100 * np.mean(ends_arr < cut):.1f}%)")
    print(f"earliest last-bar {ends_arr[0]}  latest {ends_arr[-1]}")
    for cut in ["2016-02-01", "2018-01-01", "2020-01-01", "2022-01-01", "2024-01-01"]:
        print(f"  start > {cut} (not present): {int(np.sum(starts_arr > cut))}/{len(starts_arr)} "
              f"({100 * np.mean(starts_arr > cut):.1f}%)")


if __name__ == "__main__":
    main()
