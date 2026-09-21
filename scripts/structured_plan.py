#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from collections import defaultdict
from pathlib import Path
from state_io import atomic_write_text as state_atomic_write_text, atomic_write_json as state_atomic_write_json

PROTOCOL = "v2-structured-plan-v1"
REPAIR_PROTOCOL = "v2-structured-plan-repair-v1"
MAP_PROTOCOL = "v2-structured-plan-map-v1"
PLAN_MARKER = "<!-- IMPLEMENTATION_PLAN_COMPLETE -->"
RUN_CHECKS_COMMAND = ".opencode-v2/bin/run-checks"

ROLE_ALLOWLIST = {
    "probe-builder",
    "implementer",
    "core-builder",
    "feature-builder",
    "reasoning-builder",
    "integrator",
    "tester",
    "test-builder",
}
WRITE_ROLES = ROLE_ALLOWLIST - {"tester"}
KEY_RE = re.compile(r"^[a-z][a-z0-9_]{1,47}$")
ACC_RE = re.compile(r"^A\d{3}$")
PATH_RE = re.compile(r"^[^\s`]+$")
TASK_SHAPE_OWNED_LIMIT = {"S": 2, "M": 3}
TASK_SHAPE_ACCEPTANCE_LIMIT = {"S": 2, "M": 4}
TASK_SHAPE_REPEAT_LIMIT = {"S": 4, "M": 6}
EXPLICIT_STAGE_WORD_RE = re.compile(
    r"(?<!-)\b(?:first|second|third)\b(?!-)|\b(?:then|followed\s+by)\b",
    re.I,
)
EXPLICIT_PAREN_STAGE_SEQUENCE_RE = re.compile(
    r"(?:\(1\).*?\(2\)|\(a\).*?\(b\))",
    re.I | re.S,
)
EXPLICIT_MIXED_STAGE_SEQUENCE_RE = re.compile(
    r"(?:\(1\)|\(a\)).*?\b(?:then|followed\s+by)\b",
    re.I | re.S,
)

def has_compound_stage_sequence(text):
    """Detect explicit staged work without treating arbitrary IDs as stages."""
    text=str(text or "")
    return bool(
        len(EXPLICIT_STAGE_WORD_RE.findall(text))>=2
        or EXPLICIT_PAREN_STAGE_SEQUENCE_RE.search(text)
        or EXPLICIT_MIXED_STAGE_SEQUENCE_RE.search(text)
    )
EXTERNAL_ACQUISITION_RE = re.compile(
    r"(?:\b(?:fetch(?:ed|ing)?|download(?:ed|ing)?|vendor(?:ed|ing)?|research(?:ed|ing)?)\b"
    r".{0,100}\b(?:https?://|external|public\s+source|authoritative|cdn|jpl|horizons|unpkg|jsdelivr)\b"
    r"|\b(?:curl|wget)\s+https?://)",
    re.I,
)
PREEXISTING_EXTERNAL_STATE_RE = re.compile(
    r"\b(?:already|previously|preexisting|pre-existing|pre)\s*[- ]?\s*"
    r"(?:fetched|downloaded|vendored|researched)\b",
    re.I,
)

def external_acquisition_match(text):
    # Explicitly pre-existing/frozen artifacts are inputs, not acquisition
    # performed by this leaf. Unqualified/active acquisition remains fail-closed.
    scrubbed=PREEXISTING_EXTERNAL_STATE_RE.sub("preexisting-artifact",str(text or ""))
    return EXTERNAL_ACQUISITION_RE.search(scrubbed)
HOST_REMEDIATION_RE = re.compile(
    r"\b(?:sudo|systemctl|service\s+[-\w.@:]+\s+(?:start|stop|restart|reload|force-reload)|"
    r"daemon-reload|apt(?:-get)?\s+install|dnf\s+install|yum\s+install|pacman\s+-S|"
    r"restart\s+(?:the\s+)?(?:service|daemon)|enable\s+(?:the\s+)?(?:service|daemon))\b",
    re.I,
)

def atomic_write_text(path: Path, text: str):
    state_atomic_write_text(path,text)

def atomic_write_json(path: Path, obj):
    state_atomic_write_json(path,obj)

def canonical_owned(paths: list[str]) -> str:
    if not paths:
        return "none"
    return ", ".join(f"`{p}`" for p in paths)

def clean_string(value, label, key, errors, *, allow_empty=False):
    if not isinstance(value, str):
        errors.append({"key":key,"code":"type","message":f"{key}: {label} must be a string"})
        return ""
    value=value.strip()
    if not value and not allow_empty:
        errors.append({"key":key,"code":"missing","message":f"{key}: missing {label}"})
    if "\n" in value or "\r" in value:
        errors.append({"key":key,"code":"multiline","message":f"{key}: {label} must be one line"})
        value=" ".join(value.splitlines())
    return value

def validate_path(path: str) -> str:
    if not isinstance(path,str):
        return "owned artifact path must be a string"
    path=path.strip()
    if not path:
        return "owned artifact path is empty"
    if not PATH_RE.fullmatch(path):
        return f"owned artifact path is not canonical: {path!r}"
    if path.startswith(("/", "./", "~")) or ".." in Path(path).parts:
        return f"owned artifact path is unsafe/non-relative: {path}"
    return ""

def ownership_overlap(leaves):
    errors=[]
    items=[]
    for leaf in leaves:
        for path in leaf["owned_artifacts"]:
            items.append((leaf["key"],path.rstrip("/"),path.endswith("/")))
    for i,(ka,pa,da) in enumerate(items):
        for kb,pb,db in items[i+1:]:
            if ka==kb:
                continue
            overlap=(pa==pb or (da and pb.startswith(pa+"/")) or (db and pa.startswith(pb+"/")))
            if overlap:
                errors.append({
                    "key":ka,
                    "keys":[ka,kb],
                    "code":"ownership-overlap",
                    "message":f"{ka},{kb}: ownership overlap between `{pa + ('/' if da else '')}` and `{pb + ('/' if db else '')}`"
                })
    return errors

def topo_order(leaves, dep_fields):
    by_key={x["key"]:x for x in leaves}
    index={x["key"]:i for i,x in enumerate(leaves)}
    indeg={k:0 for k in by_key}
    outgoing=defaultdict(list)
    for leaf in leaves:
        for field in dep_fields:
            for dep in leaf[field]:
                if dep in by_key:
                    indeg[leaf["key"]]+=1
                    outgoing[dep].append(leaf["key"])
    ready=sorted((k for k,v in indeg.items() if v==0), key=index.get)
    result=[]
    while ready:
        k=ready.pop(0)
        result.append(k)
        for child in sorted(outgoing[k], key=index.get):
            indeg[child]-=1
            if indeg[child]==0:
                ready.append(child)
                ready.sort(key=index.get)
    if len(result)!=len(leaves):
        return [], sorted(k for k,v in indeg.items() if v>0)
    return result, []

def normalize_document(raw):
    errors=[]
    if not isinstance(raw,dict):
        return None,[{"key":"","code":"type","message":"structured plan root must be an object"}]
    allowed_root={"protocol","status","leaves"}
    extra=sorted(set(raw)-allowed_root)
    if extra:
        errors.append({"key":"","code":"unsupported-root-fields","message":"unsupported root fields: "+", ".join(extra)})
    if raw.get("protocol") != PROTOCOL:
        errors.append({"key":"","code":"protocol","message":f"protocol must be exact {PROTOCOL}"})
    if str(raw.get("status","")).lower() != "complete":
        errors.append({"key":"","code":"status","message":"status must be exact complete"})
    raw_leaves=raw.get("leaves")
    if not isinstance(raw_leaves,list) or not raw_leaves:
        errors.append({"key":"","code":"leaves","message":"leaves must be a non-empty array"})
        return None,errors
    if len(raw_leaves)>26:
        errors.append({"key":"","code":"leaf-count","message":f"structured plan has {len(raw_leaves)} leaves; maximum is 26"})

    normalized=[]
    seen=set()
    allowed_leaf={
        "key","name","outcome","owned_artifacts","launch_deps","contract_deps",
        "verify_deps","acceptance_ids","complexity","repeated_operations",
        "deep_reasoning","role","verify_command","done_when",
    }
    for i,item in enumerate(raw_leaves):
        fallback=f"leaf_{i+1}"
        if not isinstance(item,dict):
            errors.append({"key":fallback,"code":"type","message":f"{fallback}: leaf must be an object"})
            continue
        key=str(item.get("key") or fallback).strip()
        extra=sorted(set(item)-allowed_leaf)
        if extra:
            errors.append({"key":key,"code":"unsupported-fields","message":f"{key}: unsupported fields: {', '.join(extra)}"})
        if not KEY_RE.fullmatch(key):
            errors.append({"key":key,"code":"key","message":f"{key}: key must match {KEY_RE.pattern}"})
        if key in seen:
            errors.append({"key":key,"code":"duplicate-key","message":f"{key}: duplicate leaf key"})
        seen.add(key)

        leaf={"key":key}
        for fld in ("name","outcome","verify_command","done_when"):
            leaf[fld]=clean_string(item.get(fld),fld,key,errors)
        role=clean_string(item.get("role"),"role",key,errors)
        leaf["role"]=role
        if role and role not in ROLE_ALLOWLIST:
            errors.append({"key":key,"code":"role","message":f"{key}: unsupported role {role!r}"})
        complexity=clean_string(item.get("complexity"),"complexity",key,errors).upper()
        leaf["complexity"]=complexity
        if complexity not in {"S","M"}:
            errors.append({"key":key,"code":"complexity","message":f"{key}: complexity must be S or M"})
        ro=item.get("repeated_operations")
        if not isinstance(ro,int) or isinstance(ro,bool) or ro<0:
            errors.append({"key":key,"code":"repeated-operations","message":f"{key}: repeated_operations must be a non-negative integer"})
            ro=0
        leaf["repeated_operations"]=ro
        dr=item.get("deep_reasoning")
        if not isinstance(dr,bool):
            errors.append({"key":key,"code":"deep-reasoning","message":f"{key}: deep_reasoning must be boolean"})
            dr=False
        leaf["deep_reasoning"]=dr

        owned=item.get("owned_artifacts")
        if not isinstance(owned,list):
            errors.append({"key":key,"code":"owned-artifacts","message":f"{key}: owned_artifacts must be an array"})
            owned=[]
        clean_owned=[]
        for p in owned:
            err=validate_path(p)
            if err:
                errors.append({"key":key,"code":"owned-artifacts","message":f"{key}: {err}"})
                continue
            p=p.strip()
            if p not in clean_owned:
                clean_owned.append(p)
        leaf["owned_artifacts"]=clean_owned
        if role=="tester" and clean_owned:
            errors.append({"key":key,"code":"tester-ownership","message":f"{key}: tester must use owned_artifacts: []"})
        if role and role!="tester" and not clean_owned:
            errors.append({"key":key,"code":"writer-ownership","message":f"{key}: write-capable role must own at least one durable artifact"})

        for fld in ("launch_deps","contract_deps","verify_deps","acceptance_ids"):
            val=item.get(fld)
            if not isinstance(val,list):
                errors.append({"key":key,"code":fld,"message":f"{key}: {fld} must be an array"})
                val=[]
            vals=[]
            for x in val:
                if not isinstance(x,str) or not x.strip():
                    errors.append({"key":key,"code":fld,"message":f"{key}: {fld} entries must be non-empty strings"})
                    continue
                x=x.strip()
                if x not in vals:
                    vals.append(x)
            leaf[fld]=vals
        if not leaf["acceptance_ids"]:
            errors.append({
                "key":key,
                "code":"acceptance",
                "message":(
                    f"{key}: acceptance_ids must contain at least one MUST acceptance ID Axxx. "
                    "A SHOULD-only leaf is not a valid standalone implementation leaf; merge optional "
                    "SHOULD work into a compatible MUST-backed leaf without exceeding task-shape limits, "
                    "or remove the optional leaf and update dependencies."
                ),
            })
        for aid in leaf["acceptance_ids"]:
            if not ACC_RE.fullmatch(aid):
                if re.fullmatch(r"S\d{3}",aid):
                    message=(
                        f"{key}: invalid acceptance ID {aid!r}; acceptance_ids accepts MUST IDs Axxx only. "
                        "SHOULD IDs Sxxx cannot justify a standalone implementation leaf. Merge this optional "
                        "work into a compatible MUST-backed leaf without exceeding task-shape limits, or remove "
                        "the optional leaf and update dependencies."
                    )
                else:
                    message=(
                        f"{key}: invalid acceptance ID {aid!r}; acceptance_ids accepts MUST IDs Axxx only."
                    )
                errors.append({"key":key,"code":"acceptance","message":message})
        normalized.append(leaf)

    keys={x["key"] for x in normalized}
    for leaf in normalized:
        key=leaf["key"]
        for fld in ("launch_deps","contract_deps","verify_deps"):
            for dep in leaf[fld]:
                if dep==key:
                    errors.append({"key":key,"code":"self-dependency","message":f"{key}: {fld} contains self dependency"})
                elif dep not in keys:
                    errors.append({"key":key,"code":"dangling-dependency","message":f"{key}: {fld} references unknown key {dep!r}"})

        c=leaf["complexity"]
        if leaf["role"]=="probe-builder" and c!="S":
            errors.append({
                "key":key,
                "code":"probe-size",
                "message":f"{key}: probe-builder leaves must be complexity S; split external discovery/acquisition into bounded probes and a downstream writer",
            })
        if c in TASK_SHAPE_OWNED_LIMIT:
            if len(leaf["owned_artifacts"])>TASK_SHAPE_OWNED_LIMIT[c]:
                errors.append({"key":key,"code":"task-shape","message":f"{key}: {c} leaf owns {len(leaf['owned_artifacts'])} paths; maximum is {TASK_SHAPE_OWNED_LIMIT[c]}"})
            if len(leaf["acceptance_ids"])>TASK_SHAPE_ACCEPTANCE_LIMIT[c]:
                errors.append({"key":key,"code":"task-shape","message":f"{key}: {c} leaf covers {len(leaf['acceptance_ids'])} acceptance IDs; maximum is {TASK_SHAPE_ACCEPTANCE_LIMIT[c]}"})
            if leaf["repeated_operations"]>TASK_SHAPE_REPEAT_LIMIT[c]:
                errors.append({"key":key,"code":"task-shape","message":f"{key}: {c} leaf declares {leaf['repeated_operations']} repeated operations; maximum is {TASK_SHAPE_REPEAT_LIMIT[c]}"})
        shape=f"{leaf['outcome']} {leaf['done_when']}"
        if has_compound_stage_sequence(shape):
            errors.append({"key":key,"code":"compound-stage","message":f"{key}: Outcome/Done when describes multiple explicit stages"})
        if leaf["role"]!="probe-builder" and external_acquisition_match(leaf["outcome"]):
            errors.append({"key":key,"code":"external-acquisition","message":f"{key}: external acquisition/research must be a separate probe-builder leaf"})
        if leaf["role"]=="probe-builder" and HOST_REMEDIATION_RE.search(shape):
            errors.append({"key":key,"code":"host-remediation","message":f"{key}: probe-builder may observe host prerequisites but must not remediate services/packages"})

    errors.extend(ownership_overlap(normalized))

    _, cyc=topo_order(normalized,("launch_deps","contract_deps","verify_deps"))
    if cyc:
        errors.append({"key":"","keys":cyc,"code":"dependency-cycle","message":"dependency cycle involving: "+", ".join(cyc)})

    test=[x for x in normalized if ".opencode-v2/TEST_CHECKS.json" in x["owned_artifacts"]]
    if not test:
        errors.append({"key":"","code":"missing-test-manifest","message":"missing final test-builder leaf owning .opencode-v2/TEST_CHECKS.json"})
    if len(test)>1:
        keys=[leaf["key"] for leaf in test]
        errors.append({
            "key":keys[0],
            "keys":keys,
            "code":"test-manifest-singleton",
            "message":(
                ".opencode-v2/TEST_CHECKS.json is a singleton final manifest and must have "
                "exactly one structured-plan owner. Do not split an oversized final test leaf "
                "into multiple TEST_CHECKS owners. Put bounded helper test scripts/artifacts in "
                "separate test-builder leaves, then make exactly one final test-builder leaf "
                "own TEST_CHECKS.json and depend on those helpers."
            ),
        })
    for leaf in test:
        if leaf["role"]!="test-builder":
            errors.append({"key":leaf["key"],"code":"test-manifest-role","message":f"{leaf['key']}: TEST_CHECKS owner must use test-builder"})
        if leaf["verify_command"]!=RUN_CHECKS_COMMAND:
            errors.append({"key":leaf["key"],"code":"test-manifest-verify","message":f"{leaf['key']}: TEST_CHECKS verify_command must be exact {RUN_CHECKS_COMMAND}"})
    return normalized,errors

def compile_plan(project: Path):
    ctrl=project/".opencode-v2"
    src=ctrl/"IMPLEMENTATION_PLAN.structured.json"
    out=ctrl/"IMPLEMENTATION_PLAN.md"
    mapping_path=ctrl/"IMPLEMENTATION_PLAN.structured-map.json"
    repair=ctrl/"IMPLEMENTATION_PLAN.repair.json"
    guard_err=ctrl/"IMPLEMENTATION_PLAN.guard-errors.txt"

    try:
        raw=json.loads(src.read_text())
    except FileNotFoundError:
        errors=[{"key":"","code":"missing","message":"IMPLEMENTATION_PLAN.structured.json missing"}]
        write_repair(repair,errors,source="structured-compiler")
        return False,errors
    except json.JSONDecodeError as exc:
        errors=[{"key":"","code":"json","message":f"structured plan JSON invalid: line {exc.lineno} column {exc.colno}: {exc.msg}"}]
        write_repair(repair,errors,source="structured-compiler")
        return False,errors

    leaves,errors=normalize_document(raw)
    if errors:
        write_repair(repair,errors,source="structured-compiler")
        atomic_write_text(guard_err,"\n".join(x["message"] for x in errors)+"\n")
        return False,errors

    hard_order,cycle=topo_order(leaves,("launch_deps","contract_deps"))
    if cycle:
        errors=[{"key":"","keys":cycle,"code":"hard-dependency-cycle","message":"Launch/Contract dependency cycle involving: "+", ".join(cycle)}]
        write_repair(repair,errors,source="structured-compiler")
        atomic_write_text(guard_err,"\n".join(x["message"] for x in errors)+"\n")
        return False,errors

    # IDs are assigned by stable hard-dependency topological order. The model
    # never edits IDs/waves, so DAG bookkeeping cannot drift during repair.
    key_to_id={key:f"D{i+1:03d}" for i,key in enumerate(hard_order)}
    by_key={x["key"]:x for x in leaves}

    # Compute deterministic launch wave from Launch+Contract dependencies only.
    wave={}
    for key in hard_order:
        deps=by_key[key]["launch_deps"]+by_key[key]["contract_deps"]
        wave[key]=1+(max((wave[d] for d in deps),default=0))
    waves=defaultdict(list)
    for key in hard_order:
        waves[wave[key]].append(key)

    # Same-wave peers are mechanically parallel candidates; ownership conflicts
    # are already rejected above.
    parallel={}
    for key in hard_order:
        peers=[key_to_id[p] for p in waves[wave[key]] if p!=key]
        parallel[key]=", ".join(peers) if peers else "(none)"

    lines=[
        "# Implementation Plan",
        "Status: COMPLETE",
        "",
        "## Planner checkpoint",
        "Status: COMPLETE",
        "",
        "## Deliverables",
    ]
    id_to_key={}
    for key in hard_order:
        leaf=by_key[key]
        did=key_to_id[key]
        id_to_key[did]=key
        def dep_text(field):
            vals=[key_to_id[x] for x in leaf[field]]
            return "["+", ".join(vals)+"]" if vals else "(none)"
        lines.extend([
            f"### {did} — {leaf['name']}",
            f"- Outcome: {leaf['outcome']}",
            f"- Owned artifacts: {canonical_owned(leaf['owned_artifacts'])}",
            f"- Launch deps: {dep_text('launch_deps')}",
            f"- Contract deps: {dep_text('contract_deps')}",
            f"- Verify deps: {dep_text('verify_deps')}",
            f"- Acceptance IDs: {', '.join(leaf['acceptance_ids'])}",
            f"- Complexity: {leaf['complexity']}",
            "- Independent stages: 1",
            "- Expected compactions: 0",
            f"- Repeated operations: {leaf['repeated_operations']}",
            f"- Deep reasoning: {'yes' if leaf['deep_reasoning'] else 'no'}",
            f"- Role: {leaf['role']}",
            f"- Parallel-safe with: {parallel[key]}",
            f"- Verify command: {leaf['verify_command']}",
            f"- Done when: {leaf['done_when']}",
            "",
        ])
    lines.append("## Execution Waves")
    for n in sorted(waves):
        lines.append(f"- Wave {n}: "+", ".join(key_to_id[k] for k in waves[n]))
    lines.extend(["",PLAN_MARKER,""])
    rendered="\n".join(lines)
    atomic_write_text(out,rendered)
    atomic_write_json(mapping_path,{
        "protocol":MAP_PROTOCOL,
        "source_sha256":hashlib.sha256(src.read_bytes()).hexdigest(),
        "key_to_id":key_to_id,
        "id_to_key":id_to_key,
        "waves":{str(n):[key_to_id[k] for k in waves[n]] for n in sorted(waves)},
    })
    # A runtime repair packet is evidence, not a syntax error. Keep it until
    # the control guard proves an affected structured leaf actually changed.
    guard_err.unlink(missing_ok=True)
    return True,[]

def write_repair(path: Path, errors, source: str):
    """Write an actionable targeted repair packet.

    Global syntax/root failures still require whole-plan repair. A missing final
    TEST_CHECKS leaf is different: it is a bounded structural omission and can
    be repaired by adding/fixing one stable symbolic leaf instead of rewriting
    the complete plan.
    """
    affected=[]
    repair_errors=[]
    for raw in errors:
        e=dict(raw) if isinstance(raw,dict) else {
            "key":"",
            "code":"unknown",
            "message":str(raw),
        }
        if e.get("code")=="missing-test-manifest" and not e.get("key"):
            e["key"]="final_tests"
            e["message"]=(
                str(e.get("message") or "missing final test-builder leaf")
                + " Add or repair exactly one `final_tests` leaf: role "
                  "`test-builder`, owned_artifacts "
                  "[`.opencode-v2/TEST_CHECKS.json`], and verify_command exact "
                  "`.opencode-v2/bin/run-checks`. If the key is absent, adding "
                  "that one named leaf by bounded edit is authorized."
            )
        repair_errors.append(e)
        for k in ([e.get("key")] + list(e.get("keys") or [])):
            if k and k not in affected:
                affected.append(k)
    atomic_write_json(path,{
        "protocol":REPAIR_PROTOCOL,
        "source":source,
        "whole_plan":not bool(affected),
        "affected_keys":affected,
        "errors":repair_errors,
    })

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--project",required=True,type=Path)
    ap.add_argument("--selftest",action="store_true")
    ns=ap.parse_args()
    ok,errors=compile_plan(ns.project.resolve())
    if not ok:
        for e in errors:
            print(e.get("message","structured plan invalid"),file=sys.stderr)
        return 1
    return 0

if __name__=="__main__":
    raise SystemExit(main())
