# Option A re-grade — VERDICT: NULL under accurate per-name cost (model doesn't trade it yet; features retained)

**Date:** 2026-06-20  **Test:** re-grade quote-alpha (G0a quote-dynamics) on G0a's ORIGINAL full liquid-200
universe, changing ONLY the cost term (flat 3.0 bps stub → Stage-1 measured per-name realized half-spread).
Walk-forward GBM, 5 folds, panel 4,607 rows / 49 days, realized cost measured for 4,604/4,607 rows (median
8.78 bps). Discovery 2026-04-15..05-14 (23 days), Replication 2026-05-15..06-12 (26 days).

## Result: NULL on BOTH windows — fails the full robustness gate
| leg | DISCOVERY | REPLICATION |
|---|---|---|
| Δ net-$ up at ALL {2,5,10}% | ✗ (2%/5% −$90k, 10% +$7k) | ✗ (2% +$41k, 5% −$25k, 10% −$32k) |
| AUC + rankIC up vs baseline | ✗ (.594→.584, +.142→+.126) | ✗ (.529→.530, +.054→+.051) |
| per-day NW\|t\| (5% cut) ≥ 2 | ✗ (−0.52, n=4) | ✗ (+1.64, n=13, max-day-share 17%) |
| beats shuffle | ✓ | ✓ |
| **PASS ALL LEGS** | **✗** | **✗** |

Neither window passes; no disjoint replication. The replication 2%-cut looks tempting (precision 0.641, +$40k
Δ, 78 trades) but it does NOT replicate to discovery, fails every broader cut, and its per-day t (+1.64) is
not significant — the exact single-cut-blip the gate exists to reject.

## Why (the structural prediction, confirmed)
The power check predicted this: only 22% of liquid-200 names are cheaper than the old 3bps stub; 78% are
DEARER. Here the measured median cost was 8.78 bps (vs 3.0 flat), so the broad universe got DEARER, not
cheaper — and the quote-dynamics signal still isn't tradeable. The "null streak as cost-measurement artifact"
hypothesis is answered with a measured number: NO. Accurate cost makes the broad liquid head net-WORSE
(consistent with Stage 1's −22% headline haircut), and quote-dynamics add no tradeable model edge on top of a
baseline that already holds the static quote snapshot.

## Disposition (Ben's principle — this is a what-to-TRADE verdict, NOT a feature verdict)
A null here means **the current model, on the current data, does not TRADE the quote-dynamics signal under
accurate cost YET** — NOT that the feature is worthless. The quote-dynamics features (and swing_dc, and
path-geometry) remain fully INCLUDABLE and RETAINED in the store/bus: feature inclusion is liberal and
DECOUPLED from $-value, because future data + feature-interactions may make a model use them. The $-test is a
model/strategy question (what to trade on), never a feature-inclusion gate.

## Routing (pre-committed)
This is the pre-committed null branch → proceed to **#2: the #205 weekly-reversal hunt** — the turnover/
horizon attack on the actual cause of death (net-of-cost at our turnover). A weekly hold amortizes one cost
over ~5 days, so a per-period-weaker signal can be net-positive; #205's weekly reversal was real-gross in
smoke (IC +0.075, +69 bps net @5 bps). Carry the survivorship-haircut discipline (the deep multi-day panel is
0/400 delisted-in-store). I'll pre-register #2 next for the Lead's gate-read.

## Decisiveness
Option A converted the structural argument (power check: cheap names too thin, broad universe under-charged)
into a measured, replication-tested number. The answer is clean and decisive: accurate cost does not revive a
TRADEABLE quote-alpha signal. Either outcome was a win; this one routes us to the more promising un-nulled
direction (#2) with a confirmed, not assumed, prior.
