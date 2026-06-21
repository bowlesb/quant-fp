"""The battery CLI — the SINGLE script.

    python -m quantlib.battery.battery_cli --config quantlib/battery/configs/demo.py --out /tmp/battery

`--config` points at a python file exposing a module-level `CONFIG: BatteryConfig` (the declarative
"set of strategies to run together"). Adding a strategy = adding a `StrategyConfig` to that file's list.
Writes report.md + report.json to `--out` and prints the summary.
"""
from __future__ import annotations

import argparse
import dataclasses
import importlib.util
import json
import os

from quantlib.battery.battery_config import BatteryConfig
from quantlib.battery.battery_report import BatteryReport
from quantlib.battery.battery_run import run_battery


def load_config(path: str) -> BatteryConfig:
    """Load `CONFIG: BatteryConfig` from a python config file by path."""
    spec = importlib.util.spec_from_file_location("battery_config_module", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load config module from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    config = getattr(module, "CONFIG", None)
    if not isinstance(config, BatteryConfig):
        raise TypeError(f"{path} must expose a module-level CONFIG: BatteryConfig")
    return config


def write_outputs(report: BatteryReport, out_dir: str) -> None:
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "report.md"), "w") as handle:
        handle.write(report.summary_md)
    with open(os.path.join(out_dir, "report.json"), "w") as handle:
        json.dump(dataclasses.asdict(report), handle, indent=2, default=str)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a strategy battery over a shared feature matrix")
    parser.add_argument("--config", required=True, help="python file exposing CONFIG: BatteryConfig")
    parser.add_argument("--out", default=None, help="write report.md / report.json here")
    args = parser.parse_args()

    config = load_config(args.config)
    report = run_battery(config)
    print(report.summary_md)
    if args.out:
        write_outputs(report, args.out)
        print(f"\n[wrote report.md / report.json -> {args.out}]")


if __name__ == "__main__":
    main()
