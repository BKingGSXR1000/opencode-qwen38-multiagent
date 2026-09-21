#!/usr/bin/env python3
"""Build a current-control-state disposable D003-equivalent split fixture."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE=Path(__file__).resolve().parent
ROOT=HERE.parent
sys.path.insert(0,str(HERE))

import control_state
import deterministic_dispatch
import supervisor
import structured_plan
from state_io import atomic_write_json, atomic_write_text


class CanaryError(RuntimeError):
    pass


def leaf(key,name,outcome,owned,role,verify,done,acceptance,launch=()):
    return {
        "key":key,"name":name,"outcome":outcome,"owned_artifacts":owned,
        "launch_deps":list(launch),"contract_deps":[],"verify_deps":[],
        "acceptance_ids":[acceptance],"complexity":"S","repeated_operations":0,
        "deep_reasoning":False,"role":role,"verify_command":verify,"done_when":done,
    }


def structured_fixture():
    return {
        "protocol":"v2-structured-plan-v1","status":"complete",
        "leaves":[
            leaf("bootstrap_context","Bootstrap fixture context","A stable local canary input exists.",
                 ["bootstrap.txt"],"implementer","test -s bootstrap.txt","The bootstrap input is present.","A001"),
            leaf("fixture_prerequisite","Prepare fixture prerequisite","A stable local prerequisite exists.",
                 ["precondition.txt"],"implementer","test -s precondition.txt","The prerequisite is present.","A002"),
            leaf("fixture_probe_moons","Frozen ephemeris fixture probe – Galilean moons",
                 "Frozen non-error VECTORS records exist for commands 501, 502, 503, and 504.",
                 [".opencode-v2/probes/ephemeris_moons.json","fixtures/vectors/moons/"],
                 "probe-builder",
                 "test -s .opencode-v2/probes/ephemeris_moons.json && node -e 'const fs=require(\"fs\"),p=\"fixtures/vectors/moons\";const j=require(\"./.opencode-v2/probes/ephemeris_moons.json\");if(!j.commands||Object.keys(j.commands).length<4||!fs.statSync(p).isDirectory())process.exit(1);const a=fs.readdirSync(p).filter(f=>f.endsWith(\".json\"));if(a.length<4)process.exit(1);for(const f of a){const s=fs.readFileSync(p+\"/\"+f,\"utf8\");if(s.length<200||/\\b(?:404|error)\\b/i.test(s)||!/(?:\\$\\$SOE|\\bX\\s*=)/.test(s))process.exit(1)}'",
                 "The probe and owned moons directory contain valid records.","A003",
                 ("bootstrap_context","fixture_prerequisite")),
            leaf("final_tests","Record final canary checks","The canonical test manifest is available.",
                 [".opencode-v2/TEST_CHECKS.json"],"test-builder",".opencode-v2/bin/run-checks",
                 "The canonical test manifest is ready.","A004",("fixture_probe_moons",)),
        ],
    }


def acceptance_text():
    return """# Disposable D003-equivalent acceptance\n\nReference policy: none\n\n- [ ] A001: A stable bootstrap input exists.\n- [ ] A002: A stable prerequisite exists.\n- [ ] A003: The moons fixture uses valid records.\n- [ ] A004: Canonical final checks are recorded.\n\n<!-- ACCEPTANCE_COMPLETE -->\n"""


def completed_failure():
    return type("Checked",(),{"returncode":1,"stdout":"","stderr":"missing moons JSON records\\n"})()


def seed_ready(did, content):
    path=Path(supervisor.PROJECT)/({"D001":"bootstrap.txt","D002":"precondition.txt"}[did])
    atomic_write_text(path,content)
    ok,detail=supervisor.supervisor_finalize_ready(did)
    if not ok:
        raise CanaryError(f"cannot finalize seeded {did}: {detail}")


def build(project: Path, task_file: Path) -> dict:
    project=project.resolve(); task_file=task_file.resolve()
    if not project.is_dir() or any(project.iterdir()):
        raise CanaryError("project must be a fresh empty directory")
    if not task_file.is_file():
        raise CanaryError("task file is missing")
    env={**os.environ,"V2_ROOT":str(ROOT)}
    boot=subprocess.run(
        [sys.executable,str(HERE/"bootstrap-stage-a-project.py"),"--project",str(project),"--task-file",str(task_file)],
        text=True,capture_output=True,env=env,check=False,
    )
    if boot.returncode:
        raise CanaryError(f"bootstrap failed: {boot.stderr.strip() or boot.stdout.strip()}")
    ctrl=project/".opencode-v2"; work=ctrl/"work"; work.mkdir(exist_ok=True)
    atomic_write_text(ctrl/"ACCEPTANCE.md",acceptance_text())
    atomic_write_json(ctrl/"IMPLEMENTATION_PLAN.structured.json",structured_fixture())
    compiled,errors=structured_plan.compile_plan(project)
    if not compiled:
        raise CanaryError(f"structured fixture compile failed: {errors}")
    guarded=subprocess.run(
        [sys.executable,str(HERE/"control-guard.py"),"--project",str(project),"--finalize-all"],
        text=True,capture_output=True,env=env,check=False,
    )
    if guarded.returncode:
        raise CanaryError(f"current control-state finalization failed: {guarded.stdout}{guarded.stderr}")
    mapping=json.loads((ctrl/"IMPLEMENTATION_PLAN.structured-map.json").read_text())
    if mapping.get("key_to_id",{}).get("fixture_probe_moons")!="D003":
        raise CanaryError("fixture probe did not receive deterministic D003")

    old_root,old_project=supervisor.ROOT,supervisor.PROJECT
    supervisor.ROOT=ROOT; supervisor.PROJECT=str(project)
    try:
        atomic_write_json(work/"attempts.json",{
            "owner":"supervisor","protocol":supervisor.ATTEMPT_LEDGER_PROTOCOL,
            "deliverables":{
                "D001":{"count":1,"sessions":["fixture-D001"],"automatic_limit":2,"failure_history":[]},
                "D002":{"count":1,"sessions":["fixture-D002"],"automatic_limit":2,"failure_history":[]},
            },
        })
        seed_ready("D001","bootstrap\n"); seed_ready("D002","precondition\n")
        probe=project/".opencode-v2"/"probes"/"ephemeris_moons.json"
        probe.parent.mkdir(parents=True,exist_ok=True)
        atomic_write_json(probe,{"commands":{"501":"Io","502":"Europa","503":"Ganymede","504":"Callisto"}})
        (project/"fixtures"/"vectors"/"moons").mkdir(parents=True,exist_ok=True)
        verify=supervisor.load_manifest()["leaves"]["D003"]["verify_command"]
        for attempt in (1,2):
            status,count=supervisor.claim_attempt(f"synthetic-D003-{attempt}","D003")
            if status!="claimed" or count!=attempt:
                raise CanaryError(f"cannot claim synthetic D003 attempt {attempt}: {status}/{count}")
            supervisor.persist_supervisor_verify_evidence(
                "D003",f"synthetic-D003-{attempt}",verify,completed_failure(),"verify-failed-1"
            )
            recorded,outcome=supervisor.record_leaf_failure("D003","verify-failed-1","genuine")
            if not recorded or (attempt==1 and outcome!="genuine-recorded") or (attempt==2 and outcome!="split-required"):
                raise CanaryError(f"synthetic D003 failure {attempt} did not reach expected state: {recorded}/{outcome}")
        snapshot=supervisor.sync_control_status_snapshot()
        state=json.loads((ctrl/"query"/"decision.json").read_text())
        action={"kind":"launch","agent":"task-splitter","deliverable":"D003","generation":1}
        actions=deterministic_dispatch.select_actions(state)
        if action not in actions:
            raise CanaryError(
                f"current selector did not choose D003 split: {state.get('resume_phase')} "
                f"split_required={state.get('split_required')!r} actions={actions!r}"
            )
        request=json.loads((work/"D003.split-request.json").read_text())
        return {
            "protocol":"v2-d003-current-state-canary-v1","project":str(project),
            "state_version":state["state_version"],"action":action,
            "attempts":snapshot["leaves"]["D003"]["attempts"],
            "split_state":snapshot["leaves"]["D003"]["split_state"],
            "ownership":request["ownership_items"],
            "verify_evidence":request["supervisor_verify_evidence"],
        }
    finally:
        supervisor.ROOT,supervisor.PROJECT=old_root,old_project


def selftest():
    with tempfile.TemporaryDirectory(prefix="d003-current-canary-") as td:
        root=Path(td); project=root/"project"; project.mkdir()
        task=root/"TASK.md"; task.write_text("Build a disposable D003-equivalent fixture.\n")
        receipt=build(project,task)
        if receipt["action"]["deliverable"]!="D003" or receipt["attempts"]!=2:
            raise CanaryError(f"fixture selftest receipt mismatch: {receipt!r}")
        if "fixtures/vectors/moons/" not in receipt["ownership"]:
            raise CanaryError("fixture does not preserve canonical directory ownership")
    print("current D003 splitter canary fixture selftest: OK")


def main() -> int:
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--project",type=Path); ap.add_argument("--task-file",type=Path)
    ap.add_argument("--selftest",action="store_true")
    ns=ap.parse_args()
    if ns.selftest:
        selftest(); return 0
    if ns.project is None or ns.task_file is None:
        ap.error("--project and --task-file are required unless --selftest is used")
    print(json.dumps(build(ns.project,ns.task_file),sort_keys=True,indent=2))
    return 0


if __name__=="__main__":
    try:
        raise SystemExit(main())
    except CanaryError as exc:
        raise SystemExit(f"ERROR: {exc}")
