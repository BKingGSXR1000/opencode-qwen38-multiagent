#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
BIN="$(cat "$ROOT/runtime/opencode-v1.18.31.path")"
PROJECT="${1:?usage: $0 /absolute/project/path PORT PROVIDER_URL}"
PORT="${2:?usage: $0 /absolute/project/path PORT PROVIDER_URL}"
PROVIDER_URL="${3:?usage: $0 /absolute/project/path PORT PROVIDER_URL}"

[[ -d "$PROJECT" ]] || {
  echo "ERROR: project does not exist: $PROJECT" >&2
  exit 1
}
[[ "$PORT" =~ ^[0-9]+$ ]] || {
  echo "ERROR: port must be numeric" >&2
  exit 1
}
[[ "$PROVIDER_URL" =~ ^http://127\.0\.0\.1:[0-9]+/v1$ ]] || {
  echo "ERROR: provider URL must be localhost /v1" >&2
  exit 1
}

CANONICAL="$ROOT/xdg/config"
OVERLAY="$(mktemp -d /tmp/stage-a-canary-config.XXXXXX)"
trap 'rm -rf -- "$OVERLAY"' EXIT
python3 "$ROOT/scripts/stage_a_path_permissions.py"   --project "$PROJECT" --config-home "$CANONICAL"   --overlay-root "$OVERLAY" >/dev/null
python3 - "$OVERLAY/opencode/opencode.jsonc" "$PROVIDER_URL" <<'PY'
from pathlib import Path
import sys
path=Path(sys.argv[1])
provider=sys.argv[2]
text=path.read_text()
old='"baseURL": "http://127.0.0.1:18033/v1"'
if text.count(old)!=1:
    raise SystemExit(f"expected one canonical syv baseURL, found {text.count(old)}")
path.write_text(text.replace(old,f'"baseURL": "{provider}"',1))
PY

export XDG_CONFIG_HOME="$OVERLAY"
export XDG_DATA_HOME="$ROOT/xdg/data-v11831-a2"
export XDG_CACHE_HOME="$ROOT/xdg/cache-v11831-a2"
export XDG_STATE_HOME="$ROOT/xdg/state-v11831-a2"
export V2_ROOT="$ROOT"
export V2_OPENCODE_DB="$XDG_DATA_HOME/opencode/opencode.db"
export V2_OPENCODE_BASE_URL="http://127.0.0.1:$PORT"
export V2_OPENCODE_SESSION_TABLE=session
export OPENCODE_DISABLE_PROJECT_CONFIG=true
export OPENCODE_EXPERIMENTAL_BACKGROUND_SUBAGENTS=true

: "${VLLM_API_KEY:?VLLM_API_KEY must be exported}"
exec "$BIN" serve --hostname 127.0.0.1 --port "$PORT"
