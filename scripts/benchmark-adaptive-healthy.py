#!/usr/bin/env python3
"""Sequential, isolated healthy-worker smoke comparison for adaptive watchdog.

One disposable two-leaf standard-library coding fixture per mode. Never
restart shared vLLM or existing OpenCode servers. Results are observational;
a single small task cannot measure production false-positive rates.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HERE=Path(__file__).resolve().parent
ROOT=HERE.parent
sys.path.insert(0,str(HERE))
from control_state import acceptance_pass_valid
from runtime_contract import verify_state
from state_io import atomic_write_json

spec=importlib.util.spec_from_file_location(
    "benchmark_restart_fixture",HERE/"create-restart-recovery-canary.py")
fixture=importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)

MODES=("off","observe","enforce")
RUN_ROOT=Path("/home/bking/AI/a2-e2e/20261003-adaptive-healthy-compare")
PORTS={"off":58541,"observe":58542,"enforce":58543}


def proc_env(pid):
    return Path(f"/proc/{pid}/environ").read_bytes()


def safe_kill(pid,project,port=None,server=False):
    try:
        env=proc_env(pid)
        cmd=Path(f"/proc/{pid}/cmdline").read_bytes().replace(bytes((0,)),b" ")
    except OSError:
        return
    if server:
        if b"opencode serve" not in cmd or ("--port "+str(port)).encode() not in cmd:
            raise RuntimeError("refuse unrelated server process "+str(pid))
        if ("V2_OPENCODE_BASE_URL=http://127.0.0.1:"+str(port)).encode() not in env:
            raise RuntimeError("refuse server without exact base URL binding")
    elif ("V2_PROJECT="+str(project)).encode() not in env or b"supervisor.py" not in cmd:
        raise RuntimeError("refuse unrelated supervisor process "+str(pid))
    os.kill(pid,signal.SIGTERM)


def request_ready(project,port):
    query=urllib.parse.urlencode({"directory":str(project)})
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/session/status?{query}",timeout=1
        ) as r:
            return r.status==200
    except (OSError,ValueError):
        return False


def run_cmd(args,*,env=None,log=None,timeout=40):
    result=subprocess.run(args,cwd=ROOT,env=env,
        stdout=subprocess.PIPE,stderr=subprocess.STDOUT,
        text=True,timeout=timeout,check=False)
    if log is not None:
        Path(log).write_text(result.stdout)
    if result.returncode:
        raise RuntimeError(
            "command failed "+str(args[:2])+
            " exit="+str(result.returncode)+" "+
            result.stdout[-950:]
        )
    return result.stdout


def metric(project,started,ended):
    database=ROOT/"xdg/data-v11831-a2/opencode/opencode.db"
    con=sqlite3.connect(f"file:{database}?mode=ro",uri=True)
    try:
        ids=[sid for (sid,) in con.execute(
            "SELECT id FROM session WHERE directory=?",(str(project),)
        )]
        if not ids:
            return {"sessions":0,"tool_calls":0,"tool_successes":0,
                    "reasoning_tokens":0,"output_tokens":0,
                    "input_tokens":0}
        placeholders=",".join("?"*len(ids))
        rows=con.execute(
            f"SELECT data FROM part WHERE session_id IN ({placeholders})",ids
        ).fetchall()
        messages=con.execute(
            f"SELECT data FROM message WHERE session_id IN ({placeholders})",ids
        ).fetchall()
        calls=success=0
        for (raw,) in rows:
            item=json.loads(raw)
            if item.get("type")!="tool":continue
            calls+=1
            if (item.get("state") or {}).get("status")=="completed":success+=1
        totals={"input_tokens":0,"output_tokens":0,"reasoning_tokens":0}
        for (raw,) in messages:
            item=json.loads(raw)
            if item.get("role")!="assistant":continue
            tokens=item.get("tokens") or {}
            for key,field in (
                ("input_tokens","input"),("output_tokens","output"),
                ("reasoning_tokens","reasoning"),
            ):
                val=tokens.get(field,0)
                if isinstance(val,(float,int)):totals[key]+=int(val)
        return {"sessions":len(ids),"tool_calls":calls,
                "tool_successes":success,**totals}
    finally:
        con.close()


def project_telemetry(project):
    # New runs use real JSONL; the historic file still has concatenated
    # legacy rows. Analyze only the new file suffix after the run starts.
    import importlib.util
    s=importlib.util.spec_from_file_location(
        "adaptive_watchdog_report",HERE/"watchdog-report.py")
    m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
    rows=[r for r in m.load_rows(ROOT/"logs/watchdog-telemetry.jsonl")
          if r.get("directory")==str(project)]
    flags=[r for r in rows if r.get("adaptive_event")]
    decision_rows=[r.get("adaptive_reasoning") or {} for r in rows]
    return {
        "records":len(rows),
        "decision_records":sum(bool(x) for x in decision_rows),
        "would_interrupt":sum(
            r.get("adaptive_event")=="would-interrupt" for r in flags
        ),
        "confirmed_interrupt":sum(
            r.get("adaptive_event")=="interrupt-confirmed" for r in flags
        ),
        "maximum_action_age_s":max(
            (float(x.get("action_age") or 0) for x in decision_rows),default=0
        ),
        "maximum_visible_reasoning_since_tool":max(
            (int(x.get("reasoning_chars_since_action") or 0)
             for x in decision_rows),default=0
        ),
    }


def attempt_result(project):
    control=project/".opencode-v2"
    f=control/"work/attempts.json"
    if not f.exists():return {}
    data=json.loads(f.read_text())
    result={}
    for did,row in data.get("deliverables",{}).items():
        result[did]={
            "attempts":row.get("count",0),
            "genuine_failures":sum(
                x.get("classification")=="genuine"
                for x in row.get("failure_history",[])
            ),
            "infrastructure_credits":row.get("infrastructure_retry_grants",0),
        }
    return result


def run(mode,limit):
    base=RUN_ROOT/mode
    project=base/"project"
    port=PORTS[mode]
    if base.exists():
        raise RuntimeError("never overwrite benchmark canary "+str(base))
    project.mkdir(parents=True)
    task=base/"task.md"
    task.write_text(
        "Create restart_probe.txt with the exact line restart-recovery-ok; "
        "then produce and execute an independent final test manifest. "
        "Use the standard Stage-A supervisor and exact Verify.\n"
    )
    result={"mode":mode,"port":port,"project":str(project),
            "started_epoch":time.time()}
    atomic_write_json(base/"status.json",result)
    print("RUN_START",mode,"PORT",port,flush=True)
    fixture.build(project,task)
    server=None
    driver=None
    state=None
    started=time.monotonic()
    env=dict(os.environ)
    env["V2_ADAPTIVE_REASONING_MODE"]=mode
    try:
        if request_ready(project,port):
            raise RuntimeError("private server port already in use "+str(port))
        server_log=(base/"server.log").open("w")
        server=subprocess.Popen(
            ["bash",str(HERE/"run-a2-v11831-server.sh"),
             str(project),str(port)],
            env=env,cwd=ROOT,stdout=server_log,stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,start_new_session=True,
        )
        for i in range(130):
            if server.poll() is not None:
                raise RuntimeError(
                    "private server exited prematurely "+(base/"server.log").read_text()[-800:]
                )
            if request_ready(project,port):break
            time.sleep(.3)
        else:raise RuntimeError("private OpenCode never became ready")
        state=verify_state(ROOT,project,f"http://127.0.0.1:{port}")
        result["private_server_pid"]=state["server_pid"]
        run_cmd(["bash",str(HERE/"start-a2-v11831-supervisor.sh"),
                 str(project),f"http://127.0.0.1:{port}"],
                env=env,log=base/"supervisor-start.log")
        pid=int((project/".opencode-v2/work/supervisor.pid").read_text())
        assert ("V2_ADAPTIVE_REASONING_MODE="+mode).encode() in proc_env(pid)
        result["supervisor_pid"]=pid
        deadline=time.monotonic()+28
        shadow=project/".opencode-v2/query/deterministic-shadow.json"
        while not shadow.is_file():
            if time.monotonic()>deadline:
                raise RuntimeError("supervisor did not materialize shadow")
            time.sleep(.2)
        root=json.loads(run_cmd([
            sys.executable,str(HERE/"create-stage-a-root.py"),
            "--project",str(project),"--base-url",
            f"http://127.0.0.1:{port}",
        ],env=env,log=base/"root.json"))
        sid=root["root_session"]
        result["root_session"]=sid
        run_cmd([
            sys.executable,str(HERE/"stage_a_preflight.py"),
            "--project",str(project),"--task-file",str(task),
            "--base-url",f"http://127.0.0.1:{port}",
            "--root-session",sid,"--write-proof",str(base/"preflight.json")
        ],env=env,log=base/"preflight.log")
        output=(base/"driver.log").open("w")
        driver=subprocess.Popen([
            sys.executable,str(HERE/"drive-stage-a-run.py"),
            "--project",str(project),"--base-url",f"http://127.0.0.1:{port}",
            "--root-session",sid,
            "--preflight-proof",str(base/"preflight.json"),
            "--poll","1","--max-ticks",str(limit+35),
        ],env=env,cwd=ROOT,stdin=subprocess.DEVNULL,
          stdout=output,stderr=subprocess.STDOUT,start_new_session=True)
        try:
            code=driver.wait(timeout=limit)
        except subprocess.TimeoutExpired:
            result["driver_timed_out"]=True
            os.killpg(driver.pid,signal.SIGTERM)
            try:driver.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(driver.pid,signal.SIGKILL)
            code=-1
        result["driver_exit_code"]=code
        result["driver_tail"]=(
            (base/"driver.log").read_text().splitlines()[-4:]
        )
        print("DRIVER_STOP",mode,"EXIT",code,"ELAPSED",
              round(time.monotonic()-started,1),flush=True)
    except Exception as exc:
        result["error"]=type(exc).__name__+": "+str(exc)[:650]
        print("RUN_ERROR",mode,result["error"],flush=True)
    finally:
        result["duration_seconds"]=round(time.monotonic()-started,2)
        result["ended_epoch"]=time.time()
        control=project/".opencode-v2"
        try:
            decision=json.loads((control/"query/decision.json").read_text())
            result["phase"]=decision.get("resume_phase")
            result["execution_blockers"]=decision.get("execution_blockers",[])
        except (OSError,ValueError):
            result["phase"]="unknown"
        result["accepted"]=acceptance_pass_valid(project)
        try:
            report=json.loads((control/"acceptance-report.json").read_text())
            result["musts_pass"]=sum(
                x.get("status")=="PASS" for x in report.get("checks",[])
            )
            result["musts_total"]=len(report.get("checks",[]))
        except (OSError,ValueError):
            result["musts_pass"]=0;result["musts_total"]=0
        try:result["sessions"]=metric(project,started,time.monotonic())
        except Exception as exc:result["metrics_error"]=repr(exc)[:160]
        try:result["telemetry"]=project_telemetry(project)
        except Exception as exc:result["telemetry_error"]=repr(exc)[:160]
        try:result["attempts"]=attempt_result(project)
        except Exception as exc:result["attempts_error"]=repr(exc)[:160]
        atomic_write_json(base/"result.json",result)
        # Terminate only exact-project supervisor and this benchmark's server.
        try:
            pid_file=control/"work/supervisor.pid"
            if pid_file.exists():
                safe_kill(int(pid_file.read_text()),project)
        except Exception as exc:
            print("CLEANUP_WARNING",mode,"supervisor",repr(exc),flush=True)
        try:
            if server is not None:
                # Wrapper traps SIGTERM and stops the exact child OpenCode.
                os.killpg(server.pid,signal.SIGTERM)
                try:server.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    if state and state.get("server_pid"):
                        safe_kill(int(state["server_pid"]),project,port,server=True)
        except Exception as exc:
            print("CLEANUP_WARNING",mode,"server",repr(exc),flush=True)
        print("RUN_RESULT",mode,
              "ACCEPTED",result["accepted"],
              "MUST",str(result.get("musts_pass"))+"/"+str(result.get("musts_total")),
              "DURATION_S",result["duration_seconds"],
              "TOOLS",result.get("sessions",{}).get("tool_calls"),
              "INTERRUPTS",result.get("telemetry",{}).get("confirmed_interrupt"),
              flush=True)
    return result


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--modes",nargs="+",choices=MODES,default=list(MODES))
    ap.add_argument("--seconds-per-run",type=int,default=285)
    opts=ap.parse_args()
    RUN_ROOT.mkdir(parents=True,exist_ok=True)
    summary_path=RUN_ROOT/"summary.json"
    if summary_path.exists():
        existing=json.loads(summary_path.read_text())
        if (existing.get("protocol")!="adaptive-healthy-smoke-v1"
                or not isinstance(existing.get("results"),list)):
            ap.error("existing benchmark summary has unknown protocol")
        results=list(existing["results"])
    else:
        results=[]
    recorded={x["mode"] for x in results}
    if any((RUN_ROOT/mode).exists() or mode in recorded for mode in opts.modes):
        ap.error("refusing to overwrite an existing benchmark mode")
    for mode in opts.modes:
        results.append(run(mode,opts.seconds_per_run))
        atomic_write_json(summary_path,{
            "protocol":"adaptive-healthy-smoke-v1",
            "tasks_identical":True,"ordered_sequential":True,
            "same_backend":True,"same_fixture":"restart-recovery-canary-v1",
            "results":results,
            "limitations":[
                "Only one small two-deliverable task per mode.",
                "Sequential run order and shared GPU load can influence latency.",
                "No claims about false-positive rate without independent healthy workloads.",
            ],
        })
    print("COMPARISON_FINISHED",flush=True)
    return 0 if all(x.get("accepted") for x in results) else 2


if __name__=="__main__":
    raise SystemExit(main())
