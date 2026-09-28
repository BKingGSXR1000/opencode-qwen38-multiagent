set -Eeuo pipefail
ROOT=/home/bking/AI/opencode-qwen38-multiagent-v2-a2-v11831
cd "$ROOT"

echo "=== GIT STATUS ==="
git status --short

echo
echo "=== RECOVERY FUNCTION LOCATIONS ==="
grep -n -E 'def recover_implementation_max_step_continuation|def max_step_terminal_summary_db|def trusted_verify_reverify_visibility_evidence|def record_implementation_max_step_continuation|contract.challenge|CONTRACT_CHALLENGE_REQUIRED|recovery_kind' scripts/*.py | head -260 || true

echo
echo "=== RECOVERY FUNCTION BODY ==="
python3 - <<'PY'
from pathlib import Path
for path in [Path("scripts/control_state.py"), Path("scripts/stage_a_controller.py"), Path("scripts/supervisor.py")]:
    if not path.exists():
        continue
    lines=path.read_text().splitlines()
    for i,line in enumerate(lines):
        if "def recover_implementation_max_step_continuation" in line:
            lo=max(0,i-20); hi=min(len(lines),i+260)
            print(f"--- {path}:{lo+1}-{hi} ---")
            print("\n".join(f"{n+1}:{lines[n]}" for n in range(lo,hi)))
PY

echo
echo "=== RELATED TESTS ==="
grep -n -E 'max.step.continuation|contract.challenge|required|latest.verify.clean.tail|reverify.single.write.cadence' scripts/test_*.py | head -260 || true
