#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$ROOT/scripts/env.sh"

PROJECT="${1:-$PWD}"
PROJECT="$(realpath -m "$PROJECT")"

if [[ ! -d "$PROJECT" ]]; then
    echo "ERROR: Project does not exist: $PROJECT"
    exit 1
fi

BASE_URL="http://127.0.0.1:${MA_PORT}"
MODEL="syv/qwen38"
STARTED_HERE=0
STARTED_PROXY_HERE=0
PROXY_PORT="${V2_PROXY_PORT:-18033}"
PROXY_URL="http://127.0.0.1:${PROXY_PORT}"
PROXY_MODE="v2-role-aware-bounded-v1"
PROXY_PIDFILE="$ROOT/v2-wire-proxy.pid"
STAMP="$(date '+%Y%m%d-%H%M%S')"

AUTH=()
[[ -n "${VLLM_API_KEY:-}" ]] &&
    AUTH=(-H "Authorization: Bearer ${VLLM_API_KEY}")

echo
echo "============================================================"
echo " QWEN3.8 MULTI-AGENT"
echo "============================================================"
echo "Project : $PROJECT"
echo "Model   : $MODEL"
echo "Agent   : orchestrator"
echo "Server  : $BASE_URL"
echo

if ! curl -fsS --max-time 2 \
    "${AUTH[@]}" \
    "$BASE_URL/v1/models" >/dev/null 2>&1
then
    echo "Starting Qwen3.8-27B MTP..."
    "$ROOT/scripts/start-syv-mtp.sh"
    STARTED_HERE=1
fi

MODELS="$(
    curl -fsS --max-time 10 \
        "${AUTH[@]}" \
        "$BASE_URL/v1/models"
)"

if ! grep -q '"id":"qwen3.8-27b"' <<<"$MODELS"; then
    echo "ERROR: qwen3.8-27b is not being served."
    exit 1
fi

echo "Qwen3.8 server ready."

mkdir -p "$ROOT/logs"

proxy_ready() {
    local body
    body="$(curl -fsS --max-time 2 "$PROXY_URL/__proxy_health" 2>/dev/null || true)"
    [[ -n "$body" ]] || return 1
    python3 - "$PROXY_MODE" "$body" <<'PY'
import json,sys
expected=sys.argv[1]
try:
    data=json.loads(sys.argv[2])
except Exception:
    raise SystemExit(1)
raise SystemExit(0 if data.get("ok") is True and data.get("mode")==expected else 1)
PY
}

if ! proxy_ready; then
    if command -v ss >/dev/null && ss -ltn "sport = :$PROXY_PORT" 2>/dev/null | grep -q LISTEN; then
        echo "ERROR: port $PROXY_PORT is occupied by a stale/unknown proxy." >&2
        echo "Expected proxy mode: $PROXY_MODE" >&2
        exit 1
    fi
    PROXY_LOG="$ROOT/logs/v2-wire-proxy-${STAMP}.stdout.log"
    V2_PROXY_LOG="$ROOT/logs/v2-wire-${STAMP}.jsonl" \
    V2_PROXY_LISTEN_PORT="$PROXY_PORT" \
    V2_PROXY_TARGET_PORT="$MA_PORT" \
        node "$ROOT/scripts/v2-wire-proxy.mjs" >"$PROXY_LOG" 2>&1 &
    PROXY_PID=$!
    echo "$PROXY_PID" > "$PROXY_PIDFILE"
    STARTED_PROXY_HERE=1
    for _ in {1..50}; do
        if proxy_ready; then break; fi
        if ! kill -0 "$PROXY_PID" 2>/dev/null; then
            echo "ERROR: bounded V2 proxy exited during startup." >&2
            cat "$PROXY_LOG" >&2 || true
            exit 1
        fi
        sleep 0.1
    done
    proxy_ready || { echo "ERROR: bounded V2 proxy did not become ready." >&2; exit 1; }
fi

echo "Bounded V2 proxy ready: $PROXY_URL -> $BASE_URL"

curl -fsS --max-time 5 \
    "${AUTH[@]}" \
    "$BASE_URL/metrics" \
    > "$ROOT/logs/metrics-before-opencode-${STAMP}.txt" \
    2>/dev/null || true

cleanup() {
    curl -fsS --max-time 5 \
        "${AUTH[@]}" \
        "$BASE_URL/metrics" \
        > "$ROOT/logs/metrics-after-opencode-${STAMP}.txt" \
        2>/dev/null || true

    if [[ "$STARTED_PROXY_HERE" -eq 1 ]]; then
        if [[ -s "$PROXY_PIDFILE" ]]; then
            PROXY_PID="$(cat "$PROXY_PIDFILE" 2>/dev/null || true)"
            [[ "$PROXY_PID" =~ ^[0-9]+$ ]] && kill "$PROXY_PID" 2>/dev/null || true
            rm -f "$PROXY_PIDFILE"
        fi
    fi

    if [[ "$STARTED_HERE" -eq 1 ]]; then
        "$ROOT/scripts/stop-syv.sh" || true
    fi
}

trap cleanup EXIT INT TERM

cd "$PROJECT"

echo
echo "Starting OpenCode 2 explicitly with:"
echo "  model: $MODEL"
echo "  agent: orchestrator"
echo

"$OPENCODE2_BIN" --standalone "$PROJECT"
