"""Experimenter — the Modeller's always-on sandbox.

Continuously works a queue of experiments (experiments/queue.json) on the collected
panel, logging every run historically to experiments/results.jsonl and a human-readable
docs/EXPERIMENTS.md. Curious + unattached: run far more than we'd ship. Idempotent:
skips experiment ids already in results. Idles + re-checks the queue (drop in new
experiments any time; long-shots welcome).
"""
import json
import logging
import os
import time
from datetime import datetime, timezone

import psycopg

from quantlib.research import HORIZON_MIN, load_panel, run_experiment

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("experimenter")

SET_VERSION = os.environ.get("FEATURE_SET_VERSION", "v1.0.0")
CADENCE_MIN = int(os.environ.get("FEATURE_CADENCE_MIN", "30"))
QUEUE = os.environ.get("EXP_QUEUE", "/app/experiments/queue.json")
RESULTS = os.environ.get("EXP_RESULTS", "/app/experiments/results.jsonl")
LOG_MD = os.environ.get("EXP_LOG_MD", "/app/docs/EXPERIMENTS.md")
IDLE_SECONDS = int(os.environ.get("EXP_IDLE_SECONDS", "1800"))
NOMICRO_FEATURES = 13          # first 13 of 18 are the non-micro set (stable prefix)

DB_KWARGS = {
    "host": os.environ["DB_HOST"], "port": int(os.environ.get("DB_PORT", "5432")),
    "dbname": os.environ["DB_NAME"], "user": os.environ["DB_USER"],
    "password": os.environ["DB_PASSWORD"],
}


def done_ids() -> set[str]:
    if not os.path.exists(RESULTS):
        return set()
    ids = set()
    with open(RESULTS) as f:
        for line in f:
            line = line.strip()
            if line:
                ids.add(json.loads(line)["id"])
    return ids


def log_result(record: dict) -> None:
    os.makedirs(os.path.dirname(RESULTS), exist_ok=True)
    with open(RESULTS, "a") as f:
        f.write(json.dumps(record) + "\n")
    header_needed = not os.path.exists(LOG_MD)
    with open(LOG_MD, "a") as f:
        if header_needed:
            f.write("# Experiment Log\n\nAppend-only history of all experiments (the "
                    "Modeller's exploration). IC is vs the actual forward return; the "
                    "shuffle canary is the leakage arbiter. Thin panel -> exploration, "
                    "not edge.\n\n| run_at | id | horizon | label | feats | rows | "
                    "mean_IC | NW_t | canary | hypothesis |\n|---|---|---|---|---|---|"
                    "---|---|---|---|\n")
        r = record
        res = r.get("result", {})
        f.write(f"| {r['run_at']} | {r['id']} | {r['horizon']} | {r['label']} | "
                f"{res.get('n_features','')} | {res.get('n_rows','')} | "
                f"{res.get('mean_ic','')} | {res.get('nw_t','')} | "
                f"{res.get('canary_ic','')} | {r['hypothesis']} |\n")


def run_queue() -> int:
    with open(QUEUE) as f:
        experiments = json.load(f)
    finished = done_ids()
    ran = 0
    for exp in experiments:
        if exp["id"] in finished:
            continue
        horizon = exp.get("horizon", "fwd_30m")
        # named subsets of the 18-feature v1.0.0 vector (indices 0..17):
        #   nomicro   = drop micro (keep 0..12)
        #   nocalendar= drop micro + minute_of_day(11) + day_of_week(12) -> keep 0..10
        feature_idx = {
            "nomicro": list(range(13)),
            "nocalendar": list(range(11)),
        }.get(exp.get("features"))
        logger.info("running experiment %s (%s, %s, %s)", exp["id"], horizon, exp.get("label"), exp.get("features"))
        try:
            with psycopg.connect(**DB_KWARGS) as conn:
                names, ts, X, y = load_panel(conn, horizon, SET_VERSION)
            if len(y) < 1000:
                result = {"error": "panel too small", "n_rows": int(len(y))}
            else:
                result = run_experiment(
                    X, y, ts, label=exp.get("label", "raw"), feature_idx=feature_idx,
                    horizon_minutes=HORIZON_MIN.get(horizon, 30), cadence_min=CADENCE_MIN,
                )
                imp = result.get("gain_importance")
                if imp:                         # used features are a prefix of names
                    used = names[:len(imp)]
                    top = sorted(zip(used, imp), key=lambda kv: -kv[1])[:5]
                    result["top_features"] = [f"{n}:{v}" for n, v in top]
        except (psycopg.Error, ValueError, KeyError) as exc:
            result = {"error": f"{type(exc).__name__}: {exc}"}
        record = {
            "run_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "id": exp["id"], "horizon": horizon, "label": exp.get("label", "raw"),
            "features": exp.get("features", "all"), "hypothesis": exp["hypothesis"],
            "set_version": SET_VERSION, "result": result,
        }
        log_result(record)
        logger.info("logged %s: %s", exp["id"], result)
        ran += 1
    return ran


def main() -> None:
    logger.info("experimenter starting: queue=%s set=%s", QUEUE, SET_VERSION)
    while True:
        try:
            ran = run_queue()
        except (OSError, json.JSONDecodeError) as exc:
            logger.error("queue error: %s", exc)
            ran = 0
        if ran == 0:
            time.sleep(IDLE_SECONDS)


if __name__ == "__main__":
    main()
