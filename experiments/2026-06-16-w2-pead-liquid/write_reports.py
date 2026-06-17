"""Generate results.md and verdict.md from raw_results_w2.json (run after run_w2.py)."""

import json
from pathlib import Path
from typing import Any

OUTPUT_DIR = Path("/app/experiments/2026-06-16-w2-pead-liquid")
HORIZONS = ["1d", "3d", "5d", "10d", "20d", "40d"]


def fmt(value: Any, nd: int = 3) -> str:
    if value is None:
        return "  n/a"
    return f"{value:+.{nd}f}"


def signed_row(horizons: dict[str, Any], leg: str) -> str:
    cells = []
    for h in HORIZONS:
        sig = horizons[h][leg]
        boot = sig["bootstrap"]
        net = fmt(sig["mean_net_pct"])
        lo = fmt(boot["lo_pct"]) if boot["lo_pct"] is not None else "n/a"
        hi = fmt(boot["hi_pct"]) if boot["hi_pct"] is not None else "n/a"
        cells.append(f"{net}% [{lo},{hi}] n={boot['n']}")
    return " | ".join(cells)


def headline_row(horizons: dict[str, Any], leg: str) -> str:
    cells = []
    for h in HORIZONS:
        hd = horizons[h][leg]
        cells.append(f"dm={fmt(hd['alpha_demean_pct'])}% t={hd['t_demean']} n={hd['n_event_obs']}")
    return " | ".join(cells)


def main() -> None:
    with open(OUTPUT_DIR / "raw_results_w2.json") as json_file:
        res = json.load(json_file)
    meta = res["meta"]
    cohorts = res["cohorts"]
    liquid = cohorts["LIQUID tertile (PRIMARY)"]["horizons"]

    lines: list[str] = []
    lines.append("# W2 — Results: item-2.02 PEAD on LIQUID names\n")
    lines.append("## Meta")
    lines.append(f"- Panel: {meta['n_symbols_panel']} symbols, {meta['n_dates']} trading days "
                 f"({meta['date_start']} .. {meta['date_end']}).")
    lines.append(f"- Earnings (item-2.02) events with bars: **{meta['n_earnings_events_total']}**; "
                 f"in the **LIQUID tertile: {meta['n_earnings_events_liquid']}**.")
    lines.append(f"- Liquid tier: {meta['n_liquid_symbols']} symbols; top-100 megacap cut separately.")
    lines.append(f"- Measured LIQUID half-spread: {meta['liquid_half_spread_bps']:.2f} bps "
                 f"-> round-trip cost {meta['cost_rt_bps']:.1f} bps (1x), {meta['cost_rt_2x_bps']:.1f} bps (2x stress).")
    lines.append(f"- Walk-forward split: OOS starts {meta['oos_start']} (last half of the window).")
    lines.append(f"- Entry = D+1 open after available_at. Horizons (trading days): {meta['horizons']}.\n")

    lines.append("## HEADLINE — LIQUID tertile PEAD drift (cohort minus same-date control, per-symbol demeaned, day-clustered)")
    lines.append("Open-entry forward return, net of measured liquid round-trip cost. Per horizon "
                 + " / ".join(HORIZONS) + ":\n")
    lines.append("- FULL window (net): " + headline_row(liquid, "headline_full_net"))
    lines.append("- TRAIN (net):      " + headline_row(liquid, "headline_train_net"))
    lines.append("- **OOS (net):**     " + headline_row(liquid, "headline_oos_net"))
    canary = [liquid[h]["headline_oos_net"]["canary_p95_pct"] for h in HORIZONS]
    lines.append(f"- Shuffle-canary OOS p95 (pct): {[fmt(c) for c in canary]}\n")

    lines.append("## SIGNED L/S (the tradeable bet) — sign by D+1 reaction, drift measured from D+1 close, per-trade bootstrap CI (10k)")
    lines.append("net% [boot CI lo, hi] n_trades, per horizon " + " / ".join(HORIZONS) + ":\n")
    lines.append("- **LIQUID OOS net (1x cost):** " + signed_row(liquid, "signed_oos_net"))
    lines.append("- LIQUID OOS net (2x cost):     " + signed_row(liquid, "signed_oos_net_2x"))
    lines.append("- LIQUID OOS gross:             " + signed_row(liquid, "signed_oos_gross"))
    lines.append("- LIQUID FULL net (1x cost):    " + signed_row(liquid, "signed_full_net") + "\n")

    lines.append("## Context cohorts (signed L/S OOS net, 1x cost)")
    for label in ["Top-100 megacap", "Full universe (context)", "Mid tertile (context)", "Illiquid tertile (context)"]:
        if label in cohorts:
            h = cohorts[label]["horizons"]
            lines.append(f"- {label}: " + signed_row(h, "signed_oos_net"))
    lines.append("")

    (OUTPUT_DIR / "results.md").write_text("\n".join(lines))
    print("Wrote results.md")

    # Verdict — decision on LIQUID OOS signed net-of-cost bootstrap CI > 0
    verdict_lines: list[str] = ["# W2 — Verdict\n"]
    decisive_horizons = []
    for h in HORIZONS:
        boot = liquid[h]["signed_oos_net"]["bootstrap"]
        if boot.get("ci_excludes_zero_above"):
            decisive_horizons.append(h)
    any_decisive = len(decisive_horizons) > 0

    if any_decisive:
        decision = "KEEP-AS-LEAD" if len(decisive_horizons) >= 2 else "AMBIGUOUS"
    else:
        decision = "KILL"

    verdict_lines.append(f"## DECISION: {decision}\n")
    verdict_lines.append("Decisive criterion (pre-registered): LIQUID OOS signed L/S net-of-cost per-trade "
                         "bootstrap 95% CI lower bound > 0.\n")
    verdict_lines.append(f"- Horizons where LIQUID OOS net-of-cost bootstrap CI excludes zero (lo>0): "
                         f"**{decisive_horizons if decisive_horizons else 'NONE'}**")
    for h in HORIZONS:
        sig = liquid[h]["signed_oos_net"]
        boot = sig["bootstrap"]
        verdict_lines.append(f"  - H={h}: net={fmt(sig['mean_net_pct'])}% "
                             f"CI=[{fmt(boot['lo_pct']) if boot['lo_pct'] is not None else 'n/a'}, "
                             f"{fmt(boot['hi_pct']) if boot['hi_pct'] is not None else 'n/a'}] "
                             f"n={boot['n']} | day-clustered t={sig['day_clustered_t']}")
    verdict_lines.append("")
    verdict_lines.append("## Caveats (pre-flagged)")
    verdict_lines.append("- **Reaction-sign proxy, not a true SUE.** Without a consensus-estimate feed the cohort "
                         "is signed by the D+1 reaction, which is noisier than a genuine earnings surprise and "
                         "can conflate reaction with drift. A clean SUE = an estimates-feed DATA ASK.")
    verdict_lines.append("- **PEAD is heavily arbitraged in large-caps** and documented to survive mostly in "
                         "small-caps — a liquid-dead result is the *expected* null (the H10b illiquid trap).")
    verdict_lines.append("- Short window (~126 trading days since 2025-12-15); ~63-day OOS. One quarter of earnings.")
    verdict_lines.append("")
    (OUTPUT_DIR / "verdict.md").write_text("\n".join(verdict_lines))
    print(f"Wrote verdict.md — DECISION: {decision}")


if __name__ == "__main__":
    main()
