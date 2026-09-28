set -Eeuo pipefail
REPO=/home/bking/AI/opencode-qwen38-multiagent-v2-a2-v11831
BASE=/home/bking/AI/a2-e2e/20260928-final-current-tree-proof2
PROJECT="$BASE/project"
cd "$REPO"

export CUDA_HOME=/usr/local/cuda-12.9
export PATH="$CUDA_HOME/bin:$PATH"
export LD_LIBRARY_PATH="$CUDA_HOME/lib64:${LD_LIBRARY_PATH:-}"

echo "=== BEFORE D011-B2 ==="
python3 - "$PROJECT" <<'PY'
import json,sys
from pathlib import Path
p=Path(sys.argv[1])/'.opencode-v2/work/attempts.json'
d=json.loads(p.read_text())
e=d['deliverables']['D011-B2']
print(json.dumps({
  'count':e.get('count'),
  'sessions':e.get('sessions'),
  'failure_history':e.get('failure_history'),
  'implementation_max_step_continuations':e.get('implementation_max_step_continuations'),
},indent=2))
PY

echo
echo "=== AUDITED RECOVERY ==="
PYTHONPATH=scripts python3 - "$PROJECT" <<'PY'
import sys
from pathlib import Path
import supervisor

project=Path(sys.argv[1])
supervisor.PROJECT=project
if hasattr(supervisor,'LOG'):
    supervisor.LOG=project/'.opencode-v2/work/supervisor.log'

diagnostic=(
    'Exact Verify .opencode-v2/bin/run-checks remains 4/5: only '
    'readme-required-strings fails because TEST_CHECKS.json requires the literal '
    '"standard library" while the completed README does not contain that phrase. '
    'D011-B2 owns only .opencode-v2/TEST_CHECKS.json. The guard already required '
    'a single-line CONTRACT_CHALLENGE after the second exact Verify failure; do not '
    'mutate README or any unowned artifact.'
)
print(supervisor.recover_implementation_max_step_continuation(
    'D011-B2', diagnostic=diagnostic
))
PY

echo
echo "=== AFTER RECOVERY QUERY ==="
cat "$PROJECT/.opencode-v2/query/leaves/D011-B2.json" 2>/dev/null || true

echo
echo "=== AFTER RECOVERY LEDGER SUMMARY ==="
python3 - "$PROJECT" <<'PY'
import json,sys
from pathlib import Path
p=Path(sys.argv[1])/'.opencode-v2/work/attempts.json'
d=json.loads(p.read_text())
e=d['deliverables']['D011-B2']
print(json.dumps({
  'count':e.get('count'),
  'sessions':e.get('sessions'),
  'failure_history':e.get('failure_history'),
  'implementation_max_step_continuations':e.get('implementation_max_step_continuations'),
  'unmaterialized_dispatch_history':e.get('unmaterialized_dispatch_history'),
},indent=2))
PY

echo
echo "=== RESUME CONTROLLER IF REARMED ==="
if python3 - "$PROJECT" <<'PY'
import json,sys
from pathlib import Path
e=json.loads((Path(sys.argv[1])/'.opencode-v2/work/attempts.json').read_text())['deliverables']['D011-B2']
s=e.get('sessions') or []
raise SystemExit(0 if s and str(s[-1]).startswith('dispatch:max-step-continuation:') else 1)
PY
then
    echo "rearmed=yes"
    PYTHONPATH=scripts python3 scripts/stage_a_controller.py \
      --project "$PROJECT" \
      --once \
      --require-supervisor-shadow \
      --execute-first-implementation \
      --base-url http://127.0.0.1:58508 \
      --root-session ses_f190c8a0fffeWkDjwEFVICi474 || true

    if ! pgrep -af "drive-stage-a-run.py.*20260928-final-current-tree-proof2" >/dev/null; then
        nohup env PYTHONPATH=scripts python3 scripts/drive-stage-a-run.py \
          --project "$PROJECT" \
          --base-url http://127.0.0.1:58508 \
          --root-session ses_f190c8a0fffeWkDjwEFVICi474 \
          --poll 2 \
          --max-ticks 300 \
          >>"$BASE/driver.log" 2>&1 &
        echo "driver_pid=$!"
    else
        echo "driver_already_running"
    fi
else
    echo "rearmed=no"
fi

echo
echo "=== DRIVER TAIL ==="
tail -n 100 "$BASE/driver.log" 2>/dev/null || true
