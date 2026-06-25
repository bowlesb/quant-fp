#!/usr/bin/env bash
# Nightly CLEAN relaunch — prepare a trustworthy session for a given trade date.
#
# WHY: today's live capture was launched with a HARDCODED date arg, no warm-start, on the OLD universe.
# Each night this script makes tomorrow's open clean:
#   1. RE-SEED universe_membership for the session date with the merged ETF/fund filter (seed_universe,
#      which screens via quantlib.universe.is_etf_like) — the ~common-stock + index-ETF set.
#   2. RELAUNCH the feature-computer capture for the NEW date WITH FP_WARM_START=1, faithfully reproducing
#      the running container's run config (image / network / restart / mounts / --env-file / command) from
#      `docker inspect`, then adding `-e FP_WARM_START=1` and the new date. So the ring rehydrates from
#      backfill and features are parity-clean from minute one (no cold-buffer corruption).
#   3. VERIFY the container comes back healthy (subscribe log marker, warm-start marker, still running).
#
# Idempotent + safe:
#   * The reseed UPSERTs (re-running a date overwrites to the same membership).
#   * The relaunch removes any existing feature-computer first, then recreates — re-running converges.
#   * --dry-run does NOTHING destructive: it prints the reseed count (in-memory, no DB write) and the exact
#     `docker run` command it WOULD issue, but neither writes the DB nor touches the container.
#
# Usage:
#   ops/nightly_relaunch.sh 2026-06-16                 # real relaunch for that session date
#   ops/nightly_relaunch.sh 2026-06-16 --dry-run       # show what it would do; change nothing
#   STORE_ROOT=/store ENV_FILE=.env ops/nightly_relaunch.sh 2026-06-16
#   UNIVERSE_MAX_SYMBOLS=100000 ops/nightly_relaunch.sh 2026-06-16   # seed the FULL ~7.4k filtered set
#       (default cap is seed_universe's UNIVERSE_MAX_SYMBOLS=3000; raise it to seed the whole clean universe)
#
# The LEAD runs the real relaunch (this script without --dry-run) and schedules the cron; subagents only
# ever exercise --dry-run + the sim parity path (see the PR evidence).
set -euo pipefail

CONTAINER="feature-computer"
IMAGE_FALLBACK="fp-dev"
NETWORK_FALLBACK="quant_default"
ENV_FILE="${ENV_FILE:-.env}"
STORE_ROOT="${STORE_ROOT:-/store}"
LAUNCH_MODULE="quantlib.features.live_capture"
# Bias the kernel AWAY from picking fc as the OOM victim when the shared box spikes (fc has no mem LIMIT
# and was killed at 9% usage by a host-wide spike on 2026-06-24). Negative => protect; -500 (not -1000) keeps
# fc killable as a last resort, and this sets victim PRIORITY only, never a memory cap (a cap would make fc
# OOM itself as its warm rings legitimately grow). Set OOM_SCORE_ADJ= (empty) to omit the flag.
OOM_SCORE_ADJ="${OOM_SCORE_ADJ:--500}"

log()  { printf '[nightly_relaunch] %s\n' "$*" >&2; }
fail() { printf '[nightly_relaunch] FATAL: %s\n' "$*" >&2; exit 1; }

DRY_RUN=0
TRADE_DATE=""
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    [0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]) TRADE_DATE="$arg" ;;
    *) fail "unrecognized argument: $arg (usage: nightly_relaunch.sh <YYYY-MM-DD> [--dry-run])" ;;
  esac
done
[ -n "$TRADE_DATE" ] || fail "a session date (YYYY-MM-DD) is required"
[ -f "$ENV_FILE" ]   || fail "env file '$ENV_FILE' not found (run from the repo root, or set ENV_FILE=)"

# Resolve image/network from the running container when present, else fallbacks (so the reseed helper
# can still run, and so a first-ever launch has a sane default).
if docker inspect "$CONTAINER" >/dev/null 2>&1; then
  IMAGE=$(docker inspect "$CONTAINER" --format '{{.Config.Image}}')
  NETWORK=$(docker inspect "$CONTAINER" --format '{{.HostConfig.NetworkMode}}')
else
  log "no existing '$CONTAINER' container; using fallbacks image=$IMAGE_FALLBACK network=$NETWORK_FALLBACK"
  IMAGE="$IMAGE_FALLBACK"
  NETWORK="$NETWORK_FALLBACK"
fi

run_helper() {  # run a quantlib/ops python entrypoint inside fp-dev with the repo mounted + env-file
  # UNIVERSE_MAX_SYMBOLS (the membership cap seed_universe / reseed-count honor) is forwarded when set so
  # the operator can size the clean universe (e.g. the full ~7.4k filtered set, or a smaller capped rollout).
  local extra=()
  [ -n "${UNIVERSE_MAX_SYMBOLS:-}" ] && extra+=(-e "UNIVERSE_MAX_SYMBOLS=${UNIVERSE_MAX_SYMBOLS}")
  docker run --rm --env-file "$ENV_FILE" --network "$NETWORK" "${extra[@]}" \
    -v "$PWD":/app -w /app "$IMAGE" "$@"
}

# Build the EXACT `docker run` argv for the relaunch, faithfully reproduced from `docker inspect` of the
# running container (image / network / restart policy / mounts / workdir / command) + FP_WARM_START=1 and
# the NEW date. Env is supplied via --env-file (NOT per-key copied from the container — that holds secrets,
# and would print them). Prints the command to stdout; never executes it.
build_run_cmd() {
  local image network restart workdir
  if docker inspect "$CONTAINER" >/dev/null 2>&1; then
    image="$IMAGE"
    network="$NETWORK"
    restart=$(docker inspect "$CONTAINER" --format '{{.HostConfig.RestartPolicy.Name}}')
    workdir=$(docker inspect "$CONTAINER" --format '{{.Config.WorkingDir}}')
  else
    image="$IMAGE"; network="$NETWORK"; restart="unless-stopped"; workdir="/app"
  fi
  [ -n "$restart" ] || restart="unless-stopped"
  [ -n "$workdir" ] || workdir="/app"

  # Warm-start is now DEFAULT ON (WARM_START=1). The 7-col-bar-ring vs 13-col-tick-enriched ShapeError that
  # forced it OFF (2026-06-17) was fixed by the diagonal-concat ring fix (#165) + the source-agnostic
  # `populated` invariant (#170). Validated 2026-06-19 by the offline equity-relaunch dry-run (real 06-18
  # bars, 60 syms incl SPY/QQQ/IWM, 391 RTH min): 7-col seed + 13-col tick live minute concat clean, no
  # ShapeError at scale, populated asserts fire, and the warm first live minute == a never-restarted capture
  # cell-for-cell. Set WARM_START=0 to force the old cold-ring relaunch (rollback). Cross-sectional +
  # daily-frame features are clean from minute one either way.
  local cmd=(docker run -d --name "$CONTAINER" --restart "$restart" --network "$network"
             --env-file "$ENV_FILE" -w "$workdir")
  [ -n "$OOM_SCORE_ADJ" ] && cmd+=(--oom-score-adj "$OOM_SCORE_ADJ")
  [ "${WARM_START:-1}" = "1" ] && cmd+=(-e FP_WARM_START=1)
  if docker inspect "$CONTAINER" >/dev/null 2>&1; then
    # Reproduce each mount (bind: host path; volume: volume name) -> destination, preserving ro/rw.
    while IFS=$'\t' read -r mtype source dest rw; do
      [ -n "$dest" ] || continue
      local suffix=""
      [ "$rw" = "false" ] && suffix=":ro"
      cmd+=(-v "${source}:${dest}${suffix}")
    done < <(docker inspect "$CONTAINER" --format \
      '{{range .Mounts}}{{.Type}}{{"\t"}}{{if eq .Type "volume"}}{{.Name}}{{else}}{{.Source}}{{end}}{{"\t"}}{{.Destination}}{{"\t"}}{{.RW}}{{"\n"}}{{end}}')
  else
    cmd+=(-v "$PWD:/app" -v "fp_store_real:$STORE_ROOT")
  fi
  cmd+=("$image" python -m "$LAUNCH_MODULE" "$TRADE_DATE" "$STORE_ROOT")
  printf '%q ' "${cmd[@]}"
  printf '\n'
}

log "session_date=$TRADE_DATE store_root=$STORE_ROOT env_file=$ENV_FILE image=$IMAGE network=$NETWORK dry_run=$DRY_RUN"

# ---------------------------------------------------------------------------
# STEP 1 — RE-SEED the universe (merged ETF/fund filter) for the session date.
# ---------------------------------------------------------------------------
if [ "$DRY_RUN" -eq 1 ]; then
  log "STEP 1 (dry-run): filtered universe count seed_universe WOULD write (no DB write):"
  run_helper python -m ops.nightly_relaunch reseed-count "$TRADE_DATE"
else
  log "STEP 1: seeding universe_membership for $TRADE_DATE (asset_metadata + filtered membership)"
  run_helper python -m quantlib.features.seed_universe "$TRADE_DATE"
fi

# ---------------------------------------------------------------------------
# STEP 2 — RELAUNCH the capture for the NEW date WITH FP_WARM_START=1.
# ---------------------------------------------------------------------------
RUN_CMD=$(build_run_cmd)
log "STEP 2: relaunch command (FP_WARM_START=1, new date, faithful run config):"
printf '    %s\n' "$RUN_CMD" >&2

if [ "$DRY_RUN" -eq 1 ]; then
  log "STEP 2 (dry-run): NOT executing the relaunch; the LEAD runs the real relaunch."
  log "dry-run complete — nothing changed."
  exit 0
fi

log "STEP 2: removing any existing '$CONTAINER' then recreating with FP_WARM_START=1"
docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
eval "$RUN_CMD" >/dev/null
log "STEP 2: container created"

# ---------------------------------------------------------------------------
# STEP 3 — VERIFY healthy: subscribe + warm-start log markers, then still running.
# ---------------------------------------------------------------------------
log "STEP 3: waiting for subscribe + warm-start log markers"
SAW_WARM=0
SAW_SUBSCRIBE=0
for _ in $(seq 1 60); do
  LOGS=$(docker logs "$CONTAINER" 2>&1 || true)
  printf '%s\n' "$LOGS" | grep -qiE 'warm-started ring' && SAW_WARM=1
  printf '%s\n' "$LOGS" | grep -qiE 'live_capture\] day=|subscrib|authenticated|feed=' && SAW_SUBSCRIBE=1
  [ "$SAW_WARM" -eq 1 ] && [ "$SAW_SUBSCRIBE" -eq 1 ] && break
  sleep 5
done
[ "$SAW_SUBSCRIBE" -eq 1 ] || fail "no subscribe/feed marker in container log after relaunch"
if [ "$SAW_WARM" -eq 1 ]; then
  log "STEP 3: warm-start marker present"
else
  log "STEP 3: WARN no warm-start marker yet (expected if no session bars exist before pre-market)"
fi

STATE=$(docker inspect "$CONTAINER" --format '{{.State.Status}}')
[ "$STATE" = "running" ] || fail "container not running after relaunch (state=$STATE)"
log "STEP 3: container running; deferring freshness recovery to ops/healthcheck.sh"
log "relaunch complete for $TRADE_DATE — run 'ops/healthcheck.sh' once the feed is live to confirm freshness."
