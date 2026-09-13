#!/usr/bin/env python3
import argparse,base64,contextlib,hashlib,json,os,re,sqlite3,subprocess,sys,threading,time,urllib.error,urllib.parse,urllib.request
from pathlib import Path
from control_state import (phase_ready, ready_info as state_ready_info,
                           snapshot as state_snapshot, attempt_state,
                           AUTOMATIC_ATTEMPT_LIMIT,
                           MAX_INFRASTRUCTURE_RETRY_GRANTS,
                           MAX_OPERATOR_INFRASTRUCTURE_ABORTS,
                           RECURSIVE_SPLIT_PROTOCOL, MAX_SPLIT_DEPTH,
                           LEAF_READY_PROTOCOL,
                           split_depth, valid_deliverable_id)
from leaf_contract import (
    IMPLEMENTATION_ROLES, READ_ONLY_ROLES,
    strict_owned_artifact_paths as shared_strict_owned_artifact_paths,
    canonical_owned_artifacts as shared_canonical_owned_artifacts,
    validate_leaf_contract,
)
from state_io import (
    StateCorruptionError, load_json_object, atomic_write_json, atomic_write_text,
    exclusive_file_lock,
)

ROOT=Path.home()/"AI"/"opencode-qwen38-multiagent-v2"
DB=ROOT/"xdg"/"data"/"opencode"/"opencode.db"
LOG=ROOT/"logs"/"supervisor-events.log"; CSV=ROOT/"logs"/"supervisor-events.csv"; LIVE_STATUS=ROOT/"logs"/"supervisor-live.json"
PROJECT=os.environ.get("V2_PROJECT","")
START_MS=int(time.time()*1000)-5000; POLL=0.5
HARD_SECONDS=120; HARD_REASONING_CHARS=8000; HARD_TEXT_CHARS=12000
MAX_IMPLEMENTATION_PROMPT_CHARS=2500
PROBE_MAX_TOOL_TURNS_WITHOUT_DURABLE_PROGRESS=5
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
PLANNER_INITIAL_PROGRESS_GRACE_SECONDS=420
PLANNER_PROGRESS_STALL_SECONDS=300
ROOT_CONTEXT_INPUT_CEILING=43000
MAX_ROOT_RESTARTS=4
MAX_PLANNER_RESTARTS=3
# Ledger schema identifier, deliberately independent of the harness release.
# Existing historical `V2.6.7` ledgers remain readable; newly created ledgers
# use this unambiguous schema name without a destructive migration.
ATTEMPT_LEDGER_PROTOCOL="v2-attempt-ledger-v1"
SPLIT_PROPOSAL_PROTOCOL="v2-task-split-proposal-v1"
IMPLEMENTATION_AGENTS=set(IMPLEMENTATION_ROLES)
READ_ONLY_SPLIT_ROLES=set(READ_ONLY_ROLES)
MAX_CONCURRENT_IMPLEMENTATION_WORKERS=3
MAX_UNMATERIALIZED_DISPATCH_REPLAYS=MAX_INFRASTRUCTURE_RETRY_GRANTS
MAX_SPLITTER_ATTEMPTS=2
SPLITTER_LEASE_SECONDS=600
MAX_SPLIT_PARENT_FINALIZE_FAILURES=3
SPLIT_TRANSACTION_PROTOCOL="v2-split-transaction-v1"
lock=threading.RLock(); dispatch_lock=threading.RLock(); watch={}; dispatch_seen=set(); session_task={}; abort_count={}; compaction_seen={}; supervisor_abort_reasons={}
event_watch={}; event_threads={}; planner_checkpoints={}; planner_completion_seen=set(); post_finalize_seen=set(); worker_progress={}; abort_intent_lock=threading.RLock()
root_seen_active=False; root_idle_since=None; lessons_started=False; lessons_launch_attempts=0

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

Read `.opencode-v2/control-status.json` for the authoritative derived scheduler state.
Continue the ORIGINAL task from durable state only."""

PLANNER_CONTINUATION_PROMPT="""Continue implementation planning for this project.

FIRST read .opencode-v2/ORIGINAL_TASK.md.
It is the immutable authoritative original user request.
Never substitute this continuation message for the original task.

Then read:
- .opencode-v2/ACCEPTANCE.md
- .opencode-v2/CONTROL_CONTRACT.md
- .opencode-v2/IMPLEMENTATION_PLAN.md

Continue from durable file state using your progressive planner protocol."""

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

def last_assistant_text_db(sid):
    """Return concatenated text parts from the latest assistant message."""
    try:
        con=db_connect()
        rows=con.execute(
            "SELECT data FROM session_message WHERE session_id=? AND type='assistant' ORDER BY seq DESC",
            (sid,),
        ).fetchall()
        con.close()
        for (raw,) in rows:
            try: d=json.loads(raw)
            except Exception: continue
            parts=d.get("content") if isinstance(d,dict) else None
            if not isinstance(parts,list): continue
            texts=[p.get("text","") for p in parts if isinstance(p,dict) and p.get("type")=="text" and isinstance(p.get("text"),str)]
            text="\n".join(x for x in texts if x).strip()
            if text: return text
    except Exception:
        pass
    return ""

def parse_deliverable(text):
    if not text: return ""
    m=re.search(r"(?mi)^\s*DELIVERABLE\s*:\s*(D\d{3}(?:-[AB](?:[12])?)?)\s*$",text)
    return m.group(1) if m else ""

def implementation_prompt(did):
    split_child = split_depth(did) > 0
    scope_line = (
        f"Read .opencode-v2/work/{did}.scope.md; it is your authoritative split-child scope."
        if split_child
        else f"Read your {did} section in .opencode-v2/IMPLEMENTATION_PLAN.md."
    )
    return (
        f"DELIVERABLE: {did}\n"
        f"{scope_line}\n"
        f"Read .opencode-v2/work/{did}.progress.md if present.\n"
        "Inspect your owned project artifacts as they currently exist.\n"
        "Continue from actual filesystem state and execute the deliverable."
    )

def recursive_split_enabled():
    return load_manifest().get("recursive_split_protocol") == RECURSIVE_SPLIT_PROTOCOL

def leaf_automatic_limit(did):
    """Two real attempts lead to a split until the terminal split depth."""
    if recursive_split_enabled() and split_depth(did) >= 0:
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
        "children","generation","transaction_id"
    ):
        if key in previous:
            keep[key]=previous[key]
    generation=detail.pop("generation", keep.get("generation",1) or 1)
    payload={
        "owner":"supervisor",
        "parent_id":did,
        "state":state,
        "generation":generation,
        "timestamp":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime()),
        **keep,
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
        save_split_status(
            did,"split-unavailable-read-only-parent",
            generation=generation,
            reason="split parent has no durable owned artifacts to partition",
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
    for item in failures[-2:]:
        if isinstance(item,dict):
            compact.append({
                k:item.get(k)
                for k in ("attempt","classification","reason","timestamp")
            })
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
        },
        "existing_artifacts":parent_owned,
        "ownership_items":parent_owned,
        "failed_attempts":compact,
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
    retryable=claims < MAX_SPLITTER_ATTEMPTS
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
            f"# {child_id} split-child scope\n\n"
            f"Parent: {parent}\n\nScope: {child['name']}\n\n"
            f"Owned artifacts: {child['owned_artifacts']}\n\n"
            f"Verify command: `{child['verify_command']}`\n\n"
            f"Done when: {child['done_when']}\n",
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


def validate_split_proposal(parent, proposals):
    """Validate exactly two child scopes; no model-selected IDs or control edits."""
    manifest=load_manifest(); leaves=manifest.get("leaves") or {}; leaf=leaves.get(parent)
    expected=expected_children(parent)
    if not recursive_split_enabled() or not isinstance(leaf,dict) or not expected:
        raise ValueError("parent is not eligible for recursive split")
    if leaf_children(parent): raise ValueError("parent already split")
    if not isinstance(proposals,list) or len(proposals)!=2: raise ValueError("split requires exactly two proposals")
    parent_owned=set(owned_artifact_paths(leaf))
    if not parent_owned:
        raise ValueError("split parent has no validated owned artifacts")
    seen=set(); children=[]
    for index, proposal in enumerate(proposals):
        if not isinstance(proposal,dict): raise ValueError("child proposal must be an object")
        allowed={"scope","owned_artifacts","verify_command","role","depends_on_sibling","done_when"}
        if set(proposal)-allowed or not all(isinstance(proposal.get(k),str) and proposal[k].strip() for k in ("scope","owned_artifacts","verify_command","role","done_when")):
            raise ValueError("child proposal has missing or unsupported fields")
        owned_list,owned_error=_strict_owned_artifact_text(proposal["owned_artifacts"])
        if owned_error:
            raise ValueError(f"child ownership is not canonical: {owned_error}")
        contract_errors=validate_leaf_contract(proposal["role"], owned_list, proposal["verify_command"])
        if contract_errors:
            raise ValueError("; ".join(contract_errors))
        owned=set(owned_list)
        sibling=proposal.get("depends_on_sibling", "")
        if index == 1 and sibling == expected[0]:
            sibling = "first"
        if sibling not in ("", "first") or (sibling == "first" and index != 1):
            raise ValueError("only second child may depend on first child")

        read_only_role=proposal["role"] in READ_ONLY_SPLIT_ROLES
        if read_only_role and owned:
            raise ValueError(
                "read-only split role must use owned_artifacts: none; "
                "use implementer/test-builder when the child must create an artifact"
            )
        if not owned:
            if not read_only_role:
                raise ValueError(
                    "only a read-only tester split child may have owned_artifacts: none"
                )
            if proposal["owned_artifacts"].strip() != "none":
                raise ValueError(
                    "read-only tester must encode empty ownership as exact 'none'"
                )
            if index != 1 or sibling != "first":
                raise ValueError(
                    "read-only tester must be the second child and depend on the first"
                )
        else:
            if not owned <= parent_owned or seen & owned:
                raise ValueError(
                    "child ownership must be disjoint and inside parent ownership"
                )
            seen |= owned
        child=dict(leaf)
        child.update({"id":expected[index],"name":proposal["scope"],
                      "owned_artifacts":_canonical_owned_artifacts(owned_list),
                      "owned_artifact_paths":owned_list,
                      "verify_command":proposal["verify_command"],"role":proposal["role"],"done_when":proposal["done_when"],
                      "parent":parent,"split_depth":split_depth(expected[index]),"split_children":[]})
        child["launch_deps"]=list(leaf.get("launch_deps",[])) + ([expected[0]] if sibling=="first" else [])
        children.append(child)
    if seen != parent_owned: raise ValueError("child ownership must cover all unfinished parent ownership")
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

            expected,children=validate_split_proposal(parent,proposals)
            request=load_json_object(
                split_request_path(parent),label=f"split request {parent}"
            )
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
        retryable,state=record_splitter_failure(
            did,str(exc),session=session,validation=True
        )
        log(f"SPLIT_PROPOSAL_REJECTED parent={did} retryable={retryable} reason={exc}")
        return False,state


def claim_splitter(parent, dispatch_token):
    """Lease one splitter; stale leases and malformed proposals have bounded recovery."""
    if not valid_deliverable_id(parent) or not split_request_path(parent).exists():
        return False,"split-request-missing"
    with dispatch_lock:
        status=load_split_status(parent)
        state=status.get("state","split-required")
        claims=int(status.get("claim_count") or 0)
        now=time.time()
        if state=="splitter-active":
            try:
                lease_until=float(status.get("lease_until_epoch") or 0)
            except (TypeError,ValueError):
                lease_until=0
            if lease_until > now:
                return False,"splitter-active"
            if claims >= MAX_SPLITTER_ATTEMPTS:
                save_split_status(
                    parent,"splitter-failed",
                    claim_count=claims,
                    reason="splitter lease expired and claim budget is exhausted",
                    lease_until_epoch=0,
                )
                return False,"splitter-failed"
            save_split_status(
                parent,"split-retryable",
                claim_count=claims,
                reason="splitter lease expired",
                lease_until_epoch=0,
            )
            state="split-retryable"

        if state in {
            "split-validation-failed","splitter-failed",
            "split-unavailable-read-only-parent","parent-finalize-failed",
        }:
            return False,state
        if leaf_children(parent):
            return False,"already-split"
        if claims >= MAX_SPLITTER_ATTEMPTS:
            save_split_status(
                parent,"splitter-failed",
                claim_count=claims,
                reason="splitter claim budget exhausted",
                lease_until_epoch=0,
            )
            return False,"splitter-failed"

        claims += 1
        save_split_status(
            parent,"splitter-active",
            claim_count=claims,
            dispatch_token=dispatch_token,
            lease_until_epoch=now+SPLITTER_LEASE_SECONDS,
        )
    log(f"SPLITTER_CLAIM parent={parent} generation=1 token={dispatch_token} claim={claims}")
    return True,"claimed"

def parse_splitter_final_json(text):
    text=(text or "").strip()
    if text.startswith("```"):
        text=re.sub(r"^```(?:json)?\s*","",text,flags=re.I)
        text=re.sub(r"\s*```$","",text)
    try:
        obj=json.loads(text)
        return obj if isinstance(obj,dict) else None
    except Exception:
        pass
    a=text.find("{"); b=text.rfind("}")
    if a>=0 and b>a:
        try:
            obj=json.loads(text[a:b+1])
            return obj if isinstance(obj,dict) else None
        except Exception:
            pass
    return None


def complete_splitter(parent, session=""):
    """Completion callback: supervisor persists validated final JSON."""
    if split_proposal_path(parent).exists():
        return process_split_proposal(parent,session,require_proposal=True)
    if not split_request_path(parent).exists():
        txn=load_split_transaction(parent)
        if txn.get("state")=="committed":
            return True,"accepted"
        return False,"split-request-missing"
    payload=parse_splitter_final_json(last_assistant_text_db(session)) if session else None
    if not isinstance(payload,dict):
        _,state=record_splitter_failure(
            parent,"splitter-completed-without-json-proposal",
            session=session,validation=False,
        )
        return False,state
    atomic_write_json(split_proposal_path(parent),payload)
    log(f"SPLIT_PROPOSAL_PERSISTED_BY_SUPERVISOR parent={parent} session={session}")
    return process_split_proposal(parent,session,require_proposal=True)


def reconcile_split_proposals():
    """Crash recovery for prepared transactions and proposal/request state."""
    if not PROJECT:
        return
    reconcile_required_splits()
    reconcile_split_transactions()
    for request in (Path(PROJECT)/".opencode-v2"/"work").glob("D*.split-request.json"):
        did=request.name.removesuffix(".split-request.json")
        status=load_split_status(did)
        state=status.get("state")
        if state in {
            "split-validation-failed","splitter-failed",
            "split-unavailable-read-only-parent",
        }:
            continue
        if state=="splitter-active":
            try:
                expired=float(status.get("lease_until_epoch") or 0) <= time.time()
            except (TypeError,ValueError):
                expired=True
            if expired:
                claims=int(status.get("claim_count") or 0)
                if claims >= MAX_SPLITTER_ATTEMPTS:
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
        if split_proposal_path(did).exists():
            process_split_proposal(did)


def record_leaf_failure(did, reason, classification="genuine"):
    """Record a terminal worker outcome; the split-required edge is durable in the ledger."""
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
            attempt=int(entry.get("count") or 0)
            already=any(
                isinstance(x,dict) and x.get("attempt")==attempt
                for x in history
            )
            if not already:
                history.append({
                    "attempt":attempt,
                    "classification":classification,
                    "reason":reason,
                    "timestamp":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime()),
                    "source":"supervisor",
                })
            if classification=="genuine":
                genuine=sum(
                    1 for x in history
                    if isinstance(x,dict) and x.get("classification")=="genuine"
                )
                if (
                    recursive_split_enabled()
                    and genuine>=2
                    and split_depth(did)<MAX_SPLIT_DEPTH
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

def retryable_unmaterialized_dispatch_entry(entry):
    """True when the current attempt was only preclaimed and never materialized."""
    if not isinstance(entry,dict):
        return False
    try:
        count=int(entry.get("count",0))
    except Exception:
        return False
    if count <= 0:
        return False
    sessions=entry.get("sessions")
    if not isinstance(sessions,list) or not sessions:
        return False
    current=sessions[-1]
    if not (isinstance(current,str) and current.startswith("dispatch:")):
        return False
    classified=set()
    for item in entry.get("failure_history",[]):
        if not isinstance(item,dict):
            continue
        try:
            classified.add(int(item.get("attempt",0)))
        except Exception:
            pass
    if count in classified:
        return False
    seq=entry.get("unmaterialized_dispatch_sequence")
    try:
        replays=int(entry.get("unmaterialized_dispatch_replays",0)) if int(seq or 0)==count else 0
    except Exception:
        replays=0
    return replays < MAX_UNMATERIALIZED_DISPATCH_REPLAYS


def normalized_state_snapshot(project):
    data=state_snapshot(project)
    if not isinstance(data,dict):
        return data

    leaves=data.get("leaves") if isinstance(data.get("leaves"),dict) else {}
    active=active_implementation_deliverables()
    active_ids=set(active)
    active_count=len(active)
    data["scheduler"]={
        "max_concurrent_workers":MAX_CONCURRENT_IMPLEMENTATION_WORKERS,
        "active_workers":active_count,
        "available_worker_slots":max(0,MAX_CONCURRENT_IMPLEMENTATION_WORKERS-active_count),
        "active_deliverables":sorted(active_ids),
    }
    for did in active_ids:
        if isinstance(leaves.get(did),dict):
            leaves[did]["running"]=True
            leaves[did]["eligible"]=False

    try:
        attempt_entries=(load_attempts().get("deliverables") or {})
    except Exception:
        attempt_entries={}
    for did,leaf in leaves.items():
        if not isinstance(leaf,dict) or did in active_ids:
            continue
        entry=attempt_entries.get(did)
        if not retryable_unmaterialized_dispatch_entry(entry):
            continue
        if leaf.get("complete") or leaf.get("split_required") or leaf.get("launch_deps_missing"):
            continue
        leaf["attempt_limit_reached"]=False
        leaf["eligible"]=True
        leaf["unmaterialized_dispatch_reusable"]=True
        log(f"STATE_UNMATERIALIZED_DISPATCH_REUSABLE deliverable={did} attempts={leaf.get('attempts',0)}")

    clean=[]
    for item in data.get("execution_blockers",[]) if isinstance(data.get("execution_blockers"),list) else []:
        if not isinstance(item,dict):
            continue
        did=str(item.get("deliverable") or "")
        leaf=leaves.get(did) if isinstance(leaves.get(did),dict) else {}
        # New13 regression: control_state emitted attempt_limit_reached for
        # leaves whose own canonical flag was false (often attempts=0).
        if did in active_ids:
            log(
                f"STATE_BLOCKER_EXEMPT_ACTIVE deliverable={did} "
                f"reason={item.get('reason','')}"
            )
            continue
        if item.get("reason")=="attempt_limit_reached" and not leaf.get("attempt_limit_reached",False):
            log(
                f"STATE_BLOCKER_EXEMPT deliverable={did} reason=attempt_limit_reached "
                f"attempts={leaf.get('attempts',0)}"
            )
            continue
        clean.append(item)
    data["execution_blockers"]=clean

    # If the only reason for execution-blocked was the contradictory blocker
    # list and at least one leaf is legally eligible, execution can continue.
    if data.get("resume_phase")=="execution-blocked" and not clean:
        if active_ids or any(isinstance(v,dict) and v.get("eligible") for v in leaves.values()):
            data["resume_phase"]="execution"
    return data
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
        return payload
    except Exception as exc:
        payload=control_state_error_payload(exc)
        try:
            atomic_write_json(path,payload)
        except Exception as write_exc:
            log(f"CONTROL_STATUS_ERROR_WRITE_FAILED source={exc!r} write={write_exc!r}")
        log(f"CONTROL_STATUS_SNAPSHOT_ERROR {exc!r}")
        return payload
# V2.6.9 DURABLE CONTROL STATUS SNAPSHOT END

def ready_info(did):
    return state_ready_info(PROJECT,did) if PROJECT and did else {}

def plan_ready():
    return bool(PROJECT) and phase_ready(
        PROJECT,"IMPLEMENTATION_PLAN.ready","IMPLEMENTATION_PLAN.md",
        "IMPLEMENTATION_PLAN_COMPLETE",
    )

PLANNER_CHECKPOINT_RE=re.compile(
    r"(?ms)(^##\s+Planner checkpoint\s*\nStatus:\s*)([^\n]*)(\n)"
)
PLAN_DELIVERABLE_RE=re.compile(r"(?m)^###\s+D\d{3}\s+[—-]\s+\S")

def planner_plan_state(plan_path):
    """Classify bootstrap, engagement, and actual durable plan structure."""
    try: text=Path(plan_path).read_text(errors="replace")
    except OSError: return {"full_signature":None,"meaningful_signature":None,"engaged":False}
    full_signature=hashlib.sha256(text.encode()).hexdigest()
    match=PLANNER_CHECKPOINT_RE.search(text)
    engaged=bool(match and match.group(2).strip().upper()!="BOOTSTRAP")
    # A checkpoint-status mutation proves the model reached its file tool, but
    # cannot by itself reset the plan-progress timer.  Normalize that status so
    # only real plan content can create a meaningful fingerprint.
    normalized=PLANNER_CHECKPOINT_RE.sub(r"\1<checkpoint>\3",text)
    meaningful=bool(PLAN_DELIVERABLE_RE.search(normalized))
    return {
        "full_signature":full_signature,
        "meaningful_signature":(
            hashlib.sha256(normalized.encode()).hexdigest() if meaningful else None
        ),
        "engaged":engaged,
    }

def planner_progress_reason(sid,elapsed,plan_path=None):
    """Apply the one planner-owned durable-progress policy.

    Bootstrap is immediately restartable. A checkpoint mutation proves tool
    engagement but does not buy more time: the first actual Dxxx structure is
    due within the initial grace. Thereafter only meaningful-plan fingerprints
    reset the 300-second stall timer.
    """
    plan_path=Path(plan_path or (Path(PROJECT)/".opencode-v2/IMPLEMENTATION_PLAN.md"))
    plan=planner_plan_state(plan_path)
    state=planner_checkpoints.setdefault(sid,{})
    if "baseline_full_signature" not in state:
        has_meaningful=bool(plan["meaningful_signature"])
        state.update(
            baseline_full_signature=plan["full_signature"],
            last_meaningful_signature=plan["meaningful_signature"],
            last_progress=elapsed, model_progress=has_meaningful,
            engagement=plan["engaged"],
        )
        return ""
    state["engagement"]=plan["engaged"]
    if not state.get("model_progress"):
        if plan["meaningful_signature"]:
            state.update(
                model_progress=True,
                last_meaningful_signature=plan["meaningful_signature"],
                last_progress=elapsed,
            )
            return ""
        if elapsed<PLANNER_INITIAL_PROGRESS_GRACE_SECONDS: return ""
        if plan["full_signature"] is None:
            return f"planner_bootstrap_missing no_durable_progress={int(elapsed)}s"
        return (
            f"planner_no_meaningful_plan_progress elapsed={int(elapsed)}s "
            f"limit={PLANNER_INITIAL_PROGRESS_GRACE_SECONDS}s "
            f"engagement={str(state['engagement']).lower()}"
        )
    if plan["meaningful_signature"]!=state.get("last_meaningful_signature"):
        state.update(last_meaningful_signature=plan["meaningful_signature"],last_progress=elapsed)
        return ""
    waited=elapsed-state.get("last_progress",elapsed)
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

def record_planner_restart(sid,reason):
    data={"owner":"supervisor","count":planner_restart_count()+1,"retired_session":sid,"reason":reason}
    atomic_write_json(planner_restart_path(),data)

def planner_retirement_reason(sid,elapsed,plan_path=None):
    """One shared planner invariant for fresh and replacement sessions."""
    if planner_restart_count()>=MAX_PLANNER_RESTARTS and not plan_ready():
        return f"planner_restart_limit={MAX_PLANNER_RESTARTS}"
    return planner_progress_reason(sid,elapsed,plan_path)

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


def ownership_baseline_path(did):
    return Path(PROJECT)/".opencode-v2/work"/f"{did}.ownership-baseline.json"

def project_fingerprints():
    """Fingerprint regular project files for a claimed leaf's ownership check."""
    root=Path(PROJECT); result={}
    for path in root.rglob("*"):
        if not path.is_file() or ".git" in path.parts:
            continue
        relative=path.relative_to(root).as_posix()
        if relative.startswith(".opencode-v2/work/"):
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

def session_window(sid):
    try:
        con=db_connect()
        row=con.execute("SELECT time_created,time_idle FROM session_v2 WHERE id=?",(sid,)).fetchone()
        con.close()
        if not row: return None
        start=int(row[0] or 0); end=int(row[1] or int(time.time()*1000))
        return start,end
    except Exception:
        return None

def session_tool_inputs(sid):
    """Return only tool INPUTS issued by this session, never tool outputs."""
    out=[]
    try:
        con=db_connect()
        rows=con.execute("SELECT data FROM session_message WHERE session_id=? AND type='assistant' ORDER BY seq",(sid,)).fetchall()
        con.close()
        for (raw,) in rows:
            try: d=json.loads(raw)
            except Exception: continue
            for part in d.get("content",[]) if isinstance(d,dict) else []:
                if not isinstance(part,dict) or part.get("type")!="tool": continue
                state=part.get("state") if isinstance(part.get("state"),dict) else {}
                inp=state.get("input") if isinstance(state.get("input"),dict) else {}
                out.append((str(part.get("name") or ""),inp))
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
    """Owned paths of implementation sessions whose lifetime overlapped sid."""
    win=session_window(sid)
    if not win or not PROJECT: return set()
    start,end=win; result=set()
    try:
        con=db_connect()
        rows=con.execute(
            "SELECT id,time_created,time_idle FROM session_v2 WHERE parent_id IS NOT NULL AND directory=? AND time_created>=? AND id<>?",
            (PROJECT,START_MS,sid),
        ).fetchall()
        con.close()
    except Exception:
        return result
    leaves=(load_manifest().get("leaves") or {})
    for other,created,idle in rows:
        ostart=int(created or 0); oend=int(idle or int(time.time()*1000))
        if ostart>end or oend<start: continue
        odid=parse_deliverable(strip_subagent_prefix(first_user_text_db(other)))
        if not odid or odid==did: continue
        leaf=leaves.get(odid)
        if not isinstance(leaf,dict): continue
        result.update(owned_artifact_paths(leaf))
    return result

# V2.6.9 SUPERVISOR DYNAMIC OWNERSHIP EXEMPTION BEGIN
SUPERVISOR_DYNAMIC_CONTROL_PATHS={
    ".opencode-v2/control-status.json",
    ".opencode-v2/reference-gate.json",
    ".opencode-v2/reference-validation-gate.json",
    ".opencode-v2/IMPLEMENTATION_PLAN.guard.json",
}

def supervisor_dynamic_control_path(path):
    return path in SUPERVISOR_DYNAMIC_CONTROL_PATHS
# V2.6.9 SUPERVISOR DYNAMIC OWNERSHIP EXEMPTION END

def ownership_violations(did,sid=""):
    """Return changes attributable to this leaf outside declared ownership.

    Whole-project snapshots are retained, but files owned by a genuinely
    overlapping sibling are not blamed on this worker unless this worker's own
    tool INPUT explicitly targeted that path. This preserves parallel waves
    without the gametestNew4 cross-agent false-positive failure mode.
    """
    try:
        baseline=json.loads(ownership_baseline_path(did).read_text())
        before=baseline.get("files") if baseline.get("owner")=="supervisor" else None
        if not isinstance(before,dict): return ["invalid ownership baseline"]
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
def probe_loop_reason(sid,agent,did,tool_id,now=None):
    """Stop probe read/research loops before their finite OpenCode step budget."""
    if agent!="probe-builder" or not did: return ""
    now=time.monotonic() if now is None else now
    signature=durable_progress_signature(agent,did)
    state=worker_progress.setdefault(sid,{"signature":signature,"turns":0,"last_tool":""})
    if signature!=state["signature"]:
        state.update(signature=signature,turns=0,last_tool=tool_id or "")
        return ""
    if tool_id and tool_id!=state.get("last_tool"):
        state["last_tool"]=tool_id; state["turns"]+=1
    if state["turns"]>=PROBE_MAX_TOOL_TURNS_WITHOUT_DURABLE_PROGRESS:
        return ("probe_research_loop_no_owned_progress "
                f"tool_turns={state['turns']} limit={PROBE_MAX_TOOL_TURNS_WITHOUT_DURABLE_PROGRESS}")
    return ""

def supervisor_finalize_ready(did):
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

def post_session_finalize(did,sid="",runner=subprocess.run):
    """Verify actual owned files, then let the canonical leaf guard create ready."""
    leaf=(load_manifest().get("leaves") or {}).get(did)
    if not leaf or ready_info(did): return False,"not-applicable"
    paths=owned_artifact_paths(leaf)
    read_only_no_artifacts=(
        leaf.get("role") in READ_ONLY_SPLIT_ROLES and not paths
    )
    if not read_only_no_artifacts and (
        not paths or any(not (Path(PROJECT)/path).exists() for path in paths)
    ):
        progress=Path(PROJECT)/".opencode-v2/work"/f"{did}.progress.md"
        if progress.exists() and progress.stat().st_size:
            return False,"durable-progress-incomplete"
        return False,"owned-artifacts-missing"
    violations=ownership_violations(did,sid)
    if violations:
        return False,"ownership-violation:"+",".join(violations[:4])
    command=(leaf.get("verify_command") or "").strip()
    if not command: return False,"verify-command-missing"
    try:
        checked=runner(command,cwd=PROJECT,shell=True,executable="/bin/bash",timeout=240)
        if checked.returncode!=0: return False,f"verify-failed-{checked.returncode}"
        return supervisor_finalize_ready(did)
    except (OSError,subprocess.TimeoutExpired) as e:
        return False,f"verification-error-{type(e).__name__}"

# V2.6.9 GAMETESTNEW6 SPLIT-PARENT FINALIZATION BEGIN
_split_parent_finalize_next = {}


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

        ok,detail=post_session_finalize(did)
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
            save_split_status(
                did,"accepted",
                children=children,
                parent_finalize_failures=0,
                parent_finalize_last_result="finalized",
            )
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
    """
    if not did or ready_info(did): return False,"already-complete-or-unknown"
    leaf=(load_manifest().get("leaves") or {}).get(did)
    paths=owned_artifact_paths(leaf)
    progress=Path(PROJECT)/".opencode-v2/work"/f"{did}.progress.md"
    durable_present=bool(
        (paths and any((Path(PROJECT)/path).exists() for path in paths))
        or progress.exists()
    )
    with dispatch_lock:
        with attempt_lock():
            data=load_attempts()
            entry=(data.get("deliverables") or {}).get(did)
            if not isinstance(entry,dict) or sid not in entry.get("sessions",[]):
                return False,"session-not-in-ledger"
            state=attempt_state(entry)
            if not state["valid"]: return False,"attempt-ledger-invalid"
            failures=entry.setdefault("infrastructure_failures",[])
            if any(isinstance(item,dict) and item.get("session")==sid for item in failures):
                return False,"already-recorded"
            if state["infrastructure_retry_grants"] >= MAX_INFRASTRUCTURE_RETRY_GRANTS:
                return False,"infrastructure-retry-limit"
            failures.append({
                "timestamp":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime()),
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
    log(f"INFRASTRUCTURE_RETRY_GRANT session={sid} deliverable={did} kind={kind} grant=1 reason={reason}")
    csv("INFRASTRUCTURE_RETRY_GRANT",sid,"supervisor",f"{did} {kind} grant=1 reason={reason}")
    return True,"granted"

def record_compaction_infrastructure_failure(sid,did):
    """Compatibility wrapper for a failed beta compaction template."""
    return record_infrastructure_abort(sid,did,"opencode-compaction-template","opencode-compaction-template")

def compaction_failure(sid):
    """Return the terminal beta compaction failure type, if any."""
    try:
        con=db_connect()
        row=con.execute(
            "SELECT data FROM session_message WHERE session_id=? AND type='compaction' ORDER BY seq DESC LIMIT 1",
            (sid,),
        ).fetchone(); con.close()
        data=json.loads(row[0]) if row else {}
        error=data.get("error") if isinstance(data.get("error"),dict) else {}
        return error.get("type") if data.get("status")=="failed" else ""
    except Exception:
        return ""

def durable_progress_signature(agent,did=""):
    paths=[]
    if agent=="implementation-planner":
        paths=[Path(PROJECT)/".opencode-v2/IMPLEMENTATION_PLAN.md"]
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
    """Persisted OpenCode heartbeat for invisible-stream fallback.

    A child can be healthy and actively producing completed model/tool turns
    while the beta HTTP/SSE view is temporarily not observable.  MAX(seq) and
    row count advance whenever such a turn is persisted, so they are a safe
    heartbeat without pretending that read-only activity is durable work.
    """
    try:
        con=db_connect()
        row=con.execute(
            "SELECT COALESCE(MAX(seq),-1),COUNT(*) FROM session_message WHERE session_id=?",
            (sid,),
        ).fetchone()
        con.close()
        return tuple(row or (-1,0))
    except Exception:
        return (-1,0)

def fallback_no_progress_reason(sid,agent,did,observable,now=None):
    """Bound invisible streams without treating a brief API gap as a failure."""
    state=event_watch.setdefault(sid,{"stop":threading.Event(),"created":time.monotonic()})
    if observable:
        state.pop("fallback_since",None); state.pop("fallback_signature",None); return ""
    now=time.monotonic() if now is None else now
    signature=(durable_progress_signature(agent,did),session_activity_signature(sid))
    if signature!=state.get("fallback_signature"):
        state["fallback_signature"]=signature; state["fallback_since"]=now; return ""
    state.setdefault("fallback_since",now)
    limit=600 if (agent=="reference-researcher" or agent in IMPLEMENTATION_AGENTS) else 300
    if now-state["fallback_since"]>=limit:
        return f"no_durable_progress_with_invisible_stream={int(now-state['fallback_since'])}s"
    return ""

def effective_fallback_reason(sid,agent,did,observable,now=None):
    """Keep invisible-stream fallback from contradicting planner supervision.

    Implementation planners have a more specific, filesystem-content policy:
    420 seconds to their first Dxxx structure, then 300 seconds per meaningful
    plan change. The generic artifact fallback is the same failure condition,
    so it is intentionally inapplicable to that role. SSE/context/output
    watchdogs remain independent and active.
    """
    if agent=="implementation-planner": return ""
    return fallback_no_progress_reason(sid,agent,did,observable,now)

def attempts_path(): return Path(PROJECT)/".opencode-v2"/"work"/"attempts.json"
def load_attempts():
    return load_json_object(
        attempts_path(),
        default_missing={"protocol":ATTEMPT_LEDGER_PROTOCOL,"deliverables":{}},
        label="attempt ledger",
    )
def save_attempts(data):
    atomic_write_json(attempts_path(),data)

@contextlib.contextmanager
def attempt_lock():
    """Cross-process kernel lock; stale lock files cannot deadlock recovery."""
    path=attempts_path().with_name("attempts.json.lock")
    with exclusive_file_lock(path,timeout=5.0):
        yield

def validate_dispatch(agent,text):
    """Validate a planned Dxxx dispatch before the child model can start."""
    normalized=normalize_implementation_prompt(text)
    violation=implementation_prompt_violation(normalized)
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
    if ready_info(did): return did,"already_complete"
    return did,""

# V2.6.9 THREE-SLOT IMPLEMENTATION SCHEDULER BEGIN
def active_implementation_sessions():
    """Return live implementation child sessions for this project."""
    if not PROJECT:
        return []
    try:
        con=db_connect()
        rows=con.execute(
            "SELECT id,coalesce(agent,'') FROM session_v2 "
            "WHERE parent_id IS NOT NULL AND directory=? AND time_idle IS NULL",
            (PROJECT,),
        ).fetchall()
        con.close()
    except Exception:
        return []
    return [(sid,agent) for sid,agent in rows if agent in IMPLEMENTATION_AGENTS]

def active_implementation_deliverables():
    result={}
    for sid,agent in active_implementation_sessions():
        did=parse_deliverable(strip_subagent_prefix(first_user_text_db(sid)))
        if did:
            result[did]={"session":sid,"agent":agent}
    return result

def available_implementation_slots():
    return max(0,MAX_CONCURRENT_IMPLEMENTATION_WORKERS-len(active_implementation_sessions()))
# V2.6.9 THREE-SLOT IMPLEMENTATION SCHEDULER END

def preclaim_attempt(agent,text,dispatch_token):
    """Reserve a canonical attempt before OpenCode creates its child session."""
    did,violation=validate_dispatch(agent,text)
    if violation: return "denied",did,violation,0
    if agent in IMPLEMENTATION_AGENTS and available_implementation_slots() <= 0:
        return "denied",did,"worker_slots_full",0
    claim,n=claim_attempt(f"dispatch:{dispatch_token}",did)
    if claim in {"claimed","existing"}:
        if claim=="claimed":
            write_ownership_baseline(did)
        authorized = n > AUTOMATIC_ATTEMPT_LIMIT
        suffix = " operator_authorized=true" if authorized else ""
        log(f"DISPATCH_CLAIM token={dispatch_token} agent={agent} deliverable={did} attempt={n}{suffix}")
        csv("DISPATCH_CLAIM",f"dispatch:{dispatch_token}",agent,f"{did} attempt={n}{suffix}")
        return "claimed",did,"",n
    return "denied",did,("attempt_limit" if claim=="limit" else "attempt_ledger_invalid"),n

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
                pending=[item for item in ent.get("operator_retry_attempts",[])
                         if isinstance(item,dict) and item.get("state")=="reserved" and
                         isinstance(item.get("session"),str) and item["session"].startswith("dispatch:")]
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
                uses_operator=(count > state["automatic_limit"] + state["infrastructure_retry_grants"])
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
            for item in entry.get("operator_retry_attempts",[]):
                if isinstance(item,dict) and item.get("session")==sid and item.get("state")=="reserved":
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

def durable_worker_execution(did):
    """A project-local artifact/progress change is the durable consumption point."""
    leaf=(load_manifest().get("leaves") or {}).get(did,{})
    paths=[Path(PROJECT)/p for p in owned_artifact_paths(leaf)]
    paths.append(Path(PROJECT)/".opencode-v2/work"/f"{did}.progress.md")
    return any(path.exists() and (not path.is_file() or path.stat().st_size > 0) for path in paths)

def meaningful_worker_execution(sid,did):
    """Return the first durable or completed-tool execution boundary.

    Durable owned state is preferred.  If verification later fails after a
    completed child tool action, that is still real worker execution and must
    consume the explicitly authorized retry rather than being mistaken for a
    pre-provider cancellation.
    """
    if durable_worker_execution(did):
        return "owned-artifact-or-progress"
    try:
        con=db_connect(); rows=con.execute(
            "SELECT data FROM session_message WHERE session_id=? ORDER BY seq",(sid,)
        ).fetchall(); con.close()
        for (raw,) in rows:
            data=json.loads(raw) if raw else {}
            content=data.get("content") if isinstance(data,dict) else []
            if not isinstance(content,list): continue
            for part in content:
                if not isinstance(part,dict) or part.get("type")!="tool": continue
                state=part.get("state") if isinstance(part.get("state"),dict) else {}
                if state.get("status") != "running": return "completed-worker-tool-action"
    except Exception:
        pass
    return ""

def immediate_runtime_abort(sid):
    """Return evidence only for an observed zero-work beta cancellation."""
    try:
        con=db_connect(); rows=con.execute(
            "SELECT type,data FROM session_message WHERE session_id=? ORDER BY seq",(sid,)
        ).fetchall(); con.close()
        assistants=[]
        for kind,raw in rows:
            data=json.loads(raw) if raw else {}
            if kind=="assistant": assistants.append(data if isinstance(data,dict) else {})
            if isinstance(data,dict) and data.get("type")=="tool": return ""
            if (isinstance(data,dict) and isinstance(data.get("content"),list) and
                    any(isinstance(part,dict) and part.get("type")=="tool" for part in data["content"])):
                return ""
        if len(assistants)!=1: return ""
        msg=assistants[0]; err=msg.get("error") if isinstance(msg.get("error"),dict) else {}
        content=msg.get("content") if isinstance(msg.get("content"),list) else []
        tokens=msg.get("tokens") if isinstance(msg.get("tokens"),dict) else {}
        if (msg.get("finish")=="error" and err.get("type")=="aborted" and not content and
                not any(int(tokens.get(k) or 0) for k in ("input","output","reasoning","cache"))):
            return "immediate-runtime-cancel zero-token-zero-tool aborted"
    except Exception:
        return ""
    return ""

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

    def stream_session_events(self,sid,on_event,stop):
        """Consume transient deltas that are intentionally absent from history."""
        if not self.ensure(): return
        qsid=urllib.parse.quote(sid)
        req=urllib.request.Request(
            self.base+f"/api/session/{qsid}/event",
            method="GET",headers={**self.headers(),"Accept":"text/event-stream"},
        )
        with urllib.request.urlopen(req,timeout=30) as response:
            for raw in response:
                if stop.is_set(): return
                line=raw.decode("utf-8","replace").strip()
                if not line.startswith("data:"): continue
                try: event=json.loads(line[5:].strip())
                except json.JSONDecodeError: continue
                if isinstance(event,dict): on_event(event)

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

    def start_agent_session(self,agent,text):
        """Create and steer a v2 session using the installed beta's SDK schema."""
        if not self.ensure():
            return False,"http-not-connected"
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
    try:
        con=db_connect()
        rows=con.execute(
            "SELECT data FROM session_message WHERE session_id=? AND type='assistant' ORDER BY seq DESC",
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
            # The newest assistant row is authoritative once parseable.
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

def reduce_live_event(state,event):
    """Track no-tool output directly from the beta's transient SSE deltas."""
    kind=event.get("type")
    data=event.get("data") if isinstance(event.get("data"),dict) else {}
    delta=data.get("delta") if isinstance(data.get("delta"),str) else ""
    if kind=="session.next.reasoning.delta":
        state["reasoning"]=state.get("reasoning",0)+len(delta)
    elif kind=="session.next.text.delta":
        state["text"]=state.get("text",0)+len(delta)
    elif kind=="session.next.tool.called":
        state["tool_running"]=True
    elif kind=="session.next.tool.success":
        state.update(reasoning=0,text=0,tool_running=False,last_progress=time.monotonic())
    elif kind=="session.next.tool.failed":
        state["tool_running"]=False
    elif kind=="session.next.step.started":
        state.update(reasoning=0,text=0,tool_running=False)
    state["last_event"]=time.monotonic()
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
    state={"reasoning":0,"text":0,"tool_running":False,"tool_successes":0,
           "connected":False,"last_event":time.monotonic(),"stop":stop}
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
    normalized=strip_subagent_prefix(first_user or first_user_text_db(sid)).strip()
    planned=agent in IMPLEMENTATION_AGENTS or bool(parse_deliverable(normalized))
    if not planned or sid in dispatch_seen:
        return

    # Live message persistence can lag /session/active. Missing prompt identity
    # is UNKNOWN, not a policy violation. Fall back to SQLite, then wait.
    text=first_user or first_user_text_db(sid)
    if not text:
        return

    did,violation=validate_dispatch(agent,text)
    if violation:
        if abort_session(sid,f"dispatch_protocol_violation {violation}",agent):
            dispatch_seen.add(sid)
        log(f"DISPATCH_DENY session={sid} agent={agent} {violation}")
        csv("DISPATCH_DENY",sid,agent,violation)
        return

    claim,n=claim_attempt(sid,did)
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

def root_orchestrator_id():
    if not PROJECT: return ""
    try:
        con=db_connect(); row=con.execute("SELECT id FROM session_v2 WHERE agent='orchestrator' AND directory=? AND time_created>=? ORDER BY time_created DESC LIMIT 1",(PROJECT,START_MS)).fetchone(); con.close(); return row[0] if row else ""
    except Exception: return ""

def root_rollover_path(): return Path(PROJECT)/".opencode-v2"/"root-rollovers.json"

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


# V2.6.9 GAMETESTNEW7 REFERENCE GATE BEGIN
REFERENCE_FOUNDATION_MARKER="<!-- REFERENCE_FOUNDATION_READY -->"


def reference_session_mode(sid):
    # Return foundation|validation for a reference-researcher session.
    try:
        con=db_connect()
        row=con.execute(
            "SELECT data FROM session_message "
            "WHERE session_id=? AND type='user' ORDER BY seq LIMIT 1",
            (sid,),
        ).fetchone()
        con.close()
        data=json.loads(row[0]) if row else {}
        text=str(data.get("text") or "")
    except Exception:
        text=""
    if "REFERENCE_MODE: VALIDATION" in text:
        return "validation"
    return "foundation"


def reference_session_made_progress(sid):
    # Did this completed reference slice persist authoritative durable state?
    try:
        con=db_connect()
        rows=con.execute(
            "SELECT data FROM session_message "
            "WHERE session_id=? AND type='assistant' ORDER BY seq",
            (sid,),
        ).fetchall()
        con.close()
    except Exception as e:
        log(f"REFERENCE_PROGRESS_DB_ERROR session={sid} error={e!r}")
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
    try:
        con=db_connect()
        rows=con.execute(
            "SELECT id,time_idle FROM session_v2 "
            "WHERE agent='reference-researcher' AND directory=? "
            "ORDER BY time_created",
            (PROJECT,),
        ).fetchall()
        con.close()
    except Exception as e:
        log(f"REFERENCE_GATE_DB_ERROR {e!r}")
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
    acceptance=ctrl/"ACCEPTANCE.md"
    text=acceptance.read_text(errors="replace") if acceptance.exists() else ""
    if "Reference policy: external-required" not in text:
        return {"state":"not-required","attempts":0,
                "max_attempts":MAX_REFERENCE_FOUNDATION_SESSIONS}

    evidence,parse_state=_reference_evidence()
    foundation=ctrl/"REFERENCE_FOUNDATION.md"
    foundation_text=foundation.read_text(errors="replace") if foundation.exists() else ""
    foundation_result=str(evidence.get("foundation_result") or "").upper()

    completed,active,productive,stagnant=_reference_progress_stats("foundation")
    ready=(
        foundation.exists()
        and REFERENCE_FOUNDATION_MARKER in foundation_text
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
    acceptance=ctrl/"ACCEPTANCE.md"
    text=acceptance.read_text(errors="replace") if acceptance.exists() else ""
    if "Reference policy: external-required" not in text:
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
        log(f"ROOT_CONTINUATION_LIMIT phase={phase} count={root_restart_count()}")
        return False
    ok,detail=http.start_agent_session("orchestrator",ROOT_CONTINUATION_PROMPT)
    if not ok:
        log(f"ROOT_CONTINUATION_FAILED phase={phase} detail={detail}")
        root_idle_since=time.time(); return False
    record_root_restart(detail,phase); root_idle_since=None
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

def api_poll_loop():
    while True:
        try:
            statuses=http.get_status()
            active=set(statuses)
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
                can_watch=(bool(parent) and not shape.get("assistant_completed")
                           and not tool_running
                           and (shape.get("observable") or live_observable))

                age,st=watchdog_age(sid,key,can_watch)
                reasoning=max(shape["reasoning"],int((live or {}).get("reasoning",0)))
                text_chars=max(shape["text"],int((live or {}).get("text",0)))
                fallback_reason=effective_fallback_reason(
                    sid,agent,did,shape.get("observable") or live_observable
                ) if parent else ""
                planner_retired=False

                if agent=="implementation-planner" and parent:
                    existing_plan=Path(PROJECT)/".opencode-v2/IMPLEMENTATION_PLAN.md"
                    checkpoint=planner_checkpoints.setdefault(
                        sid,{"started":time.monotonic()}
                    )
                    checkpoint.setdefault("started",time.monotonic())
                    progress_reason=planner_retirement_reason(
                        sid,time.monotonic()-checkpoint["started"],existing_plan
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
                            reason=f"no_tool_age={int(age)}s"

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

                rows[sid]={
                    "session":sid,
                    "agent":agent,
                    "parent":parent,
                    "deliverable":did,
                    "attempt":attempt,
                    "reasoning":reasoning,
                    "text":text_chars,
                    "tool_running":tool_running,
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
            log(f"HTTP_POLL_ERROR {e!r}")

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
    if not plan_ready():
        current_plan=planner_plan_state(
            Path(PROJECT)/".opencode-v2/IMPLEMENTATION_PLAN.md"
        )
        if not current_plan.get("meaningful_signature"):
            if planner_restart_count()<MAX_PLANNER_RESTARTS:
                record_planner_restart(
                    sid,
                    "planner_completed_without_meaningful_plan_progress",
                )
            log(
                f"PLANNER_COMPLETED_NO_PROGRESS session={sid} "
                f"restart_count={planner_restart_count()}"
            )
            csv(
                "PLANNER_COMPLETED_NO_PROGRESS",sid,"implementation-planner",
                f"restart_count={planner_restart_count()}",
            )
    # Mark only after all durable processing above succeeded.
    planner_completion_seen.add(sid)


def reconcile_idle_implementation_session(sid,agent):
    if sid in post_finalize_seen:
        return
    did=session_task.get(
        sid,(parse_deliverable(first_user_text_db(sid)),0)
    )[0]
    if not did:
        post_finalize_seen.add(sid)
        return

    abort_reason=immediate_runtime_abort(sid)
    cached_abort=supervisor_abort_reasons.get(sid,"")
    durable_abort=persisted_abort_reason(sid)
    supervisor_abort=cached_abort or durable_abort
    infrastructure_reason=abort_reason or supervisor_abort

    if infrastructure_reason and not ready_info(did):
        finalized=False
        detail="not-attempted"
        if durable_worker_execution(did):
            finalized,detail=post_session_finalize(did,sid=sid)
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
                    did,infrastructure_reason,"infrastructure"
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
            log(
                f"POST_SESSION_VERIFY session={sid} "
                f"deliverable={did} result={detail}"
            )
            csv("POST_SESSION_VERIFY",sid,agent,f"{did} {detail}")
            if not ok and detail != "not-applicable":
                classification=(
                    "bad-plan"
                    if detail in {
                        "verify-command-missing","owned-artifacts-missing"
                    } and not meaningful_worker_execution(sid,did)
                    else "genuine"
                )
                recorded,outcome=record_leaf_failure(
                    did,detail,classification
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
            "infrastructure-classified"
            if infrastructure_reason else
            "completed-without-observed-abort"
        )
    else:
        # Close a requested intent that never produced an aborted terminal
        # session (e.g. crash before the external interrupt happened).
        resolve_abort_intent(sid,"completed-without-observed-abort")
    post_finalize_seen.add(sid)


def reconcile_compaction_event(sid,agent,comps):
    prev=compaction_seen.get(sid,0)
    if comps<=prev:
        return
    did,_=session_task.get(
        sid,(parse_deliverable(first_user_text_db(sid)),0)
    )
    if did and ready_info(did):
        log(
            f"COMPACTION_AFTER_DONE session={sid} agent={agent} "
            f"deliverable={did} compactions={comps}"
        )
        compaction_seen[sid]=comps
        return
    if agent=="implementation-planner" and plan_ready():
        log(
            f"COMPACTION_AFTER_DONE session={sid} agent={agent} control_ready=1"
        )
        compaction_seen[sid]=comps
        return
    failed=compaction_failure(sid)
    if failed:
        granted,detail=(False,"not-implementation-child")
        if failed=="compaction.failed" and did and agent in IMPLEMENTATION_AGENTS:
            granted,detail=record_compaction_infrastructure_failure(sid,did)
            if granted or detail=="already-recorded":
                record_leaf_failure(
                    did,"opencode-compaction-template","infrastructure"
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
        compaction_seen[sid]=comps
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
    compaction_seen[sid]=comps


def persisted_reconcile_loop():
    while not DB.exists(): time.sleep(0.5)
    while True:
        try:
            reconcile_split_proposals()
            reconcile_split_parent_completions()
            sync_control_status_snapshot()
            pending_sessions=pending_ledger_session_ids()
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
                # Normal sessions belong to this supervisor epoch.  Additionally,
                # reconcile the exact current unclassified ledger session even if
                # it predates a supervisor restart.
                if int(time_created or 0) < START_MS and sid not in pending_sessions:
                    continue
                prompt=first_user_text_db(sid)
                if (
                    agent in IMPLEMENTATION_AGENTS
                    or parse_deliverable(strip_subagent_prefix(prompt))
                ) and sid not in dispatch_seen:
                    enforce_assignment(sid,agent,prompt)
                if agent=="implementation-planner" and time_idle:
                    reconcile_planner_completion(sid)
                if agent in IMPLEMENTATION_AGENTS and time_idle:
                    reconcile_idle_implementation_session(sid,agent)
                reconcile_compaction_event(sid,agent,comps)
        except Exception as e: log(f"PERSISTED_RECONCILE_ERROR {e!r}")
        time.sleep(0.5)

def main():
    global PROJECT
    ap=argparse.ArgumentParser(add_help=False)
    ap.add_argument("--claim-dispatch"); ap.add_argument("--claim-splitter"); ap.add_argument("--complete-splitter")
    ap.add_argument("--agent")
    ap.add_argument("--prompt"); ap.add_argument("--project")
    args,unknown=ap.parse_known_args()
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
        match=re.fullmatch(r"\s*SPLIT_PARENT:\s*(D\d{3}(?:-[AB](?:[12])?)?)\s*",args.prompt)
        if not match: raise SystemExit("SPLIT_DENY invalid splitter prompt")
        ok,detail=claim_splitter(match.group(1),args.claim_splitter)
        if not ok: raise SystemExit(f"SPLIT_DENY parent={match.group(1)} reason={detail}")
        print(f"SPLIT_ALLOW parent={match.group(1)} generation=1")
        return
    if args.complete_splitter:
        if unknown or not args.project:
            raise SystemExit("splitter completion requires --project --complete-splitter")
        PROJECT=args.project
        ok,detail=complete_splitter(args.complete_splitter,args.prompt or "")
        print(f"SPLIT_{'ACCEPTED' if ok else 'FAILED'} parent={args.complete_splitter} result={detail}")
        return
    ROOT.joinpath("logs").mkdir(parents=True,exist_ok=True); sync_global_lessons(); log(f"SUPERVISOR_START project={PROJECT!r} source=http-poll reason={HARD_REASONING_CHARS} text={HARD_TEXT_CHARS} implementation_compactions=3 fourth_compaction=retire")
    threading.Thread(target=control_guard_loop,daemon=True).start(); threading.Thread(target=persisted_reconcile_loop,daemon=True).start(); api_poll_loop()
if __name__=="__main__": main()
