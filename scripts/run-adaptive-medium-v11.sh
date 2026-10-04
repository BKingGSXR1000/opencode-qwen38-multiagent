#!/usr/bin/env bash
# Strict sequential medium-sized OFF/OBSERVE/ENFORCE comparison.
# No shared vLLM restart; no OpenCode upgrade; no replay of a failed baseline.
set -euo pipefail
R="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ROOT=/home/bking/AI/a2-e2e/20261004-adaptive-medium-contract-v11-compare
ACT="$HOME/AI/opencode-v2-activity"
cd "$R"
if test -e "$ROOT" && test -e "$ROOT/off/status.json";then
 echo "Refusing to overwrite an earlier v11 run: $ROOT" >&2
 exit 3
fi
mkdir -p "$ROOT"
exec 9>"$ROOT/run.lock"
if ! flock -n 9;then
 echo "v11 already running" >&2
 exit 3
fi
for mode in off observe enforce;do
 "$ACT" START "v11 $mode multi-agent benchmark (up to 900 s)"
 if python3 -u scripts/benchmark-adaptive-medium-contract-v11.py \
    --modes "$mode" --seconds-per-run 900 >"$ROOT/$mode-runner.log" 2>&1
 then
   python3 - "$ROOT/$mode/result.json" <<'PY'
import json,sys
r=json.load(open(sys.argv[1]))
assert r.get("accepted") is True
assert r.get("musts_pass")==5 and r.get("musts_total")==5
assert r.get("heldout",{}).get("pass") is True
print("ACCEPTANCE_PASS",r["mode"],r["duration_seconds"])
PY
   "$ACT" DONE "v11 $mode: independent 5/5 Acceptance and held-out PASS"
 else
   "$ACT" FAIL "v11 $mode: no complete PASS; later modes intentionally not started"
   tail -16 "$ROOT/$mode-runner.log" >&2 || true
   exit 2
 fi
done
"$ACT" DONE "v11 OFF/OBSERVE/ENFORCE all independently accepted"
