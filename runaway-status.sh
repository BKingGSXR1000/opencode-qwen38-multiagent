#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
echo "=== RUNAWAY EVENTS ==="
tail -n 60 "$ROOT/logs/runaway-events.log" 2>/dev/null || echo "(none)"
echo
echo "=== HARD ABORT / RESCUE STATE ==="
column -t -s $'\t' "$ROOT/logs/runaway-state.tsv" 2>/dev/null || echo "(none)"
