#!/usr/bin/env bash
set -Eeuo pipefail

R=/home/bking/AI/opencode-qwen38-multiagent-v2-a2-v11831
P=/home/bking/AI/a2-canaries/20260922-230614-d003b-live/project
S="$R/scripts/supervisor.py"

echo "=== HTTPCONTROL CLASS / REQUEST / ENSURE ==="
grep -n -E 'class .*HTTP|class Http|def request\(|def ensure\(' "$S" | head -40
echo
sed -n '5880,5985p' "$S"

echo
echo "=== ALL HTTP STATE RESETS / ASSIGNMENTS ==="
grep -n -E 'self\.base=|self\.mode=|http\.base|http\.mode|HTTP_CONTROL_' "$S" | head -220

echo
echo "=== OUTER POLL LOOP AROUND GENERIC HTTP_POLL_ERROR ==="
sed -n '7760,7900p' "$S"

echo
echo "=== FILESYSTEM ATOMIC/TEMP HELPERS ==="
grep -n -C 8 -E 'def (write_json|save_json|atomic|write_text)|NamedTemporaryFile|mkstemp|os\.replace|Path\(.*tmp|\.tmp' "$S" | head -320

echo
echo "=== FILE-NOT-FOUND HANDLING / UNLINK / REPLACE ==="
grep -n -C 5 -E 'FileNotFoundError|unlink\(|os\.remove|os\.replace|replace\(' "$S" | head -320

echo
echo "=== SPLITTER COMPLETION/CORRECTIVE CALL CHAIN ==="
grep -n -C 12 -E 'begin_splitter_corrective_turn|dispatch_splitter_corrective_turn|complete_splitter|recover_pending_splitter_completion' "$S" | head -520

echo
echo "=== CURRENT SUPERVISOR PID/FD DETAIL ==="
W="$P/.opencode-v2/work"
SUP_PID="$(cat "$W/supervisor.pid" 2>/dev/null || true)"
echo "SUP_PID=$SUP_PID"
if [[ "$SUP_PID" =~ ^[0-9]+$ ]] && [[ -d "/proc/$SUP_PID" ]]; then
  ps -p "$SUP_PID" -o pid,ppid,lstart,etime,stat,wchan:30,args
  echo "--- fds ---"
  for f in /proc/"$SUP_PID"/fd/*; do
    [[ -e "$f" || -L "$f" ]] || continue
    printf '%s -> ' "$(basename "$f")"
    readlink "$f" || true
  done
fi

echo
echo "=== PROJECT TMP/LOCK FILES ==="
find "$P/.opencode-v2" -type f \( -name '*.tmp' -o -name '.*.tmp' -o -name '*.lock' \) -printf '%TY-%Tm-%Td %TH:%TM:%TS %s %p\n' | sort | tail -120 || true

echo
echo "=== RUN SERVER SCRIPT ==="
sed -n '1,220p' "$R/scripts/run-a2-v11831-server.sh"

echo
echo "=== SERVER 58443 ENV RELEVANT ==="
PID="$(ss -ltnp 2>/dev/null | sed -n 's/.*127\.0\.0\.1:58443.*pid=\([0-9]\+\).*/\1/p' | head -1)"
echo "SERVER_PID=$PID"
if [[ "$PID" =~ ^[0-9]+$ ]] && [[ -r "/proc/$PID/environ" ]]; then
  tr '\0' '\n' < "/proc/$PID/environ" | grep -E '^(PWD|XDG_CONFIG_HOME|XDG_DATA_HOME|OPENCODE_|V2_|HOME)=' | sed -E 's/^(VLLM_API_KEY|OPENCODE_PASSWORD)=.*/\1=<redacted>/' || true
fi

echo
echo "=== REPEATED STATUS PROBE (20x) ==="
for i in $(seq 1 20); do
  code="$(curl -sS -o /tmp/a2-status-32.txt -w '%{http_code}' -G http://127.0.0.1:58443/session/status --data-urlencode "directory=$P" || true)"
  printf '%02d code=%s body=' "$i" "$code"
  cat /tmp/a2-status-32.txt 2>/dev/null || true
  echo
  sleep 0.1
done

echo "JOB_000032_OK"
