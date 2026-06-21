"""Crypto INPUT persistence — the additive store that makes a crypto backfill recompute possible.

The equity parity sweep compares ``source=stream`` (live) against ``source=backfill`` (re-materialized from
``/store/raw``). Crypto has NO raw tape (``raw_backfill`` is equity-only), so the crypto rehearsal recomputes
the backfill side from the SAME ``minute_agg`` (OHLCV + aggregated-tick) inputs the live feed delivered — a
genuine live-emit-vs-batch-recompute parity test (docs/CRYPTO_E2E.md §3). The live path COMPUTES features from
those inputs but does not store them; this module persists them so the recompute has its inputs.

Layout (parallels the feature store): ``<root>/crypto_input/kind=<minute_agg|trades>/date=<d>/data-<epoch>.parquet``
— one per-minute file per kind, written append-style like the feature store's per-minute writes (O(1) per
tick, idempotent on re-delivery via the per-epoch filename). Read globs ``data*.parquet`` so all the minute
files union back into the day's input frame.

This is DELIBERATELY separate from the feature partitions: it stores the raw compute INPUTS, not features, so
the crypto sweep can re-run ``materialize._write_all`` over them to produce ``source=backfill`` and compare.
The write is env-gated (``FP_CRYPTO_PERSIST_INPUTS``) so it is opt-in — the live ``crypto-capture`` container
only begins persisting inputs on the Lead's sanctioned relaunch, not implicitly.
"""
from __future__ import annotations

import datetime as dt
import os
from pathlib import Path

import polars as pl

CRYPTO_INPUT_DIRNAME = "crypto_input"
PERSIST_ENV = "FP_CRYPTO_PERSIST_INPUTS"


def persist_inputs_enabled() -> bool:
    """Whether the crypto capture path should persist its ``minute_agg``/``trades`` inputs for the backfill
    recompute. Opt-in (``FP_CRYPTO_PERSIST_INPUTS=1``) so the live container only starts persisting on a
    sanctioned relaunch; off by default keeps the current crypto capture byte-for-byte unchanged."""
    return os.environ.get(PERSIST_ENV, "").strip() in ("1", "true", "True")


def _kind_dir(root: str | Path, kind: str, day: str) -> Path:
    return Path(root) / CRYPTO_INPUT_DIRNAME / f"kind={kind}" / f"date={day}"


def write_input(root: str | Path, kind: str, day: str, frame: pl.DataFrame, minute: dt.datetime) -> Path:
    """Persist ONE minute's input ``frame`` for ``kind`` (``minute_agg`` or ``trades``) under ``day``.

    Per-minute file named by the minute epoch (idempotent: a re-delivered minute overwrites its own file via
    atomic replace), mirroring the feature store's per-minute append write. An empty frame is skipped (no
    file) — an honest 'no trades this minute', not a fabricated empty partition."""
    if frame.height == 0:
        return _kind_dir(root, kind, day)
    target = _kind_dir(root, kind, day)
    target.mkdir(parents=True, exist_ok=True)
    name = f"data-{int(minute.timestamp())}.parquet"
    tmp = target / f".tmp-{name}.{os.getpid()}"
    frame.write_parquet(tmp, compression="zstd", compression_level=1)
    os.replace(tmp, target / name)
    return target / name


def load_input(root: str | Path, kind: str, day: str) -> pl.DataFrame:
    """Read back the day's persisted input frame for ``kind`` (the union of all per-minute files), or an
    EMPTY frame if none were written. The crypto sweep feeds these to the batch recompute."""
    target = _kind_dir(root, kind, day)
    if not target.exists():
        return pl.DataFrame()
    files = sorted(str(path) for path in target.glob("data*.parquet"))
    if not files:
        return pl.DataFrame()
    return pl.concat([pl.read_parquet(path) for path in files], how="vertical_relaxed")


def input_days(root: str | Path, kind: str = "minute_agg") -> list[str]:
    """The days that have persisted ``kind`` inputs (sorted), for the sweep to discover what it can grade."""
    base = Path(root) / CRYPTO_INPUT_DIRNAME / f"kind={kind}"
    if not base.exists():
        return []
    return sorted(
        path.name[len("date=") :] for path in base.glob("date=*") if path.is_dir() and any(path.glob("data*.parquet"))
    )
