#!/usr/bin/env python3
import argparse,json,os,re,shlex,stat,subprocess,sys,tempfile,time
from pathlib import Path, PurePosixPath
from control_state import IMPLEMENTATION_PLAN_SCAFFOLD
from leaf_contract import validate_verify_command
from state_io import atomic_write_text
from test_checks_contract import RUN_CHECKS_COMMAND, TEST_CHECKS_SCHEMA

HARNESS_ROOT=Path(__file__).resolve().parents[1]

def atomic_write(path,text): atomic_write_text(path,text)
def slug(s): return re.sub(r"[^A-Za-z0-9._-]+","-",s).strip("-") or "check"

def validate_schema(value,schema,path="manifest"):
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
    validate_schema(spec,TEST_CHECKS_SCHEMA)
    return spec["checks"],spec.get("required_files",[])

def validate_required_file(project: Path, raw: str):
    raw=str(raw or "").strip()
    p=PurePosixPath(raw)
    if not raw or p.is_absolute() or raw.startswith(("./","~")) or "\\" in raw:
        raise ValueError(f"required_files entry must be a canonical project-relative path: {raw!r}")
    if any(part in ("",".","..") for part in p.parts):
        raise ValueError(f"required_files entry escapes/is not canonical: {raw!r}")
    target=(project/Path(*p.parts)).resolve(strict=False)
    try: target.relative_to(project.resolve())
    except ValueError: raise ValueError(f"required_files entry resolves outside project: {raw!r}")
    return target

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

Every `checks[].command` is also validated by the same fail-closed Verify-command
policy used by implementation leaves and is executed with `bash -euo pipefail`.
Commands that mask failure (`||`, `set +e`, forced `exit 0`, non-verifying
commands) are rejected. Every `required_files[]` path must be canonical,
project-relative, and resolve inside the project.

Run the manifest with exact project-local command `{RUN_CHECKS_COMMAND}`.
Implementation workers do not create readiness sentinels and do not invoke a
leaf-completion command. After the worker returns, the supervisor re-runs the
exact leaf Verify command and alone mints `.opencode-v2/work/Dxxx.ready`.

## Filesystem control protocol

- Bootstrap creates `.opencode-v2/IMPLEMENTATION_PLAN.md` as an explicitly
  incomplete scaffold before implementation planning.
- The deterministic control guard alone creates `.opencode-v2/ACCEPTANCE.ready`
  and `.opencode-v2/IMPLEMENTATION_PLAN.ready`.
- A leaf is complete only after supervisor verification and supervisor-owned
  readiness creation.
- The supervisor alone owns `.opencode-v2/work/attempts.json` and all other
  `.opencode-v2/work/` and `.opencode-v2/bin/` control state.
- `.opencode-v2/work/Dxxx.progress.md` is the durable retry handoff.
- `.opencode-v2/TEST_REPORT.json` with `status=pass`, internally consistent
  successful check results, and `checks_run > 0` is required final-test evidence.
- Final acceptance is not a model token alone: deterministic finalization must
  mint a hash-bound `.opencode-v2/acceptance-pass.json`.
"""

def wrapper_text(target,args):
    quoted=" ".join(shlex.quote(str(x)) for x in (target,*args))
    return "#!/usr/bin/env bash\nset -Eeuo pipefail\n" + f"exec {quoted} \"$@\"\n"

def bootstrap_control_surface(project):
    ctrl=project/".opencode-v2"
    atomic_write(ctrl/"CONTROL_CONTRACT.md",control_contract_text())
    plan=ctrl/"IMPLEMENTATION_PLAN.md"
    if not plan.exists(): atomic_write(plan,IMPLEMENTATION_PLAN_SCAFFOLD)
    wrappers={
        ctrl/"bin"/"run-checks":wrapper_text(sys.executable,(HARNESS_ROOT/"scripts"/"run-checks.py","--project",project)),
        ctrl/"bin"/"control-status":wrapper_text(sys.executable,(HARNESS_ROOT/"scripts"/"control-status.py","--project",project)),
    }
    (ctrl/"bin"/"leaf-complete").unlink(missing_ok=True)
    for path,text in wrappers.items():
        atomic_write(path,text); path.chmod(path.stat().st_mode|stat.S_IXUSR|stat.S_IXGRP|stat.S_IXOTH)
    return wrappers

def run_checks(project: Path):
    ctrl=project/".opencode-v2"; spec_path=ctrl/"TEST_CHECKS.json"; report_path=ctrl/"TEST_REPORT.json"; logs=ctrl/"test-logs"
    if not spec_path.exists(): raise ValueError("TEST_CHECKS.json missing")
    try: spec=json.loads(spec_path.read_text()); checks,required=validate_test_checks(spec)
    except (ValueError,json.JSONDecodeError) as e: raise ValueError(f"invalid TEST_CHECKS.json: {e}")
    required_targets=[]
    for raw in required: required_targets.append((raw,validate_required_file(project,raw)))
    missing=[raw for raw,target in required_targets if not target.exists()]
    results=[]; logs.mkdir(parents=True,exist_ok=True)
    for i,ch in enumerate(checks,1):
        name=ch["name"].strip(); cmd=ch["command"].strip(); timeout=ch.get("timeout_seconds",180)
        start=time.time(); timed_out=False; unsafe=validate_verify_command(cmd)
        if unsafe:
            rc=125; stdout=""; stderr="unsafe test command: "+"; ".join(unsafe)
        else:
            try:
                p=subprocess.run(["/bin/bash","-euo","pipefail","-c",cmd],cwd=project,text=True,capture_output=True,timeout=timeout)
                rc=p.returncode; stdout=p.stdout or ""; stderr=p.stderr or ""
            except subprocess.TimeoutExpired as e:
                rc=124; stdout=e.stdout or ""; stderr=e.stderr or ""; timed_out=True
        log=logs/f"{i:02d}-{slug(name)}.log"
        atomic_write(log,f"$ {cmd}\nexit={rc} timeout={timed_out}\n\n--- stdout ---\n{stdout}\n--- stderr ---\n{stderr}\n")
        results.append({"name":name,"command":cmd,"exit_code":rc,"timed_out":timed_out,"duration_seconds":round(time.time()-start,3),"log":str(log.relative_to(project))})
    passed=not missing and all(x["exit_code"]==0 and not x["timed_out"] for x in results)
    report={"protocol":"v2-test-report-v1","status":"pass" if passed else "fail","checks_run":len(results),"checks_passed":sum(1 for x in results if x["exit_code"]==0 and not x["timed_out"]),"missing_required_files":missing,"checks":results}
    atomic_write(report_path,json.dumps(report,indent=2,sort_keys=True)+"\n")
    return report, 0 if passed else 1

def selftest():
    with tempfile.TemporaryDirectory() as td:
        project=Path(td); (project/".opencode-v2").mkdir()
        (project/"ok.txt").write_text("ok\n")
        for bad in ("../etc/passwd","/etc/passwd","./ok.txt"):
            try: validate_required_file(project,bad); raise AssertionError(bad)
            except ValueError: pass
        (project/".opencode-v2/TEST_CHECKS.json").write_text(json.dumps({"checks":[{"name":"masked","command":"false; true"}],"required_files":["ok.txt"]}))
        report,rc=run_checks(project)
        assert rc!=0 and report["status"]=="fail" and report["checks"][0]["exit_code"]!=0, report
        (project/".opencode-v2/TEST_CHECKS.json").write_text(json.dumps({"checks":[{"name":"ok","command":"test -s ok.txt"}],"required_files":["ok.txt"]}))
        report,rc=run_checks(project)
        assert rc==0 and report["checks_passed"]==1, report
    print("run-checks selftest: OK")

def main():
    ap=argparse.ArgumentParser(); ap.add_argument("--project",default=".")
    ap.add_argument("--bootstrap-control-contract",action="store_true")
    ap.add_argument("--print-test-checks-schema",action="store_true")
    ap.add_argument("--selftest",action="store_true")
    args=ap.parse_args()
    if args.selftest: selftest(); return
    if args.print_test_checks_schema: print(json.dumps(TEST_CHECKS_SCHEMA,indent=2,sort_keys=True)); return
    project=Path(args.project).resolve(); ctrl=project/".opencode-v2"
    if args.bootstrap_control_contract:
        wrappers=bootstrap_control_surface(project)
        print(f"CONTROL_CONTRACT_READY {ctrl/'CONTROL_CONTRACT.md'}")
        print(f"IMPLEMENTATION_PLAN_SCAFFOLD_READY {ctrl/'IMPLEMENTATION_PLAN.md'}")
        for path in wrappers: print(f"CONTROL_COMMAND_READY {path}")
        return
    try: report,rc=run_checks(project)
    except ValueError as e: raise SystemExit(f"ERROR: {e}")
    print(json.dumps(report,indent=2)); raise SystemExit(rc)
if __name__=="__main__": main()
