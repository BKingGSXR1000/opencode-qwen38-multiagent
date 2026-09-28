set -euo pipefail
REPO=/home/bking/AI/opencode-qwen38-multiagent-v2-a2-v11831
PROJECT=/home/bking/AI/a2-e2e/20260928-final-current-tree-proof2/project
cd "$REPO"

echo "=== RECOVERY IMPLEMENTATION ==="
sed -n '6080,6215p' scripts/supervisor.py

echo
echo "=== RECOVERY TEST PATTERNS ==="
sed -n '1460,1910p' scripts/test_state_machine_invariants.py

echo
echo "=== D011-B2 QUERY ==="
cat "$PROJECT/.opencode-v2/query/leaves/D011-B2.json" 2>/dev/null || true

echo
echo "=== D011-B2 CONTEXT ==="
cat "$PROJECT/.opencode-v2/query/leaves/D011-B2-context.json" 2>/dev/null || true

echo
echo "=== ATTEMPTS ENTRY ==="
python3 - "$PROJECT/.opencode-v2/work/attempts.json" <<'PY'
import json,sys
d=json.load(open(sys.argv[1]))
if isinstance(d,dict):
    print(json.dumps(d.get("D011-B2", d.get("deliverables",{}).get("D011-B2")), indent=2, sort_keys=True))
else:
    print(type(d), len(d))
    for x in d:
        if isinstance(x,dict) and x.get("deliverable")=="D011-B2":
            print(json.dumps(x, indent=2, sort_keys=True))
PY

echo
echo "=== PROGRESS ==="
cat "$PROJECT/.opencode-v2/work/D011-B2.progress.md" 2>/dev/null || true

echo
echo "=== CONTROLLER READ-ONLY ONCE ==="
PYTHONPATH=scripts python3 scripts/stage_a_controller.py \
  --project "$PROJECT" \
  --once \
  --require-supervisor-shadow \
  --base-url http://127.0.0.1:58508 \
  --root-session ses_f190c8a0fffeWkDjwEFVICi474 || true
