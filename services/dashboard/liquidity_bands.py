"""Canonical ADV-rank / liquidity-band reference surface for the dashboard.

Every research lane currently re-derives a liquidity universe ad hoc — Lane C's five contiguous
ADV-rank bands, the FeatureInventor top-400, the deep-history pilot top-500, the B4 small-cap
rank-2000-3787 overnight cohort. They all answer the SAME question ("which symbols are in liquidity
tier X, point-in-time") from the SAME raw bars, with subtly different cuts and warmup rules. This
module makes that one canonical, reusable surface so a lane can pick a *reproducible* band instead of
an ad-hoc top-N, and so the Lane C B4 niche gets a stable membership list with turnover stats.

SOURCE — the raw MINUTE BARS (``/store/raw/bars/symbol=<S>/date=<D>/data.parquet``), the same tape
``raw_coverage`` reports on and Lane C reduces. Per (symbol, date) we compute the RTH-session dollar
volume (sum of close*volume over 13:30-19:59 UTC == 09:30-15:59 ET), exactly as Lane C's stage-1
reduction. From that compact daily table we derive, POINT-IN-TIME (no look-ahead), per (symbol, date):
  * ``adv_20d``  — trailing-20d mean RTH dollar volume (the standard ADV),
  * ``adv_rank`` — its cross-sectional rank that day (1 = most liquid),
  * ``band``     — the band label for that rank under configurable contiguous cuts.

A symbol receives a rank on a date only once it has >= ``MIN_TRAILING_DAYS`` trailing daily bars (the
20d ADV is otherwise a partial window) — the same warmup discipline Lane C used, kept honest so the
deep illiquid tail that lacks history is simply unranked rather than mis-ranked.

DEFAULT BANDS reproduce Lane C's adjudicated cuts (boundary_hypothesis.md) so the B4 niche maps to a
named band here: top-500 / 500-1000 / 1000-2000 / 2000-4000 / 4000+. Cuts are configurable via the API
(``?cuts=500,1000,2000,4000``) so a lane can carve its own reproducible universe without re-deriving
the ADV scan.

This is READ-SIDE and CHEAP-ish: stage-1 (the only heavy part, a glob-scan of the bar bodies per date)
is cached to a compact parquet keyed by the latest raw date, so it rebuilds only when new bars land;
stage-2 is pure polars window math over the small daily table. NO schema/format change — it reads the
existing raw partitions and writes only a private ``/tmp`` cache.
"""

from __future__ import annotations

import datetime as dt
import glob
import hashlib
import os
import time

import polars as pl

STORE_ROOT = os.environ.get("STORE_ROOT", "/store")

# Where the compact stage-1 daily reduction is cached. ``/store`` is mounted read-only on the
# dashboard, so the cache lives in the container's writable tmp; it is keyed by the latest raw date
# (see ``_daily_cache_path``) so a new acquire-day invalidates it without a manual bust.
CACHE_DIR = os.environ.get("LIQUIDITY_BANDS_CACHE_DIR", "/tmp/liquidity_bands")

# RTH boundary minutes in UTC (bars are tz-aware UTC; ET RTH 09:30-16:00 == 13:30-20:00 UTC). The
# 15:59 last-minute bound matches Lane C's daily reduction so the dollar-volume is the SAME RTH window.
RTH_START_HM = 1330  # 09:30 ET open
RTH_END_HM = 1959  # 15:59 ET last RTH minute

# A symbol needs this many trailing daily bars before its 20d ADV (and therefore its rank/band) is
# considered stable. Mirrors Lane C's MIN_TRAILING_DAYS warmup — an unranked early symbol is honest,
# a mis-ranked partial-window one is not.
ADV_WINDOW = 20
MIN_TRAILING_DAYS = 21

# Default contiguous ADV-rank band cuts (upper-exclusive rank boundaries). Reproduces Lane C's
# adjudicated B1-B5 (boundary_hypothesis.md) so its B4 small-cap niche == band "2000-4000" here. The
# final open-ended band ("4000+") catches everything past the last cut.
DEFAULT_CUTS = [500, 1000, 2000, 4000]

# The daily timeline can span ~18 months; the per-date arrays stay light (a few ints per date) but cap
# the default window so a page load is snappy. Full history is opt-in via ``?days=0``.
TIMELINE_DEFAULT_DAYS = 90

# Stage-1 (the bar-body scan) is minutes cold but yields a tiny daily table; stage-2 is sub-second.
# A 5-minute TTL on the assembled view keeps a busy refresh instant while staying fresh for a surface
# that only changes on a daily acquire.
VIEW_TTL = 300.0


def band_labels(cuts: list[int]) -> list[str]:
    """Human band labels for contiguous rank cuts, e.g. cuts [500,1000] -> ['1-500','500-1000','1000+']."""
    labels = []
    prev = 1
    for cut in cuts:
        labels.append(f"{prev}-{cut}")
        prev = cut
    labels.append(f"{prev}+")
    return labels


def band_label_expr(cuts: list[int]) -> pl.Expr:
    """A polars expression mapping the integer ``adv_rank`` column to its band label under ``cuts``."""
    labels = band_labels(cuts)
    expr = pl.when(pl.col("adv_rank") <= cuts[0]).then(pl.lit(labels[0]))
    for idx in range(1, len(cuts)):
        expr = expr.when(pl.col("adv_rank") <= cuts[idx]).then(pl.lit(labels[idx]))
    return expr.otherwise(pl.lit(labels[-1]))


def all_raw_dates(root: str) -> list[str]:
    """Sorted trading dates present in the raw bar store (cheap glob over the date partitions)."""
    dates = {
        os.path.basename(path).replace("date=", "") for path in glob.glob(f"{root}/raw/bars/symbol=*/date=*")
    }
    return sorted(dates)


def reduce_one_date(root: str, date_iso: str) -> pl.DataFrame | None:
    """One row per symbol for ``date_iso``: RTH dollar volume + RTH last close.

    Memory-bounded: a single glob-scan of that date's bar partitions, filtered to the RTH window, then
    reduced. Matches Lane C's stage-1 reduction (``select`` before ``with_columns`` so the hive
    ``symbol`` partition column does not collide with the file's own ``symbol`` column)."""
    pattern = f"{root}/raw/bars/symbol=*/date={date_iso}/*.parquet"
    if not glob.glob(pattern):
        return None
    bars = (
        pl.scan_parquet(pattern, hive_partitioning=True)
        .select(["symbol", "ts", "close", "volume"])
        .with_columns(
            (pl.col("ts").dt.hour().cast(pl.Int32) * 100 + pl.col("ts").dt.minute().cast(pl.Int32)).alias(
                "hm"
            )
        )
        .filter((pl.col("hm") >= RTH_START_HM) & (pl.col("hm") <= RTH_END_HM))
        .collect()
    )
    if bars.height == 0:
        return None
    return (
        bars.group_by("symbol")
        .agg(
            (pl.col("close") * pl.col("volume").cast(pl.Float64)).sum().alias("rth_dollar_vol"),
            pl.col("close").last().alias("rth_close"),
            pl.len().alias("rth_minutes"),
        )
        .with_columns(pl.lit(date_iso).alias("date"))
    )


def _daily_cache_path(root: str, dates: list[str]) -> str:
    """Cache path keyed by the (earliest, latest) raw date span — a new acquire-day changes the key
    and forces a clean rebuild, so the cache can never serve a stale tape."""
    key = hashlib.sha1(f"{root}|{dates[0]}|{dates[-1]}|{len(dates)}".encode()).hexdigest()[:16]
    return os.path.join(CACHE_DIR, f"daily_{key}.parquet")


def build_daily_dollar_vol(root: str = STORE_ROOT, force: bool = False) -> pl.DataFrame:
    """The compact per-(symbol, date) RTH dollar-volume table — the stage-1 reduction, disk-cached.

    Columns: symbol, date, rth_dollar_vol, rth_close, rth_minutes. One glob-scan per date (the only
    heavy step); cached to a parquet keyed by the raw date span so it rebuilds only when new bars land.
    Returns an empty frame (correct schema) if the raw store has no bars yet."""
    dates = all_raw_dates(root)
    schema = {
        "symbol": pl.String,
        "rth_dollar_vol": pl.Float64,
        "rth_close": pl.Float64,
        "rth_minutes": pl.UInt32,
        "date": pl.String,
    }
    if not dates:
        return pl.DataFrame(schema=schema)
    cache_path = _daily_cache_path(root, dates)
    if not force and os.path.exists(cache_path):
        return pl.read_parquet(cache_path)
    frames = [reduced for date_iso in dates if (reduced := reduce_one_date(root, date_iso)) is not None]
    daily = pl.concat(frames, how="vertical") if frames else pl.DataFrame(schema=schema)
    os.makedirs(CACHE_DIR, exist_ok=True)
    daily.write_parquet(cache_path)
    return daily


def compute_adv_rank(daily: pl.DataFrame, cuts: list[int]) -> pl.DataFrame:
    """Point-in-time trailing-20d ADV, its cross-sectional rank, and band label per (symbol, date).

    Trailing ADV is a 20d rolling mean of RTH dollar volume per symbol (computed only once the symbol
    has >= ``MIN_TRAILING_DAYS`` trailing bars — no look-ahead, no partial-window rank). The rank is the
    descending dollar-volume rank WITHIN each date (1 = most liquid), and the band is that rank under
    ``cuts``. Symbols not yet warmed up on a date are dropped from that date's cross-section (unranked),
    so the band sizes reflect only stably-ranked names."""
    if daily.height == 0:
        return daily.with_columns(
            pl.lit(None, dtype=pl.Float64).alias("adv_20d"),
            pl.lit(None, dtype=pl.UInt32).alias("adv_rank"),
            pl.lit(None, dtype=pl.String).alias("band"),
        )
    over_symbol = pl.col("symbol")
    enriched = daily.sort(["symbol", "date"]).with_columns(
        pl.col("rth_dollar_vol").rolling_mean(window_size=ADV_WINDOW).over(over_symbol).alias("adv_20d"),
        pl.col("date").cum_count().over(over_symbol).alias("bar_idx"),
    )
    ranked = (
        enriched.filter((pl.col("bar_idx") >= MIN_TRAILING_DAYS) & pl.col("adv_20d").is_not_null())
        .with_columns(
            pl.col("adv_20d")
            .rank(method="ordinal", descending=True)
            .over("date")
            .cast(pl.UInt32)
            .alias("adv_rank")
        )
        .with_columns(band_label_expr(cuts).alias("band"))
    )
    return ranked.drop("bar_idx")


def _band_sizes_over_time(ranked: pl.DataFrame, cuts: list[int]) -> list[dict[str, object]]:
    """Per-date band membership counts (how many symbols sit in each band that day)."""
    labels = band_labels(cuts)
    counts = (
        ranked.group_by(["date", "band"])
        .agg(pl.len().alias("n"))
        .pivot(values="n", index="date", on="band", aggregate_function="first")
        .sort("date")
        .fill_null(0)
    )
    out = []
    for row in counts.iter_rows(named=True):
        out.append(
            {
                "date": row["date"],
                "total": sum(int(row[label]) for label in labels if label in row),
                "bands": {label: int(row[label]) for label in labels if label in row},
            }
        )
    return out


def _band_stability(ranked: pl.DataFrame) -> dict[str, object]:
    """Membership STABILITY: how often a symbol's band changes day-to-day (turnover).

    For each symbol on consecutive ranked days, a "cross" is a change of band label vs the previous
    ranked day for that symbol. The per-band cross rate = crosses out of that band / total day-pairs
    starting in that band; a high rate means a strategy conditioned on that band churns its universe."""
    if ranked.height == 0:
        return {"overall_cross_rate": 0.0, "per_band": {}, "n_transitions": 0}
    transitions = (
        ranked.sort(["symbol", "date"])
        .with_columns(
            pl.col("band").shift(1).over("symbol").alias("prev_band"),
            pl.col("date").shift(1).over("symbol").alias("prev_date"),
        )
        .filter(pl.col("prev_band").is_not_null())
    )
    if transitions.height == 0:
        return {"overall_cross_rate": 0.0, "per_band": {}, "n_transitions": 0}
    transitions = transitions.with_columns((pl.col("band") != pl.col("prev_band")).alias("crossed"))
    overall = float(transitions["crossed"].mean())
    per_band_frame = (
        transitions.group_by("prev_band")
        .agg(
            pl.len().alias("pairs"),
            pl.col("crossed").sum().alias("crosses"),
        )
        .sort("prev_band")
    )
    per_band = {
        row["prev_band"]: {
            "pairs": int(row["pairs"]),
            "crosses": int(row["crosses"]),
            "cross_rate": round(float(row["crosses"]) / row["pairs"], 4) if row["pairs"] else 0.0,
        }
        for row in per_band_frame.iter_rows(named=True)
    }
    return {
        "overall_cross_rate": round(overall, 4),
        "per_band": per_band,
        "n_transitions": int(transitions.height),
    }


def _clip_recent(timeline: list[dict[str, object]], days: int | None) -> list[dict[str, object]]:
    """Trim the per-date band-size timeline to the most-recent ``days`` calendar days for the snappy
    default. ``days is None / 0`` keeps the full history."""
    if not days or days <= 0 or not timeline:
        return timeline
    latest = dt.date.fromisoformat(timeline[-1]["date"])  # type: ignore[arg-type]
    cutoff = (latest - dt.timedelta(days=days - 1)).isoformat()
    return [cell for cell in timeline if cell["date"] >= cutoff]  # type: ignore[operator]


def build_liquidity_bands(
    root: str = STORE_ROOT,
    cuts: list[int] | None = None,
    days: int | None = TIMELINE_DEFAULT_DAYS,
    asof: str | None = None,
    force: bool = False,
) -> dict[str, object]:
    """The canonical liquidity-band surface.

    Per-date band sizes over time, membership stability (turnover), and an as-of snapshot of the current
    (or ``asof``-date) band membership. ``cuts`` overrides the default contiguous ADV-rank cuts; ``days``
    clips the per-date timeline (0/None = full history); ``asof`` pins the snapshot to a given date
    (point-in-time, no look-ahead) instead of the latest ranked date.

    Shape:
      {generated_at, store_root, cuts, band_labels, adv_window, min_trailing_days, days,
       earliest, latest, asof, n_dates, n_ranked_symbols,
       timeline: [{date, total, bands: {label: n}}],
       stability: {overall_cross_rate, per_band: {label: {pairs, crosses, cross_rate}}, n_transitions},
       snapshot: {date, bands: {label: {n, members_sample: [...], min_adv, max_adv, median_adv}}}}
    """
    band_cuts = sorted(cuts) if cuts else list(DEFAULT_CUTS)
    labels = band_labels(band_cuts)
    daily = build_daily_dollar_vol(root, force=force)
    ranked = compute_adv_rank(daily, band_cuts)
    base: dict[str, object] = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "store_root": root,
        "cuts": band_cuts,
        "band_labels": labels,
        "adv_window": ADV_WINDOW,
        "min_trailing_days": MIN_TRAILING_DAYS,
        "days": days,
        "earliest": None,
        "latest": None,
        "asof": None,
        "n_dates": 0,
        "n_ranked_symbols": 0,
        "timeline": [],
        "stability": {"overall_cross_rate": 0.0, "per_band": {}, "n_transitions": 0},
        "snapshot": {"date": None, "bands": {}},
    }
    if ranked.height == 0:
        return base
    ranked_dates = sorted(ranked["date"].unique().to_list())
    earliest, latest = ranked_dates[0], ranked_dates[-1]
    snapshot_date = asof if asof and asof in ranked_dates else latest
    timeline = _clip_recent(_band_sizes_over_time(ranked, band_cuts), days)
    snapshot = _band_snapshot(ranked, snapshot_date, labels)
    base.update(
        {
            "earliest": earliest,
            "latest": latest,
            "asof": snapshot_date,
            "n_dates": len(ranked_dates),
            "n_ranked_symbols": int(ranked["symbol"].n_unique()),
            "timeline": timeline,
            "stability": _band_stability(ranked),
            "snapshot": snapshot,
        }
    )
    return base


def _band_snapshot(ranked: pl.DataFrame, date_iso: str, labels: list[str]) -> dict[str, object]:
    """As-of-date band membership: per band a count, a small member sample (most liquid first), and the
    band's ADV range — enough to eyeball "who is in band X today" without dumping the whole universe."""
    day = ranked.filter(pl.col("date") == date_iso).sort("adv_rank")
    bands: dict[str, object] = {}
    for label in labels:
        members = day.filter(pl.col("band") == label)
        if members.height == 0:
            bands[label] = {
                "n": 0,
                "members_sample": [],
                "min_adv": None,
                "max_adv": None,
                "median_adv": None,
            }
            continue
        bands[label] = {
            "n": int(members.height),
            "members_sample": members["symbol"].to_list()[:25],
            "rank_lo": int(members["adv_rank"].min()),
            "rank_hi": int(members["adv_rank"].max()),
            "min_adv": round(float(members["adv_20d"].min()), 1),
            "max_adv": round(float(members["adv_20d"].max()), 1),
            "median_adv": round(float(members["adv_20d"].median()), 1),
        }
    return {"date": date_iso, "bands": bands}


def symbol_history(
    symbol: str,
    root: str = STORE_ROOT,
    cuts: list[int] | None = None,
    force: bool = False,
) -> dict[str, object]:
    """One symbol's ADV-rank / band history over time — its trailing ADV, cross-sectional rank, and band
    on each ranked date. The "given a symbol, its ADV-rank history" lookup."""
    band_cuts = sorted(cuts) if cuts else list(DEFAULT_CUTS)
    daily = build_daily_dollar_vol(root, force=force)
    ranked = compute_adv_rank(daily, band_cuts)
    hist = (
        ranked.filter(pl.col("symbol") == symbol)
        .sort("date")
        .select(["date", "adv_20d", "adv_rank", "band"])
    )
    return {
        "symbol": symbol,
        "cuts": band_cuts,
        "n_dates": int(hist.height),
        "history": [
            {
                "date": row["date"],
                "adv_20d": round(float(row["adv_20d"]), 1),
                "adv_rank": int(row["adv_rank"]),
                "band": row["band"],
            }
            for row in hist.iter_rows(named=True)
        ],
    }


def band_members(
    band: str,
    root: str = STORE_ROOT,
    cuts: list[int] | None = None,
    asof: str | None = None,
    force: bool = False,
) -> dict[str, object]:
    """The full current (or ``asof``-date) membership of one band — the "given a band, its members"
    lookup, with each member's rank and trailing ADV. This is the reproducible-universe export a research
    lane uses instead of an ad-hoc top-N (e.g. band "2000-4000" == Lane C's B4 small-cap niche)."""
    band_cuts = sorted(cuts) if cuts else list(DEFAULT_CUTS)
    daily = build_daily_dollar_vol(root, force=force)
    ranked = compute_adv_rank(daily, band_cuts)
    if ranked.height == 0:
        return {"band": band, "cuts": band_cuts, "asof": None, "n_members": 0, "members": []}
    ranked_dates = sorted(ranked["date"].unique().to_list())
    snapshot_date = asof if asof and asof in ranked_dates else ranked_dates[-1]
    members = (
        ranked.filter((pl.col("date") == snapshot_date) & (pl.col("band") == band))
        .sort("adv_rank")
        .select(["symbol", "adv_rank", "adv_20d"])
    )
    return {
        "band": band,
        "cuts": band_cuts,
        "asof": snapshot_date,
        "n_members": int(members.height),
        "members": [
            {
                "symbol": row["symbol"],
                "adv_rank": int(row["adv_rank"]),
                "adv_20d": round(float(row["adv_20d"]), 1),
            }
            for row in members.iter_rows(named=True)
        ],
    }


def parse_cuts(cuts_arg: str | None) -> list[int] | None:
    """Parse a ``?cuts=500,1000,2000,4000`` query string into a sorted ascending list of positive ints.
    Returns None (use defaults) for an empty/blank arg; raises ValueError on a malformed or non-positive
    cut so the route can surface a 400 rather than silently mis-band."""
    if not cuts_arg or not cuts_arg.strip():
        return None
    cuts = [int(part) for part in cuts_arg.split(",") if part.strip()]
    if not cuts or any(cut <= 0 for cut in cuts):
        raise ValueError("cuts must be a comma-separated list of positive integers")
    if len(set(cuts)) != len(cuts):
        raise ValueError("cuts must be distinct")
    return sorted(cuts)


class LiquidityBandsCache:
    """TTL cache over the assembled views, keyed by (cuts, days, asof). Stage-1 (the bar scan) is itself
    disk-cached inside ``build_daily_dollar_vol``, so a warm view is sub-second and this just spares the
    stage-2 polars passes on a busy refresh."""

    def __init__(self, ttl: float = VIEW_TTL) -> None:
        self.ttl = ttl
        self._by_key: dict[tuple[str, int, str], tuple[float, dict[str, object]]] = {}

    def bands(
        self,
        root: str,
        cuts: list[int] | None = None,
        days: int = TIMELINE_DEFAULT_DAYS,
        asof: str | None = None,
        force: bool = False,
    ) -> dict[str, object]:
        cuts_key = ",".join(str(cut) for cut in cuts) if cuts else "default"
        key = (cuts_key, days, asof or "latest")
        now = time.monotonic()
        cached = self._by_key.get(key)
        if not force and cached is not None and (now - cached[0]) <= self.ttl:
            return cached[1]
        view = build_liquidity_bands(root, cuts=cuts, days=days, asof=asof, force=force)
        self._by_key[key] = (now, view)
        return view


CACHE = LiquidityBandsCache()
