#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$HOME/AI/opencode-qwen38-multiagent-v2"
TOOLS="$ROOT/tools/acceptance-browser"

command -v npm >/dev/null || {
  echo "ERROR: npm is required for the isolated acceptance browser."
  exit 1
}

mkdir -p "$TOOLS"

if [[ ! -f "$TOOLS/package.json" ]]; then
  cat > "$TOOLS/package.json" <<'JSON'
{
  "name": "opencode-v2-acceptance-browser",
  "private": true,
  "version": "1.0.0"
}
JSON
fi

echo "Installing Playwright locally in:"
echo "  $TOOLS"
npm install --prefix "$TOOLS" playwright

echo
echo "Installing Playwright Chromium..."
"$TOOLS/node_modules/.bin/playwright" install chromium

echo
echo "Acceptance browser ready."
