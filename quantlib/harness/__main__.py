"""The harness CLI — "just run it".

    python -m quantlib.harness \
        --daily-cache experiments/data/battery_daily_cache.parquet \
        --model gbm --label-horizon-days 1 --frac 0.10 --capital 1000000 \
        --universe-top 500 --out /tmp/harness_demo

Writes the markdown report, the equity-curve CSV, and the threshold-curve CSV to `--out` and prints the
summary to stdout.
"""
from __future__ import annotations

import argparse
import csv
import dataclasses
import json
import os

from quantlib.harness.config import Cadence, HarnessConfig, ModelKind
from quantlib.harness.report import StrategyReport
from quantlib.harness.run import run_strategy


def _build_config(args: argparse.Namespace) -> HarnessConfig:
    return HarnessConfig(
        cadence=Cadence(args.cadence),
        daily_cache=args.daily_cache,
        date_start=args.date_start,
        date_end=args.date_end,
        universe_top=args.universe_top,
        intraday_groups=json.loads(args.intraday_groups) if args.intraday_groups else None,
        label_horizon_days=args.label_horizon_days,
        label_horizon_min=args.label_horizon_min,
        model=ModelKind(args.model),
        n_folds=args.n_folds,
        long_short_frac=args.frac,
        capital=args.capital,
        cost_mult=args.cost_mult,
        slippage_bps=args.slippage_bps,
        borrow_bps_annual=args.borrow_bps_annual,
        seed=args.seed,
    )


def _write_outputs(report: StrategyReport, out_dir: str) -> None:
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "report.md"), "w") as handle:
        handle.write(report.summary_md)
    with open(os.path.join(out_dir, "equity_curve.csv"), "w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["period", "timestamp", "net_return", "equity"])
        for point in report.money.equity_curve:
            writer.writerow([point.period, point.timestamp, point.net_return, point.equity])
    with open(os.path.join(out_dir, "threshold_curve.csv"), "w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "frac",
                "n_trades",
                "directional_precision",
                "mean_fwd_return",
                "dollar_per_trade",
                "total_dollar_pnl",
                "net_per_period",
                "sharpe_net",
                "breakeven_cost_bps",
            ]
        )
        for cut in report.threshold_curve.cuts:
            writer.writerow(
                [
                    cut.frac,
                    cut.n_trades,
                    cut.directional_precision,
                    cut.mean_fwd_return,
                    cut.dollar_per_trade,
                    cut.total_dollar_pnl,
                    cut.net_per_period,
                    cut.sharpe_net,
                    cut.breakeven_cost_bps,
                ]
            )
    with open(os.path.join(out_dir, "report.json"), "w") as handle:
        json.dump(dataclasses.asdict(report), handle, indent=2, default=str)


def main() -> None:
    parser = argparse.ArgumentParser(description="Train -> apply -> evaluate strategy harness")
    parser.add_argument("--cadence", default="daily", choices=[c.value for c in Cadence])
    parser.add_argument("--daily-cache", default="experiments/data/battery_daily_cache.parquet")
    parser.add_argument("--date-start", default="2025-12-01")
    parser.add_argument("--date-end", default="2026-06-17")
    parser.add_argument("--universe-top", type=int, default=500)
    parser.add_argument("--intraday-groups", default=None, help="JSON {group: [features]} for intraday")
    parser.add_argument("--label-horizon-days", type=int, default=1)
    parser.add_argument("--label-horizon-min", type=int, default=30)
    parser.add_argument("--model", default="gbm", choices=[m.value for m in ModelKind])
    parser.add_argument("--n-folds", type=int, default=5)
    parser.add_argument("--frac", type=float, default=0.10)
    parser.add_argument("--capital", type=float, default=1_000_000.0)
    parser.add_argument("--cost-mult", type=float, default=1.0)
    parser.add_argument("--slippage-bps", type=float, default=1.0)
    parser.add_argument("--borrow-bps-annual", type=float, default=50.0)
    parser.add_argument("--seed", type=int, default=13)
    parser.add_argument(
        "--out", default=None, help="write report.md/equity_curve.csv/threshold_curve.csv here"
    )
    args = parser.parse_args()

    report = run_strategy(_build_config(args))
    print(report.summary_md)
    if args.out:
        _write_outputs(report, args.out)
        print(f"\n[wrote report.md / equity_curve.csv / threshold_curve.csv / report.json -> {args.out}]")


if __name__ == "__main__":
    main()
