"""Tests for the EDGAR cross-seam dedup (#311 G4): the same accession arriving via the live atom feed
AND the submissions backfill must land as ONE row, with the live (atom_feed) row kept as canonical.

The seam logic in ``services/edgar/main.py`` runs SQL against the Postgres ``filings`` table, so these
tests drive it through a faithful in-memory fake of that table — modelling the ``(accession_number,
available_at)`` PK + the ``ON CONFLICT DO UPDATE`` upsert + the two seam queries — so the real functions
(``drop_seam_dups``, ``find_seam_dup_rows``, ``delete_seam_dup_rows``, ``accessions_with_live_row``) are
exercised without a live database.
"""

import os
import sys
from datetime import datetime, timezone

SERVICE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "services", "edgar")
if SERVICE_DIR not in sys.path:
    sys.path.insert(0, SERVICE_DIR)

import main as edgar  # noqa: E402

ATOM = datetime(2026, 6, 16, 15, 27, 12, tzinfo=timezone.utc)  # live <updated> dissemination instant
ACCEPTED = datetime(2026, 6, 16, 11, 27, 12, tzinfo=timezone.utc)  # backfill acceptanceDateTime (~4h off)


def _live_row(accession: str) -> dict[str, object]:
    return {
        "accession_number": accession,
        "cik": "0000079879",
        "symbol": "ABC",
        "form_type": "8-K",
        "company_name": "ABC INC",
        "filed_at": None,
        "accepted_at": None,
        "available_at": ATOM,
        "available_at_source": "atom_feed",
        "link": "https://sec.gov/x",
        "source": "stream",
    }


def _backfill_row(accession: str) -> dict[str, object]:
    return {
        "accession_number": accession,
        "cik": "0000079879",
        "symbol": "ABC",
        "form_type": "8-K",
        "company_name": "ABC INC",
        "filed_at": datetime(2026, 6, 16, tzinfo=timezone.utc),
        "accepted_at": ACCEPTED,
        "available_at": ACCEPTED,
        "available_at_source": "submissions_accepted",
        "link": "https://sec.gov/x",
        "source": "backfill",
    }


class FakeCursor:
    """Models just enough of the ``filings`` table for the seam SQL: a dict keyed by the real PK
    (accession_number, available_at), the upsert's ON CONFLICT DO NOTHING-on-PK behavior, and the two
    seam SELECTs + the targeted DELETE."""

    def __init__(self, store: dict[tuple[str, datetime], dict[str, object]]) -> None:
        self._store = store
        self._result: list[tuple] = []
        self.rowcount = 0

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, sql: str, params: object = None) -> None:
        text = " ".join(sql.split())
        if text.startswith("INSERT INTO filings"):
            self._upsert(params)
        elif "SELECT DISTINCT accession_number FROM filings" in text:
            wanted = set(params[0])  # type: ignore[index]
            self._result = [
                (key[0],)
                for key, row in self._store.items()
                if row["available_at_source"] == "atom_feed" and key[0] in wanted
            ]
        elif text.startswith("WITH seam AS"):
            self._result = self._seam_dup_rows()
        elif text.startswith("DELETE FROM filings"):
            accession, available_at = params  # type: ignore[misc]
            key = (accession, available_at)
            row = self._store.get(key)
            if row is not None and row["available_at_source"] == "submissions_accepted":
                del self._store[key]
                self.rowcount = 1
            else:
                self.rowcount = 0
        else:
            raise AssertionError(f"unexpected SQL: {text[:80]}")

    def executemany(self, sql: str, seq: object) -> None:
        self.rowcount = 0
        for params in seq:  # type: ignore[attr-defined]
            self.execute(sql, params)

    def _upsert(self, row: dict[str, object]) -> None:
        key = (str(row["accession_number"]), row["available_at"])  # type: ignore[arg-type]
        # ON CONFLICT (accession_number, available_at): keep the existing row (the COALESCE merge
        # preserves available_at + first-sight fields). For the seam invariant what matters is that a
        # SECOND distinct available_at for the same accession creates a SECOND row — which it does here.
        if key not in self._store:
            self._store[key] = dict(row)

    def _seam_dup_rows(self) -> list[tuple]:
        by_accession: dict[str, set[str]] = {}
        for (accession, _), row in self._store.items():
            by_accession.setdefault(accession, set()).add(str(row["available_at_source"]))
        out: list[tuple] = []
        for (accession, available_at), row in self._store.items():
            sources = by_accession[accession]
            if (
                row["available_at_source"] == "submissions_accepted"
                and "atom_feed" in sources
                and "submissions_accepted" in sources
            ):
                out.append((accession, available_at))
        return sorted(out, key=lambda pair: (pair[0], pair[1]))

    def fetchall(self) -> list[tuple]:
        return self._result


class FakeConn:
    def __init__(self) -> None:
        self.store: dict[tuple[str, datetime], dict[str, object]] = {}

    def cursor(self) -> FakeCursor:
        return FakeCursor(self.store)


def test_drop_seam_dups_skips_accession_already_live() -> None:
    conn = FakeConn()
    edgar.upsert_filings(conn, [_live_row("0000079879-26-000209")])  # type: ignore[arg-type]
    # Backfill batch contains the already-live accession + a genuinely new one.
    batch = [_backfill_row("0000079879-26-000209"), _backfill_row("0000079879-26-000999")]
    kept = edgar.drop_seam_dups(conn, batch)  # type: ignore[arg-type]
    kept_accessions = {str(filing["accession_number"]) for filing in kept}
    assert kept_accessions == {"0000079879-26-000999"}


def test_both_paths_land_as_one_row() -> None:
    """The core G4 invariant: a filing seen by BOTH the live feed and backfill is stored ONCE."""
    conn = FakeConn()
    accession = "0000079879-26-000209"
    # Live captures it first (the dominant ordering: continuous live, later one-shot backfill).
    edgar.upsert_filings(conn, [_live_row(accession)])  # type: ignore[arg-type]
    # Backfill then runs over the same window — the seam filter drops the already-live accession.
    backfill_batch = edgar.drop_seam_dups(conn, [_backfill_row(accession)])  # type: ignore[arg-type]
    edgar.upsert_filings(conn, backfill_batch)  # type: ignore[arg-type]
    rows_for_accession = [key for key in conn.store if key[0] == accession]
    assert len(rows_for_accession) == 1
    # And the kept row is the canonical LIVE one (atom_feed dissemination available_at).
    kept = conn.store[rows_for_accession[0]]
    assert kept["available_at_source"] == "atom_feed"
    assert kept["available_at"] == ATOM


def test_dedup_finds_and_deletes_existing_seam_dups() -> None:
    """Models the pre-fix state (both rows already on disk) and the one-time cleanup."""
    conn = FakeConn()
    accession = "0000079879-26-000209"
    # Pre-fix: both rows exist (backfill bypassed the not-yet-deployed seam filter).
    conn.store[(accession, ATOM)] = _live_row(accession)
    conn.store[(accession, ACCEPTED)] = _backfill_row(accession)
    # A standalone accession with only a live row must NOT be touched.
    conn.store[("0000079879-26-000300", ATOM)] = _live_row("0000079879-26-000300")

    dups = edgar.find_seam_dup_rows(conn)  # type: ignore[arg-type]
    assert dups == [(accession, ACCEPTED)]

    deleted = edgar.delete_seam_dup_rows(conn, dups)  # type: ignore[arg-type]
    assert deleted == 1
    # The canonical live row survives; the backfill dup is gone.
    assert (accession, ATOM) in conn.store
    assert (accession, ACCEPTED) not in conn.store
    # Idempotent: a re-run finds nothing.
    assert edgar.find_seam_dup_rows(conn) == []  # type: ignore[arg-type]
    assert edgar.delete_seam_dup_rows(conn, []) == 0  # type: ignore[arg-type]


def test_drop_seam_dups_noop_when_no_live_rows() -> None:
    conn = FakeConn()
    batch = [_backfill_row("0000079879-26-000700")]
    assert edgar.drop_seam_dups(conn, batch) == batch  # type: ignore[arg-type]
