set -u
ROOT=/home/bking/AI/opencode-qwen38-multiagent-v2-a2-v11831
DB="$ROOT/xdg/data-v11831-a2/opencode/opencode.db"
SID=ses_f1868cee2ffefF4JpYtrzpFnYJ
cd "$ROOT" || exit 2

echo "=== D011-B2 LEDGER CANDIDATES ==="
python3 - "$ROOT" <<'PY'
import json,sys
from pathlib import Path
root=Path(sys.argv[1])
found=0
for p in root.rglob("attempts.json"):
    try:
        d=json.loads(p.read_text())
    except Exception:
        continue
    e=(d.get("deliverables") or {}).get("D011-B2")
    if not isinstance(e,dict):
        continue
    found+=1
    print("LEDGER",p)
    keys=("count","sessions","failure_history","implementation_max_step_continuations",
          "operator_retry_attempts","operator_retry_grants","unmaterialized_dispatch_sequence",
          "unmaterialized_dispatch_replays","split_required")
    print(json.dumps({k:e.get(k) for k in keys if k in e},indent=2))
print("MATCHING_LEDGERS",found)
PY

echo
echo "=== ACTUAL SESSION PART TIMELINE ==="
python3 - "$DB" "$SID" <<'PY'
import sqlite3,json,sys
db,id=sys.argv[1:]
con=sqlite3.connect(db)
rows=con.execute(
    "SELECT id,time_created,data FROM part WHERE session_id=? ORDER BY time_created,id",
    (sid,)
).fetchall()
con.close()
print("PARTS",len(rows))
for i,(pid,ts,raw) in enumerate(rows):
    try:
        p=json.loads(raw) if raw else {}
    except Exception:
        print(f"{i:03d} {ts} BAD_JSON")
        continue
    typ=p.get("type")
    if typ=="text":
        txt=" ".join(str(p.get("text") or "").split())
        if len(txt)>1800: txt=txt[:1800]+"..."
        print(f"{i:03d} {ts} TEXT {txt}")
    elif typ=="tool":
        st=p.get("state") or {}
        tool=p.get("tool")
        status=st.get("status")
        err=" ".join(str(st.get("error") or "").split())
        out=" ".join(str(st.get("output") or "").split())
        if len(err)>1600: err=err[:1600]+"..."
        if len(out)>800: out=out[:800]+"..."
        print(f"{i:03d} {ts} TOOL tool={tool} status={status} error={err} output={out}")
    else:
        print(f"{i:03d} {ts} {typ} keys={sorted(p.keys())}")
PY

echo
echo "=== D011-B2 PROGRESS FILES ==="
find "$ROOT" -type f -name 'D011-B2.progress.md' -print -exec sh -c 'echo "--- $1"; sed -n "1,220p" "$1"' _ {} \; 2>/dev/null:| true
