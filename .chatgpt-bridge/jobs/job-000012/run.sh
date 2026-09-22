#!/usr/bin/env bash
set -Eeuo pipefail

REPO=/home/bking/AI/opencode-qwen38-multiagent-v2-a2-v11831
PROJ=/tmp/a2-d003b-corrective-canary.oTDlKK/project

echo "=== TARGET ==="
echo "repo=$REPO"
echo "project=$PROJ"
echo "branch=$(git -C "$REPO" branch --show-current)"
echo "head=$(git -C "$REPO" rev-parse HEAD)"

echo
echo "=== EXISTING CORRECTIVE CANARY PROCESS ==="
ps -eo pid,etimes,args | grep -F "$PROJ" | grep -v grep || true

echo
echo "=== EXISTING PROJECT TOP LEVEL ==="
if [[ -d "$PROJ" ]]; then
  find "$PROJ" -maxdepth 3 -type f -printf '%TY-%Tm-%Td %TH:%TM:%TS %p\n' 2>/dev/null | sort -r | head -120
else
  echo "PROJECT_MISSING"
fi

echo
echo "=== SPLITTER/CORRECTIVE STATE FILES ==="
if [[ -d "$PROJ" ]]; then
  find "$PROJ" -type f \
    \( -iname '*split*' -o -iname '*status*' -o -iname '*control*' -o -iname '*ledger*' -o -iname '*supervisor*log*' \) \
    -print 2>/dev/null | sort | head -160
fi

echo
echo "=== RELEVANT CONTENT FROM SMALL STATE FILES ==="
if [[ -d "$PROJ" ]]; then
  while IFS= read -r f; do
    [[ -f "$f" ]] || continue
    size=$(stat -c %s "$f" 2>/dev/null || echo 99999999)
    if [[ "$size" -le 200000 ]]; then
      echo
      echo "--- $f ($size bytes) ---"
      grep -nEi 'D003|splitter|corrective|claim_count|corrective_turn_count|session|dispatch|accepted|invalid|proposal|synthetic|continuation|completion_pending' "$f" 2>/dev/null | tail -120 || true
    fi
  done < <(
    find "$PROJ" -type f \
      \( -iname '*.json' -o -iname '*.jsonl' -o -iname '*.log' -o -iname '*.txt' \) \
      -print 2>/dev/null | sort
  )
fi

echo
echo "=== CANARY CREATOR CONTRACT ==="
grep -nE 'first-turn-invalid|d003-b-equivalent|CANARY_FIRST_RESPONSE_INVALID|canary_first_turn_instruction|argparse|add_argument' \
  "$REPO/scripts/create-d003-splitter-canary.py" || true

echo
echo "=== SAVED V5 DIAGNOSTIC SCRIPT ==="
if [[ -f "$HOME/AI/diagnose-a2-d003b-canary-v5.sh" ]]; then
  sed -n '1,320p' "$HOME/AI/diagnose-a2-d003b-canary-v5.sh"
else
  echo "MISSING"
fi

echo
echo "=== REGRESSION TEST ==="
cd "$REPO"
python3 scripts/test_transport_root_no_continuation.py

echo
echo "JOB_000012_OK"
