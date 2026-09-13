#!/usr/bin/env python3
"""Summarize V2 progress-aware watchdog telemetry."""
from __future__ import annotations
import argparse,json,time
from collections import defaultdict
from pathlib import Path

ROOT=Path.home()/"AI"/"opencode-qwen38-multiagent-v2"
DEFAULT_LOG=ROOT/"logs"/"watchdog-telemetry.jsonl"
DEFAULT_LIVE=ROOT/"logs"/"supervisor-live.json"


def load_rows(path: Path):
    rows=[]
    try:
        for line in path.read_text(encoding="utf-8",errors="replace").splitlines():
            try: item=json.loads(line)
            except json.JSONDecodeError: continue
            if isinstance(item,dict) and item.get("session"):
                rows.append(item)
    except OSError:
        pass
    return rows


def summarize(rows):
    groups=defaultdict(list)
    for row in rows:
        groups[str(row.get("session"))].append(row)
    out=[]
    for sid,items in groups.items():
        items.sort(key=lambda x:float(x.get("epoch") or 0))
        phases=defaultdict(float)
        for i,row in enumerate(items):
            if i+1<len(items):
                dt=max(0.0,min(10.0,float(items[i+1].get("epoch") or 0)-float(row.get("epoch") or 0)))
            else:
                dt=0.0
            phases[str(row.get("watchdog_phase") or "unknown")]+=dt
        latest=items[-1]
        out.append({
            "session":sid,
            "agent":latest.get("agent"),
            "deliverable":latest.get("deliverable"),
            "attempt":latest.get("attempt"),
            "latest_phase":latest.get("watchdog_phase"),
            "reasoning_tokens":latest.get("reasoning_tokens",0),
            "output_tokens":latest.get("output_tokens",0),
            "visible_no_progress_age":latest.get("visible_no_progress_age",0),
            "invisible_no_progress_age":latest.get("invisible_no_progress_age",0),
            "phase_seconds":dict(sorted(phases.items(),key=lambda kv:(-kv[1],kv[0]))),
            "backend":latest.get("backend") or {},
        })
    return sorted(out,key=lambda x:(x.get("deliverable") or "",x["session"]))


def human(summary):
    if not summary:
        print("No watchdog telemetry found yet.")
        return
    for item in summary:
        print(
            f"{item.get('deliverable') or '-':10} "
            f"{item.get('agent') or '-':24} "
            f"phase={item.get('latest_phase') or '-':28} "
            f"reasoning_tok={item.get('reasoning_tokens',0):>7} "
            f"output_tok={item.get('output_tokens',0):>7}"
        )
        phases=item.get("phase_seconds") or {}
        if phases:
            print("  observed: "+", ".join(f"{k}={v:.0f}s" for k,v in phases.items()))
        backend=item.get("backend") or {}
        if backend:
            print(
                "  backend: "
                f"running={backend.get('running')} waiting={backend.get('waiting')} "
                f"prompt={backend.get('prompt_tokens')} gen={backend.get('generation_tokens')} "
                f"gpu={backend.get('gpu_util')}% kv={backend.get('kv_usage')}"
            )
        print(
            f"  no-progress: visible={item.get('visible_no_progress_age',0)}s "
            f"invisible={item.get('invisible_no_progress_age',0)}s"
        )


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--log",type=Path,default=DEFAULT_LOG)
    ap.add_argument("--json",action="store_true")
    ns=ap.parse_args()
    result=summarize(load_rows(ns.log))
    if ns.json:
        print(json.dumps(result,indent=2,sort_keys=True))
    else:
        human(result)
    return 0

if __name__=="__main__":
    raise SystemExit(main())
