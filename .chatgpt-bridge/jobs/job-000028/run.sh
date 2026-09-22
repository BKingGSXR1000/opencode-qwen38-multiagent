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

echo "=== RESUME EXISTING CANARY ==="
echo "PROJECT=$P"
echo "BASE_URL=$U"
echo "ROOT_SESSION=$S"

curl -fsS -G "$U/session/status" --data-urlencode "directory=$P" >/dev/null

echo "=== CREATE PREFLIGHT PROOF ==="
python3 "$R/scripts/stage_a_preflight.py"   --project "$P"   --task-file "$T"   --base-url "$U"   --root-session "$S"   --write-proof "$PROOF"
cat "$PROOF"

echo "=== DISPATCH PRIMARY SPLITTER ==="
python3 "$R/scripts/run-stage-a-tick.py"   --project "$P"   --base-url "$U"   --root-session "$S"   --preflight-proof "$PROOF"   | tee "$B/tick-resume.json"

F="$P/.opencode-v2/work/D003-B.split-status.json"
echo "=== WAIT FOR CORRECTIVE LIFECYCLE ==="
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

echo "=== CONTROLLER EXECUTIONS ==="
cat "$P/.opencode-v2/work/stage-a-controller-executions.json" || true

echo "=== RESPONSE ARCHIVES ==="
for f in "$P"/.opencode-v2/work/D003-B.splitter-*-response-*.txt; do
  [[ -f "$f" ]] || continue
  echo "--- $f ---"
  cat "$f"
  echo
done

echo "=== PROJECT PROCESSES ==="
ps -eo pid,etimes,args | grep -F "$P" | grep -v grep || true

echo "JOB_000028_DONE"
