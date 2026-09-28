set -Eeuo pipefail
ROOT=/home/bking/AI/opencode-qwen38-multiagent-v2-a2-v11831
cd "$ROOT"

slice () {
  local file="$1" lo="$2" hi="$3"
  echo "=== $file:$lo-$hi ==="
  sed -n "${lo},${hi}p" "$file" | nl -ba -v "$lo"
}

slice scripts/supervisor.py 6170 6665
slice scripts/supervisor.py 680 850
slice scripts/supervisor.py 12970 13045
slice scripts/test_state_machine_invariants.py 1640 1910
slice scripts/test_state_machine_invariants.py 4090 4165
