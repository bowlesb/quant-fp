# swing_dc MAGNITUDE — REPLICATION VERDICT: CONFIRMED on a disjoint window

**Date:** 2026-06-20 · The confirmatory replication the t-13-on-40-dates demanded (short panels manufacture
huge t-stats → a disjoint window is the gate). Original: 40 RECENT dates (2026-04-23→06-18). Replication:
**70 DISJOINT dates (2024-12-12→2026-04-15)** — fully non-overlapping, the earlier ~1.3 years of the tick
tape (swing_dc needs n_trades/spread, which only exist 2024-12-12+). swing_dc computed from raw bars via the
fp-dev-swingdc kernel image. Same identical magnitude feature-utility screen, STRICT control
(own_vol + size + dc_sigma30_bps, the group's own vol estimate). READ-ONLY.

## TL;DR — CONFIRMED. 9/9 original survivors hold their own-vol-independent magnitude IC on disjoint data. swing_dc IS a net-new magnitude feature (not a window artifact).

| feature | orig partial-IC | DISJOINT partial-IC | t | OOS | shuffle-z | verdict |
|---|---|---|---|---|---|---|
| **dc_resp_chunk_slope** | +0.143 | **+0.148** | 17.4 | consistent | 14.4 | ✅ CONFIRMED |
| dc_resp_nlegs_slope | −0.151 | −0.123 | −13.2 | consistent | −12.5 | ✅ |
| dc_resp_roughness | +0.106 | +0.074 | 8.4 | consistent | 9.3 | ✅ |
| dc_minutes_since_dc_s4 | −0.121 | −0.121 | −13.7 | consistent | −12.9 | ✅ |
| dc_last_leg_dur_s1 | −0.087 | −0.122 | −13.1 | consistent | −15.6 | ✅ |
| dc_os_to_dc_s4 | +0.111 | +0.107 | 10.6 | consistent | 12.2 | ✅ |
| dc_minutes_since_dc_s2 | −0.093 | −0.132 | −13.9 | consistent | −16.1 | ✅ |
| dc_minutes_since_dc_s1 | −0.092 | −0.117 | −11.9 | consistent | −12.7 | ✅ |
| dc_resp_os_ratio_mean | +0.106 | +0.088 | 8.3 | consistent | 11.9 | ✅ |

The headline `dc_resp_chunk_slope` reproduced **+0.143 → +0.148** on completely disjoint data — the
own-vol-independent magnitude IC is stable across two non-overlapping periods, OOS-consistent within each,
shuffle-robust (z 14). Every survivor holds sign + magnitude (within noise). This is NOT a short-panel
artifact; the DC threshold-response / roughness / DC-timing structure is a genuine net-new magnitude signal.

## What this confirms — and what it does NOT
- ✅ swing_dc's response-signature/roughness/timing features carry REAL, REPLICATED, own-vol-independent
  (net even of the group's own dc_sigma30 vol estimate) forward MOVE-MAGNITUDE predictive power. The
  parameter-free roughness fingerprint the multi-scale DC design was built for WORKS.
- ⚠️ This is a magnitude FEATURE, NOT tradeable alpha. It predicts move-SIZE (net of own-vol), so its value
  is as a MODEL INPUT — vol-aware sizing, a magnitude/risk feature for the strategy-battery — not a
  standalone directional P&L. swing_dc DIRECTION remains a null (#249). No tradeable-edge claim is made.

## Disposition — swing_dc RE-PROMOTES as a magnitude feature (the prerequisites)
This clears the confirmatory-replication gate → swing_dc returns as a net-new magnitude feature. The path:
1. ⚠️ FIX THE KERNEL DEFECT: #242 merged the Python group but NOT the Rust `swing_dc_fold` kernel (only on
   FeatureDev's worktree + the fp-dev-swingdc image). FeatureDev must land the `rust/` kernel on main +
   rebuild the fp-dev image — swing_dc cannot ship without it.
2. PARITY-VERIFY (the staged group's live==backfill, the platform bar) once the kernel lands.
3. The Lead sequences the controlled deploy (fp-affecting: 728→802).
4. THEN the harness tests whether the magnitude prediction translates to TRADEABLE value (vol-aware sizing /
   the magnitude family) — the feature being trustworthy is necessary, not sufficient, for $.

## Method
70 disjoint dates, top-200 liquid/day, |fwd-30m return| target, partial-IC net of own_vol+size+dc_sigma30,
OOS purge split + 60-iter shuffle per feature. CONFIRMED iff sign matches original + |pIC|≥0.02 + OOS-
consistent + |shuffle-z|≥3. All 9 pass. fp-dev-swingdc image (the only one with the kernel).
`replication_results.csv` + `swing_dc_panel_disjoint.parquet` (gitignored) are the record.
