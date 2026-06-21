"""One-time, idempotent dedup of the EDGAR cross-seam double-rows (#311 G4).

The live atom feed and the submissions-API backfill assign DIFFERENT ``available_at`` for the same filing
(the atom ``<updated>`` dissemination instant vs ``acceptanceDateTime``, ~4h apart = the ET offset), so
the ``(accession_number, available_at)`` PK does not dedupe across the seam. Any accession captured by
BOTH paths lands as two rows, inflating the filing-frequency features (a filing counted twice if both
timestamps fall in the same trailing window).

The CANONICAL row is the live ``atom_feed`` one — its ``available_at`` is the point-in-time-correct
"moment a real-time consumer could have known" (docs/EDGAR_INGESTION.md, "the parity crux"). The backfill
``submissions_accepted`` row is the lower-confidence reconstruction. So this script KEEPS the live row and
DELETES the backfill duplicate, by full PK (it can never touch the canonical row).

SAFE BY DEFAULT: dry-run unless ``--apply`` is passed — it prints exactly which rows it WOULD delete and
exits without writing. ``--apply`` performs the delete inside a single transaction. Idempotent: a second
run (dry-run or apply) finds nothing left and is a no-op. The ingest-time fix in ``main.drop_seam_dups``
prevents NEW seam dups, so this only ever has to run once (or after a backfill over an older,
pre-fix-ingest window).

Run inside the edgar container (it has the DB env), e.g.:

    docker exec quant-edgar-1 python /app/dedup_seam.py            # dry-run (default) — shows what it would do
    docker exec quant-edgar-1 python /app/dedup_seam.py --apply    # perform the delete
"""

from __future__ import annotations

import argparse
import logging

import main as edgar
import psycopg

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [edgar-dedup] %(message)s",
)
logger = logging.getLogger("edgar-dedup")


def run(apply: bool) -> int:
    """Find the seam duplicate rows and (if ``apply``) delete them. Returns the number of duplicate rows
    found (dry-run) or actually deleted (apply)."""
    with psycopg.connect(**edgar.db_kwargs(), autocommit=False) as conn:
        rows = edgar.find_seam_dup_rows(conn)
        if not rows:
            logger.info("no cross-seam duplicate rows found — nothing to do")
            return 0

        accessions = sorted({accession for accession, _ in rows})
        logger.info(
            "found %d backfill duplicate row(s) across %d accession(s) (live atom_feed row is kept)",
            len(rows),
            len(accessions),
        )
        for accession, available_at in rows:
            logger.info("  would delete backfill row: %s @ %s", accession, available_at.isoformat())

        if not apply:
            logger.info("DRY-RUN: no rows deleted. Re-run with --apply to perform the delete.")
            conn.rollback()
            return len(rows)

        deleted = edgar.delete_seam_dup_rows(conn, rows)
        conn.commit()
        logger.info("APPLIED: deleted %d backfill duplicate row(s)", deleted)
        return deleted


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="perform the delete (default is a dry-run that only reports what it would delete)",
    )
    args = parser.parse_args(argv)
    run(apply=args.apply)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
