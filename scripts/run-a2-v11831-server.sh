#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="/home/bking/AI/opencode-qwen38-multiagent-v2-a2-v11831"
BIN="$(cat "$ROOT/runtime/opencode-v1.18.31.path")"
PROJECT="${1:?usage: $0 /absolute/project/path [port]}"
PORT="${2:-57042}"

[[ -d "$PROJECT" ]] || { echo "ERROR: project does not exist: $PROJECT" >&2; exit 1; }
[[ -x "$BIN" ]] || { echo "ERROR: OpenCode v1.18.31 binary missing: $BIN" >&2; exit 1; }

export XDG_CONFIG_HOME="$ROOT/xdg/config"
export XDG_DATA_HOME="$ROOT/xdg/data-v11831-a2"
export XDG_CACHE_HOME="$ROOT/xdg/cache-v11831-a2"
export XDG_STATE_HOME="$ROOT/xdg/state-v11831-a2"
export V2_ROOT="$ROOT"
export V2_OPENCODE_DB="$XDG_DATA_HOME/opencode/opencode.db"
export V2_OPENCODE_BASE_URL="http://127.0.0.1:$PORT"
export V2_OPENCODE_SESSION_TABLE="session"
export OPENCODE_DISABLE_PROJECT_CONFIG=true
export OPENCODE_EXPERIMENTAL_BACKGROUND_SUBAGENTS=true

: "${VLLM_API_KEY:?VLLM_API_KEY must be exported before starting A2 v1.18.31}"

mkdir -p "$XDG_DATA_HOME" "$XDG_CACHE_HOME" "$XDG_STATE_HOME"

echo "============================================================"
echo " OpenCode V2 A2 / v1.18.31 integration server"
echo "============================================================"
echo "Root    : $ROOT"
echo "Project : $PROJECT"
echo "Port    : $PORT"
echo "Config  : $XDG_CONFIG_HOME/opencode/opencode.jsonc"
echo
echo "Background subagents: enabled"
echo "Technical root model : v2noop/root-noop"
echo "Semantic models      : syv/qwen38-*"
echo
exec "$BIN" serve --hostname 127.0.0.1 --port "$PORT"
