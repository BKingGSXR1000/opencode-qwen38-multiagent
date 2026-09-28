set -Eeuo pipefail
INGRESS=/home/bking/AI/chatgpt-job-ingress/ingress.py

echo "=== USERSCRIPT CORE 165-285 ==="
sed -n '165,285p' "$INGRESS"

echo
echo "=== USERSCRIPT SEND/SCAN 860-955 ==="
sed -n '860,955p' "$INGRESS"

echo
echo "=== HTTP HANDLER 947-1025 ==="
sed -n '947,1025p' "$INGRESS"
