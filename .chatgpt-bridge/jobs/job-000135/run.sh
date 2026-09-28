set -Eeuo pipefail
ING=/home/bking/AI/chatgpt-job-ingress/ingress.py
ROOT=/home/bking/AI/opencode-qwen38-multiagent-v2-a2-v11831
PROJECT=/home/bking/AI/a2-e2e/20260928-final-current-tree-proof2/project
DB="$ROOT/xdg/data-v11831-a2/opencode/opencode.db"
SID=ses_f1868cee2ffefF4JpYtrzpFnYJ

echo "=== INGRESS JOB/PUSH LOGIC ==="
grep -n -C 8 -E '(/job|git.*push|push.*git|subprocess|queue_job|job_ingress|userscript|WAKE_NEXT|do_POST|POST)' "$ING" | head -320 || true

echo
echo "=== SERVED USERSCRIPT SUBMIT/RETRY LOGIC ==="
grep -n -C 8 -E '(fetch\(|POST|JOB_ENDPOINT|submitted|localStorage|scan|retry|marker|AUTOMATION_JOB)' "$ING" | head -360 || true

echo
echo "=== D011-B2 LEDGER ==="
python3 - "$PROJECT" <<'PY'
import json,sys
from pathlib import Path
project=Path(sys.argv[1])
hits=list(project.rglob("attempts.json"))
print("ATTEMPTS_FILES", [str(p) for p in hits])
for p in hits:
    try:
        d=json.loads(p.read_text())
    except Exception as e:
        print("READ_ERROR",p,repr(e)); continue
    e=(d.get("deliverables") or {}).get("D011-B2")
    if not isinstance(e,dict):
        continue
    keys=("count","sessions","failure_history","implementation_max_step_continuations",
          "operator_retry_attempts","operator_retry_grants","unmaterialized_dispatch_sequence",
          "unmaterialized_dispatch_replays","split_required")
    print("LEDGER",p)
    print(json.dumps({k:e.get(k) for k in keys if k in e},indent=2))
PY

echo
echo "=== CONTRACT-CHALLENGE / MAX-STEP TIMELINE ==="
python3 - "$DB" "$SID" <<'PY'
import sqlite3,json,re,sys
db,sid=sys.argv[1:]
con=sqlite3.connect(db)
rows=con.execute(
    "SELECT id,time_created,data FROM part WHERE session_id=? ORDER BY time_created,id",
    (sid,)
).fetchall()
con.close()
guard_index=None
valid=[]
later=[]
maxsteps=[]
for i,(pid,ts,raw) in enumerate(rows):
    try:
        p=json.loads(raw) if raw else {}
    except Exception:
        continue
    typ=p.get("type")
    if typ=="tool":
        st=p.get("state") or {}
        err=str(st.get("error") or "")
        if "CONTRACT_CHALLENGE_REQUIRED" in err:
            guard_index=i
            print("GUARD",i,ts,p.get("tool")," ".join(err.split()))
        elif guard_index is not None and i>guard_index:
            later.append((i,ts,p.get("tool"),st.get("status")," ".join(err.split())[:500]))
    elif typ=="text":
        txt=str(p.get("text") or "")
        if re.search(r"(?mi)^\s*CONTRACT_CHALLENGE:\s*.{20,800}\s*$",txt):
            valid.append((i,ts," ".join(txt.split())[:1200]))
        if re.search(r"(?:maximum steps for this agent have been reached|max steps reached for this agent session)",txt,re.I):
            maxsteps.append((i,ts," ".join(txt.split())[:1800]))
        if "Contract-conflict reasoning" in txt or "CONTRACT_CHALLENGE_REQUIRED" in txt:
            print("RELEVANT_TEXT",i,ts," ".join(txt.split())[:1800])
print("VALID_CHALLENGES",valid)
print("LATER_TOOLS_AFTER_GUARD",later)
print("MAX_STEP_TEXTS",maxsteps)
PY

echo
echo "=== D011-B2 PROGRESS ==="
find "$PROJECT" -type f -name 'D011-B2.progress.md' -print -exec sh -c 'echo "--- $1"; sed -n "1,220p" "$1"' _ {} \; 2>/dev/null || true
