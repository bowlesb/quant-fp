"""Render W3 method.md / results.md / verdict.md from raw_results_w3.json."""

import json
from pathlib import Path
from typing import Any

OUTPUT_DIR = Path(__file__).resolve().parent
RAW = OUTPUT_DIR / "raw_results_w3.json"
HORIZONS = ["1d", "3d", "5d", "10d", "20d", "40d", "60d"]
PRIMARY = "LIQUID tertile (PRIMARY)"


def fmt(x: Any, nd: int = 2) -> str:
    if x is None:
        return "n/a"
    if isinstance(x, bool):
        return "yes" if x else "no"
    return f"{x:.{nd}f}"


def headline_table(es: dict[str, Any], cohort: str) -> list[str]:
    rows = ["| H | full dm% | t_dm | canary p95% | OOS long net% | net CI | n | excl0>0 |",
            "|---|---|---|---|---|---|---|---|"]
    for h in HORIZONS:
        c = es[cohort]["horizons"][h]
        hd = c["headline_full_net"]
        sig = c["long_oos_net"]
        boot = sig["bootstrap"]
        rows.append(
            f"| {h} | {fmt(hd['alpha_demean_pct'])} | {fmt(hd['t_demean'])} | "
            f"{fmt(hd['canary_p95_pct'])} | {fmt(sig['mean_net_pct'])} | "
            f"[{fmt(boot['lo_pct'])}, {fmt(boot['hi_pct'])}] | {boot['n']} | "
            f"{fmt(boot['ci_excludes_zero_above'])} |"
        )
    return rows


def full_table(es: dict[str, Any], cohort: str) -> list[str]:
    rows = ["| H | full long net% | net CI | gross% | gross CI | n |",
            "|---|---|---|---|---|---|"]
    for h in HORIZONS:
        c = es[cohort]["horizons"][h]
        net = c["long_full_net"]
        g = c["long_full_gross"]
        nb = net["bootstrap"]
        gb = g["bootstrap"]
        rows.append(
            f"| {h} | {fmt(net['mean_net_pct'])} | [{fmt(nb['lo_pct'])}, {fmt(nb['hi_pct'])}] | "
            f"{fmt(g['mean_net_pct'])} | [{fmt(gb['lo_pct'])}, {fmt(gb['hi_pct'])}] | {nb['n']} |"
        )
    return rows


def main() -> None:
    data = json.loads(RAW.read_text())
    meta = data["meta"]
    es = data["event_sets"]

    # results.md
    out: list[str] = ["# W3 — Results: 13D activist drift on LIQUID targets\n"]
    out.append(f"Panel: {meta['n_symbols_panel']} symbols, {meta['n_dates']} dates "
               f"({meta['date_start']}..{meta['date_end']}). "
               f"Liquid round-trip cost {fmt(meta['cost_rt_bps'],1)} bps (1x), "
               f"{fmt(meta['cost_rt_2x_bps'],1)} bps (2x), measured liquid half-spread "
               f"{fmt(meta['liquid_half_spread_bps'],2)} bps.\n")
    out.append("## Event counts\n")
    out.append("| set | total | liquid |")
    out.append("|---|---|---|")
    out.append(f"| 13D initial | {meta['n_events_initial']} | {meta['n_events_initial_liquid']} |")
    out.append(f"| 13D/A amend | {meta['n_events_amend']} | {meta['n_events_amend_liquid']} |")
    out.append(f"| 13D all | {meta['n_events_all']} | {meta['n_events_all_liquid']} |")
    out.append(f"\nOOS split: TRAIN ..{meta['oos_start']} (excl) | OOS {meta['oos_start']}..{meta['date_end']}.\n")

    for set_label in ["13D_initial", "13D_amend", "13D_all"]:
        out.append(f"\n## {set_label} — LIQUID (PRIMARY) headline + OOS bootstrap\n")
        out += headline_table(es[set_label], PRIMARY)
        out.append(f"\n### {set_label} — LIQUID full-sample long round-trip (net + gross)\n")
        out += full_table(es[set_label], PRIMARY)
        out.append(f"\n### {set_label} — Full universe (context) headline + OOS\n")
        out += headline_table(es[set_label], "Full universe (context)")
        out.append(f"\n### {set_label} — Top-300 (sub-cut) headline + OOS\n")
        out += headline_table(es[set_label], "Top-300 (sub-cut)")
    (OUTPUT_DIR / "results.md").write_text("\n".join(out) + "\n")
    print("wrote results.md")


if __name__ == "__main__":
    main()
