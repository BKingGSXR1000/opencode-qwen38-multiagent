set -Eeuo pipefail
REPO=/home/bking/AI/opencode-qwen38-multiagent-v2-a2-v11831
cd "$REPO"

echo "=== RUNNER WOKEN PATH ==="
sed -n '390,545p' /home/bking/AI/github-chatgpt-runner/runner.py
echo
grep -R -n -E 'def woken_path|WOKEN|wake_pending|wake\(' /home/bking/AI/github-chatgpt-runner/runner.py | head -120 || true

echo
echo "=== RECOVERY EVIDENCE LOGIC 6240-6345 ==="
sed -n '6240,6345p' scripts/supervisor.py

echo
echo "=== RECOVERY RECORD LOGIC 6380-6638 ==="
sed -n '6380,6638p' scripts/supervisor.py

echo
echo "=== CONTRACT CHALLENGE GUARD 12970-13075 ==="
sed -n '12970,13075p' scripts/supervisor.py

echo
echo "=== CONTRACT CHALLENGE TEST 4090-4175 ==="
sed -n '4090,4175p' scripts/test_state_machine_invariants.py

echo
echo "=== MAX STEP DB HELPER ==="
sed -n '680,735p' scripts/supervisor.py
