#!/usr/bin/env bash
set -Eeuo pipefail
R=/home/bking/AI/opencode-qwen38-multiagent-v2-a2-v11831

echo "=== STAGE_A_PREFLIGHT CREATION ==="
sed -n '160,245p' "$R/scripts/stage_a_preflight.py"

echo
echo "=== START-STAGE-A-RUN PREFLIGHT USAGE ==="
sed -n '60,100p' "$R/scripts/start-stage-a-run.sh"

echo
echo "=== DRIVE-STAGE-A-RUN PREFLIGHT USAGE ==="
sed -n '1,125p' "$R/scripts/drive-stage-a-run.py"

echo
echo "JOB_000025_OK"
