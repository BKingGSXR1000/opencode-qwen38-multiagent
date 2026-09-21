#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
PROJECT="${1:?usage: $0 /absolute/project/path http://127.0.0.1:PORT}"
BASE_URL="${2:?usage: $0 /absolute/project/path http://127.0.0.1:PORT}"
DB="$ROOT/xdg/data-v11831-a2/opencode/opencode.db"

[[ -d "$PROJECT" ]] || { echo "ERROR: project does not exist: $PROJECT" >&2; exit 1; }
[[ "$BASE_URL" =~ ^http://127\.0\.0\.1:[0-9]+$ ]] || {
  echo "ERROR: base URL must be a localhost HTTP URL with an explicit port" >&2
  exit 1
}
[[ -f "$DB" ]] || {
  echo "ERROR: v1.18.31 OpenCode database is not available: $DB" >&2
  exit 1
}

export V2_ROOT="$ROOT"
export V2_PROJECT="$PROJECT"
export V2_OPENCODE_BASE_URL="$BASE_URL"
export V2_OPENCODE_DB="$DB"
export V2_OPENCODE_SESSION_TABLE="session"

exec "$ROOT/scripts/start-supervisor-singleton.sh" "$PROJECT"
