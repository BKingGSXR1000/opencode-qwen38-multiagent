set -Eeuo pipefail
QUEUE=/home/bking/AI/chatgpt-job-ingress/wake-queue
mkdir -p "$QUEUE"

echo "=== BEFORE ==="
python3 - "$QUEUE" <<'PY'
import json, re, sys
from pathlib import Path
q=Path(sys.argv[1])
for p in sorted(q.glob("*.json")):
    try:
        obj=json.loads(p.read_text())
    except Exception:
        print("BAD",p.name)
        continue
    print(p.name, obj.get("message",""))
PY

python3 - "$QUEUE" <<'PY'
import json, re, sys
from pathlib import Path
q=Path(sys.argv[1])
removed=[]
kept=[]
for p in sorted(q.glob("*.json")):
    try:
        obj=json.loads(p.read_text())
    except Exception:
        kept.append(p.name)
        continue
    msg=str(obj.get("message",""))
    m=re.search(r'\bjob-(\d{6})\b',msg)
    if m and int(m.group(1)) <= 123:
        p.unlink(missing_ok=True)
        removed.append((p.name,msg))
    else:
        kept.append((p.name,msg))
print("REMOVED",len(removed))
for x in removed: print(" -",x[0],x[1])
print("KEPT",len(kept))
for x in kept: print(" +",x)
PY

echo "=== AFTER ==="
find "$QUEUE" -maxdepth 1 -type f -name '*.json' -printf '%f\n' | sort || true
