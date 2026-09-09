#!/usr/bin/env python3
import argparse,json,os,re,subprocess,time
from pathlib import Path

def atomic_write(path,text):
    path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_suffix(path.suffix+".tmp"); tmp.write_text(text); os.replace(tmp,path)

def slug(s): return re.sub(r"[^A-Za-z0-9._-]+","-",s).strip("-") or "check"

def main():
    ap=argparse.ArgumentParser(); ap.add_argument("--project",default="."); args=ap.parse_args()
    project=Path(args.project).resolve(); ctrl=project/".opencode-v2"
    spec_path=ctrl/"TEST_CHECKS.json"; report_path=ctrl/"TEST_REPORT.json"; logs=ctrl/"test-logs"
    if not spec_path.exists(): raise SystemExit("ERROR: TEST_CHECKS.json missing")
    spec=json.loads(spec_path.read_text()); checks=spec.get("checks")
    if not isinstance(checks,list) or not checks: raise SystemExit("ERROR: checks[] must be non-empty")
    required=spec.get("required_files") or []
    if not isinstance(required,list): raise SystemExit("ERROR: required_files must be a list")
    missing=[p for p in required if not (project/p).exists()]
    results=[]; logs.mkdir(parents=True,exist_ok=True)
    for i,ch in enumerate(checks,1):
        if not isinstance(ch,dict): raise SystemExit(f"ERROR: check #{i} must be object")
        name=str(ch.get("name") or f"check-{i}"); cmd=str(ch.get("command") or "").strip(); timeout=int(ch.get("timeout_seconds") or 180)
        if not cmd: raise SystemExit(f"ERROR: empty command for {name}")
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
