#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
SUP="$ROOT/scripts/supervisor.py"
PROJECT="${V2_PROJECT:-${1:-}}"

[[ -n "$PROJECT" ]] || {
  echo "ERROR: V2 project path not supplied" >&2
  exit 1
}

mapfile -t OLD < <(pgrep -f "^python3 $SUP$" 2>/dev/null || true)

if ((${#OLD[@]})); then
  echo "Stopping stale V2 supervisor(s): ${OLD[*]}" >&2
  kill "${OLD[@]}" 2>/dev/null || true

  for _ in {1..20}; do
    sleep 0.1
    mapfile -t LEFT < <(pgrep -f "^python3 $SUP$" 2>/dev/null || true)
    ((${#LEFT[@]} == 0)) && break
  done

  mapfile -t LEFT < <(pgrep -f "^python3 $SUP$" 2>/dev/null || true)
  if ((${#LEFT[@]})); then
    kill -KILL "${LEFT[@]}" 2>/dev/null || true
  fi
fi

V2_ROOT="$ROOT" V2_PROJECT="$PROJECT" python3 "$SUP" >/dev/null 2>>"$ROOT/logs/supervisor-stderr.log" &
PID=$!

sleep 0.5

mapfile -t NOW < <(pgrep -f "^python3 $SUP$" 2>/dev/null || true)
if ((${#NOW[@]} != 1)); then
  echo "ERROR: expected exactly 1 supervisor, found ${#NOW[@]}: ${NOW[*]-}" >&2
  kill "$PID" 2>/dev/null || true
  exit 1
fi

echo "$PID"
