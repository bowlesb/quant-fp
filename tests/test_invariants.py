"""DB-backed QA invariant suite, exposed as pytest tests.

These enforce the QA_LEDGER invariants (docs/QA_LEDGER.md) against the LIVE database. They
SKIP automatically when the DB is unreachable — e.g. the clean `make test` container, which
has no docker/DB access — so the pure-logic unit run stays green. A DB-connected run (from
the host, or CI with QA_PSQL pointed at the database) turns every invariant into a hard,
fail-loud assertion.

The single source of truth for each check is scripts/qa_invariants.py; these tests merely
invoke it, so the CLI gate (`python3 scripts/qa_invariants.py`) and the pytest gate can never
diverge. universe_is_equities_only — the check that would have caught the ETF contamination —
is asserted explicitly as well, so its absence can never be silent.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import qa_invariants as qa  # noqa: E402  (path insert must precede import)


def _db_available() -> bool:
    try:
        qa.sql("SELECT 1")
        return True
    except (RuntimeError, OSError):
        return False


pytestmark = pytest.mark.skipif(
    not _db_available(),
    reason="DB not reachable (set QA_PSQL or run where docker compose works)",
)


@pytest.mark.parametrize("name", list(qa.INVARIANTS))
def test_invariant_green(name: str) -> None:
    result = qa.INVARIANTS[name]()
    if result.status == "SKIP":
        pytest.skip(result.message)
    detail = "\n".join(f"  {line}" for line in result.details)
    assert result.status == "PASS", f"{name} FAILED: {result.message}\n{detail}"


def test_universe_is_equities_only_is_registered() -> None:
    # The contamination check must always exist by this exact name (M1 exit criterion #1).
    assert "universe_is_equities_only" in qa.INVARIANTS
