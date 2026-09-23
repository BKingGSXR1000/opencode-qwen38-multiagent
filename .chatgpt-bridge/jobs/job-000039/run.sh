#!/usr/bin/env bash
set -Eeuo pipefail

R=/home/bking/AI/opencode-qwen38-multiagent-v2-a2-v11831
CANON="$R/xdg/config/opencode/plugins/v2-bounded-subagent.js"
PID="$(ss -ltnp 2>/dev/null | sed -n 's/.*127\.0\.0\.1:58443.*pid=\([0-9]\+\).*/\1/p' | head -1)"
echo "SERVER_PID=$PID"

CFG=""
if [[ "$PID" =~ ^[0-9]+$ ]] && [[ -r "/proc/$PID/environ" ]]; then
  CFG="$(tr '\0' '\n' <"/proc/$PID/environ" | sed -n 's/^XDG_CONFIG_HOME=//p' | head -1)"
fi
LIVE="$CFG/opencode/plugins/v2-bounded-subagent.js"

echo "=== FILE IDENTITY ==="
stat -c '%y %s %n' "$CANON" "$LIVE" 2>/dev/null || true
sha256sum "$CANON" "$LIVE" 2>/dev/null || true

echo
echo "=== LIVE COMPLETION HOOK ==="
grep -n -C 12 -E 'supervisorDetached|complete-splitter|opencode-base-url' "$LIVE" | head -320 || true

echo
echo "=== CANONICAL COMPLETION HOOK ==="
grep -n -C 12 -E 'supervisorDetached|complete-splitter|opencode-base-url' "$CANON" | head -320 || true

echo
echo "=== RELEVANT DIFF ==="
diff -u "$LIVE" "$CANON" | grep -C 12 -E 'supervisorDetached|complete-splitter|opencode-base-url|prompt_async|SubtaskPart|command' | head -520 || true

echo
echo "=== OVERLAY CREATION PATH ==="
grep -n -C 12 -E 'overlay|copy|copy2|plugins|XDG_CONFIG_HOME' "$R/scripts/stage_a_path_permissions.py" "$R/scripts/run-a2-v11831-server.sh" | head -520 || true

echo
echo "JOB_000039_OK"
