#!/usr/bin/env python3
import base64,fcntl,json,os,re,sqlite3,subprocess,sys,threading,time,urllib.parse,urllib.request
from pathlib import Path
from control_state import phase_ready, ready_info as state_ready_info

ROOT=Path.home()/"AI"/"opencode-qwen38-multiagent-v2"
DB=ROOT/"xdg"/"data"/"opencode"/"opencode.db"
LOG=ROOT/"logs"/"supervisor-events.log"; CSV=ROOT/"logs"/"supervisor-events.csv"; LIVE_STATUS=ROOT/"logs"/"supervisor-live.json"
PROJECT=os.environ.get("V2_PROJECT","")
START_MS=int(time.time()*1000)-5000; POLL=0.5
HARD_SECONDS=120; HARD_REASONING_CHARS=8000; HARD_TEXT_CHARS=12000
IMPLEMENTATION_AGENTS={"probe-builder","implementer","core-builder","feature-builder","reasoning-builder","integrator","tester","test-builder"}
lock=threading.RLock(); dispatch_lock=threading.RLock(); watch={}; dispatch_seen=set(); session_task={}; abort_count={}; compaction_seen={}
root_seen_active=False; root_idle_since=None; lessons_started=False; lessons_launch_attempts=0

def log(msg):
    ROOT.joinpath("logs").mkdir(parents=True,exist_ok=True)
    line=f"[{time.strftime('%Y-%m-%dT%H:%M:%S%z')}] {msg}"
    with open(LOG,"a",encoding="utf-8") as f: f.write(line+"\n")
    print(line,file=sys.stderr,flush=True)

def csv(kind,sid="",agent="",detail=""):
    new=not CSV.exists(); detail=str(detail).replace('"','""')
    with open(CSV,"a",encoding="utf-8") as f:
        if new: f.write("timestamp,event,session,agent,detail\n")
        f.write(f'{time.strftime("%Y-%m-%dT%H:%M:%S%z")},{kind},{sid},{agent},"{detail}"\n')

def db_connect(): return sqlite3.connect(f"file:{DB}?mode=ro",uri=True,timeout=1)

def first_user_text_db(sid):
    try:
        con=db_connect(); row=con.execute("SELECT data FROM session_message WHERE session_id=? AND type='user' ORDER BY seq LIMIT 1",(sid,)).fetchone(); con.close()
        if not row: return ""
        d=json.loads(row[0]); return d.get("text","") if isinstance(d,dict) else ""
    except Exception: return ""

def parse_deliverable(text):
    if not text: return ""
    m=re.search(r"(?mi)^\s*DELIVERABLE\s*:\s*(D\d{3})\s*$",text)
    if m: return m.group(1)
    m=re.search(r"(?<![A-Za-z0-9_-])(D\d{3})(?![A-Za-z0-9_-])",text)
    return m.group(1) if m else ""

def load_manifest():
    if not PROJECT: return {}
    try: return json.loads((Path(PROJECT)/".opencode-v2"/"IMPLEMENTATION_PLAN.guard.json").read_text())
    except Exception: return {}

def ready_info(did):
    return state_ready_info(PROJECT,did) if PROJECT and did else {}

def plan_ready():
    return bool(PROJECT) and phase_ready(
        PROJECT,"IMPLEMENTATION_PLAN.ready","IMPLEMENTATION_PLAN.md",
        "IMPLEMENTATION_PLAN_COMPLETE",
    )

def attempts_path(): return Path(PROJECT)/".opencode-v2"/"work"/"attempts.json"
def load_attempts():
    try: return json.loads(attempts_path().read_text())
    except Exception: return {"protocol":"V2.6.7","deliverables":{}}
def save_attempts(data):
    p=attempts_path(); p.parent.mkdir(parents=True,exist_ok=True); t=p.with_suffix(".tmp"); t.write_text(json.dumps(data,indent=2)+"\n"); os.replace(t,p)

def claim_attempt(sid,did):
    """Atomically reserve one of the three allowed attempts for an exact leaf.

    Both the live HTTP dispatcher and persisted reconciliation call this helper.
    A denied fourth attempt is never persisted, so a ready-file verifier can
    never be poisoned by an impossible ledger count.
    """
    with dispatch_lock:
        p=attempts_path(); p.parent.mkdir(parents=True,exist_ok=True)
        lock_path=p.with_name(p.name+".lock")
        with lock_path.open("a+") as ledger_lock:
            fcntl.flock(ledger_lock.fileno(),fcntl.LOCK_EX)
            try:
                data=load_attempts()
                ent=data.setdefault("deliverables",{}).setdefault(did,{"sessions":[],"count":0})
                sessions=ent.setdefault("sessions",[])
                count=int(ent.get("count") or 0)
                if sid in sessions:
                    session_task[sid]=(did,count)
                    return "existing",count
                if count>=3:
                    return "limit",count
                count+=1; ent["count"]=count; sessions.append(sid)
                data["owner"]="supervisor"
                save_attempts(data)
                session_task[sid]=(did,count)
                return "claimed",count
            finally:
                fcntl.flock(ledger_lock.fileno(),fcntl.LOCK_UN)

class OpenCodeHTTP:
    def __init__(self):
        self.base=None
        self.password=None
        self.prefix=None
        self.last_discover=0
        self.last_wait_log=0

    def headers(self,json_body=False):
        h={"Accept":"application/json"}
        if self.password:
            tok=base64.b64encode(f"opencode:{self.password}".encode()).decode()
            h["Authorization"]=f"Basic {tok}"
        if json_body:
            h["Content-Type"]="application/json"
        return h

    def request(self,method,path,payload=None,timeout=3):
        if not self.base:
            raise RuntimeError("no server")
        body=json.dumps(payload).encode() if payload is not None else None
        req=urllib.request.Request(
            self.base+path,
            data=body,
            method=method,
            headers=self.headers(payload is not None),
        )
        with urllib.request.urlopen(req,timeout=timeout) as r:
            raw=r.read()
        if not raw:
            return {}
        try:
            obj=json.loads(raw)
        except Exception:
            return raw.decode("utf-8","replace")
        return obj.get("data") if isinstance(obj,dict) and set(obj)=={"data"} else obj

    def discover(self):
        target=os.path.realpath(str(ROOT/"xdg"/"config"))
        ss=""
        try:
            ss=subprocess.check_output(
                ["ss","-ltnp"],
                text=True,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass

        # Observed standalone topology on this machine:
        # opencode2 -> opencode2.exe serve --stdio --port 0
        # The child owns a random 127.0.0.1 TCP port and OPENCODE_PASSWORD.
        for proc in Path("/proc").glob("[0-9]*"):
            try:
                pid=int(proc.name)
                cmd=(proc/"cmdline").read_bytes().replace(
                    b"\0",b" "
                ).decode("utf-8","replace")

                low=cmd.lower()
                if "opencode2" not in low:
                    continue
                if "serve --stdio" not in low:
                    continue

                env={}
                for item in (proc/"environ").read_bytes().split(b"\0"):
                    if b"=" not in item:
                        continue
                    k,v=item.split(b"=",1)
                    env[k.decode("utf-8","replace")]=v.decode("utf-8","replace")

                xdg=env.get("XDG_CONFIG_HOME")
                if xdg and os.path.realpath(xdg)!=target:
                    continue

                password=env.get("OPENCODE_PASSWORD") or env.get(
                    "OPENCODE_SERVER_PASSWORD"
                )
                if not password:
                    continue

                ports=[]
                for line in ss.splitlines():
                    if f"pid={pid}" not in line:
                        continue
                    ports += [
                        int(x.group(1))
                        for x in re.finditer(r"127\.0\.0\.1:(\d+)",line)
                    ]

                for port in dict.fromkeys(ports):
                    self.base=f"http://127.0.0.1:{port}"
                    self.password=password

                    probes=[]
                    if PROJECT:
                        probes.append(
                            "/api/session/active?"
                            + urllib.parse.urlencode({"directory":PROJECT})
                        )
                    probes += ["/api/health","/global/health"]

                    for path in probes:
                        try:
                            # Any successful 2xx response proves this is the
                            # isolated V2 OpenCode HTTP service. {} is valid.
                            self.request("GET",path,timeout=2)
                            self.prefix="/api"
                            log(
                                f"HTTP_CONTROL_CONNECTED base={self.base} "
                                f"prefix=/api probe={path} pid={pid}"
                            )
                            csv(
                                "HTTP_CONTROL_CONNECTED",
                                detail=f"{self.base}/api probe={path} pid={pid}",
                            )
                            return True
                        except Exception:
                            pass
            except Exception:
                continue

        self.base=self.password=self.prefix=None
        return False

    def ensure(self):
        if self.base and self.prefix is not None:
            return True
        now=time.time()
        if now-self.last_discover<1:
            return False
        self.last_discover=now
        ok=self.discover()
        if not ok and now-self.last_wait_log>10:
            self.last_wait_log=now
            log("HTTP_CONTROL_WAIT no compatible OpenCode server discovered")
        return ok

    def get_status(self):
        if not self.ensure():
            return {}
        q=""
        if PROJECT:
            q="?" + urllib.parse.urlencode({"directory":PROJECT})
        try:
            obj=self.request("GET","/api/session/active"+q)
            return obj if isinstance(obj,dict) else {}
        except Exception:
            self.base=self.prefix=None
            raise

    def get_session(self,sid):
        if not self.ensure():
            return {}
        qsid=urllib.parse.quote(sid)
        for path in (
            f"/api/session/{qsid}",
            f"/session/{qsid}",
        ):
            try:
                obj=self.request("GET",path)
                return obj if isinstance(obj,dict) else {}
            except Exception:
                pass
        return {}

    def get_messages(self,sid):
        if not self.ensure():
            return []
        qsid=urllib.parse.quote(sid)
        for path in (
            f"/api/session/{qsid}/message",
            f"/api/session/{qsid}/message?limit=32",
            f"/session/{qsid}/message",
            f"/session/{qsid}/message?limit=32",
        ):
            try:
                obj=self.request("GET",path,timeout=4)
                if isinstance(obj,list):
                    return obj
                if isinstance(obj,dict):
                    for k in ("messages","items","data"):
                        value=obj.get(k)
                        if isinstance(value,list):
                            return value
                        if isinstance(value,dict):
                            for sk in ("messages","items","data"):
                                if isinstance(value.get(sk),list):
                                    return value[sk]
            except Exception:
                pass
        return []

    def interrupt(self,sid):
        if not self.ensure():
            return False
        qsid=urllib.parse.quote(sid)
        for path,payload in (
            (f"/api/session/{qsid}/interrupt",None),
            (f"/session/{qsid}/abort",{}),
        ):
            try:
                self.request("POST",path,payload=payload,timeout=4)
                return True
            except Exception:
                pass
        return False

    def start_lessons_session(self,text):
        """Launch lessons in its own session via the current OpenCode2 API."""
        if not self.ensure():
            return False,"http-not-connected"
        try:
            created=self.request(
                "POST","/api/session",
                payload={
                    "agent":"lessons-learner",
                    "location":PROJECT,
                    "title":"V2 lessons retrospective",
                },
                timeout=8,
            )
            sid=created.get("id") if isinstance(created,dict) else ""
            if not sid:
                return False,"session-create-missing-id"
            # OpenCode2's current route accepts text/files/agents and delivery;
            # there is no prompt_async endpoint. A dedicated idle session makes
            # steer immediate without consuming the root orchestrator budget.
            self.request(
                "POST",f"/api/session/{urllib.parse.quote(sid)}/prompt",
                payload={"text":text,"delivery":"steer"},
                timeout=12,
            )
            return True,sid
        except Exception as e:
            return False,repr(e)

http=OpenCodeHTTP()

def abort_session(sid,reason,agent=""):
    ok=http.interrupt(sid); kind="INTERRUPT" if ok else "INTERRUPT_FAILED"; log(f"{kind} session={sid} agent={agent} reason={reason}"); csv(kind,sid,agent,reason); return ok

def watchdog_age(sid,key,can_watch,now=None):
    """Advance a watchdog only for observable, unfinished no-tool output."""
    now=time.monotonic() if now is None else now
    st=watch.setdefault(sid,{"key":key,"start":None,"aborted_key":None})
    if st["key"]!=key:
        st["key"]=key; st["start"]=None; st["aborted_key"]=None
    if can_watch and st["start"] is None:
        st["start"]=now
    elif not can_watch:
        st["start"]=None
    return (now-st["start"]) if st["start"] is not None else 0,st

def message_shape(messages,session_info):
    # Normalize message order: API endpoints may return ascending or descending.
    normalized=[]
    for idx,item in enumerate(messages or []):
        if not isinstance(item,dict):
            continue
        info=item.get("info") if isinstance(item.get("info"),dict) else item
        if isinstance(item.get("parts"),list):
            parts=item["parts"]; parts_observable=bool(parts)
        elif isinstance(item.get("content"),list):
            parts=item["content"]; parts_observable=bool(parts)
        else:
            # The live endpoint can publish a message shell before it publishes
            # its parts. That is unknown, not an empty completed response.
            parts=[]; parts_observable=False
        if not isinstance(info,dict):
            continue
        tm=info.get("time") if isinstance(info.get("time"),dict) else {}
        created=tm.get("created") if isinstance(tm.get("created"),(int,float)) else 0
        normalized.append((created,idx,info,parts,parts_observable))
    normalized.sort(key=lambda x:(x[0],x[1]))

    users=[x for x in normalized if x[2].get("role")=="user"]
    assistants=[x for x in normalized if x[2].get("role")=="assistant"]

    first_user=""
    user_agent=""
    if users:
        _,_,uinfo,uparts,_=users[0]
        if isinstance(uinfo.get("agent"),str) and uinfo.get("agent"):
            user_agent=uinfo["agent"]
        first_user="\n".join(
            p.get("text","")
            for p in uparts
            if isinstance(p,dict)
            and p.get("type")=="text"
            and isinstance(p.get("text"),str)
        )

    agent=user_agent or (session_info.get("agent") if isinstance(session_info,dict) else "") or "unknown"
    parent=(session_info.get("parentID") or session_info.get("parent_id") or "") if isinstance(session_info,dict) else ""
    directory=(session_info.get("directory") or "") if isinstance(session_info,dict) else ""

    if not assistants:
        return {
            "agent":agent,"parent":parent,"directory":directory,"first_user":first_user,
            "message_id":"","reasoning":0,"text":0,"tool_running":False,
            "last_tool_id":"","context_input":None,"observable":False,
            "assistant_completed":False,
        }

    _,_,info,parts,parts_observable=assistants[-1]
    last_tool=-1
    last_tool_id=""
    tool_running=False
    for i,p in enumerate(parts):
        if not isinstance(p,dict) or p.get("type")!="tool":
            continue
        last_tool=i
        last_tool_id=p.get("id") or str(i)
        st=p.get("state") if isinstance(p.get("state"),dict) else {}
        tool_running=tool_running or st.get("status")=="running"

    reasoning=0
    text_chars=0
    for p in parts[last_tool+1:]:
        if not isinstance(p,dict):
            continue
        if p.get("type")=="reasoning" and isinstance(p.get("text"),str):
            reasoning+=len(p["text"])
        elif p.get("type")=="text" and isinstance(p.get("text"),str):
            text_chars+=len(p["text"])

    tokens=info.get("tokens") if isinstance(info.get("tokens"),dict) else {}
    ci=tokens.get("input")
    ci=int(ci) if isinstance(ci,(int,float)) else None
    tm=info.get("time") if isinstance(info.get("time"),dict) else {}
    completed=isinstance(tm.get("completed"),(int,float))

    return {
        "agent":agent,"parent":parent,"directory":directory,"first_user":first_user,
        "message_id":info.get("id") or "","reasoning":reasoning,"text":text_chars,
        "tool_running":tool_running,"last_tool_id":last_tool_id,
        "context_input":ci,"observable":parts_observable,"assistant_completed":completed,
    }

def enforce_assignment(sid,agent,first_user):
    if agent not in IMPLEMENTATION_AGENTS or sid in dispatch_seen:
        return

    # Live message persistence can lag /session/active. Missing prompt identity
    # is UNKNOWN, not a policy violation. Fall back to SQLite, then wait.
    text=first_user or first_user_text_db(sid)
    if not text:
        return

    did=parse_deliverable(text)
    if not re.fullmatch(r"D\d{3}",did or ""):
        log(f"DISPATCH_PENDING_ID session={sid} agent={agent} prompt_seen=1 no_exact_Dxxx")
        return

    # Only consume dispatch_seen once the exact planned ID is observable.
    dispatch_seen.add(sid)

    ctrl=Path(PROJECT)/".opencode-v2" if PROJECT else None
    if not ctrl or not plan_ready():
        abort_session(sid,"dispatch_guard plan_not_ready",agent)
        csv("DISPATCH_DENY",sid,agent,"plan_not_ready")
        return

    leaves=(load_manifest().get("leaves") or {})
    if did not in leaves:
        abort_session(sid,f"dispatch_guard unknown_deliverable={did}",agent)
        csv("DISPATCH_DENY",sid,agent,f"unknown_deliverable={did}")
        return

    missing=[d for d in leaves[did].get("launch_deps",[]) if not ready_info(d)]
    if missing:
        abort_session(sid,f"dispatch_guard unmet_launch_deps={','.join(missing)} deliverable={did}",agent)
        csv("DISPATCH_DENY",sid,agent,f"{did} unmet_launch_deps={','.join(missing)}")
        return

    if ready_info(did):
        abort_session(sid,f"dispatch_guard already_complete deliverable={did}",agent)
        csv("DISPATCH_DENY",sid,agent,f"{did} already_complete")
        return

    claim,n=claim_attempt(sid,did)
    if claim=="limit":
        abort_session(sid,f"dispatch_guard attempt_limit deliverable={did} count={n}",agent)
        csv("DISPATCH_DENY",sid,agent,f"{did} attempt_limit={n}")
        return

    log(f"DISPATCH_ALLOW session={sid} agent={agent} deliverable={did} attempt={n}")
    csv("DISPATCH_ALLOW",sid,agent,f"{did} attempt={n}")

def root_orchestrator_id():
    if not PROJECT: return ""
    try:
        con=db_connect(); row=con.execute("SELECT id FROM session_v2 WHERE agent='orchestrator' AND directory=? AND time_created>=? ORDER BY time_created DESC LIMIT 1",(PROJECT,START_MS)).fetchone(); con.close(); return row[0] if row else ""
    except Exception: return ""

def control_guard(kind):
    if not PROJECT:
        return False
    flag="--finalize-acceptance" if kind=="acceptance" else "--finalize-plan"
    try:
        r=subprocess.run(
            [sys.executable,str(ROOT/"scripts"/"control-guard.py"),
             "--project",PROJECT,flag],
            capture_output=True,text=True,timeout=8
        )
        if r.returncode!=0:
            detail=(r.stdout+r.stderr).strip().replace("\n"," | ")
            log(f"CONTROL_GUARD_INVALID kind={kind} detail={detail[:1600]}")
        return r.returncode==0
    except Exception as e:
        log(f"CONTROL_GUARD_ERROR kind={kind} error={e!r}")
        return False

def control_guard_loop():
    sigs={}
    while True:
        try:
            if PROJECT:
                ctrl=Path(PROJECT)/".opencode-v2"
                specs=(
                    ("ACCEPTANCE.md","<!-- ACCEPTANCE_COMPLETE -->","acceptance"),
                    ("IMPLEMENTATION_PLAN.md","<!-- IMPLEMENTATION_PLAN_COMPLETE -->","plan"),
                )
                for name,marker,kind in specs:
                    file=ctrl/name
                    if not file.exists():
                        continue
                    sig=(file.stat().st_mtime_ns,file.stat().st_size)
                    if sigs.get(name)==sig:
                        continue
                    lines=[x.strip() for x in file.read_text(errors="replace").splitlines() if x.strip()]
                    if not lines or lines[-1]!=marker:
                        continue
                    sigs[name]=sig
                    if control_guard(kind):
                        log(f"CONTROL_GUARD finalized={name}")
                        csv("CONTROL_GUARD",detail=f"finalized={name}")
        except Exception as e:
            log(f"CONTROL_GUARD_ERROR {e!r}")
        time.sleep(0.5)

def sync_global_lessons():
    if not PROJECT: return
    try:
        src=ROOT/"knowledge"/"LESSONS_GLOBAL.md"; dst=Path(PROJECT)/".opencode-v2"/"GLOBAL_LESSONS.md"
        if src.exists(): dst.parent.mkdir(parents=True,exist_ok=True); tmp=dst.with_suffix(".tmp"); tmp.write_text(src.read_text()); os.replace(tmp,dst)
    except Exception as e: log(f"LESSONS_SYNC_ERROR {e!r}")

def merge_lesson_candidates():
    if not PROJECT: return
    try:
        import hashlib
        ctrl=Path(PROJECT)/".opencode-v2"; ready=ctrl/"LESSONS.ready"; cand=ctrl/"LESSONS_GLOBAL_CANDIDATES.md"
        if not ready.exists() or not cand.exists(): return
        text=cand.read_text(errors="replace").strip()
        if not text: return
        h=hashlib.sha256(text.encode()).hexdigest()[:16]; gf=ROOT/"knowledge"/"LESSONS_GLOBAL.md"; gf.parent.mkdir(parents=True,exist_ok=True); existing=gf.read_text(errors="replace") if gf.exists() else ""; marker=f"<!-- candidate-batch:{h} -->"
        if marker in existing: return
        with gf.open("a") as f: f.write("\n\n"+marker+"\n"+f"## Candidate batch {h}\nSource project: `{PROJECT}`\nStatus: candidate — advisory until repeated evidence promotes it.\n\n"+text+"\n")
        log(f"LESSONS_GLOBAL_MERGED batch={h}")
    except Exception as e: log(f"LESSONS_MERGE_ERROR {e!r}")

def maybe_launch_lessons(active_sids,child_active):
    global root_seen_active,root_idle_since,lessons_started,lessons_launch_attempts
    if lessons_started or not PROJECT: return
    ctrl=Path(PROJECT)/".opencode-v2"
    if (ctrl/"LESSONS.ready").exists(): lessons_started=True; return
    root=root_orchestrator_id()
    if not root: return
    if root in active_sids: root_seen_active=True; root_idle_since=None; return
    # A short root turn can finish between HTTP polls. Its durable session row
    # is sufficient evidence that this run has started; retain the idle grace
    # period below so a just-created turn can still become active.
    if not root_seen_active: root_seen_active=True
    if child_active: root_idle_since=None; return
    if root_idle_since is None: root_idle_since=time.time(); return
    if time.time()-root_idle_since<5 or lessons_launch_attempts>=3: return
    prompt="Run the end-of-run retrospective now. Read project control/work/test/validation artifacts and prior lessons. Update .opencode-v2/LESSONS_LEARNED.md, write .opencode-v2/LESSONS_GLOBAL_CANDIDATES.md, then .opencode-v2/LESSONS.ready. Retrospective only; do not modify application code. Return LESSONS_READY."
    lessons_launch_attempts+=1
    ok,detail=http.start_lessons_session(prompt)
    if ok:
        lessons_started=True
        (ctrl/"LESSONS.launching").write_text(
            f"started={time.strftime('%Y-%m-%dT%H:%M:%S%z')}\n"
            f"root_session={root}\nlessons_session={detail}\n"
        )
        log(f"LESSONS_EXTERNAL_START root_session={root} lessons_session={detail}")
    else:
        log(f"LESSONS_EXTERNAL_START_FAILED root_session={root} attempt={lessons_launch_attempts} detail={detail}")

def api_poll_loop():
    while True:
        try:
            statuses=http.get_status()
            active=set(statuses)
            now=time.time()
            rows={}
            child_active=False

            for sid in list(active):
                info=http.get_session(sid)
                messages=http.get_messages(sid)
                shape=message_shape(messages,info)
                directory=shape["directory"]

                if PROJECT and directory and os.path.realpath(directory)!=os.path.realpath(PROJECT):
                    continue

                agent=shape["agent"]
                parent=shape["parent"]
                child_active=child_active or bool(parent)

                if agent in IMPLEMENTATION_AGENTS and parent:
                    enforce_assignment(sid,agent,shape["first_user"])

                did,attempt=session_task.get(sid,("",0))
                key=(shape["message_id"],shape["last_tool_id"])
                # Critical fail-open rule:
                # /session/active can expose a running child before its current
                # assistant message/parts become observable. Unknown is NOT
                # "no tool for 120s". Start/continue watchdog only when a live,
                # unfinished assistant message is actually visible.
                can_watch=(
                    bool(parent)
                    and shape.get("observable")
                    and not shape.get("assistant_completed")
                    and not shape["tool_running"]
                )

                age,st=watchdog_age(sid,key,can_watch)
                reasoning=shape["reasoning"]
                text_chars=shape["text"]

                if can_watch and st["aborted_key"]!=key:
                    if agent=="implementation-planner":
                        hs,hr,ht=300,20000,20000
                    else:
                        hs,hr,ht=HARD_SECONDS,HARD_REASONING_CHARS,HARD_TEXT_CHARS

                    reason=""
                    if reasoning>=hr:
                        reason=f"reasoning_chars={reasoning}"
                    elif text_chars>=ht:
                        reason=f"text_chars={text_chars}"
                    elif age>=hs:
                        reason=f"no_tool_age={int(age)}s"

                    if reason:
                        st["aborted_key"]=key
                        if abort_session(
                            sid,
                            f"runaway {reason} reasoning_chars={reasoning} text_chars={text_chars}",
                            agent,
                        ):
                            abort_count[sid]=abort_count.get(sid,0)+1

                rows[sid]={
                    "session":sid,
                    "agent":agent,
                    "parent":parent,
                    "deliverable":did,
                    "attempt":attempt,
                    "reasoning":reasoning,
                    "text":text_chars,
                    "tool_running":shape["tool_running"],
                    "context_input":shape["context_input"],
                    "observable":shape.get("observable",False),
                    "assistant_completed":shape.get("assistant_completed",False),
                    "seen":now,
                    "directory":directory or PROJECT or "",
                    "status":statuses.get(sid),
                }

            payload={
                "generated":now,
                "project":PROJECT or "",
                "source":"http-status+message-poll-v2.6.7b",
                "sessions":list(rows.values()),
            }
            tmp=LIVE_STATUS.with_suffix(".tmp")
            tmp.parent.mkdir(parents=True,exist_ok=True)
            tmp.write_text(json.dumps(payload,separators=(",",":")))
            os.replace(tmp,LIVE_STATUS)

            maybe_launch_lessons(active,child_active)
            merge_lesson_candidates()

        except Exception as e:
            log(f"HTTP_POLL_ERROR {e!r}")

        time.sleep(POLL)

def persisted_reconcile_loop():
    while not DB.exists(): time.sleep(0.5)
    while True:
        try:
            con=db_connect(); rows=con.execute("SELECT s.id,coalesce(s.agent,''),(SELECT count(*) FROM session_message m WHERE m.session_id=s.id AND m.type='compaction') FROM session_v2 s WHERE s.parent_id IS NOT NULL AND s.directory=? AND s.time_created>=?",(PROJECT,START_MS)).fetchall() if PROJECT else []; con.close()
            for sid,agent,comps in rows:
                if agent in IMPLEMENTATION_AGENTS and sid not in dispatch_seen:
                    did=parse_deliverable(first_user_text_db(sid)); leaves=(load_manifest().get("leaves") or {})
                    if re.fullmatch(r"D\d{3}",did or "") and did in leaves:
                        claim,n=claim_attempt(sid,did); dispatch_seen.add(sid)
                        if claim=="limit":
                            abort_session(sid,f"dispatch_guard attempt_limit deliverable={did} count={n}",agent)
                            csv("DISPATCH_DENY",sid,agent,f"{did} attempt_limit={n}")
                        else:
                            log(f"DISPATCH_RECONCILE session={sid} agent={agent} deliverable={did} attempt={n} claim={claim}")
                prev=compaction_seen.get(sid,0)
                if comps<=prev: continue
                compaction_seen[sid]=comps; did,_=session_task.get(sid,(parse_deliverable(first_user_text_db(sid)),0))
                if did and ready_info(did): log(f"COMPACTION_AFTER_DONE session={sid} agent={agent} deliverable={did} compactions={comps}"); continue
                if agent=="implementation-planner" and plan_ready(): log(f"COMPACTION_AFTER_DONE session={sid} agent={agent} control_ready=1"); continue
                if comps==1: log(f"COMPACTION_ALLOWED session={sid} agent={agent} count=1"); csv("COMPACTION_ALLOWED",sid,agent,"count=1")
                elif comps>=2: abort_session(sid,f"child_compaction count={comps}; second incomplete compaction",agent); log(f"RETIRED_COMPACTION session={sid} agent={agent} compactions={comps}")
        except Exception as e: log(f"PERSISTED_RECONCILE_ERROR {e!r}")
        time.sleep(0.5)

def main():
    ROOT.joinpath("logs").mkdir(parents=True,exist_ok=True); sync_global_lessons(); log(f"SUPERVISOR_START project={PROJECT!r} source=http-poll reason={HARD_REASONING_CHARS} text={HARD_TEXT_CHARS} first_compaction=allow second_compaction=retire")
    threading.Thread(target=control_guard_loop,daemon=True).start(); threading.Thread(target=persisted_reconcile_loop,daemon=True).start(); api_poll_loop()
if __name__=="__main__": main()
