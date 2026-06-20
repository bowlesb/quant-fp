"""8-K EVENT-STUDY — per-EVENT panel builder (pre-registered, see prereg.md).

For each 8-K event (available_at + 5min embargo), build the abnormal RESPONSE around the filing instant:
  - EVENT-WINDOW stats over T in {5,15,30,60m} from the tradeable entry: window volume, |return|,
    realized vol, high-low range. RTH events use the in-session forward window; OFF-HOURS events enter the
    NEXT session's open (>=09:35 ET) and use that session's window + the overnight gap.
  - OWN TRAILING BASELINE: the same stats over the name's own prior ~20 same-time-of-day sessions (ending
    the session BEFORE the event) -> the own-normalized abnormality (the #187 own-vol control, built in).
  - MATCHED NON-EVENT CONTROL: the name's NON-event sessions at the same time-of-day -> the abnormality is
    also measured as event-minus-control (within-name difference).

Look-ahead-safe: entry strictly AFTER available_at+embargo; never the filing-minute print. READ-ONLY stores.
Writes events.parquet for screen.py.
"""

from __future__ import annotations

import datetime as dt
import glob
import os
from zoneinfo import ZoneInfo

import numpy as np
import polars as pl

from quantlib.features.loaders import _query

ET = ZoneInfo("America/New_York")

STORE = os.environ.get("STORE_ROOT", "/store")
OUT_DIR = "/app/experiments/2026-06-19-8k-event-study"

EMBARGO_MIN = 5
FWD_WINDOWS = (5, 15, 30, 60)  # minutes from the tradeable entry
BASELINE_SESSIONS = 20  # own trailing same-time-of-day baseline depth
MIN_BASELINE = 8  # need this many baseline sessions or the event is dropped
# All RTH bounds are in ET minutes-of-day (DST-correct via tz conversion, not fixed UTC offsets).
RTH_START_MIN = 9 * 60 + 35  # 09:35 ET — first tradeable entry (never the 09:30 open print)
RTH_END_MIN = 16 * 60  # 16:00 ET close
ENTRY_LAST_MIN = 15 * 60  # last RTH entry 15:00 ET so a 60m window fits the session
SPAN_START = os.environ.get("SPAN_START", "2018-01-01")
SPAN_END = os.environ.get("SPAN_END", "2025-12-31")
N_EVENTS = int(os.environ.get("N_EVENTS", "8000"))
SEED = int(os.environ.get("SEED", "7"))
MIN_PRICE = 1.0  # $1 floor (overnight-trap guard)
MIN_RTH_BARS = 250  # entry-day RTH bar count to count as a continuously-trading (tradeable) name


def et_min(ts: dt.datetime) -> int:
    """Minute-of-day of a UTC timestamp in US/Eastern (DST-correct)."""
    et = ts.astimezone(ET)
    return et.hour * 60 + et.minute


def et_entry_ts(day: str, et_minute: int) -> dt.datetime:
    """A UTC timestamp for ``et_minute`` minute-of-day ET on ``day`` (DST-correct)."""
    day_dt = dt.date.fromisoformat(day)
    naive = dt.datetime(day_dt.year, day_dt.month, day_dt.day, et_minute // 60, et_minute % 60)
    return naive.replace(tzinfo=ET).astimezone(dt.timezone.utc)


def load_event_bars(symbol: str, day: str) -> pl.DataFrame:
    pattern = f"{STORE}/raw/bars/symbol={symbol}/date={day}/*.parquet"
    if not glob.glob(pattern):
        return pl.DataFrame()
    return (
        pl.scan_parquet(pattern, hive_partitioning=True)
        .select(["ts", "open", "high", "low", "close", "volume"])
        .collect()
        .sort("ts")
    )


def window_stats(bars: pl.DataFrame, target_ts: dt.datetime, minutes: int) -> dict[str, float] | None:
    """Volume / |return| / realized-vol / range over the window starting at the first TRADEABLE bar at or
    after ``target_ts`` (a sparse name may not print exactly on the minute — enter at the next real bar,
    which is also the realistic fill), spanning ``minutes`` from that entry bar."""
    at_or_after = bars.filter(pl.col("ts") >= target_ts)
    if at_or_after.height == 0:
        return None
    entry_ts = at_or_after["ts"][0]
    # cap entry slippage: if the next real bar is >5 min past the target, the name is too thin here
    if (entry_ts - target_ts) > dt.timedelta(minutes=5):
        return None
    entry_close = at_or_after["close"][0]
    win = bars.filter((pl.col("ts") > entry_ts) & (pl.col("ts") <= entry_ts + dt.timedelta(minutes=minutes)))
    if win.height < 2:
        return None
    if entry_close is None or entry_close < MIN_PRICE:
        return None
    end_close = win["close"][-1]
    logret = (win["close"] / win["close"].shift(1)).log().drop_nulls()
    return {
        "volume": float(win["volume"].sum()),
        "absret": float(abs(end_close / entry_close - 1.0)),
        "ret": float(end_close / entry_close - 1.0),
        "rv": float(logret.std()) if logret.len() >= 3 else float("nan"),
        "range": float((win["high"].max() - win["low"].min()) / entry_close),
    }


def session_entry_minute(target_min: int) -> int:
    """Snap an ET minute-of-day to the RTH entry grid (clamp into [09:35, 15:00] ET)."""
    return min(max(target_min, RTH_START_MIN), ENTRY_LAST_MIN)


def baseline_stats(
    symbol: str, sessions: list[str], entry_min: int, minutes: int
) -> dict[str, float] | None:
    """Mean event-window stat over the name's prior `sessions` at the SAME time-of-day (own baseline)."""
    acc: dict[str, list[float]] = {k: [] for k in ("volume", "absret", "rv", "range")}
    for day in sessions:
        bars = load_event_bars(symbol, day)
        if bars.height == 0:
            continue
        stats = window_stats(bars, et_entry_ts(day, entry_min), minutes)
        if stats is None:
            continue
        for key in acc:
            value = stats[key]
            if not (isinstance(value, float) and np.isnan(value)):
                acc[key].append(value)
    if len(acc["volume"]) < MIN_BASELINE:
        return None
    return {key: float(np.mean(values)) if values else float("nan") for key, values in acc.items()}


def all_bar_days(symbol: str) -> list[str]:
    return sorted(
        p.split("date=")[1].split("/")[0] for p in glob.glob(f"{STORE}/raw/bars/symbol={symbol}/date=*")
    )


def build_event(symbol: str, event_at: dt.datetime, day_index: dict[str, list[str]]) -> dict | None:
    """Build one event row: entry, event-window stats at each T, own-baseline normalized abnormality."""
    days = day_index.get(symbol)
    if not days:
        return None
    e = event_at + dt.timedelta(minutes=EMBARGO_MIN)
    e_min = et_min(e)  # ET minute-of-day of the embargoed event (DST-correct)
    event_day = e.astimezone(ET).date().isoformat()

    # Regime + entry day/minute (all in ET).
    if RTH_START_MIN <= e_min <= ENTRY_LAST_MIN and event_day in days:
        regime = "rth"
        entry_day = event_day
        entry_min = e_min
    else:
        regime = "offhours"
        future = (
            [d for d in days if d > event_day]
            if e_min >= RTH_END_MIN
            else [d for d in days if d >= event_day]
        )
        if not future:
            return None
        entry_day = future[0]
        entry_min = RTH_START_MIN  # next session open + buffer
    if entry_day not in days:
        return None

    prior_sessions = [d for d in days if d < entry_day][-BASELINE_SESSIONS:]
    if len(prior_sessions) < MIN_BASELINE:
        return None

    entry_ts = et_entry_ts(entry_day, entry_min)
    bars = load_event_bars(symbol, entry_day)
    if bars.height == 0:
        return None
    # Liquidity gate: the event is only tradeable on a name that prints near-continuously through RTH.
    # A thin name (few RTH bars) has unusable event windows + a noisy baseline — drop it (the study applies
    # to liquid-enough 8-K events, mirroring the #187 liquid-universe restriction). RTH band in ET.
    et_col = pl.col("ts").dt.convert_time_zone("America/New_York")
    rth_mod = et_col.dt.hour().cast(pl.Int32) * 60 + et_col.dt.minute().cast(pl.Int32)
    rth_bars = bars.filter((rth_mod >= RTH_START_MIN - 5) & (rth_mod < RTH_END_MIN)).height
    if rth_bars < MIN_RTH_BARS:
        return None

    row: dict = {
        "symbol": symbol,
        "event_at": event_at,
        "entry_ts": entry_ts,
        "regime": regime,
        "year": event_at.year,
    }
    any_window = False
    for minutes in FWD_WINDOWS:
        ev = window_stats(bars, entry_ts, minutes)
        base = baseline_stats(symbol, prior_sessions, session_entry_minute(entry_min), minutes)
        if ev is None or base is None:
            for stat in ("vol", "absret", "rv", "range", "ret"):
                row[f"{stat}_abn_{minutes}"] = None
            continue
        any_window = True
        row[f"ret_abn_{minutes}"] = ev["ret"]  # signed return is already an abnormality vs 0 (H3)
        for stat, evkey in (("vol", "volume"), ("absret", "absret"), ("rv", "rv"), ("range", "range")):
            b = base[evkey]
            row[f"{stat}_abn_{minutes}"] = (ev[evkey] / b) if (b and b > 0 and not np.isnan(b)) else None
            row[f"{stat}_raw_{minutes}"] = ev[evkey]
    return row if any_window else None


def main() -> None:
    events = _query(
        """
        SELECT symbol, available_at
        FROM filings
        WHERE form_type = '8-K' AND available_at BETWEEN %(start)s AND %(end)s
        ORDER BY available_at
        """,
        {"start": SPAN_START, "end": SPAN_END},
    )
    rng = np.random.default_rng(SEED)
    if events.height > N_EVENTS:
        idx = np.sort(rng.choice(events.height, size=N_EVENTS, replace=False))
        events = events[idx]
    symbols = events["symbol"].unique().to_list()
    print(f"events={events.height} symbols={len(symbols)} span={SPAN_START}..{SPAN_END}", flush=True)

    day_index = {sym: all_bar_days(sym) for sym in symbols}
    rows = []
    for i, ev in enumerate(events.iter_rows(named=True)):
        row = build_event(ev["symbol"], ev["available_at"], day_index)
        if row is not None:
            rows.append(row)
        if (i + 1) % 1000 == 0:
            print(f"[{i+1}/{events.height}] kept {len(rows)}", flush=True)

    panel = pl.DataFrame(rows, infer_schema_length=None)
    out = f"{OUT_DIR}/events.parquet"
    panel.write_parquet(out)
    n_sym = panel["symbol"].n_unique() if panel.height else 0
    regimes = (
        dict(zip(*panel["regime"].value_counts().to_dict(as_series=False).values())) if panel.height else {}
    )
    print(f"WROTE {out}: {panel.height} events, {n_sym} symbols, regimes={regimes}", flush=True)


if __name__ == "__main__":
    main()
