#!/usr/bin/env bash
# Start one generic deterministic Stage-A run.  It never accepts a task through
# a model prompt: the task is durably bootstrapped from a caller-owned file.
set -Eeuo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
PREFLIGHT=false
if [[ "${1:-}" == "--preflight" ]]; then
  PREFLIGHT=true
  shift
fi
PROJECT="${1:?usage: $0 [--preflight] /absolute/project/path /absolute/task-file [port]}"
TASK_FILE="${2:?usage: $0 [--preflight] /absolute/project/path /absolute/task-file [port]}"
PORT="${3:-57042}"
BASE_URL="http://127.0.0.1:$PORT"
PROJECT="$(cd -- "$PROJECT" && pwd -P)"
TASK_FILE="$(cd -- "$(dirname -- "$TASK_FILE")" && pwd -P)/$(basename -- "$TASK_FILE")"

[[ -d "$PROJECT" ]] || { echo "ERROR: project does not exist: $PROJECT" >&2; exit 1; }
[[ -f "$TASK_FILE" ]] || { echo "ERROR: task file does not exist: $TASK_FILE" >&2; exit 1; }
[[ "$PORT" =~ ^[0-9]+$ ]] || { echo "ERROR: port must be numeric" >&2; exit 1; }
[[ -x "$ROOT/scripts/bootstrap-stage-a-project.py" ]] || { echo "ERROR: bootstrap utility missing" >&2; exit 1; }
[[ -x "$ROOT/scripts/create-stage-a-root.py" ]] || { echo "ERROR: root utility missing" >&2; exit 1; }
[[ -x "$ROOT/scripts/drive-stage-a-run.py" ]] || { echo "ERROR: driver utility missing" >&2; exit 1; }

CONFIG="$ROOT/xdg/config/opencode/opencode.jsonc"
grep -Fq '"default_agent": "transport-root"' "$CONFIG" || { echo "ERROR: canonical transport-root config missing" >&2; exit 1; }
grep -Fq '"model": "v2noop/root-noop"' "$CONFIG" || { echo "ERROR: canonical root model config missing" >&2; exit 1; }

if "$PREFLIGHT"; then
  python3 "$ROOT/scripts/bootstrap-stage-a-project.py" --project "$PROJECT" --task-file "$TASK_FILE" --dry-run
  echo "STAGE_A_PREFLIGHT: PASS"
  exit 0
fi

: "${VLLM_API_KEY:?VLLM_API_KEY must be exported before starting a Stage-A run}"
mkdir -p "$ROOT/logs"
python3 "$ROOT/scripts/bootstrap-stage-a-project.py" --project "$PROJECT" --task-file "$TASK_FILE" >/dev/null

http_status(){
  curl -sS -o /dev/null -w '%{http_code}' -G "$1" --data-urlencode "directory=$PROJECT" 2>/dev/null || true
}

if [[ "$(http_status "$BASE_URL/session/status")" != "200" ]]; then
  if ! curl -fsS "http://127.0.0.1:57182/v1/models" >/dev/null 2>&1; then
    "$ROOT/scripts/run-a2-v11831-noop.sh" >"$ROOT/logs/stage-a-noop.log" 2>&1 &
  fi
  "$ROOT/scripts/run-a2-v11831-server.sh" "$PROJECT" "$PORT" >"$ROOT/logs/stage-a-server-$PORT.log" 2>&1 &
  for _ in {1..120}; do
    [[ "$(http_status "$BASE_URL/session/status")" == "200" ]] && break
    sleep 0.25
  done
fi
[[ "$(http_status "$BASE_URL/session/status")" == "200" ]] || {
  echo "ERROR: OpenCode server did not become ready at $BASE_URL" >&2
  exit 1
}

"$ROOT/scripts/start-a2-v11831-supervisor.sh" "$PROJECT" "$BASE_URL" >/dev/null
ROOT_RECEIPT="$(python3 "$ROOT/scripts/create-stage-a-root.py" --project "$PROJECT" --base-url "$BASE_URL")"
ROOT_SESSION="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["root_session"])' <<<"$ROOT_RECEIPT")"

for _ in {1..120}; do
  [[ -s "$PROJECT/.opencode-v2/work/root-session.json" ]] && [[ -s "$PROJECT/.opencode-v2/query/deterministic-shadow.json" ]] && break
  sleep 0.25
done
[[ -s "$PROJECT/.opencode-v2/work/root-session.json" ]] || { echo "ERROR: supervisor did not adopt the technical root" >&2; exit 1; }
[[ -s "$PROJECT/.opencode-v2/query/deterministic-shadow.json" ]] || { echo "ERROR: supervisor did not materialize deterministic shadow" >&2; exit 1; }

exec python3 "$ROOT/scripts/drive-stage-a-run.py" --project "$PROJECT" --base-url "$BASE_URL" --root-session "$ROOT_SESSION"
