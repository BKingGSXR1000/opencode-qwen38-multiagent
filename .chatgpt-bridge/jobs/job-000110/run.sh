set -Eeuo pipefail
REPO=/home/bking/AI/opencode-qwen38-multiagent-v2-a2-v11831
cd "$REPO"

echo "=== RECOVERY FUNCTION FULL BRANCH ==="
sed -n '6125,6285p' scripts/supervisor.py

echo
echo "=== CONTRACT CHALLENGE / MAX-STEP SYMBOLS ==="
grep -n -E 'CONTRACT_CHALLENGE|contract_challenge|max_step_terminal|max-step|maximum.steps|no_more_tools' scripts/supervisor.py | head -260 || true

echo
echo "=== THIRD-CONTINUATION TESTS ==="
sed -n '1760,1935p' scripts/test_state_machine_invariants.py

echo
echo "=== PLAN-CONTRACT / CONTRACT-CHALLENGE TESTS ==="
grep -n -E 'CONTRACT_CHALLENGE|contract.challenge|max.step.*challenge|challenge.*max.step' scripts/test_state_machine_invariants.py | head -220 || true

echo
echo "=== CURRENT D011-B2 LAST SESSION ==="
python3 - <<'PY'
import json
from pathlib import Path
p=Path("/home/bking/AI/a2-e2e/20260928-final-current-tree-proof2/project/.opencode-v2/work/attempts.json")
e=json.loads(p.read_text())["deliverables"]["D011-B2"]
print("session", e["sessions"][-1])
print("history kinds", [x.get("recovery_kind") for x in e.get("implementation_max_step_continuations",[])])
PY
