set -euo pipefail
echo "=== WAKE SCRIPT ==="
sed -n '1,240p' /home/bking/AI/chatgpt-wakeup.sh 2>/dev/null || true
echo
echo "=== RUNNER JOURNAL ==="
journalctl --user -u github-chatgpt-runner.service -n 120 --no-pager 2>/dev/null || true
echo
echo "=== SPOOL 102-107 ==="
for j in job-000102 job-000103 job-000104 job-000105 job-000106 job-000107; do
  echo "--- $j ---"
  find /home/bking/AI/github-chatgpt-runner/spool/$j -maxdepth 1 -type f -printf '%f\n' 2>/dev/null | sort || true
  if [ -f /home/bking/AI/github-chatgpt-runner/spool/$j/WOKEN ]; then
    echo "WOKEN=yes"
    cat /home/bking/AI/github-chatgpt-runner/spool/$j/WOKEN || true
  else
    echo "WOKEN=no"
  fi
done
echo
echo "=== FIREFOX WINDOWS ==="
for w in $(xdotool search --onlyvisible --class firefox 2>/dev/null | sort -u); do
  printf 'window=%s title=' "$w"
  xdotool getwindowname "$w" 2>/dev/null || true
done
