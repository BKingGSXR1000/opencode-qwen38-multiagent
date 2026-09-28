set -Eeuo pipefail
ROOT=/home/bking/AI/opencode-qwen38-multiagent-v2-a2-v11831
ING=/home/bking/AI/chatgpt-job-ingress/ingress.py
cd "$ROOT"

echo "=== INGRESS QUEUE_JOB + POST ==="
sed -n '85,160p' "$ING"
sed -n '1110,1165p' "$ING"

echo
echo "=== SUPERVISOR RECOVERY DECISION/PERSISTENCE ==="
sed -n '6170,6460p' scripts/supervisor.py
sed -n '6495,6635p' scripts/supervisor.py

echo
echo "=== RECOVERY TEST CLASS HEADER + THIRD CONTINUATION TESTS ==="
sed -n '1380,1510p' scripts/test_state_machine_invariants.py
sed -n '1720,1915p' scripts/test_state_machine_invariants.py
