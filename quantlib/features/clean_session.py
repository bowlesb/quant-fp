"""Premarket session-population build (#69): pivot the live polars snapshots into the clean engine's numpy
session layout + the per-symbol static labels, point-in-time-safe, at the per-session engine boundary.

The clean engine's once-per-session state (the daily-snapshot + event-tape groups read ``window.session``; the
cross-sectional / reference groups read ``window.static``) is NOT new data — the producers already exist and are
already loaded live per session (``real_capture.py``: ``backfill_daily`` / ``load_news_features`` /
``load_filings`` / ``load_reference``). This module is the ONE pivot from those held polars frames into the numpy
shapes the engine reads, aligned to the engine's fixed ``symbols`` index:

  - DAILY matrices ``session["daily_<col>"]`` ``(n_sym, n_days)`` — newest column ``[:, -1]`` = today (the
    ``(a)`` convention), NaN-padded left where a symbol has fewer days. From ``snapshots["daily"]``.
  - EVENT tapes ``session["news_*"]`` / ``session["edgar_*"]`` — ragged CSR (per-symbol sorted ``available_at``
    epochs + payload + offsets). From ``snapshots["news"]`` / ``snapshots["filings"]``. Point-in-time-safe by the
    in-group ``available_at <= minute`` gate (the build carries the WHOLE lookback; no future leak because the
    gate is applied per minute downstream).
  - STATIC labels ``static["sector"/"sector_name"/"cluster_id"/<flags>/"spy_row"]`` — from
    ``snapshots["reference"]`` + the symbol index. Constant intraday.

Every output is aligned to ``symbols`` (the engine's fixed index): a symbol absent from a snapshot gets the
all-NaN / unmapped / empty-tape default for its row, never a misaligned value.
"""

from __future__ import annotations

from typing import Callable

import numpy as np
import polars as pl

from quantlib.features.clean_groups_pointwise import _ASSET_FLAGS
from quantlib.features.clean_groups_reference import _EDGAR_FORM_CODE

_DAILY_COLS: tuple[str, ...] = ("open", "high", "low", "close", "volume", "vwap")
_MARKET_TICKER = "SPY"
_SECTOR_UNKNOWN = -1


def _normalize_sector(raw: str | None) -> str | None:
    """Legacy sector normalization (sector_beta._sector_map): lowercase + spaces→underscores; blank/None → None
    (the unmapped bucket)."""
    if raw is None:
        return None
    cleaned = raw.strip()
    if not cleaned:
        return None
    return cleaned.lower().replace(" ", "_")


def build_daily_matrices(daily: pl.DataFrame, symbols: list[str]) -> dict[str, np.ndarray]:
    """Pivot ``snapshots["daily"]`` (``symbol, date, open, high, low, close, volume, vwap``) into the per-column
    ``(n_sym, n_days)`` matrices, newest date LAST (``[:, -1]`` = the latest day = today under ``backfill_daily``,
    the ``(a)`` convention). Rows aligned to ``symbols``; a symbol with no daily history is an all-NaN row, and
    a symbol with fewer days is NaN-padded on the LEFT (the engine reads the trailing columns)."""
    index = {symbol: position for position, symbol in enumerate(symbols)}
    n_sym = len(symbols)
    dates = sorted(daily["date"].unique().to_list())
    n_days = len(dates)
    date_pos = {date: position for position, date in enumerate(dates)}
    matrices = {col: np.full((n_sym, n_days), np.nan, dtype=np.float64) for col in _DAILY_COLS}
    for row in daily.iter_rows(named=True):
        symbol = row["symbol"]
        if symbol not in index:
            continue
        row_pos = index[symbol]
        col_pos = date_pos[row["date"]]
        for col in _DAILY_COLS:
            value = row[col]
            if value is not None:
                matrices[col][row_pos, col_pos] = value
    return {f"daily_{col}": matrices[col] for col in _DAILY_COLS}


def build_event_tape(
    events: pl.DataFrame,
    symbols: list[str],
    payload_col: str,
    payload_fn: Callable[[str], int] | None = None,
) -> dict[str, np.ndarray]:
    """Pivot an event frame (``symbol, available_at, <payload_col>``) into the ragged CSR tape: per symbol sorted
    ascending by ``available_at``, flattened to ``_at`` (int64 epoch-secs) + ``_<payload>`` + ``_off`` (CSR
    offsets, ``off[i]..off[i+1]`` = symbol i's slice). ``payload_fn`` maps a raw payload to its stored numeric
    (e.g. EDGAR ``form_type`` → int code); identity when None. Returns the THREE arrays keyed bare (the caller
    prefixes the tape name). Symbols with no events get an empty slice (``off[i] == off[i+1]``)."""
    index = {symbol: position for position, symbol in enumerate(symbols)}
    per_symbol: list[list[tuple[int, float]]] = [[] for _ in symbols]
    for row in events.iter_rows(named=True):
        symbol = row["symbol"]
        if symbol not in index:
            continue
        epoch = int(row["available_at"].timestamp())
        raw = row[payload_col]
        value = float(payload_fn(raw)) if payload_fn is not None else float(raw)
        per_symbol[index[symbol]].append((epoch, value))
    at_list: list[int] = []
    payload_list: list[float] = []
    offsets = [0]
    for events_for_symbol in per_symbol:
        events_for_symbol.sort(key=lambda event: event[0])
        at_list.extend(event[0] for event in events_for_symbol)
        payload_list.extend(event[1] for event in events_for_symbol)
        offsets.append(len(at_list))
    return {
        "at": np.array(at_list, dtype=np.int64),
        "off": np.array(offsets, dtype=np.int64),
        "payload": np.array(payload_list, dtype=np.float64),
    }


def build_static_labels(reference: pl.DataFrame, symbols: list[str]) -> dict[str, np.ndarray]:
    """Per-symbol static labels from ``snapshots["reference"]`` (``symbol, sector, <flags>, cluster_id``), aligned
    to ``symbols``: ``sector`` (int code, ``-1`` unmapped — a stable per-normalized-name code), ``sector_name``
    (the normalized name str, ``"unknown"`` unmapped), ``cluster_id`` (int, ``-1`` missing), the asset-flag 0/1
    arrays, and ``spy_row`` (SPY's index position). A symbol absent from ``reference`` is unmapped/flag-NaN.
    """
    index = {symbol: position for position, symbol in enumerate(symbols)}
    n_sym = len(symbols)
    sector_name = np.array(["unknown"] * n_sym, dtype=object)
    cluster = np.full(n_sym, -1, dtype=np.int64)
    flags = {column: np.full(n_sym, np.nan, dtype=np.float64) for _, column in _ASSET_FLAGS}
    for row in reference.iter_rows(named=True):
        symbol = row["symbol"]
        if symbol not in index:
            continue
        row_pos = index[symbol]
        normalized = _normalize_sector(row["sector"])
        if normalized is not None:
            sector_name[row_pos] = normalized
        if row["cluster_id"] is not None:
            cluster[row_pos] = int(row["cluster_id"])
        for _, column in _ASSET_FLAGS:
            value = row[column]
            if value is not None:
                flags[column][row_pos] = 1.0 if value else 0.0
    # Stable int code per normalized sector name (-1 = unknown); the value is arbitrary, only same-name-same-code
    # matters (sector_return/sector_beta group by the int).
    distinct = sorted({name for name in sector_name.tolist() if name != "unknown"})
    code_of = {name: code for code, name in enumerate(distinct)}
    sector_code = np.array(
        [code_of.get(name, _SECTOR_UNKNOWN) for name in sector_name.tolist()], dtype=np.int64
    )
    static: dict[str, np.ndarray] = {
        "sector": sector_code,
        "sector_name": sector_name,
        "cluster_id": cluster,
        "spy_row": np.array([index[_MARKET_TICKER]]) if _MARKET_TICKER in index else np.array([-1]),
    }
    static.update(flags)
    return static


def build_session(
    snapshots: dict[str, pl.DataFrame], symbols: list[str]
) -> tuple[dict[str, np.ndarray], dict[str, np.ndarray]]:
    """The whole premarket build: turn the held ``snapshots`` (daily / news / filings / reference) into the
    ``(session, static)`` pair the engine takes (``engine.set_session(session)`` + ``engine.static = static``).
    Each source is OPTIONAL — an absent snapshot leaves its session keys / static labels out, and the dependent
    groups emit their NaN/empty defaults (honest, never a fabricated value)."""
    session: dict[str, np.ndarray] = {}
    if "daily" in snapshots:
        session.update(build_daily_matrices(snapshots["daily"], symbols))
    if "news" in snapshots:
        news = build_event_tape(snapshots["news"], symbols, "sentiment")
        session["news_at"] = news["at"]
        session["news_off"] = news["off"]
        session["news_sentiment"] = news["payload"]
    if "filings" in snapshots:
        edgar = build_event_tape(
            snapshots["filings"], symbols, "form_type", payload_fn=_EDGAR_FORM_CODE.__getitem__
        )
        session["edgar_at"] = edgar["at"]
        session["edgar_off"] = edgar["off"]
        session["edgar_form"] = edgar["payload"]
    static: dict[str, np.ndarray] = {}
    if "reference" in snapshots:
        static = build_static_labels(snapshots["reference"], symbols)
    return session, static
