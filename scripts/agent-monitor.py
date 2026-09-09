#!/usr/bin/env python3
import argparse,json,os,re,sqlite3,sys,time
from datetime import datetime
from pathlib import Path
ROOT=Path.home()/"AI"/"opencode-qwen38-multiagent-v2"; DB=ROOT/"xdg"/"data"/"opencode"/"opencode.db"; LIVE=ROOT/"logs"/"supervisor-live.json"
def args():
    p=argparse.ArgumentParser(); p.add_argument("--project"); p.add_argument("--all",action="store_true"); p.add_argument("--history",type=int,default=40); p.add_argument("--interval",type=float,default=1); p.add_argument("--once",action="store_true"); return p.parse_args()
def db(): c=sqlite3.connect(f"file:{DB}?mode=ro",uri=True,timeout=1); c.row_factory=sqlite3.Row; return c
def resolve(c,p):
    if p: return os.path.realpath(os.path.expanduser(p))
    r=c.execute("SELECT directory,MAX(time_created) t FROM session_v2 WHERE directory IS NOT NULL AND directory<>'' GROUP BY directory ORDER BY t DESC LIMIT 1").fetchone(); return r["directory"] if r else ""
def jj(s):
    try:return json.loads(s)
    except:return {}
def model(raw): d=jj(raw); return d.get("id","") if isinstance(d,dict) else ""
def limit(m):
    x=re.search(r"(\d{2,3})k",m or "",re.I); return int(x.group(1))*1024 if x else None
def ctx(n,l): return "-" if n is None else (f"{n/1024:.1f}/{l/1024:.0f}K" if l else f"{n/1000:.1f}K")
def kk(n): n=int(n or 0); return f"{n/1000:.1f}K" if n>=1000 else str(n)
def fmt(s): s=int(s or 0); return f"{s}s" if s<60 else f"{s//60}m{s%60:02d}s"
def task(c,sid):
    r=c.execute("SELECT data FROM session_message WHERE session_id=? AND type='user' ORDER BY seq LIMIT 1",(sid,)).fetchone()
    if not r:return "-"
    d=jj(r["data"]); t=d.get("text","") if isinstance(d,dict) else ""; m=re.search(r"\b(D\d{3})\b",t); return m.group(1) if m else "-"
def live(c,project,allp):
    try:p=json.loads(LIVE.read_text())
    except:return [],"missing"
    now=time.time(); out=[]
    for x in p.get("sessions",[]):
        if now-float(x.get("seen") or 0)>5: continue
        directory=x.get("directory") or ""
        if not allp and project and directory and os.path.realpath(directory)!=os.path.realpath(project): continue
        sid=x.get("session",""); s=c.execute("SELECT * FROM session_v2 WHERE id=?",(sid,)).fetchone()
        if s and s["time_idle"]: continue
        agent=x.get("agent") or (s["agent"] if s else "unknown"); m=model(s["model"]) if s else ""
        out.append({"id":sid,"agent":agent,"task":x.get("deliverable") or (task(c,sid) if s else "-"),"try":int(x.get("attempt") or 0),"state":"TOOL" if x.get("tool_running") else "RUNNING","ctx":x.get("context_input"),"limit":limit(m),"reason":int(x.get("reasoning") or 0),"text":int(x.get("text") or 0),"age":now-float(x.get("seen") or now)})
    return out,p.get("source","unknown")
def past(c,project,allp,n):
    q="SELECT * FROM session_v2 "; ps=[]
    if not allp:q+="WHERE directory=? "; ps.append(project)
    q+="ORDER BY time_created DESC LIMIT ?"; ps.append(max(200,n*4)); out=[]
    for s in c.execute(q,ps):
        if not s["time_idle"]:continue
        agent=s["agent"] or "unknown"; m=model(s["model"]); peak=outtok=comp=maxr=0; last=""
        for x in c.execute("SELECT type,data FROM session_message WHERE session_id=? ORDER BY seq",(s["id"],)):
            d=jj(x["data"])
            if x["type"]=="assistant":
                t=d.get("tokens") if isinstance(d,dict) else {}
                if isinstance(t,dict):
                    if isinstance(t.get("input"),(int,float)):peak=max(peak,int(t["input"]))
                    if isinstance(t.get("output"),(int,float)):outtok+=int(t["output"])
                cur=0
                for p in d.get("content") or []:
                    if not isinstance(p,dict):continue
                    if p.get("type")=="tool":cur=0
                    elif p.get("type")=="reasoning" and isinstance(p.get("text"),str):cur+=len(p["text"]);maxr=max(maxr,cur)
                    elif p.get("type")=="text":last=p.get("text","")
            elif x["type"]=="compaction":comp+=1
        u=last.upper(); state="BLOCKED" if "BLOCKED" in u else ("DONE" if re.search(r"\b[A-Z0-9_-]+_DONE\b",u) else str(s["idle_outcome"] or "ENDED").upper())
        out.append({"id":s["id"],"agent":agent,"task":task(c,s["id"]),"state":state,"peak":peak or None,"limit":limit(m),"out":outtok,"comp":comp,"maxr":maxr,"dur":max(0,(s["time_idle"]-s["time_created"])/1000)})
        if len(out)>=n:break
    return out
def main():
    a=args()
    while True:
        try:
            c=db(); project="" if a.all else resolve(c,a.project); lr,source=live(c,project,a.all); pr=past(c,project,a.all,a.history)
            if not a.once and sys.stdout.isatty():print("\033[2J\033[H",end="")
            print(f"OpenCode V2 Agent Monitor | {datetime.now():%Y-%m-%d %H:%M:%S}");print(f"Scope: {'ALL' if a.all else project}");print(f"Live source: {source}\n")
            print("LIVE");print(f"{'STATE':10} {'AGENT':22} {'TASK':10} {'TRY':3} {'CTX':12} {'REASON':9} {'TEXT':9} {'AGE':8} {'SESSION':18}");print("-"*112)
            if not lr:print("(none)")
            for r in lr:print(f"{r['state'][:10]:10} {r['agent'][:22]:22} {r['task'][:10]:10} {r['try']:3d} {ctx(r['ctx'],r['limit']):12} {kk(r['reason']):9} {kk(r['text']):9} {fmt(r['age']):8} {r['id'][:18]:18}")
            print("\nPAST");print(f"{'RESULT':10} {'AGENT':22} {'TASK':10} {'PEAK':12} {'OUT':9} {'MAX-R':9} {'COMP':4} {'DURATION':9} {'SESSION':18}");print("-"*112)
            if not pr:print("(none)")
            for r in pr:print(f"{r['state'][:10]:10} {r['agent'][:22]:22} {r['task'][:10]:10} {ctx(r['peak'],r['limit']):12} {kk(r['out']):9} {kk(r['maxr']):9} {r['comp']:4d} {fmt(r['dur']):9} {r['id'][:18]:18}")
            print("\nCtrl-C to exit.");c.close()
            if a.once:return
            time.sleep(a.interval)
        except KeyboardInterrupt:print();return
        except sqlite3.OperationalError as e:
            if a.once:raise
            print(f"SQLite busy: {e}",file=sys.stderr);time.sleep(a.interval)
if __name__=="__main__":main()
