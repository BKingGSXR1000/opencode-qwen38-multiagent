#!/usr/bin/env bash
set -Eeuo pipefail
echo "=== INGRESS RECENT ==="
journalctl --user -u chatgpt-job-ingress.service --since "10 minutes ago" --no-pager -o short-iso | tail -120 || true
echo
echo "=== RUNNER RECENT ==="
journalctl --user -u github-chatgpt-runner.service --since "10 minutes ago" --no-pager -o short-iso | tail -80 || true
echo "JOB_000027_OK"
