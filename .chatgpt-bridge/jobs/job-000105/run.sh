set -euo pipefail
REPO=/home/bking/AI/opencode-qwen38-multiagent-v2-a2-v11831
PROJECT=/home/bking/AI/a2-e2e/20260928-final-current-tree-proof2/project
cd "$REPO"

echo "=== SUPERVISOR RECOVERY SYMBOLS ==="
grep -n -E 'recover_implementation_max_step_continuation|def .*max_step|class .*upervisor|class ' scripts/supervisor.py | head -120 || true

echo
echo "=== CALL SITES ==="
grep -R -n -E 'recover_implementation_max_step_continuation|max_step_continuation' scripts | head -200 || true

echo
echo "=== CONTROL QUERY USAGE ==="
python3 scripts/control_query_views.py --help 2>&1 | head -120 || true
python3 scripts/stage_a_controller.py --help 2>&1 | head -160 || true
python3 scripts/drive-stage-a-run.py --help 2>&1 | head -180 || true

echo
echo "=== PROJECT .opencode-v2 TOP ==="
find "$PROJECT/.opencode-v2" -maxdepth 2 -type f -printf '%P\n' 2>/dev/null | sort | head -250

echo
echo "=== MATCH D011-B2 / ATTEMPT LEDGER ==="
find "$PROJECT/.opencode-v2" -type f \( -iname '*D011-B2*' -o -iname '*attempt*ledger*' -o -iname '*control*state*' \) -printf '%p\n' 2>/dev/null | sort
