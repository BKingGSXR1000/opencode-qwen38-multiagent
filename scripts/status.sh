#!/usr/bin/env bash
set -Eeuo pipefail
source "$(dirname "$0")/env.sh"
echo "=== OpenCode 2 ==="; "$OPENCODE2_BIN" --version || true
echo; echo "=== API ==="
AUTH=(); [[ -n "${VLLM_API_KEY:-}" ]] && AUTH=(-H "Authorization: Bearer ${VLLM_API_KEY}")
curl -fsS --max-time 3 "${AUTH[@]}" "http://127.0.0.1:${MA_PORT}/v1/models" || echo "not running"
echo; echo; echo "=== RTX 3090 ==="; nvidia-smi -i "$MA_GPU_INDEX"
echo; echo "=== Test project ==="; git -C "$MA_PROJECT" status --short --branch
