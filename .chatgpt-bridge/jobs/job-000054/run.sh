#!/usr/bin/env bash
set -euo pipefail
cd /home/bking/AI/opencode-qwen38-multiagent-v2-a2-v11831
echo "=== PRECHECK ==="
date -Is
git rev-parse HEAD
git status --short
python3 scripts/test_transport_root_no_continuation.py
python3 scripts/create-d003-splitter-canary.py --selftest
python3 scripts/stage_a_controller.py --selftest
echo "=== QWEN LAUNCHER ==="
sed -n '1,220p' /home/bking/AI/qwen38-27b-rtx3090-syv/single-user/start_qwen.sh
echo "=== CANARY MAIN ==="
sed -n '240,380p' scripts/create-d003-splitter-canary.py
