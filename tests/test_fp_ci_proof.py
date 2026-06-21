"""CI gate proof — the GREEN case. A trivially-passing fp test on a docs/test-only (TIER-1) PR.

This file exists only to prove the CI watcher greens a good PR. Safe to delete after the proof.
"""

from __future__ import annotations


def test_ci_proof_passes() -> None:
    assert 1 + 1 == 2
