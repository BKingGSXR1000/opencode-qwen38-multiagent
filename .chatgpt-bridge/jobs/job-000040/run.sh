#!/usr/bin/env bash
set -Eeuo pipefail

R=/home/bking/AI/opencode-qwen38-multiagent-v2-a2-v11831
P=/home/bking/AI/a2-canaries/20260922-230614-d003b-live/project
PORT=58443
LOG="$R/logs/stage-a-server-$PORT-refresh.log"
CANON="$R/xdg/config/opencode/plugins/v2-bounded-subagent.js"

echo "=== RECOVER VLLM KEY ==="
VLLM_API_KEY=""
for e in /proc/[0-9]*/environ; do
  [[ -r "$e" ]] || continue
  k="$(tr '\0' '\n' <"$e" 2>/dev/null | sed -n 's/^VLLM_API_KEY=//p' | head -1 || true)"
  if [[ -n "$k" ]]; then
    VLLM_API_KEY="$k"
    break
  fi
done
[[ -n "$VLLM_API_KEY" ]] || { echo "ERROR: VLLM_API_KEY not found"; exit 30; }
export VLLM_API_KEY
echo "VLLM_API_KEY recovered"

echo
echo "=== STOP OLD 58443 SERVER ==="
OLDPID="$(ss -ltnp 2>/dev/null | sed -n 's/.*127\.0\.0\.1:58443.*pid=\([0-9]\+\).*/\1/p' | head -1)"
echo "OLDPID=$OLDPID"
if [[ "$OLDPID" =~ ^[0-9]+$ ]]; then
  kill "$OLDPID" || true
  for _ in $(seq 1 40); do
    if ! kill -0 "$OLDPID" 2>/dev/null; then break; fi
    sleep 0.25
  done
fi
if ss -ltnp 2>/dev/null | grep -q '127\.0\.0\.1:58443'; then
  echo "ERROR: port 58443 still occupied"
  ss -ltnp 2>/dev/null | grep ':58443' || true
  exit 31
fi

echo
echo "=== START FRESH 58443 SERVER ==="
nohup "$R/scripts/run-a2-v11831-server.sh" "$P" "$PORT" >"$LOG" 2>&1 < /dev/null &
WRAPPER_PID=$!
echo "WRAPPER_PID=$WRAPPER_PID"

READY=0
for _ in $(seq 1 80); do
  if curl -fsS -G "http://127.0.0.1:$PORT/session/status" \
      --data-urlencode "directory=$P" >/tmp/a2-40-status.json 2>/dev/null; then
    READY=1
    break
  fi
  sleep 0.25
done
[[ "$READY" == 1 ]] || {
  echo "ERROR: refreshed server did not become ready"
  tail -120 "$LOG" || true
  exit 32
}

NEWPID="$(ss -ltnp 2>/dev/null | sed -n 's/.*127\.0\.0\.1:58443.*pid=\([0-9]\+\).*/\1/p' | head -1)"
echo "NEWPID=$NEWPID"
[[ "$NEWPID" =~ ^[0-9]+$ ]] || { echo "ERROR: listener PID not found"; exit 33; }

CFG="$(tr '\0' '\n' <"/proc/$NEWPID/environ" | sed -n 's/^XDG_CONFIG_HOME=//p' | head -1)"
LIVE="$CFG/opencode/plugins/v2-bounded-subagent.js"
echo "CFG=$CFG"

echo
echo "=== VERIFY LIVE PLUGIN ==="
CANON_SHA="$(sha256sum "$CANON" | awk '{print $1}')"
LIVE_SHA="$(sha256sum "$LIVE" | awk '{print $1}')"
echo "CANON_SHA=$CANON_SHA"
echo "LIVE_SHA=$LIVE_SHA"
[[ "$CANON_SHA" == "$LIVE_SHA" ]] || { echo "ERROR: refreshed overlay is still stale"; exit 34; }

grep -n -C 4 -- '--opencode-base-url' "$LIVE"
grep -n -C 4 'supervisorDetached' "$LIVE" | head -80

echo
echo "=== SERVER STATUS ==="
cat /tmp/a2-40-status.json
echo
tail -80 "$LOG" || true

echo "JOB_000040_OK"
