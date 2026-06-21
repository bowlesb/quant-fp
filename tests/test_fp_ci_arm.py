"""Unit tests for the CD arm-path decisions (auto-merge predicate).

The safety-critical property: auto-merge fires ONLY for a green TIER-1 PR when the daemon is in auto-merge
mode and the PR isn't held by ``no-auto``. Grade-only mode, TIER-2, red, or a hold label must all suppress it.
Named test_fp_* so the gate runs these on itself.
"""

from __future__ import annotations

from ops.ci_scope import Tier
from ops.ci_watcher import _should_auto_merge


def test_green_tier1_auto_on_no_label_merges() -> None:
    assert _should_auto_merge(True, Tier.AUTO, auto_merge_enabled=True, labels=[]) is True


def test_grade_only_mode_never_merges() -> None:
    # The Phase-1 safe rollout (`ci_watcher.sh grade` → --no-auto-merge) must never merge, even green TIER-1.
    assert _should_auto_merge(True, Tier.AUTO, auto_merge_enabled=False, labels=[]) is False


def test_no_auto_label_holds_the_pr() -> None:
    assert _should_auto_merge(True, Tier.AUTO, auto_merge_enabled=True, labels=["no-auto"]) is False


def test_tier2_never_auto_merges() -> None:
    assert _should_auto_merge(True, Tier.GATED, auto_merge_enabled=True, labels=[]) is False


def test_red_never_merges() -> None:
    assert _should_auto_merge(False, Tier.AUTO, auto_merge_enabled=True, labels=[]) is False


def test_workdir_base_is_not_tmp() -> None:
    # The watcher's throwaway worktrees must NOT default under /tmp (the agent harness GCs /tmp mid-grade).
    from ops.ci_watcher import WORKDIR_BASE

    assert not WORKDIR_BASE.startswith("/tmp/"), f"WORKDIR_BASE under /tmp: {WORKDIR_BASE}"


def test_workdir_creates_dir_under_base(tmp_path: object) -> None:
    # workdir() roots the temp dir at WORKDIR_BASE, not the OS default /tmp.
    import os

    import ops.ci_watcher as cw

    saved = cw.WORKDIR_BASE
    cw.WORKDIR_BASE = os.path.join(str(tmp_path), "ci-work")
    try:
        with cw.workdir(prefix="ci-wt-") as path:
            assert path.startswith(cw.WORKDIR_BASE)
            assert os.path.isdir(path)
        assert not os.path.exists(path)  # cleaned up on context exit
    finally:
        cw.WORKDIR_BASE = saved
