"""Regression guard: daily-close loading must NOT be bound to the intraday bar_source.

The bug (task #9): the v1.2.0 OFI panel build runs with bar_source='stream', and
load_daily_closes() used to read daily closes from that same source. The stream
source only spans the ~3-day OFI capture window, so daily momentum (mom_3d/5d/10d,
which need up to 11 prior trading days) was 100% NaN — 6 features silently dead. The
fix loads daily closes from the deepest available history (preferring backfill),
independent of bar_source, because daily momentum is a multi-day timescale.

This test connects to the LIVE DB and SKIPS when it is unreachable (e.g. the clean
`make test` container), like the QA invariant suite. A DB-connected run asserts that a
liquid name with deep backfill history exposes far more daily closes than the few days
the stream window holds — i.e. momentum can never again be starved by the stream-source
binding.
"""
import os
from datetime import datetime, timezone

import pytest

psycopg = pytest.importorskip("psycopg", reason="DB-backed test needs psycopg")

from quantlib.featurestore import load_daily_closes  # noqa: E402

DB_KWARGS = {
    "host": os.environ.get("DB_HOST", "localhost"),
    "port": int(os.environ.get("DB_PORT", "5433")),
    "dbname": os.environ.get("DB_NAME", "quant"),
    "user": os.environ.get("DB_USER", "quant"),
    "password": os.environ.get("DB_PASSWORD", ""),
}
PROBE_SYMBOL = "NVDA"  # a deep-history liquid name in the OFI set


def _conn() -> psycopg.Connection | None:
    try:
        return psycopg.connect(**DB_KWARGS, connect_timeout=5)
    except psycopg.Error:
        return None


pytestmark = pytest.mark.skipif(
    _conn() is None, reason="DB not reachable (run from host or with DB_* env set)"
)


def test_daily_closes_use_deep_backfill_not_just_stream() -> None:
    """A name with multi-year backfill must expose far more than the stream window's
    few daily closes — proving daily-close loading is no longer bound to source=stream
    (the bug that starved v1.2.0 momentum to 100% NaN)."""
    conn = _conn()
    assert conn is not None
    with conn:
        closes = load_daily_closes(conn, PROBE_SYMBOL, datetime.now(timezone.utc))

        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(DISTINCT ts::date) FROM bars_1m "
                "WHERE symbol=%s AND source='stream'",
                (PROBE_SYMBOL,),
            )
            stream_days = cur.fetchone()[0]

    # mom_10d needs >10 prior trading days; the stream window alone never has them.
    assert len(closes) > 60, (
        f"{PROBE_SYMBOL} exposed only {len(closes)} daily closes — momentum would be "
        f"starved; daily-close loading must draw on deep backfill history"
    )
    # And it must exceed what the stream source alone holds (the old buggy path).
    assert len(closes) > stream_days, (
        f"daily closes ({len(closes)}) not deeper than the stream window "
        f"({stream_days} days) — daily-close loading still looks stream-bound"
    )
