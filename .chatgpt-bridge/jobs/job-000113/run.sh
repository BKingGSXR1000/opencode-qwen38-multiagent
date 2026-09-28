set -Eeuo pipefail
RUNNER=/home/bking/AI/github-chatgpt-runner/runner.py
REPO=/home/bking/AI/opencode-qwen38-multiagent-v2-a2-v11831
PROJECT=/home/bking/AI/a2-e2e/20260928-final-current-tree-proof2/project

echo "=== AUTHORITATIVE WOKEN PATH ==="
python3 - "$RUNNER" <<'PY'
import importlib.util, sys
from pathlib import Path

runner_path = Path(sys.argv[1])
spec = importlib.util.spec_from_file_location("chatgpt_runner_probe", runner_path)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

woken = Path(mod.WOKEN)
woken.mkdir(parents=True, exist_ok=True)
print("WOKEN=", woken)

for n in range(102, 113):
    job = f"job-{n:06d}"
    p = mod.woken_path(job)
    if not p.exists():
        p.write_text("superseded-after-wake-repair\n")
    print(job, "marked=", p.exists(), "path=", p)
PY

echo
echo "=== D011-B2 LAST SESSION RELEVANT PARTS ==="
cd "$REPO"
PYTHONPATH=scripts python3 - "$PROJECT" <<'PY'
import sys, sqlite3, json
from pathlib import Path

project = Path(sys.argv[1])
attempts = json.loads((project/'.opencode-v2/work/attempts.json').read_text())
sid = attempts['deliverables']['D011-B2']['sessions'][-1]
print("sid=", sid)

db = Path('/home/bking/AI/opencode-qwen38-multiagent-v2-a2-v11831/xdg/data-v11831-a2/opencode/opencode.db')
con = sqlite3.connect(db)
rows = con.execute(
    "SELECT id,time_created,data FROM part WHERE session_id=? ORDER BY time_created,id",
    (sid,)
).fetchall()

needles = (
    'CONTRACT_CHALLENGE_REQUIRED',
    'EARLY_WRITE_IMPLEMENTATION_CONTRACT_CHALLENGE_REQUIRED',
    'next_action=return-single-line-CONTRACT_CHALLENGE',
    'no_more_tools=true',
    'Maximum steps for this agent have been reached',
    'max steps reached for this agent session',
)
found = 0
for rid, ts, data in rows:
    try:
        obj = json.loads(data) if data else {}
    except Exception:
        continue
    blob = json.dumps(obj, ensure_ascii=False)
    if any(n in blob for n in needles):
        found += 1
        print(f"--- {rid} {ts} type={obj.get('type')} ---")
        print(blob[:12000])
print("matched_parts=", found)
con.close()
PY
