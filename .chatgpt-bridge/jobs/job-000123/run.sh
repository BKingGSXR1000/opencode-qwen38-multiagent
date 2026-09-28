set -Eeuo pipefail
P=/home/bking/AI/chatgpt-job-ingress/ingress.py
sed -i 's#// @version      2\.10#// @version      2.11#' "$P"
python3 -m py_compile "$P"
systemctl --user restart chatgpt-job-ingress.service
sleep 1
systemctl --user is-active chatgpt-job-ingress.service
curl -fsS http://127.0.0.1:8767/bridge.user.js | grep -E '^// @version|BRIDGE_VERSION' | head -4
xdg-open http://127.0.0.1:8767/bridge.user.js >/dev/null 2>&1 || true
echo JOB123_REPAIR_DONE
