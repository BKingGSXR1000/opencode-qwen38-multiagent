set -Eeuo pipefail
ROOT=/home/bking/AI/opencode-qwen38-multiagent-v2-a2-v11831
DB="$ROOT/xdg/data-v11831-a2/opencode/opencode.db"
SID=ses_f1868cee2ffefF4JpYtrzpFnYJ
cd "$ROOT"

echo "=== D011-B2 LEDGER CONTINUATIONS ==="
python3 - <<'PY'
import json
from pathlib import Path
p=Path(".opencode-v2/work/attempts.json")
d=json.loads(p.read_text())
e=d["deliverables"]["D011-B2"]
print(json.dumps({
  "count":e.get("count"),
  "sessions":e.get("sessions"),
  "failure_history":e.get("failure_history"),
  "implementation_max_step_continuations":e.get("implementation_max_step_continuations"),
  "operator_retry_attempts":e.get("operator_retry_attempts"),
},indent=2))
PY

echo
echo "=== ACTUAL SESSION PART TIMELINE ==="
python3 - "$DB" "$SID" <<'PY'
import sqlite3,json,sys
db,sid=sys.argv[1:]
con=sqlite3.connect(db)
rows=con.execute("SELECT id,time_created,data FROM part WHERE session_id=? ORDER BY time_created,id",(sid,)).fetchall()
con.close()
for i,(pid,ts,raw) in enumerate(rows):
    try:p=json.loads(raw) if raw else {}
    except Exception as e:
        print(i,ts,"BAD_JSON",repr(raw)[:200]); continue
    typ=p.get("type")
    if typ=="text":
        txt=" ".join(str(p.get("text") or "").split())
        if len(txt)>800: txt=txt[:800]+"..."
        print(f"{i:03d} {ts} TEXT {txt}")
    elif typ=="tool":
        state=p.get("state") or {}
        tool=p.get("tool")
        status=state.get("status")
        err=" ".join(str(state.get("error") or "").split())
        if len(err)>900: err=err[:900]+"..."
        print(f"{i:03d} {ts} TOOL tool={tool} status={status} error={err}")
    else:
        print(f"{i:03d} {ts} {typ} keys={sorted(p.keys())}")
PY

echo
echo "=== CURRENT PROGRESS HEAD ==="
sed -n '1,120p' .opencode-v2/work/D011-B2.progress.md 2>/dev/null || true
