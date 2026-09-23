#!/usr/bin/env bash
set -Eeuo pipefail

R=/home/bking/AI/opencode-qwen38-multiagent-v2-a2-v11831
B=/home/bking/AI/a2-canaries/20260923-075653-d003b-fresh
P="$B/project"
DB="$R/xdg/data-v11831-a2/opencode/opencode.db"
ROOT=ses_f33295769ffegEcmlV29de6CP6
PRIMARY=ses_f332954c7ffe6A73XPsomErJdG
F="$P/.opencode-v2/work/D003-B.split-status.json"

echo "=== CURRENT STATUS ==="
cat "$F" || true

echo
echo "=== HTTP SESSION STATUS ==="
curl -fsS -G http://127.0.0.1:58443/session/status --data-urlencode "directory=$P" || true
echo

echo
echo "=== ROOT + CHILD SESSIONS ==="
python3 - "$DB" "$ROOT" <<'PY'
import sqlite3,sys,json
db,root=sys.argv[1:]
con=sqlite3.connect(f"file:{db}?mode=ro",uri=True,timeout=3)
con.row_factory=sqlite3.Row
rows=con.execute(
    "select id,parent_id,title,agent,model,tokens_input,tokens_output,time_created,time_updated "
    "from session where id=? or parent_id=? order by time_created",
    (root,root)
).fetchall()
print(json.dumps([dict(r) for r in rows],indent=2,default=str))
PY

echo
echo "=== ROOT MESSAGE/PART TIMELINE ==="
python3 - "$DB" "$ROOT" <<'PY'
import sqlite3,sys,json
db,root=sys.argv[1:]
con=sqlite3.connect(f"file:{db}?mode=ro",uri=True,timeout=3)
con.row_factory=sqlite3.Row
for table in ("message","part"):
    print("TABLE",table)
    rows=con.execute(f'select * from "{table}" where session_id=? order by time_created',(root,)).fetchall()
    for r in rows[-80:]:
        d=dict(r)
        raw=d.get("data")
        try:
            obj=json.loads(raw) if isinstance(raw,str) else raw
        except Exception:
            obj=raw
        print(json.dumps({
            "id":d.get("id"),
            "message_id":d.get("message_id"),
            "time_created":d.get("time_created"),
            "time_updated":d.get("time_updated"),
            "data":obj,
        },default=str)[:16000])
PY

echo
echo "=== PRIMARY MESSAGE/PART TAIL ==="
python3 - "$DB" "$PRIMARY" <<'PY'
import sqlite3,sys,json
db,sid=sys.argv[1:]
con=sqlite3.connect(f"file:{db}?mode=ro",uri=True,timeout=3)
con.row_factory=sqlite3.Row
for table in ("message","part"):
    print("TABLE",table)
    rows=con.execute(f'select * from "{table}" where session_id=? order by time_created',(sid,)).fetchall()
    for r in rows[-30:]:
        d=dict(r)
        try: obj=json.loads(d.get("data")) if isinstance(d.get("data"),str) else d.get("data")
        except Exception: obj=d.get("data")
        print(json.dumps({"id":d.get("id"),"message_id":d.get("message_id"),
                          "time_created":d.get("time_created"),"time_updated":d.get("time_updated"),
                          "data":obj},default=str)[:12000])
PY

echo
echo "=== SUPERVISOR LOG AROUND 07:57 ==="
grep -n -E '07:56:|07:57:|07:58:|07:59:|D003-B|SPLITTER_CORRECTIVE|HTTP_CONTROL_' \
  "$R/logs/supervisor-stderr.log" | tail -260 || true

echo
echo "=== SERVER LOG TAIL ==="
tail -260 "$R/logs/stage-a-server-58443-refresh.log" || true

echo
echo "=== PLUGIN/TRANSPORT LOG CANDIDATES ==="
find "$P/.opencode-v2" "$R/logs" -type f \
  \( -iname '*transport*' -o -iname '*plugin*' -o -iname '*hook*' \) \
  -printf '%TY-%Tm-%Td %TH:%TM:%TS %s %p\n' 2>/dev/null | sort | tail -100 || true

echo
echo "=== CORRECTIVE PROMPT EVIDENCE ==="
grep -RIn -E 'SPLITTER_CORRECTIVE_ORDINAL|Correct bounded split|fba94927a64d3c3e' \
  "$P/.opencode-v2" "$R/logs" 2>/dev/null | tail -160 || true

echo
echo "JOB_000042_OK"
