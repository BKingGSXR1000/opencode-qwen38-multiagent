#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
echo "=== SUPERVISOR EVENTS ==="
tail -n 80 "$ROOT/logs/supervisor-events.log" 2>/dev/null || echo "(none)"
echo
echo "=== SUPERVISOR STDERR ==="
tail -n 40 "$ROOT/logs/supervisor-stderr.log" 2>/dev/null || echo "(none)"
