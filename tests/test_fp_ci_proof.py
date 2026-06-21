"""CI gate proof — the RED case. A deliberately-FAILING fp test on a TIER-1 PR.

This file exists only to prove the CI watcher red-flags a broken PR (the #332-class failure the gate is
built to catch). Safe to delete after the proof.
"""

from __future__ import annotations


def test_ci_proof_deliberately_fails() -> None:
    # Deliberate failure to prove CI goes RED + non-mergeable.
    assert 1 + 1 == 3
