#!/usr/bin/env bash
set -Eeuo pipefail

echo "=== WAKE SCRIPT ==="
if [[ -f "$HOME/AI/chatgpt-wakeup.sh" ]]; then
  sed -n '1,260p' "$HOME/AI/chatgpt-wakeup.sh"
else
  echo "MISSING:$HOME/AI/chatgpt-wakeup.sh"
fi

echo
echo "=== FIREFOX WINDOWS ==="
mapfile -t WINS < <(xdotool search --onlyvisible --class firefox 2>/dev/null | sort -u || true)
printf 'count=%d\n' "${#WINS[@]}"
for w in "${WINS[@]}"; do
  title="$(xdotool getwindowname "$w" 2>/dev/null || true)"
  printf 'window=%s title=%q\n' "$w" "$title"
done

echo
echo "=== FIREFOX PROCESSES ==="
ps -ef | grep -E '[f]irefox' | head -40 || true
