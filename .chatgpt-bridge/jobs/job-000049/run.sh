#!/usr/bin/env bash
set -euo pipefail
REPO=/home/bking/AI/opencode-qwen38-multiagent-v2-a2-v11831
cd "$REPO"

echo "=== PRE ==="
date -Is
git rev-parse HEAD
git status --short

echo "=== TEST CONTEXT BEFORE ==="
sed -n '45,100p' scripts/test_transport_root_no_continuation.py

python3 - <<'PY'
from pathlib import Path
p=Path("scripts/test_transport_root_no_continuation.py")
s=p.read_text()
start=s.index("    def test_corrective_splitter_dispatch_omits_command(self):")
next_def=s.find("\n    def ", start+5)
next_class=s.find("\nclass ", start+5)
ends=[x for x in (next_def,next_class) if x >= 0]
end=min(ends) if ends else len(s)
block=s[start:end]

old="        self.assertEqual(len(calls), 1)"
if old not in block:
    raise SystemExit("expected len(calls) assertion not found in target test")
block=block.replace(
    old,
    '        post_calls=[call for call in calls if call[0] == "POST"]\n'
    '        self.assertEqual(len(post_calls), 1)',
    1,
)
if "calls[0]" in block:
    block=block.replace("calls[0]","post_calls[0]",1)

p.write_text(s[:start]+block+s[end:])
PY

echo "=== TEST CONTEXT AFTER ==="
sed -n '45,105p' scripts/test_transport_root_no_continuation.py

echo "=== TESTS ==="
python3 -m py_compile scripts/supervisor.py scripts/stage_a_controller.py scripts/test_transport_root_no_continuation.py
python3 scripts/test_transport_root_no_continuation.py
python3 -m unittest scripts.test_state_machine_invariants.AbortIntentTests

echo "=== INTENDED DIFF CHECK ==="
git diff --check -- scripts/stage_a_controller.py scripts/supervisor.py scripts/test_transport_root_no_continuation.py
git diff --stat -- scripts/stage_a_controller.py scripts/supervisor.py scripts/test_transport_root_no_continuation.py

echo "=== COMMIT ==="
git add scripts/stage_a_controller.py scripts/supervisor.py scripts/test_transport_root_no_continuation.py
git diff --cached --check
git diff --cached --stat
git commit -m "Quiesce transport root before splitter correction"
git push origin HEAD:a2-v11831-integration

echo "=== POST ==="
git rev-parse HEAD
git status --short
