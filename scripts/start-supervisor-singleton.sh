#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
SUP="$ROOT/scripts/supervisor.py"
PROJECT="${V2_PROJECT:-${1:-}}"

[[ -n "$PROJECT" ]] || {
  echo "ERROR: V2 project path not supplied" >&2
  exit 1
}

PROJECT="$(cd -- "$PROJECT" && pwd -P)"
CONTROL="$PROJECT/.opencode-v2"
PIDFILE="$CONTROL/work/supervisor.pid"
mkdir -p "$CONTROL/work"

matches_project(){
  local pid="$1"
  [[ -r "/proc/$pid/environ" ]] || return 1
  tr '\0' '\n' < "/proc/$pid/environ" | grep -Fqx "V2_PROJECT=$PROJECT"
}

old_pid=""
if [[ -s "$PIDFILE" ]]; then
  candidate="$(<"$PIDFILE")"
  if [[ "$candidate" =~ ^[0-9]+$ ]] && kill -0 "$candidate" 2>/dev/null && matches_project "$candidate"; then
    old_pid="$candidate"
  fi
fi

# Adopt supervisors created before project-scoped pidfiles existed, but never
# touch a supervisor whose V2_PROJECT belongs to a different project.
if [[ -z "$old_pid" ]]; then
  while IFS= read -r candidate; do
    if matches_project "$candidate"; then
      old_pid="$candidate"
      break
    fi
  done < <(pgrep -f "^python3 $SUP$" 2>/dev/null || true)
fi

if [[ -n "$old_pid" ]]; then
  echo "Stopping stale V2 supervisor for $PROJECT: $old_pid" >&2
  kill "$old_pid" 2>/dev/null || true

  for _ in {1..20}; do
    sleep 0.1
    kill -0 "$old_pid" 2>/dev/null || break
  done

  if kill -0 "$old_pid" 2>/dev/null; then
    kill -KILL "$old_pid" 2>/dev/null || true
  fi
fi

V2_ROOT="$ROOT" V2_PROJECT="$PROJECT" python3 "$SUP" >/dev/null 2>>"$ROOT/logs/supervisor-stderr.log" &
PID=$!

sleep 0.5

if ! kill -0 "$PID" 2>/dev/null || ! matches_project "$PID"; then
  echo "ERROR: supervisor failed to start for $PROJECT" >&2
  kill "$PID" 2>/dev/null || true
  exit 1
fi

printf '%s\n' "$PID" > "$PIDFILE"
echo "$PID"
