# HF02 — Can a LOWER-TURNOVER qimb construction clear the cost gate? (cycle 2, pre-registration)

**Registered:** 2026-06-16, BEFORE any run. Follow-up to HF01 (KILL on the turnover-compounded cost gate).
Still menu #1 (zero-dependency: data + bus in hand). NO fingerprint/feature-group changes — research only.

## What HF01 established (the one real thread)

HF01 found `qimb` (top-of-book size imbalance) is a FAINT but REAL slow signal: it canary-passes at all 9
(window×horizon) cells, its IC GROWS with horizon (0.0033 @1m → **0.0128 @5m**, sign-correct =
price-pressure continuation), and it's the only signal that does (OFI/stflow are noise intraday). It KILLED
purely on COST: best cell qimb/120/5 net +0.04 bps @1× / −0.37 bps @2× — the ~0.45 bps gross is eaten by the
~2.7 bps turnover-compounded round-trip. The signal is real; the turnover is the killer.

## Hypothesis

Because qimb is SLOW (IC strengthens with horizon) and HF01's best cell already had LOW turnover (0.15 at
h=5m, band0), a construction that pushes the holding horizon LONGER (15–30+ min) and trades only on
PERSISTENCE — paying the ~2.7 bps round-trip far less often, amortized over a larger move — clears the
turnover-compounded cost gate at the measured spread, OOS, with day-clustered t ≥ 2.

## Test design

1. **Reuse HF01's qimb construction** (10s buckets, mid-return target, trailing-window book imbalance) but
   extend the HORIZON grid to h ∈ {5, 10, 15, 30} min, and add longer trailing windows w ∈ {120, 300} s
   (qimb averaged over 2–5 min is steadier → lower flip-rate).
2. **Lower-turnover overlays (the core variable):**
   - HOLD overlay: once entered, hold for the full h minutes (no mid-hold re-evaluation), so turnover ≈ 1/h.
   - PERSISTENCE band: only enter when qimb has held the SAME sign for the trailing K buckets AND |qimb|
     exceeds a threshold; sweep K + threshold; only flip on a material, persistent reversal (hysteresis).
   - Report turnover for each overlay and the net AFTER turnover×round_trip at the MEASURED spread + 2×.
3. **THE FIXED GATES (HF01's OOS+demean bug is fixed):** use `hf_metrics_fixed.py` (per-(symbol,date) IC,
   null-safe within-symbol demean — VERIFIED to recover a planted IC without NaN). Shuffle-canary FIRST,
   per-symbol demean, walk-forward OOS (TRAIN/OOS 50/50, demean within split), then the turnover-compounded
   cost gate. The DECISIVE number is OOS turnover-compounded NET at the measured spread.
4. Bid-ask-bounce defense unchanged: MID-return target, cross-spread costing, signal strictly trailing.
5. Bigger panel if memory allows (the ~12 ≥21-day megacaps, not just 5) — HF01's 5-name panel was
   cost-conclusive but IC-modest; more names sharpen the IC/OOS power.

## Expected / confidence

- Confidence a (h≥15m × persistence-band) qimb cell nets POSITIVE after turnover-compounded cost OOS with
  t≥2: **~20%.** Lower than HF01's 30% prior because HF01 already showed the gross is only ~0.45 bps at h=5m
  and only GROWS slowly with horizon — to clear a ~2.7 bps round-trip even at turnover 1/30 (one trade per
  30 min) the per-trade move must reach ~2.7+ bps, which needs the 30-min qimb gross to be several× the 5-min
  gross. Possible (IC was still rising at h=5m) but not likely. I pre-commit to the low prior.
- KEEP-AS-LEAD: a cell clears canary+demean+OOS AND nets positive after turnover-compounded cost at measured
  spread, robust to 2× stress. → parity-safe qimb feature proposal (LATER) + HF paper container.
- AMBIGUOUS: OOS net ≈ 0, or positive only pre-2×-stress.
- KILL: no cell nets positive after turnover-compounded cost OOS → qimb is real but un-tradeable at every
  horizon, and the book-imbalance class is closed. Then HF03 (a NON-directional bet: liquidity-provision /
  spread-capture — does POSTING at the touch when the book is balanced earn the spread? a market-making
  rather than alpha bet, where you EARN the spread instead of paying it — inverting the cost adversary).

## Ordering

HF02 is the immediate follow-up (reuses HF01 infra + the verified metric fix). If HF02 KILLs, qimb-directional
is done and HF03 (spread-capture / liquidity provision — the cost INVERSION bet) is the next pre-registered
probe. All still menu #1, zero-dependency.
