set -Eeuo pipefail
INGRESS=/home/bking/AI/chatgpt-job-ingress/ingress.py
WAKE=/home/bking/AI/chatgpt-wakeup.sh

echo "=== INGRESS TOP 1-165 ==="
sed -n '1,165p' "$INGRESS"

echo
echo "=== CURRENT WAKE SCRIPT ==="
cat "$WAKE"

echo
echo "=== INGRESS SERVICE / PROCESS ==="
systemctl --user status chatgpt-job-ingress.service --no-pager 2>/dev/null || true
ps -ef | grep '[i]ngress.py' || true
