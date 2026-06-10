#!/usr/bin/env bash
#
# Phase 1 review gate: kill a worker mid-task and prove the queue loses nothing.
#
# Spins up several worker processes against a REAL Redis, kills one while it
# holds unacked tasks, lets the Reaper reclaim the orphaned PEL entries, and
# verifies: every task eventually completes (zero loss) and side effects stay
# de-duplicated (zero duplicate side effects).
#
# Usage:
#   scripts/chaos_kill_worker.sh [TASK_COUNT]
#
# Requires a reachable Redis (REDIS_URL, default redis://localhost:6379/0).
# If none is reachable it will try `docker compose up -d redis`.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

export REDIS_URL="${REDIS_URL:-redis://localhost:6379/0}"
export PYTHONPATH="${REPO_ROOT}/src:${PYTHONPATH:-}"
PY="${PYTHON:-python3}"
COUNT="${1:-200}"
HARNESS="scripts/chaos_worker.py"

PIDS=()
cleanup() {
  for pid in "${PIDS[@]:-}"; do
    kill "$pid" 2>/dev/null || true
  done
  wait 2>/dev/null || true
}
trap cleanup EXIT

# ── 0. Ensure Redis is up ───────────────────────────────────────────────────
if ! "$PY" - <<'PYEOF' 2>/dev/null
import os, sys, redis
redis.from_url(os.environ["REDIS_URL"], decode_responses=True).ping()
PYEOF
then
  echo "[chaos] Redis not reachable at $REDIS_URL — starting via docker compose"
  docker compose up -d redis
  for _ in $(seq 1 30); do
    if "$PY" - <<'PYEOF' 2>/dev/null
import os, redis
redis.from_url(os.environ["REDIS_URL"], decode_responses=True).ping()
PYEOF
    then break; fi
    sleep 1
  done
fi

# ── 1. Clean slate + seed tasks ─────────────────────────────────────────────
"$PY" "$HARNESS" flush
"$PY" "$HARNESS" enqueue --count "$COUNT" --run-id "chaos-$(date +%s)"

# ── 2. Reaper in the background (reclaims idle PEL entries) ──────────────────
"$PY" "$HARNESS" reap --idle-ms 1500 --interval 0.5 --max-seconds 120 &
PIDS+=("$!")

# ── 3. Three healthy workers ────────────────────────────────────────────────
for name in alpha bravo charlie; do
  "$PY" "$HARNESS" work --consumer "worker-$name" --max-seconds 120 &
  PIDS+=("$!")
done

# ── 4. One doomed worker: grabs tasks, never acks, then gets SIGKILLed ───────
echo "[chaos] launching doomed worker (will die holding unacked tasks)"
"$PY" "$HARNESS" work --consumer worker-doomed --die-after 5 --max-seconds 120 &
DOOMED_PID="$!"
PIDS+=("$DOOMED_PID")

# Give it a moment to consume + hold a few tasks, then make sure it is dead.
sleep 3
if kill -9 "$DOOMED_PID" 2>/dev/null; then
  echo "[chaos] sent SIGKILL to doomed worker (pid $DOOMED_PID)"
fi

# ── 5. Wait for the survivors + reaper to drain everything ──────────────────
echo "[chaos] waiting for recovery (reaper reclaim + redelivery)…"
DEADLINE=$(( $(date +%s) + 120 ))
while [ "$(date +%s)" -lt "$DEADLINE" ]; do
  if "$PY" - <<'PYEOF'
import os, sys, redis
r = redis.from_url(os.environ["REDIS_URL"], decode_responses=True)
total = int(r.get("chaos:total") or 0)
done = r.scard("chaos:completed")
sys.exit(0 if total and done >= total else 1)
PYEOF
  then break; fi
  sleep 2
done

# ── 6. Verdict ──────────────────────────────────────────────────────────────
cleanup
trap - EXIT
"$PY" "$HARNESS" verify
