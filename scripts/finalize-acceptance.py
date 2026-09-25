#!/usr/bin/env python3
import argparse,hashlib,json,os,re,subprocess,sys,tempfile
from datetime import datetime, timezone
from pathlib import Path
from acceptance_contract import must_acceptance_ids
from leaf_contract import validate_verify_command
from state_io import atomic_write_json
from worker_sandbox import run_validator_bash, SandboxError

PASS_PROTOCOL="v2-acceptance-pass-v1"
REPORT_PROTOCOL="v2-acceptance-report-v1"

def fail(msg):
    print(f"ACCEPTANCE_GATE_FAIL: {msg}",file=sys.stderr)
    raise RuntimeError(msg)

def sha(path): return hashlib.sha256(path.read_bytes()).hexdigest()

def prepare(project: Path):
    root=project/".opencode-v2"
    for name in ("acceptance-report.json","acceptance-pass.json","browser-evidence.json"):
        (root/name).unlink(missing_ok=True)
    shot=root/"acceptance"
    if shot.is_dir():
        for p in shot.glob("canvas-*.png"): p.unlink(missing_ok=True)
        (shot/"page.png").unlink(missing_ok=True)
    print("ACCEPTANCE_GATE_PREPARED")

def load_json(path,label):
    try: data=json.loads(path.read_text())
    except Exception as e: fail(f"invalid {label}: {e}")
    if not isinstance(data,dict): fail(f"{label} must be an object")
    return data

def validate_test_report(root: Path):
    path=root/"TEST_REPORT.json"
    if not path.exists(): fail("missing .opencode-v2/TEST_REPORT.json")
    data=load_json(path,"TEST_REPORT.json")
    checks=data.get("checks")
    run=data.get("checks_run"); passed=data.get("checks_passed")
    missing=data.get("missing_required_files")
    if data.get("status")!="pass": fail(f"TEST_REPORT status={data.get('status')!r}")
    if not isinstance(run,int) or isinstance(run,bool) or run<=0: fail("TEST_REPORT checks_run must be > 0")
    if not isinstance(passed,int) or isinstance(passed,bool) or passed!=run: fail("TEST_REPORT checks_passed must equal checks_run")
    if not isinstance(missing,list) or missing: fail(f"TEST_REPORT missing_required_files={missing!r}")
    if not isinstance(checks,list) or len(checks)!=run: fail("TEST_REPORT checks length mismatch")
    for i,item in enumerate(checks,1):
        if not isinstance(item,dict): fail(f"TEST_REPORT check {i} is not an object")
        if item.get("exit_code")!=0 or item.get("timed_out") is not False: fail(f"TEST_REPORT check {i} did not pass cleanly")
        cmd=item.get("command")
        if not isinstance(cmd,str) or not cmd.strip(): fail(f"TEST_REPORT check {i} missing command")
        unsafe=validate_verify_command(cmd)
        if unsafe: fail(f"TEST_REPORT check {i} unsafe command: {unsafe[0]}")
    return path,data

def finalize(project: Path):
    root=project/".opencode-v2"; plan_p=root/"ACCEPTANCE.md"; report_p=root/"acceptance-report.json"; pass_p=root/"acceptance-pass.json"; evidence_p=root/"browser-evidence.json"
    pass_p.unlink(missing_ok=True)
    if not plan_p.exists(): fail("missing .opencode-v2/ACCEPTANCE.md")
    if not report_p.exists(): fail("missing .opencode-v2/acceptance-report.json")
    plan=plan_p.read_text(errors="replace"); must=must_acceptance_ids(plan)
    if not must: fail("acceptance contract contains no exact '- [ ] Axxx: ...' MUST checks")
    if len(must)!=len(set(must)): fail("duplicate MUST IDs in acceptance contract")
    report=load_json(report_p,"acceptance-report.json")
    if report.get("protocol")!=REPORT_PROTOCOL: fail(f"acceptance-report protocol must be {REPORT_PROTOCOL}")
    checks=report.get("checks")
    if not isinstance(checks,list): fail("report.checks must be an array")
    by_id={}
    for c in checks:
        if not isinstance(c,dict): fail("every report check must be an object")
        cid=c.get("id")
        if cid in by_id: fail(f"duplicate report check {cid}")
        by_id[cid]=c
    if set(by_id)!=set(must): fail(f"report IDs do not exactly match MUST IDs; missing={sorted(set(must)-set(by_id))} extra={sorted(set(by_id)-set(must))}")
    bad=[]
    for cid in must:
        c=by_id[cid]; status=c.get("status"); evidence=str(c.get("evidence") or "").strip()
        if status!="PASS": bad.append(f"{cid}={status}")
        if len(evidence)<8: bad.append(f"{cid}=insufficient-evidence")
        executable=c.get("required_executable") is True or "command" in c or "exit_code" in c
        if executable:
            command=c.get("command"); exit_code=c.get("exit_code")
            if not isinstance(command,str) or not command.strip(): bad.append(f"{cid}=missing-executable-command")
            else:
                unsafe=validate_verify_command(command)
                if unsafe: bad.append(f"{cid}=unsafe-command:{unsafe[0]}")
            if not isinstance(exit_code,int) or isinstance(exit_code,bool): bad.append(f"{cid}=missing-executable-exit-code")
            elif exit_code!=0: bad.append(f"{cid}=reported-executable-exit-{exit_code}")
            if isinstance(command,str) and command.strip() and not validate_verify_command(command):
                try:
                    actual=run_validator_bash(project,f"acceptance-finalizer-{os.getpid()}-{cid}",command,timeout=180)
                except (SandboxError,subprocess.TimeoutExpired) as exc:
                    bad.append(f"{cid}=gate-execution-error:{type(exc).__name__}")
                else:
                    if actual!=0: bad.append(f"{cid}=gate-executable-exit-{actual}")
    if report.get("result")!="PASS": bad.append(f"report.result={report.get('result')!r}")
    test_p,_=validate_test_report(root)
    browserish=bool(re.search(r"\b(browser|web|render|visual|canvas|ui|page|three\.?js|webgl|animation|button|control)\b",plan,re.I))
    browser_summary=None
    if browserish:
        if not evidence_p.exists(): bad.append("missing-browser-evidence")
        else:
            try:
                ev=json.loads(evidence_p.read_text()); browser_summary={"http_status":ev.get("http_status"),"console_errors":len(ev.get("console_errors") or []),"page_errors":len(ev.get("page_errors") or []),"failed_requests":len(ev.get("failed_requests") or []),"screenshot_unique_colors":((ev.get("screenshot_stats") or {}).get("quantized_unique_colors")),"canvases":len(((ev.get("page") or {}).get("canvases") or []))}
                status=ev.get("http_status")
                if not isinstance(status,int) or status>=400: bad.append(f"browser-http={status}")
                if ev.get("page_errors"): bad.append("browser-page-errors")
                if ev.get("console_errors"): bad.append("browser-console-errors")
                colors=(ev.get("screenshot_stats") or {}).get("quantized_unique_colors")
                if not isinstance(colors,int) or colors<8: bad.append(f"effectively-blank-page colors={colors}")
                visible=[c for c in ((ev.get("page") or {}).get("canvases") or []) if c.get("visible") and (c.get("clientWidth") or 0)>=100 and (c.get("clientHeight") or 0)>=100]
                if visible:
                    stats=[x.get("stats") or {} for x in (ev.get("canvas_evidence") or []) if isinstance(x,dict) and "stats" in x]
                    if not stats: bad.append("visible-canvas-without-pixel-evidence")
                    elif max((s.get("quantized_unique_colors") or 0) for s in stats)<8: bad.append("effectively-blank-canvas")
            except Exception as e: bad.append(f"invalid-browser-evidence:{e}")
    if bad: fail("; ".join(bad))
    marker={"protocol":PASS_PROTOCOL,"result":"PASS","generated_at":datetime.now(timezone.utc).isoformat(),"must_checks":must,"must_count":len(must),"acceptance_sha256":sha(plan_p),"report_sha256":sha(report_p),"test_report_sha256":sha(test_p),"browser_evidence_sha256":sha(evidence_p) if browserish and evidence_p.exists() else "","browser_summary":browser_summary}
    atomic_write_json(pass_p,marker); print(f"ACCEPTANCE_GATE_PASS: {len(must)} MUST checks passed")
    return marker

def selftest():
    base=Path.home()/".local/share/v2-worker-sandbox/selftest-finalizer"; base.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(dir=base) as td:
        project=Path(td); root=project/".opencode-v2"; root.mkdir()
        (root/"ACCEPTANCE.md").write_text("# Acceptance Contract\nReference policy: none\n## MUST checks\n- [ ] A001: Works\n## SHOULD checks\n- [ ] A002: Optional polish\n<!-- ACCEPTANCE_COMPLETE -->\n")
        (root/"acceptance-report.json").write_text(json.dumps({"protocol":REPORT_PROTOCOL,"result":"PASS","checks":[{"id":"A001","status":"PASS","evidence":"deterministic evidence","required_executable":True,"command":"test -e .opencode-v2/ACCEPTANCE.md","exit_code":0}]}))
        (root/"TEST_REPORT.json").write_text(json.dumps({"protocol":"v2-test-report-v1","status":"pass","checks_run":1,"checks_passed":1,"missing_required_files":[],"checks":[{"name":"x","command":"test -e .opencode-v2/ACCEPTANCE.md","exit_code":0,"timed_out":False}]}))
        marker=finalize(project); assert marker["result"]=="PASS" and marker["must_checks"]==["A001"] and (root/"acceptance-pass.json").exists()
        data=json.loads((root/"TEST_REPORT.json").read_text()); data["checks"][0]["exit_code"]=1; (root/"TEST_REPORT.json").write_text(json.dumps(data))
        try: finalize(project); raise AssertionError("bad TEST_REPORT passed")
        except RuntimeError: pass
    print("finalize-acceptance selftest: OK")

def main():
    ap=argparse.ArgumentParser(); ap.add_argument("project",nargs="?",default="."); ap.add_argument("--prepare",action="store_true"); ap.add_argument("--selftest",action="store_true"); args=ap.parse_args()
    if args.selftest: selftest(); return
    project=Path(args.project).resolve()
    try:
        if args.prepare: prepare(project)
        else: finalize(project)
    except RuntimeError: raise SystemExit(2)
if __name__=="__main__": main()
