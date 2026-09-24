#!/usr/bin/env bash
set -u
REPO=/home/bking/AI/opencode-qwen38-multiagent-v2-a2-v11831
cd "$REPO" || exit 2
echo "=== NOW ==="
date -Is
echo "=== HEAD ==="
git rev-parse HEAD
echo "=== HTTP STATUS/ABORT IMPLEMENTATION ==="
sed -n '6090,6275p' scripts/supervisor.py
echo "=== ABORT_SESSION WRAPPER ==="
sed -n '6435,6485p' scripts/supervisor.py
echo "=== ABORT UNIT TESTS ==="
sed -n '330,365p' scripts/test_state_machine_invariants.py
echo "=== CONTROL-PLANE ABORT TESTS ==="
sed -n '140,180p' scripts/test_control_plane.py
sed -n '520,560p' scripts/test_control_plane.py
echo "=== STAGE CONTROLLER STATUS HELPERS ==="
sed -n '520,565p' scripts/stage_a_controller.py
sed -n '680,715p' scripts/stage_a_controller.py
echo "=== PROMPT_ASYNC / ABORT REFERENCES ==="
grep -RInE 'prompt_async|/session/.*/abort|session/status|abort_session' scripts/*.py scripts/*.sh 2>/dev/null | head -250 || true
