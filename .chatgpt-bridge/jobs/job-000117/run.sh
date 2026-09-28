set -Eeuo pipefail
INGRESS=/home/bking/AI/chatgpt-job-ingress/ingress.py
echo "=== USERSCRIPT DOM / RECOVERY 600-870 ==="
sed -n '600,870p' "$INGRESS"
