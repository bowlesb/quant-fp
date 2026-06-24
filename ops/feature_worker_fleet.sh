#!/usr/bin/env bash
# Respawn guard for the FEATURE-WORKER FLEET (docs/FEATURE_WORKER_FLEET.md §5).
#
# Ben wants >=5 agents continuously monitoring different feature groups, each picking the highest-priority
# unclaimed group off the lifecycle queue and advancing it one phase. This box has no systemd-user, so the
# pool is kept alive by a CHEAP cron that runs this guard every few minutes — the SAME pattern as
# ops/ci_daemon_guard.sh. The guard is idempotent: it counts the live feature-worker CONTAINERS and launches
# only the DEFICIT to reach the target, each a detached (-d) --rm fp-dev container so it survives this guard
# exiting and frees its slot on exit. Workers run IN docker (not on the host) because the python worker can
# only reach the DB on the quant_default network (DB_HOST=timescaledb) and read /store there.
#
#   ops/feature_worker_fleet.sh                 # ensure >=N workers alive (N=FLEET_SIZE, default 5)
#   ops/feature_worker_fleet.sh --status        # report how many workers are alive, change nothing
#   ops/feature_worker_fleet.sh --stop          # stop every feature-worker in the fleet
#   FLEET_SIZE=8 ops/feature_worker_fleet.sh    # a larger pool
#
# SAFETY (the boundary — see docs/FEATURE_WORKER_FLEET.md §6):
#   * DRY-RUN BY DEFAULT. Each worker runs WITHOUT --write-lock/--write-cert, so it reads the queue and logs
#     the intended claim/advance but writes NOTHING to the DB. ARMING the fleet to take the real assignment
#     lock + write certs is Ben's/the Lead's click: set FLEET_WRITE=1 (passes --write-lock --write-cert).
#   * The worker NEVER edits the live tree, restarts fc, applies a hot-swap, or enqueues a deploy. A fix for a
#     DIVERGENT group is triaged (read-only) and handed to a fixing agent → worktree→PR→Lead; the live
#     hot-swap stays behind FP_WDPC_LIVE_SWAP (Lead-gated). This guard only (re)launches the python worker.
#   * Each worker runs --once (advance ONE group, exit): the cron is the loop, so a hung monitor can't wedge a
#     slot — the slot frees on exit and the next tick refills it. Set FLEET_LOOP=1 for long-lived workers.
set -uo pipefail

# A DEDICATED, always-current checkout the workers run from (fetch + reset --hard origin/main each cycle,
# self-healing), DECOUPLED from the pinned fc bind-mount tree — exactly like ci_daemon_guard's CI_TREE. The
# fleet is read-mostly orchestration; it must never depend on (or mutate) the live fc tree.
LIVE_TREE="${FLEET_LIVE_TREE:-/home/ben/quant-fp}"
FLEET_TREE="${FLEET_TREE:-/home/ben/.fleet-repo}"
LOG_DIR="${HOME}/.quant-ops"
mkdir -p "$LOG_DIR"

FLEET_SIZE="${FLEET_SIZE:-5}"            # target number of live workers (Ben's ">=5")
FLEET_WRITE="${FLEET_WRITE:-0}"          # 0 = dry-run (default, safe); 1 = arm real lock + cert writes
FLEET_LOOP="${FLEET_LOOP:-0}"            # 0 = --once per worker (cron is the loop); 1 = long-lived loop
LOG="$LOG_DIR/feature_worker.log"

# Each worker runs IN an fp-dev container on the quant_default docker network — the ONLY place the python
# worker can reach the DB (DB_HOST=timescaledb resolves only on quant_default) AND read the /store feature
# root the clean-lifecycle monitor needs. Running on the bare host fails twice: it can't resolve timescaledb
# and has no /store. The container mirrors how the live feature-computer runs (same image, network, env-file,
# /store volume, code bind-mounted at /app from the always-current FLEET_TREE), but is fully decoupled — a
# throwaway --rm container per --once worker, named fworker-* so the fleet can count/stop its own and the
# dashboard active-owners panel ties each lock to a worker.
FLEET_IMAGE="${FLEET_IMAGE:-fp-dev:latest}"
FLEET_NETWORK="${FLEET_NETWORK:-quant_default}"
FLEET_STORE_VOLUME="${FLEET_STORE_VOLUME:-fp_store_real}"
FLEET_ENV_FILE="${FLEET_ENV_FILE:-${LIVE_TREE}/.env}"   # DB_PASSWORD etc. — never on the host shell, never logged
FLEET_NAME_PREFIX="fworker"                              # docker --name prefix; the stable count/stop match

fleet_log() { printf '%s feature-worker-fleet %s\n' "$(date -u +%FT%TZ)" "$*" | tee -a "$LOG_DIR/feature_worker_fleet.log" >&2; }

# Provision-or-refresh the dedicated worker checkout to origin/main; echo the tree to run from. Falls back to
# LIVE_TREE on any failure (degrade, don't stall the pool). Mirrors ci_daemon_guard.ensure_ci_repo.
ensure_fleet_repo() {
  local origin
  origin="$(git -C "$LIVE_TREE" remote get-url origin 2>/dev/null || echo https://github.com/bowlesb/quant-fp.git)"
  # A plain clone has a .git DIRECTORY; a git WORKTREE has a .git FILE. Only wipe-and-reclone a path that is
  # NEITHER (a stray non-repo dir) — never rm -rf a checkout someone pointed FLEET_TREE at by mistake.
  if [ ! -e "$FLEET_TREE/.git" ]; then
    [ -e "$FLEET_TREE" ] && { fleet_log "FLEET_TREE $FLEET_TREE exists but is not a git repo — wiping for re-clone"; rm -rf "$FLEET_TREE"; }
    fleet_log "provisioning dedicated worker checkout at $FLEET_TREE (clone $origin)"
    if ! git clone -q "$origin" "$FLEET_TREE" 2>>"$LOG_DIR/feature_worker_fleet.log"; then
      fleet_log "worker checkout clone FAILED — falling back to LIVE_TREE $LIVE_TREE"
      printf '%s' "$LIVE_TREE"; return 1
    fi
  fi
  if git -C "$FLEET_TREE" fetch -q origin main 2>>"$LOG_DIR/feature_worker_fleet.log" \
     && git -C "$FLEET_TREE" reset -q --hard origin/main 2>>"$LOG_DIR/feature_worker_fleet.log"; then
    printf '%s' "$FLEET_TREE"
  else
    fleet_log "worker checkout refresh FAILED — falling back to LIVE_TREE $LIVE_TREE"
    printf '%s' "$LIVE_TREE"; return 1
  fi
}

# Count live feature-worker containers (the fworker-* name prefix is the stable match — every worker runs in
# its own --rm container). grep -c prints 0 and exits non-zero on no match, so swallow the exit without a 2nd 0.
worker_count() {
  local n
  n="$(docker ps --filter "name=^/${FLEET_NAME_PREFIX}-" --format '{{.Names}}' 2>/dev/null | grep -c . 2>/dev/null)" || true
  printf '%s' "${n:-0}"
}

launch_one() {
  local tree="$1" idx="$2"
  local -a args=("--once")
  [ "$FLEET_LOOP" = "1" ] && args=("--max-iterations" "100")
  [ "$FLEET_WRITE" = "1" ] && args+=("--write-lock" "--write-cert")
  local name="${FLEET_NAME_PREFIX}-$(date -u +%s)-${idx}-$RANDOM"   # unique per launch (docker names are 1:1)
  fleet_log "launching worker $name from $tree in $FLEET_IMAGE on $FLEET_NETWORK (args: ${args[*]})"
  # --rm so the slot frees on exit (the cron is the loop); detached (-d) so this guard returns immediately.
  # The container is decoupled from the live fc: its own name, the throwaway fleet code tree at /app, ro /store.
  docker run -d --rm --name "$name" \
      --network "$FLEET_NETWORK" \
      --env-file "$FLEET_ENV_FILE" \
      -v "${FLEET_STORE_VOLUME}:/store:ro" \
      -v "${tree}:/app:ro" -w /app \
      "$FLEET_IMAGE" \
      python -m quantlib.features.feature_worker "${args[@]}" \
      >> "$LOG" 2>&1 < /dev/null || fleet_log "worker $name FAILED to launch (see $LOG)"
}

ensure_fleet() {
  local have want tree deficit
  have="$(worker_count)"
  want="$FLEET_SIZE"
  if [ "$have" -ge "$want" ]; then
    fleet_log "fleet healthy: $have/$want workers alive — no action"
    return 0
  fi
  deficit=$(( want - have ))
  tree="$(ensure_fleet_repo)"
  fleet_log "fleet below target: $have/$want — launching $deficit worker(s) [write=$FLEET_WRITE loop=$FLEET_LOOP]"
  local i
  for (( i = 0; i < deficit; i++ )); do
    launch_one "$tree" "$(( have + i + 1 ))"
    sleep 1
  done
  sleep 1
  fleet_log "post-launch: $(worker_count)/$want workers alive"
}

stop_fleet() {
  local names
  names="$(docker ps --filter "name=^/${FLEET_NAME_PREFIX}-" --format '{{.Names}}' 2>/dev/null || true)"
  if [ -z "$names" ]; then
    fleet_log "no feature-worker containers running — nothing to stop"
    return 0
  fi
  fleet_log "stopping every feature-worker container: $names"
  # shellcheck disable=SC2086
  docker rm -f $names >/dev/null 2>&1 || true
}

main() {
  case "${1:-}" in
    --status) printf 'feature-worker fleet: %s/%s workers alive\n' "$(worker_count)" "$FLEET_SIZE" ;;
    --stop) stop_fleet ;;
    "") ensure_fleet ;;
    *) echo "usage: $0 [--status | --stop]   (env: FLEET_SIZE, FLEET_WRITE, FLEET_LOOP)" >&2; return 2 ;;
  esac
}

main "$@"
