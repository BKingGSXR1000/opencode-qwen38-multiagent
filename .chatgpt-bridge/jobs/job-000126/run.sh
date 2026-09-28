set -Eeuo pipefail
STATE=/home/bking/AI/github-chatgpt-runner/state/woken
RUNNER=/home/bking/AI/github-chatgpt-runner/runner.py

echo "=== WOKEN MARKERS 113-125 ==="
for n in $(seq 113 125); do
  j=$(printf 'job-%06d' "$n")
  if [[ -e "$STATE/$j" ]]; then
    echo "$j marked"
  else
    echo "$j MISSING"
  fi
done

echo
echo "=== RUNNER WAKE SYMBOLS ==="
grep -n -E 'WOKEN|woken_path|wake|chatgpt-wakeup|CONTINUE|result_commit' "$RUNNER" | head -180 || true

echo
echo "=== RECENT RUNNER LOG ==="
journalctl --user -u github-chatgpt-runner.service --since '2026-09-28 16:00:00' --no-pager \
  | grep -E 'job-0001(1[3-9]|2[0-5])|wake|WOKEN|CONTINUE' | tail -220 || true
