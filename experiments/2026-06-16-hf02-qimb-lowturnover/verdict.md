# HF02 Verdict — qimb low-turnover overlays: **KILL** (the per-trade bootstrap settles it)

**Pre-committed criterion:** KEEP-AS-LEAD iff a cell clears canary + demean + OOS AND nets positive after
turnover-compounded cost at the measured spread, robust to 2× — AND (the decisive HF check) the per-TRADE
realized net-of-cost is significantly positive (bootstrap 95% CI excludes zero), since a low-turnover net
rests on few trades.

## Verdict: KILL

The "KEEP" the first pass reported was an ARTIFACT of two bugs, both now corrected, and the honest decisive
test — the per-trade bootstrap — KILLS it.

### What the first pass got wrong
1. **Clustering bug (t inflated ~3.5×):** the OOS/demean t-stat counted per-(symbol,date) CELLS as
   independent (n≈204) instead of day-clustering (n_days). Fixed in `hf_metrics_fixed.py`
   (RESEARCH_PITFALLS #5). The corrected OOS day-clustered t is lower but still high for some cells — and
   that does not matter, because:
2. **The `net_bps` aggregate is NOT the tradeable number.** `gross_bps`/`net_bps` is a SIGNAL-WEIGHTED
   average that does not honestly book the spread crossed on each REALIZED round-trip. It read +1.0 to +1.27
   bps "positive". The honest number is the per-TRADE realized net.

### The decisive evidence — per-trade bootstrap (the honest P&L)

Booking each realized entry→exit round-trip with the measured spread crossed:

| cell | n_trades | per-trade mean (1×) | per-trade t | 95% CI (1×) | reality |
|---|---:|---:|---:|---|---|
| **w300/h30/HOLD (the "headline")** | 1,234 | **−0.20 bps** | −0.13 | **[−3.16, +2.79]** straddles 0 | noise |
| w120/h15/HOLD | 2,501 | **−3.08 bps** | −3.58 | [−4.77, −1.43] **< 0** | loser |
| w120/h15/PERSISTENCE (best of) | 3,442 | **−2.55 bps** | −3.76 | [−3.85, −1.24] **< 0** | loser |

**ZERO of 80 cells have a per-trade 95% CI lower-bound above zero.** Not one cell is genuinely net-positive
per realized trade; most are SIGNIFICANTLY NEGATIVE. The headline HOLD-30m cell is indistinguishable from
zero (CI straddles, t=−0.13, 44% win rate).

### Why the IC was real but the trade loses

qimb genuinely correlates with the next mid-return (the IC is real and day-clustered-significant). But the
correlation is FAR too weak to overcome the spread you must cross on every realized round-trip: at ~0.5–2.5
bps half-spread and a per-trade edge that is essentially zero (−0.2 bps) after honestly crossing, the signal
does not pay. The IC-weighted `net_bps` HID this by not charging the real per-trade crossing — exactly the
trap the pre-registered per-trade bootstrap was there to catch.

## Honest caveats (kept for the record)
- The corrected OOS day-clustered IC t IS still high for some cells (w300/h30 t≈8.45 over 32 OOS days) — but
  IC significance ≠ tradeable. The per-trade P&L is the truth, and it is ~zero/negative.
- **OOS IC (0.105) > IS IC (0.082)** — a YELLOW FLAG (OOS-stronger-than-in-sample suggests regime luck /
  small-sample composition, not robustness), now moot given the per-trade kill.
- Panel: 8 megacaps, ~63 days (32 OOS). Adequate for the per-trade kill (1,234+ trades/cell).

## Disposition: KILL — NOT a feature, NOT a paper container

The first pass's "propose qimb_300_30 as a feature / HF paper container" is WITHDRAWN — it was based on the
artifact `net_bps`, not the honest per-trade P&L. qimb-directional at the short horizon is a real but
UN-TRADEABLE correlation: the edge per trade does not survive crossing the spread. (Recurrence of the cycle-1
pattern at HF speed: a real signal whose magnitude is below the friction you must pay.)

## Next (cycle 2 continues)

The cost-PAYING (taker) directional qimb bet is dead. The remaining HF-liquid probe is the cost-EARNING side:
**HF03 — spread-capture / liquidity-provision** (post at the touch, EARN the half-spread; use the qimb IC to
AVOID posting on the side about to get run over — manage adverse selection rather than take). That inverts the
adversary that killed HF01+HF02. Pre-register before running. Still menu #1, zero-dependency.
