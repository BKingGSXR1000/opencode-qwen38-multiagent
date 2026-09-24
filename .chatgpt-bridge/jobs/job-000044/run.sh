#!/usr/bin/env bash
set -u
REPO=/home/bking/AI/opencode-qwen38-multiagent-v2-a2-v11831
cd "$REPO" || exit 2
echo "=== NOW ==="
date -Is
echo "=== GIT ==="
git rev-parse HEAD
git status --short
echo "=== PROCESSES ==="
ps -eo pid,etimes,cmd | grep -E 'vllm|opencode serve|scripts/supervisor.py' | grep -v grep || true
echo "=== SUPERVISOR HTTP METHODS ==="
grep -nE 'def (ensure|discover|get_status|abort_session|dispatch_splitter_corrective_turn)|/session/status|/abort|prompt_async' scripts/supervisor.py | head -120 || true
echo "=== CORRECTIVE DISPATCH AREA ==="
grep -n 'dispatch_splitter_corrective_turn' scripts/supervisor.py | head -10 || true
N=$(grep -n 'def dispatch_splitter_corrective_turn' scripts/supervisor.py | head -1 | cut -d: -f1)
if [ -n "${N:-}" ]; then
  A=$((N-25)); [ "$A" -lt 1 ] && A=1
  B=$((N+140))
  sed -n "${A},${B}p" scripts/supervisor.py
fi
echo "=== ABORT REFERENCES REPO ==="
grep -RInE 'session/.*/abort|abort_session|/abort|session/status' scripts runtime/opencode-v1.18.31 2>/dev/null | head -200 || true
echo "=== LIVE PORTS ==="
for p in 58441 58442 58443; do
  echo "--- $p ---"
  curl -sS --max-time 2 "http://127.0.0.1:$p/session/status" || true
  echo
done
