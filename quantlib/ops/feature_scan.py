"""Continuous feature-data defender — scans EVERY feature group's live store output and surfaces
data-quality regressions: dead features (no values, usually a code/system bug), high-NaN, constant
(dead/degenerate), and non-finite (inf) values.

Design intent (the "aggressively, repetitively question the numbers" culture): cheap enough to run on a
tight cron so we re-check the same things over and over and SEE the effect of a change right away.

The key discriminator that separates a real bug from transient warmup: null% is measured over a sample
spread across the WHOLE day, not just the latest minutes. A feature that is ~100% null across the whole
day was NEVER computed (a structural bug); one that is null only recently is warmup/restart recovery.

Read-only. Runs inside the feature-computer/fp-dev container (polars + /store mount).
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import polars as pl

ET = ZoneInfo("America/New_York")
STORE_ROOT = os.environ.get("STORE_ROOT", "/store")
DEAD_THRESHOLD = 0.999  # null+nan fraction over the whole-day sample at/above which a feature is "dead"
HIGH_NAN_THRESHOLD = 0.50
SAMPLE_FILES = 40  # files sampled evenly across the day per group (full-day coverage, bounded cost)
WINDOW_RE = re.compile(r"_(\d+)m$")


def et_today() -> str:
    return datetime.now(tz=ET).date().isoformat()


def sample_paths(group: str, day: str) -> list[str]:
    paths = sorted(glob.glob(f"{STORE_ROOT}/group={group}/v=*/source=stream/date={day}/*.parquet"))
    if len(paths) <= SAMPLE_FILES:
        return paths
    step = len(paths) / SAMPLE_FILES
    return [paths[int(i * step)] for i in range(SAMPLE_FILES)]


def feature_window(name: str) -> int | None:
    match = WINDOW_RE.search(name)
    return int(match.group(1)) if match else None


def scan_group(group: str, day: str) -> dict[str, object] | None:
    paths = sample_paths(group, day)
    if not paths:
        return None
    frame = pl.concat([pl.read_parquet(path) for path in paths], how="vertical_relaxed")
    rows = frame.height
    if rows == 0:
        return None
    feats = [col for col in frame.columns if col not in ("symbol", "minute")]
    dead: list[str] = []
    high_nan: list[tuple[str, int]] = []
    const: list[str] = []
    inf_feats: list[str] = []
    bad_cells = 0
    for col in feats:
        series = frame[col]
        is_float = series.dtype.is_float()
        bad = series.null_count() + (series.is_nan().sum() if is_float else 0)
        bad_cells += bad
        frac = bad / rows
        if frac >= DEAD_THRESHOLD:
            dead.append(col)
        elif frac > HIGH_NAN_THRESHOLD:
            high_nan.append((col, round(frac * 100)))
        non_null = series.drop_nulls()
        if non_null.len() > 0 and non_null.n_unique() == 1:
            const.append(col)
        if is_float and series.is_infinite().sum() > 0:
            inf_feats.append(col)
    return {
        "group": group,
        "n_features": len(feats),
        "rows": rows,
        "nan_pct": round(100 * bad_cells / (rows * len(feats)), 1) if feats else 0.0,
        "dead": dead,
        "high_nan": high_nan,
        "const": const,
        "inf": inf_feats,
    }


def discover_groups() -> list[str]:
    return sorted({path.split("group=")[1].split("/")[0] for path in glob.glob(f"{STORE_ROOT}/group=*/")})


def run_scan(day: str) -> dict[str, object]:
    results = []
    for group in discover_groups():
        result = scan_group(group, day)
        if result is not None:
            results.append(result)
    # Structural dead = a no-window feature (should always compute) or a short-window (<=5m) feature that
    # is dead across the whole day. Those are bugs, not warmup. Long-window dead is reported but not alarmed.
    structural_dead = []
    for result in results:
        for name in result["dead"]:  # type: ignore[union-attr]
            window = feature_window(name)
            if window is None or window <= 5:
                structural_dead.append(f"{result['group']}.{name}")
    return {
        "day": day,
        "groups": results,
        "total_features": sum(int(r["n_features"]) for r in results),
        "total_dead": sum(len(r["dead"]) for r in results),  # type: ignore[arg-type]
        "structural_dead": structural_dead,
    }


def render_text(report: dict[str, object]) -> str:
    lines = [f"FEATURE-SCAN {report['day']}  ({report['total_dead']}/{report['total_features']} features dead)"]
    for result in report["groups"]:  # type: ignore[union-attr]
        flags = []
        if result["dead"]:
            flags.append(f"dead={len(result['dead'])}")
        if result["const"]:
            flags.append(f"const={len(result['const'])}")
        if result["inf"]:
            flags.append(f"inf={len(result['inf'])}")
        worst = ",".join(result["dead"][:3]) or ",".join(f"{c}:{p}%" for c, p in result["high_nan"][:2])
        lines.append(
            f"  {result['group']:24s} feats={result['n_features']:>3} nan={result['nan_pct']:>5}%"
            f"  {' '.join(flags):24s} {worst}"
        )
    structural = report["structural_dead"]
    lines.append("")
    if structural:
        lines.append(f"⚠ STRUCTURAL DEAD (short/no-window features with NO values all day — real bugs): {len(structural)}")
        for name in structural:
            lines.append(f"    {name}")
    else:
        lines.append("✓ no structural-dead features (every short/no-window feature produced values today)")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Scan live feature store for dead/NaN/const/inf regressions.")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of text")
    parser.add_argument("--day", default=None, help="ET date YYYY-MM-DD (default: today)")
    args = parser.parse_args()
    day = args.day or et_today()
    report = run_scan(day)
    if args.json:
        print(json.dumps(report))
    else:
        print(render_text(report))
    # Non-zero exit when a structural-dead feature exists, so the cron can alert.
    sys.exit(1 if report["structural_dead"] else 0)


if __name__ == "__main__":
    main()
