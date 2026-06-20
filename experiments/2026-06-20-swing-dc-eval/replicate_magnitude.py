"""swing_dc MAGNITUDE — CONFIRMATORY REPLICATION on a DISJOINT, DEEPER window.

The original magnitude win (dc_resp_chunk_slope partial-IC +0.143, t 13.5) was on 40 RECENT dates
(2026-04-23→06-18). A t-13 on a 40-date panel is exactly the case that demands disjoint confirmation (short
panels manufacture huge t-stats). This rebuilds the swing_dc magnitude panel on an EARLIER, NON-OVERLAPPING
window from the deep raw-bar tape (swing_dc computes from raw bars via the fp-dev-swingdc kernel — NO DI
backfill needed), and re-runs the IDENTICAL feature-utility screen. CONFIRMED iff the survivors hold their
partial-IC (net of own-vol + dc_sigma30), sign-stable + shuffle-robust on the disjoint window; REFUTED iff
they collapse/flip (= window-specific overfit).

Reuses build_date() from eval_swing_dc (same swing_dc compute + own_vol/size + |fwd-30m| panel construction).
READ-ONLY. fp-dev-swingdc image. Writes replication_results.csv + console.
"""

from __future__ import annotations

import glob
import math
import os
import sys

import numpy as np
import polars as pl

sys.path.insert(0, "/app/experiments/2026-06-20-swing-dc-eval")
import eval_swing_dc as E  # noqa: E402  (reuse build_date)

OUT_DIR = "/app/experiments/2026-06-20-swing-dc-eval"
STORE = "/store"
# DISJOINT window: earlier dates, non-overlapping with the original 2026-04-23..06-18.
SPAN_START = os.environ.get("SPAN_START", "2024-01-01")
SPAN_END = os.environ.get("SPAN_END", "2024-12-31")
N_DATES = int(os.environ.get("N_DATES", "60"))
SEED = 7
MIN_IC = 0.02
# the original survivors to re-test (the headline + the response-signature family + DC-timing)
ORIG = {
    "dc_resp_chunk_slope": 0.143,
    "dc_resp_nlegs_slope": -0.151,
    "dc_resp_roughness": 0.106,
    "dc_minutes_since_dc_s4": -0.121,
    "dc_last_leg_dur_s1": -0.087,
    "dc_os_to_dc_s4": 0.111,
    "dc_minutes_since_dc_s2": -0.093,
    "dc_minutes_since_dc_s1": -0.092,
    "dc_resp_os_ratio_mean": 0.106,
}


def rank(a):
    return a.argsort().argsort().astype(float)


def sp(x, y):
    return (
        float(np.corrcoef(rank(x), rank(y))[0, 1])
        if np.std(rank(x)) > 1e-12 and np.std(rank(y)) > 1e-12
        else float("nan")
    )


def partial_ic(df, f, ctrls, shuf=False, rng=None):
    pars = []
    for (_,), g in df.group_by(["date"]):
        if g.height < 20:
            continue
        x = g[f].to_numpy()
        y = g["y"].to_numpy()
        if shuf:
            y = rng.permutation(y)
        if np.std(x) < 1e-12:
            continue
        Z = np.column_stack([np.ones(g.height)] + [g[c].to_numpy() for c in ctrls])
        rx = x - Z @ np.linalg.lstsq(Z, x, rcond=None)[0]
        ry = y - Z @ np.linalg.lstsq(Z, y, rcond=None)[0]
        if np.std(rx) > 1e-12 and np.std(ry) > 1e-12:
            pars.append(sp(rx, ry))
    return np.array(pars)


def main() -> None:
    days = sorted(
        p.split("date=")[1].split("/")[0] for p in glob.glob(f"{STORE}/raw/bars/symbol=SPY/date=*")
    )
    days = [d for d in days if SPAN_START <= d <= SPAN_END]
    # evenly sample N_DATES across the disjoint span (breadth > contiguity for a robustness check)
    if len(days) > N_DATES:
        idx = np.linspace(0, len(days) - 1, N_DATES).astype(int)
        days = [days[i] for i in idx]
    print(
        f"REPLICATION window: {len(days)} disjoint dates {days[0]}..{days[-1]} (orig was 2026-04-23..06-18)",
        flush=True,
    )
    frames = []
    for i, day in enumerate(days):
        d = E.build_date(day)
        if d.height:
            frames.append(d)
        if (i + 1) % 10 == 0:
            print(f"  built {i+1}/{len(days)} ({sum(f.height for f in frames)} rows)", flush=True)
    panel = pl.concat(frames, how="vertical_relaxed").with_columns(pl.col("y_fwd").abs().alias("y"))
    lo = pl.col("y").quantile(0.01).over("date")
    hi = pl.col("y").quantile(0.99).over("date")
    panel = panel.with_columns(pl.col("y").clip(lo, hi))
    panel.write_parquet(f"{OUT_DIR}/swing_dc_panel_disjoint.parquet")
    print(f"disjoint panel: {panel.height} rows, {panel['date'].n_unique()} days", flush=True)

    chk = panel.select(["own_vol", "y"]).drop_nulls()
    print(
        f"  own_vol vs |fwd| IC = {sp(chk['own_vol'].to_numpy(), chk['y'].to_numpy()):+.3f} (the confound)"
    )
    ctrls = ["own_vol", "size", "dc_sigma30_bps"]  # the STRICT control (incl the group's own vol)
    days_p = sorted(panel["date"].unique().to_list())
    mid = days_p[len(days_p) // 2]
    rng = np.random.default_rng(SEED)
    rows = []
    print("\n=== DISJOINT-WINDOW magnitude partial-IC (ctrl own_vol+size+dc_sigma30) vs ORIGINAL ===")
    print(f"{'feature':26s} | orig pIC | disjoint pIC | t | OOS | shuffle-z | verdict")
    for f, orig_pic in ORIG.items():
        df = panel.select(["date", f, *ctrls, "y"]).drop_nulls()
        a = partial_ic(df, f, ctrls)
        if len(a) < 8:
            print(f"{f:26s} | {orig_pic:+.3f}   | (insufficient days)")
            continue
        pic = float(a.mean())
        t = float(a.mean() / (a.std(ddof=1) / np.sqrt(len(a)) + 1e-12))
        e = partial_ic(df.filter(pl.col("date") < mid), f, ctrls)
        l = partial_ic(df.filter(pl.col("date") > mid), f, ctrls)
        oos = "consistent" if (len(e) and len(l) and np.sign(e.mean()) == np.sign(l.mean())) else "FLIP"
        null = np.array([partial_ic(df, f, ctrls, True, rng).mean() for _ in range(60)])
        z = (pic - null.mean()) / (null.std() + 1e-9)
        # CONFIRMED iff: same sign as original, |pIC|>=MIN_IC, OOS-consistent, shuffle |z|>=3
        confirmed = (
            (np.sign(pic) == np.sign(orig_pic))
            and abs(pic) >= MIN_IC
            and oos == "consistent"
            and abs(z) >= 3
        )
        verdict = "CONFIRMED" if confirmed else ("WEAK/FLIP" if abs(pic) >= MIN_IC else "COLLAPSED")
        rows.append(
            {
                "feature": f,
                "orig_pic": orig_pic,
                "disjoint_pic": pic,
                "t": t,
                "oos": oos,
                "shuffle_z": z,
                "verdict": verdict,
            }
        )
        print(
            f"{f:26s} | {orig_pic:+.3f}   | {pic:+.3f}       | {t:+.1f} | {oos:10s} | {z:+.1f} | {verdict}"
        )

    pl.DataFrame(rows).write_csv(f"{OUT_DIR}/replication_results.csv")
    n_conf = sum(1 for r in rows if r["verdict"] == "CONFIRMED")
    print(
        f"\n=== ⭐ REPLICATION VERDICT: {n_conf}/{len(rows)} original survivors CONFIRMED on the disjoint window ==="
    )
    head = next((r for r in rows if r["feature"] == "dc_resp_chunk_slope"), None)
    if head:
        print(
            f"  HEADLINE dc_resp_chunk_slope: orig +0.143 → disjoint {head['disjoint_pic']:+.3f} (t {head['t']:.1f}, {head['oos']}, z {head['shuffle_z']:+.1f}) → {head['verdict']}"
        )


if __name__ == "__main__":
    main()
