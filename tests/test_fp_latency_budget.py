"""Per-group live-path latency BUDGET gate — the machine-checked accountability layer (docs/latency_budget.yaml).

Replaces the two loose GLOBAL ceilings (us/feature + a trades ms) in test_fp_latency.py, under which a 3x
single-group regression passed silently. Here EVERY runnable group has a checked-in ``compute_latest`` budget;
the gate runs the SAME ``profile(latest=True)`` harness the budget was seeded from, at the SAME reference
shard, and asserts each group stays under its budget — re-confirming a suspect with more reps so transient
host load does not flake the gate (the same host-load-robust pattern as the existing latency gate).

When a group breaches: it is a REGRESSION to investigate (an accidental O(buffer) scan, a lost latest-only
form), NOT a number to bump. Re-seed a budget only on a DELIBERATE, reviewed change (regenerate the YAML).
"""

from __future__ import annotations

from pathlib import Path

from quantlib.features.profile import build_frames, profile

_BUDGET_PATH = Path(__file__).resolve().parents[1] / "docs" / "latency_budget.yaml"
_SCREEN_REPS = 5  # first pass — min over these to shake off most contention noise
_CONFIRM_REPS = 15  # re-measure a suspect with many more samples; a real regression survives, noise does not


def _parse_budget(path: Path) -> tuple[dict[str, object], dict[str, float]]:
    """Minimal parser for the intentionally-flat budget YAML (no dependency): a ``reference_shard:`` block of
    scalar key/values and a ``budgets:`` block of ``group: float`` lines. Comments (``#``) and blanks ignored.
    Raises on a malformed line so a typo in the budget file fails LOUDLY, not silently."""
    reference: dict[str, object] = {}
    budgets: dict[str, float] = {}
    section: str | None = None
    for raw in path.read_text().splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        if not line.startswith(" "):
            section = line.rstrip(":").strip()
            continue
        key, _, value = line.strip().partition(":")
        key, value = key.strip(), value.strip()
        if section == "reference_shard":
            if value in ("true", "false"):
                reference[key] = value == "true"
            else:
                reference[key] = int(value)
        elif section == "budgets":
            budgets[key] = float(value)
    if not budgets:
        raise ValueError(f"no budgets parsed from {path}")
    return reference, budgets


def test_every_group_under_its_latency_budget() -> None:
    reference, budgets = _parse_budget(_BUDGET_PATH)
    frames = build_frames(
        n_tickers=int(reference["n_tickers"]),
        window_min=int(reference["window_min"]),
        daily_days=int(reference["daily_days"]),
        include_trades=bool(reference["include_trades"]),
    )
    measured = profile(frames, reps=_SCREEN_REPS, latest=True)
    over = [
        row
        for row in measured.iter_rows(named=True)
        if row["group"] in budgets and row["ms"] > budgets[row["group"]]
    ]
    if not over:
        return

    # Re-confirm the suspects with many more reps before failing — a real regression survives the re-measure,
    # transient host contention does not (host-load-robust, matching the existing latency gates).
    suspect_names = {row["group"] for row in over}
    reconfirm = profile(frames, reps=_CONFIRM_REPS, latest=True)
    confirmed = [
        row
        for row in reconfirm.iter_rows(named=True)
        if row["group"] in suspect_names and row["ms"] > budgets[row["group"]]
    ]
    assert not confirmed, (
        "groups over their docs/latency_budget.yaml live-path budget (CONFIRMED on a "
        + (f"{_CONFIRM_REPS}-rep re-measure — a real regression, not host noise):\n")
        + "\n".join(
            f"  {row['group']}: {row['ms']:.1f}ms > budget {budgets[row['group']]:.1f}ms "
            f"({row['ms'] / budgets[row['group']]:.2f}x)"
            for row in sorted(confirmed, key=lambda r: -r["ms"] / budgets[r["group"]])
        )
    )


def test_budget_covers_every_runnable_group() -> None:
    """The budget file must list EVERY runnable group — a new group added without a budget would slip the gate
    entirely (the silent-regression hole this harness closes). A group in the budget but not runnable here
    (e.g. an input-gated one) is allowed; a runnable group MISSING from the budget fails."""
    reference, budgets = _parse_budget(_BUDGET_PATH)
    frames = build_frames(
        n_tickers=int(reference["n_tickers"]),
        window_min=int(reference["window_min"]),
        daily_days=int(reference["daily_days"]),
        include_trades=bool(reference["include_trades"]),
    )
    measured = profile(frames, reps=1, latest=True)
    runnable_groups = {row["group"] for row in measured.iter_rows(named=True)}
    missing = sorted(runnable_groups - set(budgets))
    assert not missing, (
        f"runnable groups with NO entry in docs/latency_budget.yaml (add a budget so the gate covers them): "
        f"{missing}"
    )
