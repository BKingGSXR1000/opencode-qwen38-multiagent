#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT="${1:-$PWD}"
PROJECT="$(realpath -m "$PROJECT")"
[[ -d "$PROJECT" ]] || { echo "ERROR: Project does not exist: $PROJECT"; exit 1; }

mkdir -p "$ROOT/logs"
python3 "$ROOT/scripts/run-checks.py" --project "$PROJECT" \
  --bootstrap-control-contract >/dev/null

"$ROOT/scripts/kv-guard.sh" >/dev/null 2>&1 & KV_PID=$!
SUP_PID="$(V2_PROJECT="$PROJECT" "$ROOT/scripts/start-supervisor-singleton.sh")"
cleanup(){
  kill "$KV_PID" "$SUP_PID" 2>/dev/null || true
  wait "$KV_PID" "$SUP_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

"$ROOT/run-base.sh" "$PROJECT"
