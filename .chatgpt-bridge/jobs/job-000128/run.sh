set -Eeuo pipefail
echo "JOB_000128_CLEAN_E2E_WAKE_TEST"
echo "INGRESS=$(systemctl --user is-active chatgpt-job-ingress.service)"
echo "RUNNER=$(systemctl --user is-active github-chatgpt-runner.service)"
