#!/usr/bin/env python3
from __future__ import annotations
import argparse, json
from pathlib import Path

SPLIT_LAUNCH_STATES={"split-required","split-retryable"}
SPLIT_WAIT_STATES={"splitter-active"}
SPLIT_TERMINAL_STATES={
    "split-validation-failed","splitter-failed",
    "split-unavailable-read-only-parent","parent-finalize-failed",
}

def action(kind,**kwargs):
    return {"kind":kind,**kwargs}

def select_actions(decision):
    if not isinstance(decision,dict):
        return [action("blocked",reason="decision-not-object")]
    if decision.get("state_error"):
        return [action("blocked",reason="control-state-error",
                       detail=str(decision.get("state_error_message") or ""))]
    phase=str(decision.get("resume_phase") or "")
    scheduler=decision.get("scheduler") if isinstance(decision.get("scheduler"),dict) else {}
    active=int(scheduler.get("active_workers") or 0)
    available=int(scheduler.get("available_worker_slots") or 0)
    eligible=decision.get("eligible") if isinstance(decision.get("eligible"),list) else []
    roles=decision.get("eligible_roles") if isinstance(decision.get("eligible_roles"),dict) else {}
    blockers=decision.get("execution_blockers") if isinstance(decision.get("execution_blockers"),list) else []
    plan=decision.get("plan") if isinstance(decision.get("plan"),dict) else {}
    reference=decision.get("reference") if isinstance(decision.get("reference"),dict) else {}
    acceptance=decision.get("acceptance") if isinstance(decision.get("acceptance"),dict) else {}
    ref_validation=decision.get("reference_validation") if isinstance(decision.get("reference_validation"),dict) else {}

    if phase=="acceptance":
        nxt=str(acceptance.get("next_action") or "")
        if nxt in {"fresh","repair"}:
            return [action("launch",agent="acceptance-planner",mode=nxt)]
        if nxt=="ready": return [action("rescan")]
        if nxt=="blocked": return [action("blocked",reason="acceptance-blocked")]
        return [action("needs_projection",field="acceptance.next_action")]

    if phase=="reference-foundation":
        state=str(reference.get("foundation_state") or "")
        if state=="pending":
            return [action("launch",agent="reference-researcher",mode="foundation")]
        if state in {"ready","not-required","not-applicable"}:
            return [action("rescan")]
        if state=="blocked":
            return [action("blocked",reason="reference-foundation-blocked")]
        return [action("blocked",reason=f"unknown-reference-foundation-state:{state}")]

    if phase=="implementation-plan":
        nxt=str(plan.get("next_action") or "")
        if nxt in {"fresh","repair","continue"}:
            return [action("launch",agent="implementation-planner",mode=nxt)]
        if nxt=="ready": return [action("rescan")]
        if nxt=="blocked" or plan.get("blocked"):
            return [action("blocked",reason="implementation-plan-blocked")]
        return [action("blocked",reason=f"unknown-plan-next-action:{nxt}")]

    if phase=="recursive-split":
        launches=[]
        waiting=False
        items=decision.get("split_required") if isinstance(decision.get("split_required"),list) else []
        for item in items:
            if not isinstance(item,dict): continue
            did=str(item.get("deliverable") or "")
            state=str(item.get("split_state") or "")
            generation=int(item.get("split_generation") or 0)
            if state in SPLIT_LAUNCH_STATES:
                launches.append(action("launch",agent="task-splitter",deliverable=did,generation=generation))
            elif state in SPLIT_WAIT_STATES:
                waiting=True
            elif state in SPLIT_TERMINAL_STATES:
                return [action("blocked",reason=state,deliverable=did)]
            elif did:
                return [action("blocked",reason=f"unknown-split-state:{state}",deliverable=did)]
        if eligible and available>0:
            for did in eligible[:min(available,len(eligible))]:
                role=str(roles.get(did) or "")
                if not role:
                    return [action("blocked",reason="eligible-role-missing",deliverable=did)]
                launches.append(action("launch",agent=role,deliverable=did))
        if launches: return launches
        if waiting or active>0: return [action("wait")]
        return [action("rescan")]

    if phase=="execution":
        if eligible and available>0:
            out=[]
            for did in eligible[:min(available,len(eligible))]:
                role=str(roles.get(did) or "")
                if not role:
                    return [action("blocked",reason="eligible-role-missing",deliverable=did)]
                out.append(action("launch",agent=role,deliverable=did))
            return out
        if active>0: return [action("wait")]
        return [action("rescan")]

    if phase in {"execution-blocked","implementation-blocked"}:
        if blockers:
            first=blockers[0] if isinstance(blockers[0],dict) else {}
            return [action("blocked",reason=str(first.get("reason") or phase),
                           deliverable=str(first.get("deliverable") or ""))]
        return [action("blocked",reason=phase)]

    if phase=="final-tests":
        return [action("run_final_tests",command=".opencode-v2/bin/run-checks")]

    if phase=="acceptance-validation":
        if str(reference.get("policy") or "")=="external-required":
            state=str(ref_validation.get("state") or "")
            if state=="pending":
                return [action("launch",agent="reference-researcher",mode="validation")]
            if state=="blocked":
                return [action("blocked",reason="reference-validation-blocked")]
            if state in {"ready","not-required","not-applicable"}:
                return [action("launch",agent="acceptance-validator",mode="final")]
            return [action("needs_projection",field="reference_validation.state")]
        return [action("launch",agent="acceptance-validator",mode="final")]

    if phase=="complete":
        return [action("complete",result="ACCEPTANCE_PASS")]
    return [action("blocked",reason=f"unknown-resume-phase:{phase}")]

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--decision",required=True,type=Path)
    ns=ap.parse_args()
    print(json.dumps(select_actions(json.loads(ns.decision.read_text())),sort_keys=True,indent=2))

if __name__=="__main__":
    main()
