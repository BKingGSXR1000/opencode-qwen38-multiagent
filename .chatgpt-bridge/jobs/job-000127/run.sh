set -Eeuo pipefail
WOKEN=/home/bking/AI/github-chatgpt-runner/state/woken
mkdir -p "$WOKEN"

for n in $(seq 120 126); do
  j=$(printf 'job-%06d' "$n")
  printf 'historical-callback-drained\n' > "$WOKEN/$j"
done

echo "=== MARKED 120-126 ==="
for n in $(seq 120 126); do
  j=$(printf 'job-%06d' "$n")
  printf '%s ' "$j"
  test -e "$WOKEN/$j" && echo marked || echo MISSING
done

echo
echo "=== RUNNER SERVICE ==="
systemctl --user is-active github-chatgpt-runner.service
echo "BACKLOG_DRAINED_120_126"
