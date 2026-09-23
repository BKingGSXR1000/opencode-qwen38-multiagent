#!/usr/bin/env bash
set -Eeuo pipefail
R=/home/bking/AI/opencode-qwen38-multiagent-v2-a2-v11831
P=/home/bking/AI/a2-canaries/20260922-230614-d003b-live/project
S="$R/scripts/supervisor.py"
L="$R/logs/supervisor-stderr.log"

echo "=== EXACT ENSURE/DISCOVER ==="
sed -n '5938,6140p' "$S"

echo
echo "=== FAILURE WINDOW ==="
grep -n -E '23:16:|D003-B|HTTP_CONTROL_|HTTP_POLL_ERROR|SPLIT_PROPOSAL_REJECTED|SPLITTER_CORRECTIVE' "$L" | tail -220 || true

echo
echo "=== PERSIST_SPLIT / VALIDATION ==="
grep -n -C 18 -E '^def persist_split|materially reduce|writer\+tester|proposal.*reduce|owned_artifacts' "$S" | head -700 || true

echo
echo "=== INGRESS REJECTION WINDOW ==="
journalctl --user -u chatgpt-job-ingress.service --since "2026-09-22 21:28:00" --until "2026-09-23 07:20:00" --no-pager -o short-iso | tail -120 || true

echo
echo "=== CURRENT SERVER PROBES ==="
for i in $(seq 1 20); do
  code="$(curl -sS -o /tmp/a2-34-body -w '%{http_code}' -G http://127.0.0.1:58443/session/status --data-urlencode "directory=$P" || true)"
  printf '%02d %s ' "$i" "$code"
  cat /tmp/a2-34-body 2>/dev/null || true
  echo
done

echo "JOB_000034_OK"
