"""On-disk contract for the option-IV panel — partition layout + resumable manifest.

A SEPARATE namespace from the bar/trade/quote raw tape (``raw/<bars|trades|quotes>``) because the row
grain is different: one row per (underlying, observation-date, OCC-contract) carrying a RECONSTRUCTED
implied vol + greeks, not a raw tick. Alpaca does NOT serve historical IV/greeks (the option chain /
snapshot endpoints are current-snapshot-only); the historical surface it DOES serve is option bars /
trades on a specific OCC contract since ~2024-02. So this panel is BUILT, not downloaded: per contract we
fetch the historical daily option bar (close = the end-of-session option mark), pull the underlying spot
for the same session, and invert Black-Scholes to recover IV + the first-order greeks. The store records
the inputs alongside the reconstructed values so the inversion is auditable and re-derivable.

Point-in-time correctness (golden-set-safe): every row carries ``available_at`` = the session close
instant the option-bar close is known (16:00 ET / 21:00 UTC for the daily mark). A study reading this
panel as-of time T must filter ``available_at <= T``; an end-of-day option mark for date D is not
knowable until D's close, so available_at is never earlier than the bar it summarises.

Layout:  <store>/option_iv/underlying=<U>/date=<YYYY-MM-DD>/data.parquet
Manifest: <store>/option_iv/_manifest_option_iv.d/part-*.parquet  (append-only parts, unioned on read)

The (underlying, date) partition grain mirrors the raw tape's (symbol, date) grain so the same
manifest-driven "skip what's on disk, never double-acquire" resume logic applies verbatim.
"""

from __future__ import annotations

import datetime as dt
import glob
import logging
import os

import polars as pl

logger = logging.getLogger("option_iv_store")

TIER = "option_iv"

MANIFEST_SCHEMA: dict[str, pl.DataType] = {
    "underlying": pl.String,
    "date": pl.String,
    "n_contracts": pl.Int64,
    "n_iv_valid": pl.Int64,
    "bytes": pl.Int64,
    "fetched_at": pl.Datetime("us", "UTC"),
}

OPTION_IV_SCHEMA: dict[str, pl.DataType] = {
    "underlying": pl.String,
    "date": pl.String,
    "occ": pl.String,
    "expiration": pl.String,
    "right": pl.String,  # "C" / "P"
    "strike": pl.Float64,
    "dte": pl.Int64,  # calendar days to expiry from the observation date
    "moneyness": pl.Float64,  # strike / spot
    "spot": pl.Float64,  # underlying close for the observation session
    "option_close": pl.Float64,  # the option daily-bar close = end-of-session mark
    "option_volume": pl.Int64,
    "rate": pl.Float64,  # risk-free rate assumption used in the inversion
    "implied_vol": pl.Float64,  # BS-inverted from option_close, spot, strike, dte, rate
    "delta": pl.Float64,
    "gamma": pl.Float64,
    "vega": pl.Float64,
    "theta": pl.Float64,
    "iv_status": pl.String,  # "ok" | "no_solution" (price off the [1e-4, 5.0] model grid / below floor)
    "available_at": pl.Datetime("us", "UTC"),  # when this end-of-day mark first becomes knowable
}


def partition_dir(store: str, underlying: str, day: dt.date) -> str:
    return os.path.join(store, TIER, f"underlying={underlying}", f"date={day.isoformat()}")


def manifest_dir(store: str) -> str:
    """Directory of append-only manifest PART files — recording a partition is O(part), not O(total)."""
    return os.path.join(store, TIER, f"_manifest_{TIER}.d")


def load_manifest(store: str) -> pl.DataFrame:
    """Union every append-only manifest part — resume sees every prior (underlying, date) fetch."""
    frames = []
    parts_dir = manifest_dir(store)
    if os.path.isdir(parts_dir):
        for name in sorted(os.listdir(parts_dir)):
            if name.endswith(".parquet"):
                frames.append(pl.read_parquet(os.path.join(parts_dir, name)))
    if not frames:
        return pl.DataFrame(schema=MANIFEST_SCHEMA)
    return pl.concat(frames).unique(subset=["underlying", "date"], keep="last")


def done_keys(store: str) -> set[tuple[str, str]]:
    """(underlying, date) pairs already on disk with rows>0 — the no-double-acquire resume set.

    rows==0 manifest entries are NOT treated as done (mirrors the raw tape's poison-entry guard: a
    premature/empty write must not permanently block a real re-fetch)."""
    manifest = load_manifest(store)
    if manifest.height == 0:
        return set()
    real = manifest.filter(pl.col("n_contracts") > 0)
    return {(row["underlying"], row["date"]) for row in real.iter_rows(named=True)}


def write_partition(store: str, underlying: str, day: dt.date, frame: pl.DataFrame) -> int:
    """Write one (underlying, date) partition + append a manifest part. Returns bytes written."""
    target = partition_dir(store, underlying, day)
    os.makedirs(target, exist_ok=True)
    path = os.path.join(target, "data.parquet")
    frame.write_parquet(path)
    nbytes = os.path.getsize(path)
    n_valid = int(frame.filter(pl.col("iv_status") == "ok").height)
    parts_dir = manifest_dir(store)
    os.makedirs(parts_dir, exist_ok=True)
    manifest_row = pl.DataFrame(
        {
            "underlying": [underlying],
            "date": [day.isoformat()],
            "n_contracts": [int(frame.height)],
            "n_iv_valid": [n_valid],
            "bytes": [nbytes],
            "fetched_at": [dt.datetime.now(dt.timezone.utc)],
        },
        schema=MANIFEST_SCHEMA,
    )
    part_name = (
        f"part-{dt.datetime.now(dt.timezone.utc):%Y%m%dT%H%M%S%f}-{underlying}-{day.isoformat()}.parquet"
    )
    manifest_row.write_parquet(os.path.join(parts_dir, part_name))
    return nbytes


def load_panel(store: str, underlying: str | None = None) -> pl.DataFrame:
    """Read the option-IV panel (one underlying or all). Empty (full schema) when no partitions."""
    under_glob = f"underlying={underlying}" if underlying else "underlying=*"
    pattern = os.path.join(store, TIER, under_glob, "date=*", "*.parquet")
    if not glob.glob(pattern):
        return pl.DataFrame(schema=OPTION_IV_SCHEMA)
    return pl.read_parquet(pattern)
