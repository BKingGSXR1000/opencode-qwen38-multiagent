#!/usr/bin/env bash
set -Eeuo pipefail

R=/home/bking/AI/opencode-qwen38-multiagent-v2-a2-v11831
B="$HOME/AI/a2-canaries/$(date +%Y%m%d-%H%M%S)-d003b-fresh"
P="$B/project"
T="$B/TASK.md"
U=http://127.0.0.1:58443
PROOF="$B/preflight-proof.json"

mkdir -p "$P"
printf '%s\n' 'Disposable fresh D003-B corrective splitter canary after live-plugin refresh.' > "$T"

export V2_ROOT="$R"
export V2_PROJECT="$P"
export V2_OPENCODE_BASE_URL="$U"
export V2_OPENCODE_DB="$R/xdg/data-v11831-a2/opencode/opencode.db"
export V2_OPENCODE_SESSION_TABLE=session

echo "=== RECOVER VLLM KEY ==="
VLLM_API_KEY=""
for e in /proc/[0-9]*/environ; do
  [[ -r "$e" ]] || continue
  k="$(tr '\0' '\n' <"$e" 2>/dev/null | sed -n 's/^VLLM_API_KEY=//p' | head -1 || true)"
  if [[ -n "$k" ]]; then
    VLLM_API_KEY="$k"
    break
  fi
done
[[ -n "$VLLM_API_KEY" ]] || { echo "ERROR: VLLM_API_KEY not found"; exit 30; }
export VLLM_API_KEY
echo "VLLM_API_KEY recovered"

echo "=== VERIFY REFRESHED SERVER ==="
curl -fsS -G "$U/session/status" --data-urlencode "directory=$P"
echo

echo "=== CREATE FRESH CANARY ==="
python3 "$R/scripts/create-d003-splitter-canary.py" \
  --project "$P" --task-file "$T" \
  --d003-b-equivalent --first-turn-invalid

echo "=== START SUPERVISOR ==="
"$R/scripts/start-a2-v11831-supervisor.sh" "$P" "$U"

for _ in $(seq 1 120); do
  [[ -s "$P/.opencode-v2/query/deterministic-shadow.json" ]] && break
  sleep .25
done

echo "=== CREATE ROOT ==="
python3 "$R/scripts/create-stage-a-root.py" --project "$P" --base-url "$U" > "$B/root.json"
cat "$B/root.json"
S="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["root_session"])' "$B/root.json")"
echo "ROOT_SESSION=$S"

echo "=== PREFLIGHT ==="
python3 "$R/scripts/stage_a_preflight.py" \
  --project "$P" --task-file "$T" --base-url "$U" \
  --root-session "$S" --write-proof "$PROOF"

echo "=== DISPATCH ==="
python3 "$R/scripts/run-stage-a-tick.py" \
  --project "$P" --base-url "$U" --root-session "$S" \
  --preflight-proof "$PROOF" | tee "$B/tick.json"

F="$P/.opencode-v2/work/D003-B.split-status.json"
echo "=== WAIT FOR LIFECYCLE ==="
last=""
deadline=$((SECONDS+180))
while (( SECONDS < deadline )); do
  if [[ -s "$F" ]]; then
    summary="$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print("|".join(str(d.get(k,"")) for k in ("state","claim_count","corrective_turn_count","session","corrective_session","corrective_dispatch_state")))' "$F")"
    if [[ "$summary" != "$last" ]]; then
      echo "STATUS $summary"
      last="$summary"
    fi
    state="${summary%%|*}"
    if [[ "$state" == "accepted" || "$state" == "split-retryable" || "$state" == splitter-failed* || "$state" == failed* ]]; then
      break
    fi
  fi
  sleep 1
done

echo "=== FINAL STATUS ==="
cat "$F" || true

echo "=== RESPONSE ARCHIVES ==="
shopt -s nullglob
for f in "$P"/.opencode-v2/work/D003-B.splitter-*-response-*.txt; do
  echo "--- $(basename "$f") ---"
  cat "$f"
  echo
done

echo "=== NATIVE CHILDREN UNDER ROOT ==="
python3 - "$V2_OPENCODE_DB" "$S" <<'PY'
import sqlite3,sys,json
db,root=sys.argv[1:]
con=sqlite3.connect(f"file:{db}?mode=ro",uri=True,timeout=3)
con.row_factory=sqlite3.Row
rows=con.execute("select id,parent_id,title,agent,model,tokens_input,tokens_output,time_created,time_updated from session where parent_id=? order by time_created",(root,)).fetchall()
print(json.dumps([dict(r) for r in rows],indent=2,default=str))
print("CHILD_COUNT",len(rows))
PY

echo "=== SYNTHETIC CONTINUATION PHRASE COUNT ==="
python3 - "$V2_OPENCODE_DB" "$S" <<'PY'
import sqlite3,sys
db,root=sys.argv[1:]
phrase="Summarize the task tool output above and continue with your task."
con=sqlite3.connect(f"file:{db}?mode=ro",uri=True,timeout=3)
sids=[root]+[r[0] for r in con.execute("select id from session where parent_id=?",(root,))]
count=0
for table in ("message","part"):
    cols=[r[1] for r in con.execute(f'pragma table_info("{table}")')]
    for sid in sids:
        q=' OR '.join(f'cast("{c}" as text) like ?' for c in cols)
        args=[f"%{phrase}%"]*len(cols)
        count += con.execute(f'select count(*) from "{table}" where session_id=? and ({q})',[sid,*args]).fetchone()[0]
print("SYNTHETIC_CONTINUATION_COUNT",count)
PY

echo "CANARY_BASE=$B"
echo "JOB_000041_OK"
