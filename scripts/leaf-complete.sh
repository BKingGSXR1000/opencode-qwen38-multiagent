#!/usr/bin/env bash
set -Eeuo pipefail
if [[ $# -ne 1 ]]; then
  echo "Usage: leaf-complete.sh Dxxx" >&2
  exit 2
fi
DID="$1"
[[ "$DID" =~ ^D[0-9]{3}(-[AB][12]?)?$ ]] || { echo "ERROR: deliverable must be a canonical split ID" >&2; exit 2; }
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
children=leaf.get("split_children",[])
if children:
    if not isinstance(children,list) or len(children)!=2: raise SystemExit(f"ERROR: invalid split children for {did}")
    def complete(child):
        p=__import__('pathlib').Path('.opencode-v2/work') / f'{child}.ready'
        if not p.exists(): return False
        data=dict(line.split('=',1) for line in p.read_text(errors='replace').splitlines() if '=' in line)
        return data.get('status')=='complete' and data.get('deliverable')==child and data.get('verified')=='true'
    missing=[child for child in children if not complete(child)]
    if missing: raise SystemExit(f"ERROR: {did} split children are not ready: {', '.join(missing)}")
cmd=(leaf.get("verify_command") or "").strip()
if not cmd: raise SystemExit(f"ERROR: {did} has no Verify command")
ent=(a.get("deliverables") or {}).get(did) or {}
attempt=int(ent.get("count") or 0)
if a.get("owner") != "supervisor": raise SystemExit("ERROR: attempt ledger owner is not supervisor")
if not isinstance(ent.get("sessions"),list) or not ent["sessions"]: raise SystemExit(f"ERROR: no supervisor claim for {did}")
try:
    automatic_limit=int(ent.get("automatic_limit",3))
    operator_grants=int(ent.get("operator_retry_grants") or 0)
    infrastructure_grants=int(ent.get("infrastructure_retry_grants") or 0)
except (TypeError,ValueError): raise SystemExit(f"ERROR: invalid attempt ledger for {did}")
overrides=ent.get("operator_overrides",[])
if not isinstance(overrides,list): raise SystemExit(f"ERROR: invalid operator grants for {did}")
valid_overrides=all(isinstance(x,dict) and x.get("source")=="operator-cli" and
                    x.get("timestamp") and x.get("reason") and x.get("grant")==1
                    for x in overrides)
infra=ent.get("infrastructure_failures",[])
if not isinstance(infra,list): raise SystemExit(f"ERROR: invalid infrastructure failures for {did}")
allowed_infra_kinds={"opencode-compaction-template","supervisor-compaction-retire","runtime-cancel"}
valid_infra=all(isinstance(x,dict) and x.get("source")=="supervisor" and
                x.get("kind") in allowed_infra_kinds and x.get("timestamp") and
                isinstance(x.get("session"),str) and x.get("session") and
                x.get("evidence")=="no-owned-artifact-or-progress" and x.get("grant")==1
                for x in infra)
operator_attempts=ent.get("operator_retry_attempts",[])
if not isinstance(operator_attempts,list): raise SystemExit(f"ERROR: invalid operator retry attempts for {did}")
seen=set(); aborted=0; valid_operator_attempts=True
for item in operator_attempts:
    if not isinstance(item,dict): valid_operator_attempts=False; break
    try: sequence=int(item.get("sequence"))
    except (TypeError,ValueError): valid_operator_attempts=False; break
    status=item.get("state")
    if (sequence <= automatic_limit or sequence in seen or not isinstance(item.get("session"),str) or
        not item["session"] or item.get("source")!="supervisor" or
        status not in {"reserved","consumed","infrastructure_abort","infrastructure_blocked"}):
        valid_operator_attempts=False; break
    seen.add(sequence)
    if status=="consumed" and item.get("consumes_operator_grant") is not True: valid_operator_attempts=False; break
    if status in {"reserved","infrastructure_abort","infrastructure_blocked"} and item.get("consumes_operator_grant") is not False:
        valid_operator_attempts=False; break
    if status in {"infrastructure_abort","infrastructure_blocked"} and not item.get("outcome"):
        valid_operator_attempts=False; break
    aborted += status=="infrastructure_abort"
operator_dispatches=max(0,attempt-automatic_limit-infrastructure_grants)
if len(operator_attempts)>operator_dispatches or aborted>1: valid_operator_attempts=False
if (automatic_limit not in (2,3) or operator_grants < 0 or infrastructure_grants < 0 or
    infrastructure_grants > 1 or len(overrides) != operator_grants or not valid_overrides or
    len(infra) != infrastructure_grants or not valid_infra or
    not valid_operator_attempts or attempt < 1 or
    attempt > automatic_limit + operator_grants + infrastructure_grants + aborted):
    raise SystemExit(f"ERROR: invalid attempt count {attempt}")
print(attempt); print(cmd)
PY
)" || exit $?
readarray -t META <<<"$META_TEXT"
ATTEMPT="${META[0]}"
VERIFY_CMD="${META[1]}"
python3 - "$GUARD" "$DID" <<'PY'
import hashlib,json,pathlib,re,sys
guard=json.load(open(sys.argv[1])); did=sys.argv[2]
baseline=pathlib.Path('.opencode-v2/work') / f'{did}.ownership-baseline.json'
# Manual/legacy projects predate pre-dispatch baselines. Every normal V2
# dispatch creates one before provider execution, so its completion is strict.
if not baseline.exists(): raise SystemExit(0)
data=json.load(open(baseline))
if data.get('owner')!='supervisor' or data.get('deliverable')!=did or not isinstance(data.get('files'),dict):
    raise SystemExit(f'ERROR: invalid ownership baseline for {did}')
def scan():
    result={}
    for p in pathlib.Path('.').rglob('*'):
        if not p.is_file() or '.git' in p.parts or str(p).startswith('.opencode-v2/work/'):
            continue
        try: result[p.as_posix()]=hashlib.sha256(p.read_bytes()).hexdigest()
        except OSError: pass
    return result
raw=(guard.get('leaves',{}).get(did) or {}).get('owned_artifacts','')
items=[x.strip().rstrip('.,;:') for x in re.findall(r'`([^`]+)`',raw)]
if not items: items=[x.strip().strip('`').rstrip('.,;:') for x in raw.split(',')]
allowed=[x for x in items if x and x.lower() not in {'none','n/a','-','—'}]
allowed.append(f'.opencode-v2/work/{did}.progress.md')
before=data['files']; after=scan(); changed=set(before)^set(after)
changed.update(p for p in set(before)&set(after) if before[p]!=after[p])
bad=sorted(p for p in changed if not any(p==x or p.startswith(x.rstrip('/') + '/') for x in allowed))
if bad: raise SystemExit(f"ERROR: {did} modified unowned artifact(s): {', '.join(bad[:8])}")
PY
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
protocol=V2.6.9
EOF
mv -f "$TMP" "$READY"
echo "${DID}_READY attempt=${ATTEMPT}"
