#!/usr/bin/env bash
set -euo pipefail
REPO=/home/bking/AI/opencode-qwen38-multiagent-v2-a2-v11831
cd "$REPO"

echo "=== NOW ==="
date -Is
echo "=== HEAD / STATUS ==="
git rev-parse HEAD
git status --short

echo "=== GPU STATE ==="
nvidia-smi || true
echo "=== GPU QUERY ==="
nvidia-smi --query-gpu=index,name,temperature.gpu,fan.speed,power.draw,power.limit,memory.used,memory.total,utilization.gpu,pstate --format=csv || true

echo "=== CURRENT AI PROCESSES ==="
ps -eo pid,etimes,cmd | grep -E 'vllm|opencode serve|scripts/supervisor.py|stage_a_controller|create-d003' | grep -v grep || true

echo "=== A2 SERVER LAUNCHER ==="
sed -n '1,260p' scripts/run-a2-v11831-server.sh

echo "=== STAGE-A START LAUNCHER ==="
sed -n '1,220p' scripts/start-stage-a-run.sh

echo "=== CANARY CREATOR OPTIONS / MAIN ==="
grep -nE 'argparse|add_argument|d003-b-equivalent|first-turn-invalid|base-url|opencode|vllm|port' scripts/create-d003-splitter-canary.py | head -220 || true
sed -n '1,260p' scripts/create-d003-splitter-canary.py | tail -180

echo "=== VLLM / MODEL LAUNCH REFERENCES ==="
grep -RInE '18030|Qwen3\.8-27B-W4A16-AutoRound-fast|gpu-memory-utilization|vllm serve' \
  /home/bking/AI/qwen38-27b-rtx3090-syv \
  "$REPO"/scripts 2>/dev/null | head -260 || true

echo "=== CANDIDATE START SCRIPTS ==="
find /home/bking/AI/qwen38-27b-rtx3090-syv -maxdepth 3 -type f \
  \( -name '*.sh' -o -name '*.service' -o -name '*.py' \) -print 2>/dev/null | sort | head -220 || true

echo "=== MODEL ENDPOINT CHECK ==="
curl -sS --max-time 2 http://127.0.0.1:18030/v1/models || true
echo
