#!/usr/bin/env bash
set -Eeuo pipefail

R=/home/bking/AI/opencode-qwen38-multiagent-v2-a2-v11831
P=/home/bking/AI/a2-canaries/20260922-230614-d003b-live/project

echo "=== CONTROLLER LEDGER ==="
cat "$P/.opencode-v2/work/stage-a-controller-executions.json" || true

echo
echo "=== BASE URL + COMPLETION HOOK REFERENCES ==="
grep -RIn --exclude='*.pyc' -C 6 -E 'V2_OPENCODE_BASE_URL|complete-splitter|complete_splitter|splitter_output_b64|dispatch_token' \
  "$R/scripts" "$R/xdg/config" | head -1000 || true

echo
echo "=== FALLBACK DISCOVERY WITHOUT EXPLICIT URL ==="
env -u V2_OPENCODE_BASE_URL python3 - "$R" "$P" <<'PY'
import importlib.util, os, sys
root, project = sys.argv[1:]
sys.path.insert(0, root + "/scripts")
os.environ["V2_PROJECT"] = project
spec = importlib.util.spec_from_file_location("supdiag", root + "/scripts/supervisor.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
m.PROJECT = project
h = m.OpenCodeHTTP()
ok = h.discover()
print("discover_ok=", ok)
print("base=", h.base)
print("mode=", h.mode)
PY

echo
echo "=== DISCOVERY WITH EXPLICIT URL ==="
V2_OPENCODE_BASE_URL=http://127.0.0.1:58443 python3 - "$R" "$P" <<'PY'
import importlib.util, os, sys
root, project = sys.argv[1:]
sys.path.insert(0, root + "/scripts")
os.environ["V2_PROJECT"] = project
spec = importlib.util.spec_from_file_location("supdiag2", root + "/scripts/supervisor.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
m.PROJECT = project
h = m.OpenCodeHTTP()
ok = h.discover()
print("discover_ok=", ok)
print("base=", h.base)
print("mode=", h.mode)
PY

echo
echo "=== JOB 36 REJECTION WINDOW ==="
journalctl --user -u chatgpt-job-ingress.service --since "30 minutes ago" --no-pager -o short-iso | tail -160 || true

echo
echo "JOB_000037_OK"
