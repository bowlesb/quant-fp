"""Pre-run POWER CHECK (no $-numbers): how many names clear the realized half-spread thresholds per date,
so the universe boundary is decided BEFORE any P&L. Per the pre-reg + Lead: <2.0bps if >=~100 names/date,
else fall back to the pre-committed <3.0bps boundary."""
from __future__ import annotations

import datetime as dt
import glob
import os
from zoneinfo import ZoneInfo

import numpy as np
import polars as pl

from quantlib.data.realized_cost import realized_half_spread_bps

STORE = "/store"
ENTRY_ET = 9 * 60 + 40
N_DATES = 42
UNIVERSE_TOP = 400  # measure over a wide liquid pool so the count isn't capped artificially


def group_vdir(group: str) -> str | None:
    cand = sorted(glob.glob(f"{STORE}/group={group}/v=*"))
    return cand[-1] if cand else None


def entry_ts(day: str) -> dt.datetime:
    d = dt.date.fromisoformat(day)
    return dt.datetime(
        d.year, d.month, d.day, ENTRY_ET // 60, ENTRY_ET % 60, tzinfo=ZoneInfo("America/New_York")
    ).astimezone(dt.timezone.utc)


def covered_days(vdir: str, min_syms: int = 500) -> list[str]:
    days = sorted(os.path.basename(p).replace("date=", "") for p in glob.glob(f"{vdir}/source=backfill/date=*"))
    good = []
    for day in days:
        paths = glob.glob(f"{vdir}/source=backfill/date={day}/*.parquet")
        if paths and pl.read_parquet(paths[0], columns=["symbol"])["symbol"].n_unique() >= min_syms:
            good.append(day)
    return good


def liquid_universe(day: str, n: int) -> list[str]:
    lazy = pl.scan_parquet(f"{STORE}/raw/bars/symbol=*/date={day}/*.parquet", hive_partitioning=True).select(
        ["symbol", "ts", "close", "volume"]
    )
    et = pl.col("ts").dt.convert_time_zone("America/New_York")
    minute = et.dt.hour().cast(pl.Int32) * 60 + et.dt.minute().cast(pl.Int32)
    return (
        lazy.filter((minute >= 9 * 60 + 30) & (minute < 16 * 60))
        .group_by("symbol")
        .agg((pl.col("close") * pl.col("volume")).sum().alias("dv"))
        .sort("dv", descending=True)
        .head(n)
        .collect()["symbol"]
        .to_list()
    )


def main() -> None:
    vdir = group_vdir("volatility")
    days = covered_days(vdir, 500)[-N_DATES:]
    print(f"power check: {len(days)} dates {days[0]}..{days[-1]}, pool top-{UNIVERSE_TOP} liquid", flush=True)
    n_lt2, n_lt3, n_measured = [], [], []
    for day in days:
        syms = liquid_universe(day, UNIVERSE_TOP)
        et = entry_ts(day)
        rc = realized_half_spread_bps(STORE, day, syms, et)
        if rc.height == 0:
            continue
        hs = rc["realized_half_spread_bps"].to_numpy()
        n_measured.append(int(rc.height))
        n_lt2.append(int(np.sum(hs < 2.0)))
        n_lt3.append(int(np.sum(hs < 3.0)))
    print(f"\nper-date counts over {len(n_lt2)} dates:")
    print(f"  measured (has realized spread): median={int(np.median(n_measured))} min={min(n_measured)} max={max(n_measured)}")
    print(f"  <2.0bps: median={int(np.median(n_lt2))} min={min(n_lt2)} max={max(n_lt2)} mean={np.mean(n_lt2):.0f}")
    print(f"  <3.0bps: median={int(np.median(n_lt3))} min={min(n_lt3)} max={max(n_lt3)} mean={np.mean(n_lt3):.0f}")
    med2 = float(np.median(n_lt2))
    print(f"\nPRE-COMMITTED DECISION: <2.0bps median/date = {med2:.0f} -> "
          f"{'USE <2.0bps (>=100, adequate)' if med2 >= 100 else 'TOO THIN -> fall back to <3.0bps'}")


if __name__ == "__main__":
    main()
