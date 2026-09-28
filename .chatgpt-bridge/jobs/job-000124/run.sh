set -Eeuo pipefail
echo "JOB_000124_QUEUE_WAKE_TEST"
echo "=== SERVED VERSION ==="
curl -fsS http://127.0.0.1:8767/bridge.user.js | grep -E '^// @version|BRIDGE_VERSION|WAKE_NEXT_ENDPOINT' | head -10
echo "=== INGRESS ==="
systemctl --user is-active chatgpt-job-ingress.service
