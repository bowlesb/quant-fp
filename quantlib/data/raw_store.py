"""On-disk contract for the shared `/store/raw/` dataset — partition layout + resumable manifest.

Pure storage helpers shared by the backfill orchestrator (``raw_backfill``) and the fast multiprocess
engine (``fast_backfill``). Kept in their own module so both engines depend on this leaf instead of each
other (no circular import).

Layout:  <store>/raw/<bars|trades|quotes>/symbol=<S>/date=<YYYY-MM-DD>/data.parquet
Manifest: <store>/raw/_manifest_<tier>.d/part-*.parquet  (append-only parts; legacy single file unioned)
"""

from __future__ import annotations

import datetime as dt
import glob
import itertools
import logging
import os

import polars as pl

logger = logging.getLogger("raw_store")

MANIFEST_SCHEMA: dict[str, pl.DataType] = {
    "tier": pl.String,
    "symbol": pl.String,
    "date": pl.String,
    "rows": pl.Int64,
    "bytes": pl.Int64,
    "fetched_at": pl.Datetime("us", "UTC"),
}


def partition_dir(store: str, tier: str, symbol: str, day: dt.date) -> str:
    return os.path.join(
        store, "raw", tier, f"symbol={symbol}", f"date={day.isoformat()}"
    )


RAW_BAR_COLUMNS = ["symbol", "ts", "open", "high", "low", "close", "volume", "vwap", "trade_count"]

_RAW_BAR_SCHEMA: dict[str, pl.DataType] = {
    "symbol": pl.String,
    "ts": pl.Datetime("us", "UTC"),
    "open": pl.Float64,
    "high": pl.Float64,
    "low": pl.Float64,
    "close": pl.Float64,
    "volume": pl.Int64,
    "vwap": pl.Float64,
    "trade_count": pl.Int64,
}


def load_raw_bars(date_iso: str, store: str = "/store") -> pl.DataFrame:
    """Full-OHLCV minute bars for one day from ``<store>/raw/bars`` — the shared research bar loader.

    Returns every Alpaca minute-bar field (open/high/low/close/volume/vwap/trade_count), not just
    close+volume, so experiments can invent the bar-SHAPE feature family (body/wick, gaps, intrabar
    range, candlestick patterns) without each builder re-implementing its own loader. One hive-glob
    scan over all symbols. Research substrate — reads the raw store directly, no live feature def and
    no fingerprint surface. Empty frame (full schema) when the date has no partitions, so callers can
    branch on ``height``.
    """
    pattern = os.path.join(store, "raw", "bars", "symbol=*", f"date={date_iso}", "*.parquet")
    if not glob.glob(pattern):
        return pl.DataFrame(schema=_RAW_BAR_SCHEMA)
    return pl.scan_parquet(pattern, hive_partitioning=True).select(RAW_BAR_COLUMNS).collect()


def manifest_path(store: str, tier: str) -> str:
    return os.path.join(store, "raw", f"_manifest_{tier}.parquet")


def manifest_dir(store: str, tier: str) -> str:
    """Directory of append-only manifest PART files. Each flush writes one immutable part — recording a
    partition is then O(part size), not O(total manifest). load_manifest unions all parts (+ any legacy
    single-file manifest from an earlier run), so resume sees every prior fetch."""
    return os.path.join(store, "raw", f"_manifest_{tier}.d")


def load_manifest(store: str, tier: str) -> pl.DataFrame:
    """Union the legacy single-file manifest (if present) with every append-only part file."""
    frames = []
    legacy = manifest_path(store, tier)
    if os.path.exists(legacy):
        frames.append(pl.read_parquet(legacy))
    parts_dir = manifest_dir(store, tier)
    if os.path.isdir(parts_dir):
        for name in sorted(os.listdir(parts_dir)):
            if name.endswith(".parquet"):
                frames.append(pl.read_parquet(os.path.join(parts_dir, name)))
    if not frames:
        return pl.DataFrame(schema=MANIFEST_SCHEMA)
    return pl.concat(frames, how="vertical") if len(frames) > 1 else frames[0]


def done_keys(manifest: pl.DataFrame) -> set[tuple[str, str]]:
    """Set of (symbol, date) already recorded in a tier manifest (presence only, ignores rows)."""
    if manifest.height == 0:
        return set()
    return {
        (symbol, date)
        for symbol, date in zip(
            manifest["symbol"].to_list(), manifest["date"].to_list()
        )
    }


def resumable_done_keys(
    manifest: pl.DataFrame,
    today: dt.date,
    settle_window_days: int,
    force_refetch_symbols: frozenset[str] | None = None,
    min_settled_rows: int = 1,
) -> set[tuple[str, str]]:
    """The (symbol, date) keys a resume may SAFELY skip — rows-aware, so a premature/incomplete fetch retries.

    The plain ``done_keys`` treats ANY recorded (symbol, date) as done regardless of ``rows``. That poisons
    a RECENT day: Alpaca historical settles symbol-by-symbol over hours/~T+1, so a fetch issued before a
    symbol's tape landed records a 0-row (or tiny) "done" entry — and because the resume keys on (symbol,
    date) only, the real tape can NEVER be re-fetched. (Observed 2026-06-18: AAPL/NVDA trades rows=0, SPY
    rows=2 recorded "done" while QQQ landed 757k — a half-settled day permanently stuck.)

    Rule (per key, taking the MAX recorded ``rows`` so a later real fetch supersedes an earlier poison):
      * OLDER than ``settle_window_days`` → DONE regardless of rows (a genuine no-data / thin day — illiquid/
        delisted — must NOT be re-fetched forever; the deep history is never churned).
      * within the window → DONE iff it has a REAL tape: ``rows >= min_settled_rows`` for a forced symbol, or
        ``rows > 0`` for everything else. Otherwise PENDING → re-fetched until a real tape lands or it ages out.

    ``force_refetch_symbols`` are names where a TINY non-zero tape cannot be genuine — the pinned market
    tickers SPY/QQQ (and any liquid tier the caller passes). On a half-settled day Alpaca can return a 2-row
    stub for SPY (a real fetch that beat the settle), and ``rows > 0`` alone would wrongly accept it — stranding
    the market reference the cross-sectional sweep REQUIRES (the per-ticker sweep floor then blocks the whole
    day). Requiring ``rows >= min_settled_rows`` for these forces a re-fetch until the full tape lands, WITHOUT
    touching genuinely-illiquid names (a microcap's real 2-trade day stays done — it is not in the forced set).
    For non-forced symbols ``min_settled_rows`` is irrelevant; only ``rows > 0`` gates them.
    """
    if manifest.height == 0:
        return set()
    forced = force_refetch_symbols or frozenset()
    cutoff = (today - dt.timedelta(days=settle_window_days)).isoformat()
    per_key = manifest.group_by(["symbol", "date"]).agg(pl.col("rows").max().alias("max_rows"))
    has_real_tape = (
        pl.when(pl.col("symbol").is_in(list(forced)))
        .then(pl.col("max_rows") >= min_settled_rows)
        .otherwise(pl.col("max_rows") > 0)
    )
    resumable = per_key.filter(has_real_tape | (pl.col("date") < pl.lit(cutoff)))
    return {
        (symbol, date)
        for symbol, date in zip(
            resumable["symbol"].to_list(), resumable["date"].to_list()
        )
    }


def write_manifest_part(
    store: str, tier: str, entries: list[dict], part_seq: int
) -> None:
    """Persist a batch of manifest entries as ONE immutable append-only part file (atomic tmp+rename).
    Part name is (pid, seq)-unique so concurrent/overlapping processes never collide. Resume tolerates a
    crash mid-buffer: only the unflushed entries are lost and their partitions are simply re-fetched.
    """
    if not entries:
        return
    parts_dir = manifest_dir(store, tier)
    os.makedirs(parts_dir, exist_ok=True)
    frame = pl.DataFrame(entries, schema=MANIFEST_SCHEMA)
    name = f"part-{os.getpid()}-{part_seq:08d}.parquet"
    final_path = os.path.join(parts_dir, name)
    tmp_path = f"{final_path}.tmp"
    frame.write_parquet(tmp_path)
    os.replace(tmp_path, final_path)


def free_bytes(store: str) -> int:
    stats = os.statvfs(store)
    return stats.f_bavail * stats.f_frsize


def write_partition(
    store: str, tier: str, symbol: str, day: dt.date, frame: pl.DataFrame
) -> int:
    """Write a symbol-day partition parquet; return on-disk byte size. Empty frames still write a
    zero-row file so the manifest marks the symbol-day DONE (no-data days are not re-fetched).
    """
    out_dir = partition_dir(store, tier, symbol, day)
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "data.parquet")
    tmp_path = os.path.join(out_dir, "data.parquet.tmp")
    frame.write_parquet(tmp_path, compression="zstd")
    os.replace(tmp_path, out_path)
    return os.path.getsize(out_path)


def _parse_partition_path(path: str) -> tuple[str, str] | None:
    """Extract (symbol, date_iso) from a partition data.parquet path of the form
    ``.../symbol=<S>/date=<YYYY-MM-DD>/data.parquet``. Returns None if the path doesn't match.
    """
    parts = path.split(os.sep)
    symbol = None
    date_iso = None
    for segment in parts:
        if segment.startswith("symbol="):
            symbol = segment[len("symbol=") :]
        elif segment.startswith("date="):
            date_iso = segment[len("date=") :]
    if symbol is None or date_iso is None:
        return None
    return symbol, date_iso


def reconcile_manifest_from_disk(
    store: str, tier: str, symbols: list[str] | None = None
) -> int:
    """Record any on-disk partition that is MISSING from the manifest, returning the count reconciled.

    The manifest buffer flushes only every MANIFEST_FLUSH_EVERY units, so an OOM/crash that kills a worker
    loses its unflushed entries even though the partitions were already written to disk (write_partition
    runs before record). On the next resume those orphaned symbol-days look "pending" and are redundantly
    re-fetched — wasteful (idempotent, but it can re-pull >100k complete units). This scans the partition
    tree and appends one manifest part for the orphans (rows/bytes read from the parquet on disk), so a
    subsequent resume SKIPS them. Idempotent: a second run finds nothing to reconcile.

    ``symbols`` SCOPES the scan: when a job fetches an explicit symbol set (WINDOW+symbols / SAMPLE mode),
    the resume can only ever re-fetch THOSE symbols, so orphans of any other symbol are irrelevant to it.
    Globbing ``symbol=<S>/date=*`` per in-scope symbol — instead of ``symbol=*`` over the whole tier —
    collapses the scan from the full partition tree (the bars tier alone holds >20M partitions) to just the
    handful of symbols being fetched, removing a multi-minute startup tax from every targeted backfill. When
    ``symbols is None`` (the full-universe FULL/DAILY runs) the behavior is unchanged: scan ``symbol=*``.
    """
    manifest = load_manifest(store, tier)
    done = done_keys(manifest)
    tier_root = os.path.join(store, "raw", tier)
    if symbols is None:
        patterns = [os.path.join(tier_root, "symbol=*", "date=*", "data.parquet")]
    else:
        # One glob per in-scope symbol so the scan touches only those partition subtrees. A symbol with no
        # partitions on disk simply matches nothing (no error), so a fresh-target set is a cheap no-op.
        patterns = [
            os.path.join(tier_root, f"symbol={symbol}", "date=*", "data.parquet")
            for symbol in symbols
        ]
    orphans: list[dict] = []
    now = dt.datetime.now(dt.timezone.utc)
    for path in itertools.chain.from_iterable(
        glob.iglob(pattern) for pattern in patterns
    ):
        key = _parse_partition_path(path)
        if key is None or key in done:
            continue
        symbol, date_iso = key
        size = os.path.getsize(path)
        rows = pl.scan_parquet(path).select(pl.len()).collect().item()
        orphans.append(
            {
                "tier": tier,
                "symbol": symbol,
                "date": date_iso,
                "rows": rows,
                "bytes": size,
                "fetched_at": now,
            }
        )
        done.add(key)
    if orphans:
        existing_parts = glob.glob(os.path.join(manifest_dir(store, tier), "*.parquet"))
        write_manifest_part(store, tier, orphans, len(existing_parts) + 1)
    logger.info(
        "tier=%s reconciled %d orphaned on-disk partitions into the manifest (scope=%s)",
        tier,
        len(orphans),
        "full" if symbols is None else f"{len(symbols)} symbols",
    )
    return len(orphans)
