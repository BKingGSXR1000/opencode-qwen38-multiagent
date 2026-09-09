#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT="${1:-$PWD}"
PROJECT="$(realpath -m "$PROJECT")"

[[ -d "$PROJECT" ]] || { echo "ERROR: Project directory does not exist: $PROJECT"; exit 1; }

echo
echo "Starting a NEW V2 OpenCode parent session in:"
echo "  $PROJECT"
echo
echo "Use /oneshot-v2 for a new project/task."
echo "Use /resume-v2 when .opencode-v2/STATE.md already exists."
echo

cd "$PROJECT"
exec "$ROOT/run.sh" "$PROJECT"
