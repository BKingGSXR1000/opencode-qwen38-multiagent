#!/usr/bin/env python3
"""Summarize V2 progress-aware watchdog telemetry and control-plane anomalies."""
from __future__ import annotations
import argparse,json,sqlite3,time
from collections import defaultdict
from pathlib import Path

ROOT=Path.home()/"AI"/"opencode-qwen38-multiagent-v2"
DEFAULT_LOG=ROOT/"logs"/"watchdog-telemetry.jsonl"
DEFAULT_LIVE=ROOT/"logs"/"supervisor-live.json"
DEFAULT_DB=ROOT/"xdg"/"data"/"opencode"/"opencode.db"


def load_rows(path: Path):
    """Parse proper JSONL and the older literal-backslash-n concatenated log.

    JSONDecoder.raw_decode preserves escaped delimiter-looking text inside
    JSON string fields. Blind split on the literal characters backslash+n
    would corrupt real model/diagnostic values. A damaged row is skipped to
    the next actual newline or legacy object separator.
    """
    try:
        text=path.read_text(encoding="utf-8",errors="replace")
    except OSError:
        return []
    decoder=json.JSONDecoder()
    rows=[]
    index=0
    length=len(text)
    while index<length:
        if text.startswith("\\n",index):
            index+=2
        while index<length and text[index].isspace():
            index+=1
        if index>=length:
            break
        try:
            item,end=decoder.raw_decode(text,index)
        except json.JSONDecodeError:
            newline=text.find("\n",index)
            legacy=text.find("\\n{",index)
            points=[x for x in (newline,legacy) if x>=0]
            if not points:
                break
            index=min(points)+1 if newline>=0 and newline==min(points) else min(points)+2
            continue
        if isinstance(item,dict) and item.get("session"):
            rows.append(item)
        index=end
    return rows


def load_live(path: Path):
    try:
        data=json.loads(path.read_text(encoding="utf-8",errors="replace"))
    except (OSError,json.JSONDecodeError):
        return "",[]
    if not isinstance(data,dict):
        return "",[]
    rows=[]
    for item in data.get("sessions") or []:
        if not isinstance(item,dict) or not item.get("session"):
            continue
        row=dict(item)
        row.setdefault("epoch",float(data.get("generated") or time.time()))
        rows.append(row)
    return str(data.get("project") or ""),rows


def summarize(rows):
    groups=defaultdict(list)
    for row in rows:
        groups[str(row.get("session"))].append(row)
    out=[]
    for sid,items in groups.items():
        items.sort(key=lambda x:float(x.get("epoch") or x.get("seen") or 0))
        phases=defaultdict(float)
        for i,row in enumerate(items):
            if i+1<len(items):
                a=float(row.get("epoch") or row.get("seen") or 0)
                b=float(items[i+1].get("epoch") or items[i+1].get("seen") or 0)
                dt=max(0.0,min(10.0,b-a))
            else:
                dt=0.0
            phases[str(row.get("watchdog_phase") or "unknown")]+=dt
        latest=items[-1]
        adaptive_rows=[
            row for row in items
            if isinstance(row.get("adaptive_reasoning"),dict)
            and row["adaptive_reasoning"].get("profile")
        ]
        adaptive_summary={}
        if adaptive_rows:
            events=[]
            previous_candidate=""
            for row in adaptive_rows:
                decision=row["adaptive_reasoning"]
                reason=(str(decision.get("reason") or "")
                        if decision.get("abort") else "")
                if reason and reason!=previous_candidate:
                    events.append(reason)
                previous_candidate=reason
            last=adaptive_rows[-1]
            verdict=last["adaptive_reasoning"]
            adaptive_summary={
                "mode":last.get("adaptive_reasoning_mode") or "unknown",
                "profile":verdict.get("profile"),
                "latest_gate":verdict.get("gate"),
                "max_action_age":max(float(x["adaptive_reasoning"].get("action_age") or 0)
                                      for x in adaptive_rows),
                "max_reasoning_chars_since_action":max(
                    int(x["adaptive_reasoning"].get("reasoning_chars_since_action") or 0)
                    for x in adaptive_rows
                ),
                "candidate_episodes":len(events),
                "candidate_reasons":dict(__import__("collections").Counter(events)),
            }
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
            "adaptive_reasoning":adaptive_summary,
        })
    return sorted(out,key=lambda x:(x.get("deliverable") or "",x["session"]))


def _json(raw):
    try:
        obj=json.loads(raw or "{}")
        return obj if isinstance(obj,dict) else {}
    except Exception:
        return {}


def detect_anomalies(project: str, db_path: Path):
    """Event-first anomalies that must survive eventual run recovery/success."""
    if not project or not db_path.exists():
        return []
    anomalies=[]
    try:
        con=sqlite3.connect(str(db_path))
        sessions=con.execute(
            "SELECT id,coalesce(agent,''),coalesce(time_created,0) "
            "FROM session_v2 WHERE directory=? ORDER BY time_created",
            (project,),
        ).fetchall()
        if not sessions:
            con.close(); return []
        by_sid={sid:{"agent":agent,"created":int(created or 0)} for sid,agent,created in sessions}
        compactions=con.execute(
            "SELECT session_id,seq,data FROM session_message "
            "WHERE type='compaction' AND session_id IN "
            "(SELECT id FROM session_v2 WHERE directory=?) ORDER BY seq",
            (project,),
        ).fetchall()
        assistants=con.execute(
            "SELECT session_id,seq,data FROM session_message "
            "WHERE type='assistant' AND session_id IN "
            "(SELECT id FROM session_v2 WHERE directory=?) ORDER BY seq",
            (project,),
        ).fetchall()
        con.close()
    except sqlite3.Error as exc:
        return [{"severity":"warning","type":"analysis-db-error","detail":str(exc)}]

    failed_roots=[]
    for sid,seq,raw in compactions:
        data=_json(raw)
        if data.get("status")!="failed":
            continue
        err=data.get("error") if isinstance(data.get("error"),dict) else {}
        created=int(((data.get("time") or {}).get("created") or by_sid.get(sid,{}).get("created") or 0))
        item={
            "severity":"high" if by_sid.get(sid,{}).get("agent")=="orchestrator" else "medium",
            "type":"compaction-failed",
            "session":sid,
            "agent":by_sid.get(sid,{}).get("agent",""),
            "seq":seq,
            "time_ms":created,
            "error_type":err.get("type",""),
            "message":err.get("message",""),
        }
        anomalies.append(item)
        if item["agent"]=="orchestrator":
            failed_roots.append(item)

    for sid,seq,raw in assistants:
        data=_json(raw)
        if str(data.get("finish") or "")!="length":
            continue
        tokens=data.get("tokens") if isinstance(data.get("tokens"),dict) else {}
        model=data.get("model") if isinstance(data.get("model"),dict) else {}
        anomalies.append({
            "severity":"medium",
            "type":"output-limit-hit",
            "session":sid,
            "agent":by_sid.get(sid,{}).get("agent",""),
            "seq":seq,
            "model":model.get("id",""),
            "output_tokens":int(tokens.get("output") or 0),
            "reasoning_tokens":int(tokens.get("reasoning") or 0),
        })

    for failure in failed_roots:
        t=int(failure.get("time_ms") or 0)
        successor=next((
            (sid,created) for sid,agent,created in sessions
            if agent=="orchestrator" and int(created or 0)>t and sid!=failure["session"]
        ),None)
        if not successor:
            continue
        workers=[
            sid for sid,agent,created in sessions
            if agent!="orchestrator" and int(created or 0)>=int(successor[1] or 0)
        ]
        if workers:
            anomalies.append({
                "severity":"high",
                "type":"detached-parent-hidden-recovery",
                "failed_parent":failure["session"],
                "successor_parent":successor[0],
                "worker_sessions_after_successor":len(workers),
                "message":"Visible parent failed compaction while a successor orchestrator and workers continued in background.",
            })
    return anomalies


def human(summary,anomalies):
    if anomalies:
        print("ANOMALIES:")
        for item in anomalies:
            kind=item.get("type","unknown")
            sev=str(item.get("severity","warning")).upper()
            if kind=="detached-parent-hidden-recovery":
                print(
                    f"  [{sev}] {kind}: failed_parent={item.get('failed_parent')} "
                    f"successor={item.get('successor_parent')} "
                    f"workers_after={item.get('worker_sessions_after_successor')}"
                )
            elif kind=="compaction-failed":
                print(
                    f"  [{sev}] {kind}: session={item.get('session')} agent={item.get('agent')} "
                    f"{item.get('error_type')} {item.get('message')}"
                )
            elif kind=="output-limit-hit":
                print(
                    f"  [{sev}] {kind}: session={item.get('session')} agent={item.get('agent')} "
                    f"model={item.get('model')} output={item.get('output_tokens')} "
                    f"reasoning={item.get('reasoning_tokens')}"
                )
            else:
                print(f"  [{sev}] {kind}: {item.get('detail') or item.get('message') or ''}")
        print()
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
        adaptive=item.get("adaptive_reasoning") or {}
        if adaptive:
            print(
                "  adaptive: "
                f"mode={adaptive.get('mode')} profile={adaptive.get('profile')} "
                f"max_action_age={adaptive.get('max_action_age',0):.1f}s "
                f"max_reasoning_chars={adaptive.get('max_reasoning_chars_since_action',0)} "
                f"would_interrupt_episodes={adaptive.get('candidate_episodes',0)}"
            )


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--log",type=Path,default=DEFAULT_LOG)
    ap.add_argument("--live",type=Path,default=DEFAULT_LIVE)
    ap.add_argument("--db",type=Path,default=DEFAULT_DB)
    ap.add_argument("--project",default="")
    ap.add_argument("--json",action="store_true")
    ns=ap.parse_args()
    rows=load_rows(ns.log)
    live_project,live_rows=load_live(ns.live)
    if not rows:
        rows=live_rows
    project=ns.project or live_project
    # A project-scoped report must not quietly mix sessions from previous
    # canaries that wrote into this repo-wide telemetry log.
    if ns.project:
        rows=[row for row in rows if row.get("directory")==ns.project]
    result=summarize(rows)
    anomalies=detect_anomalies(project,ns.db)
    if ns.json:
        print(json.dumps({"project":project,"anomalies":anomalies,"sessions":result},indent=2,sort_keys=True))
    else:
        human(result,anomalies)
    return 0

if __name__=="__main__":
    raise SystemExit(main())
