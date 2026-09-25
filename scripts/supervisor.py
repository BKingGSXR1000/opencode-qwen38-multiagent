#!/usr/bin/env python3
import argparse,base64,contextlib,copy,hashlib,json,os,re,shlex,sqlite3,subprocess,sys,threading,time,traceback,urllib.error,urllib.parse,urllib.request
from pathlib import Path
from control_state import (phase_ready, ready_info as state_ready_info,
                           snapshot as state_snapshot, attempt_state,
                           load_attempts as state_load_attempts,
                           AUTOMATIC_ATTEMPT_LIMIT,
                           MAX_INFRASTRUCTURE_RETRY_GRANTS,
                           MAX_CONTEXT_DELIVERY_RETRY_GRANTS,
                           MAX_EXTERNAL_CONTRACT_RETRY_GRANTS,
                           MAX_UNMATERIALIZED_DISPATCH_REPLAYS,
                           MAX_OPERATOR_INFRASTRUCTURE_ABORTS,
                           RECURSIVE_SPLIT_PROTOCOL, MAX_SPLIT_DEPTH,
                           LEAF_READY_PROTOCOL, VERIFY_WAIT_PROTOCOL,
                           split_depth, valid_deliverable_id)
from leaf_contract import (
    IMPLEMENTATION_ROLES, READ_ONLY_ROLES,
    strict_owned_artifact_paths as shared_strict_owned_artifact_paths,
    canonical_owned_artifacts as shared_canonical_owned_artifacts,
    validate_leaf_contract, validate_verify_command,
)
from state_io import (
    StateCorruptionError, load_json_object, atomic_write_json, atomic_write_text,
    exclusive_file_lock,
)
from worker_sandbox import (
    SandboxError as WorkerSandboxError,
    cleanup_session as worker_sandbox_cleanup_session,
    run_verify_bash as worker_sandbox_run_verify_bash,
    commit_verify_outputs as worker_sandbox_commit_verify_outputs,
    session_used_sandbox as worker_session_used_sandbox,
    violation_path as worker_sandbox_violation_path,
    has_fatal_violation as worker_sandbox_has_fatal_violation,
    has_only_denied_preexecution_violations as
        worker_sandbox_has_only_denied_preexecution_violations,
    command_invokes_manual_sandbox_wrapper as
        worker_command_invokes_manual_sandbox_wrapper,
)
# V2.6.9 BATCH8 VERIFY-SANDBOX-LIFETIME-V3
from control_query_views import materialize_control_query_views
from deterministic_dispatch import select_actions as deterministic_select_actions
from watchdog_telemetry import (
    BackendTelemetrySampler, backend_phase, invisible_watchdog_decision,
    visible_watchdog_decision, visible_progress_marker,
)

ROOT=Path(os.environ.get("V2_ROOT", str(Path(__file__).resolve().parents[1])))
DB=Path(os.environ.get("V2_OPENCODE_DB", str(ROOT/"xdg"/"data"/"opencode"/"opencode.db")))
LOG=ROOT/"logs"/"supervisor-events.log"; CSV=ROOT/"logs"/"supervisor-events.csv"; LIVE_STATUS=ROOT/"logs"/"supervisor-live.json"
WATCHDOG_TELEMETRY=ROOT/"logs"/"watchdog-telemetry.jsonl"
PROJECT=os.environ.get("V2_PROJECT","")
START_MS=int(time.time()*1000)-5000; POLL=0.5
HARD_SECONDS=120; HARD_REASONING_CHARS=20000; HARD_TEXT_CHARS=12000
MAX_IMPLEMENTATION_PROMPT_CHARS=2500
PROBE_MAX_TOOL_TURNS_WITHOUT_DURABLE_PROGRESS=5
PROGRESS_HANDOFF_MAX_TOOL_TURNS_WITHOUT_DURABLE_PROGRESS=2
EARLY_WRITE_COMPLETED_TURNS_BY_COMPLEXITY={"S":4,"M":6}
MAX_REFERENCE_FOUNDATION_SESSIONS=3
MAX_REFERENCE_VALIDATION_SESSIONS=8
MAX_REFERENCE_STAGNANT_SESSIONS=2
MAX_REFERENCE_COMPACTIONS=1
MAX_IMPLEMENTATION_COMPACTIONS=3
PLANNER_CONTEXT_INPUT_CEILING=45000
# gametest2s showed three healthy setup/read sequences reaching the old 150s
# file-existence deadline (150.4-150.5s) without a first write.  The successful
# gametest2q planner did not begin its first write until 352.504s and did not
# complete it until 391.903s. The bootstrap scaffold makes the phase restartable
# immediately; this bounds *absence of model-created durable changes*, not file
# existence, while leaving room for that observed Qwen latency.
PLANNER_INITIAL_PROGRESS_GRACE_SECONDS=300
PLANNER_PROGRESS_STALL_SECONDS=120
ROOT_CONTEXT_INPUT_CEILING=43000
MAX_ROOT_RESTARTS=4
MAX_PLANNER_RESTARTS=3
# Ledger schema identifier, deliberately independent of the harness release.
# Existing historical `V2.6.7` ledgers remain readable; newly created ledgers
# use this unambiguous schema name without a destructive migration.
ATTEMPT_LEDGER_PROTOCOL="v2-attempt-ledger-v1"
SPLIT_PROPOSAL_PROTOCOL="v2-task-split-proposal-v2"
SPLIT_PARENT_CONTRACT_INVALID_PROTOCOL="v2-split-parent-contract-invalid-v1"
VERIFY_EVIDENCE_PROTOCOL="v2-supervisor-verify-evidence-v1"
IMPLEMENTATION_AGENTS=set(IMPLEMENTATION_ROLES)
READ_ONLY_SPLIT_ROLES=set(READ_ONLY_ROLES)
MAX_CONCURRENT_IMPLEMENTATION_WORKERS=3
MAX_SPLITTER_ATTEMPTS=2
MAX_SPLITTER_OUTPUT_LIMIT_RECOVERIES=1
MAX_SPLITTER_PROFILE_RECOVERIES=1
MAX_SPLITTER_EXECUTION_CONTRACT_RECOVERIES=1
SPLITTER_LEASE_SECONDS=600
# execute.after can run before the child final text is durable in the session DB.
# Give the persisted reconcile loop a short bounded window after the hook returns.
SPLITTER_COMPLETION_PERSIST_GRACE_SECONDS=15
MAX_SPLIT_PARENT_FINALIZE_FAILURES=3
SPLIT_TRANSACTION_PROTOCOL="v2-split-transaction-v1"
SPLIT_HANDOFF_VERIFY_SENTINEL="SUPERVISOR_HANDOFF_PROGRESS"
SPLIT_HANDOFF_MARKER="HANDOFF_READY: true"
RUN_CHECKS_COMMAND=".opencode-v2/bin/run-checks"
ROOT_SESSION_PROTOCOL="v2-root-session-v1"
ROOT_CONTINUATION_BLOCK_PROTOCOL="v2-root-continuation-block-v1"
MAX_SPLIT_HANDOFF_SCOPE_CHARS=1200
SPLIT_HANDOFF_STAGE_RE=re.compile(
    r"(?:\([a-z]\)|\(\d+\)|\b(?:first|second|third|then|followed\s+by)\b)",
    re.I,
)
lock=threading.RLock(); dispatch_lock=threading.RLock(); watch={}; dispatch_seen=set(); session_task={}; abort_count={}; compaction_seen={}; supervisor_abort_reasons={}
event_watch={}; event_threads={}; planner_checkpoints={}; planner_completion_seen=set(); post_finalize_seen=set(); worker_progress={}; abort_intent_lock=threading.RLock(); planner_restart_lock=threading.RLock()
state_blocker_exempt_active_seen=set()
root_seen_active=False; root_idle_since=None; lessons_started=False; lessons_launch_attempts=0
verify_wait_log_state={}
backend_sampler=BackendTelemetrySampler(ROOT)
watchdog_telemetry_last={}
deterministic_shadow_last_state_version=""

ROOT_CONTINUATION_PROMPT="""Continue orchestration for this project.

FIRST read .opencode-v2/ORIGINAL_TASK.md.
That file is the immutable authoritative ORIGINAL USER REQUEST.
Never treat this continuation message as the original user goal.

Then read:
- .opencode-v2/CONTROL_CONTRACT.md
- .opencode-v2/ACCEPTANCE.md if present
- .opencode-v2/IMPLEMENTATION_PLAN.md if present

If ORIGINAL_TASK.md is missing or empty:
ORIGINAL_TASK_MISSING
STOP.

If an existing acceptance/plan clearly describes this continuation protocol
instead of the task in ORIGINAL_TASK.md:
ORIGINAL_TASK_STATE_MISMATCH
STOP.

Direct-read `.opencode-v2/query/decision.json` for the authoritative bounded
scheduler projection. The supervisor materializes it from the same canonical
state as control-status.json. Do not direct-read full control-status.json during
normal continuation.
Continue the ORIGINAL task from durable state only."""

PLANNER_CONTINUATION_PROMPT="""Continue structured implementation planning for this project.

FIRST read .opencode-v2/ORIGINAL_TASK.md.
It is the immutable authoritative original user request.
Never substitute this continuation message for the original task.

Then read:
- .opencode-v2/ACCEPTANCE.md
- .opencode-v2/CONTROL_CONTRACT.md
- .opencode-v2/IMPLEMENTATION_PLAN.structured.json
- .opencode-v2/IMPLEMENTATION_PLAN.repair.json if present

Edit only the structured source. Never edit generated IMPLEMENTATION_PLAN.md.
Continue from durable structured state using your planner protocol."""

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

def session_table_name():
    name=str(os.environ.get("V2_OPENCODE_SESSION_TABLE") or "session_v2")
    if name not in {"session","session_v2"}:
        raise RuntimeError(f"unsupported OpenCode session table: {name}")
    return name

def stage_a_transport_mode():
    # Isolated v1.18.31 transport mode: technical root, external scheduler.
    return (
        session_table_name()=="session"
        and bool(str(os.environ.get("V2_OPENCODE_BASE_URL") or "").strip())
    )


def root_agent_name():
    # Beta keeps the semantic orchestrator; v1 Stage A uses a technical root.
    return "transport-root" if stage_a_transport_mode() else "orchestrator"

def v1_session_status_snapshot(strict=False):
    # OpenCode v1.18.31 keeps live busy/retry/idle state in the session status API,
    # not in a persisted session.time_idle column.
    base=str(os.environ.get('V2_OPENCODE_BASE_URL') or '').rstrip('/')
    if not base:
        if strict:
            raise RuntimeError('V2_OPENCODE_BASE_URL is not configured')
        return {}
    query=urllib.parse.urlencode({'directory':PROJECT}) if PROJECT else ''
    url=base+'/session/status'+(('?'+query) if query else '')
    try:
        req=urllib.request.Request(url,headers={'Accept':'application/json'})
        with urllib.request.urlopen(req,timeout=2.0) as resp:
            raw=resp.read()
        data=json.loads(raw or b'{}')
        if isinstance(data,dict) and set(data)=={'data'}:
            data=data['data']
        if not isinstance(data,dict):
            raise RuntimeError('session status response is not an object')
        return data
    except Exception as exc:
        if strict:
            raise RuntimeError(f'v1 session status unavailable: {exc}') from exc
        return {}


def v1_runtime_enabled():
    return str(os.environ.get('V2_OPENCODE_SESSION_TABLE') or '') == 'session'


ROOT_READ_GUARDED_PHASES={"execution","recursive-split","execution-blocked"}
ROOT_READ_LEAF_QUERY_RE=re.compile(
    r"^\.opencode-v2/query/leaves/D\d{3}(?:-[AB](?:[12])?)?\.json$"
)


def root_read_session_identity(sid):
    # Return whether SID is this project's primary orchestrator root session.
    if not sid:
        return False,"missing-session"
    try:
        con=db_connect()
        table=session_table_name()
        row=con.execute(
            f"SELECT parent_id,coalesce(agent,''),coalesce(directory,'') "
            f"FROM {table} WHERE id=?",
            (sid,),
        ).fetchone()
        con.close()
    except Exception as exc:
        # Efficiency guard: don't break child reads on transient DB failure.
        return False,f"session-db-unavailable:{type(exc).__name__}"
    if not row:
        return False,"session-missing"
    parent_id,agent,directory=row
    if parent_id is not None:
        return False,"child-session"
    expected=root_agent_name()
    if agent!=expected:
        return False,f"non-root-agent:{agent or 'unknown'} expected={expected}"
    try:
        if PROJECT and Path(directory).resolve()!=Path(PROJECT).resolve():
            return False,"different-project"
    except Exception:
        return False,"invalid-session-directory"
    return True,f"root-{expected}"


def root_read_relative_path(project,raw_path):
    # Canonicalize one OpenCode read path against the exact project root.
    raw=str(raw_path or "").strip()
    if not raw:
        return "","missing-filePath"
    try:
        root=Path(project).resolve()
        candidate=Path(raw)
        if not candidate.is_absolute():
            candidate=root/candidate
        candidate=candidate.resolve()
        rel=candidate.relative_to(root).as_posix()
    except Exception:
        return "","outside-project"
    return rel,"canonical"


def root_read_tool_path(tool_args):
    # OpenCode2 beta-19242 exposes read input as `path`; newer source uses
    # `filePath`. Accept either spelling, but fail closed on ambiguity.
    args=tool_args if isinstance(tool_args,dict) else {}
    path_value=args.get("path")
    file_value=args.get("filePath")

    for key,value in (("path",path_value),("filePath",file_value)):
        if value is not None and not isinstance(value,str):
            return "",f"invalid-{key}-type:{type(value).__name__}"

    path_value=(path_value or "").strip()
    file_value=(file_value or "").strip()

    if path_value and file_value and path_value!=file_value:
        return "","ambiguous-path-keys"
    if path_value:
        return path_value,"path"
    if file_value:
        return file_value,"filePath"
    return "","missing-path-argument"


def root_read_path_policy(project,phase,tool_args):
    # Bound root reads during implementation-control phases.
    if str(phase or "") not in ROOT_READ_GUARDED_PHASES:
        return "na",f"phase={phase or 'unknown'}"
    raw_path,source=root_read_tool_path(tool_args)
    if not raw_path:
        return "deny",source
    rel,detail=root_read_relative_path(project,raw_path)
    if not rel:
        return "deny",detail
    if rel==".opencode-v2/query/decision.json":
        return "allow",f"{rel} arg={source}"
    if ROOT_READ_LEAF_QUERY_RE.fullmatch(rel):
        return "allow",f"{rel} arg={source}"
    return "deny",f"path-not-allowed:{rel} arg={source}"


def root_control_read_state(sid,tool_args):
    # Apply the read firewall only to the primary root orchestrator.
    is_root,identity=root_read_session_identity(sid)
    if not is_root:
        return "na",identity
    snapshot=normalized_state_snapshot(PROJECT)
    phase=str(snapshot.get("resume_phase") or "") if isinstance(snapshot,dict) else ""
    state,detail=root_read_path_policy(PROJECT,phase,tool_args)
    return state,f"phase={phase or 'unknown'} {detail}"


def _v1_message_records(sid):
    con=db_connect()
    try:
        rows=con.execute(
            "SELECT id,time_created,time_updated,data FROM message "
            "WHERE session_id=? ORDER BY time_created,id",
            (sid,),
        ).fetchall()
    finally:
        con.close()
    out=[]
    for mid,created,updated,raw in rows:
        try:
            data=json.loads(raw) if raw else {}
        except Exception:
            continue
        if not isinstance(data,dict):
            continue
        out.append({
            "id":str(mid),
            "created":int(created or 0),
            "updated":int(updated or created or 0),
            "data":data,
        })
    return out

def _v1_active_session_ids(strict=False):
    supplied=v1_active_session_ids_from_env(strict=strict)
    if supplied is not None:
        return supplied
    status=v1_session_status_snapshot(strict=strict)
    return {
        sid for sid,info in status.items()
        if isinstance(info,dict)
        and str(info.get("type") or "") in {"busy","retry"}
    }

def _v1_latest_assistant_record(sid):
    for record in reversed(_v1_message_records(sid)):
        if record["data"].get("role")=="assistant":
            return record
    return None

def _v1_session_terminal(sid,active_ids=None):
    active_ids=_v1_active_session_ids(strict=False) if active_ids is None else set(active_ids)
    if sid in active_ids:
        return False
    record=_v1_latest_assistant_record(sid)
    if not record:
        return False
    data=record["data"]
    tm=data.get("time") if isinstance(data.get("time"),dict) else {}
    return bool(
        tm.get("completed")
        or data.get("finish")
        or isinstance(data.get("error"),dict)
    )

def _v1_session_end_ms(sid):
    values=[]
    try:
        con=db_connect()
        row=con.execute(
            "SELECT time_created,time_updated FROM session WHERE id=?",
            (sid,),
        ).fetchone()
        if row:
            values.extend(int(x or 0) for x in row)
        for table in ("message","part"):
            row=con.execute(
                f"SELECT COALESCE(MAX(time_updated),0),COALESCE(MAX(time_created),0) "
                f"FROM {table} WHERE session_id=?",
                (sid,),
            ).fetchone()
            if row:
                values.extend(int(x or 0) for x in row)
        con.close()
    except Exception:
        pass
    try:
        for record in _v1_message_records(sid):
            data=record["data"]
            tm=data.get("time") if isinstance(data.get("time"),dict) else {}
            values.append(int(tm.get("completed") or 0))
    except Exception:
        pass
    return max(values or [0])

def _v1_compaction_count(sid):
    count=0
    try:
        con=db_connect()
        rows=con.execute(
            "SELECT data FROM part WHERE session_id=? ORDER BY time_created,id",
            (sid,),
        ).fetchall()
        con.close()
        for (raw,) in rows:
            try:
                data=json.loads(raw) if raw else {}
            except Exception:
                continue
            if isinstance(data,dict) and data.get("type")=="compaction":
                count+=1
    except Exception:
        return 0
    return count

def _v1_latest_compaction_state(sid):
    records=_v1_message_records(sid)
    compactions=[]
    for record in records:
        if record["data"].get("role")!="user":
            continue
        try:
            parts=_v1_message_parts(record["id"])
        except Exception:
            continue
        if any(part.get("type")=="compaction" for part in parts):
            compactions.append(record)
    if not compactions:
        return {"seq":0,"status":"","error_type":""}
    latest=compactions[-1]
    seq=len(compactions)
    assistants=[
        record for record in records
        if record["data"].get("role")=="assistant"
        and record["data"].get("parentID")==latest["id"]
        and str(record["data"].get("mode") or "")=="compaction"
    ]
    if not assistants:
        return {"seq":seq,"status":"running","error_type":""}
    data=assistants[-1]["data"]
    error=data.get("error") if isinstance(data.get("error"),dict) else {}
    if error:
        return {"seq":seq,"status":"failed","error_type":"compaction.failed"}
    tm=data.get("time") if isinstance(data.get("time"),dict) else {}
    if tm.get("completed") or data.get("finish"):
        return {"seq":seq,"status":"completed","error_type":""}
    return {"seq":seq,"status":"running","error_type":""}

def _v1_message_rows(sid):
    con=db_connect()
    try:
        rows=con.execute(
            "SELECT id,time_created,data FROM message "
            "WHERE session_id=? ORDER BY time_created,id",
            (sid,),
        ).fetchall()
    finally:
        con.close()
    out=[]
    for mid,created,raw in rows:
        try:
            data=json.loads(raw) if raw else {}
        except Exception:
            continue
        if not isinstance(data,dict):
            continue
        out.append((str(mid),str(data.get("role") or ""),int(created or 0)))
    return out

def _v1_message_parts(mid):
    con=db_connect()
    try:
        rows=con.execute(
            "SELECT data FROM part WHERE message_id=? ORDER BY time_created,id",
            (mid,),
        ).fetchall()
    finally:
        con.close()
    out=[]
    for (raw,) in rows:
        try:
            data=json.loads(raw) if raw else {}
        except Exception:
            continue
        if isinstance(data,dict):
            out.append(data)
    return out

def _v1_first_user_text(sid):
    for mid,role,_created in _v1_message_rows(sid):
        if role!="user":
            continue
        texts=[
            str(part.get("text") or "")
            for part in _v1_message_parts(mid)
            if part.get("type")=="text" and isinstance(part.get("text"),str)
        ]
        return "\n".join(x for x in texts if x)
    return ""

def first_user_text_db(sid):
    if v1_runtime_enabled():
        try:
            return _v1_first_user_text(sid)
        except Exception:
            return ""
    try:
        con=db_connect()
        row=con.execute(
            "SELECT data FROM session_message "
            "WHERE session_id=? AND type='user' ORDER BY seq LIMIT 1",
            (sid,),
        ).fetchone()
        con.close()
        if not row:
            return ""
        d=json.loads(row[0])
        return d.get("text","") if isinstance(d,dict) else ""
    except Exception:
        return ""

def last_assistant_text_db(sid):
    """Return concatenated text parts from the latest assistant message."""
    if v1_runtime_enabled():
        try:
            for record in reversed(_v1_message_records(sid)):
                if record["data"].get("role")!="assistant":
                    continue
                texts=[
                    str(part.get("text") or "")
                    for part in _v1_message_parts(record["id"])
                    if part.get("type")=="text"
                    and isinstance(part.get("text"),str)
                ]
                text="\n".join(x for x in texts if x).strip()
                if text:
                    return text
        except Exception:
            pass
        return ""
    try:
        con=db_connect()
        rows=con.execute(
            "SELECT data FROM session_message "
            "WHERE session_id=? AND type='assistant' ORDER BY seq DESC",
            (sid,),
        ).fetchall()
        con.close()
        for (raw,) in rows:
            try:
                d=json.loads(raw)
            except Exception:
                continue
            parts=d.get("content") if isinstance(d,dict) else None
            if not isinstance(parts,list):
                continue
            texts=[
                p.get("text","") for p in parts
                if isinstance(p,dict)
                and p.get("type")=="text"
                and isinstance(p.get("text"),str)
            ]
            text="\n".join(x for x in texts if x).strip()
            if text:
                return text
    except Exception:
        pass
    return ""


def parse_split_parent(text):
    m=re.search(
        r"(?m)^\s*SPLIT_PARENT:\s*(D\d{3}(?:-[AB](?:[12])?)?)(?:\r?$|\r?\n)",
        str(text or ""),
    )
    return m.group(1) if m else ""


def parse_deliverable(text):
    if not text: return ""
    m=re.search(r"(?mi)^\s*DELIVERABLE\s*:\s*(D\d{3}(?:-[AB](?:[12])?)?)\s*$",text)
    return m.group(1) if m else ""

def implementation_prompt(did):
    return (
        f"DELIVERABLE: {did}\n"
        f"Read .opencode-v2/query/leaves/{did}-context.json exactly once for this session; "
        "it is the complete authoritative deliverable contract. Do not read "
        ".opencode-v2/IMPLEMENTATION_PLAN.md or a separate split scope. "
        "If supervisor_execution_correction is non-empty, it overrides conflicting "
        "execution details in outcome/current_progress/split_scope but never ownership, "
        "Verify, dependencies, Done-when, or Acceptance.\n"
        f"Read .opencode-v2/work/{did}.progress.md if present.\n"
        "Inspect your owned project artifacts as they currently exist.\n"
        "Continue from actual filesystem state and execute the deliverable."
    )

def early_write_completed_turn_limit(leaf):
    complexity=str(leaf.get("complexity") or "S").strip().upper() if isinstance(leaf,dict) else "S"
    return int(EARLY_WRITE_COMPLETED_TURNS_BY_COMPLEXITY.get(complexity,4))


def _ordinal_word(value):
    return {4:"FOURTH",6:"SIXTH"}.get(int(value),f"{int(value)}TH")


def _next_ordinal_word(value):
    return {4:"fifth",6:"seventh"}.get(int(value),f"{int(value)+1}th")


def verify_reporting_rule():
    return (
        "\n\nEXACT VERIFY REPORTING:\n"
        "Only report or write `exact Verify passed` when you ran the context packet's "
        "verify_command UNCHANGED and that exact command exited 0. Any modified, "
        "equivalent, diagnostic, or smoke check must be labeled `noncanonical check`; "
        "never claim it proves the canonical Verify. Run verification commands directly; "
        "do not create helper scripts, temporary files, or wrapper files outside your owned "
        "artifacts. Supervisor Verify evidence is authoritative."
    )


def implementation_runtime_prompt(did,agent):
    """Deterministically enrich the canonical child-visible prompt."""
    base=implementation_prompt(did)
    leaf=(load_manifest().get("leaves") or {}).get(did,{})
    handoff_only=bool(isinstance(leaf,dict) and leaf.get("split_handoff_only"))

    if agent=="probe-builder" and handoff_only:
        lines=base.splitlines()
        action_order=(
            "MANDATORY ACTION ORDER — PROGRESS-ONLY HANDOFF:\n"
            "1. FIRST tool-bearing response: read ONLY the authoritative context packet "
            f".opencode-v2/query/leaves/{did}-context.json exactly once for this session. "
            "Do NOT read the progress file separately: the packet field current_progress "
            "contains its current contents when short, or both its opening contract and latest "
            "conclusions when long; it is empty when none exists. Do not "
            "inspect CONTROL_CONTRACT or project artifacts yet.\n"
            "2. SECOND tool-bearing response: write/edit exactly "
            f".opencode-v2/work/{did}.progress.md with this plain-text shape:\n"
            "HANDOFF_READY: false\n\n"
            "Findings:\n<facts or none yet>\n\n"
            "Evidence:\n<evidence or none yet>\n\n"
            "Next step:\n<next bounded action>\n"
            "Labels start at column 1. Do not prefix them with #/## and do not append text "
            "after true/false. Preserve useful current_progress facts. Use true only when "
            "the downstream writer has enough evidence. A bounded inspection that establishes "
            "a required source artifact is absent, empty, or otherwise unusable is conclusive "
            "evidence: record the concrete paths/sizes, the missing requirement, and the exact "
            "writer delta, then set HANDOFF_READY: true. Do not loop searching for unavailable "
            "evidence.\n"
            "3. AFTER every valid checkpoint, exactly ONE discovery tool-bearing response "
            "is allowed. The NEXT tool-bearing response MUST update the progress file. A "
            "mechanical guard denies a second consecutive discovery with "
            "PROGRESS_CHECKPOINT_REQUIRED but keeps the session alive so you can checkpoint. "
            "A failed query is evidence and must be checkpointed.\n"
            "When sufficient, write exact HANDOFF_READY: true, run the exact Verify command, "
            "persist the result, and return. Temporary files do not count."
        )
        return "\n".join(lines[:2])+"\n\n"+action_order+"\n\n"+"\n".join(lines[3:])+verify_reporting_rule()

    owned=owned_artifact_paths(leaf) if isinstance(leaf,dict) else []

    if agent in IMPLEMENTATION_AGENTS and owned:
        implementer_direct_write=(
            "\n\nIMPLEMENTER DIRECT-WRITE ORDER — EXACT:\n"
            "1. Read the authoritative context packet exactly once. If a named owned artifact "
            "already exists, inspect only that artifact; do not explore the plan, repository, "
            "or unrelated runtime state before making progress.\n"
            "2. Your NEXT tool-bearing response MUST write or edit an owned project artifact. "
            "Start with a SMALL, parseable, contract-shaped artifact rather than trying to emit "
            "the full implementation in one large tool call. Keep the first write concise "
            "(prefer a minimal executable/exportable skeleton with no long prose/comments), "
            "then extend it with bounded edits after the write succeeds. This avoids malformed "
            "tool JSON from oversized content. Missing evidence is a value to record or validate "
            "after the artifact exists, not permission for more unbounded discovery.\n"
            "3. After the first owned-artifact change, inspect only direct dependencies needed "
            "to complete it, run the exact Verify command, and repair only owned artifacts.\n"
            "4. Call the bash tool with ONLY the intended shell command. Never invoke "
            "worker_sandbox.py, run-bash, bubblewrap, or any sandbox wrapper yourself; "
            "the runtime wraps bash automatically.\n"
            "The Early Write Gate below is a ceiling, not a target."
        )
        base=base+implementer_direct_write

    if agent=="probe-builder" and owned:
        direct_write=(
            "\n\nPROBE DIRECT-WRITE ORDER — EXACT:\n"
            "1. Context/progress reads are inspection turn 1.\n"
            "2. Use at most ONE more tool-bearing response for measurements required by Done-when/Verify. "
            "No general root/.opencode-v2/cache/venv exploration.\n"
            "3. The NEXT tool-bearing response MUST use write or edit on the primary owned artifact. "
            "For JSON include every Done-when key; a missing component/path is a measured false/not-present "
            "result, not a reason for more discovery.\n"
            "4. Bash measures only; never use it to create the primary artifact. Any shell/tool/optional-import "
            "error means persist known facts next instead of escalating the shell command.\n"
            "5. After the artifact exists, run exact Verify and repair only the owned artifact with write/edit.\n"
            "The Early Write Gate below is a ceiling, not a target."
        )
        base=base+direct_write

    if agent not in READ_ONLY_SPLIT_ROLES and owned:
        deadline=early_write_completed_turn_limit(leaf)
        gate=(
            "\n\nEARLY WRITE GATE — EXACT:\n"
            f"Use the first {deadline} completed tool-bearing turns for bounded inspection. "
            f"If no owned artifact differs at the end of turn {deadline}, the NEXT tool-bearing "
            "response is WRITE-ONLY: it MUST create or update an owned project artifact. "
            "No additional read/search/web/discovery call is allowed in that state. The only "
            "alternative is one exact progress-file write when the owned artifact is already "
            "correct and you are finalizing the handoff. A non-writing tool in WRITE-ONLY "
            "state causes deterministic session retirement."
        )
        return base+gate+verify_reporting_rule()

    return base+verify_reporting_rule()

def recursive_split_enabled():
    return load_manifest().get("recursive_split_protocol") == RECURSIVE_SPLIT_PROTOCOL

def leaf_automatic_limit(did):
    """Two real attempts lead to a split except for minimum-unit handoffs.

    Progress-only handoff children have no owned artifact that can be
    partitioned again. Treat them like terminal leaves: three genuine attempts
    are available, but they never manufacture a futile recursive split edge.
    """
    if recursive_split_enabled() and split_depth(did) >= 0:
        leaf=(load_manifest().get("leaves") or {}).get(did,{})
        if isinstance(leaf,dict) and leaf.get("split_handoff_only"):
            return AUTOMATIC_ATTEMPT_LIMIT
        return 3 if split_depth(did) >= MAX_SPLIT_DEPTH else 2
    return AUTOMATIC_ATTEMPT_LIMIT

def leaf_children(did):
    leaf=(load_manifest().get("leaves") or {}).get(did,{})
    children=leaf.get("split_children",[]) if isinstance(leaf,dict) else []
    return children if isinstance(children,list) else []

def executable_leaf(did):
    return valid_deliverable_id(did) and not leaf_children(did)

def strip_subagent_prefix(text):
    """Remove only the beta's deterministic wrapper before the user prompt."""
    return re.sub(r"\AYou are a subagent spawned by another session\.\s*", "", text or "", count=1)

def normalize_implementation_prompt(text):
    """Strip only OpenCode's deterministic subagent wrapper.

    The worker must receive its exact canonical deliverable ID in every line.
    Literal Dxxx placeholders are no longer normalized/accepted because they
    can make a child lose task identity after reading a multi-Dxxx plan.
    """
    return strip_subagent_prefix(text).strip()
def implementation_prompt_violation(text):
    """Return a dispatch-protocol violation for an implementation prompt."""
    if len(text)>MAX_IMPLEMENTATION_PROMPT_CHARS:
        return f"oversized_first_user_prompt chars={len(text)} max={MAX_IMPLEMENTATION_PROMPT_CHARS}"
    text=normalize_implementation_prompt(text)
    did=parse_deliverable(text)
    if not did:
        return "missing_exact_DELIVERABLE_Dxxx"
    if text!=implementation_prompt(did):
        return "noncanonical_or_model_derived_handoff"
    return ""

def implementation_runtime_prompt_violation(agent,text):
    # Validate deterministic post-preclaim prompt seen by a materialized child.
    text=normalize_implementation_prompt(text)
    did=parse_deliverable(text)
    if not did:
        return "missing_exact_DELIVERABLE_Dxxx"
    # The preclaim already bounds the canonical controller prompt.  OpenCode
    # then appends this role's deterministic direct-write/gate protocol before
    # persisting the native child's first user message, so the exact runtime
    # form may legitimately exceed that transport cap.  Accept only the exact
    # derived runtime form; an oversized noncanonical form remains denied.
    if text==implementation_runtime_prompt(did,agent):
        return ""
    if len(text)>MAX_IMPLEMENTATION_PROMPT_CHARS:
        return f"oversized_first_user_prompt chars={len(text)} max={MAX_IMPLEMENTATION_PROMPT_CHARS}"
    return "noncanonical_runtime_handoff"

def split_leaf_overlay_path():
    return Path(PROJECT)/".opencode-v2"/"work"/"split-leaves.json"

def load_split_leaf_overlay():
    default={"owner":"supervisor","protocol":"v2-split-leaf-overlay-v1","parents":{}}
    if not PROJECT:
        return default
    data=load_json_object(
        split_leaf_overlay_path(),default_missing=default,label="split leaf overlay"
    )
    if data.get("owner") not in (None,"supervisor"):
        raise StateCorruptionError("split leaf overlay owner is invalid")
    parents=data.get("parents",{})
    if not isinstance(parents,dict):
        raise StateCorruptionError("split leaf overlay parents must be an object")
    data.setdefault("owner","supervisor")
    data.setdefault("protocol","v2-split-leaf-overlay-v1")
    data["parents"]=parents
    return data

def save_split_leaf_overlay(data):
    atomic_write_json(split_leaf_overlay_path(),data)

def apply_split_leaf_overlay(manifest):
    if not isinstance(manifest,dict):
        manifest={}
    leaves=manifest.setdefault("leaves",{})
    overlay=load_split_leaf_overlay()
    parents=overlay.get("parents") if isinstance(overlay.get("parents"),dict) else {}
    for parent,entry in parents.items():
        if not isinstance(entry,dict):
            continue
        child_defs=entry.get("child_defs") if isinstance(entry.get("child_defs"),dict) else {}
        children=entry.get("children") if isinstance(entry.get("children"),list) else list(child_defs)
        if parent in leaves and isinstance(leaves[parent],dict):
            leaves[parent]["split_children"]=list(children)
            leaves[parent]["split_depth"]=split_depth(parent)
        for did,child in child_defs.items():
            if isinstance(child,dict):
                leaves[did]=child
    return manifest

def load_manifest():
    if not PROJECT:
        return {}
    manifest=load_json_object(
        Path(PROJECT)/".opencode-v2"/"IMPLEMENTATION_PLAN.guard.json",
        default_missing={},
        label="implementation manifest",
    )
    return apply_split_leaf_overlay(manifest)

def save_manifest(manifest):
    atomic_write_json(
        Path(PROJECT)/".opencode-v2"/"IMPLEMENTATION_PLAN.guard.json",manifest
    )

def split_request_path(did):
    return Path(PROJECT)/".opencode-v2"/"work"/f"{did}.split-request.json"

def split_proposal_path(did):
    return Path(PROJECT)/".opencode-v2"/"work"/f"{did}.split-proposal.json"

def split_status_path(did):
    return Path(PROJECT)/".opencode-v2"/"work"/f"{did}.split-status.json"

def split_transaction_path(did):
    return Path(PROJECT)/".opencode-v2"/"work"/f"{did}.split-transaction.json"

def splitter_lock_path(did):
    return Path(PROJECT)/".opencode-v2"/"work"/f"{did}.splitter.lock"

@contextlib.contextmanager
def splitter_state_lock(did):
    with exclusive_file_lock(splitter_lock_path(did),timeout=8.0):
        yield

def load_split_status(did):
    return load_json_object(
        split_status_path(did),default_missing={},label=f"split status {did}"
    )


def save_split_status(did, state, **detail):
    """Persist a finite supervisor-owned split transition without losing counters."""
    previous=load_split_status(did)
    keep={}
    for key in (
        "claim_count","proposal_failures","parent_finalize_failures",
        "children","generation","transaction_id","recovery_claim_budget",
        "recovery_history","profile_recovery_fingerprints",
        "execution_contract_recovery_fingerprints",
        "direct_context_recovery_fingerprints",
        "false_parent_contract_repair_recovery_fingerprints",
        # A corrective turn is a bounded continuation of one already-claimed
        # splitter session.  Keep its audit fields through every terminal
        # transition; they are evidence, not a new recovery budget.
        "completion_pending_session","completion_pending_token",
        "completion_pending_deadline_epoch",
        "corrective_turn_count","corrective_session","corrective_dispatch_token",
        "corrective_first_output_sha256","corrective_archive",
        "corrective_context_sha256","corrective_dispatch_state",
        "corrective_execution_id","corrective_root_session",
        "corrective_reason_sha256","corrective_primary_response",
        "historical_corrective_recovery","deterministic_splitter_fallback",
    ):
        if key in previous:
            keep[key]=previous[key]
    generation=detail.pop("generation", keep.get("generation",1) or 1)
    payload={
        "owner":"supervisor",
        "parent_id":did,
        "state":state,
        "timestamp":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime()),
        **keep,
        # An explicit transition generation must override the prior preserved
        # generation.  The old field order silently kept the stale value.
        "generation":generation,
        **detail,
    }
    atomic_write_json(split_status_path(did),payload)
    return payload

def split_history_path():
    return Path(PROJECT)/".opencode-v2"/"work"/"splits.json"

def expected_children(did):
    depth=split_depth(did)
    if depth==0: return [f"{did}-A",f"{did}-B"]
    if depth==1: return [f"{did}1",f"{did}2"]
    return []

def _artifact_items(raw):
    paths,error=_strict_owned_artifact_text(raw)
    return [] if error else paths


def _split_failure_is_verification_related(reason):
    reason=str(reason or "")
    return reason.startswith((
        "verify-failed-",
        "verification-error-",
        "verify-command-unsafe:",
        "verify-mutated-owned-artifacts:",
    ))


def _split_request_verification_recovery_allowed(request):
    failures=request.get("failed_attempts",[]) if isinstance(request,dict) else []
    return any(
        isinstance(item,dict)
        and item.get("classification")=="genuine"
        and _split_failure_is_verification_related(item.get("reason"))
        for item in failures
    )


def _normalized_verify_for_recovery_compare(command):
    # Whitespace-only edits must not bypass the failed-command equality check.
    # Shell safety is still enforced separately by validate_verify_command().
    return re.sub(r"\s+"," ",str(command or "").strip())


def split_handoff_progress_checkpoint(text):
    """Recognize one durable progress-only checkpoint, READY or explicitly partial.

    The runtime prompt requires this checkpoint in the current worker session
    before bounded discovery. HANDOFF_READY:false is valid durable progress but
    is not sufficient for final leaf verification.
    """
    text=str(text or "")
    if len(text.strip()) < 80:
        return False
    lines=[line.strip() for line in text.splitlines()]
    markers=[line for line in lines if line.startswith("HANDOFF_READY:")]
    if markers not in (["HANDOFF_READY: false"],[SPLIT_HANDOFF_MARKER]):
        return False
    return all(label in lines for label in ("Findings:","Evidence:","Next step:"))


def split_handoff_progress_complete(text):
    """Require one exact READY marker line plus exact section-label lines."""
    if not split_handoff_progress_checkpoint(text):
        return False
    lines=[line.strip() for line in str(text or "").splitlines()]
    markers=[line for line in lines if line.startswith("HANDOFF_READY:")]
    return markers == [SPLIT_HANDOFF_MARKER]


def split_handoff_verify_command(child_id):
    path=f".opencode-v2/work/{child_id}.progress.md"
    # Keep the shell surface tiny. The child ID is supervisor-derived and
    # valid_deliverable_id-constrained; the quoted Python payload is data to
    # the outer shell and is rechecked by validate_verify_command().
    return (
        "python3 -c \"from pathlib import Path; "
        f"t=Path('{path}').read_text(); "
        "lines=[x.strip() for x in t.splitlines()]; "
        "markers=[x for x in lines if x.startswith('HANDOFF_READY:')]; "
        "assert markers==['HANDOFF_READY: true']; "
        "assert all(x in lines for x in ('Findings:','Evidence:','Next step:')); "
        "assert len(t.strip()) >= 80\""
    )


def _split_artifact_inventory(paths):
    root=Path(PROJECT)
    result={}
    for rel in paths:
        path=root/rel
        kind="missing"
        if path.is_file(): kind="file"
        elif path.is_dir(): kind="directory"
        elif path.exists(): kind="other"
        result[rel]={"exists":path.exists(),"kind":kind}
    return result


def _canonical_split_path_list(raw, field):
    if not isinstance(raw,list):
        raise ValueError(f"{field} must be a JSON array")
    result=[]; seen=set()
    for item in raw:
        if not isinstance(item,str) or not item.strip():
            raise ValueError(f"{field} entries must be non-empty strings")
        value=item.strip().replace("\\","/")
        path=Path(value)
        if path.is_absolute() or ".." in path.parts or value.startswith("./"):
            raise ValueError(f"{field} contains non-canonical path: {item}")
        canonical=path.as_posix()
        if canonical in ("",".",".git") or canonical.startswith(".git/"):
            raise ValueError(f"{field} contains forbidden path: {item}")
        if canonical in seen:
            raise ValueError(f"{field} contains duplicate path: {canonical}")
        seen.add(canonical); result.append(canonical)
    return result


def _path_inside_any(path, roots):
    # Canonical ownership preserves a trailing slash for a directory root, while
    # the JSON path-list parser normalizes an operation on that root without it.
    # They denote the same exact project-relative directory; preserve containment
    # semantics without granting a sibling or parent path.
    path=str(path).rstrip("/")
    return any(
        path==str(root).rstrip("/") or path.startswith(str(root).rstrip("/")+"/")
        for root in roots
    )

def split_request(did):
    """Materialize a durable split request from an already-durable ledger marker."""
    if not recursive_split_enabled() or split_depth(did) >= MAX_SPLIT_DEPTH:
        return False,"split-depth-terminal"
    manifest=load_manifest()
    leaf=(manifest.get("leaves") or {}).get(did)
    if not isinstance(leaf,dict) or leaf_children(did):
        return False,"not-splittable"
    attempts=load_attempts()
    entry=(attempts.get("deliverables") or {}).get(did,{})
    marker=entry.get("split_required") if isinstance(entry,dict) else None
    if not isinstance(marker,dict):
        return False,"split-marker-missing"
    generation=int(marker.get("generation") or 1)
    parent_owned=owned_artifact_paths(leaf)
    if not parent_owned:
        reason=(
            "progress-only handoff leaf is already the minimum split unit"
            if leaf.get("split_handoff_only")
            else "split parent has no durable owned artifacts to partition"
        )
        save_split_status(
            did,"split-unavailable-read-only-parent",
            generation=generation,
            reason=reason,
        )
        return False,"split-unavailable-read-only-parent"

    path=split_request_path(did)
    if path.exists():
        existing=load_json_object(path,label=f"split request {did}")
        if (
            existing.get("parent_id")==did
            and int(existing.get("generation") or 0)==generation
            and existing.get("protocol")==SPLIT_PROPOSAL_PROTOCOL
        ):
            return True,"split-required"
        raise StateCorruptionError(f"split request {did} conflicts with ledger generation")

    failures=entry.get("failure_history",[]) if isinstance(entry,dict) else []
    compact=[]
    failed_attempt_ids=set()
    for item in failures[-2:]:
        if isinstance(item,dict):
            row={
                k:item.get(k)
                for k in ("attempt","classification","reason","timestamp")
            }
            compact.append(row)
            try:
                failed_attempt_ids.add(int(item.get("attempt") or 0))
            except (TypeError,ValueError):
                pass
    verify_evidence=load_supervisor_verify_evidence(did)
    split_verify_evidence=[
        item for item in verify_evidence.get("entries",[])
        if isinstance(item,dict)
        and int(item.get("attempt") or 0) in failed_attempt_ids
    ][-2:]
    progress_path=Path(PROJECT)/".opencode-v2"/"work"/f"{did}.progress.md"
    try:
        progress_text=progress_path.read_text(errors="replace")[:4000]
    except OSError:
        progress_text=""
    payload={
        "protocol":SPLIT_PROPOSAL_PROTOCOL,
        "parent_id":did,
        "depth":split_depth(did),
        "generation":generation,
        "parent_scope":leaf.get("name",""),
        "parent_contract":{
            # Batch 9B: the model must receive the detailed parent outcome
            # verbatim. New26 proved name/verify/done_when alone are not
            # sufficient: a splitter rewrote APPARENT=AIRLESS to APPVENT=AIRLESS
            # because the authoritative detailed outcome was absent.
            "outcome":leaf.get("outcome","") or leaf.get("name",""),
            "name":leaf.get("name",""),
            "role":leaf.get("role",""),
            "owned_artifacts":leaf.get("owned_artifacts",""),
            "verify_command":leaf.get("verify_command",""),
            "done_when":leaf.get("done_when",""),
            "acceptance_ids":list(leaf.get("acceptance_ids",[]) or []),
            "launch_deps":list(leaf.get("launch_deps",[]) or []),
            "contract_deps":list(leaf.get("contract_deps",[]) or []),
            "verify_deps":list(leaf.get("verify_deps",[]) or []),
        },
        "ownership":leaf.get("owned_artifacts",""),
        "verification":leaf.get("verify_command",""),
        "durable_progress":{
            "path":str(Path(".opencode-v2/work")/f"{did}.progress.md"),
            "contents":progress_text,
            "authority":"worker-non-authoritative-for-canonical-verify",
        },
        "supervisor_verify_evidence":split_verify_evidence,
        "evidence_precedence":(
            "supervisor_verify_evidence is authoritative for whether the exact "
            "canonical parent Verify ran, its exit code, stdout, and stderr. "
            "durable_progress is worker-authored and MUST NOT override it."
        ),
        "existing_artifacts":[item for item in parent_owned if (Path(PROJECT)/item).exists()],
        "artifact_inventory":_split_artifact_inventory(parent_owned),
        "ownership_items":parent_owned,
        "failed_attempts":compact,
        "decomposition_policy":{
            "progress_handoff_supported":True,
            "handoff_verify_sentinel":SPLIT_HANDOFF_VERIFY_SENTINEL,
            "verification_recovery_allowed":any(
                item.get("classification")=="genuine"
                and _split_failure_is_verification_related(item.get("reason"))
                for item in compact if isinstance(item,dict)
            ),
            "parent_verify_invalid_precedence":(
                "If supervisor_verify_evidence shows the parent verify_command itself "
                "is intrinsically invalid, contradictory, or non-verifying, return "
                "v2-split-parent-contract-invalid-v1. This takes precedence over "
                "verification_recovery_allowed and writer+tester recovery."
            ),
            "rule":(
                "Split must materially reduce executable work: partition owned "
                "artifacts, or use a progress-only probe handoff followed by the "
                "artifact writer. A writer+tester split that leaves the writer with "
                "all parent work is reserved for verification-related failures."
            ),
        },
    }
    atomic_write_json(path,payload)
    status=load_split_status(did)
    if status.get("state") not in {"splitter-active","split-retryable"}:
        save_split_status(did,"split-required",generation=generation)
    return True,"split-required"


def reconcile_required_splits():
    """Recover the durable failure->split edge after a supervisor/process crash."""
    if not PROJECT:
        return
    attempts=load_attempts()
    entries=attempts.get("deliverables") or {}
    for did,entry in entries.items():
        if not isinstance(entry,dict) or not isinstance(entry.get("split_required"),dict):
            continue
        if ready_info(did) or leaf_children(did):
            continue
        if load_split_status(did).get("state") in {
            "split-validation-failed","splitter-failed",
            "split-unavailable-read-only-parent","parent-finalize-failed",
        }:
            continue
        split_request(did)


def archive_failed_split_proposal(did):
    path=split_proposal_path(did)
    if not path.exists():
        return ""
    status=load_split_status(did)
    n=int(status.get("proposal_failures") or 0)+1
    archived=path.with_name(f"{did}.split-proposal.failed-{n}.json")
    if archived.exists():
        archived.unlink()
    os.replace(path,archived)
    return str(archived.relative_to(Path(PROJECT)))


def record_splitter_failure(did, reason, session="", validation=False):
    status=load_split_status(did)
    claims=int(status.get("claim_count") or 0)
    failures=int(status.get("proposal_failures") or 0)+1
    archived=archive_failed_split_proposal(did)
    retryable=claims < splitter_claim_limit(status)
    if retryable:
        state="split-retryable"
    else:
        state="split-validation-failed" if validation else "splitter-failed"
    save_split_status(
        did,state,
        claim_count=claims,
        proposal_failures=failures,
        session=session,
        reason=str(reason)[:1000],
        archived_proposal=archived,
        lease_until_epoch=0,
    )
    return retryable,state


def splitter_claim_limit(status):
    return MAX_SPLITTER_ATTEMPTS + int(status.get("recovery_claim_budget") or 0)


def task_splitter_model_ref():
    """Return the configured task-splitter model without changing role ownership."""
    role=ROOT/"xdg"/"config"/"opencode"/"agents"/"task-splitter.md"
    match=re.search(r"^model:\s*([^\s#]+)\s*$",role.read_text(),re.MULTILINE)
    if not match:
        raise RuntimeError("task-splitter model is missing")
    return match.group(1)


def task_splitter_steps():
    """Return the task-splitter's bounded native turn budget."""
    role=ROOT/"xdg"/"config"/"opencode"/"agents"/"task-splitter.md"
    match=re.search(r"^steps:\s*([1-9][0-9]*)\s*$",role.read_text(),re.MULTILINE)
    if not match:
        raise RuntimeError("task-splitter steps are missing")
    return int(match.group(1))


def task_splitter_profile_fingerprint(model_ref=None):
    """Fingerprint the exact configured model profile used by the task splitter."""
    model_ref=model_ref or task_splitter_model_ref()
    try:
        provider,model=model_ref.split("/",1)
        config=json.loads((ROOT/"xdg"/"config"/"opencode"/"opencode.jsonc").read_text())
        profile=config["provider"][provider]["models"][model]
    except (ValueError,KeyError,FileNotFoundError,json.JSONDecodeError) as exc:
        raise RuntimeError(f"unresolvable task-splitter model profile {model_ref!r}") from exc
    payload=json.dumps(
        {"agent":"task-splitter","model":model_ref,"profile":profile},
        sort_keys=True,separators=(",",":"),ensure_ascii=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def task_splitter_execution_contract_fingerprint(steps=None):
    """Fingerprint the full splitter agent contract plus its pinned model profile."""
    role=ROOT/"xdg"/"config"/"opencode"/"agents"/"task-splitter.md"
    text=role.read_text()
    active_steps=task_splitter_steps()
    if steps is not None:
        steps=int(steps)
        if steps < 1:
            raise RuntimeError("task-splitter prior steps are invalid")
        text,count=re.subn(
            r"^steps:\s*[1-9][0-9]*\s*$",f"steps: {steps}",text,
            count=1,flags=re.MULTILINE,
        )
        if count != 1:
            raise RuntimeError("task-splitter steps cannot be projected")
    payload=json.dumps(
        {
            "protocol":"v1-task-splitter-execution-contract",
            "agent_markdown":text,
            "active_steps":active_steps if steps is None else steps,
            "model_profile":task_splitter_profile_fingerprint(),
        },
        sort_keys=True,separators=(",",":"),ensure_ascii=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def task_splitter_direct_context_fingerprint():
    """Fingerprint the complete no-tool/direct-context splitter transport contract."""
    paths=(
        ROOT/"scripts"/"stage_a_controller.py",
        ROOT/"scripts"/"supervisor.py",
        ROOT/"xdg"/"config"/"opencode"/"plugins"/"v2-bounded-subagent.js",
    )
    payload={
        "protocol":"v1-task-splitter-direct-context-contract",
        "execution_contract":task_splitter_execution_contract_fingerprint(),
        "sources":{
            str(path.relative_to(ROOT)):hashlib.sha256(path.read_bytes()).hexdigest()
            for path in paths
        },
    }
    return hashlib.sha256(
        json.dumps(payload,sort_keys=True,separators=(",",":"),ensure_ascii=True).encode()
    ).hexdigest()


def recover_splitter_direct_context_contract(parent):
    """Record one approved recovery after the exact direct-context/containment repair."""
    if not valid_deliverable_id(parent) or not split_request_path(parent).exists():
        return False,"split-request-missing"
    try:
        fingerprint=task_splitter_direct_context_fingerprint()
    except (OSError,RuntimeError) as exc:
        return False,str(exc)
    expected_reason="creates_or_updates path is outside child ownership: fixtures/vectors/moons"
    with splitter_state_lock(parent):
        status=load_split_status(parent)
        if status.get("state") != "split-validation-failed":
            return False,"state-not-split-validation-failed"
        if status.get("reason") != expected_reason:
            return False,"failure-not-exact-directory-containment"
        if leaf_children(parent) or split_proposal_path(parent).exists():
            return False,"split-already-materialized"
        previous=list(status.get("direct_context_recovery_fingerprints") or [])
        if fingerprint in previous:
            return False,"direct-context-recovery-already-used"
        history=list(status.get("recovery_history") or [])
        history.append({
            "prior_claim_count":int(status.get("claim_count") or 0),
            "prior_proposal_failures":int(status.get("proposal_failures") or 0),
            "reason":"task-splitter-direct-context-contract-recovery",
            "direct_context_contract_fingerprint":fingerprint,
        })
        save_split_status(
            parent,"split-retryable",
            recovery_claim_budget=int(status.get("recovery_claim_budget") or 0)+1,
            recovery_history=history,
            direct_context_recovery_fingerprints=previous+[fingerprint],
            reason="operator-authorized-task-splitter-direct-context-contract-recovery",
            lease_until_epoch=0,
        )
    return True,"recovered"


def _json_archive_record(value, label):
    """Decode one historical control record without accepting arbitrary data."""
    if not isinstance(value,str):
        raise ValueError(f"{label} is not serialized JSON")
    try:
        decoded=json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} is invalid JSON") from exc
    if not isinstance(decoded,dict):
        raise ValueError(f"{label} is not an object")
    return decoded


def recover_false_parent_contract_repair(parent):
    """Recreate exactly one lost split slot after a disproven model repair.

    This is intentionally narrower than the ordinary contract-repair path. It
    accepts only an archived false ``prerequisite_artifacts`` assertion for a
    parent whose canonical Verify remains deterministically valid, preserves
    the archived counters, and adds one (not an open-ended) claim slot.
    """
    if not valid_deliverable_id(parent):
        return False,"invalid-parent"
    if any(path.exists() for path in (
        split_request_path(parent),split_status_path(parent),split_proposal_path(parent),
        split_transaction_path(parent),
    )):
        return False,"split-state-already-present"
    manifest=load_manifest()
    leaf=(manifest.get("leaves") or {}).get(parent)
    if not isinstance(leaf,dict) or leaf_children(parent):
        return False,"parent-not-unsplit-leaf"
    if validate_verify_command(leaf.get("verify_command","")):
        return False,"parent-verify-command-invalid"

    attempts=load_attempts()
    entry=(attempts.get("deliverables") or {}).get(parent)
    pending=entry.get("split_rearm_after_contract_repair") if isinstance(entry,dict) else None
    if not isinstance(pending,dict) or pending.get("field")!="prerequisite_artifacts":
        return False,"missing-false-parent-repair-marker"
    try:
        generation=int(pending.get("generation") or 0)
    except (TypeError,ValueError):
        return False,"invalid-repair-generation"
    if generation < 1:
        return False,"invalid-repair-generation"

    archive_dir=Path(PROJECT)/".opencode-v2"/"work"/"contract-repair-history"
    candidates=[]
    for path in sorted(archive_dir.glob(f"{parent}.*.json")):
        try:
            archive=load_json_object(path,label=f"contract repair archive {path.name}")
            records=archive.get("records")
            if archive.get("protocol")!="v2-contract-repair-history-v1" or not isinstance(records,dict):
                continue
            status=_json_archive_record(records.get(f"{parent}.split-status.json"),"archived split status")
            proposal=_json_archive_record(records.get(f"{parent}.split-proposal.json"),"archived split proposal")
            request=_json_archive_record(records.get(f"{parent}.split-request.json"),"archived split request")
            if (
                status.get("state")!="splitter-active"
                or proposal.get("protocol")!=SPLIT_PARENT_CONTRACT_INVALID_PROTOCOL
                or proposal.get("field")!="prerequisite_artifacts"
                or proposal.get("parent_id")!=parent
                or request.get("parent_id")!=parent
                or int(status.get("generation") or 0)!=generation
                or int(proposal.get("generation") or 0)!=generation
                or int(request.get("generation") or 0)!=generation
            ):
                continue
            candidates.append((path,status))
        except (OSError,StateCorruptionError,ValueError,TypeError):
            continue
    if len(candidates)!=1:
        return False,"false-parent-repair-archive-not-unique"
    archive_path,archived_status=candidates[0]
    claims=int(archived_status.get("claim_count") or 0)
    failures=int(archived_status.get("proposal_failures") or 0)
    budget=int(archived_status.get("recovery_claim_budget") or 0)
    if (
        claims < 1 or failures < 1 or failures > claims
        or claims != splitter_claim_limit(archived_status)
    ):
        return False,"archived-split-counters-not-terminal"
    try:
        direct_context=task_splitter_direct_context_fingerprint()
    except (OSError,RuntimeError) as exc:
        return False,str(exc)
    fingerprint=hashlib.sha256(json.dumps({
        "protocol":"v1-false-parent-contract-repair-recovery",
        "archive":archive_path.name,"generation":generation,
        "claims":claims,"failures":failures,"direct_context_contract":direct_context,
    },sort_keys=True,separators=(",",":"),ensure_ascii=True).encode()).hexdigest()

    # Recreate a current canonical request from current manifest/evidence. Do
    # not resurrect the archived model prompt, stale state version, or lease.
    with splitter_state_lock(parent):
        with dispatch_lock:
            with attempt_lock():
                data=load_attempts()
                current=(data.get("deliverables") or {}).get(parent)
                current_pending=current.get("split_rearm_after_contract_repair") if isinstance(current,dict) else None
                if not isinstance(current_pending,dict) or current_pending.get("field")!="prerequisite_artifacts":
                    return False,"false-parent-repair-marker-changed"
                if current.get("split_required"):
                    return False,"split-marker-already-present"
                # Preserve historical reclassification exactly; this marker is
                # a recovery edge, not evidence rewriting.
                current["split_required"]={
                    "generation":generation,
                    "reason":"operator-authorized-false-parent-contract-repair-recovery",
                    "timestamp":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime()),
                }
                current["false_parent_contract_repair_recovery"]={
                    "archive":archive_path.name,"generation":generation,
                    "prior_claim_count":claims,"prior_proposal_failures":failures,
                    "timestamp":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime()),
                }
                save_attempts(data)
            ok,detail=split_request(parent)
            if not ok:
                raise StateCorruptionError(f"{parent} recovery could not materialize split request: {detail}")
            history=list(archived_status.get("recovery_history") or [])
            history.append({
                "prior_claim_count":claims,"prior_proposal_failures":failures,
                "reason":"false-parent-contract-repair-recovery",
                "archive":archive_path.name,
                "direct_context_contract_fingerprint":direct_context,
            })
            save_split_status(
                parent,"split-retryable",generation=generation,
                claim_count=claims,proposal_failures=failures,
                # Exactly one replacement slot: claim 7. A rejected claim 7
                # reaches the finite limit and cannot silently become claim 8.
                recovery_claim_budget=budget+1,recovery_history=history,
                false_parent_contract_repair_recovery_fingerprints=[fingerprint],
                reason="operator-authorized-false-parent-contract-repair-recovery",
                lease_until_epoch=0,
            )
    return True,"recovered-one-claim"


def _finalize_current_plan_without_rearm():
    """Compile/guard current source without running supervisor repair side effects."""
    if not compile_structured_plan():
        return False
    try:
        result=subprocess.run(
            [sys.executable,str(ROOT/"scripts"/"control-guard.py"),
             "--project",PROJECT,"--finalize-plan"],
            capture_output=True,text=True,timeout=8,
        )
    except (OSError,subprocess.SubprocessError):
        return False
    return result.returncode==0 and plan_ready()


def resolve_false_parent_contract_repair(parent):
    """Retire only the live repair packet created by the disproven assertion.

    The packet is first preserved beside the original split archive.  Current
    source is then recompiled/finalized directly, deliberately avoiding the
    supervisor's generic contract-repair rearm logic because this repair was
    never independently authorized by deterministic Verify validation.
    """
    if not valid_deliverable_id(parent):
        return False,"invalid-parent"
    status=load_split_status(parent)
    attempts=load_attempts()
    entry=(attempts.get("deliverables") or {}).get(parent)
    recovery=entry.get("false_parent_contract_repair_recovery") if isinstance(entry,dict) else None
    if not isinstance(recovery,dict) or recovery.get("resolution_archive"):
        return False,"false-parent-repair-recovery-not-pending"
    if (
        status.get("state")!="split-retryable"
        or status.get("reason")!="operator-authorized-false-parent-contract-repair-recovery"
        or int(status.get("claim_count") or 0)!=int(recovery.get("prior_claim_count") or -1)
        or int(status.get("proposal_failures") or 0)!=int(recovery.get("prior_proposal_failures") or -1)
    ):
        return False,"split-recovery-state-changed"
    pending=entry.get("split_rearm_after_contract_repair")
    if not isinstance(pending,dict) or pending.get("field")!="prerequisite_artifacts":
        return False,"false-parent-repair-marker-changed"
    repair_path=Path(PROJECT)/".opencode-v2"/STRUCTURED_PLAN_REPAIR_FILENAME
    try:
        repair=load_json_object(repair_path,label="false parent-contract repair packet")
    except (OSError,StateCorruptionError):
        return False,"false-parent-repair-packet-missing"
    errors=repair.get("errors")
    if (
        repair.get("protocol")!="v2-structured-plan-repair-v1"
        or repair.get("source")!="runtime-split-parent-contract"
        or repair.get("whole_plan") is not False
        or repair.get("affected_keys")!=[pending.get("structured_key")]
        or not isinstance(errors,list) or len(errors)!=1
        or errors[0].get("code")!="runtime-parent-prerequisite-artifacts-missing"
    ):
        return False,"false-parent-repair-packet-mismatch"

    archive_dir=Path(PROJECT)/".opencode-v2"/"work"/"contract-repair-history"
    archive_dir.mkdir(parents=True,exist_ok=True)
    resolution=archive_dir/f"{parent}.false-parent-contract-repair-resolution.{time.time_ns()}.json"
    atomic_write_json(resolution,{
        "owner":"supervisor",
        "protocol":"v1-false-parent-contract-repair-resolution-v1",
        "parent_id":parent,
        "resolved_at":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime()),
        "original_archive":recovery.get("archive"),
        "rejected_repair_packet":repair,
        "reason":"deterministically disproven prerequisite-artifacts parent repair",
    })
    repair_path.unlink()
    if not _finalize_current_plan_without_rearm():
        atomic_write_json(repair_path,repair)
        return False,"current-plan-finalization-failed"
    with dispatch_lock:
        with attempt_lock():
            data=load_attempts()
            current=(data.get("deliverables") or {}).get(parent)
            current_recovery=current.get("false_parent_contract_repair_recovery") if isinstance(current,dict) else None
            if not isinstance(current_recovery,dict) or current_recovery.get("resolution_archive"):
                return False,"false-parent-repair-recovery-changed"
            current.pop("split_rearm_after_contract_repair",None)
            current_recovery["resolution_archive"]=resolution.name
            current_recovery["resolved_at"]=time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime())
            save_attempts(data)
    return True,"resolved-current-plan"


def recover_splitter_execution_contract(parent, prior_steps):
    """Authorize one audited claim for the exact adjacent splitter-step repair."""
    if not valid_deliverable_id(parent) or not split_request_path(parent).exists():
        return False,"split-request-missing"
    try:
        current_steps=task_splitter_steps()
        prior_steps=int(prior_steps)
        if prior_steps != current_steps-1:
            return False,"prior-step-contract-not-adjacent"
        current_fingerprint=task_splitter_execution_contract_fingerprint()
        prior_fingerprint=task_splitter_execution_contract_fingerprint(prior_steps)
    except (RuntimeError,ValueError) as exc:
        return False,str(exc)
    if current_fingerprint == prior_fingerprint:
        return False,"execution-contract-unchanged"
    with splitter_state_lock(parent):
        status=load_split_status(parent)
        if status.get("state") != "splitter-failed": return False,"state-not-splitter-failed"
        if status.get("reason") != "splitter-completed-without-json-proposal":
            return False,"failure-not-output-missing"
        if leaf_children(parent) or split_proposal_path(parent).exists():
            return False,"split-already-materialized"
        previous=list(status.get("execution_contract_recovery_fingerprints") or [])
        if current_fingerprint in previous:
            return False,"execution-contract-recovery-already-used"
        if len(previous) >= MAX_SPLITTER_EXECUTION_CONTRACT_RECOVERIES:
            return False,"execution-contract-recovery-exhausted"
        history=list(status.get("recovery_history") or [])
        history.append({
            "prior_claim_count":int(status.get("claim_count") or 0),
            "prior_proposal_failures":int(status.get("proposal_failures") or 0),
            "reason":"task-splitter-execution-contract-recovery",
            "prior_steps":prior_steps,
            "prior_execution_contract_fingerprint":prior_fingerprint,
            "current_steps":current_steps,
            "current_execution_contract_fingerprint":current_fingerprint,
        })
        save_split_status(
            parent,"split-retryable",
            recovery_claim_budget=int(status.get("recovery_claim_budget") or 0)+1,
            recovery_history=history,
            execution_contract_recovery_fingerprints=previous+[current_fingerprint],
            reason="operator-authorized-task-splitter-execution-contract-recovery",
            lease_until_epoch=0,
        )
    return True,"recovered"


def recover_splitter_profile_change(parent, prior_model):
    """Authorize one audited replacement claim only for a changed splitter profile."""
    if not valid_deliverable_id(parent) or not split_request_path(parent).exists():
        return False,"split-request-missing"
    try:
        current_model=task_splitter_model_ref()
        current_fingerprint=task_splitter_profile_fingerprint(current_model)
        prior_fingerprint=task_splitter_profile_fingerprint(prior_model)
    except RuntimeError as exc:
        return False,str(exc)
    if current_fingerprint == prior_fingerprint:
        return False,"profile-unchanged"
    with splitter_state_lock(parent):
        status=load_split_status(parent)
        if status.get("state") != "splitter-failed": return False,"state-not-splitter-failed"
        if status.get("reason") != "splitter-completed-without-json-proposal":
            return False,"failure-not-output-missing"
        if leaf_children(parent) or split_proposal_path(parent).exists():
            return False,"split-already-materialized"
        previous=list(status.get("profile_recovery_fingerprints") or [])
        if current_fingerprint in previous:
            return False,"profile-recovery-already-used"
        if len(previous) >= MAX_SPLITTER_PROFILE_RECOVERIES:
            return False,"profile-recovery-exhausted"
        history=list(status.get("recovery_history") or [])
        history.append({
            "prior_claim_count":int(status.get("claim_count") or 0),
            "prior_proposal_failures":int(status.get("proposal_failures") or 0),
            "reason":"task-splitter-profile-fingerprint-recovery",
            "prior_model":prior_model,
            "prior_profile_fingerprint":prior_fingerprint,
            "current_model":current_model,
            "current_profile_fingerprint":current_fingerprint,
        })
        save_split_status(
            parent,"split-retryable",
            recovery_claim_budget=int(status.get("recovery_claim_budget") or 0)+1,
            recovery_history=history,
            profile_recovery_fingerprints=previous+[current_fingerprint],
            reason="operator-authorized-task-splitter-profile-recovery",
            lease_until_epoch=0,
        )
    return True,"recovered"


def recover_splitter_output_limit(parent):
    """Authorize one audited replacement claim after repeated output truncation."""
    if not valid_deliverable_id(parent) or not split_request_path(parent).exists():
        return False,"split-request-missing"
    with splitter_state_lock(parent):
        status=load_split_status(parent)
        if status.get("state") != "splitter-failed": return False,"state-not-splitter-failed"
        if status.get("reason") != "splitter-completed-without-json-proposal":
            return False,"failure-not-output-missing"
        if leaf_children(parent) or split_proposal_path(parent).exists():
            return False,"split-already-materialized"
        if int(status.get("recovery_claim_budget") or 0) >= MAX_SPLITTER_OUTPUT_LIMIT_RECOVERIES:
            return False,"output-limit-recovery-exhausted"
        history=list(status.get("recovery_history") or [])
        history.append({"prior_claim_count":int(status.get("claim_count") or 0),"prior_proposal_failures":int(status.get("proposal_failures") or 0),"reason":"model-profile-repair"})
        save_split_status(parent,"split-retryable",recovery_claim_budget=1,recovery_history=history,reason="operator-authorized-output-limit-recovery",lease_until_epoch=0)
    return True,"recovered"


def split_transaction_id(parent,generation,children):
    payload=json.dumps(
        {"parent":parent,"generation":generation,"children":children},
        sort_keys=True,separators=(",",":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:24]


def load_split_transaction(parent):
    path=split_transaction_path(parent)
    if not path.exists():
        return {}
    data=load_json_object(path,label=f"split transaction {parent}")
    if (
        data.get("owner")!="supervisor"
        or data.get("protocol")!=SPLIT_TRANSACTION_PROTOCOL
        or data.get("parent_id")!=parent
        or data.get("state") not in {"prepared","committed"}
    ):
        raise StateCorruptionError(f"split transaction {parent} is invalid")
    return data


def _canonical_split_child_scope_text(child_id,parent,child,binding_outcome):
    """Remove splitter-only ambiguity before rendering a durable child scope.

    The splitter cannot know supervisor-derived child IDs.  If it nevertheless
    mentions a progress path, bind it to the canonical child/handoff source.
    Also remove an exact redundant `Inherit: <parent outcome>` style clause;
    inherited contract text is rendered separately by the supervisor.
    """
    text=str(child.get("name","") or "").strip()
    handoff_only=bool(child.get("split_handoff_only"))
    handoff_source=str(child.get("split_handoff_source") or "")
    progress_owner=child_id if handoff_only else handoff_source
    if progress_owner:
        text=re.sub(
            r"\.opencode-v2/work/[A-Za-z0-9-]+\.progress\.md",
            f".opencode-v2/work/{progress_owner}.progress.md",
            text,
        )
    binding=str(binding_outcome or "").strip()
    if binding:
        for prefix in ("Inherit:", "Inherited outcome:", "Parent outcome:"):
            text=text.replace(f"{prefix} {binding}", "")
        text=re.sub(r"[ \t]{2,}", " ", text).strip(" ;")
    return text


def render_split_child_scope(child_id,parent,child,parent_leaf):
    """Render a split scope with deterministic inherited-contract precedence.

    Normal writing children remain bound by the full inherited parent contract.
    A progress-only handoff is different: its bounded child scope is the complete
    executable obligation for that child, while the full parent outcome remains
    enforced only at later writer/parent finalization.
    """
    binding_outcome=(
        child.get("parent_outcome_context","")
        or parent_leaf.get("outcome","")
        or parent_leaf.get("name","")
    )
    acceptance_ids=child.get("acceptance_ids") or []
    acceptance_text=(
        ", ".join(acceptance_ids)
        if isinstance(acceptance_ids,list)
        else str(acceptance_ids)
    )
    handoff_only=bool(child.get("split_handoff_only"))
    handoff_source=str(child.get("split_handoff_source") or "")
    scope_text=_canonical_split_child_scope_text(
        child_id,parent,child,binding_outcome
    )
    handoff_text=""
    if handoff_only:
        contract_text=(
            "## Parent contract boundary — supervisor preserved\n\n"
            "The parent outcome is retained for eventual parent collapse and for "
            "the dependent writer. It is CONTEXT ONLY for THIS progress child: it "
            "does NOT expand this child's executable work. The complete executable "
            "obligation for THIS child is the bounded Child scope below. Do not "
            "perform parent work that is not explicitly named in that Child scope.\n\n"
            f"Parent outcome (context only): {binding_outcome}\n"
        )
        handoff_text=(
            "\n## Progress-only handoff protocol — supervisor enforced\n\n"
            "THIS child must NOT create or modify the final project artifact or "
            "attempt the final parent output. Its complete deliverable is only the "
            "bounded discovery/acquisition/diagnosis in Child scope plus the durable "
            "progress handoff below. Record reusable results in "
            f"`.opencode-v2/work/{child_id}.progress.md` using all of these exact "
            "labels:\n\n"
            "`HANDOFF_READY: true`\n\n"
            "`Findings:`\n\n"
            "`Evidence:`\n\n"
            "`Next step:`\n\n"
            "The progress file is this child's durable deliverable. Keep concrete "
            "IDs, commands, API shapes, paths, values, or failure causes needed by "
            "the dependent writer so it does not repeat the probe.\n"
        )
        decomposition_text=(
            "The following Child scope is authoritative and complete for THIS "
            "progress-only child. Do not infer additional executable work from the "
            "parent outcome above.\n\n"
        )
    else:
        contract_text=(
            "## Parent contract boundary — supervisor preserved\n\n"
            "The parent outcome below is CONTEXT ONLY for THIS split child. It is "
            "retained verbatim so literals and domain meaning are not lost, but it "
            "does NOT expand this child's executable work. The supervisor enforces "
            "the complete parent outcome, parent Verify, and parent Done-when later "
            "when collapsing the split.\n\n"
            f"Parent outcome (context only): {binding_outcome}\n\n"
            f"Parent Acceptance IDs (context): {acceptance_text or 'none'}\n\n"
            f"Immediate parent Verify command (parent finalization only): "
            f"`{parent_leaf.get('verify_command','')}`\n\n"
            f"Immediate parent Done when (parent finalization only): "
            f"{parent_leaf.get('done_when','')}\n"
        )
        decomposition_text=(
            "The following Child scope, Owned artifacts, Child Verify, and Child "
            "Done-when are the COMPLETE executable obligation for THIS child. Do "
            "not perform other parent work merely because it appears in parent "
            "context above.\n\n"
        )
        if handoff_source:
            handoff_text=(
                "\n## Required predecessor handoff — supervisor enforced\n\n"
                f"Before doing project work, read `.opencode-v2/work/{handoff_source}.progress.md`. "
                "Treat its concrete findings/evidence as the durable result of the prior "
                "bounded probe. Consume that handoff and finish the artifact stage; do not "
                "repeat the predecessor's investigation unless direct validation disproves it.\n"
            )
    return (
        f"# {child_id} split-child scope\n\n"
        f"Parent: {parent}\n\n"
        f"{contract_text}"
        f"{handoff_text}\n"
        "## Bounded child decomposition\n\n"
        f"{decomposition_text}"
        f"Child scope: {scope_text}\n\n"
        f"Owned artifacts: {child['owned_artifacts']}\n\n"
        f"Child Verify command: `{child['verify_command']}`\n\n"
        f"Child Done when: {child['done_when']}\n"
    )


def apply_split_transaction(parent,txn):
    expected=list(txn["children"])
    child_defs=txn["child_defs"]
    manifest=load_manifest()
    leaves=manifest.setdefault("leaves",{})
    parent_leaf=leaves.get(parent)
    if not isinstance(parent_leaf,dict):
        raise StateCorruptionError(f"split parent {parent} disappeared")
    current=parent_leaf.get("split_children") or []
    if current not in ([],expected):
        raise StateCorruptionError(f"split parent {parent} has conflicting children {current}")
    for child_id in expected:
        child=child_defs[child_id]
        existing=leaves.get(child_id)
        if isinstance(existing,dict) and existing != child:
            raise StateCorruptionError(f"split child {child_id} conflicts with prepared transaction")
        leaves[child_id]=child
        scope_path=Path(PROJECT)/".opencode-v2"/"work"/f"{child_id}.scope.md"
        atomic_write_text(
            scope_path,
            render_split_child_scope(child_id,parent,child,parent_leaf),
        )
    parent_leaf["split_children"]=expected
    parent_leaf["split_depth"]=split_depth(parent)
    manifest["recursive_split_protocol"]=RECURSIVE_SPLIT_PROTOCOL
    save_manifest(manifest)

    overlay=load_split_leaf_overlay()
    parents=overlay.setdefault("parents",{})
    prepared_entry={
        "children":expected,
        "child_defs":child_defs,
        "transaction_id":txn["transaction_id"],
        "timestamp":txn["prepared_at"],
    }
    existing_overlay=parents.get(parent)
    if isinstance(existing_overlay,dict):
        existing_txn=existing_overlay.get("transaction_id")
        if existing_txn and existing_txn!=txn["transaction_id"]:
            raise StateCorruptionError(f"split overlay {parent} conflicts with transaction")
    parents[parent]=prepared_entry
    save_split_leaf_overlay(overlay)

    path=split_history_path()
    history=load_json_object(
        path,
        default_missing={"owner":"supervisor","protocol":SPLIT_PROPOSAL_PROTOCOL,"splits":[]},
        label="split history",
    )
    splits=history.get("splits",[])
    if not isinstance(splits,list):
        raise StateCorruptionError("split history splits must be an array")
    if not any(
        isinstance(item,dict) and item.get("transaction_id")==txn["transaction_id"]
        for item in splits
    ):
        splits.append({
            "parent":parent,
            "children":expected,
            "transaction_id":txn["transaction_id"],
            "timestamp":txn["prepared_at"],
            "source":"supervisor",
        })
    history["splits"]=splits
    atomic_write_json(path,history)

    save_split_status(
        parent,"accepted",
        generation=txn["generation"],
        children=expected,
        transaction_id=txn["transaction_id"],
        lease_until_epoch=0,
    )
    split_request_path(parent).unlink(missing_ok=True)
    split_proposal_path(parent).unlink(missing_ok=True)

    committed=dict(txn)
    committed["state"]="committed"
    committed["committed_at"]=time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime())
    atomic_write_json(split_transaction_path(parent),committed)
    return expected


def reconcile_split_transactions():
    if not PROJECT:
        return
    work=Path(PROJECT)/".opencode-v2"/"work"
    for path in work.glob("D*.split-transaction.json"):
        parent=path.name.removesuffix(".split-transaction.json")
        txn=load_split_transaction(parent)
        if txn.get("state")=="prepared":
            with dispatch_lock:
                with attempt_lock():
                    apply_split_transaction(parent,txn)


def validate_split_proposal(parent, proposals, request=None):
    """Validate one materially-shrinking two-child split.

    Accepted shapes are deliberately finite:
    1) two writers partition the parent's owned artifacts;
    2) progress-only probe handoff -> writer owning the parent artifacts;
    3) writer -> read-only tester, but only after verification-related failures.

    Shape (3) does not reduce implementation work, so it is not a generic retry
    mechanism. New30 showed that repeatedly wrapping a single-artifact writer in
    testers simply reproduced the same oversized task at every recursive depth.
    """
    manifest=load_manifest(); leaves=manifest.get("leaves") or {}; leaf=leaves.get(parent)
    expected=expected_children(parent)
    if not recursive_split_enabled() or not isinstance(leaf,dict) or not expected:
        raise ValueError("parent is not eligible for recursive split")
    if leaf_children(parent): raise ValueError("parent already split")
    if not isinstance(proposals,list) or len(proposals)!=2:
        raise ValueError("split requires exactly two proposals")
    parent_owned=set(owned_artifact_paths(leaf))
    if not parent_owned:
        raise ValueError("split parent has no validated owned artifacts")
    if request is None:
        request=load_json_object(split_request_path(parent),label=f"split request {parent}")

    seen=set(); children=[]; meta=[]
    for index, proposal in enumerate(proposals):
        if not isinstance(proposal,dict):
            raise ValueError("child proposal must be an object")
        allowed={
            "scope","owned_artifacts","verify_command","role","depends_on_sibling",
            "done_when","reads_existing","creates_or_updates",
        }
        if set(proposal)-allowed or not all(
            isinstance(proposal.get(k),str) and proposal[k].strip()
            for k in ("scope","owned_artifacts","verify_command","role","done_when")
        ):
            raise ValueError("child proposal has missing or unsupported fields")
        if "reads_existing" not in proposal or "creates_or_updates" not in proposal:
            raise ValueError("child proposal requires reads_existing and creates_or_updates arrays")
        reads_existing=_canonical_split_path_list(proposal.get("reads_existing"),"reads_existing")
        creates_or_updates=_canonical_split_path_list(proposal.get("creates_or_updates"),"creates_or_updates")
        missing_reads=[rel for rel in reads_existing if not (Path(PROJECT)/rel).exists()]
        if missing_reads:
            raise ValueError("reads_existing path does not exist: "+", ".join(missing_reads))

        owned_list,owned_error=_strict_owned_artifact_text(proposal["owned_artifacts"])
        if owned_error:
            raise ValueError(f"child ownership is not canonical: {owned_error}")
        owned=set(owned_list)
        role=proposal["role"].strip()
        sibling=proposal.get("depends_on_sibling", "")
        if index == 1 and sibling == expected[0]:
            sibling = "first"
        if sibling not in ("", "first") or (sibling == "first" and index != 1):
            raise ValueError("only second child may depend on first child")

        handoff_only=(
            index==0
            and role=="probe-builder"
            and proposal["owned_artifacts"].strip()=="none"
        )
        if handoff_only:
            if proposal["verify_command"].strip()!=SPLIT_HANDOFF_VERIFY_SENTINEL:
                raise ValueError(
                    "progress-only probe must use exact Verify sentinel "
                    + SPLIT_HANDOFF_VERIFY_SENTINEL
                )
            if sibling:
                raise ValueError("progress-only probe must be first and independent")
            scope=proposal["scope"].strip()
            if len(scope)>MAX_SPLIT_HANDOFF_SCOPE_CHARS:
                raise ValueError(
                    "progress-only probe scope is too large: "
                    f"{len(scope)} chars > {MAX_SPLIT_HANDOFF_SCOPE_CHARS}"
                )
            if len(SPLIT_HANDOFF_STAGE_RE.findall(scope))>1:
                raise ValueError(
                    "progress-only probe scope is compound; isolate one bounded "
                    "discovery/acquisition/diagnosis stage"
                )
        else:
            contract_errors=validate_leaf_contract(role, owned_list, proposal["verify_command"])
            if contract_errors:
                raise ValueError("; ".join(contract_errors))
            parent_verify=str(leaf.get("verify_command") or "").strip()
            if (
                parent_verify==RUN_CHECKS_COMMAND
                and ".opencode-v2/TEST_CHECKS.json" in owned
                and proposal["verify_command"].strip()!=RUN_CHECKS_COMMAND
            ):
                raise ValueError(
                    "final-test manifest child must preserve exact parent Verify "
                    + RUN_CHECKS_COMMAND
                )

        read_only_role=role in READ_ONLY_SPLIT_ROLES
        if handoff_only or read_only_role:
            if creates_or_updates:
                raise ValueError("progress-only/read-only child must have empty creates_or_updates")
        else:
            if not creates_or_updates:
                raise ValueError("writing child requires non-empty creates_or_updates")
            outside=[rel for rel in creates_or_updates if not _path_inside_any(rel,owned)]
            if outside:
                raise ValueError("creates_or_updates path is outside child ownership: "+", ".join(outside))

        if not handoff_only:
            if read_only_role and owned:
                raise ValueError(
                    "read-only split role must use owned_artifacts: none; "
                    "use implementer/test-builder when the child must create an artifact"
                )
            if not owned:
                if not read_only_role:
                    raise ValueError(
                        "only the first progress-only probe-builder or a read-only tester "
                        "may have owned_artifacts: none"
                    )
                if proposal["owned_artifacts"].strip() != "none":
                    raise ValueError("read-only tester must encode empty ownership as exact 'none'")
                if index != 1 or sibling != "first":
                    raise ValueError("read-only tester must be the second child and depend on the first")
            else:
                if not owned <= parent_owned or seen & owned:
                    raise ValueError("child ownership must be disjoint and inside parent ownership")
                seen |= owned

        child=dict(leaf)
        inherited_outcome=leaf.get("outcome","") or leaf.get("name","")
        child_verify=(
            split_handoff_verify_command(expected[index])
            if handoff_only else proposal["verify_command"]
        )
        child_done=(
            "Durable progress handoff records HANDOFF_READY, Findings, Evidence, and Next step; "
            "no project artifact is modified."
            if handoff_only else proposal["done_when"]
        )
        child.update({
            "id":expected[index],
            "name":proposal["scope"],
            "outcome":proposal["scope"],
            "parent_outcome_context":inherited_outcome,
            "owned_artifacts":_canonical_owned_artifacts(owned_list),
            "owned_artifact_paths":owned_list,
            "verify_command":child_verify,
            "role":role,
            "done_when":child_done,
            "parent":parent,
            "split_depth":split_depth(expected[index]),
            "split_children":[],
            "split_handoff_only":handoff_only,
            "split_handoff_source":"",
            "split_reads_existing":reads_existing,
            "split_creates_or_updates":creates_or_updates,
        })
        child["launch_deps"]=list(leaf.get("launch_deps",[])) + ([expected[0]] if sibling=="first" else [])
        if handoff_only:
            # The probe does not claim acceptance coverage and should not wait on
            # final-product Verify dependencies. Parent collapse still enforces
            # the complete inherited contract after both children are READY.
            child["acceptance_ids"]=[]
            child["verify_deps"]=[]
        children.append(child)
        meta.append({
            "owned":owned,
            "role":role,
            "sibling":sibling,
            "handoff_only":handoff_only,
        })

    handoffs=[i for i,item in enumerate(meta) if item["handoff_only"]]
    read_only=[i for i,item in enumerate(meta) if item["role"] in READ_ONLY_SPLIT_ROLES and not item["owned"]]

    if handoffs:
        if handoffs != [0]:
            raise ValueError("exactly the first child may be a progress-only handoff")
        second=meta[1]
        if second["role"] in READ_ONLY_SPLIT_ROLES or not second["owned"]:
            raise ValueError("progress handoff must be followed by a writing child")
        if second["sibling"]!="first":
            raise ValueError("writer after progress handoff must depend on first child")
        if second["owned"] != parent_owned:
            raise ValueError("writer after progress handoff must own all unfinished parent artifacts")
        parent_verify=str(leaf.get("verify_command") or "").strip()
        writer_verify=str(children[1].get("verify_command") or "").strip()
        if not parent_verify or writer_verify!=parent_verify:
            raise ValueError(
                "writer after progress handoff must preserve exact parent Verify"
            )
        children[1]["split_handoff_source"]=expected[0]
    elif read_only:
        # The only non-shrinking shape is writer -> tester. Keep it solely for
        # parent-Verify failures where independent verification can actually
        # repair the failed dimension. Runtime/ownership/progress failures must
        # decompose executable work instead of retrying the same writer.
        if read_only != [1] or meta[0]["owned"] != parent_owned or meta[1]["sibling"]!="first":
            raise ValueError("read-only tester split must be writer(all parent artifacts) -> tester")
        if not _split_request_verification_recovery_allowed(request):
            raise ValueError(
                "non-shrinking writer+tester split is only allowed after verification-related "
                "failures; use progress-only probe -> writer or partition parent artifacts"
            )

        # Batch 15B: verification recovery must actually escape the failed
        # verification path. New31 D007 produced a corrected tester but left
        # child #1 with the exact failed parent Verify (occupied port 8123).
        # Because child #2 depends on child #1, the useful verifier could never
        # become reachable.
        parent_contract=request.get("parent_contract") if isinstance(request,dict) else {}
        parent_verify=(
            parent_contract.get("verify_command","")
            if isinstance(parent_contract,dict) else ""
        ) or leaf.get("verify_command","")
        failed_verify=_normalized_verify_for_recovery_compare(parent_verify)
        writer_verify=_normalized_verify_for_recovery_compare(children[0].get("verify_command",""))
        tester_verify=_normalized_verify_for_recovery_compare(children[1].get("verify_command",""))
        if not failed_verify:
            raise ValueError("verification-recovery split is missing the failed parent Verify command")
        if writer_verify == failed_verify:
            raise ValueError(
                "verification-recovery writer must not reuse the failed parent Verify command"
            )
        if tester_verify == failed_verify:
            raise ValueError(
                "verification-recovery tester must not reuse the failed parent Verify command"
            )
        if tester_verify == writer_verify:
            raise ValueError(
                "verification-recovery tester must independently verify with a command "
                "different from the writer Verify command"
            )

    if seen != parent_owned:
        raise ValueError("child ownership must cover all unfinished parent ownership")
    return expected,children


def persist_split(parent, proposals):
    """Prepare once, then idempotently commit a recoverable split transaction."""
    with dispatch_lock:
        with attempt_lock():
            existing=load_split_transaction(parent)
            if existing:
                if existing.get("state")=="committed":
                    return list(existing.get("children") or [])
                return apply_split_transaction(parent,existing)

            request=load_json_object(
                split_request_path(parent),label=f"split request {parent}"
            )
            expected,children=validate_split_proposal(parent,proposals,request=request)
            generation=int(request.get("generation") or 1)
            child_defs={child["id"]:child for child in children}
            transaction_id=split_transaction_id(parent,generation,child_defs)
            txn={
                "owner":"supervisor",
                "protocol":SPLIT_TRANSACTION_PROTOCOL,
                "state":"prepared",
                "parent_id":parent,
                "generation":generation,
                "children":expected,
                "child_defs":child_defs,
                "transaction_id":transaction_id,
                "prepared_at":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime()),
            }
            atomic_write_json(split_transaction_path(parent),txn)
            return apply_split_transaction(parent,txn)


def _structured_plan_symbolic_key(did):
    mapping=load_json_object(
        Path(PROJECT)/".opencode-v2"/"IMPLEMENTATION_PLAN.structured-map.json",
        label="structured plan map",
    )
    if mapping.get("protocol")!="v2-structured-plan-map-v1":
        raise ValueError("structured plan map protocol mismatch")
    key=(mapping.get("id_to_key") or {}).get(did)
    if not isinstance(key,str) or not key:
        raise ValueError(f"no structured-plan key for {did}")
    return key


def _clear_split_request_state_for_contract_repair(did):
    for path in (
        split_request_path(did),
        split_proposal_path(did),
        split_status_path(did),
        split_transaction_path(did),
    ):
        path.unlink(missing_ok=True)


def _archive_split_state_for_contract_repair(did):
    """Snapshot mutable split control records before a plan repair retires them."""
    paths=(
        split_request_path(did),
        split_proposal_path(did),
        split_status_path(did),
        split_transaction_path(did),
    )
    records={}
    for path in paths:
        try:
            records[path.name]=path.read_text(errors="replace")
        except OSError:
            continue
    overlay=load_split_leaf_overlay()
    parents=overlay.get("parents") if isinstance(overlay.get("parents"),dict) else {}
    if did in parents:
        records["split-leaf-overlay-entry.json"]=parents[did]
    if not records:
        return None
    archive=Path(PROJECT)/".opencode-v2"/"work"/"contract-repair-history"
    archive.mkdir(parents=True,exist_ok=True)
    path=archive/f"{did}.{time.time_ns()}.json"
    atomic_write_json(path,{
        "owner":"supervisor",
        "protocol":"v2-contract-repair-history-v1",
        "parent_id":did,
        "archived_at":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime()),
        "records":records,
    })
    return path


def _detach_split_overlay_for_contract_repair(did):
    """Remove a stale executable split while retaining its immutable history.

    A plan repair replaces the parent contract.  Its old child definitions must
    not continue to shadow the repaired parent in the generated manifest, but
    the transaction, child progress, ready files, and ledger remain durable
    audit evidence.
    """
    overlay=load_split_leaf_overlay()
    parents=overlay.get("parents") if isinstance(overlay.get("parents"),dict) else {}
    if did not in parents:
        return False
    parents.pop(did,None)
    overlay["parents"]=parents
    save_split_leaf_overlay(overlay)
    return True


def request_parent_contract_repair(did, payload, request):
    field,reason,verify_errors=_validate_parent_contract_invalid_payload(
        did,payload,request,
    )
    if verify_errors:
        return _request_parent_contract_repair(
            did,field,reason,request,verify_errors,
        )
    raise ValueError(
        "parent-contract-invalid verify_command lacks a deterministic contract defect"
    )


def _validate_parent_contract_invalid_payload(did, payload, request):
    """Validate the alternate splitter form without granting it authority.

    The caller decides whether an otherwise well-formed assertion becomes a
    repair request or the single corrective-turn case.  This keeps the
    semantic claim separate from deterministic Verify truth.
    """
    allowed={"protocol","parent_id","depth","generation","field","reason"}
    if set(payload) != allowed:
        raise ValueError("parent-contract-invalid payload has wrong fields")
    if payload.get("protocol")!=SPLIT_PARENT_CONTRACT_INVALID_PROTOCOL:
        raise ValueError("parent-contract-invalid protocol mismatch")
    if payload.get("parent_id")!=did:
        raise ValueError("parent-contract-invalid parent mismatch")
    if payload.get("depth")!=split_depth(did):
        raise ValueError("parent-contract-invalid depth mismatch")
    if payload.get("generation")!=request.get("generation",1):
        raise ValueError("parent-contract-invalid generation mismatch")
    field=payload.get("field")
    if field != "verify_command":
        raise ValueError(
            "parent-contract-invalid prerequisite artifacts are split-recoverable; "
            "only a deterministically invalid verify_command may request plan repair"
        )
    reason=str(payload.get("reason") or "").strip()
    if len(reason)<20 or len(reason)>1200:
        raise ValueError("parent-contract-invalid reason must be 20..1200 chars")

    parent_contract=request.get("parent_contract")
    verify_command=(
        parent_contract.get("verify_command") if isinstance(parent_contract,dict) else ""
    )
    verify_errors=validate_verify_command(verify_command)
    return field,reason,verify_errors


def _request_parent_contract_repair(did, field, reason, request, verify_errors):
    """Enter the existing repair route after deterministic proof only."""

    key=_structured_plan_symbolic_key(did)
    repair_path=Path(PROJECT)/".opencode-v2"/"IMPLEMENTATION_PLAN.repair.json"
    prerequisite_gap=False
    # A missing prerequisite is a bounded repair when the affected parent and
    # its declared producers are known.  Marking this as a whole-plan repair
    # forces the planner's protocol to rewrite every leaf, which needlessly
    # risks deliverable-ID drift and can exhaust its context before any edit.
    # Preserve the source-list order so a targeted planner may edit only these
    # contracts while retaining the stable Dxxx mapping.
    repair_keys=[key]
    if prerequisite_gap:
        try:
            structured=load_json_object(
                structured_plan_path(),label="structured plan",
            )
            raw_leaves=structured.get("leaves")
            target=next(
                (
                    item for item in raw_leaves
                    if isinstance(item,dict) and item.get("key")==key
                ),
                None,
            ) if isinstance(raw_leaves,list) else None
            if not isinstance(target,dict):
                raise StateCorruptionError(
                    f"parent-contract repair key is missing from structured plan: {key}"
                )
            direct_deps=[]
            for dep_field in ("launch_deps","contract_deps","verify_deps"):
                for dep in target.get(dep_field,[]) or []:
                    if isinstance(dep,str) and dep and dep not in direct_deps:
                        direct_deps.append(dep)
            source_order=[
                item.get("key") for item in raw_leaves
                if isinstance(item,dict) and isinstance(item.get("key"),str)
            ]
            repair_keys=[
                candidate for candidate in source_order
                if candidate==key or candidate in direct_deps
            ]
            if key not in repair_keys:
                repair_keys.append(key)
        except (OSError,ValueError,TypeError,StopIteration) as exc:
            raise StateCorruptionError(
                f"cannot derive bounded prerequisite repair keys for {key}"
            ) from exc
    repair_message=(
        f"{key}: runtime split recovery found the parent verify_command "
        f"internally inconsistent or non-verifying. Repair only this leaf's "
        f"verify_command without weakening Outcome/Done when/Acceptance. "
        f"Evidence: {reason}"
        if not prerequisite_gap else
        f"{key}: runtime split handoff proved that required prerequisite artifacts "
        f"are absent and no executable parent child owns permission to create them. "
        f"Repair the structured plan so bounded producer leaves own and verify the "
        f"missing artifacts before this consumer runs. Preserve all acceptance "
        f"requirements; do not fabricate fixture data merely to satisfy a check. "
        f"Evidence: {reason}"
    )
    atomic_write_json(repair_path,{
        "protocol":"v2-structured-plan-repair-v1",
        "source":"runtime-split-parent-contract",
        "whole_plan":False,
        "affected_keys":repair_keys,
        "errors":[{
            "key":key,
            "code":(
                "runtime-parent-prerequisite-artifacts-missing"
                if prerequisite_gap else "runtime-parent-verify-invalid"
            ),
            "message":repair_message,
        }],
        "baseline":_repair_baseline(repair_keys),
    })
    (Path(PROJECT)/".opencode-v2"/"IMPLEMENTATION_PLAN.ready").unlink(missing_ok=True)

    with dispatch_lock:
        with attempt_lock():
            data=load_attempts()
            entry=(data.get("deliverables") or {}).get(did)
            if not isinstance(entry,dict):
                raise ValueError("missing attempt ledger entry for parent repair")
            changed=False
            marker=entry.get("split_required")
            try:
                prior_generation=int(
                    (marker.get("generation") if isinstance(marker,dict) else None)
                    or request.get("generation")
                    or 1
                )
            except (TypeError,ValueError):
                raise StateCorruptionError(
                    f"{did} split generation is invalid during parent contract repair"
                )
            for item in entry.get("failure_history",[]) or []:
                if (
                    isinstance(item,dict)
                    and item.get("classification")=="genuine"
                    and (
                        prerequisite_gap
                        or _split_failure_is_verification_related(item.get("reason"))
                    )
                ):
                    item["classification"]="bad-plan"
                    item["reclassified_by"]="runtime-parent-contract-repair"
                    changed=True
            entry["split_rearm_after_contract_repair"]={
                "generation":max(1,prior_generation),
                "field":field,
                "structured_key":key,
                "timestamp":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime()),
            }
            entry.pop("parent_contract_repair_resolution",None)
            changed=True
            if "split_required" in entry:
                entry.pop("split_required",None)
            if changed:
                save_attempts(data)

    _archive_split_state_for_contract_repair(did)
    _clear_split_request_state_for_contract_repair(did)
    _detach_split_overlay_for_contract_repair(did)
    log(
        f"PARENT_CONTRACT_REPAIR_REQUESTED deliverable={did} key={key} "
        f"field={field} reason={reason}"
    )
    csv(
        "PARENT_CONTRACT_REPAIR_REQUESTED","", "task-splitter",
        f"{did} key={key} field={field}"
    )
    return True,"parent-contract-repair"


# This is deliberately a correction of an unavailable conclusion, not a
# semantic decomposition hint.  The canonical request remains the full source
# of facts and the child retains its original model/profile and no-tool policy.
SPLITTER_FALSE_PARENT_INVALID_CONTEXT=(
    "The supervisor independently validated that the parent contract's exact "
    "Verify command is structurally valid. `parent-contract-invalid` is not "
    "available for this claim. Missing required artifacts are unfinished "
    "failing work and may be assigned to split children. Emit a valid bare "
    "split proposal under the existing schema only."
)


def splitter_corrective_context(reason):
    """Return only the deterministic defect, never a semantic solution."""
    if reason=="parent-contract-invalid-unavailable":
        return SPLITTER_FALSE_PARENT_INVALID_CONTEXT
    return (
        f"Your previous response was deterministically invalid: {reason}. "
        "Emit only one valid bare JSON object conforming to the existing splitter schema."
    )


def _splitter_response_fingerprint(text):
    raw=str(text or "").strip()
    payload=parse_splitter_final_json(raw)
    canonical=(
        json.dumps(payload,sort_keys=True,separators=(",",":"))
        if isinstance(payload,dict) else raw
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _false_parent_contract_invalid(did,payload,request):
    """True only for a well-formed false Verify assertion eligible to correct."""
    try:
        field,_,verify_errors=_validate_parent_contract_invalid_payload(
            did,payload,request,
        )
    except (ValueError,KeyError,TypeError):
        return False
    return field=="verify_command" and not verify_errors


def _archive_corrective_split_proposal(did,claim_count):
    path=split_proposal_path(did)
    if not path.exists():
        return ""
    archived=path.with_name(
        f"{did}.split-proposal.corrective-rejected-{claim_count}.json"
    )
    if archived.exists():
        archived.unlink()
    os.replace(path,archived)
    return str(archived.relative_to(Path(PROJECT)))


def _primary_splitter_execution_root(parent):
    ledger=load_json_object(
        Path(PROJECT)/".opencode-v2"/"work"/"stage-a-controller-executions.json",
        default_missing={"executions":{}},label="controller execution ledger",
    )
    for item in (ledger.get("executions") or {}).values():
        action=item.get("action") if isinstance(item,dict) else {}
        if isinstance(action,dict) and action.get("agent")=="task-splitter" and action.get("deliverable")==parent:
            root=str(item.get("root_session") or "")
            if root: return root
    return ""


def splitter_primary_transport_binding(session):
    """Recover the exact technical root and TaskTool part id for one child."""
    if not session:
        raise StateCorruptionError("splitter primary session is missing")
    con=db_connect()
    try:
        row=con.execute(
            "SELECT parent_id FROM session WHERE id=?",
            (session,),
        ).fetchone()
        if not row or not row[0]:
            raise StateCorruptionError(
                f"splitter primary session has no parent: {session}"
            )
        root=str(row[0])
        rows=con.execute(
            "SELECT id,data FROM part WHERE session_id=? ORDER BY time_created",
            (root,),
        ).fetchall()
    finally:
        con.close()
    for part_id,raw in rows:
        try:
            item=json.loads(raw or "{}")
        except Exception:
            continue
        state=item.get("state") if isinstance(item,dict) else {}
        metadata=state.get("metadata") if isinstance(state,dict) else {}
        if (
            isinstance(item,dict)
            and item.get("type")=="tool"
            and item.get("tool")=="task"
            and isinstance(metadata,dict)
            and str(metadata.get("sessionId") or "")==session
        ):
            return root,str(part_id)
    raise StateCorruptionError(
        f"splitter primary TaskTool binding is missing: {session}"
    )


def historical_splitter_validation_reason(parent,payload,request):
    """Recompute the deterministic rejection of one archived splitter output."""
    if not isinstance(payload,dict):
        return "historical splitter payload is not an object"
    if payload.get("protocol")==SPLIT_PARENT_CONTRACT_INVALID_PROTOCOL:
        if _false_parent_contract_invalid(parent,payload,request):
            return "parent-contract-invalid-unavailable"
        try:
            _,_,verify_errors=_validate_parent_contract_invalid_payload(
                parent,payload,request,
            )
        except (ValueError,KeyError,TypeError) as exc:
            return str(exc)
        if verify_errors:
            return ""
        return (
            "parent-contract-invalid verify_command lacks a deterministic "
            "contract defect"
        )
    if (
        payload.get("protocol")!=SPLIT_PROPOSAL_PROTOCOL
        or payload.get("parent_id")!=parent
        or payload.get("depth")!=split_depth(parent)
        or payload.get("generation")!=request.get("generation",1)
    ):
        return "proposal protocol, parent, depth, or generation does not match request"
    try:
        validate_split_proposal(parent,payload.get("proposals"),request=request)
    except (ValueError,KeyError,TypeError) as exc:
        return str(exc)
    return ""


def corrective_splitter_prompt(parent,request,primary_output,reason):
    """Direct context for the only corrective native child of one claim."""
    return (
        f"SPLIT_PARENT: {parent}\nSPLITTER_CORRECTIVE_ORDINAL: 1\n"
        "CANONICAL_SPLIT_REQUEST_JSON_BEGIN\n"
        +json.dumps(request,sort_keys=True,separators=(",",":"))+
        "\nCANONICAL_SPLIT_REQUEST_JSON_END\n"
        "PRIMARY_SPLITTER_RESPONSE_BEGIN\n"+primary_output+
        "\nPRIMARY_SPLITTER_RESPONSE_END\n"
        "DETERMINISTIC_VALIDATION_FAILURE_BEGIN\n"+reason+
        "\nDETERMINISTIC_VALIDATION_FAILURE_END\n"
        "This is the single correction opportunity for the existing logical splitter claim. "
        "The response above was rejected for the exact deterministic reason shown. "
        "Emit only one valid bare JSON object conforming to the splitter schema."
    )


# Keep the corrective launch a fresh native task-splitter child of the same
# technical root, but deliberately omit SubtaskPart.command.  A truthy
# command makes OpenCode resume the v2noop parent after task completion.
def quiesce_transport_root_for_corrective(
    root, settle_seconds=0.05, poll_seconds=0.05, timeout_seconds=2.0
):
    """Stop OpenCode's automatic parent resume before posting the corrective SubtaskPart.

    A completed TaskTool immediately resumes its parent assistant. The
    transport-root has no semantic work to do, and a corrective prompt posted
    while that automatic resume is active is persisted but not executed.
    Sample twice to cover the small completion/resume race; if the root is
    active, interrupt only that current turn and wait until the session leaves
    /session/status. Do not use abort_session(): this is transport quiescing,
    not retirement of the durable technical root.
    """
    try:
        status=http.get_status()
        if root not in status:
            if settle_seconds>0:
                time.sleep(settle_seconds)
            status=http.get_status()
        if root not in status:
            return True,"root-idle"

        if not http.interrupt(root):
            return False,"root-interrupt-failed"

        deadline=time.monotonic()+max(0.0,float(timeout_seconds))
        while True:
            if poll_seconds>0:
                time.sleep(poll_seconds)
            status=http.get_status()
            if root not in status:
                return True,"root-interrupted"
            if time.monotonic()>=deadline:
                return False,"root-interrupt-timeout"
    except Exception as exc:
        return False,f"root-quiesce-error:{exc!r}"


def dispatch_splitter_corrective_turn(root,prompt):
    if not http.ensure(): return False,"http-not-connected"
    if http.mode!="v1": return False,"splitter-corrective-requires-v1-runtime"

    ok,detail=quiesce_transport_root_for_corrective(root)
    if not ok:
        return False,detail

    try:
        query=urllib.parse.urlencode({"directory":PROJECT})
        http.request("POST",f"/session/{urllib.parse.quote(root)}/prompt_async?{query}",
            payload={"agent":"transport-root","model":{"providerID":"v2noop","modelID":"root-noop"},
                     "parts":[{"type":"subtask","prompt":prompt,"description":"Correct bounded split", "agent":"task-splitter"}]},timeout=12)
        return True,f"accepted-after-{detail}"
    except Exception as exc:
        return False,repr(exc)


def begin_splitter_corrective_turn(
    did,session,dispatch_token,reason,raw_output="",root_session="",historical_recovery=None
):
    """Persist and dispatch the sole corrective *native child* of one claim."""
    status=load_split_status(did)
    if int(status.get("corrective_turn_count") or 0) >= 1:
        return False,"splitter-corrective-turn-already-consumed"
    if not session or not dispatch_token:
        return False,"splitter-corrective-turn-missing-session-or-token"
    claim_count=int(status.get("claim_count") or 0)
    archived=_archive_corrective_split_proposal(did,claim_count)
    request=load_json_object(split_request_path(did),label=f"split request {did}")
    root=str(root_session or _primary_splitter_execution_root(did))
    if not root: return False,"splitter-corrective-root-missing"
    primary_path=Path(PROJECT)/".opencode-v2"/"work"/f"{did}.splitter-primary-response-{claim_count}.txt"
    atomic_write_text(primary_path,raw_output)
    context=corrective_splitter_prompt(did,request,raw_output,reason)
    context_sha=hashlib.sha256(context.encode()).hexdigest()
    first_sha=_splitter_response_fingerprint(raw_output)
    audit={}
    if isinstance(historical_recovery,dict):
        audit["historical_corrective_recovery"]=historical_recovery
    # Mark dispatch attempted before POST.  A transport ambiguity must never
    # cause a second corrective POST or silently create a third model turn.
    save_split_status(
        did,"splitter-corrective-awaiting-output",
        claim_count=claim_count,
        dispatch_token=dispatch_token,
        lease_until_epoch=time.time()+SPLITTER_LEASE_SECONDS,
        corrective_turn_count=1,
        corrective_session="",
        corrective_dispatch_token="",
        corrective_first_output_sha256=first_sha,
        corrective_archive=archived,
        corrective_context_sha256=context_sha,
        corrective_execution_id=hashlib.sha256(json.dumps({"parent":did,"claim":claim_count,"root":root,"primary":session,"reason":hashlib.sha256(reason.encode()).hexdigest()},sort_keys=True).encode()).hexdigest(),
        corrective_root_session=root,
        corrective_reason_sha256=hashlib.sha256(reason.encode()).hexdigest(),
        corrective_primary_response=str(primary_path.relative_to(Path(PROJECT))),
        corrective_dispatch_state="intent-persisted",
        reason=f"invalid splitter response; one bounded corrective turn: {reason}"[:1000],
        **audit,
    )
    ok,detail=dispatch_splitter_corrective_turn(root,context)
    if ok:
        save_split_status(
            did,"splitter-corrective-awaiting-output",
            claim_count=claim_count,
            dispatch_token=dispatch_token,
            lease_until_epoch=time.time()+SPLITTER_LEASE_SECONDS,
            corrective_dispatch_state="post-accepted",
        )
        log(
            f"SPLITTER_CORRECTIVE_CHILD_POST_ACCEPTED parent={did} root={root} "
            f"claim={claim_count}"
        )
        return True,"splitter-corrective-turn-pending"
    _,state=record_splitter_failure(
        did,f"splitter corrective turn dispatch failed: {detail}",
        session=session,validation=False,
    )
    log(
        f"SPLITTER_CORRECTIVE_TURN_DISPATCH_FAILED parent={did} "
        f"session={session} state={state} detail={detail}"
    )
    return False,state


def recover_historical_splitter_corrective_turn(parent):
    """Use the sole corrective turn for a preserved pre-policy failed claim."""
    status=load_split_status(parent)
    if status.get("state")!="split-validation-failed":
        return False,"historical-corrective-requires-split-validation-failed"
    if int(status.get("corrective_turn_count") or 0)>=1:
        return False,"splitter-corrective-turn-already-consumed"
    if leaf_children(parent):
        return False,"historical-corrective-parent-already-split"
    claim_count=int(status.get("claim_count") or 0)
    if claim_count<1:
        return False,"historical-corrective-claim-missing"

    session=str(status.get("session") or "")
    archive_rel=str(status.get("archived_proposal") or "")
    if not session or not archive_rel:
        return False,"historical-corrective-provenance-missing"

    project_root=Path(PROJECT).resolve()
    work_root=(project_root/".opencode-v2"/"work").resolve()
    archive=(project_root/archive_rel).resolve()
    try:
        archive.relative_to(work_root)
    except ValueError:
        return False,"historical-corrective-archive-outside-work"
    if not archive.is_file() or archive.is_symlink():
        return False,"historical-corrective-archive-missing"

    archived=load_json_object(
        archive,label=f"historical split proposal {parent}"
    )
    raw_output=last_assistant_text_db(session)
    payload=parse_splitter_final_json(raw_output)
    if not isinstance(payload,dict) or payload!=archived:
        return False,"historical-corrective-response-mismatch"

    request=load_json_object(
        split_request_path(parent),label=f"split request {parent}"
    )
    reason=historical_splitter_validation_reason(parent,payload,request)
    if not reason:
        return False,"historical-corrective-payload-no-longer-rejected"
    recorded_reason=str(status.get("reason") or "")
    if recorded_reason and recorded_reason!=reason:
        return False,"historical-corrective-rejection-drift"

    root,dispatch_token=splitter_primary_transport_binding(session)
    ledger=load_json_object(
        project_root/".opencode-v2"/"work"/"stage-a-controller-executions.json",
        default_missing={"executions":{}},label="controller execution ledger",
    )
    generation=int(status.get("generation") or 1)
    bound=False
    for item in (ledger.get("executions") or {}).values():
        action=item.get("action") if isinstance(item,dict) else {}
        if (
            isinstance(action,dict)
            and action.get("agent")=="task-splitter"
            and action.get("deliverable")==parent
            and int(action.get("generation") or 1)==generation
            and str(item.get("root_session") or "")==root
        ):
            bound=True
            break
    if not bound:
        return False,"historical-corrective-controller-intent-missing"

    recovery={
        "protocol":"v2-historical-splitter-corrective-recovery-v1",
        "claim_count":claim_count,
        "generation":generation,
        "primary_session":session,
        "primary_dispatch_token":dispatch_token,
        "primary_root_session":root,
        "archived_proposal":archive_rel,
        "archived_proposal_sha256":hashlib.sha256(archive.read_bytes()).hexdigest(),
        "primary_response_sha256":_splitter_response_fingerprint(raw_output),
        "deterministic_rejection":reason,
        "authorized_at":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime()),
    }
    ok,detail=begin_splitter_corrective_turn(
        parent,session,dispatch_token,reason,raw_output,
        root_session=root,historical_recovery=recovery,
    )
    if ok:
        log(
            f"SPLITTER_HISTORICAL_CORRECTIVE_RECOVERY parent={parent} "
            f"claim={claim_count} session={session} token={dispatch_token}"
        )
    return ok,detail


def recover_exhausted_splitter_deterministic_handoff(parent):
    """Deterministically decompose a verifier-failed parent after splitter exhaustion.

    This is not another model claim.  It is available only after both ordinary
    splitter claims and the sole corrective child have completed without a valid
    proposal.  The supervisor creates the already-supported progress-handoff ->
    writer shape from the current canonical parent contract, preserving the
    exact parent Verify and all ownership.
    """
    if not valid_deliverable_id(parent):
        return False,"invalid-parent"
    status=load_split_status(parent)
    if status.get("state")!="split-validation-failed":
        return False,"deterministic-fallback-requires-split-validation-failed"
    if int(status.get("claim_count") or 0) < MAX_SPLITTER_ATTEMPTS:
        return False,"deterministic-fallback-requires-exhausted-claims"
    if int(status.get("corrective_turn_count") or 0) < 1:
        return False,"deterministic-fallback-requires-corrective-turn"
    if status.get("corrective_dispatch_state")!="native-child-completed":
        return False,"deterministic-fallback-corrective-not-complete"
    if leaf_children(parent):
        return False,"deterministic-fallback-parent-already-split"

    request=load_json_object(
        split_request_path(parent),label=f"split request {parent}"
    )
    if not _split_request_verification_recovery_allowed(request):
        return False,"deterministic-fallback-requires-verify-failure"

    manifest=load_manifest()
    leaf=(manifest.get("leaves") or {}).get(parent)
    if not isinstance(leaf,dict):
        return False,"deterministic-fallback-parent-missing"
    owned=owned_artifact_paths(leaf)
    if not owned:
        return False,"deterministic-fallback-parent-has-no-ownership"
    role=str(leaf.get("role") or "")
    if role not in IMPLEMENTATION_AGENTS:
        return False,"deterministic-fallback-parent-role-unsupported"
    verify=str(leaf.get("verify_command") or "").strip()
    done_when=str(leaf.get("done_when") or "").strip()
    if not verify or not done_when:
        return False,"deterministic-fallback-parent-contract-incomplete"

    contract=request.get("parent_contract")
    if not isinstance(contract,dict):
        return False,"deterministic-fallback-request-contract-missing"
    request_owned,request_owned_error=_strict_owned_artifact_text(
        str(contract.get("owned_artifacts") or "")
    )
    if (
        request_owned_error
        or request_owned!=owned
        or str(contract.get("verify_command") or "").strip()!=verify
        or str(contract.get("done_when") or "").strip()!=done_when
    ):
        return False,"deterministic-fallback-request-contract-stale"

    evidence=[
        row for row in request.get("supervisor_verify_evidence",[])
        if isinstance(row,dict)
        and row.get("executed") is True
        and str(row.get("command") or "").strip()==verify
        and _split_failure_is_verification_related(row.get("result"))
    ]
    if not evidence:
        return False,"deterministic-fallback-exact-verify-evidence-missing"

    existing=[rel for rel in owned if (Path(PROJECT)/rel).exists()]
    proposals=[
        {
            "scope":(
                "Diagnose the exact failed parent Verify from authoritative "
                "supervisor evidence and current parent-owned artifacts; record "
                "the concrete failure cause and smallest writer repair delta."
            ),
            "owned_artifacts":"none",
            "verify_command":SPLIT_HANDOFF_VERIFY_SENTINEL,
            "role":"probe-builder",
            "depends_on_sibling":"",
            "done_when":"A durable HANDOFF_READY progress record identifies the exact repair delta.",
            "reads_existing":existing,
            "creates_or_updates":[],
        },
        {
            "scope":(
                "Consume the predecessor handoff and repair the parent-owned "
                "artifacts until the exact inherited parent Verify passes."
            ),
            "owned_artifacts":_canonical_owned_artifacts(owned),
            "verify_command":verify,
            "role":role,
            "depends_on_sibling":"first",
            "done_when":done_when,
            "reads_existing":existing,
            "creates_or_updates":owned,
        },
    ]
    children=persist_split(parent,proposals)
    fallback={
        "protocol":"v2-deterministic-splitter-fallback-v1",
        "claim_count":int(status.get("claim_count") or 0),
        "corrective_turn_count":int(status.get("corrective_turn_count") or 0),
        "source_state":"split-validation-failed",
        "verify_sha256":hashlib.sha256(verify.encode()).hexdigest(),
        "children":children,
        "created_at":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime()),
    }
    save_split_status(
        parent,"accepted",
        children=children,
        deterministic_splitter_fallback=fallback,
    )
    log(
        f"DETERMINISTIC_SPLITTER_FALLBACK parent={parent} "
        f"children={','.join(children)} claim_count={fallback['claim_count']}"
    )
    return True,"accepted"


DENIED_TOOL_FINALIZE_RECOVERY_PROTOCOL="v2-denied-tool-finalize-recovery-v1"


def recover_denied_tool_finalize(did):
    """Re-run finalization for one terminal attempt poisoned only by denied tools.

    The historical attempt/failure rows remain untouched.  Recovery is allowed
    only when the current terminal attempt was recorded as
    sandbox-ownership-violation, every sandbox audit row is a pre-execution
    denial, and this same attempt durably changed owned/progress state.
    """
    if not valid_deliverable_id(did):
        return False,"invalid-deliverable"
    if ready_info(did):
        return True,"already-finalized"

    project=Path(PROJECT)
    leaf=(load_manifest().get("leaves") or {}).get(did)
    if not isinstance(leaf,dict) or leaf.get("split_children"):
        return False,"denied-tool-recovery-requires-executable-leaf"

    with dispatch_lock:
        with attempt_lock():
            data=load_attempts()
            entry=(data.get("deliverables") or {}).get(did)
            if not isinstance(entry,dict):
                return False,"denied-tool-recovery-missing-ledger-entry"
            state=attempt_state(entry)
            if not state.get("valid"):
                return False,"denied-tool-recovery-invalid-attempt-ledger"
            count=int(entry.get("count") or 0)
            sessions=entry.get("sessions")
            if (
                count<1 or not isinstance(sessions,list)
                or len(sessions)!=count or not isinstance(sessions[-1],str)
                or not sessions[-1]
            ):
                return False,"denied-tool-recovery-session-mismatch"
            sid=sessions[-1]
            failures=[
                item for item in (entry.get("failure_history") or [])
                if isinstance(item,dict) and int(item.get("attempt") or 0)==count
            ]
            if len(failures)!=1:
                return False,"denied-tool-recovery-terminal-failure-missing"
            failure=failures[0]
            if (
                failure.get("classification")!="genuine"
                or failure.get("reason")!="sandbox-ownership-violation"
            ):
                return False,"denied-tool-recovery-terminal-failure-mismatch"

    if not worker_sandbox_has_only_denied_preexecution_violations(
        project,did,sid
    ):
        return False,"denied-tool-recovery-audit-not-denied-only"
    if not durable_worker_execution(did,sid):
        return False,"denied-tool-recovery-no-durable-execution"

    violation=worker_sandbox_violation_path(project,did,sid)
    try:
        violation_sha=hashlib.sha256(violation.read_bytes()).hexdigest()
    except OSError:
        return False,"denied-tool-recovery-audit-unreadable"

    with dispatch_lock:
        with attempt_lock():
            data=load_attempts()
            entry=(data.get("deliverables") or {}).get(did)
            if not isinstance(entry,dict):
                return False,"denied-tool-recovery-missing-ledger-entry"
            if (
                int(entry.get("count") or 0)!=count
                or (entry.get("sessions") or [])[-1:]!=[sid]
            ):
                return False,"denied-tool-recovery-ledger-changed"
            existing=entry.get("denied_tool_finalize_recovery")
            if isinstance(existing,dict):
                if (
                    existing.get("protocol")!=DENIED_TOOL_FINALIZE_RECOVERY_PROTOCOL
                    or int(existing.get("attempt") or 0)!=count
                    or existing.get("session")!=sid
                    or existing.get("violation_sha256")!=violation_sha
                ):
                    return False,"denied-tool-recovery-conflicting-marker"
                if existing.get("state")=="finalized" and ready_info(did):
                    return True,"already-finalized"
                if existing.get("state") not in {"prepared","failed"}:
                    return False,"denied-tool-recovery-marker-invalid"
            else:
                entry["denied_tool_finalize_recovery"]={
                    "protocol":DENIED_TOOL_FINALIZE_RECOVERY_PROTOCOL,
                    "state":"prepared",
                    "attempt":count,
                    "session":sid,
                    "violation_sha256":violation_sha,
                    "historical_failure":{
                        "classification":failure.get("classification"),
                        "reason":failure.get("reason"),
                        "timestamp":failure.get("timestamp"),
                    },
                    "prepared_at":time.strftime(
                        "%Y-%m-%dT%H:%M:%SZ",time.gmtime()
                    ),
                }
                save_attempts(data)

    ok,detail=post_session_finalize(did,sid)

    with dispatch_lock:
        with attempt_lock():
            data=load_attempts()
            entry=(data.get("deliverables") or {}).get(did)
            marker=(
                entry.get("denied_tool_finalize_recovery")
                if isinstance(entry,dict) else None
            )
            if isinstance(marker,dict):
                marker["state"]="finalized" if ok else "failed"
                marker["result"]=detail
                marker["finished_at"]=time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ",time.gmtime()
                )
                save_attempts(data)

    event=(
        "DENIED_TOOL_FINALIZE_RECOVERED"
        if ok else "DENIED_TOOL_FINALIZE_RECOVERY_FAILED"
    )
    log(
        f"{event} session={sid} deliverable={did} "
        f"attempt={count} result={detail}"
    )
    csv(event,sid,"supervisor",f"{did} attempt={count} result={detail}")
    return ok,detail



FALSE_OWNERSHIP_FINALIZE_RECOVERY_PROTOCOL=(
    "v2-false-concurrent-ownership-finalize-recovery-v1"
)


def recover_false_ownership_finalize(did):
    """Re-run finalization when a terminal ownership failure is now disproven.

    Historical failure rows remain intact. Recovery is allowed only for the
    current terminal attempt, only when its recorded reason is ownership-based,
    the worker made durable progress, no fatal sandbox violation exists, and
    the current attribution logic finds zero ownership violations for that
    exact session. An unstarted split-required state may be retired only after
    successful finalization.
    """
    if not valid_deliverable_id(did):
        return False,"invalid-deliverable"
    if ready_info(did):
        return True,"already-finalized"

    project=Path(PROJECT)
    leaf=(load_manifest().get("leaves") or {}).get(did)
    if not isinstance(leaf,dict) or leaf.get("split_children"):
        return False,"false-ownership-recovery-requires-executable-leaf"

    with dispatch_lock:
        with attempt_lock():
            data=load_attempts()
            entry=(data.get("deliverables") or {}).get(did)
            if not isinstance(entry,dict):
                return False,"false-ownership-recovery-missing-ledger-entry"
            state=attempt_state(entry)
            if not state.get("valid"):
                return False,"false-ownership-recovery-invalid-attempt-ledger"
            count=int(entry.get("count") or 0)
            sessions=entry.get("sessions")
            if (
                count<1 or not isinstance(sessions,list)
                or len(sessions)!=count or not isinstance(sessions[-1],str)
                or not sessions[-1] or sessions[-1].startswith("dispatch:")
            ):
                return False,"false-ownership-recovery-session-mismatch"
            sid=sessions[-1]
            failures=[
                item for item in (entry.get("failure_history") or [])
                if isinstance(item,dict) and int(item.get("attempt") or 0)==count
            ]
            if len(failures)!=1:
                return False,"false-ownership-recovery-terminal-failure-missing"
            failure=failures[0]
            reason=str(failure.get("reason") or "")
            if (
                failure.get("classification")!="genuine"
                or not reason.startswith("ownership-violation:")
            ):
                return False,"false-ownership-recovery-terminal-failure-mismatch"

            split_status=load_split_status(did)
            split_state=str(split_status.get("state") or "")
            if split_state and split_state!="split-required":
                return False,"false-ownership-recovery-split-already-started"
            if split_proposal_path(did).exists() or split_transaction_path(did).exists():
                return False,"false-ownership-recovery-split-already-started"

            existing=entry.get("false_ownership_finalize_recovery")
            if isinstance(existing,dict):
                if (
                    existing.get("protocol")!=FALSE_OWNERSHIP_FINALIZE_RECOVERY_PROTOCOL
                    or int(existing.get("attempt") or 0)!=count
                    or existing.get("session")!=sid
                    or existing.get("historical_reason")!=reason
                ):
                    return False,"false-ownership-recovery-conflicting-marker"
                if existing.get("state")=="finalized" and ready_info(did):
                    return True,"already-finalized"
                if existing.get("state") not in {"prepared","failed"}:
                    return False,"false-ownership-recovery-marker-invalid"

    status=v1_session_status_snapshot().get(sid)
    if isinstance(status,dict) and str(status.get("type") or "").lower()=="busy":
        return False,"false-ownership-recovery-session-still-active"
    if worker_sandbox_has_fatal_violation(project,did,sid):
        return False,"false-ownership-recovery-fatal-sandbox-violation"
    if not durable_worker_execution(did,sid):
        return False,"false-ownership-recovery-no-durable-execution"

    violations=ownership_violations(did,sid)
    if violations:
        return False,"false-ownership-recovery-still-violating:"+",".join(violations[:4])

    baseline=ownership_baseline_path(did)
    try:
        baseline_sha=hashlib.sha256(baseline.read_bytes()).hexdigest()
    except OSError:
        return False,"false-ownership-recovery-baseline-unreadable"

    with dispatch_lock:
        with attempt_lock():
            data=load_attempts()
            entry=(data.get("deliverables") or {}).get(did)
            if not isinstance(entry,dict):
                return False,"false-ownership-recovery-missing-ledger-entry"
            if (
                int(entry.get("count") or 0)!=count
                or (entry.get("sessions") or [])[-1:]!=[sid]
            ):
                return False,"false-ownership-recovery-ledger-changed"
            existing=entry.get("false_ownership_finalize_recovery")
            if not isinstance(existing,dict):
                entry["false_ownership_finalize_recovery"]={
                    "protocol":FALSE_OWNERSHIP_FINALIZE_RECOVERY_PROTOCOL,
                    "state":"prepared",
                    "attempt":count,
                    "session":sid,
                    "historical_reason":reason,
                    "ownership_baseline_sha256":baseline_sha,
                    "prepared_at":time.strftime(
                        "%Y-%m-%dT%H:%M:%SZ",time.gmtime()
                    ),
                }
                save_attempts(data)

    ok,detail=post_session_finalize(did,sid)

    if ok:
        try:
            if split_request_path(did).exists() or split_status_path(did).exists():
                _archive_split_state_for_contract_repair(did)
            _clear_split_request_state_for_contract_repair(did)
            splitter_lock_path(did).unlink(missing_ok=True)
        except Exception as exc:
            ok=False
            detail=f"false-ownership-recovery-split-clear-error-{type(exc).__name__}"

    with dispatch_lock:
        with attempt_lock():
            data=load_attempts()
            entry=(data.get("deliverables") or {}).get(did)
            marker=(
                entry.get("false_ownership_finalize_recovery")
                if isinstance(entry,dict) else None
            )
            if isinstance(marker,dict):
                marker["state"]="finalized" if ok else "failed"
                marker["result"]=detail
                marker["finished_at"]=time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ",time.gmtime()
                )
                if ok:
                    entry.pop("split_required",None)
                save_attempts(data)

    event=(
        "FALSE_OWNERSHIP_FINALIZE_RECOVERED"
        if ok else "FALSE_OWNERSHIP_FINALIZE_RECOVERY_FAILED"
    )
    log(
        f"{event} session={sid} deliverable={did} "
        f"attempt={count} result={detail}"
    )
    csv(event,sid,"supervisor",f"{did} attempt={count} result={detail}")
    return ok,detail


VERSION_SKEW_ZERO_WORK_RECOVERY_PROTOCOL=(
    "v2-version-skew-zero-work-dispatch-recovery-v1"
)


def recover_version_skew_zero_work_dispatch(did):
    """Rearm the same dispatch sequence after a proven supervisor version skew."""
    if not valid_deliverable_id(did):
        return False,"invalid-deliverable"
    if ready_info(did):
        return True,"already-complete"

    project=Path(PROJECT)
    with dispatch_lock:
        with attempt_lock():
            data=load_attempts()
            entry=(data.get("deliverables") or {}).get(did)
            if not isinstance(entry,dict):
                return False,"version-skew-recovery-missing-ledger-entry"
            state=attempt_state(entry)
            if not state.get("valid"):
                return False,"version-skew-recovery-invalid-ledger"
            count=int(entry.get("count") or 0)
            sessions=entry.get("sessions")
            if (
                count<1 or not isinstance(sessions,list)
                or len(sessions)!=count or not isinstance(sessions[-1],str)
                or not sessions[-1] or sessions[-1].startswith("dispatch:")
            ):
                return False,"version-skew-recovery-current-session-mismatch"
            sid=sessions[-1]
            classified={
                int(item.get("attempt") or 0)
                for item in (entry.get("failure_history") or [])
                if isinstance(item,dict)
            }
            if count in classified:
                return False,"version-skew-recovery-attempt-already-classified"
            if count < int(state.get("allowed_attempts") or 0):
                return False,"version-skew-recovery-not-terminal"
            prior=entry.get("version_skew_zero_work_recoveries") or []
            if not isinstance(prior,list):
                return False,"version-skew-recovery-history-invalid"
            if any(
                isinstance(item,dict)
                and int(item.get("attempt") or 0)==count
                for item in prior
            ):
                return False,"version-skew-recovery-already-used"

    if meaningful_worker_execution(sid,did):
        return False,"version-skew-recovery-meaningful-execution-present"
    if persisted_completed_tool_turns(sid):
        return False,"version-skew-recovery-tool-execution-present"

    statuses=v1_session_status_snapshot()
    status=statuses.get(sid)
    if isinstance(status,dict) and str(status.get("type") or "").lower()=="busy":
        return False,"version-skew-recovery-session-still-active"

    agent=_session_agent_db(sid)
    if agent not in IMPLEMENTATION_AGENTS:
        return False,"version-skew-recovery-agent-invalid"
    prompt=first_user_text_db(sid)
    if not prompt:
        return False,"version-skew-recovery-prompt-missing"
    did_from_prompt,violation=validate_dispatch(agent,prompt,runtime=True)
    if did_from_prompt!=did or violation:
        return False,"version-skew-recovery-current-prompt-not-canonical"

    try:
        event_text=LOG.read_text(errors="replace")
    except OSError:
        return False,"version-skew-recovery-event-log-missing"
    deny=(
        f"DISPATCH_DENY session={sid} agent={agent} "
        "noncanonical_runtime_handoff"
    )
    allow=(
        f"DISPATCH_ALLOW session={sid} agent={agent} "
        f"deliverable={did} attempt={count}"
    )
    deny_pos=event_text.find(deny)
    allow_pos=event_text.find(allow)
    if deny_pos<0 or allow_pos<0 or deny_pos>=allow_pos:
        return False,"version-skew-recovery-deny-allow-proof-missing"

    prompt_sha=hashlib.sha256(prompt.encode()).hexdigest()
    marker_id=hashlib.sha256(
        f"{did}\0{count}\0{sid}\0{prompt_sha}".encode()
    ).hexdigest()[:20]
    placeholder=f"dispatch:version-skew-recovery:{marker_id}:{did}"

    with dispatch_lock:
        with attempt_lock():
            data=load_attempts()
            entry=(data.get("deliverables") or {}).get(did)
            if not isinstance(entry,dict):
                return False,"version-skew-recovery-ledger-disappeared"
            if (
                int(entry.get("count") or 0)!=count
                or (entry.get("sessions") or [])[-1:]!=[sid]
            ):
                return False,"version-skew-recovery-ledger-changed"
            history=entry.setdefault("version_skew_zero_work_recoveries",[])
            history.append({
                "protocol":VERSION_SKEW_ZERO_WORK_RECOVERY_PROTOCOL,
                "attempt":count,
                "session":sid,
                "replacement":placeholder,
                "prompt_sha256":prompt_sha,
                "evidence":{
                    "deny":"noncanonical_runtime_handoff",
                    "allow":"current-code materialization accepted same session",
                    "meaningful_execution":False,
                    "completed_tool_turns":0,
                },
                "timestamp":time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ",time.gmtime()
                ),
            })
            entry["sessions"][-1]=placeholder
            entry["unmaterialized_dispatch_sequence"]=count
            entry["unmaterialized_dispatch_replays"]=0
            entry.setdefault("unmaterialized_dispatch_history",[]).append({
                "sequence":count,
                "replaced":sid,
                "replacement":placeholder,
                "source":"supervisor-version-skew-recovery",
                "timestamp":time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ",time.gmtime()
                ),
            })
            projected=attempt_state(entry)
            if not projected.get("valid"):
                return False,"version-skew-recovery-projected-ledger-invalid"
            if not projected.get("unmaterialized_dispatch_reusable"):
                return False,"version-skew-recovery-not-reusable"
            save_attempts(data)

    log(
        f"VERSION_SKEW_ZERO_WORK_RECOVERY deliverable={did} "
        f"attempt={count} session={sid} placeholder={placeholder}"
    )
    csv(
        "VERSION_SKEW_ZERO_WORK_RECOVERY",sid,"supervisor",
        f"{did} attempt={count} placeholder={placeholder}",
    )
    return True,"rearmed-same-attempt"


def persisted_model_bash_commands(sid,agent):
    """Recover model-supplied bash text from old plugin-persisted wrappers."""
    if not sid or agent not in IMPLEMENTATION_AGENTS:
        return []
    prefix=[
        "python3",str((ROOT/"scripts"/"worker_sandbox.py").resolve()),
        "run-bash","--project",str(Path(PROJECT).resolve()),
        "--session",sid,"--agent",agent,"--command-b64",
    ]
    out=[]
    try:
        records=_v1_message_records(sid)
    except Exception:
        return out
    for record in records:
        if record.get("data",{}).get("role")!="assistant":
            continue
        for part in _v1_message_parts(record["id"]):
            if part.get("type")!="tool" or part.get("tool") not in {"bash","shell"}:
                continue
            state=part.get("state") if isinstance(part.get("state"),dict) else {}
            raw=state.get("input")
            if isinstance(raw,dict):
                args=raw
            elif isinstance(raw,str):
                try:
                    args=json.loads(raw)
                except Exception:
                    args={}
            else:
                args={}
            command=args.get("command") if isinstance(args,dict) else None
            if not isinstance(command,str) or not command:
                continue
            try:
                parts=shlex.split(command,posix=True)
            except ValueError:
                continue
            if len(parts)!=len(prefix)+1 or parts[:-1]!=prefix:
                continue
            try:
                original=base64.b64decode(parts[-1],validate=True).decode("utf-8")
            except Exception:
                continue
            out.append({
                "command":original,
                "status":str(state.get("status") or ""),
                "output":str(state.get("output") or ""),
                "error":str(state.get("error") or ""),
            })
    return out


def sandbox_wrapper_history_evidence(sid,did):
    agent=_session_agent_db(sid)
    if agent not in IMPLEMENTATION_AGENTS:
        return {}
    commands=persisted_model_bash_commands(sid,agent)
    manual=[
        item for item in commands
        if worker_command_invokes_manual_sandbox_wrapper(item["command"])
    ]
    failure_re=re.compile(
        r"(?:unexpected EOF|can't open file|malformed canonical sandbox wrapper|"
        r"manual sandbox wrapper forbidden)",
        re.IGNORECASE,
    )
    failed=[
        item for item in manual
        if item.get("status")=="error"
        or failure_re.search(
            str(item.get("error") or "")+" "+str(item.get("output") or "")
        )
    ]
    last=last_assistant_text_db(sid)
    return {
        "agent":agent,
        "persisted_wrapper_calls":len(commands),
        "manual_wrapper_calls":len(manual),
        "manual_wrapper_failures":len(failed),
        "maximum_steps_reached":bool(
            re.search(r"maximum steps reached",last or "",re.IGNORECASE)
        ),
        "completed_tool_turns":persisted_completed_tool_turns(sid),
        "meaningful_execution":bool(meaningful_worker_execution(sid,did)),
    }


SANDBOX_WRAPPER_HISTORY_RECOVERY_PROTOCOL=(
    "v2-sandbox-wrapper-history-recovery-v1"
)


def recover_sandbox_wrapper_history_poison(did):
    if not valid_deliverable_id(did):
        return False,"invalid-deliverable"
    if ready_info(did):
        return True,"already-complete"

    with dispatch_lock:
        with attempt_lock():
            data=load_attempts()
            entry=(data.get("deliverables") or {}).get(did)
            if not isinstance(entry,dict):
                return False,"wrapper-history-recovery-missing-ledger-entry"
            state=attempt_state(entry)
            if not state.get("valid"):
                return False,"wrapper-history-recovery-invalid-ledger"
            count=int(entry.get("count") or 0)
            sessions=entry.get("sessions")
            if (
                count<1 or not isinstance(sessions,list)
                or len(sessions)!=count or not isinstance(sessions[-1],str)
            ):
                return False,"wrapper-history-recovery-session-mismatch"
            sid=sessions[-1]
            prior=entry.get("sandbox_wrapper_history_recoveries") or []
            if not isinstance(prior,list):
                return False,"wrapper-history-recovery-history-invalid"
            if any(
                isinstance(item,dict)
                and int(item.get("attempt") or 0)==count
                and item.get("session")==sid
                for item in prior
            ):
                return True,"already-recovered"
            failures=[
                item for item in (entry.get("failure_history") or [])
                if isinstance(item,dict)
                and int(item.get("attempt") or 0)==count
            ]
            if len(failures)!=1 or not (
                failures[0].get("classification")=="genuine"
                and failures[0].get("reason")=="verify-failed-1"
            ):
                return False,"wrapper-history-recovery-terminal-failure-mismatch"
            if int(state.get("infrastructure_retry_grants") or 0)>=MAX_INFRASTRUCTURE_RETRY_GRANTS:
                return False,"wrapper-history-recovery-infrastructure-limit"

    status=v1_session_status_snapshot().get(sid)
    if isinstance(status,dict) and str(status.get("type") or "").lower()=="busy":
        return False,"wrapper-history-recovery-session-still-active"
    evidence=sandbox_wrapper_history_evidence(sid,did)
    if int(evidence.get("persisted_wrapper_calls") or 0)<5:
        return False,"wrapper-history-recovery-insufficient-persisted-wrappers"
    if int(evidence.get("manual_wrapper_calls") or 0)<1:
        return False,"wrapper-history-recovery-no-manual-wrapper"
    if int(evidence.get("manual_wrapper_failures") or 0)<1:
        return False,"wrapper-history-recovery-no-wrapper-failure"
    if not evidence.get("maximum_steps_reached"):
        return False,"wrapper-history-recovery-no-max-step-terminal"
    if not evidence.get("meaningful_execution"):
        return False,"wrapper-history-recovery-no-durable-execution"

    verify=load_supervisor_verify_evidence(did).get("latest") or {}
    if not (
        isinstance(verify,dict)
        and int(verify.get("attempt") or 0)==count
        and verify.get("session")==sid
        and verify.get("result")=="verify-failed-1"
        and int(verify.get("exit_code") or -1)==1
    ):
        return False,"wrapper-history-recovery-verify-evidence-mismatch"

    try:
        plugin_text=(
            ROOT/"xdg/config/opencode/plugins/v2-bounded-subagent.js"
        ).read_text(errors="replace")
    except OSError:
        return False,"wrapper-history-recovery-current-plugin-missing"
    legacy_history_fix=(
        "captureOriginalSandboxCommand(event, output);" in plugin_text
        and "restoreOriginalSandboxCommand(event, output);" in plugin_text
    )
    transform_history_fix=all(marker in plugin_text for marker in (
        'function unwrapPersistedSandboxCommand(command, directory)',
        '"experimental.chat.messages.transform": async (_input, output) => {',
        'sanitizeSandboxHistoryForModel(output?.messages, directory);',
    ))
    if not (legacy_history_fix or transform_history_fix):
        return False,"wrapper-history-recovery-current-plugin-unfixed"

    reason=(
        "sandbox-wrapper-history-poison "
        f"persisted={evidence['persisted_wrapper_calls']} "
        f"manual={evidence['manual_wrapper_calls']} "
        f"failed={evidence['manual_wrapper_failures']} maximum_steps_reached"
    )
    with dispatch_lock:
        with attempt_lock():
            data=load_attempts()
            entry=(data.get("deliverables") or {}).get(did)
            if not isinstance(entry,dict):
                return False,"wrapper-history-recovery-ledger-disappeared"
            if (
                int(entry.get("count") or 0)!=count
                or (entry.get("sessions") or [])[-1:]!=[sid]
            ):
                return False,"wrapper-history-recovery-ledger-changed"
            failures=[
                item for item in (entry.get("failure_history") or [])
                if isinstance(item,dict)
                and int(item.get("attempt") or 0)==count
            ]
            if len(failures)!=1 or failures[0].get("classification")!="genuine":
                return False,"wrapper-history-recovery-failure-changed"
            state=attempt_state(entry)
            grants=int(state.get("infrastructure_retry_grants") or 0)
            if grants>=MAX_INFRASTRUCTURE_RETRY_GRANTS:
                return False,"wrapper-history-recovery-infrastructure-limit"

            failure=failures[0]
            failure["original_classification"]="genuine"
            failure["original_reason"]=str(failure.get("reason") or "")
            failure["classification"]="infrastructure"
            failure["reason"]=reason
            failure["reclassified_by"]="runtime-sandbox-wrapper-history-repair"
            entry.setdefault("infrastructure_failures",[]).append({
                "timestamp":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime()),
                "grant":1,
                "source":"supervisor",
                "kind":"runtime-cancel",
                "session":sid,
                "evidence":"durable-partial-state-preserved",
                "reason":reason,
            })
            entry["infrastructure_retry_grants"]=grants+1
            entry.setdefault("sandbox_wrapper_history_recoveries",[]).append({
                "protocol":SANDBOX_WRAPPER_HISTORY_RECOVERY_PROTOCOL,
                "attempt":count,
                "session":sid,
                "persisted_wrapper_calls":int(evidence["persisted_wrapper_calls"]),
                "manual_wrapper_calls":int(evidence["manual_wrapper_calls"]),
                "manual_wrapper_failures":int(evidence["manual_wrapper_failures"]),
                "completed_tool_turns":int(evidence["completed_tool_turns"]),
                "verify_result":"verify-failed-1",
                "timestamp":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime()),
            })
            projected=attempt_state(entry)
            if (
                not projected.get("valid")
                or int(projected.get("infrastructure_retry_grants") or 0)!=grants+1
                or int(projected.get("allowed_attempts") or 0)<=count
            ):
                return False,"wrapper-history-recovery-projected-ledger-invalid"
            save_attempts(data)

    log(
        f"SANDBOX_WRAPPER_HISTORY_RECOVERY deliverable={did} attempt={count} "
        f"session={sid} persisted={evidence['persisted_wrapper_calls']} "
        f"manual={evidence['manual_wrapper_calls']} "
        f"failed={evidence['manual_wrapper_failures']}"
    )
    csv(
        "SANDBOX_WRAPPER_HISTORY_RECOVERY",sid,"supervisor",
        f"{did} attempt={count} persisted={evidence['persisted_wrapper_calls']} "
        f"manual={evidence['manual_wrapper_calls']} "
        f"failed={evidence['manual_wrapper_failures']}",
    )
    return True,"recovered"


CONTEXT_DELIVERY_RECOVERY_PROTOCOL="v2-context-delivery-recovery-v1"


def context_delivery_failure_evidence(sid,did,attempt):
    """Return durable evidence for the compact-context/direct-write trap."""
    result={
        "context_reads":0,
        "truncated_context_reads":0,
        "context_verify_visible":False,
        "progress_read_denials":0,
        "write_required_denials":0,
        "maximum_steps_reached":False,
        "completed_tool_turns":0,
        "progress_baseline":"",
        "context_output_sha256":"",
    }
    if not sid or not did or int(attempt or 0)<1:
        return result

    progress_rel=f".opencode-v2/work/{did}.progress.md"
    context_rel=f".opencode-v2/query/leaves/{did}-context.json"
    context_outputs=[]
    try:
        records=_v1_message_records(sid)
    except Exception:
        records=[]
    for record in records:
        if record.get("data",{}).get("role")!="assistant":
            continue
        for part in _v1_message_parts(record["id"]):
            if part.get("type")!="tool":
                continue
            tool=str(part.get("tool") or "")
            state=part.get("state") if isinstance(part.get("state"),dict) else {}
            raw=state.get("input")
            if isinstance(raw,dict):
                args=raw
            elif isinstance(raw,str):
                try:
                    args=json.loads(raw)
                except Exception:
                    args={}
            else:
                args={}
            error=str(state.get("error") or "")
            output=str(state.get("output") or "")
            if (
                tool=="read"
                and _tool_targets_exact_project_path(
                    tool,args,context_rel
                )
            ):
                result["context_reads"]+=1
                context_outputs.append(output)
                if (
                    "line truncated to 2000 chars" in output
                    and "(End of file - total 1 lines)" in output
                ):
                    result["truncated_context_reads"]+=1
                if '"verify_command"' in output:
                    result["context_verify_visible"]=True
            if "IMPLEMENTATION_WRITE_REQUIRED" in error:
                result["write_required_denials"]+=1
                if (
                    tool=="read"
                    and _tool_targets_exact_project_path(
                        tool,args,progress_rel
                    )
                ):
                    result["progress_read_denials"]+=1

    if context_outputs:
        material="\n".join(context_outputs).encode("utf-8")
        result["context_output_sha256"]=hashlib.sha256(material).hexdigest()

    baseline=execution_baseline_path(did,attempt)
    try:
        data=load_json_object(
            baseline,label=f"execution baseline {did} attempt {attempt}"
        )
        files=data.get("files") if isinstance(data.get("files"),dict) else {}
        value=str(files.get(progress_rel) or "")
        if value.startswith("file:") and len(value)==69:
            result["progress_baseline"]=value
    except Exception:
        pass

    result["maximum_steps_reached"]=bool(
        re.search(
            r"maximum steps for this agent have been reached",
            last_assistant_text_db(sid) or "",
            re.IGNORECASE,
        )
    )
    result["completed_tool_turns"]=persisted_completed_tool_turns(sid)
    return result


def context_delivery_fixes_installed():
    """Require both halves of the repair before granting a replacement."""
    try:
        query_source=(
            ROOT/"scripts"/"control_query_views.py"
        ).read_text(errors="replace")
        supervisor_source=Path(__file__).read_text(errors="replace")
    except OSError:
        return False
    return (
        'json.dumps(payload, sort_keys=True, indent=2)' in query_source
        and "v2-implementation-progress-read-once-v1" in supervisor_source
        and "implementation_direct_write_noncompliance" in supervisor_source
    )


def recover_context_delivery_failure(did):
    """Replace one attempt lost to mechanically hidden authoritative context."""
    if not valid_deliverable_id(did):
        return False,"invalid-deliverable"
    if ready_info(did):
        return True,"already-complete"

    with dispatch_lock:
        with attempt_lock():
            data=load_attempts()
            entry=(data.get("deliverables") or {}).get(did)
            if not isinstance(entry,dict):
                return False,"context-delivery-recovery-missing-ledger-entry"
            state=attempt_state(entry)
            if not state.get("valid"):
                return False,"context-delivery-recovery-invalid-ledger"
            count=int(entry.get("count") or 0)
            sessions=entry.get("sessions")
            if (
                count<1 or not isinstance(sessions,list)
                or len(sessions)!=count
                or not isinstance(sessions[-1],str)
                or not sessions[-1]
            ):
                return False,"context-delivery-recovery-session-mismatch"
            sid=sessions[-1]
            prior=entry.get("context_delivery_recoveries") or []
            if not isinstance(prior,list):
                return False,"context-delivery-recovery-history-invalid"
            if len(prior)>=MAX_CONTEXT_DELIVERY_RETRY_GRANTS:
                if any(
                    isinstance(item,dict)
                    and int(item.get("attempt") or 0)==count
                    and item.get("session")==sid
                    for item in prior
                ):
                    return True,"already-recovered"
                return False,"context-delivery-recovery-limit"
            failures=[
                item for item in (entry.get("failure_history") or [])
                if isinstance(item,dict)
                and int(item.get("attempt") or 0)==count
            ]
            if len(failures)!=1 or not (
                failures[0].get("classification")=="genuine"
                and failures[0].get("reason")=="verify-failed-1"
            ):
                return False,"context-delivery-recovery-terminal-failure-mismatch"
            if count < int(state.get("allowed_attempts") or 0):
                return False,"context-delivery-recovery-not-exhausted"

    status=v1_session_status_snapshot().get(sid)
    if isinstance(status,dict) and str(status.get("type") or "").lower()=="busy":
        return False,"context-delivery-recovery-session-still-active"

    evidence=context_delivery_failure_evidence(sid,did,count)
    leaf=(load_manifest().get("leaves") or {}).get(did,{})
    deadline=early_write_completed_turn_limit(leaf)
    if int(evidence.get("context_reads") or 0)<1:
        return False,"context-delivery-recovery-context-read-missing"
    if int(evidence.get("truncated_context_reads") or 0)<1:
        return False,"context-delivery-recovery-context-not-truncated"
    if evidence.get("context_verify_visible"):
        return False,"context-delivery-recovery-verify-was-visible"
    if not evidence.get("progress_baseline"):
        return False,"context-delivery-recovery-progress-not-in-baseline"
    if int(evidence.get("progress_read_denials") or 0)<1:
        return False,"context-delivery-recovery-progress-read-not-denied"
    if int(evidence.get("write_required_denials") or 0)<deadline:
        return False,"context-delivery-recovery-insufficient-gate-denials"
    if not evidence.get("maximum_steps_reached"):
        return False,"context-delivery-recovery-no-max-step-terminal"
    if int(evidence.get("completed_tool_turns") or 0)<deadline:
        return False,"context-delivery-recovery-insufficient-tool-turns"
    if not context_delivery_fixes_installed():
        return False,"context-delivery-recovery-current-code-unfixed"

    verify=load_supervisor_verify_evidence(did).get("latest") or {}
    canonical=str(leaf.get("verify_command") or "")
    if not (
        isinstance(verify,dict)
        and int(verify.get("attempt") or 0)==count
        and verify.get("session")==sid
        and verify.get("executed") is True
        and verify.get("result")=="verify-failed-1"
        and int(verify.get("exit_code") or -1)==1
        and str(verify.get("command") or "")==canonical
    ):
        return False,"context-delivery-recovery-verify-evidence-mismatch"

    reason=(
        "context-delivery-failure "
        f"truncated_context={evidence['truncated_context_reads']} "
        f"progress_read_denied={evidence['progress_read_denials']} "
        f"write_required_denials={evidence['write_required_denials']} "
        "maximum_steps_reached"
    )
    with dispatch_lock:
        with attempt_lock():
            data=load_attempts()
            entry=(data.get("deliverables") or {}).get(did)
            if not isinstance(entry,dict):
                return False,"context-delivery-recovery-ledger-disappeared"
            if (
                int(entry.get("count") or 0)!=count
                or (entry.get("sessions") or [])[-1:]!=[sid]
            ):
                return False,"context-delivery-recovery-ledger-changed"
            prior=entry.setdefault("context_delivery_recoveries",[])
            if prior:
                return False,"context-delivery-recovery-limit"
            failures=[
                item for item in (entry.get("failure_history") or [])
                if isinstance(item,dict)
                and int(item.get("attempt") or 0)==count
            ]
            if len(failures)!=1 or failures[0].get("classification")!="genuine":
                return False,"context-delivery-recovery-failure-changed"

            failure=failures[0]
            failure["original_classification"]="genuine"
            failure["original_reason"]=str(failure.get("reason") or "")
            failure["classification"]="infrastructure"
            failure["reason"]=reason
            failure["reclassified_by"]="runtime-context-delivery-repair"
            prior.append({
                "protocol":CONTEXT_DELIVERY_RECOVERY_PROTOCOL,
                "source":"supervisor-context-delivery-repair",
                "grant":1,
                "attempt":count,
                "session":sid,
                "context_reads":int(evidence["context_reads"]),
                "truncated_context_reads":int(
                    evidence["truncated_context_reads"]
                ),
                "progress_read_denials":int(
                    evidence["progress_read_denials"]
                ),
                "write_required_denials":int(
                    evidence["write_required_denials"]
                ),
                "completed_tool_turns":int(
                    evidence["completed_tool_turns"]
                ),
                "progress_baseline":str(evidence["progress_baseline"]),
                "context_output_sha256":str(
                    evidence["context_output_sha256"]
                ),
                "verify_result":"verify-failed-1",
                "timestamp":time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ",time.gmtime()
                ),
            })
            projected=attempt_state(entry)
            if (
                not projected.get("valid")
                or int(
                    projected.get("context_delivery_retry_grants") or 0
                )!=1
                or int(projected.get("allowed_attempts") or 0)<=count
            ):
                return False,"context-delivery-recovery-projected-ledger-invalid"
            save_attempts(data)

    log(
        f"CONTEXT_DELIVERY_RECOVERY deliverable={did} attempt={count} "
        f"session={sid} truncated={evidence['truncated_context_reads']} "
        f"progress_denials={evidence['progress_read_denials']} "
        f"gate_denials={evidence['write_required_denials']}"
    )
    csv(
        "CONTEXT_DELIVERY_RECOVERY",sid,"supervisor",
        f"{did} attempt={count} truncated={evidence['truncated_context_reads']} "
        f"progress_denials={evidence['progress_read_denials']} "
        f"gate_denials={evidence['write_required_denials']}",
    )
    return True,"recovered"


EXTERNAL_EXECUTION_CONTRACT_CORRECTION_PROTOCOL=(
    "v2-external-execution-contract-correction-v1"
)
EXTERNAL_EXECUTION_CONTRACT_RECOVERY_PROTOCOL=(
    "v2-external-execution-contract-recovery-v1"
)


def recover_external_execution_contract(did,correction_file):
    """Replace one exhausted attempt whose external execution contract was wrong."""
    if not valid_deliverable_id(did):
        return False,"invalid-deliverable"
    if ready_info(did):
        return True,"already-complete"
    if acceptance_reference_policy()!="external-required":
        return False,"external-contract-recovery-reference-policy-mismatch"

    try:
        spec=load_json_object(
            Path(correction_file),label="external execution contract correction"
        )
    except Exception as exc:
        return False,f"external-contract-recovery-correction-unreadable:{exc}"
    allowed={
        "protocol","deliverable","correction","authoritative_sources","evidence"
    }
    if set(spec)!=allowed:
        return False,"external-contract-recovery-correction-fields"
    if (
        spec.get("protocol")!=EXTERNAL_EXECUTION_CONTRACT_CORRECTION_PROTOCOL
        or spec.get("deliverable")!=did
    ):
        return False,"external-contract-recovery-correction-identity"
    correction=str(spec.get("correction") or "").strip()
    if len(correction)<80 or len(correction)>8000:
        return False,"external-contract-recovery-correction-length"
    sources=spec.get("authoritative_sources")
    if (
        not isinstance(sources,list) or not sources or len(sources)>8
        or any(
            not isinstance(item,str) or not item.startswith("https://")
            for item in sources
        )
    ):
        return False,"external-contract-recovery-sources-invalid"
    evidence=spec.get("evidence")
    if not isinstance(evidence,dict) or not evidence:
        return False,"external-contract-recovery-evidence-missing"
    correction_sha=hashlib.sha256(correction.encode("utf-8")).hexdigest()

    leaf=(load_manifest().get("leaves") or {}).get(did,{})
    if not isinstance(leaf,dict) or leaf_children(did):
        return False,"external-contract-recovery-requires-executable-leaf"
    canonical=str(leaf.get("verify_command") or "")

    with dispatch_lock:
        with attempt_lock():
            data=load_attempts()
            entry=(data.get("deliverables") or {}).get(did)
            if not isinstance(entry,dict):
                return False,"external-contract-recovery-missing-ledger-entry"
            state=attempt_state(entry)
            if not state.get("valid"):
                return False,"external-contract-recovery-invalid-ledger"
            count=int(entry.get("count") or 0)
            sessions=entry.get("sessions")
            if (
                count<1 or not isinstance(sessions,list)
                or len(sessions)!=count
                or not isinstance(sessions[-1],str) or not sessions[-1]
            ):
                return False,"external-contract-recovery-session-mismatch"
            sid=sessions[-1]
            prior=entry.get("external_execution_contract_recoveries") or []
            if not isinstance(prior,list):
                return False,"external-contract-recovery-history-invalid"
            if len(prior)>=MAX_EXTERNAL_CONTRACT_RETRY_GRANTS:
                if any(
                    isinstance(item,dict)
                    and int(item.get("attempt") or 0)==count
                    and item.get("session")==sid
                    and item.get("correction_sha256")==correction_sha
                    for item in prior
                ):
                    return True,"already-recovered"
                return False,"external-contract-recovery-limit"
            failures=[
                item for item in (entry.get("failure_history") or [])
                if isinstance(item,dict)
                and int(item.get("attempt") or 0)==count
            ]
            if len(failures)!=1 or not (
                failures[0].get("classification")=="genuine"
                and failures[0].get("reason")=="verify-failed-1"
            ):
                return False,"external-contract-recovery-terminal-failure-mismatch"
            if count < int(state.get("allowed_attempts") or 0):
                return False,"external-contract-recovery-not-exhausted"

    status=v1_session_status_snapshot().get(sid)
    if isinstance(status,dict) and str(status.get("type") or "").lower()=="busy":
        return False,"external-contract-recovery-session-still-active"
    verify=load_supervisor_verify_evidence(did).get("latest") or {}
    if not (
        isinstance(verify,dict)
        and int(verify.get("attempt") or 0)==count
        and verify.get("session")==sid
        and verify.get("executed") is True
        and verify.get("result")=="verify-failed-1"
        and int(verify.get("exit_code") or -1)==1
        and str(verify.get("command") or "")==canonical
    ):
        return False,"external-contract-recovery-verify-evidence-mismatch"

    correction_path=(
        Path(PROJECT)/".opencode-v2"/"work"/
        f"{did}.execution-contract-correction.json"
    )
    correction_payload={
        "owner":"supervisor",
        "protocol":EXTERNAL_EXECUTION_CONTRACT_CORRECTION_PROTOCOL,
        "deliverable":did,
        "correction":correction,
        "authoritative_sources":sources,
        "evidence":evidence,
        "correction_sha256":correction_sha,
        "timestamp":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime()),
    }

    with dispatch_lock:
        with attempt_lock():
            data=load_attempts()
            entry=(data.get("deliverables") or {}).get(did)
            if not isinstance(entry,dict):
                return False,"external-contract-recovery-ledger-disappeared"
            if (
                int(entry.get("count") or 0)!=count
                or (entry.get("sessions") or [])[-1:]!=[sid]
            ):
                return False,"external-contract-recovery-ledger-changed"
            prior=entry.setdefault("external_execution_contract_recoveries",[])
            if prior:
                return False,"external-contract-recovery-limit"
            failures=[
                item for item in (entry.get("failure_history") or [])
                if isinstance(item,dict)
                and int(item.get("attempt") or 0)==count
            ]
            if len(failures)!=1 or failures[0].get("classification")!="genuine":
                return False,"external-contract-recovery-failure-changed"
            failure=failures[0]
            failure["original_classification"]="genuine"
            failure["original_reason"]=str(failure.get("reason") or "")
            failure["classification"]="bad-plan"
            failure["reason"]=(
                "external-execution-contract-invalid "
                f"correction_sha256={correction_sha}"
            )
            failure["reclassified_by"]="runtime-external-execution-contract-repair"
            prior.append({
                "protocol":EXTERNAL_EXECUTION_CONTRACT_RECOVERY_PROTOCOL,
                "source":"supervisor-external-contract-repair",
                "grant":1,
                "attempt":count,
                "session":sid,
                "correction_sha256":correction_sha,
                "correction_path":str(correction_path.relative_to(Path(PROJECT))),
                "authoritative_sources":sources,
                "timestamp":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime()),
            })
            projected=attempt_state(entry)
            if (
                not projected.get("valid")
                or int(projected.get("external_contract_retry_grants") or 0)!=1
                or int(projected.get("allowed_attempts") or 0)<=count
            ):
                return False,"external-contract-recovery-projected-ledger-invalid"
            atomic_write_json(correction_path,correction_payload)
            save_attempts(data)

    log(
        f"EXTERNAL_EXECUTION_CONTRACT_RECOVERY deliverable={did} "
        f"attempt={count} session={sid} correction_sha256={correction_sha}"
    )
    csv(
        "EXTERNAL_EXECUTION_CONTRACT_RECOVERY",sid,"supervisor",
        f"{did} attempt={count} correction_sha256={correction_sha}",
    )
    return True,"recovered"


HISTORICAL_PARENT_REPAIR_RESOLUTION_PROTOCOL=(
    "v2-historical-parent-contract-repair-resolution-v1"
)


def recover_historical_parent_contract_repair_resolution(did):
    """Restore only a missing durable retry-credit marker from archived proof."""
    if not valid_deliverable_id(did):
        return False,"invalid-deliverable"
    if not plan_ready():
        return False,"historical-parent-repair-plan-not-ready"

    project=Path(PROJECT)
    work=project/".opencode-v2"/"work"
    try:
        structured_key=_structured_plan_symbolic_key(did)
    except Exception:
        return False,"historical-parent-repair-structured-key-missing"

    archive_dir=work/"contract-repair-history"
    archives=[]
    if archive_dir.is_dir():
        for path in sorted(archive_dir.glob(f"{did}.*.json")):
            try:
                data=load_json_object(
                    path,label=f"contract repair archive {did}"
                )
            except Exception:
                continue
            if (
                data.get("owner")=="supervisor"
                and data.get("protocol")=="v2-contract-repair-history-v1"
                and data.get("parent_id")==did
                and isinstance(data.get("records"),dict)
                and data["records"]
            ):
                archives.append(path)
    if not archives:
        return False,"historical-parent-repair-archive-missing"

    event_path=work/"contract-repair-events.log"
    try:
        events=event_path.read_text(errors="replace")
    except OSError:
        return False,"historical-parent-repair-event-log-missing"
    event_token=(
        f"PARENT_CONTRACT_REPAIR_REQUESTED deliverable={did} "
        f"key={structured_key} "
    )
    if event_token not in events:
        return False,"historical-parent-repair-event-missing"

    with dispatch_lock:
        with attempt_lock():
            data=load_attempts()
            entry=(data.get("deliverables") or {}).get(did)
            if not isinstance(entry,dict):
                return False,"historical-parent-repair-ledger-missing"
            existing=entry.get("parent_contract_repair_resolution")
            if isinstance(existing,dict):
                approved=existing.get("reclassified_attempts")
                if (
                    existing.get("structured_key")==structured_key
                    and isinstance(approved,list)
                    and approved
                ):
                    return True,"already-resolved"
                return False,"historical-parent-repair-conflicting-resolution"
            if entry.get("split_rearm_after_contract_repair"):
                return False,"historical-parent-repair-still-pending"

            reclassified=[]
            for item in entry.get("failure_history",[]) or []:
                if not isinstance(item,dict) or not (
                    item.get("classification")=="bad-plan"
                    and item.get("reclassified_by")==
                        "runtime-parent-contract-repair"
                ):
                    continue
                try:
                    attempt=int(item.get("attempt") or 0)
                except (TypeError,ValueError):
                    continue
                if attempt>0 and attempt not in reclassified:
                    reclassified.append(attempt)
            if not reclassified:
                return False,"historical-parent-repair-no-reclassified-attempts"

            marker={
                "protocol":HISTORICAL_PARENT_REPAIR_RESOLUTION_PROTOCOL,
                "structured_key":structured_key,
                "reclassified_attempts":reclassified,
                "archive_files":[
                    path.relative_to(project).as_posix() for path in archives
                ],
                "event_log":event_path.relative_to(project).as_posix(),
                "timestamp":time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ",time.gmtime()
                ),
            }
            entry["parent_contract_repair_resolution"]=marker
            projected=attempt_state(entry)
            if not projected.get("valid"):
                return False,"historical-parent-repair-projected-ledger-invalid"
            if int(projected.get("bad_plan_retry_grants") or 0)!=len(reclassified):
                return False,"historical-parent-repair-credit-mismatch"
            save_attempts(data)

    log(
        f"HISTORICAL_PARENT_CONTRACT_REPAIR_RESOLVED deliverable={did} "
        f"key={structured_key} attempts={','.join(map(str,reclassified))}"
    )
    csv(
        "HISTORICAL_PARENT_CONTRACT_REPAIR_RESOLVED","", "supervisor",
        f"{did} key={structured_key} attempts={','.join(map(str,reclassified))}",
    )
    return True,"resolved"


NESTED_HANDOFF_WRITER_VERIFY_UPGRADE_PROTOCOL=(
    "v2-nested-handoff-writer-verify-upgrade-v1"
)


def recover_nested_handoff_writer_verify(parent):
    """Propagate a pre-invariant stale writer Verify through one nested split.

    Current split validation requires the writing child after a progress handoff
    to preserve the exact parent Verify. Historical transactions can predate
    that invariant. If such a stale writer was recursively split before the
    mismatch became observable, its terminal writer can exhaust against the
    stale Verify and never reach parent finalization. This migration archives
    both split generations, updates only Verify/Done-when on the two writer
    contracts, and grants the exhausted terminal writer one ordinary
    plan-contract replacement. Attempt/session history is never rewritten.
    """
    if not valid_deliverable_id(parent):
        return False,"invalid-parent"
    if split_depth(parent)!=0:
        return False,"nested-writer-upgrade-requires-root-parent"

    project=Path(PROJECT)
    work=project/".opencode-v2"/"work"
    guard_path=project/".opencode-v2"/"IMPLEMENTATION_PLAN.guard.json"

    with splitter_state_lock(parent):
        with dispatch_lock:
            with attempt_lock():
                outer_status=load_split_status(parent)
                outer_txn=load_split_transaction(parent)
                if outer_status.get("state")!="accepted":
                    return False,"nested-writer-upgrade-outer-split-not-accepted"
                if (
                    outer_txn.get("state")!="committed"
                    or outer_txn.get("parent_id")!=parent
                    or len(outer_txn.get("children") or [])!=2
                    or not isinstance(outer_txn.get("child_defs"),dict)
                ):
                    return False,"nested-writer-upgrade-outer-transaction-invalid"
                outer_children=list(outer_txn["children"])
                first_id,writer_id=outer_children
                outer_defs=outer_txn["child_defs"]
                first_def=outer_defs.get(first_id)
                writer_def=outer_defs.get(writer_id)
                if not isinstance(first_def,dict) or not isinstance(writer_def,dict):
                    return False,"nested-writer-upgrade-outer-child-definition-missing"
                if not first_def.get("split_handoff_only"):
                    return False,"nested-writer-upgrade-outer-first-not-handoff"
                if writer_def.get("split_handoff_only"):
                    return False,"nested-writer-upgrade-outer-writer-is-handoff"
                if writer_def.get("split_handoff_source")!=first_id:
                    return False,"nested-writer-upgrade-outer-handoff-source-mismatch"

                raw_manifest=load_json_object(
                    guard_path,label="implementation manifest"
                )
                leaves=raw_manifest.get("leaves")
                if not isinstance(leaves,dict):
                    return False,"nested-writer-upgrade-manifest-invalid"
                parent_leaf=leaves.get(parent)
                live_first=leaves.get(first_id)
                live_writer=leaves.get(writer_id)
                if not all(isinstance(item,dict) for item in (
                    parent_leaf,live_first,live_writer
                )):
                    return False,"nested-writer-upgrade-outer-live-leaf-missing"
                if list(parent_leaf.get("split_children") or [])!=outer_children:
                    return False,"nested-writer-upgrade-outer-active-children-mismatch"
                if not ready_info(first_id):
                    return False,"nested-writer-upgrade-outer-handoff-not-ready"
                if ready_info(writer_id):
                    return False,"nested-writer-upgrade-outer-writer-already-ready"

                parent_owned=set(owned_artifact_paths(parent_leaf))
                writer_owned=set(owned_artifact_paths(writer_def))
                if not parent_owned or writer_owned!=parent_owned:
                    return False,"nested-writer-upgrade-outer-writer-ownership-mismatch"
                parent_verify=str(parent_leaf.get("verify_command") or "").strip()
                stale_verify=str(writer_def.get("verify_command") or "").strip()
                if not parent_verify or not stale_verify:
                    return False,"nested-writer-upgrade-verify-missing"
                if stale_verify==parent_verify:
                    return False,"nested-writer-upgrade-outer-writer-already-current"
                if validate_verify_command(parent_verify):
                    return False,"nested-writer-upgrade-current-parent-verify-invalid"

                nested_children=list(live_writer.get("split_children") or [])
                if len(nested_children)!=2:
                    return False,"nested-writer-upgrade-writer-not-recursively-split"
                nested_first_id,nested_writer_id=nested_children
                nested_status=load_split_status(writer_id)
                nested_txn=load_split_transaction(writer_id)
                if nested_status.get("state")!="accepted":
                    return False,"nested-writer-upgrade-nested-split-not-accepted"
                if (
                    nested_txn.get("state")!="committed"
                    or nested_txn.get("parent_id")!=writer_id
                    or list(nested_txn.get("children") or [])!=nested_children
                    or not isinstance(nested_txn.get("child_defs"),dict)
                ):
                    return False,"nested-writer-upgrade-nested-transaction-invalid"
                nested_defs=nested_txn["child_defs"]
                nested_first_def=nested_defs.get(nested_first_id)
                nested_writer_def=nested_defs.get(nested_writer_id)
                live_nested_first=leaves.get(nested_first_id)
                live_nested_writer=leaves.get(nested_writer_id)
                if not all(isinstance(item,dict) for item in (
                    nested_first_def,nested_writer_def,
                    live_nested_first,live_nested_writer,
                )):
                    return False,"nested-writer-upgrade-nested-child-definition-missing"
                if not nested_first_def.get("split_handoff_only"):
                    return False,"nested-writer-upgrade-nested-first-not-handoff"
                if nested_writer_def.get("split_handoff_only"):
                    return False,"nested-writer-upgrade-nested-writer-is-handoff"
                if nested_writer_def.get("split_handoff_source")!=nested_first_id:
                    return False,"nested-writer-upgrade-nested-handoff-source-mismatch"
                if not ready_info(nested_first_id):
                    return False,"nested-writer-upgrade-nested-handoff-not-ready"
                if ready_info(nested_writer_id):
                    return False,"nested-writer-upgrade-nested-writer-already-ready"
                if set(owned_artifact_paths(nested_writer_def))!=writer_owned:
                    return False,"nested-writer-upgrade-nested-writer-ownership-mismatch"
                if str(nested_writer_def.get("verify_command") or "").strip()!=stale_verify:
                    return False,"nested-writer-upgrade-nested-verify-not-inherited"

                overlay=load_split_leaf_overlay()
                parents=overlay.get("parents")
                if not isinstance(parents,dict):
                    return False,"nested-writer-upgrade-overlay-invalid"
                outer_overlay=parents.get(parent)
                nested_overlay=parents.get(writer_id)
                if (
                    not isinstance(outer_overlay,dict)
                    or outer_overlay.get("transaction_id")!=outer_txn.get("transaction_id")
                    or outer_overlay.get("child_defs")!=outer_defs
                    or not isinstance(nested_overlay,dict)
                    or nested_overlay.get("transaction_id")!=nested_txn.get("transaction_id")
                    or nested_overlay.get("child_defs")!=nested_defs
                ):
                    return False,"nested-writer-upgrade-overlay-mismatch"

                attempts=load_attempts()
                entries=attempts.get("deliverables")
                root_entry=entries.get(parent) if isinstance(entries,dict) else None
                writer_entry=entries.get(writer_id) if isinstance(entries,dict) else None
                terminal_entry=entries.get(nested_writer_id) if isinstance(entries,dict) else None
                if not all(isinstance(item,dict) for item in (
                    root_entry,writer_entry,terminal_entry
                )):
                    return False,"nested-writer-upgrade-attempt-ledger-missing"
                terminal_state=attempt_state(terminal_entry)
                if not terminal_state.get("valid"):
                    return False,"nested-writer-upgrade-terminal-ledger-invalid"
                terminal_count=int(terminal_entry.get("count") or 0)
                if (
                    terminal_count<1
                    or terminal_count!=int(terminal_state.get("allowed_attempts") or 0)
                ):
                    return False,"nested-writer-upgrade-terminal-not-exhausted"
                terminal_sessions=terminal_entry.get("sessions")
                if (
                    not isinstance(terminal_sessions,list)
                    or len(terminal_sessions)!=terminal_count
                    or not isinstance(terminal_sessions[-1],str)
                    or not terminal_sessions[-1]
                ):
                    return False,"nested-writer-upgrade-terminal-session-mismatch"
                terminal_sid=terminal_sessions[-1]
                terminal_failures=[
                    item for item in (terminal_entry.get("failure_history") or [])
                    if isinstance(item,dict)
                    and int(item.get("attempt") or 0)==terminal_count
                ]
                if len(terminal_failures)!=1 or not (
                    terminal_failures[0].get("classification")=="genuine"
                    and terminal_failures[0].get("reason")=="verify-failed-1"
                ):
                    return False,"nested-writer-upgrade-terminal-failure-mismatch"
                terminal_evidence=load_supervisor_verify_evidence(
                    nested_writer_id
                ).get("latest") or {}
                if not (
                    isinstance(terminal_evidence,dict)
                    and int(terminal_evidence.get("attempt") or 0)==terminal_count
                    and terminal_evidence.get("session")==terminal_sid
                    and terminal_evidence.get("executed") is True
                    and terminal_evidence.get("result")=="verify-failed-1"
                    and int(terminal_evidence.get("exit_code") or -1)==1
                    and str(terminal_evidence.get("command") or "")==stale_verify
                ):
                    return False,"nested-writer-upgrade-terminal-verify-evidence-mismatch"

                recoveries=root_entry.setdefault(
                    "nested_handoff_writer_verify_upgrades",[]
                )
                if not isinstance(recoveries,list):
                    return False,"nested-writer-upgrade-history-invalid"
                if recoveries:
                    return False,"nested-writer-upgrade-already-used"

                old_sha=hashlib.sha256(stale_verify.encode()).hexdigest()
                new_sha=hashlib.sha256(parent_verify.encode()).hexdigest()
                terminal_revisions=terminal_entry.setdefault(
                    "plan_contract_revisions",[]
                )
                if not isinstance(terminal_revisions,list):
                    return False,"nested-writer-upgrade-terminal-revision-history-invalid"
                if any(
                    isinstance(row,dict)
                    and row.get("source")=="supervisor-plan-contract-revision"
                    and int(row.get("attempt") or 0)==terminal_count
                    and row.get("current_verify_sha256")==new_sha
                    for row in terminal_revisions
                ):
                    return False,"nested-writer-upgrade-terminal-credit-already-present"

                outer_archive=_archive_split_state_for_contract_repair(parent)
                nested_archive=_archive_split_state_for_contract_repair(writer_id)
                if outer_archive is None or nested_archive is None:
                    return False,"nested-writer-upgrade-archive-missing"

                now=time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime())
                upgraded_outer_writer_def=dict(writer_def)
                upgraded_outer_writer_def["verify_command"]=parent_verify
                upgraded_outer_writer_def["done_when"]=str(
                    parent_leaf.get("done_when") or writer_def.get("done_when") or ""
                )
                upgraded_live_writer=dict(live_writer)
                upgraded_live_writer["verify_command"]=parent_verify
                upgraded_live_writer["done_when"]=upgraded_outer_writer_def["done_when"]

                upgraded_nested_writer_def=dict(nested_writer_def)
                upgraded_nested_writer_def["verify_command"]=parent_verify
                upgraded_nested_writer_def["done_when"]=upgraded_outer_writer_def["done_when"]
                upgraded_live_nested_writer=dict(live_nested_writer)
                upgraded_live_nested_writer["verify_command"]=parent_verify
                upgraded_live_nested_writer["done_when"]=upgraded_outer_writer_def["done_when"]

                new_outer_defs=dict(outer_defs)
                new_outer_defs[writer_id]=upgraded_outer_writer_def
                new_outer_generation=int(outer_txn.get("generation") or 1)+1
                new_outer_txn_id=split_transaction_id(
                    parent,new_outer_generation,new_outer_defs
                )

                new_nested_defs=dict(nested_defs)
                new_nested_defs[nested_writer_id]=upgraded_nested_writer_def
                new_nested_generation=int(nested_txn.get("generation") or 1)+1
                new_nested_txn_id=split_transaction_id(
                    writer_id,new_nested_generation,new_nested_defs
                )

                # The intermediate writer contract changed too. Record the
                # revision at its current count for audit and for one bounded
                # replacement should parent collapse later require execution.
                writer_count=int(writer_entry.get("count") or 0)
                writer_revisions=writer_entry.setdefault(
                    "plan_contract_revisions",[]
                )
                if not isinstance(writer_revisions,list):
                    return False,"nested-writer-upgrade-writer-revision-history-invalid"
                if not any(
                    isinstance(row,dict)
                    and row.get("source")=="supervisor-plan-contract-revision"
                    and int(row.get("attempt") or 0)==writer_count
                    and row.get("current_verify_sha256")==new_sha
                    for row in writer_revisions
                ):
                    writer_revisions.append({
                        "attempt":writer_count,
                        "source":"supervisor-plan-contract-revision",
                        "previous_verify_sha256":old_sha,
                        "current_verify_sha256":new_sha,
                        "reason":"nested-handoff-writer-verify-upgrade",
                        "timestamp":now,
                    })

                writer_projected=attempt_state(writer_entry)
                if not writer_projected.get("valid"):
                    return False,"nested-writer-upgrade-writer-revision-invalid"

                terminal_revisions.append({
                    "attempt":terminal_count,
                    "source":"supervisor-plan-contract-revision",
                    "previous_verify_sha256":old_sha,
                    "current_verify_sha256":new_sha,
                    "reason":"nested-handoff-writer-verify-upgrade",
                    "timestamp":now,
                })
                terminal_projected=attempt_state(terminal_entry)
                if (
                    not terminal_projected.get("valid")
                    or int(terminal_projected.get("plan_contract_retry_grants") or 0)<1
                    or int(terminal_projected.get("allowed_attempts") or 0)<=terminal_count
                ):
                    return False,"nested-writer-upgrade-terminal-replacement-credit-invalid"

                recovery={
                    "protocol":NESTED_HANDOFF_WRITER_VERIFY_UPGRADE_PROTOCOL,
                    "state":"prepared",
                    "writer":writer_id,
                    "terminal_writer":nested_writer_id,
                    "terminal_attempt":terminal_count,
                    "previous_verify_sha256":old_sha,
                    "current_verify_sha256":new_sha,
                    "outer_prior_transaction_id":outer_txn.get("transaction_id"),
                    "outer_replacement_transaction_id":new_outer_txn_id,
                    "nested_prior_transaction_id":nested_txn.get("transaction_id"),
                    "nested_replacement_transaction_id":new_nested_txn_id,
                    "outer_archive":str(outer_archive.relative_to(project)),
                    "nested_archive":str(nested_archive.relative_to(project)),
                    "prepared_at":now,
                }
                recoveries.append(recovery)
                save_attempts(attempts)

                leaves[writer_id]=upgraded_live_writer
                leaves[nested_writer_id]=upgraded_live_nested_writer
                raw_manifest["leaves"]=leaves
                atomic_write_json(guard_path,raw_manifest)

                parents[parent]={
                    **outer_overlay,
                    "child_defs":new_outer_defs,
                    "transaction_id":new_outer_txn_id,
                    "timestamp":now,
                }
                parents[writer_id]={
                    **nested_overlay,
                    "child_defs":new_nested_defs,
                    "transaction_id":new_nested_txn_id,
                    "timestamp":now,
                }
                overlay["parents"]=parents
                save_split_leaf_overlay(overlay)

                new_outer_txn={
                    **outer_txn,
                    "generation":new_outer_generation,
                    "child_defs":new_outer_defs,
                    "transaction_id":new_outer_txn_id,
                    "prepared_at":now,
                    "committed_at":now,
                    "replaces_transaction_id":outer_txn.get("transaction_id"),
                    "replacement_reason":"nested-handoff-writer-verify-upgrade",
                }
                new_nested_txn={
                    **nested_txn,
                    "generation":new_nested_generation,
                    "child_defs":new_nested_defs,
                    "transaction_id":new_nested_txn_id,
                    "prepared_at":now,
                    "committed_at":now,
                    "replaces_transaction_id":nested_txn.get("transaction_id"),
                    "replacement_reason":"nested-handoff-writer-verify-upgrade",
                }
                atomic_write_json(split_transaction_path(parent),new_outer_txn)
                atomic_write_json(split_transaction_path(writer_id),new_nested_txn)

                atomic_write_text(
                    work/f"{writer_id}.scope.md",
                    render_split_child_scope(
                        writer_id,parent,upgraded_outer_writer_def,parent_leaf
                    ),
                )
                atomic_write_text(
                    work/f"{nested_writer_id}.scope.md",
                    render_split_child_scope(
                        nested_writer_id,writer_id,
                        upgraded_nested_writer_def,upgraded_live_writer
                    ),
                )

                for status,new_generation,new_txn_id in (
                    (outer_status,new_outer_generation,new_outer_txn_id),
                    (nested_status,new_nested_generation,new_nested_txn_id),
                ):
                    status["generation"]=new_generation
                    status["transaction_id"]=new_txn_id
                    status["timestamp"]=now
                    status["nested_handoff_writer_verify_upgrade"]={
                        "protocol":NESTED_HANDOFF_WRITER_VERIFY_UPGRADE_PROTOCOL,
                        "previous_verify_sha256":old_sha,
                        "current_verify_sha256":new_sha,
                        "terminal_writer":nested_writer_id,
                    }
                atomic_write_json(split_status_path(parent),outer_status)
                atomic_write_json(split_status_path(writer_id),nested_status)

                history_path=split_history_path()
                history=load_json_object(
                    history_path,
                    default_missing={
                        "owner":"supervisor",
                        "protocol":SPLIT_PROPOSAL_PROTOCOL,
                        "splits":[],
                    },
                    label="split history",
                )
                splits=history.get("splits")
                if not isinstance(splits,list):
                    raise StateCorruptionError("split history splits must be an array")
                splits.extend([
                    {
                        "parent":parent,
                        "children":outer_children,
                        "transaction_id":new_outer_txn_id,
                        "replaces_transaction_id":outer_txn.get("transaction_id"),
                        "timestamp":now,
                        "source":"supervisor-nested-handoff-writer-verify-upgrade",
                    },
                    {
                        "parent":writer_id,
                        "children":nested_children,
                        "transaction_id":new_nested_txn_id,
                        "replaces_transaction_id":nested_txn.get("transaction_id"),
                        "timestamp":now,
                        "source":"supervisor-nested-handoff-writer-verify-upgrade",
                    },
                ])
                history["splits"]=splits
                atomic_write_json(history_path,history)

                # Commit the audit record only after every active projection
                # points at the replacement contracts.
                attempts=load_attempts()
                root_entry=(attempts.get("deliverables") or {}).get(parent)
                rows=(
                    root_entry.get("nested_handoff_writer_verify_upgrades")
                    if isinstance(root_entry,dict) else None
                )
                target=rows[-1] if isinstance(rows,list) and rows else None
                if not isinstance(target,dict):
                    raise StateCorruptionError(
                        f"{parent} nested writer upgrade record disappeared"
                    )
                target["state"]="committed"
                target["committed_at"]=time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ",time.gmtime()
                )
                save_attempts(attempts)

    log(
        f"NESTED_HANDOFF_WRITER_VERIFY_UPGRADED parent={parent} "
        f"writer={writer_id} terminal={nested_writer_id} "
        f"old={old_sha} new={new_sha}"
    )
    csv(
        "NESTED_HANDOFF_WRITER_VERIFY_UPGRADED","", "supervisor",
        f"{parent} writer={writer_id} terminal={nested_writer_id} "
        f"old={old_sha} new={new_sha}",
    )
    return True,"nested-writer-contract-replacement-ready"


LEGACY_HANDOFF_WRITER_VERIFY_UPGRADE_PROTOCOL=(
    "v2-legacy-handoff-writer-verify-upgrade-v1"
)


def recover_legacy_handoff_writer_verify(parent):
    """Upgrade one pre-invariant handoff writer to the exact parent Verify.

    This is a contract migration, not a semantic retry. The old committed split
    transaction and READY evidence are archived/audited, the writer's READY is
    revoked, and the normal plan-contract revision credit authorizes exactly one
    replacement attempt under the stronger Verify.
    """
    if not valid_deliverable_id(parent):
        return False,"invalid-parent"

    project=Path(PROJECT)
    work=project/".opencode-v2"/"work"
    guard_path=project/".opencode-v2"/"IMPLEMENTATION_PLAN.guard.json"

    with splitter_state_lock(parent):
        with dispatch_lock:
            with attempt_lock():
                status=load_split_status(parent)
                if status.get("state")!="parent-finalize-failed":
                    return False,"legacy-writer-upgrade-requires-parent-finalize-failed"
                if int(status.get("parent_finalize_failures") or 0)<MAX_SPLIT_PARENT_FINALIZE_FAILURES:
                    return False,"legacy-writer-upgrade-parent-finalize-not-exhausted"

                txn=load_split_transaction(parent)
                if (
                    txn.get("state")!="committed"
                    or txn.get("parent_id")!=parent
                    or not isinstance(txn.get("child_defs"),dict)
                    or not isinstance(txn.get("children"),list)
                    or len(txn.get("children") or [])!=2
                ):
                    return False,"legacy-writer-upgrade-transaction-invalid"
                children=list(txn["children"])
                first_id,writer_id=children
                child_defs=txn["child_defs"]
                first=child_defs.get(first_id)
                writer=child_defs.get(writer_id)
                if not isinstance(first,dict) or not isinstance(writer,dict):
                    return False,"legacy-writer-upgrade-child-definition-missing"
                if not first.get("split_handoff_only"):
                    return False,"legacy-writer-upgrade-first-child-not-handoff"
                if writer.get("split_handoff_only"):
                    return False,"legacy-writer-upgrade-writer-is-handoff"
                if writer.get("split_handoff_source")!=first_id:
                    return False,"legacy-writer-upgrade-handoff-source-mismatch"
                if first.get("owned_artifact_paths"):
                    return False,"legacy-writer-upgrade-handoff-has-ownership"

                raw_manifest=load_json_object(
                    guard_path,label="implementation manifest"
                )
                leaves=raw_manifest.get("leaves")
                if not isinstance(leaves,dict):
                    return False,"legacy-writer-upgrade-manifest-invalid"
                parent_leaf=leaves.get(parent)
                live_first=leaves.get(first_id)
                live_writer=leaves.get(writer_id)
                if not all(isinstance(x,dict) for x in (
                    parent_leaf,live_first,live_writer
                )):
                    return False,"legacy-writer-upgrade-live-leaf-missing"
                if list(parent_leaf.get("split_children") or [])!=children:
                    return False,"legacy-writer-upgrade-active-children-mismatch"
                if live_first!=first or live_writer!=writer:
                    return False,"legacy-writer-upgrade-live-child-drift"

                parent_owned=set(owned_artifact_paths(parent_leaf))
                writer_owned=set(owned_artifact_paths(writer))
                if not parent_owned or writer_owned!=parent_owned:
                    return False,"legacy-writer-upgrade-writer-does-not-own-parent"
                parent_verify=str(parent_leaf.get("verify_command") or "").strip()
                old_verify=str(writer.get("verify_command") or "").strip()
                if not parent_verify:
                    return False,"legacy-writer-upgrade-parent-verify-missing"
                if old_verify==parent_verify:
                    return False,"legacy-writer-upgrade-already-current"

                if not ready_info(first_id):
                    return False,"legacy-writer-upgrade-handoff-not-ready"
                writer_ready=ready_info(writer_id)
                if not writer_ready:
                    return False,"legacy-writer-upgrade-writer-not-ready"
                writer_evidence=load_supervisor_verify_evidence(writer_id)
                writer_latest=writer_evidence.get("latest")
                if not (
                    isinstance(writer_latest,dict)
                    and writer_latest.get("result")=="verified"
                    and writer_latest.get("executed") is True
                    and int(writer_latest.get("exit_code") or 0)==0
                    and str(writer_latest.get("command") or "")==old_verify
                ):
                    return False,"legacy-writer-upgrade-writer-ready-evidence-mismatch"

                parent_evidence=load_supervisor_verify_evidence(parent)
                parent_latest=parent_evidence.get("latest")
                if not (
                    isinstance(parent_latest,dict)
                    and parent_latest.get("result")=="verify-failed-1"
                    and parent_latest.get("executed") is True
                    and int(parent_latest.get("exit_code") or 0)==1
                    and str(parent_latest.get("command") or "")==parent_verify
                ):
                    return False,"legacy-writer-upgrade-parent-failure-evidence-mismatch"

                overlay=load_split_leaf_overlay()
                parents=overlay.get("parents")
                overlay_entry=parents.get(parent) if isinstance(parents,dict) else None
                if (
                    not isinstance(overlay_entry,dict)
                    or overlay_entry.get("transaction_id")!=txn.get("transaction_id")
                    or overlay_entry.get("child_defs")!=child_defs
                    or list(overlay_entry.get("children") or [])!=children
                ):
                    return False,"legacy-writer-upgrade-overlay-mismatch"

                attempts=load_attempts()
                entries=attempts.get("deliverables")
                writer_entry=entries.get(writer_id) if isinstance(entries,dict) else None
                parent_entry=entries.get(parent) if isinstance(entries,dict) else None
                if not isinstance(writer_entry,dict) or not isinstance(parent_entry,dict):
                    return False,"legacy-writer-upgrade-attempt-ledger-missing"
                writer_state=attempt_state(writer_entry)
                if not writer_state.get("valid"):
                    return False,"legacy-writer-upgrade-writer-ledger-invalid"
                writer_count=int(writer_entry.get("count") or 0)
                if writer_count<1:
                    return False,"legacy-writer-upgrade-writer-attempt-missing"

                recovery_rows=parent_entry.setdefault(
                    "legacy_handoff_writer_verify_upgrades",[]
                )
                if not isinstance(recovery_rows,list):
                    return False,"legacy-writer-upgrade-history-invalid"
                if recovery_rows:
                    return False,"legacy-writer-upgrade-already-used"

                old_sha=hashlib.sha256(old_verify.encode()).hexdigest()
                new_sha=hashlib.sha256(parent_verify.encode()).hexdigest()
                revision_rows=writer_entry.setdefault("plan_contract_revisions",[])
                if not isinstance(revision_rows,list):
                    return False,"legacy-writer-upgrade-revision-history-invalid"
                if any(
                    isinstance(row,dict)
                    and row.get("source")=="supervisor-plan-contract-revision"
                    and int(row.get("attempt") or 0)==writer_count
                    and row.get("current_verify_sha256")==new_sha
                    for row in revision_rows
                ):
                    return False,"legacy-writer-upgrade-credit-already-present"

                archive=_archive_split_state_for_contract_repair(parent)
                if archive is None:
                    return False,"legacy-writer-upgrade-archive-missing"

                ready_path=work/f"{writer_id}.ready"
                try:
                    prior_ready_text=ready_path.read_text(errors="replace")
                except OSError:
                    return False,"legacy-writer-upgrade-ready-unreadable"

                upgraded_writer=dict(writer)
                upgraded_writer["verify_command"]=parent_verify
                upgraded_writer["done_when"]=str(
                    parent_leaf.get("done_when") or writer.get("done_when") or ""
                )
                new_child_defs=dict(child_defs)
                new_child_defs[writer_id]=upgraded_writer
                new_generation=int(txn.get("generation") or 1)+1
                new_txn_id=split_transaction_id(
                    parent,new_generation,new_child_defs
                )
                now=time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime())

                revision_rows.append({
                    "attempt":writer_count,
                    "source":"supervisor-plan-contract-revision",
                    "previous_verify_sha256":old_sha,
                    "current_verify_sha256":new_sha,
                    "reason":"legacy-handoff-writer-verify-upgrade",
                    "timestamp":now,
                })
                projected=attempt_state(writer_entry)
                if (
                    not projected.get("valid")
                    or int(projected.get("plan_contract_retry_grants") or 0)<1
                    or int(projected.get("allowed_attempts") or 0)<=writer_count
                ):
                    return False,"legacy-writer-upgrade-replacement-credit-invalid"

                recovery={
                    "protocol":LEGACY_HANDOFF_WRITER_VERIFY_UPGRADE_PROTOCOL,
                    "state":"prepared",
                    "prior_transaction_id":txn.get("transaction_id"),
                    "replacement_transaction_id":new_txn_id,
                    "prior_generation":int(txn.get("generation") or 1),
                    "replacement_generation":new_generation,
                    "writer":writer_id,
                    "writer_attempt":writer_count,
                    "previous_verify_sha256":old_sha,
                    "current_verify_sha256":new_sha,
                    "writer_ready_text":prior_ready_text,
                    "archive":str(archive.relative_to(project)),
                    "prepared_at":now,
                }
                recovery_rows.append(recovery)
                save_attempts(attempts)

                leaves[writer_id]=upgraded_writer
                raw_manifest["leaves"]=leaves
                atomic_write_json(guard_path,raw_manifest)

                parents[parent]={
                    "children":children,
                    "child_defs":new_child_defs,
                    "transaction_id":new_txn_id,
                    "timestamp":now,
                }
                overlay["parents"]=parents
                save_split_leaf_overlay(overlay)

                replacement_txn={
                    "owner":"supervisor",
                    "protocol":SPLIT_TRANSACTION_PROTOCOL,
                    "state":"committed",
                    "parent_id":parent,
                    "generation":new_generation,
                    "children":children,
                    "child_defs":new_child_defs,
                    "transaction_id":new_txn_id,
                    "prepared_at":now,
                    "committed_at":now,
                    "replaces_transaction_id":txn.get("transaction_id"),
                    "replacement_reason":"legacy-handoff-writer-verify-upgrade",
                }
                atomic_write_json(split_transaction_path(parent),replacement_txn)

                atomic_write_text(
                    work/f"{writer_id}.scope.md",
                    render_split_child_scope(
                        writer_id,parent,upgraded_writer,parent_leaf
                    ),
                )
                ready_path.unlink(missing_ok=True)

                history_path=split_history_path()
                history=load_json_object(
                    history_path,
                    default_missing={
                        "owner":"supervisor",
                        "protocol":SPLIT_PROPOSAL_PROTOCOL,
                        "splits":[],
                    },
                    label="split history",
                )
                splits=history.get("splits")
                if not isinstance(splits,list):
                    raise StateCorruptionError("split history splits must be an array")
                splits.append({
                    "parent":parent,
                    "children":children,
                    "transaction_id":new_txn_id,
                    "replaces_transaction_id":txn.get("transaction_id"),
                    "timestamp":now,
                    "source":"supervisor-legacy-handoff-writer-verify-upgrade",
                })
                history["splits"]=splits
                atomic_write_json(history_path,history)

                save_split_status(
                    parent,"accepted",
                    generation=new_generation,
                    children=children,
                    transaction_id=new_txn_id,
                    parent_finalize_failures=0,
                    parent_finalize_last_result="",
                    reason="",
                    legacy_handoff_writer_verify_upgrade={
                        "protocol":LEGACY_HANDOFF_WRITER_VERIFY_UPGRADE_PROTOCOL,
                        "writer":writer_id,
                        "previous_verify_sha256":old_sha,
                        "current_verify_sha256":new_sha,
                        "replacement_transaction_id":new_txn_id,
                    },
                    lease_until_epoch=0,
                )
                _split_parent_finalize_next.pop(parent,None)

                attempts=load_attempts()
                parent_entry=(attempts.get("deliverables") or {}).get(parent)
                rows=(
                    parent_entry.get("legacy_handoff_writer_verify_upgrades")
                    if isinstance(parent_entry,dict) else None
                )
                target=rows[-1] if isinstance(rows,list) and rows else None
                if not isinstance(target,dict):
                    raise StateCorruptionError(
                        f"{parent} legacy writer upgrade record disappeared"
                    )
                target["state"]="committed"
                target["committed_at"]=time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ",time.gmtime()
                )
                save_attempts(attempts)

    log(
        f"LEGACY_HANDOFF_WRITER_VERIFY_UPGRADED parent={parent} "
        f"writer={writer_id} old_transaction={txn.get('transaction_id')} "
        f"new_transaction={new_txn_id}"
    )
    csv(
        "LEGACY_HANDOFF_WRITER_VERIFY_UPGRADED","", "supervisor",
        f"{parent} writer={writer_id} old={txn.get('transaction_id')} "
        f"new={new_txn_id}",
    )
    return True,"writer-contract-replacement-ready"


def recover_stale_split_parent_contract(parent):
    """Retire an old split projection after a proven parent contract revision.

    Historical attempts, child evidence, and the archived transaction remain
    immutable.  This transition only removes the stale *active* split overlay
    so the existing plan-contract replacement credit can run the current parent
    contract once.
    """
    if not valid_deliverable_id(parent):
        return False,"invalid-parent"
    project=Path(PROJECT)
    guard_path=project/".opencode-v2"/"IMPLEMENTATION_PLAN.guard.json"

    with splitter_state_lock(parent):
        with dispatch_lock:
            with attempt_lock():
                status=load_split_status(parent)
                if status.get("state")!="parent-finalize-failed":
                    return False,"stale-split-recovery-requires-parent-finalize-failed"

                txn=load_split_transaction(parent)
                if (
                    txn.get("state")!="committed"
                    or txn.get("parent_id")!=parent
                    or not isinstance(txn.get("child_defs"),dict)
                    or not isinstance(txn.get("children"),list)
                ):
                    return False,"stale-split-transaction-not-committed"
                children=list(txn["children"])
                if not children or any(not ready_info(child) for child in children):
                    return False,"stale-split-children-not-ready"

                raw_manifest=load_json_object(
                    guard_path,label="implementation manifest"
                )
                leaves=raw_manifest.get("leaves")
                if not isinstance(leaves,dict):
                    return False,"stale-split-manifest-invalid"
                parent_leaf=leaves.get(parent)
                if not isinstance(parent_leaf,dict):
                    return False,"stale-split-parent-missing"
                if list(parent_leaf.get("split_children") or [])!=children:
                    return False,"stale-split-active-children-mismatch"

                current_owned=set(owned_artifact_paths(parent_leaf))
                prior_owned=set()
                child_defs=txn["child_defs"]
                for child in children:
                    child_def=child_defs.get(child)
                    if not isinstance(child_def,dict):
                        return False,"stale-split-child-definition-missing"
                    prior_owned.update(owned_artifact_paths(child_def))
                    live_child=leaves.get(child)
                    if not isinstance(live_child,dict) or live_child!=child_def:
                        return False,"stale-split-live-child-drift"
                missing=sorted(current_owned-prior_owned)
                if not missing:
                    return False,"stale-split-current-ownership-already-covered"

                attempts=load_attempts()
                entry=(attempts.get("deliverables") or {}).get(parent)
                if not isinstance(entry,dict):
                    return False,"stale-split-parent-attempt-ledger-missing"
                state=attempt_state(entry)
                if not state.get("valid"):
                    return False,"stale-split-parent-attempt-ledger-invalid"
                try:
                    count=int(entry.get("count") or 0)
                except (TypeError,ValueError):
                    return False,"stale-split-parent-attempt-count-invalid"
                current_verify=str(parent_leaf.get("verify_command") or "")
                current_sha=hashlib.sha256(current_verify.encode()).hexdigest()
                revisions=entry.get("plan_contract_revisions") or []
                matching=[
                    row for row in revisions
                    if isinstance(row,dict)
                    and row.get("source")=="supervisor-plan-contract-revision"
                    and row.get("current_verify_sha256")==current_sha
                ]
                if len(matching)!=1:
                    return False,"stale-split-current-plan-revision-not-unique"
                if (
                    int(state.get("plan_contract_retry_grants") or 0)<1
                    or int(state.get("allowed_attempts") or 0)<=count
                ):
                    return False,"stale-split-no-contract-replacement-credit"
                marker=entry.get("split_required")
                if not isinstance(marker,dict):
                    return False,"stale-split-required-marker-missing"
                if int(marker.get("generation") or 0)!=int(txn.get("generation") or 0):
                    return False,"stale-split-generation-mismatch"

                overlay=load_split_leaf_overlay()
                parents=overlay.get("parents")
                overlay_entry=parents.get(parent) if isinstance(parents,dict) else None
                if (
                    not isinstance(overlay_entry,dict)
                    or overlay_entry.get("transaction_id")!=txn.get("transaction_id")
                    or overlay_entry.get("child_defs")!=child_defs
                ):
                    return False,"stale-split-overlay-mismatch"

                history=entry.setdefault("stale_split_contract_recoveries",[])
                if not isinstance(history,list):
                    return False,"stale-split-recovery-history-invalid"
                existing=next((
                    row for row in history
                    if isinstance(row,dict)
                    and row.get("transaction_id")==txn.get("transaction_id")
                    and row.get("current_verify_sha256")==current_sha
                ),None)
                if isinstance(existing,dict) and existing.get("state")=="committed":
                    return False,"stale-split-contract-already-recovered"
                if existing is None:
                    archive=_archive_split_state_for_contract_repair(parent)
                    if archive is None:
                        return False,"stale-split-archive-missing"
                    existing={
                        "protocol":"v2-stale-split-contract-recovery-v1",
                        "state":"prepared",
                        "transaction_id":txn.get("transaction_id"),
                        "generation":int(txn.get("generation") or 0),
                        "children":children,
                        "missing_current_ownership":missing,
                        "prior_owned_artifacts":sorted(prior_owned),
                        "current_owned_artifacts":sorted(current_owned),
                        "current_verify_sha256":current_sha,
                        "archive":str(archive.relative_to(project)),
                        "prepared_at":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime()),
                    }
                    history.append(existing)
                entry.pop("split_required",None)
                save_attempts(attempts)

                parents.pop(parent,None)
                overlay["parents"]=parents
                save_split_leaf_overlay(overlay)
                for child in children:
                    leaves.pop(child,None)
                parent_leaf["split_children"]=[]
                parent_leaf.pop("split_depth",None)
                raw_manifest["leaves"]=leaves
                atomic_write_json(guard_path,raw_manifest)

                _clear_split_request_state_for_contract_repair(parent)

                # Commit the transition only after both active projections are
                # gone.  The immutable archive and old child/attempt files stay.
                attempts=load_attempts()
                entry=(attempts.get("deliverables") or {}).get(parent)
                rows=entry.get("stale_split_contract_recoveries") if isinstance(entry,dict) else None
                target=next((
                    row for row in rows or []
                    if isinstance(row,dict)
                    and row.get("transaction_id")==txn.get("transaction_id")
                    and row.get("current_verify_sha256")==current_sha
                ),None)
                if not isinstance(target,dict):
                    raise StateCorruptionError(
                        f"{parent} stale split recovery record disappeared"
                    )
                target["state"]="committed"
                target["committed_at"]=time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ",time.gmtime()
                )
                save_attempts(attempts)

    log(
        f"STALE_SPLIT_CONTRACT_RETIRED parent={parent} "
        f"transaction={txn.get('transaction_id')} "
        f"missing={','.join(missing)}"
    )
    csv(
        "STALE_SPLIT_CONTRACT_RETIRED","", "supervisor",
        f"{parent} transaction={txn.get('transaction_id')} "
        f"missing={','.join(missing)}",
    )
    return True,"contract-replacement-ready"


def rearm_splits_after_parent_contract_repair():
    # Recreate a split edge only after the repaired parent plan is finalized.
    # The stale request embeds the old invalid Verify command, so it must stay
    # suppressed until the repaired plan has again passed finalization.
    if not PROJECT or not plan_ready():
        return []

    manifest=load_manifest()
    leaves=manifest.get("leaves") if isinstance(manifest.get("leaves"),dict) else {}
    rearmed=[]
    changed=False

    with dispatch_lock:
        with attempt_lock():
            data=load_attempts()
            entries=data.get("deliverables") if isinstance(data.get("deliverables"),dict) else {}
            for did,entry in entries.items():
                if not isinstance(entry,dict):
                    continue
                pending=entry.get("split_rearm_after_contract_repair")
                if not isinstance(pending,dict):
                    continue

                leaf=leaves.get(did)
                if not isinstance(leaf,dict):
                    raise StateCorruptionError(
                        f"{did} repaired parent is missing from finalized manifest"
                    )

                expected_key=str(pending.get("structured_key") or "")
                actual_key=_structured_plan_symbolic_key(did)
                if not expected_key or actual_key!=expected_key:
                    raise StateCorruptionError(
                        f"{did} structured key changed across parent contract repair "
                        f"expected={expected_key!r} actual={actual_key!r}"
                    )

                if ready_info(did) or leaf_children(did):
                    entry.pop("split_rearm_after_contract_repair",None)
                    changed=True
                    continue

                history=entry.get("failure_history",[])
                reclassified_attempts=[]
                if isinstance(history,list):
                    for item in history:
                        if not isinstance(item,dict) or not (
                            item.get("classification")=="bad-plan"
                            and item.get("reclassified_by")=="runtime-parent-contract-repair"
                        ):
                            continue
                        try:
                            attempt=int(item.get("attempt") or 0)
                        except (TypeError,ValueError):
                            continue
                        if attempt>0 and attempt not in reclassified_attempts:
                            reclassified_attempts.append(attempt)
                genuine=sum(
                    1 for item in history
                    if isinstance(item,dict)
                    and item.get("classification")=="genuine"
                ) if isinstance(history,list) else 0

                handoff_only=bool(leaf.get("split_handoff_only"))
                if (
                    recursive_split_enabled()
                    and genuine>=2
                    and split_depth(did)<MAX_SPLIT_DEPTH
                    and not handoff_only
                ):
                    try:
                        prior_generation=int(pending.get("generation") or 1)
                    except (TypeError,ValueError):
                        raise StateCorruptionError(
                            f"{did} pending repaired split generation is invalid"
                        )
                    generation=max(1,prior_generation)+1
                    entry["split_required"]={
                        "generation":generation,
                        "reason":"genuine-failure-threshold-after-contract-repair",
                        "timestamp":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime()),
                    }
                    rearmed.append(did)

                if reclassified_attempts:
                    entry["parent_contract_repair_resolution"]={
                        "structured_key":expected_key,
                        "reclassified_attempts":reclassified_attempts,
                        "timestamp":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime()),
                    }

                entry.pop("split_rearm_after_contract_repair",None)
                changed=True

            if changed:
                save_attempts(data)

    for did in rearmed:
        ok,detail=split_request(did)
        if not ok:
            raise StateCorruptionError(
                f"{did} repaired split rearm failed: {detail}"
            )
        log(
            f"PARENT_CONTRACT_REPAIR_SPLIT_REARMED deliverable={did} "
            f"detail={detail}"
        )
        csv(
            "PARENT_CONTRACT_REPAIR_SPLIT_REARMED","", "supervisor",
            f"{did} {detail}"
        )

    return rearmed


def process_split_proposal(did, session="", require_proposal=False):
    """Validate one proposal; malformed proposals receive one bounded fresh retry."""
    if not split_request_path(did).exists():
        txn=load_split_transaction(did)
        if txn.get("state")=="committed":
            return True,"accepted"
        return False,"split-request-missing"
    proposal_path=split_proposal_path(did)
    if not proposal_path.exists():
        if require_proposal:
            _,state=record_splitter_failure(
                did,"splitter-completed-without-durable-proposal",
                session=session,validation=False,
            )
            log(f"SPLIT_PROPOSAL_REJECTED parent={did} reason=missing durable proposal state={state}")
            return False,state
        return False,"proposal-pending"
    try:
        payload=load_json_object(proposal_path,label=f"split proposal {did}")
    except StateCorruptionError as exc:
        retryable,state=record_splitter_failure(
            did,str(exc),session=session,validation=True
        )
        log(f"SPLIT_PROPOSAL_REJECTED parent={did} retryable={retryable} reason={exc}")
        return False,state
    request=load_json_object(split_request_path(did),label=f"split request {did}")
    try:
        if payload.get("protocol")==SPLIT_PARENT_CONTRACT_INVALID_PROTOCOL:
            # A model cannot authorize parent repair.  A well-formed false
            # Verify assertion receives one same-claim corrective continuation;
            # malformed assertions still take the ordinary finite failure path.
            if _false_parent_contract_invalid(did,payload,request):
                status=load_split_status(did)
                if int(status.get("corrective_turn_count") or 0) < 1:
                    return begin_splitter_corrective_turn(
                        did,session,str(status.get("dispatch_token") or ""),
                        "parent-contract-invalid-unavailable",json.dumps(payload,sort_keys=True),
                    )
            return request_parent_contract_repair(did,payload,request)
        if (
            payload.get("protocol")!=SPLIT_PROPOSAL_PROTOCOL
            or payload.get("parent_id")!=did
            or payload.get("depth")!=split_depth(did)
            or payload.get("generation")!=request.get("generation",1)
        ):
            raise ValueError("proposal protocol, parent, depth, or generation does not match request")
        children=persist_split(did,payload.get("proposals"))
        log(f"SPLIT_PROPOSAL_ACCEPTED parent={did} children={','.join(children)}")
        return True,"accepted"
    except (ValueError,KeyError,TypeError) as exc:
        status=load_split_status(did)
        if session and status.get("dispatch_token") and int(status.get("corrective_turn_count") or 0) < 1:
            return begin_splitter_corrective_turn(
                did,session,str(status.get("dispatch_token") or ""),
                str(exc),json.dumps(payload,sort_keys=True),
            )
        retryable,state=record_splitter_failure(
            did,str(exc),session=session,validation=True
        )
        log(f"SPLIT_PROPOSAL_REJECTED parent={did} retryable={retryable} reason={exc}")
        return False,state


def claim_splitter(parent, dispatch_token):
    """Cross-process lease one splitter for this exact dispatch token."""
    if not valid_deliverable_id(parent) or not split_request_path(parent).exists():
        return False,"split-request-missing"
    if not dispatch_token:
        return False,"splitter-dispatch-token-missing"
    with splitter_state_lock(parent):
        with dispatch_lock:
            status=load_split_status(parent)
            state=status.get("state","split-required")
            claims=int(status.get("claim_count") or 0)
            now=time.time()
            if state=="splitter-active":
                try: lease_until=float(status.get("lease_until_epoch") or 0)
                except (TypeError,ValueError): lease_until=0
                if lease_until>now: return False,"splitter-active"
                if claims>=splitter_claim_limit(status):
                    save_split_status(parent,"splitter-failed",claim_count=claims,reason="splitter lease expired and claim budget is exhausted",lease_until_epoch=0)
                    return False,"splitter-failed"
                save_split_status(parent,"split-retryable",claim_count=claims,reason="splitter lease expired",lease_until_epoch=0)
                state="split-retryable"
            if state in {"split-validation-failed","splitter-failed","split-unavailable-read-only-parent","parent-finalize-failed"}: return False,state
            if leaf_children(parent): return False,"already-split"
            if claims>=splitter_claim_limit(status):
                save_split_status(parent,"splitter-failed",claim_count=claims,reason="splitter claim budget exhausted",lease_until_epoch=0)
                return False,"splitter-failed"
            claims+=1
            save_split_status(parent,"splitter-active",claim_count=claims,dispatch_token=dispatch_token,lease_until_epoch=now+SPLITTER_LEASE_SECONDS)
    log(f"SPLITTER_CLAIM parent={parent} generation={load_split_status(parent).get('generation',1)} token={dispatch_token} claim={claims}")
    return True,"claimed"

def claim_corrective_splitter(parent,dispatch_token):
    """Bind one native corrective child without incrementing the claim."""
    with splitter_state_lock(parent):
        status=load_split_status(parent)
        if (status.get("state")!="splitter-corrective-awaiting-output"
            or int(status.get("corrective_turn_count") or 0)!=1):
            return False,"corrective-not-pending"
        existing=str(status.get("corrective_dispatch_token") or "")
        if existing:
            return (existing==dispatch_token),("corrective-replay" if existing==dispatch_token else "corrective-already-bound")
        save_split_status(parent,"splitter-corrective-awaiting-output",
            claim_count=int(status.get("claim_count") or 0),
            corrective_dispatch_token=dispatch_token,
            corrective_dispatch_state="native-child-bound",
            lease_until_epoch=time.time()+SPLITTER_LEASE_SECONDS)
    return True,"corrective-bound"

def parse_splitter_final_json(text):
    # Accept only one exact bare JSON object and nothing else.
    text=(text or "").strip()
    if not text or text.startswith("```") or text.endswith("```"):
        return None
    try:
        obj=json.loads(text)
    except Exception:
        return None
    return obj if isinstance(obj,dict) else None


def recover_pending_splitter_completion(parent,status,now=None):
    """Recover splitter final JSON after execute.after has returned.

    OpenCode may not persist the child final assistant text until the task
    execute.after hook returns. Waiting synchronously inside that hook therefore
    cannot reliably make the text visible. Persist a pending session ID and let
    the normal 0.5s reconciliation loop read it afterward.
    """
    status=status if isinstance(status,dict) else {}
    session=str(status.get("completion_pending_session") or "")
    if not session:
        return False,"not-pending"
    token=str(status.get("completion_pending_token") or "")
    active_token=str(status.get("dispatch_token") or "")
    if token and active_token and token!=active_token:
        return False,"stale-pending-token"

    text=last_assistant_text_db(session)
    payload=parse_splitter_final_json(text)
    if isinstance(payload,dict):
        atomic_write_json(split_proposal_path(parent),payload)
        save_split_status(
            parent,"splitter-active",
            claim_count=int(status.get("claim_count") or 0),
            dispatch_token=active_token or token,
            lease_until_epoch=status.get("lease_until_epoch",0),
            completion_pending_session="",
            completion_pending_token="",
            completion_pending_deadline_epoch=0,
        )
        log(
            f"SPLIT_PROPOSAL_RECOVERED_AFTER_HOOK parent={parent} "
            f"session={session} token={token or active_token}"
        )
        return True,"proposal-recovered"

    # The hook may expose a TaskTool wrapper before the child's actual final
    # assistant text is durable.  Only classify the response once the durable
    # child-session text exists.
    if text.strip():
        save_split_status(
            parent,"splitter-active",
            claim_count=int(status.get("claim_count") or 0),
            dispatch_token=active_token or token,
            lease_until_epoch=status.get("lease_until_epoch",0),
            completion_pending_session="",
            completion_pending_token="",
            completion_pending_deadline_epoch=0,
        )
        return begin_splitter_corrective_turn(
            parent,
            session,
            token or active_token,
            "response did not contain the required bare JSON object",
            text,
        )

    now=time.time() if now is None else float(now)
    try:
        deadline=float(status.get("completion_pending_deadline_epoch") or 0)
    except (TypeError,ValueError):
        deadline=0
    if deadline and now < deadline:
        return False,"completion-pending"

    _,state=record_splitter_failure(
        parent,"splitter-completed-without-json-proposal",
        session=session,validation=False,
    )
    log(
        f"SPLIT_PROPOSAL_PERSIST_TIMEOUT parent={parent} "
        f"session={session} state={state}"
    )
    return False,state


def recover_splitter_corrective_output(parent,status):
    """Recover the one new response after the persisted corrective prompt.

    The originating false object stays in this session's history.  Comparing
    its canonical hash prevents reconciliation from treating it as a second
    response or sending another corrective prompt.
    """
    status=status if isinstance(status,dict) else {}
    session=str(status.get("corrective_session") or "")
    token=str(status.get("corrective_dispatch_token") or "")
    if not session or not token:
        return False,"corrective-state-missing-session-or-token"
    text=last_assistant_text_db(session)
    payload=parse_splitter_final_json(text)
    if not text.strip():
        return False,"corrective-output-pending"
    digest=_splitter_response_fingerprint(text)
    if digest==str(status.get("corrective_first_output_sha256") or ""):
        return False,"corrective-output-pending"
    if not isinstance(payload,dict):
        _,state=record_splitter_failure(
            parent,"corrective splitter response did not contain required bare JSON",
            session=session,validation=True,
        )
        return False,state
    atomic_write_json(split_proposal_path(parent),payload)
    save_split_status(
        parent,"splitter-corrective-processing",
        claim_count=int(status.get("claim_count") or 0),
        dispatch_token=token,
        lease_until_epoch=time.time()+SPLITTER_LEASE_SECONDS,
        corrective_dispatch_state="response-durable",
    )
    log(
        f"SPLITTER_CORRECTIVE_OUTPUT_RECOVERED parent={parent} "
        f"session={session} claim={status.get('claim_count')}"
    )
    return process_split_proposal(parent,session,require_proposal=True)


def complete_splitter(parent, session="", dispatch_token="", output_text=""):
    """Accept completion only from the currently leased splitter token."""
    with splitter_state_lock(parent):
        txn=load_split_transaction(parent)
        if txn.get("state")=="committed": return True,"accepted"
        status=load_split_status(parent)
        corrective=status.get("state")=="splitter-corrective-awaiting-output"
        expected=(status.get("corrective_dispatch_token") if corrective else status.get("dispatch_token"))
        if status.get("state") not in {"splitter-active","splitter-corrective-awaiting-output"}: return False,"stale-splitter-state"
        if not dispatch_token or expected!=dispatch_token:
            return False,"stale-splitter-completion"
        if corrective and session:
            save_split_status(parent,"splitter-corrective-awaiting-output",
                claim_count=int(status.get("claim_count") or 0),
                corrective_session=session,
                corrective_dispatch_state="native-child-completed",
                lease_until_epoch=time.time()+SPLITTER_LEASE_SECONDS)
            status=load_split_status(parent)
        if not split_request_path(parent).exists(): return False,"split-request-missing"

        payload=parse_splitter_final_json(output_text)
        source="hook-output" if isinstance(payload,dict) else ""
        if not isinstance(payload,dict) and session:
            # One non-blocking DB read can succeed on runtimes that persist the
            # child final message before execute.after. Do NOT spin here:
            # New51e demonstrated that persistence can occur only after this hook
            # returns, making synchronous sleep/retry self-defeating.
            payload=parse_splitter_final_json(last_assistant_text_db(session))
            if isinstance(payload,dict):
                source="session-db-immediate"

        if not isinstance(payload,dict):
            if not session:
                _,state=record_splitter_failure(
                    parent,"splitter-completed-without-json-proposal",
                    session=session,validation=False,
                )
                return False,state

            if corrective:
                # OpenCode v1.18.31 may persist the child's final assistant
                # text only AFTER execute.after returns.  The corrective
                # session is already durably bound above, so leave the state
                # pending and let recover_splitter_corrective_output() read
                # the final session text on the normal reconciliation tick.
                log(
                    f"SPLITTER_CORRECTIVE_COMPLETION_PENDING parent={parent} "
                    f"session={session} token={dispatch_token}"
                )
                return False,"corrective-output-pending"

            # Same persistence race for the primary splitter.  Do not mistake
            # the TaskTool wrapper from execute.after for the child's final
            # model answer.  Give SQLite the established 15-second splitter
            # persistence grace and classify the durable child text afterward.
            save_split_status(
                parent,"splitter-active",
                claim_count=int(status.get("claim_count") or 0),
                dispatch_token=dispatch_token,
                lease_until_epoch=status.get("lease_until_epoch",0),
                completion_pending_session=session,
                completion_pending_token=dispatch_token,
                completion_pending_deadline_epoch=time.time()+15,
            )
            log(
                f"SPLITTER_COMPLETION_PENDING_AFTER_HOOK parent={parent} "
                f"session={session} token={dispatch_token}"
            )
            return False,"completion-pending"

        atomic_write_json(split_proposal_path(parent),payload)
        log(
            f"SPLIT_PROPOSAL_PERSISTED_BY_SUPERVISOR parent={parent} "
            f"session={session} token={dispatch_token} source={source or 'unknown'}"
        )
        return process_split_proposal(parent,session,require_proposal=True)


def reconcile_split_proposals():
    """Crash recovery for prepared transactions and proposal/request state."""
    if not PROJECT:
        return
    reconcile_required_splits()
    reconcile_split_transactions()
    for request in (Path(PROJECT)/".opencode-v2"/"work").glob("D*.split-request.json"):
        did=request.name.removesuffix(".split-request.json")
        # Claim, completion, lease expiry, and proposal recovery mutate the same
        # per-parent state machine. Serialize all cross-process transitions on
        # the same durable parent lock.
        with splitter_state_lock(did):
            status=load_split_status(did)
            state=status.get("state")
            if state in {
                "split-validation-failed","splitter-failed",
                "split-unavailable-read-only-parent",
            }:
                continue
            if state=="splitter-corrective-awaiting-output":
                recovered,_=recover_splitter_corrective_output(did,status)
                status=load_split_status(did)
                state=status.get("state")
                if recovered or state!="splitter-corrective-awaiting-output":
                    continue
            if state=="splitter-active":
                pending_session=str(
                    status.get("completion_pending_session") or ""
                )
                if pending_session:
                    recovered,pending_detail=recover_pending_splitter_completion(
                        did,status
                    )
                    status=load_split_status(did)
                    state=status.get("state")
                    if not recovered and pending_detail=="completion-pending":
                        continue
                    if not recovered and state!="splitter-active":
                        continue

                if state=="splitter-active" and not split_proposal_path(did).exists():
                    try:
                        expired=float(status.get("lease_until_epoch") or 0) <= time.time()
                    except (TypeError,ValueError):
                        expired=True
                    if expired:
                        claims=int(status.get("claim_count") or 0)
                        if claims >= splitter_claim_limit(status):
                            save_split_status(
                                did,"splitter-failed",claim_count=claims,
                                reason="splitter lease expired and claim budget is exhausted",
                                lease_until_epoch=0,
                            )
                        else:
                            save_split_status(
                                did,"split-retryable",claim_count=claims,
                                reason="splitter lease expired",
                                lease_until_epoch=0,
                            )
            elif state=="splitter-corrective-awaiting-output":
                try:
                    expired=float(status.get("lease_until_epoch") or 0) <= time.time()
                except (TypeError,ValueError):
                    expired=True
                if expired:
                    _,terminal=record_splitter_failure(
                        did,"splitter corrective turn completed without a new bare JSON proposal",
                        session=str(status.get("corrective_session") or ""),
                        validation=False,
                    )
                    log(f"SPLITTER_CORRECTIVE_TURN_TIMEOUT parent={did} state={terminal}")
            if split_proposal_path(did).exists():
                process_split_proposal(did)


def reconcile_splits_once():
    """Run only durable split/rejoin recovery; never select or dispatch work.

    This is the restart-safe counterpart to the daemon's reconciliation loop.
    It is intentionally separate from controller execution: callers must run a
    fresh consolidated preflight before acting on any newly unblocked route.
    """
    if not PROJECT:
        raise RuntimeError("split reconciliation requires a project")
    reconcile_split_proposals()
    reconcile_split_parent_completions()
    sync_control_status_snapshot()
    work=Path(PROJECT)/".opencode-v2"/"work"
    splits={}
    for request in sorted(work.glob("D*.split-request.json")):
        did=request.name.removesuffix(".split-request.json")
        status=load_split_status(did)
        splits[did]={
            key:status[key] for key in (
                "state","generation","claim_count","proposal_failures","reason"
            ) if key in status
        }
    return {
        "protocol":"v2-split-reconcile-once-v1",
        "project":str(Path(PROJECT).resolve()),
        "splits":splits,
    }



OWNERSHIP_ATTRIBUTION_RECOVERY_MARKER="runtime-ownership-attribution-repair"


def failure_counts_as_genuine(item):
    return bool(
        isinstance(item,dict)
        and item.get("classification")=="genuine"
        and item.get("recovered_by")!=OWNERSHIP_ATTRIBUTION_RECOVERY_MARKER
    )


def _ownership_failure_paths(reason):
    text=str(reason or "")
    for prefix in (
        "ownership-violation:",
        "ownership-violation-after-verify:",
    ):
        if text.startswith(prefix):
            return [
                item.strip() for item in text[len(prefix):].split(",")
                if item.strip()
            ]
    return []


def _archive_unclaimed_split_for_ownership_recovery(did):
    paths=(
        split_request_path(did),
        split_proposal_path(did),
        split_status_path(did),
        split_transaction_path(did),
        splitter_lock_path(did),
    )
    records={}
    for path in paths:
        try:
            records[path.name]=path.read_text(errors="replace")
        except OSError:
            continue
    if not records:
        return ""
    root=(
        Path(PROJECT)/".opencode-v2"/"work"/
        "ownership-attribution-recovery-history"
    )
    root.mkdir(parents=True,exist_ok=True)
    stamp=f"{int(time.time()*1000)}-{did}"
    out=root/f"{stamp}.json"
    atomic_write_json(out,{
        "owner":"supervisor",
        "protocol":"v2-ownership-attribution-recovery-archive-v1",
        "deliverable":did,
        "records":records,
        "timestamp":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime()),
    })
    return str(out.relative_to(Path(PROJECT)))


def recover_ownership_attribution_failures(dids):
    """Recover only ownership failures proven false by durable attribution.

    The original failure row and reason remain immutable evidence.  Recovery
    adds a marker that excludes the row from genuine-failure/split counting.
    Generated Python caches are self-proving ephemeral paths; all other paths
    must belong to an overlapping sibling leaf and must not have been directly
    targeted by the failed worker.
    """
    dids=list(dict.fromkeys(dids))
    if not dids:
        raise ValueError("no deliverables requested")
    leaves=(load_manifest().get("leaves") or {})
    proofs={}
    for did in dids:
        if did not in leaves or not executable_leaf(did):
            raise ValueError(f"invalid executable deliverable {did!r}")
        entry=(load_attempts().get("deliverables") or {}).get(did)
        if not isinstance(entry,dict):
            raise ValueError(f"missing attempt ledger entry for {did}")
        if leaf_children(did):
            raise ValueError(f"{did} already has split children")
        status=load_split_status(did)
        split_state=str(status.get("state") or "")
        if split_state not in {"","split-required"}:
            raise ValueError(
                f"{did} split state already progressed: {status.get('state')}"
            )
        if split_proposal_path(did).exists() or split_transaction_path(did).exists():
            raise ValueError(f"{did} split transaction already progressed")

        rows=[]
        for item in entry.get("failure_history",[]) or []:
            if not (
                isinstance(item,dict)
                and item.get("classification")=="genuine"
                and item.get("recovered_by")!=OWNERSHIP_ATTRIBUTION_RECOVERY_MARKER
            ):
                continue
            paths=_ownership_failure_paths(item.get("reason"))
            if not paths:
                continue
            sid=str(item.get("session") or "")
            if not sid:
                continue
            status_map=v1_session_status_snapshot()
            if isinstance(status_map.get(sid),dict) and status_map[sid].get("type")=="busy":
                raise ValueError(f"{did} session still active: {sid}")
            sibling=overlapping_other_owned_paths(sid,did)
            def inside(path,roots):
                return any(
                    path==root or path.startswith(root.rstrip("/")+"/")
                    for root in roots
                )
            path_proofs=[]
            safe=True
            for path in paths:
                if generated_python_cache_path(path):
                    path_proofs.append({"path":path,"proof":"generated-python-cache"})
                    continue
                if (
                    sibling
                    and inside(path,sibling)
                    and not session_explicitly_mutated_path(sid,path)
                ):
                    path_proofs.append({"path":path,"proof":"overlapping-sibling-owned"})
                    continue
                safe=False
                break
            if not safe:
                continue
            attempt=int(item.get("attempt") or 0)
            if attempt==int(entry.get("count") or 0):
                residual=ownership_violations(did,sid)
                if residual:
                    continue
            rows.append({
                "attempt":attempt,
                "session":sid,
                "reason":item.get("reason"),
                "paths":path_proofs,
            })
        if not rows:
            raise ValueError(f"{did} has no provably false ownership failures")
        proofs[did]=rows

    archives={}
    for did in dids:
        entry=(load_attempts().get("deliverables") or {}).get(did,{})
        effective=sum(
            1 for item in entry.get("failure_history",[]) or []
            if failure_counts_as_genuine(item)
            and int(item.get("attempt") or 0) not in {
                int(row["attempt"]) for row in proofs[did]
            }
        )
        if effective<2 and (
            entry.get("split_required")
            or split_request_path(did).exists()
            or split_status_path(did).exists()
        ):
            archives[did]=_archive_unclaimed_split_for_ownership_recovery(did)

    stamp=time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime())
    with dispatch_lock:
        with attempt_lock():
            data=load_attempts()
            for did in dids:
                entry=(data.get("deliverables") or {}).get(did)
                if not isinstance(entry,dict):
                    raise ValueError(f"missing attempt ledger entry for {did}")
                by_attempt={
                    int(item.get("attempt") or 0):item
                    for item in entry.get("failure_history",[]) or []
                    if isinstance(item,dict)
                }
                for proof in proofs[did]:
                    row=by_attempt.get(int(proof["attempt"]))
                    if not (
                        isinstance(row,dict)
                        and row.get("classification")=="genuine"
                        and row.get("reason")==proof["reason"]
                    ):
                        raise StateCorruptionError(
                            f"{did} ownership failure changed during recovery"
                        )
                    row["recovered_by"]=OWNERSHIP_ATTRIBUTION_RECOVERY_MARKER
                    row["recovered_at"]=stamp
                    row["recovery_proof"]=proof["paths"]
                recoveries=entry.setdefault("ownership_attribution_recoveries",[])
                for proof in proofs[did]:
                    if not any(
                        isinstance(item,dict)
                        and int(item.get("attempt") or 0)==int(proof["attempt"])
                        for item in recoveries
                    ):
                        recoveries.append({
                            "attempt":int(proof["attempt"]),
                            "session":proof["session"],
                            "original_reason":proof["reason"],
                            "proof":proof["paths"],
                            "source":"supervisor",
                            "timestamp":stamp,
                        })
                effective=sum(
                    1 for item in entry.get("failure_history",[]) or []
                    if failure_counts_as_genuine(item)
                )
                if effective<2:
                    entry.pop("split_required",None)
            save_attempts(data)

    for did in dids:
        entry=(load_attempts().get("deliverables") or {}).get(did,{})
        effective=sum(
            1 for item in entry.get("failure_history",[]) or []
            if failure_counts_as_genuine(item)
        )
        if effective<2:
            _clear_split_request_state_for_contract_repair(did)
            splitter_lock_path(did).unlink(missing_ok=True)
        for proof in proofs[did]:
            log(
                f"OWNERSHIP_ATTRIBUTION_RECOVERED deliverable={did} "
                f"attempt={proof['attempt']} session={proof['session']}"
            )
            csv(
                "OWNERSHIP_ATTRIBUTION_RECOVERED",proof["session"],
                "supervisor",f"{did} attempt={proof['attempt']}"
            )
    return proofs,archives


def record_leaf_failure(did, reason, classification="genuine", sid=None):
    """Record a terminal worker outcome against its immutable session attempt.

    A newer dispatch may be preclaimed before an older terminal session is
    reconciled. Never classify that newer mutable count as the older session's
    failure. Callers without a concrete session retain current-count behavior
    for deterministic/unit-test state transitions.
    """
    if classification not in {"genuine","infrastructure","bad-plan"}:
        raise ValueError("invalid failure classification")
    split_needed=False
    with dispatch_lock:
        with attempt_lock():
            data=load_attempts()
            entry=(data.get("deliverables") or {}).get(did)
            if not isinstance(entry,dict):
                return False,"missing-ledger-entry"
            history=entry.setdefault("failure_history",[])
            if sid:
                sessions=entry.get("sessions")
                if not isinstance(sessions,list):
                    return False,"session-attempt-binding-invalid"
                matches=[
                    i+1 for i,value in enumerate(sessions)
                    if value==sid
                ]
                if len(matches)!=1:
                    return False,"session-attempt-binding-invalid"
                attempt=matches[0]
            else:
                attempt=int(entry.get("count") or 0)
            if attempt < 1:
                return False,"invalid-attempt"
            already=any(
                isinstance(x,dict) and x.get("attempt")==attempt
                for x in history
            )
            if not already:
                row={
                    "attempt":attempt,
                    "classification":classification,
                    "reason":reason,
                    "timestamp":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime()),
                    "source":"supervisor",
                }
                if sid:
                    row["session"]=sid
                history.append(row)
            if classification=="genuine":
                genuine=sum(
                    1 for x in history if failure_counts_as_genuine(x)
                )
                leaf=(load_manifest().get("leaves") or {}).get(did,{})
                handoff_only=isinstance(leaf,dict) and bool(leaf.get("split_handoff_only"))
                if (
                    recursive_split_enabled()
                    and genuine>=2
                    and split_depth(did)<MAX_SPLIT_DEPTH
                    and not handoff_only
                ):
                    marker=entry.get("split_required")
                    if not isinstance(marker,dict):
                        entry["split_required"]={
                            "generation":1,
                            "reason":"genuine-failure-threshold",
                            "timestamp":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime()),
                        }
                    split_needed=True
            if not already or split_needed:
                save_attempts(data)
    if already and not split_needed:
        return False,"already-recorded"
    if classification!="genuine":
        return True,classification
    if split_needed:
        return split_request(did)
    return True,"genuine-recorded"

def read_only_genuine_terminal(did):
    leaf=(load_manifest().get("leaves") or {}).get(did,{})
    if not isinstance(leaf,dict) or leaf.get("role") not in READ_ONLY_SPLIT_ROLES:
        return False
    entry=(load_attempts().get("deliverables") or {}).get(did,{})
    history=entry.get("failure_history",[]) if isinstance(entry,dict) else []
    return any(
        failure_counts_as_genuine(item)
        for item in history
    )


def retryable_unmaterialized_dispatch_entry(entry):
    """Use the canonical attempt projection for bounded zero-work replays."""
    return bool(attempt_state(entry).get("unmaterialized_dispatch_reusable"))


def note_state_blocker_exempt_active(did,reason):
    key=(str(did),str(reason or ""))
    if key in state_blocker_exempt_active_seen:
        return
    state_blocker_exempt_active_seen.add(key)
    log(
        f"STATE_BLOCKER_EXEMPT_ACTIVE deliverable={key[0]} "
        f"reason={key[1]}"
    )

def normalized_state_snapshot(project):
    data=state_snapshot(project)
    if not isinstance(data,dict):
        return data

    leaves=data.get("leaves") if isinstance(data.get("leaves"),dict) else {}
    try:
        active_sessions=active_implementation_sessions(strict=True)
        scheduler_error=""
    except Exception as exc:
        active_sessions=[]
        scheduler_error=f"{type(exc).__name__}: {exc}"
    active=active_implementation_deliverables(active_sessions)
    active_ids=set(active)
    active_count=len(active_sessions)
    try:
        attempt_data=load_attempts()
        reserved_count=reserved_dispatch_slot_count(attempt_data)
    except Exception as exc:
        attempt_data={"deliverables":{}}
        reserved_count=MAX_CONCURRENT_IMPLEMENTATION_WORKERS
        scheduler_error=scheduler_error or f"{type(exc).__name__}: {exc}"
    occupied=min(MAX_CONCURRENT_IMPLEMENTATION_WORKERS,active_count+reserved_count)
    data["scheduler"]={
        "max_concurrent_workers":MAX_CONCURRENT_IMPLEMENTATION_WORKERS,
        "active_workers":active_count,
        "reserved_workers":reserved_count,
        "occupied_worker_slots":occupied,
        "available_worker_slots":0 if scheduler_error else max(0,MAX_CONCURRENT_IMPLEMENTATION_WORKERS-active_count-reserved_count),
        "active_deliverables":sorted(active_ids),
        "error":scheduler_error,
    }
    for did in active_ids:
        if isinstance(leaves.get(did),dict):
            leaves[did]["running"]=True
            leaves[did]["eligible"]=False

    for did,leaf in leaves.items():
        if (
            isinstance(leaf,dict)
            and leaf.get("role") in READ_ONLY_SPLIT_ROLES
            and read_only_genuine_terminal(did)
            and not ready_info(did)
        ):
            leaf["eligible"]=False
            leaf["attempt_limit_reached"]=True

    clean=[]
    for item in data.get("execution_blockers",[]) if isinstance(data.get("execution_blockers"),list) else []:
        if not isinstance(item,dict):
            continue
        did=str(item.get("deliverable") or "")
        leaf=leaves.get(did) if isinstance(leaves.get(did),dict) else {}
        # New13 regression: control_state emitted attempt_limit_reached for
        # leaves whose own canonical flag was false (often attempts=0).
        if did in active_ids:
            note_state_blocker_exempt_active(did,item.get("reason",""))
            continue
        if (
            item.get("reason")=="attempt_limit_reached"
            and not leaf.get("attempt_limit_reached",False)
            and not read_only_genuine_terminal(did)
        ):
            log(
                f"STATE_BLOCKER_EXEMPT deliverable={did} reason=attempt_limit_reached "
                f"attempts={leaf.get('attempts',0)}"
            )
            continue
        clean.append(item)
    data["execution_blockers"]=clean

    # Leaf-local terminal failures must not globally stop independent work.
    # Keep each blocker visible, but continue while another leaf is legally
    # eligible. Global/state-corruption blockers remain fail-closed.
    leaf_local_blocker_reasons={
        "attempt_limit_reached",
        "parent-finalize-failed",
        "split-validation-failed",
        "splitter-failed",
        "split-unavailable-read-only-parent",
    }
    eligible_exists=any(
        isinstance(v,dict) and v.get("eligible")
        for v in leaves.values()
    )
    local_blockers_only=bool(clean) and all(
        str(item.get("deliverable") or "").startswith("D")
        and str(item.get("reason") or "") in leaf_local_blocker_reasons
        for item in clean
    )

    if data.get("resume_phase")=="execution-blocked":
        if not clean and (active_ids or eligible_exists):
            data["resume_phase"]="execution"
        elif (active_ids or eligible_exists) and local_blockers_only:
            data["resume_phase"]="execution"
            log(
                "STATE_LOCAL_BLOCKER_CONTINUE "
                f"blockers={','.join(str(x.get('deliverable') or '') for x in clean)} "
                f"active={','.join(sorted(active_ids)) or 'none'} "
                f"eligible={str(bool(eligible_exists)).lower()}"
            )

    # Reference foundation is a scheduler precondition, not a root-model memory
    # exercise. Surface it in the canonical snapshot so every transport
    # selects the same next phase.
    acceptance_complete=bool(
        isinstance(data.get("acceptance"),dict)
        and data["acceptance"].get("complete")
    )
    plan_state=data.get("plan") if isinstance(data.get("plan"),dict) else {}
    if acceptance_complete:
        policy=acceptance_reference_policy()
        gate=reference_gate_snapshot()
        data["reference"]={
            "policy":policy,
            "foundation_state":str(gate.get("state") or "not-applicable"),
            "attempts":int(gate.get("attempts") or 0),
            "max_attempts":int(gate.get("max_attempts") or 0),
            "productive_sessions":int(gate.get("productive_sessions") or 0),
            "stagnant_tail":int(gate.get("stagnant_tail") or 0),
        }
        if (
            policy=="external-required"
            and not plan_state.get("complete")
            and not plan_state.get("blocked")
        ):
            ref_state=data["reference"]["foundation_state"]
            if ref_state=="pending":
                data["resume_phase"]="reference-foundation"
            elif ref_state=="blocked":
                data["resume_phase"]="implementation-blocked"
    else:
        data["reference"]={
            "policy":"unknown",
            "foundation_state":"not-applicable",
            "attempts":0,
            "max_attempts":0,
            "productive_sessions":0,
            "stagnant_tail":0,
        }
    return apply_root_continuation_block(data)
# V2.6.9 NORMALIZED SCHEDULER STATE END

# V2.6.9 DURABLE CONTROL STATUS SNAPSHOT BEGIN
def control_state_error_payload(exc):
    return {
        "owner":"supervisor",
        "protocol":"v2-control-status-error-v1",
        "state_error":True,
        "state_error_type":type(exc).__name__,
        "state_error_message":str(exc)[:1000],
        "resume_phase":"execution-blocked",
        "execution_blockers":[{
            "deliverable":"",
            "reason":"control_state_error",
            "detail":str(exc)[:1000],
        }],
        "generated_at":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime()),
    }


def sync_control_status_snapshot():
    # Persist the authoritative derived scheduler state for the root model.
    if not PROJECT:
        return {}
    path=Path(PROJECT)/".opencode-v2"/"control-status.json"
    try:
        data=normalized_state_snapshot(PROJECT)
        if not isinstance(data,dict):
            raise StateCorruptionError("normalized scheduler snapshot is not an object")
        payload={"owner":"supervisor","state_error":False,**data}
        rendered=json.dumps(payload,indent=2,sort_keys=True)+"\n"
        if not path.exists() or path.read_text(errors="replace")!=rendered:
            atomic_write_text(path,rendered)
        manifest=(load_manifest() if payload.get("plan",{}).get("complete") else {})
        materialize_control_query_views(PROJECT,payload,manifest,rendered)
        deterministic_shadow_observe()
        return payload
    except Exception as exc:
        payload=control_state_error_payload(exc)
        rendered=json.dumps(payload,indent=2,sort_keys=True)+"\n"
        try:
            atomic_write_json(path,payload)
        except Exception as write_exc:
            log(f"CONTROL_STATUS_ERROR_WRITE_FAILED source={exc!r} write={write_exc!r}")
        try:
            materialize_control_query_views(PROJECT,payload,{},rendered)
            deterministic_shadow_observe()
        except Exception as query_exc:
            log(f"CONTROL_QUERY_VIEW_WRITE_FAILED source={exc!r} query={query_exc!r}")
        log(f"CONTROL_STATUS_SNAPSHOT_ERROR {exc!r}")
        return payload
# V2.6.9 DURABLE CONTROL STATUS SNAPSHOT END

def deterministic_shadow_observe():
    global deterministic_shadow_last_state_version
    if not PROJECT:
        return []
    if str(os.environ.get("V2_DETERMINISTIC_SHADOW","1")).strip().lower() in {"0","false","no","off"}:
        return []
    path=Path(PROJECT)/".opencode-v2/query/decision.json"
    try:
        decision=load_json_object(path,label="deterministic shadow decision")
        version=str(decision.get("state_version") or "")
        if not version or version==deterministic_shadow_last_state_version:
            return []
        actions=deterministic_select_actions(decision)
        payload={
            "owner":"supervisor",
            "protocol":"v2-deterministic-shadow-v1",
            "state_version":version,
            "resume_phase":str(decision.get("resume_phase") or ""),
            "actions":actions,
            "observed_at":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime()),
        }
        atomic_write_json(Path(PROJECT)/".opencode-v2/query/deterministic-shadow.json",payload)
        compact=json.dumps(actions,sort_keys=True,separators=(",",":"))
        log(f"DETERMINISTIC_SHADOW state={version} phase={payload['resume_phase']} actions={compact}")
        csv("DETERMINISTIC_SHADOW","","supervisor",
            f"state={version} phase={payload['resume_phase']} actions={compact}")
        deterministic_shadow_last_state_version=version
        return actions
    except Exception as exc:
        log(f"DETERMINISTIC_SHADOW_ERROR {exc!r}")
        return []

def ready_info(did):
    return state_ready_info(PROJECT,did) if PROJECT and did else {}

def plan_ready():
    return bool(PROJECT) and phase_ready(
        PROJECT,"IMPLEMENTATION_PLAN.ready","IMPLEMENTATION_PLAN.md",
        "IMPLEMENTATION_PLAN_COMPLETE",
    )

STRUCTURED_PLAN_FILENAME="IMPLEMENTATION_PLAN.structured.json"
STRUCTURED_PLAN_REPAIR_FILENAME="IMPLEMENTATION_PLAN.repair.json"

def structured_plan_path():
    return Path(PROJECT)/".opencode-v2"/STRUCTURED_PLAN_FILENAME

def _repair_baseline(keys):
    """Fingerprint only affected structured leaves for a bounded repair."""
    try:
        raw=load_json_object(structured_plan_path(),label="structured plan")
    except StateCorruptionError:
        return {"source_sha256":"","affected_leaf_sha256":{}}
    leaves=raw.get("leaves") if isinstance(raw,dict) else None
    by_key={
        str(item.get("key")):item
        for item in leaves if isinstance(leaves,list) and isinstance(item,dict)
        and isinstance(item.get("key"),str)
    }
    hashes={}
    for key in keys:
        item=by_key.get(key)
        if item is not None:
            hashes[key]=hashlib.sha256(
                json.dumps(item,sort_keys=True,separators=(",",":"),ensure_ascii=True).encode()
            ).hexdigest()
    try:
        source=hashlib.sha256(structured_plan_path().read_bytes()).hexdigest()
    except OSError:
        source=""
    return {"source_sha256":source,"affected_leaf_sha256":hashes}

def planner_plan_state(plan_path):
    """Classify durable structured-plan progress without Markdown bookkeeping."""
    path=Path(plan_path)
    try:
        raw=path.read_text(errors="replace")
    except OSError:
        return {"full_signature":None,"meaningful_signature":None,"engaged":False}
    full_signature=hashlib.sha256(raw.encode()).hexdigest()
    engaged=bool(raw.strip())
    try:
        data=json.loads(raw)
    except Exception:
        return {
            "full_signature":full_signature,
            "meaningful_signature":None,
            "engaged":engaged,
        }
    leaves=data.get("leaves") if isinstance(data,dict) else None
    meaningful=bool(
        isinstance(leaves,list)
        and any(
            isinstance(item,dict)
            and (item.get("key") or item.get("name") or item.get("outcome"))
            for item in leaves
        )
    )
    normalized=(
        json.dumps(data,sort_keys=True,separators=(",",":"))
        if isinstance(data,dict) else raw
    )
    return {
        "full_signature":full_signature,
        "meaningful_signature":(
            hashlib.sha256(normalized.encode()).hexdigest() if meaningful else None
        ),
        "engaged":engaged,
    }

def planner_session_mode(sid):
    """Classify planner mode after only recognized deterministic preambles."""
    text=strip_subagent_prefix(first_user_text_db(sid)).strip()

    # Root-authored launches sometimes prepend the exact project root before the
    # canonical planner instruction. New51g exposed that treating such a repair
    # as fresh immediately retires it against the already-invalid candidate.
    match=re.match(r"\AProject root:\s*(.+?)\s*\n+",text)
    if match:
        supplied=match.group(1).strip()
        if PROJECT and os.path.realpath(supplied)==os.path.realpath(PROJECT):
            text=text[match.end():].lstrip()

    if text.startswith("Repair structured implementation planning for this project."):
        return "repair"
    if text.startswith("Continue structured implementation planning for this project."):
        return "continue"
    return "fresh"

def planner_fresh_candidate_outcome(sid,plan_path=None):
    """Compile and retire the first syntactically valid fresh-plan candidate.

    A fresh planner may write a complete parseable source and then destroy it
    with a second monolithic self-rewrite that hits the generation cap. Hand
    off the first valid JSON candidate to deterministic compilation/guarding;
    repair/continue sessions remain free to perform bounded targeted edits.
    """
    if planner_session_mode(sid)!="fresh":
        return ""
    state=planner_checkpoints.setdefault(sid,{})
    plan=planner_plan_state(plan_path or structured_plan_path())
    signature=plan.get("meaningful_signature")
    if not signature:
        return ""
    if state.get("fresh_candidate_signature")==signature:
        return ""
    compiled=compile_structured_plan()
    if compiled and control_guard("plan") and plan_ready():
        state["fresh_candidate_signature"]=signature
        return "ready"
    repair=Path(PROJECT)/".opencode-v2"/STRUCTURED_PLAN_REPAIR_FILENAME
    if repair.exists():
        state["fresh_candidate_signature"]=signature
        return "invalid"
    # No deterministic verdict yet: leave the planner running fail-closed and
    # allow the same durable candidate to be retried on the next poll.
    return ""

def planner_progress_reason(sid,elapsed,plan_path=None,paused=False):
    """Apply the one planner-owned durable-progress policy.

    Bootstrap is immediately restartable. A checkpoint mutation proves tool
    engagement but does not buy more time: the first actual Dxxx structure is
    due within the initial grace. Thereafter only meaningful-plan fingerprints
    reset the configured stall timer.

    OpenCode compaction is infrastructure work, not planner deliberation. While
    the newest compaction row is non-terminal, freeze both the initial-progress
    and post-progress watchdog clocks instead of charging that time to the
    planner.
    """
    state=planner_checkpoints.setdefault(sid,{})
    if paused:
        state.setdefault("pause_started",float(elapsed))
        return ""

    pause_started=state.pop("pause_started",None)
    if pause_started is not None:
        paused_for=max(0.0,float(elapsed)-float(pause_started))
        state["paused_total"]=float(state.get("paused_total") or 0.0)+paused_for
    effective_elapsed=max(
        0.0,
        float(elapsed)-float(state.get("paused_total") or 0.0),
    )

    plan_path=Path(plan_path or structured_plan_path())
    plan=planner_plan_state(plan_path)
    if "baseline_full_signature" not in state:
        has_meaningful=bool(plan["meaningful_signature"])
        state.update(
            baseline_full_signature=plan["full_signature"],
            last_meaningful_signature=plan["meaningful_signature"],
            last_progress=effective_elapsed, model_progress=has_meaningful,
            engagement=plan["engaged"],
        )
        return ""
    state["engagement"]=plan["engaged"]
    if not state.get("model_progress"):
        if plan["meaningful_signature"]:
            state.update(
                model_progress=True,
                last_meaningful_signature=plan["meaningful_signature"],
                last_progress=effective_elapsed,
            )
            return ""
        if effective_elapsed<PLANNER_INITIAL_PROGRESS_GRACE_SECONDS: return ""
        if plan["full_signature"] is None:
            return f"planner_bootstrap_missing no_durable_progress={int(effective_elapsed)}s"
        return (
            f"planner_no_meaningful_plan_progress elapsed={int(effective_elapsed)}s "
            f"limit={PLANNER_INITIAL_PROGRESS_GRACE_SECONDS}s "
            f"engagement={str(state['engagement']).lower()}"
        )
    if plan["meaningful_signature"]!=state.get("last_meaningful_signature"):
        state.update(
            last_meaningful_signature=plan["meaningful_signature"],
            last_progress=effective_elapsed,
        )
        return ""
    waited=effective_elapsed-state.get("last_progress",effective_elapsed)
    if waited<PLANNER_PROGRESS_STALL_SECONDS: return ""
    return f"planner_plan_progress_stalled elapsed={int(waited)}s limit={PLANNER_PROGRESS_STALL_SECONDS}s"

def planner_restart_path(): return Path(PROJECT)/".opencode-v2/work/planner-restarts.json"

def planner_restart_count():
    data=load_json_object(
        planner_restart_path(),default_missing={"count":0},label="planner restart ledger"
    )
    try:
        count=int(data.get("count") or 0)
    except (ValueError,TypeError) as exc:
        raise StateCorruptionError("planner restart ledger count is invalid") from exc
    if count < 0:
        raise StateCorruptionError("planner restart ledger count is negative")
    return count

def planner_restart_counted_sessions(data):
    raw=data.get("counted_sessions")
    if raw is None:
        legacy=str(data.get("retired_session") or "")
        return [legacy] if legacy else []
    if (
        not isinstance(raw,list)
        or any(not isinstance(item,str) or not item for item in raw)
        or len(set(raw))!=len(raw)
    ):
        raise StateCorruptionError("planner restart counted_sessions is invalid")
    return list(raw)

def record_planner_restart(sid,reason):
    """Charge at most one restart slot to one concrete planner session."""
    if not isinstance(sid,str) or not sid:
        raise StateCorruptionError("planner restart session is invalid")
    with planner_restart_lock:
        data=load_json_object(
            planner_restart_path(),
            default_missing={"owner":"supervisor","count":0,"counted_sessions":[]},
            label="planner restart ledger",
        )
        try:
            count=int(data.get("count") or 0)
        except (ValueError,TypeError) as exc:
            raise StateCorruptionError("planner restart ledger count is invalid") from exc
        if count < 0:
            raise StateCorruptionError("planner restart ledger count is negative")
        counted=planner_restart_counted_sessions(data)
        if sid in counted or count>=MAX_PLANNER_RESTARTS:
            return False
        counted.append(sid)
        atomic_write_json(
            planner_restart_path(),
            {
                "owner":"supervisor",
                "count":count+1,
                "retired_session":sid,
                "reason":str(reason)[:1000],
                "counted_sessions":counted,
            },
        )
        return True

def planner_retirement_reason(sid,elapsed,plan_path=None,paused=False):
    """One shared planner invariant for fresh and replacement sessions."""
    if planner_restart_count()>=MAX_PLANNER_RESTARTS and not plan_ready():
        return f"planner_restart_limit={MAX_PLANNER_RESTARTS}"
    return planner_progress_reason(sid,elapsed,plan_path,paused=paused)

def _strict_owned_artifact_text(raw):
    return shared_strict_owned_artifact_paths(raw)

def _canonical_owned_artifacts(paths):
    return shared_canonical_owned_artifacts(paths)


def owned_artifact_paths(leaf):
    """Return guard-validated ownership; never infer paths from prose."""
    if not isinstance(leaf, dict):
        return []
    explicit=leaf.get("owned_artifact_paths")
    if isinstance(explicit, list):
        if not all(isinstance(path,str) for path in explicit):
            return []
        canonical=_canonical_owned_artifacts(explicit)
        checked,error=_strict_owned_artifact_text(canonical)
        return checked if not error else []
    checked,error=_strict_owned_artifact_text(leaf.get("owned_artifacts",""))
    return checked if not error else []


def verify_wait_path(did):
    return Path(PROJECT)/".opencode-v2"/"work"/f"{did}.verify-wait.json"


def missing_verify_dependencies(did):
    leaf=(load_manifest().get("leaves") or {}).get(did)
    if not isinstance(leaf,dict):
        return []
    deps=leaf.get("verify_deps") or []
    if not isinstance(deps,list):
        return []
    return [dep for dep in deps if not ready_info(dep)]


def persist_verify_wait(did,sid,missing):
    existing={}
    try:
        existing=load_verify_wait(did)
    except StateCorruptionError:
        raise
    normalized=list(missing)
    if (
        existing
        and existing.get("session","")== (sid or "")
        and existing.get("missing_verify_deps")==normalized
    ):
        return existing
    payload={
        "owner":"supervisor",
        "protocol":VERIFY_WAIT_PROTOCOL,
        "deliverable":did,
        "session":sid or "",
        "missing_verify_deps":normalized,
        "timestamp":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime()),
    }
    atomic_write_json(verify_wait_path(did),payload)
    return payload


def clear_verify_wait(did):
    verify_wait_path(did).unlink(missing_ok=True)
    verify_wait_log_state.pop(did,None)


def load_verify_wait(did):
    path=verify_wait_path(did)
    if not path.exists():
        return {}
    data=load_json_object(path,label=f"verify wait {did}")
    if data.get("owner")!="supervisor" or data.get("protocol")!=VERIFY_WAIT_PROTOCOL or data.get("deliverable")!=did:
        raise StateCorruptionError(f"verify wait {did} is invalid")
    return data


def note_verify_wait_once(did,sid,agent,detail):
    key=(sid,detail)
    if verify_wait_log_state.get(did)==key:
        return
    verify_wait_log_state[did]=key
    log(f"VERIFY_DEFERRED session={sid} agent={agent} deliverable={did} result={detail}")
    csv("VERIFY_DEFERRED",sid,agent,f"{did} {detail}")


def _paths_changed(before,after):
    changed=set(before)^set(after)
    changed.update(path for path in set(before)&set(after) if before[path]!=after[path])
    return changed


def _inside_any(path,items):
    return any(path==item or path.startswith(item.rstrip("/")+"/") for item in items)


def verify_evidence_path(did):
    return Path(PROJECT)/".opencode-v2"/"work"/f"{did}.verify-evidence.json"


def _bounded_process_text(value, limit=4000):
    if value is None:
        return ""
    if isinstance(value,bytes):
        value=value.decode("utf-8","replace")
    return str(value)[:limit]


def load_supervisor_verify_evidence(did):
    default={
        "owner":"supervisor",
        "protocol":VERIFY_EVIDENCE_PROTOCOL,
        "deliverable":did,
        "entries":[],
    }
    path=verify_evidence_path(did)
    if not path.exists():
        return default
    data=load_json_object(path,label=f"verify evidence {did}")
    if (
        data.get("owner")!="supervisor"
        or data.get("protocol")!=VERIFY_EVIDENCE_PROTOCOL
        or data.get("deliverable")!=did
        or not isinstance(data.get("entries"),list)
    ):
        raise StateCorruptionError(f"verify evidence {did} is invalid")
    return data


def reconcile_plan_contract_revisions():
    """Expire legacy completion when a new plan changes its exact Verify."""
    if not PROJECT:
        return []
    leaves=(load_manifest().get("leaves") or {})
    changed=[]
    with dispatch_lock:
        with attempt_lock():
            data=load_attempts(); entries=data.get("deliverables") or {}
            for did,leaf in leaves.items():
                if not isinstance(leaf,dict):
                    continue
                ready=Path(PROJECT)/".opencode-v2"/"work"/f"{did}.ready"
                if not ready.exists():
                    continue
                entry=entries.get(did)
                if not isinstance(entry,dict):
                    continue
                try:
                    attempt=int(entry.get("count") or 0)
                except (TypeError,ValueError):
                    continue
                command=str(leaf.get("verify_command") or "")
                latest=load_supervisor_verify_evidence(did).get("latest") or {}
                old_command=str(latest.get("command") or "") if isinstance(latest,dict) else ""
                if not command or latest.get("result")!="verified" or old_command==command:
                    continue
                digest=hashlib.sha256(command.encode()).hexdigest()
                rows=entry.setdefault("plan_contract_revisions",[])
                already=any(
                    isinstance(row,dict) and int(row.get("attempt") or 0)==attempt
                    and row.get("current_verify_sha256")==digest
                    for row in rows
                )
                if not already:
                    rows.append({
                        "attempt":attempt,
                        "source":"supervisor-plan-contract-revision",
                        "previous_verify_sha256":hashlib.sha256(old_command.encode()).hexdigest(),
                        "current_verify_sha256":digest,
                        "timestamp":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime()),
                    })
                ready.unlink(missing_ok=True)
                changed.append(did)
            if changed:
                save_attempts(data)
    for did in changed:
        log(f"PLAN_CONTRACT_READY_REVOKED deliverable={did}")
        csv("PLAN_CONTRACT_READY_REVOKED","","supervisor",did)
    return changed


def persist_supervisor_verify_evidence(did,sid,command,checked,detail,error=""):
    attempt=attempt_sequence_for_session(sid,did) if sid else 0
    if not attempt:
        entry=(load_attempts().get("deliverables") or {}).get(did,{})
        attempt=int(entry.get("count") or 0) if isinstance(entry,dict) else 0
    item={
        "attempt":int(attempt or 0),
        "session":str(sid or ""),
        "timestamp":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime()),
        "command":str(command or "")[:3000],
        "executed":checked is not None,
        "exit_code":(
            int(getattr(checked,"returncode"))
            if checked is not None and getattr(checked,"returncode",None) is not None
            else None
        ),
        "result":str(detail or "")[:1000],
        "stdout":_bounded_process_text(getattr(checked,"stdout",None)),
        "stderr":_bounded_process_text(getattr(checked,"stderr",None)),
        "error":str(error or "")[:1000],
    }
    data=load_supervisor_verify_evidence(did)
    entries=list(data.get("entries",[]))
    entries.append(item)
    data["entries"]=entries
    data["latest"]=item
    atomic_write_json(verify_evidence_path(did),data)
    return item


def run_verify_fail_closed(command,runner=subprocess.run,session=""):
    """Run leaf verification under fail-fast shell semantics.

    If the attempt used worker bash isolation, Verify reuses that exact
    session's preserved node_modules/venv/cache state instead of judging the
    implementation from a different bare-host environment.
    """
    errors=validate_verify_command(command)
    if errors:
        return None,"verify-command-unsafe:"+errors[0]

    if session:
        try:
            used_sandbox=worker_session_used_sandbox(session)
        except WorkerSandboxError as exc:
            return None,"verify-infrastructure-sandbox-state:"+str(exc)
        if used_sandbox:
            try:
                checked,mutations=worker_sandbox_run_verify_bash(
                    Path(PROJECT),session,command,timeout=240,
                    stage_test_report=(command==RUN_CHECKS_COMMAND),
                )
            except WorkerSandboxError as exc:
                return None,"verify-infrastructure-sandbox:"+str(exc)
            if mutations:
                return checked,"verify-mutated-project-sandbox:"+",".join(mutations[:4])
            if checked.returncode!=0:
                return checked,f"verify-failed-{checked.returncode}"
            return checked,"verified"

    try:
        kwargs={"cwd":PROJECT,"timeout":240}
        if runner is subprocess.run:
            kwargs.update({"capture_output":True,"text":True})
        checked=runner(
            ["/bin/bash","-euo","pipefail","-c",command],
            **kwargs,
        )
    except TypeError:
        # Small test doubles may only accept the historical signature.
        checked=runner(command,cwd=PROJECT,shell=True,executable="/bin/bash",timeout=240)
    if checked.returncode!=0:
        return checked,f"verify-failed-{checked.returncode}"
    return checked,"verified"


def execution_contract_correction_path(did):
    return (
        Path(PROJECT)/".opencode-v2"/"work"/
        f"{did}.execution-contract-correction.json"
    )


def load_supervisor_execution_contract_correction(did):
    """Load a supervisor-authored execution correction including diagnostic policy."""
    path=execution_contract_correction_path(did)
    try:
        data=json.loads(path.read_text())
    except (OSError,json.JSONDecodeError):
        return {}
    if not isinstance(data,dict):
        return {}
    correction=data.get("correction")
    digest=data.get("correction_sha256")
    if (
        data.get("owner")!="supervisor"
        or data.get("protocol")!="v2-external-execution-contract-correction-v1"
        or data.get("deliverable")!=did
        or not isinstance(correction,str)
        or not correction.strip()
        or not isinstance(digest,str)
        or digest!=hashlib.sha256(correction.encode("utf-8")).hexdigest()
    ):
        return {}
    return data


def functional_diagnostic_evidence_path(did):
    return (
        Path(PROJECT)/".opencode-v2"/"work"/
        f"{did}.functional-diagnostic-evidence.json"
    )


def _functional_json_tokens(value,keys,tokens):
    if isinstance(value,dict):
        for key,item in value.items():
            text=str(key)
            keys.append(text.lower())
            tokens.add(text.lower())
            _functional_json_tokens(item,keys,tokens)
    elif isinstance(value,list):
        for item in value:
            _functional_json_tokens(item,keys,tokens)
    elif isinstance(value,str):
        tokens.add(value.lower())


def persist_functional_diagnostic_evidence(did,sid,command,checked,result,error=""):
    item={
        "owner":"supervisor",
        "protocol":"v2-functional-diagnostic-evidence-v1",
        "deliverable":did,
        "session":str(sid or ""),
        "timestamp":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime()),
        "command":str(command or "")[:3000],
        "executed":checked is not None,
        "exit_code":(
            int(getattr(checked,"returncode"))
            if checked is not None and getattr(checked,"returncode",None) is not None
            else None
        ),
        "result":str(result or "")[:1000],
        "stdout":_bounded_process_text(getattr(checked,"stdout",None)),
        "stderr":_bounded_process_text(getattr(checked,"stderr",None)),
        "error":str(error or "")[:1000],
    }
    atomic_write_json(functional_diagnostic_evidence_path(did),item)
    return item


def run_supervisor_functional_diagnostic(did,sid="",runner=subprocess.run):
    """Run an optional supervisor-owned functional gate after canonical Verify.

    The gate is deliberately separate from the immutable leaf Verify command.
    It is allowed only when carried by a hash-valid supervisor execution
    correction and must be read-only across project artifacts.
    """
    correction=load_supervisor_execution_contract_correction(did)
    diagnostic=correction.get("functional_diagnostic")
    if not isinstance(diagnostic,dict):
        return True,"functional-diagnostic-not-required"

    command=str(diagnostic.get("command") or "").strip()
    if not command:
        return False,"functional-diagnostic-contract-invalid:missing-command"
    errors=validate_verify_command(command)
    if errors:
        return False,"functional-diagnostic-command-unsafe:"+errors[0]

    before=project_fingerprints()
    try:
        checked,detail=run_verify_fail_closed(command,runner=runner,session="")
    except (OSError,subprocess.TimeoutExpired) as exc:
        result=f"functional-diagnostic-error-{type(exc).__name__}"
        persist_functional_diagnostic_evidence(
            did,sid,command,None,result,error=str(exc)
        )
        return False,result
    after=project_fingerprints()
    changed=sorted(
        path for path in _paths_changed(before,after)
        if not supervisor_dynamic_control_path(path)
    )
    if changed:
        result="functional-diagnostic-mutated-project:"+",".join(changed[:4])
        persist_functional_diagnostic_evidence(did,sid,command,checked,result)
        return False,result
    if detail!="verified":
        result="functional-diagnostic-"+detail
        persist_functional_diagnostic_evidence(did,sid,command,checked,result)
        return False,result

    contract=diagnostic.get("stdout_json")
    if isinstance(contract,dict):
        raw=str(getattr(checked,"stdout","") or "")
        if not raw.strip():
            result="functional-diagnostic-empty-json-output"
            persist_functional_diagnostic_evidence(did,sid,command,checked,result)
            return False,result
        try:
            parsed=json.loads(raw)
        except json.JSONDecodeError as exc:
            result="functional-diagnostic-invalid-json-output"
            persist_functional_diagnostic_evidence(
                did,sid,command,checked,result,error=str(exc)
            )
            return False,result
        keys=[]
        tokens=set()
        _functional_json_tokens(parsed,keys,tokens)
        missing_tokens=[
            str(value) for value in diagnostic["stdout_json"].get(
                "required_tokens",[]
            )
            if str(value).lower() not in tokens
        ]
        if missing_tokens:
            result=(
                "functional-diagnostic-json-missing-tokens:"+
                ",".join(missing_tokens[:8])
            )
            persist_functional_diagnostic_evidence(did,sid,command,checked,result)
            return False,result
        for group in contract.get("required_key_substring_groups",[]):
            if not isinstance(group,list) or not group:
                continue
            choices=[str(value).lower() for value in group if str(value)]
            if choices and not any(
                any(choice in key for choice in choices) for key in keys
            ):
                result=(
                    "functional-diagnostic-json-missing-key-group:"+
                    "|".join(choices[:8])
                )
                persist_functional_diagnostic_evidence(
                    did,sid,command,checked,result
                )
                return False,result

    persist_functional_diagnostic_evidence(
        did,sid,command,checked,"functional-verified"
    )
    return True,"functional-verified"


def ownership_baseline_path(did):
    return Path(PROJECT)/".opencode-v2/work"/f"{did}.ownership-baseline.json"

def generated_python_cache_path(path):
    """True for interpreter bytecode/cache artifacts, never durable project work."""
    p=Path(path)
    return "__pycache__" in p.parts or p.suffix in {".pyc",".pyo"}

def project_fingerprints():
    """Fingerprint regular project files for a claimed leaf's ownership check."""
    root=Path(PROJECT); result={}
    for path in root.rglob("*"):
        if not path.is_file() or ".git" in path.parts:
            continue
        relative=path.relative_to(root).as_posix()
        if relative.startswith(".opencode-v2/work/"):
            continue
        if generated_python_cache_path(relative):
            continue
        try:
            result[relative]=hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            continue
    return result

def write_ownership_baseline(did):
    """Capture state before the provider can run the claimed worker."""
    path=ownership_baseline_path(did); path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"owner":"supervisor","deliverable":did,
                               "files":project_fingerprints()},sort_keys=True)+"\n")
    os.replace(tmp,path)

def execution_baseline_path(did,attempt):
    return Path(PROJECT)/".opencode-v2/work"/f"{did}.attempt-{int(attempt)}.execution-baseline.json"

def execution_scope_fingerprints(did):
    """Fingerprint only this leaf's durable owned/progress scope."""
    root=Path(PROJECT)
    leaf=(load_manifest().get("leaves") or {}).get(did,{})
    roots=list(owned_artifact_paths(leaf))+[f".opencode-v2/work/{did}.progress.md"]
    result={}
    for rel in roots:
        path=root/rel
        key=rel.rstrip("/")
        if not path.exists():
            result[key]="missing"
            continue
        if path.is_file():
            try:
                result[key]="file:"+hashlib.sha256(path.read_bytes()).hexdigest()
            except OSError:
                result[key]="unreadable"
            continue
        if path.is_dir():
            result[key]="dir"
            for child in sorted(path.rglob("*")):
                if not child.is_file():
                    continue
                relchild=child.relative_to(root).as_posix()
                if generated_python_cache_path(relchild):
                    continue
                try:
                    result[relchild]="file:"+hashlib.sha256(child.read_bytes()).hexdigest()
                except OSError:
                    result[relchild]="unreadable"
            continue
        result[key]="other"
    return result

def write_execution_baseline(did,attempt):
    path=execution_baseline_path(did,attempt)
    atomic_write_json(path,{
        "owner":"supervisor",
        "deliverable":did,
        "attempt":int(attempt),
        "files":execution_scope_fingerprints(did),
    })

def ensure_execution_baseline(did,attempt):
    path=execution_baseline_path(did,attempt)
    if path.exists():
        try:
            data=load_json_object(path,label=f"execution baseline {did} attempt {attempt}")
            if (
                data.get("owner")=="supervisor"
                and data.get("deliverable")==did
                and int(data.get("attempt") or 0)==int(attempt)
                and isinstance(data.get("files"),dict)
            ):
                return
        except Exception:
            pass
    write_execution_baseline(did,attempt)

def attempt_sequence_for_session(sid,did):
    cached=session_task.get(sid)
    if cached and cached[0]==did:
        try:
            return int(cached[1])
        except (TypeError,ValueError):
            pass
    data=load_attempts(); entry=(data.get("deliverables") or {}).get(did)
    if not isinstance(entry,dict):
        return 0
    sessions=entry.get("sessions")
    if not isinstance(sessions,list):
        return 0
    try:
        return sessions.index(sid)+1
    except ValueError:
        return 0

def session_window(sid):
    if v1_runtime_enabled():
        try:
            con=db_connect()
            row=con.execute(
                "SELECT time_created FROM session WHERE id=?",
                (sid,),
            ).fetchone()
            con.close()
            if not row:
                return None
            start=int(row[0] or 0)
            active=_v1_active_session_ids(strict=False)
            if sid in active or not _v1_session_terminal(sid,active):
                return start,int(time.time()*1000)
            end=_v1_session_end_ms(sid) or int(time.time()*1000)
            return start,end
        except Exception:
            return None
    try:
        con=db_connect()
        row=con.execute(
            "SELECT time_created,time_idle FROM session_v2 WHERE id=?",
            (sid,),
        ).fetchone()
        con.close()
        if not row:
            return None
        start=int(row[0] or 0)
        end=int(row[1] or int(time.time()*1000))
        return start,end
    except Exception:
        return None


def session_tool_inputs(sid):
    out=[]
    if v1_runtime_enabled():
        try:
            for mid,role,_created in _v1_message_rows(sid):
                if role!="assistant":
                    continue
                for part in _v1_message_parts(mid):
                    if part.get("type")!="tool":
                        continue
                    state=part.get("state") if isinstance(part.get("state"),dict) else {}
                    inp=state.get("input") if isinstance(state.get("input"),dict) else {}
                    out.append((str(part.get("tool") or ""),inp))
            return out
        except Exception:
            return []
    try:
        con=db_connect()
        rows=con.execute(
            "SELECT data FROM session_message "
            "WHERE session_id=? AND type='assistant' ORDER BY seq",
            (sid,),
        ).fetchall()
        con.close()
        for (raw,) in rows:
            try:
                d=json.loads(raw)
            except Exception:
                continue
            for part in d.get("content",[]) if isinstance(d,dict) else []:
                if not isinstance(part,dict) or part.get("type")!="tool":
                    continue
                state=part.get("state") if isinstance(part.get("state"),dict) else {}
                inp=state.get("input") if isinstance(state.get("input"),dict) else {}
                out.append((str(part.get("name") or part.get("tool") or ""),inp))
    except Exception:
        pass
    return out

def session_explicitly_mutated_path(sid,path):
    """Best-effort proof that THIS worker MUTATED path, not merely read it.

    Direct edit/write/apply_patch tools are authoritative. Shell commands only
    count when the path is coupled to an obvious mutating operation/redirection.
    Plain ls/cat/head/stat/sha256sum/grep references are never mutations.
    """
    if not sid:
        return False
    rel=path.lstrip("./")
    absolute=str(Path(PROJECT)/rel)
    quoted=[rel,absolute]

    def direct_path_matches(value):
        if not isinstance(value,str):
            return False
        value=value.replace("\\","/")
        return value==rel or value.endswith("/"+rel) or value==absolute

    for name,inp in session_tool_inputs(sid):
        if name in {"edit","write","apply_patch"}:
            for key in ("path","file","filePath","filename"):
                if direct_path_matches(inp.get(key)):
                    return True
            continue

        if name not in {"shell","bash"}:
            continue
        command=inp.get("command")
        if not isinstance(command,str) or not any(x in command for x in quoted):
            continue

        # Only obvious shell writes count. Read-only mentions do not.
        for target in quoted:
            q=re.escape(target)
            patterns=[
                rf"(?:>|>>)\s*['\"]?{q}(?:['\"]|\s|$)",
                rf"\btee(?:\s+-a)?\s+['\"]?{q}(?:['\"]|\s|$)",
                rf"\b(?:touch|truncate|rm|unlink)\b[^\n;]*{q}",
                rf"\b(?:sed\s+-i|perl\s+-pi)\b[^\n;]*{q}",
                rf"\b(?:cp|mv|install)\b[^\n;]*\s['\"]?{q}(?:['\"]|\s|$)",
            ]
            if any(re.search(p,command) for p in patterns):
                return True
    return False

def overlapping_other_owned_paths(sid,did):
    """Owned paths of sibling sessions overlapping this leaf's attribution window."""
    win=session_window(sid)
    if not win or not PROJECT:
        return set()
    start,_idle=win
    end=max(int(_idle or 0),int(time.time()*1000))
    result=set()

    if v1_runtime_enabled():
        try:
            active=_v1_active_session_ids(strict=False)
            con=db_connect()
            rows=con.execute(
                "SELECT id,time_created FROM session "
                "WHERE parent_id IS NOT NULL AND directory=? AND id<>?",
                (PROJECT,sid),
            ).fetchall()
            con.close()
        except Exception:
            return result
        leaves=(load_manifest().get("leaves") or {})
        for other,created in rows:
            ostart=int(created or 0)
            terminal=_v1_session_terminal(other,active)
            oend=(
                _v1_session_end_ms(other)
                if terminal else int(time.time()*1000)
            )
            oend=int(oend or int(time.time()*1000))
            if ostart>end or oend<start:
                continue
            odid=parse_deliverable(strip_subagent_prefix(first_user_text_db(other)))
            if not odid or odid==did:
                continue
            leaf=leaves.get(odid)
            if not isinstance(leaf,dict):
                continue
            result.update(owned_artifact_paths(leaf))
        return result

    try:
        con=db_connect()
        rows=con.execute(
            "SELECT id,time_created,time_idle FROM session_v2 "
            "WHERE parent_id IS NOT NULL AND directory=? AND id<>?",
            (PROJECT,sid),
        ).fetchall()
        con.close()
    except Exception:
        return result
    leaves=(load_manifest().get("leaves") or {})
    for other,created,idle in rows:
        ostart=int(created or 0)
        oend=int(idle or int(time.time()*1000))
        if ostart>end or oend<start:
            continue
        odid=parse_deliverable(strip_subagent_prefix(first_user_text_db(other)))
        if not odid or odid==did:
            continue
        leaf=leaves.get(odid)
        if not isinstance(leaf,dict):
            continue
        result.update(owned_artifact_paths(leaf))
    return result


# V2.6.9 SUPERVISOR DYNAMIC OWNERSHIP EXEMPTION BEGIN
SUPERVISOR_DYNAMIC_CONTROL_PATHS={
    ".opencode-v2/control-status.json",
    ".opencode-v2/reference-gate.json",
    ".opencode-v2/reference-validation-gate.json",
    ".opencode-v2/IMPLEMENTATION_PLAN.guard.json",
    ".opencode-v2/work/stage-a-controller-executions.json",
    ".opencode-v2/work/stage-a-controller.lock",
}
SUPERVISOR_DYNAMIC_CONTROL_PREFIXES=(
    ".opencode-v2/query/",
)

def supervisor_dynamic_control_path(path):
    if path in SUPERVISOR_DYNAMIC_CONTROL_PATHS:
        return True
    if any(path.startswith(prefix) for prefix in SUPERVISOR_DYNAMIC_CONTROL_PREFIXES):
        return True
    # Supervisor atomic writes use same-directory temporary names such as
    # .opencode-v2/.control-status.json.geel2cvt.tmp. They can appear/disappear
    # while a worker ownership baseline is live and are not worker mutations.
    # Explicit worker targeting is still checked by ownership_violations(), and
    # Bubblewrap remains the shell/direct-write containment boundary.
    for canonical in SUPERVISOR_DYNAMIC_CONTROL_PATHS:
        parent,name=canonical.rsplit("/",1)
        if path.startswith(f"{parent}/.{name}.") and path.endswith(".tmp"):
            return True
    return False
# V2.6.9 SUPERVISOR DYNAMIC OWNERSHIP EXEMPTION END

def ownership_violations(did,sid=""):
    """Return changes attributable to this leaf outside declared ownership.

    Whole-project snapshots are retained, but files owned by a sibling session
    that ran after this leaf was dispatched and before finalization are not
    blamed on an already-idle worker unless this worker's own tool INPUT
    explicitly targeted that path. This covers both true overlap and deferred
    Verify finalization while retaining fail-closed worker containment.
    """
    try:
        baseline=json.loads(ownership_baseline_path(did).read_text())
        before=baseline.get("files") if baseline.get("owner")=="supervisor" else None
        if not isinstance(before,dict): return ["invalid ownership baseline"]
        # Baselines written before the bytecode-cache exclusion may contain
        # ephemeral .pyc/__pycache__ entries. Normalize those historical
        # snapshots to the same durable-project domain as current fingerprints.
        before={
            path:digest for path,digest in before.items()
            if not generated_python_cache_path(path)
        }
    except FileNotFoundError:
        return []
    except Exception:
        return ["invalid ownership baseline"]
    leaf=(load_manifest().get("leaves") or {}).get(did,{})
    allowed=owned_artifact_paths(leaf)+[f".opencode-v2/work/{did}.progress.md"]
    after=project_fingerprints(); changed=set(before)^set(after)
    changed.update(path for path in set(before)&set(after) if before[path]!=after[path])
    sibling_owned=overlapping_other_owned_paths(sid,did) if sid else set()
    def inside(path,items):
        return any(path==item or path.startswith(item.rstrip("/")+"/") for item in items)
    violations=[]
    for path in sorted(changed):
        if inside(path,allowed):
            continue
        if supervisor_dynamic_control_path(path) and not session_explicitly_mutated_path(sid,path):
            log(f"OWNERSHIP_SUPERVISOR_EXEMPT session={sid} deliverable={did} path={path}")
            continue
        if sibling_owned and inside(path,sibling_owned) and not session_explicitly_mutated_path(sid,path):
            log(f"OWNERSHIP_CONCURRENT_EXEMPT session={sid} deliverable={did} path={path}")
            continue
        violations.append(path)
    return violations
def persisted_completed_tool_turns(sid):
    if v1_runtime_enabled():
        try:
            turns=0
            for mid,role,_created in _v1_message_rows(sid):
                if role!="assistant":
                    continue
                completed=False
                for part in _v1_message_parts(mid):
                    if part.get("type")!="tool":
                        continue
                    state=part.get("state") if isinstance(part.get("state"),dict) else {}
                    if str(state.get("status") or "").lower() in {"completed","error"}:
                        completed=True
                        break
                if completed:
                    turns+=1
            return turns
        except Exception:
            return 0
    try:
        con=db_connect()
        rows=con.execute(
            "SELECT data FROM session_message "
            "WHERE session_id=? AND type='assistant' ORDER BY seq",
            (sid,),
        ).fetchall()
        con.close()
    except Exception:
        return 0
    turns=0
    for (raw,) in rows:
        try:
            data=json.loads(raw) if raw else {}
        except Exception:
            continue
        content=data.get("content") if isinstance(data,dict) else []
        if not isinstance(content,list):
            continue
        if any(
            isinstance(part,dict)
            and part.get("type")=="tool"
            and isinstance(part.get("state"),dict)
            and str(part["state"].get("status") or "").lower() in {"completed","error"}
            for part in content
        ):
            turns+=1
    return turns

def _session_agent_db(sid):
    try:
        table=session_table_name()
        con=db_connect()
        row=con.execute(f"SELECT agent FROM {table} WHERE id=?",(sid,)).fetchone()
        con.close()
        return str(row[0] or "") if row else ""
    except Exception:
        return ""


def _owned_artifact_changed_since_execution_baseline(did,attempt):
    if not attempt: return False,"attempt-missing"
    path=execution_baseline_path(did,attempt)
    try:
        baseline=load_json_object(path,label=f"execution baseline {did} attempt {attempt}")
    except Exception:
        return False,"baseline-missing"
    before=baseline.get("files") if isinstance(baseline,dict) else None
    if not isinstance(before,dict): return False,"baseline-invalid"
    roots=owned_artifact_paths((load_manifest().get("leaves") or {}).get(did,{}))
    if not roots: return False,"no-owned-artifacts"
    after=execution_scope_fingerprints(did)
    def pick(data):
        return {key:value for key,value in data.items() if _path_inside_any(key,roots)}
    changed=pick(before)!=pick(after)
    return changed,"changed" if changed else "unchanged"


def early_write_gate_state(sid):
    """Fail closed before a fifth tool-bearing turn for writing leaves."""
    agent=_session_agent_db(sid)
    if agent not in IMPLEMENTATION_AGENTS:
        return "na","not-implementation-worker"
    did=parse_deliverable(strip_subagent_prefix(first_user_text_db(sid)))
    leaf=(load_manifest().get("leaves") or {}).get(did,{})
    if not did or not isinstance(leaf,dict):
        return "na","no-canonical-leaf"
    if agent in READ_ONLY_SPLIT_ROLES or not owned_artifact_paths(leaf):
        return "na","read-only-or-progress-only"
    turns=persisted_completed_tool_turns(sid)
    deadline=early_write_completed_turn_limit(leaf)
    if turns < deadline:
        return "allow",f"completed_tool_turns={turns} deadline={deadline}"
    if ready_info(did):
        return "satisfied","leaf-ready"
    attempt=attempt_sequence_for_session(sid,did)
    if not attempt:
        entry=(load_attempts().get("deliverables") or {}).get(did,{})
        attempt=int(entry.get("count") or 0) if isinstance(entry,dict) else 0
    changed,detail=_owned_artifact_changed_since_execution_baseline(did,attempt)
    if changed:
        return "satisfied",f"owned-artifact-delta attempt={attempt}"
    return "deny",(
        f"early_write_gate completed_tool_turns={turns} "
        f"required={deadline} complexity={str(leaf.get('complexity') or 'S').upper()} "
        f"detail={detail}"
    )


def _tool_targets_exact_progress_file(did,tool,args):
    if tool not in {"write","edit","apply_patch","patch","multiedit"}:
        return False
    target=f".opencode-v2/work/{did}.progress.md"
    raw=json.dumps(args if isinstance(args,dict) else {},sort_keys=True)
    return target in raw


def _project_relative_tool_path(value):
    if not isinstance(value,str) or not value.strip():
        return ""
    value=value.strip().replace("\\","/")
    project=str(Path(PROJECT).resolve(strict=False)).replace("\\","/").rstrip("/")
    if value.startswith(project+"/"):
        value=value[len(project)+1:]
    if value.startswith("./"):
        value=value[2:]
    path=Path(value)
    if path.is_absolute() or ".." in path.parts:
        return ""
    return path.as_posix()


def _target_path_values(args):
    result=[]
    def walk(value,key=""):
        if isinstance(value,dict):
            for k,v in value.items():
                if k in {"path","file","filePath","file_path","filename"} and isinstance(v,str):
                    result.append(v)
                else:
                    walk(v,k)
        elif isinstance(value,list):
            for item in value:
                walk(item,key)
    walk(args if isinstance(args,dict) else {})
    return result


def _current_tool_mutates_owned_artifact(did,tool,args):
    leaf=(load_manifest().get("leaves") or {}).get(did,{})
    owned=owned_artifact_paths(leaf) if isinstance(leaf,dict) else []
    if not owned:
        return False
    direct={"write","edit","apply_patch","patch","multiedit"}
    if tool in direct:
        normalized=[
            _project_relative_tool_path(value)
            for value in _target_path_values(args)
        ]
        if any(path and _path_inside_any(path,owned) for path in normalized):
            return True
        raw=json.dumps(args if isinstance(args,dict) else {},sort_keys=True)
        return any(path in raw for path in owned)

    if tool not in {"bash","shell","execute"}:
        return False
    command=(args or {}).get("command") if isinstance(args,dict) else ""
    if not isinstance(command,str):
        return False
    for rel in owned:
        candidates=[
            rel,
            str((Path(PROJECT)/rel).resolve(strict=False)).replace("\\","/"),
        ]
        for target in candidates:
            q=re.escape(target)
            patterns=(
                rf"(?:>|>>)\s*['\"]?{q}(?:['\"]|\s|$)",
                rf"\btee(?:\s+-a)?\s+['\"]?{q}(?:['\"]|\s|$)",
                rf"\b(?:touch|truncate|rm|unlink)\b[^\n;]*{q}",
                rf"\b(?:sed\s+-i|perl\s+-pi)\b[^\n;]*{q}",
                rf"\b(?:cp|mv|install)\b[^\n;]*\s['\"]?{q}(?:['\"]|\s|$)",
                rf"\bopen\s*\(\s*['\"]{q}['\"]\s*,\s*['\"][wax+]",
                rf"\bPath\s*\(\s*['\"]{q}['\"]\s*\)\s*\.\s*write_(?:text|bytes)\b",
            )
            if any(re.search(pattern,command) for pattern in patterns):
                return True
    return False


def session_completed_tool_inputs(sid):
    out=[]
    if v1_runtime_enabled():
        try:
            for mid,role,_created in _v1_message_rows(sid):
                if role!="assistant":
                    continue
                for part in _v1_message_parts(mid):
                    if part.get("type")!="tool":
                        continue
                    state=part.get("state") if isinstance(part.get("state"),dict) else {}
                    if str(state.get("status") or "").lower() not in {"completed","error"}:
                        continue
                    inp=state.get("input") if isinstance(state.get("input"),dict) else {}
                    out.append((str(part.get("tool") or ""),inp))
            return out
        except Exception:
            return []
    try:
        con=db_connect()
        rows=con.execute(
            "SELECT data FROM session_message "
            "WHERE session_id=? AND type='assistant' ORDER BY seq",
            (sid,),
        ).fetchall()
        con.close()
        for (raw,) in rows:
            try:
                data=json.loads(raw) if raw else {}
            except Exception:
                continue
            content=data.get("content") if isinstance(data,dict) else []
            if not isinstance(content,list):
                continue
            for part in content:
                if not isinstance(part,dict) or part.get("type")!="tool":
                    continue
                state=part.get("state") if isinstance(part.get("state"),dict) else {}
                if str(state.get("status") or "").lower() not in {"completed","error"}:
                    continue
                inp=state.get("input") if isinstance(state.get("input"),dict) else {}
                out.append((str(part.get("name") or part.get("tool") or ""),inp))
    except Exception:
        pass
    return out

def _tool_targets_exact_project_path(tool,args,target):
    if tool not in {"read","write","edit","apply_patch","patch","multiedit"}:
        return False
    target=target.replace("\\","/")
    target_abs=str((Path(PROJECT)/target).resolve(strict=False)).replace("\\","/")
    for raw in _target_path_values(args):
        value=str(raw).replace("\\","/")
        if value in {target,"./"+target,target_abs}:
            return True
    return False


def progress_handoff_tool_state(sid,tool,args):
    agent=_session_agent_db(sid)
    if agent!="probe-builder":
        return "na","not-probe-builder"
    did=parse_deliverable(strip_subagent_prefix(first_user_text_db(sid)))
    leaf=(load_manifest().get("leaves") or {}).get(did,{})
    if not did or not isinstance(leaf,dict) or not leaf.get("split_handoff_only"):
        return "na","not-progress-handoff"

    context=f".opencode-v2/query/leaves/{did}-context.json"
    progress=f".opencode-v2/work/{did}.progress.md"
    history=session_completed_tool_inputs(sid)

    def is_context_read(item):
        name,inp=item
        return name=="read" and _tool_targets_exact_project_path(name,inp,context)

    def is_progress_write(item):
        name,inp=item
        return _tool_targets_exact_progress_file(did,name,inp)

    context_seen=any(is_context_read(item) for item in history)
    last_progress=-1
    for index,item in enumerate(history):
        if is_progress_write(item):
            last_progress=index

    current_progress_write=_tool_targets_exact_progress_file(did,tool,args)
    if last_progress < 0:
        if not history:
            if tool=="read" and _tool_targets_exact_project_path(tool,args,context):
                return "allow","bootstrap-context-read"
            return "deny","PROGRESS_CONTEXT_REQUIRED"
        if current_progress_write and context_seen:
            return "allow","bootstrap-checkpoint-write"
        return "deny","PROGRESS_BOOTSTRAP_CHECKPOINT_REQUIRED"

    progress_path=Path(PROJECT)/progress
    try:
        progress_text=progress_path.read_text(errors="replace")
    except OSError:
        progress_text=""
    if not split_handoff_progress_checkpoint(progress_text):
        if current_progress_write:
            return "allow","repair-invalid-checkpoint"
        return "deny","PROGRESS_CHECKPOINT_REQUIRED"

    if current_progress_write:
        return "allow","checkpoint-write"

    noncheckpoint_after=[
        item for item in history[last_progress+1:]
        if not is_progress_write(item)
    ]
    if not noncheckpoint_after:
        return "allow","one-discovery-after-checkpoint"
    return "deny","PROGRESS_CHECKPOINT_REQUIRED"


def _current_tool_directly_mutates_owned_artifact(did,tool,args):
    """Probe direct-write boundary: editor mutation only, never shell mutation."""
    if tool not in {"write","edit","apply_patch","patch","multiedit"}:
        return False
    return _current_tool_mutates_owned_artifact(did,tool,args)


def probe_direct_write_gate_state(sid,tool="",args=None):
    """After two probe turns without owned progress, require a direct editor write.

    This is a recoverable tool boundary, unlike the later hard early-write
    retirement. A denied discovery tool remains visible to the model so its
    next response can switch to the required owned-artifact write.
    """
    agent=_session_agent_db(sid)
    if agent!="probe-builder":
        return "na","not-probe-builder"
    did=parse_deliverable(strip_subagent_prefix(first_user_text_db(sid)))
    leaf=(load_manifest().get("leaves") or {}).get(did,{})
    if (
        not did or not isinstance(leaf,dict)
        or leaf.get("split_handoff_only")
        or not owned_artifact_paths(leaf)
    ):
        return "na","not-owned-artifact-probe"

    turns=persisted_completed_tool_turns(sid)
    if turns < 2:
        return "allow",f"probe_direct_write completed_tool_turns={turns} required=2"
    if ready_info(did):
        return "satisfied","probe leaf-ready"

    attempt=attempt_sequence_for_session(sid,did)
    if not attempt:
        entry=(load_attempts().get("deliverables") or {}).get(did,{})
        attempt=int(entry.get("count") or 0) if isinstance(entry,dict) else 0
    changed,detail=_owned_artifact_changed_since_execution_baseline(did,attempt)
    if changed:
        return "satisfied",f"probe owned-artifact-delta attempt={attempt}"
    if _current_tool_directly_mutates_owned_artifact(did,tool,args):
        return "probe-write-only",(
            f"probe_direct_write completed_tool_turns={turns} "
            f"owned_artifact_write={did}"
        )
    return "probe-write-required",(
        f"PROBE_WRITE_REQUIRED deliverable={did} completed_tool_turns={turns} "
        f"required=2 detail={detail} next_tool=direct-owned-artifact-write"
    )


def implementation_progress_read_marker(did,attempt):
    return (
        Path(PROJECT)/".opencode-v2"/"work"/
        f"{did}.attempt-{int(attempt)}.implementation-progress-read.json"
    )


def implementation_progress_read_available(did,attempt):
    progress=Path(PROJECT)/".opencode-v2"/"work"/f"{did}.progress.md"
    return (
        int(attempt or 0)>0
        and progress.is_file()
        and not implementation_progress_read_marker(did,attempt).exists()
    )


def write_implementation_progress_read_marker(did,attempt,sid,source):
    marker=implementation_progress_read_marker(did,attempt)
    atomic_write_json(marker,{
        "owner":"supervisor",
        "protocol":"v2-implementation-progress-read-once-v1",
        "deliverable":did,
        "attempt":int(attempt),
        "session":sid,
        "path":f".opencode-v2/work/{did}.progress.md",
        "source":source,
        "timestamp":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime()),
    })
    return marker


def persisted_implementation_progress_read_seen(sid,did):
    progress_rel=f".opencode-v2/work/{did}.progress.md"
    try:
        records=_v1_message_records(sid)
    except Exception:
        records=[]
    for record in records:
        if record.get("data",{}).get("role")!="assistant":
            continue
        for part in _v1_message_parts(record["id"]):
            if part.get("type")!="tool" or str(part.get("tool") or "")!="read":
                continue
            state=part.get("state") if isinstance(part.get("state"),dict) else {}
            if str(state.get("status") or "").lower()!="completed":
                continue
            raw=state.get("input")
            if isinstance(raw,dict):
                tool_args=raw
            elif isinstance(raw,str):
                try:
                    tool_args=json.loads(raw)
                except Exception:
                    tool_args={}
            else:
                tool_args={}
            if _tool_targets_exact_project_path("read",tool_args,progress_rel):
                return True
    return False


def reconcile_implementation_progress_read_marker(sid,did,attempt):
    marker=implementation_progress_read_marker(did,attempt)
    if marker.exists():
        return True
    if int(attempt or 0)<1:
        return False
    if not persisted_implementation_progress_read_seen(sid,did):
        return False
    write_implementation_progress_read_marker(
        did,attempt,sid,"persisted-completed-read-reconciliation"
    )
    return True


def implementation_direct_write_gate_state(sid,tool="",args=None):
    """After context inspection, steer implementation leaves to an owned write.

    A pre-existing progress file may contain supervisor/predecessor state that
    cannot fit on one JSON context line. Permit exactly one direct read of that
    exact file before requiring the owned write. All other discovery remains
    denied.
    """
    agent=_session_agent_db(sid)
    if agent not in IMPLEMENTATION_AGENTS or agent in READ_ONLY_SPLIT_ROLES:
        return "na","not-writing-implementation-worker"
    did=parse_deliverable(strip_subagent_prefix(first_user_text_db(sid)))
    leaf=(load_manifest().get("leaves") or {}).get(did,{})
    if not did or not isinstance(leaf,dict) or not owned_artifact_paths(leaf):
        return "na","not-owned-artifact-implementation"
    attempt=attempt_sequence_for_session(sid,did)
    if not attempt:
        entry=(load_attempts().get("deliverables") or {}).get(did,{})
        attempt=int(entry.get("count") or 0) if isinstance(entry,dict) else 0

    progress_rel=f".opencode-v2/work/{did}.progress.md"
    reconcile_implementation_progress_read_marker(sid,did,attempt)
    progress_available=implementation_progress_read_available(did,attempt)
    if (
        progress_available
        and tool=="read"
        and _tool_targets_exact_project_path(tool,args,progress_rel)
    ):
        write_implementation_progress_read_marker(
            did,attempt,sid,"current-tool-preexecution"
        )
        turns=persisted_completed_tool_turns(sid)
        return "implementation-progress-read-once",(
            f"implementation_direct_write completed_tool_turns={turns} "
            f"allowed_once=read {progress_rel} next_tool=direct-owned-artifact-write"
        )

    turns=persisted_completed_tool_turns(sid)
    if turns < 1:
        return "allow",f"implementation_direct_write completed_tool_turns={turns} required=1"
    if ready_info(did):
        return "satisfied","implementation leaf-ready"
    changed,detail=_owned_artifact_changed_since_execution_baseline(did,attempt)
    if changed:
        return "satisfied",f"implementation owned-artifact-delta attempt={attempt}"
    if _current_tool_directly_mutates_owned_artifact(did,tool,args):
        return "implementation-write-only",(
            f"implementation_direct_write completed_tool_turns={turns} "
            f"owned_artifact_write={did}"
        )

    progress_hint=(
        f" allowed_once=read {progress_rel} then direct-owned-artifact-write"
        if progress_available else ""
    )
    return "implementation-write-required",(
        f"IMPLEMENTATION_WRITE_REQUIRED deliverable={did} completed_tool_turns={turns} "
        f"required=1 detail={detail} next_tool=direct-owned-artifact-write"
        f"{progress_hint}"
    )


def enforce_early_write_gate(sid,tool="",args=None):
    """Retire at the exact S/M deadline, except one final progress-file write."""
    implementation_state,implementation_detail=implementation_direct_write_gate_state(
        sid,tool,args
    )
    if implementation_state=="implementation-write-required":
        did=parse_deliverable(strip_subagent_prefix(first_user_text_db(sid)))
        leaf=(load_manifest().get("leaves") or {}).get(did,{})
        turns=persisted_completed_tool_turns(sid)
        deadline=early_write_completed_turn_limit(leaf)
        attempt=attempt_sequence_for_session(sid,did) if did else 0
        if not attempt and did:
            entry=(load_attempts().get("deliverables") or {}).get(did,{})
            attempt=int(entry.get("count") or 0) if isinstance(entry,dict) else 0
        # A pre-existing progress file gets exactly one direct read. Once that
        # durable context has been exposed, the ordinary hard early-write
        # deadline becomes reachable instead of being masked forever by the
        # recoverable direct-write steering branch.
        if (
            did and turns>=deadline
            and not implementation_progress_read_available(did,attempt)
        ):
            agent=_session_agent_db(sid)
            reason=(
                "implementation_direct_write_noncompliance "
                f"completed_tool_turns={turns} deadline={deadline} "
                f"deliverable={did}"
            )
            set_abort_intent(sid,reason,agent,"requested")
            log(
                f"PLUGIN_INTERRUPT_REQUESTED session={sid} "
                f"agent={agent} reason={reason}"
            )
            csv("PLUGIN_INTERRUPT_REQUESTED",sid,agent,reason)
            return "deny",reason
        log(f"IMPLEMENTATION_WRITE_REQUIRED session={sid} {implementation_detail}")
        csv("IMPLEMENTATION_WRITE_REQUIRED",sid,_session_agent_db(sid),implementation_detail)
        return implementation_state,implementation_detail
    if implementation_state in {
        "implementation-write-only",
        "implementation-progress-read-once",
        "satisfied",
    }:
        return implementation_state,implementation_detail

    probe_state,probe_detail=probe_direct_write_gate_state(sid,tool,args)
    if probe_state=="probe-write-required":
        log(f"PROBE_WRITE_REQUIRED session={sid} {probe_detail}")
        csv("PROBE_WRITE_REQUIRED",sid,"probe-builder",probe_detail)
        return probe_state,probe_detail
    if probe_state in {"probe-write-only","satisfied"}:
        return probe_state,probe_detail

    state,detail=early_write_gate_state(sid)
    if state!="deny":
        return state,detail
    did=parse_deliverable(strip_subagent_prefix(first_user_text_db(sid)))
    leaf=(load_manifest().get("leaves") or {}).get(did,{})
    deadline=early_write_completed_turn_limit(leaf)
    turns=persisted_completed_tool_turns(sid)
    if did and turns>=deadline:
        if _tool_targets_exact_progress_file(did,tool,args):
            return "final-progress",(
                f"completed_tool_turns={turns} deadline={deadline} exact_progress_write={did}"
            )
        if _current_tool_mutates_owned_artifact(did,tool,args):
            return "write-only",(
                f"completed_tool_turns={turns} deadline={deadline} owned_artifact_write={did}"
            )
    agent=_session_agent_db(sid)
    reason=(
        "early_write_deadline_no_owned_artifact_delta "
        f"{detail}"
    )
    # The CLI guard persists intent. The in-process plugin performs the native
    # session interrupt and then confirms the durable intent.
    set_abort_intent(sid,reason,agent,"requested")
    log(f"PLUGIN_INTERRUPT_REQUESTED session={sid} agent={agent} reason={reason}")
    csv("PLUGIN_INTERRUPT_REQUESTED",sid,agent,reason)
    return "deny",reason


def confirm_plugin_interrupt(sid):
    if not PROJECT or not sid:
        return False,"missing-project-or-session"
    data=load_abort_intents()
    entry=(data.get("sessions") or {}).get(sid)
    if not isinstance(entry,dict) or entry.get("state")!="requested":
        return False,"no-requested-abort-intent"
    set_abort_intent(
        sid,
        str(entry.get("reason") or "plugin-native-interrupt"),
        str(entry.get("agent") or ""),
        "confirmed",
    )
    return True,"confirmed"


def splitter_tool_boundary_state(sid,tool,args):
    if _session_agent_db(sid)!="task-splitter":
        return "na","not-task-splitter"
    parent=parse_split_parent(first_user_text_db(sid))
    if not parent:
        return "deny","missing canonical SPLIT_PARENT"
    turns=persisted_completed_tool_turns(sid)
    if turns != 0:
        return "deny",f"task-splitter already completed {turns} tool-bearing turn(s)"
    if tool!="read":
        return "deny",f"task-splitter first tool must be read, got {tool}"
    values=[]
    if isinstance(args,dict):
        for key in ("filePath","file_path","path","filename"):
            value=args.get(key)
            if isinstance(value,str):
                values.append(value)
    expected=f".opencode-v2/work/{parent}.split-request.json"
    expected_abs=str((Path(PROJECT)/expected).resolve(strict=False)).replace("\\","/")
    allowed={expected,"./"+expected,expected_abs}
    normalized={str(v).replace("\\","/") for v in values}
    if not normalized.intersection(allowed):
        return "deny",f"task-splitter may read only {expected}"
    return "allow",expected


def probe_loop_reason(sid,agent,did,tool_id,now=None):
    """Require recurring durable progress during bounded probe discovery.

    Any change to this leaf's owned/progress signature resets the existing
    five-tool-turn window. Progress-only split children are not permanently
    exempt after HANDOFF_READY:false: their progress file is their durable
    artifact, so new evidence must continue to be checkpointed.
    """
    if agent!="probe-builder" or not did: return ""
    now=time.monotonic() if now is None else now
    signature=durable_progress_signature(agent,did)
    persisted_turns=persisted_completed_tool_turns(sid)
    leaf=(load_manifest().get("leaves") or {}).get(did,{})
    handoff_only=bool(isinstance(leaf,dict) and leaf.get("split_handoff_only"))
    if handoff_only:
        # Progress-only children are governed by the execute.before state machine.
        # Do not recycle the whole session for a denied second discovery.
        return ""
    limit=PROBE_MAX_TOOL_TURNS_WITHOUT_DURABLE_PROGRESS
    state=worker_progress.get(sid)
    if state is None:
        # First observation establishes the durable-progress baseline at the
        # already-persisted tool-turn count. Do not retroactively charge turns
        # that happened before this signature was first observed.
        worker_progress[sid]={
            "signature":signature,
            "baseline_turns":persisted_turns,
            "turns":0,
        }
        return ""
    if signature!=state["signature"]:
        state.update(
            signature=signature,baseline_turns=persisted_turns,turns=0
        )
        return ""
    state["turns"]=max(0,persisted_turns-int(state.get("baseline_turns") or 0))
    if state["turns"]>=limit:
        return ("probe_research_loop_no_owned_progress "
                f"tool_turns={state['turns']} limit={limit}")
    return ""

def supervisor_finalize_ready(did, verified_command=""):
    """Atomically mint the standard Dxxx.ready after supervisor-side checks."""
    if ready_info(did):
        return True,"already-complete"
    try:
        with attempt_lock():
            data=load_attempts()
            entry=(data.get("deliverables") or {}).get(did)
            state=attempt_state(entry)
            if not isinstance(entry,dict) or not state.get("valid"):
                return False,"attempt-ledger-invalid"
            count=int(state.get("count") or entry.get("count") or 0)
            allowed=int(state.get("allowed_attempts") or count)
            if count < 1 or count > allowed:
                return False,f"attempt-count-invalid-{count}-of-{allowed}"

        path=Path(PROJECT)/".opencode-v2"/"work"/f"{did}.ready"
        path.parent.mkdir(parents=True,exist_ok=True)
        body=(
            "status=complete\n"
            f"deliverable={did}\n"
            f"attempt={count}\n"
            "verified=true\n"
            "owner=supervisor\n"
            f"verify_sha256={hashlib.sha256((verified_command or str((load_manifest().get('leaves') or {}).get(did,{}).get('verify_command') or '')).encode()).hexdigest()}\n"
            f"protocol={LEAF_READY_PROTOCOL}\n"
        )
        tmp=path.with_suffix(".tmp")
        tmp.write_text(body)
        os.replace(tmp,path)
        log(f"SUPERVISOR_LEAF_READY deliverable={did} attempt={count}")
        csv("SUPERVISOR_LEAF_READY","","supervisor",f"{did} attempt={count}")
        return True,"finalized"
    except Exception as exc:
        return False,f"ready-write-error-{type(exc).__name__}"

def post_session_finalize(did,sid="",runner=subprocess.run,verify_command_override=""):
    """Fail-closed finalization after artifacts, ownership, deps, and Verify pass.

    A non-empty verify_command_override is reserved for supervisor-owned split-
    parent recovery. All normal artifact, dependency, ownership, mutation, and
    ready-provenance checks still run; only the executable Verify command changes.
    """
    leaf=(load_manifest().get("leaves") or {}).get(did)
    if not leaf or ready_info(did):
        clear_verify_wait(did)
        return False,"not-applicable"
    split_parent_without_session=bool(
        not sid
        and isinstance(leaf.get("split_children"),list)
        and leaf.get("split_children")
    )
    if sid and worker_sandbox_has_fatal_violation(Path(PROJECT),did,sid):
        clear_verify_wait(did)
        return False,"sandbox-ownership-violation"
    paths=owned_artifact_paths(leaf)
    progress=Path(PROJECT)/".opencode-v2/work"/f"{did}.progress.md"
    handoff_only=bool(leaf.get("split_handoff_only"))
    read_only_no_artifacts=(
        leaf.get("role") in READ_ONLY_SPLIT_ROLES and not paths
    )
    if handoff_only:
        if leaf.get("role")!="probe-builder" or paths:
            clear_verify_wait(did)
            return False,"split-handoff-contract-invalid"
        try:
            handoff_text=progress.read_text(errors="replace")
        except OSError:
            handoff_text=""
        if not split_handoff_progress_complete(handoff_text):
            return False,"split-handoff-progress-incomplete"
    elif not read_only_no_artifacts and (
        not paths or any(not (Path(PROJECT)/path).exists() for path in paths)
    ):
        if progress.exists() and progress.stat().st_size:
            return False,"durable-progress-incomplete"
        return False,"owned-artifacts-missing"

    # A split parent has no executing worker session at collapse time. Its
    # original whole-project ownership baseline can span unrelated concurrent
    # leaves and therefore cannot attribute later project changes to the parent.
    # Each child has already passed its own session-bound ownership checks.
    if not split_parent_without_session:
        violations=ownership_violations(did,sid)
        if violations:
            clear_verify_wait(did)
            return False,"ownership-violation:"+",".join(violations[:4])

    missing=missing_verify_dependencies(did)
    if missing:
        persist_verify_wait(did,sid,missing)
        return False,"verify-deps-pending:"+",".join(missing)

    command=(verify_command_override or leaf.get("verify_command") or "").strip()
    command_errors=validate_verify_command(command)
    if command_errors:
        detail="verify-command-unsafe:"+command_errors[0]
        persist_supervisor_verify_evidence(
            did,sid,command,None,detail,error=command_errors[0]
        )
        clear_verify_wait(did)
        return False,detail

    before_verify=project_fingerprints()
    try:
        checked,detail=run_verify_fail_closed(command,runner=runner,session=sid)
    except (OSError,subprocess.TimeoutExpired) as e:
        detail=f"verification-error-{type(e).__name__}"
        persist_supervisor_verify_evidence(
            did,sid,command,None,detail,error=str(e)
        )
        clear_verify_wait(did)
        return False,detail
    persist_supervisor_verify_evidence(did,sid,command,checked,detail)
    if detail!="verified":
        clear_verify_wait(did)
        return False,detail

    after_verify=project_fingerprints()
    changed=_paths_changed(before_verify,after_verify)
    if split_parent_without_session:
        # Split-parent collapse has no worker session to blame for historical
        # project changes, so enforce ownership transactionally around Verify:
        # the parent Verify itself must be read-only across the project. Ignore
        # only supervisor-owned dynamic control files that may change
        # concurrently while the verification command runs.
        verify_changed=sorted(
            path for path in changed
            if not supervisor_dynamic_control_path(path)
        )
        if verify_changed:
            clear_verify_wait(did)
            return (
                False,
                "verify-mutated-project-artifacts:"+",".join(verify_changed[:4]),
            )
    else:
        owned_changed=sorted(path for path in changed if _inside_any(path,paths))
        if owned_changed:
            clear_verify_wait(did)
            return False,"verify-mutated-owned-artifacts:"+",".join(owned_changed[:4])

        violations=ownership_violations(did,sid)
        if violations:
            clear_verify_wait(did)
            return False,"ownership-violation-after-verify:"+",".join(violations[:4])

    if sid and command==RUN_CHECKS_COMMAND:
        try:
            if worker_session_used_sandbox(sid):
                worker_sandbox_commit_verify_outputs(Path(PROJECT),sid)
        except WorkerSandboxError as exc:
            clear_verify_wait(did)
            return False,"verify-report-commit-failed:"+str(exc)

    functional_ok,functional_detail=run_supervisor_functional_diagnostic(
        did,sid=sid,runner=runner
    )
    if not functional_ok:
        clear_verify_wait(did)
        return False,functional_detail

    ok,detail=supervisor_finalize_ready(did,command)
    if ok:
        clear_verify_wait(did)
    return ok,detail

# V2.6.9 GAMETESTNEW6 SPLIT-PARENT FINALIZATION BEGIN
_split_parent_finalize_next = {}


def split_parent_recovery_tester(parent,children,leaves):
    """Return one strict read-only child verifier eligible to recover parent Verify.

    This is intentionally narrow. Splitter prose may not authorize readiness. A
    fallback candidate must be a supervisor-validated tester child that inherited
    the parent's semantic contract exactly, owns nothing, depends on a sibling,
    is already READY, and uses a different safe Verify command. The command is
    re-run by the supervisor before parent readiness can be created.
    """
    parent_leaf=leaves.get(parent)
    if not isinstance(parent_leaf,dict) or not isinstance(children,list) or len(children)!=2:
        return ""
    parent_outcome=str(parent_leaf.get("outcome") or "")
    parent_acceptance=tuple(sorted(parent_leaf.get("acceptance_ids") or []))
    parent_verify=str(parent_leaf.get("verify_command") or "").strip()
    candidates=[]
    for child_id in children:
        child=leaves.get(child_id)
        if not isinstance(child,dict):
            continue
        if child.get("parent")!=parent or child.get("role")!="tester":
            continue
        if owned_artifact_paths(child):
            continue
        if str(child.get("outcome") or "")!=parent_outcome:
            continue
        if tuple(sorted(child.get("acceptance_ids") or []))!=parent_acceptance:
            continue
        siblings=[item for item in children if item!=child_id]
        launch_deps=child.get("launch_deps") or []
        if not siblings or siblings[0] not in launch_deps:
            continue
        command=str(child.get("verify_command") or "").strip()
        if not command or command==parent_verify or validate_verify_command(command):
            continue
        if not ready_info(child_id):
            continue
        candidates.append(child_id)
    return candidates[0] if len(candidates)==1 else ""


def reconcile_split_parent_completions():
    """Finalize split parents with a finite, persistent failure budget."""
    if not PROJECT:
        return
    leaves=(load_manifest().get("leaves") or {})
    now=time.monotonic()
    parents=[]
    for did,leaf in leaves.items():
        if not isinstance(leaf,dict):
            continue
        children=leaf.get("split_children")
        if isinstance(children,list) and children:
            parents.append((split_depth(did),did,children))
    parents.sort(reverse=True)

    for _depth,did,children in parents:
        if ready_info(did):
            _split_parent_finalize_next.pop(did,None)
            continue
        status=load_split_status(did)
        if status.get("state")=="parent-finalize-failed":
            continue
        if not all(ready_info(child) for child in children):
            continue
        if now < _split_parent_finalize_next.get(did,0):
            continue

        missing=missing_verify_dependencies(did)
        if missing:
            detail="verify-deps-pending:"+",".join(missing)
            note_verify_wait_once(did,"","supervisor",detail)
            _split_parent_finalize_next[did]=time.monotonic()+2.0
            continue
        verify_wait_log_state.pop(did,None)

        ok,detail=post_session_finalize(did)
        original_detail=detail
        recovery_child=""
        recovery_command=""
        if not ok and detail.startswith("verify-failed-"):
            recovery_child=split_parent_recovery_tester(did,children,leaves)
            if recovery_child:
                recovery_command=str(
                    (leaves.get(recovery_child) or {}).get("verify_command") or ""
                ).strip()
                recovered,recovery_detail=post_session_finalize(
                    did,verify_command_override=recovery_command
                )
                log(
                    f"SPLIT_PARENT_VERIFY_RECOVERY parent={did} "
                    f"tester={recovery_child} original={detail} "
                    f"recovery={recovery_detail}"
                )
                csv(
                    "SPLIT_PARENT_VERIFY_RECOVERY","","supervisor",
                    f"{did} tester={recovery_child} original={detail} "
                    f"recovery={recovery_detail}",
                )
                ok,detail=recovered,recovery_detail

        log(
            f"SPLIT_PARENT_FINALIZE parent={did} "
            f"children={','.join(children)} result={detail}"
        )
        csv(
            "SPLIT_PARENT_FINALIZE","","supervisor",
            f"{did} children={','.join(children)} result={detail}",
        )
        if ok:
            _split_parent_finalize_next.pop(did,None)
            accepted_detail={
                "children":children,
                "parent_finalize_failures":0,
                "parent_finalize_last_result":"finalized",
            }
            if recovery_child:
                accepted_detail.update({
                    "parent_finalize_mode":"split-readonly-verifier-recovery",
                    "parent_verify_original_result":original_detail,
                    "parent_verify_recovery_child":recovery_child,
                    "parent_verify_recovery_command_sha256":hashlib.sha256(
                        recovery_command.encode("utf-8")
                    ).hexdigest(),
                })
            save_split_status(did,"accepted",**accepted_detail)
            continue

        failures=int(status.get("parent_finalize_failures") or 0)+1
        if failures >= MAX_SPLIT_PARENT_FINALIZE_FAILURES:
            save_split_status(
                did,"parent-finalize-failed",
                children=children,
                parent_finalize_failures=failures,
                parent_finalize_last_result=detail,
                reason=detail,
            )
            _split_parent_finalize_next.pop(did,None)
        else:
            save_split_status(
                did,"parent-finalize-retry",
                children=children,
                parent_finalize_failures=failures,
                parent_finalize_last_result=detail,
            )
            _split_parent_finalize_next[did]=time.monotonic()+10.0

def record_infrastructure_abort(sid,did,reason,kind="runtime-cancel"):
    """Record one bounded infrastructure recovery without rewriting history.

    A supervisor/runtime cancellation is infrastructure even when useful
    partial state already exists. Preserve that state and resume from it.

    If a successor dispatch placeholder was preclaimed before this terminal
    session was reconciled, the ordinary ledger projection can be temporarily
    invalid. Recover only that exact one-successor race, and only when adding
    this session's infrastructure grant (plus its session-bound failure row
    when absent) makes the projected ledger valid and the successor placeholder
    reusable.
    """
    if not did or ready_info(did): return False,"already-complete-or-unknown"
    leaf=(load_manifest().get("leaves") or {}).get(did)
    paths=owned_artifact_paths(leaf)
    progress=Path(PROJECT)/".opencode-v2/work"/f"{did}.progress.md"
    durable_present=bool(
        (paths and any((Path(PROJECT)/path).exists() for path in paths))
        or progress.exists()
    )
    race_recovered=False
    timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime())
    with dispatch_lock:
        with attempt_lock():
            data=load_attempts()
            entry=(data.get("deliverables") or {}).get(did)
            sessions=entry.get("sessions",[]) if isinstance(entry,dict) else []
            if not isinstance(entry,dict) or sid not in sessions:
                return False,"session-not-in-ledger"
            failures=entry.setdefault("infrastructure_failures",[])
            if not isinstance(failures,list):
                return False,"attempt-ledger-invalid"
            if any(isinstance(item,dict) and item.get("session")==sid for item in failures):
                return False,"already-recorded"

            state=attempt_state(entry)
            if not state["valid"]:
                # Narrow recovery for: terminal attempt N is being classified
                # after attempt N+1 has already been reserved but not
                # materialized. Never repair an arbitrary invalid ledger.
                try:
                    count=int(entry.get("count") or 0)
                    matches=[i+1 for i,value in enumerate(sessions) if value==sid]
                    grants=int(entry.get("infrastructure_retry_grants") or 0)
                except (TypeError,ValueError):
                    return False,"attempt-ledger-invalid"
                current=sessions[-1] if sessions and isinstance(sessions[-1],str) else ""
                if (
                    len(matches)!=1
                    or matches[0] != count-1
                    or len(sessions) != count
                    or not current.startswith("dispatch:")
                    or grants >= MAX_INFRASTRUCTURE_RETRY_GRANTS
                ):
                    return False,"attempt-ledger-invalid"

                trial=copy.deepcopy(entry)
                history=trial.setdefault("failure_history",[])
                if not isinstance(history,list):
                    return False,"attempt-ledger-invalid"
                attempt=matches[0]
                rows=[
                    item for item in history
                    if isinstance(item,dict)
                    and int(item.get("attempt") or 0)==attempt
                ]
                if len(rows)>1:
                    return False,"attempt-ledger-invalid"
                if rows:
                    row=rows[0]
                    if (
                        row.get("classification")!="infrastructure"
                        or row.get("session") not in (None,sid)
                        or str(row.get("reason") or "")!=str(reason or "")
                    ):
                        return False,"attempt-ledger-invalid"
                    row.setdefault("session",sid)
                else:
                    history.append({
                        "attempt":attempt,
                        "classification":"infrastructure",
                        "reason":reason,
                        "timestamp":timestamp,
                        "source":"supervisor",
                        "session":sid,
                    })

                trial_failures=trial.setdefault("infrastructure_failures",[])
                trial_failures.append({
                    "timestamp":timestamp,
                    "grant":1,
                    "source":"supervisor",
                    "kind":kind,
                    "session":sid,
                    "evidence":("durable-partial-state-preserved" if durable_present
                                else "no-owned-artifact-or-progress"),
                    "reason":reason,
                })
                trial["infrastructure_retry_grants"]=grants+1
                projected=attempt_state(trial)
                if not (
                    projected.get("valid")
                    and projected.get("unmaterialized_dispatch_reusable")
                ):
                    return False,"attempt-ledger-invalid"
                data["deliverables"][did]=trial
                save_attempts(data)
                race_recovered=True
            else:
                if state["infrastructure_retry_grants"] >= MAX_INFRASTRUCTURE_RETRY_GRANTS:
                    return False,"infrastructure-retry-limit"
                failures.append({
                    "timestamp":timestamp,
                    "grant":1,
                    "source":"supervisor",
                    "kind":kind,
                    "session":sid,
                    "evidence":("durable-partial-state-preserved" if durable_present
                                else "no-owned-artifact-or-progress"),
                    "reason":reason,
                })
                entry["infrastructure_retry_grants"]=state["infrastructure_retry_grants"]+1
                save_attempts(data)
    if race_recovered:
        log(
            f"INFRASTRUCTURE_PRECLAIM_RACE_RECOVERY session={sid} "
            f"deliverable={did} kind={kind} grant=1 reason={reason}"
        )
        csv(
            "INFRASTRUCTURE_PRECLAIM_RACE_RECOVERY",sid,"supervisor",
            f"{did} {kind} grant=1 reason={reason}",
        )
    log(f"INFRASTRUCTURE_RETRY_GRANT session={sid} deliverable={did} kind={kind} grant=1 reason={reason}")
    csv("INFRASTRUCTURE_RETRY_GRANT",sid,"supervisor",f"{did} {kind} grant=1 reason={reason}")
    return True,("granted-preclaimed-successor-recovery" if race_recovered else "granted")

def record_compaction_infrastructure_failure(sid,did):
    """Compatibility wrapper for a failed beta compaction template."""
    return record_infrastructure_abort(sid,did,"opencode-compaction-template","opencode-compaction-template")

def latest_compaction_state(sid):
    """Return newest compaction transition in beta or native v1 history."""
    if v1_runtime_enabled():
        try:
            return _v1_latest_compaction_state(sid)
        except Exception:
            return {"seq":0,"status":"","error_type":""}
    try:
        con=db_connect()
        row=con.execute(
            "SELECT seq,data FROM session_message "
            "WHERE session_id=? AND type='compaction' ORDER BY seq DESC LIMIT 1",
            (sid,),
        ).fetchone()
        con.close()
        if not row:
            return {"seq":0,"status":"","error_type":""}
        data=json.loads(row[1]) if row[1] else {}
        error=data.get("error") if isinstance(data.get("error"),dict) else {}
        return {
            "seq":int(row[0] or 0),
            "status":str(data.get("status") or ""),
            "error_type":str(error.get("type") or ""),
        }
    except Exception:
        return {"seq":0,"status":"","error_type":""}


def compaction_failure(sid):
    state=latest_compaction_state(sid)
    return state["error_type"] if state.get("status")=="failed" else ""

def durable_progress_signature(agent,did=""):
    paths=[]
    if agent=="implementation-planner":
        paths=[Path(PROJECT)/".opencode-v2/IMPLEMENTATION_PLAN.structured.json"]
    elif agent=="reference-researcher":
        ctrl=Path(PROJECT)/".opencode-v2"
        paths=[
            ctrl/"acceptance"/"reference-evidence.json",
            ctrl/"acceptance"/"reference-work.json",
            ctrl/"acceptance"/"reference-items",
            ctrl/"REFERENCE_FOUNDATION.md",
        ]
    elif did:
        leaf=(load_manifest().get("leaves") or {}).get(did,{})
        paths=[Path(PROJECT)/p for p in owned_artifact_paths(leaf)]
        paths.append(Path(PROJECT)/".opencode-v2/work"/f"{did}.progress.md")
    signature=[]
    for path in paths:
        try:
            st=path.stat(); signature.append((str(path),st.st_mtime_ns,st.st_size))
        except OSError: signature.append((str(path),0,0))
    return tuple(signature)

def session_activity_signature(sid):
    """Persisted OpenCode heartbeat for invisible-stream fallback."""
    if v1_runtime_enabled():
        try:
            con=db_connect()
            m=con.execute(
                "SELECT COALESCE(MAX(time_updated),-1),COUNT(*) "
                "FROM message WHERE session_id=?",
                (sid,),
            ).fetchone()
            p=con.execute(
                "SELECT COALESCE(MAX(time_updated),-1),COUNT(*) "
                "FROM part WHERE session_id=?",
                (sid,),
            ).fetchone()
            con.close()
            return (
                int((m or (-1,0))[0] or -1),
                int((m or (-1,0))[1] or 0),
                int((p or (-1,0))[0] or -1),
                int((p or (-1,0))[1] or 0),
            )
        except Exception:
            return (-1,0,-1,0)
    try:
        con=db_connect()
        row=con.execute(
            "SELECT COALESCE(MAX(seq),-1),COUNT(*) "
            "FROM session_message WHERE session_id=?",
            (sid,),
        ).fetchone()
        con.close()
        return tuple(row or (-1,0))
    except Exception:
        return (-1,0)


def fallback_no_progress_reason(
    sid,agent,did,observable,backend_snapshot=None,now=None
):
    """Progress-aware invisible-stream watchdog.

    Local persisted/durable progress always resets the timer.  Once the old
    600-second threshold is reached, server-global vLLM/GPU telemetry may grant
    a bounded extension when the backend is demonstrably computing, emitting
    tokens, or queueing work.  Global telemetry never suppresses retirement
    forever because it cannot always be attributed to one of several requests.
    """
    state=event_watch.setdefault(
        sid,{"stop":threading.Event(),"created":time.monotonic()}
    )
    if observable:
        state.pop("fallback_since",None)
        state.pop("fallback_signature",None)
        state.pop("fallback_decision",None)
        state["fallback_elapsed"]=0.0
        return ""
    now=time.monotonic() if now is None else now
    signature=(durable_progress_signature(agent,did),session_activity_signature(sid))
    if signature!=state.get("fallback_signature"):
        state["fallback_signature"]=signature
        state["fallback_since"]=now
        state["fallback_decision"]={"phase":"local-persisted-progress","abort":False}
        return ""
    state.setdefault("fallback_since",now)
    elapsed=max(0.0,now-state["fallback_since"])
    decision=invisible_watchdog_decision(elapsed,backend_snapshot or {})
    state["fallback_decision"]=decision
    state["fallback_elapsed"]=elapsed
    if decision.get("abort"):
        reason=decision.get("reason") or "backend-not-progressing"
        phase=decision.get("phase") or "backend-unknown"
        limit=int(decision.get("limit") or 600)
        return (
            f"invisible_stream_stalled={int(elapsed)}s "
            f"phase={phase} limit={limit}s reason={reason}"
        )
    return ""


def effective_fallback_reason(
    sid,agent,did,observable,backend_snapshot=None,now=None
):
    """Keep invisible-stream fallback from contradicting planner supervision."""
    if agent=="implementation-planner":
        return ""
    return fallback_no_progress_reason(
        sid,agent,did,observable,backend_snapshot=backend_snapshot,now=now
    )


def session_watchdog_phase(shape,live,backend_snapshot,visible_age=0.0):
    """Best-effort phase label for observability; never used as sole truth."""
    if shape.get("tool_running") or (live or {}).get("tool_running"):
        return "tool"
    live=live or {}
    last_progress=live.get("last_progress")
    if isinstance(last_progress,(int,float)) and time.monotonic()-last_progress <= 5:
        if int(live.get("reasoning") or 0)>0:
            return "reasoning"
        if int(live.get("text") or 0)>0:
            return "decode"
        return "model-progress"
    if shape.get("reasoning_tokens") or shape.get("reasoning"):
        if visible_age < HARD_SECONDS:
            return "reasoning-idle"
    if shape.get("output_tokens") or shape.get("text"):
        if visible_age < HARD_SECONDS:
            return "decode-idle"
    return backend_phase(backend_snapshot or {})


def emit_watchdog_telemetry(sid,row,backend_snapshot,force=False):
    """Bounded 5-second JSONL telemetry for postmortem/performance analysis."""
    now=time.time()
    last=watchdog_telemetry_last.get(sid,0.0)
    if not force and now-last < 5.0:
        return
    watchdog_telemetry_last[sid]=now
    payload={
        "timestamp":time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "epoch":now,
        **row,
        "backend":backend_snapshot or {},
    }
    WATCHDOG_TELEMETRY.parent.mkdir(parents=True,exist_ok=True)
    with open(WATCHDOG_TELEMETRY,"a",encoding="utf-8") as fh:
        fh.write(json.dumps(payload,separators=(",",":"))+"\\n")


def attempts_path(): return Path(PROJECT)/".opencode-v2"/"work"/"attempts.json"
def load_attempts():
    # control_state owns the missing-ledger lifecycle invariant. A missing
    # ledger is legal only before any Dxxx execution-history artifact exists.
    missing=not attempts_path().exists()
    data=state_load_attempts(PROJECT)
    if missing:
        return {"protocol":ATTEMPT_LEDGER_PROTOCOL,"deliverables":{}}
    return data
def save_attempts(data):
    atomic_write_json(attempts_path(),data)

@contextlib.contextmanager
def attempt_lock():
    """Cross-process kernel lock; stale lock files cannot deadlock recovery."""
    path=attempts_path().with_name("attempts.json.lock")
    with exclusive_file_lock(path,timeout=5.0):
        yield

def validate_dispatch(agent,text,runtime=False):
    """Validate a planned Dxxx dispatch before the child model can start."""
    normalized=normalize_implementation_prompt(text)
    violation=(
        implementation_runtime_prompt_violation(agent,normalized)
        if runtime else implementation_prompt_violation(normalized)
    )
    if violation: return "",violation
    did=parse_deliverable(normalized)
    if not PROJECT or not plan_ready(): return did,"plan_not_ready"
    leaves=(load_manifest().get("leaves") or {}); leaf=leaves.get(did)
    if not leaf: return did,"unknown_deliverable"
    if leaf.get("split_children"):
        return did,"split-parent-not-executable"
    expected=leaf.get("role") if isinstance(leaf,dict) else ""
    if expected not in IMPLEMENTATION_AGENTS: return did,"manifest_role_invalid"
    if agent!=expected: return did,f"role_mismatch expected={expected} actual={agent}"
    missing=[d for d in leaf.get("launch_deps",[]) if not ready_info(d)]
    if missing: return did,f"unmet_launch_deps={','.join(missing)}"
    contract_missing=[d for d in leaf.get("contract_deps",[]) if not ready_info(d)]
    if contract_missing: return did,f"unmet_contract_deps={','.join(contract_missing)}"
    if verify_wait_path(did).exists():
        return did,"verification_pending"
    if ready_info(did): return did,"already_complete"
    return did,""

# V2.6.9 THREE-SLOT IMPLEMENTATION SCHEDULER BEGIN
def v1_active_session_ids_from_env(strict=False):
    raw=os.environ.get("V2_OPENCODE_ACTIVE_SESSION_IDS_JSON")
    if raw is None:
        return None
    try:
        data=json.loads(raw)
        if not isinstance(data,list) or not all(isinstance(x,str) and x for x in data):
            raise ValueError("active session snapshot must be a list of non-empty strings")
        return set(data)
    except Exception as exc:
        if strict:
            raise RuntimeError(f"invalid hook active-session snapshot: {exc}") from exc
        return set()

def active_implementation_sessions(strict=False):
    """Return live implementation children; scheduler decisions fail closed."""
    if not PROJECT:
        return []
    try:
        if v1_runtime_enabled():
            active=v1_active_session_ids_from_env(strict=strict)
            if active is None:
                # Safe only for direct CLI/supervisor use. Parent tool hooks
                # supply a snapshot because execFileSync blocks OpenCode while
                # the Python subprocess is running.
                status=v1_session_status_snapshot(strict=True)
                active={
                    sid for sid,info in status.items()
                    if isinstance(info,dict)
                    and str(info.get("type") or "") in {"busy","retry"}
                }
            if not active:
                return []
            con=db_connect()
            rows=con.execute(
                "SELECT id,coalesce(agent,'') FROM session "
                "WHERE parent_id IS NOT NULL AND directory=?",
                (PROJECT,),
            ).fetchall()
            con.close()
            return [
                (sid,agent) for sid,agent in rows
                if sid in active and agent in IMPLEMENTATION_AGENTS
            ]

        con=db_connect()
        rows=con.execute(
            "SELECT id,coalesce(agent,'') FROM session_v2 "
            "WHERE parent_id IS NOT NULL AND directory=? AND time_idle IS NULL",
            (PROJECT,),
        ).fetchall()
        con.close()
        return [(sid,agent) for sid,agent in rows if agent in IMPLEMENTATION_AGENTS]
    except Exception as exc:
        if strict:
            raise RuntimeError(f"scheduler_db_unavailable: {exc}") from exc
        return []

def active_implementation_deliverables(sessions=None):
    result={}
    sessions=active_implementation_sessions() if sessions is None else sessions
    for sid,agent in sessions:
        did=parse_deliverable(strip_subagent_prefix(first_user_text_db(sid)))
        if did:
            result[did]={"session":sid,"agent":agent}
    return result

def reserved_dispatch_deliverables(data=None):
    """Current reusable dispatch placeholders each hold one scheduler slot."""
    data=load_attempts() if data is None else data
    entries=data.get("deliverables") if isinstance(data,dict) else None
    if not isinstance(entries,dict):
        raise StateCorruptionError("attempt ledger deliverables must be an object")
    result=set()
    for did,entry in entries.items():
        if retryable_unmaterialized_dispatch_entry(entry):
            result.add(did)
    return result

def reserved_dispatch_slot_count(data=None):
    return len(reserved_dispatch_deliverables(data))

@contextlib.contextmanager
def scheduler_lock():
    """Serialize cross-process slot observation + dispatch reservation."""
    path=attempts_path().with_name("scheduler.lock")
    with exclusive_file_lock(path,timeout=5.0):
        yield

def available_implementation_slots(data=None, strict=False):
    active=len(active_implementation_sessions(strict=strict))
    reserved=reserved_dispatch_slot_count(data)
    return max(0,MAX_CONCURRENT_IMPLEMENTATION_WORKERS-active-reserved)
# V2.6.9 THREE-SLOT IMPLEMENTATION SCHEDULER END

def preclaim_attempt(agent,text,dispatch_token):
    """Atomically observe scheduler capacity and reserve one canonical attempt."""
    did,violation=validate_dispatch(agent,text)
    if violation:
        return "denied",did,violation,0
    if did and read_only_genuine_terminal(did):
        return "denied",did,"read_only_genuine_terminal",0
    try:
        with scheduler_lock():
            data=load_attempts()
            existing_slot=did in reserved_dispatch_deliverables(data)
            if (
                agent in IMPLEMENTATION_AGENTS
                and not existing_slot
                and available_implementation_slots(data,strict=True) <= 0
            ):
                return "denied",did,"worker_slots_full",0
            claim,n=claim_attempt(f"dispatch:{dispatch_token}",did)
            if claim in {"claimed","existing"}:
                if claim=="claimed":
                    write_ownership_baseline(did)
                ensure_execution_baseline(did,n)
                authorized = n > AUTOMATIC_ATTEMPT_LIMIT
                suffix = " operator_authorized=true" if authorized else ""
                log(f"DISPATCH_CLAIM token={dispatch_token} agent={agent} deliverable={did} attempt={n}{suffix}")
                csv("DISPATCH_CLAIM",f"dispatch:{dispatch_token}",agent,f"{did} attempt={n}{suffix}")
                return "claimed",did,"",n
            return "denied",did,("attempt_limit" if claim=="limit" else "attempt_ledger_invalid"),n
    except (RuntimeError,StateCorruptionError) as exc:
        log(f"DISPATCH_SCHEDULER_DENY deliverable={did} error={exc}")
        return "denied",did,"worker_scheduler_unavailable",0

def grant_operator_retry(dids, reason="explicit operator retry command"):
    """Record one human-only retry grant for each exhausted incomplete leaf.

    This is called exclusively by scripts/operator-control.py, never from an
    OpenCode tool or a model prompt.  It preserves every prior session and the
    truthful count; only the auditable future-attempt budget grows.
    """
    dids = list(dict.fromkeys(dids))
    if not dids:
        raise ValueError("no deliverables requested")
    leaves = (load_manifest().get("leaves") or {})
    with dispatch_lock:
        with attempt_lock():
            data = load_attempts()
            if data.get("owner") != "supervisor":
                raise ValueError("attempt ledger owner is not supervisor")
            entries = data.get("deliverables") if isinstance(data.get("deliverables"), dict) else {}
            selected = []
            for did in dids:
                if not valid_deliverable_id(did) or did not in leaves:
                    raise ValueError(f"unknown deliverable {did!r}")
                if not executable_leaf(did):
                    raise ValueError(f"{did} is a split parent, not an executable leaf")
                if ready_info(did):
                    raise ValueError(f"{did} is already complete")
                entry = entries.get(did)
                state = attempt_state(entry)
                if not state["valid"]:
                    raise ValueError(f"{did} attempt ledger is invalid")
                if state["count"] < state["allowed_attempts"]:
                    raise ValueError(f"{did} is not an exhausted incomplete leaf")
                selected.append((did, entry))
            stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            for did, entry in selected:
                entry.setdefault("automatic_limit", AUTOMATIC_ATTEMPT_LIMIT)
                entry["operator_retry_grants"] = int(entry.get("operator_retry_grants") or 0) + 1
                entry.setdefault("operator_overrides", []).append({
                    "timestamp": stamp, "grant": 1, "source": "operator-cli", "reason": reason,
                })
                log(f"OPERATOR_RETRY_GRANT deliverable={did} count={entry['count']} grant=1 source=operator-cli")
                csv("OPERATOR_RETRY_GRANT", "", "operator", f"{did} count={entry['count']} grant=1")
            save_attempts(data)
    return selected

def supervisor_replacement_ceiling(state):
    """Highest dispatch sequence covered without a human/operator grant."""
    return (
        int(state.get("automatic_limit") or 0)
        + int(state.get("infrastructure_retry_grants") or 0)
        + int(state.get("bad_plan_retry_grants") or 0)
        + int(state.get("plan_contract_retry_grants") or 0)
        + int(state.get("context_delivery_retry_grants") or 0)
        + int(state.get("external_contract_retry_grants") or 0)
    )


def claim_attempt(sid,did):
    """Atomically reserve an automatic or explicitly operator-authorized attempt.

    Both the live HTTP dispatcher and persisted reconciliation call this helper.
    A denied fourth attempt is never persisted, so a ready-file verifier can
    never be poisoned by an impossible ledger count.
    """
    with dispatch_lock:
        with attempt_lock():
            try:
                data=load_attempts()
                if data.get("owner") not in (None,"supervisor"):
                    return "invalid",-1
                initial={"sessions":[],"count":0}
                if recursive_split_enabled(): initial["automatic_limit"]=leaf_automatic_limit(did)
                ent=data.setdefault("deliverables",{}).setdefault(did,initial)
                sessions=ent.setdefault("sessions",[])
                state=attempt_state(ent)
                if not state["valid"]:
                    session_task.pop(sid,None)
                    return "invalid",state["count"]
                count=state["count"]
                if sid in sessions:
                    session_task[sid]=(did,count)
                    return "existing",count
                # The plugin reserves dispatch:<tool-call-id> before the beta
                # creates a model session. Bind it, never increment again.
                reservations=[x for x in sessions if isinstance(x,str) and x.startswith("dispatch:")]
                if reservations and not sid.startswith("dispatch:"):
                    # A previous beta may have failed to materialize a child.
                    # It leaves a historical dispatch placeholder behind.  Bind
                    # this observed child to the newest reservation instead of
                    # declaring the ledger corrupt merely because old, unbound
                    # placeholders exist (the gametest2y failure mode).
                    reservation=reservations[-1]
                    sessions[sessions.index(reservation)]=sid
                    for item in ent.get("operator_retry_attempts",[]):
                        if isinstance(item,dict) and item.get("session")==reservation and item.get("state")=="reserved":
                            item["session"]=sid
                    save_attempts(data); session_task[sid]=(did,count)
                    return "existing",count
                pending=[
                    item for item in ent.get("operator_retry_attempts",[])
                    if (
                        isinstance(item,dict)
                        and item.get("state")=="reserved"
                        and isinstance(item.get("session"),str)
                        and item["session"].startswith("dispatch:")
                        and int(item.get("sequence") or 0)>
                            supervisor_replacement_ceiling(state)
                    )
                ]
                if sid.startswith("dispatch:") and pending:
                    # Repeated tool delivery of the same Dxxx must re-use the
                    # extant human authorization, not reserve another one.
                    session_task[sid]=(did,int(pending[-1]["sequence"]))
                    return "existing",int(pending[-1]["sequence"])

                if sid.startswith("dispatch:") and reservations and retryable_unmaterialized_dispatch_entry(ent):
                    old_reservation=reservations[-1]
                    sessions[sessions.index(old_reservation)]=sid
                    for item in ent.get("operator_retry_attempts",[]):
                        if isinstance(item,dict) and item.get("session")==old_reservation and item.get("state")=="reserved":
                            item["session"]=sid
                    sequence=count
                    prev_seq=ent.get("unmaterialized_dispatch_sequence")
                    try:
                        prev_replays=int(ent.get("unmaterialized_dispatch_replays",0)) if int(prev_seq or 0)==sequence else 0
                    except Exception:
                        prev_replays=0
                    ent["unmaterialized_dispatch_sequence"]=sequence
                    ent["unmaterialized_dispatch_replays"]=prev_replays+1
                    ent.setdefault("unmaterialized_dispatch_history",[]).append({
                        "sequence":sequence, "replaced":old_reservation, "replacement":sid,
                        "source":"supervisor", "timestamp":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime())
                    })
                    save_attempts(data)
                    session_task[sid]=(did,sequence)
                    log(f"DISPATCH_REUSE_UNMATERIALIZED deliverable={did} attempt={sequence} replay={prev_replays+1}")
                    csv("DISPATCH_REUSE_UNMATERIALIZED",sid,"supervisor",f"{did} attempt={sequence} replay={prev_replays+1}")
                    return "existing",sequence

                if count>=state["allowed_attempts"]:
                    return "limit",count
                count+=1; ent["count"]=count; sessions.append(sid)
                uses_operator=(
                    count > supervisor_replacement_ceiling(state)
                )
                if uses_operator:
                    # This is a reservation, not consumption.  Only the
                    # supervisor can later mark it consumed after durable work.
                    ent.setdefault("operator_retry_attempts",[]).append({
                        "sequence":count, "session":sid, "state":"reserved",
                        "consumes_operator_grant":False,
                        "source":"supervisor",
                        "timestamp":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime()),
                    })
                data["owner"]="supervisor"
                save_attempts(data)
                session_task[sid]=(did,count)
                return "claimed",count
            finally: pass

def consume_operator_reservation(sid,did,evidence):
    """Consume a reserved human retry only after durable worker execution."""
    with dispatch_lock:
        with attempt_lock():
            data=load_attempts(); entry=(data.get("deliverables") or {}).get(did)
            if not isinstance(entry,dict): return False,"missing-ledger-entry"
            state=attempt_state(entry)
            for item in entry.get("operator_retry_attempts",[]):
                if isinstance(item,dict) and item.get("session")==sid and item.get("state")=="reserved":
                    sequence=int(item.get("sequence") or 0)
                    if sequence <= (
                        int(state.get("automatic_limit") or 0)
                        + int(state.get("infrastructure_retry_grants") or 0)
                        + int(state.get("bad_plan_retry_grants") or 0)
                        + int(state.get("plan_contract_retry_grants") or 0)
                        + int(state.get("context_delivery_retry_grants") or 0)
                    ):
                        return False,"supervisor-replacement-not-operator"
                    item.update({"state":"consumed", "outcome":"meaningful_execution",
                                 "consumes_operator_grant":True, "evidence":evidence,
                                 "consumed_at":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime())})
                    save_attempts(data)
                    log(f"OPERATOR_RETRY_CONSUMED session={sid} deliverable={did} evidence={evidence}")
                    csv("OPERATOR_RETRY_CONSUMED",sid,"supervisor",f"{did} evidence={evidence}")
                    return True,"consumed"
    return False,"not-reserved"

def release_operator_reservation(sid,did,reason):
    """Release one proven pre-execution abort; block a repeated one.

    The dispatch sequence remains monotonic.  Releasing changes only the
    authorization accounting for the existing human grant, never `count`.
    """
    with dispatch_lock:
        with attempt_lock():
            data=load_attempts(); entry=(data.get("deliverables") or {}).get(did)
            if not isinstance(entry,dict): return False,"missing-ledger-entry"
            state=attempt_state(entry)
            if not state["valid"]: return False,"attempt-ledger-invalid"
            for item in entry.get("operator_retry_attempts",[]):
                if not (isinstance(item,dict) and item.get("session")==sid and item.get("state")=="reserved"):
                    continue
                sequence=int(item.get("sequence") or 0)
                if sequence <= (
                    int(state.get("automatic_limit") or 0)
                    + int(state.get("infrastructure_retry_grants") or 0)
                    + int(state.get("bad_plan_retry_grants") or 0)
                    + int(state.get("plan_contract_retry_grants") or 0)
                    + int(state.get("context_delivery_retry_grants") or 0)
                ):
                    return False,"supervisor-replacement-not-operator"
                released=state["operator_infrastructure_aborted"] < MAX_OPERATOR_INFRASTRUCTURE_ABORTS
                item.update({
                    "state":"infrastructure_abort" if released else "infrastructure_blocked",
                    "outcome":"infrastructure_abort",
                    "consumes_operator_grant":False,
                    "evidence":reason,
                    "resolved_at":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime()),
                })
                save_attempts(data)
                event="OPERATOR_RETRY_RELEASED" if released else "OPERATOR_RETRY_INFRASTRUCTURE_BLOCKED"
                log(f"{event} session={sid} deliverable={did} reason={reason}")
                csv(event,sid,"supervisor",f"{did} reason={reason}")
                return released,"released" if released else "infrastructure-retry-limit"
    return False,"not-reserved"

def durable_worker_execution(did,sid=""):
    """Return True only if THIS attempt changed owned/progress state."""
    attempt=attempt_sequence_for_session(sid,did) if sid else 0
    if attempt < 1:
        try:
            entry=(load_attempts().get("deliverables") or {}).get(did,{})
            attempt=int(entry.get("count") or 0) if isinstance(entry,dict) else 0
        except Exception:
            attempt=0
    if attempt < 1:
        return False
    path=execution_baseline_path(did,attempt)
    try:
        baseline=load_json_object(path,label=f"execution baseline {did} attempt {attempt}")
    except (StateCorruptionError,OSError):
        return False
    if (
        baseline.get("owner")!="supervisor"
        or baseline.get("deliverable")!=did
        or int(baseline.get("attempt") or 0)!=attempt
        or not isinstance(baseline.get("files"),dict)
    ):
        return False
    return baseline["files"] != execution_scope_fingerprints(did)

def meaningful_worker_execution(sid,did):
    """Return the first durable or completed-tool execution boundary."""
    if durable_worker_execution(did,sid):
        return "owned-artifact-or-progress"
    if v1_runtime_enabled():
        try:
            if session_completed_tool_inputs(sid):
                return "completed-worker-tool-action"
        except Exception:
            pass
        return ""
    try:
        con=db_connect()
        rows=con.execute(
            "SELECT data FROM session_message WHERE session_id=? ORDER BY seq",
            (sid,),
        ).fetchall()
        con.close()
        for (raw,) in rows:
            data=json.loads(raw) if raw else {}
            content=data.get("content") if isinstance(data,dict) else []
            if not isinstance(content,list):
                continue
            for part in content:
                if not isinstance(part,dict) or part.get("type")!="tool":
                    continue
                state=part.get("state") if isinstance(part.get("state"),dict) else {}
                if state.get("status")!="running":
                    return "completed-worker-tool-action"
    except Exception:
        pass
    return ""


def immediate_runtime_abort(sid):
    """Return evidence only for an observed zero-work runtime cancellation."""
    if v1_runtime_enabled():
        try:
            assistants=[
                record for record in _v1_message_records(sid)
                if record["data"].get("role")=="assistant"
            ]
            if len(assistants)!=1:
                return ""
            record=assistants[0]
            msg=record["data"]
            parts=_v1_message_parts(record["id"])
            if any(part.get("type")=="tool" for part in parts):
                return ""
            meaningful=[
                part for part in parts
                if part.get("type") not in {"step-start","reasoning"}
            ]
            err=msg.get("error") if isinstance(msg.get("error"),dict) else {}
            tokens=msg.get("tokens") if isinstance(msg.get("tokens"),dict) else {}
            cache=tokens.get("cache") if isinstance(tokens.get("cache"),dict) else {}
            total=(
                int(tokens.get("input") or 0)
                + int(tokens.get("output") or 0)
                + int(tokens.get("reasoning") or 0)
                + int(cache.get("read") or 0)
                + int(cache.get("write") or 0)
            )
            if (
                err.get("name")=="MessageAbortedError"
                and not meaningful
                and total==0
            ):
                return "immediate-runtime-cancel zero-token-zero-tool aborted"
        except Exception:
            return ""
        return ""
    try:
        con=db_connect()
        rows=con.execute(
            "SELECT type,data FROM session_message "
            "WHERE session_id=? ORDER BY seq",
            (sid,),
        ).fetchall()
        con.close()
        assistants=[]
        for kind,raw in rows:
            data=json.loads(raw) if raw else {}
            if kind=="assistant":
                assistants.append(data if isinstance(data,dict) else {})
            if isinstance(data,dict) and data.get("type")=="tool":
                return ""
            if (
                isinstance(data,dict)
                and isinstance(data.get("content"),list)
                and any(
                    isinstance(part,dict) and part.get("type")=="tool"
                    for part in data["content"]
                )
            ):
                return ""
        if len(assistants)!=1:
            return ""
        msg=assistants[0]
        err=msg.get("error") if isinstance(msg.get("error"),dict) else {}
        content=msg.get("content") if isinstance(msg.get("content"),list) else []
        tokens=msg.get("tokens") if isinstance(msg.get("tokens"),dict) else {}
        if (
            msg.get("finish")=="error"
            and err.get("type")=="aborted"
            and not content
            and not any(int(tokens.get(k) or 0) for k in ("input","output","reasoning","cache"))
        ):
            return "immediate-runtime-cancel zero-token-zero-tool aborted"
    except Exception:
        return ""
    return ""


class OpenCodeHTTP:
    def __init__(self):
        self.base=None
        self.password=None
        self.prefix=None
        self.mode=None
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
        explicit=str(os.environ.get("V2_OPENCODE_BASE_URL") or "").strip().rstrip("/")
        if explicit:
            self.base=explicit
            self.password=(
                os.environ.get("V2_OPENCODE_PASSWORD")
                or os.environ.get("OPENCODE_PASSWORD")
                or os.environ.get("OPENCODE_SERVER_PASSWORD")
            )
            self.prefix=""
            self.mode="v1"
            q=""
            if PROJECT:
                q="?" + urllib.parse.urlencode({"directory":PROJECT})
            try:
                obj=self.request("GET","/session/status"+q,timeout=2)
                if not isinstance(obj,dict):
                    raise RuntimeError("v1 /session/status did not return an object")
                log(f"HTTP_CONTROL_CONNECTED base={self.base} mode=v1 probe=/session/status")
                csv("HTTP_CONTROL_CONNECTED",detail=f"{self.base} mode=v1 probe=/session/status")
                return True
            except Exception as exc:
                self.base=self.password=self.prefix=None
                self.mode=None
                log(f"HTTP_CONTROL_V1_WAIT base={explicit} error={type(exc).__name__}")
                return False

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

        # Completion hooks run in a plugin subprocess.  V1's plugin host can
        # sanitize V2_OPENCODE_BASE_URL even though the parent server was
        # launched with it.  Discover only this repository's pinned v1 binary,
        # then prove the exact PROJECT endpoint before continuing a splitter.
        # This is transport identity recovery, not a broader server search.
        v1_binary=os.path.realpath(str(ROOT/"runtime"/"opencode-v1.18.31"/"opencode"))
        for proc in Path("/proc").glob("[0-9]*"):
            try:
                pid=int(proc.name)
                cmd=(proc/"cmdline").read_bytes().replace(
                    b"\0",b" "
                ).decode("utf-8","replace")
                if not cmd.startswith(v1_binary+" serve "):
                    continue
                ports=[]
                for line in ss.splitlines():
                    if f"pid={pid}" not in line:
                        continue
                    ports += [
                        int(match.group(1))
                        for match in re.finditer(r"127\.0\.0\.1:(\d+)",line)
                    ]
                for port in dict.fromkeys(ports):
                    self.base=f"http://127.0.0.1:{port}"
                    self.password=None
                    self.prefix=""
                    self.mode="v1"
                    query=("?"+urllib.parse.urlencode({"directory":PROJECT})) if PROJECT else ""
                    try:
                        status=self.request("GET","/session/status"+query,timeout=2)
                        if not isinstance(status,dict):
                            raise RuntimeError("v1 /session/status did not return an object")
                        log(
                            f"HTTP_CONTROL_CONNECTED base={self.base} mode=v1 "
                            f"probe=/session/status discovered-pid={pid}"
                        )
                        csv(
                            "HTTP_CONTROL_CONNECTED",
                            detail=f"{self.base} mode=v1 probe=/session/status pid={pid}",
                        )
                        return True
                    except Exception:
                        continue
            except Exception:
                continue
        self.base=self.password=self.prefix=None
        self.mode=None

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
                            self.mode="beta"
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
        self.mode=None
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
            path=("/session/status"+q) if self.mode=="v1" else ("/api/session/active"+q)
            obj=self.request("GET",path)
            return obj if isinstance(obj,dict) else {}
        except Exception:
            self.base=self.prefix=None
            self.mode=None
            raise

    def get_session(self,sid):
        if not self.ensure():
            return {}
        qsid=urllib.parse.quote(sid)
        q=""
        if PROJECT:
            q="?" + urllib.parse.urlencode({"directory":PROJECT})
        paths=((f"/session/{qsid}"+q,) if self.mode=="v1" else (f"/api/session/{qsid}",f"/session/{qsid}"))
        for path in paths:
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
        if self.mode=="v1":
            q={"limit":"64"}
            if PROJECT:
                q["directory"]=PROJECT
            paths=(f"/session/{qsid}/message?" + urllib.parse.urlencode(q),)
        else:
            paths=(
                f"/api/session/{qsid}/message",
                f"/api/session/{qsid}/message?limit=32",
                f"/session/{qsid}/message",
                f"/session/{qsid}/message?limit=32",
            )
        for path in paths:
            try:
                obj=self.request("GET",path,timeout=4)
                if isinstance(obj,list):
                    return obj
                if isinstance(obj,dict):
                    for key in ("messages","items","data"):
                        value=obj.get(key)
                        if isinstance(value,list):
                            return value
                        if isinstance(value,dict):
                            for subkey in ("messages","items","data"):
                                if isinstance(value.get(subkey),list):
                                    return value[subkey]
            except Exception:
                pass
        return []

    def stream_session_events(self,sid,on_event,stop):
        if not self.ensure():
            return
        if self.mode=="v1":
            q=""
            if PROJECT:
                q="?" + urllib.parse.urlencode({"directory":PROJECT})
            req=urllib.request.Request(
                self.base+"/event"+q,
                method="GET",
                headers={**self.headers(),"Accept":"text/event-stream"},
            )
            with urllib.request.urlopen(req,timeout=30) as response:
                for raw in response:
                    if stop.is_set():
                        return
                    line=raw.decode("utf-8","replace").strip()
                    if not line.startswith("data:"):
                        continue
                    try:
                        payload=json.loads(line[5:].strip())
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(payload,dict):
                        continue
                    props=payload.get("properties")
                    if not isinstance(props,dict):
                        continue
                    event_sid=(props.get("sessionID") or props.get("sessionId") or props.get("session_id"))
                    if event_sid!=sid:
                        continue
                    on_event({"type":payload.get("type"),"data":props})
            return

        qsid=urllib.parse.quote(sid)
        req=urllib.request.Request(
            self.base+f"/api/session/{qsid}/event",
            method="GET",headers={**self.headers(),"Accept":"text/event-stream"},
        )
        with urllib.request.urlopen(req,timeout=30) as response:
            for raw in response:
                if stop.is_set():
                    return
                line=raw.decode("utf-8","replace").strip()
                if not line.startswith("data:"):
                    continue
                try:
                    event=json.loads(line[5:].strip())
                except json.JSONDecodeError:
                    continue
                if isinstance(event,dict):
                    on_event(event)

    def interrupt(self,sid):
        if not self.ensure():
            return False
        qsid=urllib.parse.quote(sid)
        if self.mode=="v1":
            q=""
            if PROJECT:
                q="?" + urllib.parse.urlencode({"directory":PROJECT})
            try:
                self.request("POST",f"/session/{qsid}/abort"+q,payload=None,timeout=4)
                return True
            except Exception:
                return False
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

    def start_agent_session(self,agent,text):
        """Create and steer a v2 session using the installed beta's SDK schema."""
        if not self.ensure():
            return False,"http-not-connected"
        if self.mode=="v1":
            # Direct v1 semantic creation bypasses transport-root TaskTool.
            return False,"stage-a-controller-owned"
        try:
            created=self.request(
                "POST","/api/session",
                payload={"agent":agent,"location":{"directory":PROJECT}},
                timeout=8,
            )
            data=created.get("data",created) if isinstance(created,dict) else {}
            sid=data.get("id") if isinstance(data,dict) else ""
            if not sid:
                return False,"session-create-missing-id"
            self.request(
                "POST",f"/api/session/{urllib.parse.quote(sid)}/prompt",
                payload={"text":text,"delivery":"steer"},
                timeout=12,
            )
            return True,sid
        except urllib.error.HTTPError as e:
            try: detail=e.read().decode("utf-8","replace")[:1000]
            except Exception: detail=""
            return False,f"HTTP {e.code}: {detail}"
        except Exception as e:
            return False,repr(e)

    def steer_session(self,sid,text):
        """Send another turn to an existing V2 session."""
        if not self.ensure():
            return False,"http-not-connected"
        if self.mode=="v1":
            # Never steer the technical root through legacy semantic continuation.
            return False,"stage-a-controller-owned"
        try:
            qsid=urllib.parse.quote(sid)
            self.request(
                "POST",f"/api/session/{qsid}/prompt",
                payload={"text":text,"delivery":"steer"},
                timeout=12,
            )
            return True,sid
        except urllib.error.HTTPError as e:
            try: detail=e.read().decode("utf-8","replace")[:1000]
            except Exception: detail=""
            return False,f"HTTP {e.code}: {detail}"
        except Exception as e:
            return False,repr(e)

    def continue_splitter_session(self,sid,text):
        """Send the one supervisor-authorized follow-up to a v1 splitter.

        This intentionally does not relax generic v1 session steering: only
        the splitter state machine invokes it after recording the sole
        corrective turn.  It neither creates a session nor supplies a model,
        agent, or tool override, so OpenCode continues the native child with
        its existing task-splitter configuration and permissions.
        """
        if not self.ensure():
            return False,"http-not-connected"
        if self.mode!="v1":
            return False,"splitter-corrective-requires-v1-runtime"
        try:
            query=urllib.parse.urlencode({"directory":PROJECT})
            self.request(
                "POST",f"/session/{urllib.parse.quote(sid)}/prompt_async?{query}",
                payload={"parts":[{"type":"text","text":text}]},timeout=12,
            )
            return True,sid
        except urllib.error.HTTPError as e:
            try: detail=e.read().decode("utf-8","replace")[:1000]
            except Exception: detail=""
            return False,f"HTTP {e.code}: {detail}"
        except Exception as e:
            return False,repr(e)

    def start_lessons_session(self,text):
        """Launch lessons outside the verdict-critical root session."""
        return self.start_agent_session("lessons-learner",text)

http=OpenCodeHTTP()

ABORT_INTENT_PROTOCOL="v2-abort-intents-v1"

def abort_intents_path():
    return Path(PROJECT)/".opencode-v2"/"work"/"abort-intents.json"

def load_abort_intents():
    data=load_json_object(
        abort_intents_path(),
        default_missing={"owner":"supervisor","protocol":ABORT_INTENT_PROTOCOL,"sessions":{}},
        label="abort intent ledger",
    )
    if data.get("owner") not in (None,"supervisor"):
        raise StateCorruptionError("abort intent ledger owner is invalid")
    sessions=data.get("sessions",{})
    if not isinstance(sessions,dict):
        raise StateCorruptionError("abort intent ledger sessions must be an object")
    data.setdefault("owner","supervisor")
    data.setdefault("protocol",ABORT_INTENT_PROTOCOL)
    data["sessions"]=sessions
    return data

def save_abort_intents(data):
    atomic_write_json(abort_intents_path(),data)

def set_abort_intent(sid,reason,agent,state):
    if not PROJECT or not sid:
        return
    with abort_intent_lock:
        data=load_abort_intents()
        entry=data["sessions"].setdefault(sid,{})
        entry.update({
            "session":sid,
            "reason":reason,
            "agent":agent,
            "state":state,
            "updated_at":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime()),
        })
        save_abort_intents(data)

def session_terminal_aborted(sid):
    if v1_runtime_enabled():
        try:
            record=_v1_latest_assistant_record(sid)
            if not record:
                return False
            error=record["data"].get("error")
            return (
                isinstance(error,dict)
                and error.get("name")=="MessageAbortedError"
            )
        except Exception:
            return False
    try:
        con=db_connect()
        rows=con.execute(
            "SELECT data FROM session_message "
            "WHERE session_id=? AND type='assistant' ORDER BY seq DESC",
            (sid,),
        ).fetchall()
        con.close()
        for (raw,) in rows:
            data=json.loads(raw) if raw else {}
            if not isinstance(data,dict):
                continue
            err=data.get("error") if isinstance(data.get("error"),dict) else {}
            if data.get("finish")=="error" and err.get("type")=="aborted":
                return True
            return False
    except Exception:
        return False
    return False


def persisted_abort_reason(sid):
    if not PROJECT or not sid:
        return ""
    data=load_abort_intents()
    entry=(data.get("sessions") or {}).get(sid)
    if not isinstance(entry,dict):
        return ""
    state=entry.get("state")
    if state=="confirmed":
        return str(entry.get("reason") or "")
    if state=="requested" and session_terminal_aborted(sid):
        return str(entry.get("reason") or "")
    return ""

def resolve_abort_intent(sid,outcome):
    if not PROJECT or not sid:
        return
    with abort_intent_lock:
        data=load_abort_intents()
        entry=(data.get("sessions") or {}).get(sid)
        if not isinstance(entry,dict):
            return
        if entry.get("state") in {"resolved","failed"}:
            return
        entry.update({
            "state":"resolved",
            "outcome":outcome,
            "resolved_at":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime()),
        })
        save_abort_intents(data)

def abort_session(sid,reason,agent=""):
    # Persist intent *before* the external side effect.  If the process dies
    # after the interrupt but before confirmation, restart reconciliation can
    # recover the reason when the session DB shows a terminal aborted message.
    set_abort_intent(sid,reason,agent,"requested")
    ok=http.interrupt(sid)
    if ok:
        set_abort_intent(sid,reason,agent,"confirmed")
        supervisor_abort_reasons[sid]=reason
    else:
        set_abort_intent(sid,reason,agent,"failed")
    kind="INTERRUPT" if ok else "INTERRUPT_FAILED"; log(f"{kind} session={sid} agent={agent} reason={reason}"); csv(kind,sid,agent,reason); return ok

def watchdog_age(sid,key,can_watch,progress_marker=None,now=None):
    """Seconds since the last observable model/tool progress, not message age."""
    now=time.monotonic() if now is None else now
    st=watch.setdefault(
        sid,
        {"key":key,"start":None,"aborted_key":None,"progress_marker":None},
    )
    if st["key"]!=key:
        st["key"]=key
        st["start"]=None
        st["aborted_key"]=None
        st["progress_marker"]=None
    if not can_watch:
        st["start"]=None
        st["progress_marker"]=progress_marker
        return 0,st
    if st["start"] is None:
        st["start"]=now
        st["progress_marker"]=progress_marker
    elif progress_marker is not None and progress_marker!=st.get("progress_marker"):
        st["progress_marker"]=progress_marker
        st["start"]=now
    return max(0.0,now-st["start"]),st

def planner_context_reason(agent,context_input,tool_running=False):
    if agent!="implementation-planner" or tool_running:
        return ""
    if not isinstance(context_input,(int,float)):
        return ""
    if context_input<PLANNER_CONTEXT_INPUT_CEILING:
        return ""
    return (
        f"planner_context_input={int(context_input)} "
        f"ceiling={PLANNER_CONTEXT_INPUT_CEILING}"
    )

def watchdog_limits(agent):
    if agent=="implementation-planner":
        return 300,20000,20000
    return HARD_SECONDS,HARD_REASONING_CHARS,HARD_TEXT_CHARS

def reduce_live_event(state,event,now=None):
    """Track transient SSE progress and distinguish progress from mere liveness."""
    now=time.monotonic() if now is None else now
    kind=event.get("type")
    data=event.get("data") if isinstance(event.get("data"),dict) else {}
    delta=data.get("delta") if isinstance(data.get("delta"),str) else ""
    progressed=False
    if kind=="session.next.reasoning.delta":
        state["reasoning"]=state.get("reasoning",0)+len(delta)
        progressed=bool(delta)
    elif kind=="session.next.text.delta":
        state["text"]=state.get("text",0)+len(delta)
        progressed=bool(delta)
    elif kind=="session.next.tool.called":
        state["tool_running"]=True
        progressed=True
    elif kind=="session.next.tool.success":
        state.update(reasoning=0,text=0,tool_running=False)
        progressed=True
    elif kind=="session.next.tool.failed":
        state["tool_running"]=False
        progressed=True
    elif kind=="session.next.step.started":
        state.update(reasoning=0,text=0,tool_running=False)
        progressed=True
    state["last_event"]=now
    if progressed:
        state["last_progress"]=now
        state["progress_seq"]=int(state.get("progress_seq") or 0)+1
    return state

def event_watchdog_reason(agent,state):
    if state.get("tool_running"): return ""
    _,reason_limit,text_limit=watchdog_limits(agent)
    if state.get("reasoning",0)>=reason_limit:
        return f"sse_reasoning_chars={state['reasoning']}"
    if state.get("text",0)>=text_limit:
        return f"sse_text_chars={state['text']}"
    return ""

def ensure_event_watch(sid):
    state=event_watch.get(sid)
    if state and state.get("thread") and state["thread"].is_alive(): return state
    stop=threading.Event()
    created=time.monotonic()
    state={"reasoning":0,"text":0,"tool_running":False,"tool_successes":0,
           "connected":False,"last_event":created,"last_progress":created,
           "progress_seq":0,"stop":stop}
    event_watch[sid]=state
    def consume(event):
        with lock:
            state["connected"]=True; state["last_event"]=time.monotonic()
            kind=event.get("type")
            reduce_live_event(state,event)
            if kind=="session.next.tool.called": state["tool_running"]=True
            elif kind in {"session.next.tool.success","session.next.tool.failed"}:
                state["tool_running"]=False
                if kind=="session.next.tool.success":
                    state["reasoning"]=0; state["text"]=0
                    state["tool_successes"]+=1
    def stream():
        while not stop.is_set():
            try:
                http.stream_session_events(sid,consume,stop)
            except Exception as e:
                state["stream_error"]=repr(e)
            finally:
                state["connected"]=False
            stop.wait(0.5)
    thread=threading.Thread(target=stream,daemon=True,name=f"v2-event-{sid[-8:]}")
    state["thread"]=thread; thread.start(); return state

def stop_inactive_event_watches(active):
    """Release SSE readers for sessions no longer reported active.

    This is deliberately best-effort: it must never take down the HTTP polling
    loop merely because an old event stream is slow to close.
    """
    with lock:
        stale=[sid for sid in event_watch if sid not in active]
        states=[event_watch.pop(sid) for sid in stale]
    for state in states:
        stop=state.get("stop")
        if stop: stop.set()

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
    # The current beta returns the session location under `location.directory`,
    # not the older top-level `directory`.  Preserve the latter for compatible
    # versions, but never monitor or enforce a different project as if it were
    # this supervisor's project.
    location=session_info.get("location") if isinstance(session_info,dict) else {}
    directory=(
        session_info.get("directory")
        or (location.get("directory") if isinstance(location,dict) else "")
        or ""
    ) if isinstance(session_info,dict) else ""

    if not assistants:
        return {
            "agent":agent,"parent":parent,"directory":directory,"first_user":first_user,
            "message_id":"","reasoning":0,"text":0,"tool_running":False,
            "last_tool_id":"","context_input":None,"output_tokens":0,
            "reasoning_tokens":0,"cache_tokens":0,"observable":False,
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
    def token_int(name):
        value=tokens.get(name)
        return int(value) if isinstance(value,(int,float)) else 0
    ci=tokens.get("input")
    ci=int(ci) if isinstance(ci,(int,float)) else None
    output_tokens=token_int("output")
    reasoning_tokens=token_int("reasoning")
    cache_value=tokens.get("cache")
    if isinstance(cache_value,dict):
        cache_tokens=sum(
            int(cache_value.get(k) or 0)
            for k in ("read","write")
            if isinstance(cache_value.get(k),(int,float))
        )
    else:
        cache_tokens=token_int("cache")
    tm=info.get("time") if isinstance(info.get("time"),dict) else {}
    completed=isinstance(tm.get("completed"),(int,float))

    return {
        "agent":agent,"parent":parent,"directory":directory,"first_user":first_user,
        "message_id":info.get("id") or "","reasoning":reasoning,"text":text_chars,
        "tool_running":tool_running,"last_tool_id":last_tool_id,
        "context_input":ci,"output_tokens":output_tokens,
        "reasoning_tokens":reasoning_tokens,"cache_tokens":cache_tokens,
        "observable":parts_observable,"assistant_completed":completed,
    }

def enforce_assignment(sid,agent,first_user):
    normalized=strip_subagent_prefix(first_user or first_user_text_db(sid)).strip()
    planned=agent in IMPLEMENTATION_AGENTS or bool(parse_deliverable(normalized))
    if not planned or sid in dispatch_seen:
        return

    # Live message persistence can lag /session/active. Missing prompt identity
    # is UNKNOWN, not a policy violation. Fall back to SQLite, then wait.
    text=first_user or first_user_text_db(sid)
    if not text:
        return

    did,violation=validate_dispatch(agent,text,runtime=True)
    if violation:
        if abort_session(sid,f"dispatch_protocol_violation {violation}",agent):
            dispatch_seen.add(sid)
        log(f"DISPATCH_DENY session={sid} agent={agent} {violation}")
        csv("DISPATCH_DENY",sid,agent,violation)
        return

    claim,n=claim_attempt(sid,did)
    if claim in {"claimed","existing"}:
        ensure_execution_baseline(did,n)
    if claim in {"limit","invalid"}:
        reason="attempt_limit" if claim=="limit" else "attempt_ledger_invalid"
        if abort_session(sid,f"dispatch_guard {reason} deliverable={did} count={n}",agent):
            dispatch_seen.add(sid)
        csv("DISPATCH_DENY",sid,agent,f"{did} {reason}={n}")
        return

    # Mark processed only after the durable attempt claim/binding succeeded.
    dispatch_seen.add(sid)
    log(f"DISPATCH_ALLOW session={sid} agent={agent} deliverable={did} attempt={n}")
    csv("DISPATCH_ALLOW",sid,agent,f"{did} attempt={n}")


def materialize_dispatch_child(sid,agent):
    """Bind one controller-observed native child to its durable reservation."""
    if agent not in IMPLEMENTATION_AGENTS:
        raise ValueError(f"unsupported implementation agent: {agent!r}")
    prompt=first_user_text_db(sid)
    if not prompt:
        raise ValueError(f"native child prompt is not durable yet: {sid}")
    did,violation=validate_dispatch(agent,prompt,runtime=True)
    if violation:
        raise ValueError(f"native child dispatch is invalid: {violation}")
    enforce_assignment(sid,agent,prompt)
    attempt=attempt_sequence_for_session(sid,did)
    if attempt < 1:
        raise StateCorruptionError(
            f"native child was not bound to a durable attempt: {sid} {did}"
        )
    normalize_supervisor_replacement_record(did)
    print(f"DISPATCH_MATERIALIZED session={sid} deliverable={did} attempt={attempt}")


def normalize_supervisor_replacement_record(did):
    """Keep a stale replacement reservation, but record its true authority."""
    with dispatch_lock:
        with attempt_lock():
            data=load_attempts(); entry=(data.get("deliverables") or {}).get(did)
            if not isinstance(entry,dict): return False
            state=attempt_state(entry)
            plan=int(state.get("plan_contract_retry_grants") or 0)
            bad=int(state.get("bad_plan_retry_grants") or 0)
            ceiling=(int(state.get("automatic_limit") or 0)+int(state.get("infrastructure_retry_grants") or 0)+plan+bad)
            changed=False
            for item in entry.get("operator_retry_attempts",[]):
                if not isinstance(item,dict): continue
                try: sequence=int(item.get("sequence") or 0)
                except (TypeError,ValueError): continue
                if sequence <= int(state.get("automatic_limit") or 0) or sequence>ceiling:
                    continue
                label="plan_contract_replacement" if plan else "bad_plan_replacement"
                item.update({"state":label,"outcome":label,"consumes_operator_grant":False,"normalized_by":"supervisor-credit-authority-v1","normalized_at":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime())})
                changed=True
            if changed: save_attempts(data)
    if changed:
        log(f"SUPERVISOR_REPLACEMENT_NORMALIZED deliverable={did}")
        csv("SUPERVISOR_REPLACEMENT_NORMALIZED",detail=did)
    return changed


# 20260911 ORIGINAL_TASK_DURABILITY_FIX

ORIGINAL_TASK_BEGIN = "=== ORIGINAL_USER_TASK_BEGIN ==="
ORIGINAL_TASK_END = "=== ORIGINAL_USER_TASK_END ==="


def original_task_path():
    return Path(PROJECT) / ".opencode-v2" / "ORIGINAL_TASK.md"


def extract_original_task(text):
    """Extract only the real $ARGUMENTS payload from /oneshot-v2."""
    if not text:
        return ""

    text = str(text)

    a = text.find(ORIGINAL_TASK_BEGIN)
    if a >= 0:
        a += len(ORIGINAL_TASK_BEGIN)
        b = text.find(ORIGINAL_TASK_END, a)

        if b >= 0:
            return text[a:b].strip()

    # Backward-compatible fallback for a root started before this patch.
    # Never persist one of our own continuation prompts as the user's task.
    stripped = text.strip()

    bad = (
        "Continue orchestration for this project.",
        "Continue implementation planning for this project.",
    )

    if any(stripped.startswith(x) for x in bad):
        return ""

    return stripped


def ensure_original_task(sid):
    """
    Persist the original task exactly once.

    Once ORIGINAL_TASK.md contains a non-empty task it is immutable for this
    run. Root rollover sessions may read it but never replace it.
    """
    if not PROJECT or not sid:
        return False

    path = original_task_path()

    try:
        if path.exists() and path.read_text(errors="replace").strip():
            return True
    except Exception:
        pass

    raw = first_user_text_db(sid)
    task = extract_original_task(raw)

    if not task:
        log(
            f"ORIGINAL_TASK_CAPTURE_MISSING session={sid} "
            f"project={PROJECT}"
        )
        csv(
            "ORIGINAL_TASK_CAPTURE_MISSING",
            sid,
            "orchestrator",
            PROJECT,
        )
        return False

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(task.rstrip() + "\n")
    os.replace(tmp, path)

    log(
        f"ORIGINAL_TASK_CAPTURED session={sid} "
        f"chars={len(task)} path={path}"
    )
    csv(
        "ORIGINAL_TASK_CAPTURED",
        sid,
        "orchestrator",
        f"chars={len(task)} path={path}",
    )

    return True



# V2.6.9 SAME_ROOT_AUTOCONTINUE
ROOT_SAME_SESSION_CONTINUATION_PROMPT="""Continue the canonical V2 pipeline from the current durable state.

Read .opencode-v2/ORIGINAL_TASK.md for the durable original goal. Do not treat
this continuation message as a new project task.

Do not regenerate ACCEPTANCE.md or IMPLEMENTATION_PLAN.md if their current
ready/guard state is already valid.

Use .opencode-v2/bin/control-status and supervisor-owned durable artifacts as
authoritative scheduler state. Child prose such as NOT READY, suggested retry,
or suggested splitting is advisory only.

Never invoke task-splitter unless authoritative durable state requires a
recursive split for that exact Dxxx AND
.opencode-v2/work/Dxxx.split-request.json exists.

If a splitter claim is denied with split-request-missing, the orchestration
decision was stale: re-read control-status and continue the action selected by
durable state. Do not stop merely because a child returned, a dispatch was
denied, compaction happened, or a tool call was refused.

Continue automatically until exact ACCEPTANCE_PASS or a genuine terminal
blocked phase.
"""

root_nudged_messages=set()
root_same_session_idle_since=None
ROOT_SAME_SESSION_IDLE_GRACE=3.0

def root_session_path(): return Path(PROJECT)/".opencode-v2"/"work"/"root-session.json"

def record_root_session(sid):
    if not PROJECT or not sid: return
    atomic_write_json(root_session_path(),{"owner":"supervisor","protocol":ROOT_SESSION_PROTOCOL,"project":str(Path(PROJECT).resolve()),"session":sid,"updated_at":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime())})

def _valid_root_session(sid):
    try:
        table=session_table_name()
        con=db_connect()
        row=con.execute(
            f"SELECT id FROM {table} "
            "WHERE id=? AND agent=? AND directory=?",
            (sid,root_agent_name(),PROJECT),
        ).fetchone()
        con.close()
        return bool(row)
    except Exception:
        return False


def root_orchestrator_id():
    if not PROJECT:
        return ""
    try:
        table=session_table_name()
        con=db_connect()
        row=con.execute(
            f"SELECT id FROM {table} "
            "WHERE agent=? AND directory=? AND time_created>=? "
            "ORDER BY time_created DESC LIMIT 1",
            (root_agent_name(),PROJECT,START_MS),
        ).fetchone()
        con.close()
        if row:
            record_root_session(row[0])
            return row[0]
        data=load_json_object(
            root_session_path(),default_missing={},label="root session tracker"
        )
        sid=str(data.get("session") or "")
        if data and (
            data.get("owner")!="supervisor"
            or data.get("protocol")!=ROOT_SESSION_PROTOCOL
        ):
            raise StateCorruptionError("root session tracker is invalid")
        return sid if sid and _valid_root_session(sid) else ""
    except StateCorruptionError:
        raise
    except Exception:
        return ""


def root_rollover_path(): return Path(PROJECT)/".opencode-v2"/"root-rollovers.json"
def root_continuation_block_path(): return Path(PROJECT)/".opencode-v2"/"root-continuation-blocked.json"

def load_root_continuation_block():
    path=root_continuation_block_path()
    if not path.exists():
        return {}
    data=load_json_object(path,label="root continuation blocker")
    if (
        data.get("owner")!="supervisor"
        or data.get("protocol")!=ROOT_CONTINUATION_BLOCK_PROTOCOL
        or data.get("reason")!="root_continuation_limit"
    ):
        raise StateCorruptionError("root continuation blocker is invalid")
    try:
        count=int(data.get("count"))
        limit=int(data.get("limit"))
    except (ValueError,TypeError) as exc:
        raise StateCorruptionError("root continuation blocker counters are invalid") from exc
    if count < limit or limit != MAX_ROOT_RESTARTS:
        raise StateCorruptionError("root continuation blocker limit is inconsistent")
    return data

def apply_root_continuation_block(data):
    block=load_root_continuation_block()
    if not block or data.get("resume_phase")=="complete":
        return data
    blockers=[
        item for item in (
            data.get("execution_blockers",[])
            if isinstance(data.get("execution_blockers"),list) else []
        )
        if not (
            isinstance(item,dict)
            and item.get("reason")=="root_continuation_limit"
        )
    ]
    blockers.append({
        "deliverable":"",
        "reason":"root_continuation_limit",
        "detail":(
            f"fresh root continuation limit reached "
            f"({block.get('count')}/{block.get('limit')}) "
            f"during {block.get('phase')}"
        ),
        "latest_session":str(block.get("latest_session") or ""),
    })
    data["execution_blockers"]=blockers
    data["root_continuation"]={
        "blocked":True,
        "reason":"root_continuation_limit",
        "count":int(block["count"]),
        "limit":int(block["limit"]),
        "phase":str(block.get("phase") or ""),
        "latest_session":str(block.get("latest_session") or ""),
    }
    data["resume_phase"]="execution-blocked"
    return data

def record_root_continuation_limit(sid,phase):
    count=root_restart_count()
    payload={
        "owner":"supervisor",
        "protocol":ROOT_CONTINUATION_BLOCK_PROTOCOL,
        "reason":"root_continuation_limit",
        "count":count,
        "limit":MAX_ROOT_RESTARTS,
        "phase":str(phase or ""),
        "latest_session":str(sid or ""),
        "recorded_at":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime()),
    }
    existing=load_root_continuation_block()
    if existing and all(
        existing.get(key)==payload.get(key)
        for key in ("owner","protocol","reason","count","limit","phase","latest_session")
    ):
        return False
    atomic_write_json(root_continuation_block_path(),payload)
    return True

def root_restart_count():
    data=load_json_object(
        root_rollover_path(),default_missing={"count":0},label="root rollover ledger"
    )
    try:
        count=int(data.get("count") or 0)
    except (ValueError,TypeError) as exc:
        raise StateCorruptionError("root rollover count is invalid") from exc
    if count < 0:
        raise StateCorruptionError("root rollover count is negative")
    return count

def record_root_restart(sid,phase):
    count=root_restart_count()+1
    atomic_write_json(
        root_rollover_path(),
        {"owner":"supervisor","count":count,"latest_session":sid,"phase":phase},
    )
    # A successful fresh-root launch supersedes any stale blocker left after an
    # operator/manual recovery that reset the rollover budget.
    root_continuation_block_path().unlink(missing_ok=True)


# V2.6.9 GAMETESTNEW7 REFERENCE GATE BEGIN
REFERENCE_FOUNDATION_MARKER="<!-- REFERENCE_FOUNDATION_READY -->"
REFERENCE_POLICY_RE=re.compile(
    r"(?im)^\s*(?:#{1,6}\s*)?Reference policy\s*:\s*"
    r"(none|internal|external-required)\s*$"
)


def reference_foundation_marker_complete(text):
    # Require one exact READY marker as the final non-empty line.
    lines=[line.strip() for line in str(text or "").splitlines() if line.strip()]
    if not lines or lines[-1] != REFERENCE_FOUNDATION_MARKER:
        return False
    markers=[line for line in lines if line == REFERENCE_FOUNDATION_MARKER]
    return markers == [REFERENCE_FOUNDATION_MARKER]


def reference_policy_from_text(text):
    matches=[value.lower() for value in REFERENCE_POLICY_RE.findall(str(text or ""))]
    return matches[0] if len(matches)==1 else ""


def acceptance_reference_policy():
    if not PROJECT:
        return ""
    path=Path(PROJECT)/".opencode-v2"/"ACCEPTANCE.md"
    try:
        return reference_policy_from_text(path.read_text(errors="replace"))
    except OSError:
        return ""


def reference_session_mode(sid):
    text=first_user_text_db(sid)
    if "REFERENCE_MODE: VALIDATION" in text:
        return "validation"
    return "foundation"



def reference_session_made_progress(sid):
    """Did this completed reference slice persist authoritative durable state?"""
    if v1_runtime_enabled():
        try:
            for record in _v1_message_records(sid):
                if record["data"].get("role")!="assistant":
                    continue
                for item in _v1_message_parts(record["id"]):
                    if item.get("type")!="tool":
                        continue
                    if item.get("tool") not in {"edit","write"}:
                        continue
                    state=item.get("state") if isinstance(item.get("state"),dict) else {}
                    if state.get("status")!="completed":
                        continue
                    inp=state.get("input") if isinstance(state.get("input"),dict) else {}
                    path=str(
                        inp.get("path")
                        or inp.get("filePath")
                        or inp.get("file_path")
                        or inp.get("filename")
                        or ""
                    )
                    if (
                        ".opencode-v2/acceptance/" in path
                        or path.endswith(".opencode-v2/REFERENCE_FOUNDATION.md")
                    ):
                        return True
            return False
        except Exception as exc:
            log(f"REFERENCE_PROGRESS_DB_ERROR session={sid} error={exc!r}")
            return False
    try:
        con=db_connect()
        rows=con.execute(
            "SELECT data FROM session_message "
            "WHERE session_id=? AND type='assistant' ORDER BY seq",
            (sid,),
        ).fetchall()
        con.close()
    except Exception as exc:
        log(f"REFERENCE_PROGRESS_DB_ERROR session={sid} error={exc!r}")
        return False
    for (raw,) in rows:
        try:
            data=json.loads(raw)
        except Exception:
            continue
        content=data.get("content") if isinstance(data,dict) else None
        if not isinstance(content,list):
            continue
        for item in content:
            if not isinstance(item,dict) or item.get("type")!="tool":
                continue
            if item.get("name") not in {"edit","write"}:
                continue
            state=item.get("state") if isinstance(item.get("state"),dict) else {}
            if state.get("status")!="completed":
                continue
            inp=state.get("input") if isinstance(state.get("input"),dict) else {}
            path=str(inp.get("path") or "")
            if (
                ".opencode-v2/acceptance/" in path
                or path.endswith(".opencode-v2/REFERENCE_FOUNDATION.md")
            ):
                return True
    return False



def _reference_evidence():
    path=Path(PROJECT)/".opencode-v2/acceptance/reference-evidence.json"
    if not path.exists():
        return {}, "MISSING"
    try:
        obj=json.loads(path.read_text(errors="replace"))
        return (obj if isinstance(obj,dict) else {}), "OK"
    except Exception:
        return {}, "MALFORMED"


def _reference_sessions(mode):
    rows=[]
    if v1_runtime_enabled():
        try:
            active=_v1_active_session_ids(strict=False)
            con=db_connect()
            raw=con.execute(
                "SELECT id,time_created FROM session "
                "WHERE agent='reference-researcher' AND directory=? "
                "ORDER BY time_created",
                (PROJECT,),
            ).fetchall()
            con.close()
            for sid,_created in raw:
                if reference_session_mode(sid)!=mode:
                    continue
                terminal=_v1_session_terminal(sid,active)
                idle_marker=(_v1_session_end_ms(sid) or 1) if terminal else None
                rows.append((sid,idle_marker))
        except Exception as exc:
            log(f"REFERENCE_GATE_DB_ERROR {exc!r}")
        return rows
    try:
        con=db_connect()
        rows=con.execute(
            "SELECT id,time_idle FROM session_v2 "
            "WHERE agent='reference-researcher' AND directory=? "
            "ORDER BY time_created",
            (PROJECT,),
        ).fetchall()
        con.close()
    except Exception as exc:
        log(f"REFERENCE_GATE_DB_ERROR {exc!r}")
    return [r for r in rows if reference_session_mode(r[0])==mode]



def _reference_progress_stats(mode):
    rows=_reference_sessions(mode)
    completed=[sid for sid,time_idle in rows if time_idle]
    active=[sid for sid,time_idle in rows if not time_idle]
    flags=[(sid,reference_session_made_progress(sid)) for sid in completed]
    productive=sum(1 for _,ok in flags if ok)
    stagnant=0
    for _,ok in reversed(flags):
        if ok:
            break
        stagnant+=1
    return completed,active,productive,stagnant


def reference_gate_snapshot():
    # PRE-PLANNING gate: only the compact external foundation must be ready.
    if not PROJECT:
        return {"state":"not-applicable","attempts":0,
                "max_attempts":MAX_REFERENCE_FOUNDATION_SESSIONS}

    ctrl=Path(PROJECT)/".opencode-v2"
    if acceptance_reference_policy() != "external-required":
        return {"state":"not-required","attempts":0,
                "max_attempts":MAX_REFERENCE_FOUNDATION_SESSIONS}

    evidence,parse_state=_reference_evidence()
    foundation=ctrl/"REFERENCE_FOUNDATION.md"
    foundation_text=foundation.read_text(errors="replace") if foundation.exists() else ""
    foundation_result=str(evidence.get("foundation_result") or "").upper()

    completed,active,productive,stagnant=_reference_progress_stats("foundation")
    ready=(
        foundation.exists()
        and reference_foundation_marker_complete(foundation_text)
        and foundation_result=="READY"
    )
    hard=len(completed)>=MAX_REFERENCE_FOUNDATION_SESSIONS
    stalled=stagnant>=MAX_REFERENCE_STAGNANT_SESSIONS
    state="ready" if ready else "blocked" if (hard or stalled) else "pending"
    return {
        "phase":"foundation",
        "state":state,
        "attempts":len(completed),
        "max_attempts":MAX_REFERENCE_FOUNDATION_SESSIONS,
        "productive_sessions":productive,
        "stagnant_tail":stagnant,
        "active_sessions":active,
        "foundation_result":foundation_result or parse_state,
        "foundation_present":foundation.exists(),
        "session_ids":completed[-MAX_REFERENCE_FOUNDATION_SESSIONS:],
    }


def reference_validation_gate_snapshot():
    # FINAL gate: full independent evidence must be READY before acceptance.
    if not PROJECT:
        return {"state":"not-applicable","attempts":0,
                "max_attempts":MAX_REFERENCE_VALIDATION_SESSIONS}

    ctrl=Path(PROJECT)/".opencode-v2"
    if acceptance_reference_policy() != "external-required":
        return {"state":"not-required","attempts":0,
                "max_attempts":MAX_REFERENCE_VALIDATION_SESSIONS}

    evidence,parse_state=_reference_evidence()
    result=str(evidence.get("result") or "").upper()
    completed,active,productive,stagnant=_reference_progress_stats("validation")
    ready=(result=="READY")
    hard=len(completed)>=MAX_REFERENCE_VALIDATION_SESSIONS
    stalled=stagnant>=MAX_REFERENCE_STAGNANT_SESSIONS
    state="ready" if ready else "blocked" if (hard or stalled) else "pending"
    return {
        "phase":"validation",
        "state":state,
        "attempts":len(completed),
        "max_attempts":MAX_REFERENCE_VALIDATION_SESSIONS,
        "productive_sessions":productive,
        "stagnant_tail":stagnant,
        "active_sessions":active,
        "evidence_result":result or parse_state,
        "session_ids":completed[-MAX_REFERENCE_VALIDATION_SESSIONS:],
    }


def _sync_reference_gate(path,data):
    if not PROJECT or data.get("state") in {"not-applicable","not-required"}:
        return data
    path.parent.mkdir(parents=True,exist_ok=True)
    payload={"owner":"supervisor",**data}
    rendered=json.dumps(payload,indent=2,sort_keys=True)+"\n"
    try:
        if not path.exists() or path.read_text(errors="replace")!=rendered:
            tmp=path.with_suffix(".tmp")
            tmp.write_text(rendered)
            os.replace(tmp,path)
            log(
                f"REFERENCE_GATE phase={data.get('phase')} state={data.get('state')} "
                f"attempts={data.get('attempts',0)} productive={data.get('productive_sessions',0)} "
                f"stagnant={data.get('stagnant_tail',0)}"
            )
    except Exception as e:
        log(f"REFERENCE_GATE_WRITE_ERROR {e!r}")
    return data


def sync_reference_gate():
    data=reference_gate_snapshot()
    path=Path(PROJECT)/".opencode-v2/reference-gate.json" if PROJECT else Path("/tmp/none")
    return _sync_reference_gate(path,data)


def sync_reference_validation_gate():
    data=reference_validation_gate_snapshot()
    path=Path(PROJECT)/".opencode-v2/reference-validation-gate.json" if PROJECT else Path("/tmp/none")
    return _sync_reference_gate(path,data)
# V2.6.9 GAMETESTNEW7 REFERENCE GATE END

def maybe_nudge_idle_root(active_sids,child_active):
    """Resume an idle completed root while durable work is still pending."""
    global root_same_session_idle_since

    # Stage A: external deterministic controller owns semantic continuation.
    if stage_a_transport_mode():
        root_same_session_idle_since=None
        return False

    if not PROJECT or child_active:
        root_same_session_idle_since=None
        return False

    root=root_orchestrator_id()
    if not root:
        root_same_session_idle_since=None
        return False

    # Preserve the original user task before any automatic continuation.
    if not ensure_original_task(root):
        root_same_session_idle_since=None
        return False

    ref_gate=sync_reference_gate()
    if ref_gate.get("state")=="blocked":
        log(
            f"ROOT_SAME_SESSION_BLOCKED reference "
            f"attempts={ref_gate.get('attempts')}"
        )
        root_same_session_idle_since=None
        return False

    state=normalized_state_snapshot(PROJECT)
    phase=state.get("resume_phase")
    if phase=="complete" or phase in {"implementation-blocked","execution-blocked"}:
        root_same_session_idle_since=None
        return False

    info=http.get_session(root)
    shape=message_shape(http.get_messages(root),info)

    # A running tool or unfinished assistant turn is not idle.
    if shape.get("tool_running") or not shape.get("assistant_completed"):
        root_same_session_idle_since=None
        return False

    message_id=shape.get("message_id") or ""
    if not message_id:
        return False

    # At most one auto-continuation for each completed assistant message.
    if message_id in root_nudged_messages:
        return False

    if root_same_session_idle_since is None:
        root_same_session_idle_since=time.time()
        return False

    if time.time()-root_same_session_idle_since < ROOT_SAME_SESSION_IDLE_GRACE:
        return False

    ok,detail=http.steer_session(root,ROOT_SAME_SESSION_CONTINUATION_PROMPT)
    if not ok:
        log(f"ROOT_SAME_SESSION_NUDGE_FAILED session={root} phase={phase} detail={detail}")
        root_same_session_idle_since=time.time()
        return False

    root_nudged_messages.add(message_id)
    root_same_session_idle_since=None
    log(f"ROOT_SAME_SESSION_NUDGE session={root} phase={phase} message={message_id}")
    csv("ROOT_SAME_SESSION_NUDGE",root,"orchestrator",f"phase={phase} message={message_id}")
    return True


def maybe_continue_root(active_sids,child_active):
    """Replace only a terminated root; never copy its conversation or child prose."""
    global root_idle_since

    # Stage A: external deterministic controller owns semantic continuation.
    if stage_a_transport_mode():
        root_idle_since=None
        return False

    if not PROJECT:
        root_idle_since=None
        return False

    root=root_orchestrator_id()

    # Capture the real user task as soon as we can see the root session.
    # This happens before any rollover and even while a child is active.
    task_ok=True
    if root:
        task_ok=ensure_original_task(root)

    if child_active:
        root_idle_since=None
        return False

    if not root or root in active_sids:
        root_idle_since=None
        return False

    # Fail closed. Never launch a generic continuation root unless the real
    # user request has already been made durable.
    if not task_ok:
        log(f"ROOT_CONTINUATION_BLOCKED original_task_missing root={root}")
        csv(
            "ROOT_CONTINUATION_BLOCKED",
            root,
            "orchestrator",
            "original_task_missing",
        )
        root_idle_since=None
        return False
    ref_gate=sync_reference_gate()
    if ref_gate.get("state")=="blocked":
        log(
            f"ROOT_CONTINUATION_BLOCKED reference "
            f"attempts={ref_gate.get('attempts')}"
        )
        root_idle_since=None
        return False

    state=normalized_state_snapshot(PROJECT); phase=state.get("resume_phase")
    if phase=="complete": return False
    if phase in {"implementation-blocked","execution-blocked"}:
        blockers=",".join(item.get("deliverable","") for item in state.get("execution_blockers",[]))
        if root_idle_since is None:
            log(f"ROOT_CONTINUATION_BLOCKED phase={phase} blockers={blockers or 'planner'}")
            root_idle_since=time.time()
        return False
    if root_idle_since is None: root_idle_since=time.time(); return False
    if time.time()-root_idle_since<5: return False
    if root_restart_count()>=MAX_ROOT_RESTARTS:
        count=root_restart_count()
        newly_blocked=record_root_continuation_limit(root,phase)
        if newly_blocked:
            log(f"ROOT_CONTINUATION_LIMIT phase={phase} count={count}")
            csv(
                "ROOT_CONTINUATION_LIMIT",
                root,
                "orchestrator",
                f"phase={phase} count={count} limit={MAX_ROOT_RESTARTS}",
            )
        sync_control_status_snapshot()
        return False
    ok,detail=http.start_agent_session(root_agent_name(),ROOT_CONTINUATION_PROMPT)
    if not ok:
        log(f"ROOT_CONTINUATION_FAILED phase={phase} detail={detail}")
        root_idle_since=time.time(); return False
    record_root_restart(detail,phase); record_root_session(detail); root_idle_since=None
    log(f"ROOT_CONTINUATION_STARTED session={detail} phase={phase}")
    csv("ROOT_CONTINUATION_STARTED",detail,"orchestrator",phase)
    return True

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
            return False
        if kind=="plan":
            reconcile_plan_contract_revisions()
            rearm_splits_after_parent_contract_repair()
        return True
    except Exception as e:
        log(f"CONTROL_GUARD_ERROR kind={kind} error={e!r}")
        return False

def compile_structured_plan():
    if not PROJECT:
        return False
    try:
        r=subprocess.run(
            [sys.executable,str(ROOT/"scripts"/"structured_plan.py"),
             "--project",PROJECT],
            capture_output=True,text=True,timeout=8
        )
        if r.returncode!=0:
            detail=(r.stdout+r.stderr).strip().replace("\n"," | ")
            log(f"STRUCTURED_PLAN_INVALID detail={detail[:1600]}")
        return r.returncode==0
    except Exception as e:
        log(f"STRUCTURED_PLAN_COMPILE_ERROR error={e!r}")
        return False

def control_guard_loop():
    sigs={}
    while True:
        try:
            if PROJECT:
                ctrl=Path(PROJECT)/".opencode-v2"
                structured=ctrl/STRUCTURED_PLAN_FILENAME
                if structured.exists():
                    sig=(structured.stat().st_mtime_ns,structured.stat().st_size)
                    if sigs.get(STRUCTURED_PLAN_FILENAME)!=sig:
                        sigs[STRUCTURED_PLAN_FILENAME]=sig
                        if compile_structured_plan():
                            log("STRUCTURED_PLAN_COMPILED source=IMPLEMENTATION_PLAN.structured.json")
                            csv("STRUCTURED_PLAN_COMPILED",detail="source=IMPLEMENTATION_PLAN.structured.json")
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

    # Stage A: lessons-learner is semantic and controller-owned.
    if stage_a_transport_mode():
        return False
    if lessons_started or not PROJECT: return
    if normalized_state_snapshot(PROJECT).get("resume_phase")!="complete": return
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


def persist_live_status(payload):
    """Atomically publish the shared live-status snapshot across supervisors."""
    atomic_write_text(
        LIVE_STATUS,
        json.dumps(payload,separators=(",",":")),
    )


def api_poll_loop():
    while True:
        try:
            statuses=http.get_status()
            active=set(statuses)
            backend_snapshot=backend_sampler.sample()
            now=time.time()
            rows={}
            child_active=False
            root_context_candidate=None

            for sid in list(active):
                info=http.get_session(sid)
                messages=http.get_messages(sid)
                shape=message_shape(messages,info)
                directory=shape["directory"]

                if PROJECT and directory and os.path.realpath(directory)!=os.path.realpath(PROJECT):
                    continue

                agent=shape["agent"]
                parent=shape["parent"]

                if agent=="reference-researcher" and parent:
                    ref_mode=reference_session_mode(sid)
                    ref_gate=(
                        reference_validation_gate_snapshot()
                        if ref_mode=="validation"
                        else reference_gate_snapshot()
                    )
                    if ref_gate.get("state")=="blocked":
                        abort_session(
                            sid,
                            f"reference_{ref_mode}_gate_blocked "
                            f"attempts={ref_gate.get('attempts',0)} "
                            f"stagnant_tail={ref_gate.get('stagnant_tail',0)}",
                            agent,
                        )
                        log(
                            f"REFERENCE_GATE_ABORT session={sid} mode={ref_mode} "
                            f"attempts={ref_gate.get('attempts',0)}"
                        )
                        continue

                if agent=="implementation-planner" and parent:
                    ref_gate=sync_reference_gate()
                    if ref_gate.get("state") not in {
                        "ready", "not-required", "not-applicable"
                    }:
                        abort_session(
                            sid,
                            f"reference_gate_{ref_gate.get('state')} "
                            f"attempts={ref_gate.get('attempts',0)}",
                            agent,
                        )
                        log(
                            f"REFERENCE_GATE_PLANNER_ABORT session={sid} "
                            f"state={ref_gate.get('state')} "
                            f"attempts={ref_gate.get('attempts',0)}"
                        )
                        continue

                child_active=child_active or (
                    bool(parent)
                    and (
                        shape.get("tool_running")
                        or not shape.get("assistant_completed")
                    )
                )
                if (agent=="orchestrator" and not parent and
                    isinstance(shape.get("context_input"),(int,float)) and
                    shape["context_input"]>=ROOT_CONTEXT_INPUT_CEILING and
                    not shape["tool_running"]):
                    root_context_candidate=(sid,int(shape["context_input"]))

                live=None
                if parent:
                    live=ensure_event_watch(sid)

                if parent and (agent in IMPLEMENTATION_AGENTS or parse_deliverable(strip_subagent_prefix(shape["first_user"]))):
                    enforce_assignment(sid,agent,shape["first_user"])

                did,attempt=session_task.get(sid,("",0))
                execution=meaningful_worker_execution(sid,did) if did else ""
                if execution:
                    consume_operator_reservation(sid,did,execution)
                probe_reason=probe_loop_reason(sid,agent,did,shape["last_tool_id"])
                if probe_reason and not shape["tool_running"]:
                    abort_session(sid,probe_reason,agent)
                    log(f"PROBE_RECYCLE session={sid} deliverable={did} {probe_reason}")
                    csv("PROBE_RECYCLE",sid,agent,probe_reason)
                    continue
                key=(shape["message_id"],shape["last_tool_id"])
                # Critical fail-open rule:
                # /session/active can expose a running child before its current
                # assistant message/parts become observable. Unknown is NOT
                # "no tool for 120s". Start/continue watchdog only when a live,
                # unfinished assistant message is actually visible.
                live_observable=bool(live and live.get("connected"))
                tool_running=shape["tool_running"] or bool(live and live.get("tool_running"))
                planner_compaction_active=False
                if agent=="implementation-planner" and parent:
                    compaction_state=latest_compaction_state(sid)
                    planner_compaction_active=bool(
                        compaction_state.get("seq")
                        and compaction_state.get("status") not in {"completed","failed"}
                    )
                can_watch=(bool(parent) and not shape.get("assistant_completed")
                           and not tool_running
                           and not planner_compaction_active
                           and (shape.get("observable") or live_observable))

                progress_marker=visible_progress_marker(shape,live)
                age,st=watchdog_age(
                    sid,key,can_watch,progress_marker=progress_marker
                )
                reasoning=max(shape["reasoning"],int((live or {}).get("reasoning",0)))
                text_chars=max(shape["text"],int((live or {}).get("text",0)))
                fallback_reason=effective_fallback_reason(
                    sid,agent,did,shape.get("observable") or live_observable,
                    backend_snapshot=backend_snapshot,
                ) if parent and not planner_compaction_active else ""
                planner_retired=False

                if agent=="implementation-planner" and parent:
                    existing_plan=structured_plan_path()
                    checkpoint=planner_checkpoints.setdefault(
                        sid,{"started":time.monotonic()}
                    )
                    checkpoint.setdefault("started",time.monotonic())

                    candidate_outcome=planner_fresh_candidate_outcome(
                        sid,existing_plan
                    )
                    if candidate_outcome and not checkpoint.get("aborted"):
                        checkpoint["aborted"]=True
                        if candidate_outcome=="invalid":
                            reason="planner_fresh_candidate_invalid"
                            if planner_restart_count()<MAX_PLANNER_RESTARTS:
                                record_planner_restart(sid,reason)
                        else:
                            reason="planner_fresh_candidate_ready"
                        abort_session(sid,reason,agent)
                        log(
                            f"PLANNER_FRESH_HANDOFF session={sid} "
                            f"outcome={candidate_outcome}"
                        )
                        csv(
                            "PLANNER_FRESH_HANDOFF",sid,agent,
                            f"outcome={candidate_outcome}",
                        )
                        planner_retired=True

                    progress_reason=(
                        "" if planner_retired else planner_retirement_reason(
                            sid,
                            time.monotonic()-checkpoint["started"],
                            existing_plan,
                            paused=planner_compaction_active,
                        )
                    )
                    if progress_reason and not checkpoint.get("aborted"):
                        checkpoint["aborted"]=True
                        if planner_restart_count()<MAX_PLANNER_RESTARTS:
                            record_planner_restart(sid,progress_reason)
                        abort_session(sid,progress_reason,agent)
                        planner_retired=True

                if can_watch and st["aborted_key"]!=key and not planner_retired:
                    hs,hr,ht=watchdog_limits(agent)

                    reason=planner_context_reason(
                        agent,shape["context_input"],tool_running
                    )
                    if not reason:
                        if reasoning>=hr:
                            reason=f"reasoning_chars={reasoning}"
                        elif text_chars>=ht:
                            reason=f"text_chars={text_chars}"
                        elif age>=hs:
                            visible_decision=visible_watchdog_decision(
                                age,backend_snapshot,base_seconds=hs
                            )
                            if visible_decision.get("abort"):
                                detail=str(
                                    visible_decision.get("reason")
                                    or "backend-not-progressing"
                                )
                                reason=(
                                    f"no_tool_age={int(age)}s {detail}"
                                )

                    if reason:
                        st["aborted_key"]=key
                        if agent=="implementation-planner":
                            if planner_restart_count()>=MAX_PLANNER_RESTARTS:
                                reason=f"planner_restart_limit={MAX_PLANNER_RESTARTS} {reason}"
                            else:
                                record_planner_restart(sid,reason)
                        if abort_session(
                            sid,
                            f"runaway {reason} reasoning_chars={reasoning} text_chars={text_chars}",
                            agent,
                        ):
                            abort_count[sid]=abort_count.get(sid,0)+1

                if fallback_reason and not (live or {}).get("fallback_aborted"):
                    if live is not None: live["fallback_aborted"]=True
                    abort_session(sid,f"runaway {fallback_reason}",agent)

                fallback_state=(live or {}).get("fallback_decision") or {}
                phase=session_watchdog_phase(
                    shape,live,backend_snapshot,visible_age=age
                )
                row={
                    "session":sid,
                    "agent":agent,
                    "parent":parent,
                    "deliverable":did,
                    "attempt":attempt,
                    "reasoning":reasoning,
                    "text":text_chars,
                    "reasoning_tokens":shape.get("reasoning_tokens",0),
                    "output_tokens":shape.get("output_tokens",0),
                    "cache_tokens":shape.get("cache_tokens",0),
                    "tool_running":tool_running,
                    "context_input":shape["context_input"],
                    "observable":shape.get("observable",False),
                    "assistant_completed":shape.get("assistant_completed",False),
                    "watchdog_phase":phase,
                    "visible_no_progress_age":round(age,3),
                    "sse_connected":bool(live and live.get("connected")),
                    "sse_last_progress_age":(
                        round(max(0.0,time.monotonic()-live["last_progress"]),3)
                        if live and isinstance(live.get("last_progress"),(int,float))
                        else None
                    ),
                    "invisible_no_progress_age":round(float((live or {}).get("fallback_elapsed") or 0),3),
                    "invisible_watchdog":fallback_state,
                    "seen":now,
                    "directory":directory or PROJECT or "",
                    "status":statuses.get(sid),
                }
                rows[sid]=row
                if parent:
                    emit_watchdog_telemetry(sid,row,backend_snapshot)

            payload={
                "generated":now,
                "project":PROJECT or "",
                "source":"http-status+message-poll-v2.6.7b",
                "backend":backend_snapshot,
                "sessions":list(rows.values()),
            }
            persist_live_status(payload)

            if root_context_candidate and not child_active:
                sid,context_input=root_context_candidate
                abort_session(sid,f"root_context_rollover input={context_input} ceiling={ROOT_CONTEXT_INPUT_CEILING}","orchestrator")
            stop_inactive_event_watches(active)
            sync_reference_gate()
            sync_reference_validation_gate()
            sync_control_status_snapshot()
            nudged=maybe_nudge_idle_root(active,child_active)
            if not nudged:
                maybe_continue_root(active,child_active)
            maybe_launch_lessons(active,child_active)
            merge_lesson_candidates()

        except Exception as e:
            trace=" | ".join(
                line.strip()
                for line in traceback.format_exc(limit=8).splitlines()
                if line.strip()
            )
            log(f"HTTP_POLL_ERROR {e!r} trace={trace[:4000]}")

        time.sleep(POLL)

def pending_ledger_session_ids():
    """Current unclassified materialized attempts survive supervisor restart."""
    pending=set()
    data=load_attempts()
    entries=data.get("deliverables",{})
    if not isinstance(entries,dict):
        raise StateCorruptionError("attempt ledger deliverables must be an object")
    for did,entry in entries.items():
        if not isinstance(entry,dict) or ready_info(did):
            continue
        try:
            count=int(entry.get("count") or 0)
        except (TypeError,ValueError):
            continue
        sessions=entry.get("sessions",[])
        if count < 1 or not isinstance(sessions,list) or not sessions:
            continue
        sid=sessions[-1]
        if not isinstance(sid,str) or not sid or sid.startswith("dispatch:"):
            continue
        history=entry.get("failure_history",[])
        classified=any(
            isinstance(item,dict) and item.get("attempt")==count
            for item in history if isinstance(history,list)
        )
        if not classified:
            pending.add(sid)
    return pending


def reconcile_planner_completion(sid):
    if sid in planner_completion_seen:
        return

    abort_reason=persisted_abort_reason(sid)
    if re.match(r"^reference_gate_(?:pending|blocked)\b",abort_reason):
        # A supervisor precondition abort is not a failed planning attempt.
        log(
            f"PLANNER_PRECONDITION_ABORT_NOT_COUNTED session={sid} "
            f"reason={abort_reason}"
        )
        csv(
            "PLANNER_PRECONDITION_ABORT_NOT_COUNTED",sid,
            "implementation-planner",abort_reason,
        )
        resolve_abort_intent(sid,"planner-precondition-not-counted")
        planner_completion_seen.add(sid)
        return

    if not plan_ready():
        # Compile + guard synchronously when a planner goes idle. This avoids a
        # race where the root launches another planner before valid structured
        # output has been deterministically rendered/finalized.
        if compile_structured_plan():
            control_guard("plan")
    if not plan_ready():
        current_plan=planner_plan_state(structured_plan_path())
        repair=Path(PROJECT)/".opencode-v2"/STRUCTURED_PLAN_REPAIR_FILENAME
        if current_plan.get("meaningful_signature"):
            reason=(
                "planner_completed_with_invalid_structured_plan"
                if repair.exists()
                else "planner_completed_before_structured_plan_finalization"
            )
            if planner_restart_count()<MAX_PLANNER_RESTARTS:
                record_planner_restart(sid,reason)
            log(
                f"PLANNER_COMPLETED_INVALID session={sid} "
                f"reason={reason} restart_count={planner_restart_count()}"
            )
            csv(
                "PLANNER_COMPLETED_INVALID",sid,"implementation-planner",
                f"reason={reason} restart_count={planner_restart_count()}",
            )
        else:
            if planner_restart_count()<MAX_PLANNER_RESTARTS:
                record_planner_restart(
                    sid,
                    "planner_completed_without_meaningful_structured_plan",
                )
            log(
                f"PLANNER_COMPLETED_NO_PROGRESS session={sid} "
                f"restart_count={planner_restart_count()}"
            )
            csv(
                "PLANNER_COMPLETED_NO_PROGRESS",sid,"implementation-planner",
                f"restart_count={planner_restart_count()}",
            )
    planner_completion_seen.add(sid)


def worker_behavior_abort_reason(reason):
    """Return deterministic model-behavior failures, never infrastructure faults."""
    reason=str(reason or "")
    prefixes=(
        "probe_research_loop_no_owned_progress",
        "early_write_deadline_no_owned_artifact_delta",
    )
    return reason if reason.startswith(prefixes) else ""


def classify_post_session_failure(did,detail,execution):
    """Classify a terminal implementation result from authoritative contract state.

    Missing declared output is worker failure, not a bad plan. `bad-plan` is
    reserved for an actually unusable contract: unsafe/missing Verify or no
    concrete owned artifact path at all.
    """
    if str(detail or "").startswith("verify-command-unsafe:"):
        return "bad-plan"
    if detail=="verify-command-missing" and not execution:
        return "bad-plan"
    if detail=="owned-artifacts-missing" and not execution:
        leaf=(load_manifest().get("leaves") or {}).get(did,{})
        if not owned_artifact_paths(leaf):
            return "bad-plan"
    return "genuine"


def reconcile_idle_implementation_session(sid,agent):
    if sid in post_finalize_seen:
        return
    did=session_task.get(
        sid,(parse_deliverable(first_user_text_db(sid)),0)
    )[0]
    if not did:
        worker_sandbox_cleanup_session(sid)
        post_finalize_seen.add(sid)
        return

    abort_reason=immediate_runtime_abort(sid)
    cached_abort=supervisor_abort_reasons.get(sid,"")
    durable_abort=persisted_abort_reason(sid)
    supervisor_abort=cached_abort or durable_abort
    compaction_abort=(
        "opencode-compaction-template"
        if compaction_failure(sid)=="compaction.failed"
        else ""
    )
    worker_behavior_reason=worker_behavior_abort_reason(supervisor_abort)
    infrastructure_reason=(
        abort_reason
        or ("" if worker_behavior_reason else supervisor_abort)
        or compaction_abort
    )

    if worker_behavior_reason and not ready_info(did):
        # New37: this is not an infrastructure outage. The model successfully
        # executed several read/probe tool turns but violated the probe's
        # durable-progress contract. Count it as a genuine leaf failure so the
        # normal bounded retry -> recursive-split recovery can activate.
        finalized=False
        detail="not-attempted"
        if durable_worker_execution(did,sid):
            finalized,detail=post_session_finalize(did,sid=sid)
            if detail.startswith("verify-deps-pending:"):
                note_verify_wait_once(did,sid,agent,detail)
                return
            verify_wait_log_state.pop(did,None)
            log(
                f"POST_SESSION_VERIFY_AFTER_WORKER_BEHAVIOR_ABORT session={sid} "
                f"deliverable={did} result={detail}"
            )
            csv(
                "POST_SESSION_VERIFY_AFTER_WORKER_BEHAVIOR_ABORT",sid,agent,
                f"{did} {detail}"
            )
        if not finalized and not ready_info(did):
            recorded,outcome=record_leaf_failure(
                did,worker_behavior_reason,"genuine",sid=sid
            )
            release_operator_reservation(
                sid,did,worker_behavior_reason
            )
            log(
                f"LEAF_WORKER_BEHAVIOR_FAILURE session={sid} "
                f"deliverable={did} recorded={str(recorded).lower()} "
                f"outcome={outcome} reason={worker_behavior_reason}"
            )
            csv(
                "LEAF_WORKER_BEHAVIOR_FAILURE",sid,agent,
                f"{did} recorded={str(recorded).lower()} "
                f"outcome={outcome} reason={worker_behavior_reason}"
            )
    elif infrastructure_reason and not ready_info(did):
        finalized=False
        detail="not-attempted"
        if durable_worker_execution(did,sid):
            finalized,detail=post_session_finalize(did,sid=sid)
            if detail.startswith("verify-deps-pending:"):
                note_verify_wait_once(did,sid,agent,detail)
                return
            verify_wait_log_state.pop(did,None)
            log(
                f"POST_SESSION_VERIFY_AFTER_INFRA session={sid} "
                f"deliverable={did} result={detail}"
            )
            csv(
                "POST_SESSION_VERIFY_AFTER_INFRA",sid,agent,
                f"{did} {detail}"
            )
        if not finalized and not ready_info(did):
            kind=(
                "supervisor-compaction-retire"
                if "child_compaction" in infrastructure_reason
                else "runtime-cancel"
            )
            granted,grant_detail=record_infrastructure_abort(
                sid,did,infrastructure_reason,kind
            )
            # Crash/replay safe: if the grant was already persisted, the
            # failure-history classification may still need to be completed.
            if granted or grant_detail=="already-recorded":
                record_leaf_failure(
                    did,infrastructure_reason,"infrastructure",sid=sid
                )
            release_operator_reservation(
                sid,did,infrastructure_reason
            )
            log(
                f"LEAF_INFRASTRUCTURE_ABORT session={sid} "
                f"deliverable={did} granted={str(granted).lower()} "
                f"reason={infrastructure_reason}"
            )
    else:
        execution=meaningful_worker_execution(sid,did)
        if execution:
            consume_operator_reservation(sid,did,execution)
        if not ready_info(did):
            ok,detail=post_session_finalize(did,sid=sid)
            if detail.startswith("verify-deps-pending:"):
                note_verify_wait_once(did,sid,agent,detail)
                return
            verify_wait_log_state.pop(did,None)
            log(
                f"POST_SESSION_VERIFY session={sid} "
                f"deliverable={did} result={detail}"
            )
            csv("POST_SESSION_VERIFY",sid,agent,f"{did} {detail}")
            if not ok and detail != "not-applicable":
                if detail.startswith(("verify-infrastructure-","verification-error-")):
                    granted,grant_detail=record_infrastructure_abort(
                        sid,did,detail,"verification-environment"
                    )
                    recorded=False; outcome=grant_detail
                    if granted or grant_detail=="already-recorded":
                        recorded,outcome=record_leaf_failure(
                            did,detail,"infrastructure",sid=sid
                        )
                    log(
                        f"LEAF_VERIFY_INFRASTRUCTURE session={sid} "
                        f"deliverable={did} granted={str(granted).lower()} "
                        f"detail={detail} outcome={outcome}"
                    )
                    csv(
                        "LEAF_VERIFY_INFRASTRUCTURE",sid,agent,
                        f"{did} granted={str(granted).lower()} "
                        f"detail={detail} outcome={outcome}"
                    )
                else:
                    classification=classify_post_session_failure(
                        did,detail,execution
                    )
                    recorded,outcome=record_leaf_failure(
                        did,detail,classification,sid=sid
                    )
                    if recorded:
                        log(
                            f"LEAF_FAILURE session={sid} deliverable={did} "
                            f"classification={classification} outcome={outcome}"
                        )
                        csv(
                            "LEAF_FAILURE",sid,agent,
                            f"{did} classification={classification} "
                            f"outcome={outcome}"
                        )

    if cached_abort:
        supervisor_abort_reasons.pop(sid,None)
    if durable_abort:
        resolve_abort_intent(
            sid,
            "genuine-worker-behavior-failure"
            if worker_behavior_reason else
            "infrastructure-classified"
            if infrastructure_reason else
            "completed-without-observed-abort"
        )
    else:
        # Close a requested intent that never produced an aborted terminal
        # session (e.g. crash before the external interrupt happened).
        resolve_abort_intent(sid,"completed-without-observed-abort")
    # execute.after no longer destroys worker runtime. Cleanup occurs
    # only after terminal supervisor finalization/classification. Early
    # verify-deps-pending returns intentionally retain the environment.
    worker_sandbox_cleanup_session(sid)
    post_finalize_seen.add(sid)


def restart_orphan_candidate(sid, active_ids, pending_sessions, time_created):
    """A pre-restart pending child is orphaned only if the live server lost it."""
    return (
        sid in pending_sessions
        and int(time_created or 0) < START_MS
        and sid not in set(active_ids or ())
    )


def reconcile_restart_orphaned_implementation_session(sid,agent):
    """Classify an in-flight child left behind by a restarted OpenCode server.

    v1 keeps active session execution in the server process.  A child created
    before this supervisor process, absent from the fresh server's active set,
    and lacking a terminal assistant record cannot resume.  It is an
    infrastructure abort, never a semantic failure.  This path preserves any
    partial artifact and releases only the bounded infrastructure recovery
    defined by the attempt ledger.
    """
    if sid in post_finalize_seen:
        return
    did=session_task.get(sid,(parse_deliverable(first_user_text_db(sid)),0))[0]
    if not did or ready_info(did):
        return
    reason="opencode-server-restart-incomplete-session"
    granted,detail=record_infrastructure_abort(
        sid,did,reason,"opencode-server-restart"
    )
    if granted or detail=="already-recorded":
        record_leaf_failure(did,reason,"infrastructure",sid=sid)
    release_operator_reservation(sid,did,reason)
    log(
        f"LEAF_RESTART_ORPHANED_INFRASTRUCTURE session={sid} "
        f"deliverable={did} granted={str(granted).lower()} detail={detail}"
    )
    csv(
        "LEAF_RESTART_ORPHANED_INFRASTRUCTURE",sid,agent,
        f"{did} granted={str(granted).lower()} detail={detail}",
    )
    worker_sandbox_cleanup_session(sid)
    post_finalize_seen.add(sid)


def reconcile_compaction_event(sid,agent,comps):
    latest=latest_compaction_state(sid)
    status=latest.get("status","")
    seq=int(latest.get("seq") or 0)
    error_type=latest.get("error_type","")
    # Do not mark a newly-created/running compaction as seen. OpenCode updates
    # the same DB row in place; New32 showed that count-only tracking can log an
    # in-flight row as ALLOWED and then permanently miss its failed terminal
    # update.
    if not seq or status not in {"completed","failed"}:
        return
    transition=(seq,status,error_type)
    if compaction_seen.get(sid)==transition:
        return
    did,_=session_task.get(
        sid,(parse_deliverable(first_user_text_db(sid)),0)
    )
    if did and ready_info(did):
        log(
            f"COMPACTION_AFTER_DONE session={sid} agent={agent} "
            f"deliverable={did} compactions={comps}"
        )
        compaction_seen[sid]=transition
        return
    if agent=="implementation-planner" and plan_ready():
        log(
            f"COMPACTION_AFTER_DONE session={sid} agent={agent} control_ready=1"
        )
        compaction_seen[sid]=transition
        return
    failed=error_type if status=="failed" else ""
    if failed:
        granted,detail=(False,"not-implementation-child")
        if failed=="compaction.failed" and did and agent in IMPLEMENTATION_AGENTS:
            granted,detail=record_compaction_infrastructure_failure(sid,did)
            if granted or detail=="already-recorded":
                record_leaf_failure(
                    did,"opencode-compaction-template","infrastructure",sid=sid
                )
        log(
            f"COMPACTION_FAILED session={sid} agent={agent} "
            f"deliverable={did or 'unknown'} type={failed} "
            f"infrastructure_credit={str(granted).lower()} detail={detail}"
        )
        csv(
            "COMPACTION_FAILED",sid,agent,
            f"{did or 'unknown'} type={failed} "
            f"infrastructure_credit={str(granted).lower()} detail={detail}"
        )
        compaction_seen[sid]=transition
        return

    compaction_limit=(
        MAX_REFERENCE_COMPACTIONS
        if agent=="reference-researcher"
        else MAX_IMPLEMENTATION_COMPACTIONS
        if agent in IMPLEMENTATION_AGENTS
        else 1
    )
    if comps<=compaction_limit:
        log(
            f"COMPACTION_ALLOWED session={sid} agent={agent} "
            f"count={comps} limit={compaction_limit}"
        )
        csv(
            "COMPACTION_ALLOWED",sid,agent,
            f"count={comps} limit={compaction_limit}"
        )
    else:
        if not abort_session(
            sid,
            f"child_compaction count={comps}; limit={compaction_limit}",
            agent,
        ):
            raise RuntimeError(
                f"compaction interrupt failed for session {sid}"
            )
        log(
            f"RETIRED_COMPACTION session={sid} agent={agent} "
            f"compactions={comps} limit={compaction_limit}"
        )
    # Seen only after the durable/logical transition succeeded.
    compaction_seen[sid]=transition


def persisted_reconcile_loop():
    while not DB.exists():
        time.sleep(0.5)
    while True:
        try:
            reconcile_split_proposals()
            reconcile_split_parent_completions()
            sync_control_status_snapshot()
            pending_sessions=pending_ledger_session_ids()

            if v1_runtime_enabled():
                active=_v1_active_session_ids(strict=True)
                con=db_connect()
                rows=con.execute(
                    "SELECT id,coalesce(agent,''),time_created "
                    "FROM session "
                    "WHERE parent_id IS NOT NULL AND directory=?",
                    (PROJECT,),
                ).fetchall() if PROJECT else []
                con.close()

                for sid,agent,time_created in rows:
                    if int(time_created or 0)<START_MS and sid not in pending_sessions:
                        continue
                    prompt=first_user_text_db(sid)
                    if (
                        agent in IMPLEMENTATION_AGENTS
                        or parse_deliverable(strip_subagent_prefix(prompt))
                    ) and sid not in dispatch_seen:
                        enforce_assignment(sid,agent,prompt)

                    terminal=_v1_session_terminal(sid,active)
                    if agent=="implementation-planner" and terminal:
                        reconcile_planner_completion(sid)

                    comps=_v1_compaction_count(sid)
                    reconcile_compaction_event(sid,agent,comps)

                    if agent in IMPLEMENTATION_AGENTS and terminal:
                        reconcile_idle_implementation_session(sid,agent)
                    elif (
                        agent in IMPLEMENTATION_AGENTS
                        and restart_orphan_candidate(
                            sid, active, pending_sessions, time_created
                        )
                    ):
                        reconcile_restart_orphaned_implementation_session(sid,agent)
            else:
                con=db_connect()
                rows=con.execute(
                    "SELECT s.id,coalesce(s.agent,''),"
                    "(SELECT count(*) FROM session_message m "
                    "WHERE m.session_id=s.id AND m.type='compaction'),"
                    "s.time_idle,s.time_created "
                    "FROM session_v2 s "
                    "WHERE s.parent_id IS NOT NULL AND s.directory=?",
                    (PROJECT,),
                ).fetchall() if PROJECT else []
                con.close()
                for sid,agent,comps,time_idle,time_created in rows:
                    if int(time_created or 0)<START_MS and sid not in pending_sessions:
                        continue
                    prompt=first_user_text_db(sid)
                    if (
                        agent in IMPLEMENTATION_AGENTS
                        or parse_deliverable(strip_subagent_prefix(prompt))
                    ) and sid not in dispatch_seen:
                        enforce_assignment(sid,agent,prompt)
                    if agent=="implementation-planner" and time_idle:
                        reconcile_planner_completion(sid)
                    reconcile_compaction_event(sid,agent,comps)
                    if agent in IMPLEMENTATION_AGENTS and time_idle:
                        reconcile_idle_implementation_session(sid,agent)
        except Exception as exc:
            log(f"PERSISTED_RECONCILE_ERROR {exc!r}")
        time.sleep(0.5)


def main():
    global PROJECT
    ap=argparse.ArgumentParser(add_help=False)
    ap.add_argument("--claim-dispatch"); ap.add_argument("--claim-splitter"); ap.add_argument("--claim-corrective-splitter"); ap.add_argument("--complete-splitter"); ap.add_argument("--recover-splitter-output-limit"); ap.add_argument("--recover-splitter-profile-change"); ap.add_argument("--prior-splitter-model"); ap.add_argument("--recover-splitter-execution-contract"); ap.add_argument("--prior-splitter-steps"); ap.add_argument("--recover-splitter-direct-context-contract"); ap.add_argument("--recover-historical-splitter-corrective"); ap.add_argument("--recover-exhausted-splitter-fallback"); ap.add_argument("--recover-denied-tool-finalize"); ap.add_argument("--recover-false-ownership-finalize"); ap.add_argument("--recover-version-skew-zero-work-dispatch"); ap.add_argument("--recover-sandbox-wrapper-history-poison"); ap.add_argument("--recover-context-delivery-failure"); ap.add_argument("--recover-external-execution-contract"); ap.add_argument("--correction-file"); ap.add_argument("--recover-historical-parent-contract-repair-resolution"); ap.add_argument("--recover-legacy-handoff-writer-verify"); ap.add_argument("--recover-nested-handoff-writer-verify"); ap.add_argument("--recover-stale-split-parent-contract"); ap.add_argument("--recover-false-parent-contract-repair"); ap.add_argument("--resolve-false-parent-contract-repair")
    ap.add_argument("--reconcile-splits-once",action="store_true")
    ap.add_argument("--render-runtime-prompt")
    ap.add_argument("--render-dispatch-prompt")
    ap.add_argument("--root-read-check")
    ap.add_argument("--early-write-check")
    ap.add_argument("--tool-name",default="")
    ap.add_argument("--tool-args-b64",default="")
    ap.add_argument("--splitter-tool-check")
    ap.add_argument("--progress-handoff-tool-check")
    ap.add_argument("--confirm-plugin-interrupt")
    ap.add_argument("--materialize-dispatch-child")
    ap.add_argument("--agent")
    ap.add_argument("--prompt"); ap.add_argument("--project")
    ap.add_argument("--dispatch-token"); ap.add_argument("--splitter-output-b64")
    ap.add_argument("--opencode-base-url",default="")
    args,unknown=ap.parse_known_args()
    if args.opencode_base_url:
        # The plugin completion hook runs in a short-lived child process.  Pass
        # its known native server identity explicitly so a bounded corrective
        # continuation does not depend on inherited discovery state.
        os.environ["V2_OPENCODE_BASE_URL"]=args.opencode_base_url.rstrip("/")
    if args.materialize_dispatch_child:
        if unknown or not args.project or not args.agent:
            raise SystemExit(
                "native child materialization requires --project --agent "
                "--materialize-dispatch-child"
            )
        PROJECT=args.project
        try:
            materialize_dispatch_child(args.materialize_dispatch_child,args.agent)
        except (ValueError,StateCorruptionError) as exc:
            raise SystemExit(f"DISPATCH_MATERIALIZE_DENY {exc}") from exc
        return
    if args.reconcile_splits_once:
        if unknown or not args.project:
            raise SystemExit("split reconciliation requires --project --reconcile-splits-once")
        project_path=Path(args.project).resolve()
        if not project_path.is_dir():
            raise SystemExit(f"split reconciliation project does not exist: {project_path}")
        PROJECT=str(project_path)
        print(json.dumps(reconcile_splits_once(),sort_keys=True))
        return
    if args.root_read_check:
        if unknown or not args.project:
            raise SystemExit("root read check requires --project --root-read-check")
        PROJECT=args.project
        tool_args={}
        if args.tool_args_b64:
            try:
                decoded=base64.b64decode(args.tool_args_b64).decode("utf-8")
                tool_args=json.loads(decoded)
            except Exception as exc:
                raise SystemExit(f"invalid --tool-args-b64: {exc}")
        state,detail=root_control_read_state(args.root_read_check,tool_args)
        if state=="deny":
            raise SystemExit(
                f"ROOT_READ_DENY session={args.root_read_check} {detail}"
            )
        print(
            f"ROOT_READ_{state.upper()} "
            f"session={args.root_read_check} {detail}"
        )
        return
    if args.confirm_plugin_interrupt:
        if unknown or not args.project:
            raise SystemExit("plugin interrupt confirmation requires --project")
        PROJECT=args.project
        ok,detail=confirm_plugin_interrupt(args.confirm_plugin_interrupt)
        if not ok:
            raise SystemExit(
                f"PLUGIN_INTERRUPT_CONFIRM_DENY session={args.confirm_plugin_interrupt} {detail}"
            )
        print(f"PLUGIN_INTERRUPT_CONFIRMED session={args.confirm_plugin_interrupt} {detail}")
        return
    if args.early_write_check:
        if unknown or not args.project:
            raise SystemExit("early-write check requires --project --early-write-check")
        PROJECT=args.project
        tool_args={}
        if args.tool_args_b64:
            try:
                decoded=base64.b64decode(args.tool_args_b64).decode("utf-8")
                tool_args=json.loads(decoded)
            except Exception as exc:
                raise SystemExit(f"invalid --tool-args-b64: {exc}")
        state,detail=enforce_early_write_gate(
            args.early_write_check,args.tool_name,tool_args
        )
        if state=="deny":
            raise SystemExit(f"EARLY_WRITE_DENY session={args.early_write_check} {detail}")
        label=state.upper().replace("-","_")
        print(f"EARLY_WRITE_{label} session={args.early_write_check} {detail}")
        return
    if args.progress_handoff_tool_check:
        if unknown or not args.project:
            raise SystemExit("progress handoff tool check requires --project")
        PROJECT=args.project
        tool_args={}
        if args.tool_args_b64:
            try:
                decoded=base64.b64decode(args.tool_args_b64).decode("utf-8")
                tool_args=json.loads(decoded)
            except Exception as exc:
                raise SystemExit(f"invalid --tool-args-b64: {exc}")
        state,detail=progress_handoff_tool_state(
            args.progress_handoff_tool_check,args.tool_name,tool_args
        )
        if state=="deny":
            raise SystemExit(
                f"PROGRESS_HANDOFF_DENY session={args.progress_handoff_tool_check} {detail}"
            )
        print(
            f"PROGRESS_HANDOFF_{state.upper()} "
            f"session={args.progress_handoff_tool_check} {detail}"
        )
        return
    if args.splitter_tool_check:
        if unknown or not args.project:
            raise SystemExit("splitter tool check requires --project")
        PROJECT=args.project
        tool_args={}
        if args.tool_args_b64:
            try:
                decoded=base64.b64decode(args.tool_args_b64).decode("utf-8")
                tool_args=json.loads(decoded)
            except Exception as exc:
                raise SystemExit(f"invalid --tool-args-b64: {exc}")
        state,detail=splitter_tool_boundary_state(
            args.splitter_tool_check,args.tool_name,tool_args
        )
        if state=="deny":
            raise SystemExit(
                f"SPLITTER_TOOL_DENY session={args.splitter_tool_check} {detail}"
            )
        print(f"SPLITTER_TOOL_{state.upper()} session={args.splitter_tool_check} {detail}")
        return
    if args.render_dispatch_prompt:
        if unknown or not args.project or not args.agent:
            raise SystemExit("dispatch prompt render requires --project --agent --render-dispatch-prompt")
        PROJECT=args.project
        did=args.render_dispatch_prompt
        if not valid_deliverable_id(did):
            raise SystemExit(f"DISPATCH_PROMPT_DENY invalid_deliverable={did}")
        leaf=(load_manifest().get("leaves") or {}).get(did)
        if not isinstance(leaf,dict):
            raise SystemExit(f"DISPATCH_PROMPT_DENY unknown_deliverable={did}")
        if leaf.get("split_children"):
            raise SystemExit(f"DISPATCH_PROMPT_DENY split_parent={did}")
        expected=leaf.get("role","")
        if expected!=args.agent:
            raise SystemExit(
                f"DISPATCH_PROMPT_DENY role_mismatch expected={expected} actual={args.agent}"
            )
        print(implementation_prompt(did))
        return
    if args.render_runtime_prompt:
        if unknown or not args.project or not args.agent:
            raise SystemExit("runtime prompt render requires --project --agent --render-runtime-prompt")
        PROJECT=args.project
        did=args.render_runtime_prompt
        if not valid_deliverable_id(did):
            raise SystemExit(f"RUNTIME_PROMPT_DENY invalid_deliverable={did}")
        leaf=(load_manifest().get("leaves") or {}).get(did)
        if not isinstance(leaf,dict):
            raise SystemExit(f"RUNTIME_PROMPT_DENY unknown_deliverable={did}")
        if leaf.get("split_children"):
            raise SystemExit(f"RUNTIME_PROMPT_DENY split_parent={did}")
        expected=leaf.get("role","")
        if expected!=args.agent:
            raise SystemExit(
                f"RUNTIME_PROMPT_DENY role_mismatch expected={expected} actual={args.agent}"
            )
        print(implementation_runtime_prompt(did,args.agent))
        return
    if args.claim_dispatch:
        if unknown or not args.project or not args.agent or args.prompt is None:
            raise SystemExit("dispatch claim requires --project --agent --prompt --claim-dispatch")
        PROJECT=args.project
        status,did,reason,count=preclaim_attempt(args.agent,args.prompt,args.claim_dispatch)
        if status!="claimed": raise SystemExit(f"DISPATCH_DENY deliverable={did or 'unknown'} reason={reason} count={count}")
        suffix=" operator_authorized=true" if count>AUTOMATIC_ATTEMPT_LIMIT else ""
        print(f"DISPATCH_ALLOW deliverable={did} attempt={count}{suffix}")
        return
    if args.claim_splitter:
        if unknown or not args.project or args.agent!="task-splitter" or args.prompt is None:
            raise SystemExit("splitter claim requires --project --agent task-splitter --prompt --claim-splitter")
        PROJECT=args.project
        parent=parse_split_parent(args.prompt)
        if not parent: raise SystemExit("SPLIT_DENY invalid splitter prompt")
        ok,detail=claim_splitter(parent,args.claim_splitter)
        if not ok: raise SystemExit(f"SPLIT_DENY parent={parent} reason={detail}")
        print(f"SPLIT_ALLOW parent={parent} generation=1")
        return
    if args.claim_corrective_splitter:
        if unknown or not args.project or args.agent!="task-splitter" or args.prompt is None:
            raise SystemExit("corrective splitter claim requires canonical task-splitter prompt")
        PROJECT=args.project; parent=parse_split_parent(args.prompt)
        if not parent or "SPLITTER_CORRECTIVE_ORDINAL: 1" not in args.prompt:
            raise SystemExit("SPLIT_DENY invalid corrective splitter prompt")
        ok,detail=claim_corrective_splitter(parent,args.claim_corrective_splitter)
        if not ok: raise SystemExit(f"SPLIT_DENY parent={parent} reason={detail}")
        print(f"SPLIT_CORRECTIVE_ALLOW parent={parent} ordinal=1")
        return
    if args.recover_splitter_direct_context_contract:
        if unknown or not args.project:
            raise SystemExit("splitter direct-context recovery requires --project")
        PROJECT=args.project
        ok,detail=recover_splitter_direct_context_contract(
            args.recover_splitter_direct_context_contract
        )
        if not ok:
            raise SystemExit(f"SPLIT_DIRECT_CONTEXT_RECOVERY_DENY parent={args.recover_splitter_direct_context_contract} reason={detail}")
        print(f"SPLIT_DIRECT_CONTEXT_RECOVERY_ALLOW parent={args.recover_splitter_direct_context_contract} reason={detail}")
        return
    if args.recover_historical_splitter_corrective:
        if unknown or not args.project or not args.opencode_base_url:
            raise SystemExit(
                "historical splitter corrective recovery requires "
                "--project --opencode-base-url"
            )
        PROJECT=args.project
        ok,detail=recover_historical_splitter_corrective_turn(
            args.recover_historical_splitter_corrective
        )
        if not ok:
            raise SystemExit(
                f"SPLIT_HISTORICAL_CORRECTIVE_RECOVERY_DENY "
                f"parent={args.recover_historical_splitter_corrective} "
                f"reason={detail}"
            )
        print(
            f"SPLIT_HISTORICAL_CORRECTIVE_RECOVERY_ALLOW "
            f"parent={args.recover_historical_splitter_corrective} "
            f"reason={detail}"
        )
        return
    if args.recover_exhausted_splitter_fallback:
        if unknown or not args.project:
            raise SystemExit(
                "exhausted splitter fallback requires --project"
            )
        PROJECT=args.project
        ok,detail=recover_exhausted_splitter_deterministic_handoff(
            args.recover_exhausted_splitter_fallback
        )
        if not ok:
            raise SystemExit(
                f"SPLIT_DETERMINISTIC_FALLBACK_DENY "
                f"parent={args.recover_exhausted_splitter_fallback} "
                f"reason={detail}"
            )
        print(
            f"SPLIT_DETERMINISTIC_FALLBACK_ALLOW "
            f"parent={args.recover_exhausted_splitter_fallback} "
            f"reason={detail}"
        )
        return
    if args.recover_denied_tool_finalize:
        if unknown or not args.project:
            raise SystemExit(
                "denied-tool finalize recovery requires --project"
            )
        PROJECT=args.project
        ok,detail=recover_denied_tool_finalize(
            args.recover_denied_tool_finalize
        )
        if not ok:
            raise SystemExit(
                f"DENIED_TOOL_FINALIZE_RECOVERY_DENY "
                f"deliverable={args.recover_denied_tool_finalize} "
                f"reason={detail}"
            )
        print(
            f"DENIED_TOOL_FINALIZE_RECOVERY_ALLOW "
            f"deliverable={args.recover_denied_tool_finalize} "
            f"reason={detail}"
        )
        return
    if args.recover_false_ownership_finalize:
        if unknown or not args.project:
            raise SystemExit(
                "false-ownership finalize recovery requires --project"
            )
        PROJECT=args.project
        ok,detail=recover_false_ownership_finalize(
            args.recover_false_ownership_finalize
        )
        if not ok:
            raise SystemExit(
                f"FALSE_OWNERSHIP_FINALIZE_RECOVERY_DENY "
                f"deliverable={args.recover_false_ownership_finalize} "
                f"reason={detail}"
            )
        print(
            f"FALSE_OWNERSHIP_FINALIZE_RECOVERY_ALLOW "
            f"deliverable={args.recover_false_ownership_finalize} "
            f"reason={detail}"
        )
        return
    if args.recover_version_skew_zero_work_dispatch:
        if unknown or not args.project or not args.opencode_base_url:
            raise SystemExit(
                "version-skew zero-work recovery requires "
                "--project --opencode-base-url"
            )
        PROJECT=args.project
        ok,detail=recover_version_skew_zero_work_dispatch(
            args.recover_version_skew_zero_work_dispatch
        )
        if not ok:
            raise SystemExit(
                f"VERSION_SKEW_ZERO_WORK_RECOVERY_DENY "
                f"deliverable={args.recover_version_skew_zero_work_dispatch} "
                f"reason={detail}"
            )
        print(
            f"VERSION_SKEW_ZERO_WORK_RECOVERY_ALLOW "
            f"deliverable={args.recover_version_skew_zero_work_dispatch} "
            f"reason={detail}"
        )
        return
    if args.recover_sandbox_wrapper_history_poison:
        if unknown or not args.project:
            raise SystemExit(
                "sandbox-wrapper history recovery requires --project"
            )
        PROJECT=args.project
        ok,detail=recover_sandbox_wrapper_history_poison(
            args.recover_sandbox_wrapper_history_poison
        )
        if not ok:
            raise SystemExit(
                f"SANDBOX_WRAPPER_HISTORY_RECOVERY_DENY "
                f"deliverable={args.recover_sandbox_wrapper_history_poison} "
                f"reason={detail}"
            )
        print(
            f"SANDBOX_WRAPPER_HISTORY_RECOVERY_ALLOW "
            f"deliverable={args.recover_sandbox_wrapper_history_poison} "
            f"reason={detail}"
        )
        return
    if args.recover_context_delivery_failure:
        if unknown or not args.project:
            raise SystemExit(
                "context-delivery recovery requires --project"
            )
        PROJECT=args.project
        ok,detail=recover_context_delivery_failure(
            args.recover_context_delivery_failure
        )
        if not ok:
            raise SystemExit(
                f"CONTEXT_DELIVERY_RECOVERY_DENY "
                f"deliverable={args.recover_context_delivery_failure} "
                f"reason={detail}"
            )
        print(
            f"CONTEXT_DELIVERY_RECOVERY_ALLOW "
            f"deliverable={args.recover_context_delivery_failure} "
            f"reason={detail}"
        )
        return
    if args.recover_external_execution_contract:
        if unknown or not args.project or not args.correction_file:
            raise SystemExit(
                "external execution-contract recovery requires "
                "--project --correction-file"
            )
        PROJECT=args.project
        ok,detail=recover_external_execution_contract(
            args.recover_external_execution_contract,
            args.correction_file,
        )
        if not ok:
            raise SystemExit(
                f"EXTERNAL_EXECUTION_CONTRACT_RECOVERY_DENY "
                f"deliverable={args.recover_external_execution_contract} "
                f"reason={detail}"
            )
        print(
            f"EXTERNAL_EXECUTION_CONTRACT_RECOVERY_ALLOW "
            f"deliverable={args.recover_external_execution_contract} "
            f"reason={detail}"
        )
        return
    if args.recover_historical_parent_contract_repair_resolution:
        if unknown or not args.project:
            raise SystemExit(
                "historical parent-contract repair resolution requires --project"
            )
        PROJECT=args.project
        ok,detail=recover_historical_parent_contract_repair_resolution(
            args.recover_historical_parent_contract_repair_resolution
        )
        if not ok:
            raise SystemExit(
                f"HISTORICAL_PARENT_CONTRACT_REPAIR_RESOLUTION_DENY "
                f"deliverable="
                f"{args.recover_historical_parent_contract_repair_resolution} "
                f"reason={detail}"
            )
        print(
            f"HISTORICAL_PARENT_CONTRACT_REPAIR_RESOLUTION_ALLOW "
            f"deliverable="
            f"{args.recover_historical_parent_contract_repair_resolution} "
            f"reason={detail}"
        )
        return
    if args.recover_legacy_handoff_writer_verify:
        if unknown or not args.project:
            raise SystemExit(
                "legacy handoff writer Verify recovery requires --project"
            )
        PROJECT=args.project
        ok,detail=recover_legacy_handoff_writer_verify(
            args.recover_legacy_handoff_writer_verify
        )
        if not ok:
            raise SystemExit(
                f"LEGACY_HANDOFF_WRITER_VERIFY_UPGRADE_DENY "
                f"parent={args.recover_legacy_handoff_writer_verify} "
                f"reason={detail}"
            )
        print(
            f"LEGACY_HANDOFF_WRITER_VERIFY_UPGRADE_ALLOW "
            f"parent={args.recover_legacy_handoff_writer_verify} "
            f"reason={detail}"
        )
        return
    if args.recover_nested_handoff_writer_verify:
        if unknown or not args.project:
            raise SystemExit(
                "nested handoff writer Verify recovery requires --project"
            )
        PROJECT=args.project
        ok,detail=recover_nested_handoff_writer_verify(
            args.recover_nested_handoff_writer_verify
        )
        if not ok:
            raise SystemExit(
                f"NESTED_HANDOFF_WRITER_VERIFY_UPGRADE_DENY "
                f"parent={args.recover_nested_handoff_writer_verify} "
                f"reason={detail}"
            )
        print(
            f"NESTED_HANDOFF_WRITER_VERIFY_UPGRADE_ALLOW "
            f"parent={args.recover_nested_handoff_writer_verify} "
            f"reason={detail}"
        )
        return
    if args.recover_stale_split_parent_contract:
        if unknown or not args.project:
            raise SystemExit(
                "stale split parent contract recovery requires --project"
            )
        PROJECT=args.project
        ok,detail=recover_stale_split_parent_contract(
            args.recover_stale_split_parent_contract
        )
        if not ok:
            raise SystemExit(
                f"STALE_SPLIT_CONTRACT_RECOVERY_DENY "
                f"parent={args.recover_stale_split_parent_contract} "
                f"reason={detail}"
            )
        print(
            f"STALE_SPLIT_CONTRACT_RECOVERY_ALLOW "
            f"parent={args.recover_stale_split_parent_contract} "
            f"reason={detail}"
        )
        return
    if args.recover_false_parent_contract_repair:
        if unknown or not args.project:
            raise SystemExit("false parent-contract repair recovery requires --project")
        PROJECT=args.project
        ok,detail=recover_false_parent_contract_repair(
            args.recover_false_parent_contract_repair
        )
        if not ok:
            raise SystemExit(
                f"FALSE_PARENT_CONTRACT_REPAIR_RECOVERY_DENY "
                f"parent={args.recover_false_parent_contract_repair} reason={detail}"
            )
        print(
            f"FALSE_PARENT_CONTRACT_REPAIR_ALLOW "
            f"parent={args.recover_false_parent_contract_repair} reason={detail}"
        )
        return
    if args.resolve_false_parent_contract_repair:
        if unknown or not args.project:
            raise SystemExit("false parent-contract repair resolution requires --project")
        PROJECT=args.project
        ok,detail=resolve_false_parent_contract_repair(
            args.resolve_false_parent_contract_repair
        )
        if not ok:
            raise SystemExit(
                f"FALSE_PARENT_CONTRACT_REPAIR_RESOLUTION_DENY "
                f"parent={args.resolve_false_parent_contract_repair} reason={detail}"
            )
        print(
            f"FALSE_PARENT_CONTRACT_REPAIR_RESOLUTION_ALLOW "
            f"parent={args.resolve_false_parent_contract_repair} reason={detail}"
        )
        return
    if args.recover_splitter_output_limit:
        if unknown or not args.project:
            raise SystemExit("splitter output-limit recovery requires --project")
        PROJECT=args.project
        ok,detail=recover_splitter_output_limit(args.recover_splitter_output_limit)
        if not ok:
            raise SystemExit(f"SPLIT_RECOVERY_DENY parent={args.recover_splitter_output_limit} reason={detail}")
        print(f"SPLIT_RECOVERY_ALLOW parent={args.recover_splitter_output_limit} reason={detail}")
        return
    if args.recover_splitter_profile_change:
        if unknown or not args.project or not args.prior_splitter_model:
            raise SystemExit("splitter profile recovery requires --project --prior-splitter-model")
        PROJECT=args.project
        ok,detail=recover_splitter_profile_change(
            args.recover_splitter_profile_change,args.prior_splitter_model
        )
        if not ok:
            raise SystemExit(f"SPLIT_PROFILE_RECOVERY_DENY parent={args.recover_splitter_profile_change} reason={detail}")
        print(f"SPLIT_PROFILE_RECOVERY_ALLOW parent={args.recover_splitter_profile_change} reason={detail}")
        return
    if args.recover_splitter_execution_contract:
        if unknown or not args.project or args.prior_splitter_steps is None:
            raise SystemExit("splitter execution-contract recovery requires --project --prior-splitter-steps")
        PROJECT=args.project
        ok,detail=recover_splitter_execution_contract(
            args.recover_splitter_execution_contract,args.prior_splitter_steps
        )
        if not ok:
            raise SystemExit(f"SPLIT_EXECUTION_CONTRACT_RECOVERY_DENY parent={args.recover_splitter_execution_contract} reason={detail}")
        print(f"SPLIT_EXECUTION_CONTRACT_RECOVERY_ALLOW parent={args.recover_splitter_execution_contract} reason={detail}")
        return
    if args.complete_splitter:
        if unknown or not args.project or not args.dispatch_token:
            raise SystemExit("splitter completion requires --project --complete-splitter --dispatch-token")
        PROJECT=args.project
        try: output=base64.b64decode(args.splitter_output_b64 or "").decode("utf-8")
        except Exception: raise SystemExit("SPLIT_FAILED invalid splitter output encoding")
        ok,detail=complete_splitter(args.complete_splitter,args.prompt or "",args.dispatch_token,output)
        print(f"SPLIT_{'ACCEPTED' if ok else 'FAILED'} parent={args.complete_splitter} result={detail}")
        if not ok: raise SystemExit(3)
        return
        return
    if args.project:
        PROJECT=args.project
    if not PROJECT:
        raise SystemExit("supervisor daemon requires --project or V2_PROJECT")
    ROOT.joinpath("logs").mkdir(parents=True,exist_ok=True); sync_global_lessons(); log(f"SUPERVISOR_START project={PROJECT!r} source=http-poll reason={HARD_REASONING_CHARS} text={HARD_TEXT_CHARS} implementation_compactions=3 fourth_compaction=retire")
    threading.Thread(target=control_guard_loop,daemon=True).start(); threading.Thread(target=persisted_reconcile_loop,daemon=True).start(); api_poll_loop()
if __name__=="__main__": main()
