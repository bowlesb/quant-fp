# R7 — Cross-sectional multi-day RETURN RANK (ST-reversal / XS-momentum factor input), PRE-REGISTERED

## Idea / non-redundancy (vs the 654-feature library)
multi_day has daily_return_{w}d (the per-symbol return LEVEL) and cross_sectional_rank has
return_rank_{w}m (INTRADAY minute-return ranks). NEITHER is the cross-sectional rank of the MULTI-DAY
return — the direct input to the documented short-term-reversal (ST-reversal: biggest recent losers
bounce) and cross-sectional-momentum premia. A tree CANNOT form a cross-sectional rank from per-symbol
levels (rank needs the whole cross-section), so an explicit multi-day-return XS rank is non-redundant —
exactly the argument that justified liquidity_rank despite adv_dollar levels existing.

## Pre-registered study (daily bars via /store, all 378d, liquid tier)
For w in {1, 5, 20} trading days: daily_return_w = close_t/close_{t-w} - 1; xs_rank_w = cross-sectional
percentile [0,1] of daily_return_w within the day's universe. FEATURE study (characterize + a light
predictive sanity), pre-committed:
1. By construction xs_rank is uniform [0,1] (a rank) -> well-spread (trivially). The real questions:
2. PERSISTENCE / TURNOVER: is xs_rank_w autocorrelated day-to-day (a real slow factor, not daily noise)?
   rank-autocorr lag-1 day. (ST-reversal needs SOME persistence to be a usable conditioner.)
3. PREDICTIVE SANITY (tradeable entry, per the tradeable-entry rule): does xs_rank_w at day d predict
   the next-day return (d+1 open->close, the tradeable window)? rank-IC of xs_rank_w vs fwd_1d_ret,
   per w. ST-reversal => NEGATIVE IC at short w (losers bounce); XS-momentum => positive at longer w.
   This is a SANITY check that the rank carries cross-sectional information, NOT a strategy claim.

## Falsification / feature decision
- xs_rank is trivially well-spread (it's a rank); the bar is NON-REDUNDANT + NOT-NOISE. If the rank has
  zero day-to-day persistence AND zero forward-IC at every w (pure daily noise), it adds nothing a tree
  can't get -> reconsider. If it shows persistence OR a coherent forward-IC sign structure (reversal at
  short w / momentum at long w), SHIP xs_return_rank (the factor input the model conditions on).
- Honest either way.

## Parity note
xs_rank_w = cross-sectional rank of a deterministic multi-day return, universe-pinned (same pin
liquidity_rank/return_dispersion use) -> parity-true by construction. Daily-broadcast, STATIC windowed
(no FeatureState). NULL during warmup / when the w-day return is undefined. Degenerate-guard baked in
(rank of nulls -> null), the vol_term_structure/DataIntegrity-4 discipline from the start.

## Output
xs_return_rank FEATURE candidate (batch-1f) + the persistence/IC characterization. KILL/KEEP honest.
