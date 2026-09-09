#!/usr/bin/env bash
set -Eeuo pipefail
if [[ $# -ne 1 ]]; then
  echo "Usage: leaf-complete.sh Dxxx" >&2
  exit 2
fi
DID="$1"
[[ "$DID" =~ ^D[0-9]{3}$ ]] || { echo "ERROR: deliverable must be exact Dxxx" >&2; exit 2; }
ROOT="${HOME}/AI/opencode-qwen38-multiagent-v2"
GUARD=".opencode-v2/IMPLEMENTATION_PLAN.guard.json"
ATTEMPTS=".opencode-v2/work/attempts.json"
[[ -f "$GUARD" ]] || { echo "ERROR: validated plan manifest missing" >&2; exit 2; }
[[ -f "$ATTEMPTS" ]] || { echo "ERROR: attempt ledger missing" >&2; exit 2; }
META_TEXT="$(python3 - "$GUARD" "$ATTEMPTS" "$DID" <<'PY'
import json,sys
g=json.load(open(sys.argv[1])); a=json.load(open(sys.argv[2])); did=sys.argv[3]
leaf=(g.get("leaves") or {}).get(did)
if not leaf: raise SystemExit(f"ERROR: {did} not in validated plan")
cmd=(leaf.get("verify_command") or "").strip()
if not cmd: raise SystemExit(f"ERROR: {did} has no Verify command")
ent=(a.get("deliverables") or {}).get(did) or {}
attempt=int(ent.get("count") or 0)
if attempt < 1 or attempt > 3: raise SystemExit(f"ERROR: invalid attempt count {attempt}")
print(attempt); print(cmd)
PY
)" || exit $?
readarray -t META <<<"$META_TEXT"
ATTEMPT="${META[0]}"
VERIFY_CMD="${META[1]}"
echo "=== ${DID} deterministic verification (attempt ${ATTEMPT}) ==="
bash -lc "$VERIFY_CMD"
mkdir -p .opencode-v2/work
TMP=".opencode-v2/work/${DID}.ready.tmp"
READY=".opencode-v2/work/${DID}.ready"
cat > "$TMP" <<EOF
status=complete
deliverable=${DID}
attempt=${ATTEMPT}
verified=true
protocol=V2.6.7
EOF
mv -f "$TMP" "$READY"
echo "${DID}_READY attempt=${ATTEMPT}"
