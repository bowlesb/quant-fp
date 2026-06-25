# RESULTS — Regime-conditional feature interactions (the last unturned alpha cell)

**Date run:** 2026-06-25  **Author:** Modeller  **Status:** COMPLETE — frozen §7 verdict filled below.
**Verdict:** **NO-GO** (0 of 40 conditional cells clear all 5 frozen gates).

This documents the pre-registered screen in `PRE_REGISTRATION.md` (frozen at code SHA `1d46b52`,
committed to `main` via #465 at `b751076`). Every degree of freedom — features, regimes, horizons,
gates, and the GO/NO-GO rule — was locked BEFORE any number was produced. No post-hoc edits.

---

## 1. Code / data state

- **Pre-reg code SHA:** `1d46b52` (origin/main HEAD at pre-reg time; pre-reg committed at `b751076`, #465).
- **Screen script:** ran per the §8 execution discipline as an ephemeral `/tmp` job against the trusted
  feature store (`/store` RO); it was not committed (experiments/ scratch, not quantlib). Its exact SHA
  is therefore not recoverable from the artifacts. No quantlib/groups/fingerprint change was made — this
  was a read-only IC + decile-basket screen, fp-neutral.
- **Panel:** 45 trusted dates **2026-04-15 .. 2026-06-18** (the ≥500-sym-coverage common window — 45 dates,
  one fewer than the "~46" pre-reg estimate; the exact set is fixed by coverage, logged in `screen.log`).
  Universe = top-200 by trailing ADV per day. 424,200 panel rows.
- **Grid:** 5 features × 4 regime buckets × 2 horizons = **40 conditional cells** (all 40 evaluable;
  `n_evaluable_cells=40`, `data_absent_features=[]`).

### Feature / regime density (from `screen.log`)

A material data-coverage caveat surfaced at panel build and is reported honestly here:

| Channel | Density |
|---|---|
| F1 `ret_15m` | 99.1% |
| F2 `quote_imbalance_15m` | **3.4%** |
| F3 `signed_trade_ratio_15m` | **8.1%** |
| F4 `ret_60m` | 98.8% |
| F5 `vwap_deviation_15m` | 98.0% |
| R-VOL (`realized_vol_30m`) | 99.8% |
| R-LIQ (liquidity scalar) | **3.4%** |
| `_cost` (Stage-1 realized half-spread) | **3.4%** |

F2, F3, the R-LIQ regime axis, and the realized-cost column are sparse on this window. This thins the
R-LIQ buckets and the F2/F3 cells (small `n_cell`), and means the cost charge is only populated on the
liquid-tier sliver where it exists. It does not rescue any cell — see §3 — but it is the honest reason
the R-LIQ and F2/F3 cells are statistically weak rather than merely null.

---

## 2. Counter-bug reconciliation (REQUIRED honesty note)

`summary.json` reports `n_nwt_ge2 = 8` while `max_nw_t = 0.8853`. **These are mutually inconsistent:** if the
maximum per-day NW-t across all 40 cells is 0.885, then ZERO cells can have NW-t ≥ 2. The reported `8` is a
**counter bug** in the screen's summary aggregation.

I re-derived every per-gate count directly from `regime_cells.csv` (column `nw_t_5pct`, the per-day NW-t of
the conditional L/S basket at the ≥5% cut, which is the Gate-2 statistic). The TRUE count of cells with
`nw_t_5pct ≥ 2` is **0** — fully consistent with `max_nw_t = 0.8853`. The exact buggy predicate is not
recoverable (the script was ephemeral; `|nw_t| ≥ 2` would give 26, not 8, so it was not a clean abs — most
likely a NaN/sign/column-index slip in the summary tally). The fix is not load-bearing: the GO/NO-GO turns
on the **intersection** of all five gates, which is 0 under either count.

The other three reported gate counts reconcile EXACTLY against the CSV and are correct as reported.

### Corrected per-gate counts (re-derived from `regime_cells.csv`)

| Gate | Pre-reg definition (§6/§7) | Reported (summary.json) | **Corrected (from CSV)** |
|---|---|---|---|
| FDR | incr-IC > 0 AND BY-FDR q=0.10 reject across N=40 (`fdr_reject`) | 3 | **3** ✓ |
| Per-day NW-t ≥ 2 | `nw_t_5pct ≥ 2` at the ≥5% cut | 8 | **0** ✗ → corrected |
| Beats shuffle | conditional IC dominates within-bucket label shuffle (`ic_beats_shuffle`) | 10 | **10** ✓ |
| Sign-replication | holds sign across both disjoint OOS halves (`sign_replicates`) | 15 | **15** ✓ |
| **ALL 5 gates** (incl. 2%→5% non-collapse) | §7 intersection (`GO_all_gates`) | 0 | **0** ✓ |

`max_incr_ic = 0.2319`. `max_nw_t = 0.8853` (the single best per-day t, far below the 2.0 gate).

---

## 3. Per-cell results vs the five frozen gates

The full 40-cell table is in `regime_cells.csv` (and `screen.log`). The decisive findings:

- **Gate 1 (FDR + incr-IC > 0):** only 3 cells pass — F3 `signed_trade_ratio_15m` in R-VOL-high (H15,
  incr-IC +0.196) and R-VOL-low (H15, incr-IC +0.232, the global max), and F5 `vwap_deviation_15m` in
  R-VOL-low (H30, incr-IC +0.122). Note F5's "pass" sits on a degenerate thin slice (`n_days = 3`,
  `cond_ic = 1.0`) — a coverage artifact, not a signal.
- **Gate 2 (per-day NW-t ≥ 2):** **0 cells.** The most positive per-day t anywhere is **0.885**
  (F4 `ret_60m`, R-LIQ-high, H30); every other cell is below it and 30 of 32 non-NaN cells have a
  *negative* basket t (the L/S baskets lose money net of Stage-1 cost). The three FDR-passing cells have
  NW-t of −12.4, −13.0, and NaN respectively — the IC sign does not translate into a tradeable, costed,
  per-day-significant basket.
- **Gate 3 (no 2%→5% ≥3× collapse), Gate 4 (beats shuffle), Gate 5 (sign-replication):** individually 10
  and 15 cells clear shuffle / sign-replication, but no cell that clears Gates 1–2 also clears the rest.
- **Intersection of all five gates = 0** (`GO_all_gates` is false for all 40 rows).

The pattern is the expected one: the cells with the largest incremental conditional IC (F3 in the vol
buckets) are exactly the ones whose costed L/S basket is most strongly *negative* per day — the apparent
conditional rank-IC is not a tradeable directional edge once Stage-1 cost and per-day t are applied.

---

## 4. FROZEN VERDICT (§7)

**NO-GO.** No (feature × regime × horizon) cell satisfies all five pre-committed gates:

1. incremental conditional rank-IC > 0 AND survives BY-FDR q=0.10 across N=40 — 3 cells; AND
2. per-day NW-t ≥ 2 at a ≥5% cut — **0 cells** (max 0.885); AND
3. $ does not collapse ≥3× from 2%→5%; AND
4. conditional IC dominates the within-bucket label-shuffle; AND
5. holds sign + FDR-significance in BOTH disjoint OOS halves.

The intersection is **0**. The strongest cell maxed a per-day NW-t of **0.885 < 2** — it does not clear the
single hardest gate (the same per-day-t discipline that killed the 06-25 tick screen), let alone all five.

**Interpretation (per §1/§7):** the unconditional nulls were **not** hiding a regime-specific direction
effect. An effect that fires only in a vol/liquidity tercile and is washed out unconditionally does not
exist on the current data at tradeable, costed significance. This was the last genuinely unturned alpha
cell (per-name feature × regime interaction on a per-name forward return); **every direction lane is now
settled-null.** The alpha space is exhausted on current data.

**Routing (frozen in §7, no new degree of freedom):**
- **NO feature PR. NO backfill spend. NO follow-on regime screen** (the 2-D vol×liq cross was conditioned
  on this firing — it did not).
- Route future Modeller EV to: (a) **delisting-inclusive universe acquisition** so weekly-reversal's clean
  +0.025 IC becomes tradeable, and (b) **baseline deployment-hardening**.
- The strategic pivot (deployment-harden the net-positive baseline vs. continue hunting) is Ben's call.

---

## 5. Artifacts in this directory

| File | Contents |
|---|---|
| `PRE_REGISTRATION.md` | The frozen pre-reg (committed pre-run via #465). |
| `RESULTS.md` | This document. |
| `summary.json` | Raw screen summary (note: `n_nwt_ge2=8` is the counter bug — see §2; true = 0). |
| `regime_cells.csv` | Per-cell results table (40 rows × all gate columns) — the source of truth for §2/§3. |
| `screen.log` | Full screen run log (panel build, densities, per-cell table, summary). |

`regime_panel.parquet` (the 384 KB regenerable per-name panel) and `regime_cells.parquet` (the binary twin
of the csv) are intentionally NOT committed — regenerable / redundant with the csv.
