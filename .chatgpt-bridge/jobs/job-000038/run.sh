#!/usr/bin/env bash
set -Eeuo pipefail

R=/home/bking/AI/opencode-qwen38-multiagent-v2-a2-v11831
S="$R/scripts/supervisor.py"
P="$R/xdg/config/opencode/plugins/v2-bounded-subagent.js"

echo "=== PLUGIN supervisorDetached ==="
grep -n -C 18 -E 'function supervisorDetached|const supervisorDetached|supervisorDetached[[:space:]]*=' "$P" | head -260 || true

echo
echo "=== PLUGIN SPLITTER COMPLETION ==="
sed -n '570,635p' "$P"

echo
echo "=== SUPERVISOR opencode-base-url REFERENCES ==="
grep -n -C 8 -E 'opencode-base-url|opencode_base_url|V2_OPENCODE_BASE_URL' "$S" | tail -300 || true

echo
echo "=== SUPERVISOR ARGPARSE / MAIN ==="
sed -n '8580,8695p' "$S"

echo
echo "=== PLUGIN PROCESS SPAWN REFERENCES ==="
grep -n -C 12 -E 'spawn|execFile|supervisorDetached|detached' "$P" | head -420 || true

echo
echo "=== SERVER CONFIG PLUGIN COPY IDENTITY ==="
PID="$(ss -ltnp 2>/dev/null | sed -n 's/.*127\.0\.0\.1:58443.*pid=\([0-9]\+\).*/\1/p' | head -1)"
echo "SERVER_PID=$PID"
if [[ "$PID" =~ ^[0-9]+$ ]] && [[ -r "/proc/$PID/environ" ]]; then
  CFG="$(tr '\0' '\n' <"/proc/$PID/environ" | sed -n 's/^XDG_CONFIG_HOME=//p' | head -1)"
  echo "CFG=$CFG"
  if [[ -f "$CFG/opencode/plugins/v2-bounded-subagent.js" ]]; then
    sha256sum "$CFG/opencode/plugins/v2-bounded-subagent.js" "$P"
    grep -n -C 5 -- '--opencode-base-url' "$CFG/opencode/plugins/v2-bounded-subagent.js" || true
  fi
fi

echo "JOB_000038_OK"
