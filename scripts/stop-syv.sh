#!/usr/bin/env bash
set -Eeuo pipefail

source "$(dirname "$0")/env.sh"

PIDFILE="$MA_ROOT/server.pid"
GPUPIDFILE="$MA_ROOT/gpu-logger.pid"
PORT="$MA_PORT"

# 1. Try the process group recorded when we launched the server.
if [[ -f "$PIDFILE" ]]; then
    PID="$(cat "$PIDFILE" 2>/dev/null || true)"

    if [[ -n "$PID" ]]; then
        # Important: try the process GROUP even if the original leader PID
        # has already exited.
        kill -TERM -- "-$PID" 2>/dev/null || true
    fi

    rm -f "$PIDFILE"
fi

# Give the server a short graceful-stop window.
for _ in {1..20}; do
    if ! ss -ltn "sport = :$PORT" 2>/dev/null | grep -q LISTEN; then
        break
    fi
    sleep 0.25
done

# 2. Fallback: if our Qwen server is still listening, kill the actual listener.
if ss -ltn "sport = :$PORT" 2>/dev/null | grep -q LISTEN; then
    echo "Stopping remaining server on port $PORT..."
    fuser -k "${PORT}/tcp" >/dev/null 2>&1 || true
fi

# Verify.
for _ in {1..20}; do
    if ! ss -ltn "sport = :$PORT" 2>/dev/null | grep -q LISTEN; then
        break
    fi
    sleep 0.25
done

if ss -ltn "sport = :$PORT" 2>/dev/null | grep -q LISTEN; then
    echo "ERROR: server is still listening on port $PORT" >&2
    exit 1
fi

# Stop GPU logger.
if [[ -f "$GPUPIDFILE" ]]; then
    GPID="$(cat "$GPUPIDFILE" 2>/dev/null || true)"
    [[ -n "$GPID" ]] && kill "$GPID" 2>/dev/null || true
    rm -f "$GPUPIDFILE"
fi

echo "Qwen/vLLM stopped."
