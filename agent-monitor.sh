#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="${HOME}/AI/opencode-qwen38-multiagent-v2"
exec python3 "${ROOT}/scripts/agent-monitor.py" "$@"
