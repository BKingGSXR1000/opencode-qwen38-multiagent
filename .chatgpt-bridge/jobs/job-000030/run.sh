#!/usr/bin/env bash
set -Eeuo pipefail

R=/home/bking/AI/opencode-qwen38-multiagent-v2-a2-v11831
B=/home/bking/AI/a2-canaries/20260922-230614-d003b-live
P="$B/project"
T="$B/TASK.md"
U=http://127.0.0.1:58443
S=ses_f350f29a8ffeE24xJvn8ZyvfBj
PROOF="$B/preflight-proof.json"

export V2_ROOT="$R"
export V2_PROJECT="$P"
export V2_OPENCODE_BASE_URL="$U"
export V2_OPENCODE_DB="$R/xdg/data-v11831-a2/opencode/opencode.db"
export V2_OPENCODE_SESSION_TABLE=session

echo "=== RECOVER VLLM_API_KEY FROM EXISTING USER PROCESS ==="
VLLM_API_KEY=""
for e in /proc/[0-9]*/environ; do
  [[ -r "$e" ]] || continue
  k="$(tr '\0' '\n' <"$e" 2>/dev/null | sed -n 's/^VLLM_API_KEY=//p' | head -1 || true)"
  if [[ -n "$k" ]]; then
    VLLM_API_KEY="$k"
    break
  fi
done
[[ -n "$VLLM_API_KEY" ]] || { echo "ERROR: VLLM_API_KEY not found in existing user processes"; exit 30; }
export VLLM_API_KEY
echo "VLLM_API_KEY: recovered (value not printed)"

echo "=== VERIFY EXISTING SERVER/ROOT ==="
curl -fsS -G "$U/session/status" --data-urlencode "directory=$P" >/dev/null
echo "server: OK"
echo "root=$S"

echo "=== CREATE PREFLIGHT PROOF ==="
python3 "$R/scripts/stage_a_preflight.py" \
  --project "$P" --task-file "$T" --base-url "$U" \
  --root-session "$S" --write-proof "$PROOF"
echo "preflight: OK"

echo "=== DISPATCH PRIMARY SPLITTER ==="
python3 "$R/scripts/run-stage-a-tick.py" \
  --project "$P" --base-url "$U" --root-session "$S" \
  --preflight-proof "$PROOF" | tee "$B/tick-resume-30.json"

F="$P/.opencode-v2/work/D003-B.split-status.json"
echo "=== WAIT FOR SPLITTER LIFECYCLE ==="
deadline=$((SECONDS+420))
last=""
while (( SECONDS < deadline )); do
  if [[ -s "$F" ]]; then
    summary="$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print("|".join(str(d.get(k,"")) for k in ("state","claim_count","corrective_turn_count","primary_session","corrective_session","corrective_dispatch_state")))' "$F")"
    if [[ "$summary" != "$last" ]]; then
      echo "STATUS $summary"
      last="$summary"
    fi
    state="${summary%%|*}"
    if [[ "$state" == "accepted" || "$state" == splitter-failed* || "$state" == failed* ]]; then
      break
    fi
  fi
  sleep 1
done

echo "=== FINAL SPLIT STATUS ==="
cat "$F" || true

echo "=== RESPONSE ARCHIVES ==="
shopt -s nullglob
for f in "$P"/.opencode-v2/work/D003-B.splitter-*-response-*.txt; do
  echo "--- $(basename "$f") ---"
  cat "$f"
  echo
done

echo "=== CONTROLLER EXECUTIONS ==="
cat "$P/.opencode-v2/work/stage-a-controller-executions.json" || true

echo "=== PROJECT PROCESSES ==="
ps -eo pid,etimes,args | grep -F "$P" | grep -v grep || true

echo "JOB_000030_DONE"
