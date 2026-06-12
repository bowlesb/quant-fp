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


def test_live_feature_coverage_is_registered() -> None:
    # Same-day live-path coverage check must exist by this exact name (Ben's 2026-06-12 ask).
    assert "live_feature_coverage" in qa.INVARIANTS


def test_fill_reconciliation_is_registered() -> None:
    # The live-basket execution gate must exist by this exact name (exec-recon-one-directional).
    assert "fill_reconciliation" in qa.INVARIANTS
    assert "fill_reconciliation" in qa.FAST_INVARIANTS


def test_nan_report_is_callable() -> None:
    # The canonical per-feature in-vector NaN report (Manager fixture 2026-06-13) must exist by
    # this exact name — every "panel is clean" claim cites it, so its absence can never be silent.
    assert callable(qa.nan_report)


def test_known_issue_fingerprint_does_not_camouflage() -> None:
    # The EXPECTED/OWNED tag must match ONLY the tight fingerprint, never blanket-suppress an
    # invariant. v1.2.0 with all-WIP-family reds is owned; the moment a STABLE feature (or a
    # different set) goes red, classify_known must return None so it surfaces as a real FAIL.
    wip = qa.Result(
        "warmup_coverage",
        "FAIL",
        "ACTIVE set v1.2.0: ...",
        [
            "[15] mom_3d: early=100.0% late=100.0% — 100.0% NaN (dead)",
            "[22] ofi_5m: 16.6% NaN mid-session (post-warmup, steady-state degrade)",
        ],
    )
    assert qa.classify_known(wip) is not None
    stable_red = qa.Result(
        "warmup_coverage",
        "FAIL",
        "ACTIVE set v1.2.0: ...",
        wip.details
        + ["[1] ret_5m: 25.0% NaN mid-session (post-warmup, steady-state degrade)"],
    )
    assert qa.classify_known(stable_red) is None
    other_set = qa.Result(
        "warmup_coverage",
        "FAIL",
        "ACTIVE set v1.1.1: ...",
        ["[1] ret_5m: 25.0% NaN mid-session (post-warmup, steady-state degrade)"],
    )
    assert qa.classify_known(other_set) is None


def test_terminal_order_states_match_executor() -> None:
    # qa.TERMINAL_ORDER_STATES is a hand-kept mirror of services/executor/main.py's set; if the
    # executor's terminal set drifts, fill_reconciliation would mis-judge "stuck" orders. Guard it.
    executor_main = (qa.REPO / "services" / "executor" / "main.py").read_text()
    assert (
        'TERMINAL_ORDER_STATES = {"filled", "canceled", "expired", "rejected", "done_for_day", "replaced"}'
        in executor_main
    ), "executor's TERMINAL_ORDER_STATES changed — update qa.TERMINAL_ORDER_STATES to match"


def test_family_valued_pct_averages_only_present_indices() -> None:
    # DB-free: family valued% is the mean over the indices that exist in today's vector.
    valued = {1: 90.0, 2: 80.0, 12: 100.0, 13: 100.0}
    assert qa._family_valued_pct(valued, [1, 2]) == 85.0
    assert qa._family_valued_pct(valued, [12, 13]) == 100.0
    # An index absent from today's data is skipped, not counted as 0.
    assert qa._family_valued_pct(valued, [1, 99]) == 90.0
    # No present indices -> 0.0 (nothing to average), never a ZeroDivisionError.
    assert qa._family_valued_pct(valued, [98, 99]) == 0.0
