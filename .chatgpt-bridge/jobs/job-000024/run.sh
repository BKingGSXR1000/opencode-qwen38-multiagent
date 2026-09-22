#!/usr/bin/env bash
set -Eeuo pipefail
R=/home/bking/AI/opencode-qwen38-multiagent-v2-a2-v11831
P=/home/bking/AI/a2-canaries/20260922-230614-d003b-live/project

echo "=== LOCAL RUN-STAGE-A-TICK PREFLIGHT CONTRACT ==="
grep -n -C 8 -E 'preflight-proof|preflight_proof|PREFLIGHT|proof' "$R/scripts/run-stage-a-tick.py" || true

echo
echo "=== RELATED LOCAL HELPERS ==="
grep -RIn -E 'preflight-proof|preflight_proof' "$R/scripts" --exclude='run-stage-a-tick.py' | head -120 || true

echo
echo "=== EXISTING PROJECT PROOF-LIKE FILES ==="
find "$P/.opencode-v2" -maxdepth 3 -type f \( -iname '*proof*' -o -iname '*preflight*' \) -print 2>/dev/null | sort || true

echo
echo "=== ROOT RECEIPT ==="
cat /home/bking/AI/a2-canaries/20260922-230614-d003b-live/root.json || true

echo
echo "JOB_000024_OK"
