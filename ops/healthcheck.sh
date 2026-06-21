#!/usr/bin/env bash
# Host wrapper for the live feature-platform health-check.
#
# Does the docker-level checks that the in-container python cannot, then execs the python health-check
# for everything else. Conservative, clearly-logged SAFE fixes only:
#   - verifies the feature-computer container is Up with restart=unless-stopped
#   - verifies it holds an ESTABLISHED :443 connection to the Alpaca stream host
#   - with --fix, restarts the container ONLY if it is demonstrably dead (exited/crash-looping),
#     never when it is merely degraded
#   - runs `docker exec feature-computer python -m quantlib.ops.healthcheck --json "$@"`,
#     pretty-prints the result, and propagates the python exit code
#
# Exit codes: 0 = no FAIL anywhere, 1 = a FAIL (host-level or python), 2 = wrapper internal error.
#
# Usage:
#   ops/healthcheck.sh                       # read-only, text output
#   ops/healthcheck.sh --json                # read-only, JSON (for the cron)
#   ops/healthcheck.sh --fix                 # allow a conservative restart if container is dead
#   ops/healthcheck.sh --fix --session-phase rth
set -u

CONTAINER="feature-computer"
ALPACA_HOSTS="stream.data.alpaca.markets stream.data.sandbox.alpaca.markets"
DO_FIX=0
PY_ARGS=()

for arg in "$@"; do
  case "$arg" in
    --fix) DO_FIX=1 ;;
    *) PY_ARGS+=("$arg") ;;
  esac
done

log()  { printf '[healthcheck.sh] %s\n' "$*" >&2; }
fail() { printf '[healthcheck.sh] FAIL: %s\n' "$*" >&2; }
warn() { printf '[healthcheck.sh] WARN: %s\n' "$*" >&2; }

HOST_FAIL=0

# ---------------------------------------------------------------------------
# 1. Container is Up with restart=unless-stopped
# ---------------------------------------------------------------------------
if ! docker inspect "$CONTAINER" >/dev/null 2>&1; then
  fail "container '$CONTAINER' does not exist"
  exit 2
fi

STATE=$(docker inspect "$CONTAINER" --format '{{.State.Status}}')
RUNNING=$(docker inspect "$CONTAINER" --format '{{.State.Running}}')
RESTART_POLICY=$(docker inspect "$CONTAINER" --format '{{.HostConfig.RestartPolicy.Name}}')
RESTART_COUNT=$(docker inspect "$CONTAINER" --format '{{.RestartCount}}')

log "container state=$STATE running=$RUNNING restart_policy=$RESTART_POLICY restart_count=$RESTART_COUNT"

if [ "$RESTART_POLICY" != "unless-stopped" ]; then
  warn "restart policy is '$RESTART_POLICY', expected 'unless-stopped'"
fi

# ---------------------------------------------------------------------------
# 2. crash-loop / dead detection — drives whether --fix may act
# ---------------------------------------------------------------------------
# A short-lived state file tracks RestartCount between runs so we can see it climbing.
STATE_DIR="${HEALTHCHECK_STATE_DIR:-/tmp/quant-healthcheck}"
mkdir -p "$STATE_DIR" 2>/dev/null || true
RC_FILE="$STATE_DIR/${CONTAINER}.restart_count"
PREV_RC=0
[ -f "$RC_FILE" ] && PREV_RC=$(cat "$RC_FILE" 2>/dev/null || echo 0)
echo "$RESTART_COUNT" > "$RC_FILE" 2>/dev/null || true
RC_DELTA=$(( RESTART_COUNT - PREV_RC ))

CONTAINER_DEAD=0
if [ "$RUNNING" != "true" ] || [ "$STATE" = "exited" ] || [ "$STATE" = "dead" ]; then
  fail "container is not running (state=$STATE)"
  CONTAINER_DEAD=1
  HOST_FAIL=1
fi

if [ "$RC_DELTA" -gt 0 ]; then
  if [ "$RC_DELTA" -gt 2 ]; then
    fail "container restarted $RC_DELTA times since last check — crash-looping"
    CONTAINER_DEAD=1
    HOST_FAIL=1
  else
    warn "container restarted $RC_DELTA time(s) since last check"
  fi
fi

# ---------------------------------------------------------------------------
# 3. ESTABLISHED :443 connection to an Alpaca stream host
#    (no ss/netstat in the container — decode /proc/net/tcp instead)
# ---------------------------------------------------------------------------
ALPACA_OK=0
if [ "$CONTAINER_DEAD" -eq 0 ]; then
  # Resolve Alpaca stream IPs (from inside the container's resolver) and look for an established
  # (TCP state 01) connection to one of them on remote port 443 (hex 01BB) in /proc/net/tcp.
  RESOLVED_IPS=""
  for host in $ALPACA_HOSTS; do
    ip=$(docker exec "$CONTAINER" python -c "import socket,sys
try:
    print(socket.gethostbyname(sys.argv[1]))
except OSError:
    pass" "$host" 2>/dev/null)
    [ -n "$ip" ] && RESOLVED_IPS="$RESOLVED_IPS $ip"
  done

  # Decode /proc/net/tcp in python (the container's /bin/sh is dash; no bash substring expansion).
  # Emit the remote IPs of ESTABLISHED (state 01) connections on remote port 443 (hex 01BB).
  ESTABLISHED=$(docker exec "$CONTAINER" python -c '
peer_ips = set()
with open("/proc/net/tcp") as handle:
    next(handle)
    for line in handle:
        parts = line.split()
        if len(parts) < 4 or parts[3] != "01":
            continue
        ip_hex, port_hex = parts[2].split(":")
        if port_hex.upper() != "01BB":
            continue
        octets = [int(ip_hex[offset:offset + 2], 16) for offset in (6, 4, 2, 0)]
        peer_ips.add(".".join(str(octet) for octet in octets))
print("\n".join(sorted(peer_ips)))
' 2>/dev/null)

  for resolved in $RESOLVED_IPS; do
    if printf '%s\n' "$ESTABLISHED" | grep -qx "$resolved"; then
      ALPACA_OK=1
      log "ESTABLISHED :443 connection to Alpaca stream host ($resolved)"
      break
    fi
  done

  if [ "$ALPACA_OK" -eq 0 ]; then
    # Fall back to the launcher log marker before declaring failure.
    if docker logs "$CONTAINER" 2>&1 | grep -qiE 'live_capture.*feed=|subscrib|authenticated'; then
      warn "no ESTABLISHED :443 to Alpaca found in /proc/net/tcp, but capture log shows a feed marker"
    else
      fail "no ESTABLISHED :443 connection to an Alpaca stream host"
      HOST_FAIL=1
    fi
  fi
fi

# ---------------------------------------------------------------------------
# 4. Conservative SAFE fix — restart ONLY if demonstrably dead/crash-looping
# ---------------------------------------------------------------------------
if [ "$DO_FIX" -eq 1 ]; then
  if [ "$CONTAINER_DEAD" -eq 1 ]; then
    log "FIX: container is demonstrably dead/crash-looping (state=$STATE delta=$RC_DELTA) — restarting"
    if docker restart "$CONTAINER" >/dev/null 2>&1; then
      log "FIX: docker restart $CONTAINER issued"
    else
      fail "FIX: docker restart failed"
    fi
  else
    log "FIX: container is alive (possibly degraded) — NOT restarting (safe-fix policy)"
  fi
fi

# ---------------------------------------------------------------------------
# 5. In-container python checks
# ---------------------------------------------------------------------------
PY_EXIT=0
if [ "$CONTAINER_DEAD" -eq 1 ] && [ "$DO_FIX" -eq 0 ]; then
  warn "skipping in-container checks: container is not running"
  PY_EXIT=1
else
  PY_JSON=$(docker exec "$CONTAINER" python -m quantlib.ops.healthcheck --json "${PY_ARGS[@]}" 2>/dev/null)
  PY_EXIT=$?
  if [ -z "$PY_JSON" ]; then
    fail "in-container healthcheck produced no output (exit=$PY_EXIT)"
    PY_EXIT=2
  else
    # Pretty-print: compact one-line-per-check table + summary, jq if present else raw.
    if command -v jq >/dev/null 2>&1; then
      printf '%s\n' "$PY_JSON" | jq -r '
        "HEALTHCHECK phase=\(.phase) ts=\(.ts)",
        (.checks[] | "  \(.status|.[0:4]) \(.name)  \(.detail)"),
        "HEALTHCHECK \(.summary.pass) PASS / \(.summary.warn) WARN / \(.summary.fail) FAIL\((.summary.skip // 0) | if . > 0 then " / \(.) SKIP" else "" end)"
      '
    else
      printf '%s\n' "$PY_JSON"
    fi
  fi
fi

# ---------------------------------------------------------------------------
# 6. Page on failure (G9). Fire the single host notifier when the FAIL count exceeds the baseline. After
#    the G10 market-aware SKIP fix the baseline on a healthy trading day is 0, and store-coverage checks
#    SKIP (not FAIL) off-session — so this pages on a REAL outage, not every weekend. notify.py is a no-op
#    when QUANT_ALERT_WEBHOOK is unset and rate-limits the same dedup key, so this is safe + non-spammy.
# ---------------------------------------------------------------------------
PY_FAIL_COUNT=0
FAIL_NAMES=""
if [ -n "${PY_JSON:-}" ] && command -v jq >/dev/null 2>&1; then
  PY_FAIL_COUNT=$(printf '%s\n' "$PY_JSON" | jq -r '.summary.fail // 0' 2>/dev/null || echo 0)
  FAIL_NAMES=$(printf '%s\n' "$PY_JSON" | jq -r '[.checks[] | select(.status=="FAIL") | .name] | join(", ")' 2>/dev/null || echo "")
fi
TOTAL_FAIL=$(( HOST_FAIL + PY_FAIL_COUNT ))
if [ "$TOTAL_FAIL" -gt "${HEALTHCHECK_FAIL_BASELINE:-0}" ]; then
  NOTIFY="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/notify.py"
  [ -n "$FAIL_NAMES" ] || FAIL_NAMES="host-level check"
  python3 "$NOTIFY" --dedup-key healthcheck-fail \
    --title "healthcheck: ${TOTAL_FAIL} FAIL" \
    --body "failing: ${FAIL_NAMES} (host_fail=${HOST_FAIL}, py_fail=${PY_FAIL_COUNT})" >/dev/null 2>&1 || true
fi

# ---------------------------------------------------------------------------
# 7. Combined exit code
# ---------------------------------------------------------------------------
if [ "$HOST_FAIL" -eq 1 ] || [ "$PY_EXIT" -eq 1 ]; then
  exit 1
fi
if [ "$PY_EXIT" -eq 2 ]; then
  exit 2
fi
exit 0
