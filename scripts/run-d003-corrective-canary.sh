#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
FAULT_PORT="${FAULT_PORT:-18034}"
OPENCODE_PORT="${OPENCODE_PORT:-58449}"
BASE="${CANARY_BASE:-$HOME/AI/a2-canaries/$(date +%Y%m%d-%H%M%S)-d003b-corrective}"
PROJECT="$BASE/project"
TASK="$BASE/TASK.md"
URL="http://127.0.0.1:$OPENCODE_PORT"
FAULT_URL="http://127.0.0.1:$FAULT_PORT/v1"
PROOF="$BASE/preflight-proof.json"
FAULT_PID=""
SERVER_PID=""
SUPERVISOR_PID=""
NOOP_PID=""

stop_pid(){
  local pid="$1"
  [[ "$pid" =~ ^[0-9]+$ ]] || return 0
  kill "$pid" 2>/dev/null || return 0
  for _ in {1..20}; do
    kill -0 "$pid" 2>/dev/null || break
    sleep .1
  done
  kill -0 "$pid" 2>/dev/null && kill -KILL "$pid" 2>/dev/null || true
  wait "$pid" 2>/dev/null || true
}

cleanup(){
  set +e
  stop_pid "$SUPERVISOR_PID"
  stop_pid "$SERVER_PID"
  stop_pid "$FAULT_PID"
  stop_pid "$NOOP_PID"
}
trap cleanup EXIT INT TERM

if [[ -s /home/bking/AI/qwen38-27b-rtx3090-syv/api_key.txt ]]; then
  export VLLM_API_KEY="$(tr -d '\r\n' < /home/bking/AI/qwen38-27b-rtx3090-syv/api_key.txt)"
else
  export VLLM_API_KEY=local-multiagent-test
fi
curl -fsS --max-time 3 -H "Authorization: Bearer $VLLM_API_KEY"   http://127.0.0.1:18030/v1/models >/dev/null || {
  echo "ERROR: vLLM backend is not ready on 18030" >&2
  exit 20
}
curl -fsS --max-time 3 http://127.0.0.1:18033/__proxy_health >/dev/null || {
  echo "ERROR: bounded V2 proxy is not ready on 18033" >&2
  exit 21
}
for port in "$FAULT_PORT" "$OPENCODE_PORT"; do
  if ss -ltn "sport = :$port" 2>/dev/null | grep -q LISTEN; then
    echo "ERROR: required canary port $port is already in use" >&2
    exit 22
  fi
done

mkdir -p "$PROJECT" "$ROOT/logs"
printf '%s\n'   'Deterministic malformed-primary D003-B corrective splitter canary.'   > "$TASK"

python3 "$ROOT/scripts/create-d003-splitter-canary.py"   --project "$PROJECT" --task-file "$TASK" --d003-b-equivalent

python3 "$ROOT/scripts/d003-splitter-fault-proxy.py"   --listen "$FAULT_PORT" --target http://127.0.0.1:18033 --greedy-after-fault   >"$BASE/fault-proxy.log" 2>&1 &
FAULT_PID=$!
if ! curl -fsS --max-time 2 http://127.0.0.1:57182/v1/models >/dev/null 2>&1; then
  "$ROOT/scripts/run-a2-v11831-noop.sh" >"$BASE/noop.log" 2>&1 &
  NOOP_PID=$!
fi
for _ in {1..40}; do
  curl -fsS --max-time 2 http://127.0.0.1:57182/v1/models >/dev/null 2>&1 && break
  sleep .25
done
curl -fsS http://127.0.0.1:57182/v1/models >/dev/null

"$ROOT/scripts/run-a2-v11831-canary-server.sh"   "$PROJECT" "$OPENCODE_PORT" "$FAULT_URL"   >"$BASE/opencode.log" 2>&1 &
SERVER_PID=$!
for _ in {1..120}; do
  code="$(curl -sS --max-time 1 -o /dev/null -w '%{http_code}' -G "$URL/session/status"     --data-urlencode "directory=$PROJECT" 2>/dev/null || true)"
  [[ "$code" == 200 ]] && break
  sleep .25
done
curl -fsS --max-time 2 -G "$URL/session/status"   --data-urlencode "directory=$PROJECT" >/dev/null

SUPERVISOR_PID="$("$ROOT/scripts/start-a2-v11831-supervisor.sh" "$PROJECT" "$URL")"
for _ in {1..120}; do
  [[ -s "$PROJECT/.opencode-v2/query/deterministic-shadow.json" ]] && break
  sleep .25
done
[[ -s "$PROJECT/.opencode-v2/query/deterministic-shadow.json" ]]
python3 "$ROOT/scripts/create-stage-a-root.py"   --project "$PROJECT" --base-url "$URL" > "$BASE/root.json"
ROOT_SESSION="$(python3 -c   'import json,sys; print(json.load(open(sys.argv[1]))["root_session"])'   "$BASE/root.json")"

python3 "$ROOT/scripts/stage_a_preflight.py"   --project "$PROJECT" --task-file "$TASK" --base-url "$URL"   --root-session "$ROOT_SESSION" --write-proof "$PROOF" >/dev/null
python3 "$ROOT/scripts/run-stage-a-tick.py"   --project "$PROJECT" --base-url "$URL"   --root-session "$ROOT_SESSION" --preflight-proof "$PROOF"   > "$BASE/tick.json"

STATUS="$PROJECT/.opencode-v2/work/D003-B.split-status.json"
deadline=$((SECONDS+180))
while (( SECONDS < deadline )); do
  if [[ -s "$STATUS" ]]; then
    state="$(python3 -c       'import json,sys; print(json.load(open(sys.argv[1])).get("state",""))'       "$STATUS")"
    [[ "$state" == accepted || "$state" == split-retryable ||        "$state" == splitter-failed* || "$state" == failed* ]] && break
  fi
  sleep 1
done
[[ -s "$STATUS" ]] || { echo "ERROR: split status never appeared" >&2; exit 30; }
python3 - "$PROJECT" "$ROOT_SESSION" "$ROOT" <<'PY'
import json,sqlite3,sys
from pathlib import Path
project=Path(sys.argv[1]); root_session=sys.argv[2]; root=Path(sys.argv[3])
work=project/".opencode-v2"/"work"
status=json.loads((work/"D003-B.split-status.json").read_text())
assert status.get("state")=="accepted",status
assert status.get("claim_count")==1,status
assert status.get("corrective_turn_count")==1,status
assert status.get("corrective_session"),status
assert status.get("corrective_dispatch_state")=="native-child-completed",status
primary=(work/"D003-B.splitter-primary-response-1.txt").read_text().strip()
assert primary=="CANARY_FIRST_RESPONSE_INVALID",primary
assert status.get("children")==["D003-B1","D003-B2"],status

db=root/"xdg"/"data-v11831-a2"/"opencode"/"opencode.db"
con=sqlite3.connect(f"file:{db}?mode=ro",uri=True,timeout=3)
rows=con.execute(
    "select id from session where parent_id=? order by time_created",
    (root_session,),
).fetchall()
assert len(rows)==2,rows
sids=[root_session]+[r[0] for r in rows]
phrase="Summarize the task tool output above and continue with your task."
count=0
for table in ("message","part"):
    cols=[r[1] for r in con.execute(f'pragma table_info("{table}")')]
    for sid in sids:
        q=" OR ".join(f'cast("{c}" as text) like ?' for c in cols)
        args=[f"%{phrase}%"]*len(cols)
        count+=con.execute(
            f'select count(*) from "{table}" where session_id=? and ({q})',
            [sid,*args],
        ).fetchone()[0]
assert count==0,count
print("D003_CORRECTIVE_CANARY_PASS")
print("ROOT_SESSION",root_session)
print("CHILD_COUNT",len(rows))
print("SYNTHETIC_CONTINUATION_COUNT",count)
PY

echo "CANARY_BASE=$BASE"
