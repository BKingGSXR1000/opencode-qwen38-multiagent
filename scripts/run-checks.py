#!/usr/bin/env python3
import argparse,json,os,re,shlex,stat,subprocess,sys,time
from pathlib import Path
from control_state import IMPLEMENTATION_PLAN_SCAFFOLD

HARNESS_ROOT=Path(__file__).resolve().parents[1]
RUN_CHECKS_COMMAND=".opencode-v2/bin/run-checks"

TEST_CHECKS_SCHEMA={
    "$schema":"https://json-schema.org/draft/2020-12/schema",
    "title":"V2 TEST_CHECKS.json",
    "type":"object",
    "additionalProperties":False,
    "required":["checks"],
    "properties":{
        "checks":{
            "type":"array",
            "minItems":1,
            "items":{
                "type":"object",
                "additionalProperties":False,
                "required":["name","command"],
                "properties":{
                    "name":{"type":"string","minLength":1},
                    "command":{"type":"string","minLength":1},
                    "timeout_seconds":{"type":"integer","minimum":1},
                },
            },
        },
        "required_files":{
            "type":"array",
            "items":{"type":"string","minLength":1},
        },
    },
}

def atomic_write(path,text):
    path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_suffix(path.suffix+".tmp"); tmp.write_text(text); os.replace(tmp,path)

def slug(s): return re.sub(r"[^A-Za-z0-9._-]+","-",s).strip("-") or "check"

def validate_schema(value,schema,path="manifest"):
    """Small validator for the schema features used by this public contract."""
    kind=schema.get("type")
    if kind=="object":
        if not isinstance(value,dict): raise ValueError(f"{path} must be an object")
        properties=schema.get("properties",{})
        if schema.get("additionalProperties") is False:
            extra=set(value)-set(properties)
            if extra: raise ValueError(f"{path} has unknown field(s): {', '.join(sorted(extra))}")
        for field in schema.get("required",[]):
            if field not in value: raise ValueError(f"{path}.{field} is required")
        for field,item in value.items():
            if field in properties: validate_schema(item,properties[field],f"{path}.{field}")
        return
    if kind=="array":
        if not isinstance(value,list): raise ValueError(f"{path} must be an array")
        if len(value)<schema.get("minItems",0): raise ValueError(f"{path} must not be empty")
        for index,item in enumerate(value,1): validate_schema(item,schema["items"],f"{path}[{index}]")
        return
    if kind=="string":
        if not isinstance(value,str): raise ValueError(f"{path} must be a string")
        if len(value)<schema.get("minLength",0): raise ValueError(f"{path} must not be empty")
        return
    if kind=="integer":
        if isinstance(value,bool) or not isinstance(value,int): raise ValueError(f"{path} must be an integer")
        if value<schema.get("minimum",float("-inf")): raise ValueError(f"{path} is below minimum")
        return
    raise ValueError(f"unsupported schema type for {path}: {kind!r}")

def validate_test_checks(spec):
    """Validate from the same schema emitted into CONTROL_CONTRACT.md."""
    validate_schema(spec,TEST_CHECKS_SCHEMA)
    return spec["checks"],spec.get("required_files",[])

def control_contract_text():
    schema=json.dumps(TEST_CHECKS_SCHEMA,indent=2,sort_keys=True)
    return f"""# V2 project-local control contract

This deterministic file is generated at V2 project bootstrap by the current
test runner. It is the only control-protocol reference agents need inside this
project. Do not inspect harness-repository source to infer these rules.

## TEST_CHECKS.json

The final test leaf writes `.opencode-v2/TEST_CHECKS.json`. The canonical runner
mechanically validates it against this exact machine-readable JSON Schema before
running any command:

```json
{schema}
```

Use one `checks[]` entry per intended test command. Run it with the exact
project-local command `{RUN_CHECKS_COMMAND}`. Implementation workers do not
create readiness sentinels and do not invoke a leaf-completion command. After
the worker returns, the supervisor re-runs the exact leaf Verify command and
alone mints `.opencode-v2/work/Dxxx.ready`. Never inspect the wrapper or harness
source merely to infer this contract. Do not create a probe deliverable to
discover this schema and do not guess or substitute a fallback manifest format.

## Filesystem control protocol

- Bootstrap creates `.opencode-v2/IMPLEMENTATION_PLAN.md` as an explicitly
  incomplete scaffold before implementation planning. Planners progressively
  fill that same file; an untouched scaffold has no completion marker and can
  never qualify for `IMPLEMENTATION_PLAN.ready`.
- The deterministic control guard alone creates `.opencode-v2/ACCEPTANCE.ready`
  and `.opencode-v2/IMPLEMENTATION_PLAN.ready`; agents never create, modify, or
  request either sentinel.
- A leaf is complete only after the supervisor has re-run its exact Verify
  command, checked ownership, and minted `.opencode-v2/work/Dxxx.ready`.
  Agents never create, modify, request, or emulate leaf-ready sentinels.
- The supervisor alone owns `.opencode-v2/work/attempts.json`; an exact Dxxx has
  at most three automatic implementation attempts. A pre-dispatch denial has
  no claim. At most one supervisor-recorded OpenCode compaction-template
  failure with no owned/progress artifact may receive a separately auditable
  recovery slot. Only a human running the trusted external
  `./scripts/operator-control.py --project <project> retry-failed` (or `retry
  Dxxx ...`) can record one additional auditable attempt; agents never invoke or
  emulate that command. A human grant is reserved before child launch and is
  consumed only after durable state or a completed worker tool action. One proven zero-work runtime
  cancellation releases that same reservation without decrementing historical
  dispatch count; a repeated cancellation is execution-blocked infrastructure
  until a human explicitly acts. Never edit, repair, or create alternative/salvage IDs.
  New ledgers identify this stable schema as `v2-attempt-ledger-v1`; legacy
  `V2.6.7` is a historical ledger label, not the active harness release.
- `.opencode-v2/work/Dxxx.progress.md` is the durable retry handoff. Read it
  when present before re-deriving work.
- `.opencode-v2/TEST_REPORT.json` with `status=pass` and `checks_run > 0` is the
  required final-test evidence. The only success verdict is exact bare
`ACCEPTANCE_PASS`.
"""

def wrapper_text(target,args):
    quoted=" ".join(shlex.quote(str(x)) for x in (target,*args))
    return "#!/usr/bin/env bash\nset -Eeuo pipefail\n" + f"exec {quoted} \"$@\"\n"

def bootstrap_control_surface(project):
    """Generate project-local delegates to the canonical harness implementation."""
    ctrl=project/".opencode-v2"
    atomic_write(ctrl/"CONTROL_CONTRACT.md",control_contract_text())
    # The planner always starts from durable, explicitly incomplete state.  Do
    # not overwrite a partial plan on a later bootstrap/restart.
    plan=ctrl/"IMPLEMENTATION_PLAN.md"
    if not plan.exists(): atomic_write(plan,IMPLEMENTATION_PLAN_SCAFFOLD)
    wrappers={
        ctrl/"bin"/"run-checks":wrapper_text(
            sys.executable,(HARNESS_ROOT/"scripts"/"run-checks.py","--project",project)
        ),
        ctrl/"bin"/"control-status":wrapper_text(
            sys.executable,(HARNESS_ROOT/"scripts"/"control-status.py","--project",project)
        ),
    }
    legacy_leaf_complete=ctrl/"bin"/"leaf-complete"
    legacy_leaf_complete.unlink(missing_ok=True)
    for path,text in wrappers.items():
        atomic_write(path,text)
        path.chmod(path.stat().st_mode|stat.S_IXUSR|stat.S_IXGRP|stat.S_IXOTH)
    return wrappers

def main():
    ap=argparse.ArgumentParser(); ap.add_argument("--project",default=".")
    ap.add_argument("--bootstrap-control-contract",action="store_true")
    ap.add_argument("--print-test-checks-schema",action="store_true")
    args=ap.parse_args()
    if args.print_test_checks_schema:
        print(json.dumps(TEST_CHECKS_SCHEMA,indent=2,sort_keys=True)); return
    project=Path(args.project).resolve(); ctrl=project/".opencode-v2"
    if args.bootstrap_control_contract:
        wrappers=bootstrap_control_surface(project)
        print(f"CONTROL_CONTRACT_READY {ctrl/'CONTROL_CONTRACT.md'}")
        print(f"IMPLEMENTATION_PLAN_SCAFFOLD_READY {ctrl/'IMPLEMENTATION_PLAN.md'}")
        for path in wrappers: print(f"CONTROL_COMMAND_READY {path}")
        return
    spec_path=ctrl/"TEST_CHECKS.json"; report_path=ctrl/"TEST_REPORT.json"; logs=ctrl/"test-logs"
    if not spec_path.exists(): raise SystemExit("ERROR: TEST_CHECKS.json missing")
    try: spec=json.loads(spec_path.read_text()); checks,required=validate_test_checks(spec)
    except (ValueError,json.JSONDecodeError) as e: raise SystemExit(f"ERROR: invalid TEST_CHECKS.json: {e}")
    missing=[p for p in required if not (project/p).exists()]
    results=[]; logs.mkdir(parents=True,exist_ok=True)
    for i,ch in enumerate(checks,1):
        name=ch["name"].strip(); cmd=ch["command"].strip(); timeout=ch.get("timeout_seconds",180)
        start=time.time(); timed_out=False
        try:
            p=subprocess.run(cmd,cwd=project,shell=True,executable="/bin/bash",text=True,capture_output=True,timeout=timeout)
            rc=p.returncode; stdout=p.stdout or ""; stderr=p.stderr or ""
        except subprocess.TimeoutExpired as e:
            rc=124; stdout=e.stdout or ""; stderr=e.stderr or ""; timed_out=True
        log=logs/f"{i:02d}-{slug(name)}.log"
        log.write_text(f"$ {cmd}\nexit={rc} timeout={timed_out}\n\n--- stdout ---\n{stdout}\n--- stderr ---\n{stderr}\n")
        results.append({"name":name,"command":cmd,"exit_code":rc,"timed_out":timed_out,"duration_seconds":round(time.time()-start,3),"log":str(log.relative_to(project))})
    passed=not missing and all(x["exit_code"]==0 for x in results)
    report={"protocol":"V2.6.7","status":"pass" if passed else "fail","checks_run":len(results),"checks_passed":sum(1 for x in results if x["exit_code"]==0),"missing_required_files":missing,"checks":results}
    atomic_write(report_path,json.dumps(report,indent=2)+"\n"); print(json.dumps(report,indent=2)); raise SystemExit(0 if passed else 1)
if __name__=="__main__": main()
