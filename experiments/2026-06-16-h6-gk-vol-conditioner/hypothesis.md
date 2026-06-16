# H6 — Garman-Klass efficient volatility as a vwap_dev reversion CONDITIONER — pre-registration

**Registered:** 2026-06-16 (before any data run). ON-DECK after H3. Uses BARS only (full 126-day depth in
`/store/raw/bars`) — no quotes/trades/corporate-action data needed, so it is unblocked and cheap.

## Hypothesis

Same conditioner framing that H3 applies to book state, but with an OHLC-efficient VOLATILITY regime instead
of spread/depth. Garman-Klass vol (5–8× more efficient than close-to-close, uses the full OHLC bar) per
(symbol, minute) over a trailing window. Hypothesis: the `vwap_dev` reversion (our only canary-clearing
signal, t −2.76) is CLEANER and CHEAPER in LOW-volatility names/minutes — so a low-GK-vol tercile lifts the
reversion's net-of-cost gross above its cost, where the flat signal does not.

GK per bar: `0.5*(ln(H/L))^2 − (2 ln 2 − 1)*(ln(C/O))^2`; rolling-mean over `w ∈ {15, 30}` min, sqrt for a
vol level. Tercile the cross-section by GK-vol each minute (low/mid/high).

## Test design

Decile L/S on `vwap_dev_15` within each GK-vol tercile, net-of-MEASURED-cost (the cost anchor is the
liquid-tier spread; lower-vol names also tend to be tighter, so the low-vol tercile gets BOTH a cleaner
signal AND a lower cost — the double benefit is the thesis). H15 + H30, 10-seed within-CS shuffle canary,
day-clustered t over the available days. Baseline = flat vwap_dev (no conditioning). Reuse the bars panel;
add the GK column.

## Prior

GK/RS estimators are the standard efficient intraday vol measures (Garman-Klass 1980). Reversion is
empirically stronger and spreads tighter in calm regimes; conditioning on an EFFICIENT vol estimate sharpens
the regime cut without adding turnover (the vol tercile is slow-moving). Attacks the cost wall by selection,
not by gating an illiquid-concentrated signal (the H1 trap).

## Expected / confidence

- Confidence the low-GK-vol tercile lifts vwap_dev breakeven above its measured cost: ~25%. Honest risk:
  same wall as H1/H2/H3 — the liquid-tier vwap_dev is −0.017/−0.014, possibly too weak for ANY conditioner
  to rescue net-of-cost.
- KEEP needs the low-vol tercile net-of-cost gross > its measured round-trip spread AND clearing canary at
  H15 or H30.

## Kill

KILL if NO GK-vol tercile lifts vwap_dev net-of-cost above its own measured cost beyond canary — then
efficient-vol conditioning adds nothing and H6 is closed.

## Ordering

Dispatch AFTER H3 (one heavy job at a time). If H3 and H6 both kill, the standing position hardens: the
liquid-tradeable vwap_dev reversion cannot be rescued by any microstructure/vol conditioner — and the next
backlog turn moves to the low-turnover EVENT families (H4 splits / H5 dividends), which attack the cost wall
by holding-horizon rather than by conditioning, pending corporate-action data.
