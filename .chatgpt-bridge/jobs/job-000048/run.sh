#!/usr/bin/env bash
set -euo pipefail
REPO=/home/bking/AI/opencode-qwen38-multiagent-v2-a2-v11831
cd "$REPO"

echo "=== PRE ==="
date -Is
git rev-parse HEAD
git status --short

python3 - <<'PY'
from pathlib import Path
p=Path("scripts/test_transport_root_no_continuation.py")
s=p.read_text()
old='self.assertEqual(detail, "accepted")'
new='self.assertEqual(detail, "accepted-after-root-idle")'
count=s.count(old)
if count != 1:
    raise SystemExit(f"expected exactly one old assertion, found {count}")
p.write_text(s.replace(old,new,1))
PY

echo "=== STAGE CONTROLLER DIFF REVIEW ==="
git diff -- scripts/stage_a_controller.py | sed -n '1,240p'

echo "=== TESTS ==="
python3 -m py_compile scripts/supervisor.py scripts/stage_a_controller.py scripts/test_transport_root_no_continuation.py
python3 scripts/test_transport_root_no_continuation.py
python3 -m unittest scripts.test_state_machine_invariants.AbortIntentTests

echo "=== INTENDED DIFF CHECK ==="
git diff --check -- scripts/stage_a_controller.py scripts/supervisor.py scripts/test_transport_root_no_continuation.py
git diff -- scripts/stage_a_controller.py scripts/supervisor.py scripts/test_transport_root_no_continuation.py

echo "=== COMMIT ==="
git add scripts/stage_a_controller.py scripts/supervisor.py scripts/test_transport_root_no_continuation.py
git diff --cached --check
git diff --cached --stat
git commit -m "Quiesce transport root before splitter correction"
git push origin HEAD:a2-v11831-integration

echo "=== POST ==="
git rev-parse HEAD
git status --short
