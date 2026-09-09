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
