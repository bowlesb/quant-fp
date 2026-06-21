"""Auto-deploy QUEUE — a serialized, file-backed deploy queue with a batching window.

docs/AUTO_DEPLOY.md. The queue decouples "a merge happened, these services are stale" (the producer, written
by the auto-deploy daemon as it observes new main SHAs) from "apply the deploys, one at a time" (the
consumer, the single applier). File-backed (JSON Lines under ``~/.quant-ops/deploy_queue/``) so it survives a
daemon restart and is inspectable; an exclusive ``filelock`` serializes every mutation so no two appliers race.

A queue ENTRY records: the service, its tier, the merge SHA that made it stale, the changed-path count, and
when it was enqueued. The applier drains the queue in a BATCH: it coalesces all pending entries for the same
service to a SINGLE redeploy at the newest SHA (no point rebuilding dashboard 5x for 5 merges), and it groups
TIER-2/fc entries separately so they wait for the coordinated relaunch rather than deploying mid-session.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import time
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

QUEUE_DIR = os.path.expanduser(os.environ.get("CI_DEPLOY_QUEUE_DIR", "~/.quant-ops/deploy_queue"))
QUEUE_FILE = os.path.join(QUEUE_DIR, "pending.jsonl")
LOCK_FILE = os.path.join(QUEUE_DIR, ".lock")
# Coalesce merges landing within this window into one deploy batch (so a burst of merges = one rebuild/svc).
BATCH_WINDOW_S = int(os.environ.get("CI_DEPLOY_BATCH_WINDOW_S", "120"))


@dataclass
class DeployEntry:
    """One enqueued deploy: a service made stale by a merge."""

    service: str
    tier: str  # DeployTier value ("tier-1-auto" / "tier-2-coordinated")
    sha: str  # the main SHA that made it stale (newest wins on coalesce)
    n_paths: int
    enqueued_at: str  # ISO-8601 UTC

    @classmethod
    def new(cls, service: str, tier: str, sha: str, n_paths: int) -> DeployEntry:
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return cls(service=service, tier=tier, sha=sha, n_paths=n_paths, enqueued_at=stamp)


def _ensure_dir() -> None:
    os.makedirs(QUEUE_DIR, exist_ok=True)


@contextlib.contextmanager
def _lock() -> Iterator[None]:
    """Exclusive advisory lock on the queue dir (stdlib fcntl.flock — no third-party dep). Serializes every
    read-modify-write so two appliers can't race. Blocks until acquired (the critical sections are tiny)."""
    _ensure_dir()
    fd = os.open(LOCK_FILE, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _read_all_locked() -> list[DeployEntry]:
    if not os.path.isfile(QUEUE_FILE):
        return []
    entries: list[DeployEntry] = []
    with open(QUEUE_FILE) as handle:
        for line in handle:
            line = line.strip()
            if line:
                entries.append(DeployEntry(**json.loads(line)))
    return entries


def _write_all_locked(entries: list[DeployEntry]) -> None:
    tmp = QUEUE_FILE + ".tmp"
    with open(tmp, "w") as handle:
        for entry in entries:
            handle.write(json.dumps(asdict(entry)) + "\n")
    os.replace(tmp, QUEUE_FILE)  # atomic


def enqueue(entries: list[DeployEntry]) -> None:
    """Append deploy entries (one per affected service). Idempotent on (service, sha): re-enqueuing the same
    service+SHA does not duplicate it (so a daemon re-observing the same merge is harmless)."""
    if not entries:
        return
    with _lock():
        existing = _read_all_locked()
        seen = {(entry.service, entry.sha) for entry in existing}
        added = [entry for entry in entries if (entry.service, entry.sha) not in seen]
        if added:
            _write_all_locked(existing + added)


def peek() -> list[DeployEntry]:
    """All pending entries (read-only, no mutation)."""
    with _lock():
        return _read_all_locked()


def claim_batch() -> tuple[list[DeployEntry], list[DeployEntry]]:
    """Atomically drain the queue into (auto_batch, coordinated_batch), COALESCED per service to the newest SHA.

    Returns the TIER-1 services to deploy now (one entry per service, newest SHA) and the TIER-2/coordinated
    entries to defer to the relaunch. The queue is CLEARED of the auto entries (claimed); the coordinated
    entries are KEPT (they wait for the relaunch, which drains them separately via ``drain_coordinated``). Only
    entries older than ``BATCH_WINDOW_S`` are claimed for auto-deploy, so a still-arriving burst coalesces.
    """
    now = time.time()
    with _lock():
        entries = _read_all_locked()
        ripe: list[DeployEntry] = []
        unripe: list[DeployEntry] = []
        for entry in entries:
            age = now - _parse_ts(entry.enqueued_at)
            (ripe if age >= BATCH_WINDOW_S else unripe).append(entry)

        auto_ripe = [entry for entry in ripe if entry.tier == "tier-1-auto"]
        coordinated = [entry for entry in entries if entry.tier == "tier-2-coordinated"]

        # Coalesce auto entries per service → newest SHA (one redeploy per service for the whole batch).
        auto_batch = _coalesce_newest(auto_ripe)

        # Keep: everything not claimed for auto = the unripe auto entries + ALL coordinated (await relaunch).
        keep = unripe + coordinated
        # de-dup keep on (service, sha)
        seen: set[tuple[str, str]] = set()
        deduped: list[DeployEntry] = []
        for entry in keep:
            key = (entry.service, entry.sha)
            if key not in seen:
                seen.add(key)
                deduped.append(entry)
        _write_all_locked(deduped)
        return auto_batch, _coalesce_newest(coordinated)


def drain_coordinated() -> list[DeployEntry]:
    """Atomically remove + return the coordinated (TIER-2/fc) entries, coalesced per service. Called by the
    relaunch path AFTER it has FF'd + relaunched fc, to clear the batched fc/fp deploys it just satisfied."""
    with _lock():
        entries = _read_all_locked()
        coordinated = [entry for entry in entries if entry.tier == "tier-2-coordinated"]
        remaining = [entry for entry in entries if entry.tier != "tier-2-coordinated"]
        _write_all_locked(remaining)
        return _coalesce_newest(coordinated)


def _coalesce_newest(entries: list[DeployEntry]) -> list[DeployEntry]:
    """One entry per service, keeping the newest-enqueued (→ newest SHA). Order: stable by service name."""
    by_service: dict[str, DeployEntry] = {}
    for entry in entries:
        prev = by_service.get(entry.service)
        if prev is None or _parse_ts(entry.enqueued_at) >= _parse_ts(prev.enqueued_at):
            by_service[entry.service] = entry
    return [by_service[name] for name in sorted(by_service)]


def _parse_ts(stamp: str) -> float:
    return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
