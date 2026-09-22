#!/usr/bin/env bash
set -Eeuo pipefail
echo "BRIDGE_POST_REPAIR_SMOKE"
echo "hostname=$(hostname)"
REPO=/home/bking/AI/opencode-qwen38-multiagent-v2-a2-v11831
echo "branch=$(git -C "$REPO" rev-parse --abbrev-ref HEAD)"
echo "head=$(git -C "$REPO" rev-parse HEAD)"
echo "BRIDGE_POST_REPAIR_OK"
