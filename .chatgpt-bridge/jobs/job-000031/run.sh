#!/usr/bin/env bash
set -Eeuo pipefail

R=/home/bking/AI/opencode-qwen38-multiagent-v2-a2-v11831
P=/home/bking/AI/a2-canaries/20260922-230614-d003b-live/project
PORT=58443
URL="http://127.0.0.1:$PORT"
W="$P/.opencode-v2/work"

echo "=== D003-B CORRECTIVE DISPATCH FAILURE DIAGNOSIS ==="
date --iso-8601=seconds

echo
echo "=== CURRENT SPLIT STATUS ==="
cat "$W/D003-B.split-status.json" || true

echo
echo "=== CORRECTIVE REJECT ARCHIVE ==="
cat "$W/D003-B.split-proposal.corrective-rejected-1.json" || true

echo
echo "=== SPLIT REQUEST / CANARY STIMULUS ==="
python3 - "$W/D003-B.split-request.json" <<'PY'
import json,sys
d=json.load(open(sys.argv[1]))
print(json.dumps({
  "canary_first_turn_instruction": d.get("canary_first_turn_instruction"),
  "parent_id": d.get("parent_id"),
  "generation": d.get("generation"),
  "protocol": d.get("protocol"),
}, indent=2))
PY

echo
echo "=== PRIMARY RESPONSE ==="
cat "$W/D003-B.splitter-primary-response-1.txt" || true

echo
echo "=== SERVER 58443 PROCESS / SOCKET ==="
ps -eo pid,ppid,lstart,etime,stat,args | grep -E "opencode.*serve.*58443|58443" | grep -v grep || true
ss -ltnp 2>/dev/null | grep ":$PORT" || true

echo
echo "=== SERVER STATUS NOW ==="
curl -sv -G "$URL/session/status" --data-urlencode "directory=$P" -o /tmp/a2-30-status-body.txt 2>/tmp/a2-30-status-curl.txt || true
cat /tmp/a2-30-status-curl.txt
echo "--- body ---"
cat /tmp/a2-30-status-body.txt 2>/dev/null || true

echo
echo "=== SERVER LOG CANDIDATES ==="
find "$R/logs" -maxdepth 1 -type f \( -name "*58443*" -o -name "*server*" -o -name "*opencode*" \) -printf '%TY-%Tm-%Td %TH:%TM:%TS %10s %p\n' | sort -r | head -40 || true
for f in "$R/logs"/*58443*; do
  [[ -f "$f" ]] || continue
  echo "--- $f ---"
  tail -200 "$f" || true
done

echo
echo "=== SUPERVISOR PROCESS / ENV ==="
SUP_PID="$(cat "$W/supervisor.pid" 2>/dev/null || true)"
echo "SUP_PID=$SUP_PID"
if [[ "$SUP_PID" =~ ^[0-9]+$ ]] && [[ -r "/proc/$SUP_PID/environ" ]]; then
  tr '\0' '\n' < "/proc/$SUP_PID/environ" | grep -E '^(V2_PROJECT|V2_OPENCODE_BASE_URL|V2_OPENCODE_DB|V2_OPENCODE_SESSION_TABLE)=' || true
  echo "--- supervisor fds (db/socket-related) ---"
  ls -l "/proc/$SUP_PID/fd" 2>/dev/null | grep -E 'opencode|socket|deleted' | head -80 || true
fi

echo
echo "=== OPENCODE DB ==="
DB="$R/xdg/data-v11831-a2/opencode/opencode.db"
ls -l "$DB" "$DB-wal" "$DB-shm" 2>/dev/null || true
python3 - "$DB" <<'PY'
import sqlite3,sys,os
db=sys.argv[1]
print("exists", os.path.exists(db), "size", os.path.getsize(db) if os.path.exists(db) else None)
try:
    con=sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=3)
    print("quick_check", con.execute("pragma quick_check").fetchone()[0])
    print("sessions", con.execute('select count(*) from session').fetchone()[0])
    con.close()
except Exception as e:
    print("DB_ERROR", repr(e))
PY

echo
echo "=== SOURCE: HTTP_POLL_ERROR / HTTP CONNECTIVITY ==="
grep -n -C 12 -E 'HTTP_POLL_ERROR|http-not-connected|HTTP_CONTROL_CONNECTED|http_connected|HTTP_CONNECTED' "$R/scripts/supervisor.py" || true

echo
echo "=== SOURCE: CORRECTIVE DISPATCH ==="
grep -n -C 18 -E 'def dispatch_splitter_corrective_turn|corrective turn dispatch failed|corrective_dispatch_state|begin_splitter_corrective_turn' "$R/scripts/supervisor.py" || true

echo
echo "=== SUPERVISOR EVENTS AROUND FAILURE ==="
grep -n -E '23:16:|D003-B|HTTP_POLL_ERROR|CORRECTIVE|SPLITTER_' "$R/logs/supervisor-stderr.log" | tail -180 || true

echo
echo "=== NATIVE PRIMARY SESSION ==="
python3 - "$DB" <<'PY'
import sqlite3,json,sys
db=sys.argv[1]
sid="ses_f35058c64ffe0E25TxVTWnBNqe"
con=sqlite3.connect(f"file:{db}?mode=ro",uri=True,timeout=3)
con.row_factory=sqlite3.Row
row=con.execute("select * from session where id=?",(sid,)).fetchone()
print(json.dumps(dict(row) if row else None,indent=2,default=str))
for table in ("message","part"):
    try:
        cols=[r[1] for r in con.execute(f'pragma table_info("{table}")')]
        print("TABLE",table,"COLS",cols)
        for r in con.execute(f'select * from "{table}" where ' + ' OR '.join(f'cast("{c}" as text) like ?' for c in cols),
                             tuple(f"%{sid}%" for _ in cols)).fetchall():
            s=json.dumps(dict(r),default=str)
            if "CANARY_FIRST_RESPONSE_INVALID" in s or "task-splitter" in s or sid in s:
                print(table, s[:12000])
    except Exception as e:
        print(table,"ERROR",repr(e))
PY

echo
echo "JOB_000031_OK"
