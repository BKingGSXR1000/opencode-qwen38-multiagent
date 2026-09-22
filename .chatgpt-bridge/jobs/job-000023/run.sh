#!/usr/bin/env bash
set -Eeuo pipefail

R=/home/bking/AI/opencode-qwen38-multiagent-v2-a2-v11831
B="$HOME/AI/a2-canaries/$(date +%Y%m%d-%H%M%S)-d003b-live"
P="$B/project"
T="$B/TASK.md"
mkdir -p "$P"
printf '%s\n' 'Disposable live D003-B corrective splitter canary.' > "$T"

U=""
for port in 58443 58441 58442 49381 34169 58277 45211 35475 34489; do
  u="http://127.0.0.1:$port"
  code="$(curl -sS -o /dev/null -w '%{http_code}' -G "$u/session/status" --data-urlencode "directory=$P" 2>/dev/null || true)"
  if [[ "$code" == 200 ]]; then U="$u"; break; fi
done
[[ -n "$U" ]] || { echo "ERROR no healthy A2 server"; exit 20; }

export V2_ROOT="$R"
export V2_PROJECT="$P"
export V2_OPENCODE_BASE_URL="$U"
export V2_OPENCODE_DB="$R/xdg/data-v11831-a2/opencode/opencode.db"
export V2_OPENCODE_SESSION_TABLE=session

echo "CANARY_BASE=$B"
echo "BASE_URL=$U"

python3 "$R/scripts/create-d003-splitter-canary.py" \
  --project "$P" --task-file "$T" \
  --d003-b-equivalent --first-turn-invalid

"$R/scripts/start-a2-v11831-supervisor.sh" "$P" "$U"

for _ in $(seq 1 120); do
  [[ -s "$P/.opencode-v2/query/deterministic-shadow.json" ]] && break
  sleep .25
done

python3 "$R/scripts/create-stage-a-root.py" --project "$P" --base-url "$U" > "$B/root.json"
S="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["root_session"])' "$B/root.json")"
echo "ROOT_SESSION=$S"

python3 "$R/scripts/run-stage-a-tick.py" \
  --project "$P" --base-url "$U" --root-session "$S" > "$B/tick.json"
cat "$B/tick.json"

F="$P/.opencode-v2/work/D003-B.split-status.json"
for _ in $(seq 1 360); do
  if [[ -s "$F" ]]; then
    state="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("state",""))' "$F")"
    [[ "$state" == accepted || "$state" == splitter-failed* || "$state" == failed* ]] && break
  fi
  sleep 1
done

echo "=== SPLIT STATUS ==="
cat "$F" || true
echo "=== EXECUTIONS ==="
cat "$P/.opencode-v2/work/stage-a-controller-executions.json" || true
echo "=== RESPONSES ==="
for f in "$P"/.opencode-v2/work/D003-B.splitter-*-response-*.txt; do
  [[ -f "$f" ]] || continue
  echo "--- $f ---"
  cat "$f"
done
echo "CANARY_BASE=$B"
echo "JOB_000023_DONE"
