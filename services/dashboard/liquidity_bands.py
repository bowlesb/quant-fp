"""Canonical ADV-rank / liquidity-band reference surface for the dashboard.

Every research lane re-derives its own liquidity partition from raw dollar-volume — Lane C's overnight
boundary used ADV-rank bands B1-B5, FeatureInventor screens on "top-400 liquid", the deep-raw pilot pulls
"top-500 by ADV". They all mean the same thing (rank symbols by trailing dollar volume, cut into bands) but
each hand-rolls the cut, so "which band is AAPL in / how big is B4 / is the membership stable" has no single
answer. This module is that single answer: ONE canonical ADV → rank → band map, read-side only, that the
dashboard renders and agents query, so a lane can reference it instead of re-deriving.

DEFINITION (mirrors experiments/2026-06-19-laneC-scope-horizon/build_bands.py — the band hypothesis that has
already been adjudicated, so this canonicalizes the EXISTING convention, it does not invent a new one):

  * RTH dollar volume, per (symbol, date) = sum(close * volume) over the regular-session minutes
    (13:30..19:59 UTC = 09:30..15:59 ET). The raw-bar daily liquidity reduction.
  * ADV (point-in-time), per (symbol, date) = trailing-20d rolling mean of RTH dollar volume (raw $).
  * Stable per-symbol ADV = mean of the trailing-20d ADV over the symbol's valid days; a symbol needs
    >= MIN_DAYS_FOR_RANK (60) valid days to receive a stable cross-sectional rank (so a 3-day-old listing
    doesn't get a spurious rank). Symbols are ranked descending by stable ADV (rank 1 = most liquid).
  * BANDS = the pre-declared contiguous ADV-rank ranges (lo inclusive, hi exclusive) B1..B5.

SOURCE — the raw minute bars (``<store>/raw/bars``), reduced over a BOUNDED recent window. Unlike the raw-
coverage surface (a pure manifest read), ADV needs the dollar-volume bodies, which are not in the manifest;
so this reads bars, but only over the most-recent ``DEFAULT_WINDOW_DAYS`` trading dates — enough for the
60-day rank floor + the 20-day ADV warmup — NOT the full 18-month tape. The store is mounted read-only, so
nothing is written; the ~25-30s cold reduction is amortized behind a long TTL cache (the bands only move on
the daily acquire). NO schema/format change, no live feature def, no fingerprint surface.

DEPTH vs STABILITY, the two questions this answers:
  * COMPOSITION — how many symbols sit in each band today, and their ADV range / boundaries (so "B4 = ADV
    rank 2000-4000, median ADV $4.6M, min $0.9M" reads off at a glance).
  * MEMBERSHIP STABILITY — band turnover over the window: of the symbols in band B today, what fraction were
    in the SAME band N days ago (so a lane knows whether a band is a stable universe or churns).
"""

from __future__ import annotations

import datetime as dt
import glob
import os
import time

import polars as pl

STORE_ROOT = os.environ.get("STORE_ROOT", "/store")

# Regular-session minute window in UTC HHMM (09:30..15:59 ET), matching the Lane C daily reduction. The
# dollar-volume sum is taken over [RTH_START_HM, RTH_END_HM] inclusive.
RTH_START_HM = 1330
RTH_END_HM = 1959

# Trailing window for the point-in-time ADV (rolling mean of RTH dollar volume), in trading days.
ADV_WINDOW = 20

# A symbol needs at least this many valid (ADV-defined) days to receive a stable cross-sectional rank — so a
# freshly-listed name doesn't get a spurious rank off a handful of days. Mirrors build_bands.MIN_DAYS_FOR_RANK.
MIN_DAYS_FOR_RANK = 60

# How many recent trading dates to reduce: the 60-day rank floor + the 20-day ADV warmup + headroom. Bounds
# the raw-bar read to a snappy-enough cold build instead of the full 18-month tape.
DEFAULT_WINDOW_DAYS = 85

# The canonical ADV-rank bands (lo inclusive, hi exclusive), from experiments/2026-06-19-laneC-scope-horizon/
# build_bands.py BANDS — the partition the overnight boundary adjudication already ran on. Keep these in
# lock-step with that file: this surface is the canonical READOUT of that convention, not a competing one.
BANDS: list[tuple[str, str, int, int]] = [
    ("B1", "rank 1-500 (most liquid)", 1, 501),
    ("B2", "rank 500-1000", 501, 1001),
    ("B3", "rank 1000-2000", 1001, 2001),
    ("B4", "rank 2000-4000 (small-cap)", 2001, 4001),
    ("B5", "rank 4000-6000 (micro)", 4001, 6001),
]

# Stability snapshot offsets (trading days back) — "of today's band members, what fraction shared the band
# this many days ago". 5d ~ a week, 20d ~ a month.
STABILITY_LOOKBACKS = [5, 20]


def _store_dates(root: str) -> list[str]:
    """Sorted distinct trading dates present in the raw bar store (cheap glob over the partition dirs)."""
    date_dirs = glob.glob(os.path.join(root, "raw", "bars", "symbol=*", "date=*"))
    return sorted({os.path.basename(path).replace("date=", "") for path in date_dirs})


def _daily_dollar_vol(root: str, date_iso: str) -> pl.DataFrame:
    """Per-symbol RTH dollar volume for one date: sum(close * volume) over the regular session. ``symbol`` is
    a real column in the raw bars, so hive partitioning is OFF (turning it on collides with that column and
    silently zeroes the read)."""
    pattern = os.path.join(root, "raw", "bars", "symbol=*", f"date={date_iso}", "*.parquet")
    if not glob.glob(pattern):
        return pl.DataFrame(schema={"symbol": pl.String, "rth_dollar_vol": pl.Float64, "date": pl.String})
    return (
        pl.scan_parquet(pattern, hive_partitioning=False)
        .select(["symbol", "ts", "close", "volume"])
        .with_columns(
            (pl.col("ts").dt.hour().cast(pl.Int32) * 100 + pl.col("ts").dt.minute().cast(pl.Int32)).alias(
                "hm"
            )
        )
        .filter((pl.col("hm") >= RTH_START_HM) & (pl.col("hm") <= RTH_END_HM))
        .group_by("symbol")
        .agg((pl.col("close") * pl.col("volume")).sum().alias("rth_dollar_vol"))
        .with_columns(pl.lit(date_iso).alias("date"))
        .collect()
    )


def build_daily_table(root: str, window_days: int = DEFAULT_WINDOW_DAYS) -> pl.DataFrame:
    """Per (symbol, date) RTH dollar volume over the most-recent ``window_days`` trading dates. The only
    raw-bar read; everything downstream is in-memory over this compact table."""
    dates = _store_dates(root)
    window = dates[-window_days:] if window_days > 0 else dates
    frames = [frame for frame in (_daily_dollar_vol(root, date_iso) for date_iso in window) if frame.height]
    if not frames:
        return pl.DataFrame(schema={"symbol": pl.String, "rth_dollar_vol": pl.Float64, "date": pl.String})
    return pl.concat(frames, how="vertical")


def rank_table(daily: pl.DataFrame) -> pl.DataFrame:
    """Per-(symbol, date) trailing-20d ADV, plus each symbol's stable cross-sectional rank.

    Returns one row per (symbol, date) with: ``adv20`` (point-in-time trailing-20d ADV), ``adv`` (the
    symbol's stable ADV = mean adv20 over its valid days), ``rank`` (descending by stable ADV, rank 1 = most
    liquid), and ``band`` (the canonical band label, or None for symbols below the rank floor / unbanded
    tail). Only (symbol, date) cells with a defined trailing-20d ADV are kept.
    """
    if daily.height == 0:
        return daily.with_columns(
            pl.lit(None, dtype=pl.Float64).alias("adv20"),
            pl.lit(None, dtype=pl.Float64).alias("adv"),
            pl.lit(None, dtype=pl.UInt32).alias("rank"),
            pl.lit(None, dtype=pl.String).alias("band"),
            pl.lit(None, dtype=pl.UInt32).alias("pit_rank"),
            pl.lit(None, dtype=pl.String).alias("pit_band"),
        )
    adv = (
        daily.sort(["symbol", "date"])
        .with_columns(pl.col("rth_dollar_vol").rolling_mean(ADV_WINDOW).over("symbol").alias("adv20"))
        .filter(pl.col("adv20").is_not_null())
    )
    stable = (
        adv.group_by("symbol")
        .agg(pl.col("adv20").mean().alias("adv"), pl.len().alias("n_valid_days"))
        .filter(pl.col("n_valid_days") >= MIN_DAYS_FOR_RANK)
        .sort("adv", descending=True)
        .with_row_index("rank", offset=1)
    )
    stable = stable.with_columns(_band_expr(pl.col("rank")).alias("band"))
    ranked = adv.join(stable.select("symbol", "adv", "rank", "band"), on="symbol", how="inner")
    # POINT-IN-TIME band: rank each date's symbols by THAT day's adv20 and band them, so band membership can
    # migrate day to day. The stable ``band`` (one per symbol, Lane C's convention) drives the headline
    # composition; ``pit_band`` drives the turnover/stability view, which would be a trivial ~100% if it used
    # the stable rank (a symbol's stable band is constant across the window by construction).
    pit_rank = (
        ranked.sort(["date", "adv20"], descending=[False, True])
        .with_columns(pl.col("adv20").rank("ordinal", descending=True).over("date").alias("pit_rank"))
        .with_columns(_band_expr(pl.col("pit_rank")).alias("pit_band"))
    )
    return pit_rank


def _band_expr(rank: pl.Expr) -> pl.Expr:
    """Map an ADV rank to its canonical band label (None outside every band's range)."""
    expr = pl.lit(None, dtype=pl.String)
    for name, _label, lo, hi in BANDS:
        expr = pl.when((rank >= lo) & (rank < hi)).then(pl.lit(name)).otherwise(expr)
    return expr


def _band_composition(ranked: pl.DataFrame, anchor_date: str) -> list[dict[str, object]]:
    """Per-band membership + ADV range on the anchor (latest) date — today's band sizes and boundaries."""
    today = ranked.filter(pl.col("date") == anchor_date)
    composition: list[dict[str, object]] = []
    for name, label, lo, hi in BANDS:
        members = today.filter(pl.col("band") == name)
        composition.append(
            {
                "band": name,
                "label": label,
                "rank_lo": lo,
                "rank_hi": hi - 1,
                "n_symbols": members.height,
                "adv_min": _opt_float(members, "adv", "min"),
                "adv_median": _opt_float(members, "adv", "median"),
                "adv_max": _opt_float(members, "adv", "max"),
            }
        )
    return composition


def _opt_float(frame: pl.DataFrame, column: str, agg: str) -> float | None:
    """A float aggregate of ``column`` (min/median/max), or None on an empty band — kept tidy so a band with
    no members on the anchor date reads as null, not 0 (0 ADV would imply a $0-volume name, which is wrong).
    """
    if frame.height == 0:
        return None
    value = getattr(frame[column], agg)()
    return round(float(value), 2) if value is not None else None


def _band_stability(ranked: pl.DataFrame, anchor_date: str, dates: list[str]) -> list[dict[str, object]]:
    """Per-band membership turnover using the POINT-IN-TIME band (``pit_band`` — each date's own adv20 rank):
    of the symbols in band B on the anchor date, what fraction were in the SAME band ``lookback`` trading days
    earlier. High retained-fraction = a stable universe a lane can treat as fixed; low = real liquidity churn
    across the boundary. (The stable-rank band would read a trivial ~100% here by construction.)"""
    anchor_idx = dates.index(anchor_date)
    today = ranked.filter(pl.col("date") == anchor_date).select("symbol", "pit_band")
    stability: list[dict[str, object]] = []
    for name, _label, _lo, _hi in BANDS:
        today_members = set(today.filter(pl.col("pit_band") == name)["symbol"].to_list())
        row: dict[str, object] = {"band": name, "n_today": len(today_members)}
        for lookback in STABILITY_LOOKBACKS:
            past_idx = anchor_idx - lookback
            if past_idx < 0 or not today_members:
                row[f"retained_{lookback}d_pct"] = None
                continue
            past_date = dates[past_idx]
            past_members = set(
                ranked.filter((pl.col("date") == past_date) & (pl.col("pit_band") == name))[
                    "symbol"
                ].to_list()
            )
            retained = len(today_members & past_members)
            row[f"retained_{lookback}d_pct"] = round(100.0 * retained / len(today_members), 1)
        stability.append(row)
    return stability


def build_liquidity_bands(
    root: str = STORE_ROOT, window_days: int = DEFAULT_WINDOW_DAYS
) -> dict[str, object]:
    """The canonical ADV-rank / liquidity-band reference surface.

    Reduces the most-recent ``window_days`` trading dates of raw minute bars to per-symbol daily RTH dollar
    volume, computes trailing-20d ADV + each symbol's stable cross-sectional rank, assigns the canonical
    bands, and returns per-band composition (sizes + ADV ranges on the anchor date) + membership stability
    (turnover over the window). Read-side only; nothing is written.

    Shape (see docs/LIQUIDITY_BANDS.md):
      {generated_at, store_root, window_days, anchor_date, window_first, window_last,
       n_dates, n_ranked_symbols, adv_window, min_days_for_rank,
       bands: [{band, label, rank_lo, rank_hi, n_symbols, adv_min, adv_median, adv_max}],
       stability: [{band, n_today, retained_5d_pct, retained_20d_pct}]}
    """
    daily = build_daily_table(root, window_days)
    ranked = rank_table(daily)
    if ranked.height == 0:
        return {
            "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "store_root": root,
            "window_days": window_days,
            "anchor_date": None,
            "window_first": None,
            "window_last": None,
            "n_dates": 0,
            "n_ranked_symbols": 0,
            "adv_window": ADV_WINDOW,
            "min_days_for_rank": MIN_DAYS_FOR_RANK,
            "bands": [
                {
                    "band": name,
                    "label": label,
                    "rank_lo": lo,
                    "rank_hi": hi - 1,
                    "n_symbols": 0,
                    "adv_min": None,
                    "adv_median": None,
                    "adv_max": None,
                }
                for name, label, lo, hi in BANDS
            ],
            "stability": [],
        }
    dates = sorted(ranked["date"].unique().to_list())
    anchor_date = dates[-1]
    return {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "store_root": root,
        "window_days": window_days,
        "anchor_date": anchor_date,
        "window_first": dates[0],
        "window_last": anchor_date,
        "n_dates": len(dates),
        "n_ranked_symbols": ranked.select("symbol").n_unique(),
        "adv_window": ADV_WINDOW,
        "min_days_for_rank": MIN_DAYS_FOR_RANK,
        "bands": _band_composition(ranked, anchor_date),
        "stability": _band_stability(ranked, anchor_date, dates),
    }


def lookup_symbol(
    symbol: str, root: str = STORE_ROOT, window_days: int = DEFAULT_WINDOW_DAYS
) -> dict[str, object]:
    """One symbol's current liquidity placement: its stable ADV, cross-sectional rank, band, and the trailing-
    20d ADV on the anchor date. ``found=False`` when the symbol is below the rank floor / absent from the
    window (so a caller can distinguish "unranked tail" from "typo")."""
    daily = build_daily_table(root, window_days)
    ranked = rank_table(daily)
    upper = symbol.upper()
    rows = ranked.filter(pl.col("symbol") == upper)
    if rows.height == 0:
        return {"symbol": upper, "found": False}
    dates = sorted(ranked["date"].unique().to_list())
    anchor = rows.filter(pl.col("date") == dates[-1])
    latest_adv20 = float(anchor["adv20"][0]) if anchor.height else None
    return {
        "symbol": upper,
        "found": True,
        "rank": int(rows["rank"][0]),
        "band": rows["band"][0],
        "adv": round(float(rows["adv"][0]), 2),
        "latest_adv20": round(latest_adv20, 2) if latest_adv20 is not None else None,
        "n_valid_days": rows.height,
    }


def band_members(
    band: str, root: str = STORE_ROOT, window_days: int = DEFAULT_WINDOW_DAYS, limit: int = 250
) -> dict[str, object]:
    """The symbols in one band on the anchor date, ordered by ADV rank (most liquid first), capped at
    ``limit``. Lets a lane pull a band's universe directly instead of re-deriving the cut."""
    daily = build_daily_table(root, window_days)
    ranked = rank_table(daily)
    if ranked.height == 0:
        return {"band": band, "n_symbols": 0, "members": []}
    dates = sorted(ranked["date"].unique().to_list())
    members = (
        ranked.filter((pl.col("date") == dates[-1]) & (pl.col("band") == band))
        .sort("rank")
        .select("symbol", "rank", "adv")
    )
    rows = members.head(limit)
    return {
        "band": band,
        "n_symbols": members.height,
        "shown": rows.height,
        "members": [
            {"symbol": record["symbol"], "rank": int(record["rank"]), "adv": round(float(record["adv"]), 2)}
            for record in rows.iter_rows(named=True)
        ],
    }


class LiquidityBandsCache:
    """TTL cache for the band surface — the raw-bar reduction is ~25-30s cold (bounded window), so a long TTL
    (10 min) keeps the page responsive; the bands only move on the daily acquire, so 10 min is plenty fresh.
    Keyed by ``window_days`` so a non-default window has its own slot. The full ranked table is cached so the
    symbol-lookup / band-members views reuse it instead of re-reducing."""

    def __init__(self, ttl: float = 600.0) -> None:
        self.ttl = ttl
        self._surface: dict[int, tuple[float, dict[str, object]]] = {}
        self._ranked: dict[int, tuple[float, pl.DataFrame]] = {}

    def _ranked_table(self, root: str, window_days: int, force: bool) -> pl.DataFrame:
        now = time.monotonic()
        cached = self._ranked.get(window_days)
        if not force and cached is not None and (now - cached[0]) <= self.ttl:
            return cached[1]
        ranked = rank_table(build_daily_table(root, window_days))
        self._ranked[window_days] = (now, ranked)
        return ranked

    def surface(
        self, root: str, window_days: int = DEFAULT_WINDOW_DAYS, force: bool = False
    ) -> dict[str, object]:
        now = time.monotonic()
        cached = self._surface.get(window_days)
        if not force and cached is not None and (now - cached[0]) <= self.ttl:
            return cached[1]
        view = build_liquidity_bands(root, window_days)
        self._surface[window_days] = (now, view)
        return view

    def lookup(self, symbol: str, root: str, window_days: int = DEFAULT_WINDOW_DAYS) -> dict[str, object]:
        ranked = self._ranked_table(root, window_days, force=False)
        upper = symbol.upper()
        rows = ranked.filter(pl.col("symbol") == upper)
        if rows.height == 0:
            return {"symbol": upper, "found": False}
        dates = sorted(ranked["date"].unique().to_list())
        anchor = rows.filter(pl.col("date") == dates[-1])
        latest_adv20 = float(anchor["adv20"][0]) if anchor.height else None
        return {
            "symbol": upper,
            "found": True,
            "rank": int(rows["rank"][0]),
            "band": rows["band"][0],
            "adv": round(float(rows["adv"][0]), 2),
            "latest_adv20": round(latest_adv20, 2) if latest_adv20 is not None else None,
            "n_valid_days": rows.height,
        }

    def members(
        self, band: str, root: str, window_days: int = DEFAULT_WINDOW_DAYS, limit: int = 250
    ) -> dict[str, object]:
        ranked = self._ranked_table(root, window_days, force=False)
        if ranked.height == 0:
            return {"band": band, "n_symbols": 0, "members": []}
        dates = sorted(ranked["date"].unique().to_list())
        rows_all = (
            ranked.filter((pl.col("date") == dates[-1]) & (pl.col("band") == band))
            .sort("rank")
            .select("symbol", "rank", "adv")
        )
        rows = rows_all.head(limit)
        return {
            "band": band,
            "n_symbols": rows_all.height,
            "shown": rows.height,
            "members": [
                {
                    "symbol": record["symbol"],
                    "rank": int(record["rank"]),
                    "adv": round(float(record["adv"]), 2),
                }
                for record in rows.iter_rows(named=True)
            ],
        }


CACHE = LiquidityBandsCache()
