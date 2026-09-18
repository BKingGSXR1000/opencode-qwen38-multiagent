#!/usr/bin/env python3
from __future__ import annotations
import argparse, sqlite3, subprocess
from pathlib import Path

IMPLEMENTATION_AGENTS={
    "probe-builder","implementer","core-builder","feature-builder",
    "reasoning-builder","integrator","tester","test-builder",
}
SEMANTIC_CHILD_AGENTS=IMPLEMENTATION_AGENTS|{
    "acceptance-planner","acceptance-validator","implementation-planner",
    "reference-researcher","task-splitter",
}

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--project",required=True,type=Path)
    ap.add_argument("--root",type=Path,default=Path.home()/ "AI/opencode-qwen38-multiagent-v2")
    ns=ap.parse_args()
    project=ns.project.expanduser().resolve()
    root=ns.root.expanduser().resolve()
    db=root/"xdg/data/opencode/opencode.db"
    plugin=root/"xdg/config/opencode/plugins/v2-bounded-subagent.js"
    runtime=root/"runtime/opencode2/bin/opencode2"
    for p in (db,plugin,runtime):
        if not p.is_file(): raise SystemExit(f"ERROR: missing {p}")
    version=subprocess.check_output([str(runtime),"--version"],text=True).strip()
    text=plugin.read_text(errors="replace")
    required=[
        'event.tool !== "subagent" && event.tool !== "task"',
        '"--claim-dispatch"','"--claim-splitter"',
        "enableNativeImplementationBackground","execute.before","execute.after",
    ]
    missing=[x for x in required if x not in text]
    con=sqlite3.connect(f"file:{db}?mode=ro",uri=True,timeout=2)
    try:
        rows=con.execute(
            "SELECT id,parent_id,coalesce(agent,''),coalesce(directory,'') "
            "FROM session_v2 WHERE directory=? ORDER BY time_created",
            (str(project),)
        ).fetchall()
    finally:
        con.close()
    roots={sid for sid,parent,agent,_ in rows if parent is None and agent=="orchestrator"}
    children=[(sid,parent,agent) for sid,parent,agent,_ in rows
              if parent is not None and agent in SEMANTIC_CHILD_AGENTS]
    impl=[x for x in children if x[2] in IMPLEMENTATION_AGENTS]
    rootless=[(sid,parent,agent) for sid,parent,agent,_ in rows
              if agent in IMPLEMENTATION_AGENTS and parent is None]
    bad=[x for x in children if x[1] not in roots]
    print("V2 native TaskTool parenting evidence")
    print("===================================")
    print(f"runtime version              : {version}")
    print(f"project                      : {project}")
    print(f"orchestrator roots           : {len(roots)}")
    print(f"semantic child sessions      : {len(children)}")
    print(f"implementation children      : {len(impl)}")
    print(f"rootless impl sessions       : {len(rootless)}")
    print(f"children with non-root parent: {len(bad)}")
    print(f"plugin contract missing      : {len(missing)}")
    if missing:
        for x in missing: print(f"  MISSING: {x}")
    if rootless:
        for sid,_,agent in rootless[:10]: print(f"  ROOTLESS: {sid} {agent}")
    if bad:
        for sid,parent,agent in bad[:10]: print(f"  BAD_PARENT: {sid} parent={parent} agent={agent}")
    if missing: raise SystemExit("NATIVE_TASKTOOL_CONTRACT: FAIL")
    if not roots: raise SystemExit("NATIVE_TASKTOOL_PARENTING_EVIDENCE: NO_ROOT")
    if not impl: raise SystemExit("NATIVE_TASKTOOL_PARENTING_EVIDENCE: NO_IMPLEMENTATION_CHILDREN")
    if rootless: raise SystemExit("NATIVE_TASKTOOL_PARENTING_EVIDENCE: ROOTLESS_IMPLEMENTATION_FOUND")
    if bad: raise SystemExit("NATIVE_TASKTOOL_PARENTING_EVIDENCE: WRONG_PARENT_FOUND")
    print("NATIVE_TASKTOOL_CONTRACT: PASS")
    print("NATIVE_TASKTOOL_PARENTING_EVIDENCE: PASS")
    print("MODEL PROMPTS SENT BY THIS PROBE: NONE")

if __name__=="__main__":
    main()
