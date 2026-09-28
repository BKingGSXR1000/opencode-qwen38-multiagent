set -euo pipefail

REPO=/home/bking/AI/opencode-qwen38-multiagent-v2-a2-v11831
PROJECT=/home/bking/AI/a2-e2e/20260928-final-current-tree-proof2/project
BASE=/home/bking/AI/a2-e2e/20260928-final-current-tree-proof2
cd "$REPO"

export CUDA_HOME=/usr/local/cuda-12.9
export PATH="$CUDA_HOME/bin:$PATH"
export LD_LIBRARY_PATH="$CUDA_HOME/lib64:${LD_LIBRARY_PATH:-}"

echo "=== PRE-RECOVERY CONTROLLER ==="
PYTHONPATH=scripts python3 scripts/control-guard.py --project "$PROJECT" status || true

echo
echo "=== PRE-RECOVERY D011-B2 LEDGER/PROGRESS ==="
python3 - <<'PY'
from pathlib import Path
p=Path("/home/bking/AI/a2-e2e/20260928-final-current-tree-proof2/project/.opencode-v2")
for name in ["control-state.json","attempt-ledger.json","D011-B2.progress.md","D011-B2.ready"]:
    q=p/name
    print(f"--- {name} {'PRESENT' if q.exists() else 'MISSING'} ---")
    if q.exists():
        txt=q.read_text(errors="replace")
        if len(txt)>12000: txt=txt[-12000:]
        print(txt)
PY

echo
echo "=== AUDITED SAME-ATTEMPT MAX-STEP RECOVERY ==="
PYTHONPATH=scripts python3 - <<'PY'
import json, sys
from pathlib import Path
sys.path.insert(0, "scripts")
from supervisor import Supervisor

project = Path("/home/bking/AI/a2-e2e/20260928-final-current-tree-proof2/project")
diagnostic = (
    'Exact Verify .opencode-v2/bin/run-checks remains 4/5: only '
    'readme-required-strings fails because TEST_CHECKS.json requires the literal '
    '"standard library" while the completed README does not contain that phrase. '
    'D011-B2 owns only .opencode-v2/TEST_CHECKS.json. The guard already required '
    'a single-line CONTRACT_CHALLENGE after the second exact Verify failure; do not '
    'mutate README or any unowned artifact.'
)

sup = Supervisor(project)
try:
    out = sup.recover_implementation_max_step_continuation(
        "D011-B2",
        diagnostic=diagnostic,
    )
    print("RECOVERY_RESULT", out)
except Exception as e:
    print("RECOVERY_REFUSED", type(e).__name__, str(e))
PY

echo
echo "=== ENSURE DRIVER ==="
if pgrep -af "drive-stage-a-run.py.*20260928-final-current-tree-proof2" >/dev/null; then
    echo "driver already running"
else
    echo "driver not running; starting existing proof2 driver"
    nohup env PYTHONPATH=scripts python3 scripts/drive-stage-a-run.py \
      --project "$PROJECT" \
      --task "$BASE/task.md" \
      --server-url http://127.0.0.1:58508 \
      --root-session ses_f190c8a0fffeWkDjwEFVICi474 \
      >>"$BASE/driver.log" 2>&1 &
    echo "driver pid=$!"
fi

echo
echo "=== POLL DURABLE STATE ==="
for i in $(seq 1 90); do
    sleep 2
    phase=$(python3 - <<'PY'
import json
from pathlib import Path
p=Path("/home/bking/AI/a2-e2e/20260928-final-current-tree-proof2/project/.opencode-v2/control-state.json")
try:
    d=json.loads(p.read_text())
    print(d.get("resume_phase") or d.get("phase") or "")
except Exception:
    print("")
PY
)
    ready="no"
    [ -f "$PROJECT/.opencode-v2/D011-B2.ready" ] && ready="yes"
    printf 'poll=%02d phase=%s D011-B2.ready=%s\n' "$i" "$phase" "$ready"
    if [ "$ready" = yes ]; then break; fi
    if [ "$phase" != "execution-blocked" ] && [ -n "$phase" ]; then
        # allow driver to make some progress after recovery
        if [ "$i" -ge 10 ]; then break; fi
    fi
done

echo
echo "=== POST CONTROLLER ==="
PYTHONPATH=scripts python3 scripts/control-guard.py --project "$PROJECT" status || true

echo
echo "=== D011-B2 CURRENT ARTIFACTS ==="
python3 - <<'PY'
from pathlib import Path
p=Path("/home/bking/AI/a2-e2e/20260928-final-current-tree-proof2/project/.opencode-v2")
for name in ["D011-B2.progress.md","D011-B2.ready","attempt-ledger.json"]:
    q=p/name
    print(f"--- {name} {'PRESENT' if q.exists() else 'MISSING'} ---")
    if q.exists():
        txt=q.read_text(errors="replace")
        if len(txt)>16000: txt=txt[-16000:]
        print(txt)
PY

echo
echo "=== DRIVER TAIL ==="
tail -n 180 "$BASE/driver.log" || true
