set -Eeuo pipefail
INGRESS=/home/bking/AI/chatgpt-job-ingress/ingress.py

echo "=== ROUTE SYMBOLS ==="
grep -n -E 'def do_GET|def do_POST|bridge.user.js|BRIDGE_VERSION|ENDPOINT|GM_xmlhttpRequest|setInterval|MutationObserver|class .*Handler|HTTPServer|ThreadingHTTPServer' "$INGRESS" | head -220 || true

echo
echo "=== HTTP HANDLER REGION ==="
grep -n -E 'def do_GET|def do_POST' "$INGRESS" | head -20
