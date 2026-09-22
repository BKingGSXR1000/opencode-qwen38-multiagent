#!/usr/bin/env bash
set -Eeuo pipefail

REPO=/home/bking/AI/opencode-qwen38-multiagent-v2-a2-v11831

echo "=== REPO ==="
git -C "$REPO" branch --show-current
git -C "$REPO" rev-parse HEAD
git -C "$REPO" status --short

echo
echo "=== INTENDED LOCAL DIFF: stage_a_controller.py ==="
git -C "$REPO" diff -- scripts/stage_a_controller.py || true

echo
echo "=== INTENDED LOCAL DIFF: supervisor.py ==="
git -C "$REPO" diff -- scripts/supervisor.py || true

echo
echo "=== LOCAL TEST: test_transport_root_no_continuation.py ==="
if [[ -f "$REPO/scripts/test_transport_root_no_continuation.py" ]]; then
    sed -n '1,360p' "$REPO/scripts/test_transport_root_no_continuation.py"
else
    echo "MISSING"
fi

echo
echo "=== CANARY/RUN HELPERS ==="
find "$REPO/scripts" -maxdepth 1 -type f \
  \( -iname '*canary*' -o -iname '*stage-a*' -o -iname '*a2*v11831*' \) \
  -printf '%f\n' | sort

echo
echo "=== RELEVANT USER SERVICES ==="
systemctl --user --no-pager --plain list-units --type=service --all \
  | grep -Ei 'opencode|stage|supervisor|v11831|llama|github-chatgpt|ingress' || true

echo
echo "=== RELEVANT PROCESSES ==="
ps -eo pid,etimes,args \
  | grep -E '[o]pencode|[s]tage_a|[s]upervisor.py|[l]lama-server|[v]llm' \
  | head -100 || true

echo
echo "=== RECENT D003/CANARY ARTIFACTS UNDER ~/AI ==="
find "$HOME/AI" -maxdepth 3 \
  \( -iname '*d003*' -o -iname '*canary*' \) \
  -printf '%TY-%Tm-%Td %TH:%TM:%TS %p\n' 2>/dev/null \
  | sort -r | head -120 || true
