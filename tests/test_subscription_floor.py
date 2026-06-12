"""Regression guard for the OFI≥500 build-time floor (qa review condition #2).

OFI_SYMBOLS_SQL filters adv_dollar IS NOT NULL with no floor check, so if many names
have a NULL ADV on some rebuild date the OFI set could silently shrink under the M2
≥500 criterion with nothing tripping. load_subscription() now asserts the floor at
build time. This test proves the assert fires below the floor and passes the live
universe above it.

DB-backed; SKIPS when the DB is unreachable (bare `make test` container), like the QA
invariant suite.
"""
import os
import sys
from pathlib import Path

import pytest

psycopg = pytest.importorskip("psycopg", reason="DB-backed test needs psycopg")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "ingestor"))

from app_ingestor.subscription import OFI_MIN_COUNT, load_subscription  # noqa: E402

DB_KWARGS: dict[str, str | int] = {
    "host": os.environ.get("DB_HOST", "localhost"),
    "port": int(os.environ.get("DB_PORT", "5433")),
    "dbname": os.environ.get("DB_NAME", "quant"),
    "user": os.environ.get("DB_USER", "quant"),
    "password": os.environ.get("DB_PASSWORD", ""),
}


def _db_reachable() -> bool:
    try:
        psycopg.connect(**DB_KWARGS, connect_timeout=5).close()
        return True
    except psycopg.Error:
        return False


pytestmark = pytest.mark.skipif(
    not _db_reachable(), reason="DB not reachable (run from host or with DB_* env set)"
)


def test_live_universe_meets_ofi_floor() -> None:
    """The real universe yields >= the M2 floor of OFI names (default top-512)."""
    _bars, ofi, _shards = load_subscription(DB_KWARGS, n_shards=4)
    assert len(ofi) >= OFI_MIN_COUNT


def test_under_floor_ofi_count_raises() -> None:
    """Forcing the OFI count below the floor must FAIL LOUD at build, not return a
    silently-undersized set."""
    with pytest.raises(RuntimeError, match="below|floor|< floor"):
        load_subscription(DB_KWARGS, n_shards=4, ofi_count=100)
